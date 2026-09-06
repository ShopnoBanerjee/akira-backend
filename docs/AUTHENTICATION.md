# Authentication — every way somebody gets into AKIRA Ops

The complete picture: who can sign in, with what, what the system checks, and
what each kind of session may then do. Verified against the deployed system on
7 September 2026, not written from memory.

Companion documents: `SECURITY.md` (the threat table), `DECISIONS.md` D3 (the
shared tablet), D31 (training), D33 (organisations and the second factor),
`RUNBOOK_DEPLOY.md` §3a (the post-deploy account steps).

---

## 1. The shape of it, in one paragraph

Supabase Auth is the only thing that issues a session. It never decides what
anybody may do. The API verifies the token Supabase minted, looks the caller up
in `profiles` (or `outlet_devices`), works out which organisation and which
outlets they may reach, and enforces every rule in code. Row level security
sits behind that as a second line, so a leaked browser key still reads nothing
it should not. There is no sign-up: every login is created by an administrator.

---

## 2. The four kinds of caller

| Caller | Credential | Shell | Roles |
|---|---|---|---|
| **Management login** | email + password (+ authenticator where required) | `/app` | `owner`, `ops_manager`, `outlet_manager` |
| **Shared tablet** | email + password held by the device | `/floor` | a device account, pseudo-role `staff` |
| **Floor person on a tablet** | a 4–8 digit PIN, on top of the tablet's session | `/floor` | `shift_lead`, `staff` |
| **Platform admin** | email + password + authenticator, always | `/platform` | `platform_admin` |

Floor staff also have individual Supabase logins in the development
organisation, but that is a testing convenience. In the restaurant, floor work
happens on the shared tablet and the PIN is what names the person.

---

## 3. How a management login signs in

1. The browser sends email and password to Supabase Auth directly. The API
   never sees a password and has no login endpoint of its own.
2. Supabase returns an **access token** (a JWT, ES256, valid **60 minutes**)
   and a refresh token. The Supabase client refreshes silently in the
   background, so a person stays signed in across a working day.
3. Every API call carries `Authorization: Bearer <access token>`.
4. The API verifies it against Supabase's **JWKS** endpoint:
   - signature, using the public key named by the token's `kid`;
   - the algorithm is pinned to what the *key* declares (`ES256` or `RS256`),
     never to what the token header claims — that is the alg-confusion hole;
   - audience `authenticated`, issuer `<project>/auth/v1`, expiry, with a small
     clock leeway;
   - anonymous Supabase sessions are rejected outright.

The email address is only an identifier. Nothing is authorised by it.

---

## 4. What happens after the token is verified

One database read turns the token's subject into an identity, then that
identity is cached for **60 seconds per caller** so a busy screen does not pay
for it on every request. The read resolves, in order:

1. **A device?** If the subject matches `outlet_devices`, this is a tablet.
   It gets a pseudo-role of `staff`, no profile of its own, and its outlet and
   organisation come from the device row.
2. **A profile?** Otherwise the subject must match `profiles.id`.
3. **The organisation.** From the profile, or from the device's outlet.
4. **Reach.** Every active outlet of that organisation, plus the caller's own
   `outlet_members` rows.

### The refusals, and what each means

| Situation | Response | What the person sees |
|---|---|---|
| Token malformed, wrong audience, wrong issuer, expired | 401 | "Your session has expired. Sign in again." |
| Subject matches no profile and no device | 403 `pending-activation` | "This login is not set up in AKIRA Ops. Ask your administrator." |
| Profile soft-deleted | 401 | "This account has been removed." |
| Profile `is_active = false` | 403 `pending-activation` | "Your account is not active." |
| Profile has no organisation | 403 | "This account belongs to no organisation." |
| Organisation inactive or deleted | 403 | "This organisation is not active." |
| Second factor owed, not presented | 403 `mfa-required` | routed to the authenticator screen |

**There is no self-signup.** An authenticated stranger is nobody: refused, and
nothing is written. (Supabase's own "allow new users to sign up" should also be
switched off in the dashboard as defence in depth — see §11.)

Deactivating somebody takes effect within the 60-second cache, or immediately
when an administrator changes them, because that path clears the cache.

---

## 5. The second factor

Required for the accounts that can do the most damage:

- **the platform admin — always**;
- **an owner whose organisation is onboarded** (`organisations.onboarded_at`
  is set). AKIRA is onboarded, so `management@simplyakira.com` needs one.
  The development organisation is not, so every `@akira.test` login is never
  asked. That is deliberate: testing must stay frictionless.

Nobody else is asked. Managers, floor staff, tablets and PINs are untouched.

**How it is proved.** Supabase writes an `aal` claim into the access token:
`aal1` after a password alone, `aal2` after a TOTP challenge. The API reads it.
Until a session is `aal2`, everything is refused with the `mfa-required`
problem type except `GET /users/me` and the health probes — just enough for the
web app to learn why and show the right screen.

**Enrolling.** The web app talks to Supabase directly: it lists existing
factors, clears any enrolment that was started and abandoned, enrols a new TOTP
factor, shows the QR code (and the key, for a device that cannot scan), then
verifies the first six-digit code. After that the session is `aal2` and the app
opens.

**Recovery.** If somebody loses their authenticator, an administrator removes
their factors in the Supabase dashboard (Authentication → Users → the user).
A platform-admin route for this, audited, is P26b.

