"""Ingesting a Petpooja export.

The shape of this, and why:

    upload  ->  hash  ->  already seen?  ->  store  ->  row  ->  background parse

**Idempotent by content, not by filename.** `data_uploads.file_sha256` is
unique, so re-uploading the same export — which somebody will do, because
nothing about a spreadsheet tells you whether you already sent it — returns the
original upload instead of doubling six weeks of revenue. Filenames carry an
export timestamp and change every time; the bytes do not.

**Parsing never runs inside the request.** A six-week export is 452 bills and
growing; the same rule that keeps photo hashing out of the request path applies
here, and for the same reason — it is bracketed by a `job_runs` row so a failure
is something an admin can read rather than a lost upload.

**The uploader names the outlet.** The Orders Master Report has no outlet
column; its preamble says only "Akira", and both outlets would export files
that look identical. So the outlet is chosen explicitly at upload, scoped to
what the uploader can access.

**The source file is kept.** That is what `storage_path` is for: an adapter
version bump should be able to re-read the original rather than asking somebody
to export it again. It does mean the raw export — customer phone numbers and
all — sits in Storage, so the bucket is private and there is no browser read
path to it. Only the salted digest ever reaches a database column.
"""

import hashlib
import uuid
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record
from app.core.config import get_settings
from app.core.deps import CurrentUser
from app.core.enums import AuditAction, UploadStatus
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.domains.sales import petpooja
from app.integrations import storage

XLSX_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
    "application/octet-stream",
}
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

SOURCE_ORDERS = "petpooja_orders"


def phone_hasher() -> "Any":
    """Salted SHA-256, per the spec's privacy note.

    The salt lives in the environment. Rotating it orphans every existing hash,
    which is the documented trade-off for it not being guessable from the
    digest alone.
    """
    salt = get_settings().PHONE_HASH_SALT

    def digest(digits: str) -> str:
        return hashlib.sha256(f"{salt}:{digits}".encode()).hexdigest()

    return digest


def storage_path_for(outlet_id: uuid.UUID, file_sha256: str) -> str:
    """Keyed by content hash, so the same file always lands in the same place
    and re-uploading overwrites rather than accumulating."""
    return f"{outlet_id}/{file_sha256}.xlsx"


async def create_upload(
    db: AsyncSession,
    user: CurrentUser,
    *,
    outlet_id: uuid.UUID,
    filename: str,
    content_type: str,
    data: bytes,
    **audit_ctx: Any,
) -> dict[str, Any]:
    """Accept a file, or hand back the upload that already holds it."""
    if not user.can_access_outlet(outlet_id):
        raise ForbiddenError("You do not have access to that outlet.")
    if content_type not in XLSX_TYPES and not filename.lower().endswith(".xlsx"):
        raise ValidationError(
            "Sales exports must be .xlsx files, as Petpooja produces them.",
            extra={"content_type": content_type},
        )
    if not data:
        raise ValidationError("That file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValidationError(
            f"That file is over {MAX_UPLOAD_BYTES // (1024 * 1024)}MB.",
            extra={"bytes": len(data)},
        )

    file_sha256 = hashlib.sha256(data).hexdigest()

    # Idempotency. Somebody will re-send the same export, because nothing about
    # a spreadsheet says whether it has been sent before.
    existing = (
        (
            await db.execute(
                text(
                    "select id, outlet_id, status, row_count, created_at"
                    " from data_uploads where file_sha256 = :h"
                ),
                {"h": file_sha256},
            )
        )
        .mappings()
        .first()
    )
    if existing is not None:
        if existing["outlet_id"] != outlet_id:
            # The same bytes cannot belong to two outlets. Refusing beats
            # silently filing a New Town export under another outlet.
            raise ConflictError(
                "This exact file has already been uploaded for a different outlet.",
                extra={"upload_id": str(existing["id"])},
            )
        return {
            **dict(existing),
            "id": str(existing["id"]),
            "already_ingested": True,
            "detail": "This file has been uploaded before; nothing was ingested twice.",
        }

    path = storage_path_for(outlet_id, file_sha256)
    await storage.upload_bytes(
        path,
        data,
        bucket=storage.SALES_UPLOAD_BUCKET,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    upload_id = (
        await db.execute(
            text(
                """
                insert into data_uploads
                    (outlet_id, uploaded_by, source, original_filename,
                     storage_path, file_sha256, status)
                values (:outlet_id, :uploaded_by, :source, :filename,
                        :path, :sha, 'received')
                returning id
                """
            ),
            {
                "outlet_id": outlet_id,
                "uploaded_by": user.profile_id,
                "source": SOURCE_ORDERS,
                "filename": filename[:250],
                "path": path,
                "sha": file_sha256,
            },
        )
    ).scalar_one()

    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=outlet_id,
        entity_table="data_uploads",
        entity_id=upload_id,
        action=AuditAction.CREATE,
        after={"filename": filename, "bytes": len(data), "sha256": file_sha256},
        **audit_ctx,
    )
    await db.commit()
    return {
        "id": str(upload_id),
        "outlet_id": outlet_id,
        "status": UploadStatus.RECEIVED.value,
        "row_count": None,
        "already_ingested": False,
        "detail": "Received. Parsing runs in the background.",
    }


