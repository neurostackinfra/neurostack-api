"""Integration tests for the scheduling-gate wiring in EventService.

Drives ``EventService._convert_pipeline_result_to_response`` with a
stubbed LLM client so we can inject any ``suggested_response`` and assert
that the gate rewrites (or preserves) it under real production wiring.

Focus areas (WISH Group 4):
  * Gate replaces the response when the LLM emits a confirmation /
    availability / processed-change claim.
  * Deterministic responses (e.g. link builder) never get scanned or
    rewritten.
  * The gate is a sibling of the price-gate's ``_insurance_active``
    branch — clinics that do NOT accept insurance still get the gate.
  * ``SCHEDULING_GATE_MODE=observe`` preserves the original text while
    the counter still increments.
  * ``SCHEDULING_GATE_MODE`` unset behaves as enforce.

Uses a short-lived local event loop to drive coroutines without relying on
or mutating pytest's global event-loop state.
"""

from __future__ import annotations

import asyncio
from typing import Any, Optional
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from prometheus_client import REGISTRY

from src.core.pipeline import CognitivePipeline, PipelineResult
from src.observability.scheduling_gate import (
    CATEGORY_AVAILABILITY,
    CATEGORY_CONFIRMED,
    CATEGORY_PROCESSED_CHANGE,
)
from src.services.event_service import EventService

# ============================================
# Fixtures / helpers
# ============================================


class _StubLLM:
    """Minimal LLM client stand-in.

    ``get_tenant_llm_client`` is patched to return this object. Its
    ``generate`` coroutine returns a fixed content payload.
    """

    def __init__(self, content: str) -> None:
        self._content = content

    async def generate(self, messages: list[dict], **_kwargs) -> dict:
        return {"content": self._content, "usage": {}, "model": "stub"}


def _make_result(
    *,
    handoff_required: bool = False,
    funnel_stage: str = "agendamento_solicitado",
    deterministic_response: Optional[str] = None,
    atendimento_tipo: Optional[str] = None,
) -> PipelineResult:
    return PipelineResult(
        tenant_id=uuid4(),
        session_id=uuid4(),
        intent="confirmar_presenca",
        intent_category="scheduling",
        funnel_stage=funnel_stage,
        action="collect_info",
        confidence=0.9,
        safety_concern=False,
        safety_category="none",
        needs_rag=False,
        handoff_required=handoff_required,
        system_prompt="sys",
        instructions="inst",
        mode="normal",
        state_patch={"slots": {}},
        deterministic_response=deterministic_response,
        atendimento_tipo=atendimento_tipo,
    )


def _run_convert(
    service: EventService,
    result: PipelineResult,
    *,
    llm_content: str = "",
    clinic_config: Optional[dict] = None,
    user_message: str = "posso confirmar?",
    language: str = "pt-BR",
) -> tuple[Any, dict, str, list[dict[str, Any]], dict[str, int]]:
    """Drive _convert_pipeline_result_to_response with a stubbed LLM."""
    stub = _StubLLM(llm_content)

    async def _run() -> tuple[Any, dict, str, list[dict[str, Any]], dict[str, int]]:
        with (
            patch(
                "src.llm.client.get_tenant_llm_client",
                return_value=stub,
            ),
            patch(
                "src.services.event_service._load_runtime_llm_credentials",
                new=AsyncMock(return_value={"llm_model_name": "stub"}),
            ),
        ):
            return await service._convert_pipeline_result_to_response(
                result,
                user_message=user_message,
                clinic_config=clinic_config or {},
                tenant_id=result.tenant_id,
                message_history=[],
                language=language,
            )

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_run())
    finally:
        loop.close()


def _violation_count(tenant_id: UUID, category: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "neurostack_scheduling_gate_violations_total",
            {"tenant_id": str(tenant_id), "category": category},
        )
        or 0.0
    )


@pytest.fixture
def service() -> EventService:
    # The gate hook lives on CognitivePipeline, so we must use a real one
    # (or at least something exposing check_response_scheduling_gate).
    # CognitivePipeline() with defaults is safe: we never run the SRM
    # engine — only _convert_pipeline_result_to_response.
    pipeline = CognitivePipeline()
    return EventService(pipeline=pipeline)


@pytest.fixture(autouse=True)
def _default_enforce_mode(monkeypatch: pytest.MonkeyPatch):
    # Ensure each test starts with SCHEDULING_GATE_MODE unset (which
    # defaults to enforce) unless it explicitly overrides.
    monkeypatch.delenv("SCHEDULING_GATE_MODE", raising=False)
    yield


