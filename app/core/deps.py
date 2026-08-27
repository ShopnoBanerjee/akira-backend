"""Request dependencies: who is calling, and what they may do.

This module is where authorisation actually lives. RLS is defence in depth; the
guards here are the control. Every protected route composes one of them.
"""

import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_db, get_session_factory
from app.core.enums import GLOBAL_ROLES, UserRole
from app.core.errors import AuthError, ForbiddenError, PendingActivationError
from app.core.security import TokenClaims, TokenVerifier

# auto_error=False so a missing header produces our problem+json rather than
# Starlette's bare {"detail": ...}.
bearer_scheme = HTTPBearer(auto_error=False)


@lru_cache
def get_verifier() -> TokenVerifier:
    return TokenVerifier(get_settings())


@dataclass(frozen=True)
class OutletMembership:
    outlet_id: uuid.UUID
    outlet_code: str
    outlet_name: str
    role_at_outlet: UserRole
    is_primary: bool


@dataclass(frozen=True)
class DeviceContext:
    """Set when the caller is a shared outlet tablet rather than a person."""

    device_id: uuid.UUID
    outlet_id: uuid.UUID
    label: str


@dataclass(frozen=True)
class CurrentUser:
    profile_id: uuid.UUID
    full_name: str
    email: str | None
    global_role: UserRole
    is_active: bool
    memberships: list[OutletMembership] = field(default_factory=list)
    #: Present when this request came from a shared tablet. The device
    #: authenticates the request; a PIN attributes the action to a person.
    device: DeviceContext | None = None

    @property
    def is_global(self) -> bool:
        """Owners and ops managers reach every outlet without a membership row."""
        return self.global_role in GLOBAL_ROLES

    @property
    def outlet_ids(self) -> set[uuid.UUID]:
        return {m.outlet_id for m in self.memberships}

    def can_access_outlet(self, outlet_id: uuid.UUID) -> bool:
        return self.is_global or outlet_id in self.outlet_ids

    def role_at(self, outlet_id: uuid.UUID) -> UserRole | None:
        if self.is_global:
            return self.global_role
        for m in self.memberships:
            if m.outlet_id == outlet_id:
                return m.role_at_outlet
        return None


