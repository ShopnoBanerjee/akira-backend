"""The three scheduled jobs.

Each is an ordinary async function taking a session, so it can be run by the
scheduler, by an admin pressing "run now", or by a test — with no difference in
behaviour between the three. Every one of them goes through `run_job`, so every
execution leaves a job_runs row whether it worked or not.

    materialise_runs   05:00 local. Creates the day's pending runs.
    mark_missed        every 15 minutes. Pending runs past grace become missed
                       and raise an exception.
    daily_digest       09:00 local. Yesterday's numbers, per outlet, to the
                       people accountable for them.
"""

import logging
import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.business_date import OUTLET_TZ
from app.core.business_date import business_date as to_business_date
from app.core.settings_value import resolve
from app.domains.sop import runs_service
from app.jobs import digest as digest_module
from app.jobs import narrate as narrate_module
from app.jobs.notify import Notification, get_notifier
from app.jobs.runner import run_job

logger = logging.getLogger(__name__)

MATERIALISE = "materialise_runs"
MARK_MISSED = "mark_missed"
DAILY_DIGEST = "daily_digest"

#: Everything an admin may trigger by hand. The photo passes are not here:
#: they are attached to a specific upload, not to a schedule.
MANUAL_JOBS = (MATERIALISE, MARK_MISSED, DAILY_DIGEST)


# ---------------------------------------------------------------------------
# 05:00 — materialise the day's runs
# ---------------------------------------------------------------------------


async def materialise_runs(
    *, for_date: date | None = None, triggered_by: uuid.UUID | None = None
) -> dict[str, Any]:
    """Create today's pending runs from the active assignments.

    A thin wrapper: the logic and its idempotence live in
    runs_service.materialise_runs and were proven in P5. What this adds is the
    job_runs row, which is what makes a morning where it did not fire visible.
    """
    business_day = for_date or to_business_date(datetime.now(tz=OUTLET_TZ))

    async def body(db: AsyncSession) -> dict[str, Any]:
        result = await runs_service.materialise_runs(
            db, for_date=business_day, triggered_by=triggered_by
        )
        return {"business_date": str(business_day), **result}

    return await run_job(MATERIALISE, body, business_date=business_day, triggered_by=triggered_by)


# ---------------------------------------------------------------------------
# Every 15 minutes — mark the ones nobody did
# ---------------------------------------------------------------------------

#: Deliberately `pending` only. An in_progress run is being worked on right
#: now; flipping it to missed would lock it (missed is terminal) and throw away
#: a half-finished checklist. Late is what `late` is for.
_OVERDUE_SQL = text(
    """
    select r.id, r.outlet_id, r.business_date, r.due_at, a.grace_minutes,
           t.name as template_name, t.name_bn as template_name_bn,
           o.code as outlet_code, o.timezone as outlet_timezone
      from checklist_runs r
      join checklist_assignments a on a.id = r.assignment_id
      join checklist_templates t on t.id = r.template_id
      join outlets o on o.id = r.outlet_id
     where r.status = 'pending'
       and r.due_at is not null
       and now() > r.due_at + make_interval(mins => a.grace_minutes)
    """
)


def _local(when: datetime, timezone: str | None) -> str:
    from zoneinfo import ZoneInfo

    tz = ZoneInfo(timezone) if timezone else OUTLET_TZ
    return when.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z")


async def sweep_overdue(db: AsyncSession) -> dict[str, Any]:
    """Pending runs past due plus grace become missed, each raising a
    medium-severity exception.

    Medium, not high: nobody did the checklist, which is a management problem
    to chase, not the same class of event as a critical food-safety item being
    failed outright.

    A module-level function rather than a closure inside the job so the tests
    can point it at their own database — the job wrapper below only adds the
    job_runs bracket.
    """
    overdue = (await db.execute(_OVERDUE_SQL)).mappings().all()
    if not overdue:
        return {"marked_missed": 0, "runs": []}

    # One statement flips every run and raises every exception, however
    # many there are. The loop this replaced cost two round trips per run —
    # and, worse, it raised the exception even when its guarded update
    # matched nothing because somebody had started the run between the
    # select and the update. Joining the insert to the update's `returning`
    # makes "flipped" and "raised an exception" the same set by
    # construction.
    #
    # Titles and details stay rendered in Python: the outlet-local
    # timestamp formatting has no business being duplicated into SQL.
    flipped_ids = {
        row[0]
        for row in await db.execute(
            text(
                """
                with flipped as (
                    update checklist_runs
                       set status = 'missed'
                     where id = any(:ids) and status = 'pending'
                     returning id
                ),
                raised as (
                    insert into sop_exceptions
                        (outlet_id, business_date, severity, title, detail)
                    select p.outlet_id, p.business_date, 'medium', p.title, p.detail
                      from unnest(
                               cast(:ids as uuid[]), cast(:outlet_ids as uuid[]),
                               cast(:business_dates as date[]),
                               cast(:titles as text[]), cast(:details as text[])
                           ) as p(id, outlet_id, business_date, title, detail)
                      join flipped f on f.id = p.id
                )
                select id from flipped
                """
            ),
            {
                "ids": [run["id"] for run in overdue],
                "outlet_ids": [run["outlet_id"] for run in overdue],
                "business_dates": [run["business_date"] for run in overdue],
                "titles": [f"Missed: {run['template_name']}" for run in overdue],
                # Rendered in the outlet's own timezone. A manager in
                # Kolkata reading "19:00 UTC" has to do arithmetic to find
                # out whether the closing checklist was skipped, and will
                # get it wrong.
                "details": [
                    f"Due {_local(run['due_at'], run['outlet_timezone'])} plus "
                    f"{run['grace_minutes']} minutes grace. Never started."
                    for run in overdue
                ],
            },
        )
    }
    await db.commit()
    marked = [
        {
            "run_id": str(run["id"]),
            "outlet": run["outlet_code"],
            "template": run["template_name"],
            "business_date": str(run["business_date"]),
        }
        for run in overdue
        if run["id"] in flipped_ids
    ]
    return {"marked_missed": len(marked), "runs": marked}


