"""
NeuroStack - SRM - Handoff Decider
Determines when human handoff is required based on conversation state.
"""

import re
from typing import Optional

from src.srm.models import (
    HandoffReason,
    HandoffResult,
    IntentCategory,
    SafetyCategory,
    SafetyResult,
    FunnelStage,
)


# ============================================
# Handoff Trigger Patterns
# ============================================

EXPLICIT_HANDOFF_PATTERNS = [
    r"\b(falar|conversar|atendimento)\b.*\b(humano|pessoa|atendente|real)\b",
    r"\b(quero|preciso|pode)\b.*\b(falar|conversar)\b.*\b(algu\u00e9m|alguem|atendente)\b",
    r"\b(n\u00e3o|nao)\b.*\b(robo|rob\u00f4|bot|m\u00e1quina|maquina|autom\u00e1tico)\b",
    r"\b(transferir?|passar?)\b.*\b(para|atendente|humano)\b",
    r"\b(atendente|operador|funcion\u00e1rio)\b.*\b(humano|real|verdadeiro)\b",
    r"^(atendente|humano|pessoa)$",
]

# Confirmation response patterns for the scheduling two-step flow.
#
# STRONG positives: match regardless of sentence length.
# WEAK positives: only valid when the whole trimmed message is \u2264 3 words,
#   so "quero saber os hor\u00e1rios antes" (5 words) never fires the handoff.
_STRONG_POSITIVE_RE = re.compile(
    r"\b(sim|s[\u00edi]m|yes|claro|com\s+certeza|confirmo|confirmar|aceito|aceitar"
    r"|encaminhe|encaminha|perfeito|[\u00f3o]timo|beleza)\b",
    re.IGNORECASE | re.UNICODE,
)
_WEAK_POSITIVE_RE = re.compile(
    r"^(pode|quero|ok|okay|isso|vai|v[a\u00e1]|manda|mande|tudo\s+bem|por\s+favor|certo|exato|exatamente|boa)[\s!.,]*$",
    re.IGNORECASE | re.UNICODE,
)
_NEGATIVE_RE = re.compile(
    r"\b(n[a\u00e3]o|no|nunca|negativo|nem|agora\s+n[a\u00e3]o|ainda\s+n[a\u00e3]o"
    r"|por\s+enquanto\s+n[a\u00e3]o|cancel|cancela|desist|volta)\b",
    re.IGNORECASE | re.UNICODE,
)


