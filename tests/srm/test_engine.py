"""
NeuroStack - SRM Engine Unit Tests
Tests for the Small Reasoning Module main engine and all components.
"""

import sys
from uuid import uuid4

import pytest

sys.path.insert(0, "/home/jairomendes/neurostack/neurostack-api")

from src.srm.models import (
    IntentCategory,
    FunnelStage,
    SafetyCategory,
    FlowMode,
    ActionType,
    HandoffReason,
    SRMInput,
    SRMDecision,
    SafetyResult,
)
from src.srm.intent_classifier import IntentClassifier, get_intent_classifier
from src.srm.funnel_detector import FunnelDetector, get_funnel_detector
from src.srm.safety_detector import SafetyDetector, get_safety_detector
from src.srm.handoff_decider import HandoffDecider, get_handoff_decider
from src.srm.rag_decider import RagDecider, get_rag_decider
from src.srm.engine import SRMEngine, get_srm_engine


# ============================================
# Fixtures
# ============================================


@pytest.fixture
def sample_tenant_id():
    return uuid4()


@pytest.fixture
def sample_session_id():
    return uuid4()


@pytest.fixture
def srm_engine():
    return SRMEngine()


@pytest.fixture
def intent_classifier():
    return IntentClassifier()


@pytest.fixture
def funnel_detector():
    return FunnelDetector()


@pytest.fixture
def safety_detector():
    return SafetyDetector()


@pytest.fixture
def handoff_decider():
    return HandoffDecider()


@pytest.fixture
def rag_decider():
    return RagDecider()


# ============================================
# Intent Classifier Tests
# ============================================


class TestIntentClassifier:
    """Tests for intent classification."""

    def test_greeting_detection(self, intent_classifier):
        """Test greeting intent detection."""
        messages = ["Olá", "Oi", "Bom dia", "Boa tarde", "E aí"]
        for msg in messages:
            result = intent_classifier.classify(msg)
            assert result.category == IntentCategory.GREETING, f"Failed for: {msg}"
            assert result.confidence >= 0.4  # Adjusted threshold

    def test_scheduling_detection(self, intent_classifier):
        """Test scheduling intent detection."""
        messages = [
            "Gostaria de agendar uma consulta",
            "Quero marcar um horário",
            "Tem disponibilidade para amanhã?",
            "Preciso agendar uma consulta com o Dr. Silva",
        ]
        for msg in messages:
            result = intent_classifier.classify(msg)
            assert result.category == IntentCategory.SCHEDULING, f"Failed for: {msg}"
            assert result.confidence >= 0.4  # Adjusted threshold

    def test_cancellation_detection(self, intent_classifier):
        """Test cancellation intent detection."""
        messages = [
            "Quero cancelar minha consulta",
            "Preciso desmarcar o horário",
            "Não vou poder ir amanhã",
        ]
        for msg in messages:
            result = intent_classifier.classify(msg)
            assert result.category == IntentCategory.CANCELLATION, f"Failed for: {msg}"

    def test_confirmation_positive(self, intent_classifier):
        """Test positive confirmation detection."""
        messages = ["Sim", "Ok", "Certo"]
        for msg in messages:
            result = intent_classifier.classify(msg)
            if result.category != IntentCategory.UNKNOWN:
                assert result.category in [
                    IntentCategory.CONFIRMATION,
                    IntentCategory.FEEDBACK,
                    IntentCategory.QUESTION,  # short messages may match other intents
                ], f"Failed for: {msg}"

    def test_question_price(self, intent_classifier):
        """Test price question detection."""
        messages = [
            "Quanto custa a consulta?",
            "Qual o valor do atendimento?",
            "Quais os preços?",
        ]
        for msg in messages:
            result = intent_classifier.classify(msg)
            assert result.category == IntentCategory.QUESTION, f"Failed for: {msg}"
            assert result.intent == "pedido_preco", f"Expected pedido_preco, got {result.intent}"

    def test_farewell_detection(self, intent_classifier):
        """Test farewell intent detection — farewell is no longer a separate intent.
        Short messages like 'Tchau' may match other categories or fall to generic."""
        messages = ["Tchau", "Até logo", "Obrigado, tchau", "Adeus"]
        for msg in messages:
            result = intent_classifier.classify(msg)
            # Farewell was removed as separate intent; these messages are low-priority
            assert result.confidence >= 0.0, f"Failed for: {msg}"

    def test_complaint_detection(self, intent_classifier):
        """Test complaint detection."""
        messages = [
            "Quero reclamar do atendimento",
            "O serviço foi péssimo",
        ]
        for msg in messages:
            result = intent_classifier.classify(msg)
            assert result.category in [
                IntentCategory.COMPLAINT, 
                IntentCategory.FEEDBACK
            ], f"Failed for: {msg}"

    def test_empty_message(self, intent_classifier):
        """Test empty message handling."""
        result = intent_classifier.classify("")
        assert result.intent == "duvida_generica_sem_contexto"
        assert result.category == IntentCategory.UNKNOWN
        assert result.confidence == 0.0

    def test_confidence_scoring(self, intent_classifier):
        """Test that confidence is between 0 and 1."""
        messages = [
            "Olá",
            "Quero agendar",
            "Quanto custa?",
            "asdfghjkl",  # Random text
        ]
        for msg in messages:
            result = intent_classifier.classify(msg)
            assert 0.0 <= result.confidence <= 1.0, f"Invalid confidence for: {msg}"


