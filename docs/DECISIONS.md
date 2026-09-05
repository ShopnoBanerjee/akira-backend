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

**Retired 29 Aug 2026 by D22** — all four pillars now produce, so the blend
D14 refused to fake is real arithmetic. The reasoning below stands as the
record of why it waited.

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

## D16 — Latency is round trips, so round trips are what we cut (P10)

Every endpoint took over a second. The instinct was to blame the queries. It
was measured instead, and the queries were never the problem:

    TCP handshake, Kolkata to Supabase's Mumbai region ... 152 ms
    `select 1` on an already-open connection ............. 151 ms
    what Postgres spends executing it (EXPLAIN ANALYZE) .. 0.1-0.2 ms

The database answers in a fifth of a millisecond and the wire costs 152. So
response time is very nearly `152 ms x (number of times this request talks to
Supabase)`, and every fix below is about that multiplier and nothing else.

**True millisecond fetches are not reachable from a laptop in Kolkata.** They
are reachable in production, where the API sits in the same region as the
database and that 152 ms becomes about 1 ms — but only if the trip *count* is
low, which is the part that is fixed here and would otherwise have shipped.

### What was actually costing trips

| Cause | Cost |
|---|---|
| `pool_pre_ping=True` | a liveness round trip before every request |
| a transaction around read-only requests | `BEGIN` + `ROLLBACK`, 2 trips |
| three separate auth queries per request | 3 trips before the handler ran |
| a new `httpx.AsyncClient` per signed URL | a fresh TLS handshake each time |
| signing photo URLs one at a time, serially | 1 HTTPS call per photo |
| the dashboard looping over outlets | 3 trips per outlet |
| `update profiles set last_seen_at` | a write on every authenticated request |

### What replaced them

`pool_pre_ping` is gone and `pool_recycle` is 240 s — shorter than any
plausible idle timeout, which is the guarantee the ping was buying at the price
of a round trip. **GET and HEAD get an autocommit session** (`db.py`), chosen by
method in one place rather than by a second dependency on every read route; a
single `SELECT` is atomic without a transaction. The trade is that several
reads in one handler no longer share a snapshot — invisible for these screens,
and the handlers that most needed consistency are the ones now down to a single
statement anyway.

**One identity query** (`deps.py`) via `left join lateral` + `json_agg`, where
there were three. **One signing request** for every photo on a screen, through
Storage's batch endpoint, on a client that stays open. **One statement for all
outlets** on the dashboard, via `unnest` of the outlet ids — the loop meant the
comparison screen got slower as the group grew, which is the one thing a
multi-outlet comparison must not do. `last_seen_at` is folded into the profile
read and only written when it has actually gone stale; it was producing a dead
tuple and an index update per request to move a timestamp by milliseconds.

### Measured, best of three, warm, against the live system

| endpoint | before | after |
|---|---:|---:|
| `/sop/runs/{id}/detail` | 4889 ms | 1883 ms |
| `/dashboard/outlets` | 2761 ms | 823 ms |
| `/sop/reference-photos` | 2410 ms | 834 ms |
| `/sop/runs` | 1678 ms | 420 ms |
| `/settings` | 1360 ms | 430 ms |
| **all eleven** | **22.4 s** | **7.7 s** |

The worst endpoint went from 9 SQL statements and 2 serial HTTPS calls to
**4 statements and 1**. Most list endpoints now sit at two round trips — one to
authenticate, one to answer — which is the floor without caching identity.

### The N+1 audit that followed

An AST sweep for awaited queries inside loops found seven sites. Two were
real and fixed: `mark_missed` paid two round trips per overdue run (and had a
race — the exception was raised even when the guarded update matched nothing
because someone had started the run in between; the set-based version joins
the insert to the update's `returning`, so "flipped" and "raised" are the same
set by construction), and submit paid one insert per critical fail — on the
floor's hot path, so a bad night got slower in proportion to how bad it was.

Three were left alone, each for a reason worth keeping: the sales writer's
loop is the deliberate 1000-row chunking from P9, not an N+1;
`materialise_runs` keeps its per-assignment loop because the cadence predicate
lives in Python on purpose and moving it into SQL would give it two homes; and
`replace_memberships` iterates over an admin's outlet list, which is bounded by
the number of outlets the group has.

### What was rejected

**supabase-py instead of SQLAlchemy.** Benchmarked head to head rather than
argued about:

| operation | asyncpg | supabase-py |
|---|---:|---:|
| one row by id | 152 ms | 343 ms |
| 452 sales_orders rows | 160 ms | 378 ms |
| `count(*)` | 152 ms | 330 ms |
| `group by business_date` | 154 ms | *cannot express* |

It is ~2.2x slower, because PostgREST is an HTTP hop *in front of* the same
152 ms wire, not instead of it. It also cannot express the queries this app is
built on — grouped aggregates, `count(*) filter (...)`, the `unnest` upsert,
the lateral-join identity query — so each would have become a database function
called by RPC: the same SQL, moved out of migrations and behind an extra hop.
And it would put a second data path beside the API, which is the "two
half-backends" failure CLAUDE.md warns about twice.

**Caching the identity lookup.** It would take most endpoints from two trips to
one, and it is the obvious next move — but a cached role or membership is a
stale authorisation decision, and that is not a trade to make casually or late
at night. Left for a deliberate change with an explicit invalidation story.

---

## D17 — The stock count engine parses on paper's terms, not the model's (P11)

