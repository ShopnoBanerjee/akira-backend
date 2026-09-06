-- ---------------------------------------------------------------------------
-- 0026 — Organisations: the tenant above the outlet (P26a, D33)
--
-- Everything that was brand-level - templates, catalogue, menu map, settings,
-- people, outlets - now belongs to an organisation. Outlet-scoped rows inherit
-- theirs through the outlet and are untouched. Two organisations exist from
-- this migration on:
--
--   akira      AKIRA's real organisation: outlet AKR-SP01 and the real owner
--   akira-dev  everything that was here before: the two test outlets, the
--              seeded checklists, the @akira.test accounts, the Petpooja
--              uploads that live on AKR-NT01. The owner asked for the real
--              account to be cut out of the development items; in a tenant
--              model that is a second tenant.
--
-- Isolation is enforced three times, as the outlet rule already is: in the
-- API guards, here in RLS, and in tests that act as the wrong organisation's
-- owner. The RLS trick that keeps this migration small: every outlet-scoped
-- policy already reads `auth_is_global_admin() or outlet_id = any
-- (auth_outlet_ids())`. Those two helpers are redefined below so that
-- "global admin" means platform admin only, and "my outlet ids" means every
-- outlet of my organisation for an owner or ops manager. Forty policies become
-- organisation-safe without being rewritten; only the organisation-level
-- content tables get new policies.
-- ---------------------------------------------------------------------------

-- --- 1. The tenant ----------------------------------------------------------

create table organisations (
    id            uuid primary key default gen_random_uuid(),
    slug          text not null unique,
    name          text not null,
    is_active     boolean not null default true,
    -- Set when the onboarding checklist is complete (P26b). Until then the
    -- organisation is in development and MFA is not enforced on its owners.
    onboarded_at  timestamptz,
    -- The owner's answer: a cap exists, and it is 100000 for now.
    max_outlets   integer not null default 100000,
    max_people    integer not null default 100000,
    created_by    uuid,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz,
    deleted_at    timestamptz,

    constraint organisations_slug_shape check (slug ~ '^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$'),
    constraint organisations_caps_positive check (max_outlets > 0 and max_people > 0)
);

comment on table organisations is
    'A tenant: one restaurant business. Owns outlets, people, templates, catalogue, menu map and settings. Platform admins belong to none.';

insert into organisations (id, slug, name, onboarded_at) values
    ('a1000000-0000-4000-8000-000000000001', 'akira',     'AKIRA', now()),
    ('a1000000-0000-4000-8000-000000000002', 'akira-dev', 'AKIRA (development)', null);

-- --- 2. Outlets and people belong to one ----------------------------------

alter table outlets  add column organisation_id uuid references organisations (id);
alter table profiles add column organisation_id uuid references organisations (id);

update outlets set organisation_id = 'a1000000-0000-4000-8000-000000000001'
 where code = 'AKR-SP01';
update outlets set organisation_id = 'a1000000-0000-4000-8000-000000000002'
 where organisation_id is null;
alter table outlets alter column organisation_id set not null;

-- Codes are unique within an organisation now, not across the platform.
alter table outlets drop constraint outlets_code_key;
create unique index outlets_org_code_uq on outlets (organisation_id, code);
create index outlets_org_idx on outlets (organisation_id) where deleted_at is null;

-- The real owner goes to AKIRA; every other existing person is development.
update profiles p set organisation_id = 'a1000000-0000-4000-8000-000000000001'
  from auth.users u
 where u.id = p.id and lower(u.email) = 'management@simplyakira.com';
update profiles set organisation_id = 'a1000000-0000-4000-8000-000000000002'
 where organisation_id is null and global_role <> 'platform_admin';
create index profiles_org_idx on profiles (organisation_id) where deleted_at is null;

comment on column profiles.organisation_id is
    'Null only for platform_admin. Every other person belongs to exactly one organisation.';

-- --- 3. Organisation-level content ----------------------------------------
-- Null organisation_id = the platform starter kit (P26c): readable by every
-- organisation, editable only by a platform admin.

alter table checklist_templates    add column organisation_id uuid references organisations (id);
alter table sop_categories         add column organisation_id uuid references organisations (id);
alter table inventory_departments  add column organisation_id uuid references organisations (id);
alter table inventory_categories   add column organisation_id uuid references organisations (id);
alter table inventory_items        add column organisation_id uuid references organisations (id);
alter table inventory_item_aliases add column organisation_id uuid references organisations (id);
alter table menu_items             add column organisation_id uuid references organisations (id);
alter table menu_item_aliases      add column organisation_id uuid references organisations (id);
alter table recipes                add column organisation_id uuid references organisations (id);

