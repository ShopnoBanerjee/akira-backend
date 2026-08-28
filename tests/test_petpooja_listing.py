"""The Order Listing adapter, against synthetic workbooks.

Shaped cell-for-cell like AKIRA's real export (which is never committed — it
carries customer names, phones and addresses in the clear). The traps pinned
here are the ones the real file demonstrated: split-payment continuation rows
repeating an Order No., and item names that are one typed comma away from
mangling a naive split.
"""

from datetime import date
from io import BytesIO
from typing import Any

import pytest

from app.domains.sales import service
from app.domains.sales.petpooja import UnreadableExport
from app.domains.sales.petpooja_listing import parse_listing, split_items

#: The real export's header row, verbatim.
HEADERS = [
    "Order No.",
    "Client OrderID",
    "Order Type",
    "Sub Order Type",
    "Customer Name",
    "Customer Phone",
    "GSTIN",
    "Customer Address",
    "Delivery Boy",
    "Delivery Boy Number",
    "Items",
    "My Amount (₹)",
    "Total Discount (₹)",
    "Delivery Charge (₹)",
    "Container Charge (₹)",
    "Total Tax (₹)",
    "Round Off (₹)",
    "Grand Total (₹)",
    "Payment Type",
    "Payment Description",
    "Status",
    "Created",
    "Sequence Name",
]
AT = {name: i for i, name in enumerate(HEADERS)}


def listing_row(
    order_no: str,
    items: str | None,
    *,
    created: str | None = "22 Aug 2026 20:15:00",
    amount: float | None = 500.0,
    status: str | None = "Printed",
    name: str | None = None,
    phone: str | None = None,
) -> list[Any]:
    row: list[Any] = [None] * len(HEADERS)
    row[AT["Order No."]] = order_no
    row[AT["Items"]] = items
    row[AT["Created"]] = created
    row[AT["My Amount (₹)"]] = amount
    row[AT["Status"]] = status
    row[AT["Customer Name"]] = name
    row[AT["Customer Phone"]] = phone
    return row


def split_row(order_no: str, amount: float, payment: str) -> list[Any]:
    """A split-payment continuation, exactly as the real file writes them:
    the Order No. again, a partial Grand Total, a Payment Type — and no
    Items, no Created, no Status."""
    row: list[Any] = [None] * len(HEADERS)
    row[AT["Order No."]] = order_no
    row[AT["Grand Total (₹)"]] = amount
    row[AT["Payment Type"]] = payment
    return row


def export(rows: list[list[Any]], *, headers: list[str] | None = None) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Name:", "Order Report"])
    ws.append(["Restaurant Name:", "Akira"])
    ws.append(["Restaurant Address:", "34 Naskar Para Kolkata 70007"])
    ws.append([])
    ws.append(headers if headers is not None else HEADERS)
    for row in rows:
        ws.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


class TestSplitItems:
    def test_a_plain_list_splits_on_comma_space(self) -> None:
        names, rejoined = split_items("Chicken Gyoza, Pork Yakitori, Akira Dora Cake")
        assert names == ["Chicken Gyoza", "Pork Yakitori", "Akira Dora Cake"]
        assert rejoined is False

    def test_parenthesised_variants_survive(self) -> None:
        names, _ = split_items("Akira Shoyu Ramen (pork), Akira Tonkatsu (Tofu)")
        assert names == ["Akira Shoyu Ramen (pork)", "Akira Tonkatsu (Tofu)"]

    def test_a_comma_inside_parentheses_is_rejoined_and_flagged(self) -> None:
        """No current menu name does this, but menu names are typed by humans
        into Petpooja and one day one will."""
        names, rejoined = split_items("Ramen (pork, extra nori), Chicken Gyoza")
        assert names == ["Ramen (pork, extra nori)", "Chicken Gyoza"]
        assert rejoined is True


