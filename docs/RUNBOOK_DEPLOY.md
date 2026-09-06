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
| akira-backend | applies pending migrations (`scripts/migrate.py --apply`, tracked in `schema_migrations`), `flyctl deploy --remote-only --ha=false` to region `bom`, then curls `/healthz` and `/readyz` and fails if either is wrong |
| akira-frontend | builds with the production `VITE_*` values, uploads `dist` to Cloudflare Pages with wrangler, then curls the site for the app and the CSP header |

Nothing runs until the repository variable `DEPLOY_ENABLED` is `true`, so
merging this pipeline changes nothing by itself. Secrets live in the GitHub
environment, never in the repo. The setup below is yours; about half an
hour, once.

### 2a. Accounts and tokens

1. **Fly.io.** `fly auth signup`, then from `akira-backend`:
   ```bash
   fly apps create akira-ops-api --org personal
   fly tokens create deploy -x 8760h        # a deploy-only token, one year
   ```
   Keep the token for 2c.
2. **Cloudflare.** Create an account, then in the dashboard: Workers &
   Pages, Create, Pages, "Upload assets" (direct upload), project name
   `akira-ops`. Then My Profile, API Tokens, Create Token, template
   "Edit Cloudflare Workers" (it includes Pages), and note your Account ID
   from the Workers & Pages overview.
3. **Supabase.** Project Settings, Database, Connect: copy the **Session
   pooler** string (host `aws-0-ap-south-1.pooler.supabase.com`, port
   **5432**, user `postgres.zvskxgmmlahhybzpcicl`). GitHub runners are
   IPv4-only and the direct host is IPv6-only; port 6543 is transaction
   mode and will not run migrations.

### 2b. The API's runtime secrets, once

These are read by the running machine, not by the pipeline. Generate a
fresh salt: `python -c "import secrets; print(secrets.token_urlsafe(32))"`.

```bash
fly secrets set -a akira-ops-api \
  DATABASE_URL='postgresql+asyncpg://postgres:<DB_PASSWORD>@db.zvskxgmmlahhybzpcicl.supabase.co:5432/postgres' \
  SUPABASE_URL='https://zvskxgmmlahhybzpcicl.supabase.co' \
  SUPABASE_PUBLISHABLE_KEY='sb_publishable_ySp9Uovntyxh9nJ-QuNm-Q_I40WS4y_' \
  SUPABASE_SECRET_KEY='<sb_secret_...>' \
  SUPABASE_JWKS_URL='https://zvskxgmmlahhybzpcicl.supabase.co/auth/v1/.well-known/jwks.json' \
  PHONE_HASH_SALT='<the generated value>' \
  CORS_ORIGINS='https://akira-ops.pages.dev' \
  AI_REVIEW_PROVIDER='openai' STOCK_EXTRACT_PROVIDER='gemini' GEMINI_API_KEY='<key>' \
  SMTP_HOST='<host>' SMTP_PORT='587' SMTP_USERNAME='<user>' SMTP_PASSWORD='<pass>' \
  SMTP_FROM='AKIRA Ops <ops@<your domain>>'
```

If `/readyz` reports `unreachable` after the first deploy, Fly's IPv6 egress
could not reach the direct host: set `DATABASE_URL` to the session pooler
string from 2a-3 (with `postgresql+asyncpg://`) instead.

### 2c. GitHub, both repositories

Settings, Environments, New environment `production`, tick **Required
reviewers** and add yourself. Then, in that environment:

| Repo | Secrets | Variables |
|---|---|---|
| akira-backend | `FLY_API_TOKEN` (2a-1), `MIGRATIONS_DATABASE_URL` (2a-3, plain `postgresql://`) | `API_URL` = `https://akira-ops-api.fly.dev` |
| akira-frontend | `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` (2a-2) | `CF_PAGES_PROJECT` = `akira-ops`, `WEB_URL` = `https://akira-ops.pages.dev`, `VITE_SUPABASE_URL`, `VITE_SUPABASE_PUBLISHABLE_KEY`, `VITE_API_BASE_URL` = the API URL |

Finally, Settings, Secrets and variables, Actions, Variables, **repository**
variable `DEPLOY_ENABLED` = `true` in each repo. From then on every push to
`main` that passes CI offers you a deploy to approve. The environment URL on
the run links to what it deployed.

### 2d. The first deploy

Approve the backend run first (schema, then API), then the frontend run.
`/healthz` says `"env": "production"`, `/readyz` says `"database": "ok"`;
the site answers with the CSP header. The runs' smoke steps check exactly
that and fail loudly otherwise. If the machine restarts in a loop, `fly logs`
shows the guard's list: `Refusing to start with ENV=production:` followed by
what is missing.

### 2e. Measure

From Kolkata, before this deploy, the dashboard answered in ~250 ms and
single-statement endpoints in 40 to 80 ms, all of it wire. Beside the
database the same screens should be under 100 ms; check with the browser's
network tab on `/dashboard/outlet-health`.

### 2f. Supabase network restrictions (optional, recommended)

Project Settings, Database, Network Restrictions: allow only Fly's egress
addresses (`fly ips list`, plus a machine's IPv6 from `fly ssh console -C
"curl -6 ifconfig.co"`), GitHub's runner ranges if you want the pipeline's
migrations to keep working (they change; the pooler accepts them by default),
and your own address for `scripts/backup_db.py`. Do this AFTER the first
deploy is verified, because a wrong entry locks out the API too.

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
applies pending migrations, deploys, and smokes (section 2). `fly deploy
--ha=false` by hand still works for an emergency; `scripts/migrate.py --plan`
says what a deploy would apply.

**Logs.** `fly logs`. The guard, the scheduler's start line, every
`job_runs` failure and every 5xx land there. Nothing is sent to a telemetry
vendor.

**Rate limit tuning.** `RATE_LIMIT_PER_MINUTE` is per bearer token; a
dashboard load is about 12 requests, a floor run about 20 over ten minutes.
A 429 in the logs from a real user means the number is wrong, not the user.

**Supabase pauses free projects after a week without activity.** The
scheduler's five-minute reconciler and the 15-minute missed-run sweep keep
the database busy, so a running API is what prevents that. If the API is
ever down for a week, expect to un-pause the project by hand.
