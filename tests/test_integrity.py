"""The integrity engine.

Two things this suite exists to prove, because both are ways the feature fails
quietly rather than loudly:

- a re-used photo IS caught, and the flag names what it matched;
- two genuinely different photos of the same station are NOT caught. A
  duplicate detector with false positives gets switched off within a week, and
  then nothing is checked at all.
"""

import io
import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from PIL import Image, ImageDraw
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.enums import IntegrityFlag
from app.domains.sop import integrity

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Image helpers — real JPEGs, because the point is the codec's behaviour
# ---------------------------------------------------------------------------


def _jpeg(image: Image.Image, quality: int = 80) -> bytes:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def prep_station(seed: int = 0, size: int = 320) -> Image.Image:
    """A synthetic "photo": structured enough to have a meaningful perceptual
    hash, and different per seed the way two stations differ."""
    image = Image.new("RGB", (size, size), (190, 190, 195))
    draw = ImageDraw.Draw(image)
    rng = (seed * 37) % 90
    draw.rectangle([20 + rng, 30, 150 + rng, 200], fill=(40 + seed * 20, 60, 90))
    draw.ellipse([180, 60 + rng, 290, 170 + rng], fill=(220, 200 - seed * 15, 80))
    draw.line([0, 240 + (seed % 3) * 10, size, 210], fill=(30, 30, 30), width=9)
    return image


def flat(level: int, size: int = 320) -> Image.Image:
    return Image.new("RGB", (size, size), (level, level, level))


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


class TestHamming:
    def test_identical_hashes_are_zero_apart(self) -> None:
        assert integrity.hamming("ffee001122334455", "ffee001122334455") == 0

    def test_counts_differing_bits(self) -> None:
        # 0x0 vs 0xf differ in four bits.
        assert integrity.hamming("0000000000000000", "000000000000000f") == 4

    def test_different_lengths_raise_rather_than_lie(self) -> None:
        """Comparing hashes of different sizes would produce a number with no
        meaning, which is worse than an error."""
        with pytest.raises(ValueError, match="lengths differ"):
            integrity.hamming("ffee", "ffee0011")


class TestPerceptualHash:
    def test_same_photo_recompressed_stays_within_the_threshold(self) -> None:
        """What a re-used photo actually looks like: the same image round-
        tripped through the client resize and a second JPEG encode."""
        original = prep_station(1)
        a = integrity.phash_hex(_jpeg(original, quality=90))
        b = integrity.phash_hex(_jpeg(original.resize((256, 256)).resize((320, 320)), quality=55))
        assert integrity.hamming(a, b) <= 5

    def test_two_different_stations_are_far_apart(self) -> None:
        """The false-positive case. A detector that flags honest work gets
        turned off, and then nothing is checked at all."""
        a = integrity.phash_hex(_jpeg(prep_station(1)))
        b = integrity.phash_hex(_jpeg(prep_station(5)))
        assert integrity.hamming(a, b) > 5

    def test_hash_is_sixteen_hex_characters(self) -> None:
        value = integrity.phash_hex(_jpeg(prep_station(2)))
        assert len(value) == 16
        int(value, 16)


class TestLuminance:
    def test_black_is_near_zero_and_white_near_full(self) -> None:
        assert integrity.mean_luminance(_jpeg(flat(0))) < 5
        assert integrity.mean_luminance(_jpeg(flat(255))) > 250

    def test_mid_grey_is_mid(self) -> None:
        assert 120 < integrity.mean_luminance(_jpeg(flat(128))) < 136

    def test_a_dark_kitchen_photo_falls_below_the_default_threshold(self) -> None:
        """ai_review.min_luminance defaults to 40."""
        assert integrity.mean_luminance(_jpeg(flat(20))) < 40


