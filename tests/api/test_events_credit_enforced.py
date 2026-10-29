"""Enforced ``POST /v1/events`` (neurostack-credit-system Group 3b-1), SQLite.

The real application (lifespan, middleware, SQLite ``:memory:``) with the
credit switch forced effective and ``credit_enforcement_at`` set; only the
LLM is faked.  What is pinned here:

* the order of answers, precedence by precedence (MEDIUM-1): 403
  ``caller_role_forbidden`` -> 422 reserved field -> 400 ``Idempotency-Key``
  -> 403 session ownership (the rate-limit slot comes back) -> the claim's
  table (200 replay, 409) -> 402 ``credit_pending`` (the slot stays
  consumed) -> claim -> phase 1; a request refused before the claim insert
  writes no account, claim or effect;
* the claim's state table (item 4-vi), one test per row, with the capped
  ``Retry-After``; ``payload_conflict`` before any pipeline, lock or effect;
* one decision = one trace (debit, then debt beyond the capacity; a builder
  with no lot is served as debt; no LLM configured still costs a trace),
  atomic with the bundle and the stored answer, replayed identically within
  ``credit_replay_retention_hours`` and ``duplicate_event`` afterwards;
* the effects of zero (``trace_credit_pending``) for a late hold, the phase 1
  handoff charge (``credit_effect_id`` in the handoff context and in the
  ``srm.decision`` row, never exposed by ``GET /handoffs/{id}``), the
  post-final-transaction webhook dispatch;
* fault injection at every write of the pre-check, the claim, phase 1 and
  the final transaction leaves no partial write, and a known failure
  releases the lease;
* ``simulator`` and ``unknown`` are never enforced; with the switch off or
  before the enforcement no enforced sibling ever runs.

Fences under real concurrency (stalled phase 1 / final transaction, double
takeover, clinic delete) are PostgreSQL-only:
``tests/integration/test_credit_metering_postgres.py``.
"""

from __future__ import annotations

import asyncio
import functools
import secrets
from collections.abc import Callable, Iterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import jwt
import pytest
from sqlalchemy import select, update

import src.models  # noqa: F401  (registers every mapper for create_all)
from src.api.middleware.auth import pwd_context
from src.api.middleware.rate_limit import rate_limit_store
from src.api.routes import events as events_routes
from src.config.settings import Settings, settings
from src.core.pipeline import CognitivePipeline
from src.interaction_context import InteractionOrigin
from src.memory.event_store import EventStore
from src.models.builder import Builder
from src.models.credit import (
    CreditAccount,
    CreditDebt,
    CreditEffect,
    CreditEnforcementState,
    CreditIngressClaim,
    CreditLedgerEntry,
    CreditObservationalEffect,
    CreditResponseOutbox,
)
from src.models.event import Event as EventRow
from src.models.handoff import HandoffRecord
from src.models.session import Session
from src.models.tenant import Tenant
from src.repositories.credit import CreditRepository
from src.repositories.database import get_db_session
from src.services import credit_metering
from src.services import handoff_service as handoff_service_module
from src.services.credit_ledger import (
    CreditLedger,
    GrantSpec,
    topup_grant_effect_id,
    topup_lot_expires_at,
)
from src.services.credit_metering import (
    CLAIM_LEASE_SECONDS,
    CREDIT_PENDING_EFFECT_KIND,
    TRACE_COST_MC,
    event_payload_sha256,
    trace_effect_id,
)
from src.services.credit_switch import CreditSwitch
from src.services.event_service import EventService

EVENTS = "/v1/events"
_LLM_REPLY = "Olá! Posso ajudar com o seu agendamento."
_HANDOFF_TEXT = "Quero falar com um atendente humano"
ROLE_FORBIDDEN = {"detail": {"code": "caller_role_forbidden"}}
ORIGIN_RESERVED = {"detail": {"code": "interaction_origin_server_managed"}}
TENANT_MISMATCH = {
    "error": "tenant_identity_mismatch",
    "message": "Tenant identity does not match the authenticated request",
}


# ---------------------------------------------------------------------------
# The seeded world and the fakes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class World:
    builder: UUID
    api_tenant: UUID
    api_key: str
    api_session: UUID
    standalone_tenant: UUID
    standalone_key: str
    standalone_session: UUID
    other_tenant: UUID
    other_session: UUID
    jwt_tenant: UUID
    jwt_session: UUID


def _tenant_row(tenant_id: UUID, builder_id: UUID | None, **extra: Any) -> Tenant:
    return Tenant(
        id=tenant_id,
        name=f"Enforced {tenant_id.hex[:8]}",
        slug=f"enforced-{tenant_id.hex}",
        api_key_hash=extra.pop("api_key_hash", f"unused-{tenant_id.hex}"),
        builder_id=builder_id,
        config={},
        **extra,
    )


def _session_row(session_id: UUID, tenant_id: UUID) -> Session:
    return Session(
        id=session_id,
        tenant_id=tenant_id,
        channel="test",
        cognitive_state={},
        session_metadata={},
        message_count=0,
    )


async def _seed(world: World) -> None:
    database = get_db_session()
    try:
        async with database.begin():
            database.add(Builder(id=world.builder, name="Enforced builder"))
            await database.flush()
            database.add_all(
                [
                    _tenant_row(
                        world.api_tenant,
                        world.builder,
                        api_key_hash=pwd_context.hash(world.api_key),
                    ),
                    _tenant_row(
                        world.standalone_tenant,
                        None,
                        api_key_hash=pwd_context.hash(world.standalone_key),
                    ),
                    _tenant_row(world.other_tenant, world.builder),
                    _tenant_row(world.jwt_tenant, world.builder),
                ]
            )
            await database.flush()
            database.add_all(
                [
                    _session_row(world.api_session, world.api_tenant),
                    _session_row(world.standalone_session, world.standalone_tenant),
                    _session_row(world.other_session, world.other_tenant),
                    _session_row(world.jwt_session, world.jwt_tenant),
                ]
            )
    finally:
        await database.close()


async def _enforce() -> None:
    now = datetime.now(timezone.utc) - timedelta(minutes=1)
    database = get_db_session()
    try:
        async with database.begin():
            database.add(
                CreditEnforcementState(
                    id=1, credit_enforcement_at=now, set_by="test", set_at=now
                )
            )
    finally:
        await database.close()


@pytest.fixture
def world(client: Any) -> Iterator[World]:
    seeded = World(
        builder=uuid4(),
        api_tenant=uuid4(),
        api_key=secrets.token_urlsafe(24),
        api_session=uuid4(),
        standalone_tenant=uuid4(),
        standalone_key=secrets.token_urlsafe(24),
        standalone_session=uuid4(),
        other_tenant=uuid4(),
        other_session=uuid4(),
        jwt_tenant=uuid4(),
        jwt_session=uuid4(),
    )
    client._submit(_seed(seeded))
    yield seeded
    for tenant in (seeded.api_tenant, seeded.standalone_tenant, seeded.jwt_tenant):
        rate_limit_store.clear_tenant(str(tenant))


@pytest.fixture
def enforced(client: Any, world: World) -> World:
    client._submit(_enforce())
    return world


class _FakeLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(
        self, messages: list[dict[str, str]], **_: Any
    ) -> dict[str, Any]:
        self.calls += 1
        return {
            "content": _LLM_REPLY,
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "model": "enforced-fake",
        }


