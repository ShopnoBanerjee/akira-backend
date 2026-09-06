"""Onboarding readiness: what an outlet still needs before AKIRA Ops works.

A new organisation starts empty (D33). Every screen then reads as broken —
an empty dashboard, "no data yet" everywhere — without saying which of a
dozen possible causes it is. This turns that into a list: what is missing,
why it matters, and the screen that fixes it.

It is a **checklist, not a gate**. Nothing here blocks the app; a restaurant
can run checklists on day one with no sales file uploaded. What it blocks is
the guessing.

Status is computed from the data itself, never stored. There is no "mark as
done" to drift out of step with reality: upload the report and the step is
done, delete everything and it is pending again.

## Which Petpooja exports, and why each one

Petpooja has no sales API (`docs/PLAN_MULTI_TENANT.md` §2) — everything
arrives as an export a human downloads. Four matter, and they are not
interchangeable:

- **Item Wise** teaches the menu itself: every dish Petpooja prints, with its
  category. Nothing else supplies the names that sales, recipes and the menu
  map all join on, which is why it is first.
- **Order Listing** carries the bills: one row per item per bill, so the
  system can measure what actually sold together.
- **Category Wise** carries Petpooja's own per-period counts, the reported
  half of the attach rates (D29). Keeping both halves is deliberate: when
  they disagree, that is a finding, not a bug.
- **Item Report: Day Wise** gives per-day units per dish, which is what turns
  recipes into theoretical consumption. Recommended rather than required:
  no Akira export of it has ever been uploaded (`docs/OPEN_ITEMS.md`), so
  demanding it would block onboarding on a file that may not exist.
"""

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import CurrentUser
from app.core.errors import ForbiddenError, NotFoundError


@dataclass(frozen=True)
class Step:
    """One thing to do. `count` is what was found, for honest feedback:
    "3 of these" reads better than a tick with no evidence behind it."""

    key: str
    title: str
    why: str
    how: str
    required: bool
    done: bool
    count: int
    href: str


#: One query per step would be nine round trips to Mumbai for one screen.
#: Everything the checklist needs, in a single pass.
_READINESS = text(
    """
    with outlet as (
        select o.id, o.organisation_id from outlets o where o.id = :outlet_id
    )
    select
        (select count(*) from menu_items m, outlet o
          where m.organisation_id = o.organisation_id)                     as menu_items,
        (select count(*) from sales_orders s, outlet o
          where s.outlet_id = o.id)                                        as bills,
        (select count(*) from sales_category_periods c, outlet o
          where c.outlet_id = o.id)                                        as category_rows,
        (select count(*) from sales_item_days d, outlet o
          where d.outlet_id = o.id)                                        as item_days,
        (select count(*) from checklist_assignments a, outlet o
          where a.outlet_id = o.id and a.deleted_at is null and a.is_active) as assignments,
        (select count(*) from outlet_members m
           join profiles p on p.id = m.profile_id, outlet o
          where m.outlet_id = o.id and m.deleted_at is null
            and p.deleted_at is null and p.is_active
            and p.global_role in ('outlet_manager', 'shift_lead', 'staff'))  as people,
        (select count(*) from outlet_devices d, outlet o
          where d.outlet_id = o.id and d.deleted_at is null and d.is_active) as devices,
        (select count(*) from recipes r, outlet o
          where r.is_active
            and (r.organisation_id is null or r.organisation_id = o.organisation_id)) as recipes,
        (select count(*) from app_settings s, outlet o
          where s.key = 'sales.petpooja_restaurant_name'
            and (s.outlet_id = o.id or s.organisation_id = o.organisation_id))  as restaurant_name
    """
)


