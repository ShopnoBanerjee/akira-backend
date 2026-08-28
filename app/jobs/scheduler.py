"""The in-process scheduler.

APScheduler on the API's own event loop. Stage 1 runs a single instance, and
that is a constraint rather than a preference: two replicas with this enabled
would both materialise at 05:00 and both send the digest at 09:00. The
materialiser is idempotent so the first is only wasteful, but the second means
every manager gets the digest twice. Before a second instance exists, this
needs a shared advisory lock. `SCHEDULER_ENABLED` is the switch.

**Job times come from settings, and settings can change.** `jobs.materialise_time`
and `jobs.digest_time` are editable on the admin screen, so a schedule fixed at
startup would silently ignore an edit until somebody restarted the API — an
admin changing a time and watching nothing happen is worse than not offering
the setting at all. A cheap reconciler re-reads them every few minutes and
re-triggers anything that moved.

Times are Asia/Kolkata, and the materialiser must not be moved before 05:00:
earlier than the rollover and it would create the runs on the previous trading
day. The registry says so; nothing here enforces it, because an admin who
really wants 05:30 should get 05:30.
"""

import logging
from dataclasses import dataclass
from datetime import time
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.business_date import OUTLET_TZ
from app.core.config import get_settings
from app.core.db import get_session_factory
from app.core.settings_value import resolve_int, resolve_time
from app.jobs import tasks

logger = logging.getLogger(__name__)

RECONCILE = "reconcile_schedule"
RECONCILE_MINUTES = 5

_scheduler: AsyncIOScheduler | None = None

#: The schedule currently installed. Compared against on every reconcile,
#: because re-adding an unchanged job is not free — see reconcile().
_applied: "Schedule | None" = None


@dataclass(frozen=True)
class Schedule:
    materialise_at: time
    digest_at: time
    missed_every_minutes: int
    anomalies_at: time


async def read_schedule() -> Schedule:
    async with get_session_factory()() as db:
        return Schedule(
            materialise_at=await resolve_time(db, "jobs.materialise_time"),
            digest_at=await resolve_time(db, "jobs.digest_time"),
            missed_every_minutes=await resolve_int(db, "jobs.missed_check_minutes"),
            anomalies_at=await resolve_time(db, "jobs.anomalies_time"),
        )


def _apply(scheduler: AsyncIOScheduler, schedule: Schedule) -> None:
    """Install or move the three jobs.

    `replace_existing` makes this safe to call twice, but NOT free: replacing a
    job rebuilds its trigger, and an interval trigger measures its next fire
    from the moment it was built. Call this on an unchanged schedule and every
    interval job's clock restarts. Only call it when something actually moved.
    """
    global _applied
    scheduler.add_job(
        tasks.materialise_runs,
        CronTrigger(
            hour=schedule.materialise_at.hour,
            minute=schedule.materialise_at.minute,
            timezone=OUTLET_TZ,
        ),
        id=tasks.MATERIALISE,
        name="Materialise the day's runs",
        replace_existing=True,
        # A restart at 05:02 should still create the day, not skip it because
        # the scheduler was down at exactly 05:00.
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        tasks.mark_missed,
        IntervalTrigger(minutes=schedule.missed_every_minutes, timezone=OUTLET_TZ),
        id=tasks.MARK_MISSED,
        name="Mark overdue runs missed",
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        tasks.daily_digest,
        CronTrigger(
            hour=schedule.digest_at.hour, minute=schedule.digest_at.minute, timezone=OUTLET_TZ
        ),
        id=tasks.DAILY_DIGEST,
        name="Daily digest",
        replace_existing=True,
        # Half an hour. A digest four hours late is worse than no digest —
        # people have already started their day on the old numbers.
        misfire_grace_time=1800,
        coalesce=True,
        max_instances=1,
    )
    scheduler.add_job(
        tasks.stock_anomalies,
        CronTrigger(
            hour=schedule.anomalies_at.hour,
            minute=schedule.anomalies_at.minute,
            timezone=OUTLET_TZ,
        ),
        id=tasks.STOCK_ANOMALIES,
        name="Consumption windows and stock anomalies",
        replace_existing=True,
        # Derived data: running late loses nothing, so the grace is generous.
        misfire_grace_time=3600,
        coalesce=True,
        max_instances=1,
    )
    _applied = schedule


async def reconcile() -> None:
    """Pick up an edited job time without a restart.

    **Only when it changed.** This runs every few minutes; re-applying an
    unchanged schedule rebuilds each trigger, and a rebuilt interval trigger
    starts counting again from now. A 15-minute job reconciled every 5 minutes
    is a job that is always 15 minutes away and therefore never runs — while
    /jobs/schedule reports a perfectly plausible next fire time.
    """
    if _scheduler is None:
        return
    try:
        schedule = await read_schedule()
    except Exception:
        # A blip reading settings must not tear down a working schedule.
        logger.exception("could not re-read the job schedule; keeping the current one")
        return
    if schedule == _applied:
        return
    logger.info("job schedule changed: %s -> %s", _applied, schedule)
    _apply(_scheduler, schedule)


async def start() -> AsyncIOScheduler | None:
    global _scheduler
    settings = get_settings()
    if not settings.SCHEDULER_ENABLED:
        logger.info("scheduler disabled by SCHEDULER_ENABLED")
        return None
    if not settings.DATABASE_URL:
        logger.warning("scheduler not started: DATABASE_URL is not configured")
        return None

    scheduler = AsyncIOScheduler(timezone=OUTLET_TZ)
    try:
        schedule = await read_schedule()
    except Exception:
        logger.exception("scheduler not started: could not read the job schedule")
        return None

    _apply(scheduler, schedule)
    scheduler.add_job(
        reconcile,
        IntervalTrigger(minutes=RECONCILE_MINUTES, timezone=OUTLET_TZ),
        id=RECONCILE,
        name="Re-read job times from settings",
        replace_existing=True,
        max_instances=1,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "scheduler started: materialise %s, digest %s, missed check every %d min (Asia/Kolkata)",
        schedule.materialise_at,
        schedule.digest_at,
        schedule.missed_every_minutes,
    )
    return scheduler


async def shutdown() -> None:
    global _scheduler, _applied
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
    _applied = None


def describe() -> list[dict[str, Any]]:
    """What is actually scheduled, for the admin screen.

    Read off the live scheduler rather than recomputed from settings: the
    question the jobs page is answering is "is this really going to run", and
    only the scheduler knows.
    """
    if _scheduler is None:
        return []
    return [
        {
            "id": job.id,
            "name": job.name,
            "trigger": str(job.trigger),
            "next_run_at": job.next_run_time.isoformat() if job.next_run_time else None,
        }
        for job in _scheduler.get_jobs()
    ]
