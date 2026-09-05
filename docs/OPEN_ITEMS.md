# Open items

Things that are knowingly incomplete, and why. Every entry says what unblocks
it and who can do that — because the difference between "nobody has got to
this" and "this is waiting on a credential only Shopno has" matters when you
are deciding what to pick up.

This is not a bug list. Nothing here is broken; broken things get fixed, not
filed. It is the set of deliberate gaps, and it should shrink.

Last reviewed: 6 Sep 2026, after the production-readiness pass (P23).

**Keep it honest.** When you close one, delete the entry rather than marking it
done — the file is only useful if everything in it is still true. When you open
one, say what would unblock it, not just what is missing.

---

## Waiting on something only the owner can supply

### The Anthropic vision path has never run

`ANTHROPIC_API_KEY` is not set on the build machine, so `AI_REVIEW_PROVIDER` is
`openai` — Gemini's free tier through its OpenAI-compatible layer (D28). The
pipeline is proven end to end against a real model — a grimy
sink judged against that outlet's own standard came back `fail` at confidence
1.0, wrote its `run_item_ai_reviews` row, raised `ai_mismatch` and rendered on
the review screen — but the line that has never executed is
`client.messages.parse` in `_review_anthropic`.

Everything around the call is shared between providers and tested. What is
untested is the Anthropic transport specifically.

**Unblocked by:** setting `ANTHROPIC_API_KEY` in `akira-backend/.env` and
flipping `AI_REVIEW_PROVIDER=anthropic`. Re-reviewing a photo then writes a
*second* row rather than overwriting the existing one — the table is keyed on
`(run_item_id, model, prompt_version)` — so the two verdicts can be compared
side by side, which is worth doing once.

**Also:** Groq is gone from the code and from `.env` (D28); the key that had
leaked through a chat transcript should still be **revoked in the Groq
console** — nothing here uses it, but it is live until someone does that.

### The daily digest does not send mail

No SMTP host is configured, so `get_notifier` falls back to `LogNotifier` and
records `smtp_not_configured` on every `job_runs` row. This is deliberate
(D12.6) and requested: degrading visibly beats a digest that quietly stopped
sending while reporting success every morning.

**Unblocked by:** `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`,
`SMTP_FROM` in `.env`. No code change. Note the seeded accounts are all
`@akira.test`, which cannot receive mail — testing real delivery needs one real
address on the recipient list.

---

## Waiting on someone to walk the outlet

### Reference standards are barely captured

| Outlet | Captured | Photo-requiring items |
|---|---:|---:|
| AKR-NT01 (AKIRA Safuipara) | 0 | 18 |
| AKR-DEV02 (Dev Outlet 2, synthetic) | 0 | 18 |

The two Safuipara standards that had been captured were lost with the
`sop-photos` bucket when the Sydney project was paused before Storage could
be copied (5 Sep 2026); their rows are inactive, not deleted. So were 471
photos on the seeded demo runs — those screens now say "photo unavailable",
which is the truth. If the Sydney project can ever be resumed, the objects
are still in its bucket and `scripts/copy_storage.py` recovers them.

The AI reviewer compares a submitted photo against that outlet's own standard.
Without one it judges on the item's written instruction alone and is told to
lean towards `uncertain` — useful, but a long way short of what the feature is
for. Per-outlet is deliberate (D6): the New Town clean prep station is not
another outlet's clean prep station.

**Unblocked by:** a manager walking the outlet with a tablet, under normal
service lighting, at `/app/sop/reference-photos`. The screen lists what is
still missing and shows the measured brightness of each standard so one shot in
a dark storeroom is visible as such. This is a physical job; no amount of code
shortens it.

---

## Waiting on a report Petpooja may not produce

### Item-level sales are not ingested

`sales_order_items` is empty and P9 does not fill it. No Petpooja item export
carries a bill number — Item Sale Report (hourly), Item Report Day Wise, Item
Wise Sales and Highest Selling Items were all checked, and every one is
pre-aggregated. There is nothing to hang `order_id` on (D15).

Nothing in Stage 1 reads the table. The Sales pillar of the health card is
Stage 2, and it needs bill-level totals, which `sales_orders` has.

**Partially unblocked, 27 Aug 2026.** The Order Listing report (checked
against a real export) DOES carry a bill number — `Order No.` — with an
`Items` column beside it. But the column is a comma-joined list of item
*names*: no per-line quantity, no per-line price. So `sales_order_items`
still cannot be filled honestly from it — a name appearing once could be
quantity three.

**Resolved as far as Petpooja allows, 29 Aug 2026 (P14).** The
`petpooja.listing.v1` adapter ingests names-per-bill into
`sales_order_items`, joined to the master bill on `Order No.` =
`external_bill_no` (verified: all 89 orders of the real export matched,
amounts to the paisa). Quantity and price are NULL by migration 0017 — the
export does not carry them, and a default of 0 would lie. What remains open,
and stays here:

