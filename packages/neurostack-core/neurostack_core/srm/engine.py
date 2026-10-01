"""
NeuroStack - SRM (Small Reasoning Module) - Main Engine
The core deterministic decision engine for cognitive processing.
"""

import re
import time
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Optional, cast
from uuid import UUID

from neurostack_core.metrics import (
    SRM_DECISIONS,
    SRM_HANDOFFS,
    SRM_LATENCY,
    SRM_SAFETY_TRIGGERS,
)
from neurostack_core.presets import get_preset
from neurostack_core.protocol import (
    ActionType,
    FlowMode,
    FunnelStage,
    HandoffReason,
    IntentCategory,
    SafetyCategory,
    SentimentLabel,
)
from neurostack_core.srm.funnel_detector import get_funnel_detector
from neurostack_core.srm.handoff_decider import get_handoff_decider
from neurostack_core.srm.intent_classifier import get_intent_classifier
from neurostack_core.srm.models import (
    FunnelResult,
    HandoffResult,
    IntentResult,
    RagResult,
    SafetyResult,
    SRMDecision,
    SRMInput,
    StatePatch,
)
from neurostack_core.srm.rag_decider import get_rag_decider
from neurostack_core.srm.safety_detector import get_safety_detector

_ENGINE_PRESET = cast(Mapping[str, object], get_preset().srm["engine"])
_ENGINE_REGEXES = cast(Mapping[str, str], _ENGINE_PRESET["regexes"])
_REGEX_FLAGS = re.IGNORECASE | re.UNICODE
_SCHEDULING_HAS_TIME_RE = re.compile(
    _ENGINE_REGEXES["scheduling_has_time"], _REGEX_FLAGS
)
_ATENDIMENTO_TIPO_RE = re.compile(_ENGINE_REGEXES["atendimento_tipo"], _REGEX_FLAGS)

INTENT_ACTIONS: Mapping[str, ActionType] = MappingProxyType(
    {
        intent: ActionType(action)
        for intent, action in cast(
            Mapping[str, str], _ENGINE_PRESET["intent_actions"]
        ).items()
    }
)
CATEGORY_ACTIONS: Mapping[IntentCategory, ActionType] = MappingProxyType(
    {
        IntentCategory(category): ActionType(action)
        for category, action in cast(
            Mapping[str, str], _ENGINE_PRESET["category_actions"]
        ).items()
    }
)

_IMPERATIVE_LITERALS = cast(
    Mapping[str, Mapping[str, tuple[str, ...]]],
    get_preset().srm["imperative_literals"],
)
_ENGINE_IMPERATIVES = _IMPERATIVE_LITERALS["srm.engine"]
_PROCESS_LITERALS = _ENGINE_IMPERATIVES["SRMEngine.process"]
_SLOT_LITERALS = _ENGINE_IMPERATIVES["SRMEngine._extract_slots"]
_ENGINE_OUTPUTS = cast(
    Mapping[str, str], _ENGINE_IMPERATIVES["SRMEngine.output_values"]
)
_RAW_ACTION_DETAILS = cast(
    Mapping[str, str], _ENGINE_IMPERATIVES["SRMEngine._get_action_details"]
)
if set(_RAW_ACTION_DETAILS) != {action.value for action in ActionType}:
    raise RuntimeError("preset action details must exactly cover ActionType")
