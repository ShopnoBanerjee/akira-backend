"""warm_pool opens distinct connections, together, and hands them back.

The point of warming is that the pool ends up holding N ready connections —
not one connection exercised N times, which is what a naive loop of
connect/close produces. So the assertion is on the pool's own count.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.db import read_with, warm_pool

pytestmark = pytest.mark.asyncio


async def test_the_pool_holds_that_many_ready_connections(migrated_db: str) -> None:
    engine = create_async_engine(
        migrated_db.replace("postgresql://", "postgresql+asyncpg://"), pool_size=6
    )
    try:
        assert engine.pool.checkedin() == 0
        opened = await warm_pool(engine, 4)
        assert opened == 4
        assert engine.pool.checkedin() == 4, "four distinct connections, all returned"
    finally:
        await engine.dispose()


async def test_a_bad_database_does_not_stop_startup() -> None:
    """An unreachable database at boot is /readyz's problem to report."""
    engine = create_async_engine("postgresql+asyncpg://nobody@127.0.0.1:1/none")
    try:
        assert await warm_pool(engine, 2) == 0
    finally:
        await engine.dispose()


async def test_read_with_fans_out_on_the_callers_engine(migrated_db: str) -> None:
    """A sibling session must be bound to the same database as the session it
    was given — a test database here — and never to DATABASE_URL."""
    from sqlalchemy.ext.asyncio import AsyncSession

    engine = create_async_engine(migrated_db.replace("postgresql://", "postgresql+asyncpg://"))
    try:
        async with AsyncSession(engine) as db:

            async def whoami(session: AsyncSession) -> str:
                return str((await session.execute(text("select current_database()"))).scalar())

            here = await whoami(db)
            there = await read_with(db, whoami)
            assert here == there
            assert here.endswith("_test") or "akira_ops_test" in here
    finally:
        await engine.dispose()
