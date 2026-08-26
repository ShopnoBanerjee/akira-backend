# AKIRA Ops Suite — Handoff Manual

**Read this first, in full, before touching anything.** It is written for a
Claude Code session picking the project up cold. It tells you what exists, why
it was built the way it was, how to run it, what will bite you, and what to do
next.

Last updated: end of P6, 27 Aug 2026. Both repos clean and pushed.

---

## 1. What this is

An internal multi-outlet operations platform for **AKIRA**, a Japanese ramen
restaurant group in Kolkata. Stage 1 delivers three things:

1. **Compliance** — every outlet demonstrably runs the same SOPs, with
   photographic proof and a manager sign-off that cannot be faked.
2. **Foundation** — auth, roles, outlets, users, inventory catalogue, settings.
3. **Sales ingestion skeleton** — not built yet (P9).

The governing specification is `docs/STAGE1_SPEC.md` (present in both repos).
It is the contract. Where this build deviates from it — and it does, in eleven
places — every deviation is recorded in `docs/DECISIONS.md` as D1–D11 with its
reasoning. **Read DECISIONS.md before proposing any change**; several
"obvious improvements" are things that were deliberately decided against.

The plan is in `docs/EPIC_PLAN.md`.

---

## 2. Repository layout

Two **separate** git repositories, not a monorepo (D1).

| | |
|---|---|
| `C:\Users\KIIT\Desktop\akira-app\akira-backend` | FastAPI · Python 3.12 · uv · port **8000** |
| `C:\Users\KIIT\Desktop\akira-app\akira-frontend` | Vite 6 · React 19 · TS strict · port **5173** |

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
                 business_date, scoring, enums, settings_registry
app/domains/     outlets/ users/ devices/ inventory/ settings/ jobs/ sop/
                 — each: router.py (HTTP) → service.py (logic, transactions,
                   audit) → repository.py (SQL) → schemas.py (pydantic)
app/integrations/ storage.py (Supabase Storage), supabase_auth.py (Auth Admin)
app/jobs/        EMPTY — this is P7's home
supabase/migrations/  0001–0012, append-only, source of truth for schema
supabase/seed/        001_outlets_and_sop.sql, 002_inventory_catalogue.sql
supabase/local/       0000_local_auth_shim.sql — TEST ONLY, never on Supabase
scripts/         export_openapi.py, seed_users.py,
                 generate_sop_seed.py, generate_inventory_seed.py
```

`app/domains/sop/` is the largest: `router.py` (template authoring),
`runs_router.py` + `runs_service.py` (the runner), `review_router.py` (P6).

### Frontend structure

```
src/app/         Router.tsx (path-based), AppShell (/app), FloorShell (/floor),
                 navigate.ts
src/features/    auth/ dashboard/ admin/{outlets,users,devices,inventory,
                 settings,jobs}/ sop/{templates,review}/ floor/
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

**RLS.** Enabled and FORCED on all 25 tables. `anon` has zero grants;
`authenticated` has SELECT only. There is no browser write path — every write
goes through FastAPI, which holds the service role.

---

## 4. The eleven decisions (D1–D11)

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

---

## 5. Environment and credentials

**Never commit secrets.** Both repos gitignore `.env` and
`.seed-credentials.md`. Scan staged diffs before every commit:

```bash
git diff --cached | grep -E "^\+" | grep -qE "sb_secret_|eyJ[A-Za-z0-9_-]{30,}" && echo ABORT || echo clean
```

`akira-backend/.env` holds (values are IN that file, not here):
`SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, `SUPABASE_SECRET_KEY`,
`SUPABASE_JWKS_URL`, `DATABASE_URL`, `TEST_DATABASE_URL`, `PHONE_HASH_SALT`.

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
pnpm typecheck && pnpm lint && pnpm test && pnpm build
```

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

Live and fully migrated. **`db.<ref>.supabase.co` resolves to IPv6 only** — it
works from this machine but is unreachable from IPv4-only environments (many CI
runners). Use the session pooler there. Storage bucket `sop-photos` exists,
private, 5MB cap, image MIME types only.

