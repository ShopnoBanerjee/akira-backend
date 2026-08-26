-- ---------------------------------------------------------------------------
-- 0009 — Inventory catalogue
--
-- Pulled forward from Stage 2 so an admin can add and edit stock items now.
-- Stage 1 ships the catalogue and per-outlet levels only: there is no counting
-- flow and no requisition engine yet, but both will build on these tables
-- rather than replacing them.
--
-- Shape: ONE shared catalogue across outlets, with levels set per outlet. Add
-- an item once and every outlet can stock it; a larger outlet holds more of it.
-- Two outlets entering the same ingredient under two ids would make any
-- cross-outlet consumption or cost comparison meaningless.
-- ---------------------------------------------------------------------------

-- Units as they appear on AKIRA's actual count sheets.
create type inventory_unit as enum (
    'piece',
    'gram',
    'kilogram',
    'millilitre',
    'litre',
    'roll',
    'packet',
    'box',
    'bottle',
    'jug'
);


-- The 'Department' column on the paper sheets: which station counts this.
create table inventory_departments (
    id          uuid primary key default gen_random_uuid(),
    key         text not null unique,   -- 'fnb_hot_range'
    label       text not null,
    label_bn    text,
    sort_order  integer not null default 0,
    is_active   boolean not null default true,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz,
    deleted_at  timestamptz
);


-- The 'Category' column: what kind of thing it is.
create table inventory_categories (
    id          uuid primary key default gen_random_uuid(),
    key         text not null unique,   -- 'vegetables'
    label       text not null,
    label_bn    text,
    sort_order  integer not null default 0,
    is_active   boolean not null default true,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz,
    deleted_at  timestamptz
);


create table inventory_items (
    id             uuid primary key default gen_random_uuid(),
    code           text unique,          -- optional internal or vendor code
    name           text not null,
    name_bn        text,
    department_id  uuid not null references inventory_departments (id),
    category_id    uuid references inventory_categories (id),
    unit           inventory_unit not null,
    notes          text,
    is_active      boolean not null default true,
    created_by     uuid references profiles (id) on delete set null,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz,
    deleted_at     timestamptz
);

-- The same name may legitimately recur across departments (the bar and the hot
-- range both stock club soda), but not twice within one.
create unique index inventory_items_name_per_department_uq
    on inventory_items (department_id, lower(name))
    where deleted_at is null;


-- Per-outlet stocking decisions. Absence of a row means the outlet has not
-- configured the item; is_stocked = false means it deliberately does not carry
-- it, which is a different thing and worth being able to say.
create table inventory_outlet_levels (
    id           uuid primary key default gen_random_uuid(),
    outlet_id    uuid not null references outlets (id) on delete cascade,
    item_id      uuid not null references inventory_items (id) on delete cascade,

    -- Minimum to hold. Taken from the "Minimum" column on the mise-en-place
    -- sheet where one exists.
    par_level    numeric,
    -- Quantity to order when below par. Feeds the Stage 2 requisition engine.
    reorder_qty  numeric,
    -- Round order quantities up to this. A supplier selling only full cases
    -- makes 2.3 cases a meaningless number.
    order_unit   numeric,

    is_stocked   boolean not null default true,
    updated_by   uuid references profiles (id) on delete set null,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz,

    unique (outlet_id, item_id),
    constraint inventory_levels_par_non_negative
        check (par_level is null or par_level >= 0),
    constraint inventory_levels_reorder_non_negative
        check (reorder_qty is null or reorder_qty >= 0),
    constraint inventory_levels_order_unit_positive
        check (order_unit is null or order_unit > 0)
);


create index inventory_items_department_idx
    on inventory_items (department_id, name)
    where deleted_at is null;

create index inventory_items_category_idx
    on inventory_items (category_id)
    where deleted_at is null;

create index inventory_outlet_levels_outlet_idx
    on inventory_outlet_levels (outlet_id, is_stocked);


create trigger inventory_departments_set_updated_at
    before update on inventory_departments
    for each row execute function set_updated_at();

create trigger inventory_categories_set_updated_at
    before update on inventory_categories
    for each row execute function set_updated_at();

create trigger inventory_items_set_updated_at
    before update on inventory_items
    for each row execute function set_updated_at();

create trigger inventory_outlet_levels_set_updated_at
    before update on inventory_outlet_levels
    for each row execute function set_updated_at();


-- --- RLS -------------------------------------------------------------------
-- Same rule as everywhere else: authenticated reads, no browser write path.
-- The catalogue is network-wide reference data; levels are outlet-scoped.

do $$
declare
    t text;
begin
    foreach t in array array[
        'inventory_departments', 'inventory_categories',
        'inventory_items', 'inventory_outlet_levels'
    ]
    loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        execute format('revoke all on table %I from anon', t);
        execute format('grant select on table %I to authenticated', t);
    end loop;
end
$$;

create policy inventory_departments_read_all on inventory_departments
    for select to authenticated
    using (auth_profile_role() is not null);

create policy inventory_categories_read_all on inventory_categories
    for select to authenticated
    using (auth_profile_role() is not null);

create policy inventory_items_read_all on inventory_items
    for select to authenticated
    using (auth_profile_role() is not null);

create policy inventory_outlet_levels_read_own_outlet on inventory_outlet_levels
    for select to authenticated
    using (
        auth_is_global_admin()
        or outlet_id = any (auth_outlet_ids())
    );
