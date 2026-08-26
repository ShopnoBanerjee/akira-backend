"""The integrity engine.

A photo-based SOP system without integrity checks becomes a photo-reuse system
within three weeks (spec 4.2). Six checks ship:

    duplicate_photo   a perceptual hash matching a recent photo of the same
                      item at the same outlet
    burst_upload      a run whose photos all landed in the last few minutes
                      before submission, or that was finished implausibly fast
    out_of_geofence   submitted further from the outlet than its radius
    late              submitted after due time plus grace
    stale_capture     a photo taken outside the window the run was open —
                      picked from the gallery rather than taken now
    too_dark          mean luminance below the threshold. Deterministic, not an
                      AI judgement (D6)

**Nothing here blocks a submission.** Flags surface on the manager's review
screen and count against the outlet's integrity score. Blocking creates
workarounds; visibility creates accountability. That is a spec principle, not a
preference, and it holds even when a flag looks damning.

Two things every check obeys:

- **A flag carries its evidence.** integrity_detail says what the hash matched,
  how far apart they were, what the luminance measured. A red chip a manager
  cannot check is one they will learn to ignore.
- **Absence of a signal is not a flag.** A device that withheld its location
  produces geo_ok = null and no flag, because refusing location is usually a
  permission a staff member cannot change. Punishing it would teach everyone to
  turn location off.

Item-level flags belong to a photo; run-level flags belong to a submission. The
split is real and 0013 gives each its own column.
"""

import io
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from PIL import Image

from app.core.enums import IntegrityFlag
from app.core.settings_value import resolve_many
from app.integrations import storage

logger = logging.getLogger(__name__)

#: Flags the deterministic photo pass owns. It recomputes all of them for a
#: photo, so it also clears any that no longer apply — a re-shot photo must not
#: keep yesterday's duplicate flag.
PHOTO_FLAGS: frozenset[str] = frozenset(
    {
        IntegrityFlag.DUPLICATE_PHOTO.value,
        IntegrityFlag.STALE_CAPTURE.value,
        IntegrityFlag.TOO_DARK.value,
    }
)

#: Written by the AI reviewer, which is a separate pass with its own history.
AI_FLAGS: frozenset[str] = frozenset({IntegrityFlag.AI_MISMATCH.value})

#: Properties of the submission rather than of any one photo.
RUN_FLAGS: frozenset[str] = frozenset(
    {
        IntegrityFlag.LATE.value,
        IntegrityFlag.OUT_OF_GEOFENCE.value,
        IntegrityFlag.BURST_UPLOAD.value,
    }
)

_THRESHOLD_KEYS = [
    "integrity.phash_max_distance",
    "integrity.phash_lookback_days",
    "integrity.burst_window_minutes",
    "integrity.burst_share",
    "ai_review.min_luminance",
]

#: A ten-item checklist genuinely walked takes minutes. Under this, nobody
#: looked at anything. Kept in code rather than settings: it is a floor on
#: physical plausibility, not a policy dial.
FAST_RUN_ITEM_THRESHOLD = 10
FAST_RUN_SECONDS = 90


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def hamming(a: str, b: str) -> int:
    """Bit distance between two hex-encoded perceptual hashes.

    Raises on a length mismatch rather than comparing hashes of different
    sizes, which would silently produce a distance with no meaning.
    """
    if len(a) != len(b):
        raise ValueError(f"hash lengths differ: {len(a)} vs {len(b)}")
    return bin(int(a, 16) ^ int(b, 16)).count("1")


class UndecodableImage(ValueError):
    """The stored object is not an image this can read.

    Raised rather than swallowed. A truncated or corrupt upload proves nothing,
    and letting it pass silently would leave the review screen showing a clean
    bill of health for a file nothing could open. The failure lands in job_runs
    and the item stays unprocessed, which reads as "not checked" — which is the
    truth.
    """


def _open(image_bytes: bytes) -> "Image.Image":
    from PIL import Image, UnidentifiedImageError

    try:
        image = Image.open(io.BytesIO(image_bytes))
        image.load()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise UndecodableImage(f"Not a readable image ({len(image_bytes)} bytes): {exc}") from exc
    return image


