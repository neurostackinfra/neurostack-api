"""
NeuroStack - Flow Manager - Normal Mode Handler
Handles standard conversation flow.
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

_IMPERATIVE_LITERALS = cast(
    Mapping[str, Mapping[str, tuple[str, ...]]],
    get_preset().flow["imperative_literals"],
)
_PROMPT_ADDITIONS = _IMPERATIVE_LITERALS["flow.handlers.normal"][
    "NormalModeHandler.get_mode_prompt_additions"
]


class NormalModeHandler(BaseModeHandler):
    """
    Handler for NORMAL mode.

    Standard conversation flow where the AI responds normally
    using the prompt engine and context.
    """

    def __init__(self) -> None:
        super().__init__(FlowMode.NORMAL)

    async def handle(self, context: FlowContext) -> ModeHandlerResult:
        """
        Handle a message in NORMAL mode.

        In normal mode, we:
        1. Check if we need to transition to another mode
        2. Process the message normally
        3. Return the result for prompt generation
        """
        start_time = time.perf_counter()

        # Check for mode transitions first
        should_exit, trigger = self.should_exit(context)

        if should_exit and trigger:
            self.logger.debug(f"NORMAL mode exiting via {trigger}")
            return self.create_result(
                success=True,
                should_transition=True,
                next_trigger=trigger,
                state_updates={"mode_exit_reason": trigger.value},
                start_time=start_time,
            )

        # Log the normal processing event
        events = [
            {
                "event_type": "flow.normal.process",
                "data": {
                    "intent": context.detected_intent,
                    "confidence": context.intent_confidence,
                    "turn_count": context.turn_count,
                },
            }
        ]

        self.logger.debug(
            f"Processing in NORMAL mode: intent={context.detected_intent}, "
            f"confidence={context.intent_confidence:.2f}"
        )

        return self.create_result(
            success=True,
            should_transition=False,
            state_updates={
                "last_mode": "normal",
                "processed_normally": True,
            },
            events=events,
            start_time=start_time,
        )

    def should_exit(
        self, context: FlowContext
    ) -> tuple[bool, Optional[TransitionTrigger]]:
        """
        Check if we should exit NORMAL mode.

        Exit conditions:
        - Safety concern detected -> SAFETY
        - Handoff required -> HANDOFF
        - RAG needed -> RAG
        """
        # Safety has highest priority
        if context.safety_concern:
            return True, TransitionTrigger.SAFETY_CONCERN_DETECTED

        # Handoff next
        if context.handoff_required:
            return True, TransitionTrigger.HANDOFF_REQUESTED

        # RAG if needed
        if context.needs_rag:
            return True, TransitionTrigger.RAG_REQUIRED

        return False, None

    def get_mode_prompt_additions(self) -> list[str]:
        """Get additional prompt instructions for NORMAL mode."""
        return list(_PROMPT_ADDITIONS)

    def get_response_modifiers(self) -> list[str]:
        """Get response modifiers for NORMAL mode."""
        return ["conversational", "helpful"]