---

## 7. What is built (P0–P6)

**65 API operations across 51 paths. 162 backend tests. 25 tables. All live on
Supabase.**

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

There is **real data** in the system now: several approved and submitted runs,
photos in storage, one resolved exception. Do not wipe it — it is what makes
P7 and P8 testable.

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
anchor matched.** Prettier and ruff reflow lines; a silent no-op patch cost a
long debugging session in P5.

**Heredocs break on nested quotes.** For files containing `'` inside SQL
strings or f-strings with quotes, use the Write tool, not `cat <<'EOF'`.

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

---

## 9. NEXT: P7 — Integrity engine, scheduled jobs, AI photo review

This is the last large epic and it has three distinct parts. Read
`docs/STAGE1_SPEC.md` section 4.2 and `docs/DECISIONS.md` D6 first.

### 9.1 Integrity checks (`app/domains/sop/integrity.py`)

Run at photo-confirm and again at submit. Flags are written to
`checklist_run_items.integrity_flags` (a text array) and counted into
`checklist_runs.integrity_flag_count`. **Flags never block a submission.**

| Check | Rule | Flag |
|---|---|---|
| Re-used photo | `imagehash.phash` on upload, stored in `photo_phash` (16-char hex). Compare against the last N days for the same `(outlet_id, template_item_id)`; Hamming distance ≤ threshold = match. Record WHICH run it matched so the UI can say so. | `duplicate_photo` |
| Batch faking | >X% of a run's photos uploaded within Y minutes of `submitted_at`, or a 10+ item run completed in under 90 seconds | `burst_upload` |
| Off-site | haversine(submit geo, outlet geo) > `geofence_radius_m`. **Missing geolocation is NOT a flag** — `geo_ok` stays null and is counted separately. Already implemented in `runs_service.submit_run`; the flag write is not. | `out_of_geofence` |
| Late | `submitted_at > due_at + grace_minutes`. Already computed; needs the flag. | `late` |
| Gallery pick | `photo_uploaded_at` outside `[started_at, submitted_at]` | `stale_capture` |
| Too dark | Mean luminance below `ai_review.min_luminance`. **Deterministic, not AI.** | `too_dark` |

Thresholds come from the settings registry, already defined:
`integrity.phash_max_distance` (5), `integrity.phash_lookback_days` (30),
`integrity.burst_window_minutes` (3), `integrity.burst_share` (0.8),
`ai_review.min_luminance` (40). Resolve them with the `setting_value(key,
outlet_id, at)` SQL function or a Python helper reading the registry defaults.

**Photo processing must not run inside the request.** Use a FastAPI
`BackgroundTask` writing to `job_runs` so a failure is visible rather than
silent. You will need `pillow` and `imagehash` added to `pyproject.toml`.

### 9.2 Scheduled jobs (`app/jobs/`, currently empty)

APScheduler in-process (single instance for Stage 1). **Every job records
start/finish/error in `job_runs`** — the table and its read API already exist,
and `/app/settings/jobs` in the frontend is already built and waiting for rows.

1. **`materialise_runs`** — 05:00 Asia/Kolkata daily. The logic already exists
   and is tested (`runs_service.materialise_runs`, idempotent, verified). This
   is just the schedule wrapper. Time from `jobs.materialise_time`.
2. **`mark_missed`** — every `jobs.missed_check_minutes` (15). Runs still
   `pending` past `due_at + grace_minutes` become `missed` and raise a
   medium-severity `sop_exception`.
3. **`daily_digest`** — 09:00 IST. Per outlet: yesterday's completion rate,
   on-time rate, mean score, critical fails, open exceptions, integrity flags,
   and a random `jobs.digest_spot_check_share` (10%) sample of approved runs
   flagged for owner spot-check — **use `run_review_views` for the "approved
   without looking" signal**, which is exactly why that table exists. Rendered
   as HTML email to owner + ops_manager + that outlet's manager.

   Use a pluggable `Notifier` interface with `EmailNotifier` and `LogNotifier`.
   **Do not hardcode email** — WhatsApp is expected in Stage 2 and staff in
   Indian F&B live on WhatsApp.

