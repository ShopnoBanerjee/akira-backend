"""Replace the hand-made test dates with eight weeks of coherent history.

    uv run python scripts/seed_history.py

Why this exists: P5-P7 testing materialised business dates by hand (2026-08-25,
-26, -28), which left a history that reads worse than the outlets deserve —
08-25 is almost entirely `missed` because nothing was ever going to be done on
a day invented after the fact, and six of 08-26's photos are 262-byte stubs
that can never decode. OPEN_ITEMS said replace it wholesale, and this does.

What it deliberately KEEPS: business date 2026-08-27 — the real runs, the real
photos, and the two genuine Groq verdicts on them. Synthetic history should
surround the real artifacts, not bury them.

How it stays honest:

- Runs are created by the REAL `materialise_runs`, so cadence, day parts and
  due times come from the same code path production uses — the seed cannot
  drift from the scheduler because it *is* the scheduler.
- Scores come from the REAL `scoring.run_score` over item results the seed
  actually writes. Nothing invents a percentage.
- Photos are real decodable JPEGs generated per item, uploaded to Storage, and
  then processed by the REAL `process_photo` — hashes, luminance and flags all
  computed by production code. Two of them are planted to be caught: a
  duplicate pair and a too-dark shot, both at DEV02, so the integrity screens
  have something true to show.
- Approvals respect separation of duties; the database CHECK would refuse
  anything else.
- No AI verdicts are fabricated. A synthetic `run_item_ai_reviews` row would
  be a lie with a model name on it. History shows "not reviewed", which is
  true.
- `created_at` is NOT backdated. The client never derives a date from
  created_at (the business-date rule), so honest row-creation timestamps cost
  nothing and quietly prove the rule holds.

The two outlets tell different stories on purpose: New Town runs a healthy
ship (occasional lateness, rare misses); Dev Outlet 2 is mediocre with a
visibly bad week in late July and carries stale open criticals — so the
dashboard, the digest and the health penalties all have something real to
disagree about.

Deterministic: every random draw is seeded from (outlet, date), so re-running
produces the same story. Materialisation is idempotent (on conflict do
nothing) and already-answered days are left alone, so a re-run after a partial
failure completes the missing days rather than doubling anything.
"""

import asyncio
import io
import random
import sys
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import scoring
from app.core.db import get_session_factory
from app.core.enums import ItemResult, SalesChannel
from app.domains.sales import petpooja
from app.domains.sales import service as sales_service
from app.domains.sop import integrity
from app.domains.sop.runs_service import materialise_runs, photo_path_for
from app.integrations import storage

IST = ZoneInfo("Asia/Kolkata")

FIRST_DAY = date(2026, 7, 1)
LAST_DAY = date(2026, 8, 26)  # 08-27 is today's real, untouched activity
PHOTO_DAYS_FROM = date(2026, 8, 17)  # uploads only for the recent stretch
WIPE_DATES = [date(2026, 8, 25), date(2026, 8, 26), date(2026, 8, 28)]
REVIEW_LAG_FROM = date(2026, 8, 24)  # newer submissions may still await review


@dataclass(frozen=True)
class OutletStory:
    code: str
    fail_p: float  # a non-critical item failing
    crit_fail_p: float  # a critical item failing
    na_p: float  # an allow_na item answered n/a
    miss_p: float  # the whole run never happening
    late_p: float  # submitted past due + grace
    bad_week: tuple[date, date] | None  # everything gets worse here


STORIES = {
    "AKR-NT01": OutletStory(
        code="AKR-NT01",
        fail_p=0.02,
        crit_fail_p=0.01,
        na_p=0.03,
        miss_p=0.03,
        late_p=0.07,
        bad_week=None,
    ),
    "AKR-DEV02": OutletStory(
        code="AKR-DEV02",
        fail_p=0.07,
        crit_fail_p=0.04,
        na_p=0.05,
        miss_p=0.13,
        late_p=0.20,
        bad_week=(date(2026, 7, 20), date(2026, 7, 26)),
    ),
}

