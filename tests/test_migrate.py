"""The tracked migration runner (P25) against a real, throwaway database.

Its own database, not the shared fixture: it has to start from nothing to
prove that applying from zero, applying nothing twice, applying only what is
new, rolling back a broken file, and baselining a hand-migrated schema all
behave. The shim goes in first because the migrations reference auth.users.
"""

import sys
from pathlib import Path

import asyncpg
import pytest

from tests.conftest import SHIM, _probe, _swap_database, admin_dsn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import migrate

pytestmark = pytest.mark.asyncio

DB = "akira_ops_migrate_test"


@pytest.fixture
async def conn():  # type: ignore[no-untyped-def]
    skip = await _probe()
    if skip:
        pytest.skip(skip)
    admin = await asyncpg.connect(admin_dsn())
    try:
        await admin.execute(f'drop database if exists "{DB}" with (force)')
        await admin.execute(f'create database "{DB}"')
    finally:
        await admin.close()
    c = await asyncpg.connect(_swap_database(admin_dsn(), DB))
    await c.execute(SHIM.read_text(encoding="utf-8"))
    try:
        yield c
    finally:
        await c.close()
        admin = await asyncpg.connect(admin_dsn())
        try:
            await admin.execute(f'drop database if exists "{DB}" with (force)')
        finally:
            await admin.close()


ALL = migrate.load_migrations()


class TestFromNothing:
    async def test_the_plan_on_an_empty_database_is_everything(self, conn) -> None:  # type: ignore[no-untyped-def]
        p = await migrate.plan(conn, ALL)
        assert [m.filename for m in p.pending] == [m.filename for m in ALL]
        assert p.problems == []

    async def test_apply_runs_all_records_all_and_then_nothing(self, conn) -> None:  # type: ignore[no-untyped-def]
        ran = await migrate.apply(conn, ALL)
        assert ran == [m.filename for m in ALL]
        rows = await conn.fetch("select filename, checksum, baseline from schema_migrations")
        assert {r["filename"]: r["checksum"] for r in rows} == {m.filename: m.checksum for m in ALL}
        assert not any(r["baseline"] for r in rows)
        assert await migrate.apply(conn, ALL) == []
        # The real schema arrived: the last table this repo added exists and is locked.
        assert await conn.fetchval(
            "select relforcerowsecurity from pg_class where relname = 'training_records'"
        )

    async def test_the_tracking_table_is_locked_down_like_everything_else(self, conn) -> None:  # type: ignore[no-untyped-def]
        await migrate.apply(conn, ALL)
        assert await conn.fetchval(
            "select relforcerowsecurity from pg_class where relname = 'schema_migrations'"
        )
        grants = await conn.fetch(
            "select grantee from information_schema.role_table_grants"
            " where table_name = 'schema_migrations' and grantee in ('anon', 'authenticated')"
        )
        assert grants == []
        # An explicit deny-all policy, so the catalog audit (test_rls) never
        # mistakes it for a table somebody forgot.
        assert (
            await conn.fetchval(
                "select count(*) from pg_policies where tablename = 'schema_migrations'"
            )
            == 1
        )


class TestOnlyWhatIsNew:
    async def test_a_new_file_is_the_only_thing_that_runs(self, conn) -> None:  # type: ignore[no-untyped-def]
        await migrate.apply(conn, ALL)
        extra = migrate.Migration("9999_extra.sql", "create table migrate_extra (id int);")
        ran = await migrate.apply(conn, [*ALL, extra])
        assert ran == ["9999_extra.sql"]
        assert await conn.fetchval("select to_regclass('migrate_extra') is not null")

    async def test_a_broken_file_rolls_back_and_is_not_recorded(self, conn) -> None:  # type: ignore[no-untyped-def]
        await migrate.apply(conn, ALL)
        broken = migrate.Migration(
            "9999_broken.sql",
            "create table migrate_half (id int); select 1/0;",
        )
        with pytest.raises(asyncpg.PostgresError):
            await migrate.apply(conn, [*ALL, broken])
        assert await conn.fetchval("select to_regclass('migrate_half') is null")
        assert not await conn.fetchval(
            "select exists (select 1 from schema_migrations where filename = '9999_broken.sql')"
        )
        # and the retry after a fix works
        fixed = migrate.Migration("9999_broken.sql", "create table migrate_half (id int);")
        assert await migrate.apply(conn, [*ALL, fixed]) == ["9999_broken.sql"]

    async def test_an_edited_applied_file_is_refused(self, conn) -> None:  # type: ignore[no-untyped-def]
        await migrate.apply(conn, ALL)
        edited = [*ALL]
        edited[6] = migrate.Migration(ALL[6].filename, ALL[6].sql + "\n-- edited")
        p = await migrate.plan(conn, edited)
        assert p.changed == [ALL[6].filename]
        with pytest.raises(RuntimeError, match="append-only"):
            await migrate.apply(conn, edited)


