"""The Inventory discipline pillar (spec section 5). Pure, working shown.

The spec names four components. Two are computable from what P11-P13 built
and two are not, and the pillar says so out loud rather than padding:

- **Requisition accuracy** (live) — the share of finalised requisition lines
  NOT flagged `padding`. The flag was computed at requisition time against
  the par gap (asked > 1.3x need); this only reads it back. "Requested vs
  consumed" in the spec's words, measured by the check that already runs.
- **Stockout incidents** (live) — items counted at ZERO on confirmed counts,
  normalised per 28 days so a 7-day view and a quarter read on one scale.
  The unchanged-count anomaly deliberately exempts zero ("a purchasing
  problem, not a counting one") — this is where that problem lands.
- **Theoretical vs actual variance** (live since P17, where measurable) —
  the median absolute gap between what the counts say a window used and
  what the recipes say its sales should have used. Only windows where both
  sides are computable participate; none in the period and the component
  stays pending, not zero.
- **Wastage % of COGS** (pending) — needs a wastage log, which does not
  exist yet.

A period with no confirmed counts AND no finalised requisitions scores None:
the discipline has not been measured, which is not the same as it failing.
The pillar arms itself the day the first count is confirmed — the same way
the anomaly job did.
"""

from dataclasses import dataclass

from app.core.pillar_math import (
    Component,
    Pillar,
    band_for_target,
    bigger_better,
    pending,
    smaller_better,
    weighted,
)


@dataclass(frozen=True)
class InventoryInputs:
    """Aggregates for one outlet over one period."""

    period_days: int
    #: Finalised requisition lines in the period, and how many carry the
    #: padding flag.
    req_lines: int
    padded_lines: int
    #: Item-lines counted at zero on confirmed counts in the period, and the
    #: total counted lines they sit among.
    counted_lines: int
    stockout_lines: int
    #: Windows in the period where both apparent and theoretical consumption
    #: were computable, and the median absolute variance across them.
    variance_windows: int = 0
    median_abs_variance: float | None = None


@dataclass(frozen=True)
class InventoryTargets:
    clean_req_share: float
    max_stockouts_28d: float
    max_variance: float
    w_requisition: float
    w_stockouts: float
    w_variance: float
    green: float
    amber: float


def inventory_pillar(inputs: InventoryInputs, targets: InventoryTargets) -> Pillar:
    components: list[Component] = []

    if inputs.req_lines > 0:
        clean = (inputs.req_lines - inputs.padded_lines) / inputs.req_lines
        score = bigger_better(clean, targets.clean_req_share)
        components.append(
            Component(
                key="requisition_accuracy",
                label="Requisition accuracy",
                value=round(clean, 4),
                display=f"{100 * clean:.0f}% of lines within need",
                target_display=f"green ≥ {100 * targets.clean_req_share:.0f}%",
                score=score,
                weight=targets.w_requisition,
                contribution=round(score * targets.w_requisition, 1),
                band=band_for_target(clean, targets.clean_req_share),
            )
        )
    else:
        components.append(
            pending(
                "requisition_accuracy",
                "Requisition accuracy",
                "no finalised requisitions in the period",
            )
        )

    if inputs.counted_lines > 0:
        # Normalised to a 28-day rate so the target means one thing whatever
        # the period length.
        rate = inputs.stockout_lines * 28.0 / max(inputs.period_days, 1)
        score = smaller_better(rate, targets.max_stockouts_28d)
        components.append(
            Component(
                key="stockouts",
                label="Stockout incidents",
                value=round(rate, 2),
                display=f"{inputs.stockout_lines} at zero ({rate:.1f}/28d)",
                target_display=f"keep ≤ {targets.max_stockouts_28d:g}/28d",
                score=score,
                weight=targets.w_stockouts,
                contribution=round(score * targets.w_stockouts, 1),
                band="green" if rate <= targets.max_stockouts_28d else "red",
            )
        )
    else:
        components.append(
            pending("stockouts", "Stockout incidents", "no confirmed counts in the period")
        )

    if inputs.variance_windows > 0 and inputs.median_abs_variance is not None:
        med = inputs.median_abs_variance
        score = smaller_better(med, targets.max_variance)
        band = (
            "green"
            if med <= targets.max_variance
            else "amber"
            if med <= 2 * targets.max_variance
            else "red"
        )
        components.append(
            Component(
                key="theoretical_variance",
                label="Theoretical vs actual variance",
                value=round(med, 4),
                display=(
                    f"±{100 * med:.0f}% median gap over {inputs.variance_windows} window"
                    f"{'s' if inputs.variance_windows != 1 else ''}"
                ),
                target_display=f"keep ≤ {100 * targets.max_variance:.0f}%",
                score=score,
                weight=targets.w_variance,
                contribution=round(score * targets.w_variance, 1),
                band=band,
            )
        )
    else:
        components.append(
            pending(
                "theoretical_variance",
                "Theoretical vs actual variance",
                "no windows where both counted usage and recipe-implied usage were computable",
            )
        )
    components.append(pending("wastage", "Wastage % of COGS", "needs a wastage log"))

    return weighted(
        components,
        green=targets.green,
        amber=targets.amber,
        detail={
            "period_days": inputs.period_days,
            "req_lines": inputs.req_lines,
            "counted_lines": inputs.counted_lines,
        },
    )