@pytest.fixture
def llm(monkeypatch: pytest.MonkeyPatch) -> _FakeLLM:
    fake = _FakeLLM()
    monkeypatch.setattr(
        "src.llm.client.get_tenant_llm_client", MagicMock(return_value=fake)
    )
    monkeypatch.setattr(
        "src.llm.client.get_multimodal_text_llm_client", MagicMock(return_value=None)
    )
    return fake


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    def force(effective: bool) -> None:
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: effective)

    force(True)
    # LOW-2: none of these tests is about the Group 3a writer's 250 ms
    # budget; a loaded machine must not turn its write into a timeout.  The
    # route resolves the module attribute (the forced plugin's counter
    # included), so it is wrapped with a generous budget.
    monkeypatch.setattr(
        credit_metering,
        "record_observational_trace",
        functools.partial(credit_metering.record_observational_trace, budget_seconds=5),
    )
    return force


def _body(session_id: UUID, content: str = "Olá, quero agendar", **extra: Any) -> Any:
    return {
        "session_id": str(session_id),
        "event_type": extra.pop("event_type", "message.in"),
        "payload": {"content": content, "channel": "whatsapp", "user_id": "enf"},
        **extra,
    }


def _post(
    client: Any,
    world: World,
    *,
    key: str | None = "order-1",
    content: str = "Olá, quero agendar",
    session: UUID | None = None,
    standalone: bool = False,
    body: dict[str, Any] | None = None,
) -> Any:
    headers = {"X-API-Key": world.standalone_key if standalone else world.api_key}
    if key is not None:
        headers["Idempotency-Key"] = key
    if session is None:
        session = world.standalone_session if standalone else world.api_session
    return client.post(EVENTS, json=body or _body(session, content), headers=headers)


def _jwt(tenant_id: UUID, role: str = "clinic_admin") -> dict[str, str]:
    now = datetime.now(timezone.utc)
    token = jwt.encode(
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
    return {"Authorization": f"Bearer {token}"}


async def _all(model: type[Any]) -> list[Any]:
    database = get_db_session()
    try:
        return list((await database.execute(select(model))).scalars())
    finally:
        await database.close()


def _rows(client: Any, model: type[Any]) -> list[Any]:
    return list(client._submit(_all(model)))


def _credit_rows(client: Any) -> dict[str, int]:
    return {
        model.__tablename__: len(_rows(client, model))
        for model in (
            CreditAccount,
            CreditIngressClaim,
            CreditEffect,
            CreditLedgerEntry,
            CreditResponseOutbox,
            CreditObservationalEffect,
        )
    }


_NOTHING: dict[str, int] = {
    "credit_accounts": 0,
    "credit_ingress_claims": 0,
    "credit_effects": 0,
    "credit_ledger_entries": 0,
    "credit_response_outbox": 0,
    "credit_observational_effects": 0,
}


def _tokens(tenant_id: UUID) -> float:
    return float(rate_limit_store.get_or_create_bucket(str(tenant_id)).tokens)


def _forbid_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the pipeline ran")

    # Never the shared singleton's instance.  The class-level patches can be
    # shadowed by an earlier suite that patched the singleton's instance
    # (pytest's undo leaves the bound original there), so the enforced
    # phase 1 itself -- a module attribute the route resolves per call -- is
    # forbidden too.
    monkeypatch.setattr(CognitivePipeline, "process", fail)
    monkeypatch.setattr(EventService, "run_pipeline", fail)
    monkeypatch.setattr(credit_metering, "run_enforced_phase1", fail)


def _sum_entries(client: Any, entry_type: str) -> int:
    return sum(
        int(e.amount_mc)
        for e in _rows(client, CreditLedgerEntry)
        if e.entry_type == entry_type
    )


# ---------------------------------------------------------------------------
# The order of answers, precedence by precedence (MEDIUM-1).
# ---------------------------------------------------------------------------


def test_a_forbidden_role_is_403_before_the_reserved_field_and_writes_nothing(
    client: Any, enforced: World, switch: Any, llm: _FakeLLM
) -> None:
    body = _body(enforced.jwt_session)
    body["interaction_origin"] = "production"
    response = client.post(
        EVENTS, json=body, headers=_jwt(enforced.jwt_tenant, role="investor")
    )
    assert response.status_code == 403 and response.json() == ROLE_FORBIDDEN
    # The slot stays consumed (a refund skips the rate-limit headers).
    assert "X-RateLimit-Remaining" in response.headers
    assert _credit_rows(client) == _NOTHING and llm.calls == 0


def test_the_reserved_field_is_422_before_the_idempotency_key(
    client: Any, enforced: World, switch: Any, llm: _FakeLLM
) -> None:
    body = _body(enforced.api_session)
    body["payload"]["metadata"] = {"interaction_origin": "production"}
    response = _post(client, enforced, key=None, body=body)
    assert response.status_code == 422 and response.json() == ORIGIN_RESERVED
    assert _credit_rows(client) == _NOTHING and llm.calls == 0


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (None, "idempotency_key_required"),
        ("", "idempotency_key_invalid"),
        ("k" * 256, "idempotency_key_invalid"),
        ("chave com espaço", "idempotency_key_invalid"),
        ("chave-ç", "idempotency_key_invalid"),
    ],
)
def test_the_key_is_400_before_the_session_ownership(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    raw: str | None,
    code: str,
) -> None:
    headers: dict[str, Any] = {"X-API-Key": enforced.api_key}
    if raw is not None:
        headers["Idempotency-Key"] = raw.encode("latin-1")  # raw header bytes
    # A FOREIGN session: the key is judged first.
    response = client.post(EVENTS, json=_body(enforced.other_session), headers=headers)
    assert response.status_code == 400 and response.json() == {"code": code}
    assert "X-RateLimit-Remaining" in response.headers  # consumed
    assert _credit_rows(client) == _NOTHING and llm.calls == 0


def test_a_key_of_255_visible_characters_is_accepted(
    client: Any, enforced: World, switch: Any, llm: _FakeLLM
) -> None:
    response = _post(client, enforced, key="k" * 255)
    assert response.status_code == 200, response.text


def test_a_foreign_session_is_403_before_the_claim_and_gives_the_slot_back(
    client: Any, enforced: World, switch: Any, llm: _FakeLLM
) -> None:
    decided = _post(client, enforced, key="shared-key")
    assert decided.status_code == 200
    rows = _credit_rows(client)
    before = _tokens(enforced.api_tenant)
    # The same key (whose decided answer would replay) on a foreign session.
    response = _post(client, enforced, key="shared-key", session=enforced.other_session)
    assert response.status_code == 403 and response.json() == TENANT_MISMATCH
    # Refunded: the refund path skips the rate-limit headers, and the bucket
    # got its token back (only the refill can have added more since).
    assert "X-RateLimit-Remaining" not in response.headers
    assert _tokens(enforced.api_tenant) >= before
    assert _credit_rows(client) == rows


