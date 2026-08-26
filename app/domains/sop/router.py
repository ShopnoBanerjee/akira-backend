"""HTTP surface for SOP template authoring. /app only — owner and ops manager
author templates; outlet managers and below run them, in a later epic."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import text

from app.core.audit import record
from app.core.deps import CurrentUserDep, DbDep, require_admin, require_management
from app.core.enums import AuditAction
from app.core.errors import NotFoundError
from app.domains.sop import service
from app.domains.sop.schemas import (
    Assignment,
    CategoryOut,
    CreateAssignmentRequest,
    CreateTemplateRequest,
    ItemFields,
    ReorderRequest,
    TemplateDetail,
    TemplateSummary,
    UpdateAssignmentRequest,
    UpdateItemRequest,
    UpdateTemplateRequest,
)

router = APIRouter(prefix="/sop", tags=["sop"])


def _ctx(request: Request) -> dict[str, Any]:
    return {
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }


@router.get(
    "/categories",
    response_model=list[CategoryOut],
    dependencies=[Depends(require_management)],
    summary="SOP categories",
)
async def list_categories(db: DbDep) -> list[CategoryOut]:
    rows = (
        await db.execute(
            text(
                "select id, key, label, label_bn, sort_order, icon"
                " from sop_categories order by sort_order"
            )
        )
    ).mappings()
    return [CategoryOut(**r) for r in rows]


@router.get(
    "/templates",
    response_model=list[TemplateSummary],
    dependencies=[Depends(require_management)],
    summary="Checklist templates",
)
async def list_templates(
    db: DbDep,
    category_id: uuid.UUID | None = Query(default=None),
    include_inactive: bool = Query(default=True),
) -> list[TemplateSummary]:
    return await service.list_templates(
        db, category_id=category_id, include_inactive=include_inactive
    )


@router.post(
    "/templates",
    response_model=TemplateDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
    summary="Create a template",
)
async def create_template(
    payload: CreateTemplateRequest, request: Request, db: DbDep, user: CurrentUserDep
) -> TemplateDetail:
    return await service.create_template(db, user, payload, **_ctx(request))


@router.get(
    "/templates/{template_id}",
    response_model=TemplateDetail,
    dependencies=[Depends(require_management)],
    summary="One template with its items",
)
async def get_template(template_id: uuid.UUID, db: DbDep) -> TemplateDetail:
    return await service.get_template(db, template_id)


@router.patch(
    "/templates/{template_id}",
    response_model=TemplateDetail,
    dependencies=[Depends(require_admin)],
    summary="Edit template details",
)
async def update_template(
    template_id: uuid.UUID,
    payload: UpdateTemplateRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> TemplateDetail:
    """Name, category, frequency, day part, active flag. None of these bumps
    the version — only item changes are material."""
    return await service.update_template(db, user, template_id, payload, **_ctx(request))


@router.post(
    "/templates/{template_id}/duplicate",
    response_model=TemplateDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
    summary="Duplicate a template",
)
async def duplicate_template(
    template_id: uuid.UUID, request: Request, db: DbDep, user: CurrentUserDep
) -> TemplateDetail:
    """A fresh version-1 copy, inactive and unassigned, ready to edit."""
    return await service.duplicate_template(db, user, template_id, **_ctx(request))


@router.post(
    "/templates/{template_id}/items",
    response_model=TemplateDetail,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
    summary="Add an item",
)
async def add_item(
    template_id: uuid.UUID,
    payload: ItemFields,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> TemplateDetail:
    """Bumps the template version and snapshots every item, in one transaction."""
    return await service.add_item(db, user, template_id, payload, **_ctx(request))


@router.patch(
    "/templates/{template_id}/items/{item_id}",
    response_model=TemplateDetail,
    dependencies=[Depends(require_admin)],
    summary="Edit an item",
)
async def update_item(
    template_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: UpdateItemRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> TemplateDetail:
    """Every field here is material: the edit bumps the version, and runs
    recorded before it keep rendering the old definition."""
    return await service.update_item(db, user, template_id, item_id, payload, **_ctx(request))


@router.delete(
    "/templates/{template_id}/items/{item_id}",
    response_model=TemplateDetail,
    dependencies=[Depends(require_admin)],
    summary="Remove an item",
)
async def delete_item(
    template_id: uuid.UUID,
    item_id: uuid.UUID,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> TemplateDetail:
    """Hard delete only if no run has ever answered against it; otherwise a
    soft delete that keeps history renderable."""
    return await service.delete_item(db, user, template_id, item_id, **_ctx(request))


@router.put(
    "/templates/{template_id}/items/reorder",
    response_model=TemplateDetail,
    dependencies=[Depends(require_admin)],
    summary="Reorder items",
)
async def reorder_items(
    template_id: uuid.UUID,
    payload: ReorderRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> TemplateDetail:
    """Takes every current item id in the new order. Partial lists are refused
    rather than silently dropping the forgotten items."""
    return await service.reorder_items(db, user, template_id, payload, **_ctx(request))


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------

_ASSIGNMENT_SQL = """
    select a.id, a.template_id, t.name as template_name, a.outlet_id,
           o.code as outlet_code, a.assigned_role, a.active_weekdays,
           a.interval_days, a.due_time_local, a.grace_minutes, a.is_active
      from checklist_assignments a
      join checklist_templates t on t.id = a.template_id
      join outlets o on o.id = a.outlet_id
     where a.deleted_at is null and t.deleted_at is null and o.deleted_at is null
