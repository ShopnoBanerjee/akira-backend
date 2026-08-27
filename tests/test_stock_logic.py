"""The deterministic heart of the stock engine: parsing, mapping, arithmetic.

Every quantity case here was read off the real 27 Aug 2026 count sheet — the
fixture PDF — not invented. The parser's contract is the thing under test:
succeed with working attached, or refuse with the raw preserved. A parser
that guesses would pass a weaker suite and corrupt a requisition in month
three.
"""

from app.core.enums import InventoryUnit
from app.domains.inventory.mapping import CatalogueEntry, Mapper, normalise
from app.domains.inventory.normalize import Parsed, Refused, parse_quantity
from app.domains.inventory.requisition import LineInputs, compute_line, round_up_to

G = InventoryUnit.GRAM
PC = InventoryUnit.PIECE
KG = InventoryUnit.KILOGRAM


class TestParsingTheRealSheetsHandwriting:
    def test_a_plain_number_is_the_items_unit(self) -> None:
        result = parse_quantity("500", G)
        assert isinstance(result, Parsed) and result.qty == 500

    def test_the_kitchens_thousands_dot(self) -> None:
        """ "1.500" in a grams column is 1.5 kg. Nobody counts half a gram of
        ginger. "2.800" on the sugar row is the same convention."""
        assert parse_quantity("1.500", G).qty == 1500  # type: ignore[union-attr]
        assert parse_quantity("2.800", G).qty == 2800  # type: ignore[union-attr]

    def test_a_two_digit_decimal_is_refused_not_guessed(self) -> None:
        """ "1.5" could be 1.5 kg or a slip. The convention is three digits;
        anything else goes to a human."""
        result = parse_quantity("1.5", G)
        assert isinstance(result, Refused) and result.reason == "ambiguous_decimal"

    def test_kg_written_on_a_grams_row_converts(self) -> None:
        result = parse_quantity("1kg", G)
        assert isinstance(result, Parsed) and result.qty == 1000
        assert parse_quantity("3 kg", G).qty == 3000  # type: ignore[union-attr]

    def test_grams_written_on_a_kilogram_item_converts_down(self) -> None:
        result = parse_quantity("500g", KG)
        assert isinstance(result, Parsed) and result.qty == 0.5

    def test_packets_on_a_grams_item_are_refused(self) -> None:
        """ "5pk" of chilli powder: packets of what weight? Only the kitchen
        knows, so only the kitchen answers."""
        result = parse_quantity("5pk", G)
        assert isinstance(result, Refused) and result.reason == "unit_mismatch"

    def test_pieces_on_a_piece_item_parse(self) -> None:
        result = parse_quantity("7pc", PC)
        assert isinstance(result, Parsed) and result.qty == 7

    def test_a_compound_scrawl_is_refused(self) -> None:
        """ "1kg 7pc" — the real button-mushroom cell. Two numbers, two units,
        one cell. Machine steps back."""
        result = parse_quantity("1kg 7pc", G)
        assert isinstance(result, Refused) and result.reason == "unparseable"

    def test_the_circled_zero_is_a_real_zero(self) -> None:
        for raw in ("0", "O", "⊘"):
            result = parse_quantity(raw, G)
            assert isinstance(result, Parsed) and result.qty == 0, raw

    def test_blank_is_not_zero(self) -> None:
        """Blank means nobody counted — a different fact from counted-none,
        and the difference must survive to the screen."""
        assert parse_quantity(None, G) is None
        assert parse_quantity("   ", G) is None

    def test_an_unknown_unit_is_refused(self) -> None:
        result = parse_quantity("3 bunches", G)
        assert isinstance(result, Refused) and result.reason == "unknown_unit"

    def test_the_working_is_always_attached(self) -> None:
        """A reviewer sees reasoning, not verdicts."""
        parsed = parse_quantity("1.500", G)
        assert isinstance(parsed, Parsed) and parsed.detail["read_as"] == "thousands_dot"
        refused = parse_quantity("5pk", G)
        assert isinstance(refused, Refused) and refused.detail["raw"] == "5pk"


def entry(item_id: str, name: str, bn: str | None = None) -> CatalogueEntry:
    return CatalogueEntry(item_id=item_id, name=name, name_bn=bn, unit="gram")


