"""Recipes, theoretical consumption, and the fourth anomaly.

The chain under test: item-day sales x recipe lines -> what a window SHOULD
have used, written beside what the counts say it did — and the zero-sales
finding that only fires when the arithmetic was actually possible. The
embarrassing cases: no sales data read as zero sales, an unmapped item read
as zero usage, and a recipe silently keeping a line its correction removed.
"""

import uuid
from datetime import date as date_type

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.errors import ValidationError
from app.domains.inventory import anomalies_service, recipes_service
from app.domains.inventory.consumption import zero_sales_consumption

pytestmark = pytest.mark.asyncio


class TestZeroSalesPure:
    def test_usage_against_computed_zero_sales_flags(self) -> None:
        found = zero_sales_consumption(item_name="Boiled Pork", apparent=900.0, theoretical=0.0)
        assert found is not None and found.kind == "zero_sales_consumption"

    def test_uncomputable_theoretical_is_a_gap_not_an_anomaly(self) -> None:
        assert zero_sales_consumption(item_name="X", apparent=900.0, theoretical=None) is None

    def test_matched_sales_never_flag(self) -> None:
        assert zero_sales_consumption(item_name="X", apparent=900.0, theoretical=850.0) is None

    def test_no_drawdown_never_flags(self) -> None:
        assert zero_sales_consumption(item_name="X", apparent=0.0, theoretical=0.0) is None


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
                "recipe_lines",
                "recipes",
                "sales_item_days",
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
            " values (:id, 'Recipe Admin', 'owner', true)"
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
) -> None:
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


async def _sold(db: AsyncSession, outlet: uuid.UUID, day: str, name: str, qty: float) -> None:
    await db.execute(
        text(
            """
            insert into sales_item_days (outlet_id, report_date, item_name, qty, net_paise)
            values (:o, cast(:d as date), :n, :q, 0)
            on conflict (outlet_id, report_date, item_name) do update set qty = excluded.qty
            """
        ),
        # A str here trips asyncpg's toordinal — the date must be a date.
        {"o": outlet, "d": date_type.fromisoformat(day), "n": name, "q": qty},
    )
    await db.commit()


class TestRecipeCrud:
    async def test_save_replaces_lines_wholesale(self, session: AsyncSession) -> None:
        """A corrected recipe must also REMOVE the ingredient it no longer
        uses — keeping it would quietly inflate theoretical usage forever."""
        who = await _profile(session)
        pork = await _item(session, "Boiled Pork")
        corn = await _item(session, "Sweet Corn")

        await recipes_service.save_recipe(
            session,
            menu_item_name="Akira Shoyu Ramen (pork)",
            lines=[
                {"item_id": pork, "qty_per_unit": 120},
                {"item_id": corn, "qty_per_unit": 30},
            ],
            notes=None,
            is_active=True,
            created_by=who,
        )
        await session.commit()
        await recipes_service.save_recipe(
            session,
            menu_item_name="Akira Shoyu Ramen (pork)",
            lines=[{"item_id": pork, "qty_per_unit": 140}],
            notes="corrected",
            is_active=True,
            created_by=who,
        )
        await session.commit()

        recipes = await recipes_service.list_recipes(session)
        assert len(recipes) == 1
        lines = recipes[0]["lines"]
        assert len(lines) == 1
        assert lines[0]["qty_per_unit"] == 140.0

    async def test_a_recipe_with_no_lines_is_refused(self, session: AsyncSession) -> None:
        who = await _profile(session)
        with pytest.raises(ValidationError, match="at least one"):
            await recipes_service.save_recipe(
                session,
                menu_item_name="Empty Dish",
                lines=[],
                notes=None,
                is_active=True,
                created_by=who,
            )

    async def test_unmapped_lists_sold_names_without_recipes(self, session: AsyncSession) -> None:
        who = await _profile(session)
        outlet = await _outlet(session)
        pork = await _item(session, "Boiled Pork")
        await _sold(session, outlet, "2026-08-21", "Mapped Ramen", 30)
        await _sold(session, outlet, "2026-08-21", "Unmapped Special", 2)
        await recipes_service.save_recipe(
            session,
            menu_item_name="Mapped Ramen",
            lines=[{"item_id": pork, "qty_per_unit": 100}],
            notes=None,
            is_active=True,
            created_by=who,
        )
        await session.commit()

        names = [r["item_name"] for r in await recipes_service.unmapped_names(session)]
        assert "Unmapped Special" in names
        assert "Mapped Ramen" not in names


