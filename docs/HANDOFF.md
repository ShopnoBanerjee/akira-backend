# AKIRA Ops Suite — Handoff Manual

**Read this first, in full, before touching anything.** It is written for a
Claude Code session picking the project up cold. It tells you what exists, why
it was built the way it was, how to run it, what will bite you, and what to do
next.

Last updated: end of P17, 3 Sep 2026. Both repos clean and pushed.

> The single most reliable thing in this file is that it will fall behind.
> Run `git log --oneline | head` in both repos before trusting any epic
> number below — that is the source of truth, and this header has been
> wrong by ten epics before.

---

## 1. What this is

An internal multi-outlet operations platform for **AKIRA**, a Japanese ramen
restaurant group in Kolkata. Stage 1 delivers three things:

1. **Compliance** — every outlet demonstrably runs the same SOPs, with
   photographic proof and a manager sign-off that cannot be faked.
2. **Foundation** — auth, roles, outlets, users, inventory catalogue, settings.
3. **Sales ingestion skeleton** — not built yet (P9).

The governing specification is `docs/STAGE1_SPEC.md` (present in both repos).
It is the contract. Where this build deviates from it — and it does, in twenty-four
places — every deviation is recorded in `docs/DECISIONS.md` as D1–D24 with its
reasoning. **Read DECISIONS.md before proposing any change**; several
"obvious improvements" are things that were deliberately decided against.

The plan is in `docs/EPIC_PLAN.md`. What is knowingly incomplete, and what
would unblock each of those things, is in `docs/OPEN_ITEMS.md` — read it before
concluding something is broken.

---

## 2. Repository layout

Two **separate** git repositories, not a monorepo (D1).

| | |
|---|---|
| `C:\Users\KIIT\Desktop\akira\akira-app\akira-backend` | FastAPI · Python 3.12 · uv · port **8000** |
| `C:\Users\KIIT\Desktop\akira\akira-app\akira-frontend` | Vite 6 · React 19 · TS strict · port **5173** |

Remotes: `github.com/ShopnoBanerjee/akira-backend` and `.../akira-frontend`.

The backend owns the database schema, all business logic, and the OpenAPI
contract. The frontend generates its types from that contract:

```bash
cp ../akira-backend/openapi.json . && pnpm gen:api
```

CI in both repos fails if the committed types drift.

### Backend structure

```
app/core/        config, db, deps, errors, audit, security, actor,
                 business_date, scoring, enums, settings_registry,
                 settings_value (resolves a setting at a moment in time)
app/domains/     outlets/ users/ devices/ inventory/ settings/ jobs/ sop/
                 — each: router.py (HTTP) → service.py (logic, transactions,
                   audit) → repository.py (SQL) → schemas.py (pydantic)
app/integrations/ storage.py (Supabase Storage), supabase_auth.py (Auth Admin),
                 vision.py (the advisory photo review's model call)
app/jobs/        runner.py (job_runs bracketing), scheduler.py (APScheduler),
                 tasks.py (the three jobs), digest.py, notify.py
supabase/migrations/  0001–0019, append-only, source of truth for schema
supabase/seed/        001_outlets_and_sop.sql, 002_inventory_catalogue.sql
supabase/local/       0000_local_auth_shim.sql — TEST ONLY, never on Supabase
scripts/         export_openapi.py, seed_users.py,
                 generate_sop_seed.py, generate_inventory_seed.py
```

`app/domains/sop/` is the largest: `router.py` (template authoring),
`runs_router.py` + `runs_service.py` (the runner), `review_router.py` (P6),
`integrity.py` + `ai_review.py` + `reference_router.py` (P7).

### Frontend structure

```
src/app/         Router.tsx (path-based), AppShell (/app), FloorShell (/floor),
                 navigate.ts
src/features/    auth/ dashboard/ admin/{outlets,users,devices,inventory,
                 settings,jobs}/ sop/{templates,review,reference}/ floor/
src/lib/         api.ts (fetch + problem+json + actor token), supabase.ts,
                 image.ts (client resize), utils.ts
src/components/ui/primitives.tsx   hand-rolled Button/Dialog/etc.
```

---

## 3. Non-negotiable conventions

These are in `CLAUDE.md` in each repo. Violating any of them is a bug, not a
style choice.

