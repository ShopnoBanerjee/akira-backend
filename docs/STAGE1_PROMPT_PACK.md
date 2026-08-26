# AKIRA Ops Suite — Claude Code Prompt Pack (Stage 1)

Companion to `AKIRA_OpsSuite_Stage1_Spec.md`. Run these **in order**, one per session or one per session-chunk. Each is self-contained enough to paste cold.

## How to run this

1. `mkdir akira-ops && cd akira-ops && git init`
2. Save the spec doc into the repo as `docs/STAGE1_SPEC.md` — several prompts tell Claude Code to read it.
3. Start Claude Code in that directory.
4. Paste **P0** first. It creates `CLAUDE.md`, which every later session reads automatically.
5. After each prompt: run the acceptance checks listed, commit, then move on. Do not batch two epics into one session — context rot causes exactly the "it rewrote my working code" problem.

**Two habits that matter more than the prompts:**
- Start each epic with `/clear`. Long sessions produce worse code.
- When Claude Code proposes something that contradicts `CLAUDE.md`, say so explicitly rather than accepting it — one silent deviation (a float for money, a `created_at::date` group-by) compounds across the whole codebase.

---

## P0 — Bootstrap: CLAUDE.md and repo skeleton

````
You are setting up a new project. Read docs/STAGE1_SPEC.md in full before writing anything.

This is AKIRA Ops Suite — an internal multi-outlet restaurant operations platform for a Japanese
ramen restaurant group in Kolkata, India. Stage 1 delivers auth + org foundation + an SOP/checklist
compliance module with photo proof, plus a sales-file ingestion skeleton.

Stack (fixed, do not propose alternatives):
- Frontend: Vite + React + TypeScript (strict) + Tailwind + shadcn/ui + TanStack Query + TanStack Router
- Backend: FastAPI (Python 3.12) + SQLAlchemy 2.x async + Alembic-style SQL migrations
- Data: Supabase — Postgres, Auth, Storage
- Package managers: pnpm (web), uv (api)

TASK 1 — Create CLAUDE.md at the repo root containing these rules verbatim in spirit, organised
clearly. This file is the constitution of the project; every future session reads it.

## Architecture boundary
- The frontend calls FastAPI for ALL business data.
- The frontend uses the Supabase JS client for exactly three things: auth session management,
  direct-to-Storage uploads using a signed URL that FastAPI minted, and realtime subscriptions.
- Never query application tables from the frontend with the Supabase client.
- FastAPI verifies the Supabase JWT against the JWKS endpoint, connects to Postgres directly,
  and enforces authorisation in code. RLS is enabled on every table as defence in depth.

## Non-negotiable conventions
- BUSINESS DATE: this restaurant trades past midnight. A trading night starting 18:00 Saturday and
  ending 01:30 Sunday is ONE business day. business_date = (ts at Asia/Kolkata − 5 hours)::date.
  Every dated operational row stores business_date. NEVER group or filter reports by created_at::date.
  The rollover is expressed in exactly two places — core/business_date.py and a Postgres function
  business_date(timestamptz) — and they are tested against each other.
- TIME: all timestamps are timestamptz stored UTC, rendered Asia/Kolkata. Outlet-local scheduled
  times are `time` + the outlet's timezone column.
- MONEY: integer paise (bigint), column names end in _paise. Never float, never Decimal in transport.
  Format only at the UI edge.
- IDS: uuid primary keys, gen_random_uuid().
- SOFT DELETE: deleted_at timestamptz on all user-facing entities; all queries filter it.
- AUDIT: every mutating service method writes an audit_log row. No exceptions.
- ENUMS: Postgres enums for closed sets, mirrored in packages/shared and re-exported to TS and Python.
- SEPARATION OF DUTIES: a checklist run's approver can never be its submitter — enforced by a
  Postgres CHECK constraint, not only in the UI.

## Backend layering
Each domain package under app/domains/<name>/ has router.py (HTTP only), service.py (business logic,
transactions, audit writes), repository.py (SQL only), schemas.py (pydantic).
Routers never touch the database. Repositories never contain business rules.
Scoring and money logic are pure functions in app/core/, unit-tested with table-driven tests.
Errors use one AppError hierarchy rendered as RFC 7807 problem+json. Never leak SQL or stack traces.

