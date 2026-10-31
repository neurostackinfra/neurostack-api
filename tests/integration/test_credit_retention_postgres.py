"""Credit lifecycle and retention rules on real PostgreSQL (Group 5).

PostgreSQL only -- no S3, no Qdrant.  A fresh child database at ``head``
(041) of the smoke cluster.  Pinned here:

* every append-only table of 041 (transitions, threshold alerts, roles,
  authorizations and their receipts, hold events, purge fences, tombstones,
  jobs, results and receipts) refuses UPDATE and DELETE with SQLSTATE 23000
  -- and the same statement succeeds with triggers disabled, so the refusal
  is the trigger's, not an accident;
* the episode guard: the binding grace and the suspension deadline only
  shorten, ``exhausted_at``/``deletion_due_at`` are immutable,
  ``observed_inactive_at`` is first-write-wins, a reactivated episode is
  frozen, nothing is deleted; the hold generation grows by exactly one;
* at most one open episode per builder (partial unique index, 23505), and
  concurrent writers under the account lock never open a second one;
* concurrency never creates a second threshold alert of one arm, a second
  ``suspended`` transition (worker and request racing at the deadline), nor
  a second purge fence of one generation.

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.models.builder import Builder
from src.models.content_retention import (
    CreditLifecycleEpisode,
    CreditLifecycleTransition,
    CreditThresholdAlert,
)
from src.models.tenant import Tenant
from src.repositories.content_retention import ContentRetentionRepository
from src.services import credit_inventory, credit_lifecycle
from src.services.credit_ledger import (
    CreditLedger,
    GrantSpec,
    topup_grant_effect_id,
    topup_lot_expires_at,
)
from src.services.credit_lifecycle import DEBT_GRACE
from tests.integration.test_asaas_billing_postgres import (
    _sqlstate,
    _validated_admin_url,
)
from tests.integration.test_migration_036_postgres import _Cluster, _migrate

pytestmark = pytest.mark.integration

UTC = timezone.utc
ENFORCED = datetime(2026, 11, 2, 12, tzinfo=UTC)
_HEX = "b" * 64


@dataclass(frozen=True)
class _Harness:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]

    def session(self) -> AsyncSession:
        return self.sessions()


@pytest.fixture(scope="module")
async def harness() -> AsyncIterator[_Harness]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    cluster = _Cluster(admin_url=admin_url, admin=admin, created=[])
    engine: AsyncEngine | None = None
    try:
        name = await cluster.fresh()
        await _migrate(cluster.url(name), "upgrade", "head")
        engine = create_async_engine(cluster.url(name), poolclass=NullPool)
        async with engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "041"
            )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO credit_enforcement_state "
                    "(id, credit_enforcement_at, set_by, set_at) "
                    "VALUES (1, :at, 'test', :at)"
                ),
                {"at": ENFORCED},
            )
        yield _Harness(
            engine=engine,
            sessions=async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
            ),
        )
    finally:
        if engine is not None:
            await engine.dispose()
        for created in list(reversed(cluster.created)):
            await cluster.drop(created)
        await admin.dispose()


@dataclass
class _Clock:
    now: datetime


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Clock]:
    current = _Clock(ENFORCED + timedelta(hours=1))

    async def reference_now(_self: CreditLedger) -> datetime:
        return current.now

    monkeypatch.setattr(CreditLedger, "reference_now", reference_now)
    monkeypatch.setattr(credit_inventory, "_now", lambda: current.now)
    yield current


@pytest.fixture(autouse=True)
def _switch_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """The worker's tick claims nothing unless the switch is effective."""

    from src.services.credit_switch import CreditSwitch

    monkeypatch.setattr(CreditSwitch, "effective", lambda _self: True)


@dataclass(frozen=True)
class _World:
    builder: UUID
    tenant: UUID


