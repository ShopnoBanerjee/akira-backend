"""The outlet SOP score (spec 4.3).

Worked by hand from the spec's arithmetic, because a scoring bug is the kind
that never announces itself — the number is plausible, everybody acts on it for
a quarter, and the error only surfaces when somebody finally recomputes a month
by hand and gets something different.
"""

import pytest

from app.core.scoring import (
    Component,
    OutletCounts,
    ScoreWeights,
    band_for,
    outlet_score,
    rate,
)


def counts(**overrides: object) -> OutletCounts:
    base: dict[str, object] = {
        "scheduled": 100,
        "approved": 100,
        "submitted": 100,
        "on_time": 100,
        "missed": 0,
        "mean_run_score": 100.0,
        "integrity_flags": 0,
        "open_critical": 0,
        "stale_critical": 0,
    }
    return OutletCounts(**{**base, **overrides})  # type: ignore[arg-type]


class TestTheFormula:
    def test_a_perfect_period_scores_one_hundred(self) -> None:
        assert outlet_score(counts()).score == 100.0

    def test_the_spec_weights_are_applied_as_written(self) -> None:
        """0.50 x 80 + 0.30 x 50 + 0.20 x 50 = 40 + 15 + 10 = 65."""
        score = outlet_score(
            counts(scheduled=100, approved=50, submitted=100, on_time=50, mean_run_score=80.0)
        )
        assert score.score == 65.0

    def test_completion_is_approved_over_scheduled(self) -> None:
        score = outlet_score(counts(scheduled=40, approved=30))
        completion = next(c for c in score.components if c.key == "completion_rate")
        assert completion.value == 75.0

    def test_on_time_is_measured_against_what_was_submitted(self) -> None:
        """Not against what was scheduled. A run nobody started is a completion
        failure, and counting it twice would punish the same miss in two
        separate terms."""
        score = outlet_score(counts(scheduled=100, approved=50, submitted=50, on_time=25))
        on_time = next(c for c in score.components if c.key == "on_time_rate")
        assert on_time.value == 50.0

    def test_a_component_with_no_denominator_contributes_nothing(self) -> None:
        """An outlet that approved no runs has earned no run-score credit.
        Re-weighting the remaining terms would hand it full marks for a paper
        it never sat."""
        score = outlet_score(
            counts(scheduled=10, approved=0, submitted=0, on_time=0, mean_run_score=None)
        )
        assert score.score == 0.0
        run_score = next(c for c in score.components if c.key == "run_score")
        assert run_score.value is None and run_score.contribution == 0.0

    def test_nothing_scheduled_is_no_score_rather_than_zero(self) -> None:
        """A closed outlet did not fail. Scoring it zero would drag a network
        average down for days the doors were shut."""
        score = outlet_score(counts(scheduled=0, approved=0, submitted=0, on_time=0))
        assert score.score is None
        assert score.band == "none"


class TestPenalties:
    def test_two_points_per_stale_critical_failure(self) -> None:
        score = outlet_score(counts(open_critical=3, stale_critical=3))
        penalty = next(p for p in score.penalties if p.key == "stale_exceptions")
        assert penalty.points == 6.0
        # 100 - 6, then held at amber by the unresolved criticals.
        assert score.score == 94.0

    def test_integrity_flags_are_penalised_as_a_rate_not_a_count(self) -> None:
        """ "1 point per integrity flag per 10 runs". An outlet running twice as
        many checklists must not be punished twice as hard for the same
        standard of honesty."""
        small = outlet_score(
            counts(scheduled=10, approved=10, submitted=10, on_time=10, integrity_flags=2)
        )
        large = outlet_score(
            counts(scheduled=100, approved=100, submitted=100, on_time=100, integrity_flags=20)
        )
        assert small.score == large.score == 98.0

    def test_the_penalty_scales_with_the_flag_rate(self) -> None:
        score = outlet_score(
            counts(scheduled=50, approved=50, submitted=50, on_time=50, integrity_flags=6)
        )
        penalty = next(p for p in score.penalties if p.key == "integrity_flags")
        assert penalty.points == 1.2
        assert "6 across 50 runs" in penalty.detail

    def test_a_penalty_with_nothing_to_penalise_is_not_listed(self) -> None:
        assert outlet_score(counts()).penalties == []

    def test_the_score_is_clamped_at_zero(self) -> None:
        score = outlet_score(
            counts(
                scheduled=1,
                approved=0,
                submitted=0,
                on_time=0,
                mean_run_score=None,
                integrity_flags=40,
                open_critical=20,
                stale_critical=20,
            )
        )
        assert score.score == 0.0


