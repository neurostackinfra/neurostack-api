"""Pure SLA snapshot, classification, aggregation, and period helpers."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Any, Literal

from neurostack_core.prompt import resolve_clinic_tz

SLA_FALLBACK_SECONDS = 600
_PERCENT_QUANTUM = Decimal("0.1")


class SlaCategory(str, Enum):
    """The exhaustive and disjoint SLA states for one handoff."""

    WITHIN_SLA = "within_sla"
    CONFIRMED_LATE = "confirmed_late"
    UNANSWERED_OVERDUE = "unanswered_overdue"
    MATURING = "maturing"
    CANCELLED_UNCONFIRMED = "cancelled_unconfirmed"
    UNMEASURED_LEGACY = "unmeasured_legacy"


FallbackReason = Literal["missing", "invalid"]
HandoffStatus = Literal[
    "pending",
    "accepted",
    "in_progress",
    "completed",
    "cancelled",
    "timeout",
]
_HANDOFF_STATUSES = frozenset(
    {"pending", "accepted", "in_progress", "completed", "cancelled", "timeout"}
)


@dataclass(frozen=True)
class SlaTargetResolution:
    """A snapshot value plus a bounded, safe-to-log fallback reason."""

    seconds: int
    fallback_reason: FallbackReason | None = None

    @property
    def used_fallback(self) -> bool:
        return self.fallback_reason is not None


@dataclass(frozen=True)
class SlaOccurrence:
    """Inputs required to classify an occurrence without persistence access."""

    created_at: datetime
    confirmed_at: datetime | None
    sla_target_seconds: int | None
    status: HandoffStatus

    def __post_init__(self) -> None:
        if self.status not in _HANDOFF_STATUSES:
            raise ValueError("status must be a valid handoff status")


@dataclass(frozen=True)
class SlaClassification:
    """Classification result retaining the facts needed for aggregation."""

    category: SlaCategory
    confirmed: bool
    elapsed_seconds: float | None
    deadline: datetime | None


@dataclass(frozen=True)
class SlaMetrics:
    """SLA aggregate over a classified cohort."""

    confirmed_count: int
    sla_eligible_count: int
    average_confirmed_seconds: float | None
    pct_within_sla: Decimal | None


@dataclass(frozen=True)
class SlaPeriod:
    """UTC half-open bounds and the clock used to classify open handoffs."""

    start_utc: datetime
    end_exclusive_utc: datetime
    as_of: datetime


def resolve_sla_target(handoff_config: Mapping[str, Any] | None) -> SlaTargetResolution:
    """Resolve a new occurrence's immutable target without rewriting config.

    Only a real integer number of minutes in the current 1..120 contract is
    accepted. Missing and malformed legacy values both fall back to ten
    minutes. The result intentionally exposes only a bounded reason label, not
    the raw configuration value, so a future caller can log it safely.
    """

    if handoff_config is None or "meta_minutos_atendimento" not in handoff_config:
        return SlaTargetResolution(SLA_FALLBACK_SECONDS, "missing")

    minutes = handoff_config["meta_minutos_atendimento"]
    if type(minutes) is not int or not 1 <= minutes <= 120:
        return SlaTargetResolution(SLA_FALLBACK_SECONDS, "invalid")
    return SlaTargetResolution(minutes * 60)


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def classify_sla(
    occurrence: SlaOccurrence,
    *,
    as_of: datetime,
) -> SlaClassification:
    """Classify one handoff according to its persisted SLA snapshot.

    Missing snapshots always remain legacy/unmeasured. For measured rows a
    confirmation wins over status (including cancellation), while an
    unconfirmed cancellation wins over the temporal open-state categories.
    """

    _require_aware(as_of, "as_of")
    _require_aware(occurrence.created_at, "created_at")
    if occurrence.confirmed_at is not None:
        _require_aware(occurrence.confirmed_at, "confirmed_at")

    created_at = occurrence.created_at.astimezone(timezone.utc)
    as_of_utc = as_of.astimezone(timezone.utc)
    confirmed_at = (
        occurrence.confirmed_at.astimezone(timezone.utc)
        if occurrence.confirmed_at is not None
        else None
    )
    confirmed = confirmed_at is not None
    if occurrence.sla_target_seconds is None:
        return SlaClassification(
            category=SlaCategory.UNMEASURED_LEGACY,
            confirmed=confirmed,
            elapsed_seconds=None,
            deadline=None,
        )
    if (
        type(occurrence.sla_target_seconds) is not int
        or not 60 <= occurrence.sla_target_seconds <= 7200
    ):
        raise ValueError("sla_target_seconds must be an integer from 60 to 7200")

    deadline = created_at + timedelta(seconds=occurrence.sla_target_seconds)
    if confirmed_at is not None:
        elapsed_seconds = (confirmed_at - created_at).total_seconds()
        if elapsed_seconds < 0:
            raise ValueError("confirmed_at must not precede created_at")
        category = (
            SlaCategory.WITHIN_SLA
            if confirmed_at <= deadline
            else SlaCategory.CONFIRMED_LATE
        )
        return SlaClassification(
            category=category,
            confirmed=True,
            elapsed_seconds=elapsed_seconds,
            deadline=deadline,
        )

    if occurrence.status == "cancelled":
        category = SlaCategory.CANCELLED_UNCONFIRMED
    elif as_of_utc >= deadline:
        category = SlaCategory.UNANSWERED_OVERDUE
    else:
        category = SlaCategory.MATURING
    return SlaClassification(
        category=category,
        confirmed=False,
        elapsed_seconds=None,
        deadline=deadline,
    )


def aggregate_sla(classifications: Iterable[SlaClassification]) -> SlaMetrics:
    """Aggregate classifications with decimal half-up percentage rounding."""

    cohort = tuple(classifications)
    eligible_categories = {
        SlaCategory.WITHIN_SLA,
        SlaCategory.CONFIRMED_LATE,
        SlaCategory.UNANSWERED_OVERDUE,
    }
    eligible = [item for item in cohort if item.category in eligible_categories]
    confirmed_count = sum(
        item.category in {SlaCategory.WITHIN_SLA, SlaCategory.CONFIRMED_LATE}
        for item in cohort
    )
    measurable_confirmed = [
        item.elapsed_seconds
        for item in cohort
        if item.confirmed and item.elapsed_seconds is not None
    ]
    average = (
        sum(measurable_confirmed) / len(measurable_confirmed)
        if measurable_confirmed
        else None
    )
    if eligible:
        within_count = sum(item.category is SlaCategory.WITHIN_SLA for item in eligible)
        percentage = (
            Decimal(within_count) * Decimal(100) / Decimal(len(eligible))
        ).quantize(_PERCENT_QUANTUM, rounding=ROUND_HALF_UP)
    else:
        percentage = None
    return SlaMetrics(
        confirmed_count=confirmed_count,
        sla_eligible_count=len(eligible),
        average_confirmed_seconds=average,
        pct_within_sla=percentage,
    )


def resolve_sla_period(
    start_date: date,
    end_date: date,
    clinic_timezone: str | None,
    *,
    as_of: datetime,
) -> SlaPeriod:
    """Resolve inclusive clinic-local UI dates into UTC half-open bounds."""

    if isinstance(start_date, datetime) or isinstance(end_date, datetime):
        raise TypeError("start_date and end_date must be civil dates")
    if end_date < start_date:
        raise ValueError("end_date must not precede start_date")
    if end_date == date.max:
        raise ValueError("end_date must be earlier than date.max")
    _require_aware(as_of, "as_of")

    clinic_tz = resolve_clinic_tz(clinic_timezone)
    local_start = datetime.combine(start_date, time.min, tzinfo=clinic_tz)
    local_end = datetime.combine(
        end_date + timedelta(days=1),
        time.min,
        tzinfo=clinic_tz,
    )
    return SlaPeriod(
        start_utc=local_start.astimezone(timezone.utc),
        end_exclusive_utc=local_end.astimezone(timezone.utc),
        as_of=as_of.astimezone(timezone.utc),
    )
