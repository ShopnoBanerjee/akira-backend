"""Response models for the platform domain."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class OrganisationRow(BaseModel):
    organisation_id: uuid.UUID
    slug: str
    name: str
    is_active: bool
    onboarded_at: datetime | None
    max_outlets: int
    max_people: int
    outlets: int
    people: int
    owners: int
    created_at: datetime
