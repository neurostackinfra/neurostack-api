"""Observational metering on real PostgreSQL (neurostack-credit-system 3a).

What SQLite cannot show, against migration 038 at head:

* the writer's budget is ONE ``asyncio.timeout`` around the connection
  checkout, every statement and the commit: under lock contention on the
  tenant row the write is dropped inside that total, logged, and a
  connection cancelled mid-statement is invalidated (its backend is gone),
  never pooled; with the pool exhausted (``pool_size=5, max_overflow=0``) it
  is dropped at once, without waiting for a connection;
* ``lock_timeout`` / ``statement_timeout`` are set LOCAL to the writer's
  transaction (server backstops only): a pooled connection answers ``SHOW``
  with the server defaults afterwards;
* a credit-side failure -- a claim CHECK violation, an append-only trigger,
  a lock timeout, the claim's connection lost -- writes nothing and only
  logs, and through the real app (PostgreSQL pool, lifespan, middleware) the
  answer, its status and the stored events stay those of the switch off;
* the request holds at most one pooled connection while the writer runs:
  its own session and the main path's sessions ended their transactions;
* claims deduplicate per tenant under concurrency, for a standalone tenant
  and across a reparent (the tenant/builder trigger agrees), and nothing is
  written once ``credit_enforcement_at`` is set.

Group 3b-1, the enforced ``POST /v1/events`` through the real app on a
database with ``credit_enforcement_at`` set (LOW-6: the fences and the
concurrency are PostgreSQL-only):

* one decision, one trace, committed with its decision; ``lock_timeout`` of
  15 s only inside the enforced phase 1 and gone with its commit; the LLM
  never inside a transaction; the phase 1 handoff charged and linked, its
  creation webhook announced once, after the final transaction;
* the fences (HIGH-1): a phase 1 stalled past its lease loses its marker to
  a takeover and rolls everything back; a retry waits on ``FOR SHARE`` for a
  final transaction that passed its check and replays it, never
  ``event_failed``; a final check after the lease writes nothing; two
  takeovers and two first requests of one key run phase 1 once; a lock
  timeout releases the lease for an immediate takeover;
* the lock order (MEDIUM-1): a final transaction and ``DELETE
  /clinics/{clinic_id}`` finish without deadlock (the tenant ``FOR SHARE``
  first), and a phase 1 holding a session never blocks the final
  transaction of another session of the same builder.

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import asyncio
import functools
import json
import os
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.interaction_context import InteractionOrigin
from src.models.builder import Builder
from src.models.credit import CreditIngressClaim, CreditObservationalEffect
from src.models.session import Session
from src.models.tenant import Tenant
from src.repositories.credit import CreditRepository
from src.services import credit_metering
from src.services.credit_metering import (
    OBSERVATIONAL_WRITE_BUDGET_SECONDS,
    ObservationalIngress,
    record_observational_trace,
    trace_effect_id,
)
from src.services.credit_principal import credit_identity
from src.services.credit_switch import CreditSwitch
from tests.integration.test_asaas_billing_postgres import _validated_admin_url
from tests.integration.test_migration_036_postgres import _Cluster, _migrate

pytestmark = pytest.mark.integration

# Drop decisions must land inside the budget; the margin absorbs scheduling.
_MARGIN = 0.35


@dataclass(frozen=True)
class _PG:
    cluster: _Cluster
    url: URL


@pytest.fixture(scope="module")
async def pg() -> AsyncIterator[_PG]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    cluster = _Cluster(admin_url=admin_url, admin=admin, created=[])
    try:
        name = await cluster.fresh()
        await _migrate(cluster.url(name), "upgrade", "head")
        yield _PG(cluster=cluster, url=cluster.url(name))
    finally:
        for created in list(reversed(cluster.created)):
            await cluster.drop(created)
        await admin.dispose()


@dataclass
class _Pool:
    """The process pool's shape (``pool_size=5``, ``max_overflow=0``)."""

    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


@pytest.fixture
async def pool(pg: _PG) -> AsyncIterator[_Pool]:
    engine = create_async_engine(pg.url, pool_size=5, max_overflow=0, pool_timeout=30)
    try:
        yield _Pool(
            engine=engine,
            sessions=async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
            ),
        )
    finally:
        await engine.dispose()