"""


@router.get(
    "/assignments",
    response_model=list[Assignment],
    dependencies=[Depends(require_management)],
    summary="Template-to-outlet assignments",
)
async def list_assignments(
    db: DbDep,
    user: CurrentUserDep,
    outlet_id: uuid.UUID | None = Query(default=None),
) -> list[Assignment]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    if outlet_id is not None:
        clauses.append("a.outlet_id = :outlet_id")
        params["outlet_id"] = outlet_id
    elif not user.is_global:
        if not user.outlet_ids:
            return []
        clauses.append("a.outlet_id = any(:ids)")
        params["ids"] = sorted(user.outlet_ids)
    sql = _ASSIGNMENT_SQL + "".join(f" and {c}" for c in clauses)
    sql += " order by o.code, t.name"
    rows = (await db.execute(text(sql), params)).mappings()
    return [Assignment(**r) for r in rows]


@router.post(
    "/assignments",
    response_model=Assignment,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
    summary="Assign a template to an outlet",
)
async def create_assignment(
    payload: CreateAssignmentRequest, request: Request, db: DbDep, user: CurrentUserDep
) -> Assignment:
    template = (
        await db.execute(
            text("select name from checklist_templates where id = :id and deleted_at is null"),
            {"id": payload.template_id},
        )
    ).first()
    if template is None:
        raise NotFoundError("That template does not exist.")
    outlet = (
        await db.execute(
            text("select code from outlets where id = :id and deleted_at is null"),
            {"id": payload.outlet_id},
        )
    ).first()
    if outlet is None:
        raise NotFoundError("That outlet does not exist.")

    await service.upsert_assignment_conflict_check(db, payload.template_id, payload.outlet_id)

    assignment_id = (
        await db.execute(
            text(
                """
                insert into checklist_assignments
                    (template_id, outlet_id, assigned_role, active_weekdays,
                     interval_days, anchor_date, due_time_local, grace_minutes)
                values (:template_id, :outlet_id, cast(:assigned_role as user_role),
                        :active_weekdays,
                        :interval_days,
                        case when :interval_days is null then null
                             else current_date end,
                        :due_time_local, :grace_minutes)
                returning id
                """
            ),
            {**payload.model_dump(), "assigned_role": payload.assigned_role.value},
        )
    ).scalar_one()

    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=payload.outlet_id,
        entity_table="checklist_assignments",
        entity_id=assignment_id,
        action=AuditAction.CREATE,
        after={
            "template_id": str(payload.template_id),
            "due_time_local": str(payload.due_time_local),
        },
        **_ctx(request),
    )
    await db.commit()
    return await _get_assignment(db, assignment_id)


@router.patch(
    "/assignments/{assignment_id}",
    response_model=Assignment,
    dependencies=[Depends(require_admin)],
    summary="Edit an assignment",
)
async def update_assignment(
    assignment_id: uuid.UUID,
    payload: UpdateAssignmentRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> Assignment:
    before = await _get_assignment(db, assignment_id)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return before
    if changes.get("assigned_role") is not None:
        changes["assigned_role"] = changes["assigned_role"].value

    assignments_sql = ", ".join(
        f"{col} = cast(:{col} as user_role)" if col == "assigned_role" else f"{col} = :{col}"
        for col in changes
    )
    await db.execute(
        text(
            f"update checklist_assignments set {assignments_sql}"
            " where id = :id and deleted_at is null"
        ),
        {**changes, "id": assignment_id},
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=before.outlet_id,
        entity_table="checklist_assignments",
        entity_id=assignment_id,
        action=AuditAction.UPDATE,
        after={k: str(v) for k, v in changes.items()},
        **_ctx(request),
    )
    await db.commit()
    return await _get_assignment(db, assignment_id)


@router.delete(
    "/assignments/{assignment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
    summary="Remove an assignment",
)
async def delete_assignment(
    assignment_id: uuid.UUID, request: Request, db: DbDep, user: CurrentUserDep
) -> None:
    """Soft delete. Runs already materialised from it are untouched; no new
    ones are created."""
    before = await _get_assignment(db, assignment_id)
    await db.execute(
        text(
            "update checklist_assignments set deleted_at = now(), is_active = false where id = :id"
        ),
        {"id": assignment_id},
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=before.outlet_id,
        entity_table="checklist_assignments",
        entity_id=assignment_id,
        action=AuditAction.DELETE,
        before={"template_name": before.template_name, "outlet_code": before.outlet_code},
        **_ctx(request),
    )
    await db.commit()


async def _get_assignment(db: DbDep, assignment_id: uuid.UUID) -> Assignment:
    row = (
        (await db.execute(text(_ASSIGNMENT_SQL + " and a.id = :id"), {"id": assignment_id}))
        .mappings()
        .first()
    )
    if row is None:
        raise NotFoundError("That assignment does not exist.")
    return Assignment(**row)
