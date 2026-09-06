"""SOP template authoring, and the versioning rule that keeps history honest.

THE VERSIONING RULE (docs/DECISIONS.md D11). Any material change to a
template's items — adding one, editing one's meaning, removing one, reordering
them — must, in ONE transaction:

    1. bump checklist_templates.version,
    2. insert a checklist_template_item_versions row for every current item at
       the new version,
    3. write the audit entry.

Runs snapshot the version at creation, so a run from three weeks ago keeps
rendering against the definitions that were live then. Editing only the
template's name or description is not material and must not bump — the items
are the contract with history, not the label on the folder.
"""

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record
from app.core.deps import CurrentUser
from app.core.enums import AuditAction
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.domains.sop.schemas import (
    CreateTemplateRequest,
    ItemFields,
    ReorderRequest,
    TemplateDetail,
    TemplateItem,
    TemplateSummary,
    UpdateTemplateItemRequest,
    UpdateTemplateRequest,
)

#: Advisory ceilings from the spec — the known failure modes of checklist
#: programmes. Warn, never block.
MAX_RECOMMENDED_ITEMS = 15
MAX_RECOMMENDED_CRITICAL_SHARE = 0.5

_SUMMARY_SQL = """
    select t.id, t.name, t.name_bn, t.description, t.category_id,
           c.key as category_key, c.label as category_label,
           t.frequency, t.day_part, t.version, t.is_active,
           (select count(*) from checklist_template_items i
             where i.template_id = t.id and i.deleted_at is null) as item_count,
           (select count(*) from checklist_template_items i
             where i.template_id = t.id and i.deleted_at is null
               and i.is_critical) as critical_count,
           (select count(*) from checklist_assignments a
             where a.template_id = t.id and a.deleted_at is null
               and a.is_active) as assignment_count
      from checklist_templates t
      join sop_categories c on c.id = t.category_id
     where t.deleted_at is null
       and (t.organisation_id is null or t.organisation_id = cast(:org as uuid))
"""

_ITEMS_SQL = """
    select id, sort_order, title, title_bn, instruction, instruction_bn,
           reference_photo_path, requires_photo, requires_value, value_type,
           value_min, value_max, value_unit, is_critical, allow_na
      from checklist_template_items
     where template_id = :template_id and deleted_at is null
     order by sort_order
"""

#: Every column an item snapshot carries. One list, so insert and copy can
#: never drift apart.
_ITEM_COLUMNS = (
    "title, title_bn, instruction, instruction_bn, reference_photo_path, "
    "requires_photo, requires_value, value_type, value_min, value_max, "
    "value_unit, is_critical, allow_na"
)


def _warnings(item_count: int, critical_count: int) -> list[str]:
    warnings: list[str] = []
    if item_count > MAX_RECOMMENDED_ITEMS:
        warnings.append(
            f"This checklist has {item_count} items. Past about "
            f"{MAX_RECOMMENDED_ITEMS}, completion quality drops — staff start "
            "ticking without doing. Consider splitting it by day-part."
        )
    if item_count and critical_count / item_count > MAX_RECOMMENDED_CRITICAL_SHARE:
        warnings.append(
            f"{critical_count} of {item_count} items are critical. When most "
            "items escalate, none of them stand out — reserve critical for "
            "genuine food-safety and hygiene failures."
        )
    return warnings


# ---------------------------------------------------------------------------
# The versioning transaction
# ---------------------------------------------------------------------------


async def _bump_and_snapshot(
    db: AsyncSession,
    template_id: uuid.UUID,
    changed_by: uuid.UUID,
    change_note: str,
) -> int:
    """Steps 1 and 2 of the rule. Joins the caller's open transaction; the
    caller audits and commits, so the bump can never land without its snapshot
    or its audit trail."""
    new_version = (
        await db.execute(
            text(
                "update checklist_templates set version = version + 1"
                " where id = :id and deleted_at is null returning version"
            ),
            {"id": template_id},
        )
    ).scalar_one()

    await db.execute(
        text(
            f"""
            insert into checklist_template_item_versions
                (template_item_id, template_id, template_version, sort_order,
                 {_ITEM_COLUMNS}, is_deleted, changed_by, change_note)
            select id, template_id, :version, sort_order, {_ITEM_COLUMNS},
                   false, :changed_by, :note
              from checklist_template_items
             where template_id = :template_id and deleted_at is null
            """
        ),
        {
            "version": new_version,
            "template_id": template_id,
            "changed_by": changed_by,
            "note": change_note,
        },
    )
    return int(new_version)


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