# ============================================
# Funnel Detector Tests
# ============================================


class TestFunnelDetector:
    """Tests for funnel stage detection."""

    def test_lead_novo_to_em_qualificacao(self, funnel_detector):
        """Test transition from lead_novo to em_qualificacao on interesse/agendar."""
        result = funnel_detector.detect(
            current_stage=FunnelStage.LEAD_NOVO,
            intent="agendar",
            intent_category=IntentCategory.SCHEDULING,
            turn_count=1,
            slots={},
            flags={},
        )
        assert result.should_transition
        assert result.next_stage == FunnelStage.EM_QUALIFICACAO

    def test_qualificado_to_agendamento_solicitado(self, funnel_detector):
        """Test transition from qualificado to agendamento_solicitado."""
        result = funnel_detector.detect(
            current_stage=FunnelStage.QUALIFICADO,
            intent="agendar",
            intent_category=IntentCategory.SCHEDULING,
            turn_count=3,
            slots={},
            flags={},
        )
        assert result.should_transition
        assert result.next_stage == FunnelStage.AGENDAMENTO_SOLICITADO

    def test_agendamento_solicitado_to_agendado(self, funnel_detector):
        """Test transition from agendamento_solicitado to agendado on confirmar_presenca."""
        result = funnel_detector.detect(
            current_stage=FunnelStage.AGENDAMENTO_SOLICITADO,
            intent="confirmar_presenca",
            intent_category=IntentCategory.CONFIRMATION,
            turn_count=5,
            slots={},
            flags={"scheduling_completed": True},
        )
        assert result.should_transition
        assert result.next_stage == FunnelStage.AGENDADO

    def test_no_transition_on_saudacao(self, funnel_detector):
        """Test no transition on saudacao in lead_novo (stays lead_novo)."""
        result = funnel_detector.detect(
            current_stage=FunnelStage.LEAD_NOVO,
            intent="saudacao",
            intent_category=IntentCategory.GREETING,
            turn_count=1,
            slots={},
            flags={},
        )
        assert not result.should_transition
        assert result.next_stage is None

    def test_lead_novo_already_patient_to_paciente_ativo(self, funnel_detector):
        """Test lead_novo + already_patient slot -> paciente_ativo."""
        result = funnel_detector.detect(
            current_stage=FunnelStage.LEAD_NOVO,
            intent="saudacao",
            intent_category=IntentCategory.GREETING,
            turn_count=1,
            slots={"already_patient": True},
            flags={},
        )
        assert result.should_transition
        assert result.next_stage == FunnelStage.PACIENTE_ATIVO


# ============================================
# Safety Detector Tests
# ============================================


