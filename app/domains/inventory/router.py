"""Inventory catalogue administration.

One shared catalogue across outlets, levels per outlet (docs/DECISIONS.md D10):
an item is added once and each outlet sets its own par level. Stage 1 ships
catalogue and levels only — the counting flow and requisition engine build on
these tables in Stage 2.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record
from app.core.deps import CurrentUserDep, DbDep, require_admin, require_management
from app.core.enums import AuditAction, InventoryUnit
from app.core.errors import ConflictError, ForbiddenError, NotFoundError

router = APIRouter(prefix="/inventory", tags=["inventory"])


class Department(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    label: str
    label_bn: str | None
    sort_order: int
    item_count: int = 0


class Category(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    label: str
    label_bn: str | None
    sort_order: int


class OutletLevel(BaseModel):
    outlet_id: uuid.UUID
    outlet_code: str
    par_level: float | None
    reorder_qty: float | None
    order_unit: float | None
    is_stocked: bool


class Item(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    name_bn: str | None
    department_id: uuid.UUID
    department_label: str
    category_id: uuid.UUID | None
    category_label: str | None
    unit: InventoryUnit
    notes: str | None
    is_active: bool
    levels: list[OutletLevel] = []


class CreateItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    name_bn: str | None = Field(default=None, max_length=120)
    department_id: uuid.UUID
    category_id: uuid.UUID | None = None
    unit: InventoryUnit
    notes: str | None = Field(default=None, max_length=500)


class UpdateItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    name_bn: str | None = Field(default=None, max_length=120)
    category_id: uuid.UUID | None = None
    unit: InventoryUnit | None = None
    notes: str | None = Field(default=None, max_length=500)
    is_active: bool | None = None


class SetLevelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    par_level: float | None = Field(default=None, ge=0)
    reorder_qty: float | None = Field(default=None, ge=0)
    order_unit: float | None = Field(default=None, gt=0)
    is_stocked: bool = True


@router.get(
    "/departments",
    response_model=list[Department],
    dependencies=[Depends(require_management)],
    summary="Departments (count stations)",
)
async def list_departments(db: DbDep, user: CurrentUserDep) -> list[Department]:
    """The organisation's departments, plus the starter kit's (D33)."""
    rows = (
        await db.execute(
            text(
                """
                select d.id, d.key, d.label, d.label_bn, d.sort_order,
                       count(i.id) filter (where i.deleted_at is null) as item_count
                  from inventory_departments d
                  left join inventory_items i on i.department_id = d.id
                 where d.deleted_at is null
                   and (d.organisation_id is null or d.organisation_id = cast(:org as uuid))
                 group by d.id
                 order by d.sort_order
                """
            ),
            {"org": user.organisation_id},
        )
    ).mappings()
    return [Department(**r) for r in rows]


@router.get(
    "/categories",
    response_model=list[Category],
    dependencies=[Depends(require_management)],
    summary="Item categories",
)
async def list_categories(db: DbDep, user: CurrentUserDep) -> list[Category]:
    rows = (
        await db.execute(
            text(
                "select id, key, label, label_bn, sort_order from inventory_categories"
                " where deleted_at is null"
                "   and (organisation_id is null or organisation_id = cast(:org as uuid))"
                " order by sort_order"
            ),
            {"org": user.organisation_id},
        )
    ).mappings()
    return [Category(**r) for r in rows]


_ITEM_COLUMNS = """
    i.id, i.name, i.name_bn, i.department_id, d.label as department_label,
    i.category_id, c.label as category_label, i.unit, i.notes, i.is_active
"""
_ITEM_FROM = """
    from inventory_items i
    join inventory_departments d on d.id = i.department_id
    left join inventory_categories c on c.id = i.category_id