ACTION_DETAILS: Mapping[ActionType, str] = MappingProxyType(
    {ActionType(action): detail for action, detail in _RAW_ACTION_DETAILS.items()}
)


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
        # Optional scheduling-link handoff is active only when the clinic enabled it AND
        # a URL is configured — an enabled-but-empty link must not change the flow.
        _scheduling_cfg = (input_data.clinic_config or {}).get("scheduling", {})
        scheduling_link_enabled = bool(
            _scheduling_cfg.get("scheduling_link_enabled")
        ) and bool(_scheduling_cfg.get("scheduling_link_url"))
        handoff_result = self.handoff_decider.decide(
            message=input_data.message,
            intent=intent_result.intent,
            intent_category=intent_result.category,
            confidence=intent_result.confidence,
            turn_count=input_data.turn_count,
            safety_result=safety_result,
            funnel_stage=input_data.current_funnel_stage,
            flags=input_data.flags,
            scheduling_link_enabled=scheduling_link_enabled,
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
            message=input_data.message,
            input_slots=input_data.slots,
        )
        # P2 fix: set scheduling_has_time when scheduling_interest is already active
        # and current message contains a time/date — covers follow-up replies that
        # are classified as duvida_generica (not "agendar"), e.g. "às 10h", "dia 12",
        # "semana que vem".  _extract_slots() only sets this flag when intent=="agendar",
        # so standalone time replies would otherwise never flip it.
        existing_scheduling_interest = input_data.flags.get(
            "scheduling_interest", False
        ) or state_patch.flags.get("scheduling_interest", False)
        if existing_scheduling_interest and _SCHEDULING_HAS_TIME_RE.search(
            input_data.message
        ):
            state_patch.flags["scheduling_has_time"] = True

        # Scheduling confirmation flow: propagate pending/cancelled flags
        if handoff_result.confirmation_pending:
            state_patch.flags["handoff_confirmation_pending"] = True
            # Remember whether this confirmation was opened by a cancellation so the
            # scheduling-link path can exclude it on the confirm turn (intent is then
            # a bare "sim", no longer "cancelar").
            state_patch.flags["scheduling_cancel_pending"] = (
                intent_result.intent == "cancelar"
            )
        if handoff_result.scheduling_cancelled:
            state_patch.flags["handoff_confirmation_pending"] = False
            state_patch.flags["scheduling_interest"] = False
            state_patch.flags["scheduling_has_time"] = False
            state_patch.flags["scheduling_cancel_pending"] = False
        # P2a/P2-fix: when handoff fires, clear all scheduling flags so a resumed
        # session (e.g. after handoff-pause expires) starts fresh instead of
        # re-entering the confirmation step.
        if handoff_result.required:
            state_patch.flags["handoff_confirmation_pending"] = False
            state_patch.flags["scheduling_interest"] = False
            state_patch.flags["scheduling_has_time"] = False
            state_patch.flags["scheduling_cancel_pending"] = False
        # Scheduling-link lifecycle: the send turn arms scheduling_link_sent so next
        # turn is the enrich turn; the enrich turn disarms it once the outcome is read.
        if handoff_result.verification_status == "link_sent":
            state_patch.flags["scheduling_link_sent"] = True
        elif handoff_result.verification_status in (
            _ENGINE_OUTPUTS["verification_completed"],
            _ENGINE_OUTPUTS["verification_not_completed"],
        ):
            state_patch.flags["scheduling_link_sent"] = False

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
        _ESCALATION_INTENTS = {
            "reclamacao",
            "conduta_inadequada_ameaca",
            "pedido_atendente_humano",
        }
        is_escalation = intent_result.intent in _ESCALATION_INTENTS
        if is_escalation:
            final_funnel_stage = FunnelStage.STANDBY
            handoff_result.required = True
            handoff_result.reason = HandoffReason.ESCALATION
            handoff_result.urgency = _ENGINE_OUTPUTS["priority"]
            if not handoff_result.sector:
                handoff_result.sector = _PROCESS_LITERALS[0]
            if not handoff_result.sla:
                handoff_result.sla = _ENGINE_OUTPUTS["priority"]
            mode = FlowMode.HANDOFF

        # Clinical-safety intents force safety_concern=True, handoff=True
        _CLINICAL_SAFETY_INTENTS = {
            "pos_atendimento_sintomas_preocupantes",
            "pedido_alterar_medicacao",
        }
        is_clinical_safety = intent_result.intent in _CLINICAL_SAFETY_INTENTS
        if is_clinical_safety:
            handoff_result.required = True
            if not handoff_result.reason:
                handoff_result.reason = HandoffReason.SAFETY_CONCERN
            handoff_result.urgency = handoff_result.urgency or _ENGINE_OUTPUTS["high"]
            if not handoff_result.sector:
                handoff_result.sector = _PROCESS_LITERALS[1]
            if not handoff_result.sla:
                handoff_result.sla = _ENGINE_OUTPUTS["priority"]
            mode = FlowMode.SAFETY

        # Safety concern: safety_detector OR escalation OR clinical-safety intent
        safety_concern = (
            (not safety_result.is_safe) or is_escalation or is_clinical_safety
        )
        safety_category = (
            SafetyCategory.CONDUCT_RISK
            if is_escalation and safety_result.is_safe
            else (
                SafetyCategory.CLINICAL_RISK
                if is_clinical_safety and safety_result.is_safe
                else safety_result.category
            )
        )

        # Action details: include escalation_notice for escalation intents
        action_details = self._get_action_details(action, intent_result.intent)
        if is_escalation:
            action_details = _PROCESS_LITERALS[2]

        # Build decision
        decision = SRMDecision(
            intent=intent_result.intent,
            intent_category=intent_result.category,
            funnel_stage=final_funnel_stage,
            funnel_transition=(
                "Escalation: handoff to human"
                if is_escalation
                else (
                    funnel_result.transition_reason
                    if funnel_result.should_transition
                    else None
                )
            ),
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
            handoff_verification_status=handoff_result.verification_status,
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
        safety_result: SafetyResult,
        handoff_result: HandoffResult,
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
        safety_result: SafetyResult,
        handoff_result: HandoffResult,
        rag_result: RagResult,
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
        intent_result: IntentResult,
        funnel_result: FunnelResult,
        safety_result: SafetyResult,
        action: ActionType,
        message: str = "",
        input_slots: Optional[dict[str, Any]] = None,
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

        # Update slots and flags (incl. objection_active, authority_done, atendimento_tipo)
        self._extract_slots(intent_result.intent, patch, message, input_slots or {})

        return patch

    def _extract_slots(
        self,
        intent: str,
        patch: StatePatch,
        message: str = "",
        input_slots: Optional[dict[str, Any]] = None,
    ) -> None:
        """Extract slot updates based on intent (official 29 intents)."""
        if intent == "agendar":
            patch.flags["scheduling_interest"] = True
            # Track whether the user has mentioned a specific day/time.
            # HandoffDecider gates the confirmation prompt on this flag so we
            # don't ask "Can I forward you?" before collecting the time preference.
            if _SCHEDULING_HAS_TIME_RE.search(message):
                patch.flags["scheduling_has_time"] = True
        if intent == "pedido_preco":
            patch.constraints.append("C_SEQUENCE_AUTHORITY_BEFORE_PRICE")
        if intent == "confirmar_presenca":
            patch.flags["confirmation_pending"] = False
        if intent == "objecao_vou_pensar":
            patch.flags["objection_active"] = True
        if intent in (
            "documentos_e_comprovantes",
            "orcamento_existente",
            "cobranca_renegociacao",
        ):
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

        # atendimento_tipo (convenio-particular-triage): derive from the same
        # word signal used by intent_classifier.py:262 (`particular|convenio|convênio`)
        # so we don't duplicate the intent surface. Only write to the patch when the
        # value would change — silent no-op preserves the existing slot otherwise.
        tipo_match = _ATENDIMENTO_TIPO_RE.search(message or "")
        if tipo_match:
            raw = tipo_match.group(1).lower()
            new_tipo = (
                _SLOT_LITERALS[0] if raw == _SLOT_LITERALS[1] else _SLOT_LITERALS[2]
            )
            previous = (input_slots or {}).get(_SLOT_LITERALS[3])
            if new_tipo != previous:
                patch.slots[_SLOT_LITERALS[4]] = new_tipo

    def _get_action_details(self, action: ActionType, intent: str) -> str:
        """Get detailed description of the action (Action Catalog v0.1)."""
        return ACTION_DETAILS[action]

    def _generate_reasoning(
        self,
        intent_result: IntentResult,
        safety_result: SafetyResult,
        handoff_result: HandoffResult,
        rag_result: RagResult,
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
            if handoff_result.reason is not None:
                parts.append(f"Handoff required: {handoff_result.reason.value}")
            else:
                parts.append("Handoff required")

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
            SRM_SAFETY_TRIGGERS.labels(category=decision.safety_category.value).inc()

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
