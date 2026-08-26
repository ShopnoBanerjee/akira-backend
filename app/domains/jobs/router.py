"""Scheduled and background jobs: what ran, what is next, and run-now.

The read surface came first so the admin area was complete before the jobs
existed. P7 adds the schedule view and the manual triggers.

A gap where a scheduled job should have run is as significant as a failure row.
That is why `next_run_at` is read off the live scheduler rather than recomputed
from settings: the question this screen answers is "is this really going to
run", and only the scheduler knows.
"""

import uuid
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from app.core.deps import CurrentUserDep, DbDep, require_admin, require_owner
from app.core.errors import ValidationError
from app.jobs import scheduler, tasks

router = APIRouter(prefix="/jobs", tags=["jobs"])


class JobRun(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_name: str
    status: str
    outlet_id: uuid.UUID | None
    business_date: datetime | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    detail: dict[str, Any]
    error_detail: str | None
    triggered_by_name: str | None


@router.get(
    "/runs",
    response_model=list[JobRun],
    dependencies=[Depends(require_admin)],
    summary="Recent job executions",
)
async def list_job_runs(
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    job_name: str | None = Query(default=None, max_length=80),
) -> list[JobRun]:
    """Newest first. A gap where a scheduled job should have run is as
    significant as a failure row — both mean the job did not do its work."""
    clauses = []
    params: dict[str, Any] = {"limit": limit}
    if job_name:
        clauses.append("j.job_name = :job_name")
        params["job_name"] = job_name
    where = f"where {' and '.join(clauses)}" if clauses else ""
    rows = (
        await db.execute(
            text(
                f"""
                select j.id, j.job_name, j.status, j.outlet_id, j.business_date,
                       j.started_at, j.finished_at, j.duration_ms, j.detail,
                       j.error_detail, p.full_name as triggered_by_name
                  from job_runs j
                  left join profiles p on p.id = j.triggered_by
                 {where}
                 order by j.started_at desc
                 limit :limit
                """
            ),
            params,
        )
    ).mappings()

    import json

    return [
        JobRun(
            **{
                **dict(r),
                "detail": json.loads(r["detail"])
                if isinstance(r["detail"], str)
                else (r["detail"] or {}),
            }
        )
        for r in rows
    ]


class ScheduledJob(BaseModel):
    id: str
    name: str
    trigger: str
    next_run_at: datetime | None


class ScheduleView(BaseModel):
    #: False when SCHEDULER_ENABLED is off, or the process failed to start it.
    #: An empty list with running=true would look the same as a healthy idle
    #: scheduler, and it is not.
    running: bool
    jobs: list[ScheduledJob]


@router.get(
    "/schedule",
    response_model=ScheduleView,
    dependencies=[Depends(require_admin)],
    summary="What is scheduled, and when it next fires",
)
async def job_schedule() -> ScheduleView:
    jobs = scheduler.describe()
    return ScheduleView(running=bool(jobs), jobs=[ScheduledJob(**j) for j in jobs])


class RunJobRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: For materialise_runs and daily_digest. Defaults to the job's own idea of
    #: the right day — today for materialise, yesterday for the digest.
    business_date: date | None = None


@router.post(
    "/{job_name}/run",
    dependencies=[Depends(require_owner)],
    summary="Run a scheduled job now (owner only)",
)
async def run_now(job_name: str, payload: RunJobRequest, user: CurrentUserDep) -> dict[str, Any]:
    """Owner only, and every one of these is safe to press twice.

    Materialisation is idempotent by unique constraint; marking missed only
    moves runs already past grace; the digest re-sends a report rather than
    changing anything. Nothing here is destructive, which is why it can be a
    button at all — but it does send mail, so it is not left to ops managers.
    """
    if job_name not in tasks.MANUAL_JOBS:
        raise ValidationError(
            f"{job_name} cannot be run by hand.",
            extra={"runnable": list(tasks.MANUAL_JOBS)},
        )
    return await tasks.run_by_name(
        job_name, triggered_by=user.profile_id, for_date=payload.business_date
    )
