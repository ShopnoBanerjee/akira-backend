"""HTTP surface for sales ingestion.

Upload, list, and the raw table view the spec's E9 asks for. Nothing here
computes anything: the sales dashboard is Stage 2, and the job of Stage 1 is to
get the rows in truthfully so that dashboard has something honest to read.
"""

import uuid
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.core.audit import record
from app.core.business_date import business_date as to_business_date
from app.core.business_date import outlet_now
from app.core.deps import CurrentUserDep, DbDep, require_management
from app.core.enums import AuditAction
from app.core.errors import ForbiddenError, NotFoundError
from app.domains.sales import forecast_service, service

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
    #: The "Restaurant Name:" the file claimed. Null for uploads that predate
    #: the guard, or for a report with no such preamble line. This is the
    #: string to copy into the expected-restaurant setting.
    restaurant_name: str | None = None
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
    #: Item names from the Order Listing report, in bill order. Empty until a
    #: listing covering this bill has been uploaded. Names only — the export
    #: carries no quantities, and this API does not invent them.
    items: list[str]


class DailyTotal(BaseModel):
    business_date: date
    bills: int
    net_paise: int
    covers: int


class ItemSummaryRow(BaseModel):
    item_name: str
    #: Bills carrying this item at least once — NOT units sold. The Order
    #: Listing has no quantities, so appearances are the honest unit.
    bills: int
    first_date: date
    last_date: date


class ForecastDay(BaseModel):
    target_date: date
    #: Null when the model refused — `reason` says why.
    net_paise: int | None
    covers: int | None
    reason: str | None
    #: The working: median, sample dates, trend factor, event multiplier.
    components: dict[str, Any]


class ForecastEventRow(BaseModel):
    id: uuid.UUID
    #: Null means the event applies to every outlet.
    outlet_id: uuid.UUID | None
    event_date: date
    multiplier: float
    label: str
    created_by_name: str | None = None
    created_at: datetime


