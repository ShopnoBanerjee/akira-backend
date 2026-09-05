"""The production cut-over script, rehearsed against a real schema.

Everything runs inside the `db` fixture's transaction and is rolled back, so
the shared test database keeps both outlets for every other suite. The
cut-over's own statements use savepoints (asyncpg nests transactions that
way), which is exactly what lets a rehearsal prove the cascades without
keeping the result.
"""

import sys
import uuid
from datetime import date
from pathlib import Path

import asyncpg
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import prod_cutover as cutover

pytestmark = pytest.mark.asyncio

KEEP_FROM = date(2026, 9, 8)


async def _person(conn: asyncpg.Connection, email: str, role: str, *, active: bool = True):
    pid = uuid.uuid4()
    await conn.execute("insert into auth.users (id, email) values ($1, $2)", pid, email)
    await conn.execute(
        "insert into profiles (id, full_name, global_role, is_active) values ($1, $2, $3, $4)",
        pid,
        email.split("@")[0],
        role,
        active,
    )
    return pid


async def _run(conn: asyncpg.Connection, outlet_id: uuid.UUID, business_date: date):
    assignment = await conn.fetchrow(
        "select id, template_id from checklist_assignments where outlet_id = $1 limit 1", outlet_id
    )
    if assignment is None:
        template = await conn.fetchval("select id from checklist_templates limit 1")
        assignment_id = await conn.fetchval(
            """
            insert into checklist_assignments
                (template_id, outlet_id, assigned_role, due_time_local)
            values ($1, $2, 'staff', '10:00') returning id
            """,
            template,
            outlet_id,
        )
    else:
        assignment_id, template = assignment["id"], assignment["template_id"]
    run_id = await conn.fetchval(
        """
        insert into checklist_runs
            (assignment_id, template_id, template_version, outlet_id, business_date, status)
        values ($1, $2, 1, $3, $4, 'pending') returning id
        """,
        assignment_id,
        template,
        outlet_id,
        business_date,
    )
    item = await conn.fetchval(
        "select id from checklist_template_items where template_id = $1 limit 1", template
    )
    await conn.execute(
        """
        insert into checklist_run_items (run_id, template_item_id, sort_order, result, photo_path)
        values ($1, $2, 1, 'pass', $3)
        """,
        run_id,
        item,
        f"{outlet_id}/{business_date.isoformat()}/{run_id}.jpg",
    )
    await conn.execute(
        """
        insert into sop_exceptions (outlet_id, business_date, severity, title, status)
        values ($1, $2, 'medium', 'seeded', 'open')
        """,
        outlet_id,
        business_date,
    )
    return run_id


@pytest.fixture
async def world(db: asyncpg.Connection) -> dict[str, object]:
    real = await db.fetchval("select id from outlets where code = 'AKR-NT01'")
    fake = await db.fetchval("select id from outlets where code = 'AKR-DEV02'")
    before = await _run(db, real, date(2026, 8, 20))
    after = await _run(db, real, date(2026, 9, 10))
    await _run(db, fake, date(2026, 8, 20))
    test_owner = await _person(db, "owner@akira.test", "owner")
    test_staff = await _person(db, "staff.nt@akira.test", "staff")
    device_auth = uuid.uuid4()
    await db.execute(
        "insert into auth.users (id, email) values ($1, 'device.nt01@akira.test')", device_auth
    )
    await db.execute(
        """
        insert into outlet_devices (outlet_id, auth_user_id, label)
        values ($1, $2, 'test tablet')
        """,
        real,
        device_auth,
    )
    await db.execute(
        """
        insert into app_settings (key, scope, value)
        values ('sales.petpooja_restaurant_name', 'global', '"Akira"'::jsonb)
        """
    )
    return {
        "real": real,
        "fake": fake,
        "before": before,
        "after": after,
        "test_owner": test_owner,
        "test_staff": test_staff,
        "device_auth": device_auth,
    }


def _shim_deleter(conn: asyncpg.Connection) -> cutover.DeleteAuthUser:
    """What the Auth Admin API does, on the local shim: delete the auth row."""

    async def delete(user_id: uuid.UUID) -> None:
        await conn.execute("delete from auth.users where id = $1", user_id)

    return delete


