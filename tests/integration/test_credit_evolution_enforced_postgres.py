"""The Evolution adapter under the courtesy rule, on real PostgreSQL.

debito-cortesia-automatico Group 4 (DESIGN items 9, 10 and 11, Decisions 12,
16 and 18; wish Decision 7), through the real application (its lifespan,
middleware and pool, ``tests.integration.test_credit_metering_postgres
._running_app``) on a head database with neither ``credit_enforcement_at``
nor a mark: a courtesy grant alone puts the world's builder -- and so its
Evolution tenant -- under the rule.  Evolution has no origin proof (every
callback is ``unknown``) and no egress flag.  The LLM and the providers are
faked (``_fake_llm_and_providers``), the egress question must never be
asked, and the replay store is this process's memory (no Redis server is
touched).  Pinned:

* a charged turn is claimed in PostgreSQL (``evolution``, ``enforced``) on
  top of the Redis claim, charged ONE trace on the courtesy lot only, its
  reply sent inline from the outbox by the Evolution branch of the route's
  sender with its echo id (``key.id``) stored; every charged row's
  ``decision_digest`` carries ``unknown`` (the digest is recomputed from the
  stored claim and decision, never only spied);
* a redelivery of the same ``data.key.id`` neither debits again nor gives a
  second reply -- also across a mode change between the deliveries (the
  rule removed, the builder marked, a grant), with the replay store holding
  the first claim and, where it forgot it, with the PostgreSQL claim alone
  (the observational claim of a first free delivery included); in the
  charged -> free direction with the store down, the accepted residual of
  Decision 16: a second, free reply, never a second debit;
* two concurrent deliveries of one message (the replay store down) meet at
  the PostgreSQL claim: one decision, one debit, one reply;
* the race: the courtesy removed between the capture and the account lock
  is 503 with nothing written; the tenant moved to another builder between
  the pre-check and the claim is 503 and charges nobody;
* blocked: ``credit_suspended`` with nothing written, no session and
  nothing sent; with a paused session the text-only capture stays;
* a charged reply whose process died after the final commit is recovered
  by the publisher (a courtesy lot keeps it on) through the Evolution
  branch, once; its redelivery is ``duplicate_message``;
* the pipeline's own handoff is one trace charged in phase 1;
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
    pg,
)

pytestmark = pytest.mark.integration

__all__ = ["pg"]

_PHONE = "5511900041001"
_EVOLUTION = "/v1/webhook/evolution"
# The faked send's ``key.id`` (``_fake_llm_and_providers``).
_REPLY_ID = "credit-dominance-evolution-reply"
_REPLY = "Olá! Posso ajudar com o seu agendamento."
# The real digest function (the per-test spy below wraps the module's).
_REAL_DIGEST = credit_metering.decision_digest


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


@pytest.fixture
def evolution_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    """No egress question (Evolution has no egress flag: asking one fails the
    test) and no Redis server: the replay claim and the echo store are this
    process's memory."""

    from src.api.routes import webhook

    async def question() -> bool:
        raise AssertionError("an egress flag was read for Evolution")

    monkeypatch.setattr(egress_policy, "uazapi_egress_open", question)
    monkeypatch.setattr(webhook, "_get_redis", lambda: None)


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


def _text(
    world: Any, message_id: str, *, phone: str = _PHONE, content: str | None = None
) -> Any:
    return {
        "event": "messages.upsert",
        "instance": world.evolution_instance,
        "data": {
            "key": {
                "remoteJid": f"{phone}@s.whatsapp.net",
                "fromMe": False,
                "id": message_id,
            },
            "message": {"conversation": content or "Olá, quero agendar"},
            "pushName": "Paciente",
        },
    }


async def _under_the_rule(direct: AsyncEngine, world: Any, credits: int = 10) -> UUID:
    """The world's builder -- its Evolution tenant's (the shared world seeds
    it under the builder) -- under the rule by a courtesy grant; returns the
    courtesy lot."""

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
    tenant = UUID(world.evolution_tenant)
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


def _evolution_sent(phone: str = _PHONE) -> list[str]:
    """What the faked ``send_evolution_text(url, name, key, phone, text)``
    sent to ``phone``."""

    from src.api.routes import webhook

    return [
        call.args[4]
        for call in webhook.send_evolution_text.await_args_list  # type: ignore[attr-defined]
        if call.args[3] == phone
    ]


