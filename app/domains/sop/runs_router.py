"""HTTP surface for checklist runs, and the floor identify flow."""

import uuid
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.core import actor as actor_tokens
from app.core.audit import record
from app.core.business_date import OUTLET_TZ
from app.core.business_date import business_date as to_business_date
from app.core.deps import CurrentUserDep, DbDep, require_admin
from app.core.enums import AuditAction, ItemResult
from app.core.errors import AuthError, ForbiddenError, RateLimitError
from app.core.security import verify_pin
from app.domains.sop import integrity, runs_service
from app.domains.sop.runs_service import FloorActor, resolve_actor

runs_router = APIRouter(prefix="/sop/runs", tags=["sop-runs"])
floor_router = APIRouter(prefix="/floor", tags=["floor"])

ActorTokenHeader = Annotated[
    str | None,
    Header(
        alias="X-Actor-Token",
        description="Required on shared-tablet sessions: the assertion from /floor/identify.",
    ),
]


def _ctx(request: Request) -> dict[str, Any]:
    return {
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }


async def floor_actor(user: CurrentUserDep, actor_token: ActorTokenHeader = None) -> FloorActor:
    return resolve_actor(user, actor_token)


FloorActorDep = Annotated[FloorActor, Depends(floor_actor)]


# ---------------------------------------------------------------------------
# Floor identity — the PIN flow (D3)
# ---------------------------------------------------------------------------


class FloorStaff(BaseModel):
    profile_id: uuid.UUID
    full_name: str
    role: str
    has_pin: bool


class IdentifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: uuid.UUID
    pin: str = Field(min_length=4, max_length=8)


class IdentifyResponse(BaseModel):
    actor_token: str
    expires_at: int
    profile_id: uuid.UUID
    full_name: str
    role: str


@floor_router.get(
    "/staff",
    response_model=list[FloorStaff],
    summary="Who can identify on this tablet",
)
async def list_floor_staff(db: DbDep, user: CurrentUserDep) -> list[FloorStaff]:
    """The PIN-pad picker: floor staff of the tablet's outlet. Only meaningful
    on a device session; an individual login is already somebody."""
    if user.device is None:
        raise ForbiddenError("This endpoint is for shared tablets.")
    rows = (
        await db.execute(
            text(
                """
                select p.id as profile_id, p.full_name, p.global_role as role,
                       p.pin_hash is not null as has_pin
                  from profiles p
                  join outlet_members m on m.profile_id = p.id
                   and m.deleted_at is null
                 where m.outlet_id = :outlet_id
                   and p.is_active and p.deleted_at is null
                   and p.global_role in ('staff', 'shift_lead')
                 order by p.full_name
                """
            ),
            {"outlet_id": user.device.outlet_id},
        )
    ).mappings()
    return [FloorStaff(**r) for r in rows]


@floor_router.post(
    "/identify",
    response_model=IdentifyResponse,
    summary="Enter your PIN",
)
async def identify(
    payload: IdentifyRequest, request: Request, db: DbDep, user: CurrentUserDep
) -> IdentifyResponse:
    """Turns a device session plus a correct PIN into a short-lived actor
    assertion. Five wrong attempts lock the PIN for five minutes. Every
    failure is audited.

    The assertion authorises floor actions only. It can never approve a run
    and never reaches the management shell.
    """
    if user.device is None:
        raise ForbiddenError("PIN identification is only for shared tablets.")

    person = (
        (
            await db.execute(
                text(
                    """
                select p.id, p.full_name, p.global_role, p.pin_hash,
                       p.pin_failed_attempts, p.pin_locked_until
                  from profiles p
                  join outlet_members m on m.profile_id = p.id
                   and m.deleted_at is null and m.outlet_id = :outlet_id
                 where p.id = :profile_id
                   and p.is_active and p.deleted_at is null
                   and p.global_role in ('staff', 'shift_lead')
                """
                ),
                {"profile_id": payload.profile_id, "outlet_id": user.device.outlet_id},
            )
        )
        .mappings()
        .first()
    )

    async def audit_failure(reason: str) -> None:
        await record(
            db,
            actor_profile_id=None,
            outlet_id=user.device.outlet_id if user.device else None,
            entity_table="profiles",
            entity_id=payload.profile_id,
            action=AuditAction.LOGIN,
            after={"pin_identify": "failed", "reason": reason},
            **_ctx(request),
        )
        await db.commit()

    if person is None:
        await audit_failure("unknown_or_not_at_outlet")
        # One message for every failure mode: the tablet must not reveal who
        # exists, who has a PIN, or who is locked.
        raise AuthError("That PIN is not right.")

    if person["pin_locked_until"] is not None:
        locked_until = person["pin_locked_until"]
        if locked_until > datetime.now(tz=locked_until.tzinfo):
            await audit_failure("locked")
            raise RateLimitError("Too many wrong attempts. Wait a few minutes and try again.")

    if not verify_pin(person["pin_hash"], payload.pin):
        await db.execute(
            text(
                """
                update profiles
                   set pin_failed_attempts = pin_failed_attempts + 1,
                       pin_locked_until = case
                           when pin_failed_attempts + 1 >= 5
                           then now() + interval '5 minutes' else null end
                 where id = :id
                """
            ),
            {"id": person["id"]},
        )
        await audit_failure("wrong_pin")
        raise AuthError("That PIN is not right.")

    await db.execute(
        text(
            "update profiles set pin_failed_attempts = 0, pin_locked_until = null,"
            " last_seen_at = now() where id = :id"
        ),
        {"id": person["id"]},
    )
    await record(
        db,
        actor_profile_id=person["id"],
        outlet_id=user.device.outlet_id,
        entity_table="profiles",
        entity_id=person["id"],
        action=AuditAction.LOGIN,
        after={"pin_identify": "ok", "device": user.device.label},
        **_ctx(request),
    )
    await db.commit()

    token, expires_at = actor_tokens.mint(
        profile_id=person["id"],
        device_id=user.device.device_id,
        outlet_id=user.device.outlet_id,
        full_name=person["full_name"],
        role=person["global_role"],
    )
    return IdentifyResponse(
        actor_token=token,
        expires_at=expires_at,
        profile_id=person["id"],
        full_name=person["full_name"],
        role=person["global_role"],
    )


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


class AnswerItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result: ItemResult
    value_numeric: float | None = None
    value_text: str | None = Field(default=None, max_length=500)
    note: str | None = Field(default=None, max_length=1000)


class PhotoUrlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_type: str
    byte_size: int = Field(gt=0)


class PhotoConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=500)


class SubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    geo_lat: float | None = Field(default=None, ge=-90, le=90)
    geo_lng: float | None = Field(default=None, ge=-180, le=180)


class MaterialiseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Defaults to the current business date.
    business_date: date | None = None


@runs_router.get("/today", summary="Today's runs for an outlet")
async def today(
    db: DbDep,
    actor: FloorActorDep,
    outlet_id: uuid.UUID = Query(),
) -> list[dict[str, Any]]:
    """Scoped by role: staff see staff runs, shift leads see floor runs,
    managers see everything at the outlet. 'Today' is the business date —
    at 1am this still returns the evening's checklists."""
    return await runs_service.list_today(
        db, actor, outlet_id=outlet_id, now=datetime.now(tz=OUTLET_TZ)
    )


@runs_router.post(
    "/materialise", dependencies=[Depends(require_admin)], summary="Create today's runs now"
)
async def materialise(
    payload: MaterialiseRequest, db: DbDep, user: CurrentUserDep
) -> dict[str, int]:
    """Manual trigger for the 05:00 job (which arrives in the integrity epic).
    Idempotent — re-running creates nothing twice."""
    for_date = payload.business_date or to_business_date(datetime.now(tz=OUTLET_TZ))
    return await runs_service.materialise_runs(db, for_date=for_date, triggered_by=user.profile_id)


@runs_router.get("/{run_id}", summary="One run with its items")
async def get_run(run_id: uuid.UUID, db: DbDep, actor: FloorActorDep) -> dict[str, Any]:
    """Items carry the snapshot definitions they will be answered against —
    the version that was live when the run was created, not today's."""
    return await runs_service.get_run(db, actor, run_id)


@runs_router.post("/{run_id}/start", summary="Start a run")
async def start(
    run_id: uuid.UUID, request: Request, db: DbDep, actor: FloorActorDep
) -> dict[str, Any]:
    return await runs_service.start_run(db, actor, run_id, **_ctx(request))


@runs_router.patch("/{run_id}/items/{item_id}", summary="Answer an item")
async def answer(
    run_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: AnswerItemRequest,
    request: Request,
    db: DbDep,
    actor: FloorActorDep,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict[str, Any]:
    """Last write wins, and identical retries are naturally idempotent — the
    offline queue can safely replay this after a dropped response."""
    return await runs_service.answer_item(
        db,
        actor,
        run_id,
        item_id,
        result=payload.result,
        value_numeric=payload.value_numeric,
        value_text=payload.value_text,
        note=payload.note,
        **_ctx(request),
    )


@runs_router.post("/{run_id}/items/{item_id}/photo-url", summary="Get a photo upload URL")
async def photo_url(
    run_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: PhotoUrlRequest,
    db: DbDep,
    actor: FloorActorDep,
) -> dict[str, Any]:
    """A signed grant for one exact object path, minted here so the client
    never chooses where its bytes land."""
    return await runs_service.create_photo_upload(
        db,
        actor,
        run_id,
        item_id,
        content_type=payload.content_type,
        byte_size=payload.byte_size,
    )


@runs_router.post("/{run_id}/items/{item_id}/photo-confirm", summary="Confirm an uploaded photo")
async def photo_confirm(
    run_id: uuid.UUID,
    item_id: uuid.UUID,
    payload: PhotoConfirmRequest,
    background: BackgroundTasks,
    db: DbDep,
    actor: FloorActorDep,
) -> dict[str, Any]:
    """Verifies the object exists in storage, then writes the metadata. Never
    the other way round.

    Hashing the photo and running the duplicate lookback happen afterwards, in
    a background task recorded to job_runs. The response returns as soon as the
    metadata is written — the tablet is standing in a kitchen."""
    result = await runs_service.confirm_photo(db, actor, run_id, item_id, path=payload.path)
    background.add_task(
        integrity.background_photo_pass,
        item_id,
        outlet_id=result.pop("outlet_id"),
        business_date=result.pop("business_date"),
    )
    return result


@runs_router.post("/{run_id}/submit", summary="Submit a run")
async def submit(
    run_id: uuid.UUID,
    payload: SubmitRequest,
    request: Request,
    background: BackgroundTasks,
    db: DbDep,
    actor: FloorActorDep,
) -> dict[str, Any]:
    """Validates completeness (422 lists the offending items), computes the
    score, late-ness and geofence, raises an exception per critical fail, and
    locks the run into 'submitted'. Denied geolocation is not a flag — submit
    proceeds and geo_ok stays null."""
    run = await runs_service.submit_run(
        db,
        actor,
        run_id,
        geo_lat=payload.geo_lat,
        geo_lng=payload.geo_lng,
        **_ctx(request),
    )
    # Catch up any photo confirmed too close to submission to have been hashed
    # yet. A duplicate that only surfaces after approval surfaced too late.
    background.add_task(
        integrity.background_run_pass,
        run_id,
        outlet_id=run["outlet_id"],
        business_date=run["business_date"],
    )
    return run