class TestBurstShare:
    def test_no_photos_is_not_a_burst(self) -> None:
        """A run with nothing to photograph has not been batch-faked."""
        assert integrity.burst_share([], datetime.now(tz=UTC), 3) == 0.0

    def test_all_inside_the_window(self) -> None:
        submitted = datetime(2026, 8, 27, 23, 0, tzinfo=UTC)
        uploads = [submitted - timedelta(seconds=s) for s in (10, 40, 100)]
        assert integrity.burst_share(uploads, submitted, 3) == 1.0

    def test_work_spread_across_a_shift_is_not_a_burst(self) -> None:
        submitted = datetime(2026, 8, 27, 23, 0, tzinfo=UTC)
        uploads = [submitted - timedelta(minutes=m) for m in (55, 40, 22, 1)]
        assert integrity.burst_share(uploads, submitted, 3) == 0.25


class TestStaleCapture:
    started = datetime(2026, 8, 27, 18, 0, tzinfo=UTC)
    submitted = datetime(2026, 8, 27, 19, 0, tzinfo=UTC)

    def test_photo_inside_the_run_window_is_fine(self) -> None:
        assert not integrity.is_stale_capture(
            self.started + timedelta(minutes=20), self.started, self.submitted
        )

    def test_photo_predating_the_run_is_a_gallery_pick(self) -> None:
        assert integrity.is_stale_capture(
            self.started - timedelta(minutes=1), self.started, self.submitted
        )

    def test_unstarted_run_makes_no_judgement(self) -> None:
        assert not integrity.is_stale_capture(self.started, None, None)

    def test_open_run_has_no_upper_bound_yet(self) -> None:
        assert not integrity.is_stale_capture(self.started + timedelta(hours=3), self.started, None)


class TestImplausiblySpeed:
    started = datetime(2026, 8, 27, 18, 0, tzinfo=UTC)

    def test_twelve_items_in_a_minute_is_not_real_work(self) -> None:
        assert integrity.completed_implausibly_fast(
            self.started, self.started + timedelta(seconds=60), 12
        )

    def test_a_short_checklist_can_legitimately_be_quick(self) -> None:
        assert not integrity.completed_implausibly_fast(
            self.started, self.started + timedelta(seconds=60), 4
        )

    def test_a_long_checklist_walked_properly_is_fine(self) -> None:
        assert not integrity.completed_implausibly_fast(
            self.started, self.started + timedelta(minutes=25), 20
        )


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


@pytest.fixture
async def session(migrated_db: str):  # type: ignore[no-untyped-def]
    """A session whose runs are swept up afterwards.

    The engine commits internally, so a transaction cannot isolate these tests.
    Instead each one gets the runs it created deleted at the end — which also
    keeps the duplicate lookback from finding another test's photos, the
    failure mode that would make this suite pass or fail by ordering.
    """
    engine = create_async_engine(migrated_db.replace("postgresql://", "postgresql+asyncpg://"))
    async with AsyncSession(engine, expire_on_commit=False) as db:
        keep = [r[0] for r in await db.execute(text("select id from checklist_runs"))]
        try:
            yield db
        finally:
            await db.rollback()
            await db.execute(
                text("delete from checklist_runs where not (id = any(:keep))"),
                {"keep": keep},
            )
            await db.commit()
    await engine.dispose()


BUSINESS_DATE = date(2026, 8, 27)


