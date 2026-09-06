"""Platform endpoints: what the account above organisations may do (D33).

P26a: read the list of organisations. Creating one, its owner, and the
onboarding checklist arrive in P26b. Every route here requires the platform
admin role; the identity gate has already refused a platform admin's writes
anywhere else and recorded their reads.
"""

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.core.deps import DbDep, require_platform_admin
from app.domains.platform.schemas import OrganisationRow

router = APIRouter(
    prefix="/platform", tags=["platform"], dependencies=[Depends(require_platform_admin)]
)


@router.get(
    "/organisations",
    response_model=list[OrganisationRow],
    summary="Every organisation on the platform",
)
async def list_organisations(db: DbDep) -> list[OrganisationRow]:
    rows = (
        await db.execute(
            text(
                """
                select g.id as organisation_id, g.slug, g.name, g.is_active, g.onboarded_at,
                       g.max_outlets, g.max_people, g.created_at,
                       (select count(*) from outlets o
                         where o.organisation_id = g.id and o.deleted_at is null) as outlets,
                       (select count(*) from profiles p
                         where p.organisation_id = g.id and p.deleted_at is null) as people,
                       (select count(*) from profiles p
                         where p.organisation_id = g.id and p.deleted_at is null
                           and p.global_role = 'owner' and p.is_active) as owners
                  from organisations g
                 where g.deleted_at is null
                 order by g.created_at
                """
            )
        )
    ).mappings()
    return [OrganisationRow(**r) for r in rows]
