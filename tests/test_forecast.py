"""The forecasting baseline (spec 5.1). Pure model first, then the store.

The cases that would embarrass it: a median of one Saturday, a trend
computed from three days, a forecast that peeks at the future, a job run
twice rewriting what was predicted, and an event multiplier applied to the
wrong outlet.
"""

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.domains.sales import forecast_service
from app.domains.sales.forecast import DayActual, forecast_day, trend_factor

pytestmark = pytest.mark.asyncio

AS_OF = date(2026, 8, 28)  # a Friday


def day(iso: str, net: int, covers: int | None = None) -> DayActual:
    return DayActual(business_date=date.fromisoformat(iso), net_paise=net, covers=covers)


def weeks_of(weekday_iso: str, nets: list[int]) -> list[DayActual]:
    """The same weekday going back, newest last."""
    start = date.fromisoformat(weekday_iso)
    return [
        DayActual(
            business_date=start - timedelta(weeks=len(nets) - 1 - i),
            net_paise=net,
            covers=None,
        )
        for i, net in enumerate(nets)
    ]


class TestTrendFactor:
    def history(self, recent_per_day: int, prior_per_day: int) -> list[DayActual]:
        out = []
        for offset in range(28):
            d = AS_OF - timedelta(days=offset)
            out.append(DayActual(d, recent_per_day if offset < 14 else prior_per_day, None))
        return out

    def test_flat_trading_is_a_factor_of_one(self) -> None:
        factor, _ = trend_factor(
            self.history(10_000, 10_000), as_of=AS_OF, clamp_min=0.8, clamp_max=1.3
        )
        assert factor == 1.0

    def test_growth_is_clamped_at_the_ceiling(self) -> None:
        """A festival fortnight must not double next week's forecast."""
        factor, working = trend_factor(
            self.history(20_000, 10_000), as_of=AS_OF, clamp_min=0.8, clamp_max=1.3
        )
        assert factor == 1.3
        assert working["trend_raw"] == 2.0
        assert "clamped" in working["trend_note"]

    def test_collapse_is_clamped_at_the_floor(self) -> None:
        factor, _ = trend_factor(
            self.history(4_000, 10_000), as_of=AS_OF, clamp_min=0.8, clamp_max=1.3
        )
        assert factor == 0.8

    def test_a_thin_window_holds_at_one_and_says_so(self) -> None:
        """Three recent days against twelve prior is noise dressed as
        momentum — the factor refuses to move."""
        thin = [DayActual(AS_OF - timedelta(days=o), 10_000, None) for o in (0, 1, 2, 15, 16, 17)]
        factor, working = trend_factor(thin, as_of=AS_OF, clamp_min=0.8, clamp_max=1.3)
        assert factor == 1.0
        assert "too thin" in working["trend_note"]

    def test_the_trend_is_per_trading_day_not_raw_sums(self) -> None:
        """Twelve recent trading days against six prior ones, all at the same
        daily rate, is NOT growth — it is more open days."""
        history = [
            *(DayActual(AS_OF - timedelta(days=o), 10_000, None) for o in range(12)),
            *(DayActual(AS_OF - timedelta(days=14 + o), 10_000, None) for o in range(7)),
        ]
        factor, _ = trend_factor(history, as_of=AS_OF, clamp_min=0.8, clamp_max=1.3)
        assert factor == 1.0


