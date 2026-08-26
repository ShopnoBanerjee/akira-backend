"""Migrations, applied from zero against a real database.

These tests exist because the expensive failures in this system are silent
ones: a business date that disagrees between Python and SQL, an approver who
turns out to be the submitter, an enum value that drifted out of step with the
schema. None of those raise on their own.
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import asyncpg
import pytest

from app.core import enums
from app.core.business_date import OUTLET_TZ, business_date, due_at

pytestmark = pytest.mark.asyncio

IST = OUTLET_TZ


# --------------------------------------------------------------------------
# The rollover, agreed between Python and Postgres
# --------------------------------------------------------------------------


class TestBusinessDateParity:
    """business_date() is expressed in exactly two places. If they ever
    disagree, every report built on the wrong one is quietly wrong."""

    @pytest.mark.parametrize(
        ("moment", "expected"),
        [
            (datetime(2026, 8, 23, 1, 30, tzinfo=IST), date(2026, 8, 22)),
            (datetime(2026, 8, 23, 4, 59, tzinfo=IST), date(2026, 8, 22)),
            (datetime(2026, 8, 23, 5, 0, tzinfo=IST), date(2026, 8, 23)),
            (datetime(2026, 8, 23, 6, 0, tzinfo=IST), date(2026, 8, 23)),
            (datetime(2027, 1, 1, 2, 0, tzinfo=IST), date(2026, 12, 31)),
            (datetime(2028, 3, 1, 2, 0, tzinfo=IST), date(2028, 2, 29)),
        ],
    )
    async def test_spec_examples(
        self, db: asyncpg.Connection, moment: datetime, expected: date
    ) -> None:
        sql_result = await db.fetchval("select business_date($1)", moment)
        assert sql_result == expected
        assert business_date(moment) == expected

    async def test_agrees_across_a_full_day_at_ten_minute_steps(
        self, db: asyncpg.Connection
    ) -> None:
        """Walk a whole trading day and compare every step. A boundary bug that
        a handful of spot checks miss shows up here."""
        probe = datetime(2026, 8, 22, 5, 0, tzinfo=IST)
        end = probe + timedelta(days=1)
        moments: list[datetime] = []
        while probe < end:
            moments.append(probe)
            probe += timedelta(minutes=10)

        rows = await db.fetch(
            "select t, business_date(t) as bd from unnest($1::timestamptz[]) as t",
            moments,
        )
        mismatches = [
            (r["t"].astimezone(IST).isoformat(), r["bd"], business_date(r["t"]))
            for r in rows
            if r["bd"] != business_date(r["t"])
        ]
        assert not mismatches, f"Python and SQL disagree at: {mismatches[:5]}"

    async def test_immutable_so_the_planner_can_use_it_in_indexes(
        self, db: asyncpg.Connection
    ) -> None:
        """Declared immutable on purpose. This is the reason the 05:00 rollover
        is not an admin-editable setting - see docs/DECISIONS.md D9."""
        volatility = await db.fetchval(
            # provolatile is a "char" column, which comes back as bytes.
            "select provolatile::text from pg_proc where proname = 'business_date'"
        )
        assert volatility == "i", "business_date must stay IMMUTABLE"

    async def test_due_at_across_midnight_agrees_with_sql(self, db: asyncpg.Connection) -> None:
        """A closing checklist due 00:30 must land on the next calendar day but
        still belong to the trading day it was scheduled for."""
        day = date(2026, 8, 22)
        computed = due_at(day, time(0, 30))
        assert computed.date() == date(2026, 8, 23)
        assert await db.fetchval("select business_date($1)", computed) == day


# --------------------------------------------------------------------------
# Constraints that carry real weight
# --------------------------------------------------------------------------


async def _fixture_run(db: asyncpg.Connection) -> tuple[str, str, str]:
    """An outlet, an assignment and two people. Returns their ids."""
    outlet = await db.fetchval("select id from outlets where code = 'AKR-NT01'")
    assignment = await db.fetchval(
        "select id from checklist_assignments where outlet_id = $1 limit 1", outlet
    )
    template, version = await db.fetchrow(
        "select template_id, (select version from checklist_templates t"
        "        where t.id = a.template_id) as version"
        "   from checklist_assignments a where a.id = $1",
        assignment,
    )
    for uid, name, role in [
        ("11111111-1111-1111-1111-111111111111", "Lead", "shift_lead"),
        ("22222222-2222-2222-2222-222222222222", "Manager", "ops_manager"),
    ]:
        await db.execute("insert into auth.users (id, email) values ($1::uuid, $2)", uid, name)
        await db.execute(
            "insert into profiles (id, full_name, global_role, is_active)"
            " values ($1::uuid, $2, $3::user_role, true)",
            uid,
            name,
            role,
        )
    return assignment, template, version


class TestSeparationOfDuties:
    """Without this constraint the whole compliance system is theatre."""

    async def test_approver_cannot_be_submitter(self, db: asyncpg.Connection) -> None:
        assignment, template, version = await _fixture_run(db)
        with pytest.raises(asyncpg.CheckViolationError) as exc:
            await db.execute(
                "insert into checklist_runs (assignment_id, template_id, template_version,"
                "  outlet_id, business_date, submitted_by, approved_by)"
                " select $1, $2, $3, outlet_id, '2026-08-26',"
                "  '11111111-1111-1111-1111-111111111111'::uuid,"
                "  '11111111-1111-1111-1111-111111111111'::uuid"
                " from checklist_assignments where id = $1",
                assignment,
                template,
                version,
            )
        assert "approver_is_not_submitter" in str(exc.value)

    async def test_a_different_approver_is_accepted(self, db: asyncpg.Connection) -> None:
        assignment, template, version = await _fixture_run(db)
        run_id = await db.fetchval(
            "insert into checklist_runs (assignment_id, template_id, template_version,"
            "  outlet_id, business_date, submitted_by, approved_by)"
            " select $1, $2, $3, outlet_id, '2026-08-26',"
            "  '11111111-1111-1111-1111-111111111111'::uuid,"
            "  '22222222-2222-2222-2222-222222222222'::uuid"
            " from checklist_assignments where id = $1 returning id",
            assignment,
            template,
            version,
        )
        assert run_id is not None

    async def test_an_unapproved_run_is_fine(self, db: asyncpg.Connection) -> None:
        assignment, template, version = await _fixture_run(db)
        run_id = await db.fetchval(
            "insert into checklist_runs (assignment_id, template_id, template_version,"
            "  outlet_id, business_date, submitted_by)"
            " select $1, $2, $3, outlet_id, '2026-08-26',"
            "  '11111111-1111-1111-1111-111111111111'::uuid"
            " from checklist_assignments where id = $1 returning id",
            assignment,
            template,
            version,
        )
        assert run_id is not None


class TestIdempotency:
    async def test_one_run_per_assignment_per_business_date_and_day_part(
        self, db: asyncpg.Connection
    ) -> None:
        """The 05:00 materialiser must be safe to re-run."""
        assignment, template, version = await _fixture_run(db)
        insert = (
            "insert into checklist_runs (assignment_id, template_id, template_version,"
            "  outlet_id, business_date)"
            " select $1, $2, $3, outlet_id, '2026-08-26'"
            " from checklist_assignments where id = $1"
        )
        await db.execute(insert, assignment, template, version)
        with pytest.raises(asyncpg.UniqueViolationError):
            await db.execute(insert, assignment, template, version)

    async def test_a_sales_file_cannot_be_ingested_twice(self, db: asyncpg.Connection) -> None:
        outlet = await db.fetchval("select id from outlets where code = 'AKR-NT01'")
        insert = (
            "insert into data_uploads (outlet_id, source, original_filename,"
            " storage_path, file_sha256) values ($1, 'petpooja_orders', 'o.xlsx',"
            " 'p/o.xlsx', 'deadbeef')"
        )
        await db.execute(insert, outlet)
        with pytest.raises(asyncpg.UniqueViolationError):
            await db.execute(insert, outlet)


# --------------------------------------------------------------------------
# Enum parity
# --------------------------------------------------------------------------


class TestEnumParity:
    """app/core/enums.py mirrors the Postgres types. A drift here surfaces as a
    mystery 500 at runtime, so catch it at build time instead."""

    @pytest.mark.parametrize(
        ("pg_type", "py_enum"),
        [
            ("user_role", enums.UserRole),
            ("run_status", enums.RunStatus),
            ("item_result", enums.ItemResult),
            ("value_type", enums.ValueType),
            ("frequency", enums.Frequency),
            ("day_part", enums.DayPart),
            ("exception_status", enums.ExceptionStatus),
            ("severity", enums.Severity),
            ("sales_channel", enums.SalesChannel),
            ("upload_status", enums.UploadStatus),
            ("audit_action", enums.AuditAction),
            ("ai_verdict", enums.AiVerdict),
            ("job_status", enums.JobStatus),
            ("inventory_unit", enums.InventoryUnit),
            ("setting_scope", enums.SettingScope),
        ],
    )
    async def test_values_match(
        self, db: asyncpg.Connection, pg_type: str, py_enum: type[enums.StrEnum]
    ) -> None:
        rows = await db.fetch(
            "select e.enumlabel from pg_enum e join pg_type t on t.oid = e.enumtypid"
            " where t.typname = $1 order by e.enumsortorder",
            pg_type,
        )
        assert [r["enumlabel"] for r in rows] == [m.value for m in py_enum]


# --------------------------------------------------------------------------
# Defence in depth
# --------------------------------------------------------------------------


class TestRowLevelSecurity:
    async def test_every_table_has_forced_rls(self, db: asyncpg.Connection) -> None:
        rows = await db.fetch(
            "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace"
            " where n.nspname = 'public' and c.relkind = 'r'"
            "   and not (c.relrowsecurity and c.relforcerowsecurity)"
        )
        assert [r["relname"] for r in rows] == []

    async def test_anon_has_no_table_privileges(self, db: asyncpg.Connection) -> None:
        """A leaked publishable key must not be able to read anything."""
        count = await db.fetchval(
            "select count(*) from information_schema.role_table_grants"
            " where grantee = 'anon' and table_schema = 'public'"
        )
        assert count == 0

    async def test_authenticated_can_only_select(self, db: asyncpg.Connection) -> None:
        """Every write goes through the API, which is the only place the
        business rules and audit writes live."""
        rows = await db.fetch(
            "select distinct privilege_type from information_schema.role_table_grants"
            " where grantee = 'authenticated' and table_schema = 'public'"
        )
        assert {r["privilege_type"] for r in rows} == {"SELECT"}


# --------------------------------------------------------------------------
# Settings and template versions resolve history correctly
# --------------------------------------------------------------------------


class TestSettingResolution:
    async def test_outlet_override_beats_global(self, db: asyncpg.Connection) -> None:
        outlet = await db.fetchval("select id from outlets where code = 'AKR-NT01'")
        other = await db.fetchval("select id from outlets where code = 'AKR-DEV02'")
        await db.execute(
            "insert into app_settings (key, scope, value, effective_from)"
            " values ('scoring.band.green', 'global', '90', '2026-06-01T00:00:00+05:30')"
        )
        await db.execute(
            "insert into app_settings (key, scope, outlet_id, value, effective_from)"
            " values ('scoring.band.green', 'outlet', $1, '93', '2026-08-01T00:00:00+05:30')",
            outlet,
        )
        assert (
            await db.fetchval("select setting_value('scoring.band.green', $1, now())", outlet)
            == "93"
        )
        assert (
            await db.fetchval("select setting_value('scoring.band.green', $1, now())", other)
            == "90"
        )

    async def test_a_past_period_resolves_to_the_value_that_was_live_then(
        self, db: asyncpg.Connection
    ) -> None:
        """The reason app_settings is append-only: historical scores must stay
        reproducible when somebody nudges a weight."""
        await db.execute(
            "insert into app_settings (key, scope, value, effective_from) values"
            " ('scoring.weight.run_score', 'global', '0.40', '2026-06-01T00:00:00+05:30'),"
            " ('scoring.weight.run_score', 'global', '0.50', '2026-08-01T00:00:00+05:30')"
        )
        july = await db.fetchval(
            "select setting_value('scoring.weight.run_score', null, '2026-07-15T12:00:00+05:30')"
        )
        today = await db.fetchval("select setting_value('scoring.weight.run_score', null, now())")
        assert july == "0.40"
        assert today == "0.50"

    async def test_a_future_change_is_not_yet_in_force(self, db: asyncpg.Connection) -> None:
        await db.execute(
            "insert into app_settings (key, scope, value, effective_from) values"
            " ('integrity.phash_max_distance', 'global', '5', '2026-06-01T00:00:00+05:30'),"
            " ('integrity.phash_max_distance', 'global', '3', '2026-12-01T00:00:00+05:30')"
        )
        assert (
            await db.fetchval("select setting_value('integrity.phash_max_distance', null, now())")
            == "5"
        )

    async def test_unknown_key_falls_back_to_the_code_default(self, db: asyncpg.Connection) -> None:
        assert await db.fetchval("select setting_value('no.such.key', null, now())") is None


class TestTemplateItemVersions:
    async def test_every_seeded_item_has_a_retrievable_version(
        self, db: asyncpg.Connection
    ) -> None:
        """template_version on a run must point at something."""
        orphans = await db.fetchval(
            "select count(*) from checklist_template_items i"
            " join checklist_templates t on t.id = i.template_id"
            " where not exists (select 1 from checklist_template_item_versions v"
            "   where v.template_item_id = i.id and v.template_version = t.version)"
        )
        assert orphans == 0

    async def test_an_admin_edit_does_not_reach_backwards(self, db: asyncpg.Connection) -> None:
        """Mark an item critical today; a run recorded before the edit must
        still resolve to the definition it was answered against."""
        item = await db.fetchrow(
            "select i.id, i.template_id, t.version from checklist_template_items i"
            " join checklist_templates t on t.id = i.template_id"
            " where i.is_critical = false and i.deleted_at is null limit 1"
        )
        v1 = await db.fetchval(
            "select id from checklist_template_item_versions"
            " where template_item_id = $1 and template_version = $2",
            item["id"],
            item["version"],
        )

        await db.execute(
            "update checklist_template_items set is_critical = true where id = $1", item["id"]
        )
        await db.execute(
            "update checklist_templates set version = version + 1 where id = $1",
            item["template_id"],
        )
        v2 = await db.fetchval(
            "insert into checklist_template_item_versions"
            " (template_item_id, template_id, template_version, sort_order, title,"
            "  requires_photo, requires_value, is_critical, allow_na)"
            " select id, template_id, $2, sort_order, title, requires_photo,"
            "  requires_value, true, allow_na"
            " from checklist_template_items where id = $1 returning id",
            item["id"],
            item["version"] + 1,
        )

        assert (
            await db.fetchval(
                "select is_critical from checklist_template_item_versions where id = $1", v1
            )
            is False
        ), "the old definition must not change"
        assert (
            await db.fetchval(
                "select is_critical from checklist_template_item_versions where id = $1", v2
            )
            is True
        )
        assert (
            await db.fetchval(
                "select is_critical from checklist_template_items where id = $1", item["id"]
            )
            is True
        ), "the live row reflects the edit"


# --------------------------------------------------------------------------
# The seed itself
# --------------------------------------------------------------------------


class TestSeed:
    @pytest.mark.parametrize(
        ("query", "expected", "what"),
        [
            ("select count(*) from outlets", 2, "outlets, including the dev second outlet"),
            ("select count(*) from sop_categories", 6, "SOP categories"),
            ("select count(*) from checklist_templates", 14, "templates"),
            ("select count(*) from checklist_template_items", 57, "items"),
            ("select count(*) from checklist_assignments", 28, "assignments (14 x 2 outlets)"),
            ("select count(*) from checklist_template_items where requires_photo", 18, "photo"),
            ("select count(*) from checklist_template_items where is_critical", 11, "critical"),
            ("select count(*) from checklist_template_items where requires_value", 23, "valued"),
            ("select count(*) from inventory_items", 151, "catalogue items"),
            ("select count(*) from inventory_departments", 5, "departments"),
        ],
    )
    async def test_counts(
        self, db: asyncpg.Connection, query: str, expected: int, what: str
    ) -> None:
        assert await db.fetchval(query) == expected, what

    async def test_no_template_exceeds_the_fifteen_item_ceiling(
        self, db: asyncpg.Connection
    ) -> None:
        """The known failure mode of checklist programmes is length."""
        worst = await db.fetchrow(
            "select t.name, count(i.id) as n from checklist_templates t"
            " join checklist_template_items i on i.template_id = t.id"
            " group by t.name order by n desc limit 1"
        )
        assert worst["n"] <= 15, f"{worst['name']} has {worst['n']} items"

    async def test_bengali_survives_a_round_trip(self, db: asyncpg.Connection) -> None:
        """Staff read Bengali. Mojibake here makes the app useless to them."""
        row = await db.fetchrow(
            "select name, name_bn from inventory_items where name = 'Ajino Moto'"
        )
        assert row["name_bn"] == "আজিনো মোটো"

        item = await db.fetchval(
            "select title_bn from checklist_template_items where title = 'Daily floor cleaning'"
        )
        assert item == "প্রতিদিন মেঝে পরিষ্কার করা"

    async def test_every_item_carries_a_bengali_title(self, db: asyncpg.Connection) -> None:
        missing = await db.fetchval(
            "select count(*) from checklist_template_items"
            " where title_bn is null or btrim(title_bn) = ''"
        )
        assert missing == 0

    async def test_valued_items_declare_a_type(self, db: asyncpg.Connection) -> None:
        bad = await db.fetchval(
            "select count(*) from checklist_template_items"
            " where requires_value and value_type is null"
        )
        assert bad == 0

    async def test_the_second_outlet_exists_so_multi_outlet_paths_are_exercised(
        self, db: asyncpg.Connection
    ) -> None:
        codes = [r["code"] for r in await db.fetch("select code from outlets order by code")]
        assert codes == ["AKR-DEV02", "AKR-NT01"]

    async def test_outlet_timezone_matches_the_business_date_function(
        self, db: asyncpg.Connection
    ) -> None:
        for tz in await db.fetch("select distinct timezone from outlets"):
            assert ZoneInfo(tz["timezone"]) == IST, (
                "an outlet in another zone needs business_date() parameterised first"
            )
