-- ---------------------------------------------------------------------------
-- 0003 — Core: outlets, people, devices, audit
-- ---------------------------------------------------------------------------

create table outlets (
    id                 uuid primary key default gen_random_uuid(),
    code               text not null unique,          -- 'AKR-NT01'
    name               text not null,
    address_line       text,
    city               text,
    geo_lat            double precision,
    geo_lng            double precision,
    geofence_radius_m  integer not null default 150,
    timezone           text not null default 'Asia/Kolkata',
    opened_on          date,
    is_active          boolean not null default true,
    created_at         timestamptz not null default now(),
    updated_at         timestamptz,
    deleted_at         timestamptz,

    constraint outlets_geofence_positive check (geofence_radius_m > 0),
    constraint outlets_lat_range check (geo_lat is null or geo_lat between -90 and 90),
    constraint outlets_lng_range check (geo_lng is null or geo_lng between -180 and 180)
);

comment on column outlets.geofence_radius_m is
    'Submissions further than this from geo_lat/lng are flagged out_of_geofence. Never blocked.';


-- One row per human. id matches auth.users.id; Supabase Auth owns identity and
-- this table owns everything else about the person.
create table profiles (
    id             uuid primary key,
    full_name      text not null,
    phone          text,
    employee_code  text,
    global_role    user_role not null default 'staff',

    -- Floor staff share an outlet tablet rather than carrying phones, so they
    -- identify with a PIN to attribute a run to a real person. Argon2 hash of
    -- the PIN, never the PIN. A PIN authorises floor actions only: it can never
    -- approve a run and can never reach the management shell.
    pin_hash            text,
    pin_set_at          timestamptz,
    pin_failed_attempts integer not null default 0,
    pin_locked_until    timestamptz,

    is_active      boolean not null default false,
    last_seen_at   timestamptz,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz,
    deleted_at     timestamptz
);

comment on column profiles.is_active is
    'Defaults false. A self-signup gets a profile but no access until an admin activates it.';

-- The auth.users foreign key only exists on Supabase. Local and CI databases
-- have no auth schema, so add it conditionally rather than forking the file.
do $$
begin
    if exists (select 1 from information_schema.tables
               where table_schema = 'auth' and table_name = 'users') then
        alter table profiles
            add constraint profiles_id_fkey
            foreign key (id) references auth.users (id) on delete cascade;
    end if;
end
$$;


-- A person can serve more than one outlet, with a different role at each.
create table outlet_members (
    id              uuid primary key default gen_random_uuid(),
    outlet_id       uuid not null references outlets (id) on delete cascade,
    profile_id      uuid not null references profiles (id) on delete cascade,
    role_at_outlet  user_role not null,
    is_primary      boolean not null default false,
    created_at      timestamptz not null default now(),
    updated_at      timestamptz,
    deleted_at      timestamptz,

    unique (outlet_id, profile_id)
);


-- The shared tablet itself. It holds one Supabase session bound to one outlet;
-- individual staff then identify with a PIN. Keeping the device as its own row
-- means a lost tablet is revoked without touching any person's account.
create table outlet_devices (
    id             uuid primary key default gen_random_uuid(),
    outlet_id      uuid not null references outlets (id) on delete cascade,
    auth_user_id   uuid not null unique,   -- the device's own Supabase auth user
    label          text not null,          -- 'New Town — kitchen pass tablet'
    is_active      boolean not null default true,
    last_seen_at   timestamptz,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz,
    deleted_at     timestamptz
);

comment on table outlet_devices is
    'Shared floor tablets. A device session authenticates the request; a staff PIN attributes the action.';


create table audit_log (
    id                uuid primary key default gen_random_uuid(),
    actor_profile_id  uuid references profiles (id) on delete set null,
    outlet_id         uuid references outlets (id) on delete set null,
    entity_table      text not null,
    entity_id         uuid,
    action            audit_action not null,
    before            jsonb,
    after             jsonb,
    ip                inet,
    user_agent        text,
    at                timestamptz not null default now()
);

comment on table audit_log is
    'Every mutating service call writes here. No exceptions for small edits: a template quietly edited to drop a step is exactly what you need to reconstruct.';


create trigger outlets_set_updated_at
    before update on outlets
    for each row execute function set_updated_at();

create trigger profiles_set_updated_at
    before update on profiles
    for each row execute function set_updated_at();

create trigger outlet_members_set_updated_at
    before update on outlet_members
    for each row execute function set_updated_at();

create trigger outlet_devices_set_updated_at
    before update on outlet_devices
    for each row execute function set_updated_at();
