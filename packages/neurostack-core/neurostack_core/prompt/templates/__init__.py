"""
NeuroStack - Prompt Engine - Templates
Templates for different conversation modes.
"""

from neurostack_core.prompt.templates.base import BaseTemplate, TemplateSection
from neurostack_core.prompt.templates.handoff import HandoffTemplate
from neurostack_core.prompt.templates.normal import NormalTemplate
from neurostack_core.prompt.templates.safety import SafetyTemplate

__all__ = [
    "BaseTemplate",
    "TemplateSection",
    "NormalTemplate",
    "SafetyTemplate",
    "HandoffTemplate",
]
