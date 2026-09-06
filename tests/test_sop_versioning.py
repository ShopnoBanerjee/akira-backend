"""The D11 versioning rule, exercised through the service layer against a real
database.

The failure this guards against: an admin flips is_critical on an item today,
and every past run that failed it retroactively looks like a critical failure.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.deps import CurrentUser
from app.core.enums import UserRole
from app.domains.sop import service
from app.domains.sop.schemas import (
    ItemFields,
    ReorderRequest,
    UpdateTemplateItemRequest,
    UpdateTemplateRequest,
)
from tests.conftest import DEV_ORG, dev_outlet_ids

pytestmark = pytest.mark.asyncio

OWNER_ID = uuid.UUID("99999999-9999-9999-9999-999999999999")


def owner() -> CurrentUser:
    return CurrentUser(
        profile_id=OWNER_ID,
        full_name="Test Owner",
        email="owner@test",
        global_role=UserRole.OWNER,
        is_active=True,
        organisation_id=DEV_ORG,
        organisation_outlet_ids=dev_outlet_ids(),
    )


@pytest.fixture
async def session(migrated_db: str):  # type: ignore[no-untyped-def]
    """An AsyncSession against the migrated test database, with the test's
    writes rolled back afterwards so cases stay independent."""
    url = migrated_db.replace("postgresql://", "postgresql+asyncpg://")
    engine = create_async_engine(url, poolclass=None)
    async with AsyncSession(engine, expire_on_commit=False) as db:
        # The service commits internally; wrap everything in a savepoint-like
        # cleanup by truncating our own writes at the end instead. Simpler:
        # each test creates its own template, so leftovers cannot collide.
        yield db
    await engine.dispose()


async def _seed_owner_profile(db: AsyncSession) -> None:
    from sqlalchemy import text

    await db.execute(
        text(
            "insert into auth.users (id, email) values (:id, 'owner@test')"
            " on conflict (id) do nothing"
        ),
        {"id": OWNER_ID},
    )
    await db.execute(
        text(
            "insert into profiles (id, full_name, global_role, is_active)"
            " values (:id, 'Test Owner', 'owner', true)"
            " on conflict (id) do nothing"
        ),
        {"id": OWNER_ID},
    )
    await db.commit()


async def _make_template(db: AsyncSession) -> Any:
    from sqlalchemy import text

    await _seed_owner_profile(db)
    category_id = (await db.execute(text("select id from sop_categories limit 1"))).scalar_one()
    from app.core.enums import DayPart, Frequency
    from app.domains.sop.schemas import CreateTemplateRequest

    detail = await service.create_template(
        db,
        owner(),
        CreateTemplateRequest(
            name=f"Versioning test {uuid.uuid4().hex[:8]}",
            category_id=category_id,
            frequency=Frequency.DAILY,
            day_part=DayPart.CLOSING,
        ),
    )
    return detail


async def _version_rows(db: AsyncSession, template_id: uuid.UUID) -> list[dict]:
    from sqlalchemy import text

    rows = (
        await db.execute(
            text(
                "select template_version, title, is_critical, requires_photo, sort_order"
                " from checklist_template_item_versions"
                " where template_id = :id order by template_version, sort_order"
            ),
            {"id": template_id},
        )
    ).mappings()
    return [dict(r) for r in rows]


class TestMaterialEditsBump:
    async def test_adding_an_item_bumps_and_snapshots(self, session: AsyncSession) -> None:
        template = await _make_template(session)
        assert template.version == 1

        after = await service.add_item(
            session, owner(), template.id, ItemFields(title="Wipe the pass")
        )
        assert after.version == 2

        versions = await _version_rows(session, template.id)
        assert [v["template_version"] for v in versions] == [2]
        assert versions[0]["title"] == "Wipe the pass"

    async def test_editing_meaning_bumps_but_history_keeps_the_old_truth(
        self, session: AsyncSession
    ) -> None:
        template = await _make_template(session)
        after = await service.add_item(
            session, owner(), template.id, ItemFields(title="Check the fridge")
        )
        item = after.items[0]
        assert item.is_critical is False

        edited = await service.update_item(
            session,
            owner(),
            template.id,
            item.id,
            UpdateTemplateItemRequest(is_critical=True, requires_photo=True),
        )
        assert edited.version == 3
        assert edited.items[0].is_critical is True

        versions = await _version_rows(session, template.id)
        v2 = [v for v in versions if v["template_version"] == 2]
        v3 = [v for v in versions if v["template_version"] == 3]
        assert v2[0]["is_critical"] is False, "the old definition must not change"
        assert v2[0]["requires_photo"] is False
        assert v3[0]["is_critical"] is True

    async def test_reorder_bumps_and_is_complete_or_refused(self, session: AsyncSession) -> None:
        template = await _make_template(session)
        for title in ("First", "Second", "Third"):
            template = await service.add_item(
                session, owner(), template.id, ItemFields(title=title)
            )
        ids = [i.id for i in template.items]

        # A partial list must be refused, not silently drop the missing item.
        from app.core.errors import ValidationError

        with pytest.raises(ValidationError):
            await service.reorder_items(
                session, owner(), template.id, ReorderRequest(item_ids=ids[:2])
            )

        reordered = await service.reorder_items(
            session, owner(), template.id, ReorderRequest(item_ids=list(reversed(ids)))
        )
        assert [i.title for i in reordered.items] == ["Third", "Second", "First"]
        assert [i.sort_order for i in reordered.items] == [1, 2, 3]
        assert reordered.version == template.version + 1


class TestNonMaterialEditsDoNot:
    async def test_renaming_the_template_does_not_bump(self, session: AsyncSession) -> None:
        template = await _make_template(session)
        template = await service.add_item(
            session, owner(), template.id, ItemFields(title="Anything")
        )
        version_before = template.version

        renamed = await service.update_template(
            session,
            owner(),
            template.id,
            UpdateTemplateRequest(name="A better name", description="A description-only edit."),
        )
        assert renamed.version == version_before
        assert renamed.name == "A better name"


class TestDeletion:
    async def test_unused_item_is_hard_deleted(self, session: AsyncSession) -> None:
        from sqlalchemy import text

        template = await _make_template(session)
        template = await service.add_item(
            session, owner(), template.id, ItemFields(title="Never used")
        )
        item_id = template.items[0].id

        await service.delete_item(session, owner(), template.id, item_id)
        gone = (
            await session.execute(
                text("select 1 from checklist_template_items where id = :id"),
                {"id": item_id},
            )
        ).first()
        assert gone is None, "never referenced by a run, so truly deleted"

    async def test_item_used_by_a_run_is_soft_deleted(self, session: AsyncSession) -> None:
        from sqlalchemy import text

        template = await _make_template(session)
        template = await service.add_item(
            session, owner(), template.id, ItemFields(title="Answered once")
        )
        item = template.items[0]

        # Wire up the minimum a run needs: assignment -> run -> run item.
        outlet_id = (await session.execute(text("select id from outlets limit 1"))).scalar_one()
        assignment_id = (
            await session.execute(
                text(
                    """
                    insert into checklist_assignments
                        (template_id, outlet_id, assigned_role, due_time_local)
                    values (:t, :o, 'staff', '17:00') returning id
                    """
                ),
                {"t": template.id, "o": outlet_id},
            )
        ).scalar_one()
        run_id = (
            await session.execute(
                text(
                    """
                    insert into checklist_runs
                        (assignment_id, template_id, template_version, outlet_id,
                         business_date)
                    values (:a, :t, :v, :o, current_date) returning id
                    """
                ),
                {"a": assignment_id, "t": template.id, "v": template.version, "o": outlet_id},
            )
        ).scalar_one()
        await session.execute(
            text(
                "insert into checklist_run_items (run_id, template_item_id, sort_order)"
                " values (:r, :i, 1)"
            ),
            {"r": run_id, "i": item.id},
        )
        await session.commit()

        detail = await service.delete_item(session, owner(), template.id, item.id)
        assert all(i.id != item.id for i in detail.items), "gone from the live list"

        still_there = (
            (
                await session.execute(
                    text("select deleted_at from checklist_template_items where id = :id"),
                    {"id": item.id},
                )
            )
            .mappings()
            .first()
        )
        assert still_there is not None, "the row survives for history"
        assert still_there["deleted_at"] is not None


class TestSeededTemplatesRemainCoherent:
    async def test_every_template_version_has_snapshot_rows(self, session: AsyncSession) -> None:
        """For every template, its current version must be renderable from the
        version table — no template may point at a version with no rows."""
        from sqlalchemy import text

        orphans = (
            await session.execute(
                text(
                    """
                    select count(*) from checklist_templates t
                     where t.deleted_at is null
                       and exists (select 1 from checklist_template_items i
                                    where i.template_id = t.id and i.deleted_at is null)
                       and not exists (
                           select 1 from checklist_template_item_versions v
                            where v.template_id = t.id
                              and v.template_version = t.version)
                    """
                )
            )
        ).scalar_one()
        assert orphans == 0
