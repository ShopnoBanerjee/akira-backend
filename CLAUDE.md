# CLAUDE.md — AKIRA Ops Suite API

Constitution for the backend repo. Read this before any schema or module work,
then read `docs/STAGE1_SPEC.md`. When a request conflicts with this file, say so
and ask — do not silently deviate.

**This is one of two repos.** The web client lives in a separate repository,
`akira-frontend` (`../akira-frontend` locally,
`github.com/ShopnoBanerjee/akira-frontend`). This repo owns the database schema,
all business logic, and the OpenAPI contract. See `docs/DECISIONS.md` for why
the split is drawn where it is.

---

## Architecture boundary

- The frontend calls **this API** for all business data.
- The frontend uses the Supabase JS client for exactly three things: auth
  session management, direct-to-Storage uploads using a signed URL that **this
  API** minted, and realtime subscriptions.
- The frontend never queries application tables with the Supabase client.
- This API verifies the Supabase JWT against the JWKS endpoint, connects to
  Postgres directly with the service role, and enforces authorisation **in
  code**. RLS is enabled on every table as defence in depth, never as the
  primary control.

The failure mode this rule exists to prevent is two half-backends. If a feature
seems to want data fetched straight from Supabase in the browser, that is a
signal the endpoint is missing, not that the rule should bend.

---

## Non-negotiable conventions

**BUSINESS DATE.** This restaurant trades past midnight. A trading night
starting 18:00 Saturday and ending 01:30 Sunday is ONE business day.
`business_date = (ts at Asia/Kolkata − 5 hours)::date`. Every dated operational
row stores `business_date`. **Never** group or filter reports by
`created_at::date`. The rollover is expressed in exactly two places —
`app/core/business_date.py` and the Postgres function `business_date(timestamptz)`
— and they are tested against each other. Getting this wrong silently corrupts
every weekend number in the system.

**TIME.** All timestamps are `timestamptz`, stored UTC, rendered Asia/Kolkata.
Outlet-local scheduled times are `time` plus the outlet's `timezone` column.

**MONEY.** Integer paise (`bigint`), column names end in `_paise`. Never float,
never `Decimal` in transport. Format only at the UI edge.

**IDS.** `uuid` primary keys, `gen_random_uuid()`.

**SOFT DELETE.** `deleted_at timestamptz` on all user-facing entities. All
queries filter it.

**AUDIT.** Every mutating service method writes an `audit_log` row with before
and after. No exceptions for "small" edits — an SOP template quietly edited to
remove a step is exactly the event you will need to reconstruct.

**ENUMS.** Postgres enums for closed sets. Python mirrors live in
`app/core/enums.py` and are the source of truth for the API surface; the
frontend derives its copies from the generated OpenAPI schema, so a value cannot
drift. Adding an enum value means: migration, `app/core/enums.py`, re-export
`openapi.json`.

**SEPARATION OF DUTIES.** A checklist run's approver can never be its submitter
— enforced by a Postgres CHECK constraint, not only in the API. Without it the
whole compliance system is theatre.

---

## Layering

Each domain package under `app/domains/<name>/`:

| File | Responsibility |
|---|---|
| `router.py` | HTTP only — parse, validate, delegate, serialise |
| `service.py` | Business logic, transaction boundaries, audit writes |
| `repository.py` | SQL only |
| `schemas.py` | Pydantic request/response models |

Routers never touch the database. Repositories never contain business rules.

Scoring, money and date logic are **pure functions** in `app/core/`, unit-tested
with table-driven tests. Errors use one `AppError` hierarchy rendered as
RFC 7807 problem+json. Never leak SQL or stack traces.

---

## Auth model — shared outlet tablet

AKIRA floor staff share one outlet tablet rather than carrying individual
phones. This deviates from the spec, which assumes individual logins throughout.
The resolution:

- **Managers** (`owner`, `ops_manager`, `outlet_manager`) hold ordinary
  individual Supabase Auth logins and use the `/app` shell.
- **The tablet** holds one Supabase session bound to a single outlet — a device
  account row in `outlet_devices`.
- **Individual staff** identify with a per-person PIN (`profiles.pin_hash`,
  Argon2) to start and submit a run, so `submitted_by` still resolves to a real
  person and the separation-of-duties constraint keeps working.

Constraints on PINs, all of which must hold:

- A PIN authorises floor actions only. It can never reach `/app` management
  endpoints, user administration, or approvals.
- A PIN never mints a Supabase JWT. The device JWT authenticates the request;
  the PIN produces a short-lived actor assertion scoped to one run.
- A PIN is only accepted on a device already authenticated to that staff
  member's outlet.
- PIN attempts are rate limited per device and per profile, and every failure
  is audited.

Approval always requires an individual manager login. A PIN can never approve
anything.

---

## Working rules

- Read `docs/STAGE1_SPEC.md` before any schema or module work.
- Migrations are **append-only**: never edit an applied migration, always add a
  new one.
- Every new endpoint gets a pytest. Every scoring, date and money function gets
  a unit test.
- After any endpoint change, re-run `uv run python scripts/export_openapi.py`
  and commit `openapi.json` — the frontend's types are generated from it.
- Prefer boring, explicit code over clever abstraction. This is a small team's
  internal tool.
- Photo processing, XLSX parsing and scoring never run inside a request path;
  they run as background tasks that record to `job_runs` so a failure is visible
  rather than silent.

## Commands

```bash
uv sync                                    # install
uv run uvicorn app.main:app --reload       # dev server on :8000
uv run pytest                              # tests
uv run ruff check . && uv run ruff format --check .
uv run mypy app
uv run python scripts/export_openapi.py    # regenerate the frontend contract
docker compose up -d db                    # local Postgres 16
```
