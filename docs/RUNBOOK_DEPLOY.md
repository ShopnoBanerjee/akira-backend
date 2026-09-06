# Runbook: production deployment and go-live

How the API and the web app get to production, in the order that works, and
the checklist that turns the development system into the live one. Written
6 Sep 2026 (P23). `docs/RUNBOOK.md` is what to do once it is running;
`docs/RUNBOOK_REGION_MOVE.md` is how the database got to Mumbai.

The shape: **one API machine in Mumbai next to the database, one static
site on a CDN, secrets in the platform and nowhere else.** Everything below
follows from those three.

---

## 0. What "production" means to this codebase

Setting `ENV=production` changes behaviour, not just a label:

| With `ENV=production` | Why |
|---|---|
| The API **refuses to start** unless the configuration passes `Settings.production_problems()` | A missed secret should fail a deploy, not a customer |
| `/docs`, `/redoc` and `/openapi.json` are off | The contract is `openapi.json` in the repo; the live API exposes nothing it does not need |
| `Strict-Transport-Security` is sent | A promise about a hostname that only a real hostname can keep |

The checks, exactly: `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_SECRET_KEY`
and `SUPABASE_JWKS_URL` set, with the JWKS URL under `SUPABASE_URL`;
`PHONE_HASH_SALT` neither default nor under 24 characters; `CORS_ORIGINS`
non-empty, https only, no localhost, no `*`; `SQL_ECHO` off; `SMTP_FROM`
not a placeholder. The failure message lists every problem at once.

Always on, in every environment: a token-bucket rate limit per caller
(`RATE_LIMIT_PER_MINUTE`, default 600; 429 problem+json with `Retry-After`;
probes exempt) and the security headers (`nosniff`, `no-store`,
`X-Frame-Options`, `Referrer-Policy`, `Permissions-Policy`).

**Exactly one API process.** The in-process scheduler, the identity cache
and the rate limiter are all per-process by design. Scale the machine, never
the count. The Dockerfile runs one uvicorn worker; `fly.toml` pins one
machine and turns auto-stop off; `fly deploy --ha=false` is not optional.

---

## 1. Before anything: the owner's own accounts

Today everyone signs in as `owner@akira.test`. That account is deleted at
go-live (section 5), so a real one must exist first.

1. Sign in as `owner@akira.test`, Users screen, invite yourself with your
   real address, role **owner**, both outlets for now.
2. Supabase sends the invitation. **Supabase's built-in mailer is capped at
   two messages an hour and is meant for development.** For invites and
   password resets to be reliable in production, set a custom SMTP under
   Authentication, SMTP Settings in the Supabase dashboard. The same
   credentials can serve the digest (`SMTP_*` in the API).
3. Accept, sign in once, confirm you can open the dashboard.
4. Invite the managers the same way. Staff who only use the tablet do not
   need an invitation: they are created on the Users screen with a PIN.

Also in the Supabase dashboard, Authentication, URL Configuration: set
**Site URL** to the production web origin and add it to the redirect
allow-list, otherwise invitation links land on localhost.

---

## 2. The pipeline (P25): what deploys, and the one-time setup

Both repositories deploy from GitHub Actions. On every push to `main` the
existing checks run; when they pass, a `deploy` job waits in the
**`production` environment until you approve it** in the Actions tab, then:

| Repo | The deploy job |
|---|---|
| akira-backend | applies pending migrations (`scripts/migrate.py --apply`, tracked in `schema_migrations`), asks Render's API to deploy the service and waits for it to report `live`, then curls `/healthz` and `/readyz` and fails if either is wrong |
| akira-frontend | builds with the production `VITE_*` values, uploads `dist` to Cloudflare Pages with wrangler, then curls the site for the app and the CSP header |

Nothing runs until the repository variable `DEPLOY_ENABLED` is `true`, so
merging this pipeline changes nothing by itself. Secrets live in the GitHub
environment, never in the repo. The setup below is yours; about half an
hour, once, and no card anywhere.