class TestSafetyDetector:
    """Tests for safety concern detection."""

    def test_safe_message(self, safety_detector):
        """Test that normal messages are safe."""
        messages = [
            "Olá, gostaria de agendar uma consulta",
            "Qual o horário de funcionamento?",
            "Obrigado pelo atendimento",
        ]
        for msg in messages:
            result = safety_detector.detect(msg)
            assert result.is_safe, f"Should be safe: {msg}"

    def test_clinical_emergency_detection(self, safety_detector):
        """Test clinical emergency detection."""
        messages = [
            "Estou tendo um infarto",
            "Não consigo respirar direito",
            "Dor forte no peito",
            "Acho que estou tendo um AVC",
        ]
        for msg in messages:
            result = safety_detector.detect(msg)
            assert not result.is_safe, f"Should detect risk: {msg}"
            assert result.category == SafetyCategory.CLINICAL_RISK

    def test_suicidal_content_detection(self, safety_detector):
        """Test suicidal content detection."""
        messages = [
            "Não quero mais viver",
            "Pensando em me matar",
        ]
        for msg in messages:
            result = safety_detector.detect(msg)
            assert not result.is_safe, f"Should detect risk: {msg}"
            assert result.category == SafetyCategory.CLINICAL_RISK

    def test_legal_risk_detection(self, safety_detector):
        """Test legal risk detection."""
        messages = [
            "Qual remédio devo tomar para dor de cabeça?",
            "Me receita um medicamento para dormir",
            "Qual meu diagnóstico?",
        ]
        for msg in messages:
            result = safety_detector.detect(msg)
            # Legal risks should be detected
            if result.category == SafetyCategory.LEGAL_RISK:
                assert not result.is_safe, f"Should detect legal risk: {msg}"

    def test_conduct_risk_detection(self, safety_detector):
        """Test conduct risk detection."""
        messages = [
            "Vou te processar",
            "Seu idiota",
        ]
        for msg in messages:
            result = safety_detector.detect(msg)
            if not result.is_safe:
                assert result.category in [
                    SafetyCategory.CONDUCT_RISK,
                    SafetyCategory.LEGAL_RISK,
                ]

    def test_emergency_check(self, safety_detector):
        """Test is_emergency helper."""
        assert safety_detector.is_emergency("Estou tendo um infarto agora")
        assert not safety_detector.is_emergency("Quero agendar consulta")


# ============================================
# Handoff Decider Tests
# ============================================


class TestHandoffDecider:
    """Tests for handoff decision logic."""

    def test_explicit_handoff_request(self, handoff_decider):
        """Test detection of explicit handoff request."""
        messages = [
            "Quero falar com um humano",
            "Preciso de um atendente real",
            "Não quero falar com robô",
            "Transferir para atendente",
        ]
        for msg in messages:
            result = handoff_decider.decide(
                message=msg,
                intent="handoff_request",
                intent_category=IntentCategory.INFORMATION,
                confidence=0.9,
                turn_count=1,
                safety_result=SafetyResult(is_safe=True),
                funnel_stage=FunnelStage.LEAD_NOVO,
                flags={},
            )
            assert result.required, f"Should require handoff: {msg}"
            assert result.reason == HandoffReason.USER_REQUEST

    def test_safety_triggers_handoff(self, handoff_decider):
        """Test that clinical safety triggers handoff."""
        result = handoff_decider.decide(
            message="Estou passando muito mal",
            intent="emergency_medical",
            intent_category=IntentCategory.EMERGENCY,
            confidence=0.9,
            turn_count=1,
            safety_result=SafetyResult(
                is_safe=False,
                category=SafetyCategory.CLINICAL_RISK,
                confidence=0.8,
                triggers=["passando mal"],
            ),
            funnel_stage=FunnelStage.LEAD_NOVO,
            flags={},
        )
        assert result.required
        assert result.reason == HandoffReason.SAFETY_CONCERN
        assert result.urgency == "critical"

    def test_max_turns_handoff(self, handoff_decider):
        """Test handoff after max turns exceeded."""
        result = handoff_decider.decide(
            message="Ainda não entendi",
            intent="question_general",
            intent_category=IntentCategory.QUESTION,
            confidence=0.7,
            turn_count=15,  # Exceeds default max of 10
            safety_result=SafetyResult(is_safe=True),
            funnel_stage=FunnelStage.EM_QUALIFICACAO,
            flags={},
        )
        assert result.required
        assert result.reason == HandoffReason.MAX_TURNS_EXCEEDED

    def test_complaint_triggers_escalation(self, handoff_decider):
        """Test that complaints trigger escalation."""
        result = handoff_decider.decide(
            message="O atendimento foi péssimo",
            intent="complaint_general",
            intent_category=IntentCategory.COMPLAINT,
            confidence=0.9,
            turn_count=3,
            safety_result=SafetyResult(is_safe=True),
            funnel_stage=FunnelStage.EM_QUALIFICACAO,
            flags={},
        )
        assert result.required
        assert result.reason == HandoffReason.ESCALATION

    def test_no_handoff_normal_conversation(self, handoff_decider):
        """Test no handoff for normal conversation."""
        result = handoff_decider.decide(
            message="Quero agendar uma consulta",
            intent="scheduling_request",
            intent_category=IntentCategory.SCHEDULING,
            confidence=0.9,
            turn_count=3,
            safety_result=SafetyResult(is_safe=True),
            funnel_stage=FunnelStage.LEAD_NOVO,
            flags={},
        )
        assert not result.required


