"""The Item Report: Day Wise adapter, against synthetic workbooks.

Shaped like the real export: Item, Date, Qty., Total (₹), with a Total row
straight after the header — the same summary trap as the master's, pinned
here so it can never be re-learned the expensive way.
"""

from datetime import date
from io import BytesIO
from typing import Any

import pytest

from app.domains.sales import service
from app.domains.sales.petpooja import UnreadableExport
from app.domains.sales.petpooja_itemdays import parse_itemdays

HEADERS = ["Item", "Date", "Qty.", "Total (₹)"]


def export(
    rows: list[list[Any]],
    *,
    with_total: bool = True,
    headers: list[str] | None = None,
) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Date:", "2026-08-01 to 2026-08-28"])
    ws.append(["Name:", "Item Report: Day Wise"])
    ws.append(["Restaurant Name:", "Akira"])
    ws.append([])
    ws.append([])
    ws.append(headers if headers is not None else HEADERS)
    if with_total:
        total_qty = sum(float(r[2]) for r in rows if len(r) > 2 and r[2] is not None)
        ws.append(["Total", None, str(total_qty), "999999"])
    for row in rows:
        ws.append(row)
    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


class TestParsing:
    def test_it_reads_items_days_and_quantities(self) -> None:
        result = parse_itemdays(
            export(
                [
                    ["Akira Shoyu Ramen (pork)", "2026-08-21", "5.0", "2100.0"],
                    ["Akira Shoyu Ramen (pork)", "2026-08-22", "8.0", "3360.0"],
                    ["Chicken Gyoza", "2026-08-21", "3.0", "750.0"],
                ]
            )
        )
        assert result.restaurant == "Akira"
        assert result.adapter_version == "petpooja.itemdays.v1"
        assert len(result.rows) == 3
        first = result.rows[0]
        assert first.item_name == "Akira Shoyu Ramen (pork)"
        assert first.report_date == date(2026, 8, 21)
        assert first.qty == 5.0
        assert first.net_paise == 2100_00
        assert result.period_start == date(2026, 8, 1)

    def test_the_files_own_total_row_is_kept_apart_not_ingested(self) -> None:
        """Ingesting the Total row as an item called "Total" selling 16
        units would poison every recipe join that follows."""
        result = parse_itemdays(
            export(
                [
                    ["Ramen", "2026-08-21", "10.0", "100.0"],
                    ["Gyoza", "2026-08-21", "6.0", "60.0"],
                ]
            )
        )
        assert len(result.rows) == 2
        assert all(r.item_name != "Total" for r in result.rows)
        assert result.reported_qty == 16.0
        assert result.total_qty == 16.0

    def test_fractional_quantities_survive(self) -> None:
        result = parse_itemdays(export([["Half Ramen", "2026-08-21", "2.5", "500.0"]]))
        assert result.rows[0].qty == 2.5

    def test_a_duplicate_item_day_is_taken_once_and_reported(self) -> None:
        result = parse_itemdays(
            export(
                [
                    ["Ramen", "2026-08-21", "5.0", "100.0"],
                    ["Ramen", "2026-08-21", "7.0", "140.0"],
                ]
            )
        )
        assert len(result.rows) == 1
        assert result.rows[0].qty == 5.0
        assert [w["kind"] for w in result.warnings] == ["duplicate_item_day"]

    def test_an_unreadable_row_warns_and_moves_on(self) -> None:
        result = parse_itemdays(
            export(
                [
                    ["Ramen", "someday", "5.0", "100.0"],
                    ["Gyoza", "2026-08-21", "3.0", "60.0"],
                ]
            )
        )
        assert len(result.rows) == 1
        assert [w["kind"] for w in result.warnings] == ["bad_row"]

    def test_a_master_report_is_refused(self) -> None:
        with pytest.raises(UnreadableExport, match="Day Wise"):
            parse_itemdays(
                export(
                    [["1", "2026-08-21", "5.0", "100.0"]],
                    headers=["Invoice No.", "Date", "Payment Type", "Status"],
                )
            )

    def test_no_rows_is_an_error_not_an_empty_result(self) -> None:
        with pytest.raises(UnreadableExport, match="no item-day rows"):
            parse_itemdays(export([], with_total=False))


class TestDetection:
    def test_an_itemdays_export_is_recognised(self) -> None:
        data = export([["Ramen", "2026-08-21", "5.0", "100.0"]])
        assert service.detect_source(data) == service.SOURCE_ITEMDAYS

    def test_the_three_reports_are_told_apart(self) -> None:
        """One upload button, three shapes — each must land on its own
        adapter, whatever order the header checks run in."""
        from tests.test_petpooja import bill
        from tests.test_petpooja import export as master_export
        from tests.test_petpooja_listing import export as listing_export
        from tests.test_petpooja_listing import listing_row

        assert (
            service.detect_source(master_export([bill("1", when="2026-08-22 20:00:00")]))
            == service.SOURCE_ORDERS
        )
        assert (
            service.detect_source(listing_export([listing_row("1", "Veg Ramen")]))
            == service.SOURCE_LISTING
        )
        assert (
            service.detect_source(export([["Ramen", "2026-08-21", "5.0", "100.0"]]))
            == service.SOURCE_ITEMDAYS
        )