**BUSINESS DATE.** AKIRA trades past midnight. A night starting 18:00 Saturday
and ending 01:30 Sunday is ONE business day, rolling over at **05:00 IST**.
Expressed in exactly two places — `app/core/business_date.py` and the Postgres
function `business_date(timestamptz)` — which are tested against each other
across a full day at ten-minute steps. **Never** group or filter a report by
`created_at::date`. The rollover is deliberately **not** an editable setting:
the SQL function is `immutable` so the planner can use it in indexes, and every
historical row already stores its `business_date`.

**MONEY.** Integer paise, `bigint`, columns end `_paise`. Never float. Format
only at the UI edge.

**AUDIT.** Every mutating service method writes an `audit_log` row, joining the
caller's transaction so an audit row can never survive a rolled-back change.

**SEPARATION OF DUTIES.** A run's approver can never be its submitter. Enforced
three deep: the review router refuses device sessions and non-approver roles,
the handler refuses `caller == submitter`, and a Postgres CHECK constraint
would reject the row anyway.

**SOFT DELETE.** `deleted_at` on user-facing entities; all queries filter it.

**RLS.** Enabled and FORCED on all 25 tables. Note 0013 added columns only, no
new tables. `anon` has zero grants;
`authenticated` has SELECT only. There is no browser write path — every write
goes through FastAPI, which holds the service role.

---

## 4. The decisions (D1–D24)

Full text in `docs/DECISIONS.md`. Summary, because each one will look like a
mistake until you know why:

| | |
|---|---|
| **D1** | Two repos, not a monorepo. Migrations live in the backend; the frontend derives enums from OpenAPI instead of a shared package. |
| **D2** | Frontend is Vite, not Next.js. The Supabase snippet supplied at kickoff was Next-specific dashboard boilerplate. Only `createBrowserClient` is used; there is no `server.ts` or `middleware.ts`. |
| **D3** | **Shared outlet tablet.** Floor staff have no personal phones. The tablet holds one outlet-bound Supabase session; individual staff identify with an Argon2 PIN which mints a short-lived HMAC actor token. `submitted_by` still resolves to a real person. A PIN authorises floor actions only and can NEVER approve. |
| **D4/D8** | Only **2 of 7** supplied "checklist" PDFs were actually checklists; the other five were inventory count sheets. Seed uses the two real ones + mise-en-place as numeric prep checks + one Food Safety template (the paper logs no temperatures at all). |
| **D5** | Supabase signs JWTs with **ES256** (asymmetric). Verify against JWKS. There is no shared HS256 secret and none should be configured. |
| **D6** | **AI photo review moved into Stage 1** at the user's request (spec put it in Stage 2). Per-outlet reference photos; the AI is **advisory only** — verdict + confidence + rationale, never blocks a submission, never approves. "Visible light" is a deterministic luminance check, not an AI call. |
| **D7** | Schema extensions the real checklists forced: bilingual columns everywhere (English + Bengali — the kitchen reads Bengali), `alternate_day` and `fortnightly` frequencies with `interval_days`/`anchor_date`. |
| **D9** | `app_settings` is **append-only with an effective date**. The value in force at a moment is the newest row at-or-before it, outlet override beating global. Historical scores stay reproducible. The registry in `app/core/settings_registry.py` owns each key's type/default/range. |
| **D10** | Inventory catalogue pulled into Stage 1: ONE shared catalogue, levels per outlet. 151 items seeded from the real count sheets. |
| **D11** | **Template item versioning.** Any material item edit bumps `checklist_templates.version` AND snapshots every item into `checklist_template_item_versions`, in one transaction. Runs point at the exact version they were answered against, so an admin flipping `is_critical` today cannot retroactively make past runs look like critical failures. |
| **D15** | **Sales is orders only, and the uploader picks the outlet.** No Petpooja item export carries a bill number — all four were checked — so `sales_order_items` cannot be filled and is deliberately left empty. Idempotency is on the file's bytes, not its name. Overlapping exports upsert. Written set-at-a-time: the per-row loop took 75s for 452 bills, the `unnest` upsert takes 5.4s. |
| **D14** | **No blended health score yet.** Spec section 5's four pillars sum to 100; Stage 1 has one of them. The SOP score is the headline and the other three are greyed — `0.30 x SOP` would read as 27/100 for a perfect outlet, and rescaling would make the number jump the day a second pillar lands. Also: a component with no denominator contributes 0 rather than being re-weighted out; nothing scheduled is *no score*, not zero; the critical-failure cap holds the BAND at amber and leaves the number honest. |
| **D13** | **A second vision provider, testing only.** `AI_REVIEW_PROVIDER` picks `anthropic` (default, production) or `groq`. Same prompt, same question, different transport; the model that actually answered is stored on every verdict. Groq's only image-capable model is `qwen/qwen3.8-27b` and its free tier is 8000 TPM, which two photos exhaust — so a 429 is a first-class "no verdict". |
| **D12** | **What P7 decided for itself.** Six rules, each of which looks arbitrary until you know why: a flag carries its evidence (`integrity_detail`); run-level flags live on the run, not stamped onto each photo; each pass clears only its own flags; `mark_missed` never touches `in_progress`; the AI confidence threshold is applied at READ time and `ai_mismatch` fires in one direction only; notifications degrade **loudly**. Vision model is `claude-sonnet-5` by the owner's decision. |

