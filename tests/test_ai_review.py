"""The advisory AI photo review (D6).

The model call itself is stubbed. What is worth testing is everything around
it — the rules that decide when an opinion becomes a red chip on a manager's
screen — because those are the rules that decide whether the feature is useful
or merely noisy.
"""

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.enums import IntegrityFlag
from app.domains.sop import ai_review
from app.integrations import vision

pytestmark = pytest.mark.asyncio


class TestEffectiveVerdict:
    """A fail the model is 40% sure of is not a fail. It is the model saying it
    cannot tell, in more assertive words than it should have used."""

    def test_a_confident_fail_stays_a_fail(self) -> None:
        assert ai_review.effective_verdict("fail", 0.92, 0.7) == "fail"

    def test_a_confident_pass_stays_a_pass(self) -> None:
        assert ai_review.effective_verdict("pass", 0.85, 0.7) == "pass"

    def test_a_low_confidence_fail_becomes_uncertain(self) -> None:
        assert ai_review.effective_verdict("fail", 0.4, 0.7) == "uncertain"

    def test_a_low_confidence_pass_becomes_uncertain_too(self) -> None:
        """Symmetry matters. Downgrading only the fails would make the reviewer
        look reliable in one direction and cautious in the other."""
        assert ai_review.effective_verdict("pass", 0.4, 0.7) == "uncertain"

    def test_exactly_at_the_threshold_counts(self) -> None:
        assert ai_review.effective_verdict("fail", 0.7, 0.7) == "fail"

    def test_uncertain_stays_uncertain_however_confident(self) -> None:
        assert ai_review.effective_verdict("uncertain", 0.99, 0.7) == "uncertain"

    def test_a_missing_confidence_is_not_trusted(self) -> None:
        assert ai_review.effective_verdict("fail", None, 0.7) == "uncertain"


class TestShouldFlag:
    def test_a_recorded_pass_the_reviewer_confidently_fails(self) -> None:
        assert ai_review.should_flag("pass", "fail")

    def test_agreement_on_a_pass_flags_nothing(self) -> None:
        assert not ai_review.should_flag("pass", "pass")

    def test_agreement_on_a_fail_flags_nothing(self) -> None:
        assert not ai_review.should_flag("fail", "fail")

    def test_staff_being_harder_than_the_model_is_not_a_flag(self) -> None:
        """Staff recorded a fail; the model thinks it looks fine. Flagging that
        would punish honesty, which is the last thing this system should do."""
        assert not ai_review.should_flag("fail", "pass")

    def test_uncertainty_never_flags(self) -> None:
        assert not ai_review.should_flag("pass", "uncertain")
        assert not ai_review.should_flag("fail", "uncertain")

    def test_an_na_item_never_flags(self) -> None:
        assert not ai_review.should_flag("na", "fail")


class TestPrompt:
    def test_it_names_the_item_and_what_staff_recorded(self) -> None:
        prompt = vision.build_prompt(
            title="Sink clean",
            instruction="Scrub and dry the basin.",
            has_reference=True,
            recorded_result="pass",
        )
        assert "Sink clean" in prompt
        assert "Scrub and dry the basin." in prompt
        assert "recorded this item as: pass" in prompt

    def test_with_a_reference_it_says_which_image_is_which(self) -> None:
        """The images are sent reference-first. If the prompt did not say so,
        every comparison would be silently inverted."""
        prompt = vision.build_prompt(
            title="Sink clean", instruction=None, has_reference=True, recorded_result="pass"
        )
        assert "first image is this outlet's reference standard" in prompt
        assert "second image is what staff submitted" in prompt

    def test_without_a_reference_it_asks_for_caution(self) -> None:
        prompt = vision.build_prompt(
            title="Sink clean", instruction=None, has_reference=False, recorded_result="pass"
        )
        assert "no reference photo" in prompt
        assert "uncertain" in prompt

    def test_the_system_prompt_says_it_is_advisory(self) -> None:
        assert "advisory" in vision.SYSTEM.lower()
        assert "human manager makes the decision" in vision.SYSTEM

    def test_the_system_prompt_forbids_commenting_on_people(self) -> None:
        """Shared kitchen photos will contain staff. The reviewer's job is the
        station, never the person in front of it."""
        assert "Never mention people" in vision.SYSTEM