Stage 2 opens with the count-sheet pipeline. The governing spec rule — the LLM
parses and explains, deterministic code decides — was enforced structurally,
then each choice inside it was measured.

**The extraction provider was decided by experiment, not preference.** The
real 27 Aug sheet (8 photographed pages, handwritten counts over printed
rows) went to the Groq qwen vision model twice: naive, then with anchored
catalogue names and double-resolution half-pages. Names anchored fine; the
handwritten values did not — they landed on neighbouring rows at 0.9
confidence, and "2.800" became 2500, "700" became 500. Wrong-and-confident is
the one failure mode the pipeline cannot tolerate, so Anthropic is the
production extraction provider (STOCK_EXTRACT_PROVIDER) and every line the
Groq fallback produces is forced into human review regardless of its claimed
confidence. Groq remains for exercising the pipeline on a free key.

**The quantity parser never guesses.** Conventions were read off the real
sheet: "1.500" under a grams column is the kitchen's thousands-dot (1.5 kg);
"1kg" converts; a circled zero is a counted zero; blank is "nobody counted"
and stays null. Everything else — "5pk" on a grams item, "1kg 7pc", "1.5" —
is refused with the raw preserved and the reason attached. A wrong number
that looks right flows into an order; a refusal flows to a person.

**The fuzzy-match floor is 0.92, set by measured clusters.** Real OCR slips
of catalogue names score >= 0.96 ("Peelred Garlic" 0.963); the measured false
positive — "Mystery Sauce" onto Oyster Sauce, found by a test — scores 0.880.
Fuzzy also refuses when two candidates score within 0.06 of each other,
because the Chilli Powder / Chilli Flakes / Dry Chilli / Green Chilli family
is real and mapping between them corrupts an order quietly. Confirmed human
corrections are remembered as aliases (spec: confirm once, remembered).

**The requisition formula is the par gap, nothing more.**
max(0, par − on_hand) rounded UP to order_unit, working attached to every
line. The spec's fuller formula needs a covers forecast and recipes — Stage 3;
pretending otherwise with six weeks of history would dress a guess as
arithmetic. No par means no number and a `no_par` flag, never an invention.
And the chef's handwritten ask beats the formula as the default final
quantity: the person who ran the shift knows about tomorrow's booking, the
formula does not. `padding` flags an ask more than 1.3× the computed need —
advisory, like every flag in this system.

**A half-reviewed count cannot confirm, and a requisition needs a confirmed
count.** Both refusals exist so no number a manager acts on ever traces back
to an unreviewed model output. Counts restrict-delete from requisitions for
the same reason: the evidence outlives the conclusion.

---

## D18 — Gemini is the AI provider, decided by a golden set, not a preference

The Anthropic key is off the table for now (budget). The replacement was
chosen the way D17 demands: measured. `tests/fixtures/golden_page1.json` is a
human's reading of the real sheet's page 1 — 60 handwritten cells, with
genuinely ambiguous cells carrying every faithful transcription instead of a
pretended certainty — and `scripts/eval_extractor.py` scores any provider
against it. The numbers that decided:

| provider | cell accuracy | row-shift errors | blank fidelity |
|---|---|---|---|
| gemini-3-flash-preview | **60/60 (100%)** | **0** | **32/32** |
| qwen3.8-27b (groq) | 50/60 (83%) | 6 | 30/32 |

Row-shift errors are the metric that matters most: a wrong value that is a
NEIGHBOUR's right value corrupts a requisition silently. Groq produced six;
Gemini produced none — including the Ginger-500/Potato-1.500 pair that was
Groq's signature failure.

**The free tier fits the business.** A single outlet is ~20-30 requests a day
against a ~1,500/day quota. Photo review moved to Gemini in the same pass
(same SYSTEM prompt, same question, third transport in vision.py) and its
first live verdict was honest: it called a synthetic test photo "a digital
graphic, not a photo of the floor". The accepted trade, named out loud:
free-tier requests may be used by Google for training. Count sheets are
ingredient names and quantities — no customer data. The sales exports would
be a different conversation, and they do not go to Gemini.

**Trust policy is per provider.** groq lines are force-reviewed (measured row
shifts are invisible to any confidence threshold). gemini reports a flat,
non-discriminating confidence, so its gate is the deterministic parser —
compounds, unit mismatches and unknown names still stop for a human, which
is where the stopping logic belonged all along. The prompt now also forces
compound cells ("1kg 7pc") to be transcribed whole, turning the one Gemini
soft spot into parser refusals.

**No agent framework.** Every model call here is a single-shot, stateless
transformation — image to JSON, photo to verdict. There is no loop for an
agent framework to own; FastAPI + job_runs + deterministic Python already
are the orchestrator, and that is why every number traces to paper. ADK (or
any agent kit) becomes interesting only if a multi-turn manager copilot is
built — a Stage 4 idea, parked.

**stub is the fourth provider.** It replays a committed fixture with one of
everything the pipeline must handle, so the full extraction contract runs in
CI and keyless local dev. Keys decide which model answers; they never decide
whether the pipeline is testable.

The Gemini key arrived through a chat transcript, like the Groq key before
it: both need rotation from their consoles. Both live only in gitignored
.env. The secret scan gains the AQ. prefix.

---

## D19 — The Sales pillar and the narrated digest (P12)

The health card's second pillar, built the way the first earned trust.

