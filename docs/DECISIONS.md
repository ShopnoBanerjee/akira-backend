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
> Refined by D9/D11: the seeded flags are a starting point, not a fixed
> decision — admins edit them, and D11 keeps those edits from reaching
> backwards into completed runs.

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

## D9 — Admin-editable settings, and what stays locked

**Decided 26 Aug 2026.** Requested directly: an admin settings area where
"everything" is editable.

`app_settings` (migration 0010) is **append-only with an effective date**. A
change inserts a new row; the value in force at any moment is the newest row
whose `effective_from` is at or before it, with an outlet override beating the
global value. Scoring a period from three months ago therefore uses the weights
that were live then, so historical outlet scores stay reproducible instead of
silently rewriting themselves whenever somebody nudges a weight.

The **code owns the schema of settings**, not the table. `app/core/settings.py`
holds a registry: every known key with its type, default, valid range, and
whether an outlet may override it. A key absent from the registry is ignored on
read. That is what stops a typo or an out-of-range value from quietly breaking
scoring.

Editable: scoring weights and health bands, integrity thresholds, AI review
controls, job times and notification recipients.

**Deliberately not editable — the 05:00 business-date rollover.** Two concrete
reasons, not caution:

1. `business_date()` is declared `immutable`, which is what lets the planner use
   it in index expressions. Reading a runtime value would force it down to
   `stable` and cost those indexes.
2. Every historical row already stores its `business_date`. A changed rule would
   disagree with months of recorded data.

Changing it is a migration plus an explicit backfill, on purpose.

## D10 — Inventory catalogue pulled into Stage 1

**Decided 26 Aug 2026.** The spec defers the whole stock module to Stage 2.
Adding inventory items through admin requires the catalogue now, so migration
0009 lands it. Stage 1 ships **catalogue and per-outlet levels only** — there is
no counting flow and no requisition engine; both will build on these tables
rather than replacing them.

**Shape: one shared catalogue, levels per outlet.** Add an item once and every
outlet can stock it; a larger outlet holds more of it. Two outlets entering the
same ingredient under two ids would make cross-outlet consumption and cost
comparison meaningless.

Seeded with all 151 items transcribed from the five paper count sheets, with
Bengali names, units and departments. `scripts/generate_inventory_seed.py` holds
the transcription so it stays auditable and regenerable; the SQL it emits is
idempotent.

**Three data-quality problems found in the source sheets**, corrected in the
seed with a `notes` value recording the original:

- *Begun* carried the Bengali "শুরু করা হয়েছে" (= "has been started"), a
  machine-translation error. Begun is the aubergine, বেগুন.
- *Habit Panko* carried "অভ্যাস" (= "habit"), the same class of literal
  mistranslation. Recorded as Panko breadcrumbs.
- *Pillow* (বালিশ) is filed under Cleaning in housekeeping, which looks wrong —
  most likely a scouring pad. Left as-is pending confirmation.

Also note the bar sheet and the mise-en-place sheet both list Club Soda, Sugar
Syrup, Lemon and Ice. The unique index is per department, so both survive as
separate rows; they may or may not be genuine duplicates.

## D11 — Template item version history

**Decided 26 Aug 2026.** Follows directly from D9: once admins can freely edit
`requires_photo` and `is_critical`, those edits must not reach backwards.

`checklist_runs` already snapshotted `template_version`, and the spec requires
historical runs to render against the definitions live when they ran — but
`template_version` referred to nothing retrievable. Item rows are mutated in
place, so a run from three weeks ago would re-render against today's flags.
Marking an item critical would retroactively make every past run that failed it
look like a critical failure, and any recomputed score would change with it.

`checklist_template_item_versions` (migration 0011) is the thing
`template_version` refers to. Every material edit writes an immutable row at the
new template version; `checklist_run_items.template_item_version_id` points at
the exact definition answered against. The live
`checklist_template_items` row remains the current definition for new runs.

Same philosophy as `app_settings`: history is reproducible, not rewritten. There
the value in force resolves by effective date; here the definition in force
resolves by template version.

