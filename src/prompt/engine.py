"""
NeuroStack - Prompt Engine
Main engine for prompt generation and management.
"""

import logging
import time
from typing import Any, Optional

from prometheus_client import Counter, Histogram

from src.prompt.models import (
    GeneratedPrompt,
    OutputSchema,
    PromptConfig,
    PromptMode,
    PromptVariables,
    ValidationResult,
)
from src.prompt.renderer import PromptRenderer, VariableResolver, get_prompt_renderer
from src.prompt.templates.base import BaseTemplate
from src.prompt.validator import (
    OutputValidator,
    ResponseExtractor,
    get_output_validator,
)

logger = logging.getLogger(__name__)


# ============================================
# Prometheus Metrics
# ============================================

PROMPT_GENERATIONS_TOTAL = Counter(
    "neurostack_prompt_generations_total",
    "Total prompt generations",
    ["mode"],
)

PROMPT_GENERATION_DURATION = Histogram(
    "neurostack_prompt_generation_seconds",
    "Prompt generation time",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05),
)

PROMPT_TOKENS_TOTAL = Counter(
    "neurostack_prompt_tokens_total",
    "Total tokens in generated prompts",
    ["mode"],
)

OUTPUT_VALIDATIONS_TOTAL = Counter(
    "neurostack_output_validations_total",
    "Total output validations",
    ["mode", "valid"],
)


class PromptEngine:
    """
    Main Prompt Engine.

    Orchestrates prompt generation by:
    1. Selecting appropriate templates based on mode
    2. Rendering templates with variables
    3. Managing conversation history
    4. Validating LLM outputs

    This is the central component for all prompt-related operations.
    """

    def __init__(
        self,
        renderer: Optional[PromptRenderer] = None,
        validator: Optional[OutputValidator] = None,
    ):
        """
        Initialize the Prompt Engine.

        Args:
            renderer: Prompt renderer instance
            validator: Output validator instance
        """
        self._renderer = renderer or get_prompt_renderer()
        self._validator = validator or get_output_validator()

    async def generate(
        self,
        mode: PromptMode,
        variables: PromptVariables,
        history: Optional[list[dict[str, str]]] = None,
        config: Optional[PromptConfig] = None,
    ) -> GeneratedPrompt:
        """
        Generate a prompt for the given mode and context.

        Args:
            mode: Prompt mode (NORMAL, RAG, SAFETY, HANDOFF)
            variables: Variables for template rendering
            history: Optional conversation history
            config: Optional prompt configuration

        Returns:
            GeneratedPrompt with messages ready for LLM
        """
        start_time = time.perf_counter()

        # Create config if not provided
        prompt_config = config or PromptConfig(mode=mode)
        prompt_config.mode = mode

        # Render prompt
        prompt = self._renderer.render(
            variables=variables,
            config=prompt_config,
            history_messages=history,
        )

        # Record metrics
        self._record_generation_metrics(mode, prompt, start_time)

        logger.debug(
            f"Generated prompt: mode={mode.value}, "
            f"messages={len(prompt.messages)}, "
            f"tokens≈{prompt.total_tokens_estimate}"
        )

        return prompt

    async def generate_simple(
        self,
        message: str,
        mode: PromptMode = PromptMode.NORMAL,
        **kwargs: Any,
    ) -> GeneratedPrompt:
        """
        Generate a simple prompt with minimal configuration.

        Args:
            message: User message
            mode: Prompt mode
            **kwargs: Additional variables

        Returns:
            GeneratedPrompt
        """
        variables = PromptVariables(user_message=message, **kwargs)
        return await self.generate(mode, variables)

    async def generate_from_context(
        self,
        context_window: Any,
        mode: PromptMode,
        user_message: str,
    ) -> GeneratedPrompt:
        """
        Generate prompt from a ContextWindow.

        Args:
            context_window: ContextWindow from CTX
            mode: Prompt mode
            user_message: Current user message

        Returns:
            GeneratedPrompt
        """
        # Convert context to variables
        variables = VariableResolver.from_context_window(context_window)
        variables.user_message = user_message

        # Get history from context
        history = context_window.to_messages()

        return await self.generate(mode, variables, history)

    def validate_output(
        self,
        output: str,
        mode: PromptMode,
        schema: Optional[OutputSchema] = None,
    ) -> ValidationResult:
        """
        Validate LLM output against schema.

        Args:
            output: Raw LLM output
            mode: Current mode (for schema selection)
            schema: Optional custom schema

        Returns:
            ValidationResult
        """
        result = self._validator.validate(output, mode, schema)

        # Record metrics
        OUTPUT_VALIDATIONS_TOTAL.labels(
            mode=mode.value,
            valid=str(result.is_valid).lower(),
        ).inc()

        return result

    def extract_response(self, validated_output: ValidationResult) -> str:
        """
        Extract the response text from validated output.

        Args:
            validated_output: ValidationResult

        Returns:
            Response text
        """
        if validated_output.sanitized_output:
            return ResponseExtractor.extract_response_text(
                validated_output.sanitized_output
            )
        return ""

    def get_template(self, mode: PromptMode) -> BaseTemplate:
        """Get the template for a mode."""
        return self._renderer.get_template(mode)

    def register_template(self, mode: PromptMode, template: BaseTemplate) -> None:
        """Register a custom template."""
        self._renderer.register_template(mode, template)

    def register_schema(self, mode: PromptMode, schema: OutputSchema) -> None:
        """Register a custom output schema."""
        self._validator.register_schema(mode, schema)

    def _record_generation_metrics(
        self,
        mode: PromptMode,
        prompt: GeneratedPrompt,
        start_time: float,
    ) -> None:
        """Record Prometheus metrics."""
        duration = time.perf_counter() - start_time

        PROMPT_GENERATIONS_TOTAL.labels(mode=mode.value).inc()
        PROMPT_GENERATION_DURATION.observe(duration)
        PROMPT_TOKENS_TOTAL.labels(mode=mode.value).inc(prompt.total_tokens_estimate)


