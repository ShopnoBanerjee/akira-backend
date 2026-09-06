"""SQL for the users domain. No business rules live here."""

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_SELECT_PROFILE = text(
    """
    select id, full_name, phone, employee_code, global_role, is_active,
           pin_hash is not null as has_pin, can_restart_training
      from profiles
     where id = :profile_id
       and deleted_at is null
    """
)

_UPDATE_PROFILE = text(
    """
    update profiles
       set full_name = coalesce(:full_name, full_name),
           phone     = coalesce(:phone, phone)
     where id = :profile_id
       and deleted_at is null
    returning id, full_name, phone, employee_code, global_role, is_active,
              pin_hash is not null as has_pin, can_restart_training
    """
)

#: How stale `last_seen_at` is allowed to get before a read refreshes it.
#: The column answers "is this person still around", which five minutes settles
#: as well as five seconds.
LAST_SEEN_STALE_MINUTES = 5

#: Reading your own profile and recording that you were here, in one statement.
#:
#: Two things were wrong with doing it in two. It cost a second round trip on
#: the first call after every sign-in, and it wrote a row to `profiles` on
#: *every* request that touched it — a dead tuple and an index update each
#: time, to move a timestamp by a few hundred milliseconds. The update now only
#: fires when the stored value has actually gone stale.
#:
#: The select reads the row as it was before the update, so `last_seen_at`
#: comes back as of the previous visit rather than this instant. That is the
#: more useful reading of the field, and it is what a separate select would
#: have returned anyway if it had run first.
_SELECT_PROFILE_AND_TOUCH = text(
    """
    with touched as (
        update profiles
           set last_seen_at = now()
         where id = :profile_id
           and deleted_at is null
           and (last_seen_at is null
                or last_seen_at < now() - make_interval(mins => :stale_minutes))
    )
    select id, full_name, phone, employee_code, global_role, is_active,
           pin_hash is not null as has_pin, can_restart_training
      from profiles
     where id = :profile_id
       and deleted_at is null
    """
)


async def get_profile(db: AsyncSession, profile_id: uuid.UUID) -> dict[str, Any] | None:
    row = (await db.execute(_SELECT_PROFILE, {"profile_id": profile_id})).mappings().first()
    return dict(row) if row else None


