"""Feeding, storing and scoring the forecast baseline.

Three jobs, kept apart on purpose:

- **compute** reads trading history and events and calls the pure model.
  The same function serves the API's live view and the nightly job, so the
  number a manager sees is the number the job stores.
- **store** writes forecast rows that are never updated. The unique key is
  (outlet, target_date, made_on, model): tomorrow's forecast made today and
  the same day's forecast made three days ago are BOTH kept, each at its
  own horizon. History does not get rewritten by a better-informed later
  guess.
- **accuracy** joins what was stored against what actually traded. MAPE is
  reported at horizon 1 (the operational "what do I prep tomorrow" claim)
  and overall, per the spec's "report MAPE weekly".
"""

import asyncio
import json
import logging
import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.business_date import business_date as to_business_date
from app.core.business_date import outlet_now
from app.core.db import read_with
from app.core.settings_value import resolve_many
from app.domains.sales import forecast as model

logger = logging.getLogger(__name__)

_SETTING_KEYS = [
    "forecast.horizon_days",
    "forecast.trend_clamp_min",
    "forecast.trend_clamp_max",
]

#: How much history the model reads. WEEKDAY_WINDOW Saturdays needs about
#: five weeks; twelve gives the trend windows room and stays cheap.
LOOKBACK_DAYS = 12 * 7


async def daily_history(
    db: AsyncSession, outlet_id: uuid.UUID, *, as_of: date
) -> list[model.DayActual]:
    rows = (
        (
            await db.execute(
                text(
                    """
                    select business_date,
                           coalesce(sum(net_paise), 0) as net_paise,
                           sum(covers) as covers
                      from sales_orders
                     where outlet_id = :o
                       and business_date > cast(:start as date)
                       and business_date <= cast(:end as date)
                     group by business_date
                     order by business_date
                    """
                ),
                {
                    "o": outlet_id,
                    "start": as_of - timedelta(days=LOOKBACK_DAYS),
                    "end": as_of,
                },
            )
        )
        .mappings()
        .all()
    )
    return [
        model.DayActual(
            business_date=r["business_date"],
            net_paise=int(r["net_paise"]),
            covers=int(r["covers"]) if r["covers"] is not None else None,
        )
        for r in rows
    ]


async def events_for(
    db: AsyncSession, outlet_id: uuid.UUID, *, start: date, end: date
) -> dict[date, dict[str, Any]]:
    """The event multiplier per date. An outlet-specific flag beats a
    group-wide one; within a scope the most recent entry wins — the manager
    who corrected the multiplier meant the correction."""
    rows = (
        (
            await db.execute(
                text(
                    """
                    select event_date, cast(multiplier as float8) as multiplier,
                           label, outlet_id, created_at
                      from forecast_events
                     where (outlet_id = :o or outlet_id is null)
                       and event_date between :start and :end
                     order by event_date,
                              (outlet_id is null),  -- outlet-specific first
                              created_at desc
                    """
                ),
                {"o": outlet_id, "start": start, "end": end},
            )
        )
        .mappings()
        .all()
    )
    out: dict[date, dict[str, Any]] = {}
    for r in rows:
        if r["event_date"] not in out:
            out[r["event_date"]] = {"multiplier": float(r["multiplier"]), "label": r["label"]}
    return out


async def compute(
    db: AsyncSession, outlet_id: uuid.UUID, *, as_of: date, horizon: int | None = None
) -> list[model.Forecast]:
    """Settings, history and event flags are three independent reads. When
    the caller names the horizon — the live endpoint does — all three go on
    the wire together. The nightly job leaves it to the setting, so it has to
    read that first; it runs at 05:30 and nobody is waiting on it."""
    if horizon is None:
        settings = await resolve_many(db, _SETTING_KEYS, outlet_id=outlet_id)
        days = int(settings["forecast.horizon_days"])
        targets = [as_of + timedelta(days=n) for n in range(1, days + 1)]
        history = await daily_history(db, outlet_id, as_of=as_of)
        events = await events_for(db, outlet_id, start=targets[0], end=targets[-1])
    else:
        days = horizon
        targets = [as_of + timedelta(days=n) for n in range(1, days + 1)]
        settings, history, events = await asyncio.gather(
            read_with(db, resolve_many, _SETTING_KEYS, outlet_id=outlet_id),
            read_with(db, daily_history, outlet_id, as_of=as_of),
            read_with(db, events_for, outlet_id, start=targets[0], end=targets[-1]),
        )
    out = []
    for target in targets:
        event = events.get(target, {})
        out.append(
            model.forecast_day(
                history,
                target=target,
                as_of=as_of,
                event_multiplier=float(event.get("multiplier", 1.0)),
                event_label=event.get("label"),
                clamp_min=float(settings["forecast.trend_clamp_min"]),
                clamp_max=float(settings["forecast.trend_clamp_max"]),
            )
        )
    return out


