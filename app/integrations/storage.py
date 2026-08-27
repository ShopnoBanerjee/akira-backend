"""Supabase Storage, service-side.

The browser never gets the service key. This API mints a signed upload URL for
one exact object path; the browser PUTs the bytes straight to Storage with it;
then the API confirms the object really exists before writing any metadata row.
Photo metadata is only ever written after the object is confirmed — a row
pointing at a missing object is how galleries end up full of broken images.

**The client is shared, and signing is batched.** Both were measured, on the
six photos a real run detail screen asks for:

    a fresh AsyncClient per path, serially .... 4716 ms
    one shared client, serially ............... 1989 ms
    one shared client, gathered ...............  953 ms
    the batch sign endpoint, one request ......  395 ms

Building an `AsyncClient` inside each call throws away the connection pool it
exists to hold, so every signature paid for a new TLS handshake — 2.7 s of the
4.7 s above, spent re-introducing this process to a host it had just finished
talking to.
"""

from dataclasses import dataclass

import httpx

from app.core.config import get_settings
from app.core.errors import AppError

SOP_PHOTO_BUCKET = "sop-photos"

#: Petpooja exports, kept so a file can be re-parsed under a newer adapter
#: without asking anybody to export it again. Private, like the photos: these
#: files carry customer phone numbers and names.
SALES_UPLOAD_BUCKET = "sales-uploads"

TIMEOUT = 20


class StorageError(AppError):
    status_code = 502
    title = "Storage Error"
    type_uri = "https://akira.ops/errors/storage"


_shared: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    """The one client, kept open for the life of the process.

    Not a context manager on purpose. Every caller below used to open and close
    its own, which discarded the keep-alive connection each time; the module
    docstring has what that cost. Closed by `aclose_client()` from the app
    lifespan.
    """
    global _shared
    if _shared is None or _shared.is_closed:
        settings = get_settings()
        _shared = httpx.AsyncClient(
            base_url=settings.SUPABASE_URL.rstrip("/") + "/storage/v1",
            headers={
                "apikey": settings.SUPABASE_SECRET_KEY,
                "Authorization": f"Bearer {settings.SUPABASE_SECRET_KEY}",
            },
            timeout=TIMEOUT,
        )
    return _shared


async def aclose_client() -> None:
    global _shared
    if _shared is not None and not _shared.is_closed:
        await _shared.aclose()
    _shared = None


@dataclass(frozen=True)
class SignedUpload:
    #: Absolute URL the browser PUTs/POSTs the file to.
    url: str
    #: Token supabase-js uploadToSignedUrl() wants alongside the path.
    token: str
    path: str


async def create_signed_upload(
    path: str, *, upsert: bool = True, bucket: str = SOP_PHOTO_BUCKET
) -> SignedUpload:
    """A one-object upload grant. The path is fixed server-side, so a client
    can never choose where its bytes land."""
    client = _client()
    response = await client.post(
        f"/object/upload/sign/{bucket}/{path}",
        headers={"x-upsert": "true"} if upsert else {},
    )
    if response.status_code >= 400:
        raise StorageError(
            "Could not prepare the photo upload. Try again.",
            extra={"provider_status": response.status_code},
        )
    payload = response.json()
    settings = get_settings()
    relative = str(payload.get("url", ""))
    token = str(payload.get("token", ""))
    absolute = settings.SUPABASE_URL.rstrip("/") + "/storage/v1" + relative
    return SignedUpload(url=absolute, token=token, path=path)


@dataclass(frozen=True)
class ObjectStat:
    exists: bool
    size_bytes: int
    content_type: str


async def stat_object(path: str, *, bucket: str = SOP_PHOTO_BUCKET) -> ObjectStat:
    """Does the object actually exist, and how big is it?"""
    client = _client()
    response = await client.get(f"/object/info/{bucket}/{path}")
    if response.status_code == 404 or response.status_code == 400:
        return ObjectStat(exists=False, size_bytes=0, content_type="")
    if response.status_code >= 400:
        raise StorageError(
            "Could not verify the uploaded photo.",
            extra={"provider_status": response.status_code},
        )
    info = response.json()
    return ObjectStat(
        exists=True,
        size_bytes=int(info.get("size") or info.get("metadata", {}).get("size") or 0),
        content_type=str(info.get("contentType") or info.get("metadata", {}).get("mimetype") or ""),
    )


