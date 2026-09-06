"""Outlet business logic. Owns transactions and audit writes."""

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record
from app.core.deps import CurrentUser, forget_all_identities
from app.core.enums import AuditAction
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.domains.outlets import repository
from app.domains.outlets.schemas import (
    CreateOutletRequest,
    OutletResponse,
    UpdateOutletRequest,
)


async def list_for(
    db: AsyncSession, user: CurrentUser, *, include_inactive: bool = False
) -> list[OutletResponse]:
    """Owners and ops managers see every outlet of their organisation; everyone
    else sees their own. A platform admin sees every organisation's."""
    if user.is_platform_admin:
        rows = await repository.list_outlets(db, outlet_ids=None, include_inactive=include_inactive)
    elif user.is_global:
        rows = await repository.list_outlets(
            db,
            outlet_ids=None,
            include_inactive=include_inactive,
            organisation_id=user.organisation_id,
        )
    elif not user.outlet_ids:
        rows = []
    else:
        rows = await repository.list_outlets(
            db, outlet_ids=sorted(user.outlet_ids), include_inactive=include_inactive
        )
    return [OutletResponse(**row) for row in rows]


async def get_one(db: AsyncSession, user: CurrentUser, outlet_id: uuid.UUID) -> OutletResponse:
    # Access is re-checked against the id rather than inferred from possession
    # of it. Trusting the id is the fetch-by-id IDOR.
    if not user.can_access_outlet(outlet_id):
        raise ForbiddenError(
            "You do not have access to that outlet.",
            extra={"outlet_id": str(outlet_id)},
        )
    row = await repository.get_outlet(db, outlet_id)
    if row is None:
        raise NotFoundError("That outlet does not exist.")
    return OutletResponse(**row)


async def create(
    db: AsyncSession,
    user: CurrentUser,
    payload: CreateOutletRequest,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> OutletResponse:
    code = payload.code.upper()
    if user.organisation_id is None:
        raise ForbiddenError("An outlet belongs to an organisation; this login has none.")
    if await repository.code_exists(db, code, user.organisation_id):
        raise ConflictError(
            f"An outlet with code {code} already exists.",
            extra={"field": "code"},
        )
    used, cap = await repository.outlet_headroom(db, user.organisation_id)
    if used >= cap:
        raise ForbiddenError(
            f"This organisation is at its limit of {cap} outlets.",
            extra={"max_outlets": cap, "outlets": used},
        )

    values: dict[str, Any] = payload.model_dump()
    values["code"] = code
    values["organisation_id"] = user.organisation_id
    outlet_id = await repository.insert_outlet(db, values)
    # Every owner's cached identity lists the organisation's outlets; there is
    # now one more.
    forget_all_identities()

    row = await repository.get_outlet(db, outlet_id)
    assert row is not None

    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=outlet_id,
        entity_table="outlets",
        entity_id=outlet_id,
        action=AuditAction.CREATE,
        after=values,
        ip=ip,
        user_agent=user_agent,
    )
    await db.commit()
    return OutletResponse(**row)


async def update(
    db: AsyncSession,
    user: CurrentUser,
    outlet_id: uuid.UUID,
    payload: UpdateOutletRequest,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> OutletResponse:
    before = await repository.get_outlet(db, outlet_id)
    if before is None:
        raise NotFoundError("That outlet does not exist.")

    # exclude_unset so a PATCH changes only the fields it actually named. Using
    # the whole model would blank every omitted column.
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        return OutletResponse(**before)

    await repository.update_outlet(db, outlet_id, changes)
    after = await repository.get_outlet(db, outlet_id)
    assert after is not None

    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=outlet_id,
        entity_table="outlets",
        entity_id=outlet_id,
        action=AuditAction.UPDATE,
        before={k: before.get(k) for k in changes},
        after=changes,
        ip=ip,
        user_agent=user_agent,
    )
    await db.commit()
    if "is_active" in changes:
        # An outlet going inactive drops out of every member's membership
        # list, which is part of who they are to the guards. That is many
        # people at once, so everyone is forgotten rather than guessed at.
        forget_all_identities()
    return OutletResponse(**after)


async def soft_delete(
    db: AsyncSession,
    user: CurrentUser,
    outlet_id: uuid.UUID,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    before = await repository.get_outlet(db, outlet_id)
    if before is None:
        raise NotFoundError("That outlet does not exist.")

    open_runs = await repository.count_open_runs(db, outlet_id)
    if open_runs:
        raise ConflictError(
            f"{before['name']} still has {open_runs} checklist "
            f"{'run' if open_runs == 1 else 'runs'} in progress. "
            "Finish or reject them before closing the outlet.",
            extra={"open_runs": open_runs},
        )

    await repository.soft_delete_outlet(db, outlet_id)
    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=outlet_id,
        entity_table="outlets",
        entity_id=outlet_id,
        action=AuditAction.DELETE,
        before=before,
        ip=ip,
        user_agent=user_agent,
    )
    await db.commit()
    forget_all_identities()  # see update(): memberships changed for everyone