update checklist_templates    set organisation_id = 'a1000000-0000-4000-8000-000000000002' where organisation_id is null;
update sop_categories         set organisation_id = 'a1000000-0000-4000-8000-000000000002' where organisation_id is null;
update inventory_departments  set organisation_id = 'a1000000-0000-4000-8000-000000000002' where organisation_id is null;
update inventory_categories   set organisation_id = 'a1000000-0000-4000-8000-000000000002' where organisation_id is null;
update inventory_items        set organisation_id = 'a1000000-0000-4000-8000-000000000002' where organisation_id is null;
update inventory_item_aliases set organisation_id = 'a1000000-0000-4000-8000-000000000002' where organisation_id is null;
update menu_items             set organisation_id = 'a1000000-0000-4000-8000-000000000002' where organisation_id is null;
update menu_item_aliases      set organisation_id = 'a1000000-0000-4000-8000-000000000002' where organisation_id is null;
update recipes                set organisation_id = 'a1000000-0000-4000-8000-000000000002' where organisation_id is null;

-- Uniqueness per organisation. The library (null) counts as its own scope.
alter table sop_categories        drop constraint sop_categories_key_key;
alter table inventory_departments drop constraint inventory_departments_key_key;
alter table inventory_categories  drop constraint inventory_categories_key_key;
alter table inventory_items       drop constraint inventory_items_code_key;
alter table menu_items            drop constraint menu_items_name_key;
alter table recipes               drop constraint recipes_menu_item_name_key;
drop index inventory_item_aliases_alias_uq;
drop index menu_item_aliases_alias_uq;

create unique index sop_categories_org_key_uq
    on sop_categories (coalesce(organisation_id, '00000000-0000-0000-0000-000000000000'), key);
create unique index inventory_departments_org_key_uq
    on inventory_departments (coalesce(organisation_id, '00000000-0000-0000-0000-000000000000'), key);
create unique index inventory_categories_org_key_uq
    on inventory_categories (coalesce(organisation_id, '00000000-0000-0000-0000-000000000000'), key);
create unique index inventory_items_org_code_uq
    on inventory_items (coalesce(organisation_id, '00000000-0000-0000-0000-000000000000'), code)
    where code is not null;
create unique index inventory_item_aliases_org_alias_uq
    on inventory_item_aliases (coalesce(organisation_id, '00000000-0000-0000-0000-000000000000'), lower(alias));
create unique index menu_items_org_name_uq
    on menu_items (coalesce(organisation_id, '00000000-0000-0000-0000-000000000000'), lower(name));
create unique index menu_item_aliases_org_alias_uq
    on menu_item_aliases (coalesce(organisation_id, '00000000-0000-0000-0000-000000000000'), lower(alias));
create unique index recipes_org_menu_item_uq
    on recipes (coalesce(organisation_id, '00000000-0000-0000-0000-000000000000'), menu_item_name);

-- --- 4. Settings: 'global' becomes 'organisation', except the platform's own -

alter table app_settings add column organisation_id uuid references organisations (id);

-- The scheduler is one process for the platform; its times stay global.
update app_settings
   set scope = 'organisation', organisation_id = 'a1000000-0000-4000-8000-000000000002'
 where scope = 'global' and key not like 'jobs.%';

-- AKIRA starts with the same values its development twin had (the restaurant
-- guard, targets, weights); history included, so "what was in force when" is
-- answerable for both.
insert into app_settings (key, scope, organisation_id, value, effective_from, note, set_by)
select key, 'organisation', 'a1000000-0000-4000-8000-000000000001', value, effective_from,
       coalesce(note, '') || ' [copied to akira at 0026]', set_by
  from app_settings
 where scope = 'organisation' and organisation_id = 'a1000000-0000-4000-8000-000000000002';

alter table app_settings drop constraint app_settings_outlet_scope_consistent;
alter table app_settings add constraint app_settings_scope_consistent check (
    (scope = 'global'       and outlet_id is null     and organisation_id is null)
    or (scope = 'organisation' and outlet_id is null and organisation_id is not null)
    or (scope = 'outlet'    and outlet_id is not null)
);
create unique index app_settings_org_uq
    on app_settings (key, organisation_id, effective_from)
    where scope = 'organisation';

-- Resolution: outlet override > organisation row > platform row > default.
-- The organisation is the outlet's when an outlet is given, else the one
-- passed explicitly (an owner reading organisation settings with no outlet).
-- The 0010 signature must go, or Postgres keeps both as overloads and every
-- three-argument call becomes ambiguous.
drop function if exists setting_value(text, uuid, timestamptz);

