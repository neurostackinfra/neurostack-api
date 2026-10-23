"""Regression contract for bounded, identity-free Prometheus dimensions."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import textwrap
from collections.abc import Iterable

import pytest
from prometheus_client import CollectorRegistry, Counter

from neurostack_core import metrics as core_metrics
from src.observability.metric_dimensions import (
    METRIC_DIMENSION_CONTRACTS,
    ContractCounter,
    MetricDimensionContractError,
    bounded_metric_labels,
    metric_dimension_contract,
    validate_metric_registry,
)

_APPLICATION_METRIC_MODULES = (
    "src.api.middleware.performance",
    "src.core.pipeline",
    "src.ctx.orchestrator",
    "src.handoff.webhook",
    "src.memory.event_store",
    "src.memory.state_store",
    "src.observability.metrics",
    "src.observability.price_gate",
    "src.observability.scheduling_gate",
    "src.services.handoff_service",
)

_IDENTIFYING_LABELS = (
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
)


def _application_instruments() -> Iterable[object]:
    seen: set[int] = set()
    for module_name in _APPLICATION_METRIC_MODULES:
        module = importlib.import_module(module_name)
        for candidate in vars(module).values():
            name = getattr(candidate, "_name", "")
            if (
                isinstance(name, str)
                and name.startswith("neurostack_")
                and hasattr(candidate, "_labelnames")
                and id(candidate) not in seen
            ):
                seen.add(id(candidate))
                yield candidate


def _sample_labels(metric_name: str) -> dict[str, str]:
    contract = metric_dimension_contract(metric_name)
    result: dict[str, str] = {}
    for name, policy in contract.policies.items():
        if policy.normalizer == "http_status":
            result[name] = "200"
        elif policy.normalizer == "route_template":
            result[name] = "/v1/tenants/{id}"
        else:
            assert policy.vocabulary
            result[name] = sorted(policy.vocabulary)[0]
    return result


def _exercise(instrument: object) -> None:
    metric_name = instrument._name
    labels = _sample_labels(metric_name)
    child = instrument.labels(**labels) if labels else instrument
    if hasattr(child, "observe"):
        child.observe(0.01)
    elif hasattr(child, "set"):
        child.set(1)
    else:
        child.inc()


def test_real_registry_and_core_collector_match_the_closed_contract() -> None:
    registry = CollectorRegistry()
    instruments = [*_application_instruments(), *core_metrics._INSTRUMENTS]

    for instrument in instruments:
        contract = metric_dimension_contract(instrument._name)
        assert tuple(instrument._labelnames) == contract.labels
        registry.register(instrument)
        _exercise(instrument)

    validate_metric_registry(registry)
    observed = {
        family.name
        for family in registry.collect()
        if family.name.startswith("neurostack_")
    }
    # 61 pre-existing families + 7 Group 3 billing families + 2 Group 5
    # control-plane families (Group 10 E7 retired the two migration families)
    # + 4 Group 9 billing fact worker families + 1 Group 6b billing summary
    # family.
    assert len(METRIC_DIMENSION_CONTRACTS) == 75
    assert observed == set(METRIC_DIMENSION_CONTRACTS)


def test_core_family_labels_and_vocabularies_match_the_central_contract() -> None:
    instruments = {
        instrument._name: instrument for instrument in core_metrics._INSTRUMENTS
    }

    assert set(instruments) == set(core_metrics._CORE_LABEL_POLICIES)
    for metric_name, label_policies in core_metrics._CORE_LABEL_POLICIES.items():
        contract = metric_dimension_contract(metric_name)
        assert tuple(instruments[metric_name]._labelnames) == tuple(label_policies)
        assert contract.labels == tuple(label_policies)

        for label, vocabulary in label_policies.items():
            policy = contract.policies[label]
            assert policy.vocabulary == vocabulary
            expected_fallback = (
                "unknown"
                if label in core_metrics._CORE_NORMALIZED_LABELS
                and "unknown" in vocabulary
                else None
            )
            assert policy.fallback == expected_fallback


def test_effective_application_registry_with_core_enable_is_enforced() -> None:
    source = textwrap.dedent("""
        from prometheus_client import REGISTRY
        import src.api.main
        import neurostack_core as core
        from src.observability.metric_dimensions import validate_metric_registry

        core.enable_metrics()
        validate_metric_registry(REGISTRY)
        assert any(
            family.name == "neurostack_policy_evaluations"
            for family in REGISTRY.collect()
        )
        """)
    env = os.environ.copy()
    env["PYTHONPATH"] = "packages/neurostack-core"
    result = subprocess.run(
        [sys.executable, "-c", source],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_contract_uses_only_explicit_origin_policies() -> None:
    assert METRIC_DIMENSION_CONTRACTS
    for metric_name, contract in METRIC_DIMENSION_CONTRACTS.items():
        assert metric_name.startswith("neurostack_")
        assert set(contract.labels) == set(contract.policies)
        assert not set(contract.labels).intersection(_IDENTIFYING_LABELS)
        assert {policy.source for policy in contract.policies.values()} <= {
            "enum",
            "template",
            "normalizer",
        }


def test_unknown_neurostack_family_fails_closed() -> None:
    with pytest.raises(MetricDimensionContractError, match="unknown"):
        metric_dimension_contract("neurostack_unreviewed_payload_total")

    with pytest.raises(MetricDimensionContractError, match="unknown"):
        ContractCounter(
            "neurostack_unreviewed_payload_total",
            "unknown public family",
            ["reason"],
            registry=None,
        )


def test_public_instrument_normalizes_before_a_series_is_created() -> None:
    from src.observability.metrics import HANDOFFS_TOTAL

    sensitive = (
        "requested",
        "patient@example.com",
        "52998224725",
        "request-12345678901234567890",
        "session-12345678901234567890",
        "trace-12345678901234567890",
        "aBc-VeryLongCapabilityToken_0123456789xyz",
    )
    for value in sensitive:
        HANDOFFS_TOTAL.labels(reason=value).inc()
    samples = tuple(HANDOFFS_TOTAL.collect())[0].samples
    exported = {
        sample.labels["reason"] for sample in samples if "reason" in sample.labels
    }
    assert "unknown" in exported
    assert not exported.intersection(sensitive)


@pytest.mark.parametrize("label", _IDENTIFYING_LABELS)
def test_identifying_dimensions_are_rejected(label: str) -> None:
    registry = CollectorRegistry()
    metric = Counter(
        "neurostack_handoff_webhook_retries_total",
        "invalid identity-bearing regression fixture",
        [label],
        registry=registry,
    )
    metric.labels(**{label: "patient@example.com"}).inc()

    with pytest.raises(MetricDimensionContractError, match="labels"):
        validate_metric_registry(registry)


@pytest.mark.parametrize(
    "endpoint",
    (
        "/patients/patient@example.com",
        "/patients/52998224725",
        "/patients/+5511999999999",
        "/requests/12345678-1234-1234-1234-123456789012",
        "/webhook/aBc-VeryLongCapabilityToken_0123456789xyz",
    ),
)
def test_endpoint_policy_collapses_pii_and_accepts_route_templates(
    endpoint: str,
) -> None:
    assert (
        bounded_metric_labels(
            "neurostack_requests",
            method="GET",
            endpoint=endpoint,
            status="200",
        )["endpoint"]
        == "/{unmatched}"
    )

    assert (
        bounded_metric_labels(
            "neurostack_requests",
            method="GET",
            endpoint="/v1/tenants/{tenant_id}/sessions/{id}",
            status="200",
        )["endpoint"]
        == "/v1/tenants/{tenant_id}/sessions/{id}"
    )


def test_arbitrary_legacy_values_collapse_to_a_bounded_vocabulary() -> None:
    labels = bounded_metric_labels(
        "neurostack_business_srm_decisions",
        flow_mode="patient@example.com",
    )
    assert labels == {"flow_mode": "unknown"}
