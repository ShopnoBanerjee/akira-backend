"""Recipes: the map from what the till sells to what the kitchen uses.

Brand-level, like the catalogue (D10) — one menu across outlets. The key is
the menu item name AS PETPOOJA PRINTS IT, because that string is the join to
both sales_item_days and sales_order_items; the same alias caveat as the
Item Wise reconciliation (OPEN_ITEMS) applies, and the unmapped worklist is
how those aliases get noticed.

A recipe's lines are replaced wholesale on save — a recipe is one fact about
one dish, not an append-only ledger. History lives in the audit log.
"""

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationError


async def list_recipes(db: AsyncSession) -> list[dict[str, Any]]:
    rows = (
        (
            await db.execute(
                text(
                    """
                    select r.id, r.menu_item_name, r.is_active, r.notes,
                           r.created_at, r.updated_at,
                           coalesce(
                               json_agg(
                                   json_build_object(
                                       'item_id', rl.item_id,
                                       'item_name', i.name,
                                       'unit', i.unit,
                                       'qty_per_unit', cast(rl.qty_per_unit as float8)
                                   ) order by i.name
                               ) filter (where rl.id is not null),
                               '[]'
                           ) as lines
                      from recipes r
                      left join recipe_lines rl on rl.recipe_id = r.id
                      left join inventory_items i on i.id = rl.item_id
                     group by r.id
                     order by r.menu_item_name
                    """
                )
            )
        )
        .mappings()
        .all()
    )
    import json

    out = []
    for r in rows:
        data = dict(r)
        raw = data["lines"]
        data["lines"] = json.loads(raw) if isinstance(raw, str) else raw
        out.append(data)
    return out


async def unmapped_names(db: AsyncSession) -> list[dict[str, Any]]:
    """Menu names the till has actually sold that no active recipe covers —
    the worklist. Ordered by units sold, because mapping the ramen that
    sells thirty a night matters before the seasonal special."""
    rows = (
        (
            await db.execute(
                text(
                    """
                    with sold as (
                        select item_name, sum(qty) as units, max(report_date) as last_seen
                          from sales_item_days group by item_name
                        union all
                        select item_name, null, max(business_date)
                          from sales_order_items group by item_name
                    ),
                    merged as (
                        select item_name,
                               max(units) as units,
                               max(last_seen) as last_seen
                          from sold group by item_name
                    )
                    select m.item_name, cast(m.units as float8) as units, m.last_seen
                      from merged m
                     where not exists (
                           select 1 from recipes r
                            where r.menu_item_name = m.item_name and r.is_active
                     )
                     order by m.units desc nulls last, m.item_name
                    """
                )
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def save_recipe(
    db: AsyncSession,
    *,
    menu_item_name: str,
    lines: list[dict[str, Any]],
    notes: str | None,
    is_active: bool,
    created_by: uuid.UUID,
) -> dict[str, Any]:
    """Create or replace the recipe for one menu item, lines wholesale."""
    if not lines:
        raise ValidationError("A recipe needs at least one ingredient line.")
    item_ids = [line["item_id"] for line in lines]
    if len(set(item_ids)) != len(item_ids):
        raise ValidationError("The same ingredient appears twice on this recipe.")
    known = {
        uuid.UUID(str(r[0]))
        for r in await db.execute(
            text(
                "select id from inventory_items"
                " where id = any(:ids) and is_active and deleted_at is null"
            ),
            {"ids": item_ids},
        )
    }
    missing = [str(i) for i in item_ids if i not in known]
    if missing:
        raise ValidationError(
            "Some ingredients are not active catalogue items.", extra={"item_ids": missing}
        )

    recipe_id = (
        await db.execute(
            text(
                """
                insert into recipes (menu_item_name, is_active, notes, created_by)
                values (:name, :active, :notes, :by)
                on conflict (menu_item_name) do update
                   set is_active = excluded.is_active,
                       notes = excluded.notes
                returning id
                """
            ),
            {"name": menu_item_name, "active": is_active, "notes": notes, "by": created_by},
        )
    ).scalar_one()
    await db.execute(text("delete from recipe_lines where recipe_id = :r"), {"r": recipe_id})
    await db.execute(
        text(
            """
            insert into recipe_lines (recipe_id, item_id, qty_per_unit)
            select :r, b.item_id, b.qty
              from unnest(cast(:items as uuid[]), cast(:qtys as numeric[])) as b(item_id, qty)
            """
        ),
        {
            "r": recipe_id,
            "items": item_ids,
            "qtys": [line["qty_per_unit"] for line in lines],
        },
    )
    return {"id": recipe_id, "menu_item_name": menu_item_name, "lines": len(lines)}


async def delete_recipe(db: AsyncSession, recipe_id: uuid.UUID) -> str:
    name = (
        await db.execute(
            text("delete from recipes where id = :id returning menu_item_name"),
            {"id": recipe_id},
        )
    ).scalar_one_or_none()
    if name is None:
        raise NotFoundError("That recipe does not exist.")
    return str(name)
