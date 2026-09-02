"""The nightly consumption-and-anomalies pass.

Runs after the 05:00 materialisation, when yesterday's trading day is closed.
Two phases, both idempotent:

1. **Windows**: every pair of consecutive confirmed counts per outlet becomes
   stock_consumption rows (one per item), upserted — everything derived,
   freely recomputable.
2. **Anomalies**: the three section-6 checks over that history. A finding
   lands on the EXISTING exception board (sop_exceptions), because a manager
   already works that queue and a second inbox is where flags go to die.
   Deduplicated: an anomaly is raised only when no open exception with the
   same title exists for the outlet — the same ongoing problem must not
   arrive again every morning.

Thin orchestration; the arithmetic lives in consumption.py under unit tests.
"""

import json
import logging
import uuid
from datetime import date as date_type
from itertools import pairwise
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.business_date import business_date as to_business_date
from app.core.business_date import outlet_now
from app.core.settings_value import resolve_many
from app.domains.inventory import consumption

logger = logging.getLogger(__name__)

_THRESHOLD_KEYS = [
    "anomaly.unchanged_streak",
    "anomaly.padding_min_lines",
    "anomaly.padding_share",
    "anomaly.min_windows",
    "anomaly.z_threshold",
]


async def run(db: AsyncSession) -> dict[str, Any]:
    outlets = [
        uuid.UUID(str(r[0]))
        for r in await db.execute(
            text("select id from outlets where is_active and deleted_at is null")
        )
    ]
    totals = {"outlets": len(outlets), "windows": 0, "raised": [], "skipped_open": 0}
    for outlet_id in outlets:
        result = await _run_outlet(db, outlet_id)
        totals["windows"] += result["windows"]
        totals["raised"] += result["raised"]
        totals["skipped_open"] += result["skipped_open"]
    await db.commit()
    return totals


