"""The purge machinery (Group 5) with a recording fake store, SQLite.

:func:`request_purge` -- the only public entry -- is ``purge_disabled``
(tests/api/test_credit_operational_authorization.py).  This suite drives
:class:`PurgeExecutor` directly, as Group 8 will: every refusal happens
before any tombstone, job, fence or storage call; a hold that wins before
the fence restores every item; the receipt exists only once every item has a
final result, and never claims ``completed`` for an item not confirmed
deleted; crash recovery restores unfenced items and finishes fenced ones.
The SERIALIZABLE barriers and the real stores run in
``tests/integration/test_credit_legal_hold_barriers.py`` and
``tests/integration/test_credit_purge_jobs.py``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from src.config.settings import settings
from src.models.builder import Builder
from src.models.content_retention import (
    CreditHoldEvent,
    CreditHoldGeneration,
    CreditLifecycleEpisode,
    CreditLifecycleTransition,
    CreditPurgeFence,
    CreditPurgeJob,
    CreditPurgeReceipt,
    CreditPurgeResult,
    CreditRetentionTombstone,
)
from src.models.credit import CreditAccount
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset
from src.models.operational_capability import CreditOperationalAuthorizationReceipt
from src.models.tenant import Tenant
from src.models.user import User, UserRole
from src.repositories.database import get_db_session
from src.services import content_retention
from src.services.content_retention import (
    Actor,
    PresentedAuthorization,
    PurgeExecutor,
    RetentionRefused,
)

UTC = timezone.utc
KEY = "credit-authorization-signing-key-for-tests-0123456789"
NOW = datetime(2026, 12, 20, 12, tzinfo=UTC)


@dataclass
class FakeStore:
    purge_outcomes: list[str] = field(default_factory=list)
    calls: list[tuple[str, UUID]] = field(default_factory=list)

    async def tombstone(self, item: CreditRetentionTombstone) -> None:
        self.calls.append(("tombstone", item.source_id))

    async def restore(self, item: CreditRetentionTombstone) -> None:
        self.calls.append(("restore", item.source_id))

    async def lookup(self, item: CreditRetentionTombstone) -> Any:
        return None

    async def purge(self, item: CreditRetentionTombstone) -> str:
        self.calls.append(("purge", item.source_id))
        return self.purge_outcomes.pop(0) if self.purge_outcomes else "deleted"

    def count(self, kind: str) -> int:
        return sum(1 for name, _ in self.calls if name == kind)


@dataclass(frozen=True)
class World:
    builder: UUID
    tenant: UUID
    issuer: Actor
    owner: Actor
    plain_admin: Actor
    media: UUID
    document: UUID


def run(client: Any, coroutine: Any) -> Any:
    return client._submit(coroutine)


async def _seed(deletion_due: bool = True) -> World:
    world = World(
        builder=uuid4(),
        tenant=uuid4(),
        issuer=Actor(uuid4(), "super_admin", True),
        owner=Actor(uuid4(), "super_admin", True),
        plain_admin=Actor(uuid4(), "super_admin", True),
        media=uuid4(),
        document=uuid4(),
    )
    session = get_db_session()
    try:
        async with session.begin():
            session.add(Builder(id=world.builder, name="Purge builder"))
            await session.flush()
            session.add(
                Tenant(
                    id=world.tenant,
                    name="Purge clinic",
                    slug=f"purge-{world.tenant.hex}",
                    api_key_hash="x",
                    builder_id=world.builder,
                    config={},
                )
            )
            await session.flush()
            for actor in (world.issuer, world.owner, world.plain_admin):
                session.add(
                    User(
                        id=actor.user_id,
                        tenant_id=world.tenant,
                        email=f"{actor.user_id.hex[:10]}@example.invalid",
                        password_hash="x",
                        role=UserRole.super_admin,
                    )
                )
            payload = b"\x89PNG\r\n\x1a\npurge-me"
            session.add(
                MediaAsset(
                    id=world.media,
                    tenant_id=world.tenant,
                    display_name="m",
                    original_filename="m.png",
                    category="image",
                    mime_type="image/png",
                    size_bytes=len(payload),
                    sha256=hashlib.sha256(payload).digest(),
                    payload=payload,
                )
            )
            session.add(
                KnowledgeDocument(
                    id=world.document,
                    tenant_id=world.tenant,
                    title="d",
                    content="conteúdo",
                    content_type="text",
                    document_metadata={},
                )
            )
            account = uuid4()
            session.add(CreditAccount(id=account, builder_id=world.builder))
            exhausted = NOW - timedelta(days=40)
            episode = uuid4()
            session.add(
                CreditLifecycleEpisode(
                    id=episode,
                    builder_id=world.builder,
                    account_id=account,
                    opened_effect_id="e",
                    exhausted_at=exhausted,
                    suspension_due_at=exhausted + timedelta(hours=72),
                    deletion_due_at=exhausted + timedelta(days=30),
                )
            )
            if deletion_due:
                for name in ("suspended", "deletion_due"):
                    session.add(
                        CreditLifecycleTransition(
                            id=uuid4(),
                            builder_id=world.builder,
                            episode_id=episode,
                            transition=name,
                            effect_id=f"lifecycle:{episode}:{name}",
                            occurred_at=exhausted,
                            source="worker",
                            detail={},
                        )
                    )
    finally:
        await session.close()
    return world


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "credit_authorization_signing_key", SecretStr(KEY))


@pytest.fixture
def world(client: Any) -> World:
    return run(client, _seed())


async def _grant_owner(world: World) -> None:
    session = get_db_session()
    try:
        async with session.begin():
            await content_retention.grant_assignment(
                session,
                world.issuer,
                role="production_data_lifecycle_owner",
                user_id=world.owner.user_id,
                reason="test",
                now=NOW,
            )
    finally:
        await session.close()


async def _authorize(
    world: World, operation: UUID, generation: int = 0, subject: Actor | None = None
) -> PresentedAuthorization:
    session = get_db_session()
    try:
        async with session.begin():
            issued = await content_retention.issue_authorization(
                session,
                world.issuer,
                purpose="purge",
                builder_id=world.builder,
                scope="builder",
                subject_user_id=(subject or world.owner).user_id,
                resource=str(operation),
                fence_generation=generation,
                now=NOW,
            )
    finally:
        await session.close()
    return PresentedAuthorization(issued.authorization_id, issued.signature)


async def _count(model: Any) -> int:
    session = get_db_session()
    try:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)
    finally:
        await session.close()


async def _hold(world: World, generation: int) -> None:
    """A committed hold request (as a hold event of the stream would leave)."""

    session = get_db_session()
    try:
        async with session.begin():
            existing = await session.get(
                CreditHoldGeneration, (world.builder, "builder")
            )
            if existing is None:
                session.add(
                    CreditHoldGeneration(
                        builder_id=world.builder, scope="builder", generation=generation
                    )
                )
            else:
                existing.generation = generation
            session.add(
                CreditHoldEvent(
                    id=uuid4(),
                    intent_id=uuid4(),
                    builder_id=world.builder,
                    scope="builder",
                    event_type="hold_requested",
                    reason_code="litigation",
                    actor_user_id=uuid4(),
                    occurred_at=NOW,
                    generation=generation,
                    authorization_id=uuid4(),
                    digest="c" * 64,
                )
            )
    finally:
        await session.close()


def _executor(store: FakeStore) -> PurgeExecutor:
    return PurgeExecutor(get_db_session, store, clock=lambda: NOW)


def _untouched(client: Any, store: FakeStore) -> None:
    assert store.calls == []
    for model in (
        CreditRetentionTombstone,
        CreditPurgeJob,
        CreditPurgeFence,
        CreditPurgeResult,
        CreditPurgeReceipt,
        CreditOperationalAuthorizationReceipt,
    ):
        assert run(client, _count(model)) == 0, model.__tablename__


@pytest.mark.parametrize(
    "case,code",
    [
        ("not_super_admin", "super_admin_jwt_required"),
        ("super_admin_alone", "assignment_required"),
        ("expected_environment", "environment_mismatch"),
        ("stale_generation", "authorization_resource_mismatch"),
        ("other_operation", "authorization_resource_mismatch"),
        ("hold_effective", "legal_hold_effective"),
    ],
)
def test_prepare_refuses_before_any_tombstone_or_storage(
    client: Any, world: World, case: str, code: str
) -> None:
    store = FakeStore()
    operation = uuid4()
    actor = world.owner
    expected_environment = None
    if case != "super_admin_alone":
        run(client, _grant_owner(world))
    generation = 0
    if case == "hold_effective":
        # A committed hold moved the generation to 1; the authorization names
        # that very generation, so only the effective hold refuses.
        run(client, _hold(world, 1))
        generation = 1
    presented = run(
        client,
        _authorize(
            world, operation if case != "other_operation" else uuid4(), generation
        ),
    )
    if case == "not_super_admin":
        actor = Actor(world.owner.user_id, "clinic_admin", True)
    elif case == "expected_environment":
        expected_environment = "production"
    elif case == "stale_generation":
        run(client, _hold(world, 1))  # a hold moved the generation (and is effective)
    with pytest.raises(RetentionRefused) as refused:
        run(
            client,
            _executor(store).prepare(
                actor,
                operation_id=operation,
                builder_id=world.builder,
                scope="builder",
                presented=presented,
                expected_environment=expected_environment,
            ),
        )
    assert refused.value.code == code
    _untouched(client, store)


def test_prepare_refuses_a_builder_not_yet_deletion_due(client: Any) -> None:
    world = run(client, _seed(deletion_due=False))
    run(client, _grant_owner(world))
    operation = uuid4()
    presented = run(client, _authorize(world, operation))
    store = FakeStore()
    with pytest.raises(RetentionRefused, match="not_deletion_due"):
        run(
            client,
            _executor(store).prepare(
                world.owner,
                operation_id=operation,
                builder_id=world.builder,
                scope="builder",
                presented=presented,
            ),
        )
    _untouched(client, store)


def _prepared(client: Any, world: World, store: FakeStore) -> tuple[UUID, list[UUID]]:
    run(client, _grant_owner(world))
    operation = uuid4()
    presented = run(client, _authorize(world, operation))
    tokens = run(
        client,
        _executor(store).prepare(
            world.owner,
            operation_id=operation,
            builder_id=world.builder,
            scope="builder",
            presented=presented,
        ),
    )
    assert len(tokens) == 2
    return operation, tokens


def test_full_purge_is_completed_once_and_replay_deletes_nothing_twice(
    client: Any, world: World
) -> None:
    store = FakeStore()
    operation, _tokens = _prepared(client, world, store)
    executor = _executor(store)
    run(client, executor.apply_tombstones(operation))
    assert store.count("tombstone") == 2 and store.count("purge") == 0
    assert run(client, executor.fence(operation)) is not None
    report = run(client, executor.purge(operation))
    assert report.status == "completed"
    assert set(report.outcomes.values()) == {"deleted"}
    replay = run(client, executor.purge(operation))
    assert replay.status == "completed"
    assert store.count("purge") == 2
    assert run(client, _count(CreditPurgeReceipt)) == 1


def test_hold_committed_before_the_fence_wins_and_restores(
    client: Any, world: World
) -> None:
    store = FakeStore()
    operation, _tokens = _prepared(client, world, store)
    executor = _executor(store)
    run(client, executor.apply_tombstones(operation))
    run(client, _hold(world, 1))
    assert run(client, executor.fence(operation)) is None
    assert store.count("restore") == 2 and store.count("purge") == 0
    assert run(client, _count(CreditPurgeFence)) == 0
    report = run(client, executor.finish(operation))
    assert report.status == "blocked"
    assert set(report.outcomes.values()) == {"held"}
    with pytest.raises(RetentionRefused, match="fence_required"):
        run(client, executor.purge(operation))


def test_one_generation_never_gets_a_second_fence(client: Any, world: World) -> None:
    store = FakeStore()
    first, _ = _prepared(client, world, store)
    executor = _executor(store)
    assert run(client, executor.fence(first)) is not None
    second = uuid4()
    presented = run(client, _authorize(world, second))
    run(
        client,
        executor.prepare(
            world.owner,
            operation_id=second,
            builder_id=world.builder,
            scope="builder",
            presented=presented,
        ),
    )
    assert run(client, executor.fence(second)) is None
    assert run(client, _count(CreditPurgeFence)) == 1


def test_a_failed_purge_never_produces_a_receipt_until_confirmed(
    client: Any, world: World
) -> None:
    store = FakeStore(purge_outcomes=["deleted", "failed"])
    operation, _ = _prepared(client, world, store)
    executor = _executor(store)
    run(client, executor.apply_tombstones(operation))
    run(client, executor.fence(operation))
    report = run(client, executor.purge(operation))
    assert report.status == "incomplete"
    assert run(client, _count(CreditPurgeReceipt)) == 0
    # Crash recovery retries the fenced item only (no second delete).
    assert run(client, executor.reconcile()) == 1
    assert store.count("purge") == 3
    assert run(client, executor.finish(operation)).status == "completed"


def test_crash_before_the_fence_restores_by_default(client: Any, world: World) -> None:
    store = FakeStore()
    operation, _ = _prepared(client, world, store)
    executor = _executor(store)
    run(client, executor.apply_tombstones(operation))
    # The process died here: no fence.  Recovery restores, never deletes.
    assert run(client, executor.reconcile()) == 2
    assert store.count("restore") == 2 and store.count("purge") == 0
    report = run(client, executor.finish(operation))
    assert report.status == "blocked"
    assert set(report.outcomes.values()) == {"blocked"}
    # Idempotent: a second recovery finds nothing left.
    assert run(client, executor.reconcile()) == 0


def test_crash_after_the_last_result_before_the_receipt_is_recovered(
    client: Any, world: World
) -> None:
    """Every item purged and recorded ``deleted``, then the process died
    before :meth:`PurgeExecutor.finish`: no token is unfinished, yet the
    recovery writes the receipt (never a second delete)."""

    store = FakeStore()
    operation, _ = _prepared(client, world, store)
    executor = _executor(store)
    run(client, executor.apply_tombstones(operation))
    assert run(client, executor.fence(operation)) is not None

    async def purge_without_finish() -> None:
        items = await _tombstones(operation)
        for item in items:
            await executor._purge_one(item)

    run(client, purge_without_finish())
    assert store.count("purge") == 2
    assert run(client, _count(CreditPurgeReceipt)) == 0
    assert run(client, executor.reconcile()) == 0  # no token left to work
    assert run(client, _count(CreditPurgeReceipt)) == 1
    assert run(client, executor.finish(operation)).status == "completed"
    assert store.count("purge") == 2


async def _tombstones(operation: UUID) -> list[CreditRetentionTombstone]:
    session = get_db_session()
    try:
        return list(
            (
                await session.execute(
                    select(CreditRetentionTombstone).where(
                        CreditRetentionTombstone.operation_id == operation
                    )
                )
            ).scalars()
        )
    finally:
        await session.close()


def test_crash_before_the_fence_with_a_hold_records_held(
    client: Any, world: World
) -> None:
    store = FakeStore()
    operation, _ = _prepared(client, world, store)
    executor = _executor(store)
    run(client, _hold(world, 1))
    run(client, executor.reconcile())
    report = run(client, executor.finish(operation))
    assert set(report.outcomes.values()) == {"held"}
    assert report.status == "blocked"


def test_expired_authorization_fails_before_any_tombstone(
    client: Any, world: World
) -> None:
    run(client, _grant_owner(world))
    operation = uuid4()

    async def issue_long_ago() -> PresentedAuthorization:
        session = get_db_session()
        try:
            async with session.begin():
                issued = await content_retention.issue_authorization(
                    session,
                    world.issuer,
                    purpose="purge",
                    builder_id=world.builder,
                    scope="builder",
                    subject_user_id=world.owner.user_id,
                    resource=str(operation),
                    fence_generation=0,
                    ttl=timedelta(hours=1),
                    now=NOW - timedelta(hours=2),
                )
        finally:
            await session.close()
        return PresentedAuthorization(issued.authorization_id, issued.signature)

    presented = run(client, issue_long_ago())
    store = FakeStore()
    with pytest.raises(RetentionRefused, match="authorization_expired"):
        run(
            client,
            _executor(store).prepare(
                world.owner,
                operation_id=operation,
                builder_id=world.builder,
                scope="builder",
                presented=presented,
            ),
        )
    _untouched(client, store)