create or replace function setting_value(
    p_key             text,
    p_outlet_id       uuid default null,
    p_at              timestamptz default now(),
    p_organisation_id uuid default null
)
returns jsonb
language sql
stable
as $$
    with org as (
        select coalesce(
                   p_organisation_id,
                   (select o.organisation_id from outlets o where o.id = p_outlet_id)
               ) as id
    )
    select s.value
      from app_settings s, org
     where s.key = p_key
       and s.effective_from <= p_at
       and (
           (s.scope = 'outlet'       and s.outlet_id = p_outlet_id)
           or (s.scope = 'organisation' and s.organisation_id = org.id)
           or s.scope = 'global'
       )
     order by (s.scope = 'outlet') desc, (s.scope = 'organisation') desc, s.effective_from desc
     limit 1
$$;

-- --- 5. Audit and jobs carry the organisation -----------------------------

alter table audit_log add column organisation_id uuid references organisations (id);
alter table job_runs  add column organisation_id uuid references organisations (id);
update audit_log a set organisation_id = o.organisation_id from outlets o where o.id = a.outlet_id;
update job_runs  j set organisation_id = o.organisation_id from outlets o where o.id = j.outlet_id;
create index audit_log_org_idx on audit_log (organisation_id, at desc);

-- Group-wide event flags (outlet_id null) meant "every outlet". Now they
-- mean every outlet OF ONE organisation: a public holiday in Kolkata is not
-- a multiplier for a tenant in another city.
alter table forecast_events add column organisation_id uuid references organisations (id);
update forecast_events e set organisation_id = o.organisation_id
  from outlets o where o.id = e.outlet_id;
update forecast_events set organisation_id = 'a1000000-0000-4000-8000-000000000002'
 where organisation_id is null;
alter table forecast_events alter column organisation_id set not null;
create index forecast_events_org_idx on forecast_events (organisation_id, event_date);

-- --- 6. RLS helpers, redefined ---------------------------------------------

create or replace function auth_org_id()
returns uuid
language sql
stable
security definer
set search_path = public, pg_temp
as $$
    select p.organisation_id
      from profiles p
     where p.id = auth.uid() and p.deleted_at is null and p.is_active
$$;

create or replace function auth_is_platform_admin()
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
    select coalesce(auth_profile_role() = 'platform_admin', false)
$$;

create or replace function auth_is_org_admin()
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
    select coalesce(auth_profile_role() in ('owner', 'ops_manager'), false)
           and auth_org_id() is not null
$$;

-- Was "owner or ops manager, sees everything". Now: the platform admin only.
-- Every outlet-scoped policy of the form `auth_is_global_admin() or outlet_id
-- = any (auth_outlet_ids())` therefore no longer lets an owner cross into
-- another organisation.
create or replace function auth_is_global_admin()
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
    select auth_is_platform_admin()
$$;

-- Was "the outlets I am a member of". Now: for an owner or ops manager, every
-- outlet of my organisation; for everyone else, my memberships, restricted
-- to my organisation's outlets.
create or replace function auth_outlet_ids()
returns uuid[]
language sql
stable
security definer
set search_path = public, pg_temp
as $$
    select case
        when auth_is_org_admin() then
            coalesce((select array_agg(o.id) from outlets o
                       where o.organisation_id = auth_org_id() and o.deleted_at is null), '{}')
        else
            coalesce((select array_agg(om.outlet_id)
                        from outlet_members om
                        join profiles p on p.id = om.profile_id
                        join outlets o on o.id = om.outlet_id
                       where om.profile_id = auth.uid()
                         and om.deleted_at is null
                         and p.deleted_at is null and p.is_active
                         and o.organisation_id = auth_org_id()), '{}')
    end
$$;

-- --- 7. Policies for the organisation-level tables -------------------------

alter table organisations enable row level security;
alter table organisations force row level security;
revoke all on table organisations from anon;
grant select on table organisations to authenticated;
create policy organisations_read_own on organisations
    for select to authenticated
    using (auth_is_platform_admin() or id = auth_org_id());

drop policy forecast_events_read on forecast_events;
create policy forecast_events_read on forecast_events
    for select to authenticated
    using (
        auth_is_platform_admin()
        or (outlet_id is null and organisation_id = auth_org_id())
        or outlet_id = any (auth_outlet_ids())
    );

