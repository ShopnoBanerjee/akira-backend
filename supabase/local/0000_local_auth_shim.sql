-- ---------------------------------------------------------------------------
-- LOCAL AND CI ONLY — never applied to Supabase.
--
-- Supabase provides the auth schema, auth.uid(), and the anon / authenticated
-- roles. A bare Postgres does not. This shim creates just enough of them that
-- the real RLS migration compiles and can be exercised locally.
--
-- It is deliberately kept out of supabase/migrations/ so it can never run
-- against the hosted project and clobber Supabase's own auth.uid().
-- ---------------------------------------------------------------------------

create schema if not exists auth;

do $$
begin
    if not exists (select 1 from pg_roles where rolname = 'anon') then
        create role anon nologin;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'authenticated') then
        create role authenticated nologin;
    end if;
    if not exists (select 1 from pg_roles where rolname = 'service_role') then
        create role service_role nologin bypassrls;
    end if;
end
$$;

-- Supabase reads the subject claim from the request JWT. Locally we read a
-- session variable instead, so a test can say "act as this user".
create or replace function auth.uid()
returns uuid
language sql
stable
as $$
    select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid
$$;

-- Stand-in for Supabase's auth.users, so the conditional foreign key in 0003
-- has something to point at when the shim is applied before the migrations.
create table if not exists auth.users (
    id    uuid primary key,
    email text
);

grant usage on schema auth to anon, authenticated, service_role;
grant usage on schema public to anon, authenticated, service_role;