@pytest.fixture
async def world(harness: _Harness) -> _World:
    world = _World(uuid4(), uuid4())
    async with harness.session() as session, session.begin():
        session.add(Builder(id=world.builder, name="Retention builder"))
        await session.flush()
        session.add(
            Tenant(
                id=world.tenant,
                name=f"Retention {world.tenant.hex[:6]}",
                slug=f"retention-{world.tenant.hex}",
                api_key_hash=f"hash-{world.tenant.hex}",
                plan_type="clinic",
                builder_id=world.builder,
                config={},
            )
        )
    return world


async def _sql(
    harness: _Harness, sql: str, *, replica: bool = False, **params: Any
) -> None:
    async with harness.engine.begin() as connection:
        if replica:
            await connection.execute(
                text("SET LOCAL session_replication_role = replica")
            )
        await connection.execute(text(sql), params)


async def _refused(harness: _Harness, sql: str, match: str, **params: Any) -> str:
    """Run ``sql``; it must fail, with ``match`` in the error.  Returns the
    SQLSTATE (pinned by the caller)."""

    with pytest.raises(DBAPIError, match=match) as raised:
        await _sql(harness, sql, **params)
    state = _sqlstate(raised.value)
    assert state is not None
    return state


# ---------------------------------------------------------------------------
# Append-only tables.
# ---------------------------------------------------------------------------

_APPEND_ONLY_ROWS: dict[str, tuple[str, str]] = {
    "credit_lifecycle_transitions": (
        "INSERT INTO credit_lifecycle_transitions (id, builder_id, episode_id, "
        "transition, effect_id, occurred_at, source, detail) VALUES (:id, "
        "gen_random_uuid(), gen_random_uuid(), 'opened', 'e', now(), 'worker', '{}')",
        "source = 'effect'",
    ),
    "credit_threshold_alerts": (
        "INSERT INTO credit_threshold_alerts (id, builder_id, arm_key, threshold, "
        "capacity_mc, available_mc, effect_id, occurred_at) VALUES (:id, "
        "gen_random_uuid(), 'a', 50, 0, 0, 'e', now())",
        "available_mc = 1",
    ),
    "credit_operational_role_assignments": (
        "INSERT INTO credit_operational_role_assignments (id, role, user_id, "
        "environment, action, actor_user_id, reason) VALUES (:id, "
        "'legal_hold_officer', gen_random_uuid(), 'test', 'granted', "
        "gen_random_uuid(), 'r')",
        "reason = 'changed'",
    ),
    "credit_operational_authorizations": (
        "INSERT INTO credit_operational_authorizations (id, purpose, environment, "
        "builder_id, scope, subject_user_id, issued_by, issued_at, expires_at, "
        "key_fingerprint, signature) VALUES (:id, 'hold_request', 'test', "
        "gen_random_uuid(), 'builder', gen_random_uuid(), gen_random_uuid(), now(), "
        f"now() + interval '1 hour', '{_HEX}', '{_HEX}')",
        "expires_at = now() + interval '1 day'",
    ),
    "credit_operational_authorization_receipts": (
        "INSERT INTO credit_operational_authorization_receipts (authorization_id, "
        "consumed_by, consumed_for, result_ref, consumed_at) VALUES (:id, "
        "gen_random_uuid(), 'hold_requested', 'r', now())",
        "result_ref = 'changed'",
    ),
    "credit_hold_events": (
        "INSERT INTO credit_hold_events (id, intent_id, builder_id, scope, "
        "event_type, reason_code, actor_user_id, occurred_at, generation, "
        "authorization_id, digest) VALUES (:id, gen_random_uuid(), "
        "gen_random_uuid(), 'builder', 'hold_requested', 'r', gen_random_uuid(), "
        f"now(), 1, gen_random_uuid(), '{_HEX}')",
        "reason_code = 'changed'",
    ),
    "credit_purge_fences": (
        "INSERT INTO credit_purge_fences (id, builder_id, scope, generation, "
        "operation_id) VALUES (:id, gen_random_uuid(), 'builder', 0, "
        "gen_random_uuid())",
        "generation = 1",
    ),
    "credit_retention_tombstones": (
        "INSERT INTO credit_retention_tombstones (id, token, operation_id, "
        "builder_id, tenant_id, scope, source_kind, source_id, content_sha256, "
        "size_bytes, qdrant_point_ids, observed_generation) VALUES (:id, "
        "gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), gen_random_uuid(), "
        f"'builder', 'media', gen_random_uuid(), '{_HEX}', 1, '[]', 0)",
        "size_bytes = 2",
    ),
    "credit_purge_jobs": (
        "INSERT INTO credit_purge_jobs (id, token, operation_id, state) VALUES "
        "(:id, gen_random_uuid(), gen_random_uuid(), 'prepared')",
        "state = 'storage_applied'",
    ),
    "credit_purge_results": (
        "INSERT INTO credit_purge_results (id, token, operation_id, attempt, "
        "outcome, detail) VALUES (:id, gen_random_uuid(), gen_random_uuid(), 1, "
        "'failed', 'd')",
        "outcome = 'deleted'",
    ),
    "credit_purge_receipts": (
        "INSERT INTO credit_purge_receipts (operation_id, builder_id, scope, "
        "status, deleted_count, held_count, failed_count, blocked_count, digest) "
        f"VALUES (:id, gen_random_uuid(), 'builder', 'blocked', 0, 0, 0, 0, '{_HEX}')",
        "deleted_count = 1",
    ),
}
_KEY = {
    "credit_operational_authorization_receipts": "authorization_id",
    "credit_purge_receipts": "operation_id",
}