async def store(
    db: AsyncSession,
    outlet_id: uuid.UUID,
    forecasts: list[model.Forecast],
    *,
    made_on: date,
) -> int:
    """Write the rows that MAPE will be scored against. `on conflict do
    nothing`: a forecast already made for this (target, made_on, model)
    stands — running the job twice must not launder a revision in."""
    written = 0
    for f in forecasts:
        if f.net_paise is None:
            continue
        result = await db.execute(
            text(
                """
                insert into sales_forecasts
                    (outlet_id, target_date, made_on, model,
                     forecast_net_paise, forecast_covers, components)
                values (:o, :target, :made, :model, :net, :covers,
                        cast(:components as jsonb))
                on conflict (outlet_id, target_date, made_on, model) do nothing
                """
            ),
            {
                "o": outlet_id,
                "target": f.target_date,
                "made": made_on,
                "model": model.MODEL,
                "net": f.net_paise,
                "covers": f.covers,
                "components": json.dumps(f.components),
            },
        )
        written += int(getattr(result, "rowcount", 0) or 0)
    return written


async def run(db: AsyncSession) -> dict[str, Any]:
    """The nightly pass: forecast the horizon for every active outlet and
    store what was predicted. Runs after the 05:00 materialisation, when
    yesterday's trading day has closed and is part of the history."""
    outlets = [
        uuid.UUID(str(r[0]))
        for r in await db.execute(
            text("select id from outlets where is_active and deleted_at is null")
        )
    ]
    made_on = to_business_date(outlet_now())
    totals: dict[str, Any] = {"outlets": len(outlets), "stored": 0, "refused": 0}
    for outlet_id in outlets:
        forecasts = await compute(db, outlet_id, as_of=made_on)
        totals["stored"] += await store(db, outlet_id, forecasts, made_on=made_on)
        totals["refused"] += sum(1 for f in forecasts if f.net_paise is None)
    await db.commit()
    return totals


async def accuracy(db: AsyncSession, outlet_id: uuid.UUID, *, weeks: int = 8) -> dict[str, Any]:
    """Stored forecasts against what actually traded. Only days that have
    both a stored forecast and an actual count; a day the outlet never
    opened scores nothing rather than infinity."""
    rows = (
        (
            await db.execute(
                text(
                    """
                    with actuals as (
                        select business_date, sum(net_paise) as net
                          from sales_orders
                         where outlet_id = :o
                         group by business_date
                    )
                    select f.target_date, f.made_on,
                           (f.target_date - f.made_on) as horizon,
                           f.forecast_net_paise, a.net as actual_net_paise
                      from sales_forecasts f
                      join actuals a on a.business_date = f.target_date
                     where f.outlet_id = :o and f.model = :model
                       and f.target_date >= :since
                       and a.net > 0
                     order by f.target_date
                    """
                ),
                {
                    "o": outlet_id,
                    "model": model.MODEL,
                    "since": to_business_date(outlet_now()) - timedelta(weeks=weeks),
                },
            )
        )
        .mappings()
        .all()
    )

    def mape(subset: list[Any]) -> float | None:
        if not subset:
            return None
        return round(
            100
            * sum(
                abs(int(r["forecast_net_paise"]) - int(r["actual_net_paise"]))
                / int(r["actual_net_paise"])
                for r in subset
            )
            / len(subset),
            1,
        )

    day_ahead = [r for r in rows if int(r["horizon"]) == 1]
    return {
        "model": model.MODEL,
        "weeks": weeks,
        "scored_days": len(rows),
        "mape_all_horizons": mape(list(rows)),
        "mape_day_ahead": mape(day_ahead),
        "day_ahead_days": len(day_ahead),
        "recent": [
            {
                "target_date": str(r["target_date"]),
                "horizon": int(r["horizon"]),
                "forecast_net_paise": int(r["forecast_net_paise"]),
                "actual_net_paise": int(r["actual_net_paise"]),
            }
            for r in list(rows)[-14:]
        ],
    }


# ---------------------------------------------------------------------------
# Event flags — the manual override the spec's formula names
# ---------------------------------------------------------------------------


async def list_events(
    db: AsyncSession, outlet_id: uuid.UUID, *, start: date, end: date
) -> list[dict[str, Any]]:
    rows = (
        (
            await db.execute(
                text(
                    """
                    select e.id, e.outlet_id, e.event_date,
                           cast(e.multiplier as float8) as multiplier, e.label,
                           p.full_name as created_by_name, e.created_at
                      from forecast_events e
                      left join profiles p on p.id = e.created_by
                     where (e.outlet_id = :o or e.outlet_id is null)
                       and e.event_date between :start and :end
                     order by e.event_date, e.created_at
                    """
                ),
                {"o": outlet_id, "start": start, "end": end},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def create_event(
    db: AsyncSession,
    *,
    outlet_id: uuid.UUID | None,
    event_date: date,
    multiplier: float,
    label: str,
    created_by: uuid.UUID,
) -> dict[str, Any]:
    row = (
        (
            await db.execute(
                text(
                    """
                    insert into forecast_events
                        (outlet_id, event_date, multiplier, label, created_by)
                    values (:o, :d, :m, :label, :by)
                    returning id, outlet_id, event_date,
                              cast(multiplier as float8) as multiplier, label, created_at
                    """
                ),
                {
                    "o": outlet_id,
                    "d": event_date,
                    "m": multiplier,
                    "label": label,
                    "by": created_by,
                },
            )
        )
        .mappings()
        .one()
    )
    await db.commit()
    return dict(row)


async def delete_event(db: AsyncSession, event_id: uuid.UUID) -> bool:
    result = await db.execute(text("delete from forecast_events where id = :id"), {"id": event_id})
    await db.commit()
    return bool(getattr(result, "rowcount", 0))
