"""What happened at an outlet over a period, counted once.

The dashboard and the daily digest both need completion, on-time rate, mean
score and the exception picture. Two queries would eventually disagree, and the
day they do, nobody would know which number to believe — so there is one, and
the digest calls it for a single business date while the dashboard calls it for
a range.

Counting only. Turning these numbers into a score is `app/core/scoring.py`, and
resolving the weights that do it is the caller's job, because a period must be
scored with the weights that were live at its end (D9).
"""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.scoring import OutletCounts

#: `submitted` counts runs that reached submission or beyond — a run approved
#: today was submitted at some point, and excluding it would make the on-time
#: denominator shrink as the review queue is cleared.
_COUNTS_SQL = text(
    """
    select
        count(*)                                              as scheduled,
        count(*) filter (where status = 'approved')           as approved,
        count(*) filter (where status in ('submitted', 'approved')) as submitted,
        count(*) filter (
            where status in ('submitted', 'approved') and not is_late
        )                                                     as on_time,
        count(*) filter (where status = 'missed')             as missed,
        cast(avg(score_pct) filter (where status = 'approved') as float8)
                                                              as mean_run_score,
        coalesce(sum(integrity_flag_count), 0)                as integrity_flags
      from checklist_runs
     where outlet_id = :outlet_id
       and business_date between :start and :end
    """
)

#: Exceptions are counted as they stand NOW, not as of the period's end.
#: "Unresolved" is a present-tense claim: a critical failure from three weeks
#: ago that is still open is still a live problem, and the point of the penalty
#: is to make leaving it open expensive.
_EXCEPTIONS_SQL = text(
    """
    select
        count(*) filter (
            where severity = 'high' and status in ('open', 'acknowledged')
        ) as open_critical,
        count(*) filter (
            where severity = 'high' and status in ('open', 'acknowledged')
              and created_at < now() - interval '48 hours'
        ) as stale_critical
      from sop_exceptions
     where outlet_id = :outlet_id
    """
)


async def outlet_counts(
    db: AsyncSession, *, outlet_id: uuid.UUID, start: date, end: date
) -> OutletCounts:
    """Everything the score needs, for one outlet over an inclusive date range."""
    runs = (
        (await db.execute(_COUNTS_SQL, {"outlet_id": outlet_id, "start": start, "end": end}))
        .mappings()
        .one()
    )
    exceptions = (await db.execute(_EXCEPTIONS_SQL, {"outlet_id": outlet_id})).mappings().one()
    mean = runs["mean_run_score"]
    return OutletCounts(
        scheduled=runs["scheduled"],
        approved=runs["approved"],
        submitted=runs["submitted"],
        on_time=runs["on_time"],
        missed=runs["missed"],
        mean_run_score=round(mean, 1) if mean is not None else None,
        integrity_flags=runs["integrity_flags"],
        open_critical=exceptions["open_critical"],
        stale_critical=exceptions["stale_critical"],
    )


async def daily_scores(
    db: AsyncSession, *, outlet_id: uuid.UUID, start: date, end: date
) -> list[dict[str, Any]]:
    """Mean approved-run score per business date, for the sparkline.

    Days with no approved run are omitted rather than plotted as zero — a
    Monday the outlet was shut is not a Monday it failed.
    """
    rows = (
        (
            await db.execute(
                text(
                    """
                    select business_date,
                           cast(avg(score_pct) as float8) as score,
                           count(*) as approved
                      from checklist_runs
                     where outlet_id = :outlet_id
                       and business_date between :start and :end
                       and status = 'approved'
                       and score_pct is not null
                     group by business_date
                     order by business_date
                    """
                ),
                {"outlet_id": outlet_id, "start": start, "end": end},
            )
        )
        .mappings()
        .all()
    )
    return [
        {
            "business_date": r["business_date"],
            "score": round(r["score"], 1),
            "approved": r["approved"],
        }
        for r in rows
    ]
