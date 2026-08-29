"""The blended outlet health score. D14 retires here (P15).

D14 refused to blend while only one pillar existed: one live number times
0.30 reads as a catastrophe or, silently rescaled, changes the day a second
pillar lands. With all four pillars now producing (two in full, two with
their gaps declared), the blend is real:

    health = sum(pillar score x weight) / sum(weight), over MEASURED pillars

Renormalised over what was measured, not padded with zeroes: an outlet whose
inventory pillar reads "not measured" (no confirmed counts yet) is scored on
the pillars that were — and the response says which. The day the first count
is confirmed, the denominator grows and the number's meaning is visible in
`weights_used`, not smuggled.

Pure arithmetic; the router feeds it. Weights are the spec's 30/30/25/15,
fixed in the dashboard router — they become registry settings the day
somebody actually needs to tune them, and not before.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PillarReading:
    key: str
    label: str
    weight: float
    score: float | None  # None = not measured this period


@dataclass(frozen=True)
class BlendedHealth:
    score: float | None
    band: str
    #: Sum of the weights of the pillars that were measured, out of the total.
    #: 100 means every pillar contributed; less names the honesty gap.
    weights_used: float
    weights_total: float
    measured: list[str] = field(default_factory=list)
    unmeasured: list[str] = field(default_factory=list)


def blended_health(pillars: list[PillarReading], *, green: float, amber: float) -> BlendedHealth:
    measured = [p for p in pillars if p.score is not None]
    unmeasured = [p for p in pillars if p.score is None]
    total = sum(p.weight for p in pillars)
    used = sum(p.weight for p in measured)
    if used == 0:
        return BlendedHealth(
            score=None,
            band="none",
            weights_used=0,
            weights_total=total,
            measured=[],
            unmeasured=[p.key for p in unmeasured],
        )
    score = round(sum((p.score or 0) * p.weight for p in measured) / used, 1)
    band = "green" if score >= green else "amber" if score >= amber else "red"
    return BlendedHealth(
        score=score,
        band=band,
        weights_used=used,
        weights_total=total,
        measured=[p.key for p in measured],
        unmeasured=[p.key for p in unmeasured],
    )
