"""The inventory and guest pillars, the shared arithmetic, and the blend.

Pure modules first, each with the case that would embarrass it — an
unmeasured pillar scoring zero instead of None, a pending component dragging
a weight in, the blend punishing an outlet for a pillar nobody measured —
then the two aggregate services against a real database.
"""

import uuid
from datetime import date as date_type

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core import pillar_math
from app.domains.dashboard.health import PillarReading, blended_health
from app.domains.inventory.pillar import InventoryInputs, InventoryTargets, inventory_pillar
from app.domains.sales.guest_pillar import GuestInputs, GuestTargets, guest_pillar

pytestmark = pytest.mark.asyncio

GREEN, AMBER = 85.0, 70.0


def inv_targets(**overrides: float) -> InventoryTargets:
    base: dict[str, float] = {
        "clean_req_share": 0.9,
        "max_stockouts_28d": 2,
        "max_variance": 0.2,
        "w_requisition": 0.4,
        "w_stockouts": 0.3,
        "w_variance": 0.3,
        "green": GREEN,
        "amber": AMBER,
    }
    base.update(overrides)
    return InventoryTargets(**base)  # type: ignore[arg-type]


def guest_targets(**overrides: float) -> GuestTargets:
    base: dict[str, float] = {"repeat_rate": 0.2, "w_repeat": 1.0, "green": GREEN, "amber": AMBER}
    base.update(overrides)
    return GuestTargets(**base)  # type: ignore[arg-type]


class TestPillarMath:
    def test_weights_renormalise_over_what_is_live(self) -> None:
        """Two live components at 0.6/0.4 must read on the same 0-100 scale
        as six would — a pillar is not penalised for its declared gaps."""
        comps = [
            pillar_math.Component(
                key="a",
                label="A",
                value=1.0,
                display="",
                target_display="",
                score=100.0,
                weight=0.6,
                contribution=60.0,
                band="green",
            ),
            pillar_math.Component(
                key="b",
                label="B",
                value=1.0,
                display="",
                target_display="",
                score=50.0,
                weight=0.4,
                contribution=20.0,
                band="red",
            ),
            pillar_math.pending("c", "C", "not built"),
        ]
        pillar = pillar_math.weighted(comps, green=GREEN, amber=AMBER)
        assert pillar.score == 80.0  # (100*.6 + 50*.4) / 1.0

    def test_only_pendings_and_monitors_means_not_measured(self) -> None:
        comps = [
            pillar_math.pending("a", "A", "no data"),
            pillar_math.monitor("b", "B", 0.5, "50%", "no target"),
        ]
        pillar = pillar_math.weighted(comps, green=GREEN, amber=AMBER)
        assert pillar.score is None and pillar.band == "none"
        assert pillar.detail["reason"] == "not_measured"

    def test_the_worst_component_ignores_monitors(self) -> None:
        comps = [
            pillar_math.Component(
                key="a",
                label="A",
                value=1.0,
                display="",
                target_display="",
                score=90.0,
                weight=1.0,
                contribution=90.0,
                band="green",
            ),
            pillar_math.monitor("m", "M", 0.1, "10%", "watched"),
        ]
        pillar = pillar_math.weighted(comps, green=GREEN, amber=AMBER)
        assert pillar.worst_component is not None
        assert pillar.worst_component.key == "a"


class TestInventoryPillar:
    def test_clean_requisitions_and_no_stockouts_score_100(self) -> None:
        pillar = inventory_pillar(
            InventoryInputs(
                period_days=28, req_lines=10, padded_lines=0, counted_lines=40, stockout_lines=0
            ),
            inv_targets(),
        )
        assert pillar.score == 100.0 and pillar.band == "green"

    def test_padding_drags_the_accuracy_component(self) -> None:
        pillar = inventory_pillar(
            InventoryInputs(
                period_days=28, req_lines=10, padded_lines=5, counted_lines=40, stockout_lines=0
            ),
            inv_targets(),
        )
        acc = next(c for c in pillar.components if c.key == "requisition_accuracy")
        # 50% clean vs a 90% target -> 55.6
        assert acc.score == 55.6 and acc.band == "red"

    def test_stockouts_are_normalised_per_28_days(self) -> None:
        """Two zeroes in a 7-day view is an 8-per-28d rate, not a pass."""
        pillar = inventory_pillar(
            InventoryInputs(
                period_days=7, req_lines=5, padded_lines=0, counted_lines=40, stockout_lines=2
            ),
            inv_targets(),
        )
        so = next(c for c in pillar.components if c.key == "stockouts")
        assert so.value == 8.0 and so.band == "red"
        assert so.score == 25.0  # min(100, 100 * 2/8)

    def test_nothing_measured_scores_none_not_zero(self) -> None:
        pillar = inventory_pillar(
            InventoryInputs(
                period_days=28, req_lines=0, padded_lines=0, counted_lines=0, stockout_lines=0
            ),
            inv_targets(),
        )
        assert pillar.score is None and pillar.band == "none"
        assert all(c.status == "pending" for c in pillar.components)

    def test_the_two_spec_gaps_are_declared_not_hidden(self) -> None:
        pillar = inventory_pillar(
            InventoryInputs(
                period_days=28, req_lines=10, padded_lines=0, counted_lines=40, stockout_lines=0
            ),
            inv_targets(),
        )
        keys = {c.key for c in pillar.components if c.status == "pending"}
        assert keys == {"theoretical_variance", "wastage"}


