-- ---------------------------------------------------------------------------
-- 0001 — Extensions and enums
--
-- Closed sets are Postgres enums so a value cannot drift. Adding a value later
-- means a new migration plus app/core/enums.py plus a fresh openapi.json.
-- ---------------------------------------------------------------------------

create extension if not exists pgcrypto;

-- Who a person is, globally and per outlet.
create type user_role as enum (
    'owner',
    'ops_manager',
    'outlet_manager',
    'shift_lead',
    'staff'
);

create type run_status as enum (
    'pending',
    'in_progress',
    'submitted',
    'approved',
    'rejected',
    'missed'
);

create type item_result as enum ('pass', 'fail', 'na', 'pending');

create type value_type as enum ('number', 'text', 'temperature_c', 'time');

-- Extended beyond the Stage 1 spec. AKIRA's real checklists run on cadences the
-- spec's four values cannot express: the kitchen cleaning list has alternate-day
-- tasks, and service housekeeping has fortnightly ones. Neither aligns to a
-- weekly cycle, so active_weekdays alone cannot represent them.
create type frequency as enum (
    'per_shift',
    'daily',
    'alternate_day',
    'weekly',
    'fortnightly',
    'monthly'
);

create type day_part as enum ('opening', 'mid', 'closing', 'any');

create type exception_status as enum ('open', 'acknowledged', 'resolved', 'waived');

create type severity as enum ('high', 'medium', 'low');

create type sales_channel as enum ('dine_in', 'pickup', 'delivery');

create type upload_status as enum ('received', 'parsing', 'parsed', 'failed');

create type audit_action as enum (
    'create',
    'update',
    'delete',
    'approve',
    'reject',
    'login'
);

-- The AI photo reviewer is advisory only. It never blocks a submission and
-- never approves a run; a manager still decides. 'uncertain' is a first-class
-- outcome so the model is not forced into a false binary.
create type ai_verdict as enum ('pass', 'fail', 'uncertain');

create type job_status as enum ('running', 'succeeded', 'failed');
