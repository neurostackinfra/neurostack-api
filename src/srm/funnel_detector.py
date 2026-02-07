"""
NeuroStack - SRM - Funnel Detector
Detects and manages funnel stage transitions based on the 11-stage canonical funnel (MVP v0.5).
"""

from typing import Any, Optional

from src.srm.models import FunnelStage, FunnelResult, IntentCategory


# ============================================
# Funnel Stage Definitions (11 etapas canônicas)
# ============================================

FUNNEL_STAGES: dict[FunnelStage, dict[str, Any]] = {
    FunnelStage.LEAD_NOVO: {
        "description": "Primeiro contato; verificar se já é paciente",
        "next_stages": [FunnelStage.EM_QUALIFICACAO, FunnelStage.PACIENTE_ATIVO],
        "entry_conditions": ["first_contact", "new_session", "saudacao"],
    },
    FunnelStage.EM_QUALIFICACAO: {
        "description": "Fluxo de venda em andamento; coleta nome e serviço de interesse",
        "next_stages": [FunnelStage.QUALIFICADO, FunnelStage.LEAD_NOVO],
        "entry_conditions": ["interesse_tratamento", "pedido_preco", "nome_coletado"],
    },
    FunnelStage.QUALIFICADO: {
        "description": "Qualificação concluída; pronto para decisão de agendamento",
        "next_stages": [FunnelStage.FOLLOWUP, FunnelStage.AGENDAMENTO_SOLICITADO],
        "entry_conditions": ["qualificacao_completa"],
    },
    FunnelStage.FOLLOWUP: {
        "description": "Reengajamento após qualificado; retomar intenção",
        "next_stages": [FunnelStage.AGENDAMENTO_SOLICITADO, FunnelStage.QUALIFICADO],
        "entry_conditions": ["gatilho_temporal", "objeção_ativa"],
    },
    FunnelStage.AGENDAMENTO_SOLICITADO: {
        "description": "Usuário informou preferências de agendamento (dia/turno)",
        "next_stages": [FunnelStage.AGENDADO, FunnelStage.QUALIFICADO],
        "entry_conditions": ["agendar", "preferencias_informadas"],
    },
    FunnelStage.AGENDADO: {
        "description": "Pessoa confirmou agendamento",
        "next_stages": [FunnelStage.POS_ATENDIMENTO, FunnelStage.AGENDAMENTO_SOLICITADO],
        "entry_conditions": ["confirmar_presenca", "agendamento_finalizado"],
    },
    FunnelStage.PACIENTE_ATIVO: {
        "description": "Usuário já é paciente da clínica",
        "next_stages": [FunnelStage.EM_QUALIFICACAO, FunnelStage.POS_ATENDIMENTO],
        "entry_conditions": ["already_patient", "paciente_declarado"],
    },
    FunnelStage.POS_ATENDIMENTO: {
        "description": "Dúvidas ou suporte após atendimento presencial",
        "next_stages": [FunnelStage.FIDELIZACAO, FunnelStage.PACIENTE_ATIVO],
        "entry_conditions": ["pos_atendimento_duvida", "pos_atendimento_sintomas"],
    },
    FunnelStage.FIDELIZACAO: {
        "description": "Gatilho de reengajamento (ex: 30 dias pós-atendimento)",
        "next_stages": [FunnelStage.AGENDAMENTO_SOLICITADO, FunnelStage.PACIENTE_ATIVO],
        "entry_conditions": ["gatilho_temporal"],
    },
    FunnelStage.INATIVO: {
        "description": "Sem interação por x dias",
        "next_stages": [FunnelStage.LEAD_NOVO, FunnelStage.EM_QUALIFICACAO],
        "entry_conditions": ["inatividade_dias"],
    },
    FunnelStage.STANDBY: {
        "description": "Automação desligada; atendimento 100% humano",
        "next_stages": [],  # Saída apenas por configuração/handoff expirado
        "entry_conditions": ["handoff_ativado"],
    },
}


# ============================================
# Transition Rules (Intent -> next stage)
# Intenções oficiais (CTX Intents v0.1 / SRM Intents v0.1)
# ============================================