@pytest.mark.parametrize("table", sorted(_APPEND_ONLY_ROWS))
async def test_group_tables_are_append_only_by_trigger(
    harness: _Harness, table: str
) -> None:
    insert, change = _APPEND_ONLY_ROWS[table]
    key = _KEY.get(table, "id")
    row = uuid4()
    # Planted with triggers off: the FKs of an isolated row are not the point.
    await _sql(harness, insert, replica=True, id=row)
    update = f"UPDATE {table} SET {change} WHERE {key} = :id"
    delete = f"DELETE FROM {table} WHERE {key} = :id"
    assert await _refused(harness, update, "is append-only", id=row) == "23000"
    assert await _refused(harness, delete, "is append-only", id=row) == "23000"
    # Not vacuous: the same statements succeed with the trigger disabled.
    await _sql(harness, update, replica=True, id=row)
    await _sql(harness, delete, replica=True, id=row)


# ---------------------------------------------------------------------------
# Guard triggers.
# ---------------------------------------------------------------------------


async def _plant_episode(harness: _Harness, *, grace: bool = True) -> UUID:
    episode = uuid4()
    exhausted = ENFORCED + timedelta(days=1)
    await _sql(
        harness,
        "INSERT INTO credit_lifecycle_episodes (id, builder_id, account_id, "
        "opened_effect_id, exhausted_at, cycle_end_at, grace_ends_at, "
        "suspension_due_at, deletion_due_at) VALUES (:id, gen_random_uuid(), "
        "gen_random_uuid(), 'e', :exhausted, :cycle_end, :grace, :due, :deletion)",
        replica=True,
        id=episode,
        exhausted=exhausted,
        cycle_end=exhausted - timedelta(hours=1) if grace else None,
        grace=exhausted + timedelta(days=4) if grace else None,
        due=exhausted + timedelta(days=4) + DEBT_GRACE,
        deletion=exhausted + timedelta(days=30),
    )
    return episode


