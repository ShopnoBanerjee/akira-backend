"""SQL for outlets. No business rules here."""

import uuid
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_COLUMNS = """
    o.id, o.code, o.name, o.address_line, o.city, o.geo_lat, o.geo_lng,
    o.geofence_radius_m, o.timezone, o.opened_on, o.is_active,
    (select count(*) from outlet_members m
      where m.outlet_id = o.id and m.deleted_at is null) as member_count,
    (select count(*) from outlet_devices d
      where d.outlet_id = o.id and d.deleted_at is null and d.is_active) as device_count
"""


async def list_outlets(
    db: AsyncSession, *, outlet_ids: list[uuid.UUID] | None, include_inactive: bool
) -> list[dict[str, Any]]:
    """outlet_ids None means "every outlet" — only for owner and ops_manager."""
    clauses = ["o.deleted_at is null"]
    params: dict[str, Any] = {}
    if outlet_ids is not None:
        clauses.append("o.id = any(:ids)")
        params["ids"] = outlet_ids
    if not include_inactive:
        clauses.append("o.is_active")
    sql = f"select {_COLUMNS} from outlets o where {' and '.join(clauses)} order by o.code"
    return [dict(r) for r in (await db.execute(text(sql), params)).mappings()]


async def get_outlet(db: AsyncSession, outlet_id: uuid.UUID) -> dict[str, Any] | None:
    row = (
        (
            await db.execute(
                text(f"select {_COLUMNS} from outlets o where o.id = :id and o.deleted_at is null"),
                {"id": outlet_id},
            )
        )
        .mappings()
        .first()
    )
    return dict(row) if row else None


async def code_exists(db: AsyncSession, code: str) -> bool:
    return (
        await db.execute(text("select 1 from outlets where code = :code"), {"code": code})
    ).first() is not None


async def insert_outlet(db: AsyncSession, values: dict[str, Any]) -> uuid.UUID:
    outlet_id = (
        await db.execute(
            text(
                """
                insert into outlets
                    (code, name, address_line, city, geo_lat, geo_lng,
                     geofence_radius_m, timezone, opened_on)
                values
                    (:code, :name, :address_line, :city, :geo_lat, :geo_lng,
                     :geofence_radius_m, :timezone, :opened_on)
                returning id
                """
            ),
            values,
        )
    ).scalar_one()
    return uuid.UUID(str(outlet_id))


async def update_outlet(db: AsyncSession, outlet_id: uuid.UUID, changes: dict[str, Any]) -> None:
    if not changes:
        return
    assignments = ", ".join(f"{column} = :{column}" for column in changes)
    await db.execute(
        text(f"update outlets set {assignments} where id = :id and deleted_at is null"),
        {**changes, "id": outlet_id},
    )


async def soft_delete_outlet(db: AsyncSession, outlet_id: uuid.UUID) -> None:
    await db.execute(
        text(
            "update outlets set deleted_at = now(), is_active = false"
            " where id = :id and deleted_at is null"
        ),
        {"id": outlet_id},
    )


async def count_open_runs(db: AsyncSession, outlet_id: uuid.UUID) -> int:
    """Runs that are still live. Deleting an outlet under them would strand
    work someone is in the middle of."""
    count = (
        await db.execute(
            text(
                "select count(*) from checklist_runs"
                " where outlet_id = :id and status in ('pending','in_progress','submitted')"
            ),
            {"id": outlet_id},
        )
    ).scalar_one()
    return int(count)
