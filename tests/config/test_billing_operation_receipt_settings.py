"""Configuration contract for the billing operator-receipt lifetime cap."""

import pytest
from pydantic import ValidationError

from src.config.settings import Settings

_VARIABLE = "BILLING_OPERATION_RECEIPT_MAX_TTL_SECONDS"


def test_receipt_lifetime_cap_defaults_to_twenty_four_hours(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(_VARIABLE, raising=False)

    assert Settings(_env_file=None).billing_operation_receipt_max_ttl_seconds == 86400


@pytest.mark.parametrize("value", ["1", "3600", "86400", "604800"])
def test_receipt_lifetime_cap_accepts_one_second_to_seven_days(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(_VARIABLE, value)

    assert Settings(_env_file=None).billing_operation_receipt_max_ttl_seconds == int(
        value
    )


@pytest.mark.parametrize("value", ["0", "-1", "604801", "1.5", "+60", " 60", "", "x"])
def test_receipt_lifetime_cap_outside_the_range_prevents_startup(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv(_VARIABLE, value)

    with pytest.raises(
        ValidationError, match="billing_operation_receipt_max_ttl_seconds"
    ):
        Settings(_env_file=None)


@pytest.mark.parametrize("value", [True, 60.0])
def test_receipt_lifetime_cap_rejects_coercible_non_integers(value: object) -> None:
    with pytest.raises(
        ValidationError, match="billing_operation_receipt_max_ttl_seconds"
    ):
        Settings(
            _env_file=None,
            billing_operation_receipt_max_ttl_seconds=value,  # type: ignore[arg-type]
        )
