"""Tests for deterministic clinic-local business-hours classification."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

import src.services.business_hours as business_hours
from src.models.clinic_config import BusinessHoursWindow, ClinicOperations
from src.services.business_hours import (
    _is_configured,
    format_business_hours,
    is_outside_business_hours,
    summarize_outside_hours,
)

_WEEKDAY_WINDOW = {"open": "09:00", "close": "17:00"}
_SATURDAY_WINDOW = {"open": "10:00", "close": "13:00"}


def _weekly_schedule() -> dict[str, dict[str, str]]:
    return {
        "mon": _WEEKDAY_WINDOW,
        "tue": _WEEKDAY_WINDOW,
        "wed": _WEEKDAY_WINDOW,
        "thu": _WEEKDAY_WINDOW,
        "fri": _WEEKDAY_WINDOW,
        "sat": _SATURDAY_WINDOW,
    }


def _operations(*, as_dict: bool = False, **overrides):
    values: dict = {
        "timezone": "UTC",
        "business_hours": _weekly_schedule(),
        **overrides,
    }
    return values if as_dict else ClinicOperations(**values)


def _utc(day: int, hour: int, minute: int = 0) -> datetime:
    # 2026-01-05 is a Monday; 2026-01-10 is a Saturday; 2026-01-11 is Sunday.
    return datetime(2026, 1, day, hour, minute, tzinfo=timezone.utc)


@pytest.mark.parametrize("as_dict", [False, True], ids=["model", "dict"])
@pytest.mark.parametrize(
    ("created_at", "outside"),
    [
        (_utc(5, 10), False),  # Monday within weekday window
        (_utc(5, 8, 59), True),  # Monday before open
        (_utc(5, 9), False),  # Monday exactly at open — inside
        (_utc(5, 17), True),  # Monday exactly at close — outside
        (_utc(5, 17, 1), True),  # Monday just after close
        (_utc(10, 11), False),  # Saturday within its own window
        (_utc(10, 9), True),  # Saturday before Saturday open
        (_utc(10, 13), True),  # Saturday exactly at close — outside
        (_utc(11, 12), True),  # Sunday — missing day is always outside
    ],
    ids=[
        "within",
        "before",
        "exact-open",
        "exact-close",
        "after",
        "sat-within",
        "sat-before",
        "sat-exact-close",
        "sun-missing-day",
    ],
)
def test_classifies_window_boundaries_for_model_and_dict(
    as_dict: bool, created_at: datetime, outside: bool
) -> None:
    assert (
        is_outside_business_hours(created_at, _operations(as_dict=as_dict)) is outside
    )


def test_converts_utc_to_sao_paulo_before_classifying() -> None:
    operations = _operations(as_dict=True, timezone="America/Sao_Paulo")

    # 11:00 UTC is 08:00 in São Paulo → before the 09:00 open (outside).
    assert is_outside_business_hours(_utc(5, 11), operations) is True
    # 12:00 UTC is 09:00 in São Paulo → exactly at open (inside).
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
        [_utc(5, 8), _utc(5, 10), _utc(11, 10)],
        _operations(as_dict=as_dict),
    )

    assert result == {
        "business_hours_configured": True,
        "outside_hours_count": 2,
        "outside_hours_pct": 66.7,
    }


def test_summary_resolves_timezone_once_for_multiple_timestamps(
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
        # No business_hours key at all
        _operations(as_dict=True, business_hours=None),
        # Empty schedule (falls back to None after validation)
        {"business_hours": {}},
        # Every day malformed → schedule reduces to None
        {"business_hours": {"mon": {"open": "invalid", "close": "18:00"}}},
        {"business_hours": {"mon": {"open": "09:00", "close": "09:00"}}},
        {"business_hours": {"mon": {"open": "09:00", "close": "08:00"}}},
        # Invalid day key silently dropped → schedule empty → not configured
        {"business_hours": {"funday": {"open": "09:00", "close": "17:00"}}},
        # Bare non-dict value
        {"business_hours": "not a schedule"},
        # ClinicOperations may be entirely None (real production shape when
        # no config row exists — see trace_service.py and builder.py).
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


def test_all_days_closed_map_is_unconfigured() -> None:
    """A schedule whose only entries are malformed windows is unconfigured."""
    operations = {
        "business_hours": {
            "mon": {"open": "09:00", "close": "09:00"},
            "tue": {"open": "10:00", "close": "10:00"},
        }
    }

    assert _is_configured(operations) is False
    assert summarize_outside_hours([_utc(5, 10)], operations) == {
        "business_hours_configured": False,
        "outside_hours_count": 0,
        "outside_hours_pct": None,
    }


@pytest.mark.parametrize(
    "window_kwargs",
    [
        {"open": "09:00", "close": "09:00"},  # close == open
        {"open": "18:00", "close": "09:00"},  # close < open
        {"open": "25:00", "close": "18:00"},  # bad hour
        {"open": "09:00", "close": "9h"},  # bad format
    ],
    ids=["equal", "before", "bad-hour", "bad-format"],
)
def test_business_hours_window_model_rejects_bad_values(
    window_kwargs: dict[str, str],
) -> None:
    with pytest.raises(ValidationError):
        BusinessHoursWindow(**window_kwargs)


def test_clinic_operations_rejects_invalid_day_key() -> None:
    with pytest.raises(ValidationError):
        ClinicOperations(business_hours={"funday": {"open": "09:00", "close": "18:00"}})


def test_clinic_operations_collapses_empty_business_hours_to_none() -> None:
    operations = ClinicOperations(business_hours={})
    assert operations.business_hours is None


def test_clinic_operations_lowercases_day_keys() -> None:
    operations = ClinicOperations(
        business_hours={"MON": {"open": "09:00", "close": "18:00"}}
    )
    assert set(operations.business_hours) == {"mon"}


# --- format_business_hours ---------------------------------------------------


@pytest.mark.parametrize(
    ("operations", "expected"),
    [
        # Not configured
        (None, ""),
        ({}, ""),
        ({"business_hours": {}}, ""),
        # Single day open — closed days are grouped into one run per
        # Decision #7 ("fechado é um valor de janela como outro"), so the
        # six remaining closed days collapse into ``"Ter–Dom: fechado"``.
        (
            {"business_hours": {"mon": {"open": "09:00", "close": "18:00"}}},
            "Seg: 09:00–18:00 | Ter–Dom: fechado",
        ),
        # Weekday range + distinct Saturday + closed Sunday (the spec example)
        (
            {
                "business_hours": {
                    "mon": {"open": "09:00", "close": "19:00"},
                    "tue": {"open": "09:00", "close": "19:00"},
                    "wed": {"open": "09:00", "close": "19:00"},
                    "thu": {"open": "09:00", "close": "19:00"},
                    "fri": {"open": "09:00", "close": "19:00"},
                    "sat": {"open": "09:00", "close": "14:00"},
                }
            },
            "Seg–Sex: 09:00–19:00 | Sáb: 09:00–14:00 | Dom: fechado",
        ),
        # All seven days share one window — one grouped run
        (
            {
                "business_hours": {
                    day: {"open": "08:00", "close": "18:00"}
                    for day in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
                }
            },
            "Seg–Dom: 08:00–18:00",
        ),
    ],
    ids=[
        "none",
        "empty-dict",
        "empty-schedule",
        "single-day",
        "wish-example",
        "all-days",
    ],
)
def test_format_business_hours_groups_and_labels(operations, expected: str) -> None:
    assert format_business_hours(operations) == expected


def test_format_business_hours_accepts_pydantic_operations() -> None:
    operations = ClinicOperations(
        business_hours={
            "mon": BusinessHoursWindow(open="09:00", close="19:00"),
            "tue": BusinessHoursWindow(open="09:00", close="19:00"),
            "wed": BusinessHoursWindow(open="09:00", close="19:00"),
            "thu": BusinessHoursWindow(open="09:00", close="19:00"),
            "fri": BusinessHoursWindow(open="09:00", close="19:00"),
            "sat": BusinessHoursWindow(open="09:00", close="14:00"),
        }
    )
    assert (
        format_business_hours(operations)
        == "Seg–Sex: 09:00–19:00 | Sáb: 09:00–14:00 | Dom: fechado"
    )
