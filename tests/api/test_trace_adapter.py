"""
Tests for the trace adapter service.
Validates SRM decision → FrontendDecisionTrace mapping.
"""

from src.services.trace_adapter import (
    FrontendDecisionTrace,
    FrontendSRMDecision,
    adapt_srm_decision,
    build_decision_trace,
    generate_selected_context,
)
from src.srm.models import (
    ActionType,
    FunnelStage,
    IntentCategory,
    SafetyCategory,
    SRMDecision,
    StatePatch,
)


def _make_decision(**overrides) -> SRMDecision:
    """Helper to build SRMDecision with defaults."""
    defaults = dict(
        intent="saudacao",
        intent_category=IntentCategory.GREETING,
        funnel_stage=FunnelStage.LEAD_NOVO,
        action=ActionType.RESPOND,
        confidence=0.98,
        safety_concern=False,
        safety_category=SafetyCategory.NONE,
        handoff_required=False,
        state_patch=StatePatch(),
    )
    defaults.update(overrides)
    return SRMDecision(**defaults)


# ============================================
# adapt_srm_decision tests
# ============================================


class TestAdaptSRMDecision:
    """Tests for adapt_srm_decision()."""

    def test_greeting_basic(self):
        decision = _make_decision()
        result = adapt_srm_decision(decision)

        assert isinstance(result, FrontendSRMDecision)
        assert result.intent == "saudacao"
        assert result.stage == "initial"  # lead_novo → initial
        assert result.action == "respond"
        assert result.confidence == 0.98
        assert result.safety_concern is False
        assert result.handoff_required is False
        assert result.gate_authority_before_price is False

    def test_stage_mapping_lead_novo(self):
        decision = _make_decision(funnel_stage=FunnelStage.LEAD_NOVO)
        result = adapt_srm_decision(decision)
        assert result.stage == "initial"

    def test_stage_mapping_standby(self):
        decision = _make_decision(funnel_stage=FunnelStage.STANDBY)
        result = adapt_srm_decision(decision)
        assert result.stage == "escalation"

    def test_stage_mapping_inativo(self):
        decision = _make_decision(funnel_stage=FunnelStage.INATIVO)
        result = adapt_srm_decision(decision)
        assert result.stage == "initial"

    def test_stage_mapping_passthrough(self):
        """Stages not in the map pass through as-is."""
        for stage in [
            FunnelStage.EM_QUALIFICACAO,
            FunnelStage.QUALIFICADO,
            FunnelStage.FOLLOWUP,
            FunnelStage.AGENDAMENTO_SOLICITADO,
            FunnelStage.AGENDADO,
            FunnelStage.PACIENTE_ATIVO,
            FunnelStage.POS_ATENDIMENTO,
            FunnelStage.FIDELIZACAO,
        ]:
            decision = _make_decision(funnel_stage=stage)
            result = adapt_srm_decision(decision)
            assert (
                result.stage == stage.value
            ), f"Expected {stage.value}, got {result.stage}"

    def test_constraints_from_state_patch(self):
        decision = _make_decision(
            state_patch=StatePatch(
                constraints=["C_ESCALATE_TO_HUMAN", "C_POST_PROCEDURE_SAFETY"]
            )
        )
        result = adapt_srm_decision(decision)
        assert "C_ESCALATE_TO_HUMAN" in result.constraints
        assert "C_POST_PROCEDURE_SAFETY" in result.constraints

    def test_gate_authority_before_price(self):
        decision = _make_decision(
            intent="pedido_preco",
            state_patch=StatePatch(constraints=["C_SEQUENCE_AUTHORITY_BEFORE_PRICE"]),
        )
        result = adapt_srm_decision(decision)
        assert result.gate_authority_before_price is True

    def test_gate_authority_false_without_constraint(self):
        decision = _make_decision(intent="pedido_preco")
        result = adapt_srm_decision(decision)
        assert result.gate_authority_before_price is False


# ============================================
# generate_selected_context tests
# ============================================


