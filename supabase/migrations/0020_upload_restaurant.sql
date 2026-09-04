-- ---------------------------------------------------------------------------
-- 0020 — What restaurant an export said it was
--
-- Every Petpooja report carries a "Restaurant Name:" line in its preamble.
-- Until now all three adapters read it and threw it away, which meant an
-- export from a different venue — someone's other restaurant, a file sent to
-- the wrong group — would ingest silently into whichever outlet the uploader
-- picked. Nothing about the resulting rows would look wrong afterwards.
--
-- The check itself lives in code, against the `sales.petpooja_restaurant_name`
-- setting (per-outlet overridable, empty by default). This column is the
-- evidence trail beside it: what the file CLAIMED, kept whether or not the
-- guard was armed when it arrived, so that
--
--   select distinct restaurant_name from data_uploads
--
-- answers "has anything foreign ever been ingested here" for the whole history
-- rather than only for uploads that arrived after the setting was configured.
-- It is also where an admin reads the exact string to put in that setting.
--
-- Null means either the file had no such preamble line or it predates this
-- migration. Backfilling is a re-parse away — the original bytes are kept in
-- Storage precisely for that — and deliberately not done here: a migration
-- that silently downloads and re-reads every stored export is a migration
-- nobody can predict the runtime of.
-- ---------------------------------------------------------------------------

alter table data_uploads
    add column restaurant_name text;

comment on column data_uploads.restaurant_name is
    'The "Restaurant Name:" preamble from the export, verbatim. What the file claimed, not what was expected — the expectation is the sales.petpooja_restaurant_name setting.';
