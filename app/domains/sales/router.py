"""HTTP surface for sales ingestion.

Upload, list, and the raw table view the spec's E9 asks for. Nothing here
computes anything: the sales dashboard is Stage 2, and the job of Stage 1 is to
get the rows in truthfully so that dashboard has something honest to read.
"""

import uuid
from datetime import date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from app.core.deps import CurrentUserDep, DbDep, require_management
from app.core.errors import ForbiddenError, NotFoundError
from app.domains.sales import service

router = APIRouter(prefix="/sales", tags=["sales"])


def _ctx(request: Request) -> dict[str, Any]:
    return {
        "ip": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
    }


class UploadRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    outlet_id: uuid.UUID
    outlet_code: str
    source: str
    original_filename: str
    file_sha256: str
    status: str
    row_count: int | None
    period_start: date | None
    period_end: date | None
    warnings: list[dict[str, Any]]
    error_detail: str | None
    adapter_version: str | None
    parsed_net_paise: int | None
    uploaded_by_name: str | None
    created_at: datetime
    parsed_at: datetime | None


class OrderRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    outlet_id: uuid.UUID
    outlet_code: str
    external_bill_no: str
    business_date: date
    ordered_at: datetime
    channel: str | None
    covers: int | None
    gross_paise: int
    discount_paise: int
    tax_paise: int
    net_paise: int
    payment_mode: str | None
    table_no: str | None
    #: Whether a phone was captured — never the hash itself, which is still
    #: personal data even hashed.
    has_phone: bool


class DailyTotal(BaseModel):
    business_date: date
    bills: int
    net_paise: int
    covers: int


@router.post(
    "/uploads",
    dependencies=[Depends(require_management)],
    summary="Upload a Petpooja export",
)
async def upload_export(
    request: Request,
    background: BackgroundTasks,
    db: DbDep,
    user: CurrentUserDep,
    outlet_id: Annotated[uuid.UUID, Form()],
    file: Annotated[UploadFile, File()],
) -> dict[str, Any]:
    """Accepts an Orders Master Report .xlsx and parses it in the background.

    The outlet is chosen here rather than read from the file: a Petpooja export
    names only the restaurant, so two outlets produce indistinguishable files.

    Re-sending a file already ingested returns the original upload untouched —
    idempotency is on the bytes, not the filename, which carries an export
    timestamp and changes every time.
    """
    data = await file.read()
    result = await service.create_upload(
        db,
        user,
        outlet_id=outlet_id,
        filename=file.filename or "upload.xlsx",
        content_type=file.content_type or "",
        data=data,
        **_ctx(request),
    )
    if not result["already_ingested"]:
        background.add_task(service.background_parse, uuid.UUID(result["id"]), outlet_id)
    return result


@router.get(
    "/uploads",
    response_model=list[UploadRow],
    dependencies=[Depends(require_management)],
    summary="Uploads and how they parsed",
)
async def list_uploads(
    db: DbDep,
    user: CurrentUserDep,
    outlet_id: uuid.UUID | None = Query(default=None),
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[UploadRow]:
    """Newest first, each carrying its warnings. A parser that dropped rows
    silently would be a parser nobody could trust, so what it skipped travels
    with the upload."""
    rows = await service.list_uploads(db, user, outlet_id=outlet_id, limit=limit)
    return [UploadRow(**r) for r in rows]


@router.post(
    "/uploads/{upload_id}/reparse",
    dependencies=[Depends(require_management)],
    summary="Parse a stored file again",
)
async def reparse(
    upload_id: uuid.UUID,
    background: BackgroundTasks,
    db: DbDep,
    user: CurrentUserDep,
) -> dict[str, Any]:
    """Re-read the stored original — after an adapter version bump, or after
    fixing whatever made it fail. The file is kept precisely so nobody has to
    go back to Petpooja and export it again."""
    row = (
        (
            await db.execute(
                text("select outlet_id, status from data_uploads where id = :id"),
                {"id": upload_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFoundError("That upload does not exist.")
    if not user.can_access_outlet(row["outlet_id"]):
        raise ForbiddenError("You do not have access to that outlet.")

    background.add_task(service.background_parse, upload_id, row["outlet_id"])
    return {"id": str(upload_id), "status": "parsing", "detail": "Re-parsing in the background."}


@router.get(
    "/orders",
    response_model=list[OrderRow],
    dependencies=[Depends(require_management)],
    summary="Ingested bills",
)
async def list_orders(
    db: DbDep,
    user: CurrentUserDep,
    outlet_id: uuid.UUID | None = Query(default=None),
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[OrderRow]:
    """Grouped and filtered by `business_date`, never by the calendar date —
    a bill at 00:45 belongs to the night before."""
    rows = await service.list_orders(
        db, user, outlet_id=outlet_id, date_from=date_from, date_to=date_to, limit=limit
    )
    return [OrderRow(**r) for r in rows]


@router.get(
    "/daily",
    response_model=list[DailyTotal],
    dependencies=[Depends(require_management)],
    summary="Net sales per trading day",
)
async def daily(
    db: DbDep,
    user: CurrentUserDep,
    outlet_id: Annotated[uuid.UUID, Query()],
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
) -> list[DailyTotal]:
    """What the Stage 2 sales pillar will read. Here now because it is the
    cheapest way to see whether an ingest landed on the days it should."""
    rows = await service.daily_totals(
        db, user, outlet_id=outlet_id, date_from=date_from, date_to=date_to
    )
    return [DailyTotal(**r) for r in rows]
