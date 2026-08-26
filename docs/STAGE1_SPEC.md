# AKIRA Ops Suite — Stage 1 Specification

**Version** 1.0 · **Date** 26 Aug 2026 · **Owner** Ano
**Codename** `akira-ops`

---

## 0. What this is

An internal, multi-outlet operations platform for AKIRA. Three long-term pillars:

1. **Compliance** — every outlet demonstrably runs the same SOPs, with photographic proof.
2. **Supply** — stock counts in, next-day requisitions out, anomalies flagged.
3. **Visibility** — one outlet health score per outlet, plus sales analytics and forecasting.

**Stage 1 ships pillar 1 plus the foundation everything else sits on.** Pillars 2 and 3 are specified here at the level needed to make Stage 1's data model correct, but are not built yet.

### Stage decisions locked

| Decision | Choice |
|---|---|
| Stage 1 scope | Foundation (auth, roles, outlets, users) + full SOP compliance module + sales ingestion skeleton |
| Sales data | Manual Petpooja XLSX upload now; ingestion layer abstracted so an API sync swaps in later |
| Outlet model | Multi-outlet from day one — one outlet today, no rewrite at outlet 2 |
| Stack | Vite + React + TypeScript · shadcn/ui · FastAPI · Supabase (Postgres, Auth, Storage) |

---

## 1. Architecture

### 1.1 Repo layout

```
akira-ops/
├── CLAUDE.md                  # project rules for Claude Code — read first, always
├── docker-compose.yml         # local postgres/supabase + api
├── apps/
│   ├── web/                   # Vite + React 19 + TS + Tailwind + shadcn/ui
│   │   ├── src/
│   │   │   ├── app/           # route tree (TanStack Router or React Router v7)
│   │   │   ├── features/      # feature-sliced: auth/ outlets/ sop/ sales/ admin/
│   │   │   ├── components/ui/ # shadcn generated primitives — never hand-edit
│   │   │   ├── lib/           # api client, supabase client, formatters, dates
│   │   │   └── types/api.ts   # GENERATED from OpenAPI — do not hand-edit
│   └── api/                   # FastAPI
│       ├── app/
│       │   ├── core/          # config, security, deps, business_date, errors
│       │   ├── domains/       # outlets/ users/ sop/ sales/ — router+service+schema+repo
│       │   ├── integrations/  # supabase, storage, petpooja/
│       │   ├── jobs/          # scheduled tasks
│       │   └── main.py
├── supabase/
│   ├── migrations/            # numbered SQL migrations — source of truth for schema
│   └── seed/                  # seed outlets, roles, starter SOP templates
└── packages/
    └── shared/                # enums + constants mirrored to TS and Python
```

### 1.2 The Supabase / FastAPI boundary (read this twice)

The most common way this stack goes wrong is having two half-backends. One rule:

> **The frontend talks to FastAPI for all business data. It talks to Supabase directly for exactly three things: the auth session, direct-to-storage file uploads via signed URLs that FastAPI issued, and realtime subscriptions.**

| Concern | Owner |
|---|---|
| User identity, JWT issuance, password reset, magic link | Supabase Auth |
| Authorisation (who can do what) | FastAPI, enforced in a dependency; RLS as defence-in-depth |
| All reads/writes of application data | FastAPI → Postgres via SQLAlchemy/asyncpg |
| File bytes | Browser → Supabase Storage, using a signed upload URL minted by FastAPI |
| File metadata rows | FastAPI, written only after the upload confirms |
| Scheduled jobs, AI calls, XLSX parsing, scoring | FastAPI |

FastAPI connects to Postgres with the **service role / direct DB connection** and enforces authz in code. RLS is still enabled with restrictive policies on every table, so that a leaked anon key or a future direct-read path cannot exfiltrate another outlet's data.

### 1.3 Auth flow