**Normalisation is the simplest honest thing**: min(100, 100 × value/target),
capped so a blowout week cannot bank surplus against a bad one. A sigmoid
would score "nicer" and nobody could check it on their fingers; every
component carries its value, target, weight and contribution so a manager
can. Targets are the spec's own Stage-2 bands, seeded into the settings
registry effective-dated and outlet-overridable — re-opening last month
scores against last month's targets (D9). Component weights are registry
keys too: net/day 0.35, orders/day 0.25, Mon–Wed share 0.15, phone capture
0.15, AOV 0.05 (the spec's own instruction — growth is bill count, not
ticket size), discount control 0.05.

**Verified against the real trades, not fixtures**: NT01's actual August
reads ₹13,990/day (amber against the ₹18k target), 42% phone capture (up
from the 31% baseline), pillar 71.1 dragged down by phone capture — matching
a hand-run SQL check to the rupee, and matching the exact growth story the
spec tells. The test suite pins the spec's published baseline as a
regression case. One live-found bug pinned by a test: asyncpg returns sums
as Decimal, coerced at the service boundary so the pure module never sees
one.

**Still no blended health number.** Two pillars is half the evidence; the
figure would change the day the next pillar lands with nothing about the
outlet having changed. D14's argument does not expire until the card is
mostly real.

**The narrated digest applies the extraction rule to prose**: code computes
every number, the model narrates ONLY the facts list it is handed — the full
table prints directly beneath the paragraph, so an invented figure is
instantly visible as a lie. Zero-valued facts are omitted so the model
cannot dramatise a non-event. Advisory to the bone: no key, a 429, an
outage — the digest sends without the paragraph and the job detail records
"skipped"; a morning email is never hostage to a model. Toggleable per
outlet via jobs.digest_narrative. The preview model spends thinking tokens
inside maxOutputTokens — 500 truncated mid-word, 2000 does not; found live,
like everything else in this file.

---

## D20 — Consumption is derived, anomalies land on the board that exists (P13)

**Apparent consumption, and the adjective is doing honest work.** A window
spans two consecutive confirmed counts: from_qty + requisitioned − to_qty.
There is no goods-received flow yet, so finalised requisition quantities
stand in for deliveries — the assumption is recorded on every row's detail,
and a window with no requisition data keeps apparent_consumption NULL rather
than defaulting receipts to zero. When a goods-received flow lands, the
column tightens and the name stops hedging. Everything in stock_consumption
is derived and freely recomputable; deleting it loses nothing but time.

**Three checks, the simplest versions that cannot lie** (spec section 6):
an unchanged non-zero count across N consecutive counts (a streak, not a
statistic — repeated zero is a purchasing problem and exempt); a share of
requisition lines already carrying the padding flag (no re-judgement, just
"does it keep happening"); and a z-score of the latest consumption-per-cover
against the item's own trailing distribution, refusing to compute against
fewer than five windows because a z-score against four points is a coin
toss. All thresholds are registry keys, outlet-overridable.

**Findings are exceptions, not a new inbox.** They land on sop_exceptions
with severity low/medium, deduplicated against open ones — the manager
already works that queue, and a second inbox is where flags go to die. The
nightly job (05:45, after materialisation closes the trading day) is
idempotent and owner-runnable.

**The live first run reported 0 windows and raised nothing** — the correct
answer for a database with no confirmed counts yet. The machinery arms
itself the day the first count is confirmed; the tests prove the rest with
planted anomalies against a real database.

---

## D21 — Item names per bill, quantities honestly null (P14)

**Decision.** The Order Listing report is ingested by a second versioned
adapter, `petpooja.listing.v1`, through the SAME upload endpoint — the file
is told apart by its header row (`Invoice No.` = master, `Order No.` +
`Items` = listing), because the uploader should not have to know Petpooja's
report names. It writes item names into `sales_order_items` joined to the
master bill on `Order No.` = `external_bill_no`, with quantity and unit
price made nullable by migration 0017 and left NULL.

**Why null, not zero.** The export carries names only — "Veg Ramen" once on
a bill could be quantity three. `qty not null default 0` (the speculative
0005 shape) would have recorded knowledge nobody has; NULL is the same
honest spelling `apparent_consumption` uses in 0016. The visible unit
everywhere is "on N bills", never units sold.

**Bills come only from the master.** An order the master has not ingested
gets a warning, not an invented `sales_orders` row — a names-only export
would give it a net of nothing and a date of maybe. The listing decorates;
it never creates. Items for an order are wholly owned by the latest listing
that mentioned it (delete-and-insert), so a corrected export also removes.

**This export's trap is split payments, not summary rows**: a bill paid
part-UPI part-cash repeats its Order No. in rows carrying only Grand Total
and Payment Type (no Created, no Items — the discriminator). Recognised and
counted, not warned about and never a duplicate.

**PII discipline is tighter than the master's.** The file carries customer
names, phones and addresses in the clear; none of them leave the parse loop,
not even hashed — the master already owns customer identity and the fewer
places personal data passes through, the fewer places it can leak from.

**Live verification, 29 Aug 2026**: all 89 orders of the real export matched
ingested master bills; the listing's My Amount sum equals the master's GROSS
for those bills to the paisa (the ₹12.90 gap to net is exactly the
discounts); 23/25 item names matched the Item Wise report, the two misses
being Petpooja's own short-name aliases (recorded in OPEN_ITEMS).

---

## D22 — All four pillars produce, and the blend is real (P15)