async def _make_run(
    db: AsyncSession, *, items: int = 3, on: date = BUSINESS_DATE
) -> dict[str, uuid.UUID]:
    """A run with `items` photo items, built on the seeded outlet and template.

    Uses the real assignment/template rows so template_item_version_id is a
    genuine snapshot, which is what the duplicate lookback joins through.
    One run per assignment per business date, so callers wanting two runs pass
    two dates.
    """
    row = (
        (
            await db.execute(
                text(
                    """
                    select a.id as assignment_id, a.template_id, a.outlet_id,
                           t.version
                      from checklist_assignments a
                      join checklist_templates t on t.id = a.template_id
                     where a.is_active
                     limit 1
                    """
                )
            )
        )
        .mappings()
        .first()
    )
    assert row is not None, "the seed should provide at least one assignment"

    run_id = (
        await db.execute(
            text(
                """
                insert into checklist_runs
                    (assignment_id, template_id, template_version, outlet_id,
                     business_date, day_part, status, due_at, started_at)
                values (:assignment_id, :template_id, :version, :outlet_id,
                        :business_date, 'any', 'in_progress',
                        :due_at, :started_at)
                returning id
                """
            ),
            {
                **dict(row),
                "business_date": on,
                "due_at": datetime(2026, 8, 27, 17, 0, tzinfo=UTC),
                "started_at": datetime(2026, 8, 27, 16, 0, tzinfo=UTC),
            },
        )
    ).scalar_one()

    versions = (
        (
            await db.execute(
                text(
                    """
                select v.id, v.template_item_id
                  from checklist_template_item_versions v
                 where v.template_id = :template_id and v.template_version = :version
                 order by v.sort_order
                 limit :limit
                """
                ),
                {"template_id": row["template_id"], "version": row["version"], "limit": items},
            )
        )
        .mappings()
        .all()
    )

    item_ids = []
    for index, version in enumerate(versions):
        item_id = (
            await db.execute(
                text(
                    """
                    insert into checklist_run_items
                        (run_id, template_item_id, sort_order,
                         template_item_version_id, result)
                    values (:run_id, :template_item_id, :sort_order, :version_id, 'pass')
                    returning id
                    """
                ),
                {
                    "run_id": run_id,
                    "template_item_id": version["template_item_id"],
                    "sort_order": index,
                    "version_id": version["id"],
                },
            )
        ).scalar_one()
        item_ids.append(item_id)

    await db.commit()
    return {
        "run_id": run_id,
        "outlet_id": row["outlet_id"],
        "item_ids": item_ids,  # type: ignore[dict-item]
    }


async def _attach_photo(
    db: AsyncSession, item_id: uuid.UUID, *, uploaded_at: datetime | None = None
) -> None:
    await db.execute(
        text(
            """
            update checklist_run_items
               set photo_path = :path,
                   photo_uploaded_at = coalesce(:uploaded_at, now())
             where id = :id
            """
        ),
        {
            "id": item_id,
            "path": f"test/{item_id}.jpg",
            "uploaded_at": uploaded_at or datetime(2026, 8, 27, 16, 30, tzinfo=UTC),
        },
    )
    await db.commit()


