"""Materialisation and the submit pipeline, against a real database."""

from datetime import date

import pytest

from app.domains.sop.runs_service import assignment_occurs_on

pytestmark = pytest.mark.asyncio


class TestOccurrence:
    """The cadence maths that decides which runs exist each morning."""

    def test_daily_runs_every_day(self) -> None:
        for day in range(20, 27):
            assert assignment_occurs_on(
                date(2026, 8, day),
                active_weekdays=[0, 1, 2, 3, 4, 5, 6],
                interval_days=None,
                anchor_date=None,
            )

    def test_monday_only(self) -> None:
        # 2026-08-24 is a Monday (Postgres dow 1).
        assert assignment_occurs_on(
            date(2026, 8, 24), active_weekdays=[1], interval_days=None, anchor_date=None
        )
        assert not assignment_occurs_on(
            date(2026, 8, 25), active_weekdays=[1], interval_days=None, anchor_date=None
        )

    def test_sunday_is_postgres_dow_zero(self) -> None:
        # 2026-08-23 is a Sunday.
        assert assignment_occurs_on(
            date(2026, 8, 23), active_weekdays=[0], interval_days=None, anchor_date=None
        )

    def test_alternate_day_counts_from_anchor(self) -> None:
        anchor = date(2026, 8, 20)
        assert assignment_occurs_on(
            date(2026, 8, 20), active_weekdays=[], interval_days=2, anchor_date=anchor
        )
        assert not assignment_occurs_on(
            date(2026, 8, 21), active_weekdays=[], interval_days=2, anchor_date=anchor
        )
        assert assignment_occurs_on(
            date(2026, 8, 22), active_weekdays=[], interval_days=2, anchor_date=anchor
        )

    def test_before_the_anchor_never_occurs(self) -> None:
        assert not assignment_occurs_on(
            date(2026, 8, 19),
            active_weekdays=[],
            interval_days=2,
            anchor_date=date(2026, 8, 20),
        )

    def test_fortnightly(self) -> None:
        anchor = date(2026, 8, 12)
        assert assignment_occurs_on(
            date(2026, 8, 26), active_weekdays=[], interval_days=14, anchor_date=anchor
        )
        assert not assignment_occurs_on(
            date(2026, 8, 27), active_weekdays=[], interval_days=14, anchor_date=anchor
        )
