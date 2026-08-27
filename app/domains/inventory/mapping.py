"""Mapping extracted row names onto the canonical catalogue.

The printed sheet was generated FROM the catalogue, so most rows match
exactly. What breaks the exactness is the extractor: OCR slips ("Peelred
Garlic"), respellings ("Shiitake" for "Shitake"), and outright substitutions
of a similar-looking word. The ladder here runs cheapest-and-safest first:

    exact english  ->  exact bengali  ->  remembered alias  ->  fuzzy  ->  nobody

Fuzzy is deliberately conservative: a high bar, and REFUSED outright when two
catalogue items score close to each other — mapping "Chilli" onto the wrong
one of Chilli Powder / Chilli Flakes / Dry Chilli would corrupt a requisition
quietly, and that family of near-neighbours is real in this catalogue.

Once a human resolves an unmatched row, the spelling is remembered as an
alias (spec: "human confirms unmatched rows once, mapping is remembered") —
so the ladder gets shorter every month the kitchen uses it.
"""

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

#: Below this, a fuzzy candidate is not a match at all. Calibrated on
#: measured pairs, not intuition: real OCR slips of catalogue names score
#: >= 0.96 ("Peelred Garlic" 0.963, "Shiitake" for Shitake 0.970), while the
#: measured false positive — "Mystery Sauce" onto Oyster Sauce — scores
#: 0.880. The floor sits between the clusters.
FUZZY_FLOOR = 0.92
#: If the runner-up is within this of the best, the best is not trustworthy —
#: two catalogue items are plausibly the same sheet row, so nobody wins.
FUZZY_AMBIGUITY_GAP = 0.06


def normalise(name: str) -> str:
    """One spelling for comparison: casefold, strip accents-preserving Bengali,
    collapse whitespace and punctuation."""
    text = unicodedata.normalize("NFKC", name).casefold().strip()
    text = re.sub(r"[^\w\sঀ-৿]", " ", text)  # keep Bengali block
    return re.sub(r"\s+", " ", text).strip()


@dataclass(frozen=True)
class CatalogueEntry:
    item_id: str
    name: str
    name_bn: str | None
    unit: str


@dataclass(frozen=True)
class Match:
    item_id: str
    method: str  # 'exact' | 'bengali' | 'alias' | 'fuzzy'
    detail: dict[str, Any]


class Mapper:
    """Built once per extraction run from the catalogue + alias table, then
    asked about each row. Pure and synchronous: the database work happens
    before and after, never inside."""

    def __init__(
        self,
        entries: list[CatalogueEntry],
        aliases: dict[str, str],  # normalised alias -> item_id
    ) -> None:
        self._by_name: dict[str, CatalogueEntry] = {}
        self._by_bengali: dict[str, CatalogueEntry] = {}
        for entry in entries:
            self._by_name.setdefault(normalise(entry.name), entry)
            if entry.name_bn:
                self._by_bengali.setdefault(normalise(entry.name_bn), entry)
        self._aliases = {normalise(k): v for k, v in aliases.items()}
        self._entries = entries

    def match(self, raw_name: str) -> Match | None:
        wanted = normalise(raw_name)
        if not wanted:
            return None

        if entry := self._by_name.get(wanted):
            return Match(entry.item_id, "exact", {})
        if entry := self._by_bengali.get(wanted):
            return Match(entry.item_id, "bengali", {})
        if item_id := self._aliases.get(wanted):
            return Match(item_id, "alias", {"alias": wanted})

        scored = sorted(
            ((SequenceMatcher(None, wanted, normalise(e.name)).ratio(), e) for e in self._entries),
            key=lambda pair: pair[0],
            reverse=True,
        )
        if not scored or scored[0][0] < FUZZY_FLOOR:
            return None
        best_score, best = scored[0]
        if len(scored) > 1 and best_score - scored[1][0] < FUZZY_AMBIGUITY_GAP:
            # Two plausible owners. Guessing between "Chilli Flakes" and
            # "Chilli Powder" is how a kitchen orders the wrong stock.
            return None
        return Match(
            best.item_id,
            "fuzzy",
            {"score": round(best_score, 3), "matched_name": best.name},
        )