class TestParsing:
    def test_it_reads_a_wellformed_export(self) -> None:
        result = parse_listing(
            export(
                [
                    listing_row("498", "Akira Shoyu Ramen (pork), Chicken Gyoza", amount=2055.0),
                    listing_row("497", "Veg Ramen", amount=498.0),
                ]
            )
        )
        assert result.restaurant == "Akira"
        assert result.adapter_version == "petpooja.listing.v1"
        assert [o.external_bill_no for o in result.orders] == ["498", "497"]
        assert result.orders[0].item_names == ["Akira Shoyu Ramen (pork)", "Chicken Gyoza"]
        assert result.orders[0].raw_items == "Akira Shoyu Ramen (pork), Chicken Gyoza"
        assert result.orders[0].amount_paise == 2055_00
        assert result.warnings == []

    def test_split_payment_rows_are_counted_not_duplicated(self) -> None:
        """Bill 476 in the real export appears four times: once in full, then
        three continuation rows carrying only a partial amount and its payment
        type. One order, zero duplicate warnings."""
        result = parse_listing(
            export(
                [
                    listing_row("476", "Veg Ramen", amount=1575.0),
                    split_row("476", 154.0, "UPI [IDFC First]"),
                    split_row("476", 1500.0, "Cash"),
                    split_row("476", 0.0, "Not Paid"),
                ]
            )
        )
        assert len(result.orders) == 1
        assert result.payment_split_rows == 3
        assert result.warnings == []

    def test_a_genuine_duplicate_full_row_is_taken_once_and_reported(self) -> None:
        result = parse_listing(
            export(
                [
                    listing_row("11", "Veg Ramen"),
                    listing_row("11", "Chicken Gyoza"),
                ]
            )
        )
        assert len(result.orders) == 1
        assert result.orders[0].item_names == ["Veg Ramen"]
        assert [w["kind"] for w in result.warnings] == ["duplicate_order"]

    def test_a_full_row_with_no_items_warns(self) -> None:
        result = parse_listing(
            export(
                [
                    listing_row("11", "Veg Ramen"),
                    listing_row("12", None),
                ]
            )
        )
        assert len(result.orders) == 1
        assert [w["kind"] for w in result.warnings] == ["empty_items"]

    def test_customer_details_never_leave_the_parse_loop(self) -> None:
        """The file carries names and phones in the clear. Nothing derived
        from them — not even a hash — appears anywhere in the result."""
        result = parse_listing(
            export([listing_row("11", "Veg Ramen", name="Asha Test", phone="5550000001")])
        )
        blob = repr(result)
        assert "Asha Test" not in blob
        assert "5550000001" not in blob

    def test_the_period_is_in_business_dates(self) -> None:
        """An order struck at 00:46 belongs to the previous trading day, so a
        file spanning one late night reports one business day, not two."""
        result = parse_listing(
            export(
                [
                    listing_row("2", "Veg Ramen", created="26 Aug 2026 00:46:15"),
                    listing_row("1", "Chicken Gyoza", created="25 Aug 2026 19:00:00"),
                ]
            )
        )
        assert result.period == (date(2026, 8, 25), date(2026, 8, 25))

    def test_an_unreadable_created_warns_but_keeps_the_items(self) -> None:
        result = parse_listing(export([listing_row("9", "Veg Ramen", created="whenever")]))
        assert len(result.orders) == 1
        assert result.orders[0].created_at is None
        assert [w["kind"] for w in result.warnings] == ["bad_timestamp"]

    def test_a_master_report_is_refused(self) -> None:
        """The Orders Master must not parse as a listing: its header has no
        Items column, and refusing beats returning zero orders."""
        with pytest.raises(UnreadableExport, match="Order Listing"):
            parse_listing(
                export(
                    [listing_row("1", "x")],
                    headers=["Invoice No.", "Date", "Payment Type", "Status"],
                )
            )

    def test_an_export_with_no_orders_is_an_error_not_an_empty_result(self) -> None:
        with pytest.raises(UnreadableExport, match="no orders"):
            parse_listing(export([]))


class TestDetection:
    def test_a_listing_is_recognised(self) -> None:
        data = export([listing_row("1", "Veg Ramen")])
        assert service.detect_source(data) == service.SOURCE_LISTING

    def test_a_master_is_recognised(self) -> None:
        from tests.test_petpooja import bill
        from tests.test_petpooja import export as master_export

        data = master_export([bill("1", when="2026-08-22 20:00:00", net=100.0)])
        assert service.detect_source(data) == service.SOURCE_ORDERS

    def test_anything_else_is_refused_at_upload(self) -> None:
        from app.core.errors import ValidationError

        with pytest.raises(ValidationError, match="not a report"):
            service.detect_source(export([], headers=["Sl", "Thing", "Amount"]))

    def test_unreadable_bytes_are_refused(self) -> None:
        from app.core.errors import ValidationError

        with pytest.raises(ValidationError, match="readable"):
            service.detect_source(b"this is not a workbook")
