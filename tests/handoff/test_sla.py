"""Unit tests for immutable handoff SLA semantics."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from src.handoff.models import HandoffDetail
from src.handoff.sla import (
    DEFAULT_CLINIC_TIMEZONE,
    DEFAULT_SLA_TARGET_SECONDS,
    SlaCategory,
    classify_handoff_sla,
    resolve_clinic_civil_date_range,
    resolve_clinic_civil_period,
    resolve_sla_target_seconds,
    round_half_up_percentage,
)
from src.models.clinic_config import ClinicHandoff

UTC = timezone.utc
CREATED_AT = datetime(2026, 9, 28, 12, tzinfo=UTC)


@pytest.mark.parametrize("minutes", [1, 120])
def test_clinic_handoff_accepts_strict_minute_boundaries(minutes: int) -> None:
    assert (
        ClinicHandoff(meta_minutos_atendimento=minutes).meta_minutos_atendimento
        == minutes
    )


@pytest.mark.parametrize("value", [0, 121, True, 10.0, "10", None])
def test_clinic_handoff_rejects_invalid_or_coerced_minutes(value: object) -> None:
    with pytest.raises(ValidationError):
        ClinicHandoff(meta_minutos_atendimento=value)  # type: ignore[arg-type]


def test_clinic_handoff_absent_value_keeps_valid_default() -> None:
    assert ClinicHandoff().meta_minutos_atendimento == 10


@pytest.mark.parametrize(
    ("raw_value", "expected_seconds"),
    [
        (1, 60),
        (120, 7200),
        (10, 600),
        (None, DEFAULT_SLA_TARGET_SECONDS),
        (0, DEFAULT_SLA_TARGET_SECONDS),
        (121, DEFAULT_SLA_TARGET_SECONDS),
        (True, DEFAULT_SLA_TARGET_SECONDS),
        (10.0, DEFAULT_SLA_TARGET_SECONDS),
        ("10", DEFAULT_SLA_TARGET_SECONDS),
    ],
)
def test_legacy_sla_resolution_is_pure_and_safe(
    raw_value: object, expected_seconds: int
) -> None:
    assert resolve_sla_target_seconds(raw_value) == expected_seconds


@pytest.mark.parametrize(
    ("confirmed_after", "status", "as_of_after", "expected"),
    [
        (599, "in_progress", 700, SlaCategory.WITHIN_SLA),
        (600, "completed", 700, SlaCategory.WITHIN_SLA),
        (601, "completed", 700, SlaCategory.CONFIRMED_LATE),
        (None, "pending", 599, SlaCategory.MATURING),
        (None, "accepted", 600, SlaCategory.UNANSWERED_OVERDUE),
        (None, "pending", 601, SlaCategory.UNANSWERED_OVERDUE),
        (None, "timeout", 599, SlaCategory.MATURING),
        (None, "timeout", 600, SlaCategory.UNANSWERED_OVERDUE),
        (None, "cancelled", 700, SlaCategory.CANCELLED_UNCONFIRMED),
        (599, "cancelled", 700, SlaCategory.WITHIN_SLA),
        (601, "cancelled", 700, SlaCategory.CONFIRMED_LATE),
    ],
)
def test_classification_matrix(
    confirmed_after: int | None,
    status: str,
    as_of_after: int,
    expected: SlaCategory,
) -> None:
    confirmed_at = (
        CREATED_AT + timedelta(seconds=confirmed_after)
        if confirmed_after is not None
        else None
    )

    assert (
        classify_handoff_sla(
            created_at=CREATED_AT,
            confirmed_at=confirmed_at,
            target_seconds=600,
            status=status,
            as_of=CREATED_AT + timedelta(seconds=as_of_after),
        )
        is expected
    )


def test_legacy_snapshot_has_precedence_over_status_and_confirmation() -> None:
    assert (
        classify_handoff_sla(
            created_at=CREATED_AT,
            confirmed_at=CREATED_AT + timedelta(seconds=1),
            target_seconds=None,
            status="cancelled",
            as_of=CREATED_AT + timedelta(hours=1),
        )
        is SlaCategory.UNMEASURED_LEGACY
    )


@pytest.mark.parametrize("target_seconds", [600, None])
def test_classifier_rejects_confirmation_before_creation(
    target_seconds: int | None,
) -> None:
    with pytest.raises(
        ValueError,
        match="^confirmed_at must not precede created_at$",
    ):
        classify_handoff_sla(
            created_at=CREATED_AT,
            confirmed_at=CREATED_AT - timedelta(microseconds=1),
            target_seconds=target_seconds,
            status="completed",
            as_of=CREATED_AT,
        )


def test_categories_are_disjoint_and_exhaustive_for_normative_cohort() -> None:
    cases = [
        (599, "completed", 900, 600),
        (600, "in_progress", 900, 600),
        (601, "completed", 900, 600),
        (None, "timeout", 600, 600),
        (None, "pending", 601, 600),
        (None, "pending", 599, 600),
        (None, "cancelled", 900, 600),
        (None, "cancelled", 900, None),
    ]
    categories = [
        classify_handoff_sla(
            created_at=CREATED_AT,
            confirmed_at=(
                CREATED_AT + timedelta(seconds=confirmed_after)
                if confirmed_after is not None
                else None
            ),
            target_seconds=target_seconds,
            status=status,
            as_of=CREATED_AT + timedelta(seconds=as_of_after),
        )
        for confirmed_after, status, as_of_after, target_seconds in cases
    ]

    assert len(categories) == len(cases)
    assert set(categories) == set(SlaCategory)
    assert categories.count(SlaCategory.WITHIN_SLA) == 2
    assert categories.count(SlaCategory.CONFIRMED_LATE) == 1
    assert categories.count(SlaCategory.UNANSWERED_OVERDUE) == 2
    assert categories.count(SlaCategory.MATURING) == 1
    assert categories.count(SlaCategory.CANCELLED_UNCONFIRMED) == 1
    assert categories.count(SlaCategory.UNMEASURED_LEGACY) == 1


@pytest.mark.parametrize("field", ["created_at", "confirmed_at", "as_of"])
def test_classifier_rejects_naive_datetimes(field: str) -> None:
    values = {
        "created_at": CREATED_AT,
        "confirmed_at": CREATED_AT + timedelta(seconds=1),
        "target_seconds": 600,
        "status": "completed",
        "as_of": CREATED_AT + timedelta(seconds=2),
    }
    values[field] = values[field].replace(tzinfo=None)  # type: ignore[union-attr]

    with pytest.raises(ValueError, match="timezone-aware"):
        classify_handoff_sla(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("target_seconds", [0, -1, 59, 7201, 10**100, True, 60.0])
def test_classifier_rejects_invalid_nonlegacy_targets(target_seconds: object) -> None:
    with pytest.raises(
        ValueError,
        match="^target_seconds must be an integer between 60 and 7200 or None$",
    ):
        classify_handoff_sla(
            created_at=CREATED_AT,
            confirmed_at=None,
            target_seconds=target_seconds,  # type: ignore[arg-type]
            status="pending",
            as_of=CREATED_AT,
        )


def test_classifier_rejects_unknown_status() -> None:
    with pytest.raises(ValueError, match="^status must be a normative handoff status$"):
        classify_handoff_sla(
            created_at=CREATED_AT,
            confirmed_at=None,
            target_seconds=600,
            status="unknown",
            as_of=CREATED_AT,
        )


@pytest.mark.parametrize("confirmed", [True, False])
def test_classifier_uses_elapsed_instant_across_spring_forward(
    confirmed: bool,
) -> None:
    zone = ZoneInfo("America/New_York")
    created_at = datetime(2026, 3, 8, 1, 30, tzinfo=zone)
    as_of = datetime(2026, 3, 8, 3, 0, tzinfo=zone)
    confirmed_at = as_of if confirmed else None

    assert (as_of.replace(tzinfo=None) - created_at.replace(tzinfo=None)) == timedelta(
        seconds=5400
    )
    assert as_of.astimezone(UTC) - created_at.astimezone(UTC) == timedelta(seconds=1800)
    expected = SlaCategory.WITHIN_SLA if confirmed else SlaCategory.MATURING
    assert (
        classify_handoff_sla(
            created_at=created_at,
            confirmed_at=confirmed_at,
            target_seconds=3600,
            status="completed" if confirmed else "pending",
            as_of=as_of,
        )
        is expected
    )


@pytest.mark.parametrize("confirmed", [True, False])
def test_classifier_uses_elapsed_instant_across_fall_back(confirmed: bool) -> None:
    zone = ZoneInfo("America/New_York")
    created_at = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    as_of = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=1)
    confirmed_at = as_of if confirmed else None

    assert as_of.replace(tzinfo=None) - created_at.replace(tzinfo=None) == timedelta(0)
    assert as_of.astimezone(UTC) - created_at.astimezone(UTC) == timedelta(seconds=3600)
    expected = (
        SlaCategory.CONFIRMED_LATE if confirmed else SlaCategory.UNANSWERED_OVERDUE
    )
    assert (
        classify_handoff_sla(
            created_at=created_at,
            confirmed_at=confirmed_at,
            target_seconds=1800,
            status="completed" if confirmed else "pending",
            as_of=as_of,
        )
        is expected
    )


def test_percentage_uses_round_half_up_and_preserves_no_sample() -> None:
    assert round_half_up_percentage(2, 5) == Decimal("40.0")
    assert round_half_up_percentage(2, 3) == Decimal("66.7")
    assert round_half_up_percentage(1, 16) == Decimal("6.3")
    assert round_half_up_percentage(0, 0) is None


@pytest.mark.parametrize(
    ("numerator", "denominator", "error"),
    [(-1, 1, ValueError), (2, 1, ValueError), (True, 1, TypeError)],
)
def test_percentage_rejects_invalid_counts(
    numerator: object, denominator: int, error: type[Exception]
) -> None:
    with pytest.raises(error):
        round_half_up_percentage(numerator, denominator)  # type: ignore[arg-type]


def test_period_uses_clinic_timezone_and_single_as_of_for_default_day() -> None:
    as_of = datetime(2026, 9, 28, 1, tzinfo=UTC)

    period = resolve_clinic_civil_period(
        clinic_timezone="America/Sao_Paulo",
        as_of=as_of,
    )

    assert period.local_day == date(2026, 9, 27)
    assert period.timezone_name == "America/Sao_Paulo"
    assert period.start_utc == datetime(2026, 9, 27, 3, tzinfo=UTC)
    assert period.end_exclusive_utc == datetime(2026, 9, 28, 3, tzinfo=UTC)
    assert period.start_utc.tzinfo is UTC
    assert period.end_exclusive_utc.tzinfo is UTC


@pytest.mark.parametrize("configured_timezone", [None, "", "Not/A_Real_Zone", 123])
def test_invalid_timezone_uses_sao_paulo_fallback(
    configured_timezone: object,
) -> None:
    period = resolve_clinic_civil_period(
        clinic_timezone=configured_timezone,
        local_day=date(2026, 9, 28),
        as_of=CREATED_AT,
    )

    assert period.timezone_name == DEFAULT_CLINIC_TIMEZONE
    assert period.start_utc == datetime(2026, 9, 28, 3, tzinfo=UTC)
    assert period.end_exclusive_utc == datetime(2026, 9, 29, 3, tzinfo=UTC)


@pytest.mark.parametrize(
    ("local_day", "expected_hours"),
    [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25)],
)
def test_period_observes_dst_day_length(local_day: date, expected_hours: int) -> None:
    period = resolve_clinic_civil_period(
        clinic_timezone="America/New_York",
        local_day=local_day,
        as_of=CREATED_AT,
    )

    assert period.end_exclusive_utc - period.start_utc == timedelta(
        hours=expected_hours
    )


def test_period_rejects_naive_as_of() -> None:
    with pytest.raises(ValueError, match="as_of must be timezone-aware"):
        resolve_clinic_civil_period(
            clinic_timezone="UTC",
            as_of=datetime(2026, 9, 28, 12),
        )


@pytest.mark.parametrize(
    ("local_day", "as_of"),
    [
        (date.max, CREATED_AT),
        (None, datetime.max.replace(tzinfo=UTC)),
    ],
)
def test_period_rejects_maximum_local_day(
    local_day: date | None, as_of: datetime
) -> None:
    with pytest.raises(ValueError, match="^local_day must be earlier than date.max$"):
        resolve_clinic_civil_period(
            clinic_timezone="UTC",
            local_day=local_day,
            as_of=as_of,
        )


def test_date_range_defaults_to_31_inclusive_dates_and_keeps_partial_defaults() -> None:
    as_of = datetime(2026, 9, 28, 12, tzinfo=UTC)

    default = resolve_clinic_civil_date_range(
        clinic_timezone="America/New_York",
        as_of=as_of,
    )
    from_only = resolve_clinic_civil_date_range(
        clinic_timezone="America/New_York",
        as_of=as_of,
        start_date=date(2026, 9, 1),
    )
    to_only = resolve_clinic_civil_date_range(
        clinic_timezone="America/New_York",
        as_of=as_of,
        end_date=date(2026, 9, 10),
    )

    assert (default.start_date, default.end_date) == (
        date(2026, 8, 29),
        date(2026, 9, 28),
    )
    assert (default.end_date - default.start_date).days == 30
    assert (from_only.start_date, from_only.end_date) == (
        date(2026, 9, 1),
        date(2026, 9, 28),
    )
    assert (to_only.start_date, to_only.end_date) == (
        date(2026, 8, 29),
        date(2026, 9, 10),
    )
    assert default.as_of == as_of


@pytest.mark.parametrize(
    ("local_day", "expected_hours"),
    [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25)],
)
def test_date_range_observes_dst_for_single_inclusive_date(
    local_day: date,
    expected_hours: int,
) -> None:
    period = resolve_clinic_civil_date_range(
        clinic_timezone="America/New_York",
        as_of=CREATED_AT,
        start_date=local_day,
        end_date=local_day,
    )

    assert period.end_exclusive_utc - period.start_utc == timedelta(
        hours=expected_hours
    )


def test_date_range_rejects_reversed_and_unrepresentable_boundaries() -> None:
    with pytest.raises(ValueError, match="end_date must not precede start_date"):
        resolve_clinic_civil_date_range(
            clinic_timezone="UTC",
            as_of=CREATED_AT,
            start_date=date(2026, 9, 29),
            end_date=date(2026, 9, 28),
        )

    with pytest.raises(OverflowError):
        resolve_clinic_civil_date_range(
            clinic_timezone="Asia/Tokyo",
            as_of=CREATED_AT,
            start_date=date.min,
            end_date=date.min,
        )


def test_lifecycle_version_remains_out_of_current_public_dto() -> None:
    assert "lifecycle_version" not in HandoffDetail.model_fields
