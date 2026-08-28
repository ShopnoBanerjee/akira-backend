"""Consumption windows and the three section-6 anomaly checks.

The pure module first — streaks, shares, z-scores, each with the case that
would embarrass it — then the whole nightly pass against a real database with
two planted anomalies, exactly the way the seed rehearsals plant theirs: an
unchanged count and a consistently padded item, both of which the job must
find, and find only ONCE.
"""

import uuid
from datetime import date as date_type

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.domains.inventory import anomalies_service, consumption
from app.domains.inventory.consumption import CountPoint

pytestmark = pytest.mark.asyncio


def point(count_id: str, day: str, qty: float) -> CountPoint:
    return CountPoint(count_id=count_id, business_date=day, qty=qty)


class TestWindows:
    def test_consecutive_counts_pair_up(self) -> None:
        wins = consumption.windows(
            [point("a", "2026-08-01", 1000), point("b", "2026-08-03", 400)],
            {("a", "b"): 500},
        )
        assert len(wins) == 1
        w = wins[0]
        # 1000 on hand + 500 received - 400 left = 1100 used.
        assert w.apparent_consumption == 1100
        assert w.detail["formula"].startswith("from_qty")

    def test_no_requisition_data_means_no_invented_consumption(self) -> None:
        """The receipts stand-in is an assumption, not a default of zero. No
        requisition in the window → apparent stays null, delta still visible."""
        wins = consumption.windows([point("a", "2026-08-01", 1000), point("b", "2026-08-03", 400)])
        assert wins[0].apparent_consumption is None
        assert wins[0].from_qty == 1000 and wins[0].to_qty == 400

    def test_unsorted_input_is_ordered_by_date(self) -> None:
        wins = consumption.windows([point("b", "2026-08-03", 400), point("a", "2026-08-01", 1000)])
        assert wins[0].from_count_id == "a"


class TestUnchangedCount:
    def test_a_streak_of_identical_counts_flags(self) -> None:
        found = consumption.unchanged_count(
            [
                point("a", "2026-08-01", 500),
                point("b", "2026-08-03", 500),
                point("c", "2026-08-05", 500),
            ],
            item_name="Ajino Moto",
            streak=3,
        )
        assert found is not None and found.kind == "unchanged_count"
        assert found.detail["qty"] == 500

    def test_a_repeated_zero_is_a_purchasing_problem_not_a_counting_one(self) -> None:
        found = consumption.unchanged_count(
            [point("a", "2026-08-01", 0), point("b", "2026-08-03", 0), point("c", "2026-08-05", 0)],
            item_name="X",
            streak=3,
        )
        assert found is None

    def test_a_changing_count_never_flags(self) -> None:
        found = consumption.unchanged_count(
            [
                point("a", "2026-08-01", 500),
                point("b", "2026-08-03", 500),
                point("c", "2026-08-05", 400),
            ],
            item_name="X",
            streak=3,
        )
        assert found is None

    def test_too_short_a_history_stays_silent(self) -> None:
        found = consumption.unchanged_count(
            [point("a", "2026-08-01", 500), point("b", "2026-08-03", 500)],
            item_name="X",
            streak=3,
        )
        assert found is None


class TestPaddingConsistent:
    def test_mostly_padded_lines_flag(self) -> None:
        found = consumption.padding_consistent(
            item_name="Sugar", flagged=3, total=4, min_lines=3, share_threshold=0.5
        )
        assert found is not None and found.detail["share"] == 0.75

    def test_below_the_minimum_sample_stays_silent(self) -> None:
        assert (
            consumption.padding_consistent(
                item_name="X", flagged=2, total=2, min_lines=3, share_threshold=0.5
            )
            is None
        )

    def test_occasional_padding_is_not_consistent(self) -> None:
        assert (
            consumption.padding_consistent(
                item_name="X", flagged=1, total=5, min_lines=3, share_threshold=0.5
            )
            is None
        )


