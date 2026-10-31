"""The Group 7 Prometheus rules remain inert until Group 8 mounts them."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_credit_alert_rules_are_separate_and_identity_free() -> None:
    rules = yaml.safe_load((ROOT / "config/prometheus_rules_credit.yml").read_text())
    expressions = [rule["expr"] for group in rules["groups"] for rule in group["rules"]]
    assert expressions == [
        "neurostack_credit_reconciliation_ready < 1",
        "neurostack_credit_orphan_effects > 0",
    ]
    assert (
        not (ROOT / "config/prometheus.yml")
        .read_text()
        .count("prometheus_rules_credit.yml")
    )