def _steps(found: dict[str, int]) -> list[Step]:
    """The checklist, in the order somebody should actually do it.

    Order matters: the menu map is first because the sales reports join on
    the names it teaches, and checklists come before people because there is
    nothing for a new starter to open until a template is assigned.
    """
    return [
        Step(
            key="menu_map",
            title="Upload an Item Wise report",
            why=(
                "It teaches the system your menu: every dish Petpooja prints and the "
                "category it sits in. Sales, recipes and attach rates all join on those "
                "exact names, so nothing else works properly until this is in."
            ),
            how=(
                "Petpooja → Reports → Item Wise. Export any recent period, then upload it on Sales."
            ),
            required=True,
            done=found["menu_items"] > 0,
            count=found["menu_items"],
            href="/app/sales",
        ),
        Step(
            key="bills",
            title="Upload an Order Listing report",
            why=(
                "The bills themselves, one row per item per bill. This is what lets the "
                "system measure what actually sold together rather than trusting a summary."
            ),
            how=(
                "Petpooja → Reports → Order Listing. Export the same period, then "
                "upload it on Sales."
            ),
            required=True,
            done=found["bills"] > 0,
            count=found["bills"],
            href="/app/sales",
        ),
        Step(
            key="category_mix",
            title="Upload a Category Wise report",
            why=(
                "Petpooja's own count of how many bills carried each category. Kept "
                "beside the figure measured from your bills on purpose: when the two "
                "disagree, that disagreement is the finding."
            ),
            how="Petpooja → Reports → Category Wise. Same period again, then upload it on Sales.",
            required=True,
            done=found["category_rows"] > 0,
            count=found["category_rows"],
            href="/app/sales",
        ),
        Step(
            key="checklists",
            title="Assign checklists to this outlet",
            why=(
                "Until a template is assigned there is nothing for the floor to open, "
                "and the compliance pillar of the score has no input."
            ),
            how="SOP Templates → pick one → Assignments → add this outlet and its day part.",
            required=True,
            done=found["assignments"] > 0,
            count=found["assignments"],
            href="/app/sop/assignments",
        ),
        Step(
            key="people",
            title="Add the people who work here",
            why=(
                "Somebody has to run the checklists and somebody else has to approve "
                "them — an approver can never be the submitter, so one person alone "
                "cannot close the loop."
            ),
            how=(
                "People → Invite. Give floor staff a PIN so their work on the tablet is attributed."
            ),
            required=True,
            done=found["people"] > 0,
            count=found["people"],
            href="/app/settings/users",
        ),
        Step(
            key="restaurant_guard",
            title="Set the Petpooja restaurant name",
            why=(
                "With it set, an export belonging to a different restaurant is refused "
                "at upload instead of quietly becoming this outlet's numbers."
            ),
            how=(
                "Settings → Sales → Petpooja restaurant name. Copy it exactly as "
                "the export prints it."
            ),
            required=False,
            done=found["restaurant_name"] > 0,
            count=found["restaurant_name"],
            href="/app/settings",
        ),
        Step(
            key="tablet",
            title="Register the floor tablet",
            why=(
                "The floor shares one tablet per outlet; staff identify with a PIN so "
                "each run still belongs to a real person."
            ),
            how="Tablets → Register. The tablet's own login is created in Supabase first.",
            required=False,
            done=found["devices"] > 0,
            count=found["devices"],
            href="/app/settings/devices",
        ),
        Step(
            key="item_days",
            title="Upload an Item Report: Day Wise",
            why=(
                "Units sold per dish per day. Recipes need it to turn sales into "
                "expected ingredient usage, which is what makes stock variance mean "
                "anything. Nothing breaks without it; that half of the picture is "
                "simply blank."
            ),
            how="Petpooja → Reports → Item Report: Day Wise, then upload it on Sales.",
            required=False,
            done=found["item_days"] > 0,
            count=found["item_days"],
            href="/app/sales",
        ),
        Step(
            key="recipes",
            title="Map a few dishes to their ingredients",
            why=(
                "A recipe turns a dish sold into ingredients used. Start with what "
                "sells most; the unmapped list is ordered by volume for that reason."
            ),
            how="Recipes → pick a name from the unmapped list → add its ingredient lines.",
            required=False,
            done=found["recipes"] > 0,
            count=found["recipes"],
            href="/app/settings/recipes",
        ),
    ]


async def readiness(
    db: AsyncSession, user: CurrentUser, *, outlet_id: uuid.UUID | None
) -> dict[str, Any]:
    """The checklist for one outlet. With no outlet named, the caller's own —
    unambiguous for a single-outlet organisation, which is every new one."""
    if outlet_id is None:
        reachable = sorted(user.outlet_ids)
        if not reachable:
            raise NotFoundError("There is no outlet to set up yet.")
        if len(reachable) > 1:
            raise NotFoundError(
                "Name the outlet to check.",
                extra={"outlet_ids": [str(o) for o in reachable]},
            )
        outlet_id = reachable[0]
    elif not user.can_access_outlet(outlet_id):
        raise ForbiddenError("You do not have access to that outlet.")

    row = (await db.execute(_READINESS, {"outlet_id": outlet_id})).mappings().first()
    if row is None:
        raise NotFoundError("That outlet does not exist.")

    steps = _steps({k: int(v or 0) for k, v in row.items()})
    required = [s for s in steps if s.required]
    return {
        "outlet_id": outlet_id,
        "steps": [s.__dict__ for s in steps],
        "required_done": sum(1 for s in required if s.done),
        "required_total": len(required),
        "recommended_done": sum(1 for s in steps if not s.required and s.done),
        "recommended_total": sum(1 for s in steps if not s.required),
        # True once nothing required is outstanding. Informational: the app
        # never refuses anything on the strength of it.
        "ready": all(s.done for s in required),
    }
