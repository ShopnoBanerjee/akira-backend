"""Copy every Storage bucket and object from one Supabase project to another.

Part of the region move (docs/RUNBOOK_REGION_MOVE.md). pg_dump carries the
database; it does NOT carry the files behind `storage.objects` — the SOP
photos, the stored sales exports, the stock-sheet scan. Those live in the
project's object store and have to be copied through the Storage API, which
is what this does.

Idempotent: an object that already exists at the target with the same size
is skipped, so a run that dies halfway is simply run again. Buckets are
created private; nothing here is ever public (see docs/SECURITY.md).

    SOURCE_URL=https://xxx.supabase.co SOURCE_KEY=sb_secret_... \\
    TARGET_URL=https://yyy.supabase.co TARGET_KEY=sb_secret_... \\
    uv run python scripts/copy_storage.py [--dry-run]

Service-role (secret) keys on both sides. Reads them from the environment
only, never from a file, so a paste into a terminal is the only place they
appear.
"""

import os
import sys
from typing import Any

import httpx

DRY = "--dry-run" in sys.argv


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"{name} is not set")
    return value


SRC_URL, SRC_KEY = _env("SOURCE_URL").rstrip("/"), _env("SOURCE_KEY")
DST_URL, DST_KEY = _env("TARGET_URL").rstrip("/"), _env("TARGET_KEY")
if SRC_URL == DST_URL:
    sys.exit("source and target are the same project")


def _client(url: str, key: str) -> httpx.Client:
    return httpx.Client(
        base_url=f"{url}/storage/v1",
        headers={"Authorization": f"Bearer {key}", "apikey": key},
        timeout=120,
    )


def list_objects(c: httpx.Client, bucket: str, prefix: str = "") -> list[dict[str, Any]]:
    """Every object under a prefix, recursing into folders. The list endpoint
    returns folders as entries with no `id`; those are descended into."""
    out: list[dict[str, Any]] = []
    offset = 0
    while True:
        r = c.post(
            f"/object/list/{bucket}",
            json={
                "prefix": prefix,
                "limit": 1000,
                "offset": offset,
                "sortBy": {"column": "name", "order": "asc"},
            },
        )
        r.raise_for_status()
        page = r.json()
        for entry in page:
            path = f"{prefix}/{entry['name']}" if prefix else entry["name"]
            if entry.get("id") is None:  # a folder
                out.extend(list_objects(c, bucket, path))
            else:
                out.append(
                    {
                        "path": path,
                        "size": (entry.get("metadata") or {}).get("size"),
                        "mimetype": (entry.get("metadata") or {}).get("mimetype"),
                    }
                )
        if len(page) < 1000:
            return out
        offset += 1000


def head_size(c: httpx.Client, bucket: str, path: str) -> int | None:
    r = c.head(f"/object/{bucket}/{path}")
    if r.status_code != 200:
        return None
    try:
        return int(r.headers.get("content-length", "-1"))
    except ValueError:
        return None


def main() -> None:
    with _client(SRC_URL, SRC_KEY) as src, _client(DST_URL, DST_KEY) as dst:
        buckets = src.get("/bucket").raise_for_status().json()
        existing = {b["id"] for b in dst.get("/bucket").raise_for_status().json()}
        total_copied = total_skipped = total_failed = 0
        for b in buckets:
            name = b["id"]
            if name not in existing:
                print(f"bucket {name}: creating (private)")
                if not DRY:
                    dst.post(
                        "/bucket", json={"id": name, "name": name, "public": False}
                    ).raise_for_status()
            objects = list_objects(src, name)
            print(f"bucket {name}: {len(objects)} objects at source")
            copied = skipped = failed = 0
            for o in objects:
                have = head_size(dst, name, o["path"])
                if have is not None and (o["size"] is None or have == int(o["size"])):
                    skipped += 1
                    continue
                if DRY:
                    copied += 1
                    continue
                body = src.get(f"/object/{name}/{o['path']}")
                if body.status_code != 200:
                    print(f"  FAILED read  {o['path']} -> {body.status_code}")
                    failed += 1
                    continue
                put = dst.post(
                    f"/object/{name}/{o['path']}",
                    content=body.content,
                    headers={
                        "Content-Type": o["mimetype"]
                        or body.headers.get("content-type", "application/octet-stream"),
                        "x-upsert": "true",
                    },
                )
                if put.status_code not in (200, 201):
                    print(f"  FAILED write {o['path']} -> {put.status_code} {put.text[:120]}")
                    failed += 1
                    continue
                copied += 1
            print(f"  copied {copied}  skipped(already there) {skipped}  failed {failed}")
            total_copied += copied
            total_skipped += skipped
            total_failed += failed
        prefix = "DRY RUN — " if DRY else ""
        print(
            f"\n{prefix}total: copied {total_copied}, "
            f"skipped {total_skipped}, failed {total_failed}"
        )
        if total_failed:
            sys.exit(1)


if __name__ == "__main__":
    main()