async def _run_outlet(db: AsyncSession, outlet_id: uuid.UUID) -> dict[str, Any]:
    thresholds = await resolve_many(db, _THRESHOLD_KEYS, outlet_id=outlet_id)

    # Every (item, confirmed count) quantity, oldest first. qty null means the
    # item was on the sheet but not counted — those rows do not make windows.
    rows = (
        (
            await db.execute(
                text(
                    """
                    select l.item_id, i.name as item_name, c.id as count_id,
                           c.business_date, cast(l.qty as float8) as qty
                      from stock_count_lines l
                      join stock_counts c on c.id = l.count_id
                      join inventory_items i on i.id = l.item_id
                     where c.outlet_id = :o and c.status = 'confirmed'
                       and l.item_id is not null and l.qty is not null
                     order by c.business_date
                    """
                ),
                {"o": outlet_id},
            )
        )
        .mappings()
        .all()
    )
    by_item: dict[str, dict[str, Any]] = {}
    for r in rows:
        entry = by_item.setdefault(str(r["item_id"]), {"name": r["item_name"], "points": []})
        entry["points"].append(
            consumption.CountPoint(
                count_id=str(r["count_id"]),
                business_date=str(r["business_date"]),
                qty=r["qty"],
            )
        )

    # Finalised requisition quantities per item, dated by business_date — the
    # receipts stand-in, summed into whichever window the date falls in.
    req_rows = (
        (
            await db.execute(
                text(
                    """
                    select rl.item_id, r.business_date,
                           cast(rl.final_qty as float8) as final_qty,
                           'padding' = any(rl.flags) as padded
                      from requisition_lines rl
                      join requisitions r on r.id = rl.requisition_id
                     where r.outlet_id = :o and r.status = 'final'
                       and rl.final_qty is not null
                     order by r.business_date
                    """
                ),
                {"o": outlet_id},
            )
        )
        .mappings()
        .all()
    )

    # Theoretical consumption inputs (P17): what the recipes say each window
    # SHOULD have used, from item-day sales. Three prefetches, then pure
    # lookups inside the loop. Dates stay ISO strings — lexicographic order
    # is date order, and this module does no date maths.
    sales_days = {
        str(r[0])
        for r in await db.execute(
            text("select distinct report_date from sales_item_days where outlet_id = :o"),
            {"o": outlet_id},
        )
    }
    recipe_items = {
        str(r[0])
        for r in await db.execute(
            text(
                "select distinct rl.item_id from recipe_lines rl"
                " join recipes r on r.id = rl.recipe_id where r.is_active"
            )
        )
    }
    theo_by_item_day: dict[tuple[str, str], float] = {
        (str(r[0]), str(r[1])): float(r[2])
        for r in await db.execute(
            text(
                """
                select rl.item_id, sid.report_date,
                       sum(sid.qty * rl.qty_per_unit) as qty
                  from sales_item_days sid
                  join recipes r on r.menu_item_name = sid.item_name and r.is_active
                  join recipe_lines rl on rl.recipe_id = r.id
                 where sid.outlet_id = :o
                 group by rl.item_id, sid.report_date
                """
            ),
            {"o": outlet_id},
        )
    }

    def theoretical_for(item_id: str, from_date: str, to_date: str) -> float | None:
        """Sum of recipe-implied usage across the window, or None when it
        cannot honestly be computed: no item-day sales cover the window, or
        no active recipe mentions the item."""
        if item_id not in recipe_items:
            return None
        covered = [d for d in sales_days if from_date < d <= to_date]
        if not covered:
            return None
        return sum(theo_by_item_day.get((item_id, d), 0.0) for d in covered)

    windows_written = 0
    raised: list[str] = []
    skipped_open = 0

    for item_id, entry in by_item.items():
        points: list[consumption.CountPoint] = entry["points"]
        if len(points) < 2:
            continue

        ordered = sorted(points, key=lambda p: p.business_date)
        req_map: dict[tuple[str, str], float] = {}
        for prev, curr in pairwise(ordered):
            inside = [
                float(rr["final_qty"])
                for rr in req_rows
                if str(rr["item_id"]) == item_id
                and prev.business_date < str(rr["business_date"]) <= curr.business_date
            ]
            if inside:
                req_map[(prev.count_id, curr.count_id)] = sum(inside)

        item_windows = consumption.windows(ordered, req_map)
        per_cover_series: list[float] = []
        latest_theoretical: float | None = None
        for window in item_windows:
            theoretical = theoretical_for(item_id, window.from_date, window.to_date)
            latest_theoretical = theoretical
            covers = (
                await db.execute(
                    text(
                        """
                        select coalesce(sum(covers), 0) from sales_orders
                         where outlet_id = :o
                           and business_date > cast(:a as date)
                           and business_date <= cast(:b as date)
                        """
                    ),
                    {
                        "o": outlet_id,
                        "a": date_type.fromisoformat(window.from_date),
                        "b": date_type.fromisoformat(window.to_date),
                    },
                )
            ).scalar_one()
            covers = int(covers)
            await db.execute(
                text(
                    """
                    insert into stock_consumption
                        (outlet_id, item_id, from_count_id, to_count_id,
                         from_date, to_date, days_between, from_qty, to_qty,
                         requisitioned_qty, apparent_consumption, theoretical_qty,
                         covers, detail)
                    values (:o, :item, :fc, :tc, cast(:fd as date),
                            cast(:td as date),
                            cast(:td as date) - cast(:fd as date),
                            :fq, :tq, :req, :apparent, :theoretical, :covers,
                            cast(:detail as jsonb))
                    on conflict (to_count_id, item_id) do update set
                        requisitioned_qty = excluded.requisitioned_qty,
                        apparent_consumption = excluded.apparent_consumption,
                        theoretical_qty = excluded.theoretical_qty,
                        covers = excluded.covers,
                        detail = excluded.detail
                    """
                ),
                {
                    "o": outlet_id,
                    "item": item_id,
                    "fc": window.from_count_id,
                    "tc": window.to_count_id,
                    "fd": date_type.fromisoformat(window.from_date),
                    "td": date_type.fromisoformat(window.to_date),
                    "fq": window.from_qty,
                    "tq": window.to_qty,
                    "req": window.requisitioned_qty,
                    "apparent": window.apparent_consumption,
                    "theoretical": theoretical,
                    "covers": covers,
                    "detail": json.dumps(window.detail),
                },
            )
            windows_written += 1
            if window.apparent_consumption is not None and covers > 0:
                per_cover_series.append(window.apparent_consumption / covers)

        findings = [
            consumption.unchanged_count(
                ordered,
                item_name=entry["name"],
                streak=int(thresholds["anomaly.unchanged_streak"]),
            ),
            consumption.consumption_jump(
                per_cover_series,
                item_name=entry["name"],
                min_windows=int(thresholds["anomaly.min_windows"]),
                z_threshold=float(thresholds["anomaly.z_threshold"]),
            ),
        ]
        if item_windows:
            findings.append(
                consumption.zero_sales_consumption(
                    item_name=entry["name"],
                    apparent=item_windows[-1].apparent_consumption,
                    theoretical=latest_theoretical,
                )
            )
        item_reqs = [rr for rr in req_rows if str(rr["item_id"]) == item_id]
        findings.append(
            consumption.padding_consistent(
                item_name=entry["name"],
                flagged=sum(1 for rr in item_reqs if rr["padded"]),
                total=len(item_reqs),
                min_lines=int(thresholds["anomaly.padding_min_lines"]),
                share_threshold=float(thresholds["anomaly.padding_share"]),
            )
        )

        for finding in findings:
            if finding is None:
                continue
            already_open = (
                await db.execute(
                    text(
                        """
                        select count(*) from sop_exceptions
                         where outlet_id = :o and title = :t
                           and status in ('open', 'acknowledged')
                        """
                    ),
                    {"o": outlet_id, "t": finding.title},
                )
            ).scalar_one()
            if already_open:
                skipped_open += 1
                continue
            await db.execute(
                text(
                    """
                    insert into sop_exceptions
                        (outlet_id, business_date, severity, title, detail)
                    values (:o, :d, cast(:sev as severity), :t, :detail)
                    """
                ),
                {
                    "o": outlet_id,
                    "d": to_business_date(outlet_now()),
                    "sev": finding.severity,
                    "t": finding.title,
                    "detail": json.dumps(finding.detail),
                },
            )
            raised.append(finding.title)

    return {"windows": windows_written, "raised": raised, "skipped_open": skipped_open}
