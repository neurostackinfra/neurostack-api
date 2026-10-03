"""Local, deterministic opt-in language switching for conversations.

The detector is deliberately limited to a fixed Latin-language corpus.  It is
not intended to make universal out-of-distribution language claims.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Literal, Optional, cast

from lingua import Language, LanguageDetector, LanguageDetectorBuilder

from src.config.logging import get_logger

logger = get_logger(__name__)

ConfiguredLanguage = Literal["pt-BR", "en"]
DetectedLanguage = Literal["pt-BR", "en", "es", "fr", "it", "de", "nl"]
Decision = Literal["abstain", "propose", "accept", "reject", "ignore"]

MIN_ALPHABETIC_TOKENS = 5
MIN_CONFIDENCE = 0.80
MIN_MARGIN = 0.20
MIN_TOKEN_CONFIDENCE = 0.25
MIN_MIXED_LANGUAGE_EVIDENCE = 0.30
MAX_IDNA_HOSTNAME_LENGTH = 253

ENGLISH_PROMPT = "Would you like to continue in English?"
PORTUGUESE_PROMPT = "Deseja continuar em português?"

_DETECTOR_LANGUAGES = (
    Language.PORTUGUESE,
    Language.ENGLISH,
    Language.SPANISH,
    Language.FRENCH,
    Language.ITALIAN,
    Language.GERMAN,
    Language.DUTCH,
)
_LANGUAGE_CODES: dict[Language, DetectedLanguage] = {
    Language.PORTUGUESE: "pt-BR",
    Language.ENGLISH: "en",
    Language.SPANISH: "es",
    Language.FRENCH: "fr",
    Language.ITALIAN: "it",
    Language.GERMAN: "de",
    Language.DUTCH: "nl",
}
_SWITCHABLE_LANGUAGES = frozenset(("pt-BR", "en"))
_ACCEPTANCE_PHRASES: dict[str, frozenset[str]] = {
    "en": frozenset(("yes", "yes please")),
    "pt-BR": frozenset(("sim", "sim por favor")),
}
_REFUSAL_PHRASES = frozenset(("no", "no thanks", "não", "não obrigado"))
_TERMINAL_PUNCTUATION = ".!?"
_IDNA_SEPARATOR_TRANSLATION = str.maketrans(
    {"\u3002": ".", "\uff0e": ".", "\uff61": "."}
)
_WWW_PATTERN = re.compile(r"\bwww\.", re.IGNORECASE)
_BARE_DOMAIN_PATTERN = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:[a-z]{2,63}|xn--[a-z0-9-]{2,59})",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LanguageDetection:
    """A raw detector winner plus its independent switching eligibility."""

    language: Optional[DetectedLanguage]
    confidence: Optional[float]
    margin: Optional[float]
    eligible_for_switch: bool = True


@dataclass(frozen=True)
class LanguageSwitchResult:
    """Pure state-machine output; only two values are eligible for persistence."""

    decision: Decision
    configured_language: ConfiguredLanguage
    active_language: ConfiguredLanguage
    pending_language: Optional[ConfiguredLanguage]
    detected_language: Optional[DetectedLanguage]
    confidence: Optional[float]
    margin: Optional[float]
    prompt: Optional[str] = None

    @property
    def state_updates(self) -> dict[str, Optional[str]]:
        """Return the complete and intentionally narrow persistence patch."""
        return {
            "active_language": self.active_language,
            "pending_language": self.pending_language,
        }


def normalize_configured_language(value: Any) -> ConfiguredLanguage:
    """Normalize tenant configuration; untrusted request values are not inputs."""
    if isinstance(value, str) and value in _SWITCHABLE_LANGUAGES:
        return cast(ConfiguredLanguage, value)
    return "pt-BR"


def _normalize_active_language(
    value: Any, configured_language: ConfiguredLanguage
) -> ConfiguredLanguage:
    if isinstance(value, str) and value in _SWITCHABLE_LANGUAGES:
        return cast(ConfiguredLanguage, value)
    return configured_language


def _normalize_pending_language(value: Any) -> Optional[ConfiguredLanguage]:
    if isinstance(value, str) and value in _SWITCHABLE_LANGUAGES:
        return cast(ConfiguredLanguage, value)
    return None


def normalize_confirmation(text: str) -> str:
    """Normalize a confirmation without erasing internal punctuation or words."""
    return (
        unicodedata.normalize("NFKC", text)
        .strip()
        .casefold()
        .rstrip(_TERMINAL_PUNCTUATION)
    )


def _alphabetic_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    for character in text:
        if character.isalpha():
            current.append(character)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))
    return tokens


def _contains_non_latin_letter(text: str) -> bool:
    for character in text:
        if character.isalpha() and not unicodedata.name(character, "").startswith(
            "LATIN "
        ):
            return True
    return False


def _idna_hostname_candidates(text: str) -> list[str]:
    """Extract hostname-shaped runs independently of whitespace or punctuation."""
    candidates: list[str] = []
    current: list[str] = []

    for character in text:
        is_mark = unicodedata.category(character).startswith("M")
        if character.isalnum() or is_mark or character in ".-":
            current.append(character)
            continue

        candidate = "".join(current).strip(".")
        if "." in candidate:
            candidates.append(candidate)
        current = []

    candidate = "".join(current).strip(".")
    if "." in candidate:
        candidates.append(candidate)
    return candidates


def _contains_uri_scheme(text: str) -> bool:
    """Recognize an RFC-style scheme in one forward pass."""
    index = 0
    while index < len(text):
        character = text[index]
        previous_is_word = index > 0 and (
            text[index - 1].isalnum() or text[index - 1] == "_"
        )
        if not character.isascii() or not character.isalpha() or previous_is_word:
            index += 1
            continue

        index += 1
        while index < len(text):
            character = text[index]
            if character.isascii() and (character.isalnum() or character in "+.-"):
                index += 1
                continue
            break
        if index < len(text) and text[index] == ":":
            return True
    return False


def _looks_like_url(text: str) -> bool:
    """Match explicit URI schemes, ``www.``, or plausible bare domains."""
    if _contains_uri_scheme(text) or _WWW_PATTERN.search(text):
        return True

    for hostname in _idna_hostname_candidates(text):
        if len(hostname) > MAX_IDNA_HOSTNAME_LENGTH:
            return True
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii")
        except UnicodeError:
            return True
        if _BARE_DOMAIN_PATTERN.fullmatch(ascii_hostname):
            return True
    return False


def _normalize_for_detection(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_IDNA_SEPARATOR_TRANSLATION)


def _should_abstain_before_detection(text: str) -> bool:
    """Apply input-only guards before either real or injected detection."""
    return (
        _looks_like_url(text)
        or _contains_non_latin_letter(text)
        or len(_alphabetic_tokens(text)) < MIN_ALPHABETIC_TOKENS
    )


def _has_required_evidence(detection: LanguageDetection) -> bool:
    return (
        detection.confidence is not None
        and detection.confidence >= MIN_CONFIDENCE
        and detection.margin is not None
        and detection.margin >= MIN_MARGIN
    )


@lru_cache(maxsize=1)
def get_language_detector() -> LanguageDetector:
    """Return the process singleton restricted to the seven supported models."""
    return LanguageDetectorBuilder.from_languages(*_DETECTOR_LANGUAGES).build()


def _has_mixed_language_evidence(detector: LanguageDetector, tokens: list[str]) -> bool:
    """Identify strong token-level evidence for more than one corpus language."""
    evidence: dict[Language, float] = {}
    for token in tokens:
        values = detector.compute_language_confidence_values(token)
        if len(values) < 2 or values[0].value < MIN_TOKEN_CONFIDENCE:
            continue
        evidence[values[0].language] = evidence.get(values[0].language, 0.0) + (
            float(values[0].value) - float(values[1].value)
        )
    represented_languages = sum(
        score >= MIN_MIXED_LANGUAGE_EVIDENCE for score in evidence.values()
    )
    return represented_languages > 1


def detect_language(text: str) -> LanguageDetection:
    """Detect a supported language, abstaining before or below fixed thresholds."""
    normalized = _normalize_for_detection(text)
    if _should_abstain_before_detection(normalized):
        return LanguageDetection(None, None, None)
    tokens = _alphabetic_tokens(normalized)

    detector = get_language_detector()
    confidence_values = detector.compute_language_confidence_values(normalized)
    if not confidence_values:
        return LanguageDetection(None, None, None)

    top = confidence_values[0]
    runner_up = confidence_values[1] if len(confidence_values) > 1 else None
    confidence = float(top.value)
    margin = confidence - (float(runner_up.value) if runner_up else 0.0)
    raw_language = _LANGUAGE_CODES[top.language]
    detection = LanguageDetection(raw_language, confidence, margin)
    if not _has_required_evidence(detection):
        return LanguageDetection(raw_language, confidence, margin, False)
    if _has_mixed_language_evidence(detector, tokens):
        return LanguageDetection(raw_language, confidence, margin, False)
    return detection


def decide_language_switch(
    *,
    text: str,
    configured_language: Any,
    active_language: Any,
    pending_language: Any,
    detection: Optional[LanguageDetection],
) -> LanguageSwitchResult:
    """Apply the language-switch state machine without side effects."""
    configured = normalize_configured_language(configured_language)
    active = _normalize_active_language(active_language, configured)
    pending = _normalize_pending_language(pending_language)

    if pending is not None:
        confirmation = normalize_confirmation(text)
        if confirmation in _ACCEPTANCE_PHRASES[pending]:
            return LanguageSwitchResult(
                "accept", configured, pending, None, None, None, None
            )
        if confirmation in _REFUSAL_PHRASES:
            return LanguageSwitchResult(
                "reject", configured, active, None, None, None, None
            )
        return LanguageSwitchResult(
            "ignore", configured, active, None, None, None, None
        )

    normalized = _normalize_for_detection(text)
    if _should_abstain_before_detection(normalized):
        return LanguageSwitchResult(
            "abstain", configured, active, None, None, None, None
        )

    if detection is None:
        detection = detect_language(normalized)
    if detection.language is None:
        return LanguageSwitchResult(
            "abstain",
            configured,
            active,
            None,
            None,
            detection.confidence,
            detection.margin,
        )
    if not detection.eligible_for_switch or not _has_required_evidence(detection):
        return LanguageSwitchResult(
            "abstain",
            configured,
            active,
            None,
            detection.language,
            detection.confidence,
            detection.margin,
        )
    if detection.language not in _SWITCHABLE_LANGUAGES:
        return LanguageSwitchResult(
            "ignore",
            configured,
            active,
            None,
            detection.language,
            detection.confidence,
            detection.margin,
        )
    if detection.language == active:
        return LanguageSwitchResult(
            "ignore",
            configured,
            active,
            None,
            detection.language,
            detection.confidence,
            detection.margin,
        )

    target = cast(ConfiguredLanguage, detection.language)
    prompt = ENGLISH_PROMPT if target == "en" else PORTUGUESE_PROMPT
    return LanguageSwitchResult(
        "propose",
        configured,
        active,
        target,
        target,
        detection.confidence,
        detection.margin,
        prompt,
    )


def evaluate_conversation_language(
    *,
    text: str,
    configured_language: Any,
    active_language: Any,
    pending_language: Any,
    tenant_id: Any,
    session_id: Any,
    detection: Optional[LanguageDetection] = None,
) -> LanguageSwitchResult:
    """Evaluate one turn and emit only the approved operational metadata."""
    result = decide_language_switch(
        text=text,
        configured_language=configured_language,
        active_language=active_language,
        pending_language=pending_language,
        detection=detection,
    )
    logger.info(
        "conversation_language_decision",
        tenant_id=str(tenant_id),
        session_id=str(session_id),
        configured_language=result.configured_language,
        active_language=result.active_language,
        detected_language=result.detected_language,
        confidence=(
            round(result.confidence, 2) if result.confidence is not None else None
        ),
        margin=round(result.margin, 2) if result.margin is not None else None,
        decision=result.decision,
    )
    return result


__all__ = [
    "ENGLISH_PROMPT",
    "PORTUGUESE_PROMPT",
    "LanguageDetection",
    "LanguageSwitchResult",
    "decide_language_switch",
    "detect_language",
    "evaluate_conversation_language",
    "get_language_detector",
    "normalize_configured_language",
    "normalize_confirmation",
]