class PromptBuilder:
    """
    Fluent builder for creating prompts.

    Usage:
        prompt = (PromptBuilder()
            .mode(PromptMode.NORMAL)
            .message("Hello")
            .with_context(funnel_stage="consideration")
            .with_constraints(["Be polite"])
            .build())
    """

    def __init__(self) -> None:
        """Initialize builder."""
        self._mode = PromptMode.NORMAL
        self._user_message = ""
        self._variables = PromptVariables()
        self._history: list[dict[str, str]] = []
        self._config = PromptConfig()

    def mode(self, mode: PromptMode) -> "PromptBuilder":
        """Set prompt mode."""
        self._mode = mode
        self._config.mode = mode
        return self

    def message(self, message: str) -> "PromptBuilder":
        """Set user message."""
        self._user_message = message
        self._variables.user_message = message
        return self

    def with_context(self, **kwargs: Any) -> "PromptBuilder":
        """Add context variables."""
        for key, value in kwargs.items():
            if hasattr(self._variables, key):
                setattr(self._variables, key, value)
            else:
                self._variables.custom[key] = value
        return self

    def with_constraints(self, constraints: list[str]) -> "PromptBuilder":
        """Add constraints."""
        self._variables.constraints.extend(constraints)
        return self

    def with_safety(
        self,
        category: str,
        level: int = 1,
        reminders: Optional[list[str]] = None,
    ) -> "PromptBuilder":
        """Add safety context."""
        self._variables.safety_category = category
        self._variables.safety_level = level
        if reminders:
            self._variables.safety_reminders.extend(reminders)
        return self

    def with_knowledge(
        self,
        context: str,
        sources: Optional[list[str]] = None,
    ) -> "PromptBuilder":
        """Add knowledge context."""
        self._variables.knowledge_context = context
        if sources:
            self._variables.knowledge_sources.extend(sources)
        return self

    def with_history(self, history: list[dict[str, str]]) -> "PromptBuilder":
        """Add conversation history."""
        self._history = history
        return self

    def with_config(self, **kwargs: Any) -> "PromptBuilder":
        """Set config options."""
        for key, value in kwargs.items():
            if hasattr(self._config, key):
                setattr(self._config, key, value)
        return self

    async def build(self) -> GeneratedPrompt:
        """Build the prompt."""
        engine = get_prompt_engine()
        return await engine.generate(
            mode=self._mode,
            variables=self._variables,
            history=self._history,
            config=self._config,
        )

    def build_sync(self) -> GeneratedPrompt:
        """Build prompt synchronously (for non-async contexts)."""
        renderer = get_prompt_renderer()
        return renderer.render(
            variables=self._variables,
            config=self._config,
            history_messages=self._history,
        )


# ============================================
# Singleton Instance
# ============================================

_engine: Optional[PromptEngine] = None


def get_prompt_engine() -> PromptEngine:
    """Get or create the global PromptEngine instance."""
    global _engine
    if _engine is None:
        _engine = PromptEngine()
    return _engine


async def generate_prompt(
    message: str,
    mode: PromptMode = PromptMode.NORMAL,
    history: Optional[list[dict[str, str]]] = None,
    **kwargs: Any,
) -> GeneratedPrompt:
    """
    Convenience function for generating prompts.

    Args:
        message: User message
        mode: Prompt mode
        history: Optional conversation history
        **kwargs: Additional variables

    Returns:
        GeneratedPrompt
    """
    engine = get_prompt_engine()
    variables = PromptVariables(user_message=message, **kwargs)
    return await engine.generate(mode, variables, history)
