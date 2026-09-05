"""Turn the development database into the production one. Dry run by default.

    uv run python scripts/prod_cutover.py                          # report only
    uv run python scripts/prod_cutover.py --keep-from 2026-09-08   # report, with the cut
    uv run python scripts/prod_cutover.py --keep-from 2026-09-08 --execute --confirm AKIRA

What "production" removes, and what it deliberately keeps:

  REMOVES  the synthetic outlet AKR-DEV02 and everything under it (hard
           delete; every table cascades from outlets)
  REMOVES  AKIRA Safuipara's checklist history BEFORE --keep-from: the
           seeded runs, their items, photos, review views and exceptions.
           Photos' Storage objects are removed after the rows commit.
  REMOVES  every @akira.test account - the nine seeded people and the two
           device accounts - through the Auth Admin API, which cascades to
           profiles, memberships and PINs; their device rows go with them
  KEEPS    the SOP templates, assignments and inventory catalogue (they are
           the real configuration, not sample data)
  KEEPS    every sales row: the Petpooja uploads are AKIRA's real trading
  KEEPS    stock counts, requisitions and consumption windows at Safuipara
  KEEPS    settings history, job_runs and the audit log - append-only by
           design, and the audit of this very script lands there too

It will not execute unless an ACTIVE OWNER with a real (non @akira.test)
email already exists. Invite yourself through the Users screen first, sign
in with that account once, and only then run this - otherwise the last step
deletes the only login that can administer the system.

Owner's instruction on record: "do not deactivate sample data till we go to
prod, its still in development." This script is what "go to prod" runs. Until
that day it is a report.
"""

import argparse
import asyncio
import json
import sys
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import asyncpg
import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402

SYNTHETIC_OUTLET = "AKR-DEV02"
REAL_OUTLET = "AKR-NT01"
TEST_DOMAIN = "@akira.test"
PHOTO_BUCKET = "sop-photos"

DeleteAuthUser = Callable[[uuid.UUID], Awaitable[None]]


@dataclass
class Plan:
    keep_from: date | None
    real_outlet_id: uuid.UUID | None = None
    synthetic_outlet_id: uuid.UUID | None = None
    synthetic_counts: dict[str, int] = field(default_factory=dict)
    runs_to_delete: int = 0
    exceptions_to_delete: int = 0
    photo_paths: list[str] = field(default_factory=list)
    test_accounts: list[tuple[uuid.UUID, str]] = field(default_factory=list)
    test_devices: int = 0
    real_owners: list[str] = field(default_factory=list)
    guard_armed: bool = False

    @property
    def blockers(self) -> list[str]:
        out: list[str] = []
        if not self.real_owners:
            out.append(
                "no ACTIVE owner with a real email exists - invite one, sign in once, re-run"
            )
        if self.keep_from is None:
            out.append("--keep-from YYYY-MM-DD is required to execute")
        if not self.guard_armed:
            out.append("sales.petpooja_restaurant_name is not set (the restaurant guard, D25)")
        return out


# --- Reading -------------------------------------------------------------------


