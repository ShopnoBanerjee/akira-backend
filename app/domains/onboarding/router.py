"""The onboarding checklist endpoint."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.deps import CurrentUserDep, DbDep, require_management
from app.domains.onboarding import service
from app.domains.onboarding.schemas import OnboardingStatus

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


@router.get(
    "",
    response_model=OnboardingStatus,
    dependencies=[Depends(require_management)],
    summary="What this outlet still needs before the app can do its job",
)
async def read_onboarding(
    db: DbDep,
    user: CurrentUserDep,
    outlet_id: Annotated[uuid.UUID | None, Query()] = None,
) -> OnboardingStatus:
    """A checklist computed from the data itself, so it can never drift out of
    step with reality. Omit `outlet_id` when the organisation has one outlet."""
    return OnboardingStatus.model_validate(await service.readiness(db, user, outlet_id=outlet_id))