async def test_episode_grace_and_deadline_only_shorten(harness: _Harness) -> None:
    episode = await _plant_episode(harness)
    extend = (
        "UPDATE credit_lifecycle_episodes SET grace_ends_at = grace_ends_at "
        "+ interval '1 day' WHERE id = :id"
    )
    assert await _refused(harness, extend, "can only shorten", id=episode) == "23000"
    later = (
        "UPDATE credit_lifecycle_episodes SET suspension_due_at = "
        "suspension_due_at + interval '1 second' WHERE id = :id"
    )
    assert await _refused(harness, later, "never extends", id=episode) == "23000"
    for column in ("exhausted_at", "deletion_due_at"):
        moved = (
            f"UPDATE credit_lifecycle_episodes SET {column} = {column} "
            "- interval '1 hour' WHERE id = :id"
        )
        assert await _refused(harness, moved, "immutable", id=episode) == "23000"
    # Shortening both is allowed.
    await _sql(
        harness,
        "UPDATE credit_lifecycle_episodes SET grace_ends_at = grace_ends_at - "
        "interval '2 days', suspension_due_at = suspension_due_at - interval "
        "'2 days' WHERE id = :id",
        id=episode,
    )
    # Not vacuous: with the trigger off the extension goes through.
    await _sql(harness, extend, replica=True, id=episode)


async def test_episode_first_writes_win_and_a_closed_episode_is_frozen(
    harness: _Harness,
) -> None:
    episode = await _plant_episode(harness)
    first = ENFORCED + timedelta(days=2)
    await _sql(
        harness,
        "UPDATE credit_lifecycle_episodes SET observed_inactive_at = :at WHERE id = :id",
        id=episode,
        at=first,
    )
    second = (
        "UPDATE credit_lifecycle_episodes SET observed_inactive_at = :at WHERE id = :id"
    )
    assert (
        await _refused(
            harness,
            second,
            "first-write-wins",
            id=episode,
            at=first + timedelta(hours=1),
        )
        == "23000"
    )
    await _sql(
        harness,
        "UPDATE credit_lifecycle_episodes SET reactivated_at = :at WHERE id = :id",
        id=episode,
        at=first + timedelta(hours=2),
    )
    reopen = "UPDATE credit_lifecycle_episodes SET reactivated_at = NULL WHERE id = :id"
    assert await _refused(harness, reopen, "is frozen", id=episode) == "23000"
    delete = "DELETE FROM credit_lifecycle_episodes WHERE id = :id"
    assert await _refused(harness, delete, "append-only", id=episode) == "23000"


async def test_one_open_episode_per_builder_by_partial_index(harness: _Harness) -> None:
    builder = uuid4()
    insert = (
        "INSERT INTO credit_lifecycle_episodes (id, builder_id, account_id, "
        "opened_effect_id, exhausted_at, suspension_due_at, deletion_due_at, "
        "reactivated_at) VALUES (gen_random_uuid(), :builder, gen_random_uuid(), "
        "'e', now(), now() + interval '72 hours', now() + interval '30 days', :closed)"
    )
    await _sql(harness, insert, replica=True, builder=builder, closed=None)
    with pytest.raises(DBAPIError, match="uq_credit_lifecycle_episodes_open") as raised:
        await _sql(harness, insert, replica=True, builder=builder, closed=None)
    assert _sqlstate(raised.value) == "23505"
    # A closed episode does not count.
    await _sql(
        harness,
        insert,
        replica=True,
        builder=builder,
        closed=datetime.now(UTC) + timedelta(days=1),
    )


async def test_hold_generation_grows_by_exactly_one(harness: _Harness) -> None:
    builder = uuid4()
    await _sql(
        harness,
        "INSERT INTO credit_hold_generations (builder_id, scope) VALUES (:b, 'builder')",
        b=builder,
    )
    jump = (
        "UPDATE credit_hold_generations SET generation = generation + 2 "
        "WHERE builder_id = :b"
    )
    assert await _refused(harness, jump, "exactly one", b=builder) == "23000"
    await _sql(
        harness,
        "UPDATE credit_hold_generations SET generation = generation + 1 "
        "WHERE builder_id = :b",
        b=builder,
    )
    delete = "DELETE FROM credit_hold_generations WHERE builder_id = :b"
    assert await _refused(harness, delete, "never deleted", b=builder) == "23000"
    await _sql(harness, jump, replica=True, b=builder)


