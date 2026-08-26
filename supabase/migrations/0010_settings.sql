-- ---------------------------------------------------------------------------
-- 0010 — Admin-editable settings
--
-- Behaviour that used to be constants in code: scoring weights and health
-- bands, integrity thresholds, AI review controls, job times and notification
-- recipients.
--
-- Two design rules make this safe rather than a footgun:
--
-- 1. APPEND-ONLY WITH AN EFFECTIVE DATE. A change inserts a new row rather
--    than updating one. The value in force for any moment is the newest row
--    whose effective_from is at or before it. Scoring a period from three
--    months ago therefore uses the weights that were live then, so historical
--    outlet scores stay reproducible instead of silently rewriting themselves
--    every time somebody nudges a weight.
--
-- 2. THE CODE OWNS THE SCHEMA OF SETTINGS, NOT THIS TABLE. app/core/settings.py
--    holds a registry: every known key with its type, its default, its valid
--    range and whether an outlet may override it. A key absent from the
--    registry is ignored on read. That is what stops a typo, or a value out of
--    range, from quietly breaking scoring.
--
-- Deliberately NOT a setting: the 05:00 business-date rollover. business_date()
-- is declared immutable so the planner can use it in index expressions; reading
-- a runtime value would force it down to stable and cost those indexes. Worse,
-- every historical row already stores its business_date, so a changed rule
-- would disagree with months of recorded data. Changing it is a migration plus
-- an explicit backfill, on purpose.
-- ---------------------------------------------------------------------------

create type setting_scope as enum ('global', 'outlet');

create table app_settings (
    id         uuid primary key default gen_random_uuid(),
    key        text not null,
    scope      setting_scope not null default 'global',

    -- Null for global, required for an outlet override.
    outlet_id  uuid references outlets (id) on delete cascade,

    -- jsonb so one table carries numbers, booleans, strings and lists without
    -- a column per type. The registry in code says what shape each key takes
    -- and validates before write.
    value      jsonb not null,

    -- When this value starts applying. Defaults to now; a future date lets a
    -- change be staged, and a past one lets a correction be backdated.
    effective_from timestamptz not null default now(),

    note       text,   -- why it was changed, shown in the settings history UI
    set_by     uuid references profiles (id) on delete set null,
    created_at timestamptz not null default now(),

    constraint app_settings_outlet_scope_consistent
        check (
            (scope = 'global' and outlet_id is null)
            or (scope = 'outlet' and outlet_id is not null)
        )
);

comment on table app_settings is
    'Append-only. A change inserts a new row; the value in force is the newest row with effective_from <= the moment being evaluated.';

-- One value per key per scope per instant. Re-setting the same key at the same
-- timestamp is a mistake, not two settings.
create unique index app_settings_global_uq
    on app_settings (key, effective_from)
    where scope = 'global';

create unique index app_settings_outlet_uq
    on app_settings (key, outlet_id, effective_from)
    where scope = 'outlet';

-- The hot path: "what is this key set to right now, for this outlet".
create index app_settings_lookup_idx
    on app_settings (key, effective_from desc);


-- Resolve one setting at a moment in time. An outlet override wins over the
-- global value; null means no row exists and the caller falls back to the
-- registry default in app/core/settings.py.
create or replace function setting_value(
    p_key       text,
    p_outlet_id uuid default null,
    p_at        timestamptz default now()
)
returns jsonb
language sql
stable
as $$
    select s.value
    from app_settings s
    where s.key = p_key
      and s.effective_from <= p_at
      and (
          (s.scope = 'outlet' and s.outlet_id = p_outlet_id)
          or s.scope = 'global'
      )
    -- Outlet override first, then most recent.
    order by (s.scope = 'outlet') desc, s.effective_from desc
    limit 1
$$;

comment on function setting_value(text, uuid, timestamptz) is
    'Value in force for a key at a moment. Outlet override beats global. Null means use the registry default in code.';


alter table app_settings enable row level security;
alter table app_settings force row level security;
revoke all on table app_settings from anon;
grant select on table app_settings to authenticated;

-- Settings are not secrets, but an outlet override is only visible to people
-- who can see that outlet. Writes go through the API, which restricts them to
-- owner and (for operational keys) ops_manager.
create policy app_settings_read on app_settings
    for select to authenticated
    using (
        auth_is_global_admin()
        or scope = 'global'
        or outlet_id = any (auth_outlet_ids())
    );
