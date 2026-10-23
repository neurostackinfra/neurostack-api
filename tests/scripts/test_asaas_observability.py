"""Local observability verifier tests without starting containers."""

from __future__ import annotations

import hashlib
import importlib.util
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify-asaas-observability.py"
RULES = ROOT / "config" / "prometheus_rules.yml"
METRICS = ROOT / "src" / "observability" / "metrics.py"
_LABEL_SCAN_FAILURE = "billing metric labels may contain an identity or secret"
# The billing control-plane instruments the identity scan must cover, in
# metrics.py order; the first one is the scan anchor.
_SCANNED_INSTRUMENTS = (
    "BILLING_OPERATION_RECEIPTS_TOTAL",
    "BILLING_TOPUP_CAPABILITY_TRANSITIONS_TOTAL",
    "BILLING_FACT_BACKLOG",
    "BILLING_FACT_PUBLISH_ATTEMPTS_TOTAL",
    "BILLING_FACT_WORKER_TICKS_TOTAL",
    "BILLING_FACT_WORKER_LAST_TICK",
)
# The Group 3 / 6b instruments defined just before the scanned block.
_UNSCANNED_INSTRUMENTS = (
    "BILLING_WEBHOOK_DELIVERIES_TOTAL",
    "BILLING_READBACK_BACKLOG",
    "BILLING_SUMMARY_CHECKOUT_UNAVAILABLE_TOTAL",
)


def _module() -> Any:
    spec = importlib.util.spec_from_file_location("verify_asaas_observability", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _local_verify(module: Any, tmp_path: Path) -> list[str]:
    """Run the offline contract against whatever RULES/METRICS now point at."""

    smoke = tmp_path / "smoke.json"
    smoke.write_text('{"result":"local-only"}\n', encoding="utf-8")
    errors: list[str] = module.verify(
        rules_digest=hashlib.sha256(module.RULES.read_bytes()).hexdigest(),
        smoke_file=smoke,
        smoke_digest=hashlib.sha256(smoke.read_bytes()).hexdigest(),
    )
    return errors


def _with_forbidden_label(metrics_text: str, symbol: str, word: str) -> str:
    """Add a label list carrying ``word`` to one instrument definition."""

    start = metrics_text.index(f"\n{symbol} = ")
    end = metrics_text.index("\n)\n", start)
    return metrics_text[:end] + f'\n    ["{word}"],' + metrics_text[end:]


def test_required_alerts_and_metrics_are_the_live_billing_surface() -> None:
    module = _module()

    assert module.REQUIRED_ALERTS == {
        "BillingReadbackBacklog",
        "BillingOperationReceiptRejected",
        "BillingTopupCapabilityRevoked",
        "BillingFactDeliveryStale",
        "BillingFactDeliveryBlocked",
        "BillingFactWorkerStalled",
    }
    assert module.REQUIRED_METRICS == {
        "neurostack_billing_readback_backlog",
        "neurostack_billing_operation_receipts_total",
        "neurostack_billing_topup_capability_transitions_total",
        "neurostack_billing_fact_backlog",
        "neurostack_billing_fact_worker_last_tick_timestamp_seconds",
    }
    # Every family a required alert reads is itself required, so the parity
    # check fails before a required alert can go dead.
    rules = yaml.safe_load(RULES.read_text(encoding="utf-8"))
    expressions = {
        rule["alert"]: rule["expr"]
        for group in rules["groups"]
        for rule in group["rules"]
        if rule.get("alert") in module.REQUIRED_ALERTS
    }
    assert set(expressions) == module.REQUIRED_ALERTS
    read = {
        family
        for expression in expressions.values()
        for family in re.findall(r"\bneurostack_[a-z0-9_]+", expression)
    }
    assert read == module.REQUIRED_METRICS


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("alert: BillingReadbackBacklog", "alert: BillingMigrationReadbackBacklog"),
        ("alert: BillingFactWorkerStalled", "alert: BillingFactWorkerStall"),
    ],
)
def test_a_missing_required_alert_fails_rule_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old: str, new: str
) -> None:
    module = _module()
    text = RULES.read_text(encoding="utf-8")
    assert text.count(old) == 1
    mutated = tmp_path / "prometheus_rules.yml"
    mutated.write_text(text.replace(old, new), encoding="utf-8")
    monkeypatch.setattr(module, "RULES", mutated)

    assert "billing alert rule parity is incomplete" in _local_verify(module, tmp_path)


def test_label_scan_reads_exactly_the_control_plane_block() -> None:
    module = _module()
    text = METRICS.read_text(encoding="utf-8")
    section = module._billing_label_scan_section(text)

    assert text.count(module.BILLING_LABEL_SCAN_ANCHOR) == 1
    assert section is not None
    assert section.startswith(f"{_SCANNED_INSTRUMENTS[0]} = Counter(")
    assert section == text[text.index(f"\n{_SCANNED_INSTRUMENTS[0]} = ") + 1 :]
    for symbol in _SCANNED_INSTRUMENTS:
        assert re.search(rf"^{symbol} = ", section, re.M), symbol
    for symbol in _UNSCANNED_INSTRUMENTS:
        assert re.search(rf"^{symbol} = ", text, re.M), symbol
        assert symbol not in section, symbol


@pytest.mark.parametrize("symbol", _SCANNED_INSTRUMENTS)
def test_label_scan_rejects_a_forbidden_label_on_every_scanned_instrument(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, symbol: str
) -> None:
    module = _module()
    mutated = tmp_path / "metrics.py"
    mutated.write_text(
        _with_forbidden_label(METRICS.read_text(encoding="utf-8"), symbol, "provider"),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "METRICS", mutated)

    assert _LABEL_SCAN_FAILURE in _local_verify(module, tmp_path)