async def _stored_echo_ids(direct: AsyncEngine, world: Any) -> list[str]:
    async with direct.connect() as connection:
        meta = (
            await connection.execute(
                text(
                    "SELECT metadata FROM sessions WHERE tenant_id = :t "
                    "AND user_id = :p"
                ),
                {"t": UUID(world.evolution_tenant), "p": _PHONE},
            )
        ).scalar_one()
    return [entry[0] for entry in (meta or {}).get("ai_msg_ids", [])]


@dataclass
class _Digests:
    """Every ``decision_digest`` the module computed: its keywords and its
    value."""

    calls: list[tuple[dict[str, Any], str]]


@pytest.fixture(autouse=True)
def digests(monkeypatch: pytest.MonkeyPatch) -> _Digests:
    seen = _Digests(calls=[])
    original = credit_metering.decision_digest

    def spy(**kwargs: Any) -> str:
        value = original(**kwargs)
        seen.calls.append((dict(kwargs), value))
        return value

    monkeypatch.setattr(credit_metering, "decision_digest", spy)
    return seen


async def _charged_digests_are_unknown(
    direct: AsyncEngine, world: Any, digests: _Digests
) -> int:
    """The "Origem real" criterion (DESIGN item 10): every charged Evolution row
    (``credit_effects`` of the tenant) stores the digest the module computed
    with origin ``unknown`` -- and recomputing that very decision as
    ``production`` gives another digest, so the stored value pins the
    origin.  Returns how many rows were checked."""

    from src.interaction_context import InteractionOrigin

    async with direct.connect() as connection:
        stored = (
            await connection.execute(
                text(
                    "SELECT effect_id, decision_digest FROM credit_effects "
                    "WHERE tenant_id = :t ORDER BY effect_id"
                ),
                {"t": UUID(world.evolution_tenant)},
            )
        ).all()
    assert digests.calls, "no decision digest was computed"
    assert {kwargs["origin"] for kwargs, _ in digests.calls} == {
        InteractionOrigin.UNKNOWN
    }
    by_value = {value: kwargs for kwargs, value in digests.calls}
    for effect_id, digest in stored:
        assert digest in by_value, (effect_id, digest)
        as_production = _REAL_DIGEST(
            **{**by_value[digest], "origin": InteractionOrigin.PRODUCTION}
        )
        assert as_production != digest
    return len(stored)


async def test_a_charged_evolution_turn_is_claimed_and_charged_once_on_postgres(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    evolution_edges: None,
    digests: _Digests,
) -> None:
    """One enforced ``evolution`` claim (on top of the Redis one), one trace
    on the courtesy lot (never a paid lot, never debt), the reply sent inline
    from the outbox -- ``delivered``, its PII erased, its echo id stored --
    and ``unknown`` in the digest of every charged row; the redelivery is
    ``duplicate_message``."""

    async with _running_app(rule_pg, monkeypatch) as running:
        world, client = running.world, running.client
        await _under_the_rule(rule_direct, world)
        body = _text(world, "pg-evolution-charged-1")
        async with asyncio.timeout(_TURN_SECONDS):
            answer = await client.post(_EVOLUTION, json=body)
            again = await client.post(_EVOLUTION, json=body)
        replies = _evolution_sent()
    assert answer.json() == {"ok": True, "replied": True}, answer.text
    assert again.json() == {"ok": True, "skipped": "duplicate_message"}
    assert replies == [_REPLY]
    seen = await _rows(rule_direct, world)
    assert seen.claims == [("evolution", "enforced", "pg-evolution-charged-1")]
    assert len(seen.traces) == 1 and seen.debits == [(1_000, "courtesy")]
    assert seen.debts == 0 and seen.observed == []
    [(state, payload)] = seen.outbox
    assert state == "delivered" and payload["channel"] == "evolution"
    assert "phone" not in payload and "text" not in payload
    assert not [k for k in payload if "token" in k or "url" in k or "api" in k]
    assert await _stored_echo_ids(rule_direct, world) == [_REPLY_ID]
    assert await _charged_digests_are_unknown(rule_direct, world, digests) == 1


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
    evolution_edges: None,
    replay_store_down: Callable[[bool], None],
    change: str,
    store_down: bool,
) -> None:
    """DESIGN Decision 16, without a live redelivery: the same ``data.key.id``
    posted twice with the mode changed in between.  The replay store's claim
    (kept on every turn, where it was) answers the redelivery whatever the
    new mode; where the store forgot it, the PostgreSQL claim of the first
    delivery -- enforced, or the observational one of a free first delivery
    -- answers it for a charged second capture, and a blocked one is
    ``credit_suspended``: never a second debit, never a second reply.  (The
    charged -> free direction with the store down is the residual below.)"""

    async with _running_app(rule_pg, monkeypatch) as running:
        world, client = running.world, running.client
        lot = None
        if change != "first_free_then_granted":
            lot = await _under_the_rule(rule_direct, world)
        body = _text(world, f"pg-evolution-redelivered-{change}-{store_down}")
        async with asyncio.timeout(_TURN_SECONDS):
            first = await client.post(_EVOLUTION, json=body)
            if change == "removed":
                assert lot is not None
                await remove_courtesy(rule_direct, lot)
            elif change == "marked":
                await record_mark(rule_direct, UUID(world.builder))
            else:
                await grant_courtesy(rule_direct, UUID(world.builder), 5)
            replay_store_down(store_down)
            second = await client.post(_EVOLUTION, json=body)
        replies = _evolution_sent()
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
        assert [state for state, _p in seen.outbox] == ["delivered"]
    assert seen.debts == 0