# ============================================
# Enforce mode — text replaced on violation
# ============================================


class TestEnforceModeReplacesResponse:
    def test_pt_confirmed_is_replaced(self, service: EventService) -> None:
        result = _make_result()
        before = _violation_count(result.tenant_id, CATEGORY_CONFIRMED)

        response, _usage, _model, _attempts, _totals = _run_convert(
            service,
            result,
            llm_content="seu agendamento está confirmado para amanhã às 10h",
            clinic_config={"business_rules": {"accepts_insurance": "no"}},
        )

        assert response.prompt is not None
        # Original text must be gone.
        assert (
            "seu agendamento está confirmado" not in response.prompt.suggested_response
        )
        # Counter incremented.
        assert _violation_count(result.tenant_id, CATEGORY_CONFIRMED) == before + 1

    def test_en_confirmed_is_replaced(self, service: EventService) -> None:
        result = _make_result()
        before = _violation_count(result.tenant_id, CATEGORY_CONFIRMED)

        response, _u, _m, _attempts, _totals = _run_convert(
            service,
            result,
            llm_content="Your appointment is confirmed for tomorrow at 10am",
            language="en",
            clinic_config={"business_rules": {"accepts_insurance": "no"}},
        )
        assert (
            "your appointment is confirmed"
            not in response.prompt.suggested_response.lower()
        )
        assert _violation_count(result.tenant_id, CATEGORY_CONFIRMED) == before + 1

    def test_same_sentence_adversative_confirmation_is_replaced(
        self, service: EventService
    ) -> None:
        result = _make_result()
        before = _violation_count(result.tenant_id, CATEGORY_CONFIRMED)
        llm_content = (
            "Não posso confirmar que sua consulta foi marcada, mas "
            "Sua consulta foi marcada."
        )

        response, _u, _m, _attempts, _totals = _run_convert(
            service,
            result,
            llm_content=llm_content,
            clinic_config={"business_rules": {"accepts_insurance": "no"}},
        )

        assert response.prompt is not None
        assert response.prompt.suggested_response != llm_content
        assert (
            "sua consulta foi marcada" not in response.prompt.suggested_response.lower()
        )
        assert _violation_count(result.tenant_id, CATEGORY_CONFIRMED) == before + 1

    def test_pt_availability_is_replaced(self, service: EventService) -> None:
        result = _make_result()
        response, _u, _m, _attempts, _totals = _run_convert(
            service,
            result,
            llm_content="Sim, esse horário está disponível na sexta",
            clinic_config={"business_rules": {"accepts_insurance": "no"}},
        )
        assert "esse horário está disponível" not in response.prompt.suggested_response
        assert _violation_count(result.tenant_id, CATEGORY_AVAILABILITY) >= 1

    @pytest.mark.parametrize(
        "llm_content, language",
        [
            ("Temos horários disponíveis.", "pt-BR"),
            ("We have appointments available.", "en"),
        ],
    )
    def test_direct_availability_assertion_is_replaced(
        self, service: EventService, llm_content: str, language: str
    ) -> None:
        result = _make_result()
        before = _violation_count(result.tenant_id, CATEGORY_AVAILABILITY)

        response, _u, _m, _attempts, _totals = _run_convert(
            service,
            result,
            llm_content=llm_content,
            language=language,
            clinic_config={"business_rules": {"accepts_insurance": "no"}},
        )

        assert response.prompt.suggested_response != llm_content
        assert _violation_count(result.tenant_id, CATEGORY_AVAILABILITY) == before + 1

    def test_pt_processed_change_is_replaced(self, service: EventService) -> None:
        result = _make_result(funnel_stage="agendado")
        response, _u, _m, _attempts, _totals = _run_convert(
            service,
            result,
            llm_content="Pronto, seu cancelamento foi processado.",
            clinic_config={"business_rules": {"accepts_insurance": "no"}},
        )
        assert (
            "seu cancelamento foi processado" not in response.prompt.suggested_response
        )
        assert _violation_count(result.tenant_id, CATEGORY_PROCESSED_CHANGE) >= 1


# ============================================
# Deterministic responses never scanned
# ============================================


