"""Feeding the inventory pillar: one statement per source, any outlet count.

Same shape as sales/pillar_service.py on purpose (D16): the dashboard's
comparison row calls this for every outlet in one round trip, and the single
outlet card gets the same statements underneath so they cannot disagree.

Requisitions and counts are dated by business_date, so the period bounds mean
the same trading days the sales pillar uses.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_value import resolve_many_outlets
from app.domains.inventory.pillar import InventoryInputs, InventoryTargets

_TARGET_KEYS = [
    "inventory.target.clean_req_share",
    "inventory.max.stockouts_28d",
    "inventory.weight.requisition_accuracy",
    "inventory.weight.stockouts",
    "scoring.band.green",
    "scoring.band.amber",
]

_AGG_SQL = text(
    """
    with wanted as (
        select unnest(cast(:ids as uuid[])) as outlet_id
    ),
    reqs as (
        select r.outlet_id,
               count(*)                                          as req_lines,
               count(*) filter (where 'padding' = any(rl.flags)) as padded_lines
          from requisition_lines rl
          join requisitions r on r.id = rl.requisition_id
         where r.outlet_id = any (cast(:ids as uuid[]))
           and r.status = 'final'
           and rl.final_qty is not null
           and r.business_date between :start and :end
         group by r.outlet_id
    ),
    counts as (
        select c.outlet_id,
               count(*)                             as counted_lines,
               count(*) filter (where l.qty = 0)    as stockout_lines
          from stock_count_lines l
          join stock_counts c on c.id = l.count_id
         where c.outlet_id = any (cast(:ids as uuid[]))
           and c.status = 'confirmed'
           and l.qty is not null
           and c.business_date between :start and :end
         group by c.outlet_id
    )
    select w.outlet_id,
           coalesce(r.req_lines, 0)      as req_lines,
           coalesce(r.padded_lines, 0)   as padded_lines,
           coalesce(c.counted_lines, 0)  as counted_lines,
           coalesce(c.stockout_lines, 0) as stockout_lines
      from wanted w
      left join reqs r on r.outlet_id = w.outlet_id
      left join counts c on c.outlet_id = w.outlet_id
    """
)


async def inventory_inputs_many(
    db: AsyncSession, *, outlet_ids: list[uuid.UUID], start: date, end: date
) -> dict[uuid.UUID, InventoryInputs]:
    if not outlet_ids:
        return {}
    rows = (
        (await db.execute(_AGG_SQL, {"ids": outlet_ids, "start": start, "end": end}))
        .mappings()
        .all()
    )
    period_days = (end - start).days + 1
    # asyncpg hands count() back as int already, but the boundary coercion
    # stays explicit — the pure module must never meet a Decimal.
    return {
        uuid.UUID(str(r["outlet_id"])): InventoryInputs(
            period_days=period_days,
            req_lines=int(r["req_lines"]),
            padded_lines=int(r["padded_lines"]),
            counted_lines=int(r["counted_lines"]),
            stockout_lines=int(r["stockout_lines"]),
        )
        for r in rows
    }


def _targets(values: dict[str, Any]) -> InventoryTargets:
    return InventoryTargets(
        clean_req_share=float(values["inventory.target.clean_req_share"]),
        max_stockouts_28d=float(values["inventory.max.stockouts_28d"]),
        w_requisition=float(values["inventory.weight.requisition_accuracy"]),
        w_stockouts=float(values["inventory.weight.stockouts"]),
        green=float(values["scoring.band.green"]),
        amber=float(values["scoring.band.amber"]),
    )


async def inventory_targets_many(
    db: AsyncSession, *, outlet_ids: list[uuid.UUID], at: datetime
) -> dict[uuid.UUID, InventoryTargets]:
    """Targets in force at the period's END, per outlet (D9)."""
    per_outlet = await resolve_many_outlets(db, _TARGET_KEYS, outlet_ids=outlet_ids, at=at)
    return {outlet: _targets(values) for outlet, values in per_outlet.items()}
