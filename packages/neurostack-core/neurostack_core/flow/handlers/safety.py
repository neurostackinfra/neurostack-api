"""
NeuroStack - Flow Manager - Safety Mode Handler
Handles safety-critical conversation states.
"""

import logging
import time
from typing import Optional

from neurostack_core.flow.handlers.base import BaseModeHandler
from neurostack_core.flow.models import (
    FlowContext,
    FlowMode,
    ModeHandlerResult,
    TransitionTrigger,
)

logger = logging.getLogger(__name__)


# Safety response templates
SAFETY_RESPONSES = {
    "clinical_risk": (
        "Percebo que voc\u00ea pode estar passando por uma situa\u00e7\u00e3o dif\u00edcil. "
        "Por favor, procure ajuda profissional imediata. "
        "CVV: 188 (24h) | SAMU: 192 | Emerg\u00eancia: 190"
    ),
    "legal_risk": (
        "N\u00e3o posso fornecer orienta\u00e7\u00e3o m\u00e9dica, diagn\u00f3sticos ou prescri\u00e7\u00f5es. "
        "Por favor, consulte um profissional de sa\u00fade qualificado."
    ),
    "conduct_risk": (
        "Vamos manter uma conversa respeitosa. "
        "Estou aqui para ajudar da melhor forma poss\u00edvel."
    ),
    "emergency": (
        "EMERG\u00caNCIA DETECTADA. Por favor, ligue imediatamente para: "
        "SAMU 192 | Bombeiros 193 | Pol\u00edcia 190"
    ),
    "default": (
        "Entendo sua preocupa\u00e7\u00e3o. Para sua seguran\u00e7a, recomendo "
        "buscar orienta\u00e7\u00e3o profissional adequada."
    ),
}


class SafetyModeHandler(BaseModeHandler):
    """
    Handler for SAFETY mode.

    Handles safety-critical situations with appropriate responses
    and escalation procedures. Safety mode has the highest priority
    and cannot be overridden by other modes.
    """

    def __init__(self) -> None:
        super().__init__(FlowMode.SAFETY)
        self.escalation_threshold = 2  # Triggers before auto-handoff

    async def handle(self, context: FlowContext) -> ModeHandlerResult:
        """
        Handle a message in SAFETY mode.

        In safety mode, we:
        1. Provide immediate safety response
        2. Log the safety event
        3. Consider escalation to human
        """
        start_time = time.perf_counter()

        # Get appropriate safety response
        safety_category = context.safety_category or "default"
        safety_response = SAFETY_RESPONSES.get(
            safety_category, SAFETY_RESPONSES["default"]
        )

        # Calculate safety level
        safety_level = self._calculate_safety_level(context)

        # Log safety event
        events = [
            {
                "event_type": "flow.safety.triggered",
                "data": {
                    "category": safety_category,
                    "level": safety_level,
                    "message_preview": context.message[:50],
                    "turn_count": context.turn_count,
                },
            }
        ]

        self.logger.warning(
            f"SAFETY mode: category={safety_category}, level={safety_level}, "
            f"session={context.session_id}"
        )

        # Check if we should escalate to handoff
        should_escalate = safety_level >= 3 or safety_category in (
            "clinical_risk",
            "emergency",
        )

        if should_escalate:
            events.append(
                {
                    "event_type": "flow.safety.escalation",
                    "data": {
                        "reason": "high_safety_level",
                        "category": safety_category,
                    },
                }
            )

            return self.create_result(
                success=True,
                response=safety_response,
                should_transition=True,
                next_trigger=TransitionTrigger.HANDOFF_REQUESTED,
                state_updates={
                    "safety_level": safety_level,
                    "safety_category": safety_category,
                    "escalated": True,
                },
                events=events,
                start_time=start_time,
            )

        # Check if safety concern resolved
        should_exit, trigger = self.should_exit(context)

        return self.create_result(
            success=True,
            response=safety_response,
            should_transition=should_exit,
            next_trigger=trigger,
            state_updates={
                "safety_level": safety_level,
                "safety_category": safety_category,
                "last_mode": "safety",
            },
            events=events,
            start_time=start_time,
        )

    def should_exit(
        self, context: FlowContext
    ) -> tuple[bool, Optional[TransitionTrigger]]:
        """
        Check if we should exit SAFETY mode.

        Exit conditions:
        - Safety concern resolved -> NORMAL
        - Handoff required -> HANDOFF (escalation)
        """
        # Handoff escalation
        if context.handoff_required:
            return True, TransitionTrigger.HANDOFF_REQUESTED

        # Safety resolved
        if not context.safety_concern:
            return True, TransitionTrigger.SAFETY_RESOLVED

        return False, None

    def _calculate_safety_level(self, context: FlowContext) -> int:
        """
        Calculate safety level (0-3).

        0 = none, 1 = low, 2 = medium, 3 = high
        """
        category = context.safety_category

        if category in ("emergency", "clinical_risk"):
            return 3
        elif category == "legal_risk":
            return 2
        elif category == "conduct_risk":
            return 1
        else:
            return 1 if context.safety_concern else 0

    def get_mode_prompt_additions(self) -> list[str]:
        """Get additional prompt instructions for SAFETY mode."""
        return [
            "MODO SEGURAN\u00c7A ATIVO.",
            "Priorize a seguran\u00e7a do usu\u00e1rio acima de tudo.",
            "N\u00e3o forne\u00e7a informa\u00e7\u00f5es que possam causar dano.",
            "Direcione para recursos de ajuda profissional.",
            "Mantenha tom calmo e acolhedor.",
        ]

    def get_response_modifiers(self) -> list[str]:
        """Get response modifiers for SAFETY mode."""
        return ["safety_first", "empathetic", "professional", "resource_providing"]