def measure(image_bytes: bytes) -> tuple[str, float]:
    """Perceptual hash and mean luminance, decoding once.

    The hash is perceptual, not cryptographic: re-compressing, resizing or
    slightly recolouring the same photo lands within a few bits, while two
    different photos of the same tidy station do not. That gap is the whole
    basis of the duplicate check.

    Luminance uses PIL's "L" conversion, which is ITU-R 601 luma and weights
    green over red over blue the way an eye does. A plain RGB average would
    call a saturated red surface bright.
    """
    import imagehash
    from PIL import ImageStat

    with _open(image_bytes) as image:
        return (
            str(imagehash.phash(image.convert("RGB"))),
            float(ImageStat.Stat(image.convert("L")).mean[0]),
        )


def phash_hex(image_bytes: bytes) -> str:
    return measure(image_bytes)[0]


def mean_luminance(image_bytes: bytes) -> float:
    return measure(image_bytes)[1]


def burst_share(uploaded_at: list[datetime], submitted_at: datetime, window_minutes: int) -> float:
    """Fraction of a run's photos that landed inside the window before submit.

    Zero photos is 0.0, not 1.0. A run with nothing to photograph has not been
    batch-faked; it has nothing to fake.
    """
    if not uploaded_at:
        return 0.0
    window_start = submitted_at - timedelta(minutes=window_minutes)
    inside = sum(1 for ts in uploaded_at if window_start <= ts <= submitted_at)
    return inside / len(uploaded_at)


def is_stale_capture(
    uploaded_at: datetime, started_at: datetime | None, submitted_at: datetime | None
) -> bool:
    """Was the photo taken outside the window this run was open?

    The client asks the camera for a live capture, but a determined phone can
    always hand back a gallery file. The server-side signal is the upload
    landing before the run was started — or, once submitted, after it closed.

    An unstarted run cannot judge this, so it does not.
    """
    if started_at is None:
        return False
    if uploaded_at < started_at:
        return True
    return submitted_at is not None and uploaded_at > submitted_at


def completed_implausibly_fast(
    started_at: datetime | None, submitted_at: datetime | None, item_count: int
) -> bool:
    """A ten-plus item run submitted within ninety seconds of starting."""
    if started_at is None or submitted_at is None:
        return False
    if item_count < FAST_RUN_ITEM_THRESHOLD:
        return False
    return (submitted_at - started_at).total_seconds() < FAST_RUN_SECONDS


# ---------------------------------------------------------------------------
# Writing flags
# ---------------------------------------------------------------------------


async def merge_flags(
    db: AsyncSession,
    *,
    table: str,
    row_id: uuid.UUID,
    owned: frozenset[str],
    present: dict[str, dict[str, Any]],
    extra_set: str = "",
) -> list[str]:
    """Replace this pass's own flags, leave everybody else's alone.

    A pass that recomputes duplicate_photo must be able to *clear* it when the
    photo is re-shot, but must not clear ai_mismatch, which it knows nothing
    about. So each pass declares what it owns and this only touches that.
    """
    import json

    current = (
        (
            await db.execute(
                text(f"select integrity_flags, integrity_detail from {table} where id = :id"),
                {"id": row_id},
            )
        )
        .mappings()
        .first()
    )
    if current is None:
        raise ValueError(f"{table} row {row_id} does not exist")

    existing = set(current["integrity_flags"] or [])
    detail_raw = current["integrity_detail"]
    detail = json.loads(detail_raw) if isinstance(detail_raw, str) else dict(detail_raw or {})

    flags = sorted((existing - owned) | set(present))
    for flag in owned:
        detail.pop(flag, None)
    detail.update(present)

    await db.execute(
        text(
            f"""
            update {table}
               set integrity_flags = cast(:flags as text[]),
                   integrity_detail = cast(:detail as jsonb)
                   {extra_set}
             where id = :id
            """
        ),
        {"id": row_id, "flags": flags, "detail": json.dumps(detail, default=str)},
    )
    return flags


