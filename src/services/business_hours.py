"""Timezone-aware classification for a clinic's weekly business hours schedule.

The clinic's structured business hours are a per-weekday map:

    operations.business_hours = {
        "mon": {"open": "09:00", "close": "18:00"},
        ...
    }

This module reads that map defensively (Pydantic model or raw JSONB dict),
classifies UTC timestamps as inside/outside the clinic-local window, and
formats the schedule into the human-readable phrase the AI recites to the
patient.  Malformed / unconfigured data is treated as "not configured" —
functions never raise.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone, tzinfo
from typing import Any

from neurostack_core.prompt import resolve_clinic_tz
from src.models.clinic_config import BusinessHoursWindow

_DAY_CODES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_DAY_LABELS_PT = {
    "mon": "Seg",
    "tue": "Ter",
    "wed": "Qua",
    "thu": "Qui",
    "fri": "Sex",
    "sat": "Sáb",
    "sun": "Dom",
}
_TIME_PATTERN = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


def _operation_value(operations: Any, field: str) -> Any:
    """Read a field from either a Pydantic model or a raw JSONB dict."""
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


def _extract_window(window: Any) -> tuple[int, int] | None:
    """Return (open_minutes, close_minutes) for a valid window, else None."""
    if isinstance(window, BusinessHoursWindow):
        open_minutes = _minutes(window.open)
        close_minutes = _minutes(window.close)
    elif isinstance(window, dict):
        open_minutes = _minutes(window.get("open"))
        close_minutes = _minutes(window.get("close"))
    else:
        return None
    if open_minutes is None or close_minutes is None:
        return None
    if close_minutes <= open_minutes:
        return None
    return open_minutes, close_minutes


def _schedule(operations: Any) -> dict[str, tuple[int, int]] | None:
    """Parse ``operations.business_hours`` into a per-day minute-window map.

    Accepts both a Pydantic ``ClinicOperations`` and a raw dict (the builder
    call site passes raw JSONB).  Days whose key or window is malformed are
    dropped.  Returns ``None`` for an empty / missing / all-invalid schedule.
    Never raises.
    """
    raw = _operation_value(operations, "business_hours")
    if raw is None:
        return None
    if not isinstance(raw, dict) or not raw:
        return None

    schedule: dict[str, tuple[int, int]] = {}
    for day, window in raw.items():
        if not isinstance(day, str):
            continue
        key = day.lower()
        if key not in _DAY_CODES:
            continue
        parsed = _extract_window(window)
        if parsed is None:
            continue
        schedule[key] = parsed

    return schedule or None


def _is_configured(operations: Any) -> bool:
    """Whether ``operations`` contains at least one valid day window."""
    return _schedule(operations) is not None


def _is_outside_business_hours(
    created_at_utc: datetime,
    schedule: dict[str, tuple[int, int]],
    clinic_tz: tzinfo,
) -> bool:
    """Classify a timestamp against an already-parsed schedule and timezone."""
    timestamp = created_at_utc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    local_timestamp = timestamp.astimezone(clinic_tz)
    day_code = _DAY_CODES[local_timestamp.weekday()]
    window = schedule.get(day_code)
    if window is None:
        return True
    open_minutes, close_minutes = window
    local_minutes = local_timestamp.hour * 60 + local_timestamp.minute
    return local_minutes < open_minutes or local_minutes >= close_minutes


def is_outside_business_hours(created_at_utc: datetime, operations: Any) -> bool:
    """Classify a UTC timestamp against the clinic-local business window.

    Unconfigured or malformed operations return ``False``; callers can use
    ``_is_configured`` or ``summarize_outside_hours`` to expose that state.
    Naïve timestamps are interpreted as UTC, matching the database contract.
    """
    schedule = _schedule(operations)
    if schedule is None:
        return False

    clinic_tz = resolve_clinic_tz(_operation_value(operations, "timezone"))
    return _is_outside_business_hours(created_at_utc, schedule, clinic_tz)


def summarize_outside_hours(
    created_ats: list[datetime], operations: Any
) -> dict[str, bool | int | float | None]:
    """Aggregate outside-hours counts and percentage for trace timestamps."""
    schedule = _schedule(operations)
    if schedule is None:
        return {
            "business_hours_configured": False,
            "outside_hours_count": 0,
            "outside_hours_pct": None,
        }

    clinic_tz = resolve_clinic_tz(_operation_value(operations, "timezone"))
    total = len(created_ats)
    count = sum(
        _is_outside_business_hours(created_at, schedule, clinic_tz)
        for created_at in created_ats
    )
    return {
        "business_hours_configured": True,
        "outside_hours_count": count,
        "outside_hours_pct": round(count / total * 100, 1) if total else None,
    }


def _format_window(window: tuple[int, int] | None) -> str:
    if window is None:
        return "fechado"
    open_minutes, close_minutes = window
    return (
        f"{open_minutes // 60:02d}:{open_minutes % 60:02d}"
        f"–"
        f"{close_minutes // 60:02d}:{close_minutes % 60:02d}"
    )


def format_business_hours(operations: Any) -> str:
    """Render the weekly schedule as the phrase the AI recites to patients.

    Iterates ``mon..sun`` in fixed order, groups consecutive days that share
    an identical window (closed counts as a window value), and emits
    ``"Seg: HH:MM–HH:MM"`` for a 1-day run or ``"Seg–Sex: HH:MM–HH:MM"`` for
    longer runs.  Closed runs render as ``"Dom: fechado"``.  Not configured
    → ``""``.
    """
    schedule = _schedule(operations)
    if schedule is None:
        return ""

    # Build the full-week window list, using None for closed days.
    per_day: list[tuple[str, tuple[int, int] | None]] = [
        (day, schedule.get(day)) for day in _DAY_CODES
    ]

    groups: list[tuple[list[str], tuple[int, int] | None]] = []
    for day, window in per_day:
        if groups and groups[-1][1] == window:
            groups[-1][0].append(day)
        else:
            groups.append(([day], window))

    parts: list[str] = []
    for days, window in groups:
        label = (
            _DAY_LABELS_PT[days[0]]
            if len(days) == 1
            else f"{_DAY_LABELS_PT[days[0]]}–{_DAY_LABELS_PT[days[-1]]}"
        )
        parts.append(f"{label}: {_format_window(window)}")

    return " | ".join(parts)
