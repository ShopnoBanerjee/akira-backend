"""Check that the configured model-provider keys actually work.

    uv run python scripts/check_provider_keys.py

Prints whether each key is present and whether the vendor accepts it. It never
prints, logs or returns a key — only a length and a four-character prefix, so a
screenshot of the output is safe to share.

Written for key rotation: after editing `.env`, run this to confirm the new key
works before trusting a background job to discover it at 5am.
"""

import os
import pathlib
import sys

import httpx

ROOT = pathlib.Path(__file__).resolve().parent.parent


def load_env() -> dict[str, str]:
    out: dict[str, str] = {}
    env = ROOT / ".env"
    if not env.exists():
        return out
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def redact(value: str) -> str:
    if not value:
        return "(absent)"
    return f"{value[:4]}... len={len(value)}"


def check_openai_compat(key: str, base_url: str) -> tuple[bool, str]:
    """Any OpenAI-compatible endpoint (D28): GET {base}/models with the key."""
    r = httpx.get(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {key}"},
        timeout=20,
    )
    if r.status_code == 200:
        n = len(r.json().get("data", []))
        return True, f"accepted ({n} models visible at {base_url})"
    if r.status_code in (401, 403):
        return False, f"REJECTED by {base_url} — the key is wrong, revoked, or not active"
    return False, f"unexpected HTTP {r.status_code} from {base_url}"


def check_gemini(key: str) -> tuple[bool, str]:
    r = httpx.get(
        "https://generativelanguage.googleapis.com/v1beta/models",
        params={"key": key},
        timeout=20,
    )
    if r.status_code == 200:
        return True, f"accepted ({len(r.json().get('models', []))} models visible)"
    if r.status_code in (400, 401, 403):
        return False, "REJECTED — the key is wrong, revoked, or restricted"
    return False, f"unexpected HTTP {r.status_code}"


def check_anthropic(key: str) -> tuple[bool, str]:
    r = httpx.get(
        "https://api.anthropic.com/v1/models",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        timeout=20,
    )
    if r.status_code == 200:
        return True, "accepted"
    if r.status_code in (401, 403):
        return False, "REJECTED — the key is wrong or revoked"
    return False, f"unexpected HTTP {r.status_code}"


CHECKS = {
    "GEMINI_API_KEY": check_gemini,
    "GOOGLE_API_KEY": check_gemini,
    "ANTHROPIC_API_KEY": check_anthropic,
}

GEMINI_OPENAI_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


def main() -> int:
    env = {**load_env(), **{k: v for k, v in os.environ.items() if k in CHECKS}}
    any_failure = False
    checked = 0

    print("Provider keys (values never printed):\n")
    # The OpenAI-compatible endpoint resolves its key the way the app does:
    # its own key, else the Gemini key when the base URL is Gemini's.
    base = env.get("OPENAI_COMPAT_BASE_URL") or GEMINI_OPENAI_BASE
    compat_key = env.get("OPENAI_COMPAT_API_KEY") or (
        env.get("GEMINI_API_KEY", "") if "generativelanguage.googleapis.com" in base else ""
    )
    label = "OPENAI_COMPAT (resolved)"
    if compat_key:
        checked += 1
        try:
            ok, detail = check_openai_compat(compat_key, base)
        except httpx.HTTPError as exc:
            ok, detail = False, f"could not reach the endpoint: {type(exc).__name__}"
        mark = "OK  " if ok else "FAIL"
        print(f"  {label:<24} {redact(compat_key):<24} {mark}  {detail}")
        any_failure |= not ok
    else:
        print(f"  {label:<24} {'(no key resolves)':<24} skipped")
    for name, check in CHECKS.items():
        value = env.get(name, "")
        if not value:
            print(f"  {name:<20} {'(not set)':<24} skipped")
            continue
        checked += 1
        try:
            ok, detail = check(value)
        except httpx.HTTPError as exc:
            ok, detail = False, f"could not reach the vendor: {type(exc).__name__}"
        mark = "OK  " if ok else "FAIL"
        print(f"  {name:<20} {redact(value):<24} {mark}  {detail}")
        any_failure |= not ok

    if checked == 0:
        print("\nNo provider keys configured.")
        return 0
    print(
        "\nA rotated key that reports OK here is live. If it reports REJECTED, the"
        "\nold key may still be the one in .env, or the new one has not activated."
    )
    return 1 if any_failure else 0


if __name__ == "__main__":
    sys.exit(main())
