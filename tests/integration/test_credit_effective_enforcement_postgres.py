"""Per-builder enforcement on real PostgreSQL (cobranca-credito-por-builder G2).

``CreditRepository.effective_enforcement(builder_id)`` is the request path's
reader of the credit mode: the global ``credit_enforcement_at`` when set,
else the ``occurred_at`` of the builder's latest event in
``credit_enforcement_marks`` when it is a ``mark``, else none -- with its
origin.  ``enforcement_at()`` (the global reader) does not change.  Proven
here, every database a clone of one migrated template (the 046 one for the
gate):

* the rule: global wins, then the latest mark, an unmark ends it, a
  re-mark starts at its own instant, a request without a builder only ever
  sees the global; the batch read, the writer (strictly increasing
  ``occurred_at``), "any mark" and the window state of the operator's
  ``mark`` refusal;
* the 047 gate: on a 046 database (no table) the marks are never queried --
  by the repository, by a request through the real app or by the credit
  read API -- and nothing breaks;
* a marked builder with only a courtesy lot is debited by expiry (FEFO) on
  ``POST /v1/events`` by API key, Z-API and Meta, and reads ``mode``
  ``enforced``; an unmarked builder in the same test, without courtesy
  (one with courtesy is under the courtesy rule, debito-cortesia-automatico),
  reads ``observational`` and its ledger never moves;
* the channels: Uazapi and Evolution traffic of the marked builder stays
  observational (recorded, never silent, never charged), and its outgoing
  media there is recorded observationally while Z-API and Meta media is
  charged;
* D1 per tenant, both ways: a tenant seen enforced by a mark gets 503
  ``credit_enforcement_unavailable`` when its mark becomes unreadable (a
  failed read or the gate closed) while another builder's tenant answers as
  before; a successful "not marked" read clears it; seeing a mark never sets
  the global latch;
* the race: the tenant relinked between the capture and the enforced
  transaction's lock is 503 before any write on ``POST /v1/events`` (a retry
  then decides again), on Z-API and Meta (the message is lost) and on a
  knowledge upload; a tenant unlinked from its marked builder is never
  ``credit_pending``; relinked during the LLM call, the builder locked at
  the final transaction is never charged.
* debito-cortesia-automatico Group 2, through the real app on
  ``POST /v1/events``, Z-API and Meta: a builder with no global and no mark
  that ever received courtesy is captured on the courtesy rule -- charged
  from courtesy lots only (even in an ASAAS quarantine and with an
  unusable binding coverage, where its marked twin is ``credit_pending``),
  ``blocked`` answered 402/200 ``credit_suspended`` with nothing written (no
  session, no reply, no LGPD notice) until a new grant unblocks it; the
  race: a grant or a removal between the capture and the lock is 503 before
  any write, the final transaction never re-reads the mode (a removal or a
  mark during the LLM call changes nothing of its debit) and a tenant
  relinked to another builder is charged to neither; the rule's per-tenant
  D1; a charged row of an ``unknown`` turn carries ``unknown`` in its
  ``decision_digest``, a production one ``production``; the publisher and
  the orphan pass run on a courtesy lot alone; and the shared block gate
  (``tenant_suspended``, ``reservation_verdict``) reports the rule's block
  and never a stale mark-era episode for a charged builder.

How each proof shows that an unmarked builder is never charged, blocked or
written to: its credit rows (accounts, effects, ledger, lots, allocations,
episodes, enforced claims, enforced egress) are counted before and after and
must not move, and its answers are the observational ones (``main``'s).

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import src.models  # noqa: F401  (registers every mapper)
from src.repositories.credit import (
    NOT_ENFORCED,
    CreditRepository,
    EffectiveEnforcement,
    EnforcementMarksUnavailable,
)
from src.services import credit_inventory, credit_metering
from tests.integration import test_credit_metering_postgres as metering_suite
from tests.integration.credit_rollout_world import (
    civil_today,
    debt_rows,
    effective_of,
    execute_on,
    force_credit_switch,
    forget_enforcement_latches,
    grant_courtesy,
    make_cycle,
    make_subscription,
    make_topup,
    mark_events,
    paid_lot_moves,
    record_mark,
    relink_tenant,
    remove_courtesy,
)
from tests.integration.test_asaas_billing_postgres import _validated_admin_url
from tests.integration.test_migration_036_postgres import _Cluster, _migrate

pytestmark = pytest.mark.integration

_TURN_SECONDS = 60
_HOUR = timedelta(hours=1)
# The builder-scoped credit tables a request could write: an unmarked
# builder's counts must never move (the observational effects and claims
# are written for every builder, as before, and are checked apart).
_BUILDER_TABLES = (
    "credit_accounts",
    "credit_effects",
    "credit_ledger_entries",
    "credit_debts",
    "credit_lots",
    "credit_allocations",
    "credit_lifecycle_episodes",
)


# ---------------------------------------------------------------------------
# Databases: one template at head (047) and one at 046, each test a clone.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Databases:
    cluster: _Cluster
    head: str
    below: str


@pytest.fixture(scope="module")
async def databases() -> AsyncIterator[_Databases]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    cluster = _Cluster(admin_url=admin_url, admin=admin, created=[])
    try:
        head = await cluster.fresh()
        await _migrate(cluster.url(head), "upgrade", "head")
        below = await cluster.fresh()
        await _migrate(cluster.url(below), "upgrade", "046")
        yield _Databases(cluster=cluster, head=head, below=below)
    finally:
        for created in list(reversed(cluster.created)):
            await cluster.drop(created)
        await admin.dispose()


@asynccontextmanager
async def _clone(databases: _Databases, template: str) -> AsyncIterator[Any]:
    name = await databases.cluster.clone(template)
    try:
        yield metering_suite._PG(
            cluster=databases.cluster, url=databases.cluster.url(name)
        )
    finally:
        await databases.cluster.drop(name)


@pytest.fixture
async def pg(databases: _Databases) -> AsyncIterator[Any]:
    async with _clone(databases, databases.head) as cloned:
        yield cloned


@pytest.fixture
async def pg046(databases: _Databases) -> AsyncIterator[Any]:
    async with _clone(databases, databases.below) as cloned:
        yield cloned


@pytest.fixture
async def direct(pg: Any) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg.url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def _fresh_latches() -> Iterator[None]:
    """Every test is a fresh process for the three D1 latches."""

    forget_enforcement_latches()
    yield
    forget_enforcement_latches()


@pytest.fixture(autouse=True)
def _patient_observational_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    """These proofs COUNT the observational rows ("never silent"), so the
    best-effort writer gets a wide budget: its 250 ms budget and server
    backstops are main's and pinned by test_credit_metering_postgres.py; on
    a loaded machine they drop a row by design (``timeout``), which is not
    what is proven here."""

    def patient(module: Any, name: str) -> None:
        original = getattr(module, name)

        async def wider(*args: Any, **kwargs: Any) -> Any:
            kwargs["budget_seconds"] = 10.0
            return await original(*args, **kwargs)

        monkeypatch.setattr(module, name, wider)

    patient(credit_metering, "record_observational_trace")
    patient(credit_inventory, "record_observational_upload")
    patient(credit_inventory, "record_observational_egress")
    monkeypatch.setattr(credit_metering, "OBSERVATIONAL_SERVER_TIMEOUT", "10s")
    monkeypatch.setattr(credit_inventory, "OBSERVATIONAL_SERVER_TIMEOUT", "10s")


@asynccontextmanager
async def _app(pg: Any, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """The real app (lifespan, middleware, pool) on ``pg`` with a seeded
    world, as the metering suite runs it; its switch reads the database."""

    async with metering_suite._running_app(pg, monkeypatch) as running:
        forget_enforcement_latches()
        yield running


# ---------------------------------------------------------------------------
# Seeds and probes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Builder:
    builder: UUID
    tenant: UUID
    session: UUID
    api_key: str


async def _builder_with_api_tenant(
    engine: AsyncEngine, name: str = "Unmarked builder"
) -> _Builder:
    """A builder, one clinic with an API key and a session of it."""

    from src.api.middleware.auth import pwd_context
    from src.models.builder import Builder
    from src.models.session import Session
    from src.models.tenant import Tenant

    seeded = _Builder(uuid4(), uuid4(), uuid4(), secrets.token_urlsafe(24))
    async with AsyncSession(engine) as session, session.begin():
        session.add(Builder(id=seeded.builder, name=name))
        await session.flush()
        session.add(
            Tenant(
                id=seeded.tenant,
                name=f"{name} clinic",
                slug=f"effective-{seeded.tenant.hex}",
                api_key_hash=pwd_context.hash(seeded.api_key),
                plan_type="clinic",
                builder_id=seeded.builder,
                config={},
            )
        )
        await session.flush()
        session.add(
            Session(
                id=seeded.session,
                tenant_id=seeded.tenant,
                channel="test",
                cognitive_state={},
                session_metadata={},
            )
        )
    return seeded


async def _builder_rows(engine: AsyncEngine, builder_id: UUID) -> dict[str, int]:
    """The builder's rows in every builder-scoped credit table, plus its
    ENFORCED claims and egress (the observational ones are not charges)."""

    counts: dict[str, int] = {}
    async with engine.connect() as connection:
        for table in _BUILDER_TABLES:
            counts[table] = int(
                await connection.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE builder_id = :b"),
                    {"b": builder_id},
                )
            )
        counts["enforced_claims"] = int(
            await connection.scalar(
                text(
                    "SELECT count(*) FROM credit_ingress_claims WHERE "
                    "builder_id = :b AND mode = 'enforced'"
                ),
                {"b": builder_id},
            )
        )
        counts["enforced_egress"] = int(
            await connection.scalar(
                text(
                    "SELECT count(*) FROM credit_egress_deliveries WHERE "
                    "builder_id = :b AND mode = 'enforced'"
                ),
                {"b": builder_id},
            )
        )
    return counts


async def _tenant_rows(engine: AsyncEngine, tenant_id: UUID) -> dict[str, int]:
    """What one request of a tenant may have written, by table."""

    tables = {
        "claims": "credit_ingress_claims",
        "effects": "credit_effects",
        "observations": "credit_observational_effects",
        "outbox": "credit_response_outbox",
        "documents": "knowledge_documents",
        "events": "events",
    }
    async with engine.connect() as connection:
        return {
            name: int(
                await connection.scalar(
                    text(f"SELECT count(*) FROM {table} WHERE tenant_id = :t"),
                    {"t": tenant_id},
                )
            )
            for name, table in tables.items()
        }


async def _ledger(engine: AsyncEngine, builder_id: UUID) -> list[tuple[str, int, Any]]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT entry_type, amount_mc, lot_id FROM credit_ledger_entries "
                "WHERE builder_id = :b ORDER BY occurred_at, id"
            ),
            {"b": builder_id},
        )
        return [(str(kind), int(amount), lot) for kind, amount, lot in rows]


async def _debits(engine: AsyncEngine, builder_id: UUID) -> list[tuple[str, int, Any]]:
    """The builder's ``debit`` entries with their effect's kind."""

    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT f.effect_kind, e.amount_mc, e.lot_id FROM "
                "credit_ledger_entries e JOIN credit_effects f ON f.builder_id = "
                "e.builder_id AND f.effect_id = e.effect_id WHERE e.builder_id = :b "
                "AND e.entry_type = 'debit' ORDER BY e.occurred_at, e.id"
            ),
            {"b": builder_id},
        )
        return [(str(kind), int(amount), lot) for kind, amount, lot in rows]


async def _unlinked_home(engine: AsyncEngine, builder_id: UUID) -> tuple[UUID, UUID]:
    """The production shape of #219: a builder user whose home tenant is
    linked to no builder (``tenants.builder_id`` NULL); returns the tenant
    and the user (the JWT ``sub``)."""

    from src.models.tenant import Tenant

    home, user = uuid4(), uuid4()
    async with AsyncSession(engine) as session, session.begin():
        session.add(
            Tenant(
                id=home,
                name="Builder home (unlinked)",
                slug=f"home-{home.hex}",
                api_key_hash=f"unused-{home.hex}",
                builder_id=None,
                config={},
            )
        )
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (id, tenant_id, email, password_hash, role, "
                "builder_id, is_active, created_at, updated_at) VALUES (:id, "
                ":tenant, :email, 'unused', 'builder', :builder, true, now(), now())"
            ),
            {
                "id": user,
                "tenant": home,
                "email": f"builder-{home.hex[:12]}@example.com",
                "builder": builder_id,
            },
        )
    return home, user


async def _grant_courtesy(engine: AsyncEngine, builder_id: UUID, credits: int) -> UUID:
    from src.services import credit_courtesy

    async with AsyncSession(engine) as session, session.begin():
        granted = await credit_courtesy.grant(
            session,
            builder_id=builder_id,
            credits=credits,
            reason="per-builder charge test",
            actor_user_id=uuid4(),
            idempotency_key=f"effective-{uuid4()}",
        )
    return granted.lot_id


def _api(key: str, idempotency_key: str | None) -> dict[str, str]:
    headers = {"X-API-Key": key}
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _builder_jwt(
    builder_id: UUID, tenant_id: UUID, subject: UUID | None = None
) -> dict[str, str]:
    import jwt

    from src.config.settings import settings

    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(subject or uuid4()),
            "tenant_id": str(tenant_id),
            "role": "builder",
            "builder_id": str(builder_id),
            "iat": now - timedelta(minutes=1),
            "exp": now + _HOUR,
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def _zapi(world: Any, message_id: str, phone: str) -> dict[str, Any]:
    return {
        "instanceId": world.zapi_instance,
        "messageId": message_id,
        "phone": phone,
        "fromMe": False,
        "text": {"message": "Olá, quero agendar"},
    }


def _meta_message(message_id: str, phone: str) -> dict[str, Any]:
    return {
        "from": phone,
        "id": message_id,
        "type": "text",
        "text": {"body": "Olá, quero agendar"},
    }


@contextmanager
def _statements(engine: AsyncEngine) -> Iterator[list[str]]:
    seen: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)


def _app_engine() -> AsyncEngine:
    from src.repositories.database import get_db_engine

    engine: AsyncEngine = get_db_engine()
    return engine


# ---------------------------------------------------------------------------
# Pure: the verdict, the per-tenant D1 set and the reparent rule.
# ---------------------------------------------------------------------------


def test_the_verdict_is_the_instant_and_the_origin_only() -> None:
    at = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
    builder = uuid4()
    assert EffectiveEnforcement(at, "mark", builder_id=builder) == (
        EffectiveEnforcement(at, "mark")
    )
    assert EffectiveEnforcement(None, None, mark_unread=True) == NOT_ENFORCED
    assert EffectiveEnforcement(at, "mark") != EffectiveEnforcement(at, "global")
    assert not NOT_ENFORCED.enforced and EffectiveEnforcement(at, "mark").enforced


def test_the_reparent_rule_applies_to_a_captured_mark_or_courtesy_rule() -> None:
    at = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
    builder, other = uuid4(), uuid4()
    mark = EffectiveEnforcement(at, "mark", builder_id=builder)
    # debito-cortesia-automatico (DESIGN item 11): the courtesy rule too.
    rule = EffectiveEnforcement(at, "courtesy", builder_id=builder, state="charged")
    reparented = credit_metering.reparented_since_capture
    for captured in (mark, rule):
        assert reparented(captured, other)
        assert not reparented(captured, builder)
        assert not reparented(captured, None)  # standalone: LOW-5's own branch
    assert not reparented(EffectiveEnforcement(at, "global"), other)
    assert not reparented(None, other)


# ---------------------------------------------------------------------------
# The repository: the rule, the gate and the shared mark primitives.
# ---------------------------------------------------------------------------


async def test_the_global_wins_then_the_latest_mark_then_none(
    pg: Any, direct: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_credit_switch(monkeypatch)
    marked = await _builder_with_api_tenant(direct, "Marked builder")
    other = await _builder_with_api_tenant(direct)
    sessions = async_sessionmaker(direct, expire_on_commit=False)

    async def effective(builder_id: UUID | None) -> EffectiveEnforcement:
        async with sessions() as session:
            try:
                return await CreditRepository(session).effective_enforcement(builder_id)
            finally:
                await session.rollback()

    assert await effective(marked.builder) == NOT_ENFORCED  # no event at all
    first = await record_mark(direct, marked.builder)
    found = await effective(marked.builder)
    assert (found.at, found.origin, found.builder_id) == (first, "mark", marked.builder)
    assert await effective(other.builder) == NOT_ENFORCED
    assert await effective(None) == NOT_ENFORCED  # no builder: the global only
    await record_mark(direct, marked.builder, "unmark")
    assert await effective(marked.builder) == NOT_ENFORCED
    again = await record_mark(direct, marked.builder)
    assert again > first
    assert await effective(marked.builder) == EffectiveEnforcement(again, "mark")
    assert [kind for kind, _ in await mark_events(direct, marked.builder)] == [
        "mark",
        "unmark",
        "mark",
    ]
    async with sessions() as session:
        repository = CreditRepository(session)
        assert await repository.tenant_effective_enforcement(
            marked.tenant
        ) == EffectiveEnforcement(again, "mark")
        assert await repository.tenant_effective_enforcement(other.tenant) == (
            NOT_ENFORCED
        )
        assert await repository.tenant_effective_enforcement(uuid4()) == (NOT_ENFORCED)
        assert await repository.enforcement_at() is None  # the global reader
        # The global, set in this transaction only (rolled back), wins over
        # every mark and is what a request without a builder sees.
        armed = datetime.now(timezone.utc) - timedelta(days=3)
        await session.execute(
            text(
                "INSERT INTO credit_enforcement_state (id, credit_enforcement_at, "
                "set_by, set_at) VALUES (1, :at, 'effective-test', :at)"
            ),
            {"at": armed},
        )
        for builder_id in (marked.builder, other.builder, None):
            assert await repository.effective_enforcement(builder_id) == (
                EffectiveEnforcement(armed, "global")
            )
        assert await repository.tenant_effective_enforcement(marked.tenant) == (
            EffectiveEnforcement(armed, "global")
        )
        await session.rollback()
    assert await effective(marked.builder) == EffectiveEnforcement(again, "mark")


async def test_the_shared_mark_primitives(
    pg: Any, direct: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The batch read is ONE statement; the writer keeps ``occurred_at``
    strictly increasing even when the clock is not past the latest event;
    "any mark" sees an ``unmark`` too; the window state of the ``mark``
    refusal sees an opening of the current window."""

    from src.services.credit_allocations import accounting_window

    force_credit_switch(monkeypatch)
    first = await _builder_with_api_tenant(direct, "First")
    second = await _builder_with_api_tenant(direct, "Second")
    third = await _builder_with_api_tenant(direct, "Third")
    sessions = async_sessionmaker(direct, expire_on_commit=False)
    async with sessions() as session:
        repository = CreditRepository(session)
        assert await repository.any_enforcement_mark() is False
        assert await repository.latest_enforcement_marks([]) == {}
        await session.rollback()
    await record_mark(direct, first.builder)
    await record_mark(direct, first.builder, "unmark")
    marked_at = await record_mark(direct, second.builder)
    async with sessions() as session:
        repository = CreditRepository(session)
        with _statements(direct) as seen:
            latest = await repository.latest_enforcement_marks(
                [first.builder, second.builder, third.builder, first.builder]
            )
        assert len([s for s in seen if "credit_enforcement_marks" in s]) == 1
        assert latest is not None and set(latest) == {first.builder, second.builder}
        assert latest[first.builder].kind == "unmark"
        assert (latest[second.builder].kind, latest[second.builder].occurred_at) == (
            "mark",
            marked_at,
        )
        assert await repository.any_enforcement_mark() is True
        # The marked builders without a builder in hand (the workers'): only
        # a latest ``mark`` counts, at its own instant.
        marked_now = await repository.marked_builders()
        assert {b: at for b, at in marked_now.items() if b in latest} == {
            second.builder: marked_at
        }
        await session.rollback()

    # The writer: a database clock behind the latest event still yields a
    # later instant (ties would break on the random id).
    async with sessions() as session, session.begin():
        repository = CreditRepository(session)

        async def behind() -> datetime:
            return marked_at - _HOUR

        monkeypatch.setattr(repository, "database_now", behind)
        written = await repository.insert_enforcement_mark(
            builder_id=second.builder,
            kind="unmark",
            actor_user_id=uuid4(),
            reason="writer test",
        )
        with pytest.raises(ValueError):
            await repository.insert_enforcement_mark(
                builder_id=second.builder,
                kind="pause",
                actor_user_id=uuid4(),
                reason="writer test",
            )
    assert written.occurred_at == marked_at + timedelta(microseconds=1)
    async with sessions() as session:
        assert second.builder not in await CreditRepository(session).marked_builders()
        await session.rollback()
    assert (await mark_events(direct, second.builder))[-1] == (
        "unmark",
        written.occurred_at,
    )

    now = datetime.now(timezone.utc)
    window = accounting_window((), now)
    async with sessions() as session:
        state = await CreditRepository(session).mark_window_state(
            second.builder, at=now
        )
        await session.rollback()
    assert state.window_start == window.start and state.window_opened is False
    assert state.latest_mark is not None and state.latest_mark.occurred_at == marked_at
    account = uuid4()
    async with direct.begin() as connection:
        await connection.execute(
            text("INSERT INTO credit_accounts (id, builder_id) VALUES (:a, :b)"),
            {"a": account, "b": second.builder},
        )
        await connection.execute(
            text(
                "INSERT INTO credit_allocations (id, builder_id, account_id, "
                "window_start, window_end, category, allocation_kind, source_key, "
                "units, target_mc, charged_mc) VALUES (:id, :b, :a, :start, :end, "
                "'rag', 'baseline', :key, 0, 0, 0)"
            ),
            {
                "id": uuid4(),
                "b": second.builder,
                "a": account,
                "start": window.start,
                "end": window.end,
                "key": f"opening:test:{uuid4()}",
            },
        )
    async with sessions() as session:
        opened = await CreditRepository(session).mark_window_state(
            second.builder, at=now
        )
        never = await CreditRepository(session).mark_window_state(third.builder, at=now)
        await session.rollback()
    assert opened.window_opened is True
    assert never.latest_mark is None and never.window_opened is False


