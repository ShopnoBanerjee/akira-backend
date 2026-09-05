"""Take a restorable backup of the hosted database, the way the region move did.

    uv run python scripts/backup_db.py            # -> local/backups/<stamp>/
    uv run python scripts/backup_db.py --check    # only prove pg_dump is reachable

Two files, because two owners:

  public.dump      our schema and data, custom format (pg_restore)
  auth_users.sql   GoTrue's auth.users + auth.identities as plain INSERTs,
                   so the same UUIDs - which profiles.id and every audit row
                   reference - come back with their password hashes intact

Restore order is auth_users.sql FIRST, then public.dump, then migration 0021
for the grant posture. docs/RUNBOOK_REGION_MOVE.md section 3 is the as-run
record of exactly that and the mistake that taught it.

Why this exists at all: the Supabase free tier keeps no backups of its own,
and the paid tier's daily snapshot cannot be exported. The one time the
project was paused, the database survived because a dump existed and the
photo bucket was lost because nothing dumped it. This script is the database
half of not letting that happen again; Storage is scripts/copy_storage.py.

Reads DATABASE_URL from .env. Never commits anything: local/ is gitignored.
Writes nowhere else.
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.core.config import get_settings  # noqa: E402

BACKUP_ROOT = ROOT / "local" / "backups"

COMMON_FLAGS = ["--no-owner", "--no-privileges", "--quote-all-identifiers"]


def find_pg_dump() -> str:
    """pg_dump from PGBIN, then PATH, then the usual Windows install roots."""
    pgbin = os.environ.get("PGBIN")
    if pgbin:
        candidate = Path(pgbin) / ("pg_dump.exe" if os.name == "nt" else "pg_dump")
        if candidate.exists():
            return str(candidate)
    on_path = shutil.which("pg_dump")
    if on_path:
        return on_path
    if os.name == "nt":
        hits = sorted(glob.glob(r"C:\Program Files\PostgreSQL\*\bin\pg_dump.exe"), reverse=True)
        if hits:
            return hits[0]
    sys.exit("pg_dump not found. Set PGBIN to the PostgreSQL bin directory.")


def connection_parts(database_url: str) -> tuple[str, int, str, str, str]:
    url = urlparse(database_url.replace("postgresql+asyncpg://", "postgresql://", 1))
    if not url.hostname or not url.username or not url.password:
        sys.exit("DATABASE_URL must carry host, user and password.")
    return (
        url.hostname,
        url.port or 5432,
        url.username,
        unquote(url.password),
        (url.path or "/postgres").lstrip("/") or "postgres",
    )


def run(cmd: list[str], env: dict[str, str]) -> None:
    printable = " ".join(c if " " not in c else f'"{c}"' for c in cmd)
    print(f"  $ {printable}")
    completed = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if completed.returncode != 0:
        sys.exit(f"pg_dump failed ({completed.returncode}):\n{completed.stderr.strip()}")
    if completed.stderr.strip():
        print(completed.stderr.strip())


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--check", action="store_true", help="only verify pg_dump and the connection"
    )
    args = parser.parse_args()

    settings = get_settings()
    if not settings.DATABASE_URL:
        sys.exit("DATABASE_URL is not set in .env")
    host, port, user, password, dbname = connection_parts(settings.DATABASE_URL)
    pg_dump = find_pg_dump()
    env = {**os.environ, "PGPASSWORD": password}
    base = [pg_dump, "-h", host, "-p", str(port), "-U", user, "-d", dbname, *COMMON_FLAGS]

    print(f"pg_dump: {pg_dump}")
    print(f"target : {user}@{host}:{port}/{dbname}")

    if args.check:
        completed = subprocess.run(
            [*base, "--schema-only", "--schema=public", "--table=public.outlets"],
            env=env,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            sys.exit(f"cannot reach the database:\n{completed.stderr.strip()}")
        print("ok: pg_dump reaches the database")
        return

    stamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%SZ")
    out = BACKUP_ROOT / stamp
    out.mkdir(parents=True, exist_ok=False)

    public_dump = out / "public.dump"
    auth_sql = out / "auth_users.sql"
    run([*base, "--schema=public", "-Fc", "-f", str(public_dump)], env)
    run(
        [
            *base,
            "--data-only",
            "--column-inserts",
            "--table=auth.users",
            "--table=auth.identities",
            "-f",
            str(auth_sql),
        ],
        env,
    )

    # A dump that pg_restore cannot list is not a backup.
    pg_restore = str(Path(pg_dump).with_name(Path(pg_dump).name.replace("pg_dump", "pg_restore")))
    listing = subprocess.run(
        [pg_restore, "--list", str(public_dump)], env=env, capture_output=True, text=True
    )
    if listing.returncode != 0:
        sys.exit(f"pg_restore cannot read the dump just written:\n{listing.stderr.strip()}")
    tables = sum(1 for line in listing.stdout.splitlines() if " TABLE DATA " in line)
    users = sum(
        1
        for line in auth_sql.read_text(encoding="utf-8").splitlines()
        if line.startswith('INSERT INTO "auth"."users"')
    )

    print()
    print(f"wrote {out.relative_to(ROOT)}")
    size_mb = public_dump.stat().st_size / 1_048_576
    print(f"  public.dump     {size_mb:.1f} MB, {tables} tables with data")
    print(f"  auth_users.sql  {auth_sql.stat().st_size / 1024:.0f} KB, {users} auth users")
    print()
    print("Storage is not in this backup. Run scripts/copy_storage.py for the buckets.")
    print("Restore: auth_users.sql first, then public.dump, then migration 0021.")


if __name__ == "__main__":
    main()