async def recount_run_flags(db: AsyncSession, run_id: uuid.UUID) -> int:
    """integrity_flag_count = run-level flags + every item-level flag.

    One number the review queue and the outlet-score penalty both read, so it
    has to mean the same thing in both.
    """
    total = (
        await db.execute(
            text(
                """
                select coalesce(array_length(r.integrity_flags, 1), 0)
                     + coalesce((
                         select sum(coalesce(array_length(ri.integrity_flags, 1), 0))
                           from checklist_run_items ri where ri.run_id = r.id
                       ), 0)
                  from checklist_runs r
                 where r.id = :id
                """
            ),
            {"id": run_id},
        )
    ).scalar_one()
    await db.execute(
        text("update checklist_runs set integrity_flag_count = :n where id = :id"),
        {"id": run_id, "n": int(total)},
    )
    return int(total)


# ---------------------------------------------------------------------------
# The photo pass — background only
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PhotoResult:
    run_item_id: uuid.UUID
    phash: str
    luminance: float
    flags: list[str]
    detail: dict[str, Any]


_ITEM_SQL = text(
    """
    select ri.id, ri.run_id, ri.template_item_id, ri.photo_path,
           ri.photo_uploaded_at, r.outlet_id, r.business_date,
           r.started_at, r.submitted_at, v.title
      from checklist_run_items ri
      join checklist_runs r on r.id = ri.run_id
      join checklist_template_item_versions v on v.id = ri.template_item_version_id
     where ri.id = :id
    """
)


async def process_photo(db: AsyncSession, run_item_id: uuid.UUID) -> PhotoResult:
    """Hash one photo, measure it, and apply the three photo-level checks.

    Downloads the object and runs Pillow, so it belongs in a background task
    and nowhere else.
    """
    item = (await db.execute(_ITEM_SQL, {"id": run_item_id})).mappings().first()
    if item is None:
        raise ValueError(f"run item {run_item_id} does not exist")
    if not item["photo_path"]:
        raise ValueError(f"run item {run_item_id} has no photo to process")

    thresholds = await resolve_many(db, _THRESHOLD_KEYS, outlet_id=item["outlet_id"])
    image_bytes = await storage.download_object(item["photo_path"])
    photo_hash, luminance = measure(image_bytes)

    present: dict[str, dict[str, Any]] = {}

    # --- too dark ---------------------------------------------------------
    min_luminance = float(thresholds["ai_review.min_luminance"])
    if luminance < min_luminance:
        present[IntegrityFlag.TOO_DARK.value] = {
            "luminance": round(luminance, 1),
            "minimum": min_luminance,
        }

    # --- gallery pick -----------------------------------------------------
    uploaded_at = item["photo_uploaded_at"]
    if uploaded_at is not None and is_stale_capture(
        uploaded_at, item["started_at"], item["submitted_at"]
    ):
        present[IntegrityFlag.STALE_CAPTURE.value] = {
            "photo_uploaded_at": uploaded_at,
            "run_started_at": item["started_at"],
            "run_submitted_at": item["submitted_at"],
        }

    # --- re-used photo ----------------------------------------------------
    match = await _find_duplicate(
        db,
        run_item_id=run_item_id,
        template_item_id=item["template_item_id"],
        outlet_id=item["outlet_id"],
        business_date=item["business_date"],
        photo_hash=photo_hash,
        max_distance=int(thresholds["integrity.phash_max_distance"]),
        lookback_days=int(thresholds["integrity.phash_lookback_days"]),
    )
    if match is not None:
        present[IntegrityFlag.DUPLICATE_PHOTO.value] = match

    await db.execute(
        text(
            """
            update checklist_run_items
               set photo_phash = :phash, photo_luminance = :luminance
             where id = :id
            """
        ),
        {"id": run_item_id, "phash": photo_hash, "luminance": luminance},
    )
    flags = await merge_flags(
        db,
        table="checklist_run_items",
        row_id=run_item_id,
        owned=PHOTO_FLAGS,
        present=present,
        extra_set=", photo_processed_at = now()",
    )
    await recount_run_flags(db, item["run_id"])
    await db.commit()

    return PhotoResult(
        run_item_id=run_item_id,
        phash=photo_hash,
        luminance=luminance,
        flags=[f for f in flags if f in PHOTO_FLAGS],
        detail=present,
    )


