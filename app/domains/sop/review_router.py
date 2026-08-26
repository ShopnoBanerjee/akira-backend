"""The manager review queue: browse, inspect, approve, reject, exceptions.

Approval is the one place separation of duties bites, and it is enforced
three deep: this router refuses device sessions outright (a PIN can never
approve), the service refuses caller == submitter, and the database CHECK
would reject the row even if both were bypassed.
"""

import uuid
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record
from app.core.deps import CurrentUser, CurrentUserDep, DbDep, require_management
from app.core.enums import APPROVER_ROLES, AuditAction, RunStatus
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.domains.sop import ai_review
from app.integrations import storage

router = APIRouter(prefix="/sop", tags=["sop-review"])


def _ctx(request: Request) -> dict[str, Any]:
    return {
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }


def _as_dict(raw: Any) -> dict[str, Any]:
    """jsonb comes back as text over asyncpg. Decode at the edge so the client
    gets an object, not a string containing an object."""
    import json

    if isinstance(raw, str):
        return dict(json.loads(raw or "{}"))
    return dict(raw or {})


def _require_individual_approver(user: CurrentUser) -> None:
    """Approvals need a person with a management login. Never a tablet."""
    if user.device is not None:
        raise ForbiddenError("Approvals need an individual manager login, not the shared tablet.")
    if user.global_role not in APPROVER_ROLES:
        raise ForbiddenError(
            "Your role cannot approve or reject checklist runs.",
            extra={"your_role": user.global_role.value},
        )


async def _load_run(db: AsyncSession, user: CurrentUser, run_id: uuid.UUID) -> dict[str, Any]:
    run = (
        (
            await db.execute(
                text(
                    """
                select r.id, r.outlet_id, r.status, r.submitted_by, r.template_id,
                       r.business_date, t.name as template_name
                  from checklist_runs r
                  join checklist_templates t on t.id = r.template_id
                 where r.id = :id
                """
                ),
                {"id": run_id},
            )
        )
        .mappings()
        .first()
    )
    if run is None:
        raise NotFoundError("That run does not exist.")
    if not user.can_access_outlet(run["outlet_id"]):
        raise ForbiddenError("You do not have access to that outlet.")
    return dict(run)


# ---------------------------------------------------------------------------
# Queue and detail
# ---------------------------------------------------------------------------


class QueueRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    outlet_id: uuid.UUID
    outlet_code: str
    template_name: str
    template_name_bn: str | None
    business_date: date
    status: str
    submitted_by_name: str | None
    submitted_at: datetime | None
    score_pct: float | None
    critical_fail_count: int
    integrity_flag_count: int
    is_late: bool
    item_count: int
    fail_count: int


