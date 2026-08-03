"""
NeuroStack — Trace Adapter
Maps SRM engine output to the frontend DecisionTrace contract.
"""

from datetime import datetime, timezone
from typing import Optional

from pydantic import BaseModel, Field

from src.observability.pii import mask_carteirinha
from src.srm.models import SRMDecision


# ============================================
# Frontend-compatible schemas
# ============================================


class FrontendSRMDecision(BaseModel):
    """Maps to frontend SRMDecision interface."""

    intent: str
    stage: str
    action: str
    constraints: list[str] = Field(default_factory=list)
    gate_authority_before_price: bool = False
    safety_concern: bool = False
    handoff_required: bool = False
    confidence: float = 0.0


class FrontendSelectedContext(BaseModel):
    """Maps to frontend SelectedContext interface."""

    key: str
    value: str
    source: str


class FrontendDecisionTrace(BaseModel):
    """Maps to frontend DecisionTrace interface."""

    id: str
    timestamp: str
    userMessage: str
    srmDecision: FrontendSRMDecision
    selectedContext: list[FrontendSelectedContext] = Field(default_factory=list)
    promptBuilt: str = ""
    llmResponse: str = ""
    processingTimeMs: int = 0


# ============================================
# Stage mapping
# ============================================

_STAGE_MAP = {
    "lead_novo": "initial",
    "standby": "escalation",
    "inativo": "initial",
}


def _map_stage(funnel_stage_value: str) -> str:
    return _STAGE_MAP.get(funnel_stage_value, funnel_stage_value)


# ============================================
# Adapter functions
# ============================================


def adapt_srm_decision(decision: SRMDecision) -> FrontendSRMDecision:
    """Convert SRM engine decision to frontend format."""
    constraints = list(decision.state_patch.constraints)

    return FrontendSRMDecision(
        intent=decision.intent,
        stage=_map_stage(decision.funnel_stage.value),
        action=decision.action.value,
        constraints=constraints,
        gate_authority_before_price="C_SEQUENCE_AUTHORITY_BEFORE_PRICE" in constraints,
        safety_concern=decision.safety_concern,
        handoff_required=decision.handoff_required,
        confidence=decision.confidence,
    )


_ESCALATION_INTENTS = {"reclamacao", "conduta_inadequada_ameaca", "pedido_atendente_humano"}
_SCHEDULING_INTENTS = {"agendar", "reagendar", "cancelar", "confirmar_presenca", "disponibilidade_horarios"}

_CONSTRAINT_DESCRIPTIONS = {
    "C_ESCALATE_TO_HUMAN": "Encaminhar para atendimento humano imediatamente",
    "C_SEQUENCE_AUTHORITY_BEFORE_PRICE": "Gerar autoridade antes de apresentar preço",
    "C_POST_PROCEDURE_SAFETY": "Protocolo de segurança pós-procedimento",
}


def generate_selected_context(decision: SRMDecision) -> list[FrontendSelectedContext]:
    """Generate selectedContext items from SRM decision."""
    items: list[FrontendSelectedContext] = []

    intent = decision.intent

    if intent in _ESCALATION_INTENTS:
        items.append(FrontendSelectedContext(
            key="escalation_notice",
            value="O paciente expressou insatisfação. Priorize empatia, acolhimento e encaminhe para atendimento humano imediatamente.",
            source="srm_engine",
        ))

    if intent in _SCHEDULING_INTENTS:
        items.append(FrontendSelectedContext(
            key="scheduling_context",
            value=decision.action_details or "Coletar preferências de agendamento",
            source="srm_engine",
        ))

    if intent == "pedido_preco":
        items.append(FrontendSelectedContext(
            key="authority_context",
            value=decision.action_details or "Gerar autoridade antes de apresentar preço",
            source="srm_engine",
        ))

    if intent == "pos_atendimento_sintomas_preocupantes":
        items.append(FrontendSelectedContext(
            key="safety_alert",
            value="Paciente relatou sintomas preocupantes. Protocolo de segurança ativado.",
            source="safety_detector",
        ))

    for constraint in decision.state_patch.constraints:
        items.append(FrontendSelectedContext(
            key=constraint,
            value=_CONSTRAINT_DESCRIPTIONS.get(constraint, constraint),
            source="policy",
        ))

    return items


def build_decision_trace(
    event_id: str,
    message: str,
    decision: SRMDecision,
    prompt_built: str = "",
    llm_response: str = "",
    processing_time_ms: int = 0,
) -> FrontendDecisionTrace:
    """Build a complete DecisionTrace from SRM output.

    Carteirinha numbers are masked in every free-text field that surfaces
    to the frontend trace view (user message, built prompt, LLM
    response) so the raw PII never lands in observability artifacts.
    """
    return FrontendDecisionTrace(
        id=event_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        userMessage=mask_carteirinha(message) or "",
        srmDecision=adapt_srm_decision(decision),
        selectedContext=generate_selected_context(decision),
        promptBuilt=mask_carteirinha(prompt_built) or "",
        llmResponse=mask_carteirinha(llm_response) or "",
        processingTimeMs=processing_time_ms,
    )
