-- ---------------------------------------------------------------------------
-- 0021 — The grant posture, stated once for the whole schema
--
-- SECURITY.md row 3 says: `authenticated` holds SELECT only, `anon` holds
-- nothing, on every public table. Until now that was true only as the sum of
-- per-table statements scattered through 0007–0019, each added when its table
-- was. Two things showed that to be fragile on 5 Sep 2026:
--
--   1. A pg_dump/restore into a new Supabase project (`--no-privileges`, the
--      documented way) drops every one of those statements, and the new
--      project's DEFAULT privileges then grant anon and authenticated ALL on
--      every table the restore creates. RLS was still forced everywhere, so
--      nothing was reachable — but the posture this document promises was
--      gone, and only a catalog query noticed.
--   2. Every future table has to remember its own revoke, and one day one
--      will not.
--
-- So this migration does three things, all idempotent, all set-based over
-- the catalog rather than a list that can fall behind:
--
--   - sweep every existing public table, sequence and function;
--   - set DEFAULT privileges for the role that creates our objects, so a
--     table added tomorrow starts life with the same posture;
--   - keep the three RLS helper functions from 0007 executable by
--     `authenticated`, because the policies call them.
--
-- `service_role` and `postgres` are untouched: the API connects as one of
-- them and enforces authorisation in code. The browser only ever reaches
-- Postgres through the publishable key, i.e. as `anon` or `authenticated`,
-- and this is what those two may do — read, under RLS, and nothing else.
-- ---------------------------------------------------------------------------

do $$
declare
    r record;
begin
    -- Tables: anon nothing; authenticated SELECT only.
    for r in
        select c.relname
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'public' and c.relkind in ('r', 'p', 'v', 'm')
    loop
        execute format('revoke all on table public.%I from anon, authenticated', r.relname);
        execute format('grant select on table public.%I to authenticated', r.relname);
    end loop;

    -- Sequences: neither role touches them (nothing in the browser inserts).
    for r in
        select c.relname
          from pg_class c
          join pg_namespace n on n.oid = c.relnamespace
         where n.nspname = 'public' and c.relkind = 'S'
    loop
        execute format('revoke all on sequence public.%I from anon, authenticated', r.relname);
    end loop;

    -- Functions: anon executes nothing. authenticated executes only the RLS
    -- helpers the policies depend on (declared in 0007).
    for r in
        select p.oid::regprocedure as sig, p.proname
          from pg_proc p
          join pg_namespace n on n.oid = p.pronamespace
         where n.nspname = 'public'
    loop
        execute format('revoke all on function %s from anon, authenticated, public', r.sig);
        if r.proname in ('auth_profile_role', 'auth_is_global_admin', 'auth_outlet_ids') then
            execute format('grant execute on function %s to authenticated', r.sig);
        end if;
    end loop;
end
$$;

-- From here on, objects created by the role running the migrations start
-- with this posture instead of the platform default. Only OUR role: on a
-- Supabase project the platform's own `supabase_admin` also has defaults for
-- `public`, but `postgres` is not permitted to alter them (tried, 5 Sep 2026)
-- and nothing of ours is ever created by that role.
alter default privileges in schema public revoke all on tables from anon, authenticated;
alter default privileges in schema public grant select on tables to authenticated;
alter default privileges in schema public revoke all on sequences from anon, authenticated;
alter default privileges in schema public revoke all on functions from anon, authenticated, public;

comment on schema public is
    'anon: no privileges. authenticated: SELECT only, under forced RLS. Writes go through the API. See 0021.';