---

## 5. Environment and credentials

**Never commit secrets.** Both repos gitignore `.env` and
`.seed-credentials.md`. Scan staged diffs before every commit:

```bash
git diff --cached | grep -E "^\+" | grep -qE "sb_secret_|sk-ant-|eyJ[A-Za-z0-9_-]{30,}" && echo ABORT || echo clean
```

(Editing this line will make the scan match itself. Read what it matched
before believing an ABORT — but always read it.)

`akira-backend/.env` holds (values are IN that file, not here):
`SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SECRET_KEY`,
`SUPABASE_JWKS_URL`, `DATABASE_URL`, `TEST_DATABASE_URL`, `PHONE_HASH_SALT`.

P7 added, all optional and all documented in `.env.example`:

| | |
|---|---|
| `SCHEDULER_ENABLED` | Default true. **Exactly one instance may have this on.** |
| `SMTP_HOST` / `_PORT` / `_USERNAME` / `_PASSWORD` / `_FROM` / `_STARTTLS` | Unset, so the digest degrades to logging and says so. |
| `ANTHROPIC_API_KEY` | **Not set.** Without it the AI review records a skip rather than a verdict. |
| `AI_REVIEW_MODEL` | `claude-sonnet-5` (D12). |
| `AI_REVIEW_PROVIDER` | `anthropic` by default. Set to `groq` to test without an Anthropic key (D13). |
| `GROQ_API_KEY` / `GROQ_VISION_MODEL` | Currently set to a Groq key **that must be rotated** — it was pasted into a chat transcript. Model `qwen/qwen3.8-27b`. |

`akira-frontend/.env.local` holds the two `VITE_SUPABASE_*` values (both
browser-safe) and `VITE_API_BASE_URL`.

**Test logins** are in `akira-backend/.seed-credentials.md` (gitignored),
regenerated by `uv run python scripts/seed_users.py`. Nine users + two device
accounts, all `@akira.test`:

| Login | Role | PIN |
|---|---|---|
| `owner@akira.test` | owner, both outlets | — |
| `ops@akira.test` | ops_manager, both outlets | — |
| `manager.nt@akira.test` | outlet_manager AKR-NT01 | — |
| `lead.nt@akira.test` | shift_lead | 1111 |
| `lead2.nt@akira.test` | shift_lead | 2222 |
| `staff.nt@akira.test` | staff | 3333 |
| `staff2.nt@akira.test` | staff | 4444 |
| `manager.dev@akira.test` | outlet_manager AKR-DEV02 | — |
| `staff.dev@akira.test` | staff AKR-DEV02 | 5555 |
| `device.nt01@akira.test` | shared tablet, AKR-NT01 | (device) |
| `device.dev02@akira.test` | shared tablet, AKR-DEV02 | (device) |

Passwords rotate every time the seed script runs — always re-read the file.

> `@akira.test` is a **reserved special-use domain** that `email-validator`
> rejects. The invite endpoint will 422 on it. Use a real-looking domain when
> testing invites.

---

## 6. Running it

```bash
# Backend
cd akira-backend
uv sync --all-groups
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000   # /docs for OpenAPI UI

# Frontend
cd akira-frontend
pnpm install
pnpm dev                                                    # http://localhost:5173
```

Checks (run all before committing):

