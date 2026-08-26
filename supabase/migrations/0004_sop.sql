-- ---------------------------------------------------------------------------
-- 0004 — SOP compliance module
--
-- Bilingual throughout. Every one of AKIRA's paper checklists carries an
-- English and a Bengali column, and the kitchen reads Bengali. An English-only
-- rendering would be less usable than the paper it replaces.
-- ---------------------------------------------------------------------------

create table sop_categories (
    id          uuid primary key default gen_random_uuid(),
    key         text not null unique,   -- 'opening' | 'cleaning' | 'food_safety' | ...
    label       text not null,
    label_bn    text,
    sort_order  integer not null default 0,
    icon        text,
    created_at  timestamptz not null default now(),
    updated_at  timestamptz
);


create table checklist_templates (
    id           uuid primary key default gen_random_uuid(),
    category_id  uuid not null references sop_categories (id),
    name         text not null,
    name_bn      text,
    description  text,
    frequency    frequency not null,
    day_part     day_part not null default 'any',

    -- Bumped on any material change to the item set or to an item's meaning.
    -- Runs snapshot the version at creation so history always renders against
    -- the definitions that were live when it ran.
    version      integer not null default 1,

    is_active    boolean not null default true,
    created_by   uuid references profiles (id) on delete set null,
    created_at   timestamptz not null default now(),
    updated_at   timestamptz,
    deleted_at   timestamptz,

    constraint checklist_templates_version_positive check (version >= 1)
);


create table checklist_template_items (
    id            uuid primary key default gen_random_uuid(),
    template_id   uuid not null references checklist_templates (id) on delete cascade,
    sort_order    integer not null,

    title            text not null,
    title_bn         text,
    instruction      text,
    instruction_bn   text,

    -- Fallback reference shot. Per-outlet standards live in
    -- outlet_item_reference_photos and take precedence over this.
    reference_photo_path text,

    requires_photo  boolean not null default false,
    requires_value  boolean not null default false,
    value_type      value_type,
    value_min       numeric,
    value_max       numeric,
    value_unit      text,

    -- Weight 3 in scoring; a failure raises an exception immediately.
    is_critical     boolean not null default false,
    allow_na        boolean not null default false,

    created_at      timestamptz not null default now(),
    updated_at      timestamptz,
    -- Soft delete only, once a run references the item. History must stay
    -- renderable, so a used item is never hard-deleted.
    deleted_at      timestamptz,

    unique (template_id, sort_order) deferrable initially deferred,
    constraint template_items_value_type_present
        check (not requires_value or value_type is not null),
    constraint template_items_value_range_ordered
        check (value_min is null or value_max is null or value_min <= value_max)
);

comment on constraint template_items_value_type_present on checklist_template_items is
    'An item that demands a value must say what kind of value it is.';


-- Per-outlet photographic standard for an item. AKIRA requires each outlet to
-- hold its own reference shots: the New Town clean prep station is not another
-- outlet clean prep station. The AI reviewer compares a submitted photo against
-- the reference for that outlet, falling back to the generic template one.
create table outlet_item_reference_photos (
    id                uuid primary key default gen_random_uuid(),
    outlet_id         uuid not null references outlets (id) on delete cascade,
    template_item_id  uuid not null references checklist_template_items (id) on delete cascade,
    photo_path        text not null,
    caption           text,
    caption_bn        text,

    -- Standards are captured under normal service lighting, so a later
    -- submission can be compared on like terms. Populated by the same
    -- luminance check that flags dark submissions.
    luminance_mean    numeric,

    captured_by       uuid references profiles (id) on delete set null,
    captured_at       timestamptz not null default now(),
    is_active         boolean not null default true,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz,
    deleted_at        timestamptz
);

-- One active standard per outlet per item; superseded ones stay for history.
create unique index outlet_item_reference_photos_active_uq
    on outlet_item_reference_photos (outlet_id, template_item_id)
    where is_active and deleted_at is null;


create table checklist_assignments (
    id               uuid primary key default gen_random_uuid(),
    template_id      uuid not null references checklist_templates (id) on delete cascade,
    outlet_id        uuid not null references outlets (id) on delete cascade,
    assigned_role    user_role not null,

    -- 0 = Sunday, matching Postgres extract(dow).
    active_weekdays  integer[] not null default '{0,1,2,3,4,5,6}',

    -- For cadences that do not fit a weekly cycle: alternate_day is
    -- interval_days = 2, fortnightly is 14. Occurrence is counted from
    -- anchor_date. Null interval_days means "use active_weekdays".
    interval_days    integer,
    anchor_date      date,

    due_time_local   time not null,
    grace_minutes    integer not null default 30,
    is_active        boolean not null default true,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz,
    deleted_at       timestamptz,

    constraint assignments_grace_non_negative check (grace_minutes >= 0),
    constraint assignments_interval_positive
        check (interval_days is null or interval_days > 0),
    -- An interval cadence is meaningless without a point to count from.
    constraint assignments_interval_needs_anchor
        check (interval_days is null or anchor_date is not null),
    constraint assignments_weekdays_valid
        check (active_weekdays <@ array[0,1,2,3,4,5,6])
);