def test_the_claim_answers_before_the_402_precheck(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _post(client, enforced, key="decided").status_code == 200
    first = _post(client, enforced, key="decided").json()

    async def held(_self: CreditRepository, _builder: UUID) -> bool:
        return True

    monkeypatch.setattr(CreditRepository, "quarantine_hold", held)
    replay = _post(client, enforced, key="decided")
    assert replay.status_code == 200 and replay.json() == first
    conflict = _post(client, enforced, key="decided", content="outra mensagem")
    assert conflict.status_code == 409 and conflict.json() == {
        "code": "payload_conflict"
    }
    pending = _post(client, enforced, key="fresh")
    assert pending.status_code == 402 and pending.json() == {"code": "credit_pending"}


def test_a_standalone_tenant_is_402_without_llm_or_any_credit_row(
    client: Any, enforced: World, switch: Any, llm: _FakeLLM
) -> None:
    response = _post(client, enforced, key="standalone-1", standalone=True)
    assert response.status_code == 402 and response.json() == {"code": "credit_pending"}
    assert "X-RateLimit-Remaining" in response.headers  # the slot stays consumed
    assert llm.calls == 0
    assert _credit_rows(client) == _NOTHING
    events = [
        e for e in _rows(client, EventRow) if e.tenant_id == enforced.standalone_tenant
    ]
    assert events == []


def test_a_builder_on_hold_is_402_and_not_even_its_account_is_written(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def held(_self: CreditRepository, _builder: UUID) -> bool:
        return True

    monkeypatch.setattr(CreditRepository, "quarantine_hold", held)
    _forbid_pipeline(monkeypatch)
    response = _post(client, enforced, key="held-1")
    assert response.status_code == 402 and response.json() == {"code": "credit_pending"}
    assert llm.calls == 0 and _credit_rows(client) == _NOTHING


# ---------------------------------------------------------------------------
# One decision = one trace, atomic with its bundle and its stored answer.
# ---------------------------------------------------------------------------


def _only(client: Any, model: type[Any]) -> Any:
    [row] = _rows(client, model)
    return row


def test_a_decision_is_charged_once_with_its_bundle_and_replayed_identically(
    client: Any, enforced: World, switch: Any, llm: _FakeLLM
) -> None:
    first = _post(client, enforced, key="order-123")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["prompt"]["suggested_response"] == _LLM_REPLY
    assert body["interaction_event_id"] is not None

    claim = _only(client, CreditIngressClaim)
    assert (claim.mode, claim.source, claim.decision_version) == (
        "enforced",
        "events",
        1,
    )
    assert claim.session_version is not None and claim.session_version >= 1
    assert claim.principal_id == f"api_key:{enforced.api_tenant}"
    effect_id = trace_effect_id(enforced.api_tenant, claim.interaction_id)
    effect = _only(client, CreditEffect)
    assert (effect.effect_id, effect.effect_kind, effect.builder_id) == (
        effect_id,
        "trace",
        enforced.builder,
    )
    assert effect.tenant_id == enforced.api_tenant
    assert effect.decision_digest and len(effect.decision_digest) == 64
    # No lot: the whole trace is debt (a builder that never paid is served).
    assert _sum_entries(client, "debt") == TRACE_COST_MC
    assert _sum_entries(client, "debit") == 0
    [account] = _rows(client, CreditAccount)
    assert account.builder_id == enforced.builder
    outbox = _only(client, CreditResponseOutbox)
    assert (outbox.effect_id, outbox.state) == (effect_id, "delivered")
    assert outbox.payload == body
    [decision] = [
        e
        for e in _rows(client, EventRow)
        if e.event_type == "srm.decision" and e.tenant_id == enforced.api_tenant
    ]
    assert decision.event_metadata["credit_effect_id"] == effect_id
    assert str(decision.id) == body["event_id"]
    assert _rows(client, CreditObservationalEffect) == []

    replay = _post(client, enforced, key="order-123")
    assert replay.status_code == 200 and replay.content == first.content
    assert llm.calls == 1
    assert len(_rows(client, CreditEffect)) == 1
    assert _sum_entries(client, "debt") == TRACE_COST_MC


async def _grant(builder_id: UUID, granted_mc: int) -> None:
    database = get_db_session()
    try:
        async with database.begin():
            ledger = CreditLedger(database)
            now = datetime.now(timezone.utc)
            book = await ledger.open_book(builder_id, at=now)
            order = uuid4()
            paid = date.today()
            book.grant(
                GrantSpec(
                    lot_kind="topup",
                    effect_id=topup_grant_effect_id(order),
                    payment_id=f"pay_{order.hex[:8]}",
                    paid_at=paid,
                    granted_mc=granted_mc,
                    expires_at=topup_lot_expires_at(paid),
                    order_id=order,
                ),
                at=now,
            )
            await ledger.persist(book)
    finally:
        await database.close()


def test_a_decision_above_the_balance_consumes_it_and_owes_the_rest(
    client: Any, enforced: World, switch: Any, llm: _FakeLLM
) -> None:
    client._submit(_grant(enforced.builder, 400))
    assert _post(client, enforced, key="above").status_code == 200
    assert _sum_entries(client, "debit") == 400
    assert _sum_entries(client, "debt") == TRACE_COST_MC - 400
    [debt] = _rows(client, CreditDebt)
    assert debt.amount_mc == TRACE_COST_MC - 400


def test_a_decision_without_a_configured_llm_still_costs_one_trace(
    client: Any, enforced: World, switch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "src.llm.client.get_tenant_llm_client", MagicMock(return_value=None)
    )
    monkeypatch.setattr(
        "src.llm.client.get_multimodal_text_llm_client", MagicMock(return_value=None)
    )
    response = _post(client, enforced, key="no-llm")
    assert response.status_code == 200
    assert not response.json()["prompt"].get("suggested_response")
    assert _sum_entries(client, "debt") == TRACE_COST_MC
    assert _only(client, CreditEffect).effect_kind == "trace"


def test_a_non_message_event_is_charged_too(
    client: Any, enforced: World, switch: Any, llm: _FakeLLM
) -> None:
    body = _body(enforced.api_session, event_type="session.start")
    response = _post(client, enforced, key="session-start", body=body)
    assert response.status_code == 200, response.text
    assert response.json()["interaction_event_id"] is None
    assert _only(client, CreditEffect).effect_kind == "trace"


def test_the_debit_origin_is_the_live_context_only(
    client: Any, enforced: World, switch: Any, llm: _FakeLLM
) -> None:
    """Pre-deploy ``unknown`` rows and late ``unknown`` handoff events of the
    session never turn the production decision into a free one."""

    async def seed() -> None:
        database = get_db_session()
        try:
            async with database.begin():
                for event_type in ("message.in", "srm.decision", "handoff.accept"):
                    database.add(
                        EventRow(
                            id=uuid4(),
                            tenant_id=enforced.api_tenant,
                            session_id=enforced.api_session,
                            event_type=event_type,
                            interaction_origin="unknown",
                            event_data={"content": "pre-deploy"},
                            event_metadata={},
                        )
                    )
        finally:
            await database.close()

    client._submit(seed())
    assert _post(client, enforced, key="live").status_code == 200
    assert _only(client, CreditEffect).effect_kind == "trace"
    assert _sum_entries(client, "debt") == TRACE_COST_MC


# ---------------------------------------------------------------------------
# Never enforced: simulator, unknown, the switch off, before the enforcement.
# ---------------------------------------------------------------------------


def test_the_panel_jwt_is_served_without_a_key_and_never_charged(
    client: Any, enforced: World, switch: Any, llm: _FakeLLM
) -> None:
    response = client.post(
        EVENTS, json=_body(enforced.jwt_session), headers=_jwt(enforced.jwt_tenant)
    )
    assert response.status_code == 200, response.text
    rows = _credit_rows(client)
    assert rows["credit_effects"] == rows["credit_ingress_claims"] == 0
    assert rows["credit_ledger_entries"] == rows["credit_accounts"] == 0


def test_an_unknown_origin_is_served_without_a_key_and_never_charged(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        events_routes,
        "classify_interaction_origin",
        lambda _tenant: InteractionOrigin.UNKNOWN,
    )
    response = _post(client, enforced, key=None)
    assert response.status_code == 200, response.text
    rows = _credit_rows(client)
    assert rows["credit_effects"] == rows["credit_ingress_claims"] == 0
    assert rows["credit_ledger_entries"] == rows["credit_accounts"] == 0


def _forbid_enforced_siblings(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("an enforced sibling ran")

    monkeypatch.setattr(EventService, "generate_response", fail)
    monkeypatch.setattr(EventService, "write_interaction_bundle", fail)


def test_before_the_enforcement_the_answer_is_main_s(
    client: Any,
    world: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _forbid_enforced_siblings(monkeypatch)
    response = _post(client, world, key=None)
    assert response.status_code == 200
    rows = _credit_rows(client)
    assert rows["credit_effects"] == rows["credit_ledger_entries"] == 0
    assert rows["credit_observational_effects"] == 1  # Group 3a metering


def test_with_the_switch_off_an_enforced_database_answers_like_main(
    client: Any,
    enforced: World,
    switch: Callable[[bool], None],
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switch(False)
    _forbid_enforced_siblings(monkeypatch)
    response = _post(client, enforced, key=None)
    assert response.status_code == 200
    assert _credit_rows(client) == _NOTHING


# ---------------------------------------------------------------------------
# The claim's state table (item 4-vi), one test per row.
# ---------------------------------------------------------------------------


async def _insert_claim(
    world: World,
    *,
    key: str,
    content: str = "Olá, quero agendar",
    started: datetime,
    session_version: int | None = None,
    mode: str = "enforced",
) -> UUID:
    from src.api.schemas.events import CanonicalEventRequest, EventRequest

    request = EventRequest.model_validate(_body(world.api_session, content))
    canonical = CanonicalEventRequest(
        tenant_id=world.api_tenant,
        session_id=world.api_session,
        event_type=request.event_type,
        payload=request.payload,
    )
    interaction = uuid4()
    database = get_db_session()
    try:
        async with database.begin():
            database.add(
                CreditIngressClaim(
                    id=uuid4(),
                    tenant_id=world.api_tenant,
                    builder_id=world.builder,
                    source="events",
                    principal_id=f"api_key:{world.api_tenant}",
                    idempotency_key=key,
                    interaction_id=interaction,
                    payload_sha256=event_payload_sha256(canonical),
                    decision_version=1,
                    session_version=session_version,
                    mode=mode,
                    created_at=started,
                )
            )
    finally:
        await database.close()
    return interaction


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _lease() -> timedelta:
    return timedelta(seconds=CLAIM_LEASE_SECONDS)


def test_a_claim_without_phase1_within_its_lease_is_in_progress(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client._submit(_insert_claim(enforced, key="busy", started=_now()))
    _forbid_pipeline(monkeypatch)
    response = _post(client, enforced, key="busy")
    assert response.status_code == 409 and response.json() == {
        "code": "event_in_progress"
    }
    assert response.headers["Retry-After"] == "5"
    assert len(_rows(client, CreditIngressClaim)) == 1 and llm.calls == 0


def test_retry_after_follows_the_lease_left_and_never_exceeds_5_seconds(
    client: Any, enforced: World, switch: Any, llm: _FakeLLM
) -> None:
    almost = _now() - _lease() + timedelta(seconds=2.2)
    client._submit(_insert_claim(enforced, key="almost", started=almost))
    response = _post(client, enforced, key="almost")
    assert response.status_code == 409
    assert int(response.headers["Retry-After"]) in {1, 2, 3}
    assert credit_metering.retry_after_seconds(10_000) == 5
    assert credit_metering.retry_after_seconds(0.2) == 1
    assert credit_metering.retry_after_seconds(4.1) == 5
    assert credit_metering.retry_after_seconds(2.5) == 3


def test_an_abandoned_claim_before_phase1_is_taken_over_under_its_interaction(
    client: Any, enforced: World, switch: Any, llm: _FakeLLM
) -> None:
    stale = _now() - _lease() - timedelta(seconds=1)
    interaction = client._submit(_insert_claim(enforced, key="crashed", started=stale))
    response = _post(client, enforced, key="crashed")
    assert response.status_code == 200, response.text
    claim = _only(client, CreditIngressClaim)
    assert claim.interaction_id == interaction and claim.decision_version == 1
    assert claim.session_version is not None
    assert claim.created_at.replace(tzinfo=timezone.utc) > stale
    assert _only(client, CreditEffect).effect_id == trace_effect_id(
        enforced.api_tenant, interaction
    )


def test_phase1_committed_within_the_lease_is_in_progress(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client._submit(_insert_claim(enforced, key="p1", started=_now(), session_version=3))
    _forbid_pipeline(monkeypatch)
    response = _post(client, enforced, key="p1")
    assert response.status_code == 409 and response.json() == {
        "code": "event_in_progress"
    }
    assert 1 <= int(response.headers["Retry-After"]) <= 5


def test_phase1_committed_after_the_lease_is_a_final_event_failed(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stale = _now() - _lease() - timedelta(seconds=1)
    interaction = client._submit(
        _insert_claim(enforced, key="lost", started=stale, session_version=3)
    )
    _forbid_pipeline(monkeypatch)
    for _ in range(2):  # final: never re-executed, never recomputed
        response = _post(client, enforced, key="lost")
        assert response.status_code == 409 and response.json() == {
            "code": "event_failed",
            "interaction_id": str(interaction),
        }
        assert "Retry-After" not in response.headers
    assert llm.calls == 0 and _rows(client, CreditEffect) == []


def test_an_observational_claim_is_a_duplicate_never_executed(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interaction = client._submit(
        _insert_claim(
            enforced,
            key="before",
            started=_now(),
            session_version=0,
            mode="observational",
        )
    )
    _forbid_pipeline(monkeypatch)
    response = _post(client, enforced, key="before")
    assert response.status_code == 409 and response.json() == {
        "code": "duplicate_event",
        "interaction_id": str(interaction),
    }
    assert _rows(client, CreditEffect) == []


async def _age_outbox(by: timedelta) -> None:
    database = get_db_session()
    try:
        async with database.begin():
            for row in (await database.execute(select(CreditResponseOutbox))).scalars():
                row.created_at = row.created_at - by
    finally:
        await database.close()


async def _empty_outbox_payload() -> None:
    database = get_db_session()
    try:
        async with database.begin():
            await database.execute(update(CreditResponseOutbox).values(payload={}))
    finally:
        await database.close()


def test_a_decided_answer_past_the_retention_is_a_duplicate(
    client: Any, enforced: World, switch: Any, llm: _FakeLLM
) -> None:
    first = _post(client, enforced, key="old")
    claim = _only(client, CreditIngressClaim)
    within = settings.credit_replay_retention_hours
    client._submit(_age_outbox(timedelta(hours=within) - timedelta(minutes=5)))
    assert _post(client, enforced, key="old").json() == first.json()
    client._submit(_age_outbox(timedelta(minutes=6)))
    response = _post(client, enforced, key="old")
    assert response.status_code == 409 and response.json() == {
        "code": "duplicate_event",
        "interaction_id": str(claim.interaction_id),
    }
    assert llm.calls == 1


def test_a_swept_payload_is_a_duplicate(
    client: Any, enforced: World, switch: Any, llm: _FakeLLM
) -> None:
    _post(client, enforced, key="swept")
    client._submit(_empty_outbox_payload())
    response = _post(client, enforced, key="swept")
    assert response.status_code == 409 and response.json()["code"] == "duplicate_event"


def test_the_retention_setting_defaults_to_24_and_refuses_above_168() -> None:
    assert Settings.model_fields["credit_replay_retention_hours"].default == 24
    assert (
        Settings(credit_replay_retention_hours=168).credit_replay_retention_hours == 168
    )
    with pytest.raises(ValueError):
        Settings(credit_replay_retention_hours=169)
    with pytest.raises(ValueError):
        Settings(credit_replay_retention_hours=0)


def test_a_different_payload_is_a_logged_conflict_before_any_pipeline(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _post(client, enforced, key="same-key")
    rows = _credit_rows(client)
    claim = _only(client, CreditIngressClaim)
    warnings: list[tuple[str, dict[str, Any]]] = []

    class _Log:
        def warning(self, message: str, **fields: Any) -> None:
            warnings.append((message, fields))

        def info(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    monkeypatch.setattr(credit_metering, "logger", _Log())
    _forbid_pipeline(monkeypatch)
    response = _post(client, enforced, key="same-key", content="Outro conteúdo")
    assert response.status_code == 409 and response.json() == {
        "code": "payload_conflict"
    }
    [(message, fields)] = warnings
    assert message == "credit_payload_conflict"
    assert fields["tenant_id"] == str(enforced.api_tenant)
    assert fields["interaction_id"] == str(claim.interaction_id)
    assert fields["stored_payload_sha256"] == claim.payload_sha256
    assert fields["request_payload_sha256"] != claim.payload_sha256
    assert "Outro conteúdo" not in repr(fields)
    assert _credit_rows(client) == rows


# ---------------------------------------------------------------------------
# Effects of zero and the late hold (D2).
# ---------------------------------------------------------------------------


def test_a_hold_seen_only_at_the_final_transaction_delivers_with_an_effect_of_zero(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bool] = []

    async def late(_self: CreditRepository, _builder: UUID) -> bool:
        seen.append(True)
        return len(seen) > 1  # free at the pre-check, held afterwards

    monkeypatch.setattr(CreditRepository, "quarantine_hold", late)
    response = _post(client, enforced, key="late-hold")
    assert response.status_code == 200, response.text
    assert response.json()["prompt"]["suggested_response"] == _LLM_REPLY
    effect = _only(client, CreditEffect)
    assert effect.effect_kind == CREDIT_PENDING_EFFECT_KIND
    assert effect.decision_digest and len(effect.decision_digest) == 64
    assert _rows(client, CreditLedgerEntry) == []
    assert _only(client, CreditResponseOutbox).state == "delivered"


# ---------------------------------------------------------------------------
# The phase 1 handoff (D6/#145, MEDIUM-5, LOW-6).
# ---------------------------------------------------------------------------


@pytest.fixture
def dispatches(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Order of the final commit and the post-commit dispatch."""

    order: list[str] = []
    original_final = credit_metering.run_enforced_final
    original_dispatch = events_routes.dispatch_committed_handoff_webhooks

    async def final(*args: Any, **kwargs: Any) -> Any:
        outcome = await original_final(*args, **kwargs)
        order.append("final")
        return outcome

    async def dispatch(session: Any) -> None:
        order.append("dispatch")
        await original_dispatch(session)

    monkeypatch.setattr(credit_metering, "run_enforced_final", final)
    monkeypatch.setattr(events_routes, "dispatch_committed_handoff_webhooks", dispatch)
    return order


def test_a_phase1_handoff_is_charged_once_and_linked_never_exposed(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    dispatches: list[str],
) -> None:
    response = _post(client, enforced, key="handoff-1", content=_HANDOFF_TEXT)
    assert response.status_code == 200, response.text
    assert response.json()["handoff_summary"] is not None
    claim = _only(client, CreditIngressClaim)
    effect_id = trace_effect_id(enforced.api_tenant, claim.interaction_id)
    effect = _only(client, CreditEffect)
    assert (effect.effect_id, effect.effect_kind) == (effect_id, "trace")
    assert _sum_entries(client, "debt") == TRACE_COST_MC
    record = _only(client, HandoffRecord)
    assert record.context["credit_effect_id"] == effect_id
    assert record.context.get("key_info") is not None  # the record's context kept
    [decision] = [e for e in _rows(client, EventRow) if e.event_type == "srm.decision"]
    assert decision.event_metadata["credit_effect_id"] == effect_id
    assert dispatches == ["final", "dispatch"]
    detail = client.get(
        f"/v1/handoffs/{record.id}", headers={"X-API-Key": enforced.api_key}
    )
    assert detail.status_code == 200, detail.text
    assert "credit_effect_id" not in detail.text
    # A retry replays: no second handoff, no second charge.
    assert _post(client, enforced, key="handoff-1", content=_HANDOFF_TEXT).json() == (
        response.json()
    )
    assert len(_rows(client, HandoffRecord)) == 1
    assert len(_rows(client, CreditEffect)) == 1


def test_a_hold_at_the_phase1_handoff_writes_zero_and_the_handoff_goes_on(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[bool] = []

    async def late(_self: CreditRepository, _builder: UUID) -> bool:
        seen.append(True)
        return len(seen) > 1

    monkeypatch.setattr(CreditRepository, "quarantine_hold", late)
    response = _post(client, enforced, key="handoff-hold", content=_HANDOFF_TEXT)
    assert response.status_code == 200, response.text
    effect = _only(client, CreditEffect)
    assert effect.effect_kind == CREDIT_PENDING_EFFECT_KIND
    assert _rows(client, CreditLedgerEntry) == []
    assert _only(client, HandoffRecord).context["credit_effect_id"] == effect.effect_id
    # The final transaction reused it: one effect only, the answer stored.
    assert len(seen) == 2
    assert _only(client, CreditResponseOutbox).effect_id == effect.effect_id


def test_a_dispatch_error_never_changes_the_answer(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def broken(_session: Any) -> None:
        raise RuntimeError("clinic webhook down")

    failures: list[str] = []

    def record(error: BaseException, **fields: Any) -> None:
        failures.append(str(fields["owner"]))

    monkeypatch.setattr(events_routes, "dispatch_committed_handoff_webhooks", broken)
    monkeypatch.setattr(
        events_routes, "record_handoff_post_commit_dispatch_failure", record
    )
    response = _post(client, enforced, key="handoff-broken", content=_HANDOFF_TEXT)
    assert response.status_code == 200
    assert failures == ["events"]
    assert len(_rows(client, CreditEffect)) == 1


def test_an_llm_error_still_costs_one_trace(
    client: Any, enforced: World, switch: Any, llm: _FakeLLM, monkeypatch: Any
) -> None:
    """D5: the decision exists and is returned without a generated answer."""

    async def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm, "generate", broken)
    response = _post(client, enforced, key="llm-down")
    assert response.status_code == 200, response.text
    assert not response.json()["prompt"].get("suggested_response")
    assert _only(client, CreditEffect).effect_kind == "trace"
    assert _sum_entries(client, "debt") == TRACE_COST_MC


def _standalone_at(monkeypatch: pytest.MonkeyPatch, *calls: int) -> None:
    """``tenant_scope(lock=True)`` reads the tenant as standalone on the
    given calls (1 pre-check, 2 claim insert, 3 phase 1, 4 final)."""

    from src.repositories.credit import TenantScope

    original = CreditRepository.tenant_scope
    seen: list[int] = []

    async def scope(
        self: CreditRepository, tenant_id: UUID, *, lock: bool = False
    ) -> Any:
        found = await original(self, tenant_id, lock=lock)
        if not lock or found is None:
            return found
        seen.append(1)
        if len(seen) in calls:
            return TenantScope(tenant_id=tenant_id, builder_id=None)
        return found

    monkeypatch.setattr(CreditRepository, "tenant_scope", scope)


def test_a_handoff_of_a_tenant_standalone_at_phase1_is_never_charged_later(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOW-5: the phase 1 handoff decided the charge (none: standalone then);
    the final transaction reuses that outcome and never looks the builder up
    again."""

    _standalone_at(monkeypatch, 3)
    response = _post(client, enforced, key="standalone-p1", content=_HANDOFF_TEXT)
    assert response.status_code == 200, response.text
    assert _rows(client, CreditEffect) == [] and _rows(client, CreditLedgerEntry) == []
    record = _only(client, HandoffRecord)
    assert "credit_effect_id" not in record.context
    [decision] = [e for e in _rows(client, EventRow) if e.event_type == "srm.decision"]
    assert "credit_effect_id" not in decision.event_metadata
    assert _only(client, CreditResponseOutbox).payload == response.json()


def test_a_tenant_standalone_by_the_final_transaction_commits_without_an_effect(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOW-5: bundle and stored answer commit, no new effect, logged."""

    _standalone_at(monkeypatch, 4)
    response = _post(client, enforced, key="standalone-final")
    assert response.status_code == 200, response.text
    assert _rows(client, CreditEffect) == [] and _rows(client, CreditLedgerEntry) == []
    [decision] = [e for e in _rows(client, EventRow) if e.event_type == "srm.decision"]
    assert "credit_effect_id" not in decision.event_metadata
    assert _only(client, CreditResponseOutbox).payload == response.json()


# ---------------------------------------------------------------------------
# Fault injection: no partial write, and the lease is released (M7, D6).
# ---------------------------------------------------------------------------


def _boom(name: str) -> Callable[..., Any]:
    async def fail(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(f"injected {name}")

    return fail


@pytest.mark.parametrize(
    "target", ["tenant_scope", "ensure_account", "quarantine_hold"]
)
def test_a_precheck_fault_writes_nothing(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    monkeypatch.setattr(CreditRepository, target, _boom(target))
    response = _post(client, enforced, key="precheck-fault")
    assert response.status_code == 500
    assert _credit_rows(client) == _NOTHING and llm.calls == 0


def test_a_claim_insert_fault_writes_no_claim(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(CreditRepository, "insert_enforced_claim", _boom("claim"))
    response = _post(client, enforced, key="claim-fault")
    assert response.status_code == 500
    rows = _credit_rows(client)
    assert rows["credit_ingress_claims"] == rows["credit_effects"] == 0
    assert llm.calls == 0


@pytest.mark.parametrize(
    "target", ["link_handoff_credit_effect", "mark_claim_phase1", "insert_effect"]
)
def test_a_phase1_fault_after_the_handoff_write_leaves_nothing_and_releases(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    if target == "insert_effect":
        # The effect of zero of a hold seen at the phase 1 debit.
        calls: list[int] = []

        async def late(_self: CreditRepository, _builder: UUID) -> bool:
            calls.append(1)
            return len(calls) > 1

        monkeypatch.setattr(CreditRepository, "quarantine_hold", late)
    monkeypatch.setattr(CreditRepository, target, _boom(target))
    response = _post(client, enforced, key="p1-fault", content=_HANDOFF_TEXT)
    assert response.status_code == 500
    assert _rows(client, HandoffRecord) == []
    handoff_events = [
        e for e in _rows(client, EventRow) if e.event_type == "handoff.request"
    ]
    assert handoff_events == []
    assert _rows(client, CreditEffect) == [] and _rows(client, CreditLedgerEntry) == []
    claim = _only(client, CreditIngressClaim)
    assert claim.session_version is None
    # Released: the next retry takes over at once and decides.
    monkeypatch.undo()
    monkeypatch.setattr(CreditSwitch, "effective", lambda _self: True)
    monkeypatch.setattr(
        "src.llm.client.get_tenant_llm_client", MagicMock(return_value=llm)
    )
    retry = _post(client, enforced, key="p1-fault", content=_HANDOFF_TEXT)
    assert retry.status_code == 200, retry.text
    assert len(_rows(client, HandoffRecord)) == 1
    assert len(_rows(client, CreditEffect)) == 1


def _failing_append(monkeypatch: pytest.MonkeyPatch) -> None:
    original = EventStore.append_batch

    async def append(self: EventStore, events: Any, *, session: Any = None) -> Any:
        if session is not None and any(e.event_type == "srm.decision" for e in events):
            raise RuntimeError("injected bundle failure")
        return await original(self, events, session=session)

    monkeypatch.setattr(EventStore, "append_batch", append)


def test_a_bundle_fault_undoes_decision_and_debit_together_and_fails_final(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _failing_append(monkeypatch)
    response = _post(client, enforced, key="bundle-fault")
    assert response.status_code == 500
    decisions = [e for e in _rows(client, EventRow) if e.event_type == "srm.decision"]
    assert decisions == []
    assert _rows(client, CreditEffect) == [] and _rows(client, CreditLedgerEntry) == []
    assert _rows(client, CreditResponseOutbox) == []
    claim = _only(client, CreditIngressClaim)
    assert claim.session_version is not None  # phase 1 committed
    # The lease was released: the retry is a final event_failed at once.
    retry = _post(client, enforced, key="bundle-fault")
    assert retry.status_code == 409 and retry.json() == {
        "code": "event_failed",
        "interaction_id": str(claim.interaction_id),
    }


def test_the_same_bundle_fault_outside_the_enforced_path_stays_best_effort(
    client: Any,
    world: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = EventStore.append_batch

    async def append(self: EventStore, events: Any, *, session: Any = None) -> Any:
        if any(e.event_type == "srm.decision" for e in events):
            raise RuntimeError("injected bundle failure")
        return await original(self, events, session=session)

    monkeypatch.setattr(EventStore, "append_batch", append)
    response = _post(client, world, key=None)  # no enforcement row
    assert response.status_code == 200
    assert response.json()["interaction_event_id"] is None
    assert _rows(client, CreditEffect) == []


def test_a_final_fault_after_a_phase1_handoff_never_charges_twice(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _failing_append(monkeypatch)
    response = _post(client, enforced, key="handoff-final-fault", content=_HANDOFF_TEXT)
    assert response.status_code == 500
    # The handoff and its debit committed together in phase 1.
    assert len(_rows(client, HandoffRecord)) == 1
    effect = _only(client, CreditEffect)
    assert effect.effect_kind == "trace"
    assert _sum_entries(client, "debt") == TRACE_COST_MC
    retry = _post(client, enforced, key="handoff-final-fault", content=_HANDOFF_TEXT)
    assert retry.status_code == 409 and retry.json()["code"] == "event_failed"
    assert len(_rows(client, CreditEffect)) == 1
    assert _sum_entries(client, "debt") == TRACE_COST_MC


@pytest.mark.parametrize(
    "target", ["insert_response_outbox", "hold_claim_final_fence", "insert_effect"]
)
def test_a_final_transaction_fault_leaves_no_partial_write(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
    target: str,
) -> None:
    if target == "insert_effect":
        calls: list[int] = []

        async def late(_self: CreditRepository, _builder: UUID) -> bool:
            calls.append(1)
            return len(calls) > 1

        monkeypatch.setattr(CreditRepository, "quarantine_hold", late)
    monkeypatch.setattr(CreditRepository, target, _boom(target))
    response = _post(client, enforced, key="final-fault")
    assert response.status_code == 500
    assert [e for e in _rows(client, EventRow) if e.event_type == "srm.decision"] == []
    assert _rows(client, CreditEffect) == [] and _rows(client, CreditLedgerEntry) == []
    assert _rows(client, CreditResponseOutbox) == []


def test_an_llm_failure_releases_the_lease_after_phase1(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(EventService, "generate_response", _boom("llm"))
    response = _post(client, enforced, key="llm-fault")
    assert response.status_code == 500
    claim = _only(client, CreditIngressClaim)
    started = claim.created_at.replace(tzinfo=timezone.utc)
    assert started <= _now() - _lease() + timedelta(seconds=5)
    assert _rows(client, CreditEffect) == []


def test_a_lost_final_fence_writes_no_bundle_and_answers_by_the_table(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = CreditRepository.hold_claim_final_fence

    async def expired(self: CreditRepository, claim_id: UUID, **kwargs: Any) -> bool:
        # The real predicate, evaluated as if the lease were already over.
        return bool(await original(self, claim_id, **{**kwargs, "lease": timedelta(0)}))

    monkeypatch.setattr(CreditRepository, "hold_claim_final_fence", expired)
    response = _post(client, enforced, key="fence")
    # Re-read by the table: phase 1 committed, the claim's own lease alive.
    assert (
        response.status_code == 409 and response.json()["code"] == "event_in_progress"
    )
    assert 1 <= int(response.headers["Retry-After"]) <= 5
    assert [e for e in _rows(client, EventRow) if e.event_type == "srm.decision"] == []
    assert (
        _rows(client, CreditEffect) == [] and _rows(client, CreditResponseOutbox) == []
    )


def test_a_lost_phase1_fence_rolls_everything_back(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def lost(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(CreditRepository, "mark_claim_phase1", lost)
    response = _post(client, enforced, key="p1-fence", content=_HANDOFF_TEXT)
    assert (
        response.status_code == 409 and response.json()["code"] == "event_in_progress"
    )
    assert _rows(client, HandoffRecord) == []
    assert _rows(client, CreditEffect) == [] and llm.calls == 0


# ---------------------------------------------------------------------------
# D1 (owner, option B): the enforcement is one-way per process.
# ---------------------------------------------------------------------------


class _Log:
    def __init__(self) -> None:
        self.entries: list[tuple[str, dict[str, Any]]] = []

    def _record(self, level: str, _message: str, **fields: Any) -> None:
        self.entries.append((level, fields))

    def error(self, message: str, **fields: Any) -> None:
        self._record("error", message, **fields)

    def warning(self, message: str, **fields: Any) -> None:
        self._record("warning", message, **fields)

    def info(self, message: str, **fields: Any) -> None:
        self._record("info", message, **fields)

    def reasons(self, level: str) -> list[Any]:
        return [f.get("reason") for lvl, f in self.entries if lvl == level]


def _unreadable_enforcement(monkeypatch: pytest.MonkeyPatch) -> None:
    from sqlalchemy.exc import OperationalError

    async def unreadable(_self: CreditRepository) -> Any:
        raise OperationalError("SELECT", {}, Exception("credit_enforcement_state"))

    monkeypatch.setattr(CreditRepository, "enforcement_at", unreadable)


def test_a_process_that_saw_the_enforcement_answers_503_when_it_cannot_read_it(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not credit_metering.enforcement_seen()
    assert _post(client, enforced, key="seen").status_code == 200
    assert credit_metering.enforcement_seen()
    rows, calls = _credit_rows(client), llm.calls
    log = _Log()
    monkeypatch.setattr(credit_metering, "logger", log)
    _unreadable_enforcement(monkeypatch)

    refused = _post(client, enforced, key="seen-2")
    assert refused.status_code == 503 and refused.json() == {
        "code": "credit_enforcement_unavailable"
    }
    assert "X-RateLimit-Remaining" in refused.headers  # the slot stays consumed
    assert _credit_rows(client) == rows and llm.calls == calls
    [(level, fields)] = [e for e in log.entries if e[1].get("alert")]
    assert (level, fields["alert"], fields["reason"]) == (
        "error",
        "credit_enforcement_unavailable",
        "enforcement_unreadable_after_enforced",
    )
    # Never a block for the panel (simulator): it never reads the enforcement.
    panel = client.post(
        EVENTS, json=_body(enforced.jwt_session), headers=_jwt(enforced.jwt_tenant)
    )
    assert panel.status_code == 200, panel.text


def test_a_process_that_never_saw_the_enforcement_keeps_main_s_path_on_a_failed_read(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = _Log()
    monkeypatch.setattr(credit_metering, "logger", log)
    _unreadable_enforcement(monkeypatch)
    response = _post(client, enforced, key=None)  # enforced, but never seen
    assert response.status_code == 200, response.text
    assert not credit_metering.enforcement_seen()
    rows = _credit_rows(client)
    assert rows["credit_effects"] == rows["credit_ingress_claims"] == 0
    assert "enforcement_unreadable" in log.reasons("warning")
    assert log.reasons("error") == []


# ---------------------------------------------------------------------------
# LOW-1: a cancellation releases the lease too.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("point", ["phase1", "llm"])
def test_a_cancelled_decision_releases_its_lease(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
) -> None:
    """The client goes away mid-decision: the lease is released before the
    cancellation propagates -- the next retry takes over at once (cancelled
    in phase 1) or gets the final ``event_failed`` at once (cancelled after
    it), instead of ``event_in_progress`` for the whole lease."""

    reached = asyncio.Event()

    async def stuck(*_args: Any, **_kwargs: Any) -> Any:
        reached.set()
        await asyncio.Event().wait()  # until cancelled

    headers = {"X-API-Key": enforced.api_key, "Idempotency-Key": "cancelled"}

    async def cancelled() -> None:
        request = asyncio.create_task(
            client._client.post(
                EVENTS, json=_body(enforced.api_session), headers=headers
            )
        )
        await asyncio.wait_for(reached.wait(), 30)
        request.cancel()
        with suppress(asyncio.CancelledError):
            await request

    with monkeypatch.context() as patched:
        if point == "phase1":
            # Inside the open phase 1 (pipeline, state, all written): its
            # marker.  Repository objects are fresh per call, so no earlier
            # suite's instance patch can shadow this class-level one.
            patched.setattr(CreditRepository, "mark_claim_phase1", stuck)
        else:
            patched.setattr(EventService, "generate_response", stuck)
        client._submit(cancelled())
    claim = _only(client, CreditIngressClaim)
    started = claim.created_at.replace(tzinfo=timezone.utc)
    assert started <= _now() - _lease() + timedelta(seconds=5)  # released
    retry = _post(client, enforced, key="cancelled")
    if point == "phase1":
        assert claim.session_version is None
        assert retry.status_code == 200, retry.text
        assert _only(client, CreditIngressClaim).interaction_id == claim.interaction_id
    else:
        assert claim.session_version is not None  # phase 1 committed
        assert (retry.status_code, retry.json()) == (
            409,
            {"code": "event_failed", "interaction_id": str(claim.interaction_id)},
        )
        assert _rows(client, CreditEffect) == []


@pytest.mark.parametrize("point", ["phase1", "llm"])
def test_a_cancellation_delivered_again_during_the_release_still_releases(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
    point: str,
) -> None:
    """The cancellation arrives three times, twice while the release is
    running: the shielded release still ends, no task is left behind, and
    the next retry decides as for a single cancellation (ported from the
    focused review)."""

    reached = asyncio.Event()
    release_started = asyncio.Event()
    release_go = asyncio.Event()
    leaked: list[asyncio.Task[Any]] = []

    async def stuck(*_args: Any, **_kwargs: Any) -> Any:
        reached.set()
        await asyncio.Event().wait()

    original_release = credit_metering.release_events_lease

    async def slow_release(*args: Any, **kwargs: Any) -> Any:
        release_started.set()
        await release_go.wait()
        return await original_release(*args, **kwargs)

    headers = {"X-API-Key": enforced.api_key, "Idempotency-Key": "thrice"}

    async def scenario() -> None:
        before = asyncio.all_tasks()
        request = asyncio.create_task(
            client._client.post(
                EVENTS, json=_body(enforced.api_session), headers=headers
            )
        )
        await asyncio.wait_for(reached.wait(), 30)
        request.cancel()
        await asyncio.wait_for(release_started.wait(), 30)
        request.cancel()  # delivered again while the release runs
        await asyncio.sleep(0.05)
        request.cancel()  # and a third time
        await asyncio.sleep(0.05)
        release_go.set()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(request, 30)
        await asyncio.sleep(0.05)
        leaked.extend(
            task
            for task in asyncio.all_tasks() - before
            if task is not asyncio.current_task()
        )

    with monkeypatch.context() as patched:
        if point == "phase1":
            patched.setattr(CreditRepository, "mark_claim_phase1", stuck)
        else:
            patched.setattr(EventService, "generate_response", stuck)
        patched.setattr(credit_metering, "release_events_lease", slow_release)
        client._submit(scenario())
    assert [task for task in leaked if not task.done()] == []
    claim = _only(client, CreditIngressClaim)
    started = claim.created_at.replace(tzinfo=timezone.utc)
    assert started <= _now() - _lease() + timedelta(seconds=5)  # released
    retry = _post(client, enforced, key="thrice")
    if point == "phase1":
        assert retry.status_code == 200, retry.text
    else:
        assert retry.status_code == 409 and retry.json()["code"] == "event_failed"


def test_the_release_on_cancellation_does_not_spin(
    client: Any,
    enforced: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One cancellation, a release of 0.5 s: the cleanup waits for it once.
    Starlette re-delivers the cancellation at every await; re-awaiting a
    shielded future in a loop would wake up tens of thousands of times."""

    reached = asyncio.Event()
    shields = 0
    real_shield = asyncio.shield

    def counting(awaitable: Any) -> Any:
        nonlocal shields
        shields += 1
        return real_shield(awaitable)

    async def stuck(*_args: Any, **_kwargs: Any) -> Any:
        reached.set()
        await asyncio.Event().wait()

    original_release = credit_metering.release_events_lease

    async def slow_release(*args: Any, **kwargs: Any) -> Any:
        await asyncio.sleep(0.5)
        return await original_release(*args, **kwargs)

    headers = {"X-API-Key": enforced.api_key, "Idempotency-Key": "spin"}

    async def scenario() -> None:
        request = asyncio.create_task(
            client._client.post(
                EVENTS, json=_body(enforced.api_session), headers=headers
            )
        )
        await asyncio.wait_for(reached.wait(), 30)
        request.cancel()  # ONE cancellation
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(request, 30)

    with monkeypatch.context() as patched:
        patched.setattr(CreditRepository, "mark_claim_phase1", stuck)
        patched.setattr(credit_metering, "release_events_lease", slow_release)
        patched.setattr(asyncio, "shield", counting)
        client._submit(scenario())
    assert shields <= 3, f"{shields} shield awaits during one 0.5 s release"
    claim = _only(client, CreditIngressClaim)
    started = claim.created_at.replace(tzinfo=timezone.utc)
    assert started <= _now() - _lease() + timedelta(seconds=5)  # released


# ---------------------------------------------------------------------------
# OpenAPI and the pinned lease.
# ---------------------------------------------------------------------------


def test_the_openapi_documents_every_credit_answer(client: Any) -> None:
    from src.api.main import app

    operation = app.openapi()["paths"]["/v1/events"]["post"]
    responses = operation["responses"]
    assert responses["402"]["content"]["application/json"]["example"] == {
        "code": "credit_pending"
    }
    # D1: the one-way enforcement that can no longer be read (LOW-6).
    unavailable = responses["503"]["content"]["application/json"]
    assert unavailable["example"] == {"code": "credit_enforcement_unavailable"}
    assert unavailable["schema"]["properties"]["code"]["enum"] == [
        "credit_enforcement_unavailable"
    ]
    examples_400 = responses["400"]["content"]["application/json"]["examples"]
    assert {"idempotency_key_required", "idempotency_key_invalid"} <= set(examples_400)
    conflict = responses["409"]
    codes = conflict["content"]["application/json"]["schema"]["properties"]["code"]
    assert set(codes["enum"]) == {
        "payload_conflict",
        "duplicate_event",
        "event_in_progress",
        "event_failed",
    }
    assert conflict["headers"]["Retry-After"]["schema"]["maximum"] == 5
    [header] = [p for p in operation["parameters"] if p["name"] == "Idempotency-Key"]
    assert header["in"] == "header" and header["required"] is False
    assert "idempotency_key_required" in header["description"]


def test_the_claim_lease_covers_the_worst_case_of_phase1_llm_and_final() -> None:
    import inspect

    from src.handoff.models import WebhookConfig
    from src.llm.client import LLMClient

    def bound(name: str, kind: str) -> int:
        for item in WebhookConfig.model_fields[name].metadata:
            value = getattr(item, kind, None)
            if value is not None:
                return int(value)
        raise AssertionError(f"{name} has no {kind} bound")

    attempts = bound("retry_count", "le")
    timeout = bound("timeout_seconds", "le")
    delay = bound("retry_delay_seconds", "le")
    webhook = attempts * timeout + sum(delay * 2 ** (k - 1) for k in range(1, attempts))
    llm_timeout = float(inspect.signature(LLMClient).parameters["timeout"].default)
    llm = 3 * llm_timeout  # two primary calls and one fallback
    assert webhook == credit_metering.LEASE_WEBHOOK_SECONDS == 1500
    assert llm == credit_metering.LEASE_LLM_SECONDS == 90
    assert credit_metering.PHASE1_LOCK_TIMEOUT_SECONDS == 15
    worst = (
        webhook
        + credit_metering.PHASE1_LOCK_TIMEOUT_SECONDS
        + llm
        + credit_metering.LEASE_FINAL_TRANSACTION_SECONDS
    )
    assert CLAIM_LEASE_SECONDS == 1680 >= worst
    assert credit_metering.RETRY_AFTER_MAX_SECONDS == 5


def test_the_effects_of_zero_are_pinned() -> None:
    assert credit_metering.CREDIT_PENDING_EFFECT_KIND == "trace_credit_pending"
    assert credit_metering.NO_RESPONSE_EFFECT_KIND == "trace_no_response"
    assert credit_metering.TRACE_COST_MC == 1_000


@pytest.fixture(autouse=True)
def _a_fresh_process() -> Iterator[None]:
    """D1's latch is per process: each test starts and leaves a process that
    never saw the enforcement (no leak into the suites that follow)."""

    credit_metering.forget_enforcement_seen()
    yield
    credit_metering.forget_enforcement_seen()


@pytest.fixture(autouse=True)
def _no_slot_leak(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Guard: the module slots this suite swaps come back as they were."""

    before = (
        credit_metering.run_enforced_final,
        credit_metering.logger,
        credit_metering.CLAIM_LEASE_SECONDS,
        events_routes.dispatch_committed_handoff_webhooks,
        handoff_service_module.dispatch_committed_handoff_webhooks,
    )
    yield
    monkeypatch.undo()
    after = (
        credit_metering.run_enforced_final,
        credit_metering.logger,
        credit_metering.CLAIM_LEASE_SECONDS,
        events_routes.dispatch_committed_handoff_webhooks,
        handoff_service_module.dispatch_committed_handoff_webhooks,
    )
    assert after == before
