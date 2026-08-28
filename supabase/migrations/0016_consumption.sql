-- ---------------------------------------------------------------------------
-- 0016 — Consumption windows (Stage 2, P13)
--
-- What an outlet actually used, derived from the one ground truth this system
-- trusts: confirmed physical counts. A window spans two CONSECUTIVE confirmed
-- counts of the same item at the same outlet:
--
--     apparent_consumption = from_qty + requisitioned_between − to_qty
--
-- "Apparent", and the word is doing honest work: there is no goods-received
-- flow yet, so finalised requisition quantities dated inside the window stand
-- in for deliveries. The assumption is recorded on every row's detail — when
-- a goods-received flow lands in a later epic, the column tightens and the
-- name stops hedging. A window with no requisition data still records the
-- raw count delta; it never invents a receipts figure.
--
-- Rows are written by the nightly stock_anomalies job, idempotently: one row
-- per (item, ending count), recomputed freely because everything here is
-- derived. Deleting the table loses nothing but time.
-- ---------------------------------------------------------------------------

create table stock_consumption (
    id              uuid primary key default gen_random_uuid(),
    outlet_id       uuid not null references outlets (id) on delete cascade,
    item_id         uuid not null references inventory_items (id) on delete cascade,

    from_count_id   uuid not null references stock_counts (id) on delete cascade,
    to_count_id     uuid not null references stock_counts (id) on delete cascade,
    from_date       date not null,
    to_date         date not null,
    days_between    integer not null,

    from_qty        numeric not null,
    to_qty          numeric not null,
    -- Sum of finalised requisition final_qty for this item dated inside the
    -- window. Null when no finalised requisition exists in the window, which
    -- is different from a requisition of zero.
    requisitioned_qty numeric,
    -- from + requisitioned − to. Null when requisitioned_qty is null: without
    -- a receipts stand-in the figure would be a guess wearing a column name.
    apparent_consumption numeric,
    -- Covers served at the outlet across the window's trading days, from
    -- sales_orders. What the per-cover anomaly divides by.
    covers          integer,

    -- The working: formula, inputs, and the stated receipts assumption.
    detail          jsonb not null default '{}'::jsonb,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz,

    unique (to_count_id, item_id),
    constraint consumption_window_ordered check (from_date <= to_date)
);

create index stock_consumption_outlet_item_idx
    on stock_consumption (outlet_id, item_id, to_date desc);

create trigger stock_consumption_set_updated_at
    before update on stock_consumption
    for each row execute function set_updated_at();

alter table stock_consumption enable row level security;
alter table stock_consumption force row level security;
revoke all on table stock_consumption from anon;
grant select on table stock_consumption to authenticated;

create policy stock_consumption_read_own_outlet on stock_consumption
    for select to authenticated
    using (auth_is_global_admin() or outlet_id = any (auth_outlet_ids()));
