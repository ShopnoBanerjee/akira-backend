"""Who may do what to whom.

Pure functions, no database, no request. Authorisation rules stated as code you
can read in one sitting and test exhaustively — the alternative is the same
rules scattered through service methods, where the gap nobody noticed lives.

The governing rule: **you can never create or grant a role at or above your
own.** Without it, an operations manager promotes themselves to owner in two
moves, and every other control becomes decorative.
"""

import uuid
from dataclasses import dataclass

from app.core.enums import UserRole

#: Roles an outlet manager may grant, and only inside their own outlet.
OUTLET_MANAGER_GRANTABLE: frozenset[UserRole] = frozenset({UserRole.SHIFT_LEAD, UserRole.STAFF})


@dataclass(frozen=True)
class Actor:
    """The person attempting the change."""

    profile_id: uuid.UUID
    role: UserRole
    outlet_ids: frozenset[uuid.UUID]

    @property
    def is_global(self) -> bool:
        return self.role in {UserRole.OWNER, UserRole.OPS_MANAGER}


def can_grant_role(actor: Actor, target_role: UserRole) -> bool:
    """May this actor grant this role to somebody?"""
    if target_role is UserRole.PLATFORM_ADMIN:
        # Not through the API at all: scripts/create_platform_admin.py (D33).
        return False
    if actor.role is UserRole.OWNER:
        # An owner may appoint another owner. That is a real decision, but it is
        # theirs to make; nobody else can.
        return True
    if actor.role is UserRole.OPS_MANAGER:
        return target_role.rank < UserRole.OPS_MANAGER.rank
    if actor.role is UserRole.OUTLET_MANAGER:
        return target_role in OUTLET_MANAGER_GRANTABLE
    return False


def grantable_roles(actor: Actor) -> list[UserRole]:
    """Everything this actor may grant. The UI renders the rest disabled with a
    reason rather than omitting them — a rule you cannot see is a rule you will
    argue with."""
    return [role for role in UserRole if can_grant_role(actor, role)]


def can_manage_outlets(actor: Actor, outlet_ids: set[uuid.UUID]) -> bool:
    """May this actor place somebody into these outlets?"""
    if actor.is_global:
        return True
    return outlet_ids.issubset(actor.outlet_ids)


def can_administer(actor: Actor, target_role: UserRole, target_outlets: set[uuid.UUID]) -> bool:
    """May this actor edit, deactivate or re-role this existing person?

    Editing somebody at or above your own rank is refused for the same reason
    granting that rank is: it would let a manager neutralise the person
    supervising them.
    """
    if actor.role is UserRole.OWNER:
        return True
    if target_role.rank >= actor.role.rank:
        return False
    if actor.is_global:
        return True
    # An outlet manager reaches only people wholly inside their own outlets.
    return bool(target_outlets) and target_outlets.issubset(actor.outlet_ids)


def can_manage_pins(actor: Actor, target_outlets: set[uuid.UUID]) -> bool:
    """PINs authorise floor actions on a shared tablet, so whoever runs the
    outlet may set them. They never grant management access, which is why an
    outlet manager setting one is not an escalation."""
    if actor.is_global:
        return True
    if actor.role is UserRole.OUTLET_MANAGER:
        return bool(target_outlets) and target_outlets.issubset(actor.outlet_ids)
    return False


def refusal_reason(actor: Actor, target_role: UserRole) -> str:
    """Plain language for why a grant was refused, shown in the UI."""
    if actor.role is UserRole.OUTLET_MANAGER:
        return (
            "An outlet manager can only invite shift leads and staff, "
            "and only into their own outlet."
        )
    if target_role.rank >= actor.role.rank:
        return (
            f"You cannot assign {target_role.value.replace('_', ' ')}, "
            "because it is at or above your own role."
        )
    return "Your role does not allow this."
