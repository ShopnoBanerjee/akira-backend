"""The two menu-mix adapters (D29), against synthetic workbooks shaped exactly
like AKIRA's 5 Sep 2026 exports — including the traps: a Total row whose
order count is a SUM of per-category counts, charge rows with no menu behind
them, and item rows whose category is stated once per group.
"""

from datetime import date
from io import BytesIO
from typing import Any

import pytest

from app.domains.sales.petpooja import UnreadableExport
from app.domains.sales.petpooja_categories import parse_categories
from app.domains.sales.petpooja_itemwise import parse_itemwise

CAT_HEADERS = [
    "Category",
    "No. of Orders",
    "Total Items Ordered",
    "Net Amount (₹)",
    "Total Discount (₹)",
    "Total Tax (₹)",
    "Total Sales (₹)",
    "Net Sales (₹)(N.A - T.D)",
    "Percentage (%)",
]
ITEM_HEADERS = ["Category", "Item", "Code", "Sap Code", "Qty.", "Total (₹)"]


def _book(
    preamble_name: str, headers: list[str], body: list[list[Any]], *, restaurant: str = "Akira"
) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(["Date:", "2026-07-17 to 2026-09-05"])
    ws.append(["Name:", preamble_name])
    ws.append(["Restaurant Name:", restaurant])
    ws.append([])
    ws.append([])
    ws.append(headers)
    for row in body:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def categories_export(**kw: Any) -> bytes:
    body = [
        ["Total", 1915, 2709, 646024, 6525.35, 14360.08, 654372.16, 639498.65],
        ["Min.", 0, 0, 0, 0, 0, 50, 0],
        ["Max.", 418, 620, 222830, 2329.8, 4914.44, 225414.64, 220500.2],
        ["Avg.", 174.09, 246.27, 58729.45, 593.21, 1305.46, 59488.38, 58136.24],
        ["Ramen", 413.0, 620.0, 222830.0, 2329.8, 4914.44, 225414.64, 220500.2, 34.47],
        ["Gyoza", 418.0, 582.0, 125389.0, 1343.5, 2883.7, 126929.2, 124045.5, 19.41],
        ["Refreshments", 310.0, 529.0, 87381.0, 1159.55, 1835.92, 88057.37, 86221.45, 13.47],
        ["Dessert", 246.0, 304.0, 41146.0, 357.4, 1147.38, 41935.98, 40788.6, 6.41],
        ["dips", 2.0, 5.0, 300.0, 0.0, 15.0, 315.0, 300.0, 0.05],
        ["Container Charge", 0, 0, 0, 0, 0, 50.0, 0, 0],
        ["Round Off", 0, 0, 0, 0, 0, 118.43, 0, 0],
    ]
    return _book("Sales Report: Category Wise", CAT_HEADERS, body, **kw)


def itemwise_export(**kw: Any) -> bytes:
    body = [
        ["Total", None, None, None, 2709, 646024],
        ["Min.", None, None, None, 2, 120],
        ["Ramen", "Akira Shoyu Ramen (pork)", 1, None, 49.0, 17361.0],
        ["", "Akira Tonkatsu (pork)", 3, None, 148.0, 55947.0],
        ["Sub Total", None, None, None, 197, 73308],
        ["Gyoza", "Chicken Gyoza", 10, None, 229.0, 47889.0],
        ["", "Cream Cheese Mushroom", "cream cheese mushroo", None, 215.0, 45950.0],
        ["Sub Total", None, None, None, 444, 93839],
        ["Refreshments", "Sakura", 17, None, 236.0, 39884.0],
        ["Sub Total", None, None, None, 236, 39884],
    ]
    return _book("Item Wise: Sales Report", ITEM_HEADERS, body, **kw)


