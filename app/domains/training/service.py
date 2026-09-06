"""The training walkthrough: what the database remembers about it (D31).

The content of the tour lives in the web app (its steps point at buttons);
this module keeps the record. The rules are here rather than in the client,
because the client is the thing being distrusted:

- A person's track follows their role. Managers walk the management shell,
  floor staff the floor shell. The client cannot ask to be recorded on the
  other one.
- Only the owner may skip. For everyone else the only way out of "required"
  is `complete`.
- A completion stays valid across content versions. Only a restart makes the
  tour required again; the restart supersedes every earlier attempt on that
  track, and the next attempt remembers who asked.
- Restarting is the owner's, delegable per manager (`profiles.
  can_restart_training`): a delegated ops manager restarts anyone, a
  delegated outlet manager restarts people at their own outlets.

Identity is whoever is acting: a manager's own login, or the PIN-identified
person on a shared tablet. The router resolves that and passes a Trainee in.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import record as audit
from app.core.deps import CurrentUser
from app.core.enums import AuditAction, UserRole
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.domains.training.schemas import (
    Language,
    PersonTraining,
    Status,
    Track,
    TrainingRecord,
    TrainingStatus,
)

MANAGEMENT_ROLES = frozenset({UserRole.OWNER, UserRole.OPS_MANAGER, UserRole.OUTLET_MANAGER})

#: Who may skip the tour. The owner, and nobody else - an ops manager is
#: exactly the kind of person the owner wants walked through.
SKIP_ROLES = frozenset({UserRole.OWNER})


@dataclass(frozen=True)
class Trainee:
    """The person the tour is for, however the request authenticated."""

    profile_id: uuid.UUID
    full_name: str
    role: UserRole
    #: The shared tablet it ran on, when it did.
    device_id: uuid.UUID | None


def track_for(role: UserRole) -> Track:
    return "management" if role in MANAGEMENT_ROLES else "floor"


def can_skip(role: UserRole) -> bool:
    return role in SKIP_ROLES


def can_reset(
    *,
    actor_role: UserRole,
    actor_delegated: bool,
    actor_outlets: set[uuid.UUID],
    target_outlets: set[uuid.UUID],
) -> bool:
    """May this person restart that person's training?

    Owner: always. A manager with the owner's delegation: an ops manager
    anywhere, an outlet manager only for someone who works at one of their
    outlets. Everybody else: no. Pure, so the table of cases is testable.
    """
    if actor_role is UserRole.OWNER:
        return True
    if not actor_delegated or actor_role not in MANAGEMENT_ROLES:
        return False
    if actor_role is UserRole.OPS_MANAGER:
        return True
    return bool(actor_outlets & target_outlets)


def _status_of(row: dict[str, Any] | None) -> Status:
    if row is None:
        return "not_started"
    if row["completed_at"] is not None:
        return "completed"
    if row["skipped_at"] is not None:
        return "skipped"
    if row["superseded_at"] is not None:
        return "reset"
    return "in_progress" if row["last_step"] > 0 else "not_started"


def _to_record(row: dict[str, Any]) -> TrainingRecord:
    return TrainingRecord(
        id=row["id"],
        profile_id=row["profile_id"],
        track=row["track"],
        version=row["version"],
        language=row["language"],
        total_steps=row["total_steps"],
        last_step=row["last_step"],
        status=_status_of(row),
        started_at=row["started_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
        skipped_at=row["skipped_at"],
        triggered_by=row["triggered_by"],
        triggered_by_name=row.get("triggered_by_name"),
    )


_RECORD_COLUMNS = """
    r.id, r.profile_id, r.track, r.version, r.language, r.total_steps, r.last_step,
    r.steps, r.device_id, r.triggered_by, t.full_name as triggered_by_name,
    r.started_at, r.updated_at, r.completed_at, r.skipped_at,
    r.superseded_at, r.superseded_by
