"""Real PostgreSQL proofs for the Group 9 billing.fact.v1 worker.

Every "worker" here is a :class:`BillingFactWorker` on its own engine, so two
workers are genuinely separate backends competing for the same rows, exactly
like two ECS tasks.  Facts are produced by ``BillingFactProducer`` on top of
migration-head schema (triggers, CHECKs, immutability) from a disposable
child database.  No network: the consumer transport is an in-memory fake.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.models.billing import BillingPaymentEffect, BillingPaymentProjection
from src.services import billing_outbox as outbox_module
from src.services.billing_catalog import BILLING_CATALOG
from src.services.billing_checkout import topup_pricing_snapshot
from src.services.billing_fact_worker import (
    BillingFactWorker,
    BillingFactWorkerConfig,
    billing_fact_maintenance_advisory_key,
)
from src.services.billing_outbox import (
    ACK_OVERDUE_DETAIL,
    ACK_SCHEMA_VERSION,
    RETRY_EXHAUSTED_DETAIL,
    BillingFactProducer,
    BillingFactReconciler,
    _detail_digest,
)
from tests.integration.test_asaas_billing_postgres import (
    _CHILD_PATTERN,
    _create_child,
    _drop_child,
    _run_alembic,
    _validated_admin_url,
)

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _Harness:
    url: Any
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]

    def factory(self) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
        """A separate engine: one more competing backend (one more ECS task)."""

        engine = create_async_engine(self.url, poolclass=NullPool)
        return engine, async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )


@dataclass(frozen=True)
class _Template:
    admin_engine: AsyncEngine
    admin_url: Any
    name: str


def _child_name() -> str:
    name = f"neurostack_asaas_smoke_{uuid4().hex[:12]}"
    assert _CHILD_PATTERN.fullmatch(name)
    return name


@pytest.fixture(scope="module")
async def template() -> AsyncIterator[_Template]:
    """Migrate one database to head once; each proof copies it (TEMPLATE)."""

    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin_engine = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    name = _child_name()
    created = False
    try:
        await _create_child(admin_engine, name)
        created = True
        await _run_alembic(admin_url.set(database=name), "upgrade", "head")
        yield _Template(admin_engine=admin_engine, admin_url=admin_url, name=name)
    finally:
        if created:
            await _drop_child(admin_engine, name)
        await admin_engine.dispose()


@pytest.fixture
def presentation_preconditions_waived(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit test-only waiver: production cannot wire a transport yet."""

    monkeypatch.setattr(outbox_module, "PRESENTATION_PRECONDITIONS_OPEN", False)


@pytest.fixture
async def clean(template: _Template) -> AsyncIterator[_Harness]:
    """A fresh migrated database per proof: no open fact from another test."""

    name = _child_name()
    async with template.admin_engine.connect() as connection:
        quote = connection.dialect.identifier_preparer.quote
        await connection.exec_driver_sql(
            f"CREATE DATABASE {quote(name)} TEMPLATE {quote(template.name)}"
        )
    url = template.admin_url.set(database=name)
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        yield _Harness(
            url=url,
            engine=engine,
            sessions=async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
            ),
        )
    finally:
        await engine.dispose()
        await _drop_child(template.admin_engine, name)


class _Clock:
    def __init__(self) -> None:
        self.value = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **delta: float) -> None:
        self.value += timedelta(**delta)


def _config(**overrides: Any) -> BillingFactWorkerConfig:
    values: dict[str, Any] = {
        "interval_seconds": 30.0,
        "batch_size": 50,
        "max_attempts": 3,
        "stale_after_seconds": 900.0,
        "transport_timeout_seconds": 5.0,
    }
    values.update(overrides)
    return BillingFactWorkerConfig(**values)


