"""SQL for the users domain. No business rules live here."""

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_SELECT_PROFILE = text(
    """
    select id, full_name, phone, employee_code, global_role, is_active,
           pin_hash is not null as has_pin
      from profiles
     where id = :profile_id
       and deleted_at is null
    """
)

_UPDATE_PROFILE = text(
    """
    update profiles
       set full_name = coalesce(:full_name, full_name),
           phone     = coalesce(:phone, phone)
     where id = :profile_id
       and deleted_at is null
    returning id, full_name, phone, employee_code, global_role, is_active,
              pin_hash is not null as has_pin
    """
)

_TOUCH_LAST_SEEN = text("update profiles set last_seen_at = now() where id = :profile_id")


async def get_profile(db: AsyncSession, profile_id: uuid.UUID) -> dict[str, Any] | None:
    row = (await db.execute(_SELECT_PROFILE, {"profile_id": profile_id})).mappings().first()
    return dict(row) if row else None


async def update_profile(
    db: AsyncSession,
    profile_id: uuid.UUID,
    *,
    full_name: str | None,
    phone: str | None,
) -> dict[str, Any] | None:
    row = (
        (
            await db.execute(
                _UPDATE_PROFILE,
                {"profile_id": profile_id, "full_name": full_name, "phone": phone},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def touch_last_seen(db: AsyncSession, profile_id: uuid.UUID) -> None:
    await db.execute(_TOUCH_LAST_SEEN, {"profile_id": profile_id})