@pytest.mark.parametrize("change", ["marked", "removed_and_marked"])
async def test_charged_then_free_with_the_replay_store_down_is_the_accepted_residual(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    evolution_edges: None,
    replay_store_down: Callable[[bool], None],
    change: str,
) -> None:
    """DOCUMENTS the accepted residual of DESIGN Decision 16 on Evolution, as
    on Uazapi; it pins today's behaviour, it does not ask for a change.  The
    first delivery charged under the rule, then the builder marked (with or
    without the courtesy removed) so the redelivery is Evolution's free
    observational path: the dedupe rests on the Redis replay claim alone (the
    free path writes its observational claim only after the reply), so with
    the store down the redelivery is answered again, free -- a second reply
    but never a second debit; the free turn's observational claim conflicts
    with the enforced one and reuses it (no second claim row), its
    observation is ``unknown`` and free, and the one outbox row is the
    charged reply's."""

    async with _running_app(rule_pg, monkeypatch) as running:
        world, client = running.world, running.client
        lot = await _under_the_rule(rule_direct, world)
        body = _text(world, f"pg-evolution-charged-then-free-{change}")
        async with asyncio.timeout(_TURN_SECONDS):
            first = await client.post(_EVOLUTION, json=body)
            if change == "removed_and_marked":
                await remove_courtesy(rule_direct, lot)
            await record_mark(rule_direct, UUID(world.builder))
            replay_store_down(True)
            second = await client.post(_EVOLUTION, json=body)
        replies = _evolution_sent()
    assert first.json() == {"ok": True, "replied": True}, first.text
    assert second.json() == {"ok": True, "replied": True}, second.text
    assert len(replies) == 2  # the residual: the free redelivery replies again
    seen = await _rows(rule_direct, world)
    assert [mode for _s, mode, _p in seen.claims] == ["enforced"]
    assert seen.debits == [(1_000, "courtesy")] and len(seen.traces) == 1
    assert seen.debts == 0
    assert seen.observed == [("unknown", False, 0)]
    assert [state for state, _p in seen.outbox] == ["delivered"]