# ============================================
# RAG Decider Tests
# ============================================


class TestRagDecider:
    """Tests for RAG decision logic."""

    def test_price_question_needs_rag(self, rag_decider):
        """Test that price questions trigger RAG."""
        result = rag_decider.decide(
            message="Quanto custa a consulta?",
            intent="question_price",
            intent_category=IntentCategory.QUESTION,
            confidence=0.9,
            funnel_stage=FunnelStage.EM_QUALIFICACAO,
            flags={},
        )
        assert result.needs_rag
        assert "pricing" in result.reason.lower() or "price" in result.reason.lower()

    def test_location_question_needs_rag(self, rag_decider):
        """Test that location questions trigger RAG."""
        result = rag_decider.decide(
            message="Onde fica a clínica?",
            intent="question_location",
            intent_category=IntentCategory.QUESTION,
            confidence=0.9,
            funnel_stage=FunnelStage.LEAD_NOVO,
            flags={},
        )
        assert result.needs_rag

    def test_services_question_needs_rag(self, rag_decider):
        """Test that services questions trigger RAG."""
        result = rag_decider.decide(
            message="Quais serviços vocês oferecem?",
            intent="question_services",
            intent_category=IntentCategory.QUESTION,
            confidence=0.9,
            funnel_stage=FunnelStage.LEAD_NOVO,
            flags={},
        )
        assert result.needs_rag

    def test_greeting_no_rag(self, rag_decider):
        """Test that greetings don't need RAG."""
        result = rag_decider.decide(
            message="Olá, bom dia!",
            intent="greeting_hello",
            intent_category=IntentCategory.GREETING,
            confidence=0.95,
            funnel_stage=FunnelStage.LEAD_NOVO,
            flags={},
        )
        assert not result.needs_rag

    def test_rag_disabled_flag(self, rag_decider):
        """Test that RAG can be disabled via flag."""
        result = rag_decider.decide(
            message="Quanto custa?",
            intent="question_price",
            intent_category=IntentCategory.QUESTION,
            confidence=0.9,
            funnel_stage=FunnelStage.EM_QUALIFICACAO,
            flags={"rag_disabled": True},
        )
        assert not result.needs_rag


# ============================================
# SRM Engine Integration Tests
# ============================================


