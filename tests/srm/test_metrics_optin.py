"""Subprocess coverage for the process-global metrics ownership contract."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CORE_SOURCE = ROOT / "packages" / "neurostack-core"


def run_isolated(source: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(CORE_SOURCE)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def assert_isolated_ok(source: str) -> None:
    result = run_isolated(source)
    assert result.returncode == 0, result.stdout + result.stderr


def test_import_is_side_effect_free_and_default_first_is_idempotent() -> None:
    assert_isolated_ok("""
        from prometheus_client import REGISTRY
        before = set(REGISTRY._names_to_collectors)
        import neurostack_core as core
        assert set(REGISTRY._names_to_collectors) == before
        core.enable_metrics()
        after = set(REGISTRY._names_to_collectors)
        assert len(after - before) > 14
        core.enable_metrics()
        assert set(REGISTRY._names_to_collectors) == after
        """)


def test_custom_first_owns_and_different_registry_rejects() -> None:
    assert_isolated_ok("""
        from prometheus_client import CollectorRegistry
        import neurostack_core as core
        first = CollectorRegistry()
        second = CollectorRegistry()
        core.enable_metrics(first)
        core.enable_metrics(first)
        try:
            core.enable_metrics(second)
        except RuntimeError as exc:
            assert "another registry" in str(exc)
        else:
            raise AssertionError("different owner was accepted")
        assert not second._names_to_collectors
        """)


def test_default_then_custom_registry_rejects() -> None:
    assert_isolated_ok("""
        from prometheus_client import CollectorRegistry
        import neurostack_core as core
        core.enable_metrics()
        other = CollectorRegistry()
        try:
            core.enable_metrics(other)
        except RuntimeError as exc:
            assert "another registry" in str(exc)
        else:
            raise AssertionError("custom registry replaced the default owner")
        assert not other._names_to_collectors
        """)


def test_custom_then_default_registry_rejects() -> None:
    assert_isolated_ok("""
        from prometheus_client import CollectorRegistry, REGISTRY
        import neurostack_core as core
        custom = CollectorRegistry()
        before = dict(REGISTRY._names_to_collectors)
        core.enable_metrics(custom)
        try:
            core.enable_metrics()
        except RuntimeError as exc:
            assert "another registry" in str(exc)
        else:
            raise AssertionError("default registry replaced the custom owner")
        assert REGISTRY._names_to_collectors == before
        """)


def test_exact_fourteen_metric_family_types_and_labels() -> None:
    assert_isolated_ok("""
        import neurostack_core.metrics as metrics
        expected = {
            "neurostack_srm_decisions": ("counter", ("intent_category", "funnel_stage", "mode")),
            "neurostack_srm_latency_seconds": ("histogram", ()),
            "neurostack_srm_safety_triggers": ("counter", ("category",)),
            "neurostack_srm_handoffs": ("counter", ("reason",)),
            "neurostack_flow_transitions": ("counter", ("from_mode", "to_mode", "trigger")),
            "neurostack_flow_handler_duration_seconds": ("histogram", ("mode",)),
            "neurostack_flow_decisions": ("counter", ("mode", "action")),
            "neurostack_policy_evaluations": ("counter", ("result",)),
            "neurostack_policy_violations": ("counter", ("category", "severity", "action")),
            "neurostack_policy_latency_seconds": ("histogram", ()),
            "neurostack_prompt_generations": ("counter", ("mode",)),
            "neurostack_prompt_generation_seconds": ("histogram", ()),
            "neurostack_prompt_tokens": ("counter", ("mode",)),
            "neurostack_output_validations": ("counter", ("mode", "valid")),
        }
        actual = {
            instrument._name: (instrument._type, tuple(instrument._labelnames))
            for instrument in metrics._INSTRUMENTS
        }
        assert len(metrics._INSTRUMENTS) == 14
        assert actual == expected
        """)


@pytest.mark.parametrize(
    "collision_name",
    [
        "neurostack_srm_decisions_created",
        "neurostack_srm_latency_seconds_bucket",
        "neurostack_output_validations_total",
    ],
)
def test_any_family_or_generated_suffix_collision_is_atomic(
    collision_name: str,
) -> None:
    assert_isolated_ok(f"""
        from prometheus_client import CollectorRegistry, Gauge
        import neurostack_core as core
        registry = CollectorRegistry()
        Gauge({collision_name!r}, "occupied", registry=registry)
        before = dict(registry._names_to_collectors)
        try:
            core.enable_metrics(registry)
        except ValueError:
            pass
        else:
            raise AssertionError("collision was accepted")
        assert registry._names_to_collectors == before
        clean = CollectorRegistry()
        core.enable_metrics(clean)
        assert "neurostack_output_validations_total" in clean._names_to_collectors
        """)


def test_observations_made_before_enable_are_retained() -> None:
    assert_isolated_ok("""
        from prometheus_client import CollectorRegistry
        import neurostack_core as core
        core.SRM_HANDOFFS.labels(reason="requested").inc(3)
        core.FLOW_HANDLER_DURATION.labels(mode="normal").observe(0.25)
        registry = CollectorRegistry()
        core.enable_metrics(registry)
        values = {
            sample.name: sample.value
            for metric in registry.collect()
            for sample in metric.samples
            if sample.labels in ({"reason": "unknown"}, {"mode": "normal"})
        }
        assert values["neurostack_srm_handoffs_total"] == 3
        assert values["neurostack_flow_handler_duration_seconds_count"] == 1
        assert values["neurostack_flow_handler_duration_seconds_sum"] == 0.25
        """)


def test_public_core_labels_cannot_export_identity_values() -> None:
    assert_isolated_ok("""
        from prometheus_client import CollectorRegistry, generate_latest
        import neurostack_core as core

        rejected = (
            "requested",
            "patient@example.com",
            "52998224725",
            "request-12345678901234567890",
            "session-12345678901234567890",
            "trace-12345678901234567890",
            "aBc-VeryLongCapabilityToken_0123456789xyz",
        )
        for value in rejected:
            core.SRM_HANDOFFS.labels(reason=value).inc()
        registry = CollectorRegistry()
        core.enable_metrics(registry)
        scrape = generate_latest(registry).decode()
        assert 'reason="unknown"' in scrape
        for value in rejected:
            assert value not in scrape
        """)


def test_core_constructor_rejects_an_unknown_family() -> None:
    assert_isolated_ok("""
        import neurostack_core.metrics as metrics
        try:
            metrics.Counter(
                "neurostack_unknown_total",
                "unknown family",
                registry=None,
            )
        except ValueError as exc:
            assert "unknown core metric family" in str(exc)
        else:
            raise AssertionError("unknown core family was accepted")
        """)


def test_concurrent_first_call_has_one_owner_and_no_partial_registration() -> None:
    assert_isolated_ok("""
        from concurrent.futures import ThreadPoolExecutor
        from prometheus_client import CollectorRegistry
        import neurostack_core as core
        registries = (CollectorRegistry(), CollectorRegistry())
        def claim(registry):
            try:
                core.enable_metrics(registry)
                return "owned"
            except RuntimeError:
                return "rejected"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, registries))
        assert sorted(results) == ["owned", "rejected"]
        sizes = sorted(len(registry._names_to_collectors) for registry in registries)
        assert sizes[0] == 0
        assert sizes[1] > 14
        """)


def test_manual_reload_fails_closed_and_preserves_original_instruments() -> None:
    assert_isolated_ok("""
        import importlib
        from prometheus_client import CollectorRegistry
        import neurostack_core.metrics as metrics
        original = metrics.PROMPT_TOKENS_TOTAL
        original.labels(mode="normal").inc(2)
        try:
            importlib.reload(metrics)
        except RuntimeError as exc:
            assert "cannot be reloaded" in str(exc)
        else:
            raise AssertionError("manual reload was accepted")
        assert metrics.PROMPT_TOKENS_TOTAL is original
        registry = CollectorRegistry()
        metrics.enable_metrics(registry)
        sample = registry.get_sample_value(
            "neurostack_prompt_tokens_total", {"mode": "normal"}
        )
        assert sample == 2
        """)