@router.get(
    "/runs",
    response_model=list[QueueRow],
    dependencies=[Depends(require_management)],
    summary="Runs for review",
)
async def list_runs(
    db: DbDep,
    user: CurrentUserDep,
    outlet_id: uuid.UUID | None = Query(default=None),
    status: RunStatus | None = Query(default=None),
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[QueueRow]:
    """Oldest submitted first — the queue rewards clearing the backlog, and
    bulk approve is deliberately not offered anywhere."""
    clauses = []
    params: dict[str, Any] = {"limit": limit}
    if outlet_id is not None:
        if not user.can_access_outlet(outlet_id):
            raise ForbiddenError("You do not have access to that outlet.")
        clauses.append("r.outlet_id = :outlet_id")
        params["outlet_id"] = outlet_id
    elif not user.is_global:
        if not user.outlet_ids:
            return []
        clauses.append("r.outlet_id = any(:ids)")
        params["ids"] = sorted(user.outlet_ids)
    if status is not None:
        clauses.append("r.status = cast(:status as run_status)")
        params["status"] = status.value
    if date_from is not None:
        clauses.append("r.business_date >= :date_from")
        params["date_from"] = date_from
    if date_to is not None:
        clauses.append("r.business_date <= :date_to")
        params["date_to"] = date_to

    where = ("where " + " and ".join(clauses)) if clauses else ""
    rows = (
        await db.execute(
            text(
                f"""
                select r.id, r.outlet_id, o.code as outlet_code,
                       t.name as template_name, t.name_bn as template_name_bn,
                       r.business_date, r.status,
                       p.full_name as submitted_by_name, r.submitted_at,
                       cast(r.score_pct as float8) as score_pct,
                       r.critical_fail_count, r.integrity_flag_count, r.is_late,
                       (select count(*) from checklist_run_items ri
                         where ri.run_id = r.id) as item_count,
                       (select count(*) from checklist_run_items ri
                         where ri.run_id = r.id and ri.result = 'fail') as fail_count
                  from checklist_runs r
                  join checklist_templates t on t.id = r.template_id
                  join outlets o on o.id = r.outlet_id
                  left join profiles p on p.id = r.submitted_by
                 {where}
                 order by case when r.status = 'submitted' then 0 else 1 end,
                          r.submitted_at asc nulls last, r.business_date desc
                 limit :limit
                """
            ),
            params,
        )
    ).mappings()
    return [QueueRow(**r) for r in rows]


@router.get(
    "/runs/{run_id}/detail",
    dependencies=[Depends(require_management)],
    summary="A run for review, with photo view URLs",
)
async def run_detail(run_id: uuid.UUID, db: DbDep, user: CurrentUserDep) -> dict[str, Any]:
    """Items with their snapshot definitions, integrity flags, and short-lived
    signed photo URLs minted per request — never stored, expired in minutes."""
    run = (
        (
            await db.execute(
                text(
                    """
                select r.id, r.outlet_id, o.code as outlet_code, r.status,
                       r.business_date, r.day_part, r.template_version,
                       t.name as template_name, t.name_bn as template_name_bn,
                       r.started_at, r.submitted_at, r.due_at, r.is_late,
                       r.minutes_late, cast(r.score_pct as float8) as score_pct,
                       r.critical_fail_count, r.integrity_flag_count,
                       r.integrity_flags, r.integrity_detail,
                       r.geo_ok, r.rejection_reason,
                       sp.full_name as submitted_by_name, r.submitted_by,
                       ap.full_name as approved_by_name, r.approved_at,
                       d.label as device_label
                  from checklist_runs r
                  join checklist_templates t on t.id = r.template_id
                  join outlets o on o.id = r.outlet_id
                  left join profiles sp on sp.id = r.submitted_by
                  left join profiles ap on ap.id = r.approved_by
                  left join outlet_devices d on d.id = r.device_id
                 where r.id = :id
                """
                ),
                {"id": run_id},
            )
        )
        .mappings()
        .first()
    )
    if run is None:
        raise NotFoundError("That run does not exist.")
    if not user.can_access_outlet(run["outlet_id"]):
        raise ForbiddenError("You do not have access to that outlet.")

    items = [
        dict(r)
        for r in (
            await db.execute(
                text(
                    """
                    select ri.id, ri.sort_order, ri.result,
                           cast(ri.value_numeric as float8) as value_numeric,
                           ri.value_text, ri.out_of_range, ri.note,
                           ri.photo_path, ri.photo_uploaded_at, ri.integrity_flags,
                           ri.integrity_detail, ri.photo_processed_at,
                           cast(ri.photo_luminance as float8) as photo_luminance,
                           v.title, v.title_bn, v.instruction, v.instruction_bn,
                           v.requires_photo, v.requires_value, v.value_type,
                           cast(v.value_min as float8) as value_min,
                           cast(v.value_max as float8) as value_max,
                           v.value_unit, v.is_critical, v.allow_na
                      from checklist_run_items ri
                      join checklist_template_item_versions v
                        on v.id = ri.template_item_version_id
                     where ri.run_id = :run_id
                     order by ri.sort_order
                    """
                ),
                {"run_id": run_id},
            )
        ).mappings()
    ]

    # Which photos this reviewer has already opened.
    viewed = {
        r[0]
        for r in await db.execute(
            text(
                "select run_item_id from run_review_views"
                " where run_id = :run_id and reviewer_id = :reviewer"
            ),
            {"run_id": run_id, "reviewer": user.profile_id},
        )
    }

    # Advisory verdicts, threshold already applied. Attached per item so the
    # manager sees the opinion beside the photo it is about, never as a
    # separate screen they would have to go and look for.
    verdicts = await ai_review.latest_for_run(db, run_id)

    for item in items:
        item["viewed_by_me"] = item["id"] in viewed
        item["integrity_detail"] = _as_dict(item["integrity_detail"])
        item["ai_review"] = verdicts.get(item["id"])
        if item["photo_path"]:
            item["photo_view_url"] = await storage.create_signed_view_url(
                item["photo_path"], expires_in=300
            )
        else:
            item["photo_view_url"] = None

    return {
        **dict(run),
        "integrity_detail": _as_dict(run["integrity_detail"]),
        "items": items,
    }


class MarkViewedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: uuid.UUID


@router.post(
    "/runs/{run_id}/viewed",
    dependencies=[Depends(require_management)],
    summary="Record that the reviewer opened a photo",
)
async def mark_viewed(
    run_id: uuid.UUID, payload: MarkViewedRequest, db: DbDep, user: CurrentUserDep
) -> dict[str, bool]:
    await _load_run(db, user, run_id)
    await db.execute(
        text(
            """
            insert into run_review_views (run_id, run_item_id, reviewer_id)
            values (:run_id, :item_id, :reviewer)
            on conflict (run_item_id, reviewer_id)
              do update set viewed_at = now()
            """
        ),
        {"run_id": run_id, "item_id": payload.item_id, "reviewer": user.profile_id},
    )
    await db.commit()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Approve / reject
# ---------------------------------------------------------------------------


class RejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=1000)
    #: The items that need redoing. Only these are cleared; everything else
    #: keeps its answers, so staff redo what was wrong rather than everything.
    item_ids: list[uuid.UUID] = Field(min_length=1)


