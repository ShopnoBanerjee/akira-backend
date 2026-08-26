"""Dump the OpenAPI schema to openapi.json.

The frontend repo generates src/types/api.ts from this file, so the two repos
stay in step without needing a running API. Run after any endpoint change:

    uv run python scripts/export_openapi.py
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The project is not installed as a package (see [tool.uv] package = false), so
# running this file directly puts scripts/ on sys.path rather than the root.
sys.path.insert(0, str(ROOT))

from app.main import app  # noqa: E402

OUT = ROOT / "openapi.json"


def main() -> None:
    OUT.write_text(
        json.dumps(app.openapi(), indent=2) + "\n",
        encoding="utf-8",
        # Force LF on Windows, so the CI drift check compares content and not
        # line endings.
        newline="\n",
    )
    print(f"wrote {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
