"""The per-caller identity cache in app/core/deps.py.

Every authenticated request used to pay one round trip to learn who was
calling before its handler did anything. The cache removes that trip for a
minute per caller. These tests pin the two things that make that safe: a
usable identity is served from memory until it is forgotten or expires, and
an identity that is NOT usable — pending, deactivated, unknown — is never
cached, so activating someone takes effect on their very next request.

No database. The dependency's one statement is stubbed, and what is asserted
is whether it ran.
"""

import json
import uuid
from typing import Any

import pytest
from fastapi import Request

from app.core import deps
from app.core.errors import PendingActivationError
from app.core.security import TokenClaims

pytestmark = pytest.mark.asyncio

ORG = uuid.UUID("a1000000-0000-4000-8000-000000000002")


class _Result:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def mappings(self) -> "_Result":
        return self

    def first(self) -> dict[str, Any] | None:
        return self._row


class FakeDb:
    """Answers the identity statement with a fixed row and counts the asks."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row
        self.calls = 0

    async def execute(self, *_: Any, **__: Any) -> _Result:
        self.calls += 1
        return _Result(self.row)


def _request() -> Request:
    return Request({"type": "http", "method": "GET", "path": "/", "headers": []})


def _claims(subject: uuid.UUID) -> TokenClaims:
    return TokenClaims(subject=str(subject), email="x@akira.test", expires_at=2**31, raw={})


def _profile_row(profile_id: uuid.UUID, *, active: bool = True, role: str = "owner") -> dict:
    return {
        "device_id": None,
        "device_outlet_id": None,
        "device_label": None,
        "profile_id": profile_id,
        "full_name": "Cache Test",
        "global_role": role,
        "is_active": active,
        "deleted_at": None,
        "memberships": json.dumps([]),
        "organisation_id": ORG,
        "organisation_slug": "akira-dev",
        "organisation_name": "AKIRA (development)",
        "organisation_active": True,
        "organisation_deleted_at": None,
        "organisation_onboarded_at": None,
        "organisation_outlet_ids": [],
    }


def _device_row(device_id: uuid.UUID) -> dict:
    return {
        "device_id": device_id,
        "device_outlet_id": uuid.uuid4(),
        "device_label": "NT tablet",
        "profile_id": None,
        "full_name": None,
        "global_role": None,
        "is_active": None,
        "deleted_at": None,
        "memberships": None,
        "organisation_id": ORG,
        "organisation_slug": "akira-dev",
        "organisation_name": "AKIRA (development)",
        "organisation_active": True,
        "organisation_deleted_at": None,
        "organisation_onboarded_at": None,
        "organisation_outlet_ids": [],
    }


@pytest.fixture(autouse=True)
def _clean_cache() -> None:
    deps.forget_all_identities()


async def _call(db: FakeDb, subject: uuid.UUID) -> deps.CurrentUser:
    return await deps.current_user(_request(), _claims(subject), db)  # type: ignore[arg-type]


class TestAUsableIdentityIsServedFromMemory:
    async def test_the_second_request_does_not_ask_the_database(self) -> None:
        pid = uuid.uuid4()
        db = FakeDb(_profile_row(pid))
        first = await _call(db, pid)
        second = await _call(db, pid)
        assert db.calls == 1
        assert first.profile_id == second.profile_id == pid
        assert second.global_role.value == "owner"

    async def test_a_device_session_is_cached_too(self) -> None:
        """The shared tablet makes more requests than anyone, and its
        identity — device, outlet, label — is the most stable of all."""
        did = uuid.uuid4()
        db = FakeDb(_device_row(did))
        await _call(db, did)
        user = await _call(db, did)
        assert db.calls == 1
        assert user.device is not None and user.device.device_id == did

    async def test_two_callers_do_not_share_an_entry(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()
        db_a, db_b = FakeDb(_profile_row(a)), FakeDb(_profile_row(b, role="staff"))
        ua = await _call(db_a, a)
        ub = await _call(db_b, b)
        assert ua.global_role.value == "owner" and ub.global_role.value == "staff"
        assert db_a.calls == 1 and db_b.calls == 1


class TestForgettingAndExpiry:
    async def test_forget_identity_forces_a_reload(self) -> None:
        """What every user write calls after its commit. A role change must
        be visible on the next request, not the next minute."""
        pid = uuid.uuid4()
        db = FakeDb(_profile_row(pid, role="staff"))
        assert (await _call(db, pid)).global_role.value == "staff"

        db.row = _profile_row(pid, role="owner")
        assert (await _call(db, pid)).global_role.value == "staff", "still cached"

        deps.forget_identity(pid)
        assert (await _call(db, pid)).global_role.value == "owner"
        assert db.calls == 2

    async def test_forget_accepts_strings_uuids_and_none(self) -> None:
        pid = uuid.uuid4()
        db = FakeDb(_profile_row(pid))
        await _call(db, pid)
        deps.forget_identity(None, str(pid))
        await _call(db, pid)
        assert db.calls == 2

    async def test_forget_all_clears_everyone(self) -> None:
        a, b = uuid.uuid4(), uuid.uuid4()
        db_a, db_b = FakeDb(_profile_row(a)), FakeDb(_profile_row(b))
        await _call(db_a, a)
        await _call(db_b, b)
        deps.forget_all_identities()
        await _call(db_a, a)
        await _call(db_b, b)
        assert db_a.calls == 2 and db_b.calls == 2

    async def test_an_entry_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The TTL is the backstop for a change made outside this process."""
        pid = uuid.uuid4()
        db = FakeDb(_profile_row(pid))
        now = [1000.0]
        monkeypatch.setattr(deps.time, "monotonic", lambda: now[0])
        await _call(db, pid)
        now[0] += deps.IDENTITY_CACHE_TTL_SECONDS - 1
        await _call(db, pid)
        assert db.calls == 1
        now[0] += 2
        await _call(db, pid)
        assert db.calls == 2

    async def test_a_zero_ttl_disables_caching(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(deps, "IDENTITY_CACHE_TTL_SECONDS", 0)
        pid = uuid.uuid4()
        db = FakeDb(_profile_row(pid))
        await _call(db, pid)
        await _call(db, pid)
        assert db.calls == 2


class TestOnlyUsableIdentitiesAreCached:
    async def test_a_pending_account_is_re_read_every_time(self) -> None:
        """Activating someone must work on their next click. If the pending
        row were cached, the admin's change would sit invisible for a minute
        and read as a bug."""
        pid = uuid.uuid4()
        db = FakeDb(_profile_row(pid, active=False))
        with pytest.raises(PendingActivationError):
            await _call(db, pid)
        with pytest.raises(PendingActivationError):
            await _call(db, pid)
        assert db.calls == 2

        db.row = _profile_row(pid, active=True)
        user = await _call(db, pid)
        assert user.is_active and db.calls == 3

    async def test_a_deleted_account_is_not_cached(self) -> None:
        from app.core.errors import AuthError

        pid = uuid.uuid4()
        row = _profile_row(pid)
        row["deleted_at"] = "2026-09-01T00:00:00+00:00"
        db = FakeDb(row)
        for _ in range(2):
            with pytest.raises(AuthError):
                await _call(db, pid)
        assert db.calls == 2

    async def test_the_per_request_memo_still_wins(self) -> None:
        """Within one request the first resolution is reused via
        request.state, exactly as before the cross-request cache existed."""
        pid = uuid.uuid4()
        db = FakeDb(_profile_row(pid))
        request = _request()
        claims = _claims(pid)
        first = await deps.current_user(request, claims, db)  # type: ignore[arg-type]
        deps.forget_all_identities()
        second = await deps.current_user(request, claims, db)  # type: ignore[arg-type]
        assert first is second and db.calls == 1
