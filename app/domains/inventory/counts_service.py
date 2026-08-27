"""Stock count ingestion: from a photographed sheet to a confirmed count.

The flow, and who owns each step (spec section 6):

    upload (PDF/photo)  ->  data_uploads row + private bucket      [code]
    page images         ->  transcribed rows with confidence       [LLM]
    raw text            ->  item mapping + normalised quantities   [code]
    what code refused   ->  a person resolves it, once             [human]
    confirmed count     ->  the outlet's on-hand truth             [human]

Everything the model touches lands in raw_* columns and is re-derivable; the
derived columns record who or what derived them. A Groq-extracted line is
ALWAYS needs_review regardless of its claimed confidence, because the measured
failure mode (values shifted one row at 0.9 confidence) is invisible to any
threshold — see sheet_extraction.py.

Extraction runs as a background job bracketed by job_runs, like every other
background task in this codebase. A sheet that cannot be read is a failed job
with a reason — never a silently empty count.
"""

import hashlib
import io
import json
import logging
import uuid
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.business_date import business_date as to_business_date
from app.core.business_date import outlet_now
from app.core.deps import CurrentUser
from app.core.enums import InventoryUnit
from app.core.errors import ConflictError, ForbiddenError, NotFoundError, ValidationError
from app.domains.inventory.mapping import CatalogueEntry, Mapper, normalise
from app.domains.inventory.normalize import Parsed, Refused, parse_quantity
from app.integrations import sheet_extraction, storage
from app.jobs.runner import run_job

logger = logging.getLogger(__name__)

STOCK_SHEET_BUCKET = "stock-sheets"
UPLOAD_SOURCE = "stock_sheet"

ACCEPTED_TYPES = {
    "application/pdf": "pdf",
    "image/jpeg": "jpg",
    "image/png": "png",
}

#: Below this extraction confidence a line needs a human even from the
#: production extractor. Groq lines need one unconditionally.
REVIEW_BELOW = 0.85

#: Long side of the page image sent to the model. The full-resolution scans
#: are ~2500px; this keeps tokens sane without losing the handwriting.
PAGE_LONG_SIDE = 1800

#: Hard byte budget per page image. Groq's request cap answered 413 above
#: roughly this; Anthropic's limit is far higher, but one budget keeps the
#: pipeline provider-agnostic.
PAGE_MAX_BYTES = 300_000


def sheet_path_for(outlet_id: uuid.UUID, sha256: str, extension: str) -> str:
    return f"{outlet_id}/{sha256}.{extension}"


async def _require_outlet_access(user: CurrentUser, outlet_id: uuid.UUID) -> None:
    if not user.can_access_outlet(outlet_id):
        raise ForbiddenError("You do not have access to that outlet.")


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


async def create_sheet_upload(
    db: AsyncSession,
    user: CurrentUser,
    *,
    outlet_id: uuid.UUID,
    filename: str,
    content_type: str,
    data: bytes,
) -> dict[str, Any]:
    """Store the file, open the count shell, hand extraction to the caller's
    background task. Idempotent by content hash, same as the sales exports."""
    await _require_outlet_access(user, outlet_id)
    if content_type not in ACCEPTED_TYPES:
        raise ValidationError("Count sheets arrive as a PDF or a photo (.pdf, .jpg, .png).")
    if not data:
        raise ValidationError("That file is empty.")

    sha = hashlib.sha256(data).hexdigest()
    existing = (
        (
            await db.execute(
                text("select id from data_uploads where file_sha256 = :sha"),
                {"sha": sha},
            )
        )
        .mappings()
        .first()
    )
    if existing:
        count = (
            (
                await db.execute(
                    text("select id, status from stock_counts where upload_id = :u"),
                    {"u": existing["id"]},
                )
            )
            .mappings()
            .first()
        )
        if count:
            return {
                "upload_id": str(existing["id"]),
                "count_id": str(count["id"]),
                "status": count["status"],
                "already_ingested": True,
            }

    extension = ACCEPTED_TYPES[content_type]
    path = sheet_path_for(outlet_id, sha, extension)
    await storage.ensure_private_bucket(
        STOCK_SHEET_BUCKET,
        file_size_limit=25 * 1024 * 1024,
        allowed_mime_types=list(ACCEPTED_TYPES),
    )
    await storage.upload_bytes(path, data, bucket=STOCK_SHEET_BUCKET, content_type=content_type)

    upload_id = (
        await db.execute(
            text(
                """
                insert into data_uploads
                    (outlet_id, source, original_filename, storage_path,
                     file_sha256, status, uploaded_by)
                values (:o, :src, :name, :path, :sha, 'received', :by)
                on conflict (file_sha256) do update set status = data_uploads.status
                returning id
                """
            ),
            {
                "o": outlet_id,
                "src": UPLOAD_SOURCE,
                "name": filename,
                "path": path,
                "sha": sha,
                "by": user.profile_id,
            },
        )
    ).scalar_one()

    # The trading day the count belongs to defaults to the outlet's current
    # business date; the extractor's read of the sheet's own Date field and
    # the confirming human can both override it.
    count_id = (
        await db.execute(
            text(
                """
                insert into stock_counts (outlet_id, upload_id, business_date, status)
                values (:o, :u, :d, 'extracting')
                returning id
                """
            ),
            {"o": outlet_id, "u": upload_id, "d": to_business_date(outlet_now())},
        )
    ).scalar_one()
    await db.commit()
    return {
        "upload_id": str(upload_id),
        "count_id": str(count_id),
        "status": "extracting",
        "already_ingested": False,
    }


