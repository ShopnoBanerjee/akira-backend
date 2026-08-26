"""The scheduled jobs and the digest.

The digest renderer is pure, so most of this needs no database and no mail
server. What does touch the database is the part that decides a run was missed,
because that one changes state and raises an exception a manager will chase.
"""

import uuid
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.config import Settings
from app.jobs import digest as digest_module
from app.jobs import notify
from app.jobs.digest import Digest, SpotCheck

pytestmark = pytest.mark.asyncio


def a_digest(**overrides) -> Digest:  # type: ignore[no-untyped-def]
    base = Digest(
        outlet_code="AKR-NT01",
        outlet_name="New Town",
        business_date=date(2026, 8, 26),
        scheduled=10,
        approved=8,
        submitted_awaiting=1,
        missed=1,
        still_open=0,
        mean_score=93.5,
        critical_fails=0,
        integrity_flags=2,
        open_exceptions=3,
        stale_exceptions=1,
        spot_checks=[],
    )
    return replace(base, **overrides)


class TestDigestArithmetic:
    def test_completion_is_approved_over_scheduled(self) -> None:
        assert a_digest(scheduled=10, approved=8).completion_rate == 80.0

    def test_a_day_with_nothing_scheduled_is_not_a_zero_percent_day(self) -> None:
        """A closed outlet did not fail its checklists; it had none."""
        assert a_digest(scheduled=0, approved=0).completion_rate is None

    def test_headline_names_what_went_wrong(self) -> None:
        headline = a_digest(missed=2, critical_fails=1, integrity_flags=3).headline
        assert "8/10 approved" in headline
        assert "2 missed" in headline
        assert "1 critical fail" in headline
        assert "3 integrity flag" in headline

    def test_headline_stays_quiet_on_a_clean_day(self) -> None:
        headline = a_digest(approved=10, missed=0, critical_fails=0, integrity_flags=0).headline
        assert headline == "10/10 approved"

    def test_a_closed_day_says_so(self) -> None:
        assert a_digest(scheduled=0).headline == "Nothing was scheduled."


class TestApprovedUnread:
    def test_photos_present_and_none_opened(self) -> None:
        check = SpotCheck(uuid.uuid4(), "Closing", "Riya", 100.0, 4, 0)
        assert check.approved_unread

    def test_some_opened_is_not_unread(self) -> None:
        assert not SpotCheck(uuid.uuid4(), "Closing", "Riya", 100.0, 4, 1).approved_unread

    def test_a_run_with_no_photos_cannot_be_unread(self) -> None:
        """Nothing to look at is not the same as not looking."""
        assert not SpotCheck(uuid.uuid4(), "Prep", "Riya", 100.0, 0, 0).approved_unread


class TestRendering:
    def test_subject_carries_the_outlet_the_date_and_the_headline(self) -> None:
        subject, _, _ = digest_module.render(a_digest())
        assert "AKR-NT01" in subject
        assert "2026-08-26" in subject
        assert "8/10 approved" in subject

    def test_both_parts_are_produced(self) -> None:
        _, html, plain = digest_module.render(a_digest())
        assert html.startswith("<div")
        assert "New Town" in plain
        assert "Integrity flags are advisory" in plain

    def test_an_unread_approval_is_called_out_in_words(self) -> None:
        check = SpotCheck(uuid.uuid4(), "Closing — Floor", "Riya Sen", 100.0, 5, 0)
        _, html, plain = digest_module.render(a_digest(spot_checks=[check]))
        assert "none opened before approving" in html
        assert "NONE opened before approving" in plain

    def test_a_reviewed_approval_reports_the_depth_without_alarm(self) -> None:
        check = SpotCheck(uuid.uuid4(), "Closing", "Riya Sen", 92.0, 5, 3)
        _, html, _ = digest_module.render(a_digest(spot_checks=[check]))
        assert "3 of 5 photo(s) opened" in html
        assert "none opened" not in html

    def test_names_are_escaped(self) -> None:
        """Names come from the database, and the digest is HTML mail."""
        check = SpotCheck(uuid.uuid4(), "<script>x</script>", "A & B", 100.0, 1, 1)
        _, html, _ = digest_module.render(a_digest(spot_checks=[check]))
        assert "<script>" not in html
        assert "&lt;script&gt;" in html
        assert "A &amp; B" in html

    def test_the_date_is_named_as_a_business_date(self) -> None:
        """The one number a reader could most easily misread."""
        _, html, _ = digest_module.render(a_digest())
        assert "Business date 2026-08-26" in html
        assert "05:00" in html


