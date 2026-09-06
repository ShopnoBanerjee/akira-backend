"""Test fixtures.

The database fixtures build a throwaway database from zero on every session:
shim, then every migration in filename order, then the seed. That is the only
way to know the migrations actually apply cleanly rather than merely that some
long-lived dev database happens to have the right shape.

Point TEST_DATABASE_URL at a local cluster. Never at Supabase — these fixtures
drop the database they create.
"""

import asyncio
import os
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from app.core.deps import CurrentUser, OutletMembership
from app.core.enums import UserRole

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("[0-9]*.sql"))
SHIM = ROOT / "supabase" / "local" / "0000_local_auth_shim.sql"
SEEDS = sorted((ROOT / "supabase" / "seed").glob("[0-9]*.sql"))

DEFAULT_ADMIN_DSN = "postgresql://postgres@127.0.0.1:5433/postgres"
TEST_DB_NAME = "akira_ops_test"


def isolated_settings(**overrides: object):  # type: ignore[no-untyped-def]
    """Settings built from declared defaults, ignoring the developer's .env.

    A bare Settings() reads that file, so a test asserting a default passes or
    fails depending on whose machine it runs on. Worse, a locally configured
    AI_REVIEW_PROVIDER once sent a dispatch test at the real network. Anything
    in this suite that constructs Settings should come through here.
    """
    from app.core.config import Settings

    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def admin_dsn() -> str:
    dsn = os.environ.get("TEST_DATABASE_URL", DEFAULT_ADMIN_DSN)
    # Accept a SQLAlchemy-style URL for convenience.
    return dsn.replace("postgresql+asyncpg://", "postgresql://")


def _swap_database(dsn: str, name: str) -> str:
    return re.sub(r"/[^/?]+(\?|$)", f"/{name}\\1", dsn, count=1)


async def _probe() -> str | None:
    """Return a reason to skip, or None if a usable server is reachable."""
    try:
        conn = await asyncio.wait_for(asyncpg.connect(admin_dsn()), timeout=5)
    except Exception as exc:
        return f"no Postgres at {admin_dsn()}: {type(exc).__name__}: {exc}"
    await conn.close()
    return None


@pytest.fixture(scope="session")
def event_loop():  # type: ignore[no-untyped-def]
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


#: The development organisation (0026): where every seeded outlet lives.
DEV_ORG = uuid.UUID("a1000000-0000-4000-8000-000000000002")
_dev_outlets: frozenset[uuid.UUID] = frozenset()


def dev_outlet_ids() -> frozenset[uuid.UUID]:
    """Every seeded outlet's id. A test that builds an owner by hand gives it
    these as `organisation_outlet_ids`, so its reach matches what the identity
    loader would compute (D33). Filled once the session database is built."""
    return _dev_outlets


def dev_user(
    role: UserRole = UserRole.OWNER,
    *,
    profile_id: uuid.UUID | None = None,
    full_name: str = "Owner",
    email: str | None = None,
    memberships: list[OutletMembership] | None = None,
) -> CurrentUser:
    """A caller of the development organisation, as the identity loader would
    build one."""
    return CurrentUser(
        profile_id=profile_id or uuid.uuid4(),
        full_name=full_name,
        email=email,
        global_role=role,
        is_active=True,
        memberships=memberships or [],
        organisation_id=DEV_ORG,
        organisation_slug="akira-dev",
        organisation_name="AKIRA (development)",
        organisation_outlet_ids=dev_outlet_ids(),
    )


@pytest_asyncio.fixture(scope="session")
async def migrated_db() -> AsyncIterator[str]:
    """A freshly built database. Yields its DSN."""
    skip = await _probe()
    if skip:
        pytest.skip(skip)

    admin = await asyncpg.connect(admin_dsn())
    try:
        await admin.execute(f'drop database if exists "{TEST_DB_NAME}" with (force)')
        await admin.execute(f'create database "{TEST_DB_NAME}"')
    finally:
        await admin.close()

    dsn = _swap_database(admin_dsn(), TEST_DB_NAME)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(SHIM.read_text(encoding="utf-8"))
        for path in MIGRATIONS:
            try:
                await conn.execute(path.read_text(encoding="utf-8"))
            except Exception as exc:
                pytest.fail(f"migration {path.name} failed to apply: {exc}")
        for path in SEEDS:
            try:
                await conn.execute(path.read_text(encoding="utf-8"))
            except Exception as exc:
                pytest.fail(f"seed {path.name} failed to apply: {exc}")
        global _dev_outlets
        _dev_outlets = frozenset(
            r["id"]
            for r in await conn.fetch(
                "select id from outlets where organisation_id = $1 and deleted_at is null", DEV_ORG
            )
        )
    finally:
        await conn.close()

    yield dsn

    admin = await asyncpg.connect(admin_dsn())
    try:
        await admin.execute(f'drop database if exists "{TEST_DB_NAME}" with (force)')
    finally:
        await admin.close()


@pytest_asyncio.fixture
async def db(migrated_db: str) -> AsyncIterator[asyncpg.Connection]:
    """A connection whose writes are rolled back, so tests cannot leak into
    each other."""
    conn = await asyncpg.connect(migrated_db)
    tx = conn.transaction()
    await tx.start()
    try:
        yield conn
    finally:
        await tx.rollback()
        await conn.close()
