"""Round trips, held down.

P10 collapsed several per-row and per-outlet loops into single statements
because the wire, not Postgres, is what this app waits on. The danger with that
kind of change is that it stays correct on the developer's two-outlet database
and quietly goes wrong on the third outlet, or the outlet with no runs, or the
one with an override — the cases a loop handled by accident and a set-based
query has to handle on purpose.

So these are not performance tests. They are the correctness tests that the
performance work needs in order to be safe.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.settings_value import resolve_many, resolve_many_outlets
from app.domains.sop import metrics
from app.domains.users import repository as users_repo

pytestmark = pytest.mark.asyncio

WEIGHT_KEY = "scoring.weight.run_score"


@pytest.fixture
async def session(migrated_db: str):  # type: ignore[no-untyped-def]
    engine = create_async_engine(migrated_db.replace("postgresql://", "postgresql+asyncpg://"))
    async with AsyncSession(engine, expire_on_commit=False) as db:
        try:
            yield db
        finally:
            await db.rollback()
            await db.execute(text("delete from app_settings"))
            await db.execute(text("delete from auth.users where email like '%@akira.test'"))
            await db.commit()
    await engine.dispose()


async def _profile(db: AsyncSession) -> uuid.UUID:
    """The from-zero migration suite seeds outlets but no people, so anything
    about a profile has to bring its own."""
    profile_id = uuid.uuid4()
    await db.execute(
        text("insert into auth.users (id, email) values (:id, :e)"),
        {"id": profile_id, "e": f"{profile_id}@akira.test"},
    )
    await db.execute(
        text(
            "insert into profiles (id, full_name, global_role, is_active)"
            " values (:id, 'Batching Test', 'owner', true)"
        ),
        {"id": profile_id},
    )
    await db.commit()
    return profile_id


async def _outlets(db: AsyncSession) -> list[uuid.UUID]:
    return [
        uuid.UUID(str(r[0])) for r in await db.execute(text("select id from outlets order by code"))
    ]


class TestCountingManyOutletsAtOnce:
    async def test_every_outlet_asked_for_comes_back(self, session: AsyncSession) -> None:
        """The loop this replaced produced a row per outlet unconditionally.
        A `group by` does not: an outlet with no runs in the period has nothing
        to group, and would silently drop off the comparison table."""
        outlets = await _outlets(session)
        assert len(outlets) >= 2

        counts = await metrics.outlet_counts_many(
            session,
            outlet_ids=outlets,
            # A window far enough back that nothing can have happened in it.
            start=date(2001, 1, 1),
            end=date(2001, 1, 7),
        )

        assert set(counts) == set(outlets)
        for outlet in outlets:
            assert counts[outlet].scheduled == 0
            assert counts[outlet].mean_run_score is None

    async def test_an_empty_request_asks_the_database_nothing(self, session: AsyncSession) -> None:
        """A manager with no outlets must not turn into `= any('{}')`."""
        assert (
            await metrics.outlet_counts_many(
                session, outlet_ids=[], start=date(2026, 1, 1), end=date(2026, 1, 2)
            )
            == {}
        )

    async def test_the_single_outlet_helper_agrees_with_the_batch(
        self, session: AsyncSession
    ) -> None:
        """The digest calls one, the dashboard calls the other. They are the
        same statement underneath, and this is what keeps them that way."""
        outlets = await _outlets(session)
        start, end = date(2026, 8, 1), date(2026, 8, 31)

        batch = await metrics.outlet_counts_many(session, outlet_ids=outlets, start=start, end=end)
        for outlet in outlets:
            assert (
                await metrics.outlet_counts(session, outlet_id=outlet, start=start, end=end)
                == batch[outlet]
            )


class TestResolvingSettingsForManyOutlets:
    async def test_an_override_stays_attached_to_its_own_outlet(
        self, session: AsyncSession
    ) -> None:
        """The whole point of the per-outlet loop was that outlets can differ.
        Cross-joining the unnests must not smear one outlet's override across
        the rest — which is exactly the bug that would look fine locally and
        misreport every other outlet in production."""
        outlets = await _outlets(session)
        overridden, *others = outlets
        await session.execute(
            text(
                "insert into app_settings (key, scope, outlet_id, value, effective_from)"
                " values (:k, 'outlet', :o, '0.99'::jsonb, now() - interval '1 day')"
            ),
            {"k": WEIGHT_KEY, "o": overridden},
        )
        await session.commit()

        resolved = await resolve_many_outlets(
            session, [WEIGHT_KEY], outlet_ids=outlets, at=datetime.now(UTC)
        )

        assert float(resolved[overridden][WEIGHT_KEY]) == 0.99
        for other in others:
            assert float(resolved[other][WEIGHT_KEY]) != 0.99

    async def test_it_matches_resolving_one_outlet_at_a_time(self, session: AsyncSession) -> None:
        outlets = await _outlets(session)
        at = datetime.now(UTC)
        keys = [WEIGHT_KEY, "scoring.band.green"]

        batch = await resolve_many_outlets(session, keys, outlet_ids=outlets, at=at)
        for outlet in outlets:
            assert await resolve_many(session, keys, outlet_id=outlet, at=at) == batch[outlet]

    async def test_no_outlets_means_no_query(self, session: AsyncSession) -> None:
        assert await resolve_many_outlets(session, [WEIGHT_KEY], outlet_ids=[]) == {}

    async def test_an_undeclared_key_still_raises(self, session: AsyncSession) -> None:
        outlets = await _outlets(session)
        with pytest.raises(KeyError, match=r"[Nn]ot declared"):
            await resolve_many_outlets(session, ["scoring.made.up"], outlet_ids=outlets)


class TestRecordingThatSomebodyWasHere:
    async def test_a_stale_timestamp_is_refreshed(self, session: AsyncSession) -> None:
        profile = await _profile(session)
        stale = datetime.now(UTC) - timedelta(hours=3)
        await session.execute(
            text("update profiles set last_seen_at = :t where id = :id"),
            {"t": stale, "id": profile},
        )
        await session.commit()

        assert await users_repo.get_profile_and_touch(session, profile) is not None
        await session.commit()

        now_stored = (
            await session.execute(
                text("select last_seen_at from profiles where id = :id"), {"id": profile}
            )
        ).scalar_one()
        assert now_stored > stale

    async def test_a_fresh_timestamp_is_left_alone(self, session: AsyncSession) -> None:
        """Every authenticated screen used to rewrite this column. Throttling
        it is the point of the change, so a second call inside the window must
        not touch the row at all."""
        profile = await _profile(session)
        await users_repo.get_profile_and_touch(session, profile)
        await session.commit()
        first = (
            await session.execute(
                text("select last_seen_at from profiles where id = :id"), {"id": profile}
            )
        ).scalar_one()

        await users_repo.get_profile_and_touch(session, profile)
        await session.commit()
        second = (
            await session.execute(
                text("select last_seen_at from profiles where id = :id"), {"id": profile}
            )
        ).scalar_one()

        assert second == first

    async def test_it_still_returns_the_profile_it_touched(self, session: AsyncSession) -> None:
        profile = await _profile(session)
        row = await users_repo.get_profile_and_touch(session, profile)
        assert row is not None
        assert uuid.UUID(str(row["id"])) == profile
        assert "has_pin" in row

    async def test_an_unknown_profile_returns_nothing(self, session: AsyncSession) -> None:
        assert await users_repo.get_profile_and_touch(session, uuid.uuid4()) is None
