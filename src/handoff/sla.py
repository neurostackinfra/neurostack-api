"""Pure SLA classification and clinic-local period helpers for handoffs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_SLA_TARGET_SECONDS = 600
DEFAULT_CLINIC_TIMEZONE = "America/Sao_Paulo"
# Public queue/export windows use elapsed civil-date days. A difference of 90
# therefore remains valid (91 inclusive dates); a difference of 91 is rejected.
MAX_SLA_RANGE_ELAPSED_DAYS = 90
_NON_IANA_ZONE_KEYS = frozenset({"localtime", "posixrules"})
_NORMATIVE_HANDOFF_STATUSES = frozenset(
    {"pending", "accepted", "in_progress", "completed", "cancelled", "timeout"}
)


class SlaCategory(str, Enum):
    """One disjoint SLA category for a handoff in the selected cohort."""

    WITHIN_SLA = "within_sla"
    CONFIRMED_LATE = "confirmed_late"
    UNANSWERED_OVERDUE = "unanswered_overdue"
    MATURING = "maturing"
    CANCELLED_UNCONFIRMED = "cancelled_unconfirmed"
    UNMEASURED_LEGACY = "unmeasured_legacy"


@dataclass(frozen=True)
class CivilPeriod:
    """A clinic-local civil day represented as a semi-open UTC interval."""

    local_day: date
    timezone_name: str
    start_utc: datetime
    end_exclusive_utc: datetime


@dataclass(frozen=True)
class CivilDateRange:
    """Inclusive clinic-local dates represented by semi-open UTC bounds."""

    start_date: date
    end_date: date
    timezone_name: str
    start_utc: datetime
    end_exclusive_utc: datetime
    as_of: datetime


def resolve_sla_target_seconds(configured_minutes: object) -> int:
    """Resolve a raw legacy SLA setting to a safe snapshot in seconds.

    New configuration writes are validated by ``ClinicHandoff``. This helper
    deliberately accepts raw legacy JSON and falls back without mutating it.
    The lifecycle caller is responsible for logging the fallback without
    including patient data.
    """

    if (
        isinstance(configured_minutes, int)
        and not isinstance(configured_minutes, bool)
        and 1 <= configured_minutes <= 120
    ):
        return configured_minutes * 60
    return DEFAULT_SLA_TARGET_SECONDS


def classify_handoff_sla(
    *,
    created_at: datetime,
    confirmed_at: datetime | None,
    target_seconds: int | None,
    status: str,
    as_of: datetime,
) -> SlaCategory:
    """Classify one handoff into exactly one normative SLA category."""

    _require_aware("created_at", created_at)
    _require_aware("as_of", as_of)
    if confirmed_at is not None:
        _require_aware("confirmed_at", confirmed_at)

    if target_seconds is not None and (
        not isinstance(target_seconds, int)
        or isinstance(target_seconds, bool)
        or not 60 <= target_seconds <= 7200
    ):
        raise ValueError(
            "target_seconds must be an integer between 60 and 7200 or None"
        )
    if status not in _NORMATIVE_HANDOFF_STATUSES:
        raise ValueError("status must be a normative handoff status")

    created_utc = created_at.astimezone(timezone.utc)
    confirmed_utc = (
        confirmed_at.astimezone(timezone.utc) if confirmed_at is not None else None
    )
    as_of_utc = as_of.astimezone(timezone.utc)
    if confirmed_utc is not None and confirmed_utc < created_utc:
        raise ValueError("confirmed_at must not precede created_at")
    if target_seconds is None:
        return SlaCategory.UNMEASURED_LEGACY

    if confirmed_utc is not None:
        elapsed = (confirmed_utc - created_utc).total_seconds()
        if elapsed <= target_seconds:
            return SlaCategory.WITHIN_SLA
        return SlaCategory.CONFIRMED_LATE

    if status == "cancelled":
        return SlaCategory.CANCELLED_UNCONFIRMED

    deadline = created_utc + timedelta(seconds=target_seconds)
    if as_of_utc >= deadline:
        return SlaCategory.UNANSWERED_OVERDUE
    return SlaCategory.MATURING


def round_half_up_percentage(numerator: int, denominator: int) -> Decimal | None:
    """Return a percentage rounded half-up to one decimal, or no sample."""

    if (
        isinstance(numerator, bool)
        or isinstance(denominator, bool)
        or not isinstance(numerator, int)
        or not isinstance(denominator, int)
    ):
        raise TypeError("percentage counts must be integers")
    if numerator < 0 or denominator < 0 or numerator > denominator:
        raise ValueError("percentage counts must satisfy 0 <= numerator <= denominator")
    if denominator == 0:
        return None

    percentage = Decimal(numerator) * Decimal(100) / Decimal(denominator)
    return percentage.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)


def resolve_clinic_civil_period(
    *,
    clinic_timezone: object,
    as_of: datetime,
    local_day: date | None = None,
) -> CivilPeriod:
    """Resolve one clinic-local civil day to a semi-open UTC interval.

    ``as_of`` is required so callers capture the clock once. When no day is
    selected, that same instant determines the clinic's current civil date.
    Invalid or missing configuration uses the product fallback timezone.
    """

    _require_aware("as_of", as_of)
    zone_name, zone = _resolve_clinic_zone(clinic_timezone)
    resolved_day = local_day or as_of.astimezone(zone).date()
    if resolved_day == date.max:
        raise ValueError("local_day must be earlier than date.max")
    next_day = resolved_day + timedelta(days=1)
    start_local = datetime.combine(resolved_day, time.min, tzinfo=zone)
    end_local = datetime.combine(next_day, time.min, tzinfo=zone)
    return CivilPeriod(
        local_day=resolved_day,
        timezone_name=zone_name,
        start_utc=start_local.astimezone(timezone.utc),
        end_exclusive_utc=end_local.astimezone(timezone.utc),
    )


def resolve_clinic_civil_date_range(
    *,
    clinic_timezone: object,
    as_of: datetime,
    start_date: date | None = None,
    end_date: date | None = None,
) -> CivilDateRange:
    """Resolve inclusive clinic dates, defaulting to the last 31 civil dates.

    Partial ranges retain the missing side's default: thirty clinic dates before
    today for ``start_date`` and the clinic's current date for ``end_date``.
    The caller may translate representational boundary failures into its public
    error contract before issuing a cohort query.
    """

    _require_aware("as_of", as_of)
    if start_date is not None and type(start_date) is not date:
        raise TypeError("start_date must be a civil date")
    if end_date is not None and type(end_date) is not date:
        raise TypeError("end_date must be a civil date")

    zone_name, zone = _resolve_clinic_zone(clinic_timezone)
    resolved_as_of = as_of.astimezone(timezone.utc)
    local_today = resolved_as_of.astimezone(zone).date()
    resolved_start = start_date or local_today - timedelta(days=30)
    resolved_end = end_date or local_today
    if resolved_end < resolved_start:
        raise ValueError("end_date must not precede start_date")
    if resolved_end == date.max:
        raise ValueError("end_date must be earlier than date.max")

    start_local = datetime.combine(resolved_start, time.min, tzinfo=zone)
    end_local = datetime.combine(
        resolved_end + timedelta(days=1),
        time.min,
        tzinfo=zone,
    )
    return CivilDateRange(
        start_date=resolved_start,
        end_date=resolved_end,
        timezone_name=zone_name,
        start_utc=start_local.astimezone(timezone.utc),
        end_exclusive_utc=end_local.astimezone(timezone.utc),
        as_of=resolved_as_of,
    )


def _resolve_clinic_zone(configured_timezone: object) -> tuple[str, ZoneInfo]:
    if isinstance(configured_timezone, str):
        candidate = configured_timezone.strip()
        if candidate and candidate not in _NON_IANA_ZONE_KEYS:
            try:
                return candidate, ZoneInfo(candidate)
            except (ZoneInfoNotFoundError, ValueError):
                pass
    return DEFAULT_CLINIC_TIMEZONE, ZoneInfo(DEFAULT_CLINIC_TIMEZONE)


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