class TestGuestPillar:
    def test_repeat_rate_scores_against_target(self) -> None:
        pillar = guest_pillar(
            GuestInputs(
                bills=100,
                identified_customers=50,
                repeat_customers=5,
                peak_net_paise=50_000,
                net_paise=100_000,
            ),
            guest_targets(),
        )
        # 10% vs 20% target -> 50, the only weighted component.
        assert pillar.score == 50.0
        repeat = next(c for c in pillar.components if c.key == "repeat_rate")
        assert repeat.display.startswith("10% of 50")

    def test_peak_share_is_a_monitor_never_a_score(self) -> None:
        pillar = guest_pillar(
            GuestInputs(
                bills=100,
                identified_customers=50,
                repeat_customers=10,
                peak_net_paise=90_000,
                net_paise=100_000,
            ),
            guest_targets(),
        )
        peak = next(c for c in pillar.components if c.key == "peak_share")
        assert peak.status == "monitor" and peak.score is None and peak.weight == 0
        # A 90% peak share must not move the pillar either way.
        assert pillar.score == 100.0

    def test_no_identified_customers_means_not_measured(self) -> None:
        """An outlet that never captures phones has an unmeasured guest
        pillar, not a zero — the capture gap is already scored in sales."""
        pillar = guest_pillar(
            GuestInputs(
                bills=100,
                identified_customers=0,
                repeat_customers=0,
                peak_net_paise=1,
                net_paise=2,
            ),
            guest_targets(),
        )
        assert pillar.score is None and pillar.band == "none"


class TestBlendedHealth:
    def pillar(self, key: str, weight: float, score: float | None) -> PillarReading:
        return PillarReading(key=key, label=key, weight=weight, score=score)

    def test_all_four_measured_blend_by_spec_weights(self) -> None:
        health = blended_health(
            [
                self.pillar("sales", 30, 80.0),
                self.pillar("sop", 30, 90.0),
                self.pillar("inventory", 25, 60.0),
                self.pillar("guest", 15, 40.0),
            ],
            green=GREEN,
            amber=AMBER,
        )
        # (80*30 + 90*30 + 60*25 + 40*15) / 100 = 72.0
        assert health.score == 72.0 and health.band == "amber"
        assert health.weights_used == 100 and health.unmeasured == []

    def test_an_unmeasured_pillar_leaves_the_denominator(self) -> None:
        """No confirmed counts yet must not read as a failing inventory."""
        health = blended_health(
            [
                self.pillar("sales", 30, 80.0),
                self.pillar("sop", 30, 90.0),
                self.pillar("inventory", 25, None),
                self.pillar("guest", 15, 40.0),
            ],
            green=GREEN,
            amber=AMBER,
        )
        # (80*30 + 90*30 + 40*15) / 75 = 76.0
        assert health.score == 76.0
        assert health.weights_used == 75 and health.unmeasured == ["inventory"]

    def test_nothing_measured_is_none_not_zero(self) -> None:
        health = blended_health(
            [self.pillar("sales", 30, None), self.pillar("sop", 30, None)],
            green=GREEN,
            amber=AMBER,
        )
        assert health.score is None and health.band == "none"


# ---------------------------------------------------------------------------
# The aggregate services, against a real database
# ---------------------------------------------------------------------------


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
                "sales_orders",
                "data_uploads",
            ):
                await db.execute(text(f"delete from {table}"))
            await db.commit()
    await engine.dispose()


async def _outlet(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(
        str((await db.execute(text("select id from outlets order by code limit 1"))).scalar_one())
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
            " values (:id, 'Pillar Test', 'owner', true)"
        ),
        {"id": pid},
    )
    await db.commit()
    return pid


