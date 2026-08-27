"""Row level security, exercised as the attacker would.

The API enforces authorisation in code; RLS is the second line (0007). A second
line that has never been fired at is decoration, so this suite connects the way
a leaked publishable key would — as the `authenticated` role with a JWT subject
claim — and tries to read the other outlet's data.

Two kinds of test, deliberately separate:

- **The audit** walks the catalog: every table locked down, every grant as
  narrow as 0007 says. This is what catches migration 0015 adding a table and
  forgetting the policy — the mistake nobody makes on purpose.
- **The behaviour tests** act as real identities against seeded rows in two
  outlets. Structure can be perfect while a `using` clause is subtly wrong;
  only reading as the wrong person catches that.

The shim in supabase/local reads the subject from `request.jwt.claim.sub`, so
"act as this user" is one set_config away.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date

import asyncpg
import pytest
import pytest_asyncio

pytestmark = pytest.mark.asyncio

BDATE = "2020-02-02"  # inert: far from anything other suites materialise
BDAY = date.fromisoformat(BDATE)


# --- Acting as somebody ------------------------------------------------------


async def act_as(conn: asyncpg.Connection, profile_id: uuid.UUID | None) -> None:
    """Become the `authenticated` role carrying this subject claim.

    `set_config(..., false)` is session-scoped on purpose: `set role` cannot be
    parameterised, and a transaction-scoped claim would vanish before the reads
    it is meant to scope.
    """
    await conn.execute("reset role")
    await conn.execute(
        "select set_config('request.jwt.claim.sub', $1, false)",
        str(profile_id) if profile_id else "",
    )
    await conn.execute("set role authenticated")


async def act_as_anon(conn: asyncpg.Connection) -> None:
    await conn.execute("reset role")
    await conn.execute("select set_config('request.jwt.claim.sub', '', false)")
    await conn.execute("set role anon")


# --- The seeded world --------------------------------------------------------


@dataclass
class World:
    outlet_a: uuid.UUID
    outlet_b: uuid.UUID
    owner: uuid.UUID
    manager_a: uuid.UUID  # outlet_manager, member of A only
    manager_b: uuid.UUID  # outlet_manager, member of B only
    inactive_a: uuid.UUID  # member of A, is_active = false
    run_a: uuid.UUID = field(default_factory=uuid.uuid4)
    run_b: uuid.UUID = field(default_factory=uuid.uuid4)


@pytest_asyncio.fixture(scope="module")
async def world(migrated_db: str):  # type: ignore[no-untyped-def]
    """Two outlets, four people, and one of everything outlet-scoped in each.

    Module-scoped because it is read-only for every test here, and torn down
    row by row so the shared session database is left as it was found.
    """
    conn = await asyncpg.connect(migrated_db)

    outlets = [r["id"] for r in await conn.fetch("select id from outlets order by code limit 2")]
    assert len(outlets) == 2, "the seed should have two outlets"
    w = World(
        outlet_a=outlets[0],
        outlet_b=outlets[1],
        owner=uuid.uuid4(),
        manager_a=uuid.uuid4(),
        manager_b=uuid.uuid4(),
        inactive_a=uuid.uuid4(),
    )

    people = [
        (w.owner, "RLS Owner", "owner", True),
        (w.manager_a, "RLS Manager A", "outlet_manager", True),
        (w.manager_b, "RLS Manager B", "outlet_manager", True),
        (w.inactive_a, "RLS Former Manager", "outlet_manager", False),
    ]
    for pid, name, role, active in people:
        await conn.execute(
            "insert into auth.users (id, email) values ($1, $2)", pid, f"{pid}@rls.test"
        )
        await conn.execute(
            "insert into profiles (id, full_name, global_role, is_active)"
            " values ($1, $2, $3::user_role, $4)",
            pid,
            name,
            role,
            active,
        )
    for pid, outlet in [
        (w.manager_a, w.outlet_a),
        (w.manager_b, w.outlet_b),
        (w.inactive_a, w.outlet_a),
    ]:
        await conn.execute(
            "insert into outlet_members (outlet_id, profile_id, role_at_outlet)"
            " values ($1, $2, 'outlet_manager')",
            outlet,
            pid,
        )

    # One of everything, per outlet, all tagged 'rls-probe' where a text column
    # allows it so teardown and assertions can find exactly these rows.
    for outlet, run_id in [(w.outlet_a, w.run_a), (w.outlet_b, w.run_b)]:
        assignment = await conn.fetchrow(
            "select a.id, a.template_id, t.version"
            "  from checklist_assignments a join checklist_templates t on t.id = a.template_id"
            " where a.outlet_id = $1 limit 1",
            outlet,
        )
        assert assignment is not None, "the seed should assign templates to both outlets"
        await conn.execute(
            "insert into checklist_runs (id, assignment_id, template_id, template_version,"
            " outlet_id, business_date, day_part, status)"
            " values ($1, $2, $3, $4, $5, $6, 'any', 'submitted')",
            run_id,
            assignment["id"],
            assignment["template_id"],
            assignment["version"],
            outlet,
            BDAY,
        )
        item = await conn.fetchrow(
            "select id from checklist_template_items where template_id = $1 limit 1",
            assignment["template_id"],
        )
        item_id = await conn.fetchval(
            "insert into checklist_run_items (run_id, template_item_id, sort_order, result)"
            " values ($1, $2, 1, 'pass') returning id",
            run_id,
            item["id"],
        )
        await conn.execute(
            "insert into run_item_ai_reviews (run_item_id, verdict, rationale, model,"
            " prompt_version) values ($1, 'pass', 'rls-probe', 'test', 'rls-probe')",
            item_id,
        )
        await conn.execute(
            "insert into run_review_views (run_id, run_item_id, reviewer_id) values ($1, $2, $3)",
            run_id,
            item_id,
            w.owner,
        )
        await conn.execute(
            "insert into sop_exceptions (outlet_id, business_date, severity, title,"
            " detail) values ($1, $2, 'high', 'rls-probe', 'rls-probe')",
            outlet,
            BDAY,
        )
        upload_id = await conn.fetchval(
            "insert into data_uploads (outlet_id, source, original_filename, storage_path,"
            " file_sha256, status) values ($1, 'petpooja_orders', 'rls-probe.xlsx',"
            " $2, $3, 'parsed') returning id",
            outlet,
            f"{outlet}/rls-probe.xlsx",
            f"rls-probe-{outlet}",
        )
        await conn.execute(
            "insert into sales_orders (outlet_id, upload_id, external_bill_no,"
            " business_date, ordered_at, net_paise, customer_phone_hash)"
            " values ($1, $2, 'rls-probe', $3, now(), 10000, 'rls-probe-hash')",
            outlet,
            upload_id,
            BDAY,
        )
        await conn.execute(
            "insert into job_runs (job_name, status, outlet_id, business_date, finished_at)"
            " values ('rls-probe', 'succeeded', $1, $2, now())",
            outlet,
            BDAY,
        )
        await conn.execute(
            "insert into audit_log (outlet_id, entity_table, action)"
            " values ($1, 'rls-probe', 'update')",
            outlet,
        )

    # Rows scoped to nothing: a network-wide job and a global audit line. The
    # policy says an outlet manager cannot see these; prove it.
    await conn.execute(
        "insert into job_runs (job_name, status, business_date, finished_at)"
        " values ('rls-probe-global', 'succeeded', $1, now())",
        BDAY,
    )
    await conn.execute(
        "insert into audit_log (entity_table, action) values ('rls-probe-global', 'update')"
    )

    # A per-outlet settings override on each side; global rows already exist or
    # are irrelevant — the policy grants scope = 'global' unconditionally.
    for outlet in (w.outlet_a, w.outlet_b):
        await conn.execute(
            "insert into app_settings (key, scope, outlet_id, value, note)"
            " values ('scoring.band.green', 'outlet', $1, '85'::jsonb, 'rls-probe')",
            outlet,
        )

    yield w

    await conn.execute("reset role")
    await conn.execute("delete from app_settings where note = 'rls-probe'")
    await conn.execute("delete from audit_log where entity_table like 'rls-probe%'")
    await conn.execute("delete from job_runs where job_name like 'rls-probe%'")
    await conn.execute("delete from sales_orders where external_bill_no = 'rls-probe'")
    await conn.execute("delete from data_uploads where original_filename = 'rls-probe.xlsx'")
    await conn.execute("delete from sop_exceptions where title = 'rls-probe'")
    await conn.execute("delete from checklist_runs where id = any($1)", [w.run_a, w.run_b])
    await conn.execute(
        "delete from profiles where id = any($1)",
        [w.owner, w.manager_a, w.manager_b, w.inactive_a],
    )
    await conn.execute("delete from auth.users where email like '%@rls.test'")
    await conn.close()


@pytest_asyncio.fixture
async def conn(migrated_db: str):  # type: ignore[no-untyped-def]
    """A connection that always comes back as superuser, whatever role a test
    left it in."""
    c = await asyncpg.connect(migrated_db)
    try:
        yield c
    finally:
        await c.execute("reset role")
        await c.close()


# --- The audit ---------------------------------------------------------------


class TestEveryTableIsLockedDown:
    async def test_rls_is_enabled_and_forced_on_every_public_table(
        self, conn: asyncpg.Connection
    ) -> None:
        """FORCE matters: without it the table owner walks straight past the
        policies. A table missing from this check is exactly the future
        migration this test exists to catch."""
        unlocked = await conn.fetch(
            """
            select c.relname, c.relrowsecurity, c.relforcerowsecurity
              from pg_class c join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'public' and c.relkind = 'r'
               and not (c.relrowsecurity and c.relforcerowsecurity)
            """
        )
        assert unlocked == [], f"tables without forced RLS: {[r['relname'] for r in unlocked]}"

    async def test_anon_can_touch_nothing(self, conn: asyncpg.Connection) -> None:
        held = await conn.fetch(
            """
            select table_name, privilege_type
              from information_schema.role_table_grants
             where grantee = 'anon' and table_schema = 'public'
            """
        )
        assert held == [], f"anon holds: {[(r['table_name'], r['privilege_type']) for r in held]}"

    async def test_authenticated_holds_select_and_nothing_else(
        self, conn: asyncpg.Connection
    ) -> None:
        """No insert, update or delete grants anywhere. Every write goes
        through the API; a browser with a session token must be read-only even
        before any policy is consulted."""
        writes = await conn.fetch(
            """
            select table_name, privilege_type
              from information_schema.role_table_grants
             where grantee = 'authenticated' and table_schema = 'public'
               and privilege_type <> 'SELECT'
            """
        )
        assert writes == [], (
            f"authenticated can write: {[(r['table_name'], r['privilege_type']) for r in writes]}"
        )

    async def test_every_table_has_at_least_one_policy(self, conn: asyncpg.Connection) -> None:
        """RLS enabled with zero policies means deny-all — safe but silently
        broken for the direct-read features (realtime) RLS exists to serve.
        Either way a policyless table is a decision nobody made."""
        bare = await conn.fetch(
            """
            select c.relname
              from pg_class c join pg_namespace n on n.oid = c.relnamespace
             where n.nspname = 'public' and c.relkind = 'r'
               and not exists (select 1 from pg_policy p where p.polrelid = c.oid)
            """
        )
        assert bare == [], f"tables with no policy at all: {[r['relname'] for r in bare]}"


# --- The behaviour -----------------------------------------------------------

#: table -> the column that identifies which outlet's probe row came back.
DIRECTLY_SCOPED = {
    "checklist_runs": "outlet_id",
    "sop_exceptions": "outlet_id",
    "sales_orders": "outlet_id",
    "data_uploads": "outlet_id",
    "job_runs": "outlet_id",
    "audit_log": "outlet_id",
}

PROBE_FILTERS = {
    "checklist_runs": f"business_date = '{BDATE}'",
    "sop_exceptions": "title = 'rls-probe'",
    "sales_orders": "external_bill_no = 'rls-probe'",
    "data_uploads": "original_filename = 'rls-probe.xlsx'",
    "job_runs": "job_name like 'rls-probe%'",
    "audit_log": "entity_table like 'rls-probe%'",
}


class TestAnOutletManagerSeesOnlyTheirOutlet:
    @pytest.mark.parametrize("table", sorted(DIRECTLY_SCOPED))
    async def test_the_other_outlets_rows_do_not_exist_for_them(
        self, conn: asyncpg.Connection, world: World, table: str
    ) -> None:
        await act_as(conn, world.manager_a)
        rows = await conn.fetch(
            f"select {DIRECTLY_SCOPED[table]} as outlet_id from {table}"
            f" where {PROBE_FILTERS[table]}"
        )
        seen = {r["outlet_id"] for r in rows}
        assert world.outlet_b not in seen, f"{table}: outlet B leaked to A's manager"
        assert world.outlet_a in seen, f"{table}: manager A cannot even see their own outlet"

    async def test_rows_scoped_to_no_outlet_are_not_theirs_either(
        self, conn: asyncpg.Connection, world: World
    ) -> None:
        """A network-wide job run and a global audit line have no outlet_id.
        For an outlet manager the policy treats null as 'not yours'."""
        await act_as(conn, world.manager_a)
        assert (
            await conn.fetchval("select count(*) from job_runs where job_name = 'rls-probe-global'")
            == 0
        )
        assert (
            await conn.fetchval(
                "select count(*) from audit_log where entity_table = 'rls-probe-global'"
            )
            == 0
        )

    async def test_run_items_and_ai_reviews_follow_their_run(
        self, conn: asyncpg.Connection, world: World
    ) -> None:
        """These tables carry no outlet_id; their policies walk the join. The
        walk is where a wrong `using` clause would leak."""
        await act_as(conn, world.manager_a)
        item_runs = {
            r["run_id"]
            for r in await conn.fetch(
                "select run_id from checklist_run_items where run_id = any($1)",
                [world.run_a, world.run_b],
            )
        }
        assert item_runs == {world.run_a}
        reviews = await conn.fetch(
            "select ri.run_id from run_item_ai_reviews a"
            " join checklist_run_items ri on ri.id = a.run_item_id"
            " where a.rationale = 'rls-probe'"
        )
        assert {r["run_id"] for r in reviews} == {world.run_a}
        views = await conn.fetch(
            "select run_id from run_review_views where run_id = any($1)",
            [world.run_a, world.run_b],
        )
        assert {r["run_id"] for r in views} == {world.run_a}

    async def test_outlets_and_colleagues_stop_at_the_membership_line(
        self, conn: asyncpg.Connection, world: World
    ) -> None:
        await act_as(conn, world.manager_a)
        outlet_ids = {r["id"] for r in await conn.fetch("select id from outlets")}
        assert world.outlet_a in outlet_ids and world.outlet_b not in outlet_ids

        profile_ids = {
            r["id"]
            for r in await conn.fetch("select id from profiles where full_name like 'RLS %'")
        }
        # Self and the (inactive) colleague at A. Manager B works elsewhere,
        # and the owner holds no outlet membership to be seen through.
        assert world.manager_a in profile_ids
        assert world.inactive_a in profile_ids
        assert world.manager_b not in profile_ids
        assert world.owner not in profile_ids

    async def test_settings_show_global_and_their_own_override_only(
        self, conn: asyncpg.Connection, world: World
    ) -> None:
        await act_as(conn, world.manager_a)
        rows = await conn.fetch(
            "select scope, outlet_id from app_settings where note = 'rls-probe'"
        )
        assert {r["outlet_id"] for r in rows} == {world.outlet_a}
        # Global rows stay readable — they are configuration, not secrets.
        await conn.fetch("select key from app_settings where scope = 'global'")


class TestTheOtherIdentities:
    async def test_a_global_admin_sees_both_outlets(
        self, conn: asyncpg.Connection, world: World
    ) -> None:
        await act_as(conn, world.owner)
        seen = {
            r["outlet_id"]
            for r in await conn.fetch(
                "select outlet_id from sales_orders where external_bill_no = 'rls-probe'"
            )
        }
        assert seen == {world.outlet_a, world.outlet_b}

    async def test_a_deactivated_account_sees_nothing_at_all(
        self, conn: asyncpg.Connection, world: World
    ) -> None:
        """Deactivation must sever data access even while the JWT stays valid —
        tokens outlive the decision to revoke someone. The helpers filter on
        is_active, so membership stops counting the moment the flag drops."""
        await act_as(conn, world.inactive_a)
        assert await conn.fetchval("select count(*) from outlets") == 0
        assert (
            await conn.fetchval(
                "select count(*) from checklist_runs where business_date = $1",
                BDAY,
            )
            == 0
        )
        # Reference data is gated on being an active somebody, too.
        assert await conn.fetchval("select count(*) from checklist_templates") == 0
        # The one thing left is their own profile row.
        mine = await conn.fetch("select id from profiles")
        assert {r["id"] for r in mine} == {world.inactive_a}

    async def test_a_forged_subject_sees_nothing(
        self, conn: asyncpg.Connection, world: World
    ) -> None:
        """A syntactically valid token whose subject matches no profile."""
        await act_as(conn, uuid.uuid4())
        assert await conn.fetchval("select count(*) from outlets") == 0
        assert await conn.fetchval("select count(*) from profiles") == 0
        assert await conn.fetchval("select count(*) from checklist_templates") == 0

    async def test_anon_is_refused_outright(self, conn: asyncpg.Connection, world: World) -> None:
        """Not zero rows — an error. anon has no select grant to filter."""
        await act_as_anon(conn)
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.fetch("select id from outlets")

    async def test_authenticated_cannot_write_even_their_own_outlet(
        self, conn: asyncpg.Connection, world: World
    ) -> None:
        await act_as(conn, world.manager_a)
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "update sales_orders set net_paise = 0 where outlet_id = $1", world.outlet_a
            )
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "insert into sop_exceptions (outlet_id, business_date, severity, title)"
                " values ($1, $2, 'low', 'forged')",
                world.outlet_a,
                BDAY,
            )
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute("delete from audit_log where outlet_id = $1", world.outlet_a)
