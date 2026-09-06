# Stage 1 epic plan (revised)

Supersedes the sequence in `docs/STAGE1_PROMPT_PACK.md`. The prompt pack's
E0–E9 assumed a monorepo and a smaller admin surface; both changed. See
`docs/DECISIONS.md` for why.

## Status

**Everything P0–P26a is done, live on Supabase (Mumbai), pushed, and CI is green.**
P25 is the deploy pipeline; switching it on (accounts, secrets,
`DEPLOY_ENABLED`) is the owner's step (`docs/RUNBOOK_DEPLOY.md` §2).
Verify with `git log --oneline | head` rather than trusting this table — it
sat claiming "P2 next" until P17 had already shipped.

### Stage 1 — complete

| Epic | Scope |
|---|---|
| **P0** | Repo scaffold, tooling, CI — both repos |
| **P1** | Schema, RLS, indexes, seed, migration tests |
| **P2** | Auth: JWT verification, roles, shared-tablet PIN, protected routing |
| **P3a** | Admin — organisation: outlets, users, roles, devices |
| **P3b** | Admin — configuration: inventory catalogue, settings, job runs |
| **P4** | SOP template builder, versioning, assignments |
| **P5** | Checklist runner (mobile, offline-tolerant) |
| **P6** | Manager review and approval queue |
| **P7** | Integrity engine, AI photo review, scheduled jobs |
| **P8** | Compliance dashboard |
| **P9** | Sales ingestion skeleton |
| **P10** | Hardening pass |

### Stage 2 — in progress

| Epic | Scope | Decision |
|---|---|---|
| **P11** | Stock count extraction, review, requisitions | D16–D18 |
| **P12** | Sales pillar + narrated digest | D19 |
| **P13** | Consumption windows + section-6 anomalies | D20 |
| **P14** | Order Listing adapter — item names per bill | D21 |
| **P15** | All four pillars + the blended health score | D22 |
| **P16** | Forecasting baseline — median × trend × event | D23 |
| **P17** | Recipes + theoretical consumption | D24 |
| **P18** | Security re-review of the Stage 2 surface | — |
| **P19** | The export has to say which restaurant it is | D25 |
| **P20** | Latency: spend the wire once | D26 |
| **P21** | Move to Mumbai; grant posture as one migration | D27 |
| **P22** | OpenAI-format provider replaces Groq; menu mix and attach rates | D28, D29 |
| **P23** | Production readiness: startup guard, rate limit, headers, Docker/Fly, backup and cut-over scripts | D30 |
| **P24** | Training walkthrough: role tracks, EN/BN, tracked per person, owner-restartable, delegable; mobile nav for /app | D31 |
| **P25** | CI/CD: approval-gated deploy jobs (Fly `bom`, Cloudflare Pages), tracked migrations, post-deploy smoke | D32 |
| **P26a** | Multi-tenancy core: `organisations`, organisation-scoped services and RLS, `platform_admin` (read-only inside tenants, audited), TOTP second factor for owners and the platform, no self-signup, `/platform/organisations` | D33 |

### Next

**P26b** (platform screens, create organisation + owner, onboarding wizard)
then **P26c** (starter kit) — `docs/PLAN_MULTI_TENANT.md` §6–8 and §10.
`docs/OPEN_ITEMS.md` still lists what is blocked on an export or a walk of
the outlet rather than on code.

## Why users are seeded through the Auth API, not SQL

P1 is complete — this section stayed in the future tense long after
`scripts/seed_users.py` had run, and is kept only for the reason behind it.

`profiles.id` references `auth.users`, which Supabase Auth owns. Inserting those
rows directly produces accounts that look right in the table and cannot sign in:
no password hash, no confirmation state, none of the encrypted columns GoTrue
maintains. The script creates them through the Auth Admin API instead, then
inserts the matching `profiles`, `outlet_members` and `outlet_devices` rows.
Credentials land in `.seed-credentials.md`, which is gitignored.

This is also where the shared-tablet model first became real: one device
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
