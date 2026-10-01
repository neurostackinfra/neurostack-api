"""Stable, dependency-free identifiers shared by core engines and presets.

This module is intentionally a leaf: it imports no preset loader and no runtime
consumer. Values here are persisted, emitted, or used as control-flow keys and
therefore are not translated by a preset.
"""

from enum import Enum
from types import MappingProxyType
from typing import Final, Mapping


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
    """Canonical persisted funnel stages."""

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
    """Flow modes used by SRM decisions."""

    NORMAL = "normal"
    RAG = "rag"
    SAFETY = "safety"
    HANDOFF = "handoff"


class HandoffReason(str, Enum):
    """Reasons emitted when a conversation is handed to a person."""

    USER_REQUEST = "user_request"
    SAFETY_CONCERN = "safety_concern"
    MAX_TURNS_EXCEEDED = "max_turns_exceeded"
    LOW_CONFIDENCE = "low_confidence"
    COMPLEX_QUERY = "complex_query"
    ESCALATION = "escalation"
    SCHEDULING = "scheduling"


INSURANCE_TRIAGE_CUSTOM_KEY: Final[str] = "ask_convenio_triage"
CARE_MODALITY_SLOT: Final[str] = "atendimento_tipo"
INSURANCE_CARE_MODALITY: Final[str] = "convenio"
PRIVATE_PAY_CARE_MODALITY: Final[str] = "particular"
CAN_QUOTE_PRIVATE_PAY_CUSTOM_KEY: Final[str] = "can_quote_particular"


class ActionType(str, Enum):
    """Canonical action catalogue emitted by SRM."""

    PERGUNTAR_SE_JA_E_PACIENTE = "perguntar_se_ja_e_paciente"
    PERGUNTAR_NOME = "perguntar_nome"
    PERGUNTA_UNICA_PARA_DESTRAVAR = "pergunta_unica_para_destravar"
    INICIAR_FLUXO_QUALIFICACAO_SRM = "iniciar_fluxo_qualificacao_srm"
    CONTINUAR_FLUXO_SRM = "continuar_fluxo_srm"
    SOLICITAR_DECISAO_AGENDAMENTO = "solicitar_decisao_agendamento"
    DEFINIR_FUNNEL_STAGE = "definir_funnel_stage"
    COLETAR_PREFERENCIAS_AGENDAMENTO = "coletar_preferencias_agendamento"
    ROTEAR_PARA_AGENTE_AGENDAMENTO = "rotear_para_agente_agendamento"
    HANDOFF_AGENDAMENTO = "handoff_agendamento"
    PROCESSAR_CANCELAMENTO = "processar_cancelamento"
    PROCESSAR_REAGENDAMENTO = "processar_reagendamento"
    PROCESSAR_CONFIRMACAO_PRESENCA = "processar_confirmacao_presenca"
    CONSULTAR_RAG_RESPONDER = "consultar_rag_responder"
    CONSULTAR_RAG_E_SE_FALTAR_HANDOFF = "consultar_rag_e_se_faltar_handoff"
    HANDOFF_FINANCEIRO = "handoff_financeiro"
    HANDOFF_ADMINISTRATIVO = "handoff_administrativo"
    HANDOFF_GERENCIA = "handoff_gerencia"
    HANDOFF_CLINICO = "handoff_clinico"
    HANDOFF_URGENCIA = "handoff_urgencia"
    ATIVAR_MODO_SAFETY_E_HANDOFF = "ativar_modo_safety_e_handoff"
    ATIVAR_PROTOCOLO_CONDUTA_NADEQUADA = "ativar_protocolo_conduta_inadequada"
    ENFILEIRAR_HANDOFF_FORA_DO_HORARIO = "enfileirar_handoff_fora_do_horario"
    RESPOND = "respond"
    CLARIFY = "clarify"


OFFICIAL_INTENT_SEQUENCE: Final[tuple[str, ...]] = (
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
)

OFFICIAL_INTENT_IDS: Final[frozenset[str]] = frozenset(OFFICIAL_INTENT_SEQUENCE)

ACTION_IDS: Final[frozenset[str]] = frozenset(item.value for item in ActionType)

POLICY_RULE_IDS: Final[frozenset[str]] = frozenset(
    {
        "C_MEDICAL_NO_DIAGNOSIS",
        "C_MEDICAL_NO_MED_CHANGE_POSTCARE",
        "C_COMPLIANCE_NO_PROMISE_OF_RESULTS",
        "global_no_legal_advice",
        "global_no_false_promises",
        "global_privacy",
        "global_professional_tone",
        "POL_SAFETY_001_EMERGENCY_POSTCARE_OUT_OF_HOURS",
    }
)

POLICY_RULE_NAMES: Final[frozenset[str]] = frozenset(
    {
        "no_medical_diagnosis",
        "no_medication_prescription",
        "no_treatment_guarantee",
        "no_legal_advice",
        "no_false_promises",
        "privacy_compliance",
        "professional_tone",
        "emergency_redirect",
    }
)

