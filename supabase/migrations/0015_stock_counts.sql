-- ---------------------------------------------------------------------------
-- 0015 — Stock counts and requisitions (Stage 2 opening)
--
-- The physical sheet this digitises is AKIRA's "Daily Chef and Range" count:
-- printed item rows (English + Bengali, matching inventory_items), and two
-- handwritten columns — Physical Closing Count and Requisition Qty Need.
-- A photographed sheet arrives as a PDF or images; an LLM extracts rows with
-- a confidence each; deterministic code normalises quantities and maps rows
-- to the catalogue; a person confirms what the machine was unsure about.
--
-- The spec's rule, mechanically enforced by this schema: THE LLM PARSES AND
-- EXPLAINS, DETERMINISTIC CODE DECIDES. Extraction output lands in raw_*
-- columns verbatim and is never overwritten — qty and item_id beside them are
-- what code (or a human) derived, so every number a manager acts on can be
-- traced back to what was actually on the paper.
--
-- Files themselves ride the existing data_uploads ledger (source
-- 'stock_sheet'): same content-hash idempotency, same reparse lever, same
-- retention argument as the sales exports.
-- ---------------------------------------------------------------------------

create type stock_count_status as enum (
    'extracting',   -- the background job is reading the file
    'review',       -- extracted; lines await mapping/confirmation
    'confirmed',    -- a person signed it off; this is the outlet's count now
    'failed'        -- extraction failed; the job_runs row says why
);

create type requisition_status as enum ('draft', 'final');


create table stock_counts (
    id             uuid primary key default gen_random_uuid(),
    outlet_id      uuid not null references outlets (id) on delete cascade,
    upload_id      uuid not null references data_uploads (id) on delete restrict,

    -- The trading day the count belongs to, as printed/written on the sheet —
    -- confirmed by a person when the extractor was unsure. Never derived from
    -- created_at.
    business_date  date not null,
    -- "3 PM", "closing" — whatever the sheet's Time field says, verbatim.
    counted_at_label text,

    status         stock_count_status not null default 'extracting',
    -- Which model + prompt read the sheet. A re-extraction under a newer
    -- prompt is then distinguishable from the original, same as the sales
    -- adapter_version.
    extractor      text,

    page_count     integer,
    confirmed_by   uuid references profiles (id) on delete set null,
    confirmed_at   timestamptz,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz,

    constraint stock_counts_confirmed_has_actor
        check (status <> 'confirmed' or confirmed_by is not null)
);

create index stock_counts_outlet_date_idx
    on stock_counts (outlet_id, business_date desc);


create table stock_count_lines (
    id           uuid primary key default gen_random_uuid(),
    count_id     uuid not null references stock_counts (id) on delete cascade,

    page         integer,
    sl_no        integer,

    -- What the extractor read, verbatim. These columns are append-only in
    -- spirit: corrections happen in the derived columns below, never here,
    -- so the paper trail survives every edit.
    raw_name        text not null,
    raw_closing     text,
    raw_requisition text,
    extract_confidence numeric(4, 3),

    -- What deterministic code (or a person) derived.
    item_id      uuid references inventory_items (id),
    -- 'exact' | 'bengali' | 'alias' | 'fuzzy' | 'human'. Null while unmapped.
    match_method text,
    -- Closing count normalised into the ITEM's canonical unit. Null when the
    -- cell was blank or the parse was refused (see needs_review).
    qty            numeric,
    requested_qty  numeric,
    -- The parser's working: what it assumed, what it refused, and why —
    -- rendered under the row so a reviewer sees the reasoning, not a verdict.
    parse_detail   jsonb not null default '{}'::jsonb,

    needs_review boolean not null default true,
    reviewed_by  uuid references profiles (id) on delete set null,
    reviewed_at  timestamptz,

    created_at   timestamptz not null default now(),
    updated_at   timestamptz,

    constraint stock_lines_qty_non_negative
        check (qty is null or qty >= 0),
    constraint stock_lines_requested_non_negative
        check (requested_qty is null or requested_qty >= 0)
);

create index stock_count_lines_count_idx
    on stock_count_lines (count_id, page, sl_no);

create index stock_count_lines_review_idx
    on stock_count_lines (count_id)
    where needs_review;


