"""Opt-in Prometheus metrics for the NeuroStack cognitive core.

Metric objects are useful before registration: observations accumulate locally
and become visible when :func:`enable_metrics` registers the one composite
collector. A single lock makes registry ownership and the all-family
registration transaction indivisible.

Manual module reload is unsupported. Failing closed preserves the original
instruments and their observations instead of constructing a second metric set.
"""

from __future__ import annotations

from collections.abc import Iterable
from threading import Lock

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Histogram
from prometheus_client.metrics_core import Metric
from prometheus_client.registry import Collector

if globals().get("_NEUROSTACK_METRICS_IMPORT_SENTINEL") is not None:
    raise RuntimeError("neurostack_core.metrics cannot be reloaded")
_NEUROSTACK_METRICS_IMPORT_SENTINEL = object()


SRM_DECISIONS = Counter(
    "neurostack_srm_decisions_total",
    "Total number of SRM decisions",
    ["intent_category", "funnel_stage", "mode"],
    registry=None,
)

SRM_LATENCY = Histogram(
    "neurostack_srm_latency_seconds",
    "SRM processing latency in seconds",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25),
    registry=None,
)

SRM_SAFETY_TRIGGERS = Counter(
    "neurostack_srm_safety_triggers_total",
    "Total number of safety triggers",
    ["category"],
    registry=None,
)

SRM_HANDOFFS = Counter(
    "neurostack_srm_handoffs_total",
    "Total number of handoff decisions",
    ["reason"],
    registry=None,
)

FLOW_TRANSITIONS_TOTAL = Counter(
    "neurostack_flow_transitions_total",
    "Total flow state transitions",
    ["from_mode", "to_mode", "trigger"],
    registry=None,
)

FLOW_HANDLER_DURATION = Histogram(
    "neurostack_flow_handler_duration_seconds",
    "Flow handler processing time",
    ["mode"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5),
    registry=None,
)

FLOW_DECISIONS_TOTAL = Counter(
    "neurostack_flow_decisions_total",
    "Total flow decisions made",
    ["mode", "action"],
    registry=None,
)

POLICY_EVALUATIONS = Counter(
    "neurostack_policy_evaluations_total",
    "Total number of policy evaluations",
    ["tenant_id", "result"],
    registry=None,
)

POLICY_VIOLATIONS = Counter(
    "neurostack_policy_violations_total",
    "Total number of policy violations",
    ["category", "severity", "action"],
    registry=None,
)

POLICY_LATENCY = Histogram(
    "neurostack_policy_latency_seconds",
    "Policy evaluation latency in seconds",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1),
    registry=None,
)

PROMPT_GENERATIONS_TOTAL = Counter(
    "neurostack_prompt_generations_total",
    "Total prompt generations",
    ["mode"],
    registry=None,
)

PROMPT_GENERATION_DURATION = Histogram(
    "neurostack_prompt_generation_seconds",
    "Prompt generation time",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05),
    registry=None,
)

PROMPT_TOKENS_TOTAL = Counter(
    "neurostack_prompt_tokens_total",
    "Total tokens in generated prompts",
    ["mode"],
    registry=None,
)

OUTPUT_VALIDATIONS_TOTAL = Counter(
    "neurostack_output_validations_total",
    "Total output validations",
    ["mode", "valid"],
    registry=None,
)

_INSTRUMENTS = (
    SRM_DECISIONS,
    SRM_LATENCY,
    SRM_SAFETY_TRIGGERS,
    SRM_HANDOFFS,
    FLOW_TRANSITIONS_TOTAL,
    FLOW_HANDLER_DURATION,
    FLOW_DECISIONS_TOTAL,
    POLICY_EVALUATIONS,
    POLICY_VIOLATIONS,
    POLICY_LATENCY,
    PROMPT_GENERATIONS_TOTAL,
    PROMPT_GENERATION_DURATION,
    PROMPT_TOKENS_TOTAL,
    OUTPUT_VALIDATIONS_TOTAL,
)


class _CompositeCollector(Collector):
    """Present all core instruments to a registry as one atomic collector."""

    def collect(self) -> Iterable[Metric]:
        for instrument in _INSTRUMENTS:
            yield from instrument.collect()

    def describe(self) -> Iterable[Metric]:
        for instrument in _INSTRUMENTS:
            yield from instrument.describe()


_COLLECTOR = _CompositeCollector()
_ENABLE_LOCK = Lock()
_OWNER: CollectorRegistry | None = None


def enable_metrics(registry: CollectorRegistry | None = None) -> None:
    """Register all core metric families once in exactly one registry.

    The first successful call owns the metric set. Repeating with that exact
    registry object is idempotent; using another registry is an error. Registry
    collision errors leave ownership unset and the registry unmodified because
    prometheus-client checks every name before inserting this composite.
    """

    global _OWNER

    target = REGISTRY if registry is None else registry
    with _ENABLE_LOCK:
        if _OWNER is target:
            return
        if _OWNER is not None:
            raise RuntimeError("NeuroStack metrics already belong to another registry")
        target.register(_COLLECTOR)
        _OWNER = target