class ForecastEventCreate(BaseModel):
    #: Omit for a group-wide event (a public holiday is nobody's override).
    outlet_id: uuid.UUID | None = None
    event_date: date
    multiplier: Annotated[float, Field(gt=0, le=5)]
    label: Annotated[str, Field(min_length=1, max_length=120)]


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
    """Accepts a Petpooja .xlsx export and parses it in the background.

    Two reports are understood, told apart by their header row: the Orders
    Master Report (bills) and the Order Listing report (item names per bill).
    The uploader does not choose — the file says what it is, and anything
    else is refused here in the request.

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


@router.get(
    "/items",
    response_model=list[ItemSummaryRow],
    dependencies=[Depends(require_management)],
    summary="How often each item appears on a bill",
)
async def items(
    db: DbDep,
    user: CurrentUserDep,
    outlet_id: Annotated[uuid.UUID, Query()],
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
) -> list[ItemSummaryRow]:
    """Appearances, not units: the Order Listing carries names without
    quantities, so a bill with two Shoyu Ramen counts once. Empty until a
    listing has been uploaded for the outlet."""
    rows = await service.item_summary(
        db, user, outlet_id=outlet_id, date_from=date_from, date_to=date_to
    )
    return [ItemSummaryRow(**r) for r in rows]


class MenuMixCategory(BaseModel):
    category: str
    #: Bills that carried the category, per Petpooja's own count.
    orders: int
    #: orders ÷ bills sales_orders holds for the same period. The attach rate.
    share_of_bills: float | None
    items: int
    items_per_order: float | None
    net_sales_paise: int
    avg_spend_per_bill_paise: int | None
    #: Petpooja's own "Percentage (%)" — share of net sales, not of bills.
    share_of_net_pct: float | None
    #: Container Charge / Round Off / Waived Off: money, not menu.
    is_charge: bool


class MenuMixReported(BaseModel):
    period_start: date
    period_end: date
    bills_in_period: int
    categories: list[MenuMixCategory]


class MenuMixMeasuredCategory(BaseModel):
    category: str
    bills_with: int
    share_of_bills: float | None


class MenuMixMeasured(BaseModel):
    from_: date | None = Field(default=None, alias="from")
    to: date | None = None
    #: Bills whose item names have been uploaded (Order Listing, D21).
    bills_measured: int
    categories: list[MenuMixMeasuredCategory]
    #: Names on bills that menu_items does not know. Each one silently lowers
    #: every measured rate until an Item Wise export that names it is uploaded.
    unmapped_item_names: list[str]

    model_config = {"populate_by_name": True}


class MenuMixResponse(BaseModel):
    outlet_id: uuid.UUID
    menu_items_known: int
    reported: MenuMixReported | None
    measured: MenuMixMeasured


@router.get(
    "/menu-mix",
    response_model=MenuMixResponse,
    response_model_by_alias=True,
    dependencies=[Depends(require_management)],
    summary="Category attach: share of bills carrying each category",
)
async def menu_mix(
    db: DbDep,
    user: CurrentUserDep,
    outlet_id: Annotated[uuid.UUID, Query()],
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
) -> MenuMixResponse:
    """The beverage-per-ticket, dessert-per-ticket numbers (D29), two ways
    and labelled: Petpooja's own per-period count of bills carrying each
    category, and the same thing measured on the bills whose items were
    uploaded. Empty until a Category Wise report has been uploaded; the
    measured half stays empty until an Item Wise report has taught the menu
    map and an Order Listing has supplied names per bill."""
    payload = await service.menu_mix(
        db, user, outlet_id=outlet_id, date_from=date_from, date_to=date_to
    )
    return MenuMixResponse.model_validate(payload)


@router.get(
    "/forecast",
    response_model=list[ForecastDay],
    dependencies=[Depends(require_management)],
    summary="The baseline forecast for the coming days",
)
async def forecast(
    db: DbDep,
    user: CurrentUserDep,
    outlet_id: Annotated[uuid.UUID, Query()],
    days: Annotated[int, Query(ge=1, le=14)] = 7,
) -> list[ForecastDay]:
    """Spec 5.1's baseline: median of the last four same weekdays, times a
    clamped 14-day trend, times any manual event flag. Every row carries its
    working — the sample dates, the factor, the multiplier — because a
    forecast a manager cannot check is one they learn to ignore.

    This is the LIVE view; the nightly job stores the same numbers so
    accuracy is scored against what was predicted in advance.
    """
    if not user.can_access_outlet(outlet_id):
        raise ForbiddenError("You do not have access to that outlet.")
    as_of = to_business_date(outlet_now())
    forecasts = await forecast_service.compute(db, outlet_id, as_of=as_of, horizon=days)
    return [
        ForecastDay(
            target_date=f.target_date,
            net_paise=f.net_paise,
            covers=f.covers,
            reason=f.reason,
            components=f.components,
        )
        for f in forecasts
    ]


@router.get(
    "/forecast/accuracy",
    dependencies=[Depends(require_management)],
    summary="How the stored forecasts scored against reality",
)
async def forecast_accuracy(
    db: DbDep,
    user: CurrentUserDep,
    outlet_id: Annotated[uuid.UUID, Query()],
    weeks: Annotated[int, Query(ge=1, le=26)] = 8,
) -> dict[str, Any]:
    """MAPE against the rows the nightly job stored BEFORE those days
    traded. The spec's graduation rule reads from here: a learned model may
    replace the baseline only when this history says it genuinely loses."""
    if not user.can_access_outlet(outlet_id):
        raise ForbiddenError("You do not have access to that outlet.")
    return await forecast_service.accuracy(db, outlet_id, weeks=weeks)


@router.get(
    "/forecast/events",
    response_model=list[ForecastEventRow],
    dependencies=[Depends(require_management)],
    summary="Event flags in the forecast window",
)
async def forecast_events(
    db: DbDep,
    user: CurrentUserDep,
    outlet_id: Annotated[uuid.UUID, Query()],
    date_from: date | None = Query(default=None, alias="from"),
    date_to: date | None = Query(default=None, alias="to"),
) -> list[ForecastEventRow]:
    if not user.can_access_outlet(outlet_id):
        raise ForbiddenError("You do not have access to that outlet.")
    today = to_business_date(outlet_now())
    rows = await forecast_service.list_events(
        db,
        outlet_id,
        start=date_from or today,
        end=date_to or today + timedelta(days=60),
    )
    return [ForecastEventRow(**r) for r in rows]


@router.post(
    "/forecast/events",
    response_model=ForecastEventRow,
    dependencies=[Depends(require_management)],
    summary="Flag an event before it happens",
)
async def create_forecast_event(
    request: Request,
    body: ForecastEventCreate,
    db: DbDep,
    user: CurrentUserDep,
) -> ForecastEventRow:
    """The manual override in the spec's formula: "Durga Puja weekend,
    expect 1.3x", written down in advance. Outlet-scoped, or group-wide
    when no outlet is named."""
    if body.outlet_id is not None and not user.can_access_outlet(body.outlet_id):
        raise ForbiddenError("You do not have access to that outlet.")
    if body.outlet_id is None and not user.is_global:
        raise ForbiddenError("Only a global role can flag an event for every outlet.")
    row = await forecast_service.create_event(
        db,
        outlet_id=body.outlet_id,
        event_date=body.event_date,
        multiplier=body.multiplier,
        label=body.label,
        created_by=user.profile_id,
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=body.outlet_id,
        entity_table="forecast_events",
        entity_id=row["id"],
        action=AuditAction.CREATE,
        after={"date": str(body.event_date), "multiplier": body.multiplier, "label": body.label},
        **_ctx(request),
    )
    await db.commit()
    return ForecastEventRow(**row, created_by_name=user.full_name)


@router.delete(
    "/forecast/events/{event_id}",
    dependencies=[Depends(require_management)],
    summary="Remove an event flag",
)
async def delete_forecast_event(
    event_id: uuid.UUID,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> dict[str, Any]:
    row = (
        (
            await db.execute(
                text("select outlet_id from forecast_events where id = :id"),
                {"id": event_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFoundError("That event flag does not exist.")
    if row["outlet_id"] is not None and not user.can_access_outlet(row["outlet_id"]):
        raise ForbiddenError("You do not have access to that outlet.")
    if row["outlet_id"] is None and not user.is_global:
        raise ForbiddenError("Only a global role can remove a group-wide event.")
    await forecast_service.delete_event(db, event_id)
    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=row["outlet_id"],
        entity_table="forecast_events",
        entity_id=event_id,
        action=AuditAction.DELETE,
        after=None,
        **_ctx(request),
    )
    await db.commit()
    return {"id": str(event_id), "deleted": True}
