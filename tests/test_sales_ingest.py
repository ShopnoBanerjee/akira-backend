"""Writing parsed bills into the database.

The parser has its own suite; this is about what happens after it. Two things
matter enough to hold down with a real database: the upsert must update rather
than duplicate when exports overlap, and no raw phone number may reach a
column.
"""

import uuid
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.enums import SalesChannel
from app.domains.sales import petpooja, service

pytestmark = pytest.mark.asyncio


def order(
    bill_no: str,
    *,
    net: int = 100_00,
    when: str = "2026-08-22 20:00:00",
    business: str = "2026-08-22",
    phone: str | None = None,
    channel: SalesChannel | None = SalesChannel.DINE_IN,
) -> petpooja.ParsedOrder:
    return petpooja.ParsedOrder(
        external_bill_no=bill_no,
        ordered_at=petpooja.parse_timestamp(when),
        business_date=date.fromisoformat(business),
        channel=channel,
        covers=2,
        gross_paise=net,
        discount_paise=0,
        tax_paise=0,
        net_paise=net,
        payment_mode="Cash",
        table_no=None,
        customer_phone_hash=phone,
    )


def result(*orders: petpooja.ParsedOrder) -> petpooja.ParseResult:
    return petpooja.ParseResult(orders=list(orders))


class TestPureHelpers:
    def test_the_storage_path_is_keyed_by_content(self) -> None:
        """Same bytes, same place. Re-uploading overwrites rather than
        accumulating a copy per attempt."""
        outlet = uuid.uuid4()
        assert service.storage_path_for(outlet, "abc123") == f"{outlet}/abc123.xlsx"

    def test_the_phone_hash_is_stable_and_salted(self) -> None:
        digest = service.phone_hasher()
        assert digest("5550000001") == digest("5550000001")
        assert digest("5550000001") != digest("5550000002")
        assert len(digest("5550000001")) == 64
        # The raw number must not be recoverable by looking at it.
        assert "5550000001" not in digest("5550000001")


@pytest.fixture
async def session(migrated_db: str):  # type: ignore[no-untyped-def]
    engine = create_async_engine(migrated_db.replace("postgresql://", "postgresql+asyncpg://"))
    async with AsyncSession(engine, expire_on_commit=False) as db:
        try:
            yield db
        finally:
            await db.rollback()
            await db.execute(text("delete from sales_orders"))
            await db.execute(text("delete from data_uploads"))
            await db.commit()
    await engine.dispose()


