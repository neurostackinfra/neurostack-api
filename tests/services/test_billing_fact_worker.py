"""Group 9: periodic billing.fact.v1 delivery, bounded retry and escalation.

SQLite proves the state machine (backoff, cap, terminal ``blocked``, overdue
escalation, shutdown, flag-off inertness); PostgreSQL concurrency and lock
semantics live in ``tests/integration/test_billing_fact_worker_postgres.py``.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import os
import re
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import pytest
import yaml
from prometheus_client import REGISTRY
from pydantic import ValidationError
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config.settings import Settings, settings
from src.models.base import Base
from src.models.billing import (
    BillingFactAck,
    BillingFactDelivery,
    BillingFactHandoff,
    BillingFactReconciliationAlert,
    BillingOutboxEvent,
    BillingPaymentEffect,
    BillingPaymentProjection,
    BillingTopupOrder,
    BillingWebhookDelivery,
)
from src.models.builder import Builder
from src.repositories.database import _patch_jsonb_for_sqlite
from src.services import billing_fact_worker as worker_module
from src.services import billing_outbox as outbox_module
from src.services.billing_catalog import BILLING_CATALOG
from src.services.billing_checkout import topup_pricing_snapshot
from src.services.billing_fact_worker import (
    BillingFactWorker,
    BillingFactWorkerConfig,
    start_billing_fact_worker,
)
from src.services.billing_outbox import (
    ACK_MISSING_DETAIL,
    ACK_OVERDUE_DETAIL,
    ACK_SCHEMA_VERSION,
    RETRY_EXHAUSTED_DETAIL,
    BillingFactProducer,
    BillingFactPublisher,
    BillingFactReconciler,
    FactRetryPolicy,
    TransportNotReady,
    _detail_digest,
)

ROOT = Path(__file__).resolve().parents[2]
PRIMARY_RULES = ROOT / "config" / "prometheus_rules.yml"
MIRROR_RULES = ROOT / "src" / "config" / "prometheus_rules.yml"
_MAX_RULE = re.compile(
    r'^max\((?P<metric>[a-z_]+)\{(?P<label>[a-z_]+)="(?P<value>[a-z_]+)"\}\) '
    r"> (?P<threshold>\d+)$"
)
_STALLED_RULE = re.compile(
    r"^max\((?P<metric>[a-z_]+)\) > 0 and \(time\(\) - max\((?P=metric)\)\) "
    r"> (?P<threshold>\d+)$"
)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


class _Clock:
    """Injected wall clock; deliveries keep their real server-default time."""

    def __init__(self) -> None:
        self.value = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **delta: float) -> None:
        self.value += timedelta(**delta)


class _Transport:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[str] = []

    async def __call__(self, payload: dict[str, object]) -> None:
        self.sent.append(str(payload["event_id"]))
        if self.fail:
            raise RuntimeError("consumer unavailable")


@pytest.fixture
async def factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    _patch_jsonb_for_sqlite()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'facts.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest.fixture
def presentation_preconditions_waived(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the presentation guard closed for one test.

    The guard is closed by default since asaas-rollout-opening Group 2c; the
    tests of the fake transport state machine still pin it, so they keep
    proving the machine if the guard is ever reopened.
    """

    monkeypatch.setattr(outbox_module, "PRESENTATION_PRECONDITIONS_OPEN", False)


class _LogRecorder:
    def __init__(self) -> None:
        self.errors: list[tuple[str, dict[str, Any]]] = []
        self.warnings: list[tuple[str, dict[str, Any]]] = []

    def error(self, event: str, **fields: Any) -> None:
        self.errors.append((event, fields))

    def warning(self, event: str, **fields: Any) -> None:
        self.warnings.append((event, fields))

    def info(self, event: str, **fields: Any) -> None:
        pass


def _config(**overrides: Any) -> BillingFactWorkerConfig:
    values: dict[str, Any] = {
        "interval_seconds": 30.0,
        "batch_size": 10,
        "max_attempts": 3,
        "stale_after_seconds": 900.0,
        "transport_timeout_seconds": 1.0,
    }
    values.update(overrides)
    return BillingFactWorkerConfig(**values)


async def _seed_fact(factory: async_sessionmaker[AsyncSession]) -> UUID:
    """Persist one settled top-up fact exactly as the webhook producer does."""

    builder_id, order_id, projection_id, delivery_id = (uuid4() for _ in range(4))
    payment_id = f"pay_worker_{projection_id.hex}"
    now = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
    async with factory() as session:
        session.add_all(
            (
                Builder(id=builder_id, name="Fact Worker Fixture"),
                BillingTopupOrder(
                    id=order_id,
                    builder_id=builder_id,
                    catalog_version_id="payment-2026-10-03",
                    offer_id="package_credits_500_v1",
                    payment_method="pix",
                    amount_cents=10_000,
                    credit_grant_mc=500_000,
                    pricing_snapshot=topup_pricing_snapshot(
                        BILLING_CATALOG["package_credits_500_v1"],
                        payment_method="pix",
                    ),
                ),
                BillingWebhookDelivery(
                    id=delivery_id,
                    builder_id=builder_id,
                    provider="asaas",
                    event_id=f"event-{delivery_id}",
                    event_name="PAYMENT_RECEIVED",
                    provider_created_at=now,
                    body_bytes=2,
                    body_sha256="a" * 64,
                    status="processed",
                ),
                BillingPaymentProjection(
                    id=projection_id,
                    builder_id=builder_id,
                    provider="asaas",
                    payment_id=payment_id,
                    aggregate_kind="topup",
                    subscription_id=None,
                    topup_order_id=order_id,
                    checkout_session_id=None,
                    status="settled",
                    amount_cents=10_000,
                    settled_cents=10_000,
                    reversed_cents=0,
                    restored_cents=0,
                    restoration_epoch=0,
                    paid_at=date(2026, 10, 11),
                    provider_created_at=now,
                    ordering_key="00000000000000001.00:fixture",
                    last_event_id="fixture",
                ),
            )
        )
        await session.flush()
        effect = BillingPaymentEffect(
            id=uuid4(),
            builder_id=builder_id,
            projection_id=projection_id,
            delivery_id=delivery_id,
            provider="asaas",
            payment_id=payment_id,
            effect_kind="settlement",
            source_revision="settlement:v1",
            amount_cents=10_000,
        )
        session.add(effect)
        await session.flush()
        projection = await session.get(BillingPaymentProjection, projection_id)
        assert projection is not None
        outbox = await BillingFactProducer(session).record_effect(effect, projection)
        assert outbox is not None
        await session.commit()
        return outbox.id


async def _delivery(
    factory: async_sessionmaker[AsyncSession], outbox_id: UUID
) -> BillingFactDelivery:
    async with factory() as session:
        delivery = await session.scalar(
            select(BillingFactDelivery).where(
                BillingFactDelivery.outbox_event_id == outbox_id
            )
        )
        assert delivery is not None
        return delivery


async def _alerts(
    factory: async_sessionmaker[AsyncSession], outbox_id: UUID, detail: str
) -> int:
    async with factory() as session:
        return int(
            await session.scalar(
                select(func.count(BillingFactReconciliationAlert.id)).where(
                    BillingFactReconciliationAlert.outbox_event_id == outbox_id,
                    BillingFactReconciliationAlert.kind == "backlog",
                    BillingFactReconciliationAlert.detail_sha256
                    == _detail_digest(detail),
                )
            )
            or 0
        )


async def _ack(
    factory: async_sessionmaker[AsyncSession],
    outbox_id: UUID,
    *,
    fact_sha256: str | None = None,
) -> None:
    """Deliberately a consumer-owned insert: the producer never writes acks."""

    async with factory() as session:
        stored = await session.get(BillingOutboxEvent, outbox_id)
        handoff = await session.scalar(
            select(BillingFactHandoff).where(
                BillingFactHandoff.outbox_event_id == outbox_id
            )
        )
        assert stored is not None and handoff is not None
        session.add(
            BillingFactAck(
                id=uuid4(),
                outbox_event_id=outbox_id,
                event_id=outbox_id,
                schema_version=ACK_SCHEMA_VERSION,
                outcome="accepted",
                inbox_event_id=uuid4(),
                fact_sha256=fact_sha256 or stored.payload_sha256,
                bundle_digest=handoff.bundle_digest,
                deployment_digest=handoff.deployment_digest,
                effects_digest="e" * 64,
                acknowledged_at=datetime(2026, 10, 12, 12, tzinfo=timezone.utc),
            )
        )
        await session.commit()


