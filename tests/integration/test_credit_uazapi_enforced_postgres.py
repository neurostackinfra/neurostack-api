"""The Uazapi adapter under the courtesy rule, on real PostgreSQL.

debito-cortesia-automatico Group 3 (DESIGN item 8, Decisions 16, 18 and 19;
wish Decision 7), through the real application (its lifespan, middleware
and pool, ``tests.integration.test_credit_metering_postgres._running_app``)
on a head database with neither ``credit_enforcement_at`` nor a mark: a
courtesy grant alone puts the world's builder under the rule, and the
world's Uazapi tenant (standalone in the shared world) is linked to it.
The LLM and the providers are faked (``_fake_llm_and_providers``), the
egress question is a stand-in, and the replay store is this process's
memory (no Redis server is touched).  Pinned:

* a charged turn -- unverified (``unknown``) or verified (``production``) --
  is claimed in PostgreSQL (``uazapi``, ``enforced``) on top of the Redis
  claim, charged ONE trace on the courtesy lot only, its reply sent inline
  from the outbox by the Uazapi branch of the route's sender with its echo
  id stored; its ``decision_digest`` carries the turn's real origin;
* a redelivery of the same ``messageid`` neither debits again nor gives a
  second reply -- also across a mode change between the deliveries (the
  rule removed, the builder marked, a grant), with the replay store holding
  the first claim and, where it forgot it, with the PostgreSQL claim alone
  (the observational claim of a first free delivery included); in the
  charged -> free direction (then marked) with the store down, the accepted
  residual: a second, free reply, never a second debit;
* two concurrent deliveries of one message (the replay store down) meet at
  the PostgreSQL claim: one decision, one debit, one reply;
* the race: the courtesy removed between the capture and the account lock
  is 503 with nothing written;
* blocked: ``credit_suspended`` with nothing written, no session and
  nothing sent; with a paused session the text-only capture stays;
* ``uazapi_egress_enabled`` closed: a charged builder's turn is main's
  observational turn (nothing charged);
* a charged reply whose process died after the final commit is recovered
  by the publisher (a courtesy lot keeps it on) through the Uazapi branch,
  once; its redelivery is ``duplicate_message``;
* the long-audio handoff commits its ``uazapi`` claim, the handoff and its
  one trace together;
* under a mark the same turn is main's observational turn (no enforced
  claim, no debit).

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from src.repositories.credit import CreditRepository
from src.services import credit_metering, egress_policy
from tests.integration.credit_rollout_world import (
    grant_courtesy,
    record_mark,
    relink_tenant,
    remove_courtesy,
)
from tests.integration.test_credit_metering_postgres import (
    _PG,
    _TURN_SECONDS,
    _Gate,
    _migrate,
    _no_app_publisher,
    _pause,
    _running_app,
    _seed_patient,
    _sent_to,
    pg,
)

pytestmark = pytest.mark.integration

__all__ = ["pg"]

_PHONE = "5511900031001"
_UAZAPI = "/v1/webhook/uazapi"
_REPLY_ID = "credit-dominance-uazapi-reply"  # the faked send's MessageID


@pytest.fixture(scope="module")
async def rule_pg(pg: _PG) -> AsyncIterator[_PG]:
    """A head database with neither the global nor a mark."""

    name = await pg.cluster.fresh()
    url = pg.cluster.url(name)
    await _migrate(url, "upgrade", "head")
    yield _PG(cluster=pg.cluster, url=url)


@pytest.fixture
async def rule_direct(rule_pg: _PG) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(rule_pg.url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


@dataclass
class _Egress:
    answer: bool = True
    reads: int = 0


@pytest.fixture
def uazapi_edges(monkeypatch: pytest.MonkeyPatch) -> _Egress:
    """The egress question (open unless a test closes it), the image egress
    gate following it, and no Redis server: the replay claim and the echo
    store are this process's memory."""

    from src.api.routes import webhook
    from src.services.egress_policy import EgressDisabledError

    egress = _Egress()

    async def question() -> bool:
        egress.reads += 1
        return egress.answer

    async def gate() -> None:
        if not egress.answer:
            raise EgressDisabledError("uazapi_egress_disabled")

    monkeypatch.setattr(egress_policy, "uazapi_egress_open", question)
    monkeypatch.setattr(webhook, "require_uazapi_egress", gate)
    monkeypatch.setattr(webhook, "_get_redis", lambda: None)
    return egress


