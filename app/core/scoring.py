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


# ---------------------------------------------------------------------------
# Outlet-level scoring (spec 4.3)
# ---------------------------------------------------------------------------
#
#   outlet SOP score (period) =
#         0.50 x mean(run.score_pct for approved runs)
#       + 0.30 x completion_rate      -- runs approved / runs scheduled
#       + 0.20 x on_time_rate         -- submitted before due+grace / submitted
#       - 2 points per open high-severity exception older than 48h
#       - 1 point per integrity flag per 10 runs
#       (clamped 0-100)
#
# Weights, penalties and bands are all admin-editable, so callers resolve them
# from the settings registry AT THE END OF THE PERIOD BEING SCORED and pass
# them in. That is what keeps a score from three months ago reproducible when
# somebody nudges a weight today (D9). This module stays pure.

GREEN_BAND = 90.0
AMBER_BAND = 75.0


@dataclass(frozen=True)
class OutletCounts:
    """What happened at one outlet over one period. Raw, not yet a score."""

    scheduled: int
    approved: int
    #: Runs that reached submitted or beyond. The denominator for on-time.
    submitted: int
    on_time: int
    missed: int
    #: Mean score_pct across approved runs. None when none were approved.
    mean_run_score: float | None
    integrity_flags: int
    #: Open or acknowledged high-severity exceptions — every one of these is a
    #: critical checklist item that failed and has not been dealt with.
    open_critical: int
    #: The subset of those older than 48 hours, which is what the spec penalises.
    stale_critical: int


@dataclass(frozen=True)
class ScoreWeights:
    run_score: float = 0.50
    completion_rate: float = 0.30
    on_time_rate: float = 0.20
    stale_exception_penalty: float = 2.0
    integrity_flag_penalty: float = 1.0
    green: float = GREEN_BAND
    amber: float = AMBER_BAND


@dataclass(frozen=True)
class Component:
    key: str
    label: str
    #: 0-100, or None when the period gave it no denominator.
    value: float | None
    weight: float

    @property
    def contribution(self) -> float:
        """A component with no denominator contributes nothing.

        Not the same as excluding it and re-weighting the rest: an outlet that
        approved no runs has genuinely earned no run-score credit, and
        re-weighting would hand it a full marks for a term it never sat.
        """
        return 0.0 if self.value is None else round(self.weight * self.value, 2)


@dataclass(frozen=True)
class Penalty:
    key: str
    label: str
    points: float
    detail: str


@dataclass(frozen=True)
class OutletScore:
    #: None when nothing was scheduled. A closed outlet has no score; it did
    #: not score zero.
    score: float | None
    band: str
    components: list[Component]
    penalties: list[Penalty]
    #: True when an unresolved critical failure held the band at amber despite
    #: the arithmetic saying green.
    capped_by_critical: bool
    counts: OutletCounts

    @property
    def worst_component(self) -> Component | None:
        """The single component to name on the card.

        The spec asks for "dragged down by: on-time rate 62%". Picking the
        lowest *value* rather than the lowest contribution is deliberate — a
        heavily weighted component that is nearly perfect is not what a manager
        should go and fix.
        """
        scored = [c for c in self.components if c.value is not None]
        return min(scored, key=lambda c: c.value or 0.0) if scored else None


def band_for(score: float | None, weights: ScoreWeights = ScoreWeights()) -> str:
    if score is None:
        return "none"
    if score >= weights.green:
        return "green"
    return "amber" if score >= weights.amber else "red"


def rate(numerator: int, denominator: int) -> float | None:
    """A percentage, or None when there is nothing to divide by."""
    return None if denominator == 0 else round(100.0 * numerator / denominator, 1)


def outlet_score(counts: OutletCounts, weights: ScoreWeights = ScoreWeights()) -> OutletScore:
    """The outlet's SOP compliance score for a period."""
    components = [
        Component("run_score", "Mean approved run score", counts.mean_run_score, weights.run_score),
        Component(
            "completion_rate",
            "Completion rate",
            rate(counts.approved, counts.scheduled),
            weights.completion_rate,
        ),
        Component(
            "on_time_rate",
            "On-time rate",
            rate(counts.on_time, counts.submitted),
            weights.on_time_rate,
        ),
    ]

    penalties: list[Penalty] = []
    if counts.stale_critical:
        points = weights.stale_exception_penalty * counts.stale_critical
        penalties.append(
            Penalty(
                "stale_exceptions",
                "Unresolved critical failures over 48h",
                round(points, 2),
                f"{counts.stale_critical} open more than 48 hours",
            )
        )
    if counts.integrity_flags and counts.scheduled:
        # "1 point per integrity flag per 10 runs" — a rate, so that an outlet
        # running twice as many checklists is not punished twice as hard for
        # the same standard of honesty.
        per_ten = 10.0 * counts.integrity_flags / counts.scheduled
        points = weights.integrity_flag_penalty * per_ten
        penalties.append(
            Penalty(
                "integrity_flags",
                "Integrity flags",
                round(points, 2),
                f"{counts.integrity_flags} across {counts.scheduled} runs",
            )
        )

    if counts.scheduled == 0:
        return OutletScore(None, "none", components, penalties, False, counts)

    raw = sum(c.contribution for c in components) - sum(p.points for p in penalties)
    score = round(max(0.0, min(100.0, raw)), 1)

    arithmetic_band = band_for(score, weights)
    # "A single unresolved critical failure caps the outlet at amber regardless
    # of arithmetic." The number stays honest; the band is what gets held back,
    # so the card can say why rather than quietly showing a lower score.
    capped = counts.open_critical > 0 and arithmetic_band == "green"
    return OutletScore(
        score,
        "amber" if capped else arithmetic_band,
        components,
        penalties,
        capped,
        counts,
    )
