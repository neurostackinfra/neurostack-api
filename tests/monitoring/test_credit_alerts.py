"""The Group 7 Prometheus rules remain inert until Group 8 mounts them."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_credit_alert_rules_are_separate_and_identity_free() -> None:
    rules = yaml.safe_load((ROOT / "config/prometheus_rules_credit.yml").read_text())
    expressions = [rule["expr"] for group in rules["groups"] for rule in group["rules"]]
    assert expressions == [
        "neurostack_credit_reconciliation_ready < 1"
        " and neurostack_credit_switch_effective == 1",
        "neurostack_credit_orphan_effects > 0",
        "increase(neurostack_credit_fact_rejected_total[15m]) > 0",
        "increase(neurostack_credit_observational_write_dropped_total[15m]) > 0",
        "increase(neurostack_credit_lifecycle_transitions_total"
        '{transition="suspended"}[15m]) > 0',
        "increase(neurostack_credit_retention_operations_total"
        '{outcome="refused"}[15m]) > 0',
    ]
    for expression in expressions:
        # An empty on() would match ANY process's switch: a switched-off
        # process would alert whenever another one is switched on.
        assert "on()" not in expression.replace(" ", "")
        for identity in ("tenant", "builder", "subscription"):
            assert identity not in expression
    assert (
        not (ROOT / "config/prometheus.yml")
        .read_text()
        .count("prometheus_rules_credit.yml")
    )
