-- ---------------------------------------------------------------------------
-- 0012 — Review depth tracking
--
-- The spec's risk table names "manager approves everything unread" as a known
-- failure mode. This records which photos an approver actually opened, feeding
-- the owner digest's "approved without looking" signal (P7). It is an
-- owner-level check, deliberately not surfaced as a punishment metric in the
-- manager UI.
-- ---------------------------------------------------------------------------

create table run_review_views (
    id           uuid primary key default gen_random_uuid(),
    run_id       uuid not null references checklist_runs (id) on delete cascade,
    run_item_id  uuid not null references checklist_run_items (id) on delete cascade,
    reviewer_id  uuid not null references profiles (id) on delete cascade,
    viewed_at    timestamptz not null default now(),

    -- One row per reviewer per photo; repeat opens refresh the timestamp.
    unique (run_item_id, reviewer_id)
);

create index run_review_views_run_idx on run_review_views (run_id, reviewer_id);

alter table run_review_views enable row level security;
alter table run_review_views force row level security;
revoke all on table run_review_views from anon;
grant select on table run_review_views to authenticated;

create policy run_review_views_read_own_outlet on run_review_views
    for select to authenticated
    using (
        exists (
            select 1 from checklist_runs r
             where r.id = run_review_views.run_id
               and (auth_is_global_admin() or r.outlet_id = any (auth_outlet_ids()))
        )
    );
