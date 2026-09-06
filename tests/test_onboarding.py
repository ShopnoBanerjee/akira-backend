"""The onboarding checklist (P26b).

A new organisation starts empty, and every screen then reads as broken without
saying why. These tests pin the two things that make the checklist trustworthy:
it reports what is genuinely there rather than a stored flag, and it never
reaches past the caller's own outlet.
"""

import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.enums import UserRole
from app.core.errors import ForbiddenError, NotFoundError
from app.domains.onboarding import service
from tests.conftest import DEV_ORG, dev_user

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session(migrated_db: str):  # type: ignore[no-untyped-def]
    engine = create_async_engine(migrated_db.replace("postgresql://", "postgresql+asyncpg://"))
    async with AsyncSession(engine, expire_on_commit=False) as db:
        try:
            yield db
        finally:
            await db.rollback()
    await engine.dispose()


async def _bare_outlet(db: AsyncSession) -> uuid.UUID:
    """An outlet of the development organisation with nothing attached."""
    return uuid.UUID(
        str(
            await db.scalar(
                text(
                    "insert into outlets (organisation_id, code, name, city)"
                    " values (:org, :code, 'Onboarding Test', 'Kolkata') returning id"
                ),
                {"org": DEV_ORG, "code": f"ONB-{uuid.uuid4().hex[:6].upper()}"},
            )
        )
    )


def _owner_of(outlet_id: uuid.UUID):  # type: ignore[no-untyped-def]
    user = dev_user(UserRole.OWNER)
    object.__setattr__(user, "organisation_outlet_ids", frozenset({outlet_id}))
    return user


class TestABrandNewOutlet:
    async def test_nothing_is_done_and_the_menu_map_comes_first(
        self, session: AsyncSession
    ) -> None:
        outlet = await _bare_outlet(session)
        status = await service.readiness(session, _owner_of(outlet), outlet_id=outlet)

        assert status["ready"] is False
        assert status["required_done"] == 0
        assert status["required_total"] == 5
        assert [s["key"] for s in status["steps"]][:3] == ["menu_map", "bills", "category_mix"]
        assert all(s["done"] is False for s in status["steps"])
        # Recipes are seeded against no organisation only if the starter kit
        # exists; the development organisation's own are what count here.
        assert {s["key"] for s in status["steps"] if s["required"]} == {
            "menu_map",
            "bills",
            "category_mix",
            "checklists",
            "people",
        }

    async def test_every_step_says_what_to_do_and_where(self, session: AsyncSession) -> None:
        """A checklist that only says "missing" moves the problem rather than
        solving it. Each step names the Petpooja report and the screen."""
        outlet = await _bare_outlet(session)
        status = await service.readiness(session, _owner_of(outlet), outlet_id=outlet)
        for step in status["steps"]:
            assert step["why"].strip() and step["how"].strip()
            assert step["href"].startswith("/app")
        petpooja = {s["key"]: s["how"] for s in status["steps"] if "Petpooja" in s["how"]}
        assert set(petpooja) == {
            "menu_map",
            "bills",
            "category_mix",
            "item_days",
            "restaurant_guard",
        }
        assert "Item Wise" in petpooja["menu_map"]
        assert "Order Listing" in petpooja["bills"]
        assert "Category Wise" in petpooja["category_mix"]
        assert "Day Wise" in petpooja["item_days"]


