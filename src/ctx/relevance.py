"""
NeuroStack - CTX - Relevance Logic
Determines what context items are relevant for the current conversation.
"""

import logging
from typing import Optional

from src.ctx.models import (
    ContextMessage,
    ContextWindow,
    KnowledgeChunk,
    RelevanceLevel,
    MessageRole,
)

logger = logging.getLogger(__name__)


# ============================================
# Relevance Weights
# ============================================

# How much recency affects relevance (0-1)
RECENCY_WEIGHT = 0.4

# How much semantic similarity affects relevance (0-1)
SIMILARITY_WEIGHT = 0.4

# How much role affects relevance (0-1)
ROLE_WEIGHT = 0.2

# Role relevance scores
ROLE_SCORES = {
    MessageRole.SYSTEM: 1.0,
    MessageRole.USER: 0.9,
    MessageRole.ASSISTANT: 0.7,
}

# Keywords that increase relevance
HIGH_RELEVANCE_KEYWORDS = [
    "confirmar", "agendar", "cancelar", "horário",
    "urgente", "emergência", "problema", "erro",
    "sim", "não", "ok", "certo",
]


class RelevanceScorer:
    """
    Scores the relevance of context items.
    
    Uses multiple signals to determine relevance:
    1. Recency - more recent messages are more relevant
    2. Semantic similarity - messages related to current topic
    3. Role - system/user messages often more important
    4. Intent continuity - messages in same intent chain
    """

    def __init__(
        self,
        recency_weight: float = RECENCY_WEIGHT,
        similarity_weight: float = SIMILARITY_WEIGHT,
        role_weight: float = ROLE_WEIGHT,
    ):
        """
        Initialize the relevance scorer.
        
        Args:
            recency_weight: Weight for recency score
            similarity_weight: Weight for similarity score
            role_weight: Weight for role score
        """
        self.recency_weight = recency_weight
        self.similarity_weight = similarity_weight
        self.role_weight = role_weight

    def score_message(
        self,
        message: ContextMessage,
        current_message: str,
        position: int,
        total_messages: int,
    ) -> float:
        """
        Score the relevance of a message.
        
        Args:
            message: Message to score
            current_message: Current user message
            position: Position in message list (0 = oldest)
            total_messages: Total number of messages
            
        Returns:
            Relevance score (0-1)
        """
        # Recency score (more recent = higher)
        recency_score = (position + 1) / total_messages if total_messages > 0 else 1.0
        
        # Role score
        role_score = ROLE_SCORES.get(message.role, 0.5)
        
        # Similarity score (keyword-based for now)
        similarity_score = self._compute_similarity(message.content, current_message)
        
        # Weighted combination
        final_score = (
            self.recency_weight * recency_score +
            self.similarity_weight * similarity_score +
            self.role_weight * role_score
        )
        
        return min(final_score, 1.0)

    def _compute_similarity(
        self,
        text1: str,
        text2: str,
    ) -> float:
        """
        Compute simple similarity between two texts.
        
        Uses keyword overlap. In production, this would use embeddings.
        """
        words1 = set(text1.lower().split())
        words2 = set(text2.lower().split())
        
        if not words1 or not words2:
            return 0.5
        
        # Jaccard similarity
        intersection = len(words1 & words2)
        union = len(words1 | words2)
        
        jaccard = intersection / union if union > 0 else 0.0
        
        # Boost for high-relevance keywords
        keyword_boost = 0.0
        for keyword in HIGH_RELEVANCE_KEYWORDS:
            if keyword in text1.lower() and keyword in text2.lower():
                keyword_boost += 0.1
        
        return min(jaccard + keyword_boost, 1.0)

    def score_knowledge_chunk(
        self,
        chunk: KnowledgeChunk,
        current_message: str,
    ) -> float:
        """
        Score the relevance of a knowledge chunk.
        
        Args:
            chunk: Knowledge chunk to score
            current_message: Current user message
            
        Returns:
            Relevance score (0-1)
        """
        # Start with the chunk's existing relevance score
        base_score = chunk.relevance_score
        
        # Add similarity to current message
        similarity = self._compute_similarity(chunk.content, current_message)
        
        # Combine
        return (base_score * 0.6 + similarity * 0.4)

    def get_relevance_level(self, score: float) -> RelevanceLevel:
        """Convert numeric score to relevance level."""
        if score >= 0.8:
            return RelevanceLevel.CRITICAL
        elif score >= 0.6:
            return RelevanceLevel.HIGH
        elif score >= 0.4:
            return RelevanceLevel.MEDIUM
        elif score >= 0.2:
            return RelevanceLevel.LOW
        else:
            return RelevanceLevel.NONE


class RelevanceFilter:
    """
    Filters context items by relevance.
    """

    def __init__(
        self,
        scorer: Optional[RelevanceScorer] = None,
        min_relevance: float = 0.2,
    ):
        """
        Initialize the filter.
        
        Args:
            scorer: Relevance scorer to use
            min_relevance: Minimum relevance score to include
        """
        self.scorer = scorer or RelevanceScorer()
        self.min_relevance = min_relevance

    def filter_messages(
        self,
        messages: list[ContextMessage],
        current_message: str,
        max_messages: int,
    ) -> list[ContextMessage]:
        """
        Filter messages by relevance, keeping most relevant.
        
        Args:
            messages: Messages to filter
            current_message: Current user message
            max_messages: Maximum messages to keep
            
        Returns:
            Filtered list of messages
        """
        if len(messages) <= max_messages:
            return messages
        
        # Score all messages
        scored = []
        for i, msg in enumerate(messages):
            score = self.scorer.score_message(
                msg, current_message, i, len(messages)
            )
            scored.append((msg, score))
        
        # Sort by score (highest first)
        scored.sort(key=lambda x: x[1], reverse=True)
        
        # Keep top N by score
        top_messages = [msg for msg, _ in scored[:max_messages]]
        
        # Re-sort by original order (chronological)
        message_order = {id(msg): i for i, msg in enumerate(messages)}
        top_messages.sort(key=lambda m: message_order.get(id(m), 0))
        
        return top_messages

    def filter_knowledge(
        self,
        chunks: list[KnowledgeChunk],
        current_message: str,
        max_chunks: int = 5,
    ) -> list[KnowledgeChunk]:
        """
        Filter knowledge chunks by relevance.
        
        Args:
            chunks: Knowledge chunks to filter
            current_message: Current user message
            max_chunks: Maximum chunks to keep
            
        Returns:
            Filtered list of chunks
        """
        if len(chunks) <= max_chunks:
            return chunks
        
        # Score and sort
        scored = [
            (chunk, self.scorer.score_knowledge_chunk(chunk, current_message))
            for chunk in chunks
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        
        # Keep top N
        return [chunk for chunk, _ in scored[:max_chunks]]


# ============================================
# Singleton Instances
# ============================================

_scorer: Optional[RelevanceScorer] = None
_filter: Optional[RelevanceFilter] = None


def get_relevance_scorer() -> RelevanceScorer:
    """Get or create the global RelevanceScorer instance."""
    global _scorer
    if _scorer is None:
        _scorer = RelevanceScorer()
    return _scorer


def get_relevance_filter() -> RelevanceFilter:
    """Get or create the global RelevanceFilter instance."""
    global _filter
    if _filter is None:
        _filter = RelevanceFilter()
    return _filter