class TestForecastDay:
    def test_the_median_of_four_same_weekdays(self) -> None:
        # Four Fridays: 10k, 12k, 11k, 30k (one festival outlier). The median
        # shrugs the outlier off — that is why it is a median.
        history = weeks_of("2026-08-28", [10_000, 12_000, 11_000, 30_000])
        result = forecast_day(history, target=date(2026, 9, 4), as_of=AS_OF)
        assert result.net_paise == 11_500
        assert result.components["sample_dates"] == [
            "2026-08-07",
            "2026-08-14",
            "2026-08-21",
            "2026-08-28",
        ]

    def test_one_weekday_in_history_is_a_refusal_not_a_forecast(self) -> None:
        history = weeks_of("2026-08-28", [10_000])
        result = forecast_day(history, target=date(2026, 9, 4), as_of=AS_OF)
        assert result.net_paise is None
        assert result.reason is not None and "1 Friday" in result.reason

    def test_it_never_peeks_past_as_of(self) -> None:
        """A Friday AFTER as_of must not join the sample, however real the
        row looks — forecasting tomorrow with tomorrow is not forecasting."""
        history = [
            *weeks_of("2026-08-28", [10_000, 10_000, 10_000]),
            day("2026-09-04", 99_000),  # the future
        ]
        result = forecast_day(history, target=date(2026, 9, 4), as_of=AS_OF)
        assert result.net_paise == 10_000
        assert "2026-09-04" not in result.components["sample_dates"]

    def test_the_event_multiplier_applies_and_is_recorded(self) -> None:
        history = weeks_of("2026-08-28", [10_000, 10_000, 10_000])
        result = forecast_day(
            history,
            target=date(2026, 9, 4),
            as_of=AS_OF,
            event_multiplier=1.3,
            event_label="Durga Puja",
        )
        assert result.net_paise == 13_000
        assert result.components["event_multiplier"] == 1.3
        assert result.components["event_label"] == "Durga Puja"

    def test_covers_stay_null_on_a_patchy_covers_history(self) -> None:
        """The real Petpooja data records covers on some days only. One
        sample day with covers is not a covers forecast."""
        history = weeks_of("2026-08-28", [10_000, 10_000, 10_000])
        history = [
            DayActual(d.business_date, d.net_paise, 40 if i == 2 else None)
            for i, d in enumerate(history)
        ]
        result = forecast_day(history, target=date(2026, 9, 4), as_of=AS_OF)
        assert result.net_paise == 10_000
        assert result.covers is None
        assert "covers" in result.components["covers_note"]

    def test_dense_covers_forecast_alongside_net(self) -> None:
        history = weeks_of("2026-08-28", [10_000, 10_000, 10_000])
        history = [DayActual(d.business_date, d.net_paise, 40) for d in history]
        result = forecast_day(history, target=date(2026, 9, 4), as_of=AS_OF)
        assert result.covers == 40


# ---------------------------------------------------------------------------
# The store and the accuracy join, against a real database
# ---------------------------------------------------------------------------


@pytest.fixture
async def session(migrated_db: str):  # type: ignore[no-untyped-def]
    engine = create_async_engine(migrated_db.replace("postgresql://", "postgresql+asyncpg://"))
    async with AsyncSession(engine, expire_on_commit=False) as db:
        try:
            yield db
        finally:
            await db.rollback()
            for table in ("sales_forecasts", "forecast_events", "sales_orders", "data_uploads"):
                await db.execute(text(f"delete from {table}"))
            await db.commit()
    await engine.dispose()


async def _outlet(db: AsyncSession, offset: int = 0) -> uuid.UUID:
    return uuid.UUID(
        str(
            (
                await db.execute(
                    text("select id from outlets order by code offset :n limit 1"),
                    {"n": offset},
                )
            ).scalar_one()
        )
    )


async def _trade(db: AsyncSession, outlet: uuid.UUID, iso: str, net: int) -> None:
    upload = (
        await db.execute(
            text(
                """
                insert into data_uploads
                    (outlet_id, source, original_filename, storage_path, file_sha256, status)
                values (:o, 'petpooja_orders', 'e.xlsx', :p, :sha, 'parsed') returning id
                """
            ),
            {"o": outlet, "p": f"{outlet}/{uuid.uuid4()}.xlsx", "sha": f"sha-{uuid.uuid4()}"},
        )
    ).scalar_one()
    await db.execute(
        text(
            """
            insert into sales_orders
                (outlet_id, upload_id, external_bill_no, business_date, ordered_at,
                 gross_paise, discount_paise, tax_paise, net_paise)
            values (:o, :u, :no, cast(:d as date),
                    (cast(:d as date) + interval '20 hours') at time zone 'Asia/Kolkata',
                    :net, 0, 0, :net)
            """
        ),
        {
            "o": outlet,
            "u": upload,
            "no": str(uuid.uuid4())[:12],
            "d": date.fromisoformat(iso),
            "net": net,
        },
    )
    await db.commit()