async def test_below_047_the_marks_table_is_never_queried(
    pg046: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 046 database (the table does not exist): the gate is closed on the
    reading, every mark read is skipped without a query -- the transaction
    stays usable --, the readers that must not guess refuse, and the real
    app answers a production request, a webhook and the credit read API as
    before, without naming the table in any statement."""

    engine = create_async_engine(pg046.url, poolclass=NullPool)
    try:
        builder = await _builder_with_api_tenant(engine, "Builder at 046")
        force_credit_switch(monkeypatch, revision="046")
        async with AsyncSession(engine) as session:
            repository = CreditRepository(session)
            with _statements(engine) as seen:
                found = await repository.effective_enforcement(builder.builder)
                by_tenant = await repository.tenant_effective_enforcement(
                    builder.tenant
                )
                assert await repository.any_enforcement_mark() is False
                assert await repository.marked_builders() == {}
                assert (
                    await repository.latest_enforcement_marks([builder.builder]) is None
                )
                with pytest.raises(EnforcementMarksUnavailable):
                    await repository.latest_enforcement_mark(builder.builder)
                with pytest.raises(EnforcementMarksUnavailable):
                    await repository.mark_window_state(
                        builder.builder, at=datetime.now(timezone.utc)
                    )
                with pytest.raises(EnforcementMarksUnavailable):
                    await repository.insert_enforcement_mark(
                        builder_id=builder.builder,
                        kind="mark",
                        actor_user_id=uuid4(),
                        reason="gate closed",
                    )
                assert (await session.execute(text("SELECT 1"))).scalar_one() == 1
            await session.rollback()
        assert found == NOT_ENFORCED and found.mark_unread is True
        assert by_tenant == NOT_ENFORCED and by_tenant.mark_unread is True
        assert seen and not any("credit_enforcement_marks" in s for s in seen)
    finally:
        await engine.dispose()
    monkeypatch.undo()

    # The real app on the 046 database: its own switch reads 046 (effective,
    # gate closed).
    async with _app(pg046, monkeypatch) as running:
        world, client = running.world, running.client
        from src.services.credit_switch import credit_switch_reading

        assert credit_switch_reading().revision == "046"
        with _statements(_app_engine()) as seen:
            async with asyncio.timeout(_TURN_SECONDS):
                answer = await client.post(
                    "/v1/events",
                    json=metering_suite._events_body(world.api_session),
                    headers=_api(world.api_key, "below-047-1"),
                )
                zapi = await client.post(
                    f"/v1/webhook/zapi/{world.zapi_token}",
                    json=_zapi(world, "below-047-zapi-1", "5511900007001"),
                )
                summary = await client.get(
                    "/v1/credits/summary",
                    headers=_builder_jwt(UUID(world.builder), UUID(world.api_tenant)),
                )
    assert answer.status_code == 200, answer.text
    assert zapi.json() == {"ok": True, "replied": True}, zapi.text
    assert summary.status_code == 200, summary.text
    assert summary.json()["state"]["mode"] == "observational"
    assert seen and not any("credit_enforcement_marks" in s for s in seen)


# ---------------------------------------------------------------------------
# The request path: charged by its mark, the unmarked untouched.
# ---------------------------------------------------------------------------


async def test_a_marked_builder_is_charged_fefo_and_reads_enforced(
    pg: Any, direct: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Success Criteria 3: the marked builder's courtesy lot pays its API
    key, Z-API and Meta decisions (one trace each, by expiry) and its
    knowledge upload's reservation, ``mode`` is ``enforced`` for it; the
    unmarked builder of the same test stays ``observational`` and nothing of
    its credit moves.  None of it sets a global latch (the inventory's
    included: the upload reads its mode).

    debito-cortesia-automatico: the unmarked builder has NO courtesy here --
    an unmarked builder with courtesy is under the courtesy rule and charged
    (tests/integration/test_credit_courtesy_rule_postgres.py); one that
    never had courtesy is exactly the unmarked builder of before."""

    from src.api.routes import webhook

    async with _app(pg, monkeypatch) as running:
        world, client = running.world, running.client
        marked = UUID(world.builder)
        other = await _builder_with_api_tenant(direct)
        lot = await _grant_courtesy(direct, marked, 100)
        other_before = await _builder_rows(direct, other.builder)
        await record_mark(direct, marked)
        async with asyncio.timeout(_TURN_SECONDS):
            events = await client.post(
                "/v1/events",
                json=metering_suite._events_body(world.api_session),
                headers=_api(world.api_key, "charged-1"),
            )
            zapi = await client.post(
                f"/v1/webhook/zapi/{world.zapi_token}",
                json=_zapi(world, "charged-zapi-1", "5511900007101"),
            )
            raw, headers = metering_suite._signed_meta(
                world, _meta_message("wamid.charged-meta-1", "5511900007102")
            )
            meta = await client.post("/v1/webhook/meta", content=raw, headers=headers)
            unmarked = await client.post(
                "/v1/events",
                json=metering_suite._events_body(str(other.session)),
                headers=_api(other.api_key, "unmarked-1"),
            )
            mine = await client.get(
                "/v1/credits/summary",
                headers=_builder_jwt(marked, UUID(world.api_tenant)),
            )
            theirs = await client.get(
                "/v1/credits/summary", headers=_builder_jwt(other.builder, other.tenant)
            )
            # Decision 15: the builder's own unlinked home tenant reads its
            # account (#219) -- and its mode -- from scope.builder_id.
            home, user = await _unlinked_home(direct, marked)
            through_home = await client.get(
                "/v1/credits/summary", headers=_builder_jwt(marked, home, user)
            )
            # The marked builder's knowledge upload: its mode is read by the
            # inventory (read_credit_mode) and its reservation is charged.
            upload = await client.post(
                "/v1/knowledge/documents",
                json={"content": "Atendemos de segunda a sexta.", "metadata": {}},
                headers=_api(world.api_key, None),
            )
            after_upload = await client.get(
                "/v1/credits/summary",
                headers=_builder_jwt(marked, UUID(world.api_tenant)),
            )
        replies = webhook.send_text_message.await_count
        meta_replies = webhook.send_meta_cloud_text.await_count
        seen_global = credit_metering.enforcement_seen()
        inventory_global = credit_inventory._enforcement_seen
        marked_seen = {
            tenant: credit_metering.enforcement_mark_seen(UUID(tenant))
            for tenant in (world.api_tenant, world.zapi_tenant, world.meta_tenant)
        }
        other_seen = credit_metering.enforcement_mark_seen(other.tenant)

    assert events.status_code == 200, events.text
    assert zapi.json() == {"ok": True, "replied": True}, zapi.text
    assert meta.status_code == 200, meta.text
    assert replies >= 1 and meta_replies >= 1
    assert unmarked.status_code == 200, unmarked.text
    assert upload.status_code == 201, upload.text
    # FEFO: the only lot is the courtesy one: three traces of one credit,
    # then the upload's RAG reservation.
    ledger = await _ledger(direct, marked)
    assert [(kind, amount) for kind, amount, _ in ledger][:1] == [("grant", 100_000)]
    assert {kind for kind, _, _ in ledger[1:]} == {"debit"}
    debits = await _debits(direct, marked)
    assert [(kind, amount) for kind, amount, _ in debits[:3]] == [("trace", 1_000)] * 3
    [(rag_kind, rag_mc, _)] = debits[3:]
    assert rag_kind == "rag" and rag_mc > 0
    assert {lot_id for _, _, lot_id in debits} == {lot}
    marked_rows = await _builder_rows(direct, marked)
    assert marked_rows["credit_debts"] == 0
    assert marked_rows["enforced_claims"] == 3
    # The first debit opened the accounting window at the mark: a free
    # baseline per category (the window open at the builder's instant).
    async with direct.connect() as connection:
        openings = sorted(
            (str(category), str(kind), int(charged))
            for category, kind, charged in await connection.execute(
                text(
                    "SELECT category, allocation_kind, charged_mc FROM "
                    "credit_allocations WHERE builder_id = :b AND "
                    "allocation_kind IN ('baseline', 'maintenance')"
                ),
                {"b": marked},
            )
        )
    assert openings == [("media", "baseline", 0), ("rag", "baseline", 0)]
    assert mine.status_code == 200, mine.text
    assert mine.json()["state"]["mode"] == "enforced"
    assert mine.json()["consumption"]["mode"] == "enforced"
    assert mine.json()["state"]["enforcement_at"] is not None
    assert mine.json()["balance"]["courtesy_available_mc"] == 97_000
    assert through_home.status_code == 200, through_home.text
    assert through_home.json()["state"]["mode"] == "enforced"
    assert through_home.json()["balance"]["courtesy_available_mc"] == 97_000
    assert after_upload.json()["balance"]["courtesy_available_mc"] == (97_000 - rag_mc)
    # The unmarked builder: observational, its lot untouched, no new row.
    assert theirs.status_code == 200, theirs.text
    assert theirs.json()["state"]["mode"] == "observational"
    assert theirs.json()["consumption"]["mode"] == "observational"
    assert theirs.json()["state"]["enforcement_at"] is None
    assert theirs.json()["balance"]["courtesy_available_mc"] == 0
    assert await _builder_rows(direct, other.builder) == other_before
    assert await _ledger(direct, other.builder) == []
    observed = await _tenant_rows(direct, other.tenant)
    assert observed["observations"] == 1 and observed["effects"] == 0
    # Seeing marks never sets a global latch -- the inventory's neither,
    # after the marked upload --; it set the per-tenant one.
    assert seen_global is False
    assert inventory_global is False
    assert marked_seen == {
        world.api_tenant: True,
        world.zapi_tenant: True,
        world.meta_tenant: True,
    }
    assert other_seen is False


async def test_uazapi_and_evolution_stay_observational_for_a_marked_builder(
    pg: Any, direct: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 12: the marked builder's Uazapi and Evolution traffic is
    recorded on the GLOBAL read (observational, never silent) and never
    charged, while its Z-API traffic in the same run is."""

    async with _app(pg, monkeypatch) as running:
        world, client = running.world, running.client
        marked = UUID(world.builder)
        await relink_tenant(direct, UUID(world.uazapi_tenant), marked)
        await record_mark(direct, marked)
        async with asyncio.timeout(_TURN_SECONDS):
            uazapi = await client.post(
                "/v1/webhook/uazapi",
                json={
                    "instanceName": world.uazapi_instance,
                    "message": {
                        "messageid": "marked-uazapi-1",
                        "text": "Olá, qual o valor da consulta?",
                        "fromMe": False,
                        "type": "text",
                        "sender_pn": "5511900007201@s.whatsapp.net",
                    },
                    "chat": {"wa_chatid": "5511900007201@s.whatsapp.net"},
                },
            )
            evolution = await client.post(
                "/v1/webhook/evolution",
                json={
                    "event": "messages.upsert",
                    "instance": world.evolution_instance,
                    "data": {
                        "key": {
                            "remoteJid": "5511900007202@s.whatsapp.net",
                            "fromMe": False,
                            "id": "marked-evolution-1",
                        },
                        "message": {"conversation": "Olá, vocês aceitam convênio?"},
                        "pushName": "Paciente",
                    },
                },
            )
            zapi = await client.post(
                f"/v1/webhook/zapi/{world.zapi_token}",
                json=_zapi(world, "marked-zapi-2", "5511900007203"),
            )
    assert uazapi.status_code == 200 and uazapi.json()["replied"] is True
    assert evolution.status_code == 200, evolution.text
    assert zapi.json() == {"ok": True, "replied": True}, zapi.text
    for tenant in (world.uazapi_tenant, world.evolution_tenant):
        rows = await _tenant_rows(direct, UUID(tenant))
        assert rows["effects"] == 0 and rows["outbox"] == 0, tenant
        assert rows["observations"] == 1, tenant  # recorded, never silent
        async with direct.connect() as connection:
            modes = set(
                (
                    await connection.execute(
                        text(
                            "SELECT mode FROM credit_ingress_claims WHERE "
                            "tenant_id = :t"
                        ),
                        {"t": UUID(tenant)},
                    )
                ).scalars()
            )
        assert modes == {"observational"}, tenant
    assert (await _tenant_rows(direct, UUID(world.zapi_tenant)))["effects"] == 1
    assert [kind for kind, _, _ in await _ledger(direct, marked)] == ["debt"]


async def test_outgoing_media_is_charged_only_on_meta_and_zapi(
    pg: Any, direct: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The agent's file of a marked builder (``reply_media``): Meta and Z-API
    deliveries are charged as egress; Uazapi and Evolution keep the global
    read and record the egress observationally (never charged); an unmarked
    builder's Z-API delivery stays observational."""

    from src.api.middleware.media_limits import (
        AGENT_MEDIA_SENDS_PER_PROCESS,
        QueuedLimiter,
    )
    from src.models.media_asset import MediaAsset
    from src.services import egress_policy
    from src.services.media_assets import media_payload_sha256
    from src.services.reply_media import ReplyMediaChannel, deliver_reply_media

    force_credit_switch(monkeypatch)

    async def flag_on() -> bool:
        return True

    monkeypatch.setattr(egress_policy, "agent_media_send_enabled", flag_on)
    marked = await _builder_with_api_tenant(direct, "Marked media builder")
    other = await _builder_with_api_tenant(direct)
    await record_mark(direct, marked.builder)
    pool = QueuedLimiter(AGENT_MEDIA_SENDS_PER_PROCESS)
    engine = create_async_engine(pg.url, pool_size=2, max_overflow=0)

    async def asset(tenant_id: UUID) -> UUID:
        body = b"%PDF-1.7\n" + b"7" * 991
        media_id = uuid4()
        async with AsyncSession(direct) as session, session.begin():
            session.add(
                MediaAsset(
                    id=media_id,
                    tenant_id=tenant_id,
                    display_name="Preparo do exame",
                    original_filename="preparo.pdf",
                    category="pdf",
                    mime_type="application/pdf",
                    size_bytes=len(body),
                    sha256=media_payload_sha256(body),
                    payload=body,
                    auto_suggest=True,
                    trigger_intents=["detalhes_servicos"],
                )
            )
        return media_id

    async def send_media(payload: bytes, **_kwargs: Any) -> Mapping[str, object]:
        return {"messageId": "media-message-id"}

    async def send_text(_text: str) -> Mapping[str, object]:
        return {"messageId": "notice-message-id"}

    async def record(_sent: Mapping[str, object]) -> None:
        return None

    async def deliver(builder: _Builder, provider: Any) -> str:
        result = await deliver_reply_media(
            channel=ReplyMediaChannel(provider, send_media, send_text),
            media_id=await asset(builder.tenant),
            tenant_id=builder.tenant,
            session_id=builder.session,
            response_language="pt-BR",
            session_factory=async_sessionmaker(
                engine, expire_on_commit=False, autoflush=False
            ),
            agent_sends=pool,
            record_sent_id=record,
        )
        return str(result.outcome)

    other_before = await _builder_rows(direct, other.builder)
    try:
        outcomes = {
            provider: await deliver(marked, provider)
            for provider in ("zapi", "meta", "uazapi", "evolution")
        }
        outcomes["unmarked-zapi"] = await deliver(other, "zapi")
    finally:
        await engine.dispose()
    assert set(outcomes.values()) == {"sent"}, outcomes
    async with direct.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT d.builder_id, d.mode, count(*) FROM credit_egress_deliveries d "
                "WHERE d.builder_id IN (:m, :o) GROUP BY d.builder_id, d.mode"
            ),
            {"m": marked.builder, "o": other.builder},
        )
        by_mode = {(UUID(str(b)), str(mode)): int(n) for b, mode, n in rows}
    assert by_mode == {
        (marked.builder, "enforced"): 2,  # Z-API and Meta
        (marked.builder, "observational"): 2,  # Uazapi and Evolution
        (other.builder, "observational"): 1,
    }
    other_after = await _builder_rows(direct, other.builder)
    assert other_after == other_before
    # The marked deliveries read their mode through the inventory: seeing
    # the mark set neither global latch.
    assert credit_inventory._enforcement_seen is False
    assert credit_metering.enforcement_seen() is False


