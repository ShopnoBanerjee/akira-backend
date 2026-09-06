"""Setting a value and reading it back, for every type the registry has.

The registry's own suite checks validation — what may be written. This one
checks the far side: that what comes back out is what went in, with the same
type it went in as.

That gap hid a real bug. `_decode` ran `json.loads` on anything that came back
as a `str`, which is right when the driver hands over jsonb as JSON text and
wrong when its codec has already decoded it. Numbers and booleans survive both
ways — a decoded one is not a `str` at all — so every setting anyone had ever
changed through the admin screen (nine rows, all numeric or boolean) worked,
and the four `jobs.*_time` settings would have raised inside the scheduler's
reconciler the first time somebody moved the digest by half an hour.

So: one test per type, and a restaurant called "123", which is the case that
`json.loads` gets wrong rather than merely crashes on.
"""

import json
import uuid
from datetime import time

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.settings_value import resolve, resolve_many, resolve_many_outlets, resolve_time
from tests.conftest import DEV_ORG, dev_user

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session(migrated_db: str):  # type: ignore[no-untyped-def]
    engine = create_async_engine(migrated_db.replace("postgresql://", "postgresql+asyncpg://"))
    async with AsyncSession(engine, expire_on_commit=False) as db:
        try:
            yield db
        finally:
            await db.rollback()
            await db.execute(text("delete from app_settings"))
            await db.commit()
    await engine.dispose()


async def put(
    db: AsyncSession, key: str, value: object, *, outlet_id: uuid.UUID | None = None
) -> None:
    """Write a setting the way the admin endpoint does — as jsonb."""
    await db.execute(
        text(
            """
            insert into app_settings
                (key, scope, outlet_id, organisation_id, value, effective_from)
            values (:k, cast(:scope as setting_scope), :o, :org, cast(:v as jsonb),
                    now() - interval '1 minute')
            """
        ),
        {
            "k": key,
            # As the admin endpoint decides it (D33): outlet, else the
            # platform's job clock stays global, else the organisation's.
            "scope": "outlet"
            if outlet_id
            else ("global" if key.startswith("jobs.") else "organisation"),
            "org": None if outlet_id or key.startswith("jobs.") else DEV_ORG,
            "o": outlet_id,
            "v": json.dumps(value),
        },
    )
    await db.commit()


async def an_outlet(db: AsyncSession) -> uuid.UUID:
    row = (await db.execute(text("select id from outlets order by code limit 1"))).scalar_one()
    return uuid.UUID(str(row))


class TestEveryTypeSurvivesTheRoundTrip:
    async def test_number(self, session: AsyncSession) -> None:
        await put(session, "scoring.band.green", 88.5)
        assert await resolve(session, "scoring.band.green", organisation_id=DEV_ORG) == 88.5

    async def test_integer(self, session: AsyncSession) -> None:
        await put(session, "integrity.phash_max_distance", 9)
        assert await resolve(session, "integrity.phash_max_distance", organisation_id=DEV_ORG) == 9

    async def test_boolean(self, session: AsyncSession) -> None:
        await put(session, "ai_review.enabled", False)
        assert await resolve(session, "ai_review.enabled", organisation_id=DEV_ORG) is False

    async def test_time(self, session: AsyncSession) -> None:
        """The one that would have taken the scheduler down."""
        await put(session, "jobs.digest_time", "09:15")
        assert await resolve(session, "jobs.digest_time", organisation_id=DEV_ORG) == "09:15"
        assert await resolve_time(session, "jobs.digest_time", organisation_id=DEV_ORG) == time(
            9, 15
        )

    async def test_string(self, session: AsyncSession) -> None:
        await put(session, "notifications.channel", "log_only")
        assert (
            await resolve(session, "notifications.channel", organisation_id=DEV_ORG) == "log_only"
        )

    async def test_a_string_that_looks_like_a_number_stays_a_string(
        self, session: AsyncSession
    ) -> None:
        """A restaurant named "123" must not resolve to the integer 123. This
        is the case a bare json.loads gets silently wrong rather than loudly."""
        await put(session, "sales.petpooja_restaurant_name", "123")
        got = await resolve(session, "sales.petpooja_restaurant_name", organisation_id=DEV_ORG)
        assert got == "123"
        assert isinstance(got, str)

    async def test_a_string_that_looks_like_a_boolean_stays_a_string(
        self, session: AsyncSession
    ) -> None:
        await put(session, "sales.petpooja_restaurant_name", "true")
        got = await resolve(session, "sales.petpooja_restaurant_name", organisation_id=DEV_ORG)
        assert got == "true"
        assert isinstance(got, str)


class TestTheBatchedReadersDecodeTheSameWay:
    """`resolve_many` and `resolve_many_outlets` exist to save round trips, not
    to have their own opinion about types."""

    async def test_resolve_many(self, session: AsyncSession) -> None:
        await put(session, "jobs.digest_time", "09:15")
        await put(session, "notifications.channel", "log_only")
        await put(session, "integrity.phash_max_distance", 9)
        got = await resolve_many(
            session,
            ["jobs.digest_time", "notifications.channel", "integrity.phash_max_distance"],
            organisation_id=DEV_ORG,
        )
        assert got == {
            "jobs.digest_time": "09:15",
            "notifications.channel": "log_only",
            "integrity.phash_max_distance": 9,
        }

    async def test_resolve_many_outlets(self, session: AsyncSession) -> None:
        outlet = await an_outlet(session)
        await put(session, "sales.petpooja_restaurant_name", "Akira Ramen")
        got = await resolve_many_outlets(
            session, ["sales.petpooja_restaurant_name"], outlet_ids=[outlet]
        )
        assert got[outlet]["sales.petpooja_restaurant_name"] == "Akira Ramen"


class TestUnsetKeysStillFallBack:
    """The path that always worked, kept honest — a key with no row must reach
    the registry default rather than None."""

    async def test_no_row_means_the_default(self, session: AsyncSession) -> None:
        assert await resolve(session, "jobs.digest_time", organisation_id=DEV_ORG) == "09:00"
        assert (
            await resolve(session, "sales.petpooja_restaurant_name", organisation_id=DEV_ORG) == ""
        )


class TestTheAdminScreensDecodeTheSameWay:
    """`list_settings` and `setting_history` each had their own copy of the
    decode, and the copies raised on any text value — so saving a job time or
    a restaurant name returned 500 for the WHOLE settings screen, every key,
    not just the one that was set. They share the resolver's decoder now.
    """

    async def test_the_settings_list_survives_a_text_value(self, session: AsyncSession) -> None:
        from app.core.settings_registry import REGISTRY
        from app.domains.settings.router import list_settings

        await put(session, "sales.petpooja_restaurant_name", "Akira Ramen")
        await put(session, "jobs.digest_time", "09:15")

        views = await list_settings(session, dev_user())
        assert len(views) == len(REGISTRY)
        by_key = {v.key: v for v in views}
        assert by_key["sales.petpooja_restaurant_name"].value == "Akira Ramen"
        assert by_key["sales.petpooja_restaurant_name"].is_set is True
        assert by_key["jobs.digest_time"].value == "09:15"
        # An untouched key still reads as its default and says so.
        assert by_key["scoring.band.green"].value == 90
        assert by_key["scoring.band.green"].is_set is False

    async def test_history_survives_a_text_value(self, session: AsyncSession) -> None:
        from app.domains.settings.router import setting_history

        await put(session, "sales.petpooja_restaurant_name", "Akira Ramen")
        rows = await setting_history("sales.petpooja_restaurant_name", session, dev_user())
        assert [r.value for r in rows] == ["Akira Ramen"]
