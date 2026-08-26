"""Outlet reads.

An early slice of P3a, included here so outlet scoping is testable end to end
as soon as auth works. Create, update and delete land with the rest of P3a.
"""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from app.core.deps import CurrentUserDep, DbDep
from app.core.errors import ForbiddenError, NotFoundError

router = APIRouter(prefix="/outlets", tags=["outlets"])


class Outlet(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    city: str | None
    timezone: str
    geo_lat: float | None
    geo_lng: float | None
    geofence_radius_m: int
    is_active: bool


_BASE = """
    select id, code, name, city, timezone, geo_lat, geo_lng,
           geofence_radius_m, is_active
      from outlets
     where deleted_at is null
"""


@router.get("", response_model=list[Outlet], summary="Outlets you can see")
async def list_outlets(db: DbDep, user: CurrentUserDep) -> list[Outlet]:
    """Owners and ops managers see every outlet. Everyone else sees only the
    outlets they are a member of."""
    if user.is_global:
        rows = (await db.execute(text(_BASE + " order by code"))).mappings().all()
    else:
        if not user.outlet_ids:
            return []
        rows = (
            (
                await db.execute(
                    text(_BASE + " and id = any(:ids) order by code"),
                    {"ids": list(user.outlet_ids)},
                )
            )
            .mappings()
            .all()
        )
    return [Outlet(**row) for row in rows]


@router.get("/{outlet_id}", response_model=Outlet, summary="One outlet")
async def get_outlet(outlet_id: uuid.UUID, db: DbDep, user: CurrentUserDep) -> Outlet:
    """Re-checks access against the id rather than trusting that possession of
    an id implies permission — the fetch-by-id IDOR."""
    if not user.can_access_outlet(outlet_id):
        raise ForbiddenError(
            "You do not have access to that outlet.",
            extra={"outlet_id": str(outlet_id)},
        )
    row = (await db.execute(text(_BASE + " and id = :id"), {"id": outlet_id})).mappings().first()
    if row is None:
        raise NotFoundError("That outlet does not exist.")
    return Outlet(**row)