drop policy outlets_read_own on outlets;
create policy outlets_read_own on outlets
    for select to authenticated
    using (auth_is_platform_admin() or id = any (auth_outlet_ids()));

drop policy profiles_read_self_or_colleagues on profiles;
create policy profiles_read_self_or_colleagues on profiles
    for select to authenticated
    using (
        id = auth.uid()
        or auth_is_platform_admin()
        or (auth_is_org_admin() and organisation_id = auth_org_id())
        or exists (
            select 1
              from outlet_members om
             where om.profile_id = profiles.id
               and om.deleted_at is null
               and om.outlet_id = any (auth_outlet_ids())
        )
    );

drop policy training_records_read_self_or_colleagues on training_records;
create policy training_records_read_self_or_colleagues on training_records
    for select to authenticated
    using (
        profile_id = auth.uid()
        or auth_is_platform_admin()
        or exists (
            select 1 from profiles p
             where p.id = training_records.profile_id
               and p.organisation_id = auth_org_id()
               and (auth_is_org_admin()
                    or exists (select 1 from outlet_members om
                                where om.profile_id = p.id and om.deleted_at is null
                                  and om.outlet_id = any (auth_outlet_ids())))
        )
    );

drop policy app_settings_read on app_settings;
create policy app_settings_read on app_settings
    for select to authenticated
    using (
        auth_is_platform_admin()
        or scope = 'global'
        or (scope = 'organisation' and organisation_id = auth_org_id())
        or (scope = 'outlet' and outlet_id = any (auth_outlet_ids()))
    );

-- Content: mine, or the platform library.
drop policy sop_categories_read_all on sop_categories;
create policy sop_categories_read on sop_categories for select to authenticated
    using (auth_is_platform_admin() or organisation_id is null or organisation_id = auth_org_id());

drop policy checklist_templates_read_all on checklist_templates;
create policy checklist_templates_read on checklist_templates for select to authenticated
    using (auth_is_platform_admin() or organisation_id is null or organisation_id = auth_org_id());

drop policy checklist_template_items_read_all on checklist_template_items;
create policy checklist_template_items_read on checklist_template_items for select to authenticated
    using (exists (select 1 from checklist_templates t
                    where t.id = checklist_template_items.template_id
                      and (auth_is_platform_admin() or t.organisation_id is null
                           or t.organisation_id = auth_org_id())));

drop policy inventory_departments_read_all on inventory_departments;
create policy inventory_departments_read on inventory_departments for select to authenticated
    using (auth_is_platform_admin() or organisation_id is null or organisation_id = auth_org_id());

drop policy inventory_categories_read_all on inventory_categories;
create policy inventory_categories_read on inventory_categories for select to authenticated
    using (auth_is_platform_admin() or organisation_id is null or organisation_id = auth_org_id());

drop policy inventory_items_read_all on inventory_items;
create policy inventory_items_read on inventory_items for select to authenticated
    using (auth_is_platform_admin() or organisation_id is null or organisation_id = auth_org_id());

drop policy inventory_item_aliases_read_all on inventory_item_aliases;
create policy inventory_item_aliases_read on inventory_item_aliases for select to authenticated
    using (auth_is_platform_admin() or organisation_id is null or organisation_id = auth_org_id());

drop policy menu_items_read on menu_items;
create policy menu_items_read on menu_items for select to authenticated
    using (auth_is_platform_admin() or organisation_id is null or organisation_id = auth_org_id());

drop policy menu_item_aliases_read on menu_item_aliases;
create policy menu_item_aliases_read on menu_item_aliases for select to authenticated
    using (auth_is_platform_admin() or organisation_id is null or organisation_id = auth_org_id());

drop policy recipes_read on recipes;
create policy recipes_read on recipes for select to authenticated
    using (auth_is_platform_admin() or organisation_id is null or organisation_id = auth_org_id());

drop policy recipe_lines_read on recipe_lines;
create policy recipe_lines_read on recipe_lines for select to authenticated
    using (exists (select 1 from recipes r
                    where r.id = recipe_lines.recipe_id
                      and (auth_is_platform_admin() or r.organisation_id is null
                           or r.organisation_id = auth_org_id())));

-- Helpers are security definer; `authenticated` needs execute on the new ones
-- the same way 0021 granted the original three.
grant execute on function auth_org_id() to authenticated;
grant execute on function auth_is_platform_admin() to authenticated;
grant execute on function auth_is_org_admin() to authenticated;
revoke all on function auth_org_id() from anon;
revoke all on function auth_is_platform_admin() from anon;
revoke all on function auth_is_org_admin() from anon;
