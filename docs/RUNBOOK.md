# Runbook

How to run, watch, and un-break this system. Written for whoever is on the
other end of "the dashboard looks wrong" at 09:15 — start at the symptom, not
at the architecture. `docs/HANDOFF.md` is the cold-start manual; this is the
warm-emergency one.

---

## Run it

```bash
uv sync                          # install
uv run uvicorn app.main:app --reload --port 8000
uv run pytest -q                 # needs TEST_DATABASE_URL at a LOCAL cluster
uv run ruff check . && uv run mypy app/
```

`.env` is gitignored and holds everything: `DATABASE_URL` (direct Postgres,
service role), `SUPABASE_URL`, `SUPABASE_SECRET_KEY`, `SUPABASE_PUBLISHABLE_KEY`,
`SUPABASE_JWKS_URL`, `ACTOR_TOKEN_SECRET`, `PHONE_HASH_SALT`, and the AI pair
(`AI_REVIEW_PROVIDER` + its key). Missing pieces fail loudly at startup or at
first use — nothing silently degrades except the two things designed to
(digest mail and AI review, below).

Seed scripts, in order, on an empty project: migrations (`supabase/migrations`
in filename order), `scripts/seed_users.py` (auth accounts + PINs →
`.seed-credentials.md`), `scripts/seed_history.py` (eight weeks of coherent
demo history — destructive on the dates it owns; read its docstring first).

---

## The scheduled jobs

In-process APScheduler, IST, enabled by `SCHEDULER_ENABLED`. Times come from
the settings registry (`jobs.*` keys), re-read by a reconciler every few
minutes — change the setting, no restart. Every firing writes a `job_runs`
row; the UI for it is `/app/settings/jobs`.

| Job | When | What | Left alone when broken? |
|---|---|---|---|
| `materialise_runs` | 05:00 daily (the business-date rollover) | Creates the day's pending runs from assignments | Missed fire replays within the grace window on restart |
| `mark_missed` | every 15 min | Overdue pending runs → `missed` + a medium exception | Idempotent; next tick catches up |
| `daily_digest` | 09:00 daily | Per-outlet email digest | 30-min misfire grace — a digest four hours late is worse than none |
| `stock_anomalies` | 05:45 daily | Consumption windows from confirmed counts; section-6 anomaly checks onto the exception board | Idempotent; derived data, so running late loses nothing |

**First diagnostic for anything time-related:** `GET /jobs/runs?limit=50` (or
the settings/jobs screen). Every background task brackets itself there —
started, finished, duration, detail, error. A job that "didn't run" either has
a row saying why it failed, or has no row, which means the scheduler itself is
down — check `/jobs/status` and `SCHEDULER_ENABLED`.

---

## Symptom → cause

**"Today's checklists are missing."**
05:00 materialise didn't fire or failed. `GET /jobs/runs?job=materialise_runs`.
Manual recovery is one call:
`POST /jobs/materialise_runs/run` (owner only) or
`POST /sop/runs/materialise {"business_date": "YYYY-MM-DD"}` — both idempotent,
neither will duplicate what exists.

**"A run everyone forgot is still pending at noon."**
`mark_missed` sweeps every 15 minutes; if the row shows it running and the run
still isn't flipped, check the run has a `due_at` — runs materialised by hand
without one are invisible to the sweep by design.

**"No digest arrived."**
Expected until SMTP is configured: `job_runs` will show `smtp_not_configured`
on every digest row (D12.6 — degrading loudly was chosen over a digest that
quietly stops). If SMTP *is* set, the row's detail says which outlet's send
failed and why; one outlet failing never stops the others.

**"Photos say 'not checked yet' on the review screen."**
The photo pass runs in the background after photo-confirm. Its failures land in
`job_runs` (`photo_integrity`). A photo that can never decode stays honestly
unprocessed — that is the rendering for corrupt bytes, not a bug to chase.

**"AI review is missing on a photo."**
Advisory by design — it never blocks. No key configured → recorded as a skip
with a reason, not a failure. Check `AI_REVIEW_PROVIDER` matches the key that
is actually set (`anthropic` ↔ `ANTHROPIC_API_KEY`, `gemini` ↔ `GEMINI_API_KEY`,
`openai` ↔ `OPENAI_COMPAT_API_KEY`, or `GEMINI_API_KEY` when the base URL is
Gemini's). `uv run python scripts/check_provider_keys.py` says which resolve.

**"Everything is slow."**
Almost always the wire, not the database — the database answers in ~0.2 ms
(D16). From a laptop, ~150 ms × round trips is the whole story; deployed
beside the database that multiplier is ~1 ms. If an endpoint regressed, count
its statements before touching any SQL: the fixed budget is 1 auth query + 1–2
statements for lists, and the engine deliberately runs autocommit for GETs
with no `pool_pre_ping` — do not "fix" a timeout by turning the ping back on;
`pool_recycle=240` is what replaces it.

**"An outlet's score looks wrong."**
Read `/dashboard/outlet-health` before doubting the number — it shows every
component, weight, penalty and the worst component by name. The counting rules
that look like bugs and are not: nothing scheduled → `null`, not 0; a
component with no denominator contributes 0 and is not re-weighted; one open
critical caps the *band* at amber and leaves the number alone; weights are the
ones in force at the period's END (D9).

**"The tablet won't take a PIN."**
Five wrong attempts locks that person for 5 minutes; the tablet shows the same
message for every failure mode on purpose. The audit log
(`action=login`, `pin_identify=failed`) says which mode it actually was.

**"Someone was deactivated but still has the app open."**
Their next API call fails: the profile is loaded per request. RLS cuts direct
reads the same moment. Nothing to do.

---

## Deliberate degradations (do not "fix" these)

- Digest without SMTP logs and records `smtp_not_configured` — visible on the
  jobs screen every morning until credentials exist.
- AI review without a key records a skip. Advisory means advisory.
- A corrupt photo stays unprocessed forever and renders as "not checked".
- `/dashboard` shows three pillars greyed. That is D14, not missing data.

## Recovery levers, in one place

| Lever | How | Safe because |
|---|---|---|
| Re-materialise a date | `POST /sop/runs/materialise` | idempotent, conflict-skipping |
| Re-parse a sales file | `POST /sales/uploads/{id}/reparse` | the original file is retained in Storage; upsert updates, never duplicates |
| Re-run a digest | `POST /jobs/daily_digest/run` (owner only) | reads only; sends only what it builds |
| Rebuild demo data | `scripts/seed_history.py` | deterministic; wipes only the dates it owns; rehearse against a local DB first (its docstring shows how) |
| Rotate the actor secret | change `ACTOR_TOKEN_SECRET`, restart | floor re-identifies with PINs; nothing persisted |
| Rotate an AI key | change the env var, restart | reviews are advisory; history keeps `(model, prompt_version)` rows |

Database backups are Supabase-managed (daily, point-in-time on paid tiers) —
recovery from data loss is a Supabase dashboard operation, not a repo one.
The one thing a restore cannot bring back is Storage objects deleted since the
snapshot; photo metadata rows point at them, which is why deletes in this
codebase are soft everywhere the spec allows.

## When you change something

- New table → RLS in the same migration, or `test_rls.py` fails the build.
- New endpoint → loading state, empty state, error state in the frontend; the
  two-trip budget; `require_management` or an explicit reason why not.
- New background work → wrap it in `run_job` so it brackets itself in
  `job_runs`. Nothing runs invisibly.
- New secret → `.env`, the settings class, and the secret-scan pattern if it
  has a recognisable prefix.
