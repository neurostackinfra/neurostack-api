"""
NeuroStack - SRM (Small Reasoning Module) - Main Engine
The core deterministic decision engine for cognitive processing.
"""

import time
from typing import Any, Optional
from uuid import UUID

from prometheus_client import Counter, Histogram

from src.srm.models import (
    SRMInput,
    SRMDecision,
    StatePatch,
    IntentCategory,
    FunnelStage,
    SafetyCategory,
    FlowMode,
    ActionType,
    HandoffReason,
)
from src.srm.intent_classifier import IntentClassifier, get_intent_classifier
from src.srm.funnel_detector import FunnelDetector, get_funnel_detector
from src.srm.safety_detector import SafetyDetector, get_safety_detector
from src.srm.handoff_decider import HandoffDecider, get_handoff_decider
from src.srm.rag_decider import RagDecider, get_rag_decider
from src.srm.models import SentimentLabel


# ============================================
# Prometheus Metrics
# ============================================

SRM_DECISIONS = Counter(
    "neurostack_srm_decisions_total",
    "Total number of SRM decisions",
    ["intent_category", "funnel_stage", "mode"],
)

SRM_LATENCY = Histogram(
    "neurostack_srm_latency_seconds",
    "SRM processing latency in seconds",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25),
)

SRM_SAFETY_TRIGGERS = Counter(
    "neurostack_srm_safety_triggers_total",
    "Total number of safety triggers",
    ["category"],
)

SRM_HANDOFFS = Counter(
    "neurostack_srm_handoffs_total",
    "Total number of handoff decisions",
    ["reason"],
)


# ============================================
# Action Mapping (Intent -> Action Catalog v0.1)
# ============================================

# Official intents -> official actions (doc MVP v0.5)
INTENT_ACTIONS: dict[str, ActionType] = {
    "saudacao": ActionType.PERGUNTA_UNICA_PARA_DESTRAVAR,
    "interesse_tratamento": ActionType.INICIAR_FLUXO_QUALIFICACAO_SRM,
    "pedido_preco": ActionType.INICIAR_FLUXO_QUALIFICACAO_SRM,
    "comparacao_tratamentos": ActionType.CONTINUAR_FLUXO_SRM,
    "objecao_vou_pensar": ActionType.CONTINUAR_FLUXO_SRM,
    "duvida_generica_sem_contexto": ActionType.CONTINUAR_FLUXO_SRM,
    "agendar": ActionType.COLETAR_PREFERENCIAS_AGENDAMENTO,
    "reagendar": ActionType.PROCESSAR_REAGENDAMENTO,
    "cancelar": ActionType.PROCESSAR_CANCELAMENTO,
    "confirmar_presenca": ActionType.PROCESSAR_CONFIRMACAO_PRESENCA,
    "disponibilidade_horarios": ActionType.CONSULTAR_RAG_RESPONDER,
    "detalhes_servicos": ActionType.CONSULTAR_RAG_RESPONDER,
    "pre_atendimento": ActionType.CONSULTAR_RAG_RESPONDER,
    "faq": ActionType.CONSULTAR_RAG_RESPONDER,
    "cultura_historia": ActionType.CONSULTAR_RAG_RESPONDER,
    "qualificacao_publico": ActionType.CONSULTAR_RAG_RESPONDER,
    "informacoes_profissionais": ActionType.CONSULTAR_RAG_RESPONDER,
    "documentos_e_comprovantes": ActionType.HANDOFF_ADMINISTRATIVO,
    "orcamento_existente": ActionType.HANDOFF_ADMINISTRATIVO,
    "cobranca_renegociacao": ActionType.HANDOFF_FINANCEIRO,
    "pos_atendimento_duvida": ActionType.CONSULTAR_RAG_E_SE_FALTAR_HANDOFF,
    "pos_atendimento_sintomas_preocupantes": ActionType.ATIVAR_MODO_SAFETY_E_HANDOFF,
    "pedido_alterar_medicacao": ActionType.ATIVAR_MODO_SAFETY_E_HANDOFF,
    "reclamacao": ActionType.HANDOFF_GERENCIA,
    "conduta_inadequada_ameaca": ActionType.ATIVAR_PROTOCOLO_CONDUTA_NADEQUADA,
}

