"""The requisition arithmetic. Pure Python, no model anywhere near it.

    suggested = max(0, par_level - on_hand), rounded UP to order_unit

That is the whole formula for now. The spec's fuller version multiplies a
covers forecast through recipes — that is Stage 3, and pretending otherwise
with six weeks of history would dress a guess up as arithmetic. With par
levels, the gap-to-par is a number a manager can check on their fingers,
which is the standard every number here has to meet.

Each line carries `detail`: the inputs and the steps, rendered under the row
in the UI. A manager who cannot see the working will either trust every
number or none of them, and both are failure modes.

The day-one anomaly from the spec rides along: `padding` flags a chef's
requested quantity more than PADDING_FACTOR above the computed need. It is
advisory — the manager sets final_qty, the flag just says why the row is
worth a look.
"""

import math
from dataclasses import dataclass, field
from typing import Any

#: Spec section 6: "requisition consistently 30%+ above consumption (padding)".
#: Applied per line against the par-gap until consumption history exists.
PADDING_FACTOR = 1.3


@dataclass(frozen=True)
class LineInputs:
    item_id: str
    on_hand: float | None  # None = the cell was blank; nobody counted
    par_level: float | None
    order_unit: float | None
    requested_qty: float | None  # the chef's handwritten ask


@dataclass(frozen=True)
class LineResult:
    item_id: str
    on_hand: float | None
    par_level: float | None
    order_unit: float | None
    suggested_qty: float | None
    requested_qty: float | None
    final_qty: float | None
    flags: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)


def round_up_to(value: float, step: float | None) -> float:
    """2.3 cases from a supplier who sells whole cases is 3 cases."""
    if step is None or step <= 0:
        return value
    return math.ceil(value / step - 1e-9) * step


def compute_line(inputs: LineInputs) -> LineResult:
    flags: list[str] = []
    detail: dict[str, Any] = {}

    suggested: float | None = None
    if inputs.par_level is None:
        # No par, no formula, no number. A suggestion invented from nothing
        # would be the confident nonsense this module exists to prevent.
        flags.append("no_par")
        detail["suggested"] = "no par level configured for this outlet"
    elif inputs.on_hand is None:
        flags.append("not_counted")
        detail["suggested"] = "item was not counted on this sheet"
    else:
        gap = max(0.0, inputs.par_level - inputs.on_hand)
        suggested = round_up_to(gap, inputs.order_unit)
        detail["suggested"] = {
            "formula": "max(0, par - on_hand) rounded up to order_unit",
            "par": inputs.par_level,
            "on_hand": inputs.on_hand,
            "gap": gap,
            "order_unit": inputs.order_unit,
            "result": suggested,
        }

    if (
        suggested is not None
        and inputs.requested_qty is not None
        and inputs.requested_qty > suggested * PADDING_FACTOR
        and suggested >= 0
    ):
        flags.append("padding")
        detail["padding"] = {
            "requested": inputs.requested_qty,
            "suggested": suggested,
            "threshold": f"> {PADDING_FACTOR}x suggested",
        }

    # The manager edits final_qty; it starts as the chef's ask, falling back
    # to the suggestion. Chef first: the person who ran the shift knows about
    # tomorrow's party booking, and the formula does not.
    final = inputs.requested_qty if inputs.requested_qty is not None else suggested

    return LineResult(
        item_id=inputs.item_id,
        on_hand=inputs.on_hand,
        par_level=inputs.par_level,
        order_unit=inputs.order_unit,
        suggested_qty=suggested,
        requested_qty=inputs.requested_qty,
        final_qty=final,
        flags=flags,
        detail=detail,
    )
