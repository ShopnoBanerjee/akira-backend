"""The slice of the Supabase Auth Admin API this application uses.

Identity is Supabase's job. This module is the only place that talks to it, so
there is exactly one thing to change if that ever stops being true.

The secret key used here bypasses RLS entirely and must never reach a browser.
"""

import httpx

from app.core.errors import AppError

TIMEOUT_SECONDS = 20


class AuthProviderError(AppError):
    """Supabase refused or could not be reached. Distinct from our own 4xx so
    it is obvious in logs whose fault a failure was."""

    status_code = 502
    title = "Identity Provider Error"
    type_uri = "https://akira.ops/errors/identity-provider"


class SupabaseAuthAdmin:
    def __init__(self, base_url: str, secret_key: str) -> None:
        if not base_url or not secret_key:
            raise RuntimeError(
                "SUPABASE_URL and SUPABASE_SECRET_KEY must be set to administer users."
            )
        self._base = base_url.rstrip("/") + "/auth/v1"
        self._headers = {
            "apikey": secret_key,
            "Authorization": f"Bearer {secret_key}",
        }

    async def find_by_email(self, email: str) -> str | None:
        """The auth user id for this address, or None."""
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            try:
                response = await client.get(
                    f"{self._base}/admin/users",
                    headers=self._headers,
                    params={"page": 1, "per_page": 200},
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise AuthProviderError(
                    "Could not reach the identity provider to check that address."
                ) from exc

        wanted = email.strip().lower()
        for user in response.json().get("users", []):
            if str(user.get("email", "")).lower() == wanted:
                return str(user["id"])
        return None

    async def invite(self, email: str, full_name: str) -> str:
        """Send an invitation and return the new auth user id.

        Supabase sends the mail and owns the token, so this application never
        handles a password or a reset link.
        """
        async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as client:
            try:
                response = await client.post(
                    f"{self._base}/invite",
                    headers=self._headers,
                    json={"email": email, "data": {"full_name": full_name}},
                )
            except httpx.HTTPError as exc:
                raise AuthProviderError(
                    "Could not reach the identity provider to send the invitation."
                ) from exc

        if response.status_code >= 400:
            raise AuthProviderError(
                "The identity provider rejected that invitation.",
                extra={"provider_status": response.status_code},
            )
        return str(response.json()["id"])
