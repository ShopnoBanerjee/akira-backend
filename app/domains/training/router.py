"""HTTP surface for the training walkthrough.

`/training/me/*` is for the person being trained and works for both kinds of
caller: a manager's own login, and the PIN-identified person on a shared
tablet (device session + `X-Actor-Token`, exactly as the floor endpoints).
`/training/people` is the owner's view; `/reset` is the restart.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query, Request

from app.core.deps import CurrentUser, CurrentUserDep, DbDep, require_management
from app.domains.sop.runs_service import resolve_actor
from app.domains.training import service
from app.domains.training.schemas import (
    PersonTraining,
    RecordRequest,
    StartRequest,
    StepRequest,
    TrainingRecord,
    TrainingStatus,
)
from app.domains.training.service import Trainee

router = APIRouter(prefix="/training", tags=["training"])

ActorTokenHeader = Annotated[
    str | None,
    Header(
        alias="X-Actor-Token",
        description="On a shared-tablet session: the assertion from /floor/identify.",
    ),
]


def _ctx(request: Request) -> dict[str, Any]:
    return {
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }


async def trainee(user: CurrentUserDep, actor_token: ActorTokenHeader = None) -> Trainee:
    """Who is being trained. On a tablet that is the PIN-identified person,
    never the device; without a PIN there is nobody to train."""
    if user.device is not None:
        actor = resolve_actor(user, actor_token)
        return Trainee(
            profile_id=actor.profile_id,
            full_name=actor.full_name,
            role=actor.role,
            device_id=actor.device_id,
        )
    return Trainee(
        profile_id=user.profile_id,
        full_name=user.full_name,
        role=user.global_role,
        device_id=None,
    )


TraineeDep = Annotated[Trainee, Depends(trainee)]


@router.get(
    "/me",
    response_model=TrainingStatus,
    summary="Whether the walkthrough has to run for the person acting",
)
async def my_status(
    db: DbDep,
    who: TraineeDep,
    version: Annotated[
        str,
        Query(min_length=1, max_length=40, description="The content version the client carries"),
    ],
) -> TrainingStatus:
    return await service.status(db, who, version=version)


@router.post(
    "/me/start",
    response_model=TrainingRecord,
    summary="Begin the walkthrough, or resume the attempt already open",
)
async def start(payload: StartRequest, db: DbDep, who: TraineeDep) -> TrainingRecord:
    return await service.start(
        db,
        who,
        version=payload.version,
        total_steps=payload.total_steps,
        language=payload.language,
    )


@router.post(
    "/me/step",
    response_model=TrainingRecord,
    summary="Record that a step was reached",
)
async def step(payload: StepRequest, db: DbDep, who: TraineeDep) -> TrainingRecord:
    return await service.advance(db, who, record_id=payload.record_id, step=payload.step)


@router.post(
    "/me/complete",
    response_model=TrainingRecord,
    summary="Finish the walkthrough",
)
async def complete(
    payload: RecordRequest, request: Request, db: DbDep, who: TraineeDep
) -> TrainingRecord:
    return await service.complete(db, who, record_id=payload.record_id, **_ctx(request))


@router.post(
    "/me/skip",
    response_model=TrainingRecord,
    summary="Skip the walkthrough (owner only)",
)
async def skip(
    payload: RecordRequest, request: Request, db: DbDep, who: TraineeDep
) -> TrainingRecord:
    return await service.skip(db, who, record_id=payload.record_id, **_ctx(request))


@router.get(
    "/people",
    response_model=list[PersonTraining],
    summary="Who has been through the walkthrough, and who has not",
)
async def people_status(
    db: DbDep, user: Annotated[CurrentUser, Depends(require_management)]
) -> list[PersonTraining]:
    return await service.people(db, user)


@router.post(
    "/people/{profile_id}/reset",
    response_model=PersonTraining,
    summary="Restart somebody's training; their next visit runs the walkthrough again",
)
async def reset(
    profile_id: uuid.UUID,
    request: Request,
    db: DbDep,
    user: Annotated[CurrentUser, Depends(require_management)],
) -> PersonTraining:
    """The owner may restart anyone's. A manager may only when the owner has
    delegated it to them (People page), and only for people at their outlets;
    the service decides and refuses otherwise."""
    return await service.reset(db, user, profile_id, **_ctx(request))
