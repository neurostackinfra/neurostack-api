"""
NeuroStack - SRM (Small Reasoning Module) - Models
Data models for SRM decision engine.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


# ============================================
# Enums
# ============================================


class IntentCategory(str, Enum):
    """High-level intent categories for classification."""

    SCHEDULING = "scheduling"
    QUESTION = "question"
    COMPLAINT = "complaint"
    GREETING = "greeting"
    FAREWELL = "farewell"
    CONFIRMATION = "confirmation"
    CANCELLATION = "cancellation"
    INFORMATION = "information"
    FEEDBACK = "feedback"
    EMERGENCY = "emergency"
    UNKNOWN = "unknown"


class FunnelStage(str, Enum):
    """
    Funil canônico de 11 etapas (MVP v0.5).
    Identificadores usados em eventos, state store, traces e integrações CRM.
    """

    LEAD_NOVO = "lead_novo"
    EM_QUALIFICACAO = "em_qualificacao"
    QUALIFICADO = "qualificado"
    FOLLOWUP = "followup"
    AGENDAMENTO_SOLICITADO = "agendamento_solicitado"
    AGENDADO = "agendado"
    PACIENTE_ATIVO = "paciente_ativo"
    POS_ATENDIMENTO = "pos_atendimento"
    FIDELIZACAO = "fidelizacao"
    INATIVO = "inativo"
    STANDBY = "standby"


class SafetyCategory(str, Enum):
    """Safety concern categories."""

    CLINICAL_RISK = "clinical_risk"
    LEGAL_RISK = "legal_risk"
    CONDUCT_RISK = "conduct_risk"
    NONE = "none"


class SentimentLabel(str, Enum):
    """Sentiment labels for user messages."""

    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"


class FlowMode(str, Enum):
    """Flow modes for conversation state."""

    NORMAL = "normal"
    RAG = "rag"
    SAFETY = "safety"
    HANDOFF = "handoff"


class HandoffReason(str, Enum):
    """Reasons for human handoff."""

    USER_REQUEST = "user_request"
    SAFETY_CONCERN = "safety_concern"
    MAX_TURNS_EXCEEDED = "max_turns_exceeded"
    LOW_CONFIDENCE = "low_confidence"
    COMPLEX_QUERY = "complex_query"
    ESCALATION = "escalation"
    SCHEDULING = "scheduling"


# 23 intenções canônicas (CTX/SRM Intents v0.1) - documentação MVP v0.5
OFFICIAL_INTENTS = [
    "saudacao",
    "interesse_tratamento",
    "pedido_preco",
    "comparacao_tratamentos",
    "objecao_vou_pensar",
    "duvida_generica_sem_contexto",
    "agendar",
    "reagendar",
    "cancelar",
    "confirmar_presenca",
    "disponibilidade_horarios",
    "detalhes_servicos",
    "pre_atendimento",
    "faq",
    "cultura_historia",
    "qualificacao_publico",
    "informacoes_profissionais",
    "documentos_e_comprovantes",
    "orcamento_existente",
    "cobranca_renegociacao",
    "pos_atendimento_duvida",
    "pos_atendimento_sintomas_preocupantes",
    "pedido_alterar_medicacao",
    "reclamacao",
    "conduta_inadequada_ameaca",
    "pedido_atendente_humano",
    "paciente_ativo",
    "fora_do_escopo",
    "fora_do_horario",
]


class ActionType(str, Enum):
    """
    Catálogo de ações oficiais (CTX Action Catalog v0.1).
    Substitui ações genéricas pelas ações da documentação.
    """

    # Coleta e clarificação
    PERGUNTAR_SE_JA_E_PACIENTE = "perguntar_se_ja_e_paciente"
    PERGUNTAR_NOME = "perguntar_nome"
    PERGUNTA_UNICA_PARA_DESTRAVAR = "pergunta_unica_para_destravar"
    # Roteamento SRM e funil
    INICIAR_FLUXO_QUALIFICACAO_SRM = "iniciar_fluxo_qualificacao_srm"
    CONTINUAR_FLUXO_SRM = "continuar_fluxo_srm"
    SOLICITAR_DECISAO_AGENDAMENTO = "solicitar_decisao_agendamento"
    DEFINIR_FUNNEL_STAGE = "definir_funnel_stage"
    # Agendamento
    COLETAR_PREFERENCIAS_AGENDAMENTO = "coletar_preferencias_agendamento"
    ROTEAR_PARA_AGENTE_AGENDAMENTO = "rotear_para_agente_agendamento"
    HANDOFF_AGENDAMENTO = "handoff_agendamento"
    PROCESSAR_CANCELAMENTO = "processar_cancelamento"
    PROCESSAR_REAGENDAMENTO = "processar_reagendamento"
    PROCESSAR_CONFIRMACAO_PRESENCA = "processar_confirmacao_presenca"
    # RAG
    CONSULTAR_RAG_RESPONDER = "consultar_rag_responder"
    CONSULTAR_RAG_E_SE_FALTAR_HANDOFF = "consultar_rag_e_se_faltar_handoff"
    # Handoff por setor
    HANDOFF_FINANCEIRO = "handoff_financeiro"
    HANDOFF_ADMINISTRATIVO = "handoff_administrativo"
    HANDOFF_GERENCIA = "handoff_gerencia"
    HANDOFF_CLINICO = "handoff_clinico"
    HANDOFF_URGENCIA = "handoff_urgencia"
    # Segurança e conduta
    ATIVAR_MODO_SAFETY_E_HANDOFF = "ativar_modo_safety_e_handoff"
    ATIVAR_PROTOCOLO_CONDUTA_NADEQUADA = "ativar_protocolo_conduta_inadequada"
    # Fora do horário
    ENFILEIRAR_HANDOFF_FORA_DO_HORARIO = "enfileirar_handoff_fora_do_horario"
    # Fallback genérico (compatibilidade)
    RESPOND = "respond"
    CLARIFY = "clarify"


# ============================================
# Input Models
# ============================================


class SRMInput(BaseModel):
    """Input for SRM processing."""

    tenant_id: UUID
    session_id: UUID
    message: str
    channel: str = "unknown"
    user_id: Optional[str] = None
    language: str = "pt-BR"

    # Current session state
    current_funnel_stage: FunnelStage = FunnelStage.LEAD_NOVO
    turn_count: int = 0
    intent_history: list[str] = Field(default_factory=list)
    slots: dict[str, Any] = Field(default_factory=dict)
    flags: dict[str, bool] = Field(default_factory=dict)
    safety_triggers: int = 0
    
    # Configuration
    max_turns_for_handoff: int = 10
    safety_threshold: float = 0.5
    confidence_threshold: float = 0.7
    # Doc MVP v0.5: ClinicConfig e UserState (quando disponíveis)
    clinic_config: Optional[dict[str, Any]] = None
    user_state: Optional[dict[str, Any]] = None


# ============================================
# Output Models
# ============================================


class IntentResult(BaseModel):
    """Result from intent classification."""

    intent: str
    category: IntentCategory
    confidence: float = Field(ge=0.0, le=1.0)
    matched_patterns: list[str] = Field(default_factory=list)


class SafetyResult(BaseModel):
    """Result from safety detection."""

    is_safe: bool = True
    category: SafetyCategory = SafetyCategory.NONE
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    triggers: list[str] = Field(default_factory=list)
    action: str = "continue"


class FunnelResult(BaseModel):
    """Result from funnel detection."""

    current_stage: FunnelStage
    should_transition: bool = False
    next_stage: Optional[FunnelStage] = None
    transition_reason: Optional[str] = None


class HandoffResult(BaseModel):
    """Result from handoff decision (doc: setor + SLA)."""

    required: bool = False
    reason: Optional[HandoffReason] = None
    urgency: str = "normal"
    summary: Optional[str] = None
    sector: Optional[str] = None  # agendamento, financeiro, gerencia, administrativo, clinico, urgencia, fidelizacao
    sla: Optional[str] = None  # imediato, prioritario, rotina
    # Two-step scheduling confirmation flow
    confirmation_pending: bool = False   # ask user to confirm before triggering handoff
    scheduling_cancelled: bool = False   # user declined — clear scheduling_interest flag


class RagResult(BaseModel):
    """Result from RAG decision."""

    needs_rag: bool = False
    query_suggestion: Optional[str] = None
    reason: Optional[str] = None


class SentimentResult(BaseModel):
    """Result from sentiment classification."""

    label: SentimentLabel = SentimentLabel.NEUTRAL
    score: float = Field(default=0.0, ge=-1.0, le=1.0)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    probabilities: dict[str, float] = Field(default_factory=dict)


class StatePatch(BaseModel):
    """State changes to apply to session (UserState/FunnelState doc)."""

    funnel_stage: Optional[str] = None
    last_intent: Optional[str] = None
    last_action: Optional[str] = None
    slots: dict[str, Any] = Field(default_factory=dict)
    flags: dict[str, bool] = Field(default_factory=dict)
    safety_trigger: bool = False
    confidence: Optional[float] = None
    sentiment_label: Optional[str] = None
    sentiment_score: Optional[float] = None
    sentiment_confidence: Optional[float] = None
    # lead (doc)
    lead_name: Optional[str] = None
    city_residence: Optional[str] = None
    already_patient: Optional[bool] = None
    # sales (doc): profile, authority_done, objection_active
    sales_profile: Optional[str] = None  # novidade, decepcao, continuidade
    authority_done: Optional[bool] = None
    objection_active: Optional[bool] = None
    # last (policy_refs, constraints applied in policy layer)
    policy_refs: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)


class SRMDecision(BaseModel):
    """
    Complete SRM decision output.
    
    Contains the cognitive analysis of the user's message including
    intent classification, funnel stage, safety assessment, and action recommendations.
    """

    # Intent
    intent: str
    intent_category: IntentCategory
    
    # Funnel
    funnel_stage: FunnelStage
    funnel_transition: Optional[str] = None
    
    # Action
    action: ActionType
    action_details: Optional[str] = None
    
    # Confidence
    confidence: float = Field(ge=0.0, le=1.0)
    
    # Safety
    safety_concern: bool = False
    safety_category: SafetyCategory = SafetyCategory.NONE

    # Sentiment
    sentiment_label: SentimentLabel = SentimentLabel.NEUTRAL
    sentiment_score: float = Field(default=0.0, ge=-1.0, le=1.0)
    sentiment_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    
    # Flow
    mode: FlowMode = FlowMode.NORMAL
    needs_rag: bool = False
    
    # Handoff (doc: setor + SLA)
    handoff_required: bool = False
    handoff_reason: Optional[HandoffReason] = None
    handoff_urgency: str = "normal"
    handoff_sector: Optional[str] = None
    handoff_sla: Optional[str] = None
    
    # State changes
    state_patch: StatePatch = Field(default_factory=StatePatch)
    
    # Reasoning (for debugging/audit)
    reasoning: Optional[str] = None
    processing_time_ms: Optional[int] = None

    def to_api_response(self) -> dict[str, Any]:
        """Convert to API response format."""
        return {
            "intent": self.intent,
            "intent_category": self.intent_category.value,
            "funnel_stage": self.funnel_stage.value,
            "action": self.action.value,
            "confidence": self.confidence,
            "safety_concern": self.safety_concern,
            "safety_category": self.safety_category.value,
            "sentiment_label": self.sentiment_label.value,
            "sentiment_score": self.sentiment_score,
            "sentiment_confidence": self.sentiment_confidence,
            "needs_rag": self.needs_rag,
            "handoff_required": self.handoff_required,
            "handoff_reason": self.handoff_reason.value if self.handoff_reason else None,
            "reasoning": self.reasoning,
        }

