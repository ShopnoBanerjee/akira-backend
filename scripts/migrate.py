"""Apply the migrations that have not been applied yet, in order, tracked.

    uv run python scripts/migrate.py --plan               # what would run
    uv run python scripts/migrate.py --apply              # run it
    uv run python scripts/migrate.py --baseline           # one-time: mark every
                                                          #   file applied, run none

Until P25 the migrations were applied by hand with psql, in filename order,
and nothing recorded which had run; the runbook carried "never re-run 0007"
as a warning because re-running it collides on `create policy`. A pipeline
cannot work from a warning. This script keeps `schema_migrations` - one row
per applied file, with its checksum - and applies only what is missing.

Rules:

- Each file runs inside its own transaction and is recorded in the same
  transaction, so a failed file leaves nothing behind and the next run
  retries it. (Postgres DDL is transactional; every file in this repo has
  been checked to run as one statement batch.)
- An applied file whose checksum has changed is an error, not a silent
  re-run: migrations are append-only (CLAUDE.md), and a changed one means
  somebody edited history.
- `--baseline` exists for the database that already had everything applied
  by hand. It records the files present without running them. Run it once,
  against Mumbai, before the first pipeline deploy; the plan output says
  loudly when the table is empty and the schema is not.

Reads the DSN from MIGRATIONS_DATABASE_URL, else DATABASE_URL. From a GitHub
runner use the session pooler (IPv4); the direct host is IPv6-only.
"""

import argparse
import asyncio
import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import asyncpg

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = ROOT / "supabase" / "migrations"

CREATE_TABLE = """
create table if not exists schema_migrations (
    filename    text primary key,
    checksum    text not null,
    applied_at  timestamptz not null default now(),
    applied_by  text not null default current_user,
    baseline    boolean not null default false
)
"""

#: Nobody reads this table but the migrator, and the platform's default ACL
#: would otherwise hand `anon` every privilege on it (SECURITY.md #22).
LOCK_DOWN = """
alter table schema_migrations enable row level security;
alter table schema_migrations force row level security;
revoke all on table schema_migrations from anon;
revoke all on table schema_migrations from authenticated;
drop policy if exists schema_migrations_nobody on schema_migrations;
create policy schema_migrations_nobody on schema_migrations for select using (false);
"""


@dataclass(frozen=True)
class Migration:
    filename: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def load_migrations(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    files = sorted(p for p in directory.glob("[0-9]*.sql"))
    return [Migration(p.name, p.read_text(encoding="utf-8")) for p in files]


def normalised_dsn(raw: str) -> str:
    return raw.replace("postgresql+asyncpg://", "postgresql://", 1)


async def ensure_table(conn: asyncpg.Connection) -> None:
    await conn.execute(CREATE_TABLE)
    await conn.execute(LOCK_DOWN)


async def applied(conn: asyncpg.Connection) -> dict[str, str]:
    rows = await conn.fetch("select filename, checksum from schema_migrations")
    return {r["filename"]: r["checksum"] for r in rows}


async def schema_looks_populated(conn: asyncpg.Connection) -> bool:
    return bool(
        await conn.fetchval(
            "select exists (select 1 from information_schema.tables"
            " where table_schema = 'public' and table_name = 'outlets')"
        )
    )


@dataclass(frozen=True)
class Plan:
    pending: list[Migration]
    changed: list[str]
    unknown_in_db: list[str]
    empty_table_populated_schema: bool

    @property
    def problems(self) -> list[str]:
        out = []
        if self.changed:
            out.append(
                "applied migrations whose file has since changed (append-only!): "
                + ", ".join(self.changed)
            )
        if self.unknown_in_db:
            out.append(
                "schema_migrations names files this checkout does not have: "
                + ", ".join(self.unknown_in_db)
            )
        if self.empty_table_populated_schema:
            out.append(
                "schema_migrations is empty but the schema is not - this database was "
                "migrated by hand. Run --baseline once before --apply."
            )
        return out


async def plan(conn: asyncpg.Connection, migrations: list[Migration]) -> Plan:
    await ensure_table(conn)
    done = await applied(conn)
    by_name = {m.filename: m for m in migrations}
    return Plan(
        pending=[m for m in migrations if m.filename not in done],
        changed=[
            name
            for name, sum_ in done.items()
            if name in by_name and by_name[name].checksum != sum_
        ],
        unknown_in_db=[name for name in done if name not in by_name],
        empty_table_populated_schema=(not done) and await schema_looks_populated(conn),
    )


async def apply(conn: asyncpg.Connection, migrations: list[Migration]) -> list[str]:
    """Run every pending file, each in its own transaction. Returns what ran."""
    p = await plan(conn, migrations)
    if p.problems:
        raise RuntimeError("; ".join(p.problems))
    ran: list[str] = []
    for m in p.pending:
        async with conn.transaction():
            await conn.execute(m.sql)
            await conn.execute(
                "insert into schema_migrations (filename, checksum) values ($1, $2)",
                m.filename,
                m.checksum,
            )
        ran.append(m.filename)
    # 0021's grant sweep and default privileges hand `authenticated` SELECT on
    # every table, this one included, so the lock-down is re-asserted after
    # the files have run rather than trusted from before they did.
    await conn.execute(LOCK_DOWN)
    return ran


async def baseline(conn: asyncpg.Connection, migrations: list[Migration]) -> list[str]:
    """Record every file as applied without running it. For the hand-migrated
    database only; refuses if anything is already recorded."""
    await ensure_table(conn)
    if await applied(conn):
        raise RuntimeError("schema_migrations is not empty; baseline is for a fresh table only")
    async with conn.transaction():
        for m in migrations:
            await conn.execute(
                "insert into schema_migrations (filename, checksum, baseline)"
                " values ($1, $2, true)",
                m.filename,
                m.checksum,
            )
    return [m.filename for m in migrations]


async def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--baseline", action="store_true")
    args = parser.parse_args()

    raw = os.environ.get("MIGRATIONS_DATABASE_URL") or os.environ.get("DATABASE_URL", "")
    if not raw:
        # .env is for local use; the pipeline passes the URL in the environment.
        env_file = ROOT / ".env"
        if env_file.exists():
            for line in env_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("DATABASE_URL="):
                    raw = line.split("=", 1)[1].strip()
    if not raw:
        print("no MIGRATIONS_DATABASE_URL or DATABASE_URL", file=sys.stderr)
        return 2

    migrations = load_migrations()
    conn = await asyncpg.connect(normalised_dsn(raw), timeout=30)
    try:
        host = urlparse(normalised_dsn(raw)).hostname
        print(f"database: {host}; {len(migrations)} migration files in the checkout")
        if args.baseline:
            names = await baseline(conn, migrations)
            print(f"baseline: recorded {len(names)} files as applied ({names[0]} .. {names[-1]})")
            return 0
        p = await plan(conn, migrations)
        for problem in p.problems:
            print(f"PROBLEM: {problem}")
        if p.pending:
            print("pending:")
            for m in p.pending:
                print(f"  {m.filename}")
        else:
            print("pending: none")
        if args.plan:
            return 1 if p.problems else 0
        ran = await apply(conn, migrations)
        print(f"applied {len(ran)}: {', '.join(ran) if ran else '-'}")
        return 0
    finally:
        await conn.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
