-- ---------------------------------------------------------------------------
-- 0014 — What ingesting a real Petpooja export needs
--
-- 0005 built the tables. Parsing an actual file exposed two things it could
-- not record.
--
-- 1. WHICH ADAPTER READ IT. The spec's risk table says a Petpooja format
--    change should be a new adapter rather than a rewrite. That only helps if
--    a row remembers which adapter produced it — otherwise re-parsing a
--    six-week backlog under v2 is indistinguishable from the v1 read, and
--    there is no way to tell which numbers were revised.
--
-- 2. WHEN THE PARSE ACTUALLY FINISHED. status alone cannot separate "still
--    parsing" from "started parsing an hour ago and the worker died", which is
--    the same distinction job_runs exists to make everywhere else.
-- ---------------------------------------------------------------------------

alter table data_uploads
    add column adapter_version text,
    add column parsed_at timestamptz,
    -- What the file itself claimed its totals were, in paise. Kept beside the
    -- rows we derived so a disagreement between the export's own summary and
    -- our sum is visible rather than something you would have to go and
    -- recompute by hand.
    add column reported_net_paise bigint,
    add column parsed_net_paise bigint;

comment on column data_uploads.adapter_version is
    'Which versioned parser read this file. A re-parse under a newer adapter is then distinguishable from the original read.';
comment on column data_uploads.reported_net_paise is
    'The Total row inside the export. Compared against parsed_net_paise so a mismatch surfaces instead of being discovered months later.';

-- No new indexes. 0008 already covers both query patterns this needed:
-- sales_orders (outlet_id, business_date) for the table view and the dashboard
-- rollups, and data_uploads (outlet_id, created_at desc) for the upload list.
-- Checked, rather than added again.
