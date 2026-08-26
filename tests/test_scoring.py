"""Table-driven scoring tests, per spec section 4.3."""

import pytest

from app.core.enums import ItemResult
from app.core.scoring import (
    ScorableItem,
    critical_fail_count,
    haversine_m,
    item_weight,
    out_of_range,
    run_score,
)

P, F, NA = ItemResult.PASS, ItemResult.FAIL, ItemResult.NA


def items(*spec: tuple[ItemResult, bool]) -> list[ScorableItem]:
    return [ScorableItem(result=r, is_critical=c) for r, c in spec]


class TestItemWeight:
    def test_normal_is_one(self) -> None:
        assert item_weight(False) == 1

    def test_critical_is_three_by_default(self) -> None:
        assert item_weight(True) == 3

    def test_critical_weight_is_configurable(self) -> None:
        assert item_weight(True, critical_weight=5) == 5


class TestRunScore:
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            # All pass -> 100, regardless of criticality mix.
            ([(P, False), (P, False)], 100.0),
            ([(P, True), (P, False)], 100.0),
            # All fail -> 0.
            ([(F, False), (F, True)], 0.0),
            # One normal fail among three normals: 2/3.
            ([(P, False), (P, False), (F, False)], 66.67),
            # A critical fail hurts three times as much: pass 1 normal,
            # fail 1 critical -> 1/4.
            ([(P, False), (F, True)], 25.0),
            # The same shapes with n/a excluded from the denominator.
            ([(P, False), (NA, False)], 100.0),
            ([(F, False), (NA, True)], 0.0),
            ([(P, False), (F, False), (NA, True)], 50.0),
            # Spec worked example: 10 items, 2 critical; fail one critical.
            (
                [(P, True), (F, True)] + [(P, False)] * 8,
                round(100 * 11 / 14, 2),
            ),
        ],
    )
    def test_table(self, spec: list, expected: float) -> None:
        assert run_score(items(*spec)) == expected

    def test_all_na_is_none_not_zero(self) -> None:
        """A run with nothing applicable did not fail — scoring it 0 would
        poison the outlet mean, and 100 would reward doing nothing."""
        assert run_score(items((NA, False), (NA, True))) is None

    def test_zero_items_is_none(self) -> None:
        assert run_score([]) is None

    def test_all_critical_all_pass(self) -> None:
        assert run_score(items((P, True), (P, True))) == 100.0

    def test_pending_counts_against_the_score(self) -> None:
        """An unanswered item is applicable weight that was not earned. Submit
        validation refuses pending items anyway; this is the backstop."""
        assert run_score(items((P, False), (ItemResult.PENDING, False))) == 50.0

    def test_custom_critical_weight_changes_the_arithmetic(self) -> None:
        # weight 5: pass 1 normal, fail 1 critical -> 1/6
        assert run_score(items((P, False), (F, True)), critical_weight=5) == 16.67


class TestCriticalFailCount:
    def test_counts_only_critical_fails(self) -> None:
        assert (
            critical_fail_count(items((F, True), (F, False), (P, True), (F, True), (NA, True))) == 2
        )


class TestOutOfRange:
    @pytest.mark.parametrize(
        ("value", "lo", "hi", "expected"),
        [
            (80, 75, 95, False),
            (75, 75, 95, False),  # inclusive lower bound
            (95, 75, 95, False),  # inclusive upper bound
            (74.9, 75, 95, True),
            (95.1, 75, 95, True),
            (-20, -18, -15, True),  # freezer: too cold is also wrong
            (-16, -18, -15, False),
            (5, None, 10, False),  # only an upper bound
            (11, None, 10, True),
            (5, 1, None, False),  # only a lower bound
            (0, 1, None, True),
            (999, None, None, False),  # no bounds -> never out of range
        ],
    )
    def test_table(self, value: float, lo: float | None, hi: float | None, expected: bool) -> None:
        assert out_of_range(value, lo, hi) is expected


class TestHaversine:
    def test_zero_distance(self) -> None:
        assert haversine_m(22.5023, 88.3852, 22.5023, 88.3852) == 0.0

    def test_known_distance_new_town_to_esplanade(self) -> None:
        """~8 km between AKIRA New Town and central Kolkata. Loose tolerance —
        the geofence check cares about 150 m vs 8 km, not survey precision."""
        d = haversine_m(22.5023, 88.3852, 22.5726, 88.3639)
        assert 7_000 < d < 9_000

    def test_inside_a_150m_geofence(self) -> None:
        # ~55 m north of the outlet.
        d = haversine_m(22.5023, 88.3852, 22.5028, 88.3852)
        assert d < 150