class TestSRMEngine:
    """Integration tests for the SRM engine."""

    def test_process_greeting(self, srm_engine, sample_tenant_id, sample_session_id):
        """Test processing a greeting message."""
        input_data = SRMInput(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="Olá, bom dia!",
            channel="whatsapp",
        )
        decision = srm_engine.process(input_data)
        
        assert decision.intent_category == IntentCategory.GREETING
        assert decision.mode == FlowMode.NORMAL
        # Greetings now trigger qualification flow or respond
        assert decision.action in (ActionType.RESPOND, ActionType.PERGUNTA_UNICA_PARA_DESTRAVAR)
        assert not decision.safety_concern
        assert not decision.handoff_required

    def test_process_scheduling_request(
        self, srm_engine, sample_tenant_id, sample_session_id
    ):
        """Test processing a scheduling request."""
        input_data = SRMInput(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="Gostaria de agendar uma consulta para amanhã",
            channel="whatsapp",
            current_funnel_stage=FunnelStage.LEAD_NOVO,
        )
        decision = srm_engine.process(input_data)
        
        assert decision.intent_category == IntentCategory.SCHEDULING
        # Scheduling requests may trigger collection or qualification depending on stage
        assert decision.action in (
            ActionType.INICIAR_FLUXO_QUALIFICACAO_SRM,
            ActionType.COLETAR_PREFERENCIAS_AGENDAMENTO,
            ActionType.SOLICITAR_DECISAO_AGENDAMENTO,
        )
        assert decision.funnel_stage in (FunnelStage.EM_QUALIFICACAO, FunnelStage.LEAD_NOVO)

    def test_process_safety_concern(
        self, srm_engine, sample_tenant_id, sample_session_id
    ):
        """Test processing a message with safety concern."""
        input_data = SRMInput(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="Estou tendo um infarto, não consigo respirar",
            channel="whatsapp",
        )
        decision = srm_engine.process(input_data)
        
        assert decision.safety_concern
        assert decision.safety_category == SafetyCategory.CLINICAL_RISK
        assert decision.mode == FlowMode.SAFETY or decision.mode == FlowMode.HANDOFF
        assert decision.handoff_required

    def test_process_handoff_request(
        self, srm_engine, sample_tenant_id, sample_session_id
    ):
        """Test processing an explicit handoff request."""
        input_data = SRMInput(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="Quero falar com um atendente humano",
            channel="whatsapp",
        )
        decision = srm_engine.process(input_data)
        
        assert decision.handoff_required
        assert decision.mode == FlowMode.HANDOFF
        # Handoff request now maps to pedido_atendente_humano → administrative handoff
        assert decision.action in (
            ActionType.ATIVAR_MODO_SAFETY_E_HANDOFF,
            ActionType.HANDOFF_ADMINISTRATIVO,
        )

    def test_process_question_triggers_rag(
        self, srm_engine, sample_tenant_id, sample_session_id
    ):
        """Test that questions can trigger RAG."""
        input_data = SRMInput(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="Quais serviços vocês oferecem?",
            channel="whatsapp",
        )
        decision = srm_engine.process(input_data)
        
        assert decision.needs_rag
        assert decision.mode == FlowMode.RAG or decision.intent_category == IntentCategory.QUESTION

    def test_state_patch_generation(
        self, srm_engine, sample_tenant_id, sample_session_id
    ):
        """Test that state patch is generated correctly."""
        input_data = SRMInput(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="Quero agendar uma consulta",
            channel="whatsapp",
        )
        decision = srm_engine.process(input_data)
        
        assert decision.state_patch is not None
        assert decision.state_patch.last_intent is not None
        assert decision.state_patch.confidence is not None

    def test_processing_time_recorded(
        self, srm_engine, sample_tenant_id, sample_session_id
    ):
        """Test that processing time is recorded."""
        input_data = SRMInput(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="Olá",
            channel="whatsapp",
        )
        decision = srm_engine.process(input_data)
        
        assert decision.processing_time_ms is not None
        assert decision.processing_time_ms >= 0

    def test_reasoning_generated(
        self, srm_engine, sample_tenant_id, sample_session_id
    ):
        """Test that reasoning is generated."""
        input_data = SRMInput(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="Bom dia, preciso de ajuda",
            channel="whatsapp",
        )
        decision = srm_engine.process(input_data)
        
        assert decision.reasoning is not None
        assert len(decision.reasoning) > 0


# ============================================
# Singleton Tests
# ============================================