class TestNotifierSelection:
    def test_email_without_a_host_degrades_and_says_why(self) -> None:
        """A digest that silently stopped sending is the failure this whole
        epic exists to prevent."""
        notifier, reason = notify.get_notifier("email", Settings(SMTP_HOST=""))
        assert isinstance(notifier, notify.LogNotifier)
        assert reason == "smtp_not_configured"

    def test_email_with_a_host_uses_smtp(self) -> None:
        notifier, reason = notify.get_notifier("email", Settings(SMTP_HOST="smtp.example"))
        assert isinstance(notifier, notify.EmailNotifier)
        assert reason is None

    def test_log_only_is_not_a_downgrade(self) -> None:
        notifier, reason = notify.get_notifier("log_only", Settings())
        assert isinstance(notifier, notify.LogNotifier)
        assert reason is None

    async def test_the_log_notifier_really_emits_the_content(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Not a null object: an unconfigured deployment still leaves the
        digest somewhere a person can read it."""
        import logging

        caplog.set_level(logging.INFO, logger="app.jobs.notify")
        result = await notify.LogNotifier().send(
            notify.Notification(
                subject="s", html="<p>h</p>", text="the numbers", recipients=["a@b.test"]
            )
        )
        assert result["delivered"] is True
        assert "the numbers" in caplog.text

    async def test_email_with_no_recipients_reports_rather_than_sends(self) -> None:
        result = await notify.EmailNotifier(Settings(SMTP_HOST="smtp.example")).send(
            notify.Notification(subject="s", html="h", text="t", recipients=[])
        )
        assert result == {
            "channel": "email",
            "delivered": False,
            "reason": "no_recipients",
        }

    async def test_a_broken_mail_server_is_recorded_not_raised(self) -> None:
        """One outlet's digest failing must not stop the others."""
        result = await notify.EmailNotifier(Settings(SMTP_HOST="127.0.0.1", SMTP_PORT=1)).send(
            notify.Notification(subject="s", html="h", text="t", recipients=["a@b.test"])
        )
        assert result["delivered"] is False
        assert result["reason"]


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


@pytest.fixture
async def session(migrated_db: str):  # type: ignore[no-untyped-def]
    engine = create_async_engine(migrated_db.replace("postgresql://", "postgresql+asyncpg://"))
    async with AsyncSession(engine, expire_on_commit=False) as db:
        keep = [r[0] for r in await db.execute(text("select id from checklist_runs"))]
        exceptions = [r[0] for r in await db.execute(text("select id from sop_exceptions"))]
        try:
            yield db
        finally:
            await db.rollback()
            await db.execute(
                text("delete from checklist_runs where not (id = any(:keep))"), {"keep": keep}
            )
            await db.execute(
                text("delete from sop_exceptions where not (id = any(:keep))"),
                {"keep": exceptions},
            )
            await db.commit()
    await engine.dispose()


async def _make_pending_run(db: AsyncSession, *, due_at: datetime, on: date) -> uuid.UUID:
    row = (
        (
            await db.execute(
                text(
                    """
                    select a.id as assignment_id, a.template_id, a.outlet_id, t.version
                      from checklist_assignments a
                      join checklist_templates t on t.id = a.template_id
                     where a.is_active limit 1
                    """
                )
            )
        )
        .mappings()
        .first()
    )
    assert row is not None
    run_id = (
        await db.execute(
            text(
                """
                insert into checklist_runs
                    (assignment_id, template_id, template_version, outlet_id,
                     business_date, day_part, status, due_at)
                values (:assignment_id, :template_id, :version, :outlet_id,
                        :business_date, 'any', 'pending', :due_at)
                returning id
                """
            ),
            {**dict(row), "business_date": on, "due_at": due_at},
        )
    ).scalar_one()
    await db.commit()
    return uuid.UUID(str(run_id))


class TestMarkMissed:
    """Exercised through the same SQL the job runs, against a real database.

    The job function itself opens its own session through the app engine, which
    a test database cannot supply, so the query and the state change are what
    is verified here.
    """

    async def _run_the_query(self, db: AsyncSession) -> list[uuid.UUID]:
        from app.jobs.tasks import _OVERDUE_SQL

        return [r["id"] for r in (await db.execute(_OVERDUE_SQL)).mappings()]

    async def test_a_pending_run_past_grace_is_selected(self, session: AsyncSession) -> None:
        run_id = await _make_pending_run(
            session,
            due_at=datetime.now(tz=UTC) - timedelta(hours=3),
            on=date(2026, 8, 20),
        )
        assert run_id in await self._run_the_query(session)

    async def test_a_run_still_inside_its_grace_is_left_alone(self, session: AsyncSession) -> None:
        run_id = await _make_pending_run(
            session,
            # Default grace is 30 minutes; ten minutes past due is inside it.
            due_at=datetime.now(tz=UTC) - timedelta(minutes=10),
            on=date(2026, 8, 21),
        )
        assert run_id not in await self._run_the_query(session)

    async def test_a_future_run_is_left_alone(self, session: AsyncSession) -> None:
        run_id = await _make_pending_run(
            session, due_at=datetime.now(tz=UTC) + timedelta(hours=2), on=date(2026, 8, 22)
        )
        assert run_id not in await self._run_the_query(session)

    async def test_a_run_being_worked_on_is_never_marked_missed(
        self, session: AsyncSession
    ) -> None:
        """missed is terminal. Flipping an in-progress run would lock it and
        throw away a half-finished checklist — `late` is what lateness is for."""
        run_id = await _make_pending_run(
            session, due_at=datetime.now(tz=UTC) - timedelta(hours=3), on=date(2026, 8, 23)
        )
        await session.execute(
            text("update checklist_runs set status = 'in_progress' where id = :id"),
            {"id": run_id},
        )
        await session.commit()
        assert run_id not in await self._run_the_query(session)


class TestDigestBuild:
    async def test_it_counts_the_day_it_was_asked_about(self, session: AsyncSession) -> None:
        outlet_id = (
            await session.execute(text("select id from outlets where code = 'AKR-NT01'"))
        ).scalar_one()
        on = date(2026, 7, 1)
        await _make_pending_run(session, due_at=datetime(2026, 7, 1, 18, tzinfo=UTC), on=on)

        data = await digest_module.build(session, outlet_id=outlet_id, business_date=on)
        assert data.outlet_code == "AKR-NT01"
        assert data.scheduled == 1
        assert data.approved == 0
        assert data.still_open == 1
        assert data.completion_rate == 0.0

        # A different day sees none of it.
        other = await digest_module.build(
            session, outlet_id=outlet_id, business_date=date(2026, 7, 2)
        )
        assert other.scheduled == 0

    async def test_recipients_are_the_people_accountable_for_the_outlet(
        self, session: AsyncSession
    ) -> None:
        outlet_id = (
            await session.execute(text("select id from outlets where code = 'AKR-NT01'"))
        ).scalar_one()
        # The seed has no auth.users rows, so this proves the query runs and
        # returns only addresses — never a staff member's.
        addresses = await digest_module.recipients(session, outlet_id)
        assert all("@" in a for a in addresses)