@pytest.fixture
def replay_store_down(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    """``forget(True)``: the replay store forgot every id (another process,
    Redis lost): only the PostgreSQL claim can stop a redelivery."""

    from src.api.routes import webhook

    real = webhook._claim_incoming_message

    def forget(down: bool) -> None:
        monkeypatch.setattr(
            webhook,
            "_claim_incoming_message",
            (lambda *_args: True) if down else real,
        )

    return forget


def _text(world: Any, message_id: str, *, phone: str = _PHONE, **extra: Any) -> Any:
    return {
        "instanceName": world.uazapi_instance,
        "message": {
            "messageid": message_id,
            "type": "text",
            "text": "Olá, quero agendar",
            "fromMe": False,
            "sender_pn": f"{phone}@s.whatsapp.net",
        },
        "chat": {"wa_chatid": f"{phone}@s.whatsapp.net"},
        **extra,
    }


def _long_audio(world: Any, message_id: str, *, phone: str = _PHONE) -> Any:
    return {
        "instanceName": world.uazapi_instance,
        "message": {
            "messageid": message_id,
            "type": "media",
            "messageType": "AudioMessage",
            "mediaType": "ptt",
            "content": {"seconds": 600, "mimetype": "audio/ogg; codecs=opus"},
            "fromMe": False,
            "sender_pn": f"{phone}@s.whatsapp.net",
        },
        "chat": {"wa_chatid": f"{phone}@s.whatsapp.net"},
    }


async def _under_the_rule(direct: AsyncEngine, world: Any, credits: int = 10) -> UUID:
    """The world's Uazapi tenant linked to the builder, and the builder
    under the rule by a courtesy grant; returns the courtesy lot."""

    await relink_tenant(direct, UUID(world.uazapi_tenant), UUID(world.builder))
    granted = await grant_courtesy(direct, UUID(world.builder), credits)
    return UUID(str(granted.lot_id))


@dataclass
class _Rows:
    claims: list[tuple[str, str, str]]
    traces: list[str]
    debits: list[tuple[int, str]]
    debts: int
    outbox: list[tuple[str, Any]]
    observed: list[tuple[str, bool, int]]
    sessions: int
    handoffs: list[Any]


async def _rows(direct: AsyncEngine, world: Any, phone: str = _PHONE) -> _Rows:
    tenant = UUID(world.uazapi_tenant)
    builder = UUID(world.builder)

    async def all_(connection: Any, sql: str, **params: Any) -> list[Any]:
        return list((await connection.execute(text(sql), params)).all())

    async with direct.connect() as connection:
        claims = await all_(
            connection,
            "SELECT source, mode, provider_message_id FROM credit_ingress_claims "
            "WHERE tenant_id = :t ORDER BY created_at, id",
            t=tenant,
        )
        traces = await all_(
            connection,
            "SELECT effect_id FROM credit_effects WHERE tenant_id = :t AND "
            "effect_kind = 'trace' ORDER BY created_at, effect_id",
            t=tenant,
        )
        debits = await all_(
            connection,
            "SELECT e.amount_mc, l.lot_kind FROM credit_ledger_entries e JOIN "
            "credit_lots l ON l.builder_id = e.builder_id AND l.id = e.lot_id "
            "WHERE e.builder_id = :b AND e.entry_type = 'debit' ORDER BY "
            "e.occurred_at, e.id",
            b=builder,
        )
        debts = await connection.scalar(
            text("SELECT count(*) FROM credit_debts WHERE builder_id = :b"),
            {"b": builder},
        )
        outbox = await all_(
            connection,
            "SELECT state, payload FROM credit_response_outbox WHERE tenant_id = :t "
            "ORDER BY created_at, id",
            t=tenant,
        )
        observed = await all_(
            connection,
            "SELECT origin, authenticated, would_debit_mc FROM "
            "credit_observational_effects WHERE tenant_id = :t ORDER BY "
            "effect_id",
            t=tenant,
        )
        sessions = await connection.scalar(
            text("SELECT count(*) FROM sessions WHERE tenant_id = :t AND user_id = :p"),
            {"t": tenant, "p": phone},
        )
        handoffs = await all_(
            connection,
            "SELECT context FROM handoff_records WHERE tenant_id = :t",
            t=tenant,
        )
    return _Rows(
        claims=[(str(s), str(m), str(p)) for s, m, p in claims],
        traces=[str(effect) for (effect,) in traces],
        debits=[(int(amount), str(kind)) for amount, kind in debits],
        debts=int(debts or 0),
        outbox=[(str(state), payload) for state, payload in outbox],
        observed=[(str(o), bool(a), int(w)) for o, a, w in observed],
        sessions=int(sessions or 0),
        handoffs=[context for (context,) in handoffs],
    )


def _uazapi_sent(phone: str = _PHONE) -> list[str]:
    from src.api.routes import webhook

    return _sent_to(webhook.send_uazapi_text, phone)


def _digest_origins(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    original = credit_metering.decision_digest

    def spy(**kwargs: Any) -> str:
        seen.append(kwargs["origin"].value)
        return original(**kwargs)

    monkeypatch.setattr(credit_metering, "decision_digest", spy)
    return seen


async def _stored_echo_ids(direct: AsyncEngine, world: Any) -> list[str]:
    async with direct.connect() as connection:
        meta = (
            await connection.execute(
                text(
                    "SELECT metadata FROM sessions WHERE tenant_id = :t "
                    "AND user_id = :p"
                ),
                {"t": UUID(world.uazapi_tenant), "p": _PHONE},
            )
        ).scalar_one()
    return [entry[0] for entry in (meta or {}).get("ai_msg_ids", [])]


@pytest.mark.parametrize("verified", [False, True], ids=["unverified", "verified"])
async def test_a_charged_uazapi_turn_is_claimed_and_charged_once_on_postgres(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    uazapi_edges: _Egress,
    verified: bool,
) -> None:
    """One enforced ``uazapi`` claim (on top of the Redis one), one trace on
    the courtesy lot (never a paid lot, never debt), the reply sent inline
    from the outbox -- ``delivered``, its PII erased, its echo id stored --
    and the real origin in the digest; the redelivery is
    ``duplicate_message``."""

    from src.api.routes import webhook

    origins = _digest_origins(monkeypatch)
    if verified:

        async def enforced() -> bool:
            return True

        monkeypatch.setattr(webhook, "uazapi_webhook_auth_enforced", enforced)
    async with _running_app(rule_pg, monkeypatch) as running:
        world, client = running.world, running.client
        await _under_the_rule(rule_direct, world)
        body = _text(world, f"pg-uazapi-charged-{verified}")
        if verified:
            async with rule_direct.connect() as connection:
                body["token"] = (
                    await connection.execute(
                        text(
                            "SELECT api_credentials ->> 'uazapi_token' FROM "
                            "clinic_configs WHERE tenant_id = :t"
                        ),
                        {"t": UUID(world.uazapi_tenant)},
                    )
                ).scalar_one()
        async with asyncio.timeout(_TURN_SECONDS):
            answer = await client.post(_UAZAPI, json=body)
            again = await client.post(_UAZAPI, json=body)
        replies = _uazapi_sent()
    assert answer.json() == {"ok": True, "replied": True}, answer.text
    assert again.json() == {"ok": True, "skipped": "duplicate_message"}
    assert replies == ["Olá! Posso ajudar com o seu agendamento."]
    seen = await _rows(rule_direct, world)
    assert seen.claims == [("uazapi", "enforced", f"pg-uazapi-charged-{verified}")]
    assert len(seen.traces) == 1 and seen.debits == [(1_000, "courtesy")]
    assert seen.debts == 0 and seen.observed == []
    [(state, payload)] = seen.outbox
    assert state == "delivered" and payload["channel"] == "uazapi"
    assert "phone" not in payload and "text" not in payload
    assert not [k for k in payload if "token" in k or "url" in k]
    assert origins == ["production" if verified else "unknown"]
    assert await _stored_echo_ids(rule_direct, world) == [_REPLY_ID]
    assert uazapi_edges.reads == 1


@pytest.mark.parametrize(
    ("change", "store_down"),
    [
        ("removed", False),
        ("removed", True),
        ("marked", False),
        ("granted", True),
        ("first_free_then_granted", True),
    ],
)
async def test_a_redelivery_across_a_mode_change_neither_debits_nor_replies_twice(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    uazapi_edges: _Egress,
    replay_store_down: Callable[[bool], None],
    change: str,
    store_down: bool,
) -> None:
    """DESIGN Decision 16, without a live redelivery: the same ``messageid``
    posted twice with the mode changed in between.  The replay store's claim
    (kept on every turn, where it was) answers the redelivery whatever the
    new mode; where the store forgot it, the PostgreSQL claim of the first
    delivery -- enforced, or the observational one of a free first delivery
    -- answers it for a charged second capture, and a blocked one is
    ``credit_suspended``: never a second debit, never a second reply."""

    async with _running_app(rule_pg, monkeypatch) as running:
        world, client = running.world, running.client
        if change == "first_free_then_granted":
            await relink_tenant(
                rule_direct, UUID(world.uazapi_tenant), UUID(world.builder)
            )
            lot = None
        else:
            lot = await _under_the_rule(rule_direct, world)
        body = _text(world, f"pg-uazapi-redelivered-{change}-{store_down}")
        async with asyncio.timeout(_TURN_SECONDS):
            first = await client.post(_UAZAPI, json=body)
            if change == "removed":
                assert lot is not None
                await remove_courtesy(rule_direct, lot)
            elif change == "marked":
                await record_mark(rule_direct, UUID(world.builder))
            else:
                await grant_courtesy(rule_direct, UUID(world.builder), 5)
            replay_store_down(store_down)
            second = await client.post(_UAZAPI, json=body)
        replies = _uazapi_sent()
    assert first.json() == {"ok": True, "replied": True}, first.text
    if store_down and change == "removed":
        expected = {"ok": True, "skipped": "credit_suspended"}
    else:
        expected = {"ok": True, "skipped": "duplicate_message"}
    assert second.json() == expected, second.text
    assert len(replies) == 1
    seen = await _rows(rule_direct, world)
    if change == "first_free_then_granted":
        # Free first (no courtesy yet), its observational claim the only one.
        assert [mode for _s, mode, _p in seen.claims] == ["observational"]
        assert seen.debits == [] and seen.traces == []
        assert seen.observed == [("unknown", False, 0)]
    else:
        assert [mode for _s, mode, _p in seen.claims] == ["enforced"]
        assert seen.debits == [(1_000, "courtesy")] and len(seen.traces) == 1
    assert seen.debts == 0


@pytest.mark.parametrize("change", ["marked", "removed_and_marked"])
async def test_charged_then_free_with_the_replay_store_down_is_the_accepted_residual(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    uazapi_edges: _Egress,
    replay_store_down: Callable[[bool], None],
    change: str,
) -> None:
    """DOCUMENTS the accepted residual of DESIGN Decision 16 (Group 3 review
    LOW-3); it pins today's behaviour, it does not ask for a change.  Across
    a mode change in the charged -> free direction (the first delivery
    charged under the rule, then the builder marked -- with or without the
    courtesy removed -- so the redelivery is Uazapi's free observational
    path), the dedupe rests on the Redis replay claim alone: the free path
    writes its observational claim only after the reply, so with the replay
    store down the redelivery is answered again, free -- a second reply but
    never a second debit (the first delivery's enforced claim and its one
    trace are the only charge)."""

    async with _running_app(rule_pg, monkeypatch) as running:
        world, client = running.world, running.client
        lot = await _under_the_rule(rule_direct, world)
        body = _text(world, f"pg-uazapi-charged-then-free-{change}")
        async with asyncio.timeout(_TURN_SECONDS):
            first = await client.post(_UAZAPI, json=body)
            if change == "removed_and_marked":
                await remove_courtesy(rule_direct, lot)
            await record_mark(rule_direct, UUID(world.builder))
            replay_store_down(True)
            second = await client.post(_UAZAPI, json=body)
        replies = _uazapi_sent()
    assert first.json() == {"ok": True, "replied": True}, first.text
    assert second.json() == {"ok": True, "replied": True}, second.text
    assert len(replies) == 2  # the residual: the free redelivery replies again
    seen = await _rows(rule_direct, world)
    assert [mode for _s, mode, _p in seen.claims] == ["enforced"]
    assert seen.debits == [(1_000, "courtesy")] and len(seen.traces) == 1
    assert seen.debts == 0
    # NIT-2 (Group 3 re-check): the free redelivery is main's observation --
    # ``unknown``, free -- and the one outbox row is the charged reply's.
    assert seen.observed == [("unknown", False, 0)]
    assert [state for state, _p in seen.outbox] == ["delivered"]


async def test_two_concurrent_deliveries_of_one_uazapi_message_charge_once(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    uazapi_edges: _Egress,
    replay_store_down: Callable[[bool], None],
) -> None:
    """The replay store down: two route sessions receive the SAME message at
    once and meet at the PostgreSQL claim -- one decides and replies, the
    other is ``duplicate_message``: one trace, one courtesy debit, one
    reply."""

    replay_store_down(True)
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
    async with _running_app(rule_pg, monkeypatch) as running:
        world, client = running.world, running.client
        await _under_the_rule(rule_direct, world)
        await _seed_patient(rule_pg.url, world.uazapi_tenant, _PHONE)
        body = _text(world, "pg-uazapi-race-1")
        async with asyncio.timeout(_TURN_SECONDS):
            tasks = [
                asyncio.create_task(client.post(_UAZAPI, json=body)) for _ in range(2)
            ]
            await both.reached.wait()
            both.opened.set()
            answers = await asyncio.gather(*tasks)
        replies = _uazapi_sent()
    assert sorted(json.dumps(a.json(), sort_keys=True) for a in answers) == sorted(
        [
            json.dumps({"ok": True, "replied": True}, sort_keys=True),
            json.dumps({"ok": True, "skipped": "duplicate_message"}, sort_keys=True),
        ]
    )
    assert sum(row is not None for row in inserted) == 1
    seen = await _rows(rule_direct, world)
    assert seen.claims == [("uazapi", "enforced", "pg-uazapi-race-1")]
    assert seen.debits == [(1_000, "courtesy")] and len(replies) == 1


async def test_the_courtesy_removed_between_capture_and_lock_is_503_with_nothing_written(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    uazapi_edges: _Egress,
) -> None:
    """DESIGN item 11: the capture said ``charged``; the courtesy is removed
    before the pre-check's account lock -- the locked re-read differs, 503
    ``credit_enforcement_unavailable``, no session, no claim, no debit,
    nothing sent."""

    lots: list[UUID] = []
    real = credit_metering.decide_webhook

    async def then_remove(db: Any, capture: Any) -> Any:
        await remove_courtesy(rule_direct, lots[0])
        return await real(db, capture)

    monkeypatch.setattr(credit_metering, "decide_webhook", then_remove)
    async with _running_app(rule_pg, monkeypatch) as running:
        world, client = running.world, running.client
        lots.append(await _under_the_rule(rule_direct, world))
        async with asyncio.timeout(_TURN_SECONDS):
            answer = await client.post(_UAZAPI, json=_text(world, "pg-uazapi-lock-1"))
        replies = _uazapi_sent()
    assert answer.status_code == 503
    assert answer.json() == {"error": "credit_enforcement_unavailable"}
    assert replies == []
    seen = await _rows(rule_direct, world)
    assert seen.claims == [] and seen.debits == [] and seen.sessions == 0
    assert seen.outbox == []


async def test_a_blocked_uazapi_builder_writes_nothing_but_the_paused_capture(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    uazapi_edges: _Egress,
) -> None:
    """Blocked (the courtesy spent, no paid plan): ``credit_suspended`` with
    no session, claim, effect or send, and the egress never read; with a
    paused session the text-only capture stays for the team."""

    from src.models.session import Session

    async with _running_app(rule_pg, monkeypatch) as running:
        world, client = running.world, running.client
        lot = await _under_the_rule(rule_direct, world)
        await remove_courtesy(rule_direct, lot)
        uazapi_edges.answer = False
        async with asyncio.timeout(_TURN_SECONDS):
            blocked = await client.post(_UAZAPI, json=_text(world, "pg-uazapi-b-1"))
        paused_phone = "5511900031002"
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(rule_direct) as session, session.begin():
            session.add(
                Session(
                    tenant_id=UUID(world.uazapi_tenant),
                    user_id=paused_phone,
                    channel="whatsapp",
                    cognitive_state={},
                    session_metadata={},
                    is_paused=True,
                )
            )
        async with asyncio.timeout(_TURN_SECONDS):
            paused = await client.post(
                _UAZAPI, json=_text(world, "pg-uazapi-b-2", phone=paused_phone)
            )
        replies = _uazapi_sent() + _uazapi_sent(paused_phone)
    assert blocked.json() == {"ok": True, "skipped": "credit_suspended"}
    assert paused.status_code == 202
    assert paused.json() == {"ok": True, "skipped": "session_paused"}
    assert replies == [] and uazapi_edges.reads == 0
    seen = await _rows(rule_direct, world)
    assert seen.claims == [] and seen.traces == [] and seen.sessions == 0
    assert seen.observed == []
    async with rule_direct.connect() as connection:
        captured = (
            await connection.execute(
                text(
                    "SELECT event_data ->> 'content' FROM events WHERE tenant_id = :t "
                    "AND metadata ->> 'paused_capture' = 'true'"
                ),
                {"t": UUID(world.uazapi_tenant)},
            )
        ).scalars()
        assert list(captured) == ["Olá, quero agendar"]


async def test_with_the_egress_closed_a_charged_uazapi_turn_is_not_charged(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    uazapi_edges: _Egress,
) -> None:
    """DESIGN Decision 19: ``uazapi_egress_enabled`` closed -- nothing would
    be delivered: main's observational turn (its claim and observation),
    no enforced claim, no debit; main's ``uazapi_egress_disabled``."""

    from src.api.routes import webhook
    from src.services.egress_policy import EgressDisabledError

    async def closed(*_args: Any, **_kwargs: Any) -> Any:
        raise EgressDisabledError("uazapi_egress_disabled")

    uazapi_edges.answer = False
    async with _running_app(rule_pg, monkeypatch) as running:
        world, client = running.world, running.client
        await _under_the_rule(rule_direct, world)
        monkeypatch.setattr(webhook, "send_uazapi_text", closed)
        async with asyncio.timeout(_TURN_SECONDS):
            answer = await client.post(_UAZAPI, json=_text(world, "pg-uazapi-c-1"))
    assert answer.json() == {
        "ok": True,
        "replied": False,
        "reason": "uazapi_egress_disabled",
    }
    seen = await _rows(rule_direct, world)
    assert seen.claims == [("uazapi", "observational", "pg-uazapi-c-1")]
    assert seen.debits == [] and seen.traces == [] and seen.outbox == []
    assert seen.observed == [("unknown", False, 0)]


async def test_a_crashed_charged_uazapi_reply_is_recovered_once_by_the_publisher(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    uazapi_edges: _Egress,
    replay_store_down: Callable[[bool], None],
) -> None:
    """A charged Uazapi decision whose process died right after the final
    commit: the publisher (the route's real sender, its Uazapi branch)
    recovers the pending reply on a courtesy lot alone, once; the provider's
    redelivery (the replay store forgot it) is ``duplicate_message``."""

    from src.api.routes import webhook
    from src.repositories.database import get_db_session_factory

    _no_app_publisher(monkeypatch)
    monkeypatch.setattr(credit_metering, "PUBLISHER_MIN_AGE_SECONDS", 0)

    async def crashed(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the process died after the final commit")

    async with _running_app(rule_pg, monkeypatch) as running:
        world, client = running.world, running.client
        await _under_the_rule(rule_direct, world)
        body = _text(world, "pg-uazapi-recover-1")
        async with asyncio.timeout(_TURN_SECONDS):
            with monkeypatch.context() as patch:
                patch.setattr(credit_metering, "deliver_reply_inline", crashed)
                with pytest.raises(RuntimeError, match="the process died"):
                    await client.post(_UAZAPI, json=body)
            pending = await _rows(rule_direct, world)
            tick = await credit_metering.CreditResponsePublisher(
                webhook.CREDIT_REPLY_SENDER, session_factory=get_db_session_factory()
            ).run_once()
            replies = _uazapi_sent()
            replay_store_down(True)
            redelivered = await client.post(_UAZAPI, json=body)
        replies_after = _uazapi_sent()
    assert [state for state, _p in pending.outbox] == ["pending"]
    assert tick is not None and tick.enforced and tick.outcomes == ["delivered"]
    assert len(replies) == 1 and replies_after == replies
    assert redelivered.json() == {"ok": True, "skipped": "duplicate_message"}
    seen = await _rows(rule_direct, world)
    assert [mode for _s, mode, _p in seen.claims] == ["enforced"]
    assert seen.debits == [(1_000, "courtesy")]
    [(state, payload)] = seen.outbox
    assert state == "delivered" and "phone" not in payload
    assert await _stored_echo_ids(rule_direct, world) == [_REPLY_ID]


async def test_a_charged_uazapi_long_audio_hands_off_in_one_transaction(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    uazapi_edges: _Egress,
) -> None:
    """The long audio: never downloaded; its ``uazapi`` claim, the handoff
    record (with ``credit_effect_id``) and its one courtesy trace commit
    together; the notice goes out after."""

    from src.api.routes import webhook
    from src.services.handoff_service import HandoffService

    async def no_dispatch(*_args: Any, **_kwargs: Any) -> tuple[bool, None]:
        return False, None

    monkeypatch.setattr(HandoffService, "_dispatch_creation_webhook", no_dispatch)

    async def no_download(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("a long audio was downloaded")

    monkeypatch.setattr(webhook, "download_uazapi_media", no_download)
    async with _running_app(rule_pg, monkeypatch) as running:
        world, client = running.world, running.client
        await _under_the_rule(rule_direct, world)
        async with asyncio.timeout(_TURN_SECONDS):
            answer = await client.post(
                _UAZAPI, json=_long_audio(world, "pg-uazapi-long-1")
            )
        notices = _uazapi_sent()
    assert answer.json() == {"ok": True, "skipped": "audio_too_long"}, answer.text
    assert notices == [webhook._AUDIO_TOO_LONG_MSG_PT]
    seen = await _rows(rule_direct, world)
    assert seen.claims == [("uazapi", "enforced", "pg-uazapi-long-1")]
    [trace] = seen.traces
    [context] = seen.handoffs
    assert context["credit_effect_id"] == trace
    assert seen.debits == [(1_000, "courtesy")] and seen.observed == []


async def test_under_a_mark_the_uazapi_turn_stays_observational_on_postgres(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    uazapi_edges: _Egress,
) -> None:
    """DESIGN Decision 18: a marked builder with courtesy -- the mark decides
    first -- keeps Uazapi observational: main's claim and observation, no
    enforced claim, no debit, no egress question."""

    async with _running_app(rule_pg, monkeypatch) as running:
        world, client = running.world, running.client
        await _under_the_rule(rule_direct, world)
        await record_mark(rule_direct, UUID(world.builder))
        async with asyncio.timeout(_TURN_SECONDS):
            answer = await client.post(_UAZAPI, json=_text(world, "pg-uazapi-m-1"))
        replies = _uazapi_sent()
    assert answer.json() == {"ok": True, "replied": True}, answer.text
    assert len(replies) == 1 and uazapi_edges.reads == 0
    seen = await _rows(rule_direct, world)
    assert seen.claims == [("uazapi", "observational", "pg-uazapi-m-1")]
    assert seen.debits == [] and seen.traces == [] and seen.outbox == []
    assert seen.observed == [("unknown", False, 0)]


async def test_a_uazapi_tenant_relinked_before_its_claim_is_503_and_charges_nobody(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    uazapi_edges: _Egress,
) -> None:
    """DESIGN item 11 (``reparented_since_capture`` holds for ``courtesy``):
    the tenant moved to another builder -- with courtesy of its own --
    between the pre-check's commit and the claim: 503, no claim, and neither
    builder's courtesy is debited."""

    from tests.integration.test_credit_effective_enforcement_postgres import (
        _builder_with_api_tenant,
    )

    async with _running_app(rule_pg, monkeypatch) as running:
        world, client = running.world, running.client
        await _under_the_rule(rule_direct, world)
        other = await _builder_with_api_tenant(rule_direct)
        await grant_courtesy(rule_direct, other.builder, 5)
        real = credit_metering.precheck_uazapi
        moved = {"n": 0}

        async def then_relink(*args: Any, **kwargs: Any) -> Any:
            verdict = await real(*args, **kwargs)
            moved["n"] += 1
            await relink_tenant(rule_direct, UUID(world.uazapi_tenant), other.builder)
            return verdict

        monkeypatch.setattr(credit_metering, "precheck_uazapi", then_relink)
        async with asyncio.timeout(_TURN_SECONDS):
            answer = await client.post(_UAZAPI, json=_text(world, "pg-uazapi-moved-1"))
        replies = _uazapi_sent()
    assert moved == {"n": 1}
    assert answer.status_code == 503
    assert answer.json() == {"error": "credit_enforcement_unavailable"}
    assert replies == []
    seen = await _rows(rule_direct, world)
    assert seen.claims == [] and seen.debits == [] and seen.outbox == []
    async with rule_direct.connect() as connection:
        other_debits = await connection.scalar(
            text(
                "SELECT count(*) FROM credit_ledger_entries WHERE builder_id = :b "
                "AND entry_type <> 'grant'"
            ),
            {"b": other.builder},
        )
    assert other_debits == 0
