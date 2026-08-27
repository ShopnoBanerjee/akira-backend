# Security review

The P10 review of what protects what, where each control is enforced, and how
we know it works. "Proven by" names the test or the live verification — a
control nobody has fired at is listed as such, not counted.

Reviewed: 27 Aug 2026. Re-review when an epic touches auth, storage, or adds a
table.

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

---

## Known gaps, deliberately recorded

- **No general API rate limiting.** The PIN flow has its own lockout (#7); the
  rest relies on Supabase Auth's own limits and the small user population of an
  internal tool. Add a gateway limit before any public exposure.
- **The Groq key must be rotated.** It reached the project through a chat
  transcript (OPEN_ITEMS). The control that failed was human, not code.
- **`PHONE_HASH_SALT` has a dev default.** Production must set it; the default
  is deliberately labelled `dev-only-not-a-secret` so a missed override is
  visible in any config dump. Rotating it orphans existing hashes — that is
  documented in the field's comment and is the accepted trade.
- **Seeded PINs are sequential** (`1111`, `2222`, …) and their file says so
  loudly. They exist to be typed during testing and must be replaced through
  the admin UI before real use.
- **No CSP/security headers on the API responses.** The API serves JSON to a
  known SPA; headers belong on the frontend host. Recorded so nobody assumes
  they exist here.