class TestStoreAndAccuracy:
    async def test_stored_forecasts_are_immutable_across_reruns(
        self, session: AsyncSession
    ) -> None:
        """Running the job twice on the same morning must not rewrite what
        was predicted — that is the whole point of storing it."""
        outlet = await _outlet(session)
        from app.domains.sales.forecast import Forecast

        made = date(2026, 8, 28)
        first = Forecast(
            target_date=date(2026, 8, 29),
            net_paise=10_000,
            covers=None,
            reason=None,
            components={"model": "baseline.v1"},
        )
        assert await forecast_service.store(session, outlet, [first], made_on=made) == 1
        await session.commit()

        revised = Forecast(
            target_date=date(2026, 8, 29),
            net_paise=99_000,
            covers=None,
            reason=None,
            components={},
        )
        assert await forecast_service.store(session, outlet, [revised], made_on=made) == 0
        await session.commit()

        stored = (
            await session.execute(
                text("select forecast_net_paise from sales_forecasts where outlet_id = :o"),
                {"o": outlet},
            )
        ).scalar_one()
        assert int(stored) == 10_000

    async def test_a_refusal_stores_nothing(self, session: AsyncSession) -> None:
        outlet = await _outlet(session)
        from app.domains.sales.forecast import Forecast

        refusal = Forecast(
            target_date=date(2026, 8, 29),
            net_paise=None,
            covers=None,
            reason="too thin",
        )
        assert (
            await forecast_service.store(session, outlet, [refusal], made_on=date(2026, 8, 28)) == 0
        )

    async def test_accuracy_scores_only_days_with_both_sides(self, session: AsyncSession) -> None:
        outlet = await _outlet(session)
        from app.domains.sales.forecast import MODEL, Forecast

        # Two stored day-ahead forecasts; only one target day actually traded.
        for target, net in ((date(2026, 8, 27), 10_000), (date(2026, 8, 28), 12_000)):
            await forecast_service.store(
                session,
                outlet,
                [
                    Forecast(
                        target_date=target,
                        net_paise=net,
                        covers=None,
                        reason=None,
                        components={"model": MODEL},
                    )
                ],
                made_on=target - timedelta(days=1),
            )
        await session.commit()
        await _trade(session, outlet, "2026-08-27", 8_000)

        result = await forecast_service.accuracy(session, outlet, weeks=8)
        assert result["scored_days"] == 1
        # |10000 - 8000| / 8000 = 25%
        assert result["mape_day_ahead"] == 25.0
        assert result["day_ahead_days"] == 1

    async def test_an_outlet_event_beats_a_group_event(self, session: AsyncSession) -> None:
        outlet = await _outlet(session)
        target = date(2026, 9, 5)
        await session.execute(
            text(
                "insert into forecast_events (outlet_id, event_date, multiplier, label)"
                " values (null, :d, 1.5, 'group holiday'), (:o, :d, 1.1, 'outlet quiet')"
            ),
            {"o": outlet, "d": target},
        )
        await session.commit()
        events = await forecast_service.events_for(session, outlet, start=target, end=target)
        assert events[target]["multiplier"] == 1.1
        assert events[target]["label"] == "outlet quiet"

    async def test_another_outlets_event_is_invisible(self, session: AsyncSession) -> None:
        mine = await _outlet(session, 0)
        theirs = await _outlet(session, 1)
        target = date(2026, 9, 5)
        await session.execute(
            text(
                "insert into forecast_events (outlet_id, event_date, multiplier, label)"
                " values (:o, :d, 2.0, 'their festival')"
            ),
            {"o": theirs, "d": target},
        )
        await session.commit()
        events = await forecast_service.events_for(session, mine, start=target, end=target)
        assert events == {}
