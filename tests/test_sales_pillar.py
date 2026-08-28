"""The Sales pillar arithmetic (spec section 5), worked by hand.

Same reasoning as the SOP score suite: a scoring bug produces a plausible
number that everyone acts on for a quarter. The cases here include AKIRA's
own published baseline (Rs 12,791/day, AOV Rs 1,075, 31% phone capture) so
the pillar's reading of the real situation is pinned, not just its edges.
"""

from app.domains.sales.pillar import (
    SalesInputs,
    SalesTargets,
    rupees,
    sales_pillar,
)


def targets(**overrides: float) -> SalesTargets:
    base: dict[str, float] = {
        "net_per_day_paise": 18_000_00,
        "amber_net_per_day_paise": 13_000_00,
        "orders_per_day": 20,
        "aov_paise": 1_150_00,
        "monwed_share": 0.45,
        "phone_capture": 0.80,
        "max_discount_share": 0.03,
        "w_net": 0.35,
        "w_orders": 0.25,
        "w_monwed": 0.15,
        "w_phone": 0.15,
        "w_aov": 0.05,
        "w_discount": 0.05,
        "green": 85,
        "amber": 70,
    }
    base.update(overrides)
    return SalesTargets(**base)  # type: ignore[arg-type]


def perfect_inputs() -> SalesInputs:
    """A 10-day period that hits every target exactly."""
    return SalesInputs(
        trading_days=10,
        bills=200,  # 20/day
        net_paise=200 * 1_150_00,  # AOV exactly on target -> net/day 23,000 > 18k
        gross_paise=200 * 1_150_00,
        discount_paise=0,
        monwed_net_paise=int(200 * 1_150_00 * 0.45),
        bills_with_phone=160,  # 80%
    )


class TestTheFormula:
    def test_hitting_every_target_scores_one_hundred(self) -> None:
        pillar = sales_pillar(perfect_inputs(), targets())
        assert pillar.score == 100.0
        assert pillar.band == "green"
        assert all(c.score == 100.0 for c in pillar.components)

    def test_akiras_own_baseline_reads_as_the_spec_describes(self) -> None:
        """The spec's published current numbers: Rs 12,791/day over 38 trading
        days, 452 bills, AOV Rs 1,075, ~40% Mon-Wed, 31% phone capture, 0.8%
        discount. The pillar should read this as a red-to-amber situation
        dragged down by phone capture — which is exactly the growth story the
        spec tells."""
        inputs = SalesInputs(
            trading_days=38,
            bills=452,
            net_paise=4_86_076_35,  # Rs 4,86,076.35
            gross_paise=4_90_000_00,
            discount_paise=int(4_90_000_00 * 0.008),
            monwed_net_paise=int(4_86_076_35 * 0.40),
            bills_with_phone=140,  # 31%
        )
        pillar = sales_pillar(inputs, targets())
        assert pillar.score is not None
        # Hand check: net/day 12,791/18,000 = 71.1 x .35 = 24.9
        #             orders 11.9/20 = 59.5 x .25 = 14.9
        #             monwed 40/45 = 88.9 x .15 = 13.3
        #             phone 31/80 = 38.7 x .15 = 5.8
        #             aov 1075/1150 = 93.5 x .05 = 4.7
        #             discount 0.8% <= 3% = 100 x .05 = 5.0
        # total = 68.6
        assert 67.0 <= pillar.score <= 70.0
        assert pillar.band == "red"  # below the 70 amber line, just
        worst = pillar.worst_component
        assert worst is not None and worst.key == "phone_capture"

    def test_every_component_shows_its_working(self) -> None:
        pillar = sales_pillar(perfect_inputs(), targets())
        for component in pillar.components:
            assert component.display
            assert component.target_display
            assert component.contribution == round(component.score * component.weight, 1)

    def test_scores_cap_at_one_hundred(self) -> None:
        """Doubling the target must not double the score — a blowout week
        cannot bank surplus against a bad one."""
        inputs = perfect_inputs()
        doubled = SalesInputs(
            trading_days=inputs.trading_days,
            bills=inputs.bills * 4,
            net_paise=inputs.net_paise * 4,
            gross_paise=inputs.gross_paise * 4,
            discount_paise=0,
            monwed_net_paise=inputs.monwed_net_paise * 4,
            bills_with_phone=inputs.bills * 4,
        )
        pillar = sales_pillar(doubled, targets())
        assert pillar.score == 100.0


class TestBands:
    def test_net_sales_uses_the_specs_explicit_amber_floor(self) -> None:
        """Rs 13-18k is amber, below 13k is red — the one component with a
        published floor rather than a derived one."""

        def with_net_per_day(paise_per_day: int) -> str:
            inputs = SalesInputs(
                trading_days=10,
                bills=200,
                net_paise=paise_per_day * 10,
                gross_paise=paise_per_day * 10,
                discount_paise=0,
                monwed_net_paise=int(paise_per_day * 10 * 0.45),
                bills_with_phone=160,
            )
            pillar = sales_pillar(inputs, targets())
            return next(c for c in pillar.components if c.key == "net_per_day").band

        assert with_net_per_day(19_000_00) == "green"
        assert with_net_per_day(15_000_00) == "amber"
        assert with_net_per_day(12_000_00) == "red"

    def test_discount_over_the_ceiling_is_red_not_amber(self) -> None:
        inputs = SalesInputs(
            trading_days=10,
            bills=200,
            net_paise=200 * 1_150_00,
            gross_paise=200 * 1_150_00,
            discount_paise=int(200 * 1_150_00 * 0.10),  # 10% discounting
            monwed_net_paise=int(200 * 1_150_00 * 0.45),
            bills_with_phone=160,
        )
        pillar = sales_pillar(inputs, targets())
        discount = next(c for c in pillar.components if c.key == "discount_control")
        assert discount.band == "red"
        assert discount.score == 30.0  # 100 x 3/10


class TestNothingTraded:
    def test_a_shut_period_scores_none_not_zero(self) -> None:
        inputs = SalesInputs(
            trading_days=0,
            bills=0,
            net_paise=0,
            gross_paise=0,
            discount_paise=0,
            monwed_net_paise=0,
            bills_with_phone=0,
        )
        pillar = sales_pillar(inputs, targets())
        assert pillar.score is None
        assert pillar.band == "none"


class TestIndianGrouping:
    def test_the_lakh_comma(self) -> None:
        assert rupees(4_86_076_35) == "₹4,86,076"
        assert rupees(18_000_00) == "₹18,000"
        assert rupees(999_00) == "₹999"
        assert rupees(1_07_500_00) == "₹1,07,500"