@router.post("/runs/{run_id}/approve", summary="Approve a run")
async def approve_run(
    run_id: uuid.UUID, request: Request, db: DbDep, user: CurrentUserDep
) -> dict[str, Any]:
    """Locks the run. Refused when the approver is the submitter — here, and
    again by the database CHECK if this code were ever bypassed."""
    _require_individual_approver(user)
    run = await _load_run(db, user, run_id)

    if run["status"] != RunStatus.SUBMITTED.value:
        raise ConflictError(
            f"This run is {run['status']}, not submitted.",
            extra={"status": run["status"]},
        )
    if run["submitted_by"] == user.profile_id:
        raise ForbiddenError(
            "You submitted this run, so someone else has to approve it. "
            "Separation of duties is what makes the record worth anything."
        )

    await db.execute(
        text(
            """
            update checklist_runs
               set status = 'approved', approved_by = :approver, approved_at = now()
             where id = :id and status = 'submitted'
            """
        ),
        {"id": run_id, "approver": user.profile_id},
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=run["outlet_id"],
        entity_table="checklist_runs",
        entity_id=run_id,
        action=AuditAction.APPROVE,
        after={"template": run["template_name"]},
        **_ctx(request),
    )
    await db.commit()
    return {"id": str(run_id), "status": "approved"}


