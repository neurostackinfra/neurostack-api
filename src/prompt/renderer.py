"""
NeuroStack - Prompt Engine - Renderer
Renders prompt templates with variables.
"""

import logging
import time
from typing import Any, Optional

from src.prompt.models import (
    GeneratedPrompt,
    MessageRole,
    PromptConfig,
    PromptMessage,
    PromptMode,
    PromptVariables,
)
from src.prompt.templates.base import BaseTemplate
from src.prompt.templates.handoff import HandoffTemplate
from src.prompt.templates.normal import NormalTemplate
from src.prompt.templates.safety import SafetyTemplate

logger = logging.getLogger(__name__)


# Characters per token (approximation)
CHARS_PER_TOKEN = 4


class PromptRenderer:
    """
    Renders prompt templates with variables.

    Handles:
    - Variable substitution
    - Message history formatting
    - Token estimation
    - Template selection by mode
    """

    def __init__(self) -> None:
        """Initialize the renderer with templates."""
        self._templates: dict[PromptMode, BaseTemplate] = {
            PromptMode.NORMAL: NormalTemplate(),
            PromptMode.RAG: NormalTemplate(),  # RAG uses Normal with knowledge context
            PromptMode.SAFETY: SafetyTemplate(),
            PromptMode.HANDOFF: HandoffTemplate(),
        }

    def render(
        self,
        variables: PromptVariables,
        config: PromptConfig,
        history_messages: Optional[list[dict[str, str]]] = None,
    ) -> GeneratedPrompt:
        """
        Render a complete prompt.

        Args:
            variables: Variables for rendering
            config: Prompt configuration
            history_messages: Optional conversation history

        Returns:
            GeneratedPrompt with all messages
        """
        start_time = time.perf_counter()
        messages: list[PromptMessage] = []
        warnings: list[str] = []
        variables_used: list[str] = []

        # Get template for mode
        template = self._templates.get(config.mode, self._templates[PromptMode.NORMAL])

        # Build system message
        if config.include_system:
            system_message = template.build_system_message(variables)
            messages.append(system_message)

            # Track used variables
            variables_used.extend(template.get_used_variables(system_message.content))

        # Contexto principal é a jornada do usuário (user_journey no system). Histórico de mensagens é omitido.
        if config.include_history and history_messages and not variables.user_journey:
            for msg in history_messages:
                role = MessageRole(msg.get("role", "user"))
                content = msg.get("content", "")
                messages.append(PromptMessage(role=role, content=content))

        # Add current user message
        if variables.user_message:
            messages.append(
                PromptMessage(
                    role=MessageRole.USER,
                    content=variables.user_message,
                )
            )

        # Estimate tokens
        total_tokens = self._estimate_tokens(messages)

        # Check token limit
        if total_tokens > config.max_tokens:
            warnings.append(
                f"Estimated tokens ({total_tokens}) exceed limit ({config.max_tokens})"
            )
            # Trim history if needed
            messages = self._trim_to_fit(messages, config.max_tokens)
            total_tokens = self._estimate_tokens(messages)

        # Calculate generation time
        generation_time = (time.perf_counter() - start_time) * 1000

        logger.debug(
            f"Rendered prompt: mode={config.mode.value}, "
            f"messages={len(messages)}, tokens≈{total_tokens}"
        )
        # Temporary: log system prompt for debugging greeting issue
        if messages and messages[0].role == MessageRole.SYSTEM:
            sys_content = messages[0].content
            if "Saudação" in sys_content or "saudacao" in sys_content.lower():
                logger.warning(
                    f"[GREETING_DEBUG] System prompt excerpt (last 500 chars): ...{sys_content[-500:]}"
                )

        return GeneratedPrompt(
            messages=messages,
            mode=config.mode,
            variables_used=list(set(variables_used)),
            total_tokens_estimate=total_tokens,
            generation_time_ms=generation_time,
            warnings=warnings,
        )

    def render_simple(
        self,
        user_message: str,
        mode: PromptMode = PromptMode.NORMAL,
        **kwargs: Any,
    ) -> GeneratedPrompt:
        """
        Render a simple prompt with minimal configuration.

        Args:
            user_message: User's message
            mode: Prompt mode
            **kwargs: Additional variables

        Returns:
            GeneratedPrompt
        """
        variables = PromptVariables(
            user_message=user_message,
            **kwargs,
        )

        config = PromptConfig(mode=mode)

        return self.render(variables, config)

    def _estimate_tokens(self, messages: list[PromptMessage]) -> int:
        """Estimate token count for messages."""
        total_chars = sum(len(m.content) + len(m.role.value) + 10 for m in messages)
        return total_chars // CHARS_PER_TOKEN

    def _trim_to_fit(
        self,
        messages: list[PromptMessage],
        max_tokens: int,
    ) -> list[PromptMessage]:
        """
        Trim messages to fit within token limit.

        Preserves system message and most recent messages.
        """
        if not messages:
            return messages

        # Always keep system message and last message
        system_msg = None
        if messages[0].role == MessageRole.SYSTEM:
            system_msg = messages[0]
            messages = messages[1:]

        # Keep removing oldest until within limit
        while (
            len(messages) > 1
            and self._estimate_tokens(
                [system_msg] + messages if system_msg else messages
            )
            > max_tokens
        ):
            messages = messages[1:]

        # Reconstruct
        if system_msg:
            return [system_msg] + messages
        return messages

    def get_template(self, mode: PromptMode) -> BaseTemplate:
        """Get template for a mode."""
        return self._templates.get(mode, self._templates[PromptMode.NORMAL])

    def register_template(self, mode: PromptMode, template: BaseTemplate) -> None:
        """Register a custom template for a mode."""
        self._templates[mode] = template
        logger.info(f"Registered custom template for mode {mode.value}")