class TestThePlan:
    async def test_it_counts_what_would_go_and_names_the_blocker(
        self, db: asyncpg.Connection, world: dict[str, object]
    ) -> None:
        p = await cutover.plan(db, KEEP_FROM)
        assert p.synthetic_outlet_id == world["fake"]
        assert p.synthetic_counts["checklist_runs"] >= 1
        assert p.runs_to_delete == 1 and p.exceptions_to_delete == 1
        assert len(p.photo_paths) == 1
        assert {e for _, e in p.test_accounts} == {
            "owner@akira.test",
            "staff.nt@akira.test",
            "device.nt01@akira.test",
        }
        assert p.test_devices == 1
        assert p.guard_armed
        assert p.blockers == [
            "no ACTIVE owner with a real email exists - invite one, sign in once, re-run"
        ]
        text = cutover.describe(p)
        assert "BLOCKED" in text and "owner@akira.test" in text

    async def test_no_keep_from_is_a_blocker_too(
        self, db: asyncpg.Connection, world: dict[str, object]
    ) -> None:
        await _person(db, "shopno@akira.example", "owner")
        p = await cutover.plan(db, None)
        assert p.blockers == ["--keep-from YYYY-MM-DD is required to execute"]
        assert p.runs_to_delete == 0  # nothing counted without a date

    async def test_a_deactivated_real_owner_does_not_count(
        self, db: asyncpg.Connection, world: dict[str, object]
    ) -> None:
        await _person(db, "old@akira.example", "owner", active=False)
        p = await cutover.plan(db, KEEP_FROM)
        assert p.real_owners == []


class TestApplying:
    async def test_blocked_plans_refuse_to_apply(
        self, db: asyncpg.Connection, world: dict[str, object]
    ) -> None:
        p = await cutover.plan(db, KEEP_FROM)
        with pytest.raises(RuntimeError, match="blocked"):
            await cutover.apply_database(db, p, actor_note="test")
        assert await db.fetchval("select count(*) from outlets where code = 'AKR-DEV02'") == 1

    async def test_the_cut_removes_exactly_the_synthetic_and_keeps_the_real(
        self, db: asyncpg.Connection, world: dict[str, object]
    ) -> None:
        await _person(db, "shopno@akira.example", "owner")
        sales_before = await db.fetchval(
            "select count(*) from sales_orders where outlet_id = $1", world["real"]
        )
        p = await cutover.plan(db, KEEP_FROM)
        assert p.blockers == []

        await cutover.apply_database(db, p, actor_note="rehearsal")
        failures = await cutover.apply_accounts(p, _shim_deleter(db))
        assert failures == []

        # the synthetic outlet and everything under it
        assert await db.fetchval("select count(*) from outlets where code = 'AKR-DEV02'") == 0
        assert (
            await db.fetchval(
                "select count(*) from checklist_runs where outlet_id = $1", world["fake"]
            )
            == 0
        )
        # history before the cut is gone, after it is kept
        assert (
            await db.fetchval("select count(*) from checklist_runs where id = $1", world["before"])
            == 0
        )
        assert (
            await db.fetchval("select count(*) from checklist_runs where id = $1", world["after"])
            == 1
        )
        assert (
            await db.fetchval(
                "select count(*) from sop_exceptions where outlet_id = $1 and business_date < $2",
                world["real"],
                KEEP_FROM,
            )
            == 0
        )
        # test accounts, their profiles and the device row
        assert (
            await db.fetchval("select count(*) from auth.users where email like '%@akira.test'")
            == 0
        )
        assert (
            await db.fetchval("select count(*) from profiles where id = $1", world["test_owner"])
            == 0
        )
        assert (
            await db.fetchval(
                "select count(*) from outlet_devices where auth_user_id = $1", world["device_auth"]
            )
            == 0
        )
        # the real owner, the real outlet, the sales and the templates stay
        assert (
            await db.fetchval(
                "select count(*) from profiles where global_role = 'owner' and is_active"
            )
            == 1
        )
        assert await db.fetchval("select count(*) from outlets where code = 'AKR-NT01'") == 1
        assert (
            await db.fetchval(
                "select count(*) from sales_orders where outlet_id = $1", world["real"]
            )
            == sales_before
        )
        assert await db.fetchval("select count(*) from checklist_templates") > 0
        # and the audit row says what happened
        row = await db.fetchrow(
            """
            select after from audit_log
             where action = 'delete' and entity_table = 'outlets'
             order by at desc limit 1
            """
        )
        assert row is not None and '"stage": "production"' in row["after"]

    async def test_a_failing_account_deletion_is_reported_not_fatal(
        self, db: asyncpg.Connection, world: dict[str, object]
    ) -> None:
        await _person(db, "shopno@akira.example", "owner")
        p = await cutover.plan(db, KEEP_FROM)

        async def flaky(user_id: uuid.UUID) -> None:
            if user_id == world["test_staff"]:
                raise RuntimeError("auth admin returned 500")
            await db.execute("delete from auth.users where id = $1", user_id)

        failures = await cutover.apply_accounts(p, flaky)
        assert failures == ["staff.nt@akira.test: auth admin returned 500"]
        assert (
            await db.fetchval("select count(*) from auth.users where email like '%@akira.test'")
            == 1
        )
