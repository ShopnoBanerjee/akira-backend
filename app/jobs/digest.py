"""The daily digest: what one outlet's trading day actually looked like.

Sent at 09:00 to the owner, the operations manager and that outlet's manager,
covering the business date that ended at 05:00 that morning — never the
calendar date, because a Saturday night that closes at 01:30 belongs to
Saturday.

The part that is not a summary is the **spot check**. Spec section 8's risk
table names "manager approves everything unread" as a known failure mode, which
is why `run_review_views` exists. Every approved run therefore reports how many
of its photos the approver actually opened, and a random sample is put in front
of the owner. It is an owner-level signal, deliberately not a metric shown to
the manager being measured: the moment it becomes a score, the cheapest way to
raise it is to click every photo without looking.

Rendering is a pure function of the numbers, so it is testable without a mail
server, and it emits both HTML and text — some clients want the second one, and
so does the log fallback.
"""

import random
import uuid
from dataclasses import dataclass, field
from datetime import date
from html import escape
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.settings_value import resolve_float
from app.domains.sop import metrics

#: Bumped when the layout changes materially, so a stored digest can be read
#: against the shape it was written in.
DIGEST_VERSION = "1"


@dataclass(frozen=True)
class SpotCheck:
    run_id: uuid.UUID
    template_name: str
    approved_by: str | None
    score_pct: float | None
    photo_count: int
    photos_opened: int

    @property
    def approved_unread(self) -> bool:
        """Approved with photos on the run and none of them opened."""
        return self.photo_count > 0 and self.photos_opened == 0


@dataclass(frozen=True)
class Digest:
    outlet_code: str
    outlet_name: str
    business_date: date
    scheduled: int
    approved: int
    submitted_awaiting: int
    missed: int
    still_open: int
    mean_score: float | None
    critical_fails: int
    integrity_flags: int
    open_exceptions: int
    stale_exceptions: int
    spot_checks: list[SpotCheck] = field(default_factory=list)
    #: Net sales for the trading day, already formatted (₹, Indian grouping);
    #: None when no bills landed. Computed by code — the narrative may only
    #: repeat it.
    net_display: str | None = None
    net_target_display: str | None = None
    #: One model-written paragraph, or None when the narrator was skipped or
    #: unavailable. Advisory: the digest reads fine without it.
    narrative: str | None = None

    @property
    def completion_rate(self) -> float | None:
        """Approved out of scheduled. None when nothing was scheduled — a
        closed day is not a 0% day."""
        if self.scheduled == 0:
            return None
        return round(100.0 * self.approved / self.scheduled, 1)

    @property
    def headline(self) -> str:
        if self.scheduled == 0:
            return "Nothing was scheduled."
        parts = [f"{self.approved}/{self.scheduled} approved"]
        if self.missed:
            parts.append(f"{self.missed} missed")
        if self.critical_fails:
            parts.append(f"{self.critical_fails} critical fail(s)")
        if self.integrity_flags:
            parts.append(f"{self.integrity_flags} integrity flag(s)")
        return " · ".join(parts)


async def build(db: AsyncSession, *, outlet_id: uuid.UUID, business_date: date) -> Digest:
    outlet = (
        (await db.execute(text("select code, name from outlets where id = :id"), {"id": outlet_id}))
        .mappings()
        .first()
    )
    if outlet is None:
        raise ValueError(f"outlet {outlet_id} does not exist")

    # The same counter the dashboard uses, for one business date. Two queries
    # would eventually disagree, and the morning they did nobody would know
    # which number to believe.
    counts = await metrics.outlet_counts(
        db, outlet_id=outlet_id, start=business_date, end=business_date
    )
    # Only the two the shared counter has no reason to carry.
    extra = (
        (
            await db.execute(
                text(
                    """
                    select count(*) filter (where status in ('pending', 'in_progress'))
                               as still_open,
                           count(*) filter (where status = 'submitted') as submitted_awaiting,
                           coalesce(sum(critical_fail_count), 0) as critical_fails
                      from checklist_runs
                     where outlet_id = :outlet_id and business_date = :business_date
                    """
                ),
                {"outlet_id": outlet_id, "business_date": business_date},
            )
        )
        .mappings()
        .one()
    )

    open_exceptions = (
        await db.execute(
            text(
                "select count(*) from sop_exceptions where outlet_id = :outlet_id"
                " and status in ('open', 'acknowledged')"
            ),
            {"outlet_id": outlet_id},
        )
    ).scalar_one()

    return Digest(
        outlet_code=outlet["code"],
        outlet_name=outlet["name"],
        business_date=business_date,
        scheduled=counts.scheduled,
        approved=counts.approved,
        submitted_awaiting=extra["submitted_awaiting"],
        missed=counts.missed,
        still_open=extra["still_open"],
        mean_score=counts.mean_run_score,
        critical_fails=extra["critical_fails"],
        integrity_flags=counts.integrity_flags,
        open_exceptions=open_exceptions,
        stale_exceptions=counts.stale_critical,
        spot_checks=await _spot_checks(db, outlet_id=outlet_id, business_date=business_date),
    )


