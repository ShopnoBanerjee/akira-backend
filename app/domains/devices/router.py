"""Shared outlet tablets.

Each device holds one Supabase session bound to a single outlet. Individual
staff then identify with a PIN, so a run is still attributed to a real person
and the separation-of-duties constraint keeps its meaning.

Registering a device is deliberately an owner action: a device account is a
standing credential sitting on a counter, and handing those out is exactly the
kind of thing that should need the top of the tree.
"""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record
from app.core.deps import CurrentUserDep, DbDep, require_management, require_owner
from app.core.enums import AuditAction
from app.core.errors import ConflictError, ForbiddenError, NotFoundError

router = APIRouter(prefix="/devices", tags=["devices"])


class Device(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    outlet_id: uuid.UUID
    outlet_code: str
    outlet_name: str
    label: str
    is_active: bool
    last_seen_at: datetime | None
    created_at: datetime


class RegisterDeviceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outlet_id: uuid.UUID
    label: str = Field(min_length=1, max_length=120)
    #: The Supabase auth user this tablet signs in as. Created out of band by an
    #: owner, so no credential is ever minted or transported by this API.
    auth_user_id: uuid.UUID


class UpdateDeviceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str | None = Field(default=None, min_length=1, max_length=120)
    is_active: bool | None = None


_COLUMNS = """
    d.id, d.outlet_id, o.code as outlet_code, o.name as outlet_name,
    d.label, d.is_active, d.last_seen_at, d.created_at
"""
_FROM = " from outlet_devices d join outlets o on o.id = d.outlet_id"


async def _get(db: AsyncSession, device_id: uuid.UUID) -> Device:
    row = (
        (
            await db.execute(
                text(f"select {_COLUMNS}{_FROM} where d.id = :id and d.deleted_at is null"),
                {"id": device_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFoundError("That tablet is not registered.")
    return Device(**row)


@router.get(
    "",
    response_model=list[Device],
    dependencies=[Depends(require_management)],
    summary="Registered tablets",
)
async def list_devices(
    db: DbDep,
    user: CurrentUserDep,
    outlet_id: uuid.UUID | None = Query(default=None),
) -> list[Device]:
    clauses = ["d.deleted_at is null", "o.deleted_at is null"]
    params: dict[str, Any] = {}

    if outlet_id is not None:
        if not user.can_access_outlet(outlet_id):
            raise ForbiddenError("You do not have access to that outlet.")
        clauses.append("d.outlet_id = :outlet_id")
        params["outlet_id"] = outlet_id
    elif not user.is_global:
        if not user.outlet_ids:
            return []
        clauses.append("d.outlet_id = any(:ids)")
        params["ids"] = sorted(user.outlet_ids)

    sql = f"select {_COLUMNS}{_FROM} where {' and '.join(clauses)} order by o.code, d.label"
    rows = (await db.execute(text(sql), params)).mappings()
    return [Device(**row) for row in rows]


@router.post(
    "",
    response_model=Device,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_owner)],
    summary="Register a tablet",
)
async def register_device(
    payload: RegisterDeviceRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> Device:
    """Owner only. Binds an existing Supabase auth account to one outlet.

    The account itself is created out of band, so this API never mints or
    transports a credential.
    """
    outlet = (
        await db.execute(
            text("select id from outlets where id = :id and deleted_at is null"),
            {"id": payload.outlet_id},
        )
    ).first()
    if outlet is None:
        raise NotFoundError("That outlet does not exist.")

    clash = (
        (
            await db.execute(
                text(
                    "select id, label from outlet_devices"
                    " where auth_user_id = :auth_id and deleted_at is null"
                ),
                {"auth_id": payload.auth_user_id},
            )
        )
        .mappings()
        .first()
    )
    if clash:
        label = clash["label"]
        raise ConflictError(
            f"That account is already registered as {label}. One account belongs to one tablet.",
            extra={"device_id": str(clash["id"])},
        )

    device_id = (
        await db.execute(
            text(
                """
                insert into outlet_devices (outlet_id, auth_user_id, label)
                values (:outlet_id, :auth_user_id, :label)
                returning id
                """
            ),
            payload.model_dump(),
        )
    ).scalar_one()

    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=payload.outlet_id,
        entity_table="outlet_devices",
        entity_id=device_id,
        action=AuditAction.CREATE,
        after={"label": payload.label, "outlet_id": str(payload.outlet_id)},
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
    return await _get(db, device_id)


@router.patch(
    "/{device_id}",
    response_model=Device,
    dependencies=[Depends(require_owner)],
    summary="Rename or suspend a tablet",
)
async def update_device(
    device_id: uuid.UUID,
    payload: UpdateDeviceRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> Device:
    before = await _get(db, device_id)
    changes = payload.model_dump(exclude_unset=True)
    if changes:
        assignments = ", ".join(f"{c} = :{c}" for c in changes)
        await db.execute(
            text(f"update outlet_devices set {assignments} where id = :id"),
            {**changes, "id": device_id},
        )
        await record(
            db,
            actor_profile_id=user.profile_id,
            outlet_id=before.outlet_id,
            entity_table="outlet_devices",
            entity_id=device_id,
            action=AuditAction.UPDATE,
            before={"label": before.label, "is_active": before.is_active},
            after=changes,
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        await db.commit()
    return await _get(db, device_id)


@router.delete(
    "/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_owner)],
    summary="Revoke a tablet",
)
async def revoke_device(
    device_id: uuid.UUID,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> None:
    """Owner only. Use this the moment a tablet goes missing: it stops that
    account reaching any outlet data, without touching the runs it already
    recorded."""
    before = await _get(db, device_id)
    await db.execute(
        text(
            "update outlet_devices set deleted_at = now(), is_active = false"
            " where id = :id and deleted_at is null"
        ),
        {"id": device_id},
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=before.outlet_id,
        entity_table="outlet_devices",
        entity_id=device_id,
        action=AuditAction.DELETE,
        before={"label": before.label, "outlet_id": str(before.outlet_id)},
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()
