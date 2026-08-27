"""Advisory AI photo review (docs/DECISIONS.md D6).

**It never blocks a submission and it never approves a run.** It writes an
opinion into `run_item_ai_reviews` — verdict, confidence, rationale, model,
prompt version — and, in one narrow case, adds the advisory `ai_mismatch` flag.
A manager still decides.

Three decisions this module encodes:

**The threshold is applied at read time, not at write time.** The model's own
verdict and confidence are stored raw. `ai_review.uncertain_below_confidence`
turns a low-confidence verdict into `uncertain` when the flag is decided and
when the review screen renders it. Storing the downgraded verdict would make
the record unreadable against a threshold that has since moved — the same
reason `app_settings` is effective-dated rather than overwritten.

**`ai_mismatch` fires in exactly one direction.** Staff recorded a pass and the
reviewer is confident it is a fail. The reverse — staff recorded a fail and the
model thinks it looks fine — is staff being harder on themselves than the
machine, and flagging that would be perverse. Agreement in either direction
flags nothing.

**A missing reference photo degrades rather than blocks.** D6 says the AI
compares against that outlet's own standard, and it should. But requiring one
would mean nothing works until every station at every outlet has been shot
under service lighting, which is precisely the prerequisite that went unbuilt
for three epics. Without a reference the model judges on the item's own
instruction and is told to lean towards `uncertain`; the review records that
it had no standard to compare against.
"""

import logging
import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.enums import IntegrityFlag
from app.core.settings_value import resolve_bool, resolve_float
from app.domains.sop import integrity
from app.integrations import storage, vision

logger = logging.getLogger(__name__)


def effective_verdict(verdict: str, confidence: float | None, threshold: float) -> str:
    """What the manager should be shown.

    A `fail` the model is only 40% sure of is not a fail; it is the model
    saying it cannot tell, in more assertive words than it should have used.
    """
    if verdict == "uncertain":
        return "uncertain"
    if confidence is None or confidence < threshold:
        return "uncertain"
    return verdict


def should_flag(recorded_result: str, verdict: str) -> bool:
    """Only a confident contradiction of a recorded pass."""
    return recorded_result == "pass" and verdict == "fail"


_ITEM_SQL = text(
    """
    select ri.id, ri.run_id, ri.template_item_id, ri.photo_path, ri.result,
           r.outlet_id, r.business_date,
           v.title, v.instruction, v.requires_photo
      from checklist_run_items ri
      join checklist_runs r on r.id = ri.run_id
      join checklist_template_item_versions v on v.id = ri.template_item_version_id
     where ri.id = :id
    """
)


async def review_photo(
    db: AsyncSession, run_item_id: uuid.UUID, *, force: bool = False
) -> dict[str, Any]:
    """Review one submitted photo. Returns what happened, never raises for an
    ordinary "not applicable" — the caller is a background task and the
    distinction between "off" and "broken" belongs in job_runs.detail."""
    item = (await db.execute(_ITEM_SQL, {"id": run_item_id})).mappings().first()
    if item is None:
        raise ValueError(f"run item {run_item_id} does not exist")
    if not item["photo_path"]:
        return {"skipped": "no_photo"}

    outlet_id = item["outlet_id"]
    if not force and not await resolve_bool(db, "ai_review.enabled", outlet_id=outlet_id):
        # Off means off: no key read, no bytes fetched, no call made.
        return {"skipped": "disabled"}

    reference = await _active_reference(db, outlet_id, item["template_item_id"])
    submitted_bytes = await storage.download_object(item["photo_path"])
    reference_bytes = await storage.download_object(reference["photo_path"]) if reference else None

    try:
        result = await vision.review(
            submitted=submitted_bytes,
            reference=reference_bytes,
            title=item["title"],
            instruction=item["instruction"],
            recorded_result=item["result"],
        )
    except vision.VisionUnavailable as exc:
        # Recorded as a skip with a reason rather than a failed job. The floor
        # did nothing wrong; the configuration did.
        logger.warning("AI review unavailable for %s: %s", run_item_id, exc)
        return {"skipped": "unavailable", "reason": str(exc)}

    await db.execute(
        text(
            """
            insert into run_item_ai_reviews
                (run_item_id, reference_photo_id, verdict, confidence, rationale,
                 model, prompt_version, latency_ms)
            values (:run_item_id, :reference_photo_id, cast(:verdict as ai_verdict),
                    :confidence, :rationale, :model, :prompt_version, :latency_ms)
            on conflict (run_item_id, model, prompt_version) do update
               set verdict = excluded.verdict,
                   confidence = excluded.confidence,
                   rationale = excluded.rationale,
                   reference_photo_id = excluded.reference_photo_id,
                   latency_ms = excluded.latency_ms,
                   reviewed_at = now()
            """
        ),
        {
            "run_item_id": run_item_id,
            "reference_photo_id": reference["id"] if reference else None,
            "verdict": result.verdict,
            "confidence": result.confidence,
            "rationale": result.rationale,
            "model": result.model,
            "prompt_version": result.prompt_version,
            "latency_ms": result.latency_ms,
        },
    )

    threshold = await resolve_float(db, "ai_review.uncertain_below_confidence", outlet_id=outlet_id)
    shown = effective_verdict(result.verdict, result.confidence, threshold)
    flag = should_flag(item["result"], shown)

    await integrity.merge_flags(
        db,
        table="checklist_run_items",
        row_id=run_item_id,
        owned=integrity.AI_FLAGS,
        present=(
            {
                IntegrityFlag.AI_MISMATCH.value: {
                    "verdict": result.verdict,
                    "shown_as": shown,
                    "confidence": round(result.confidence, 2),
                    "threshold": threshold,
                    "rationale": result.rationale,
                    "model": result.model,
                    "compared_to_reference": result.compared_to_reference,
                }
            }
            if flag
            else {}
        ),
    )
    await integrity.recount_run_flags(db, item["run_id"])
    await db.commit()

    return {
        "run_item_id": str(run_item_id),
        "verdict": result.verdict,
        "shown_as": shown,
        "confidence": round(result.confidence, 2),
        "flagged": flag,
        "model": result.model,
        "latency_ms": result.latency_ms,
        "compared_to_reference": result.compared_to_reference,
    }