**Why Render, and why Singapore.** The owner has no card; Fly.io, Cloud Run,
Koyeb and the rest require one even for their free tiers. Render's free
Docker web service does not. Its closest region to the Mumbai database is
Singapore (about 50 to 70 ms a query, what a laptop in Kolkata measured), so
multi-statement screens will answer in 200 to 300 ms rather than under 100.
Free services spin down after fifteen idle minutes, which would stop the
in-process scheduler: `.github/workflows/keepalive.yml` pings `/healthz`
every ten minutes so it never idles. `fly.toml` stays in the repo for the
day a card exists; the deploy step is the only thing to change.

### 2a. Accounts and tokens

1. **Render.** Sign up at render.com with your GitHub account (no card).
   Dashboard, New, **Blueprint**, connect `ShopnoBanerjee/akira-backend`;
   Render reads `render.yaml` and asks for the values marked `sync: false`:
   `DATABASE_URL` = the **session pooler** with the asyncpg scheme:
   `postgresql+asyncpg://postgres.zvskxgmmlahhybzpcicl:<password>@aws-0-ap-south-1.pooler.supabase.com:5432/postgres`.
   Not the direct `db.<ref>` host: it is IPv6-only and Render has no IPv6
   (first boot proved it: `Network is unreachable` at pool warm-up).
   Session mode (5432) keeps asyncpg's prepared statements working;
   measured 75 ms a query warm through the app's own engine settings.
   `SUPABASE_SECRET_KEY`, `PHONE_HASH_SALT` (generate:
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`),
   `GEMINI_API_KEY`. Apply. Render builds once on creation; that first
   build is fine to let run. Then Account Settings, API Keys, Create API
   Key (keep it), and note the service id from the service's URL
   (`srv-...`). Turn nothing else on: auto-deploy is already off in the
   blueprint.
2. **Cloudflare.** Create an account, then My Profile, API Tokens, Create
   Token, template "Edit Cloudflare Workers" (it includes Pages), leave
   account/zone as offered and IP filtering empty, and note your Account ID
   from the Workers & Pages overview. Do NOT connect the repository through
   the Pages wizard: that path deploys on every push by itself, around the
   approval gate. The pipeline creates the project on its first run.
3. **Supabase.** Project Settings, Database, Connect: copy the **Session
   pooler** string (host `aws-0-ap-south-1.pooler.supabase.com`, port
   **5432**, user `postgres.zvskxgmmlahhybzpcicl`). GitHub runners are
   IPv4-only and the direct host is IPv6-only; port 6543 is transaction
   mode and will not run migrations.

### 2b. GitHub, both repositories

Settings, Environments, New environment `production`, tick **Required
reviewers** and add yourself. Then:

| Repo | Environment secrets | Variables |
|---|---|---|
| akira-backend | `RENDER_API_KEY`, `RENDER_SERVICE_ID` (2a-1), `MIGRATIONS_DATABASE_URL` (2a-3, plain `postgresql://`) | **repository** variable `API_URL` = `https://akira-ops-api.onrender.com` (repository-level because the keep-alive job reads it too) |
| akira-frontend | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` (2a-2) | environment variables `CF_PAGES_PROJECT` = `akira-ops`, `WEB_URL` = `https://akira-ops.pages.dev`, `VITE_SUPABASE_URL` = `https://zvskxgmmlahhybzpcicl.supabase.co`, `VITE_SUPABASE_PUBLISHABLE_KEY` = `sb_publishable_ySp9Uovntyxh9nJ-QuNm-Q_I40WS4y_`, `VITE_API_BASE_URL` = the API URL |

Finally, Settings, Secrets and variables, Actions, Variables, **repository**
variable `DEPLOY_ENABLED` = `true` in each repo. From then on every push to
`main` that passes CI offers you a deploy to approve, and the keep-alive
starts pinging.

### 2c. The first deploy

Approve the backend run first (schema, then API), then the frontend run.
`/healthz` says `"env": "production"`, `/readyz` says `"database": "ok"`;
the site answers with the CSP header. The runs' smoke steps check exactly
that and fail loudly otherwise. If the service restarts in a loop, Render's
Logs tab shows the guard's list: `Refusing to start with ENV=production:`
followed by what is missing.

### 2d. Cloudflare Pages project name

The blueprint and the workflow assume `akira-ops`, which gives
`https://akira-ops.pages.dev`. If that name is taken on Cloudflare, the
first deploy fails at "pages project create"; change `CF_PAGES_PROJECT`,
`WEB_URL`, `CORS_ORIGINS` in Render, and Supabase's Site URL together.

### 2e. Measure

From Kolkata, before this deploy, the dashboard answered in ~250 ms and
single-statement endpoints in 40 to 80 ms, all of it wire. Beside the
database the same screens should be under 100 ms; check with the browser's
network tab on `/dashboard/outlet-health`.

### 2f. Supabase network restrictions (optional)

Project Settings, Database, Network Restrictions: allow only Render's
static outbound addresses (Service, Connect, Outbound), GitHub's runner ranges if you want the pipeline's
migrations to keep working (they change; the pooler accepts them by default),
and your own address for `scripts/backup_db.py`. Do this AFTER the first
deploy is verified, because a wrong entry locks out the API too.

### 2g. As configured on 6 Sep 2026 (the as-run record)

What exists, by name only; no value is written anywhere in either repository.

| Where | Item | Holds |
|---|---|---|
| Render, service `akira-ops-api` (Singapore, free, id `srv-…` in the dashboard URL) | Environment tab | `DATABASE_URL` (session pooler, asyncpg scheme), `SUPABASE_SECRET_KEY`, `PHONE_HASH_SALT`, `GEMINI_API_KEY`; the blueprint's plain values (`ENV`, `PORT`, `CORS_ORIGINS`, Supabase URL/publishable key/JWKS, providers) |
| Render, Account Settings | API key | used only by the backend deploy job |
| GitHub `akira-backend`, environment `production` (reviewer: ShopnoBanerjee) | secrets | `RENDER_API_KEY`, `RENDER_SERVICE_ID`, `MIGRATIONS_DATABASE_URL` |
| GitHub `akira-backend`, repository variables | | `API_URL` = `https://akira-ops-api.onrender.com`, `DEPLOY_ENABLED` = `true` |
| GitHub `akira-frontend`, environment `production` (reviewer: ShopnoBanerjee) | secrets | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` |
| GitHub `akira-frontend`, environment `production` | variables | `CF_PAGES_PROJECT` = `akira-ops`, `WEB_URL` = `https://akira-ops.pages.dev`, `VITE_SUPABASE_URL`, `VITE_SUPABASE_PUBLISHABLE_KEY`, `VITE_API_BASE_URL` |
| GitHub `akira-frontend`, repository variables | | `DEPLOY_ENABLED` = `true` |
| Cloudflare, account `e1f7f46f…` | Pages project `akira-ops` | created by the pipeline's first run; production branch `main` |
| Supabase project `zvskxgmmlahhybzpcicl` | `schema_migrations` | baselined 0001–0024 through the pooler |

Live since that evening: API `https://akira-ops-api.onrender.com` (deployed
by the approved pipeline run, `/readyz` database ok), web
`https://akira-ops.pages.dev` (serving with CSP, HSTS, noindex). The first
frontend deploy job went red on a one-shot header check that has since been
made to retry; the site it uploaded is the live one.

**Rotate at go-live**, because each has been through a chat transcript: the
database password, `SUPABASE_SECRET_KEY`, `GEMINI_API_KEY`, the Cloudflare
API token, the Render API key. Tokens are free to reissue; the database
password and secret key change in the Supabase dashboard and then in Render's
Environment tab and GitHub's `MIGRATIONS_DATABASE_URL`.

## 3. Migrations, from now on

Add a file to `supabase/migrations/` (append-only, next number), commit,
push. The pipeline runs `scripts/migrate.py --plan` then `--apply` before
the API deploys: only files not yet in `schema_migrations` run, each in its
own transaction, recorded only on success. An applied file whose contents
changed fails the deploy on purpose. Locally, against the same database:

```bash
uv run python scripts/migrate.py --plan
```

Mumbai was baselined on 6 Sep 2026 (0001 to 0024 recorded as applied by
hand); `--baseline` is never needed again unless a database is restored from
a dump without the table.

## 4. Custom domains (optional)

`fly certs add api.<domain>` and a CNAME; Cloudflare Pages has its own
custom-domain flow. Update `CORS_ORIGINS` (fly secrets), the three `VITE_*`
variables and `WEB_URL`/`API_URL` in GitHub, the CSP in `public/_headers`,
and Supabase's Site URL together; then push, approve, done.

---

## 5. Go-live: the cut from development data to production

Do this once, on the day, after sections 1 to 3 are verified and a manager
has run a real checklist on the deployed system.

```bash
cd akira-backend
uv run python scripts/backup_db.py                       # a restorable dump first
uv run python scripts/prod_cutover.py --keep-from <first real business date>
```

Read the plan. It refuses to execute unless an active owner with a real
email exists. When the plan is right:

```bash
uv run python scripts/prod_cutover.py --keep-from <date> --execute --confirm AKIRA
```

What it does, in one transaction plus two API passes: removes outlet
`AKR-DEV02` and everything under it; removes Safuipara's checklist history
before the date (the seeded runs); removes every `@akira.test` account and
their device rows; leaves sales, stock counts, settings, templates, the
catalogue, `job_runs` and `audit_log` alone; writes its own audit row.
`tests/test_prod_cutover.py` rehearses exactly this against a fresh schema.

Then, by hand:

- [ ] Register the real tablet: Devices screen, then sign the tablet in
      with the device account it creates.
- [ ] Set every staff member's PIN (Users screen). The seeded `1111`…
      PINs go with the seeded accounts.
- [ ] Set `AKR-NT01`'s membership list to the real people only.
- [ ] Capture the 18 reference standards (Reference photos screen).
- [ ] Rotate `SUPABASE_SECRET_KEY` and the database password in the
      Supabase dashboard. Both have been through chat transcripts. Then
      `fly secrets set` the new values.
- [ ] Disable the legacy JWT secret under Project Settings, API Keys, if
      the dashboard still offers it.
- [ ] Revoke the leaked Groq key in Groq's console.
- [ ] Hand the staff notice (`docs/STAFF_NOTICE_PHOTOS.md`) to everyone
      who will be photographed, and keep the signed copies.
- [ ] `uv run python scripts/backup_db.py` again, and put the folder
      somewhere that is not this laptop.

---

## 6. Running in production

**Backups.** The Supabase free tier keeps none. Weekly, or before any
migration:

```bash
uv run python scripts/backup_db.py
```

Writes `local/backups/<stamp>/public.dump` and `auth_users.sql` and proves
`pg_restore` can read them. Storage (photos, uploads) is a separate copy:
`scripts/copy_storage.py`. Both are gitignored; move them off the machine.

**Migrations and deploys.** Push to `main`, approve the run. The pipeline
applies pending migrations, deploys, and smokes (section 2). Render's
"Manual Deploy" button still works for an emergency; `scripts/migrate.py
--plan` says what a deploy would apply.

**The keep-alive.** `keepalive.yml` pings every ten minutes while
`DEPLOY_ENABLED` is true. GitHub disables schedules in a repository with no
commits for 60 days; any commit re-enables it. If the Actions tab shows the
schedule paused, push something.

**Logs.** Render dashboard, the service's Logs tab. The guard, the scheduler's start line, every
`job_runs` failure and every 5xx land there. Nothing is sent to a telemetry
vendor.

**Rate limit tuning.** `RATE_LIMIT_PER_MINUTE` is per bearer token; a
dashboard load is about 12 requests, a floor run about 20 over ten minutes.
A 429 in the logs from a real user means the number is wrong, not the user.

**Supabase pauses free projects after a week without activity.** The
scheduler's five-minute reconciler and the 15-minute missed-run sweep keep
the database busy, so a running API is what prevents that. If the API is
ever down for a week, expect to un-pause the project by hand.
