"""Event-service <-> context-episode wiring tests (wish Group 2, chunk 5).

Covers the remaining 4 Group 2 acceptance criteria — the ones that exercise
``EventService``/the webhook and session-service reactivation entrypoints
end-to-end, rather than the bare CAS primitives:

* ``AC1``       — phase boundary: blocks ``_save_events_to_store`` (Phase 2)
                  entirely and proves the pause + ``prior_episode_context``
                  are durable from Phase 1 alone — the reactivated session
                  still "finds" the trigger message even though its
                  ``message.in`` event was never persisted.
* ``AC6``       — ``lead_name`` wiring through the real ``EventService``/SRM
                  pipeline (no SRM stage mocked): a self-identifying message
                  creates the slot; a non-identifying message neither creates
                  nor alters it.
* ``AC8`` / A1  — the ORM identity-map staleness proof: a full pipeline turn
                  run on the SAME ``db``/session object immediately after
                  ``_check_pause_state`` reactivates it, with no reopen,
                  lands on the post-CAS baseline (not the stale pre-CAS one).
* ``AC9`` / A2  — no ORM-pending ``cognitive_state`` write survives a
                  follow-on flush, in the same transaction, after either the
                  pause or the reactivation CAS.

Sibling chunk ``tests/services/test_session_pause_transition.py`` owns
AC2/AC3/AC4/AC5/AC7/AC10/AC11 (the bare CAS-primitive contract tests);
nothing here duplicates them. ``tests/api/test_webhook_uazapi.py`` owns AC12.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select

from src.core import pipeline as pipeline_module
from src.core.pipeline import CognitivePipeline
from src.models.session import Session
from src.repositories.session import SessionRepository
from src.services import event_service as event_service_module
from src.services.event_service import EventService

# ============================================================
# Shared fixtures / helpers
# ============================================================


class _NoOpHandoffStub:
    """Persistence-free handoff double used only by the AC1 scenario.

    The pause branch under test consults the ``initiate_handoff`` return
    value exclusively on the ``verification_status == "link_sent"`` path,
    which this file's forced SRM decision never reaches, so ``None`` is a
    valid, side-effect-free substitute for the real DB/webhook-backed
    ``HandoffService``.
    """

    async def initiate_handoff(
        self, request: Any, *, context: Any = None, session: Any = None
    ) -> None:
        return None


def _session(
    tenant_id: UUID,
    *,
    session_id: UUID | None = None,
    user_id: str | None,
    activity: datetime,
    paused: bool = False,
    paused_until: datetime | None = None,
    active: bool = True,
    cognitive_state: dict[str, Any] | None = None,
) -> Session:
    """Mirror ``tests/services/test_session_pause_transition.py:_session``."""
    return Session(
        id=session_id or uuid4(),
        tenant_id=tenant_id,
        user_id=user_id,
        channel="whatsapp",
        cognitive_state=cognitive_state if cognitive_state is not None else {},
        session_metadata={},
        last_activity_at=activity,
        is_active=active,
        is_paused=paused,
        paused_until=paused_until,
    )


def _seed(client: TestClient, *, tenant_id: UUID, sessions: list[Session]) -> None:
    from src.models.tenant import Tenant
    from src.repositories.database import get_db_session

    async def _run() -> None:
        db = get_db_session()
        try:
            db.add(
                Tenant(
                    id=tenant_id,
                    name=f"Clinic {tenant_id}",
                    slug=f"clinic-{tenant_id}",
                    api_key_hash=f"hash-{tenant_id}",
                    config={},
                )
            )
            db.add_all(sessions)
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]


def _read_session_row(client: TestClient, session_id: UUID) -> dict[str, Any]:
    """Fetch a session row by id from a fresh ``AsyncSession``."""
    from src.repositories.database import get_db_session

    async def _run() -> dict[str, Any]:
        db = get_db_session()
        try:
            row = await db.get(Session, session_id)
            assert row is not None, "session row vanished unexpectedly"
            return {
                "cognitive_state": dict(row.cognitive_state or {}),
                "is_paused": bool(row.is_paused),
                "paused_until": row.paused_until,
            }
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined,no-any-return]


def _count_message_in_events(client: TestClient, session_id: UUID) -> int:
    """Count persisted ``message.in`` event-store rows for a session."""
    from src.models.event import Event
    from src.repositories.database import get_db_session

    async def _run() -> int:
        db = get_db_session()
        try:
            rows = (
                (
                    await db.execute(
                        select(Event).where(
                            Event.session_id == session_id,
                            Event.event_type == "message.in",
                        )
                    )
                )
                .scalars()
                .all()
            )
            return len(rows)
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined,no-any-return]


# ============================================================
# AC1 — phase boundary: pause/context survive without message.in persisted
# ============================================================


def test_ac1_reactivation_finds_trigger_message_without_message_in_persisted(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Block Phase 2 entirely, run only Phase 1, then reactivate.

    Simulates a worker crashing right after the ``run_pipeline`` commit and
    before ``finish_response``/``_save_events_to_store`` ever executes. The
    pause and its ``prior_episode_context`` must already be durable from
    Phase 1 alone, and ``SessionService.reactivate_session`` (the manual
    reactivation entrypoint, since the default handoff policy pauses
    forever) must still recover the trigger message's text — composed
    synchronously inside ``CognitivePipeline.process`` — even though no
    ``message.in`` row for it was ever written.
    """
    from src.api.schemas.events import EventPayload, EventRequest, EventType
    from src.repositories.database import get_db_session
    from src.services.session_service import SessionService

    monkeypatch.setattr(
        event_service_module, "_open_redis", AsyncMock(return_value=None)
    )
    # "bloqueia _save_events_to_store": any call is a hard failure — Phase 2
    # must never run in this scenario.
    monkeypatch.setattr(
        EventService,
        "_save_events_to_store",
        AsyncMock(side_effect=AssertionError("phase 2 must never run")),
    )

    tenant_id = uuid4()
    session_id = uuid4()
    phone = "5511988880000"
    trigger_text = "Quero falar com um atendente humano agora, por favor"
    now = datetime.now(timezone.utc)
    session = _session(
        tenant_id,
        session_id=session_id,
        user_id=phone,
        activity=now,
        paused=False,
        paused_until=None,
    )
    _seed(client, tenant_id=tenant_id, sessions=[session])

    pipeline = CognitivePipeline(handoff_service=_NoOpHandoffStub())
    forced_decision = pipeline_module.SRMDecisionModel(
        intent="atendimento_humano",
        intent_category="information",
        funnel_stage="em_qualificacao",
        action="handoff_administrativo",
        confidence=0.99,
        handoff_required=True,
        handoff_reason="user_request",
    )
    monkeypatch.setattr(pipeline, "_run_srm", AsyncMock(return_value=forced_decision))

    async def _phase1_only() -> bool:
        db = get_db_session()
        try:
            service = EventService(pipeline=pipeline)
            request = EventRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                event_type=EventType.MESSAGE_IN,
                payload=EventPayload(
                    content=trigger_text, channel="whatsapp", user_id=phone
                ),
            )
            result, _clinic_config, _history = await service.run_pipeline(
                request, session=db
            )
            await db.commit()
            # Phase 2 (finish_response) is intentionally never invoked here.
            return result.should_pause
        finally:
            await db.close()

    should_pause = client._submit(_phase1_only())  # type: ignore[attr-defined]
    assert should_pause is True

    paused_row = _read_session_row(client, session_id)
    assert paused_row["is_paused"] is True
    assert paused_row["paused_until"] is None  # default policy: PAUSE_FOREVER
    prior_ctx = paused_row["cognitive_state"]["prior_episode_context"]
    assert prior_ctx is not None
    assert trigger_text in prior_ctx["summary"], (
        "the trigger message must be captured in prior_episode_context during "
        "Phase 1, independent of whether Phase 2 ever persists message.in"
    )
    assert _count_message_in_events(client, session_id) == 0, (
        "message.in must never have been persisted — Phase 2 was blocked "
        "and never called"
    )

    async def _reactivate() -> str:
        db = get_db_session()
        try:
            response = await SessionService().reactivate_session(
                tenant_id, session_id, db
            )
            await db.commit()
            return response.status
        finally:
            await db.close()

    status = client._submit(_reactivate())  # type: ignore[attr-defined]
    assert status == "reactivated"

    final_row = _read_session_row(client, session_id)
    assert final_row["is_paused"] is False
    final_ctx = final_row["cognitive_state"]["prior_episode_context"]
    assert trigger_text in final_ctx["summary"], (
        "the trigger message must still be recoverable after reactivation — "
        "found via the already-composed prior_episode_context, never via a "
        "message.in row that was never written"
    )


