"""Actor assertions for the shared tablet.

The tablet's Supabase session authenticates the DEVICE; a staff PIN identifies
the PERSON. A successful PIN check mints a short-lived, HMAC-signed actor
assertion binding (profile, device, outlet). Floor endpoints on a device
session require it, and resolve submitted_by/started_by from it.

Deliberate properties (see CLAUDE.md "Auth model — shared outlet tablet"):

- It is NOT a Supabase JWT and grants nothing outside the floor endpoints.
  Management routes never accept it.
- It is bound to one device and one outlet. Stolen and replayed from another
  device account, verification fails on the device id.
- It expires within a shift, and handover on the tablet drops it client-side.
- A PIN can never approve a run. Approval endpoints (P6) take individual
  manager logins only.
"""

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass

from app.core.config import get_settings
from app.core.errors import AuthError

#: Long enough to cover a double shift, short enough that a tablet left
#: signed-in overnight is useless by morning prep.
ACTOR_TTL_SECONDS = 12 * 3600

_ALGORITHM = "AKIRA-HMAC-SHA256-v1"


def _key() -> bytes:
    settings = get_settings()
    secret = settings.SUPABASE_SECRET_KEY
    if not secret:
        raise RuntimeError("SUPABASE_SECRET_KEY must be set to mint actor tokens.")
    # Derive a purpose-specific key so this material is never the raw secret.
    return hashlib.sha256(f"actor-token:{secret}".encode()).digest()


@dataclass(frozen=True)
class Actor:
    profile_id: uuid.UUID
    device_id: uuid.UUID
    outlet_id: uuid.UUID
    full_name: str
    role: str
    expires_at: int


def _encode(payload: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


def mint(
    *,
    profile_id: uuid.UUID,
    device_id: uuid.UUID,
    outlet_id: uuid.UUID,
    full_name: str,
    role: str,
) -> tuple[str, int]:
    """Returns (token, expires_at_epoch)."""
    expires_at = int(time.time()) + ACTOR_TTL_SECONDS
    body = _encode(
        {
            "alg": _ALGORITHM,
            "p": str(profile_id),
            "d": str(device_id),
            "o": str(outlet_id),
            "n": full_name,
            "r": role,
            "exp": expires_at,
        }
    )
    signature = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}", expires_at


def verify(token: str, *, device_id: uuid.UUID) -> Actor:
    """Verify signature, expiry and device binding. Raises AuthError with a
    message safe to show on the tablet."""
    try:
        body, signature = token.rsplit(".", 1)
        expected = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise AuthError("Identify yourself with your PIN again.")
        payload = json.loads(base64.urlsafe_b64decode(body))
    except AuthError:
        raise
    except Exception as exc:
        raise AuthError("Identify yourself with your PIN again.") from exc

    if payload.get("alg") != _ALGORITHM:
        raise AuthError("Identify yourself with your PIN again.")
    if int(payload.get("exp", 0)) < time.time():
        raise AuthError("Your shift sign-in has expired. Enter your PIN again.")
    if payload.get("d") != str(device_id):
        # A token minted on another tablet. Refuse: the binding is the point.
        raise AuthError("Identify yourself with your PIN on this tablet.")

    return Actor(
        profile_id=uuid.UUID(payload["p"]),
        device_id=uuid.UUID(payload["d"]),
        outlet_id=uuid.UUID(payload["o"]),
        full_name=str(payload.get("n", "")),
        role=str(payload.get("r", "staff")),
        expires_at=int(payload["exp"]),
    )
