"""Bracketing a piece of background work with a job_runs row.

The rule this exists to enforce: a job that fails must leave something a person
can read. Photo hashing, run materialisation and the digest all run where
nobody is watching, and the failure mode that costs the most is the silent one
— a scheduler that stopped firing three weeks ago and a screen that shows
nothing because nothing was written.

So every job gets a row the moment it starts (status `running`), and that row
is closed out either way. A crash mid-job leaves a `running` row with no
finish, which reads as "this died", which is exactly right.

The bookkeeping uses its own short-lived session, opened twice, so a work
transaction that blew up cannot take the failure record down with it.
"""

import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session_factory
from app.core.enums import JobStatus

logger = logging.getLogger(__name__)

#: What the work function returns and what lands in job_runs.detail.
JobDetail = dict[str, Any]
JobBody = Callable[[AsyncSession], Awaitable[JobDetail]]


async def _open(
    name: str,
    *,
    outlet_id: uuid.UUID | None,
    business_date: date | None,
    triggered_by: uuid.UUID | None,
) -> uuid.UUID:
    async with get_session_factory()() as db:
        job_id = (
            await db.execute(
                text(
                    """
                    insert into job_runs
                        (job_name, status, outlet_id, business_date, triggered_by)
                    values (:name, 'running', :outlet_id, :business_date, :triggered_by)
                    returning id
                    """
                ),
                {
                    "name": name,
                    "outlet_id": outlet_id,
                    "business_date": business_date,
                    "triggered_by": triggered_by,
                },
            )
        ).scalar_one()
        await db.commit()
    return uuid.UUID(str(job_id))


async def _close(
    job_id: uuid.UUID,
    *,
    status: JobStatus,
    detail: JobDetail,
    error: str | None,
    duration_ms: int,
) -> None:
    import json

    async with get_session_factory()() as db:
        await db.execute(
            text(
                """
                update job_runs
                   set status = cast(:status as job_status),
                       finished_at = now(),
                       duration_ms = :duration_ms,
                       detail = cast(:detail as jsonb),
                       error_detail = :error
                 where id = :id
                """
            ),
            {
                "id": job_id,
                "status": status.value,
                "duration_ms": duration_ms,
                "detail": json.dumps(detail, default=str),
                "error": error,
            },
        )
        await db.commit()


async def run_job(
    name: str,
    body: JobBody,
    *,
    outlet_id: uuid.UUID | None = None,
    business_date: date | None = None,
    triggered_by: uuid.UUID | None = None,
) -> JobDetail:
    """Run ``body`` in its own session, recorded in job_runs either way.

    Never raises. A background task that propagates an exception into the event
    loop is a log line at best; the job_runs row is the thing anybody will
    actually look at.
    """
    job_id = await _open(
        name, outlet_id=outlet_id, business_date=business_date, triggered_by=triggered_by
    )
    started = time.monotonic()
    try:
        async with get_session_factory()() as db:
            detail = await body(db)
    except Exception as exc:
        logger.exception("job %s failed", name)
        await _close(
            job_id,
            status=JobStatus.FAILED,
            detail={},
            error=f"{type(exc).__name__}: {exc}"[:2000],
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return {"error": str(exc)}

    await _close(
        job_id,
        status=JobStatus.SUCCEEDED,
        detail=detail,
        error=None,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return detail
