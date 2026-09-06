"""Response models for the onboarding checklist."""

import uuid

from pydantic import BaseModel


class OnboardingStep(BaseModel):
    key: str
    title: str
    #: Why it matters, in the owner's terms rather than the schema's.
    why: str
    #: Where to get it and what to do with it.
    how: str
    #: Required steps gate `ready`; recommended ones only unlock more.
    required: bool
    done: bool
    #: What was actually found, so a tick has evidence behind it.
    count: int
    #: The screen that does this job.
    href: str


class OnboardingStatus(BaseModel):
    outlet_id: uuid.UUID
    steps: list[OnboardingStep]
    required_done: int
    required_total: int
    recommended_done: int
    recommended_total: int
    #: Nothing required outstanding. Informational: nothing is refused on it.
    ready: bool
