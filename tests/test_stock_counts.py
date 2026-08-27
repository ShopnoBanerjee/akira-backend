"""The count flow against a real database, with the model call stubbed.

What must hold: extraction lands raw text verbatim and derives beside it; a
Groq-extracted line is always review-bound; the human's correction writes an
alias exactly when asked; a half-reviewed count refuses to confirm; and the
requisition computes only from a confirmed count, arithmetic attached.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.deps import CurrentUser
from app.core.enums import UserRole
from app.domains.inventory import counts_service, requisitions_service
from app.integrations.sheet_extraction import (
    EXTRACTOR_VERSION,
    ExtractedPage,
    ExtractedRow,
    PageResult,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def session(migrated_db: str):  # type: ignore[no-untyped-def]
    engine = create_async_engine(migrated_db.replace("postgresql://", "postgresql+asyncpg://"))
    async with AsyncSession(engine, expire_on_commit=False) as db:
        try:
            yield db
        finally:
            await db.rollback()
            for table in (
                "requisition_lines",
                "requisitions",
                "stock_count_lines",
                "stock_counts",
                "inventory_item_aliases",
                "data_uploads",
            ):
                await db.execute(text(f"delete from {table}"))
            await db.commit()
    await engine.dispose()


async def _outlet(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(
        str((await db.execute(text("select id from outlets order by code limit 1"))).scalar_one())
    )


def owner(profile_id: uuid.UUID | None = None) -> CurrentUser:
    return CurrentUser(
        profile_id=profile_id or uuid.uuid4(),
        full_name="Owner",
        email=None,
        global_role=UserRole.OWNER,
        is_active=True,
    )


async def _profile(db: AsyncSession) -> uuid.UUID:
    pid = uuid.uuid4()
    await db.execute(
        text("insert into auth.users (id, email) values (:id, :e)"),
        {"id": pid, "e": f"{pid}@akira.test"},
    )
    await db.execute(
        text(
            "insert into profiles (id, full_name, global_role, is_active)"
            " values (:id, 'Counter', 'owner', true)"
        ),
        {"id": pid},
    )
    await db.commit()
    return pid


async def _items(db: AsyncSession, *names: str) -> dict[str, uuid.UUID]:
    rows = (
        (
            await db.execute(
                text("select id, name from inventory_items where name = any(:n)"),
                {"n": list(names)},
            )
        )
        .mappings()
        .all()
    )
    found = {r["name"]: uuid.UUID(str(r["id"])) for r in rows}
    assert set(found) == set(names), f"seed items missing: {set(names) - set(found)}"
    return found


async def _seed_count(
    db: AsyncSession,
    outlet: uuid.UUID,
    lines: list[dict[str, Any]],
    *,
    status: str = "review",
    confirmed_by: uuid.UUID | None = None,
) -> uuid.UUID:
    upload_id = (
        await db.execute(
            text(
                """
                insert into data_uploads
                    (outlet_id, source, original_filename, storage_path, file_sha256, status)
                values (:o, 'stock_sheet', 'sheet.jpg', :p, :sha, 'parsed')
                returning id
                """
            ),
            {"o": outlet, "p": f"{outlet}/x.jpg", "sha": f"sha-{uuid.uuid4()}"},
        )
    ).scalar_one()
    count_id = (
        await db.execute(
            text(
                """
                insert into stock_counts
                    (outlet_id, upload_id, business_date, status, confirmed_by,
                     confirmed_at, extractor)
                values (:o, :u, '2026-08-27', cast(:s as stock_count_status),
                        cast(:by as uuid),
                        case when cast(:by as uuid) is not null then now() end, :x)
                returning id
                """
            ),
            {"o": outlet, "u": upload_id, "s": status, "by": confirmed_by, "x": EXTRACTOR_VERSION},
        )
    ).scalar_one()
    for line in lines:
        await db.execute(
            text(
                """
                insert into stock_count_lines
                    (count_id, page, sl_no, raw_name, raw_closing, raw_requisition,
                     extract_confidence, item_id, match_method, qty, requested_qty,
                     needs_review)
                values (:c, 1, :sl, :name, :closing, :req, :conf, :item, :method,
                        :qty, :requested, :review)
                """
            ),
            {
                "c": count_id,
                "sl": line.get("sl", 1),
                "name": line["raw_name"],
                "closing": line.get("raw_closing"),
                "req": line.get("raw_requisition"),
                "conf": line.get("conf", 0.95),
                "item": line.get("item_id"),
                "method": line.get("method"),
                "qty": line.get("qty"),
                "requested": line.get("requested"),
                "review": line.get("needs_review", False),
            },
        )
    await db.commit()
    return uuid.UUID(str(count_id))


class TestExtractionPipeline:
    async def test_raw_lands_verbatim_and_derivations_sit_beside_it(
        self, session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole architecture in one test: the model's text is stored
        untouched, the mapping and the parse are separate columns, and a
        refused parse leaves qty null with the reason in parse_detail."""
        outlet = await _outlet(session)
        items = await _items(session, "Sweet Corn", "Black Pepper")

        page = ExtractedPage(
            sheet_date="2026-08-27",
            counted_at_label="3 PM",
            rows=[
                ExtractedRow(
                    sl_no=1,
                    item_name="Sweet Corn",
                    closing_count_raw="1.500",
                    requisition_raw="1kg",
                    confidence=0.95,
                ),
                ExtractedRow(
                    sl_no=2,
                    item_name="Black Pepper",
                    closing_count_raw="5pk",
                    requisition_raw=None,
                    confidence=0.9,
                ),
                ExtractedRow(
                    sl_no=3,
                    item_name="Mystery Sauce",
                    closing_count_raw="2",
                    requisition_raw=None,
                    confidence=0.4,
                ),
            ],
        )

        async def fake_extract(image_bytes: bytes, *, vocabulary: list[str]) -> PageResult:
            assert "Sweet Corn" in vocabulary  # the catalogue anchors the prompt
            return PageResult(
                page=page, model="stub", extractor_version=EXTRACTOR_VERSION, latency_ms=1
            )

        async def fake_download(path: str, *, bucket: str = "") -> bytes:
            # A 1x1 JPEG: enough for _page_images to treat it as one photo.
            import io

            from PIL import Image

            buf = io.BytesIO()
            Image.new("RGB", (400, 300)).save(buf, "JPEG")
            return buf.getvalue()

        monkeypatch.setattr(counts_service.sheet_extraction, "extract_page", fake_extract)
        monkeypatch.setattr(counts_service.storage, "download_object", fake_download)

        count_id = await _seed_count(session, outlet, [], status="extracting")
        result = await counts_service.extract_count(session, count_id)
        assert result["lines"] == 3

        lines = (
            (
                await session.execute(
                    text(
                        "select raw_name, raw_closing, qty, requested_qty, item_id,"
                        " match_method, needs_review, parse_detail"
                        " from stock_count_lines where count_id = :c order by sl_no"
                    ),
                    {"c": count_id},
                )
            )
            .mappings()
            .all()
        )
        corn, chilli, mystery = lines
        # Verbatim raw + derived number beside it, with the working.
        assert corn["raw_closing"] == "1.500" and float(corn["qty"]) == 1500
        assert float(corn["requested_qty"]) == 1000
        assert uuid.UUID(str(corn["item_id"])) == items["Sweet Corn"]
        # The refusal: raw kept, qty null, reason recorded, review required.
        assert chilli["raw_closing"] == "5pk" and chilli["qty"] is None
        assert chilli["parse_detail"]["closing"]["refused"] == "unit_mismatch"
        assert chilli["needs_review"] is True
        # The unknown item: unmapped, review required.
        assert mystery["item_id"] is None and mystery["needs_review"] is True

        header = (
            (
                await session.execute(
                    text(
                        "select status, business_date, counted_at_label, extractor"
                        " from stock_counts where id = :c"
                    ),
                    {"c": count_id},
                )
            )
            .mappings()
            .one()
        )
        assert header["status"] == "review"
        assert str(header["business_date"]) == "2026-08-27"  # the sheet's own date won
        assert header["counted_at_label"] == "3 PM"
        assert EXTRACTOR_VERSION in header["extractor"]


