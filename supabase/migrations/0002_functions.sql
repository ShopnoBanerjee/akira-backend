-- ---------------------------------------------------------------------------
-- 0002 — Shared functions
--
-- business_date() is the single SQL expression of the 05:00 rollover. Its only
-- counterpart is app/core/business_date.py, and the two are tested against each
-- other. Change neither without changing both.
-- ---------------------------------------------------------------------------

-- AKIRA trades past midnight. A trading night that starts 18:00 Saturday and
-- ends 01:30 Sunday is ONE business day. Subtracting five hours from the local
-- wall clock puts everything before 05:00 onto the previous calendar date.
--
--   2026-08-23 01:30 IST -> 2026-08-22   (still Saturday's service)
--   2026-08-23 06:00 IST -> 2026-08-23   (Sunday proper)
--
-- Never group or filter a report by created_at::date. Doing so silently splits
-- every weekend night across two days.
create or replace function business_date(ts timestamptz)
returns date
language sql
immutable
parallel safe
as $$
    select ((ts at time zone 'Asia/Kolkata') - interval '5 hours')::date
$$;

comment on function business_date(timestamptz) is
    'Trading date for a timestamp, rolling over at 05:00 Asia/Kolkata. Mirrored in app/core/business_date.py.';


-- Keeps updated_at honest without every service remembering to set it.
create or replace function set_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;
