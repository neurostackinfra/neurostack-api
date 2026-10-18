"""Configuration contracts for the SPA served by the API (Group 6c)."""

import pytest
from pydantic import ValidationError

from src.config.settings import Settings


def test_spa_defaults_serve_nothing_until_a_build_directory_is_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SPA_ENABLED", raising=False)
    monkeypatch.delenv("SPA_DIST_DIR", raising=False)

    configured = Settings(_env_file=None)

    assert configured.spa_enabled is True
    assert configured.spa_dist_dir == ""


# The production-serving ECS service runs with ENVIRONMENT=staging, so nothing
# about the SPA may depend on the stage.
@pytest.mark.parametrize("environment", ["development", "test", "staging"])
def test_spa_settings_are_read_from_the_environment_in_every_stage(
    monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    monkeypatch.setenv("ENVIRONMENT", environment)
    monkeypatch.setenv("SECRET_KEY", "spa-settings-test-secret-key-0123456789abcdef")
    monkeypatch.setenv("SPA_ENABLED", "false")
    monkeypatch.setenv("SPA_DIST_DIR", "/app/spa")

    configured = Settings(_env_file=None)

    assert configured.spa_enabled is False
    assert configured.spa_dist_dir == "/app/spa"


def test_spa_enabled_rejects_non_boolean_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPA_ENABLED", "sometimes")

    with pytest.raises(
        ValidationError, match=r"spa_enabled\n  Input should be a valid boolean"
    ):
        Settings(_env_file=None)