class TestItFollowsTheDataNotAFlag:
    async def test_assigning_a_checklist_completes_that_step(self, session: AsyncSession) -> None:
        outlet = await _bare_outlet(session)
        user = _owner_of(outlet)
        before = await service.readiness(session, user, outlet_id=outlet)
        assert next(s for s in before["steps"] if s["key"] == "checklists")["done"] is False

        template = await session.scalar(
            text("select id from checklist_templates where deleted_at is null limit 1")
        )
        await session.execute(
            text(
                "insert into checklist_assignments"
                "   (template_id, outlet_id, assigned_role, due_time_local, is_active)"
                " values (:t, :o, 'shift_lead', '09:00', true)"
            ),
            {"t": template, "o": outlet},
        )

        after = await service.readiness(session, user, outlet_id=outlet)
        step = next(s for s in after["steps"] if s["key"] == "checklists")
        assert step["done"] is True and step["count"] == 1
        assert after["required_done"] == 1 and after["ready"] is False

    async def test_the_menu_map_is_organisation_wide_not_per_outlet(
        self, session: AsyncSession
    ) -> None:
        """One menu across the organisation's outlets (D29): a second outlet
        does not have to upload Item Wise again."""
        outlet = await _bare_outlet(session)
        status = await service.readiness(session, _owner_of(outlet), outlet_id=outlet)
        menu = next(s for s in status["steps"] if s["key"] == "menu_map")
        seeded = await session.scalar(
            text("select count(*) from menu_items where organisation_id = :org"), {"org": DEV_ORG}
        )
        assert menu["count"] == seeded

    async def test_a_fully_set_up_outlet_reads_ready(self, session: AsyncSession) -> None:
        outlet = await _bare_outlet(session)
        user = _owner_of(outlet)
        template = await session.scalar(
            text("select id from checklist_templates where deleted_at is null limit 1")
        )
        await session.execute(
            text(
                "insert into checklist_assignments"
                "   (template_id, outlet_id, assigned_role, due_time_local, is_active)"
                " values (:t, :o, 'shift_lead', '09:00', true)"
            ),
            {"t": template, "o": outlet},
        )
        person = uuid.uuid4()
        await session.execute(
            text("insert into auth.users (id, email) values (:id, :e)"),
            {"id": person, "e": f"{person}@onboarding.test"},
        )
        await session.execute(
            text(
                "insert into profiles (id, full_name, global_role, is_active, organisation_id)"
                " values (:id, 'Onboarding Staff', 'staff', true, :org)"
            ),
            {"id": person, "org": DEV_ORG},
        )
        await session.execute(
            text(
                "insert into outlet_members (outlet_id, profile_id, role_at_outlet)"
                " values (:o, :p, 'staff')"
            ),
            {"o": outlet, "p": person},
        )
        upload = await session.scalar(
            text(
                "insert into data_uploads (outlet_id, source, original_filename,"
                "   storage_path, file_sha256, status)"
                " values (:o, 'petpooja_listing', 'onb.xlsx', :p, :h, 'parsed')"
                " returning id"
            ),
            {"o": outlet, "p": f"{outlet}/onb.xlsx", "h": f"onb-{outlet}"},
        )
        await session.execute(
            text(
                "insert into sales_orders (outlet_id, upload_id, external_bill_no, business_date,"
                " ordered_at, net_paise, customer_phone_hash)"
                " values (:o, :u, 'onb-1', '2020-02-02', now(), 10000, 'onb-hash')"
            ),
            {"o": outlet, "u": upload},
        )
        await session.execute(
            text(
                "insert into menu_items (organisation_id, name, category, upload_id)"
                " values (:org, 'Onboarding Ramen', 'Ramen', :u)"
            ),
            {"org": DEV_ORG, "u": upload},
        )
        await session.execute(
            text(
                "insert into sales_category_periods"
                "   (outlet_id, upload_id, period_start, period_end, category, orders, items)"
                " values (:o, :u, '2020-02-01', '2020-02-28', 'Refreshments', 5, 7)"
            ),
            {"o": outlet, "u": upload},
        )

        status = await service.readiness(session, user, outlet_id=outlet)
        outstanding = [s["key"] for s in status["steps"] if s["required"] and not s["done"]]
        assert outstanding == [], outstanding
        assert status["ready"] is True
        assert status["required_done"] == status["required_total"] == 5


class TestItStopsAtTheCallersOwnOutlet:
    async def test_another_outlet_is_refused(self, session: AsyncSession) -> None:
        mine, theirs = await _bare_outlet(session), await _bare_outlet(session)
        with pytest.raises(ForbiddenError):
            await service.readiness(session, _owner_of(mine), outlet_id=theirs)

    async def test_with_one_outlet_it_needs_no_argument(self, session: AsyncSession) -> None:
        outlet = await _bare_outlet(session)
        status = await service.readiness(session, _owner_of(outlet), outlet_id=None)
        assert status["outlet_id"] == outlet

    async def test_with_several_it_asks_which(self, session: AsyncSession) -> None:
        a, b = await _bare_outlet(session), await _bare_outlet(session)
        user = dev_user(UserRole.OWNER)
        object.__setattr__(user, "organisation_outlet_ids", frozenset({a, b}))
        with pytest.raises(NotFoundError, match="Name the outlet"):
            await service.readiness(session, user, outlet_id=None)

    async def test_someone_with_no_outlet_is_told_so(self, session: AsyncSession) -> None:
        user = dev_user(UserRole.OUTLET_MANAGER)
        object.__setattr__(user, "organisation_outlet_ids", frozenset())
        with pytest.raises(NotFoundError, match="no outlet"):
            await service.readiness(session, user, outlet_id=None)
