"""Per-outlet reference photos: the standard a submitted photo is judged against.

This is the prerequisite that went unbuilt for three epics. `outlet_item_reference_photos`
has existed since migration 0004 and has been empty ever since, because the
upload pipeline it needed only arrived with the checklist runner in P5. It
reuses that pipeline exactly: the API mints a signed URL for one exact object
path, the browser PUTs the bytes, and the metadata row is written only after
the object is confirmed to exist.

**Why per outlet, and not one photo per template item.** The New Town clean
prep station is not another outlet's clean prep station — different room,
different equipment, different light. One network-wide photograph would produce
failures that are really just differences in the building, and a compliance
system whose failures are mostly noise stops being read.

**Who may set the standard.** The outlet's own manager and above. Defining what
"clean" looks like at New Town is a judgement about New Town, and the person
who has to answer for it should be the one who makes it. Superseded standards
are deactivated rather than deleted: a verdict from six weeks ago has to stay
readable against the photograph it was actually compared to.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.core.audit import record
from app.core.deps import CurrentUserDep, DbDep, require_management
from app.core.enums import AuditAction, UserRole
from app.core.errors import ForbiddenError, NotFoundError, ValidationError
from app.domains.sop.runs_service import ALLOWED_PHOTO_TYPES, MAX_PHOTO_BYTES
from app.integrations import storage

router = APIRouter(prefix="/sop/reference-photos", tags=["sop-reference-photos"])


def _ctx(request: Request) -> dict[str, Any]:
    return {
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }


def reference_path(outlet_id: uuid.UUID, template_item_id: uuid.UUID) -> str:
    """Fixed server-side, like every other object path in this system, so a
    client can never choose where its bytes land."""
    return f"reference/{outlet_id}/{template_item_id}.jpg"


def _require_can_set_standard(user: CurrentUserDep, outlet_id: uuid.UUID) -> None:
    if user.device is not None:
        raise ForbiddenError(
            "Reference standards are set from the management app, not the floor tablet."
        )
    if user.global_role in (UserRole.STAFF, UserRole.SHIFT_LEAD):
        raise ForbiddenError(
            "Only an outlet manager or above can set an outlet's reference standard.",
            extra={"your_role": user.global_role.value},
        )
    if not user.can_access_outlet(outlet_id):
        raise ForbiddenError("You do not have access to that outlet.")


class ReferencePhoto(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID | None
    template_item_id: uuid.UUID
    title: str
    title_bn: str | None
    instruction: str | None
    requires_photo: bool
    is_critical: bool
    template_id: uuid.UUID
    template_name: str
    #: Null when this item has no standard yet — which is the point of the
    #: screen, so it is a first-class row rather than an omission.
    photo_path: str | None
    photo_view_url: str | None
    caption: str | None
    caption_bn: str | None
    luminance_mean: float | None
    captured_by_name: str | None
    captured_at: datetime | None


@router.get(
    "",
    response_model=list[ReferencePhoto],
    dependencies=[Depends(require_management)],
    summary="Reference standards for an outlet, including the ones not captured yet",
)
async def list_reference_photos(
    db: DbDep,
    user: CurrentUserDep,
    outlet_id: Annotated[uuid.UUID, Query()],
    template_id: uuid.UUID | None = Query(default=None),
    missing_only: bool = Query(default=False),
) -> list[ReferencePhoto]:
    """Every photo-requiring item, with its standard attached if one exists.

    Listing the gaps is the whole job. A screen that showed only what has been
    captured would make an outlet with two standards look finished.
    """
    if not user.can_access_outlet(outlet_id):
        raise ForbiddenError("You do not have access to that outlet.")

    clauses = ["i.deleted_at is null", "i.requires_photo", "t.deleted_at is null", "t.is_active"]
    params: dict[str, Any] = {"outlet_id": outlet_id}
    if template_id is not None:
        clauses.append("t.id = :template_id")
        params["template_id"] = template_id
    if missing_only:
        clauses.append("r.id is null")

    rows = (
        (
            await db.execute(
                text(
                    f"""
                    select i.id as template_item_id, i.title, i.title_bn, i.instruction,
                           i.requires_photo, i.is_critical,
                           t.id as template_id, t.name as template_name,
                           r.id, r.photo_path, r.caption, r.caption_bn,
                           cast(r.luminance_mean as float8) as luminance_mean,
                           p.full_name as captured_by_name, r.captured_at
                      from checklist_template_items i
                      join checklist_templates t on t.id = i.template_id
                      left join outlet_item_reference_photos r
                        on r.template_item_id = i.id
                       and r.outlet_id = :outlet_id
                       and r.is_active and r.deleted_at is null
                      left join profiles p on p.id = r.captured_by
                     where {" and ".join(clauses)}
                     order by t.name, i.sort_order
                    """
                ),
                params,
            )
        )
        .mappings()
        .all()
    )

    out = []
    for row in rows:
        data = dict(row)
        data["photo_view_url"] = (
            await storage.create_signed_view_url(row["photo_path"], expires_in=300)
            if row["photo_path"]
            else None
        )
        out.append(ReferencePhoto(**data))
    return out


class ReferenceUploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outlet_id: uuid.UUID
    template_item_id: uuid.UUID
    content_type: str
    byte_size: int = Field(gt=0)


@router.post("/upload-url", summary="Get an upload URL for a reference standard")
async def reference_upload_url(
    payload: ReferenceUploadRequest, db: DbDep, user: CurrentUserDep
) -> dict[str, str]:
    _require_can_set_standard(user, payload.outlet_id)
    if payload.content_type not in ALLOWED_PHOTO_TYPES:
        raise ValidationError("Reference photos must be JPEG, PNG or WebP.")
    if payload.byte_size > MAX_PHOTO_BYTES:
        raise ValidationError("That photo is over 5MB.")

    exists = (
        await db.execute(
            text(
                "select 1 from checklist_template_items"
                " where id = :id and deleted_at is null and requires_photo"
            ),
            {"id": payload.template_item_id},
        )
    ).scalar()
    if not exists:
        raise NotFoundError("That checklist item does not exist, or does not take a photo.")

    signed = await storage.create_signed_upload(
        reference_path(payload.outlet_id, payload.template_item_id)
    )
    return {"upload_url": signed.url, "token": signed.token, "path": signed.path}


class ConfirmReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outlet_id: uuid.UUID
    template_item_id: uuid.UUID
    path: str = Field(min_length=1, max_length=500)
    caption: str | None = Field(default=None, max_length=300)
    caption_bn: str | None = Field(default=None, max_length=300)


@router.post("", summary="Set an outlet's reference standard for an item")
async def set_reference_photo(
    payload: ConfirmReferenceRequest,
    request: Request,
    background: BackgroundTasks,
    db: DbDep,
    user: CurrentUserDep,
) -> dict[str, Any]:
    """Confirms the object exists, retires the previous standard, and records
    the new one. The luminance measurement runs afterwards: a standard shot in
    a dark room would make every honest submission look bright by comparison,
    so the number is captured for the same reason submissions are measured."""
    _require_can_set_standard(user, payload.outlet_id)

    expected = reference_path(payload.outlet_id, payload.template_item_id)
    if payload.path != expected:
        raise ValidationError("That is not this item's reference photo path.")

    stat = await storage.stat_object(payload.path)
    if not stat.exists:
        raise ValidationError(
            "The photo has not arrived in storage yet. Upload it first, then confirm."
        )

    # Retired, not deleted. A verdict from six weeks ago must stay readable
    # against the photograph it was actually compared to.
    await db.execute(
        text(
            """
            update outlet_item_reference_photos
               set is_active = false
             where outlet_id = :outlet_id
               and template_item_id = :template_item_id
               and is_active and deleted_at is null
            """
        ),
        {"outlet_id": payload.outlet_id, "template_item_id": payload.template_item_id},
    )
    new_id = (
        await db.execute(
            text(
                """
                insert into outlet_item_reference_photos
                    (outlet_id, template_item_id, photo_path, caption, caption_bn,
                     captured_by, is_active)
                values (:outlet_id, :template_item_id, :photo_path, :caption,
                        :caption_bn, :captured_by, true)
                returning id
                """
            ),
            {
                "outlet_id": payload.outlet_id,
                "template_item_id": payload.template_item_id,
                "photo_path": payload.path,
                "caption": payload.caption,
                "caption_bn": payload.caption_bn,
                "captured_by": user.profile_id,
            },
        )
    ).scalar_one()
    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=payload.outlet_id,
        entity_table="outlet_item_reference_photos",
        entity_id=new_id,
        action=AuditAction.CREATE,
        after={"template_item_id": str(payload.template_item_id), "path": payload.path},
        **_ctx(request),
    )
    await db.commit()

    background.add_task(_measure_reference, new_id, payload.path)
    return {"id": str(new_id), "photo_path": payload.path}


async def _measure_reference(reference_id: uuid.UUID, path: str) -> None:
    """Record the standard's own mean luminance, in a job_runs-bracketed task."""
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.domains.sop import integrity
    from app.jobs.runner import run_job

    async def body(db: AsyncSession) -> dict[str, Any]:
        image_bytes = await storage.download_object(path)
        _, luminance = integrity.measure(image_bytes)
        await db.execute(
            text("update outlet_item_reference_photos set luminance_mean = :lum where id = :id"),
            {"id": reference_id, "lum": luminance},
        )
        await db.commit()
        return {"reference_photo_id": str(reference_id), "luminance": round(luminance, 1)}

    await run_job("reference_photo_measure", body)


@router.delete("/{reference_id}", summary="Retire a reference standard")
async def retire_reference_photo(
    reference_id: uuid.UUID, request: Request, db: DbDep, user: CurrentUserDep
) -> dict[str, str]:
    row = (
        (
            await db.execute(
                text(
                    "select id, outlet_id, template_item_id from"
                    " outlet_item_reference_photos where id = :id and deleted_at is null"
                ),
                {"id": reference_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFoundError("That reference photo does not exist.")
    _require_can_set_standard(user, row["outlet_id"])

    await db.execute(
        text(
            "update outlet_item_reference_photos"
            " set is_active = false, deleted_at = now() where id = :id"
        ),
        {"id": reference_id},
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=row["outlet_id"],
        entity_table="outlet_item_reference_photos",
        entity_id=reference_id,
        action=AuditAction.DELETE,
        before={"template_item_id": str(row["template_item_id"])},
        **_ctx(request),
    )
    await db.commit()
    return {"id": str(reference_id), "status": "retired"}
