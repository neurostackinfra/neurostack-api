"""Real PostgreSQL proofs for the operator requeue of exhausted facts.

Deliveries reach ``blocked`` only the way production gets them there: the
retry cap through :class:`BillingFactWorker` with a failing consumer, the
ack mismatch through :class:`BillingFactReconciler`.  Facts come from
``BillingFactProducer`` on migration-head schema (triggers, CHECKs,
immutability) in a disposable child database.  No network: the consumer is
an in-memory fake.  ``tests/ops/test_billing_fact_requeue.py`` covers the
output contract on SQLite.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool
from sqlalchemy.util import await_only

from src.ops import billing_fact_requeue as requeue
from src.services import billing_outbox as outbox_module
from src.services.billing_fact_worker import BillingFactWorker, BillingFactWorkerConfig
from src.services.billing_outbox import (
    ACK_MISSING_DETAIL,
    ACK_OVERDUE_DETAIL,
    ACK_SCHEMA_VERSION,
    RETRY_EXHAUSTED_DETAIL,
    BillingFactReconciler,
)
from tests.integration.test_asaas_billing_postgres import (
    _CHILD_PATTERN,
    _create_child,
    _drop_child,
    _run_alembic,
    _validated_admin_url,
)
from tests.integration.test_billing_fact_worker_postgres import _seed_fact

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
RUNBOOK = ROOT / "docs" / "runbooks" / "asaas-billing-reconciliation.md"


def _runbook_sql(heading: str) -> str:
    """The one SQL block of a runbook subsection (the doc stays executable)."""

    section = RUNBOOK.read_text(encoding="utf-8").split(heading, 1)[1]
    (sql,) = re.findall(r"```sql\n(.*?)```", section.split("\n### ", 1)[0], re.S)
    return sql


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
async def clean(template: _Template) -> AsyncIterator[_Harness]:
    """A fresh migrated database per proof."""

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


@pytest.fixture(autouse=True)
def presentation_preconditions_waived(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit test-only waiver: the worker must present to exhaust a fact."""

    monkeypatch.setattr(outbox_module, "PRESENTATION_PRECONDITIONS_OPEN", False)


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
        "max_attempts": 2,
        "stale_after_seconds": 900.0,
        "transport_timeout_seconds": 5.0,
    }
    values.update(overrides)
    return BillingFactWorkerConfig(**values)


async def _rows(harness: _Harness, sql: str, **params: Any) -> list[Any]:
    async with harness.engine.connect() as connection:
        return list((await connection.execute(text(sql), params)).all())


async def _delivery(harness: _Harness, outbox_id: UUID) -> tuple[Any, ...]:
    (row,) = await _rows(
        harness,
        "SELECT state, attempt_count, last_attempt_at FROM billing_fact_deliveries "
        "WHERE outbox_event_id = :id",
        id=outbox_id,
    )
    return tuple(row)


async def _snapshot(harness: _Harness) -> tuple[list[Any], list[Any]]:
    deliveries = await _rows(
        harness,
        "SELECT outbox_event_id, state, attempt_count, last_attempt_at, updated_at "
        "FROM billing_fact_deliveries ORDER BY outbox_event_id",
    )
    alerts = await _rows(
        harness,
        "SELECT id, outbox_event_id, kind, detail_sha256, observed_at "
        "FROM billing_fact_reconciliation_alerts ORDER BY id",
    )
    return [tuple(row) for row in deliveries], [tuple(row) for row in alerts]


async def _requeue_rows(harness: _Harness) -> list[UUID]:
    rows = await _rows(
        harness,
        "SELECT outbox_event_id FROM billing_fact_reconciliation_alerts "
        "WHERE kind = 'backlog' AND detail_sha256 = :d ORDER BY observed_at",
        d=requeue.detail_digest(requeue.REQUEUE_DETAIL),
    )
    return [row[0] for row in rows]