TRANSITION_TRIGGERS: dict[tuple[FunnelStage, str], FunnelStage] = {
    # lead_novo -> em_qualificacao (interesse, preço, agendar)
    (FunnelStage.LEAD_NOVO, "saudacao"): FunnelStage.LEAD_NOVO,  # mantém
    (FunnelStage.LEAD_NOVO, "interesse_tratamento"): FunnelStage.EM_QUALIFICACAO,
    (FunnelStage.LEAD_NOVO, "pedido_preco"): FunnelStage.EM_QUALIFICACAO,
    (FunnelStage.LEAD_NOVO, "agendar"): FunnelStage.EM_QUALIFICACAO,
    (FunnelStage.LEAD_NOVO, "duvida_generica_sem_contexto"): FunnelStage.LEAD_NOVO,
    # lead_novo -> paciente_ativo (já é paciente - por slot/flag, não intent único)
    # em_qualificacao -> qualificado (quando qualificação completa, via slots/flags)
    (FunnelStage.EM_QUALIFICACAO, "agendar"): FunnelStage.AGENDAMENTO_SOLICITADO,
    (FunnelStage.EM_QUALIFICACAO, "disponibilidade_horarios"): FunnelStage.EM_QUALIFICACAO,
    # qualificado -> agendamento_solicitado
    (FunnelStage.QUALIFICADO, "agendar"): FunnelStage.AGENDAMENTO_SOLICITADO,
    (FunnelStage.QUALIFICADO, "disponibilidade_horarios"): FunnelStage.AGENDAMENTO_SOLICITADO,
    (FunnelStage.QUALIFICADO, "objecao_vou_pensar"): FunnelStage.QUALIFICADO,
    # followup -> agendamento_solicitado
    (FunnelStage.FOLLOWUP, "agendar"): FunnelStage.AGENDAMENTO_SOLICITADO,
    (FunnelStage.FOLLOWUP, "interesse_tratamento"): FunnelStage.EM_QUALIFICACAO,
    # agendamento_solicitado -> agendado (confirmar que finalizou)
    (FunnelStage.AGENDAMENTO_SOLICITADO, "confirmar_presenca"): FunnelStage.AGENDADO,
    (FunnelStage.AGENDAMENTO_SOLICITADO, "reagendar"): FunnelStage.AGENDAMENTO_SOLICITADO,
    (FunnelStage.AGENDAMENTO_SOLICITADO, "cancelar"): FunnelStage.QUALIFICADO,
    # agendado -> pos_atendimento (quando "já foi atendido" - via flag/slot)
    (FunnelStage.AGENDADO, "confirmar_presenca"): FunnelStage.AGENDADO,
    (FunnelStage.AGENDADO, "reagendar"): FunnelStage.AGENDAMENTO_SOLICITADO,
    (FunnelStage.AGENDADO, "cancelar"): FunnelStage.QUALIFICADO,
    (FunnelStage.AGENDADO, "pos_atendimento_duvida"): FunnelStage.POS_ATENDIMENTO,
    (FunnelStage.AGENDADO, "pos_atendimento_sintomas_preocupantes"): FunnelStage.POS_ATENDIMENTO,
    # paciente_ativo
    (FunnelStage.PACIENTE_ATIVO, "interesse_tratamento"): FunnelStage.EM_QUALIFICACAO,
    (FunnelStage.PACIENTE_ATIVO, "pedido_preco"): FunnelStage.EM_QUALIFICACAO,
    (FunnelStage.PACIENTE_ATIVO, "pos_atendimento_duvida"): FunnelStage.POS_ATENDIMENTO,
    # pos_atendimento (mantém ou fidelização por gatilho temporal)
    (FunnelStage.POS_ATENDIMENTO, "pos_atendimento_duvida"): FunnelStage.POS_ATENDIMENTO,
    (FunnelStage.POS_ATENDIMENTO, "pos_atendimento_sintomas_preocupantes"): FunnelStage.POS_ATENDIMENTO,
    # inativo -> reativação
    (FunnelStage.INATIVO, "saudacao"): FunnelStage.LEAD_NOVO,
    (FunnelStage.INATIVO, "interesse_tratamento"): FunnelStage.EM_QUALIFICACAO,
}

# Turn thresholds for optional auto-advancement (configurável por Clinic Config)
TURN_THRESHOLDS: dict[FunnelStage, int] = {
    FunnelStage.LEAD_NOVO: 3,
    FunnelStage.EM_QUALIFICACAO: 8,
    FunnelStage.QUALIFICADO: 5,
    FunnelStage.FOLLOWUP: 5,
    FunnelStage.AGENDAMENTO_SOLICITADO: 5,
    FunnelStage.AGENDADO: 10,
    FunnelStage.PACIENTE_ATIVO: 10,
    FunnelStage.POS_ATENDIMENTO: 10,
    FunnelStage.FIDELIZACAO: 5,
    FunnelStage.INATIVO: 0,
    FunnelStage.STANDBY: 0,
}


