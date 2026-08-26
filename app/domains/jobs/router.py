"""Scheduled and background job history.

The jobs themselves arrive in P7; this read surface exists now so the admin
area is complete and a failed job is visible the day the first one runs.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from app.core.deps import DbDep, require_admin

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