**Decision.** The Inventory discipline and Guest & throughput pillars go
live with their gaps declared, and the health card's headline becomes the
blended score spec section 5 always intended:

    health = sum(pillar score x weight) / sum(weight), over MEASURED pillars

Renormalised over what was measured: an outlet with no confirmed counts yet
has its inventory pillar named in `unmeasured` and left out of the
denominator — never padded with a zero, because "not measured" and "failed"
must not be the same number. The comparison row (`/dashboard/outlets`) now
shows blended health too, with each pillar's own score alongside. D14
retires; its reasoning stood until there were four numbers to blend.

**Component honesty, three kinds.** A pillar component is `live` (scored,
weighted), a `monitor` (shown, never scored — peak-hour share, which the
spec explicitly says to watch, not target), or `pending` (declared with the
reason there is no number: recipes, a wastage log, table numbers, a ratings
source). Weights renormalise over the live set, so a pillar is not punished
for its declared gaps. Shared arithmetic lives in `app/core/pillar_math.py`;
the sales pillar keeps its own identical copy because its numbers and digest
text are pinned and a refactor could only change them by accident.

**What went live, measured from AKIRA's own data (29 Aug 2026):**

- *Inventory*: requisition accuracy (share of finalised lines without the
  padding flag, computed at requisition time) weighted 0.6; stockout
  incidents (items counted at zero on confirmed counts, normalised per 28
  days) weighted 0.4. Live state: unmeasured — arms the day the first count
  is confirmed, same as the anomaly job.
- *Guest*: repeat-customer rate (identified customers seen on 2+ trading
  days) — live baseline 7-9% against a 20% target. Peak-hour share rides as
  a monitor (51% live, matching the spec's 52.9% baseline). Table turns are
  pending because Petpooja's exports carry no table numbers (the Area column
  is empty on all 1,533 real dine-in bills); Google rating is pending for
  want of a source.

**Phone capture stays in the sales pillar** where D19 put it, though the
spec lists it under Guest. Moving it would mean re-cutting shipped default
weights, which silently rewrites historical scores (registry defaults are
not effective-dated — only overrides are). The split is defensible on its
own terms: capture is a till habit scored beside the other till habits;
repeat rate is the guest outcome the habit exists to enable. One metric,
counted once.

**Two bills the same night are not a repeat.** Repeat means seen on two or
more TRADING days — a split order at 21:00 and 23:30 is one visit, and the
business-date rollover keeps the after-midnight half attached to it.

---

## D23 — Forecasting starts boring, and the AI system is a promotion ladder (P16)

**Decision.** Spec 5.1's baseline ships exactly as written:

    forecast(outlet, date) = median(same weekday, last 4 same-weekday
                             trading days)
                           x trend_factor(last 14d vs prior 14d per trading
                             day, clamped 0.8-1.3)
                           x event_multiplier(manual flag)

Pure module (`sales/forecast.py`), refusals over guesses: fewer than two
same-weekday samples is no forecast with the reason; a trend window thinner
than seven trading days on either side holds at 1.0 and says so; covers are
forecast only where the covers history is dense, because Petpooja records
them patchily and null beats invented diners. The trend is per trading day,
not raw sums — twelve open days against six is not growth.

**A forecast is only a forecast if it was made in advance.** The nightly
job (05:30, after materialisation closes yesterday) stores the horizon's
rows and they are never updated — unique (outlet, target_date, made_on,
model), `on conflict do nothing`. MAPE is computed from those rows against
actuals, headline at horizon 1. The live API view calls the same compute,
so the number a manager sees is the number the job stores.

**Event flags are the human channel.** `forecast_events`: a manager writes
"Durga Puja weekend, 1.3x" before it happens; outlet-specific beats
group-wide; only a global role flags every outlet. Every applied multiplier
is named in the forecast's working.

### The advanced system — designed now, gated by the baseline

The schema is the design: the `model` column plus immutable in-advance rows
make champion-vs-challenger a GROUP BY, not a refactor. The ladder:

1. **`baseline.v1` is the champion** and runs forever, even after it loses —
   a permanently-running shadow is the regression alarm for whatever
   replaces it. Any change to its arithmetic is a new model string.
2. **The gate is the spec's**: no learned model until the baseline has 12+
   weeks of stored MAPE history (so, from ~late November 2026) AND a
   challenger genuinely beats it. "Beats" means: lower day-ahead MAPE on
   stored in-advance forecasts for four consecutive weeks — never on
   backtests alone, which flatter every model.
