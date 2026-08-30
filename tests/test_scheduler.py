"""The reconciler that keeps job times in step with settings.

This suite exists because of a bug that hid perfectly. The reconciler ran every
five minutes and re-added all three jobs with `replace_existing=True`.
Replacing a job rebuilds its trigger, and an interval trigger measures its next
fire from the moment it is built — so `mark_missed`, on a fifteen-minute
interval, was pushed fifteen minutes into the future every five. It could never
fire. Nothing looked wrong: the scheduler reported it as scheduled, the jobs
screen showed a plausible next run time, and the only evidence was an absence —
no automatic executions in `job_runs`, ever.

The rule these tests hold down: **reconciling an unchanged schedule must do
nothing at all.**
"""

from datetime import time

import pytest

from app.jobs import scheduler as sched

pytestmark = pytest.mark.asyncio


def a_schedule(**overrides: object) -> sched.Schedule:
    base: dict[str, object] = {
        "materialise_at": time(5, 0),
        "digest_at": time(9, 0),
        "missed_every_minutes": 15,
        "anomalies_at": time(5, 45),
        "forecast_at": time(5, 30),
    }
    return sched.Schedule(**{**base, **overrides})  # type: ignore[arg-type]


class _FakeScheduler:
    """Records what would have been installed, without a running event loop."""

    def __init__(self) -> None:
        self.added: list[str] = []

    def add_job(self, _func: object, _trigger: object, **kwargs: object) -> None:
        self.added.append(str(kwargs.get("id")))


@pytest.fixture(autouse=True)
def _clean_module_state():  # type: ignore[no-untyped-def]
    """The scheduler keeps module-level state; do not let it leak between tests."""
    before_sched, before_applied = sched._scheduler, sched._applied
    yield
    sched._scheduler, sched._applied = before_sched, before_applied


class TestReconcileOnlyActsOnChange:
    async def test_an_unchanged_schedule_reinstalls_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole bug, in one assertion. Re-adding here would restart every
        interval job's clock, and a job whose clock restarts more often than
        its interval never runs."""
        fake = _FakeScheduler()
        monkeypatch.setattr(sched, "_scheduler", fake)
        sched._apply(fake, a_schedule())  # type: ignore[arg-type]
        assert len(fake.added) == 5
        fake.added.clear()

        async def unchanged() -> sched.Schedule:
            return a_schedule()

        monkeypatch.setattr(sched, "read_schedule", unchanged)
        await sched.reconcile()
        await sched.reconcile()
        await sched.reconcile()

        assert fake.added == [], "an unchanged schedule must not touch the jobs"

    async def test_a_changed_time_is_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeScheduler()
        monkeypatch.setattr(sched, "_scheduler", fake)
        sched._apply(fake, a_schedule())  # type: ignore[arg-type]
        fake.added.clear()

        async def moved() -> sched.Schedule:
            return a_schedule(digest_at=time(10, 30))

        monkeypatch.setattr(sched, "read_schedule", moved)
        await sched.reconcile()

        assert sorted(fake.added) == sorted(
            [
                sched.tasks.MATERIALISE,
                sched.tasks.MARK_MISSED,
                sched.tasks.DAILY_DIGEST,
                sched.tasks.STOCK_ANOMALIES,
                sched.tasks.SALES_FORECAST,
            ]
        )

    async def test_a_changed_interval_is_installed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeScheduler()
        monkeypatch.setattr(sched, "_scheduler", fake)
        sched._apply(fake, a_schedule())  # type: ignore[arg-type]
        fake.added.clear()

        async def faster() -> sched.Schedule:
            return a_schedule(missed_every_minutes=5)

        monkeypatch.setattr(sched, "read_schedule", faster)
        await sched.reconcile()
        assert sched.tasks.MARK_MISSED in fake.added

    async def test_it_settles_after_a_change(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """One install, then quiet — not an install every five minutes forever."""
        fake = _FakeScheduler()
        monkeypatch.setattr(sched, "_scheduler", fake)
        sched._apply(fake, a_schedule())  # type: ignore[arg-type]
        fake.added.clear()

        async def moved() -> sched.Schedule:
            return a_schedule(materialise_at=time(6, 0))

        monkeypatch.setattr(sched, "read_schedule", moved)
        await sched.reconcile()
        installed = len(fake.added)
        await sched.reconcile()
        await sched.reconcile()
        assert len(fake.added) == installed


class TestReconcileIsDefensive:
    async def test_a_settings_blip_keeps_the_working_schedule(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A database hiccup must not leave the API with no schedule at all."""
        fake = _FakeScheduler()
        monkeypatch.setattr(sched, "_scheduler", fake)
        sched._apply(fake, a_schedule())  # type: ignore[arg-type]
        fake.added.clear()

        async def boom() -> sched.Schedule:
            raise RuntimeError("connection reset")

        monkeypatch.setattr(sched, "read_schedule", boom)
        await sched.reconcile()  # must not raise
        assert fake.added == []
        assert sched._applied == a_schedule()

    async def test_it_does_nothing_when_the_scheduler_is_off(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sched, "_scheduler", None)

        async def never_called() -> sched.Schedule:
            raise AssertionError("settings must not be read when there is no scheduler")

        monkeypatch.setattr(sched, "read_schedule", never_called)
        await sched.reconcile()


class TestScheduleEquality:
    """The change detection is a dataclass comparison, so it has to be exact."""

    def test_identical_schedules_compare_equal(self) -> None:
        assert a_schedule() == a_schedule()

    def test_every_field_is_part_of_the_comparison(self) -> None:
        assert a_schedule() != a_schedule(materialise_at=time(6, 0))
        assert a_schedule() != a_schedule(digest_at=time(10, 0))
        assert a_schedule() != a_schedule(missed_every_minutes=30)
        assert a_schedule() != a_schedule(anomalies_at=time(6, 15))


class TestShutdown:
    async def test_it_forgets_what_was_applied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Otherwise a restart in the same process would compare against a
        schedule that is no longer installed and skip installing it."""
        fake = _FakeScheduler()
        monkeypatch.setattr(sched, "_scheduler", fake)
        sched._apply(fake, a_schedule())  # type: ignore[arg-type]
        assert sched._applied is not None

        class _Stoppable(_FakeScheduler):
            def shutdown(self, wait: bool = True) -> None:
                pass

        monkeypatch.setattr(sched, "_scheduler", _Stoppable())
        await sched.shutdown()
        assert sched._applied is None