```bash
# backend
uv run ruff check . && uv run ruff format --check . && uv run mypy app
TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5433/postgres uv run pytest
uv run python scripts/export_openapi.py     # after ANY endpoint change

# frontend
pnpm typecheck && pnpm lint && pnpm format:check && pnpm test && pnpm build
```

`pnpm format:check` is in CI and is easy to forget locally.

Starting the API also starts the scheduler. To work without it firing jobs at
you, `SCHEDULER_ENABLED=false uv run uvicorn ...`.

### Test database

The machine has PostgreSQL 18 installed but **port 5432 is occupied and
password-protected**. Tests use a throwaway cluster on **5433** created from the
same binaries. If it is not running:

```bash
SCRATCH="<scratchpad>/pgdata"
"/c/Program Files/PostgreSQL/18/bin/initdb.exe" -D "$SCRATCH" -U postgres \
  --auth-local=trust --auth-host=trust --encoding=UTF8 --locale=C
"/c/Program Files/PostgreSQL/18/bin/pg_ctl.exe" -D "$SCRATCH" -l "$SCRATCH/../pg.log" \
  -o "-p 5433 -c listen_addresses=127.0.0.1" start
```

The pytest fixtures build a database from zero every session — shim, all
migrations in order, then seeds. Docker Compose exists but Docker Desktop was
never successfully started on this machine; it is not required.

### Supabase

Live and fully migrated through 0013. **`db.<ref>.supabase.co` resolves to IPv6
only** — it works from this machine but is unreachable from IPv4-only
environments (many CI runners). Use the session pooler there. Storage bucket
`sop-photos` exists, private, 5MB cap, image MIME types only. It now holds
submitted photos under `{outlet}/{business_date}/{run}/{item}.jpg` and reference
standards under `reference/{outlet}/{template_item}.jpg`.

Migrations are applied to Supabase by hand — there is no `supabase db push` in
this workflow. Read the file, run it inside a transaction with asyncpg using
`DATABASE_URL`, then verify the columns exist. That is how 0013 landed.

---

## 7. What is built (P0–P17)

**98 API operations across 80 paths. 36 tables, 19 migrations. All live on
Supabase, and CI is green.** Test counts move every epic — run the suites
rather than quoting this line.

Stage 1 is P0–P10. Stage 2 so far is P11 (stock-count extraction), P12 (sales
pillar + narrated digest), P13 (consumption windows + anomalies), P14 (the
Order Listing adapter), P15 (all four pillars + the blended score), P16 (the
forecasting baseline), P17 (recipes + theoretical consumption). Each has its
own decision entry; read `docs/DECISIONS.md` D16–D24 for Stage 2.

- **P0** Scaffold both repos, CI, tooling.
- **P1** Schema (12 migrations), RLS, indexes, seed: 2 outlets, 6 categories,
  14 templates, 57 items, 28 assignments, 151 inventory items. Migration test
  suite including Python↔SQL business-date parity.
- **P2** Auth: ES256 JWT verification against JWKS with kid-miss refresh,
  `CurrentUser` with outlet memberships, role guards, RFC 7807 problem+json
  errors, two role-aware shells.
- **P3a** Organisation admin: outlets CRUD, user invite/roles/outlets/PINs,
  device registration. Permission rules are pure functions in
  `app/domains/users/permissions.py` with exhaustive tests — **nobody can grant
  a role at or above their own**.
- **P3b** Configuration admin: inventory catalogue (bilingual search), settings
  registry (23 keys, effective-dated history), job runs view.
- **P4** SOP template builder with the D11 versioning transaction, drag-free
  reorder, assignments matrix, live "what staff will see" phone preview.
- **P5** The checklist runner: PIN identify, today's runs, item-by-item flow,
  photo capture with client resize + signed-URL upload, offline draft in
  IndexedDB with a sync queue that gates submit, scoring, geofence, exceptions.
- **P6** Manager review: queue oldest-first (no bulk approve, deliberately),
  photo lightbox with signed view URLs, review-depth tracking
  (`run_review_views`), approve/reject with separation of duties, exception
  board with acknowledge/resolve/waive.
- **P7** The integrity engine (all six checks, with evidence), the three
  scheduled jobs on an in-process APScheduler, and the advisory AI photo
  review with its per-outlet reference-photo capture flow. Migration 0013.
  See D12 for the six choices it forced, and D13 for the second vision
  provider added so the AI path could actually be run.
