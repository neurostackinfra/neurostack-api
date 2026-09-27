"""
NeuroStack - SRM - RAG Decider
Determines when knowledge retrieval (RAG) is needed to answer a query.
"""

import re
from typing import Optional

from neurostack_core.srm.models import FunnelStage, IntentCategory, RagResult

# ============================================
# RAG Trigger Patterns
# ============================================

RAG_TRIGGER_PATTERNS = {
    # Questions about services/procedures
    "services": [
        r"\b(quais?|que)\b.*\b(servi\u00e7os?|procedimentos?|tratamentos?)\b",
        r"\b(fazem|oferecem|realizam)\b.*\b(que|quais?)\b",
        r"\b(tipos?)\b.*\b(consulta|atendimento|procedimento)\b",
    ],
    # Questions about professionals
    "professionals": [
        r"\b(quem|qual)\b.*\b(m\u00e9dico|doutor|profissional|especialista)\b",
        r"\b(especialidade|forma\u00e7\u00e3o|experi\u00eancia)\b.*\b(m\u00e9dico|doutor)\b",
        r"\b(equipe|profissionais)\b",
    ],
    # Questions about location/address
    "location": [
        r"\b(onde|qual)\b.*\b(fica|endere\u00e7o|localiza\u00e7\u00e3o)\b",
        r"\b(como)\b.*\b(chegar|chego|ir)\b",
        r"\b(estacionamento|acesso|entrada)\b",
    ],
    # Questions about hours/availability
    "hours": [
        r"\b(hor\u00e1rio|hora)\b.*\b(funciona|atende|abre|fecha)\b",
        r"\b(funcionamento|expediente)\b",
        r"\b(s\u00e1bado|domingo|feriado)\b.*\b(abre|funciona|atende)\b",
    ],
    # Questions about pricing
    "pricing": [
        r"\b(quanto|qual)\b.*\b(custa|valor|pre\u00e7o)\b",
        r"\b(valores?|pre\u00e7os?|custo)\b.*\b(consulta|procedimento)\b",
        r"\b(tabela|lista)\b.*\b(pre\u00e7o|valor)\b",
        r"\b(parcelamento|pagamento)\b",
    ],
    # Questions about insurance/plans
    "insurance": [
        r"\b(aceita|atende|trabalha)\b.*\b(conv\u00eanio|plano|seguro)\b",
        r"\b(conv\u00eanios?|planos?)\b.*\b(aceitos?|atendidos?)\b",
        r"\b(particular|sem conv\u00eanio)\b",
    ],
    # Questions about preparation
    "preparation": [
        r"\b(preparo|prepara\u00e7\u00e3o|preparar)\b.*\b(exame|consulta|procedimento)\b",
        r"\b(o que|que|como)\b.*\b(levar|trazer|fazer antes)\b",
        r"\b(jejum|documentos?|exames? anteriores?)\b",
    ],
    # Questions about policies
    "policies": [
        r"\b(pol\u00edtica|regra|norma)\b.*\b(cancelamento|remarca\u00e7\u00e3o|atraso)\b",
        r"\b(multa|taxa)\b.*\b(cancelamento|falta)\b",
        r"\b(anteced\u00eancia|prazo)\b.*\b(cancelar|remarcar)\b",
    ],
    # General FAQ-style questions
    "faq": [
        r"\b(d\u00favida|pergunta)\b.*\b(sobre|quanto)\b",
        r"^(como|onde|quando|quanto|qual|quais|o que|por que)\b",
    ],
}

# Intent categories that typically need RAG
RAG_INTENT_CATEGORIES = [
    IntentCategory.QUESTION,
    IntentCategory.INFORMATION,
]


