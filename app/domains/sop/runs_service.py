"""Checklist runs: materialisation, the item-by-item flow, photos, submit.

Actor resolution is the shared-tablet model (D3) made real. A request is
performed BY a person even when it is authenticated AS a device:

- An individual login (managers, or staff with their own account) acts as
  themselves.
- A device session must carry an actor token minted by /floor/identify, and
  the person in that token is who started/submitted the run.

Every write here records the resolved person, so separation of duties keeps
meaning on a shared tablet.
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoring
from app.core.actor import verify as verify_actor
from app.core.audit import record
from app.core.business_date import OUTLET_TZ, due_at
from app.core.business_date import business_date as to_business_date
from app.core.deps import CurrentUser
from app.core.enums import AuditAction, ItemResult, RunStatus, UserRole
from app.core.errors import (
    AuthError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    ValidationError,
)
from app.domains.sop import integrity
from app.integrations import storage

ALLOWED_PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_PHOTO_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class FloorActor:
    """The person performing floor work, however the request authenticated."""

    profile_id: uuid.UUID
    full_name: str
    role: UserRole
    #: Outlets this actor may touch. For a device actor, exactly the tablet's.
    outlet_ids: set[uuid.UUID] | None  # None = every outlet (global roles)
    device_id: uuid.UUID | None

    def can_touch(self, outlet_id: uuid.UUID) -> bool:
        return self.outlet_ids is None or outlet_id in self.outlet_ids


def resolve_actor(user: CurrentUser, actor_token: str | None) -> FloorActor:
    if user.device is None:
        return FloorActor(
            profile_id=user.profile_id,
            full_name=user.full_name,
            role=user.global_role,
            outlet_ids=None if user.is_global else set(user.outlet_ids),
            device_id=None,
        )
    if not actor_token:
        raise AuthError(
            "This tablet needs to know who you are. Enter your PIN first.",
            extra={"identify_at": "/floor/identify"},
        )
    actor = verify_actor(actor_token, device_id=user.device.device_id)
    return FloorActor(
        profile_id=actor.profile_id,
        full_name=actor.full_name,
        role=UserRole(actor.role),
        outlet_ids={user.device.outlet_id},
        device_id=user.device.device_id,
    )


# ---------------------------------------------------------------------------
# Materialisation
# ---------------------------------------------------------------------------


def assignment_occurs_on(
    day: date,
    *,
    active_weekdays: list[int],
    interval_days: int | None,
    anchor_date: date | None,
) -> bool:
    """Does this assignment produce a run on this business date?

    Weekday cadences use Postgres dow numbering (0 = Sunday). Interval
    cadences count whole days from their anchor; a day before the anchor
    never occurs.
    """
    if interval_days is not None:
        if anchor_date is None or day < anchor_date:
            return False
        return (day - anchor_date).days % interval_days == 0
    # Python: Monday=0..Sunday=6. Postgres dow: Sunday=0..Saturday=6.
    postgres_dow = (day.weekday() + 1) % 7
    return postgres_dow in active_weekdays


async def materialise_runs(
    db: AsyncSession,
    *,
    for_date: date,
    triggered_by: uuid.UUID | None = None,
) -> dict[str, int]:
    """Create the day's pending runs. Idempotent: the unique constraint on
    (assignment_id, business_date, day_part) makes re-running safe, and
    ON CONFLICT DO NOTHING makes it quiet."""
    assignments = (
        await db.execute(
            text(
                """
                select a.id, a.template_id, a.outlet_id, a.active_weekdays,
                       a.interval_days, a.anchor_date, a.due_time_local,
                       t.version as template_version, t.day_part,
                       o.timezone
                  from checklist_assignments a
                  join checklist_templates t on t.id = a.template_id
                  join outlets o on o.id = a.outlet_id
                 where a.is_active and a.deleted_at is null
                   and t.is_active and t.deleted_at is null
                   and o.is_active and o.deleted_at is null
                """
            )
        )
    ).mappings()

    created = skipped = 0
    for assignment in assignments:
        if not assignment_occurs_on(
            for_date,
            active_weekdays=list(assignment["active_weekdays"]),
            interval_days=assignment["interval_days"],
            anchor_date=assignment["anchor_date"],
        ):
            continue

        run_due = due_at(for_date, assignment["due_time_local"], assignment["timezone"])
        run_id = (
            await db.execute(
                text(
                    """
                    insert into checklist_runs
                        (assignment_id, template_id, template_version, outlet_id,
                         business_date, day_part, status, due_at)
                    values (:assignment_id, :template_id, :template_version,
                            :outlet_id, :business_date, cast(:day_part as day_part),
                            'pending', :due_at)
                    on conflict (assignment_id, business_date, day_part) do nothing
                    returning id
                    """
                ),
                {
                    "assignment_id": assignment["id"],
                    "template_id": assignment["template_id"],
                    "template_version": assignment["template_version"],
                    "outlet_id": assignment["outlet_id"],
                    "business_date": for_date,
                    "day_part": assignment["day_part"],
                    "due_at": run_due,
                },
            )
        ).scalar()
        if run_id is None:
            skipped += 1
            continue

        # Items snapshot the exact definition they will be answered against.
        await db.execute(
            text(
                """
                insert into checklist_run_items
                    (run_id, template_item_id, sort_order, template_item_version_id)
                select :run_id, i.id, i.sort_order, v.id
                  from checklist_template_items i
                  join checklist_template_item_versions v
                    on v.template_item_id = i.id
                   and v.template_version = :version
                 where i.template_id = :template_id and i.deleted_at is null
                """
            ),
            {
                "run_id": run_id,
                "version": assignment["template_version"],
                "template_id": assignment["template_id"],
            },
        )
        created += 1

    await record(
        db,
        actor_profile_id=triggered_by,
        entity_table="checklist_runs",
        entity_id=None,
        action=AuditAction.CREATE,
        after={
            "materialised_for": str(for_date),
            "created": created,
            "already_existed": skipped,
        },
    )
    await db.commit()
    return {"created": created, "already_existed": skipped}


# ---------------------------------------------------------------------------
# Reading runs
# ---------------------------------------------------------------------------

_RUN_SQL = """
    select r.id, r.assignment_id, r.template_id, r.template_version, r.outlet_id,
           r.business_date, r.day_part, r.status, r.due_at, r.is_late,
           r.started_by, r.started_at, r.submitted_by, r.submitted_at,
           cast(r.score_pct as float8) as score_pct, r.critical_fail_count, r.rejection_reason,
           t.name as template_name, t.name_bn as template_name_bn,
           o.code as outlet_code, o.timezone as outlet_timezone,
           a.assigned_role, a.grace_minutes,
           (select count(*) from checklist_run_items ri where ri.run_id = r.id)
               as item_count,
           (select count(*) from checklist_run_items ri
             where ri.run_id = r.id and ri.result <> 'pending') as answered_count
      from checklist_runs r
      join checklist_templates t on t.id = r.template_id
      join checklist_assignments a on a.id = r.assignment_id
      join outlets o on o.id = r.outlet_id
