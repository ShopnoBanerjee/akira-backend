"""Stock counts and requisitions over HTTP.

Management only, whole flow: the count sheet is an office artifact, not a
floor one — the tablet photographs SOP evidence, a manager ingests stock
sheets. Review, confirm and requisition all follow the same rule.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.core.deps import CurrentUserDep, DbDep, require_management
from app.core.errors import NotFoundError
from app.domains.inventory import counts_service, requisitions_service

router = APIRouter(
    prefix="/inventory",
    tags=["stock-counts"],
    dependencies=[Depends(require_management)],
)


# ---------------------------------------------------------------------------
# Counts
# ---------------------------------------------------------------------------


@router.post("/counts", summary="Upload a photographed count sheet")
async def upload_sheet(
    background: BackgroundTasks,
    db: DbDep,
    user: CurrentUserDep,
    outlet_id: Annotated[uuid.UUID, Form()],
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    """Accepts the sheet, answers immediately; extraction runs behind a
    job_runs bracket and the count appears in `review` when it lands."""
    result = await counts_service.create_sheet_upload(
        db,
        user,
        outlet_id=outlet_id,
        filename=file.filename or "sheet",
        content_type=file.content_type or "application/octet-stream",
        data=await file.read(),
    )
    if not result["already_ingested"]:
        background.add_task(
            counts_service.background_extract,
            uuid.UUID(result["count_id"]),
            outlet_id=outlet_id,
        )
    return result


@router.get("/counts", summary="Counts for an outlet, newest first")
async def list_counts(
    db: DbDep,
    user: CurrentUserDep,
    outlet_id: Annotated[uuid.UUID, Query()],
    limit: Annotated[int, Query(ge=1, le=100)] = 30,
) -> list[dict[str, Any]]:
    await counts_service._require_outlet_access(user, outlet_id)
    rows = (
        (
            await db.execute(
                text(
                    """
                    select c.id, c.business_date, c.counted_at_label, c.status,
                           c.extractor, c.page_count, c.created_at,
                           c.confirmed_at, p.full_name as confirmed_by_name,
                           u.original_filename,
                           count(l.id) as line_count,
                           count(l.id) filter (where l.needs_review) as needs_review
                      from stock_counts c
                      join data_uploads u on u.id = c.upload_id
                      left join profiles p on p.id = c.confirmed_by
                      left join stock_count_lines l on l.count_id = c.id
                     where c.outlet_id = :o
                     group by c.id, u.original_filename, p.full_name
                     order by c.created_at desc
                     limit :n
                    """
                ),
                {"o": outlet_id, "n": limit},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@router.get("/counts/{count_id}", summary="One count, with every line")
async def get_count(count_id: uuid.UUID, db: DbDep, user: CurrentUserDep) -> dict[str, Any]:
    header = (
        (
            await db.execute(
                text(
                    """
                    select c.id, c.outlet_id, c.business_date, c.counted_at_label,
                           c.status, c.extractor, c.page_count, c.created_at,
                           c.confirmed_at, p.full_name as confirmed_by_name,
                           u.original_filename
                      from stock_counts c
                      join data_uploads u on u.id = c.upload_id
                      left join profiles p on p.id = c.confirmed_by
                     where c.id = :id
                    """
                ),
                {"id": count_id},
            )
        )
        .mappings()
        .first()
    )
    if header is None:
        raise NotFoundError("That count does not exist.")
    await counts_service._require_outlet_access(user, header["outlet_id"])

    lines = (
        (
            await db.execute(
                text(
                    """
                    select l.id, l.page, l.sl_no, l.raw_name, l.raw_closing,
                           l.raw_requisition,
                           cast(l.extract_confidence as float8) as extract_confidence,
                           l.item_id, i.name as item_name, i.unit as item_unit,
                           l.match_method,
                           cast(l.qty as float8) as qty,
                           cast(l.requested_qty as float8) as requested_qty,
                           l.parse_detail, l.needs_review,
                           p.full_name as reviewed_by_name, l.reviewed_at
                      from stock_count_lines l
                      left join inventory_items i on i.id = l.item_id
                      left join profiles p on p.id = l.reviewed_by
                     where l.count_id = :id
                     order by l.page, l.sl_no nulls last, l.created_at
                    """
                ),
                {"id": count_id},
            )
        )
        .mappings()
        .all()
    )
    return {**dict(header), "lines": [dict(line) for line in lines]}


class ReviewLineRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: uuid.UUID | None = None
    qty: float | None = Field(default=None, ge=0)
    requested_qty: float | None = Field(default=None, ge=0)
    #: Remember this sheet spelling for the chosen item, so next month's sheet
    #: maps without asking anyone.
    remember_alias: bool = False


@router.patch("/counts/{count_id}/lines/{line_id}", summary="Resolve a line")
async def review_line(
    count_id: uuid.UUID,
    line_id: uuid.UUID,
    payload: ReviewLineRequest,
    db: DbDep,
    user: CurrentUserDep,
) -> dict[str, Any]:
    return await counts_service.review_line(
        db,
        user,
        count_id=count_id,
        line_id=line_id,
        item_id=payload.item_id,
        qty=payload.qty,
        requested_qty=payload.requested_qty,
        remember_alias=payload.remember_alias,
    )


@router.post("/counts/{count_id}/re-extract", summary="Read the stored sheet again")
async def re_extract(
    count_id: uuid.UUID,
    background: BackgroundTasks,
    db: DbDep,
    user: CurrentUserDep,
) -> dict[str, Any]:
    """The stored file is re-read — after a failure, or under a newer
    extractor. Existing unconfirmed lines are replaced wholesale."""
    result = await counts_service.re_extract(db, user, count_id)
    outlet_id = (
        await db.execute(
            text("select outlet_id from stock_counts where id = :id"), {"id": count_id}
        )
    ).scalar_one()
    background.add_task(
        counts_service.background_extract, count_id, outlet_id=uuid.UUID(str(outlet_id))
    )
    return result


@router.post("/counts/{count_id}/confirm", summary="Sign the count off")
async def confirm_count(count_id: uuid.UUID, db: DbDep, user: CurrentUserDep) -> dict[str, Any]:
    return await counts_service.confirm_count(db, user, count_id)


# ---------------------------------------------------------------------------
# Requisitions
# ---------------------------------------------------------------------------


@router.post("/requisitions", summary="Compute a requisition from a confirmed count")
async def build_requisition(
    db: DbDep,
    user: CurrentUserDep,
    count_id: Annotated[uuid.UUID, Query()],
) -> dict[str, Any]:
    return await requisitions_service.build_from_count(db, user, count_id)


@router.get("/requisitions/{requisition_id}", summary="One requisition, lines and working")
async def get_requisition(
    requisition_id: uuid.UUID, db: DbDep, user: CurrentUserDep
) -> dict[str, Any]:
    header = await requisitions_service._load_header(db, requisition_id)
    await counts_service._require_outlet_access(user, header["outlet_id"])
    full = (
        (
            await db.execute(
                text(
                    """
                    select r.id, r.outlet_id, r.business_date, r.status,
                           r.created_at, r.finalised_at,
                           cp.full_name as created_by_name,
                           fp.full_name as finalised_by_name
                      from requisitions r
                      left join profiles cp on cp.id = r.created_by
                      left join profiles fp on fp.id = r.finalised_by
                     where r.id = :id
                    """
                ),
                {"id": requisition_id},
            )
        )
        .mappings()
        .one()
    )
    lines = (
        (
            await db.execute(
                text(
                    """
                    select l.item_id, i.name as item_name, i.unit as item_unit,
                           cast(l.on_hand as float8) as on_hand,
                           cast(l.par_level as float8) as par_level,
                           cast(l.order_unit as float8) as order_unit,
                           cast(l.suggested_qty as float8) as suggested_qty,
                           cast(l.requested_qty as float8) as requested_qty,
                           cast(l.final_qty as float8) as final_qty,
                           l.flags, l.detail
                      from requisition_lines l
                      join inventory_items i on i.id = l.item_id
                     where l.requisition_id = :id
                     order by i.name
                    """
                ),
                {"id": requisition_id},
            )
        )
        .mappings()
        .all()
    )
    return {**dict(full), "lines": [dict(line) for line in lines]}


class FinalQtyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: uuid.UUID
    final_qty: float | None = Field(default=None, ge=0)


@router.patch("/requisitions/{requisition_id}/lines", summary="Set a final quantity")
async def set_final_qty(
    requisition_id: uuid.UUID,
    payload: FinalQtyRequest,
    db: DbDep,
    user: CurrentUserDep,
) -> dict[str, Any]:
    return await requisitions_service.set_final_qty(
        db,
        user,
        requisition_id=requisition_id,
        item_id=payload.item_id,
        final_qty=payload.final_qty,
    )


@router.post("/requisitions/{requisition_id}/finalise", summary="Lock the requisition")
async def finalise_requisition(
    requisition_id: uuid.UUID, db: DbDep, user: CurrentUserDep
) -> dict[str, Any]:
    return await requisitions_service.finalise(db, user, requisition_id)
