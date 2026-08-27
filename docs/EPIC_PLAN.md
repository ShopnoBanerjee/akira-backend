# Stage 1 epic plan (revised)

Supersedes the sequence in `docs/STAGE1_PROMPT_PACK.md`. The prompt pack's
E0–E9 assumed a monorepo and a smaller admin surface; both changed. See
`docs/DECISIONS.md` for why.

## Status

| Epic | Scope | State |
|---|---|---|
| **P0** | Repo scaffold, tooling, CI — both repos | ✅ done |
| **P1** | Schema, RLS, indexes, seed, migration tests | 🔨 all but user seeding |
| **P2** | Auth: JWT verification, roles, shared-tablet PIN, protected routing | next |
| **P3a** | Admin — organisation: outlets, users, roles, devices | |
| **P3b** | Admin — configuration: inventory catalogue, settings, job runs | |
| **P11** | Stage 2 opens: stock count extraction, review, requisitions (D17) | |
| **P4** | SOP template builder, versioning, assignments | |
| **P5** | Checklist runner (mobile, offline-tolerant) | |
| **P6** | Manager review and approval queue | |
| **P7** | Integrity engine, AI photo review, scheduled jobs | |
| **P8** | Compliance dashboard | |
| **P9** | Sales ingestion skeleton | |
| **P10** | Hardening pass | |

## P1 remainder: seeding users

Everything else in P1 is done and applied to Supabase. Users are not seeded,
and deliberately not by SQL.

`profiles.id` references `auth.users`, which Supabase Auth owns. Inserting those
rows directly produces accounts that look right in the table and cannot sign in:
no password hash, no confirmation state, none of the encrypted columns GoTrue
maintains. `scripts/seed_users.py` will create them through the Auth Admin API
instead, then insert the matching `profiles`, `outlet_members` and
`outlet_devices` rows.

This is also where the shared-tablet model first becomes real: one device
account per outlet, plus a PIN for each staff member.

## Why P3 is split

The prompt pack's E3 was "CRUD outlets; invite user, assign role; deactivate".
Three later decisions grew it well past one epic:

- **D9** added admin-editable settings with effective-dated history, plus the
  UI to browse and change them.
- **D10** added the inventory catalogue — 151 items, departments, categories,
  and per-outlet par levels.
- **D6** added per-outlet reference photo capture, which needs its own admin
  flow before AI review can work at all.

Rather than let one epic sprawl:

**P3a — Organisation.** Outlets CRUD with geo and geofence. User invite, role
assignment, outlet membership, deactivation. Outlet device registration and
revocation, and staff PIN management. Everything here is about *who* and
*where*.

**P3b — Configuration.** Inventory catalogue CRUD with per-outlet par levels.
The settings screens for scoring, integrity, AI review and jobs, each with its
change history. The job runs table. Everything here is about *how the system
behaves*.

P3a must land before P3b: P3b's per-outlet settings and par levels have nothing
to attach to until outlets and users exist.

## Sequencing note

The prompt pack put E8 (integrity) before E7 (dashboard) because the dashboard's
integrity numbers need the flags to exist. That still holds: **P7 before P8**.

P7 also now carries the AI photo review pipeline (D6), which needs per-outlet
reference photos captured through P3a. Reference photo capture is therefore part
of P3a's definition of done, not P7's.

## Carried assumptions

Listed in `docs/DECISIONS.md` under "Assumptions in force". The two that most
affect sequencing:

- `ops_manager` approves outlet-manager submissions, so the separation-of-duties
  constraint does not block the real closing-checklist workflow.
- Petpooja stays a manual XLSX upload for all of Stage 1, so P9 has no vendor
  dependency and can slip without blocking anything else.
