"""Configuration contracts for fixed media concurrency ceilings."""

import pytest
from pydantic import ValidationError

from src.config.settings import Settings


def test_media_concurrency_defaults_are_fixed_when_environment_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEDIA_MAX_CONCURRENT_UPLOADS", raising=False)
    monkeypatch.delenv("MEDIA_MAX_CONCURRENT_DOWNLOADS", raising=False)

    configured = Settings(_env_file=None)

    assert configured.media_max_concurrent_uploads == 4
    assert configured.media_max_concurrent_downloads == 8


@pytest.mark.parametrize("value", ["1", "2", "3", "4"])
def test_upload_concurrency_can_only_be_reduced_to_a_positive_integer(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("MEDIA_MAX_CONCURRENT_UPLOADS", value)
    assert Settings(_env_file=None).media_max_concurrent_uploads == int(value)


@pytest.mark.parametrize("value", ["1", "2", "4", "7", "8"])
def test_download_concurrency_can_only_be_reduced_to_a_positive_integer(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("MEDIA_MAX_CONCURRENT_DOWNLOADS", value)
    assert Settings(_env_file=None).media_max_concurrent_downloads == int(value)


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "four", "5"])
def test_invalid_upload_concurrency_prevents_settings_startup(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("MEDIA_MAX_CONCURRENT_UPLOADS", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


@pytest.mark.parametrize("value", [True, 1.0, "4.0", "+2", " 2 "])
def test_upload_concurrency_rejects_coercible_non_integer_input(value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, media_max_concurrent_uploads=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "eight", "9"])
def test_invalid_download_concurrency_prevents_settings_startup(
    monkeypatch: pytest.MonkeyPatch,
    value: str,
) -> None:
    monkeypatch.setenv("MEDIA_MAX_CONCURRENT_DOWNLOADS", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
