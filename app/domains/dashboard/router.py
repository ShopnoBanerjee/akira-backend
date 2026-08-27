"""The Outlet Health card.

Spec section 5 describes one number per outlet from four weighted pillars.
**Stage 1 delivers exactly one of them**, so this endpoint returns the SOP
compliance score as the headline and the other three as declared-but-pending.

It does not compute a blended health score. Multiplying a live pillar by 0.30
and calling the result "outlet health" would be a number that means nothing —
it would read as a catastrophic 27/100 for a perfect outlet, or, if silently
rescaled, would change the moment a second pillar arrived without anything
about the outlet having changed. The layout is built for four pillars so it
does not move later; the arithmetic waits until there is something to do it
with. Recorded as D14.

Weights are resolved at the END of the period being scored, so re-opening last
month uses the weights that were live last month (D9).
"""

import uuid
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.business_date import business_date as to_business_date
from app.core.business_date import business_date_bounds, outlet_now
from app.core.deps import CurrentUser, CurrentUserDep, DbDep, require_management
from app.core.errors import ForbiddenError, NotFoundError
from app.core.scoring import ScoreWeights, outlet_score
from app.core.settings_value import resolve_many, resolve_many_outlets
from app.domains.sop import metrics

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

DEFAULT_PERIOD_DAYS = 28

#: Spec section 5. Weights are fixed here rather than in the settings registry
#: because three of the four have nothing to weight yet; they become editable
#: when Stage 2 gives them components.
PILLARS = [
    {"key": "sales", "label": "Sales & growth", "weight": 30},
    {"key": "sop", "label": "SOP compliance", "weight": 30},
    {"key": "inventory", "label": "Inventory discipline", "weight": 25},
    {"key": "guest", "label": "Guest & throughput", "weight": 15},
]

_WEIGHT_KEYS = [
    "scoring.weight.run_score",
    "scoring.weight.completion_rate",
    "scoring.weight.on_time_rate",
    "scoring.penalty.stale_exception",
    "scoring.penalty.integrity_flag",
    "scoring.band.green",
    "scoring.band.amber",
]


def _weights(values: dict[str, Any]) -> ScoreWeights:
    return ScoreWeights(
        run_score=float(values["scoring.weight.run_score"]),
        completion_rate=float(values["scoring.weight.completion_rate"]),
        on_time_rate=float(values["scoring.weight.on_time_rate"]),
        stale_exception_penalty=float(values["scoring.penalty.stale_exception"]),
        integrity_flag_penalty=float(values["scoring.penalty.integrity_flag"]),
        green=float(values["scoring.band.green"]),
        amber=float(values["scoring.band.amber"]),
    )


async def _weights_at(db: AsyncSession, *, outlet_id: uuid.UUID, end: date) -> ScoreWeights:
    """The weights that were in force at the end of the period.

    Not "now". Scoring July with August's weights would make a historical
    number change every time somebody adjusts a dial, which is exactly what
    the effective-dated settings table exists to prevent.
    """
    # The instant that trading day closed: the 05:00 rollover on the next
    # calendar day, in the outlet's own zone.
    _, closed_at = business_date_bounds(end)
    return _weights(await resolve_many(db, _WEIGHT_KEYS, outlet_id=outlet_id, at=closed_at))


async def _weights_at_many(
    db: AsyncSession, *, outlet_ids: list[uuid.UUID], end: date
) -> dict[uuid.UUID, ScoreWeights]:
    """The same, for every outlet on the comparison row, in one round trip."""
    _, closed_at = business_date_bounds(end)
    per_outlet = await resolve_many_outlets(db, _WEIGHT_KEYS, outlet_ids=outlet_ids, at=closed_at)
    return {outlet: _weights(values) for outlet, values in per_outlet.items()}


class OutletHealthRow(BaseModel):
    outlet_id: uuid.UUID
    outlet_code: str
    outlet_name: str
    score: float | None
    band: str
    capped_by_critical: bool


