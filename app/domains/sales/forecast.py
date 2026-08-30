"""The forecasting baseline (spec 5.1). Pure arithmetic, working attached.

    forecast(outlet, date) = median(same weekday, last 4 same-weekday
                             business dates that traded)
                           x trend_factor(last 14 days vs prior 14, clamped)
                           x event_multiplier(manual flag, default 1)

The spec's own words: "start boring — Prophet/ARIMA/an LLM will overfit
noise and produce confident nonsense" on six weeks of data. This model can
be checked on a napkin, which is the point: every forecast carries the
median it started from, the sample dates behind that median, the trend
factor and the event multiplier, so a manager who disbelieves the number
can see exactly which Saturday made it.

Refusals over guesses, as everywhere else in this codebase:

- Fewer than MIN_SAMPLES same-weekday trading days -> no forecast, with the
  reason. A median of one Saturday is that Saturday wearing a formula.
- A short trend window (under half of 14 days traded on either side) -> the
  factor is 1.0 and says so. A trend computed from three days against
  twelve is noise dressed as momentum.
- Covers are forecast only where the covers history is dense enough
  (at least MIN_SAMPLES same-weekday days WITH covers); the real Petpooja
  data records covers patchily, and null beats invented diners.

The model id is versioned like the adapters: a change in this arithmetic is
a new model string, so stored forecasts always name the arithmetic that
made them and MAPE never silently mixes two models.
"""

import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

MODEL = "baseline.v1"

#: Same-weekday history: how many recent same-weekday trading days feed the
#: median, and the minimum below which the forecast refuses.
WEEKDAY_WINDOW = 4
MIN_SAMPLES = 2

#: Trend: last 14 trading-day window against the prior 14, clamped per spec.
TREND_DAYS = 14
TREND_MIN_DAYS = 7


@dataclass(frozen=True)
class DayActual:
    """One trading day's actuals. Days with no bills simply do not appear."""

    business_date: date
    net_paise: int
    covers: int | None  # None when Petpooja recorded no cover counts


@dataclass(frozen=True)
class Forecast:
    target_date: date
    net_paise: int | None
    covers: int | None
    #: Why there is no number, when there is none.
    reason: str | None
    components: dict[str, Any] = field(default_factory=dict)


def trend_factor(
    history: list[DayActual], *, as_of: date, clamp_min: float, clamp_max: float
) -> tuple[float, dict[str, Any]]:
    """Net in the last 14 calendar days over the 14 before, per trading day.

    Per trading day, not raw sums: a window with 12 trading days against one
    with 9 would otherwise read as growth that is really just opening days.
    """
    recent_start = as_of - timedelta(days=TREND_DAYS - 1)
    prior_start = recent_start - timedelta(days=TREND_DAYS)
    recent = [d for d in history if recent_start <= d.business_date <= as_of]
    prior = [d for d in history if prior_start <= d.business_date < recent_start]

    if len(recent) < TREND_MIN_DAYS or len(prior) < TREND_MIN_DAYS:
        return 1.0, {
            "trend_factor": 1.0,
            "trend_note": (
                f"held at 1.0 — {len(recent)} recent vs {len(prior)} prior trading "
                f"days is too thin a base for a trend"
            ),
        }

    recent_rate = sum(d.net_paise for d in recent) / len(recent)
    prior_rate = sum(d.net_paise for d in prior) / len(prior)
    if prior_rate <= 0:
        return 1.0, {"trend_factor": 1.0, "trend_note": "held at 1.0 — no prior revenue"}

    raw = recent_rate / prior_rate
    clamped = max(clamp_min, min(clamp_max, raw))
    working: dict[str, Any] = {
        "trend_factor": round(clamped, 3),
        "trend_raw": round(raw, 3),
        "trend_recent_per_day": round(recent_rate),
        "trend_prior_per_day": round(prior_rate),
    }
    if clamped != raw:
        working["trend_note"] = f"clamped from {raw:.2f} to [{clamp_min}, {clamp_max}]"
    return clamped, working


def forecast_day(
    history: list[DayActual],
    *,
    target: date,
    as_of: date,
    event_multiplier: float = 1.0,
    event_label: str | None = None,
    clamp_min: float = 0.8,
    clamp_max: float = 1.3,
) -> Forecast:
    """One day's forecast from the trading history known at `as_of`.

    `history` may contain any range; only days at or before `as_of` are
    used — forecasting tomorrow must not peek at tomorrow.
    """
    known = sorted((d for d in history if d.business_date <= as_of), key=lambda d: d.business_date)
    same_weekday = [d for d in known if d.business_date.weekday() == target.weekday()]
    samples = same_weekday[-WEEKDAY_WINDOW:]

    if len(samples) < MIN_SAMPLES:
        return Forecast(
            target_date=target,
            net_paise=None,
            covers=None,
            reason=(
                f"only {len(samples)} {target.strftime('%A')}s traded in the history — "
                f"a median needs at least {MIN_SAMPLES}"
            ),
        )

    median_net = statistics.median(d.net_paise for d in samples)
    factor, trend_working = trend_factor(
        known, as_of=as_of, clamp_min=clamp_min, clamp_max=clamp_max
    )
    net = round(median_net * factor * event_multiplier)

    covers_samples = [d.covers for d in samples if d.covers is not None and d.covers > 0]
    covers: int | None = None
    if len(covers_samples) >= MIN_SAMPLES:
        covers = round(statistics.median(covers_samples) * factor * event_multiplier)

    components: dict[str, Any] = {
        "model": MODEL,
        "weekday": target.strftime("%A"),
        "median_net_paise": round(median_net),
        "sample_dates": [str(d.business_date) for d in samples],
        **trend_working,
        "event_multiplier": event_multiplier,
    }
    if event_label:
        components["event_label"] = event_label
    if covers is None and covers_samples:
        components["covers_note"] = (
            f"only {len(covers_samples)} of {len(samples)} sample days recorded covers"
        )

    return Forecast(
        target_date=target,
        net_paise=net,
        covers=covers,
        reason=None,
        components=components,
    )
