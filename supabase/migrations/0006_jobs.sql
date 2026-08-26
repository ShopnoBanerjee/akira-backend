-- ---------------------------------------------------------------------------
-- 0006 — Scheduled and background job history
--
-- Every scheduled task and every background photo/parse job records here. The
-- point is that a missed 05:00 materialisation is visible rather than silent:
-- a job that never ran leaves a gap you can see, and one that failed leaves an
-- error you can read.
-- ---------------------------------------------------------------------------

create table job_runs (
    id            uuid primary key default gen_random_uuid(),
    job_name      text not null,          -- 'materialise_runs' | 'mark_missed' | ...
    status        job_status not null default 'running',

    -- Null for network-wide jobs; set for per-outlet work.
    outlet_id     uuid references outlets (id) on delete cascade,

    -- The business date the job was acting on, not the date it ran. A 05:00
    -- materialisation on the 23rd is materialising the 23rd.
    business_date date,

    started_at    timestamptz not null default now(),
    finished_at   timestamptz,
    duration_ms   integer,

    -- Whatever the job wants to report: rows created, photos hashed, files
    -- parsed. Kept loose on purpose, since each job counts different things.
    detail        jsonb not null default '{}'::jsonb,
    error_detail  text,

    -- Set when a person pressed "run now" rather than the scheduler firing.
    triggered_by  uuid references profiles (id) on delete set null,

    created_at    timestamptz not null default now(),

    constraint job_runs_finished_has_status
        check (status = 'running' or finished_at is not null),
    constraint job_runs_failed_has_error
        check (status <> 'failed' or error_detail is not null)
);

comment on table job_runs is
    'Execution history for scheduled and background jobs, so a silent failure becomes a visible one.';
