-- ---------------------------------------------------------------------------
-- 0017 — Order items from the Order Listing export (Stage 2, P14)
--
-- sales_order_items was declared in 0005 for a line-item export that Petpooja
-- turned out not to produce. What it does produce is the Order Listing report:
-- item NAMES per bill, comma-joined, with no per-line quantity or price. The
-- petpooja.listing.v1 adapter writes those names here, joined to the master
-- bill on external_bill_no.
--
-- The 0005 shape lied by default: `qty not null default 0` would record "we
-- know it was zero" when the truth is "the export does not say". Quantity and
-- line value become nullable with no default — null means unknown, exactly as
-- apparent_consumption does in 0016. If Petpooja ever yields a true line-item
-- export, a later adapter fills them and nothing here changes.
--
-- Items for an order are wholly owned by the latest listing upload that
-- mentioned that order: the write path deletes and re-inserts the order's
-- set. sl_no is the item's position on the bill; the unique constraint is a
-- guard against a double-write bug, not part of the data model.
-- ---------------------------------------------------------------------------

alter table sales_order_items
    alter column qty drop default,
    alter column qty drop not null,
    alter column line_net_paise drop default,
    alter column line_net_paise drop not null;

alter table sales_order_items
    add column sl_no integer,
    add column upload_id uuid references data_uploads (id) on delete set null;

alter table sales_order_items
    add constraint sales_order_items_order_sl_key unique (order_id, sl_no);