# ============================================================
# AC6 — lead_name wiring through the real EventService + SRM pipeline
# ============================================================


def test_ac6_lead_name_slot_created_only_on_self_identification(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Real ``EventService``/SRM flow — no pipeline stage is mocked here.

    * "Meu nome é Ana" on a session with no prior ``lead_name`` slot must
      result in ``cognitive_state.slots.lead_name == "Ana"`` after the normal
      ``_persist_session_state`` patch.
    * A message with no self-identification must neither create the slot on
      a fresh session nor alter it on a session that already carries one.
    """
    from src.api.schemas.events import EventPayload, EventRequest, EventType
    from src.repositories.database import get_db_session

    monkeypatch.setattr(
        event_service_module, "_open_redis", AsyncMock(return_value=None)
    )

    tenant_id = uuid4()
    now = datetime.now(timezone.utc)
    session_creates = _session(
        tenant_id, user_id="5511900000001", activity=now, paused=False
    )
    session_no_create = _session(
        tenant_id, user_id="5511900000002", activity=now, paused=False
    )
    session_no_alter = _session(
        tenant_id,
        user_id="5511900000003",
        activity=now,
        paused=False,
        cognitive_state={"slots": {"lead_name": "Carlos"}},
    )
    _seed(
        client,
        tenant_id=tenant_id,
        sessions=[session_creates, session_no_create, session_no_alter],
    )

    async def _send(session_id: UUID, phone: str, content: str) -> None:
        db = get_db_session()
        try:
            service = EventService()  # real pipeline, real SRM — nothing mocked
            request = EventRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                event_type=EventType.MESSAGE_IN,
                payload=EventPayload(
                    content=content, channel="whatsapp", user_id=phone
                ),
            )
            await service.run_pipeline(request, session=db)
            await db.commit()
        finally:
            await db.close()

    client._submit(  # type: ignore[attr-defined]
        _send(session_creates.id, "5511900000001", "Meu nome é Ana")
    )
    client._submit(  # type: ignore[attr-defined]
        _send(session_no_create.id, "5511900000002", "Oi, bom dia")
    )
    client._submit(  # type: ignore[attr-defined]
        _send(session_no_alter.id, "5511900000003", "Oi, bom dia")
    )

    creates_slots = _read_session_row(client, session_creates.id)["cognitive_state"][
        "slots"
    ]
    no_create_slots = _read_session_row(client, session_no_create.id)[
        "cognitive_state"
    ]["slots"]
    no_alter_slots = _read_session_row(client, session_no_alter.id)["cognitive_state"][
        "slots"
    ]

    assert creates_slots["lead_name"] == "Ana"
    assert "lead_name" not in no_create_slots
    assert no_alter_slots["lead_name"] == "Carlos"


# ============================================================
# AC8 / A1 — identity-map staleness proof across a real reactivation + turn
# ============================================================


def test_ac8_full_turn_after_check_pause_state_reactivation_uses_post_cas_state(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run a full pipeline turn on the SAME ``db``/session object, in the
    same request, immediately after ``_check_pause_state`` auto-reactivates
    the session — never reopening the session or the ORM object.

    If the identity-mapped object had NOT been refreshed post-CAS (an A1
    violation), the stale pre-CAS ``turn_count`` (7) would still be resident
    on the Python object and the subsequent turn would land on 8, not 1. The
    FINAL assertion (``turn_count == 1``) is therefore a stronger proof than
    checking the immediate post-reactivation value alone — it proves the
    *entire* downstream turn built on the post-CAS baseline.
    """
    from src.api.routes.webhook import _check_pause_state
    from src.api.schemas.events import EventPayload, EventRequest, EventType
    from src.repositories.database import get_db_session

    monkeypatch.setattr(
        event_service_module, "_open_redis", AsyncMock(return_value=None)
    )

    tenant_id = uuid4()
    session_id = uuid4()
    phone = "5511999990000"
    now = datetime.now(timezone.utc)
    stale_state = {
        "funnel_stage": "agendamento_solicitado",
        "turn_count": 7,
        "slots": {"specialty": "dermatologia"},
        "intent_history": ["agendamento"],
        "flags": {},
        "safety_triggers": 0,
        "last_intent": "agendamento",
    }
    session = _session(
        tenant_id,
        session_id=session_id,
        user_id=phone,
        activity=now - timedelta(hours=2),
        paused=True,
        paused_until=now - timedelta(minutes=5),  # already expired -> auto path
        cognitive_state=stale_state,
    )
    _seed(client, tenant_id=tenant_id, sessions=[session])

    captured: dict[str, Any] = {}

    async def _run() -> None:
        db = get_db_session()
        try:
            action = await _check_pause_state(tenant_id, phone, db, now=now)
            assert (
                action is None
            ), "expired pause must auto-reactivate and continue processing"

            repo = SessionRepository(db)
            reloaded = await repo.get_by_id(session_id)
            assert reloaded is not None, "session row vanished unexpectedly"
            post_reactivation = reloaded.get_cognitive_state()
            captured["turn_count_post_reactivation"] = post_reactivation.turn_count
            captured["slots_post_reactivation"] = dict(post_reactivation.slots)

            service = EventService()  # real pipeline, real SRM
            request = EventRequest(
                tenant_id=tenant_id,
                session_id=session_id,
                event_type=EventType.MESSAGE_IN,
                payload=EventPayload(
                    content="Oi, quero continuar", channel="whatsapp", user_id=phone
                ),
            )
            # Same `db`/session the webhook just reactivated — never reopened,
            # mirroring the production request-scoped session lifecycle.
            await service.run_pipeline(request, session=db)
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]

    assert captured["turn_count_post_reactivation"] == 0
    assert captured["slots_post_reactivation"] == {}

    final_row = _read_session_row(client, session_id)
    assert final_row["cognitive_state"]["turn_count"] == 1
    assert final_row["cognitive_state"]["funnel_stage"] != "agendamento_solicitado"
    assert final_row["cognitive_state"]["slots"] == {}


