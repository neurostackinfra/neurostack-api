#!/usr/bin/env python3
"""Verify the local billing observability contract without contacting a service."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timezone
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import yaml  # type: ignore[import-untyped]  # Runtime dependency; stubs are not packaged.

ROOT = Path(__file__).resolve().parents[1]
RULES = ROOT / "config" / "prometheus_rules.yml"
ALERTMANAGER = ROOT / "config" / "alertmanager" / "alertmanager.yml"
PROMETHEUS = ROOT / "config" / "prometheus.yml"
METRICS = ROOT / "src" / "observability" / "metrics.py"
REQUIRED_ALERTS = {
    "BillingMigrationManualIntervention",
    "BillingMigrationReadbackBacklog",
    "BillingOperationReceiptRejected",
    "BillingTopupCapabilityRevoked",
    "BillingFactDeliveryStale",
    "BillingFactDeliveryBlocked",
    "BillingFactWorkerStalled",
}
REQUIRED_METRICS = {
    "neurostack_billing_migration_operations_total",
    "neurostack_billing_migration_backlog",
    "neurostack_billing_operation_receipts_total",
    "neurostack_billing_topup_capability_transitions_total",
    "neurostack_billing_fact_backlog",
    "neurostack_billing_fact_worker_last_tick_timestamp_seconds",
}
FORBIDDEN_LABEL_WORDS = {
    "actor",
    "customer",
    "email",
    "id",
    "provider",
    "receipt",
    "secret",
}
_FORBIDDEN_ACK_KEYS = {
    "address",
    "customer",
    "document",
    "email",
    "name",
    "phone",
    "secret",
    "token",
}


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _non_loopback_https(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port is not None
        and not 1 <= port <= 65535
    ):
        return False
    if parsed.hostname.lower() == "localhost":
        return False
    try:
        return not ip_address(parsed.hostname).is_loopback
    except ValueError:
        return True


def _receiver_urls(alertmanager: object, receiver_base: str) -> list[str]:
    """Expand the runtime receiver template into concrete URLs."""

    receivers = (
        alertmanager.get("receivers", []) if isinstance(alertmanager, dict) else []
    )
    urls: list[str] = []
    for receiver in receivers:
        if not isinstance(receiver, dict):
            continue
        for config in receiver.get("webhook_configs", []):
            if not isinstance(config, dict) or not isinstance(config.get("url"), str):
                continue
            url = config["url"]
            if url.startswith("${BILLING_ALERT_RECEIVER_URL}"):
                if not receiver_base:
                    urls.append(url)
                else:
                    urls.append(
                        receiver_base.rstrip("/")
                        + url[len("${BILLING_ALERT_RECEIVER_URL}") :]
                    )
            else:
                urls.append(url)
    return urls


def _contains_forbidden_ack_key(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            not isinstance(key, str)
            or key.lower() in _FORBIDDEN_ACK_KEYS
            or _contains_forbidden_ack_key(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_ack_key(item) for item in value)
    return False


def _acknowledgement_signature(
    acknowledgement: dict[str, object], token: str
) -> str:
    """Authenticate the receiver acknowledgement without storing the token."""

    payload = {
        "acknowledged": acknowledgement.get("acknowledged"),
        "nonce": acknowledgement.get("nonce"),
        "observed_at": acknowledgement.get("observed_at"),
        "receiver_url": acknowledgement.get("receiver_url"),
        "rules_digest": acknowledgement.get("rules_digest"),
        "smoke_digest": acknowledgement.get("smoke_digest"),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )
    return hmac.new(token.encode("ascii"), encoded, hashlib.sha256).hexdigest()


def _production_runtime_challenge(
    *, receiver_url: str, rules_digest: str, smoke_digest: str
) -> dict[str, object]:
    """Perform the required authenticated smoke against the configured receiver."""

    token = os.environ.get("BILLING_OBSERVABILITY_SMOKE_TOKEN", "")
    if (
        not token
        or not token.isascii()
        or any(character.isspace() for character in token)
    ):
        raise RuntimeError("observability smoke authentication is unavailable")
    issued_at = datetime.now(timezone.utc).replace(microsecond=0)
    nonce = secrets.token_hex(16)
    challenge = {
        "issued_at": issued_at.isoformat(),
        "nonce": nonce,
        "receiver_url": receiver_url,
        "rules_digest": rules_digest,
        "smoke_digest": smoke_digest,
        "scope": "billing_observability_smoke.v1",
    }
    encoded = json.dumps(challenge, sort_keys=True, separators=(",", ":")).encode(
        "ascii"
    )
    signature = hmac.new(token.encode("ascii"), encoded, hashlib.sha256).hexdigest()
    request = Request(
        receiver_url,
        data=encoded,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Billing-Observability-Nonce": nonce,
            "X-Billing-Observability-Signature": signature,
        },
    )
    try:
        with urlopen(request, timeout=5) as response:  # nosec B310 - URL is validated
            if response.status != 200:
                raise RuntimeError("observability receiver rejected smoke")
            body = response.read(16_384)
    except Exception as exc:
        raise RuntimeError("observability receiver smoke failed") from exc
    try:
        acknowledgement = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("observability receiver acknowledgement is invalid") from exc
    if _contains_forbidden_ack_key(acknowledgement):
        raise RuntimeError("observability acknowledgement contains forbidden data")
    if not isinstance(acknowledgement, dict):
        raise RuntimeError("observability acknowledgement is invalid")
    if (
        acknowledgement.get("nonce") != nonce
        or acknowledgement.get("receiver_url") != receiver_url
        or acknowledgement.get("rules_digest") != rules_digest
        or acknowledgement.get("smoke_digest") != smoke_digest
        or acknowledgement.get("acknowledged") is not True
        or acknowledgement.get("signature")
        != _acknowledgement_signature(acknowledgement, token)
    ):
        raise RuntimeError("observability acknowledgement is not challenge-bound")
    return acknowledgement


def verify(
    *,
    rules_digest: str,
    smoke_file: Path,
    smoke_digest: str,
    require_non_loopback: bool = False,
    verify_mounted_rule_digest: bool = False,
    require_acknowledged_smoke: bool = False,
    production_required: bool = False,
    max_ack_age_seconds: int = 300,
) -> list[str]:
    """Return all local contract failures without printing source contents."""

    errors: list[str] = []
    if max_ack_age_seconds <= 0:
        errors.append("runtime smoke acknowledgement age is invalid")
    if _digest(RULES) != rules_digest:
        errors.append("rules digest does not bind config/prometheus_rules.yml")
    if verify_mounted_rule_digest:
        mounted_digest = os.environ.get("BILLING_PROMETHEUS_RULES_DIGEST")
        if mounted_digest is None or mounted_digest != _digest(RULES):
            errors.append("mounted Prometheus rules digest is invalid")
    if _digest(smoke_file) != smoke_digest:
        errors.append("smoke digest does not bind the supplied smoke artifact")
    try:
        rules = yaml.safe_load(RULES.read_text(encoding="utf-8"))
        alertmanager = yaml.safe_load(ALERTMANAGER.read_text(encoding="utf-8"))
        prometheus = yaml.safe_load(PROMETHEUS.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return errors + ["observability YAML is unreadable"]
    groups = rules.get("groups", []) if isinstance(rules, dict) else []
    alert_names = {
        rule.get("alert")
        for group in groups
        if isinstance(group, dict)
        for rule in group.get("rules", [])
        if isinstance(rule, dict)
    }
    if not REQUIRED_ALERTS <= alert_names:
        errors.append("billing alert rule parity is incomplete")
    external = prometheus.get("global", {}).get("external_labels", {})
    if not isinstance(external, dict) or external.get("environment") != "production":
        errors.append("Prometheus production external label is invalid")
    production_required = production_required or require_acknowledged_smoke
    receiver_base = os.environ.get("BILLING_ALERT_RECEIVER_URL", "")
    urls = _receiver_urls(alertmanager, receiver_base) if receiver_base else []
    if receiver_base and (
        not urls or not all(_non_loopback_https(url) for url in urls)
    ):
        errors.append("Alertmanager receiver must be non-loopback HTTPS")
    if (require_non_loopback or production_required) and not urls:
        errors.append("Alertmanager receiver is not configured")
    if production_required:
        runtime_receiver = urls[0] if urls and _non_loopback_https(urls[0]) else ""
        try:
            acknowledgement = (
                _production_runtime_challenge(
                    receiver_url=runtime_receiver,
                    rules_digest=_digest(RULES),
                    smoke_digest=smoke_digest,
                )
                if runtime_receiver
                else {}
            )
        except RuntimeError as exc:
            errors.append(str(exc))
            acknowledgement = {}
        try:
            smoke = yaml.safe_load(smoke_file.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            smoke = None
        static_acknowledgement = (
            smoke.get("acknowledgement") if isinstance(smoke, dict) else None
        )
        timestamp = (
            acknowledgement.get("observed_at")
            if isinstance(acknowledgement, dict)
            else None
        )
        receiver = (
            acknowledgement.get("receiver_url")
            if isinstance(acknowledgement, dict)
            else None
        )
        try:
            parsed_timestamp = datetime.fromisoformat(
                str(timestamp).replace("Z", "+00:00")
            )
        except ValueError:
            parsed_timestamp = None
        if (
            not isinstance(smoke, dict)
            or smoke.get("scope") != "billing_observability"
            or smoke.get("version") != 1
            or not isinstance(receiver, str)
            or receiver != runtime_receiver
            or not _non_loopback_https(receiver)
            or acknowledgement.get("acknowledged") is not True
            or acknowledgement.get("nonce") is None
            or not isinstance(acknowledgement.get("signature"), str)
            or acknowledgement.get("signature")
            != _acknowledgement_signature(
                acknowledgement,
                os.environ.get("BILLING_OBSERVABILITY_SMOKE_TOKEN", ""),
            )
            or acknowledgement.get("rules_digest") != _digest(RULES)
            or acknowledgement.get("smoke_digest") != smoke_digest
            or parsed_timestamp is None
            or parsed_timestamp.tzinfo is None
            or parsed_timestamp > datetime.now(timezone.utc)
            or (datetime.now(timezone.utc) - parsed_timestamp).total_seconds()
            > max_ack_age_seconds
        ):
            errors.append("runtime smoke lacks a fresh bound receiver acknowledgement")
        if static_acknowledgement is not None and _contains_forbidden_ack_key(
            static_acknowledgement
        ):
            errors.append("smoke artifact contains forbidden data")
    metrics_text = METRICS.read_text(encoding="utf-8")
    if not all(metric in metrics_text for metric in REQUIRED_METRICS):
        errors.append("billing Prometheus metric parity is incomplete")
    billing_section = metrics_text.split("BILLING_MIGRATION_OPERATIONS_TOTAL", 1)[-1]
    if any(f'"{word}"' in billing_section.lower() for word in FORBIDDEN_LABEL_WORDS):
        errors.append("billing metric labels may contain an identity or secret")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rules-digest",
        default=os.environ.get("BILLING_PROMETHEUS_RULES_DIGEST", _digest(RULES)),
    )
    parser.add_argument(
        "--smoke-file",
        type=Path,
        default=Path(
            os.environ.get(
                "BILLING_OBSERVABILITY_SMOKE_PATH",
                str(ROOT / "config/billing-observability-smoke.json"),
            )
        ),
    )
    parser.add_argument(
        "--smoke-digest",
        default=os.environ.get(
            "BILLING_OBSERVABILITY_SMOKE_DIGEST",
            _digest(
                Path(
                    os.environ.get(
                        "BILLING_OBSERVABILITY_SMOKE_PATH",
                        str(ROOT / "config/billing-observability-smoke.json"),
                    )
                )
            ),
        ),
    )
    parser.add_argument("--require-non-loopback", action="store_true")
    parser.add_argument("--verify-mounted-rule-digest", action="store_true")
    parser.add_argument("--require-acknowledged-smoke", action="store_true")
    parser.add_argument(
        "--production-required",
        action="store_true",
        help="Require a fresh, nonce-bound acknowledgement from the configured receiver",
    )
    parser.add_argument("--max-ack-age-seconds", type=int, default=300)
    args = parser.parse_args()
    errors = verify(
        rules_digest=args.rules_digest,
        smoke_file=args.smoke_file,
        smoke_digest=args.smoke_digest,
        require_non_loopback=args.require_non_loopback,
        verify_mounted_rule_digest=args.verify_mounted_rule_digest,
        require_acknowledged_smoke=args.require_acknowledged_smoke,
        production_required=args.production_required,
        max_ack_age_seconds=args.max_ack_age_seconds,
    )
    if errors:
        for error in errors:
            print(error)
        return 1
    print("asaas billing observability verification passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