class TestMappingExtractedNames:
    def test_exact_beats_everything(self) -> None:
        mapper = Mapper([entry("a", "Sweet Corn")], aliases={})
        match = mapper.match("sweet  corn")
        assert match and match.item_id == "a" and match.method == "exact"

    def test_bengali_matches_too(self) -> None:
        mapper = Mapper([entry("a", "Begun (Aubergine)", "বেগুন")], aliases={})
        match = mapper.match("বেগুন")
        assert match and match.method == "bengali"

    def test_a_remembered_alias_wins_over_fuzzy(self) -> None:
        """The month-two payoff: last month's correction answers this month's
        sheet without asking anyone."""
        mapper = Mapper([entry("a", "Shitake Mushroom")], aliases={"shiitake mushroom": "a"})
        match = mapper.match("Shiitake Mushroom")
        assert match and match.item_id == "a" and match.method == "alias"

    def test_an_ocr_slip_fuzzy_matches(self) -> None:
        """ "Peelred Garlic" — the model's actual misread of Peeled Garlic."""
        mapper = Mapper([entry("a", "Peeled Garlic"), entry("b", "Spring Onion")], aliases={})
        match = mapper.match("Peelred Garlic")
        assert match and match.item_id == "a" and match.method == "fuzzy"

    def test_the_chilli_family_refuses_to_guess(self) -> None:
        """Chilli Powder, Chilli Flakes, Dry Chilli, Green Chilli — real
        near-neighbours in this catalogue. A bare "Chilli" maps to nobody."""
        mapper = Mapper(
            [
                entry("a", "Chilli Powder"),
                entry("b", "Chilli Flakes"),
                entry("c", "Dry Chilli"),
                entry("d", "Green Chilli"),
            ],
            aliases={},
        )
        assert mapper.match("Chilli") is None

    def test_a_wrong_but_similar_name_stays_unmatched(self) -> None:
        """The measured false positive that set the floor: "Mystery Sauce"
        scores 0.880 against Oyster Sauce — high enough to fool a loose
        threshold, wrong enough to corrupt an order. Real OCR slips score
        above 0.96; the floor sits between."""
        mapper = Mapper([entry("a", "Oyster Sauce")], aliases={})
        assert mapper.match("Mystery Sauce") is None

    def test_garbage_maps_to_nobody(self) -> None:
        mapper = Mapper([entry("a", "Sweet Corn")], aliases={})
        assert mapper.match("Wagyu A5 Striploin") is None
        assert mapper.match("") is None

    def test_normalise_is_stable_for_aliases(self) -> None:
        assert normalise("  Shiitake   Mushroom!! ") == "shiitake mushroom"


class TestRequisitionArithmetic:
    def test_the_formula_from_the_spec(self) -> None:
        line = compute_line(
            LineInputs(item_id="a", on_hand=400, par_level=2000, order_unit=500, requested_qty=None)
        )
        # gap 1600, rounded up to order_unit 500 -> 2000
        assert line.suggested_qty == 2000
        assert line.detail["suggested"]["gap"] == 1600

    def test_rounding_goes_up_never_down(self) -> None:
        assert round_up_to(2.3, 1) == 3
        assert round_up_to(1600, 500) == 2000
        assert round_up_to(2000, 500) == 2000  # exact stays exact

    def test_at_or_above_par_suggests_zero(self) -> None:
        line = compute_line(
            LineInputs(
                item_id="a", on_hand=2500, par_level=2000, order_unit=None, requested_qty=None
            )
        )
        assert line.suggested_qty == 0

    def test_no_par_means_no_number(self) -> None:
        """The confident-nonsense guard: no inputs, no output, a flag instead."""
        line = compute_line(
            LineInputs(
                item_id="a", on_hand=400, par_level=None, order_unit=None, requested_qty=1000
            )
        )
        assert line.suggested_qty is None
        assert "no_par" in line.flags
        assert line.final_qty == 1000  # the chef's ask still stands

    def test_uncounted_is_flagged_not_zeroed(self) -> None:
        line = compute_line(
            LineInputs(
                item_id="a", on_hand=None, par_level=2000, order_unit=None, requested_qty=None
            )
        )
        assert line.suggested_qty is None
        assert "not_counted" in line.flags

    def test_padding_is_flagged_but_the_chef_still_wins(self) -> None:
        """Requested far above the par gap raises the spec's day-one anomaly —
        and final_qty still defaults to the chef's number, because the person
        who ran the shift knows about tomorrow's booking and the formula
        does not."""
        line = compute_line(
            LineInputs(
                item_id="a", on_hand=1800, par_level=2000, order_unit=None, requested_qty=3000
            )
        )
        assert line.suggested_qty == 200
        assert "padding" in line.flags
        assert line.final_qty == 3000

    def test_a_reasonable_ask_is_not_flagged(self) -> None:
        line = compute_line(
            LineInputs(
                item_id="a", on_hand=500, par_level=2000, order_unit=None, requested_qty=1600
            )
        )
        assert "padding" not in line.flags

    def test_every_number_shows_its_working(self) -> None:
        line = compute_line(
            LineInputs(item_id="a", on_hand=400, par_level=2000, order_unit=500, requested_qty=3000)
        )
        working = line.detail["suggested"]
        assert working["par"] == 2000 and working["on_hand"] == 400
        assert working["formula"].startswith("max(0, par - on_hand)")