create table checklist_runs (
    id                uuid primary key default gen_random_uuid(),
    assignment_id     uuid not null references checklist_assignments (id) on delete cascade,
    template_id       uuid not null references checklist_templates (id),
    template_version  integer not null,
    outlet_id         uuid not null references outlets (id) on delete cascade,
    business_date     date not null,
    day_part          day_part not null default 'any',
    status            run_status not null default 'pending',

    started_by        uuid references profiles (id) on delete set null,
    started_at        timestamptz,
    submitted_by      uuid references profiles (id) on delete set null,
    submitted_at      timestamptz,
    approved_by       uuid references profiles (id) on delete set null,
    approved_at       timestamptz,
    rejection_reason  text,

    -- The device the run was performed on, when it came from a shared tablet.
    device_id         uuid references outlet_devices (id) on delete set null,

    due_at            timestamptz,
    is_late           boolean not null default false,
    minutes_late      integer,

    score_pct             numeric(5, 2),
    critical_fail_count   integer not null default 0,
    integrity_flag_count  integer not null default 0,

    submit_geo_lat    double precision,
    submit_geo_lng    double precision,
    -- Null means the device withheld location, which is a permission staff
    -- often cannot change. That is counted separately and is never a flag.
    geo_ok            boolean,

    created_at        timestamptz not null default now(),
    updated_at        timestamptz,

    unique (assignment_id, business_date, day_part),

    -- Separation of duties. Enforced here and not only in the UI, because
    -- without it the entire compliance system is theatre.
    constraint checklist_runs_approver_is_not_submitter
        check (approved_by is null or approved_by <> submitted_by),
    constraint checklist_runs_score_range
        check (score_pct is null or score_pct between 0 and 100)
);

comment on constraint checklist_runs_approver_is_not_submitter on checklist_runs is
    'The approver of a run can never be its submitter.';


create table checklist_run_items (
    id                uuid primary key default gen_random_uuid(),
    run_id            uuid not null references checklist_runs (id) on delete cascade,
    template_item_id  uuid not null references checklist_template_items (id),
    sort_order        integer not null,

    result            item_result not null default 'pending',
    value_numeric     numeric,
    value_text        text,
    out_of_range      boolean not null default false,
    note              text,

    photo_path         text,
    photo_uploaded_at  timestamptz,
    photo_bytes        bigint,
    photo_phash        text,
    -- Mean luminance of the submitted photo. "Taken under visible light" is a
    -- deterministic check, not a judgement call, so code decides it.
    photo_luminance    numeric,

    -- 'duplicate_photo' | 'burst_upload' | 'out_of_geofence' | 'late'
    -- | 'stale_capture' | 'too_dark' | 'ai_mismatch'
    integrity_flags   text[] not null default '{}',

    completed_at      timestamptz,
    created_at        timestamptz not null default now(),
    updated_at        timestamptz,

    unique (run_id, template_item_id),
    constraint run_items_fail_needs_note
        check (result <> 'fail' or note is not null or photo_path is not null)
);


-- The AI reviewer opinion on one photo. Kept in its own table so a review can
-- be re-run against a newer model without destroying what an earlier one said,
-- and so the model and prompt version behind any verdict stay auditable.
--
-- Advisory only. Nothing here blocks a submission or approves a run: a manager
-- still decides, with this in front of them.
create table run_item_ai_reviews (
    id                  uuid primary key default gen_random_uuid(),
    run_item_id         uuid not null references checklist_run_items (id) on delete cascade,
    reference_photo_id  uuid references outlet_item_reference_photos (id) on delete set null,

    verdict     ai_verdict not null,
    confidence  numeric(4, 3),
    rationale   text not null,

    model            text not null,
    prompt_version   text not null,
    latency_ms       integer,
    reviewed_at      timestamptz not null default now(),
    created_at       timestamptz not null default now(),

    constraint ai_reviews_confidence_range
        check (confidence is null or confidence between 0 and 1)
);

comment on table run_item_ai_reviews is
    'Advisory AI verdict on a submitted photo. Never blocks submission, never approves a run.';

create unique index run_item_ai_reviews_latest_uq
    on run_item_ai_reviews (run_item_id, model, prompt_version);


create table sop_exceptions (
    id               uuid primary key default gen_random_uuid(),
    run_item_id      uuid references checklist_run_items (id) on delete set null,
    outlet_id        uuid not null references outlets (id) on delete cascade,
    business_date    date not null,
    severity         severity not null,
    title            text not null,
    detail           text,
    photo_path       text,
    status           exception_status not null default 'open',
    assigned_to      uuid references profiles (id) on delete set null,
    resolved_by      uuid references profiles (id) on delete set null,
    resolved_at      timestamptz,
    resolution_note  text,
    created_at       timestamptz not null default now(),
    updated_at       timestamptz,

    constraint sop_exceptions_resolution_recorded
        check (status <> 'resolved' or (resolved_by is not null and resolved_at is not null))
);


create trigger sop_categories_set_updated_at
    before update on sop_categories
    for each row execute function set_updated_at();

create trigger checklist_templates_set_updated_at
    before update on checklist_templates
    for each row execute function set_updated_at();

create trigger checklist_template_items_set_updated_at
    before update on checklist_template_items
    for each row execute function set_updated_at();

create trigger outlet_item_reference_photos_set_updated_at
    before update on outlet_item_reference_photos
    for each row execute function set_updated_at();

create trigger checklist_assignments_set_updated_at
    before update on checklist_assignments
    for each row execute function set_updated_at();

create trigger checklist_runs_set_updated_at
    before update on checklist_runs
    for each row execute function set_updated_at();

create trigger checklist_run_items_set_updated_at
    before update on checklist_run_items
    for each row execute function set_updated_at();

create trigger sop_exceptions_set_updated_at
    before update on sop_exceptions
    for each row execute function set_updated_at();
