"""The arithmetic every health pillar shares. Pure, no domain knowledge.

Extracted when the third and fourth pillars arrived (P15) so the same
normalisation cannot drift between them. The sales pillar (P12) predates this
module and keeps its own identical copy deliberately — its numbers are live
and its digest text is pinned by tests; a refactor that could only change
them by accident buys nothing.

The normalisation is the simplest thing that is honest:

    bigger-is-better:  score = min(100, 100 x value / target)
    smaller-is-better: score = 100 at or under the ceiling, then
                       min(100, 100 x ceiling / value)

A component can also be a **monitor**: a value shown with no weight and no
score, because the spec names it worth watching but names no target (peak-hour
share: "monitor for truncation, not a target"). And it can be **pending**: the
spec promises it but the data to compute it does not exist yet — declared so
the card shows the shape of the finished thing, exactly like the greyed
pillars did before they went live.
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Component:
    key: str
    label: str
    value: float | None
    display: str
    target_display: str
    score: float | None
    weight: float
    contribution: float
    band: str  # green | amber | red | none
    #: live | monitor | pending
    status: str = "live"
    #: For pending components: why there is no number yet.
    note: str | None = None


@dataclass(frozen=True)
class Pillar:
    score: float | None
    band: str
    components: list[Component] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def worst_component(self) -> Component | None:
        scored = [c for c in self.components if c.score is not None and c.weight > 0]
        return min(scored, key=lambda c: c.score) if scored else None  # type: ignore[arg-type, return-value]


def bigger_better(value: float, target: float) -> float:
    if target <= 0:
        return 100.0
    return round(min(100.0, 100.0 * value / target), 1)


def smaller_better(value: float, ceiling: float) -> float:
    if value <= ceiling:
        return 100.0
    if value <= 0:
        return 100.0
    return round(min(100.0, 100.0 * ceiling / value), 1)


def band_for_score(score: float, *, green: float, amber: float) -> str:
    if score >= green:
        return "green"
    if score >= amber:
        return "amber"
    return "red"


def band_for_target(value: float, target: float, *, red_floor: float | None = None) -> str:
    """Where a bigger-is-better value sits against its target. Without an
    explicit red floor, red starts at 75% of target — same convention the
    sales pillar uses."""
    if value >= target:
        return "green"
    floor = red_floor if red_floor is not None else 0.75 * target
    return "amber" if value >= floor else "red"


def pending(key: str, label: str, note: str) -> Component:
    return Component(
        key=key,
        label=label,
        value=None,
        display="—",
        target_display="—",
        score=None,
        weight=0.0,
        contribution=0.0,
        band="none",
        status="pending",
        note=note,
    )


def monitor(key: str, label: str, value: float, display: str, note: str) -> Component:
    return Component(
        key=key,
        label=label,
        value=round(value, 4),
        display=display,
        target_display="monitor",
        score=None,
        weight=0.0,
        contribution=0.0,
        band="none",
        status="monitor",
        note=note,
    )


def weighted(
    components: list[Component],
    *,
    green: float,
    amber: float,
    detail: dict[str, Any] | None = None,
) -> Pillar:
    """The pillar score: contributions renormalised over the weights actually
    present, so two live components carrying 0.6 and 0.4 read on the same
    0-100 scale as six would. Monitors and pendings ride along unscored."""
    live = [c for c in components if c.score is not None and c.weight > 0]
    total_weight = sum(c.weight for c in live)
    if total_weight == 0:
        return Pillar(
            score=None,
            band="none",
            components=components,
            detail={**(detail or {}), "reason": "not_measured"},
        )
    score = round(sum((c.score or 0) * c.weight for c in live) / total_weight, 1)
    return Pillar(
        score=score,
        band=band_for_score(score, green=green, amber=amber),
        components=components,
        detail={
            **(detail or {}),
            "formula": "sum(score x weight) / sum(weight) over live components",
        },
    )
