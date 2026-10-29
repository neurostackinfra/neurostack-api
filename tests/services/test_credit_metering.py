"""Observational metering (neurostack-credit-system Group 3a) on SQLite.

Three layers, PostgreSQL-only proofs (pool, lock and statement timeouts,
triggers, a lost connection, the budget under contention) live in
``tests/integration/test_credit_metering_postgres.py``:

* the pure rules: the trace effect id, the decision digest (pinned), the
  payload digests, the ingress ids and the cost matrix;
* the writer (``record_observational_trace``) against its own SQLite file:
  claim then effect, per-tenant deduplication with the existing claim's
  ``interaction_id`` reused, no claim without a key or for simulator/unknown,
  nothing once ``credit_enforcement_at`` is set, the single budget around
  checkout, statements and commit, failures logged and swallowed;
* the real app (``src.api.main.app``, SQLite, LLM and providers faked) with
  the switch forced on per test: every ingress records the right claim and
  observational trace (origin from the live context, ``authenticated`` from
  the derived principal), the anchor is the claim or the public ``event_id``
  (never ``context.interaction_event_id``), every path outside the allowlist
  writes nothing, a writer failure leaves answer and events as ``main``'s, and
  with the switch off no SQL touches a credit table.
"""

from __future__ import annotations

import ast
import asyncio
import functools
import hashlib
import inspect
import json
import secrets
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import jwt
import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import src.models  # noqa: F401  (registers every mapper for create_all)
from src.api.middleware.auth import pwd_context
from src.api.routes import events as events_routes
from src.api.routes import webhook
from src.api.schemas.events import (
    CanonicalEventRequest,
    EventPayload,
    EventRequest,
    EventType,
)
from src.config.settings import settings
from src.interaction_context import (
    InteractionOrigin,
    build_interaction_context,
    unknown_interaction_context,
)
from src.models.base import Base
from src.models.builder import Builder
from src.models.clinic_config_table import ClinicConfigTable
from src.models.credit import (
    CreditEnforcementState,
    CreditIngressClaim,
    CreditObservationalEffect,
)
from src.models.event import Event as EventRow
from src.models.session import Session
from src.models.tenant import Tenant
from src.repositories.credit import CreditRepository
from src.repositories.database import (
    _patch_jsonb_for_sqlite,
    get_db_engine,
    get_db_session,
)
from src.services import credit_metering
from src.services.credit_metering import (
    DECISION_DIGEST_SCHEMA,
    OBSERVATIONAL_WRITE_BUDGET_SECONDS,
    TRACE_COST_MC,
    ObservationalIngress,
    decision_digest,
    deliverable_response,
    end_route_transaction,
    event_payload_sha256,
    provider_message_sha256,
    record_observational_trace,
    response_decision,
    trace_effect_id,
    valid_ingress_id,
    would_debit_mc,
    zapi_payload_sha256,
)
from src.services.credit_principal import credit_identity
from src.services.credit_switch import CreditSwitch

EVENTS = "/v1/events"
META = "/v1/webhook/meta"
_LLM_REPLY = "Olá! Posso ajudar com o seu agendamento."
_BUILDER = UUID("b0000000-0000-4000-8000-0000000000b1")


# ---------------------------------------------------------------------------
# The pure rules.
# ---------------------------------------------------------------------------


def test_one_trace_costs_one_credit() -> None:
    assert TRACE_COST_MC == 1_000
    assert OBSERVATIONAL_WRITE_BUDGET_SECONDS == 0.25


def test_the_trace_effect_id_is_keyed_by_tenant_and_decision_version() -> None:
    tenant, interaction = uuid4(), uuid4()
    assert trace_effect_id(tenant, interaction) == (
        f"trace:{tenant}:{interaction}:decision:1"
    )
    assert trace_effect_id(tenant, interaction, 2).endswith(":decision:2")
    assert trace_effect_id(tenant, interaction) == trace_effect_id(tenant, interaction)