FAIL_NOTES = [
    "Not up to standard, redoing before service",
    "Ran out of time before opening, flagged to manager",
    "Equipment issue, maintenance informed",
    "Stock short, adjusted prep plan",
    "Cleaning incomplete at handover",
]


def worse(story: OutletStory, day: date) -> OutletStory:
    if story.bad_week and story.bad_week[0] <= day <= story.bad_week[1]:
        return OutletStory(
            code=story.code,
            fail_p=min(0.25, story.fail_p * 2.5),
            crit_fail_p=min(0.12, story.crit_fail_p * 2.5),
            na_p=story.na_p,
            miss_p=min(0.4, story.miss_p * 2.6),
            late_p=min(0.45, story.late_p * 2.2),
            bad_week=story.bad_week,
        )
    return story


# ---------------------------------------------------------------------------
# People
# ---------------------------------------------------------------------------


async def load_people(db: AsyncSession) -> dict[str, dict[str, list[uuid.UUID]]]:
    """Submitters and approvers per outlet, looked up by seed email."""
    rows = (
        (
            await db.execute(
                text(
                    """
                    select u.email, p.id
                      from profiles p join auth.users u on u.id = p.id
                     where u.email like '%@akira.test' and p.is_active
                    """
                )
            )
        )
        .mappings()
        .all()
    )
    by_email = {r["email"]: r["id"] for r in rows}

    def need(email: str) -> uuid.UUID:
        if email not in by_email:
            raise SystemExit(f"seed user {email} is missing — run scripts/seed_users.py first")
        return by_email[email]

    return {
        "AKR-NT01": {
            "submitters": [
                need("lead.nt@akira.test"),
                need("lead2.nt@akira.test"),
                need("staff.nt@akira.test"),
                need("staff2.nt@akira.test"),
            ],
            "approvers": [need("manager.nt@akira.test"), need("ops@akira.test")],
        },
        "AKR-DEV02": {
            "submitters": [need("staff.dev@akira.test")],
            "approvers": [need("manager.dev@akira.test"), need("ops@akira.test")],
        },
    }


# ---------------------------------------------------------------------------
# Wipe
# ---------------------------------------------------------------------------


async def wipe(db: AsyncSession) -> dict[str, int]:
    """The invented dates go wholesale — runs, their items, their AI rows,
    their exceptions, and their Storage objects. 08-27 is not touched."""
    paths = [
        r[0]
        for r in await db.execute(
            text(
                "select ri.photo_path from checklist_run_items ri"
                " join checklist_runs r on r.id = ri.run_id"
                " where r.business_date = any(:d) and ri.photo_path is not null"
            ),
            {"d": WIPE_DATES},
        )
    ]
    exceptions = (
        await db.execute(
            text("delete from sop_exceptions where business_date = any(:d) returning id"),
            {"d": WIPE_DATES},
        )
    ).rowcount
    runs = (
        await db.execute(
            text("delete from checklist_runs where business_date = any(:d) returning id"),
            {"d": WIPE_DATES},
        )
    ).rowcount
    await db.commit()

    # Best-effort: an orphaned object costs pennies, a failed wipe costs the
    # reseed. Storage deletion never blocks the rebuild.
    removed = 0
    if paths:
        try:
            client = storage._client()
            response = await client.request(
                "DELETE",
                f"/object/{storage.SOP_PHOTO_BUCKET}",
                json={"prefixes": paths},
            )
            removed = len(paths) if response.status_code < 400 else 0
        except Exception:
            pass
    return {"runs": runs, "exceptions": exceptions, "storage_objects": removed}


# ---------------------------------------------------------------------------
# One day of history
# ---------------------------------------------------------------------------