async def _seed_fact(harness: _Harness) -> UUID:
    """Pre-existing webhook state, then only the producer writes the fact."""

    builder_id, order_id, delivery_id, projection_id, effect_id = (
        uuid4() for _ in range(5)
    )
    payment_id = f"pay-worker-{projection_id}"
    snapshot = topup_pricing_snapshot(
        BILLING_CATALOG["package_credits_500_v1"], payment_method="pix"
    )
    async with harness.engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO builders (id, name) VALUES (:id, 'Fact Worker PG')"),
            {"id": builder_id},
        )
        await connection.execute(
            text("""
                INSERT INTO billing_topup_orders (
                    id, builder_id, payment_method, catalog_version_id,
                    offer_id, amount_cents, credit_grant_mc, pricing_snapshot
                ) VALUES (
                    :id, :builder_id, 'pix', :catalog_version_id,
                    'package_credits_500_v1', 10000, :grant, CAST(:snapshot AS JSONB)
                )
            """),
            {
                "id": order_id,
                "builder_id": builder_id,
                "catalog_version_id": snapshot["catalog_version_id"],
                "grant": snapshot["credit_grant_mc"],
                "snapshot": json.dumps(snapshot),
            },
        )
        await connection.execute(
            text("""
                INSERT INTO billing_webhook_deliveries (
                    id, builder_id, provider, event_id, event_name,
                    provider_created_at, body_bytes, body_sha256
                ) VALUES (
                    :id, :builder_id, 'asaas', :event_id, 'PAYMENT_RECEIVED',
                    now(), 2, :sha
                )
            """),
            {
                "id": delivery_id,
                "builder_id": builder_id,
                "event_id": f"worker-{delivery_id}",
                "sha": "a" * 64,
            },
        )
        await connection.execute(
            text("""
                INSERT INTO billing_payment_projections (
                    id, builder_id, provider, payment_id, aggregate_kind,
                    topup_order_id, status, amount_cents, settled_cents,
                    paid_at, provider_created_at, ordering_key, last_event_id
                ) VALUES (
                    :id, :builder_id, 'asaas', :payment_id, 'topup',
                    :order_id, 'settled', 10000, 10000, DATE '2026-10-11', now(),
                    '2026-10-12T00:00:00Z:0', :event_id
                )
            """),
            {
                "id": projection_id,
                "builder_id": builder_id,
                "payment_id": payment_id,
                "order_id": order_id,
                "event_id": f"worker-{delivery_id}",
            },
        )
    async with harness.sessions() as session, session.begin():
        await session.execute(
            text("""
                INSERT INTO billing_payment_effects (
                    id, builder_id, projection_id, delivery_id, provider,
                    payment_id, effect_kind, source_revision, amount_cents
                ) VALUES (
                    :id, :builder_id, :projection_id, :delivery_id, 'asaas',
                    :payment_id, 'settlement', 'settlement:v1', 10000
                )
            """),
            {
                "id": effect_id,
                "builder_id": builder_id,
                "projection_id": projection_id,
                "delivery_id": delivery_id,
                "payment_id": payment_id,
            },
        )
        effect = await session.get(BillingPaymentEffect, effect_id)
        projection = await session.get(BillingPaymentProjection, projection_id)
        assert effect is not None and projection is not None
        outbox = await BillingFactProducer(session).record_effect(effect, projection)
        assert outbox is not None
        outbox_id = outbox.id
    return outbox_id


async def _rows(harness: _Harness, sql: str, **params: Any) -> list[Any]:
    async with harness.engine.connect() as connection:
        return list((await connection.execute(text(sql), params)).all())


async def _delivery(harness: _Harness, outbox_id: UUID) -> tuple[str, int]:
    (row,) = await _rows(
        harness,
        "SELECT state, attempt_count FROM billing_fact_deliveries "
        "WHERE outbox_event_id = :id",
        id=outbox_id,
    )
    return str(row[0]), int(row[1])


async def _alert_count(harness: _Harness, outbox_id: UUID, detail: str) -> int:
    (row,) = await _rows(
        harness,
        "SELECT count(*) FROM billing_fact_reconciliation_alerts "
        "WHERE outbox_event_id = :id AND kind = 'backlog' AND detail_sha256 = :d",
        id=outbox_id,
        d=_detail_digest(detail),
    )
    return int(row[0])


