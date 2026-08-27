"""Building a requisition from a confirmed count.

Thin on purpose: the arithmetic lives in requisition.py where a unit test can
hold it down, and this module only feeds it inputs and files the results.
Every line's `detail` column carries the formula's working, because a manager
who cannot see the working will either trust every number or none of them.
"""

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.domains.inventory.requisition import LineInputs, compute_line


async def _require_outlet_access(user: CurrentUser, outlet_id: uuid.UUID) -> None:
    if not user.can_access_outlet(outlet_id):
        raise ForbiddenError("You do not have access to that outlet.")


async def build_from_count(
    db: AsyncSession, user: CurrentUser, count_id: uuid.UUID
) -> dict[str, Any]:
    """One requisition per confirmed count: on-hand from the sheet, par from
    the outlet's levels, the chef's ask carried through, arithmetic attached."""
    count = (
        (
            await db.execute(
                text(
                    "select id, outlet_id, business_date, status from stock_counts where id = :id"
                ),
                {"id": count_id},
            )
        )
        .mappings()
        .first()
    )
    if count is None:
        raise NotFoundError("That count does not exist.")
    await _require_outlet_access(user, count["outlet_id"])
    if count["status"] != "confirmed":
        raise ValidationError(
            "Requisitions are computed from confirmed counts only. Confirm the "
            "count first — the formula must not run on unreviewed numbers."
        )
    existing = (
        await db.execute(text("select id from requisitions where count_id = :c"), {"c": count_id})
    ).scalar()
    if existing:
        raise ConflictError(
            "A requisition already exists for this count.",
            extra={"requisition_id": str(existing)},
        )

    # Every mapped line of the count, joined to the outlet's par levels. A line
    # that never got an item mapping cannot be requisitioned — it shows on the
    # count screen, not here.
    lines = (
        (
            await db.execute(
                text(
                    """
                    select l.item_id, l.qty as on_hand, l.requested_qty,
                           lv.par_level, lv.order_unit
                      from stock_count_lines l
                      left join inventory_outlet_levels lv
                        on lv.item_id = l.item_id and lv.outlet_id = :outlet
                     where l.count_id = :count and l.item_id is not null
                    """
                ),
                {"count": count_id, "outlet": count["outlet_id"]},
            )
        )
        .mappings()
        .all()
    )
    if not lines:
        raise ValidationError("This count has no mapped lines to requisition from.")

    requisition_id = (
        await db.execute(
            text(
                """
                insert into requisitions
                    (outlet_id, count_id, business_date, status, created_by)
                values (:o, :c, :d, 'draft', :by)
                returning id
                """
            ),
            {
                "o": count["outlet_id"],
                "c": count_id,
                "d": count["business_date"],
                "by": user.profile_id,
            },
        )
    ).scalar_one()

    computed = 0
    for line in lines:
        result = compute_line(
            LineInputs(
                item_id=str(line["item_id"]),
                on_hand=float(line["on_hand"]) if line["on_hand"] is not None else None,
                par_level=float(line["par_level"]) if line["par_level"] is not None else None,
                order_unit=float(line["order_unit"]) if line["order_unit"] is not None else None,
                requested_qty=(
                    float(line["requested_qty"]) if line["requested_qty"] is not None else None
                ),
            )
        )
        await db.execute(
            text(
                """
                insert into requisition_lines
                    (requisition_id, item_id, on_hand, par_level, order_unit,
                     suggested_qty, requested_qty, final_qty, flags, detail)
                values (:r, :item, :on_hand, :par, :unit, :suggested,
                        :requested, :final, :flags, cast(:detail as jsonb))
                on conflict (requisition_id, item_id) do nothing
                """
            ),
            {
                "r": requisition_id,
                "item": line["item_id"],
                "on_hand": result.on_hand,
                "par": result.par_level,
                "unit": result.order_unit,
                "suggested": result.suggested_qty,
                "requested": result.requested_qty,
                "final": result.final_qty,
                "flags": result.flags,
                "detail": json.dumps(result.detail),
            },
        )
        computed += 1
    await db.commit()
    return {"requisition_id": str(requisition_id), "lines": computed}


async def set_final_qty(
    db: AsyncSession,
    user: CurrentUser,
    *,
    requisition_id: uuid.UUID,
    item_id: uuid.UUID,
    final_qty: float | None,
) -> dict[str, Any]:
    header = await _load_header(db, requisition_id)
    await _require_outlet_access(user, header["outlet_id"])
    if header["status"] == "final":
        raise ConflictError("This requisition is finalised.")
    updated = (
        await db.execute(
            text(
                """
                update requisition_lines
                   set final_qty = :qty
                 where requisition_id = :r and item_id = :item
                returning id
                """
            ),
            {"r": requisition_id, "item": item_id, "qty": final_qty},
        )
    ).scalar()
    if updated is None:
        raise NotFoundError("That item is not on this requisition.")
    await db.commit()
    return {"item_id": str(item_id), "final_qty": final_qty}


async def finalise(
    db: AsyncSession, user: CurrentUser, requisition_id: uuid.UUID
) -> dict[str, Any]:
    header = await _load_header(db, requisition_id)
    await _require_outlet_access(user, header["outlet_id"])
    if header["status"] == "final":
        raise ConflictError("This requisition is already finalised.")
    await db.execute(
        text(
            """
            update requisitions
               set status = 'final', finalised_by = :by, finalised_at = now()
             where id = :id
            """
        ),
        {"id": requisition_id, "by": user.profile_id},
    )
    await db.commit()
    return {"requisition_id": str(requisition_id), "status": "final"}


async def _load_header(db: AsyncSession, requisition_id: uuid.UUID) -> dict[str, Any]:
    header = (
        (
            await db.execute(
                text("select id, outlet_id, status from requisitions where id = :id"),
                {"id": requisition_id},
            )
        )
        .mappings()
        .first()
    )
    if header is None:
        raise NotFoundError("That requisition does not exist.")
    return dict(header)
