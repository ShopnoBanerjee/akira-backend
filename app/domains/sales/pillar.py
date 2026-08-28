"""The Sales & growth pillar (spec section 5). Pure arithmetic, working shown.

Six components, each normalised 0-100 against its effective-dated target and
weighted into one pillar score. The normalisation is the simplest thing that
is honest:

    bigger-is-better:  score = min(100, 100 x value / target)
    smaller-is-better: score = 100 at or under the ceiling, then
                       min(100, 100 x ceiling / value)

Nothing cleverer, on purpose. A sigmoid would score "nicer" and nobody could
check it on their fingers; this one a manager can. Every component carries its
value, target, and contribution so the card can show the working, exactly like
the SOP score and the requisition formula before it.

Bands are separate from scores, same as D14's cap logic: the component's
band is where its value sits against the target (and, for net sales, the
spec's explicit amber floor); the pillar's band is the weighted score run
through the same green/amber thresholds the SOP pillar uses, so the two live
pillars read on one scale.

A period with no trading days scores None, not zero — a shut fortnight is not
a failed fortnight. Same principle as run scores, applied one level up.
"""

from dataclasses import dataclass, field
from typing import Any

#: The pillar reuses the SOP score's band thresholds (settings
#: scoring.band.green / amber) so "green" means the same thing on both live
#: pillars. Resolved by the caller and passed in.


@dataclass(frozen=True)
class SalesInputs:
    """Aggregates for one outlet over one period, from sales_orders."""

    trading_days: int
    bills: int
    net_paise: int
    gross_paise: int
    discount_paise: int
    monwed_net_paise: int
    bills_with_phone: int


@dataclass(frozen=True)
class SalesTargets:
    net_per_day_paise: float
    amber_net_per_day_paise: float
    orders_per_day: float
    aov_paise: float
    monwed_share: float
    phone_capture: float
    max_discount_share: float
    # component weights, expected to sum to 1
    w_net: float
    w_orders: float
    w_monwed: float
    w_phone: float
    w_aov: float
    w_discount: float
    # band thresholds shared with the SOP pillar
    green: float
    amber: float


@dataclass(frozen=True)
class SalesComponent:
    key: str
    label: str
    value: float | None
    display: str
    target_display: str
    score: float | None
    weight: float
    contribution: float
    band: str  # green | amber | red | none


@dataclass(frozen=True)
class SalesPillar:
    score: float | None
    band: str
    components: list[SalesComponent] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def worst_component(self) -> SalesComponent | None:
        scored = [c for c in self.components if c.score is not None and c.weight > 0]
        return min(scored, key=lambda c: c.score) if scored else None  # type: ignore[arg-type, return-value]


def _bigger_better(value: float, target: float) -> float:
    if target <= 0:
        return 100.0
    return round(min(100.0, 100.0 * value / target), 1)


def _smaller_better(value: float, ceiling: float) -> float:
    if value <= ceiling:
        return 100.0
    if value <= 0:
        return 100.0
    return round(min(100.0, 100.0 * ceiling / value), 1)


def _band_for_score(score: float, targets: SalesTargets) -> str:
    if score >= targets.green:
        return "green"
    if score >= targets.amber:
        return "amber"
    return "red"


def rupees(paise: float) -> str:
    """Indian digit grouping for the display strings only — the frontend has
    its own formatter; this feeds the digest text."""
    whole = round(paise / 100)
    s = f"{whole:,}"
    # Convert western grouping to Indian (last three, then twos).
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join([*parts, tail])
    return f"₹{s}"


def sales_pillar(inputs: SalesInputs, targets: SalesTargets) -> SalesPillar:
    if inputs.trading_days == 0 or inputs.bills == 0:
        # Nothing traded, nothing to score. None, not zero: a shut period has
        # not failed, it has not been measured.
        return SalesPillar(score=None, band="none", components=[], detail={"reason": "no_trading"})

    net_per_day = inputs.net_paise / inputs.trading_days
    orders_per_day = inputs.bills / inputs.trading_days
    aov = inputs.net_paise / inputs.bills
    monwed = inputs.monwed_net_paise / inputs.net_paise if inputs.net_paise else 0.0
    phone = inputs.bills_with_phone / inputs.bills
    discount = inputs.discount_paise / inputs.gross_paise if inputs.gross_paise else 0.0

    def band_target(value: float, target: float, red_floor: float | None = None) -> str:
        if value >= target:
            return "green"
        if red_floor is not None:
            return "amber" if value >= red_floor else "red"
        return "amber" if value >= 0.75 * target else "red"

    raw: list[tuple[str, str, float, str, str, float, float, str]] = [
        (
            "net_per_day",
            "Net sales / trading day",
            net_per_day,
            f"{rupees(net_per_day)}/day",
            f"green ≥ {rupees(targets.net_per_day_paise)}",
            _bigger_better(net_per_day, targets.net_per_day_paise),
            targets.w_net,
            band_target(net_per_day, targets.net_per_day_paise, targets.amber_net_per_day_paise),
        ),
        (
            "orders_per_day",
            "Orders / trading day",
            orders_per_day,
            f"{orders_per_day:.1f}/day",
            f"green ≥ {targets.orders_per_day:g}",
            _bigger_better(orders_per_day, targets.orders_per_day),
            targets.w_orders,
            band_target(orders_per_day, targets.orders_per_day),
        ),
        (
            "monwed_share",
            "Mon-Wed share of sales",
            monwed,
            f"{100 * monwed:.0f}%",
            f"green ≥ {100 * targets.monwed_share:.0f}%",
            _bigger_better(monwed, targets.monwed_share),
            targets.w_monwed,
            band_target(monwed, targets.monwed_share),
        ),
        (
            "phone_capture",
            "Phone capture rate",
            phone,
            f"{100 * phone:.0f}%",
            f"green ≥ {100 * targets.phone_capture:.0f}%",
            _bigger_better(phone, targets.phone_capture),
            targets.w_phone,
            band_target(phone, targets.phone_capture),
        ),
        (
            "aov",
            "Average order value",
            aov,
            rupees(aov),
            f"green ≥ {rupees(targets.aov_paise)}",
            _bigger_better(aov, targets.aov_paise),
            targets.w_aov,
            band_target(aov, targets.aov_paise),
        ),
        (
            "discount_control",
            "Discount % of gross",
            discount,
            f"{100 * discount:.1f}%",
            f"keep < {100 * targets.max_discount_share:.0f}%",
            _smaller_better(discount, targets.max_discount_share),
            targets.w_discount,
            "green" if discount <= targets.max_discount_share else "red",
        ),
    ]

    components = [
        SalesComponent(
            key=key,
            label=label,
            value=round(value, 4),
            display=display,
            target_display=target_display,
            score=score,
            weight=weight,
            contribution=round(score * weight, 1),
            band=band,
        )
        for key, label, value, display, target_display, score, weight, band in raw
    ]
    total = round(sum(c.contribution for c in components), 1)
    return SalesPillar(
        score=total,
        band=_band_for_score(total, targets),
        components=components,
        detail={
            "formula": "sum(component score x weight); scores are min(100, 100 x value/target)",
            "trading_days": inputs.trading_days,
            "bills": inputs.bills,
        },
    )