@pytest.mark.parametrize(
    "word",
    ["actor", "customer", "email", "id", "provider", "receipt", "secret"],
)
def test_label_scan_rejects_every_forbidden_word_at_the_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, word: str
) -> None:
    module = _module()
    assert word in module.FORBIDDEN_LABEL_WORDS
    mutated = tmp_path / "metrics.py"
    mutated.write_text(
        _with_forbidden_label(
            METRICS.read_text(encoding="utf-8"), _SCANNED_INSTRUMENTS[0], word
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "METRICS", mutated)

    assert _LABEL_SCAN_FAILURE in _local_verify(module, tmp_path)


def test_label_scan_fails_closed_when_its_anchor_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    text = METRICS.read_text(encoding="utf-8")
    mutated = tmp_path / "metrics.py"
    mutated.write_text(
        text.replace(
            "BILLING_OPERATION_RECEIPTS_TOTAL = Counter(",
            "BILLING_OPERATION_RECEIPT_COUNTS = Counter(",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "METRICS", mutated)

    errors = _local_verify(module, tmp_path)
    assert "billing metric label scan anchor is missing" in errors
    assert _LABEL_SCAN_FAILURE not in errors


def test_local_observability_verifier_accepts_bound_rules_and_smoke(
    tmp_path: Path,
) -> None:
    module = _module()
    smoke = tmp_path / "smoke.json"
    smoke.write_text('{"result":"local-only"}\n', encoding="utf-8")
    rules = ROOT / "config" / "prometheus_rules.yml"

    errors = module.verify(
        rules_digest=hashlib.sha256(rules.read_bytes()).hexdigest(),
        smoke_file=smoke,
        smoke_digest=hashlib.sha256(smoke.read_bytes()).hexdigest(),
    )

    assert errors == []


def test_required_smoke_uses_fresh_runtime_challenge_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    smoke = tmp_path / "smoke.json"
    smoke.write_text(
        '{"scope":"billing_observability","version":1}\n', encoding="utf-8"
    )
    rules = ROOT / "config" / "prometheus_rules.yml"
    receiver = "https://receiver.example.test/v1/billing/default"
    monkeypatch.setenv("BILLING_ALERT_RECEIVER_URL", receiver.removesuffix("/default"))
    monkeypatch.setenv(
        "BILLING_PROMETHEUS_RULES_DIGEST",
        hashlib.sha256(rules.read_bytes()).hexdigest(),
    )

    def _test_only_fake_receiver_challenge(
        *, receiver_url: str, rules_digest: str, smoke_digest: str
    ) -> dict[str, object]:
        acknowledgement = {
            "acknowledged": True,
            "nonce": "fresh-runtime-nonce",
            "receiver_url": receiver_url,
            "rules_digest": rules_digest,
            "smoke_digest": smoke_digest,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        acknowledgement["signature"] = module._acknowledgement_signature(
            acknowledgement, "test-only-token"
        )
        return acknowledgement

    monkeypatch.setenv("BILLING_OBSERVABILITY_SMOKE_TOKEN", "test-only-token")
    monkeypatch.setattr(
        module, "_production_runtime_challenge", _test_only_fake_receiver_challenge
    )
    errors = module.verify(
        rules_digest=hashlib.sha256(rules.read_bytes()).hexdigest(),
        smoke_file=smoke,
        smoke_digest=hashlib.sha256(smoke.read_bytes()).hexdigest(),
        require_non_loopback=True,
        verify_mounted_rule_digest=True,
        production_required=True,
    )
    assert errors == []


def test_required_smoke_rejects_static_ack_without_runtime_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    smoke = tmp_path / "smoke.json"
    smoke.write_text(
        '{"acknowledgement":{"readback":true},"scope":"billing_observability",'
        '"version":1}\n',
        encoding="utf-8",
    )
    rules = ROOT / "config" / "prometheus_rules.yml"
    monkeypatch.setenv(
        "BILLING_ALERT_RECEIVER_URL", "https://receiver.example.test/v1/billing"
    )

    def _test_only_fake_receiver_challenge(**_kwargs: object) -> dict[str, object]:
        raise RuntimeError("runtime smoke challenge failed")

    monkeypatch.setattr(
        module,
        "_production_runtime_challenge",
        _test_only_fake_receiver_challenge,
    )
    monkeypatch.setenv("BILLING_OBSERVABILITY_SMOKE_TOKEN", "test-only-token")
    errors = module.verify(
        rules_digest=hashlib.sha256(rules.read_bytes()).hexdigest(),
        smoke_file=smoke,
        smoke_digest=hashlib.sha256(smoke.read_bytes()).hexdigest(),
        require_non_loopback=True,
        production_required=True,
    )
    assert "runtime smoke challenge failed" in errors
    assert any("fresh bound receiver" in error for error in errors)


def test_production_required_smoke_rejects_missing_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    smoke = tmp_path / "smoke.json"
    smoke.write_text(
        '{"scope":"billing_observability","version":1}\n', encoding="utf-8"
    )
    rules = ROOT / "config" / "prometheus_rules.yml"
    monkeypatch.setenv(
        "BILLING_ALERT_RECEIVER_URL", "https://receiver.example.test/v1/billing"
    )
    monkeypatch.delenv("BILLING_OBSERVABILITY_SMOKE_TOKEN", raising=False)
    errors = module.verify(
        rules_digest=hashlib.sha256(rules.read_bytes()).hexdigest(),
        smoke_file=smoke,
        smoke_digest=hashlib.sha256(smoke.read_bytes()).hexdigest(),
        production_required=True,
    )
    assert "observability smoke authentication is unavailable" in errors
    assert any("fresh bound receiver" in error for error in errors)
