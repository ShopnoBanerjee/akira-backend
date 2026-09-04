"""Admin-editable settings.

app_settings is append-only with an effective date (migration 0010): a change
inserts a new row, and the value in force at any moment is the newest row at or
before it, with an outlet override beating the global value. That is what keeps
historical scores reproducible when a weight is nudged.

The registry in app/core/settings_registry.py owns each key's meaning; this
router owns reading and writing the rows.
"""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text

from app.core.audit import record
from app.core.deps import CurrentUserDep, DbDep, require_admin
from app.core.enums import AuditAction
from app.core.errors import ForbiddenError, NotFoundError, ValidationError
from app.core.settings_registry import REGISTRY, SettingDef, validate_value
from app.core.settings_value import decode_stored

router = APIRouter(prefix="/settings", tags=["settings"])


class SettingView(BaseModel):
    """One setting as the admin screen shows it: definition plus current value."""

    key: str
    group: str
    type: str
    label: str
    description: str
    outlet_overridable: bool
    minimum: float | None
    maximum: float | None
    choices: list[str]
    default: Any
    #: The value in force right now (global scope), or the default if never set.
    value: Any
    #: True when the value comes from an app_settings row rather than the default.
    is_set: bool


class SettingHistoryRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    scope: str
    outlet_id: uuid.UUID | None
    value: Any
    effective_from: datetime
    note: str | None
    set_by_name: str | None
    created_at: datetime


class SetSettingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: Any
    #: Why it changed. Shown in the history, so future readers understand.
    note: str | None = Field(default=None, max_length=500)
    #: Set to stage a change for the future or backdate a correction.
    #: Defaults to now.
    effective_from: datetime | None = None
    #: For an outlet-scoped override; only valid on overridable keys.
    outlet_id: uuid.UUID | None = None


def _definition(key: str) -> SettingDef:
    definition = REGISTRY.get(key)
    if definition is None:
        raise NotFoundError(
            f"{key} is not a known setting. Settings are declared in code; "
            "an unknown key cannot be set."
        )
    return definition


@router.get(
    "",
    response_model=list[SettingView],
    dependencies=[Depends(require_admin)],
    summary="Every setting with its current value",
)
async def list_settings(db: DbDep) -> list[SettingView]:
    """The full registry, each key resolved to the value in force right now at
    global scope. Grouped client-side by `group`."""
    rows = (
        await db.execute(
            text(
                """
                select distinct on (key) key, value
                  from app_settings
                 where scope = 'global' and effective_from <= now()
                 order by key, effective_from desc
                """
            )
        )
    ).mappings()
    current: dict[str, Any] = {r["key"]: r["value"] for r in rows}

    views: list[SettingView] = []
    for definition in REGISTRY.values():
        raw = current.get(definition.key)
        # One decoder, shared with the resolver. This had its own copy, and
        # the copy raised on any text value — so saving a job time or a
        # restaurant name took the whole settings screen down with a 500,
        # for every key, not just the one that was set.
        value = decode_stored(raw, definition) if raw is not None else None
        views.append(
            SettingView(
                key=definition.key,
                group=definition.group,
                type=definition.type,
                label=definition.label,
                description=definition.description,
                outlet_overridable=definition.outlet_overridable,
                minimum=definition.minimum,
                maximum=definition.maximum,
                choices=list(definition.choices),
                default=definition.default,
                value=definition.default if raw is None else value,
                is_set=raw is not None,
            )
        )
    return views


@router.get(
    "/{key}/history",
    response_model=list[SettingHistoryRow],
    dependencies=[Depends(require_admin)],
    summary="Every value this setting has held",
)
async def setting_history(
    key: str,
    db: DbDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[SettingHistoryRow]:
    definition = _definition(key)

    rows = (
        await db.execute(
            text(
                """
                select s.id, s.scope, s.outlet_id, s.value, s.effective_from,
                       s.note, p.full_name as set_by_name, s.created_at
                  from app_settings s
                  left join profiles p on p.id = s.set_by
                 where s.key = :key
                 order by s.effective_from desc
                 limit :limit
                """
            ),
            {"key": key, "limit": limit},
        )
    ).mappings()
    return [
        SettingHistoryRow(
            id=r["id"],
            scope=r["scope"],
            outlet_id=r["outlet_id"],
            value=decode_stored(r["value"], definition),
            effective_from=r["effective_from"],
            note=r["note"],
            set_by_name=r["set_by_name"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


@router.put(
    "/{key}",
    response_model=SettingView,
    status_code=status.HTTP_200_OK,
    dependencies=[Depends(require_admin)],
    summary="Change a setting",
)
async def set_setting(
    key: str,
    payload: SetSettingRequest,
    request: Request,
    db: DbDep,
    user: CurrentUserDep,
) -> SettingView:
    """Inserts a new effective-dated row rather than editing the old one, so
    scoring a past period keeps using the values that were live then.

    Scoring weights and bands are owner-only; the operational groups
    (integrity, AI review, jobs) accept an operations manager too.
    """
    definition = _definition(key)

    if definition.group == "scoring" and user.global_role.value != "owner":
        raise ForbiddenError(
            "Scoring weights and bands change every outlet's score, so only an "
            "owner may change them.",
            extra={"your_role": user.global_role.value},
        )

    problem = validate_value(definition, payload.value)
    if problem is not None:
        raise ValidationError(
            f"{definition.label}: {problem}",
            extra={"key": key, "value": payload.value},
        )

    if payload.outlet_id is not None and not definition.outlet_overridable:
        raise ValidationError(
            f"{definition.label} is a network-wide setting and cannot be overridden per outlet."
        )

    import json

    scope = "outlet" if payload.outlet_id is not None else "global"
    await db.execute(
        text(
            """
            insert into app_settings (key, scope, outlet_id, value, effective_from, note, set_by)
            values (:key, cast(:scope as setting_scope), :outlet_id,
                    cast(:value as jsonb),
                    coalesce(cast(:effective_from as timestamptz), now()),
                    :note, :set_by)
            """
        ),
        {
            "key": key,
            "scope": scope,
            "outlet_id": payload.outlet_id,
            "value": json.dumps(payload.value),
            "effective_from": payload.effective_from,
            "note": payload.note,
            "set_by": user.profile_id,
        },
    )
    await record(
        db,
        actor_profile_id=user.profile_id,
        outlet_id=payload.outlet_id,
        entity_table="app_settings",
        entity_id=None,
        action=AuditAction.UPDATE,
        after={
            "key": key,
            "value": payload.value,
            "scope": scope,
            "note": payload.note,
        },
        ip=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    await db.commit()

    views = await list_settings(db)
    return next(v for v in views if v.key == key)