- **P8** The compliance dashboard: the outlet formula from spec 4.3, one shared
  counter behind it and the digest, and the four-pillar card with three pillars
  greyed. See D14.
- **P9** Sales ingestion. A versioned Petpooja adapter, upload idempotent by
  content hash, background parse to `sales_orders`, and the raw table view.
  Migration 0014. See D15.
- **P10** Hardening. Latency cut 3–5× by cutting round trips (D16 — measured,
  not guessed: the wire is 152 ms and Postgres is 0.2 ms). The RLS suite that
  attacks the policies as a leaked key would (`test_rls.py`). The N+1 audit
  (two fixed, one with a real race; three recorded non-findings). SECURITY.md,
  RUNBOOK.md, and `scripts/seed_history.py` — eight weeks of coherent demo
  history built through the real materialiser, scorer and photo pass.

There is **real history** in the system now, seeded by
`scripts/seed_history.py` on 27 Aug: ~850 runs across eight weeks
(2026-07-01 onward) for both outlets, 212 decodable photos processed by the
real photo pass (including a planted duplicate pair and a too-dark shot at
DEV02, both caught), 97 late runs with real integrity flags, exceptions in
every state, and synthetic-but-labelled DEV02 sales beside NT01's REAL
Petpooja data (467 bills through 27 Aug, whose export's own Total matches
our parse to the paisa — `reported_net_paise` = `parsed_net_paise`). Business
date 2026-08-27 carries genuine artifacts: real photos and the two real Groq
verdicts. The old caveats (stub photos, hand-materialised dates) were wiped
with the reseed. To rebuild: the script is deterministic — read its docstring,
rehearse locally first.

---

## 8. How to work on this codebase

This matters more than any individual instruction below.

**Test against the live system, not your reading of the code.** Every single
epic surfaced at least one bug that code review would never have found:

- Device sessions were bounced at "awaiting activation" — the floor endpoints
  were unreachable from the very sessions they existed for.
- Postgres `numeric` arrives in JSON as a **string**; arithmetic silently broke.
- A concurrent-reorder **deadlock** from row-by-row `UPDATE` in list order.
- TanStack Query's cache **survived sign-out**, leaking one person's data to the
  next on a shared tablet.
- The post-login redirect keyed off the URL, so a manager signing in after a
  staff member on the same tablet could never reach the management UI.
- Deep links redirected to `/app`, breaking every bookmark.

The pattern: write it, run it, drive the real UI or the real API, then check
the **database** for ground truth. Instrumentation lies; the database does not.
I once nearly "fixed" working code because a `fetch` interceptor missed a call
the database showed had happened.

**When patching a formatted file with a Python string replace, assert the
anchor matched — and that it matched exactly once.** Prettier and ruff reflow
lines, so a silent no-op patch cost a long debugging session in P5. Worse, in
P7 an anchor ending `**audit_ctx,
    )
    await db.commit()` matched both
`start_run` and `submit_run`, and `str.replace` cheerfully edited both. Assert
`s.count(anchor) == 1`, not `anchor in s`.

**Do not let Prettier near the synced docs.** `HANDOFF.md`, `DECISIONS.md` and
`STAGE1_SPEC.md` exist in both repos as identical copies. `pnpm format`
reflowed every markdown table in this file, which would make the two diverge on
every sync for no reason. `docs` is now in the frontend's `.prettierignore`.

**Heredocs break on nested quotes.** For files containing `'` inside SQL
strings or f-strings with quotes, use the Write tool, not `cat <<'EOF'`. This
bit again in P7 while appending a test file; the shell died on an apostrophe
inside a docstring and wrote nothing.

**A bare `Settings()` in a test reads the developer's `.env`.** So a test
asserting a default passes or fails depending on whose machine it runs on, and
one dispatch test quietly reached the real network once a provider was
configured locally. Use `tests/conftest.py::isolated_settings`.

**A job that reports a next run time is not a job that runs.** P7's schedule
reconciler re-added every job every five minutes with `replace_existing=True`.
Replacing a job rebuilds its trigger, and an interval trigger counts from the
moment it is built — so `mark_missed`, on a fifteen-minute interval, was pushed
fifteen minutes into the future every five and could never fire. Everything
*said* it was fine: the scheduler listed it, `/jobs/schedule` showed a
plausible next fire time, the logs showed the reconciler succeeding. The only
evidence was an absence — no row in `job_runs` with `triggered_by is null`,
ever. **When a background job is meant to have run, the question is not "is it
scheduled" but "what did it write".**