class TestStubProvider:
    def test_the_stub_replays_its_fixture_without_any_key(self) -> None:
        """CI and keyless local runs exercise the full extraction contract
        through this. The fixture deliberately contains one of everything the
        pipeline must handle: a thousands-dot, a compound, a unit mismatch, a
        blank, and an unknown item."""
        from app.integrations.sheet_extraction import _extract_stub

        result = _extract_stub()
        assert result.model == "stub"
        names = [row.item_name for row in result.page.rows]
        assert "Ginger" in names and "Mystery Sauce" in names
        ginger = next(r for r in result.page.rows if r.item_name == "Ginger")
        assert ginger.closing_count_raw == "1.500"  # verbatim survives the fixture
        compound = next(r for r in result.page.rows if r.item_name == "Button Mushroom")
        assert compound.closing_count_raw == "1kg 7pc"


class TestReviewAndConfirm:
    async def test_resolving_a_line_can_remember_the_spelling(self, session: AsyncSession) -> None:
        outlet = await _outlet(session)
        items = await _items(session, "Shitake Mushroom")
        reviewer = await _profile(session)
        count_id = await _seed_count(
            session,
            outlet,
            [{"raw_name": "Shiitake Mushroom", "raw_closing": "1kg", "needs_review": True}],
        )
        line_id = (
            await session.execute(
                text("select id from stock_count_lines where count_id = :c"), {"c": count_id}
            )
        ).scalar_one()

        user = owner()
        object.__setattr__(user, "profile_id", reviewer)
        await counts_service.review_line(
            session,
            user,
            count_id=count_id,
            line_id=uuid.UUID(str(line_id)),
            item_id=items["Shitake Mushroom"],
            qty=1000,
            requested_qty=None,
            remember_alias=True,
        )

        alias = (
            (
                await session.execute(
                    text("select item_id, alias from inventory_item_aliases where alias = :a"),
                    {"a": "shiitake mushroom"},
                )
            )
            .mappings()
            .first()
        )
        assert alias is not None
        assert uuid.UUID(str(alias["item_id"])) == items["Shitake Mushroom"]

        line = (
            (
                await session.execute(
                    text(
                        "select item_id, match_method, needs_review, qty"
                        " from stock_count_lines where id = :id"
                    ),
                    {"id": line_id},
                )
            )
            .mappings()
            .one()
        )
        assert line["match_method"] == "human" and line["needs_review"] is False
        assert float(line["qty"]) == 1000

    async def test_a_half_reviewed_count_refuses_to_confirm(self, session: AsyncSession) -> None:
        from app.core.errors import ValidationError

        outlet = await _outlet(session)
        items = await _items(session, "Sweet Corn")
        count_id = await _seed_count(
            session,
            outlet,
            [
                {"raw_name": "Sweet Corn", "item_id": items["Sweet Corn"], "qty": 800},
                {"raw_name": "??", "sl": 2, "needs_review": True},
            ],
        )
        with pytest.raises(ValidationError, match="need review"):
            await counts_service.confirm_count(session, owner(), count_id)

    async def test_a_clean_count_confirms_and_records_who(self, session: AsyncSession) -> None:
        outlet = await _outlet(session)
        items = await _items(session, "Sweet Corn")
        confirmer = await _profile(session)
        count_id = await _seed_count(
            session,
            outlet,
            [{"raw_name": "Sweet Corn", "item_id": items["Sweet Corn"], "qty": 800}],
        )
        user = owner()
        object.__setattr__(user, "profile_id", confirmer)
        result = await counts_service.confirm_count(session, user, count_id)
        assert result["status"] == "confirmed"


