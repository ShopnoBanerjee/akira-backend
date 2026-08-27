# Open items

Things that are knowingly incomplete, and why. Every entry says what unblocks
it and who can do that — because the difference between "nobody has got to
this" and "this is waiting on a credential only Shopno has" matters when you
are deciding what to pick up.

This is not a bug list. Nothing here is broken; broken things get fixed, not
filed. It is the set of deliberate gaps, and it should shrink.

Last reviewed: 27 Aug 2026, after P9a.

**Keep it honest.** When you close one, delete the entry rather than marking it
done — the file is only useful if everything in it is still true. When you open
one, say what would unblock it, not just what is missing.

---

## Waiting on something only the owner can supply

### The Anthropic vision path has never run

`ANTHROPIC_API_KEY` is not set on the build machine, so `AI_REVIEW_PROVIDER` is
`groq` (D13). The pipeline is proven end to end against a real model — a grimy
sink judged against that outlet's own standard came back `fail` at confidence
1.0, wrote its `run_item_ai_reviews` row, raised `ai_mismatch` and rendered on
the review screen — but the line that has never executed is
`client.messages.parse` in `_review_anthropic`.

Everything around the call is shared between providers and tested. What is
untested is the Anthropic transport specifically.

**Unblocked by:** setting `ANTHROPIC_API_KEY` in `akira-backend/.env` and
flipping `AI_REVIEW_PROVIDER=anthropic`. Re-reviewing a photo then writes a
*second* row rather than overwriting the Groq one — the table is keyed on
`(run_item_id, model, prompt_version)` — so the two verdicts can be compared
side by side, which is worth doing once.

**Also:** the Groq key currently in `.env` arrived through a chat transcript
and should be rotated in the Groq console.

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
| AKR-NT01 (New Town) | 2 | 18 |
| AKR-DEV02 (Dev Outlet 2) | 0 | 18 |

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

**Unblocked by:** finding out whether this Petpooja plan can export an
order-wise or bill-wise item report. If it can, that is a second adapter beside
`petpooja.orders.v1` and a straightforward one. If it cannot, the decision to
make later is whether item-level analysis is worth relaxing `order_id` and
storing period aggregates under a table whose name would stop describing its
contents.

---

## Housekeeping, queued for P10

### Six photos will never verify

The photos uploaded during P5 and P6 are 262-byte stubs, not decodable images.
They carry plausible `photo_bytes` values, which is why nobody noticed until
P7 downloaded one. `process_photo` raises `UndecodableImage` on them, the
failure lands in `job_runs`, and the item stays unprocessed — which the review
screen shows as "not checked yet" rather than clean. That is the honest
rendering, and it is permanent for these six.

**Unblocked by:** deciding whether to clear `photo_path` on those rows (the
runs then read as photoless, which is also true) or to leave them as a visible
scar. Do it as part of the P10 seed refresh, not before.

### Two business dates were materialised by hand

2026-08-25 and 2026-08-28 were created during P7 testing rather than by the
05:00 job. 08-25 is almost entirely `missed`, because nothing was ever going to
be done on a day invented after the fact. Any dashboard period spanning it
reads worse than the outlet deserves.

**Unblocked by:** the P10 realistic 8-week seed dataset, which should replace
this data wholesale rather than patch it.

---

## Watch, not yet broken

### The CI actions are on a deprecated Node

Both workflows warn:

> Node.js 20 is deprecated. The following actions target Node.js 20 but are
> being forced to run on Node.js 24: `actions/checkout@v4`,
> `actions/setup-node@v4`, `pnpm/action-setup@v4`.

CI is green; GitHub is already forcing the newer runtime. It will stop being a
warning at some point.

**Unblocked by:** bumping the action majors — but do it on its own, as a
deliberate change, so that if a bump breaks something the failure is
unambiguous. It was left alone on 27 Aug specifically because backend CI had
just been made green after eight epics red, and stacking an unrelated CI change
on top of that would have muddied the signal.

---

## Not open, recorded so nobody re-opens it

- **The three leftover test outlets** (AKR-TEST9, AKR-T469, AKR-SL03) do *not*
  clutter the outlet pickers. They are soft-deleted and inactive, and every
  query filters them. This was asserted as a problem in an earlier draft of
  HANDOFF.md without being checked; it is not one.
- **There is no blended outlet health score**, and that is D14, not an
  omission. Stage 1 measures one of four pillars; a number built from a quarter
  of the evidence would be worse than none.
