"""Supabase JWT verification, and staff PIN hashing.

This project's Supabase instance signs asymmetrically with ES256 (verified
against the live JWKS endpoint at kickoff — see docs/DECISIONS.md D5). There is
no shared HS256 secret, and none should ever be configured: a symmetric secret
that can verify a token can also mint one.
"""

import time
from dataclasses import dataclass
from typing import Any

import httpx
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from jose import jwt
from jose.exceptions import ExpiredSignatureError, JWTError

from app.core.config import Settings
from app.core.errors import AuthError

#: Supabase issues access tokens with this audience.
EXPECTED_AUDIENCE = "authenticated"

#: Re-fetch the key set at most this often, absent a kid miss.
JWKS_TTL_SECONDS = 600

#: Tolerance for clock skew between this server and Supabase.
LEEWAY_SECONDS = 10


@dataclass(frozen=True)
class TokenClaims:
    """The parts of a verified access token this application uses."""

    subject: str
    email: str | None
    expires_at: int
    raw: dict[str, Any]

    @property
    def assurance_level(self) -> str:
        """Supabase's `aal` claim: `aal1` after a password, `aal2` after a
        second factor. Owners and platform admins must reach aal2 (D33)."""
        return str(self.raw.get("aal") or "aal1")

    @property
    def is_anonymous(self) -> bool:
        """Supabase can mint tokens for anonymous sign-ins. Those must never
        reach an application endpoint."""
        return bool(self.raw.get("is_anonymous", False))


class JWKSCache:
    """Caches Supabase's public keys, refreshing on expiry or on a kid miss.

    A kid miss means Supabase rotated its signing key. Refetching once on that
    signal is the difference between a seamless rotation and every request
    failing until a redeploy.
    """

    def __init__(self, jwks_url: str, ttl: int = JWKS_TTL_SECONDS) -> None:
        self._url = jwks_url
        self._ttl = ttl
        self._keys: dict[str, dict[str, Any]] = {}
        self._fetched_at: float = 0.0

    def _fetch(self) -> None:
        try:
            response = httpx.get(self._url, timeout=10)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise AuthError(
                "Could not reach the identity provider to verify your session."
            ) from exc
        self._keys = {key["kid"]: key for key in response.json().get("keys", []) if "kid" in key}
        self._fetched_at = time.monotonic()

    def get(self, kid: str) -> dict[str, Any]:
        stale = time.monotonic() - self._fetched_at > self._ttl
        if not self._keys or stale:
            self._fetch()
        if kid not in self._keys:
            # Probably a rotation. Refetch once before giving up.
            self._fetch()
        key = self._keys.get(kid)
        if key is None:
            raise AuthError("Your session was signed with an unrecognised key.")
        return key

    def clear(self) -> None:
        self._keys = {}
        self._fetched_at = 0.0


class TokenVerifier:
    def __init__(self, settings: Settings) -> None:
        if not settings.SUPABASE_JWKS_URL:
            raise RuntimeError("SUPABASE_JWKS_URL is not configured.")
        self._jwks = JWKSCache(settings.SUPABASE_JWKS_URL)
        self._issuer = settings.SUPABASE_URL.rstrip("/") + "/auth/v1"

    def verify(self, token: str) -> TokenClaims:
        try:
            header = jwt.get_unverified_header(token)
        except JWTError as exc:
            raise AuthError("That session token is malformed.") from exc

        kid = header.get("kid")
        if not kid:
            raise AuthError("That session token is missing a key id.")

        algorithm = header.get("alg")
        # Pin the algorithm to what the key actually declares. Accepting whatever
        # the header claims is how alg-confusion attacks work.
        if algorithm not in {"ES256", "RS256"}:
            raise AuthError(f"Unsupported token algorithm: {algorithm}.")

        key = self._jwks.get(kid)

        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[algorithm],
                audience=EXPECTED_AUDIENCE,
                issuer=self._issuer,
                options={
                    "verify_signature": True,
                    "verify_aud": True,
                    "verify_iss": True,
                    "verify_exp": True,
                    "leeway": LEEWAY_SECONDS,
                },
            )
        except ExpiredSignatureError as exc:
            raise AuthError("Your session has expired. Sign in again.") from exc
        except JWTError as exc:
            raise AuthError("Your session could not be verified.") from exc

        subject = claims.get("sub")
        if not subject:
            raise AuthError("That session token identifies no one.")

        token_claims = TokenClaims(
            subject=str(subject),
            email=claims.get("email"),
            expires_at=int(claims.get("exp", 0)),
            raw=claims,
        )
        if token_claims.is_anonymous:
            raise AuthError("Anonymous sessions cannot access this application.")
        return token_claims


# ---------------------------------------------------------------------------
# Staff PINs
# ---------------------------------------------------------------------------

# A 4-digit PIN has ~13 bits of entropy, so the hash has to carry the weight the
# secret does not. These parameters cost roughly 50ms per verification, which is
# unnoticeable on a tablet and ruinous for an offline brute force.
_pin_hasher = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=2)

#: A PIN alone is never enough. It is only accepted on a device already
#: authenticated to that staff member's outlet, it authorises floor actions
#: only, and it can never approve a run. See CLAUDE.md.
MIN_PIN_LENGTH = 4
MAX_PIN_LENGTH = 8


def hash_pin(pin: str) -> str:
    validate_pin_format(pin)
    return _pin_hasher.hash(pin)


def verify_pin(pin_hash: str | None, pin: str) -> bool:
    if not pin_hash:
        return False
    try:
        return _pin_hasher.verify(pin_hash, pin)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def pin_needs_rehash(pin_hash: str) -> bool:
    """True when the stored hash used weaker parameters than we now require."""
    try:
        return _pin_hasher.check_needs_rehash(pin_hash)
    except InvalidHashError:
        return True


def validate_pin_format(pin: str) -> None:
    if not pin.isdigit():
        raise ValueError("A PIN must be digits only.")
    if not MIN_PIN_LENGTH <= len(pin) <= MAX_PIN_LENGTH:
        raise ValueError(f"A PIN must be between {MIN_PIN_LENGTH} and {MAX_PIN_LENGTH} digits.")
