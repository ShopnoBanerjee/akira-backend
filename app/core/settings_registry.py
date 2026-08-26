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
SettingGroup = Literal["scoring", "integrity", "ai_review", "jobs"]


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