class TestTheHandMigratedDatabase:
    async def test_an_empty_table_on_a_populated_schema_refuses_to_apply(self, conn) -> None:  # type: ignore[no-untyped-def]
        # Apply by hand, the old way: no tracking rows.
        for m in ALL:
            await conn.execute(m.sql)
        p = await migrate.plan(conn, ALL)
        assert p.empty_table_populated_schema
        with pytest.raises(RuntimeError, match="baseline"):
            await migrate.apply(conn, ALL)

    async def test_baseline_records_without_running_then_apply_is_a_no_op(self, conn) -> None:  # type: ignore[no-untyped-def]
        for m in ALL:
            await conn.execute(m.sql)
        names = await migrate.baseline(conn, ALL)
        assert names == [m.filename for m in ALL]
        assert await conn.fetchval("select bool_and(baseline) from schema_migrations")
        assert await migrate.apply(conn, ALL) == []
        with pytest.raises(RuntimeError, match="not empty"):
            await migrate.baseline(conn, ALL)


class TestAgainstLivedInData:
    """The seeded test database has never had a setting changed; Mumbai had.
    0026 rewrote 'global' settings to 'organisation' before replacing the
    0010 check that only knew two scopes, and the first deploy failed on
    exactly that row. Apply everything before 0026, put Mumbai's shape of
    data in, then apply 0026."""

    async def test_0026_applies_over_changed_settings(self, conn) -> None:  # type: ignore[no-untyped-def]
        before = [m for m in ALL if m.filename < "0026"]
        after = [m for m in ALL if m.filename >= "0026"]
        assert before and after
        await migrate.apply(conn, before)

        outlet = await conn.fetchval(
            "insert into outlets (code, name, city) values ('LIVED-01', 'Lived in', 'Kolkata')"
            " returning id"
        )
        await conn.execute(
            """
            insert into app_settings (key, scope, outlet_id, value) values
                ('ai_review.enabled', 'global', null, 'true'::jsonb),
                ('integrity.phash_max_distance', 'global', null, '9'::jsonb),
                ('sales.petpooja_restaurant_name', 'global', null, '"Akira Ramen"'::jsonb),
                ('jobs.digest_time', 'global', null, '"09:15"'::jsonb),
                ('ai_review.enabled', 'outlet', $1, 'false'::jsonb),
                ('scoring.band.green', 'outlet', $1, '85'::jsonb)
            """,
            outlet,
        )

        # The runner wants the whole checkout; it applies only what is pending.
        assert await migrate.apply(conn, ALL) == [m.filename for m in after]

        rows = await conn.fetch(
            "select key, scope::text, outlet_id, organisation_id"
            "  from app_settings order by key, scope"
        )
        by_scope = {}
        for r in rows:
            by_scope.setdefault(r["scope"], []).append(r)
        # Every changed setting became the development organisation's, and
        # was copied to AKIRA; the job clock stayed global; outlet overrides
        # kept their outlet.
        assert {r["key"] for r in by_scope["global"]} == {"jobs.digest_time"}
        org_keys = {r["key"] for r in by_scope["organisation"]}
        assert org_keys == {
            "ai_review.enabled",
            "integrity.phash_max_distance",
            "sales.petpooja_restaurant_name",
        }
        assert {str(r["organisation_id"]) for r in by_scope["organisation"]} == {
            "a1000000-0000-4000-8000-000000000001",
            "a1000000-0000-4000-8000-000000000002",
        }
        assert all(r["outlet_id"] == outlet for r in by_scope["outlet"])
        # The new outlet, created before 0026 knew about organisations, was
        # backfilled into the development organisation like the seeded ones.
        assert str(
            await conn.fetchval("select organisation_id from outlets where id = $1", outlet)
        ) == ("a1000000-0000-4000-8000-000000000002")