**Verified:** with an item edited from non-critical to critical and the template
bumped v1 to v2, a run recorded at v1 still resolves to `is_critical = false`
and `requires_photo = false`, a run at v2 resolves to true for both, and the
live row shows the admin's edit.

**What this obliges the service layer to do (P4).** A material edit must, in one
transaction: bump `checklist_templates.version`, insert a new version row for
every item in the template, and write the audit entry. Editing only a
template's description is not material and must not bump the version. The run
materialiser must stamp `template_item_version_id` on every run item it creates.

## D12 — What the integrity engine had to decide for itself

**Decided 27 Aug 2026.** Building P7 forced six choices the spec and D6 left
open. Each is here because it will look arbitrary to whoever reads the code
next.

**1. A flag carries its evidence.** Migration 0004 gave run items an
`integrity_flags` array and nowhere to say *why*. `duplicate_photo` on its own
tells a manager to distrust a photo without telling them what it matched, which
is the kind of unfalsifiable red chip staff learn to ignore within a month.
0013 adds `integrity_detail`: the matched run and Hamming distance, the measured
luminance against its floor, the share of a run that landed inside the burst
window. The review screen renders the evidence beneath every chip.

**2. Run-level flags are not item-level flags.** `late`, `out_of_geofence` and
`burst_upload` describe a submission, not a photograph. Stamping them onto each
photo would misplace the evidence and inflate any per-photo count built on top.
`checklist_runs` gets its own `integrity_flags` array; `integrity_flag_count`
is the sum of both, which is the number the outlet-score penalty reads.

**3. Each pass owns its own flags.** The deterministic photo pass recomputes
duplicate/stale/dark and therefore *clears* them when a photo is re-shot — a
rejected-and-redone item must not keep an accusation it has answered — but
never touches `ai_mismatch`, which it knows nothing about. The AI pass is the
mirror image.

**4. `mark_missed` touches `pending` only, never `in_progress`.** `missed` is
terminal, so flipping a run somebody is halfway through would lock it and
discard the work. A run being done late is what the `late` flag exists for. The
exception it raises is **medium**, not high: nobody did the checklist, which is
a management problem to chase, not the same class of event as a critical
food-safety item failed outright.

**5. Three AI-review rules, refining D6.**

- *The confidence threshold is applied at read time.* The model's raw verdict
  and confidence are stored; `ai_review.uncertain_below_confidence` downgrades a
  low-confidence verdict to `uncertain` when the flag is decided and when the
  screen renders it. Storing the downgraded value would make the record
  unreadable against a threshold that has since moved — the same reason
  `app_settings` is effective-dated (D9).
- *`ai_mismatch` fires in one direction only:* staff recorded a pass and the
  reviewer is confident it is a fail. The reverse is staff being harder on
  themselves than the machine, and flagging it would punish honesty.
- *A missing reference photo degrades rather than blocks.* D6 says the AI
  compares against that outlet's own standard, and it does when one exists.
  Requiring one would mean nothing works until every station at every outlet has
  been shot under service lighting — which is exactly how that prerequisite went
  unbuilt for three epics. Without one the model judges on the item's
  instruction and is told to lean towards `uncertain`; the review records that it
  had no standard.

Silence from the model is **not** `uncertain`. An unreachable provider is a
recorded skip with a reason, never a row: writing one either way would put a
fabricated opinion into an audit trail.

**6. Notifications degrade loudly.** Refines A3. With no SMTP host configured
the digest is still built, rendered and logged, and the `job_runs` row records
`smtp_not_configured`. Requested directly: leave it degrading visibly rather
than switching the configured channel to `log_only`, because a digest that
quietly stopped sending while reporting success every morning is the precise
failure this epic exists to prevent.

**Vision model: `claude-sonnet-5`, by the owner's decision.** Every photo on
every run at every outlet is reviewed, so the per-photo cost is the whole cost.
`run_item_ai_reviews` records the model and prompt version on every verdict and
is keyed on both, so re-running a period against a more capable model later is
additive and never overwrites what the first one said.

