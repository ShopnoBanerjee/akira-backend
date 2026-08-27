"""The Petpooja Orders Master Report adapter.

Two kinds of test here, and the split is deliberate.

Most of them build the spreadsheet in code. That keeps the input to each case
visible in the test rather than buried in a binary, and — the reason that
matters more — AKIRA's real export carries 141 customer phone numbers and 142
names. It must never become a committed fixture.

The last class parses the real file when it happens to be on the machine and
skips when it is not, the same way the database tests skip without Postgres.
That is where the spec's two acceptance facts are actually checked: 452 bills
totalling Rs 4,86,076, and a bill after midnight landing on the previous
trading day.
"""

import hashlib
import pathlib
from datetime import date, datetime
from io import BytesIO
from typing import Any

import pytest

from app.core.enums import SalesChannel
from app.domains.sales import petpooja
from app.domains.sales.petpooja import UnreadableExport, parse_orders, to_paise

#: Where the real export sits on the machine it was exported on.
REAL_EXPORT = pathlib.Path("C:/Users/KIIT/Downloads/Orders_Master_Report_2026_08_25_20_46_24.xlsx")

HEADERS = [
    "Invoice No.",
    "Date",
    "Biller",
    "KOT No.",
    "Payment Type",
    "Payment Description",
    "Order Type",
    "Status",
    "Area",
    "Sub Order Type",
    "Group Name",
    "Brand Name",
    "GSTIN",
    "Assign To",
    "Phone",
    "Name",
    "Address",
    "Locality",
    "Persons",
    "Order Cancel Reason",
    "My Amount (₹)",
    "Discount (₹)",
    "Net Sales (₹)(M.A - D)",
    "Delivery Charge",
    "Container Charge",
    "Service Charge",
    "Additional Charge",
    "Deduction Charge",
    "Total Tax (₹)",
    "Round Off",
    "Waived off",
    "Total (₹)",
]

AT = {name: i for i, name in enumerate(HEADERS)}


def bill(
    no: str,
    when: str,
    *,
    net: float = 100.0,
    gross: float | None = None,
    discount: float = 0.0,
    tax: float = 0.0,
    order_type: str = "Dine In",
    status: str = "Success",
    persons: object = None,
    phone: object = None,
    payment: str = "Cash",
) -> list[Any]:
    row: list[Any] = [None] * len(HEADERS)
    row[AT["Invoice No."]] = no
    row[AT["Date"]] = when
    row[AT["Order Type"]] = order_type
    row[AT["Status"]] = status
    row[AT["Persons"]] = persons
    row[AT["Phone"]] = phone
    row[AT["Payment Type"]] = payment
    row[AT["My Amount (₹)"]] = net if gross is None else gross
    row[AT["Discount (₹)"]] = discount
    row[AT["Net Sales (₹)(M.A - D)"]] = net
    row[AT["Total Tax (₹)"]] = tax
    return row


