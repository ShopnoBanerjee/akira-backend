"""Business logic for the users domain. Owns transactions and audit writes."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record
from app.core.deps import CurrentUser
from app.core.enums import AuditAction, UserRole
from app.core.errors import NotFoundError
from app.domains.users import repository
from app.domains.users.schemas import (
    DeviceSummary,
    MeResponse,
    OrganisationSummary,
    OutletSummary,
    UpdateMeRequest,
)

MANAGEMENT_ROLES = {UserRole.OWNER, UserRole.OPS_MANAGER, UserRole.OUTLET_MANAGER}


def _organisation_of(user: CurrentUser) -> OrganisationSummary | None:
    if user.organisation_id is None or user.organisation_slug is None:
        return None
    return OrganisationSummary(
        organisation_id=user.organisation_id,
        slug=user.organisation_slug,
        name=user.organisation_name or user.organisation_slug,
        onboarded=user.organisation_onboarded,
    )


def _to_me(user: CurrentUser, profile: dict[str, Any]) -> MeResponse:
    return MeResponse(
        profile_id=profile["id"],
        full_name=profile["full_name"],
        email=user.email,
        phone=profile["phone"],
        employee_code=profile["employee_code"],
        global_role=UserRole(profile["global_role"]),
        is_active=profile["is_active"],
        is_management=UserRole(profile["global_role"]) in MANAGEMENT_ROLES,
        is_global=user.is_global,
        is_platform_admin=user.is_platform_admin,
        organisation=_organisation_of(user),
        mfa_required=user.mfa_required,
        mfa_verified=user.assurance_level == "aal2",
        has_pin=profile["has_pin"],
        can_restart_training=bool(profile.get("can_restart_training")),
        outlets=[
            OutletSummary(
                outlet_id=m.outlet_id,
                code=m.outlet_code,
                name=m.outlet_name,
                role_at_outlet=m.role_at_outlet,
                is_primary=m.is_primary,
            )
            for m in user.memberships
        ],
        device=(
            DeviceSummary(
                device_id=user.device.device_id,
                outlet_id=user.device.outlet_id,
                label=user.device.label,
            )
            if user.device
            else None
        ),
    )


async def get_me(db: AsyncSession, user: CurrentUser) -> MeResponse:
    if user.device is not None:
        # A shared tablet: no profile of its own. The client sees device mode
        # and shows the PIN screen; a person appears only after /floor/identify.
        return MeResponse(
            profile_id=user.profile_id,
            full_name=user.device.label,
            email=user.email,
            phone=None,
            employee_code=None,
            global_role=UserRole.STAFF,
            is_active=True,
            is_management=False,
            is_global=False,
            organisation=_organisation_of(user),
            has_pin=False,
            can_restart_training=False,
            outlets=[],
            device=DeviceSummary(
                device_id=user.device.device_id,
                outlet_id=user.device.outlet_id,
                label=user.device.label,
            ),
        )
    profile = await repository.get_profile_and_touch(db, user.profile_id)
    if profile is None:
        raise NotFoundError("Your profile could not be found.")
    await db.commit()
    return _to_me(user, profile)


async def update_me(
    db: AsyncSession,
    user: CurrentUser,
    payload: UpdateMeRequest,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> MeResponse:
    before = await repository.get_profile(db, user.profile_id)
    if before is None:
        raise NotFoundError("Your profile could not be found.")

    after = await repository.update_profile(
        db,
        user.profile_id,
        full_name=payload.full_name,
        phone=payload.phone,
    )
    if after is None:
        raise NotFoundError("Your profile could not be found.")

    await record(
        db,
        actor_profile_id=user.profile_id,
        entity_table="profiles",
        entity_id=user.profile_id,
        action=AuditAction.UPDATE,
        before={k: str(v) for k, v in before.items()},
        after={k: str(v) for k, v in after.items()},
        ip=ip,
        user_agent=user_agent,
    )
    await db.commit()
    return _to_me(user, after)


__all__ = ["get_me", "update_me", "uuid"]