"""

_LATEST_LIVE = text(
    f"""
    select {_RECORD_COLUMNS}
      from training_records r
      left join profiles t on t.id = r.triggered_by
     where r.profile_id = :profile_id and r.track = :track
       and r.superseded_at is null
     order by r.started_at desc
     limit 1
    """
)

#: The status read, as one round trip: the newest live attempt plus whether
#: any live attempt on the track is finished. Two facts, one wire crossing.
_STATUS = text(
    f"""
    select {_RECORD_COLUMNS},
           exists (
               select 1 from training_records d
                where d.profile_id = :profile_id and d.track = :track
                  and d.superseded_at is null
                  and (d.completed_at is not null or d.skipped_at is not null)
           ) as done
      from training_records r
      left join profiles t on t.id = r.triggered_by
     where r.profile_id = :profile_id and r.track = :track
       and r.superseded_at is null
     order by r.started_at desc
     limit 1
    """
)

_LATEST_SUPERSEDED = text(
    """
    select superseded_at, superseded_by
      from training_records
     where profile_id = :profile_id and track = :track and superseded_at is not null
     order by superseded_at desc
     limit 1
    """
)

_BY_ID = text(
    f"""
    select {_RECORD_COLUMNS}
      from training_records r
      left join profiles t on t.id = r.triggered_by
     where r.id = :id
    """
)


async def _latest_live(
    db: AsyncSession, profile_id: uuid.UUID, track: Track
) -> dict[str, Any] | None:
    found = (
        (await db.execute(_LATEST_LIVE, {"profile_id": profile_id, "track": track}))
        .mappings()
        .first()
    )
    return dict(found) if found is not None else None


async def status(db: AsyncSession, who: Trainee, *, version: str) -> TrainingStatus:
    track = track_for(who.role)
    row = (
        (await db.execute(_STATUS, {"profile_id": who.profile_id, "track": track}))
        .mappings()
        .first()
    )
    latest = dict(row) if row is not None else None
    done = bool(latest["done"]) if latest is not None else False
    open_here = (
        latest
        if latest is not None
        and latest["version"] == version
        and latest["completed_at"] is None
        and latest["skipped_at"] is None
        else None
    )
    return TrainingStatus(
        profile_id=who.profile_id,
        full_name=who.full_name,
        role=who.role,
        track=track,
        version=version,
        required=not done,
        can_skip=can_skip(who.role),
        record=_to_record(open_here) if open_here is not None else None,
    )


async def start(
    db: AsyncSession,
    who: Trainee,
    *,
    version: str,
    total_steps: int,
    language: Language,
) -> TrainingRecord:
    """Begin an attempt, or return the one already open at this version.

    Idempotent on purpose: the tablet reloads, the app reopens, the same
    attempt continues rather than a second row appearing. Changing language
    mid-attempt is a client-side toggle; the record keeps the first choice.
    """
    track = track_for(who.role)
    latest = await _latest_live(db, who.profile_id, track)
    if (
        latest is not None
        and latest["version"] == version
        and latest["completed_at"] is None
        and latest["skipped_at"] is None
    ):
        return _to_record(latest)

    # A restart remembers who asked for it: the most recent supersession
    # that happened after whatever attempt is currently live.
    triggered_by: uuid.UUID | None = None
    superseded = (
        (await db.execute(_LATEST_SUPERSEDED, {"profile_id": who.profile_id, "track": track}))
        .mappings()
        .first()
    )
    if superseded is not None and (
        latest is None or superseded["superseded_at"] > latest["started_at"]
    ):
        triggered_by = superseded["superseded_by"]

    new_id = await db.scalar(
        text(
            """
            insert into training_records
                (profile_id, track, version, language, total_steps, device_id, triggered_by)
            values
                (:profile_id, :track, :version, :language, :total_steps, :device_id, :triggered_by)
            returning id
            """
        ),
        {
            "profile_id": who.profile_id,
            "track": track,
            "version": version,
            "language": language,
            "total_steps": total_steps,
            "device_id": who.device_id,
            "triggered_by": triggered_by,
        },
    )
    await db.commit()
    return await _get(db, uuid.UUID(str(new_id)))


async def _get(db: AsyncSession, record_id: uuid.UUID) -> TrainingRecord:
    row = (await db.execute(_BY_ID, {"id": record_id})).mappings().first()
    if row is None:
        raise NotFoundError("That training attempt does not exist.")
    return _to_record(dict(row))


async def _own_open(db: AsyncSession, who: Trainee, record_id: uuid.UUID) -> dict[str, Any]:
    row = (await db.execute(_BY_ID, {"id": record_id})).mappings().first()
    # Somebody else's attempt is "not found", not "forbidden": the id space
    # tells an outsider nothing either way.
    if row is None or row["profile_id"] != who.profile_id:
        raise NotFoundError("That training attempt does not exist.")
    if row["completed_at"] or row["skipped_at"] or row["superseded_at"]:
        raise ConflictError("That training attempt is already closed.")
    return dict(row)


_ADVANCE = text(
    f"""
    with moved as (
        update training_records
           set last_step = greatest(last_step, :step),
               steps = steps || cast(:event as jsonb),
               updated_at = now()
         where id = :id and profile_id = :profile_id
           and completed_at is null and skipped_at is null and superseded_at is null
           and :step <= total_steps
        returning *
    )
    select {_RECORD_COLUMNS}
      from moved r
      left join profiles t on t.id = r.triggered_by
    """
)


async def advance(
    db: AsyncSession, who: Trainee, *, record_id: uuid.UUID, step: int
) -> TrainingRecord:
    """The hot path of the tour - once per step - so it is one statement.

    The ownership, open-attempt and range guards are the UPDATE's WHERE; a
    miss returns no row, and only then is a second read spent working out
    which guard it was, so the person gets the right message.
    """
    event = {"step": step, "at": datetime.now(UTC).isoformat()}
    row = (
        (
            await db.execute(
                _ADVANCE,
                {
                    "id": record_id,
                    "profile_id": who.profile_id,
                    "step": step,
                    "event": json.dumps(event),
                },
            )
        )
        .mappings()
        .first()
    )
    if row is None:
        current = await _own_open(db, who, record_id)  # raises not-found / closed
        raise ConflictError(
            f"Step {step} is past the end of a {current['total_steps']}-step tour.",
            extra={"total_steps": current["total_steps"]},
        )
    await db.commit()
    return _to_record(dict(row))


async def complete(
    db: AsyncSession,
    who: Trainee,
    *,
    record_id: uuid.UUID,
    ip: str | None = None,
    user_agent: str | None = None,
) -> TrainingRecord:
    row = await _own_open(db, who, record_id)
    await db.execute(
        text(
            """
            update training_records
               set completed_at = now(), last_step = total_steps, updated_at = now()
             where id = :id
            """
        ),
        {"id": record_id},
    )
    await audit(
        db,
        actor_profile_id=who.profile_id,
        entity_table="training_records",
        entity_id=record_id,
        action=AuditAction.UPDATE,
        before={"status": _status_of(row), "last_step": row["last_step"]},
        after={"status": "completed", "track": row["track"], "version": row["version"]},
        ip=ip,
        user_agent=user_agent,
    )
    await db.commit()
    return await _get(db, record_id)


async def skip(
    db: AsyncSession,
    who: Trainee,
    *,
    record_id: uuid.UUID,
    ip: str | None = None,
    user_agent: str | None = None,
) -> TrainingRecord:
    if not can_skip(who.role):
        raise ForbiddenError("Only the owner may skip the walkthrough.")
    row = await _own_open(db, who, record_id)
    await db.execute(
        text("update training_records set skipped_at = now(), updated_at = now() where id = :id"),
        {"id": record_id},
    )
    await audit(
        db,
        actor_profile_id=who.profile_id,
        entity_table="training_records",
        entity_id=record_id,
        action=AuditAction.UPDATE,
        before={"status": _status_of(row), "last_step": row["last_step"]},
        after={"status": "skipped", "track": row["track"], "version": row["version"]},
        ip=ip,
        user_agent=user_agent,
    )
    await db.commit()
    return await _get(db, record_id)


# --- The owner's view ------------------------------------------------------------

_TRACK_CASE = """
    case when p.global_role in ('owner', 'ops_manager', 'outlet_manager')
         then 'management' else 'floor' end