Add `POST /sop/runs/materialise` already exists as the manual trigger; add
"run now" buttons for the other jobs on the jobs settings page (owner only).

### 9.3 AI photo review (D6)

**Advisory only. It never blocks a submission and never approves a run.**

Two tables already exist and are empty:
- `outlet_item_reference_photos` — per-outlet photographic standard for an
  item, one active per `(outlet_id, template_item_id)`.
- `run_item_ai_reviews` — verdict (`pass`/`fail`/`uncertain`), confidence,
  rationale, model, prompt_version, latency. Separate from the run item so a
  review can be re-run against a newer model without destroying history.

**Prerequisite that does not exist yet: an admin flow to capture reference
photos.** Nothing can be compared until New Town's standards are shot under
service lighting. This was moved into P3a's definition of done but never built
because the upload pipeline arrived in P5 — that pipeline (`storage.py` signed
uploads) now exists and should be reused.

The pipeline: on photo-confirm, if `ai_review.enabled` for that outlet, queue a
background task that fetches the submitted photo and that outlet's reference
photo, asks a vision model whether the task appears done, and writes a
`run_item_ai_reviews` row. Verdicts below
`ai_review.uncertain_below_confidence` display as uncertain. A `fail` verdict
adds the `ai_mismatch` integrity flag — which, like every flag, is advisory.

**Before writing any model-calling code, load the `claude-api` skill** for
current model IDs and the vision API shape. Do not guess model names.

The review UI already renders integrity flags as red chips with plain-language
tooltips (`FLAG_COPY` in `ReviewDetailPage.tsx`) — `ai_mismatch` copy is
already written. Add the AI verdict and rationale to that screen.

### 9.4 Definition of done for P7

- 05:00 job creates exactly the right runs for both outlets; re-running creates
  no duplicates (already proven for the underlying function).
- An unstarted run past grace flips to `missed` and raises an exception.
- Re-uploading yesterday's photo produces a visible `duplicate_photo` flag in
  the review screen, naming the run it matched.
- pHash does NOT flag two genuinely different photos of the same station.
- A dark photo flags `too_dark`.
- Job failures appear in `/app/settings/jobs` rather than vanishing.
- Reference photos can be captured per outlet through the admin UI.
- With AI review enabled, a submitted photo gets a verdict + rationale visible
  to the reviewing manager, and disabling it stops the calls entirely.

---

## 10. After P7

- **P8** Compliance dashboard. `app/core/scoring.py` already has the run-level
  functions; the outlet-level formula (0.50 mean + 0.30 completion + 0.20
  on-time, minus penalties, clamped, with the amber cap on unresolved critical
  failures) is spec section 4.3 and not yet written. The dashboard must render
  as a four-pillar card with Sales/Inventory/Guest greyed as "Coming in Stage
  2" so the layout does not change later.
- **P9** Sales ingestion. Real Petpooja exports are in `C:\Users\KIIT\Downloads`
  (`Orders_Master_Report_2026_08_25_*.xlsx`, `Item_Sale_Report_Hourly_Wise_*.xlsx`).
  Totals must reconcile to ₹4,86,076 across 452 orders; a bill at 00:45 on
  23 Aug must land on business_date 2026-08-22.
- **P10** Hardening: security review table, an RLS cross-outlet pytest, N+1
  audit, realistic 8-week seed dataset, RUNBOOK.md.
- **Stage 2** starts with the inventory/requisition engine. The user has already
  supplied a real requisition PDF for testing the AI extraction:
  `tests/fixtures/requisition_27aug2026.pdf`. The rule that carries over: **the
  LLM parses and explains; deterministic code decides.**

---

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
