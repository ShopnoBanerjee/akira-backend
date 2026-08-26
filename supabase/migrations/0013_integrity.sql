-- ---------------------------------------------------------------------------
-- 0013 — What the integrity engine needs to say WHY
--
-- 0004 gave every run item an integrity_flags array. Running the checks for
-- real exposed two gaps in it.
--
-- 1. A flag with no evidence is an accusation. "duplicate_photo" on its own
--    tells a manager to distrust a photo without telling them what it matched,
--    which is exactly the kind of unfalsifiable red chip staff learn to ignore.
--    integrity_detail carries the evidence: which run the hash matched, the
--    Hamming distance, the measured luminance, the burst share.
--
-- 2. Three of the six checks are properties of the RUN, not of a photo. Late,
--    off-site and batch-faking describe the whole submission. Stamping them on
--    to each photo item would be a lie about where the evidence lives and
--    would inflate any per-photo count built on top of it.
--
-- integrity_flag_count on checklist_runs keeps its meaning: run-level flags
-- plus every item-level flag. That total is what the outlet score penalises.
-- ---------------------------------------------------------------------------

alter table checklist_runs
    add column integrity_flags  text[] not null default '{}',
    add column integrity_detail jsonb  not null default '{}'::jsonb;

comment on column checklist_runs.integrity_flags is
    'Run-level flags: late, out_of_geofence, burst_upload. Advisory — never blocks a submission.';
comment on column checklist_runs.integrity_detail is
    'Evidence per run-level flag, keyed by flag name. A flag without evidence is an accusation.';


alter table checklist_run_items
    add column integrity_detail jsonb not null default '{}'::jsonb,
    -- Photo hashing and luminance run in a background task, so "no flags" and
    -- "not looked at yet" are different states and must be distinguishable.
    -- A review screen that cannot tell them apart shows a clean bill of health
    -- for a photo nothing has examined.
    add column photo_processed_at timestamptz;

comment on column checklist_run_items.integrity_detail is
    'Evidence per item-level flag, keyed by flag name: the matched run for a duplicate, the measured luminance for a dark photo.';
comment on column checklist_run_items.photo_processed_at is
    'When the background integrity pass finished for this photo. Null means it has not run yet, which is not the same as clean.';


-- The duplicate lookback: same outlet, same template item, last N days, rows
-- that actually have a hash. template_item_id alone already has an index, but
-- it includes every photoless row, and most run items are photoless.
create index checklist_run_items_dup_lookback_idx
    on checklist_run_items (template_item_id, photo_uploaded_at desc)
    where photo_phash is not null;

-- The photo-integrity background pass claims work by run item.
create index job_runs_outlet_business_date_idx
    on job_runs (outlet_id, business_date, job_name);
