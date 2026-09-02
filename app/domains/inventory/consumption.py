"""Consumption windows and the anomaly arithmetic. Pure, working attached.

The spec's section-6 rule holds here with no model in sight — these are the
"Statistics" rows of its ownership table. Three checks, each the simplest
version that cannot lie:

- **unchanged count**: the same on-hand quantity across N consecutive counts
  is a count nobody performed. A streak, not a statistic.
- **consistent padding**: the share of a trailing window's requisition lines
  already flagged `padding` (requested > 1.3x the par gap, computed at
  requisition time). A share of existing flags, not a re-judgement.
- **consumption-per-cover jump**: z-score of the latest window's
  consumption-per-cover against that item's own trailing distribution.
  |z| > 2.5 flags; fewer than MIN_WINDOWS samples flags nothing, because a
  z-score against four points is a coin toss wearing a Greek letter.
- **consumption with zero sales** (P17): the latest window used stock while
  the recipes say its dishes sold nothing. Staff meals or wastage nobody
  logged. Fires only when theoretical consumption is COMPUTABLE for the
  window — no sales data is never treated as zero sales.

Every result carries its inputs. An anomaly a manager cannot check is one
they learn to ignore — the same argument as the integrity chips.
"""

from dataclasses import dataclass, field
from itertools import pairwise
from statistics import mean, pstdev
from typing import Any


@dataclass(frozen=True)
class CountPoint:
    """One item's quantity on one confirmed count."""

    count_id: str
    business_date: str  # ISO date; kept as text — this module never does date maths
    qty: float


@dataclass(frozen=True)
class Window:
    from_count_id: str
    to_count_id: str
    from_date: str
    to_date: str
    from_qty: float
    to_qty: float
    requisitioned_qty: float | None
    apparent_consumption: float | None
    detail: dict[str, Any] = field(default_factory=dict)


def windows(
    points: list[CountPoint],
    requisitioned_between: dict[tuple[str, str], float] | None = None,
) -> list[Window]:
    """Consecutive confirmed counts become windows. `requisitioned_between`
    maps (from_count_id, to_count_id) to the finalised requisition quantity
    inside that window; absence means no requisition data, which keeps
    apparent_consumption honestly null."""
    received = requisitioned_between or {}
    out: list[Window] = []
    ordered = sorted(points, key=lambda p: p.business_date)
    for prev, curr in pairwise(ordered):
        req = received.get((prev.count_id, curr.count_id))
        apparent = prev.qty + req - curr.qty if req is not None else None
        out.append(
            Window(
                from_count_id=prev.count_id,
                to_count_id=curr.count_id,
                from_date=prev.business_date,
                to_date=curr.business_date,
                from_qty=prev.qty,
                to_qty=curr.qty,
                requisitioned_qty=req,
                apparent_consumption=apparent,
                detail={
                    "formula": "from_qty + requisitioned_between - to_qty",
                    "assumption": (
                        "finalised requisitions stand in for deliveries until a "
                        "goods-received flow exists"
                    ),
                    "from_qty": prev.qty,
                    "requisitioned": req,
                    "to_qty": curr.qty,
                },
            )
        )
    return out


@dataclass(frozen=True)
class Anomaly:
    kind: str  # 'unchanged_count' | 'padding_consistent' | 'consumption_jump'
    severity: str  # 'low' | 'medium'
    title: str
    detail: dict[str, Any]


def unchanged_count(points: list[CountPoint], *, item_name: str, streak: int) -> Anomaly | None:
    """The same quantity on `streak` consecutive counts. Zero is exempt when
    it repeats — an item that stays out of stock is a purchasing problem, not
    a counting one, and flagging it here would teach people to ignore the
    flag."""
    if len(points) < streak:
        return None
    ordered = sorted(points, key=lambda p: p.business_date)
    tail = ordered[-streak:]
    values = {p.qty for p in tail}
    if len(values) != 1 or tail[0].qty == 0:
        return None
    return Anomaly(
        kind="unchanged_count",
        severity="low",
        title=f"Count never changes: {item_name}",
        detail={
            "qty": tail[0].qty,
            "streak": streak,
            "dates": [p.business_date for p in tail],
            "reading": (
                "the same on-hand figure on consecutive counts usually means "
                "the shelf was not actually counted"
            ),
        },
    )


def padding_consistent(
    *,
    item_name: str,
    flagged: int,
    total: int,
    min_lines: int,
    share_threshold: float,
) -> Anomaly | None:
    """A share of requisition lines already carrying the padding flag. The
    flag itself was computed at requisition time against the par gap; this
    only asks whether it keeps happening."""
    if total < min_lines:
        return None
    share = flagged / total
    if share < share_threshold:
        return None
    return Anomaly(
        kind="padding_consistent",
        severity="medium",
        title=f"Requisitions consistently above need: {item_name}",
        detail={
            "flagged_lines": flagged,
            "total_lines": total,
            "share": round(share, 2),
            "threshold": share_threshold,
            "reading": (
                "asked-for quantities have exceeded the par gap by more than "
                "30% on most recent requisitions"
            ),
        },
    )


def consumption_jump(
    per_cover_series: list[float],
    *,
    item_name: str,
    min_windows: int,
    z_threshold: float,
) -> Anomaly | None:
    """|z| of the LATEST window against the trailing ones. The trailing set
    excludes the point being judged — a jump large enough to drag its own
    baseline would otherwise hide itself."""
    if len(per_cover_series) < min_windows:
        return None
    *trailing, latest = per_cover_series
    mu = mean(trailing)
    sigma = pstdev(trailing)
    if sigma == 0:
        # A perfectly flat history makes any change infinitely surprising by
        # the formula; flag only a real move, with the working shown.
        if latest == mu:
            return None
        z = float("inf")
    else:
        z = (latest - mu) / sigma
    if abs(z) < z_threshold:
        return None
    return Anomaly(
        kind="consumption_jump",
        severity="medium",
        title=f"Consumption per cover jumped: {item_name}",
        detail={
            "latest_per_cover": round(latest, 4),
            "trailing_mean": round(mu, 4),
            "trailing_stdev": round(sigma, 4),
            "z": round(z, 2) if z != float("inf") else "inf",
            "windows": len(per_cover_series),
            "reading": (
                "usage per cover moved sharply against this item's own "
                "history — theft, over-portioning, or a menu change nobody "
                "recorded"
            ),
        },
    )


def zero_sales_consumption(
    *,
    item_name: str,
    apparent: float | None,
    theoretical: float | None,
) -> Anomaly | None:
    """Stock left the shelf while the recipes say nothing that uses it sold.

    `theoretical is None` means it could not be computed — no item-day sales
    covering the window, or no recipe mentions the item — and that is a data
    gap, not an anomaly. Only a computed zero against a real drawdown flags.
    """
    if theoretical is None or apparent is None:
        return None
    if theoretical > 0 or apparent <= 0:
        return None
    return Anomaly(
        kind="zero_sales_consumption",
        severity="medium",
        title=f"Used with no matching sales: {item_name}",
        detail={
            "apparent_consumption": apparent,
            "theoretical": 0,
            "reading": (
                "the counts say this item was used, but no dish whose recipe "
                "includes it sold in the window — staff meals or wastage "
                "nobody logged"
            ),
        },
    )