async def _ack(
    harness: _Harness,
    outbox_id: UUID,
    *,
    outcome: str = "accepted",
    fact_sha256: str | None = None,
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
                SELECT :id, outbox.id, outbox.id, :schema, :outcome, :inbox,
                       COALESCE(:fact_sha256, outbox.payload_sha256),
                       handoff.bundle_digest, handoff.deployment_digest,
                       :effects, now()
                FROM billing_outbox outbox
                JOIN billing_fact_handoffs handoff
                  ON handoff.outbox_event_id = outbox.id
                WHERE outbox.id = :outbox_id
            """),
            {
                "id": uuid4(),
                "schema": ACK_SCHEMA_VERSION,
                "outcome": outcome,
                "inbox": uuid4(),
                "effects": "e" * 64,
                "outbox_id": outbox_id,
                "fact_sha256": fact_sha256,
            },
        )


async def _reconcile(harness: _Harness) -> None:
    async with harness.sessions() as session, session.begin():
        await BillingFactReconciler(session).reconcile()


async def _exhaust(harness: _Harness, outbox_id: UUID, clock: _Clock) -> None:
    """The real worker against a consumer that always fails, until blocked."""

    async def failing(payload: dict[str, object]) -> None:
        raise ConnectionError("consumer unavailable")

    engine, sessions = harness.factory()
    worker = BillingFactWorker(sessions, transport=failing, config=_config(), now=clock)
    try:
        for _ in range(10):
            await worker.run_tick()
            clock.advance(hours=1)
            if (await _delivery(harness, outbox_id))[0] == "blocked":
                break
    finally:
        await engine.dispose()
    assert (await _delivery(harness, outbox_id))[0] == "blocked"
    (evidence,) = await _rows(
        harness,
        "SELECT count(*) FROM billing_fact_reconciliation_alerts "
        "WHERE outbox_event_id = :id AND kind = 'backlog' AND detail_sha256 = :d",
        id=outbox_id,
        d=requeue.detail_digest(RETRY_EXHAUSTED_DETAIL),
    )
    assert evidence[0] == 1


async def _present_due(harness: _Harness, clock: _Clock) -> list[str]:
    """One tick with a consumer that accepts the call; returns what it saw."""

    seen: list[str] = []

    async def recording(payload: dict[str, object]) -> None:
        seen.append(str(payload["event_id"]))

    engine, sessions = harness.factory()
    worker = BillingFactWorker(
        sessions, transport=recording, config=_config(), now=clock
    )
    try:
        await worker.run_tick()
    finally:
        await engine.dispose()
    return seen


@dataclass(frozen=True)
class _World:
    exhausted: UUID
    divergent_ack: UUID
    rejected_ack: UUID
    healthy_pending: UUID
    healthy_acknowledged: UUID


async def _world(harness: _Harness, clock: _Clock) -> _World:
    exhausted, divergent = [await _seed_fact(harness) for _ in range(2)]
    await _exhaust(harness, exhausted, clock)
    # The divergent fact exhausted its retries too, then a late ack with a
    # wrong fact digest arrived: only "any ack" now keeps it from a requeue.
    assert (await _delivery(harness, divergent))[0] == "blocked"
    rejected, pending, acknowledged = [await _seed_fact(harness) for _ in range(3)]
    await _ack(harness, divergent, fact_sha256="0" * 64)
    await _ack(harness, rejected, outcome="rejected")
    await _ack(harness, acknowledged)
    await _reconcile(harness)
    assert (await _delivery(harness, divergent))[0] == "blocked"
    assert (await _delivery(harness, rejected))[0] == "blocked"
    assert (await _delivery(harness, pending))[0] == "pending"
    assert (await _delivery(harness, acknowledged))[0] == "acknowledged"
    return _World(exhausted, divergent, rejected, pending, acknowledged)


def _counts(**values: int) -> dict[str, int]:
    return {**dict.fromkeys(requeue.COUNT_NAMES, 0), **values}


# ---------------------------------------------------------------------------
# Selection and idempotency
# ---------------------------------------------------------------------------


async def test_only_the_exhausted_delivery_is_requeued_and_a_rerun_is_inert(
    clean: _Harness,
) -> None:
    clock = _Clock()
    world = await _world(clean, clock)
    before = {
        outbox_id: await _delivery(clean, outbox_id)
        for outbox_id in (
            world.divergent_ack,
            world.rejected_ack,
            world.healthy_pending,
            world.healthy_acknowledged,
        )
    }

    before_plan = await _snapshot(clean)
    planned = await requeue.run("plan", clean.sessions)
    assert await _snapshot(clean) == before_plan  # the plan writes nothing
    first = await requeue.run("requeue", clean.sessions)
    after_first = await _snapshot(clean)
    second = await requeue.run("requeue", clean.sessions)

    assert planned.counts == _counts(
        eligible_before=1, eligible_remaining=1, blocked_with_ack=2
    )
    assert first.counts == _counts(eligible_before=1, requeued=1, blocked_with_ack=2)
    assert (first.verdict, first.action) == ("investigate", "requeued")
    assert first.reasons == ("blocked_with_ack",)
    assert await _delivery(clean, world.exhausted) == ("pending", 0, None)
    for outbox_id, state in before.items():
        assert await _delivery(clean, outbox_id) == state
    assert await _requeue_rows(clean) == [world.exhausted]
    # Idempotent: the second run neither moves a row nor adds evidence.
    assert second.counts == _counts(blocked_with_ack=2)
    assert second.action == "none"
    assert await _snapshot(clean) == after_first

    # The requeued fact is claimable again; the ack-blocked ones are not.
    presented = await _present_due(clean, clock)
    assert str(world.exhausted) in presented
    assert str(world.divergent_ack) not in presented
    assert str(world.rejected_ack) not in presented
    assert (await _delivery(clean, world.exhausted))[0] == "awaiting_ack"
    await _ack(clean, world.exhausted)
    await _reconcile(clean)
    assert (await _delivery(clean, world.exhausted))[0] == "acknowledged"


async def test_the_plan_runs_in_a_read_only_transaction(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    outbox_id = await _seed_fact(clean)
    await _exhaust(clean, outbox_id, clock)
    counted = requeue._count

    async def writing_count(session: AsyncSession, condition: Any) -> int:
        await session.execute(
            text("UPDATE billing_fact_deliveries SET attempt_count = attempt_count")
        )
        return await counted(session, condition)

    monkeypatch.setattr(requeue, "_count", writing_count)

    with pytest.raises(DBAPIError, match="read-only transaction"):
        await requeue.run("plan", clean.sessions)
    result = await requeue.run("requeue", clean.sessions)

    assert result.counts["requeued"] == 1


async def test_a_late_ack_after_exhaustion_forbids_the_requeue(
    clean: _Harness,
) -> None:
    """Any ack keeps ``_due_claim`` away, even a valid one not yet reconciled."""

    clock = _Clock()
    outbox_id = await _seed_fact(clean)
    await _exhaust(clean, outbox_id, clock)
    await _ack(clean, outbox_id)
    blocked = await _delivery(clean, outbox_id)

    result = await requeue.run("requeue", clean.sessions)

    assert result.counts == _counts(blocked_with_ack=1)
    assert await _delivery(clean, outbox_id) == blocked
    assert await _requeue_rows(clean) == []
    await _reconcile(clean)  # the late valid ack converges it instead
    assert (await _delivery(clean, outbox_id))[0] == "acknowledged"


async def test_an_ack_committed_between_the_candidate_read_and_the_update_wins(
    clean: _Harness,
) -> None:
    """The ``UPDATE`` re-checks the conditions in its own, fresher snapshot.

    A consumer ack does not touch the delivery row, so the row lock alone
    cannot see it: only the re-check keeps the delivery ``blocked``.  The ack
    is committed on another backend right before the ``UPDATE`` executes.
    """

    clock = _Clock()
    outbox_id = await _seed_fact(clean)
    await _exhaust(clean, outbox_id, clock)
    fired: list[str] = []

    def ack_before_update(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().startswith("UPDATE billing_fact_deliveries") and (
            not fired
        ):
            fired.append(statement)
            await_only(_ack(clean, outbox_id))

    event.listen(clean.engine.sync_engine, "before_cursor_execute", ack_before_update)
    try:
        result = await requeue.run("requeue", clean.sessions)
    finally:
        event.remove(
            clean.engine.sync_engine, "before_cursor_execute", ack_before_update
        )

    assert fired, "the UPDATE never ran"
    assert result.counts == _counts(eligible_before=1, blocked_with_ack=1)
    assert (await _delivery(clean, outbox_id))[0] == "blocked"
    assert await _requeue_rows(clean) == []


async def test_a_blocked_delivery_without_exhaustion_evidence_is_refused(
    clean: _Harness,
) -> None:
    outbox_id = await _seed_fact(clean)
    async with clean.engine.begin() as connection:  # an unknown writer
        await connection.execute(
            text(
                "UPDATE billing_fact_deliveries SET state = 'blocked', "
                "attempt_count = 2 WHERE outbox_event_id = :id"
            ),
            {"id": outbox_id},
        )

    result = await requeue.run("requeue", clean.sessions)

    assert result.counts == _counts(blocked_without_evidence=1)
    assert result.verdict == "investigate"
    assert (await _delivery(clean, outbox_id))[0] == "blocked"


async def test_the_runbook_listing_shows_every_blocked_delivery(
    clean: _Harness,
) -> None:
    """The runbook's own SQL, read from the doc, on all three blocked kinds.

    The delivery without any alert row is the one the alert-driven
    classification query cannot show.  A fourth, blocked by an unknown
    writer with zero attempts and every other ``backlog`` detail, keeps
    ``exhausted`` tied to the ``retry cap exhausted`` row alone.  A
    ``rejected`` ack still counts as an ack, and a healthy ``pending``
    delivery must not be listed.
    """

    clock = _Clock()
    exhausted, divergent = [await _seed_fact(clean) for _ in range(2)]
    await _exhaust(clean, exhausted, clock)  # the tick exhausts both
    rejected, healthy = [await _seed_fact(clean) for _ in range(2)]
    await _ack(clean, divergent, fact_sha256="0" * 64)
    await _ack(clean, rejected, outcome="rejected")
    await _reconcile(clean)
    assert (await _delivery(clean, healthy))[0] == "pending"
    no_evidence, other_backlog = [await _seed_fact(clean) for _ in range(2)]
    async with clean.engine.begin() as connection:  # an unknown writer
        for outbox_id, attempts in ((no_evidence, 2), (other_backlog, 0)):
            await connection.execute(
                text(
                    "UPDATE billing_fact_deliveries SET state = 'blocked', "
                    "attempt_count = :n, updated_at = clock_timestamp() "
                    "WHERE outbox_event_id = :id"
                ),
                {"id": outbox_id, "n": attempts},
            )
        for detail in (ACK_MISSING_DETAIL, ACK_OVERDUE_DETAIL, requeue.REQUEUE_DETAIL):
            await connection.execute(
                text(
                    "INSERT INTO billing_fact_reconciliation_alerts "
                    "(id, outbox_event_id, kind, detail_sha256, observed_at) "
                    "VALUES (:id, :outbox_id, 'backlog', :d, now())"
                ),
                {
                    "id": uuid4(),
                    "outbox_id": other_backlog,
                    "d": requeue.detail_digest(detail),
                },
            )
    (alerts,) = await _rows(
        clean,
        "SELECT count(*) FROM billing_fact_reconciliation_alerts "
        "WHERE outbox_event_id = :id",
        id=no_evidence,
    )
    assert alerts[0] == 0
    assert (await requeue.run("plan", clean.sessions)).counts == _counts(
        eligible_before=1,
        eligible_remaining=1,
        blocked_with_ack=2,
        blocked_without_evidence=2,
    )

    listing = _runbook_sql("### Listing every `blocked` delivery")
    rows = await _rows(clean, listing)

    assert {row.outbox_event_id: (row.has_ack, row.exhausted) for row in rows} == {
        exhausted: (False, True),
        divergent: (True, True),
        rejected: (True, False),
        no_evidence: (False, False),
        other_backlog: (False, False),
    }
    expected = await _rows(
        clean,
        "SELECT outbox_event_id, attempt_count, updated_at "
        "FROM billing_fact_deliveries WHERE state = 'blocked'",
    )
    # The worker stamps both exhausted rows from one fake clock: ties allowed.
    assert sorted(tuple(row[:3]) for row in rows) == sorted(
        tuple(row) for row in expected
    )
    assert [row.updated_at for row in rows] == sorted(row.updated_at for row in rows)


@pytest.mark.parametrize("command", ["plan", "requeue"])
async def test_every_run_bounds_its_lock_and_statement_waits(
    clean: _Harness, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """The settings are read inside the run's own transaction."""

    clock = _Clock()
    await _exhaust(clean, await _seed_fact(clean), clock)
    counted = requeue._count
    seen: list[tuple[str, str]] = []

    async def observing_count(session: AsyncSession, condition: Any) -> int:
        lock = await session.scalar(text("SHOW lock_timeout"))
        statement = await session.scalar(text("SHOW statement_timeout"))
        seen.append((str(lock), str(statement)))
        return await counted(session, condition)

    monkeypatch.setattr(requeue, "_count", observing_count)

    await requeue.run(command, clean.sessions)

    assert seen
    assert set(seen) == {("5s", "1min")}


# ---------------------------------------------------------------------------
# Concurrency: row locks, never a double reopen
# ---------------------------------------------------------------------------


async def test_a_locked_delivery_is_skipped_and_left_for_the_next_run(
    clean: _Harness,
) -> None:
    clock = _Clock()
    outbox_id = await _seed_fact(clean)
    await _exhaust(clean, outbox_id, clock)

    holder_engine, _sessions = clean.factory()
    try:
        async with holder_engine.connect() as holder:
            transaction = await holder.begin()
            await holder.execute(
                text(
                    "SELECT 1 FROM billing_fact_deliveries "
                    "WHERE outbox_event_id = :id FOR UPDATE"
                ),
                {"id": outbox_id},
            )
            held = await asyncio.wait_for(
                requeue.run("requeue", clean.sessions), timeout=30
            )
            await transaction.rollback()
    finally:
        await holder_engine.dispose()
    released = await requeue.run("requeue", clean.sessions)

    assert held.counts == _counts(eligible_before=1, eligible_remaining=1)
    assert (held.verdict, held.exit_code) == ("pending", 10)
    assert released.counts == _counts(eligible_before=1, requeued=1)
    assert await _requeue_rows(clean) == [outbox_id]


async def test_concurrent_runs_reopen_each_delivery_exactly_once(
    clean: _Harness,
) -> None:
    clock = _Clock()
    ids = [await _seed_fact(clean) for _ in range(3)]
    for outbox_id in ids:
        await _exhaust(clean, outbox_id, clock)
    factories: list[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]] = [
        clean.factory() for _ in range(4)
    ]
    try:
        results = await asyncio.gather(
            *(requeue.run("requeue", sessions) for _engine, sessions in factories)
        )
    finally:
        for engine, _sessions in factories:
            await engine.dispose()

    assert sum(result.counts["requeued"] for result in results) == 3
    assert sorted(await _requeue_rows(clean)) == sorted(ids)
    for outbox_id in ids:
        assert (await _delivery(clean, outbox_id))[:2] == ("pending", 0)