async def get_claims(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> TokenClaims:
    if credentials is None or not credentials.credentials:
        raise AuthError("This endpoint needs a bearer token.")
    if credentials.scheme.lower() != "bearer":
        raise AuthError("Authorization must use the Bearer scheme.")
    return get_verifier().verify(credentials.credentials)


#: Device, profile and memberships in one statement.
#:
#: These were three sequential queries, and every authenticated request paid
#: all three — roughly 500ms of pure network before a handler had done any of
#: its own work. They are independent lookups keyed on the same subject, so
#: there was never a reason to ask three times.
#:
#: The memberships come back as a json array rather than as extra rows, so the
#: whole thing stays one row and needs no grouping on the client side.
_IDENTITY_SQL = text(
    """
    select
        d.id                as device_id,
        d.outlet_id         as device_outlet_id,
        d.label             as device_label,
        p.id                as profile_id,
        p.full_name,
        p.global_role,
        p.is_active,
        p.deleted_at,
        coalesce(m.memberships, '[]'::json) as memberships
    from (select cast(:subject as uuid) as subject) s
    left join outlet_devices d
           on d.auth_user_id = s.subject
          and d.is_active
          and d.deleted_at is null
          and exists (
              select 1 from outlets o
               where o.id = d.outlet_id and o.deleted_at is null
          )
    left join profiles p
           on p.id = s.subject
    left join lateral (
        select json_agg(
                   json_build_object(
                       'outlet_id', m.outlet_id,
                       'code', o.code,
                       'name', o.name,
                       'role_at_outlet', m.role_at_outlet,
                       'is_primary', m.is_primary
                   )
                   order by m.is_primary desc, o.code
               ) as memberships
          from outlet_members m
          join outlets o on o.id = m.outlet_id
         where m.profile_id = s.subject
           and m.deleted_at is null
           and o.deleted_at is null
           and o.is_active
    ) m on true
    """
)


async def current_user(
    request: Request,
    claims: Annotated[TokenClaims, Depends(get_claims)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CurrentUser:
    """Load the caller's identity, once per request, in one round trip."""
    cached = getattr(request.state, "current_user", None)
    if isinstance(cached, CurrentUser):
        return cached

    subject = claims.subject
    row = (await db.execute(_IDENTITY_SQL, {"subject": subject})).mappings().first()

    device = None
    if row is not None and row["device_id"] is not None:
        device = DeviceContext(
            device_id=row["device_id"],
            outlet_id=row["device_outlet_id"],
            label=row["device_label"],
        )

    if row is None or row["profile_id"] is None:
        if device is not None:
            # A tablet has no profile of its own. It is a valid caller — the
            # floor endpoints accept it and then demand a staff PIN before any
            # action is attributed to a person. Its pseudo-role is staff, so
            # every management guard rejects it.
            user = CurrentUser(
                profile_id=device.device_id,
                full_name=device.label,
                email=claims.email,
                global_role=UserRole.STAFF,
                is_active=True,
                memberships=[],
                device=device,
            )
            request.state.current_user = user
            return user
        # Authenticated with no profile: a self-signup. Give them a dormant
        # profile so an admin can find and activate them, but no access. This
        # writes, so it needs a session that can commit.
        async with get_session_factory()() as writer:
            await writer.execute(
                text(
                    """
                    insert into profiles (id, full_name, global_role, is_active)
                    values (:subject, :name, 'staff', false)
                    on conflict (id) do nothing
                    """
                ),
                {"subject": subject, "name": claims.email or "New user"},
            )
            await writer.commit()
        raise PendingActivationError(
            "Your account exists but has not been activated yet. "
            "An administrator needs to assign your role and outlet."
        )

    if row["deleted_at"] is not None:
        raise AuthError("This account has been removed.")
    if not row["is_active"]:
        raise PendingActivationError(
            "Your account is not active. An administrator needs to activate it."
        )

    raw = row["memberships"]
    entries = json.loads(raw) if isinstance(raw, str) else (raw or [])
    memberships = [
        OutletMembership(
            outlet_id=uuid.UUID(str(m["outlet_id"])),
            outlet_code=m["code"],
            outlet_name=m["name"],
            role_at_outlet=UserRole(m["role_at_outlet"]),
            is_primary=m["is_primary"],
        )
        for m in entries
    ]

    user = CurrentUser(
        profile_id=row["profile_id"],
        full_name=row["full_name"],
        email=claims.email,
        global_role=UserRole(row["global_role"]),
        is_active=row["is_active"],
        memberships=memberships,
        device=device,
    )
    request.state.current_user = user
    return user


CurrentUserDep = Annotated[CurrentUser, Depends(current_user)]
DbDep = Annotated[AsyncSession, Depends(get_db)]


def require_role(*roles: UserRole) -> Callable[[CurrentUser], Awaitable[CurrentUser]]:
    """Allow only these global roles.

    Owners are *not* implicitly allowed: pass UserRole.OWNER explicitly. Implicit
    superuser access is how an endpoint ends up reachable by someone nobody
    intended.
    """
    allowed = frozenset(roles)

    async def guard(user: CurrentUserDep) -> CurrentUser:
        if user.global_role not in allowed:
            raise ForbiddenError(
                "Your role does not allow this.",
                extra={
                    "required_roles": sorted(r.value for r in allowed),
                    "your_role": user.global_role.value,
                },
            )
        return user

    return guard


def require_outlet_access(
    outlet_id_param: str = "outlet_id",
) -> Callable[..., Awaitable[CurrentUser]]:
    """Require membership of the outlet named in the path or query.

    Raises 403, never 404. Disguising a denial as not-found makes a real bug
    indistinguishable from a permission problem.
    """

    async def guard(request: Request, user: CurrentUserDep) -> CurrentUser:
        raw = request.path_params.get(outlet_id_param) or request.query_params.get(outlet_id_param)
        if raw is None:
            raise ForbiddenError(f"This endpoint needs {outlet_id_param}.")
        try:
            outlet_id = uuid.UUID(str(raw))
        except ValueError as exc:
            raise ForbiddenError(f"{outlet_id_param} is not a valid id.") from exc

        if not user.can_access_outlet(outlet_id):
            raise ForbiddenError(
                "You do not have access to that outlet.",
                extra={"outlet_id": str(outlet_id)},
            )
        return user

    return guard


async def require_owner(user: CurrentUserDep) -> CurrentUser:
    if user.global_role is not UserRole.OWNER:
        raise ForbiddenError(
            "Only an owner can do this.",
            extra={"your_role": user.global_role.value},
        )
    return user


async def require_admin(user: CurrentUserDep) -> CurrentUser:
    """Owner or ops manager — the roles that administer the whole network."""
    if not user.is_global:
        raise ForbiddenError(
            "Only an owner or operations manager can do this.",
            extra={"your_role": user.global_role.value},
        )
    return user


async def require_management(user: CurrentUserDep) -> CurrentUser:
    """Anyone who belongs in the /app shell rather than /floor."""
    if user.global_role in {UserRole.SHIFT_LEAD, UserRole.STAFF}:
        raise ForbiddenError(
            "The management area is not available to your role.",
            extra={"your_role": user.global_role.value, "use_instead": "/floor"},
        )
    return user
