"""The business date.

AKIRA trades past midnight. A trading night that starts 18:00 Saturday and ends
01:30 Sunday is ONE business day, rolling over at 05:00 Asia/Kolkata rather than
at midnight.

This module is one of exactly two places that rollover is expressed. The other
is the Postgres function ``business_date(timestamptz)`` in
``supabase/migrations/0002_functions.sql``. They are tested against each other
in ``tests/test_migrations.py``; change neither without changing both.

Never derive a reporting date from ``created_at``. Doing so silently splits
every weekend night across two days, and the error is invisible until someone
questions a number months later.
"""

from datetime import date, datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo

__all__ = [
    "OUTLET_TZ",
    "ROLLOVER_HOUR",
    "business_date",
    "business_date_bounds",
    "due_at",
    "outlet_now",
]

#: The trading day rolls over at this local hour, not at midnight.
ROLLOVER_HOUR = 5

#: Default outlet timezone. Outlets carry their own ``timezone`` column; pass it
#: explicitly rather than assuming every outlet is in Kolkata.
OUTLET_TZ = ZoneInfo("Asia/Kolkata")


def _tz(timezone: str | tzinfo | None) -> tzinfo:
    if timezone is None:
        return OUTLET_TZ
    if isinstance(timezone, str):
        return ZoneInfo(timezone)
    return timezone


def business_date(ts: datetime, timezone: str | tzinfo | None = None) -> date:
    """Trading date for a timestamp.

    Mirrors the SQL::

        select ((ts at time zone 'Asia/Kolkata') - interval '5 hours')::date

    >>> business_date(datetime(2026, 8, 23, 1, 30, tzinfo=OUTLET_TZ))
    datetime.date(2026, 8, 22)
    >>> business_date(datetime(2026, 8, 23, 6, 0, tzinfo=OUTLET_TZ))
    datetime.date(2026, 8, 23)

    Raises:
        ValueError: if ``ts`` is naive. A naive timestamp here is always a bug —
            it means a UTC value has been mistaken for a local one, or the other
            way round, and the resulting date would be silently wrong.
    """
    if ts.tzinfo is None:
        raise ValueError(
            "business_date() needs an aware datetime. A naive one cannot be "
            "placed on a trading day without guessing its zone."
        )
    local = ts.astimezone(_tz(timezone))
    return (local - timedelta(hours=ROLLOVER_HOUR)).date()


def business_date_bounds(
    on: date, timezone: str | tzinfo | None = None
) -> tuple[datetime, datetime]:
    """The half-open UTC interval ``[start, end)`` covering one trading day.

    Use this to filter by timestamp when a stored ``business_date`` column is
    not available. When one *is* available, filter on it directly — it is
    indexed and needs no arithmetic.
    """
    tz = _tz(timezone)
    start = datetime.combine(on, time(hour=ROLLOVER_HOUR), tzinfo=tz)
    return start, start + timedelta(days=1)


def due_at(on: date, due_time_local: time, timezone: str | tzinfo | None = None) -> datetime:
    """When a checklist scheduled at ``due_time_local`` is due on trading day ``on``.

    A due time before the rollover hour belongs to the *next* calendar day: a
    closing checklist due 00:30 on the trading day of 22 Aug is due at 00:30 on
    23 Aug. Getting this backwards makes every closing checklist look a day
    late, which is the sort of error that quietly destroys trust in the numbers.
    """
    tz = _tz(timezone)
    calendar_day = on + timedelta(days=1) if due_time_local.hour < ROLLOVER_HOUR else on
    return datetime.combine(calendar_day, due_time_local, tzinfo=tz)


def outlet_now(timezone: str | tzinfo | None = None) -> datetime:
    """Current time in an outlet's own timezone."""
    return datetime.now(_tz(timezone))
