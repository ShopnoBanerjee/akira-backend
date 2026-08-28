"""Feeding the sales pillar: one statement per outlet set, targets resolved
at the period's end.

Same shape as sop/metrics.py on purpose — the dashboard calls it for many
outlets in one round trip (D16), the digest for one, and both get the same
statement underneath so they cannot disagree.

Trading day = a business date with at least one bill. A shut Monday is not a
failed Monday; it is simply not in the denominator — the same principle the
run score applies to N/A items.
"""

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_value import resolve_many_outlets
from app.domains.sales.pillar import SalesInputs, SalesTargets

_TARGET_KEYS = [
    "sales.target.net_per_day_paise",
    "sales.amber.net_per_day_paise",
    "sales.target.orders_per_day",
    "sales.target.aov_paise",
    "sales.target.monwed_share",
    "sales.target.phone_capture",
    "sales.max.discount_share",
    "sales.weight.net",
    "sales.weight.orders",
    "sales.weight.monwed",
    "sales.weight.phone",
    "sales.weight.aov",
    "sales.weight.discount",
    "scoring.band.green",
    "scoring.band.amber",
]

#: One statement, any number of outlets. `wanted` keeps an outlet with no
#: sales in the period on the result — as zeroes, which the pillar reads as
#: "not measured", never as a crash or a missing row.
_AGG_SQL = text(
    """
    with wanted as (
        select unnest(cast(:ids as uuid[])) as outlet_id
    ),
    agg as (
        select
            outlet_id,
            count(distinct business_date)                       as trading_days,
            count(*)                                            as bills,
            coalesce(sum(net_paise), 0)                         as net_paise,
            coalesce(sum(gross_paise), 0)                       as gross_paise,
            coalesce(sum(discount_paise), 0)                    as discount_paise,
            -- Postgres dow: Monday 1 .. Wednesday 3, on the BUSINESS date —
            -- a bill at 00:45 belongs to the trading night that earned it.
            coalesce(sum(net_paise) filter (
                where extract(isodow from business_date) between 1 and 3
            ), 0)                                               as monwed_net_paise,
            count(*) filter (where customer_phone_hash is not null)
                                                                as bills_with_phone
          from sales_orders
         where outlet_id = any (cast(:ids as uuid[]))
           and business_date between :start and :end
         group by outlet_id
    )
    select w.outlet_id,
           coalesce(a.trading_days, 0)     as trading_days,
           coalesce(a.bills, 0)            as bills,
           coalesce(a.net_paise, 0)        as net_paise,
           coalesce(a.gross_paise, 0)      as gross_paise,
           coalesce(a.discount_paise, 0)   as discount_paise,
           coalesce(a.monwed_net_paise, 0) as monwed_net_paise,
           coalesce(a.bills_with_phone, 0) as bills_with_phone
      from wanted w
      left join agg a on a.outlet_id = w.outlet_id
    """
)


async def sales_inputs_many(
    db: AsyncSession, *, outlet_ids: list[uuid.UUID], start: date, end: date
) -> dict[uuid.UUID, SalesInputs]:
    if not outlet_ids:
        return {}
    rows = (
        (await db.execute(_AGG_SQL, {"ids": outlet_ids, "start": start, "end": end}))
        .mappings()
        .all()
    )
    # asyncpg hands sum() back as Decimal; the pillar does float arithmetic.
    # Coerced HERE, at the boundary, so the pure module never sees a Decimal.
    return {
        uuid.UUID(str(r["outlet_id"])): SalesInputs(
            trading_days=int(r["trading_days"]),
            bills=int(r["bills"]),
            net_paise=int(r["net_paise"]),
            gross_paise=int(r["gross_paise"]),
            discount_paise=int(r["discount_paise"]),
            monwed_net_paise=int(r["monwed_net_paise"]),
            bills_with_phone=int(r["bills_with_phone"]),
        )
        for r in rows
    }


def _targets(values: dict[str, Any]) -> SalesTargets:
    return SalesTargets(
        net_per_day_paise=float(values["sales.target.net_per_day_paise"]),
        amber_net_per_day_paise=float(values["sales.amber.net_per_day_paise"]),
        orders_per_day=float(values["sales.target.orders_per_day"]),
        aov_paise=float(values["sales.target.aov_paise"]),
        monwed_share=float(values["sales.target.monwed_share"]),
        phone_capture=float(values["sales.target.phone_capture"]),
        max_discount_share=float(values["sales.max.discount_share"]),
        w_net=float(values["sales.weight.net"]),
        w_orders=float(values["sales.weight.orders"]),
        w_monwed=float(values["sales.weight.monwed"]),
        w_phone=float(values["sales.weight.phone"]),
        w_aov=float(values["sales.weight.aov"]),
        w_discount=float(values["sales.weight.discount"]),
        green=float(values["scoring.band.green"]),
        amber=float(values["scoring.band.amber"]),
    )


async def sales_targets_many(
    db: AsyncSession, *, outlet_ids: list[uuid.UUID], at: datetime
) -> dict[uuid.UUID, SalesTargets]:
    """Targets in force at the period's END, per outlet — re-opening last
    month scores against last month's targets (D9), same as the SOP weights."""
    per_outlet = await resolve_many_outlets(db, _TARGET_KEYS, outlet_ids=outlet_ids, at=at)
    return {outlet: _targets(values) for outlet, values in per_outlet.items()}