# ---------------------------------------------------------------------------
# Extraction (background)
# ---------------------------------------------------------------------------


def _page_images(data: bytes, content_type: str) -> list[bytes]:
    """PDF pages or the single photo, upright and resized for the model.

    AKIRA's sheets are printed landscape and photographed portrait, so a
    portrait page is rotated to landscape. Verified against the real fixture;
    if a future batch arrives the other way up the extractor's confidence
    collapses and the count fails loudly rather than reading garbage.
    """
    from PIL import Image

    raws: list[bytes] = []
    if content_type == "application/pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        for page in reader.pages:
            for image in page.images:
                raws.append(image.data)
                break  # one scan per page on these sheets
    else:
        raws.append(data)

    prepared: list[bytes] = []
    for raw in raws:
        img: Image.Image = Image.open(io.BytesIO(raw))
        if img.mode != "RGB":
            img = img.convert("RGB")
        if img.width < img.height:
            img = img.transpose(Image.Transpose.ROTATE_90)
        img.thumbnail((PAGE_LONG_SIDE, PAGE_LONG_SIDE))
        # Providers cap request size (Groq answered 413 to a dense scan at
        # quality 88). Step quality, then resolution, until the page fits —
        # legibility of handwriting degrades gracefully; a refused request
        # reads nothing at all.
        encoded = b""
        for quality in (88, 80, 72):
            buffer = io.BytesIO()
            img.save(buffer, "JPEG", quality=quality)
            encoded = buffer.getvalue()
            if len(encoded) <= PAGE_MAX_BYTES:
                break
        while len(encoded) > PAGE_MAX_BYTES and img.width > 900:
            img = img.resize((int(img.width * 0.85), int(img.height * 0.85)))
            buffer = io.BytesIO()
            img.save(buffer, "JPEG", quality=80)
            encoded = buffer.getvalue()
        prepared.append(encoded)
    return prepared


async def _load_mapper(db: AsyncSession) -> tuple[Mapper, dict[str, InventoryUnit], list[str]]:
    rows = (
        (
            await db.execute(
                text(
                    """
                    select i.id, i.name, i.name_bn, i.unit
                      from inventory_items i
                     where i.deleted_at is null and i.is_active
                    """
                )
            )
        )
        .mappings()
        .all()
    )
    entries = [
        CatalogueEntry(item_id=str(r["id"]), name=r["name"], name_bn=r["name_bn"], unit=r["unit"])
        for r in rows
    ]
    units = {str(r["id"]): InventoryUnit(r["unit"]) for r in rows}
    aliases = {
        r["alias"]: str(r["item_id"])
        for r in (
            await db.execute(text("select alias, item_id from inventory_item_aliases"))
        ).mappings()
    }
    vocabulary = sorted(r["name"] for r in rows)
    return Mapper(entries, aliases), units, vocabulary


