"""
NeuroStack - Handoff Summarizer
Generates structured summaries of conversations for human handoff.
"""

import re
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from src.handoff.models import (
    ConversationMessage,
    ConversationSummary,
    HandoffReason,
    HandoffUrgency,
    KeyInformation,
)


# ============================================
# Extraction Patterns
# ============================================

# Name patterns (Portuguese)
NAME_PATTERNS = [
    r"(?:meu nome é|me chamo|sou o|sou a|pode me chamar de)\s+([A-ZÀ-Ú][a-zà-ú]+(?:\s+[A-ZÀ-Ú][a-zà-ú]+)?)",
    r"^([A-ZÀ-Ú][a-zà-ú]+)\s+aqui",
]

# Contact patterns
PHONE_PATTERN = r"(?:\+?55\s?)?(?:\(?[0-9]{2}\)?[\s.-]?)?[0-9]{4,5}[\s.-]?[0-9]{4}"
EMAIL_PATTERN = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"

# Symptom keywords (healthcare)
SYMPTOM_KEYWORDS = [
    "dor", "dores", "febre", "tosse", "enjoo", "náusea", "vômito",
    "tontura", "cansaço", "fadiga", "inchaço", "vermelhidão",
    "coceira", "ardência", "formigamento", "sangramento", "falta de ar",
    "dificuldade para respirar", "palpitação", "insônia", "ansiedade",
    "dor de cabeça", "enxaqueca", "mal estar",
]

# Appointment preferences
APPOINTMENT_KEYWORDS = [
    "manhã", "tarde", "noite", "segunda", "terça", "quarta", "quinta",
    "sexta", "sábado", "semana que vem", "amanhã", "hoje", "urgente",
    "próxima semana", "qualquer horário", "horário livre",
]


