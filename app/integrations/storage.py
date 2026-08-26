"""Supabase Storage, service-side.

The browser never gets the service key. This API mints a signed upload URL for
one exact object path; the browser PUTs the bytes straight to Storage with it;
then the API confirms the object really exists before writing any metadata row.
Photo metadata is only ever written after the object is confirmed — a row
pointing at a missing object is how galleries end up full of broken images.
"""

from dataclasses import dataclass

import httpx

from app.core.config import get_settings
from app.core.errors import AppError

SOP_PHOTO_BUCKET = "sop-photos"
TIMEOUT = 20


class StorageError(AppError):
    status_code = 502
    title = "Storage Error"
    type_uri = "https://akira.ops/errors/storage"


def _client() -> httpx.AsyncClient:
    settings = get_settings()
    return httpx.AsyncClient(
        base_url=settings.SUPABASE_URL.rstrip("/") + "/storage/v1",
        headers={
            "apikey": settings.SUPABASE_SECRET_KEY,
            "Authorization": f"Bearer {settings.SUPABASE_SECRET_KEY}",
        },
        timeout=TIMEOUT,
    )


@dataclass(frozen=True)
class SignedUpload:
    #: Absolute URL the browser PUTs/POSTs the file to.
    url: str
    #: Token supabase-js uploadToSignedUrl() wants alongside the path.
    token: str
    path: str


async def create_signed_upload(path: str, *, upsert: bool = True) -> SignedUpload:
    """A one-object upload grant. The path is fixed server-side, so a client
    can never choose where its bytes land."""
    async with _client() as client:
        response = await client.post(
            f"/object/upload/sign/{SOP_PHOTO_BUCKET}/{path}",
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


async def stat_object(path: str) -> ObjectStat:
    """Does the object actually exist, and how big is it?"""
    async with _client() as client:
        response = await client.get(f"/object/info/{SOP_PHOTO_BUCKET}/{path}")
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


async def create_signed_view_url(path: str, *, expires_in: int = 300) -> str:
    """Short-lived read URL for the review screens. Never store these — mint
    per request."""
    async with _client() as client:
        response = await client.post(
            f"/object/sign/{SOP_PHOTO_BUCKET}/{path}",
            json={"expiresIn": expires_in},
        )
    if response.status_code >= 400:
        raise StorageError(
            "Could not prepare the photo for viewing.",
            extra={"provider_status": response.status_code},
        )
    settings = get_settings()
    return (
        settings.SUPABASE_URL.rstrip("/")
        + "/storage/v1"
        + str(response.json().get("signedURL", ""))
    )


async def ensure_bucket() -> None:
    """Create the private photo bucket if it does not exist. Idempotent."""
    async with _client() as client:
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