class VariableResolver:
    """
    Resolves and validates template variables.
    """

    @staticmethod
    def from_context_window(context: Any) -> PromptVariables:
        """
        Create PromptVariables from a ContextWindow.

        Args:
            context: ContextWindow from CTX

        Returns:
            PromptVariables
        """
        # Jornada do usuário a partir do estado cognitivo (condução pelo funil)
        cog = context.cognitive
        journey_parts = [
            f"**Etapa atual do funil:** {cog.funnel_stage}",
            f"**Última intenção:** {cog.last_intent or '—'}",
        ]
        if cog.intent_history:
            journey_parts.append(
                f"**Intenções já percorridas:** {', '.join(cog.intent_history[-10:])}"
            )
        if cog.slots:
            journey_parts.append(
                "**Dados já coletados:** "
                + ", ".join(f"{k}={v}" for k, v in list(cog.slots.items())[:15])
            )
        user_journey = "\n".join(journey_parts)

        return PromptVariables(
            session_id=context.session_id,
            tenant_id=context.tenant_id,
            funnel_stage=cog.funnel_stage,
            last_intent=cog.last_intent,
            slots=cog.slots,
            constraints=context.constraints,
            safety_reminders=context.safety_reminders,
            history_summary=context.compressed_history,
            user_journey=user_journey,
        )

    @staticmethod
    def from_flow_context(flow_context: Any) -> PromptVariables:
        """
        Create PromptVariables from FlowContext.

        Args:
            flow_context: FlowContext from Flow Manager

        Returns:
            PromptVariables
        """
        return PromptVariables(
            session_id=flow_context.session_id,
            tenant_id=flow_context.tenant_id,
            turn_count=flow_context.turn_count,
            last_intent=flow_context.detected_intent,
            safety_category=flow_context.safety_category,
            handoff_reason=flow_context.handoff_reason,
            user_message=flow_context.message,
        )

    @staticmethod
    def merge(
        base: PromptVariables,
        override: PromptVariables,
    ) -> PromptVariables:
        """
        Merge two PromptVariables, with override taking precedence.

        Args:
            base: Base variables
            override: Override variables

        Returns:
            Merged PromptVariables
        """
        base_dict = base.model_dump()
        override_dict = override.model_dump(exclude_unset=True)

        # Merge, preferring override values
        merged = {**base_dict, **override_dict}

        # Special handling for dicts (merge instead of replace)
        for key in ["slots", "custom"]:
            if key in base_dict and key in override_dict:
                merged[key] = {**base_dict[key], **override_dict[key]}

        # Special handling for lists (extend instead of replace)
        for key in ["constraints", "safety_reminders", "knowledge_sources"]:
            if key in base_dict and key in override_dict:
                merged[key] = list(set(base_dict[key] + override_dict[key]))

        return PromptVariables(**merged)


# ============================================
# Singleton Instance
# ============================================

_renderer: Optional[PromptRenderer] = None


def get_prompt_renderer() -> PromptRenderer:
    """Get or create the global PromptRenderer instance."""
    global _renderer
    if _renderer is None:
        _renderer = PromptRenderer()
    return _renderer
