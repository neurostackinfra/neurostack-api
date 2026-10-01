"""
NeuroStack - Prompt Engine - Safety Template
Template for safety-critical conversations.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import cast

from neurostack_core.presets import get_preset
from neurostack_core.prompt.models import PromptMode, PromptVariables
from neurostack_core.prompt.templates.base import BaseTemplate, TemplateSection

_PROMPT_PRESET = cast(Mapping[str, object], get_preset().prompt)

SAFETY_SYSTEM_PROMPT = cast(str, _PROMPT_PRESET["safety_system_prompt"])
SAFETY_CATEGORY_INSTRUCTIONS = cast(
    Mapping[str, str], _PROMPT_PRESET["safety_category_instructions"]
)

_IMPERATIVE_LITERALS = cast(Mapping[str, object], _PROMPT_PRESET["imperative_literals"])
_SAFETY_IMPERATIVES = cast(
    Mapping[str, tuple[str, ...]],
    _IMPERATIVE_LITERALS["prompt.templates.safety"],
)
_RESIDUAL_GROUPS = cast(
    Mapping[str, tuple[str, ...]],
    _IMPERATIVE_LITERALS["prompt.templates.safety.residual"],
)
_RESIDUAL_LITERALS: Mapping[str, str] = MappingProxyType(
    {key: values[0] for key, values in _RESIDUAL_GROUPS.items()}
)
_CONTEXT_LITERALS = _SAFETY_IMPERATIVES["SafetyTemplate.get_context_instructions"]
_CONSTRAINT_LITERALS = _SAFETY_IMPERATIVES["SafetyTemplate.get_constraints"]


class SafetyTemplate(BaseTemplate):
    """
    Template for SAFETY mode conversations.

    Handles safety-critical situations with appropriate
    protocols and resources.
    """

    def __init__(self) -> None:
        super().__init__(PromptMode.SAFETY)

    def get_system_prompt(self, variables: PromptVariables) -> str:
        """Get the safety system prompt."""
        return self.render_template(SAFETY_SYSTEM_PROMPT, variables)

    def get_context_instructions(self, variables: PromptVariables) -> str:
        """Get safety-specific instructions based on category."""
        parts = []

        # Category-specific instructions
        category = variables.safety_category or "clinical_risk"
        if category in SAFETY_CATEGORY_INSTRUCTIONS:
            parts.append(SAFETY_CATEGORY_INSTRUCTIONS[category])

        # Resources section
        resources_section = TemplateSection(_RESIDUAL_LITERALS["resources_title"])
        resources_section.add_list(
            [
                _CONTEXT_LITERALS[0],
                _CONTEXT_LITERALS[1],
                _CONTEXT_LITERALS[2],
                _CONTEXT_LITERALS[3],
                _CONTEXT_LITERALS[4],
                _CONTEXT_LITERALS[5],
            ]
        )
        parts.append(resources_section.build())

        # Session context if available
        if variables.history_summary:
            context_section = TemplateSection(_RESIDUAL_LITERALS["history_title"])
            context_section.add(variables.history_summary)
            parts.append(context_section.build())

        return "\n".join(parts)

    def get_constraints(self, variables: PromptVariables) -> str:
        """Get safety constraints (stricter than normal)."""
        # Always include safety constraints
        safety_constraints = [
            _CONSTRAINT_LITERALS[0],
            _RESIDUAL_LITERALS["no_medication_dosages"],
            _CONSTRAINT_LITERALS[1],
            _CONSTRAINT_LITERALS[2],
            _CONSTRAINT_LITERALS[3],
        ]

        # Add user-defined constraints
        all_constraints = safety_constraints + list(variables.constraints)

        constraints_text = "\n".join(f"- {c}" for c in all_constraints)
        return f"{_CONSTRAINT_LITERALS[4]}{constraints_text}\n"