# ---------------------------------------------------------------------------
# D1 per tenant, both ways.
# ---------------------------------------------------------------------------


async def test_d1_per_tenant_both_ways_and_another_builder_unaffected(
    pg: Any, direct: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.services import credit_switch

    async with _app(pg, monkeypatch) as running:
        world, client = running.world, running.client
        marked, tenant = UUID(world.builder), UUID(world.api_tenant)
        other = await _builder_with_api_tenant(direct)
        other_before = await _builder_rows(direct, other.builder)
        await record_mark(direct, marked)
        broken = {"on": False}
        original = CreditRepository._marked_enforcement

        async def maybe_broken(
            self: CreditRepository, builder_id: UUID | None, readable: Any, *rest: Any
        ) -> EffectiveEnforcement:
            # ``rest``: the courtesy rule's gate and instant
            # (debito-cortesia-automatico), forwarded untouched.
            if broken["on"]:
                raise OperationalError("SELECT", {}, Exception("marks unreadable"))
            return await original(self, builder_id, readable, *rest)

        monkeypatch.setattr(CreditRepository, "_marked_enforcement", maybe_broken)
        keys = iter(f"d1-{n}" for n in range(100))

        async def post(builder: str, session: str, key: str) -> Any:
            async with asyncio.timeout(_TURN_SECONDS):
                return await client.post(
                    "/v1/events",
                    json=metering_suite._events_body(session),
                    headers=_api(key, next(keys)),
                )

        enforced = await post(world.builder, world.api_session, world.api_key)
        seen_after_enforced = credit_metering.enforcement_mark_seen(tenant)
        broken["on"] = True
        refused = await post(world.builder, world.api_session, world.api_key)
        unaffected = await post(str(other.builder), str(other.session), other.api_key)
        broken["on"] = False
        await record_mark(direct, marked, "unmark")
        cleared = await post(world.builder, world.api_session, world.api_key)
        seen_after_clear = credit_metering.enforcement_mark_seen(tenant)
        broken["on"] = True
        free_again = await post(world.builder, world.api_session, world.api_key)
        broken["on"] = False
        # The gate closed (a reading below 047) for a tenant seen marked.
        await record_mark(direct, marked)
        enforced_again = await post(world.builder, world.api_session, world.api_key)
        head = credit_switch.credit_switch_reading()
        credit_switch._SWITCH.publish(
            credit_switch.CreditSwitchReading(True, "schema_ready", "046")
        )
        try:
            gate_closed = await post(world.builder, world.api_session, world.api_key)
            other_gate_closed = await post(
                str(other.builder), str(other.session), other.api_key
            )
        finally:
            credit_switch._SWITCH.publish(head)
        latches = (
            credit_metering.enforcement_seen(),
            credit_inventory._enforcement_seen,
        )

    assert enforced.status_code == 200, enforced.text
    assert seen_after_enforced is True
    assert refused.status_code == 503 and refused.json() == {
        "code": "credit_enforcement_unavailable"
    }
    assert unaffected.status_code == 200, unaffected.text
    assert cleared.status_code == 200, cleared.text
    assert seen_after_clear is False
    assert free_again.status_code == 200, free_again.text  # no longer latched
    assert enforced_again.status_code == 200, enforced_again.text
    assert gate_closed.status_code == 503 and gate_closed.json() == {
        "code": "credit_enforcement_unavailable"
    }
    assert other_gate_closed.status_code == 200, other_gate_closed.text
    assert latches == (False, False)
    # Two enforced decisions charged the marked builder (served as debt: no
    # lot); the others were free; the unmarked builder never moved.
    assert [kind for kind, _, _ in await _ledger(direct, marked)] == ["debt", "debt"]
    # The lifecycle evaluated the marked builder (no lot: its first debt
    # opened the exhaustion episode), as it would under the global.
    assert (await _builder_rows(direct, marked))["credit_lifecycle_episodes"] == 1
    assert await _builder_rows(direct, other.builder) == other_before
    assert (await _tenant_rows(direct, other.tenant))["observations"] == 2


# ---------------------------------------------------------------------------
# The race: relinked between the capture and the lock.
# ---------------------------------------------------------------------------


def _relink_after(
    monkeypatch: pytest.MonkeyPatch,
    module: Any,
    name: str,
    direct: AsyncEngine,
    tenant_id: UUID,
    builder_id: UUID | None,
) -> dict[str, int]:
    """Wrap ``module.name`` (the capture of the mode) so that, right after it
    returned, the tenant is relinked -- the admin's link or unlink landing
    between the capture and the enforced transaction's lock.  Once."""

    original = getattr(module, name)
    calls = {"relinked": 0}

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        captured = await original(*args, **kwargs)
        if not calls["relinked"]:
            calls["relinked"] += 1
            await relink_tenant(direct, tenant_id, builder_id)
        return captured

    monkeypatch.setattr(module, name, wrapped)
    return calls


@pytest.mark.parametrize("relinked_to", ["unmarked", "standalone"])
async def test_the_race_on_events_is_503_before_any_write_and_retryable(
    pg: Any,
    direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    relinked_to: str,
) -> None:
    async with _app(pg, monkeypatch) as running:
        world, client = running.world, running.client
        marked, tenant = UUID(world.builder), UUID(world.api_tenant)
        other = await _builder_with_api_tenant(direct)
        target = other.builder if relinked_to == "unmarked" else None
        await record_mark(direct, marked)
        marked_before = await _builder_rows(direct, marked)
        other_before = await _builder_rows(direct, other.builder)
        tenant_before = await _tenant_rows(direct, tenant)
        calls = _relink_after(
            monkeypatch,
            credit_metering,
            "capture_events_enforcement",
            direct,
            tenant,
            target,
        )
        async with asyncio.timeout(_TURN_SECONDS):
            raced = await client.post(
                "/v1/events",
                json=metering_suite._events_body(world.api_session),
                headers=_api(world.api_key, "raced-1"),
            )
        after_race = (
            await _builder_rows(direct, marked),
            await _builder_rows(direct, other.builder),
            await _tenant_rows(direct, tenant),
        )
        async with asyncio.timeout(_TURN_SECONDS):
            retried = await client.post(
                "/v1/events",
                json=metering_suite._events_body(world.api_session),
                headers=_api(world.api_key, "raced-1"),
            )
    assert calls == {"relinked": 1}
    assert raced.status_code == 503 and raced.json() == {
        "code": "credit_enforcement_unavailable"
    }
    # Before any write: no claim, effect, account, event -- of anyone.
    assert after_race == (marked_before, other_before, tenant_before)
    # Repeatable: the retry decides again, on the builder linked now (not
    # marked, or none): main's path, never 402 credit_pending, never charged.
    assert retried.status_code == 200, retried.text
    assert await _builder_rows(direct, marked) == marked_before
    assert await _builder_rows(direct, other.builder) == other_before
    rows = await _tenant_rows(direct, tenant)
    assert rows["claims"] == 1 and rows["effects"] == 0 and rows["observations"] == 1


async def test_the_race_on_a_zapi_webhook_is_503_before_any_write(
    pg: Any, direct: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a webhook the race's 503 loses the message (the Redis replay claim
    is spent): accepted (Decision 6); nothing is written."""

    async with _app(pg, monkeypatch) as running:
        world, client = running.world, running.client
        marked, tenant = UUID(world.builder), UUID(world.zapi_tenant)
        other = await _builder_with_api_tenant(direct)
        await record_mark(direct, marked)
        before = (
            await _builder_rows(direct, marked),
            await _builder_rows(direct, other.builder),
            await _tenant_rows(direct, tenant),
        )
        calls = _relink_after(
            monkeypatch,
            credit_metering,
            "read_tenant_enforcement",
            direct,
            tenant,
            other.builder,
        )
        async with asyncio.timeout(_TURN_SECONDS):
            raced = await client.post(
                f"/v1/webhook/zapi/{world.zapi_token}",
                json=_zapi(world, "raced-zapi-1", "5511900007301"),
            )
        after = (
            await _builder_rows(direct, marked),
            await _builder_rows(direct, other.builder),
            await _tenant_rows(direct, tenant),
        )
    assert calls == {"relinked": 1}
    assert raced.status_code == 503 and raced.json() == {
        "error": "credit_enforcement_unavailable"
    }
    assert after == before


@pytest.mark.parametrize("relinked_to", ["unmarked", "standalone"])
async def test_the_race_on_a_meta_webhook_is_503_before_any_write(
    pg: Any,
    direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    relinked_to: str,
) -> None:
    """Meta shares Z-API's pre-check (``precheck_meta`` ->
    ``precheck_webhook``): the mark captured by ``read_tenant_enforcement``
    is re-checked under the tenant and account locks, and a tenant relinked
    in between is 503 with Z-API's body, before the session, the claim or
    any credit row.  As on Z-API the message is lost (Decision 6): Meta's
    replay claim (``_claim_incoming_message``) is taken before the
    pre-check, so the provider's redelivery answers ``duplicate_message``
    and is never decided again; nothing is written by either."""

    async with _app(pg, monkeypatch) as running:
        world, client = running.world, running.client
        marked, tenant = UUID(world.builder), UUID(world.meta_tenant)
        other = await _builder_with_api_tenant(direct)
        target = other.builder if relinked_to == "unmarked" else None
        await record_mark(direct, marked)
        before = (
            await _builder_rows(direct, marked),
            await _builder_rows(direct, other.builder),
            await _tenant_rows(direct, tenant),
        )
        calls = _relink_after(
            monkeypatch,
            credit_metering,
            "read_tenant_enforcement",
            direct,
            tenant,
            target,
        )
        raw, headers = metering_suite._signed_meta(
            world, _meta_message("wamid.raced-meta-1", "5511900007311")
        )
        async with asyncio.timeout(_TURN_SECONDS):
            raced = await client.post("/v1/webhook/meta", content=raw, headers=headers)
        after_race = (
            await _builder_rows(direct, marked),
            await _builder_rows(direct, other.builder),
            await _tenant_rows(direct, tenant),
        )
        async with asyncio.timeout(_TURN_SECONDS):
            redelivered = await client.post(
                "/v1/webhook/meta", content=raw, headers=headers
            )
        after_redelivery = (
            await _builder_rows(direct, marked),
            await _builder_rows(direct, other.builder),
            await _tenant_rows(direct, tenant),
        )
    assert calls == {"relinked": 1}
    assert raced.status_code == 503 and raced.json() == {
        "error": "credit_enforcement_unavailable"
    }, raced.text
    assert after_race == before
    # Lost, not retryable: the spent replay claim answers the redelivery.
    assert redelivered.status_code == 200 and redelivered.json() == {
        "ok": True,
        "skipped": "duplicate_message",
    }, redelivered.text
    assert after_redelivery == before


@pytest.mark.parametrize("relinked_to", ["unmarked", "standalone"])
async def test_the_race_on_a_knowledge_upload_is_503_and_stores_nothing(
    pg: Any,
    direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    relinked_to: str,
) -> None:
    async with _app(pg, monkeypatch) as running:
        world, client = running.world, running.client
        marked, tenant = UUID(world.builder), UUID(world.api_tenant)
        other = await _builder_with_api_tenant(direct)
        target = other.builder if relinked_to == "unmarked" else None
        await record_mark(direct, marked)
        before = (
            await _builder_rows(direct, marked),
            await _builder_rows(direct, other.builder),
            await _tenant_rows(direct, tenant),
        )
        calls = _relink_after(
            monkeypatch, credit_inventory, "read_credit_mode", direct, tenant, target
        )
        async with asyncio.timeout(_TURN_SECONDS):
            raced = await client.post(
                "/v1/knowledge/documents",
                json={"content": "Atendemos de segunda a sexta.", "metadata": {}},
                headers=_api(world.api_key, None),
            )
        after = (
            await _builder_rows(direct, marked),
            await _builder_rows(direct, other.builder),
            await _tenant_rows(direct, tenant),
        )
    assert calls == {"relinked": 1}
    assert raced.status_code == 503, raced.text
    assert raced.json()["detail"] == {"code": "credit_enforcement_unavailable"}
    assert after == before  # no document, allocation, effect or account


async def _handoffs(engine: AsyncEngine, tenant_id: UUID) -> int:
    async with engine.connect() as connection:
        return int(
            await connection.scalar(
                text("SELECT count(*) FROM handoff_records WHERE tenant_id = :t"),
                {"t": tenant_id},
            )
        )


async def test_the_race_on_a_meta_file_handoff_is_503_before_any_write(
    pg: Any, direct: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file handoff is an enforced transaction of its own after the
    pre-check (``run_meta_file_handoff``): a tenant relinked to an unmarked
    builder in between is re-checked under the account lock -- 503 like the
    pre-check's (the message is lost), no claim, handoff, debit or account
    for anyone."""

    async with _app(pg, monkeypatch) as running:
        world, client = running.world, running.client
        marked, tenant = UUID(world.builder), UUID(world.meta_tenant)
        other = await _builder_with_api_tenant(direct)
        await record_mark(direct, marked)
        phone = "5511900007401"
        raw, headers = metering_suite._signed_meta(
            world,
            {
                "from": phone,
                "id": "wamid.race-file-document",
                "type": "document",
                "document": {
                    "id": "race-media-document",
                    "mime_type": "application/pdf",
                },
            },
        )
        async with asyncio.timeout(_TURN_SECONDS):
            acked = await client.post("/v1/webhook/meta", content=raw, headers=headers)
        before = (
            await _builder_rows(direct, marked),
            await _builder_rows(direct, other.builder),
            await _tenant_rows(direct, tenant),
            await _handoffs(direct, tenant),
        )
        original = credit_metering.run_meta_file_handoff
        calls = {"relinked": 0}

        async def relinked(*args: Any, **kwargs: Any) -> Any:
            calls["relinked"] += 1
            await relink_tenant(direct, tenant, other.builder)
            return await original(*args, **kwargs)

        monkeypatch.setattr(credit_metering, "run_meta_file_handoff", relinked)
        raw, headers = metering_suite._signed_meta(
            world,
            {
                "from": phone,
                "id": "wamid.race-file-forward",
                "type": "text",
                "text": {"body": "Pode encaminhar para a equipe, por favor"},
            },
        )
        async with asyncio.timeout(_TURN_SECONDS):
            raced = await client.post("/v1/webhook/meta", content=raw, headers=headers)
        after = (
            await _builder_rows(direct, marked),
            await _builder_rows(direct, other.builder),
            await _tenant_rows(direct, tenant),
            await _handoffs(direct, tenant),
        )
    assert acked.status_code == 200, acked.text
    assert calls == {"relinked": 1}  # the forward reached the file handoff
    assert raced.status_code == 503 and raced.json() == {
        "error": "credit_enforcement_unavailable"
    }, raced.text
    assert after == before


