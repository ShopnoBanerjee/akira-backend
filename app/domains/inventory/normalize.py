"""Turning handwritten quantities into numbers — or refusing to.

The sheet's Unit column says GM, and the chef writes "500", "1.500", "1kg",
"5pk", "1kg 7pc", a circled zero. Most of these have exactly one sensible
reading; some have none. The contract of this module is that it NEVER guesses:
a parse either succeeds with its reasoning attached, or refuses with the raw
string preserved for a human. A wrong number that looks right is worse than a
blank — it flows into a requisition and someone orders against it.

Every convention encoded here was read off the real 27 Aug 2026 sheet:

- "1.500" under a grams column is 1.5 kg — the dot is the kitchen's
  thousands separator, not a decimal of a gram. Nobody counts half a gram
  of ginger. The same convention makes "2.800" 2800 g.
- "1kg" on a grams row is 1000. Unit suffixes convert when they are the same
  dimension; "5pk" on a grams row does not convert to anything and is refused.
- A circled zero (extracted as "0", "O", or "⊘") is a real count of zero —
  the chef checked and there is none. Blank is different: nobody counted.
  Blank stays None and is never turned into 0.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from app.core.enums import InventoryUnit

#: Suffixes the kitchen actually writes, mapped to (canonical unit, factor
#: into that unit's base). Grams and millilitres are their own base.
_SUFFIXES: dict[str, tuple[InventoryUnit, float]] = {
    "g": (InventoryUnit.GRAM, 1),
    "gm": (InventoryUnit.GRAM, 1),
    "gms": (InventoryUnit.GRAM, 1),
    "gram": (InventoryUnit.GRAM, 1),
    "kg": (InventoryUnit.GRAM, 1000),
    "kgs": (InventoryUnit.GRAM, 1000),
    "ml": (InventoryUnit.MILLILITRE, 1),
    "l": (InventoryUnit.MILLILITRE, 1000),
    "ltr": (InventoryUnit.MILLILITRE, 1000),
    "litre": (InventoryUnit.MILLILITRE, 1000),
    "pc": (InventoryUnit.PIECE, 1),
    "pcs": (InventoryUnit.PIECE, 1),
    "piece": (InventoryUnit.PIECE, 1),
    "pk": (InventoryUnit.PACKET, 1),
    "pkt": (InventoryUnit.PACKET, 1),
    "packet": (InventoryUnit.PACKET, 1),
    "roll": (InventoryUnit.ROLL, 1),
    "box": (InventoryUnit.BOX, 1),
    "btl": (InventoryUnit.BOTTLE, 1),
    "bottle": (InventoryUnit.BOTTLE, 1),
    "jug": (InventoryUnit.JUG, 1),
}

#: Units whose plain numbers are counts of discrete things. "1.500" of these
#: would be nonsense, so the thousands-dot convention only applies to the
#: continuous units.
_CONTINUOUS = {
    InventoryUnit.GRAM,
    InventoryUnit.KILOGRAM,
    InventoryUnit.MILLILITRE,
    InventoryUnit.LITRE,
}

#: What each catalogue unit means as a base for arithmetic. kilogram/litre
#: items store their counts in their own unit; the sheet writes grams-column
#: numbers, so conversion targets matter.
_BASE_OF = {
    InventoryUnit.KILOGRAM: (InventoryUnit.GRAM, 1000),
    InventoryUnit.LITRE: (InventoryUnit.MILLILITRE, 1000),
}

_ZEROS = {"0", "o", "⊘", "∅", "nil", "zero"}

_TOKEN = re.compile(r"^(\d+(?:[.,]\d+)?)\s*([a-z]+)?$")


@dataclass(frozen=True)
class Parsed:
    """A successful read: the quantity in the item's own unit, plus working."""

    qty: float
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Refused:
    """A deliberate refusal, with the reason a reviewer will read."""

    reason: str
    detail: dict[str, Any] = field(default_factory=dict)


def _to_item_unit(
    value: float, value_unit: InventoryUnit, item_unit: InventoryUnit
) -> float | None:
    """Convert between a written unit and the item's catalogue unit, or None
    when they are different dimensions."""
    if value_unit == item_unit:
        return value
    v_base = _BASE_OF.get(value_unit)
    i_base = _BASE_OF.get(item_unit)
    # written kg, item gram: 1 kg -> 1000 g
    if v_base and v_base[0] == item_unit:
        return value * v_base[1]
    # written gram, item kilogram: 500 g -> 0.5 kg
    if i_base and i_base[0] == value_unit:
        return value / i_base[1]
    return None


def parse_quantity(raw: str | None, item_unit: InventoryUnit) -> Parsed | Refused | None:
    """One handwritten cell into a number in the item's unit.

    Returns None for a blank cell — which is "not counted", a different fact
    from zero, and must stay distinguishable all the way to the screen.
    """
    if raw is None:
        return None
    text = raw.strip().lower()
    if not text:
        return None

    if text in _ZEROS:
        return Parsed(qty=0.0, detail={"read_as": "zero"})

    match = _TOKEN.match(text.replace(" ", ""))
    if not match:
        # "1kg 7pc", "150 100" — a compound or ambiguous scrawl. A human
        # decides; the machine records why it stepped back.
        return Refused(
            reason="unparseable",
            detail={"raw": raw, "why": "not a single number with an optional unit"},
        )

    number_text, suffix = match.group(1), match.group(2)
    number = float(number_text.replace(",", "."))

    if suffix:
        if suffix not in _SUFFIXES:
            return Refused(reason="unknown_unit", detail={"raw": raw, "unit": suffix})
        written_unit, factor = _SUFFIXES[suffix]
        in_written = number * factor
        converted = _to_item_unit(in_written, written_unit, item_unit)
        if converted is None:
            # "5pk" on a grams item. Packets of WHAT weight? Only the kitchen
            # knows. Refusing is the only honest move.
            return Refused(
                reason="unit_mismatch",
                detail={
                    "raw": raw,
                    "written_unit": written_unit.value,
                    "item_unit": item_unit.value,
                },
            )
        return Parsed(
            qty=converted,
            # The suffix as WRITTEN, not its canonical unit — a reviewer
            # checking "3kg" against the note must see "3 kg", not "3 gram".
            detail={"read_as": f"{number:g} {suffix}", "converted_to": item_unit.value},
        )

    # No suffix: the number is in the sheet's column unit, which is the item's
    # unit — except for the kitchen's thousands-dot on continuous units.
    if "." in number_text and item_unit in _CONTINUOUS:
        whole, frac = number_text.split(".")
        if len(frac) == 3:
            # "1.500", "2.800" — three digits after the dot is the kg-written-
            # in-grams-column convention, always.
            qty = float(whole) * 1000 + float(frac)
            return Parsed(qty=qty, detail={"read_as": "thousands_dot", "raw": raw})
        # "1.5" on a grams row: 1.5 grams of ginger is not a thing the kitchen
        # counts; 1.5 kg is. But inferring that silently is a guess.
        return Refused(
            reason="ambiguous_decimal",
            detail={"raw": raw, "why": "decimal on a continuous unit; kg or unit unclear"},
        )

    return Parsed(qty=number, detail={"read_as": f"plain {item_unit.value}"})