"""
#: An organisation reads its own catalogue rows and the starter kit's (D33)...
_ORG_READ = "(i.organisation_id is null or i.organisation_id = cast(:org as uuid))"
#: ...and edits only its own.
_ORG_WRITE = "i.organisation_id = cast(:org as uuid)"


async def _levels_for(
    db: AsyncSession, item_ids: list[uuid.UUID], outlet_ids: list[uuid.UUID] | None
) -> dict[uuid.UUID, list[OutletLevel]]:
    """Levels for a page of items in one query, scoped to visible outlets."""
    if not item_ids:
        return {}
    clauses = ["l.item_id = any(:item_ids)", "o.deleted_at is null"]
    params: dict[str, Any] = {"item_ids": item_ids}
    if outlet_ids is not None:
        clauses.append("l.outlet_id = any(:outlet_ids)")
        params["outlet_ids"] = outlet_ids
    rows = (
        await db.execute(
            text(
                f"""
                select l.item_id, l.outlet_id, o.code as outlet_code,
                       l.par_level, l.reorder_qty, l.order_unit, l.is_stocked
                  from inventory_outlet_levels l
                  join outlets o on o.id = l.outlet_id
                 where {" and ".join(clauses)}
                 order by o.code
                """
            ),
            params,
        )
    ).mappings()
    grouped: dict[uuid.UUID, list[OutletLevel]] = {}
    for r in rows:
        grouped.setdefault(r["item_id"], []).append(
            OutletLevel(
                outlet_id=r["outlet_id"],
                outlet_code=r["outlet_code"],
                par_level=r["par_level"],
                reorder_qty=r["reorder_qty"],
                order_unit=r["order_unit"],
                is_stocked=r["is_stocked"],
            )
        )
    return grouped


@router.get(
    "/items",
    response_model=list[Item],
    dependencies=[Depends(require_management)],
    summary="The item catalogue",
)
async def list_items(
    db: DbDep,
    user: CurrentUserDep,
    department_id: uuid.UUID | None = Query(default=None),
    search: str | None = Query(default=None, max_length=80),
    include_inactive: bool = Query(default=False),
) -> list[Item]:
    """The shared catalogue. Levels are included for the outlets the caller can
    see, so an outlet manager reads their own pars without seeing another
    outlet's."""
    clauses = ["i.deleted_at is null", _ORG_READ]
    params: dict[str, Any] = {"org": user.organisation_id}
    if not include_inactive:
        clauses.append("i.is_active")
    if department_id is not None:
        clauses.append("i.department_id = :department_id")
        params["department_id"] = department_id
    if search:
        # Search both scripts: staff think in Bengali, invoices arrive in English.
        clauses.append("(i.name ilike :search or coalesce(i.name_bn,'') like :search_raw)")
        params["search"] = f"%{search}%"
        params["search_raw"] = f"%{search}%"

    sql = (
        f"select {_ITEM_COLUMNS}{_ITEM_FROM} where {' and '.join(clauses)}"
        " order by d.sort_order, i.name"
    )
    rows = [dict(r) for r in (await db.execute(text(sql), params)).mappings()]

    visible = None if user.is_platform_admin else sorted(user.outlet_ids)
    levels = await _levels_for(db, [r["id"] for r in rows], visible)
    return [Item(**r, levels=levels.get(r["id"], [])) for r in rows]


