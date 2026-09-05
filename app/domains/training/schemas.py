"""Request and response models for the training walkthrough."""

import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.enums import UserRole

Track = Literal["management", "floor"]
Language = Literal["en", "bn"]

#: Where an attempt stands. `reset` means training was restarted and the
#: person has not begun the new attempt yet.
Status = Literal["not_started", "in_progress", "completed", "skipped", "reset"]


class TrainingRecord(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    profile_id: uuid.UUID
    track: Track
    version: str
    language: Language | None
    total_steps: int
    last_step: int
    status: Status
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    skipped_at: datetime | None
    #: Who restarted training for this attempt; null on a first-time run.
    triggered_by: uuid.UUID | None
    triggered_by_name: str | None


class TrainingStatus(BaseModel):
    """What the client needs to decide whether to run the tour right now."""

    profile_id: uuid.UUID
    full_name: str
    role: UserRole
    track: Track
    #: The content version the client asked about.
    version: str
    #: True when this person has never finished (or been allowed to skip) the
    #: tour on their track since it was last restarted. The client blocks the
    #: shell. A completion on an older content version still counts (D31).
    required: bool
    #: Only the owner may skip. Everybody else finishes.
    can_skip: bool
    #: The open attempt at this version, if one has begun.
    record: TrainingRecord | None


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Annotated[str, Field(min_length=1, max_length=40)]
    total_steps: Annotated[int, Field(ge=1, le=200)]
    language: Language


class StepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: uuid.UUID
    #: 1-based; the step just finished.
    step: Annotated[int, Field(ge=1, le=200)]


class RecordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: uuid.UUID


class PersonTraining(BaseModel):
    """One row of the owner's 'who has been trained' view."""

    profile_id: uuid.UUID
    full_name: str
    global_role: UserRole
    is_active: bool
    track: Track
    status: Status
    version: str | None
    language: Language | None
    last_step: int
    total_steps: int | None
    started_at: datetime | None
    completed_at: datetime | None
    skipped_at: datetime | None
    #: Who asked for the current attempt, when it was a restart.
    triggered_by_name: str | None
    #: When training was last restarted for this person, and by whom.
    reset_at: datetime | None
    reset_by_name: str | None
    #: Whether the caller may restart this person's training.
    can_reset: bool
