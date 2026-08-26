"""HTTP surface for outlets."""

import uuid

from fastapi import APIRouter, Depends, Query, Request, status

from app.core.deps import CurrentUserDep, DbDep, require_owner
from app.domains.outlets import service
from app.domains.outlets.schemas import (
    CreateOutletRequest,
    OutletResponse,
    UpdateOutletRequest,
)

router = APIRouter(prefix="/outlets", tags=["outlets"])


@router.get("", response_model=list[OutletResponse], summary="Outlets you can see")
async def list_outlets(
    db: DbDep,
    user: CurrentUserDep,
    include_inactive: bool = Query(
        default=False, description="Include outlets that have been deactivated."
    ),
) -> list[OutletResponse]:
    """Owners and operations managers see every outlet. Everyone else sees only
    the outlets they belong to."""
    return await service.list_for(db, user, include_inactive=include_inactive)


@router.post(
    "",
    response_model=OutletResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_owner)],
    summary="Create an outlet",
)
async def create_outlet(
    payload: CreateOutletRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> OutletResponse:
    """Owner only. The code must be unique and is fixed once created."""
    return await service.create(
        db,
        user,
        payload,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.get("/{outlet_id}", response_model=OutletResponse, summary="One outlet")
async def get_outlet(outlet_id: uuid.UUID, db: DbDep, user: CurrentUserDep) -> OutletResponse:
    return await service.get_one(db, user, outlet_id)


@router.patch(
    "/{outlet_id}",
    response_model=OutletResponse,
    dependencies=[Depends(require_owner)],
    summary="Update an outlet",
)
async def update_outlet(
    outlet_id: uuid.UUID,
    payload: UpdateOutletRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> OutletResponse:
    """Owner only. Changes only the fields present in the body. The code cannot
    be changed — it appears in exports and printed sheets."""
    return await service.update(
        db,
        user,
        outlet_id,
        payload,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )


@router.delete(
    "/{outlet_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_owner)],
    summary="Close an outlet",
)
async def delete_outlet(
    outlet_id: uuid.UUID,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> None:
    """Owner only. A soft delete: history is kept, the outlet stops appearing.

    Refused with 409 while any checklist run is still pending, in progress or
    awaiting review — closing the outlet under them would strand work someone
    is in the middle of.
    """
    await service.soft_delete(
        db,
        user,
        outlet_id,
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
