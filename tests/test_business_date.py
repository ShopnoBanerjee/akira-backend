"""The business date, in Python.

Parity with the Postgres function is asserted separately, in
test_migrations.py, against a live database.
"""

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.core.business_date import (
    OUTLET_TZ,
    business_date,
    business_date_bounds,
    due_at,
)

IST = OUTLET_TZ
UTC = ZoneInfo("UTC")


def ist(y: int, m: int, d: int, hh: int = 0, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=IST)


@pytest.mark.parametrize(
    ("moment", "expected", "why"),
    [
        (ist(2026, 8, 22, 18, 0), date(2026, 8, 22), "evening service, same day"),
        (ist(2026, 8, 23, 0, 0), date(2026, 8, 22), "midnight is still Saturday's night"),
        (ist(2026, 8, 23, 1, 30), date(2026, 8, 22), "01:30 close belongs to the night before"),
        (ist(2026, 8, 23, 4, 59), date(2026, 8, 22), "one minute before rollover"),
        (ist(2026, 8, 23, 5, 0), date(2026, 8, 23), "exactly at rollover, new trading day"),
        (ist(2026, 8, 23, 5, 1), date(2026, 8, 23), "just after rollover"),
        (ist(2026, 8, 23, 6, 0), date(2026, 8, 23), "morning proper"),
        (ist(2026, 8, 23, 23, 59), date(2026, 8, 23), "late evening, same day"),
        # A year boundary crossed by a late close.
        (ist(2027, 1, 1, 2, 0), date(2026, 12, 31), "new year's small hours are still NYE"),
        # A month boundary.
        (ist(2026, 9, 1, 3, 0), date(2026, 8, 31), "small hours of the 1st belong to the 31st"),
        # A leap day.
        (ist(2028, 3, 1, 2, 0), date(2028, 2, 29), "leap day night"),
    ],
)
def test_rollover(moment: datetime, expected: date, why: str) -> None:
    assert business_date(moment) == expected, why


def test_utc_input_is_converted_not_assumed() -> None:
    """19:30 UTC is 01:00 IST the next day, which is still the previous
    trading day. Treating the UTC date as the business date would be wrong by
    two days here, which is exactly the bug this function exists to prevent."""
    moment = datetime(2026, 8, 22, 19, 30, tzinfo=UTC)
    assert moment.astimezone(IST).date() == date(2026, 8, 23)
    assert business_date(moment) == date(2026, 8, 22)


def test_naive_datetime_is_rejected() -> None:
    """A naive timestamp cannot be placed on a trading day without guessing its
    zone, and a wrong guess is silent. Better to raise."""
    with pytest.raises(ValueError, match="aware datetime"):
        business_date(datetime(2026, 8, 23, 1, 30))


def test_bounds_are_half_open_and_cover_exactly_one_day() -> None:
    start, end = business_date_bounds(date(2026, 8, 22))
    assert business_date(start) == date(2026, 8, 22)
    assert business_date(end) == date(2026, 8, 23), "end is exclusive"
    assert (end - start).total_seconds() == 24 * 3600


def test_every_moment_in_bounds_maps_back_to_the_same_day() -> None:
    day = date(2026, 8, 22)
    start, end = business_date_bounds(day)
    probe = start
    while probe < end:
        assert business_date(probe) == day
        probe += (end - start) / 24


class TestDueAt:
    """A due time before the rollover hour belongs to the NEXT calendar day.
    Getting this backwards makes every closing checklist look a day late."""

    def test_closing_checklist_after_midnight(self) -> None:
        # Due 00:30 on the trading day of 22 Aug = 00:30 on 23 Aug.
        assert due_at(date(2026, 8, 22), time(0, 30)) == ist(2026, 8, 23, 0, 30)

    def test_opening_checklist_same_day(self) -> None:
        assert due_at(date(2026, 8, 22), time(17, 0)) == ist(2026, 8, 22, 17, 0)

    def test_due_time_at_rollover_hour_stays_on_the_day(self) -> None:
        assert due_at(date(2026, 8, 22), time(5, 0)) == ist(2026, 8, 22, 5, 0)

    def test_due_time_just_before_rollover_moves_to_next_day(self) -> None:
        assert due_at(date(2026, 8, 22), time(4, 59)) == ist(2026, 8, 23, 4, 59)

    @pytest.mark.parametrize("due_time", [time(0, 30), time(17, 0), time(23, 45)])
    def test_due_at_always_lands_inside_its_own_trading_day(self, due_time: time) -> None:
        day = date(2026, 8, 22)
        assert business_date(due_at(day, due_time)) == day
