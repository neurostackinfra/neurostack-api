"""Audit export window policy shared by the trace and handoff CSV exports.

Every clinic may export at most ``EXPORT_MAX_DAYS`` inclusive civil dates per
file, starting no earlier than ``AUDIT_LOOKBACK_MONTHS`` calendar months before
the clinic's current civil date. The per-clinic ``audit_extended_access``
add-on waives only the lookback; the per-file cap always applies.
"""

from __future__ import annotations

import calendar
from datetime import date

AUDIT_LOOKBACK_MONTHS = 6
EXPORT_MAX_DAYS = 60

INVALID_DATE_RANGE = "invalid_date_range"
EXPORT_WINDOW_60_DAYS = "export_window_60_days"
EXPORT_LOOKBACK_6_MONTHS = "export_lookback_6_months"


class ExportWindowError(Exception):
    """The requested export window violates the audit export policy."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def lookback_floor(local_today: date) -> date:
    """Return the earliest exportable civil date without the add-on.

    The floor keeps ``local_today``'s day of month six calendar months earlier,
    clamped to the last day of that month (31/08 -> 28/02, or 29/02 in a leap
    year). Dates before year 1 clamp to ``date.min``.
    """

    month_index = local_today.year * 12 + local_today.month - 1 - AUDIT_LOOKBACK_MONTHS
    year, zero_based_month = divmod(month_index, 12)
    if year < date.min.year:
        return date.min
    month = zero_based_month + 1
    day = min(local_today.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def validate_export_window(
    start: date,
    end: date,
    *,
    local_today: date,
    extended_access: bool,
) -> None:
    """Validate inclusive civil export dates against the audit policy.

    Checks run in a fixed order: ordering (``invalid_date_range``), the per-file
    cap (``export_window_60_days``), then the lookback
    (``export_lookback_6_months``), which ``extended_access`` waives.
    """

    if end < start:
        raise ExportWindowError(INVALID_DATE_RANGE)
    if (end - start).days + 1 > EXPORT_MAX_DAYS:
        raise ExportWindowError(EXPORT_WINDOW_60_DAYS)
    if not extended_access and start < lookback_floor(local_today):
        raise ExportWindowError(EXPORT_LOOKBACK_6_MONTHS)
