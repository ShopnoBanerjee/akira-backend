"""User administration: invite, re-role, assign outlets, set PINs, deactivate.

Every permission decision here routes through permissions.py, so the rules live
in one readable place rather than scattered across these methods.
"""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record
from app.core.config import get_settings
from app.core.deps import CurrentUser
from app.core.enums import AuditAction, UserRole
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.core.security import hash_pin
from app.domains.users import permissions, repository
from app.domains.users.schemas import (
    GrantableRolesResponse,
    InviteUserRequest,
    InviteUserResponse,
    OutletSummary,
    SetOutletsRequest,
    SetPinRequest,
    SetRoleRequest,
    UpdateUserRequest,
    UserListItem,
)
from app.integrations.supabase_auth import SupabaseAuthAdmin


def _actor(user: CurrentUser) -> permissions.Actor:
    return permissions.Actor(
        profile_id=user.profile_id,
        role=user.global_role,
        outlet_ids=frozenset(user.outlet_ids),
    )


def _to_item(row: dict[str, Any], memberships: list[dict[str, Any]]) -> UserListItem:
    return UserListItem(
        profile_id=row["profile_id"],
        full_name=row["full_name"],
        email=row.get("email"),
        phone=row["phone"],
        employee_code=row["employee_code"],
        global_role=UserRole(row["global_role"]),
        is_active=row["is_active"],
        has_pin=row["has_pin"],
        last_seen_at=row["last_seen_at"],
        outlets=[
            OutletSummary(
                outlet_id=m["outlet_id"],
                code=m["code"],
                name=m["name"],
                role_at_outlet=UserRole(m["role_at_outlet"]),
                is_primary=m["is_primary"],
            )
            for m in memberships
        ],
    )


async def list_users(
    db: AsyncSession,
    user: CurrentUser,
    *,
    outlet_id: uuid.UUID | None = None,
    role: UserRole | None = None,
    is_active: bool | None = None,
    search: str | None = None,
) -> list[UserListItem]:
    if outlet_id is not None:
        if not user.can_access_outlet(outlet_id):
            raise ForbiddenError(
                "You do not have access to that outlet.",
                extra={"outlet_id": str(outlet_id)},
            )
        scope: list[uuid.UUID] | None = [outlet_id]
    elif user.is_global:
        scope = None
    else:
        # Someone with no outlet sees nobody, rather than everybody.
        scope = sorted(user.outlet_ids) or [uuid.UUID(int=0)]

    rows = await repository.list_users(
        db,
        outlet_ids=scope,
        role=role.value if role else None,
        is_active=is_active,
        search=search,
    )
    memberships = await repository.memberships_for(db, [r["profile_id"] for r in rows])
    return [_to_item(r, memberships.get(r["profile_id"], [])) for r in rows]


def grantable_roles(user: CurrentUser) -> GrantableRolesResponse:
    actor = _actor(user)
    grantable = permissions.grantable_roles(actor)
    return GrantableRolesResponse(
        grantable=grantable,
        all_roles=list(UserRole),
        reasons={
            role.value: permissions.refusal_reason(actor, role)
            for role in UserRole
            if role not in grantable
        },
    )


