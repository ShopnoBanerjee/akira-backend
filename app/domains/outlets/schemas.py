"""Outlet request and response models."""

import uuid
from datetime import date

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OutletBase(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    address_line: str | None = Field(default=None, max_length=250)
    city: str | None = Field(default=None, max_length=80)
    geo_lat: float | None = Field(default=None, ge=-90, le=90)
    geo_lng: float | None = Field(default=None, ge=-180, le=180)
    geofence_radius_m: int = Field(default=150, gt=0, le=5000)
    timezone: str = Field(default="Asia/Kolkata", max_length=64)
    opened_on: date | None = None

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"{value!r} is not a known timezone.") from exc
        return value


class CreateOutletRequest(OutletBase):
    model_config = ConfigDict(extra="forbid")

    # Short, stable, human-quotable. Used in exports and conversation, so it is
    # deliberately not derived from the name.
    code: str = Field(min_length=3, max_length=20, pattern=r"^[A-Z0-9][A-Z0-9-]*$")


class UpdateOutletRequest(BaseModel):
    """Every field optional: a PATCH changes only what it names.

    `code` is absent on purpose. It appears in exports, conversation and
    printed sheets, so renaming it silently would break references nobody is
    tracking.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    address_line: str | None = Field(default=None, max_length=250)
    city: str | None = Field(default=None, max_length=80)
    geo_lat: float | None = Field(default=None, ge=-90, le=90)
    geo_lng: float | None = Field(default=None, ge=-180, le=180)
    geofence_radius_m: int | None = Field(default=None, gt=0, le=5000)
    timezone: str | None = Field(default=None, max_length=64)
    opened_on: date | None = None
    is_active: bool | None = None


class OutletResponse(OutletBase):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    is_active: bool
    member_count: int = 0
    device_count: int = 0