**Found while testing, not while reading:** the six photos already in Storage
from P5 and P6 are 262-byte stubs, not decodable images. `process_photo` now
raises `UndecodableImage` rather than swallowing it, the failure lands in
`job_runs`, and the item stays unprocessed — which the review screen shows as
"not checked yet", which is the truth. `scripts/backfill_photo_integrity.py`
hashes pre-P7 photos and is idempotent.

## D13 — A second vision provider, for testing only

**Decided 27 Aug 2026.** `AI_REVIEW_PROVIDER` selects `anthropic` (default) or
`groq`. Requested directly: there was no Anthropic key to hand and the point
was to find out whether the AI pipeline works at all.

**Why this rather than waiting.** Everything between the photo and the
manager's screen — reference lookup, the confidence threshold, the
one-directional `ai_mismatch`, the stored row, the review UI — is provider
agnostic. Leaving it unexercised because one vendor's key was missing would
have shipped an epic on unit tests alone. What was learned by running it is not
reproducible from reading: the free tier's 8000 tokens-per-minute ceiling is
hit by two photos in one request, which is why a 429 is now a first-class
"no verdict" rather than an error.

**What is deliberately shared, and what is not.** The system prompt and the
question are byte-identical between providers, so a verdict means the same
thing whichever answered. Only the transport differs. `run_item_ai_reviews`
stores the model that *actually* replied — not the one that was asked for —
and is keyed on `(run_item_id, model, prompt_version)`, so an Anthropic verdict
and a Groq verdict on the same photo coexist as separate rows rather than
overwriting each other.

**Groq specifics, all established by probing the live API rather than from
documentation:** the only image-capable model on the roster is
`qwen/qwen3.8-27b` — every other model either 400s on an image block or is
text-only. It accepts two images in one request, which the reference
comparison depends on, and it honours `response_format: json_schema` with
`strict`. Called over plain `httpx`; a second vendor SDK for one POST would
cost more than it saves.

**Anthropic stays the production default** and the default is asserted against
the *declared* field rather than a constructed `Settings`, because a bare
`Settings()` reads the developer's `.env`. That is not hypothetical: adding
`AI_REVIEW_PROVIDER=groq` locally broke a pre-existing test and sent another at
the real network. `tests/conftest.py::isolated_settings` now builds settings
from declared defaults, and every test that constructs them goes through it.

**Caveat on the model itself.** Qwen reports extreme confidence — 1.0 and 0.0
were both observed on real photos. The confidence threshold exists precisely
because a model's self-reported certainty is not evidence, but it does less
work when the values are saturated. Treat Groq verdicts as a demonstration that
the plumbing works, not as a calibrated compliance signal.

## D14 — No blended health score until there is something to blend

**Decided 27 Aug 2026.** Spec section 5 defines outlet health as four weighted
pillars summing to 100: Sales 30, SOP 30, Inventory 25, Guest 15. Stage 1
delivers **only SOP**. P8 therefore ships the SOP compliance score as the
headline number and draws the other three greyed, rather than computing a
blended figure.

**Why not just apply the weights now.** `0.30 x SOP` would render a flawless
outlet as 27/100. Silently rescaling to the live weight instead — dividing by
0.30 — makes the number *change* the day a second pillar lands, with nothing
about the outlet having changed, which quietly invalidates every screenshot,
target and conversation that referenced it. Both options produce a number
somebody would act on and neither means what its label says. The layout is
built for four pillars so nothing moves later; the arithmetic waits.

**Consequences, all in P8:**

- `GET /dashboard/outlet-health` returns `pillars` with `status: "live"` for
  SOP and `"stage_2"` for the rest. The card greys them rather than hiding
  them, so the shape of the finished thing is visible from the start.
- `GET /dashboard/outlets` returns one SOP score per outlet, ordered by code
  and **not** by score. A league table invites gaming the number rather than
  doing the work.

**Three scoring decisions the spec's formula did not settle:**

1. **A component with no denominator contributes zero, and is not re-weighted
   out.** An outlet that approved no runs has earned no run-score credit;
   re-weighting the remaining terms would hand it full marks for a paper it
   never sat.