# ---------------------------------------------------------------------------
# The production entry point
# ---------------------------------------------------------------------------


async def _module(
    harness: _Harness, tmp_path: Path, *argv: str
) -> tuple[int, dict[str, Any], str]:
    """``python -m src.ops.billing_fact_requeue`` with DATABASE_URL only."""

    workdir = tmp_path / f"app-{uuid4().hex[:8]}"
    workdir.mkdir()
    (workdir / "src").symlink_to(ROOT / "src", target_is_directory=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "ENVIRONMENT": "staging",
        "SECRET_KEY": "s" * 48,
        "DATABASE_URL": harness.url.render_as_string(hide_password=False),
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "src.ops.billing_fact_requeue",
        *argv,
        cwd=workdir,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(process.communicate(), timeout=300)
    (line,) = out.decode().splitlines()
    assert len(err.decode().splitlines()) == 1, err.decode()[-2000:]
    assert process.returncode is not None
    return process.returncode, json.loads(line), out.decode() + err.decode()


async def test_the_module_entry_point_plans_then_requeues_on_postgres(
    clean: _Harness, tmp_path: Path
) -> None:
    clock = _Clock()
    world = await _world(clean, clock)
    database = str(clean.url.database)

    plan_code, plan, plan_text = await _module(clean, tmp_path, "plan")
    run_code, run, run_text = await _module(clean, tmp_path, "requeue", "--limit", "5")
    again_code, again, _again_text = await _module(clean, tmp_path, "requeue")

    assert (plan_code, plan["verdict"]) == (20, "investigate")
    assert plan["counts"] == _counts(
        eligible_before=1, eligible_remaining=1, blocked_with_ack=2
    )
    assert (run_code, run["action"], run["limit"]) == (20, "requeued", 5)
    assert run["counts"]["requeued"] == 1
    assert (again_code, again["action"]) == (20, "none")
    assert await _requeue_rows(clean) == [world.exhausted]
    for text_out in (plan_text, run_text):
        assert database not in text_out
        assert str(world.exhausted) not in text_out
