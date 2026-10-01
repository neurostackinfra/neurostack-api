"""Framework-independent NeuroStack core.

Prometheus registration is deliberately opt-in. Importing this package creates
in-memory instruments but does not mutate the default registry.
"""

from typing import TYPE_CHECKING, Any

from neurostack_core.metrics import (
    FLOW_DECISIONS_TOTAL,
    FLOW_HANDLER_DURATION,
    FLOW_TRANSITIONS_TOTAL,
    OUTPUT_VALIDATIONS_TOTAL,
    POLICY_EVALUATIONS,
    POLICY_LATENCY,
    POLICY_VIOLATIONS,
    PROMPT_GENERATION_DURATION,
    PROMPT_GENERATIONS_TOTAL,
    PROMPT_TOKENS_TOTAL,
    SRM_DECISIONS,
    SRM_HANDOFFS,
    SRM_LATENCY,
    SRM_SAFETY_TRIGGERS,
    enable_metrics,
)

if TYPE_CHECKING:
    from neurostack_core.presets import Preset, PresetError, get_preset, use_preset


def __getattr__(name: str) -> Any:
    """Resolve preset startup APIs lazily without selecting or importing content."""

    if name in {"Preset", "PresetError", "get_preset", "use_preset"}:
        from neurostack_core import presets

        value = getattr(presets, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "FLOW_DECISIONS_TOTAL",
    "FLOW_HANDLER_DURATION",
    "FLOW_TRANSITIONS_TOTAL",
    "OUTPUT_VALIDATIONS_TOTAL",
    "POLICY_EVALUATIONS",
    "POLICY_LATENCY",
    "POLICY_VIOLATIONS",
    "PROMPT_GENERATION_DURATION",
    "PROMPT_GENERATIONS_TOTAL",
    "PROMPT_TOKENS_TOTAL",
    "SRM_DECISIONS",
    "SRM_HANDOFFS",
    "SRM_LATENCY",
    "SRM_SAFETY_TRIGGERS",
    "Preset",
    "PresetError",
    "enable_metrics",
    "get_preset",
    "use_preset",
]
