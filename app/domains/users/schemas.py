"""Request and response models for the users domain."""

import uuid

from pydantic import BaseModel, ConfigDict, Field

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
    #: True for owner and ops_manager, who reach every outlet.
    is_global: bool
    has_pin: bool
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
