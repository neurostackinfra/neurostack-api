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
from typing import Any, TypeVar

from prometheus_client import REGISTRY, CollectorRegistry
from prometheus_client import Counter as PrometheusCounter
from prometheus_client import Histogram as PrometheusHistogram
from prometheus_client.metrics import MetricWrapperBase
from prometheus_client.metrics_core import Metric
from prometheus_client.registry import Collector

if globals().get("_NEUROSTACK_METRICS_IMPORT_SENTINEL") is not None:
    raise RuntimeError("neurostack_core.metrics cannot be reloaded")
_NEUROSTACK_METRICS_IMPORT_SENTINEL = object()


_CORE_LABEL_POLICIES = {
    "neurostack_srm_decisions": {
        "intent_category": frozenset(
            {
                "scheduling",
                "question",
                "complaint",
                "greeting",
                "farewell",
                "confirmation",
                "cancellation",
                "information",
                "feedback",
                "emergency",
                "unknown",
            }
        ),
        "funnel_stage": frozenset(
            {
                "lead_novo",
                "em_qualificacao",
                "qualificado",
                "followup",
                "agendamento_solicitado",
                "agendado",
                "paciente_ativo",
                "pos_atendimento",
                "fidelizacao",
                "inativo",
                "standby",
                "unknown",
            }
        ),
        "mode": frozenset({"normal", "rag", "safety", "handoff", "error", "unknown"}),
    },
    "neurostack_srm_safety_triggers": {
        "category": frozenset(
            {
                "clinical_risk",
                "legal_risk",
                "conduct_risk",
                "clinical",
                "legal",
                "conduct",
                "emergency",
                "none",
                "unknown",
            }
        )
    },
    "neurostack_srm_latency_seconds": {},
    "neurostack_srm_handoffs": {
        "reason": frozenset(
            {
                "user_request",
                "explicit_request",
                "safety_concern",
                "safety",
                "max_turns_exceeded",
                "max_turns",
                "low_confidence",
                "complex_query",
                "escalation",
                "scheduling",
                "unknown",
            }
        )
    },
    "neurostack_flow_transitions": {
        "from_mode": frozenset(
            {"normal", "rag", "safety", "handoff", "error", "unknown"}
        ),
        "to_mode": frozenset(
            {"normal", "rag", "safety", "handoff", "error", "unknown"}
        ),
        "trigger": frozenset(
            {
                "safety_concern_detected",
                "safety_resolved",
                "handoff_requested",
                "handoff_completed",
                "handoff_cancelled",
                "rag_required",
                "rag_completed",
                "continue_normal",
                "reset_to_normal",
                "session_timeout",
                "session_end",
            }
        ),
    },
    "neurostack_flow_handler_duration_seconds": {
        "mode": frozenset({"normal", "rag", "safety", "handoff", "error", "unknown"})
    },
    "neurostack_flow_decisions": {
        "mode": frozenset({"normal", "rag", "safety", "handoff", "error", "unknown"}),
        "action": frozenset({"process", "escalate", "defer", "block"}),
    },
    "neurostack_policy_evaluations": {"result": frozenset({"passed", "violated"})},
    "neurostack_policy_violations": {
        "category": frozenset(
            {
                "safety_clinical",
                "safety_legal",
                "safety_ethical",
                "content",
                "behavior",
                "compliance",
                "tone",
                "custom",
            }
        ),
        "severity": frozenset({"info", "warning", "error", "critical"}),
        "action": frozenset({"log", "warn", "flag", "block", "handoff"}),
    },
    "neurostack_policy_latency_seconds": {},
    "neurostack_prompt_generations": {
        "mode": frozenset({"normal", "rag", "safety", "handoff", "error", "unknown"})
    },
    "neurostack_prompt_generation_seconds": {},
    "neurostack_prompt_tokens": {
        "mode": frozenset({"normal", "rag", "safety", "handoff", "error", "unknown"})
    },
    "neurostack_output_validations": {
        "mode": frozenset({"normal", "rag", "safety", "handoff", "error", "unknown"}),
        "valid": frozenset({"true", "false"}),
    },
}
_CORE_NORMALIZED_LABELS = frozenset(
    {
        "intent_category",
        "funnel_stage",
        "mode",
        "category",
        "reason",
        "from_mode",
        "to_mode",
    }
)
_MetricT = TypeVar("_MetricT", bound=MetricWrapperBase)


def _contract_metric(metric: _MetricT) -> _MetricT:
    if metric._name not in _CORE_LABEL_POLICIES:
        raise ValueError(f"unknown core metric family: {metric._name}")
    policies = _CORE_LABEL_POLICIES[metric._name]
    labelnames = tuple(metric._labelnames)
    if labelnames != tuple(policies):
        raise ValueError(f"missing or mismatched core metric contract: {metric._name}")
    if not labelnames:
        return metric
    prometheus_labels = metric.labels

    def labels(*values: Any, **kwargs: Any) -> _MetricT:
        if values and kwargs:
            raise ValueError("Can't pass both *args and **kwargs")
        if kwargs:
            if set(kwargs) != set(labelnames):
                raise ValueError("Incorrect label names")
            supplied = {name: str(kwargs[name]) for name in labelnames}
        else:
            if len(values) != len(labelnames):
                raise ValueError("Incorrect label count")
            supplied = {name: str(value) for name, value in zip(labelnames, values)}
        normalized: dict[str, str] = {}
        for name, value in supplied.items():
            if value in policies[name]:
                normalized[name] = value
            elif name in _CORE_NORMALIZED_LABELS and "unknown" in policies[name]:
                normalized[name] = "unknown"
            else:
                raise ValueError(f"{metric._name}.{name} rejected out-of-policy value")
        return prometheus_labels(**normalized)

    setattr(metric, "labels", labels)
    return metric


def Counter(*args: Any, **kwargs: Any) -> PrometheusCounter:
    return _contract_metric(PrometheusCounter(*args, **kwargs))


def Histogram(*args: Any, **kwargs: Any) -> PrometheusHistogram:
    return _contract_metric(PrometheusHistogram(*args, **kwargs))


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
    ["result"],
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