async def invite(
    db: AsyncSession,
    user: CurrentUser,
    payload: InviteUserRequest,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> InviteUserResponse:
    actor = _actor(user)
    requested = set(payload.outlet_ids)

    if not permissions.can_grant_role(actor, payload.global_role):
        raise ForbiddenError(
            permissions.refusal_reason(actor, payload.global_role),
            extra={
                "your_role": user.global_role.value,
                "requested_role": payload.global_role.value,
            },
        )
    if not permissions.can_manage_outlets(actor, requested):
        raise ForbiddenError(
            "You can only invite people into your own outlets.",
            extra={"your_outlets": [str(o) for o in sorted(user.outlet_ids)]},
        )

    settings = get_settings()
    auth = SupabaseAuthAdmin(settings.SUPABASE_URL, settings.SUPABASE_SECRET_KEY)

    existing = await auth.find_by_email(payload.email)
    if existing:
        # Not an error: someone returning, or joining a second outlet, keeps the
        # login they already have.
        auth_user_id = uuid.UUID(existing)
        invite_sent = False
        detail = (
            f"{payload.email} already had an account. "
            "Their role and outlets have been updated; no new invitation was sent."
        )
    else:
        auth_user_id = uuid.UUID(await auth.invite(payload.email, payload.full_name))
        invite_sent = True
        detail = f"An invitation has been emailed to {payload.email}."

    await repository.upsert_profile(
        db,
        auth_user_id,
        full_name=payload.full_name,
        global_role=payload.global_role.value,
        employee_code=payload.employee_code,
        phone=payload.phone,
    )
    await repository.replace_memberships(
        db, auth_user_id, list(payload.outlet_ids), payload.global_role.value
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=payload.outlet_ids[0],
        entity_table="profiles",
        entity_id=auth_user_id,
        action=AuditAction.CREATE,
        after={
            "email": payload.email,
            "full_name": payload.full_name,
            "global_role": payload.global_role.value,
            "outlet_ids": [str(o) for o in payload.outlet_ids],
            "invite_sent": invite_sent,
        },
        ip=ip,
        user_agent=user_agent,
    )
    await db.commit()

    return InviteUserResponse(
        profile_id=auth_user_id,
        email=payload.email,
        invite_sent=invite_sent,
        detail=detail,
    )


async def _load_target(
    db: AsyncSession, user: CurrentUser, profile_id: uuid.UUID
) -> tuple[dict[str, Any], set[uuid.UUID]]:
    target = await repository.get_user(db, profile_id)
    if target is None:
        raise NotFoundError("That person does not exist.")
    outlets = await repository.outlet_ids_for(db, profile_id)
    if not permissions.can_administer(_actor(user), UserRole(target["global_role"]), outlets):
        raise ForbiddenError(
            "You cannot administer this person.",
            extra={
                "their_role": target["global_role"],
                "your_role": user.global_role.value,
            },
        )
    return target, outlets


async def update_user(
    db: AsyncSession,
    user: CurrentUser,
    profile_id: uuid.UUID,
    payload: UpdateUserRequest,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> UserListItem:
    target, _ = await _load_target(db, user, profile_id)
    changes = payload.model_dump(exclude_unset=True)

    if changes.get("is_active") is False:
        if profile_id == user.profile_id:
            raise ConflictError("You cannot deactivate your own account.")
        if UserRole(target["global_role"]) is UserRole.OWNER:
            remaining = await repository.count_other_owners(db, profile_id)
            if remaining == 0:
                raise ConflictError(
                    "This is the last active owner. Promote another owner first, "
                    "or nobody will be able to administer outlets and users."
                )

    await repository.patch_profile(db, profile_id, changes)
    await record(
        db,
        actor_profile_id=user.profile_id,
        entity_table="profiles",
        entity_id=profile_id,
        action=AuditAction.UPDATE,
        before={k: target.get(k) for k in changes},
        after=changes,
        ip=ip,
        user_agent=user_agent,
    )
    await db.commit()
    return await get_one(db, profile_id)


async def set_role(
    db: AsyncSession,
    user: CurrentUser,
    profile_id: uuid.UUID,
    payload: SetRoleRequest,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> UserListItem:
    target, outlets = await _load_target(db, user, profile_id)
    actor = _actor(user)

    if not permissions.can_grant_role(actor, payload.global_role):
        raise ForbiddenError(
            permissions.refusal_reason(actor, payload.global_role),
            extra={"requested_role": payload.global_role.value},
        )
    if (
        UserRole(target["global_role"]) is UserRole.OWNER
        and payload.global_role is not UserRole.OWNER
    ):
        remaining = await repository.count_other_owners(db, profile_id)
        if remaining == 0:
            raise ConflictError("This is the last active owner. Promote another owner first.")

    await repository.set_role(db, profile_id, payload.global_role.value)
    # Outlet roles follow the global role, so the two cannot silently disagree.
    if outlets:
        await repository.replace_memberships(
            db, profile_id, sorted(outlets), payload.global_role.value
        )
    await record(
        db,
        actor_profile_id=user.profile_id,
        entity_table="profiles",
        entity_id=profile_id,
        action=AuditAction.UPDATE,
        before={"global_role": target["global_role"]},
        after={"global_role": payload.global_role.value},
        ip=ip,
        user_agent=user_agent,
    )
    await db.commit()
    return await get_one(db, profile_id)


async def set_outlets(
    db: AsyncSession,
    user: CurrentUser,
    profile_id: uuid.UUID,
    payload: SetOutletsRequest,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> UserListItem:
    target, before = await _load_target(db, user, profile_id)
    requested = set(payload.outlet_ids)

    if not permissions.can_manage_outlets(_actor(user), requested):
        raise ForbiddenError("You can only assign people to your own outlets.")
    # Removing someone from an outlet you cannot see would be an invisible edit.
    if not permissions.can_manage_outlets(_actor(user), before):
        raise ForbiddenError("This person belongs to an outlet you do not administer.")

    await repository.replace_memberships(
        db, profile_id, list(payload.outlet_ids), target["global_role"]
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        entity_table="outlet_members",
        entity_id=profile_id,
        action=AuditAction.UPDATE,
        before={"outlet_ids": [str(o) for o in sorted(before)]},
        after={"outlet_ids": [str(o) for o in payload.outlet_ids]},
        ip=ip,
        user_agent=user_agent,
    )
    await db.commit()
    return await get_one(db, profile_id)


async def set_pin(
    db: AsyncSession,
    user: CurrentUser,
    profile_id: uuid.UUID,
    payload: SetPinRequest,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> UserListItem:
    target = await repository.get_user(db, profile_id)
    if target is None:
        raise NotFoundError("That person does not exist.")
    outlets = await repository.outlet_ids_for(db, profile_id)

    if not permissions.can_manage_pins(_actor(user), outlets):
        raise ForbiddenError("You cannot manage PINs for this person.")

    role = UserRole(target["global_role"])
    if role not in {UserRole.SHIFT_LEAD, UserRole.STAFF} and payload.pin is not None:
        # A PIN exists to attribute floor work on a shared tablet. Giving one to
        # a manager implies it could approve something, which it never can.
        raise ConflictError(
            "PINs are for shift leads and staff, who use the shared tablet. "
            "Managers sign in individually.",
            extra={"their_role": role.value},
        )

    await repository.set_pin_hash(db, profile_id, hash_pin(payload.pin) if payload.pin else None)
    await record(
        db,
        actor_profile_id=user.profile_id,
        entity_table="profiles",
        entity_id=profile_id,
        action=AuditAction.UPDATE,
        # Never record the PIN itself, only that it changed.
        before={"has_pin": target["has_pin"]},
        after={"has_pin": payload.pin is not None},
        ip=ip,
        user_agent=user_agent,
    )
    await db.commit()
    return await get_one(db, profile_id)


async def get_one(db: AsyncSession, profile_id: uuid.UUID) -> UserListItem:
    row = await repository.get_user(db, profile_id)
    if row is None:
        raise NotFoundError("That person does not exist.")
    memberships = await repository.memberships_for(db, [profile_id])
    return _to_item(row, memberships.get(profile_id, []))