async def plan(conn: asyncpg.Connection, keep_from: date | None) -> Plan:
    p = Plan(keep_from=keep_from)
    p.real_outlet_id = await conn.fetchval("select id from outlets where code = $1", REAL_OUTLET)
    p.synthetic_outlet_id = await conn.fetchval(
        "select id from outlets where code = $1", SYNTHETIC_OUTLET
    )

    if p.synthetic_outlet_id:
        for table in (
            "checklist_runs",
            "checklist_assignments",
            "sop_exceptions",
            "sales_orders",
            "sales_order_items",
            "data_uploads",
            "stock_counts",
            "outlet_devices",
            "outlet_members",
        ):
            p.synthetic_counts[table] = await conn.fetchval(
                f"select count(*) from {table} where outlet_id = $1", p.synthetic_outlet_id
            )

    if p.real_outlet_id and keep_from:
        p.runs_to_delete = await conn.fetchval(
            "select count(*) from checklist_runs where outlet_id = $1 and business_date < $2",
            p.real_outlet_id,
            keep_from,
        )
        p.exceptions_to_delete = await conn.fetchval(
            "select count(*) from sop_exceptions where outlet_id = $1 and business_date < $2",
            p.real_outlet_id,
            keep_from,
        )
        rows = await conn.fetch(
            """
            select ri.photo_path
              from checklist_run_items ri
              join checklist_runs r on r.id = ri.run_id
             where r.outlet_id = $1 and r.business_date < $2 and ri.photo_path is not null
            """,
            p.real_outlet_id,
            keep_from,
        )
        p.photo_paths = [r["photo_path"] for r in rows]

    accounts = await conn.fetch(
        "select id, email from auth.users where lower(email) like $1 order by email",
        "%" + TEST_DOMAIN,
    )
    p.test_accounts = [(a["id"], a["email"]) for a in accounts]
    if p.test_accounts:
        p.test_devices = await conn.fetchval(
            "select count(*) from outlet_devices where auth_user_id = any($1::uuid[])",
            [a[0] for a in p.test_accounts],
        )

    owners = await conn.fetch(
        """
        select u.email
          from profiles pr
          join auth.users u on u.id = pr.id
         where pr.global_role = 'owner' and pr.is_active and pr.deleted_at is null
           and lower(u.email) not like $1
        """,
        "%" + TEST_DOMAIN,
    )
    p.real_owners = [o["email"] for o in owners]

    guard = await conn.fetchval(
        """
        select value::text from app_settings
         where key = 'sales.petpooja_restaurant_name' and outlet_id is null
           and effective_from <= now()
         order by effective_from desc limit 1
        """
    )
    p.guard_armed = bool(guard) and guard not in ('""', "null")
    return p


def describe(p: Plan) -> str:
    lines = ["Production cut-over plan", ""]
    if p.synthetic_outlet_id:
        lines.append(f"REMOVE outlet {SYNTHETIC_OUTLET} and everything under it:")
        for table, n in p.synthetic_counts.items():
            lines.append(f"    {table:<20} {n:>6}")
    else:
        lines.append(f"{SYNTHETIC_OUTLET}: already gone")
    lines.append("")
    if p.keep_from:
        lines.append(f"REMOVE {REAL_OUTLET} checklist history before {p.keep_from.isoformat()}:")
        lines.append(
            f"    checklist_runs       {p.runs_to_delete:>6}   (items, photos, reviews cascade)"
        )
        lines.append(f"    sop_exceptions       {p.exceptions_to_delete:>6}")
        lines.append(f"    photo objects        {len(p.photo_paths):>6}   in {PHOTO_BUCKET}")
    else:
        lines.append(f"{REAL_OUTLET} history: no --keep-from given, nothing would be cut")
    lines.append("")
    lines.append(
        f"REMOVE {len(p.test_accounts)} {TEST_DOMAIN} accounts ({p.test_devices} device rows):"
    )
    for _, email in p.test_accounts:
        lines.append(f"    {email}")
    lines.append("")
    lines.append("KEEP   sales, stock counts, settings, job_runs, audit_log, templates, catalogue")
    lines.append("")
    if p.real_owners:
        lines.append("Real owner(s) who will remain: " + ", ".join(p.real_owners))
    blockers = p.blockers
    if blockers:
        lines.append("")
        lines.append("BLOCKED - will not execute:")
        lines.extend(f"  - {b}" for b in blockers)
    return "\n".join(lines)


# --- Doing ---------------------------------------------------------------------


async def apply_database(conn: asyncpg.Connection, p: Plan, *, actor_note: str) -> None:
    """The database half, in one transaction. Raises on any blocker."""
    if p.blockers:
        raise RuntimeError("blocked: " + "; ".join(p.blockers))
    assert p.keep_from is not None
    async with conn.transaction():
        if p.synthetic_outlet_id:
            await conn.execute("delete from outlets where id = $1", p.synthetic_outlet_id)
        if p.real_outlet_id:
            await conn.execute(
                "delete from sop_exceptions where outlet_id = $1 and business_date < $2",
                p.real_outlet_id,
                p.keep_from,
            )
            await conn.execute(
                "delete from checklist_runs where outlet_id = $1 and business_date < $2",
                p.real_outlet_id,
                p.keep_from,
            )
        if p.test_accounts:
            await conn.execute(
                "delete from outlet_devices where auth_user_id = any($1::uuid[])",
                [a[0] for a in p.test_accounts],
            )
        await conn.execute(
            """
            insert into audit_log (action, entity_table, entity_id, outlet_id, before, after)
            values ('delete', 'outlets', $1, $2, $3::jsonb, $4::jsonb)
            """,
            p.synthetic_outlet_id or p.real_outlet_id,
            p.real_outlet_id,
            json.dumps({"stage": "development", "synthetic_outlet": SYNTHETIC_OUTLET}),
            json.dumps(
                {
                    "stage": "production",
                    "keep_from": p.keep_from.isoformat(),
                    "runs_removed": p.runs_to_delete,
                    "accounts_removed": [e for _, e in p.test_accounts],
                    "note": actor_note,
                }
            ),
        )