async def list_templates(
    db: AsyncSession,
    *,
    category_id: uuid.UUID | None,
    include_inactive: bool,
    organisation_id: uuid.UUID | None,
) -> list[TemplateSummary]:
    """The organisation's templates plus the starter kit's (D33)."""
    clauses: list[str] = []
    params: dict[str, Any] = {"org": organisation_id}
    if category_id is not None:
        clauses.append("t.category_id = :category_id")
        params["category_id"] = category_id
    if not include_inactive:
        clauses.append("t.is_active")
    sql = _SUMMARY_SQL + "".join(f" and {c}" for c in clauses)
    sql += " order by c.sort_order, t.name"
    rows = (await db.execute(text(sql), params)).mappings()
    return [TemplateSummary(**r) for r in rows]


async def get_template(
    db: AsyncSession, template_id: uuid.UUID, *, organisation_id: uuid.UUID | None
) -> TemplateDetail:
    row = (
        (
            await db.execute(
                text(_SUMMARY_SQL + " and t.id = :id"),
                {"id": template_id, "org": organisation_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFoundError("That template does not exist.")
    items = (await db.execute(text(_ITEMS_SQL), {"template_id": template_id})).mappings()
    return TemplateDetail(
        **row,
        items=[TemplateItem(**i) for i in items],
        warnings=_warnings(row["item_count"], row["critical_count"]),
    )


async def create_template(
    db: AsyncSession, user: CurrentUser, payload: CreateTemplateRequest, **audit_ctx: Any
) -> TemplateDetail:
    category = (
        await db.execute(
            text(
                "select 1 from sop_categories where id = :id"
                "   and (organisation_id is null or organisation_id = cast(:org as uuid))"
            ),
            {"id": payload.category_id, "org": user.organisation_id},
        )
    ).first()
    if category is None:
        raise NotFoundError("That category does not exist.")

    template_id = (
        await db.execute(
            text(
                """
                insert into checklist_templates
                    (organisation_id, category_id, name, name_bn, description, frequency,
                     day_part, version, created_by)
                values (:org, :category_id, :name, :name_bn, :description,
                        cast(:frequency as frequency), cast(:day_part as day_part),
                        1, :created_by)
                returning id
                """
            ),
            {
                "org": user.organisation_id,
                **payload.model_dump(),
                "frequency": payload.frequency.value,
                "day_part": payload.day_part.value,
                "created_by": user.profile_id,
            },
        )
    ).scalar_one()

    await record(
        db,
        actor_profile_id=user.profile_id,
        entity_table="checklist_templates",
        entity_id=template_id,
        action=AuditAction.CREATE,
        after={"name": payload.name, "frequency": payload.frequency.value},
        **audit_ctx,
    )
    await db.commit()
    return await get_template(db, template_id, organisation_id=user.organisation_id)


async def update_template(
    db: AsyncSession,
    user: CurrentUser,
    template_id: uuid.UUID,
    payload: UpdateTemplateRequest,
    **audit_ctx: Any,
) -> TemplateDetail:
    """Template-level fields only — no version bump. See the module docstring."""
    before = await get_template(db, template_id, organisation_id=user.organisation_id)
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return before

    for enum_field in ("frequency", "day_part"):
        if enum_field in changes and changes[enum_field] is not None:
            changes[enum_field] = changes[enum_field].value

    assignments = ", ".join(
        f"{col} = cast(:{col} as frequency)"
        if col == "frequency"
        else f"{col} = cast(:{col} as day_part)"
        if col == "day_part"
        else f"{col} = :{col}"
        for col in changes
    )
    await db.execute(
        text(f"update checklist_templates set {assignments} where id = :id and deleted_at is null"),
        {**changes, "id": template_id},
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        entity_table="checklist_templates",
        entity_id=template_id,
        action=AuditAction.UPDATE,
        before={k: str(getattr(before, k, None)) for k in changes},
        after={k: str(v) for k, v in changes.items()},
        **audit_ctx,
    )
    await db.commit()
    return await get_template(db, template_id, organisation_id=user.organisation_id)


async def duplicate_template(
    db: AsyncSession, user: CurrentUser, template_id: uuid.UUID, **audit_ctx: Any
) -> TemplateDetail:
    """A fresh version-1 copy, unassigned. The copy's history starts clean —
    it inherits the items, not the past."""
    source = await get_template(db, template_id, organisation_id=user.organisation_id)

    new_id = (
        await db.execute(
            text(
                """
                insert into checklist_templates
                    (organisation_id, category_id, name, name_bn, description, frequency,
                     day_part, version, is_active, created_by)
                select :org, category_id, 'Copy of ' || name, name_bn, description,
                       frequency, day_part, 1, false, :created_by
                  from checklist_templates where id = :id
                returning id
                """
            ),
            {"id": template_id, "created_by": user.profile_id, "org": user.organisation_id},
        )
    ).scalar_one()

    await db.execute(
        text(
            f"""
            insert into checklist_template_items
                (template_id, sort_order, {_ITEM_COLUMNS})
            select :new_id, sort_order, {_ITEM_COLUMNS}
              from checklist_template_items
             where template_id = :source_id and deleted_at is null
            """
        ),
        {"new_id": new_id, "source_id": template_id},
    )
    await db.execute(
        text(
            f"""
            insert into checklist_template_item_versions
                (template_item_id, template_id, template_version, sort_order,
                 {_ITEM_COLUMNS}, is_deleted, changed_by, change_note)
            select id, template_id, 1, sort_order, {_ITEM_COLUMNS},
                   false, :changed_by, 'Duplicated from ' || :source_name
              from checklist_template_items
             where template_id = :new_id
            """
        ),
        {"new_id": new_id, "changed_by": user.profile_id, "source_name": source.name},
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        entity_table="checklist_templates",
        entity_id=new_id,
        action=AuditAction.CREATE,
        after={"duplicated_from": str(template_id), "name": f"Copy of {source.name}"},
        **audit_ctx,
    )
    await db.commit()
    return await get_template(db, new_id, organisation_id=user.organisation_id)


# ---------------------------------------------------------------------------
# Items — every mutation here is material and goes through the bump
# ---------------------------------------------------------------------------


async def add_item(
    db: AsyncSession,
    user: CurrentUser,
    template_id: uuid.UUID,
    payload: ItemFields,
    **audit_ctx: Any,
) -> TemplateDetail:
    # 404 before any write
    await get_template(db, template_id, organisation_id=user.organisation_id)

    values = payload.model_dump()
    if values.get("value_type") is not None:
        values["value_type"] = values["value_type"].value

    item_id = (
        await db.execute(
            text(
                f"""
                insert into checklist_template_items
                    (template_id, sort_order, {_ITEM_COLUMNS})
                values (:template_id,
                        coalesce((select max(sort_order) + 1
                                    from checklist_template_items
                                   where template_id = :template_id
                                     and deleted_at is null), 1),
                        :title, :title_bn, :instruction, :instruction_bn, null,
                        :requires_photo, :requires_value,
                        cast(:value_type as value_type),
                        :value_min, :value_max, :value_unit, :is_critical, :allow_na)
                returning id
                """
            ),
            {**values, "template_id": template_id},
        )
    ).scalar_one()

    version = await _bump_and_snapshot(
        db, template_id, user.profile_id, f"Added item: {payload.title}"
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        entity_table="checklist_template_items",
        entity_id=item_id,
        action=AuditAction.CREATE,
        after={"title": payload.title, "template_version": version},
        **audit_ctx,
    )
    await db.commit()
    return await get_template(db, template_id, organisation_id=user.organisation_id)


async def update_item(
    db: AsyncSession,
    user: CurrentUser,
    template_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: UpdateTemplateItemRequest,
    **audit_ctx: Any,
) -> TemplateDetail:
    before = (
        (
            await db.execute(
                text(
                    "select title, requires_photo, requires_value, value_type,"
                    " value_min, value_max, is_critical, allow_na"
                    " from checklist_template_items"
                    " where id = :id and template_id = :template_id and deleted_at is null"
                ),
                {"id": item_id, "template_id": template_id},
            )
        )
        .mappings()
        .first()
    )
    if before is None:
        raise NotFoundError("That item does not exist on this template.")

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return await get_template(db, template_id, organisation_id=user.organisation_id)
    if changes.get("value_type") is not None:
        changes["value_type"] = changes["value_type"].value

    assignments = ", ".join(
        f"{col} = cast(:{col} as value_type)" if col == "value_type" else f"{col} = :{col}"
        for col in changes
    )
    await db.execute(
        text(f"update checklist_template_items set {assignments} where id = :id"),
        {**changes, "id": item_id},
    )

    version = await _bump_and_snapshot(
        db, template_id, user.profile_id, f"Edited item: {before['title']}"
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        entity_table="checklist_template_items",
        entity_id=item_id,
        action=AuditAction.UPDATE,
        before={k: str(before.get(k)) for k in changes if k in before},
        after={k: str(v) for k, v in changes.items()} | {"template_version": str(version)},
        **audit_ctx,
    )
    await db.commit()
    return await get_template(db, template_id, organisation_id=user.organisation_id)


async def delete_item(
    db: AsyncSession,
    user: CurrentUser,
    template_id: uuid.UUID,
    item_id: uuid.UUID,
    **audit_ctx: Any,
) -> TemplateDetail:
    """Hard delete only if no run has ever answered against it. Once used,
    history must stay renderable, so it soft-deletes instead."""
    item = (
        (
            await db.execute(
                text(
                    "select title from checklist_template_items"
                    " where id = :id and template_id = :template_id and deleted_at is null"
                ),
                {"id": item_id, "template_id": template_id},
            )
        )
        .mappings()
        .first()
    )
    if item is None:
        raise NotFoundError("That item does not exist on this template.")

    used = (
        await db.execute(
            text("select 1 from checklist_run_items where template_item_id = :id limit 1"),
            {"id": item_id},
        )
    ).first()

    if used:
        await db.execute(
            text("update checklist_template_items set deleted_at = now() where id = :id"),
            {"id": item_id},
        )
        mode = "soft"
    else:
        # Version rows cascade with the item; nothing has ever referenced them.
        await db.execute(
            text("delete from checklist_template_items where id = :id"), {"id": item_id}
        )
        mode = "hard"

    version = await _bump_and_snapshot(
        db, template_id, user.profile_id, f"Removed item: {item['title']}"
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        entity_table="checklist_template_items",
        entity_id=item_id,
        action=AuditAction.DELETE,
        before={"title": item["title"]},
        after={"mode": mode, "template_version": str(version)},
        **audit_ctx,
    )
    await db.commit()
    return await get_template(db, template_id, organisation_id=user.organisation_id)


async def reorder_items(
    db: AsyncSession,
    user: CurrentUser,
    template_id: uuid.UUID,
    payload: ReorderRequest,
    **audit_ctx: Any,
) -> TemplateDetail:
    """One transaction, complete list required. The deferred unique constraint
    on (template_id, sort_order) lets every row move at once without a
    collision mid-flight."""
    current = {
        r[0]
        for r in await db.execute(
            text(
                "select id from checklist_template_items"
                " where template_id = :id and deleted_at is null"
            ),
            {"id": template_id},
        )
    }
    if not current:
        raise NotFoundError("That template does not exist or has no items.")

    requested = list(payload.item_ids)
    if len(requested) != len(set(requested)):
        raise ValidationError("The order lists an item twice.")
    if set(requested) != current:
        missing = current - set(requested)
        extra = set(requested) - current
        raise ValidationError(
            "The order must name every current item exactly once.",
            extra={
                "missing": [str(i) for i in missing],
                "unknown": [str(i) for i in extra],
            },
        )

    # One statement, not a loop. Row-by-row updates acquire row locks in list
    # order, and two concurrent reorders then deadlock against each other
    # (observed live). A single UPDATE takes its locks in one scan order.
    await db.execute(
        text(
            """
            update checklist_template_items i
               set sort_order = v.position
              from (select unnest(cast(:ids as uuid[])) as id,
                           generate_series(1, cardinality(cast(:ids as uuid[]))) as position) v
             where i.id = v.id
            """
        ),
        {"ids": requested},
    )

    version = await _bump_and_snapshot(db, template_id, user.profile_id, "Reordered items")
    await record(
        db,
        actor_profile_id=user.profile_id,
        entity_table="checklist_templates",
        entity_id=template_id,
        action=AuditAction.UPDATE,
        after={"reordered": True, "template_version": str(version)},
        **audit_ctx,
    )
    await db.commit()
    return await get_template(db, template_id, organisation_id=user.organisation_id)


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------


async def upsert_assignment_conflict_check(
    db: AsyncSession, template_id: uuid.UUID, outlet_id: uuid.UUID
) -> None:
    clash = (
        await db.execute(
            text(
                "select 1 from checklist_assignments"
                " where template_id = :template_id and outlet_id = :outlet_id"
                "   and deleted_at is null"
            ),
            {"template_id": template_id, "outlet_id": outlet_id},
        )
    ).first()
    if clash:
        raise ConflictError(
            "That template is already assigned to that outlet. "
            "Edit the existing assignment instead of adding a second."
        )
