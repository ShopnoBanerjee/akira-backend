"""Database engine and session.

FastAPI connects with the service role and enforces authorisation in code. RLS
is enabled on every table as defence in depth, but it is not what protects this
application — the dependencies in deps.py are.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def build_engine(settings: Settings) -> AsyncEngine:
    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is not configured.")
    return create_async_engine(
        settings.DATABASE_URL,
        pool_pre_ping=True,
        # Supabase sits behind a pooler and will close idle connections itself;
        # recycling first avoids handing out a socket the server already dropped.
        pool_recycle=1800,
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
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """One session per request, rolled back if the handler raises.

    Services own their transaction boundaries; this only guarantees that a
    failed request never leaves a half-applied write behind.
    """
    async with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