async def _find_duplicate(
    db: AsyncSession,
    *,
    run_item_id: uuid.UUID,
    template_item_id: uuid.UUID,
    outlet_id: uuid.UUID,
    business_date: Any,
    photo_hash: str,
    max_distance: int,
    lookback_days: int,
) -> dict[str, Any] | None:
    """Nearest recent photo of the same item at the same outlet, if it is near
    enough to be the same photo.

    Scoped to (outlet, template item) on purpose. The same clean prep station
    photographed at two outlets is two legitimate photos; the same photo posted
    twice for New Town's Monday and Tuesday fridge check is not. Comparing
    across items would flag every plain stainless surface in the kitchen.

    Returns the *closest* match rather than the first found, so the evidence
    shown to a manager is the strongest one available.
    """
    candidates = (
        await db.execute(
            text(
                """
                select ri.id, ri.photo_phash, ri.run_id, r.business_date,
                       ri.photo_uploaded_at, t.name as template_name
                  from checklist_run_items ri
                  join checklist_runs r on r.id = ri.run_id
                  join checklist_templates t on t.id = r.template_id
                 where ri.template_item_id = :template_item_id
                   and r.outlet_id = :outlet_id
                   and ri.photo_phash is not null
                   and ri.id <> :self_id
                   and r.business_date >= :since
                 order by ri.photo_uploaded_at desc
                 limit 500
                """
            ),
            {
                "template_item_id": template_item_id,
                "outlet_id": outlet_id,
                "self_id": run_item_id,
                "since": business_date - timedelta(days=lookback_days),
            },
        )
    ).mappings()

    best: dict[str, Any] | None = None
    for row in candidates:
        try:
            distance = hamming(photo_hash, row["photo_phash"])
        except ValueError:
            # A hash written by an older algorithm. Skip it rather than
            # inventing a distance.
            continue
        if distance > max_distance:
            continue
        if best is None or distance < best["distance"]:
            best = {
                "distance": distance,
                "max_distance": max_distance,
                "matched_run_id": str(row["run_id"]),
                "matched_run_item_id": str(row["id"]),
                "matched_business_date": str(row["business_date"]),
                "matched_template_name": row["template_name"],
            }
        if best["distance"] == 0:
            break
    return best


# ---------------------------------------------------------------------------
# The run pass — at submit, and again after any photo is processed
# ---------------------------------------------------------------------------


async def evaluate_run(db: AsyncSession, run_id: uuid.UUID) -> list[str]:
    """Apply the three run-level checks and refresh the flag count.

    Cheap: no image work, only columns the submit path already wrote. Safe to
    call inside a request, and it is — a manager opening the review queue five
    seconds after a submit must already see `late` and `out_of_geofence`.
    """
    run = (
        (
            await db.execute(
                text(
                    """
                    select r.id, r.status, r.started_at, r.submitted_at, r.due_at,
                           r.is_late, r.minutes_late, r.geo_ok, r.outlet_id,
                           a.grace_minutes,
                           (select count(*) from checklist_run_items ri
                             where ri.run_id = r.id) as item_count
                      from checklist_runs r
                      join checklist_assignments a on a.id = r.assignment_id
                     where r.id = :id
                    """
                ),
                {"id": run_id},
            )
        )
        .mappings()
        .first()
    )
    if run is None:
        raise ValueError(f"run {run_id} does not exist")

    thresholds = await resolve_many(db, _THRESHOLD_KEYS, outlet_id=run["outlet_id"])
    present: dict[str, dict[str, Any]] = {}

    if run["is_late"]:
        present[IntegrityFlag.LATE.value] = {
            "minutes_late": run["minutes_late"],
            "due_at": run["due_at"],
            "grace_minutes": run["grace_minutes"],
        }

    # False, not "not true": null means the device withheld location, which is
    # counted separately and is never a flag.
    if run["geo_ok"] is False:
        present[IntegrityFlag.OUT_OF_GEOFENCE.value] = {"geo_ok": False}

    submitted_at = run["submitted_at"]
    if submitted_at is not None:
        uploads = [
            r[0]
            for r in await db.execute(
                text(
                    "select photo_uploaded_at from checklist_run_items"
                    " where run_id = :id and photo_uploaded_at is not null"
                ),
                {"id": run_id},
            )
        ]
        window = int(thresholds["integrity.burst_window_minutes"])
        share_limit = float(thresholds["integrity.burst_share"])
        share = burst_share(uploads, submitted_at, window)
        too_fast = completed_implausibly_fast(
            run["started_at"], submitted_at, int(run["item_count"])
        )
        if (uploads and share >= share_limit) or too_fast:
            present[IntegrityFlag.BURST_UPLOAD.value] = {
                "photos": len(uploads),
                "share_in_window": round(share, 2),
                "window_minutes": window,
                "share_threshold": share_limit,
                "completed_in_seconds": (
                    int((submitted_at - run["started_at"]).total_seconds())
                    if run["started_at"]
                    else None
                ),
                "implausibly_fast": too_fast,
            }

    flags = await merge_flags(
        db,
        table="checklist_runs",
        row_id=run_id,
        owned=RUN_FLAGS,
        present=present,
    )
    await recount_run_flags(db, run_id)
    return [f for f in flags if f in RUN_FLAGS]