**Nobody had ever looked at CI.** It was red on every backend push from P1 to
P8 — eight epics — over a single stray `
` in a workflow. There is no `gh` on
this machine, but the public API needs no auth:
`curl -s "https://api.github.com/repos/ShopnoBanerjee/akira-backend/actions/runs?per_page=5"`.
Check it after pushing.

**`text()` bind parameters cannot be followed by `::casts`.** `:action::audit_action`
is a syntax error through SQLAlchemy. Always `cast(:action as audit_action)`.

**Pydantic model names are global in OpenAPI.** Two domains both declaring
`UpdateItemRequest` mangled the generated frontend types. Keep them unique.

**Warn, never block.** Integrity flags, item-count ceilings, critical-share
warnings — all advisory. Blocking creates workarounds; visibility creates
accountability. This is a spec principle and it holds throughout.

**Bilingual everywhere.** Every user-facing string on the floor shows Bengali
first, English second. Kitchen staff read Bengali. Any new checklist-facing
field needs a `_bn` counterpart.

**A flag without evidence is an accusation.** P7's version of "warn, never
block": every integrity flag records *why* in `integrity_detail`, and the UI
renders that beneath the chip. A red badge a manager cannot check is one they
learn to ignore, at which point the check may as well not exist. Apply the same
rule to anything P8 flags.

**Check what is actually in Storage, not what the row says.** The six photos
uploaded during P5 and P6 have plausible `photo_bytes` values and are 262-byte
stubs that no decoder will open. The row was not lying; nobody had ever read
the object back. P7 found this by downloading them, not by reading code.

---

## 9. What P7 actually shipped, and where the edges are

Three parts, all live. Read D12 before changing any of it.

### 9.1 The integrity engine — `app/domains/sop/integrity.py`

All six checks run, and all but one are proven against the live system. Flags
never block a submission.

| Flag | Where it lives | Proven by |
|---|---|---|
| `duplicate_photo` | item | re-uploading the previous day's sink photo through the real tablet flow — matched at Hamming distance 0 and named the run |
| `too_dark` | item | a luminance-14 photo against the default floor of 40 |
| `stale_capture` | item | unit tests only; needs a photo confirmed before the run was started |
| `late` | run | a run submitted 33 hours past due |
| `out_of_geofence` | run | submitting from central Kolkata, 9km from New Town |
| `burst_upload` | run | a scripted run that uploaded everything in 36 seconds |

Two failure modes to know about:

- **pHash false positives are what would kill this feature.** A duplicate
  detector that flags honest work gets switched off, and then nothing is
  checked at all. There is a test asserting two genuinely different photos of
  the same station stay well outside the threshold. Keep it.
- **Photo work never runs in a request.** `confirm-photo` and `submit` hand it
  to a `BackgroundTask` wrapped in `run_job`, so a storage timeout or a corrupt
  JPEG lands on `/app/settings/jobs`. An undecodable image raises rather than
  being swallowed, and the item stays unprocessed — which the review screen
  shows as "not checked", which is honest.

`scripts/backfill_photo_integrity.py` hashes photos uploaded before P7. It is
idempotent and skips already-processed rows unless given `--all`.

### 9.2 The scheduled jobs — `app/jobs/`

APScheduler on the API's own event loop, started from the lifespan.
**`SCHEDULER_ENABLED` must be true on exactly one instance.** Two replicas
would both send the digest and every manager would get it twice. A shared
advisory lock is the prerequisite for a second instance.

- `materialise_runs` — 05:00 local, from `jobs.materialise_time`.
- `mark_missed` — every `jobs.missed_check_minutes`. **`pending` only**, never
  `in_progress`: `missed` is terminal and would discard half-finished work.
- `daily_digest` — 09:00 local, one `job_runs` row per outlet.
- `reconcile_schedule` — every 5 minutes, re-reads the two times from settings
  so an admin editing them does not have to restart the API.

`POST /jobs/{name}/run` is owner-only and refuses anything outside the three.
`GET /jobs/schedule` reads the live scheduler rather than recomputing from
settings, because the question that screen answers is "is this really going to
run".

