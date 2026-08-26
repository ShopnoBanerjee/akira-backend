-- ---------------------------------------------------------------------------
-- 0005 — Sales ingestion
--
-- Stage 1 ingests and stores only. The sales dashboard is Stage 2; the point of
-- getting these tables right now is that a Petpooja API sync can replace the
-- XLSX upload later without touching anything downstream.
-- ---------------------------------------------------------------------------

create table data_uploads (
    id                 uuid primary key default gen_random_uuid(),
    outlet_id          uuid not null references outlets (id) on delete cascade,
    uploaded_by        uuid references profiles (id) on delete set null,
    source             text not null,   -- 'petpooja_orders' | 'petpooja_items' | 'manual'
    original_filename  text not null,
    storage_path       text not null,

    -- Idempotency. Re-uploading the same export must not double the numbers.
    file_sha256        text not null unique,

    period_start   date,
    period_end     date,
    status         upload_status not null default 'received',
    row_count      integer,

    -- Parsers never silently drop an unknown column. Everything unrecognised
    -- lands here and is shown to whoever uploaded the file.
    warnings       jsonb not null default '[]'::jsonb,
    error_detail   text,

    created_at     timestamptz not null default now(),
    updated_at     timestamptz,

    constraint data_uploads_period_ordered
        check (period_start is null or period_end is null or period_start <= period_end)
);


create table sales_orders (
    id               uuid primary key default gen_random_uuid(),
    outlet_id        uuid not null references outlets (id) on delete cascade,
    upload_id        uuid references data_uploads (id) on delete cascade,
    external_bill_no text not null,
    business_date    date not null,
    ordered_at       timestamptz not null,
    channel          sales_channel,
    covers           integer,

    -- Integer paise everywhere. Never float, never numeric for currency.
    gross_paise      bigint not null default 0,
    discount_paise   bigint not null default 0,
    tax_paise        bigint not null default 0,
    net_paise        bigint not null default 0,

    payment_mode     text,
    table_no         text,

    -- Salted SHA-256. The raw number is never persisted. Phone capture is a
    -- stated growth target, so this table will become the CRM source of truth;
    -- the privacy shape has to be right before it grows.
    customer_phone_hash text,

    created_at       timestamptz not null default now(),
    updated_at       timestamptz,

    unique (outlet_id, external_bill_no),
    constraint sales_orders_covers_non_negative check (covers is null or covers >= 0)
);

comment on column sales_orders.business_date is
    'Derived with business_date(ordered_at). A bill at 00:45 belongs to the previous trading day.';


create table sales_order_items (
    id                uuid primary key default gen_random_uuid(),
    order_id          uuid not null references sales_orders (id) on delete cascade,
    outlet_id         uuid not null references outlets (id) on delete cascade,
    business_date     date not null,
    item_name         text not null,
    item_category     text,
    qty               numeric not null default 0,
    unit_price_paise  bigint,
    line_net_paise    bigint not null default 0,
    created_at        timestamptz not null default now()
);


create trigger data_uploads_set_updated_at
    before update on data_uploads
    for each row execute function set_updated_at();

create trigger sales_orders_set_updated_at
    before update on sales_orders
    for each row execute function set_updated_at();
