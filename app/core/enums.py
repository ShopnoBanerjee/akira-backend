"""Python mirrors of the Postgres enums.

Source of truth is ``supabase/migrations/0001_extensions_and_enums.sql`` (plus
0009 and 0010 for the later additions). These classes must match it value for
value; ``tests/test_migrations.py`` asserts that against a live database, so a
drift fails the build rather than surfacing as a mystery 500.

The frontend derives its own copies from the generated OpenAPI schema, so there
is no third hand-maintained list anywhere.

Adding a value means: a new migration, this file, then re-running
``scripts/export_openapi.py``.
"""

from enum import StrEnum

__all__ = [
    "AiVerdict",
    "AuditAction",
    "DayPart",
    "ExceptionStatus",
    "Frequency",
    "IntegrityFlag",
    "InventoryUnit",
    "ItemResult",
    "JobStatus",
    "RunStatus",
    "SalesChannel",
    "SettingScope",
    "Severity",
    "UploadStatus",
    "UserRole",
    "ValueType",
]


class UserRole(StrEnum):
    OWNER = "owner"
    OPS_MANAGER = "ops_manager"
    OUTLET_MANAGER = "outlet_manager"
    SHIFT_LEAD = "shift_lead"
    STAFF = "staff"
    #: Creates organisations and their owners; belongs to none (D33). Last,
    #: because Postgres appends enum values and the parity test reads order.
    PLATFORM_ADMIN = "platform_admin"

    @property
    def rank(self) -> int:
        """Higher outranks lower. Used to stop anyone assigning a role at or
        above their own."""
        return _ROLE_RANK[self]

    def outranks(self, other: "UserRole") -> bool:
        return self.rank > other.rank


_ROLE_RANK: dict[UserRole, int] = {
    UserRole.STAFF: 10,
    UserRole.SHIFT_LEAD: 20,
    UserRole.OUTLET_MANAGER: 30,
    UserRole.OPS_MANAGER: 40,
    UserRole.OWNER: 50,
    UserRole.PLATFORM_ADMIN: 60,
}

#: Roles that see every outlet without an explicit membership row.
GLOBAL_ROLES: frozenset[UserRole] = frozenset({UserRole.OWNER, UserRole.OPS_MANAGER})

#: Roles that may approve a submitted run. Never a shift lead or staff member,
#: and never the person who submitted it — see the separation-of-duties CHECK.
APPROVER_ROLES: frozenset[UserRole] = frozenset(
    {UserRole.OWNER, UserRole.OPS_MANAGER, UserRole.OUTLET_MANAGER}
)


class RunStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    MISSED = "missed"


#: An approved run is immutable. Further item edits return 409.
TERMINAL_RUN_STATUSES: frozenset[RunStatus] = frozenset({RunStatus.APPROVED, RunStatus.MISSED})


class ItemResult(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    NA = "na"
    PENDING = "pending"


class ValueType(StrEnum):
    NUMBER = "number"
    TEXT = "text"
    TEMPERATURE_C = "temperature_c"
    TIME = "time"


class Frequency(StrEnum):
    PER_SHIFT = "per_shift"
    DAILY = "daily"
    # Cadences AKIRA's real checklists use that a weekly cycle cannot express.
    ALTERNATE_DAY = "alternate_day"
    WEEKLY = "weekly"
    FORTNIGHTLY = "fortnightly"
    MONTHLY = "monthly"

    @property
    def interval_days(self) -> int | None:
        """Days between occurrences, for cadences not driven by weekday."""
        return {Frequency.ALTERNATE_DAY: 2, Frequency.FORTNIGHTLY: 14}.get(self)


class DayPart(StrEnum):
    OPENING = "opening"
    MID = "mid"
    CLOSING = "closing"
    ANY = "any"


class ExceptionStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"
    WAIVED = "waived"


class Severity(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SalesChannel(StrEnum):
    DINE_IN = "dine_in"
    PICKUP = "pickup"
    DELIVERY = "delivery"


class UploadStatus(StrEnum):
    RECEIVED = "received"
    PARSING = "parsing"
    PARSED = "parsed"
    FAILED = "failed"


class AuditAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    APPROVE = "approve"
    REJECT = "reject"
    LOGIN = "login"
    #: A platform admin looked inside an organisation (D33).
    READ = "read"


class AiVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    #: A first-class outcome. Forcing a model into a binary produces confident
    #: nonsense, which is worse than admitting doubt to the reviewing manager.
    UNCERTAIN = "uncertain"


class JobStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class InventoryUnit(StrEnum):
    PIECE = "piece"
    GRAM = "gram"
    KILOGRAM = "kilogram"
    MILLILITRE = "millilitre"
    LITRE = "litre"
    ROLL = "roll"
    PACKET = "packet"
    BOX = "box"
    BOTTLE = "bottle"
    JUG = "jug"


class SettingScope(StrEnum):
    #: Platform-wide: the scheduler's own times. Nothing a tenant owns.
    GLOBAL = "global"
    OUTLET = "outlet"
    #: One organisation; what "global" meant when there was one brand (D33).
    ORGANISATION = "organisation"


class IntegrityFlag(StrEnum):
    """Values stored in ``checklist_run_items.integrity_flags``.

    A text array rather than a Postgres enum, because flags are appended by
    several checks and the set grows faster than the schema should. Flags never
    block a submission: they surface on the manager's review screen and count
    against the outlet's integrity score. Blocking creates workarounds;
    visibility creates accountability.
    """

    DUPLICATE_PHOTO = "duplicate_photo"
    BURST_UPLOAD = "burst_upload"
    OUT_OF_GEOFENCE = "out_of_geofence"
    LATE = "late"
    STALE_CAPTURE = "stale_capture"
    #: Photo too dark to show what it claims to. Deterministic luminance check,
    #: not an AI judgement.
    TOO_DARK = "too_dark"
    #: The AI reviewer disagreed with the submitted result. Advisory only.
    AI_MISMATCH = "ai_mismatch"
