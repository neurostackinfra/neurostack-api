"""
NeuroStack - CTX - History Compression
Compresses long conversation history to fit within context limits.
"""

import logging
from typing import Optional

from src.ctx.models import (
    ContextMessage,
    ContextWindow,
    CompressionResult,
    MessageRole,
)

logger = logging.getLogger(__name__)


# ============================================
# Compression Strategies
# ============================================

# Characters per token (approximation)
CHARS_PER_TOKEN = 4

# Minimum messages to keep uncompressed
MIN_RECENT_MESSAGES = 3

# Template for summary
SUMMARY_TEMPLATE = """Resumo da conversa anterior:
- Assunto: {topic}
- Intenção principal: {intent}
- Informações coletadas: {slots}
- Status: {status}
"""


class HistoryCompressor:
    """
    Compresses long conversation history.
    
    Strategies:
    1. Keep N most recent messages uncompressed
    2. Summarize older messages into a single context
    3. Remove redundant or low-information messages
    4. Truncate very long individual messages
    """

    def __init__(
        self,
        max_tokens: int = 4000,
        min_recent: int = MIN_RECENT_MESSAGES,
        max_message_length: int = 500,
    ):
        """
        Initialize the compressor.
        
        Args:
            max_tokens: Maximum tokens for context
            min_recent: Minimum recent messages to keep
            max_message_length: Maximum characters per message
        """
        self.max_tokens = max_tokens
        self.min_recent = min_recent
        self.max_message_length = max_message_length

    def compress(
        self,
        messages: list[ContextMessage],
        target_tokens: Optional[int] = None,
    ) -> tuple[list[ContextMessage], Optional[str]]:
        """
        Compress message history.
        
        Args:
            messages: Messages to compress
            target_tokens: Target token count (default: max_tokens)
            
        Returns:
            Tuple of (compressed messages, summary of removed messages)
        """
        target = target_tokens or self.max_tokens
        
        # Estimate current tokens
        current_tokens = self._estimate_tokens(messages)
        
        if current_tokens <= target:
            # No compression needed
            return messages, None
        
        logger.info(
            f"Compressing history: {len(messages)} messages, "
            f"~{current_tokens} tokens -> target {target}"
        )
        
        # Step 1: Truncate long messages
        truncated = self._truncate_messages(messages)
        current_tokens = self._estimate_tokens(truncated)
        
        if current_tokens <= target:
            return truncated, None
        
        # Step 2: Keep recent, summarize old
        recent = truncated[-self.min_recent:]
        older = truncated[:-self.min_recent]
        
        # Generate summary of older messages
        summary = self._generate_summary(older)
        
        # Step 3: Continue removing if still too long
        while current_tokens > target and len(recent) > self.min_recent:
            recent = recent[1:]  # Remove oldest
            current_tokens = self._estimate_tokens(recent) + len(summary) // CHARS_PER_TOKEN
        
        return recent, summary

    def _estimate_tokens(self, messages: list[ContextMessage]) -> int:
        """Estimate token count for messages."""
        total_chars = sum(len(m.content) + 10 for m in messages)
        return total_chars // CHARS_PER_TOKEN

    def _truncate_messages(
        self,
        messages: list[ContextMessage],
    ) -> list[ContextMessage]:
        """Truncate individual messages that are too long."""
        truncated = []
        
        for msg in messages:
            if len(msg.content) > self.max_message_length:
                # Truncate with ellipsis
                new_content = msg.content[:self.max_message_length - 3] + "..."
                new_msg = ContextMessage(
                    role=msg.role,
                    content=new_content,
                    timestamp=msg.timestamp,
                    intent=msg.intent,
                    confidence=msg.confidence,
                    metadata={**msg.metadata, "truncated": True},
                )
                truncated.append(new_msg)
            else:
                truncated.append(msg)
        
        return truncated

    def _generate_summary(
        self,
        messages: list[ContextMessage],
    ) -> str:
        """
        Generate a summary of messages.
        
        In production, this would use an LLM. For now, we create
        a simple structured summary.
        """
        if not messages:
            return ""
        
        # Extract information
        user_messages = [m for m in messages if m.role == MessageRole.USER]
        intents = [m.intent for m in messages if m.intent]
        
        # Determine topic from first user message
        topic = user_messages[0].content[:50] + "..." if user_messages else "N/A"
        
        # Main intent
        main_intent = intents[-1] if intents else "unknown"
        
        # Status
        status = f"{len(messages)} mensagens anteriores resumidas"
        
        summary = SUMMARY_TEMPLATE.format(
            topic=topic,
            intent=main_intent,
            slots="(informações coletadas anteriormente)",
            status=status,
        )
        
        return summary

    def get_compression_result(
        self,
        original: list[ContextMessage],
        compressed: list[ContextMessage],
        summary: Optional[str],
    ) -> CompressionResult:
        """Get compression statistics."""
        original_tokens = self._estimate_tokens(original)
        compressed_tokens = self._estimate_tokens(compressed)
        
        if summary:
            compressed_tokens += len(summary) // CHARS_PER_TOKEN
        
        return CompressionResult(
            original_token_count=original_tokens,
            compressed_token_count=compressed_tokens,
            compression_ratio=compressed_tokens / original_tokens if original_tokens > 0 else 1.0,
            summary=summary,
            removed_messages=len(original) - len(compressed),
        )


class SlidingWindowCompressor:
    """
    Simple sliding window compressor.
    
    Keeps only the N most recent messages.
    """

    def __init__(self, window_size: int = 10):
        """
        Initialize with window size.
        
        Args:
            window_size: Number of recent messages to keep
        """
        self.window_size = window_size

    def compress(
        self,
        messages: list[ContextMessage],
    ) -> list[ContextMessage]:
        """
        Keep only the most recent messages.
        
        Args:
            messages: Messages to compress
            
        Returns:
            Windowed messages
        """
        if len(messages) <= self.window_size:
            return messages
        
        return messages[-self.window_size:]


# ============================================
# Singleton Instance
# ============================================

_compressor: Optional[HistoryCompressor] = None


def get_history_compressor() -> HistoryCompressor:
    """Get or create the global HistoryCompressor instance."""
    global _compressor
    if _compressor is None:
        _compressor = HistoryCompressor()
    return _compressor