@router.post("/runs/{run_id}/reject", summary="Reject a run back to the floor")
async def reject_run(
    run_id: uuid.UUID,
    payload: RejectRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> dict[str, Any]:
    """Back to in_progress with the reason attached. Clears results on the
    named items only — the rest of the run stays done."""
    _require_individual_approver(user)
    run = await _load_run(db, user, run_id)

    if run["status"] != RunStatus.SUBMITTED.value:
        raise ConflictError(
            f"This run is {run['status']}, not submitted.",
            extra={"status": run["status"]},
        )

    valid_ids = {
        r[0]
        for r in await db.execute(
            text("select id from checklist_run_items where run_id = :run_id"),
            {"run_id": run_id},
        )
    }
    unknown = [str(i) for i in payload.item_ids if i not in valid_ids]
    if unknown:
        raise NotFoundError(
            "Some of those items do not belong to this run.",
            extra={"unknown_item_ids": unknown},
        )

    await db.execute(
        text(
            """
            update checklist_run_items
               set result = 'pending', completed_at = null, out_of_range = false
             where run_id = :run_id and id = any(:item_ids)
            """
        ),
        {"run_id": run_id, "item_ids": list(payload.item_ids)},
    )
    await db.execute(
        text(
            """
            update checklist_runs
               set status = 'in_progress', rejection_reason = :reason,
                   submitted_by = null, submitted_at = null,
                   score_pct = null, critical_fail_count = 0
             where id = :id and status = 'submitted'
            """
        ),
        {"id": run_id, "reason": payload.reason},
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=run["outlet_id"],
        entity_table="checklist_runs",
        entity_id=run_id,
        action=AuditAction.REJECT,
        after={"reason": payload.reason, "items_cleared": len(payload.item_ids)},
        **_ctx(request),
    )
    await db.commit()
    return {"id": str(run_id), "status": "in_progress", "items_cleared": len(payload.item_ids)}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ExceptionRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    outlet_id: uuid.UUID
    outlet_code: str
    business_date: date
    severity: str
    status: str
    title: str
    detail: str | None
    photo_path: str | None
    assigned_to_name: str | None
    resolved_by_name: str | None
    resolution_note: str | None
    created_at: datetime
    age_hours: float


@router.get(
    "/exceptions",
    response_model=list[ExceptionRow],
    dependencies=[Depends(require_management)],
    summary="The exception board",
)
async def list_exceptions(
    db: DbDep,
    user: CurrentUserDep,
    outlet_id: uuid.UUID | None = Query(default=None),
    status: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[ExceptionRow]:
    clauses = []
    params: dict[str, Any] = {"limit": limit}
    if outlet_id is not None:
        if not user.can_access_outlet(outlet_id):
            raise ForbiddenError("You do not have access to that outlet.")
        clauses.append("e.outlet_id = :outlet_id")
        params["outlet_id"] = outlet_id
    elif not user.is_global:
        if not user.outlet_ids:
            return []
        clauses.append("e.outlet_id = any(:ids)")
        params["ids"] = sorted(user.outlet_ids)
    if status is not None:
        clauses.append("e.status = cast(:status as exception_status)")
        params["status"] = status
    if severity is not None:
        clauses.append("e.severity = cast(:severity as severity)")
        params["severity"] = severity

    where = ("where " + " and ".join(clauses)) if clauses else ""
    rows = (
        await db.execute(
            text(
                f"""
                select e.id, e.outlet_id, o.code as outlet_code, e.business_date,
                       e.severity, e.status, e.title, e.detail, e.photo_path,
                       ap.full_name as assigned_to_name,
                       rp.full_name as resolved_by_name, e.resolution_note,
                       e.created_at,
                       extract(epoch from (now() - e.created_at)) / 3600.0 as age_hours
                  from sop_exceptions e
                  join outlets o on o.id = e.outlet_id
                  left join profiles ap on ap.id = e.assigned_to
                  left join profiles rp on rp.id = e.resolved_by
                 {where}
                 order by case e.status when 'open' then 0 when 'acknowledged' then 1 else 2 end,
                          case e.severity when 'high' then 0 when 'medium' then 1 else 2 end,
                          e.created_at asc
                 limit :limit
                """
            ),
            params,
        )
    ).mappings()
    return [ExceptionRow(**r) for r in rows]


class ResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution_note: str = Field(min_length=3, max_length=1000)


class WaiveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=3, max_length=1000)


async def _load_exception(
    db: AsyncSession, user: CurrentUser, exception_id: uuid.UUID
) -> dict[str, Any]:
    row = (
        (
            await db.execute(
                text("select id, outlet_id, status, title from sop_exceptions where id = :id"),
                {"id": exception_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFoundError("That exception does not exist.")
    if not user.can_access_outlet(row["outlet_id"]):
        raise ForbiddenError("You do not have access to that outlet.")
    return dict(row)


@router.post(
    "/exceptions/{exception_id}/acknowledge",
    dependencies=[Depends(require_management)],
    summary="Acknowledge an exception",
)
async def acknowledge_exception(
    exception_id: uuid.UUID, request: Request, db: DbDep, user: CurrentUserDep
) -> dict[str, str]:
    exception = await _load_exception(db, user, exception_id)
    if exception["status"] not in ("open",):
        raise ConflictError(f"This exception is already {exception['status']}.")
    await db.execute(
        text(
            "update sop_exceptions set status = 'acknowledged', assigned_to = :who where id = :id"
        ),
        {"id": exception_id, "who": user.profile_id},
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=exception["outlet_id"],
        entity_table="sop_exceptions",
        entity_id=exception_id,
        action=AuditAction.UPDATE,
        after={"status": "acknowledged"},
        **_ctx(request),
    )
    await db.commit()
    return {"status": "acknowledged"}


@router.post(
    "/exceptions/{exception_id}/resolve",
    dependencies=[Depends(require_management)],
    summary="Resolve an exception",
)
async def resolve_exception(
    exception_id: uuid.UUID,
    payload: ResolveRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> dict[str, str]:
    exception = await _load_exception(db, user, exception_id)
    if exception["status"] in ("resolved", "waived"):
        raise ConflictError(f"This exception is already {exception['status']}.")
    await db.execute(
        text(
            """
            update sop_exceptions
               set status = 'resolved', resolved_by = :who, resolved_at = now(),
                   resolution_note = :note
             where id = :id
            """
        ),
        {"id": exception_id, "who": user.profile_id, "note": payload.resolution_note},
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=exception["outlet_id"],
        entity_table="sop_exceptions",
        entity_id=exception_id,
        action=AuditAction.UPDATE,
        after={"status": "resolved", "note": payload.resolution_note},
        **_ctx(request),
    )
    await db.commit()
    return {"status": "resolved"}


@router.post(
    "/exceptions/{exception_id}/waive",
    summary="Waive an exception (owner / ops manager)",
)
async def waive_exception(
    exception_id: uuid.UUID,
    payload: WaiveRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> dict[str, str]:
    """Waiving says "we accept this and are not fixing it" — a call above the
    outlet, so it needs the network roles and always a reason."""
    if not user.is_global or user.device is not None:
        raise ForbiddenError("Waiving an exception is limited to the owner and operations manager.")
    exception = await _load_exception(db, user, exception_id)
    if exception["status"] in ("resolved", "waived"):
        raise ConflictError(f"This exception is already {exception['status']}.")
    await db.execute(
        text(
            """
            update sop_exceptions
               set status = 'waived', resolved_by = :who, resolved_at = now(),
                   resolution_note = :note
             where id = :id
            """
        ),
        {"id": exception_id, "who": user.profile_id, "note": f"WAIVED: {payload.reason}"},
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=exception["outlet_id"],
        entity_table="sop_exceptions",
        entity_id=exception_id,
        action=AuditAction.UPDATE,
        after={"status": "waived", "reason": payload.reason},
        **_ctx(request),
    )
    await db.commit()
    return {"status": "waived"}