"""

#: What each floor role may see in the today list. Leads supervise staff work;
#: managers see everything at their outlet.
_VISIBLE_ROLES: dict[UserRole, list[str] | None] = {
    UserRole.STAFF: ["staff"],
    UserRole.SHIFT_LEAD: ["staff", "shift_lead"],
    UserRole.OUTLET_MANAGER: None,
    UserRole.OPS_MANAGER: None,
    UserRole.OWNER: None,
}


async def list_today(
    db: AsyncSession, actor: FloorActor, *, outlet_id: uuid.UUID, now: datetime
) -> list[dict[str, Any]]:
    if not actor.can_touch(outlet_id):
        raise ForbiddenError("You do not have access to that outlet.")
    today = to_business_date(now)
    clauses = ["r.outlet_id = :outlet_id", "r.business_date = :business_date"]
    params: dict[str, Any] = {"outlet_id": outlet_id, "business_date": today}

    visible = _VISIBLE_ROLES.get(actor.role)
    if visible is not None:
        clauses.append("a.assigned_role = any(cast(:roles as user_role[]))")
        params["roles"] = visible

    sql = _RUN_SQL + " where " + " and ".join(clauses) + " order by r.due_at nulls last"
    return [dict(r) for r in (await db.execute(text(sql), params)).mappings()]


async def get_run(db: AsyncSession, actor: FloorActor, run_id: uuid.UUID) -> dict[str, Any]:
    run = (
        (await db.execute(text(_RUN_SQL + " where r.id = :id"), {"id": run_id})).mappings().first()
    )
    if run is None:
        raise NotFoundError("That run does not exist.")
    if not actor.can_touch(run["outlet_id"]):
        raise ForbiddenError("You do not have access to that outlet.")

    items = (
        await db.execute(
            text(
                """
                select ri.id, ri.template_item_id, ri.sort_order, ri.result,
                       cast(ri.value_numeric as float8) as value_numeric,
                       ri.value_text, ri.out_of_range, ri.note,
                       ri.photo_path, ri.photo_uploaded_at,
                       v.title, v.title_bn, v.instruction, v.instruction_bn,
                       v.requires_photo, v.requires_value, v.value_type,
                       cast(v.value_min as float8) as value_min,
                       cast(v.value_max as float8) as value_max, v.value_unit,
                       v.is_critical, v.allow_na
                  from checklist_run_items ri
                  join checklist_template_item_versions v
                    on v.id = ri.template_item_version_id
                 where ri.run_id = :run_id
                 order by ri.sort_order
                """
            ),
            {"run_id": run_id},
        )
    ).mappings()
    return {**run, "items": [dict(i) for i in items]}


# ---------------------------------------------------------------------------
# The flow: start -> answer items -> photos -> submit
# ---------------------------------------------------------------------------


async def start_run(
    db: AsyncSession, actor: FloorActor, run_id: uuid.UUID, **audit_ctx: Any
) -> dict[str, Any]:
    """pending -> in_progress. Idempotent: starting an in_progress run returns
    it unchanged, so a retry after a dropped response cannot double-start."""
    run = await get_run(db, actor, run_id)
    if run["status"] == RunStatus.IN_PROGRESS.value:
        return run
    if run["status"] != RunStatus.PENDING.value:
        raise ConflictError(
            f"This run is {run['status']} and cannot be started.",
            extra={"status": run["status"]},
        )
    await db.execute(
        text(
            """
            update checklist_runs
               set status = 'in_progress', started_by = :actor,
                   started_at = now(), device_id = :device_id
             where id = :id and status = 'pending'
            """
        ),
        {"id": run_id, "actor": actor.profile_id, "device_id": actor.device_id},
    )
    await record(
        db,
        actor_profile_id=actor.profile_id,
        outlet_id=run["outlet_id"],
        entity_table="checklist_runs",
        entity_id=run_id,
        action=AuditAction.UPDATE,
        after={"status": "in_progress"},
        **audit_ctx,
    )
    await db.commit()
    return await get_run(db, actor, run_id)


async def _load_run_item(db: AsyncSession, run_id: uuid.UUID, item_id: uuid.UUID) -> dict[str, Any]:
    row = (
        (
            await db.execute(
                text(
                    """
                select ri.id, ri.run_id, ri.result, r.status as run_status,
                       r.outlet_id, r.business_date, r.started_at, r.submitted_at,
                       v.title, v.requires_photo, v.requires_value, v.value_type,
                       v.value_min, v.value_max, v.allow_na, v.is_critical
                  from checklist_run_items ri
                  join checklist_runs r on r.id = ri.run_id
                  join checklist_template_item_versions v
                    on v.id = ri.template_item_version_id
                 where ri.run_id = :run_id and ri.id = :item_id
                """
                ),
                {"run_id": run_id, "item_id": item_id},
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        raise NotFoundError("That item does not belong to this run.")
    return dict(row)


def _require_open(run_status: str) -> None:
    if run_status in (RunStatus.APPROVED.value, RunStatus.MISSED.value):
        raise ConflictError(
            "This run is locked and can no longer be edited.",
            extra={"status": run_status},
        )
    if run_status == RunStatus.SUBMITTED.value:
        raise ConflictError(
            "This run has been submitted. Ask a manager to reject it if something needs fixing.",
            extra={"status": run_status},
        )
    if run_status == RunStatus.PENDING.value:
        raise ConflictError("Start the run before answering items.")


async def answer_item(
    db: AsyncSession,
    actor: FloorActor,
    run_id: uuid.UUID,
    item_id: uuid.UUID,
    *,
    result: ItemResult,
    value_numeric: float | None,
    value_text: str | None,
    note: str | None,
    **audit_ctx: Any,
) -> dict[str, Any]:
    item = await _load_run_item(db, run_id, item_id)
    if not actor.can_touch(item["outlet_id"]):
        raise ForbiddenError("You do not have access to that outlet.")
    _require_open(item["run_status"])

    if result is ItemResult.NA and not item["allow_na"]:
        raise ValidationError(f"'{item['title']}' cannot be marked N/A.")
    if result is ItemResult.FAIL and not (note and note.strip()):
        raise ValidationError(
            f"'{item['title']}' failed — add a note saying what you found.",
            extra={"item_id": str(item_id), "needs": "note"},
        )
    if (
        item["requires_value"]
        and result is ItemResult.PASS
        and value_numeric is None
        and item["value_type"] in ("number", "temperature_c")
    ):
        raise ValidationError(
            f"'{item['title']}' needs a recorded value.",
            extra={"item_id": str(item_id), "needs": "value"},
        )

    is_out = (
        scoring.out_of_range(value_numeric, item["value_min"], item["value_max"])
        if value_numeric is not None
        else False
    )

    await db.execute(
        text(
            """
            update checklist_run_items
               set result = cast(:result as item_result),
                   value_numeric = :value_numeric,
                   value_text = :value_text,
                   out_of_range = :out_of_range,
                   note = :note,
                   completed_at = now()
             where id = :id
            """
        ),
        {
            "id": item_id,
            "result": result.value,
            "value_numeric": value_numeric,
            "value_text": value_text,
            "out_of_range": is_out,
            "note": note,
        },
    )
    await db.commit()
    return {"item_id": str(item_id), "result": result.value, "out_of_range": is_out}


def photo_path_for(
    outlet_id: uuid.UUID, business_day: date, run_id: uuid.UUID, item_id: uuid.UUID
) -> str:
    return f"{outlet_id}/{business_day}/{run_id}/{item_id}.jpg"


async def create_photo_upload(
    db: AsyncSession,
    actor: FloorActor,
    run_id: uuid.UUID,
    item_id: uuid.UUID,
    *,
    content_type: str,
    byte_size: int,
) -> dict[str, Any]:
    item = await _load_run_item(db, run_id, item_id)
    if not actor.can_touch(item["outlet_id"]):
        raise ForbiddenError("You do not have access to that outlet.")
    _require_open(item["run_status"])
    if not item["requires_photo"]:
        raise ValidationError(f"'{item['title']}' does not take a photo.")
    if content_type not in ALLOWED_PHOTO_TYPES:
        raise ValidationError("Photos must be JPEG, PNG or WebP.")
    if byte_size > MAX_PHOTO_BYTES:
        raise ValidationError(
            "That photo is over 5MB. The app resizes before upload — "
            "if you see this, the resize step was skipped."
        )

    path = photo_path_for(item["outlet_id"], item["business_date"], run_id, item_id)
    signed = await storage.create_signed_upload(path)
    return {"upload_url": signed.url, "token": signed.token, "path": signed.path}


async def confirm_photo(
    db: AsyncSession,
    actor: FloorActor,
    run_id: uuid.UUID,
    item_id: uuid.UUID,
    *,
    path: str,
) -> dict[str, Any]:
    """Metadata is written ONLY after the object is confirmed to exist."""
    item = await _load_run_item(db, run_id, item_id)
    if not actor.can_touch(item["outlet_id"]):
        raise ForbiddenError("You do not have access to that outlet.")
    _require_open(item["run_status"])

    expected = photo_path_for(item["outlet_id"], item["business_date"], run_id, item_id)
    if path != expected:
        # The path is derived server-side; a differing one is a client bug or
        # an attempt to claim someone else's object.
        raise ValidationError("That is not this item's photo path.")

    stat = await storage.stat_object(path)
    if not stat.exists:
        raise ValidationError(
            "The photo has not arrived in storage yet. Upload it first, then confirm."
        )

    await db.execute(
        text(
            """
            update checklist_run_items
               set photo_path = :path, photo_uploaded_at = now(),
                   photo_bytes = :bytes
             where id = :id
            """
        ),
        {"id": item_id, "path": path, "bytes": stat.size_bytes},
    )
    await db.commit()
    # The hash, the luminance and the duplicate lookback all need the bytes
    # back out of storage. The router hands this to a BackgroundTask: the floor
    # must not wait on work nobody on the floor is waiting for.
    return {
        "item_id": str(item_id),
        "photo_path": path,
        "photo_bytes": stat.size_bytes,
        "outlet_id": item["outlet_id"],
        "business_date": item["business_date"],
    }


async def submit_run(
    db: AsyncSession,
    actor: FloorActor,
    run_id: uuid.UUID,
    *,
    geo_lat: float | None,
    geo_lng: float | None,
    **audit_ctx: Any,
) -> dict[str, Any]:
    run = await get_run(db, actor, run_id)
    _require_open(run["status"])

    # Completeness: every item answered; every requires_photo item confirmed.
    unanswered = [i for i in run["items"] if i["result"] == ItemResult.PENDING.value]
    missing_photos = [
        i
        for i in run["items"]
        if i["requires_photo"]
        and i["result"] in (ItemResult.PASS.value, ItemResult.FAIL.value)
        and not i["photo_path"]
    ]
    if unanswered or missing_photos:
        raise ValidationError(
            "The run is not complete.",
            extra={
                "unanswered_item_ids": [str(i["id"]) for i in unanswered],
                "missing_photo_item_ids": [str(i["id"]) for i in missing_photos],
            },
        )

    scorable = [
        scoring.ScorableItem(result=ItemResult(i["result"]), is_critical=i["is_critical"])
        for i in run["items"]
    ]
    score = scoring.run_score(scorable)
    critical_fails = scoring.critical_fail_count(scorable)

    now = datetime.now(tz=OUTLET_TZ)
    is_late = False
    minutes_late: int | None = None
    if run["due_at"] is not None:
        deadline = run["due_at"] + timedelta(minutes=run["grace_minutes"] or 0)
        if now > deadline:
            is_late = True
            minutes_late = int((now - deadline).total_seconds() // 60)

    # Geofence: null geo is NOT a flag — it is often a permission the staff
    # member cannot change. geo_ok stays null and is counted separately.
    geo_ok: bool | None = None
    outlet = (
        (
            await db.execute(
                text("select geo_lat, geo_lng, geofence_radius_m from outlets where id = :id"),
                {"id": run["outlet_id"]},
            )
        )
        .mappings()
        .first()
    )
    if (
        geo_lat is not None
        and geo_lng is not None
        and outlet
        and outlet["geo_lat"] is not None
        and outlet["geo_lng"] is not None
    ):
        distance = scoring.haversine_m(geo_lat, geo_lng, outlet["geo_lat"], outlet["geo_lng"])
        geo_ok = distance <= (outlet["geofence_radius_m"] or 150)

    await db.execute(
        text(
            """
            update checklist_runs
               set status = 'submitted', submitted_by = :actor, submitted_at = now(),
                   score_pct = :score, critical_fail_count = :critical_fails,
                   is_late = :is_late, minutes_late = :minutes_late,
                   submit_geo_lat = :geo_lat, submit_geo_lng = :geo_lng,
                   geo_ok = :geo_ok,
                   device_id = coalesce(:device_id, device_id)
             where id = :id
            """
        ),
        {
            "id": run_id,
            "actor": actor.profile_id,
            "score": score,
            "critical_fails": critical_fails,
            "is_late": is_late,
            "minutes_late": minutes_late,
            "geo_lat": geo_lat,
            "geo_lng": geo_lng,
            "geo_ok": geo_ok,
            "device_id": actor.device_id,
        },
    )

    # Every critical fail becomes a tracked exception, immediately — and in
    # one statement, because this runs inside submit while somebody on the
    # floor is watching a spinner. A statement per fail meant a bad night (the
    # exact case with several critical fails) got slower in proportion to how
    # bad it was.
    fails = [
        item
        for item in run["items"]
        if item["is_critical"] and item["result"] == ItemResult.FAIL.value
    ]
    if fails:
        await db.execute(
            text(
                """
                insert into sop_exceptions
                    (run_item_id, outlet_id, business_date, severity,
                     title, detail, photo_path)
                select f.run_item_id, :outlet_id, :business_date, 'high',
                       f.title, f.detail, f.photo_path
                  from unnest(
                           cast(:item_ids as uuid[]), cast(:titles as text[]),
                           cast(:details as text[]), cast(:photo_paths as text[])
                       ) as f(run_item_id, title, detail, photo_path)
                """
            ),
            {
                "outlet_id": run["outlet_id"],
                "business_date": run["business_date"],
                "item_ids": [item["id"] for item in fails],
                "titles": [f"Critical fail: {item['title']}" for item in fails],
                "details": [item["note"] for item in fails],
                "photo_paths": [item["photo_path"] for item in fails],
            },
        )

    await record(
        db,
        actor_profile_id=actor.profile_id,
        outlet_id=run["outlet_id"],
        entity_table="checklist_runs",
        entity_id=run_id,
        action=AuditAction.UPDATE,
        after={
            "status": "submitted",
            "score_pct": score,
            "critical_fail_count": critical_fails,
            "is_late": is_late,
            "geo_ok": geo_ok,
        },
        **audit_ctx,
    )

    # Run-level integrity, inline: no image work, only columns just written. A
    # manager opening the queue five seconds later must already see `late` and
    # `out_of_geofence`. The photo-level checks are caught up in the background.
    await integrity.evaluate_run(db, run_id)
    await db.commit()
    return await get_run(db, actor, run_id)
