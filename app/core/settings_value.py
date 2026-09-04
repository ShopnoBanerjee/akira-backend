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

from app.core.settings_registry import REGISTRY, SettingDef

__all__ = [
    "decode_stored",
    "resolve",
    "resolve_bool",
    "resolve_float",
    "resolve_int",
    "resolve_many",
    "resolve_many_outlets",
    "resolve_time",
]

_RESOLVE_SQL = text(
    "select setting_value(:key, cast(:outlet_id as uuid), "
    "coalesce(cast(:at as timestamptz), now())) as value"
)


def decode_stored(raw: Any, definition: SettingDef) -> Any:
    """One stored jsonb value as Python, whichever way the driver handed it over.

    jsonb arrives either as its JSON *text* (``'"Akira Ramen"'``) or already
    decoded to the bare value (``'Akira Ramen'``), depending on whether the
    driver's jsonb codec ran. For numbers and booleans the two are
    distinguishable by type — a decoded one is not a `str` at all — which is
    why this went unnoticed: every setting anybody had ever changed was a
    number or a boolean.

    A *string* setting is not distinguishable that way, and `json.loads` is the
    wrong tool for telling them apart. It raises on an already-decoded name,
    which is a crash, and worse it SUCCEEDS on a restaurant called "123",
    quietly resolving a text setting to the integer 123. So string and time
    values are unwrapped only when they carry the quotes that mark them as
    JSON text, and taken as-is otherwise.
    """
    if not isinstance(raw, str):
        return raw
    if definition.type in ("string", "time"):
        if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
            return json.loads(raw)
        return raw
    return json.loads(raw)


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
    value = decode_stored(raw, definition)
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
        value = decode_stored(row["value"], REGISTRY[row["key"]])
        resolved[row["key"]] = REGISTRY[row["key"]].default if value is None else value
    # unnest drops nothing, but a key that somehow missed still gets its default
    # rather than a KeyError three frames away in the caller.
    for key in keys:
        resolved.setdefault(key, REGISTRY[key].default)
    return resolved


async def resolve_many_outlets(
    db: AsyncSession,
    keys: list[str],
    *,
    outlet_ids: list[uuid.UUID],
    at: datetime | None = None,
) -> dict[uuid.UUID, dict[str, Any]]:
    """The same keys, resolved separately for each outlet, in one round trip.

    Each outlet still gets its own answer — that is the whole point of an
    override — but the dashboard was asking for them one outlet at a time,
    which is a round trip per outlet to resolve seven constants. Cross-joining
    the two unnests lets Postgres evaluate the lot in one pass.
    """
    if not outlet_ids:
        return {}
    unknown = [k for k in keys if k not in REGISTRY]
    if unknown:
        raise KeyError(f"Not declared settings: {', '.join(sorted(unknown))}")
    rows = (
        await db.execute(
            text(
                """
                select o as outlet_id,
                       k as key,
                       setting_value(k, o,
                                     coalesce(cast(:at as timestamptz), now())) as value
                  from unnest(cast(:ids as uuid[])) as o
                  cross join unnest(cast(:keys as text[])) as k
                """
            ),
            {"keys": keys, "ids": outlet_ids, "at": at},
        )
    ).mappings()
    resolved: dict[uuid.UUID, dict[str, Any]] = {o: {} for o in outlet_ids}
    for row in rows:
        key = row["key"]
        value = decode_stored(row["value"], REGISTRY[key])
        outlet = uuid.UUID(str(row["outlet_id"]))
        resolved[outlet][key] = REGISTRY[key].default if value is None else value
    for per_outlet in resolved.values():
        for key in keys:
            per_outlet.setdefault(key, REGISTRY[key].default)
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