async def _outlet(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(
        str((await db.execute(text("select id from outlets order by code limit 1"))).scalar_one())
    )


async def _upload(db: AsyncSession, outlet_id: uuid.UUID, sha: str) -> uuid.UUID:
    row = await db.execute(
        text(
            """
            insert into data_uploads
                (outlet_id, source, original_filename, storage_path, file_sha256, status)
            values (:o, 'petpooja_orders', 'export.xlsx', :p, :sha, 'received')
            returning id
            """
        ),
        {"o": outlet_id, "p": f"{outlet_id}/{sha}.xlsx", "sha": sha},
    )
    await db.commit()
    return uuid.UUID(str(row.scalar_one()))


class TestWritingOrders:
    async def test_it_writes_every_bill_once(self, session: AsyncSession) -> None:
        outlet = await _outlet(session)
        upload = await _upload(session, outlet, "sha-a")

        written = await service._write_orders(
            session, outlet, upload, result(order("1"), order("2"), order("3"))
        )
        await session.commit()

        assert written == {"inserted": 3, "updated": 0}
        assert (await session.execute(text("select count(*) from sales_orders"))).scalar_one() == 3

    async def test_an_overlapping_export_updates_rather_than_duplicates(
        self, session: AsyncSession
    ) -> None:
        """Petpooja exports overlap — the next one covers the same six weeks
        plus a few more days. Duplicating would double six weeks of revenue."""
        outlet = await _outlet(session)
        first = await _upload(session, outlet, "sha-a")
        await service._write_orders(session, outlet, first, result(order("1"), order("2")))
        await session.commit()

        second = await _upload(session, outlet, "sha-b")
        written = await service._write_orders(
            session, outlet, second, result(order("2"), order("3"))
        )
        await session.commit()

        assert written == {"inserted": 1, "updated": 1}
        assert (await session.execute(text("select count(*) from sales_orders"))).scalar_one() == 3

    async def test_a_corrected_bill_overwrites_the_old_total(self, session: AsyncSession) -> None:
        """Skipping a bill already present would leave a corrected total
        showing its old value for good."""
        outlet = await _outlet(session)
        first = await _upload(session, outlet, "sha-a")
        await service._write_orders(session, outlet, first, result(order("7", net=100_00)))
        await session.commit()

        second = await _upload(session, outlet, "sha-b")
        await service._write_orders(session, outlet, second, result(order("7", net=250_00)))
        await session.commit()

        net = (
            await session.execute(
                text("select net_paise from sales_orders where external_bill_no = '7'")
            )
        ).scalar_one()
        assert net == 250_00

    async def test_the_business_date_is_what_the_parser_decided(
        self, session: AsyncSession
    ) -> None:
        """Never re-derived from ordered_at::date here. A bill at 00:45 on the
        23rd is stored against the 22nd."""
        outlet = await _outlet(session)
        upload = await _upload(session, outlet, "sha-a")
        await service._write_orders(
            session,
            outlet,
            upload,
            result(order("1", when="2026-08-23 00:45:00", business="2026-08-22")),
        )
        await session.commit()

        row = (
            (
                await session.execute(
                    text(
                        "select business_date, ordered_at from sales_orders"
                        " where external_bill_no = '1'"
                    )
                )
            )
            .mappings()
            .one()
        )
        assert row["business_date"] == date(2026, 8, 22)

    async def test_a_null_channel_is_stored_rather_than_refused(
        self, session: AsyncSession
    ) -> None:
        """An unrecognised order type warns and leaves the channel unset. The
        bill and its money still count."""
        outlet = await _outlet(session)
        upload = await _upload(session, outlet, "sha-a")
        await service._write_orders(session, outlet, upload, result(order("1", channel=None)))
        await session.commit()

        row = (
            (
                await session.execute(
                    text("select channel, net_paise from sales_orders where external_bill_no='1'")
                )
            )
            .mappings()
            .one()
        )
        assert row["channel"] is None
        assert row["net_paise"] == 100_00

    async def test_only_a_digest_ever_reaches_the_column(self, session: AsyncSession) -> None:
        outlet = await _outlet(session)
        upload = await _upload(session, outlet, "sha-a")
        digest = service.phone_hasher()("5550000001")
        await service._write_orders(session, outlet, upload, result(order("1", phone=digest)))
        await session.commit()

        stored = (
            await session.execute(
                text("select customer_phone_hash from sales_orders where external_bill_no='1'")
            )
        ).scalar_one()
        assert stored == digest
        assert len(stored) == 64

        raw = (
            await session.execute(
                text(r"select count(*) from sales_orders where customer_phone_hash ~ '^[0-9]{10}$'")
            )
        ).scalar_one()
        assert raw == 0

    async def test_a_large_batch_goes_in_chunks(self, session: AsyncSession) -> None:
        """The per-row loop this replaced took 75 seconds for 452 bills against
        Supabase. Whatever the chunk size, the counts must still be exact."""
        outlet = await _outlet(session)
        upload = await _upload(session, outlet, "sha-a")
        many = result(*[order(str(n)) for n in range(1, service.WRITE_CHUNK + 51)])

        written = await service._write_orders(session, outlet, upload, many)
        await session.commit()

        assert written["inserted"] == service.WRITE_CHUNK + 50
        assert (
            await session.execute(text("select count(*) from sales_orders"))
        ).scalar_one() == service.WRITE_CHUNK + 50


class TestDailyTotals:
    async def test_it_groups_by_trading_day(self, session: AsyncSession) -> None:
        """Two bills either side of midnight on the same trading night must
        land on one row, not two."""
        from app.core.deps import CurrentUser
        from app.core.enums import UserRole

        outlet = await _outlet(session)
        upload = await _upload(session, outlet, "sha-a")
        await service._write_orders(
            session,
            outlet,
            upload,
            result(
                order("1", when="2026-08-22 23:30:00", business="2026-08-22", net=100_00),
                order("2", when="2026-08-23 00:45:00", business="2026-08-22", net=150_00),
            ),
        )
        await session.commit()

        owner = CurrentUser(
            profile_id=uuid.uuid4(),
            full_name="Owner",
            email=None,
            global_role=UserRole.OWNER,
            is_active=True,
        )
        totals = await service.daily_totals(
            session, owner, outlet_id=outlet, date_from=None, date_to=None
        )
        assert len(totals) == 1
        assert totals[0]["business_date"] == date(2026, 8, 22)
        assert totals[0]["bills"] == 2
        assert totals[0]["net_paise"] == 250_00


class TestUploadValidation:
    async def test_a_non_xlsx_is_refused(self, session: AsyncSession) -> None:
        from app.core.deps import CurrentUser
        from app.core.enums import UserRole
        from app.core.errors import ValidationError

        outlet = await _outlet(session)
        owner = CurrentUser(
            profile_id=uuid.uuid4(),
            full_name="Owner",
            email=None,
            global_role=UserRole.OWNER,
            is_active=True,
        )
        with pytest.raises(ValidationError, match=r"\.xlsx"):
            await service.create_upload(
                session,
                owner,
                outlet_id=outlet,
                filename="sales.csv",
                content_type="text/csv",
                data=b"a,b,c",
            )

    async def test_an_empty_file_is_refused(self, session: AsyncSession) -> None:
        from app.core.deps import CurrentUser
        from app.core.enums import UserRole
        from app.core.errors import ValidationError

        outlet = await _outlet(session)
        owner = CurrentUser(
            profile_id=uuid.uuid4(),
            full_name="Owner",
            email=None,
            global_role=UserRole.OWNER,
            is_active=True,
        )
        with pytest.raises(ValidationError, match="empty"):
            await service.create_upload(
                session,
                owner,
                outlet_id=outlet,
                filename="sales.xlsx",
                content_type="application/octet-stream",
                data=b"",
            )

    async def test_an_outlet_you_cannot_see_is_refused(self, session: AsyncSession) -> None:
        from app.core.deps import CurrentUser
        from app.core.enums import UserRole
        from app.core.errors import ForbiddenError

        outlet = await _outlet(session)
        stranger = CurrentUser(
            profile_id=uuid.uuid4(),
            full_name="Other Manager",
            email=None,
            global_role=UserRole.OUTLET_MANAGER,
            is_active=True,
            memberships=[],
        )
        with pytest.raises(ForbiddenError, match="access to that outlet"):
            await service.create_upload(
                session,
                stranger,
                outlet_id=outlet,
                filename="sales.xlsx",
                content_type="application/octet-stream",
                data=b"x" * 10,
            )