async def day_runs(db: AsyncSession, day: date) -> list[dict]:
    rows = (
        (
            await db.execute(
                text(
                    """
                    select r.id, r.outlet_id, r.due_at, o.code, a.grace_minutes
                      from checklist_runs r
                      join outlets o on o.id = r.outlet_id
                      join checklist_assignments a on a.id = r.assignment_id
                     where r.business_date = :d and r.status = 'pending'
                    """
                ),
                {"d": day},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def run_items(db: AsyncSession, run_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[dict]]:
    rows = (
        (
            await db.execute(
                text(
                    """
                    select ri.id, ri.run_id, ri.template_item_id,
                           v.is_critical, v.allow_na,
                           v.requires_value, v.requires_photo,
                           cast(v.value_min as float8) as value_min,
                           cast(v.value_max as float8) as value_max
                      from checklist_run_items ri
                      join checklist_template_item_versions v
                        on v.id = ri.template_item_version_id
                     where ri.run_id = any(:ids)
                     order by ri.run_id, ri.sort_order
                    """
                ),
                {"ids": run_ids},
            )
        )
        .mappings()
        .all()
    )
    grouped: dict[uuid.UUID, list[dict]] = {}
    for r in rows:
        grouped.setdefault(r["run_id"], []).append(dict(r))
    return grouped


def decide_item(rng: random.Random, item: dict, story: OutletStory) -> dict:
    """One item's answer: result, value, note."""
    if item["allow_na"] and rng.random() < story.na_p:
        return {"result": "na", "value": None, "note": None, "out_of_range": False}
    fail_p = story.crit_fail_p if item["is_critical"] else story.fail_p
    failed = rng.random() < fail_p
    value = None
    out_of_range = False
    if item["requires_value"]:
        lo = item["value_min"] if item["value_min"] is not None else 1.0
        hi = item["value_max"] if item["value_max"] is not None else lo + 10.0
        if failed and rng.random() < 0.6:
            # A failed measured item usually failed BECAUSE the number was off.
            value = round(hi + rng.uniform(0.5, 5.0), 1)
            out_of_range = True
        else:
            value = round(rng.uniform(lo, hi), 1)
    return {
        "result": "fail" if failed else "pass",
        "value": value,
        "note": rng.choice(FAIL_NOTES) if failed else None,
        "out_of_range": out_of_range,
    }


async def write_day(
    db: AsyncSession,
    day: date,
    people: dict,
    stats: dict[str, int],
) -> None:
    """Answer, submit, approve (or miss) every pending run of one day.

    Set-based: one unnest update for the items, one for the runs, one insert
    for the exceptions.
    """
    runs = await day_runs(db, day)
    if not runs:
        return
    items_by_run = await run_items(db, [r["id"] for r in runs])

    item_updates: list[dict] = []
    run_updates: list[dict] = []
    exceptions: list[dict] = []

    for run in runs:
        story = worse(STORIES[run["code"]], day)
        rng = random.Random(f"{run['code']}:{day}:{run['id']}")
        cast = people[run["code"]]

        if rng.random() < story.miss_p:
            run_updates.append(
                {
                    "id": run["id"],
                    "status": "missed",
                    "submitted_by": None,
                    "submitted_at": None,
                    "approved_by": None,
                    "approved_at": None,
                    "score": None,
                    "critical_fails": 0,
                    "is_late": False,
                    "minutes_late": None,
                    "started_at": None,
                    "started_by": None,
                }
            )
            exceptions.append(
                {
                    "run_item_id": None,
                    "outlet_id": run["outlet_id"],
                    "day": day,
                    "severity": "medium",
                    "title": "Missed: checklist never started",
                    "detail": "Raised by the overdue sweep. Never started.",
                    "rng": rng.random(),
                    "approver": cast["approvers"][0],
                }
            )
            stats["missed"] += 1
            continue

        items = items_by_run.get(run["id"], [])
        decisions = [(item, decide_item(rng, item, story)) for item in items]
        scorable = [
            scoring.ScorableItem(result=ItemResult(d["result"]), is_critical=item["is_critical"])
            for item, d in decisions
        ]
        score = scoring.run_score(scorable)
        critical_fails = scoring.critical_fail_count(scorable)

        due = run["due_at"]
        grace = run["grace_minutes"] or 0
        late = rng.random() < story.late_p
        if late:
            minutes_late = rng.randint(10, 150)
            submitted_at = due + timedelta(minutes=grace + minutes_late)
        else:
            minutes_late = None
            submitted_at = due - timedelta(minutes=rng.randint(2, 40))
        started_at = submitted_at - timedelta(minutes=rng.randint(12, 45))

        submitter = rng.choice(cast["submitters"])
        # Reviewed unless it is recent enough that a queue backlog is normal.
        reviewed = day < REVIEW_LAG_FROM or rng.random() < 0.5
        approver = rng.choice([a for a in cast["approvers"] if a != submitter])
        approved_at = submitted_at + timedelta(hours=rng.uniform(1, 20)) if reviewed else None

        for item, d in decisions:
            item_updates.append(
                {
                    "id": item["id"],
                    "result": d["result"],
                    "value": d["value"],
                    "note": d["note"],
                    "out_of_range": d["out_of_range"],
                }
            )
            if item["is_critical"] and d["result"] == "fail":
                exceptions.append(
                    {
                        "run_item_id": item["id"],
                        "outlet_id": run["outlet_id"],
                        "day": day,
                        "severity": "high",
                        "title": "Critical fail: item failed on the floor",
                        "detail": d["note"],
                        "rng": rng.random(),
                        "approver": approver,
                    }
                )
                stats["critical_fails"] += 1

        run_updates.append(
            {
                "id": run["id"],
                "status": "approved" if reviewed else "submitted",
                "submitted_by": submitter,
                "submitted_at": submitted_at,
                "approved_by": approver if reviewed else None,
                "approved_at": approved_at,
                "score": score,
                "critical_fails": critical_fails,
                "is_late": late,
                "minutes_late": minutes_late,
                "started_at": started_at,
                "started_by": submitter,
            }
        )
        stats["approved" if reviewed else "submitted"] += 1
        if late:
            stats["late"] += 1

    if item_updates:
        await db.execute(
            text(
                """
                update checklist_run_items ri
                   set result = cast(u.result as item_result),
                       value_numeric = u.value,
                       note = u.note,
                       out_of_range = u.out_of_range
                  from unnest(
                           cast(:ids as uuid[]), cast(:results as text[]),
                           cast(:values as numeric[]), cast(:notes as text[]),
                           cast(:oor as boolean[])
                       ) as u(id, result, value, note, out_of_range)
                 where ri.id = u.id
                """
            ),
            {
                "ids": [u["id"] for u in item_updates],
                "results": [u["result"] for u in item_updates],
                "values": [u["value"] for u in item_updates],
                "notes": [u["note"] for u in item_updates],
                "oor": [u["out_of_range"] for u in item_updates],
            },
        )
    await db.execute(
        text(
            """
            update checklist_runs r
               set status = cast(u.status as run_status),
                   started_at = u.started_at, started_by = u.started_by,
                   submitted_at = u.submitted_at, submitted_by = u.submitted_by,
                   approved_at = u.approved_at, approved_by = u.approved_by,
                   score_pct = u.score, critical_fail_count = u.critical_fails,
                   is_late = u.is_late, minutes_late = u.minutes_late
              from unnest(
                       cast(:ids as uuid[]), cast(:statuses as text[]),
                       cast(:started_ats as timestamptz[]), cast(:started_bys as uuid[]),
                       cast(:submitted_ats as timestamptz[]), cast(:submitted_bys as uuid[]),
                       cast(:approved_ats as timestamptz[]), cast(:approved_bys as uuid[]),
                       cast(:scores as numeric[]), cast(:critical_fails as int[]),
                       cast(:lates as boolean[]), cast(:minutes as int[])
                   ) as u(id, status, started_at, started_by, submitted_at,
                          submitted_by, approved_at, approved_by, score,
                          critical_fails, is_late, minutes_late)
             where r.id = u.id
            """
        ),
        {
            "ids": [u["id"] for u in run_updates],
            "statuses": [u["status"] for u in run_updates],
            "started_ats": [u["started_at"] for u in run_updates],
            "started_bys": [u["started_by"] for u in run_updates],
            "submitted_ats": [u["submitted_at"] for u in run_updates],
            "submitted_bys": [u["submitted_by"] for u in run_updates],
            "approved_ats": [u["approved_at"] for u in run_updates],
            "approved_bys": [u["approved_by"] for u in run_updates],
            "scores": [u["score"] for u in run_updates],
            "critical_fails": [u["critical_fails"] for u in run_updates],
            "lates": [u["is_late"] for u in run_updates],
            "minutes": [u["minutes_late"] for u in run_updates],
        },
    )
    if exceptions:
        # Older exceptions are mostly worked; the recent ones stay open so the
        # exception board is a live queue, not a museum. DEV02's old open highs
        # are what feeds the stale-critical penalty, on purpose.
        for e in exceptions:
            if day >= REVIEW_LAG_FROM - timedelta(days=3):
                e["status"], e["resolved"] = "open", False
            elif e["rng"] < 0.7:
                e["status"], e["resolved"] = "resolved", True
            elif e["rng"] < 0.9:
                e["status"], e["resolved"] = "acknowledged", False
            else:
                e["status"], e["resolved"] = "open", False
        await db.execute(
            text(
                """
                insert into sop_exceptions
                    (run_item_id, outlet_id, business_date, severity, title,
                     detail, status, resolved_by, resolved_at, resolution_note)
                select u.run_item_id, u.outlet_id, u.day, cast(u.severity as severity),
                       u.title, u.detail, cast(u.status as exception_status),
                       u.resolved_by, u.resolved_at,
                       case when u.resolved_by is not null then 'Handled on the day' end
                  from unnest(
                           cast(:item_ids as uuid[]), cast(:outlet_ids as uuid[]),
                           cast(:days as date[]), cast(:severities as text[]),
                           cast(:titles as text[]), cast(:details as text[]),
                           cast(:statuses as text[]), cast(:resolved_bys as uuid[]),
                           cast(:resolved_ats as timestamptz[])
                       ) as u(run_item_id, outlet_id, day, severity, title,
                              detail, status, resolved_by, resolved_at)
                """
            ),
            {
                "item_ids": [e["run_item_id"] for e in exceptions],
                "outlet_ids": [e["outlet_id"] for e in exceptions],
                "days": [e["day"] for e in exceptions],
                "severities": [e["severity"] for e in exceptions],
                "titles": [e["title"] for e in exceptions],
                "details": [e["detail"] for e in exceptions],
                "statuses": [e["status"] for e in exceptions],
                "resolved_bys": [e["approver"] if e["resolved"] else None for e in exceptions],
                "resolved_ats": [
                    (
                        datetime.combine(e["day"], time(23, 0), tzinfo=IST) + timedelta(days=1)
                        if e["resolved"]
                        else None
                    )
                    for e in exceptions
                ],
            },
        )
    await db.commit()


# ---------------------------------------------------------------------------
# Photos
# ---------------------------------------------------------------------------


def make_photo(rng: random.Random, *, dark: bool = False) -> bytes:
    """A real, decodable JPEG that no two items share (unless planted)."""
    from PIL import Image, ImageDraw

    base = 24 if dark else rng.randint(110, 170)
    img = Image.new("RGB", (480, 360), (base, base - 8, base - 16))
    draw = ImageDraw.Draw(img)
    for _ in range(60):
        x, y = rng.randint(0, 470), rng.randint(0, 350)
        w, h = rng.randint(6, 90), rng.randint(6, 70)
        shade = max(0, min(255, base + rng.randint(-70, 70)))
        draw.rectangle([x, y, x + w, y + h], fill=(shade, shade - 4, shade - 10))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return buf.getvalue()


async def upload_photos(db: AsyncSession) -> dict[str, int]:
    """Upload one generated JPEG per photo-requiring item, then let the REAL
    photo pass compute hashes, luminance and flags.

    Finds its own work — every answered photo item in the window that has no
    photo yet — so a run that died halfway resumes exactly where it stopped
    instead of leaving the tail photoless forever.

    The upload timestamp sits inside the run's open window, because that is
    when a real tablet would have sent it — and `stale_capture` checks exactly
    that. Two planted anomalies are the exception: one template item at DEV02
    reuses the same bytes on adjacent days (a true positive for the phash
    lookback), and one DEV02 shot is genuinely too dark.
    """
    todo = [
        dict(r)
        for r in (
            await db.execute(
                text(
                    """
                    select ri.id, ri.template_item_id, r.id as run_id,
                           r.outlet_id, r.business_date as day, o.code,
                           r.started_at, r.submitted_at
                      from checklist_run_items ri
                      join checklist_template_item_versions v
                        on v.id = ri.template_item_version_id
                      join checklist_runs r on r.id = ri.run_id
                      join outlets o on o.id = r.outlet_id
                     where v.requires_photo
                       and ri.result in ('pass', 'fail')
                       and ri.photo_path is null
                       and r.status in ('submitted', 'approved')
                       and r.business_date between :a and :b
                     order by r.business_date, ri.id
                    """
                ),
                {"a": PHOTO_DAYS_FROM, "b": LAST_DAY},
            )
        ).mappings()
    ]

    dup_item = next(
        (
            i["template_item_id"]
            for i in todo
            if i["code"] == "AKR-DEV02" and i["day"] == date(2026, 8, 24)
        ),
        None,
    )
    planted_dup: bytes | None = None
    dark_done = False

    uploaded = 0
    for item in todo:
        rng = random.Random(f"photo:{item['id']}")
        if (
            dup_item is not None
            and item["template_item_id"] == dup_item
            and item["day"] in (date(2026, 8, 24), date(2026, 8, 25))
        ):
            if planted_dup is None:
                planted_dup = make_photo(rng)
            blob = planted_dup
        elif item["code"] == "AKR-DEV02" and item["day"] == date(2026, 8, 25) and not dark_done:
            blob = make_photo(rng, dark=True)
            dark_done = True
        else:
            blob = make_photo(rng)

        path = photo_path_for(item["outlet_id"], item["day"], item["run_id"], item["id"])
        for attempt in range(3):
            try:
                await storage.upload_bytes(
                    path, blob, bucket=storage.SOP_PHOTO_BUCKET, content_type="image/jpeg"
                )
                break
            except Exception as exc:
                if attempt == 2:
                    raise
                print(f"  upload retry {attempt + 1} for {path}: {exc}")
                await asyncio.sleep(2 * (attempt + 1))

        window = (item["submitted_at"] - item["started_at"]).total_seconds()
        uploaded_at = item["started_at"] + timedelta(seconds=rng.uniform(60, max(120, window - 60)))
        await db.execute(
            text(
                "update checklist_run_items set photo_path = :p, photo_bytes = :b,"
                " photo_uploaded_at = :at where id = :id"
            ),
            {"p": path, "b": len(blob), "at": uploaded_at, "id": item["id"]},
        )
        await db.commit()  # per photo, so a crash loses one photo, not a day
        uploaded += 1
        if uploaded % 25 == 0:
            print(f"  uploaded {uploaded}/{len(todo)}")

    # Process oldest day first, so the duplicate SECOND appearance is the one
    # that gets flagged — the same order reality would have run in. Also
    # restart-safe: only unprocessed photos are picked up.
    unprocessed = [
        dict(r)
        for r in (
            await db.execute(
                text(
                    """
                    select ri.id, ri.run_id
                      from checklist_run_items ri
                      join checklist_runs r on r.id = ri.run_id
                     where ri.photo_path is not null
                       and ri.photo_processed_at is null
                       and r.business_date between :a and :b
                     order by r.business_date, ri.id
                    """
                ),
                {"a": PHOTO_DAYS_FROM, "b": LAST_DAY},
            )
        ).mappings()
    ]
    processed = flagged = 0
    for row in unprocessed:
        try:
            result = await integrity.process_photo(db, row["id"])
            await integrity.recount_run_flags(db, row["run_id"])
            await db.commit()
            processed += 1
            flagged += 1 if result.flags else 0
        except Exception as exc:
            await db.rollback()
            print(f"  photo pass failed for {row['id']}: {exc}")
    return {"uploaded": uploaded, "processed": processed, "flagged": flagged}


# ---------------------------------------------------------------------------
# DEV02 sales — synthetic, and labelled as such
# ---------------------------------------------------------------------------


async def seed_dev_sales(db: AsyncSession) -> dict[str, int]:
    """NT01 keeps its REAL Petpooja data untouched; fabricating rows beside it
    would dilute the one genuine dataset. DEV02 has nothing, so it gets a
    clearly-labelled synthetic eight weeks through the production upsert."""
    outlet_id = (
        await db.execute(text("select id from outlets where code = 'AKR-DEV02'"))
    ).scalar_one()
    existing = (
        await db.execute(
            text("select count(*) from sales_orders where outlet_id = :o"), {"o": outlet_id}
        )
    ).scalar_one()
    if existing:
        return {"orders": 0, "skipped_existing": existing}

    upload_id = (
        await db.execute(
            text(
                """
                insert into data_uploads
                    (outlet_id, source, original_filename, storage_path,
                     file_sha256, status, adapter_version, parsed_at)
                values (:o, 'manual', 'seed_history.synthetic', 'seed/none',
                        :sha, 'parsed', 'seed_history.v1', now())
                on conflict (file_sha256) do update set status = 'parsed'
                returning id
                """
            ),
            {"o": outlet_id, "sha": f"seed-history-dev02-{FIRST_DAY}"},
        )
    ).scalar_one()

    orders: list[petpooja.ParsedOrder] = []
    bill_no = 90000
    day = FIRST_DAY
    while day <= LAST_DAY:
        rng = random.Random(f"sales:{day}")
        weekend = day.weekday() in (4, 5, 6)
        bills = rng.randint(34, 52) if weekend else rng.randint(18, 32)
        for _ in range(bills):
            bill_no += 1
            hour = rng.choices(
                [12, 13, 14, 18, 19, 20, 21, 22, 23, 0], weights=[6, 8, 5, 7, 10, 12, 12, 9, 5, 2]
            )[0]
            ordered = datetime.combine(
                day + timedelta(days=1) if hour < 5 else day,
                time(hour, rng.randint(0, 59)),
                tzinfo=IST,
            )
            net = rng.randint(280_00, 1450_00)
            orders.append(
                petpooja.ParsedOrder(
                    external_bill_no=str(bill_no),
                    ordered_at=ordered,
                    # The parser's rule: past-midnight bills stay on the trading day.
                    business_date=day,
                    channel=rng.choices(
                        [SalesChannel.DINE_IN, SalesChannel.DELIVERY, SalesChannel.PICKUP],
                        weights=[6, 3, 1],
                    )[0],
                    covers=rng.randint(1, 6),
                    gross_paise=net,
                    discount_paise=0,
                    tax_paise=0,
                    net_paise=net,
                    payment_mode=rng.choice(["Cash", "UPI", "Card"]),
                    table_no=None,
                    customer_phone_hash=None,
                )
            )
        day += timedelta(days=1)

    written = await sales_service._write_orders(
        db, outlet_id, upload_id, petpooja.ParseResult(orders=orders)
    )
    await db.commit()
    return {"orders": written["inserted"]}


# ---------------------------------------------------------------------------


async def main() -> None:
    factory = get_session_factory()
    stats: dict[str, int] = {
        "missed": 0,
        "approved": 0,
        "submitted": 0,
        "late": 0,
        "critical_fails": 0,
    }
    async with factory() as db:
        people = await load_people(db)

        wiped = await wipe(db)
        print(f"wiped: {wiped}")

        day = FIRST_DAY
        while day <= LAST_DAY:
            made = await materialise_runs(db, for_date=day)
            await db.commit()
            await write_day(db, day, people, stats)
            print(f"{day}: materialised {made.get('created', 0)}")
            day += timedelta(days=1)

        print(f"outcomes: {stats}")
        photos = await upload_photos(db)
        print(f"photos: {photos}")
        sales = await seed_dev_sales(db)
        print(f"dev02 sales: {sales}")

        # Late runs get their real integrity flags from the real code.
        late_ids = [
            r[0]
            for r in await db.execute(
                text(
                    "select id from checklist_runs where is_late"
                    " and business_date between :a and :b"
                ),
                {"a": FIRST_DAY, "b": LAST_DAY},
            )
        ]
        for run_id in late_ids:
            await integrity.evaluate_run(db, uuid.UUID(str(run_id)))
        await db.commit()
        print(f"integrity: evaluated {len(late_ids)} late runs")

    await storage.aclose_client()


if __name__ == "__main__":
    asyncio.run(main())