class TestGenerateSelectedContext:
    """Tests for generate_selected_context()."""

    def test_escalation_reclamacao(self):
        decision = _make_decision(
            intent="reclamacao",
            intent_category=IntentCategory.COMPLAINT,
            funnel_stage=FunnelStage.STANDBY,
            safety_concern=True,
            handoff_required=True,
        )
        items = generate_selected_context(decision)
        keys = [i.key for i in items]
        assert "escalation_notice" in keys
        notice = next(i for i in items if i.key == "escalation_notice")
        assert notice.source == "srm_engine"
        assert "empatia" in notice.value

    def test_escalation_conduta(self):
        decision = _make_decision(
            intent="conduta_inadequada_ameaca",
            intent_category=IntentCategory.COMPLAINT,
        )
        items = generate_selected_context(decision)
        keys = [i.key for i in items]
        assert "escalation_notice" in keys

    def test_escalation_pedido_humano(self):
        decision = _make_decision(
            intent="pedido_atendente_humano",
            intent_category=IntentCategory.COMPLAINT,
        )
        items = generate_selected_context(decision)
        keys = [i.key for i in items]
        assert "escalation_notice" in keys

    def test_scheduling_agendar(self):
        decision = _make_decision(
            intent="agendar",
            intent_category=IntentCategory.SCHEDULING,
            action_details="Coletar dia e turno",
        )
        items = generate_selected_context(decision)
        keys = [i.key for i in items]
        assert "scheduling_context" in keys
        ctx = next(i for i in items if i.key == "scheduling_context")
        assert ctx.value == "Coletar dia e turno"
        assert ctx.source == "srm_engine"

    def test_scheduling_reagendar(self):
        decision = _make_decision(intent="reagendar")
        items = generate_selected_context(decision)
        keys = [i.key for i in items]
        assert "scheduling_context" in keys

    def test_scheduling_cancelar(self):
        decision = _make_decision(intent="cancelar")
        items = generate_selected_context(decision)
        keys = [i.key for i in items]
        assert "scheduling_context" in keys

    def test_price_intent(self):
        decision = _make_decision(
            intent="pedido_preco",
            intent_category=IntentCategory.QUESTION,
            action_details="Gerar autoridade antes de apresentar preço",
        )
        items = generate_selected_context(decision)
        keys = [i.key for i in items]
        assert "authority_context" in keys
        ctx = next(i for i in items if i.key == "authority_context")
        assert "autoridade" in ctx.value

    def test_safety_intent(self):
        decision = _make_decision(
            intent="pos_atendimento_sintomas_preocupantes",
            safety_concern=True,
        )
        items = generate_selected_context(decision)
        keys = [i.key for i in items]
        assert "safety_alert" in keys
        alert = next(i for i in items if i.key == "safety_alert")
        assert alert.source == "safety_detector"
        assert "sintomas preocupantes" in alert.value

    def test_constraints_as_context(self):
        decision = _make_decision(
            state_patch=StatePatch(
                constraints=["C_ESCALATE_TO_HUMAN", "C_SEQUENCE_AUTHORITY_BEFORE_PRICE"]
            )
        )
        items = generate_selected_context(decision)
        constraint_items = [i for i in items if i.source == "policy"]
        assert len(constraint_items) == 2
        constraint_keys = {i.key for i in constraint_items}
        assert "C_ESCALATE_TO_HUMAN" in constraint_keys
        assert "C_SEQUENCE_AUTHORITY_BEFORE_PRICE" in constraint_keys

    def test_greeting_no_special_context(self):
        decision = _make_decision()
        items = generate_selected_context(decision)
        assert len(items) == 0

    def test_multiple_contexts_escalation_with_constraints(self):
        decision = _make_decision(
            intent="reclamacao",
            state_patch=StatePatch(constraints=["C_ESCALATE_TO_HUMAN"]),
        )
        items = generate_selected_context(decision)
        keys = [i.key for i in items]
        assert "escalation_notice" in keys
        assert "C_ESCALATE_TO_HUMAN" in keys
        assert len(items) >= 2


# ============================================
# build_decision_trace tests
# ============================================


class TestBuildDecisionTrace:
    """Tests for build_decision_trace()."""

    def test_complete_trace(self):
        decision = _make_decision(
            intent="reclamacao",
            intent_category=IntentCategory.COMPLAINT,
            funnel_stage=FunnelStage.STANDBY,
            action=ActionType.HANDOFF_GERENCIA,
            confidence=0.97,
            safety_concern=True,
            handoff_required=True,
            state_patch=StatePatch(constraints=["C_ESCALATE_TO_HUMAN"]),
        )
        trace = build_decision_trace(
            event_id="test-123",
            message="não gostei do resultado",
            decision=decision,
            prompt_built="<prompt>",
            llm_response="<response>",
            processing_time_ms=42,
        )

        assert isinstance(trace, FrontendDecisionTrace)
        assert trace.id == "test-123"
        assert trace.userMessage == "não gostei do resultado"
        assert trace.srmDecision.intent == "reclamacao"
        assert trace.srmDecision.stage == "escalation"
        assert trace.srmDecision.safety_concern is True
        assert trace.srmDecision.handoff_required is True
        assert "C_ESCALATE_TO_HUMAN" in trace.srmDecision.constraints
        assert trace.promptBuilt == "<prompt>"
        assert trace.llmResponse == "<response>"
        assert trace.processingTimeMs == 42
        assert len(trace.selectedContext) >= 2  # escalation_notice + constraint

    def test_trace_timestamp_present(self):
        decision = _make_decision()
        trace = build_decision_trace(
            event_id="ts-test",
            message="Oi",
            decision=decision,
        )
        assert trace.timestamp is not None
        assert "T" in trace.timestamp  # ISO format

    def test_trace_defaults(self):
        decision = _make_decision()
        trace = build_decision_trace(
            event_id="defaults",
            message="Oi",
            decision=decision,
        )
        assert trace.promptBuilt == ""
        assert trace.llmResponse == ""
        assert trace.processingTimeMs == 0
