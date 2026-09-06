"""The Outlet Health card.

Spec section 5: one number per outlet from four weighted pillars. All four
now produce (P15) — SOP and Sales in full, Inventory and Guest with their
gaps declared as pending components — so the blended score D14 refused to
fake is finally arithmetic: sum(pillar x weight) renormalised over the
pillars actually measured this period. D14 retires; D22 records the blend.

An unmeasured pillar (no confirmed counts yet, say) is left out of the
denominator and named in `health.unmeasured` — never padded with a zero,
because "not measured" and "failed" must not be the same number.

Weights are resolved at the END of the period being scored, so re-opening last
month uses the weights that were live last month (D9).
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.business_date import business_date as to_business_date
from app.core.business_date import business_date_bounds, outlet_now
from app.core.db import read_with
from app.core.deps import CurrentUser, CurrentUserDep, DbDep, require_management
from app.core.errors import ForbiddenError, NotFoundError
from app.core.pillar_math import Pillar
from app.core.scoring import ScoreWeights, outlet_score
from app.core.settings_value import resolve_many, resolve_many_outlets
from app.domains.dashboard.health import PillarReading, blended_health
from app.domains.inventory import pillar_service as inv_service
from app.domains.inventory.pillar import inventory_pillar
from app.domains.sales import pillar_service
from app.domains.sales.guest_pillar import guest_pillar
from app.domains.sales.pillar import sales_pillar
from app.domains.sop import metrics

router = APIRouter(prefix="/dashboard", tags=["dashboard"])

DEFAULT_PERIOD_DAYS = 28

#: Spec section 5's weights, fixed here rather than in the settings registry —
#: they become editable the day somebody actually needs to tune them.
PILLARS = [
    {"key": "sales", "label": "Sales & growth", "weight": 30},
    {"key": "sop", "label": "SOP compliance", "weight": 30},
    {"key": "inventory", "label": "Inventory discipline", "weight": 25},
    {"key": "guest", "label": "Guest & throughput", "weight": 15},
]


def _pillar_block(pillar: Pillar) -> dict[str, Any]:
    """One serialisation for the pillar_math-shaped pillars, so inventory and
    guest cannot drift apart on the wire."""
    worst = pillar.worst_component
    return {
        "score": pillar.score,
        "band": pillar.band,
        "components": [
            {
                "key": c.key,
                "label": c.label,
                "display": c.display,
                "target": c.target_display,
                "score": c.score,
                "weight": c.weight,
                "contribution": c.contribution,
                "band": c.band,
                "status": c.status,
                "note": c.note,
            }
            for c in pillar.components
        ],
        "dragged_down_by": (
            {"key": worst.key, "label": worst.label, "display": worst.display}
            if worst is not None
            else None
        ),
        "detail": pillar.detail,
    }


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


#: Every setting the card needs, from all four pillars, resolved in ONE
#: statement. Each pillar service keeps its own `*_targets_many` for callers
#: that want one pillar — the digest — but here they were four round trips
#: asking the same function about the same outlets at the same instant, and
#: the wire is what this endpoint waits on.
_ALL_SETTING_KEYS = sorted(
    {
        *_WEIGHT_KEYS,
        *pillar_service.TARGET_KEYS,
        *pillar_service.GUEST_TARGET_KEYS,
        *inv_service.TARGET_KEYS,
    }
)


@dataclass(frozen=True)
class _Loaded:
    """Everything the card computes from, for a set of outlets."""

    counts: dict[uuid.UUID, Any]
    settings: dict[uuid.UUID, dict[str, Any]]
    sales_in: dict[uuid.UUID, Any]
    inv_in: dict[uuid.UUID, Any]
    guest_in: dict[uuid.UUID, Any]
    trend: list[dict[str, Any]]


async def _nothing() -> list[dict[str, Any]]:
    return []


async def _load(
    db: AsyncSession,
    ids: list[uuid.UUID],
    *,
    start: date,
    end: date,
    trend_for: uuid.UUID | None = None,
) -> _Loaded:
    """All of the card's reads, on the wire at once.

    They are independent aggregates over different tables for the same
    outlets and period, so there is no reason for one to wait for another.
    Each goes on its own pooled connection (`read_with`) and `gather` sends
    them together: the wall time is one round trip rather than five or six.
    Before this the comparison row cost ten sequential trips — 3.4 s with the
    database in Sydney — and did nothing with the first nine except wait.
    """
    _, closed_at = business_date_bounds(end)
    counts, settings, sales_in, inv_in, guest_in, trend = await asyncio.gather(
        read_with(db, metrics.outlet_counts_many, outlet_ids=ids, start=start, end=end),
        read_with(db, resolve_many_outlets, _ALL_SETTING_KEYS, outlet_ids=ids, at=closed_at),
        read_with(db, pillar_service.sales_inputs_many, outlet_ids=ids, start=start, end=end),
        read_with(db, inv_service.inventory_inputs_many, outlet_ids=ids, start=start, end=end),
        read_with(db, pillar_service.guest_inputs_many, outlet_ids=ids, start=start, end=end),
        (
            read_with(db, metrics.daily_scores, outlet_id=trend_for, start=start, end=end)
            if trend_for is not None
            else _nothing()
        ),
    )
    return _Loaded(
        counts=counts,
        settings=settings,
        sales_in=sales_in,
        inv_in=inv_in,
        guest_in=guest_in,
        trend=trend,
    )


class OutletHealthRow(BaseModel):
    outlet_id: uuid.UUID
    outlet_code: str
    outlet_name: str
    #: The BLENDED health since P15 — previously this was the SOP score alone.
    score: float | None
    band: str
    capped_by_critical: bool
    #: Each pillar's own score, None where nothing was measured this period.
    pillars: dict[str, float | None]
    unmeasured: list[str]


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
    elif not user.is_platform_admin:
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

    # A fixed number of round trips whatever the outlet count (D16), and all
    # of them in flight together. The row shows BLENDED health since P15, so
    # it needs every pillar's inputs — each is one set-based statement, never
    # one per outlet.
    ids = [outlet["id"] for outlet in outlets]
    loaded = await _load(db, ids, start=start, end=end)

    out = []
    for outlet in outlets:
        oid = outlet["id"]
        values = loaded.settings[oid]
        weights = _weights(values)
        sop = outlet_score(loaded.counts[oid], weights)
        by_key: dict[str, float | None] = {
            "sop": sop.score,
            "sales": sales_pillar(loaded.sales_in[oid], pillar_service.targets_from(values)).score,
            "inventory": inventory_pillar(
                loaded.inv_in[oid], inv_service.targets_from(values)
            ).score,
            "guest": guest_pillar(
                loaded.guest_in[oid], pillar_service.guest_targets_from(values)
            ).score,
        }
        health = blended_health(
            [
                PillarReading(
                    key=str(pillar["key"]),
                    label=str(pillar["label"]),
                    weight=float(pillar["weight"]),  # type: ignore[arg-type]
                    score=by_key[str(pillar["key"])],
                )
                for pillar in PILLARS
            ],
            green=weights.green,
            amber=weights.amber,
        )
        out.append(
            OutletHealthRow(
                outlet_id=oid,
                outlet_code=outlet["code"],
                outlet_name=outlet["name"],
                score=health.score,
                band=health.band,
                capped_by_critical=sop.capped_by_critical,
                pillars=by_key,
                unmeasured=health.unmeasured,
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
    # Every read for the card in flight together, with all four pillars'
    # settings in one of them — resolved at the period's END (D9), so
    # re-opening last month scores against last month's targets.
    loaded = await _load(db, [outlet_id], start=start, end=end, trend_for=outlet_id)
    values = loaded.settings[outlet_id]
    weights = _weights(values)
    counts = loaded.counts[outlet_id]
    score = outlet_score(counts, weights)
    trend = loaded.trend

    sales = sales_pillar(loaded.sales_in[outlet_id], pillar_service.targets_from(values))
    sales_worst = sales.worst_component
    inventory = inventory_pillar(loaded.inv_in[outlet_id], inv_service.targets_from(values))
    guest = guest_pillar(loaded.guest_in[outlet_id], pillar_service.guest_targets_from(values))

    by_key: dict[str, float | None] = {
        "sop": score.score,
        "sales": sales.score,
        "inventory": inventory.score,
        "guest": guest.score,
    }
    band_by_key: dict[str, str] = {
        "sop": score.band,
        "sales": sales.band,
        "inventory": inventory.band,
        "guest": guest.band,
    }
    health = blended_health(
        [
            PillarReading(
                key=str(pillar["key"]),
                label=str(pillar["label"]),
                weight=float(pillar["weight"]),  # type: ignore[arg-type]
                score=by_key[str(pillar["key"])],
            )
            for pillar in PILLARS
        ],
        green=weights.green,
        amber=weights.amber,
    )

    worst = score.worst_component
    return {
        "outlet_id": str(outlet_id),
        "outlet_code": outlet["code"],
        "outlet_name": outlet["name"],
        "period": {"from": str(start), "to": str(end), "days": days},
        "health": {
            "score": health.score,
            "band": health.band,
            "weights_used": health.weights_used,
            "weights_total": health.weights_total,
            "unmeasured": health.unmeasured,
        },
        "pillars": [
            {
                **pillar,
                "score": by_key[str(pillar["key"])],
                "band": band_by_key[str(pillar["key"])],
                # A pillar with nothing to measure this period says so rather
                # than showing a zero.
                "status": ("live" if by_key[str(pillar["key"])] is not None else "not_measured"),
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
        "sales": {
            "score": sales.score,
            "band": sales.band,
            "components": [
                {
                    "key": c.key,
                    "label": c.label,
                    "display": c.display,
                    "target": c.target_display,
                    "score": c.score,
                    "weight": c.weight,
                    "contribution": c.contribution,
                    "band": c.band,
                }
                for c in sales.components
            ],
            "dragged_down_by": (
                {"key": sales_worst.key, "label": sales_worst.label, "display": sales_worst.display}
                if sales_worst is not None
                else None
            ),
            "detail": sales.detail,
        },
        "inventory": _pillar_block(inventory),
        "guest": _pillar_block(guest),
        "trend": trend,
    }
