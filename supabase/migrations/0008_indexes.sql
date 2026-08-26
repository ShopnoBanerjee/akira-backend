-- ---------------------------------------------------------------------------
-- 0008 — Indexes
--
-- Each one is here because a named query needs it, noted alongside. Indexes
-- without a query behind them are a cost with no benefit.
-- ---------------------------------------------------------------------------

-- The floor list: today's runs for one outlet. Also every dashboard rollup,
-- all of which group by business_date and never by created_at.
create index checklist_runs_outlet_business_date_idx
    on checklist_runs (outlet_id, business_date);

-- The mark_missed job, every 15 minutes: runs still open past due_at + grace.
-- Partial, because the finished states are the overwhelming majority.
create index checklist_runs_open_due_idx
    on checklist_runs (status, due_at)
    where status in ('pending', 'in_progress');

-- The manager review queue: submitted runs, oldest first.
create index checklist_runs_review_queue_idx
    on checklist_runs (outlet_id, status, submitted_at)
    where status = 'submitted';

create index checklist_run_items_run_idx
    on checklist_run_items (run_id);

-- Duplicate photo detection compares against the last 30 days for the same
-- outlet and item, so the hash lookup is only ever over rows that have one.
create index checklist_run_items_phash_idx
    on checklist_run_items (photo_phash)
    where photo_phash is not null;

create index checklist_run_items_template_item_idx
    on checklist_run_items (template_item_id);

create index run_item_ai_reviews_run_item_idx
    on run_item_ai_reviews (run_item_id, reviewed_at desc);

-- The exception board, and the "open high-severity older than 48h" penalty in
-- the outlet score.
create index sop_exceptions_outlet_status_date_idx
    on sop_exceptions (outlet_id, status, business_date);

create index sop_exceptions_open_severity_idx
    on sop_exceptions (severity, created_at)
    where status in ('open', 'acknowledged');

-- Materialisation walks active assignments per outlet each morning.
create index checklist_assignments_outlet_active_idx
    on checklist_assignments (outlet_id, is_active)
    where deleted_at is null;

create index checklist_template_items_template_sort_idx
    on checklist_template_items (template_id, sort_order)
    where deleted_at is null;

-- Reference photo lookup during AI review: one item, one outlet.
create index outlet_item_reference_photos_lookup_idx
    on outlet_item_reference_photos (template_item_id, outlet_id)
    where is_active and deleted_at is null;

-- Sales reporting, all business_date based.
create index sales_orders_outlet_business_date_idx
    on sales_orders (outlet_id, business_date);

create index sales_orders_business_date_idx
    on sales_orders (business_date);

create index sales_order_items_outlet_date_item_idx
    on sales_order_items (outlet_id, business_date, item_name);

create index sales_order_items_order_idx
    on sales_order_items (order_id);

create index data_uploads_outlet_created_idx
    on data_uploads (outlet_id, created_at desc);

-- Audit lookups go two ways: "what happened to this row" and "what did this
-- person do".
create index audit_log_entity_idx
    on audit_log (entity_table, entity_id);

create index audit_log_actor_idx
    on audit_log (actor_profile_id, at desc);

-- The jobs settings screen shows the last 50 runs.
create index job_runs_name_started_idx
    on job_runs (job_name, started_at desc);

-- Membership lookup drives auth_outlet_ids() on every RLS check.
create index outlet_members_profile_idx
    on outlet_members (profile_id)
    where deleted_at is null;