# Intent category fallback when intent not in INTENT_ACTIONS
CATEGORY_ACTIONS: dict[IntentCategory, ActionType] = {
    IntentCategory.GREETING: ActionType.PERGUNTA_UNICA_PARA_DESTRAVAR,
    IntentCategory.FAREWELL: ActionType.CONTINUAR_FLUXO_SRM,
    IntentCategory.SCHEDULING: ActionType.COLETAR_PREFERENCIAS_AGENDAMENTO,
    IntentCategory.CANCELLATION: ActionType.PROCESSAR_CANCELAMENTO,
    IntentCategory.CONFIRMATION: ActionType.PROCESSAR_CONFIRMACAO_PRESENCA,
    IntentCategory.QUESTION: ActionType.CONSULTAR_RAG_RESPONDER,
    IntentCategory.COMPLAINT: ActionType.HANDOFF_GERENCIA,
    IntentCategory.INFORMATION: ActionType.CONTINUAR_FLUXO_SRM,
    IntentCategory.FEEDBACK: ActionType.CONTINUAR_FLUXO_SRM,
    IntentCategory.EMERGENCY: ActionType.ATIVAR_MODO_SAFETY_E_HANDOFF,
    IntentCategory.UNKNOWN: ActionType.CLARIFY,
}


class SRMEngine:
    """
    Small Reasoning Module - Main Engine.
    
    The SRM is the core deterministic decision engine that:
    - Classifies user intent
    - Detects funnel stage transitions
    - Evaluates safety concerns
    - Decides on handoff requirements
    - Determines RAG necessity
    - Generates state patches
    
    All decisions are rule-based and deterministic, not ML-based.
    """

    def __init__(
        self,
        confidence_threshold: float = 0.7,
        safety_threshold: float = 0.5,
        max_turns_for_handoff: int = 10,
    ):
        """
        Initialize the SRM engine.
        
        Args:
            confidence_threshold: Minimum confidence for high-confidence decisions
            safety_threshold: Minimum score to trigger safety concern
            max_turns_for_handoff: Maximum turns before suggesting handoff
        """
        self.confidence_threshold = confidence_threshold
        self.safety_threshold = safety_threshold
        self.max_turns_for_handoff = max_turns_for_handoff
        
        # Initialize sub-components
        self.intent_classifier = get_intent_classifier()
        self.funnel_detector = get_funnel_detector()
        self.safety_detector = get_safety_detector(threshold=safety_threshold)
        self.handoff_decider = get_handoff_decider(
            max_turns=max_turns_for_handoff,
            min_confidence=confidence_threshold,
        )
        self.rag_decider = get_rag_decider(confidence_threshold=confidence_threshold)

    def process(self, input_data: SRMInput) -> SRMDecision:
        """
        Process a message through the SRM pipeline.
        
        Args:
            input_data: SRM input with message and context
            
        Returns:
            SRMDecision with complete cognitive analysis
        """
        start_time = time.perf_counter()
        
        # Step 1: Classify intent
        intent_result = self.intent_classifier.classify(
            input_data.message, language=input_data.language
        )
        
        # Step 2: Detect safety concerns
        safety_result = self.safety_detector.detect(input_data.message)

        # Step 3: Detect funnel stage transition
        funnel_result = self.funnel_detector.detect(
            current_stage=input_data.current_funnel_stage,
            intent=intent_result.intent,
            intent_category=intent_result.category,
            turn_count=input_data.turn_count,
            slots=input_data.slots,
            flags=input_data.flags,
        )

        # Step 4: Check for handoff requirement
        handoff_result = self.handoff_decider.decide(
            message=input_data.message,
            intent=intent_result.intent,
            intent_category=intent_result.category,
            confidence=intent_result.confidence,
            turn_count=input_data.turn_count,
            safety_result=safety_result,
            funnel_stage=input_data.current_funnel_stage,
            flags=input_data.flags,
        )

        # Step 5: Decide on RAG
        rag_result = self.rag_decider.decide(
            message=input_data.message,
            intent=intent_result.intent,
            intent_category=intent_result.category,
            confidence=intent_result.confidence,
            funnel_stage=input_data.current_funnel_stage,
            flags=input_data.flags,
        )

        # Step 6: Determine action
        action = self._determine_action(
            intent=intent_result.intent,
            intent_category=intent_result.category,
            safety_result=safety_result,
            handoff_result=handoff_result,
        )

        # Step 7: Determine flow mode
        mode = self._determine_mode(
            safety_result=safety_result,
            handoff_result=handoff_result,
            rag_result=rag_result,
        )

        # Step 8: Generate state patch
        state_patch = self._generate_state_patch(
            intent_result=intent_result,
            funnel_result=funnel_result,
            safety_result=safety_result,
            action=action,
        )

        # Step 9: Generate reasoning
        reasoning = self._generate_reasoning(
            intent_result=intent_result,
            safety_result=safety_result,
            handoff_result=handoff_result,
            rag_result=rag_result,
        )
        
        # Calculate processing time
        processing_time_ms = int((time.perf_counter() - start_time) * 1000)
        
        # Determine final funnel stage
        final_funnel_stage = (
            funnel_result.next_stage
            if funnel_result.should_transition and funnel_result.next_stage
            else funnel_result.current_stage
        )

        # Escalation intents force STANDBY (100% human), safety_concern=True, handoff=True
        _ESCALATION_INTENTS = {"reclamacao", "conduta_inadequada_ameaca", "pedido_atendente_humano"}
        is_escalation = intent_result.intent in _ESCALATION_INTENTS
        if is_escalation:
            final_funnel_stage = FunnelStage.STANDBY
            handoff_result.required = True
            handoff_result.reason = HandoffReason.ESCALATION
            handoff_result.urgency = "prioritario"
            if not handoff_result.sector:
                handoff_result.sector = "gerencia"
            if not handoff_result.sla:
                handoff_result.sla = "prioritario"
            mode = FlowMode.HANDOFF

        # Clinical-safety intents force safety_concern=True, handoff=True
        _CLINICAL_SAFETY_INTENTS = {"pos_atendimento_sintomas_preocupantes", "pedido_alterar_medicacao"}
        is_clinical_safety = intent_result.intent in _CLINICAL_SAFETY_INTENTS
        if is_clinical_safety:
            handoff_result.required = True
            if not handoff_result.reason:
                handoff_result.reason = HandoffReason.SAFETY_CONCERN
            handoff_result.urgency = handoff_result.urgency or "alto"
            if not handoff_result.sector:
                handoff_result.sector = "clinico"
            if not handoff_result.sla:
                handoff_result.sla = "prioritario"
            mode = FlowMode.SAFETY

        # Safety concern: safety_detector OR escalation OR clinical-safety intent
        safety_concern = (not safety_result.is_safe) or is_escalation or is_clinical_safety
        safety_category = (
            SafetyCategory.CONDUCT_RISK
            if is_escalation and safety_result.is_safe
            else SafetyCategory.CLINICAL_RISK
            if is_clinical_safety and safety_result.is_safe
            else safety_result.category
        )

        # Action details: include escalation_notice for escalation intents
        action_details = self._get_action_details(action, intent_result.intent)
        if is_escalation:
            action_details = (
                "escalation_notice: O paciente expressou insatisfação. "
                "Priorize empatia, acolhimento e encaminhe para atendimento humano imediatamente."
            )

        # Build decision
        decision = SRMDecision(
            intent=intent_result.intent,
            intent_category=intent_result.category,
            funnel_stage=final_funnel_stage,
            funnel_transition="Escalation: handoff to human" if is_escalation else (funnel_result.transition_reason if funnel_result.should_transition else None),
            action=action,
            action_details=action_details,
            confidence=intent_result.confidence,
            safety_concern=safety_concern,
            safety_category=safety_category,
            sentiment_label=SentimentLabel.NEUTRAL,
            sentiment_score=0.0,
            sentiment_confidence=0.0,
            mode=mode,
            needs_rag=rag_result.needs_rag,
            handoff_required=handoff_result.required,
            handoff_reason=handoff_result.reason,
            handoff_urgency=handoff_result.urgency,
            handoff_sector=handoff_result.sector,
            handoff_sla=handoff_result.sla,
            state_patch=state_patch,
            reasoning=reasoning,
            processing_time_ms=processing_time_ms,
        )
        
        # Record metrics
        self._record_metrics(decision)
        
        return decision

    def _determine_action(
        self,
        intent: str,
        intent_category: IntentCategory,
        safety_result,
        handoff_result,
    ) -> ActionType:
        """Determine the action based on intent and context (Action Catalog v0.1)."""
        # Handoff required -> use intent-specific handoff if available, else fallback
        if handoff_result.required:
            if intent in INTENT_ACTIONS:
                return INTENT_ACTIONS[intent]
            return ActionType.HANDOFF_ADMINISTRATIVO
        
        # Safety concerns -> safety + handoff
        if not safety_result.is_safe:
            if safety_result.category == SafetyCategory.CLINICAL_RISK:
                return ActionType.ATIVAR_MODO_SAFETY_E_HANDOFF
            if safety_result.category == SafetyCategory.CONDUCT_RISK:
                return ActionType.ATIVAR_PROTOCOLO_CONDUTA_NADEQUADA
            if safety_result.action == "block":
                return ActionType.RESPOND
        
        # Intent-specific actions (official 23 intents)
        if intent in INTENT_ACTIONS:
            return INTENT_ACTIONS[intent]
        
        return CATEGORY_ACTIONS.get(intent_category, ActionType.CONTINUAR_FLUXO_SRM)

    def _determine_mode(
        self,
        safety_result,
        handoff_result,
        rag_result,
    ) -> FlowMode:
        """Determine the flow mode."""
        if handoff_result.required:
            return FlowMode.HANDOFF
        
        if not safety_result.is_safe:
            return FlowMode.SAFETY
        
        if rag_result.needs_rag:
            return FlowMode.RAG
        
        return FlowMode.NORMAL

    def _generate_state_patch(
        self,
        intent_result,
        funnel_result,
        safety_result,
        action: ActionType,
    ) -> StatePatch:
        """Generate state changes to apply to session (UserState doc)."""
        patch = StatePatch(
            last_intent=intent_result.intent,
            last_action=action.value,
            confidence=intent_result.confidence,
        )

        # Update funnel stage if transitioning
        if funnel_result.should_transition and funnel_result.next_stage:
            patch.funnel_stage = funnel_result.next_stage.value

        # Set safety trigger flag
        if not safety_result.is_safe:
            patch.safety_trigger = True
            patch.flags["safety_triggered"] = True
            patch.flags[f"safety_{safety_result.category.value}"] = True

        # Update slots and flags (incl. objection_active, authority_done)
        self._extract_slots(intent_result.intent, patch)

        return patch

    def _extract_slots(self, intent: str, patch: StatePatch) -> None:
        """Extract slot updates based on intent (official 29 intents)."""
        if intent == "agendar":
            patch.flags["scheduling_interest"] = True
        if intent == "pedido_preco":
            patch.constraints.append("C_SEQUENCE_AUTHORITY_BEFORE_PRICE")
        if intent == "confirmar_presenca":
            patch.flags["confirmation_pending"] = False
        if intent == "objecao_vou_pensar":
            patch.flags["objection_active"] = True
        if intent in ("documentos_e_comprovantes", "orcamento_existente", "cobranca_renegociacao"):
            patch.flags["handoff_requested"] = True
        # Complaints and threats → escalation constraint + handoff flag
        if intent in ("reclamacao", "conduta_inadequada_ameaca"):
            patch.constraints.append("C_ESCALATE_TO_HUMAN")
            patch.flags["handoff_requested"] = True
            patch.flags["escalation_required"] = True
        if intent == "pedido_atendente_humano":
            patch.constraints.append("C_ESCALATE_TO_HUMAN")
            patch.flags["handoff_requested"] = True
        if intent == "pos_atendimento_sintomas_preocupantes":
            patch.constraints.append("C_POST_PROCEDURE_SAFETY")

    def _get_action_details(self, action: ActionType, intent: str) -> str:
        """Get detailed description of the action (Action Catalog v0.1)."""
        details = {
            ActionType.PERGUNTAR_SE_JA_E_PACIENTE: "Perguntar se o usuário já é paciente",
            ActionType.PERGUNTAR_NOME: "Perguntar nome do usuário",
            ActionType.PERGUNTA_UNICA_PARA_DESTRAVAR: "Perguntar objetivo do contato",
            ActionType.INICIAR_FLUXO_QUALIFICACAO_SRM: "Iniciar fluxo de qualificação no SRM",
            ActionType.CONTINUAR_FLUXO_SRM: "Continuar fluxo SRM",
            ActionType.SOLICITAR_DECISAO_AGENDAMENTO: "Solicitar decisão de agendamento",
            ActionType.DEFINIR_FUNNEL_STAGE: "Definir etapa do funil",
            ActionType.COLETAR_PREFERENCIAS_AGENDAMENTO: "Coletar preferências de agendamento",
            ActionType.ROTEAR_PARA_AGENTE_AGENDAMENTO: "Rotear para agente de agendamento",
            ActionType.HANDOFF_AGENDAMENTO: "Handoff para setor agendamento",
            ActionType.PROCESSAR_CANCELAMENTO: "Processar cancelamento e handoff",
            ActionType.PROCESSAR_REAGENDAMENTO: "Processar reagendamento e handoff",
            ActionType.PROCESSAR_CONFIRMACAO_PRESENCA: "Processar confirmação de presença",
            ActionType.CONSULTAR_RAG_RESPONDER: "Consultar RAG e responder",
            ActionType.CONSULTAR_RAG_E_SE_FALTAR_HANDOFF: "Consultar RAG; handoff se faltar",
            ActionType.HANDOFF_FINANCEIRO: "Handoff financeiro",
            ActionType.HANDOFF_ADMINISTRATIVO: "Handoff administrativo",
            ActionType.HANDOFF_GERENCIA: "Handoff gerência",
            ActionType.HANDOFF_CLINICO: "Handoff clínico",
            ActionType.HANDOFF_URGENCIA: "Handoff urgência",
            ActionType.ATIVAR_MODO_SAFETY_E_HANDOFF: "Ativar modo safety e handoff",
            ActionType.ATIVAR_PROTOCOLO_CONDUTA_NADEQUADA: "Ativar protocolo conduta inadequada",
            ActionType.ENFILEIRAR_HANDOFF_FORA_DO_HORARIO: "Enfileirar handoff fora do horário",
            ActionType.RESPOND: "Responder ao usuário",
            ActionType.CLARIFY: "Esclarecer intenção do usuário",
        }
        return details.get(action, "")

    def _generate_reasoning(
        self,
        intent_result,
        safety_result,
        handoff_result,
        rag_result,
    ) -> str:
        """Generate a brief reasoning explanation."""
        parts = []

        # Intent reasoning
        parts.append(
            f"Detected intent '{intent_result.intent}' "
            f"({intent_result.category.value}) with {intent_result.confidence:.0%} confidence"
        )

        # Safety reasoning
        if not safety_result.is_safe:
            parts.append(
                f"Safety concern: {safety_result.category.value} "
                f"(triggers: {', '.join(safety_result.triggers[:2])})"
            )

        # Handoff reasoning
        if handoff_result.required:
            parts.append(f"Handoff required: {handoff_result.reason.value}")

        # RAG reasoning
        if rag_result.needs_rag:
            parts.append(f"RAG needed: {rag_result.reason}")

        return ". ".join(parts)

    def _record_metrics(self, decision: SRMDecision) -> None:
        """Record Prometheus metrics."""
        SRM_DECISIONS.labels(
            intent_category=decision.intent_category.value,
            funnel_stage=decision.funnel_stage.value,
            mode=decision.mode.value,
        ).inc()
        
        if decision.processing_time_ms:
            SRM_LATENCY.observe(decision.processing_time_ms / 1000)
        
        if decision.safety_concern:
            SRM_SAFETY_TRIGGERS.labels(
                category=decision.safety_category.value
            ).inc()
        
        if decision.handoff_required and decision.handoff_reason:
            SRM_HANDOFFS.labels(reason=decision.handoff_reason.value).inc()