async def _spot_checks(
    db: AsyncSession, *, outlet_id: uuid.UUID, business_date: date
) -> list[SpotCheck]:
    """A random sample of the day's approved runs, with review depth attached.

    Sampled rather than exhaustive on purpose. A list of every approved run is
    a list nobody reads; a random three that the owner knows could be any three
    is a check that actually changes behaviour.
    """
    share = await resolve_float(db, "jobs.digest_spot_check_share", outlet_id=outlet_id)
    if share <= 0:
        return []

    rows = (
        (
            await db.execute(
                text(
                    """
                    select r.id, t.name as template_name, r.approved_by,
                           p.full_name as approved_by_name,
                           cast(r.score_pct as float8) as score_pct,
                           (select count(*) from checklist_run_items ri
                             where ri.run_id = r.id and ri.photo_path is not null)
                               as photo_count,
                           (select count(*) from run_review_views v
                             where v.run_id = r.id and v.reviewer_id = r.approved_by)
                               as photos_opened
                      from checklist_runs r
                      join checklist_templates t on t.id = r.template_id
                      left join profiles p on p.id = r.approved_by
                     where r.outlet_id = :outlet_id
                       and r.business_date = :business_date
                       and r.status = 'approved'
                    """
                ),
                {"outlet_id": outlet_id, "business_date": business_date},
            )
        )
        .mappings()
        .all()
    )
    if not rows:
        return []

    approved = [
        SpotCheck(
            run_id=r["id"],
            template_name=r["template_name"],
            approved_by=r["approved_by_name"],
            score_pct=r["score_pct"],
            photo_count=r["photo_count"],
            photos_opened=r["photos_opened"],
        )
        for r in rows
    ]
    # Anything approved without a photo being opened goes in regardless of the
    # sample: that is the signal the table was built for, and leaving it to
    # chance would mean the one run worth looking at is the one that got away.
    unread = [s for s in approved if s.approved_unread]
    remaining = [s for s in approved if not s.approved_unread]
    wanted = max(1, round(share * len(approved))) - len(unread)
    if wanted > 0:
        unread += random.sample(remaining, min(wanted, len(remaining)))
    return unread


async def recipients(db: AsyncSession, outlet_id: uuid.UUID) -> list[str]:
    """Owner, ops manager, and that outlet's manager.

    Addresses live in auth.users, not profiles — the API never stores a second
    copy of something Supabase Auth owns.
    """
    rows = (
        await db.execute(
            text(
                """
                select distinct u.email
                  from profiles p
                  join auth.users u on u.id = p.id
                 where p.is_active and p.deleted_at is null and u.email is not null
                   and (
                       p.global_role in ('owner', 'ops_manager')
                       or (
                           p.global_role = 'outlet_manager'
                           and exists (
                               select 1 from outlet_members m
                                where m.profile_id = p.id
                                  and m.outlet_id = :outlet_id
                                  and m.deleted_at is null
                           )
                       )
                   )
                 order by u.email
                """
            ),
            {"outlet_id": outlet_id},
        )
    ).scalars()
    return list(rows)


# ---------------------------------------------------------------------------
# Rendering — pure
# ---------------------------------------------------------------------------

_INK = "#231f20"
_RED = "#ee3345"
_BLUE = "#326fb7"
_GREEN = "#2f9e5f"
_AMBER = "#e0a020"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0f}%"


def _band(score: float | None) -> str:
    if score is None:
        return _INK
    if score >= 90:
        return _GREEN
    return _AMBER if score >= 75 else _RED


