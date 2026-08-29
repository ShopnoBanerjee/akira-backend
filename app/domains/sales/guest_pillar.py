"""The Guest & throughput pillar (spec section 5). Pure, working shown.

The spec names four components; one is computable today, one is a declared
monitor, two are honestly pending:

- **Repeat-customer rate** (live) — of the customers identified by a phone
  hash in the period, the share seen on two or more trading days. This is
  the OUTCOME that phone capture exists to enable; the capture rate itself
  stays in the sales pillar where D19 put it (a till habit beside the other
  till habits), so the same metric is never counted twice. Live baseline at
  build time: 9% of 134 identified customers.
- **Peak-hour share** (monitor) — revenue struck 20:00-23:00 as a share of
  the total. The spec's own baseline table says "monitor for truncation, not
  a target", so it carries no weight and no score, just the number.
- **Peak-hour table turns** (pending) — Petpooja's exports carry no table
  numbers (the Area column is empty on all 1,533 real dine-in bills), so
  turns cannot be computed until the till starts recording tables.
- **Google rating delta** (pending) — no ratings source is connected.

Identification is by salted hash, so "repeat" means "same phone seen again";
customers who never give a phone are invisible here. That bias is stated on
the component rather than corrected by guesswork — the phone-capture
component in the sales pillar is what shrinks it.
"""

from dataclasses import dataclass

from app.core.pillar_math import (
    Component,
    Pillar,
    band_for_target,
    bigger_better,
    monitor,
    pending,
    weighted,
)


@dataclass(frozen=True)
class GuestInputs:
    """Aggregates for one outlet over one period, from sales_orders."""

    bills: int
    #: Distinct phone-hash customers in the period, and how many of them were
    #: seen on 2+ distinct trading days.
    identified_customers: int
    repeat_customers: int
    #: Revenue struck 20:00-23:00 outlet time, and the period total.
    peak_net_paise: int
    net_paise: int


@dataclass(frozen=True)
class GuestTargets:
    repeat_rate: float
    w_repeat: float
    green: float
    amber: float


def guest_pillar(inputs: GuestInputs, targets: GuestTargets) -> Pillar:
    components: list[Component] = []

    if inputs.identified_customers > 0:
        rate = inputs.repeat_customers / inputs.identified_customers
        score = bigger_better(rate, targets.repeat_rate)
        components.append(
            Component(
                key="repeat_rate",
                label="Repeat-customer rate",
                value=round(rate, 4),
                display=(
                    f"{100 * rate:.0f}% of {inputs.identified_customers} identified customers"
                ),
                target_display=f"green ≥ {100 * targets.repeat_rate:.0f}%",
                score=score,
                weight=targets.w_repeat,
                contribution=round(score * targets.w_repeat, 1),
                band=band_for_target(rate, targets.repeat_rate),
            )
        )
    else:
        components.append(
            pending(
                "repeat_rate",
                "Repeat-customer rate",
                "no customers identified by phone in the period",
            )
        )

    if inputs.net_paise > 0:
        share = inputs.peak_net_paise / inputs.net_paise
        components.append(
            monitor(
                "peak_share",
                "Peak-hour share (20:00-23:00)",
                share,
                f"{100 * share:.0f}% of revenue",
                "watched for truncation, not scored — the spec sets no target",
            )
        )

    components.append(
        pending(
            "peak_turns",
            "Peak-hour table turns",
            "Petpooja exports carry no table numbers",
        )
    )
    components.append(pending("google_rating", "Google rating delta", "no ratings source yet"))

    return weighted(
        components,
        green=targets.green,
        amber=targets.amber,
        detail={
            "bills": inputs.bills,
            "identified_customers": inputs.identified_customers,
        },
    )