class TestMediaTypeSniffing:
    def test_jpeg(self) -> None:
        assert vision._media_type(b"\xff\xd8\xff\xe0rest") == "image/jpeg"

    def test_png(self) -> None:
        assert vision._media_type(b"\x89PNG\r\n\x1a\n") == "image/png"

    def test_webp(self) -> None:
        assert vision._media_type(b"RIFF\x00\x00\x00\x00WEBP") == "image/webp"

    def test_unknown_falls_back_rather_than_guessing_wildly(self) -> None:
        assert vision._media_type(b"not an image") == "image/jpeg"


class TestVisionUnavailable:
    async def test_no_api_key_raises_rather_than_inventing_a_verdict(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Silence from the model is not the same as `uncertain`. Writing a
        row either way would be a fabricated opinion in an audit trail."""
        from app.core.config import Settings, get_settings

        get_settings.cache_clear()
        monkeypatch.setattr(
            "app.integrations.vision.get_settings", lambda: Settings(ANTHROPIC_API_KEY="")
        )
        with pytest.raises(vision.VisionUnavailable, match="ANTHROPIC_API_KEY"):
            await vision.review(
                submitted=b"\xff\xd8\xff",
                reference=None,
                title="Sink clean",
                instruction=None,
                recorded_result="pass",
            )


# ---------------------------------------------------------------------------
# Against a real database
# ---------------------------------------------------------------------------


@pytest.fixture
async def session(migrated_db: str):  # type: ignore[no-untyped-def]
    engine = create_async_engine(migrated_db.replace("postgresql://", "postgresql+asyncpg://"))
    async with AsyncSession(engine, expire_on_commit=False) as db:
        keep = [r[0] for r in await db.execute(text("select id from checklist_runs"))]
        try:
            yield db
        finally:
            await db.rollback()
            await db.execute(
                text("delete from checklist_runs where not (id = any(:keep))"), {"keep": keep}
            )
            await db.execute(text("delete from outlet_item_reference_photos"))
            await db.commit()
    await engine.dispose()


async def _run_with_photo(db: AsyncSession, *, result: str = "pass") -> dict[str, uuid.UUID]:
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
                     business_date, day_part, status, started_at)
                values (:assignment_id, :template_id, :version, :outlet_id,
                        :business_date, 'any', 'submitted', :started_at)
                returning id
                """
            ),
            {
                **dict(row),
                "business_date": date(2026, 6, 15),
                "started_at": datetime(2026, 6, 15, 16, tzinfo=UTC),
            },
        )
    ).scalar_one()
    version = (
        (
            await db.execute(
                text(
                    """
                    select id, template_item_id from checklist_template_item_versions
                     where template_id = :t and template_version = :v
                     order by sort_order limit 1
                    """
                ),
                {"t": row["template_id"], "v": row["version"]},
            )
        )
        .mappings()
        .one()
    )
    item_id = (
        await db.execute(
            text(
                """
                insert into checklist_run_items
                    (run_id, template_item_id, sort_order, template_item_version_id,
                     result, photo_path, photo_uploaded_at)
                values (:run_id, :tid, 0, :vid, cast(:result as item_result),
                        'test/photo.jpg', now())
                returning id
                """
            ),
            {
                "run_id": run_id,
                "tid": version["template_item_id"],
                "vid": version["id"],
                "result": result,
            },
        )
    ).scalar_one()
    await db.commit()
    return {
        "run_id": run_id,
        "item_id": item_id,
        "outlet_id": row["outlet_id"],
        "template_item_id": version["template_item_id"],
    }


