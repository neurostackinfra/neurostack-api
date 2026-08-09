"""Timezone-aware classification for a clinic's structured business hours."""

from __future__ import annotations

import re
from datetime import datetime, timezone, tzinfo
from typing import Any

from src.prompt.timeutils import resolve_clinic_tz

_DAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_TIME_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


def _operation_value(operations: Any, field: str) -> Any:
    """Read a field from either a Pydantic model or raw JSONB defensively."""
    try:
        if isinstance(operations, dict):
            return operations.get(field)
        return getattr(operations, field, None)
    except (AttributeError, TypeError):
        return None


def _minutes(value: Any) -> int | None:
    if not isinstance(value, str) or _TIME_PATTERN.fullmatch(value) is None:
        return None
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


def _configuration(operations: Any) -> tuple[int, int, set[str]] | None:
    """Return parsed business hours, or ``None`` for legacy/malformed data."""
    if _operation_value(operations, "business_hours_enabled") is not True:
        return None

    open_minutes = _minutes(_operation_value(operations, "business_hours_open"))
    close_minutes = _minutes(_operation_value(operations, "business_hours_close"))
    days = _operation_value(operations, "business_hours_days")
    if open_minutes is None or close_minutes is None or close_minutes <= open_minutes:
        return None
    if not isinstance(days, list) or not days:
        return None
    if any(not isinstance(day, str) or day not in _DAY_CODES for day in days):
        return None
    return open_minutes, close_minutes, set(days)


def _is_configured(operations: Any) -> bool:
    """Whether operations contains a complete, valid same-day window."""
    return _configuration(operations) is not None


def _is_outside_business_hours(
    created_at_utc: datetime,
    configuration: tuple[int, int, set[str]],
    clinic_tz: tzinfo,
) -> bool:
    """Classify a timestamp using an already prepared window and timezone."""
    open_minutes, close_minutes, days = configuration
    timestamp = created_at_utc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    local_timestamp = timestamp.astimezone(clinic_tz)
    local_minutes = local_timestamp.hour * 60 + local_timestamp.minute
    return (
        _DAY_CODES[local_timestamp.weekday()] not in days
        or local_minutes < open_minutes
        or local_minutes >= close_minutes
    )


def is_outside_business_hours(created_at_utc: datetime, operations: Any) -> bool:
    """Classify a UTC timestamp against the clinic-local business window.

    Unconfigured or malformed operations return ``False``; callers can use
    ``_is_configured`` or ``summarize_outside_hours`` to expose that state.
    Naïve timestamps are interpreted as UTC, matching the database contract.
    """
    configuration = _configuration(operations)
    if configuration is None:
        return False

    clinic_tz = resolve_clinic_tz(_operation_value(operations, "timezone"))
    return _is_outside_business_hours(created_at_utc, configuration, clinic_tz)


def summarize_outside_hours(
    created_ats: list[datetime], operations: Any
) -> dict[str, bool | int | float | None]:
    """Aggregate outside-hours counts and percentage for trace timestamps."""
    configuration = _configuration(operations)
    if configuration is None:
        return {
            "business_hours_configured": False,
            "outside_hours_count": 0,
            "outside_hours_pct": None,
        }

    clinic_tz = resolve_clinic_tz(_operation_value(operations, "timezone"))
    total = len(created_ats)
    count = sum(
        _is_outside_business_hours(created_at, configuration, clinic_tz)
        for created_at in created_ats
    )
    return {
        "business_hours_configured": True,
        "outside_hours_count": count,
        "outside_hours_pct": round(count / total * 100, 1) if total else None,
    }
