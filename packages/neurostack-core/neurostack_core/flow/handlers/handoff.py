"""
NeuroStack - Flow Manager - Handoff Mode Handler
Handles human handoff flow.
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


HANDOFF_RESPONSES = cast(Mapping[str, str], get_preset().flow["handoff_responses"])

_IMPERATIVE_LITERALS = cast(
    Mapping[str, Mapping[str, tuple[str, ...]]],
    get_preset().flow["imperative_literals"],
)
_PROMPT_ADDITIONS = _IMPERATIVE_LITERALS["flow.handlers.handoff"][
    "HandoffModeHandler.get_mode_prompt_additions"
]


class HandoffModeHandler(BaseModeHandler):
    """
    Handler for HANDOFF mode.

    Manages the transition from AI to human agent, including:
    - Queue management
    - Agent notification
    - Context transfer
    - Session state preservation
    """

    def __init__(self) -> None:
        super().__init__(FlowMode.HANDOFF)
        self.default_wait_time = 5  # minutes

    async def handle(self, context: FlowContext) -> ModeHandlerResult:
        """
        Handle a message in HANDOFF mode.

        In handoff mode, we:
        1. Acknowledge the handoff request
        2. Notify the queue system (placeholder)
        3. Provide status updates
        """
        start_time = time.perf_counter()

        # Get handoff reason
        handoff_reason = context.handoff_reason or "default"

        # Get current handoff status from metadata
        handoff_status = context.metadata.get("handoff_status", "pending")
        agent_id = context.metadata.get("handoff_agent_id")

        # Determine response based on status
        if handoff_status == "connected" and agent_id:
            response = HANDOFF_RESPONSES["connected"].format(agent_name=agent_id)
        elif handoff_status == "waiting":
            wait_time = context.metadata.get("wait_time", self.default_wait_time)
            response = HANDOFF_RESPONSES["waiting"].format(wait_time=wait_time)
        else:
            response = HANDOFF_RESPONSES.get(
                handoff_reason, HANDOFF_RESPONSES["default"]
            )

        # Log handoff event
        events = [
            {
                "event_type": "flow.handoff.process",
                "data": {
                    "reason": handoff_reason,
                    "status": handoff_status,
                    "agent_id": agent_id,
                    "turn_count": context.turn_count,
                },
            }
        ]

        self.logger.info(
            f"HANDOFF mode: reason={handoff_reason}, status={handoff_status}, "
            f"session={context.session_id}"
        )

        # Check for transitions
        should_exit, trigger = self.should_exit(context)

        # Safety can override handoff
        if context.safety_concern:
            events.append(
                {
                    "event_type": "flow.handoff.safety_override",
                    "data": {"category": context.safety_category},
                }
            )
            return self.create_result(
                success=True,
                response=response,
                should_transition=True,
                next_trigger=TransitionTrigger.SAFETY_CONCERN_DETECTED,
                state_updates={
                    "handoff_status": handoff_status,
                    "safety_override": True,
                },
                events=events,
                start_time=start_time,
            )

        return self.create_result(
            success=True,
            response=response,
            should_transition=should_exit,
            next_trigger=trigger,
            state_updates={
                "handoff_status": handoff_status,
                "handoff_reason": handoff_reason,
                "last_mode": "handoff",
            },
            events=events,
            start_time=start_time,
        )

    def should_exit(
        self, context: FlowContext
    ) -> tuple[bool, Optional[TransitionTrigger]]:
        """
        Check if we should exit HANDOFF mode.

        Exit conditions:
        - Safety concern -> SAFETY (priority override)
        - Handoff completed -> NORMAL
        - Handoff cancelled -> NORMAL
        """
        # Safety has highest priority
        if context.safety_concern:
            return True, TransitionTrigger.SAFETY_CONCERN_DETECTED

        # Check handoff status
        handoff_status = context.metadata.get("handoff_status", "pending")

        if handoff_status == "completed":
            return True, TransitionTrigger.HANDOFF_COMPLETED

        if handoff_status == "cancelled":
            return True, TransitionTrigger.HANDOFF_CANCELLED

        # Check if handoff is no longer required
        if not context.handoff_required and handoff_status == "pending":
            return True, TransitionTrigger.HANDOFF_CANCELLED

        return False, None

    async def initiate_handoff(
        self,
        context: FlowContext,
        priority: str = "normal",
    ) -> dict[str, object]:
        """
        Initiate a handoff to human agent.

        This is a placeholder for the actual handoff system integration.

        Args:
            context: Flow context
            priority: Handoff priority (low, normal, high, urgent)

        Returns:
            Handoff initiation result
        """
        # Placeholder - would integrate with actual queue system
        return {
            "status": "queued",
            "queue_position": 1,
            "estimated_wait_time": self.default_wait_time,
            "priority": priority,
            "session_id": str(context.session_id),
        }

    def get_mode_prompt_additions(self) -> list[str]:
        """Get additional prompt instructions for HANDOFF mode."""
        return list(_PROMPT_ADDITIONS)

    def get_response_modifiers(self) -> list[str]:
        """Get response modifiers for HANDOFF mode."""
        return ["transitional", "supportive", "informative"]
