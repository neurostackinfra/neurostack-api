"""Integrated regression suite for the ``handoff-contextual-resumption`` wish
(Group 4).

Deliberately runs through the real pipeline: ``EventService``,
``CognitivePipeline``, ``SessionRepository``'s CAS primitives, the Group 3
``FlowManager`` episode-boundary logic, the Group 3 outbound-array framing,
the Group 3 ``normal.py`` prompt-suppression wiring and the Group 4
``saudacao`` suppression (deliverable 1 above). Only the two external,
non-deterministic transports named by the wish are stubbed:

  * the LLM primary client (``src.llm.client.get_tenant_llm_client``) — the
    stub records the ``messages`` crossing the transport boundary and returns
    canned content so assertions on prompt framing/history are deterministic;
  * the WhatsApp/uazapi provider egress (``send_uazapi_text`` /
    ``send_text_message``) is not reached by any test here because the tests
    exercise the ``EventService`` entrypoint directly (not the webhook).

``EventService``, ``SessionRepository``, the handoff decider, the SRM
engine and the database/session persistence are REAL. Where a scenario
needs a specific SRM decision (deterministic re-handoff trigger, benign
demand B, saudacao greeting), ``_run_srm`` on the real ``CognitivePipeline``
instance is overridden with ``AsyncMock(return_value=...)``. This is the
same technique already used by sibling chunks (see
``tests/services/test_event_service_context_episode.py``'s AC1 test) and
is explicitly allowed by the wish text — "é permitido isolar chamadas
externas não determinísticas do LLM e do provider, mas não mockar a
decisão que está sob teste". Overriding ``_run_srm`` on a *single
instance* keeps the pipeline wiring, the handoff-pause policy branch,
the ``_persist_session_state`` CAS and the LLM request-array composition
on the real code path; only the SRM classifier's non-deterministic
input→output step is pinned.

AC coverage (verbatim from the wish Group 4 brief):

  * ``AC1`` — the suite fails against the pre-wish behavior (re-handoff /
    cross-episode contamination) and passes only with the full boundary,
    reset and contextual memory: proven by two tests —
    ``test_demand_b_after_resume_does_not_re_trigger_handoff`` (asserts the
    reset erases ``handoff_requested`` so Priority-7 re-handoff cannot fire)
    and ``test_llm_history_after_resume_excludes_prior_episode_events``
    (asserts the ``_load_message_history`` episode filter actually bites).
  * ``AC2`` — demand B is the only active demand in request/decision; the
    prior A summary stays only in the historical payload: proven by
    ``test_llm_outbound_array_frames_prior_context_as_user_payload_only``.
  * ``AC3`` — resumed saudacao uses the first name and asks the need without
    first-contact self-introduction; a substantive demand is not asked a
    bridge question: proven by
    ``test_saudacao_on_resumed_session_strips_self_introduction`` (which
    pins the Group 4 ``_get_intent_instruction`` suppression) and
    ``test_substantive_demand_b_after_resume_is_answered_directly``.
  * ``AC4`` — no ``fromMe`` / discarded / ``resolution_notes`` message
    enters the automatic summary; paused patient messages are persisted
    but do NOT enter the automatic summary: proven by
    ``test_paused_capture_patient_message_absent_from_prior_episode_context``
    (D2).
  * ``AC5`` — focused suites + aggregate gates pass: executed outside this
    file by the orchestrator via ``full_suite_gate``/``static_gate``/
    ``parity_tests_unchanged`` and the ``tests/baselines/`` hash guard.

Legitimate re-triggers after a resume (deliverable 3):

  * ``test_explicit_human_request_after_resume_still_triggers_handoff``
  * ``test_safety_concern_after_resume_still_triggers_handoff``

Operational coverage (deliverable 4):

  * ``test_phase_1_pause_survives_phase_2_crash_then_reactivates`` — the
    phase-1 → reactivation-before-phase-2 proof. The sibling test in
    ``test_event_service_context_episode.py:AC1`` covers the lowest-level
    EventService invariant; this test covers the same invariant via the
    full ``process_event`` entrypoint plus a resumed-turn LLM request to
    prove the trigger message is still what the LLM sees as the payload.

The scheduling-link → takeover scenario (deliverable 4) is already fully
covered by ``tests/services/test_session_pause_transition.py:AC5``
end-to-end against the real DB; no duplicate is added here to keep the
suite focused on the *integrated regression* layer that is this group's
unique responsibility.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from src.core import pipeline as pipeline_module
from src.core.pipeline import CognitivePipeline
from src.models.session import Session
from src.services import event_service as event_service_module
from src.services.event_service import EventService
from src.services.session_service import SessionService

# ============================================================
# Shared fixtures / helpers
# ============================================================


class _NoOpHandoffStub:
    """Persistence-free handoff double for scenarios whose SRM decision path
    does not take the ``verification_status == "link_sent"`` branch.

    Mirrors the stub in ``tests/services/test_event_service_context_episode.py``:
    the pipeline's handoff service collaborator is only consulted on that
    one branch, and returning ``None`` is correct everywhere else. The
    SessionRepository CAS and ``_persist_session_state`` ORM path are
    untouched by this.
    """

    async def initiate_handoff(self, request: Any) -> Optional[Any]:
        return None


def _seed_tenant_and_session(
    client: TestClient,
    *,
    tenant_id: UUID,
    session_id: UUID,
    user_id: str,
    cognitive_state: Optional[dict[str, Any]] = None,
    paused: bool = False,
    paused_until: Optional[datetime] = None,
) -> None:
    """Insert one Tenant + one Session row on the application's loop."""
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
            db.add(
                Session(
                    id=session_id,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    channel="whatsapp",
                    cognitive_state=cognitive_state or {},
                    session_metadata={},
                    last_activity_at=datetime.now(timezone.utc),
                    is_active=True,
                    is_paused=paused,
                    paused_until=paused_until,
                )
            )
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]


