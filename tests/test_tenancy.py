"""Multi-tenancy (D33): an organisation is a fence nobody's role climbs.

Three layers, each tested where it lives:

- **The identity.** `CurrentUser` computes reach from the organisation, so an
  owner's "every outlet" stops at their tenant and a manager's memberships
  cannot point outside it. The loader refuses dead organisations, never
  self-creates a profile, and works out who owes a second factor.
- **The services.** Listing outlets, people and templates as the owner of one
  organisation never returns another's; creating past the cap is refused;
  settings resolve outlet > organisation > global.
- **The database.** RLS, exercised as an attacker with a leaked key: the other
  organisation's rows do not exist for you.
"""

import json
import uuid
from typing import Any

import asyncpg
import pytest
import pytest_asyncio
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core import deps
from app.core.deps import CurrentUser, OutletMembership
from app.core.enums import UserRole
from app.core.errors import (
    ForbiddenError,
    MfaRequiredError,
    NotFoundError,
    PendingActivationError,
)
from app.core.security import TokenClaims
from app.core.settings_value import resolve
from app.domains.outlets import service as outlets_service
from app.domains.outlets.schemas import CreateOutletRequest
from app.domains.sop import service as sop_service
from app.domains.users import admin_service
from app.domains.users.schemas import InviteUserRequest
from tests.conftest import DEV_ORG, dev_outlet_ids, dev_user
from tests.test_rls import act_as

pytestmark = pytest.mark.asyncio

ORG_B = uuid.UUID("b1000000-0000-4000-8000-00000000000b")


# --- The identity: pure ---------------------------------------------------------


def _member(outlet_id: uuid.UUID, role: UserRole = UserRole.OUTLET_MANAGER) -> OutletMembership:
    return OutletMembership(
        outlet_id=outlet_id,
        outlet_code="X",
        outlet_name="X",
        role_at_outlet=role,
        is_primary=True,
    )


def _user(
    role: UserRole,
    *,
    organisation_id: uuid.UUID | None = DEV_ORG,
    org_outlets: frozenset[uuid.UUID] = frozenset(),
    memberships: list[OutletMembership] | None = None,
    mfa_required: bool = False,
    aal: str = "aal1",
) -> CurrentUser:
    return CurrentUser(
        profile_id=uuid.uuid4(),
        full_name="T",
        email=None,
        global_role=role,
        is_active=True,
        memberships=memberships or [],
        organisation_id=organisation_id,
        organisation_outlet_ids=org_outlets,
        mfa_required=mfa_required,
        assurance_level=aal,
    )


class TestTheFence:
    def test_an_owner_reaches_every_outlet_of_the_organisation_and_no_other(self) -> None:
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        owner = _user(UserRole.OWNER, org_outlets=frozenset({mine}))
        assert owner.outlet_ids == {mine}
        assert owner.visible_outlet_ids == {mine}
        assert owner.can_access_outlet(mine)
        assert not owner.can_access_outlet(theirs)

    def test_a_membership_outside_the_organisation_does_not_count(self) -> None:
        """A stale or mistaken outlet_members row pointing at another tenant's
        outlet must not become access."""
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        manager = _user(
            UserRole.OUTLET_MANAGER,
            org_outlets=frozenset({mine}),
            memberships=[_member(mine), _member(theirs)],
        )
        assert manager.outlet_ids == {mine}
        assert not manager.can_access_outlet(theirs)

    def test_only_the_platform_admin_has_no_filter(self) -> None:
        platform = _user(UserRole.PLATFORM_ADMIN, organisation_id=None)
        assert platform.is_platform_admin
        assert not platform.is_global
        assert platform.visible_outlet_ids is None
        assert platform.can_access_outlet(uuid.uuid4())
        # And nobody else, however senior.
        assert _user(UserRole.OWNER).visible_outlet_ids == set()

    def test_mfa_satisfied(self) -> None:
        assert _user(UserRole.STAFF).mfa_satisfied
        assert not _user(UserRole.OWNER, mfa_required=True).mfa_satisfied
        assert _user(UserRole.OWNER, mfa_required=True, aal="aal2").mfa_satisfied


# --- The identity: loaded --------------------------------------------------------


class _Result:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def mappings(self) -> "_Result":
        return self

    def first(self) -> dict[str, Any] | None:
        return self._row