@pytest.fixture
async def direct(pg: _PG) -> AsyncIterator[AsyncEngine]:
    """Connections outside the pool under test (contention, readbacks)."""

    engine = create_async_engine(pg.url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class _Tenant:
    tenant_id: UUID
    session_id: UUID
    builder_id: UUID | None


async def _tenant(direct: AsyncEngine, *, standalone: bool = False) -> _Tenant:
    builder_id = None if standalone else uuid4()
    tenant_id, session_id = uuid4(), uuid4()
    async with AsyncSession(direct) as session, session.begin():
        if builder_id is not None:
            session.add(Builder(id=builder_id, name="Metering builder"))
            await session.flush()
        session.add(
            Tenant(
                id=tenant_id,
                name="Metering clinic",
                slug=f"metering-{tenant_id.hex}",
                api_key_hash=f"unused-{tenant_id.hex}",
                plan_type="clinic",
                builder_id=builder_id,
                config={},
            )
        )
        await session.flush()
        session.add(
            Session(
                id=session_id,
                tenant_id=tenant_id,
                channel="test",
                cognitive_state={},
                session_metadata={},
                message_count=2,
            )
        )
    return _Tenant(tenant_id, session_id, builder_id)


def _ingress(
    seeded: _Tenant, *, key: str | None = "key-1", **changes: Any
) -> ObservationalIngress:
    base = ObservationalIngress(
        source="events",
        tenant_id=seeded.tenant_id,
        session_id=seeded.session_id,
        origin=InteractionOrigin.PRODUCTION,
        identity=credit_identity("api_key", seeded.tenant_id),
        payload_sha256="a" * 64,
        idempotency_key=key,
    )
    return replace(base, **changes)


async def _rows(direct: AsyncEngine, tenant_id: UUID) -> tuple[list[Any], list[Any]]:
    async with AsyncSession(direct) as session:
        claims = list(
            (
                await session.execute(
                    select(CreditIngressClaim).where(
                        CreditIngressClaim.tenant_id == tenant_id
                    )
                )
            ).scalars()
        )
        effects = list(
            (
                await session.execute(
                    select(CreditObservationalEffect).where(
                        CreditObservationalEffect.tenant_id == tenant_id
                    )
                )
            ).scalars()
        )
    return claims, effects


class _Recorder:
    def __init__(self) -> None:
        self.warnings: list[dict[str, Any]] = []
        self.infos: list[dict[str, Any]] = []

    def warning(self, _message: str, **fields: Any) -> None:
        self.warnings.append(fields)

    def info(self, _message: str, **fields: Any) -> None:
        self.infos.append(fields)

    def reasons(self) -> list[Any]:
        return [fields.get("reason") for fields in self.warnings]


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(credit_metering, "logger", recorder)
    return recorder


@pytest.fixture
def invalidations(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Every ``AsyncConnection.invalidate`` (class-level spy; the original
    still runs)."""

    seen: list[object] = []
    original = AsyncConnection.invalidate

    async def spy(self: AsyncConnection, exception: Any = None) -> None:
        seen.append(self)
        await original(self, exception)

    monkeypatch.setattr(AsyncConnection, "invalidate", spy)
    return seen


async def _write(
    pool: _Pool, ingress: ObservationalIngress, anchor: UUID | None = None
) -> tuple[str, float]:
    started = time.monotonic()
    outcome = await record_observational_trace(
        ingress,
        anchor_id=anchor or uuid4(),
        decision={"kind": "pipeline"},
        deliverable=True,
        session_factory=pool.sessions,
    )
    return outcome, time.monotonic() - started


async def _server_timeouts(connection: AsyncConnection) -> tuple[str, str]:
    lock = (await connection.execute(text("SHOW lock_timeout"))).scalar_one()
    statement = (await connection.execute(text("SHOW statement_timeout"))).scalar_one()
    return str(lock), str(statement)


async def _defaults(direct: AsyncEngine) -> tuple[str, str]:
    async with direct.connect() as connection:
        return await _server_timeouts(connection)


@asynccontextmanager
async def _tenant_row_held(direct: AsyncEngine, tenant_id: UUID) -> AsyncIterator[int]:
    """Another transaction holds the tenant row ``FOR NO KEY UPDATE``: the
    writer's ``FOR SHARE`` waits, foreign-key checks (``FOR KEY SHARE``) of
    the main path do not."""

    async with direct.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(
            text("SELECT 1 FROM tenants WHERE id = :t FOR NO KEY UPDATE"),
            {"t": tenant_id},
        )
        holder = int(
            (await connection.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )
        try:
            yield holder
        finally:
            await transaction.rollback()


async def _lock_waiters(direct: AsyncEngine, holder: int) -> int:
    async with direct.connect() as connection:
        return int(
            (
                await connection.execute(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                        "current_database() AND wait_event_type = 'Lock' "
                        "AND pid NOT IN (pg_backend_pid(), :holder)"
                    ),
                    {"holder": holder},
                )
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# The writer against the process pool's shape.
# ---------------------------------------------------------------------------


async def test_a_write_takes_one_connection_and_leaves_server_defaults(
    pool: _Pool,
    direct: AsyncEngine,
    logs: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _tenant(direct)
    defaults = await _defaults(direct)
    seen: list[tuple[int, tuple[str, str]]] = []
    original = CreditRepository.insert_observational_effect

    async def observe(self: CreditRepository, **kwargs: Any) -> bool:
        connection = await self.session.connection()
        seen.append((pool.engine.pool.checkedout(), await _server_timeouts(connection)))
        return await original(self, **kwargs)

    monkeypatch.setattr(CreditRepository, "insert_observational_effect", observe)
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    event.listen(pool.engine.sync_engine, "before_cursor_execute", record)
    try:
        outcome, elapsed = await _write(pool, _ingress(seeded))
    finally:
        event.remove(pool.engine.sync_engine, "before_cursor_execute", record)
    assert outcome == "recorded" and logs.warnings == []
    # Its own transaction: no SAVEPOINT, the backstops first, the commit last.
    assert not [s for s in statements if "SAVEPOINT" in s.upper()]
    assert "set_config('lock_timeout'" in statements[0]
    assert elapsed < OBSERVATIONAL_WRITE_BUDGET_SECONDS + _MARGIN
    # One connection, and the backstops are LOCAL to the writer's transaction.
    assert seen == [(1, ("250ms", "250ms"))]
    assert pool.engine.pool.checkedout() == 0
    async with pool.engine.connect() as connection:  # the same pooled backend
        assert await _server_timeouts(connection) == defaults
    claims, effects = await _rows(direct, seeded.tenant_id)
    assert len(claims) == 1 and claims[0].session_version == 2
    assert [e.effect_id for e in effects] == [
        trace_effect_id(seeded.tenant_id, claims[0].interaction_id)
    ]


@pytest.mark.parametrize("backstop", ["server", "budget"])
async def test_lock_contention_is_dropped_inside_the_budget(
    pool: _Pool,
    direct: AsyncEngine,
    logs: _Recorder,
    invalidations: list[object],
    monkeypatch: pytest.MonkeyPatch,
    backstop: str,
) -> None:
    """``server``: the real backstops (either the lock_timeout or the budget
    fires); ``budget``: a server timeout far beyond it, so the budget cancels
    the statement mid-flight and the connection must be invalidated."""

    if backstop == "budget":
        monkeypatch.setattr(credit_metering, "OBSERVATIONAL_SERVER_TIMEOUT", "30s")
    seeded = await _tenant(direct)
    defaults = await _defaults(direct)
    async with _tenant_row_held(direct, seeded.tenant_id) as holder:
        outcome, elapsed = await _write(pool, _ingress(seeded))
        await asyncio.sleep(0.2)
        waiting = await _lock_waiters(direct, holder)
    assert elapsed < OBSERVATIONAL_WRITE_BUDGET_SECONDS + _MARGIN, elapsed
    assert outcome in {"timeout", "failed"}
    assert waiting == 0, "the cancelled statement is still waiting on the lock"
    if backstop == "budget":
        assert outcome == "timeout"
        assert len(invalidations) == 1  # never returned to the pool mid-statement
    assert logs.reasons() == [outcome]
    assert pool.engine.pool.checkedout() == 0
    async with pool.engine.connect() as connection:
        assert await _server_timeouts(connection) == defaults
    assert await _rows(direct, seeded.tenant_id) == ([], [])
    # The pool still serves the next write.
    assert (await _write(pool, _ingress(seeded)))[0] == "recorded"


async def test_an_exhausted_pool_is_dropped_without_waiting(
    pool: _Pool, direct: AsyncEngine, logs: _Recorder
) -> None:
    """Owner decision: with no free connection the writer never queues for
    the pool (``pool_timeout=30`` here); it drops at once, the request's
    connections keep working and none leaks."""

    seeded = await _tenant(direct)
    held = [await pool.engine.connect() for _ in range(5)]
    try:
        assert pool.engine.pool.checkedout() == 5
        outcome, elapsed = await _write(pool, _ingress(seeded))
        assert pool.engine.pool.checkedout() == 5  # nothing taken, nothing leaked
        for connection in held:  # the request path is unaffected
            assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        for connection in held:
            await connection.close()
    assert outcome == "pool_exhausted"
    assert elapsed < 0.1, elapsed
    assert logs.reasons() == ["pool_exhausted"]
    assert logs.warnings[0]["alert"] == "credit_observational_write"
    assert await _rows(direct, seeded.tenant_id) == ([], [])
    assert pool.engine.pool.checkedout() == 0
    # The pool serves the next write once a connection is free.
    assert (await _write(pool, _ingress(seeded)))[0] == "recorded"


@contextmanager
def _append_only_insert_trigger_sql() -> Iterator[tuple[str, str]]:
    yield (
        "CREATE TRIGGER trg_test_metering_refuse BEFORE INSERT ON "
        "credit_observational_effects FOR EACH ROW EXECUTE FUNCTION "
        "reject_credit_history_mutation()",
        "DROP TRIGGER trg_test_metering_refuse ON credit_observational_effects",
    )


async def _terminate_then(self: CreditRepository, original: Any, **kwargs: Any) -> Any:
    """The claim's connection is lost: its own backend is terminated."""

    await self.session.execute(text("SELECT pg_terminate_backend(pg_backend_pid())"))
    return await original(self, **kwargs)


@pytest.mark.parametrize(
    "fault", ["claim_constraint", "append_only_trigger", "connection_lost"]
)
async def test_a_credit_side_fault_writes_nothing_and_only_logs(
    pool: _Pool,
    direct: AsyncEngine,
    logs: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    seeded = await _tenant(direct)
    ingress = _ingress(seeded)
    cleanup: str | None = None
    if fault == "claim_constraint":
        # ``ck_credit_ingress_claims_payload_sha256`` (PostgreSQL-only).
        ingress = replace(ingress, payload_sha256="not-a-sha256")
    elif fault == "append_only_trigger":
        with _append_only_insert_trigger_sql() as (create, drop):
            async with direct.begin() as connection:
                await connection.execute(text(create))
            cleanup = drop
    else:
        original = CreditRepository.insert_ingress_claim
        monkeypatch.setattr(
            CreditRepository,
            "insert_ingress_claim",
            functools.partialmethod(_terminate_then, original),
        )
    try:
        outcome, elapsed = await _write(pool, ingress)
    finally:
        if cleanup is not None:
            async with direct.begin() as connection:
                await connection.execute(text(cleanup))
    assert outcome == "failed"
    assert elapsed < OBSERVATIONAL_WRITE_BUDGET_SECONDS + _MARGIN
    [fields] = logs.warnings
    assert fields["alert"] == "credit_observational_write"
    assert fields["reason"] == "failed" and fields["error_type"]
    assert await _rows(direct, seeded.tenant_id) == ([], [])
    assert pool.engine.pool.checkedout() == 0


async def test_concurrent_writers_of_one_claim_record_one_trace(
    pool: _Pool, direct: AsyncEngine
) -> None:
    seeded = await _tenant(direct)
    first, second = uuid4(), uuid4()
    outcomes = await asyncio.gather(
        _write(pool, _ingress(seeded, key="concurrent"), first),
        _write(pool, _ingress(seeded, key="concurrent"), second),
    )
    assert sorted(outcome for outcome, _ in outcomes) == ["recorded", "replayed"]
    claims, effects = await _rows(direct, seeded.tenant_id)
    assert len(claims) == 1 and len(effects) == 1
    assert claims[0].interaction_id in {first, second}
    assert effects[0].trace_id == claims[0].interaction_id


async def test_claims_deduplicate_by_tenant_standalone_and_reparented(
    pool: _Pool, direct: AsyncEngine
) -> None:
    seeded = await _tenant(direct, standalone=True)
    zapi = _ingress(
        seeded,
        key=None,
        source="zapi",
        identity=credit_identity("zapi", seeded.tenant_id),
        provider_message_id="zapi-1",
    )
    assert (await _write(pool, zapi))[0] == "recorded"
    builder_id = uuid4()
    async with AsyncSession(direct) as session, session.begin():
        session.add(Builder(id=builder_id, name="Reparent target"))
        await session.flush()
        tenant = await session.get(Tenant, seeded.tenant_id)
        assert tenant is not None
        tenant.builder_id = builder_id
    assert (await _write(pool, zapi))[0] == "replayed"
    # A new message after the reparent carries the new builder (the trigger
    # checks it against the tenant's current builder).
    assert (await _write(pool, replace(zapi, provider_message_id="zapi-2")))[0] == (
        "recorded"
    )
    claims, effects = await _rows(direct, seeded.tenant_id)
    assert sorted((c.provider_message_id, c.builder_id) for c in claims) == sorted(
        [("zapi-1", None), ("zapi-2", builder_id)]
    )
    assert sorted(e.builder_id is None for e in effects) == [False, True]


async def test_nothing_is_written_after_the_enforcement(
    pool: _Pool, direct: AsyncEngine, logs: _Recorder
) -> None:
    seeded = await _tenant(direct)
    async with direct.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO credit_enforcement_state (id, credit_enforcement_at, "
                "set_by, set_at) VALUES (1, :at, 'test', :at)"
            ),
            {"at": datetime(2026, 10, 1, tzinfo=timezone.utc)},
        )
    try:
        assert (await _write(pool, _ingress(seeded)))[0] == "enforced"
    finally:
        async with direct.begin() as connection:
            await connection.execute(text("DELETE FROM credit_enforcement_state"))
    assert logs.warnings == []  # info, never an alert
    assert [fields["reason"] for fields in logs.infos] == ["enforced"]
    assert "alert" not in logs.infos[0]
    assert await _rows(direct, seeded.tenant_id) == ([], [])


# ---------------------------------------------------------------------------
# The real app on PostgreSQL (lifespan, middleware, the process pool).
# ---------------------------------------------------------------------------


@dataclass
class _App:
    client: Any
    world: Any
    twin_session: str


@asynccontextmanager
async def _running_app(pg: _PG, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_App]:
    import httpx

    import src.repositories as repositories
    from src.api.main import app
    from src.config.settings import settings
    from tests.conftest import _production_init_database
    from tests.integration.test_credit_schema_dominance_postgres import (
        _fake_llm_and_providers,
        _seed_world,
        _World,
    )

    world = _World.generate()
    await _seed_world(pg.url, world)
    twin = uuid4()
    seeder = create_async_engine(pg.url, poolclass=NullPool)
    try:
        async with AsyncSession(seeder) as session, session.begin():
            session.add(
                Session(
                    id=twin,
                    tenant_id=UUID(world.api_tenant),
                    channel="test",
                    cognitive_state={},
                    session_metadata={},
                )
            )
    finally:
        await seeder.dispose()
    monkeypatch.setattr(settings, "credit_hot_path_enabled", True)
    monkeypatch.setattr(settings, "credit_switch_refresh_seconds", 3600)
    monkeypatch.setattr(
        repositories,
        "init_database",
        functools.partial(
            _production_init_database,
            database_url=pg.url.render_as_string(hide_password=False),
            engine_factory=create_async_engine,
        ),
    )
    # Each running app is a fresh process for D1's one-way latch.
    credit_metering.forget_enforcement_seen()
    try:
        with _fake_llm_and_providers():
            async with app.router.lifespan_context(app):
                from src.services.credit_switch import credit_switch_effective

                assert credit_switch_effective(), "the real switch is not effective"
                transport = httpx.ASGITransport(app=app, client=("testclient", 50000))
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver"
                ) as client:
                    yield _App(client=client, world=world, twin_session=str(twin))
    finally:
        credit_metering.forget_enforcement_seen()


def _events_body(
    session_id: str, content: str = "Olá, quero agendar"
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "event_type": "message.in",
        "payload": {"content": content, "channel": "whatsapp", "user_id": "pg"},
    }


def _signed_meta(world: Any, message: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
    import hashlib
    import hmac

    raw = json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "pg-waba",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": world.meta_phone},
                                "messages": [message],
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()
    signature = hmac.new(world.meta_secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": f"sha256={signature}",
    }


@dataclass
class _RequestConnections:
    """While the writer inserts its effect: (pooled connections checked out
    by THIS task -- the request --, ``pool.checkedout()``)."""

    seen: list[tuple[int, int]]
    owners: dict[int, object]
    engines: list[Any]

    def checkout(self, _dbapi: Any, record: Any, _proxy: Any) -> None:
        self.owners[id(record)] = asyncio.current_task()

    def checkin(self, _dbapi: Any, record: Any) -> None:
        self.owners.pop(id(record), None)

    def attach(self) -> None:
        from src.repositories.database import get_db_engine

        pool = get_db_engine().sync_engine.pool
        event.listen(pool, "checkout", self.checkout)
        event.listen(pool, "checkin", self.checkin)
        self.engines.append(pool)

    def detach(self) -> None:
        for pool in self.engines:
            event.remove(pool, "checkout", self.checkout)
            event.remove(pool, "checkin", self.checkin)
        self.engines.clear()


@pytest.fixture
def request_connections(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_RequestConnections]:
    from src.repositories.database import get_db_engine

    probe = _RequestConnections(seen=[], owners={}, engines=[])
    original = CreditRepository.insert_observational_effect

    async def observe(self: CreditRepository, **kwargs: Any) -> bool:
        assert probe.engines, "the pool listener was not attached"
        mine = sum(
            1 for task in probe.owners.values() if task is asyncio.current_task()
        )
        probe.seen.append((mine, get_db_engine().pool.checkedout()))
        return await original(self, **kwargs)

    monkeypatch.setattr(CreditRepository, "insert_observational_effect", observe)
    try:
        yield probe
    finally:
        probe.detach()


async def test_the_request_holds_at_most_one_connection_while_metering(
    pg: _PG,
    direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    request_connections: _RequestConnections,
) -> None:
    """Events, Z-API, Meta text and the Meta ``audio_too_long`` file handoff
    (outside phase 1: no session lock, D6 untouched): each writer runs with
    the request holding only its own pooled connection, and what the main
    path committed (events, the handoff, the reply id) stays visible."""

    async def resolve(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"url": "https://media.invalid/long.ogg", "file_size": 50_000_000}

    monkeypatch.setattr("src.api.routes.webhook.resolve_meta_media", resolve)
    async with _running_app(pg, monkeypatch) as running:
        request_connections.attach()
        world, client = running.world, running.client
        answers = [
            await client.post(
                "/v1/events",
                json=_events_body(world.api_session),
                headers={"X-API-Key": world.api_key, "Idempotency-Key": "pg-key"},
            ),
            await client.post(
                f"/v1/webhook/zapi/{world.zapi_token}",
                json={
                    "instanceId": world.zapi_instance,
                    "messageId": "pg-zapi-1",
                    "phone": "5511900000901",
                    "fromMe": False,
                    "text": {"message": "Olá, quero agendar"},
                },
            ),
        ]
        for message in (
            {
                "from": "5511900000902",
                "id": "wamid.pg-1",
                "type": "text",
                "text": {"body": "Olá, quero agendar"},
            },
            {
                "from": "5511900000903",
                "id": "wamid.pg-long-audio",
                "type": "audio",
                "audio": {"id": "pg-media", "mime_type": "audio/ogg"},
            },
        ):
            raw, headers = _signed_meta(world, message)
            answers.append(
                await client.post("/v1/webhook/meta", content=raw, headers=headers)
            )
        request_connections.detach()
    assert [a.status_code for a in answers] == [200, 200, 200, 200], [
        a.text for a in answers
    ]
    assert [a.json().get("replied") for a in answers[1:3]] == [True, True]
    assert answers[3].json() == {"ok": True, "skipped": "audio_too_long"}
    assert len(request_connections.seen) == 4
    for mine, total in request_connections.seen:
        assert mine == 1, request_connections.seen  # only the writer's own
        assert total >= mine
    async with direct.connect() as connection:
        handoff_events = (
            await connection.execute(
                text(
                    "SELECT interaction_origin FROM events WHERE tenant_id = :t "
                    "AND event_type = 'handoff.request'"
                ),
                {"t": UUID(world.meta_tenant)},
            )
        ).all()
        handoffs = (
            await connection.execute(
                text("SELECT id FROM handoff_records WHERE tenant_id = :t"),
                {"t": UUID(world.meta_tenant)},
            )
        ).all()
        reply_ids = (
            await connection.execute(
                text(
                    "SELECT metadata -> 'ai_msg_ids' FROM sessions "
                    "WHERE tenant_id = :t"
                ),
                {"t": UUID(world.zapi_tenant)},
            )
        ).scalars()
    assert [row[0] for row in handoff_events] == ["production"]
    [(handoff_id,)] = handoffs
    assert any(
        entries and entries[-1][0] == "credit-dominance-zapi-reply"
        for entries in reply_ids
    )
    claims, effects = await _rows(direct, UUID(world.meta_tenant))
    file_claim = [c for c in claims if c.provider_message_id == "wamid.pg-long-audio"]
    assert [c.interaction_id for c in file_claim] == [handoff_id]
    assert {(e.trace_id, e.origin, e.would_debit_mc) for e in effects} >= {
        (handoff_id, "production", 1_000)
    }


@pytest.mark.parametrize(
    "fault",
    ["claim_constraint", "append_only_trigger", "lock_timeout", "connection_lost"],
)
async def test_a_credit_fault_in_the_real_app_answers_and_stores_like_main(
    pg: _PG,
    direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    from tests.integration.test_credit_schema_dominance_postgres import _mask

    masked = frozenset(
        {
            "event_id",
            "interaction_event_id",
            "timestamp",
            "processed_at",
            "processing_time_ms",
            "stage_timings",
        }
    )

    async def stored(session_id: str) -> list[str]:
        async with direct.connect() as connection:
            rows = await connection.execute(
                text(
                    "SELECT event_type, interaction_origin, event_data, metadata "
                    "FROM events WHERE session_id = :s"
                ),
                {"s": UUID(session_id)},
            )
            return sorted(
                json.dumps(_mask(list(row), masked, set()), sort_keys=True, default=str)
                for row in rows
            )

    recorder = _Recorder()
    async with _running_app(pg, monkeypatch) as running:
        world, client = running.world, running.client
        headers = {"X-API-Key": world.api_key, "Idempotency-Key": f"fault-{fault}"}
        # main: this request captures the switch OFF (class-level verdict).
        with monkeypatch.context() as off:
            off.setattr(CreditSwitch, "effective", lambda _self: False)
            baseline = await client.post(
                "/v1/events", json=_events_body(world.api_session), headers=headers
            )
        baseline_events = await stored(world.api_session)
        monkeypatch.setattr(credit_metering, "logger", recorder)
        cleanup: str | None = None
        if fault == "claim_constraint":
            # ``ck_credit_ingress_claims_payload_sha256`` refuses it.
            monkeypatch.setattr(
                credit_metering, "event_payload_sha256", lambda _request: "zz"
            )
        elif fault == "append_only_trigger":
            with _append_only_insert_trigger_sql() as (create, drop):
                async with direct.begin() as connection:
                    await connection.execute(text(create))
                cleanup = drop
        elif fault == "connection_lost":
            original = CreditRepository.insert_ingress_claim
            monkeypatch.setattr(
                CreditRepository,
                "insert_ingress_claim",
                functools.partialmethod(_terminate_then, original),
            )
        twin = {"json": _events_body(running.twin_session), "headers": headers}
        try:
            if fault == "lock_timeout":
                async with _tenant_row_held(direct, UUID(world.api_tenant)):
                    faulted = await client.post("/v1/events", **twin)
            else:
                faulted = await client.post("/v1/events", **twin)
        finally:
            if cleanup is not None:
                async with direct.begin() as connection:
                    await connection.execute(text(cleanup))
        twin_events = await stored(running.twin_session)
        # The pool is healthy afterwards: the next decision is metered.
        after = await client.post(
            "/v1/events",
            json=_events_body(world.api_session, "Mais uma dúvida"),
            headers={"X-API-Key": world.api_key},
        )
    assert baseline.status_code == faulted.status_code == after.status_code == 200
    assert _mask(faulted.json(), masked, set()) == _mask(baseline.json(), masked, set())
    # The EventStore committed the faulted decision exactly like main's.
    assert twin_events and twin_events == baseline_events
    assert recorder.reasons() in (["failed"], ["timeout"])
    claims, effects = await _rows(direct, UUID(world.api_tenant))
    assert claims == []  # neither the switched-off nor the faulted key claimed
    assert [e.trace_id for e in effects] == [UUID(after.json()["event_id"])]


# ---------------------------------------------------------------------------
# Group 3b-1: enforced POST /v1/events -- the claim's fences, the lock order
# and the atomic decision under real concurrency (PostgreSQL only, LOW-6).
# ---------------------------------------------------------------------------

_HANDOFF_TEXT = "Quero falar com um atendente humano"
# Every enforced turn runs under asyncio.timeout: a self-wait or a deadlock
# fails fast instead of hanging the suite.
_TURN_SECONDS = 60


@pytest.fixture(scope="module")
async def enforced_pg(pg: _PG) -> AsyncIterator[_PG]:
    """A second database at head with ``credit_enforcement_at`` set; the
    observational tests above never see it."""

    name = await pg.cluster.fresh()
    url = pg.cluster.url(name)
    await _migrate(url, "upgrade", "head")
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO credit_enforcement_state (id, credit_enforcement_at, "
                    "set_by, set_at) VALUES (1, clock_timestamp() - interval "
                    "'1 minute', 'test', clock_timestamp())"
                )
            )
    finally:
        await engine.dispose()
    yield _PG(cluster=pg.cluster, url=url)


@pytest.fixture
async def enforced_direct(enforced_pg: _PG) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(enforced_pg.url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


class _Gate:
    """Pause the selected callers of a patched coroutine until opened;
    ``reached`` is set once ``parties`` of them arrived."""

    def __init__(self, parties: int = 1) -> None:
        self.parties = parties
        self.arrived = 0
        self.reached = asyncio.Event()
        self.opened = asyncio.Event()

    async def wait(self) -> None:
        self.arrived += 1
        if self.arrived >= self.parties:
            self.reached.set()
        await asyncio.wait_for(self.opened.wait(), _TURN_SECONDS)


def _pause(
    monkeypatch: pytest.MonkeyPatch,
    owner: type,
    name: str,
    gate: _Gate,
    *,
    calls: frozenset[int] = frozenset({1}),
    after: bool = False,
    results: list[Any] | None = None,
) -> None:
    """Class-level wrapper (never a shared instance): the ``calls``-th
    callers wait on ``gate`` before (or after) the original runs."""

    original = getattr(owner, name)
    count = 0

    async def paused(self: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal count
        count += 1
        selected = count in calls
        if selected and not after:
            await gate.wait()
        result = await original(self, *args, **kwargs)
        if results is not None:
            results.append(result)
        if selected and after:
            await gate.wait()
        return result

    monkeypatch.setattr(owner, name, paused)


async def _until_lock_waiters(
    direct: AsyncEngine, count: int, *, query: str = "%"
) -> None:
    """Until ``count`` backends of this database wait on a lock."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _TURN_SECONDS
    while True:
        async with direct.connect() as connection:
            waiting = int(
                (
                    await connection.execute(
                        text(
                            "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                            "current_database() AND wait_event_type = 'Lock' "
                            "AND pid <> pg_backend_pid() AND query ILIKE :query"
                        ),
                        {"query": query},
                    )
                ).scalar_one()
            )
        if waiting >= count:
            return
        if loop.time() > deadline:
            raise AssertionError(f"{waiting} lock waiters, expected {count}")
        await asyncio.sleep(0.05)


async def _until_lease_over(
    direct: AsyncEngine, tenant_id: UUID, key: str, seconds: int
) -> None:
    """Until the database clock is past the claim's lease (a plain read: a
    row lock never blocks it)."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _TURN_SECONDS
    while True:
        async with direct.connect() as connection:
            over = (
                await connection.execute(
                    text(
                        "SELECT clock_timestamp() >= created_at + make_interval("
                        "secs => :seconds) FROM credit_ingress_claims WHERE "
                        "tenant_id = :t AND idempotency_key = :k"
                    ),
                    {"seconds": seconds, "t": tenant_id, "k": key},
                )
            ).scalar_one()
        if over:
            return
        if loop.time() > deadline:
            raise AssertionError("the lease never ended")
        await asyncio.sleep(0.1)


@dataclass(frozen=True)
class _Enforced:
    claims: list[Any]
    effects: list[tuple[str, str]]
    entries: dict[str, int]
    outbox: list[tuple[str, str, Any]]
    handoffs: list[tuple[UUID, Any]]
    handoff_requests: int
    decisions: list[tuple[UUID, Any]]
    message_count: dict[UUID, int]


async def _enforced_rows(direct: AsyncEngine, world: Any) -> _Enforced:
    tenant, builder = UUID(world.api_tenant), UUID(world.builder)

    async def rows(connection: AsyncConnection, sql: str, **params: Any) -> list[Any]:
        return list((await connection.execute(text(sql), params)).all())

    async with direct.connect() as connection:
        claims = await rows(
            connection,
            "SELECT id, interaction_id, mode, session_version, created_at, "
            "idempotency_key FROM credit_ingress_claims WHERE tenant_id = :t",
            t=tenant,
        )
        effects = await rows(
            connection,
            "SELECT effect_id, effect_kind FROM credit_effects WHERE builder_id = :b "
            "AND effect_kind LIKE 'trace%' ORDER BY effect_id",
            b=builder,
        )
        entries = await rows(
            connection,
            "SELECT entry_type, sum(amount_mc) FROM credit_ledger_entries "
            "WHERE builder_id = :b GROUP BY entry_type",
            b=builder,
        )
        outbox = await rows(
            connection,
            "SELECT effect_id, state, payload FROM credit_response_outbox "
            "WHERE tenant_id = :t ORDER BY effect_id",
            t=tenant,
        )
        handoffs = await rows(
            connection,
            "SELECT id, context FROM handoff_records WHERE tenant_id = :t",
            t=tenant,
        )
        requests = await rows(
            connection,
            "SELECT count(*) FROM events WHERE tenant_id = :t "
            "AND event_type = 'handoff.request'",
            t=tenant,
        )
        decisions = await rows(
            connection,
            "SELECT id, metadata FROM events WHERE tenant_id = :t "
            "AND event_type = 'srm.decision'",
            t=tenant,
        )
        counts = await rows(
            connection,
            "SELECT id, message_count FROM sessions WHERE tenant_id = :t",
            t=tenant,
        )
    return _Enforced(
        claims=claims,
        effects=[(str(e), str(k)) for e, k in effects],
        entries={str(kind): int(total) for kind, total in entries},
        outbox=[(str(e), str(s), p) for e, s, p in outbox],
        handoffs=[(UUID(str(i)), c) for i, c in handoffs],
        handoff_requests=int(requests[0][0]),
        decisions=[(UUID(str(i)), m) for i, m in decisions],
        message_count={UUID(str(i)): int(c) for i, c in counts},
    )


@pytest.fixture
def announced(monkeypatch: pytest.MonkeyPatch) -> list[UUID]:
    """The handoff creation webhooks that would leave the process (only a
    committed record gets this far); nothing is sent."""

    from src.services.handoff_service import HandoffService

    sent: list[UUID] = []

    async def record(_self: Any, handoff: Any, *_args: Any) -> tuple[bool, None]:
        sent.append(handoff.id)
        return False, None

    monkeypatch.setattr(HandoffService, "_dispatch_creation_webhook", record)
    return sent


def _api(world: Any, key: str) -> dict[str, str]:
    return {"X-API-Key": world.api_key, "Idempotency-Key": key}


def _trace(world: Any, claim: Any) -> str:
    return f"trace:{world.api_tenant}:{claim.interaction_id}:decision:1"


async def test_an_enforced_decision_charges_once_atomically_on_postgres(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    announced: list[UUID],
) -> None:
    """One plain decision and one synchronous phase 1 handoff: each costs one
    trace committed with its decision; ``lock_timeout`` is 15 s only inside
    the enforced phase 1 and dies with its commit; the LLM never runs inside
    a transaction; a retry replays the stored answer byte for byte."""

    lock_timeouts: dict[str, list[str]] = {"phase1": [], "final": []}

    def spy_timeout(name: str, phase: str) -> None:
        original = getattr(CreditRepository, name)

        async def spy(self: CreditRepository, *args: Any, **kwargs: Any) -> Any:
            shown = await self.session.execute(text("SHOW lock_timeout"))
            lock_timeouts[phase].append(str(shown.scalar_one()))
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(CreditRepository, name, spy)

    spy_timeout("mark_claim_phase1", "phase1")
    spy_timeout("hold_claim_final_fence", "final")
    from src.services.event_service import EventService

    open_transactions: list[int] = []
    generate = EventService.generate_response

    async def outside(self: Any, *args: Any, **kwargs: Any) -> Any:
        async with enforced_direct.connect() as connection:
            open_transactions.append(
                int(
                    (
                        await connection.execute(
                            text(
                                "SELECT count(*) FROM pg_stat_activity WHERE datname "
                                "= current_database() AND pid <> pg_backend_pid() "
                                "AND state LIKE 'idle in transaction%'"
                            )
                        )
                    ).scalar_one()
                )
            )
        return await generate(self, *args, **kwargs)

    monkeypatch.setattr(EventService, "generate_response", outside)
    defaults = await _defaults(enforced_direct)
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        async with asyncio.timeout(_TURN_SECONDS):
            plain = await client.post(
                "/v1/events",
                json=_events_body(running.twin_session),
                headers=_api(world, "pg-plain"),
            )
            handoff = await client.post(
                "/v1/events",
                json=_events_body(world.api_session, _HANDOFF_TEXT),
                headers=_api(world, "pg-handoff"),
            )
            replay = await client.post(
                "/v1/events",
                json=_events_body(world.api_session, _HANDOFF_TEXT),
                headers=_api(world, "pg-handoff"),
            )
            [handoff_id] = [
                h for h, _ in (await _enforced_rows(enforced_direct, world)).handoffs
            ]
            detail = await client.get(
                f"/v1/handoffs/{handoff_id}", headers={"X-API-Key": world.api_key}
            )
        from src.repositories.database import get_db_engine

        async with get_db_engine().connect() as pooled:
            after = await _server_timeouts(pooled)
    assert plain.status_code == handoff.status_code == 200, (plain.text, handoff.text)
    assert replay.status_code == 200 and replay.content == handoff.content
    assert handoff.json()["handoff_summary"] is not None
    assert plain.json()["prompt"]["suggested_response"]
    assert lock_timeouts == {"phase1": ["15s", "15s"], "final": [defaults[0]] * 2}
    assert after == defaults
    assert open_transactions == [0, 0]
    seen = await _enforced_rows(enforced_direct, world)
    by_key = {c.idempotency_key: c for c in seen.claims}
    assert {c.mode for c in seen.claims} == {"enforced"}
    assert all(c.session_version is not None for c in seen.claims)
    expected = sorted((_trace(world, c), "trace") for c in seen.claims)
    assert seen.effects == expected
    assert seen.entries == {"debt": 2_000}  # no lot: served as debt, one trace each
    assert sorted((e, s) for e, s, _ in seen.outbox) == sorted(
        (effect, "delivered") for effect, _ in expected
    )
    assert {e: p for e, _, p in seen.outbox}[_trace(world, by_key["pg-handoff"])] == (
        handoff.json()
    )
    [(_, context)] = seen.handoffs
    assert context["credit_effect_id"] == _trace(world, by_key["pg-handoff"])
    assert context.get("key_info") is not None  # merged, not replaced
    assert seen.handoff_requests == 1
    assert sorted(m["credit_effect_id"] for _, m in seen.decisions) == sorted(
        effect for effect, _ in expected
    )
    assert announced == [handoff_id]  # after the final transaction, once
    assert detail.status_code == 200 and "credit_effect_id" not in detail.text


async def test_a_stalled_phase1_loses_its_marker_after_a_takeover(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    announced: list[UUID],
) -> None:
    """HIGH-1 (a): executor A stalls before its phase 1 marker -- holding the
    session row, its handoff and its debit, uncommitted -- past its lease;
    B takes the claim over and waits for the session.  A's marker no longer
    matches: A rolls everything back (no handoff, no debit, no creation
    webhook) and answers by the table; B decides once."""

    monkeypatch.setattr(credit_metering, "CLAIM_LEASE_SECONDS", 2)
    stalled = _Gate()
    _pause(monkeypatch, CreditRepository, "mark_claim_phase1", stalled)
    takeover = CreditRepository.take_over_claim

    async def take_over(self: CreditRepository, *args: Any, **kwargs: Any) -> Any:
        observed = await takeover(self, *args, **kwargs)
        if observed is not None:
            # From now on the lease is long: B's own fences hold, and A's
            # resumed marker fails on the claim it observed, not the clock.
            monkeypatch.setattr(credit_metering, "CLAIM_LEASE_SECONDS", 600)
        return observed

    monkeypatch.setattr(CreditRepository, "take_over_claim", take_over)
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        body = _events_body(world.api_session, _HANDOFF_TEXT)
        async with asyncio.timeout(_TURN_SECONDS):
            first = asyncio.create_task(
                client.post("/v1/events", json=body, headers=_api(world, "stall-1"))
            )
            await stalled.reached.wait()
            await _until_lease_over(
                enforced_direct, UUID(world.api_tenant), "stall-1", 2
            )
            second = asyncio.create_task(
                client.post("/v1/events", json=body, headers=_api(world, "stall-1"))
            )
            # B took over and now waits on the session row A holds.
            await _until_lock_waiters(enforced_direct, 1, query="%sessions%")
            assert not first.done() and not second.done()
            stalled.opened.set()
            a, b = await asyncio.gather(first, second)
    assert b.status_code == 200, b.text
    assert (a.status_code, a.json()) == (409, {"code": "event_in_progress"}) or (
        a.status_code == 200 and a.content == b.content
    ), a.text
    seen = await _enforced_rows(enforced_direct, world)
    [claim] = seen.claims
    assert seen.effects == [(_trace(world, claim), "trace")]
    assert seen.entries == {"debt": 1_000}
    [(handoff_id, context)] = seen.handoffs
    assert seen.handoff_requests == 1
    assert context["credit_effect_id"] == _trace(world, claim)
    assert announced == [handoff_id]  # A's creation webhook never left
    assert seen.message_count[UUID(world.api_session)] == 1  # advanced once


async def test_a_retry_waits_for_a_final_transaction_past_its_lease_and_replays(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HIGH-1 (b): the final check passed and keeps the claim's row lock;
    the transaction pauses before COMMIT until the lease is over.  A retry
    blocks on ``FOR SHARE``, and once the commit lands it replays the 200 --
    never ``event_failed`` for a decision that commits and is charged."""

    lease = 6
    monkeypatch.setattr(credit_metering, "CLAIM_LEASE_SECONDS", lease)
    held = _Gate()
    _pause(monkeypatch, CreditRepository, "hold_claim_final_fence", held, after=True)
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        body = _events_body(world.api_session)
        async with asyncio.timeout(_TURN_SECONDS):
            first = asyncio.create_task(
                client.post("/v1/events", json=body, headers=_api(world, "held-1"))
            )
            await held.reached.wait()
            await _until_lease_over(
                enforced_direct, UUID(world.api_tenant), "held-1", lease
            )
            retry = asyncio.create_task(
                client.post("/v1/events", json=body, headers=_api(world, "held-1"))
            )
            blocked = asyncio.create_task(
                _until_lock_waiters(
                    enforced_direct, 1, query="%credit_ingress_claims%FOR SHARE%"
                )
            )
            await asyncio.wait({retry, blocked}, return_when=asyncio.FIRST_COMPLETED)
            # The retry must wait on the claim row the final check holds: an
            # answer now would be about a decision that is about to commit.
            assert not retry.done(), retry.result().text
            await blocked
            held.opened.set()
            a, r = await asyncio.gather(first, retry)
    assert a.status_code == 200, a.text
    assert r.status_code == 200 and r.content == a.content
    seen = await _enforced_rows(enforced_direct, world)
    [claim] = seen.claims
    assert seen.effects == [(_trace(world, claim), "trace")]
    assert seen.entries == {"debt": 1_000}


async def test_a_final_transaction_reaching_its_fence_after_the_lease_writes_nothing(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HIGH-1 (b): the bundle, the debit and the stored answer are written
    but the final check comes only after the lease: it fails, everything is
    rolled back and the answer is the final ``event_failed``."""

    lease = 6
    monkeypatch.setattr(credit_metering, "CLAIM_LEASE_SECONDS", lease)
    late = _Gate()
    _pause(monkeypatch, CreditRepository, "hold_claim_final_fence", late)
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        async with asyncio.timeout(_TURN_SECONDS):
            first = asyncio.create_task(
                client.post(
                    "/v1/events",
                    json=_events_body(world.api_session),
                    headers=_api(world, "late-1"),
                )
            )
            await late.reached.wait()
            await _until_lease_over(
                enforced_direct, UUID(world.api_tenant), "late-1", lease
            )
            late.opened.set()
            answer = await first
    seen = await _enforced_rows(enforced_direct, world)
    [claim] = seen.claims
    assert answer.status_code == 409 and answer.json() == {
        "code": "event_failed",
        "interaction_id": str(claim.interaction_id),
    }
    assert claim.session_version is not None  # phase 1 committed
    assert seen.decisions == [] and seen.outbox == []
    assert seen.effects == [] and seen.entries == {}


async def test_a_reader_that_saw_the_lease_end_wins_over_a_waiting_final_check(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HIGH-1, R1 (reproduced by the review).  A retry holds the claim ``FOR
    SHARE`` while the lease is alive; the final check's ``UPDATE`` waits on
    it; the lease ends; the retry reads the clock, finds no stored answer and
    answers the final ``event_failed``.  The final check must then fail too:
    PostgreSQL judged a ``WHERE`` before the wait and does not judge it again
    after a ``FOR SHARE`` holder (it changed nothing), so the lease is judged
    in ``RETURNING`` once the row is locked.  Every answer agrees --
    ``event_failed`` three times -- and nothing is charged."""

    lease = 8
    monkeypatch.setattr(credit_metering, "CLAIM_LEASE_SECONDS", lease)
    before_check = _Gate()
    _pause(monkeypatch, CreditRepository, "hold_claim_final_fence", before_check)
    # The retry pauses INSIDE its clock read: after its FOR SHARE, before the
    # clock -- the read must happen while the share lock is held (M9).
    reading = _Gate()
    _pause(monkeypatch, CreditRepository, "lease_clock", reading)
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        body = _events_body(world.api_session)
        async with asyncio.timeout(_TURN_SECONDS):
            original = asyncio.create_task(
                client.post("/v1/events", json=body, headers=_api(world, "r1"))
            )
            await before_check.reached.wait()  # bundle, debit, answer: written
            retry = asyncio.create_task(
                client.post("/v1/events", json=body, headers=_api(world, "r1"))
            )
            await reading.reached.wait()  # FOR SHARE held, lease still alive
            before_check.opened.set()
            await _until_lock_waiters(
                enforced_direct, 1, query="%UPDATE credit_ingress_claims%"
            )
            await _until_lease_over(
                enforced_direct, UUID(world.api_tenant), "r1", lease
            )
            reading.opened.set()
            a, r = await asyncio.gather(original, retry)
            third = await client.post(
                "/v1/events", json=body, headers=_api(world, "r1")
            )
    seen = await _enforced_rows(enforced_direct, world)
    [claim] = seen.claims
    failed = {"code": "event_failed", "interaction_id": str(claim.interaction_id)}
    assert (r.status_code, r.json()) == (409, failed), r.text
    assert (a.status_code, a.json()) == (409, failed), a.text
    assert (third.status_code, third.json()) == (409, failed), third.text
    assert seen.effects == [] and seen.entries == {}
    assert seen.outbox == [] and seen.decisions == []


async def test_a_phase1_stalled_past_its_lease_without_a_retry_rolls_everything_back(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    announced: list[UUID],
) -> None:
    """m09: nobody takes the claim over; phase 1 (with a handoff and its
    debit) only resumes after its lease.  Its marker judges the lease once
    the row is locked: it fails, EVERYTHING is rolled back -- handoff,
    ``handoff.request``, debit -- no creation webhook leaves, and a later
    retry takes over and decides once."""

    monkeypatch.setattr(credit_metering, "CLAIM_LEASE_SECONDS", 3)
    stalled = _Gate()
    _pause(monkeypatch, CreditRepository, "mark_claim_phase1", stalled)
    takeover = CreditRepository.take_over_claim

    async def take_over(self: CreditRepository, *args: Any, **kwargs: Any) -> Any:
        observed = await takeover(self, *args, **kwargs)
        if observed is not None:  # the retry's own lease is long
            monkeypatch.setattr(credit_metering, "CLAIM_LEASE_SECONDS", 600)
        return observed

    monkeypatch.setattr(CreditRepository, "take_over_claim", take_over)
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        body = _events_body(world.api_session, _HANDOFF_TEXT)
        async with asyncio.timeout(_TURN_SECONDS):
            first = asyncio.create_task(
                client.post("/v1/events", json=body, headers=_api(world, "m09"))
            )
            await stalled.reached.wait()
            await _until_lease_over(enforced_direct, UUID(world.api_tenant), "m09", 3)
            stalled.opened.set()
            late = await first
            rolled_back = await _enforced_rows(enforced_direct, world)
            retry = await client.post(
                "/v1/events", json=body, headers=_api(world, "m09")
            )
    assert (late.status_code, late.json()) == (409, {"code": "event_in_progress"})
    [claim] = rolled_back.claims
    assert claim.session_version is None
    assert rolled_back.handoffs == [] and rolled_back.handoff_requests == 0
    assert rolled_back.effects == [] and rolled_back.entries == {}
    assert retry.status_code == 200, retry.text
    seen = await _enforced_rows(enforced_direct, world)
    [taken] = seen.claims
    assert taken.interaction_id == claim.interaction_id
    assert seen.effects == [(_trace(world, taken), "trace")]
    assert seen.entries == {"debt": 1_000}
    [(handoff_id, _)] = seen.handoffs
    assert announced == [handoff_id]  # the stalled phase 1 announced nothing


async def test_a_marker_waiting_behind_a_reader_past_the_lease_loses_to_its_takeover(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    announced: list[UUID],
) -> None:
    """HIGH-1 (a), through the route.  Executor A reaches its phase 1 marker
    (a handoff and its debit written) while a retry R holds the claim ``FOR
    SHARE`` with the lease alive; A's marker waits on R; the lease ends; R
    reads the clock under its share lock, sees an abandoned claim and takes
    it over.  A's marker -- judged once the row is locked -- fails, A rolls
    everything back, and R decides once."""

    lease = 5
    monkeypatch.setattr(credit_metering, "CLAIM_LEASE_SECONDS", lease)
    at_marker = _Gate()
    _pause(monkeypatch, CreditRepository, "mark_claim_phase1", at_marker)
    reading = _Gate()
    _pause(monkeypatch, CreditRepository, "lease_clock", reading)
    takeover = CreditRepository.take_over_claim

    async def take_over(self: CreditRepository, *args: Any, **kwargs: Any) -> Any:
        observed = await takeover(self, *args, **kwargs)
        if observed is not None:  # R's own lease is long
            monkeypatch.setattr(credit_metering, "CLAIM_LEASE_SECONDS", 600)
        return observed

    monkeypatch.setattr(CreditRepository, "take_over_claim", take_over)
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        body = _events_body(world.api_session, _HANDOFF_TEXT)
        async with asyncio.timeout(_TURN_SECONDS):
            first = asyncio.create_task(
                client.post("/v1/events", json=body, headers=_api(world, "behind"))
            )
            await at_marker.reached.wait()
            retry = asyncio.create_task(
                client.post("/v1/events", json=body, headers=_api(world, "behind"))
            )
            await reading.reached.wait()  # R holds FOR SHARE, lease alive
            at_marker.opened.set()
            await _until_lock_waiters(
                enforced_direct, 1, query="%UPDATE credit_ingress_claims%"
            )
            await _until_lease_over(
                enforced_direct, UUID(world.api_tenant), "behind", lease
            )
            reading.opened.set()
            a, r = await asyncio.gather(first, retry)
    assert r.status_code == 200, r.text
    assert (a.status_code, a.json()) == (409, {"code": "event_in_progress"}) or (
        a.status_code == 200 and a.content == r.content
    ), a.text
    seen = await _enforced_rows(enforced_direct, world)
    [claim] = seen.claims
    assert seen.effects == [(_trace(world, claim), "trace")]
    assert seen.entries == {"debt": 1_000}
    [(handoff_id, _)] = seen.handoffs
    assert seen.handoff_requests == 1
    assert announced == [handoff_id]  # A's rolled-back handoff never left


# -- The fences one statement at a time (repository level): every actor a real
# connection, the order forced by row locks and pg_stat_activity, the lease
# ended by the database clock (ported from the focused review).

_FENCE_LEASE_SECONDS = 3
_FENCE_LEASE = timedelta(seconds=_FENCE_LEASE_SECONDS)


def _fence_enforcement(seeded: _Tenant, key: str) -> Any:
    import hashlib

    return credit_metering.EventsEnforcement(
        tenant_id=seeded.tenant_id,
        session_id=seeded.session_id,
        principal_id=f"api_key:{seeded.tenant_id}",
        payload_sha256=hashlib.sha256(key.encode()).hexdigest(),
        idempotency_key=key,
        key_problem=None,
    )


async def _fence_claim(direct: AsyncEngine, seeded: _Tenant, key: str) -> Any:
    enforcement = _fence_enforcement(seeded, key)
    async with AsyncSession(direct) as session:
        repository = CreditRepository(session)
        await repository.tenant_scope(seeded.tenant_id, lock=True)
        row = await repository.insert_enforced_claim(
            tenant_id=seeded.tenant_id,
            builder_id=seeded.builder_id,
            principal_id=enforcement.principal_id,
            idempotency_key=key,
            interaction_id=uuid4(),
            payload_sha256=enforcement.payload_sha256,
            decision_version=1,
        )
        await session.commit()
    assert row is not None
    return row


async def _fence_phase1(direct: AsyncEngine, row: Any, version: int = 7) -> None:
    async with AsyncSession(direct) as session:
        assert await CreditRepository(session).mark_claim_phase1(
            row.id, observed=row.created_at, session_version=version, lease=_FENCE_LEASE
        )
        await session.commit()


async def _db_past(direct: AsyncEngine, instant: datetime) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _TURN_SECONDS
    while True:
        async with direct.connect() as connection:
            if (
                await connection.execute(
                    text("SELECT clock_timestamp() >= :t"), {"t": instant}
                )
            ).scalar_one():
                return
        assert loop.time() < deadline, "the database clock never got there"
        await asyncio.sleep(0.05)


async def _fence_row(direct: AsyncEngine, claim_id: UUID) -> Any:
    async with direct.connect() as connection:
        return (
            await connection.execute(
                text(
                    "SELECT session_version, created_at FROM credit_ingress_claims "
                    "WHERE id = :i"
                ),
                {"i": claim_id},
            )
        ).one()


async def _fence_read(direct: AsyncEngine, enforcement: Any) -> Any:
    async with AsyncSession(direct) as session:
        return await credit_metering.read_events_claim(session, enforcement)


@pytest.fixture
def fence_lease(monkeypatch: pytest.MonkeyPatch) -> timedelta:
    """Readers judge with the module constant: the same lease as the fences."""

    monkeypatch.setattr(credit_metering, "CLAIM_LEASE_SECONDS", _FENCE_LEASE_SECONDS)
    return _FENCE_LEASE


@pytest.fixture
def reader_clock(monkeypatch: pytest.MonkeyPatch) -> _Gate:
    """The first reader pauses INSIDE ``lease_clock``: its ``FOR SHARE`` is
    held, its clock not read yet."""

    gate = _Gate()
    _pause(monkeypatch, CreditRepository, "lease_clock", gate)
    return gate


async def test_fence_a_marker_waiting_behind_a_reader_past_its_lease_loses(
    enforced_direct: AsyncEngine, fence_lease: timedelta, reader_clock: _Gate
) -> None:
    """The reader holds ``FOR SHARE`` (lease alive, no phase 1); A's marker
    waits; the lease ends; the reader reads the clock UNDER its share lock
    and decides "take over"; A's marker must fail, and the takeover wins."""

    seeded = await _tenant(enforced_direct)
    row = await _fence_claim(enforced_direct, seeded, "m1")
    reader = asyncio.create_task(
        _fence_read(enforced_direct, _fence_enforcement(seeded, "m1"))
    )
    await asyncio.wait_for(reader_clock.reached.wait(), _TURN_SECONDS)
    a = AsyncSession(enforced_direct)
    try:
        marker = asyncio.create_task(
            CreditRepository(a).mark_claim_phase1(
                row.id, observed=row.created_at, session_version=3, lease=fence_lease
            )
        )
        await _until_lock_waiters(
            enforced_direct, 1, query="%UPDATE credit_ingress_claims%"
        )
        await _db_past(enforced_direct, row.created_at + fence_lease)
        reader_clock.opened.set()
        verdict = await asyncio.wait_for(reader, _TURN_SECONDS)
        assert not isinstance(verdict, credit_metering.ClaimAnswer), verdict
        assert await asyncio.wait_for(marker, _TURN_SECONDS) is False
    finally:
        await a.rollback()
        await a.close()
    async with AsyncSession(enforced_direct) as b:
        taken = await CreditRepository(b).take_over_claim(
            row.id, observed=verdict.created_at, lease=fence_lease
        )
        await b.commit()
    assert taken is not None
    assert (await _fence_row(enforced_direct, row.id)).session_version is None


async def test_fence_a_reader_that_saw_the_end_beats_a_waiting_final_check(
    enforced_direct: AsyncEngine, fence_lease: timedelta, reader_clock: _Gate
) -> None:
    """R1 one statement at a time: the reader holds ``FOR SHARE`` with the
    lease alive; the final check waits; the lease ends; the reader answers
    ``event_failed``; the final check must judge the lease dead."""

    seeded = await _tenant(enforced_direct)
    row = await _fence_claim(enforced_direct, seeded, "r1")
    await _fence_phase1(enforced_direct, row)
    enforcement = _fence_enforcement(seeded, "r1")
    reader = asyncio.create_task(_fence_read(enforced_direct, enforcement))
    await asyncio.wait_for(reader_clock.reached.wait(), _TURN_SECONDS)
    final = AsyncSession(enforced_direct)
    try:
        repository = CreditRepository(final)
        await repository.tenant_scope(seeded.tenant_id, lock=True)
        await repository.insert_response_outbox(
            tenant_id=seeded.tenant_id,
            effect_id=trace_effect_id(seeded.tenant_id, row.interaction_id),
            payload={"answer": "stored"},
            state="delivered",
        )
        check = asyncio.create_task(
            repository.hold_claim_final_fence(
                row.id, observed=row.created_at, session_version=7, lease=fence_lease
            )
        )
        await _until_lock_waiters(
            enforced_direct, 1, query="%UPDATE credit_ingress_claims%"
        )
        await _db_past(enforced_direct, row.created_at + fence_lease)
        reader_clock.opened.set()
        answer = await asyncio.wait_for(reader, _TURN_SECONDS)
        assert answer.body["code"] == "event_failed", answer
        assert await asyncio.wait_for(check, _TURN_SECONDS) is False
    finally:
        await final.rollback()
        await final.close()
    again = await _fence_read(enforced_direct, enforcement)
    assert again.body["code"] == "event_failed"


async def test_fence_a_marker_holding_first_then_a_takeover_waits_and_loses(
    enforced_direct: AsyncEngine, fence_lease: timedelta
) -> None:
    seeded = await _tenant(enforced_direct)
    row = await _fence_claim(enforced_direct, seeded, "m2")
    a, b = AsyncSession(enforced_direct), AsyncSession(enforced_direct)
    try:
        assert await CreditRepository(a).mark_claim_phase1(
            row.id, observed=row.created_at, session_version=3, lease=fence_lease
        )
        await _db_past(enforced_direct, row.created_at + fence_lease)
        take = asyncio.create_task(
            CreditRepository(b).take_over_claim(
                row.id, observed=row.created_at, lease=fence_lease
            )
        )
        await _until_lock_waiters(
            enforced_direct, 1, query="%UPDATE credit_ingress_claims%"
        )
        await a.commit()  # phase 1 committed: never taken over
        assert await asyncio.wait_for(take, _TURN_SECONDS) is None
        await b.commit()
    finally:
        await a.close()
        await b.close()
    assert (await _fence_row(enforced_direct, row.id)).session_version == 3


async def test_fence_a_takeover_never_passes_a_final_check_after_phase1(
    enforced_direct: AsyncEngine, fence_lease: timedelta
) -> None:
    """Phase 1 committed, the final check holds the row, the lease ends: a
    takeover answers ``None`` at once -- it never even waits for that row --
    and the decision commits and replays."""

    seeded = await _tenant(enforced_direct)
    row = await _fence_claim(enforced_direct, seeded, "tf")
    await _fence_phase1(enforced_direct, row)
    final = AsyncSession(enforced_direct)
    b = AsyncSession(enforced_direct)
    try:
        repository = CreditRepository(final)
        await repository.tenant_scope(seeded.tenant_id, lock=True)
        await repository.insert_response_outbox(
            tenant_id=seeded.tenant_id,
            effect_id=trace_effect_id(seeded.tenant_id, row.interaction_id),
            payload={"answer": "stored"},
            state="delivered",
        )
        assert await repository.hold_claim_final_fence(
            row.id, observed=row.created_at, session_version=7, lease=fence_lease
        )
        await _db_past(enforced_direct, row.created_at + fence_lease)
        take = asyncio.create_task(
            CreditRepository(b).take_over_claim(
                row.id, observed=row.created_at, lease=fence_lease
            )
        )
        done, _ = await asyncio.wait({take}, timeout=5)
        await final.commit()
        taken = await asyncio.wait_for(take, _TURN_SECONDS)
        await b.commit()
    finally:
        await final.close()
        await b.close()
    assert done, "the takeover waited on the final check's row"
    assert taken is None
    answer = await _fence_read(enforced_direct, _fence_enforcement(seeded, "tf"))
    assert answer.status == 200 and answer.replay


@pytest.mark.parametrize("first", ["release", "takeover"])
async def test_fence_a_release_and_a_takeover_of_one_expired_lease(
    enforced_direct: AsyncEngine, fence_lease: timedelta, first: str
) -> None:
    """A late release never kills a takeover that already happened, and a
    takeover waiting behind a release finds the claim changed."""

    seeded = await _tenant(enforced_direct)
    row = await _fence_claim(enforced_direct, seeded, f"rel-{first}")
    await _db_past(enforced_direct, row.created_at + fence_lease)
    a, b = AsyncSession(enforced_direct), AsyncSession(enforced_direct)

    async def release() -> bool:
        return await CreditRepository(a).release_claim_lease(
            row.id, observed=row.created_at, session_version=None, lease=fence_lease
        )

    async def takeover() -> Any:
        return await CreditRepository(b).take_over_claim(
            row.id, observed=row.created_at, lease=fence_lease
        )

    try:
        if first == "release":
            assert await release() is True
            other = asyncio.create_task(takeover())
            await _until_lock_waiters(enforced_direct, 1, query="%UPDATE%")
            await a.commit()
            assert await asyncio.wait_for(other, _TURN_SECONDS) is None
            await b.commit()
            released = await _fence_row(enforced_direct, row.id)
            async with AsyncSession(enforced_direct) as c:  # the next retry
                assert (
                    await CreditRepository(c).take_over_claim(
                        row.id, observed=released.created_at, lease=fence_lease
                    )
                    is not None
                )
                await c.commit()
        else:
            new = await takeover()
            assert new is not None
            other = asyncio.create_task(release())
            await _until_lock_waiters(enforced_direct, 1, query="%UPDATE%")
            await b.commit()
            assert await asyncio.wait_for(other, _TURN_SECONDS) is False
            await a.commit()
            assert (await _fence_row(enforced_direct, row.id)).created_at == new
    finally:
        await a.close()
        await b.close()


async def _stale_claim(direct: AsyncEngine, world: Any, key: str, content: str) -> UUID:
    from src.api.schemas.events import CanonicalEventRequest, EventRequest

    request = EventRequest.model_validate(_events_body(world.api_session, content))
    canonical = CanonicalEventRequest(
        tenant_id=UUID(world.api_tenant),
        session_id=request.session_id,
        event_type=request.event_type,
        payload=request.payload,
    )
    interaction = uuid4()
    async with direct.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO credit_ingress_claims (id, tenant_id, builder_id, source, "
                "principal_id, idempotency_key, interaction_id, payload_sha256, "
                "decision_version, mode, created_at) VALUES (:id, :t, :b, 'events', "
                ":p, :k, :i, :sha, 1, 'enforced', clock_timestamp() - interval "
                "'1 hour')"
            ),
            {
                "id": uuid4(),
                "t": UUID(world.api_tenant),
                "b": UUID(world.builder),
                "p": f"api_key:{world.api_tenant}",
                "k": key,
                "i": interaction,
                "sha": credit_metering.event_payload_sha256(canonical),
            },
        )
    return interaction


async def test_two_concurrent_takeovers_of_one_claim_let_one_win(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HIGH-1 (c): both retries read the abandoned claim and reach the
    takeover together; one CAS wins and re-executes under the same
    ``interaction_id``, the other answers by the table."""

    from src.services.event_service import EventService

    both = _Gate(parties=2)
    takeovers: list[Any] = []
    _pause(
        monkeypatch,
        CreditRepository,
        "take_over_claim",
        both,
        calls=frozenset({1, 2}),
        results=takeovers,
    )
    pipelines: list[Any] = []
    _pause(
        monkeypatch,
        EventService,
        "run_pipeline",
        _Gate(),
        calls=frozenset(),
        results=pipelines,
    )
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        content = "Olá, quero agendar"
        interaction = await _stale_claim(enforced_direct, world, "abandoned", content)
        body = _events_body(world.api_session, content)
        async with asyncio.timeout(_TURN_SECONDS):
            tasks = [
                asyncio.create_task(
                    client.post(
                        "/v1/events", json=body, headers=_api(world, "abandoned")
                    )
                )
                for _ in range(2)
            ]
            await both.reached.wait()
            both.opened.set()
            answers = await asyncio.gather(*tasks)
    assert sum(observed is not None for observed in takeovers) == 1, takeovers
    assert len(takeovers) == 2 and len(pipelines) == 1
    winner = [a for a in answers if a.status_code == 200]
    assert winner, [a.text for a in answers]
    for answer in answers:
        assert answer.content == winner[0].content or (
            answer.status_code,
            answer.json(),
        ) == (409, {"code": "event_in_progress"})
    seen = await _enforced_rows(enforced_direct, world)
    [claim] = seen.claims
    assert claim.interaction_id == interaction
    assert seen.effects == [(_trace(world, claim), "trace")]
    assert len(seen.decisions) == 1


async def test_two_concurrent_requests_with_one_key_run_phase1_once(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.event_service import EventService

    both = _Gate(parties=2)
    inserted: list[Any] = []
    _pause(
        monkeypatch,
        CreditRepository,
        "insert_enforced_claim",
        both,
        calls=frozenset({1, 2}),
        results=inserted,
    )
    pipelines: list[Any] = []
    _pause(
        monkeypatch,
        EventService,
        "run_pipeline",
        _Gate(),
        calls=frozenset(),
        results=pipelines,
    )
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        body = _events_body(world.api_session)
        async with asyncio.timeout(_TURN_SECONDS):
            tasks = [
                asyncio.create_task(
                    client.post("/v1/events", json=body, headers=_api(world, "twice"))
                )
                for _ in range(2)
            ]
            await both.reached.wait()
            both.opened.set()
            answers = await asyncio.gather(*tasks)
    assert sum(row is not None for row in inserted) == 1 and len(pipelines) == 1
    winner = [a for a in answers if a.status_code == 200]
    assert len(winner) >= 1, [a.text for a in answers]
    for answer in answers:
        assert answer.content == winner[0].content or (
            answer.status_code,
            answer.json(),
        ) == (409, {"code": "event_in_progress"})
    seen = await _enforced_rows(enforced_direct, world)
    [claim] = seen.claims
    assert seen.effects == [(_trace(world, claim), "trace")]
    assert seen.entries == {"debt": 1_000}


def _super_admin() -> dict[str, str]:
    import jwt

    from src.config.settings import settings

    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(uuid4()),
            "role": "super_admin",
            "email": "admin@test.com",
            "iat": now,
            "exp": now.replace(year=now.year + 1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


async def test_a_final_transaction_and_a_clinic_delete_finish_without_deadlock(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEDIUM-1: the final transaction takes the tenant ``FOR SHARE`` FIRST.
    Paused right after it (before the bundle), a concurrent ``DELETE
    /clinics/{clinic_id}`` waits on the tenant row; the decision commits
    whole, then the delete cascades -- no deadlock, no partial write."""

    from src.services.event_service import EventService

    before_bundle = _Gate()
    _pause(monkeypatch, EventService, "write_interaction_bundle", before_bundle)
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        async with asyncio.timeout(_TURN_SECONDS):
            decision = asyncio.create_task(
                client.post(
                    "/v1/events",
                    json=_events_body(world.api_session),
                    headers=_api(world, "deleted-later"),
                )
            )
            await before_bundle.reached.wait()
            delete = asyncio.create_task(
                client.delete(
                    f"/v1/admin/clinics/{world.api_tenant}", headers=_super_admin()
                )
            )
            blocked = asyncio.create_task(
                _until_lock_waiters(enforced_direct, 1, query="%tenants%")
            )
            await asyncio.wait({delete, blocked}, return_when=asyncio.FIRST_COMPLETED)
            # The final transaction already holds the tenant row: the delete
            # waits for it instead of cascading under the decision.
            assert not delete.done(), delete.result().text
            await blocked
            before_bundle.opened.set()
            answer, deleted = await asyncio.gather(decision, delete)
    assert answer.status_code == 200, answer.text
    assert answer.json()["interaction_event_id"] is not None
    assert deleted.status_code == 200 and deleted.json() == {"ok": True}
    async with enforced_direct.connect() as connection:
        gone = (
            await connection.execute(
                text("SELECT count(*) FROM tenants WHERE id = :t"),
                {"t": UUID(world.api_tenant)},
            )
        ).scalar_one()
        effects = (
            await connection.execute(
                text(
                    "SELECT effect_kind, decision_digest IS NOT NULL FROM "
                    "credit_effects WHERE tenant_id = :t"
                ),
                {"t": UUID(world.api_tenant)},
            )
        ).all()
    assert gone == 0
    # The effect outlives its tenant (its digest is the decision's proof).
    assert [tuple(e) for e in effects] == [("trace", True)]


async def test_a_phase1_holding_a_session_never_blocks_another_sessions_final(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MEDIUM-1: Y's phase 1 holds session S ``FOR UPDATE`` (stalled before
    its marker) and X's final transaction waits on S through its bundle's
    foreign keys -- holding no credit lock yet, the bundle precedes the
    account.  Z's final transaction on another session T of the same builder
    commits meanwhile; X and Y finish once Y resumes."""

    from src.services.event_service import EventService

    bundle = _Gate()
    _pause(monkeypatch, EventService, "write_interaction_bundle", bundle)
    # Y stalls at its phase 1 marker (the 2nd one), still holding S FOR
    # UPDATE; repository objects are fresh per call, so an instance patch of
    # the shared pipeline by an earlier suite cannot shadow this one.
    pipeline = _Gate()
    _pause(
        monkeypatch,
        CreditRepository,
        "mark_claim_phase1",
        pipeline,
        calls=frozenset({2}),
    )
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        s, t = world.api_session, running.twin_session
        async with asyncio.timeout(_TURN_SECONDS):
            x = asyncio.create_task(
                client.post(
                    "/v1/events", json=_events_body(s), headers=_api(world, "x")
                )
            )
            await bundle.reached.wait()
            y = asyncio.create_task(
                client.post(
                    "/v1/events",
                    json=_events_body(s, "Pode ser amanhã?"),
                    headers=_api(world, "y"),
                )
            )
            await pipeline.reached.wait()
            bundle.opened.set()
            await _until_lock_waiters(enforced_direct, 1, query="%events%")
            z = await asyncio.wait_for(
                client.post(
                    "/v1/events", json=_events_body(t), headers=_api(world, "z")
                ),
                20,
            )
            assert z.status_code == 200, z.text
            assert not x.done() and not y.done()
            pipeline.opened.set()
            answers = await asyncio.gather(x, y)
    assert [a.status_code for a in answers] == [200, 200], [a.text for a in answers]
    seen = await _enforced_rows(enforced_direct, world)
    assert len(seen.effects) == 3 and seen.entries == {"debt": 3_000}


@asynccontextmanager
async def _session_row_held(
    direct: AsyncEngine, session_id: UUID
) -> AsyncIterator[None]:
    async with direct.connect() as connection:
        transaction = await connection.begin()
        await connection.execute(
            text("SELECT 1 FROM sessions WHERE id = :s FOR UPDATE"), {"s": session_id}
        )
        try:
            yield
        finally:
            await transaction.rollback()


async def test_a_phase1_lock_timeout_releases_the_lease_for_an_immediate_takeover(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HIGH-1 (e): phase 1 hits its ``lock_timeout`` (shortened here) on a
    session row held elsewhere: 500, nothing written, and the executor
    expires its own lease, so the next retry takes over at once."""

    monkeypatch.setattr(credit_metering, "PHASE1_LOCK_TIMEOUT_SECONDS", 1)
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        body = _events_body(world.api_session)
        async with asyncio.timeout(_TURN_SECONDS):
            async with _session_row_held(enforced_direct, UUID(world.api_session)):
                failed = await client.post(
                    "/v1/events", json=body, headers=_api(world, "lock-timeout")
                )
            released = await _enforced_rows(enforced_direct, world)
            retry = await client.post(
                "/v1/events", json=body, headers=_api(world, "lock-timeout")
            )
    assert failed.status_code == 500
    [claim] = released.claims
    assert claim.session_version is None
    assert released.effects == [] and released.handoffs == []
    async with enforced_direct.connect() as connection:
        expired = (
            await connection.execute(
                text(
                    "SELECT clock_timestamp() >= CAST(:c AS timestamptz) "
                    "+ interval '1680 seconds'"
                ),
                {"c": claim.created_at},
            )
        ).scalar_one()
    assert expired  # created_at = clock_timestamp() - lease
    assert retry.status_code == 200, retry.text
    seen = await _enforced_rows(enforced_direct, world)
    [taken] = seen.claims
    assert taken.interaction_id == claim.interaction_id
    assert seen.effects == [(_trace(world, taken), "trace")]


# ---------------------------------------------------------------------------
# Group 3b-2: enforced Z-API/Meta webhooks, the Meta file handoff and the
# response outbox on PostgreSQL (the races SQLite cannot show).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Webhook:
    claims: list[Any]
    effects: list[tuple[str, str]]
    entries: dict[str, int]
    outbox: list[tuple[str, str, Any, int]]
    handoffs: list[tuple[UUID, Any]]
    handoff_requests: int
    links: list[Any]


async def _webhook_rows(direct: AsyncEngine, tenant_id: UUID) -> _Webhook:
    async def rows(connection: AsyncConnection, sql: str) -> list[Any]:
        return list((await connection.execute(text(sql), {"t": tenant_id})).all())

    async with direct.connect() as connection:
        claims = await rows(
            connection,
            "SELECT id, source, mode, provider_message_id, interaction_id, "
            "session_version, payload_sha256 FROM credit_ingress_claims "
            "WHERE tenant_id = :t",
        )
        effects = await rows(
            connection,
            "SELECT effect_id, effect_kind FROM credit_effects WHERE tenant_id = :t "
            "ORDER BY effect_id",
        )
        entries = await rows(
            connection,
            "SELECT e.entry_type, sum(e.amount_mc) FROM credit_ledger_entries e "
            "JOIN credit_effects f ON f.builder_id = e.builder_id "
            "AND f.effect_id = e.effect_id WHERE f.tenant_id = :t "
            "GROUP BY e.entry_type",
        )
        outbox = await rows(
            connection,
            "SELECT effect_id, state, payload, attempts FROM credit_response_outbox "
            "WHERE tenant_id = :t ORDER BY effect_id",
        )
        handoffs = await rows(
            connection, "SELECT id, context FROM handoff_records WHERE tenant_id = :t"
        )
        requests = await rows(
            connection,
            "SELECT count(*) FROM events WHERE tenant_id = :t "
            "AND event_type = 'handoff.request'",
        )
        links = await rows(
            connection,
            "SELECT metadata ->> 'credit_effect_id' FROM events WHERE tenant_id = :t "
            "AND event_type = 'srm.decision'",
        )
    return _Webhook(
        claims=claims,
        effects=[(str(e), str(k)) for e, k in effects],
        entries={str(kind): int(total) for kind, total in entries},
        outbox=[(str(e), str(s), p, int(a)) for e, s, p, a in outbox],
        handoffs=[(UUID(str(i)), c) for i, c in handoffs],
        handoff_requests=int(requests[0][0]),
        links=[link for (link,) in links],
    )


def _clinic_admin(tenant_id: UUID) -> dict[str, str]:
    import jwt

    from src.config.settings import settings

    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(tenant_id),
            "role": "clinic_admin",
            "iat": now - timedelta(minutes=1),
            "exp": now + timedelta(hours=1),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _wtrace(tenant_id: Any, claim: Any) -> str:
    return f"trace:{tenant_id}:{claim.interaction_id}:decision:1"


@pytest.fixture
def redis_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis is out: no echo/notice store, and the replay claim -- per-process
    memory outside ``is_production`` (``ENVIRONMENT=staging`` in real
    production) -- answers "first delivery" to every process.  Patched, so
    the criterion does not depend on PR #112 (LOW-3)."""

    from src.api.routes import webhook

    monkeypatch.setattr(webhook, "_get_redis", lambda: None)
    monkeypatch.setattr(webhook, "_claim_incoming_message", lambda *_args: True)


async def _seed_patient(url: URL, tenant_id: str, phone: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with AsyncSession(engine) as session, session.begin():
            session.add(
                Session(
                    id=uuid4(),
                    tenant_id=UUID(tenant_id),
                    user_id=phone,
                    channel="whatsapp",
                    cognitive_state={},
                    session_metadata={"phone": phone, "source": "webhook"},
                )
            )
    finally:
        await engine.dispose()


def _sent_to(mock: Any, phone: str) -> list[str]:
    return [call.args[3] for call in mock.await_args_list if call.args[2] == phone]


@pytest.mark.parametrize("channel", ["zapi", "meta"])
async def test_redis_down_two_sessions_charge_one_decision(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    redis_down: None,
    channel: str,
) -> None:
    """Redis down, ``ENVIRONMENT=staging``, the switch on, the enforcement
    set: two route sessions receive the SAME provider message at once (both
    pass the replay store) and meet at the PostgreSQL claim.  One decides
    and replies, the other answers ``duplicate_message``: one trace, one
    reply."""

    from src.api.routes import webhook
    from src.config.settings import settings

    both = _Gate(parties=2)
    inserted: list[Any] = []
    _pause(
        monkeypatch,
        CreditRepository,
        "insert_webhook_claim",
        both,
        calls=frozenset({1, 2}),
        results=inserted,
    )
    phone = "5511900009001" if channel == "zapi" else "5511900009002"
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        monkeypatch.setattr(settings, "environment", "staging")
        assert settings.is_production is False
        tenant = world.zapi_tenant if channel == "zapi" else world.meta_tenant
        await _seed_patient(enforced_pg.url, tenant, phone)
        if channel == "zapi":
            body = json.dumps(
                {
                    "instanceId": world.zapi_instance,
                    "messageId": "pg-redis-down-1",
                    "phone": phone,
                    "fromMe": False,
                    "text": {"message": "Olá, quero agendar"},
                }
            ).encode()
            path, headers = f"/v1/webhook/zapi/{world.zapi_token}", {
                "Content-Type": "application/json"
            }
            sender = webhook.send_text_message
        else:
            body, headers = _signed_meta(
                world,
                {
                    "from": phone,
                    "id": "wamid.pg-redis-down-1",
                    "type": "text",
                    "text": {"body": "Olá, quero agendar"},
                },
            )
            path, sender = "/v1/webhook/meta", webhook.send_meta_cloud_text
        async with asyncio.timeout(_TURN_SECONDS):
            tasks = [
                asyncio.create_task(client.post(path, content=body, headers=headers))
                for _ in range(2)
            ]
            await both.reached.wait()
            both.opened.set()
            answers = await asyncio.gather(*tasks)
        replies = _sent_to(sender, phone)
    assert [a.status_code for a in answers] == [200, 200], [a.text for a in answers]
    assert sorted(json.dumps(a.json(), sort_keys=True) for a in answers) == sorted(
        [
            json.dumps({"ok": True, "replied": True}, sort_keys=True),
            json.dumps({"ok": True, "skipped": "duplicate_message"}, sort_keys=True),
        ]
    )
    assert sum(row is not None for row in inserted) == 1  # PostgreSQL decided
    seen = await _webhook_rows(enforced_direct, UUID(tenant))
    [claim] = seen.claims
    assert (claim.source, claim.mode) == (channel, "enforced")
    assert seen.effects == [(_wtrace(tenant, claim), "trace")]
    assert seen.entries == {"debt": 1_000}  # charged exactly once
    assert [(e, s) for e, s, _p, _a in seen.outbox] == [
        (_wtrace(tenant, claim), "delivered")
    ]
    assert len(replies) == 1  # one reply, never two


async def test_an_enforced_zapi_decision_charges_once_and_delivers_inline_on_postgres(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claim, phase 1, the LLM outside every transaction, then bundle +
    trace + ``pending`` reply in one commit; the inline send claims the row,
    sends once and marks it ``delivered`` with phone and text erased."""

    from src.api.routes import webhook
    from src.services.event_service import EventService

    open_transactions: list[int] = []
    generate = EventService.generate_response

    async def outside(self: Any, *args: Any, **kwargs: Any) -> Any:
        async with enforced_direct.connect() as connection:
            open_transactions.append(
                int(
                    (
                        await connection.execute(
                            text(
                                "SELECT count(*) FROM pg_stat_activity WHERE datname "
                                "= current_database() AND pid <> pg_backend_pid() "
                                "AND state LIKE 'idle in transaction%'"
                            )
                        )
                    ).scalar_one()
                )
            )
        return await generate(self, *args, **kwargs)

    monkeypatch.setattr(EventService, "generate_response", outside)
    # M7 (the pinned deviation): no ``lock_timeout`` in the webhook phase 1.
    phase1_lock_timeouts: list[str] = []
    marker = CreditRepository.mark_webhook_claim_phase1

    async def spy_marker(self: CreditRepository, *args: Any, **kwargs: Any) -> Any:
        shown = await self.session.execute(text("SHOW lock_timeout"))
        phase1_lock_timeouts.append(str(shown.scalar_one()))
        return await marker(self, *args, **kwargs)

    monkeypatch.setattr(CreditRepository, "mark_webhook_claim_phase1", spy_marker)
    defaults = await _defaults(enforced_direct)
    states: list[str] = []
    original_send = webhook._send_reply_with_optional_tts

    async def observed_send(**kwargs: Any) -> Any:
        async with enforced_direct.connect() as connection:
            states.extend(
                str(state)
                for (state,) in await connection.execute(
                    text(
                        "SELECT state FROM credit_response_outbox WHERE "
                        "payload ->> 'phone' = :p"
                    ),
                    {"p": kwargs["phone"]},
                )
            )
        return await original_send(**kwargs)

    monkeypatch.setattr(webhook, "_send_reply_with_optional_tts", observed_send)
    phone = "5511900009101"
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        async with asyncio.timeout(_TURN_SECONDS):
            answer = await client.post(
                f"/v1/webhook/zapi/{world.zapi_token}",
                json={
                    "instanceId": world.zapi_instance,
                    "messageId": "pg-zapi-enforced-1",
                    "phone": phone,
                    "fromMe": False,
                    "text": {"message": "Olá, quero agendar"},
                },
            )
        replies = _sent_to(webhook.send_text_message, phone)
    assert answer.json() == {"ok": True, "replied": True}, answer.text
    assert phase1_lock_timeouts == [defaults[0]]  # the server default, not 15s
    assert open_transactions == [0]  # the LLM ran outside every transaction
    assert states == ["sending"]  # the row was claimed, committed, before the send
    assert replies == ["Olá! Posso ajudar com o seu agendamento."]
    seen = await _webhook_rows(enforced_direct, UUID(world.zapi_tenant))
    [claim] = seen.claims
    assert claim.session_version is not None
    effect = _wtrace(world.zapi_tenant, claim)
    assert seen.effects == [(effect, "trace")] and seen.entries == {"debt": 1_000}
    assert seen.links == [effect]
    [(outbox_effect, state, payload, attempts)] = seen.outbox
    assert (outbox_effect, state, attempts) == (effect, "delivered", 0)
    assert "phone" not in payload and "text" not in payload
    assert not [key for key in payload if "token" in key or "secret" in key]


async def test_an_enforced_meta_file_handoff_commits_atomically_on_postgres(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    redis_down: None,
) -> None:
    """O2/MEDIUM-5: claim, handoff (record and ``handoff.request``), debit
    and the ``credit_effect_id`` link commit together; the creation webhook
    leaves only after that commit; a redelivery (Redis down) creates no
    second handoff nor debit; a fault after the handoff insert leaves
    nothing; ``GET /handoffs/{id}`` never exposes the link."""

    from src.services.handoff_service import HandoffService

    committed_at_dispatch: list[tuple[int, int]] = []
    tenants: list[UUID] = []

    async def announce(_self: Any, handoff: Any, *_args: Any) -> tuple[bool, None]:
        # Seen from another connection: the record AND its charge committed.
        async with enforced_direct.connect() as connection:
            committed_at_dispatch.append(
                tuple(  # type: ignore[arg-type]
                    (
                        await connection.execute(
                            text(
                                "SELECT (SELECT count(*) FROM handoff_records WHERE "
                                "id = :h AND context ? 'credit_effect_id'), (SELECT "
                                "count(*) FROM credit_effects WHERE tenant_id = :t)"
                            ),
                            {"h": handoff.id, "t": tenants[0]},
                        )
                    ).one()
                )
            )
        return False, None

    monkeypatch.setattr(HandoffService, "_dispatch_creation_webhook", announce)

    async def resolve(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"url": "https://media.invalid/long.ogg", "file_size": 50_000_000}

    monkeypatch.setattr("src.api.routes.webhook.resolve_meta_media", resolve)
    phone = "5511900009201"
    document = {
        "from": phone,
        "id": "wamid.pg-document",
        "type": "document",
        "document": {"id": "pg-doc", "mime_type": "application/pdf"},
    }
    forward = {
        "from": phone,
        "id": "wamid.pg-forward",
        "type": "text",
        "text": {"body": "Pode encaminhar para a equipe"},
    }
    audio = {
        "from": phone,
        "id": "wamid.pg-long-audio",
        "type": "audio",
        "audio": {"id": "pg-media", "mime_type": "audio/ogg"},
    }
    broken = {**audio, "id": "wamid.pg-broken"}
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        tenants.append(UUID(world.meta_tenant))
        answers = []
        async with asyncio.timeout(_TURN_SECONDS):
            for message in (document, forward, audio, forward, audio):
                raw, headers = _signed_meta(world, message)
                answers.append(
                    await client.post("/v1/webhook/meta", content=raw, headers=headers)
                )
            before_fault = await _webhook_rows(enforced_direct, tenants[0])

            async def fault(_self: Any, **_kwargs: Any) -> bool:
                raise RuntimeError("injected after the handoff insert")

            with monkeypatch.context() as patch:
                patch.setattr(CreditRepository, "link_handoff_credit_effect", fault)
                raw, headers = _signed_meta(world, broken)
                with pytest.raises(RuntimeError, match="after the handoff insert"):
                    await client.post("/v1/webhook/meta", content=raw, headers=headers)
            seen = await _webhook_rows(enforced_direct, tenants[0])
            details = [
                await client.get(
                    f"/v1/handoffs/{handoff_id}", headers=_clinic_admin(tenants[0])
                )
                for handoff_id, _context in seen.handoffs
            ]
    assert [a.json() for a in answers] == [
        {"ok": True, "replied": True, "action": "file_ack"},
        {"ok": True, "replied": True, "action": "file_handoff"},
        {"ok": True, "skipped": "audio_too_long"},
        {"ok": True, "skipped": "duplicate_message"},
        {"ok": True, "skipped": "duplicate_message"},
    ]
    assert seen == before_fault  # the fault left nothing durable
    claims = {c.provider_message_id: c for c in seen.claims}
    assert set(claims) == {"wamid.pg-forward", "wamid.pg-long-audio"}
    assert all(
        c.mode == "enforced" and c.session_version is not None for c in claims.values()
    )
    expected = sorted(_wtrace(tenants[0], c) for c in claims.values())
    assert sorted(e for e, _ in seen.effects) == expected
    assert {k for _, k in seen.effects} == {"trace"}
    assert seen.entries == {"debt": 2_000}  # one trace per file handoff
    assert sorted(c["credit_effect_id"] for _, c in seen.handoffs) == expected
    assert all(c.get("key_info") for _, c in seen.handoffs)  # merged, not replaced
    assert seen.handoff_requests == 2
    assert seen.outbox == []  # the patient's text is sent as today
    assert committed_at_dispatch == [(1, 1), (1, 2)]
    assert [d.status_code for d in details] == [200, 200]
    assert all("credit_effect_id" not in d.text for d in details)


# -- the response outbox: publishers, the inline claim and the sweep -------


async def _outbox_row(
    direct: AsyncEngine,
    seeded: _Tenant,
    *,
    state: str = "pending",
    age_seconds: float = 3600,
    payload: dict[str, Any] | None = None,
) -> UUID:
    """A stored reply as ``run_webhook_final`` writes it, for ``seeded``'s
    session (its OD-2 snapshot: not paused); ``marker`` = the row id."""

    row_id = uuid4()
    async with direct.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO credit_response_outbox (id, tenant_id, effect_id, "
                "payload, state, attempts, created_at) VALUES (:id, :t, :e, "
                "CAST(:p AS jsonb), :s, 0, clock_timestamp() - make_interval("
                "secs => :age))"
            ),
            {
                "id": row_id,
                "t": seeded.tenant_id,
                "e": f"trace:{seeded.tenant_id}:{uuid4()}:decision:1",
                "p": json.dumps(
                    {
                        "kind": "webhook_reply.v1",
                        "channel": "zapi",
                        "tenant_id": str(seeded.tenant_id),
                        "session_id": str(seeded.session_id),
                        "phone": "5511900009301",
                        "text": "Olá!",
                        "decided_at": datetime.now(timezone.utc).isoformat(),
                        "session_pause": [False, None],
                        "marker": str(row_id),
                    }
                    if payload is None
                    else payload
                ),
                "s": state,
                "age": age_seconds,
            },
        )
    return row_id


async def _outbox_state(direct: AsyncEngine, row_id: UUID) -> tuple[str, Any, int]:
    async with direct.connect() as connection:
        state, payload, attempts = (
            await connection.execute(
                text(
                    "SELECT state, payload, attempts FROM credit_response_outbox "
                    "WHERE id = :id"
                ),
                {"id": row_id},
            )
        ).one()
    return str(state), payload, int(attempts)


class _Recorded:
    """A ``ReplySender`` that records its sends (by ``marker``)."""

    def __init__(self, delay: float = 0) -> None:
        self.sent: list[str] = []
        self.delay = delay

    async def prepare(self, payload: Any, db: AsyncSession) -> Any:
        return payload

    async def send(self, prepared: Any) -> Any:
        self.sent.append(prepared["marker"])
        if self.delay:
            await asyncio.sleep(self.delay)  # the provider call overlaps
        return {"messageId": "pg"}

    async def record(self, prepared: Any, sent: Any, db: AsyncSession) -> None:
        return None


@pytest.fixture
async def enforced_pool(enforced_pg: _PG) -> AsyncIterator[_Pool]:
    engine = create_async_engine(
        enforced_pg.url, pool_size=5, max_overflow=0, pool_timeout=30
    )
    try:
        yield _Pool(
            engine=engine,
            sessions=async_sessionmaker(
                engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
            ),
        )
    finally:
        await engine.dispose()


async def test_two_publishers_never_send_the_same_row(
    enforced_direct: AsyncEngine,
    enforced_pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``FOR UPDATE SKIP LOCKED`` and the ``sending`` claim committed before
    the provider call: two ECS tasks never send one row at once."""

    monkeypatch.setattr(CreditSwitch, "effective", lambda _self: True)
    seeded = await _tenant(enforced_direct)
    rows = [await _outbox_row(enforced_direct, seeded) for _ in range(6)]
    sender = _Recorded(delay=0.05)
    publishers = [
        credit_metering.CreditResponsePublisher(
            sender, session_factory=enforced_pool.sessions
        )
        for _ in range(2)
    ]
    async with asyncio.timeout(_TURN_SECONDS):
        ticks = await asyncio.gather(*(p.run_once() for p in publishers))
    assert all(tick is not None and tick.enforced for tick in ticks)
    assert len(sender.sent) == len(set(sender.sent))  # no row sent twice
    mine = sorted(s for s in sender.sent if UUID(s) in rows)
    assert mine == sorted(map(str, rows))
    for row in rows:
        state, payload, attempts = await _outbox_state(enforced_direct, row)
        assert (state, attempts) == ("delivered", 1) and "phone" not in payload


async def test_the_inline_claim_waits_for_the_publishers_claim_and_loses(
    enforced_direct: AsyncEngine,
    enforced_pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOW-2: the publisher holds the row ``FOR UPDATE`` (its claim not yet
    committed); the inline ``UPDATE ... WHERE state = 'pending' RETURNING``
    waits, then sees ``sending`` and returns nothing: no inline send.  A
    second publisher skips the locked row without waiting."""

    monkeypatch.setattr(CreditSwitch, "effective", lambda _self: True)
    seeded = await _tenant(enforced_direct)
    row = await _outbox_row(enforced_direct, seeded)
    held = _Gate()
    _pause(
        monkeypatch,
        CreditRepository,
        "claim_outbox_for_publish",
        held,
        calls=frozenset({1}),
        after=True,
    )
    published = _Recorded()
    inline_sender = _Recorded()
    first = credit_metering.CreditResponsePublisher(
        published, session_factory=enforced_pool.sessions
    )
    second = credit_metering.CreditResponsePublisher(
        published, session_factory=enforced_pool.sessions
    )
    async with asyncio.timeout(_TURN_SECONDS):
        holder = asyncio.create_task(first.run_once())
        await held.reached.wait()  # the row is locked, its claim uncommitted
        skipped = await second.run_once()  # SKIP LOCKED: never waits
        assert skipped is not None and str(row) not in published.sent
        inline = asyncio.create_task(
            credit_metering.deliver_reply_inline(
                row, sender=inline_sender, session_factory=enforced_pool.sessions
            )
        )
        await _until_lock_waiters(
            enforced_direct, 1, query="%UPDATE credit_response_outbox%"
        )
        held.opened.set()
        delivery = await inline
        tick = await holder
    assert delivery.outcome == "not_claimed" and inline_sender.sent == []
    assert tick is not None and "delivered" in tick.outcomes
    assert published.sent.count(str(row)) == 1
    assert (await _outbox_state(enforced_direct, row))[0] == "delivered"


async def test_the_retention_sweep_on_postgres(
    enforced_direct: AsyncEngine,
    enforced_pool: _Pool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OD-3: with the switch OFF and the config off, every row that is not
    ``sending`` and older than the retention gets ``payload = '{}'``
    (``delivered``, ``failed``, abandoned ``pending``, a ``POST /v1/events``
    answer); ``sending`` and young rows keep theirs; an emptied row is never
    rewritten; a row another transaction holds is skipped without waiting;
    nothing is claimed."""

    from src.config.settings import settings

    monkeypatch.setattr(CreditSwitch, "effective", lambda _self: False)
    monkeypatch.setattr(settings, "credit_hot_path_enabled", False)
    seeded = await _tenant(enforced_direct)
    old = settings.credit_replay_retention_hours * 3600 + 60
    events_answer = {"event_id": str(uuid4()), "prompt": {"suggested_response": "x"}}
    rows = {
        "delivered": await _outbox_row(
            enforced_direct, seeded, state="delivered", age_seconds=old
        ),
        "failed": await _outbox_row(
            enforced_direct, seeded, state="failed", age_seconds=old
        ),
        "pending": await _outbox_row(
            enforced_direct, seeded, state="pending", age_seconds=old
        ),
        "events": await _outbox_row(
            enforced_direct,
            seeded,
            state="delivered",
            age_seconds=old,
            payload=events_answer,
        ),
        "sending": await _outbox_row(
            enforced_direct, seeded, state="sending", age_seconds=old
        ),
        "young": await _outbox_row(
            enforced_direct, seeded, state="delivered", age_seconds=60
        ),
        "empty": await _outbox_row(
            enforced_direct, seeded, state="delivered", age_seconds=old, payload={}
        ),
        "locked": await _outbox_row(
            enforced_direct, seeded, state="failed", age_seconds=old
        ),
    }

    async def xmin(row: UUID) -> str:
        async with enforced_direct.connect() as connection:
            return str(
                (
                    await connection.execute(
                        text("SELECT xmin FROM credit_response_outbox WHERE id = :id"),
                        {"id": row},
                    )
                ).scalar_one()
            )

    empty_before = await xmin(rows["empty"])
    async with enforced_direct.connect() as holder:
        transaction = await holder.begin()
        await holder.execute(
            text("SELECT 1 FROM credit_response_outbox WHERE id = :id FOR UPDATE"),
            {"id": rows["locked"]},
        )
        async with asyncio.timeout(_TURN_SECONDS):
            swept = await credit_metering.sweep_expired_replies(enforced_pool.sessions)
        await transaction.rollback()
    assert swept >= 4  # other tests' rows may be swept too
    for name in ("delivered", "failed", "pending", "events"):
        assert (await _outbox_state(enforced_direct, rows[name]))[1] == {}, name
    assert (await _outbox_state(enforced_direct, rows["pending"]))[0] == "pending"
    for name in ("sending", "young", "locked"):
        assert (await _outbox_state(enforced_direct, rows[name]))[1]["phone"], name
    assert await xmin(rows["empty"]) == empty_before  # never rewritten


async def test_the_sweep_skips_a_database_without_the_outbox(
    pg: _PG, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Credit code on a ``037`` database works like main: the always-on
    sweep finds no outbox and touches nothing (no error, no alert)."""

    name = await pg.cluster.fresh()
    url = pg.cluster.url(name)
    await _migrate(url, "upgrade", "037")
    engine = create_async_engine(url, poolclass=NullPool)
    recorder = _Recorder()
    recorder.error = recorder.warning  # type: ignore[attr-defined]
    monkeypatch.setattr(credit_metering, "logger", recorder)
    try:
        sessions = async_sessionmaker(engine, class_=AsyncSession)
        assert await credit_metering.sweep_expired_replies(sessions) == 0
    finally:
        await engine.dispose()
    assert recorder.warnings == []


# -- the reviewer's probes (3b-2 review), ported -----------------------------


def _no_app_publisher(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app's own publisher stays out of the races these tests drive."""

    import src.api.main as main_module

    monkeypatch.setattr(
        main_module, "start_credit_response_publisher", lambda *_a, **_k: None
    )


async def _patient_session(direct: AsyncEngine, tenant: str, phone: str) -> UUID:
    async with direct.connect() as connection:
        return UUID(
            str(
                (
                    await connection.execute(
                        text(
                            "SELECT id FROM sessions WHERE tenant_id = :t AND "
                            "user_id = :p"
                        ),
                        {"t": UUID(tenant), "p": phone},
                    )
                ).scalar_one()
            )
        )


def _zapi_message(world: Any, message_id: str, phone: str, text_: str) -> Any:
    return {
        "instanceId": world.zapi_instance,
        "messageId": message_id,
        "phone": phone,
        "fromMe": False,
        "text": {"message": text_},
    }


async def test_a_session_lock_during_the_bookkeeping_never_multiplies_a_reply(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    redis_down: None,
) -> None:
    """Review probe A (OD-5): the provider ACCEPTED the reply, then the echo
    bookkeeping waits on the patient's session row (another message's phase
    1).  Only the provider call is under the send timeout: the reply is
    delivered once, the bookkeeping gives up at its own bound (its
    connection invalidated, never pooled) and the publisher has nothing to
    resend."""

    from src.api.routes import webhook
    from src.repositories.database import get_db_session_factory

    _no_app_publisher(monkeypatch)
    monkeypatch.setattr(credit_metering, "REPLY_SEND_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(credit_metering, "REPLY_RECORD_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(credit_metering, "PUBLISHER_MIN_AGE_SECONDS", 0)
    monkeypatch.setattr(credit_metering, "SENDING_LEASE_SECONDS", 1)
    phone = "5511900019001"
    sends: list[str] = []
    ready, release = asyncio.Event(), asyncio.Event()
    holder: asyncio.Task[None] | None = None
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        await _seed_patient(enforced_pg.url, world.zapi_tenant, phone)
        sid = await _patient_session(enforced_direct, world.zapi_tenant, phone)

        async def hold() -> None:
            async with enforced_direct.connect() as connection:
                transaction = await connection.begin()
                await connection.execute(
                    text("SELECT 1 FROM sessions WHERE id = :s FOR UPDATE"), {"s": sid}
                )
                ready.set()
                await release.wait()
                await transaction.rollback()

        async def provider(
            _inst: str, _tok: str, to: str, body: str, *, client_token: str = ""
        ) -> dict[str, Any]:
            nonlocal holder
            sends.append(body)
            if holder is None:
                holder = asyncio.create_task(hold())
                await ready.wait()
            return {"messageId": f"pg-accepted-{len(sends)}"}

        with monkeypatch.context() as patch:
            patch.setattr(webhook, "send_text_message", provider)
            async with asyncio.timeout(_TURN_SECONDS):
                answer = await client.post(
                    f"/v1/webhook/zapi/{world.zapi_token}",
                    json=_zapi_message(world, "pg-bookkeeping-1", phone, "Olá"),
                )
                await asyncio.sleep(1.2)  # past the patched sending lease
                tick = await credit_metering.CreditResponsePublisher(
                    webhook.CREDIT_REPLY_SENDER,
                    session_factory=get_db_session_factory(),
                ).run_once()
                release.set()
                if holder is not None:
                    await holder
                # The pool is healthy afterwards: the next decision is served.
                after = await client.post(
                    f"/v1/webhook/zapi/{world.zapi_token}",
                    json=_zapi_message(world, "pg-bookkeeping-2", phone, "Mais uma"),
                )
    assert answer.json() == {"ok": True, "replied": True}
    assert after.json() == {"ok": True, "replied": True}
    assert tick is not None  # (rows of other tests may be in this database)
    assert len(sends) == 2  # one per decision, never a resend
    seen = await _webhook_rows(enforced_direct, UUID(world.zapi_tenant))
    assert seen.entries == {"debt": 2_000}
    assert [s for _e, s, _p, _a in seen.outbox] == ["delivered", "delivered"]


async def test_a_failed_inline_send_is_never_resent_even_after_a_takeover(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    redis_down: None,
) -> None:
    """Review probe B (OD-1): the inline send fails (a provider 5xx); the
    clinic then takes over.  Like main the reply is lost -- ``failed`` at
    once, PII erased -- and the publisher sends nothing, ever."""

    from src.api.routes import webhook
    from src.repositories.database import get_db_session_factory

    _no_app_publisher(monkeypatch)
    monkeypatch.setattr(credit_metering, "PUBLISHER_MIN_AGE_SECONDS", 0)
    phone = "5511900019002"
    calls: list[str] = []
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        await _seed_patient(enforced_pg.url, world.zapi_tenant, phone)

        async def provider(
            _inst: str, _tok: str, to: str, body: str, *, client_token: str = ""
        ) -> dict[str, Any]:
            calls.append(body)
            raise RuntimeError("provider 502")

        with monkeypatch.context() as patch:
            patch.setattr(webhook, "send_text_message", provider)
            async with asyncio.timeout(_TURN_SECONDS):
                first = await client.post(
                    f"/v1/webhook/zapi/{world.zapi_token}",
                    json=_zapi_message(world, "pg-takeover-1", phone, "Olá"),
                )
                async with enforced_direct.begin() as connection:
                    await connection.execute(
                        text(
                            "UPDATE sessions SET is_paused = true, paused_until = "
                            "clock_timestamp() + interval '1 hour' WHERE tenant_id = "
                            ":t AND user_id = :p"
                        ),
                        {"t": UUID(world.zapi_tenant), "p": phone},
                    )
                tick = await credit_metering.CreditResponsePublisher(
                    webhook.CREDIT_REPLY_SENDER,
                    session_factory=get_db_session_factory(),
                ).run_once()
    assert first.json()["reason"] == "send_failed"
    assert tick is not None
    assert len(calls) == 1  # never resent
    seen = await _webhook_rows(enforced_direct, UUID(world.zapi_tenant))
    [(_effect, state, payload, _attempts)] = seen.outbox
    assert state == "failed" and "phone" not in payload and "text" not in payload
    assert seen.entries == {"debt": 1_000}  # OD-4: the decision stays charged


async def _crash_after_the_final_commit(
    client: Any, world: Any, message_id: str, phone: str
) -> None:
    """The inline send never runs: the process dies right after the final
    commit (the row stays ``pending`` for the publisher)."""

    async def crashed(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the process died after the final commit")

    patch = pytest.MonkeyPatch()
    patch.setattr(credit_metering, "deliver_reply_inline", crashed)
    try:
        with pytest.raises(RuntimeError, match="the process died"):
            await client.post(
                f"/v1/webhook/zapi/{world.zapi_token}",
                json=_zapi_message(world, message_id, phone, "Olá"),
            )
    finally:
        patch.undo()


async def test_a_recovered_reply_is_refused_after_a_takeover(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    redis_down: None,
) -> None:
    """OD-2 on the publisher: the inline send crashed; before the recovery
    the clinic took over -- the publisher re-reads the session and marks
    the row ``failed`` without sending."""

    from src.api.routes import webhook
    from src.repositories.database import get_db_session_factory

    _no_app_publisher(monkeypatch)
    monkeypatch.setattr(credit_metering, "PUBLISHER_MIN_AGE_SECONDS", 0)
    phone = "5511900019006"
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        await _seed_patient(enforced_pg.url, world.zapi_tenant, phone)
        async with asyncio.timeout(_TURN_SECONDS):
            await _crash_after_the_final_commit(client, world, "pg-recover-1", phone)
            async with enforced_direct.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE sessions SET is_paused = true, paused_until = "
                        "clock_timestamp() + interval '1 hour' WHERE tenant_id = "
                        ":t AND user_id = :p"
                    ),
                    {"t": UUID(world.zapi_tenant), "p": phone},
                )
            tick = await credit_metering.CreditResponsePublisher(
                webhook.CREDIT_REPLY_SENDER, session_factory=get_db_session_factory()
            ).run_once()
        replies = _sent_to(webhook.send_text_message, phone)
    assert tick is not None and "failed" in tick.outcomes
    assert replies == []
    seen = await _webhook_rows(enforced_direct, UUID(world.zapi_tenant))
    [(_effect, state, payload, _attempts)] = seen.outbox
    assert state == "failed" and "phone" not in payload


async def test_the_publisher_holds_no_lock_nor_transaction_across_the_provider(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    redis_down: None,
) -> None:
    """Review probe C: through the real send adapter, while the recovered
    reply's provider call is in flight, its row is ``sending`` (committed),
    neither it nor the patient's session row is locked, and no backend is
    idle in transaction."""

    from src.api.routes import webhook
    from src.repositories.database import get_db_session_factory

    _no_app_publisher(monkeypatch)
    monkeypatch.setattr(credit_metering, "PUBLISHER_MIN_AGE_SECONDS", 0)
    phone = "5511900019003"
    entered, leave = asyncio.Event(), asyncio.Event()
    probes: dict[str, Any] = {}
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        await _seed_patient(enforced_pg.url, world.zapi_tenant, phone)
        sid = await _patient_session(enforced_direct, world.zapi_tenant, phone)

        async def provider(
            _inst: str, _tok: str, to: str, body: str, *, client_token: str = ""
        ) -> dict[str, Any]:
            entered.set()
            await leave.wait()
            return {"messageId": "pg-slow"}

        async with asyncio.timeout(_TURN_SECONDS):
            await _crash_after_the_final_commit(client, world, "pg-nolock-1", phone)
            [row] = (
                await _webhook_rows(enforced_direct, UUID(world.zapi_tenant))
            ).outbox
            with monkeypatch.context() as patch:
                patch.setattr(webhook, "send_text_message", provider)
                tick_task = asyncio.create_task(
                    credit_metering.CreditResponsePublisher(
                        webhook.CREDIT_REPLY_SENDER,
                        session_factory=get_db_session_factory(),
                    ).run_once()
                )
                await entered.wait()
                async with enforced_direct.connect() as connection:
                    transaction = await connection.begin()
                    probes["row"] = (
                        await connection.execute(
                            text(
                                "SELECT state FROM credit_response_outbox WHERE "
                                "effect_id = :e FOR UPDATE NOWAIT"
                            ),
                            {"e": row[0]},
                        )
                    ).scalar_one()
                    probes["session"] = (
                        await connection.execute(
                            text(
                                "SELECT 1 FROM sessions WHERE id = :s FOR UPDATE NOWAIT"
                            ),
                            {"s": sid},
                        )
                    ).scalar_one()
                    await transaction.rollback()
                    probes["idle_in_tx"] = (
                        await connection.execute(
                            text(
                                "SELECT count(*) FROM pg_stat_activity WHERE datname "
                                "= current_database() AND pid <> pg_backend_pid() AND "
                                "state LIKE 'idle in transaction%'"
                            )
                        )
                    ).scalar_one()
                leave.set()
                tick = await tick_task
    assert probes == {"row": "sending", "session": 1, "idle_in_tx": 0}
    assert tick is not None and "delivered" in tick.outcomes
    after = await _webhook_rows(enforced_direct, UUID(world.zapi_tenant))
    assert [state for _e, state, _p, _a in after.outbox] == ["delivered"]


async def test_concurrent_file_handoff_redeliveries_hand_off_once(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    redis_down: None,
) -> None:
    """Review probe D: two route sessions run the forward-on-request file
    handoff of the SAME Meta message at once (Redis down): one handoff, one
    trace, one confirmation, one ``duplicate_message``."""

    from src.api.routes import webhook

    _no_app_publisher(monkeypatch)
    phone = "5511900019004"
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        async with asyncio.timeout(_TURN_SECONDS):
            raw, headers = _signed_meta(
                world,
                {
                    "from": phone,
                    "id": "wamid.pg-concurrent-doc",
                    "type": "document",
                    "document": {"id": "pg-doc", "mime_type": "application/pdf"},
                },
            )
            ack = await client.post("/v1/webhook/meta", content=raw, headers=headers)
            raw, headers = _signed_meta(
                world,
                {
                    "from": phone,
                    "id": "wamid.pg-concurrent-forward",
                    "type": "text",
                    "text": {"body": "Pode encaminhar para a equipe"},
                },
            )
            answers = await asyncio.gather(
                *(
                    client.post("/v1/webhook/meta", content=raw, headers=headers)
                    for _ in range(2)
                )
            )
        confirmations = [
            text_
            for text_ in _sent_to(webhook.send_meta_cloud_text, phone)
            if text_ in (webhook._FILE_FORWARDED_MSG_PT, webhook._FILE_FORWARDED_MSG_EN)
        ]
    assert ack.json()["action"] == "file_ack"
    assert sorted(json.dumps(a.json(), sort_keys=True) for a in answers) == sorted(
        [
            json.dumps(
                {"ok": True, "replied": True, "action": "file_handoff"}, sort_keys=True
            ),
            json.dumps({"ok": True, "skipped": "duplicate_message"}, sort_keys=True),
        ]
    )
    seen = await _webhook_rows(enforced_direct, UUID(world.meta_tenant))
    assert len(seen.handoffs) == 1 and seen.handoff_requests == 1
    assert seen.entries == {"debt": 1_000}
    assert len(confirmations) == 1


async def test_a_clinic_delete_during_the_webhook_final_transaction(
    enforced_pg: _PG,
    enforced_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    redis_down: None,
) -> None:
    """Review probe E: ``DELETE`` of the tenant while the webhook final
    transaction holds the tenant ``FOR SHARE`` (paused at its fence): no
    deadlock; the delete waits for the commit and then removes everything
    (``send_queued``: the reply row went with the tenant)."""

    _no_app_publisher(monkeypatch)
    gate = _Gate()
    _pause(
        monkeypatch,
        CreditRepository,
        "hold_webhook_claim_final_fence",
        gate,
        calls=frozenset({1}),
    )
    phone = "5511900019005"
    async with _running_app(enforced_pg, monkeypatch) as running:
        world, client = running.world, running.client
        await _seed_patient(enforced_pg.url, world.zapi_tenant, phone)
        async with asyncio.timeout(_TURN_SECONDS):
            post = asyncio.create_task(
                client.post(
                    f"/v1/webhook/zapi/{world.zapi_token}",
                    json=_zapi_message(world, "pg-delete-1", phone, "Olá"),
                )
            )
            await gate.reached.wait()

            async def delete() -> str:
                async with enforced_direct.begin() as connection:
                    await connection.execute(text("SET LOCAL lock_timeout = '20s'"))
                    await connection.execute(
                        text("DELETE FROM tenants WHERE id = :t"),
                        {"t": UUID(world.zapi_tenant)},
                    )
                return "deleted"

            deleter = asyncio.create_task(delete())
            await _until_lock_waiters(enforced_direct, 1, query="%DELETE FROM tenants%")
            gate.opened.set()
            answer = await post
            outcome = await deleter
    assert outcome == "deleted"
    assert answer.status_code == 200
    assert answer.json() in (
        {"ok": True, "replied": True},
        {"ok": True, "replied": False, "reason": "send_queued"},
        {"ok": True, "replied": False, "reason": "session_gone"},
    )
    seen = await _webhook_rows(enforced_direct, UUID(world.zapi_tenant))
    assert seen.claims == [] and seen.outbox == []
