"""
NeuroStack - CTX (Context Orchestrator)
Assembles and manages context for LLM prompt generation.

The CTX is responsible for:
- Building context windows from multiple sources
- Managing message history with relevance filtering
- Compressing long histories to fit token limits
- Integrating knowledge from the Knowledge Service
- Including policy constraints for the prompt

Usage:
    from src.ctx import ContextOrchestrator, get_context_orchestrator, build_context

    # Using the orchestrator
    orchestrator = get_context_orchestrator()
    response = await orchestrator.build_context(request)

    # Using the convenience function
    context = await build_context(
        tenant_id=tenant_id,
        session_id=session_id,
        message="Qual o horário de funcionamento?",
        needs_rag=True,
    )
"""

from src.core.exceptions import KnowledgeServiceError
from src.ctx.compression import (
    HistoryCompressor,
    SlidingWindowCompressor,
    get_history_compressor,
)
from src.ctx.context_builder import ContextBuilder, get_context_builder
from src.ctx.models import (  # Enums; Core Models
    CognitiveContext,
    CompressionResult,
    ContextMessage,
    ContextRequest,
    ContextResponse,
    ContextWindow,
    KnowledgeChunk,
    MessageRole,
    PolicyConstraint,
    RelevanceLevel,
    SessionInfo,
)

# Note: KnowledgeService is not exported here to avoid circular imports
# Import it directly from src.services.knowledge_service if needed
from src.ctx.orchestrator import (
    ContextOrchestrator,
    build_context,
    get_context_orchestrator,
)
from src.ctx.relevance import (
    RelevanceFilter,
    RelevanceScorer,
    get_relevance_filter,
    get_relevance_scorer,
)

__all__ = [
    # Enums
    "MessageRole",
    "RelevanceLevel",
    # Models
    "ContextMessage",
    "ContextWindow",
    "ContextRequest",
    "ContextResponse",
    "CognitiveContext",
    "KnowledgeChunk",
    "PolicyConstraint",
    "SessionInfo",
    "CompressionResult",
    # Context Builder
    "ContextBuilder",
    "get_context_builder",
    # Relevance
    "RelevanceScorer",
    "RelevanceFilter",
    "get_relevance_scorer",
    "get_relevance_filter",
    # Compression
    "HistoryCompressor",
    "SlidingWindowCompressor",
    "get_history_compressor",
    # Knowledge Service Error (but not the service itself to avoid circular imports)
    "KnowledgeServiceError",
    # Orchestrator
    "ContextOrchestrator",
    "get_context_orchestrator",
    "build_context",
]