async def extract_count(db: AsyncSession, count_id: uuid.UUID) -> dict[str, Any]:
    """The job body: read the file, transcribe every page, map and parse every
    row, leave the count in review."""
    count = (
        (
            await db.execute(
                text(
                    """
                    select c.id, c.outlet_id, u.storage_path, u.id as upload_id,
                           u.original_filename
                      from stock_counts c join data_uploads u on u.id = c.upload_id
                     where c.id = :id
                    """
                ),
                {"id": count_id},
            )
        )
        .mappings()
        .first()
    )
    if count is None:
        raise ValueError(f"stock count {count_id} does not exist")

    data = await storage.download_object(count["storage_path"], bucket=STOCK_SHEET_BUCKET)
    content_type = "application/pdf" if count["storage_path"].endswith(".pdf") else "image/jpeg"
    pages = _page_images(data, content_type)
    mapper, units, vocabulary = await _load_mapper(db)

    from app.core.config import get_settings

    # Trust is per provider, set by measurement (see sheet_extraction.py):
    # groq shifts rows silently, so everything it says is review-bound. The
    # others are gated by what the PARSER refused plus low extraction
    # confidence — gemini reports a flat confidence, so its gate is
    # effectively the parser, which is the deterministic one anyway.
    force_review = get_settings().STOCK_EXTRACT_PROVIDER == "groq"

    inserted = 0
    needs_review_count = 0
    sheet_date: str | None = None
    counted_label: str | None = None
    model_used = None

    for page_no, image in enumerate(pages, start=1):
        result = await sheet_extraction.extract_page(image, vocabulary=vocabulary)
        model_used = result.model
        sheet_date = sheet_date or result.page.sheet_date
        counted_label = counted_label or result.page.counted_at_label

        for row in result.page.rows:
            match = mapper.match(row.item_name)
            item_id = match.item_id if match else None
            item_unit = units.get(item_id) if item_id else None

            parse_detail: dict[str, Any] = {}
            if match:
                parse_detail["match"] = {"method": match.method, **match.detail}
            qty = requested = None
            refused = False
            if item_unit is not None:
                for field_name, raw in (
                    ("closing", row.closing_count_raw),
                    ("requisition", row.requisition_raw),
                ):
                    outcome = parse_quantity(raw, item_unit)
                    if isinstance(outcome, Parsed):
                        parse_detail[field_name] = outcome.detail
                        if field_name == "closing":
                            qty = outcome.qty
                        else:
                            requested = outcome.qty
                    elif isinstance(outcome, Refused):
                        parse_detail[field_name] = {
                            "refused": outcome.reason,
                            **outcome.detail,
                        }
                        refused = True

            needs_review = (
                force_review
                or match is None
                or match.method == "fuzzy"
                or refused
                or (row.confidence is not None and row.confidence < REVIEW_BELOW)
            )
            needs_review_count += 1 if needs_review else 0

            await db.execute(
                text(
                    """
                    insert into stock_count_lines
                        (count_id, page, sl_no, raw_name, raw_closing,
                         raw_requisition, extract_confidence, item_id,
                         match_method, qty, requested_qty, parse_detail,
                         needs_review)
                    values (:c, :page, :sl, :name, :closing, :req, :conf,
                            :item, :method, :qty, :requested,
                            cast(:detail as jsonb), :review)
                    """
                ),
                {
                    "c": count_id,
                    "page": page_no,
                    "sl": row.sl_no,
                    "name": row.item_name,
                    "closing": row.closing_count_raw,
                    "req": row.requisition_raw,
                    "conf": row.confidence,
                    "item": item_id,
                    "method": match.method if match else None,
                    "qty": qty,
                    "requested": requested,
                    "detail": json.dumps(parse_detail),
                    "review": needs_review,
                },
            )
            inserted += 1

    parsed_date: date | None = None
    if sheet_date:
        try:
            parsed_date = date.fromisoformat(sheet_date)
        except ValueError:
            parsed_date = None

    await db.execute(
        text(
            """
            update stock_counts
               set status = 'review',
                   extractor = :extractor,
                   page_count = :pages,
                   counted_at_label = :label,
                   business_date = coalesce(:sheet_date, business_date)
             where id = :id
            """
        ),
        {
            "id": count_id,
            "extractor": f"{model_used} · {sheet_extraction.EXTRACTOR_VERSION}",
            "pages": len(pages),
            "label": counted_label,
            "sheet_date": parsed_date,
        },
    )
    await db.execute(
        text("update data_uploads set status = 'parsed', parsed_at = now() where id = :u"),
        {"u": count["upload_id"]},
    )
    await db.commit()
    return {
        "count_id": str(count_id),
        "pages": len(pages),
        "lines": inserted,
        "needs_review": needs_review_count,
        "extractor": model_used,
    }


async def background_extract(count_id: uuid.UUID, *, outlet_id: uuid.UUID) -> None:
    """What the upload endpoint hands to a BackgroundTask."""

    async def body(db: AsyncSession) -> dict[str, Any]:
        try:
            return await extract_count(db, count_id)
        except Exception:
            await db.rollback()
            await db.execute(
                text("update stock_counts set status = 'failed' where id = :id"),
                {"id": count_id},
            )
            await db.commit()
            raise

    await run_job("stock_extract", body, outlet_id=outlet_id)