class TestInventoryInputs:
    async def test_it_reads_padding_and_stockouts_inside_the_period(
        self, session: AsyncSession
    ) -> None:
        from app.domains.inventory import pillar_service

        outlet = await _outlet(session)
        who = await _profile(session)
        item, item2 = [
            r[0]
            for r in await session.execute(
                text("select id from inventory_items order by name limit 2")
            )
        ]

        upload = (
            await session.execute(
                text(
                    """
                    insert into data_uploads
                        (outlet_id, source, original_filename, storage_path, file_sha256, status)
                    values (:o, 'stock_sheet', 's.jpg', :p, :sha, 'parsed') returning id
                    """
                ),
                {"o": outlet, "p": f"{outlet}/x.jpg", "sha": f"sha-{uuid.uuid4()}"},
            )
        ).scalar_one()
        count = (
            await session.execute(
                text(
                    """
                    insert into stock_counts
                        (outlet_id, upload_id, business_date, status, confirmed_by, confirmed_at)
                    values (:o, :u, cast(:d as date), 'confirmed', :by, now()) returning id
                    """
                ),
                {"o": outlet, "u": upload, "d": date_type(2026, 8, 22), "by": who},
            )
        ).scalar_one()
        # Two lines: one healthy, one at zero — the stockout.
        for sl, qty in ((1, 500), (2, 0)):
            await session.execute(
                text(
                    """
                    insert into stock_count_lines
                        (count_id, page, sl_no, raw_name, item_id, match_method, qty, needs_review)
                    values (:c, 1, :sl, 'seed', :item, 'human', :qty, false)
                    """
                ),
                {"c": count, "sl": sl, "item": item, "qty": qty},
            )
        req = (
            await session.execute(
                text(
                    """
                    insert into requisitions
                        (outlet_id, count_id, business_date, status, created_by,
                         finalised_by, finalised_at)
                    values (:o, :c, cast(:d as date), 'final', :by, :by, now()) returning id
                    """
                ),
                {"o": outlet, "c": count, "d": date_type(2026, 8, 23), "by": who},
            )
        ).scalar_one()
        # Two finalised lines, one padded.
        await session.execute(
            text(
                """
                insert into requisition_lines
                    (requisition_id, item_id, suggested_qty, requested_qty, final_qty, flags)
                values (:r, :i, 200, 3000, 3000, '{padding}'),
                       (:r, :j, 200, 200, 200, '{}')
                """
            ),
            {"r": req, "i": item, "j": item2},
        )
        await session.commit()

        inputs = await pillar_service.inventory_inputs_many(
            session,
            outlet_ids=[outlet],
            start=date_type(2026, 8, 1),
            end=date_type(2026, 8, 28),
        )
        got = inputs[outlet]
        assert got.req_lines == 2 and got.padded_lines == 1
        assert got.counted_lines == 2 and got.stockout_lines == 1

        # Outside the period, nothing leaks in.
        empty = await pillar_service.inventory_inputs_many(
            session,
            outlet_ids=[outlet],
            start=date_type(2026, 7, 1),
            end=date_type(2026, 7, 28),
        )
        assert empty[outlet].req_lines == 0 and empty[outlet].counted_lines == 0


class TestGuestInputs:
    async def test_a_repeat_is_two_trading_days_not_two_bills(self, session: AsyncSession) -> None:
        """Two bills the same night is one visit with a split order; the
        repeat that matters is coming BACK."""
        from app.domains.sales import pillar_service

        outlet = await _outlet(session)
        upload = (
            await session.execute(
                text(
                    """
                    insert into data_uploads
                        (outlet_id, source, original_filename, storage_path, file_sha256, status)
                    values (:o, 'petpooja_orders', 'e.xlsx', :p, :sha, 'parsed') returning id
                    """
                ),
                {"o": outlet, "p": f"{outlet}/y.xlsx", "sha": f"sha-{uuid.uuid4()}"},
            )
        ).scalar_one()

        async def bill(no: str, day: str, hour: int, phone: str | None) -> None:
            await session.execute(
                text(
                    """
                    insert into sales_orders
                        (outlet_id, upload_id, external_bill_no, business_date, ordered_at,
                         gross_paise, discount_paise, tax_paise, net_paise, customer_phone_hash)
                    values (:o, :u, :no, cast(:d as date),
                            -- Wall-clock at the outlet, stamped as IST — a
                            -- naive timestamp would be read as UTC and land
                            -- 5.5 hours outside the peak window.
                            (cast(:d as date) + make_interval(hours => :h))
                                at time zone 'Asia/Kolkata',
                            10000, 0, 0, 10000, :ph)
                    """
                ),
                {
                    "o": outlet,
                    "u": upload,
                    "no": no,
                    # A str here trips asyncpg's "no attribute toordinal" —
                    # the date must be a date.
                    "d": date_type.fromisoformat(day),
                    "h": hour,
                    "ph": phone,
                },
            )

        # Customer A: two different trading nights -> a repeat.
        await bill("1", "2026-08-20", 20, "hash-a")
        await bill("2", "2026-08-22", 21, "hash-a")
        # Customer B: two bills, one night -> not a repeat.
        await bill("3", "2026-08-21", 19, "hash-b")
        await bill("4", "2026-08-21", 22, "hash-b")
        # An anonymous bill: invisible to identification.
        await bill("5", "2026-08-21", 20, None)
        await session.commit()

        inputs = await pillar_service.guest_inputs_many(
            session,
            outlet_ids=[outlet],
            start=date_type(2026, 8, 1),
            end=date_type(2026, 8, 28),
        )
        got = inputs[outlet]
        assert got.identified_customers == 2
        assert got.repeat_customers == 1
        assert got.bills == 5
        # Bills 1, 2, 4 and 5 sit in 20:00-22:59; bill 3 at 19:00 does not.
        assert got.peak_net_paise == 40000
        assert got.net_paise == 50000
