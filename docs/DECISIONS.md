# Decision log

Deviations from `STAGE1_SPEC.md` and the choices the spec left open. Each entry
records what was decided and why, so a future session does not "fix" a
deliberate choice back into the spec's default.

Date format is absolute. Newest last.

---

## D1 — Two repositories, not a monorepo

**Decided 26 Aug 2026.** The spec describes a single `akira-ops/` monorepo with
`apps/web` and `apps/api`. We ship two independent repositories instead:

- `akira-backend` — FastAPI, the database schema, migrations, seed, OpenAPI
- `akira-frontend` — Vite + React web client

**Why:** requested directly. Separate deploy targets and separate CI, with no
shared build orchestration to maintain.

**Consequences:**

- `supabase/migrations` and `supabase/seed` live in **this** repo. The backend
  owns the schema outright.
- The spec's `packages/shared` enum mirror is **dropped**. A shared package
  across two repos is a synchronisation problem with no upside here, because the
  OpenAPI schema already carries every enum. The backend commits `openapi.json`;
  the frontend generates `src/types/api.ts` from it and CI fails on drift.
- `docs/STAGE1_SPEC.md` is copied into both repos so either can be worked on
  alone.
- The root `pnpm dev` running both apps concurrently no longer exists. Each repo
  starts independently; see each README.

## D2 — Frontend is Vite + React, not Next.js

**Decided 26 Aug 2026.** The Supabase setup snippet supplied at kickoff was
Next.js-specific (`next/headers`, `middleware.ts`, `utils/supabase/server.ts`).
That is Supabase's default dashboard onboarding, which always renders Next
regardless of the project's stack.

**Why:** the spec fixes Vite and forbids alternatives, and a Next server runtime
would invite exactly the two-half-backends failure that section 1.2 warns about
twice.

**Consequences:** only `createBrowserClient` is used. There is no `server.ts`
and no `middleware.ts` — neither has meaning in a Vite SPA. Session refresh is
handled by the Supabase JS client.

## D3 — Shared outlet tablet, PIN-attributed staff

**Decided 26 Aug 2026.** Resolves spec open question 2. Floor staff do **not**
have individual smartphones; each outlet has one shared tablet.

**Why:** confirmed as the operational reality at New Town.

**Consequences:** this is the largest deviation from the spec, which assumes
individual logins for every role. The design is specified in `CLAUDE.md` under
"Auth model — shared outlet tablet". In short: the tablet holds one
outlet-bound Supabase session, individual staff identify with an Argon2-hashed
PIN, and `submitted_by` still resolves to a real person so the separation-of-
duties CHECK constraint keeps its meaning. Schema additions in E1:
`profiles.pin_hash` and an `outlet_devices` table. A PIN authorises floor
actions only and can never approve a run.

## D4 — SOP seed comes from the real checklists

> Refined by D8: only two of the seven documents turned out to be checklists.

**Decided 26 Aug 2026.** Resolves spec open question 3. AKIRA has seven existing
operational checklist documents (Kitchen Cleaning, Mise-en-place, Housekeeping,
FNB Hot Range, FNB Service, FNB Desserts, Beverages).

**Why:** the spec itself says that if real SOP documentation exists it *is* the
seed data and should replace section 4.4's invented templates. Staff recognise
their own checklists; they will not recognise plausible-sounding substitutes.

**Consequences:** section 4.4's six starter templates are **not** seeded.
E1 extracts templates from the real documents instead, mapping each line to
`requires_photo` / `is_critical` / `value_type` / bounds. The spec's 15-item cap
warning applies during extraction — long paper checklists should be split by
day-part rather than seeded whole.

## D5 — Supabase JWTs are ES256, verified against JWKS

**Decided 26 Aug 2026.** The project's JWKS endpoint serves a single ES256
elliptic-curve public key. Supabase signs asymmetrically here; there is no
legacy HS256 shared secret.

**Why:** verified directly against the live endpoint at kickoff, not assumed.

**Consequences:** `app/core/security.py` (P2) verifies with the public key set
fetched from `SUPABASE_JWKS_URL`, caching it and refreshing on a `kid` miss.
Never configure or expect a symmetric `SUPABASE_JWT_SECRET`. Environment
variables use Supabase's current names — `SUPABASE_SECRET_KEY` and
`SUPABASE_JWKS_URL` — rather than the older `SUPABASE_SERVICE_KEY` and
`SUPABASE_ANON_KEY` the spec's prompt pack references.

## D6 — AI photo review in Stage 1, advisory only