class TestCategoryWise:
    def test_it_reads_categories_and_keeps_both_nets(self) -> None:
        r = parse_categories(categories_export())
        assert r.restaurant == "Akira"
        assert (r.period_start, r.period_end) == (date(2026, 7, 17), date(2026, 9, 5))
        ramen = next(c for c in r.rows if c.category == "Ramen")
        assert ramen.orders == 413 and ramen.items == 620
        assert ramen.net_amount_paise == 222_830_00
        assert ramen.net_sales_paise == 220_500_20
        assert ramen.gross_paise == 225_414_64
        assert ramen.share_pct == 34.47
        assert ramen.is_charge is False

    def test_the_total_row_is_kept_apart_and_its_orders_are_not_a_bill_count(self) -> None:
        """1,915 is 413+418+310+246+2+... — the sum of per-category bill
        counts. Treating it as bills would put every attach rate under 25%."""
        r = parse_categories(categories_export())
        assert all(c.category != "Total" for c in r.rows)
        assert r.reported_items == 2709
        assert r.reported_net_sales_paise == 639_498_65
        assert not hasattr(r, "reported_orders")

    def test_charge_rows_are_flagged_not_dropped(self) -> None:
        r = parse_categories(categories_export())
        charges = [c.category for c in r.rows if c.is_charge]
        assert charges == ["Container Charge", "Round Off"]
        assert next(c for c in r.rows if c.category == "Round Off").gross_paise == 118_43

    def test_items_total_reconciles_or_warns(self) -> None:
        r = parse_categories(categories_export())
        # The synthetic file omits Yakitori/Donburi/Karaage on purpose, so
        # the parsed items fall short of the file's own Total and it says so.
        kinds = {w["kind"] for w in r.warnings}
        assert "items_total_mismatch" in kinds

    def test_a_wrong_report_is_refused(self) -> None:
        with pytest.raises(UnreadableExport, match="Category Wise"):
            parse_categories(_book("Something", ["Item", "Date", "Qty."], [["x", "2026-08-01", 1]]))


class TestItemWise:
    def test_category_is_forward_filled_across_the_group(self) -> None:
        r = parse_itemwise(itemwise_export())
        assert r.categories == {
            "Ramen": ["Akira Shoyu Ramen (pork)", "Akira Tonkatsu (pork)"],
            "Gyoza": ["Chicken Gyoza", "Cream Cheese Mushroom"],
            "Refreshments": ["Sakura"],
        }

    def test_sub_totals_and_summary_rows_are_not_items(self) -> None:
        r = parse_itemwise(itemwise_export())
        assert all(i.item_name not in ("Sub Total", "Total", "Min.") for i in r.items)
        assert r.reported_qty == 2709.0

    def test_codes_are_text_whatever_petpooja_sent(self) -> None:
        r = parse_itemwise(itemwise_export())
        by = {i.item_name: i for i in r.items}
        assert by["Chicken Gyoza"].petpooja_code == "10"
        assert by["Cream Cheese Mushroom"].petpooja_code == "cream cheese mushroo"
        assert by["Sakura"].qty == 236.0 and by["Sakura"].net_paise == 39_884_00

    def test_it_is_not_mistaken_for_the_day_wise_report(self) -> None:
        with pytest.raises(UnreadableExport, match="Item Wise"):
            parse_itemwise(
                _book(
                    "Item Report: Day Wise",
                    ["Item", "Date", "Qty.", "Total (₹)"],
                    [["Ramen", "2026-08-01", 1, 100]],
                )
            )


class TestSourceDetection:
    def test_all_five_reports_are_told_apart(self) -> None:
        from app.domains.sales import service
        from tests.test_petpooja_itemdays import export as itemdays_export
        from tests.test_petpooja_listing import export as listing_export
        from tests.test_petpooja_listing import listing_row

        assert service.detect_source(categories_export()) == service.SOURCE_CATEGORIES
        assert service.detect_source(itemwise_export()) == service.SOURCE_ITEMWISE
        assert (
            service.detect_source(itemdays_export([["Ramen", "2026-08-21", "5.0", "100.0"]]))
            == service.SOURCE_ITEMDAYS
        )
        assert (
            service.detect_source(listing_export([listing_row("1", "Veg Ramen")]))
            == service.SOURCE_LISTING
        )

    def test_the_restaurant_guard_sees_the_new_reports_too(self) -> None:
        from app.domains.sales import service

        assert (
            service.inspect_export(categories_export(restaurant="Akira Express")).restaurant
            == "Akira Express"
        )
        assert (
            service.inspect_export(itemwise_export(restaurant="Akira Express")).restaurant
            == "Akira Express"
        )
