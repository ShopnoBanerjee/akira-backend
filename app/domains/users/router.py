"""HTTP surface for the users domain. Parse, delegate, serialise."""

import uuid

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.deps import CurrentUserDep, DbDep, require_management
from app.core.enums import UserRole
from app.domains.users import admin_service, service
from app.domains.users.schemas import (
    GrantableRolesResponse,
    InviteUserRequest,
    InviteUserResponse,
    MeResponse,
    SetOutletsRequest,
    SetPinRequest,
    SetRoleRequest,
    UpdateMeRequest,
    UpdateUserRequest,
    UserListItem,
)

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=MeResponse, summary="The signed-in user")
async def read_me(db: DbDep, user: CurrentUserDep) -> MeResponse:
    """Profile, global role and outlet memberships for the current session.

    The client uses this to decide which shell to render, so it is the first
    call after sign-in.
    """
    return await service.get_me(db, user)


@router.patch("/me", response_model=MeResponse, summary="Update your own details")
async def update_me(
    payload: UpdateMeRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> MeResponse:
    """Change your own name or phone. Role, outlets and activation are not
    editable here — those are administrative actions."""
    return await service.update_me(
        db,
        user,
        payload,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.get(
    "/roles/grantable",
    response_model=GrantableRolesResponse,
    summary="Roles you may assign",
)
async def get_grantable_roles(user: CurrentUserDep) -> GrantableRolesResponse:
    """What the signed-in user may grant, plus a plain-language reason for each
    role they may not. The UI disables the rest with the reason attached rather
    than omitting them — a rule you cannot see is a rule you will argue with."""
    return admin_service.grantable_roles(user)


@router.get(
    "",
    response_model=list[UserListItem],
    dependencies=[Depends(require_management)],
    summary="People you can administer",
)
async def list_users(
    db: DbDep,
    user: CurrentUserDep,
    outlet_id: uuid.UUID | None = Query(default=None),
    role: UserRole | None = Query(default=None),
    is_active: bool | None = Query(default=None),
    search: str | None = Query(default=None, max_length=80),
) -> list[UserListItem]:
    """Owners and operations managers see everyone. An outlet manager sees only
    people in their own outlets."""
    return await admin_service.list_users(
        db, user, outlet_id=outlet_id, role=role, is_active=is_active, search=search
    )


@router.post(
    "/invite",
    response_model=InviteUserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_management)],
    summary="Invite someone",
)
async def invite_user(
    payload: InviteUserRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> InviteUserResponse:
    """Supabase sends the invitation and owns the token, so no password is ever
    handled here.

    You can never grant a role at or above your own. An existing address is not
    an error: that person keeps the login they already have.
    """
    return await admin_service.invite(
        db,
        user,
        payload,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.get(
    "/{profile_id}",
    response_model=UserListItem,
    dependencies=[Depends(require_management)],
    summary="One person",
)
async def get_user(profile_id: uuid.UUID, db: DbDep) -> UserListItem:
    return await admin_service.get_one(db, profile_id)


@router.patch(
    "/{profile_id}",
    response_model=UserListItem,
    dependencies=[Depends(require_management)],
    summary="Update someone's details",
)
async def update_user(
    profile_id: uuid.UUID,
    payload: UpdateUserRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> UserListItem:
    """Deactivating the last active owner is refused: it would lock everyone out
    of outlet and user administration permanently."""
    return await admin_service.update_user(
        db,
        user,
        profile_id,
        payload,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.put(
    "/{profile_id}/role",
    response_model=UserListItem,
    dependencies=[Depends(require_management)],
    summary="Change someone's role",
)
async def set_user_role(
    profile_id: uuid.UUID,
    payload: SetRoleRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> UserListItem:
    """Outlet roles follow the global role, so the two cannot silently disagree."""
    return await admin_service.set_role(
        db,
        user,
        profile_id,
        payload,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.put(
    "/{profile_id}/outlets",
    response_model=UserListItem,
    dependencies=[Depends(require_management)],
    summary="Replace someone's outlets",
)
async def set_user_outlets(
    profile_id: uuid.UUID,
    payload: SetOutletsRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> UserListItem:
    """Replaces the whole set. The first outlet becomes their primary."""
    return await admin_service.set_outlets(
        db,
        user,
        profile_id,
        payload,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.put(
    "/{profile_id}/pin",
    response_model=UserListItem,
    dependencies=[Depends(require_management)],
    summary="Set or clear a shared-tablet PIN",
)
async def set_user_pin(
    profile_id: uuid.UUID,
    payload: SetPinRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> UserListItem:
    """A PIN attributes floor work on a shared tablet to a real person. It
    authorises floor actions only, can never approve a run, and is only accepted
    on a device already bound to that person's outlet.

    Sending `null` clears it, which is how a departing staff member comes off
    the tablet without losing their history. Only shift leads and staff may
    have one.
    """
    return await admin_service.set_pin(
        db,
        user,
        profile_id,
        payload,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
