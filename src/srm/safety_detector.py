"""
NeuroStack - SRM - Safety Detector
Detects safety concerns in messages: clinical, legal, and conduct risks.
"""

import re
from typing import Optional, TypedDict

from src.srm.models import SafetyCategory, SafetyResult


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


# ============================================
# Safety Pattern Definitions
# ============================================

SAFETY_PATTERNS: dict[SafetyCategory, SafetyPatternDefinition] = {
    SafetyCategory.CLINICAL_RISK: {
        "description": "Medical/clinical safety concerns",
        "patterns": [
            # Emergency symptoms
            r"\b(infarto|avc|derrame|convuls\u00e3o|convulsao|desmaio|desmaiando)\b",
            r"\b(n\u00e3o|nao)\b.*\b(consigo|consegue)\b.*\b(respirar|respira)\b",
            r"\b(dor)\b.*\b(peito|t\u00f3rax|torax)\b.*\b(forte|intensa|aguda)\b",
            r"\b(sangue|sangramento)\b.*\b(muito|demais|intenso|n\u00e3o para)\b",
            r"\b(perda)\b.*\b(consci\u00eancia|consciencia|sentidos)\b",
            # Suicidal/self-harm indicators
            r"\b(suic\u00eddio|suicidio|me matar|matar\s?me|acabar com tudo)\b",
            r"\b(n\u00e3o|nao)\b.*\b(quero|aguento)\b.*\b(viver|vida)\b",
            r"\b(automutila\u00e7\u00e3o|automutilacao|me cortar|me machucar)\b",
            # Drug/medication concerns
            r"\b(overdose|intoxica\u00e7\u00e3o|intoxicacao|envenenamento)\b",
            r"\b(tomei|ingerir?)\b.*\b(muito|demais|todo|rem\u00e9dio|medicamento)\b",
            # Pregnancy/birth emergencies
            r"\b(sangramento|hemorragia)\b.*\b(gr\u00e1vida|gravida|gestante)\b",
            r"\b(contra\u00e7\u00f5es|contracoes)\b.*\b(forte|muita|parto)\b",
            # Pediatric emergencies
            r"\b(beb\u00ea|bebe|crian\u00e7a|crianca)\b.*\b(engasgou|engasgando|n\u00e3o respira)\b",
            r"\b(febre)\b.*\b(muito alta|40|41|convuls\u00e3o)\b",
        ],
        "keywords": [
            "emerg\u00eancia m\u00e9dica",
            "urg\u00eancia",
            "samu",
            "upa",
            "pronto socorro",
            "passando mal",
            "dor forte",
            "n\u00e3o consigo respirar",
            "me ajuda",
            "suic\u00eddio",
            "me matar",
            "n\u00e3o aguento mais",
        ],
        "action": "escalate",
        "urgency": "critical",
    },
    SafetyCategory.LEGAL_RISK: {
        "description": "Legal/liability concerns",
        "patterns": [
            # Diagnosis requests
            r"\b(qual|o que)\b.*\b(meu|minha)\b.*\b(doen\u00e7a|diagn\u00f3stico|diagnostico|problema)\b",
            r"\b(me diz|diga)\b.*\b(o que|qual)\b.*\b(tenho|\u00e9)\b",
            r"\b(voc\u00ea|voce|vc)\b.*\b(acha|pensa)\b.*\b(que|o que)\b.*\b(tenho|\u00e9)\b",
            # Medication advice
            r"\b(qual|que)\b.*\b(rem\u00e9dio|medicamento|medica\u00e7\u00e3o)\b.*\b(tomar|usar|comprar)\b",
            r"\b(pode|posso)\b.*\b(tomar|usar)\b.*\b(rem\u00e9dio|medicamento)\b",
            r"\b(me receita|prescreve|indica)\b.*\b(rem\u00e9dio|medicamento)\b",
            # Treatment advice
            r"\b(devo|preciso)\b.*\b(fazer|cirurgia|tratamento|procedimento)\b",
            r"\b(melhor|recomenda)\b.*\b(tratamento|procedimento|cirurgia)\b",
            # False promises
            r"\b(garante|garantia|certeza)\b.*\b(cura|resultado|sucesso)\b",
            r"\b(promete|prometer|vai curar)\b",
        ],
        "keywords": [
            "diagn\u00f3stico",
            "qual rem\u00e9dio",
            "que medicamento",
            "prescri\u00e7\u00e3o",
            "receita m\u00e9dica",
            "devo fazer",
            "garantia de resultado",
            "vai curar",
        ],
        "action": "warn",
        "urgency": "high",
    },
    SafetyCategory.CONDUCT_RISK: {
        "description": "Behavioral/conduct concerns",
        "patterns": [
            # Inappropriate content
            r"\b(sexo|sexual|nude|nudes|safad[oa]|gostosa?)\b",
            r"\b(ass\u00e9dio|assedio|abuso|viol\u00eancia|violencia)\b",
            # Threats/aggression
            r"\b(vou te|te|vou)\b.*\b(matar|processar|denunciar|acabar)\b",
            r"\b(amea\u00e7a|ameaco|amea\u00e7ar|ameacar)\b",
            r"\b(filho da|fdp|desgra\u00e7a|desgraca|idiota|imbecil)\b",
            # Fraud/deception
            r"\b(golpe|fraude|enganar|falsificar)\b",
            r"\b(atestado|laudo)\b.*\b(falso|forjar|comprar)\b",
            # Discrimination
            r"\b(racismo|homofobia|preconceito|discrimina\u00e7\u00e3o)\b",
        ],
        "keywords": [
            "amea\u00e7a",
            "vou processar",
            "vou denunciar",
            "palavr\u00e3o",
            "ofensa",
            "ass\u00e9dio",
            "abuso",
            "atestado falso",
            "fraude",
        ],
        "action": "block",
        "urgency": "high",
    },
}