def _sample(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _rules(path: Path = PRIMARY_RULES) -> dict[str, dict[str, Any]]:
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {
        rule["alert"]: rule
        for group in parsed["groups"]
        for rule in group["rules"]
        if "alert" in rule
    }


def _backlog_rule_fires(alert: str) -> bool:
    """Evaluate the exact ``max(metric{label="v"}) > N`` rule shape locally."""

    match = _MAX_RULE.fullmatch(_rules()[alert]["expr"].strip())
    assert match is not None, "rule shape changed; update the local evaluator"
    value = REGISTRY.get_sample_value(match["metric"], {match["label"]: match["value"]})
    return value is not None and value > int(match["threshold"])


def _stalled_rule_fires(now: float) -> bool:
    match = _STALLED_RULE.fullmatch(_rules()["BillingFactWorkerStalled"]["expr"])
    assert match is not None, "rule shape changed; update the local evaluator"
    value = REGISTRY.get_sample_value(match["metric"]) or 0.0
    return value > 0 and now - value > int(match["threshold"])


# ---------------------------------------------------------------------------
# Flag and lifespan wiring
# ---------------------------------------------------------------------------


def test_flag_defaults_off_and_bounds_are_validated() -> None:
    assert Settings.model_fields["billing_fact_worker_enabled"].default is False
    assert Settings.model_fields["billing_fact_worker_stale_after_seconds"].default == (
        900
    )
    with pytest.raises(
        ValidationError, match=r"billing_fact_worker_interval_seconds\n.*greater than"
    ):
        Settings(billing_fact_worker_interval_seconds=1)
    with pytest.raises(
        ValidationError, match=r"billing_fact_worker_max_attempts\n.*less than"
    ):
        Settings(billing_fact_worker_max_attempts=51)
    with pytest.raises(
        ValueError, match="^billing fact worker bounds must be positive$"
    ):
        _config(batch_size=0)
    with pytest.raises(ValueError, match="^max_attempts must be positive$"):
        FactRetryPolicy(max_attempts=0)
    with pytest.raises(
        ValueError, match="^retry backoff must be positive and ordered$"
    ):
        FactRetryPolicy(max_attempts=1, base_backoff=timedelta(0))


async def test_flag_off_touches_nothing_and_starts_no_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import main as main_module

    def no_pool() -> Any:
        raise AssertionError("flag-off path must not touch the database pool")

    monkeypatch.setattr(settings, "billing_fact_worker_enabled", False)
    monkeypatch.setattr(
        "src.repositories.database.get_db_session_factory", no_pool, raising=True
    )
    assert start_billing_fact_worker(settings, no_pool) is None
    assert main_module._start_billing_fact_worker() is None
    assert all(task.get_name() != "billing-fact-worker" for task in asyncio.all_tasks())


async def test_real_lifespan_starts_and_stops_the_loop_only_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.main import app

    async def loop_tasks() -> list[asyncio.Task[Any]]:
        return [
            task
            for task in asyncio.all_tasks()
            if task.get_name() == "billing-fact-worker"
        ]

    async def keep_aiosqlite_responsive() -> None:
        while True:
            await asyncio.sleep(0.01)

    heartbeat = asyncio.create_task(keep_aiosqlite_responsive())
    try:
        async with app.router.lifespan_context(app):
            assert await loop_tasks() == []

        monkeypatch.setattr(settings, "billing_fact_worker_enabled", True)
        async with app.router.lifespan_context(app):
            (task,) = await loop_tasks()
            assert not task.done()
        # Graceful: the lifespan stopped the loop before closing the pool.
        assert task.done() and not task.cancelled()
    finally:
        heartbeat.cancel()


async def test_enabled_start_runs_ticks_until_stopped(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    enabled = settings.model_copy(
        update={
            "billing_fact_worker_enabled": True,
            "billing_fact_worker_batch_size": 5,
        }
    )
    worker = start_billing_fact_worker(enabled, lambda: factory)
    assert worker is not None and worker.consumer_ready is False
    before = _sample("neurostack_billing_fact_worker_ticks_total", outcome="completed")
    deadline = time.monotonic() + 10
    while (
        _sample("neurostack_billing_fact_worker_ticks_total", outcome="completed")
        <= before
    ):
        assert time.monotonic() < deadline, "the enabled loop never completed a tick"
        await asyncio.sleep(0.02)
    await worker.stop()
    assert worker._task is not None and worker._task.done()


# ---------------------------------------------------------------------------
# Presentation, backoff, replay and acknowledgement
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_lost_ack_re_presents_the_same_fact_only_after_its_backoff(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_fact(factory)
    clock, transport = _Clock(), _Transport()
    worker = BillingFactWorker(
        factory, transport=transport, config=_config(), now=clock
    )

    first = await worker.run_tick()
    assert (first.presented, first.maintained) == (1, True)
    assert (await _delivery(factory, outbox_id)).state == "awaiting_ack"

    assert (await worker.run_tick()).presented == 0  # backoff(1) = 60s not due
    clock.advance(seconds=61)
    assert (await worker.run_tick()).presented == 1

    assert transport.sent == [str(outbox_id), str(outbox_id)]  # same event id
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.state, delivery.attempt_count) == ("awaiting_ack", 2)
    async with factory() as session:
        assert await session.scalar(select(func.count(BillingOutboxEvent.id))) == 1


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_ack_ends_presentation_and_replays_add_no_evidence(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_fact(factory)
    clock, transport = _Clock(), _Transport()
    worker = BillingFactWorker(
        factory, transport=transport, config=_config(), now=clock
    )
    await worker.run_tick()
    await _ack(factory, outbox_id)

    clock.advance(hours=2)
    report = await worker.run_tick()
    assert report.presented == 0
    delivery = await _delivery(factory, outbox_id)
    assert delivery.state == "acknowledged"
    assert delivery.acknowledged_at is not None
    async with factory() as session:
        alerts = await session.scalar(
            select(func.count(BillingFactReconciliationAlert.id))
        )
    for _ in range(3):
        clock.advance(hours=1)
        assert (await worker.run_tick()).presented == 0
    async with factory() as session:
        assert (
            await session.scalar(select(func.count(BillingFactReconciliationAlert.id)))
            == alerts
        )
    assert transport.sent == [str(outbox_id)]


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_ack_race_after_claim_is_skipped_without_presentation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_fact(factory)
    await _ack(factory, outbox_id)
    transport = _Transport()
    async with factory() as session:
        publisher = BillingFactPublisher(session)
        # Bypass the claim's NOT EXISTS so the post-lock ack re-read is what
        # stops the presentation (the window a concurrent consumer commit has).
        original = publisher._due_claim

        def claim_ignoring_ack(policy: FactRetryPolicy, now: datetime) -> Any:
            return (
                select(BillingFactDelivery, BillingOutboxEvent)
                .join(
                    BillingOutboxEvent,
                    BillingOutboxEvent.id == BillingFactDelivery.outbox_event_id,
                )
                .limit(1)
            )

        publisher._due_claim = claim_ignoring_ack  # type: ignore[method-assign]
        attempt = await publisher.publish_next(
            transport, policy=FactRetryPolicy(max_attempts=3), timeout=1.0
        )
        publisher._due_claim = original  # type: ignore[method-assign]
    assert attempt is not None and attempt.outcome == "skipped"
    assert transport.sent == []
    with pytest.raises(ValueError, match="^timeout must be positive$"):
        async with factory() as session:
            await BillingFactPublisher(session).publish_next(
                transport, policy=FactRetryPolicy(max_attempts=1), timeout=0
            )


# ---------------------------------------------------------------------------
# Bounded retry and terminal state
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_retry_cap_blocks_retains_the_fact_and_alerts_once(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_fact(factory)
    async with factory() as session:
        original = await session.get(BillingOutboxEvent, outbox_id)
        assert original is not None
        payload, digest = dict(original.payload), original.payload_sha256
    clock, transport = _Clock(), _Transport(fail=True)
    worker = BillingFactWorker(
        factory, transport=transport, config=_config(max_attempts=3), now=clock
    )
    exhausted_before = _sample(
        "neurostack_billing_fact_publish_attempts_total", outcome="exhausted"
    )

    reports = []
    for _ in range(3):
        reports.append(await worker.run_tick())
        clock.advance(hours=1)  # past every capped backoff
    assert [report.failed for report in reports] == [1, 1, 1]
    assert (await _delivery(factory, outbox_id)).state == "pending"

    terminal = await worker.run_tick()
    assert (terminal.exhausted, terminal.failed, terminal.presented) == (1, 0, 0)
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.state, delivery.attempt_count) == ("blocked", 3)
    assert delivery.acknowledged_at is None
    assert await _alerts(factory, outbox_id, RETRY_EXHAUSTED_DETAIL) == 1
    assert terminal.backlog.blocked == 1 and terminal.backlog.unacked == 0
    assert _sample("neurostack_billing_fact_backlog", condition="blocked") == 1
    assert _backlog_rule_fires("BillingFactDeliveryBlocked")
    assert (
        _sample("neurostack_billing_fact_publish_attempts_total", outcome="exhausted")
        == exhausted_before + 1
    )

    for _ in range(2):
        clock.advance(hours=2)
        report = await worker.run_tick()
        assert (report.presented, report.failed, report.exhausted) == (0, 0, 0)
    assert len(transport.sent) == 3
    assert await _alerts(factory, outbox_id, RETRY_EXHAUSTED_DETAIL) == 1
    async with factory() as session:
        stored = await session.get(BillingOutboxEvent, outbox_id)
        assert stored is not None
        assert (dict(stored.payload), stored.payload_sha256) == (payload, digest)


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_late_valid_ack_still_converges_a_blocked_fact(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_fact(factory)
    clock = _Clock()
    worker = BillingFactWorker(
        factory,
        transport=_Transport(fail=True),
        config=_config(max_attempts=1),
        now=clock,
    )
    await worker.run_tick()
    clock.advance(hours=1)
    assert (await worker.run_tick()).exhausted == 1
    assert (await _delivery(factory, outbox_id)).state == "blocked"

    await _ack(factory, outbox_id)
    report = await worker.run_tick()
    assert (await _delivery(factory, outbox_id)).state == "acknowledged"
    assert report.backlog.blocked == 0


def test_backoff_is_exponential_capped_and_bounded_by_the_cap() -> None:
    policy = FactRetryPolicy(max_attempts=10)
    assert [policy.backoff(n).total_seconds() for n in range(0, 9)] == [
        0,
        60,
        120,
        240,
        480,
        960,
        1920,
        3600,
        3600,
    ]
    assert policy.backoff(500) == timedelta(hours=1)


# ---------------------------------------------------------------------------
# No consumer: retained, retriable, escalated
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_without_a_consumer_facts_stay_retriable_and_the_stale_alert_fires(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    first, second = await _seed_fact(factory), await _seed_fact(factory)
    clock = _Clock()
    worker = BillingFactWorker(factory, transport=None, config=_config(), now=clock)

    fresh = await worker.run_tick()
    assert (fresh.presented, fresh.escalated, fresh.backlog.unacked) == (0, 0, 2)
    assert fresh.backlog.stale == 0
    assert _sample("neurostack_billing_fact_backlog", condition="unacked") == 2
    assert not _backlog_rule_fires("BillingFactDeliveryStale")

    clock.advance(minutes=16)
    stuck = await worker.run_tick()
    assert (stuck.escalated, stuck.backlog.stale) == (2, 2)
    assert _sample("neurostack_billing_fact_backlog", condition="stale") == 2
    assert _backlog_rule_fires("BillingFactDeliveryStale")
    for outbox_id in (first, second):
        delivery = await _delivery(factory, outbox_id)
        assert (delivery.state, delivery.attempt_count) == ("pending", 0)
        assert delivery.last_attempt_at is None
        assert await _alerts(factory, outbox_id, ACK_OVERDUE_DETAIL) == 1
        assert await _alerts(factory, outbox_id, ACK_MISSING_DETAIL) == 1

    clock.advance(hours=6)
    again = await worker.run_tick()
    assert (again.escalated, again.backlog.stale) == (0, 2)
    assert await _alerts(factory, first, ACK_OVERDUE_DETAIL) == 1

    # Retriable: the same untouched rows are presented once a consumer exists.
    transport = _Transport()
    consumer = BillingFactWorker(
        factory, transport=transport, config=_config(), now=clock
    )
    assert (await consumer.run_tick()).presented == 2
    assert sorted(transport.sent) == sorted((str(first), str(second)))


async def test_stalled_worker_rule_is_silent_when_disabled_and_fires_when_stale(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    from src.observability.metrics import BILLING_FACT_WORKER_LAST_TICK

    BILLING_FACT_WORKER_LAST_TICK.set(0)  # a never-enabled process exports 0
    assert not _stalled_rule_fires(time.time())
    worker = BillingFactWorker(factory, transport=None, config=_config())
    await worker.run_tick()
    assert not _stalled_rule_fires(time.time())
    assert _stalled_rule_fires(time.time() + 601)


# ---------------------------------------------------------------------------
# Bounded runtime and graceful shutdown
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_tick_budget_stops_new_claims_after_the_deadline(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    for _ in range(3):
        await _seed_fact(factory)
    readings = iter([0.0, 0.0, 31.0])
    worker = BillingFactWorker(
        factory,
        transport=_Transport(),
        config=_config(),
        monotonic=lambda: next(readings, 31.0),
    )
    before = _sample("neurostack_billing_fact_worker_ticks_total", outcome="deadline")
    report = await worker.run_tick()
    assert (report.outcome, report.presented) == ("deadline", 1)
    assert report.backlog.unacked == 3  # presented is awaiting_ack, still unacked
    assert (
        _sample("neurostack_billing_fact_worker_ticks_total", outcome="deadline")
        == before + 1
    )


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_batch_size_bounds_one_tick(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    for _ in range(4):
        await _seed_fact(factory)
    transport = _Transport()
    worker = BillingFactWorker(
        factory, transport=transport, config=_config(batch_size=3)
    )
    assert (await worker.run_tick()).presented == 3
    assert (await worker.run_tick()).presented == 1


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_graceful_stop_finishes_the_in_flight_fact_and_claims_no_more(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = [await _seed_fact(factory) for _ in range(3)]
    entered, release = asyncio.Event(), asyncio.Event()
    sent: list[str] = []

    async def transport(payload: dict[str, object]) -> None:
        sent.append(str(payload["event_id"]))
        if len(sent) == 2:
            entered.set()
            await release.wait()

    worker = BillingFactWorker(
        factory,
        transport=transport,
        config=_config(transport_timeout_seconds=30.0),
    )
    worker.start()
    await asyncio.wait_for(entered.wait(), timeout=10)
    stopping = asyncio.create_task(worker.stop(grace=10))
    await asyncio.sleep(0.05)
    assert not stopping.done()  # waits for the in-flight presentation
    release.set()
    await asyncio.wait_for(stopping, timeout=10)

    states = [await _delivery(factory, outbox_id) for outbox_id in ids]
    presented = [d for d in states if d.state == "awaiting_ack"]
    untouched = [d for d in states if d.state == "pending"]
    assert len(sent) == 2 and len(presented) == 2 and len(untouched) == 1
    assert untouched[0].attempt_count == 0


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_stop_after_grace_cancels_the_stuck_presentation_and_keeps_its_attempt(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_fact(factory)
    entered = asyncio.Event()

    async def hanging(payload: dict[str, object]) -> None:
        entered.set()
        await asyncio.Event().wait()

    worker = BillingFactWorker(
        factory,
        transport=hanging,
        config=_config(transport_timeout_seconds=30.0),
    )
    task = worker.start()
    await asyncio.wait_for(entered.wait(), timeout=10)
    await worker.stop(grace=0.1)
    assert task.cancelled()
    # The consumer may have seen the fact: its attempt was committed before
    # the presentation, so the cancel neither erases it nor bypasses the cap.
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.state, delivery.attempt_count) == ("pending", 1)
    assert delivery.last_attempt_at is not None


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_slow_consumer_times_out_as_a_failed_attempt(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_fact(factory)

    async def slow(payload: dict[str, object]) -> None:
        await asyncio.sleep(5)

    worker = BillingFactWorker(
        factory, transport=slow, config=_config(transport_timeout_seconds=0.05)
    )
    report = await worker.run_tick()
    assert (report.failed, report.presented) == (1, 0)
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.state, delivery.attempt_count) == ("pending", 1)


async def test_a_failing_tick_is_counted_and_the_loop_keeps_running(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    calls = {"n": 0}

    def flaky() -> AsyncSession:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("database unavailable")
        return factory()

    worker = BillingFactWorker(
        flaky,  # type: ignore[arg-type]
        transport=None,
        config=_config(interval_seconds=0.01),
    )
    failed = _sample("neurostack_billing_fact_worker_ticks_total", outcome="failed")
    completed = _sample(
        "neurostack_billing_fact_worker_ticks_total", outcome="completed"
    )
    task = worker.start()
    deadline = time.monotonic() + 10
    while (
        _sample("neurostack_billing_fact_worker_ticks_total", outcome="completed")
        <= completed
    ):
        assert time.monotonic() < deadline, "loop did not recover after a failure"
        await asyncio.sleep(0.02)
    await worker.stop()
    assert (
        _sample("neurostack_billing_fact_worker_ticks_total", outcome="failed")
        == failed + 1
    )
    assert task.done() and not task.cancelled()


# ---------------------------------------------------------------------------
# Bounded reconciliation pages
# ---------------------------------------------------------------------------


async def test_reconcile_pages_are_bounded_wrap_and_visit_every_open_fact(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = sorted([await _seed_fact(factory) for _ in range(5)], key=lambda u: u.hex)
    seen: list[int] = []
    cursors: list[UUID | None] = []
    after: UUID | None = None
    for _ in range(3):
        async with factory() as session:
            page = await BillingFactReconciler(session).reconcile_open(
                limit=2, after=after
            )
            await session.commit()
        seen.append(page.result.backlog)
        cursors.append(page.next_after)
        after = page.next_after
    assert seen == [2, 2, 1]
    assert cursors == [ids[1], ids[3], None]
    for outbox_id in ids:
        assert await _alerts(factory, outbox_id, ACK_MISSING_DETAIL) == 1
    with pytest.raises(ValueError, match="^limit must be positive$"):
        async with factory() as session:
            await BillingFactReconciler(session).reconcile_open(limit=0)
    with pytest.raises(ValueError, match="^limit must be positive$"):
        async with factory() as session:
            await BillingFactReconciler(session).escalate_overdue(
                stale_before=datetime.now(timezone.utc), limit=0
            )


async def test_full_reconcile_and_open_page_agree_on_the_same_facts(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    acked = await _seed_fact(factory)
    await _seed_fact(factory)
    await _ack(factory, acked)
    async with factory() as session:
        full = await BillingFactReconciler(session).reconcile()
        await session.rollback()
    async with factory() as session:
        page = await BillingFactReconciler(session).reconcile_open(limit=10)
        await session.rollback()
    assert full == page.result
    assert (full.backlog, full.gaps, full.collisions) == (1, 0, 0)


# ---------------------------------------------------------------------------
# Review fix loop 1: liveness, ack exclusion, cancellation, guards
# ---------------------------------------------------------------------------


def _backlog_samples() -> dict[str, float | None]:
    return {
        condition: REGISTRY.get_sample_value(
            "neurostack_billing_fact_backlog", {"condition": condition}
        )
        for condition in ("unacked", "stale", "blocked")
    }


async def _wait_until(predicate: Any, message: str, *, seconds: float = 10) -> None:
    deadline = time.monotonic() + seconds
    while not await predicate():
        assert time.monotonic() < deadline, message
        await asyncio.sleep(0.02)


async def test_a_worker_failing_from_boot_fires_the_stall_rule_after_its_window() -> (
    None
):
    from src.observability.metrics import (
        BILLING_FACT_BACKLOG,
        BILLING_FACT_WORKER_LAST_TICK,
    )

    BILLING_FACT_WORKER_LAST_TICK.set(0)
    BILLING_FACT_BACKLOG.clear()
    clock = _Clock()
    booted = clock.value.timestamp()

    def broken() -> AsyncSession:
        raise ConnectionError("database unavailable")

    worker = BillingFactWorker(
        broken,  # type: ignore[arg-type]
        transport=None,
        config=_config(interval_seconds=0.01),
        now=clock,
    )
    failed = _sample("neurostack_billing_fact_worker_ticks_total", outcome="failed")
    completed = _sample(
        "neurostack_billing_fact_worker_ticks_total", outcome="completed"
    )
    worker.start()

    async def three_failures() -> bool:
        return (
            _sample("neurostack_billing_fact_worker_ticks_total", outcome="failed")
            >= failed + 3
        )

    await _wait_until(three_failures, "the broken loop stopped retrying")
    await worker.stop()
    # Never a completed tick and never a backlog sample: only the start stamp
    # can make this worker visible to an alert.
    assert (
        _sample("neurostack_billing_fact_worker_ticks_total", outcome="completed")
        == completed
    )
    assert _backlog_samples() == {"unacked": None, "stale": None, "blocked": None}
    assert not _backlog_rule_fires("BillingFactDeliveryStale")
    assert not _backlog_rule_fires("BillingFactDeliveryBlocked")
    assert _sample("neurostack_billing_fact_worker_last_tick_timestamp_seconds") == (
        booted
    )
    assert not _stalled_rule_fires(booted + 599)
    assert _stalled_rule_fires(booted + 601)


async def test_flag_off_exports_nothing_any_fact_alert_can_fire_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.main import app
    from src.observability.metrics import (
        BILLING_FACT_BACKLOG,
        BILLING_FACT_WORKER_LAST_TICK,
    )

    BILLING_FACT_WORKER_LAST_TICK.set(0)
    BILLING_FACT_BACKLOG.clear()
    monkeypatch.setattr(settings, "billing_fact_worker_enabled", False)

    async def keep_aiosqlite_responsive() -> None:
        while True:
            await asyncio.sleep(0.01)

    heartbeat = asyncio.create_task(keep_aiosqlite_responsive())
    try:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.05)
    finally:
        heartbeat.cancel()
    assert start_billing_fact_worker(settings, lambda: None) is None  # type: ignore
    assert _sample("neurostack_billing_fact_worker_last_tick_timestamp_seconds") == 0
    assert _backlog_samples() == {"unacked": None, "stale": None, "blocked": None}
    far_future = time.time() + 10**7
    assert not _stalled_rule_fires(far_future)
    assert not _backlog_rule_fires("BillingFactDeliveryStale")
    assert not _backlog_rule_fires("BillingFactDeliveryBlocked")


async def test_acked_facts_are_neither_stale_nor_escalated_before_reconciliation(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = [await _seed_fact(factory) for _ in range(3)]
    for outbox_id in ids:
        await _ack(factory, outbox_id)
    clock = _Clock()
    clock.advance(minutes=16)
    # batch_size=1: one fact converges per tick, the other two are still
    # ``pending`` rows that already carry a valid consumer acknowledgement.
    worker = BillingFactWorker(
        factory, transport=None, config=_config(batch_size=1), now=clock
    )
    report = await worker.run_tick()
    assert (report.backlog.unacked, report.backlog.stale) == (0, 0)
    assert report.escalated == 0
    for outbox_id in ids:
        assert await _alerts(factory, outbox_id, ACK_OVERDUE_DETAIL) == 0


async def test_a_mismatched_ack_still_counts_as_stale_until_it_is_blocked(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_fact(factory)
    await _ack(factory, outbox_id, fact_sha256="0" * 64)
    clock = _Clock()
    clock.advance(minutes=16)
    stale_before = clock() - timedelta(seconds=900)
    async with factory() as session:
        reconciler = BillingFactReconciler(session, now=clock)
        backlog = await reconciler.backlog(stale_before=stale_before)
        escalated = await reconciler.escalate_overdue(
            stale_before=stale_before, limit=10
        )
        await session.commit()
    # Only a *valid* ack removes a fact from the backlog: a mismatching one
    # keeps alerting until the reconciler moves it to ``blocked``.
    assert (backlog.unacked, backlog.stale, escalated) == (1, 1, 1)
    report = await BillingFactWorker(
        factory, transport=None, config=_config(), now=clock
    ).run_tick()
    assert (report.backlog.stale, report.backlog.blocked) == (0, 1)
    assert (await _delivery(factory, outbox_id)).state == "blocked"


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_a_stray_transport_cancellation_is_a_failed_attempt_not_a_dead_loop(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    outbox_id = await _seed_fact(factory)
    recorder = _LogRecorder()
    monkeypatch.setattr(worker_module, "logger", recorder)

    async def cancels(payload: dict[str, object]) -> None:
        raise asyncio.CancelledError()  # not aimed at the worker task

    worker = BillingFactWorker(
        factory,
        transport=cancels,
        config=_config(interval_seconds=0.01),
        monotonic=lambda: 0.0,  # a short interval must not exhaust the budget
    )
    task = worker.start()

    async def attempt_recorded() -> bool:
        return (await _delivery(factory, outbox_id)).attempt_count >= 1 or task.done()

    await _wait_until(attempt_recorded, "the stray cancellation was never recorded")
    await asyncio.sleep(0.05)
    assert not task.done(), "a stray cancellation killed the worker loop"
    await worker.stop()
    assert task.done() and not task.cancelled()
    delivery = await _delivery(factory, outbox_id)
    # Counted like any failed presentation: backoff and the cap now apply.
    assert (delivery.state, delivery.attempt_count) == ("pending", 1)
    assert any(
        fields.get("failure_class") == "CancelledError"
        for _event, fields in recorder.errors
    )


async def test_a_stray_cancellation_outside_the_transport_is_logged_and_survived(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _LogRecorder()
    monkeypatch.setattr(worker_module, "logger", recorder)
    calls = {"n": 0}

    def cancelling_once() -> AsyncSession:
        calls["n"] += 1
        if calls["n"] == 1:
            raise asyncio.CancelledError()  # e.g. a driver-internal cancel
        return factory()

    worker = BillingFactWorker(
        cancelling_once,  # type: ignore[arg-type]
        transport=None,
        config=_config(interval_seconds=0.01),
    )
    completed = _sample(
        "neurostack_billing_fact_worker_ticks_total", outcome="completed"
    )
    task = worker.start()

    async def recovered() -> bool:
        return task.done() or (
            _sample("neurostack_billing_fact_worker_ticks_total", outcome="completed")
            > completed
        )

    await _wait_until(recovered, "the loop never recovered")
    assert not task.done(), "a stray cancellation killed the worker loop"
    await worker.stop()
    assert task.done() and not task.cancelled()
    assert (
        "billing fact worker tick failed",
        {"failure_class": "CancelledError"},
    ) in recorder.errors


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_a_genuine_cancellation_of_the_loop_is_never_swallowed(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    outbox_id = await _seed_fact(factory)
    recorder = _LogRecorder()
    monkeypatch.setattr(worker_module, "logger", recorder)
    entered = asyncio.Event()

    async def hanging(payload: dict[str, object]) -> None:
        entered.set()
        await asyncio.Event().wait()

    in_flight = BillingFactWorker(
        factory, transport=hanging, config=_config(transport_timeout_seconds=30.0)
    )
    task = in_flight.start()
    await asyncio.wait_for(entered.wait(), timeout=10)
    task.cancel()  # a real cancellation aimed at the loop task
    await asyncio.wait({task}, timeout=10)  # never re-cancels on timeout
    assert task.done(), "a genuine cancellation was swallowed"
    assert task.cancelled()
    # Cancelled mid-presentation: the attempt leased before it stays counted.
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.state, delivery.attempt_count) == ("pending", 1)

    idle = BillingFactWorker(
        factory, transport=None, config=_config(interval_seconds=60.0)
    )
    idle_task = idle.start()
    await asyncio.sleep(0.2)  # first tick done, now waiting for the next
    idle_task.cancel()
    await asyncio.wait({idle_task}, timeout=10)
    assert idle_task.done(), "a genuine cancellation was swallowed"
    assert idle_task.cancelled()
    # A loop that died before stop() was asked to stop it is reported.
    await idle.stop()
    assert any(
        event == "billing fact worker loop was not running"
        for event, _fields in recorder.errors
    )


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_an_acked_unreconciled_fact_is_never_claimed_ahead_of_due_work(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    acked, due = await _seed_fact(factory), await _seed_fact(factory)
    await _ack(factory, acked)
    async with factory() as session:
        await session.execute(
            update(BillingOutboxEvent)
            .where(BillingOutboxEvent.id == acked)
            .values(created_at=datetime.now(timezone.utc) - timedelta(hours=1))
        )
        await session.commit()
    transport = _Transport()
    async with factory() as session:
        attempt = await BillingFactPublisher(session).publish_next(
            transport, policy=FactRetryPolicy(max_attempts=3), timeout=1.0
        )
        await session.commit()
    # The claim's NOT EXISTS(ack) filter, not the post-lock re-read, picks the
    # due fact: without it the oldest acked row is claimed and skipped forever.
    assert attempt is not None
    assert (attempt.outcome, attempt.event_id) == ("presented", due)
    assert transport.sent == [str(due)]


async def test_lifespan_stops_the_worker_before_closing_the_database_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.repositories as repositories
    from src.api.main import app

    order: list[str] = []
    real_close = repositories.close_database
    real_stop = BillingFactWorker.stop

    async def close_database() -> None:
        order.append("close_database")
        await real_close()

    async def stop(self: BillingFactWorker, **kwargs: Any) -> None:
        order.append("worker.stop")
        await real_stop(self, **kwargs)

    monkeypatch.setattr(repositories, "close_database", close_database)
    monkeypatch.setattr(BillingFactWorker, "stop", stop)
    monkeypatch.setattr(settings, "billing_fact_worker_enabled", True)

    async def keep_aiosqlite_responsive() -> None:
        while True:
            await asyncio.sleep(0.01)

    heartbeat = asyncio.create_task(keep_aiosqlite_responsive())
    try:
        async with app.router.lifespan_context(app):
            await asyncio.sleep(0.05)
    finally:
        heartbeat.cancel()
    assert order == ["worker.stop", "close_database"]


async def test_successive_ticks_advance_the_reconcile_cursor_over_every_fact(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = [await _seed_fact(factory) for _ in range(3)]
    worker = BillingFactWorker(factory, transport=None, config=_config(batch_size=1))
    for _ in range(3):
        assert (await worker.run_tick()).maintained
    # One page of one fact per tick: only an advancing cursor reaches all three.
    for outbox_id in ids:
        assert await _alerts(factory, outbox_id, ACK_MISSING_DETAIL) == 1


# ---------------------------------------------------------------------------
# The closed presentation guard and the main.py wiring
# (asaas-rollout-opening Group 2c)
# ---------------------------------------------------------------------------


async def test_the_presentation_guard_is_closed_and_bites_again_when_reopened(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Closed on purpose: the two-phase record (2a) and the requeue and its
    # runbook (2b) are on main.  Inverts the pin that asserted it open.
    assert outbox_module.PRESENTATION_PRECONDITIONS_OPEN is False
    outbox_module.require_presentation_allowed()
    first = await _seed_fact(factory)
    transport = _Transport()
    worker = BillingFactWorker(factory, transport=transport, config=_config())
    assert worker.consumer_ready
    assert (await worker.run_tick()).presented == 1
    assert transport.sent == [str(first)]

    # Reopened, every presenting entry point raises again before any work.
    monkeypatch.setattr(outbox_module, "PRESENTATION_PRECONDITIONS_OPEN", True)
    guard = outbox_module.PresentationPreconditionsOpen
    second = await _seed_fact(factory)
    with pytest.raises(guard, match="presentation preconditions are open"):
        BillingFactWorker(factory, transport=transport, config=_config())
    enabled = settings.model_copy(update={"billing_fact_worker_enabled": True})
    with pytest.raises(guard, match="presentation preconditions are open"):
        start_billing_fact_worker(enabled, lambda: factory, transport=transport)
    assert all(task.get_name() != "billing-fact-worker" for task in asyncio.all_tasks())
    async with factory() as session:
        with pytest.raises(guard, match="presentation preconditions are open"):
            await BillingFactPublisher(session).publish_next(
                transport, policy=FactRetryPolicy(max_attempts=3), timeout=1.0
            )
    assert transport.sent == [str(first)]
    delivery = await _delivery(factory, second)
    assert (delivery.state, delivery.attempt_count) == ("pending", 0)


def test_the_legacy_single_phase_sender_is_gone() -> None:
    # Whoever closes the guard removes the path it never covered.
    for name in ("publish_pending", "pending", "_record_attempt"):
        assert not hasattr(BillingFactPublisher, name), name
    assert not hasattr(outbox_module, "PublishedFact")
    source = (ROOT / "src/services/billing_outbox.py").read_text(encoding="utf-8")
    for name in ("publish_pending", "_record_attempt", "PublishedFact"):
        assert name not in source, name


def _captured_start(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    from src.api import main as main_module

    captured: dict[str, object] = {}

    def fake_start(source: Settings, provider: Any, **wiring: Any) -> None:
        assert source is settings
        captured.update(wiring)

    monkeypatch.setattr(main_module, "start_billing_fact_worker", fake_start)
    return captured


@pytest.mark.parametrize(
    ("worker_on", "transport_on"),
    [
        pytest.param(True, False, id="worker-on-transport-off"),
        pytest.param(False, True, id="worker-off-transport-on"),
        pytest.param(False, False, id="both-off"),
    ],
)
def test_main_wires_todays_worker_unless_both_settings_are_on(
    monkeypatch: pytest.MonkeyPatch, worker_on: bool, transport_on: bool
) -> None:
    from src.api import main as main_module

    assert Settings.model_fields["billing_fact_transport_enabled"].default is False
    monkeypatch.setattr(settings, "billing_fact_worker_enabled", worker_on)
    monkeypatch.setattr(settings, "billing_fact_transport_enabled", transport_on)
    captured = _captured_start(monkeypatch)
    assert main_module._start_billing_fact_worker() is None
    # Exactly today's call: no transport and no readiness hook.
    assert captured == {"transport": None}


def test_main_wires_the_credit_consumer_behind_its_readiness_hook(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import main as main_module

    monkeypatch.setattr(settings, "billing_fact_worker_enabled", True)
    monkeypatch.setattr(settings, "billing_fact_transport_enabled", True)
    captured = _captured_start(monkeypatch)
    main_module._start_billing_fact_worker()
    assert set(captured) == {"transport", "transport_ready"}
    assert captured["transport_ready"] is main_module._billing_fact_transport_ready
    assert inspect.iscoroutinefunction(captured["transport"])


async def test_with_the_transport_setting_off_main_starts_todays_worker(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.api import main as main_module

    outbox_id = await _seed_fact(factory)
    monkeypatch.setattr(settings, "billing_fact_worker_enabled", True)
    monkeypatch.setattr(settings, "billing_fact_transport_enabled", False)
    monkeypatch.setattr(
        "src.repositories.database.get_db_session_factory", lambda: factory
    )
    worker = main_module._start_billing_fact_worker()
    assert worker is not None
    await worker.stop()
    assert worker.consumer_ready is False
    report = await worker.run_tick()  # the loop is stopped: one more tick
    assert (report.presented, report.failed, report.not_ready) == (0, 0, 0)
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.state, delivery.attempt_count, delivery.last_attempt_at) == (
        "pending",
        0,
        None,
    )


# The consumer module registers its own metric families on import, so "not
# imported" is what keeps the setting-off process identical to today.
_CONSUMER_IMPORT_PROBE = """
import json, sys
from src.api import main
calls = []
main.start_billing_fact_worker = lambda source, provider, **wiring: calls.append(
    sorted(wiring)
)
main._start_billing_fact_worker()
print(json.dumps({
    "calls": calls,
    "imported": "src.services.credit_fact_consumer" in sys.modules,
}))
"""


@pytest.mark.parametrize(
    ("transport_setting", "imported", "wiring"),
    [
        pytest.param("false", False, ["transport"], id="off-never-imported"),
        pytest.param("true", True, ["transport", "transport_ready"], id="on"),
    ],
)
def test_with_the_transport_setting_off_the_consumer_is_never_imported(
    transport_setting: str, imported: bool, wiring: list[str]
) -> None:
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONSTARTUP"}
    }
    environment.update(
        PYTHONDONTWRITEBYTECODE="1",
        BILLING_FACT_WORKER_ENABLED="true",
        BILLING_FACT_TRANSPORT_ENABLED=transport_setting,
    )
    result = subprocess.run(
        [sys.executable, "-c", _CONSUMER_IMPORT_PROBE],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    probe = json.loads(result.stdout.strip().splitlines()[-1])
    assert probe == {"calls": [wiring], "imported": imported}


# ---------------------------------------------------------------------------
# The readiness hook: no attempt is spent while the consumer is not ready
# ---------------------------------------------------------------------------

# The switch causes, then the image-level faults of the consumer's own
# contract check (``consumer_contract_digests``): faults of the image, never
# of one fact, so none of them may spend an attempt.
_IMAGE_FAULTS = (
    "bundle_digest_unavailable",
    "bundle_digest_differs",
    "schema_unavailable",
    "schema_digest_differs",
    "catalog_differs",
)
_UNREADY_CAUSES = ("config_off", "schema_below_042", *_IMAGE_FAULTS)


@pytest.fixture
async def credit_switch_reset() -> AsyncIterator[None]:
    from src.services.credit_switch import stop_credit_switch

    try:
        yield
    finally:
        await stop_credit_switch(None)  # the process verdict back to off


async def _drive_readiness(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    cause: str | None,
) -> str:
    """Make the REAL switch verdict and contract check show ``cause``."""

    from src.services import credit_fact_consumer as consumer_module
    from src.services.credit_switch import refresh_credit_switch

    async with factory() as session:
        await session.execute(
            text(
                "CREATE TABLE IF NOT EXISTS alembic_version "
                "(version_num VARCHAR(32) NOT NULL)"
            )
        )
        await session.execute(text("DELETE FROM alembic_version"))
        await session.execute(
            text("INSERT INTO alembic_version (version_num) VALUES (:revision)"),
            {"revision": "041" if cause == "schema_below_042" else "042"},
        )
        await session.commit()
    reading = await refresh_credit_switch(
        enabled=cause != "config_off", session_factory=factory
    )
    missing = ROOT / "no-such-dir"
    if cause == "bundle_digest_unavailable":
        real_bundle = outbox_module.billing_fact_bundle_digest
        monkeypatch.setattr(
            consumer_module,
            "billing_fact_bundle_digest",
            lambda: real_bundle(missing / "SHA256SUMS"),
        )
    elif cause == "bundle_digest_differs":
        monkeypatch.setattr(consumer_module, "PINNED_BUNDLE_DIGEST", "0" * 64)
    elif cause == "schema_unavailable":
        real_schema = outbox_module.billing_fact_schema_digest
        monkeypatch.setattr(
            consumer_module,
            "billing_fact_schema_digest",
            lambda: real_schema(missing / "billing_fact_v1.schema.json"),
        )
    elif cause == "schema_digest_differs":
        monkeypatch.setattr(consumer_module, "PINNED_SCHEMA_DIGEST", "0" * 64)
    elif cause == "catalog_differs":
        real_catalog = consumer_module.catalog_snapshot_bytes
        monkeypatch.setattr(
            consumer_module,
            "catalog_snapshot_bytes",
            lambda: real_catalog() + b"another catalog",
        )
    return reading.reason


@pytest.mark.usefixtures("credit_switch_reset")
@pytest.mark.parametrize(
    ("cause", "reason"),
    [
        pytest.param(None, "schema_ready", id="ready"),
        pytest.param("config_off", "config_disabled", id="config_off"),
        pytest.param("schema_below_042", "schema_below_minimum", id="schema_041"),
        pytest.param("bundle_digest_unavailable", "schema_ready", id="no_manifest"),
        pytest.param("bundle_digest_differs", "schema_ready", id="bundle_pin"),
        pytest.param("schema_unavailable", "schema_ready", id="no_schema"),
        pytest.param("schema_digest_differs", "schema_ready", id="schema_pin"),
        pytest.param("catalog_differs", "schema_ready", id="catalog_pin"),
    ],
)
async def test_the_readiness_hook_is_false_for_each_of_its_causes(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    cause: str | None,
    reason: str,
) -> None:
    from src.api import main as main_module

    assert await _drive_readiness(factory, monkeypatch, cause) == reason
    assert main_module._billing_fact_transport_ready() is (cause is None)


def _not_ready_total() -> float:
    return _sample("neurostack_billing_fact_consumer_not_ready_total")


def _attempt_totals() -> dict[str, float]:
    return {
        outcome: _sample(
            "neurostack_billing_fact_publish_attempts_total", outcome=outcome
        )
        for outcome in ("presented", "failed", "exhausted")
    }


@pytest.mark.usefixtures("credit_switch_reset")
@pytest.mark.parametrize("cause", _UNREADY_CAUSES)
async def test_an_unready_consumer_never_spends_or_blocks_through_main(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    cause: str,
) -> None:
    from src.api import main as main_module
    from src.services import credit_fact_consumer as consumer_module

    await _drive_readiness(factory, monkeypatch, cause)
    handled: list[object] = []

    async def consumer(payload: dict[str, object]) -> None:
        handled.append(payload["event_id"])

    monkeypatch.setattr(consumer_module, "handle_billing_fact", consumer)
    outbox_id = await _seed_fact(factory)
    clock = _Clock()
    worker = BillingFactWorker(
        factory,
        transport=main_module._credit_fact_transport(),
        transport_ready=main_module._billing_fact_transport_ready,
        # One attempt allowed: a single spent attempt would block the fact.
        config=_config(max_attempts=1),
        now=clock,
    )
    not_ready, attempts = _not_ready_total(), _attempt_totals()
    for _ in range(4):
        report = await worker.run_tick()
        assert (report.outcome, report.not_ready, report.presented) == (
            "completed",
            1,
            0,
        )
        assert (report.failed, report.exhausted, report.backlog.blocked) == (0, 0, 0)
        delivery = await _delivery(factory, outbox_id)
        assert (delivery.state, delivery.attempt_count) == ("pending", 0)
        clock.advance(hours=2)  # past every backoff: due again each tick
    assert handled == []  # the hook turned it away before the consumer
    assert _not_ready_total() == not_ready + 4
    assert _attempt_totals() == attempts


@pytest.mark.usefixtures("credit_switch_reset")
async def test_the_switch_off_inside_the_real_consumer_is_not_an_attempt(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.api import main as main_module
    from src.services.credit_fact_consumer import CreditSwitchOff

    await _drive_readiness(factory, monkeypatch, "config_off")
    outbox_id = await _seed_fact(factory)
    async with factory() as session:
        stored = await session.get(BillingOutboxEvent, outbox_id)
        assert stored is not None
        payload = dict(stored.payload)
    transport = main_module._credit_fact_transport()
    with pytest.raises(TransportNotReady) as turned_away:
        await transport(payload)
    assert isinstance(turned_away.value.__cause__, CreditSwitchOff)

    # The hook is forced to say ready, so only main's translation of the
    # real consumer's CreditSwitchOff keeps the attempt from being counted.
    worker = BillingFactWorker(
        factory,
        transport=transport,
        transport_ready=lambda: True,
        config=_config(max_attempts=1),
        now=_Clock(),
    )
    report = await worker.run_tick()
    assert (report.not_ready, report.failed, report.exhausted) == (1, 0, 0)
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.state, delivery.attempt_count) == ("pending", 0)


@pytest.mark.parametrize(
    ("flips", "counted"),
    [
        pytest.param(True, False, id="readiness-flipped-off"),
        pytest.param(False, True, id="still-ready-a-real-failure"),
    ],
)
async def test_readiness_flipping_off_during_a_presentation_is_not_counted(
    factory: async_sessionmaker[AsyncSession], flips: bool, counted: bool
) -> None:
    outbox_id = await _seed_fact(factory)
    ready = [True]

    async def consumer(payload: dict[str, object]) -> None:
        if flips:
            ready[0] = False  # e.g. the switch refresher, mid-presentation
        raise RuntimeError("consumer failed")

    worker = BillingFactWorker(
        factory,
        transport=consumer,
        transport_ready=lambda: ready[0],
        config=_config(),
        now=_Clock(),
    )
    report = await worker.run_tick()
    assert (report.not_ready, report.failed) == ((0, 1) if counted else (1, 0))
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.state, delivery.attempt_count) == ("pending", int(counted))


_REAL_FAILURES = ("handoff_digest_mismatch", "unresolvable_fact", "database_error")


async def _make_the_real_consumer_fail(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    outbox_id: UUID,
    failure: str,
) -> type[BaseException]:
    """Make the REAL consumer raise a non-switch error for this one fact."""

    from sqlalchemy.exc import OperationalError

    from src.repositories.credit_fact_ack import CreditFactAckRepository
    from src.services import credit_fact_consumer as consumer_module

    monkeypatch.setattr(consumer_module, "default_session_factory", lambda: factory)
    if failure == "handoff_digest_mismatch":
        # Per fact (credit_fact_consumer ``_consume``): the image passes its
        # own contract check, the handoff names another bundle digest.
        async with factory() as session:
            await session.execute(
                update(BillingFactHandoff)
                .where(BillingFactHandoff.outbox_event_id == outbox_id)
                .values(bundle_digest="f" * 64)
            )
            await session.commit()
        return consumer_module.ConsumerContractDivergence
    if failure == "unresolvable_fact":
        async with factory() as session:
            await session.execute(
                BillingFactHandoff.__table__.delete().where(
                    BillingFactHandoff.outbox_event_id == outbox_id
                )
            )
            await session.commit()
        return consumer_module.UnresolvableFact

    async def unavailable(self: object, event_id: UUID) -> None:
        raise OperationalError("SELECT handoff", {}, ConnectionError("db down"))

    monkeypatch.setattr(CreditFactAckRepository, "resolve_handoff", unavailable)
    return OperationalError


@pytest.mark.usefixtures("credit_switch_reset")
@pytest.mark.parametrize("hook", ["forced_true", "main_hook"])
@pytest.mark.parametrize("failure", _REAL_FAILURES)
async def test_only_the_switch_is_translated_a_real_consumer_failure_is_counted(
    factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    hook: str,
) -> None:
    """Through main's transport and the REAL consumer, with the hook true: a
    failure that is not ``CreditSwitchOff`` spends its attempt (a broadened
    translation would make every failure free and the fact eternal)."""

    from src.api import main as main_module

    assert await _drive_readiness(factory, monkeypatch, None) == "schema_ready"
    outbox_id = await _seed_fact(factory)
    raised = await _make_the_real_consumer_fail(
        factory, monkeypatch, outbox_id, failure
    )
    async with factory() as session:
        stored = await session.get(BillingOutboxEvent, outbox_id)
        assert stored is not None
        payload = dict(stored.payload)
    transport = main_module._credit_fact_transport()
    with pytest.raises(raised):
        await transport(payload)
    # The per-fact faults never touch the image-level check.
    assert main_module._billing_fact_transport_ready() is True

    worker = BillingFactWorker(
        factory,
        transport=transport,
        transport_ready=(
            (lambda: True)
            if hook == "forced_true"
            else main_module._billing_fact_transport_ready
        ),
        config=_config(),
        now=_Clock(),
    )
    not_ready, attempts = _not_ready_total(), _attempt_totals()
    report = await worker.run_tick()
    assert (report.failed, report.not_ready, report.presented) == (1, 0, 0)
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.state, delivery.attempt_count) == ("pending", 1)
    assert _not_ready_total() == not_ready
    assert _attempt_totals() == {**attempts, "failed": attempts["failed"] + 1}


@pytest.mark.usefixtures("credit_switch_reset")
async def test_the_readiness_hook_never_reaches_the_database(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The image check is file-level: no statement per call of the hook."""

    from sqlalchemy import event

    from src.api import main as main_module

    await _drive_readiness(factory, monkeypatch, None)
    engine = factory.kw["bind"].sync_engine
    statements: list[str] = []

    def record(*args: object) -> None:
        statements.append(str(args[2]))

    event.listen(engine, "before_cursor_execute", record)
    try:
        started = time.perf_counter()
        for _ in range(50):
            assert main_module._billing_fact_transport_ready() is True
        elapsed = time.perf_counter() - started
    finally:
        event.remove(engine, "before_cursor_execute", record)
    assert statements == []
    # Measured: ~0.2 ms per call idle, ~0.5 ms with every core busy (at most
    # 27 ms for 50 calls): 0.5 s keeps a wide margin and still fails a hook
    # of 10 ms per call.
    assert elapsed < 0.5


@pytest.mark.usefixtures("credit_switch_reset")
async def test_the_hook_reads_only_a_contract_divergence_as_not_ready(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Any other error of the contract check is a bug: the hook lets it out,
    so the worker logs ``readiness unreadable`` instead of turning silent."""

    from src.api import main as main_module
    from src.services import credit_fact_consumer as consumer_module

    await _drive_readiness(factory, monkeypatch, None)

    def broken() -> bytes:
        raise TypeError("catalog snapshot is broken")

    monkeypatch.setattr(consumer_module, "catalog_snapshot_bytes", broken)
    with pytest.raises(TypeError, match="catalog snapshot is broken"):
        main_module._billing_fact_transport_ready()

    recorder = _LogRecorder()
    monkeypatch.setattr(worker_module, "logger", recorder)
    outbox_id = await _seed_fact(factory)
    transport = _Transport()
    worker = BillingFactWorker(
        factory,
        transport=transport,
        transport_ready=main_module._billing_fact_transport_ready,
        config=_config(),
        now=_Clock(),
    )
    report = await worker.run_tick()
    assert (report.not_ready, report.failed) == (1, 0)
    assert transport.sent == []
    assert (await _delivery(factory, outbox_id)).attempt_count == 0
    assert (
        "billing fact consumer readiness unreadable",
        {"failure_class": "TypeError"},
    ) in recorder.errors


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_a_timeout_behind_the_hook_stays_a_counted_attempt(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """The timeout cancels the presentation: the readiness wrapper must let
    the cancellation through even when the hook has turned false."""

    outbox_id = await _seed_fact(factory)
    ready = [True]

    async def hanging(payload: dict[str, object]) -> None:
        ready[0] = False
        await asyncio.Event().wait()

    worker = BillingFactWorker(
        factory,
        transport=hanging,
        transport_ready=lambda: ready[0],
        config=_config(transport_timeout_seconds=0.05),
        now=_Clock(),
    )
    report = await worker.run_tick()
    assert (report.failed, report.not_ready) == (1, 0)
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.state, delivery.attempt_count) == ("pending", 1)


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_a_cancellation_behind_the_hook_is_never_swallowed(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_fact(factory)
    ready = [True]
    entered = asyncio.Event()

    async def hanging(payload: dict[str, object]) -> None:
        ready[0] = False
        entered.set()
        await asyncio.Event().wait()

    worker = BillingFactWorker(
        factory,
        transport=hanging,
        transport_ready=lambda: ready[0],
        config=_config(transport_timeout_seconds=30.0),
    )
    task = worker.start()
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        task.cancel()  # a real cancellation aimed at the loop task
        await asyncio.wait({task}, timeout=10)
        assert task.done(), "a genuine cancellation was swallowed"
        assert task.cancelled()
    finally:
        await worker.stop(grace=0.1)
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.state, delivery.attempt_count) == ("pending", 1)


async def test_a_readiness_hook_that_raises_or_is_not_true_is_not_ready(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    recorder = _LogRecorder()
    monkeypatch.setattr(worker_module, "logger", recorder)
    outbox_id = await _seed_fact(factory)
    clock = _Clock()

    def broken() -> bool:
        raise RuntimeError("readiness unreadable")

    for hook in (broken, lambda: 1, lambda: None):
        transport = _Transport()
        worker = BillingFactWorker(
            factory,
            transport=transport,
            transport_ready=hook,  # type: ignore[arg-type]
            config=_config(),
            now=clock,
        )
        assert (await worker.run_tick()).not_ready == 1
        assert transport.sent == []
        clock.advance(minutes=2)
    assert (await _delivery(factory, outbox_id)).attempt_count == 0
    assert (
        "billing fact consumer readiness unreadable",
        {"failure_class": "RuntimeError"},
    ) in recorder.errors


def test_a_readiness_hook_needs_a_transport(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    with pytest.raises(ValueError, match="^a readiness hook needs a transport$"):
        BillingFactWorker(
            factory, transport=None, transport_ready=lambda: True, config=_config()
        )


def test_not_ready_has_its_own_label_less_counter_outside_the_attempts() -> None:
    from src.observability.metric_dimensions import metric_dimension_contract

    contract = metric_dimension_contract("neurostack_billing_fact_consumer_not_ready")
    assert contract.labels == ()
    attempts = metric_dimension_contract("neurostack_billing_fact_publish_attempts")
    assert attempts.labels == ("outcome",)
    assert attempts.policies["outcome"].vocabulary == frozenset(
        {"presented", "failed", "exhausted"}
    )
    # Exported at zero from import, before any not-ready happened.
    assert (
        REGISTRY.get_sample_value("neurostack_billing_fact_consumer_not_ready_total")
        is not None
    )


# ---------------------------------------------------------------------------
# Two-phase attempt record (asaas-rollout-opening Group 2a)
# ---------------------------------------------------------------------------


class _ProcessDeath(BaseException):
    """Stands in for the process dying: no ``except Exception`` catches it."""


class _NotReady:
    """A transport whose consumer turns presentations away until ``ready``."""

    def __init__(self) -> None:
        self.ready = False
        self.sent: list[str] = []

    async def __call__(self, payload: dict[str, object]) -> None:
        self.sent.append(str(payload["event_id"]))
        if not self.ready:
            raise TransportNotReady("consumer readiness flipped off")


def _same_instant(stored: datetime | None, expected: datetime) -> bool:
    # SQLite drops the UTC annotation the producer stores (see ``_utc``).
    assert stored is not None
    return stored.replace(tzinfo=timezone.utc) == expected


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_the_attempt_is_committed_before_the_fact_is_presented(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_fact(factory)
    clock = _Clock()
    seen: list[tuple[str, int, datetime | None]] = []

    async def observing(payload: dict[str, object]) -> None:
        # Another connection, mid-presentation: only committed state is seen.
        delivery = await _delivery(factory, outbox_id)
        seen.append((delivery.state, delivery.attempt_count, delivery.last_attempt_at))

    worker = BillingFactWorker(
        factory, transport=observing, config=_config(), now=clock
    )
    assert (await worker.run_tick()).presented == 1
    ((state, count, last),) = seen
    assert (state, count) == ("pending", 1)
    assert _same_instant(last, clock.value)
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.state, delivery.attempt_count) == ("awaiting_ack", 1)


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_a_death_after_the_send_leaves_exactly_one_attempt_counted(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_fact(factory)
    clock = _Clock()
    sent: list[str] = []

    async def dies_after_send(payload: dict[str, object]) -> None:
        sent.append(str(payload["event_id"]))  # consumer-visible
        raise _ProcessDeath()

    dying = BillingFactWorker(
        factory, transport=dies_after_send, config=_config(), now=clock
    )
    with pytest.raises(_ProcessDeath):
        await dying.run_tick()
    assert sent == [str(outbox_id)]
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.state, delivery.attempt_count) == ("pending", 1)
    assert _same_instant(delivery.last_attempt_at, clock.value)

    # The restarted worker honours the backoff of the attempt that was sent.
    transport = _Transport()
    restarted = BillingFactWorker(
        factory, transport=transport, config=_config(), now=clock
    )
    assert (await restarted.run_tick()).presented == 0
    clock.advance(seconds=59)
    assert (await restarted.run_tick()).presented == 0
    clock.advance(seconds=2)
    assert (await restarted.run_tick()).presented == 1
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.state, delivery.attempt_count) == ("awaiting_ack", 2)


@pytest.mark.parametrize("timeout", [60.0, 61.0, 3600.0])
def test_a_transport_timeout_not_shorter_than_the_base_backoff_is_refused(
    timeout: float,
) -> None:
    assert FactRetryPolicy(max_attempts=3).base_backoff == timedelta(seconds=60)
    with pytest.raises(
        ValueError,
        match="^billing fact transport timeout must be shorter than the retry "
        "base backoff$",
    ):
        _config(transport_timeout_seconds=timeout)


def test_a_transport_timeout_shorter_than_the_base_backoff_is_accepted() -> None:
    config = _config(transport_timeout_seconds=59.999)
    assert timedelta(seconds=config.transport_timeout_seconds) < (
        config.retry_policy.base_backoff
    )
    default = BillingFactWorkerConfig(
        interval_seconds=30, batch_size=1, max_attempts=1, stale_after_seconds=60
    )
    assert default.transport_timeout_seconds == worker_module.TRANSPORT_TIMEOUT_SECONDS


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_publish_next_refuses_a_lease_shorter_than_its_timeout(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_fact(factory)
    transport = _Transport()
    short = FactRetryPolicy(max_attempts=3, base_backoff=timedelta(seconds=5))
    for timeout in (5.0, 6.0):
        with pytest.raises(
            ValueError, match="^timeout must be shorter than the retry base backoff$"
        ):
            async with factory() as session:
                await BillingFactPublisher(session).publish_next(
                    transport, policy=short, timeout=timeout
                )
    assert transport.sent == []
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.attempt_count, delivery.last_attempt_at) == (0, None)


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_publish_next_owns_its_commits_and_refuses_an_open_transaction(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_fact(factory)
    transport = _Transport()
    with pytest.raises(RuntimeError, match="^publish_next commits its own phases"):
        async with factory() as session, session.begin():
            await BillingFactPublisher(session).publish_next(
                transport, policy=FactRetryPolicy(max_attempts=3), timeout=1.0
            )
    assert transport.sent == []
    assert (await _delivery(factory, outbox_id)).attempt_count == 0


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_an_unready_consumer_spends_no_attempt_and_does_not_spin(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_fact(factory)
    clock, transport = _Clock(), _NotReady()
    # One attempt allowed: a spent not-ready attempt would exhaust the fact.
    worker = BillingFactWorker(
        factory, transport=transport, config=_config(max_attempts=1), now=clock
    )
    turned_away = clock.value
    first = await worker.run_tick()
    assert (first.not_ready, first.presented, first.failed) == (1, 0, 0)
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.state, delivery.attempt_count) == ("pending", 0)
    assert _same_instant(delivery.last_attempt_at, turned_away)  # kept

    for step in (0, 30, 29):  # up to 59 s later: not due, nothing presented
        clock.advance(seconds=step)
        report = await worker.run_tick()
        assert (report.not_ready, report.presented, report.exhausted) == (0, 0, 0)
    assert transport.sent == [str(outbox_id)]

    clock.advance(seconds=2)  # base backoff passed: due again, still unspent
    transport.ready = True
    due = await worker.run_tick()
    assert (due.presented, due.exhausted) == (1, 0)
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.state, delivery.attempt_count) == ("awaiting_ack", 1)
    assert transport.sent == [str(outbox_id)] * 2


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_a_not_ready_re_presentation_keeps_its_state_and_its_backoff(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_fact(factory)
    clock, transport = _Clock(), _NotReady()
    transport.ready = True
    worker = BillingFactWorker(
        factory, transport=transport, config=_config(), now=clock
    )
    assert (await worker.run_tick()).presented == 1  # attempt 1, ack lost
    clock.advance(seconds=61)
    transport.ready = False
    turned_away = clock.value
    assert (await worker.run_tick()).not_ready == 1
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.state, delivery.attempt_count) == ("awaiting_ack", 1)
    assert _same_instant(delivery.last_attempt_at, turned_away)
    clock.advance(seconds=59)
    assert (await worker.run_tick()).not_ready == 0  # backoff(1) not over
    clock.advance(seconds=2)
    transport.ready = True
    assert (await worker.run_tick()).presented == 1
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.state, delivery.attempt_count) == ("awaiting_ack", 2)
    assert len(transport.sent) == 3


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_an_unready_consumer_ends_the_batch_instead_of_touching_every_fact(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    ids = [await _seed_fact(factory) for _ in range(3)]
    clock, transport = _Clock(), _NotReady()
    worker = BillingFactWorker(
        factory, transport=transport, config=_config(batch_size=10), now=clock
    )
    report = await worker.run_tick()
    assert (report.outcome, report.not_ready, report.presented) == ("completed", 1, 0)
    assert len(transport.sent) == 1
    untouched = [
        d
        for d in [await _delivery(factory, i) for i in ids]
        if d.last_attempt_at is None
    ]
    assert len(untouched) == 2


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_a_not_ready_presentation_is_logged_but_never_counted_as_an_attempt(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_fact(factory)
    recorder = _LogRecorder()
    monkeypatch.setattr(worker_module, "logger", recorder)
    outcomes = ("presented", "failed", "exhausted")
    before = {
        outcome: _sample(
            "neurostack_billing_fact_publish_attempts_total", outcome=outcome
        )
        for outcome in outcomes
    }
    not_ready = _sample("neurostack_billing_fact_consumer_not_ready_total")
    completed = _sample(
        "neurostack_billing_fact_worker_ticks_total", outcome="completed"
    )
    worker = BillingFactWorker(
        factory, transport=_NotReady(), config=_config(), now=_Clock()
    )
    assert (await worker.run_tick()).not_ready == 1
    assert {
        outcome: _sample(
            "neurostack_billing_fact_publish_attempts_total", outcome=outcome
        )
        for outcome in outcomes
    } == before
    # Its own bounded signal (Group 2c), while the tick itself completed.
    assert _sample("neurostack_billing_fact_consumer_not_ready_total") == (
        not_ready + 1
    )
    assert _sample(
        "neurostack_billing_fact_worker_ticks_total", outcome="completed"
    ) == (completed + 1)
    assert ("billing fact consumer not ready", {"attempt_count": 0}) in (
        recorder.warnings
    )
    # Every emitted label value stays inside the metric dimension contract.
    assert all(
        event != "billing fact metric emission failed"
        for event, _fields in recorder.warnings
    )


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_a_running_loop_with_an_unready_consumer_does_not_spin(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_fact(factory)
    transport = _NotReady()
    worker = BillingFactWorker(
        factory,
        transport=transport,
        config=_config(interval_seconds=0.01),
        monotonic=lambda: 0.0,
    )
    task = worker.start()

    async def turned_away() -> bool:
        return bool(transport.sent) or task.done()

    await _wait_until(turned_away, "the unready consumer was never called")
    await asyncio.sleep(0.3)  # dozens of 10 ms ticks
    await worker.stop()
    assert task.done() and not task.cancelled()
    assert transport.sent == [str(outbox_id)]
    delivery = await _delivery(factory, outbox_id)
    assert (delivery.state, delivery.attempt_count) == ("pending", 0)
    assert delivery.last_attempt_at is not None


@pytest.mark.usefixtures("presentation_preconditions_waived")
async def test_a_zero_count_with_a_last_attempt_waits_the_base_backoff(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _seed_fact(factory)
    clock = _Clock()
    async with factory() as session:
        await session.execute(
            update(BillingFactDelivery)
            .where(BillingFactDelivery.outbox_event_id == outbox_id)
            .values(attempt_count=0, last_attempt_at=clock.value)
        )
        await session.commit()
    policy = FactRetryPolicy(max_attempts=3)
    transport = _Transport()

    async def publish() -> Any:
        async with factory() as session:
            return await BillingFactPublisher(session, now=clock).publish_next(
                transport, policy=policy, timeout=1.0
            )

    assert await publish() is None
    clock.advance(seconds=59)
    assert await publish() is None
    clock.advance(seconds=2)
    attempt = await publish()
    assert attempt is not None and (attempt.outcome, attempt.attempt_count) == (
        "presented",
        1,
    )


@pytest.mark.usefixtures("presentation_preconditions_waived")
@pytest.mark.parametrize("ready", [True, False])
@pytest.mark.parametrize(
    "taken_over_count",
    [
        pytest.param(2, id="next-count"),
        # The SAME count as this lease, only ``last_attempt_at`` differs: the
        # other worker's lease (2) was given back by its not-ready outcome,
        # or a requeue (0, NULL) was followed by a fresh lease (1).
        pytest.param(1, id="same-count"),
    ],
)
async def test_phase_two_never_overwrites_a_lease_another_worker_took_over(
    factory: async_sessionmaker[AsyncSession], ready: bool, taken_over_count: int
) -> None:
    outbox_id = await _seed_fact(factory)
    clock = _Clock()
    taken_over = clock.value + timedelta(seconds=61)

    async def overrun(payload: dict[str, object]) -> None:
        # This presentation outlived its lease: another worker re-leased it.
        async with factory() as session:
            await session.execute(
                update(BillingFactDelivery)
                .where(BillingFactDelivery.outbox_event_id == outbox_id)
                .values(
                    attempt_count=taken_over_count,
                    last_attempt_at=taken_over,
                    state="pending",
                )
            )
            await session.commit()
        if not ready:
            raise TransportNotReady("consumer readiness flipped off")

    async with factory() as session:
        attempt = await BillingFactPublisher(session, now=clock).publish_next(
            overrun, policy=FactRetryPolicy(max_attempts=3), timeout=1.0
        )
        await session.commit()  # whatever publish_next left pending lands too
    assert attempt is not None
    assert attempt.outcome == ("presented" if ready else "not_ready")
    delivery = await _delivery(factory, outbox_id)
    # Neither its state nor a compensation lands on the other worker's lease.
    assert (delivery.state, delivery.attempt_count) == ("pending", taken_over_count)
    assert _same_instant(delivery.last_attempt_at, taken_over)


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
async def test_a_retry_is_due_exactly_when_its_backoff_has_elapsed(
    factory: async_sessionmaker[AsyncSession],
    attempt_count: int,
    backoff: timedelta,
    early: timedelta,
    due: bool,
) -> None:
    outbox_id = await _seed_fact(factory)
    clock = _Clock()
    policy = FactRetryPolicy(max_attempts=3)
    assert policy.backoff(max(attempt_count, 1)) == backoff
    async with factory() as session:
        await session.execute(
            update(BillingFactDelivery)
            .where(BillingFactDelivery.outbox_event_id == outbox_id)
            .values(
                attempt_count=attempt_count,
                last_attempt_at=clock.value - backoff + early,
            )
        )
        await session.commit()
    transport = _Transport()
    async with factory() as session:
        attempt = await BillingFactPublisher(session, now=clock).publish_next(
            transport, policy=policy, timeout=1.0
        )
    if due:
        assert attempt is not None
        assert (attempt.outcome, attempt.attempt_count) == (
            "presented",
            attempt_count + 1,
        )
        assert transport.sent == [str(outbox_id)]
    else:
        assert attempt is None
        assert transport.sent == []
        delivery = await _delivery(factory, outbox_id)
        assert delivery.attempt_count == attempt_count


# ---------------------------------------------------------------------------
# Static boundaries: rules, contracts and no credit dependency
# ---------------------------------------------------------------------------


def test_fact_alert_rules_are_exact_and_mirrored() -> None:
    assert PRIMARY_RULES.read_bytes() == MIRROR_RULES.read_bytes()
    rules = _rules()
    assert rules["BillingFactDeliveryStale"]["expr"] == (
        'max(neurostack_billing_fact_backlog{condition="stale"}) > 0'
    )
    assert rules["BillingFactDeliveryBlocked"]["expr"] == (
        'max(neurostack_billing_fact_backlog{condition="blocked"}) > 0'
    )
    assert rules["BillingFactWorkerStalled"]["expr"] == (
        "max(neurostack_billing_fact_worker_last_tick_timestamp_seconds) > 0 and "
        "(time() - max(neurostack_billing_fact_worker_last_tick_timestamp_seconds))"
        " > 600"
    )
    for name, severity in (
        ("BillingFactDeliveryStale", "critical"),
        ("BillingFactDeliveryBlocked", "critical"),
        ("BillingFactWorkerStalled", "warning"),
    ):
        assert rules[name]["labels"] == {"severity": severity, "team": "backend"}
        assert rules[name]["annotations"]["runbook_url"] == (
            "https://docs.neurostack.io/runbooks/asaas-billing-reconciliation"
        )
    # The stall window must cover at least two of the slowest allowed ticks.
    interval = Settings.model_fields["billing_fact_worker_interval_seconds"]
    assert any(
        getattr(meta, "le", None) is not None and 2 * meta.le <= 600
        for meta in interval.metadata
    )


@pytest.mark.parametrize(
    ("path", "forbidden"),
    [
        # The new worker module meets the full Group 4 architectural gate.
        (
            "src/services/billing_fact_worker.py",
            r"credit_(ledger|balance|grant)|CreditLedger|dead.?letter",
        ),
        # The producer modules legitimately read the aggregates'
        # ``credit_grant_mc`` column (pre-existing); ledger terms stay absent.
        (
            "src/services/billing_outbox.py",
            r"credit_(ledger|balance)|CreditLedger|dead.?letter",
        ),
        (
            "src/services/billing_webhook.py",
            r"credit_(ledger|balance)|CreditLedger|dead.?letter",
        ),
    ],
)
def test_billing_fact_delivery_never_depends_on_credit_or_ledger_code(
    path: str, forbidden: str
) -> None:
    source = (ROOT / path).read_text(encoding="utf-8")
    assert re.search(forbidden, source) is None
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {name for name in imported if re.search("credit|ledger", name)}


def test_graceful_shutdown_always_outwaits_one_presentation() -> None:
    # A lifespan stop never has to cancel a presentation that is still within
    # its own timeout, so a healthy consumer is never cut off mid-handoff.
    assert (
        worker_module.TRANSPORT_TIMEOUT_SECONDS < worker_module.SHUTDOWN_GRACE_SECONDS
    )