class TestTheoreticalConsumption:
    async def test_theoretical_lands_beside_apparent_and_matches_the_recipe(
        self, session: AsyncSession
    ) -> None:
        outlet = await _outlet(session)
        who = await _profile(session)
        pork = await _item(session, "Boiled Pork")

        await recipes_service.save_recipe(
            session,
            menu_item_name="Shoyu Ramen",
            lines=[{"item_id": pork, "qty_per_unit": 100}],
            notes=None,
            is_active=True,
            created_by=who,
        )
        await session.commit()
        # Window 2026-08-20 -> 2026-08-23, with sales on two days inside it:
        # (5 + 7) units x 100 g = 1200 g theoretical.
        await _confirmed_count(session, outlet, who, "2026-08-20", {pork: 3000})
        await _confirmed_count(session, outlet, who, "2026-08-23", {pork: 1700})
        await _sold(session, outlet, "2026-08-21", "Shoyu Ramen", 5)
        await _sold(session, outlet, "2026-08-22", "Shoyu Ramen", 7)

        await anomalies_service.run(session)

        row = (
            (
                await session.execute(
                    text(
                        "select cast(theoretical_qty as float8) t,"
                        " apparent_consumption from stock_consumption where item_id = :i"
                    ),
                    {"i": pork},
                )
            )
            .mappings()
            .one()
        )
        assert row["t"] == 1200.0
        # No finalised requisitions -> apparent stays honestly null even
        # while theoretical is computable. The two are independent claims.
        assert row["apparent_consumption"] is None

    async def test_no_item_day_sales_means_null_not_zero(self, session: AsyncSession) -> None:
        """The outlet sold plenty — but no Item Day Wise export was uploaded.
        Theoretical must be NULL, or every item would look like staff meals."""
        outlet = await _outlet(session)
        who = await _profile(session)
        pork = await _item(session, "Boiled Pork")
        await recipes_service.save_recipe(
            session,
            menu_item_name="Shoyu Ramen",
            lines=[{"item_id": pork, "qty_per_unit": 100}],
            notes=None,
            is_active=True,
            created_by=who,
        )
        await session.commit()
        await _confirmed_count(session, outlet, who, "2026-08-20", {pork: 3000})
        await _confirmed_count(session, outlet, who, "2026-08-23", {pork: 1700})

        result = await anomalies_service.run(session)
        theoretical = (
            await session.execute(
                text("select theoretical_qty from stock_consumption where item_id = :i"),
                {"i": pork},
            )
        ).scalar_one()
        assert theoretical is None
        assert not any("no matching sales" in t for t in result["raised"])

    async def test_an_unmapped_item_gets_null_not_zero(self, session: AsyncSession) -> None:
        """Sales data exists, but no recipe mentions Sweet Corn. Its windows
        must say "cannot compute", not "sold nothing"."""
        outlet = await _outlet(session)
        who = await _profile(session)
        pork = await _item(session, "Boiled Pork")
        corn = await _item(session, "Sweet Corn")
        await recipes_service.save_recipe(
            session,
            menu_item_name="Shoyu Ramen",
            lines=[{"item_id": pork, "qty_per_unit": 100}],
            notes=None,
            is_active=True,
            created_by=who,
        )
        await session.commit()
        await _confirmed_count(session, outlet, who, "2026-08-20", {corn: 2000})
        await _confirmed_count(session, outlet, who, "2026-08-23", {corn: 900})
        await _sold(session, outlet, "2026-08-21", "Shoyu Ramen", 5)

        result = await anomalies_service.run(session)
        theoretical = (
            await session.execute(
                text("select theoretical_qty from stock_consumption where item_id = :i"),
                {"i": corn},
            )
        ).scalar_one()
        assert theoretical is None
        assert not any("Sweet Corn" in t for t in result["raised"])

    async def test_usage_with_zero_matching_sales_reaches_the_board(
        self, session: AsyncSession
    ) -> None:
        """Pork left the shelf, pork dishes sold nothing, and a finalised
        requisition makes the drawdown computable: the fourth spec anomaly."""
        outlet = await _outlet(session)
        who = await _profile(session)
        pork = await _item(session, "Boiled Pork")
        await recipes_service.save_recipe(
            session,
            menu_item_name="Shoyu Ramen",
            lines=[{"item_id": pork, "qty_per_unit": 100}],
            notes=None,
            is_active=True,
            created_by=who,
        )
        await session.commit()
        await _confirmed_count(session, outlet, who, "2026-08-20", {pork: 3000})
        await _confirmed_count(session, outlet, who, "2026-08-23", {pork: 2100})
        # Sales data covers the window, but only for a dish without pork.
        await _sold(session, outlet, "2026-08-21", "Veg Ramen", 9)
        # A finalised requisition inside the window makes apparent computable.
        first_count = (
            await session.execute(
                text("select id from stock_counts where outlet_id = :o limit 1"), {"o": outlet}
            )
        ).scalar_one()
        req = (
            await session.execute(
                text(
                    """
                    insert into requisitions
                        (outlet_id, count_id, business_date, status, created_by,
                         finalised_by, finalised_at)
                    values (:o, :c, cast(:d as date), 'final', :by, :by, now()) returning id
                    """
                ),
                {
                    "o": outlet,
                    "c": first_count,
                    "d": date_type.fromisoformat("2026-08-21"),
                    "by": who,
                },
            )
        ).scalar_one()
        await session.execute(
            text(
                """
                insert into requisition_lines
                    (requisition_id, item_id, suggested_qty, requested_qty, final_qty, flags)
                values (:r, :i, 0, 0, 0, '{}')
                """
            ),
            {"r": req, "i": pork},
        )
        await session.commit()

        result = await anomalies_service.run(session)
        assert "Used with no matching sales: Boiled Pork" in result["raised"]
