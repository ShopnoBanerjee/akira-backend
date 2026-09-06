"""The training walkthrough's rules (D31), against a real schema.

Pure rules first (track by role, who may skip, who may restart), then the
record-keeping through the service: idempotent start, monotonic steps, a
completion that survives content versions, a restart that supersedes and
remembers who asked, and the owner's view scoped to what the caller may see.
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.core.deps import CurrentUser, OutletMembership
from app.core.enums import UserRole
from app.core.errors import ConflictError, ForbiddenError, NotFoundError
from app.domains.training import service
from app.domains.training.service import Trainee, can_reset, can_skip, track_for

V1 = "floor.v1"
V2 = "floor.v2"
MV1 = "management.v1"
#: The development organisation (0026): where the seeded outlets live.
DEV_ORG = uuid.UUID("a1000000-0000-4000-8000-000000000002")


# --- Pure rules ----------------------------------------------------------------


class TestTheRules:
    @pytest.mark.parametrize(
        ("role", "track"),
        [
            (UserRole.OWNER, "management"),
            (UserRole.OPS_MANAGER, "management"),
            (UserRole.OUTLET_MANAGER, "management"),
            (UserRole.SHIFT_LEAD, "floor"),
            (UserRole.STAFF, "floor"),
        ],
    )
    def test_the_track_follows_the_role(self, role: UserRole, track: str) -> None:
        assert track_for(role) == track

    def test_only_the_owner_may_skip(self) -> None:
        assert can_skip(UserRole.OWNER)
        for role in (
            UserRole.OPS_MANAGER,
            UserRole.OUTLET_MANAGER,
            UserRole.SHIFT_LEAD,
            UserRole.STAFF,
        ):
            assert not can_skip(role), role

    def test_the_owner_restarts_anyone_without_delegation(self) -> None:
        assert can_reset(
            actor_role=UserRole.OWNER,
            actor_delegated=False,
            actor_outlets=set(),
            target_outlets={uuid.uuid4()},
        )

    def test_a_manager_without_delegation_restarts_nobody(self) -> None:
        o = uuid.uuid4()
        for role in (UserRole.OPS_MANAGER, UserRole.OUTLET_MANAGER):
            assert not can_reset(
                actor_role=role, actor_delegated=False, actor_outlets={o}, target_outlets={o}
            )

    def test_a_delegated_ops_manager_restarts_anywhere(self) -> None:
        assert can_reset(
            actor_role=UserRole.OPS_MANAGER,
            actor_delegated=True,
            actor_outlets=set(),
            target_outlets={uuid.uuid4()},
        )

    def test_a_delegated_outlet_manager_only_at_a_shared_outlet(self) -> None:
        mine, theirs = uuid.uuid4(), uuid.uuid4()
        assert can_reset(
            actor_role=UserRole.OUTLET_MANAGER,
            actor_delegated=True,
            actor_outlets={mine},
            target_outlets={mine, theirs},
        )
        assert not can_reset(
            actor_role=UserRole.OUTLET_MANAGER,
            actor_delegated=True,
            actor_outlets={mine},
            target_outlets={theirs},
        )

    def test_floor_roles_cannot_hold_the_delegation(self) -> None:
        o = uuid.uuid4()
        for role in (UserRole.SHIFT_LEAD, UserRole.STAFF):
            assert not can_reset(
                actor_role=role, actor_delegated=True, actor_outlets={o}, target_outlets={o}
            )


# --- The record --------------------------------------------------------------------


@pytest.fixture
async def session(migrated_db: str):  # type: ignore[no-untyped-def]
    engine = create_async_engine(migrated_db.replace("postgresql://", "postgresql+asyncpg://"))
    async with AsyncSession(engine, expire_on_commit=False) as db:
        try:
            yield db
        finally:
            await db.rollback()
            await db.execute(text("delete from training_records"))
            await db.execute(text("delete from audit_log where entity_table = 'training_records'"))
            await db.execute(
                text(
                    "delete from outlet_members where profile_id in "
                    "(select id from profiles where full_name like 'Trn %')"
                )
            )
            await db.execute(text("delete from profiles where full_name like 'Trn %'"))
            await db.execute(text("delete from auth.users where email like 'trn-%@akira.test'"))
            await db.commit()
    await engine.dispose()


async def _person(
    db: AsyncSession,
    role: UserRole,
    *outlet_codes: str,
    delegated: bool = False,
) -> CurrentUser:
    pid = uuid.uuid4()
    name = f"Trn {role.value} {str(pid)[:4]}"
    await db.execute(
        text("insert into auth.users (id, email) values (:id, :e)"),
        {"id": pid, "e": f"trn-{pid}@akira.test"},
    )
    await db.execute(
        text(
            "insert into profiles (id, full_name, global_role, is_active, can_restart_training,"
            " organisation_id) values (:id, :n, :r, true, :d,"
            " (select organisation_id from outlets where code = 'AKR-NT01'))"
        ),
        {"id": pid, "n": name, "r": role.value, "d": delegated},
    )
    memberships = []
    for code in outlet_codes:
        row = (
            (await db.execute(text("select id, name from outlets where code = :c"), {"c": code}))
            .mappings()
            .one()
        )
        await db.execute(
            text(
                "insert into outlet_members (outlet_id, profile_id, role_at_outlet, is_primary)"
                " values (:o, :p, :r, true)"
            ),
            {"o": row["id"], "p": pid, "r": role.value},
        )
        memberships.append(
            OutletMembership(
                outlet_id=row["id"],
                outlet_code=code,
                outlet_name=row["name"],
                role_at_outlet=role,
                is_primary=True,
            )
        )
    await db.commit()
    org_outlets = (
        await db.execute(
            text(
                "select id from outlets where deleted_at is null and is_active and"
                " organisation_id = (select organisation_id from outlets where code = 'AKR-NT01')"
            )
        )
    ).scalars()
    return CurrentUser(
        profile_id=pid,
        full_name=name,
        email=None,
        global_role=role,
        is_active=True,
        memberships=memberships,
        organisation_id=DEV_ORG,
        organisation_outlet_ids=frozenset(org_outlets),
    )


def _trainee(user: CurrentUser, device_id: uuid.UUID | None = None) -> Trainee:
    return Trainee(
        profile_id=user.profile_id,
        full_name=user.full_name,
        role=user.global_role,
        device_id=device_id,
    )


@pytest.mark.asyncio
class TestFirstTime:
    async def test_a_new_staff_member_is_required_and_cannot_skip(
        self, session: AsyncSession
    ) -> None:
        staff = await _person(session, UserRole.STAFF, "AKR-NT01")
        st = await service.status(session, _trainee(staff), version=V1)
        assert st.required and not st.can_skip and st.record is None
        assert st.track == "floor"

    async def test_start_is_idempotent_and_keeps_the_first_language(
        self, session: AsyncSession
    ) -> None:
        staff = await _person(session, UserRole.STAFF, "AKR-NT01")
        who = _trainee(staff)
        first = await service.start(session, who, version=V1, total_steps=6, language="bn")
        again = await service.start(session, who, version=V1, total_steps=6, language="en")
        assert again.id == first.id and again.language == "bn"
        n = await session.scalar(
            text("select count(*) from training_records where profile_id = :p"),
            {"p": staff.profile_id},
        )
        assert n == 1

    async def test_steps_are_recorded_and_never_go_backwards(self, session: AsyncSession) -> None:
        staff = await _person(session, UserRole.STAFF, "AKR-NT01")
        who = _trainee(staff)
        rec = await service.start(session, who, version=V1, total_steps=6, language="en")
        await service.advance(session, who, record_id=rec.id, step=1)
        await service.advance(session, who, record_id=rec.id, step=3)
        back = await service.advance(session, who, record_id=rec.id, step=2)
        assert back.last_step == 3 and back.status == "in_progress"
        events = await session.scalar(
            text("select jsonb_array_length(steps) from training_records where id = :id"),
            {"id": rec.id},
        )
        assert events == 3
        with pytest.raises(ConflictError, match="past the end"):
            await service.advance(session, who, record_id=rec.id, step=7)

    async def test_completing_clears_required_and_is_audited(self, session: AsyncSession) -> None:
        staff = await _person(session, UserRole.STAFF, "AKR-NT01")
        who = _trainee(staff)
        rec = await service.start(session, who, version=V1, total_steps=6, language="en")
        done = await service.complete(session, who, record_id=rec.id)
        assert done.status == "completed" and done.last_step == 6
        assert not (await service.status(session, who, version=V1)).required
        audited = await session.scalar(
            text(
                "select count(*) from audit_log where entity_table = 'training_records'"
                " and entity_id = :id and actor_profile_id = :p"
            ),
            {"id": rec.id, "p": staff.profile_id},
        )
        assert audited == 1
        with pytest.raises(ConflictError, match="already closed"):
            await service.advance(session, who, record_id=rec.id, step=1)

    async def test_a_completion_survives_a_content_version_bump(
        self, session: AsyncSession
    ) -> None:
        staff = await _person(session, UserRole.STAFF, "AKR-NT01")
        who = _trainee(staff)
        rec = await service.start(session, who, version=V1, total_steps=6, language="en")
        await service.complete(session, who, record_id=rec.id)
        st = await service.status(session, who, version=V2)
        assert not st.required, "only a restart may re-require the tour (D31)"

    async def test_somebody_elses_attempt_is_not_found(self, session: AsyncSession) -> None:
        a = await _person(session, UserRole.STAFF, "AKR-NT01")
        b = await _person(session, UserRole.STAFF, "AKR-NT01")
        rec = await service.start(session, _trainee(a), version=V1, total_steps=6, language="en")
        with pytest.raises(NotFoundError):
            await service.advance(session, _trainee(b), record_id=rec.id, step=1)

    async def test_the_device_it_ran_on_is_kept(self, session: AsyncSession) -> None:
        staff = await _person(session, UserRole.STAFF, "AKR-NT01")
        outlet = await session.scalar(text("select id from outlets where code = 'AKR-NT01'"))
        device_auth = uuid.uuid4()
        device_id = await session.scalar(
            text(
                "insert into outlet_devices (outlet_id, auth_user_id, label)"
                " values (:o, :a, 'Trn tablet') returning id"
            ),
            {"o": outlet, "a": device_auth},
        )
        await session.commit()
        try:
            rec = await service.start(
                session, _trainee(staff, device_id), version=V1, total_steps=6, language="en"
            )
            stored = await session.scalar(
                text("select device_id from training_records where id = :id"), {"id": rec.id}
            )
            assert stored == device_id
        finally:
            await session.execute(
                text("delete from training_records where profile_id = :p"), {"p": staff.profile_id}
            )
            await session.execute(
                text("delete from outlet_devices where id = :id"), {"id": device_id}
            )
            await session.commit()


@pytest.mark.asyncio
class TestSkipping:
    async def test_the_owner_may_skip(self, session: AsyncSession) -> None:
        owner = await _person(session, UserRole.OWNER)
        who = _trainee(owner)
        st = await service.status(session, who, version=MV1)
        assert st.required and st.can_skip and st.track == "management"
        rec = await service.start(session, who, version=MV1, total_steps=9, language="en")
        skipped = await service.skip(session, who, record_id=rec.id)
        assert skipped.status == "skipped"
        assert not (await service.status(session, who, version=MV1)).required

    async def test_an_ops_manager_may_not(self, session: AsyncSession) -> None:
        ops = await _person(session, UserRole.OPS_MANAGER)
        who = _trainee(ops)
        rec = await service.start(session, who, version=MV1, total_steps=9, language="en")
        with pytest.raises(ForbiddenError, match="Only the owner"):
            await service.skip(session, who, record_id=rec.id)
        assert (await service.status(session, who, version=MV1)).required


@pytest.mark.asyncio
class TestRestarting:
    async def test_a_restart_makes_it_required_again_and_remembers_who_asked(
        self, session: AsyncSession
    ) -> None:
        owner = await _person(session, UserRole.OWNER)
        staff = await _person(session, UserRole.STAFF, "AKR-NT01")
        who = _trainee(staff)
        rec = await service.start(session, who, version=V1, total_steps=6, language="en")
        await service.complete(session, who, record_id=rec.id)

        person = await service.reset(session, owner, staff.profile_id)
        assert person.status == "reset" and person.reset_at is not None
        assert person.reset_by_name == owner.full_name

        st = await service.status(session, who, version=V1)
        assert st.required and st.record is None
        fresh = await service.start(session, who, version=V1, total_steps=6, language="bn")
        assert fresh.id != rec.id
        assert fresh.triggered_by == owner.profile_id
        assert fresh.triggered_by_name == owner.full_name

    async def test_restarting_someone_who_never_started_still_records_the_request(
        self, session: AsyncSession
    ) -> None:
        owner = await _person(session, UserRole.OWNER)
        staff = await _person(session, UserRole.STAFF, "AKR-NT01")
        person = await service.reset(session, owner, staff.profile_id)
        assert person.status == "reset" and person.reset_by_name == owner.full_name
        fresh = await service.start(
            session, _trainee(staff), version=V1, total_steps=6, language="en"
        )
        assert fresh.triggered_by == owner.profile_id

    async def test_an_undelegated_manager_is_refused(self, session: AsyncSession) -> None:
        manager = await _person(session, UserRole.OUTLET_MANAGER, "AKR-NT01")
        staff = await _person(session, UserRole.STAFF, "AKR-NT01")
        with pytest.raises(ForbiddenError, match="delegated"):
            await service.reset(session, manager, staff.profile_id)

    async def test_a_delegated_outlet_manager_only_reaches_their_own_people(
        self, session: AsyncSession
    ) -> None:
        manager = await _person(session, UserRole.OUTLET_MANAGER, "AKR-NT01", delegated=True)
        mine = await _person(session, UserRole.STAFF, "AKR-NT01")
        theirs = await _person(session, UserRole.STAFF, "AKR-DEV02")
        person = await service.reset(session, manager, mine.profile_id)
        assert person.status == "reset"
        with pytest.raises(ForbiddenError):
            await service.reset(session, manager, theirs.profile_id)


@pytest.mark.asyncio
class TestTheOwnersView:
    async def test_statuses_and_scope(self, session: AsyncSession) -> None:
        owner = await _person(session, UserRole.OWNER)
        manager = await _person(session, UserRole.OUTLET_MANAGER, "AKR-NT01")
        done = await _person(session, UserRole.STAFF, "AKR-NT01")
        midway = await _person(session, UserRole.STAFF, "AKR-NT01")
        elsewhere = await _person(session, UserRole.STAFF, "AKR-DEV02")

        rec = await service.start(session, _trainee(done), version=V1, total_steps=6, language="en")
        await service.complete(session, _trainee(done), record_id=rec.id)
        rec2 = await service.start(
            session, _trainee(midway), version=V1, total_steps=6, language="bn"
        )
        await service.advance(session, _trainee(midway), record_id=rec2.id, step=2)

        # A voluntary re-run after completing must not blank the completion date.
        rerun = await service.start(
            session, _trainee(done), version=V1, total_steps=6, language="en"
        )
        assert rerun.id != rec.id and rerun.status == "not_started"

        everyone = {p.profile_id: p for p in await service.people(session, owner)}
        assert everyone[done.profile_id].status == "completed"
        assert everyone[done.profile_id].completed_at is not None
        assert everyone[midway.profile_id].status == "in_progress"
        assert (everyone[midway.profile_id].last_step, everyone[midway.profile_id].total_steps) == (
            2,
            6,
        )
        assert everyone[midway.profile_id].language == "bn"
        assert everyone[elsewhere.profile_id].status == "not_started"
        assert all(p.can_reset for p in everyone.values()), "the owner may restart anyone"

        # An outlet manager sees only their outlet's people, and cannot reset
        # until delegated.
        visible = {p.profile_id: p for p in await service.people(session, manager)}
        assert done.profile_id in visible and midway.profile_id in visible
        assert elsewhere.profile_id not in visible
        assert not any(p.can_reset for p in visible.values())

    async def test_the_timestamps_are_timezone_aware(self, session: AsyncSession) -> None:
        staff = await _person(session, UserRole.STAFF, "AKR-NT01")
        rec = await service.start(
            session, _trainee(staff), version=V1, total_steps=6, language="en"
        )
        assert rec.started_at.tzinfo is not None
        assert rec.started_at <= datetime.now(UTC)
