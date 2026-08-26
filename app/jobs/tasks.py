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
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.business_date import OUTLET_TZ
from app.core.business_date import business_date as to_business_date
from app.core.settings_value import resolve
from app.domains.sop import runs_service
from app.jobs import digest as digest_module
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


async def mark_missed(*, triggered_by: uuid.UUID | None = None) -> dict[str, Any]:
    """Pending runs past due plus grace become missed, each raising a
    medium-severity exception.

    Medium, not high: nobody did the checklist, which is a management problem
    to chase, not the same class of event as a critical food-safety item being
    failed outright.
    """

    async def body(db: AsyncSession) -> dict[str, Any]:
        overdue = (await db.execute(_OVERDUE_SQL)).mappings().all()
        marked: list[dict[str, Any]] = []
        for run in overdue:
            await db.execute(
                text(
                    "update checklist_runs set status = 'missed'"
                    " where id = :id and status = 'pending'"
                ),
                {"id": run["id"]},
            )
            await db.execute(
                text(
                    """
                    insert into sop_exceptions
                        (outlet_id, business_date, severity, title, detail)
                    values (:outlet_id, :business_date, 'medium', :title, :detail)
                    """
                ),
                {
                    "outlet_id": run["outlet_id"],
                    "business_date": run["business_date"],
                    "title": f"Missed: {run['template_name']}",
                    # Rendered in the outlet's own timezone. A manager in
                    # Kolkata reading "19:00 UTC" has to do arithmetic to find
                    # out whether the closing checklist was skipped, and will
                    # get it wrong.
                    "detail": (
                        f"Due {_local(run['due_at'], run['outlet_timezone'])} plus "
                        f"{run['grace_minutes']} minutes grace. Never started."
                    ),
                },
            )
            marked.append(
                {
                    "run_id": str(run["id"]),
                    "outlet": run["outlet_code"],
                    "template": run["template_name"],
                    "business_date": str(run["business_date"]),
                }
            )
        await db.commit()
        return {"marked_missed": len(marked), "runs": marked}

    return await run_job(MARK_MISSED, body, triggered_by=triggered_by)


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
        return {**digest_module.to_detail(data), "delivery": delivery}

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