2. **Nothing scheduled means no score, not zero.** A closed outlet has not
   failed — it has not been measured. `score` is null and the band is `none`,
   the same principle as `run_score()` returning None for an all-N/A run.
3. **The critical-failure cap holds the BAND, not the number.** "A single
   unresolved critical failure caps the outlet at amber regardless of
   arithmetic." The score stays honest at, say, 94 and the band reads amber
   with an explicit reason, so the card can say *why* rather than showing a
   silently depressed figure nobody can reconcile.

**"1 point per integrity flag per 10 runs" is a rate, not a count.** The
penalty is `10 x flags / scheduled`, so an outlet running twice as many
checklists is not punished twice as hard for the same standard of honesty.

**Weights are resolved at the END of the period being scored**, never "now"
(D9). Re-opening July uses July's weights. `app/domains/sop/metrics.py` is the
single counter behind both this and the daily digest — two queries would
eventually disagree, and the morning they did nobody would know which number to
believe.

## D15 — Sales ingestion is orders only, and the uploader names the outlet

**Decided 27 Aug 2026.** Reading AKIRA's real Petpooja exports before writing
the parser changed two things the spec assumed.

**1. `sales_order_items` cannot be populated, so P9 does not populate it.**
The spec says it comes "from Item Sale Report". It cannot: **no Petpooja item
export carries a bill or invoice number.** All four in the supplied set were
checked — Item Sale Report (hourly), Item Report Day Wise, Item Wise Sales,
Highest Selling Items — and every one is pre-aggregated by hour, day, category
or item. `sales_order_items.order_id` is `not null`; there is nothing to point
it at.

The options were to leave it empty, to relax `order_id` and store aggregates
under a table whose name would then be a lie, or to wait. **Decided: leave it
empty**, requested directly. Nothing in Stage 1 reads it — the Sales pillar is
Stage 2 — so the cost today is zero, and a schema change made on a guess about
a report nobody has seen would be worse than a gap that is written down.
Tracked in `docs/OPEN_ITEMS.md`. If Petpooja turns out to offer a bill-wise
item export on this plan, that is a second adapter, not a rewrite.

**2. The uploader picks the outlet.** The Orders Master Report has no outlet
column; its preamble names only the restaurant ("Akira"), so both outlets would
produce files that look identical. Matching on a stored `petpooja_name` was
considered and rejected for now: it needs per-outlet configuration before the
first upload can work, and breaks silently if the name is ever edited in
Petpooja. An explicit choice at upload time is auditable and wrong choices are
correctable, because ingestion is idempotent.

**Idempotency is on the bytes, not the filename.** `data_uploads.file_sha256`
is unique. Petpooja filenames carry an export timestamp and change every time,
so a filename check would catch nothing; somebody will re-send the same export
because nothing about a spreadsheet says whether it has been sent. Re-uploading
returns the original row and ingests nothing. The same file offered for a
*different* outlet is refused rather than filed twice.

**The source file is retained**, which is what `storage_path` is for: an
adapter version bump must be able to re-read the original instead of asking for
a fresh export. That does mean the raw export — 141 customer phone numbers and
142 names in the current one — sits in Supabase Storage, so `sales-uploads` is
private with no browser read path. Only the salted digest reaches a database
column, and the raw number never leaves the parse loop.

**Overlapping exports upsert rather than insert.** Each new export covers the
same weeks plus a few more days. A bill already present is updated in place, so
a corrected total replaces the old one instead of being skipped forever.

**Written set-at-a-time.** The first version looped one statement per bill and
took **75 seconds** for 452 rows against Supabase — 452 round trips. The same
work as a single `unnest` upsert takes **5.4 seconds**. Six weeks is the small
case; a year would have taken eleven minutes and looked like a hang.

**Verified against the real file, not a sample.** 452 bills, Rs 4,86,076.35 to
the paisa, 38 trading days, zero warnings. The 21 bills struck before 05:00 all
sit on the previous trading day. An independent cross-check: the daily total
for 25 Aug is Rs 8,564.00, which is exactly the Total row of the separate
hourly Item Sale Report for that day.

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