class TestConsumptionJump:
    def test_a_real_jump_flags_with_its_working(self) -> None:
        # Stable ~10 g/cover, then triple.
        found = consumption.consumption_jump(
            [10.0, 10.5, 9.8, 10.2, 30.0],
            item_name="Boiled Pork",
            min_windows=5,
            z_threshold=2.5,
        )
        assert found is not None
        assert found.detail["latest_per_cover"] == 30.0
        assert float(found.detail["z"]) > 2.5

    def test_stable_usage_never_flags(self) -> None:
        assert (
            consumption.consumption_jump(
                [10.0, 10.5, 9.8, 10.2, 10.1],
                item_name="X",
                min_windows=5,
                z_threshold=2.5,
            )
            is None
        )

    def test_a_z_score_against_four_points_is_refused(self) -> None:
        assert (
            consumption.consumption_jump(
                [10.0, 10.0, 30.0], item_name="X", min_windows=5, z_threshold=2.5
            )
            is None
        )

    def test_a_flat_history_with_a_move_still_flags(self) -> None:
        """stdev 0 would divide by zero; a genuine move off a flat line is
        infinitely surprising and must not hide behind the arithmetic."""
        found = consumption.consumption_jump(
            [10.0, 10.0, 10.0, 10.0, 25.0],
            item_name="X",
            min_windows=5,
            z_threshold=2.5,
        )
        assert found is not None and found.detail["z"] == "inf"


# ---------------------------------------------------------------------------
# The nightly pass, against a real database
# ---------------------------------------------------------------------------


@pytest.fixture
async def session(migrated_db: str):  # type: ignore[no-untyped-def]
    engine = create_async_engine(migrated_db.replace("postgresql://", "postgresql+asyncpg://"))
    async with AsyncSession(engine, expire_on_commit=False) as db:
        try:
            yield db
        finally:
            await db.rollback()
            for table in (
                "stock_consumption",
                "requisition_lines",
                "requisitions",
                "stock_count_lines",
                "stock_counts",
                "sales_orders",
                "data_uploads",
                "sop_exceptions",
            ):
                await db.execute(text(f"delete from {table}"))
            await db.commit()
    await engine.dispose()