def render(digest: Digest) -> tuple[str, str, str]:
    """(subject, html, text). No I/O, so this is unit-testable as itself."""
    subject = f"AKIRA {digest.outlet_code} — {digest.business_date}: {digest.headline}"

    narrative_html = (
        f'<p style="font-size:15px;line-height:1.5;margin:0 0 14px">{escape(digest.narrative)}</p>'
        if digest.narrative
        else ""
    )

    stats = [
        ("Runs approved", f"{digest.approved} of {digest.scheduled}"),
        ("Completion", _pct(digest.completion_rate)),
        ("Mean score", _pct(digest.mean_score)),
        ("Still awaiting review", str(digest.submitted_awaiting)),
        ("Missed", str(digest.missed)),
        ("Critical failures", str(digest.critical_fails)),
        ("Integrity flags", str(digest.integrity_flags)),
        (
            "Open exceptions",
            f"{digest.open_exceptions}"
            + (f" ({digest.stale_exceptions} over 48h)" if digest.stale_exceptions else ""),
        ),
    ]

    rows = "".join(
        f'<tr><td style="padding:6px 14px 6px 0;color:#231f20a0">{escape(label)}</td>'
        f'<td style="padding:6px 0;font-weight:600;text-align:right">{escape(value)}</td></tr>'
        for label, value in stats
    )

    if digest.spot_checks:
        checks = "".join(
            '<li style="margin-bottom:6px">'
            f"<strong>{escape(s.template_name)}</strong> — approved by "
            f"{escape(s.approved_by or 'unknown')}, score {_pct(s.score_pct)}. "
            + (
                f'<span style="color:{_RED};font-weight:600">'
                f"{s.photo_count} photo(s), none opened before approving.</span>"
                if s.approved_unread
                else f"{s.photos_opened} of {s.photo_count} photo(s) opened."
            )
            + "</li>"
            for s in digest.spot_checks
        )
        spot_html = (
            f'<h3 style="margin:24px 0 6px;font-size:14px;color:{_BLUE}">Spot check</h3>'
            f'<p style="margin:0 0 8px;font-size:13px;color:#231f20a0">'
            "A sample of yesterday's approvals, with how much of each the approver "
            "actually looked at.</p>"
            f'<ul style="margin:0;padding-left:18px;font-size:13px">{checks}</ul>'
        )
    else:
        spot_html = ""

    html = (
        f"{narrative_html}"
        f"<div style=\"font-family:'Noto Sans',Arial,sans-serif;color:{_INK};"
        'max-width:560px">'
        f'<p style="margin:0;font-size:12px;letter-spacing:.08em;text-transform:uppercase;'
        f'color:{_BLUE}">AKIRA Ops · daily digest</p>'
        f'<h2 style="margin:4px 0 2px;font-size:20px">{escape(digest.outlet_name)} '
        f"({escape(digest.outlet_code)})</h2>"
        f'<p style="margin:0 0 16px;font-size:13px;color:#231f20a0">'
        f"Business date {digest.business_date} — the trading day that ended at 05:00 today."
        "</p>"
        f'<p style="margin:0 0 16px;font-size:16px;font-weight:600;'
        f'color:{_band(digest.mean_score)}">{escape(digest.headline)}</p>'
        f'<table style="border-collapse:collapse;font-size:13px;width:100%">{rows}</table>'
        f"{spot_html}"
        f'<p style="margin:24px 0 0;font-size:11px;color:#231f2080">'
        "Integrity flags are advisory. They never block a submission — they are "
        "there so a manager can ask a question.</p>"
        "</div>"
    )

    lines = [
        "AKIRA Ops — daily digest",
        f"{digest.outlet_name} ({digest.outlet_code}) — business date {digest.business_date}",
        "",
        digest.headline,
        "",
        *(f"  {label}: {value}" for label, value in stats),
    ]
    if digest.spot_checks:
        lines += ["", "Spot check:"]
        for s in digest.spot_checks:
            depth = (
                f"{s.photo_count} photo(s), NONE opened before approving"
                if s.approved_unread
                else f"{s.photos_opened} of {s.photo_count} photo(s) opened"
            )
            lines.append(
                f"  - {s.template_name} — approved by {s.approved_by or 'unknown'}, "
                f"score {_pct(s.score_pct)}. {depth}."
            )
    lines += [
        "",
        "Integrity flags are advisory. They never block a submission.",
    ]
    if digest.narrative:
        lines.insert(0, digest.narrative)
        lines.insert(1, "")
    return subject, html, "\n".join(lines)


def to_detail(digest: Digest) -> dict[str, Any]:
    """The shape recorded in job_runs.detail, so a digest that was sent can be
    reconstructed without re-querying a day that has since moved on."""
    return {
        "outlet": digest.outlet_code,
        "business_date": str(digest.business_date),
        "scheduled": digest.scheduled,
        "approved": digest.approved,
        "missed": digest.missed,
        "mean_score": digest.mean_score,
        "critical_fails": digest.critical_fails,
        "integrity_flags": digest.integrity_flags,
        "open_exceptions": digest.open_exceptions,
        "spot_checks": len(digest.spot_checks),
        "approved_unread": sum(1 for s in digest.spot_checks if s.approved_unread),
        "digest_version": DIGEST_VERSION,
    }
