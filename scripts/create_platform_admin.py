"""Create (or reset) the platform admin login: the account above organisations.

    uv run python scripts/create_platform_admin.py platform@simplyakira.com "Platform"

There is no API for this on purpose (D33): the role that creates tenants is
created by the person holding the database and the Supabase secret, at a
keyboard, and the password is shown ONCE. The account belongs to no
organisation, must enrol a second factor on first sign-in, and can read but
never write inside any organisation.

Idempotent: an existing auth user gets a new password; an existing profile is
re-pointed at the platform role and detached from any organisation.

Writes the credentials to local/platform-admin.md (gitignored) as well as
printing them, so a terminal that scrolls away does not lose the only copy.
"""

import argparse
import asyncio
import secrets
import string
import sys
from datetime import UTC, datetime
from pathlib import Path

import asyncpg
import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

OUT = ROOT / "local" / "platform-admin.md"
ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"


def _password() -> str:
    while True:
        candidate = "".join(secrets.choice(ALPHABET) for _ in range(22))
        if (
            any(c.islower() for c in candidate)
            and any(c.isupper() for c in candidate)
            and any(c.isdigit() for c in candidate)
            and any(c in "!@#$%^&*()-_=+" for c in candidate)
        ):
            return candidate


def _env(name: str) -> str:
    import os

    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"{name} is not set; put it in .env or the environment")
    return value


class Auth:
    def __init__(self, base_url: str, secret_key: str) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/auth/v1",
            headers={"apikey": secret_key, "Authorization": f"Bearer {secret_key}"},
            timeout=30,
        )

    def find(self, email: str) -> str | None:
        resp = self._client.get("/admin/users", params={"page": 1, "per_page": 200})
        resp.raise_for_status()
        for user in resp.json().get("users", []):
            if str(user.get("email", "")).lower() == email.lower():
                return str(user["id"])
        return None

    def create(self, email: str, password: str, full_name: str) -> str:
        resp = self._client.post(
            "/admin/users",
            json={
                "email": email,
                "password": password,
                "email_confirm": True,
                "user_metadata": {"full_name": full_name, "platform_admin": True},
            },
        )
        if resp.status_code >= 400:
            raise SystemExit(f"creating {email} failed: {resp.status_code} {resp.text}")
        return str(resp.json()["id"])

    def set_password(self, user_id: str, password: str) -> None:
        resp = self._client.put(f"/admin/users/{user_id}", json={"password": password})
        resp.raise_for_status()

    def close(self) -> None:
        self._client.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("email")
    parser.add_argument("full_name")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    auth = Auth(_env("SUPABASE_URL"), _env("SUPABASE_SECRET_KEY"))
    dsn = _env("DATABASE_URL").replace("postgresql+asyncpg://", "postgresql://")

    password = _password()
    existing = auth.find(args.email)
    if existing:
        auth.set_password(existing, password)
        user_id, verb = existing, "reset"
    else:
        user_id, verb = auth.create(args.email, password, args.full_name), "created"
    auth.close()

    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(
            """
            insert into profiles (id, full_name, global_role, is_active, organisation_id)
            values ($1, $2, 'platform_admin', true, null)
            on conflict (id) do update set
                full_name       = excluded.full_name,
                global_role     = 'platform_admin',
                is_active       = true,
                deleted_at      = null,
                organisation_id = null
            """,
            user_id,
            args.full_name,
        )
        await conn.execute(
            "delete from outlet_members where profile_id = $1",
            user_id,
        )
        await conn.execute(
            """
            insert into audit_log (actor_profile_id, entity_table, entity_id, action, after)
            values ($1, 'profiles', $1, 'create',
                    jsonb_build_object('role', 'platform_admin', 'by', 'create_platform_admin.py'))
            """,
            user_id,
        )
    finally:
        await conn.close()

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(
        f"# Platform admin ({verb} {datetime.now(UTC).isoformat(timespec='seconds')})\n\n"
        f"- email: {args.email}\n"
        f"- password: `{password}`\n"
        f"- auth user id: {user_id}\n\n"
        "Change the password after first sign-in and enrol the authenticator app:\n"
        "the API refuses every call from this login until the session carries a\n"
        "second factor. This file is gitignored. Do not paste it anywhere.\n",
        encoding="utf-8",
    )
    print(f"platform admin {verb}: {args.email}")
    print(f"password (shown once): {password}")
    print(f"also written to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
