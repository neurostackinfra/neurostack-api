"""Closed dimension contract for every exportable ``neurostack_*`` metric.

Prometheus labels are an aggregation API, not a diagnostics payload.  This
module is intentionally dependency-free so metric owners can validate values
without importing the application graph.  Tenant and request identities stay
in structured logs; only bounded technical dimensions are accepted here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping, TypeVar

from prometheus_client import CollectorRegistry
from prometheus_client import Counter as PrometheusCounter
from prometheus_client import Gauge as PrometheusGauge
from prometheus_client import Histogram as PrometheusHistogram
from prometheus_client.metrics import MetricWrapperBase

LabelSource = Literal["enum", "template", "normalizer"]


class MetricDimensionContractError(ValueError):
    """Raised when an exportable metric violates the closed contract."""


@dataclass(frozen=True)
class LabelPolicy:
    """Origin and vocabulary allowed for one technical label."""

    source: LabelSource
    vocabulary: frozenset[str] | None = None
    normalizer: str | None = None
    fallback: str | None = None

    def accepts(self, value: str) -> bool:
        if self.vocabulary is not None:
            return value in self.vocabulary
        if self.normalizer == "http_status":
            return (
                value == "unknown" or re.fullmatch(r"[1-5][0-9]{2}", value) is not None
            )
        if self.normalizer == "route_template":
            return _is_route_template(value)
        return False


@dataclass(frozen=True)
class MetricDimensionContract:
    """Exact ordered labels and their server-controlled value policies."""

    labels: tuple[str, ...]
    policies: Mapping[str, LabelPolicy]


_FORBIDDEN_LABEL_NAMES = frozenset(
    {
        "tenant",
        "tenant_id",
        "email",
        "cpf",
        "phone",
        "telefone",
        "user",
        "user_id",
        "session",
        "session_id",
        "trace",
        "trace_id",
        "request",
        "request_id",
        "message",
        "message_id",
        "prompt",
        "model",
        "rule_id",
    }
)
_GENERATED_LABELS = frozenset({"le", "quantile"})
_ROUTE_TEMPLATE_RE = re.compile(
    r"^/(?:[A-Za-z0-9._~-]+|\{[a-z_][a-z0-9_]*\})(?:/(?:[A-Za-z0-9._~-]+|\{[a-z_][a-z0-9_]*\}))*$|^/$"
)
_PII_ROUTE_RE = re.compile(
    r"@|\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b|(?:\+?\d[\s().-]?){8,}",
    flags=re.IGNORECASE,
)
_DYNAMIC_ROUTE_SEGMENT_RE = re.compile(
    r"(?:^|/)(?:[0-9a-f]{8}-[0-9a-f-]{27,}|[A-Za-z0-9_-]{20,})(?=/|$)",
    flags=re.IGNORECASE,
)


def _is_route_template(value: str) -> bool:
    return (
        len(value) <= 200
        and _ROUTE_TEMPLATE_RE.fullmatch(value) is not None
        and _PII_ROUTE_RE.search(value) is None
        and _DYNAMIC_ROUTE_SEGMENT_RE.search(value) is None
    )


def _enum(*values: str, fallback: str | None = None) -> LabelPolicy:
    return LabelPolicy("enum", frozenset(values), fallback=fallback)


def _normalized(*values: str, fallback: str = "unknown") -> LabelPolicy:
    return LabelPolicy(
        "normalizer",
        frozenset(values),
        normalizer="closed_vocabulary",
        fallback=fallback,
    )


def _contract(**policies: LabelPolicy) -> MetricDimensionContract:
    return MetricDimensionContract(tuple(policies), MappingProxyType(dict(policies)))


_NONE = _contract()
_FLOW_MODE = _normalized("normal", "rag", "safety", "handoff", "error", "unknown")
_FUNNEL_STAGE = _normalized(
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
)
_INTENT = _normalized(
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
)
_SAFETY_CATEGORY = _normalized(
    "clinical_risk",
    "legal_risk",
    "conduct_risk",
    "clinical",
    "legal",
    "conduct",
    "emergency",
    "none",
    "unknown",
)
_HANDOFF_REASON = _normalized(
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
)
_SEVERITY = _normalized(
    "info", "low", "warning", "medium", "error", "high", "critical", "unknown"
)
_HTTP_METHOD = _enum(
    "GET",
    "HEAD",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
    "CONNECT",
    "TRACE",
    "OTHER",
    fallback="OTHER",
)
_HTTP_STATUS = LabelPolicy("normalizer", normalizer="http_status", fallback="unknown")
_ENDPOINT = LabelPolicy(
    "template",
    normalizer="route_template",
    fallback="/{unmatched}",
)
_BILLING_MIGRATION_OPERATION = _enum(
    "roster_import",
    "cancel_begin",
    "cancel_reconcile",
    "recreate_pending",
    "recreate_reserve",
    "recreate_reopen",
    "checkout_recovery",
    "provider_flip",
    "manual_intervention",
)
_BILLING_MIGRATION_STATE = _enum(
    "pending",
    "in_progress",
    "legacy_cancel_pending",
    "legacy_cancelled",
    "recreate_pending",
    "migrated",
    "manual_intervention",
)
_BILLING_OPERATION_ROLE = _enum("deployer", "approver", "reviewer", "unknown")
_BILLING_OPERATION_OUTCOME = _enum("accepted", "rejected", "consumed")
_BILLING_TOPUP_DIRECTION = _enum("enable", "disable")


# Keys are prometheus-client family names (Counter strips the ``_total``
# suffix).  Every family emitted by the application registry and by the core
# opt-in collector is listed explicitly; additions must make an intentional
# policy choice here before they can pass validation.
METRIC_DIMENSION_CONTRACTS: Mapping[str, MetricDimensionContract] = MappingProxyType(
    {
        # HTTP/runtime
        "neurostack_request_latency_seconds": _contract(
            method=_HTTP_METHOD, endpoint=_ENDPOINT, status=_HTTP_STATUS
        ),
        "neurostack_requests": _contract(
            method=_HTTP_METHOD, endpoint=_ENDPOINT, status=_HTTP_STATUS
        ),
        "neurostack_active_requests": _contract(method=_HTTP_METHOD),
        "neurostack_cache_hits": _contract(cache_type=_enum("response")),
        "neurostack_cache_misses": _contract(cache_type=_enum("response")),
        # Application pipeline and stores
        "neurostack_pipeline": _contract(
            mode=_FLOW_MODE,
            result=_enum("success", "service_error", "error"),
        ),
        "neurostack_pipeline_latency_seconds": _NONE,
        "neurostack_pipeline_stage_latency_seconds": _contract(
            stage=_normalized(
                "policy",
                "srm",
                "flow",
                "ctx",
                "prompt",
                "handoff",
                "llm",
                "unknown",
            )
        ),
        "neurostack_ctx_builds": _contract(mode=_enum("normal", "rag")),
        "neurostack_ctx_latency_seconds": _NONE,
        "neurostack_ctx_tokens": _NONE,
        "neurostack_redis_operations": _contract(
            operation=_enum(
                "connect",
                "set_tenant_ttl",
                "create_session",
                "get_session",
                "update_session",
                "delete_session",
                "exists",
                "list_sessions",
                "count_sessions",
                "refresh_ttl",
            ),
            status=_enum("success", "not_found", "error"),
        ),
        "neurostack_redis_latency_seconds": _contract(
            operation=_enum(
                "set_tenant_ttl",
                "create_session",
                "get_session",
                "update_session",
                "delete_session",
                "exists",
                "list_sessions",
                "count_sessions",
                "refresh_ttl",
            )
        ),
        "neurostack_event_store_operations": _contract(
            operation=_enum(
                "append",
                "append_batch",
                "query_by_session",
                "query_by_tenant",
                "get_event",
                "get_recent_messages",
                "count_by_type",
                "count_sessions",
            ),
            status=_enum("success", "not_found", "error"),
        ),
        "neurostack_event_store_latency_seconds": _contract(
            operation=_enum(
                "append",
                "append_batch",
                "query_by_session",
                "query_by_tenant",
                "get_event",
                "get_recent_messages",
                "count_by_type",
                "count_sessions",
            )
        ),
        "neurostack_events_appended": _contract(
            event_type=_enum(
                "message.in",
                "message.out",
                "session.start",
                "session.end",
                "session.timeout",
                "srm.decision",
                "state.patch",
                "funnel.transition",
                "flow.transition",
                "mode.change",
                "safety.trigger",
                "safety.block",
                "handoff.request",
                "handoff.accept",
                "handoff.complete",
                "handoff.timeout",
                "rag.query",
                "rag.result",
                "policy.applied",
                "policy.violation",
                "prompt.generated",
                "ns.atendimento_tipo_set",
                "ns.funnel_stage_changed",
                "ns.handoff_created",
                "error",
                "warning",
            )
        ),
        "neurostack_handoff_operations": _contract(
            operation=_enum("initiate", "update"),
            status=_enum("success", "error", "conflict", "not_found", "rejected"),
        ),
        "neurostack_handoff_by_reason": _contract(
            reason=_HANDOFF_REASON,
            urgency=_enum("low", "normal", "high", "critical"),
        ),
        "neurostack_handoff_latency_seconds": _contract(
            operation=_enum("initiate", "update")
        ),
        "neurostack_handoff_webhook_operations": _contract(
            operation=_enum("send"), status=_enum("success", "failed")
        ),
        "neurostack_handoff_webhook_latency_seconds": _contract(
            operation=_enum("send")
        ),
        "neurostack_handoff_webhook_retries": _NONE,
        # Active guards
        "neurostack_price_gate_violations": _contract(
            atendimento_tipo=_enum("convenio", "undefined")
        ),
        "neurostack_scheduling_gate_violations": _contract(
            category=_enum(
                "confirmed_appointment", "asserted_availability", "processed_change"
            )
        ),
        # Legacy business instruments
        "neurostack_business_srm_decisions": _contract(flow_mode=_FLOW_MODE),
        "neurostack_srm_processing_seconds": _contract(
            component=_normalized(
                "total",
                "intent_classifier",
                "funnel_detector",
                "safety_detector",
                "handoff_detector",
                "rag_detector",
                "sentiment_detector",
                "unknown",
            )
        ),
        "neurostack_intent_classifications": _contract(intent=_INTENT),
        "neurostack_intent_confidence": _contract(intent=_INTENT),
        "neurostack_funnel_stage": _contract(stage=_FUNNEL_STAGE),
        "neurostack_safety_triggers": _contract(category=_SAFETY_CATEGORY),
        "neurostack_safety_confidence": _contract(category=_SAFETY_CATEGORY),
        "neurostack_safety_severity": _contract(
            severity=_SEVERITY, category=_SAFETY_CATEGORY
        ),
        "neurostack_business_flow_transitions": _contract(
            from_mode=_FLOW_MODE, to_mode=_FLOW_MODE
        ),
        "neurostack_flow_mode_duration_seconds": _contract(mode=_FLOW_MODE),
        "neurostack_active_sessions_by_mode": _contract(mode=_FLOW_MODE),
        "neurostack_handoffs": _contract(reason=_HANDOFF_REASON),
        "neurostack_handoff_turn_count": _contract(reason=_HANDOFF_REASON),
        "neurostack_handoff_response_seconds": _NONE,
        "neurostack_handoff_webhooks": _contract(
            status=_enum("success", "failed", "timeout", "unknown")
        ),
        "neurostack_rag_queries": _NONE,
        "neurostack_rag_results_count": _NONE,
        "neurostack_rag_latency_seconds": _NONE,
        "neurostack_policies_applied": _contract(
            policy_type=_normalized("global", "tenant", "session", "safety", "unknown")
        ),
        "neurostack_business_policy_violations": _NONE,
        "neurostack_pipeline_total_seconds": _NONE,
        "neurostack_pipeline_stage_seconds": _contract(
            stage=_normalized(
                "policy",
                "srm",
                "flow",
                "ctx",
                "prompt",
                "handoff",
                "llm",
                "unknown",
            )
        ),
        "neurostack_llm_generation_attempts": _contract(
            tier=_enum("primary", "multimodal_fallback"),
            outcome=_enum("response", "invalid", "empty", "error"),
        ),
        # ASAAS billing webhook/readback (Group 3).  Only closed technical
        # vocabularies: never builder, payment, subscription or event IDs.
        "neurostack_billing_webhook_deliveries": _contract(
            family=_enum("checkout", "subscription", "payment", "unknown"),
            outcome=_enum(
                "processed",
                "duplicate",
                "unknown_event",
                "uncorrelated",
                "readback_required",
                "quarantined",
            ),
        ),
        "neurostack_billing_webhook_rejections": _contract(
            reason=_enum(
                "unauthorized",
                "unavailable",
                "unsupported_media_type",
                "malformed",
                "too_large",
                "timeout",
                "invalid_json",
                "invalid_event",
                "processing_unavailable",
            )
        ),
        "neurostack_billing_alerts": _contract(
            kind=_enum(
                "unknown_event",
                "uncorrelated_entity",
                "drift",
                "backlog",
                "readback_failure",
                "quarantine",
            )
        ),
        "neurostack_billing_webhook_failures": _contract(
            failure=_enum(
                "integrity",
                "data_error",
                "canary_budget_exceeded",
                "transition_rejected",
                "capability_rejected",
                "state_unavailable",
                "serialization",
                "lock_timeout",
                "database",
                "timeout",
                "connection",
                "unexpected",
            ),
            disposition=_enum("quarantined", "already_applied", "retry"),
        ),
        "neurostack_billing_readbacks": _contract(
            resource=_enum("checkout_session", "subscription", "payment", "webhook"),
            outcome=_enum("converged", "pending", "drift", "failed"),
        ),
        "neurostack_billing_readback_backlog": _contract(
            resource=_enum("delivery", "checkout_session", "subscription", "payment")
        ),
        "neurostack_billing_webhook_config_ready": _NONE,
        "neurostack_billing_summary_checkout_unavailable": _NONE,
        # Group 5 migration control plane.  The vocabulary remains closed so
        # no migration, actor, provider, subscription, receipt or deployment
        # identity can enter Prometheus.
        "neurostack_billing_migration_operations": _contract(
            operation=_BILLING_MIGRATION_OPERATION,
            outcome=_BILLING_OPERATION_OUTCOME,
        ),
        "neurostack_billing_migration_backlog": _contract(
            state=_BILLING_MIGRATION_STATE
        ),
        "neurostack_billing_operation_receipts": _contract(
            role=_BILLING_OPERATION_ROLE,
            outcome=_BILLING_OPERATION_OUTCOME,
        ),
        "neurostack_billing_topup_capability_transitions": _contract(
            direction=_BILLING_TOPUP_DIRECTION,
            outcome=_BILLING_OPERATION_OUTCOME,
        ),
        # Group 9 billing.fact.v1 outbox worker: delivery conditions and
        # outcomes only; event, builder and aggregate identities stay out.
        "neurostack_billing_fact_backlog": _contract(
            condition=_enum("unacked", "stale", "blocked")
        ),
        "neurostack_billing_fact_publish_attempts": _contract(
            outcome=_enum("presented", "failed", "exhausted")
        ),
        "neurostack_billing_fact_worker_ticks": _contract(
            outcome=_enum("completed", "deadline", "stopped", "failed")
        ),
        "neurostack_billing_fact_worker_last_tick_timestamp_seconds": _NONE,
        # neurostack-core opt-in collector
        "neurostack_srm_decisions": _contract(
            intent_category=_INTENT, funnel_stage=_FUNNEL_STAGE, mode=_FLOW_MODE
        ),
        "neurostack_srm_latency_seconds": _NONE,
        "neurostack_srm_safety_triggers": _contract(category=_SAFETY_CATEGORY),
        "neurostack_srm_handoffs": _contract(reason=_HANDOFF_REASON),
        "neurostack_flow_transitions": _contract(
            from_mode=_FLOW_MODE,
            to_mode=_FLOW_MODE,
            trigger=_enum(
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
            ),
        ),
        "neurostack_flow_handler_duration_seconds": _contract(mode=_FLOW_MODE),
        "neurostack_flow_decisions": _contract(
            mode=_FLOW_MODE, action=_enum("process", "escalate", "defer", "block")
        ),
        "neurostack_policy_evaluations": _contract(result=_enum("passed", "violated")),
        "neurostack_policy_violations": _contract(
            category=_enum(
                "safety_clinical",
                "safety_legal",
                "safety_ethical",
                "content",
                "behavior",
                "compliance",
                "tone",
                "custom",
            ),
            severity=_enum("info", "warning", "error", "critical"),
            action=_enum("log", "warn", "flag", "block", "handoff"),
        ),
        "neurostack_policy_latency_seconds": _NONE,
        "neurostack_prompt_generations": _contract(mode=_FLOW_MODE),
        "neurostack_prompt_generation_seconds": _NONE,
        "neurostack_prompt_tokens": _contract(mode=_FLOW_MODE),
        "neurostack_output_validations": _contract(
            mode=_FLOW_MODE, valid=_enum("true", "false")
        ),
    }
)


def metric_dimension_contract(metric_name: str) -> MetricDimensionContract:
    """Return the policy for a family, failing closed for unknown names."""

    try:
        return METRIC_DIMENSION_CONTRACTS[metric_name]
    except KeyError as exc:
        if metric_name.startswith("neurostack_"):
            raise MetricDimensionContractError(
                f"unknown neurostack metric family: {metric_name}"
            ) from exc
        raise


def bounded_metric_labels(metric_name: str, **labels: object) -> dict[str, str]:
    """Normalize an emitter's labels according to its exact family contract."""

    contract = metric_dimension_contract(metric_name)
    if tuple(labels) != contract.labels:
        raise MetricDimensionContractError(
            f"{metric_name} labels {tuple(labels)!r} != {contract.labels!r}"
        )

    normalized: dict[str, str] = {}
    for label, raw_value in labels.items():
        value = str(raw_value)
        policy = contract.policies[label]
        if policy.accepts(value):
            normalized[label] = value
        elif policy.fallback is not None and policy.accepts(policy.fallback):
            normalized[label] = policy.fallback
        else:
            raise MetricDimensionContractError(
                f"{metric_name}.{label} rejected value outside its {policy.source} policy"
            )
    return normalized