# ============================================================
# AC9 / A2 — no pending ORM cognitive_state write survives a follow-on flush
# ============================================================


def test_ac9_pause_cas_no_pending_cognitive_state_survives_followon_flush(
    client: TestClient,
) -> None:
    """Prove the pause branch's single-writer discipline (A2).

    ``_persist_session_state``'s pause branch computes ``cognitive.to_dict()``
    and passes it DIRECTLY as the CAS ``.values()`` — it never assigns
    ``session_row.cognitive_state = ...`` on the identity-mapped ORM object.
    This test exercises exactly that discipline against the raw repository
    call and proves a follow-on flush in the same transaction (triggered by
    an unrelated attribute write, mirroring the real
    ``session_row.increment_message_count()`` call right after the CAS)
    cannot silently reintroduce a stale value — there is nothing pending to
    reintroduce.
    """
    tenant_id = uuid4()
    session_id = uuid4()
    now = datetime.now(timezone.utc)
    initial_state = {"slots": {"stale": "pre-cas-value"}, "funnel_stage": "x"}
    session = _session(
        tenant_id,
        session_id=session_id,
        user_id="5511900000009",
        activity=now,
        paused=False,
        paused_until=None,
        cognitive_state=initial_state,
    )
    _seed(client, tenant_id=tenant_id, sessions=[session])

    candidate_state = {"slots": {"fresh": "cas-value"}, "funnel_stage": "y"}
    captured: dict[str, Any] = {}

    async def _run() -> None:
        from src.repositories.database import get_db_session

        db = get_db_session()
        try:
            repo = SessionRepository(db)
            row = await repo.get_by_id(session_id)
            assert row is not None, "session row vanished unexpectedly"
            captured["baseline_dirty"] = sa_inspect(
                row
            ).attrs.cognitive_state.history.has_changes()

            outcome = await repo.pause_effective(
                tenant_id,
                session_id,
                cognitive_state=candidate_state,
                paused_until=None,
            )
            captured["outcome"] = outcome
            # No Python assignment to row.cognitive_state ever happened — the
            # attribute must still report zero pending changes, even though
            # the raw CAS UPDATE just ran underneath this very ORM object.
            captured["dirty_immediately_after_cas"] = sa_inspect(
                row
            ).attrs.cognitive_state.history.has_changes()

            await db.refresh(
                row, attribute_names=["cognitive_state", "is_paused", "paused_until"]
            )
            captured["row_after_refresh"] = dict(row.cognitive_state)
            captured["dirty_after_refresh"] = sa_inspect(
                row
            ).attrs.cognitive_state.history.has_changes()

            # The production pause branch calls increment_message_count() and
            # flushes right after the refresh — reproduce that follow-on
            # flush here and prove cognitive_state is untouched by it.
            row.increment_message_count()
            await db.flush()
            captured["dirty_after_followon_flush"] = sa_inspect(
                row
            ).attrs.cognitive_state.history.has_changes()

            db_value = (
                await db.execute(
                    select(Session.cognitive_state).where(Session.id == session_id)
                )
            ).scalar_one()
            captured["db_value_after_followon_flush"] = dict(db_value)
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]

    assert captured["baseline_dirty"] is False
    assert captured["outcome"] == "paused"
    assert captured["dirty_immediately_after_cas"] is False
    assert captured["row_after_refresh"] == candidate_state
    assert captured["dirty_after_refresh"] is False
    assert captured["dirty_after_followon_flush"] is False
    assert captured["db_value_after_followon_flush"] == candidate_state