async def _visible_outlets(
    db: AsyncSession, user: CurrentUser, outlet_id: uuid.UUID | None
) -> list[dict[str, Any]]:
    clauses = ["o.is_active", "o.deleted_at is null"]
    params: dict[str, Any] = {}
    if outlet_id is not None:
        if not user.can_access_outlet(outlet_id):
            raise ForbiddenError("You do not have access to that outlet.")
        clauses.append("o.id = :outlet_id")
        params["outlet_id"] = outlet_id
    elif not user.is_global:
        if not user.outlet_ids:
            return []
        clauses.append("o.id = any(:ids)")
        params["ids"] = sorted(user.outlet_ids)
    rows = (
        (
            await db.execute(
                text(
                    f"select o.id, o.code, o.name, o.timezone from outlets o"
                    f" where {' and '.join(clauses)} order by o.code"
                ),
                params,
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


def _period(days: int, to: date | None) -> tuple[date, date]:
    end = to or to_business_date(outlet_now())
    return end - timedelta(days=days - 1), end


@router.get(
    "/outlets",
    response_model=list[OutletHealthRow],
    dependencies=[Depends(require_management)],
    summary="One score per outlet the caller can see",
)
async def outlet_scores(
    db: DbDep,
    user: CurrentUserDep,
    days: Annotated[int, Query(ge=1, le=365)] = DEFAULT_PERIOD_DAYS,
    to: date | None = Query(default=None),
) -> list[OutletHealthRow]:
    """The comparison row across outlets. Ordered by code, not by score — a
    league table invites gaming the number rather than doing the work."""
    start, end = _period(days, to)
    outlets = await _visible_outlets(db, user, None)
    if not outlets:
        return []

    # Three round trips total, whatever the outlet count. The loop that was
    # here made three per outlet, so this screen got slower as the group grew —
    # the one thing a multi-outlet comparison must not do.
    ids = [outlet["id"] for outlet in outlets]
    counts = await metrics.outlet_counts_many(db, outlet_ids=ids, start=start, end=end)
    weights = await _weights_at_many(db, outlet_ids=ids, end=end)

    out = []
    for outlet in outlets:
        score = outlet_score(counts[outlet["id"]], weights[outlet["id"]])
        out.append(
            OutletHealthRow(
                outlet_id=outlet["id"],
                outlet_code=outlet["code"],
                outlet_name=outlet["name"],
                score=score.score,
                band=score.band,
                capped_by_critical=score.capped_by_critical,
            )
        )
    return out


@router.get(
    "/outlet-health",
    dependencies=[Depends(require_management)],
    summary="The four-pillar health card for one outlet",
)
async def outlet_health(
    db: DbDep,
    user: CurrentUserDep,
    outlet_id: Annotated[uuid.UUID, Query()],
    days: Annotated[int, Query(ge=1, le=365)] = DEFAULT_PERIOD_DAYS,
    to: date | None = Query(default=None),
) -> dict[str, Any]:
    """Everything the card renders: the score, how it was arrived at, what
    dragged it down, and the trend behind it."""
    outlets = await _visible_outlets(db, user, outlet_id)
    if not outlets:
        raise NotFoundError("That outlet does not exist, or is not active.")
    outlet = outlets[0]

    start, end = _period(days, to)
    counts = await metrics.outlet_counts(db, outlet_id=outlet_id, start=start, end=end)
    weights = await _weights_at(db, outlet_id=outlet_id, end=end)
    score = outlet_score(counts, weights)
    trend = await metrics.daily_scores(db, outlet_id=outlet_id, start=start, end=end)

    worst = score.worst_component
    return {
        "outlet_id": str(outlet_id),
        "outlet_code": outlet["code"],
        "outlet_name": outlet["name"],
        "period": {"from": str(start), "to": str(end), "days": days},
        "pillars": [
            {
                **pillar,
                "score": score.score if pillar["key"] == "sop" else None,
                "band": score.band if pillar["key"] == "sop" else "none",
                # The card greys these rather than hiding them, so the shape of
                # the finished thing is visible from the start.
                "status": "live" if pillar["key"] == "sop" else "stage_2",
            }
            for pillar in PILLARS
        ],
        "sop": {
            "score": score.score,
            "band": score.band,
            "capped_by_critical": score.capped_by_critical,
            "components": [
                {
                    "key": c.key,
                    "label": c.label,
                    "value": c.value,
                    "weight": c.weight,
                    "contribution": c.contribution,
                }
                for c in score.components
            ],
            "penalties": [
                {"key": p.key, "label": p.label, "points": p.points, "detail": p.detail}
                for p in score.penalties
            ],
            "dragged_down_by": (
                {"key": worst.key, "label": worst.label, "value": worst.value}
                if worst is not None
                else None
            ),
            "counts": {
                "scheduled": counts.scheduled,
                "approved": counts.approved,
                "submitted": counts.submitted,
                "on_time": counts.on_time,
                "missed": counts.missed,
                "integrity_flags": counts.integrity_flags,
                "open_critical": counts.open_critical,
                "stale_critical": counts.stale_critical,
            },
        },
        "trend": trend,
    }