async def parse_upload(db: AsyncSession, upload_id: uuid.UUID) -> dict[str, Any]:
    """Read the stored file and write its bills. Idempotent per upload.

    Runs in a background task. Anything it raises is caught by `run_job` and
    written to `job_runs`; anything it can explain is written to the upload's
    own `error_detail`, which is what the person who sent the file will read.
    """
    upload = (
        (
            await db.execute(
                text(
                    "select id, outlet_id, storage_path, original_filename, status"
                    " from data_uploads where id = :id"
                ),
                {"id": upload_id},
            )
        )
        .mappings()
        .first()
    )
    if upload is None:
        raise NotFoundError("That upload does not exist.")

    await db.execute(
        text("update data_uploads set status = 'parsing' where id = :id"), {"id": upload_id}
    )
    await db.commit()

    try:
        data = await storage.download_object(
            upload["storage_path"], bucket=storage.SALES_UPLOAD_BUCKET
        )
        result = petpooja.parse_orders(data, hash_phone=phone_hasher())
    except Exception as exc:
        await db.rollback()
        await db.execute(
            text(
                "update data_uploads set status = 'failed', error_detail = :err,"
                " parsed_at = now() where id = :id"
            ),
            {"id": upload_id, "err": f"{type(exc).__name__}: {exc}"[:2000]},
        )
        await db.commit()
        raise

    written = await _write_orders(db, upload["outlet_id"], upload_id, result)

    import json

    await db.execute(
        text(
            """
            update data_uploads
               set status = 'parsed',
                   row_count = :rows,
                   period_start = :start,
                   period_end = :end,
                   warnings = cast(:warnings as jsonb),
                   adapter_version = :adapter,
                   parsed_net_paise = :net,
                   reported_net_paise = :reported,
                   parsed_at = now(),
                   error_detail = null
             where id = :id
            """
        ),
        {
            "id": upload_id,
            "rows": len(result.orders),
            "start": result.period_start,
            "end": result.period_end,
            "warnings": json.dumps(result.warnings, default=str),
            "adapter": result.adapter_version,
            "reported": result.reported_net_paise,
            "net": result.net_paise,
        },
    )
    await db.commit()

    return {
        "upload_id": str(upload_id),
        "orders_parsed": len(result.orders),
        "orders_written": written["inserted"],
        "orders_updated": written["updated"],
        "net_paise": result.net_paise,
        "period": [str(result.period_start), str(result.period_end)],
        "warnings": len(result.warnings),
        "adapter": result.adapter_version,
    }


#: One statement per chunk rather than one per bill. The first version of this
#: looped, and 452 bills took 75 seconds against Supabase — 452 round trips.
#: Six weeks of history is the small case; a year would have taken eleven
#: minutes and looked like a hang.
WRITE_CHUNK = 1000

