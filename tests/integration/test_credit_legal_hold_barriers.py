"""Legal hold versus purge fence under controlled commits (Group 5).

Real PostgreSQL, SERIALIZABLE transactions, the real hold service and the
real fence of :class:`PurgeExecutor` (the storage side is a recording fake:
the barrier is a database property).  Barriers pause one transaction while
it holds the ``credit_hold_generations`` row and release it only once the
other is proven blocked on that row:

* hold-before-fence: a hold committed before the CAS fence always wins -- no
  fence row, every tombstoned item restored and recorded ``held``;
* fence-before-hold: a hold requested after the fence commits observes the
  scope ``closed`` (a first attempt may instead abort with a serialization
  conflict, which writes nothing; its retry observes ``closed``); nothing is
  ever restored retroactively and no hold event exists.

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.config.settings import settings
from src.models.builder import Builder
from src.models.content_retention import (
    CreditHoldEvent,
    CreditLifecycleEpisode,
    CreditLifecycleTransition,
    CreditPurgeFence,
    CreditPurgeResult,
    CreditRetentionTombstone,
)
from src.models.credit import CreditAccount
from src.models.media_asset import MediaAsset
from src.models.operational_capability import CreditOperationalAuthorizationReceipt
from src.models.tenant import Tenant
from src.models.user import User, UserRole
from src.repositories.content_retention import ContentRetentionRepository
from src.services import content_retention
from src.services.content_retention import (
    Actor,
    PresentedAuthorization,
    PurgeExecutor,
    RetentionRefused,
)
from tests.integration.test_asaas_billing_postgres import _validated_admin_url
from tests.integration.test_migration_036_postgres import _Cluster, _migrate

pytestmark = pytest.mark.integration

UTC = timezone.utc
KEY = "credit-authorization-signing-key-for-barriers-0123456789"
_BLOCKED = 0.5  # seconds a contender must still be waiting


@dataclass(frozen=True)
class _Harness:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


@pytest.fixture(scope="module")
async def harness() -> AsyncIterator[_Harness]:
    raw = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw)
    admin = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    cluster = _Cluster(admin_url=admin_url, admin=admin, created=[])
    engine: AsyncEngine | None = None
    try:
        name = await cluster.fresh()
        await _migrate(cluster.url(name), "upgrade", "head")
        engine = create_async_engine(cluster.url(name), poolclass=NullPool)
        yield _Harness(
            engine,
            async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
        )
    finally:
        if engine is not None:
            await engine.dispose()
        for created in list(reversed(cluster.created)):
            await cluster.drop(created)
        await admin.dispose()


@pytest.fixture(autouse=True)
def _key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "credit_authorization_signing_key", SecretStr(KEY))


@dataclass
class FakeStore:
    calls: list[tuple[str, UUID]] = field(default_factory=list)

    async def tombstone(self, item: Any) -> None:
        self.calls.append(("tombstone", item.source_id))

    async def restore(self, item: Any) -> None:
        self.calls.append(("restore", item.source_id))

    async def lookup(self, item: Any) -> Any:
        return None

    async def purge(self, item: Any) -> str:
        self.calls.append(("purge", item.source_id))
        return "deleted"

    def count(self, kind: str) -> int:
        return sum(1 for name, _ in self.calls if name == kind)


@dataclass(frozen=True)
class _World:
    builder: UUID
    tenant: UUID
    issuer: Actor
    owner: Actor
    officer: Actor


async def _world(harness: _Harness) -> _World:
    world = _World(
        uuid4(),
        uuid4(),
        Actor(uuid4(), "super_admin", True),
        Actor(uuid4(), "super_admin", True),
        Actor(uuid4(), "clinic_admin", True),
    )
    now = datetime.now(UTC)
    async with harness.sessions() as session, session.begin():
        session.add(Builder(id=world.builder, name="Barrier builder"))
        await session.flush()
        session.add(
            Tenant(
                id=world.tenant,
                name="Barrier clinic",
                slug=f"barrier-{world.tenant.hex}",
                api_key_hash=f"hash-{world.tenant.hex}",
                plan_type="clinic",
                builder_id=world.builder,
                config={},
            )
        )
        await session.flush()
        for actor in (world.issuer, world.owner, world.officer):
            session.add(
                User(
                    id=actor.user_id,
                    tenant_id=world.tenant,
                    email=f"{actor.user_id.hex[:12]}@example.invalid",
                    password_hash="x",
                    role=(
                        UserRole.super_admin
                        if actor.role == "super_admin"
                        else UserRole.clinic_admin
                    ),
                )
            )
        payload = b"\x89PNG\r\n\x1a\nbarrier"
        session.add(
            MediaAsset(
                id=uuid4(),
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
        account = uuid4()
        session.add(CreditAccount(id=account, builder_id=world.builder))
        await session.flush()
        exhausted = now - timedelta(days=40)
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
        await session.flush()
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
    async with harness.sessions() as session, session.begin():
        for role, actor in (
            ("production_data_lifecycle_owner", world.owner),
            ("legal_hold_officer", world.officer),
        ):
            await content_retention.grant_assignment(
                session, world.issuer, role=role, user_id=actor.user_id, reason="t"
            )
    return world


async def _authorize(
    harness: _Harness,
    world: _World,
    purpose: Any,
    subject: Actor,
    *,
    resource: str | None = None,
    fence_generation: int | None = None,
    scope: str = "builder",
) -> PresentedAuthorization:
    async with harness.sessions() as session, session.begin():
        issued = await content_retention.issue_authorization(
            session,
            world.issuer,
            purpose=purpose,
            builder_id=world.builder,
            scope=scope,
            subject_user_id=subject.user_id,
            resource=resource,
            fence_generation=fence_generation,
        )
    return PresentedAuthorization(issued.authorization_id, issued.signature)


async def _prepared(
    harness: _Harness, world: _World, store: FakeStore, scope: str = "builder"
) -> tuple[PurgeExecutor, UUID]:
    operation = uuid4()
    presented = await _authorize(
        harness,
        world,
        "purge",
        world.owner,
        resource=str(operation),
        fence_generation=0,
        scope=scope,
    )
    executor = PurgeExecutor(harness.sessions, store)
    tokens = await executor.prepare(
        world.owner,
        operation_id=operation,
        builder_id=world.builder,
        scope=scope,
        presented=presented,
    )
    assert len(tokens) == 1
    await executor.apply_tombstones(operation)
    return executor, operation


async def _count(harness: _Harness, model: Any, builder: UUID | None = None) -> int:
    async with harness.sessions() as session:
        statement = select(func.count()).select_from(model)
        if builder is not None:
            statement = statement.where(model.builder_id == builder)
        return int(await session.scalar(statement) or 0)


async def _hold_request(
    harness: _Harness,
    world: _World,
    presented: PresentedAuthorization,
    scope: str = "builder",
) -> Any:
    async with harness.sessions() as session:
        return await content_retention.record_hold_event(
            session,
            world.officer,
            event_type="hold_requested",
            builder_id=world.builder,
            scope=scope,
            presented=presented,
            reason_code="litigation",
        )


async def test_hold_committed_before_the_fence_always_wins(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(harness)
    store = FakeStore()
    executor, operation = await _prepared(harness, world, store)
    presented = await _authorize(harness, world, "hold_request", world.officer)

    locked, release = asyncio.Event(), asyncio.Event()
    original = ContentRetentionRepository.bump_hold_generation

    async def paused_bump(self: Any, builder_id: UUID, scope: str, current: int) -> int:
        value = await original(self, builder_id, scope, current)
        locked.set()
        await release.wait()  # the hold holds the generation row, uncommitted
        return value

    monkeypatch.setattr(ContentRetentionRepository, "bump_hold_generation", paused_bump)
    hold = asyncio.create_task(_hold_request(harness, world, presented))
    await asyncio.wait_for(locked.wait(), 10)
    fence = asyncio.create_task(executor.fence(operation))
    await asyncio.sleep(_BLOCKED)
    assert not fence.done(), "the fence must wait on the hold's generation row"
    release.set()
    result = await asyncio.wait_for(hold, 10)
    assert result.generation == 1
    assert await asyncio.wait_for(fence, 10) is None
    assert await _count(harness, CreditPurgeFence, world.builder) == 0
    assert store.count("restore") == 1 and store.count("purge") == 0
    report = await executor.finish(operation)
    assert report.status == "blocked"
    assert set(report.outcomes.values()) == {"held"}


async def test_hold_requested_after_the_fence_observes_closed_and_restores_nothing(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = await _world(harness)
    store = FakeStore()
    executor, operation = await _prepared(harness, world, store)
    presented = await _authorize(harness, world, "hold_request", world.officer)

    inserted, release = asyncio.Event(), asyncio.Event()
    original = ContentRetentionRepository.insert_fence

    async def paused_fence(self: Any, **kwargs: Any) -> UUID | None:
        value = await original(self, **kwargs)
        inserted.set()
        await release.wait()  # the fence holds the generation row, uncommitted
        return value

    monkeypatch.setattr(ContentRetentionRepository, "insert_fence", paused_fence)
    fence = asyncio.create_task(executor.fence(operation))
    await asyncio.wait_for(inserted.wait(), 10)
    hold = asyncio.create_task(_hold_request(harness, world, presented))
    await asyncio.sleep(_BLOCKED)
    assert not hold.done(), "the hold must wait on the fence's generation row"
    release.set()
    assert await asyncio.wait_for(fence, 10) is not None
    with pytest.raises(RetentionRefused) as first:
        await asyncio.wait_for(hold, 10)
    assert first.value.code in ("scope_closed", "hold_conflict")
    # Whatever the first attempt saw, a retry observes the closed scope.
    with pytest.raises(RetentionRefused, match="scope_closed"):
        await _hold_request(harness, world, presented)
    assert await _count(harness, CreditHoldEvent, world.builder) == 0
    async with harness.sessions() as session:
        consumed = await session.scalar(
            select(func.count())
            .select_from(CreditOperationalAuthorizationReceipt)
            .where(
                CreditOperationalAuthorizationReceipt.authorization_id
                == presented.authorization_id
            )
        )
    assert consumed == 0
    # The purge goes on; nothing is ever restored retroactively.
    report = await executor.purge(operation)
    assert report.status == "completed"
    assert store.count("restore") == 0 and store.count("purge") == 1


async def test_concurrent_fences_of_one_generation_create_one(
    harness: _Harness,
) -> None:
    world = await _world(harness)
    store = FakeStore()
    executor, first = await _prepared(harness, world, store)
    _other, second = await _prepared(harness, world, store)
    fences = await asyncio.gather(executor.fence(first), executor.fence(second))
    assert len([value for value in fences if value is not None]) == 1
    assert await _count(harness, CreditPurgeFence, world.builder) == 1
    async with harness.engine.connect() as connection:
        held = await connection.scalar(
            text(
                "SELECT count(*) FROM credit_purge_results r JOIN "
                "credit_retention_tombstones t ON t.token = r.token "
                "WHERE t.builder_id = :b AND r.outcome = 'held'"
            ),
            {"b": world.builder},
        )
    assert held == 1  # the loser restored its item
    assert await _count(harness, CreditRetentionTombstone, world.builder) == 2
    assert await _count(harness, CreditPurgeResult) >= 1


# ---------------------------------------------------------------------------
# Overlapping scopes: ``builder`` contains every ``tenant:X`` of the builder.
# A hold on one and a fence on the other serialize on the builder's
# generation row (every hold and fence locks it first) and on the tenant
# rows, in the one fixed order; the purge folds the holds of both.
# ---------------------------------------------------------------------------


def _tenant_scope(world: _World) -> str:
    return f"tenant:{world.tenant}"


@pytest.mark.parametrize(
    ("hold_on", "purge_on"),
    [("tenant", "builder"), ("builder", "tenant")],
    ids=["tenant_hold_vs_builder_fence", "builder_hold_vs_tenant_fence"],
)
async def test_cross_scope_hold_committed_before_the_fence_always_wins(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, hold_on: str, purge_on: str
) -> None:
    world = await _world(harness)
    hold_scope = _tenant_scope(world) if hold_on == "tenant" else "builder"
    purge_scope = _tenant_scope(world) if purge_on == "tenant" else "builder"
    store = FakeStore()
    executor, operation = await _prepared(harness, world, store, purge_scope)
    presented = await _authorize(
        harness, world, "hold_request", world.officer, scope=hold_scope
    )

    locked, release = asyncio.Event(), asyncio.Event()
    original = ContentRetentionRepository.bump_hold_generation

    async def paused_bump(self: Any, builder_id: UUID, scope: str, current: int) -> int:
        value = await original(self, builder_id, scope, current)
        locked.set()
        await release.wait()  # the hold holds its generation rows, uncommitted
        return value

    monkeypatch.setattr(ContentRetentionRepository, "bump_hold_generation", paused_bump)
    hold = asyncio.create_task(_hold_request(harness, world, presented, hold_scope))
    await asyncio.wait_for(locked.wait(), 10)
    fence = asyncio.create_task(executor.fence(operation))
    await asyncio.sleep(_BLOCKED)
    assert not fence.done(), "the fence must wait on the hold's builder row"
    release.set()
    result = await asyncio.wait_for(hold, 10)
    assert result.generation == 1
    assert await asyncio.wait_for(fence, 10) is None
    assert await _count(harness, CreditPurgeFence, world.builder) == 0
    assert store.count("restore") == 1 and store.count("purge") == 0
    report = await executor.finish(operation)
    assert report.status == "blocked"
    assert set(report.outcomes.values()) == {"held"}


@pytest.mark.parametrize(
    ("fence_on", "hold_on"),
    [("builder", "tenant"), ("tenant", "builder")],
    ids=["builder_fence_vs_tenant_hold", "tenant_fence_vs_builder_hold"],
)
async def test_cross_scope_hold_requested_after_the_fence_observes_closed(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, fence_on: str, hold_on: str
) -> None:
    world = await _world(harness)
    fence_scope = _tenant_scope(world) if fence_on == "tenant" else "builder"
    hold_scope = _tenant_scope(world) if hold_on == "tenant" else "builder"
    store = FakeStore()
    executor, operation = await _prepared(harness, world, store, fence_scope)
    presented = await _authorize(
        harness, world, "hold_request", world.officer, scope=hold_scope
    )

    inserted, release = asyncio.Event(), asyncio.Event()
    original = ContentRetentionRepository.insert_fence

    async def paused_fence(self: Any, **kwargs: Any) -> UUID | None:
        value = await original(self, **kwargs)
        inserted.set()
        await release.wait()  # the fence holds the generation rows, uncommitted
        return value

    monkeypatch.setattr(ContentRetentionRepository, "insert_fence", paused_fence)
    fence = asyncio.create_task(executor.fence(operation))
    await asyncio.wait_for(inserted.wait(), 10)
    hold = asyncio.create_task(_hold_request(harness, world, presented, hold_scope))
    await asyncio.sleep(_BLOCKED)
    assert not hold.done(), "the hold must wait on the fence's builder row"
    release.set()
    assert await asyncio.wait_for(fence, 10) is not None
    with pytest.raises(RetentionRefused) as first:
        await asyncio.wait_for(hold, 10)
    assert first.value.code in ("scope_closed", "hold_conflict")
    # Whatever the first attempt saw, a retry observes the closed scope.
    with pytest.raises(RetentionRefused, match="scope_closed"):
        await _hold_request(harness, world, presented, hold_scope)
    assert await _count(harness, CreditHoldEvent, world.builder) == 0
    # The purge goes on; nothing is ever restored retroactively.
    report = await executor.purge(operation)
    assert report.status == "completed"
    assert store.count("restore") == 0 and store.count("purge") == 1


async def test_a_tenant_hold_stops_a_builder_wide_purge_at_prepare(
    harness: _Harness,
) -> None:
    """The reviewer's reproduction: a hold on ``tenant:X`` alone, then a
    builder-wide purge authorized for the very coverage generation that
    hold produced -- refused ``legal_hold_effective`` before any tombstone."""

    world = await _world(harness)
    presented = await _authorize(
        harness, world, "hold_request", world.officer, scope=_tenant_scope(world)
    )
    assert (
        await _hold_request(harness, world, presented, _tenant_scope(world))
    ).generation == 1
    operation = uuid4()
    purge = await _authorize(
        harness,
        world,
        "purge",
        world.owner,
        resource=str(operation),
        fence_generation=1,
    )
    store = FakeStore()
    with pytest.raises(RetentionRefused, match="legal_hold_effective"):
        await PurgeExecutor(harness.sessions, store).prepare(
            world.owner,
            operation_id=operation,
            builder_id=world.builder,
            scope="builder",
            presented=purge,
        )
    assert await _count(harness, CreditRetentionTombstone, world.builder) == 0
    assert store.calls == []


async def test_hold_and_fence_transactions_run_serializable(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every hold event and every fence reads ``transaction_isolation`` =
    ``serializable`` while it holds its generation rows."""

    world = await _world(harness)
    store = FakeStore()
    executor, operation = await _prepared(harness, world, store)
    seen: list[str] = []
    original = ContentRetentionRepository.lock_hold_generations

    async def observed(self: Any, builder_id: UUID, scopes: Any) -> Any:
        seen.append(str(await self.session.scalar(text("SHOW transaction_isolation"))))
        return await original(self, builder_id, scopes)

    monkeypatch.setattr(ContentRetentionRepository, "lock_hold_generations", observed)
    presented = await _authorize(
        harness, world, "hold_request", world.officer, scope=_tenant_scope(world)
    )
    await _hold_request(harness, world, presented, _tenant_scope(world))
    assert await executor.fence(operation) is None  # the hold moved the CAS
    assert seen == ["serializable", "serializable"]