-- "Human confirms unmatched rows ONCE, the mapping is remembered." This is
-- the memory. An alias is the normalised raw string a sheet used for an item;
-- next month's sheet with the same spelling maps without asking anyone.
create table inventory_item_aliases (
    id         uuid primary key default gen_random_uuid(),
    item_id    uuid not null references inventory_items (id) on delete cascade,
    alias      text not null,
    created_by uuid references profiles (id) on delete set null,
    created_at timestamptz not null default now()
);

-- One spelling maps to exactly one item, or the memory is worthless.
create unique index inventory_item_aliases_alias_uq
    on inventory_item_aliases (lower(alias));


create table requisitions (
    id            uuid primary key default gen_random_uuid(),
    outlet_id     uuid not null references outlets (id) on delete cascade,
    -- The confirmed count this was computed from. Restrict, not cascade: a
    -- requisition that outlives its evidence is exactly the untraceable
    -- number this schema exists to prevent.
    count_id      uuid not null references stock_counts (id) on delete restrict,
    business_date date not null,
    status        requisition_status not null default 'draft',

    created_by    uuid references profiles (id) on delete set null,
    created_at    timestamptz not null default now(),
    finalised_by  uuid references profiles (id) on delete set null,
    finalised_at  timestamptz,
    updated_at    timestamptz,

    constraint requisitions_final_has_actor
        check (status <> 'final' or finalised_by is not null)
);

create index requisitions_outlet_date_idx
    on requisitions (outlet_id, business_date desc);


create table requisition_lines (
    id             uuid primary key default gen_random_uuid(),
    requisition_id uuid not null references requisitions (id) on delete cascade,
    item_id        uuid not null references inventory_items (id),

    -- The inputs, snapshotted at computation time so the arithmetic stays
    -- reproducible after par levels change.
    on_hand       numeric,
    par_level     numeric,
    order_unit    numeric,

    -- max(0, par - on_hand), rounded UP to order_unit. Null when the outlet
    -- has no par for the item — no formula, no number, never a guess.
    suggested_qty numeric,
    -- What the chef wrote on the sheet.
    requested_qty numeric,
    -- What the manager settles on. Defaults to requested; editing it is the
    -- manager's call and the whole point of the screen.
    final_qty     numeric,

    -- 'padding' when requested > 1.3 x suggested (spec's day-one anomaly),
    -- 'no_par' when suggestion was impossible.
    flags         text[] not null default '{}',
    -- The formula's working, shown to the manager: inputs, steps, result.
    detail        jsonb not null default '{}'::jsonb,

    unique (requisition_id, item_id),
    constraint req_lines_final_non_negative
        check (final_qty is null or final_qty >= 0)
);


create trigger stock_counts_set_updated_at
    before update on stock_counts
    for each row execute function set_updated_at();

create trigger stock_count_lines_set_updated_at
    before update on stock_count_lines
    for each row execute function set_updated_at();

create trigger requisitions_set_updated_at
    before update on requisitions
    for each row execute function set_updated_at();


-- --- RLS -------------------------------------------------------------------
-- Counts and requisitions are outlet data; aliases are network-wide reference
-- like the catalogue itself. Same second-line rule as everywhere: the API
-- writes, browsers read at most.

do $$
declare
    t text;
begin
    foreach t in array array[
        'stock_counts', 'stock_count_lines',
        'inventory_item_aliases', 'requisitions', 'requisition_lines'
    ]
    loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        execute format('revoke all on table %I from anon', t);
        execute format('grant select on table %I to authenticated', t);
    end loop;
end
$$;

create policy stock_counts_read_own_outlet on stock_counts
    for select to authenticated
    using (auth_is_global_admin() or outlet_id = any (auth_outlet_ids()));

create policy stock_count_lines_read_via_count on stock_count_lines
    for select to authenticated
    using (
        exists (
            select 1 from stock_counts c
             where c.id = stock_count_lines.count_id
               and (auth_is_global_admin() or c.outlet_id = any (auth_outlet_ids()))
        )
    );

create policy inventory_item_aliases_read_all on inventory_item_aliases
    for select to authenticated
    using (auth_profile_role() is not null);

create policy requisitions_read_own_outlet on requisitions
    for select to authenticated
    using (auth_is_global_admin() or outlet_id = any (auth_outlet_ids()));

create policy requisition_lines_read_via_requisition on requisition_lines
    for select to authenticated
    using (
        exists (
            select 1 from requisitions r
             where r.id = requisition_lines.requisition_id
               and (auth_is_global_admin() or r.outlet_id = any (auth_outlet_ids()))
        )
    );
