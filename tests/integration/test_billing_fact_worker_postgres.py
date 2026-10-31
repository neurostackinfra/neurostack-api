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
import signal
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    BillingFactPublisher,
    BillingFactReconciler,
    FactRetryPolicy,
    PublishAttempt,
    TransportNotReady,
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

ROOT = Path(__file__).resolve().parents[2]


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
    """Per-worker fake consumer; the pause keeps the presentation in flight."""

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
        # The other backend finishes a whole tick while the lease is held.
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
async def test_shutdown_mid_batch_keeps_committed_work_and_the_in_flight_attempt(
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
        config=_config(transport_timeout_seconds=30.0),
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
    # Leased before it was presented: the cancel keeps the attempt counted.
    assert await _delivery(clean, in_flight) == ("pending", 1)
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


# ---------------------------------------------------------------------------
# Two-phase attempt record (asaas-rollout-opening Group 2a)
# ---------------------------------------------------------------------------

# A throwaway worker process that is SIGKILLed by its own transport right
# after the fact reached the consumer: no unwinding, no rollback, no phase 2;
# PostgreSQL only sees the backend connection vanish.
_KILLED_AFTER_SEND = textwrap.dedent("""
    import asyncio, os, signal
    from datetime import datetime
    from sqlalchemy.ext.asyncio import (
        AsyncSession, async_sessionmaker, create_async_engine,
    )
    from sqlalchemy.pool import NullPool
    from src.services import billing_outbox
    from src.services.billing_fact_worker import (
        BillingFactWorker, BillingFactWorkerConfig,
    )

    # The same explicit test-only waiver as the in-process fixture.
    billing_outbox.PRESENTATION_PRECONDITIONS_OPEN = False
    print(billing_outbox.__file__, flush=True)
    now = datetime.fromisoformat(os.environ["FACT_KILL_NOW"])

    async def killed_after_send(payload):
        with open(os.environ["FACT_KILL_MARKER"], "w", encoding="utf-8") as sent:
            sent.write(str(payload["event_id"]))
        os.kill(os.getpid(), signal.SIGKILL)

    async def main():
        engine = create_async_engine(os.environ["FACT_KILL_URL"], poolclass=NullPool)
        sessions = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        config = BillingFactWorkerConfig(
            interval_seconds=30.0, batch_size=50, max_attempts=3,
            stale_after_seconds=900.0, transport_timeout_seconds=5.0,
        )
        worker = BillingFactWorker(
            sessions, transport=killed_after_send, config=config, now=lambda: now
        )
        await worker.run_tick()

    asyncio.run(main())
    raise SystemExit(3)  # reached only if nothing was presented
""")


async def _attempt_row(harness: _Harness, outbox_id: UUID) -> tuple[str, int, Any]:
    (row,) = await _rows(
        harness,
        "SELECT state, attempt_count, last_attempt_at FROM billing_fact_deliveries "
        "WHERE outbox_event_id = :id",
        id=outbox_id,
    )
    return str(row[0]), int(row[1]), row[2]


async def _unlocked(harness: _Harness) -> int:
    async with harness.engine.begin() as connection:
        locked = await connection.execute(
            text(
                "SELECT count(*) FROM (SELECT id FROM billing_fact_deliveries "
                "FOR UPDATE NOWAIT) AS locked_rows"
            )
        )
        return int(locked.scalar_one())


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_a_worker_killed_after_the_send_leaves_exactly_one_attempt_counted(
    clean: _Harness, tmp_path: Path
) -> None:
    outbox_id = await _seed_fact(clean)
    clock = _Clock()
    marker = tmp_path / "sent"
    child = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-c", _KILLED_AFTER_SEND],
        cwd=ROOT,
        env={
            **os.environ,
            "FACT_KILL_URL": clean.url.render_as_string(hide_password=False),
            "FACT_KILL_NOW": clock.value.isoformat(),
            "FACT_KILL_MARKER": str(marker),
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert child.returncode == -signal.SIGKILL, child.stderr
    # This checkout's producer ran (not another worktree on the venv path),
    # and the consumer really received the fact before the process died.
    assert child.stdout.splitlines()[0] == str(ROOT / "src/services/billing_outbox.py")
    assert marker.read_text(encoding="utf-8") == str(outbox_id)

    state, count, last_attempt_at = await _attempt_row(clean, outbox_id)
    assert (state, count, last_attempt_at) == ("pending", 1, clock.value)
    assert await _unlocked(clean) == 1  # the dead backend left no row lock

    log: list[tuple[str, str]] = []
    engine, sessions = clean.factory()
    restarted = BillingFactWorker(
        sessions,
        transport=_Recorder("restarted", log, pause=0.0),
        config=_config(),
        now=clock,
    )
    try:
        # The attempt the dead process sent still holds its backoff...
        assert (await restarted.run_tick()).presented == 0
        clock.advance(seconds=59)
        assert (await restarted.run_tick()).presented == 0
        # ...and only then is the same fact presented again, counted once more.
        clock.advance(seconds=2)
        assert (await restarted.run_tick()).presented == 1
    finally:
        await engine.dispose()
    assert log == [("restarted", str(outbox_id))]
    assert await _delivery(clean, outbox_id) == ("awaiting_ack", 2)


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_concurrent_workers_never_duplicate_an_attempt(clean: _Harness) -> None:
    outbox_id = await _seed_fact(clean)
    clock = _Clock()
    held, release = asyncio.Event(), asyncio.Event()
    holder_sent: list[str] = []

    async def holding(payload: dict[str, object]) -> None:
        holder_sent.append(str(payload["event_id"]))
        held.set()
        await release.wait()

    # The competitor runs 59 s later: still inside backoff(1) of the lease.
    late = _Clock()
    late.value = clock.value + timedelta(seconds=59)
    other_log: list[tuple[str, str]] = []
    holder_engine, holder_sessions = clean.factory()
    other_engine, other_sessions = clean.factory()
    holder = BillingFactWorker(
        holder_sessions,
        transport=holding,
        config=_config(transport_timeout_seconds=30.0),
        now=clock,
    )
    other = BillingFactWorker(
        other_sessions,
        transport=_Recorder("other", other_log, pause=0.0),
        config=_config(),
        now=late,
    )
    try:
        holding_tick = asyncio.create_task(holder.run_tick())
        await asyncio.wait_for(held.wait(), timeout=10)
        # Mid-presentation, from a third backend: the attempt is committed
        # and no row lock is held, so only the lease keeps the others out.
        in_flight = await _attempt_row(clean, outbox_id)
        unlocked = await _unlocked(clean)
        competing = await asyncio.wait_for(
            asyncio.gather(other.run_tick(), other.run_tick()), timeout=10
        )
        assert not holding_tick.done()
        release.set()
        finished = await asyncio.wait_for(holding_tick, timeout=10)
    finally:
        release.set()
        await holder_engine.dispose()
        await other_engine.dispose()

    assert in_flight == ("pending", 1, clock.value)
    assert unlocked == 1
    assert [report.presented for report in competing] == [0, 0]
    assert other_log == []
    assert (finished.presented, holder_sent) == (1, [str(outbox_id)])
    assert await _delivery(clean, outbox_id) == ("awaiting_ack", 1)


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_a_fact_locked_by_a_competing_phase_one_is_skipped_never_presented(
    clean: _Harness,
) -> None:
    outbox_id = await _seed_fact(clean)
    log: list[tuple[str, str]] = []
    engine, sessions = clean.factory()
    worker = BillingFactWorker(
        sessions, transport=_Recorder("b", log, pause=0.0), config=_config()
    )
    try:
        async with clean.engine.connect() as competitor:
            transaction = await competitor.begin()
            # Another worker inside phase 1: row claimed, attempt not committed.
            await competitor.execute(
                text(
                    "SELECT id FROM billing_fact_deliveries "
                    "WHERE outbox_event_id = :id FOR UPDATE"
                ),
                {"id": outbox_id},
            )
            during = await asyncio.wait_for(worker.run_tick(), timeout=10)
            await competitor.execute(
                text(
                    "UPDATE billing_fact_deliveries SET attempt_count = "
                    "attempt_count + 1, last_attempt_at = now() "
                    "WHERE outbox_event_id = :id"
                ),
                {"id": outbox_id},
            )
            await transaction.commit()
        # Once that lease is committed, the claim re-reads it: still not due.
        after = await asyncio.wait_for(worker.run_tick(), timeout=10)
    finally:
        await engine.dispose()
    assert (during.presented, after.presented, log) == (0, 0, [])
    assert await _delivery(clean, outbox_id) == ("pending", 1)


class _Unready:
    def __init__(self) -> None:
        self.ready = False
        self.sent: list[str] = []

    async def __call__(self, payload: dict[str, object]) -> None:
        self.sent.append(str(payload["event_id"]))
        if not self.ready:
            raise TransportNotReady("consumer readiness flipped off")


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_an_unready_consumer_spends_no_attempt_and_does_not_spin_on_postgres(
    clean: _Harness,
) -> None:
    ids = [await _seed_fact(clean) for _ in range(2)]
    clock, consumer = _Clock(), _Unready()
    engine, sessions = clean.factory()
    # One attempt allowed: a spent not-ready attempt would exhaust the fact.
    looping = BillingFactWorker(
        sessions,
        transport=consumer,
        config=_config(interval_seconds=0.01, max_attempts=1),
        now=clock,
        monotonic=lambda: 0.0,
    )
    try:
        task = looping.start()
        deadline = asyncio.get_running_loop().time() + 10
        while len(consumer.sent) < len(ids) and not task.done():
            assert asyncio.get_running_loop().time() < deadline, consumer.sent
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.5)  # many 10 ms ticks at the same instant
        await looping.stop()
        assert task.done() and not task.cancelled()
        # Each fact was turned away exactly once: no spin, no attempt spent,
        # and the turned-away instant is kept as the start of the backoff.
        assert sorted(consumer.sent) == sorted(str(i) for i in ids)
        for outbox_id in ids:
            assert await _attempt_row(clean, outbox_id) == ("pending", 0, clock.value)

        clock.advance(seconds=61)
        consumer.ready = True
        ready = BillingFactWorker(
            sessions, transport=consumer, config=_config(max_attempts=1), now=clock
        )
        report = await ready.run_tick()
    finally:
        await engine.dispose()
    assert (report.presented, report.exhausted, report.not_ready) == (2, 0, 0)
    for outbox_id in ids:
        assert await _delivery(clean, outbox_id) == ("awaiting_ack", 1)


async def _set_delivery(
    harness: _Harness, outbox_id: UUID, values: str, **params: Any
) -> None:
    async with harness.engine.begin() as connection:
        await connection.execute(
            text(
                f"UPDATE billing_fact_deliveries SET {values} "
                "WHERE outbox_event_id = :id"
            ),
            {"id": outbox_id, **params},
        )


async def _publish_once(
    harness: _Harness, clock: _Clock, transport: Any
) -> PublishAttempt | None:
    async with harness.sessions() as session:
        return await BillingFactPublisher(session, now=clock).publish_next(
            transport, policy=FactRetryPolicy(max_attempts=3), timeout=5.0
        )


@pytest.mark.usefixtures("presentation_preconditions_waived")
@pytest.mark.parametrize(
    ("attempt_count", "backoff"),
    [
        (0, timedelta(seconds=60)),  # a not-ready attempt given back
        (1, timedelta(seconds=60)),
        (2, timedelta(seconds=120)),
    ],
)
@pytest.mark.parametrize(
    ("early", "due"),
    [
        pytest.param(timedelta(0), True, id="at-the-instant"),
        pytest.param(timedelta(microseconds=1), False, id="one-microsecond-before"),
    ],
)
async def test_a_retry_is_due_exactly_when_its_backoff_has_elapsed_on_postgres(
    clean: _Harness,
    attempt_count: int,
    backoff: timedelta,
    early: timedelta,
    due: bool,
) -> None:
    outbox_id = await _seed_fact(clean)
    clock = _Clock()
    last_attempt_at = clock.value - backoff + early
    await _set_delivery(
        clean,
        outbox_id,
        "attempt_count = :count, last_attempt_at = :last",
        count=attempt_count,
        last=last_attempt_at,
    )
    log: list[tuple[str, str]] = []
    attempt = await _publish_once(clean, clock, _Recorder("w", log, pause=0.0))
    if due:
        assert attempt is not None
        assert (attempt.outcome, attempt.attempt_count) == (
            "presented",
            attempt_count + 1,
        )
        assert log == [("w", str(outbox_id))]
    else:
        assert attempt is None
        assert log == []
        assert await _attempt_row(clean, outbox_id) == (
            "pending",
            attempt_count,
            last_attempt_at,
        )


@pytest.mark.usefixtures("presentation_preconditions_waived")
@pytest.mark.parametrize("ready", [True, False])
@pytest.mark.parametrize(
    "takeover",
    [
        # Worker B leased (2, t1) after this lease expired, was turned away
        # and gave its attempt back: (1, t1), this lease's count.
        pytest.param(
            ("attempt_count = 2, last_attempt_at = :t1", "attempt_count = 1"),
            id="not-ready-give-back",
        ),
        # An operator requeue (0, NULL), then a fresh lease (1, t1).
        pytest.param(
            (
                "state = 'pending', attempt_count = 0, last_attempt_at = NULL",
                "attempt_count = 1, last_attempt_at = :t1",
            ),
            id="requeue-then-lease",
        ),
    ],
)
async def test_phase_two_never_overwrites_a_same_count_lease_on_postgres(
    clean: _Harness, ready: bool, takeover: tuple[str, ...]
) -> None:
    outbox_id = await _seed_fact(clean)
    clock = _Clock()
    t1 = clock.value + timedelta(seconds=61)

    async def overrun(payload: dict[str, object]) -> None:
        # Committed by other backends while this presentation outlived its
        # lease: only ``last_attempt_at`` tells the two leases apart.
        for values in takeover:
            await _set_delivery(clean, outbox_id, values, t1=t1)
        if not ready:
            raise TransportNotReady("consumer readiness flipped off")

    attempt = await _publish_once(clean, clock, overrun)
    assert attempt is not None
    assert attempt.outcome == ("presented" if ready else "not_ready")
    # No state, no compensation: the row is exactly as the other lease left it.
    assert await _attempt_row(clean, outbox_id) == ("pending", 1, t1)


@pytest.mark.usefixtures("presentation_preconditions_waived")
@pytest.mark.parametrize("ready", [True, False])
@pytest.mark.parametrize(
    ("terminal", "ack_sha256"),
    [
        pytest.param("acknowledged", None, id="acknowledged"),
        pytest.param("blocked", "0" * 64, id="blocked-digest-mismatch"),
    ],
)
async def test_a_fact_reconciled_mid_presentation_is_never_rewritten_on_postgres(
    clean: _Harness, ready: bool, terminal: str, ack_sha256: str | None
) -> None:
    outbox_id = await _seed_fact(clean)
    clock = _Clock()
    converged: list[Any] = []

    async def reconciled_mid_flight(payload: dict[str, object]) -> None:
        # No row lock is held during the send: the consumer acks (a digest
        # mismatch blocks) and the reconciler converges the row meanwhile.
        await _consumer_ack(clean, outbox_id, fact_sha256=ack_sha256)
        engine, sessions = clean.factory()
        try:
            async with sessions() as session, session.begin():
                converged.append(
                    await asyncio.wait_for(
                        BillingFactReconciler(session, now=clock).reconcile_open(
                            limit=10
                        ),
                        timeout=10,
                    )
                )
        finally:
            await engine.dispose()
        assert await _attempt_row(clean, outbox_id) == (terminal, 1, clock.value)
        if not ready:
            raise TransportNotReady("consumer readiness flipped off")

    attempt = await _publish_once(clean, clock, reconciled_mid_flight)
    assert len(converged) == 1
    assert attempt is not None
    assert attempt.outcome == ("presented" if ready else "not_ready")
    # Terminal stays terminal; nothing compensated, no attempt rewritten.
    assert await _attempt_row(clean, outbox_id) == (terminal, 1, clock.value)


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_phase_two_waits_for_a_reconciler_holding_the_row_on_postgres(
    clean: _Harness,
) -> None:
    outbox_id = await _seed_fact(clean)
    clock = _Clock()
    engine, sessions = clean.factory()
    holding = asyncio.Event()
    reconciler_pid: list[int] = []

    async def blocked_waiters() -> int:
        (row,) = await _rows(
            clean,
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE :pid = ANY(pg_blocking_pids(pid))",
            pid=reconciler_pid[0],
        )
        return int(row[0])

    try:
        async with sessions() as reconciler:

            async def acked_while_presenting(payload: dict[str, object]) -> None:
                # The reconciler converges the row and keeps its transaction
                # (and the row lock) open as this presentation returns.
                await _consumer_ack(clean, outbox_id)
                await BillingFactReconciler(reconciler, now=clock).reconcile_open(
                    limit=10
                )
                reconciler_pid.append(
                    int(await reconciler.scalar(text("SELECT pg_backend_pid()")))
                )
                holding.set()

            publishing = asyncio.create_task(
                _publish_once(clean, clock, acked_while_presenting)
            )
            await asyncio.wait_for(holding.wait(), timeout=10)
            # Commit only once phase 2 is provably queued behind the lock.
            deadline = asyncio.get_running_loop().time() + 10
            while await blocked_waiters() == 0:
                assert not publishing.done(), "phase 2 did not wait for the row lock"
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.01)
            await reconciler.commit()
            attempt = await asyncio.wait_for(publishing, timeout=10)
    finally:
        await engine.dispose()
    assert attempt is not None and attempt.outcome == "presented"
    assert await _attempt_row(clean, outbox_id) == ("acknowledged", 1, clock.value)