class RagDecider:
    """
    Decides when to use RAG (Retrieval Augmented Generation).

    RAG is triggered when:
    - User asks about specific business information (hours, location, prices)
    - User asks about services, procedures, or professionals
    - User asks FAQ-style questions
    - High uncertainty about the topic
    """

    def __init__(
        self,
        custom_patterns: Optional[dict[str, list[str]]] = None,
        confidence_threshold: float = 0.7,
    ):
        """
        Initialize the RAG decider.

        Args:
            custom_patterns: Optional custom pattern definitions
            confidence_threshold: Below this confidence, suggest RAG
        """
        self.patterns = RAG_TRIGGER_PATTERNS.copy()
        if custom_patterns:
            for topic, patterns in custom_patterns.items():
                if topic in self.patterns:
                    self.patterns[topic].extend(patterns)
                else:
                    self.patterns[topic] = patterns

        self.confidence_threshold = confidence_threshold

        # Compile patterns
        self._compiled: dict[str, list[re.Pattern[str]]] = {}
        for topic, patterns in self.patterns.items():
            self._compiled[topic] = [
                re.compile(p, re.IGNORECASE | re.UNICODE) for p in patterns
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
        """
        Decide if RAG is needed for this message.

        Args:
            message: User message
            intent: Detected intent
            intent_category: Intent category
            confidence: Intent confidence
            funnel_stage: Current funnel stage
            flags: Session flags

        Returns:
            RagResult with RAG decision
        """
        message_lower = message.strip().lower()

        # Check if intent category typically needs RAG
        needs_rag_category = intent_category in RAG_INTENT_CATEGORIES

        # Check for pattern matches
        matched_topics: list[str] = []
        for topic, patterns in self._compiled.items():
            for pattern in patterns:
                if pattern.search(message_lower):
                    matched_topics.append(topic)
                    break

        # Determine if RAG is needed
        needs_rag = False
        query_suggestion = None
        reason = None

        if matched_topics:
            needs_rag = True
            primary_topic = matched_topics[0]
            query_suggestion = self._generate_query(message_lower, primary_topic)
            reason = f"Question about {', '.join(matched_topics)}"
        elif needs_rag_category and confidence < self.confidence_threshold:
            # Low confidence question - might benefit from RAG
            needs_rag = True
            query_suggestion = message_lower
            reason = "Low confidence question - RAG may help"
        elif self._is_knowledge_intent(intent):
            needs_rag = True
            query_suggestion = message_lower
            reason = f"Knowledge-seeking intent: {intent}"

        # Don't use RAG for simple confirmations, greetings, etc.
        if intent_category in [
            IntentCategory.GREETING,
            IntentCategory.FAREWELL,
            IntentCategory.CONFIRMATION,
        ]:
            needs_rag = False
            query_suggestion = None
            reason = None

        # Check if RAG was disabled by flags
        if flags.get("rag_disabled", False):
            needs_rag = False
            reason = None

        return RagResult(
            needs_rag=needs_rag,
            query_suggestion=query_suggestion,
            reason=reason,
        )

    def _is_knowledge_intent(self, intent: str) -> bool:
        """Check if intent typically requires knowledge lookup."""
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
        """Generate a refined query for RAG."""
        # For now, use the original message
        # In the future, this could be enhanced with query refinement
        topic_prefixes = {
            "services": "servi\u00e7os e procedimentos",
            "professionals": "profissionais e especialistas",
            "location": "localiza\u00e7\u00e3o e endere\u00e7o",
            "hours": "hor\u00e1rios de funcionamento",
            "pricing": "valores e pre\u00e7os",
            "insurance": "conv\u00eanios aceitos",
            "preparation": "prepara\u00e7\u00e3o para exames",
            "policies": "pol\u00edticas e regras",
            "faq": "",
        }

        prefix = topic_prefixes.get(topic, "")
        if prefix and prefix not in message:
            return f"{prefix}: {message}"
        return message

    def get_matched_topics(self, message: str) -> list[str]:
        """Get all topics that match in a message."""
        message_lower = message.strip().lower()
        matched: list[str] = []

        for topic, patterns in self._compiled.items():
            for pattern in patterns:
                if pattern.search(message_lower):
                    matched.append(topic)
                    break

        return matched


# ============================================
# Singleton Instance
# ============================================

_decider: Optional[RagDecider] = None


def get_rag_decider(confidence_threshold: float = 0.7) -> RagDecider:
    """Get or create the global RagDecider instance."""
    global _decider
    if _decider is None:
        _decider = RagDecider(confidence_threshold=confidence_threshold)
    return _decider
