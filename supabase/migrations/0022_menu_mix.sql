-- ---------------------------------------------------------------------------
-- 0022 — Menu mix: the menu's own taxonomy, and category attach per period
--
-- Two exports the owner supplied on 5 Sep 2026 carry what the bill-level
-- data cannot (D29):
--
-- **menu_items** — from the "Item Wise: Sales Report": every menu item under
-- its Petpooja category, with Petpooja's item code. Brand-level like recipes
-- (D10, D24): one menu across outlets, keyed by the printed name because that
-- name is the join to sales_order_items.item_name (names per bill, D21) and
-- sales_item_days.item_name (units per day, D24). With this map, "share of
-- bills that carried a drink" is a join, not a guess.
--
-- **sales_category_periods** — from the "Sales Report: Category Wise": for a
-- date range, how many BILLS carried each category and what they were worth.
-- Petpooja's own count, over its own calendar dates. Stored per period
-- exactly as reported; the denominator (bills in the period) comes from
-- sales_orders at read time, because the report's Total row sums the
-- per-category counts and is not a bill count.
--
-- `is_charge` marks Container Charge / Round Off / Waived Off — rows with
-- money and no menu — so no one computes an attach rate for rounding.
-- ---------------------------------------------------------------------------

create table menu_items (
    id             uuid primary key default gen_random_uuid(),
    -- Petpooja's printed name, verbatim. The join key everywhere.
    name           text not null unique,
    category       text not null,
    petpooja_code  text,
    upload_id      uuid references data_uploads (id) on delete set null,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now()
);

comment on table menu_items is
    'The menu as Petpooja prints it: item name under its category. Brand-level; the name is the join key to bills and item-days.';

create index menu_items_category_idx on menu_items (category);


create table sales_category_periods (
    id                uuid primary key default gen_random_uuid(),
    outlet_id         uuid not null references outlets (id) on delete cascade,
    upload_id         uuid references data_uploads (id) on delete set null,
    period_start      date not null,
    period_end        date not null,
    category          text not null,
    -- Bills that carried at least one item of the category, per Petpooja.
    orders            integer not null,
    -- Units of the category across those bills.
    items             integer not null,
    net_amount_paise  bigint not null default 0,
    discount_paise    bigint not null default 0,
    tax_paise         bigint not null default 0,
    gross_paise       bigint not null default 0,
    net_sales_paise   bigint not null default 0,
    share_pct         numeric,
    is_charge         boolean not null default false,
    created_at        timestamptz not null default now(),

    constraint sales_category_periods_ordered check (period_start <= period_end),
    unique (outlet_id, period_start, period_end, category)
);

comment on table sales_category_periods is
    'Petpooja Category Wise report rows, per period, verbatim. orders = bills carrying the category; divide by bills in the period from sales_orders for the attach rate.';

create index sales_category_periods_outlet_period_idx
    on sales_category_periods (outlet_id, period_end desc, period_start);


-- RLS, forced, read-only for authenticated, scoped like every other sales
-- table. 0021 sets the default grants; stated here as well so this file
-- stands alone.
alter table menu_items enable row level security;
alter table menu_items force row level security;
revoke all on table menu_items from anon;
grant select on table menu_items to authenticated;
create policy menu_items_read on menu_items for select to authenticated using (true);

alter table sales_category_periods enable row level security;
alter table sales_category_periods force row level security;
revoke all on table sales_category_periods from anon;
grant select on table sales_category_periods to authenticated;
create policy sales_category_periods_read_own_outlet on sales_category_periods
    for select to authenticated
    using (auth_is_global_admin() or outlet_id = any (auth_outlet_ids()));
