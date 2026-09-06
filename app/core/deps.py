"""Request dependencies: who is calling, and what they may do.

This module is where authorisation actually lives. RLS is defence in depth; the
guards here are the control. Every protected route composes one of them.
"""

import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_db, get_session_factory
from app.core.enums import GLOBAL_ROLES, UserRole
from app.core.errors import (
    AuthError,
    ForbiddenError,
    MfaRequiredError,
    PendingActivationError,
)
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
    #: The tenant (D33). None only for a platform admin.
    organisation_id: uuid.UUID | None = None
    organisation_slug: str | None = None
    organisation_name: str | None = None
    #: Set once the platform has finished onboarding the organisation; the
    #: point from which its owners owe a second factor.
    organisation_onboarded: bool = False
    #: Policy: this login must present a second factor. Platform admins
    #: always; owners once their organisation is onboarded (D33).
    mfa_required: bool = False
    #: What the token proved: "aal1" password only, "aal2" plus a factor.
    assurance_level: str = "aal1"

    @property
    def mfa_satisfied(self) -> bool:
        return not self.mfa_required or self.assurance_level == "aal2"

    #: Every active outlet of the organisation. What an owner or ops manager
    #: may reach; the fence a manager's memberships sit inside.
    organisation_outlet_ids: frozenset[uuid.UUID] = frozenset()

    @property
    def is_platform_admin(self) -> bool:
        return self.global_role is UserRole.PLATFORM_ADMIN

    @property
    def is_global(self) -> bool:
        """Owners and ops managers reach every outlet OF THEIR ORGANISATION
        without a membership row. Not the platform: that is is_platform_admin."""
        return self.global_role in GLOBAL_ROLES

    @property
    def outlet_ids(self) -> set[uuid.UUID]:
        """The outlets this caller may see: the whole organisation for an owner
        or ops manager, the memberships for everyone else. Never another
        organisation's, whatever the role."""
        if self.is_global:
            return set(self.organisation_outlet_ids)
        return {
            m.outlet_id for m in self.memberships if m.outlet_id in self.organisation_outlet_ids
        }

    @property
    def visible_outlet_ids(self) -> set[uuid.UUID] | None:
        """For repository filters: None means "no filter" and is only ever the
        platform admin. Everyone else gets a concrete set, so a query can never
        widen past the organisation by accident."""
        return None if self.is_platform_admin else self.outlet_ids

    def can_access_outlet(self, outlet_id: uuid.UUID) -> bool:
        if self.is_platform_admin:
            return True
        return outlet_id in self.outlet_ids

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
        coalesce(p.organisation_id, dorg.organisation_id) as organisation_id,
        org.slug            as organisation_slug,
        org.name            as organisation_name,
        org.is_active       as organisation_active,
        org.deleted_at      as organisation_deleted_at,
        org.onboarded_at    as organisation_onboarded_at,
        coalesce(m.memberships, '[]'::json) as memberships,
        coalesce(oo.outlet_ids, '{}'::uuid[]) as organisation_outlet_ids
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
    left join outlets dorg
           on dorg.id = d.outlet_id
    left join organisations org
           on org.id = coalesce(p.organisation_id, dorg.organisation_id)
    left join lateral (
        select array_agg(o.id) as outlet_ids
          from outlets o
         where o.organisation_id = coalesce(p.organisation_id, dorg.organisation_id)
           and o.deleted_at is null and o.is_active
    ) oo on true
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


#: How long a loaded identity is trusted before Postgres is asked again.
#:
#: The identity statement was the one round trip EVERY authenticated request
#: paid before its handler did anything — with the database 300 ms away, half
#: the latency of a typical screen, and the whole of it for the many screens
#: that need one statement of their own. Who somebody is changes rarely, and
#: always through this API, so every write that can change it calls
#: `forget_identity` and the next request reloads. The TTL is the backstop for
#: a change that arrives some other way — SQL by hand, a second API process —
#: and bounds how long a revoked role or deactivated account can linger.
#:
#: Only identities that resolved to a usable caller are cached. A pending
#: activation, a deactivated or deleted account, and an unknown subject are
#: re-read every time, so activating someone takes effect on their next click
#: without anybody having to remember to clear anything.
IDENTITY_CACHE_TTL_SECONDS = 60.0
#: A ceiling, not a target: the whole staff of a restaurant group is a few
#: hundred subjects. If it is ever reached, something is minting tokens.
_IDENTITY_CACHE_MAX = 5000

_identity_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def forget_identity(*subjects: object) -> None:
    """Drop the cached identity for these auth subjects (profile ids, or a
    device's auth_user_id). Call it AFTER the commit that changed them: called
    before, a request racing the write could re-cache the old row."""
    for subject in subjects:
        if subject is not None:
            _identity_cache.pop(str(subject), None)


def forget_all_identities() -> None:
    """For a change whose reach is not one person — an outlet deactivated,
    which changes the membership list of everyone attached to it."""
    _identity_cache.clear()


def _cached_identity(subject: str) -> dict[str, Any] | None:
    hit = _identity_cache.get(subject)
    if hit is None:
        return None
    expires_at, row = hit
    if expires_at <= time.monotonic():
        _identity_cache.pop(subject, None)
        return None
    return row


def _remember_identity(subject: str, row: dict[str, Any]) -> None:
    if IDENTITY_CACHE_TTL_SECONDS <= 0:
        return
    if len(_identity_cache) >= _IDENTITY_CACHE_MAX:
        _identity_cache.clear()
    _identity_cache[subject] = (time.monotonic() + IDENTITY_CACHE_TTL_SECONDS, row)