_UPSERT_ORDERS = text(
    """
    insert into sales_orders
        (outlet_id, upload_id, external_bill_no, business_date, ordered_at,
         channel, covers, gross_paise, discount_paise, tax_paise, net_paise,
         payment_mode, table_no, customer_phone_hash)
    select :outlet_id, :upload_id, b.bill_no, b.business_date, b.ordered_at,
           cast(b.channel as sales_channel), b.covers, b.gross, b.discount,
           b.tax, b.net, b.payment, b.table_no, b.phone
      from unnest(
              cast(:bill_nos as text[]),
              cast(:business_dates as date[]),
              cast(:ordered_ats as timestamptz[]),
              cast(:channels as text[]),
              cast(:covers as integer[]),
              cast(:gross as bigint[]),
              cast(:discount as bigint[]),
              cast(:tax as bigint[]),
              cast(:net as bigint[]),
              cast(:payments as text[]),
              cast(:tables as text[]),
              cast(:phones as text[])
           ) as b(bill_no, business_date, ordered_at, channel, covers, gross,
                  discount, tax, net, payment, table_no, phone)
    on conflict (outlet_id, external_bill_no) do update
       set upload_id = excluded.upload_id,
           business_date = excluded.business_date,
           ordered_at = excluded.ordered_at,
           channel = excluded.channel,
           covers = excluded.covers,
           gross_paise = excluded.gross_paise,
           discount_paise = excluded.discount_paise,
           tax_paise = excluded.tax_paise,
           net_paise = excluded.net_paise,
           payment_mode = excluded.payment_mode,
           table_no = excluded.table_no,
           customer_phone_hash = excluded.customer_phone_hash
    returning (xmax = 0) as inserted
    """
)


async def _write_orders(
    db: AsyncSession,
    outlet_id: uuid.UUID,
    upload_id: uuid.UUID,
    result: petpooja.ParseResult,
) -> dict[str, int]:
    """Upsert every bill on (outlet_id, external_bill_no), set at a time.

    Petpooja exports overlap: the next one covers the same six weeks plus a few
    more days. A bill already present is updated in place rather than inserted
    again — the unique constraint would refuse it anyway, and skipping would
    leave a corrected bill showing its old total forever.

    `xmax = 0` is Postgres telling us which rows the upsert actually inserted
    rather than updated, so the count is the database's answer and not a guess
    made before the statement ran.
    """
    inserted = updated = 0
    orders = result.orders
    for offset in range(0, len(orders), WRITE_CHUNK):
        chunk = orders[offset : offset + WRITE_CHUNK]
        rows = (
            (
                await db.execute(
                    _UPSERT_ORDERS,
                    {
                        "outlet_id": outlet_id,
                        "upload_id": upload_id,
                        "bill_nos": [o.external_bill_no for o in chunk],
                        "business_dates": [o.business_date for o in chunk],
                        "ordered_ats": [o.ordered_at for o in chunk],
                        "channels": [o.channel.value if o.channel else None for o in chunk],
                        "covers": [o.covers for o in chunk],
                        "gross": [o.gross_paise for o in chunk],
                        "discount": [o.discount_paise for o in chunk],
                        "tax": [o.tax_paise for o in chunk],
                        "net": [o.net_paise for o in chunk],
                        "payments": [o.payment_mode for o in chunk],
                        "tables": [o.table_no for o in chunk],
                        "phones": [o.customer_phone_hash for o in chunk],
                    },
                )
            )
            .scalars()
            .all()
        )
        inserted += sum(1 for was_new in rows if was_new)
        updated += sum(1 for was_new in rows if not was_new)
    return {"inserted": inserted, "updated": updated}


async def background_parse(upload_id: uuid.UUID, outlet_id: uuid.UUID) -> None:
    """What the upload endpoint hands to a BackgroundTask."""

    async def body(db: AsyncSession) -> dict[str, Any]:
        return await parse_upload(db, upload_id)

    from app.jobs.runner import run_job

    await run_job("sales_ingest", body, outlet_id=outlet_id)


# ---------------------------------------------------------------------------
# Reading it back
# ---------------------------------------------------------------------------


