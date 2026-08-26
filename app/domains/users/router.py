"""HTTP surface for the users domain. Parse, delegate, serialise."""

from fastapi import APIRouter, Request

from app.core.deps import CurrentUserDep, DbDep
from app.domains.users import service
from app.domains.users.schemas import MeResponse, UpdateMeRequest

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