def export(
    bills: list[list[Any]],
    *,
    period: str = "2026-07-17 to 2026-08-25",
    headers: list[str] | None = None,
    with_summary: bool = True,
) -> bytes:
    """An .xlsx with the exact shape Petpooja produces."""
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Date:", period])
    ws.append(["Name:", "Orders: Master Report"])
    ws.append(["Restaurant Name:", "Akira"])
    ws.append([])
    ws.append([])
    ws.append(headers if headers is not None else HEADERS)
    if with_summary:
        # The file's own Total/Min/Max/Avg rows. These are not bills.
        for label, amount in [
            ("Total", 999999.0),
            ("Min.", 1.0),
            ("Max.", 5000.0),
            ("Avg.", 900.0),
        ]:
            row: list[Any] = [None] * len(HEADERS)
            row[0] = label
            row[AT["Net Sales (₹)(M.A - D)"]] = amount
            ws.append(row)
    for b in bills:
        ws.append(b)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def sha(digits: str) -> str:
    return hashlib.sha256(("salt" + digits).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestToPaise:
    def test_whole_rupees(self) -> None:
        assert to_paise(1077.0) == 107700

    def test_the_float_trap(self) -> None:
        """int(81.6 * 100) is 8159. Seven cells in AKIRA's real export lose a
        paisa that way, and a paisa per bill is a rupee a night that never
        reconciles."""
        assert to_paise(81.6) == 8160
        assert to_paise(2594.7) == 259470
        assert to_paise(136.7) == 13670
        assert to_paise(486076.35) == 48607635

    def test_blank_is_zero(self) -> None:
        assert to_paise(None) == 0
        assert to_paise("") == 0

    def test_it_survives_a_formatted_string(self) -> None:
        assert to_paise("1,077.50") == 107750
        assert to_paise("₹ 81.6") == 8160

    def test_negative_amounts_are_kept(self) -> None:
        """A refund is a real number, not a parse error."""
        assert to_paise(-250.5) == -25050


class TestParseTimestamp:
    def test_the_format_petpooja_writes(self) -> None:
        got = petpooja.parse_timestamp("2026-08-25 20:37:10")
        assert (got.year, got.month, got.day, got.hour, got.minute) == (2026, 8, 25, 20, 37)

    def test_a_real_datetime_cell(self) -> None:
        got = petpooja.parse_timestamp(datetime(2026, 8, 25, 20, 37, 10))
        assert got.hour == 20

    def test_it_is_stamped_as_outlet_local(self) -> None:
        """The export has no zone on it; it is wall-clock at the restaurant.
        Reading it as UTC would move every late bill onto the wrong day."""
        assert petpooja.parse_timestamp("2026-08-25 20:37:10").utcoffset() is not None

    def test_an_unreadable_cell_says_so(self) -> None:
        with pytest.raises(ValueError, match="unrecognised timestamp"):
            petpooja.parse_timestamp("last Tuesday")


class TestParsePeriod:
    def test_a_range(self) -> None:
        assert petpooja.parse_period("2026-07-17 to 2026-08-25") == (
            date(2026, 7, 17),
            date(2026, 8, 25),
        )

    def test_a_single_day(self) -> None:
        assert petpooja.parse_period("2026-08-25 to 2026-08-25") == (
            date(2026, 8, 25),
            date(2026, 8, 25),
        )

    def test_nothing_recognisable(self) -> None:
        assert petpooja.parse_period("all time") == (None, None)


# ---------------------------------------------------------------------------
# The whole adapter, against spreadsheets built in code
# ---------------------------------------------------------------------------


class TestShape:
    def test_it_reads_the_preamble(self) -> None:
        result = parse_orders(export([bill("1", "2026-08-25 20:00:00")]))
        assert result.period_start == date(2026, 7, 17)
        assert result.period_end == date(2026, 8, 25)
        assert result.restaurant == "Akira"
        assert result.adapter_version == petpooja.ORDERS_ADAPTER_VERSION

    def test_it_skips_the_files_own_summary_rows(self) -> None:
        """The single most damaging mistake available here. Total/Min/Max/Avg
        sit between the header and the first bill; ingesting them adds a
        ten-lakh order that looks entirely plausible."""
        result = parse_orders(export([bill("1", "2026-08-25 20:00:00", net=250.0)]))
        assert len(result.orders) == 1
        assert result.net_paise == 25000
        assert all(o.external_bill_no == "1" for o in result.orders)

    def test_the_files_own_total_is_kept_beside_our_sum(self) -> None:
        """The Total row is skipped as a bill but its Net Sales is recorded —
        it is the export's own claim, and a disagreement between it and our
        sum should be a visible column pair, not a hand recomputation.

        The gap this pins down: the 0014 column existed for two epics with
        nothing filling it, found only when a fresh export arrived."""
        result = parse_orders(export([bill("1", "2026-08-25 20:00:00", net=250.0)]))
        # The builder's summary Total says 999999.0 — deliberately NOT the sum
        # of the bills, so this also proves the two numbers are independent.
        assert result.reported_net_paise == 99999900
        assert result.net_paise == 25000

    def test_no_total_row_means_no_claim(self) -> None:
        result = parse_orders(export([bill("1", "2026-08-25 20:00:00")], with_summary=False))
        assert result.reported_net_paise is None

    def test_it_finds_the_header_wherever_it_sits(self) -> None:
        """The preamble length already differs between Petpooja report types,
        so the header is searched for rather than assumed at a fixed offset."""
        result = parse_orders(export([bill("1", "2026-08-25 20:00:00")], with_summary=False))
        assert len(result.orders) == 1

    def test_blank_rows_are_ignored(self) -> None:
        result = parse_orders(
            export(
                [
                    bill("1", "2026-08-25 20:00:00"),
                    [None] * len(HEADERS),
                    bill("2", "2026-08-25 21:00:00"),
                ]
            )
        )
        assert [o.external_bill_no for o in result.orders] == ["1", "2"]


class TestFields:
    def test_money_lands_in_paise(self) -> None:
        result = parse_orders(
            export(
                [
                    bill(
                        "1",
                        "2026-08-25 20:00:00",
                        gross=2450.0,
                        discount=81.6,
                        net=2368.4,
                        tax=118.42,
                    )
                ]
            )
        )
        order = result.orders[0]
        assert (order.gross_paise, order.discount_paise, order.net_paise, order.tax_paise) == (
            245000,
            8160,
            236840,
            11842,
        )

    def test_order_types_map_to_channels(self) -> None:
        result = parse_orders(
            export(
                [
                    bill("1", "2026-08-25 20:00:00", order_type="Dine In"),
                    bill("2", "2026-08-25 20:01:00", order_type="Pick Up"),
                    bill("3", "2026-08-25 20:02:00", order_type="Delivery(Parcel)"),
                ]
            )
        )
        assert [o.channel for o in result.orders] == [
            SalesChannel.DINE_IN,
            SalesChannel.PICKUP,
            SalesChannel.DELIVERY,
        ]

    def test_an_unknown_order_type_warns_rather_than_guesses(self) -> None:
        result = parse_orders(export([bill("1", "2026-08-25 20:00:00", order_type="Drone")]))
        assert result.orders[0].channel is None
        assert any(w["kind"] == "unknown_order_type" for w in result.warnings)

    def test_covers_and_a_missing_cover_count(self) -> None:
        result = parse_orders(
            export(
                [
                    bill("1", "2026-08-25 20:00:00", persons=4),
                    bill("2", "2026-08-25 20:01:00", persons=None),
                ]
            )
        )
        assert [o.covers for o in result.orders] == [4, None]

    def test_the_phone_is_hashed_and_never_returned_raw(self) -> None:
        result = parse_orders(
            export([bill("1", "2026-08-25 20:00:00", phone=5550000001)]),
            hash_phone=sha,
        )
        assert result.orders[0].customer_phone_hash == sha("5550000001")
        assert "5550000001" not in str(result.orders[0])

    def test_without_a_hasher_the_number_is_dropped(self) -> None:
        """Not returned in the clear. A caller that forgot to pass one gets no
        phone rather than a raw one."""
        result = parse_orders(export([bill("1", "2026-08-25 20:00:00", phone=5550000001)]))
        assert result.orders[0].customer_phone_hash is None


class TestBusinessDate:
    def test_an_evening_bill_is_its_own_day(self) -> None:
        result = parse_orders(export([bill("1", "2026-08-22 20:37:10")]))
        assert result.orders[0].business_date == date(2026, 8, 22)

    def test_a_bill_after_midnight_belongs_to_the_night_before(self) -> None:
        """The spec's acceptance case: 00:45 on 23 Aug is Saturday's trade."""
        result = parse_orders(export([bill("1", "2026-08-23 00:45:00")]))
        assert result.orders[0].business_date == date(2026, 8, 22)

    def test_the_rollover_is_at_five_not_midnight(self) -> None:
        result = parse_orders(
            export(
                [
                    bill("1", "2026-08-23 04:59:59"),
                    bill("2", "2026-08-23 05:00:01"),
                ]
            )
        )
        assert result.orders[0].business_date == date(2026, 8, 22)
        assert result.orders[1].business_date == date(2026, 8, 23)


class TestRowsItRefuses:
    def test_a_cancelled_bill_is_skipped_and_reported(self) -> None:
        """Not folded into the totals, and not silently dropped either."""
        result = parse_orders(
            export(
                [
                    bill("1", "2026-08-25 20:00:00", net=500.0),
                    bill("2", "2026-08-25 20:05:00", net=900.0, status="Cancelled"),
                ]
            )
        )
        assert [o.external_bill_no for o in result.orders] == ["1"]
        assert result.net_paise == 50000
        assert any(w["kind"] == "skipped_status" and w["bill_no"] == "2" for w in result.warnings)

    def test_a_repeated_bill_number_is_taken_once(self) -> None:
        result = parse_orders(
            export(
                [
                    bill("7", "2026-08-25 20:00:00", net=100.0),
                    bill("7", "2026-08-25 21:00:00", net=100.0),
                ]
            )
        )
        assert len(result.orders) == 1
        assert any(w["kind"] == "duplicate_bill_no" for w in result.warnings)

    def test_an_unparseable_date_skips_that_bill_only(self) -> None:
        result = parse_orders(
            export(
                [
                    bill("1", "not a date"),
                    bill("2", "2026-08-25 20:00:00"),
                ]
            )
        )
        assert [o.external_bill_no for o in result.orders] == ["2"]
        assert any(w["kind"] == "bad_timestamp" for w in result.warnings)


class TestFilesItRefuses:
    def test_not_a_spreadsheet(self) -> None:
        with pytest.raises(UnreadableExport, match=r"readable \.xlsx"):
            parse_orders(b"this is not a workbook")

    def test_the_wrong_report(self) -> None:
        """An Item Sale Report has no Invoice No. column. Saying so beats
        writing zero orders and calling it a success."""
        wrong = export([], headers=["Hour", "Item", "Price", "Quantity"], with_summary=False)
        with pytest.raises(UnreadableExport, match="Orders Master Report"):
            parse_orders(wrong)

    def test_an_export_with_no_bills(self) -> None:
        with pytest.raises(UnreadableExport, match="no bills"):
            parse_orders(export([]))

    def test_a_missing_optional_column_warns_but_parses(self) -> None:
        headers = [h for h in HEADERS if h != "Phone"]
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        for row in (
            ["Date:", "2026-08-25 to 2026-08-25"],
            ["Name:", "Orders: Master Report"],
            ["Restaurant Name:", "Akira"],
            [],
            [],
            headers,
        ):
            ws.append(row)
        line: list[Any] = [None] * len(headers)
        line[headers.index("Invoice No.")] = "1"
        line[headers.index("Date")] = "2026-08-25 20:00:00"
        line[headers.index("Net Sales (₹)(M.A - D)")] = 500.0
        ws.append(line)
        buffer = BytesIO()
        wb.save(buffer)

        result = parse_orders(buffer.getvalue())
        assert len(result.orders) == 1
        assert any(
            w["kind"] == "missing_column" and w["column"] == "Phone" for w in result.warnings
        )


# ---------------------------------------------------------------------------
# The real export — the spec's acceptance criteria
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not REAL_EXPORT.exists(), reason=f"real export not at {REAL_EXPORT}")
class TestTheRealExport:
    """Skips when the file is not on this machine, like the database tests do
    without Postgres. When it IS present these are the numbers that matter."""

    @staticmethod
    def parsed() -> petpooja.ParseResult:
        return parse_orders(REAL_EXPORT.read_bytes(), hash_phone=sha)

    def test_it_reconciles_to_the_spec(self) -> None:
        """452 bills totalling Rs 4,86,076 — spec section 10."""
        result = self.parsed()
        assert len(result.orders) == 452
        assert result.net_paise == 48_607_635

    def test_it_parses_without_a_single_warning(self) -> None:
        assert self.parsed().warnings == []

    def test_the_period_matches_the_preamble(self) -> None:
        result = self.parsed()
        assert result.period_start == date(2026, 7, 17)
        assert result.period_end == date(2026, 8, 25)
        assert result.restaurant == "Akira"

    def test_every_after_midnight_bill_rolls_back(self) -> None:
        """21 of the 452 are struck before 05:00. Not one may keep its calendar
        date, or every weekend number in the system is wrong."""
        rolled = [o for o in self.parsed().orders if o.ordered_at.hour < 5]
        assert len(rolled) == 21
        assert all(o.business_date < o.ordered_at.date() for o in rolled)

    def test_it_covers_the_six_weeks_of_history(self) -> None:
        days = {o.business_date for o in self.parsed().orders}
        assert len(days) == 38
        assert min(days) == date(2026, 7, 17)
        assert max(days) == date(2026, 8, 25)

    def test_no_raw_phone_number_survives_parsing(self) -> None:
        """The export holds 141 real numbers. None may reach a dataclass, a log
        line, or this test file — which is why the assertion looks for *any*
        ten-digit Indian mobile rather than naming one."""
        import re

        result = self.parsed()
        hashed = [o for o in result.orders if o.customer_phone_hash]
        assert len(hashed) == 141
        assert all(len(o.customer_phone_hash or "") == 64 for o in hashed)
        blob = " ".join(str(o) for o in result.orders)
        assert re.search(r"[6-9]\d{9}", blob) is None
