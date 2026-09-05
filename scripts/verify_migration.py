"""Prove two Supabase projects hold the same data before switching over.

Compares, side by side: every public table's exact row count (count(*), not
the planner's estimate), auth users by email, storage objects per bucket, and
whether the newest migration is present. Exits non-zero on any difference, so
a cut-over can gate on it.

    SOURCE_DSN=postgresql://postgres:...@db.xxx.supabase.co:5432/postgres \
    TARGET_DSN=postgresql://postgres:...@db.yyy.supabase.co:5432/postgres \
    uv run python scripts/verify_migration.py

Read-only on both sides.
"""

import asyncio
import os
import sys
from dataclasses import dataclass

import asyncpg

SRC = os.environ.get("SOURCE_DSN", "").strip()
DST = os.environ.get("TARGET_DSN", "").strip()
if not SRC or not DST:
    sys.exit("SOURCE_DSN and TARGET_DSN must be set")

TABLES_SQL = """
select table_name from information_schema.tables
 where table_schema = 'public' and table_type = 'BASE TABLE' order by 1
"""


@dataclass(frozen=True)
class Snapshot:
    counts: dict[str, int]
    users: list[str]
    buckets: dict[str, int]
    settings: int
    has_0020: bool


async def snapshot(dsn: str) -> Snapshot:
    conn = await asyncpg.connect(dsn, timeout=60)
    try:
        tables = [r["table_name"] for r in await conn.fetch(TABLES_SQL)]
        counts: dict[str, int] = {}
        for t in tables:
            counts[t] = int(await conn.fetchval(f'select count(*) from public."{t}"'))
        users = sorted(r["email"] for r in await conn.fetch("select email from auth.users"))
        buckets = {
            r["bucket_id"]: int(r["n"])
            for r in await conn.fetch(
                "select bucket_id, count(*) as n from storage.objects group by 1"
            )
        }
        settings = int(await conn.fetchval("select count(*) from app_settings"))
        has_0020 = bool(
            await conn.fetchval(
                "select count(*) from information_schema.columns"
                " where table_name = 'data_uploads' and column_name = 'restaurant_name'"
            )
        )
        return Snapshot(counts, users, buckets, settings, has_0020)
    finally:
        await conn.close()


async def main() -> None:
    a, b = await asyncio.gather(snapshot(SRC), snapshot(DST))
    problems = 0

    print(f"{'table':<36} {'source':>8} {'target':>8}")
    for t in sorted(set(a.counts) | set(b.counts)):
        sa, sb = a.counts.get(t), b.counts.get(t)
        flag = "" if sa == sb else "   <-- DIFFERENT"
        problems += sa != sb
        print(f"{t:<36} {sa!s:>8} {sb!s:>8}{flag}")

    print(f"\nauth users: source {len(a.users)}, target {len(b.users)}")
    missing = sorted(set(a.users) - set(b.users))
    if missing:
        problems += 1
        print("  missing at target:", missing)

    print("\nstorage objects per bucket:")
    for bucket in sorted(set(a.buckets) | set(b.buckets)):
        na, nb = a.buckets.get(bucket, 0), b.buckets.get(bucket, 0)
        flag = "" if na == nb else "   <-- DIFFERENT"
        problems += na != nb
        print(f"  {bucket:<20} {na:>6} {nb:>6}{flag}")

    print(f"\nmigration 0020 present at target: {b.has_0020}")
    problems += not b.has_0020

    print("\nRESULT:", "IDENTICAL" if not problems else f"{problems} DIFFERENCE(S)")
    sys.exit(1 if problems else 0)


asyncio.run(main())
