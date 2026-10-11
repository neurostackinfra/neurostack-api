"""Configuration contracts for the localhost AppConfig Agent cache."""

from typing import Any

import pytest

from src.config.settings import Settings
from src.core.exceptions import ConfigurationError


def _production_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "environment": "production",
        "secret_key": "production-secret-key-with-at-least-32-characters",
        "database_url": "postgresql+asyncpg://runtime-user:runtime-pass@database/api",
        "allowed_origins": ["https://app.neurostack.example"],
        "allowed_hosts": ["api.neurostack.example"],
        "abacatepay_webhook_secret": "legacy-fixture-secret",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


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
