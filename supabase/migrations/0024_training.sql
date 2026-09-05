-- ---------------------------------------------------------------------------
-- 0024 — Training records: who has been walked through the app, and when
--
-- A new manager or staff member gets a tap-by-tap tour of the screens their
-- role uses the first time they appear (first login for managers, first PIN
-- identify for floor staff on the shared tablet). The tour's CONTENT lives in
-- the web app, because its steps point at buttons; what the database keeps is
-- the fact: this person, this track, this version of the content, in this
-- language, started here, reached step N, finished (or the owner let them
-- skip) at this time.
--
-- One row per attempt, never updated into a different attempt. When training
-- is restarted for somebody - the case this exists for: a manager or staff
-- member changes and the owner hands the same device to the new one - the
-- old rows are marked superseded and a fresh row starts, carrying who asked
-- for it. "Has this person been trained?" is then a query over rows that were
-- never edited. A completion stays valid across content versions (D31): only
-- a restart makes the tour required again.
--
-- Restarting is the owner's, delegable per manager: profiles gains a flag
-- the owner sets on the People page.
-- ---------------------------------------------------------------------------

create table training_records (
    id             uuid primary key default gen_random_uuid(),
    profile_id     uuid not null references profiles (id) on delete cascade,
    -- 'management' for owner/ops_manager/outlet_manager (the /app shell),
    -- 'floor' for shift_lead/staff (the /floor shell). Text with a check
    -- rather than an enum: adding a third shell should not need 0001 edited.
    track          text not null,
    -- The content version the person actually saw, e.g. 'management.v1'.
    version        text not null,
    -- 'en' or 'bn': the language they chose when the tour began.
    language       text,
    total_steps    integer not null,
    last_step      integer not null default 0,
    -- [{"step": 3, "at": "2026-09-06T10:15:00+05:30"}, ...] in the order
    -- reached. Enough to see where somebody stalled.
    steps          jsonb not null default '[]'::jsonb,
    -- Where it ran, when it ran on a shared tablet.
    device_id      uuid references outlet_devices (id) on delete set null,
    -- Null for a first-time run; whoever restarted it otherwise.
    triggered_by   uuid references profiles (id) on delete set null,
    started_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    completed_at   timestamptz,
    -- Only the owner may skip; everyone else finishes.
    skipped_at     timestamptz,
    -- Set when training was restarted; this attempt no longer counts.
    superseded_at  timestamptz,
    superseded_by  uuid references profiles (id) on delete set null,

    constraint training_records_track_known check (track in ('management', 'floor')),
    constraint training_records_language_known check (language is null or language in ('en', 'bn')),
    constraint training_records_steps_positive check (total_steps > 0),
    constraint training_records_step_in_range
        check (last_step >= 0 and last_step <= total_steps),
    constraint training_records_one_outcome
        check (num_nonnulls(completed_at, skipped_at) <= 1)
);

comment on table training_records is
    'One row per walkthrough attempt: who, which track, content version and language, how far, finished or skipped when, restarted by whom. Never edited into a new attempt.';

create index training_records_profile_idx
    on training_records (profile_id, track, started_at desc);

alter table training_records enable row level security;
alter table training_records force row level security;
revoke all on table training_records from anon;
grant select on table training_records to authenticated;

-- You can see your own; global admins see everyone's; an outlet manager sees
-- the people who work at their outlets - the same shape as profiles.
create policy training_records_read_self_or_colleagues on training_records
    for select to authenticated
    using (
        profile_id = auth.uid()
        or auth_is_global_admin()
        or exists (
            select 1
            from outlet_members om
            where om.profile_id = training_records.profile_id
              and om.deleted_at is null
              and om.outlet_id = any (auth_outlet_ids())
        )
    );

-- The delegation. Owner-set, per manager; meaningless (and ignored) on a
-- floor role. Owners restart anyone regardless.
alter table profiles
    add column can_restart_training boolean not null default false;

comment on column profiles.can_restart_training is
    'Owner-granted: this manager may restart training for people at their outlets (ops managers: everywhere). Owners may always.';