def _stub_vision(
    monkeypatch: pytest.MonkeyPatch, *, verdict: str, confidence: float
) -> dict[str, object]:
    """Replace only the model call. Everything around it stays real."""
    seen: dict[str, object] = {}

    async def fake_review(**kwargs: object) -> vision.ReviewResult:
        seen.update(kwargs)
        return vision.ReviewResult(
            verdict=verdict,
            confidence=confidence,
            rationale="Grease visible along the back edge of the hob.",
            model="claude-opus-5",
            prompt_version=vision.PROMPT_VERSION,
            latency_ms=1234,
            compared_to_reference=kwargs.get("reference") is not None,
        )

    monkeypatch.setattr(ai_review.vision, "review", fake_review)

    async def fake_download(path: str) -> bytes:
        return b"\xff\xd8\xff" + path.encode()

    monkeypatch.setattr(ai_review.storage, "download_object", fake_download)
    return seen


async def _enable(db: AsyncSession) -> None:
    await db.execute(
        text(
            "insert into app_settings (key, scope, value, effective_from)"
            " values ('ai_review.enabled', 'global', 'true'::jsonb,"
            " now() - interval '1 hour')"
        )
    )
    await db.commit()


class TestReviewPhoto:
    async def test_disabled_by_default_makes_no_call_at_all(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ai_review.enabled defaults to false. Off must mean no bytes fetched
        and no request made, not a call whose result is discarded."""
        run = await _run_with_photo(session)
        seen = _stub_vision(monkeypatch, verdict="fail", confidence=0.9)

        result = await ai_review.review_photo(session, run["item_id"])
        assert result == {"skipped": "disabled"}
        assert seen == {}

    async def test_a_confident_fail_on_a_recorded_pass_writes_the_flag(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await _run_with_photo(session, result="pass")
        await _enable(session)
        _stub_vision(monkeypatch, verdict="fail", confidence=0.94)

        result = await ai_review.review_photo(session, run["item_id"])
        assert result["shown_as"] == "fail"
        assert result["flagged"] is True

        row = (
            (
                await session.execute(
                    text(
                        "select integrity_flags, integrity_detail"
                        " from checklist_run_items where id = :id"
                    ),
                    {"id": run["item_id"]},
                )
            )
            .mappings()
            .one()
        )
        assert IntegrityFlag.AI_MISMATCH.value in row["integrity_flags"]
        import json

        raw = row["integrity_detail"]
        detail = json.loads(raw) if isinstance(raw, str) else raw
        # The rationale travels with the flag: a red chip with no reason is
        # one a manager learns to ignore.
        assert "Grease visible" in detail["ai_mismatch"]["rationale"]

    async def test_a_low_confidence_fail_records_the_opinion_but_no_flag(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await _run_with_photo(session, result="pass")
        await _enable(session)
        _stub_vision(monkeypatch, verdict="fail", confidence=0.35)

        result = await ai_review.review_photo(session, run["item_id"])
        assert result["shown_as"] == "uncertain"
        assert result["flagged"] is False

        stored = (
            (
                await session.execute(
                    text(
                        "select verdict, cast(confidence as float8) as confidence"
                        " from run_item_ai_reviews where run_item_id = :id"
                    ),
                    {"id": run["item_id"]},
                )
            )
            .mappings()
            .one()
        )
        # Stored raw, downgraded only on the way out — so the record stays
        # readable against a threshold that has since moved.
        assert stored["verdict"] == "fail"
        assert stored["confidence"] == pytest.approx(0.35)

        flags = (
            await session.execute(
                text("select integrity_flags from checklist_run_items where id = :id"),
                {"id": run["item_id"]},
            )
        ).scalar_one()
        assert IntegrityFlag.AI_MISMATCH.value not in flags

    async def test_agreeing_with_a_recorded_fail_flags_nothing(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await _run_with_photo(session, result="fail")
        await _enable(session)
        _stub_vision(monkeypatch, verdict="fail", confidence=0.95)

        result = await ai_review.review_photo(session, run["item_id"])
        assert result["flagged"] is False

    async def test_it_uses_the_outlets_own_reference_when_there_is_one(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await _run_with_photo(session)
        await _enable(session)
        reference_id = (
            await session.execute(
                text(
                    """
                    insert into outlet_item_reference_photos
                        (outlet_id, template_item_id, photo_path, is_active)
                    values (:o, :t, 'reference/standard.jpg', true)
                    returning id
                    """
                ),
                {"o": run["outlet_id"], "t": run["template_item_id"]},
            )
        ).scalar_one()
        await session.commit()
        seen = _stub_vision(monkeypatch, verdict="pass", confidence=0.9)

        result = await ai_review.review_photo(session, run["item_id"])
        assert result["compared_to_reference"] is True
        assert seen["reference"] is not None

        stored = (
            await session.execute(
                text("select reference_photo_id from run_item_ai_reviews where run_item_id = :id"),
                {"id": run["item_id"]},
            )
        ).scalar_one()
        assert stored == reference_id

    async def test_without_a_reference_it_still_reviews_and_says_so(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Requiring a reference would mean nothing works until every station
        at every outlet has been photographed."""
        run = await _run_with_photo(session)
        await _enable(session)
        seen = _stub_vision(monkeypatch, verdict="pass", confidence=0.8)

        result = await ai_review.review_photo(session, run["item_id"])
        assert result["compared_to_reference"] is False
        assert seen["reference"] is None

        stored = (
            await session.execute(
                text("select reference_photo_id from run_item_ai_reviews where run_item_id = :id"),
                {"id": run["item_id"]},
            )
        ).scalar_one()
        assert stored is None

    async def test_an_unavailable_model_is_a_skip_not_a_fabricated_verdict(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await _run_with_photo(session)
        await _enable(session)
        _stub_vision(monkeypatch, verdict="pass", confidence=0.9)

        async def unavailable(**_: object) -> vision.ReviewResult:
            raise vision.VisionUnavailable("no key")

        monkeypatch.setattr(ai_review.vision, "review", unavailable)

        result = await ai_review.review_photo(session, run["item_id"])
        assert result["skipped"] == "unavailable"
        rows = (
            await session.execute(
                text("select count(*) from run_item_ai_reviews where run_item_id = :id"),
                {"id": run["item_id"]},
            )
        ).scalar_one()
        assert rows == 0

    async def test_rereviewing_replaces_rather_than_duplicates(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await _run_with_photo(session)
        await _enable(session)
        _stub_vision(monkeypatch, verdict="fail", confidence=0.9)
        await ai_review.review_photo(session, run["item_id"])
        _stub_vision(monkeypatch, verdict="pass", confidence=0.95)
        await ai_review.review_photo(session, run["item_id"])

        rows = (
            (
                await session.execute(
                    text("select verdict from run_item_ai_reviews where run_item_id = :id"),
                    {"id": run["item_id"]},
                )
            )
            .scalars()
            .all()
        )
        assert rows == ["pass"]

        # And the flag it had written is cleared, because the pass owns it now.
        flags = (
            await session.execute(
                text("select integrity_flags from checklist_run_items where id = :id"),
                {"id": run["item_id"]},
            )
        ).scalar_one()
        assert IntegrityFlag.AI_MISMATCH.value not in flags

    async def test_it_leaves_the_deterministic_flags_alone(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await _run_with_photo(session)
        await _enable(session)
        await session.execute(
            text("update checklist_run_items set integrity_flags = '{too_dark}' where id = :id"),
            {"id": run["item_id"]},
        )
        await session.commit()
        _stub_vision(monkeypatch, verdict="pass", confidence=0.95)

        await ai_review.review_photo(session, run["item_id"])
        flags = (
            await session.execute(
                text("select integrity_flags from checklist_run_items where id = :id"),
                {"id": run["item_id"]},
            )
        ).scalar_one()
        assert IntegrityFlag.TOO_DARK.value in flags


class TestReadingBack:
    async def test_latest_for_run_applies_the_threshold(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        run = await _run_with_photo(session)
        await _enable(session)
        _stub_vision(monkeypatch, verdict="fail", confidence=0.5)
        await ai_review.review_photo(session, run["item_id"])

        verdicts = await ai_review.latest_for_run(session, run["run_id"])
        entry = verdicts[run["item_id"]]
        assert entry["verdict"] == "fail"
        assert entry["shown_as"] == "uncertain"
        assert entry["uncertain_below"] == 0.7
        assert "Grease visible" in entry["rationale"]
