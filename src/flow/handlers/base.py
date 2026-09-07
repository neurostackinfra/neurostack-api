"""
NeuroStack - Flow Manager - Base Handler
Base class for mode handlers.
"""

import logging
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

from src.flow.models import FlowContext, FlowMode, ModeHandlerResult, TransitionTrigger

logger = logging.getLogger(__name__)


class BaseModeHandler(ABC):
    """
    Abstract base class for flow mode handlers.

    Each mode (NORMAL, RAG, SAFETY, HANDOFF) has a handler that
    processes messages and determines next actions.
    """

    def __init__(self, mode: FlowMode):
        """
        Initialize the handler.

        Args:
            mode: The flow mode this handler manages
        """
        self.mode = mode
        self.logger = logging.getLogger(f"{__name__}.{mode.value}")

    @abstractmethod
    async def handle(self, context: FlowContext) -> ModeHandlerResult:
        """
        Handle a message in this mode.

        Args:
            context: Flow context with current state

        Returns:
            ModeHandlerResult with handler decision
        """
        pass

    @abstractmethod
    def should_exit(
        self, context: FlowContext
    ) -> tuple[bool, Optional[TransitionTrigger]]:
        """
        Check if the handler should exit to another mode.

        Args:
            context: Flow context

        Returns:
            Tuple of (should_exit, trigger)
        """
        pass

    def get_mode_prompt_additions(self) -> list[str]:
        """Get additional prompt instructions for this mode."""
        return []

    def get_response_modifiers(self) -> list[str]:
        """Get response modifiers for this mode."""
        return []

    def create_result(
        self,
        success: bool = True,
        response: Optional[str] = None,
        should_transition: bool = False,
        next_trigger: Optional[TransitionTrigger] = None,
        state_updates: Optional[dict[str, Any]] = None,
        events: Optional[list[dict[str, Any]]] = None,
        start_time: Optional[float] = None,
    ) -> ModeHandlerResult:
        """
        Create a handler result.

        Helper method for creating consistent results.
        """
        processing_time = 0.0
        if start_time:
            processing_time = (time.perf_counter() - start_time) * 1000

        return ModeHandlerResult(
            mode=self.mode,
            success=success,
            response=response,
            should_transition=should_transition,
            next_trigger=next_trigger,
            state_updates=state_updates or {},
            events_to_log=events or [],
            processing_time_ms=processing_time,
        )
