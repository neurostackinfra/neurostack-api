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
from datetime import datetime, timezone
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
    with _fake_llm_and_providers():
        async with app.router.lifespan_context(app):
            from src.services.credit_switch import credit_switch_effective

            assert credit_switch_effective(), "the real switch is not effective"
            transport = httpx.ASGITransport(app=app, client=("testclient", 50000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                yield _App(client=client, world=world, twin_session=str(twin))


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