async def _active_reference(
    db: AsyncSession, outlet_id: uuid.UUID, template_item_id: uuid.UUID
) -> dict[str, Any] | None:
    """This outlet's own standard, falling back to the template's generic one.

    Per-outlet first, deliberately: the New Town clean prep station is not
    another outlet's clean prep station, and holding every outlet to one
    photograph is how a compliance system starts producing failures that are
    really just differences in the room.
    """
    row = (
        (
            await db.execute(
                text(
                    """
                    select id, photo_path
                      from outlet_item_reference_photos
                     where outlet_id = :outlet_id
                       and template_item_id = :template_item_id
                       and is_active and deleted_at is null
                     limit 1
                    """
                ),
                {"outlet_id": outlet_id, "template_item_id": template_item_id},
            )
        )
        .mappings()
        .first()
    )
    if row is not None:
        return dict(row)

    generic = (
        await db.execute(
            text("select reference_photo_path from checklist_template_items where id = :id"),
            {"id": template_item_id},
        )
    ).scalar()
    if generic:
        # No row id: this is the template's fallback, not an outlet standard,
        # and run_item_ai_reviews.reference_photo_id must stay null so the
        # record does not claim an outlet standard that does not exist.
        return {"id": None, "photo_path": generic}
    return None


# ---------------------------------------------------------------------------
# Background entry points
# ---------------------------------------------------------------------------


async def review_photo_if_enabled(
    db: AsyncSession, run_item_id: uuid.UUID
) -> dict[str, Any] | None:
    """Called from the photo integrity pass. Returns None when nothing was
    done, so the job detail stays quiet rather than carrying a "skipped" line
    for every photo at every outlet where review is off."""
    result = await review_photo(db, run_item_id)
    return None if result.get("skipped") == "disabled" else result


async def review_run_if_enabled(db: AsyncSession, run_id: uuid.UUID) -> list[dict[str, Any]]:
    """Called from the submit pass: review any photographed item that has no
    verdict yet."""
    if not await _enabled_for_run(db, run_id):
        return []
    pending = [
        r[0]
        for r in await db.execute(
            text(
                """
                select ri.id
                  from checklist_run_items ri
                 where ri.run_id = :run_id
                   and ri.photo_path is not null
                   and not exists (
                       select 1 from run_item_ai_reviews a where a.run_item_id = ri.id
                   )
                """
            ),
            {"run_id": run_id},
        )
    ]
    results = []
    for item_id in pending:
        results.append(await review_photo(db, uuid.UUID(str(item_id))))
    return results


async def _enabled_for_run(db: AsyncSession, run_id: uuid.UUID) -> bool:
    outlet_id = (
        await db.execute(
            text("select outlet_id from checklist_runs where id = :id"), {"id": run_id}
        )
    ).scalar()
    if outlet_id is None:
        return False
    return await resolve_bool(db, "ai_review.enabled", outlet_id=uuid.UUID(str(outlet_id)))


# ---------------------------------------------------------------------------
# Reading verdicts back
# ---------------------------------------------------------------------------


async def latest_for_run(
    db: AsyncSession, run_id: uuid.UUID, *, outlet_id: uuid.UUID | None = None
) -> dict[uuid.UUID, dict[str, Any]]:
    """The newest verdict per item, with the threshold already applied.

    Keyed by run item so the review endpoint can attach one to each row.

    Pass `outlet_id` when you have it. The confidence threshold is
    outlet-overridable, so this needs to know which outlet — and looking it up
    from the run is a round trip the review endpoint had already spent.
    """
    if outlet_id is None:
        raw = (
            await db.execute(
                text("select outlet_id from checklist_runs where id = :id"), {"id": run_id}
            )
        ).scalar()
        outlet_id = uuid.UUID(str(raw)) if raw else None
    threshold = await resolve_float(db, "ai_review.uncertain_below_confidence", outlet_id=outlet_id)
    rows = (
        (
            await db.execute(
                text(
                    """
                    select distinct on (a.run_item_id)
                           a.run_item_id, a.verdict,
                           cast(a.confidence as float8) as confidence,
                           a.rationale, a.model, a.prompt_version, a.reviewed_at,
                           a.reference_photo_id is not null as compared_to_reference
                      from run_item_ai_reviews a
                      join checklist_run_items ri on ri.id = a.run_item_id
                     where ri.run_id = :run_id
                     order by a.run_item_id, a.reviewed_at desc
                    """
                ),
                {"run_id": run_id},
            )
        )
        .mappings()
        .all()
    )
    return {
        r["run_item_id"]: {
            **dict(r),
            "shown_as": effective_verdict(r["verdict"], r["confidence"], threshold),
            "uncertain_below": threshold,
        }
        for r in rows
    }
