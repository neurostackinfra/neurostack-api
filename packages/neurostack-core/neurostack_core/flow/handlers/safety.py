"""
NeuroStack - Flow Manager - Safety Mode Handler
Handles safety-critical conversation states.
"""

import logging
import time
from collections.abc import Mapping
from typing import Optional, cast

from neurostack_core.flow.handlers.base import BaseModeHandler
from neurostack_core.flow.models import (
    FlowContext,
    FlowMode,
    ModeHandlerResult,
    TransitionTrigger,
)
from neurostack_core.presets import get_preset

logger = logging.getLogger(__name__)


SAFETY_RESPONSES = cast(Mapping[str, str], get_preset().flow["safety_responses"])

_IMPERATIVE_LITERALS = cast(
    Mapping[str, Mapping[str, tuple[str, ...]]],
    get_preset().flow["imperative_literals"],
)
_PROMPT_ADDITIONS = _IMPERATIVE_LITERALS["flow.handlers.safety"][
    "SafetyModeHandler.get_mode_prompt_additions"
]


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
        return list(_PROMPT_ADDITIONS)

    def get_response_modifiers(self) -> list[str]:
        """Get response modifiers for SAFETY mode."""
        return ["safety_first", "empathetic", "professional", "resource_providing"]