async def test_concurrent_fence_cas_never_creates_a_second_fence(
    harness: _Harness,
) -> None:
    builder = uuid4()

    async def fence() -> UUID | None:
        async with harness.session() as session, session.begin():
            return await ContentRetentionRepository(session).insert_fence(
                builder_id=builder, scope="builder", generation=3, operation_id=uuid4()
            )

    results = await asyncio.gather(*(fence() for _ in range(6)))
    assert len([value for value in results if value is not None]) == 1
    async with harness.engine.connect() as connection:
        count = await connection.scalar(
            text("SELECT count(*) FROM credit_purge_fences WHERE builder_id = :b"),
            {"b": builder},
        )
    assert count == 1


# ---------------------------------------------------------------------------
# The lifecycle under concurrency (the account lock serializes).
# ---------------------------------------------------------------------------


async def _grant_topup(harness: _Harness, builder: UUID, mc: int, at: datetime) -> None:
    order = uuid4()
    async with harness.session() as session, session.begin():
        ledger = CreditLedger(session)
        book = await ledger.open_book(builder, at=at)
        book.grant(
            GrantSpec(
                lot_kind="topup",
                effect_id=topup_grant_effect_id(order),
                payment_id=f"pay-{order}",
                paid_at=at.date(),
                granted_mc=mc,
                expires_at=topup_lot_expires_at(at.date()),
                order_id=order,
            ),
            at=at,
        )
        await ledger.persist(book)


async def _debit(harness: _Harness, world: _World, mc: int, at: datetime) -> str:
    async with harness.session() as session, session.begin():
        result = await CreditLedger(session).debit(
            world.builder,
            f"trace:{world.tenant}:{uuid4()}",
            mc,
            at=at,
            effect_kind="trace",
            tenant_id=world.tenant,
        )
        return result.status


async def _rows(harness: _Harness, model: Any, builder: UUID) -> list[Any]:
    async with harness.session() as session:
        return list(
            (
                await session.execute(select(model).where(model.builder_id == builder))
            ).scalars()
        )


async def test_concurrent_exhausting_debits_open_one_episode_and_one_alert_each(
    harness: _Harness, world: _World, clock: _Clock
) -> None:
    await _grant_topup(harness, world.builder, 4_000, ENFORCED + timedelta(minutes=1))
    statuses = await asyncio.gather(
        *(_debit(harness, world, 1_000, clock.now) for _ in range(8))
    )
    assert statuses == ["applied"] * 8
    episodes = await _rows(harness, CreditLifecycleEpisode, world.builder)
    assert len(episodes) == 1
    alerts = await _rows(harness, CreditThresholdAlert, world.builder)
    assert sorted(alert.threshold for alert in alerts) == [50, 90, 100]
    assert len({alert.arm_key for alert in alerts}) == 1


async def test_worker_and_request_racing_at_the_deadline_record_one_suspension(
    harness: _Harness, world: _World, clock: _Clock
) -> None:
    assert await _debit(harness, world, 1_000, clock.now) == "applied"
    [episode] = await _rows(harness, CreditLifecycleEpisode, world.builder)
    due = episode.suspension_due_at
    clock.now = due

    async def gate() -> str:
        async with harness.session() as session, session.begin():
            return await credit_lifecycle.request_gate(session, world.builder, now=due)

    def factory() -> AsyncSession:
        return harness.session()

    results = await asyncio.gather(
        gate(),
        gate(),
        credit_lifecycle.run_lifecycle_once(factory, now=due),
        credit_lifecycle.run_lifecycle_once(factory, now=due),
        return_exceptions=True,
    )
    assert not [value for value in results if isinstance(value, BaseException)], results
    assert results[:2] == ["suspended", "suspended"]
    transitions = await _rows(harness, CreditLifecycleTransition, world.builder)
    suspended = [t for t in transitions if t.transition == "suspended"]
    assert len(suspended) == 1
    assert suspended[0].occurred_at == due