class TestDeterministicResponseNotScanned:
    def test_deterministic_link_untouched_even_when_mentions_agendamento(
        self, service: EventService
    ) -> None:
        deterministic_text = (
            "Segue o link para agendamento: https://example.com/booking\n"
            "seu agendamento está confirmado ao clicar."  # ← trigger phrase inside
        )
        result = _make_result(deterministic_response=deterministic_text)
        before = _violation_count(result.tenant_id, CATEGORY_CONFIRMED)

        response, _u, _m, _attempts, _totals = _run_convert(
            service,
            result,
            llm_content="",  # LLM never called on deterministic path
            clinic_config={"business_rules": {"accepts_insurance": "no"}},
        )
        # Deterministic text preserved byte-for-byte.
        assert response.prompt.suggested_response == deterministic_text
        assert response.prompt.deterministic is True
        # Counter did not budge.
        assert _violation_count(result.tenant_id, CATEGORY_CONFIRMED) == before


# ============================================
# Clinic without insurance still gets scheduling gate
# ============================================


class TestGateAppliesRegardlessOfInsurance:
    def test_no_insurance_key_still_triggers_scheduling_gate(
        self, service: EventService
    ) -> None:
        # This is the regression assertion for the exact bug being
        # avoided: the gate MUST NOT be nested inside the
        # ``_insurance_active`` branch.
        result = _make_result()
        before = _violation_count(result.tenant_id, CATEGORY_CONFIRMED)

        # clinic_config with NO business_rules at all — insurance branch
        # would evaluate to False in the price gate.
        response, _u, _m, _attempts, _totals = _run_convert(
            service,
            result,
            llm_content="seu agendamento está confirmado",
            clinic_config={},
        )
        # Response was rewritten.
        assert (
            "seu agendamento está confirmado" not in response.prompt.suggested_response
        )
        assert _violation_count(result.tenant_id, CATEGORY_CONFIRMED) == before + 1

    def test_insurance_explicitly_false_still_triggers(
        self, service: EventService
    ) -> None:
        result = _make_result()
        before = _violation_count(result.tenant_id, CATEGORY_CONFIRMED)

        response, _u, _m, _attempts, _totals = _run_convert(
            service,
            result,
            llm_content="seu agendamento está confirmado",
            clinic_config={"business_rules": {"accepts_insurance": False}},
        )
        assert (
            "seu agendamento está confirmado" not in response.prompt.suggested_response
        )
        assert _violation_count(result.tenant_id, CATEGORY_CONFIRMED) == before + 1


# ============================================
# Observe mode — text preserved, counter still moves
# ============================================


class TestObserveMode:
    def test_observe_preserves_text_but_still_counts(
        self, service: EventService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SCHEDULING_GATE_MODE", "observe")
        result = _make_result()
        before = _violation_count(result.tenant_id, CATEGORY_CONFIRMED)

        response, _u, _m, _attempts, _totals = _run_convert(
            service,
            result,
            llm_content="seu agendamento está confirmado para amanhã",
            clinic_config={"business_rules": {"accepts_insurance": "no"}},
        )
        # Original text preserved verbatim.
        assert "seu agendamento está confirmado" in response.prompt.suggested_response
        # Counter still bumped — observe means "watch, don't block".
        assert _violation_count(result.tenant_id, CATEGORY_CONFIRMED) == before + 1

    def test_unset_env_behaves_as_enforce(
        self, service: EventService, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("SCHEDULING_GATE_MODE", raising=False)
        result = _make_result()
        response, _u, _m, _attempts, _totals = _run_convert(
            service,
            result,
            llm_content="seu agendamento está confirmado",
            clinic_config={"business_rules": {"accepts_insurance": "no"}},
        )
        assert (
            "seu agendamento está confirmado" not in response.prompt.suggested_response
        )


# ============================================
# Funnel = agendado — reply must neither deny nor reaffirm
# ============================================


class TestAgendadoFunnelReplacement:
    def test_agendado_confirmed_replacement_is_neutral_pt(
        self, service: EventService
    ) -> None:
        result = _make_result(funnel_stage="agendado")
        response, _u, _m, _attempts, _totals = _run_convert(
            service,
            result,
            llm_content="Sim, sua consulta está agendada para amanhã!",
            clinic_config={"business_rules": {"accepts_insurance": "no"}},
        )
        reply = response.prompt.suggested_response or ""
        low = reply.lower()
        # No reaffirmation of the appointment.
        assert "está agendada" not in low
        assert "está confirmado" not in low
        assert "confirmed" not in low
        # No denial framings either.
        assert "você não tem" not in low
        assert "voce nao tem" not in low
        assert "não há agendamento" not in low
        assert "no appointment" not in low
        # Neutral hold-and-forward posture: mentions team validation.
        assert "equipe" in low
