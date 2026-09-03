"""Score an extraction provider against the hand-labelled golden set.

    uv run python scripts/eval_extractor.py gemini
    uv run python scripts/eval_extractor.py groq gemini      # head to head

Why this exists: provider choices in this codebase are made by measurement,
not preference (D17), and this script is where the measurement lives. It runs
page 1 of the real requisition PDF through the named provider(s) and scores
each handwritten cell against `tests/fixtures/golden_page1.json` — a human's
reading of the full-resolution scan, with genuinely ambiguous cells carrying
every faithful transcription rather than a pretended certainty.

Three numbers per provider, and what they mean:

- cell accuracy   — of the 60 handwritten cells, how many were transcribed
                    to an accepted reading. The headline.
- row integrity   — cells whose WRONG value is another row's RIGHT value.
                    This is the silent-corruption failure (Groq's measured
                    mode); a provider can have decent cell accuracy and still
                    be unusable if its errors are neighbours' values.
- blank fidelity  — blanks read as blanks. An invented number on an
                    uncounted row becomes a phantom on-hand quantity.

Needs the provider's key in .env. Run it whenever the prompt, the model, or
the provider changes — the numbers go in the commit message.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.domains.inventory.counts_service import _page_images
from app.integrations import sheet_extraction

GOLDEN = Path("tests/fixtures/golden_page1.json")

# The scan itself is NOT in the repo. It is a real AKIRA stock sheet carrying
# staff handwriting and signatures, and these repos are public, so it lives on
# the machine that needs it. Put it anywhere and point this at it:
#
#     set AKIRA_REQUISITION_PDF=C:\path\to\requisition_27aug2026.pdf
#
# The default below is inside the gitignored local/ directory, so dropping the
# file at local/requisition_27aug2026.pdf is enough. The golden set stays in
# the repo: it is a transcription, not the sheet, and it is what makes the
# provider choice measured rather than remembered (D17).
PDF = Path(os.environ.get("AKIRA_REQUISITION_PDF", "local/requisition_27aug2026.pdf"))


def normalise_cell(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip().lower().replace(" ", "")
    return text if text else None


async def run_provider(provider: str, page: bytes, vocabulary: list[str]) -> dict:
    from app.core import config

    # Point the dispatcher at the provider under test for this call only.
    settings = config.get_settings()
    original = settings.STOCK_EXTRACT_PROVIDER
    object.__setattr__(settings, "STOCK_EXTRACT_PROVIDER", provider)
    try:
        result = await sheet_extraction.extract_page(page, vocabulary=vocabulary)
    finally:
        object.__setattr__(settings, "STOCK_EXTRACT_PROVIDER", original)
    return {
        "model": result.model,
        "latency_ms": result.latency_ms,
        "rows": {
            row.sl_no: {
                "item": row.item_name,
                "closing": row.closing_count_raw,
                "requisition": row.requisition_raw,
            }
            for row in result.page.rows
            if row.sl_no is not None
        },
    }


def score(golden: dict, extracted: dict) -> dict:
    total = correct = 0
    blanks_total = blanks_correct = 0
    row_shift = 0
    misses: list[str] = []

    # Every accepted non-null reading anywhere on the sheet, for shift checks.
    all_values: dict[str, list[int]] = {}
    for row in golden["rows"]:
        for column in ("closing", "requisition"):
            for accepted in row[column]["accept"]:
                if accepted is not None:
                    all_values.setdefault(normalise_cell(accepted), []).append(row["sl_no"])

    for row in golden["rows"]:
        got = extracted["rows"].get(row["sl_no"], {})
        for column in ("closing", "requisition"):
            accepted = {normalise_cell(a) for a in row[column]["accept"]}
            value = normalise_cell(got.get(column))
            total += 1
            is_blank_truth = accepted == {None}
            if is_blank_truth:
                blanks_total += 1
            if value in accepted:
                correct += 1
                if is_blank_truth:
                    blanks_correct += 1
            else:
                owners = all_values.get(value, [])
                shifted = bool(value) and any(o != row["sl_no"] for o in owners)
                row_shift += 1 if shifted else 0
                misses.append(
                    f"row {row['sl_no']:>2} {row['item_name'][:22]:22} {column:11} "
                    f"got {str(got.get(column))!r:14} wanted one of "
                    f"{list(row[column]['accept'])}" + ("  << NEIGHBOUR'S VALUE" if shifted else "")
                )

    return {
        "cell_accuracy": f"{correct}/{total} ({100 * correct / total:.0f}%)",
        "row_shift_errors": row_shift,
        "blank_fidelity": f"{blanks_correct}/{blanks_total}",
        "misses": misses,
    }


def load_inputs() -> tuple[dict, bytes]:
    return json.loads(GOLDEN.read_text(encoding="utf-8")), _page_images(
        PDF.read_bytes(), "application/pdf"
    )[0]


async def main() -> None:
    providers = sys.argv[1:] or ["gemini"]
    golden, page = load_inputs()
    vocabulary = sorted({row["item_name"] for row in golden["rows"]})

    for provider in providers:
        print(f"\n=== {provider} ===")
        try:
            extracted = await run_provider(provider, page, vocabulary)
        except Exception as exc:  # pragma: no cover - operator tool
            import traceback

            print(f"  failed: {type(exc).__name__}: {exc}")
            traceback.print_exc()
            continue
        result = score(golden, extracted)
        print(f"  model:            {extracted['model']}  ({extracted['latency_ms']} ms)")
        print(f"  cell accuracy:    {result['cell_accuracy']}")
        print(f"  row-shift errors: {result['row_shift_errors']}  (the silent-corruption count)")
        print(f"  blank fidelity:   {result['blank_fidelity']}")
        if result["misses"]:
            print("  misses:")
            for miss in result["misses"]:
                print(f"    {miss}")


if __name__ == "__main__":
    asyncio.run(main())