- **Units sold are still unknown.** "On N bills" is the honest unit until a
  true line-item export exists; none of the six report types checked carries
  one.
- **Bill names are Petpooja's short names.** The live reconciliation against
  the Item Wise report matched 23 of 25 names exactly; the other two are
  aliases ("Donburi Chicken" on bills vs "Chicken Karaage Donburi" in the
  catalogue). Any future name-level join needs the same alias treatment the
  inventory Mapper gives stock sheets.

---

## Waiting for go-live day

### The API is not deployed; the frontend is not hosted

Everything needed is in the repos — `Dockerfile`, `fly.toml` (region `bom`),
the production guard, `public/_headers` / `vercel.json` for the web — and
`docs/RUNBOOK_DEPLOY.md` is the order to do it in. What is missing is an
account on Fly.io and on a CDN host, and about an hour.

**Unblocked by:** the owner following RUNBOOK_DEPLOY sections 1–3. Section 1
comes first: a real owner account has to exist before anything else, because
`owner@akira.test` is deleted at go-live.

### The synthetic data is still live

`AKR-DEV02`, the seeded runs at Safuipara, the eleven `@akira.test` accounts
and their `1111`-style PINs. By the owner's instruction they stay until
production. `scripts/prod_cutover.py` removes exactly that set and nothing
else, is a dry run unless told otherwise, refuses without a real owner
account, and is rehearsed by `tests/test_prod_cutover.py` against a fresh
schema.

**Unblocked by:** go-live day. RUNBOOK_DEPLOY §5 is the checklist.

---

## Not open, recorded so nobody re-opens it

- **The three leftover test outlets** (AKR-TEST9, AKR-T469, AKR-SL03) do *not*
  clutter the outlet pickers. They are soft-deleted and inactive, and every
  query filters them. This was asserted as a problem in an earlier draft of
  HANDOFF.md without being checked; it is not one.
- **The blended health score exists**, and D22 is why. D14 held it back
  while Stage 1 measured one pillar of four; P15 built the other three, so the
  blend now runs over whatever is actually measured and renormalises. The
  inventory pillar stays dark until an outlet has its first confirmed stock
  count — measured-or-absent, never a zero standing in for no evidence. This
  file claimed the opposite until 4 Sep 2026; `app/domains/dashboard/health.py`
  is the answer, not this paragraph.
- **A restaurant-name guard on sales uploads is not missing.** Every adapter
  reads the export's `Restaurant Name:` preamble, and it is checked against
  `sales.petpooja_restaurant_name` before anything is stored or written (D25).
  Two things about it are deliberate and should not be "fixed":
  - It **ships unarmed** — an empty setting accepts any name — so it could
    land without taking sales ingestion down. **Armed globally to `Akira` on
    5 Sep 2026**, which is what all three real Petpooja exports carry. If an
    export is ever refused because Petpooja's account name was edited, copy
    the name off the upload card into Settings → Sales rather than clearing
    the setting.
  - It catches the wrong **restaurant**, never the wrong **outlet**. Both
    outlets sit under one Petpooja account and print the same name; nothing
    in the file tells them apart, which is why the uploader picks the outlet
    by hand.

---

## Waiting on people

### The restaurant guard is built but not armed

`sales.petpooja_restaurant_name` is empty, so uploads are accepted from any
restaurant (D25). Nothing is wrong until somebody uploads the wrong file —
and the point of the guard is that nothing *looks* wrong then either.

**The value to use is `Akira`** — read off all three real exports already in
Storage (two Orders Master Reports and one Order Listing), and backfilled onto
`data_uploads.restaurant_name` so the upload cards show it.

**Unblocked by:** setting Settings → Sales → "Expected Petpooja restaurant
name" to exactly `Akira`. Left unarmed on purpose rather than switched on
here, for one reason: no Akira *Item Report: Day Wise* export exists yet, so
whether Petpooja prints the same account name on that report type is
unverified. If it prints something else, arming now would refuse the very
export this project is waiting for. Arm it once one has landed — or arm it
today and expect to widen it if that upload bounces.

### An Akira Item Report: Day Wise export (P17)

The adapter, recipes and theoretical-consumption arithmetic are live and
tested, but no Akira export of this report has been uploaded yet — the
shape was established from an older export of a different restaurant. Until
one arrives, `sales_item_days` is empty and every dependent surface
(variance component, zero-sales anomaly, true units) reads "pending" with
its reason. Export it from Petpooja (Reports → Item Report: Day Wise) for
the same period the master covers and drop it on the Sales page.
