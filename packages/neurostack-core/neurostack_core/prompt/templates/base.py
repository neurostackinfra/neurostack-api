"""
NeuroStack - Prompt Engine - Base Template
Base class for prompt templates.
"""

import logging
import re
from abc import ABC, abstractmethod

from neurostack_core.prompt.models import (
    MessageRole,
    PromptMessage,
    PromptMode,
    PromptVariables,
)

logger = logging.getLogger(__name__)


class BaseTemplate(ABC):
    """
    Abstract base class for prompt templates.

    Templates define the structure and content of prompts
    for different flow modes.
    """

    def __init__(self, mode: PromptMode):
        """
        Initialize the template.

        Args:
            mode: The flow mode this template handles
        """
        self.mode = mode

    @abstractmethod
    def get_system_prompt(self, variables: PromptVariables) -> str:
        """
        Get the system prompt for this template.

        Args:
            variables: Variables for rendering

        Returns:
            Rendered system prompt
        """
        pass

    @abstractmethod
    def get_context_instructions(self, variables: PromptVariables) -> str:
        """
        Get context-specific instructions.

        Args:
            variables: Variables for rendering

        Returns:
            Context instructions
        """
        pass

    def get_constraints(self, variables: PromptVariables) -> str:
        """
        Get policy constraints as formatted text.

        Args:
            variables: Variables with constraints

        Returns:
            Formatted constraints
        """
        if not variables.constraints:
            return ""

        constraints_text = "\n".join(f"- {c}" for c in variables.constraints)
        return f"\n## Restri\u00e7\u00f5es Importantes\n{constraints_text}\n"

    def get_safety_reminders(self, variables: PromptVariables) -> str:
        """
        Get safety reminders as formatted text.

        Args:
            variables: Variables with safety reminders

        Returns:
            Formatted safety reminders
        """
        if not variables.safety_reminders:
            return ""

        reminders_text = "\n".join(
            f"\u26a0\ufe0f {r}" for r in variables.safety_reminders
        )
        return f"\n## Lembretes de Seguran\u00e7a\n{reminders_text}\n"

    def _get_response_format_reminder(self) -> str:
        """Final reinforcement block — placed last so the LLM weighs it heavily."""
        return (
            "\n## FORMATO DA RESPOSTA (OBRIGATÓRIO)\n"
            "- Responda em NO MÁXIMO 2-3 frases curtas (máximo 40 palavras).\n"
            "- Faça APENAS UMA pergunta por mensagem. NUNCA duas ou mais.\n"
            "- Estilo WhatsApp: direto, acolhedor, sem parágrafos longos.\n"
            "- NUNCA use parênteses com observações, dicas ou meta-comentários. Ex: '(Se você tiver...)' é PROIBIDO.\n"
            "- NUNCA invente serviços que não estão no Clinic Config. Só mencione o que está listado em Serviços Oferecidos.\n"
            "- NUNCA invente pré-requisitos (ex: 'é necessário uma consulta antes'). Só informe o que está explícito no Clinic Config.\n"
            "- Se 'Tipo de consulta inicial' for Gratuita/Cortesia, a consulta inicial é uma CORTESIA da clínica. NUNCA diga que custa dinheiro, NUNCA diga que será 'ressarcida'. É uma CORTESIA.\n"
            "- NUNCA use listas com bullet points (*, -, 1.). Responda em texto corrido.\n"
            "- NÃO se apresente novamente se já se apresentou antes (verifique o histórico).\n"
        )

    def build_system_message(self, variables: PromptVariables) -> PromptMessage:
        """
        Build the complete system message.

        Args:
            variables: Variables for rendering

        Returns:
            Complete system PromptMessage
        """
        parts = [
            self.get_system_prompt(variables),
            self.get_context_instructions(variables),
            self.get_constraints(variables),
            self.get_safety_reminders(variables),
            self._get_response_format_reminder(),
        ]

        # Filter empty parts and join
        content = "\n".join(p for p in parts if p.strip())

        return PromptMessage(
            role=MessageRole.SYSTEM,
            content=content,
        )

    def render_template(
        self,
        template: str,
        variables: PromptVariables,
    ) -> str:
        """
        Render a template string with variables.

        Supports {{variable}} syntax for substitution.

        Args:
            template: Template string
            variables: Variables for substitution

        Returns:
            Rendered string
        """
        result = template

        # Get all variable values as dict
        var_dict = variables.model_dump()

        # Find all {{variable}} patterns
        pattern = r"\{\{(\w+)\}\}"
        matches = re.findall(pattern, result)

        for var_name in matches:
            if var_name in var_dict:
                value = var_dict[var_name]
                if value is not None:
                    result = result.replace(f"{{{{{var_name}}}}}", str(value))
                else:
                    result = result.replace(f"{{{{{var_name}}}}}", "")
            elif var_name in var_dict.get("custom", {}):
                value = var_dict["custom"][var_name]
                result = result.replace(f"{{{{{var_name}}}}}", str(value))
            else:
                # Leave placeholder if variable not found
                logger.warning(f"Template variable not found: {var_name}")

        return result

    def get_used_variables(self, template: str) -> list[str]:
        """Get list of variables used in a template."""
        pattern = r"\{\{(\w+)\}\}"
        return re.findall(pattern, template)


class TemplateSection:
    """Helper class for building template sections."""

    def __init__(self, title: str):
        self.title = title
        self.content: list[str] = []

    def add(self, text: str) -> "TemplateSection":
        """Add text to section."""
        self.content.append(text)
        return self

    def add_list(self, items: list[str]) -> "TemplateSection":
        """Add a list of items."""
        for item in items:
            self.content.append(f"- {item}")
        return self

    def build(self) -> str:
        """Build the section as string."""
        if not self.content:
            return ""

        lines = [f"## {self.title}", *self.content, ""]
        return "\n".join(lines)
