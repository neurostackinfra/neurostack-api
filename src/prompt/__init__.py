"""
NeuroStack - Prompt Engine
Generates and manages prompts for LLM interactions.

The Prompt Engine is responsible for:
- Creating system prompts based on conversation mode
- Rendering templates with context variables
- Managing conversation history formatting
- Validating LLM outputs against schemas

Usage:
    from src.prompt import PromptEngine, get_prompt_engine, generate_prompt

    # Using the engine
    engine = get_prompt_engine()
    prompt = await engine.generate(PromptMode.NORMAL, variables)

    # Using the convenience function
    prompt = await generate_prompt(
        message="Qual o horário de funcionamento?",
        mode=PromptMode.NORMAL,
        funnel_stage="lead_novo",
    )

    # Using the builder
    prompt = await (PromptBuilder()
        .mode(PromptMode.SAFETY)
        .message("Preciso de ajuda")
        .with_safety("clinical_risk", level=2)
        .build())
"""

from src.prompt.models import (
    # Enums
    PromptMode,
    MessageRole,
    # Models
    PromptMessage,
    PromptVariables,
    PromptConfig,
    GeneratedPrompt,
    OutputSchema,
    ValidationResult,
    # Standard schemas
    RESPONSE_SCHEMA,
    SAFETY_RESPONSE_SCHEMA,
    HANDOFF_RESPONSE_SCHEMA,
)
from src.prompt.templates import (
    BaseTemplate,
    TemplateSection,
    NormalTemplate,
    SafetyTemplate,
    HandoffTemplate,
)
from src.prompt.renderer import (
    PromptRenderer,
    VariableResolver,
    get_prompt_renderer,
)
from src.prompt.validator import (
    OutputValidator,
    ResponseExtractor,
    get_output_validator,
)
from src.prompt.engine import (
    PromptEngine,
    PromptBuilder,
    get_prompt_engine,
    generate_prompt,
)

__all__ = [
    # Enums
    "PromptMode",
    "MessageRole",
    # Models
    "PromptMessage",
    "PromptVariables",
    "PromptConfig",
    "GeneratedPrompt",
    "OutputSchema",
    "ValidationResult",
    # Schemas
    "RESPONSE_SCHEMA",
    "SAFETY_RESPONSE_SCHEMA",
    "HANDOFF_RESPONSE_SCHEMA",
    # Templates
    "BaseTemplate",
    "TemplateSection",
    "NormalTemplate",
    "SafetyTemplate",
    "HandoffTemplate",
    # Renderer
    "PromptRenderer",
    "VariableResolver",
    "get_prompt_renderer",
    # Validator
    "OutputValidator",
    "ResponseExtractor",
    "get_output_validator",
    # Engine
    "PromptEngine",
    "PromptBuilder",
    "get_prompt_engine",
    "generate_prompt",
]