1. Browser signs in via Supabase Auth → receives JWT.
2. Every FastAPI request carries `Authorization: Bearer <supabase_jwt>`.
3. FastAPI verifies the JWT signature against Supabase's JWKS, extracts `sub` (= `profiles.id`).
4. A `CurrentUser` dependency loads the profile plus their outlet memberships, cached per request.
5. Route guards: `require_role("owner", "ops_manager")` and `require_outlet_access(outlet_id)`.

No custom password handling anywhere. No session cookies. Refresh handled by the Supabase JS client.

---

## 2. Roles and access

### 2.1 Roles

| Role | Who | Scope |
|---|---|---|
| `owner` | You, co-founders | All outlets. Everything, including user management and financials. |
| `ops_manager` | Head of operations / area manager | All outlets. All ops, approves checklists and requisitions, authors SOP templates. Cannot change roles at or above `ops_manager`, cannot see cost/P&L in later stages unless granted. |
| `outlet_manager` | Outlet-level manager | Their assigned outlet(s) only. Approves checklists, submits requisitions, sees own outlet KPIs and outlet-vs-network rank (not other outlets' detail). |
| `shift_lead` | Senior on shift | Their outlet. Runs and submits checklists, submits stock counts. **Cannot approve a run they submitted.** |
| `staff` | Floor / kitchen | Their outlet. Sees only checklists assigned to them today. No dashboards, no other people's submissions. |

Stage 2 adds `auditor` (read-only, all outlets, no user management).

### 2.2 Permission matrix

| Capability | owner | ops_manager | outlet_manager | shift_lead | staff |
|---|:--:|:--:|:--:|:--:|:--:|
| Create/edit outlets | ✅ | — | — | — | — |
| Invite users, assign roles | ✅ | ✅ (below own) | ✅ (`shift_lead`, `staff`, own outlet) | — | — |
| Create/edit SOP templates | ✅ | ✅ | — | — | — |
| Assign templates to outlets | ✅ | ✅ | — | — | — |
| Start / submit a checklist run | ✅ | ✅ | ✅ | ✅ | ✅ (assigned only) |
| Approve / reject a run | ✅ | ✅ | ✅ | — | — |
| View own outlet compliance dashboard | ✅ | ✅ | ✅ | ✅ (read) | — |
| View all-outlet comparison | ✅ | ✅ | rank only | — | — |
| Upload sales exports | ✅ | ✅ | ✅ | — | — |
| View sales figures | ✅ | ✅ | own outlet | — | — |
| View audit log | ✅ | ✅ | own outlet | — | — |

**Separation of duties rule:** `submitted_by != approved_by` is enforced at the database level with a CHECK constraint, not just in the UI. Without it the whole compliance system is theatre.

---

## 3. Core data model

Conventions applied to every table: `id uuid primary key default gen_random_uuid()`, `created_at timestamptz not null default now()`, `updated_at timestamptz`, `deleted_at timestamptz` (soft delete), and RLS enabled.

### 3.1 Cross-cutting conventions — non-negotiable

**Business date.** AKIRA already books ~8% of revenue after midnight and is extending to 1am+. A trading night that starts 18:00 Saturday and ends 01:30 Sunday is **one** business day. Therefore:

```sql
-- business_date = the calendar date of the trading day, rolling over at 05:00 IST
create or replace function business_date(ts timestamptz)
returns date language sql immutable as $$
  select (ts at time zone 'Asia/Kolkata' - interval '5 hours')::date
$$;
```

Every dated row stores `business_date date not null`. Every report groups by it. Never group by `created_at::date`. Getting this wrong silently corrupts every weekend number in the system.

**Time.** All timestamps `timestamptz`, stored UTC, rendered `Asia/Kolkata`. Outlet-local scheduled times (`due_time_local`) stored as `time` plus the outlet's `timezone`.

**Money.** Integer paise. Never float, never `numeric` for currency in the API layer. `amount_paise bigint`. Format at the edge only.

**Enums.** Postgres enums for closed sets (`user_role`, `run_status`, `item_result`). Mirrored in `packages/shared` and re-exported to both TS and Python so a value can never drift.

**Audit.** Every mutating service call writes to `audit_log`. No exceptions for "small" edits — an SOP template silently edited to remove a step is exactly the event you will need to reconstruct.

### 3.2 Foundation tables

```
outlets
  id, code (unique, e.g. 'AKR-NT01'), name, address_line, city, geo_lat, geo_lng,
  geofence_radius_m (default 150), timezone (default 'Asia/Kolkata'),
  opened_on, is_active, created_at, updated_at, deleted_at

profiles                        -- 1:1 with auth.users
  id (= auth.users.id), full_name, phone, employee_code,
  global_role user_role,        -- owner | ops_manager | outlet_manager | shift_lead | staff
  is_active, last_seen_at, created_at, updated_at, deleted_at

outlet_members                  -- a user can serve multiple outlets
  id, outlet_id → outlets, profile_id → profiles,
  role_at_outlet user_role, is_primary bool,
  unique (outlet_id, profile_id)

audit_log
  id, actor_profile_id, outlet_id (nullable), entity_table, entity_id,
  action ('create'|'update'|'delete'|'approve'|'reject'|'login'),
  before jsonb, after jsonb, ip inet, user_agent text, at timestamptz
```

### 3.3 SOP module tables

```
sop_categories
  id, key ('opening'|'closing'|'cleaning'|'food_safety'|'maintenance'|'inventory'),
  label, sort_order, icon

checklist_templates
  id, category_id → sop_categories, name, description,
  frequency ('per_shift'|'daily'|'weekly'|'monthly'),
  day_part ('opening'|'mid'|'closing'|'any'),
  version int not null default 1,       -- bump on any item change; runs snapshot the version
  is_active, created_by, created_at, updated_at

checklist_template_items
  id, template_id → checklist_templates, sort_order,
  title, instruction, reference_photo_path,
  requires_photo bool default false,
  requires_value bool default false,
  value_type ('number'|'text'|'temperature_c'|'time'),
  value_min numeric, value_max numeric, value_unit,
  is_critical bool default false,       -- weight 3 in scoring; a fail escalates immediately
  allow_na bool default false,
  unique (template_id, sort_order)

checklist_assignments                   -- which outlet runs which template, when, by whom
  id, template_id, outlet_id,
  assigned_role user_role,              -- who is expected to complete it
  active_weekdays int[] default '{0,1,2,3,4,5,6}',
  due_time_local time not null,
  grace_minutes int default 30,
  is_active

checklist_runs
  id, assignment_id, template_id, template_version int,
  outlet_id, business_date date, day_part,
  status ('pending'|'in_progress'|'submitted'|'approved'|'rejected'|'missed'),
  started_by, started_at, submitted_by, submitted_at,
  approved_by, approved_at, rejection_reason,
  due_at timestamptz, is_late bool, minutes_late int,
  score_pct numeric(5,2), critical_fail_count int,
  integrity_flag_count int, submit_geo_lat, submit_geo_lng, geo_ok bool,
  unique (assignment_id, business_date, day_part),
  check (approved_by is null or approved_by <> submitted_by)   -- separation of duties

checklist_run_items
  id, run_id → checklist_runs, template_item_id, sort_order,
  result ('pass'|'fail'|'na'|'pending'),
  value_numeric, value_text, out_of_range bool,
  note text,
  photo_path text, photo_uploaded_at, photo_bytes, photo_phash text,
  integrity_flags text[],               -- 'duplicate_photo','burst_upload','out_of_geofence','late'
  completed_at

sop_exceptions                          -- every critical fail becomes a tracked exception
  id, run_item_id, outlet_id, business_date, severity ('high'|'medium'|'low'),
  title, detail, photo_path,
  status ('open'|'acknowledged'|'resolved'|'waived'),
  assigned_to, resolved_by, resolved_at, resolution_note
```

### 3.4 Sales ingestion skeleton (Stage 1: ingest + store only)

```
data_uploads
  id, outlet_id, uploaded_by, source ('petpooja_orders'|'petpooja_items'|'manual'),
  original_filename, storage_path, file_sha256 (unique — idempotency),
  period_start date, period_end date,
  status ('received'|'parsing'|'parsed'|'failed'), row_count, error_detail, created_at

sales_orders                            -- one row per bill, from Orders Master Report
  id, outlet_id, upload_id, external_bill_no, business_date, ordered_at timestamptz,
  channel ('dine_in'|'pickup'|'delivery'), covers int,
  gross_paise, discount_paise, tax_paise, net_paise,
  payment_mode, customer_phone_hash, table_no,
  unique (outlet_id, external_bill_no)

sales_order_items                       -- from Item Sale Report
  id, order_id, outlet_id, business_date, item_name, item_category,
  qty numeric, unit_price_paise, line_net_paise
```

`customer_phone_hash` — store a salted SHA-256, never the raw number, unless and until there is a stated retention/consent policy. Phone capture is a known target (31% today, goal 80%+) so this table will grow into the CRM source of truth; get the privacy shape right before it does.

---

## 4. SOP compliance module — behaviour

### 4.1 The daily loop

```
05:00 IST  scheduler materialises today's runs (status=pending) from checklist_assignments
           for every active outlet × active template × matching weekday

on shift    staff opens app → sees only their outlet's pending runs assigned to their role
            → taps a run → status=in_progress, started_at stamped
            → works item by item: pass / fail / n-a, photo where required, value where required
            → submits → status=submitted, score computed, integrity checks run

manager     sees submit queue → reviews photos → approve or reject with reason
            approve → status=approved (locked, immutable)
            reject  → back to in_progress with the rejection reason visible on the offending items

due_at + grace passes with status=pending → status=missed, alert fires
critical item failed → sop_exception row created, alert fires immediately
```

### 4.2 Photo integrity — assume staff will game it

A photo-based SOP system without integrity checks becomes a photo-reuse system within three weeks. Stage 1 ships all five:

| Check | Implementation | Flag |
|---|---|---|
| Re-used photo | Perceptual hash (`imagehash.phash`) on upload; compare against the last 30 days of photos for the same `(outlet_id, template_item_id)`; Hamming distance ≤ 5 = match | `duplicate_photo` |
| Batch faking at end of shift | All photos in a run uploaded within a window far shorter than the run's expected duration, or > 80% uploaded in the final 3 minutes | `burst_upload` |
| Done off-site | Browser geolocation at submit vs outlet `geo_lat/lng` + `geofence_radius_m` | `out_of_geofence` |
| Done late | `submitted_at > due_at + grace_minutes` | `late` |
| Gallery pick instead of live capture | `<input type="file" accept="image/*" capture="environment">`, plus server-side check that `photo_uploaded_at` falls between `started_at` and `submitted_at` | `stale_capture` |

Flags do **not** block submission — they surface on the manager's review screen and count against the outlet's integrity score. Blocking creates workarounds; visibility creates accountability.

Storage path: `sop-photos/{outlet_id}/{business_date}/{run_id}/{run_item_id}.jpg`
Images resized client-side to max 1600px longest edge, JPEG q80, before upload. A 40-item checklist at full phone resolution is 200MB/day/outlet; at q80/1600px it is ~8MB.

### 4.3 Scoring

```
item_weight        = 3 if is_critical else 1
applicable_weight  = Σ item_weight where result != 'na'
earned_weight      = Σ item_weight where result == 'pass'
run.score_pct      = 100 × earned_weight / applicable_weight

outlet SOP score (period) =
      0.50 × mean(run.score_pct for approved runs)
    + 0.30 × completion_rate      -- runs approved ÷ runs scheduled
    + 0.20 × on_time_rate         -- runs submitted before due_at+grace ÷ runs submitted
    − 2 points per open high-severity exception older than 48h
    − 1 point per integrity flag per 10 runs
    (clamped 0–100)
```

Bands: **≥ 90 green · 75–89 amber · < 75 red.** A single unresolved critical failure caps the outlet at amber regardless of arithmetic.

### 4.4 Starter SOP templates to seed

Seed these so the system is usable on day one rather than being an empty template builder:

1. **Opening — Kitchen** (pre-service): broth temp log (`temperature_c`, 75–95), noodle prep count, walk-in fridge temp (0–5), station sanitiser prepared (photo), gas/fryer check, prep list posted (photo)
2. **Opening — Floor**: dining area swept & mopped (photo), tables & seats wiped (photo), washroom check (photo), menu cards clean, POS opened, music/lighting set
3. **Closing — Kitchen**: fryer oil filtered/logged, all surfaces sanitised (photo), floor drains cleared (photo), fridge temps logged, waste out, gas off (photo), FIFO labels checked
4. **Closing — Floor**: tables reset (photo), washroom final (photo), cash drawer reconciled, day-end POS report filed (photo), lights/AC off, shutters locked (photo)
5. **Weekly Deep Clean** (Mon, low-cover day): exhaust hood degrease (photo), fridge shelves out & washed (photo), pest-control sighting log, ice machine, storeroom FIFO audit
6. **Food Safety Daily**: hot-holding temps, cold-holding temps, staff grooming & handwash check (photo), allergen station separation (photo), oil TPM reading

---

## 5. Outlet Health Score — the target model (built Stage 2, designed now)

One number per outlet, 0–100, four weighted pillars. Stage 1 delivers pillar 2 only; the schema above already supports it.

| Pillar | Weight | Component KPIs |
|---|:--:|---|
| **Sales & growth** | 30 | Net sales vs target · WoW growth % · AOV · covers/trading day · weekday (Mon–Wed) share of sales |
| **SOP compliance** | 30 | Run score mean · completion rate · on-time rate · open critical exceptions · integrity flags |
| **Inventory discipline** | 25 | Theoretical vs actual variance % · wastage % of COGS · stockout incidents · requisition accuracy (requested vs consumed) |
| **Guest & throughput** | 15 | Google rating delta · repeat-customer rate · phone-capture rate · peak-hour (20:00–23:00) table turns |

Each component is normalised 0–100 against a target band, the pillar is the weighted mean of its components, health = Σ (pillar × weight). Display: big number, colour band, sparkline vs prior 28 days, and the single worst-performing component named explicitly ("dragged down by: on-time rate 62%").

**Baseline targets from AKIRA's actual 17 Jul – 25 Aug data** — use these as seed targets, review monthly:

| Metric | Current | Stage-2 target band |
|---|---|---|
| Net sales / trading day | ₹12,791 (Aug ₹14,014) | green ≥ ₹18,000 · amber ₹13–18k · red < ₹13k |
| AOV | ₹1,075 | green ≥ ₹1,150 (AOV is stable — growth is coming from bill count, so weight this low) |
| Orders / day | ~15.0 last full week | green ≥ 20 |
| Mon–Wed share of sales | ~40% | green ≥ 45% (the known weak flank) |
| Phone capture rate | 31% | green ≥ 80% |
| Discount % of gross | 0.8% | keep < 3% |
| Peak-hour share (20:00–23:00) | 52.9% of revenue | monitor for truncation, not a target |

### 5.1 Forecasting — Stage 3, and start boring

AKIRA has ~6 weeks and 452 bills of history. Prophet/ARIMA/an LLM will overfit noise and produce confident nonsense.

**Baseline (build this first, it will be hard to beat for a year):**
```
forecast(outlet, date) = median(same weekday, last 4 business dates)
                       × trend_factor(last 14 days vs prior 14 days, clamped 0.8–1.3)
                       × event_multiplier(holiday / festival / promo flag, manual override)
```
Report MAPE weekly. Only graduate to a learned model once the naive baseline has 12+ weeks of error history to beat and it is genuinely losing. Forecast output feeds two things: covers → requisition quantities, and covers → labour scheduling.

---

## 6. AI features — the architecture rule

Stage 2 introduces the stock/requisition engine. One principle decides whether it works:

> **The LLM parses and explains. Deterministic code decides.**

| Step | Owner |
|---|---|
| Read the uploaded stock file (any layout, messy headers, Bengali/English item names, handwritten photo) | LLM — structured extraction to a fixed JSON schema, with a confidence per row |
| Map extracted rows to canonical `inventory_items` | Fuzzy match + LLM fallback → **human confirms unmatched rows once**, mapping is remembered |
| Compute required quantity | **Pure Python.** `need = forecast_covers × recipe_qty_per_cover × wastage_factor − on_hand − in_transit + par_safety_stock`, rounded up to `order_unit` |
| Detect anomalies | **Statistics.** z-score of consumption-per-cover vs that item's trailing 28-day distribution; flag \|z\| > 2.5. Also: on-hand rising while sales rise, count deviating > 20% from theoretical, item ordered but zero movement |
| Write the explanation and the manager-facing summary | LLM — narrates the numbers it was given, never recomputes them |

An LLM doing the arithmetic will be wrong occasionally and confidently, and nobody will catch it because the output looks reasonable. Every number a manager acts on must be traceable to a formula.

Anomalies worth flagging on day one of Stage 2: consumption per cover jumping without a menu change (theft or over-portioning), on-hand counts that never change (not actually counting), requisition consistently 30%+ above consumption (padding), an item consumed with zero corresponding sales (staff meals or wastage not logged).

---

## 7. Stage 1 build plan

| Epic | Deliverable | Definition of done |
|---|---|---|
| **E0** | Repo, tooling, CI, `CLAUDE.md` | `pnpm dev` runs web+api; lint, typecheck, pytest, vitest all green in CI |
| **E1** | Supabase schema, RLS, enums, seed | Migrations apply clean from zero; seed creates 1 outlet, 5 users (one per role), 6 SOP templates |
| **E2** | Auth + role-aware routing | Login, logout, refresh, password reset; protected routes; role-based nav; 401/403 handled |
| **E3** | Admin: outlets & users | CRUD outlets; invite user, assign global role + outlet memberships; deactivate |
| **E4** | SOP template builder | Create/edit template + items, reorder, mark critical/photo/value, versioning on change, assign to outlets with schedule |
| **E5** | Checklist runner (mobile-first) | Today's runs list, item-by-item flow, camera capture + client resize, offline queue (IndexedDB) with retry, submit |
| **E6** | Manager review queue | Submitted runs, photo lightbox, integrity flags visible, approve/reject with reason, separation-of-duties enforced |
| **E7** | Compliance dashboard | Outlet SOP score + bands, 28-day trend, completion/on-time/critical-fail breakdown, all-outlet comparison table, exception list |
| **E8** | Integrity + scheduled jobs | pHash duplicate detection, burst/geofence/late flags, 05:00 run materialisation, missed-run alerts, daily digest email |
| **E9** | Sales ingestion skeleton | Upload Petpooja XLSX, idempotent by file hash, parse to `sales_orders`/`sales_order_items`, raw table view, ingestion interface ready for a `PetpoojaAPISource` implementation |

### 7.1 Suggested sequence for Claude Code

`E0 → E1 → E2 → E3 → E4 → E5 → E6 → E8 → E7 → E9`

E8 before E7 because the dashboard's integrity numbers need the flags to exist. E9 last because nothing in Stage 1 depends on it.

### 7.2 Explicitly out of scope for Stage 1

Inventory/requisition engine · AI parsing · forecasting · sales dashboard · labour scheduling · vendor/PO management · recipe & costing · guest CRM · WhatsApp/notification delivery beyond email · mobile app (web PWA only).

---

## 8. Frontend conventions

- **Two shells.** `/app/*` = desktop-first management UI (sidebar, data tables, dashboard). `/floor/*` = mobile-first staff UI (single column, thumb-reach actions, large tap targets, minimal chrome). Different layouts, same auth.
- **shadcn/ui** components generated into `components/ui/`, never hand-edited. Extensions go in `components/`.
- **Server state** via TanStack Query, keyed `['sop','runs',outletId,businessDate]`. No global store for server data. Zustand only for genuine UI state (offline queue, active run draft).
- **Forms** react-hook-form + zod. Zod schemas generated from the OpenAPI spec where possible so validation cannot drift from the backend.
- **Dates** one utility module. `formatBusinessDate`, `toBusinessDate`, `outletNow(outlet)`. Never `new Date().toISOString().slice(0,10)` anywhere in feature code.
- **Brand** red `#ee3345`, blue `#326fb7`, ink `#231f20`, white ground. Noto Sans. Red is an accent — use it for the primary action and the "red" health band, not for chrome. Health bands: green `#2f9e5f`, amber `#e0a020`, red `#ee3345`.
- **Empty and offline states are designed, not afterthoughts.** In-store wifi will drop; the checklist runner must never lose a half-finished run.

---

## 9. Backend conventions

- One package per domain: `router.py` (HTTP only) → `service.py` (business logic, transactions, audit) → `repository.py` (SQL) → `schemas.py` (pydantic). Routers never touch the DB; repositories never contain business rules.
- All money and scoring logic lives in `core/scoring.py` and `core/money.py`, pure functions, unit-tested with table-driven tests.
- `core/business_date.py` is the only place the 05:00 rollover is expressed. Python and SQL versions tested against each other.
- Errors: one `AppError` hierarchy → RFC 7807 problem+json. Never leak SQL or stack traces.
- Idempotency: file uploads keyed by SHA-256; run submission keyed by `(assignment_id, business_date, day_part)`.
- Scheduled jobs run under APScheduler in-process for Stage 1 (single instance), with a `job_runs` table recording each execution so a missed 05:00 materialisation is visible rather than silent. Move to a worker queue when there is a second API instance.

---

## 10. Risks and how Stage 1 handles them

| Risk | Mitigation |
|---|---|
| Staff photograph yesterday's clean kitchen | pHash duplicate detection, burst-upload flag, geofence |
| Manager approves everything unread | Approval requires opening each photo (tracked); random 10% of runs auto-flagged for owner spot-check in the digest |
| Checklist becomes a 60-item chore nobody completes | Cap templates at ~15 items; only mark genuinely critical items critical; measure completion rate weekly and cut items that always pass |
| Wifi drops mid-checklist | IndexedDB draft + retry queue; run is never lost |
| Schema doesn't survive outlet 2 | `outlet_id` on every operational table from day one; no global singletons; seed data includes a second dummy outlet in dev |
| Midnight trading corrupts reports | `business_date` with 05:00 rollover, enforced in one function, tested |
| Sales file re-uploaded twice | SHA-256 idempotency on `data_uploads`, unique `(outlet_id, external_bill_no)` |
| Petpooja changes export format | Parser is a versioned adapter behind a `SalesSource` interface; a format change is a new adapter, not a rewrite |

---

## 11. Open questions to resolve before E1

1. Petpooja: is API access available on your plan, and what does the vendor charge? Determines whether E9's `PetpoojaAPISource` is Stage 2 or Stage 4.
2. Staff device reality: do floor staff have their own smartphones, or is there one shared outlet tablet? A shared device changes the auth model (kiosk mode + PIN per staff, rather than individual logins).
3. Current SOP documentation: does a written cleaning/opening/closing checklist already exist on paper? If yes, it is the seed data and should replace section 4.4's guesses.
4. Outlet 2 timeline — if it is within 3 months, the second dummy outlet in dev seed is not optional.
5. Who is the approver when the outlet manager is the one running the closing checklist? (Likely: `ops_manager` approves outlet-manager submissions. Needs confirming or the separation-of-duties constraint will block real workflows.)
6. Notification channel: email only for Stage 1, or is WhatsApp expected? Staff in Indian F&B ops largely live on WhatsApp — worth knowing early even if it ships in Stage 2.