_MetricT = TypeVar("_MetricT", bound=MetricWrapperBase)


def enforce_metric_dimensions(metric: _MetricT) -> _MetricT:
    """Make a Prometheus instrument enforce the contract on every label bind.

    Validation at scrape time is too late: public instruments otherwise retain
    arbitrary identity-bearing values in their child cache.  Wrapping the
    parent ``labels`` method preserves prometheus-client's public instrument
    API while normalizing or rejecting values before a time series exists.
    """

    if not metric._name.startswith("neurostack_"):
        return metric
    contract = metric_dimension_contract(metric._name)
    labelnames = tuple(metric._labelnames)
    if labelnames != contract.labels:
        raise MetricDimensionContractError(
            f"{metric._name} labels {labelnames!r} != {contract.labels!r}"
        )
    if not labelnames:
        return metric

    prometheus_labels = metric.labels

    def contract_labels(*labelvalues: Any, **labelkwargs: Any) -> _MetricT:
        if labelvalues and labelkwargs:
            raise ValueError("Can't pass both *args and **kwargs")
        if labelkwargs:
            if set(labelkwargs) != set(labelnames):
                raise ValueError("Incorrect label names")
            supplied = {name: labelkwargs[name] for name in labelnames}
        else:
            if len(labelvalues) != len(labelnames):
                raise ValueError("Incorrect label count")
            supplied = dict(zip(labelnames, labelvalues))
        normalized = bounded_metric_labels(metric._name, **supplied)
        return prometheus_labels(**normalized)

    setattr(metric, "labels", contract_labels)
    return metric


