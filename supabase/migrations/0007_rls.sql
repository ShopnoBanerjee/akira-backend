-- ---------------------------------------------------------------------------
-- 0007 — Row level security
--
-- FastAPI connects with the service role and enforces authorisation in code.
-- These policies are the second line of defence, not the first: they exist so
-- that a leaked publishable key, or some future direct-read path, still cannot
-- pull another outlet's data.
--
-- Shape of the rule:
--   anon           — nothing, ever.
--   authenticated  — READ ONLY, and only rows for outlets they belong to.
--                    Owners and ops_managers read every outlet.
--   service_role   — bypasses RLS; this is how the API works.
--
-- Note there are no insert, update or delete policies for authenticated. Every
-- write goes through the API, which is the only place the business rules and
-- audit writes live. A browser must never write here directly.
-- ---------------------------------------------------------------------------

-- --- Helpers ---------------------------------------------------------------
-- All security definer, so they can read membership without tripping the very
-- policies they are used by. search_path is pinned to defeat shadowing.

create or replace function auth_profile_role()
returns user_role
language sql
stable
security definer
set search_path = public, pg_temp
as $$
    select p.global_role
    from profiles p
    where p.id = auth.uid()
      and p.deleted_at is null
      and p.is_active
$$;

create or replace function auth_is_global_admin()
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
    select coalesce(auth_profile_role() in ('owner', 'ops_manager'), false)
$$;

-- The outlets this user may see. Global admins are handled separately in each
-- policy rather than by returning every outlet id here, so the intent stays
-- readable at the point of use.
create or replace function auth_outlet_ids()
returns uuid[]
language sql
stable
security definer
set search_path = public, pg_temp
as $$
    select coalesce(array_agg(om.outlet_id), '{}')
    from outlet_members om
    join profiles p on p.id = om.profile_id
    where om.profile_id = auth.uid()
      and om.deleted_at is null
      and p.is_active
      and p.deleted_at is null
$$;

revoke all on function auth_profile_role() from public;
revoke all on function auth_is_global_admin() from public;
revoke all on function auth_outlet_ids() from public;
grant execute on function auth_profile_role() to authenticated;
grant execute on function auth_is_global_admin() to authenticated;
grant execute on function auth_outlet_ids() to authenticated;


-- --- Enable RLS everywhere -------------------------------------------------
-- force row level security applies the policies to the table owner too, so a
-- mistake in a migration cannot quietly read across outlets.

do $$
declare
    t text;
begin
    foreach t in array array[
        'outlets', 'profiles', 'outlet_members', 'outlet_devices', 'audit_log',
        'sop_categories', 'checklist_templates', 'checklist_template_items',
        'outlet_item_reference_photos', 'checklist_assignments',
        'checklist_runs', 'checklist_run_items', 'run_item_ai_reviews',
        'sop_exceptions', 'data_uploads', 'sales_orders', 'sales_order_items',
        'job_runs'
    ]
    loop
        execute format('alter table %I enable row level security', t);
        execute format('alter table %I force row level security', t);
        execute format('revoke all on table %I from anon', t);
        execute format('grant select on table %I to authenticated', t);
    end loop;
end
$$;


-- --- Outlet-scoped tables --------------------------------------------------
-- Everything carrying an outlet_id follows one rule, so generate the policies
-- rather than hand-writing eighteen near-identical blocks and getting one
-- subtly wrong.

do $$
declare
    t text;
begin
    foreach t in array array[
        'outlet_devices', 'outlet_item_reference_photos',
        'checklist_assignments', 'checklist_runs', 'sop_exceptions',
        'data_uploads', 'sales_orders', 'sales_order_items', 'job_runs'
    ]
    loop
        execute format($f$
            create policy %1$I_read_own_outlet on %1$I
                for select to authenticated
                using (
                    auth_is_global_admin()
                    or (outlet_id is not null and outlet_id = any (auth_outlet_ids()))
                )
        $f$, t);
    end loop;
end
$$;


-- --- Tables reached through a parent ---------------------------------------

create policy outlets_read_own on outlets
    for select to authenticated
    using (
        auth_is_global_admin()
        or id = any (auth_outlet_ids())
    );

-- You can always see yourself. Global admins see everyone; an outlet manager
-- sees the people who work at their outlets.
create policy profiles_read_self_or_colleagues on profiles
    for select to authenticated
    using (
        id = auth.uid()
        or auth_is_global_admin()
        or exists (
            select 1
            from outlet_members om
            where om.profile_id = profiles.id
              and om.deleted_at is null
              and om.outlet_id = any (auth_outlet_ids())
        )
    );

create policy outlet_members_read_own_outlet on outlet_members
    for select to authenticated
    using (
        auth_is_global_admin()
        or outlet_id = any (auth_outlet_ids())
        or profile_id = auth.uid()
    );

create policy audit_log_read_own_outlet on audit_log
    for select to authenticated
    using (
        auth_is_global_admin()
        or (outlet_id is not null and outlet_id = any (auth_outlet_ids()))
    );

create policy checklist_run_items_read_via_run on checklist_run_items
    for select to authenticated
    using (
        exists (
            select 1
            from checklist_runs r
            where r.id = checklist_run_items.run_id
              and (auth_is_global_admin() or r.outlet_id = any (auth_outlet_ids()))
        )
    );

create policy run_item_ai_reviews_read_via_item on run_item_ai_reviews
    for select to authenticated
    using (
        exists (
            select 1
            from checklist_run_items ri
            join checklist_runs r on r.id = ri.run_id
            where ri.id = run_item_ai_reviews.run_item_id
              and (auth_is_global_admin() or r.outlet_id = any (auth_outlet_ids()))
        )
    );


-- --- Network-wide reference data -------------------------------------------
-- Categories and templates are not outlet-specific: the same SOP definition is
-- shared across outlets. Readable by any active authenticated user, still
-- writable only through the API.

create policy sop_categories_read_all on sop_categories
    for select to authenticated
    using (auth_profile_role() is not null);

create policy checklist_templates_read_all on checklist_templates
    for select to authenticated
    using (auth_profile_role() is not null);

create policy checklist_template_items_read_all on checklist_template_items
    for select to authenticated
    using (auth_profile_role() is not null);
