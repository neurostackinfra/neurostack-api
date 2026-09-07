"""
NeuroStack - Flow Manager - RAG Mode Handler
Handles Retrieval Augmented Generation flow.
"""

import logging
import time
from typing import Optional

from src.flow.handlers.base import BaseModeHandler
from src.flow.models import FlowContext, FlowMode, ModeHandlerResult, TransitionTrigger

logger = logging.getLogger(__name__)


class RagModeHandler(BaseModeHandler):
    """
    Handler for RAG mode.

    Handles conversations that require knowledge retrieval
    from the vector database before generating a response.

    This is a placeholder implementation that will be expanded
    when the Knowledge Service is fully implemented.
    """

    def __init__(self) -> None:
        super().__init__(FlowMode.RAG)
        self.max_rag_turns = 3  # Max turns in RAG mode before reset

    async def handle(self, context: FlowContext) -> ModeHandlerResult:
        """
        Handle a message in RAG mode.

        In RAG mode, we:
        1. Check if knowledge retrieval is still needed
        2. Coordinate with Knowledge Service (placeholder)
        3. Include knowledge context in the response
        """
        start_time = time.perf_counter()

        # Check for priority transitions
        should_exit, trigger = self.should_exit(context)

        if should_exit and trigger:
            self.logger.debug(f"RAG mode exiting via {trigger}")
            return self.create_result(
                success=True,
                should_transition=True,
                next_trigger=trigger,
                state_updates={"mode_exit_reason": trigger.value},
                start_time=start_time,
            )

        # RAG processing (placeholder)
        # In production, this would:
        # 1. Query the Knowledge Service
        # 2. Get relevant chunks
        # 3. Include them in context

        rag_query_count = context.metadata.get("rag_query_count", 0) + 1

        events = [
            {
                "event_type": "flow.rag.process",
                "data": {
                    "intent": context.detected_intent,
                    "query": context.message[:100],  # First 100 chars
                    "rag_query_count": rag_query_count,
                },
            }
        ]

        self.logger.debug(
            f"Processing in RAG mode: query_count={rag_query_count}, "
            f"intent={context.detected_intent}"
        )

        # Check if we should exit RAG after this turn
        should_exit_rag = not context.needs_rag or rag_query_count >= self.max_rag_turns

        return self.create_result(
            success=True,
            should_transition=should_exit_rag,
            next_trigger=TransitionTrigger.RAG_COMPLETED if should_exit_rag else None,
            state_updates={
                "last_mode": "rag",
                "rag_query_count": rag_query_count,
                "knowledge_used": True,  # Placeholder flag
            },
            events=events,
            start_time=start_time,
        )

    def should_exit(
        self, context: FlowContext
    ) -> tuple[bool, Optional[TransitionTrigger]]:
        """
        Check if we should exit RAG mode.

        Exit conditions:
        - Safety concern -> SAFETY (priority)
        - Handoff required -> HANDOFF
        - RAG no longer needed -> NORMAL
        """
        # Safety has highest priority
        if context.safety_concern:
            return True, TransitionTrigger.SAFETY_CONCERN_DETECTED

        # Handoff
        if context.handoff_required:
            return True, TransitionTrigger.HANDOFF_REQUESTED

        # RAG completed (no longer needed)
        if not context.needs_rag:
            return True, TransitionTrigger.RAG_COMPLETED

        return False, None

    def get_mode_prompt_additions(self) -> list[str]:
        """Get additional prompt instructions for RAG mode."""
        return [
            "Use as informa\u00e7\u00f5es do contexto de conhecimento para responder.",
            "Cite fontes quando relevante.",
            "Se n\u00e3o encontrar a informa\u00e7\u00e3o, informe ao usu\u00e1rio.",
            "Mantenha as respostas factuais e precisas.",
        ]

    def get_response_modifiers(self) -> list[str]:
        """Get response modifiers for RAG mode."""
        return ["informative", "factual", "sourced"]
