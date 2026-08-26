"""Request and response models for SOP template authoring."""

import uuid
from datetime import time

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.enums import DayPart, Frequency, UserRole, ValueType


class TemplateItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sort_order: int
    title: str
    title_bn: str | None
    instruction: str | None
    instruction_bn: str | None
    reference_photo_path: str | None
    requires_photo: bool
    requires_value: bool
    value_type: ValueType | None
    value_min: float | None
    value_max: float | None
    value_unit: str | None
    is_critical: bool
    allow_na: bool


class TemplateSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    name_bn: str | None
    description: str | None
    category_id: uuid.UUID
    category_key: str
    category_label: str
    frequency: Frequency
    day_part: DayPart
    version: int
    is_active: bool
    item_count: int
    critical_count: int
    assignment_count: int


class TemplateDetail(TemplateSummary):
    items: list[TemplateItem]
    #: Advisory only — the known failure modes of checklist programmes.
    #: Warn, never block: an informed exception beats a worked-around rule.
    warnings: list[str]


class CreateTemplateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=160)
    name_bn: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    category_id: uuid.UUID
    frequency: Frequency
    day_part: DayPart = DayPart.ANY


class UpdateTemplateRequest(BaseModel):
    """Template-level fields only. None of these is a material change, so none
    bumps the version — the items are the contract with history, not the
    label on the folder."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=160)
    name_bn: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=1000)
    category_id: uuid.UUID | None = None
    frequency: Frequency | None = None
    day_part: DayPart | None = None
    is_active: bool | None = None


class ItemFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=250)
    title_bn: str | None = Field(default=None, max_length=250)
    instruction: str | None = Field(default=None, max_length=1000)
    instruction_bn: str | None = Field(default=None, max_length=1000)
    requires_photo: bool = False
    requires_value: bool = False
    value_type: ValueType | None = None
    value_min: float | None = None
    value_max: float | None = None
    value_unit: str | None = Field(default=None, max_length=20)
    is_critical: bool = False
    allow_na: bool = False

    @model_validator(mode="after")
    def _value_shape(self) -> "ItemFields":
        if self.requires_value and self.value_type is None:
            raise ValueError("An item that records a value must say what kind.")
        if (
            self.value_min is not None
            and self.value_max is not None
            and self.value_min > self.value_max
        ):
            raise ValueError("The minimum cannot exceed the maximum.")
        return self


class UpdateTemplateItemRequest(BaseModel):
    """Partial edit. Every field here is material (D11): changing any of them
    bumps the template version so history keeps rendering what was true."""

    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=250)
    title_bn: str | None = Field(default=None, max_length=250)
    instruction: str | None = Field(default=None, max_length=1000)
    instruction_bn: str | None = Field(default=None, max_length=1000)
    requires_photo: bool | None = None
    requires_value: bool | None = None
    value_type: ValueType | None = None
    value_min: float | None = None
    value_max: float | None = None
    value_unit: str | None = Field(default=None, max_length=20)
    is_critical: bool | None = None
    allow_na: bool | None = None


class ReorderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Every current item id, in the new order. Partial lists are refused —
    #: a reorder that forgets an item would silently drop it from view.
    item_ids: list[uuid.UUID] = Field(min_length=1)


class Assignment(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    template_id: uuid.UUID
    template_name: str
    outlet_id: uuid.UUID
    outlet_code: str
    assigned_role: UserRole
    active_weekdays: list[int]
    interval_days: int | None
    due_time_local: time
    grace_minutes: int
    is_active: bool


class CreateAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: uuid.UUID
    outlet_id: uuid.UUID
    assigned_role: UserRole
    active_weekdays: list[int] = Field(default=[0, 1, 2, 3, 4, 5, 6])
    #: For cadences that do not fit a weekly cycle (alternate day = 2,
    #: fortnightly = 14). Anchored to the day the assignment is created.
    interval_days: int | None = Field(default=None, gt=0)
    due_time_local: time
    grace_minutes: int = Field(default=30, ge=0, le=480)

    @model_validator(mode="after")
    def _weekdays_valid(self) -> "CreateAssignmentRequest":
        if any(d < 0 or d > 6 for d in self.active_weekdays):
            raise ValueError("Weekdays are 0 (Sunday) through 6 (Saturday).")
        if not self.active_weekdays and self.interval_days is None:
            raise ValueError("Pick at least one weekday, or an interval.")
        return self


class UpdateAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assigned_role: UserRole | None = None
    active_weekdays: list[int] | None = None
    interval_days: int | None = Field(default=None, gt=0)
    due_time_local: time | None = None
    grace_minutes: int | None = Field(default=None, ge=0, le=480)
    is_active: bool | None = None


class CategoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    key: str
    label: str
    label_bn: str | None
    sort_order: int
    icon: str | None
