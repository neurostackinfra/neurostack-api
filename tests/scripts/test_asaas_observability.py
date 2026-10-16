"""Local observability verifier tests without starting containers."""

from __future__ import annotations

import hashlib
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "verify-asaas-observability.py"


def _module() -> object:
    spec = importlib.util.spec_from_file_location("verify_asaas_observability", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
