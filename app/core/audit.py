"""Audit trail.

Every mutating service method writes here. No exceptions for "small" edits: an
SOP template quietly edited to remove a step is exactly the event you will need
to reconstruct, and the edit that matters is never the one anybody expected to
need.

Writes join the caller's transaction on purpose. An audit row that survives a
rolled-back change would describe something that never happened.
"""

import json
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import AuditAction

_INSERT = text(
    """
    insert into audit_log
        (actor_profile_id, outlet_id, entity_table, entity_id, action,
         before, after, ip, user_agent)
    values
        (:actor_profile_id, :outlet_id, :entity_table, :entity_id, cast(:action as audit_action),
         cast(:before as jsonb), cast(:after as jsonb), cast(:ip as inet), :user_agent)
    """
)


def _json(value: dict[str, Any] | None) -> str | None:
    return None if value is None else json.dumps(value, default=str)


async def record(
    db: AsyncSession,
    *,
    actor_profile_id: uuid.UUID | None,
    entity_table: str,
    entity_id: uuid.UUID | None,
    action: AuditAction,
    outlet_id: uuid.UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    await db.execute(
        _INSERT,
        {
            "actor_profile_id": actor_profile_id,
            "outlet_id": outlet_id,
            "entity_table": entity_table,
            "entity_id": entity_id,
            "action": action.value,
            "before": _json(before),
            "after": _json(after),
            # An unparseable address must never fail the write it describes.
            "ip": ip or None,
            "user_agent": (user_agent or "")[:500] or None,
        },
    )