async def test_two_concurrent_deliveries_of_one_evolution_message_charge_once(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    evolution_edges: None,
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
        await _seed_patient(rule_pg.url, world.evolution_tenant, _PHONE)
        body = _text(world, "pg-evolution-race-1")
        async with asyncio.timeout(_TURN_SECONDS):
            tasks = [
                asyncio.create_task(client.post(_EVOLUTION, json=body))
                for _ in range(2)
            ]
            await both.reached.wait()
            both.opened.set()
            answers = await asyncio.gather(*tasks)
        replies = _evolution_sent()
    assert sorted(json.dumps(a.json(), sort_keys=True) for a in answers) == sorted(
        [
            json.dumps({"ok": True, "replied": True}, sort_keys=True),
            json.dumps({"ok": True, "skipped": "duplicate_message"}, sort_keys=True),
        ]
    )
    assert sum(row is not None for row in inserted) == 1
    seen = await _rows(rule_direct, world)
    assert seen.claims == [("evolution", "enforced", "pg-evolution-race-1")]
    assert seen.debits == [(1_000, "courtesy")] and len(replies) == 1


async def test_the_courtesy_removed_between_capture_and_lock_is_503_with_nothing_written(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    evolution_edges: None,
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
            answer = await client.post(
                _EVOLUTION, json=_text(world, "pg-evolution-lock-1")
            )
        replies = _evolution_sent()
    assert answer.status_code == 503
    assert answer.json() == {"error": "credit_enforcement_unavailable"}
    assert replies == []
    seen = await _rows(rule_direct, world)
    assert seen.claims == [] and seen.debits == [] and seen.sessions == 0
    assert seen.outbox == [] and seen.observed == []


async def test_a_blocked_evolution_builder_writes_nothing_but_the_paused_capture(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    evolution_edges: None,
) -> None:
    """Blocked (the courtesy spent, no paid plan): ``credit_suspended`` with
    no session, claim, effect or send; with a paused session the text-only
    capture stays for the team."""

    from sqlalchemy.ext.asyncio import AsyncSession

    from src.models.session import Session

    async with _running_app(rule_pg, monkeypatch) as running:
        world, client = running.world, running.client
        lot = await _under_the_rule(rule_direct, world)
        await remove_courtesy(rule_direct, lot)
        async with asyncio.timeout(_TURN_SECONDS):
            blocked = await client.post(
                _EVOLUTION, json=_text(world, "pg-evolution-b-1")
            )
        paused_phone = "5511900041002"
        async with AsyncSession(rule_direct) as session, session.begin():
            session.add(
                Session(
                    tenant_id=UUID(world.evolution_tenant),
                    user_id=paused_phone,
                    channel="whatsapp",
                    cognitive_state={},
                    session_metadata={},
                    is_paused=True,
                )
            )
        async with asyncio.timeout(_TURN_SECONDS):
            paused = await client.post(
                _EVOLUTION, json=_text(world, "pg-evolution-b-2", phone=paused_phone)
            )
        replies = _evolution_sent() + _evolution_sent(paused_phone)
    assert blocked.json() == {"ok": True, "skipped": "credit_suspended"}
    assert paused.status_code == 202
    assert paused.json() == {"ok": True, "skipped": "session_paused"}
    assert replies == []
    seen = await _rows(rule_direct, world)
    assert seen.claims == [] and seen.traces == [] and seen.sessions == 0
    assert seen.observed == [] and seen.outbox == []
    async with rule_direct.connect() as connection:
        captured = (
            await connection.execute(
                text(
                    "SELECT event_data ->> 'content' FROM events WHERE tenant_id = :t "
                    "AND metadata ->> 'paused_capture' = 'true'"
                ),
                {"t": UUID(world.evolution_tenant)},
            )
        ).scalars()
        assert list(captured) == ["Olá, quero agendar"]


async def test_a_crashed_charged_evolution_reply_is_recovered_once_by_the_publisher(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    evolution_edges: None,
    replay_store_down: Callable[[bool], None],
) -> None:
    """A charged Evolution decision whose process died right after the final
    commit: the publisher (the route's real sender, its Evolution branch)
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
        body = _text(world, "pg-evolution-recover-1")
        async with asyncio.timeout(_TURN_SECONDS):
            with monkeypatch.context() as patch:
                patch.setattr(credit_metering, "deliver_reply_inline", crashed)
                with pytest.raises(RuntimeError, match="the process died"):
                    await client.post(_EVOLUTION, json=body)
            pending = await _rows(rule_direct, world)
            tick = await credit_metering.CreditResponsePublisher(
                webhook.CREDIT_REPLY_SENDER, session_factory=get_db_session_factory()
            ).run_once()
            replies = _evolution_sent()
            replay_store_down(True)
            redelivered = await client.post(_EVOLUTION, json=body)
        replies_after = _evolution_sent()
    assert [state for state, _p in pending.outbox] == ["pending"]
    assert tick is not None and tick.enforced and tick.outcomes == ["delivered"]
    assert replies == [_REPLY] and replies_after == replies
    assert redelivered.json() == {"ok": True, "skipped": "duplicate_message"}
    seen = await _rows(rule_direct, world)
    assert [mode for _s, mode, _p in seen.claims] == ["enforced"]
    assert seen.debits == [(1_000, "courtesy")]
    [(state, payload)] = seen.outbox
    assert state == "delivered" and "phone" not in payload
    assert await _stored_echo_ids(rule_direct, world) == [_REPLY_ID]


async def test_a_charged_evolution_ai_handoff_is_one_trace_charged_in_phase1(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    evolution_edges: None,
    digests: _Digests,
) -> None:
    """The pipeline's own handoff (phase 1) on Evolution under the rule: its
    ``evolution`` claim, the handoff record (with ``credit_effect_id``) and
    its one courtesy trace commit in phase 1; the reply goes out once and
    the digest is ``unknown``."""

    from src.services.handoff_service import HandoffService
    from tests.services.test_credit_metering import _HANDOFF_TEXT

    async def no_dispatch(*_args: Any, **_kwargs: Any) -> tuple[bool, None]:
        return False, None

    monkeypatch.setattr(HandoffService, "_dispatch_creation_webhook", no_dispatch)
    async with _running_app(rule_pg, monkeypatch) as running:
        world, client = running.world, running.client
        await _under_the_rule(rule_direct, world)
        async with asyncio.timeout(_TURN_SECONDS):
            answer = await client.post(
                _EVOLUTION,
                json=_text(world, "pg-evolution-handoff-1", content=_HANDOFF_TEXT),
            )
        replies = _evolution_sent()
    assert answer.json() == {"ok": True, "replied": True}, answer.text
    assert len(replies) == 1
    seen = await _rows(rule_direct, world)
    assert seen.claims == [("evolution", "enforced", "pg-evolution-handoff-1")]
    [trace] = seen.traces
    [context] = seen.handoffs
    assert context["credit_effect_id"] == trace
    assert seen.debits == [(1_000, "courtesy")] and seen.observed == []
    assert await _charged_digests_are_unknown(rule_direct, world, digests) == 1


async def test_under_a_mark_the_evolution_turn_stays_observational_on_postgres(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    evolution_edges: None,
) -> None:
    """DESIGN Decision 18: a marked builder with courtesy -- the mark decides
    first -- keeps Evolution observational: main's claim and observation, no
    enforced claim, no debit."""

    async with _running_app(rule_pg, monkeypatch) as running:
        world, client = running.world, running.client
        await _under_the_rule(rule_direct, world)
        await record_mark(rule_direct, UUID(world.builder))
        async with asyncio.timeout(_TURN_SECONDS):
            answer = await client.post(
                _EVOLUTION, json=_text(world, "pg-evolution-m-1")
            )
        replies = _evolution_sent()
    assert answer.json() == {"ok": True, "replied": True}, answer.text
    assert len(replies) == 1
    seen = await _rows(rule_direct, world)
    assert seen.claims == [("evolution", "observational", "pg-evolution-m-1")]
    assert seen.debits == [] and seen.traces == [] and seen.outbox == []
    assert seen.observed == [("unknown", False, 0)]


async def test_an_evolution_tenant_relinked_before_its_claim_is_503_and_charges_nobody(
    rule_pg: _PG,
    rule_direct: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
    evolution_edges: None,
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
        real = credit_metering.precheck_evolution
        moved = {"n": 0}

        async def then_relink(*args: Any, **kwargs: Any) -> Any:
            verdict = await real(*args, **kwargs)
            moved["n"] += 1
            await relink_tenant(
                rule_direct, UUID(world.evolution_tenant), other.builder
            )
            return verdict

        monkeypatch.setattr(credit_metering, "precheck_evolution", then_relink)
        async with asyncio.timeout(_TURN_SECONDS):
            answer = await client.post(
                _EVOLUTION, json=_text(world, "pg-evolution-moved-1")
            )
        replies = _evolution_sent()
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