POLICY_RULE_PAIRS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "C_MEDICAL_NO_DIAGNOSIS": "no_medical_diagnosis",
        "C_MEDICAL_NO_MED_CHANGE_POSTCARE": "no_medication_prescription",
        "C_COMPLIANCE_NO_PROMISE_OF_RESULTS": "no_treatment_guarantee",
        "global_no_legal_advice": "no_legal_advice",
        "global_no_false_promises": "no_false_promises",
        "global_privacy": "privacy_compliance",
        "global_professional_tone": "professional_tone",
        "POL_SAFETY_001_EMERGENCY_POSTCARE_OUT_OF_HOURS": "emergency_redirect",
    }
)

HANDOFF_RESPONSE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "explicit_request",
        "safety_concern",
        "max_turns_exceeded",
        "low_confidence",
        "complex_query",
        "escalation",
        "scheduling",
        "default",
        "waiting",
        "connected",
    }
)

SAFETY_RESPONSE_KEYS: Final[frozenset[str]] = frozenset(
    {"clinical_risk", "legal_risk", "conduct_risk", "emergency", "default"}
)

PROMPT_LOOKUP_KEYS: Final[frozenset[str]] = frozenset(
    {
        "base.constraint_heading",
        "base.safety_heading",
        "normal.system_prompt",
        "normal.system_prompt_en",
        "normal.funnel_instructions",
        "normal.funnel_instructions_en",
        "normal.build_system_message",
        "normal.context_instructions",
        "normal.intent_instructions",
        "normal.clinic_context",
        "handoff.system_prompt",
        "handoff.reason_instructions",
        "handoff.context_instructions",
        "handoff.summary",
        "safety.system_prompt",
        "safety.category_instructions",
        "safety.context_instructions",
        "safety.constraints",
        "renderer.warning",
        "renderer.journey",
    }
)

ENGINE_REGEX_KEYS: Final[frozenset[str]] = frozenset(
    {"scheduling_has_time", "atendimento_tipo"}
)

PROMPT_CONTROL_IDS: Final[frozenset[str]] = frozenset(
    {
        INSURANCE_TRIAGE_CUSTOM_KEY,
        CARE_MODALITY_SLOT,
        INSURANCE_CARE_MODALITY,
        PRIVATE_PAY_CARE_MODALITY,
        CAN_QUOTE_PRIVATE_PAY_CUSTOM_KEY,
    }
)

ENUM_VALUE_NAMESPACES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "intent_categories": frozenset(item.value for item in IntentCategory),
        "funnel_stages": frozenset(item.value for item in FunnelStage),
        "safety_categories": frozenset(item.value for item in SafetyCategory),
        "sentiment_labels": frozenset(item.value for item in SentimentLabel),
        "flow_modes": frozenset(item.value for item in FlowMode),
        "handoff_reasons": frozenset(item.value for item in HandoffReason),
    }
)

PROTOCOL_NAMESPACES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        **ENUM_VALUE_NAMESPACES,
        "official_intents": OFFICIAL_INTENT_IDS,
        "actions": ACTION_IDS,
        "policy_rule_ids": POLICY_RULE_IDS,
        "policy_rule_names": POLICY_RULE_NAMES,
        "handoff_response_keys": HANDOFF_RESPONSE_KEYS,
        "safety_response_keys": SAFETY_RESPONSE_KEYS,
        "prompt_lookup_keys": PROMPT_LOOKUP_KEYS,
        "prompt_control_ids": PROMPT_CONTROL_IDS,
        "engine_regex_keys": ENGINE_REGEX_KEYS,
    }
)

PROTOCOL_IDENTIFIERS: Final[frozenset[str]] = frozenset().union(
    *PROTOCOL_NAMESPACES.values()
)

__all__ = [
    "ACTION_IDS",
    "ActionType",
    "CAN_QUOTE_PRIVATE_PAY_CUSTOM_KEY",
    "CARE_MODALITY_SLOT",
    "ENUM_VALUE_NAMESPACES",
    "ENGINE_REGEX_KEYS",
    "FlowMode",
    "FunnelStage",
    "HANDOFF_RESPONSE_KEYS",
    "HandoffReason",
    "INSURANCE_CARE_MODALITY",
    "INSURANCE_TRIAGE_CUSTOM_KEY",
    "IntentCategory",
    "OFFICIAL_INTENT_IDS",
    "OFFICIAL_INTENT_SEQUENCE",
    "POLICY_RULE_IDS",
    "POLICY_RULE_NAMES",
    "POLICY_RULE_PAIRS",
    "PROMPT_LOOKUP_KEYS",
    "PROMPT_CONTROL_IDS",
    "PROTOCOL_IDENTIFIERS",
    "PROTOCOL_NAMESPACES",
    "PRIVATE_PAY_CARE_MODALITY",
    "SAFETY_RESPONSE_KEYS",
    "SafetyCategory",
    "SentimentLabel",
]