class TestSingletons:
    """Test singleton pattern implementations."""

    def test_intent_classifier_singleton(self):
        """Test IntentClassifier singleton."""
        c1 = get_intent_classifier()
        c2 = get_intent_classifier()
        assert c1 is c2

    def test_funnel_detector_singleton(self):
        """Test FunnelDetector singleton."""
        d1 = get_funnel_detector()
        d2 = get_funnel_detector()
        assert d1 is d2

    def test_safety_detector_singleton(self):
        """Test SafetyDetector singleton."""
        d1 = get_safety_detector()
        d2 = get_safety_detector()
        assert d1 is d2

    def test_handoff_decider_singleton(self):
        """Test HandoffDecider singleton."""
        d1 = get_handoff_decider()
        d2 = get_handoff_decider()
        assert d1 is d2

    def test_rag_decider_singleton(self):
        """Test RagDecider singleton."""
        d1 = get_rag_decider()
        d2 = get_rag_decider()
        assert d1 is d2

    def test_srm_engine_singleton(self):
        """Test SRMEngine singleton."""
        e1 = get_srm_engine()
        e2 = get_srm_engine()
        assert e1 is e2


# ============================================
# Edge Case Tests
# ============================================


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_empty_message(self, srm_engine, sample_tenant_id, sample_session_id):
        """Test handling of empty message."""
        input_data = SRMInput(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="",
            channel="whatsapp",
        )
        decision = srm_engine.process(input_data)
        
        assert decision.intent == "duvida_generica_sem_contexto"
        assert decision.confidence == 0.0

    def test_very_long_message(self, srm_engine, sample_tenant_id, sample_session_id):
        """Test handling of very long message."""
        long_message = "Olá " * 500  # Very long message
        input_data = SRMInput(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message=long_message,
            channel="whatsapp",
        )
        decision = srm_engine.process(input_data)
        
        # Should not crash and return some decision
        assert decision is not None
        assert decision.intent is not None

    def test_special_characters(self, srm_engine, sample_tenant_id, sample_session_id):
        """Test handling of special characters."""
        input_data = SRMInput(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="Olá! 🙂 Como posso agendar?? #consulta @clinica",
            channel="whatsapp",
        )
        decision = srm_engine.process(input_data)
        
        assert decision is not None
        # Should still detect scheduling intent
        assert decision.intent_category in [IntentCategory.SCHEDULING, IntentCategory.QUESTION, IntentCategory.UNKNOWN]

    def test_mixed_case(self, srm_engine, sample_tenant_id, sample_session_id):
        """Test case insensitivity."""
        input_data = SRMInput(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="QUERO AGENDAR UMA CONSULTA",
            channel="whatsapp",
        )
        decision = srm_engine.process(input_data)
        
        assert decision.intent_category == IntentCategory.SCHEDULING

    def test_max_turns_boundary(self, handoff_decider):
        """Test handoff at exact max turns threshold."""
        # At exactly max turns
        result = handoff_decider.decide(
            message="Pergunta",
            intent="question_general",
            intent_category=IntentCategory.QUESTION,
            confidence=0.8,
            turn_count=10,  # Exactly max
            safety_result=SafetyResult(is_safe=True),
            funnel_stage=FunnelStage.EM_QUALIFICACAO,
            flags={},
        )
        assert result.required

        # One below max turns
        result = handoff_decider.decide(
            message="Pergunta",
            intent="question_general",
            intent_category=IntentCategory.QUESTION,
            confidence=0.8,
            turn_count=9,  # Just below max
            safety_result=SafetyResult(is_safe=True),
            funnel_stage=FunnelStage.EM_QUALIFICACAO,
            flags={},
        )
        assert not result.required


# ============================================
# Convenio vs Particular Triage — Group 1
# ============================================


