-- ---------------------------------------------------------------------------
-- 0019 — Recipes and item-day sales (Stage 2, P17)
--
-- The map from what the till sells to what the kitchen uses, and the
-- quantity source that makes it worth having.
--
-- **sales_item_days** holds the Petpooja "Item Report: Day Wise" export —
-- true units per menu item per day, which no bill-level export carries
-- (D21: the Order Listing has names only). `report_date` is stored VERBATIM
-- from the file: this report gives no timestamps, so the 05:00 business-date
-- rule cannot be applied to it. Petpooja groups by its own configured day
-- close; at a consumption window's midnight edges the two groupings can
-- disagree by one late-night day. Known, documented (D24), and bounded.
--
-- **recipes** are brand-level, like the inventory catalogue (D10): one menu
-- across outlets. `menu_item_name` matches the names Petpooja prints —
-- "Akira Shoyu Ramen (pork)" — because those are the join key to both
-- sales_item_days and sales_order_items. Lines carry qty per unit sold, in
-- the inventory item's own canonical unit.
--
-- **stock_consumption.theoretical_qty**: what the recipes say a window
-- SHOULD have used, written by the nightly pass beside what the counts say
-- it did. Null when no item-day sales cover the window — no sales data is
-- not the same as zero usage, the same honesty rule as apparent_consumption.
-- ---------------------------------------------------------------------------

create table sales_item_days (
    id            uuid primary key default gen_random_uuid(),
    outlet_id     uuid not null references outlets (id) on delete cascade,
    -- Petpooja's own day grouping, verbatim. NOT derived from created_at,
    -- because this export has no timestamps to derive from.
    report_date   date not null,
    item_name     text not null,
    qty           numeric not null,
    net_paise     bigint not null,
    upload_id     uuid references data_uploads (id) on delete set null,
    created_at    timestamptz not null default now(),

    unique (outlet_id, report_date, item_name)
);

create index sales_item_days_outlet_date_idx
    on sales_item_days (outlet_id, report_date desc);

create table recipes (
    id              uuid primary key default gen_random_uuid(),
    -- The name as Petpooja prints it on bills and reports — the join key.
    menu_item_name  text not null unique,
    is_active       boolean not null default true,
    notes           text,
    created_by      uuid references profiles (id) on delete set null,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz
);

create table recipe_lines (
    id            uuid primary key default gen_random_uuid(),
    recipe_id     uuid not null references recipes (id) on delete cascade,
    item_id       uuid not null references inventory_items (id) on delete cascade,
    -- Per unit sold, in the inventory item's own canonical unit.
    qty_per_unit  numeric not null,
    created_at    timestamptz not null default now(),

    unique (recipe_id, item_id),
    constraint recipe_lines_qty_positive check (qty_per_unit > 0)
);

create index recipe_lines_item_idx on recipe_lines (item_id);

alter table stock_consumption add column theoretical_qty numeric;

create trigger recipes_set_updated_at
    before update on recipes
    for each row execute function set_updated_at();

alter table sales_item_days enable row level security;
alter table sales_item_days force row level security;
revoke all on table sales_item_days from anon;
grant select on table sales_item_days to authenticated;
create policy sales_item_days_read_own_outlet on sales_item_days
    for select to authenticated
    using (auth_is_global_admin() or outlet_id = any (auth_outlet_ids()));

alter table recipes enable row level security;
alter table recipes force row level security;
revoke all on table recipes from anon;
grant select on table recipes to authenticated;
create policy recipes_read on recipes for select to authenticated using (true);

alter table recipe_lines enable row level security;
alter table recipe_lines force row level security;
revoke all on table recipe_lines from anon;
grant select on table recipe_lines to authenticated;
create policy recipe_lines_read on recipe_lines for select to authenticated using (true);