async def _consumer_ack(
    harness: _Harness, outbox_id: UUID, *, fact_sha256: str | None = None
) -> None:
    """Consumer-owned append-only ack; the producer only ever reads it."""

    async with harness.engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO billing_fact_acks (
                    id, outbox_event_id, event_id, schema_version, outcome,
                    inbox_event_id, fact_sha256, bundle_digest, deployment_digest,
                    effects_digest, acknowledged_at
                )
                SELECT :id, outbox.id, outbox.id, :schema, 'accepted', :inbox,
                       COALESCE(:fact_sha256, outbox.payload_sha256),
                       handoff.bundle_digest,
                       handoff.deployment_digest, :effects, now()
                FROM billing_outbox outbox
                JOIN billing_fact_handoffs handoff ON handoff.outbox_event_id = outbox.id
                WHERE outbox.id = :outbox_id
            """),
            {
                "id": uuid4(),
                "schema": ACK_SCHEMA_VERSION,
                "inbox": uuid4(),
                "effects": "e" * 64,
                "outbox_id": outbox_id,
                "fact_sha256": fact_sha256,
            },
        )


class _Recorder:
    """Per-worker fake consumer; the pause keeps the row lock held for a while."""

    def __init__(self, name: str, log: list[tuple[str, str]], pause: float) -> None:
        self.name, self.log, self.pause = name, log, pause

    async def __call__(self, payload: dict[str, object]) -> None:
        self.log.append((self.name, str(payload["event_id"])))
        await asyncio.sleep(self.pause)


# ---------------------------------------------------------------------------
# Claims: two competing workers, no double presentation
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_two_workers_present_every_fact_exactly_once(clean: _Harness) -> None:
    ids = {str(await _seed_fact(clean)) for _ in range(10)}
    log: list[tuple[str, str]] = []
    engines = []
    workers = []
    clock = _Clock()
    for name in ("task-a", "task-b"):
        engine, sessions = clean.factory()
        engines.append(engine)
        workers.append(
            BillingFactWorker(
                sessions,
                transport=_Recorder(name, log, pause=0.05),
                config=_config(),
                now=clock,
            )
        )
    try:
        for _ in range(3):  # repeated concurrent ticks: nothing new is due
            await asyncio.gather(*(worker.run_tick() for worker in workers))
    finally:
        for engine in engines:
            await engine.dispose()

    presented = [event_id for _name, event_id in log]
    assert sorted(presented) == sorted(ids)  # each fact exactly once
    assert {name for name, _event in log} == {"task-a", "task-b"}  # truly parallel
    for outbox_id in ids:
        assert await _delivery(clean, UUID(outbox_id)) == ("awaiting_ack", 1)


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_a_claimed_fact_is_skipped_not_waited_on_by_the_other_worker(
    clean: _Harness,
) -> None:
    ids = [await _seed_fact(clean) for _ in range(4)]
    held, release = asyncio.Event(), asyncio.Event()
    holder_sent: list[str] = []
    other_log: list[tuple[str, str]] = []

    async def holding(payload: dict[str, object]) -> None:
        holder_sent.append(str(payload["event_id"]))
        held.set()
        await release.wait()

    holder_engine, holder_sessions = clean.factory()
    other_engine, other_sessions = clean.factory()
    holder = BillingFactWorker(
        holder_sessions,
        transport=holding,
        config=_config(batch_size=1, transport_timeout_seconds=30.0),
    )
    other = BillingFactWorker(
        other_sessions,
        transport=_Recorder("other", other_log, pause=0.0),
        config=_config(),
    )
    try:
        holding_tick = asyncio.create_task(holder.run_tick())
        await asyncio.wait_for(held.wait(), timeout=10)
        # The other backend finishes a whole tick while the row lock is held.
        report = await asyncio.wait_for(other.run_tick(), timeout=10)
        assert not holding_tick.done()
        release.set()
        await asyncio.wait_for(holding_tick, timeout=10)
    finally:
        release.set()
        await holder_engine.dispose()
        await other_engine.dispose()

    (locked,) = holder_sent
    other_sent = [event_id for _name, event_id in other_log]
    assert locked not in other_sent
    assert report.presented == 3
    assert sorted([locked, *other_sent]) == sorted(str(i) for i in ids)


# ---------------------------------------------------------------------------
# Maintenance: one reconciler/escalator at a time, no duplicate evidence
# ---------------------------------------------------------------------------


async def test_maintenance_lock_is_exclusive_and_never_waited_on(
    clean: _Harness,
) -> None:
    outbox_id = await _seed_fact(clean)
    clock = _Clock()
    clock.advance(minutes=16)
    engine, sessions = clean.factory()
    worker = BillingFactWorker(sessions, transport=None, config=_config(), now=clock)
    try:
        async with clean.engine.connect() as holder:
            transaction = await holder.begin()
            await holder.execute(
                text("SELECT pg_advisory_xact_lock(:key)"),
                {"key": billing_fact_maintenance_advisory_key()},
            )
            skipped = await asyncio.wait_for(worker.run_tick(), timeout=10)
            await transaction.rollback()
        assert (skipped.maintained, skipped.escalated) == (False, 0)
        assert skipped.backlog.stale >= 1  # observation still runs everywhere
        assert await _alert_count(clean, outbox_id, ACK_OVERDUE_DETAIL) == 0

        maintained = await worker.run_tick()
        assert maintained.maintained is True and maintained.escalated >= 1
        assert await _alert_count(clean, outbox_id, ACK_OVERDUE_DETAIL) == 1
    finally:
        await engine.dispose()


async def test_concurrent_workers_without_a_consumer_escalate_each_fact_once(
    clean: _Harness,
) -> None:
    ids = [await _seed_fact(clean) for _ in range(6)]
    clock = _Clock()
    clock.advance(minutes=16)
    engines, workers = [], []
    for _ in range(3):
        engine, sessions = clean.factory()
        engines.append(engine)
        workers.append(
            BillingFactWorker(
                sessions, transport=None, config=_config(batch_size=2), now=clock
            )
        )
    try:
        for _ in range(4):
            reports = await asyncio.gather(*(w.run_tick() for w in workers))
            assert all(report.presented == 0 for report in reports)
    finally:
        for engine in engines:
            await engine.dispose()
    assert reports[0].backlog.stale == len(ids)
    for outbox_id in ids:
        assert await _alert_count(clean, outbox_id, ACK_OVERDUE_DETAIL) == 1
        assert await _delivery(clean, outbox_id) == ("pending", 0)


# ---------------------------------------------------------------------------
# Bounded retry, terminal state, replay and shutdown on PostgreSQL
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_retry_cap_blocks_on_postgres_and_the_fact_is_kept(
    clean: _Harness,
) -> None:
    outbox_id = await _seed_fact(clean)
    (before,) = await _rows(
        clean,
        "SELECT payload_sha256, payload::text FROM billing_outbox WHERE id = :id",
        id=outbox_id,
    )

    async def failing(payload: dict[str, object]) -> None:
        raise ConnectionError("consumer unavailable")

    clock = _Clock()
    engine, sessions = clean.factory()
    worker = BillingFactWorker(
        sessions, transport=failing, config=_config(max_attempts=2), now=clock
    )
    try:
        outcomes = []
        for _ in range(4):
            report = await worker.run_tick()
            outcomes.append((report.failed, report.exhausted))
            clock.advance(hours=1)
    finally:
        await engine.dispose()
    assert outcomes == [(1, 0), (1, 0), (0, 1), (0, 0)]
    assert await _delivery(clean, outbox_id) == ("blocked", 2)
    assert await _alert_count(clean, outbox_id, RETRY_EXHAUSTED_DETAIL) == 1
    (after,) = await _rows(
        clean,
        "SELECT payload_sha256, payload::text FROM billing_outbox WHERE id = :id",
        id=outbox_id,
    )
    assert tuple(after) == tuple(before)
    with pytest.raises(DBAPIError, match="billing_outbox is append-only"):
        async with clean.engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM billing_outbox WHERE id = :id"), {"id": outbox_id}
            )


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_lost_ack_replay_then_ack_converges_on_postgres(
    clean: _Harness,
) -> None:
    outbox_id = await _seed_fact(clean)
    log: list[tuple[str, str]] = []
    clock = _Clock()
    engine, sessions = clean.factory()
    worker = BillingFactWorker(
        sessions,
        transport=_Recorder("a", log, pause=0.0),
        config=_config(),
        now=clock,
    )
    try:
        await worker.run_tick()
        clock.advance(seconds=61)
        await worker.run_tick()  # ack lost: the same event id again
        await _consumer_ack(clean, outbox_id)
        clock.advance(hours=1)
        await worker.run_tick()
        clock.advance(hours=1)
        replay = await worker.run_tick()
    finally:
        await engine.dispose()
    assert [event_id for _name, event_id in log] == [str(outbox_id)] * 2
    assert replay.presented == 0
    assert await _delivery(clean, outbox_id) == ("acknowledged", 2)
    (facts,) = await _rows(
        clean, "SELECT count(*) FROM billing_outbox WHERE id = :id", id=outbox_id
    )
    assert facts[0] == 1


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_shutdown_mid_batch_keeps_committed_work_and_rolls_back_the_rest(
    clean: _Harness,
) -> None:
    ids = [await _seed_fact(clean) for _ in range(3)]
    entered = asyncio.Event()
    sent: list[str] = []

    async def second_hangs(payload: dict[str, object]) -> None:
        sent.append(str(payload["event_id"]))
        if len(sent) == 2:
            entered.set()
            await asyncio.Event().wait()

    engine, sessions = clean.factory()
    worker = BillingFactWorker(
        sessions,
        transport=second_hangs,
        config=_config(transport_timeout_seconds=60.0),
    )
    try:
        task = worker.start()
        await asyncio.wait_for(entered.wait(), timeout=10)
        await worker.stop(grace=0.2)
        assert task.cancelled()
    finally:
        await engine.dispose()

    first, in_flight = (UUID(event_id) for event_id in sent)
    (untouched,) = set(ids) - {first, in_flight}
    assert await _delivery(clean, first) == ("awaiting_ack", 1)  # committed
    assert await _delivery(clean, in_flight) == ("pending", 0)  # rolled back
    assert await _delivery(clean, untouched) == ("pending", 0)  # never claimed
    async with clean.engine.begin() as connection:
        locked = await connection.execute(
            text(
                "SELECT count(*) FROM (SELECT id FROM billing_fact_deliveries "
                "FOR UPDATE NOWAIT) AS locked_rows"
            )
        )
        assert locked.scalar_one() == 3  # no row lock survived the shutdown


# ---------------------------------------------------------------------------
# Acknowledged facts never alert (the correlated valid-ack EXISTS on PG types)
# ---------------------------------------------------------------------------


async def test_acked_unreconciled_facts_are_not_stale_or_escalated_on_postgres(
    clean: _Harness,
) -> None:
    ids = [await _seed_fact(clean) for _ in range(3)]
    for outbox_id in ids:
        await _consumer_ack(clean, outbox_id)
    mismatched = await _seed_fact(clean)
    await _consumer_ack(clean, mismatched, fact_sha256="0" * 64)
    stale_before = datetime.now(timezone.utc) + timedelta(minutes=1)
    # No reconcile first: every delivery is still ``pending`` with its ack.
    async with clean.sessions() as session, session.begin():
        reconciler = BillingFactReconciler(session)
        backlog = await reconciler.backlog(stale_before=stale_before)
        escalated = await reconciler.escalate_overdue(
            stale_before=stale_before, limit=10
        )
    # Only the mismatching ack still counts and is escalated.
    assert (backlog.unacked, backlog.stale, backlog.blocked) == (1, 1, 0)
    assert escalated == 1
    for outbox_id in ids:
        assert await _delivery(clean, outbox_id) == ("pending", 0)
        assert await _alert_count(clean, outbox_id, ACK_OVERDUE_DETAIL) == 0
    assert await _alert_count(clean, mismatched, ACK_OVERDUE_DETAIL) == 1
