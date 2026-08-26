"""Reading the value a setting held at a moment.

The registry in ``settings_registry`` owns each key's meaning and default; the
``app_settings`` table owns its history (D9). This module is the one place that
joins the two, so nothing else has to remember the resolution order:

    outlet override  >  global row  >  registry default

all evaluated at a point in time, because the table is append-only with an
effective date. Scoring a night from three months ago must use the weights that
were live that night, not today's.

A key absent from the registry raises rather than returning None. Settings are
declared in code; a typo should fail loudly at the call site rather than quietly
resolve to nothing halfway through a scheduled job.
"""

import json
import uuid
from datetime import datetime, time
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_registry import REGISTRY

__all__ = [
    "resolve",
    "resolve_bool",
    "resolve_float",
    "resolve_int",
    "resolve_many",
    "resolve_time",
]

_RESOLVE_SQL = text(
    "select setting_value(:key, cast(:outlet_id as uuid), "
    "coalesce(cast(:at as timestamptz), now())) as value"
)


def _decode(raw: Any) -> Any:
    """asyncpg hands jsonb back as text; SQLAlchemy sometimes decodes it."""
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


async def resolve(
    db: AsyncSession,
    key: str,
    *,
    outlet_id: uuid.UUID | None = None,
    at: datetime | None = None,
) -> Any:
    """The value in force for ``key``, falling back to the registry default."""
    definition = REGISTRY.get(key)
    if definition is None:
        raise KeyError(f"{key} is not a declared setting. See app/core/settings_registry.py.")
    raw = (await db.execute(_RESOLVE_SQL, {"key": key, "outlet_id": outlet_id, "at": at})).scalar()
    if raw is None:
        return definition.default
    value = _decode(raw)
    return definition.default if value is None else value


async def resolve_many(
    db: AsyncSession,
    keys: list[str],
    *,
    outlet_id: uuid.UUID | None = None,
    at: datetime | None = None,
) -> dict[str, Any]:
    """Several keys in one round trip. A job resolving eight thresholds one at
    a time is eight round trips it does not need."""
    unknown = [k for k in keys if k not in REGISTRY]
    if unknown:
        raise KeyError(f"Not declared settings: {', '.join(sorted(unknown))}")
    rows = (
        await db.execute(
            text(
                """
                select k as key,
                       setting_value(k, cast(:outlet_id as uuid),
                                     coalesce(cast(:at as timestamptz), now())) as value
                  from unnest(cast(:keys as text[])) as k
                """
            ),
            {"keys": keys, "outlet_id": outlet_id, "at": at},
        )
    ).mappings()
    resolved: dict[str, Any] = {}
    for row in rows:
        value = _decode(row["value"])
        resolved[row["key"]] = REGISTRY[row["key"]].default if value is None else value
    # unnest drops nothing, but a key that somehow missed still gets its default
    # rather than a KeyError three frames away in the caller.
    for key in keys:
        resolved.setdefault(key, REGISTRY[key].default)
    return resolved


async def resolve_int(
    db: AsyncSession,
    key: str,
    *,
    outlet_id: uuid.UUID | None = None,
    at: datetime | None = None,
) -> int:
    return int(await resolve(db, key, outlet_id=outlet_id, at=at))


async def resolve_float(
    db: AsyncSession,
    key: str,
    *,
    outlet_id: uuid.UUID | None = None,
    at: datetime | None = None,
) -> float:
    return float(await resolve(db, key, outlet_id=outlet_id, at=at))


async def resolve_bool(
    db: AsyncSession,
    key: str,
    *,
    outlet_id: uuid.UUID | None = None,
    at: datetime | None = None,
) -> bool:
    return bool(await resolve(db, key, outlet_id=outlet_id, at=at))


def parse_time(value: str) -> time:
    """ "05:00" to a time. The registry has already validated the shape."""
    hour, minute = value.split(":")
    return time(hour=int(hour), minute=int(minute))


async def resolve_time(
    db: AsyncSession,
    key: str,
    *,
    outlet_id: uuid.UUID | None = None,
    at: datetime | None = None,
) -> time:
    return parse_time(str(await resolve(db, key, outlet_id=outlet_id, at=at)))
