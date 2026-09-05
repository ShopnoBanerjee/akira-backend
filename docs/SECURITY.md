# Security review

The P10 review of what protects what, where each control is enforced, and how
we know it works. "Proven by" names the test or the live verification — a
control nobody has fired at is listed as such, not counted.

Reviewed: 27 Aug 2026 (P10), re-reviewed 3 Sep 2026 (P18), production posture
added 6 Sep 2026 (P23, rows 23–26). The P18 review covered P11–P17, which added
11 tables, ~20 operations, two storage buckets and — new in kind — outbound
calls carrying photographs to third-party model vendors.

Re-review when an epic touches auth, storage, adds a table, or sends data to a
new external service. That last clause is here because P7 and P11 introduced a
data-egress path the original review had no row for.

The architecture this defends: the browser holds only the publishable key and
a user JWT; every write goes through the API, which connects with the service
role and authorises **in code**; RLS is the second line for the day a
publishable key leaks or a future feature reads Supabase directly.

---

## The table

| # | Asset / surface | Threat | Control | Enforced in | Proven by |
|---|---|---|---|---|---|
| 1 | API session tokens | Forged or tampered JWT | Full JWKS verification: signature, audience, issuer, expiry; algorithm pinned to what the key declares (`ES256`/`RS256` only), never to what the header claims — the alg-confusion hole | `app/core/security.py` `TokenVerifier` | `test_permissions.py`; every live request since P1 |
| 2 | Every business table | Reading another outlet's data through the API | Role + outlet membership checks in code before any query; `can_access_outlet` on every outlet-scoped handler | `app/core/deps.py`, per-domain services | `test_permissions.py`, `test_sales_ingest.py` (stranger refused) |
| 3 | Every business table | Reading another outlet's data **around** the API with a leaked publishable key | RLS enabled AND forced on every public table; `authenticated` holds SELECT only, scoped by membership; `anon` holds nothing; policyless tables impossible | `supabase/migrations/0007_rls.sql` + successors | `test_rls.py` — fired at as the attacker: audit of the whole catalog plus behaviour tests per identity |
| 4 | Every business table | A future migration forgetting RLS on a new table | The catalog audit fails the build if any public table lacks forced RLS or a policy | `test_rls.py::TestEveryTableIsLockedDown` | itself — it is the tripwire |
| 5 | Revoked people | A deactivated account whose JWT has not yet expired | All RLS helpers filter `is_active` and `deleted_at`; in-code checks load the profile per request, so deactivation cuts access on the next call | `0007_rls.sql` helpers, `app/core/deps.py` | `test_rls.py` (deactivated sees nothing but self), `test_permissions.py` |
| 6 | Shared tablet (D3) | Anyone at the counter acting as anyone else | Device account can only list staff and check PINs; a person exists only after Argon2id PIN verify mints a device-bound, HMAC-signed actor token (12 h TTL, dropped on handover) | `app/core/actor.py`, `app/domains/sop/runs_router.py` | live floor flows P5–P10; `test_runs.py` |
| 7 | Staff PINs | Brute-forcing a 4-digit PIN on the tablet | Argon2id (t=3, m=64 MiB); 5 wrong attempts locks for 5 minutes; every failure audited; one uniform error for unknown person / no PIN / locked / wrong, so the tablet reveals nothing | `runs_router.py` identify flow | code-reviewed this pass; lockout is exercised by the audit rows it writes |
| 8 | Manager approvals | Floor actor reaching management endpoints | An actor token authorises floor actions only; `/app` requires an individual JWT with a management role — the actor token is not a session | `app/core/deps.py`, `require_management` | `test_permissions.py` |
| 9 | Run integrity | Approving your own submission | Approver ≠ submitter in the router, the service, and a Postgres CHECK — the database refuses even if both code layers regress | `0004_sop.sql` CHECK, `runs_service.py` | `test_runs.py`; seed rehearsal wrote 832 runs with zero violations |
| 10 | Photos | Reading photos without authorisation; a client choosing where its bytes land | Private bucket, no public read path; upload grants are signed per-object with the path fixed server-side; view URLs minted per request, 5-minute expiry, never stored | `app/integrations/storage.py`, `confirm_photo` path check | `test_runs.py` path-claim refusal; live P5–P10 |
| 11 | Customer PII | Phone numbers reaching the database | Only a salted SHA-256 digest is ever bound to a column; the raw number dies in the parse loop; the raw export lives in a private no-browser-read bucket | `sales/service.py` `phone_hasher` | `test_sales_ingest.py` — asserts no 10-digit value in any row, against a real database |
| 12 | Customer PII | A real export becoming a committed test fixture | Real files stay outside the repo; the parser suite generates synthetic workbooks; pre-commit secret scan greps staged diffs for Indian mobile patterns | working practice + `tests/test_petpooja.py` | the scan caught exactly this once (P9) and the fixture was replaced |
| 13 | SQL layer | Injection through user input | Every statement is `text()` with bound parameters; the few f-string fragments interpolate only fixed identifier maps, never input | throughout | ruff + review; no string-concatenated user input exists |
| 14 | Input surface | Malformed or oversized payloads | Pydantic models with `extra="forbid"`; explicit size/type checks on uploads (`.xlsx` only, non-empty, 5 MB Storage cap on photos enforced bucket-side too) | routers, `ensure_bucket` | `test_sales_ingest.py` upload validation |
| 15 | Audit trail | Tampering with history | `audit_log` is insert-only in practice: no update/delete endpoint exists, `authenticated` holds SELECT only, writes join the caller's transaction so they cannot survive a rollback of the thing they describe | `app/core/audit.py`, RLS grants | `test_rls.py` write refusal; `db.py` docstring records the transaction rule |
| 16 | Secrets | Keys reaching git or the browser | `.env` and `.seed-credentials.md` gitignored; secret scan before every commit (the Supabase, Anthropic and Groq key prefixes, plus Indian mobile-number patterns — spelled out in HANDOFF, not here, so this file never trips the scan itself); the service key exists only server-side; browsers get the publishable key and short-lived signed URLs | working practice | the scan runs in every commit in this repo's history since P7 |
| 17 | Browser | Cross-origin calls | CORS allowlist from `CORS_ORIGINS`, not `*` | `app/main.py` | config review |
| 18 | Photographs of kitchens and stock sheets | Business imagery leaving the building to a third-party model vendor | This is a real egress path, not a hypothetical: the photo review posts kitchen photographs and the sheet extractor posts scans of handwritten count sheets. Payloads carry the **image and task text only** — no bill, customer, phone or staff-identity field is ever attached, verified by reading both payload builders. Vendor is switchable per surface (`AI_REVIEW_PROVIDER`, `STOCK_EXTRACT_PROVIDER`) and review is off per outlet by default (`ai_review.enabled`) | `app/integrations/vision.py`, `app/integrations/sheet_extraction.py` | payload builders read this pass — images plus prompt, nothing joined from the database. **Residual, accepted:** a kitchen photo may incidentally show a staff member, and a count sheet carries their handwriting and signature. Nobody has been told their photos go to a US vendor |
| 19 | `stock-sheets` and `sales-uploads` buckets | A client choosing where its bytes land, or reading another outlet's uploads | Both private, no public read path; the object path is derived server-side from `(outlet_id, sha256, extension)` and the user's filename never becomes the path; outlet access checked before the upload is accepted, not after | `app/domains/inventory/counts_service.py` `sheet_path_for`, `_require_outlet_access`; `app/integrations/storage.py` | read this pass; same shape as the photo path check proven in `test_runs.py` |
| 20 | The 11 tables added by P11–P17 | A new table shipping without RLS | The catalog audit (#4) is not a per-table list — it walks `pg_class` and fails on any public table lacking forced RLS or a policy, so tables added after this review are covered without editing it | `test_rls.py::TestEveryTableIsLockedDown` | ran 3 Sep 2026 against all 36 tables: 19 passed |
| 21 | Every route | An endpoint shipping with no authorisation at all | Mechanical audit of all 98 routes: each carries an identity dependency; role guards distribute as 38 `require_management`, 21 `require_admin`, 7 `require_owner`, 7 floor-actor | routers throughout | AST audit run 3 Sep 2026 — **0 routes without a guard**. One flagged endpoint (`GET /outlets/{outlet_id}`) was a false positive: its check lives one layer down in `service.get_one` |
| 22 | Grant posture after a restore | `pg_dump --no-privileges` into a new Supabase project drops every per-table GRANT/REVOKE; the platform default ACL then grants `anon` and `authenticated` ALL on every restored table (observed on Mumbai, 5 Sep 2026: anon ALL on 18 tables, authenticated ALL on 36) | `0021_grant_posture.sql`: a catalog-driven sweep of tables, sequences and functions, plus DEFAULT privileges for the migrating role so a table created tomorrow starts with the same posture; RLS stayed forced throughout, so nothing was exposed | `supabase/migrations/0021_grant_posture.sql` | `test_rls.py` catalog assertions run by hand against Mumbai after restore (anon 0 grants, authenticated SELECT on 36, 3 helper functions only); a throwaway table created after 0021 came up anon-nothing / authenticated-SELECT |
| 23 | Every route | A runaway client, a credential-stuffing loop, or one tablet in a retry storm saturating the connection pool | Token bucket per caller (bearer token hash, else client address): `RATE_LIMIT_PER_MINUTE` (600) per minute with burst to the full minute; 429 problem+json with `Retry-After`; probes exempt; in-process, correct because the deployment is one process by construction | `app/core/hardening.py` `RateLimitMiddleware`, mounted inside CORS in `app/main.py` | `test_hardening.py` — burst then refusal, per-token budgets, probe exemption, headers on the 429 |
| 24 | Every response | A browser reinterpreting JSON; a shared tablet's back-forward cache showing the last person's screen; the API framed | `X-Content-Type-Options: nosniff`, `Cache-Control: no-store` unless a handler set its own, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Permissions-Policy`, HSTS in production only | `app/core/hardening.py` `SecurityHeadersMiddleware` | `test_hardening.py` — every response including the 429; HSTS only when production |
| 25 | Production configuration | Shipping with a dev default: the placeholder salt, a localhost CORS origin, a JWKS URL from another project, `SQL_ECHO` on | With `ENV=production` the API **refuses to start** and lists every problem; `/docs`, `/redoc`, `/openapi.json` are off | `Settings.production_problems()`, `check_production_config` in `lifespan` | `test_hardening.py` — each rule individually, the complete configuration passing, the guard silent outside production |
| 26 | The web origin | Script injection, clickjacking, mixed content, search indexing of an internal tool | CSP (`script-src 'self'`, `connect-src` limited to Supabase and the API, `frame-ancestors 'none'`), `X-Robots-Tag: noindex`, `robots.txt`, HSTS, immutable caching only under `/assets/` | `akira-frontend/public/_headers`, `vercel.json` (Cloudflare Pages / Netlify / Vercel) | config review; the CSP names `*.fly.dev` until the API origin is fixed — RUNBOOK_DEPLOY §3 says to tighten it |
| 27 | `training_records` (P24) | A person marking themselves trained without the tour; a staff member skipping; a manager restarting training they were never given authority over | The client is not trusted: track follows the role server-side, skip is refused for all but owner, restart is owner or owner-delegated (`can_restart_training`, owner-only to set), someone else's attempt is 404. RLS: read self, global admins, or colleagues at one's outlets | `app/domains/training/service.py` `can_skip`, `can_reset`, `_own_open`; `0024_training.sql` policy | `tests/test_training.py` — every rule; the catalog audit (#4) covers the table |

---

## Known gaps, deliberately recorded

- **Both GitHub repos are public.** No sales, customer or credential data is in
  either. `.gitignore` refuses `*.xlsx`/`*.xls`/`*.csv`, the usual data
  directories, and `local/`, so an export or a source document cannot be swept
  in by `git add -A`. The one real scan that had been committed —
  `requisition_27aug2026.pdf`, an AKIRA stock sheet with staff handwriting and
  signatures — was removed from the working tree and purged from the pushed
  history on 3 Sep 2026. **It was publicly fetchable between 27 Aug and 3 Sep,
  so treat its contents as disclosed**; a history rewrite removes the copy, not
  the week. The scoring transcription (`golden_page1.json`) stays: it carries
  item names and counts, no handwriting and no signature, and it is what keeps
  the D17 provider choice measurable.

- **Nobody has been told their photographs go to a third-party vendor.** Row 18
  covers what leaves; this covers who knows. If staff photographs are ever used
  for anything but the advisory review, that becomes a consent question rather
  than a config one.

- **Rate limiting is in-process** (#23). Correct for one machine, which the
  scheduler already requires; a second replica would double the effective
  limit. If the API ever scales out, both move to a shared store together.
- **The old Groq key must be revoked in Groq's console.** It reached the
  project through a chat transcript; Groq is out of the code and `.env` (D28),
  but the key is live until revoked. The control that failed was human.
- **`PHONE_HASH_SALT` has a dev default** and production refuses it (#25).
  Rotating it orphans existing hashes — documented in the field's comment and
  the accepted trade.
- **Seeded PINs are sequential** (`1111`, `2222`, …) and their file says so
  loudly. They exist to be typed during testing and must be replaced through
  the admin UI before real use.
- **The database password and the secret key have both been through chat
  transcripts** (5 Sep 2026, during the region move). Neither is public, but
  neither is known only to the dashboard either. Rotate both at go-live;
  RUNBOOK_DEPLOY §5 has it on the checklist. `scripts/prod_cutover.py` says so
  again when it finishes.
- **Seeded accounts and the synthetic outlet are still live** by the owner's
  instruction until go-live. `scripts/prod_cutover.py` is the one-shot removal,
  rehearsed by `tests/test_prod_cutover.py`; it will not run without a real
  owner account in place.