@pytest.mark.parametrize("channel", ["events", "zapi", "meta"])
async def test_relinked_before_the_claim_is_503_and_writes_no_claim(
    pg: Any, direct: AsyncEngine, monkeypatch: pytest.MonkeyPatch, channel: str
) -> None:
    """Relinked between the pre-check's commit and the claim insert: the
    claim's transaction refuses (503, retryable on ``POST /v1/events``; a
    webhook's message is lost) -- no claim row records the other builder,
    nothing of it moves.  On Meta the refusal comes from the shared enforced
    branch (``acquire_webhook_claim`` inside ``_run_pipeline_transactionally``)
    and is answered by the handler's own ``except`` around that call, with
    Z-API's body."""

    async with _app(pg, monkeypatch) as running:
        world, client = running.world, running.client
        marked = UUID(world.builder)
        tenant = UUID(
            {
                "events": world.api_tenant,
                "zapi": world.zapi_tenant,
                "meta": world.meta_tenant,
            }[channel]
        )
        other = await _builder_with_api_tenant(direct)
        await record_mark(direct, marked)
        name = f"precheck_{channel}"
        original = getattr(credit_metering, name)
        calls = {"relinked": 0}

        async def relinked(*args: Any, **kwargs: Any) -> Any:
            verdict = await original(*args, **kwargs)
            if not calls["relinked"]:
                calls["relinked"] += 1
                await relink_tenant(direct, tenant, other.builder)
            return verdict

        monkeypatch.setattr(credit_metering, name, relinked)
        claims_before = (await _tenant_rows(direct, tenant))["claims"]
        other_before = await _builder_rows(direct, other.builder)
        async with asyncio.timeout(_TURN_SECONDS):
            if channel == "events":
                raced = await client.post(
                    "/v1/events",
                    json=metering_suite._events_body(world.api_session),
                    headers=_api(world.api_key, "raced-claim-1"),
                )
            elif channel == "zapi":
                raced = await client.post(
                    f"/v1/webhook/zapi/{world.zapi_token}",
                    json=_zapi(world, "raced-claim-zapi-1", "5511900007501"),
                )
            else:
                raw, headers = metering_suite._signed_meta(
                    world, _meta_message("wamid.raced-claim-meta-1", "5511900007502")
                )
                raced = await client.post(
                    "/v1/webhook/meta", content=raw, headers=headers
                )
        rows = await _tenant_rows(direct, tenant)
        other_after = await _builder_rows(direct, other.builder)
        if channel == "events":
            async with asyncio.timeout(_TURN_SECONDS):
                retried = await client.post(
                    "/v1/events",
                    json=metering_suite._events_body(world.api_session),
                    headers=_api(world.api_key, "raced-claim-1"),
                )
    assert calls == {"relinked": 1}
    expected = (
        {"code": "credit_enforcement_unavailable"}
        if channel == "events"
        else {"error": "credit_enforcement_unavailable"}
    )
    assert raced.status_code == 503 and raced.json() == expected, raced.text
    assert rows["claims"] == claims_before  # no claim for the other builder
    assert rows["effects"] == 0 and rows["outbox"] == 0
    assert other_after == other_before
    if channel == "events":
        # Repeatable: decided again on the builder linked now (not marked).
        assert retried.status_code == 200, retried.text
        assert await _builder_rows(direct, other.builder) == other_before