class TestBands:
    def test_the_spec_thresholds(self) -> None:
        assert band_for(90.0) == "green"
        assert band_for(89.9) == "amber"
        assert band_for(75.0) == "amber"
        assert band_for(74.9) == "red"

    def test_bands_come_from_the_weights_so_admins_can_move_them(self) -> None:
        strict = ScoreWeights(green=95.0, amber=85.0)
        assert band_for(92.0, strict) == "amber"
        assert band_for(92.0) == "green"

    def test_one_unresolved_critical_failure_caps_the_outlet_at_amber(self) -> None:
        """Spec 4.3, and the whole point of the compliance system: you cannot
        be a green outlet with an open critical food-safety failure."""
        score = outlet_score(counts(open_critical=1, stale_critical=0))
        assert score.score == 100.0  # the arithmetic is left honest
        assert score.band == "amber"
        assert score.capped_by_critical is True

    def test_the_cap_does_not_raise_a_red_outlet_to_amber(self) -> None:
        score = outlet_score(
            counts(scheduled=100, approved=20, submitted=20, on_time=10, open_critical=1)
        )
        assert score.band == "red"
        assert score.capped_by_critical is False

    def test_a_resolved_critical_failure_stops_capping(self) -> None:
        assert outlet_score(counts(open_critical=0)).band == "green"


class TestWhatToFix:
    def test_it_names_the_lowest_component(self) -> None:
        score = outlet_score(
            counts(scheduled=100, approved=95, submitted=100, on_time=62, mean_run_score=97.0)
        )
        worst = score.worst_component
        assert worst is not None
        assert worst.key == "on_time_rate"
        assert worst.value == 62.0

    def test_it_picks_the_lowest_value_not_the_lowest_contribution(self) -> None:
        """The heavily weighted term contributes least in absolute points when
        it is nearly perfect. Naming it would send a manager to fix the thing
        that is already working."""
        score = outlet_score(
            counts(scheduled=100, approved=100, submitted=100, on_time=30, mean_run_score=99.0)
        )
        assert score.worst_component is not None
        assert score.worst_component.key == "on_time_rate"

    def test_nothing_to_name_when_the_period_is_empty(self) -> None:
        score = outlet_score(
            counts(scheduled=0, approved=0, submitted=0, on_time=0, mean_run_score=None)
        )
        assert score.worst_component is None


class TestRate:
    def test_it_does_not_clamp_an_impossible_rate(self) -> None:
        """on_time is a subset of submitted by construction — the SQL counts it
        with a filter over the same rows. A rate above 100% would mean the
        counting is wrong, and silently clamping it would hide that."""
        assert rate(11, 10) == 110.0

    def test_a_percentage(self) -> None:
        assert rate(3, 4) == 75.0

    def test_no_denominator_is_none_not_zero(self) -> None:
        assert rate(0, 0) is None

    def test_it_rounds_to_one_place(self) -> None:
        assert rate(1, 3) == 33.3


class TestComponent:
    def test_contribution_is_weight_times_value(self) -> None:
        assert Component("k", "l", 80.0, 0.5).contribution == 40.0

    def test_an_absent_value_contributes_nothing(self) -> None:
        assert Component("k", "l", None, 0.5).contribution == 0.0


class TestAgainstAWorkedExample:
    def test_a_realistic_month(self) -> None:
        """Worked by hand:

        56 scheduled, 48 approved, 52 submitted, 41 on time,
        mean approved score 91.4, 7 integrity flags,
        2 open high-severity exceptions, both over 48h.

        run score      0.50 x 91.4                  = 45.70
        completion     0.30 x (48/56 = 85.7)        = 25.71
        on time        0.20 x (41/52 = 78.8)        = 15.76
                                                      ------
                                                      87.17
        stale          - 2 x 2                      = -4.00
        integrity      - 1 x (10 x 7 / 56 = 1.25)   = -1.25
                                                      ------
                                                      81.92 -> 81.9
        """
        score = outlet_score(
            counts(
                scheduled=56,
                approved=48,
                submitted=52,
                on_time=41,
                missed=4,
                mean_run_score=91.4,
                integrity_flags=7,
                open_critical=2,
                stale_critical=2,
            )
        )
        assert score.score == pytest.approx(81.9, abs=0.1)
        assert score.band == "amber"
        assert score.worst_component is not None
        assert score.worst_component.key == "on_time_rate"
