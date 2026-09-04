"""The registry of admin-editable settings.

The code owns the schema of settings, not the app_settings table (see
docs/DECISIONS.md D9). Every known key is declared here with its type, default,
valid range and whether an outlet may override it. A key absent from this
registry is ignored on read and refused on write — that is what stops a typo,
or a value out of range, from quietly breaking scoring.

The table stores history; this module stores meaning.

Deliberately NOT here: the 05:00 business-date rollover. business_date() is
immutable so the planner can use it in index expressions, and every historical
row already stores its business_date. Changing it is a migration plus an
explicit backfill, on purpose.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

SettingType = Literal["number", "integer", "boolean", "string", "time"]

#: Groups drive the admin UI's sectioning and each group's permission note.
SettingGroup = Literal["scoring", "sales", "inventory", "guest", "integrity", "ai_review", "jobs"]


@dataclass(frozen=True)
class SettingDef:
    key: str
    group: SettingGroup
    type: SettingType
    default: Any
    label: str
    description: str
    #: Whether an outlet-level override is meaningful for this key.
    outlet_overridable: bool = False
    minimum: float | None = None
    maximum: float | None = None
    #: For string settings with a closed set of values.
    choices: tuple[str, ...] = field(default=())


REGISTRY: dict[str, SettingDef] = {
    d.key: d
    for d in [
        # --- Scoring and health bands (spec section 4.3) -------------------
        SettingDef(
            "scoring.weight.run_score",
            "scoring",
            "number",
            0.50,
            "Run score weight",
            "Share of the outlet SOP score carried by the mean approved-run score. "
            "The three weights should sum to 1.",
            minimum=0,
            maximum=1,
            outlet_overridable=False,
        ),
        SettingDef(
            "scoring.weight.completion_rate",
            "scoring",
            "number",
            0.30,
            "Completion rate weight",
            "Share carried by runs approved out of runs scheduled.",
            minimum=0,
            maximum=1,
        ),
        SettingDef(
            "scoring.weight.on_time_rate",
            "scoring",
            "number",
            0.20,
            "On-time rate weight",
            "Share carried by runs submitted before due time plus grace.",
            minimum=0,
            maximum=1,
        ),
        SettingDef(
            "scoring.critical_item_weight",
            "scoring",
            "integer",
            3,
            "Critical item weight",
            "Weight of a critical checklist item relative to a normal item's 1.",
            minimum=1,
            maximum=10,
        ),
        SettingDef(
            "scoring.penalty.stale_exception",
            "scoring",
            "number",
            2.0,
            "Stale exception penalty",
            "Points deducted per open high-severity exception older than 48 hours.",
            minimum=0,
            maximum=20,
        ),
        SettingDef(
            "scoring.penalty.integrity_flag",
            "scoring",
            "number",
            1.0,
            "Integrity flag penalty",
            "Points deducted per integrity flag per 10 runs.",
            minimum=0,
            maximum=20,
        ),
        SettingDef(
            "scoring.band.green",
            "scoring",
            "number",
            90,
            "Green band threshold",
            "Scores at or above this are green.",
            minimum=0,
            maximum=100,
            outlet_overridable=True,
        ),
        SettingDef(
            "scoring.band.amber",
            "scoring",
            "number",
            75,
            "Amber band threshold",
            "Scores at or above this (and below green) are amber; below is red.",
            minimum=0,
            maximum=100,
            outlet_overridable=True,
        ),
        # --- Inventory discipline pillar (spec section 5 / P15) ------------
        SettingDef(
            "inventory.target.clean_req_share",
            "inventory",
            "number",
            0.90,
            "Requisition accuracy target",
            "Green when at least this share of finalised requisition lines "
            "stayed within need (no padding flag).",
            minimum=0,
            maximum=1,
            outlet_overridable=True,
        ),
        SettingDef(
            "inventory.max.stockouts_28d",
            "inventory",
            "number",
            2,
            "Stockout ceiling per 28 days",
            "Item-lines counted at zero on confirmed counts, normalised to a "
            "28-day rate. At or under the ceiling scores 100.",
            minimum=0,
            outlet_overridable=True,
        ),
        SettingDef(
            "inventory.max.variance",
            "inventory",
            "number",
            0.2,
            "Theoretical variance ceiling",
            "Spec section 6: a count deviating more than 20% from what the "
            "recipes imply is worth attention. Median gap at or under this "
            "scores 100.",
            minimum=0,
            maximum=1,
            outlet_overridable=True,
        ),
        SettingDef(
            "inventory.weight.requisition_accuracy",
            "inventory",
            "number",
            0.4,
            "Requisition accuracy weight",
            "Share of the inventory pillar carried by requisition accuracy. "
            "Weights renormalise over whatever is measured in the period.",
            minimum=0,
            maximum=1,
        ),
        SettingDef(
            "inventory.weight.stockouts",
            "inventory",
            "number",
            0.3,
            "Stockout weight",
            "Share of the inventory pillar carried by stockout incidents.",
            minimum=0,
            maximum=1,
        ),
        SettingDef(
            "inventory.weight.variance",
            "inventory",
            "number",
            0.3,
            "Theoretical variance weight",
            "Share of the inventory pillar carried by theoretical-vs-actual "
            "variance. Weights renormalise over whatever is measured. The "
            "P15 defaults were re-cut here (P17) before the pillar ever "
            "produced a live number, so no history moved.",
            minimum=0,
            maximum=1,
        ),
        # --- Guest & throughput pillar (spec section 5 / P15) --------------
        SettingDef(
            "guest.target.repeat_rate",
            "guest",
            "number",
            0.20,
            "Repeat-customer rate target",
            "Green when at least this share of phone-identified customers "
            "were seen on two or more trading days. Live baseline at build "
            "time: 9%.",
            minimum=0,
            maximum=1,
            outlet_overridable=True,
        ),
        SettingDef(
            "guest.weight.repeat",
            "guest",
            "number",
            1.0,
            "Repeat rate weight",
            "Share of the guest pillar carried by the repeat rate — currently "
            "all of it, re-cut when table turns or a ratings source arrive.",
            minimum=0,
            maximum=1,
        ),
        # --- Integrity thresholds (spec section 4.2) -----------------------
        SettingDef(
            "integrity.phash_max_distance",
            "integrity",
            "integer",
            5,
            "Duplicate photo distance",
            "Maximum pHash Hamming distance treated as a re-used photo. Lower is stricter.",
            minimum=0,
            maximum=32,
        ),
        SettingDef(
            "integrity.phash_lookback_days",
            "integrity",
            "integer",
            30,
            "Duplicate photo lookback",
            "How many days of past photos a new upload is compared against.",
            minimum=1,
            maximum=365,
        ),
        SettingDef(
            "integrity.burst_window_minutes",
            "integrity",
            "integer",
            3,
            "Burst upload window",
            "A run where most photos land within this many minutes of submission "
            "is flagged as batch-faked.",
            minimum=1,
            maximum=60,
        ),
        SettingDef(
            "integrity.burst_share",
            "integrity",
            "number",
            0.8,
            "Burst upload share",
            "Fraction of a run's photos inside the window that triggers the flag.",
            minimum=0.1,
            maximum=1,
        ),
        SettingDef(
            "integrity.default_geofence_m",
            "integrity",
            "integer",
            150,
            "Default geofence radius (m)",
            "Fallback radius for outlets without their own. Each outlet's own "
            "radius is set on the outlet itself.",
            minimum=10,
            maximum=5000,
        ),
        SettingDef(
            "integrity.photo_max_bytes",
            "integrity",
            "integer",
            5 * 1024 * 1024,
            "Photo size cap (bytes)",
            "Uploads larger than this are refused before they reach storage.",
            minimum=100 * 1024,
            maximum=25 * 1024 * 1024,
        ),
        SettingDef(
            "integrity.photo_max_edge_px",
            "integrity",
            "integer",
            1600,
            "Photo resize edge (px)",
            "Client resizes the longest edge to this before upload.",
            minimum=480,
            maximum=4096,
        ),
        # --- AI photo review (docs/DECISIONS.md D6) ------------------------
        SettingDef(
            "ai_review.enabled",
            "ai_review",
            "boolean",
            False,
            "AI photo review",
            "Compare submitted photos against the outlet's reference standards. "
            "Advisory only: it never blocks a submission and never approves a run. "
            "Off until reference photos are captured.",
            outlet_overridable=True,
        ),
        SettingDef(
            "ai_review.uncertain_below_confidence",
            "ai_review",
            "number",
            0.7,
            "Uncertainty threshold",
            "Verdicts with confidence below this display as uncertain rather than pass or fail.",
            minimum=0,
            maximum=1,
        ),
        SettingDef(
            "ai_review.min_luminance",
            "ai_review",
            "number",
            40,
            "Minimum luminance",
            "Mean luminance (0-255) below which a photo is flagged too_dark. "
            "A deterministic check, not an AI judgement.",
            minimum=0,
            maximum=255,
        ),
        SettingDef(
            "jobs.digest_narrative",
            "jobs",
            "boolean",
            True,
            "Narrated digest",
            "One model-written opening paragraph on the daily digest, built "
            "only from numbers the code computed. Advisory: when the model is "
            "unavailable the digest sends without it.",
            outlet_overridable=True,
        ),
        SettingDef(
            "jobs.anomalies_time",
            "jobs",
            "time",
            "05:45",
            "Stock anomalies run time",
            "When the nightly consumption-and-anomalies pass runs. After the "
            "05:00 materialisation, when yesterday's trading day is closed.",
        ),
        SettingDef(
            "anomaly.unchanged_streak",
            "jobs",
            "number",
            3,
            "Unchanged-count streak",
            "The same non-zero on-hand figure on this many consecutive counts "
            "flags the item as probably not being counted.",
            minimum=2,
            outlet_overridable=True,
        ),
        SettingDef(
            "anomaly.padding_min_lines",
            "jobs",
            "number",
            3,
            "Padding check minimum lines",
            "Fewer finalised requisition lines than this and the consistency "
            "check stays silent — a share of two points is noise.",
            minimum=1,
            outlet_overridable=True,
        ),
        SettingDef(
            "anomaly.padding_share",
            "jobs",
            "number",
            0.5,
            "Padding share threshold",
            "Flag when at least this share of recent requisition lines "
            "carried the padding flag (asked > 1.3x the par gap).",
            minimum=0,
            maximum=1,
            outlet_overridable=True,
        ),
        SettingDef(
            "anomaly.min_windows",
            "jobs",
            "number",
            5,
            "Consumption z-score minimum windows",
            "Fewer consumption windows than this and the per-cover jump check "
            "stays silent. A z-score against four points is a coin toss.",
            minimum=3,
            outlet_overridable=True,
        ),
        SettingDef(
            "anomaly.z_threshold",
            "jobs",
            "number",
            2.5,
            "Consumption jump z threshold",
            "Spec section 6: flag |z| above this for consumption per cover "
            "against the item's own trailing distribution.",
            minimum=1,
            outlet_overridable=True,
        ),
        # --- Forecasting baseline (spec section 5.1 / P16) -----------------
        SettingDef(
            "jobs.forecast_time",
            "jobs",
            "time",
            "05:30",
            "Forecast run time",
            "When the nightly forecast pass runs. After the 05:00 "
            "materialisation, so yesterday's closed trading day is history.",
        ),
        SettingDef(
            "forecast.horizon_days",
            "sales",
            "integer",
            7,
            "Forecast horizon",
            "How many days ahead the nightly pass forecasts and stores.",
            minimum=1,
            maximum=14,
        ),
        SettingDef(
            "forecast.trend_clamp_min",
            "sales",
            "number",
            0.8,
            "Trend clamp floor",
            "Spec 5.1: the 14-day trend factor never drops below this.",
            minimum=0.1,
            maximum=1,
        ),
        SettingDef(
            "forecast.trend_clamp_max",
            "sales",
            "number",
            1.3,
            "Trend clamp ceiling",
            "Spec 5.1: the 14-day trend factor never rises above this.",
            minimum=1,
            maximum=3,
        ),
        # --- Which restaurant an export is allowed to name -----------------
        SettingDef(
            "sales.petpooja_restaurant_name",
            "sales",
            "string",
            "",
            "Expected Petpooja restaurant name",
            "The name in a Petpooja export's 'Restaurant Name:' preamble. An "
            "upload naming anything else is refused. Leave empty to accept any "
            "name — the guard is off until this is set, and the uploads list "
            "shows what each accepted file actually claimed, which is where to "
            "read the exact string to put here.",
            outlet_overridable=True,
        ),
        # --- Sales pillar targets (spec section 5 / P12) -------------------
        # Baselines from AKIRA's actual 17 Jul - 25 Aug trading data, seeded
        # as the spec's Stage-2 target bands and reviewed monthly. All
        # effective-dated: re-opening last month scores against the targets
        # that were live last month (D9), same as the SOP weights.
        SettingDef(
            "sales.target.net_per_day_paise",
            "sales",
            "number",
            18_000_00,
            "Daily net sales target",
            "Green at or above this net per trading day, in paise. "
            "Spec baseline: current Rs 12,791/day, target Rs 18,000.",
            minimum=0,
            outlet_overridable=True,
        ),
        SettingDef(
            "sales.amber.net_per_day_paise",
            "sales",
            "number",
            13_000_00,
            "Daily net sales amber floor",
            "Below this the component band is red. Rs 13,000 in paise.",
            minimum=0,
            outlet_overridable=True,
        ),
        SettingDef(
            "sales.target.orders_per_day",
            "sales",
            "number",
            20,
            "Orders per trading day target",
            "Green at or above. Spec baseline: ~15/day currently.",
            minimum=0,
            outlet_overridable=True,
        ),
        SettingDef(
            "sales.target.aov_paise",
            "sales",
            "number",
            1_150_00,
            "Average order value target",
            "Green at or above, in paise. AOV is stable at Rs 1,075 - growth "
            "comes from bill count, which is why its weight is low.",
            minimum=0,
            outlet_overridable=True,
        ),
        SettingDef(
            "sales.target.monwed_share",
            "sales",
            "number",
            0.45,
            "Mon-Wed share of sales target",
            "The known weak flank: green when Mon-Wed carries at least this "
            "share of the week's net. Currently ~40%.",
            minimum=0,
            maximum=1,
            outlet_overridable=True,
        ),
        SettingDef(
            "sales.target.phone_capture",
            "sales",
            "number",
            0.80,
            "Phone capture rate target",
            "Share of bills carrying a customer phone. The stated growth target; currently 31%.",
            minimum=0,
            maximum=1,
            outlet_overridable=True,
        ),
        SettingDef(
            "sales.max.discount_share",
            "sales",
            "number",
            0.03,
            "Discount ceiling",
            "Discounts as a share of gross should stay under this. Currently 0.8%.",
            minimum=0,
            maximum=1,
            outlet_overridable=True,
        ),
        # Component weights inside the pillar. Sum to 1. AOV deliberately low
        # (spec: growth is bill count, not ticket size); discount is a small
        # hygiene weight.
        SettingDef(
            "sales.weight.net",
            "sales",
            "number",
            0.35,
            "Weight: net sales/day",
            "Share of the sales pillar carried by daily net vs target.",
            minimum=0,
            maximum=1,
        ),
        SettingDef(
            "sales.weight.orders",
            "sales",
            "number",
            0.25,
            "Weight: orders/day",
            "Share carried by order count vs target.",
            minimum=0,
            maximum=1,
        ),
        SettingDef(
            "sales.weight.monwed",
            "sales",
            "number",
            0.15,
            "Weight: Mon-Wed share",
            "Share carried by the weekday-sales flank.",
            minimum=0,
            maximum=1,
        ),
        SettingDef(
            "sales.weight.phone",
            "sales",
            "number",
            0.15,
            "Weight: phone capture",
            "Share carried by the capture rate.",
            minimum=0,
            maximum=1,
        ),
        SettingDef(
            "sales.weight.aov",
            "sales",
            "number",
            0.05,
            "Weight: AOV",
            "Deliberately low - see the AOV target's description.",
            minimum=0,
            maximum=1,
        ),
        SettingDef(
            "sales.weight.discount",
            "sales",
            "number",
            0.05,
            "Weight: discount control",
            "Small hygiene weight for staying under the discount ceiling.",
            minimum=0,
            maximum=1,
        ),
        # --- Jobs and notifications (spec section 4.1 / P7) ----------------
        SettingDef(
            "jobs.materialise_time",
            "jobs",
            "time",
            "05:00",
            "Run materialisation time",
            "Local time the day's checklist runs are created. Keep at or after "
            "the 05:00 business-date rollover, or runs land on the wrong day.",
        ),
        SettingDef(
            "jobs.digest_time",
            "jobs",
            "time",
            "09:00",
            "Daily digest time",
            "Local time the daily digest email is sent.",
        ),
        SettingDef(
            "jobs.missed_check_minutes",
            "jobs",
            "integer",
            15,
            "Missed-run check interval",
            "How often pending runs past grace are marked missed, in minutes.",
            minimum=5,
            maximum=120,
        ),
        SettingDef(
            "jobs.digest_spot_check_share",
            "jobs",
            "number",
            0.1,
            "Owner spot-check sample",
            "Share of approved runs flagged in the digest for owner review.",
            minimum=0,
            maximum=1,
        ),
        SettingDef(
            "notifications.channel",
            "jobs",
            "string",
            "email",
            "Notification channel",
            "How alerts and digests are delivered. WhatsApp arrives in Stage 2.",
            choices=("email", "log_only"),
        ),
    ]
}


def validate_value(definition: SettingDef, value: Any) -> str | None:
    """A plain-language problem with the value, or None if it is acceptable."""
    if definition.type == "boolean":
        if not isinstance(value, bool):
            return "Expected true or false."
        return None
    if definition.type in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, int | float):
            return "Expected a number."
        if definition.type == "integer" and int(value) != value:
            return "Expected a whole number."
        if definition.minimum is not None and value < definition.minimum:
            return f"Must be at least {definition.minimum}."
        if definition.maximum is not None and value > definition.maximum:
            return f"Must be at most {definition.maximum}."
        return None
    if definition.type == "time":
        if not isinstance(value, str):
            return "Expected a time like 05:00."
        parts = value.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            return "Expected a time like 05:00."
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return "That is not a valid time of day."
        return None
    # string
    if not isinstance(value, str):
        return "Expected text."
    if definition.choices and value not in definition.choices:
        return f"Must be one of: {', '.join(definition.choices)}."
    return None
