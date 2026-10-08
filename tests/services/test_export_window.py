"""Unit tests for the pure audit export window policy."""

from datetime import date, timedelta

import pytest

from src.services.export_window import (
    AUDIT_LOOKBACK_MONTHS,
    EXPORT_MAX_DAYS,
    ExportWindowError,
    lookback_floor,
    validate_export_window,
)

TODAY = date(2026, 10, 7)


def _code(start: date, end: date, *, extended_access: bool = False) -> str | None:
    try:
        validate_export_window(
            start, end, local_today=TODAY, extended_access=extended_access
        )
    except ExportWindowError as exc:
        return exc.code
    return None


def test_policy_constants() -> None:
    assert AUDIT_LOOKBACK_MONTHS == 6
    assert EXPORT_MAX_DAYS == 60


def test_sixty_inclusive_dates_pass_and_sixty_one_fail() -> None:
    end = TODAY
    assert _code(end - timedelta(days=59), end) is None
    assert _code(end - timedelta(days=60), end) == "export_window_60_days"


def test_single_day_is_one_inclusive_date() -> None:
    assert _code(TODAY, TODAY) is None


@pytest.mark.parametrize(
    ("today", "floor"),
    [
        (date(2026, 10, 7), date(2026, 4, 7)),
        (date(2026, 8, 31), date(2026, 2, 28)),
        (date(2028, 8, 31), date(2028, 2, 29)),
        (date(2026, 3, 31), date(2025, 9, 30)),
        (date(2026, 1, 15), date(2025, 7, 15)),
        (date(2026, 6, 30), date(2025, 12, 30)),
        (date(1, 3, 1), date.min),
    ],
)
def test_lookback_floor_keeps_day_of_month_clamped_to_month_end(
    today: date, floor: date
) -> None:
    assert lookback_floor(today) == floor


def test_exact_six_month_floor_is_inclusive() -> None:
    floor = date(2026, 4, 7)
    assert _code(floor, floor + timedelta(days=59)) is None
    assert _code(floor - timedelta(days=1), floor + timedelta(days=58)) == (
        "export_lookback_6_months"
    )


def test_month_end_floor_on_august_31() -> None:
    def code(today: date, start: date) -> str | None:
        try:
            validate_export_window(
                start, start, local_today=today, extended_access=False
            )
        except ExportWindowError as exc:
            return exc.code
        return None

    assert code(date(2026, 8, 31), date(2026, 2, 28)) is None
    assert code(date(2026, 8, 31), date(2026, 2, 27)) == "export_lookback_6_months"
    assert code(date(2028, 8, 31), date(2028, 2, 29)) is None
    assert code(date(2028, 8, 31), date(2028, 2, 28)) == "export_lookback_6_months"


def test_end_before_start_is_invalid_range_first() -> None:
    assert _code(TODAY, TODAY - timedelta(days=1)) == "invalid_date_range"
    # Ordering wins even when the bounds are also far in the past.
    assert _code(date(2020, 1, 2), date(2020, 1, 1)) == "invalid_date_range"


def test_window_cap_is_checked_before_lookback() -> None:
    assert _code(date(2020, 1, 1), date(2020, 12, 31)) == "export_window_60_days"


def test_addon_waives_lookback_but_not_the_sixty_day_cap() -> None:
    old_start = date(2020, 1, 1)
    assert _code(old_start, old_start + timedelta(days=59)) == (
        "export_lookback_6_months"
    )
    assert _code(old_start, old_start + timedelta(days=59), extended_access=True) is (
        None
    )
    assert (
        _code(old_start, old_start + timedelta(days=60), extended_access=True)
        == "export_window_60_days"
    )
    assert (
        _code(TODAY, TODAY - timedelta(days=1), extended_access=True)
        == "invalid_date_range"
    )
