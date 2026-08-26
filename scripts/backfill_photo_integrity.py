"""Hash and measure photos that were uploaded before the integrity engine existed.

Every photo confirmed from P5 onwards is hashed by a background task at
confirm time. The ones already in storage when P7 landed were not, and a
duplicate check that cannot see them has a blind spot exactly as long as the
lookback window.

Idempotent: it only touches rows where photo_processed_at is null, so running
it twice is safe and running it after a partial failure resumes.

    uv run python scripts/backfill_photo_integrity.py [--limit N] [--all]

--all re-processes rows that already have a result, which is what you want
after changing a threshold or the hash itself.
"""

import argparse
import asyncio
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from app.core.db import dispose_engine, get_session_factory
from app.domains.sop import integrity


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--all", action="store_true", help="redo rows already processed")
    args = parser.parse_args()

    where = "" if args.all else "and ri.photo_processed_at is null"
    async with get_session_factory()() as db:
        rows = (
            (
                await db.execute(
                    text(
                        f"""
                    select ri.id, ri.photo_path, r.outlet_id, r.business_date
                      from checklist_run_items ri
                      join checklist_runs r on r.id = ri.run_id
                     where ri.photo_path is not null {where}
                     order by ri.photo_uploaded_at
                     limit :limit
                    """
                    ),
                    {"limit": args.limit},
                )
            )
            .mappings()
            .all()
        )

        print(f"{len(rows)} photo(s) to process")
        runs: set[uuid.UUID] = set()
        failed = 0
        for row in rows:
            try:
                result = await integrity.process_photo(db, row["id"])
            except Exception as exc:  # one bad object must not stop the backfill
                failed += 1
                print(f"  FAILED {row['id']}: {type(exc).__name__}: {exc}")
                await db.rollback()
                continue
            runs.add(row["id"])
            print(
                f"  {row['id']}  phash={result.phash}  "
                f"lum={result.luminance:6.1f}  flags={result.flags or '-'}"
            )
            for flag, evidence in result.detail.items():
                print(f"      {flag}: {evidence}")

        # Re-evaluate every run those photos belong to, so the counts agree.
        touched = (
            (
                await db.execute(
                    text(
                        "select distinct run_id from checklist_run_items"
                        " where photo_processed_at is not null"
                    )
                )
            )
            .scalars()
            .all()
        )
        for run_id in touched:
            await integrity.evaluate_run(db, run_id)
        await db.commit()
        print(f"re-evaluated {len(touched)} run(s); {failed} failure(s)")

    await dispose_engine()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