---

## 6. The shared tablet, and the PIN

The tablet holds **one** Supabase session, bound to one outlet. That session
authenticates the *device*, and nothing more: it can read the floor screens and
the PIN pad, and every management guard rejects it.

A tablet's Supabase account is created **out of band** — the API never mints or
transports a credential. An owner then binds that existing account to an outlet
with `POST /devices` (owner only).

### Identifying a person

`POST /floor/identify` takes a profile id and a PIN. It is refused outright on
anything but a device session. The person must be `staff` or `shift_lead`,
active, and a member of *that tablet's outlet*.

- PINs are **4 to 8 digits**, hashed with **Argon2id** (time cost 3, 64 MiB,
  parallelism 2). The plaintext is never stored.
- **Five wrong attempts locks the PIN for five minutes.**
- Every failure gives the same message — "That PIN is not right." — whether the
  person does not exist, works elsewhere, has no PIN, or is locked. A shared
  tablet in a dining room must not become a directory of who works here.
- Every attempt, successful or not, is written to the audit log with the
  device, the outlet and the reason.

### The actor assertion

A correct PIN mints an **actor token**: HMAC-SHA256, signed with a key derived
from the service secret (never the raw secret), carrying profile, device and
outlet. Sent as `X-Actor-Token`.

- It is **not** a Supabase JWT and grants nothing outside the floor endpoints.
- It is **bound to one device and one outlet**. Replayed from another tablet,
  verification fails.
- It lasts **12 hours** — long enough for a double shift, short enough that a
  tablet left signed in overnight is useless by morning prep.
- Handover on the tablet drops it client-side, so the next person must identify
  themselves.

**A PIN can never approve a run.** Approval requires an individual manager
login, always. That is what keeps separation of duties real: a checklist's
approver can never be its submitter, enforced by a database constraint as well
as in code.

---

## 7. What a session may do once it is in

Authentication says *who*. These decide *what*:

**Role rank** — `staff` 10, `shift_lead` 20, `outlet_manager` 30,
`ops_manager` 40, `owner` 50, `platform_admin` 60. Nobody may grant a role at
or above their own, and **no API call grants `platform_admin` at all**; it is
created only by `scripts/create_platform_admin.py`.

**Outlet reach** —
- `owner` and `ops_manager` reach every outlet **of their own organisation**,
  without a membership row;
- everyone else reaches the outlets they are a member of, and only those
  inside their organisation — a membership row pointing anywhere else is not
  access;
- the platform admin alone has no outlet filter.

**The organisation fence** — every list, every query, every catalogue is
scoped to the caller's organisation. Two tenants may both have an outlet coded
`SP01` and an item called "Pork belly"; neither can see the other's.

**The platform admin is read-only inside a tenant.** Writes outside
`/platform`, `/users/me`, `/training/me` and `/auth/` are refused in one place
in the dependency, so a new route cannot forget the rule. Every read it makes
inside an organisation is written to that organisation's audit log as
`action = 'read'`, naming the path. The tenant can see that the platform
looked.

**Row level security** is enabled and forced on every table as a second line.
The helpers resolve the caller's organisation from the JWT subject, so a leaked
browser key reads only what that person could see anyway.

---

## 8. Uploads

The browser never gets a storage credential. It asks the API, the API mints a
**signed URL** scoped to one object path it chose itself, and the browser
uploads to that. Object paths are fixed server-side, so a client cannot write
outside its own outlet's prefix.

---

## 9. Everything else the edge enforces

- **Rate limit**: a token bucket per caller, 600 requests per minute by
  default, returning 429 with a `Retry-After`.
- **Security headers** and a strict CSP on the web origin; `frame-ancestors
  'none'`, `X-Robots-Tag: noindex`, HSTS.
- **CORS** is an allowlist, not `*`.
- **Errors** are RFC 7807 problem+json. They never leak SQL or stack traces,
  and authorisation failures are honest 403s rather than 404s in disguise.

---

## 10. Who holds which login today

| Login | Role | Organisation | Second factor | Where the password lives |
|---|---|---|---|---|
| `platform@simplyakira.com` | platform_admin | none | **required** | `local/platform-admin.md` (gitignored) |
| `management@simplyakira.com` | owner | `akira` | **required** | `.seed-credentials.md` (gitignored) |
| 9 × `@akira.test` people | owner → staff | `akira-dev` | not asked | `.seed-credentials.md` |
| 2 × `device.*@akira.test` | tablets | `akira-dev` | not asked | `.seed-credentials.md` |

The `@akira.test` addresses cannot receive mail, so they can never do a real
password reset, and re-running `scripts/seed_users.py` rotates all of them.
Both real passwords have been through a chat transcript and should be changed.

---

## 11. The one dashboard switch still open

Supabase → Authentication → Providers → Email → **"Allow new users to sign
up"** should be **off**. The API already refuses any subject an administrator
did not create, so this is defence in depth: it stops Supabase minting the
account in the first place. TOTP is already enabled.

---

## 12. Deliberate limits, recorded so nobody re-opens them

- **No password reset for `@akira.test`.** Not a bug: the domain is
  undeliverable on purpose.
- **No manager-level MFA yet.** Optional enrolment for managers is P26b.
- **No self-service factor recovery.** An administrator clears factors in the
  dashboard until P26b adds the audited route.
- **No SSO.** Email and password, plus TOTP where it matters.