def test_ac9_reactivation_cas_no_pending_cognitive_state_survives_followon_flush(
    client: TestClient,
) -> None:
    """Same A2 proof, for the reactivation branch's single-writer discipline.

    ``_check_pause_state``'s auto-reactivation (and ``SessionService.
    reactivate_session``'s manual path) compute ``projected.to_dict()`` and
    pass it straight to the repository CAS — never pre-assigning
    ``session.cognitive_state`` on the identity-mapped object either.
    """
    tenant_id = uuid4()
    session_id = uuid4()
    episode_id = uuid4()
    now = datetime.now(timezone.utc)
    initial_state = {"slots": {"stale": "pre-reactivation"}, "funnel_stage": "x"}
    session = _session(
        tenant_id,
        session_id=session_id,
        user_id="5511900000010",
        activity=now,
        paused=True,
        paused_until=None,
        cognitive_state=initial_state,
    )
    _seed(client, tenant_id=tenant_id, sessions=[session])

    candidate_state = {
        "slots": {},
        "funnel_stage": "lead_novo",
        "context_episode_id": str(episode_id),
    }
    captured: dict[str, Any] = {}

    async def _run() -> None:
        from src.repositories.database import get_db_session

        db = get_db_session()
        try:
            repo = SessionRepository(db)
            row = await repo.get_by_id(session_id)
            assert row is not None, "session row vanished unexpectedly"
            captured["baseline_dirty"] = sa_inspect(
                row
            ).attrs.cognitive_state.history.has_changes()

            outcome = await repo.reactivate_effective_manual(
                tenant_id=tenant_id,
                session_id=session_id,
                new_cognitive_state=candidate_state,
                new_episode_id=episode_id,
            )
            captured["outcome"] = outcome
            captured["dirty_immediately_after_cas"] = sa_inspect(
                row
            ).attrs.cognitive_state.history.has_changes()

            await db.refresh(
                row, attribute_names=["cognitive_state", "is_paused", "paused_until"]
            )
            captured["row_after_refresh"] = dict(row.cognitive_state)
            captured["dirty_after_refresh"] = sa_inspect(
                row
            ).attrs.cognitive_state.history.has_changes()

            row.increment_message_count()
            await db.flush()
            captured["dirty_after_followon_flush"] = sa_inspect(
                row
            ).attrs.cognitive_state.history.has_changes()

            db_value = (
                await db.execute(
                    select(Session.cognitive_state).where(Session.id == session_id)
                )
            ).scalar_one()
            captured["db_value_after_followon_flush"] = dict(db_value)
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]

    assert captured["baseline_dirty"] is False
    assert captured["outcome"] == "reactivated"
    assert captured["dirty_immediately_after_cas"] is False
    assert captured["row_after_refresh"] == candidate_state
    assert captured["dirty_after_refresh"] is False
    assert captured["dirty_after_followon_flush"] is False
    assert captured["db_value_after_followon_flush"] == candidate_state
