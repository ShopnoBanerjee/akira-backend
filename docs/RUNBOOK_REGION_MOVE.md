# Runbook: moving the database from Sydney to Mumbai

Why: the Supabase project is in `ap-southeast-2`. One round trip from Kolkata
is 310 ms; from `ap-south-1` it is 46 ms. After P20 most endpoints are one
round trip, so the region is now the whole of the latency (D26). Supabase does
not move a project between regions; the move is a new project and a copy.

The database is 19 MB, 36 tables, 11 auth users, ~490 objects (12 MB) across
three private buckets. The whole thing copies in minutes. What takes care is
the order, so that nothing is written to the old project after the copy and
nothing points at the new one before it is verified.

This was rehearsed end to end against a local Postgres 18 on 5 Sep 2026,
twice. The first run restored `public` before the auth users, and every one
of the 36 tables matched Sydney's row counts — while the foreign key from
`profiles.id` to `auth.users.id` had silently failed to be re-created,
because at the moment pg_restore added it there were no auth users for it to
point at. Counts alone would have passed that. **Auth users go in first**;
the order below is the one that works, and step 5 checks the constraint by
name. The one error the public restore prints — `schema "public" already
exists` — is expected on any target that already has a public schema, which
is every Supabase project.

## 0. Before starting — the owner creates the project

In the Supabase dashboard: **New project → Region: South Asia (Mumbai)**.
Same organisation. Set a database password and keep it. Then collect from
*Project Settings*:

- Project URL (`https://<ref>.supabase.co`) and the project ref
- Publishable key and secret key (API → Keys)
- The direct database host (`db.<ref>.supabase.co`) — **not** the pooler

Nothing else is configured in the dashboard. Auth settings (email provider,
redirect URLs) must be copied by hand — check *Authentication → URL
Configuration* against the Sydney project before the switch.

## 1. Freeze writes to Sydney

Stop the API process and tell anyone with a tablet to pause. The scheduler
runs inside the API, so stopping it stops the nightly jobs too. From here to
step 6 the system is down; it should be under an hour.

## 2. Dump Sydney

```bash
cd akira-backend
set -a; . ./.env; set +a
export PGPASSWORD="$SUPABASE_DB_PASSWORD"
PGBIN="/c/Program Files/PostgreSQL/18/bin"
STAMP=$(date +%Y%m%d_%H%M%S)
mkdir -p local/backups            # gitignored; never commit a dump

"$PGBIN/pg_dump.exe" -h "$SUPABASE_DB_HOST" -p 5432 -U postgres -d postgres \
  --no-owner --no-privileges --quote-all-identifiers \
  --schema=public -Fc -f "local/backups/public_${STAMP}.dump"

"$PGBIN/pg_dump.exe" -h "$SUPABASE_DB_HOST" -p 5432 -U postgres -d postgres \
  --no-owner --no-privileges --quote-all-identifiers --data-only --column-inserts \
  --table=auth.users --table=auth.identities \
  -f "local/backups/auth_users_${STAMP}.sql"
```

Two dumps on purpose. `public` is ours end to end and goes as a custom-format
archive. Auth users are GoTrue's tables and go as plain INSERTs of the two
tables that matter, so the same UUIDs — which `profiles.id` and every audit
row reference — land in the new project with their password hashes intact.
Nobody has to be re-invited.

## 3. Restore into Mumbai — auth users FIRST

```bash
export NEW_HOST="db.<newref>.supabase.co"
export PGPASSWORD="<new db password>"

# 3a. auth users and identities, as plain inserts into GoTrue's own tables.
#     Must come first: profiles.id references auth.users.id, and pg_restore
#     re-creates that foreign key at the end of the public restore.
"$PGBIN/psql.exe" -h "$NEW_HOST" -p 5432 -U postgres -d postgres \
  -v ON_ERROR_STOP=1 -f "local/backups/auth_users_${STAMP}.sql"

# 3b. public schema + data. The one expected error is the public-schema clash.
"$PGBIN/pg_restore.exe" -h "$NEW_HOST" -p 5432 -U postgres -d postgres \
  --no-owner --no-privileges "local/backups/public_${STAMP}.dump" 2>&1 \
  | grep -v 'schema "public" already exists'
```