"""

_PEOPLE = text(
    f"""
    with latest as (
        select distinct on (r.profile_id, r.track)
               r.profile_id, r.track, r.version, r.language, r.total_steps, r.last_step,
               r.started_at, r.completed_at, r.skipped_at, r.triggered_by
          from training_records r
         where r.superseded_at is null
         order by r.profile_id, r.track, r.started_at desc
    ),
    done as (
        select profile_id, track, max(coalesce(completed_at, skipped_at)) as done_at,
               bool_or(completed_at is not null) as completed,
               bool_or(skipped_at is not null) as skipped
          from training_records
         where superseded_at is null
           and (completed_at is not null or skipped_at is not null)
         group by profile_id, track
    ),
    resets as (
        select distinct on (profile_id, track)
               profile_id, track, superseded_at as reset_at, superseded_by
          from training_records
         where superseded_at is not null
         order by profile_id, track, superseded_at desc
    )
    select p.id as profile_id, p.full_name, p.global_role, p.is_active,
           l.version, l.language, l.total_steps, l.last_step, l.started_at,
           l.completed_at, l.skipped_at,
           d.done_at, d.completed as ever_completed, d.skipped as ever_skipped,
           t.full_name as triggered_by_name,
           rs.reset_at,
           rb.full_name as reset_by_name,
           coalesce(mo.outlet_ids, '{{}}') as outlet_ids,
           coalesce(
               (select c.can_restart_training from profiles c where c.id = :caller), false
           ) as caller_delegated
      from profiles p
      left join lateral (
           select * from latest
            where latest.profile_id = p.id and latest.track = {_TRACK_CASE}
      ) l on true
      left join lateral (
           select * from done
            where done.profile_id = p.id and done.track = {_TRACK_CASE}
      ) d on true
      left join profiles t on t.id = l.triggered_by
      left join lateral (
           select reset_at, superseded_by from resets
            where resets.profile_id = p.id and resets.track = {_TRACK_CASE}
      ) rs on true
      left join profiles rb on rb.id = rs.superseded_by
      left join lateral (
           select array_agg(om.outlet_id) as outlet_ids
             from outlet_members om
            where om.profile_id = p.id and om.deleted_at is null
      ) mo on true
     where p.deleted_at is null
       and (cast(:org as uuid) is null or p.organisation_id = cast(:org as uuid))
       and (:all_outlets or exists (
              select 1 from outlet_members om
               where om.profile_id = p.id and om.deleted_at is null
                 and om.outlet_id = any(:outlet_ids)))
     order by p.full_name
    """
)


async def _delegated(db: AsyncSession, profile_id: uuid.UUID) -> bool:
    return bool(
        await db.scalar(
            text("select can_restart_training from profiles where id = :id"), {"id": profile_id}
        )
    )


async def people(db: AsyncSession, user: CurrentUser) -> list[PersonTraining]:
    """Everyone the caller may administer, with where their training stands
    and whether this caller may restart it."""
    rows = (
        await db.execute(
            _PEOPLE,
            {
                "all_outlets": user.is_global or user.is_platform_admin,
                "outlet_ids": list(user.outlet_ids),
                "caller": user.profile_id,
                "org": user.organisation_id,
            },
        )
    ).mappings()
    out: list[PersonTraining] = []
    for r in rows:
        role = UserRole(r["global_role"])
        delegated = bool(r["caller_delegated"])
        st: Status
        if r["ever_completed"]:
            st = "completed"
        elif r["ever_skipped"]:
            st = "skipped"
        elif r["version"] is not None and (r["last_step"] or 0) > 0:
            st = "in_progress"
        elif r["reset_at"] is not None:
            st = "reset"
        else:
            st = "not_started"
        out.append(
            PersonTraining(
                profile_id=r["profile_id"],
                full_name=r["full_name"],
                global_role=role,
                is_active=r["is_active"],
                track=track_for(role),
                status=st,
                version=r["version"],
                language=r["language"],
                last_step=r["last_step"] or 0,
                total_steps=r["total_steps"],
                started_at=r["started_at"],
                # A voluntary re-run leaves a newer open attempt; the date the
                # owner cares about is the completion that still counts.
                completed_at=r["completed_at"] or (r["done_at"] if r["ever_completed"] else None),
                skipped_at=r["skipped_at"]
                or (r["done_at"] if r["ever_skipped"] and not r["ever_completed"] else None),
                triggered_by_name=r["triggered_by_name"],
                reset_at=r["reset_at"],
                reset_by_name=r["reset_by_name"],
                can_reset=can_reset(
                    actor_role=user.global_role,
                    actor_delegated=delegated,
                    actor_outlets=user.outlet_ids,
                    target_outlets={uuid.UUID(str(o)) for o in (r["outlet_ids"] or [])},
                ),
            )
        )
    return out


async def reset(
    db: AsyncSession,
    user: CurrentUser,
    profile_id: uuid.UUID,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
) -> PersonTraining:
    """Restart somebody's training. Every earlier attempt on their track is
    superseded; the next attempt they begin records that this person asked."""
    target = (
        (
            await db.execute(
                text(
                    "select id, full_name, global_role from profiles"
                    " where id = :id and deleted_at is null"
                ),
                {"id": profile_id},
            )
        )
        .mappings()
        .first()
    )
    if target is None:
        raise NotFoundError("That person does not exist.")
    target_outlets = {
        r[0]
        for r in await db.execute(
            text(
                "select outlet_id from outlet_members where profile_id = :id and deleted_at is null"
            ),
            {"id": profile_id},
        )
    }
    if not can_reset(
        actor_role=user.global_role,
        actor_delegated=await _delegated(db, user.profile_id),
        actor_outlets=user.outlet_ids,
        target_outlets=target_outlets,
    ):
        raise ForbiddenError(
            "Restarting training is the owner's, unless the owner has delegated it to you "
            "for people at your outlets."
        )

    track = track_for(UserRole(target["global_role"]))
    before = await _latest_live(db, profile_id, track)
    superseded_ids = list(
        await db.scalars(
            text(
                """
                update training_records
                   set superseded_at = now(), superseded_by = :admin, updated_at = now()
                 where profile_id = :profile_id and track = :track and superseded_at is null
                returning id
                """
            ),
            {"admin": user.profile_id, "profile_id": profile_id, "track": track},
        )
    )
    if not superseded_ids:
        # Nothing to supersede - they had never started. Record the request
        # anyway, as a superseded placeholder, so "who asked" survives.
        await db.execute(
            text(
                """
                insert into training_records
                    (profile_id, track, version, total_steps, superseded_at, superseded_by)
                values (:profile_id, :track, 'none', 1, now(), :admin)
                """
            ),
            {"admin": user.profile_id, "profile_id": profile_id, "track": track},
        )
    await audit(
        db,
        actor_profile_id=user.profile_id,
        entity_table="training_records",
        entity_id=profile_id,
        action=AuditAction.UPDATE,
        before={"status": _status_of(before), "track": track},
        after={"status": "reset", "track": track},
        ip=ip,
        user_agent=user_agent,
    )
    await db.commit()
    for person in await people(db, user):
        if person.profile_id == profile_id:
            return person
    raise NotFoundError("That person is not in your outlets.")
