"""
NeuroStack - Prompt Engine - Models
Data models for prompt generation and validation.
"""

from enum import Enum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class PromptMode(str, Enum):
    """Prompt modes matching flow modes."""

    NORMAL = "normal"
    RAG = "rag"
    SAFETY = "safety"
    HANDOFF = "handoff"


class MessageRole(str, Enum):
    """Role in conversation."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


class PromptMessage(BaseModel):
    """A message in the prompt."""

    role: MessageRole
    content: str
    name: Optional[str] = None

    def to_dict(self) -> dict[str, str]:
        """Convert to OpenAI-compatible format."""
        result = {"role": self.role.value, "content": self.content}
        if self.name:
            result["name"] = self.name
        return result


class PromptVariables(BaseModel):
    """Variables for prompt template rendering."""

    # Session info
    session_id: Optional[UUID] = None
    tenant_id: Optional[UUID] = None
    channel: str = "unknown"
    turn_count: int = 0

    # Cognitive state
    funnel_stage: str = "lead_novo"
    last_intent: Optional[str] = None
    slots: dict[str, Any] = Field(default_factory=dict)

    # Current message
    user_message: str = ""

    # Knowledge context (RAG)
    knowledge_context: Optional[str] = None
    knowledge_sources: list[str] = Field(default_factory=list)

    # Safety context
    safety_category: Optional[str] = None
    safety_level: int = 0

    # Handoff context
    handoff_reason: Optional[str] = None
    handoff_status: str = "pending"

    # Policies
    constraints: list[str] = Field(default_factory=list)
    safety_reminders: list[str] = Field(default_factory=list)

    # Conversation history summary
    history_summary: Optional[str] = None

    # Jornada do usuário no funil (etapas, intenções, dados coletados) — contexto principal para condução
    user_journey: Optional[str] = None

    # Custom variables
    custom: dict[str, Any] = Field(default_factory=dict)

    # Response language (pt-BR default)
    language: str = "pt-BR"


class PromptConfig(BaseModel):
    """Configuration for prompt generation."""

    mode: PromptMode = PromptMode.NORMAL
    max_tokens: int = 4096
    temperature: float = 0.7
    include_system: bool = True
    include_history: bool = True
    include_knowledge: bool = True
    include_constraints: bool = True
    response_format: Optional[str] = None  # "json" | "text" | None


class GeneratedPrompt(BaseModel):
    """Result of prompt generation."""

    messages: list[PromptMessage]
    mode: PromptMode
    variables_used: list[str] = Field(default_factory=list)
    total_tokens_estimate: int = 0
    generation_time_ms: float = 0.0
    warnings: list[str] = Field(default_factory=list)

    def to_messages_list(self) -> list[dict[str, str]]:
        """Convert to list of message dicts for LLM API."""
        return [m.to_dict() for m in self.messages]

    def get_system_prompt(self) -> Optional[str]:
        """Get the system prompt content."""
        for msg in self.messages:
            if msg.role == MessageRole.SYSTEM:
                return msg.content
        return None


class OutputSchema(BaseModel):
    """Schema for validating LLM output."""

    type: str = "object"
    properties: dict[str, Any] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)

    def validate_output(self, output: dict[str, Any]) -> tuple[bool, list[str]]:
        """
        Validate output against schema.

        Returns:
            Tuple of (is_valid, list of errors)
        """
        errors = []

        # Check required fields
        for field in self.required:
            if field not in output:
                errors.append(f"Missing required field: {field}")

        # Check property types (basic validation)
        for prop, spec in self.properties.items():
            if prop in output:
                expected_type = spec.get("type", "string")
                value = output[prop]

                if expected_type == "string" and not isinstance(value, str):
                    errors.append(
                        f"Field '{prop}' should be string, got {type(value).__name__}"
                    )
                elif expected_type == "number" and not isinstance(value, (int, float)):
                    errors.append(
                        f"Field '{prop}' should be number, got {type(value).__name__}"
                    )
                elif expected_type == "boolean" and not isinstance(value, bool):
                    errors.append(
                        f"Field '{prop}' should be boolean, got {type(value).__name__}"
                    )
                elif expected_type == "array" and not isinstance(value, list):
                    errors.append(
                        f"Field '{prop}' should be array, got {type(value).__name__}"
                    )
                elif expected_type == "object" and not isinstance(value, dict):
                    errors.append(
                        f"Field '{prop}' should be object, got {type(value).__name__}"
                    )

        return len(errors) == 0, errors


class ValidationResult(BaseModel):
    """Result of output validation."""

    is_valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    sanitized_output: Optional[dict[str, Any]] = None


# ============================================
# Standard Output Schemas
# ============================================

RESPONSE_SCHEMA = OutputSchema(
    type="object",
    properties={
        "response": {"type": "string", "description": "The assistant's response"},
        "intent_detected": {"type": "string", "description": "Detected user intent"},
        "confidence": {"type": "number", "description": "Confidence level 0-1"},
        "suggested_actions": {
            "type": "array",
            "description": "Suggested follow-up actions",
        },
    },
    required=["response"],
)

SAFETY_RESPONSE_SCHEMA = OutputSchema(
    type="object",
    properties={
        "response": {"type": "string", "description": "Safety-focused response"},
        "safety_acknowledged": {
            "type": "boolean",
            "description": "Whether safety concern was addressed",
        },
        "escalation_needed": {
            "type": "boolean",
            "description": "Whether human escalation is needed",
        },
        "resources_provided": {
            "type": "array",
            "description": "Help resources provided",
        },
    },
    required=["response", "safety_acknowledged"],
)

HANDOFF_RESPONSE_SCHEMA = OutputSchema(
    type="object",
    properties={
        "response": {"type": "string", "description": "Handoff transition response"},
        "handoff_summary": {"type": "string", "description": "Summary for human agent"},
        "priority": {"type": "string", "description": "Handoff priority level"},
        "context_preserved": {
            "type": "boolean",
            "description": "Whether context was preserved",
        },
    },
    required=["response"],
)
