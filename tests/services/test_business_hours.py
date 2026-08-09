"""Tests for deterministic clinic-local business-hours classification."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

import src.services.business_hours as business_hours
from src.models.clinic_config import ClinicOperations
from src.services.business_hours import (
    _is_configured,
    is_outside_business_hours,
    summarize_outside_hours,
)


def _operations(*, as_dict: bool = False, **overrides):
    values = {
        "timezone": "UTC",
        "business_hours_enabled": True,
        "business_hours_open": "09:00",
        "business_hours_close": "17:00",
        "business_hours_days": ["mon", "tue", "wed", "thu", "fri"],
        **overrides,
    }
    return values if as_dict else ClinicOperations(**values)


def _utc(day: int, hour: int, minute: int = 0) -> datetime:
    # 2026-01-05 is a Monday.
    return datetime(2026, 1, day, hour, minute, tzinfo=timezone.utc)


@pytest.mark.parametrize("as_dict", [False, True], ids=["model", "dict"])
@pytest.mark.parametrize(
    ("created_at", "outside"),
    [
        (_utc(5, 10), False),
        (_utc(5, 8, 59), True),
        (_utc(5, 9), False),
        (_utc(5, 17), True),
        (_utc(5, 17, 1), True),
        (_utc(10, 10), True),  # Saturday
    ],
    ids=["within", "before", "exact-open", "exact-close", "after", "non-day"],
)
def test_classifies_window_boundaries_for_model_and_dict(
    as_dict: bool, created_at: datetime, outside: bool
) -> None:
    assert (
        is_outside_business_hours(created_at, _operations(as_dict=as_dict)) is outside
    )


def test_converts_utc_to_sao_paulo_before_classifying() -> None:
    operations = _operations(as_dict=True, timezone="America/Sao_Paulo")

    # 11:00 UTC is 08:00 in São Paulo and therefore before the 09:00 open.
    assert is_outside_business_hours(_utc(5, 11), operations) is True
    assert is_outside_business_hours(_utc(5, 12), operations) is False


@pytest.mark.parametrize("configured_timezone", [None, "Not/A_Real_Zone"])
def test_missing_and_invalid_timezones_fall_back_to_sao_paulo(
    configured_timezone: str | None,
) -> None:
    operations = _operations(as_dict=True, timezone=configured_timezone)

    assert is_outside_business_hours(_utc(5, 11), operations) is True
    assert is_outside_business_hours(_utc(5, 12), operations) is False


@pytest.mark.parametrize("as_dict", [False, True], ids=["model", "dict"])
def test_summary_counts_and_rounds_percentage(as_dict: bool) -> None:
    result = summarize_outside_hours(
        [_utc(5, 8), _utc(5, 10), _utc(10, 10)],
        _operations(as_dict=as_dict),
    )

    assert result == {
        "business_hours_configured": True,
        "outside_hours_count": 2,
        "outside_hours_pct": 66.7,
    }


def test_summary_resolves_invalid_timezone_once_for_multiple_timestamps(
    mocker,
) -> None:
    resolver = mocker.patch(
        "src.services.business_hours.resolve_clinic_tz",
        wraps=business_hours.resolve_clinic_tz,
    )

    result = summarize_outside_hours(
        [_utc(5, 11), _utc(5, 12), _utc(5, 20)],
        _operations(as_dict=True, timezone="Not/A_Real_Zone"),
    )

    assert result == {
        "business_hours_configured": True,
        "outside_hours_count": 2,
        "outside_hours_pct": 66.7,
    }
    resolver.assert_called_once_with("Not/A_Real_Zone")


def test_valid_configuration_with_no_timestamps_has_null_percentage() -> None:
    assert summarize_outside_hours([], _operations()) == {
        "business_hours_configured": True,
        "outside_hours_count": 0,
        "outside_hours_pct": None,
    }


@pytest.mark.parametrize(
    "operations",
    [
        _operations(as_dict=True, business_hours_enabled=False),
        {"business_hours_enabled": True},
        _operations(as_dict=True, business_hours_open="invalid"),
        _operations(as_dict=True, business_hours_days=["funday"]),
        _operations(as_dict=True, business_hours_days="mon"),
        _operations(as_dict=True, business_hours_close="09:00"),
        _operations(as_dict=True, business_hours_close="08:00"),
        None,
    ],
)
def test_unconfigured_or_malformed_data_never_raises(operations) -> None:
    assert _is_configured(operations) is False
    assert is_outside_business_hours(_utc(5, 10), operations) is False
    assert summarize_outside_hours([_utc(5, 10)], operations) == {
        "business_hours_configured": False,
        "outside_hours_count": 0,
        "outside_hours_pct": None,
    }


def test_model_allows_close_not_after_open_for_classifier_to_handle() -> None:
    operations = _operations(business_hours_close="09:00")

    assert operations.business_hours_close == "09:00"
    assert _is_configured(operations) is False


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("business_hours_open", "25:00"),
        ("business_hours_close", "9h"),
        ("business_hours_days", ["funday"]),
    ],
)
def test_model_rejects_invalid_structured_hours(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        ClinicOperations(**{field: value})


def test_model_normalizes_empty_values_and_day_case() -> None:
    operations = ClinicOperations(
        business_hours_open="",
        business_hours_close="",
        business_hours_days=[],
    )
    assert operations.business_hours_open is None
    assert operations.business_hours_close is None
    assert operations.business_hours_days is None

    normalized = ClinicOperations(business_hours_days=["MON", "Fri"])
    assert normalized.business_hours_days == ["mon", "fri"]
