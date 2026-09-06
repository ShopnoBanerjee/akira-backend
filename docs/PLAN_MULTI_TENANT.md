# Plan: multi-tenant AKIRA Ops (P26)

Status: **for the owner's review, 7 Sep 2026. Nothing here is built.** When
approved, the decisions move into `DECISIONS.md` as D33 and the phases become
epics. Questions asked and answered before this was written are recorded in
section 1 so nothing below is an assumption.

---

## 0. In one paragraph

Today the system is one brand: one set of SOP templates, one inventory
catalogue, one menu map, one settings table, and every outlet and person
belongs to it. The change introduces a top level, the **organisation**, and
makes everything that is brand-level today organisation-level. Shopno becomes
the **platform administrator**, the only person who creates organisations and
their first owner login; nobody can sign themselves up. An organisation owns
its outlets, and each outlet its tablets and staff, exactly as now. Onboarding
a new organisation is a wizard: name it, add outlets with their Petpooja
restaurant names, upload the two Petpooja reports that define the menu map for
each outlet, register tablets, add people. Owners and the platform
administrator must use an authenticator app to sign in. AKIRA becomes
organisation number one with everything it already has.

---

## 1. Decisions already made (asked, not assumed)

| Question | Answer (7 Sep 2026) |
|---|---|
| Who administers the platform; how do owners get logins? | Shopno is platform admin. He creates an organisation and its first owner login. No self-signup. |
| Authentication strength | MFA (TOTP authenticator app) **required** for platform admin and owners; optional for managers; staff keep the tablet PIN model. |
| What a new organisation must upload to be "ready" | The menu map only: **Item Wise** and **Category Wise** reports per outlet. Orders Master and Order Listing are optional at onboarding. |
| Where a new organisation's templates and catalogue come from | Cloned from a **platform starter kit** Shopno curates (AKIRA's current templates, categories, catalogue, default settings). The organisation then owns its copy. |
| Platform admin's reach inside an organisation | **Read everything, manage owners only.** Can open any organisation's screens read-only and create/reset its owner logins. Cannot approve, change settings or act as staff. Every such read is audited. |
| Build order | Full plan document first (this), then phase 1. |

Still open, decide before phase 1 starts (section 9).

---

## 2. Petpooja: what exists, what we use, what onboarding needs

