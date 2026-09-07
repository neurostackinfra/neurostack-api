"""
NeuroStack - CTX - Context Builder
Builds the context window by assembling session state, messages, and policies.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from src.ctx.models import (
    CognitiveContext,
    ContextMessage,
    ContextRequest,
    ContextWindow,
    KnowledgeChunk,
    MessageRole,
)

logger = logging.getLogger(__name__)


class ContextBuilder:
    """
    Builds context windows for LLM prompt generation.

    The ContextBuilder is responsible for:
    1. Including current session state (cognitive state)
    2. Including recent message history
    3. Including applicable policies and constraints
    4. Managing context window size
    """

    def __init__(
        self,
        default_max_messages: int = 10,
        default_max_tokens: int = 4000,
    ):
        """
        Initialize the context builder.

        Args:
            default_max_messages: Default maximum messages to include
            default_max_tokens: Default maximum tokens for context
        """
        self.default_max_messages = default_max_messages
        self.default_max_tokens = default_max_tokens

    def build(
        self,
        request: ContextRequest,
        messages: Optional[list[ContextMessage]] = None,
        knowledge_chunks: Optional[list[KnowledgeChunk]] = None,
        constraints: Optional[list[str]] = None,
        safety_reminders: Optional[list[str]] = None,
    ) -> ContextWindow:
        """
        Build a complete context window.

        Args:
            request: Context request parameters
            messages: Recent messages to include
            knowledge_chunks: Knowledge chunks from RAG
            constraints: Policy constraints
            safety_reminders: Safety reminder messages

        Returns:
            Complete ContextWindow
        """
        # Build cognitive context
        cognitive = self._build_cognitive_context(request)

        # Process messages
        processed_messages = self._process_messages(
            messages or [],
            request.current_message,
            request.max_messages,
        )

        # Calculate estimated tokens
        estimated_tokens = self._estimate_tokens(processed_messages, knowledge_chunks)

        # Build context window
        context = ContextWindow(
            session_id=request.session_id,
            tenant_id=request.tenant_id,
            cognitive=cognitive,
            messages=processed_messages,
            knowledge_chunks=knowledge_chunks or [],
            constraints=constraints or [],
            safety_reminders=safety_reminders or [],
            context_tokens=estimated_tokens,
        )

        logger.debug(
            f"Built context for session {request.session_id} "
            f"with {len(processed_messages)} messages, "
            f"~{estimated_tokens} tokens"
        )

        return context

    def _build_cognitive_context(
        self,
        request: ContextRequest,
    ) -> CognitiveContext:
        """Build cognitive context from request."""
        if request.cognitive_state:
            return request.cognitive_state

        # Default cognitive context
        return CognitiveContext(
            funnel_stage="lead_novo",
            last_intent=request.srm_intent,
            confidence_avg=request.srm_confidence,
            current_mode="rag" if request.needs_rag else "normal",
        )

    def _process_messages(
        self,
        messages: list[ContextMessage],
        current_message: str,
        max_messages: int,
    ) -> list[ContextMessage]:
        """
        Process and limit messages for context.

        Args:
            messages: Historical messages
            current_message: Current user message
            max_messages: Maximum messages to include

        Returns:
            Processed list of messages
        """
        # Add current message as first
        current = ContextMessage(
            role=MessageRole.USER,
            content=current_message,
            timestamp=datetime.now(timezone.utc),
        )

        # Take most recent messages up to limit (minus 1 for current)
        recent = messages[-(max_messages - 1) :] if messages else []

        # Combine: recent history + current message
        return recent + [current]

    def _estimate_tokens(
        self,
        messages: list[ContextMessage],
        knowledge_chunks: Optional[list[KnowledgeChunk]],
    ) -> int:
        """
        Estimate token count for context.

        Uses a simple heuristic: ~4 characters per token.
        """
        total_chars = 0

        for msg in messages:
            total_chars += len(msg.content) + len(msg.role.value) + 10

        if knowledge_chunks:
            for chunk in knowledge_chunks:
                total_chars += len(chunk.content) + 20

        return total_chars // 4

    def add_system_context(
        self,
        context: ContextWindow,
        system_prompt: str,
    ) -> ContextWindow:
        """
        Add system context message to the beginning.

        Args:
            context: Existing context window
            system_prompt: System prompt to add

        Returns:
            Updated context window
        """
        system_message = ContextMessage(
            role=MessageRole.SYSTEM,
            content=system_prompt,
        )

        # Insert at beginning
        context.messages = [system_message] + context.messages

        return context

    def trim_to_token_limit(
        self,
        context: ContextWindow,
        max_tokens: int,
    ) -> ContextWindow:
        """
        Trim context to fit within token limit.

        Removes older messages first while preserving recent ones.

        Args:
            context: Context to trim
            max_tokens: Maximum tokens allowed

        Returns:
            Trimmed context window
        """
        if context.context_tokens and context.context_tokens <= max_tokens:
            return context

        # Start removing from oldest (but keep at least last 2)
        while (
            len(context.messages) > 2
            and context.context_tokens
            and context.context_tokens > max_tokens
        ):
            # Remove oldest non-system message
            for i, msg in enumerate(context.messages):
                if msg.role != MessageRole.SYSTEM:
                    context.messages.pop(i)
                    break

            # Recalculate
            context.context_tokens = self._estimate_tokens(
                context.messages,
                context.knowledge_chunks,
            )

        return context


# ============================================
# Singleton Instance
# ============================================

_builder: Optional[ContextBuilder] = None


def get_context_builder() -> ContextBuilder:
    """Get or create the global ContextBuilder instance."""
    global _builder
    if _builder is None:
        _builder = ContextBuilder()
    return _builder