async def mark_missed(*, triggered_by: uuid.UUID | None = None) -> dict[str, Any]:
    return await run_job(MARK_MISSED, sweep_overdue, triggered_by=triggered_by)


# ---------------------------------------------------------------------------
# 09:00 — the digest
# ---------------------------------------------------------------------------


async def daily_digest(
    *, for_date: date | None = None, triggered_by: uuid.UUID | None = None
) -> dict[str, Any]:
    """One digest per active outlet, for the business date that just closed.

    Each outlet is its own job_runs row. A mail failure at one outlet must not
    hide another outlet's numbers, and the jobs screen should say which one
    went wrong.
    """
    business_day = for_date or (to_business_date(datetime.now(tz=OUTLET_TZ)) - timedelta(days=1))

    from app.core.db import get_session_factory

    async with get_session_factory()() as db:
        outlets = (
            (
                await db.execute(
                    text(
                        "select id from outlets"
                        " where is_active and deleted_at is null order by code"
                    )
                )
            )
            .scalars()
            .all()
        )

    results = []
    for outlet_id in outlets:
        results.append(
            await _digest_for_outlet(
                uuid.UUID(str(outlet_id)),
                business_day=business_day,
                triggered_by=triggered_by,
            )
        )
    return {"business_date": str(business_day), "outlets": results}


async def _digest_for_outlet(
    outlet_id: uuid.UUID, *, business_day: date, triggered_by: uuid.UUID | None
) -> dict[str, Any]:
    async def body(db: AsyncSession) -> dict[str, Any]:
        data = await digest_module.build(db, outlet_id=outlet_id, business_date=business_day)

        # The day's sales line, computed by code; the narrator may only repeat
        # it. One aggregate call, same statement the dashboard pillar uses.
        from app.domains.sales import pillar_service
        from app.domains.sales.pillar import rupees

        sales_in = await pillar_service.sales_inputs_many(
            db, outlet_ids=[outlet_id], start=business_day, end=business_day
        )
        day = sales_in.get(outlet_id)
        if day and day.bills:
            targets = await pillar_service.sales_targets_many(
                db, outlet_ids=[outlet_id], at=datetime.now(tz=UTC)
            )
            data = replace(
                data,
                net_display=rupees(day.net_paise),
                net_target_display=rupees(targets[outlet_id].net_per_day_paise),
            )

        # Advisory narration. A morning email is never hostage to a model.
        narrated = "disabled"
        if bool(await resolve(db, "jobs.digest_narrative", outlet_id=outlet_id)):
            facts = narrate_module.build_facts(
                outlet_code=data.outlet_code,
                business_date=data.business_date,
                headline=data.headline,
                completion_rate=data.completion_rate,
                mean_score=data.mean_score,
                missed=data.missed,
                critical_fails=data.critical_fails,
                open_exceptions=data.open_exceptions,
                stale_exceptions=data.stale_exceptions,
                net_display=data.net_display,
                net_target_display=data.net_target_display,
            )
            data = replace(data, narrative=await narrate_module.narrate(facts))
            narrated = "written" if data.narrative else "skipped"

        subject, html, plain = digest_module.render(data)
        to = await digest_module.recipients(db, outlet_id)

        channel = str(await resolve(db, "notifications.channel", outlet_id=outlet_id))
        notifier, downgraded = get_notifier(channel)
        delivery = await notifier.send(
            Notification(subject=subject, html=html, text=plain, recipients=to)
        )
        if downgraded:
            # Visible, not silent. The whole point of the jobs screen.
            delivery["configured_channel"] = channel
            delivery["downgraded_because"] = downgraded
        return {**digest_module.to_detail(data), "narrative": narrated, "delivery": delivery}

    return await run_job(
        DAILY_DIGEST,
        body,
        outlet_id=outlet_id,
        business_date=business_day,
        triggered_by=triggered_by,
    )


# ---------------------------------------------------------------------------


async def run_by_name(
    name: str, *, triggered_by: uuid.UUID | None = None, for_date: date | None = None
) -> dict[str, Any]:
    """Dispatch for the "run now" buttons."""
    if name == MATERIALISE:
        return await materialise_runs(for_date=for_date, triggered_by=triggered_by)
    if name == MARK_MISSED:
        return await mark_missed(triggered_by=triggered_by)
    if name == DAILY_DIGEST:
        return await daily_digest(for_date=for_date, triggered_by=triggered_by)
    raise ValueError(f"{name} is not a job that can be run by hand.")