class ConversationSummarizer:
    """
    Generates structured summaries of conversations for human handoff.
    
    Features:
    - Extracts key information (names, contacts, topics)
    - Identifies symptoms and healthcare-specific info
    - Creates human-readable summaries
    - Preserves recent messages for context
    """

    def __init__(
        self,
        max_recent_messages: int = 5,
        max_summary_length: int = 500,
    ):
        """
        Initialize the summarizer.
        
        Args:
            max_recent_messages: Maximum recent messages to include
            max_summary_length: Maximum length of summary text
        """
        self.max_recent_messages = max_recent_messages
        self.max_summary_length = max_summary_length
        
        # Compile patterns
        self._name_patterns = [
            re.compile(p, re.IGNORECASE | re.UNICODE)
            for p in NAME_PATTERNS
        ]
        self._phone_pattern = re.compile(PHONE_PATTERN)
        self._email_pattern = re.compile(EMAIL_PATTERN)

    def summarize(
        self,
        session_id: UUID,
        tenant_id: UUID,
        messages: list[dict[str, Any]],
        reason: HandoffReason,
        urgency: HandoffUrgency = HandoffUrgency.NORMAL,
        funnel_stage: str = "unknown",
        intents: Optional[list[str]] = None,
        slots: Optional[dict[str, Any]] = None,
        safety_flags: Optional[list[str]] = None,
        session_start: Optional[datetime] = None,
        sector: Optional[Any] = None,
        sla: Optional[Any] = None,
    ) -> ConversationSummary:
        """
        Generate a structured summary of the conversation.
        
        Args:
            session_id: Session identifier
            tenant_id: Tenant identifier
            messages: List of conversation messages
            reason: Reason for handoff
            urgency: Handoff urgency level
            funnel_stage: Current funnel stage
            intents: List of detected intents
            slots: Extracted slot values
            safety_flags: Any safety flags triggered
            session_start: When the session started
            
        Returns:
            ConversationSummary with all extracted information
        """
        # Extract key information
        key_info = self._extract_key_info(messages, slots)
        
        # Convert messages to structured format
        conv_messages = self._convert_messages(messages)
        
        # Calculate duration
        duration = None
        if session_start:
            now = datetime.now(timezone.utc)
            duration = int((now - session_start).total_seconds())
        
        # Generate summary text
        summary_text = self._generate_summary_text(
            messages=conv_messages,
            key_info=key_info,
            reason=reason,
            intents=intents or [],
        )
        
        # Get recent messages
        recent = conv_messages[-self.max_recent_messages:] if conv_messages else []
        
        return ConversationSummary(
            session_id=session_id,
            tenant_id=tenant_id,
            summary_text=summary_text,
            total_messages=len(messages),
            turn_count=len([m for m in messages if m.get("role") == "user"]),
            duration_seconds=duration,
            funnel_stage=funnel_stage,
            intents_detected=intents or [],
            key_info=key_info,
            handoff_reason=reason,
            urgency=urgency,
            sector=sector,
            sla=sla,
            safety_flags=safety_flags or [],
            recent_messages=recent,
        )

    def _extract_key_info(
        self,
        messages: list[dict[str, Any]],
        slots: Optional[dict[str, Any]] = None,
    ) -> KeyInformation:
        """Extract key information from messages."""
        slots = slots or {}
        
        # Combine all user messages
        user_text = " ".join([
            m.get("content", "") 
            for m in messages 
            if m.get("role") == "user"
        ])
        
        # Extract name
        user_name = slots.get("user_name")
        if not user_name:
            user_name = self._extract_name(user_text)
        
        # Extract contact info
        contact_info = None
        phone = self._phone_pattern.search(user_text)
        email = self._email_pattern.search(user_text)
        if phone:
            contact_info = phone.group()
        elif email:
            contact_info = email.group()
        
        # Extract symptoms
        symptoms = self._extract_symptoms(user_text)
        
        # Extract appointment preferences
        appointment_prefs = self._extract_appointment_preferences(user_text)
        
        # Identify main topic from first messages
        main_topic = self._identify_main_topic(messages)
        
        # Get specific request from last user message
        specific_request = None
        user_messages = [m for m in messages if m.get("role") == "user"]
        if user_messages:
            specific_request = user_messages[-1].get("content", "")[:200]
        
        return KeyInformation(
            user_name=user_name,
            contact_info=contact_info,
            main_topic=main_topic,
            specific_request=specific_request,
            symptoms_mentioned=symptoms,
            appointment_preferences=appointment_prefs,
            extracted_slots=slots,
        )

    def _extract_name(self, text: str) -> Optional[str]:
        """Extract user name from text."""
        for pattern in self._name_patterns:
            match = pattern.search(text)
            if match:
                return match.group(1).strip()
        return None

    def _extract_symptoms(self, text: str) -> list[str]:
        """Extract mentioned symptoms from text."""
        text_lower = text.lower()
        found = []
        for symptom in SYMPTOM_KEYWORDS:
            if symptom in text_lower:
                found.append(symptom)
        return found[:5]  # Limit to 5

    def _extract_appointment_preferences(self, text: str) -> Optional[str]:
        """Extract appointment preferences from text."""
        text_lower = text.lower()
        found = []
        for pref in APPOINTMENT_KEYWORDS:
            if pref in text_lower:
                found.append(pref)
        return ", ".join(found) if found else None

    def _identify_main_topic(self, messages: list[dict[str, Any]]) -> Optional[str]:
        """Identify the main topic from initial messages."""
        # Look at first 3 user messages
        user_messages = [m for m in messages if m.get("role") == "user"][:3]
        if not user_messages:
            return None
        
        # Simple topic detection based on keywords
        combined = " ".join([m.get("content", "") for m in user_messages]).lower()
        
        topics = {
            "agendamento": ["agendar", "marcar", "consulta", "horário", "agenda"],
            "informação": ["informação", "saber", "dúvida", "pergunta", "como"],
            "cancelamento": ["cancelar", "desmarcar", "remarcar"],
            "pagamento": ["pagar", "pagamento", "valor", "preço", "custo"],
            "reclamação": ["reclamar", "problema", "ruim", "péssimo", "insatisfeito"],
            "emergência": ["urgente", "emergência", "socorro", "ajuda"],
        }
        
        for topic, keywords in topics.items():
            if any(kw in combined for kw in keywords):
                return topic
        
        return "atendimento geral"

    def _convert_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> list[ConversationMessage]:
        """Convert raw messages to ConversationMessage objects."""
        result = []
        for msg in messages:
            timestamp = msg.get("timestamp")
            if isinstance(timestamp, str):
                try:
                    timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                except ValueError:
                    timestamp = datetime.now(timezone.utc)
            elif not isinstance(timestamp, datetime):
                timestamp = datetime.now(timezone.utc)
            
            result.append(ConversationMessage(
                role=msg.get("role", "user"),
                content=msg.get("content", ""),
                timestamp=timestamp,
                intent=msg.get("intent"),
            ))
        return result

    def _generate_summary_text(
        self,
        messages: list[ConversationMessage],
        key_info: KeyInformation,
        reason: HandoffReason,
        intents: list[str],
    ) -> str:
        """Generate human-readable summary text."""
        parts = []
        
        # Opening with user info if available
        if key_info.user_name:
            parts.append(f"Usuário: {key_info.user_name}")
        
        # Main topic
        if key_info.main_topic:
            parts.append(f"Assunto principal: {key_info.main_topic}")
        
        # Handoff reason
        reason_text = self._get_reason_text(reason)
        parts.append(f"Motivo do encaminhamento: {reason_text}")
        
        # Specific request
        if key_info.specific_request:
            truncated = key_info.specific_request[:150]
            if len(key_info.specific_request) > 150:
                truncated += "..."
            parts.append(f"Última solicitação: \"{truncated}\"")
        
        # Symptoms if any
        if key_info.symptoms_mentioned:
            parts.append(f"Sintomas mencionados: {', '.join(key_info.symptoms_mentioned)}")
        
        # Contact info
        if key_info.contact_info:
            parts.append(f"Contato: {key_info.contact_info}")
        
        # Appointment preferences
        if key_info.appointment_preferences:
            parts.append(f"Preferência de horário: {key_info.appointment_preferences}")
        
        # Conversation stats
        parts.append(f"Total de mensagens: {len(messages)}")
        
        # Combine and truncate
        summary = "\n".join(parts)
        if len(summary) > self.max_summary_length:
            summary = summary[:self.max_summary_length - 3] + "..."
        
        return summary

    def _get_reason_text(self, reason: HandoffReason) -> str:
        """Get human-readable text for handoff reason."""
        texts = {
            HandoffReason.USER_REQUEST: "Usuário solicitou atendimento humano",
            HandoffReason.SAFETY_CONCERN: "Preocupação de segurança detectada",
            HandoffReason.MAX_TURNS_EXCEEDED: "Limite de interações atingido",
            HandoffReason.LOW_CONFIDENCE: "Baixa confiança nas respostas",
            HandoffReason.COMPLEX_QUERY: "Questão complexa requer especialista",
            HandoffReason.ESCALATION: "Escalação/reclamação do usuário",
            HandoffReason.SCHEDULING: "Paciente quer agendar consulta",
        }
        return texts.get(reason, "Motivo não especificado")

    def quick_summary(
        self,
        messages: list[dict[str, Any]],
        reason: HandoffReason,
        max_length: int = 200,
    ) -> str:
        """
        Generate a quick one-line summary.
        
        Args:
            messages: List of conversation messages
            reason: Reason for handoff
            max_length: Maximum length of summary
            
        Returns:
            Brief summary string
        """
        if not messages:
            return self._get_reason_text(reason)
        
        # Get last user message
        user_messages = [m for m in messages if m.get("role") == "user"]
        if not user_messages:
            return self._get_reason_text(reason)
        
        last_message = user_messages[-1].get("content", "")[:100]
        reason_text = self._get_reason_text(reason)
        
        summary = f"{reason_text}. Última mensagem: \"{last_message}\""
        if len(summary) > max_length:
            summary = summary[:max_length - 3] + "..."
        
        return summary


# ============================================
# Singleton Instance
# ============================================

_summarizer: Optional[ConversationSummarizer] = None


def get_summarizer(
    max_recent_messages: int = 5,
    max_summary_length: int = 500,
) -> ConversationSummarizer:
    """Get or create the global ConversationSummarizer instance."""
    global _summarizer
    if _summarizer is None:
        _summarizer = ConversationSummarizer(
            max_recent_messages=max_recent_messages,
            max_summary_length=max_summary_length,
        )
    return _summarizer