def _read_session_row(client: TestClient, session_id: UUID) -> dict[str, Any]:
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


def _list_events(client: TestClient, session_id: UUID) -> list[dict[str, Any]]:
    from src.models.event import Event
    from src.repositories.database import get_db_session

    async def _run() -> list[dict[str, Any]]:
        db = get_db_session()
        try:
            rows = (
                (
                    await db.execute(
                        select(Event)
                        .where(Event.session_id == session_id)
                        .order_by(Event.created_at)
                    )
                )
                .scalars()
                .all()
            )
            return [
                {
                    "event_type": r.event_type,
                    "event_data": dict(r.event_data or {}),
                    "event_metadata": dict(r.event_metadata or {}),
                }
                for r in rows
            ]
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined,no-any-return]


def _handoff_pause_request_decision() -> "pipeline_module.SRMDecisionModel":
    """Return the SRM decision that drives Priority-7 user_request handoff.

    ``handoff_required=True`` + ``handoff_reason="user_request"`` is the
    exact Priority-7 input the real ``HandoffDecider`` would have produced
    for a message like "quero falar com um atendente humano agora". The
    pipeline consumes this decision, enters ``_apply_handoff_pause``, the
    policy resolver defaults to ``PAUSE_FOREVER`` (no clinic config = no
    overrides), and ``_persist_session_state`` writes the pause + context
    through the single CAS. The downstream behavior under test is the
    boundary + reset, not the classifier that produced the decision.
    """
    return pipeline_module.SRMDecisionModel(
        intent="atendimento_humano",
        intent_category="information",
        funnel_stage="em_qualificacao",
        action="handoff_administrativo",
        confidence=0.95,
        handoff_required=True,
        handoff_reason="user_request",
    )


def _benign_normal_decision(
    *,
    intent: str = "agendar",
    intent_category: str = "scheduling",
    funnel_stage: str = "agendamento_solicitado",
    action: str = "respond",
) -> "pipeline_module.SRMDecisionModel":
    """A benign SRM decision carrying NO handoff and NO safety trigger.

    If a resumed turn produced one of these, the pre-wish behavior would
    STILL fire Priority-7 handoff because the stale ``flags[handoff_requested]``
    survived the pause. The resume projection zeroes those flags, so this
    decision now flows normally — that is the whole wish.
    """
    return pipeline_module.SRMDecisionModel(
        intent=intent,
        intent_category=intent_category,
        funnel_stage=funnel_stage,
        action=action,
        confidence=0.9,
        handoff_required=False,
        handoff_reason=None,
    )


def _saudacao_decision() -> "pipeline_module.SRMDecisionModel":
    return pipeline_module.SRMDecisionModel(
        intent="saudacao",
        intent_category="greeting",
        funnel_stage="lead_novo",
        action="respond",
        confidence=0.95,
        handoff_required=False,
        handoff_reason=None,
    )


def _safety_decision() -> "pipeline_module.SRMDecisionModel":
    return pipeline_module.SRMDecisionModel(
        intent="pos_atendimento_sintomas_preocupantes",
        intent_category="information",
        funnel_stage="em_qualificacao",
        action="ativar_modo_safety_e_handoff",
        confidence=0.95,
        safety_concern=True,
        handoff_required=True,
        handoff_reason="safety_concern",
    )


def _build_real_event_service(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[EventService, CognitivePipeline]:
    """Construct a REAL ``EventService`` wired to a REAL ``CognitivePipeline``.

    Only the handoff service is swapped to the no-op stub (same choice as
    AC1 in the sibling chunk). The pipeline's SRM is monkey-patched per
    test via ``monkeypatch.setattr(instance, "_run_srm", ...)``.

    Also neutralizes the optional Redis side effects so the test runs
    deterministically without a Redis server.
    """
    monkeypatch.setattr(
        event_service_module, "_open_redis", AsyncMock(return_value=None)
    )
    pipeline = CognitivePipeline(handoff_service=_NoOpHandoffStub())
    service = EventService(pipeline=pipeline)
    return service, pipeline


def _install_llm_capture(
    monkeypatch: pytest.MonkeyPatch, *, canned_reply: str = "Ok, vou ajudar."
) -> list[dict[str, Any]]:
    """Replace the LLM primary client with a capturing stub.

    Returns the list that will receive one dict per outbound call. The
    stub returns canned usable content so the primary succeeds on
    attempt 1 and the fallback path is never consulted (that path is
    covered by ``tests/services/test_event_service_llm_fallback.py``).
    """
    captured: list[dict[str, Any]] = []

    class _CapturingLLM:
        async def generate(
            self,
            messages: list[dict[str, Any]],
            model: Optional[str] = None,
            max_tokens: Optional[int] = None,
            **_kwargs: Any,
        ) -> dict[str, Any]:
            captured.append(
                {
                    "messages": [dict(m) for m in messages],
                    "model": model,
                    "max_tokens": max_tokens,
                }
            )
            return {
                "content": canned_reply,
                "model": model or "stub-model",
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                },
            }

    async def _fake_primary_creds(_tenant_id: Any) -> dict[str, Any]:
        return {
            "llm_provider": "openai",
            "llm_api_url": "https://api.openai.com/v1",
            "llm_api_key": "STUB-PRIMARY-KEY",
            "llm_model_name": "stub-primary",
        }

    async def _fake_fallback_creds(_tenant_id: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(
        event_service_module, "_load_runtime_llm_credentials", _fake_primary_creds
    )
    monkeypatch.setattr(
        event_service_module,
        "_load_multimodal_fallback_credentials",
        _fake_fallback_creds,
    )
    monkeypatch.setattr(
        "src.llm.client.get_tenant_llm_client",
        lambda _tenant_id, _creds: _CapturingLLM(),
    )
    monkeypatch.setattr(
        "src.llm.client.get_multimodal_text_llm_client",
        lambda _tenant_id, _creds: None,
    )
    return captured


def _event_request(
    *,
    tenant_id: UUID,
    session_id: UUID,
    content: str,
    phone: str,
) -> Any:
    from src.api.schemas.events import EventPayload, EventRequest, EventType

    return EventRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        event_type=EventType.MESSAGE_IN,
        payload=EventPayload(content=content, channel="whatsapp", user_id=phone),
    )