class FunnelDetector:
    """
    Detects and manages funnel stage transitions for the 11-stage canonical funnel.
    """

    def __init__(self, custom_transitions: Optional[dict] = None):
        self.transitions = dict(TRANSITION_TRIGGERS)
        if custom_transitions:
            self.transitions.update(custom_transitions)

    def detect(
        self,
        current_stage: FunnelStage,
        intent: str,
        intent_category: IntentCategory,
        turn_count: int,
        slots: dict,
        flags: dict,
    ) -> FunnelResult:
        """
        Detect if a funnel transition should occur based on intent and state.
        """
        # Standby e inativo: transições geralmente por configuração, não por intent único
        if current_stage == FunnelStage.STANDBY:
            return FunnelResult(
                current_stage=current_stage,
                should_transition=False,
                next_stage=None,
                transition_reason=None,
            )

        # Check explicit (current_stage, intent) -> next_stage
        transition_key = (current_stage, intent)
        if transition_key in self.transitions:
            next_stage = self.transitions[transition_key]
            if next_stage != current_stage:
                return FunnelResult(
                    current_stage=current_stage,
                    should_transition=True,
                    next_stage=next_stage,
                    transition_reason=f"Intent '{intent}' triggers transition to {next_stage.value}",
                )

        # Transições baseadas em flags/slots (já é paciente -> paciente_ativo)
        if current_stage == FunnelStage.LEAD_NOVO and slots.get("already_patient") is True:
            return FunnelResult(
                current_stage=current_stage,
                should_transition=True,
                next_stage=FunnelStage.PACIENTE_ATIVO,
                transition_reason="User declared already patient",
            )

        # em_qualificacao -> qualificado quando temos nome + serviço (simplificado por slots)
        if (
            current_stage == FunnelStage.EM_QUALIFICACAO
            and slots.get("lead_name")
            and slots.get("service_interest")
            and intent in ("agendar", "disponibilidade_horarios", "pedido_preco")
        ):
            return FunnelResult(
                current_stage=current_stage,
                should_transition=True,
                next_stage=FunnelStage.QUALIFICADO,
                transition_reason="Qualification data collected, advancing to qualificado",
            )

        # agendamento_solicitado -> agendado quando usuário confirma que finalizou
        if (
            current_stage == FunnelStage.AGENDAMENTO_SOLICITADO
            and intent == "confirmar_presenca"
            and flags.get("scheduling_completed")
        ):
            return FunnelResult(
                current_stage=current_stage,
                should_transition=True,
                next_stage=FunnelStage.AGENDADO,
                transition_reason="User confirmed scheduling completed",
            )

        # Category-based fallbacks (intents antigos ainda podem existir temporariamente)
        category_result = self._check_category_fallback(
            current_stage, intent, intent_category, turn_count, flags
        )
        if category_result:
            return category_result

        return FunnelResult(
            current_stage=current_stage,
            should_transition=False,
            next_stage=None,
            transition_reason=None,
        )

    def _check_category_fallback(
        self,
        current_stage: FunnelStage,
        intent: str,
        intent_category: IntentCategory,
        turn_count: int,
        flags: dict,
    ) -> Optional[FunnelResult]:
        """Fallback transitions using intent category for backward compatibility."""
        # Scheduling intent in lead_novo -> em_qualificacao
        if (
            current_stage == FunnelStage.LEAD_NOVO
            and intent_category in (IntentCategory.SCHEDULING, IntentCategory.QUESTION)
        ):
            if "agendar" in intent or "scheduling" in intent or "question_price" in intent:
                return FunnelResult(
                    current_stage=current_stage,
                    should_transition=True,
                    next_stage=FunnelStage.EM_QUALIFICACAO,
                    transition_reason="Interest/scheduling intent in lead_novo",
                )
        # Qualificado: agendar -> agendamento_solicitado
        if current_stage == FunnelStage.QUALIFICADO and intent_category == IntentCategory.SCHEDULING:
            return FunnelResult(
                current_stage=current_stage,
                should_transition=True,
                next_stage=FunnelStage.AGENDAMENTO_SOLICITADO,
                transition_reason="Scheduling intent in qualificado",
            )
        # Turn-based: lead_novo com muitas interações pode avançar
        threshold = TURN_THRESHOLDS.get(current_stage, 5)
        if (
            current_stage == FunnelStage.LEAD_NOVO
            and turn_count >= threshold
            and intent_category == IntentCategory.QUESTION
        ):
            return FunnelResult(
                current_stage=current_stage,
                should_transition=True,
                next_stage=FunnelStage.EM_QUALIFICACAO,
                transition_reason=f"Extended engagement ({turn_count} turns)",
            )
        return None

    def get_stage_info(self, stage: FunnelStage) -> dict:
        """Get information about a funnel stage."""
        return FUNNEL_STAGES.get(stage, {})

    def get_valid_transitions(self, stage: FunnelStage) -> list[FunnelStage]:
        """Get valid next stages from current stage."""
        info = FUNNEL_STAGES.get(stage, {})
        return info.get("next_stages", [])


_detector: Optional[FunnelDetector] = None


def get_funnel_detector() -> FunnelDetector:
    """Get or create the global FunnelDetector instance."""
    global _detector
    if _detector is None:
        _detector = FunnelDetector()
    return _detector