class TestRequisitionFromCount:
    async def test_only_a_confirmed_count_computes(self, session: AsyncSession) -> None:
        from app.core.errors import ValidationError

        outlet = await _outlet(session)
        items = await _items(session, "Sweet Corn")
        count_id = await _seed_count(
            session,
            outlet,
            [{"raw_name": "Sweet Corn", "item_id": items["Sweet Corn"], "qty": 100}],
        )
        with pytest.raises(ValidationError, match="confirmed"):
            await requisitions_service.build_from_count(session, owner(), count_id)

    async def test_the_arithmetic_lands_with_its_working(self, session: AsyncSession) -> None:
        outlet = await _outlet(session)
        items = await _items(session, "Sweet Corn", "Shitake Mushroom")
        confirmer = await _profile(session)

        # Par for one item only; the other must get no_par, never a guess.
        await session.execute(
            text(
                """
                insert into inventory_outlet_levels (outlet_id, item_id, par_level, order_unit)
                values (:o, :i, 2000, 500)
                on conflict (outlet_id, item_id)
                do update set par_level = 2000, order_unit = 500
                """
            ),
            {"o": outlet, "i": items["Sweet Corn"]},
        )
        await session.commit()

        count_id = await _seed_count(
            session,
            outlet,
            [
                {
                    "raw_name": "Sweet Corn",
                    "item_id": items["Sweet Corn"],
                    "qty": 400,
                    "requested": 3000,
                },
                {
                    "raw_name": "Shitake Mushroom",
                    "sl": 2,
                    "item_id": items["Shitake Mushroom"],
                    "qty": 1000,
                    "requested": 1000,
                },
            ],
            status="confirmed",
            confirmed_by=confirmer,
        )

        result = await requisitions_service.build_from_count(session, owner(confirmer), count_id)
        assert result["lines"] == 2

        lines = (
            (
                await session.execute(
                    text(
                        """
                        select i.name, cast(l.suggested_qty as float8) suggested,
                               cast(l.final_qty as float8) final, l.flags, l.detail
                          from requisition_lines l
                          join inventory_items i on i.id = l.item_id
                         where l.requisition_id = :r order by i.name
                        """
                    ),
                    {"r": uuid.UUID(result["requisition_id"])},
                )
            )
            .mappings()
            .all()
        )
        shitake, corn = lines
        # gap 1600 -> rounded up to 2000; requested 3000 > 1.3x -> padding.
        assert corn["suggested"] == 2000
        assert "padding" in corn["flags"]
        assert corn["final"] == 3000  # the chef still wins
        assert corn["detail"]["suggested"]["gap"] == 1600
        # No par: no number, a flag, chef's ask carried.
        assert shitake["suggested"] is None
        assert "no_par" in shitake["flags"]
        assert shitake["final"] == 1000

    async def test_one_requisition_per_count(self, session: AsyncSession) -> None:
        from app.core.errors import ConflictError

        outlet = await _outlet(session)
        items = await _items(session, "Sweet Corn")
        confirmer = await _profile(session)
        count_id = await _seed_count(
            session,
            outlet,
            [{"raw_name": "Sweet Corn", "item_id": items["Sweet Corn"], "qty": 100}],
            status="confirmed",
            confirmed_by=confirmer,
        )
        await requisitions_service.build_from_count(session, owner(confirmer), count_id)
        with pytest.raises(ConflictError, match="already exists"):
            await requisitions_service.build_from_count(session, owner(confirmer), count_id)
