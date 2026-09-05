"""Database engine and session.

FastAPI connects with the service role and enforces authorisation in code. RLS
is enabled on every table as defence in depth, but it is not what protects this
application — the dependencies in deps.py are.

---

**Everything below is about round trips, because that is the only cost that
matters here.** Measured against the hosted database from this machine:

    TCP handshake ................ 152 ms
    'select 1', warm connection .. 151 ms
    the server's own execution ... 0.1-0.2 ms

The database does its work in a fifth of a millisecond. Every millisecond after
that is the wire. So the only performance question worth asking of this file is
*how many times does one request talk to Postgres*, and the answer used to be
six and a half times for a single query:

    pool_pre_ping=True, default transaction ..... 989 ms   6.6 trips
    pre_ping off ................................ 494 ms   3.3 trips
    pre_ping off, autocommit for reads .......... 152 ms   1.0 trip

`pool_pre_ping` sends a liveness check before handing over a pooled
connection. On a local database that is free. Here it cost 412 ms on every
single request, to guard against a stale socket that `pool_recycle` already
prevents — so the recycle window is now shorter than any sensible idle timeout
and the ping is gone.

The remaining two trips were `BEGIN` and `ROLLBACK` around statements that only
read. A GET does not need a transaction, so it does not open one.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Concatenate

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: How long a pooled connection lives before it is retired and rebuilt.
#:
#: This was 240 s, chosen to stay under a pooler's idle timeout so that
#: `pool_pre_ping` could go. But this app connects to Postgres DIRECTLY — no
#: pooler, no idle timeout — and a new connection to the hosted database costs
#: 3.5 s (DNS, IPv6, TLS, auth; measured, median of four), not the 152 ms the
#: earlier note assumed for a bare handshake. At 240 s every connection in the
#: pool was torn down and rebuilt every four minutes, and whichever request
#: drew it next stalled for those 3.5 s. That is what "the API is randomly
#: slow" looked like from a browser.
#:
#: Half an hour, with TCP keepalives (below) so a NAT between here and there
#: cannot silently drop an idle socket in the meantime. A connection that dies
#: anyway fails one request and is invalidated; the pool does not keep handing
#: it out.
POOL_RECYCLE_SECONDS = 1800

#: Asked of the SERVER, so the keepalive probes come from Postgres's side and
#: cross whatever NAT sits in between. Idle 60 s, then a probe every 10 s,
#: three misses and the server gives up — well inside the recycle window.
_KEEPALIVE_SETTINGS = {
    "tcp_keepalives_idle": "60",
    "tcp_keepalives_interval": "10",
    "tcp_keepalives_count": "3",
}

#: Connections opened at startup, in parallel, before the first request. Each
#: costs 3.5 s serially; opened together they cost one 3.5 s that nobody is
#: waiting on. Sized to the dashboard's fan-out plus one for the scheduler.
WARM_CONNECTIONS = 6

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_read_session_factory: async_sessionmaker[AsyncSession] | None = None


def build_engine(settings: Settings) -> AsyncEngine:
    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured.")
    return create_async_engine(
        settings.DATABASE_URL,
        # Deliberately NOT pool_pre_ping. See the module docstring: it cost a
        # round trip on every request to protect against something
        # pool_recycle already prevents.
        pool_recycle=POOL_RECYCLE_SECONDS,
        # Sized for `read_with` fan-out: the health card puts six independent
        # reads on the wire at once, each on its own connection for the
        # duration of one round trip. Three managers opening the dashboard
        # together is eighteen; the overflow absorbs a fourth. Supabase's
        # smallest compute allows sixty direct connections, and the scheduler
        # holds one or two.
        pool_size=10,
        max_overflow=15,
        # Last in, first out: the connection just returned is the one handed
        # out next. asyncpg caches prepared statements and introspected types
        # PER CONNECTION, and a statement on a cold connection costs up to four
        # round trips (parse/describe, a type lookup for each new enum, then
        # execute) against one on a warm one. FIFO rotated every request onto
        # the coldest of ten connections; LIFO keeps a few of them hot and
        # lets the rest sit idle for the fan-out to use.
        pool_use_lifo=True,
        connect_args={"server_settings": _KEEPALIVE_SETTINGS},
        echo=settings.ENV == "local" and settings.SQL_ECHO,
    )


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        _engine = build_engine(get_settings())
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Sessions that own a transaction. Everything that writes uses these.

    An audit row has to join the caller's transaction, so that it can never
    survive a rolled-back change — which is the whole reason writes are not
    autocommit.
    """
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)
    return _session_factory


