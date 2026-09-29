"""Contract tests for credential-change emission telemetry."""

from __future__ import annotations

from pathlib import Path

import yaml

from src.observability.metrics import API_CREDENTIALS_CHANGE_EMIT_FAILURES_TOTAL

ROOT = Path(__file__).parents[2]
PRIMARY_RULES = ROOT / "config" / "prometheus_rules.yml"
MIRROR_RULES = ROOT / "src" / "config" / "prometheus_rules.yml"


def _credential_change_alert() -> dict[str, object]:
    parsed = yaml.safe_load(PRIMARY_RULES.read_text(encoding="utf-8"))
    matches = [
        rule
        for group in parsed["groups"]
        for rule in group["rules"]
        if rule.get("alert") == "ApiCredentialsChangeEmissionFailures"
    ]
    assert len(matches) == 1
    return matches[0]


def test_credential_change_failure_counter_is_label_free_and_exact() -> None:
    assert (
        API_CREDENTIALS_CHANGE_EMIT_FAILURES_TOTAL._name
        == "api_credentials_change_emit_failures"
    )
    assert API_CREDENTIALS_CHANGE_EMIT_FAILURES_TOTAL._labelnames == ()


def test_credential_change_alert_is_exact_and_rules_are_identical() -> None:
    assert PRIMARY_RULES.read_bytes() == MIRROR_RULES.read_bytes()
    alert = _credential_change_alert()
    assert (
        alert["expr"] == "increase(api_credentials_change_emit_failures_total[15m]) > 0"
    )
    assert alert["for"] == "5m"
    assert alert["labels"] == {"severity": "warning", "owner": "platform-sre"}
    assert alert["annotations"]["runbook_url"] == (
        "https://docs.neurostack.io/runbooks/"
        "api-credentials-change-emission-failures"
    )
