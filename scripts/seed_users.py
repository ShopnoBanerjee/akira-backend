"""Create the mock users, devices and PINs for testing.

Users cannot be seeded by SQL. ``profiles.id`` references ``auth.users``, which
Supabase Auth owns; inserting those rows directly produces accounts that look
correct in the table and cannot sign in — no password hash, no confirmation
state, none of the encrypted columns GoTrue maintains. So this goes through the
Auth Admin API and then writes the matching application rows.

    uv run python scripts/seed_users.py

Idempotent. Re-running reuses existing auth users and refreshes their profile,
membership and PIN rather than creating duplicates.

Passwords are generated, never hardcoded, and written to .seed-credentials.md
(gitignored). PINs are fixed and obvious because they exist to be typed during
testing — see MOCK_PIN_WARNING below.

THESE ARE TEST ACCOUNTS. Every one is @akira.test, which is not a deliverable
domain, so none of them can receive mail or be used for a real password reset.
Delete them before this system carries anything that matters.
"""

import asyncio
import os
import secrets
import string
import sys
from dataclasses import dataclass, field
from pathlib import Path

import asyncpg
import httpx
from argon2 import PasswordHasher

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CREDENTIALS_FILE = ROOT / ".seed-credentials.md"

MOCK_PIN_WARNING = (
    "PINs here are sequential on purpose so they can be typed quickly while "
    "testing. Real staff PINs must be set individually through the admin UI."
)


@dataclass
class MockUser:
    email: str
    full_name: str
    global_role: str
    outlets: list[str]
    #: Role held at each outlet. Defaults to the global role.
    role_at_outlet: str | None = None
    pin: str | None = None
    employee_code: str | None = None
    password: str = field(default="", repr=False)


# One per role, plus a second shift lead so separation of duties can actually be
# exercised, and a manager at the second outlet so cross-outlet denial can be.
MOCK_USERS: list[MockUser] = [
    MockUser(
        "owner@akira.test",
        "Ano (Owner)",
        "owner",
        ["AKR-NT01", "AKR-DEV02"],
        employee_code="AK-001",
    ),
    MockUser(
        "ops@akira.test",
        "Ops Manager",
        "ops_manager",
        ["AKR-NT01", "AKR-DEV02"],
        employee_code="AK-002",
    ),
    MockUser(
        "manager.nt@akira.test",
        "New Town Manager",
        "outlet_manager",
        ["AKR-NT01"],
        employee_code="AK-003",
    ),
    MockUser(
        "lead.nt@akira.test",
        "New Town Shift Lead",
        "shift_lead",
        ["AKR-NT01"],
        pin="1111",
        employee_code="AK-004",
    ),
    MockUser(
        "lead2.nt@akira.test",
        "New Town Shift Lead 2",
        "shift_lead",
        ["AKR-NT01"],
        pin="2222",
        employee_code="AK-005",
    ),
    MockUser(
        "staff.nt@akira.test",
        "New Town Staff",
        "staff",
        ["AKR-NT01"],
        pin="3333",
        employee_code="AK-006",
    ),
    MockUser(
        "staff2.nt@akira.test",
        "New Town Staff 2",
        "staff",
        ["AKR-NT01"],
        pin="4444",
        employee_code="AK-007",
    ),
    # Second outlet, so "this user must not see that outlet" is testable.
    MockUser(
        "manager.dev@akira.test",
        "Dev Outlet Manager",
        "outlet_manager",
        ["AKR-DEV02"],
        employee_code="AK-008",
    ),
    MockUser(
        "staff.dev@akira.test",
        "Dev Outlet Staff",
        "staff",
        ["AKR-DEV02"],
        pin="5555",
        employee_code="AK-009",
    ),
]

# The shared tablets. Each holds one outlet-bound session; individual staff then
# identify with a PIN, so submitted_by still resolves to a real person.
MOCK_DEVICES: list[tuple[str, str, str]] = [
    ("device.nt01@akira.test", "AKR-NT01", "New Town — kitchen pass tablet"),
    ("device.dev02@akira.test", "AKR-DEV02", "Dev Outlet 2 — floor tablet"),
]


def env(key: str) -> str:
    value = os.environ.get(key, "").strip()
    if not value:
        print(f"ERROR: {key} is not set. Fill in .env first.", file=sys.stderr)
        raise SystemExit(1)
    return value


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def strong_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "Ak!" + "".join(secrets.choice(alphabet) for _ in range(18))


class AuthAdmin:
    """The slice of the Supabase Auth Admin API this script needs."""

    def __init__(self, base_url: str, secret_key: str) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/") + "/auth/v1",
            headers={"apikey": secret_key, "Authorization": f"Bearer {secret_key}"},
            timeout=30,
        )

    def find_by_email(self, email: str) -> str | None:
        # The admin list endpoint pages; the filter narrows it enough that one
        # page is always sufficient for a seed of this size.
        resp = self._client.get("/admin/users", params={"page": 1, "per_page": 200})
        resp.raise_for_status()
        for user in resp.json().get("users", []):
            if user.get("email", "").lower() == email.lower():
                return str(user["id"])
        return None

    def create(self, email: str, password: str, full_name: str) -> str:
        resp = self._client.post(
            "/admin/users",
            json={
                "email": email,
                "password": password,
                # No confirmation mail: @akira.test cannot receive any, and
                # these accounts must be usable immediately.
                "email_confirm": True,
                "user_metadata": {"full_name": full_name, "seeded": True},
            },
        )
        if resp.status_code >= 400:
            raise RuntimeError(f"creating {email} failed: {resp.status_code} {resp.text}")
        return str(resp.json()["id"])

    def set_password(self, user_id: str, password: str) -> None:
        resp = self._client.put(f"/admin/users/{user_id}", json={"password": password})
        resp.raise_for_status()

    def close(self) -> None:
        self._client.close()


