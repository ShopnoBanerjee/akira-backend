"""Menu item aliases (0023): a bill's spelling resolves to the menu item's
category in the measured attach rate, one spelling maps to one item, and the
unmapped list forgets a name the moment it is mapped.

Against a real database, because the whole point is the join.
"""

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.deps import CurrentUser
from app.core.enums import UserRole
from app.core.errors import ConflictError, NotFoundError
from app.domains.sales import service
from tests.conftest import DEV_ORG, dev_outlet_ids

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session(migrated_db: str):  # type: ignore[no-untyped-def]
    engine = create_async_engine(migrated_db.replace("postgresql://", "postgresql+asyncpg://"))
    async with AsyncSession(engine, expire_on_commit=False) as db:
        try:
            yield db
        finally:
            await db.rollback()
            for table in (
                "menu_item_aliases",
                "sales_order_items",
                "sales_category_periods",
                "menu_items",
            ):
                await db.execute(text(f"delete from {table}"))
            await db.execute(text("delete from sales_orders where external_bill_no like 'ALIAS-%'"))
            await db.execute(text("delete from audit_log where entity_table = 'menu_item_aliases'"))
            await db.execute(text("delete from profiles where full_name = 'Alias Test Owner'"))
            await db.execute(text("delete from auth.users where email like 'alias-%@akira.test'"))
            await db.commit()
    await engine.dispose()


async def _owner(db: AsyncSession) -> CurrentUser:
    pid = uuid.uuid4()
    await db.execute(
        text("insert into auth.users (id, email) values (:id, :e)"),
        {"id": pid, "e": f"alias-{pid}@akira.test"},
    )
    await db.execute(
        text(
            "insert into profiles (id, full_name, global_role, is_active, organisation_id)"
            " values (:id, 'Alias Test Owner', 'owner', true,"
            " (select organisation_id from outlets where code = 'AKR-NT01'))"
        ),
        {"id": pid},
    )
    await db.commit()
    return CurrentUser(
        profile_id=pid,
        full_name="Alias Test Owner",
        email=None,
        global_role=UserRole.OWNER,
        is_active=True,
        organisation_id=DEV_ORG,
        organisation_outlet_ids=dev_outlet_ids(),
    )


async def _world(db: AsyncSession) -> uuid.UUID:
    """Two menu items, one bill carrying the SHORT name of one of them."""
    outlet = uuid.UUID(str(await db.scalar(text("select id from outlets where code = 'AKR-NT01'"))))
    await db.execute(
        text(
            "insert into menu_items (name, category) values"
            " ('Chicken Karaage Donburi', 'Donburi'), ('Sakura', 'Refreshments')"
        )
    )
    order_id = await db.scalar(
        text(
            """
            insert into sales_orders
                (outlet_id, external_bill_no, business_date, ordered_at, gross_paise, net_paise)
            values (:o, 'ALIAS-1', '2026-08-20', '2026-08-20 20:00+05:30', 60000, 60000)
            returning id
            """
        ),
        {"o": outlet},
    )
    await db.execute(
        text(
            "insert into sales_order_items"
            " (order_id, outlet_id, business_date, item_name, sl_no) values"
            " (:id, :o, '2026-08-20', 'Donburi Chicken', 1),"
            " (:id, :o, '2026-08-20', 'Sakura', 2)"
        ),
        {"id": order_id, "o": outlet},
    )
    await db.commit()
    return outlet


class TestAliasesInTheMeasuredRate:
    async def test_an_unmapped_bill_name_is_listed_and_not_counted(
        self, session: AsyncSession
    ) -> None:
        outlet = await _world(session)
        owner = await _owner(session)
        mix = await service.menu_mix(
            session, owner, outlet_id=outlet, date_from=date(2026, 8, 1), date_to=date(2026, 8, 31)
        )
        measured = mix["measured"]
        assert measured["bills_measured"] == 1
        assert measured["unmapped_item_names"] == ["Donburi Chicken"]
        assert {c["category"] for c in measured["categories"]} == {"Refreshments"}

    async def test_mapping_the_alias_makes_the_category_count(self, session: AsyncSession) -> None:
        outlet = await _world(session)
        owner = await _owner(session)
        added = await service.add_menu_alias(
            session, owner, alias="donburi chicken", menu_item_name="chicken karaage donburi"
        )
        assert added["menu_item"] == "Chicken Karaage Donburi"

        mix = await service.menu_mix(
            session, owner, outlet_id=outlet, date_from=date(2026, 8, 1), date_to=date(2026, 8, 31)
        )
        measured = mix["measured"]
        assert measured["unmapped_item_names"] == []
        by = {c["category"]: c for c in measured["categories"]}
        assert by["Donburi"]["bills_with"] == 1 and by["Donburi"]["share_of_bills"] == 1.0
        assert by["Refreshments"]["bills_with"] == 1

    async def test_the_alias_is_audited(self, session: AsyncSession) -> None:
        await _world(session)
        owner = await _owner(session)
        await service.add_menu_alias(
            session, owner, alias="Donburi Chicken", menu_item_name="Chicken Karaage Donburi"
        )
        n = await session.scalar(
            text("select count(*) from audit_log where entity_table = 'menu_item_aliases'")
        )
        assert n == 1


class TestOneSpellingOneItem:
    async def test_a_second_mapping_of_the_same_spelling_is_refused(
        self, session: AsyncSession
    ) -> None:
        await _world(session)
        owner = await _owner(session)
        await service.add_menu_alias(
            session, owner, alias="Donburi Chicken", menu_item_name="Chicken Karaage Donburi"
        )
        with pytest.raises(ConflictError, match="already maps to"):
            await service.add_menu_alias(
                session, owner, alias="DONBURI CHICKEN", menu_item_name="Sakura"
            )

    async def test_a_menu_name_cannot_be_an_alias(self, session: AsyncSession) -> None:
        await _world(session)
        owner = await _owner(session)
        with pytest.raises(ConflictError, match="already a menu item name"):
            await service.add_menu_alias(
                session, owner, alias="Sakura", menu_item_name="Chicken Karaage Donburi"
            )

    async def test_an_unknown_menu_item_is_refused(self, session: AsyncSession) -> None:
        await _world(session)
        owner = await _owner(session)
        with pytest.raises(NotFoundError, match="not a menu item"):
            await service.add_menu_alias(
                session, owner, alias="Donburi Chicken", menu_item_name="Beef Donburi"
            )

    async def test_deleting_restores_the_unmapped_listing(self, session: AsyncSession) -> None:
        outlet = await _world(session)
        owner = await _owner(session)
        added = await service.add_menu_alias(
            session, owner, alias="Donburi Chicken", menu_item_name="Chicken Karaage Donburi"
        )
        await service.delete_menu_alias(session, owner, uuid.UUID(added["id"]))
        mix = await service.menu_mix(session, owner, outlet_id=outlet, date_from=None, date_to=None)
        assert mix["measured"]["unmapped_item_names"] == ["Donburi Chicken"]
        items = await service.list_menu_items(session, organisation_id=DEV_ORG)
        assert all(i["aliases"] == [] for i in items)
