"""Configuration contracts for the localhost AppConfig Agent cache."""

from typing import Any

import pytest
from pydantic import ValidationError

from src.config.settings import Settings
from src.core.exceptions import ConfigurationError


def _production_settings(**overrides: Any) -> Settings:
    # A complete production configuration includes the Group 5 billing
    # control-plane secrets and observability evidence; reuse the validated
    # production fixture so AppConfig is the only variable under test here.
    from tests.config.test_asaas_settings import (
        _production_settings as complete_production_settings,
    )

    values: dict[str, Any] = {
        "secret_key": "production-secret-key-with-at-least-32-characters",
        "database_url": "postgresql+asyncpg://runtime-user:runtime-pass@database/api",
        "allowed_origins": ["https://app.neurostack.example"],
        "allowed_hosts": ["api.neurostack.example"],
    }
    values.update(overrides)
    return complete_production_settings(**values)


def test_appconfig_is_disabled_by_default() -> None:
    configured = Settings(_env_file=None)

    assert configured.appconfig_enabled is False
    assert configured.appconfig_origin == "http://127.0.0.1:2772"
    assert configured.appconfig_application == ""
    assert configured.appconfig_environment == ""
    assert configured.appconfig_profile == ""
    assert configured.appconfig_token.get_secret_value() == ""


def test_production_accepts_complete_loopback_agent_configuration() -> None:
    configured = _production_settings(
        appconfig_enabled=True,
        appconfig_origin="http://localhost:2772/",
        appconfig_application="neurostack",
        appconfig_environment="production",
        appconfig_profile="egress-flags",
        appconfig_token="optional-token",
    )

    assert configured.appconfig_enabled is True


@pytest.mark.parametrize(
    "missing",
    ["appconfig_application", "appconfig_environment", "appconfig_profile"],
)
def test_production_enabled_requires_all_exact_identifiers(missing: str) -> None:
    values = {
        "appconfig_enabled": True,
        "appconfig_application": "neurostack",
        "appconfig_environment": "production",
        "appconfig_profile": "egress-flags",
    }
    values[missing] = ""

    with pytest.raises(ConfigurationError, match=missing.upper()):
        _production_settings(**values)


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:2772",
        "http://10.0.0.2:2772",
        "http://user@localhost:2772",
        "http://localhost:2772/path",
        "http://localhost:2772?query=yes",
        "http://localhost:2772#fragment",
    ],
)
def test_production_enabled_rejects_non_loopback_or_ambiguous_origin(
    origin: str,
) -> None:
    with pytest.raises(ConfigurationError, match="APPCONFIG_ORIGIN"):
        _production_settings(
            appconfig_enabled=True,
            appconfig_origin=origin,
            appconfig_application="neurostack",
            appconfig_environment="production",
            appconfig_profile="egress-flags",
        )


def test_disabled_configuration_does_not_require_identifiers_or_validate_origin() -> (
    None
):
    configured = _production_settings(
        appconfig_enabled=False,
        appconfig_origin="https://remote.invalid/path?query=yes",
    )

    assert configured.appconfig_enabled is False


def test_production_validation_never_echoes_bearer_token() -> None:
    token = "secret-token-sentinel\ninvalid"

    with pytest.raises(ConfigurationError) as captured:
        _production_settings(
            appconfig_enabled=True,
            appconfig_application="neurostack",
            appconfig_environment="production",
            appconfig_profile="egress-flags",
            appconfig_token=token,
        )

    assert token not in str(captured.value)
    assert "secret-token-sentinel" not in str(captured.value)


def test_bearer_token_is_redacted_from_settings_representation() -> None:
    token = "secret-token-sentinel"

    configured = Settings(_env_file=None, appconfig_token=token)

    assert token not in repr(configured)
    assert configured.appconfig_token.get_secret_value() == token


@pytest.mark.parametrize(
    ("overrides", "error", "message"),
    [
        (
            {"billing_operation_receipt_signing_key": "k" * 31},
            ConfigurationError,
            "BILLING_OPERATION_RECEIPT_SIGNING_KEY must be at least 32 bytes in production",
        ),
        (
            {"billing_observability_smoke_token": ""},
            ConfigurationError,
            "BILLING_OBSERVABILITY_SMOKE_TOKEN is required in production",
        ),
        (
            {"billing_observability_acknowledgement": ""},
            ConfigurationError,
            "fresh authenticated runtime acknowledgement",
        ),
        (
            {"billing_alert_receiver_url": "https://127.0.0.1/hook"},
            ValidationError,
            "BILLING_ALERT_RECEIVER_URL must be a non-loopback HTTPS URL",
        ),
    ],
)
def test_complete_appconfig_fixture_does_not_mask_billing_production_gates(
    overrides: dict[str, Any], error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        _production_settings(appconfig_enabled=False, **overrides)
