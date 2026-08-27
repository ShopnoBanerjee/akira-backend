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

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings

#: Shorter than any idle timeout a pooler in front of Postgres is likely to
#: use. This is what makes dropping `pool_pre_ping` safe: rather than paying a
#: round trip to ask whether a connection is alive, the pool retires it before
#: it can go stale.
POOL_RECYCLE_SECONDS = 240

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
        pool_size=5,
        max_overflow=10,
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