async def list_uploads(
    db: AsyncSession, user: CurrentUser, *, outlet_id: uuid.UUID | None, limit: int
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if outlet_id is not None:
        if not user.can_access_outlet(outlet_id):
            raise ForbiddenError("You do not have access to that outlet.")
        clauses.append("u.outlet_id = :outlet_id")
        params["outlet_id"] = outlet_id
    elif not user.is_global:
        if not user.outlet_ids:
            return []
        clauses.append("u.outlet_id = any(:ids)")
        params["ids"] = sorted(user.outlet_ids)
    where = ("where " + " and ".join(clauses)) if clauses else ""

    import json

    rows = (
        (
            await db.execute(
                text(
                    f"""
                    select u.id, u.outlet_id, o.code as outlet_code, u.source,
                           u.original_filename, u.file_sha256, u.status,
                           u.row_count, u.period_start, u.period_end,
                           u.warnings, u.error_detail, u.adapter_version,
                           u.parsed_net_paise, u.created_at, u.parsed_at,
                           p.full_name as uploaded_by_name
                      from data_uploads u
                      join outlets o on o.id = u.outlet_id
                      left join profiles p on p.id = u.uploaded_by
                     {where}
                     order by u.created_at desc
                     limit :limit
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
        raw = data.get("warnings")
        data["warnings"] = json.loads(raw) if isinstance(raw, str) else (raw or [])
        out.append(data)
    return out


async def list_orders(
    db: AsyncSession,
    user: CurrentUser,
    *,
    outlet_id: uuid.UUID | None,
    date_from: date | None,
    date_to: date | None,
    limit: int,
) -> list[dict[str, Any]]:
    """The raw table view. Newest trading day first, newest bill first."""
    clauses: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if outlet_id is not None:
        if not user.can_access_outlet(outlet_id):
            raise ForbiddenError("You do not have access to that outlet.")
        clauses.append("s.outlet_id = :outlet_id")
        params["outlet_id"] = outlet_id
    elif not user.is_global:
        if not user.outlet_ids:
            return []
        clauses.append("s.outlet_id = any(:ids)")
        params["ids"] = sorted(user.outlet_ids)
    if date_from is not None:
        clauses.append("s.business_date >= :date_from")
        params["date_from"] = date_from
    if date_to is not None:
        clauses.append("s.business_date <= :date_to")
        params["date_to"] = date_to
    where = ("where " + " and ".join(clauses)) if clauses else ""

    rows = (
        (
            await db.execute(
                text(
                    f"""
                    select s.id, s.outlet_id, o.code as outlet_code,
                           s.external_bill_no, s.business_date, s.ordered_at,
                           s.channel, s.covers, s.gross_paise, s.discount_paise,
                           s.tax_paise, s.net_paise, s.payment_mode, s.table_no,
                           s.customer_phone_hash is not null as has_phone
                      from sales_orders s
                      join outlets o on o.id = s.outlet_id
                     {where}
                     order by s.business_date desc, s.ordered_at desc
                     limit :limit
                    """
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def daily_totals(
    db: AsyncSession,
    user: CurrentUser,
    *,
    outlet_id: uuid.UUID,
    date_from: date | None,
    date_to: date | None,
) -> list[dict[str, Any]]:
    """Net sales per trading day. Grouped by `business_date`, never by
    `ordered_at::date` — that would split every night at midnight."""
    if not user.can_access_outlet(outlet_id):
        raise ForbiddenError("You do not have access to that outlet.")
    rows = (
        (
            await db.execute(
                text(
                    """
                    select business_date,
                           count(*) as bills,
                           coalesce(sum(net_paise), 0) as net_paise,
                           coalesce(sum(covers), 0) as covers
                      from sales_orders
                     where outlet_id = :outlet_id
                       and (cast(:date_from as date) is null
                            or business_date >= cast(:date_from as date))
                       and (cast(:date_to as date) is null
                            or business_date <= cast(:date_to as date))
                     group by business_date
                     order by business_date desc
                    """
                ),
                {"outlet_id": outlet_id, "date_from": date_from, "date_to": date_to},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]