# ============================================
# Singleton Instance
# ============================================

_engine: Optional[SRMEngine] = None


def get_srm_engine(
    confidence_threshold: float = 0.7,
    safety_threshold: float = 0.5,
    max_turns: int = 10,
) -> SRMEngine:
    """Get or create the global SRMEngine instance."""
    global _engine
    if _engine is None:
        _engine = SRMEngine(
            confidence_threshold=confidence_threshold,
            safety_threshold=safety_threshold,
            max_turns_for_handoff=max_turns,
        )
    return _engine


async def process_message(
    tenant_id: UUID,
    session_id: UUID,
    message: str,
    channel: str = "unknown",
    user_id: Optional[str] = None,
    current_funnel_stage: str = "lead_novo",
    turn_count: int = 0,
    intent_history: Optional[list[str]] = None,
    slots: Optional[dict[str, Any]] = None,
    flags: Optional[dict[str, bool]] = None,
) -> SRMDecision:
    """
    Convenience function to process a message through SRM.
    
    This is the main entry point for using SRM. It creates an SRMInput
    from the provided parameters and processes it through the SRM engine.
    
    Args:
        tenant_id: Tenant identifier
        session_id: Session identifier
        message: User message to process
        channel: Communication channel (e.g., "whatsapp", "web")
        user_id: Optional user identifier
        current_funnel_stage: Current funnel stage (default: "lead_novo")
        turn_count: Number of turns in the conversation
        intent_history: List of previous intents
        slots: Extracted slot values
        flags: Session flags
        
    Returns:
        SRMDecision with complete cognitive analysis
    """
    engine = get_srm_engine()
    
    input_data = SRMInput(
        tenant_id=tenant_id,
        session_id=session_id,
        message=message,
        channel=channel,
        user_id=user_id,
        current_funnel_stage=FunnelStage(current_funnel_stage),
        turn_count=turn_count,
        intent_history=intent_history or [],
        slots=slots or {},
        flags=flags or {},
    )
    
    return engine.process(input_data)