class HandoffDecider:
    """
    Decides when to trigger human handoff.
    
    Handoff reasons:
    - USER_REQUEST: User explicitly asked for a human
    - SAFETY_CONCERN: Safety issue detected requiring human intervention
    - MAX_TURNS_EXCEEDED: Too many turns without resolution
    - LOW_CONFIDENCE: Low confidence in AI responses
    - COMPLEX_QUERY: Query too complex for AI
    - ESCALATION: Complaint or escalation request
    """

    def __init__(
        self,
        max_turns: int = 10,
        min_confidence: float = 0.5,
        escalation_keywords: Optional[list[str]] = None,
    ):
        """
        Initialize the handoff decider.
        
        Args:
            max_turns: Maximum turns before suggesting handoff
            min_confidence: Minimum confidence before suggesting handoff
            escalation_keywords: Additional keywords that trigger escalation
        """
        self.max_turns = max_turns
        self.min_confidence = min_confidence
        self.escalation_keywords = escalation_keywords or [
            "gerente", "supervisor", "respons\u00e1vel", "reclamar",
            "ouvidoria", "procon", "processo", "advogado",
        ]
        
        # Compile explicit handoff patterns
        self._patterns = [
            re.compile(p, re.IGNORECASE | re.UNICODE)
            for p in EXPLICIT_HANDOFF_PATTERNS
        ]

    def decide(
        self,
        message: str,
        intent: str,
        intent_category: IntentCategory,
        confidence: float,
        turn_count: int,
        safety_result: SafetyResult,
        funnel_stage: FunnelStage,
        flags: dict,
    ) -> HandoffResult:
        """
        Decide if handoff is required. Define setor e SLA conforme doc (MVP v0.5).
        Defaults: urgencia=imediato; agendamento/gerencia/clinico=prioritario; financeiro/administrativo=rotina.
        """
        message_lower = message.strip().lower()

        # Priority 1: Explicit handoff request → administrativo, prioritario
        explicit_result = self._check_explicit_request(message_lower)
        if explicit_result:
            return explicit_result

        # Priority 2: Safety concerns (intent + safety_result → setor/SLA)
        if not safety_result.is_safe:
            safety_handoff = self._check_safety_handoff(safety_result, intent)
            if safety_handoff:
                return safety_handoff

        # Priority 3: Escalation/complaint → gerencia, prioritario
        if intent_category == IntentCategory.COMPLAINT:
            return HandoffResult(
                required=True,
                reason=HandoffReason.ESCALATION,
                urgency="high",
                summary="User has a complaint that requires human attention",
                sector="gerencia",
                sla="prioritario",
            )

        # Intent-based handoff (doc: pos_atendimento_sintomas, conduta_inadequada, pedido_alterar_medicacao)
        if intent == "pos_atendimento_sintomas_preocupantes":
            return HandoffResult(
                required=True,
                reason=HandoffReason.SAFETY_CONCERN,
                urgency="critical",
                summary="Sintomas preocupantes pós-atendimento; encaminhar urgência",
                sector="urgencia",
                sla="imediato",
            )
        if intent == "conduta_inadequada_ameaca":
            return HandoffResult(
                required=True,
                reason=HandoffReason.ESCALATION,
                urgency="high",
                summary="Conduta inadequada ou ameaça; encaminhar gerência",
                sector="gerencia",
                sla="prioritario",
            )
        if intent == "pedido_alterar_medicacao":
            return HandoffResult(
                required=True,
                reason=HandoffReason.SAFETY_CONCERN,
                urgency="high",
                summary="Pedido de alteração de medicação; encaminhar clínico/urgência",
                sector="clinico",
                sla="prioritario",
            )

        # Priority 4: Scheduling — three-step flow with explicit confirmation
        # Step 1 (scheduling_interest not set): LLM collects name + day/time, no handoff
        # Step 2 (scheduling_interest set, confirmation NOT pending): ask for confirmation
        # Step 3 (handoff_confirmation_pending set): user replies yes/no → act accordingly
        already_collecting = flags.get("scheduling_interest", False)
        confirmation_pending = flags.get("handoff_confirmation_pending", False)
        is_scheduling = intent in ("agendar", "reagendar", "cancelar") or intent_category == IntentCategory.SCHEDULING

        if confirmation_pending:
            # User is answering the confirmation question
            if self._is_positive_confirmation(message_lower):
                return HandoffResult(
                    required=True,
                    reason=HandoffReason.SCHEDULING,
                    urgency="normal",
                    summary="Paciente confirmou encaminhamento para equipe de agendamento",
                    sector="agendamento",
                    sla="prioritario",
                )
            if self._is_negative_confirmation(message_lower):
                # User declined → clear flags, continue normally
                return HandoffResult(required=False, scheduling_cancelled=True)
            # Ambiguous answer — keep confirmation pending
            return HandoffResult(required=False, confirmation_pending=True)

        if already_collecting or (is_scheduling and intent in ("reagendar", "cancelar")):
            # Data collected, now ask for confirmation before handing off
            return HandoffResult(required=False, confirmation_pending=True)

        # Priority 5: Escalation keywords → gerencia
        escalation_result = self._check_escalation_keywords(message_lower)
        if escalation_result:
            return escalation_result

        # Priority 5: Max turns exceeded → administrativo, rotina
        if turn_count >= self.max_turns:
            return HandoffResult(
                required=True,
                reason=HandoffReason.MAX_TURNS_EXCEEDED,
                urgency="normal",
                summary=f"Conversation exceeded {self.max_turns} turns without resolution",
                sector="administrativo",
                sla="rotina",
            )

        # Priority 6: Low confidence → administrativo, rotina
        if confidence < self.min_confidence and flags.get("low_confidence_count", 0) >= 2:
            return HandoffResult(
                required=True,
                reason=HandoffReason.LOW_CONFIDENCE,
                urgency="normal",
                summary="Multiple low-confidence responses suggest human intervention needed",
                sector="administrativo",
                sla="rotina",
            )

        # Priority 7: Already in handoff waiting state
        if flags.get("handoff_requested", False):
            return HandoffResult(
                required=True,
                reason=HandoffReason.USER_REQUEST,
                urgency="normal",
                summary="User previously requested handoff and is still waiting",
                sector="administrativo",
                sla="prioritario",
            )

        return HandoffResult(required=False)

    def _is_positive_confirmation(self, message: str) -> bool:
        """Return True if user message is a positive confirmation.

        Strong positives (sim, claro, confirmo…) match in any length message.
        Weak positives (pode, quero, ok, isso…) only match when the entire
        trimmed message is ≤ 3 words, preventing false positives like
        "quero saber os horários antes" from triggering a handoff.
        """
        if _STRONG_POSITIVE_RE.search(message):
            return True
        words = message.strip().split()
        if len(words) <= 3 and _WEAK_POSITIVE_RE.search(message.strip()):
            return True
        return False

    def _is_negative_confirmation(self, message: str) -> bool:
        """Return True if user message is a negative response (não, no, agora não, etc.)."""
        return bool(_NEGATIVE_RE.search(message))

    def _check_explicit_request(self, message: str) -> Optional[HandoffResult]:
        """Check if user explicitly requested a human → administrativo, prioritario (doc)."""
        for pattern in self._patterns:
            if pattern.search(message):
                return HandoffResult(
                    required=True,
                    reason=HandoffReason.USER_REQUEST,
                    urgency="normal",
                    summary="User explicitly requested to speak with a human",
                    sector="administrativo",
                    sla="prioritario",
                )
        return None

    def _check_safety_handoff(
        self, safety_result: SafetyResult, intent: str
    ) -> Optional[HandoffResult]:
        """Check if safety concern requires handoff; set sector/SLA per doc."""
        if safety_result.category == SafetyCategory.CLINICAL_RISK:
            return HandoffResult(
                required=True,
                reason=HandoffReason.SAFETY_CONCERN,
                urgency="critical",
                summary=f"Clinical safety concern detected: {', '.join(safety_result.triggers[:3])}",
                sector="urgencia",
                sla="imediato",
            )
        if safety_result.category == SafetyCategory.CONDUCT_RISK:
            if safety_result.confidence >= 0.7:
                return HandoffResult(
                    required=True,
                    reason=HandoffReason.SAFETY_CONCERN,
                    urgency="high",
                    summary=f"Conduct concern detected: {', '.join(safety_result.triggers[:3])}",
                    sector="gerencia",
                    sla="prioritario",
                )
        if safety_result.category == SafetyCategory.LEGAL_RISK:
            if safety_result.action == "escalate":
                return HandoffResult(
                    required=True,
                    reason=HandoffReason.SAFETY_CONCERN,
                    urgency="normal",
                    summary="Legal risk requires human verification",
                    sector="administrativo",
                    sla="rotina",
                )
        return None

    def _check_escalation_keywords(self, message: str) -> Optional[HandoffResult]:
        """Check for escalation keywords → gerencia, prioritario."""
        matched = [kw for kw in self.escalation_keywords if kw in message]
        if matched:
            return HandoffResult(
                required=True,
                reason=HandoffReason.ESCALATION,
                urgency="high" if len(matched) > 1 else "normal",
                summary=f"Escalation keywords detected: {', '.join(matched[:3])}",
                sector="gerencia",
                sla="prioritario",
            )
        return None

    def should_suggest_handoff(
        self,
        turn_count: int,
        low_confidence_count: int,
    ) -> bool:
        """
        Check if we should suggest handoff option to user.
        
        This is a softer check - doesn't force handoff but suggests it.
        """
        return (
            turn_count >= self.max_turns - 2
            or low_confidence_count >= 2
        )

    def generate_handoff_message(self, result: HandoffResult) -> str:
        """Generate a message explaining the handoff to the user."""
        if not result.required:
            return ""
        
        messages = {
            HandoffReason.USER_REQUEST: (
                "Entendi! Vou transferir voc\u00ea para um atendente humano. "
                "Por favor, aguarde um momento."
            ),
            HandoffReason.SAFETY_CONCERN: (
                "Para sua seguran\u00e7a, vou encaminhar voc\u00ea para um atendimento especializado. "
                "Um profissional entrar\u00e1 em contato em breve."
            ),
            HandoffReason.MAX_TURNS_EXCEEDED: (
                "Parece que posso ajud\u00e1-lo melhor com um atendimento humano. "
                "Vou transferir voc\u00ea para um de nossos atendentes."
            ),
            HandoffReason.LOW_CONFIDENCE: (
                "Para garantir que voc\u00ea receba a melhor ajuda, "
                "vou transferir para um atendente especializado."
            ),
            HandoffReason.ESCALATION: (
                "Entendo sua situa\u00e7\u00e3o. Vou encaminhar para um supervisor "
                "que poder\u00e1 ajud\u00e1-lo melhor."
            ),
            HandoffReason.COMPLEX_QUERY: (
                "Essa \u00e9 uma quest\u00e3o que requer aten\u00e7\u00e3o especial. "
                "Vou transferir para um especialista."
            ),
            HandoffReason.SCHEDULING: (
                "Vou te encaminhar para a nossa equipe, que dar\u00e1 seguimento "
                "e verificar\u00e1 os melhores hor\u00e1rios dispon\u00edveis para voc\u00ea."
            ),
        }
        
        return messages.get(
            result.reason,
            "Vou transferir voc\u00ea para um atendente humano."
        )


# ============================================
# Singleton Instance
# ============================================

_decider: Optional[HandoffDecider] = None


def get_handoff_decider(
    max_turns: int = 10,
    min_confidence: float = 0.5,
) -> HandoffDecider:
    """Get or create the global HandoffDecider instance."""
    global _decider
    if _decider is None:
        _decider = HandoffDecider(
            max_turns=max_turns,
            min_confidence=min_confidence,
        )
    return _decider

