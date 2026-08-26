"""Run scoring. Pure functions, no I/O, table-driven tests.

Spec section 4.3:

    item_weight       = 3 if is_critical else 1
    applicable_weight = Σ item_weight where result != 'na'
    earned_weight     = Σ item_weight where result == 'pass'
    run.score_pct     = 100 x earned_weight / applicable_weight

The critical weight is admin-editable (scoring.critical_item_weight); callers
resolve it from settings and pass it in, so this module stays pure.
"""

from dataclasses import dataclass

from app.core.enums import ItemResult

DEFAULT_CRITICAL_WEIGHT = 3


@dataclass(frozen=True)
class ScorableItem:
    result: ItemResult
    is_critical: bool


def item_weight(is_critical: bool, critical_weight: int = DEFAULT_CRITICAL_WEIGHT) -> int:
    return critical_weight if is_critical else 1


def run_score(
    items: list[ScorableItem], critical_weight: int = DEFAULT_CRITICAL_WEIGHT
) -> float | None:
    """Percentage of applicable weight earned, or None when nothing applies.

    None, not 0: a run where every item was legitimately n/a did not fail —
    it had nothing to assess. Scoring it 0 would poison the outlet's mean;
    scoring it 100 would reward doing nothing. Callers skip None runs.
    """
    applicable = sum(
        item_weight(i.is_critical, critical_weight) for i in items if i.result is not ItemResult.NA
    )
    if applicable == 0:
        return None
    earned = sum(
        item_weight(i.is_critical, critical_weight) for i in items if i.result is ItemResult.PASS
    )
    return round(100.0 * earned / applicable, 2)


def critical_fail_count(items: list[ScorableItem]) -> int:
    return sum(1 for i in items if i.is_critical and i.result is ItemResult.FAIL)


def out_of_range(value: float, minimum: float | None, maximum: float | None) -> bool:
    """Bounds are inclusive: a broth at exactly 75°C when the band is 75-95
    is in range. An item with no bounds can never be out of range."""
    if minimum is not None and value < minimum:
        return True
    return maximum is not None and value > maximum


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres. Used for the geofence check."""
    from math import atan2, cos, radians, sin, sqrt

    earth_radius_m = 6_371_000.0
    p1, p2 = radians(lat1), radians(lat2)
    dp = radians(lat2 - lat1)
    dl = radians(lng2 - lng1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return earth_radius_m * 2 * atan2(sqrt(a), sqrt(1 - a))