async def _outlet(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(
        str((await db.execute(text("select id from outlets order by code limit 1"))).scalar_one())
    )


async def _profile(db: AsyncSession) -> uuid.UUID:
    pid = uuid.uuid4()
    await db.execute(
        text("insert into auth.users (id, email) values (:id, :e)"),
        {"id": pid, "e": f"{pid}@akira.test"},
    )
    await db.execute(
        text(
            "insert into profiles (id, full_name, global_role, is_active)"
            " values (:id, 'Counter', 'owner', true)"
        ),
        {"id": pid},
    )
    await db.commit()
    return pid


async def _item(db: AsyncSession, name: str) -> uuid.UUID:
    return uuid.UUID(
        str(
            (
                await db.execute(
                    text("select id from inventory_items where name = :n"), {"n": name}
                )
            ).scalar_one()
        )
    )


async def _confirmed_count(
    db: AsyncSession,
    outlet: uuid.UUID,
    confirmer: uuid.UUID,
    day: str,
    quantities: dict[uuid.UUID, float],
) -> uuid.UUID:
    upload = (
        await db.execute(
            text(
                """
                insert into data_uploads
                    (outlet_id, source, original_filename, storage_path, file_sha256, status)
                values (:o, 'stock_sheet', 's.jpg', :p, :sha, 'parsed') returning id
                """
            ),
            {"o": outlet, "p": f"{outlet}/x.jpg", "sha": f"sha-{uuid.uuid4()}"},
        )
    ).scalar_one()
    count = (
        await db.execute(
            text(
                """
                insert into stock_counts
                    (outlet_id, upload_id, business_date, status, confirmed_by, confirmed_at)
                values (:o, :u, :d, 'confirmed', :by, now()) returning id
                """
            ),
            {"o": outlet, "u": upload, "d": date_type.fromisoformat(day), "by": confirmer},
        )
    ).scalar_one()
    for sl, (item_id, qty) in enumerate(quantities.items(), start=1):
        await db.execute(
            text(
                """
                insert into stock_count_lines
                    (count_id, page, sl_no, raw_name, item_id, match_method, qty, needs_review)
                values (:c, 1, :sl, 'seed', :item, 'human', :qty, false)
                """
            ),
            {"c": count, "sl": sl, "item": item_id, "qty": qty},
        )
    await db.commit()
    return uuid.UUID(str(count))


class TestTheNightlyPass:
    async def test_windows_are_written_and_the_plants_are_found_once(
        self, session: AsyncSession
    ) -> None:
        outlet = await _outlet(session)
        confirmer = await _profile(session)
        moving = await _item(session, "Sweet Corn")
        frozen = await _item(session, "Ajino Moto")  # the planted unchanged count

        # Three confirmed counts: Sweet Corn moves, Ajino Moto never does.
        days = ["2026-08-20", "2026-08-22", "2026-08-24"]
        counts = [
            await _confirmed_count(session, outlet, confirmer, day, {moving: qty, frozen: 750})
            for day, qty in zip(days, [2000, 1400, 900], strict=True)
        ]
        assert len(counts) == 3

        result = await anomalies_service.run(session)
        # Two items x two windows each.
        assert result["windows"] == 4
        assert "Count never changes: Ajino Moto" in result["raised"]
        assert not any("Sweet Corn" in title for title in result["raised"])

        # The windows carry honest nulls: no finalised requisitions exist, so
        # apparent consumption is null while the count delta is recorded.
        row = (
            (
                await session.execute(
                    text(
                        """
                        select cast(from_qty as float8) f, cast(to_qty as float8) t,
                               apparent_consumption
                          from stock_consumption
                         where item_id = :i order by to_date limit 1
                        """
                    ),
                    {"i": moving},
                )
            )
            .mappings()
            .one()
        )
        assert row["f"] == 2000 and row["t"] == 1400
        assert row["apparent_consumption"] is None

        # Second run: idempotent — windows upsert, the open finding is not
        # raised again.
        again = await anomalies_service.run(session)
        assert again["raised"] == []
        assert again["skipped_open"] >= 1
        open_count = (
            await session.execute(
                text(
                    "select count(*) from sop_exceptions"
                    " where title = 'Count never changes: Ajino Moto'"
                )
            )
        ).scalar_one()
        assert open_count == 1

    async def test_consistent_padding_reaches_the_exception_board(
        self, session: AsyncSession
    ) -> None:
        outlet = await _outlet(session)
        confirmer = await _profile(session)
        padded = await _item(session, "Sugar")

        # Two counts so the item participates at all, plus three finalised
        # requisitions whose lines carry the padding flag.
        await _confirmed_count(session, outlet, confirmer, "2026-08-20", {padded: 3000})
        count2 = await _confirmed_count(session, outlet, confirmer, "2026-08-24", {padded: 2500})
        assert count2
        for day in ("2026-08-21", "2026-08-22", "2026-08-23"):
            first_count = (
                await session.execute(
                    text("select id from stock_counts where outlet_id = :o limit 1"),
                    {"o": outlet},
                )
            ).scalar_one()
            req = (
                await session.execute(
                    text(
                        """
                        insert into requisitions
                            (outlet_id, count_id, business_date, status, created_by,
                             finalised_by, finalised_at)
                        values (:o, :c, :d, 'final', :by, :by, now())
                        returning id
                        """
                    ),
                    {
                        "o": outlet,
                        "c": first_count,
                        "d": date_type.fromisoformat(day),
                        "by": confirmer,
                    },
                )
            ).scalar_one()
            await session.execute(
                text(
                    """
                    insert into requisition_lines
                        (requisition_id, item_id, suggested_qty, requested_qty,
                         final_qty, flags)
                    values (:r, :i, 200, 3000, 3000, '{padding}')
                    """
                ),
                {"r": req, "i": padded},
            )
        await session.commit()

        result = await anomalies_service.run(session)
        assert "Requisitions consistently above need: Sugar" in result["raised"]
        # And the window now has a receipts stand-in, so apparent consumption
        # is computable: 3000 + 9000 requisitioned - 2500 = 9500.
        apparent = (
            await session.execute(
                text(
                    "select cast(apparent_consumption as float8) from stock_consumption"
                    " where item_id = :i"
                ),
                {"i": padded},
            )
        ).scalar_one()
        assert apparent == 9500
