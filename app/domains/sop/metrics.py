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
#:
#: Exceptions are counted as they stand NOW, not as of the period's end.
#: "Unresolved" is a present-tense claim: a critical failure from three weeks
#: ago that is still open is still a live problem, and the point of the penalty
#: is to make leaving it open expensive. That is why the exception CTE has no
#: date filter while the run CTE does.
#:
#: One statement for any number of outlets. The dashboard used to call this
#: once per outlet, and each call was two queries, so the outlet comparison row
#: cost two round trips per outlet before it had drawn anything. `wanted` is
#: what makes an outlet with no runs at all still come back — as a row of
#: zeroes, which is a real answer, rather than vanishing from the table.
_COUNTS_SQL = text(
    """
    with wanted as (
        select unnest(cast(:ids as uuid[])) as outlet_id
    ),
    runs as (
        select
            outlet_id,
            count(*)                                                   as scheduled,
            count(*) filter (where status = 'approved')                as approved,
            count(*) filter (where status in ('submitted', 'approved')) as submitted,
            count(*) filter (
                where status in ('submitted', 'approved') and not is_late
            )                                                          as on_time,
            count(*) filter (where status = 'missed')                  as missed,
            cast(avg(score_pct) filter (where status = 'approved') as float8)
                                                                       as mean_run_score,
            coalesce(sum(integrity_flag_count), 0)                     as integrity_flags
          from checklist_runs
         where outlet_id = any (cast(:ids as uuid[]))
           and business_date between :start and :end
         group by outlet_id
    ),
    exceptions as (
        select
            outlet_id,
            count(*) filter (
                where severity = 'high' and status in ('open', 'acknowledged')
            ) as open_critical,
            count(*) filter (
                where severity = 'high' and status in ('open', 'acknowledged')
                  and created_at < now() - interval '48 hours'
            ) as stale_critical
          from sop_exceptions
         where outlet_id = any (cast(:ids as uuid[]))
         group by outlet_id
    )
    select
        w.outlet_id,
        coalesce(r.scheduled, 0)        as scheduled,
        coalesce(r.approved, 0)         as approved,
        coalesce(r.submitted, 0)        as submitted,
        coalesce(r.on_time, 0)          as on_time,
        coalesce(r.missed, 0)           as missed,
        r.mean_run_score                as mean_run_score,
        coalesce(r.integrity_flags, 0)  as integrity_flags,
        coalesce(e.open_critical, 0)    as open_critical,
        coalesce(e.stale_critical, 0)   as stale_critical
      from wanted w
      left join runs r       on r.outlet_id = w.outlet_id
      left join exceptions e on e.outlet_id = w.outlet_id
    """
)


def _counts(row: Any) -> OutletCounts:
    mean = row["mean_run_score"]
    return OutletCounts(
        scheduled=row["scheduled"],
        approved=row["approved"],
        submitted=row["submitted"],
        on_time=row["on_time"],
        missed=row["missed"],
        mean_run_score=round(mean, 1) if mean is not None else None,
        integrity_flags=row["integrity_flags"],
        open_critical=row["open_critical"],
        stale_critical=row["stale_critical"],
    )


async def outlet_counts_many(
    db: AsyncSession, *, outlet_ids: list[uuid.UUID], start: date, end: date
) -> dict[uuid.UUID, OutletCounts]:
    """Everything the score needs, for many outlets, in one round trip."""
    if not outlet_ids:
        return {}
    rows = (
        (await db.execute(_COUNTS_SQL, {"ids": outlet_ids, "start": start, "end": end}))
        .mappings()
        .all()
    )
    return {uuid.UUID(str(r["outlet_id"])): _counts(r) for r in rows}


async def outlet_counts(
    db: AsyncSession, *, outlet_id: uuid.UUID, start: date, end: date
) -> OutletCounts:
    """Everything the score needs, for one outlet over an inclusive date range.

    The digest scores one outlet at a time and reads better for it. It is the
    same statement underneath, so the two callers cannot drift apart.
    """
    many = await outlet_counts_many(db, outlet_ids=[outlet_id], start=start, end=end)
    return many[outlet_id]


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