def _serve(images: dict[str, bytes], monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for Supabase Storage. The engine's job is what it does with the
    bytes; where they came from is not what is under test here."""

    async def download(path: str) -> bytes:
        return images[path]

    monkeypatch.setattr(integrity.storage, "download_object", download)


class TestPhotoPass:
    async def test_a_dark_photo_is_flagged_with_its_measurement(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await _make_run(session, items=1)
        item_id = run["item_ids"][0]  # type: ignore[index]
        await _attach_photo(session, item_id)
        _serve({f"test/{item_id}.jpg": _jpeg(flat(15))}, monkeypatch)

        result = await integrity.process_photo(session, item_id)

        assert IntegrityFlag.TOO_DARK.value in result.flags
        # The evidence, not just the accusation.
        assert result.detail[IntegrityFlag.TOO_DARK.value]["minimum"] == 40
        assert result.detail[IntegrityFlag.TOO_DARK.value]["luminance"] < 40

    async def test_a_well_lit_photo_is_clean_but_still_marked_processed(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await _make_run(session, items=1)
        item_id = run["item_ids"][0]  # type: ignore[index]
        await _attach_photo(session, item_id)
        _serve({f"test/{item_id}.jpg": _jpeg(prep_station(3))}, monkeypatch)

        result = await integrity.process_photo(session, item_id)
        assert result.flags == []

        row = (
            (
                await session.execute(
                    text(
                        "select photo_phash, photo_luminance, photo_processed_at"
                        " from checklist_run_items where id = :id"
                    ),
                    {"id": item_id},
                )
            )
            .mappings()
            .first()
        )
        assert row is not None
        assert row["photo_phash"] == result.phash
        # "clean" and "not looked at yet" must be distinguishable.
        assert row["photo_processed_at"] is not None

    async def test_reusing_yesterdays_photo_names_the_run_it_matched(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        image = _jpeg(prep_station(7))

        yesterday = await _make_run(session, items=1, on=BUSINESS_DATE - timedelta(days=1))
        old_item = yesterday["item_ids"][0]  # type: ignore[index]
        await _attach_photo(session, old_item)

        today = await _make_run(session, items=1)
        new_item = today["item_ids"][0]  # type: ignore[index]
        await _attach_photo(session, new_item)

        _serve(
            {f"test/{old_item}.jpg": image, f"test/{new_item}.jpg": image},
            monkeypatch,
        )

        await integrity.process_photo(session, old_item)
        result = await integrity.process_photo(session, new_item)

        assert IntegrityFlag.DUPLICATE_PHOTO.value in result.flags
        evidence = result.detail[IntegrityFlag.DUPLICATE_PHOTO.value]
        assert evidence["matched_run_id"] == str(yesterday["run_id"])
        assert evidence["matched_business_date"] == str(BUSINESS_DATE - timedelta(days=1))
        assert evidence["distance"] == 0

    async def test_two_different_photos_of_the_same_item_are_not_flagged(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The false positive that would get the whole feature disabled."""
        yesterday = await _make_run(session, items=1, on=BUSINESS_DATE - timedelta(days=1))
        old_item = yesterday["item_ids"][0]  # type: ignore[index]
        await _attach_photo(session, old_item)

        today = await _make_run(session, items=1)
        new_item = today["item_ids"][0]  # type: ignore[index]
        await _attach_photo(session, new_item)

        _serve(
            {
                f"test/{old_item}.jpg": _jpeg(prep_station(1)),
                f"test/{new_item}.jpg": _jpeg(prep_station(6)),
            },
            monkeypatch,
        )

        await integrity.process_photo(session, old_item)
        result = await integrity.process_photo(session, new_item)
        assert IntegrityFlag.DUPLICATE_PHOTO.value not in result.flags

    async def test_a_photo_predating_the_run_is_a_gallery_pick(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await _make_run(session, items=1)
        item_id = run["item_ids"][0]  # type: ignore[index]
        # The run started 16:00; this photo was taken the previous afternoon.
        await _attach_photo(session, item_id, uploaded_at=datetime(2026, 8, 26, 14, 0, tzinfo=UTC))
        _serve({f"test/{item_id}.jpg": _jpeg(prep_station(4))}, monkeypatch)

        result = await integrity.process_photo(session, item_id)
        assert IntegrityFlag.STALE_CAPTURE.value in result.flags

    async def test_reshooting_clears_the_previous_flag(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pass owns its own flags. Re-running it after the photo is replaced
        must clear what no longer applies, or a rejected-and-redone item keeps
        an accusation it has answered."""
        run = await _make_run(session, items=1)
        item_id = run["item_ids"][0]  # type: ignore[index]
        await _attach_photo(session, item_id)

        _serve({f"test/{item_id}.jpg": _jpeg(flat(10))}, monkeypatch)
        assert (
            IntegrityFlag.TOO_DARK.value in (await integrity.process_photo(session, item_id)).flags
        )

        _serve({f"test/{item_id}.jpg": _jpeg(prep_station(8))}, monkeypatch)
        assert (await integrity.process_photo(session, item_id)).flags == []

    async def test_it_does_not_touch_flags_it_does_not_own(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await _make_run(session, items=1)
        item_id = run["item_ids"][0]  # type: ignore[index]
        await _attach_photo(session, item_id)
        await session.execute(
            text("update checklist_run_items set integrity_flags = '{ai_mismatch}' where id = :id"),
            {"id": item_id},
        )
        await session.commit()

        _serve({f"test/{item_id}.jpg": _jpeg(prep_station(9))}, monkeypatch)
        await integrity.process_photo(session, item_id)

        flags = (
            await session.execute(
                text("select integrity_flags from checklist_run_items where id = :id"),
                {"id": item_id},
            )
        ).scalar_one()
        assert IntegrityFlag.AI_MISMATCH.value in flags


class TestRunPass:
    async def _submit(
        self,
        db: AsyncSession,
        run_id: uuid.UUID,
        *,
        is_late: bool = False,
        geo_ok: bool | None = None,
        submitted_at: datetime | None = None,
    ) -> None:
        await db.execute(
            text(
                """
                update checklist_runs
                   set status = 'submitted',
                       submitted_at = coalesce(:submitted_at, now()),
                       is_late = :is_late, minutes_late = case when :is_late then 45 end,
                       geo_ok = :geo_ok
                 where id = :id
                """
            ),
            {
                "id": run_id,
                "is_late": is_late,
                "geo_ok": geo_ok,
                "submitted_at": submitted_at or datetime(2026, 8, 27, 17, 30, tzinfo=UTC),
            },
        )
        await db.commit()

    async def test_late_and_off_site_both_flag(self, session: AsyncSession) -> None:
        run = await _make_run(session, items=2)
        await self._submit(session, run["run_id"], is_late=True, geo_ok=False)

        flags = await integrity.evaluate_run(session, run["run_id"])
        await session.commit()

        assert set(flags) == {
            IntegrityFlag.LATE.value,
            IntegrityFlag.OUT_OF_GEOFENCE.value,
        }

    async def test_withheld_location_is_not_a_flag(self, session: AsyncSession) -> None:
        """Refusing location is usually a permission staff cannot change.
        Flagging it teaches everyone to keep location off."""
        run = await _make_run(session, items=2)
        await self._submit(session, run["run_id"], geo_ok=None)

        flags = await integrity.evaluate_run(session, run["run_id"])
        await session.commit()
        assert IntegrityFlag.OUT_OF_GEOFENCE.value not in flags

    async def test_photos_dumped_just_before_submit_flag_as_a_burst(
        self, session: AsyncSession
    ) -> None:
        run = await _make_run(session, items=3)
        submitted = datetime(2026, 8, 27, 17, 30, tzinfo=UTC)
        for offset, item_id in enumerate(run["item_ids"]):  # type: ignore[arg-type]
            await _attach_photo(
                session, item_id, uploaded_at=submitted - timedelta(seconds=30 * offset + 10)
            )
        await self._submit(session, run["run_id"], submitted_at=submitted)

        flags = await integrity.evaluate_run(session, run["run_id"])
        await session.commit()
        assert IntegrityFlag.BURST_UPLOAD.value in flags

    async def test_photos_spread_across_the_shift_do_not_flag(self, session: AsyncSession) -> None:
        run = await _make_run(session, items=3)
        submitted = datetime(2026, 8, 27, 17, 30, tzinfo=UTC)
        for offset, item_id in enumerate(run["item_ids"]):  # type: ignore[arg-type]
            await _attach_photo(
                session, item_id, uploaded_at=submitted - timedelta(minutes=40 - offset * 15)
            )
        await self._submit(session, run["run_id"], submitted_at=submitted)

        flags = await integrity.evaluate_run(session, run["run_id"])
        await session.commit()
        assert IntegrityFlag.BURST_UPLOAD.value not in flags

    async def test_the_count_is_run_flags_plus_item_flags(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await _make_run(session, items=2)
        dark_item = run["item_ids"][0]  # type: ignore[index]
        await _attach_photo(session, dark_item)
        _serve({f"test/{dark_item}.jpg": _jpeg(flat(8))}, monkeypatch)
        await integrity.process_photo(session, dark_item)

        await self._submit(session, run["run_id"], is_late=True, geo_ok=False)
        await integrity.evaluate_run(session, run["run_id"])
        await session.commit()

        count = (
            await session.execute(
                text("select integrity_flag_count from checklist_runs where id = :id"),
                {"id": run["run_id"]},
            )
        ).scalar_one()
        # late + out_of_geofence on the run, too_dark on the item.
        assert count == 3