class FakeDb:
    """Answers the identity statement and records every statement it was asked
    to run, so a test can prove nothing was written."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.statements: list[str] = []

    async def execute(self, statement: Any, *_: Any, **__: Any) -> _Result:
        self.statements.append(str(statement))
        return _Result(self.row)


def _request(method: str = "GET", path: str = "/outlets") -> Request:
    return Request({"type": "http", "method": method, "path": path, "headers": []})


def _claims(subject: uuid.UUID, *, aal: str = "aal1") -> TokenClaims:
    return TokenClaims(
        subject=str(subject), email="x@akira.test", expires_at=2**31, raw={"aal": aal}
    )


def _row(
    pid: uuid.UUID,
    *,
    role: str = "owner",
    org: uuid.UUID | None = DEV_ORG,
    org_active: bool = True,
    onboarded: bool = False,
) -> dict[str, Any]:
    return {
        "device_id": None,
        "device_outlet_id": None,
        "device_label": None,
        "profile_id": pid,
        "full_name": "Loaded",
        "global_role": role,
        "is_active": True,
        "deleted_at": None,
        "memberships": json.dumps([]),
        "organisation_id": org,
        "organisation_slug": "akira-dev" if org else None,
        "organisation_name": "AKIRA (development)" if org else None,
        "organisation_active": org_active if org else None,
        "organisation_deleted_at": None,
        "organisation_onboarded_at": "2026-09-01T00:00:00+00:00" if onboarded else None,
        "organisation_outlet_ids": [],
    }


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    deps.forget_all_identities()


async def _load(row: dict[str, Any] | None, request: Request, claims: TokenClaims) -> CurrentUser:
    return await deps.current_user(request, claims, FakeDb(row))  # type: ignore[arg-type]


class TestTheLoader:
    async def test_an_unknown_subject_is_nobody_and_nothing_is_written(self) -> None:
        """Self-signup is off: a valid token for a login no administrator
        created gets a refusal and leaves no profile behind."""
        db = FakeDb(None)
        with pytest.raises(PendingActivationError, match="not set up"):
            await deps.current_user(_request(), _claims(uuid.uuid4()), db)  # type: ignore[arg-type]
        assert len(db.statements) == 1  # the one read
        assert "insert" not in db.statements[0].lower()

    async def test_a_login_with_no_organisation_is_refused(self) -> None:
        with pytest.raises(ForbiddenError, match="no organisation"):
            await _load(_row(uuid.uuid4(), org=None), _request(), _claims(uuid.uuid4()))

    async def test_a_dead_organisation_takes_its_logins_with_it(self) -> None:
        with pytest.raises(ForbiddenError, match="not active"):
            await _load(_row(uuid.uuid4(), org_active=False), _request(), _claims(uuid.uuid4()))

    async def test_the_platform_admin_belongs_to_no_organisation(self) -> None:
        pid = uuid.uuid4()
        user = await _load(
            _row(pid, role="platform_admin", org=None),
            _request("GET", "/users/me"),
            _claims(pid, aal="aal2"),
        )
        assert user.is_platform_admin and user.organisation_id is None
        assert user.mfa_required

    async def test_who_owes_a_second_factor(self) -> None:
        """Owners of an onboarded organisation; not owners still in
        development (the seeded test accounts keep working); never staff."""
        # Three different logins: the identity cache is keyed by subject.
        a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        dev_owner = await _load(_row(a), _request(), _claims(a))
        assert not dev_owner.mfa_required
        live_owner = await _load(_row(b, onboarded=True), _request("GET", "/users/me"), _claims(b))
        assert live_owner.mfa_required and not live_owner.mfa_satisfied
        staff = await _load(_row(c, role="staff", onboarded=True), _request(), _claims(c))
        assert not staff.mfa_required


class TestTheMfaGate:
    async def test_a_password_alone_is_refused_with_its_own_problem_type(self) -> None:
        pid = uuid.uuid4()
        with pytest.raises(MfaRequiredError) as caught:
            await _load(_row(pid, onboarded=True), _request("GET", "/outlets"), _claims(pid))
        assert caught.value.type_uri.endswith("/mfa-required")
        assert caught.value.extra["assurance_level"] == "aal1"

    async def test_users_me_is_reachable_so_the_client_can_learn_why(self) -> None:
        pid = uuid.uuid4()
        user = await _load(_row(pid, onboarded=True), _request("GET", "/users/me"), _claims(pid))
        assert user.mfa_required

    async def test_a_verified_second_factor_passes(self) -> None:
        pid = uuid.uuid4()
        user = await _load(
            _row(pid, onboarded=True), _request("GET", "/outlets"), _claims(pid, aal="aal2")
        )
        assert user.mfa_satisfied

    async def test_writing_to_users_me_still_needs_the_factor(self) -> None:
        pid = uuid.uuid4()
        with pytest.raises(MfaRequiredError):
            await _load(_row(pid, onboarded=True), _request("PATCH", "/users/me"), _claims(pid))


class TestThePlatformAdminGate:
    async def test_a_write_inside_an_organisation_is_refused(self) -> None:
        pid = uuid.uuid4()
        with pytest.raises(ForbiddenError, match=r"read .* but not change"):
            await _load(
                _row(pid, role="platform_admin", org=None),
                _request("POST", "/outlets"),
                _claims(pid, aal="aal2"),
            )

    async def test_platform_routes_accept_their_writes(self) -> None:
        pid = uuid.uuid4()
        user = await _load(
            _row(pid, role="platform_admin", org=None),
            _request("POST", "/platform/organisations"),
            _claims(pid, aal="aal2"),
        )
        assert user.is_platform_admin

    async def test_require_platform_admin(self) -> None:
        with pytest.raises(ForbiddenError):
            await deps.require_platform_admin(_user(UserRole.OWNER))
        platform = _user(UserRole.PLATFORM_ADMIN, organisation_id=None)
        assert await deps.require_platform_admin(platform) is platform


# --- Two organisations in one database ------------------------------------------


class SecondOrg:
    def __init__(self) -> None:
        self.owner = uuid.uuid4()
        self.manager = uuid.uuid4()
        self.dev_owner = uuid.uuid4()
        self.outlet: uuid.UUID = uuid.uuid4()
        self.template: uuid.UUID = uuid.uuid4()
        self.category: uuid.UUID = uuid.uuid4()


@pytest_asyncio.fixture(scope="module")
async def org_b(migrated_db: str):  # type: ignore[no-untyped-def]
    """A second tenant with one outlet, one owner, one manager, one template,
    plus an owner of the development organisation to look across from."""
    conn = await asyncpg.connect(migrated_db)
    b = SecondOrg()
    await conn.execute(
        "insert into organisations (id, slug, name, onboarded_at, max_outlets, max_people)"
        " values ($1, 'tenancy-b', 'Tenant B', now(), 1, 100)",
        ORG_B,
    )
    b.outlet = await conn.fetchval(
        "insert into outlets (organisation_id, code, name, city)"
        " values ($1, 'B-01', 'Tenant B One', 'Elsewhere') returning id",
        ORG_B,
    )
    for pid, name, role, org in [
        (b.owner, "Tenancy Owner B", "owner", ORG_B),
        (b.manager, "Tenancy Manager B", "outlet_manager", ORG_B),
        (b.dev_owner, "Tenancy Owner Dev", "owner", DEV_ORG),
    ]:
        await conn.execute(
            "insert into auth.users (id, email) values ($1, $2)", pid, f"{pid}@tenancy.test"
        )
        await conn.execute(
            "insert into profiles (id, full_name, global_role, is_active, organisation_id)"
            " values ($1, $2, $3::user_role, true, $4)",
            pid,
            name,
            role,
            org,
        )
    await conn.execute(
        "insert into outlet_members (outlet_id, profile_id, role_at_outlet)"
        " values ($1, $2, 'outlet_manager')",
        b.outlet,
        b.manager,
    )
    b.category = await conn.fetchval(
        "insert into sop_categories (organisation_id, key, label, sort_order)"
        " values ($1, 'tenancy', 'Tenancy', 99) returning id",
        ORG_B,
    )
    b.template = await conn.fetchval(
        "insert into checklist_templates"
        " (organisation_id, category_id, name, frequency, day_part, version)"
        " values ($1, $2, 'Tenant B opening', 'daily', 'opening', 1) returning id",
        ORG_B,
        b.category,
    )
    await conn.execute(
        "insert into app_settings (key, scope, organisation_id, value, note)"
        " values ('scoring.band.green', 'organisation', $1, '77'::jsonb, 'tenancy-probe')",
        ORG_B,
    )
    yield b
    await conn.execute("reset role")
    await conn.execute("delete from app_settings where note = 'tenancy-probe'")
    await conn.execute("delete from checklist_templates where organisation_id = $1", ORG_B)
    await conn.execute("delete from sop_categories where organisation_id = $1", ORG_B)
    await conn.execute("delete from outlet_members where profile_id = $1", b.manager)
    await conn.execute(
        "delete from audit_log where actor_profile_id = any($1)",
        [b.owner, b.manager, b.dev_owner],
    )
    await conn.execute("delete from profiles where organisation_id = $1", ORG_B)
    await conn.execute("delete from profiles where id = $1", b.dev_owner)
    await conn.execute("delete from auth.users where email like '%@tenancy.test'")
    await conn.execute("delete from outlets where organisation_id = $1", ORG_B)
    await conn.execute("delete from organisations where id = $1", ORG_B)
    await conn.close()


@pytest.fixture
async def session(migrated_db: str):  # type: ignore[no-untyped-def]
    engine = create_async_engine(migrated_db.replace("postgresql://", "postgresql+asyncpg://"))
    async with AsyncSession(engine, expire_on_commit=False) as db:
        try:
            yield db
        finally:
            await db.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
async def conn(migrated_db: str):  # type: ignore[no-untyped-def]
    c = await asyncpg.connect(migrated_db)
    try:
        yield c
    finally:
        await c.execute("reset role")
        await c.close()


def _owner_b(b: SecondOrg) -> CurrentUser:
    return CurrentUser(
        profile_id=b.owner,
        full_name="Tenancy Owner B",
        email=None,
        global_role=UserRole.OWNER,
        is_active=True,
        organisation_id=ORG_B,
        organisation_slug="tenancy-b",
        organisation_name="Tenant B",
        organisation_onboarded=True,
        organisation_outlet_ids=frozenset({b.outlet}),
        mfa_required=True,
        assurance_level="aal2",
    )


def _platform() -> CurrentUser:
    return CurrentUser(
        profile_id=uuid.uuid4(),
        full_name="Platform",
        email=None,
        global_role=UserRole.PLATFORM_ADMIN,
        is_active=True,
        mfa_required=True,
        assurance_level="aal2",
    )


class TestTheServicesStopAtTheOrganisation:
    async def test_outlets_are_listed_per_organisation(
        self, session: AsyncSession, org_b: SecondOrg
    ) -> None:
        theirs = {o.id for o in await outlets_service.list_for(session, _owner_b(org_b))}
        assert theirs == {org_b.outlet}
        ours = {o.id for o in await outlets_service.list_for(session, dev_user())}
        assert org_b.outlet not in ours and ours == dev_outlet_ids()
        everyone = {o.id for o in await outlets_service.list_for(session, _platform())}
        assert org_b.outlet in everyone and dev_outlet_ids() <= everyone

    async def test_the_outlet_cap_is_enforced(
        self, session: AsyncSession, org_b: SecondOrg
    ) -> None:
        payload = CreateOutletRequest(code="B-02", name="Tenant B Two", city="Elsewhere")
        with pytest.raises(ForbiddenError, match="limit of 1 outlets"):
            await outlets_service.create(session, _owner_b(org_b), payload)

    async def test_two_organisations_may_share_an_outlet_code(
        self, session: AsyncSession, org_b: SecondOrg
    ) -> None:
        """The dev organisation has AKR-NT01; Tenant B may have one too. Its
        cap is 1, so the refusal must be the cap, not the code."""
        payload = CreateOutletRequest(code="AKR-NT01", name="Their NT01", city="Elsewhere")
        with pytest.raises(ForbiddenError, match="limit"):
            await outlets_service.create(session, _owner_b(org_b), payload)

    async def test_people_are_listed_per_organisation(
        self, session: AsyncSession, org_b: SecondOrg
    ) -> None:
        theirs = {u.profile_id for u in await admin_service.list_users(session, _owner_b(org_b))}
        assert theirs == {org_b.owner, org_b.manager}
        ours = {u.profile_id for u in await admin_service.list_users(session, dev_user())}
        assert org_b.owner not in ours and org_b.manager not in ours

    async def test_another_organisations_person_does_not_exist_for_you(
        self, session: AsyncSession, org_b: SecondOrg
    ) -> None:
        with pytest.raises(NotFoundError):
            await admin_service._load_target(session, dev_user(), org_b.manager)

    async def test_an_invitation_into_another_organisations_outlet_is_refused(
        self, session: AsyncSession, org_b: SecondOrg
    ) -> None:
        dev_outlet = next(iter(dev_outlet_ids()))
        payload = InviteUserRequest(
            email="nobody@example.com",
            full_name="Nobody",
            global_role=UserRole.STAFF,
            outlet_ids=[dev_outlet],
        )
        with pytest.raises(ForbiddenError, match="own organisation's outlets"):
            await admin_service.invite(session, _owner_b(org_b), payload)

    async def test_templates_are_listed_per_organisation(
        self, session: AsyncSession, org_b: SecondOrg
    ) -> None:
        theirs = await sop_service.list_templates(
            session, category_id=None, include_inactive=True, organisation_id=ORG_B
        )
        assert {t.id for t in theirs} == {org_b.template}
        ours = await sop_service.list_templates(
            session, category_id=None, include_inactive=True, organisation_id=DEV_ORG
        )
        assert org_b.template not in {t.id for t in ours} and ours
        with pytest.raises(NotFoundError):
            await sop_service.get_template(session, org_b.template, organisation_id=DEV_ORG)

    async def test_settings_resolve_outlet_then_organisation_then_global(
        self, session: AsyncSession, org_b: SecondOrg
    ) -> None:
        assert await resolve(session, "scoring.band.green", outlet_id=org_b.outlet) == 77
        assert await resolve(session, "scoring.band.green", organisation_id=ORG_B) == 77
        dev_value = await resolve(
            session, "scoring.band.green", outlet_id=next(iter(dev_outlet_ids()))
        )
        assert dev_value != 77
        # An outlet override still beats the organisation's value.
        await session.execute(
            text(
                "insert into app_settings (key, scope, outlet_id, value, note)"
                " values ('scoring.band.green', 'outlet', :o, '66'::jsonb, 'tenancy-probe')"
            ),
            {"o": org_b.outlet},
        )
        assert await resolve(session, "scoring.band.green", outlet_id=org_b.outlet) == 66
        await session.rollback()


class TestRowLevelSecurityBetweenOrganisations:
    async def test_the_other_organisation_does_not_exist(
        self, conn: asyncpg.Connection, org_b: SecondOrg
    ) -> None:
        await act_as(conn, org_b.owner)
        assert {r["id"] for r in await conn.fetch("select id from outlets")} == {org_b.outlet}
        assert {r["id"] for r in await conn.fetch("select id from organisations")} == {ORG_B}
        people = {r["id"] for r in await conn.fetch("select id from profiles")}
        assert people == {org_b.owner, org_b.manager}
        templates = {r["id"] for r in await conn.fetch("select id from checklist_templates")}
        assert templates == {org_b.template}
        settings = await conn.fetch(
            "select organisation_id from app_settings where scope = 'organisation'"
        )
        assert {r["organisation_id"] for r in settings} <= {ORG_B}

    async def test_and_from_the_other_side_neither_do_we(
        self, conn: asyncpg.Connection, org_b: SecondOrg
    ) -> None:
        await act_as(conn, org_b.dev_owner)
        outlets = {r["id"] for r in await conn.fetch("select id from outlets")}
        assert org_b.outlet not in outlets and outlets == dev_outlet_ids()
        assert org_b.owner not in {r["id"] for r in await conn.fetch("select id from profiles")}
        assert org_b.template not in {
            r["id"] for r in await conn.fetch("select id from checklist_templates")
        }

    async def test_a_manager_of_b_sees_b_alone(
        self, conn: asyncpg.Connection, org_b: SecondOrg
    ) -> None:
        await act_as(conn, org_b.manager)
        assert {r["id"] for r in await conn.fetch("select id from outlets")} == {org_b.outlet}
        assert {r["id"] for r in await conn.fetch("select id from organisations")} == {ORG_B}