async def apply_accounts(p: Plan, delete_auth_user: DeleteAuthUser) -> list[str]:
    """The identity half: each deletion cascades to profiles. Returns failures."""
    failures: list[str] = []
    for user_id, email in p.test_accounts:
        try:
            await delete_auth_user(user_id)
        except Exception as exc:  # report every one, stop for none
            failures.append(f"{email}: {exc}")
    return failures


async def delete_storage_objects(paths: list[str], *, base_url: str, secret_key: str) -> int:
    """Remove photo objects whose rows are gone. Missing objects are not errors:
    the bucket was lost once already (5 Sep 2026)."""
    if not paths:
        return 0
    removed = 0
    headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}
    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(paths), 100):
            chunk = paths[i : i + 100]
            response = await client.request(
                "DELETE",
                f"{base_url.rstrip('/')}/storage/v1/object/{PHOTO_BUCKET}",
                headers=headers,
                json={"prefixes": chunk},
            )
            if response.status_code < 300:
                removed += len(response.json()) if response.content else len(chunk)
    return removed


def supabase_deleter(base_url: str, secret_key: str) -> DeleteAuthUser:
    headers = {"apikey": secret_key, "Authorization": f"Bearer {secret_key}"}

    async def delete(user_id: uuid.UUID) -> None:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.delete(
                f"{base_url.rstrip('/')}/auth/v1/admin/users/{user_id}", headers=headers
            )
        if response.status_code not in (200, 204, 404):
            raise RuntimeError(f"auth admin returned {response.status_code}: {response.text[:200]}")

    return delete


# --- CLI -----------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--keep-from", type=date.fromisoformat, help="first business date to keep")
    parser.add_argument("--execute", action="store_true", help="actually do it")
    parser.add_argument("--confirm", default="", help="must be AKIRA to execute")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.DATABASE_URL:
        sys.exit("DATABASE_URL is not set")
    dsn = settings.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://", 1)
    conn = await asyncpg.connect(dsn)
    try:
        p = await plan(conn, args.keep_from)
        print(describe(p))
        if not args.execute:
            print("\nDry run. Nothing changed. Add --execute --confirm AKIRA to apply.")
            return
        if args.confirm != "AKIRA":
            sys.exit("\nRefusing: --confirm AKIRA is required with --execute.")
        if p.blockers:
            sys.exit("\nRefusing: see BLOCKED above.")

        print("\nApplying the database half...")
        await apply_database(conn, p, actor_note="scripts/prod_cutover.py")
        print("  done")
    finally:
        await conn.close()

    print("Deleting test accounts through the Auth Admin API...")
    failures = await apply_accounts(
        p, supabase_deleter(settings.SUPABASE_URL, settings.SUPABASE_SECRET_KEY)
    )
    print(f"  {len(p.test_accounts) - len(failures)} deleted")
    for f in failures:
        print(f"  FAILED {f}")

    print("Removing photo objects whose rows are gone...")
    removed = await delete_storage_objects(
        p.photo_paths, base_url=settings.SUPABASE_URL, secret_key=settings.SUPABASE_SECRET_KEY
    )
    print(f"  {removed} objects")

    print(
        "\nNow, by hand:\n"
        "  1. Set every real staff member's PIN through the Users screen.\n"
        "  2. Rotate SUPABASE_SECRET_KEY and the database password in the Supabase\n"
        "     dashboard - both have been through chat transcripts - and update the\n"
        "     API's secrets.\n"
        "  3. Capture the 18 reference standards (Reference photos screen).\n"
        "  4. Take a backup: uv run python scripts/backup_db.py"
    )


if __name__ == "__main__":
    asyncio.run(main())