def get_read_session_factory() -> async_sessionmaker[AsyncSession]:
    """Sessions for statements that only read.

    Autocommit, so no `BEGIN` and no `ROLLBACK` — two round trips that bought
    nothing, because a single `SELECT` is atomic on its own.

    The trade is real and worth naming: statements in one of these sessions do
    not share a snapshot, so a handler issuing several reads could see them at
    slightly different instants. For the screens here that is invisible — the
    numbers move continuously anyway — and the handlers that most needed
    consistency were the ones collapsed into a single statement.
    """
    global _read_session_factory
    if _read_session_factory is None:
        _read_session_factory = async_sessionmaker(
            # Same pool, different isolation. execution_options returns a
            # facade over the existing engine rather than a second pool.
            get_engine().execution_options(isolation_level="AUTOCOMMIT"),
            expire_on_commit=False,
            autoflush=False,
        )
    return _read_session_factory


async def warm_pool(engine: AsyncEngine, count: int = WARM_CONNECTIONS) -> int:
    """Open `count` connections at once and return them to the pool.

    Without this the first `count` requests after a deploy each pay a full
    connection setup — 3.5 s to the hosted database — and the dashboard's
    fan-out pays several at once. Failures are logged and swallowed: a
    database that is unreachable at boot is /readyz's problem to report, not
    a reason to refuse to start. Returns how many connections were opened.
    """

    # Held open together so the pool actually ends up with `count` distinct
    # connections rather than one, reused `count` times.
    holders: list[AsyncConnection] = []
    opened = 0
    try:
        for conn in await asyncio.gather(
            *[engine.connect() for _ in range(count)], return_exceptions=True
        ):
            if isinstance(conn, BaseException):
                logger.warning("pool warm-up: a connection failed: %s", conn)
                continue
            holders.append(conn)
        results = await asyncio.gather(
            *[c.execute(text("select 1")) for c in holders], return_exceptions=True
        )
        opened = sum(1 for r in results if not isinstance(r, BaseException))
    finally:
        for c in holders:
            await c.close()
    return opened


async def read_with[**P, T](
    db: AsyncSession,
    fn: Callable[Concatenate[AsyncSession, P], Awaitable[T]],
    /,
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Run one read-only call on a pooled connection of its own.

    A session executes one statement at a time, so a handler that needs five
    independent aggregates waits five round trips in a row — 1.5 s with the
    database where it is. Giving each its own connection lets `asyncio.gather`
    put them all on the wire together, and the wall time becomes ONE round
    trip. Nothing about the statements changes; only when they leave.

    The sibling session is bound to the same engine as `db`, so a test that
    hands in a session on a throwaway database fans out on that database and
    not on whatever DATABASE_URL points at. It cannot see `db`'s uncommitted
    work — it is another connection — so callers commit first, or keep reads
    that must share a snapshot on `db` itself.

    For reads only: each runs in its own autocommit session, exactly as a
    GET's statements already do. The price is connections — a fan-out of six
    holds six for the length of one round trip — which is what the pool size
    above allows for.
    """
    bind = db.bind
    engine = bind if isinstance(bind, AsyncEngine) else get_engine()
    session = AsyncSession(
        engine.execution_options(isolation_level="AUTOCOMMIT"),
        expire_on_commit=False,
        autoflush=False,
    )
    async with session:
        return await fn(session, *args, **kwargs)


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request, rolled back if the handler raises.

    **A read-only request gets a read-only session.** GET and HEAD cannot
    change anything by definition, so opening a transaction around them spends
    two round trips to protect nothing. Everything else gets the transactional
    session, and services keep owning their own commit boundaries.

    Chosen by method in this one place rather than by a second dependency on
    every read route: one rule, in one file, that cannot drift endpoint by
    endpoint. A GET that genuinely needs atomicity can still open one
    explicitly with `async with db.begin():`.
    """
    read_only = request.method in ("GET", "HEAD")
    factory = get_read_session_factory() if read_only else get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _session_factory, _read_session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
    _read_session_factory = None