**The digest does not currently send mail.** No SMTP host is configured, so
`get_notifier` falls back to `LogNotifier` and records `smtp_not_configured` on
the `job_runs` row. This is deliberate (D12.6): set `SMTP_HOST` and the rest in
`.env` and it starts sending with no code change. The seeded accounts are all
`@akira.test`, which cannot receive mail — testing delivery needs one real
address.

### 9.3 AI photo review — `app/domains/sop/ai_review.py`, `app/integrations/vision.py`

**`ai_review.enabled` defaults to false, and off means no bytes fetched and no
request made.** Turn it on per outlet from the settings screen once that
outlet's reference standards exist.

`/app/sop/reference-photos` is the capture flow: one standard per item per
outlet, camera capture, retired-not-deleted so an old verdict stays readable
against the photograph it was actually compared to. New Town has **1 of 18**
captured; every other outlet has none. That is the real remaining work here,
and it is physical — somebody has to walk the outlet under service lighting.

**The pipeline is proven end to end against a real model — via Groq, not
Anthropic** (D13). New Town has `ai_review.enabled` **on**; every other outlet
is off. What was verified on live data: a grimy sink photo against that
outlet's own clean standard came back `fail` at confidence 1.0 with the
rationale *"Surface is covered with scattered dark debris and a large liquid
stain, clearly unclean compared to the bare reference"*, wrote its
`run_item_ai_reviews` row, raised `ai_mismatch` on an item staff had recorded
as a pass, and rendered on the review screen. Turning the setting off produced
**zero** model calls.

**Still unverified: the Anthropic path specifically.** There is no
`ANTHROPIC_API_KEY` on this machine. The orchestration around the call is
identical for both providers and is proven; what has never run is
`client.messages.parse`. To finish it: set the key, set
`AI_REVIEW_PROVIDER=anthropic`, and re-review a photo — it will write a second
row rather than overwrite the Groq one, because the table is keyed on the model.

**Rotate the Groq key in `.env`.** It reached this project through a chat
transcript.

This gap, the digest's missing mail server and the uncaptured reference
standards are all tracked with their unblockers in `docs/OPEN_ITEMS.md`.

**Before touching any model-calling code, load the `claude-api` skill.** The
current API shape — `client.messages.parse`, `output_config.effort`, adaptive
thinking, the model IDs — is not what a training prior will tell you.

---

## 10. NEXT: not yet chosen

P11–P17 are done (section 7). There is no P18 brief because the next epic has
not been picked, and picking it is the owner's call — several Stage 2 threads
are blocked on something only they can supply.

**Read `docs/OPEN_ITEMS.md` first.** It is the maintained list of deliberate
gaps and says what unblocks each. As of P17 the live blockers are:

- **An Akira "Item Report: Day Wise" export.** P17's third adapter
  (`petpooja.itemdays.v1`) was built against another restaurant's old file
  because no AKIRA one exists yet. The recipe and theoretical-consumption
  tables stay empty until one is uploaded, so the variance component has
  nothing real behind it.
- **Reference standards are barely captured.** The AI photo review (D6/D13)
  compares against per-outlet reference shots that mostly have not been taken.
  Someone has to walk New Town with the tablet.
- **No `ANTHROPIC_API_KEY`.** The vision path runs on Groq and extraction on
  Gemini (D18); `client.messages.parse` has still never executed.
- **The digest does not send mail.** No SMTP host, so it degrades to logging
  and records `smtp_not_configured`. Deliberate until there is a mailbox.

Candidate work that is NOT blocked, if the owner wants to keep moving:
labour scheduling off the forecast (spec section 5.1 names it as the second
consumer of forecast covers), the WhatsApp notifier (the `Notifier` interface
is already pluggable and staff live on WhatsApp), or a second pass on
hardening now that the surface is three times what P10 reviewed.


## 11. Working relationship

The user is Shopno Banerjee, building this for AKIRA. They move fast, expand
scope deliberately, and expect the work finished rather than proposed. Habits
that have worked:

- Ask when a decision is genuinely theirs (operational reality, what to seed,
  who may do what). Do not ask about implementation choices you can make.
- When their instruction conflicts with the spec, say so in a sentence, then
  build what they asked and record it in DECISIONS.md.
- Commit per epic with a message that explains **why**, not just what. Push
  when asked.
- Report what was actually verified and how. Say plainly when something is
  untested or assumed.