async def download_object(path: str, *, bucket: str = SOP_PHOTO_BUCKET) -> bytes:
    """The raw bytes, service-side.

    Only the background integrity pass uses this. It never runs in a request:
    a 3MB download plus a perceptual hash inside photo-confirm would make the
    floor wait on work nobody on the floor is waiting for.
    """
    client = _client()
    response = await client.get(f"/object/{bucket}/{path}")
    if response.status_code >= 400:
        raise StorageError(
            "Could not read the photo back from storage.",
            extra={"provider_status": response.status_code, "path": path},
        )
    return response.content


async def create_signed_view_urls(
    paths: list[str], *, expires_in: int = 300, bucket: str = SOP_PHOTO_BUCKET
) -> dict[str, str]:
    """Sign many objects in one request.

    Storage takes a list of paths and returns a signature for each, so a run
    detail screen with a dozen photos costs one round trip rather than a dozen.
    A path that cannot be signed — deleted object, bad path — comes back with
    an `error` and is simply absent from the returned mapping. The caller then
    renders that photo as unavailable, which is the truth, instead of the whole
    screen failing because one object went missing.
    """
    if not paths:
        return {}
    unique = list(dict.fromkeys(paths))
    client = _client()
    response = await client.post(
        f"/object/sign/{bucket}",
        json={"expiresIn": expires_in, "paths": unique},
    )
    if response.status_code >= 400:
        raise StorageError(
            "Could not prepare the photos for viewing.",
            extra={"provider_status": response.status_code},
        )
    prefix = get_settings().SUPABASE_URL.rstrip("/") + "/storage/v1"
    signed: dict[str, str] = {}
    for entry in response.json():
        if entry.get("error") or not entry.get("signedURL"):
            continue
        # Storage echoes the path back without the leading slash it was given.
        signed[str(entry["path"]).lstrip("/")] = prefix + str(entry["signedURL"])
    return signed


async def upload_bytes(
    path: str, data: bytes, *, bucket: str, content_type: str, upsert: bool = True
) -> str:
    """Put bytes we already hold straight into Storage.

    The signed-URL dance exists so the browser never gets the service key. When
    the API is the one holding the bytes — an .xlsx that arrived on a request —
    a round trip through a signed URL buys nothing.
    """
    client = _client()
    response = await client.post(
        f"/object/{bucket}/{path}",
        content=data,
        headers={
            "Content-Type": content_type,
            **({"x-upsert": "true"} if upsert else {}),
        },
    )
    if response.status_code >= 400:
        raise StorageError(
            "Could not store the uploaded file.",
            extra={"provider_status": response.status_code, "body": response.text[:200]},
        )
    return path


async def ensure_private_bucket(
    bucket: str, *, file_size_limit: int, allowed_mime_types: list[str]
) -> None:
    """Create a private bucket if it does not exist. Idempotent."""
    client = _client()
    response = await client.post(
        "/bucket",
        json={
            "id": bucket,
            "name": bucket,
            "public": False,
            "file_size_limit": file_size_limit,
            "allowed_mime_types": allowed_mime_types,
        },
    )
    if response.status_code >= 400 and "already exists" not in response.text.lower():
        raise StorageError(
            f"Could not create the {bucket} bucket.",
            extra={"provider_status": response.status_code, "body": response.text[:200]},
        )


async def ensure_bucket() -> None:
    """Create the private photo bucket if it does not exist. Idempotent."""
    client = _client()
    response = await client.post(
        "/bucket",
        json={
            "id": SOP_PHOTO_BUCKET,
            "name": SOP_PHOTO_BUCKET,
            "public": False,
            # Matches integrity.photo_max_bytes default. Storage enforces it
            # even if a client bypasses the API's own check.
            "file_size_limit": 5 * 1024 * 1024,
            "allowed_mime_types": ["image/jpeg", "image/png", "image/webp"],
        },
    )
    # 400 "already exists" is fine; anything else is not.
    if response.status_code >= 400 and "already exists" not in response.text.lower():
        raise StorageError(
            "Could not create the photo bucket.",
            extra={"provider_status": response.status_code, "body": response.text[:200]},
        )