def test_the_decision_digest_is_canonical_and_pinned() -> None:
    tenant = UUID("11111111-1111-4111-8111-111111111111")
    interaction = UUID("22222222-2222-4222-8222-222222222222")
    decision = {"kind": "pipeline", "intent": "agendar", "handoff": False}
    digest = decision_digest(
        tenant_id=tenant,
        interaction_id=interaction,
        decision_version=1,
        origin=InteractionOrigin.PRODUCTION,
        decision=decision,
    )
    expected = hashlib.sha256(
        json.dumps(
            {
                "schema": DECISION_DIGEST_SCHEMA,
                "tenant_id": str(tenant),
                "interaction_id": str(interaction),
                "decision_version": 1,
                "origin": "production",
                "decision": decision,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    assert digest == expected
    # Pinned: a change of the canonical form is a change of contract.
    assert digest == "afdeb03a003c49d5e15a6658e42648fc4cbbc018572e5ef7860fefdff96d0fc6"
    assert DECISION_DIGEST_SCHEMA == "credit_decision.v1"
    reordered = dict(reversed(list(decision.items())))
    assert (
        decision_digest(
            tenant_id=tenant,
            interaction_id=interaction,
            decision_version=1,
            origin=InteractionOrigin.PRODUCTION,
            decision=reordered,
        )
        == digest
    )
    assert (
        decision_digest(
            tenant_id=tenant,
            interaction_id=interaction,
            decision_version=1,
            origin=InteractionOrigin.SIMULATOR,
            decision=decision,
        )
        != digest
    )


def test_the_event_payload_digest_is_the_canonical_request() -> None:
    request = CanonicalEventRequest(
        tenant_id=uuid4(),
        session_id=uuid4(),
        event_type=EventType.MESSAGE_IN,
        payload=EventPayload(content="Olá", channel="whatsapp"),
    )
    expected = hashlib.sha256(
        json.dumps(
            request.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    assert event_payload_sha256(request) == expected
    other = request.model_copy(
        update={"payload": EventPayload(content="Oi", channel="whatsapp")}
    )
    assert event_payload_sha256(other) != expected


def test_the_zapi_digest_reads_only_the_stable_fields() -> None:
    body = {
        "instanceId": "3E1",
        "messageId": "m-1",
        "phone": "5511900000001",
        "fromMe": False,
        "text": {"message": "Olá"},
        "momment": 1,
        "senderName": "A",
    }
    volatile = {**body, "momment": 2, "senderName": "B", "chatLid": "x@lid"}
    assert zapi_payload_sha256(body) == zapi_payload_sha256(volatile)
    assert zapi_payload_sha256(body) != zapi_payload_sha256(
        {**body, "text": {"message": "Oi"}}
    )
    assert provider_message_sha256({"id": "a"}) != provider_message_sha256({"id": "b"})


@pytest.mark.parametrize(
    ("raw", "valid"),
    [
        ("key-1", True),
        ("k" * 255, True),
        ("k" * 256, False),
        ("", False),
        ("with space", False),
        ("ação", False),
        (None, False),
        (7, False),
    ],
)
def test_an_ingress_id_is_one_to_255_visible_ascii_characters(
    raw: object, valid: bool
) -> None:
    assert valid_ingress_id(raw) == (raw if valid else None)


@pytest.mark.parametrize(
    ("origin", "authenticated", "deliverable", "cost"),
    [
        (InteractionOrigin.PRODUCTION, True, True, 1_000),
        (InteractionOrigin.PRODUCTION, True, False, 0),
        (InteractionOrigin.PRODUCTION, False, True, 0),
        (InteractionOrigin.SIMULATOR, True, True, 0),
        (InteractionOrigin.UNKNOWN, True, True, 0),
        (InteractionOrigin.UNKNOWN, False, True, 0),
    ],
)
def test_only_an_authenticated_production_decision_would_debit(
    origin: InteractionOrigin, authenticated: bool, deliverable: bool, cost: int
) -> None:
    assert (
        would_debit_mc(
            origin=origin, authenticated=authenticated, deliverable=deliverable
        )
        == cost
    )


def test_a_webhook_decision_is_deliverable_with_an_answer_or_a_handoff() -> None:
    def response(suggested: str | None, handoff: object) -> SimpleNamespace:
        return SimpleNamespace(
            prompt=SimpleNamespace(suggested_response=suggested),
            handoff_summary=handoff,
        )

    assert deliverable_response(response("Olá", None))
    assert deliverable_response(response(None, object()))
    assert not deliverable_response(response(None, None))
    assert not deliverable_response(response("", None))
    assert not deliverable_response(object())


def test_the_anchor_is_the_public_event_id_never_the_interaction_event_id() -> None:
    event_id, interaction_event_id = uuid4(), uuid4()
    response = SimpleNamespace(
        event_id=event_id,
        interaction_event_id=interaction_event_id,
        decision=SimpleNamespace(intent="agendar", handoff_required=False),
        prompt=SimpleNamespace(suggested_response="Olá", deterministic=False),
        handoff_summary=None,
        mode="normal",
    )
    decided = response_decision(response)
    assert decided is not None
    anchor, projection = decided
    assert anchor == event_id
    assert str(interaction_event_id) not in json.dumps(projection)
    assert "Olá" not in json.dumps(projection)  # no reply text, no PII
    assert response_decision(SimpleNamespace(event_id="not-a-uuid")) is None
    assert response_decision({"event_id": str(event_id)}) is None


# ---------------------------------------------------------------------------
# The writer, against its own SQLite file.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Store:
    sessions: async_sessionmaker[AsyncSession]
    builder: UUID
    tenant: UUID
    standalone: UUID
    session: UUID
    standalone_session: UUID


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[_Store]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'metering.db'}")
    try:
        yield await _seed_store(engine)
    finally:
        await engine.dispose()


async def _seed_store(engine: Any) -> _Store:
    _patch_jsonb_for_sqlite()
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    seeded = _Store(sessions, uuid4(), uuid4(), uuid4(), uuid4(), uuid4())
    async with sessions() as session, session.begin():
        session.add(Builder(id=seeded.builder, name="Metering builder"))
        await session.flush()
        for tenant_id, builder_id in (
            (seeded.tenant, seeded.builder),
            (seeded.standalone, None),
        ):
            session.add(_tenant_row(tenant_id, builder_id))
        await session.flush()
        session.add(_session_row(seeded.session, seeded.tenant, message_count=3))
        session.add(_session_row(seeded.standalone_session, seeded.standalone))
    return seeded


def _tenant_row(tenant_id: UUID, builder_id: UUID | None, **extra: Any) -> Tenant:
    return Tenant(
        id=tenant_id,
        name=f"Metering {tenant_id.hex[:8]}",
        slug=f"metering-{tenant_id.hex}",
        api_key_hash=extra.pop("api_key_hash", f"unused-{tenant_id.hex}"),
        builder_id=builder_id,
        config={},
        **extra,
    )


def _session_row(
    session_id: UUID, tenant_id: UUID, *, message_count: int = 0, **extra: Any
) -> Session:
    return Session(
        id=session_id,
        tenant_id=tenant_id,
        channel=extra.pop("channel", "test"),
        cognitive_state={},
        session_metadata={},
        message_count=message_count,
        **extra,
    )


def _ingress(
    tenant_id: UUID,
    session_id: UUID,
    *,
    source: str = "events",
    origin: InteractionOrigin = InteractionOrigin.PRODUCTION,
    kind: str | None = "api_key",
    key: str | None = "key-1",
    message: str | None = None,
) -> ObservationalIngress:
    return ObservationalIngress(
        source=source,  # type: ignore[arg-type]
        tenant_id=tenant_id,
        session_id=session_id,
        origin=origin,
        identity=None if kind is None else credit_identity(kind, tenant_id),
        payload_sha256="a" * 64,
        idempotency_key=key,
        provider_message_id=message,
    )


async def _all(store: _Store, model: type[Any]) -> list[Any]:
    async with store.sessions() as session:
        return list((await session.execute(select(model))).scalars())


async def _write(
    store: _Store, ingress: ObservationalIngress, anchor: UUID, **options: Any
) -> str:
    # Not a budget test: a generous budget keeps loaded CI from timing out
    # (the budget tests call record_observational_trace with small values).
    options.setdefault("budget_seconds", 5)
    return await record_observational_trace(
        ingress,
        anchor_id=anchor,
        decision=options.pop("decision", {"kind": "pipeline"}),
        deliverable=options.pop("deliverable", True),
        session_factory=store.sessions,
        **options,
    )


class _Recorder:
    """Stands in for the module logger (a module-attribute swap)."""

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []
        self.infos: list[tuple[str, dict[str, Any]]] = []

    def warning(self, message: str, **fields: Any) -> None:
        self.warnings.append((message, fields))

    def info(self, message: str, **fields: Any) -> None:
        self.infos.append((message, fields))

    def reasons(self) -> list[Any]:
        return [fields.get("reason") for _message, fields in self.warnings]


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(credit_metering, "logger", recorder)
    return recorder


async def test_a_production_event_with_a_key_records_its_claim_and_trace(
    store: _Store, logs: _Recorder
) -> None:
    anchor = uuid4()
    outcome = await _write(store, _ingress(store.tenant, store.session), anchor)
    assert outcome == "recorded"
    [claim] = await _all(store, CreditIngressClaim)
    assert (claim.tenant_id, claim.builder_id, claim.source) == (
        store.tenant,
        store.builder,
        "events",
    )
    assert claim.principal_id == f"api_key:{store.tenant}"
    assert (claim.idempotency_key, claim.provider_message_id) == ("key-1", None)
    assert (claim.interaction_id, claim.decision_version) == (anchor, 1)
    assert (claim.session_version, claim.mode) == (3, "observational")
    [effect] = await _all(store, CreditObservationalEffect)
    assert effect.effect_id == trace_effect_id(store.tenant, anchor)
    assert (effect.effect_kind, effect.trace_id, effect.builder_id) == (
        "trace",
        anchor,
        store.builder,
    )
    assert (effect.origin, effect.authenticated, effect.would_debit_mc) == (
        "production",
        True,
        1_000,
    )
    assert effect.decision_digest == decision_digest(
        tenant_id=store.tenant,
        interaction_id=anchor,
        decision_version=1,
        origin=InteractionOrigin.PRODUCTION,
        decision={"kind": "pipeline"},
    )
    assert logs.warnings == []


async def test_a_retry_reuses_the_claim_s_interaction_and_writes_no_second_trace(
    store: _Store,
) -> None:
    first, retry = uuid4(), uuid4()
    ingress = _ingress(store.tenant, store.session)
    assert await _write(store, ingress, first) == "recorded"
    assert await _write(store, ingress, retry) == "replayed"
    [claim] = await _all(store, CreditIngressClaim)
    [effect] = await _all(store, CreditObservationalEffect)
    assert claim.interaction_id == effect.trace_id == first


@pytest.mark.parametrize(
    ("origin", "kind", "key"),
    [
        (InteractionOrigin.PRODUCTION, "api_key", None),  # no key: no claim
        (InteractionOrigin.SIMULATOR, "jwt", "key-1"),  # panel JWT (H3)
        (InteractionOrigin.UNKNOWN, None, "key-1"),  # caller without proof
    ],
)
async def test_no_claim_without_a_key_or_outside_production(
    store: _Store, origin: InteractionOrigin, kind: str | None, key: str | None
) -> None:
    identity = {
        "api_key": credit_identity("api_key", store.tenant),
        "jwt": credit_identity("jwt", store.tenant, uuid4()),
        None: None,
    }[kind]
    ingress = replace(
        _ingress(store.tenant, store.session, origin=origin, key=key),
        identity=identity,
    )
    anchors = [uuid4(), uuid4()]
    for anchor in anchors:
        assert await _write(store, ingress, anchor) == "recorded"
    assert await _all(store, CreditIngressClaim) == []
    effects = await _all(store, CreditObservationalEffect)
    assert sorted(e.trace_id for e in effects) == sorted(anchors)
    expected_cost = 1_000 if origin is InteractionOrigin.PRODUCTION else 0
    assert {e.would_debit_mc for e in effects} == {expected_cost}
    assert {e.origin for e in effects} == {origin.value}
    assert {e.authenticated for e in effects} == {kind is not None}


@pytest.mark.parametrize("source", ["uazapi", "evolution"])
async def test_uazapi_and_evolution_are_unknown_and_unauthenticated(
    store: _Store, source: str
) -> None:
    ingress = _ingress(
        store.tenant,
        store.session,
        source=source,
        origin=InteractionOrigin.UNKNOWN,
        kind=source,
        key=None,
        message=f"{source}-msg-1",
    )
    anchor = uuid4()
    assert await _write(store, ingress, anchor) == "recorded"
    [claim] = await _all(store, CreditIngressClaim)
    assert (claim.source, claim.principal_id) == (source, f"{source}:{store.tenant}")
    assert claim.provider_message_id == f"{source}-msg-1"
    [effect] = await _all(store, CreditObservationalEffect)
    assert (effect.origin, effect.authenticated, effect.would_debit_mc) == (
        "unknown",
        False,
        0,
    )


async def test_provider_claims_deduplicate_by_tenant_standalone_and_reparented(
    store: _Store,
) -> None:
    standalone = _ingress(
        store.standalone,
        store.standalone_session,
        source="zapi",
        kind="zapi",
        key=None,
        message="zapi-1",
    )
    first = uuid4()
    assert await _write(store, standalone, first) == "recorded"
    assert await _write(store, standalone, uuid4()) == "replayed"
    # Reparent the standalone tenant under the builder: still one claim.
    async with store.sessions() as session, session.begin():
        tenant = await session.get(Tenant, store.standalone)
        assert tenant is not None
        tenant.builder_id = store.builder
    assert await _write(store, standalone, uuid4()) == "replayed"
    [claim] = await _all(store, CreditIngressClaim)
    assert (claim.builder_id, claim.interaction_id) == (None, first)
    [effect] = await _all(store, CreditObservationalEffect)
    assert (effect.builder_id, effect.trace_id) == (None, first)
    # The same message id of ANOTHER tenant is another claim.
    other = _ingress(
        store.tenant,
        store.session,
        source="zapi",
        kind="zapi",
        key=None,
        message="zapi-1",
    )
    assert await _write(store, other, uuid4()) == "recorded"
    assert len(await _all(store, CreditIngressClaim)) == 2


@pytest.mark.parametrize("source", ["events", "zapi"])
async def test_a_retry_never_reuses_another_tenant_s_claim(
    store: _Store, source: str
) -> None:
    """Two tenants with the same key (or provider message id): each retry
    reuses only its own tenant's claim."""

    def ingress(tenant_id: UUID, session_id: UUID) -> ObservationalIngress:
        if source == "events":
            return _ingress(tenant_id, session_id, key="shared-key")
        return _ingress(
            tenant_id,
            session_id,
            source="zapi",
            kind="zapi",
            key=None,
            message="shared-message",
        )

    first, other, retry = uuid4(), uuid4(), uuid4()
    assert await _write(store, ingress(store.tenant, store.session), first) == (
        "recorded"
    )
    assert (
        await _write(store, ingress(store.standalone, store.standalone_session), other)
        == "recorded"
    )
    assert await _write(store, ingress(store.tenant, store.session), retry) == (
        "replayed"
    )
    effects = await _all(store, CreditObservationalEffect)
    assert sorted((e.tenant_id, e.trace_id) for e in effects) == sorted(
        [(store.tenant, first), (store.standalone, other)]
    )
    assert len(await _all(store, CreditIngressClaim)) == 2


async def test_nothing_is_written_once_the_enforcement_is_set(
    store: _Store, logs: _Recorder
) -> None:
    async with store.sessions() as session, session.begin():
        session.add(
            CreditEnforcementState(
                id=1,
                credit_enforcement_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                set_by="test",
                set_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
            )
        )
    assert await _write(store, _ingress(store.tenant, store.session), uuid4()) == (
        "enforced"
    )
    assert await _all(store, CreditIngressClaim) == []
    assert await _all(store, CreditObservationalEffect) == []
    # The steady state after the enforcement: info, never an alert.
    assert logs.warnings == []
    [(message, fields)] = logs.infos
    assert message == "credit observational write dropped"
    assert fields["reason"] == "enforced" and "alert" not in fields


async def test_a_deleted_tenant_writes_nothing(store: _Store, logs: _Recorder) -> None:
    ingress = _ingress(uuid4(), uuid4())
    assert await _write(store, ingress, uuid4()) == "tenant_gone"
    assert await _all(store, CreditObservationalEffect) == []
    assert logs.reasons() == ["tenant_gone"]


async def test_a_failure_is_logged_swallowed_and_rolls_the_claim_back(
    store: _Store, logs: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def refuse(*_args: Any, **_kwargs: Any) -> bool:
        raise IntegrityError("INSERT", {}, Exception("append-only"))

    monkeypatch.setattr(CreditRepository, "insert_observational_effect", refuse)
    outcome = await _write(store, _ingress(store.tenant, store.session), uuid4())
    assert outcome == "failed"
    assert await _all(store, CreditIngressClaim) == []  # its transaction rolled back
    [(message, fields)] = logs.warnings
    assert message == "credit observational write dropped"
    assert fields["alert"] == "credit_observational_write"
    assert (fields["reason"], fields["error_type"]) == ("failed", "IntegrityError")


class _SlowFactory:
    """A session factory whose sessions stall at each point of ``stall``
    (``"checkout"``, ``"commit"``, ``"close"`` -- the first close only -- or
    several, ``"checkout+commit"``)."""

    def __init__(self, store: _Store, stall: str, seconds: float) -> None:
        self.store, self.seconds = store, seconds
        self.stalls = frozenset(stall.split("+"))
        self.closed = 0

    def __call__(self) -> AsyncSession:
        session = self.store.sessions()
        factory = self
        original_connection, original_commit = session.connection, session.commit
        original_close = session.close

        async def connection(*args: Any, **kwargs: Any) -> Any:
            if "checkout" in factory.stalls:
                await asyncio.sleep(factory.seconds)
            return await original_connection(*args, **kwargs)

        async def commit() -> None:
            if "commit" in factory.stalls:
                await asyncio.sleep(factory.seconds)
            await original_commit()

        async def close() -> None:
            factory.closed += 1
            if "close" in factory.stalls and factory.closed == 1:
                await asyncio.sleep(factory.seconds)  # the ROLLBACK round trip
            await original_close()

        session.connection = connection  # type: ignore[method-assign]
        session.commit = commit  # type: ignore[method-assign]
        session.close = close  # type: ignore[method-assign]
        return session


@pytest.mark.parametrize("stall", ["checkout", "commit"])
async def test_one_budget_covers_checkout_statements_and_commit(
    store: _Store,
    logs: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
    stall: str,
) -> None:
    invalidated: list[object] = []

    async def spy(connection: object) -> None:
        invalidated.append(connection)

    monkeypatch.setattr(credit_metering, "_invalidate_connection", spy)
    factory = _SlowFactory(store, stall, seconds=5)
    started = time.monotonic()
    outcome = await record_observational_trace(
        _ingress(store.tenant, store.session),
        anchor_id=uuid4(),
        decision={},
        deliverable=True,
        session_factory=factory,
        budget_seconds=0.2,
    )
    elapsed = time.monotonic() - started
    assert outcome == "timeout"
    assert elapsed < 1.0, elapsed  # one total budget, not one per statement
    assert factory.closed == 1
    # A connection taken before the cancellation is invalidated, never pooled.
    assert len(invalidated) == (1 if stall == "commit" else 0)
    assert logs.reasons() == ["timeout"]
    assert await _all(store, CreditObservationalEffect) == []


async def test_the_budget_is_one_total_not_one_per_step(
    store: _Store, logs: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checkout and commit each stall 0.15 s, under the 0.2 s budget one by
    one but 0.3 s together: only ONE budget around the whole write drops it
    (a per-step budget, or a doubled one, would let it commit)."""

    invalidated: list[object] = []

    async def spy(connection: object) -> None:
        invalidated.append(connection)

    monkeypatch.setattr(credit_metering, "_invalidate_connection", spy)
    factory = _SlowFactory(store, "checkout+commit", seconds=0.15)
    outcome = await record_observational_trace(
        _ingress(store.tenant, store.session),
        anchor_id=uuid4(),
        decision={},
        deliverable=True,
        session_factory=factory,
        budget_seconds=0.2,
    )
    assert outcome == "timeout"
    assert factory.closed == 1
    assert len(invalidated) == 1  # cancelled in the commit, after the checkout
    assert logs.reasons() == ["timeout"]
    assert await _all(store, CreditIngressClaim) == []
    assert await _all(store, CreditObservationalEffect) == []


@pytest.mark.parametrize("path", ["tenant_gone", "enforced"])
async def test_the_close_rollback_runs_inside_the_budget(
    store: _Store,
    logs: _Recorder,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    """The paths that end with an open transaction close it with a ROLLBACK
    round trip: that close is inside the same budget, and a close cancelled
    by it invalidates the connection instead of pooling it mid-statement."""

    if path == "enforced":
        async with store.sessions() as session, session.begin():
            session.add(
                CreditEnforcementState(
                    id=1,
                    credit_enforcement_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                    set_by="test",
                    set_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
                )
            )
        ingress = _ingress(store.tenant, store.session)
    else:
        ingress = _ingress(uuid4(), uuid4())
    invalidated: list[object] = []
    original_invalidate = credit_metering._invalidate_connection

    async def spy(connection: AsyncConnection) -> None:
        invalidated.append(connection)
        await original_invalidate(connection)

    monkeypatch.setattr(credit_metering, "_invalidate_connection", spy)
    factory = _SlowFactory(store, "close", seconds=5)
    started = time.monotonic()
    outcome = await record_observational_trace(
        ingress,
        anchor_id=uuid4(),
        decision={},
        deliverable=True,
        session_factory=factory,
        budget_seconds=0.2,
    )
    elapsed = time.monotonic() - started
    assert outcome == "timeout"
    assert elapsed < 1.0, elapsed  # the close never outlives the budget
    assert len(invalidated) == 1  # cancelled in the close, never pooled
    assert factory.closed == 2  # released again, without a round trip
    assert logs.reasons() == ["timeout"]
    assert await _all(store, CreditObservationalEffect) == []
    # The pool still serves the next write.
    assert await _write(store, _ingress(store.tenant, store.session), uuid4()) == (
        "enforced" if path == "enforced" else "recorded"
    )


async def test_a_close_failing_with_an_ordinary_error_returns_no_connection(
    store: _Store, logs: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not only a cancellation: a close that fails with an ordinary error
    invalidates the checked-out connection and releases the session, so the
    pool lends nothing afterwards."""

    invalidated: list[object] = []
    original_invalidate = credit_metering._invalidate_connection

    async def spy(connection: AsyncConnection) -> None:
        invalidated.append(connection)
        await original_invalidate(connection)

    monkeypatch.setattr(credit_metering, "_invalidate_connection", spy)
    closes = 0

    def factory() -> AsyncSession:
        session = store.sessions()
        original_close = session.close

        async def close() -> None:
            nonlocal closes
            closes += 1
            if closes == 1:
                raise RuntimeError("close failed")
            await original_close()

        session.close = close  # type: ignore[method-assign]
        return session

    outcome = await record_observational_trace(
        _ingress(uuid4(), uuid4()),  # tenant_gone: closes an open transaction
        anchor_id=uuid4(),
        decision={},
        deliverable=True,
        session_factory=factory,
        budget_seconds=5,
    )
    assert outcome == "failed"
    [(_message, fields)] = logs.warnings
    assert fields["error_type"] == "RuntimeError"
    assert len(invalidated) == 1  # the connection was never pooled as is
    assert closes == 2  # released again after the failed close
    pool = store.sessions.kw["bind"].pool
    assert pool.checkedout() == 0  # nothing left on loan
    assert await _write(store, _ingress(store.tenant, store.session), uuid4()) == (
        "recorded"
    )


async def test_a_timeout_never_destroys_an_in_memory_database(
    logs: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SQLite ``:memory:`` is ONE pooled connection (``StaticPool``): a
    statement cancelled by the budget makes SQLAlchemy invalidate it, which
    drops the whole database (the ``no such table`` flake of the suites).
    There the deadline is checked between the steps instead: the slow
    statement finishes, the write is dropped as ``timeout`` and rolled back,
    and the tables and the next write survive."""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        assert type(engine.pool).__name__ == "StaticPool"
        memory = await _seed_store(engine)
        original = CreditRepository.insert_observational_effect

        async def slow_effect(self: CreditRepository, **kwargs: Any) -> bool:
            # ~0.5 s of real work on the connection: past the 0.1 s budget.
            await self.session.execute(
                text(
                    "WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 "
                    "FROM r WHERE n < 3000000) SELECT count(*) FROM r"
                )
            )
            return await original(self, **kwargs)

        with monkeypatch.context() as slow:
            slow.setattr(CreditRepository, "insert_observational_effect", slow_effect)
            outcome = await record_observational_trace(
                _ingress(memory.tenant, memory.session),
                anchor_id=uuid4(),
                decision={},
                deliverable=True,
                session_factory=memory.sessions,
                budget_seconds=0.1,
            )
        assert outcome == "timeout"
        assert logs.reasons() == ["timeout"]
        tenants = await _all(memory, Tenant)  # the schema and seed survived
        assert {t.id for t in tenants} == {memory.tenant, memory.standalone}
        assert await _all(memory, CreditIngressClaim) == []  # rolled back
        assert await _all(memory, CreditObservationalEffect) == []
        anchor = uuid4()
        assert await _write(
            memory, _ingress(memory.tenant, memory.session), anchor
        ) == ("recorded")
        [effect] = await _all(memory, CreditObservationalEffect)
        assert effect.trace_id == anchor
    finally:
        await engine.dispose()


async def test_an_exhausted_pool_drops_the_write_without_waiting(
    store: _Store, logs: _Recorder, tmp_path: Path
) -> None:
    """``pool_size=1, max_overflow=0`` with its only connection held (the
    process pool's shape in small): the writer never queues for it, even
    with a long checkout timeout and a long budget."""

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'metering.db'}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=30,
    )
    sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        held = await engine.connect()
        try:
            assert engine.pool.checkedout() == 1  # type: ignore[attr-defined]
            started = time.monotonic()
            outcome = await record_observational_trace(
                _ingress(store.tenant, store.session),
                anchor_id=uuid4(),
                decision={},
                deliverable=True,
                session_factory=sessions,
                budget_seconds=5,
            )
            elapsed = time.monotonic() - started
            assert engine.pool.checkedout() == 1  # type: ignore[attr-defined]
            assert (await held.execute(select(Tenant.id))).first() is not None
        finally:
            await held.close()
        assert outcome == "pool_exhausted"
        assert elapsed < 0.1, elapsed
        [(message, fields)] = logs.warnings
        assert message == "credit observational write dropped"
        assert fields["alert"] == "credit_observational_write"
        assert fields["reason"] == "pool_exhausted"
        assert await _all(store, CreditObservationalEffect) == []
        # Once a connection is free again the same pool serves the write.
        free = await record_observational_trace(
            _ingress(store.tenant, store.session),
            anchor_id=uuid4(),
            decision={},
            deliverable=True,
            session_factory=sessions,
        )
        assert free == "recorded"
        assert engine.pool.checkedout() == 0  # type: ignore[attr-defined]
    finally:
        await engine.dispose()


async def test_a_cancelled_request_still_propagates(store: _Store) -> None:
    factory = _SlowFactory(store, "checkout", seconds=5)
    task = asyncio.create_task(
        record_observational_trace(
            _ingress(store.tenant, store.session),
            anchor_id=uuid4(),
            decision={},
            deliverable=True,
            session_factory=factory,
            budget_seconds=10,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_an_uninitialised_pool_is_a_logged_drop(
    logs: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.repositories import database

    monkeypatch.setattr(database, "_session_factory", None)
    outcome = await record_observational_trace(
        _ingress(uuid4(), uuid4()), anchor_id=uuid4(), decision={}, deliverable=True
    )
    assert outcome == "failed"
    assert logs.reasons() == ["failed"]


async def test_the_route_transaction_is_ended_before_the_writer(store: _Store) -> None:
    async with store.sessions() as session:
        await session.execute(select(Tenant.id))
        assert session.in_transaction()
        assert await end_route_transaction(session) is True
        assert not session.in_transaction()
        assert await end_route_transaction(session) is True  # idle: no-op
    mocked = SimpleNamespace(in_transaction=AsyncMock(return_value=True))
    assert await end_route_transaction(mocked) is True  # no real connection
    assert await end_route_transaction(SimpleNamespace()) is True

    class Stuck:
        def in_transaction(self) -> bool:
            return True

        async def rollback(self) -> None:
            raise RuntimeError("connection lost")

    assert await end_route_transaction(Stuck()) is False


@pytest.mark.parametrize("pending", ["new", "dirty", "deleted"])
async def test_the_route_transaction_is_never_rolled_back_over_pending_writes(
    store: _Store, logs: _Recorder, pending: str
) -> None:
    """A rollback would discard the route's ORM changes and expire its
    objects: the writer refuses instead, with its own log reason."""

    async with store.sessions() as session:
        tenant = await session.get(Tenant, store.tenant)
        assert tenant is not None
        if pending == "new":
            session.add(_tenant_row(uuid4(), None))
        elif pending == "dirty":
            tenant.name = "Renamed by the route"
        else:
            row = await session.get(Session, store.standalone_session)
            assert row is not None
            await session.delete(row)
        assert getattr(session, pending)
        assert await end_route_transaction(session) is False
        # Nothing was rolled back: the change and the transaction are intact.
        assert session.in_transaction() and getattr(session, pending)
        if pending == "dirty":
            assert tenant.name == "Renamed by the route"
    assert logs.reasons() == ["route_pending_writes"]
    [(_message, fields)] = logs.warnings
    assert fields["pending"] == [pending]


# ---------------------------------------------------------------------------
# The real app (SQLite), the switch forced on per test.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class World:
    api_tenant: UUID
    api_key: str
    api_session: UUID
    api_session_b: UUID
    standalone_tenant: UUID
    standalone_key: str
    standalone_session: UUID
    jwt_tenant: UUID
    jwt_session: UUID
    zapi_tenant: UUID
    zapi_token: str
    zapi_instance: str
    meta_tenant: UUID
    meta_phone: str
    meta_secret: str
    uazapi_tenant: UUID
    uazapi_instance: str
    evolution_tenant: UUID
    evolution_instance: str


def _capability_token() -> str:
    import base64

    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


async def _seed(world: World) -> None:
    database = get_db_session()
    try:
        async with database.begin():
            database.add(Builder(id=_BUILDER, name="Metering builder"))
            await database.flush()
            database.add_all(
                [
                    _tenant_row(
                        world.api_tenant,
                        _BUILDER,
                        api_key_hash=pwd_context.hash(world.api_key),
                    ),
                    _tenant_row(
                        world.standalone_tenant,
                        None,
                        api_key_hash=pwd_context.hash(world.standalone_key),
                    ),
                    _tenant_row(world.jwt_tenant, _BUILDER),
                    _tenant_row(world.zapi_tenant, _BUILDER),
                    _tenant_row(world.meta_tenant, _BUILDER),
                    _tenant_row(world.uazapi_tenant, None),
                    _tenant_row(world.evolution_tenant, _BUILDER),
                ]
            )
            await database.flush()
            database.add_all(
                [
                    _session_row(world.api_session, world.api_tenant),
                    _session_row(world.api_session_b, world.api_tenant),
                    _session_row(world.standalone_session, world.standalone_tenant),
                    _session_row(world.jwt_session, world.jwt_tenant),
                    ClinicConfigTable(
                        tenant_id=world.zapi_tenant,
                        lgpd={"lgpd_enabled": True},
                        api_credentials={
                            "whatsapp_provider": "zapi",
                            "zapi_instance_id": world.zapi_instance,
                            "zapi_token": "zapi-outbound-token",
                            "zapi_inbound_token_digest": hashlib.sha256(
                                world.zapi_token.encode("ascii")
                            ).hexdigest(),
                        },
                    ),
                    ClinicConfigTable(
                        tenant_id=world.meta_tenant,
                        api_credentials={
                            "whatsapp_provider": "meta",
                            "meta_phone_number_id": world.meta_phone,
                            "meta_app_secret": world.meta_secret,
                            "meta_permanent_token": "meta-outbound-token",
                        },
                    ),
                    ClinicConfigTable(
                        tenant_id=world.uazapi_tenant,
                        api_credentials={
                            "whatsapp_provider": "uazapi",
                            "uazapi_instance_name": world.uazapi_instance,
                            "uazapi_api_url": "https://uazapi.invalid",
                            "uazapi_token": "uazapi-outbound-token",
                        },
                    ),
                    ClinicConfigTable(
                        tenant_id=world.evolution_tenant,
                        api_credentials={
                            "whatsapp_provider": "evolution",
                            "evolution_instance_name": world.evolution_instance,
                            "evolution_api_url": "https://evolution.invalid",
                            "evolution_api_key": "evolution-outbound-key",
                        },
                    ),
                ]
            )
    finally:
        await database.close()


@pytest.fixture
def world(client: Any) -> World:
    seeded = World(
        api_tenant=uuid4(),
        api_key=secrets.token_urlsafe(24),
        api_session=uuid4(),
        api_session_b=uuid4(),
        standalone_tenant=uuid4(),
        standalone_key=secrets.token_urlsafe(24),
        standalone_session=uuid4(),
        jwt_tenant=uuid4(),
        jwt_session=uuid4(),
        zapi_tenant=uuid4(),
        zapi_token=_capability_token(),
        zapi_instance=f"3E{uuid4().hex[:10]}",
        meta_tenant=uuid4(),
        meta_phone=f"metering-meta-{uuid4().hex[:8]}",
        meta_secret=secrets.token_urlsafe(32),
        uazapi_tenant=uuid4(),
        uazapi_instance=f"metering-uazapi-{uuid4().hex[:8]}",
        evolution_tenant=uuid4(),
        evolution_instance=f"metering-evolution-{uuid4().hex[:8]}",
    )
    client._submit(_seed(seeded))
    return seeded


class _FakeLLM:
    async def generate(
        self, messages: list[dict[str, str]], **_: Any
    ) -> dict[str, Any]:
        return {
            "content": _LLM_REPLY,
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "model": "metering-fake",
        }


@dataclass
class Providers:
    sent: list[tuple[str, tuple[Any, ...]]]
    llm: MagicMock


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch) -> Providers:
    """Deterministic LLM and provider sends; nothing leaves the process."""

    sent: list[tuple[str, tuple[Any, ...]]] = []

    def sender(name: str, result: Any) -> Callable[..., Any]:
        async def send(*args: Any, **_kwargs: Any) -> Any:
            sent.append((name, args))
            return result

        return send

    llm = MagicMock(return_value=_FakeLLM())
    monkeypatch.setattr("src.llm.client.get_tenant_llm_client", llm)
    monkeypatch.setattr(
        "src.llm.client.get_multimodal_text_llm_client", MagicMock(return_value=None)
    )
    for name, result in {
        "send_text_message": {"messageId": "zapi-reply"},
        "send_zapi_audio": {"messageId": "zapi-audio"},
        "send_meta_cloud_text": {"messages": [{"id": "wamid.reply"}]},
        "send_meta_cloud_audio": {"messages": [{"id": "wamid.audio"}]},
        "send_uazapi_text": {"MessageID": "uazapi-reply"},
        "send_uazapi_audio": {"MessageID": "uazapi-audio"},
        "send_evolution_text": {"key": {"id": "evolution-reply"}},
        "send_evolution_audio": {"key": {"id": "evolution-audio"}},
        "mark_message_read": None,
        "mark_conversation_unread": None,
    }.items():
        monkeypatch.setattr(webhook, name, sender(name, result))

    async def no_speech(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("speech synthesis is off in this suite")

    monkeypatch.setattr(webhook, "synthesize_speech", no_speech)
    return Providers(sent=sent, llm=llm)


@pytest.fixture
def force_switch(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    def force(effective: bool) -> None:
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: effective)

    force(True)
    # LOW-2: the app-level tests are not about the 250 ms budget (the budget
    # tests call the writer directly, above); a loaded machine must not turn
    # their writes into timeouts.  The route resolves the module attribute
    # -- the forced plugin's counter included --, so it is wrapped.
    monkeypatch.setattr(
        credit_metering,
        "record_observational_trace",
        functools.partial(credit_metering.record_observational_trace, budget_seconds=5),
    )
    return force


async def _rows(model: type[Any]) -> list[Any]:
    database = get_db_session()
    try:
        return list((await database.execute(select(model))).scalars())
    finally:
        await database.close()


def _claims(client: Any) -> list[Any]:
    return list(client._submit(_rows(CreditIngressClaim)))


def _effects(client: Any) -> list[Any]:
    return list(client._submit(_rows(CreditObservationalEffect)))


def _jwt(tenant_id: UUID, *, role: str = "clinic_admin") -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(tenant_id),
            "role": role,
            "iat": now - timedelta(minutes=1),
            "exp": now + timedelta(hours=1),
        },
        settings.secret_key,
        algorithm="HS256",
    )


def _event_body(
    session_id: UUID,
    content: str = "Olá, quero agendar",
    event_type: str = "message.in",
) -> dict[str, Any]:
    return {
        "session_id": str(session_id),
        "event_type": event_type,
        "payload": {"content": content, "channel": "whatsapp", "user_id": "metering"},
    }


def _api_event(
    client: Any,
    world: World,
    *,
    key: str | None = None,
    standalone: bool = False,
    session: UUID | None = None,
    **body: Any,
) -> Any:
    headers = {"X-API-Key": world.standalone_key if standalone else world.api_key}
    if key is not None:
        headers["Idempotency-Key"] = key
    if session is None:
        session = world.standalone_session if standalone else world.api_session
    return client.post(EVENTS, json=_event_body(session, **body), headers=headers)


def _zapi_body(world: World, message_id: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "instanceId": world.zapi_instance,
        "messageId": message_id,
        "phone": extra.pop("phone", "5511900000101"),
        "fromMe": False,
        "text": {"message": extra.pop("text", "Olá, quero agendar")},
        "senderName": "Paciente",
    }
    body.update(extra)
    return body


def _zapi(client: Any, world: World, body: dict[str, Any]) -> Any:
    return client.post(f"/v1/webhook/zapi/{world.zapi_token}", json=body)


def _meta_body(world: World, message: dict[str, Any]) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "metering-waba",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": world.meta_phone},
                            "contacts": [{"profile": {"name": "Paciente"}}],
                            "messages": [message],
                        },
                    }
                ],
            }
        ],
    }


def _meta_text(message_id: str, text_body: str, phone: str = "5511900000201") -> Any:
    return {
        "from": phone,
        "id": message_id,
        "timestamp": "1711900000",
        "type": "text",
        "text": {"body": text_body},
    }


def _meta(client: Any, world: World, message: dict[str, Any]) -> Any:
    import hmac

    raw = json.dumps(_meta_body(world, message)).encode()
    signature = hmac.new(world.meta_secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        META,
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={signature}",
        },
    )


def _uazapi_body(world: World, message_id: str) -> dict[str, Any]:
    return {
        "instanceName": world.uazapi_instance,
        "message": {
            "messageid": message_id,
            "text": "Olá, quero agendar",
            "fromMe": False,
            "sender_pn": "5511900000301@s.whatsapp.net",
        },
        "chat": {"wa_chatid": "5511900000301@s.whatsapp.net"},
    }


def _evolution_body(world: World, message_id: str) -> dict[str, Any]:
    return {
        "event": "messages.upsert",
        "instance": world.evolution_instance,
        "data": {
            "key": {
                "remoteJid": "5511900000401@s.whatsapp.net",
                "fromMe": False,
                "id": message_id,
            },
            "message": {"conversation": "Olá, quero agendar"},
            "pushName": "Paciente",
        },
    }


def _only_effect(client: Any) -> Any:
    [effect] = _effects(client)
    return effect


def test_a_production_event_with_a_key_is_claimed_and_traced(
    client: Any, world: World, providers: Providers, force_switch: Any
) -> None:
    response = _api_event(client, world, key="order-123")
    assert response.status_code == 200, response.text
    body = response.json()
    event_id = UUID(body["event_id"])
    [claim] = _claims(client)
    assert claim.source == "events" and claim.mode == "observational"
    assert claim.principal_id == f"api_key:{world.api_tenant}"
    assert (claim.idempotency_key, claim.builder_id) == ("order-123", _BUILDER)
    assert claim.interaction_id == event_id
    canonical = CanonicalEventRequest(
        tenant_id=world.api_tenant,
        session_id=world.api_session,
        event_type=EventType.MESSAGE_IN,
        payload=EventRequest.model_validate(_event_body(world.api_session)).payload,
    )
    assert claim.payload_sha256 == event_payload_sha256(canonical)
    assert claim.session_version is not None and claim.session_version >= 1
    effect = _only_effect(client)
    assert effect.effect_id == trace_effect_id(world.api_tenant, event_id)
    assert (effect.origin, effect.authenticated, effect.would_debit_mc) == (
        "production",
        True,
        1_000,
    )
    # M3: never the reserved message.in id, which the response also carries.
    assert body["interaction_event_id"] is not None
    assert effect.trace_id == event_id != UUID(body["interaction_event_id"])
    decided = response_decision(_as_response(body))
    assert decided is not None
    assert effect.decision_digest == decision_digest(
        tenant_id=world.api_tenant,
        interaction_id=event_id,
        decision_version=1,
        origin=InteractionOrigin.PRODUCTION,
        decision=decided[1],
    )


def _as_response(body: dict[str, Any]) -> Any:
    from src.api.schemas.events import EventResponse

    return EventResponse.model_validate(body)


def test_a_retry_with_the_same_key_reuses_the_claim(
    client: Any, world: World, providers: Providers, force_switch: Any
) -> None:
    first = _api_event(client, world, key="order-retry")
    retry = _api_event(client, world, key="order-retry")
    assert first.status_code == retry.status_code == 200
    assert first.json()["event_id"] != retry.json()["event_id"]  # main re-ran
    [claim] = _claims(client)
    effect = _only_effect(client)
    assert claim.interaction_id == effect.trace_id == UUID(first.json()["event_id"])


@pytest.mark.parametrize("key", [None, "", "x" * 256, "with space"])
def test_without_a_valid_key_each_event_is_its_own_trace_and_no_claim(
    client: Any, world: World, providers: Providers, force_switch: Any, key: Any
) -> None:
    answers = [_api_event(client, world, key=key) for _ in range(2)]
    assert [a.status_code for a in answers] == [200, 200]  # main's answer
    assert _claims(client) == []
    assert sorted(e.trace_id for e in _effects(client)) == sorted(
        UUID(a.json()["event_id"]) for a in answers
    )


def test_a_non_message_event_is_anchored_on_its_public_event_id(
    client: Any, world: World, providers: Providers, force_switch: Any
) -> None:
    response = _api_event(client, world, event_type="session.start")
    assert response.status_code == 200, response.text
    assert response.json()["interaction_event_id"] is None
    effect = _only_effect(client)
    assert effect.trace_id == UUID(response.json()["event_id"])
    assert effect.decision_digest is not None and effect.would_debit_mc == 1_000


@pytest.mark.parametrize("key", [None, "panel-key"])
def test_the_panel_jwt_is_a_simulator_trace_without_claim(
    client: Any, world: World, providers: Providers, force_switch: Any, key: Any
) -> None:
    headers = {"Authorization": f"Bearer {_jwt(world.jwt_tenant)}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    response = client.post(EVENTS, json=_event_body(world.jwt_session), headers=headers)
    assert response.status_code == 200, response.text
    assert _claims(client) == []  # H3: no claim for simulator, key or not
    effect = _only_effect(client)
    assert (effect.origin, effect.authenticated, effect.would_debit_mc) == (
        "simulator",
        True,
        0,
    )
    assert effect.trace_id == UUID(response.json()["event_id"])


def test_a_standalone_tenant_is_claimed_and_deduplicated_by_tenant(
    client: Any, world: World, providers: Providers, force_switch: Any
) -> None:
    for _ in range(2):
        response = _api_event(client, world, key="solo-1", standalone=True)
        assert response.status_code == 200, response.text
    [claim] = _claims(client)
    assert (claim.tenant_id, claim.builder_id) == (world.standalone_tenant, None)
    effect = _only_effect(client)
    assert (effect.builder_id, effect.would_debit_mc) == (None, 1_000)


def test_zapi_meta_uazapi_and_evolution_follow_the_origin_matrix(
    client: Any, world: World, providers: Providers, force_switch: Any
) -> None:
    zapi_body = _zapi_body(world, "zapi-matrix")
    meta_message = _meta_text("wamid.matrix", "Olá, quero agendar")
    answers = {
        "zapi": _zapi(client, world, zapi_body),
        "meta": _meta(client, world, meta_message),
        "uazapi": client.post(
            "/v1/webhook/uazapi", json=_uazapi_body(world, "uazapi-matrix")
        ),
        "evolution": client.post(
            "/v1/webhook/evolution", json=_evolution_body(world, "evolution-matrix")
        ),
    }
    for name, answer in answers.items():
        assert answer.status_code == 200, (name, answer.text)
        assert answer.json().get("replied") is True, (name, answer.json())
    claims = {claim.source: claim for claim in _claims(client)}
    effects = {effect.tenant_id: effect for effect in _effects(client)}
    expected = {
        "zapi": (world.zapi_tenant, "zapi-matrix", "production", True, 1_000, _BUILDER),
        "meta": (
            world.meta_tenant,
            "wamid.matrix",
            "production",
            True,
            1_000,
            _BUILDER,
        ),
        "uazapi": (world.uazapi_tenant, "uazapi-matrix", "unknown", False, 0, None),
        "evolution": (
            world.evolution_tenant,
            "evolution-matrix",
            "unknown",
            False,
            0,
            _BUILDER,
        ),
    }
    assert set(claims) == set(expected)
    for source, (
        tenant,
        message,
        origin,
        authenticated,
        cost,
        builder,
    ) in expected.items():
        claim = claims[source]
        assert claim.principal_id == f"{source}:{tenant}"
        assert (claim.tenant_id, claim.provider_message_id) == (tenant, message)
        assert (claim.builder_id, claim.mode) == (builder, "observational")
        effect = effects[tenant]
        assert effect.trace_id == claim.interaction_id
        assert effect.effect_id == trace_effect_id(tenant, claim.interaction_id)
        assert (effect.origin, effect.authenticated, effect.would_debit_mc) == (
            origin,
            authenticated,
            cost,
        ), source
    assert claims["zapi"].payload_sha256 == zapi_payload_sha256(zapi_body)
    assert claims["meta"].payload_sha256 == provider_message_sha256(meta_message)


def test_a_webhook_without_a_deliverable_answer_would_cost_nothing(
    client: Any, world: World, providers: Providers, force_switch: Any
) -> None:
    providers.llm.return_value = None  # the builder has no LLM configured
    response = _zapi(client, world, _zapi_body(world, "zapi-no-llm"))
    assert response.json() == {
        "ok": True,
        "replied": False,
        "reason": "no_llm_response",
    }
    effect = _only_effect(client)
    assert (effect.origin, effect.authenticated, effect.would_debit_mc) == (
        "production",
        True,
        0,
    )


def test_a_failed_delivery_keeps_the_one_trace_of_the_decision(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decision is recorded before the reply is sent; a provider failure
    afterwards changes neither the effect nor its cost."""

    async def down(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("provider down")

    monkeypatch.setattr(webhook, "send_text_message", down)
    response = _zapi(client, world, _zapi_body(world, "zapi-send-fails"))
    assert response.json()["reason"] == "send_failed"
    [claim] = _claims(client)
    effect = _only_effect(client)
    assert effect.trace_id == claim.interaction_id
    assert (effect.origin, effect.would_debit_mc) == ("production", 1_000)


def test_a_redelivery_the_replay_store_forgot_reuses_the_claim(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The replay store forgot the id (TTL, Redis down): main processes twice.
    monkeypatch.setattr(webhook, "_claim_incoming_message", lambda *_args: True)
    body = _zapi_body(world, "zapi-redelivered")
    assert _zapi(client, world, body).json().get("replied") is True
    assert _zapi(client, world, body).json().get("replied") is True  # main re-ran
    [claim] = _claims(client)
    effect = _only_effect(client)
    assert effect.trace_id == claim.interaction_id


@pytest.fixture
def file_handoffs(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The Meta file handoff, faked at its module slot (it returns an id)."""

    calls: list[dict[str, Any]] = []

    async def initiate(
        tenant_id: UUID, session_id: UUID, from_phone: str, note: str, *, context: Any
    ) -> Any:
        handoff = SimpleNamespace(id=uuid4())
        calls.append({"note": note, "context": context, "id": handoff.id})
        return handoff

    monkeypatch.setattr(webhook, "_meta_initiate_file_handoff", initiate)
    return calls


def test_both_meta_file_handoff_call_sites_record_a_production_trace(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    file_handoffs: list[dict[str, Any]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone = "5511900000222"
    document = {
        "from": phone,
        "id": "wamid.document",
        "timestamp": "1711900000",
        "type": "document",
        "document": {"id": "media-doc", "mime_type": "application/pdf"},
    }
    ack = _meta(client, world, document)
    assert ack.json()["action"] == "file_ack"
    assert _effects(client) == [] and _claims(client) == []  # the ack is free
    forward = _meta(
        client, world, _meta_text("wamid.forward", "Pode encaminhar", phone)
    )
    assert forward.json()["action"] == "file_handoff"

    async def resolve(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"url": "https://media.invalid/a", "file_size": 10_000_000}

    monkeypatch.setattr(webhook, "resolve_meta_media", resolve)
    audio = {
        "from": "5511900000333",
        "id": "wamid.long-audio",
        "timestamp": "1711900000",
        "type": "audio",
        "audio": {"id": "media-audio", "mime_type": "audio/ogg"},
    }
    long_audio = _meta(client, world, audio)
    assert long_audio.json() == {"ok": True, "skipped": "audio_too_long"}

    notes = [call["note"] for call in file_handoffs]
    assert notes == ["file_pending_review", "audio_too_long"]
    claims = {claim.provider_message_id: claim for claim in _claims(client)}
    assert set(claims) == {"wamid.forward", "wamid.long-audio"}
    effects = {effect.trace_id: effect for effect in _effects(client)}
    for call, message_id in zip(file_handoffs, ("wamid.forward", "wamid.long-audio")):
        claim = claims[message_id]
        assert claim.interaction_id == call["id"]  # anchored on the handoff
        assert call["context"].origin is InteractionOrigin.PRODUCTION
        effect = effects[call["id"]]
        assert (effect.origin, effect.authenticated, effect.would_debit_mc) == (
            "production",
            True,
            1_000,
        )


async def _set_bot_inactive(tenant_id: UUID) -> None:
    database = get_db_session()
    try:
        async with database.begin():
            tenant = await database.get(Tenant, tenant_id)
            assert tenant is not None
            tenant.bot_active = False
    finally:
        await database.close()


async def _pause(tenant_id: UUID, phone: str) -> None:
    database = get_db_session()
    try:
        async with database.begin():
            database.add(
                _session_row(
                    uuid4(),
                    tenant_id,
                    channel="whatsapp",
                    user_id=phone,
                    is_paused=True,
                    paused_until=datetime.now(timezone.utc) + timedelta(hours=2),
                )
            )
    finally:
        await database.close()


def _audio_body(world: World, message_id: str, seconds: int) -> dict[str, Any]:
    return {
        "instanceId": world.zapi_instance,
        "messageId": message_id,
        "phone": "5511900000555",
        "fromMe": False,
        "audio": {"audioUrl": "https://media.invalid/a.ogg", "seconds": seconds},
    }


async def _set_credentials(tenant_id: UUID, **values: Any) -> None:
    database = get_db_session()
    try:
        async with database.begin():
            row = (
                await database.execute(
                    select(ClinicConfigTable).where(
                        ClinicConfigTable.tenant_id == tenant_id
                    )
                )
            ).scalar_one()
            row.api_credentials = {**row.api_credentials, **values}
    finally:
        await database.close()


_NOT_ALLOWLISTED: dict[str, Callable[[Any, World], Any]] = {
    "zapi_legacy_route": lambda c, w: c.post(
        "/v1/webhook/zapi", json=_zapi_body(w, "zapi-legacy")
    ),
    "zapi_rejected_token": lambda c, w: c.post(
        f"/v1/webhook/zapi/{_capability_token()}", json=_zapi_body(w, "zapi-bad")
    ),
    "zapi_bot_inactive": lambda c, w: (
        c._submit(_set_bot_inactive(w.zapi_tenant)),
        _zapi(c, w, _zapi_body(w, "zapi-bot-off")),
    )[1],
    "zapi_paused_session": lambda c, w: (
        c._submit(_pause(w.zapi_tenant, "5511900000444")),
        _zapi(c, w, _zapi_body(w, "zapi-paused", phone="5511900000444")),
    )[1],
    "zapi_audio_too_long": lambda c, w: _zapi(
        c, w, _audio_body(w, "zapi-long-audio", seconds=600)
    ),
    "zapi_stt_unsupported_provider": lambda c, w: (
        c._submit(_set_credentials(w.zapi_tenant, stt_provider="google_stt")),
        _zapi(c, w, _audio_body(w, "zapi-stt-skip", seconds=5)),
    )[1],
    "zapi_stt_reroute": lambda c, w: _zapi(
        c, w, _audio_body(w, "zapi-stt-reroute", seconds=5)
    ),
    "meta_file_ack": lambda c, w: _meta(
        c,
        w,
        {
            "from": "5511900000666",
            "id": "wamid.image",
            "timestamp": "1711900000",
            "type": "image",
            "image": {"id": "media-image", "mime_type": "image/jpeg"},
        },
    ),
    "events_rejected_session": lambda c, w: c.post(
        EVENTS, json=_event_body(w.jwt_session), headers={"X-API-Key": w.api_key}
    ),
}


@pytest.mark.parametrize("case", sorted(_NOT_ALLOWLISTED))
def test_paths_outside_the_allowlist_write_no_claim_and_no_effect(
    client: Any, world: World, providers: Providers, force_switch: Any, case: str
) -> None:
    response = _NOT_ALLOWLISTED[case](client, world)
    assert response.status_code in {200, 202, 401, 403}, response.text
    assert _claims(client) == [] and _effects(client) == []


def test_a_duplicate_delivery_is_not_traced_twice(
    client: Any, world: World, providers: Providers, force_switch: Any
) -> None:
    body = _zapi_body(world, "zapi-duplicate")
    assert _zapi(client, world, body).json().get("replied") is True
    assert _zapi(client, world, body).json() == {
        "ok": True,
        "skipped": "duplicate_message",
    }
    assert len(_claims(client)) == 1 and len(_effects(client)) == 1


def test_the_lgpd_notice_is_not_a_decision(
    client: Any, world: World, providers: Providers, force_switch: Any
) -> None:
    """A new session gets the privacy notice and the reply: one trace only."""

    response = _zapi(client, world, _zapi_body(world, "zapi-first", phone="5511977"))
    assert response.json().get("replied") is True
    texts = [args[3] for name, args in providers.sent if name == "send_text_message"]
    assert len(texts) == 2 and texts[1] == _LLM_REPLY  # notice, then the reply
    assert len(_effects(client)) == 1 and len(_claims(client)) == 1


def test_event_service_process_event_is_never_metered(
    client: Any, world: World, providers: Providers, force_switch: Any
) -> None:
    from src.services.event_service import get_event_service

    request = CanonicalEventRequest(
        tenant_id=world.api_tenant,
        session_id=world.api_session,
        payload=EventPayload(content="Olá", channel="whatsapp"),
    )
    client._submit(get_event_service().process_event(request))
    assert _claims(client) == [] and _effects(client) == []


async def _seed_unknown_history(tenant_id: UUID, session_id: UUID) -> None:
    database = get_db_session()
    try:
        async with database.begin():
            for event_type in ("message.in", "srm.decision", "handoff.accept"):
                database.add(
                    EventRow(
                        id=uuid4(),
                        tenant_id=tenant_id,
                        session_id=session_id,
                        event_type=event_type,
                        interaction_origin="unknown",
                        event_data={"content": "pre-deploy"},
                        event_metadata={},
                    )
                )
    finally:
        await database.close()


def test_the_origin_comes_from_the_live_context_only(
    client: Any, world: World, providers: Providers, force_switch: Any
) -> None:
    """Pre-deploy ``unknown`` rows and late ``unknown`` handoff events of the
    same session never reach the recorded origin; the writer reads no
    ``events`` row at all."""

    client._submit(_seed_unknown_history(world.api_tenant, world.api_session))
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    original = credit_metering._write

    async def observed(*args: Any, **kwargs: Any) -> Any:
        engine = get_db_engine().sync_engine
        sa_event.listen(engine, "before_cursor_execute", record)
        try:
            return await original(*args, **kwargs)
        finally:
            sa_event.remove(engine, "before_cursor_execute", record)

    patch = pytest.MonkeyPatch()
    patch.setattr(credit_metering, "_write", observed)
    try:
        response = _api_event(client, world, key="live-origin")
    finally:
        patch.undo()
    assert response.status_code == 200
    effect = _only_effect(client)
    assert effect.origin == "production"
    assert statements, "the writer ran no SQL"
    assert not [s for s in statements if " events" in s.lower()]


def _masked(value: Any) -> Any:
    """Generated values never decide equality: ids, instants (the dominance
    oracle's mask, with every UUID treated as generated)."""

    from tests.integration.test_credit_schema_dominance_postgres import _mask

    masked = frozenset(
        {
            "event_id",
            "interaction_event_id",
            "timestamp",
            "processed_at",
            "processing_time_ms",
            "stage_timings",
            "created_at",
        }
    )
    return _mask(value, masked, set())


async def _event_rows(session_id: UUID) -> list[str]:
    database = get_db_session()
    try:
        rows = (
            await database.execute(
                select(EventRow).where(EventRow.session_id == session_id)
            )
        ).scalars()
        return sorted(
            json.dumps(
                _masked(
                    {
                        "event_type": row.event_type,
                        "interaction_origin": row.interaction_origin,
                        "event_data": row.event_data,
                        "event_metadata": row.event_metadata,
                    }
                ),
                sort_keys=True,
                default=str,
            )
            for row in rows
        )
    finally:
        await database.close()


@pytest.mark.parametrize("fault", ["claim", "effect", "pool"])
def test_a_credit_side_failure_leaves_answer_and_events_as_main(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    """Twin sessions of one tenant: switch off on one, switch on with a fault
    injected on the credit side on the other."""

    force_switch(False)
    off = _api_event(client, world, key="fault-key", session=world.api_session)

    async def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise IntegrityError("INSERT", {}, Exception(f"injected {fault}"))

    if fault == "claim":
        monkeypatch.setattr(CreditRepository, "insert_ingress_claim", boom)
    elif fault == "effect":
        monkeypatch.setattr(CreditRepository, "insert_observational_effect", boom)
    else:

        def no_pool() -> Any:
            raise RuntimeError("no free connection")

        monkeypatch.setattr(credit_metering, "_session_factory", no_pool)
    recorder = _Recorder()
    monkeypatch.setattr(credit_metering, "logger", recorder)
    force_switch(True)
    on = _api_event(client, world, key="fault-key", session=world.api_session_b)

    assert on.status_code == off.status_code == 200
    assert _masked(on.json()) == _masked(off.json())
    on_events = client._submit(_event_rows(world.api_session_b))
    assert on_events and on_events == client._submit(_event_rows(world.api_session))
    assert _claims(client) == [] and _effects(client) == []
    assert recorder.reasons() == ["failed"]


def test_with_the_switch_off_no_sql_touches_a_credit_table(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Callable[[bool], None],
) -> None:
    force_switch(False)
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    engine = get_db_engine().sync_engine
    sa_event.listen(engine, "before_cursor_execute", record)
    try:
        assert _api_event(client, world, key="off").status_code == 200
        assert _zapi(client, world, _zapi_body(world, "zapi-off")).status_code == 200
        assert _meta(client, world, _meta_text("wamid.off", "Olá")).status_code == 200
    finally:
        sa_event.remove(engine, "before_cursor_execute", record)
    assert statements
    assert [s for s in statements if "credit_" in s.lower()] == []


# ---------------------------------------------------------------------------
# The writer is called from the governed decision paths only.
# ---------------------------------------------------------------------------


def _calls(module: Any) -> dict[str, list[ast.Call]]:
    tree = ast.parse(inspect.getsource(module))
    calls: dict[str, list[ast.Call]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            calls.setdefault(name, []).append(node)
    return calls


def _enclosing(module: Any, call: ast.Call) -> str:
    tree = ast.parse(inspect.getsource(module))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(
                isinstance(child, ast.Call) and child.lineno == call.lineno
                for child in ast.walk(node)
            ):
                innermost = node.name
    return innermost


def test_the_writer_runs_only_at_the_governed_decision_points() -> None:
    event_calls = _calls(events_routes)
    assert [
        _enclosing(events_routes, c) for c in event_calls["record_events_decision"]
    ] == ["process_event"]
    webhook_calls = _calls(webhook)
    [pipeline] = webhook_calls["record_webhook_decision"]
    assert _enclosing(webhook, pipeline) == "_run_pipeline_transactionally"
    handoff_sites = webhook_calls["record_meta_file_handoff"]
    assert len(handoff_sites) == len(webhook_calls["_meta_initiate_file_handoff"]) == 2
    assert {_enclosing(webhook, c) for c in handoff_sites} == {"meta_webhook"}
    # Every pipeline call site hands over its ingress (built only when on).
    pipeline_calls = webhook_calls["_run_pipeline_transactionally"]
    assert len(pipeline_calls) == 4
    assert all(
        "metering" in {keyword.arg for keyword in call.keywords}
        for call in pipeline_calls
    )
    for writer in ("record_observational_trace", "_write"):
        assert writer not in webhook_calls and writer not in event_calls


def test_unknown_contexts_build_an_unknown_trace() -> None:
    """The matrix's ``unknown`` row: a caller without proof (no identity)."""

    tenant, session = uuid4(), uuid4()
    context = unknown_interaction_context(
        effective_tenant_id=tenant, session_id=session, event_type="message.in"
    )
    production = build_interaction_context(
        effective_tenant_id=tenant,
        session_id=session,
        origin=InteractionOrigin.PRODUCTION,
        event_type="message.in",
    )
    request = SimpleNamespace(scope={"state": {"credit_switch_effective": True}})
    built = credit_metering.uazapi_ingress(
        request,  # type: ignore[arg-type]
        message={"messageid": "m"},
        context=context,
    )
    assert built is not None and built.origin is InteractionOrigin.UNKNOWN
    off = SimpleNamespace(scope={"state": {"credit_switch_effective": False}})
    assert (
        credit_metering.zapi_ingress(
            off, body={"messageId": "m"}, context=production  # type: ignore[arg-type]
        )
        is None
    )


@pytest.fixture(autouse=True)
def _no_writer_leak(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Guard: the module slots this suite swaps come back as they were (the
    test's monkeypatch is undone first; nothing may stay swapped beyond it)."""

    before = (
        credit_metering._write,
        credit_metering.logger,
        credit_metering._session_factory,
        webhook._meta_initiate_file_handoff,
    )
    yield
    monkeypatch.undo()
    after = (
        credit_metering._write,
        credit_metering.logger,
        credit_metering._session_factory,
        webhook._meta_initiate_file_handoff,
    )
    assert after == before


# ---------------------------------------------------------------------------
# Group 3b-1: the enforced path's pure rules (the route and the fences run in
# tests/api/test_events_credit_enforced.py and, under real concurrency, in
# tests/integration/test_credit_metering_postgres.py).
# ---------------------------------------------------------------------------


def _claim_row(
    *, started: datetime, session_version: int | None = None, mode: str = "enforced"
) -> Any:
    from src.repositories.credit import ClaimRow

    return ClaimRow(
        id=uuid4(),
        tenant_id=uuid4(),
        builder_id=uuid4(),
        principal_id="api_key:t",
        interaction_id=uuid4(),
        payload_sha256="a" * 64,
        decision_version=1,
        session_version=session_version,
        mode=mode,
        created_at=started,
    )


def _outbox(payload: dict[str, Any], created_at: datetime) -> Any:
    from src.repositories.credit import ResponseOutboxRow

    return ResponseOutboxRow(
        id=uuid4(), payload=payload, state="delivered", created_at=created_at
    )


_NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)
_LEASE = timedelta(seconds=credit_metering.CLAIM_LEASE_SECONDS)


@pytest.mark.parametrize(
    ("started", "session_version", "may_take_over", "expected"),
    [
        # Claim without phase 1, within the lease: in progress, Retry-After 5.
        (_NOW - timedelta(seconds=10), None, True, (409, "event_in_progress", 5)),
        # ... with 2.2 s left: Retry-After 3 (whole seconds, rounded up).
        (
            _NOW - _LEASE + timedelta(seconds=2.2),
            None,
            True,
            (409, "event_in_progress", 3),
        ),
        # Phase 1 committed, within the lease: in progress too.
        (_NOW - timedelta(seconds=10), 4, True, (409, "event_in_progress", 5)),
        # Phase 1 committed, lease over: FINAL event_failed, no Retry-After.
        (_NOW - _LEASE - timedelta(seconds=1), 4, True, (409, "event_failed", None)),
        # A fence loser never takes over: in progress, retry at once.
        (
            _NOW - _LEASE - timedelta(seconds=1),
            None,
            False,
            (409, "event_in_progress", 1),
        ),
    ],
)
def test_the_claim_state_table_answers_each_window(
    started: datetime,
    session_version: int | None,
    may_take_over: bool,
    expected: tuple[int, str, int | None],
) -> None:
    claim = _claim_row(started=started, session_version=session_version)
    answer = credit_metering._claim_state_answer(
        claim, None, _NOW, may_take_over=may_take_over
    )
    assert isinstance(answer, credit_metering.ClaimAnswer)
    status, code, retry_after = expected
    assert (answer.status, answer.body["code"], answer.retry_after) == expected
    if code == "event_failed":
        assert answer.body == {
            "code": code,
            "interaction_id": str(claim.interaction_id),
        }
    else:
        assert answer.body == {"code": code}


def test_an_abandoned_claim_before_phase1_is_takeover_material() -> None:
    claim = _claim_row(started=_NOW - _LEASE)
    assert (
        credit_metering._claim_state_answer(claim, None, _NOW, may_take_over=True)
        is claim
    )


def test_a_decided_claim_replays_within_the_retention_then_is_a_duplicate() -> None:
    claim = _claim_row(started=_NOW - timedelta(days=3), session_version=1)
    retention = timedelta(hours=settings.credit_replay_retention_hours)
    stored = {"event_id": str(uuid4()), "interaction_event_id": str(uuid4())}
    fresh = credit_metering._claim_state_answer(
        claim,
        _outbox(stored, _NOW - retention + timedelta(seconds=1)),
        _NOW,
        may_take_over=False,
    )
    assert (fresh.status, fresh.body, fresh.replay) == (200, stored, True)
    duplicate = {"code": "duplicate_event", "interaction_id": str(claim.interaction_id)}
    for outbox in (_outbox(stored, _NOW - retention), _outbox({}, _NOW)):
        late = credit_metering._claim_state_answer(
            claim, outbox, _NOW, may_take_over=True
        )
        assert (late.status, late.body, late.replay) == (409, duplicate, False)


def test_retry_after_is_whole_seconds_between_1_and_5() -> None:
    assert credit_metering.RETRY_AFTER_MAX_SECONDS == 5
    for remaining, expected in ((0.01, 1), (1.0, 1), (1.2, 2), (4.9, 5), (1e6, 5)):
        assert credit_metering.retry_after_seconds(remaining) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, (None, "idempotency_key_required")),
        ("", (None, "idempotency_key_invalid")),
        ("a b", (None, "idempotency_key_invalid")),
        ("ç", (None, "idempotency_key_invalid")),
        ("k" * 256, (None, "idempotency_key_invalid")),
        ("k" * 255, ("k" * 255, None)),
        ("order-1:~!", ("order-1:~!", None)),
    ],
)
def test_the_idempotency_key_rule(
    raw: str | None, expected: tuple[str | None, str | None]
) -> None:
    assert credit_metering._validated_key(raw) == expected


def test_the_enforced_constants_are_pinned() -> None:
    assert credit_metering.CLAIM_LEASE_SECONDS == 1680
    assert credit_metering.claim_lease() == timedelta(minutes=28)
    assert credit_metering.PHASE1_LOCK_TIMEOUT_SECONDS == 15
    assert credit_metering.ENFORCED_DECISION_VERSION == 1
    assert credit_metering.CREDIT_PENDING_EFFECT_KIND == "trace_credit_pending"
    assert credit_metering.NO_RESPONSE_EFFECT_KIND == "trace_no_response"
    assert credit_metering.PAYLOAD_CONFLICT_LOG == "credit_payload_conflict"
    assert credit_metering.replay_retention() == timedelta(
        hours=settings.credit_replay_retention_hours
    )


def test_the_stored_answer_carries_the_committed_interaction_event_id() -> None:
    from src.api.schemas.events import EventResponse

    response = EventResponse.model_validate(
        {
            "event_id": str(uuid4()),
            "decision": {
                "intent": "agendar",
                "intent_category": "scheduling",
                "funnel_stage": "lead_novo",
                "action": "responder",
                "confidence": 0.9,
                "safety_concern": False,
                "needs_rag": False,
                "handoff_required": False,
            },
            "state_patch": {},
            "prompt": {"system": "s", "instructions": "i"},
            "context": {},
            "mode": "normal",
            "processed_at": _NOW.isoformat(),
            "processing_time_ms": 1,
        }
    )
    interaction = uuid4()
    payload = credit_metering.response_payload(response, interaction)
    assert payload["interaction_event_id"] == str(interaction)
    assert response.interaction_event_id is None  # the response is not mutated
    assert payload == json.loads(
        response.model_copy(
            update={"interaction_event_id": interaction}
        ).model_dump_json()
    )


def _scope(effective: bool, identity: Any = None) -> Any:
    state: dict[str, Any] = {"credit_switch_effective": effective}
    if identity is not None:
        state["credit_identity"] = identity
    return SimpleNamespace(scope={"state": state}, headers={})


@pytest.mark.parametrize(
    ("effective", "origin", "kind"),
    [
        (False, InteractionOrigin.PRODUCTION, "api_key"),  # the switch off
        (True, InteractionOrigin.SIMULATOR, "jwt"),  # the panel
        (True, InteractionOrigin.UNKNOWN, None),  # no proof
    ],
)
async def test_only_a_production_request_with_the_switch_on_reads_the_enforcement(
    effective: bool, origin: InteractionOrigin, kind: str | None
) -> None:
    tenant, session = uuid4(), uuid4()
    context = build_interaction_context(
        effective_tenant_id=tenant,
        session_id=session,
        origin=origin,
        event_type="message.in",
    )
    identity = None
    if kind == "api_key":
        identity = credit_identity("api_key", tenant)
    elif kind == "jwt":
        identity = credit_identity("jwt", tenant, uuid4())
    db = MagicMock(spec=AsyncSession)  # any statement would fail the test
    db.execute.side_effect = AssertionError("credit_enforcement_at was read")
    request = EventRequest.model_validate(
        {
            "session_id": str(session),
            "event_type": "message.in",
            "payload": {"content": "Olá", "channel": "whatsapp"},
        }
    )
    canonical = CanonicalEventRequest(
        tenant_id=tenant,
        session_id=session,
        event_type=request.event_type,
        payload=request.payload,
    )
    verdict = await credit_metering.capture_events_enforcement(
        _scope(effective, identity),  # type: ignore[arg-type]
        db,
        tenant=SimpleNamespace(authentication_mode="none"),
        canonical_request=canonical,
        context=context,
    )
    assert verdict is None
    db.execute.assert_not_called()
