"""User-administration permission rules.

The governing rule: you can never create or grant a role at or above your own.
Without it, an operations manager promotes themselves to owner in two moves and
every other control becomes decorative. These tests exist to make that rule
impossible to erode by accident.
"""

import uuid

import pytest

from app.core.enums import UserRole
from app.domains.users.permissions import (
    Actor,
    can_administer,
    can_grant_role,
    can_manage_outlets,
    can_manage_pins,
    grantable_roles,
)

NT = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")
DEV = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000002")

ALL_ROLES = list(UserRole)


def actor(role: UserRole, *outlets: uuid.UUID) -> Actor:
    return Actor(profile_id=uuid.uuid4(), role=role, outlet_ids=frozenset(outlets))


OWNER = actor(UserRole.OWNER)
OPS = actor(UserRole.OPS_MANAGER)
NT_MANAGER = actor(UserRole.OUTLET_MANAGER, NT)
DEV_MANAGER = actor(UserRole.OUTLET_MANAGER, DEV)
LEAD = actor(UserRole.SHIFT_LEAD, NT)
STAFF = actor(UserRole.STAFF, NT)


class TestNobodyEscalates:
    """The rule everything else rests on."""

    @pytest.mark.parametrize("role", ALL_ROLES)
    def test_no_one_below_owner_can_grant_their_own_role_or_higher(self, role: UserRole) -> None:
        if role is UserRole.OWNER:
            pytest.skip("an owner may appoint another owner; that is their call")
        a = actor(role, NT)
        for target in ALL_ROLES:
            if target.rank >= role.rank:
                assert not can_grant_role(a, target), (
                    f"{role.value} must not be able to grant {target.value}"
                )

    def test_ops_manager_cannot_make_an_owner(self) -> None:
        assert not can_grant_role(OPS, UserRole.OWNER)

    def test_ops_manager_cannot_clone_itself(self) -> None:
        assert not can_grant_role(OPS, UserRole.OPS_MANAGER)

    def test_outlet_manager_cannot_promote_to_outlet_manager(self) -> None:
        assert not can_grant_role(NT_MANAGER, UserRole.OUTLET_MANAGER)

    @pytest.mark.parametrize("role", [UserRole.SHIFT_LEAD, UserRole.STAFF])
    def test_floor_roles_grant_nothing_at_all(self, role: UserRole) -> None:
        a = actor(role, NT)
        assert grantable_roles(a) == []


class TestWhatEachRoleMayGrant:
    def test_owner_may_grant_anything_inside_the_organisation(self) -> None:
        """Everything but the platform's own role, which no API call grants (D33)."""
        assert set(grantable_roles(OWNER)) == set(ALL_ROLES) - {UserRole.PLATFORM_ADMIN}

    def test_nobody_grants_platform_admin(self) -> None:
        for actor in (OWNER, OPS):
            assert not can_grant_role(actor, UserRole.PLATFORM_ADMIN)

    def test_ops_manager_grants_strictly_below(self) -> None:
        assert set(grantable_roles(OPS)) == {
            UserRole.OUTLET_MANAGER,
            UserRole.SHIFT_LEAD,
            UserRole.STAFF,
        }

    def test_outlet_manager_grants_only_floor_roles(self) -> None:
        assert set(grantable_roles(NT_MANAGER)) == {
            UserRole.SHIFT_LEAD,
            UserRole.STAFF,
        }


class TestOutletScoping:
    def test_global_roles_reach_every_outlet(self) -> None:
        assert can_manage_outlets(OWNER, {NT, DEV})
        assert can_manage_outlets(OPS, {NT, DEV})

    def test_outlet_manager_is_confined_to_their_own(self) -> None:
        assert can_manage_outlets(NT_MANAGER, {NT})
        assert not can_manage_outlets(NT_MANAGER, {DEV})

    def test_a_partial_overlap_is_still_refused(self) -> None:
        """Asking for one allowed and one forbidden outlet must fail entirely,
        not silently succeed for the half that was permitted."""
        assert not can_manage_outlets(NT_MANAGER, {NT, DEV})


class TestAdministeringExistingPeople:
    def test_owner_can_administer_anyone(self) -> None:
        for role in ALL_ROLES:
            assert can_administer(OWNER, role, {NT})

    def test_nobody_can_administer_their_own_rank(self) -> None:
        assert not can_administer(OPS, UserRole.OPS_MANAGER, {NT})
        assert not can_administer(NT_MANAGER, UserRole.OUTLET_MANAGER, {NT})

    def test_a_manager_cannot_neutralise_the_person_above_them(self) -> None:
        assert not can_administer(NT_MANAGER, UserRole.OPS_MANAGER, {NT})
        assert not can_administer(OPS, UserRole.OWNER, {NT})

    def test_outlet_manager_reaches_only_people_inside_their_outlets(self) -> None:
        assert can_administer(NT_MANAGER, UserRole.STAFF, {NT})
        assert not can_administer(NT_MANAGER, UserRole.STAFF, {DEV})
        assert not can_administer(DEV_MANAGER, UserRole.STAFF, {NT})

    def test_someone_with_no_outlet_is_not_administrable_by_an_outlet_manager(
        self,
    ) -> None:
        """An empty set is not a subset match to reason from: with no outlet
        there is nothing tying that person to this manager."""
        assert not can_administer(NT_MANAGER, UserRole.STAFF, set())


class TestPins:
    """A PIN authorises floor actions only and can never approve a run, which
    is why an outlet manager setting one is not an escalation."""

    def test_global_roles_may_set_any_pin(self) -> None:
        assert can_manage_pins(OWNER, {DEV})
        assert can_manage_pins(OPS, {DEV})

    def test_outlet_manager_may_set_pins_in_their_own_outlet(self) -> None:
        assert can_manage_pins(NT_MANAGER, {NT})
        assert not can_manage_pins(NT_MANAGER, {DEV})

    @pytest.mark.parametrize("a", [LEAD, STAFF])
    def test_floor_roles_cannot_set_pins(self, a: Actor) -> None:
        """Otherwise a staff member sets a colleague's PIN and submits as them."""
        assert not can_manage_pins(a, {NT})
