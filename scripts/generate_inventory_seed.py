"""Generate the inventory catalogue seed from AKIRA's paper count sheets.

Source documents (Aug 2026), transcribed by department:

    AKIRA - Daily Kitchen & Housekeeping .......... housekeeping     13 items
    AKIRA - Service & Daily Housekeeping .......... fnb_service      19 items
    AKIRA - Daily Chef and Range .................. fnb_hot_range    97 items
    AKIRA - Daily Dessert ......................... fnb_desserts      9 items
    AKIRA - Bar Counter ........................... beverages        13 items
                                                                    ---
                                                                    151 items

The data lives here rather than in hand-written SQL so the transcription stays
auditable and the seed can be regenerated:

    uv run python scripts/generate_inventory_seed.py

Writes supabase/seed/002_inventory_catalogue.sql.

Par levels come from a different sheet - "Kitchen Prep (Mise-en-place) &
Beverage Thresholds" - which is the only document carrying minimums.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "supabase" / "seed" / "002_inventory_catalogue.sql"

# --- Reference data ---------------------------------------------------------

DEPARTMENTS: list[tuple[str, str, str, int]] = [
    ("fnb_hot_range", "F&B Production - Hot Range", "এফ অ্যান্ড বি প্রোডাকশন - হট রেঞ্জ", 10),
    ("fnb_desserts", "F&B Production - Desserts", "এফ অ্যান্ড বি প্রোডাকশন - ডেজার্ট", 20),
    ("beverages", "Beverages / Bar Counter", "পানীয় / বার কাউন্টার", 30),
    ("fnb_service", "F&B Service", "এফ অ্যান্ড বি সার্ভিস", 40),
    ("housekeeping", "Housekeeping", "হাউসকিপিং", 50),
]

CATEGORIES: list[tuple[str, str, str, int]] = [
    ("vegetables", "Vegetables", "সবজি", 10),
    ("fruits", "Fruits", "ফল", 15),
    ("herbs", "Herbs", "ভেষজ", 20),
    ("spices", "Spices", "মশলা", 30),
    ("sauces", "Sauces & Dressings", "সস ও ড্রেসিং", 40),
    ("oils", "Oils", "তেল", 50),
    ("meat", "Meat", "মাংস", 60),
    ("poultry", "Poultry", "হাঁস-মুরগি", 65),
    ("dairy", "Dairy", "দুগ্ধজাত", 70),
    ("rice", "Rice", "চাল", 80),
    ("pulses", "Pulses", "ডাল", 85),
    ("flour", "Flour", "আটা", 90),
    ("prep", "Prepared Components", "প্রস্তুত উপকরণ", 100),
    ("desserts", "Dessert Ingredients", "ডেজার্ট উপকরণ", 110),
    ("beverage", "Beverage", "পানীয়", 120),
    ("alcohol", "Alcohol", "অ্যালকোহল", 130),
    ("sweetener", "Sweeteners", "মিষ্টিকারক", 140),
    ("snacks", "Snacks", "স্ন্যাকস", 150),
    ("dry_goods", "Dry Goods", "শুকনো সামগ্রী", 160),
    ("packaging", "Packaging", "প্যাকেজিং", 170),
    ("cleaning", "Cleaning", "পরিষ্কারক", 180),
    ("uniform", "Uniform", "ইউনিফর্ম", 190),
]

# (department, category, name, name_bn, unit, note)
ITEMS: list[tuple[str, str, str, str, str, str | None]] = [
    # --- Housekeeping (13) -------------------------------------------------
    ("housekeeping", "cleaning", "Garbage Bag", "ময়লার ব্যাগ", "piece", None),
    ("housekeeping", "packaging", "16x20 Plastic Bag", "১৬x২০ প্লাস্টিকের ব্যাগ", "piece", None),
    ("housekeeping", "packaging", "13x16 Plastic Bag", "১৩x১৬ প্লাস্টিকের ব্যাগ", "piece", None),
    ("housekeeping", "packaging", "Cling Wrap", "ক্লিং র‍্যাপ", "roll", None),
    ("housekeeping", "packaging", "8x10 Ziplock Bag", "৮x১০ জিপলক ব্যাগ", "piece", None),
    ("housekeeping", "cleaning", "Detergent Powder", "ডিটারজেন্ট পাউডার", "gram", None),
    ("housekeeping", "cleaning", "Vim Bar", "ভিম বার", "piece", None),
    ("housekeeping", "cleaning", "Duster", "ডাস্টার", "piece", None),
    ("housekeeping", "cleaning", "Scotch Brite", "স্কচ ব্রাইট", "piece", None),
    ("housekeeping", "cleaning", "Steel Wool", "স্টিল উল", "piece", None),
    (
        "housekeeping",
        "cleaning",
        "Pillow",
        "বালিশ",
        "piece",
        "Listed under Cleaning on the source sheet, which looks wrong - confirm "
        "whether this is a scouring pad rather than a pillow.",
    ),
    ("housekeeping", "cleaning", "Sink Wiper", "সিঙ্ক মোছার যন্ত্র", "piece", None),
    ("housekeeping", "cleaning", "Sink Brush", "সিঙ্ক ব্রাশ", "piece", None),
    # --- F&B Service (19) --------------------------------------------------
    ("fnb_service", "uniform", "Net Cap", "নেট ক্যাপ", "piece", None),
    ("fnb_service", "cleaning", "Colin", "কলিন", "millilitre", None),
    ("fnb_service", "cleaning", "Floor Cleaner", "মেঝে পরিষ্কারক", "millilitre", None),
    ("fnb_service", "cleaning", "Harpic", "হারপিক", "millilitre", None),
    ("fnb_service", "cleaning", "Toilet Freshner", "টয়লেট ফ্রেশনার", "piece", None),
    ("fnb_service", "cleaning", "Room Freshner", "রুম ফ্রেশনার", "piece", None),
    ("fnb_service", "cleaning", "Table Wipe", "টেবিল মোছা", "piece", None),
    ("fnb_service", "cleaning", "Floor Brush", "মেঝে ব্রাশ", "piece", None),
    ("fnb_service", "cleaning", "Wiper", "ওয়াইপার", "piece", None),
    ("fnb_service", "cleaning", "Dry Mop", "শুকনো মোছা", "piece", None),
    ("fnb_service", "cleaning", "Mop", "মপ", "piece", None),
    ("fnb_service", "snacks", "Chanachur", "চানাচুর", "gram", None),
    ("fnb_service", "packaging", "Gyoza & Yakitori Box", "গিয়োজা এবং ইয়াকিতোরি বক্স", "piece", None),
    ("fnb_service", "packaging", "Container 600ml", "কন্টেইনার ৬০০ মিলি", "piece", None),
    ("fnb_service", "packaging", "Container 1000ml", "কন্টেইনার ১০০০ মিলি", "piece", None),
    ("fnb_service", "packaging", "Guest Tissue / Napkin", "অতিথির টিস্যু / ন্যাপকিন", "packet", None),
    ("fnb_service", "dry_goods", "Wooden Skewers", "কাঠের শলাকা", "packet", None),
    ("fnb_service", "dry_goods", "Chopstick", "চপস্টিক", "packet", None),
    ("fnb_service", "dry_goods", "Water Jug", "জলের জগ", "jug", None),
    # --- Desserts (9) ------------------------------------------------------
    ("fnb_desserts", "desserts", "Mango Sticky Rice Mix", "আমের আঠালো ভাতের মিশ্রণ", "gram", None),
    ("fnb_desserts", "dairy", "Cream Cheese", "ক্রিম চিজ", "gram", None),
    ("fnb_desserts", "fruits", "Mango", "আম", "gram", None),
    ("fnb_desserts", "dairy", "Coconut Milk Powder", "নারকেলের দুধের গুঁড়ো", "gram", None),
    ("fnb_desserts", "dairy", "Dlecta Whipped Cream", "ডলেকটা হুইপড ক্রিম", "millilitre", None),
    ("fnb_desserts", "dairy", "MilkMaid", "মিল্কমেইড", "millilitre", None),
    ("fnb_desserts", "desserts", "Compound Chocolate", "যৌগিক চকোলেট", "gram", None),
    ("fnb_desserts", "desserts", "Gelatine Sheet", "জেলাটিন শিট", "piece", None),
    ("fnb_desserts", "desserts", "Baking Powder", "বেকিং পাউডার", "gram", None),
    # --- Beverages / Bar (13) ---------------------------------------------
    ("beverages", "alcohol", "Sake", "সাকে", "millilitre", None),
    (
        "beverages",
        "beverage",
        "Club Soda",
        "ক্লাব সোডা",
        "bottle",
        "Bar sheet listed this in ML, but it is counted and reordered by the "
        "bottle (750 ml each). Unit corrected to bottle so the par of 12 means "
        "12 bottles rather than 12 ml.",
    ),
    ("beverages", "beverage", "Coffee Syrup", "কফি সিরাপ", "millilitre", None),
    ("beverages", "beverage", "Grenadine", "গ্রেনাডিন", "millilitre", None),
    ("beverages", "beverage", "Litchi Slush", "লিচু স্লাশ", "millilitre", None),
    ("beverages", "beverage", "Sugar Syrup", "চিনির সিরাপ", "millilitre", None),
    ("beverages", "beverage", "Ice Cube", "বরফ কিউব", "kilogram", None),
    ("beverages", "beverage", "Instant Coffee Powder", "ইনস্ট্যান্ট কফি পাউডার", "gram", None),
    ("beverages", "beverage", "Lemon Juice", "লেবুর রস", "millilitre", None),
    ("beverages", "dairy", "Milk", "দুধ", "millilitre", None),
    ("beverages", "herbs", "Blue Pea Flower", "নীল মটর ফুল", "gram", None),
    ("beverages", "sweetener", "Honey", "মধু", "millilitre", None),
    ("beverages", "vegetables", "Lemon", "লেবু", "piece", None),
    # --- Hot Range (97) ----------------------------------------------------
    ("fnb_hot_range", "vegetables", "Cucumber Brunoy Wedge", "শসা ব্রুনয় ওয়েজ", "gram", None),
    ("fnb_hot_range", "vegetables", "Sweet Corn", "মিষ্টি ভুট্টা", "gram", None),
    ("fnb_hot_range", "vegetables", "Celery", "সেলারি", "gram", None),
    ("fnb_hot_range", "vegetables", "Carrot", "গাজর", "gram", None),
    ("fnb_hot_range", "vegetables", "Ginger", "আদা", "gram", None),
    ("fnb_hot_range", "vegetables", "Potato", "আলু", "gram", None),
    ("fnb_hot_range", "vegetables", "Cabbage", "বাঁধাকপি", "gram", None),
    ("fnb_hot_range", "vegetables", "Bokchoy", "বকচয়", "gram", None),
    ("fnb_hot_range", "vegetables", "Shitake Mushroom", "শিটাকে মাশরুম", "gram", None),
    ("fnb_hot_range", "vegetables", "Button Mushroom", "বাটন মাশরুম", "gram", None),
    ("fnb_hot_range", "vegetables", "Tomato", "টমেটো", "gram", None),
    ("fnb_hot_range", "vegetables", "Peeled Garlic", "খোসা ছাড়ানো রসুন", "gram", None),
    ("fnb_hot_range", "vegetables", "Spring Onion", "পেঁয়াজ পাতা", "gram", None),
    ("fnb_hot_range", "vegetables", "Onion", "পেঁয়াজ", "gram", None),
    ("fnb_hot_range", "vegetables", "Pickle Ginger", "আচার আদা", "gram", None),
    (
        "fnb_hot_range",
        "vegetables",
        "Begun (Aubergine)",
        "বেগুন",
        "gram",
        "Source sheet's Bengali column read 'শুরু করা হয়েছে' (= 'has been "
        "started'), a machine-translation error. Corrected to বেগুন.",
    ),
    ("fnb_hot_range", "vegetables", "Karela", "করলা", "gram", None),
    ("fnb_hot_range", "vegetables", "Cucumber", "শসা", "gram", None),
    ("fnb_hot_range", "vegetables", "Ladies Finger", "ঢেঁড়স", "gram", None),
    ("fnb_hot_range", "vegetables", "Lau", "লাউ", "gram", None),
    ("fnb_hot_range", "vegetables", "Jhingey", "ঝিঙে", "gram", None),
    ("fnb_hot_range", "vegetables", "Potol", "পটল", "gram", None),
    ("fnb_hot_range", "vegetables", "Cauliflower", "ফুলকপি", "gram", None),
    ("fnb_hot_range", "sweetener", "Sugar", "চিনি", "gram", None),
    ("fnb_hot_range", "spices", "Chili Powder", "মরিচের গুঁড়ো", "gram", None),
    ("fnb_hot_range", "spices", "Black Pepper", "গোলমরিচ", "gram", None),
    ("fnb_hot_range", "spices", "Chilli Flakes", "চিলি ফ্লেক্স", "gram", None),
    ("fnb_hot_range", "spices", "Kashmiri Dry Chilli", "কাশ্মীরি শুকনো মরিচ", "gram", None),
    ("fnb_hot_range", "spices", "Dry Chilli", "শুকনো মরিচ", "gram", None),
    ("fnb_hot_range", "spices", "Green Chilli", "কাঁচা মরিচ", "gram", None),
    ("fnb_hot_range", "spices", "Whole Garam Masala", "পুরো গরম মসলা", "gram", None),
    ("fnb_hot_range", "spices", "Whole Panch Poron", "পুরো পাঁচ ফোড়ন", "gram", None),
    ("fnb_hot_range", "spices", "Five Spice Powder", "পাঁচ মশলার গুঁড়ো", "gram", None),
    ("fnb_hot_range", "spices", "Togarashi Powder", "টগরশি পাউডার", "gram", None),
    ("fnb_hot_range", "spices", "Clove", "লবঙ্গ", "gram", None),
    ("fnb_hot_range", "spices", "Elaichi", "এলাচ", "gram", None),
    ("fnb_hot_range", "spices", "Turmeric Powder", "হলুদ গুঁড়ো", "gram", None),
    ("fnb_hot_range", "spices", "Whole Corriander", "পুরো ধনিয়া", "gram", None),
    ("fnb_hot_range", "spices", "White Pepper", "সাদা গোলমরিচ", "gram", None),
    ("fnb_hot_range", "spices", "Black Sesame Seed", "কালো তিল", "gram", None),
    ("fnb_hot_range", "spices", "White Sesame Seed", "সাদা তিলের বীজ", "gram", None),
    ("fnb_hot_range", "spices", "Broth Powder", "ঝোল পাউডার", "gram", None),
    ("fnb_hot_range", "spices", "Ajino Moto", "আজিনো মোটো", "gram", None),
    ("fnb_hot_range", "spices", "Aromat Powder", "অ্যারোমাট পাউডার", "gram", None),
    ("fnb_hot_range", "spices", "Hondashi Powder", "হোন্ডাশি পাউডার", "gram", None),
    ("fnb_hot_range", "spices", "Salt", "লবণ", "gram", None),
    ("fnb_hot_range", "prep", "Miso Paste", "মিসো পেস্ট", "gram", None),
    ("fnb_hot_range", "sauces", "Chili Paste", "মরিচের পেস্ট", "gram", None),
    ("fnb_hot_range", "sauces", "Tare", "টারে", "gram", None),
    ("fnb_hot_range", "sauces", "Chua Huh Chili Paste", "চুয়া হুহ চিলি পেস্ট", "gram", None),
    ("fnb_hot_range", "sauces", "Mayo", "মায়ো", "gram", None),
    ("fnb_hot_range", "sauces", "Vinegar", "ভিনেগার", "millilitre", None),
    ("fnb_hot_range", "sauces", "Dark Soy Sauce", "ডার্ক সয়া সস", "millilitre", None),
    ("fnb_hot_range", "sauces", "Light Soy Sauce", "হালকা সয়া সস", "millilitre", None),
    ("fnb_hot_range", "sauces", "Prawn Sauce Maker", "চিংড়ি সস প্রস্তুতকারক", "gram", None),
    ("fnb_hot_range", "sauces", "Oyster Sauce", "অয়েস্টার সস", "millilitre", None),
    ("fnb_hot_range", "sauces", "Mirin", "মিরিন", "millilitre", None),
    ("fnb_hot_range", "sauces", "Concentrate Vinegar", "ঘন ভিনেগার", "millilitre", None),
    ("fnb_hot_range", "rice", "Sticky Sushi Rice", "স্টিকি সুশি রাইস", "gram", None),
    ("fnb_hot_range", "rice", "Staff Rice", "স্টাফ রাইস", "gram", None),
    ("fnb_hot_range", "pulses", "Soyabean", "সয়াবিন", "gram", None),
    ("fnb_hot_range", "pulses", "Staff Dal", "স্টাফ ডাল", "gram", None),
    ("fnb_hot_range", "prep", "Cream Cheese Mushroom Filling", "ক্রিম চিজ মাশরুম ফিলিং", "gram", None),
    ("fnb_hot_range", "prep", "Pork Gyoza Filling", "পোর্ক গিয়োজা ফিলিং", "gram", None),
    ("fnb_hot_range", "prep", "Chicken Gyoza Filling", "চিকেন গিয়োজা ফিলিং", "gram", None),
    ("fnb_hot_range", "poultry", "Egg", "ডিম", "piece", None),
    ("fnb_hot_range", "packaging", "Aluminium Foil Roll", "অ্যালুমিনিয়াম ফয়েল রোল", "roll", None),
    ("fnb_hot_range", "oils", "Sesame Oil", "তিলের তেল", "millilitre", None),
    ("fnb_hot_range", "oils", "Refined Oil", "পরিশোধিত তেল", "millilitre", None),
    ("fnb_hot_range", "oils", "Mustard Oil", "সর্ষের তেল", "millilitre", None),
    ("fnb_hot_range", "oils", "Truffle Oil", "ট্রাফল তেল", "millilitre", None),
    ("fnb_hot_range", "meat", "Chicken Sausage", "মুরগির সসেজ", "gram", None),
    ("fnb_hot_range", "meat", "Pork Keema", "শুকরের কিমা", "gram", None),
    ("fnb_hot_range", "meat", "Boiled Pork", "সেদ্ধ শুকরের মাংস", "gram", None),
    ("fnb_hot_range", "meat", "Chicken Bones", "মুরগির হাড়", "gram", None),
    ("fnb_hot_range", "meat", "Chicken Breast Boneless", "হাড়বিহীন মুরগির বুকের মাংস", "gram", None),
    ("fnb_hot_range", "meat", "Chicken Leg Boneless", "হাড়বিহীন মুরগির রান", "gram", None),
    ("fnb_hot_range", "meat", "Pork Belly", "শুকরের পেটের মাংস", "gram", None),
    ("fnb_hot_range", "meat", "Pork Fat", "শূকরের চর্বি", "gram", None),
    ("fnb_hot_range", "meat", "Chicken Claws", "মুরগির নখ", "gram", None),
    ("fnb_hot_range", "meat", "Chicken Fat", "মুরগির চর্বি", "gram", None),
    ("fnb_hot_range", "herbs", "Mint", "পুদিনা", "gram", None),
    ("fnb_hot_range", "herbs", "Basil", "তুলসী", "gram", None),
    ("fnb_hot_range", "herbs", "Coriander", "ধনিয়া", "gram", None),
    ("fnb_hot_range", "herbs", "Bay Leaf", "তেজপাতা", "gram", None),
    ("fnb_hot_range", "flour", "Maida Flour", "ময়দা", "gram", None),
    ("fnb_hot_range", "flour", "Tempura Flour", "টেম্পুরা ফ্লাওয়ার", "gram", None),
    (
        "fnb_hot_range",
        "dry_goods",
        "Panko",
        "পাংকো",
        "gram",
        "Source sheet read 'Habit Panko' with Bengali 'অভ্যাস' (= 'habit'), a "
        "literal mistranslation. Recorded as Panko breadcrumbs - confirm.",
    ),
    ("fnb_hot_range", "dry_goods", "Gyoza Wrap", "গিয়োজা র‍্যাপ", "piece", None),
    ("fnb_hot_range", "dry_goods", "Nori Sheet", "নোরি শীট", "piece", None),
    ("fnb_hot_range", "dry_goods", "Puffed Rice", "মুড়ি", "gram", None),
    ("fnb_hot_range", "dairy", "Brown Garlic Butter", "বাদামী রসুনের মাখন", "gram", None),
    ("fnb_hot_range", "dairy", "Butter", "মাখন", "gram", None),
    ("fnb_hot_range", "dairy", "Blended Cheese", "মিশ্রিত পনির", "gram", None),
    ("fnb_hot_range", "dairy", "Paneer", "পনির", "gram", None),
    (
        "fnb_hot_range",
        "alcohol",
        "White Chinese Cooking Wine",
        "সাদা চীনা রান্নার ওয়াইন",
        "millilitre",
        None,
    ),
    ("fnb_hot_range", "alcohol", "Cooking Wine", "রান্নার ওয়াইন", "millilitre", None),
]

# Par levels from "Kitchen Prep (Mise-en-place) & Beverage Thresholds", the only
# source document that records minimums. Keyed (department, name).
PAR_LEVELS: list[tuple[str, str, float, str]] = [
    ("beverages", "Club Soda", 12, "12 bottles in hand"),
    ("beverages", "Sugar Syrup", 2000, "2 Ltr in hand"),
    ("beverages", "Lemon", 30, "30 pcs in hand"),
    ("beverages", "Ice Cube", 10, "10 kg in hand"),
    ("fnb_hot_range", "Egg", 30, "30 ajitama always ready"),
    ("fnb_hot_range", "Spring Onion", 500, "500g always cut"),
]

EXPECTED_TOTAL = 151


def sql_str(value: str | None) -> str:
    if value is None:
        return "null"
    return "'" + value.replace("'", "''") + "'"


def main() -> int:
    counts: dict[str, int] = {}
    for dept, *_ in ITEMS:
        counts[dept] = counts.get(dept, 0) + 1

    if len(ITEMS) != EXPECTED_TOTAL:
        print(f"ERROR: {len(ITEMS)} items, expected {EXPECTED_TOTAL}", file=sys.stderr)
        return 1

    dept_keys = {d[0] for d in DEPARTMENTS}
    cat_keys = {c[0] for c in CATEGORIES}
    for dept, cat, name, *_ in ITEMS:
        if dept not in dept_keys:
            print(f"ERROR: unknown department {dept!r} on {name!r}", file=sys.stderr)
            return 1
        if cat not in cat_keys:
            print(f"ERROR: unknown category {cat!r} on {name!r}", file=sys.stderr)
            return 1

    seen: set[tuple[str, str]] = set()
    for dept, _cat, name, *_ in ITEMS:
        key = (dept, name.lower())
        if key in seen:
            print(f"ERROR: duplicate {name!r} in {dept}", file=sys.stderr)
            return 1
        seen.add(key)

    lines: list[str] = []
    add = lines.append

    add("-- " + "-" * 73)
    add("-- 002 — Inventory catalogue")
    add("--")
    add("-- GENERATED by scripts/generate_inventory_seed.py — do not hand-edit.")
    add("-- Transcribed from AKIRA's paper count sheets (Aug 2026).")
    add("--")
    for key, label, _bn, _o in DEPARTMENTS:
        add(f"--   {label:<34} {counts.get(key, 0):>3} items")
    add(f"--   {'TOTAL':<34} {len(ITEMS):>3} items")
    add("--")
    add("-- Idempotent: re-running updates labels and leaves existing items alone.")
    add("-- " + "-" * 73)
    add("")

    add("insert into inventory_departments (key, label, label_bn, sort_order) values")
    rows = [
        f"    ({sql_str(k)}, {sql_str(la)}, {sql_str(bn)}, {o})" for k, la, bn, o in DEPARTMENTS
    ]
    add(",\n".join(rows))
    add("on conflict (key) do update set")
    add("    label = excluded.label,")
    add("    label_bn = excluded.label_bn,")
    add("    sort_order = excluded.sort_order;")
    add("")

    add("insert into inventory_categories (key, label, label_bn, sort_order) values")
    rows = [f"    ({sql_str(k)}, {sql_str(la)}, {sql_str(bn)}, {o})" for k, la, bn, o in CATEGORIES]
    add(",\n".join(rows))
    add("on conflict (key) do update set")
    add("    label = excluded.label,")
    add("    label_bn = excluded.label_bn,")
    add("    sort_order = excluded.sort_order;")
    add("")

    add("-- Items are matched to their department and category by key, so this file")
    add("-- never hardcodes a uuid.")
    add("insert into inventory_items (name, name_bn, department_id, category_id, unit, notes)")
    add("select v.name, v.name_bn, d.id, c.id, v.unit::inventory_unit, v.notes")
    add("from (values")
    rows = [
        "    ("
        + ", ".join(
            [
                sql_str(name),
                sql_str(name_bn),
                sql_str(dept),
                sql_str(cat),
                sql_str(unit),
                sql_str(note),
            ]
        )
        + ")"
        for dept, cat, name, name_bn, unit, note in ITEMS
    ]
    add(",\n".join(rows))
    add(") as v(name, name_bn, dept_key, cat_key, unit, notes)")
    add("join inventory_departments d on d.key = v.dept_key")
    add("left join inventory_categories c on c.key = v.cat_key")
    add("where not exists (")
    add("    select 1 from inventory_items existing")
    add("    where existing.department_id = d.id")
    add("      and lower(existing.name) = lower(v.name)")
    add("      and existing.deleted_at is null")
    add(");")
    add("")

    add("-- Par levels, for every outlet that exists. Only the mise-en-place sheet")
    add("-- carries minimums, so most items start with no par level and an admin")
    add("-- sets them per outlet.")
    add("insert into inventory_outlet_levels (outlet_id, item_id, par_level, is_stocked)")
    add("select o.id, i.id, v.par_level, true")
    add("from (values")
    rows = [
        f"    ({sql_str(name)}, {sql_str(dept)}, {par}::numeric)"
        for dept, name, par, _src in PAR_LEVELS
    ]
    add(",\n".join(rows))
    add(") as v(name, dept_key, par_level)")
    add("join inventory_departments d on d.key = v.dept_key")
    add("join inventory_items i on i.department_id = d.id and lower(i.name) = lower(v.name)")
    add("cross join outlets o")
    add("where o.deleted_at is null")
    add("on conflict (outlet_id, item_id) do nothing;")
    add("")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    print(f"wrote {OUT.relative_to(ROOT)}")
    for key, label, _bn, _o in DEPARTMENTS:
        print(f"  {label:<34} {counts.get(key, 0):>3}")
    print(f"  {'TOTAL':<34} {len(ITEMS):>3}")
    flagged = [n for *_r, n in ((i[0], i[2], i[5]) for i in ITEMS) if n]
    print(f"  {len(flagged)} items carry a data-quality note")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
