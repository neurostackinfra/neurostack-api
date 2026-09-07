"""
NeuroStack - CTX - Context Orchestrator
Main orchestrator that assembles the complete context for LLM calls.
"""

import logging
import time
from typing import Optional
from uuid import UUID

from prometheus_client import Counter, Histogram

from src.ctx.compression import HistoryCompressor, get_history_compressor
from src.ctx.context_builder import ContextBuilder, get_context_builder
from src.ctx.models import (
    CognitiveContext,
    ContextMessage,
    ContextRequest,
    ContextResponse,
    ContextWindow,
    KnowledgeChunk,
    MessageRole,
)
from src.ctx.relevance import RelevanceFilter, get_relevance_filter
from src.services import KnowledgeService, get_knowledge_service

logger = logging.getLogger(__name__)


# ============================================
# Prometheus Metrics
# ============================================

CTX_BUILD_TOTAL = Counter(
    "neurostack_ctx_builds_total",
    "Total number of context builds",
    ["tenant_id", "mode"],
)

CTX_BUILD_LATENCY = Histogram(
    "neurostack_ctx_latency_seconds",
    "Context build latency in seconds",
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25),
)

CTX_TOKEN_COUNT = Histogram(
    "neurostack_ctx_tokens",
    "Token count in built contexts",
    buckets=(100, 500, 1000, 2000, 4000, 8000),
)


