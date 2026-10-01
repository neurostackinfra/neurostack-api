"""Safety concern detection backed by the selected SRM preset."""

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Optional, TypedDict, cast

from neurostack_core.presets import get_preset
from neurostack_core.srm.models import SafetyCategory, SafetyResult


class SafetyPatternDefinition(TypedDict):
    """Complete pattern definition for one safety category."""

    description: str
    patterns: list[str]
    keywords: list[str]
    action: str
    urgency: str


class SafetyPatternOverrides(TypedDict, total=False):
    """Supported per-category customizations."""

    patterns: list[str]
    keywords: list[str]


def _load_safety_patterns() -> Mapping[SafetyCategory, Mapping[str, object]]:
    raw = cast(Mapping[str, Mapping[str, object]], get_preset().srm["safety_patterns"])
    definitions: dict[SafetyCategory, Mapping[str, object]] = {}
    for category, definition in raw.items():
        definitions[SafetyCategory(category)] = MappingProxyType(
            {
                "description": cast(str, definition["description"]),
                "patterns": cast(tuple[str, ...], definition["patterns"]),
                "keywords": cast(tuple[str, ...], definition["keywords"]),
                "action": cast(str, definition["action"]),
                "urgency": cast(str, definition["urgency"]),
            }
        )
    return MappingProxyType(definitions)


SAFETY_PATTERNS: Mapping[SafetyCategory, Mapping[str, object]] = _load_safety_patterns()

_IMPERATIVE_LITERALS = cast(
    Mapping[str, Mapping[str, tuple[str, ...]]],
    get_preset().srm["imperative_literals"],
)
_RECOMMENDED_RESPONSES = _IMPERATIVE_LITERALS["srm.safety_detector"][
    "SafetyDetector.get_recommended_response"
]


class SafetyDetector:
    """Detect clinical, legal, and conduct safety concerns in messages."""

    def __init__(
        self,
        safety_threshold: float = 0.5,
        custom_patterns: Optional[dict[SafetyCategory, SafetyPatternOverrides]] = None,
    ):
        self.threshold = safety_threshold
        self.patterns: dict[SafetyCategory, SafetyPatternDefinition] = {
            category: {
                "description": cast(str, definition["description"]),
                "patterns": list(cast(tuple[str, ...], definition["patterns"])),
                "keywords": list(cast(tuple[str, ...], definition["keywords"])),
                "action": cast(str, definition["action"]),
                "urgency": cast(str, definition["urgency"]),
            }
            for category, definition in SAFETY_PATTERNS.items()
        }
        if custom_patterns:
            for category, patterns in custom_patterns.items():
                if category in self.patterns:
                    self.patterns[category]["patterns"].extend(
                        patterns.get("patterns", [])
                    )
                    self.patterns[category]["keywords"].extend(
                        patterns.get("keywords", [])
                    )

        self._compiled: dict[SafetyCategory, list[re.Pattern[str]]] = {}
        for category, definition in self.patterns.items():
            self._compiled[category] = [
                re.compile(pattern, re.IGNORECASE | re.UNICODE)
                for pattern in definition.get("patterns", [])
            ]

    def detect(self, message: str) -> SafetyResult:
        """Return the highest-confidence safety assessment for a message."""
        message = message.strip().lower()
        if not message:
            return SafetyResult(is_safe=True)

        highest_category = SafetyCategory.NONE
        highest_confidence = 0.0
        all_triggers: list[str] = []

        for category in [
            SafetyCategory.CLINICAL_RISK,
            SafetyCategory.CONDUCT_RISK,
            SafetyCategory.LEGAL_RISK,
        ]:
            confidence, triggers = self._check_category(message, category)
            if confidence > highest_confidence:
                highest_confidence = confidence
                highest_category = category
                all_triggers = triggers

        if highest_confidence < self.threshold:
            return SafetyResult(is_safe=True)

        category_def = self.patterns.get(highest_category)
        action = category_def["action"] if category_def is not None else "warn"
        return SafetyResult(
            is_safe=False,
            category=highest_category,
            confidence=round(highest_confidence, 3),
            triggers=all_triggers,
            action=action,
        )

    def _check_category(
        self,
        message: str,
        category: SafetyCategory,
    ) -> tuple[float, list[str]]:
        """Score a message against one safety category."""
        triggers: list[str] = []
        score = 0.0

        for pattern in self._compiled.get(category, []):
            match = pattern.search(message)
            if match:
                triggers.append(match.group())
                score += 0.4

        definition = self.patterns.get(category)
        keywords = definition["keywords"] if definition is not None else []
        for keyword in keywords:
            if keyword.lower() in message:
                if keyword not in triggers:
                    triggers.append(keyword)
                score += 0.2

        score = min(score, 1.0)
        if category == SafetyCategory.CLINICAL_RISK and triggers:
            score = max(score, 0.6)
        return score, triggers

    def is_emergency(self, message: str) -> bool:
        """Return whether a message meets the clinical emergency threshold."""
        result = self.detect(message)
        return (
            not result.is_safe
            and result.category == SafetyCategory.CLINICAL_RISK
            and result.confidence >= 0.6
        )

    def get_recommended_response(self, result: SafetyResult) -> str:
        """Get the selected preset's response for a safety result."""
        if result.is_safe:
            return ""

        templates = {
            SafetyCategory.CLINICAL_RISK: _RECOMMENDED_RESPONSES[0],
            SafetyCategory.LEGAL_RISK: _RECOMMENDED_RESPONSES[1],
            SafetyCategory.CONDUCT_RISK: _RECOMMENDED_RESPONSES[2],
        }
        return templates.get(result.category, "")


_detector: Optional[SafetyDetector] = None


def get_safety_detector(threshold: float = 0.5) -> SafetyDetector:
    """Get or create the global safety detector."""
    global _detector
    if _detector is None:
        _detector = SafetyDetector(safety_threshold=threshold)
    return _detector
