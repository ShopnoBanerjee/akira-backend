"""Generate the outlet and SOP seed.

Encodes the mapping reviewed and approved on 26 Aug 2026 (see docs/DECISIONS.md
D4/D8): AKIRA's two real operational checklists, the mise-en-place par sheet
turned into numeric prep-readiness checks, and one Food Safety Daily template
covering the temperature logging the paper does not have.

    uv run python scripts/generate_sop_seed.py

Writes supabase/seed/001_outlets_and_sop.sql.

The photo and critical flags here are a STARTING POINT, not a fixed decision.
Admins edit them through the template builder, and migration 0011 keeps those
edits from reaching backwards into completed runs.

Users are deliberately NOT seeded here. profiles.id references auth.users, which
Supabase Auth owns; inserting those rows directly risks a half-formed account
that cannot sign in. See scripts/seed_users.py.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "supabase" / "seed" / "001_outlets_and_sop.sql"

PHOTO = "photo"
CRIT = "critical"

# (key, label, label_bn, sort_order, icon)
CATEGORIES: list[tuple[str, str, str, int, str]] = [
    ("opening", "Opening", "খোলা", 10, "sunrise"),
    ("closing", "Closing", "বন্ধ", 20, "moon"),
    ("cleaning", "Cleaning", "পরিষ্কার", 30, "spray-can"),
    ("food_safety", "Food Safety", "খাদ্য নিরাপত্তা", 40, "thermometer"),
    ("maintenance", "Maintenance", "রক্ষণাবেক্ষণ", 50, "wrench"),
    ("inventory", "Inventory", "মজুদ", 60, "package"),
]

# (code, name, city, lat, lng, opened_on)
OUTLETS: list[tuple[str, str, str, float | None, float | None, str | None]] = [
    ("AKR-NT01", "AKIRA New Town", "Kolkata", 22.5023, 88.3852, "2026-07-17"),
    # A second outlet from day one, so multi-outlet paths are exercised in dev
    # rather than discovered at outlet 2. Required by the spec's risk table.
    ("AKR-DEV02", "Dev Outlet 2", "Kolkata", 22.5726, 88.3639, None),
]

# An item is (title, title_bn, flags, value_spec)
#   flags      - set containing PHOTO and/or CRIT
#   value_spec - None, or (value_type, min, max, unit)
Item = tuple[str, str, set[str], tuple[str, float | None, float | None, str] | None]

# A template is (key, name, name_bn, category, frequency, day_part, role,
#                weekdays, interval_days, due_time, grace, description, items)
Template = tuple[
    str,
    str,
    str,
    str,
    str,
    str,
    str,
    list[int] | None,
    int | None,
    str,
    int,
    str,
    list[Item],
]

TEMPLATES: list[Template] = [
    # --- Kitchen Cleaning & Sanitation --------------------------------------
    (
        "kitchen_cleaning_daily",
        "Kitchen Cleaning — Daily",
        "দৈনিক রান্নাঘর পরিষ্কার",
        "cleaning",
        "daily",
        "closing",
        "shift_lead",
        None,
        None,
        "00:30",
        30,
        "Section A of the Kitchen Cleaning & Sanitation sheet.",
        [
            ("Daily working range cleaning", "দৈনিক কর্মক্ষেত্র পরিষ্কার করা", {PHOTO}, None),
            ("Daily working station cleaning", "প্রতিদিন কর্মস্থল পরিষ্কার করা", {PHOTO}, None),
            ("Daily trolley cleaning", "প্রতিদিন ট্রলি পরিষ্কার করা", set(), None),
            ("Daily floor cleaning", "প্রতিদিন মেঝে পরিষ্কার করা", {PHOTO}, None),
        ],
    ),
    (
        "kitchen_cleaning_alternate",
        "Kitchen Cleaning — Alternate Day",
        "একদিন অন্তর রান্নাঘর পরিষ্কার",
        "cleaning",
        "alternate_day",
        "closing",
        "shift_lead",
        None,
        2,
        "00:30",
        30,
        "Section B of the Kitchen Cleaning & Sanitation sheet.",
        [
            ("Equipment area dusting", "সরঞ্জাম এলাকা ধুলো পরিষ্কার করা", set(), None),
            ("Cobweb cleaning", "মাকড়সার জাল পরিষ্কার করা", {PHOTO}, None),
            ("Refrigerator outer body clean", "রেফ্রিজারেটরের বাইরের অংশ পরিষ্কার", set(), None),
            ("Back corridor brooming", "পিছনের করিডোর ঝাড়ু দেওয়া", {PHOTO}, None),
        ],
    ),
    # Section C pins a different task to each weekday, which one template cannot
    # express: active_weekdays sits on the assignment, not the item. Five
    # single-task templates is the schema-native way to say it.
    (
        "deep_clean_mon_nonveg_fridge",
        "Deep Clean — Non-veg Fridge (Mon)",
        "গভীর পরিষ্কার — আমিষ ফ্রিজ (সোম)",
        "cleaning",
        "weekly",
        "closing",
        "shift_lead",
        [1],
        None,
        "00:30",
        60,
        "Monday slot of the weekly deep-clean rota.",
        [
            (
                "Non-veg fridge clean & deep clean at night",
                "রাতে আমিষ ফ্রিজ পরিষ্কার ও ভালোভাবে পরিষ্কার করুন",
                {PHOTO, CRIT},
                None,
            ),
        ],
    ),
    (
        "deep_clean_tue_veg_chiller",
        "Deep Clean — Veg Chiller (Tue)",
        "গভীর পরিষ্কার — ভেজিটেবল চিলার (মঙ্গল)",
        "cleaning",
        "weekly",
        "closing",
        "shift_lead",
        [2],
        None,
        "00:30",
        60,
        "Tuesday slot of the weekly deep-clean rota.",
        [("Veg chiller clean", "ভেজিটেবল চিলার পরিষ্কার", {PHOTO, CRIT}, None)],
    ),
    (
        "deep_clean_wed_veg_freezer",
        "Deep Clean — Veg Freezer (Wed)",
        "গভীর পরিষ্কার — সবজি ফ্রিজার (বুধ)",
        "cleaning",
        "weekly",
        "closing",
        "shift_lead",
        [3],
        None,
        "00:30",
        60,
        "Wednesday slot of the weekly deep-clean rota.",
        [("Veg freezer clean", "সবজি ফ্রিজার পরিষ্কার", {PHOTO, CRIT}, None)],
    ),
    (
        "deep_clean_thu_staff_toilet",
        "Deep Clean — Staff Toilet (Thu)",
        "গভীর পরিষ্কার — কর্মচারীদের শৌচাগার (বৃহস্পতি)",
        "cleaning",
        "weekly",
        "closing",
        "shift_lead",
        [4],
        None,
        "00:30",
        60,
        "Thursday slot of the weekly deep-clean rota.",
        [("Staff toilet clean", "কর্মচারীদের শৌচাগার পরিষ্কার", {PHOTO}, None)],
    ),
    (
        "deep_clean_frisat_maintenance",
        "Maintenance Clean (Fri & Sat)",
        "রক্ষণাবেক্ষণমূলক পরিষ্কার (শুক্র ও শনি)",
        "cleaning",
        "weekly",
        "closing",
        "shift_lead",
        [5, 6],
        None,
        "00:30",
        60,
        "Friday and Saturday slots. Maintenance only, explicitly not a deep clean.",
        [
            (
                "Maintenance clean (no deep clean)",
                "রক্ষণাবেক্ষণমূলক পরিষ্কার (গভীরভাবে পরিষ্কার নয়)",
                set(),
                None,
            ),
        ],
    ),
    # --- Service & Housekeeping Operations ----------------------------------
    # The paper sheet runs to 16 daily items, over the 15-item ceiling, and
    # mixes dining-floor work with washroom work. Split by area and day-part.
    (
        "floor_dining_daily",
        "Floor & Dining — Daily",
        "দৈনিক ফ্লোর ও ডাইনিং",
        "opening",
        "daily",
        "opening",
        "staff",
        None,
        None,
        "17:00",
        30,
        "Dining-floor half of the Service & Housekeeping daily sheet.",
        [
            ("Chair / stool wiped", "চেয়ার / টুল মোছা হয়েছে", set(), None),
            ("Cutlery wiped and set up", "ছুরি-চামচ মুছে সাজিয়ে রাখা হয়েছে", {PHOTO}, None),
            (
                "Floor dusted, wet mopped, dry mopped",
                "মেঝে ঝাড়া + ভেজা মোছা + শুকনো মোছা",
                {PHOTO},
                None,
            ),
            ("Glass water bottles cleaned", "কাচের জলের বোতল পরিষ্কার করা হয়েছে", set(), None),
            ("Library rack dusted", "লাইব্রেরি র‍্যাকের ধুলো ঝাড়া", set(), None),
            (
                "Napkins folded",
                "ভাঁজ করা ন্যাপকিন (সর্বনিম্ন ২ প্যাকেট)",
                set(),
                ("number", 2, None, "packets"),
            ),
            ("Room freshener checked", "রুম ফ্রেশনার চেক", set(), None),
            (
                "Window panes and glass door cleaned, both sides",
                "জানালার কাচ ও কাচের দরজা (উভয় পাশ) পরিষ্কার করা হয়েছে",
                {PHOTO},
                None,
            ),
            ("Mirror clean", "আয়নার মতো পরিষ্কার", set(), None),
            ("Cobweb check", "মাকড়সার জাল পরীক্ষা", set(), None),
        ],
    ),
    (
        "washroom_waste_daily",
        "Washroom & Waste — Daily",
        "দৈনিক শৌচাগার ও বর্জ্য",
        "closing",
        "daily",
        "closing",
        "staff",
        None,
        None,
        "00:30",
        30,
        "Washroom half of the Service & Housekeeping daily sheet.",
        [
            ("Dustbin cleaning", "ময়লার ঝুড়ি পরিষ্কার", set(), None),
            ("Soap dispenser refill", "সাবান ডিসপেনসার রিফিল", set(), None),
            ("WC clean", "ডব্লিউসি (WC) পরিষ্কার", {PHOTO, CRIT}, None),
            ("Sink clean", "সিঙ্ক পরিষ্কার", {PHOTO}, None),
            ("Mirror clean", "আয়না পরিষ্কার", set(), None),
            ("Wall wiped with wet duster", "ভেজা ডাস্টার দিয়ে দেয়াল মোছা", set(), None),
        ],
    ),
    (
        "dining_deep_clean",
        "Dining Deep Clean",
        "ডাইনিং গভীর পরিষ্কার",
        "cleaning",
        "weekly",
        "closing",
        "staff",
        # The sheet says "3 days / week" without naming them. Assumed Mon/Wed/Fri.
        [1, 3, 5],
        None,
        "00:30",
        60,
        "Section B periodic tasks. Days assumed Mon/Wed/Fri — the sheet does not name them.",
        [
            (
                "Dining area scrubbed wall to wall",
                "খাবার ঘরের দেয়াল থেকে দেয়াল পর্যন্ত ঘষেমেজে পরিষ্কার করা",
                {PHOTO},
                None,
            ),
            ("Figure dusting", "মূর্তি ধুলো", set(), None),
        ],
    ),
    (
        "fortnightly_maintenance",
        "Fortnightly Maintenance",
        "পাক্ষিক রক্ষণাবেক্ষণ",
        "maintenance",
        "fortnightly",
        "mid",
        "outlet_manager",
        None,
        14,
        "15:00",
        120,
        "Section B every-15-days tasks.",
        [
            ("AC filter clean", "এসি ফিল্টার পরিষ্কার", {PHOTO}, None),
            (
                "Screw tightening & table maintenance",
                "স্ক্রু টাইট করা এবং টেবিলের রক্ষণাবেক্ষণ",
                set(),
                None,
            ),
        ],
    ),
    # --- Mise-en-place, as numeric prep readiness ---------------------------
    (
        "kitchen_prep_readiness",
        "Kitchen Prep Readiness",
        "রান্নাঘর প্রস্তুতি",
        "inventory",
        "daily",
        "opening",
        "shift_lead",
        None,
        None,
        "17:00",
        30,
        "Section A of the Mise-en-place sheet. Minimums are the printed par levels.",
        [
            ("Noodles", "নুডলস", set(), ("number", 30, None, "portions cut")),
            ("Yakitori — Pork", "ইয়াকিতোরি - শুকরের মাংস", set(), ("number", 2, None, "boxes")),
            ("Yakitori — Chicken", "ইয়াকিতোরি - চিকেন", set(), ("number", 3, None, "boxes")),
            ("Yakitori — Prawn", "ইয়াকিতোরি - চিংড়ি", set(), ("number", 3, None, "kg")),
            ("Mushroom", "মাশরুম", set(), ("number", 10, None, "packs")),
            ("Ajitama (ramen eggs)", "আজিতামা (রামেন ডিম)", {CRIT}, ("number", 30, None, "pcs")),
            ("Karaage", "কারাগে", set(), ("number", 2, None, "kg marinated")),
            ("Gyoza — Chicken", "গিয়োজা - চিকেন", set(), ("number", 50, None, "pcs")),
            ("Gyoza — Pork", "গিয়োজা - শূকরের মাংস", set(), ("number", 50, None, "pcs")),
            ("Gyoza — Mushroom", "গিয়োজা - মাশরুম", set(), ("number", 50, None, "pcs")),
            ("Pickled bok choy", "আচারযুক্ত বক চয়", set(), ("number", 400, None, "g")),
            ("Pickled mushroom", "আচারযুক্ত মাশরুম", set(), ("number", 400, None, "g")),
            ("Spring onion, cut", "পেঁয়াজ পাতা", set(), ("number", 500, None, "g")),
            ("Broths ready", "ঝোল", {CRIT}, ("number", 2, None, "types")),
        ],
    ),
    (
        "bar_stock_readiness",
        "Bar Stock Readiness",
        "বার স্টক প্রস্তুতি",
        "inventory",
        "daily",
        "opening",
        "staff",
        None,
        None,
        "17:00",
        30,
        "Section B of the Mise-en-place sheet.",
        [
            ("Club soda", "ক্লাব সোডা", set(), ("number", 12, None, "bottles")),
            ("Sugar syrup", "চিনির সিরাপ", set(), ("number", 2, None, "L")),
            ("Lemon", "লেবু", set(), ("number", 30, None, "pcs")),
            ("Ice", "বরফ", set(), ("number", 10, None, "kg")),
        ],
    ),
    # --- Added: not on any paper checklist ----------------------------------
    # Nothing in the seven source documents logs a temperature. For a kitchen
    # holding broth hot and stock cold all night that is the one gap worth
    # closing before launch. Five of six items are critical, against the spec's
    # "no more than half" guidance — deliberate on a food-safety template.
    (
        "food_safety_daily",
        "Food Safety Daily",
        "দৈনিক খাদ্য নিরাপত্তা",
        "food_safety",
        "daily",
        "mid",
        "shift_lead",
        None,
        None,
        "20:00",
        30,
        "Not from AKIRA's paper. Temperature bands are industry defaults - "
        "correct them to what the equipment actually holds.",
        [
            (
                "Broth hot-holding temperature",
                "ঝোলের গরম রাখার তাপমাত্রা",
                {CRIT},
                ("temperature_c", 75, 95, "°C"),
            ),
            (
                "Walk-in fridge temperature",
                "ওয়াক-ইন ফ্রিজের তাপমাত্রা",
                {CRIT},
                ("temperature_c", 0, 5, "°C"),
            ),
            (
                "Chiller cold-holding temperature",
                "চিলারের তাপমাত্রা",
                {CRIT},
                ("temperature_c", 0, 5, "°C"),
            ),
            (
                "Freezer temperature",
                "ফ্রিজারের তাপমাত্রা",
                {CRIT},
                ("temperature_c", -18, -15, "°C"),
            ),
            (
                "Staff grooming & handwash check",
                "কর্মীদের পরিচ্ছন্নতা ও হাত ধোয়া পরীক্ষা",
                {PHOTO, CRIT},
                None,
            ),
            ("Allergen station separation", "অ্যালার্জেন স্টেশন পৃথকীকরণ", {PHOTO}, None),
        ],
    ),
]

EXPECTED = {"templates": 14, "items": 57, "photo": 18, "critical": 11, "valued": 23}


def q(v: str | None) -> str:
    return "null" if v is None else "'" + v.replace("'", "''") + "'"


def num(v: float | None) -> str:
    return "null" if v is None else repr(v)


def main() -> int:
    items = [i for t in TEMPLATES for i in t[12]]
    actual = {
        "templates": len(TEMPLATES),
        "items": len(items),
        "photo": sum(1 for i in items if PHOTO in i[2]),
        "critical": sum(1 for i in items if CRIT in i[2]),
        "valued": sum(1 for i in items if i[3] is not None),
    }
    if actual != EXPECTED:
        print(f"ERROR: {actual} != approved {EXPECTED}", file=sys.stderr)
        return 1

    for t in TEMPLATES:
        if len(t[12]) > 15:
            print(f"ERROR: {t[0]} has {len(t[12])} items, over the 15 ceiling", file=sys.stderr)
            return 1

    L: list[str] = []
    a = L.append

    a("-- " + "-" * 73)
    a("-- 001 — Outlets, SOP categories, templates and assignments")
    a("--")
    a("-- GENERATED by scripts/generate_sop_seed.py — do not hand-edit.")
    a("--")
    a("-- The mapping approved on 26 Aug 2026: AKIRA's two real operational")
    a("-- checklists, the mise-en-place par sheet as numeric prep checks, and one")
    a("-- Food Safety Daily template covering temperature logging the paper lacks.")
    a("--")
    a(f"--   {actual['templates']} templates, {actual['items']} items")
    a(
        f"--   {actual['photo']} require a photo, {actual['critical']} critical, "
        f"{actual['valued']} take a value"
    )
    a("--")
    a("-- Photo and critical flags are a STARTING POINT. Admins edit them through")
    a("-- the template builder; migration 0011 keeps those edits from reaching")
    a("-- backwards into completed runs.")
    a("--")
    a("-- Users are NOT seeded here: profiles.id references auth.users, which")
    a("-- Supabase Auth owns. See scripts/seed_users.py.")
    a("--")
    a("-- Idempotent: re-running updates labels and adds nothing twice.")
    a("-- " + "-" * 73)
    a("")

    a("insert into outlets (code, name, city, geo_lat, geo_lng, opened_on) values")
    a(
        ",\n".join(
            f"    ({q(c)}, {q(n)}, {q(ct)}, {num(la)}, {num(lo)}, {q(op)}::date)"
            for c, n, ct, la, lo, op in OUTLETS
        )
    )
    a("on conflict (code) do update set")
    a("    name = excluded.name,")
    a("    city = excluded.city,")
    a("    geo_lat = excluded.geo_lat,")
    a("    geo_lng = excluded.geo_lng;")
    a("")

    a("insert into sop_categories (key, label, label_bn, sort_order, icon) values")
    a(
        ",\n".join(
            f"    ({q(k)}, {q(la)}, {q(bn)}, {o}, {q(ic)})" for k, la, bn, o, ic in CATEGORIES
        )
    )
    a("on conflict (key) do update set")
    a("    label = excluded.label,")
    a("    label_bn = excluded.label_bn,")
    a("    sort_order = excluded.sort_order,")
    a("    icon = excluded.icon;")
    a("")

    for tpl in TEMPLATES:
        (
            _key,
            name,
            name_bn,
            cat,
            freq,
            part,
            role,
            weekdays,
            interval,
            due,
            grace,
            desc,
            tpl_items,
        ) = tpl

        a("-- " + "=" * 70)
        a(f"-- {name}  ({len(tpl_items)} items)")
        a("-- " + "=" * 70)
        a("do $seed$")
        a("declare")
        a("    v_template_id uuid;")
        a("    v_item_id     uuid;")
        a("    v_outlet      record;")
        a("begin")
        a("    select id into v_template_id from checklist_templates")
        a(f"     where name = {q(name)} and deleted_at is null;")
        a("")
        a("    if v_template_id is null then")
        a("        insert into checklist_templates")
        a("            (category_id, name, name_bn, description, frequency, day_part, version)")
        a(f"        select c.id, {q(name)}, {q(name_bn)}, {q(desc)},")
        a(f"               {q(freq)}::frequency, {q(part)}::day_part, 1")
        a(f"          from sop_categories c where c.key = {q(cat)}")
        a("        returning id into v_template_id;")
        a("")

        for idx, (title, title_bn, flags, vs) in enumerate(tpl_items, start=1):
            vt, vmin, vmax, vunit = vs if vs else (None, None, None, None)
            a("        insert into checklist_template_items")
            a("            (template_id, sort_order, title, title_bn, requires_photo,")
            a("             requires_value, value_type, value_min, value_max, value_unit,")
            a("             is_critical, allow_na)")
            a(f"        values (v_template_id, {idx}, {q(title)}, {q(title_bn)},")
            a(f"                {str(PHOTO in flags).lower()}, {str(vs is not None).lower()},")
            a(f"                {q(vt)}::value_type, {num(vmin)}, {num(vmax)}, {q(vunit)},")
            a(f"                {str(CRIT in flags).lower()}, false)")
            a("        returning id into v_item_id;")
            a("")
            a("        -- Version 1 of this definition, so the run snapshot resolves.")
            a("        insert into checklist_template_item_versions")
            a("            (template_item_id, template_id, template_version, sort_order,")
            a("             title, title_bn, requires_photo, requires_value, value_type,")
            a("             value_min, value_max, value_unit, is_critical, allow_na, change_note)")
            a(f"        values (v_item_id, v_template_id, 1, {idx}, {q(title)}, {q(title_bn)},")
            a(f"                {str(PHOTO in flags).lower()}, {str(vs is not None).lower()},")
            a(f"                {q(vt)}::value_type, {num(vmin)}, {num(vmax)}, {q(vunit)},")
            a(f"                {str(CRIT in flags).lower()}, false, 'Seeded.');")
            a("")

        weekday_sql = f"array{weekdays}" if weekdays else "'{0,1,2,3,4,5,6}'"
        interval_sql = str(interval) if interval else "null"
        anchor_sql = "current_date" if interval else "null"

        a("        -- Assign to every outlet that exists.")
        a("        for v_outlet in select id from outlets where deleted_at is null loop")
        a("            insert into checklist_assignments")
        a("                (template_id, outlet_id, assigned_role, active_weekdays,")
        a("                 interval_days, anchor_date, due_time_local, grace_minutes)")
        a("            values (v_template_id, v_outlet.id,")
        a(f"                    {q(role)}::user_role,")
        a(f"                    {weekday_sql},")
        a(f"                    {interval_sql},")
        a(f"                    {anchor_sql},")
        a(f"                    {q(due)}::time, {grace});")
        a("        end loop;")
        a("    end if;")
        a("end")
        a("$seed$;")
        a("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(L) + "\n", encoding="utf-8", newline="\n")

    print(f"wrote {OUT.relative_to(ROOT)}")
    print(f"  {actual['templates']} templates, {actual['items']} items")
    print(f"  {actual['photo']} photo, {actual['critical']} critical, {actual['valued']} valued")
    print(f"  largest template: {max(len(t[12]) for t in TEMPLATES)} items (ceiling 15)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
