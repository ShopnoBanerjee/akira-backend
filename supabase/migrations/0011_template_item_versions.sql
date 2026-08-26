-- ---------------------------------------------------------------------------
-- 0011 — Template item version history
--
-- Closes a gap that opens as soon as admins can edit checklist items freely.
--
-- checklist_runs already snapshots template_version, and the spec requires that
-- historical runs always render against the item definitions that were live
-- when they ran. But template_version pointed at nothing: the item rows are
-- mutated in place, so a run from three weeks ago would re-render against
-- today's flags. Flip is_critical on an item and every past run that used it
-- would retroactively appear to have had a critical failure.
--
-- This table is the thing template_version refers to. Every material edit to an
-- item writes a new row here at the new template version; the live
-- checklist_template_items row stays the current definition, and run items
-- point at the exact version they were performed against.
--
-- Same philosophy as app_settings in 0010: history is reproducible, not
-- rewritten. There the value in force is resolved by effective date; here the
-- definition in force is resolved by template version.
-- ---------------------------------------------------------------------------

create table checklist_template_item_versions (
    id                uuid primary key default gen_random_uuid(),
    template_item_id  uuid not null references checklist_template_items (id) on delete cascade,
    template_id       uuid not null references checklist_templates (id) on delete cascade,

    -- The checklist_templates.version this definition belongs to.
    template_version  integer not null,

    -- Full copy of the definition as it stood at that version.
    sort_order        integer not null,
    title             text not null,
    title_bn          text,
    instruction       text,
    instruction_bn    text,
    reference_photo_path text,

    requires_photo    boolean not null,
    requires_value    boolean not null,
    value_type        value_type,
    value_min         numeric,
    value_max         numeric,
    value_unit        text,
    is_critical       boolean not null,
    allow_na          boolean not null,

    -- True when the item was already soft-deleted at this version, so a run
    -- that predates the deletion still renders it and later ones do not.
    is_deleted        boolean not null default false,

    changed_by        uuid references profiles (id) on delete set null,
    change_note       text,
    created_at        timestamptz not null default now(),

    unique (template_item_id, template_version),
    constraint template_item_versions_version_positive check (template_version >= 1)
);

comment on table checklist_template_item_versions is
    'Immutable definition of a checklist item at one template version. Runs render against this, never against the live item row.';


-- Point run items at the exact definition they were performed against.
-- Nullable: rows created before this migration have no version to reference,
-- and the materialiser backfills it going forward.
alter table checklist_run_items
    add column template_item_version_id uuid
        references checklist_template_item_versions (id) on delete set null;

comment on column checklist_run_items.template_item_version_id is
    'The item definition this was answered against. Null only for rows predating 0011.';


create index template_item_versions_lookup_idx
    on checklist_template_item_versions (template_id, template_version, sort_order);

create index template_item_versions_item_idx
    on checklist_template_item_versions (template_item_id, template_version desc);

create index checklist_run_items_version_idx
    on checklist_run_items (template_item_version_id)
    where template_item_version_id is not null;


-- Seed the history with the current state of every existing item, so version 1
-- of every template is retrievable rather than starting life with a hole.
insert into checklist_template_item_versions (
    template_item_id, template_id, template_version, sort_order,
    title, title_bn, instruction, instruction_bn, reference_photo_path,
    requires_photo, requires_value, value_type, value_min, value_max, value_unit,
    is_critical, allow_na, is_deleted, change_note
)
select
    i.id, i.template_id, t.version, i.sort_order,
    i.title, i.title_bn, i.instruction, i.instruction_bn, i.reference_photo_path,
    i.requires_photo, i.requires_value, i.value_type, i.value_min, i.value_max, i.value_unit,
    i.is_critical, i.allow_na, i.deleted_at is not null,
    'Backfilled from the live item row when version history was introduced.'
from checklist_template_items i
join checklist_templates t on t.id = i.template_id
on conflict (template_item_id, template_version) do nothing;


alter table checklist_template_item_versions enable row level security;
alter table checklist_template_item_versions force row level security;
revoke all on table checklist_template_item_versions from anon;
grant select on table checklist_template_item_versions to authenticated;

-- Template definitions are network-wide reference data, same as the live items.
create policy template_item_versions_read_all on checklist_template_item_versions
    for select to authenticated
    using (auth_profile_role() is not null);