def ContractCounter(*args: Any, **kwargs: Any) -> PrometheusCounter:
    """Construct a counter whose public label API is contract-aware."""

    return enforce_metric_dimensions(PrometheusCounter(*args, **kwargs))


def ContractGauge(*args: Any, **kwargs: Any) -> PrometheusGauge:
    """Construct a gauge whose public label API is contract-aware."""

    return enforce_metric_dimensions(PrometheusGauge(*args, **kwargs))


def ContractHistogram(*args: Any, **kwargs: Any) -> PrometheusHistogram:
    """Construct a histogram whose public label API is contract-aware."""

    return enforce_metric_dimensions(PrometheusHistogram(*args, **kwargs))


def validate_metric_registry(registry: CollectorRegistry) -> None:
    """Validate all collected ``neurostack_*`` families and observed values."""

    for family in registry.collect():
        if not family.name.startswith("neurostack_"):
            continue
        contract = metric_dimension_contract(family.name)
        for sample in family.samples:
            observed = tuple(
                label for label in sample.labels if label not in _GENERATED_LABELS
            )
            if observed != contract.labels:
                raise MetricDimensionContractError(
                    f"{family.name} labels {observed!r} != {contract.labels!r}"
                )
            for label in observed:
                if label in _FORBIDDEN_LABEL_NAMES:
                    raise MetricDimensionContractError(
                        f"{family.name} contains identifying label {label}"
                    )
                value = sample.labels[label]
                if not contract.policies[label].accepts(value):
                    raise MetricDimensionContractError(
                        f"{family.name}.{label} has out-of-policy value"
                    )


__all__ = [
    "METRIC_DIMENSION_CONTRACTS",
    "ContractCounter",
    "ContractGauge",
    "ContractHistogram",
    "LabelPolicy",
    "MetricDimensionContract",
    "MetricDimensionContractError",
    "bounded_metric_labels",
    "enforce_metric_dimensions",
    "metric_dimension_contract",
    "validate_metric_registry",
]