If 3a fails on a column GoTrue no longer has (its schema moves between
Supabase releases), edit the INSERT column list rather than dropping the
file: the UUIDs and `encrypted_password` are what must survive.

The rehearsal on a bare local Postgres also printed `function auth.uid()
does not exist` for two RLS policies. That is the local cluster lacking
Supabase's `auth` helper functions; every Supabase project has them and the
policies will create cleanly. If that error appears against Mumbai, stop —
it means the target is not a Supabase project.

## 4. Copy the files

```bash
SOURCE_URL=https://<oldref>.supabase.co SOURCE_KEY=<old secret key> \
TARGET_URL=https://<newref>.supabase.co TARGET_KEY=<new secret key> \
uv run python scripts/copy_storage.py --dry-run     # then without --dry-run
```

Idempotent; re-run until it reports `failed 0`.

## 5. Verify — do not skip

```bash
SOURCE_DSN="postgresql://postgres:<old pw>@$SUPABASE_DB_HOST:5432/postgres" \
TARGET_DSN="postgresql://postgres:<new pw>@$NEW_HOST:5432/postgres" \
uv run python scripts/verify_migration.py
```

It must print `RESULT: IDENTICAL`. Then, against the new project only:

```bash
"$PGBIN/psql.exe" -h "$NEW_HOST" -U postgres -d postgres -Atc \
  "select count(*) from pg_class c join pg_namespace n on n.oid=c.relnamespace
    where n.nspname='public' and c.relkind='r' and not c.relforcerowsecurity;"
# must be 0 — every table has RLS forced

"$PGBIN/psql.exe" -h "$NEW_HOST" -U postgres -d postgres -Atc \
  "select count(*) from pg_constraint where conname='profiles_id_fkey';"
# must be 1 — the constraint the first rehearsal silently lost
```

## 6. Repoint

`akira-backend/.env`: `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`,
`SUPABASE_SECRET_KEY`, `SUPABASE_JWKS_URL`, `DATABASE_URL`, `SUPABASE_DB_HOST`,
`SUPABASE_DB_PASSWORD`. `akira-frontend/.env.local`: `VITE_SUPABASE_URL`,
`VITE_SUPABASE_PUBLISHABLE_KEY`. CI carries no Supabase secrets (it builds a
throwaway database), so nothing changes there.

Start the API. `/readyz` must say `database: ok`. Sign in as the owner in the
browser — the same password works, because the hash moved. Open the
dashboard, one review with photos (signed URLs now come from the new bucket),
and the sales page. Re-send one already-ingested export: it must answer
`already_ingested: true`, which proves `file_sha256` and the guard survived.

## 7. Measure, then decide about the API's own location

```bash
PYTHONPATH="$PWD" uv run python <scratchpad>/latency_audit.py
```

Expect single-statement endpoints at ~50–60 ms from Kolkata instead of 315.
That is with the API still on a laptop in Kolkata. Deploying it beside the
database (a container on Cloud Run `asia-south1`, or any host in
`ap-south-1`) turns every database call into a LAN call and leaves the browser
one 46 ms hop; that is a separate change and needs a hosting account.

## 8. Afterwards

- Keep the Sydney project paused, not deleted, for two weeks. Then delete it.
- Rotate the Groq key at the same time (OPEN_ITEMS) — it is one `.env` edit
  in the same sitting.
- Delete `local/backups/*` once the Sydney project is gone; until then they
  are the rollback.
- Update `docs/HANDOFF.md` section 3 (environment) with the new ref, and
  `OPEN_ITEMS.md`: remove "The database is in Sydney".

## Rollback

Until step 6 nothing has changed for users. After it, rollback is the old
`.env` values and a restart; Sydney was frozen, not modified, so it is exactly
as it was. Any writes made to Mumbai in between are lost — which is why
step 5 is not optional and step 6 is done once, quickly, and verified.
