"""Protected ASAAS and legacy-webhook configuration tests."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from src.config.settings import (
    ASAAS_API_BASE_URL,
    ASAAS_AUTH_HEADER,
    ASAAS_DEFAULT_USER_AGENT,
    Settings,
)
from src.core.exceptions import ConfigurationError


def _production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "secret_key": "production-secret-key-at-least-32-characters",
        "database_url": "postgresql+asyncpg://app:password@postgres/app",
        "redis_url": "redis://redis:6379/0",
        "allowed_origins": ["https://app.neurostack.example"],
        "allowed_hosts": ["api.neurostack.example"],
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
    legacy_api_key = "legacy-api-key-never-render"
    configured = Settings(
        _env_file=None,
        asaas_api_key=api_key,
        asaas_webhook_token=webhook_token,
        abacatepay_api_key=legacy_api_key,
    )

    rendered = repr(configured)
    dumped = repr(configured.model_dump())
    assert api_key not in rendered
    assert api_key not in dumped
    assert webhook_token not in rendered
    assert webhook_token not in dumped
    assert legacy_api_key not in rendered
    assert legacy_api_key not in dumped
    assert isinstance(configured.asaas_api_key, SecretStr)
    assert isinstance(configured.abacatepay_api_key, SecretStr)


def test_absent_or_incomplete_asaas_configuration_is_incompatible() -> None:
    assert Settings(_env_file=None).is_asaas_configuration_compatible is False
    assert (
        Settings(
            _env_file=None,
            asaas_api_key="$aact_prod_fixture",
        ).is_asaas_configuration_compatible
        is False
    )


@pytest.mark.parametrize(
    "overrides",
    [{}, {"abacatepay_webhook_secret": " \t "}],
    ids=["absent", "whitespace-only"],
)
def test_production_startup_rejects_missing_legacy_webhook_secret(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(ConfigurationError) as exc_info:
        _production_settings(**overrides)

    message = str(exc_info.value)
    assert "ABACATEPAY_WEBHOOK_SECRET is required in production" in message
    assert "production-secret-key-at-least-32-characters" not in message
    assert "password" not in message


def test_production_startup_error_never_echoes_legacy_webhook_secret() -> None:
    with pytest.raises(ConfigurationError) as exc_info:
        _production_settings(
            abacatepay_webhook_secret="legacy-fixture-secret",
            debug=True,
        )

    message = str(exc_info.value)
    assert "DEBUG must be False in production" in message
    assert "ABACATEPAY_WEBHOOK_SECRET" not in message
    assert "legacy-fixture-secret" not in message


def test_production_startup_accepts_injected_legacy_webhook_secret() -> None:
    configured = _production_settings(abacatepay_webhook_secret="legacy-fixture-secret")

    assert configured.is_production is True
    assert isinstance(configured.abacatepay_webhook_secret, SecretStr)
    assert (
        configured.abacatepay_webhook_secret.get_secret_value()
        == "legacy-fixture-secret"
    )
    assert "legacy-fixture-secret" not in repr(configured)