class _Log:
    """Records every log call of the module it replaces (any level)."""

    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, level: str) -> Any:
        def record(_message: str, *_args: Any, **fields: Any) -> None:
            self.entries.append((level, fields))

        return record


async def test_the_gate_closing_after_the_capture_is_unreadable_not_changed(
    pg: Any, direct: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The 047 gate closes between the capture (a mark) and the pre-check's
    lock: the re-check cannot read the marks -- 503 with the D1 alert
    ``enforcement_mark_unreadable_after_marked`` (an error), never logged as
    a changed enforcement."""

    from src.services import credit_switch

    async with _app(pg, monkeypatch) as running:
        world, client = running.world, running.client
        await record_mark(direct, UUID(world.builder))
        head = credit_switch.credit_switch_reading()
        original = credit_metering.capture_events_enforcement

        async def gate_closes(*args: Any, **kwargs: Any) -> Any:
            captured = await original(*args, **kwargs)
            credit_switch._SWITCH.publish(
                credit_switch.CreditSwitchReading(True, "schema_ready", "046")
            )
            return captured

        monkeypatch.setattr(credit_metering, "capture_events_enforcement", gate_closes)
        log = _Log()
        monkeypatch.setattr(credit_metering, "logger", log)
        try:
            async with asyncio.timeout(_TURN_SECONDS):
                refused = await client.post(
                    "/v1/events",
                    json=metering_suite._events_body(world.api_session),
                    headers=_api(world.api_key, "gate-closes-1"),
                )
        finally:
            credit_switch._SWITCH.publish(head)
    assert refused.status_code == 503 and refused.json() == {
        "code": "credit_enforcement_unavailable"
    }
    alerts = [
        (level, fields.get("reason"))
        for level, fields in log.entries
        if fields.get("alert") == "credit_enforcement_unavailable"
    ]
    assert alerts == [("error", "enforcement_mark_unreadable_after_marked")]


async def test_the_recheck_runs_under_the_account_lock_and_sees_an_unmark(
    pg: Any, direct: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision 5: the pre-check re-reads the mark AFTER the builder's
    account lock (lock order step 3), so it serializes with the operator's
    ``unmark``, which takes the same lock: an unmark committed while the
    request waits for that lock is seen -- 503, nothing written -- instead
    of a decision enforced on a mark that is gone."""

    async with _app(pg, monkeypatch) as running:
        world, client = running.world, running.client
        marked, tenant = UUID(world.builder), UUID(world.api_tenant)
        await record_mark(direct, marked)
        await execute_on(
            direct,
            "INSERT INTO credit_accounts (id, builder_id) VALUES (:a, :b)",
            a=uuid4(),
            b=marked,
        )
        before = (
            await _builder_rows(direct, marked),
            await _tenant_rows(direct, tenant),
        )
        async with direct.connect() as holder:
            transaction = await holder.begin()
            await holder.execute(
                text("SELECT id FROM credit_accounts WHERE builder_id = :b FOR UPDATE"),
                {"b": marked},
            )
            request = asyncio.create_task(
                client.post(
                    "/v1/events",
                    json=metering_suite._events_body(world.api_session),
                    headers=_api(world.api_key, "under-the-lock-1"),
                )
            )
            await metering_suite._until_lock_waiters(
                direct, 1, query="%credit_accounts%FOR UPDATE%"
            )
            await record_mark(direct, marked, "unmark")
            await transaction.commit()
            answer = await asyncio.wait_for(request, _TURN_SECONDS)
        after = (
            await _builder_rows(direct, marked),
            await _tenant_rows(direct, tenant),
        )
    assert answer.status_code == 503 and answer.json() == {
        "code": "credit_enforcement_unavailable"
    }
    assert after == before


async def test_relinked_during_the_llm_the_locked_builder_is_never_charged(
    pg: Any, direct: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tenant relinked to an unmarked builder after the pre-check (during
    the LLM call) is answered, and the builder locked at the final
    transaction is not charged for a decision captured on another builder's
    mark (like LOW-5's standalone tenant); the marked builder neither."""

    from src.services.event_service import EventService

    async with _app(pg, monkeypatch) as running:
        world, client = running.world, running.client
        marked, tenant = UUID(world.builder), UUID(world.api_tenant)
        other = await _builder_with_api_tenant(direct)
        await record_mark(direct, marked)
        marked_before = await _builder_rows(direct, marked)
        other_before = await _builder_rows(direct, other.builder)
        generate = EventService.generate_response

        async def relinked(self: Any, *args: Any, **kwargs: Any) -> Any:
            await relink_tenant(direct, tenant, other.builder)
            return await generate(self, *args, **kwargs)

        monkeypatch.setattr(EventService, "generate_response", relinked)
        async with asyncio.timeout(_TURN_SECONDS):
            answer = await client.post(
                "/v1/events",
                json=metering_suite._events_body(world.api_session),
                headers=_api(world.api_key, "relinked-llm-1"),
            )
    assert answer.status_code == 200, answer.text
    marked_after = await _builder_rows(direct, marked)
    # The marked builder got its claim and its account (pre-check), no charge.
    assert marked_after["credit_ledger_entries"] == 0
    assert marked_after["credit_effects"] == 0
    assert await _builder_rows(direct, other.builder) == other_before
    rows = await _tenant_rows(direct, tenant)
    assert rows["effects"] == 0 and rows["outbox"] == 1  # answered, stored
    assert marked_before["credit_ledger_entries"] == 0


# ---------------------------------------------------------------------------
# debito-cortesia-automatico Group 2: the courtesy rule on the shared enforced
# path (POST /v1/events, Z-API, Meta) -- charged from courtesy only, blocked
# at once, the race, the rule's D1, the real origin, the workers and the
# shared block gate.
# ---------------------------------------------------------------------------


def _harness(engine: AsyncEngine) -> Any:
    """What the ``credit_rollout_world`` seeds take (``engine``, ``sessions``)."""

    return SimpleNamespace(
        engine=engine,
        sessions=async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        ),
    )


def _charged(at: datetime) -> EffectiveEnforcement:
    return EffectiveEnforcement(at, "courtesy", state="charged")


def _blocked(at: datetime) -> EffectiveEnforcement:
    return EffectiveEnforcement(at, "courtesy", state="blocked")


async def _trace_debits(engine: AsyncEngine, builder_id: UUID) -> list[tuple[int, Any]]:
    """The builder's ``debit`` entries of its ``trace`` effects."""

    return [
        (amount, lot)
        for kind, amount, lot in await _debits(engine, builder_id)
        if kind == "trace"
    ]


async def _trace_effects(engine: AsyncEngine, builder_id: UUID) -> list[str | None]:
    """The ``decision_digest`` of every ``trace`` effect of the builder."""

    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT decision_digest FROM credit_effects WHERE builder_id = :b "
                "AND effect_kind = 'trace' ORDER BY created_at, id"
            ),
            {"b": builder_id},
        )
        return [digest for (digest,) in rows]


