"""
NeuroStack - Prompt Engine - Handoff Template
Template for human handoff transitions.
"""

from collections.abc import Mapping
from types import MappingProxyType
from typing import cast

from neurostack_core.presets import get_preset
from neurostack_core.prompt.models import PromptMessage, PromptMode, PromptVariables
from neurostack_core.prompt.templates.base import BaseTemplate, TemplateSection

_PROMPT_PRESET = cast(Mapping[str, object], get_preset().prompt)

HANDOFF_SYSTEM_PROMPT = cast(str, _PROMPT_PRESET["handoff_system_prompt"])
HANDOFF_REASON_INSTRUCTIONS = cast(
    Mapping[str, str], _PROMPT_PRESET["handoff_reason_instructions"]
)

_IMPERATIVE_LITERALS = cast(Mapping[str, object], _PROMPT_PRESET["imperative_literals"])
_HANDOFF_IMPERATIVES = cast(
    Mapping[str, tuple[str, ...]],
    _IMPERATIVE_LITERALS["prompt.templates.handoff"],
)
_RESIDUAL_GROUPS = cast(
    Mapping[str, tuple[str, ...]],
    _IMPERATIVE_LITERALS["prompt.templates.handoff.residual"],
)
_RESIDUAL_LITERALS: Mapping[str, str] = MappingProxyType(
    {key: values[0] for key, values in _RESIDUAL_GROUPS.items()}
)
_CONTEXT_LITERALS = _HANDOFF_IMPERATIVES["HandoffTemplate.get_context_instructions"]
_SUMMARY_LITERALS = _HANDOFF_IMPERATIVES["HandoffTemplate.generate_handoff_summary"]


class HandoffTemplate(BaseTemplate):
    """
    Template for HANDOFF mode conversations.

    Handles the transition to human agents with
    appropriate context preservation.
    """

    def __init__(self) -> None:
        super().__init__(PromptMode.HANDOFF)

    def build_system_message(self, variables: PromptVariables) -> PromptMessage:
        """Override to append language override as the absolute last instruction."""
        msg = super().build_system_message(variables)
        if variables.language == "en":
            msg.content += _RESIDUAL_LITERALS["language_instruction_en"]
        return msg

    def get_system_prompt(self, variables: PromptVariables) -> str:
        """Get the handoff system prompt."""
        return self.render_template(HANDOFF_SYSTEM_PROMPT, variables)

    def get_context_instructions(self, variables: PromptVariables) -> str:
        """Get handoff-specific instructions based on reason."""
        parts = []

        # Reason-specific instructions
        reason = variables.handoff_reason or "explicit_request"
        if reason in HANDOFF_REASON_INSTRUCTIONS:
            parts.append(HANDOFF_REASON_INSTRUCTIONS[reason])

        # Information collection section
        collection_section = TemplateSection(_CONTEXT_LITERALS[0])
        collection_section.add_list(
            [
                _CONTEXT_LITERALS[1],
                _CONTEXT_LITERALS[2],
                _CONTEXT_LITERALS[3],
                _CONTEXT_LITERALS[4],
                _CONTEXT_LITERALS[5],
            ]
        )
        parts.append(collection_section.build())

        # Slots j\u00e1 coletados
        if variables.slots:
            collected_section = TemplateSection(_CONTEXT_LITERALS[6])
            for key, value in variables.slots.items():
                collected_section.add(f"**{key}**: {value}")
            parts.append(collected_section.build())

        # History summary
        if variables.history_summary:
            history_section = TemplateSection(_RESIDUAL_LITERALS["history_title"])
            history_section.add(variables.history_summary)
            parts.append(history_section.build())

        # Status instructions
        status = variables.handoff_status
        if status == "waiting":
            parts.append(_CONTEXT_LITERALS[7])
        elif status == "connected":
            parts.append(_CONTEXT_LITERALS[8])

        return "\n".join(parts)

    def generate_handoff_summary(self, variables: PromptVariables) -> str:
        """
        Generate a summary for the human agent.

        Args:
            variables: Prompt variables with context

        Returns:
            Formatted summary for agent
        """
        nao_especificado = _SUMMARY_LITERALS[0]
        summary_parts = [
            _SUMMARY_LITERALS[1],
            "",
            f"{_RESIDUAL_LITERALS['channel_label']}{variables.channel}",
            f"{_RESIDUAL_LITERALS['turns_label']}{variables.turn_count}",
            f"{_SUMMARY_LITERALS[2]}{variables.handoff_reason or nao_especificado}",
            "",
        ]

        if variables.funnel_stage:
            summary_parts.append(f"{_SUMMARY_LITERALS[3]}{variables.funnel_stage}")

        if variables.last_intent:
            summary_parts.append(f"{_SUMMARY_LITERALS[4]}{variables.last_intent}")

        if variables.slots:
            summary_parts.append("")
            summary_parts.append(_SUMMARY_LITERALS[5])
            for key, value in variables.slots.items():
                summary_parts.append(f"- {key}: {value}")

        if variables.history_summary:
            summary_parts.append("")
            summary_parts.append(_RESIDUAL_LITERALS["context_title"])
            summary_parts.append(variables.history_summary)

        if variables.safety_category:
            summary_parts.append("")
            summary_parts.append(f"{_SUMMARY_LITERALS[6]}{variables.safety_category}")

        return "\n".join(summary_parts)