async def get_profile_and_touch(db: AsyncSession, profile_id: uuid.UUID) -> dict[str, Any] | None:
    """Your own profile, recording the visit in the same round trip."""
    row = (
        (
            await db.execute(
                _SELECT_PROFILE_AND_TOUCH,
                {"profile_id": profile_id, "stale_minutes": LAST_SEEN_STALE_MINUTES},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def update_profile(
    db: AsyncSession,
    profile_id: uuid.UUID,
    *,
    full_name: str | None,
    phone: str | None,
) -> dict[str, Any] | None:
    row = (
        (
            await db.execute(
                _UPDATE_PROFILE,
                {"profile_id": profile_id, "full_name": full_name, "phone": phone},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


_LIST_USERS = """
    select p.id as profile_id, p.full_name, p.phone, p.employee_code,
           p.global_role, p.is_active, p.last_seen_at,
           p.pin_hash is not null as has_pin, can_restart_training,
           p.organisation_id
      from profiles p
     where p.deleted_at is null
"""


async def list_users(
    db: AsyncSession,
    *,
    outlet_ids: list[uuid.UUID] | None,
    role: str | None,
    is_active: bool | None,
    search: str | None,
    organisation_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """outlet_ids None means every outlet of the organisation (owner and
    ops_manager); organisation_id None means every organisation (platform)."""
    clauses: list[str] = []
    params: dict[str, Any] = {}

    if organisation_id is not None:
        clauses.append("p.organisation_id = :org")
        params["org"] = organisation_id

    if outlet_ids is not None:
        clauses.append(
            "exists (select 1 from outlet_members m"
            " where m.profile_id = p.id and m.deleted_at is null"
            "   and m.outlet_id = any(:outlet_ids))"
        )
        params["outlet_ids"] = outlet_ids
    if role is not None:
        clauses.append("p.global_role = cast(:role as user_role)")
        params["role"] = role
    if is_active is not None:
        clauses.append("p.is_active = :is_active")
        params["is_active"] = is_active
    if search:
        clauses.append("(p.full_name ilike :search or coalesce(p.employee_code,'') ilike :search)")
        params["search"] = f"%{search}%"

    sql = _LIST_USERS + ("".join(f" and {c}" for c in clauses)) + " order by p.full_name"
    return [dict(r) for r in (await db.execute(text(sql), params)).mappings()]


async def memberships_for(
    db: AsyncSession, profile_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """Loaded in one query for the whole page, rather than per row."""
    if not profile_ids:
        return {}
    rows = (
        await db.execute(
            text(
                """
                select m.profile_id, m.outlet_id, o.code, o.name,
                       m.role_at_outlet, m.is_primary
                  from outlet_members m
                  join outlets o on o.id = m.outlet_id
                 where m.profile_id = any(:ids)
                   and m.deleted_at is null
                   and o.deleted_at is null
                 order by m.is_primary desc, o.code
                """
            ),
            {"ids": profile_ids},
        )
    ).mappings()
    grouped: dict[uuid.UUID, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(row["profile_id"], []).append(dict(row))
    return grouped


async def get_user(db: AsyncSession, profile_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        (await db.execute(text(_LIST_USERS + " and p.id = :id"), {"id": profile_id}))
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def outlet_ids_for(db: AsyncSession, profile_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await db.execute(
        text("select outlet_id from outlet_members where profile_id = :id and deleted_at is null"),
        {"id": profile_id},
    )
    return {r[0] for r in rows}


async def upsert_profile(
    db: AsyncSession,
    profile_id: uuid.UUID,
    *,
    full_name: str,
    global_role: str,
    employee_code: str | None,
    phone: str | None,
    organisation_id: uuid.UUID,
) -> None:
    """A profile is born into one organisation and never moves (D33): an
    existing profile keeps its organisation, whoever re-invites it."""
    await db.execute(
        text(
            """
            insert into profiles
                (id, full_name, global_role, employee_code, phone, is_active, organisation_id)
            values (:id, :full_name, cast(:global_role as user_role), :employee_code, :phone,
                    true, :organisation_id)
            on conflict (id) do update set
                full_name     = excluded.full_name,
                global_role   = excluded.global_role,
                employee_code = coalesce(excluded.employee_code, profiles.employee_code),
                phone         = coalesce(excluded.phone, profiles.phone),
                is_active     = true,
                deleted_at    = null
            """
        ),
        {
            "id": profile_id,
            "full_name": full_name,
            "global_role": global_role,
            "employee_code": employee_code,
            "phone": phone,
            "organisation_id": organisation_id,
        },
    )


async def people_headroom(db: AsyncSession, organisation_id: uuid.UUID) -> tuple[int, int]:
    """(people in use, cap) for the organisation."""
    row = (
        await db.execute(
            text(
                """
                select (select count(*) from profiles p
                         where p.organisation_id = g.id and p.deleted_at is null) as used,
                       g.max_people
                  from organisations g where g.id = :org
                """
            ),
            {"org": organisation_id},
        )
    ).first()
    if row is None:
        return 0, 0
    return int(row[0]), int(row[1])


async def patch_profile(db: AsyncSession, profile_id: uuid.UUID, changes: dict[str, Any]) -> None:
    if not changes:
        return
    assignments = ", ".join(f"{c} = :{c}" for c in changes)
    await db.execute(
        text(f"update profiles set {assignments} where id = :id and deleted_at is null"),
        {**changes, "id": profile_id},
    )


async def set_role(db: AsyncSession, profile_id: uuid.UUID, role: str) -> None:
    await db.execute(
        text("update profiles set global_role = cast(:role as user_role) where id = :id"),
        {"id": profile_id, "role": role},
    )


async def set_pin_hash(db: AsyncSession, profile_id: uuid.UUID, pin_hash: str | None) -> None:
    await db.execute(
        text(
            """
            update profiles
               set pin_hash = cast(:pin_hash as text),
                   pin_set_at = case when cast(:pin_hash as text) is null
                                     then null else now() end,
                   pin_failed_attempts = 0,
                   pin_locked_until = null
             where id = :id
            """
        ),
        {"id": profile_id, "pin_hash": pin_hash},
    )


async def replace_memberships(
    db: AsyncSession, profile_id: uuid.UUID, outlet_ids: list[uuid.UUID], role: str
) -> None:
    """Soft-deletes what is gone and restores what is back, so a person
    returning to an outlet keeps their original membership row."""
    await db.execute(
        text(
            "update outlet_members set deleted_at = now()"
            " where profile_id = :id and deleted_at is null"
            + (" and not (outlet_id = any(:keep))" if outlet_ids else "")
        ),
        {"id": profile_id, **({"keep": outlet_ids} if outlet_ids else {})},
    )
    for index, outlet_id in enumerate(outlet_ids):
        await db.execute(
            text(
                """
                insert into outlet_members
                    (outlet_id, profile_id, role_at_outlet, is_primary)
                values (:outlet_id, :profile_id, cast(:role as user_role), :is_primary)
                on conflict (outlet_id, profile_id) do update set
                    role_at_outlet = excluded.role_at_outlet,
                    is_primary     = excluded.is_primary,
                    deleted_at     = null
                """
            ),
            {
                "outlet_id": outlet_id,
                "profile_id": profile_id,
                "role": role,
                "is_primary": index == 0,
            },
        )


async def count_other_owners(db: AsyncSession, excluding: uuid.UUID) -> int:
    """Used to refuse removing the last owner, which would lock everyone out of
    outlet and user administration permanently."""
    count = (
        await db.execute(
            text(
                "select count(*) from profiles"
                " where global_role = 'owner' and is_active and deleted_at is null"
                "   and id <> :id"
            ),
            {"id": excluding},
        )
    ).scalar_one()
    return int(count)