class SafetyDetector:
    """
    Detects safety concerns in messages.

    Categories:
    - CLINICAL_RISK: Medical emergencies, self-harm, critical symptoms
    - LEGAL_RISK: Diagnosis requests, medication advice, false promises
    - CONDUCT_RISK: Inappropriate content, threats, fraud
    """

    def __init__(
        self,
        safety_threshold: float = 0.5,
        custom_patterns: Optional[dict[SafetyCategory, SafetyPatternOverrides]] = None,
    ):
        """
        Initialize the safety detector.

        Args:
            safety_threshold: Minimum confidence to trigger safety concern
            custom_patterns: Optional custom pattern definitions
        """
        self.threshold = safety_threshold
        self.patterns = SAFETY_PATTERNS.copy()
        if custom_patterns:
            for category, patterns in custom_patterns.items():
                if category in self.patterns:
                    self.patterns[category]["patterns"].extend(
                        patterns.get("patterns", [])
                    )
                    self.patterns[category]["keywords"].extend(
                        patterns.get("keywords", [])
                    )

        # Compile patterns
        self._compiled: dict[SafetyCategory, list[re.Pattern[str]]] = {}
        for category, definition in self.patterns.items():
            self._compiled[category] = [
                re.compile(p, re.IGNORECASE | re.UNICODE)
                for p in definition.get("patterns", [])
            ]

    def detect(self, message: str) -> SafetyResult:
        """
        Detect safety concerns in a message.

        Args:
            message: User message to analyze

        Returns:
            SafetyResult with safety assessment
        """
        message = message.strip().lower()

        if not message:
            return SafetyResult(is_safe=True)

        # Check each category
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

        # Determine if it's a safety concern
        is_safe = highest_confidence < self.threshold

        if is_safe:
            return SafetyResult(is_safe=True)

        # Get action for the category
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
        """
        Check message against a safety category.

        Returns:
            Tuple of (confidence, matched_triggers)
        """
        triggers: list[str] = []
        score = 0.0

        # Pattern matching
        patterns = self._compiled.get(category, [])
        for pattern in patterns:
            match = pattern.search(message)
            if match:
                triggers.append(match.group())
                score += 0.4

        # Keyword matching
        definition = self.patterns.get(category)
        keywords = definition["keywords"] if definition is not None else []
        for keyword in keywords:
            if keyword.lower() in message:
                if keyword not in triggers:
                    triggers.append(keyword)
                score += 0.2

        # Cap at 1.0
        score = min(score, 1.0)

        # Apply category-specific adjustments
        if category == SafetyCategory.CLINICAL_RISK and triggers:
            # Clinical risks should be treated more seriously
            score = max(score, 0.6)

        return score, triggers

    def is_emergency(self, message: str) -> bool:
        """Quick check if message contains emergency indicators."""
        result = self.detect(message)
        return (
            not result.is_safe
            and result.category == SafetyCategory.CLINICAL_RISK
            and result.confidence >= 0.6
        )

    def get_recommended_response(self, result: SafetyResult) -> str:
        """Get a recommended response template for a safety result."""
        if result.is_safe:
            return ""

        templates = {
            SafetyCategory.CLINICAL_RISK: (
                "Identificamos uma situa\u00e7\u00e3o que pode requerer aten\u00e7\u00e3o m\u00e9dica urgente. "
                "Por favor, procure atendimento m\u00e9dico imediatamente ou ligue para o SAMU (192)."
            ),
            SafetyCategory.LEGAL_RISK: (
                "N\u00e3o posso fornecer diagn\u00f3sticos m\u00e9dicos, prescri\u00e7\u00f5es ou garantias de resultados. "
                "Para essas quest\u00f5es, por favor consulte um profissional de sa\u00fade qualificado."
            ),
            SafetyCategory.CONDUCT_RISK: (
                "Este tipo de conte\u00fado n\u00e3o \u00e9 apropriado para nossa conversa. "
                "Por favor, mantenha a comunica\u00e7\u00e3o respeitosa."
            ),
        }

        return templates.get(result.category, "")


# ============================================
# Singleton Instance
# ============================================

_detector: Optional[SafetyDetector] = None


def get_safety_detector(threshold: float = 0.5) -> SafetyDetector:
    """Get or create the global SafetyDetector instance."""
    global _detector
    if _detector is None:
        _detector = SafetyDetector(safety_threshold=threshold)
    return _detector