**No pull API for sales.** Petpooja's partner API is for aggregators: fetch or
push a menu, push online orders in, receive order-status callbacks, toggle
item stock. Access is by commercial agreement and scoped by restaurant id.
Nothing in it returns bills, history or item-level sales. Onboarding therefore
stays export-based, which is what the ingester already does.
([api-evangelist/petpooja](https://github.com/api-evangelist/petpooja),
[Petpooja integrations](https://www.petpooja.com/poss/restaurant-integrations))

**Reports are Excel downloads**, "80+" of them, optionally emailed on a
schedule; "Dynamic Reports" is a paid add-on for presentation, not extra
data. ([Reports & analytics](https://www.petpooja.com/poss/reports-and-analytics),
[Dynamic reports](https://blog.petpooja.com/poss/restaurant-business-dynamic-reports/))

Every export carries a three-row preamble ending in `Restaurant Name:`. That
line is already checked against `sales.petpooja_restaurant_name` (D25). In a
multi-tenant system it becomes the outlet's identity: the wizard records each
outlet's Petpooja restaurant name, and every later upload is matched to the
outlet by it before a row is written.

### 2a. The report matrix

| Petpooja report | Adapter today | Columns consumed | Role in onboarding |
|---|---|---|---|
| Item Wise Sales Report | `petpooja_itemwise` | Category, Item, Qty., net | **Required.** Builds `menu_items` (name → category), the map every attach rate and alias resolves against. |
| Sales Report: Category Wise | `petpooja_categories` | Category, No. of Orders, Total Items Ordered, discount, tax, sales | **Required.** The reported attach rates per category for the period. |
| Orders Master Report | `petpooja_orders` | Invoice No., Date, Payment Type, Order Type, Status, Area, Persons, Phone (hashed), Discount, Tax, Net | Optional at onboarding; the sales pillar, AOV, channel mix, forecasting, dayparts. |
| Order Listing | `petpooja_listing` | Order No., Items | Optional; the per-bill measured attach rate and menu aliases. |
| Item Report: Day Wise | `petpooja_itemdays` | Item, Date, Qty. | Optional; theoretical consumption via recipes. |
| Sales Summary: Hourly | none | | Later: dayparts direct from Petpooja instead of derived from bill timestamps. |
| Payment Type summary, Discount report, Cancelled/void orders, Tax report | none | | Later candidates; Orders Master already carries payment type and discount per bill, so these add reconciliation rather than new KPIs. |
| Customer / CRM reports | none | | Never: raw PII the app deliberately does not hold (SECURITY #11). |

"Ready" for an outlet means: restaurant name recorded, Item Wise and Category
Wise ingested for at least one period, at least one tablet registered, at
least one person with a PIN. The wizard shows the four as a checklist.

---

## 3. The target model

```
platform admin (Shopno)
└── organisation (tenant)                 e.g. "AKIRA", "Some Other Restaurant Group"
    ├── owner(s), ops manager(s)          organisation-wide roles
    ├── SOP templates, categories         organisation-level content
    ├── inventory catalogue, recipes      organisation-level content
    ├── menu items, aliases               organisation-level content
    ├── settings (organisation scope)     was "global"
    └── outlets
        ├── outlet manager(s), shift leads, staff   memberships, as now
        ├── tablets (device accounts)               as now
        ├── outlet settings overrides               as now
        └── runs, sales, counts, exceptions…        as now, unchanged
```

**Roles.** One new value in `user_role`: `platform_admin`, ranked above
`owner`. A platform admin has `organisation_id = null`. Every other profile
belongs to exactly one organisation. The existing rank rule (nobody grants a
role at or above their own) extends naturally: only a platform admin can
create an owner; an owner cannot create a platform admin.

**Isolation rule, stated once.** Every row that is not a platform row belongs
to exactly one organisation, directly (a column) or through its outlet or its
profile. No query crosses organisations except a platform admin's audited
read. This is enforced three times, as today's outlet rule is: in the API
guards, in RLS, and in tests that act as the attacker.

---

## 4. Schema changes (migration 0025, append-only)

### 4a. New tables

```sql
create table organisations (
    id            uuid primary key default gen_random_uuid(),
    slug          text not null unique,        -- 'akira'; immutable, used in storage paths
    name          text not null,               -- 'AKIRA'
    is_active     boolean not null default true,
    onboarded_at  timestamptz,                 -- set when the wizard's checklist is complete
    created_by    uuid references profiles (id) on delete set null,
    created_at    timestamptz not null default now(),
    updated_at    timestamptz,
    deleted_at    timestamptz
);
```

`platform_audit` is not a new table: `audit_log` gains `organisation_id` and
platform-admin reads are recorded there with `action = 'read'` (one new enum
value) so an organisation can see when the platform looked.

### 4b. `organisation_id` added, by table

| Table | Change | Backfill for AKIRA |
|---|---|---|
| `outlets` | `organisation_id not null`; `unique (organisation_id, code)` replaces `unique (code)` | organisation 1 |
| `profiles` | `organisation_id` nullable (null = platform admin); index | organisation 1 for all existing rows |
| `checklist_templates`, `sop_categories` | `organisation_id` nullable: **null = platform starter kit**; `unique (organisation_id, key)` for categories | organisation 1 |
| `inventory_departments`, `inventory_categories`, `inventory_items`, `inventory_item_aliases` | same pattern; per-org uniqueness on keys, names and aliases | organisation 1 |
| `menu_items`, `menu_item_aliases` | `organisation_id not null`; `unique (organisation_id, lower(name))`, `unique (organisation_id, lower(alias))` | organisation 1 |
| `recipes` | `organisation_id not null`; `unique (organisation_id, menu_item_name)` | organisation 1 |
| `app_settings` | `scope` gains `organisation`; existing `global` rows become `organisation` rows of organisation 1; the registry's "global" reads become "this organisation" | rewrite in place |
| `training_records` | inherits through `profile_id`; no column | none |
| `audit_log`, `job_runs` | `organisation_id` nullable, set from outlet or actor | backfill from outlet |
| everything with `outlet_id` | no change; organisation derived through the outlet | none |

Child tables (`checklist_template_items`, `recipe_lines`, `stock_count_lines`,
run items, review views…) inherit through their parent and get no column.

### 4c. Identity and devices

`outlet_devices` inherits through the outlet. A device account's JWT resolves
to a device, which resolves to an outlet, which resolves to an organisation;
the PIN-identified actor must be a member of that outlet, which already
implies the same organisation.

### 4d. Storage

Buckets stay three (`sop-photos`, `sales-uploads`, `stock-sheets`). Object
paths gain the organisation slug as the first segment:
`akira/<outlet_id>/<business_day>/<run_id>/<item_id>.jpg`. Existing objects
are moved once by `scripts/copy_storage.py` (it already copies by listing;
add a rename mode). The path is still fixed server-side, so the bucket rule
(SECURITY #10, #19) is unchanged.

---

## 5. Security changes

### 5a. RLS helpers (0025 replaces the three from 0007)

```sql
auth_profile_role()        -- unchanged
auth_org_id()              -- the caller's organisation (null for platform admin)
auth_is_platform_admin()   -- role = 'platform_admin'
auth_is_org_admin()        -- owner or ops_manager in their own organisation
auth_outlet_ids()          -- unchanged
```

Every existing policy gains the organisation clause. Shape, for a table with
`outlet_id`:

```sql
using (
    auth_is_platform_admin()
    or exists (select 1 from outlets o
                where o.id = <table>.outlet_id
                  and o.organisation_id = auth_org_id()
                  and (auth_is_org_admin() or o.id = any (auth_outlet_ids())))
)
```

For organisation-level tables: `organisation_id = auth_org_id() or
organisation_id is null (starter kit, read-only) or auth_is_platform_admin()`.

`test_rls.py` gains a second attacker: a valid owner of organisation B
reading organisation A's outlets, templates, menu, people, uploads. The
catalog audit (SECURITY #4) additionally fails any policy that does not
mention `auth_org_id()` or `auth_is_platform_admin()`.

### 5b. API guards

`CurrentUser` gains `organisation_id` and `is_platform_admin`. The identity
SQL joins `organisations` and refuses an inactive or deleted organisation the
same way it refuses a deactivated profile. New guards:

- `require_platform_admin` for `/platform/*`.
- `require_org` (implicit in `current_user`): a non-platform caller with no
  organisation is a misconfiguration and gets 403, never a partial view.
- `can_access_outlet()` checks the outlet's organisation before membership.
  A platform admin passes only for read routes (`GET`); a write from a
  platform-admin session inside an organisation is 403 by construction, in
  one place (`deps.py`), not per router.

The identity cache key stays the JWT subject; the cached row carries the
organisation, and `forget_all_identities()` fires when an organisation is
deactivated.

### 5c. MFA

Supabase Auth TOTP, which `supabase-js` 2.112 supports (`auth.mfa.enroll`,
`challenge`, `verify`). Enforcement is server-side: the access token carries
`aal` (`aal1` after password only, `aal2` after a TOTP challenge). The API
adds `TokenClaims.aal`, and for any caller whose role is `owner` or
`platform_admin`, every route except `/users/me`, `/auth/mfa/*` and the
probes requires `aal2` (403 `mfa_required`, a new problem type). The web app
routes such a session to an enrolment screen (QR code, verify once) or a
challenge screen (six digits) before the shell opens. Managers may enrol
optionally from their profile. Recovery: a platform admin can clear an owner's
factors (audited); an owner can clear a manager's. Device accounts and PINs
are untouched: MFA is for individual logins only.

Two settings in Supabase's dashboard are part of this phase and are the
owner's to flip: **disable email signups** (Authentication → Providers →
Email → "Allow new users to sign up" off) and **MFA enabled** (Authentication
→ Multi-Factor). The self-signup path in `deps.py` that today creates a dormant
profile is removed; an unknown subject becomes a plain 403.

---

## 6. Platform administration (phase 2)

Routes under `/platform`, `require_platform_admin`:

| Route | Does |
|---|---|
| `GET /platform/organisations` | list with outlet count, people count, onboarded_at, last activity |
| `POST /platform/organisations` | create organisation **and** its first owner: `{name, slug, owner_email, owner_full_name}`. Creates the auth user with a generated password returned once, or sends an invitation when SMTP exists. Clones the starter kit (phase 3). |
| `POST /platform/organisations/{id}/owners` | another owner login |
| `POST /platform/organisations/{id}/owners/{profile}/reset` | password reset link or MFA clear; audited |
| `PATCH /platform/organisations/{id}` | activate / deactivate (deactivation logs every user out on their next request) |
| `GET /platform/organisations/{id}/view` | the read-only pass-through: the platform admin's own JWT plus an `X-Organisation` header selects the organisation for GET routes; each request writes an `audit_log` row `action=read` |

Web: `/platform` shell for the platform admin with an organisations table,
a create form, and an "open as read-only" button that sets the header for the
session and paints a banner "Viewing AKIRA as platform admin, read only".

---

## 7. Onboarding wizard (phase 2)

Runs for an owner whose organisation has `onboarded_at is null`; the training
tour (P24) runs after it.

1. **Organisation** — confirm name; logo later.
2. **Outlets** — one or more: name, code, address, map pin, timezone,
   **Petpooja restaurant name exactly as the export prints it** (the wizard
   shows where to find it in a Petpooja export).
3. **Menu map, per outlet** — upload Item Wise, then Category Wise. The
   restaurant-name guard runs on each; a mismatch names both spellings. The
   result is shown: N items in M categories, attach rates for the period.
4. **History (optional)** — Orders Master and Order Listing for as far back
   as the owner wants the dashboard to reach.
5. **Tablets** — register one device per outlet; the wizard shows the device
   login to type on the tablet once.
6. **People** — invite managers (individual logins), add staff with PINs.
7. **Ready** — the four-item checklist per outlet; "Finish" sets
   `onboarded_at`, and the daily jobs start creating runs from the cloned
   assignments the next morning.

Everything in the wizard is the existing screens' endpoints; the wizard is
sequencing and copy, not new business logic. The uploads are the existing
`POST /sales/uploads` with the existing adapters.

---

## 8. Starter kit (phase 3)

Rows with `organisation_id is null` in `checklist_templates`,
`sop_categories`, `inventory_departments`, `inventory_categories`,
`inventory_items`, plus a `platform_default_settings` list in the registry.
They are readable by every organisation (RLS) and editable only by the
platform admin, from the same screens with a "platform library" switch.

`clone_starter_kit(organisation_id)`: copies templates with their items (at
their current version, as version 1 of the copy), categories, departments,
catalogue, and writes the default settings as organisation-scope rows.
Idempotent per organisation (refuses if anything cloned already exists).
Assignments are **not** cloned: outlets do not exist yet at clone time; the
wizard's outlet step offers "assign the standard checklists" per outlet.
AKIRA's current content is copied into the library once by a script, so
organisation 1 keeps its own rows untouched.

---

## 9. Open questions to settle before phase 1

1. **Organisation slug and name for AKIRA**: `akira` / "AKIRA"? The slug is
   permanent (storage paths).
2. **The platform admin account**: `management@simplyakira.com` was created
   as AKIRA's owner with outlet `AKR-SP01`. A platform admin has no
   organisation. Either that address becomes platform admin and AKIRA gets a
   separate owner login, or a second address (e.g. `platform@simplyakira.com`)
   is the platform admin and `management@` stays AKIRA's owner. Recommended:
   the second, so the one account that can create tenants is not also the
   one used every day.
3. **MFA on the existing seeded test owners**: they cannot enrol (no real
   mailbox is fine, TOTP needs no mail) but every tester would need an
   authenticator app. Recommended: enforce MFA only for organisations with
   `onboarded_at` set, so AKIRA's test accounts keep working until go-live.
4. **Organisation limits**: any cap on outlets or people per organisation
   for now? Recommended: none; the free tiers are the cap.
5. **Owner password delivery**: until SMTP exists, the platform admin sees
   the generated password once and passes it on; with SMTP, an invitation
   email. Acceptable?

---

## 10. Phases, tests, effort

| Phase | Scope | Tests | Effort |
|---|---|---|---|
| **P26a Tenancy core** | 0025 migration (organisations, columns, re-keyed uniqueness, RLS rewrite, `platform_admin`, `read` audit action); AKIRA backfilled as organisation 1; `CurrentUser.organisation_id`; guards; identity SQL; storage path prefix and object move; MFA claim enforcement in the API; web: MFA enrol/challenge screens | migrations from zero; `test_rls.py` with the cross-organisation attacker; guard tests per role incl. platform admin read-vs-write; `aal` enforcement tests; identity cache invalidation on organisation deactivate | 2 days |
| **P26b Platform admin + onboarding** | `/platform/*` routes and shell; create organisation + owner; read-only view with banner and audit; the seven-step wizard; readiness checklist; Supabase signup disabled | route tests per platform action; wizard readiness logic; guard-name mismatch path; end-to-end: create a second organisation, upload a synthetic Item Wise + Category Wise, see its dashboard, confirm organisation 1 cannot see it and vice versa | 2 days |
| **P26c Starter kit** | library rows, platform-library editing switch, `clone_starter_kit`, AKIRA content copied into the library, "assign standard checklists" in the wizard | clone idempotence; cloned versions start at 1; an edit in the library never touches a clone | 1 day |

Deploys go through the existing approval gate. P26a's migration runs through
`scripts/migrate.py` like any other; the storage move is a one-time script
run by hand with `--plan` first, like the cut-over.

---

## 11. What changes for AKIRA's users

Nothing visible until P26b, except that after P26a an owner signing in is asked
to set up an authenticator app once (or not, per question 3 above). The
tablets, PINs, checklists, uploads and dashboards behave exactly as now.
`scripts/prod_cutover.py` becomes organisation-aware (it cuts within
organisation 1 only).

---

## 12. Risks recorded

- **One Supabase project for every tenant.** Isolation is RLS and code, not
  separate databases. That is the industry-normal shared-schema model and is
  what the tests attack; it is not per-tenant infrastructure. A tenant that
  needs its own project later is a `pg_dump` filtered by organisation.
- **Free tiers.** Render's single free service and Supabase's free project
  serve every tenant. The keep-alive keeps both awake; the first tenant that
  is not AKIRA is the moment to price the paid tiers (Render $7/month,
  Supabase $25/month).
- **The starter kit is AKIRA's know-how.** Cloning it into another restaurant
  group's organisation gives them AKIRA's checklists. Curate what goes into
  the library (phase 3 makes that a deliberate per-template switch).
- **MFA lockout.** A lost phone locks an owner out until a platform admin
  clears the factor; a platform admin who loses theirs needs the Supabase
  dashboard. Document both in the runbook; consider two platform admins.
- **History rewrite pending.** The public stock-sheet PDF (audit of 6 Sep) is
  unrelated to this plan but should be resolved before the first outside
  tenant is onboarded into a repository anyone can read.
