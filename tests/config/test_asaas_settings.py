"""Protected ASAAS and production billing configuration tests."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr, ValidationError

from src.config.settings import (
    ASAAS_API_BASE_URL,
    ASAAS_AUTH_HEADER,
    ASAAS_DEFAULT_USER_AGENT,
    Settings,
    _runtime_acknowledgement_signed_bytes,
    runtime_acknowledgement_challenge_digest,
)
from src.core.exceptions import ConfigurationError

ROOT = Path(__file__).resolve().parents[2]


def _production_settings(**overrides: object) -> Settings:
    observed_at = datetime.now(timezone.utc).isoformat()
    challenge_nonce = "settings-test-challenge"
    rules_digest = hashlib.sha256(
        (ROOT / "config/prometheus_rules.yml").read_bytes()
    ).hexdigest()
    smoke_digest = hashlib.sha256(
        (ROOT / "config/billing-observability-smoke.json").read_bytes()
    ).hexdigest()
    challenge_digest = runtime_acknowledgement_challenge_digest(
        nonce=challenge_nonce,
        receiver_url="https://alerts.neurostack.example/v1/billing",
        rules_digest=rules_digest,
        smoke_digest=smoke_digest,
    )
    acknowledgement = {
        "acknowledged": True,
        "challenge_digest": challenge_digest,
        "nonce": "settings-test-nonce",
        "observed_at": observed_at,
        "receipt_id": "settings-receipt-1",
        "receiver_url": "https://alerts.neurostack.example/v1/billing",
        "rules_digest": rules_digest,
        "smoke_digest": smoke_digest,
    }
    acknowledgement["nonce"] = challenge_nonce
    private_key = Ed25519PrivateKey.from_private_bytes(bytes(range(1, 33)))
    acknowledgement["signature"] = private_key.sign(
        _runtime_acknowledgement_signed_bytes(acknowledgement)
    ).hex()
    acknowledgement_raw = json.dumps(
        acknowledgement, sort_keys=True, separators=(",", ":")
    )
    values: dict[str, object] = {
        "environment": "production",
        "secret_key": "production-secret-key-at-least-32-characters",
        "database_url": "postgresql+asyncpg://app:password@postgres/app",
        "redis_url": "redis://redis:6379/0",
        "allowed_origins": ["https://app.neurostack.example"],
        "allowed_hosts": ["api.neurostack.example"],
        "billing_operation_receipt_signing_key": "k" * 32,
        "billing_observability_smoke_token": "smoke-token",
        "billing_prometheus_rules_digest": rules_digest,
        "billing_observability_smoke_digest": smoke_digest,
        "billing_alert_receiver_url": "https://alerts.neurostack.example/v1/billing",
        "billing_observability_acknowledgement": acknowledgement_raw,
        "billing_observability_acknowledgement_digest": hashlib.sha256(
            acknowledgement_raw.encode("utf-8")
        ).hexdigest(),
        "billing_observability_challenge_nonce": challenge_nonce,
        "billing_observability_challenge_digest": challenge_digest,
        "billing_observability_acknowledgement_public_key": private_key.public_key()
        .public_bytes_raw()
        .hex(),
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def test_asaas_endpoint_and_auth_header_are_not_deployment_overridable() -> None:
    configured = Settings(
        _env_file=None,
        asaas_api_key="$aact_prod_fixture",
        asaas_webhook_token="fixture-webhook-token",
    )

    assert ASAAS_API_BASE_URL == "https://api.asaas.com/v3"
    assert ASAAS_AUTH_HEADER == "access_token"
    assert configured.asaas_user_agent == ASAAS_DEFAULT_USER_AGENT
    assert configured.is_asaas_configuration_compatible is True
    assert "asaas_base_url" not in Settings.model_fields


@pytest.mark.parametrize("api_key", ["aact_prod_wrong", "$aact_hml_fixture"])
def test_non_production_namespace_key_is_rejected_without_disclosure(
    api_key: str,
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, asaas_api_key=api_key)

    assert api_key not in str(exc_info.value)
    assert "production key namespace" in str(exc_info.value)


def test_secrets_are_masked_from_settings_repr_and_dump() -> None:
    api_key = "$aact_prod_fixture-never-render"
    webhook_token = "webhook-fixture-never-render"
    configured = Settings(
        _env_file=None,
        asaas_api_key=api_key,
        asaas_webhook_token=webhook_token,
    )

    rendered = repr(configured)
    dumped = repr(configured.model_dump())
    assert api_key not in rendered
    assert api_key not in dumped
    assert webhook_token not in rendered
    assert webhook_token not in dumped
    assert isinstance(configured.asaas_api_key, SecretStr)


def test_absent_or_incomplete_asaas_configuration_is_incompatible() -> None:
    assert Settings(_env_file=None).is_asaas_configuration_compatible is False
    assert (
        Settings(
            _env_file=None,
            asaas_api_key="$aact_prod_fixture",
        ).is_asaas_configuration_compatible
        is False
    )


def test_settings_expose_no_retired_legacy_gateway_fields() -> None:
    legacy_fields = {
        name for name in Settings.model_fields if "abacate" in name.lower()
    }

    assert legacy_fields == set()


def test_production_startup_accepts_complete_asaas_billing_configuration() -> None:
    configured = _production_settings()

    assert configured.is_production is True
    assert configured.is_billing_observability_compatible is True


def test_production_startup_ignores_stale_legacy_gateway_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A task definition that still injects the retired variables must neither
    # block startup nor surface their values anywhere in the settings object.
    monkeypatch.setenv("ABACATEPAY_API_KEY", "stale-legacy-api-key")
    monkeypatch.setenv("ABACATEPAY_WEBHOOK_SECRET", "stale-legacy-secret")

    configured = _production_settings()

    assert configured.is_production is True
    rendered = repr(configured) + repr(configured.model_dump())
    assert "stale-legacy-api-key" not in rendered
    assert "stale-legacy-secret" not in rendered


def test_production_startup_error_never_mentions_retired_legacy_gateway() -> None:
    with pytest.raises(ConfigurationError) as exc_info:
        _production_settings(debug=True)

    message = str(exc_info.value)
    assert "DEBUG must be False in production" in message
    assert "ABACATEPAY" not in message.upper()
    assert "production-secret-key-at-least-32-characters" not in message
    assert "password" not in message


@pytest.mark.parametrize(
    "overrides",
    [
        {"billing_alert_receiver_url": "http://127.0.0.1:9093/webhook"},
        {"billing_prometheus_rules_digest": "not-a-digest"},
        {"billing_observability_smoke_digest": ""},
        {"billing_observability_smoke_digest": "a" * 64},
    ],
)
def test_production_billing_observability_fails_closed(
    overrides: dict[str, object],
) -> None:
    with pytest.raises((ConfigurationError, ValidationError)):
        _production_settings(**overrides)


@pytest.mark.parametrize(
    "keys",
    [
        (
            "arn:aws:kms:us-east-1:123456789012:key/grant",
            "arn:aws:kms:us-east-1:123456789012:key/build",
            "arn:aws:kms:us-east-1:123456789012:key/build",
        ),
        (
            "arn:aws:kms:us-east-1:123456789012:key/grant",
            "arn:aws:kms:us-east-1:123456789012:key/build",
            "",
        ),
        ("grant", "build", "receipt"),
        (
            "arn:aws:kms:us-east-1:123456789012:key/alias/grant",
            "arn:aws:kms:us-east-1:123456789012:key/build",
            "arn:aws:kms:us-east-1:123456789012:key/receipt",
        ),
    ],
)
def test_producer_kms_key_arns_must_be_complete_distinct_key_arns(
    keys: tuple[str, str, str],
) -> None:
    with pytest.raises(ValidationError, match="producer grant, build and receipt"):
        Settings(
            _env_file=None,
            billing_producer_grant_kid=keys[0],
            billing_producer_build_kid=keys[1],
            billing_producer_receipt_kid=keys[2],
        )


def test_producer_kms_key_arns_accept_three_distinct_keys() -> None:
    configured = Settings(
        _env_file=None,
        billing_producer_grant_kid="arn:aws:kms:us-east-1:123456789012:key/grant",
        billing_producer_build_kid="arn:aws:kms:us-east-1:123456789012:key/build",
        billing_producer_receipt_kid="arn:aws:kms:us-east-1:123456789012:key/receipt",
    )

    assert (
        len(
            {
                configured.billing_producer_grant_kid,
                configured.billing_producer_build_kid,
                configured.billing_producer_receipt_kid,
            }
        )
        == 3
    )


def _staging_settings(secret_key: str) -> Settings:
    return Settings(_env_file=None, environment="staging", secret_key=secret_key)


def test_staging_rejects_default_signing_key_without_disclosure() -> None:
    default_key = "dev-secret-key-change-in-production-min-32-chars-required"

    with pytest.raises(ValidationError) as exc_info:
        Settings(_env_file=None, environment="staging", secret_key=default_key)

    assert default_key not in str(exc_info.value)
    assert "changed from the default value in staging" in str(exc_info.value)


def test_staging_runtime_requires_secret_key_injection() -> None:
    configured_key = "configured-staging-signing-key-at-least-32"
    configured = _staging_settings(configured_key)

    with pytest.raises(ConfigurationError) as exc_info:
        configured.validate_runtime_environment({"ENVIRONMENT": "staging"})

    assert "SECRET_KEY" in str(exc_info.value)
    assert configured_key not in str(exc_info.value)


def test_staging_runtime_rejects_noncanonical_api_prefix() -> None:
    signing_key = "configured-staging-signing-key-at-least-32"
    configured = Settings(
        _env_file=None,
        environment="staging",
        secret_key=signing_key,
        api_prefix="/internal",
    )

    with pytest.raises(ConfigurationError, match="canonical prefix is /v1"):
        configured.validate_runtime_environment(
            {"ENVIRONMENT": "staging", "SECRET_KEY": signing_key}
        )


@pytest.mark.parametrize(
    ("runtime_environment", "runtime_secret_key"),
    [
        ("staging", "dev-secret-key-change-in-production-min-32-chars-required"),
        ("staging", "short-staging-key"),
        ("production", "injected-staging-signing-key-at-least-32-characters"),
        ("staging", "different-staging-signing-key-at-least-32-characters"),
    ],
)
def test_staging_runtime_rejects_bad_or_mismatched_secret_without_disclosure(
    runtime_environment: str,
    runtime_secret_key: str,
) -> None:
    configured_key = "configured-staging-signing-key-at-least-32"
    configured = _staging_settings(configured_key)

    with pytest.raises(ConfigurationError) as exc_info:
        configured.validate_runtime_environment(
            {
                "ENVIRONMENT": runtime_environment,
                "SECRET_KEY": runtime_secret_key,
            }
        )

    rendered_error = str(exc_info.value)
    assert runtime_secret_key not in rendered_error
    assert configured_key not in rendered_error
    assert "Invalid staging SECRET_KEY runtime injection" in rendered_error


def test_staging_runtime_accepts_valid_injected_signing_key() -> None:
    signing_key = "injected-staging-signing-key-at-least-32-characters"
    configured = _staging_settings(signing_key)

    configured.validate_runtime_environment(
        {"ENVIRONMENT": "staging", "SECRET_KEY": signing_key}
    )