## Frontend layering
Feature-sliced under src/features/<name>/ with api/ components/ hooks/ types.ts.
Two shells: /app/* is desktop-first management UI; /floor/* is mobile-first staff UI (single column,
large tap targets, minimal chrome). Different layouts, shared auth.
shadcn/ui primitives live in src/components/ui/ and are NEVER hand-edited — extend by composition.
Server state is TanStack Query only. Zustand only for true client UI state (offline queue, run draft).
src/types/api.ts is GENERATED from the FastAPI OpenAPI schema — never hand-edited.
All date handling goes through src/lib/dates.ts. No inline date math in feature code.

## Brand tokens
red #ee3345 (primary accent) · blue #326fb7 (secondary) · ink #231f20 · white ground.
Health bands: green #2f9e5f, amber #e0a020, red #ee3345. Typeface Noto Sans (Noto Sans JP for katakana).
Flat colour, no gradients. Red is for the primary action and the red health band — not for chrome.

## Working rules for Claude Code
- Read docs/STAGE1_SPEC.md before any schema or module work.
- Migrations are append-only: never edit an applied migration, always add a new one.
- Every new endpoint gets a pytest. Every scoring/date/money function gets a unit test.
- Prefer boring, explicit code over clever abstraction. This is a small team's internal tool.
- When a request conflicts with this file, say so and ask — do not silently deviate.

TASK 2 — Scaffold the repo:

akira-ops/
├── CLAUDE.md
├── README.md
├── .env.example              # every var needed by web and api, documented
├── docker-compose.yml        # local postgres 16 + api
├── .github/workflows/ci.yml  # lint + typecheck + test for both apps
├── apps/web/                 # pnpm create vite (react-ts), Tailwind, shadcn init,
│                             #   TanStack Query + Router, folders: app/ features/ components/ lib/ types/
├── apps/api/                 # uv project: fastapi, uvicorn, sqlalchemy[asyncio], asyncpg, pydantic-settings,
│                             #   python-jose, httpx, pytest, pytest-asyncio, ruff, mypy
│                             #   app/{core,domains,integrations,jobs}/, main.py with /healthz and CORS
├── supabase/{migrations,seed}/
└── packages/shared/          # enums.ts + enums.py generated from one source of truth

Also add: root package.json scripts (`dev`, `lint`, `typecheck`, `test`) running both apps
concurrently; ruff + mypy config; eslint + prettier config; .gitignore.

ACCEPTANCE: `pnpm install && pnpm dev` starts Vite on 5173 and FastAPI on 8000; GET /healthz
returns 200; `pnpm lint && pnpm typecheck && pnpm test` all pass on the empty scaffold.

Do not write any feature code. Scaffold and configuration only.
````

---

## P1 — Database schema, RLS, seed

````
Read CLAUDE.md and docs/STAGE1_SPEC.md sections 3 and 4 before starting.

Write the complete Stage 1 Postgres schema as numbered SQL migrations in supabase/migrations/.
Use one migration file per logical group, numbered 0001_, 0002_, ...

0001_extensions_and_enums.sql
  - pgcrypto
  - enums: user_role (owner, ops_manager, outlet_manager, shift_lead, staff),
    run_status (pending, in_progress, submitted, approved, rejected, missed),
    item_result (pass, fail, na, pending), value_type (number, text, temperature_c, time),
    frequency (per_shift, daily, weekly, monthly), day_part (opening, mid, closing, any),
    exception_status (open, acknowledged, resolved, waived), severity (high, medium, low),
    sales_channel (dine_in, pickup, delivery), upload_status (received, parsing, parsed, failed)

0002_functions.sql
  - business_date(ts timestamptz) returns date, immutable:
      (ts at time zone 'Asia/Kolkata' - interval '5 hours')::date
  - set_updated_at() trigger function

0003_core.sql       — outlets, profiles, outlet_members, audit_log
0004_sop.sql        — sop_categories, checklist_templates, checklist_template_items,
                      checklist_assignments, checklist_runs, checklist_run_items, sop_exceptions
0005_sales.sql      — data_uploads, sales_orders, sales_order_items
0006_rls.sql        — RLS enabled on every table, restrictive policies (see below)
0007_indexes.sql    — see below

Column definitions are given in docs/STAGE1_SPEC.md section 3 — follow them exactly, including
the CHECK constraint `approved_by is null or approved_by <> submitted_by` on checklist_runs and the
unique constraints on (assignment_id, business_date, day_part), (outlet_id, external_bill_no),
and data_uploads.file_sha256.

RLS approach: FastAPI connects as a service role and enforces authz in code, so policies exist as a
second line of defence. Enable RLS on every table. Deny all to `anon`. For `authenticated`, write
policies that scope by outlet membership via a helper:
  create function auth_outlet_ids() returns uuid[] — reads outlet_members for auth.uid()
Owners and ops_managers see all outlets; everyone else only rows whose outlet_id is in auth_outlet_ids().

Indexes to create explicitly:
  checklist_runs (outlet_id, business_date), (status, due_at) where status in ('pending','in_progress')
  checklist_run_items (run_id), (photo_phash) where photo_phash is not null
  sop_exceptions (outlet_id, status, business_date)
  sales_orders (outlet_id, business_date), (business_date)
  sales_order_items (outlet_id, business_date, item_name)
  audit_log (entity_table, entity_id), (actor_profile_id, at desc)

Then write supabase/seed/001_seed.sql creating:
  - TWO outlets: 'AKR-NT01' AKIRA New Town (real, geo 22.5023 / 88.3852) and 'AKR-DEV02' Dev Outlet 2
    (so multi-outlet paths are exercised from day one)
  - one profile per role, all mapped to outlet 1; the ops_manager mapped to both
  - the 6 sop_categories
  - the 6 starter checklist templates with their items exactly as listed in spec section 4.4,
    with sensible requires_photo / is_critical / value_type / min / max flags
  - checklist_assignments wiring those templates to outlet 1 with realistic due times
    (opening 17:00, closing 00:30, weekly deep clean Monday 15:00, food safety daily 18:00)

Finally: a pytest that applies all migrations to a clean database, runs the seed, and asserts
business_date('2026-08-23T01:30:00+05:30') = 2026-08-22 and business_date('2026-08-23T06:00:00+05:30')
= 2026-08-23. Also assert the separation-of-duties CHECK actually rejects an insert where
approved_by = submitted_by.

ACCEPTANCE: migrations apply from zero with no errors; seed runs; both tests pass.
````

---

## P2 — Auth, roles, protected routing

````
Read CLAUDE.md. Implement authentication and authorisation end to end. No SOP features yet.

BACKEND (apps/api):
1. app/core/config.py — pydantic-settings reading SUPABASE_URL, SUPABASE_ANON_KEY,
   SUPABASE_SERVICE_KEY, SUPABASE_JWT_JWKS_URL, DATABASE_URL, ENV.
2. app/core/security.py — verify a Supabase JWT against the cached JWKS (refresh on kid miss),
   validate exp/aud/iss, return the `sub` claim. Clear AuthError on failure.
3. app/core/deps.py:
   - get_db() async session
   - current_user() -> CurrentUser (profile + outlet_members list), cached per request,
     404-safe if the auth user has no profile row yet
   - require_role(*roles) dependency factory
   - require_outlet_access(outlet_id) — owner/ops_manager pass for any outlet; others must have a
     matching outlet_members row. Raise 403 with problem+json, never 404-as-403.
4. app/core/errors.py — AppError hierarchy (AuthError, ForbiddenError, NotFoundError,
   ConflictError, ValidationError) with an exception handler emitting RFC 7807 problem+json.
5. app/domains/users/ — GET /me returning profile + role + outlets;
   PATCH /me for full_name and phone.
6. A profile-provisioning path: on first authenticated request with no profile row, create one with
   role 'staff' and is_active=false, and return 403 with a clear "awaiting activation" problem detail.
   This prevents a self-signup from silently getting access.

FRONTEND (apps/web):
1. src/lib/supabase.ts — Supabase client, session persistence, auto refresh.
2. src/lib/api.ts — fetch wrapper that attaches the current access token, parses problem+json into
   a typed ApiError, and handles 401 by refreshing once then signing out.
3. src/features/auth/ — login page (email + password, and magic link), forgot/reset password,
   AuthProvider exposing { session, profile, role, outlets, isLoading }.
4. Routing with TanStack Router:
   - /login (public)
   - /app/* — requires role in (owner, ops_manager, outlet_manager); desktop shell with sidebar
   - /floor/* — requires any authenticated active profile; mobile shell, single column
   - post-login redirect: staff and shift_lead → /floor, everyone else → /app
   - /403 and /pending-activation pages
5. A <RoleGate roles={[...]}> component and a useHasRole() hook for in-page gating.
6. Generate src/types/api.ts from the FastAPI OpenAPI schema; add a pnpm script `gen:api` that
   regenerates it. Wire it into CI so a drift between backend and frontend types fails the build.

Tests: pytest covering — valid token passes, expired token 401, wrong-outlet access 403,
staff cannot reach an ops-only route. Vitest covering the RoleGate and the api error mapping.

ACCEPTANCE: seeded users can log in; each role lands on the correct shell; a staff user hitting
/app is redirected to /403; `pnpm gen:api` produces types with no diff after a clean run.
````

---

## P3 — Admin: outlets and users

````
Read CLAUDE.md. Build outlet and user administration. Desktop shell only (/app).

BACKEND — app/domains/outlets/ and extend app/domains/users/:
  GET    /outlets                      list (scoped: owner/ops_manager see all, others see own)
  POST   /outlets                      owner only
  GET    /outlets/{id}
  PATCH  /outlets/{id}                 owner only
  DELETE /outlets/{id}                 owner only, soft delete, blocked if active runs exist
  GET    /users                        scoped by outlet access, filterable by role/outlet/active
  POST   /users/invite                 creates the Supabase auth user + profile + outlet_members,
                                       sends the invite email; caller may only assign roles strictly
                                       below their own; outlet_manager may only invite into their outlet
  PATCH  /users/{id}                   name, phone, employee_code, is_active
  PUT    /users/{id}/role              global_role — owner only above outlet_manager
  PUT    /users/{id}/outlets           replace outlet_members set
Every mutation writes an audit_log row with before/after.

FRONTEND — src/features/admin/:
  /app/settings/outlets   — data table (code, name, city, opened_on, active, member count),
                            create/edit sheet with geo lat/lng + geofence radius + timezone
  /app/settings/users     — data table (name, role badge, outlets, last seen, active),
                            invite dialog, role change with a confirm step, outlet assignment
                            multi-select, deactivate with confirm
Use shadcn Table + DataTable pattern with column sorting, a text filter, and pagination.
Role badges use brand colours. Destructive actions use AlertDialog with typed confirmation.

Show, don't hide, the permission rules: if the current user cannot assign a role, render it disabled
with a tooltip explaining why rather than omitting the option.

Tests: outlet_manager cannot invite an ops_manager (403); cannot list users of another outlet;
soft-deleted outlets disappear from list endpoints; audit rows are written for every mutation.

ACCEPTANCE: an owner can create the second outlet, invite one user per role, assign them, and see
every action reflected in audit_log.
````

---

## P4 — SOP template builder

````
Read CLAUDE.md and docs/STAGE1_SPEC.md sections 3.3 and 4. Build SOP template authoring. /app only.

BACKEND — app/domains/sop/ (templates portion):
  GET    /sop/categories
  GET    /sop/templates                     filter by category, active
  POST   /sop/templates                     owner, ops_manager
  GET    /sop/templates/{id}                with items ordered
  PATCH  /sop/templates/{id}
  POST   /sop/templates/{id}/items
  PATCH  /sop/templates/{id}/items/{itemId}
  DELETE /sop/templates/{id}/items/{itemId} hard delete only if never used in a run, else soft
  PUT    /sop/templates/{id}/items/reorder  accepts an ordered id array, rewrites sort_order in one tx
  POST   /sop/templates/{id}/duplicate
  GET    /sop/assignments?outlet_id=
  POST   /sop/assignments                   template × outlet × weekdays × due_time_local × grace
  PATCH  /sop/assignments/{id}
  DELETE /sop/assignments/{id}

VERSIONING RULE — implement carefully:
Any change to a template's item set or to an item's title/instruction/is_critical/requires_photo/
value bounds increments checklist_templates.version. Runs snapshot template_version at creation.
Historical runs must always render against the item definitions that were live when they ran, so
never hard-delete an item that has run_items pointing at it — soft delete it and keep it renderable.
Editing a template does NOT retroactively change today's already-started runs.

FRONTEND — src/features/sop/templates/:
  /app/sop/templates          list grouped by category, with item count, assigned-outlet count,
                              version, active toggle
  /app/sop/templates/:id      builder:
                                - header: name, category, frequency, day_part, description
                                - item list with drag-to-reorder (dnd-kit), inline edit
                                - per item: title, instruction, requires_photo, requires_value +
                                  type + min/max + unit, is_critical (red badge), allow_na
                                - a live "what staff will see" mobile preview pane on the right
                                - an unsaved-changes guard
  /app/sop/assignments        matrix view: templates as rows, outlets as columns, cell shows the
                              due time or "—"; click a cell to assign/edit/remove

Warn (do not block) when a template exceeds 15 items or when more than half its items are marked
critical — both are the known failure modes of checklist programmes.

Tests: reorder is transactional and leaves no duplicate sort_order; version bumps on a material edit
but not on a description-only edit of the template itself; an item used by a run cannot be hard-deleted.

ACCEPTANCE: the 6 seeded templates open in the builder, can be reordered, edited, versioned, and
assigned to both outlets.
````

---

## P5 — Checklist runner (mobile-first, offline-tolerant)

````
Read CLAUDE.md and docs/STAGE1_SPEC.md sections 4.1 and 4.2. This is the most important screen in
Stage 1 — staff use it on a phone, standing up, on bad wifi, at 1am. Build for that.

BACKEND — app/domains/sop/ (runs portion):
  GET  /sop/runs/today?outlet_id=          runs for the current business_date, scoped to the caller's
                                            role: staff see only runs whose assigned_role matches theirs
  GET  /sop/runs/{id}                       run + snapshot item definitions + current item results
  POST /sop/runs/{id}/start                 pending → in_progress, stamps started_by/started_at.
                                            Idempotent: starting an in_progress run returns it unchanged.
  PATCH /sop/runs/{id}/items/{itemId}       result, value_numeric/value_text, note.
                                            Computes out_of_range against the snapshot bounds.
                                            Accepts an Idempotency-Key header.
  POST /sop/runs/{id}/photo-url             body: { item_id, content_type, byte_size }.
                                            Validates the item requires a photo, size ≤ 5MB, type is
                                            image/jpeg|png|webp. Returns a signed Supabase Storage
                                            upload URL for
                                            sop-photos/{outlet_id}/{business_date}/{run_id}/{item_id}.jpg
                                            plus the object path.
  POST /sop/runs/{id}/photo-confirm         body: { item_id, path }. Verifies the object exists and its
                                            size, then writes photo_path/photo_uploaded_at/photo_bytes.
                                            Photo metadata is ONLY written after the object is confirmed.
  POST /sop/runs/{id}/submit                body: { geo_lat?, geo_lng? }.
                                            Validates every non-allow_na item has a result and every
                                            requires_photo item has a confirmed photo — otherwise 422
                                            listing the offending item ids.
                                            Computes score_pct and critical_fail_count using
                                            app/core/scoring.py, sets is_late/minutes_late from due_at +
                                            grace, sets geo_ok, creates a sop_exception for every
                                            critical fail, then status = submitted.

app/core/scoring.py — pure functions, table-driven unit tests:
  item_weight(is_critical) -> 3 | 1
  run_score(items) -> percentage over applicable (non-na) weight
  Edge cases tested: all n/a (score is None, not 0 or a divide-by-zero), zero items, all critical.

FRONTEND — src/features/sop/runner/ under /floor:
  /floor                       today's runs as large cards: template name, due time, a status pill,
                               progress "7/14". Overdue cards outlined red. Nothing else on the screen.
  /floor/run/:id               one item per screen, swipe or big Next button:
                                 - item title large, instruction below, reference photo if present
                                 - PASS / FAIL / N-A as three full-width thumb-reach buttons
                                 - camera input when requires_photo:
                                   <input type="file" accept="image/*" capture="environment">
                                   → resize client-side to max 1600px longest edge, JPEG q80,
                                     via canvas → request signed URL → PUT to Storage → confirm
                                 - number/temperature keypad when requires_value, with immediate
                                   out-of-range warning against the bounds
                                 - note field, optional, required when result is FAIL
                                 - sticky progress bar at top, "back" always available
  /floor/run/:id/review        summary list, all fails highlighted, Submit button.
                               Requests geolocation once at submit; if denied, submit anyway and let
                               the backend flag it. Never block on a permission the staff can't grant.

OFFLINE (this is required, not optional — in-store wifi drops):
  - A Zustand store persisted to IndexedDB holds the active run draft: every item result, note, value,
    and any photo blob not yet uploaded.
  - Item updates and photo uploads queue when offline and drain automatically on reconnect, in order,
    with exponential backoff and the Idempotency-Key so a retry can't double-write.
  - A persistent banner shows "3 changes waiting to sync" with a manual retry.
  - Submit is disabled while the queue is non-empty, with a clear explanation of why.
  - Reloading the page mid-run restores exactly where the user was.

Accessibility and ergonomics: minimum 48px tap targets, no hover-only affordances, works one-handed,
readable at arm's length in a bright kitchen (high contrast, 16px+ body).

Tests: vitest for the offline queue (enqueue, drain, retry, idempotency), the image resizer, and the
out-of-range check. Pytest for submit validation (missing photo → 422 with item ids), scoring
correctness, late calculation across the midnight boundary, and exception creation on critical fail.

ACCEPTANCE: on a throttled/offline connection, a seeded staff user can complete a 14-item closing
checklist with photos, kill the tab mid-run, reopen, resume, reconnect, and submit — with the score
and any exceptions correct on the backend.
````

---

## P6 — Manager review and approval queue

````
Read CLAUDE.md. Build the manager-side review of submitted runs. /app only.

BACKEND — extend app/domains/sop/:
  GET  /sop/runs?outlet_id=&status=&from=&to=&template_id=   paginated, scoped by outlet access
  GET  /sop/runs/{id}/detail        run + items + snapshot definitions + signed photo view URLs
                                    (short-lived, generated per request — never store public URLs)
  POST /sop/runs/{id}/approve       403 if caller == submitted_by (also enforced by the DB CHECK).
                                    Sets approved_by/approved_at, locks the run: all further item
                                    mutations return 409.
  POST /sop/runs/{id}/reject        body: { reason, item_ids[] }. Returns the run to in_progress,
                                    clears results on the named items only, stores rejection_reason.
  GET  /sop/exceptions?outlet_id=&status=&severity=
  POST /sop/exceptions/{id}/acknowledge
  POST /sop/exceptions/{id}/resolve  body: { resolution_note, photo_path? }
  POST /sop/exceptions/{id}/waive    owner/ops_manager only, requires a reason
Approve, reject, resolve and waive all write audit_log rows.

Track review depth: record which photos the approver actually opened (a lightweight
`run_review_views` table or a jsonb column on the run). This feeds the "approved without looking"
signal in the digest. Do not surface it as a punishment metric in the UI — it is an owner-level check.

FRONTEND — src/features/sop/review/:
  /app/sop/review        queue of submitted runs: outlet, template, business date, submitted by,
                         score, critical fails, integrity flag count, age. Sorted oldest first.
                         Bulk-approve is deliberately NOT offered.
  /app/sop/review/:id    two-pane: item list on the left with pass/fail/value/note; photo viewer on
                         the right with a lightbox, zoom, and the reference photo side by side.
                         Integrity flags render as explicit red chips with plain-language tooltips
                         ("This photo matches one submitted on 22 Aug for the same item").
                         Approve (disabled with an explanation if the caller submitted it) and
                         Reject (opens a dialog requiring a reason and selecting the failed items).
  /app/sop/exceptions    exception board grouped by severity, with age-in-hours, assignee, and
                         resolve/acknowledge/waive actions. Anything high-severity and older than
                         48h is visually escalated.

Tests: submitter cannot approve own run (403 at the API and disabled in the UI); approved runs reject
further item edits with 409; rejection clears only the named items; signed photo URLs expire.

ACCEPTANCE: a full submit → review → reject → fix → resubmit → approve cycle works, with every step
in audit_log, and the approve button is genuinely unusable by the submitter.
````

---

## P7 — Integrity engine and scheduled jobs

````
Read CLAUDE.md and docs/STAGE1_SPEC.md section 4.2. Build the photo integrity checks and the
scheduled jobs. Backend-heavy; small UI surface.

INTEGRITY — app/domains/sop/integrity.py, run at photo-confirm and again at submit:

1. duplicate_photo — compute imagehash.phash of the uploaded image (download from Storage, PIL,
   store the 16-char hex in checklist_run_items.photo_phash). Compare against photos for the same
   (outlet_id, template_item_id) from the previous 30 days. Hamming distance <= 5 → flag, and record
   which run it matched so the UI can show it.
2. burst_upload — at submit, if more than 80% of the run's photos were uploaded within the final
   3 minutes before submitted_at, or if the whole run took under 90 seconds for 10+ items → flag.
3. out_of_geofence — haversine(submit_geo, outlet_geo) > outlet.geofence_radius_m → flag,
   geo_ok = false. Missing geolocation is NOT a flag (it is often a device permission the staff
   member cannot change) — record geo_ok = null and count it separately.
4. late — submitted_at > due_at + grace_minutes → is_late, minutes_late.
5. stale_capture — photo_uploaded_at outside [started_at, submitted_at] → flag.

Flags never block submission. They are written to checklist_run_items.integrity_flags and counted
into checklist_runs.integrity_flag_count.

Photo processing must not run inside the request. Use a FastAPI BackgroundTask (Stage 1, single
instance) writing to a `job_runs` table so a failure is visible rather than silent.

SCHEDULED JOBS — app/jobs/ using APScheduler, all recording start/finish/error in job_runs:

1. materialise_runs — 05:00 Asia/Kolkata daily. For every active outlet × active assignment whose
   active_weekdays includes today, create a checklist_runs row (status=pending) for today's
   business_date with due_at computed from due_time_local in the outlet's timezone.
   Idempotent — safe to re-run, unique (assignment_id, business_date, day_part) protects it.
   Include an admin endpoint POST /sop/runs/materialise to trigger it manually.
2. mark_missed — every 15 minutes. Runs still 'pending' past due_at + grace_minutes become 'missed'
   and create a medium-severity sop_exception.
3. daily_digest — 09:00 Asia/Kolkata. Per outlet: yesterday's completion rate, on-time rate, mean
   score, critical fails, open exceptions, integrity flags, and a random 10% sample of approved runs
   flagged for owner spot-check. Rendered as HTML email to owner + ops_manager + that outlet's
   manager. Use a pluggable Notifier interface with an EmailNotifier implementation and a
   LogNotifier for dev — a WhatsApp notifier will be added in Stage 2, so do not hardcode email.

UI: /app/settings/jobs — a small table of the last 50 job_runs with status, duration, and error,
plus a manual "run now" button per job for owner only.

Tests: materialisation is idempotent and respects weekdays and outlet timezone; a run due 00:30
belongs to the correct business_date; pHash catches a re-uploaded identical image and a lightly
recompressed one, but does not flag two genuinely different photos of the same station; missed
marking respects grace.

ACCEPTANCE: with the clock advanced, the 05:00 job creates exactly the right runs for both outlets,
re-running it creates no duplicates, an unstarted run flips to missed after grace, and re-uploading
yesterday's photo produces a visible duplicate_photo flag in the review screen.
````

---

## P8 — Compliance dashboard

````
Read CLAUDE.md and docs/STAGE1_SPEC.md sections 4.3 and 5. Build the outlet compliance dashboard.
This is the first pillar of the eventual Outlet Health Score — structure it so the other three
pillars can be added as cards without a rewrite.

BACKEND — app/domains/sop/analytics.py + endpoints:
  GET /sop/analytics/summary?outlet_id=&from=&to=
      -> { sop_score, completion_rate, on_time_rate, mean_run_score, critical_fails,
           open_exceptions, integrity_flags, runs_scheduled, runs_approved, band }
  GET /sop/analytics/trend?outlet_id=&days=28    -> daily series of sop_score and completion_rate
  GET /sop/analytics/by-outlet?from=&to=         -> one row per accessible outlet, for comparison
  GET /sop/analytics/by-template?outlet_id=      -> which templates fail most
  GET /sop/analytics/worst-items?outlet_id=&limit=10
      -> the specific checklist items that fail most often, with fail rate

Scoring lives in app/core/scoring.py as pure functions, implementing spec section 4.3 exactly:
  outlet_sop_score = 0.50 * mean_run_score + 0.30 * completion_rate + 0.20 * on_time_rate
                     − 2 per open high-severity exception older than 48h
                     − 1 per integrity flag per 10 runs, clamped 0–100
  Bands: >= 90 green, 75–89 amber, < 75 red.
  A single unresolved critical failure caps the outlet at amber regardless of the arithmetic.
Table-driven unit tests including the cap rule and the clamps.

FRONTEND — src/features/dashboard/ at /app (the landing page for management roles):
  Row 1 — Outlet Health Score card per accessible outlet. Big 0–100 number, colour band, delta vs
          prior period, and a one-line "worst component" callout. In Stage 1 the score IS the SOP
          score; the card must already render as a four-pillar breakdown with Sales / Inventory /
          Guest shown as greyed "Coming in Stage 2" segments, so the layout does not change later.
  Row 2 — 28-day trend line (SOP score) + a completion vs on-time dual bar.
  Row 3 — Outlet comparison table (hidden for outlet_manager, who instead sees their rank only).
  Row 4 — Open exceptions list (high severity first, age in hours) + "Top failing checklist items"
          with fail rate, which is the actionable one — it tells you what is actually broken.
  Date range selector: Last 7 / 28 / 90 days / custom. All ranges are business_date based.

Charts: Recharts. Palette — green #2f9e5f, amber #e0a020, red #ee3345, blue #326fb7, ink #231f20 on
white. No gradients, no 3D, no dual y-axes. Every chart has an explicit empty state and a loading
skeleton. Numbers are formatted Indian-style (₹1,07,500 / lakh grouping) via one formatter in lib/.

Tests: scoring functions against hand-computed fixtures; outlet_manager sees rank but not other
outlets' detail; empty-period responses return zeroed structures, not nulls that crash the UI.

ACCEPTANCE: with 4 weeks of seeded run data across both outlets, the dashboard renders correct
scores that match the hand-computed fixtures, and the four-pillar card layout is already in place.
````

---

## P9 — Sales ingestion skeleton

````
Read CLAUDE.md and docs/STAGE1_SPEC.md section 3.4. Build Petpooja export ingestion. Storage and a
raw view only — the sales dashboard is Stage 2. The point of this epic is to get the ingestion
ABSTRACTION right so a Petpooja API sync can be dropped in later without touching anything downstream.

BACKEND — app/integrations/sales/:
  base.py       class SalesSource(Protocol):
                    def fetch(self, params) -> RawSalesBatch
                class SalesParser(Protocol):
                    def parse(self, raw: bytes, outlet_id) -> ParsedSalesBatch
                Dataclasses: RawSalesBatch, ParsedSalesBatch(orders[], items[], warnings[])
  file_source.py       reads an uploaded object from Supabase Storage
  petpooja_orders.py   parser for "Orders Master Report" XLSX (openpyxl)
  petpooja_items.py    parser for "Item Sale Report Hourly Wise" XLSX
  api_source.py        a stub class raising NotImplementedError with a docstring listing exactly
                       what credentials and endpoints a future implementation needs

Parsers must be tolerant and explicit:
  - Header row detection (Petpooja exports carry title/metadata rows above the header)
  - Column name mapping via a versioned dict, so a rename is a one-line change; unknown columns
    are recorded as warnings, never silently dropped
  - Currency strings ("₹1,075.00", "1075", "1,075") → integer paise
  - Dates + times → timestamptz Asia/Kolkata, then business_date via the shared function
  - Phone numbers → salted SHA-256 into customer_phone_hash; the raw number is never persisted
  - Channel mapped to sales_channel; anything unrecognised becomes a warning, not a crash
  - Every parse returns a warnings list that is stored on data_uploads and shown to the user

Endpoints — app/domains/sales/:
  POST /sales/uploads/url        { outlet_id, filename, source, content_type } → signed upload URL
  POST /sales/uploads/confirm    { outlet_id, path, source } → computes SHA-256; if the hash already
                                 exists returns 409 with the existing upload (idempotency); otherwise
                                 creates data_uploads(status=received) and queues parsing
  GET  /sales/uploads            list with status, row counts, period, warnings, uploaded by
  GET  /sales/uploads/{id}       detail incl. full warnings and error_detail
  POST /sales/uploads/{id}/reparse   owner/ops_manager, re-runs the parser (deletes and rewrites that
                                 upload's rows in one transaction)
  DELETE /sales/uploads/{id}     owner only, removes the upload and its derived rows transactionally
  GET  /sales/orders             paginated, filter outlet_id + business_date range
  GET  /sales/orders/{id}        with line items

Parsing runs as a background task writing to job_runs, with data_uploads.status progressing
received → parsing → parsed | failed. Writes are transactional per upload: a partial parse leaves
no rows behind.

FRONTEND — src/features/sales/:
  /app/sales/uploads    dropzone (XLSX/CSV), upload history table with status pills, warning count,
                        row count, period covered; clicking a row opens a detail sheet showing
                        warnings in plain language and a 20-row preview of what was parsed
  /app/sales/orders     raw orders data table — business date, bill no, channel, covers, net,
                        payment mode — with date-range and outlet filters and CSV export

Tests: use the real Petpooja exports in this project as fixtures — Orders Master Report
(17 Jul – 25 Aug 2026, 452 bills) and Item Sale Report Hourly Wise (25 Aug 2026). Assert the parsed
totals reconcile to ₹4,86,076 net across 452 orders, that a bill timestamped 00:45 on 23 Aug lands
on business_date 2026-08-22, that re-uploading the identical file returns 409 and creates no
duplicate rows, and that a file with a renamed column produces a warning rather than an exception.

ACCEPTANCE: both real exports ingest cleanly, totals reconcile to the known figures, re-upload is
idempotent, and swapping FileSource for a future ApiSource requires no change outside
app/integrations/sales/.
````

---

## P10 — Hardening pass before Stage 2

````
Read CLAUDE.md. No new features. Harden what exists.

1. SECURITY REVIEW — walk every endpoint and confirm: authentication required, correct role check,
   correct outlet scoping, no IDOR (fetching by id must re-verify outlet access, not trust the id),
   no mass-assignment through pydantic models, signed URLs are short-lived and scoped to one object.
   Write a docs/SECURITY_REVIEW.md table: endpoint, roles, scoping mechanism, test that proves it.
2. RLS VERIFICATION — a pytest that connects as a plain `authenticated` role with a staff user's JWT
   and attempts to select from every table for a different outlet. Every attempt must return zero
   rows. This proves the defence-in-depth layer actually works rather than merely existing.
3. N+1 AND INDEX AUDIT — log slow queries in dev, EXPLAIN the dashboard and review-queue endpoints,
   add any missing indexes as a new migration. Document each in the migration comment.
4. ERROR AND EMPTY STATES — every list, chart, and detail screen gets a designed loading skeleton,
   empty state with a next action, and error state with retry. No raw error strings in the UI.
5. SEED A REALISTIC DATASET — a script generating 8 weeks of runs across both outlets with a
   believable mix: ~85% completion, some late, a handful of critical fails, a few duplicate photos.
   This is what makes the dashboard reviewable and demoable.
6. RUNBOOK — docs/RUNBOOK.md: environment variables, local setup, migration procedure, how to add an
   outlet, how to add a role, how to change a scheduled job time, what to do when the 05:00
   materialisation fails, how to restore a rejected run.
7. STAGE 2 READINESS NOTE — docs/STAGE2_NOTES.md listing every place Stage 2 will hook in
   (health score pillars, inventory tables, requisition engine, forecast service, WhatsApp notifier)
   and confirming each hook point exists.

ACCEPTANCE: full test suite green; the RLS cross-outlet test passes; the dashboard is populated and
demoable with realistic data; a new engineer can go from clone to running app using only RUNBOOK.md.
````

---

## Stage 2 preview — do not start until Stage 1 is in daily use

In order: inventory items + recipes + par levels → stock count upload with LLM extraction and
human-confirmed item mapping → deterministic requisition engine → statistical anomaly detection with
LLM narration → sales dashboard → the full four-pillar Outlet Health Score → seasonal-naive forecast
→ WhatsApp notifier.

The one rule to carry into Stage 2, restated because it is the thing most likely to be violated:
**the LLM parses and explains; deterministic code decides.** Every number a manager acts on must
trace back to a formula they can be shown.
