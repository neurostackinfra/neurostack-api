"""Regex-based intent classification backed by the selected SRM preset."""

import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Optional, TypedDict, cast

from neurostack_core.presets import get_preset
from neurostack_core.srm.models import IntentCategory, IntentResult


class IntentDefinition(TypedDict):
    """Complete definition used by the default intent classifier."""

    category: IntentCategory
    patterns: list[str]
    keywords: list[str]
    weight: float


class EnglishIntentPatterns(TypedDict):
    """English pattern and keyword overrides for an intent."""

    patterns: list[str]
    keywords: list[str]


def _load_intent_definitions() -> Mapping[str, Mapping[str, object]]:
    raw = cast(Mapping[str, Mapping[str, object]], get_preset().srm["intents"])
    definitions: dict[str, Mapping[str, object]] = {}
    for name, definition in raw.items():
        definitions[name] = MappingProxyType(
            {
                "category": IntentCategory(cast(str, definition["category"])),
                "patterns": cast(tuple[str, ...], definition["patterns"]),
                "keywords": cast(tuple[str, ...], definition["keywords"]),
                "weight": float(cast(float, definition["weight"])),
            }
        )
    return MappingProxyType(definitions)


def _load_english_patterns() -> Mapping[str, Mapping[str, tuple[str, ...]]]:
    raw = cast(Mapping[str, Mapping[str, object]], get_preset().srm["intents_en"])
    definitions: dict[str, Mapping[str, tuple[str, ...]]] = {}
    for name, definition in raw.items():
        definitions[name] = MappingProxyType(
            {
                "patterns": cast(tuple[str, ...], definition["patterns"]),
                "keywords": cast(tuple[str, ...], definition["keywords"]),
            }
        )
    return MappingProxyType(definitions)


INTENT_DEFINITIONS: Mapping[str, Mapping[str, object]] = _load_intent_definitions()
EN_INTENT_PATTERNS: Mapping[str, Mapping[str, tuple[str, ...]]] = (
    _load_english_patterns()
)
_SAFETY_GUARD_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE | re.UNICODE)
    for pattern in cast(tuple[str, ...], get_preset().srm["safety_guards"])
)


class IntentClassifier:
    """Regex-based intent classifier with safety guard clauses."""

    def __init__(
        self,
        custom_intents: Optional[dict[str, IntentDefinition]] = None,
    ):
        self.intents: dict[str, IntentDefinition] = {
            name: {
                "category": cast(IntentCategory, definition["category"]),
                "patterns": list(cast(tuple[str, ...], definition["patterns"])),
                "keywords": list(cast(tuple[str, ...], definition["keywords"])),
                "weight": cast(float, definition["weight"]),
            }
            for name, definition in INTENT_DEFINITIONS.items()
        }
        if custom_intents:
            self.intents.update(custom_intents)

        self._compiled_patterns: dict[str, list[re.Pattern[str]]] = {}
        for intent_name, intent_def in self.intents.items():
            self._compiled_patterns[intent_name] = [
                re.compile(pattern, re.IGNORECASE | re.UNICODE)
                for pattern in intent_def.get("patterns", [])
            ]

        self._compiled_patterns_en: dict[str, list[re.Pattern[str]]] = {}
        self._en_keywords: dict[str, tuple[str, ...]] = {}
        for intent_name, en_def in EN_INTENT_PATTERNS.items():
            self._compiled_patterns_en[intent_name] = [
                re.compile(pattern, re.IGNORECASE | re.UNICODE)
                for pattern in en_def.get("patterns", [])
            ]
            self._en_keywords[intent_name] = en_def.get("keywords", ())

    def classify(self, message: str, language: str = "pt-BR") -> IntentResult:
        """Classify a message, applying safety guards before intent scoring."""
        message_lower = message.strip().lower()

        if not message_lower:
            return IntentResult(
                intent="duvida_generica_sem_contexto",
                category=IntentCategory.UNKNOWN,
                confidence=0.0,
                matched_patterns=[],
            )

        for pattern in _SAFETY_GUARD_PATTERNS:
            if pattern.search(message_lower):
                return IntentResult(
                    intent="pos_atendimento_sintomas_preocupantes",
                    category=IntentCategory.EMERGENCY,
                    confidence=1.0,
                    matched_patterns=[pattern.pattern],
                )

        return self._classify_with_regex(message_lower, language=language)

    def _classify_with_regex(
        self, message: str, language: str = "pt-BR"
    ) -> IntentResult:
        """Classify using regex patterns."""
        best_intent = "duvida_generica_sem_contexto"
        best_category = IntentCategory.QUESTION
        best_score = 0.0
        second_best_score = 0.0
        matched_patterns: list[str] = []

        for intent_name, intent_def in self.intents.items():
            score, patterns = self._score_intent(
                message, intent_name, intent_def, language=language
            )

            if score > best_score:
                second_best_score = best_score
                best_score = score
                best_intent = intent_name
                best_category = intent_def["category"]
                matched_patterns = patterns
            elif score > second_best_score:
                second_best_score = score

        confidence = min(best_score, 1.0)
        if best_score > 0 and best_score > second_best_score:
            confidence = max(confidence, 0.95)

        return IntentResult(
            intent=best_intent,
            category=best_category,
            confidence=round(confidence, 3),
            matched_patterns=matched_patterns,
        )

    def _score_intent(
        self,
        message: str,
        intent_name: str,
        intent_def: IntentDefinition,
        language: str = "pt-BR",
    ) -> tuple[float, list[str]]:
        """Calculate an intent score from regex and keyword matches."""
        score = 0.0
        matched: list[str] = []
        weight = intent_def.get("weight", 1.0)

        use_en = (
            language.lower().startswith("en")
            and intent_name in self._compiled_patterns_en
        )
        if use_en:
            patterns = self._compiled_patterns_en.get(intent_name, [])
            keywords: Sequence[str] = self._en_keywords.get(intent_name, ())
        else:
            patterns = self._compiled_patterns.get(intent_name, [])
            keywords = intent_def.get("keywords", [])

        for pattern in patterns:
            if pattern.search(message):
                score += 0.5 * weight
                matched.append(pattern.pattern)

        keyword_matches = sum(1 for keyword in keywords if keyword.lower() in message)
        if keywords:
            score += (keyword_matches / len(keywords)) * 0.4 * weight

        if len(matched) > 1:
            score += 0.1 * len(matched)

        return score, matched

    def get_category_intents(self, category: IntentCategory) -> list[str]:
        """Get all intents for a category."""
        return [
            name
            for name, definition in self.intents.items()
            if definition["category"] == category
        ]

    def add_custom_intent(
        self,
        name: str,
        category: IntentCategory,
        patterns: list[str],
        keywords: list[str],
        weight: float = 1.0,
    ) -> None:
        """Add a custom intent definition."""
        self.intents[name] = {
            "category": category,
            "patterns": patterns,
            "keywords": keywords,
            "weight": weight,
        }
        self._compiled_patterns[name] = [
            re.compile(pattern, re.IGNORECASE | re.UNICODE) for pattern in patterns
        ]


_classifier: Optional[IntentClassifier] = None


def get_intent_classifier() -> IntentClassifier:
    """Get or create the global intent classifier."""
    global _classifier
    if _classifier is None:
        _classifier = IntentClassifier()
    return _classifier