async def current_user(
    request: Request,
    claims: Annotated[TokenClaims, Depends(get_claims)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CurrentUser:
    """Load the caller's identity: once per request, and once per minute per
    caller across requests. See IDENTITY_CACHE_TTL_SECONDS."""
    cached = getattr(request.state, "current_user", None)
    if isinstance(cached, CurrentUser):
        return cached

    subject = str(claims.subject)
    row = _cached_identity(subject)
    from_cache = row is not None
    if row is None:
        fetched = (await db.execute(_IDENTITY_SQL, {"subject": subject})).mappings().first()
        row = dict(fetched) if fetched is not None else None

    device = None
    if row is not None and row["device_id"] is not None:
        device = DeviceContext(
            device_id=row["device_id"],
            outlet_id=row["device_outlet_id"],
            label=row["device_label"],
        )

    if row is None or row["profile_id"] is None:
        if device is not None:
            assert row is not None  # a device only exists on a loaded row
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
                organisation_id=row["organisation_id"],
                organisation_slug=row.get("organisation_slug"),
                organisation_name=row.get("organisation_name"),
                organisation_outlet_ids=frozenset(row["organisation_outlet_ids"] or ()),
            )
            if not from_cache and row is not None:
                _remember_identity(subject, row)
            request.state.current_user = user
            return user
        # Authenticated with no profile. Self-signup is off (D33): every
        # login is created by an administrator, so an unknown subject is not
        # "pending", it is nobody. Nothing is written.
        raise PendingActivationError(
            "This login is not set up in AKIRA Ops. Ask your administrator."
        )

    if row["deleted_at"] is not None:
        raise AuthError("This account has been removed.")
    if not row["is_active"]:
        raise PendingActivationError(
            "Your account is not active. An administrator needs to activate it."
        )
    role = UserRole(row["global_role"])
    mfa_required = role is UserRole.PLATFORM_ADMIN or (
        role is UserRole.OWNER and row["organisation_onboarded_at"] is not None
    )
    if role is not UserRole.PLATFORM_ADMIN:
        # A tenant that is gone takes every one of its logins with it.
        if row["organisation_id"] is None:
            raise ForbiddenError("This account belongs to no organisation. Ask your administrator.")
        if row["organisation_deleted_at"] is not None or not row["organisation_active"]:
            raise ForbiddenError("This organisation is not active.")

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
        global_role=role,
        is_active=row["is_active"],
        memberships=memberships,
        device=device,
        organisation_id=row["organisation_id"],
        organisation_slug=row.get("organisation_slug"),
        organisation_name=row.get("organisation_name"),
        organisation_onboarded=row.get("organisation_onboarded_at") is not None,
        organisation_outlet_ids=frozenset(row["organisation_outlet_ids"] or ()),
        mfa_required=mfa_required,
        assurance_level=claims.assurance_level,
    )
    if not from_cache:
        _remember_identity(subject, row)
    _mfa_gate(request, user)
    if user.is_platform_admin:
        await _platform_admin_gate(request, user, db)
    request.state.current_user = user
    return user


#: What a login that still owes a second factor may reach: enough to learn
#: that it owes one. Enrolment and verification happen against Supabase Auth
#: directly, so no API route is involved in them.
_MFA_EXEMPT_PATHS = ("/users/me", "/healthz", "/readyz")


def _mfa_gate(request: Request, user: CurrentUser) -> None:
    """A password alone is not enough for the roles that can do the most
    damage (D33). The token says what was proved; the identity says what is
    owed. The refusal has its own problem type, so the client can route to
    the enrol screen rather than show a refusal."""
    if user.mfa_satisfied:
        return
    if request.url.path in _MFA_EXEMPT_PATHS and request.method in ("GET", "HEAD", "OPTIONS"):
        return
    raise MfaRequiredError(
        "This login must verify a second factor before it can continue.",
        extra={"assurance_level": user.assurance_level},
    )


#: Routes a platform admin may WRITE to. Everything else is read-only for
#: them (D33): the account that creates tenants must not be able to act
#: inside one.
_PLATFORM_WRITE_PREFIXES = ("/platform", "/users/me", "/training/me", "/auth/")


async def _platform_admin_gate(request: Request, user: CurrentUser, db: AsyncSession) -> None:
    """Read-only inside organisations, and every read is on the record.

    The write refusal is here, in one place, rather than a check in each
    router: a new route cannot forget it. The audit row is the organisation's
    evidence that the platform looked; it names the path, not the response.
    """
    path = request.url.path
    is_write = request.method not in ("GET", "HEAD", "OPTIONS")
    if is_write and not path.startswith(_PLATFORM_WRITE_PREFIXES):
        raise ForbiddenError(
            "A platform admin can read an organisation's data but not change it.",
            extra={"path": path},
        )
    if not is_write and not path.startswith(("/platform", "/users/me", "/healthz", "/readyz")):
        async with get_session_factory()() as writer:
            await writer.execute(
                text(
                    """
                    insert into audit_log (actor_profile_id, entity_table, action, after)
                    values (:actor, 'platform', 'read', cast(:after as jsonb))
                    """
                ),
                {"actor": user.profile_id, "after": json.dumps({"path": path})},
            )
            await writer.commit()


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


async def require_platform_admin(user: CurrentUserDep) -> CurrentUser:
    """The one role that creates organisations (D33)."""
    if not user.is_platform_admin:
        raise ForbiddenError(
            "Only a platform admin can do this.",
            extra={"your_role": user.global_role.value},
        )
    return user


async def require_management(user: CurrentUserDep) -> CurrentUser:
    """Anyone who belongs in the /app shell rather than /floor. A platform
    admin passes for reads (the gate above refuses their writes)."""
    if user.global_role in {UserRole.SHIFT_LEAD, UserRole.STAFF}:
        raise ForbiddenError(
            "The management area is not available to your role.",
            extra={"your_role": user.global_role.value, "use_instead": "/floor"},
        )
    return user