async def _patient_sessions(engine: AsyncEngine, tenant_id: UUID, phone: str) -> int:
    async with engine.connect() as connection:
        return int(
            await connection.scalar(
                text(
                    "SELECT count(*) FROM sessions WHERE tenant_id = :t "
                    "AND user_id = :p"
                ),
                {"t": tenant_id, "p": phone},
            )
        )


def _digests(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every ``decision_digest`` the process computes: ``(origin, digest)``."""

    seen: list[tuple[str, str]] = []
    original = credit_metering.decision_digest

    def spy(**kwargs: Any) -> str:
        digest = original(**kwargs)
        seen.append((kwargs["origin"].value, digest))
        return digest

    monkeypatch.setattr(credit_metering, "decision_digest", spy)
    return seen


def _tenants(world: Any) -> tuple[UUID, UUID, UUID]:
    return UUID(world.api_tenant), UUID(world.zapi_tenant), UUID(world.meta_tenant)


async def test_a_charged_courtesy_builder_pays_courtesy_only_even_in_quarantine(
    pg: Any, direct: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No global, no mark, a courtesy lot: one trace per ``POST /v1/events``,
    Z-API and Meta decision, debited from the courtesy lot alone; an ASAAS
    quarantine and an unusable binding coverage refuse none of them (DESIGN
    item 7: no ``credit_pending``), no paid lot moves, no debt; every
    digest is ``production``.  The control: the same builder, once marked,
    is ``credit_pending`` by that very quarantine."""

    from src.services.credit_rollout import CreditRolloutService

    async with _app(pg, monkeypatch) as running:
        world, client = running.world, running.client
        builder = UUID(world.builder)
        harness = _harness(direct)
        granted = await grant_courtesy(direct, builder, 10)  # its account too
        subscription = await make_subscription(harness, builder)
        today = civil_today()
        await make_cycle(
            harness,
            builder,
            subscription,
            start=today - timedelta(days=3),
            end=today + timedelta(days=27),
            state="quarantined",
        )
        await make_topup(harness, builder, state="quarantined")
        async with AsyncSession(direct) as session:
            quarantined = await CreditRepository(session).quarantine_hold(builder)
            coverage = await CreditRolloutService(session).binding_coverage(builder)
            await session.rollback()
        assert quarantined and not coverage.usable  # the gates would refuse
        assert await effective_of(direct, builder) == _charged(granted.occurred_at)
        digests = _digests(monkeypatch)
        async with asyncio.timeout(_TURN_SECONDS):
            events = await client.post(
                "/v1/events",
                json=metering_suite._events_body(world.api_session),
                headers=_api(world.api_key, "rule-charged-1"),
            )
            zapi = await client.post(
                f"/v1/webhook/zapi/{world.zapi_token}",
                json=_zapi(world, "rule-charged-zapi-1", "5511900008101"),
            )
            raw, headers = metering_suite._signed_meta(
                world, _meta_message("wamid.rule-charged-meta-1", "5511900008102")
            )
            meta = await client.post("/v1/webhook/meta", content=raw, headers=headers)
        seen = [credit_metering.enforcement_rule_seen(t) for t in _tenants(world)]
        marks_seen = [credit_metering.enforcement_mark_seen(t) for t in _tenants(world)]
        global_seen = credit_metering.enforcement_seen()
        await record_mark(direct, builder)
        async with asyncio.timeout(_TURN_SECONDS):
            marked = await client.post(
                "/v1/events",
                json=metering_suite._events_body(world.api_session),
                headers=_api(world.api_key, "rule-charged-marked-1"),
            )
    assert events.status_code == 200, events.text
    assert zapi.json() == {"ok": True, "replied": True}, zapi.text
    assert meta.status_code == 200 and "skipped" not in meta.json(), meta.text
    assert await _trace_debits(direct, builder) == [(1_000, granted.lot_id)] * 3
    assert await paid_lot_moves(direct, builder) == []
    assert await debt_rows(direct, builder) == (0, 0)
    assert (await _builder_rows(direct, builder))["enforced_claims"] == 3
    stored = await _trace_effects(direct, builder)
    assert len(stored) == 3
    assert set(stored) <= {
        digest for origin, digest in digests if origin == "production"
    }
    assert {origin for origin, _ in digests} == {"production"}
    assert seen == [True, True, True]
    assert marks_seen == [False, False, False] and global_seen is False
    assert marked.status_code == 402 and marked.json() == {"code": "credit_pending"}


async def test_a_blocked_courtesy_builder_is_suspended_with_nothing_written(
    pg: Any, direct: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its courtesy removed ("Tirar tudo") and no paid plan: ``POST
    /v1/events`` 402 ``credit_suspended`` (a call without ``Idempotency-Key``
    still 400, DESIGN Risk 4), Z-API and Meta 200 ``skipped:
    credit_suspended`` -- no session, no reply, no LGPD notice, no claim,
    effect, event or credit row.  A new grant unblocks it with no
    operator."""

    from src.api.routes import webhook

    async with _app(pg, monkeypatch) as running:
        world, client = running.world, running.client
        builder = UUID(world.builder)
        granted = await grant_courtesy(direct, builder, 5)
        await remove_courtesy(direct, granted.lot_id)
        assert await effective_of(direct, builder) == _blocked(granted.occurred_at)
        zapi_phone, meta_phone = "5511900008201", "5511900008202"
        before = [await _builder_rows(direct, builder)] + [
            await _tenant_rows(direct, tenant) for tenant in _tenants(world)
        ]
        zapi_sends = webhook.send_text_message.await_count
        meta_sends = webhook.send_meta_cloud_text.await_count
        async with asyncio.timeout(_TURN_SECONDS):
            events = await client.post(
                "/v1/events",
                json=metering_suite._events_body(world.api_session),
                headers=_api(world.api_key, "rule-blocked-1"),
            )
            keyless = await client.post(
                "/v1/events",
                json=metering_suite._events_body(world.api_session),
                headers=_api(world.api_key, None),
            )
            zapi = await client.post(
                f"/v1/webhook/zapi/{world.zapi_token}",
                json=_zapi(world, "rule-blocked-zapi-1", zapi_phone),
            )
            raw, headers = metering_suite._signed_meta(
                world, _meta_message("wamid.rule-blocked-meta-1", meta_phone)
            )
            meta = await client.post("/v1/webhook/meta", content=raw, headers=headers)
        after = [await _builder_rows(direct, builder)] + [
            await _tenant_rows(direct, tenant) for tenant in _tenants(world)
        ]
        sends = (
            webhook.send_text_message.await_count - zapi_sends,
            webhook.send_meta_cloud_text.await_count - meta_sends,
        )
        patients = (
            await _patient_sessions(direct, UUID(world.zapi_tenant), zapi_phone),
            await _patient_sessions(direct, UUID(world.meta_tenant), meta_phone),
        )
        regranted = await grant_courtesy(direct, builder, 1)
        async with asyncio.timeout(_TURN_SECONDS):
            unblocked = await client.post(
                "/v1/events",
                json=metering_suite._events_body(world.api_session),
                headers=_api(world.api_key, "rule-blocked-2"),
            )
    suspended = {"ok": True, "skipped": "credit_suspended"}
    assert events.status_code == 402 and events.json() == {"code": "credit_suspended"}
    assert keyless.status_code == 400
    assert keyless.json() == {"code": "idempotency_key_required"}
    assert zapi.status_code == 200 and zapi.json() == suspended, zapi.text
    assert meta.status_code == 200 and meta.json() == suspended, meta.text
    assert after == before
    assert sends == (0, 0)  # no reply and no LGPD notice
    assert patients == (0, 0)  # no session
    assert unblocked.status_code == 200, unblocked.text
    assert await _trace_debits(direct, builder) == [(1_000, regranted.lot_id)]


def _after(monkeypatch: pytest.MonkeyPatch, name: str, change: Any) -> dict[str, int]:
    """Wrap ``credit_metering.<name>`` so that ``change`` runs right after it
    returned -- once: the operator's grant or removal landing between the
    capture of the mode and the enforced transaction's lock."""

    original = getattr(credit_metering, name)
    calls = {"changed": 0}

    async def wrapped(*args: Any, **kwargs: Any) -> Any:
        captured = await original(*args, **kwargs)
        if not calls["changed"]:
            calls["changed"] += 1
            await change()
        return captured

    monkeypatch.setattr(credit_metering, name, wrapped)
    return calls


@pytest.mark.parametrize(
    ("channel", "change"),
    [
        ("events", "removed"),
        ("events", "granted"),
        ("zapi", "removed"),
        ("meta", "granted"),
    ],
)
async def test_a_rule_change_between_the_capture_and_the_lock_is_503(
    pg: Any,
    direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
    change: str,
) -> None:
    """DESIGN item 11: captured ``charged`` then all courtesy removed
    (``blocked`` under the lock), or captured ``blocked`` then granted
    (``charged``): the pre-check's re-check by ``(origin, at, state)`` is 503
    ``credit_enforcement_unavailable`` before any write; on ``POST
    /v1/events`` a retry with the same key decides again on the new state;
    on a webhook the message is lost (as for a mark)."""

    async with _app(pg, monkeypatch) as running:
        world, client = running.world, running.client
        builder = UUID(world.builder)
        tenant = UUID(
            {
                "events": world.api_tenant,
                "zapi": world.zapi_tenant,
                "meta": world.meta_tenant,
            }[channel]
        )
        first = await grant_courtesy(direct, builder, 5)
        if change == "granted":
            await remove_courtesy(direct, first.lot_id)
        lots: dict[str, Any] = {}

        async def operator() -> None:
            if change == "removed":
                await remove_courtesy(direct, first.lot_id)
            else:
                lots["new"] = (await grant_courtesy(direct, builder, 2)).lot_id

        calls = _after(
            monkeypatch,
            (
                "capture_events_enforcement"
                if channel == "events"
                else "read_tenant_enforcement"
            ),
            operator,
        )
        before = await _tenant_rows(direct, tenant)
        async with asyncio.timeout(_TURN_SECONDS):
            if channel == "events":
                raced = await client.post(
                    "/v1/events",
                    json=metering_suite._events_body(world.api_session),
                    headers=_api(world.api_key, "rule-raced-1"),
                )
            elif channel == "zapi":
                raced = await client.post(
                    f"/v1/webhook/zapi/{world.zapi_token}",
                    json=_zapi(world, "rule-raced-zapi-1", "5511900008301"),
                )
            else:
                raw, headers = metering_suite._signed_meta(
                    world, _meta_message("wamid.rule-raced-meta-1", "5511900008302")
                )
                raced = await client.post(
                    "/v1/webhook/meta", content=raw, headers=headers
                )
        after = await _tenant_rows(direct, tenant)
        claims = (await _builder_rows(direct, builder))["enforced_claims"]
        traces = await _trace_effects(direct, builder)
        if channel == "events":
            async with asyncio.timeout(_TURN_SECONDS):
                retried = await client.post(
                    "/v1/events",
                    json=metering_suite._events_body(world.api_session),
                    headers=_api(world.api_key, "rule-raced-1"),
                )
    assert calls == {"changed": 1}
    expected = (
        {"code": "credit_enforcement_unavailable"}
        if channel == "events"
        else {"error": "credit_enforcement_unavailable"}
    )
    assert raced.status_code == 503 and raced.json() == expected, raced.text
    assert after == before  # no claim, effect, outbox, event of the request
    assert claims == 0 and traces == []
    if channel == "events":
        if change == "removed":
            assert retried.status_code == 402
            assert retried.json() == {"code": "credit_suspended"}
        else:
            assert retried.status_code == 200, retried.text
            assert await _trace_debits(direct, builder) == [(1_000, lots["new"])]


@pytest.mark.parametrize("channel", ["events", "zapi"])
@pytest.mark.parametrize("during", ["removed", "marked"])
async def test_the_final_transaction_never_rereads_the_rule(
    pg: Any,
    direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
    during: str,
) -> None:
    """DESIGN Decision 14: captured ``charged``; during the LLM call the
    operator removes all courtesy (the builder is then protected by its
    paid top-up) or marks the builder.  The final transaction never re-reads
    the mode and never answers 503: it debits what courtesy there is (the
    rest forgiven) -- no paid lot moves (a re-read would spend the top-up,
    which expires first), no debt -- and the decision is answered."""

    from src.services.event_service import EventService

    async with _app(pg, monkeypatch) as running:
        world, client = running.world, running.client
        builder = UUID(world.builder)
        granted = await grant_courtesy(direct, builder, 3)  # its account too
        await make_topup(
            _harness(direct),
            builder,
            expires_at=datetime.now(timezone.utc) + timedelta(days=3),
        )
        generate = EventService.generate_response
        changed = {"count": 0}

        async def mid_turn(self: Any, *args: Any, **kwargs: Any) -> Any:
            if not changed["count"]:
                changed["count"] += 1
                if during == "removed":
                    await remove_courtesy(direct, granted.lot_id)
                else:
                    await record_mark(direct, builder)
            return await generate(self, *args, **kwargs)

        monkeypatch.setattr(EventService, "generate_response", mid_turn)
        async with asyncio.timeout(_TURN_SECONDS):
            if channel == "events":
                answer = await client.post(
                    "/v1/events",
                    json=metering_suite._events_body(world.api_session),
                    headers=_api(world.api_key, "rule-final-1"),
                )
            else:
                answer = await client.post(
                    f"/v1/webhook/zapi/{world.zapi_token}",
                    json=_zapi(world, "rule-final-zapi-1", "5511900008401"),
                )
        now = await effective_of(direct, builder)
    assert changed == {"count": 1}
    assert answer.status_code == 200, answer.text
    if channel == "zapi":
        assert answer.json() == {"ok": True, "replied": True}
    assert now.origin == (None if during == "removed" else "mark")
    paid = [] if during == "removed" else [(1_000, granted.lot_id)]
    assert await _trace_debits(direct, builder) == paid
    assert len(await _trace_effects(direct, builder)) == 1  # forgiven or paid
    assert await paid_lot_moves(direct, builder) == []
    assert await debt_rows(direct, builder) == (0, 0)


@pytest.mark.parametrize(
    ("channel", "when"),
    [("events", "before_claim"), ("events", "during_llm"), ("zapi", "during_llm")],
)
async def test_a_tenant_relinked_after_a_rule_capture_is_charged_to_no_courtesy(
    pg: Any,
    direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
    when: str,
) -> None:
    """``reparented_since_capture`` holds for ``courtesy`` (DESIGN item 11):
    relinked to another builder with courtesy of its own after the
    pre-check, the claim is refused (503, no claim for the other builder),
    or -- relinked during the LLM call -- the decision is answered and
    charged to neither builder's courtesy."""

    from src.services.event_service import EventService

    async with _app(pg, monkeypatch) as running:
        world, client = running.world, running.client
        builder = UUID(world.builder)
        tenant = UUID(world.api_tenant if channel == "events" else world.zapi_tenant)
        other = await _builder_with_api_tenant(direct, "Courtesy builder")
        await grant_courtesy(direct, builder, 5)
        await grant_courtesy(direct, other.builder, 5)
        other_before = await _builder_rows(direct, other.builder)
        relinked = {"count": 0}

        async def relink() -> None:
            if not relinked["count"]:
                relinked["count"] += 1
                await relink_tenant(direct, tenant, other.builder)

        if when == "before_claim":
            original = credit_metering.precheck_events

            async def then_relink(*args: Any, **kwargs: Any) -> Any:
                verdict = await original(*args, **kwargs)
                await relink()
                return verdict

            monkeypatch.setattr(credit_metering, "precheck_events", then_relink)
        else:
            generate = EventService.generate_response

            async def mid_turn(self: Any, *args: Any, **kwargs: Any) -> Any:
                await relink()
                return await generate(self, *args, **kwargs)

            monkeypatch.setattr(EventService, "generate_response", mid_turn)
        async with asyncio.timeout(_TURN_SECONDS):
            if channel == "events":
                answer = await client.post(
                    "/v1/events",
                    json=metering_suite._events_body(world.api_session),
                    headers=_api(world.api_key, "rule-relinked-1"),
                )
            else:
                answer = await client.post(
                    f"/v1/webhook/zapi/{world.zapi_token}",
                    json=_zapi(world, "rule-relinked-zapi-1", "5511900008501"),
                )
        rows = await _tenant_rows(direct, tenant)
    assert relinked == {"count": 1}
    if when == "before_claim":
        assert answer.status_code == 503, answer.text
        assert answer.json() == {"code": "credit_enforcement_unavailable"}
        assert rows["claims"] == 0
    else:
        assert answer.status_code == 200, answer.text
        assert rows["effects"] == 0
    assert await _trace_debits(direct, builder) == []
    assert await _trace_debits(direct, other.builder) == []
    assert await _builder_rows(direct, other.builder) == other_before


async def test_the_rule_d1_is_per_tenant_and_another_builder_is_unaffected(
    pg: Any, direct: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DESIGN item 11: a tenant seen under the rule gets 503 when its rule
    cannot be read -- a failed read, or the 046 gate closed (a reading at
    045) -- while another builder's tenant answers as before; a successful
    read that is not under the rule (protected by a paid top-up) clears it;
    seeing the rule sets neither the global latch nor the mark set."""

    from src.services import credit_switch

    async with _app(pg, monkeypatch) as running:
        world, client = running.world, running.client
        builder, tenant = UUID(world.builder), UUID(world.api_tenant)
        other = await _builder_with_api_tenant(direct)
        granted = await grant_courtesy(direct, builder, 5)
        broken = {"on": False}
        original = CreditRepository._courtesy_enforcement

        async def maybe_broken(
            self: CreditRepository, *args: Any, **kwargs: Any
        ) -> Any:
            if broken["on"]:
                raise OperationalError("SELECT", {}, Exception("lots unreadable"))
            return await original(self, *args, **kwargs)

        monkeypatch.setattr(CreditRepository, "_courtesy_enforcement", maybe_broken)
        keys = iter(f"rule-d1-{n}" for n in range(100))

        async def post(session: str, key: str) -> Any:
            async with asyncio.timeout(_TURN_SECONDS):
                return await client.post(
                    "/v1/events",
                    json=metering_suite._events_body(session),
                    headers=_api(key, next(keys)),
                )

        charged = await post(world.api_session, world.api_key)
        seen = credit_metering.enforcement_rule_seen(tenant)
        broken["on"] = True
        refused = await post(world.api_session, world.api_key)
        unaffected = await post(str(other.session), other.api_key)
        broken["on"] = False
        head = credit_switch.credit_switch_reading()
        credit_switch._SWITCH.publish(
            credit_switch.CreditSwitchReading(True, "schema_ready", "045")
        )
        try:
            gate_closed = await post(world.api_session, world.api_key)
            other_gate_closed = await post(str(other.session), other.api_key)
        finally:
            credit_switch._SWITCH.publish(head)
        # Protected (no courtesy left, a paid top-up): read, not under it.
        await remove_courtesy(direct, granted.lot_id)
        await make_topup(_harness(direct), builder)
        cleared = await post(world.api_session, world.api_key)
        seen_after_clear = credit_metering.enforcement_rule_seen(tenant)
        broken["on"] = True
        free_again = await post(world.api_session, world.api_key)
        broken["on"] = False
        latches = (
            credit_metering.enforcement_seen(),
            credit_inventory._enforcement_seen,
            credit_metering.enforcement_mark_seen(tenant),
        )
    assert charged.status_code == 200, charged.text
    assert seen is True
    unavailable = {"code": "credit_enforcement_unavailable"}
    assert refused.status_code == 503 and refused.json() == unavailable
    assert unaffected.status_code == 200, unaffected.text
    assert gate_closed.status_code == 503 and gate_closed.json() == unavailable
    assert other_gate_closed.status_code == 200, other_gate_closed.text
    assert cleared.status_code == 200, cleared.text
    assert seen_after_clear is False
    assert free_again.status_code == 200, free_again.text
    assert latches == (False, False, False)
    # One decision was charged (the first); the others refused or free.
    assert len(await _trace_debits(direct, builder)) == 1


async def test_an_unknown_turn_is_charged_under_the_rule_and_digests_unknown(
    pg: Any, direct: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DESIGN item 10, on PostgreSQL, through the shared webhook path the
    Uazapi and Evolution adapters will call: an unauthenticated principal of
    a builder under the rule is enforced as ``unknown`` -- claimed, its
    decision debited from courtesy, its effect's ``decision_digest`` the
    digest of origin ``unknown`` -- and, once the builder is marked, the
    same principal is main's free path (nothing read past the mode)."""

    from src.interaction_context import InteractionOrigin
    from src.services.credit_principal import credit_identity

    force_credit_switch(monkeypatch)
    seeded = await metering_suite._tenant(direct)
    assert seeded.builder_id is not None
    granted = await grant_courtesy(direct, seeded.builder_id, 3)
    request = SimpleNamespace(
        scope={"state": {"credit_switch_effective": True}}, headers={}
    )

    async def precheck(message_id: str) -> Any:
        async with AsyncSession(direct) as db:
            return await credit_metering.precheck_webhook(
                request,  # type: ignore[arg-type]
                db,
                source="zapi",
                tenant_id=seeded.tenant_id,
                identity=lambda: credit_identity("uazapi", seeded.tenant_id),
                provider_message_id=message_id,
                payload_sha256=lambda: "e" * 64,
            )

    enforcement = await precheck("unknown-turn-1")
    assert isinstance(enforcement, credit_metering.WebhookEnforcement)
    assert enforcement.origin is InteractionOrigin.UNKNOWN
    response = SimpleNamespace(
        event_id=uuid4(),
        prompt=SimpleNamespace(suggested_response="Olá!", deterministic=False),
        handoff_summary=None,
        decision=None,
        mode=None,
    )

    async def pipeline() -> tuple[Any, Any, Any]:
        return SimpleNamespace(handoff_summary=None), None, []

    async def no_bundle(_effect: str | None) -> None:
        return None

    async with AsyncSession(direct) as db:
        lease = await credit_metering.acquire_webhook_claim(db, enforcement)
        assert isinstance(lease, credit_metering.ClaimLease)
        phase1 = await credit_metering.run_webhook_phase1(
            db,
            enforcement,
            lease,
            session_id=seeded.session_id,
            run_pipeline=pipeline,
        )
        assert phase1 is not None
        final = await credit_metering.run_webhook_final(
            db,
            enforcement,
            lease,
            phase1,
            response=response,
            write_bundle=no_bundle,
            reply=None,
        )
    assert final is not None and final.charge is not None
    decided = credit_metering.response_decision(response)
    assert decided is not None
    expected = {
        origin: credit_metering.decision_digest(
            tenant_id=seeded.tenant_id,
            interaction_id=lease.interaction_id,
            decision_version=credit_metering.ENFORCED_DECISION_VERSION,
            origin=origin,
            decision=decided[1],
        )
        for origin in (InteractionOrigin.UNKNOWN, InteractionOrigin.PRODUCTION)
    }
    assert await _trace_effects(direct, seeded.builder_id) == [
        expected[InteractionOrigin.UNKNOWN]
    ]
    assert expected[InteractionOrigin.UNKNOWN] != expected[InteractionOrigin.PRODUCTION]
    assert await _trace_debits(direct, seeded.builder_id) == [(1_000, granted.lot_id)]
    # Marked: the same unauthenticated turn is free (credit-system Decision 25).
    await record_mark(direct, seeded.builder_id)
    assert await precheck("unknown-turn-2") is None
    assert (await _tenant_rows(direct, seeded.tenant_id))["claims"] == 1


async def test_the_workers_run_on_a_courtesy_lot_alone_and_recover(
    pg: Any, direct: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DESIGN item 12, Decision 20: no global, no mark.  Without a courtesy
    lot the publisher looks for nothing and the orphan pass stops after its
    reads; once a courtesy lot exists the publisher recovers the pending
    charged reply (a crash after the final commit) and the orphan pass
    reconciles the rule's enforced claim left without its effect."""

    from src.services import credit_rollout
    from src.services.credit_rollout import (
        OrphanSweepState,
        run_credit_orphan_reconciliation_once,
    )

    force_credit_switch(monkeypatch)
    sessions = _harness(direct).sessions
    seeded = await metering_suite._tenant(direct)
    assert seeded.builder_id is not None
    row = await metering_suite._outbox_row(direct, seeded)  # a crash
    await execute_on(
        direct,
        "INSERT INTO credit_ingress_claims (id, tenant_id, builder_id, source, "
        "principal_id, idempotency_key, provider_message_id, interaction_id, "
        "payload_sha256, decision_version, mode) VALUES (:id, :t, :b, 'events', "
        ":p, :k, NULL, :i, :sha, 1, 'enforced')",
        id=uuid4(),
        t=seeded.tenant_id,
        b=seeded.builder_id,
        p=f"events:{seeded.tenant_id}",
        k="rule-orphan-1",
        i=uuid4(),
        sha="d" * 64,
    )
    sender = metering_suite._Recorded()
    publisher = credit_metering.CreditResponsePublisher(
        sender, session_factory=sessions
    )
    credit_rollout.CREDIT_ORPHAN_EFFECTS.set(-1)  # a sentinel no pass writes

    dormant = await publisher.run_once()
    dormant_orphans = await run_credit_orphan_reconciliation_once(
        sessions, state=OrphanSweepState()
    )
    dormant_gauge = metering_suite_gauge()
    await grant_courtesy(direct, seeded.builder_id, 1)
    tick = await publisher.run_once()
    published = await run_credit_orphan_reconciliation_once(
        sessions, state=OrphanSweepState()
    )

    assert dormant is not None and not dormant.enforced and dormant.outcomes == []
    assert dormant_orphans == 0 and dormant_gauge == -1
    assert tick is not None and tick.enforced and tick.outcomes == ["delivered"]
    assert sender.sent == [str(row)]
    state, payload, attempts = await metering_suite._outbox_state(direct, row)
    assert (state, attempts) == ("delivered", 1) and "phone" not in payload
    assert published >= 1 and metering_suite_gauge() == published


def metering_suite_gauge() -> float:
    """``neurostack_credit_orphan_effects`` as a scrape would read it."""

    from src.services import credit_rollout

    (value,) = [
        sample.value
        for metric in credit_rollout.CREDIT_ORPHAN_EFFECTS.collect()
        for sample in metric.samples
    ]
    return value


async def _plant_episode(engine: AsyncEngine, builder_id: UUID) -> None:
    """An exhaustion episode left from a mark era, past its suspension
    deadline (and still open)."""

    at = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=5)
    await execute_on(
        engine,
        "INSERT INTO credit_accounts (id, builder_id) VALUES (:a, :b) "
        "ON CONFLICT DO NOTHING",
        a=uuid4(),
        b=builder_id,
    )
    await execute_on(
        engine,
        "INSERT INTO credit_lifecycle_episodes (id, builder_id, account_id, "
        "opened_effect_id, exhausted_at, suspension_due_at, deletion_due_at) "
        "SELECT :id, :b, id, :effect, :at, :due, :deletion FROM credit_accounts "
        "WHERE builder_id = :b",
        id=uuid4(),
        b=builder_id,
        effect=f"debit:mark-era:{uuid4()}",
        at=at,
        due=at + timedelta(hours=72),
        deletion=at + timedelta(days=30),
    )


async def test_the_shared_block_gate_reports_the_rule_and_ignores_a_stale_episode(
    pg: Any, direct: AsyncEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``tenant_suspended`` and ``reservation_verdict`` with the caller's
    captured mode (what Groups 3, 4 and 5a consume): ``blocked`` is
    suspended; ``charged`` is served even with an open, past-due episode
    left from a mark era -- which, without a capture, still decides the
    reservation verdict as before (the control)."""

    from src.services import credit_lifecycle

    force_credit_switch(monkeypatch)
    charged = await metering_suite._tenant(direct)
    blocked = await metering_suite._tenant(direct)
    assert charged.builder_id is not None and blocked.builder_id is not None
    kept = await grant_courtesy(direct, charged.builder_id, 3)
    spent = await grant_courtesy(direct, blocked.builder_id, 3)
    await remove_courtesy(direct, spent.lot_id)
    for seeded in (charged, blocked):
        await _plant_episode(direct, seeded.builder_id)  # type: ignore[arg-type]
    captured_charged = await effective_of(direct, charged.builder_id)
    captured_blocked = await effective_of(direct, blocked.builder_id)
    assert captured_charged == _charged(kept.occurred_at)
    assert captured_blocked == _blocked(spent.occurred_at)

    async def verdicts(seeded: Any, captured: Any) -> tuple[bool, str, str]:
        async with AsyncSession(direct) as session:
            suspended = await credit_lifecycle.tenant_suspended(
                session, seeded.tenant_id, captured=captured
            )
            charged_verdict = await credit_lifecycle.reservation_verdict(
                session,
                builder_id=seeded.builder_id,
                effect_id=f"rag:{seeded.tenant_id}:{uuid4()}:reserve",
                charged_mc=1_000,
                captured=captured,
            )
            free_verdict = await credit_lifecycle.reservation_verdict(
                session,
                builder_id=seeded.builder_id,
                effect_id=f"rag:{seeded.tenant_id}:{uuid4()}:reserve",
                charged_mc=0,
                captured=captured,
            )
            await session.rollback()
        return suspended, charged_verdict, free_verdict

    assert await verdicts(charged, captured_charged) == (False, "ok", "ok")
    assert await verdicts(blocked, captured_blocked) == (
        True,
        "suspended",
        "suspended",
    )
    # The control: without a capture the stale episode still decides the
    # reservation verdict (an exhausted category), as before the rule.
    assert (await verdicts(charged, None))[1] == "category_exhausted"
