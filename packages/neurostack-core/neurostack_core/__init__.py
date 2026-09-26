"""Framework-independent NeuroStack core.

Prometheus registration is deliberately opt-in. Importing this package creates
in-memory instruments but does not mutate the default registry.
"""

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
    "enable_metrics",
]