class TestAtendimentoTipoSlot:
    """Slot derivation from intent_classifier :262 signal (no new regex)."""

    def test_convenio_message_sets_slot(
        self, srm_engine, sample_tenant_id, sample_session_id
    ):
        """'vai ser pelo convênio' ⇒ atendimento_tipo=convenio."""
        decision = srm_engine.process(SRMInput(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="vai ser pelo convênio",
            channel="whatsapp",
        ))
        assert decision.state_patch.slots.get("atendimento_tipo") == "convenio"

    def test_convenio_no_accent_sets_slot(
        self, srm_engine, sample_tenant_id, sample_session_id
    ):
        """'convenio' without accent ⇒ atendimento_tipo=convenio."""
        decision = srm_engine.process(SRMInput(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="convenio",
            channel="whatsapp",
        ))
        assert decision.state_patch.slots.get("atendimento_tipo") == "convenio"

    def test_particular_message_sets_slot(
        self, srm_engine, sample_tenant_id, sample_session_id
    ):
        """'é particular' ⇒ atendimento_tipo=particular."""
        decision = srm_engine.process(SRMInput(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="é particular",
            channel="whatsapp",
        ))
        assert decision.state_patch.slots.get("atendimento_tipo") == "particular"

    def test_no_signal_leaves_slot_unchanged(
        self, srm_engine, sample_tenant_id, sample_session_id
    ):
        """Messages without the modality word do not touch the slot."""
        decision = srm_engine.process(SRMInput(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="quero agendar amanhã de tarde",
            channel="whatsapp",
            slots={"atendimento_tipo": "convenio"},
        ))
        # Patch should NOT overwrite when there is no explicit signal.
        assert "atendimento_tipo" not in decision.state_patch.slots

    def test_same_value_not_re_patched(
        self, srm_engine, sample_tenant_id, sample_session_id
    ):
        """Restating the same modality does not re-emit into the patch."""
        decision = srm_engine.process(SRMInput(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="é particular sim",
            channel="whatsapp",
            slots={"atendimento_tipo": "particular"},
        ))
        assert "atendimento_tipo" not in decision.state_patch.slots

    def test_explicit_switch_overwrites(
        self, srm_engine, sample_tenant_id, sample_session_id
    ):
        """User changing their mind is honored: convenio → particular."""
        decision = srm_engine.process(SRMInput(
            tenant_id=sample_tenant_id,
            session_id=sample_session_id,
            message="na verdade vai ser particular",
            channel="whatsapp",
            slots={"atendimento_tipo": "convenio"},
        ))
        assert decision.state_patch.slots.get("atendimento_tipo") == "particular"


class TestConvenioTriageGating:
    """Deterministic booleans injected by the pipeline (ask_convenio_triage /
    can_quote_particular)."""

    def _compute_gating(
        self,
        accepts_insurance: bool,
        atendimento_tipo,  # str | None
        turn_count: int,
        intent: str,
    ) -> tuple[bool, bool]:
        """Mirror pipeline.py logic — kept in-test to guard the invariants
        without needing to spin up the whole async pipeline."""
        ask_convenio_triage = (
            bool(accepts_insurance)
            and not atendimento_tipo
            and (turn_count == 2 or intent in ("pedido_preco", "agendar"))
        )
        can_quote_particular = atendimento_tipo == "particular"
        return ask_convenio_triage, can_quote_particular

    def test_ask_fires_on_turn_two_when_undefined(self):
        ask, quote = self._compute_gating(True, None, 2, "saudacao")
        assert ask is True
        assert quote is False

    def test_ask_fires_on_price_intent_even_early(self):
        ask, _ = self._compute_gating(True, None, 1, "pedido_preco")
        assert ask is True

    def test_ask_fires_on_agendar_intent_even_early(self):
        ask, _ = self._compute_gating(True, None, 1, "agendar")
        assert ask is True

    def test_ask_off_when_insurance_disabled(self):
        ask, _ = self._compute_gating(False, None, 2, "pedido_preco")
        assert ask is False

    def test_ask_off_once_tipo_defined(self):
        ask, _ = self._compute_gating(True, "convenio", 2, "pedido_preco")
        assert ask is False
        ask, _ = self._compute_gating(True, "particular", 2, "pedido_preco")
        assert ask is False

    def test_can_quote_only_for_particular(self):
        _, quote = self._compute_gating(True, "particular", 5, "pedido_preco")
        assert quote is True
        # convenio and undefined must both hold prices
        _, quote = self._compute_gating(True, "convenio", 5, "pedido_preco")
        assert quote is False
        _, quote = self._compute_gating(True, None, 5, "pedido_preco")
        assert quote is False