3. **The first challenger is gradient-boosted trees, in-process** (sklearn
   HistGradientBoosting — already-installed ecosystem, no new service) over
   deterministic features: weekday, lag-1/-7/-14 nets, trailing medians,
   days-since-open, event flags, an Indian holiday table. It runs inside
   the same nightly job and writes rows under `gbt.v1`. NOT Prophet/ARIMA
   (the spec's own warning: they overfit six weeks of noise) and NOT an
   external forecasting API.
4. **Promotion is a settings key** (`forecast.champion_model`, added when a
   challenger exists), effective-dated like everything else (D9), so which
   model fed requisitions on a given week is answerable forever.
5. **The LLM never predicts a number** — section 6's rule holds. Its two
   forecasting jobs, both Gemini-sized: narrate the forecast in the daily
   digest from computed facts (the narrate() pattern from D19, facts in,
   prose out, None on failure); and PROPOSE event flags — scan a holiday
   calendar or local listings and draft forecast_events rows that a manager
   confirms. A proposed multiplier is a suggestion with a source; a
   confirmed one is a decision with a name on it.
6. **Weather, delivery-platform signals, and covers-based labour planning**
   wait until the MAPE history can prove they pay for their complexity.
   Each lands as features on the challenger, never as a new model family.

What the forecast feeds, in order: requisition suggestions (forecast covers
x recipe quantities, once recipes exist — the same gap the inventory
pillar's variance component waits on), then labour scheduling. Until then
it feeds the manager's eye, which is where trust gets built anyway.

---

## D24 — Recipes, true units, and theoretical consumption (P17)

**Decision.** Three connected pieces, in dependency order:

**1. The quantity source is the Item Report: Day Wise** — a third adapter,
`petpooja.itemdays.v1`, behind the same header-detected upload button. It
is the ONE Petpooja export with true units per menu item per day (the
Order Listing has names without quantities, D21; the Item Wise report has
quantities without days). Rows land in `sales_item_days` upserted on
(outlet, report_date, item_name). **`report_date` is Petpooja's own day
grouping, stored verbatim** — this export has no timestamps, so the 05:00
business-date rule cannot be applied to it. At a consumption window's
edges the two groupings can disagree by one late-night day; known and
bounded, and windows span days so the error does not compound.

**2. Recipes are brand-level config**, like the catalogue (D10): one menu
across outlets. The key is the menu item name AS PETPOOJA PRINTS IT — that
string joins to both sales tables. The unmapped worklist (sold names
without recipes, ordered by units sold) is the honesty gap made visible:
theoretical consumption counts only mapped dishes. Lines are replaced
wholesale on save; a corrected recipe must also REMOVE the ingredient it
no longer uses. Reads for management, writes for admin, audit on every
save and delete.

**3. Theoretical consumption is a third independent claim per window**,
beside the count delta and the requisition stand-in:

    theoretical = sum over window days of (units sold x recipe qty per unit)

NULL — not zero — when it cannot honestly be computed: no item-day sales
cover the window, or no active recipe mentions the item. That distinction
carries the whole feature: "no data" read as "zero sales" would flag every
unmapped ingredient as staff meals.

**What this armed:**

- **The fourth day-one anomaly** (spec section 6): apparent consumption
  against a COMPUTED zero theoretical — stock left the shelf while no dish
  whose recipe includes it sold. Fires on the latest window only, lands on
  the exception board deduplicated like the other three.
- **The inventory pillar's variance component**: median absolute
  |apparent − theoretical| / theoretical across the period's computable
  windows, target ≤ 20% (the spec's own threshold). The P15 weights were
  re-cut (0.4 requisition / 0.3 stockouts / 0.3 variance) BEFORE the
  pillar ever produced a live number, so no history moved — the same test
  D22 applied to phone capture, passed this time.
- **The forecast's future feed**: forecast covers x recipe quantities is
  the requisition suggestion D23 promised. Not wired yet — it needs the
  covers forecast to firm up first.

**Live caveat at build time:** no Akira Item Day Wise export exists yet
(the shape was established from an older export of another restaurant),
so the live tables sit empty until one is uploaded — every dependent
surface reads "pending" with its reason, which is the designed cold state.
The restaurant-name guard that used to be missing here is D25.

---

## D25 — The export has to say which restaurant it is (P19)

Every Petpooja report carries a `Restaurant Name:` line in its preamble. All
three adapters read it and threw it away, so an export from a different venue
— a second restaurant in the same Petpooja account, a file forwarded by the
wrong person — ingested silently against whichever outlet the uploader picked.
Afterwards nothing about the rows looked wrong. It just read like a quiet
month.

**The expectation is a setting, not a constant.** `sales.petpooja_restaurant_name`,
outlet-overridable, empty by default. D9's append-only settings table already
gives it history, an admin screen and a per-outlet override, so this needed no
new table and no deploy to arm.

**Empty means unarmed.** Shipping a guard that blocks every upload until
somebody configures it would have taken sales ingestion down on the day it
landed. So the check returns early when the setting is blank — but the name
the file claimed is recorded on `data_uploads.restaurant_name` either way,
which is what turns arming it into a copy-paste rather than a guess, and what
lets `select distinct restaurant_name from data_uploads` answer "has anything
foreign ever landed here" across the whole history rather than only since.

**Armed, it fails closed.** A file with no `Restaurant Name:` line at all is
refused too. "Cannot be checked" is not "checked and fine", and a stripped
preamble must not be the way in.

**Two checks, not one.** At request time, before the bytes reach Storage and
before a row exists, so a refused file leaves nothing to clean up and the
person holding the wrong spreadsheet hears about it while they still remember
sending it. Then again at parse time, because a re-parse reads a file that was
accepted under whatever the setting said *then*, and must obey what it says
*now*. The second check is the one that has no request to reject into: it
lands as `status = 'failed'` with the reason on the row.

**Matching is whole-string, after folding case and whitespace.** "AKIRA Ramen"
and "akira  ramen" are one venue; "Akira" and "Akira Ramen Bangalore" are not.
A prefix or substring match would accept exactly the file this exists to
refuse, so the normaliser deliberately does less than it could.

**What it cannot do, stated plainly:** it cannot catch New Town's export filed
against the other outlet. Both outlets sit under one Petpooja account and
print the same name — the same reason the uploader picks the outlet by hand in
the first place. Nothing in the file distinguishes them. This guard is about
the wrong *restaurant*, never the wrong *outlet*.

**Found on the way in:** `_decode` in `settings_value.py` ran `json.loads` on
any value that came back as a `str`, and the settings router had two more
copies of the same line. That works for numbers and booleans — a decoded one
is not a `str` at all — and every setting anyone had ever changed live was
numeric or boolean, so it had never fired. The first string setting broke it:
resolving raised, and `GET /settings` returned 500 for *every* key, not just
the one that was set. The four `jobs.*_time` settings were sitting on the same
mine. One decoder now, shared, and it is type-aware rather than guessing —
a restaurant called "123" must not resolve to the integer 123, which is the
case `json.loads` gets silently wrong rather than loudly.

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

---

## D26 — Latency is the wire, so spend the wire once (P20)

The API was measured, endpoint by endpoint, against the live database, with
every SQL statement timed from the start of its request. Four facts came out,
and every change below follows from one of them.

**1. The database is in Sydney.** The Supabase project sits in
`ap-southeast-2`. From Kolkata one round trip is 310–320 ms; to Mumbai
(`ap-south-1`) it is 46 ms. Postgres's own work is nothing — the whole
dashboard settings statement executes in 1.3 ms, the SOP counts in 0.35 ms —
so a request's latency is, to the millisecond, its number of round trips
times 315. `pg_stat_statements` confirms it: nothing the app runs averages
over 50 ms server-side except one 240-row list.

**2. A new connection costs 3.5 s, not 152 ms.** The earlier note in
`app/core/db.py` measured a bare TCP handshake. DNS, IPv6, TLS and auth
together are 3.5 s (median of four). `pool_recycle` was 240 s, so every
pooled connection was torn down and rebuilt every four minutes, and whichever
request drew it next stalled for 3.5 s. That is what "the API is randomly
slow" looked like from a browser: not slow queries, a reconnect tax landing
on a different person every few minutes.

**3. asyncpg's caches are per connection.** A statement on a cold connection
costs up to FOUR round trips — parse/describe, then a type lookup for each
enum it has not seen, then execute — and one on a warm connection costs one.
The server had answered asyncpg's type-introspection query 1,231 times. With
a FIFO pool of ten, each request rotated onto the coldest connection, and the
240 s recycle wiped every cache every four minutes anyway. An empty
`/sop/runs/today` — two bytes — took 1,250 ms.

**4. Every authenticated request paid a round trip to learn who was
calling** before its handler did anything. For the many screens that need one
statement of their own, that was half their latency.

### What changed

- **Identity is cached per caller for 60 s** (`deps.py`), keyed on the
  token's subject. Only identities that resolved to a usable caller are
  cached, so a pending, deactivated or unknown account is re-read on every
  request and activation takes effect on the next click. Every write that
  changes who someone is — role, outlets, deactivation, tablet suspension or
  revocation, an outlet going inactive — calls `forget_identity` (or
  `forget_all_identities`) AFTER its commit. The TTL is the backstop for a
  change made outside this process.
- **Independent reads go on the wire together.** `db.read_with(db, fn, ...)`
  runs one read on a sibling autocommit session bound to the caller's own
  engine, so `asyncio.gather` can send several at once. The health card's
  five aggregates plus its trend now leave together; so do the review
  screen's items and verdicts, and the live forecast's three inputs. The
  sibling is bound to the caller's engine on purpose: a test that hands in a
  session on a throwaway database fans out on that database, never on
  `DATABASE_URL`.
- **One settings statement for the whole card.** The four pillars each
  resolved their own targets — four trips asking the same function about the
  same outlets at the same instant. `_ALL_SETTING_KEYS` resolves them once;
  each pillar service exposes `targets_from(values)` so the digest, which
  still wants one pillar at a time, builds the same dataclass through the
  same function. A test pins the two routes to each other.
- **The pool keeps its connections and keeps them warm.** `pool_recycle`
  1800 s with server-side TCP keepalives; `pool_use_lifo=True` so the
  connection just returned is the next one handed out and a few stay hot;
  six connections opened together at startup (`warm_pool`) so the first
  requests after a deploy do not each pay 3.5 s. The pool grew to 10 + 15 to
  cover the fan-out.
- **Responses are gzipped** above 1 KB. The browser link is the other long
  wire, and a 100-row list is 45 KB of JSON.

### What it did, measured from Kolkata against Sydney

| Endpoint | Before | After | Trips now |
|---|---:|---:|---|
| `/dashboard/outlet-health` | 3,381 ms | 942 ms | 3 |
| `/dashboard/outlets` | 3,074 ms | 940 ms | 3 |
| `/sop/runs` (100 rows, 49 KB) | 1,566 ms | 324 ms | 1 |
| `/sales/orders` (47 KB) | 1,667 ms | 316 ms | 1 |
| `/sales/forecast` | 1,233 ms | 316 ms | 1 (3 in parallel) |
| every other list or lookup | 616–930 ms | 310–330 ms | 1 |

Thirty of thirty-nine GET routes are now exactly one round trip. The
randomly placed 3.5 s stall is gone.

### What it cannot do, and the decision that follows

**A response cannot be faster than one round trip, and one round trip to
Sydney is 310 ms.** The sub-90 ms target is unreachable from this topology
by any amount of code. It is reachable by moving: a Supabase project in
`ap-south-1` is 46 ms from Kolkata, and an API deployed beside it makes
every database call a LAN call, leaving the browser one 46 ms hop per
request. Supabase does not move projects between regions; the path is a new
project in Mumbai, `pg_dump`/restore (downtime = copy time; this database is
small), re-run the seed of auth users, repoint `.env`. That is the next
infrastructure decision and it is the owner's — it is filed in
`OPEN_ITEMS.md`.

Not done, on purpose: no single mega-statement for the card (the digest
shares the per-pillar statements, and two copies of the same SQL would
drift); no Redis or external cache (one process, one dictionary, a TTL);
no `pool_pre_ping` (a round trip per request to guard against what
keepalives and a sane recycle already prevent).

---

## D27 — The grant posture is one migration, not the sum of many (P21)

Moving the database to Mumbai (D26's recommendation, done 5 Sep 2026) was a
`pg_dump --no-privileges` and a restore, exactly as Supabase documents. The
policies came across — they are schema. The grants did not — they are
privileges — and the new project's platform default then gave `anon` and
`authenticated` ALL on every restored table. RLS was forced throughout, so
nothing was reachable; but SECURITY.md row 3's promise, "anon holds nothing,
authenticated holds SELECT only", had quietly stopped being true, and the
only thing that noticed was a catalog query run by hand.

Until then the posture existed as one statement per table, added in
whichever migration created the table (0007 swept the first twenty-five;
0010–0019 each did their own). That is exactly the shape that loses to a
restore, and to the first developer who forgets.

**Decision:** `0021_grant_posture.sql` states it once, over the catalog —
every public table, sequence and function, whatever their number — and sets
DEFAULT privileges for the migrating role so an object created tomorrow
starts with the posture rather than needing to remember it. Idempotent, so
it is also the thing to run after any future restore. The per-table
statements in earlier migrations stay as history; redundant now, not wrong.

Not done: altering `supabase_admin`'s default privileges — `postgres` is not
allowed to, and nothing of ours is created by that role. Verified on Mumbai:
`test_rls.py`'s catalog assertions pass by hand, and a throwaway table
created after 0021 came up anon-nothing / authenticated-SELECT.

---

## D28 — One OpenAI-format client instead of a vendor per provider (P22)

The owner asked for a zero-cost replacement for Groq that speaks the OpenAI
format. Groq had two problems: its key reached this project through a chat
transcript (OPEN_ITEMS carried "rotate it" for ten epics), and its free tier
was 8,000 tokens a minute, which two photographs mostly exhaust. Its only
virtue was that it spoke the OpenAI chat-completions format — and that
turned out to be the whole answer.

**Decision:** `AI_REVIEW_PROVIDER=openai` (and `STOCK_EXTRACT_PROVIDER=openai`)
is ANY endpoint that speaks that format, chosen by `OPENAI_COMPAT_BASE_URL`.
The default is Gemini's own compatibility layer,
`https://generativelanguage.googleapis.com/v1beta/openai`, which reuses the
`GEMINI_API_KEY` and `GEMINI_MODEL` already configured when the compat key
and model are left blank — so the zero-cost path needs no new secret. The
same code serves OpenRouter's `:free` models or a local Ollama, which costs
nothing at all and sends the photos nowhere, by changing one URL and a key.

**Why this and not just "use Gemini native":** the native Gemini path
exists and stays; it is what the extractor was measured on. But one generic
client is what makes the vendor a configuration value rather than a code
path, and that is what the owner asked for. Strict `response_format:
json_schema` is honoured by Gemini's layer (verified live, 5 Sep 2026:
schema-exact JSON, `gemini-3-flash-preview` echoed, ~3.5 s) and by OpenRouter;
an endpoint that ignores it fails the schema check and yields "no verdict",
never a guessed one.

**What stays true from D13:** one prompt, byte-identical across transports;
the model that actually answered is stored on every verdict; a 429 is a
first-class "no verdict yet", now retried twice with the endpoint's own
`Retry-After` before giving up.

**Trust for extraction is per endpoint, not per format.** Gemini through its
compat layer is the same model that was measured to row-align handwriting
correctly, so it is trusted like native Gemini. Any other OpenAI-compatible
endpoint is unmeasured, and the one non-Gemini vision model that WAS measured
shifted values onto neighbouring rows at 0.9 confidence; so
`counts_service` forces every line from a non-Gemini endpoint into human
review until `scripts/eval_extractor.py` says otherwise.

Free-tier facts as of Sep 2026, for whoever tunes this next: Gemini 3 Flash
about 1,500 requests/day and 10/minute on the free tier after Google's
December 2025 cuts; OpenRouter `:free` 50 requests/day on an unfunded
account, 1,000/day after a one-time $10 top-up; Ollama unlimited but needs
a machine with the model on it, which the API host today does not have.
Groq is gone from config, code and `.env`; the leaked key should still be
revoked in Groq's console.

---

## D29 — Attach rates come from the category report, checked against the bills (P22)

The owner asked for KPIs a business decision can rest on and named the
beverage-per-ticket and dessert-per-ticket rates. The bill-level data
could not give them: the Orders Master has no items, the Order Listing has
names without quantities and only for the weeks it was exported (D21).
Two Petpooja exports the owner supplied on 5 Sep 2026 could.

**Sales Report: Category Wise** says, for a period, how many BILLS carried
each category. That IS the attach rate's numerator. Its trap: the Total
row's "No. of Orders" is the SUM of the per-category counts (1,915 for 588
bills), so the denominator is not in the file — it comes from
`sales_orders` for the same period. Petpooja counts on its own calendar
dates and `business_date` rolls at 05:00, so the edges can differ by a
late-night bill; the response names the period it divided by.

**Item Wise: Sales Report** — dismissed for bills in D15, rightly — carries
the menu's own taxonomy: every item under its category with Petpooja's
code. Loaded into `menu_items`, keyed by the printed name like recipes
(D24), it turns the Order Listing's names-per-bill into a per-bill category
join. That gives the same rates a second way, exactly, over the bills whose
items were uploaded.

**Decision:** report both, labelled. The Petpooja count is the number to
steer by (it covers every bill); the measured one is the check on it. On
the real data they agree within a few points on every category
(Refreshments 53% reported vs 51% measured over 89 bills; Dessert 42 vs 52;
Gyoza 71 vs 63; Ramen 70 vs 79), which is what makes the reported number
trustworthy rather than merely official.

What it says about AKIRA, 17 Jul to 5 Sep 2026, 588 bills: a drink is on
53% of bills, a dessert on 42%, gyoza on 71% — and ramen on only 70%, in a
ramen restaurant. Yakitori 52%, karaage 20%, donburi 18%. Those are the
levers; targets for them are deliberately NOT set yet (no settings keys),
because a target set on the day the baseline was first seen is a guess
dressed as a goal. Two more exports and a month of watching first.

**Left open, on purpose:** two names on bills ("Donburi Chicken", "Donburi
Mushroom") are Petpooja's short forms of menu items the map knows under
their full names. A menu alias — the same treatment `inventory_item_aliases`
gives stock sheets — is the fix and is filed in OPEN_ITEMS; until then the
measured Donburi rate is blank rather than wrong. A period's category rows
are REPLACED as a set on re-upload, because Petpooja recomputes the whole
report and a category that vanished must not linger.

**Addendum, same day.** The alias table exists (0023, `menu_item_aliases`: one
spelling to one item, case-insensitive, admin-entered, audited). The owner named
the four spellings; the measured Donburi rate went from blank to 16% against
Petpooja's 18%, and no bill name is unmapped. The Sales page offers a "map this
name" control the moment a new unmapped spelling appears, so the next menu
change costs a dropdown, not a ticket.

## D30 — Production is a contract the process enforces, on one machine (P23)

**Context.** Every feature the owner asked for is built; what was missing was
the posture to run it for real. The gaps recorded in SECURITY.md were: no
rate limit, no headers, a salt with a dev default that nothing refused,
`/docs` open, no deployment target, no backup routine on a free tier that
keeps none, synthetic data that has to go on a specific day, and staff who
had not been told their photographs go to a vendor.

**Decision, in five parts.**

1. **`ENV=production` refuses dev defaults.** `Settings.production_problems()`
   returns every violation at once and `lifespan` raises before the pool
   warms, so a bad deploy never takes traffic and the platform keeps the
   previous release. Warnings were rejected: the salt's default had been
   "labelled so a config dump shows it" since P10 and nobody reads config
   dumps at 05:00.

2. **One machine, in-process everything.** The scheduler already forbade a
   second instance. Rather than build a shared store for a rate limiter and
   an identity cache to make replicas possible, the deployment states the
   constraint out loud: one uvicorn worker, one Fly machine, auto-stop off,
   `--ha=false`. The limiter is a token bucket per bearer token (per address
   when anonymous), 600 a minute with burst, 429 as problem+json. It is a
   safety valve for the connection pool, not a security boundary; the PIN
   lockout and Supabase Auth remain those.

3. **Fly.io `bom`, because it is ap-south-1.** Region was the whole reason
   for P21; a host in Singapore would give half of it back. Fly has no free
   tier (about $3/month); Cloud Run in `asia-south1` scales to zero, which
   kills the scheduler, and keeping one instance warm costs more. The web
   app is static and goes on any CDN; Cloudflare Pages recommended because
   its free tier permits commercial use and Vercel's does not.

4. **Go-live is a script with a dry run, not a checklist of SQL.**
   `scripts/prod_cutover.py` removes exactly the synthetic set (DEV02, the
   seeded Safuipara history before a date, the `@akira.test` accounts and
   their device rows), keeps everything real (sales, counts, settings,
   templates, audit), audits itself, and refuses to execute unless an active
   owner with a real email exists — the failure it guards against is
   deleting the only login. It is rehearsed by a test that runs the real
   cascades inside a rolled-back transaction. The owner's standing
   instruction ("do not deactivate sample data till we go to prod") is
   honoured by the default being a report.

5. **Backups are a script run by a person.** `scripts/backup_db.py` is the
   region move's two-dump recipe made repeatable, with a `pg_restore --list`
   proof. A scheduled job was considered and rejected: a GitHub Action would
   put a database dump in a public repo's artifact store, and the API
   machine has no durable disk. Weekly by hand, moved off the laptop, is
   honest about what this tier is.

**Rejected.** A Redis-backed limiter (a second service for a one-machine
app); Cloudflare Workers/Access in front of the API (a good idea later, a new
vendor now); disabling `/docs` everywhere (the local developer loop uses
them); deleting synthetic data "softly" (the dashboard's trend would carry
eight weeks of invented history into production).

**What this does not do.** It does not deploy anything — that needs the
owner's accounts — and it does not rotate the two secrets that have been
through chat. Both are the first and last items on RUNBOOK_DEPLOY's list.