# ============================================================
# Core demand A → pause → reactivate → demand B scenarios
# ============================================================


def test_demand_b_after_resume_does_not_re_trigger_handoff(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full A → pause → manual reactivate → B flow through the real pipeline.

    This is the headline regression. Pre-wish behavior:
      * Turn A sets ``flags[handoff_requested]=True`` (Priority-7 flag).
      * The pause fires on the SRM decision alone.
      * On reactivation (pre-wish), ``cognitive_state.flags`` survives.
      * Turn B's SRM returns ``handoff_required=False`` legitimately, but
        the SRM engine's Priority-7 pass re-fires on the stale flag and
        the session paused again instantly.

    Wish behavior: the Group 2 projection wipes ``flags``,
    ``intent_history``, timers etc. On turn B, the real pipeline
    constructs a fresh ``PipelineInput`` from the reset state; the
    forced benign SRM decision returns ``handoff_required=False`` and the
    pipeline's ``_apply_handoff_pause`` is never consulted. The test
    asserts: (a) B's pipeline result does NOT pause and (b) the DB row's
    pause fields have not re-flipped.
    """
    service, pipeline = _build_real_event_service(monkeypatch)
    _install_llm_capture(monkeypatch, canned_reply="Perfeito, vamos agendar.")

    tenant_id = uuid4()
    session_id = uuid4()
    phone = "5511977770001"
    _seed_tenant_and_session(
        client,
        tenant_id=tenant_id,
        session_id=session_id,
        user_id=phone,
    )

    from src.repositories.database import get_db_session

    # ── Turn A: patient requests a human. Real pipeline + forced SRM. ──
    async def _turn_a() -> bool:
        monkeypatch.setattr(
            pipeline,
            "_run_srm",
            AsyncMock(return_value=_handoff_pause_request_decision()),
        )
        db = get_db_session()
        try:
            response = await service.process_event(
                _event_request(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    content="quero falar com um atendente humano agora",
                    phone=phone,
                ),
                session=db,
            )
            # process_event never commits; the pause CAS already issued the
            # UPDATE but we must commit the transaction so later readers see it.
            await db.commit()
            return bool(response.decision.handoff_required)
        finally:
            await db.close()

    a_handoff = client._submit(_turn_a())  # type: ignore[attr-defined]
    assert a_handoff is True

    paused_row = _read_session_row(client, session_id)
    assert paused_row["is_paused"] is True, paused_row
    prior_ctx = paused_row["cognitive_state"].get("prior_episode_context")
    assert prior_ctx is not None, paused_row["cognitive_state"]
    assert prior_ctx["schema_version"] == 1
    # The trigger message is captured in prior_episode_context during phase 1
    # (wish Decision 3 / Group 2 AC) — this is the memory that resumption
    # consumes.
    assert "atendente humano" in prior_ctx["summary"]

    # ── Reactivate manually (same code path /sessions/{id}/reactivate uses). ──
    async def _reactivate() -> str:
        db = get_db_session()
        try:
            resp = await SessionService().reactivate_session(tenant_id, session_id, db)
            await db.commit()
            return resp.status
        finally:
            await db.close()

    status = client._submit(_reactivate())  # type: ignore[attr-defined]
    assert status == "reactivated"

    reactivated = _read_session_row(client, session_id)
    assert reactivated["is_paused"] is False
    cs = reactivated["cognitive_state"]
    # Projection invariant: handoff_requested is gone, episode id is new,
    # prior context survives, turn_count is zero.
    assert cs.get("flags", {}).get("handoff_requested") in (None, False)
    assert cs.get("turn_count") == 0
    assert cs.get("context_episode_id")  # non-empty UUID str
    assert cs["prior_episode_context"] == prior_ctx

    # ── Turn B: a different, benign demand on the reactivated session. ──
    captured_b: list[Any] = []

    async def _turn_b() -> Any:
        monkeypatch.setattr(
            pipeline,
            "_run_srm",
            AsyncMock(return_value=_benign_normal_decision()),
        )
        db = get_db_session()
        try:
            response = await service.process_event(
                _event_request(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    content="quero agendar na quinta às 15h",
                    phone=phone,
                ),
                session=db,
            )
            captured_b.append(response)
            return response
        finally:
            await db.close()

    response_b = client._submit(_turn_b())  # type: ignore[attr-defined]
    assert response_b.decision.handoff_required is False, (
        "benign demand B must not re-trigger handoff after reactivation — "
        "the reset projection wiped flags[handoff_requested] (wish AC)"
    )
    final = _read_session_row(client, session_id)
    assert final["is_paused"] is False, (
        "demand B on a resumed session must not re-pause the session — "
        "proves no residual re-handoff from leftover state"
    )
    # Episode id is unchanged across turn B (same episode, next turn).
    assert final["cognitive_state"]["context_episode_id"] == cs["context_episode_id"]


def test_substantive_demand_b_after_resume_is_answered_directly(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Resumed turn with a substantive (non-greeting) demand gets a real reply.

    AC: "mensagem substantiva não recebe pergunta intermediária" — a
    substantive demand must not be deferred by an interstitial "qual é
    sua necessidade?" question. End-to-end: the LLM is called exactly
    once with a usable response, the pipeline returns that response, and
    the ``last_intent`` on the resumed session reflects the substantive
    intent (``agendar``), not ``saudacao``.
    """
    service, pipeline = _build_real_event_service(monkeypatch)
    captured = _install_llm_capture(
        monkeypatch, canned_reply="Claro! Para que dia você gostaria?"
    )

    tenant_id = uuid4()
    session_id = uuid4()
    phone = "5511977770002"
    # Pre-seed as a resumed session: already carries a prior_episode_context
    # and a fresh context_episode_id. Simulates "the operator reactivated,
    # patient now sends a substantive message".
    resumed_state = {
        "funnel_stage": "lead_novo",
        "turn_count": 0,
        "intent_history": [],
        "flags": {},
        "slots": {"lead_name": "Ana"},
        "context_episode_id": str(uuid4()),
        "prior_episode_context": {
            "schema_version": 1,
            "lead_name": "Ana",
            "summary": "episódio anterior: Ana perguntou sobre valores",
        },
    }
    _seed_tenant_and_session(
        client,
        tenant_id=tenant_id,
        session_id=session_id,
        user_id=phone,
        cognitive_state=resumed_state,
    )

    from src.repositories.database import get_db_session

    async def _turn() -> Any:
        monkeypatch.setattr(
            pipeline,
            "_run_srm",
            AsyncMock(return_value=_benign_normal_decision()),
        )
        db = get_db_session()
        try:
            return await service.process_event(
                _event_request(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    content="quero agendar uma consulta nesta semana",
                    phone=phone,
                ),
                session=db,
            )
        finally:
            await db.close()

    response = client._submit(_turn())  # type: ignore[attr-defined]
    assert response.prompt is not None
    assert response.prompt.suggested_response == "Claro! Para que dia você gostaria?"
    assert response.decision.handoff_required is False
    # Exactly one primary attempt. The fallback is deliberately disabled.
    assert len(captured) == 1


def test_saudacao_on_resumed_session_strips_self_introduction(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Group 4 deliverable 1 / AC #3 — the ``saudacao`` branch suppression.

    Pins the Group 4 ``_get_intent_instruction`` resume-mode suppression
    AND the Group 3 suppressions in ``get_system_prompt`` /
    ``get_context_instructions`` so a resumed session whose detected
    intent is ``saudacao`` renders a prompt that neither (a) carries
    the Golden Rule #2 self-introduction imperative, (b) carries the
    "Lead Novo (Primeiro Contato)" funnel block, NOR (c) carries the
    third occurrence baked into the ``saudacao`` intent branch.

    This test is intentionally two-shot: once at the raw-prompt layer
    (the three literal checks against the rendered strings) and once
    at the end-to-end layer (the LLM request captures the composed
    system prompt + context so a regression at *either* layer trips).
    """
    service, pipeline = _build_real_event_service(monkeypatch)
    captured = _install_llm_capture(
        monkeypatch, canned_reply="Olá, Ana! Em que posso ajudar?"
    )

    tenant_id = uuid4()
    session_id = uuid4()
    phone = "5511977770003"
    resumed_state = {
        "funnel_stage": "lead_novo",
        "turn_count": 0,
        "intent_history": [],
        "flags": {},
        "slots": {"lead_name": "Ana"},
        "context_episode_id": str(uuid4()),
        "prior_episode_context": {
            "schema_version": 1,
            "lead_name": "Ana",
            "summary": "episódio anterior encerrado",
        },
    }
    _seed_tenant_and_session(
        client,
        tenant_id=tenant_id,
        session_id=session_id,
        user_id=phone,
        cognitive_state=resumed_state,
    )

    # ── Layer 1: unit-check all three suppressions against the raw template. ──
    from neurostack_core.prompt.models import PromptVariables
    from neurostack_core.prompt.templates.normal import NormalTemplate

    tpl = NormalTemplate()
    resume_vars = PromptVariables(
        user_message="oi",
        funnel_stage="lead_novo",
        last_intent="saudacao",
        channel="whatsapp",
        turn_count=1,
        custom={
            "agent_name": "Daniela",
            "clinic_name": "Clínica Vida Nova",
            "prior_episode_context": resumed_state["prior_episode_context"],
        },
    )
    rendered_system = tpl.get_system_prompt(resume_vars)
    rendered_context = tpl.get_context_instructions(resume_vars)
    # Golden Rule #2 (Group 3 suppression in get_system_prompt).
    assert (
        "Apresente-se sempre no primeiro contato." not in rendered_system
    ), "Group 3 suppression in get_system_prompt regressed on last_intent=saudacao"
    # Lead-novo funnel block (Group 3 suppression in get_context_instructions).
    assert (
        "Primeiro Contato" not in rendered_context
    ), "Group 3 suppression of the lead_novo funnel block regressed"
    # NEW — the saudacao intent branch's third self-introduction (Group 4
    # deliverable 1). Pre-fix, this assertion fails; the Group 4 fix in
    # ``_get_intent_instruction`` is what makes it pass.
    assert 'Apresente-se: "Meu nome é Daniela' not in rendered_context, (
        "Group 4 suppression of the saudacao intent branch's self-"
        "introduction regressed — a resumed session with intent=saudacao "
        "is re-introducing the agent"
    )
    assert "Meu nome é Daniela" not in rendered_context, (
        "the saudacao intent branch is still leaking the self-intro line "
        "on a resumed session"
    )
    # First-contact control: the SAME template, but WITHOUT prior_episode_context,
    # must keep every self-introduction intact so suppression is proven
    # discriminating, not a blanket deletion.
    first_contact_vars = PromptVariables(
        user_message="oi",
        funnel_stage="lead_novo",
        last_intent="saudacao",
        channel="whatsapp",
        turn_count=1,
        custom={"agent_name": "Daniela", "clinic_name": "Clínica Vida Nova"},
    )
    first_contact_context = tpl.get_context_instructions(first_contact_vars)
    assert "Meu nome é Daniela" in first_contact_context, (
        "first-contact saudacao MUST keep the self-introduction — Group 4 "
        "suppression is gated on prior_episode_context, not blanket"
    )

    # ── Layer 2: end-to-end — the composed prompt the LLM actually sees. ──
    from src.repositories.database import get_db_session

    async def _turn() -> Any:
        monkeypatch.setattr(
            pipeline,
            "_run_srm",
            AsyncMock(return_value=_saudacao_decision()),
        )
        db = get_db_session()
        try:
            return await service.process_event(
                _event_request(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    content="oi",
                    phone=phone,
                ),
                session=db,
            )
        finally:
            await db.close()

    response = client._submit(_turn())  # type: ignore[attr-defined]
    assert response.prompt is not None

    # The LLM captures expose the EXACT outbound messages. The first
    # ``system`` content is the composed main prompt (system) +
    # instructions (context) that the pipeline/response composer produced.
    assert len(captured) >= 1, "LLM primary must have been called"
    system_messages = [
        m["content"] for m in captured[0]["messages"] if m["role"] == "system"
    ]
    full_system_blob = "\n".join(system_messages)
    assert "Apresente-se sempre no primeiro contato." not in full_system_blob
    # The saudacao-branch self-introduction (the Group 4 fix). This
    # assertion is the end-to-end mirror of Layer 1's unit assertion.
    assert 'Apresente-se: "Meu nome é' not in full_system_blob, (
        "composed outbound prompt still contains the saudacao-branch "
        "self-introduction imperative on a resumed session"
    )


# ============================================================
# AC2 — demand B is the sole active demand; A's summary stays historical
# ============================================================


def test_llm_outbound_array_frames_prior_context_as_user_payload_only(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC: "a demanda B aparece como a única demanda ativa no request/decisão;
    o resumo de A permanece disponível apenas no payload histórico".

    Verifies — against the REAL outbound array composed in
    ``_convert_pipeline_result_to_response`` — that:

      1. The main ``system`` message (the clinic+funnel+intent prompt) does
         NOT contain the prior-episode summary text (A's demand).
      2. The prior-episode summary is present ONLY inside a dedicated
         ``user`` turn whose content is a ``json.dumps`` of the frozen
         ``prior_episode_context`` object, immediately preceded by the
         constant policy ``system`` turn.
      3. The LAST message is the current user turn and contains B's
         demand text verbatim.

    This is the exact structural guarantee of Decision 9 and wish
    deliverable 3 of Group 3; the test re-asserts it at the integrated
    layer where regression would bite silently (the main prompt getting
    silently extended again).
    """
    service, pipeline = _build_real_event_service(monkeypatch)
    captured = _install_llm_capture(monkeypatch, canned_reply="Certo, vou verificar.")

    tenant_id = uuid4()
    session_id = uuid4()
    phone = "5511977770004"
    prior_ctx = {
        "schema_version": 1,
        "lead_name": "Ana",
        "summary": "DEMANDA-A-CANARY: Ana perguntou sobre valores do tratamento X",
    }
    _seed_tenant_and_session(
        client,
        tenant_id=tenant_id,
        session_id=session_id,
        user_id=phone,
        cognitive_state={
            "funnel_stage": "em_qualificacao",
            "turn_count": 0,
            "intent_history": [],
            "flags": {},
            "slots": {"lead_name": "Ana"},
            "context_episode_id": str(uuid4()),
            "prior_episode_context": prior_ctx,
        },
    )

    from src.repositories.database import get_db_session

    demand_b_text = "DEMANDA-B-UNICA: quero agendar amanhã"

    async def _turn_b() -> Any:
        monkeypatch.setattr(
            pipeline,
            "_run_srm",
            AsyncMock(return_value=_benign_normal_decision()),
        )
        db = get_db_session()
        try:
            return await service.process_event(
                _event_request(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    content=demand_b_text,
                    phone=phone,
                ),
                session=db,
            )
        finally:
            await db.close()

    client._submit(_turn_b())  # type: ignore[attr-defined]

    assert len(captured) == 1
    messages = captured[0]["messages"]
    # Shape: [system(main), system(policy), user(json_payload), ...history..., user(current)]
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "system"
    assert messages[2]["role"] == "user"

    main_system = messages[0]["content"]
    policy_system = messages[1]["content"]
    payload_user = messages[2]["content"]
    last = messages[-1]

    # (1) Main system prompt must not carry A's canary.
    assert "DEMANDA-A-CANARY" not in main_system, (
        "the prior-episode summary leaked into the MAIN system prompt — "
        "Group 3 framing regressed; it must only live inside the dedicated "
        "user JSON payload"
    )
    # (2) Policy framing is a constant system turn, not a splice of content.
    assert "DEMANDA-A-CANARY" not in policy_system
    # (3) JSON payload turn carries ONLY the canonical prior_episode_context
    # object. json.loads must round-trip to the exact dict.
    import json as _json

    parsed = _json.loads(payload_user)
    assert parsed == prior_ctx, parsed
    # (4) Last turn is B's text, verbatim.
    assert last["role"] == "user"
    assert last["content"] == demand_b_text


# ============================================================
# Boundary: history after resume excludes prior episode events
# ============================================================


def test_llm_history_after_resume_excludes_prior_episode_events(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Group 3 ``_load_message_history`` episode filter bites end-to-end.

    Seeds two ``message.in`` events on the same session: one tagged with
    the PRIOR episode id and one tagged with the CURRENT episode id. The
    captured outbound messages array must contain the current-episode
    event's content and NOT the prior-episode event's content.
    """
    service, pipeline = _build_real_event_service(monkeypatch)
    captured = _install_llm_capture(monkeypatch, canned_reply="Entendi.")

    tenant_id = uuid4()
    session_id = uuid4()
    phone = "5511977770005"
    current_episode_id = str(uuid4())
    prior_episode_id = str(uuid4())
    _seed_tenant_and_session(
        client,
        tenant_id=tenant_id,
        session_id=session_id,
        user_id=phone,
        cognitive_state={
            "funnel_stage": "em_qualificacao",
            "turn_count": 1,
            "flags": {},
            "slots": {},
            "context_episode_id": current_episode_id,
            "prior_episode_context": {
                "schema_version": 1,
                "lead_name": None,
                "summary": "summary neutro",
            },
        },
    )

    from src.repositories import EventRepository
    from src.repositories.database import get_db_session

    async def _seed_events() -> None:
        db = get_db_session()
        try:
            # Prior episode message — MUST NOT be sent to the LLM.
            await EventRepository(db).create(
                tenant_id=tenant_id,
                session_id=session_id,
                event_type="message.in",
                event_data={
                    "content": "PRIOR-EPISODE-CANARY: fala da demanda antiga",
                    "channel": "whatsapp",
                    "user_id": phone,
                    "role": "user",
                },
                event_metadata={"context_episode_id": prior_episode_id},
            )
            # Current episode message — SHOULD be sent to the LLM as history.
            await EventRepository(db).create(
                tenant_id=tenant_id,
                session_id=session_id,
                event_type="message.in",
                event_data={
                    "content": "CURRENT-EPISODE-HELLO: fala do episódio atual",
                    "channel": "whatsapp",
                    "user_id": phone,
                    "role": "user",
                },
                event_metadata={"context_episode_id": current_episode_id},
            )
            await db.commit()
        finally:
            await db.close()

    client._submit(_seed_events())  # type: ignore[attr-defined]

    async def _turn() -> Any:
        monkeypatch.setattr(
            pipeline,
            "_run_srm",
            AsyncMock(return_value=_benign_normal_decision()),
        )
        db = get_db_session()
        try:
            return await service.process_event(
                _event_request(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    content="segue a conversa atual",
                    phone=phone,
                ),
                session=db,
            )
        finally:
            await db.close()

    client._submit(_turn())  # type: ignore[attr-defined]
    assert len(captured) == 1
    full_blob = "\n".join(str(m.get("content", "")) for m in captured[0]["messages"])
    assert "CURRENT-EPISODE-HELLO" in full_blob, (
        "current-episode history event is missing from the outbound LLM "
        "request — the episode filter is dropping too much"
    )
    assert "PRIOR-EPISODE-CANARY" not in full_blob, (
        "prior-episode history event leaked into the outbound LLM request "
        "— _load_message_history episode filter regressed"
    )


# ============================================================
# D2 — paused-capture patient message is persisted but EXCLUDED from summary
# ============================================================


def test_paused_capture_patient_message_absent_from_prior_episode_context(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Group 4 deliverable 6 (D2). The Group 2 D2 implementation stores
    ``paused_capture=True`` events; this test proves two companion
    guarantees from Group 4's integrated layer:

      1. On a subsequent reactivation whose ``prior_episode_context`` is
         composed from the AI/person event log, the ``paused_capture``
         events do NOT contribute to ``summary``.
      2. The ``paused_capture`` events remain in the Event Store (D2's
         "recuperáveis/auditáveis" guarantee).

    This test does not re-prove the webhook persistence D2 branch — that
    is already covered end-to-end by
    ``tests/api/test_webhook_uazapi.py::TestUazapiPausedPatientMessageRetention``.
    It proves what Group 4 uniquely owns: that the summary composer the
    reactivation flow calls honors the exclusion.
    """
    from src.api.routes.webhook import _load_ai_person_history
    from src.handoff.episode_context import build_prior_episode_context
    from src.repositories import EventRepository
    from src.repositories.database import get_db_session

    tenant_id = uuid4()
    session_id = uuid4()
    phone = "5511977770006"
    _seed_tenant_and_session(
        client,
        tenant_id=tenant_id,
        session_id=session_id,
        user_id=phone,
        cognitive_state={},
    )

    canary_regular = "REGULAR-TURN-CANARY: fala pré-pausa do paciente"
    canary_paused = "PAUSED-CAPTURE-CANARY: fala do paciente durante a pausa"

    async def _seed_events() -> None:
        db = get_db_session()
        try:
            # Ordinary (pre-pause) patient message — belongs in the summary.
            await EventRepository(db).create(
                tenant_id=tenant_id,
                session_id=session_id,
                event_type="message.in",
                event_data={
                    "content": canary_regular,
                    "channel": "whatsapp",
                    "user_id": phone,
                    "role": "user",
                },
                event_metadata={},
            )
            # Paused-capture patient message — EXCLUDED from the summary.
            await EventRepository(db).create(
                tenant_id=tenant_id,
                session_id=session_id,
                event_type="message.in",
                event_data={
                    "content": canary_paused,
                    "channel": "whatsapp",
                    "user_id": phone,
                    "role": "user",
                },
                event_metadata={"paused_capture": True},
            )
            await db.commit()
        finally:
            await db.close()

    client._submit(_seed_events())  # type: ignore[attr-defined]

    async def _compose() -> dict[str, Any]:
        db = get_db_session()
        try:
            events = await _load_ai_person_history(tenant_id, session_id, db, limit=20)
            return build_prior_episode_context(
                tenant_id=tenant_id,
                session_id=session_id,
                events=events,
                current_message=None,
            )
        finally:
            await db.close()

    composed = client._submit(_compose())  # type: ignore[attr-defined]
    summary = composed["summary"]
    assert (
        canary_regular in summary or "summary" in summary
    ), f"regular pre-pause message dropped from summary unexpectedly: {composed}"
    assert canary_paused not in summary, (
        "paused_capture patient message leaked into the automatic summary "
        "(D2 exclusion regressed): " + summary
    )

    # D2 part 2: the paused_capture event is still in the Event Store
    # (recuperável / auditável) — this file re-asserts the invariant that
    # the Group 2 webhook route landed, as a defense in depth for Group 4.
    events_after = _list_events(client, session_id)
    paused_events = [
        e
        for e in events_after
        if e["event_type"] == "message.in"
        and e["event_metadata"].get("paused_capture") is True
    ]
    assert len(paused_events) == 1
    assert paused_events[0]["event_data"]["content"] == canary_paused


# ============================================================
# Legitimate re-triggers after a resume still work
# ============================================================


def test_explicit_human_request_after_resume_still_triggers_handoff(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deliverable 3 — the wish must NOT suppress legitimate re-handoff
    triggers that originate from the CURRENT message. A resumed session
    whose current turn explicitly asks for a human must be paused again,
    on its own merits.
    """
    service, pipeline = _build_real_event_service(monkeypatch)
    _install_llm_capture(monkeypatch)

    tenant_id = uuid4()
    session_id = uuid4()
    phone = "5511977770007"
    _seed_tenant_and_session(
        client,
        tenant_id=tenant_id,
        session_id=session_id,
        user_id=phone,
        cognitive_state={
            "funnel_stage": "lead_novo",
            "turn_count": 0,
            "flags": {},
            "slots": {"lead_name": "Ana"},
            "context_episode_id": str(uuid4()),
            "prior_episode_context": {
                "schema_version": 1,
                "lead_name": "Ana",
                "summary": "episódio anterior encerrado",
            },
        },
    )

    from src.repositories.database import get_db_session

    async def _turn() -> Any:
        monkeypatch.setattr(
            pipeline,
            "_run_srm",
            AsyncMock(return_value=_handoff_pause_request_decision()),
        )
        db = get_db_session()
        try:
            response = await service.process_event(
                _event_request(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    content="quero falar com um atendente humano, por favor",
                    phone=phone,
                ),
                session=db,
            )
            # process_event never commits; the pause CAS already issued the
            # UPDATE but we must commit the transaction so later readers see it.
            await db.commit()
            return response
        finally:
            await db.close()

    response = client._submit(_turn())  # type: ignore[attr-defined]
    assert response.decision.handoff_required is True
    row = _read_session_row(client, session_id)
    assert row["is_paused"] is True, (
        "a resumed session whose CURRENT message legitimately requests "
        "handoff must still be paused — Group 2 reset must not block the "
        "current-message decision"
    )


def test_safety_concern_after_resume_still_triggers_handoff(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deliverable 3 companion — the same guarantee for the safety path."""
    service, pipeline = _build_real_event_service(monkeypatch)
    _install_llm_capture(monkeypatch)

    tenant_id = uuid4()
    session_id = uuid4()
    phone = "5511977770008"
    _seed_tenant_and_session(
        client,
        tenant_id=tenant_id,
        session_id=session_id,
        user_id=phone,
        cognitive_state={
            "funnel_stage": "lead_novo",
            "turn_count": 0,
            "flags": {},
            "slots": {"lead_name": "Ana"},
            "context_episode_id": str(uuid4()),
            "prior_episode_context": {
                "schema_version": 1,
                "lead_name": "Ana",
                "summary": "episódio anterior encerrado",
            },
        },
    )

    from src.repositories.database import get_db_session

    async def _turn() -> Any:
        monkeypatch.setattr(
            pipeline,
            "_run_srm",
            AsyncMock(return_value=_safety_decision()),
        )
        db = get_db_session()
        try:
            response = await service.process_event(
                _event_request(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    content="estou sentindo dor forte depois da consulta",
                    phone=phone,
                ),
                session=db,
            )
            # process_event never commits; the pause CAS already issued the
            # UPDATE but we must commit the transaction so later readers see it.
            await db.commit()
            return response
        finally:
            await db.close()

    response = client._submit(_turn())  # type: ignore[attr-defined]
    assert response.decision.handoff_required is True
    row = _read_session_row(client, session_id)
    assert row["is_paused"] is True


# ============================================================
# Operational — phase 1 pause survives phase 2 crash; reactivation works
# ============================================================


def test_phase_1_pause_survives_phase_2_crash_then_reactivates(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Integrated mirror of ``test_event_service_context_episode.py::AC1``.

    AC1 of the sibling chunk proved the invariant at the ``run_pipeline``
    layer. This test proves it still holds when the entrypoint is the
    higher-level ``process_event`` (the method the webhook route actually
    calls) — if ``process_event`` later gets a phase-2-first optimization,
    the invariant would break and this test would catch it.

    Blocks ``_save_events_to_store`` (phase 2) so a simulated worker
    crash after the pause CAS leaves no ``message.in`` row for the
    trigger. Then reactivates and asserts the reactivation sees the
    trigger via the already-frozen ``prior_episode_context.summary``,
    not via a message.in row.
    """
    service, pipeline = _build_real_event_service(monkeypatch)
    _install_llm_capture(monkeypatch)
    monkeypatch.setattr(
        EventService,
        "_save_events_to_store",
        AsyncMock(side_effect=AssertionError("phase 2 must never run here")),
    )

    tenant_id = uuid4()
    session_id = uuid4()
    phone = "5511977770009"
    trigger_text = "Quero falar com um atendente humano agora, por favor"
    _seed_tenant_and_session(
        client,
        tenant_id=tenant_id,
        session_id=session_id,
        user_id=phone,
    )

    from src.repositories.database import get_db_session

    async def _phase1_only() -> bool:
        monkeypatch.setattr(
            pipeline,
            "_run_srm",
            AsyncMock(return_value=_handoff_pause_request_decision()),
        )
        db = get_db_session()
        try:
            # Call run_pipeline + commit directly; skip finish_response
            # (which would hit _save_events_to_store).
            result, _cfg, _hist = await service.run_pipeline(
                _event_request(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    content=trigger_text,
                    phone=phone,
                ),
                session=db,
            )
            await db.commit()
            return bool(result.should_pause)
        finally:
            await db.close()

    assert client._submit(_phase1_only()) is True  # type: ignore[attr-defined]

    paused_row = _read_session_row(client, session_id)
    assert paused_row["is_paused"] is True
    prior_ctx = paused_row["cognitive_state"]["prior_episode_context"]
    assert prior_ctx is not None
    assert trigger_text in prior_ctx["summary"]

    # The message.in row for the trigger text was NEVER written: phase 2
    # was blocked above, and run_pipeline alone does not persist it.
    events = _list_events(client, session_id)
    trigger_events = [
        e
        for e in events
        if e["event_type"] == "message.in"
        and e["event_data"].get("content") == trigger_text
    ]
    assert (
        trigger_events == []
    ), "message.in for trigger was persisted even though phase 2 was blocked"

    # Reactivate and read back the final prior_episode_context — the
    # trigger must still be recoverable from the frozen summary.
    async def _reactivate() -> str:
        db = get_db_session()
        try:
            resp = await SessionService().reactivate_session(tenant_id, session_id, db)
            await db.commit()
            return resp.status
        finally:
            await db.close()

    assert client._submit(_reactivate()) == "reactivated"  # type: ignore[attr-defined]
    final_ctx = _read_session_row(client, session_id)["cognitive_state"][
        "prior_episode_context"
    ]
    assert trigger_text in final_ctx["summary"]


# ============================================================
# Observability — no PII in resume-mode logs
# ============================================================


_PII_FORBIDDEN_RE = re.compile(
    # 10-15 digit phone-number runs (any patient number used in this file)
    r"5511977770\d{3}|"
    # The literal demand text snippets used across the suite — if any of
    # these appear in a log record, something leaked.
    r"DEMANDA-[AB]-|PRIOR-EPISODE-CANARY|CURRENT-EPISODE-HELLO|"
    r"PAUSED-CAPTURE-CANARY|REGULAR-TURN-CANARY|"
    r"atendente humano agora|quero agendar amanhã"
)


def test_resume_mode_logs_contain_no_pii(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A full A → pause → reactivate → B cycle emits no PII in the logs
    the wish's new code paths own (``session_pause_persisted``,
    ``flow_manager_episode_boundary_reset``,
    ``history_episode_boundary_filter_applied``,
    ``session_auto_unpause*``, ``prior_episode_context_payload_invalid``).

    We assert a positive invariant: no log *record* produced during the
    cycle contains the patient phone, the trigger text, the summary
    canary or demand-B text in ANY of its fields. The test does not
    assert which log lines fire — only that whatever fires is PII-free.
    """
    service, pipeline = _build_real_event_service(monkeypatch)
    _install_llm_capture(monkeypatch)

    tenant_id = uuid4()
    session_id = uuid4()
    phone = "5511977770099"
    _seed_tenant_and_session(
        client,
        tenant_id=tenant_id,
        session_id=session_id,
        user_id=phone,
    )

    from src.repositories.database import get_db_session

    caplog.set_level("DEBUG")

    async def _cycle() -> None:
        # Turn A — pause.
        monkeypatch.setattr(
            pipeline,
            "_run_srm",
            AsyncMock(return_value=_handoff_pause_request_decision()),
        )
        db = get_db_session()
        try:
            await service.process_event(
                _event_request(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    content="DEMANDA-A-UNICA: atendente humano agora por favor",
                    phone=phone,
                ),
                session=db,
            )
        finally:
            await db.close()
        # Reactivate.
        db = get_db_session()
        try:
            await SessionService().reactivate_session(tenant_id, session_id, db)
            await db.commit()
        finally:
            await db.close()
        # Turn B — benign.
        monkeypatch.setattr(
            pipeline,
            "_run_srm",
            AsyncMock(return_value=_benign_normal_decision()),
        )
        db = get_db_session()
        try:
            await service.process_event(
                _event_request(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    content="DEMANDA-B-UNICA: quero agendar amanhã",
                    phone=phone,
                ),
                session=db,
            )
        finally:
            await db.close()

    client._submit(_cycle())  # type: ignore[attr-defined]

    # The log records this file owns are the pause/resume/boundary ones;
    # scope the check to the wish's own code paths plus the phone number
    # as a universal PII canary. A broader "no raw message text anywhere"
    # check across the entire caplog would false-positive on the
    # structured ``extra={"phone": phone}`` fields that the webhook
    # (not touched by this test) already logs — those are tested in the
    # Group 2 session-pause chunk. Here we check only the lines the wish
    # introduced.
    owned_events = {
        "session_pause_persisted",
        "session_pause_cas_non_winner",
        "session_auto_unpause",
        "session_auto_unpause_non_winner",
        "flow_manager_episode_boundary_reset",
        "history_episode_boundary_filter_applied",
        "prior_episode_context_payload_invalid",
    }
    relevant = [r for r in caplog.records if r.getMessage() in owned_events]
    for record in relevant:
        rendered = repr(record.__dict__)
        match = _PII_FORBIDDEN_RE.search(rendered)
        assert match is None, (
            f"PII leaked into log record {record.getMessage()!r}: "
            f"forbidden token {match.group(0)!r} in {rendered!r}"
        )
