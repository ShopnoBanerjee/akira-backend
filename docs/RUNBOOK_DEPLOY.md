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

## 2. Deploy the API to Fly.io, region `bom`

Fly's `bom` region is AWS ap-south-1, the same as the database. There is no
free tier; a `shared-cpu-1x` with 512 MB is about $3 a month.

```bash
cd akira-backend
fly auth login
fly launch --no-deploy --copy-config --name akira-ops-api --region bom
```

`--copy-config` keeps the committed `fly.toml`. Answer **no** to a Postgres
or Redis; the database already exists.

### 2a. Secrets

Never in `fly.toml`, never in the repo. Generate the salt fresh:

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

```bash
fly secrets set \
  DATABASE_URL='postgresql+asyncpg://postgres:<DB_PASSWORD>@db.zvskxgmmlahhybzpcicl.supabase.co:5432/postgres' \
  SUPABASE_URL='https://zvskxgmmlahhybzpcicl.supabase.co' \
  SUPABASE_PUBLISHABLE_KEY='sb_publishable_ySp9Uovntyxh9nJ-QuNm-Q_I40WS4y_' \
  SUPABASE_SECRET_KEY='<sb_secret_...>' \
  SUPABASE_JWKS_URL='https://zvskxgmmlahhybzpcicl.supabase.co/auth/v1/.well-known/jwks.json' \
  PHONE_HASH_SALT='<the generated value>' \
  CORS_ORIGINS='https://<your web origin>' \
  AI_REVIEW_PROVIDER='openai' \
  STOCK_EXTRACT_PROVIDER='gemini' \
  GEMINI_API_KEY='<key>' \
  SMTP_HOST='<host>' SMTP_PORT='587' SMTP_USERNAME='<user>' SMTP_PASSWORD='<pass>' \
  SMTP_FROM='AKIRA Ops <ops@<your domain>>'
```

`db.<ref>.supabase.co` is IPv6-only. Fly machines have IPv6 egress, so the
direct host works; if `/readyz` reports `unreachable` after deploy, switch
`DATABASE_URL` to the session pooler shown in the Supabase dashboard
(Connect, Session pooler: host `aws-1-ap-south-1.pooler.supabase.com`, port
5432, user `postgres.zvskxgmmlahhybzpcicl`). Same credentials, IPv4.

### 2b. Deploy

```bash
fly deploy --ha=false
fly scale count 1
fly status
curl -s https://akira-ops-api.fly.dev/healthz
curl -s https://akira-ops-api.fly.dev/readyz
```

`/healthz` says `"env": "production"`. `/readyz` says `"database": "ok"`.
If the machine restarts in a loop, `fly logs` shows the guard's list:
`Refusing to start with ENV=production:` followed by what is missing.

### 2c. Measure

From Kolkata, before this deploy, the dashboard answered in ~250 ms and
single-statement endpoints in 62 to 81 ms, all of it wire. Beside the
database the same screens should be under 100 ms; check with the browser's
network tab on `/dashboard/outlet-health`.

### 2d. Supabase network restrictions (optional, recommended)

Project Settings, Database, Network Restrictions: allow only Fly's egress
addresses (`fly ips list`, plus any machine's IPv6 from `fly ssh console -C
"curl -6 ifconfig.co"`) and your own address for `scripts/backup_db.py`.
Do this AFTER the deploy is verified, because a wrong entry locks out the
API too.

---

## 3. Deploy the web app

A static Vite build. Any CDN host works; region does not matter because the
bundle is cached at the edge and every data call goes to the API. The
repository carries configuration for three:

| Host | Files used | Note |
|---|---|---|
| Cloudflare Pages | `public/_headers`, `public/_redirects` | Free for commercial use, unlimited bandwidth. **Recommended.** |
| Netlify | `public/_headers`, `public/_redirects` | Free tier allows commercial use with a bandwidth cap |
| Vercel | `vercel.json` | The Hobby plan forbids commercial use; a paid plan is needed |

Settings, whichever host:

| Setting | Value |
|---|---|
| Build command | `pnpm build` |
| Output directory | `dist` |
| Node | 22 (`NODE_VERSION=22` on Cloudflare/Netlify) |
| `VITE_SUPABASE_URL` | `https://zvskxgmmlahhybzpcicl.supabase.co` |
| `VITE_SUPABASE_PUBLISHABLE_KEY` | `sb_publishable_ySp9Uovntyxh9nJ-QuNm-Q_I40WS4y_` |
| `VITE_API_BASE_URL` | `https://akira-ops-api.fly.dev` (or the custom domain) |

Then put the web origin into the API's `CORS_ORIGINS` (`fly secrets set
CORS_ORIGINS=...` restarts the machine) and into Supabase's Site URL.

**The Content-Security-Policy** in `_headers`/`vercel.json` allows
`connect-src` to `*.supabase.co` and `*.fly.dev`. Once the API has a fixed
origin, replace `https://*.fly.dev` with it. A CSP that names the wrong host
breaks every request silently, so test in the browser after any edit.

---

## 4. Custom domains (optional)

`fly certs add api.<domain>` and a CNAME; the CDN host has its own flow for
the web origin. Update `CORS_ORIGINS`, `VITE_API_BASE_URL`, the CSP, and
Supabase's Site URL together.

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

**Migrations.** Apply with `psql` against the Mumbai host in filename order,
new files only; then `fly deploy --ha=false`. Never re-run `0007`.

**Deploys.** `fly deploy --ha=false` builds from the Dockerfile, health-checks
`/healthz`, and swaps. The pool warms at boot (about 4 s) before the machine
takes traffic.

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