**Decided 26 Aug 2026.** Each outlet holds its own standard reference photos.
A submitted photo is reviewed first by an AI, then by a human.

**Why:** requested directly. Note this moves AI into Stage 1: spec section 7.2
lists AI parsing as explicitly out of scope for Stage 1, and section 6 places AI
in Stage 2.

**Consequences, shaped to keep the spec's one durable AI rule ("the LLM parses
and explains, deterministic code decides"):**

- The AI is **advisory**. It emits a verdict, a confidence and a rationale
  against that outlet's reference photo. It never blocks a submission and never
  approves a run. A manager still decides, which is what keeps the
  separation-of-duties constraint meaningful.
- **"Visible light conditions" is a deterministic check, not an AI one.** Mean
  luminance on upload, flagged `too_dark`. Cheap, repeatable, no model call.
- New tables: `outlet_item_reference_photos` (per-outlet standard, one active
  per outlet per item) and `run_item_ai_reviews` (kept separate from
  `checklist_run_items` so a review can be re-run against a newer model without
  destroying what an earlier one said; model and prompt version stay auditable).
- New integrity flags: `too_dark`, `ai_mismatch`.
- The vision pipeline itself lands with the integrity engine in P7.

## D7 — Schema extensions the real checklists forced

**Decided 26 Aug 2026.** Reading AKIRA's seven operational documents exposed
three things the spec's schema could not express.

1. **Bilingual fields.** Every paper checklist carries English and Bengali on
   every line, and the kitchen reads Bengali. Added nullable `title_bn`,
   `instruction_bn`, `name_bn`, `label_bn`, `caption_bn`. An English-only
   rendering would be less usable than the paper it replaces.
2. **`frequency` could not express the real cadences.** The spec allows
   per_shift/daily/weekly/monthly. AKIRA actually runs daily, **alternate day**,
   3 days a week, and **every 15 days**. The weekly-cycle ones fit
   `active_weekdays`; alternate-day and fortnightly do not align to a 7-day
   cycle at all. Added `alternate_day` and `fortnightly` to the enum, plus
   `interval_days` and `anchor_date` on `checklist_assignments`.
3. **Weekly deep clean is per-item-per-weekday.** The kitchen list pins a
   different task to each weekday (Mon non-veg fridge, Tue veg chiller, Wed veg
   freezer, Thu staff toilet, Fri/Sat maintenance), but `active_weekdays` sits
   on the assignment, not the item. Modelled as separate single-purpose
   templates, which is schema-native and needs no new column.

Also added `job_runs` (0006), which the conventions require but the spec's table
list omits, and `outlet_devices` for D3.

## D8 — Only 2 of the 7 operational documents are SOP checklists

**Decided 26 Aug 2026.** The other five are inventory count and requisition
sheets (Sl No / Category / Department / Item Name / Bengali Name / Unit /
Physical Closing Count / Requisition Qty Needed), which is Stage 2 stock-count
data, not Stage 1 compliance.

- **Real SOP checklists:** Kitchen Cleaning & Sanitation; Service & Housekeeping
  Operations.
- **Inventory sheets:** Hot Range (97 items), FNB Service (19), Housekeeping
  (13), Beverages (13), Desserts (9). Captured as Stage 2 seed data, not loaded
  into any Stage 1 table.
- **Mise-en-place** is a par-level tracker. Seeded as a Stage 1 prep-readiness
  checklist using `requires_value` numeric items with the paper's minimums as
  `value_min`; the same data migrates to inventory par levels in Stage 2.
- The paper has **no temperature logging, no opening or closing procedure, and
  no cash or POS reconciliation**. For a ramen kitchen the missing temperature
  log is a genuine food-safety gap, so one Food Safety Daily template is seeded
  from spec 4.4 to cover it. Nothing else is invented.

---

## Assumptions in force — challenge these if wrong

- **A1 — `ops_manager` approves outlet-manager submissions.** Spec open question
  5. Without a named approver above the outlet manager, the separation-of-duties
  constraint blocks the real closing-checklist workflow.
- **A2 — Petpooja is manual XLSX upload for all of Stage 1.** Spec open question
  1. `api_source.py` ships as a documented stub. Revisit when the vendor's API
  pricing is known.
- **A3 — Email only for Stage 1 notifications.** Spec open question 6. The
  `Notifier` interface is pluggable so a WhatsApp implementation is additive.
- **A4 — Outlet 2 timeline is unknown**, so the dev seed carries a second dummy
  outlet from day one, as the spec's risk table requires.