# ---------------------------------------------------------------------------
# Background entry points
# ---------------------------------------------------------------------------


async def background_photo_pass(
    run_item_id: uuid.UUID,
    *,
    outlet_id: uuid.UUID,
    business_date: Any,
) -> None:
    """What photo-confirm hands to a BackgroundTask.

    Wrapped in a job_runs row so a storage timeout or a corrupt JPEG is
    something an admin can read on /app/settings/jobs, rather than a log line
    on a server nobody has open.
    """

    async def body(db: AsyncSession) -> dict[str, Any]:
        result = await process_photo(db, run_item_id)
        run_id = (
            await db.execute(
                text("select run_id from checklist_run_items where id = :id"),
                {"id": run_item_id},
            )
        ).scalar_one()
        run_flags = await evaluate_run(db, uuid.UUID(str(run_id)))
        await db.commit()

        # Advisory, and last: a model call must never be able to lose the
        # deterministic result that has already been committed above.
        from app.domains.sop import ai_review

        ai = await ai_review.review_photo_if_enabled(db, run_item_id)
        return {
            "run_item_id": str(run_item_id),
            "phash": result.phash,
            "luminance": round(result.luminance, 1),
            "item_flags": result.flags,
            "run_flags": run_flags,
            "evidence": result.detail,
            **({"ai_review": ai} if ai else {}),
        }

    from app.jobs.runner import run_job

    await run_job("photo_integrity", body, outlet_id=outlet_id, business_date=business_date)


async def background_run_pass(
    run_id: uuid.UUID,
    *,
    outlet_id: uuid.UUID,
    business_date: Any,
) -> None:
    """What submit hands to a BackgroundTask: catch up any photo that has not
    been hashed yet, then re-evaluate the run.

    Submit already applied the run-level checks inline. This exists because a
    photo confirmed seconds before submission may still be mid-flight, and a
    duplicate that only surfaces after the manager has approved is a duplicate
    that surfaced too late.
    """

    async def body(db: AsyncSession) -> dict[str, Any]:
        pending = [
            r[0]
            for r in await db.execute(
                text(
                    """
                    select id from checklist_run_items
                     where run_id = :run_id
                       and photo_path is not null
                       and photo_processed_at is null
                    """
                ),
                {"run_id": run_id},
            )
        ]
        processed: list[dict[str, Any]] = []
        for item_id in pending:
            result = await process_photo(db, uuid.UUID(str(item_id)))
            processed.append({"run_item_id": str(item_id), "flags": result.flags})
        run_flags = await evaluate_run(db, run_id)
        await db.commit()

        from app.domains.sop import ai_review

        reviews = await ai_review.review_run_if_enabled(db, run_id)
        return {
            "run_id": str(run_id),
            "photos_caught_up": processed,
            "run_flags": run_flags,
            **({"ai_reviews": reviews} if reviews else {}),
        }

    from app.jobs.runner import run_job

    await run_job("run_integrity", body, outlet_id=outlet_id, business_date=business_date)
