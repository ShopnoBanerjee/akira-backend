-- ---------------------------------------------------------------------------
-- 0018 — Forecasting (Stage 2, P16)
--
-- Spec 5.1's baseline, and the schema for everything that may replace it:
--
--     forecast(outlet, date) = median(same weekday, last 4 business dates)
--                            x trend_factor(last 14d vs prior 14d, clamped)
--                            x event_multiplier(manual flag)
--
-- Two design rules, both about honesty over time:
--
-- 1. **A forecast is only a forecast if it was made in advance.** Rows are
--    written by the nightly job BEFORE the day trades and never updated —
--    (outlet, target_date, made_on, model) is unique, so re-forecasting a
--    nearer day writes a new row rather than rewriting history. MAPE is
--    computed against what was actually predicted, at the horizon it was
--    predicted at. A recomputed-after-the-fact forecast is a lie with a
--    timestamp, and this table cannot store one.
--
-- 2. **The `model` column is the graduation path.** The spec gates any
--    learned model on 12+ weeks of baseline error history and a genuine
--    win. When a challenger exists, the same job writes its rows beside the
--    baseline's under its own model id, and champion-vs-challenger MAPE is
--    a GROUP BY — not a refactor. See docs/DECISIONS.md D23.
--
-- forecast_events is the event_multiplier's source: a manager writing down
-- "Durga Puja weekend, expect 1.3x" before it happens. Outlet-scoped or
-- group-wide (outlet_id null). The multiplier is bounded in the API, not
-- here — the registry owns limits (D9), the table stores history.
-- ---------------------------------------------------------------------------

create table forecast_events (
    id           uuid primary key default gen_random_uuid(),
    -- Null means every outlet: a public holiday is nobody's override.
    outlet_id    uuid references outlets (id) on delete cascade,
    event_date   date not null,
    multiplier   numeric not null,
    label        text not null,
    created_by   uuid references profiles (id) on delete set null,
    created_at   timestamptz not null default now(),

    constraint forecast_events_multiplier_sane
        check (multiplier > 0 and multiplier <= 5)
);

create index forecast_events_date_idx on forecast_events (event_date);

create table sales_forecasts (
    id                  uuid primary key default gen_random_uuid(),
    outlet_id           uuid not null references outlets (id) on delete cascade,
    target_date         date not null,
    -- The business date the forecast was made on; horizon = target - made_on.
    made_on             date not null,
    model               text not null,
    forecast_net_paise  bigint not null,
    -- Null when the covers history is too thin to say — most of it is.
    forecast_covers     integer,
    -- The working: median, trend factor, event multiplier, sample dates.
    components          jsonb not null default '{}'::jsonb,
    created_at          timestamptz not null default now(),

    unique (outlet_id, target_date, made_on, model),
    constraint sales_forecasts_made_in_advance check (made_on <= target_date)
);

create index sales_forecasts_outlet_target_idx
    on sales_forecasts (outlet_id, target_date desc);

alter table forecast_events enable row level security;
alter table forecast_events force row level security;
revoke all on table forecast_events from anon;
grant select on table forecast_events to authenticated;
create policy forecast_events_read on forecast_events
    for select to authenticated
    using (
        auth_is_global_admin()
        or outlet_id is null
        or outlet_id = any (auth_outlet_ids())
    );

alter table sales_forecasts enable row level security;
alter table sales_forecasts force row level security;
revoke all on table sales_forecasts from anon;
grant select on table sales_forecasts to authenticated;
create policy sales_forecasts_read_own_outlet on sales_forecasts
    for select to authenticated
    using (auth_is_global_admin() or outlet_id = any (auth_outlet_ids()));
