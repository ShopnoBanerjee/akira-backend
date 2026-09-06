"""Request and response models for the users domain."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.core.enums import UserRole


class OutletSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    outlet_id: uuid.UUID
    code: str
    name: str
    role_at_outlet: UserRole
    is_primary: bool


class DeviceSummary(BaseModel):
    """Present only when the caller is a shared outlet tablet."""

    device_id: uuid.UUID
    outlet_id: uuid.UUID
    label: str


class OrganisationSummary(BaseModel):
    """The tenant the caller belongs to (D33)."""

    organisation_id: uuid.UUID
    slug: str
    name: str
    #: Onboarding complete: the organisation is live and its owners owe MFA.
    onboarded: bool


class MeResponse(BaseModel):
    profile_id: uuid.UUID
    full_name: str
    email: str | None
    phone: str | None
    employee_code: str | None
    global_role: UserRole
    is_active: bool
    #: True when the role belongs in /app rather than /floor.
    is_management: bool
    #: True for owner and ops_manager, who reach every outlet of their
    #: organisation.
    is_global: bool
    #: The one role above organisations (D33). Read-only inside them.
    is_platform_admin: bool = False
    #: None only for a platform admin.
    organisation: OrganisationSummary | None = None
    #: This login must present a second factor (D33)...
    mfa_required: bool = False
    #: ...and the current session has. When required and not verified, every
    #: other endpoint refuses with the `mfa-required` problem type; the client
    #: enrols or challenges through Supabase Auth and signs in again.
    mfa_verified: bool = False
    has_pin: bool
    #: Owner-granted: may restart training for people at their outlets (D31).
    can_restart_training: bool
    outlets: list[OutletSummary]
    device: DeviceSummary | None = None


class UpdateMeRequest(BaseModel):
    """Only what a person may change about themselves.

    Role, outlets and activation are deliberately absent: allowing them here
    would let anyone promote themselves through their own profile.
    """

    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=1, max_length=120)
    phone: str | None = Field(default=None, max_length=20)


class UserListItem(BaseModel):
    """A person as they appear in the admin table."""

    model_config = ConfigDict(from_attributes=True)

    profile_id: uuid.UUID
    full_name: str
    email: str | None
    phone: str | None
    employee_code: str | None
    global_role: UserRole
    is_active: bool
    has_pin: bool
    can_restart_training: bool
    last_seen_at: datetime | None
    outlets: list[OutletSummary]


class InviteUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: EmailStr
    full_name: str = Field(min_length=1, max_length=120)
    global_role: UserRole
    #: At least one outlet. A person with no outlet can sign in and see nothing,
    #: which reads as a broken app rather than a permissions decision.
    outlet_ids: list[uuid.UUID] = Field(min_length=1)
    employee_code: str | None = Field(default=None, max_length=30)
    phone: str | None = Field(default=None, max_length=20)


class InviteUserResponse(BaseModel):
    profile_id: uuid.UUID
    email: EmailStr
    #: False when the address already had an account, which is not an error:
    #: someone returning to a second outlet keeps their existing login.
    invite_sent: bool
    detail: str


class UpdateUserRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    full_name: str | None = Field(default=None, min_length=1, max_length=120)
    phone: str | None = Field(default=None, max_length=20)
    employee_code: str | None = Field(default=None, max_length=30)
    is_active: bool | None = None


class SetRoleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    global_role: UserRole


class SetOutletsRequest(BaseModel):
    """Replaces the whole membership set, rather than adding to it.

    A replace makes the resulting state obvious from the request. An add/remove
    pair leaves the caller guessing what the person ended up with.
    """

    model_config = ConfigDict(extra="forbid")

    outlet_ids: list[uuid.UUID]


class SetTrainingDelegateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: True lets this manager restart training for people at their outlets
    #: (an ops manager: anywhere). Owner-only to change; ignored on floor roles.
    enabled: bool


class SetPinRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Null clears the PIN, which is how you take a departing staff member off
    #: the shared tablet without deleting their history.
    pin: str | None = Field(default=None, min_length=4, max_length=8)

    @field_validator("pin")
    @classmethod
    def _digits_only(cls, value: str | None) -> str | None:
        if value is not None and not value.isdigit():
            raise ValueError("A PIN must be digits only.")
        return value


class GrantableRolesResponse(BaseModel):
    """What the signed-in user may assign, so the UI can disable the rest with
    an explanation instead of hiding it."""

    grantable: list[UserRole]
    all_roles: list[UserRole]
    reasons: dict[str, str]