async def main() -> int:
    load_dotenv(ROOT / ".env")

    supabase_url = env("SUPABASE_URL")
    secret_key = env("SUPABASE_SECRET_KEY")
    database_url = env("DATABASE_URL").replace("postgresql+asyncpg://", "postgresql://")

    hasher = PasswordHasher()
    auth = AuthAdmin(supabase_url, secret_key)
    conn = await asyncpg.connect(database_url)

    created: list[str] = []
    reused: list[str] = []

    try:
        outlet_ids: dict[str, str] = {
            r["code"]: str(r["id"])
            for r in await conn.fetch("select id, code from outlets where deleted_at is null")
        }
        missing = {code for u in MOCK_USERS for code in u.outlets if code not in outlet_ids} | {
            d[1] for d in MOCK_DEVICES if d[1] not in outlet_ids
        }
        if missing:
            print(
                f"ERROR: outlets {sorted(missing)} do not exist. "
                "Apply supabase/seed/001_outlets_and_sop.sql first.",
                file=sys.stderr,
            )
            return 1

        for user in MOCK_USERS:
            user.password = strong_password()
            existing = auth.find_by_email(user.email)
            if existing:
                user_id = existing
                auth.set_password(user_id, user.password)
                reused.append(user.email)
            else:
                user_id = auth.create(user.email, user.password, user.full_name)
                created.append(user.email)

            pin_hash = hasher.hash(user.pin) if user.pin else None

            await conn.execute(
                """
                insert into profiles
                    (id, full_name, employee_code, global_role, pin_hash, pin_set_at, is_active)
                values ($1::uuid, $2, $3, $4::user_role, $5::text,
                        case when $5::text is null then null else now() end, true)
                on conflict (id) do update set
                    full_name     = excluded.full_name,
                    employee_code = excluded.employee_code,
                    global_role   = excluded.global_role,
                    pin_hash      = excluded.pin_hash,
                    pin_set_at    = excluded.pin_set_at,
                    is_active     = true,
                    deleted_at    = null
                """,
                user_id,
                user.full_name,
                user.employee_code,
                user.global_role,
                pin_hash,
            )

            for code in user.outlets:
                await conn.execute(
                    """
                    insert into outlet_members
                        (outlet_id, profile_id, role_at_outlet, is_primary)
                    values ($1::uuid, $2::uuid, $3::user_role, $4)
                    on conflict (outlet_id, profile_id) do update set
                        role_at_outlet = excluded.role_at_outlet,
                        deleted_at     = null
                    """,
                    outlet_ids[code],
                    user_id,
                    user.role_at_outlet or user.global_role,
                    code == user.outlets[0],
                )

        device_rows: list[tuple[str, str, str]] = []
        for email, code, label in MOCK_DEVICES:
            password = strong_password()
            existing = auth.find_by_email(email)
            if existing:
                device_id = existing
                auth.set_password(device_id, password)
                reused.append(email)
            else:
                device_id = auth.create(email, password, label)
                created.append(email)

            await conn.execute(
                """
                insert into outlet_devices (outlet_id, auth_user_id, label, is_active)
                values ($1::uuid, $2::uuid, $3, true)
                on conflict (auth_user_id) do update set
                    label      = excluded.label,
                    outlet_id  = excluded.outlet_id,
                    is_active  = true,
                    deleted_at = null
                """,
                outlet_ids[code],
                device_id,
                label,
            )
            device_rows.append((email, password, label))

        write_credentials(device_rows)

    finally:
        auth.close()
        await conn.close()

    print(f"created {len(created)}, reused {len(reused)}")
    for email in created:
        print(f"  + {email}")
    for email in reused:
        print(f"  = {email} (password rotated)")
    print(f"\ncredentials written to {CREDENTIALS_FILE.name} (gitignored)")
    return 0


def write_credentials(devices: list[tuple[str, str, str]]) -> None:
    lines = [
        "# Seeded test credentials",
        "",
        "**Generated by `scripts/seed_users.py`. Gitignored. Test accounts only.**",
        "",
        "Every address is `@akira.test`, which is not a deliverable domain: these",
        "accounts cannot receive mail and cannot do a real password reset. Delete",
        "them before this system carries anything that matters.",
        "",
        "Re-running the script rotates every password below.",
        "",
        "## People",
        "",
        "| Email | Password | Role | Outlets | PIN |",
        "|---|---|---|---|---|",
    ]
    for u in MOCK_USERS:
        lines.append(
            f"| `{u.email}` | `{u.password}` | {u.global_role} | "
            f"{', '.join(u.outlets)} | {u.pin or '—'} |"
        )
    lines += [
        "",
        f"> {MOCK_PIN_WARNING}",
        "",
        "## Shared tablets",
        "",
        "Each device holds one outlet-bound session. Staff then identify with a",
        "PIN, so a run is still attributed to a real person.",
        "",
        "| Email | Password | Outlet |",
        "|---|---|---|",
    ]
    for email, password, label in devices:
        lines.append(f"| `{email}` | `{password}` | {label} |")
    lines.append("")
    CREDENTIALS_FILE.write_text("\n".join(lines), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