async def re_extract(db: AsyncSession, user: CurrentUser, count_id: uuid.UUID) -> dict[str, Any]:
    """The recovery lever, mirroring the sales reparse: the file is retained,
    so a failed or superseded extraction re-runs without asking anyone to
    photograph the sheet again. Confirmed counts are immutable — corrections
    to a signed count are a new count."""
    count = (
        (
            await db.execute(
                text("select id, outlet_id, status from stock_counts where id = :id"),
                {"id": count_id},
            )
        )
        .mappings()
        .first()
    )
    if count is None:
        raise NotFoundError("That count does not exist.")
    await _require_outlet_access(user, count["outlet_id"])
    if count["status"] == "confirmed":
        raise ConflictError("A confirmed count is signed off; upload a new sheet instead.")

    await db.execute(text("delete from stock_count_lines where count_id = :id"), {"id": count_id})
    await db.execute(
        text("update stock_counts set status = 'extracting' where id = :id"),
        {"id": count_id},
    )
    await db.commit()
    return {"count_id": str(count_id), "status": "extracting"}


# ---------------------------------------------------------------------------
# Review and confirm
# ---------------------------------------------------------------------------


async def review_line(
    db: AsyncSession,
    user: CurrentUser,
    *,
    count_id: uuid.UUID,
    line_id: uuid.UUID,
    item_id: uuid.UUID | None,
    qty: float | None,
    requested_qty: float | None,
    remember_alias: bool,
) -> dict[str, Any]:
    """A human resolves what the machine refused — and the correction is
    remembered when they say so."""
    line = (
        (
            await db.execute(
                text(
                    """
                    select l.id, l.raw_name, l.item_id, c.outlet_id, c.status
                      from stock_count_lines l
                      join stock_counts c on c.id = l.count_id
                     where l.id = :line and l.count_id = :count
                    """
                ),
                {"line": line_id, "count": count_id},
            )
        )
        .mappings()
        .first()
    )
    if line is None:
        raise NotFoundError("That line does not exist on this count.")
    await _require_outlet_access(user, line["outlet_id"])
    if line["status"] == "confirmed":
        raise ConflictError("This count is confirmed; corrections need a new count.")

    await db.execute(
        text(
            """
            update stock_count_lines
               set item_id = coalesce(:item, item_id),
                   match_method = case when :item is not null then 'human' else match_method end,
                   qty = :qty,
                   requested_qty = :requested,
                   needs_review = false,
                   reviewed_by = :by, reviewed_at = now()
             where id = :id
            """
        ),
        {
            "id": line_id,
            "item": item_id,
            "qty": qty,
            "requested": requested_qty,
            "by": user.profile_id,
        },
    )

    if remember_alias and item_id is not None:
        alias = normalise(line["raw_name"])
        if alias:
            await db.execute(
                text(
                    """
                    insert into inventory_item_aliases (item_id, alias, created_by)
                    values (:item, :alias, :by)
                    on conflict ((lower(alias))) do nothing
                    """
                ),
                {"item": item_id, "alias": alias, "by": user.profile_id},
            )
    await db.commit()
    return {"line_id": str(line_id), "reviewed": True}


async def confirm_count(db: AsyncSession, user: CurrentUser, count_id: uuid.UUID) -> dict[str, Any]:
    """Sign-off. Refused while any line still awaits review: a count that is
    half-read is not the outlet's truth yet."""
    count = (
        (
            await db.execute(
                text("select id, outlet_id, status from stock_counts where id = :id"),
                {"id": count_id},
            )
        )
        .mappings()
        .first()
    )
    if count is None:
        raise NotFoundError("That count does not exist.")
    await _require_outlet_access(user, count["outlet_id"])
    if count["status"] == "confirmed":
        raise ConflictError("This count is already confirmed.")
    if count["status"] != "review":
        raise ConflictError("This count is not ready to confirm yet.")

    open_lines = (
        await db.execute(
            text("select count(*) from stock_count_lines where count_id = :id and needs_review"),
            {"id": count_id},
        )
    ).scalar_one()
    if open_lines:
        raise ValidationError(
            f"{open_lines} line(s) still need review. Resolve them first — "
            "a half-read count must not become the outlet's on-hand truth.",
            extra={"needs_review": open_lines},
        )

    await db.execute(
        text(
            """
            update stock_counts
               set status = 'confirmed', confirmed_by = :by, confirmed_at = now()
             where id = :id
            """
        ),
        {"id": count_id, "by": user.profile_id},
    )
    await db.commit()
    return {"count_id": str(count_id), "status": "confirmed"}
