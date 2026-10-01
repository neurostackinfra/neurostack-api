"""Decide when retrieval-augmented generation is needed."""

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Optional, cast

from neurostack_core.presets import get_preset
from neurostack_core.protocol import FunnelStage, IntentCategory
from neurostack_core.srm.models import RagResult

_RAG_PRESET = cast(Mapping[str, object], get_preset().srm["rag"])
_RAW_RAG_PATTERNS = cast(Mapping[str, tuple[str, ...]], _RAG_PRESET["patterns"])
RAG_TRIGGER_PATTERNS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    dict(_RAW_RAG_PATTERNS)
)
RAG_INTENT_CATEGORIES: tuple[IntentCategory, ...] = tuple(
    IntentCategory(category)
    for category in cast(tuple[str, ...], _RAG_PRESET["intent_categories"])
)

_IMPERATIVE_LITERALS = cast(
    Mapping[str, Mapping[str, tuple[str, ...]]],
    get_preset().srm["imperative_literals"],
)
_QUERY_PREFIXES = _IMPERATIVE_LITERALS["srm.rag_decider"]["RagDecider._generate_query"]


class RagDecider:
    """Choose whether a message needs knowledge retrieval."""

    def __init__(
        self,
        custom_patterns: Optional[dict[str, list[str]]] = None,
        confidence_threshold: float = 0.7,
    ):
        self.patterns = {
            topic: list(patterns) for topic, patterns in RAG_TRIGGER_PATTERNS.items()
        }
        if custom_patterns:
            for topic, patterns in custom_patterns.items():
                if topic in self.patterns:
                    self.patterns[topic].extend(patterns)
                else:
                    self.patterns[topic] = list(patterns)

        self.confidence_threshold = confidence_threshold
        self._compiled: dict[str, list[re.Pattern[str]]] = {}
        for topic, patterns in self.patterns.items():
            self._compiled[topic] = [
                re.compile(pattern, re.IGNORECASE | re.UNICODE) for pattern in patterns
            ]

    def decide(
        self,
        message: str,
        intent: str,
        intent_category: IntentCategory,
        confidence: float,
        funnel_stage: FunnelStage,
        flags: dict[str, bool],
    ) -> RagResult:
        """Return the RAG decision for a classified message."""
        message_lower = message.strip().lower()
        needs_rag_category = intent_category in RAG_INTENT_CATEGORIES

        matched_topics: list[str] = []
        for topic, patterns in self._compiled.items():
            for pattern in patterns:
                if pattern.search(message_lower):
                    matched_topics.append(topic)
                    break

        needs_rag = False
        query_suggestion = None
        reason = None

        if matched_topics:
            needs_rag = True
            query_suggestion = self._generate_query(message_lower, matched_topics[0])
            reason = f"Question about {', '.join(matched_topics)}"
        elif needs_rag_category and confidence < self.confidence_threshold:
            needs_rag = True
            query_suggestion = message_lower
            reason = "Low confidence question - RAG may help"
        elif self._is_knowledge_intent(intent):
            needs_rag = True
            query_suggestion = message_lower
            reason = f"Knowledge-seeking intent: {intent}"

        if intent_category in [
            IntentCategory.GREETING,
            IntentCategory.FAREWELL,
            IntentCategory.CONFIRMATION,
        ]:
            needs_rag = False
            query_suggestion = None
            reason = None

        if flags.get("rag_disabled", False):
            needs_rag = False
            reason = None

        return RagResult(
            needs_rag=needs_rag,
            query_suggestion=query_suggestion,
            reason=reason,
        )

    def _is_knowledge_intent(self, intent: str) -> bool:
        """Return whether an intent normally requires knowledge lookup."""
        knowledge_intents = [
            "question_price",
            "question_location",
            "question_hours",
            "question_services",
            "question_insurance",
            "question_general",
            "information_request",
        ]
        return intent in knowledge_intents

    def _generate_query(self, message: str, topic: str) -> str:
        """Generate the preset-specific query prefix for a RAG topic."""
        topic_prefixes = {
            "services": _QUERY_PREFIXES[0],
            "professionals": _QUERY_PREFIXES[1],
            "location": _QUERY_PREFIXES[2],
            "hours": _QUERY_PREFIXES[3],
            "pricing": _QUERY_PREFIXES[4],
            "insurance": _QUERY_PREFIXES[5],
            "preparation": _QUERY_PREFIXES[6],
            "policies": _QUERY_PREFIXES[7],
            "faq": "",
        }

        prefix = topic_prefixes.get(topic, "")
        if prefix and prefix not in message:
            return f"{prefix}: {message}"
        return message

    def get_matched_topics(self, message: str) -> list[str]:
        """Get all preset topics matched by a message."""
        message_lower = message.strip().lower()
        matched: list[str] = []
        for topic, patterns in self._compiled.items():
            for pattern in patterns:
                if pattern.search(message_lower):
                    matched.append(topic)
                    break
        return matched


_decider: Optional[RagDecider] = None


def get_rag_decider(confidence_threshold: float = 0.7) -> RagDecider:
    """Get or create the global RAG decider."""
    global _decider
    if _decider is None:
        _decider = RagDecider(confidence_threshold=confidence_threshold)
    return _decider