class ContextOrchestrator:
    """
    Main Context Orchestrator.

    Responsibilities:
    1. Assemble context from multiple sources
    2. Include session state and cognitive context
    3. Include recent message history (filtered by relevance)
    4. Include knowledge from RAG (when needed)
    5. Include policy constraints
    6. Compress history if needed
    7. Manage token limits

    This is the central component that prepares all context
    needed by the Prompt Engine to generate LLM prompts.
    """

    def __init__(
        self,
        builder: Optional[ContextBuilder] = None,
        relevance_filter: Optional[RelevanceFilter] = None,
        compressor: Optional[HistoryCompressor] = None,
        knowledge_service: Optional[KnowledgeService] = None,
    ):
        """
        Initialize the orchestrator.

        Args:
            builder: ContextBuilder instance
            relevance_filter: RelevanceFilter instance
            compressor: HistoryCompressor instance
            knowledge_service: KnowledgeService instance
        """
        self._builder = builder or get_context_builder()
        self._filter = relevance_filter or get_relevance_filter()
        self._compressor = compressor or get_history_compressor()
        self._knowledge = knowledge_service or get_knowledge_service()

    async def build_context(
        self,
        request: ContextRequest,
        message_history: Optional[list[ContextMessage]] = None,
        constraints: Optional[list[str]] = None,
        safety_reminders: Optional[list[str]] = None,
    ) -> ContextResponse:
        """
        Build complete context for a request.

        This is the main entry point for context orchestration.

        Args:
            request: Context request parameters
            message_history: Message history (if available)
            constraints: Policy constraints
            safety_reminders: Safety reminders

        Returns:
            ContextResponse with built context
        """
        start_time = time.perf_counter()
        warnings: list[str] = []

        # Step 1: Filter messages by relevance
        messages = message_history or []
        if messages and len(messages) > request.max_messages:
            messages = self._filter.filter_messages(
                messages,
                request.current_message,
                request.max_messages,
            )

        # Step 2: Compress if needed
        summary = None
        if messages:
            messages, summary = self._compressor.compress(
                messages,
                target_tokens=request.max_tokens // 2,  # Reserve half for other content
            )

        # Step 3: Get knowledge (if RAG is needed)
        knowledge_chunks: list[KnowledgeChunk] = []
        if request.needs_rag and request.include_knowledge:
            knowledge_chunks = await self._get_knowledge(
                request.tenant_id,
                request.current_message,
                request.srm_intent,
            )
            if not knowledge_chunks:
                warnings.append("No knowledge found for query")

        # Step 4: Build context window
        context = self._builder.build(
            request=request,
            messages=messages,
            knowledge_chunks=knowledge_chunks,
            constraints=constraints,
            safety_reminders=safety_reminders,
        )

        # Step 5: Add compressed summary if available
        if summary:
            context.compressed_history = summary

        # Step 6: Ensure within token limits
        if context.context_tokens and context.context_tokens > request.max_tokens:
            context = self._builder.trim_to_token_limit(context, request.max_tokens)
            warnings.append(f"Context trimmed to {request.max_tokens} tokens")

        # Calculate build time
        build_time_ms = int((time.perf_counter() - start_time) * 1000)
        context.build_time_ms = build_time_ms

        # Record metrics
        self._record_metrics(request, context)

        logger.debug(
            f"Built context for session {request.session_id}: "
            f"{len(context.messages)} messages, "
            f"{len(knowledge_chunks)} knowledge chunks, "
            f"~{context.context_tokens} tokens in {build_time_ms}ms"
        )

        return ContextResponse(
            context=context,
            success=True,
            warnings=warnings,
            build_time_ms=build_time_ms,
        )

    async def _get_knowledge(
        self,
        tenant_id: UUID,
        query: str,
        intent: Optional[str],
    ) -> list[KnowledgeChunk]:
        """Retrieve knowledge from Knowledge Service."""
        try:
            if intent:
                return await self._knowledge.query_by_intent(
                    tenant_id=tenant_id,
                    intent=intent,
                    query=query,
                )
            return await self._knowledge.query(
                tenant_id=tenant_id,
                query=query,
            )
        except Exception as e:
            logger.warning(
                "Knowledge retrieval failed",
                extra={
                    "tenant_id": str(tenant_id),
                    "query": query[:100] if query else None,
                    "error": str(e),
                    "error_type": type(e).__name__,
                },
            )
            return []

    def _record_metrics(
        self,
        request: ContextRequest,
        context: ContextWindow,
    ) -> None:
        """Record Prometheus metrics."""
        mode = "rag" if request.needs_rag else "normal"

        CTX_BUILD_TOTAL.labels(
            tenant_id=str(request.tenant_id)[:8],
            mode=mode,
        ).inc()

        if context.build_time_ms:
            CTX_BUILD_LATENCY.observe(context.build_time_ms / 1000)

        if context.context_tokens:
            CTX_TOKEN_COUNT.observe(context.context_tokens)

    async def build_simple_context(
        self,
        tenant_id: UUID,
        session_id: UUID,
        message: str,
        cognitive_state: Optional[CognitiveContext] = None,
    ) -> ContextWindow:
        """
        Build a simple context with minimal options.

        Convenience method for common use cases.

        Args:
            tenant_id: Tenant ID
            session_id: Session ID
            message: Current message
            cognitive_state: Optional cognitive state

        Returns:
            ContextWindow
        """
        request = ContextRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            current_message=message,
            cognitive_state=cognitive_state,
        )

        response = await self.build_context(request)
        return response.context

    async def enrich_context(
        self,
        context: ContextWindow,
        knowledge_query: Optional[str] = None,
        additional_constraints: Optional[list[str]] = None,
    ) -> ContextWindow:
        """
        Enrich an existing context with additional information.

        Args:
            context: Existing context
            knowledge_query: Query for additional knowledge
            additional_constraints: Additional constraints to add

        Returns:
            Enriched context
        """
        # Add knowledge if requested
        if knowledge_query:
            chunks = await self._knowledge.query(
                tenant_id=context.tenant_id,
                query=knowledge_query,
            )
            context.knowledge_chunks.extend(chunks)

        # Add constraints
        if additional_constraints:
            context.constraints.extend(additional_constraints)

        # Recalculate tokens
        context.context_tokens = self._builder._estimate_tokens(
            context.messages,
            context.knowledge_chunks,
        )

        return context

    def create_message(
        self,
        role: str,
        content: str,
        intent: Optional[str] = None,
        confidence: Optional[float] = None,
    ) -> ContextMessage:
        """
        Create a context message.

        Helper method for creating properly formatted messages.
        """
        return ContextMessage(
            role=MessageRole(role),
            content=content,
            intent=intent,
            confidence=confidence,
        )


# ============================================
# Singleton Instance
# ============================================

_orchestrator: Optional[ContextOrchestrator] = None


def get_context_orchestrator() -> ContextOrchestrator:
    """Get or create the global ContextOrchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = ContextOrchestrator()
    return _orchestrator


async def build_context(
    tenant_id: UUID,
    session_id: UUID,
    message: str,
    needs_rag: bool = False,
    cognitive_state: Optional[CognitiveContext] = None,
    message_history: Optional[list[ContextMessage]] = None,
    constraints: Optional[list[str]] = None,
) -> ContextWindow:
    """
    Convenience function for building context.

    Args:
        tenant_id: Tenant ID
        session_id: Session ID
        message: Current message
        needs_rag: Whether RAG is needed
        cognitive_state: Optional cognitive state
        message_history: Optional message history
        constraints: Optional constraints

    Returns:
        Built ContextWindow
    """
    orchestrator = get_context_orchestrator()

    request = ContextRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        current_message=message,
        needs_rag=needs_rag,
        cognitive_state=cognitive_state,
    )

    response = await orchestrator.build_context(
        request=request,
        message_history=message_history,
        constraints=constraints,
    )

    return response.context