@router.post(
    "/items",
    response_model=Item,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
    summary="Add an item to the catalogue",
)
async def create_item(
    payload: CreateItemRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> Item:
    """Owner or operations manager. The item becomes available to every outlet;
    each outlet sets its own par level."""
    department = (
        await db.execute(
            text(
                "select id from inventory_departments where id = :id and deleted_at is null"
                "   and (organisation_id is null or organisation_id = cast(:org as uuid))"
            ),
            {"id": payload.department_id, "org": user.organisation_id},
        )
    ).first()
    if department is None:
        raise NotFoundError("That department does not exist.")

    duplicate = (
        await db.execute(
            text(
                "select 1 from inventory_items i"
                " where i.department_id = :dept and lower(i.name) = lower(:name)"
                f"   and i.deleted_at is null and {_ORG_READ}"
            ),
            {"dept": payload.department_id, "name": payload.name, "org": user.organisation_id},
        )
    ).first()
    if duplicate:
        raise ConflictError(
            f"{payload.name} already exists in that department.",
            extra={"field": "name"},
        )

    item_id = (
        await db.execute(
            text(
                """
                insert into inventory_items
                    (organisation_id, name, name_bn, department_id, category_id, unit, notes,
                     created_by)
                values (:org, :name, :name_bn, :department_id, :category_id,
                        cast(:unit as inventory_unit), :notes, :created_by)
                returning id
                """
            ),
            {
                **payload.model_dump(),
                "unit": payload.unit.value,
                "created_by": user.profile_id,
                "org": user.organisation_id,
            },
        )
    ).scalar_one()

    await record(
        db,
        actor_profile_id=user.profile_id,
        entity_table="inventory_items",
        entity_id=item_id,
        action=AuditAction.CREATE,
        after={"name": payload.name, "unit": payload.unit.value},
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    return await _get_item(db, user, item_id)


@router.patch(
    "/items/{item_id}",
    response_model=Item,
    dependencies=[Depends(require_admin)],
    summary="Edit a catalogue item",
)
async def update_item(
    item_id: uuid.UUID,
    payload: UpdateItemRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> Item:
    before = (
        (
            await db.execute(
                text(
                    "select name, name_bn, unit, notes, is_active from inventory_items i"
                    f" where i.id = :id and i.deleted_at is null and {_ORG_WRITE}"
                ),
                {"id": item_id, "org": user.organisation_id},
            )
        )
        .mappings()
        .first()
    )
    if before is None:
        raise NotFoundError("That item does not exist.")

    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return await _get_item(db, user, item_id)
    if "unit" in changes and changes["unit"] is not None:
        changes["unit"] = changes["unit"].value

    assignments = ", ".join(
        f"{column} = cast(:{column} as inventory_unit)"
        if column == "unit"
        else f"{column} = :{column}"
        for column in changes
    )
    await db.execute(
        text(f"update inventory_items set {assignments} where id = :id"),
        {**changes, "id": item_id},
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        entity_table="inventory_items",
        entity_id=item_id,
        action=AuditAction.UPDATE,
        before={k: str(before.get(k)) for k in changes},
        after={k: str(v) for k, v in changes.items()},
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    return await _get_item(db, user, item_id)


@router.delete(
    "/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_admin)],
    summary="Retire a catalogue item",
)
async def delete_item(
    item_id: uuid.UUID,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> None:
    """Soft delete. History and past counts keep pointing at it; it simply
    stops appearing in the catalogue and on count sheets."""
    before = (
        (
            await db.execute(
                text(
                    "select name from inventory_items i"
                    f" where i.id = :id and i.deleted_at is null and {_ORG_WRITE}"
                ),
                {"id": item_id, "org": user.organisation_id},
            )
        )
        .mappings()
        .first()
    )
    if before is None:
        raise NotFoundError("That item does not exist.")
    await db.execute(
        text("update inventory_items set deleted_at = now(), is_active = false where id = :id"),
        {"id": item_id},
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        entity_table="inventory_items",
        entity_id=item_id,
        action=AuditAction.DELETE,
        before={"name": before["name"]},
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()


@router.put(
    "/items/{item_id}/levels/{outlet_id}",
    response_model=Item,
    dependencies=[Depends(require_management)],
    summary="Set an outlet's par level for an item",
)
async def set_level(
    item_id: uuid.UUID,
    outlet_id: uuid.UUID,
    payload: SetLevelRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> Item:
    """An outlet manager sets levels for their own outlet; owners and
    operations managers for any. is_stocked false means the outlet deliberately
    does not carry the item, which is different from never having configured it.
    """
    if not user.can_access_outlet(outlet_id):
        raise ForbiddenError(
            "You do not have access to that outlet.",
            extra={"outlet_id": str(outlet_id)},
        )
    item = (
        await db.execute(
            text(
                "select 1 from inventory_items i"
                f" where i.id = :id and i.deleted_at is null and {_ORG_READ}"
            ),
            {"id": item_id, "org": user.organisation_id},
        )
    ).first()
    if item is None:
        raise NotFoundError("That item does not exist.")

    await db.execute(
        text(
            """
            insert into inventory_outlet_levels
                (outlet_id, item_id, par_level, reorder_qty, order_unit, is_stocked, updated_by)
            values (:outlet_id, :item_id, :par_level, :reorder_qty, :order_unit,
                    :is_stocked, :updated_by)
            on conflict (outlet_id, item_id) do update set
                par_level   = excluded.par_level,
                reorder_qty = excluded.reorder_qty,
                order_unit  = excluded.order_unit,
                is_stocked  = excluded.is_stocked,
                updated_by  = excluded.updated_by
            """
        ),
        {
            "outlet_id": outlet_id,
            "item_id": item_id,
            "updated_by": user.profile_id,
            **payload.model_dump(),
        },
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=outlet_id,
        entity_table="inventory_outlet_levels",
        entity_id=item_id,
        action=AuditAction.UPDATE,
        after=payload.model_dump(),
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    return await _get_item(db, user, item_id)


async def _get_item(db: AsyncSession, user: CurrentUserDep, item_id: uuid.UUID) -> Item:
    row = (
        (
            await db.execute(
                text(
                    f"select {_ITEM_COLUMNS}{_ITEM_FROM} where i.id = :id and i.deleted_at is null"
                ),
                {"id": item_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFoundError("That item does not exist.")
    visible = None if user.is_platform_admin else sorted(user.outlet_ids)
    levels = await _levels_for(db, [item_id], visible)
    return Item(**row, levels=levels.get(item_id, []))


# ---------------------------------------------------------------------------
# Recipes (P17) — what one sold dish uses from the catalogue
# ---------------------------------------------------------------------------


class RecipeLineIn(BaseModel):
    item_id: uuid.UUID
    qty_per_unit: float = Field(gt=0)


class RecipeSave(BaseModel):
    lines: list[RecipeLineIn] = Field(min_length=1)
    notes: str | None = None
    is_active: bool = True


class RecipeLineOut(BaseModel):
    item_id: uuid.UUID
    item_name: str
    unit: str
    qty_per_unit: float


class Recipe(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    menu_item_name: str
    is_active: bool
    notes: str | None
    lines: list[RecipeLineOut]


class UnmappedName(BaseModel):
    item_name: str
    #: Units sold where the Item Day Wise report covers the name; null when
    #: only the Order Listing has seen it (which carries no quantities).
    units: float | None
    last_seen: Any


@router.get(
    "/recipes",
    response_model=list[Recipe],
    dependencies=[Depends(require_management)],
    summary="Every recipe, with its ingredient lines",
)
async def list_recipes(db: DbDep, user: CurrentUserDep) -> list[Recipe]:
    from app.domains.inventory import recipes_service

    rows = await recipes_service.list_recipes(db, organisation_id=user.organisation_id)
    return [Recipe(**r) for r in rows]


@router.get(
    "/recipes/unmapped",
    response_model=list[UnmappedName],
    dependencies=[Depends(require_management)],
    summary="Sold menu items with no recipe yet",
)
async def unmapped(db: DbDep, user: CurrentUserDep) -> list[UnmappedName]:
    """The worklist, ordered by units sold — map the ramen that sells thirty
    a night before the seasonal special. Theoretical consumption only counts
    what is mapped, so this list is the honesty gap made visible."""
    from app.domains.inventory import recipes_service

    rows = await recipes_service.unmapped_names(db, organisation_id=user.organisation_id)
    return [UnmappedName(**r) for r in rows]


@router.put(
    "/recipes/{menu_item_name}",
    dependencies=[Depends(require_admin)],
    summary="Create or replace one dish's recipe",
)
async def save_recipe(
    menu_item_name: str,
    payload: RecipeSave,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> dict[str, Any]:
    """Lines are replaced wholesale — a recipe is one fact about one dish.
    The name must match what Petpooja prints on bills; the unmapped list is
    where those exact strings come from."""
    from app.domains.inventory import recipes_service

    if user.organisation_id is None:
        raise ForbiddenError("Recipes belong to an organisation; this login has none.")
    result = await recipes_service.save_recipe(
        db,
        organisation_id=user.organisation_id,
        menu_item_name=menu_item_name.strip(),
        lines=[line.model_dump() for line in payload.lines],
        notes=payload.notes,
        is_active=payload.is_active,
        created_by=user.profile_id,
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        entity_table="recipes",
        entity_id=result["id"],
        action=AuditAction.UPDATE,
        after={
            "menu_item_name": menu_item_name,
            "lines": [
                {"item_id": str(line.item_id), "qty_per_unit": line.qty_per_unit}
                for line in payload.lines
            ],
        },
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    return {**result, "id": str(result["id"])}


@router.delete(
    "/recipes/{recipe_id}",
    dependencies=[Depends(require_admin)],
    summary="Remove a recipe",
)
async def delete_recipe(
    recipe_id: uuid.UUID,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> dict[str, Any]:
    from app.domains.inventory import recipes_service

    name = await recipes_service.delete_recipe(db, recipe_id, organisation_id=user.organisation_id)
    await record(
        db,
        actor_profile_id=user.profile_id,
        entity_table="recipes",
        entity_id=recipe_id,
        action=AuditAction.DELETE,
        after={"menu_item_name": name},
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    return {"id": str(recipe_id), "deleted": True, "menu_item_name": name}
