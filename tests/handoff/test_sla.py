from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from src.handoff.sla import (
    HandoffStatus,
    SlaCategory,
    SlaOccurrence,
    aggregate_sla,
    classify_sla,
    resolve_sla_period,
    resolve_sla_target,
)
from src.models.clinic_config import ClinicHandoff

UTC = timezone.utc
CREATED_AT = datetime(2026, 1, 10, 12, tzinfo=UTC)


def _occurrence(
    *,
    confirmed_after: int | None = None,
    target: int | None = 600,
    status: HandoffStatus = "pending",
) -> SlaOccurrence:
    confirmed_at = (
        CREATED_AT + timedelta(seconds=confirmed_after)
        if confirmed_after is not None
        else None
    )
    return SlaOccurrence(
        created_at=CREATED_AT,
        confirmed_at=confirmed_at,
        sla_target_seconds=target,
        status=status,
    )


@pytest.mark.parametrize("status", ["pendng", "unknown", ""])
def test_occurrence_rejects_unknown_status_at_runtime(status: str) -> None:
    with pytest.raises(ValueError, match="valid handoff status"):
        _occurrence(status=status)  # type: ignore[arg-type]


@pytest.mark.parametrize("minutes", [1, 10, 120])
def test_new_config_accepts_only_in_range_real_integers(minutes: int) -> None:
    config = ClinicHandoff(meta_minutos_atendimento=minutes)

    assert config.meta_minutos_atendimento == minutes


@pytest.mark.parametrize(
    "value",
    [0, 121, True, False, 1.0, "10", None],
    ids=["zero", "too-high", "true", "false", "float", "string", "null"],
)
def test_new_config_rejects_invalid_or_coercible_minutes(value: object) -> None:
    with pytest.raises(ValidationError):
        ClinicHandoff(meta_minutos_atendimento=value)  # type: ignore[arg-type]


def test_config_omission_keeps_default_and_legacy_values_fallback_safely() -> None:
    assert ClinicHandoff().meta_minutos_atendimento == 10
    missing = resolve_sla_target({})
    absent_bucket = resolve_sla_target(None)

    assert missing.seconds == absent_bucket.seconds == 600
    assert missing.fallback_reason == absent_bucket.fallback_reason == "missing"
    assert missing.used_fallback is True

    for value in (0, 121, True, 10.0, "10", None):
        legacy = {"meta_minutos_atendimento": value}
        before = dict(legacy)

        resolution = resolve_sla_target(legacy)

        assert resolution.seconds == 600
        assert resolution.fallback_reason == "invalid"
        assert legacy == before

    valid = resolve_sla_target({"meta_minutos_atendimento": 120})
    assert valid.seconds == 7200
    assert valid.fallback_reason is None
    assert valid.used_fallback is False


@pytest.mark.parametrize(
    ("confirmed_after", "expected"),
    [
        (599, SlaCategory.WITHIN_SLA),
        (600, SlaCategory.WITHIN_SLA),
        (601, SlaCategory.CONFIRMED_LATE),
    ],
)
def test_confirmed_boundary_matrix(
    confirmed_after: int,
    expected: SlaCategory,
) -> None:
    result = classify_sla(
        _occurrence(confirmed_after=confirmed_after),
        as_of=CREATED_AT + timedelta(hours=1),
    )

    assert result.category is expected
    assert result.confirmed is True
    assert result.elapsed_seconds == confirmed_after
    assert result.deadline == CREATED_AT + timedelta(seconds=600)


@pytest.mark.parametrize("target", [60, 7200])
def test_sla_target_seconds_accepts_valid_boundaries(target: int) -> None:
    result = classify_sla(
        _occurrence(target=target),
        as_of=CREATED_AT,
    )

    assert result.deadline == CREATED_AT + timedelta(seconds=target)


@pytest.mark.parametrize(
    "target",
    [59, 7201, True, False, 600.0, "600"],
    ids=["below-min", "above-max", "true", "false", "float", "string"],
)
def test_sla_target_seconds_rejects_invalid_values(target: object) -> None:
    with pytest.raises(
        ValueError,
        match="sla_target_seconds must be an integer from 60 to 7200",
    ):
        classify_sla(
            _occurrence(target=target),  # type: ignore[arg-type]
            as_of=CREATED_AT,
        )


@pytest.mark.parametrize(
    ("created_at", "confirmed_at", "expected_deadline"),
    [
        (
            datetime(2026, 3, 8, 1, 55),
            datetime(2026, 3, 8, 3, 5),
            datetime(2026, 3, 8, 7, 5, tzinfo=UTC),
        ),
        (
            datetime(2026, 11, 1, 1, 55, fold=0),
            datetime(2026, 11, 1, 1, 5, fold=1),
            datetime(2026, 11, 1, 6, 5, tzinfo=UTC),
        ),
    ],
    ids=["spring-forward", "fall-back"],
)
def test_confirmed_classification_uses_elapsed_instants_across_dst(
    created_at: datetime,
    confirmed_at: datetime,
    expected_deadline: datetime,
) -> None:
    clinic_tz = ZoneInfo("America/New_York")
    occurrence = SlaOccurrence(
        created_at=created_at.replace(tzinfo=clinic_tz),
        confirmed_at=confirmed_at.replace(tzinfo=clinic_tz),
        sla_target_seconds=600,
        status="pending",
    )

    result = classify_sla(occurrence, as_of=occurrence.confirmed_at)  # type: ignore[arg-type]

    assert result.category is SlaCategory.WITHIN_SLA
    assert result.elapsed_seconds == 600
    assert result.deadline == expected_deadline


@pytest.mark.parametrize(
    ("created_at", "as_of", "expected"),
    [
        (
            datetime(2026, 3, 8, 1, 55),
            datetime(2026, 3, 8, 3, 4),
            SlaCategory.MATURING,
        ),
        (
            datetime(2026, 11, 1, 1, 55, fold=0),
            datetime(2026, 11, 1, 1, 6, fold=1),
            SlaCategory.UNANSWERED_OVERDUE,
        ),
    ],
    ids=["spring-forward", "fall-back"],
)
def test_open_classification_uses_elapsed_instants_across_dst(
    created_at: datetime,
    as_of: datetime,
    expected: SlaCategory,
) -> None:
    clinic_tz = ZoneInfo("America/New_York")
    occurrence = SlaOccurrence(
        created_at=created_at.replace(tzinfo=clinic_tz),
        confirmed_at=None,
        sla_target_seconds=600,
        status="pending",
    )

    result = classify_sla(
        occurrence,
        as_of=as_of.replace(tzinfo=clinic_tz),
    )

    assert result.category is expected


@pytest.mark.parametrize("status", ["cancelled", "timeout", "completed"])
def test_confirmation_precedes_status(status: str) -> None:
    result = classify_sla(
        _occurrence(confirmed_after=601, status=status),
        as_of=CREATED_AT + timedelta(seconds=700),
    )

    assert result.category is SlaCategory.CONFIRMED_LATE


def test_unconfirmed_deadline_timeout_cancellation_and_maturing_matrix() -> None:
    before = classify_sla(
        _occurrence(status="timeout"),
        as_of=CREATED_AT + timedelta(seconds=599),
    )
    at_deadline = classify_sla(
        _occurrence(),
        as_of=CREATED_AT + timedelta(seconds=600),
    )
    after = classify_sla(
        _occurrence(status="timeout"),
        as_of=CREATED_AT + timedelta(seconds=601),
    )
    cancelled = classify_sla(
        _occurrence(status="cancelled"),
        as_of=CREATED_AT + timedelta(days=1),
    )

    assert before.category is SlaCategory.MATURING
    assert at_deadline.category is SlaCategory.UNANSWERED_OVERDUE
    assert after.category is SlaCategory.UNANSWERED_OVERDUE
    assert cancelled.category is SlaCategory.CANCELLED_UNCONFIRMED


@pytest.mark.parametrize(
    ("confirmed_after", "status"),
    [(None, "pending"), (None, "cancelled"), (100, "cancelled")],
)
def test_missing_snapshot_has_precedence_and_is_unmeasured(
    confirmed_after: int | None,
    status: str,
) -> None:
    result = classify_sla(
        _occurrence(
            confirmed_after=confirmed_after,
            target=None,
            status=status,
        ),
        as_of=CREATED_AT + timedelta(days=1),
    )

    assert result.category is SlaCategory.UNMEASURED_LEGACY
    assert result.confirmed is (confirmed_after is not None)
    assert result.elapsed_seconds is None
    assert result.deadline is None


def test_categories_are_exhaustive_disjoint_and_values_are_stable() -> None:
    assert {category.value for category in SlaCategory} == {
        "within_sla",
        "confirmed_late",
        "unanswered_overdue",
        "maturing",
        "cancelled_unconfirmed",
        "unmeasured_legacy",
    }
    cases = [
        _occurrence(confirmed_after=599),
        _occurrence(confirmed_after=601),
        _occurrence(),
        _occurrence(status="cancelled"),
        _occurrence(target=None),
    ]
    categories = [
        classify_sla(case, as_of=CREATED_AT + timedelta(seconds=600)).category
        for case in cases
    ]
    assert categories == [
        SlaCategory.WITHIN_SLA,
        SlaCategory.CONFIRMED_LATE,
        SlaCategory.UNANSWERED_OVERDUE,
        SlaCategory.CANCELLED_UNCONFIRMED,
        SlaCategory.UNMEASURED_LEGACY,
    ]


def test_aggregate_normative_cohort_is_disjoint_and_exhaustive() -> None:
    classifications = [
        classify_sla(
            _occurrence(confirmed_after=300),
            as_of=CREATED_AT + timedelta(hours=1),
        ),
        classify_sla(
            _occurrence(confirmed_after=600),
            as_of=CREATED_AT + timedelta(hours=1),
        ),
        classify_sla(
            _occurrence(confirmed_after=900),
            as_of=CREATED_AT + timedelta(hours=1),
        ),
        classify_sla(
            _occurrence(status="timeout"),
            as_of=CREATED_AT + timedelta(hours=1),
        ),
        classify_sla(
            _occurrence(),
            as_of=CREATED_AT + timedelta(hours=1),
        ),
        classify_sla(
            _occurrence(),
            as_of=CREATED_AT + timedelta(seconds=599),
        ),
        classify_sla(
            _occurrence(status="cancelled"),
            as_of=CREATED_AT + timedelta(hours=1),
        ),
        classify_sla(
            _occurrence(confirmed_after=100, target=None),
            as_of=CREATED_AT + timedelta(hours=1),
        ),
    ]

    metrics = aggregate_sla(classifications)
    category_counts = {
        category: sum(item.category is category for item in classifications)
        for category in SlaCategory
    }

    assert metrics.confirmed_count == 3
    assert metrics.confirmed_count == (
        category_counts[SlaCategory.WITHIN_SLA]
        + category_counts[SlaCategory.CONFIRMED_LATE]
    )
    assert metrics.sla_eligible_count == 5
    assert metrics.average_confirmed_seconds == 600
    assert metrics.pct_within_sla == Decimal("40.0")
    assert category_counts == {
        SlaCategory.WITHIN_SLA: 2,
        SlaCategory.CONFIRMED_LATE: 1,
        SlaCategory.UNANSWERED_OVERDUE: 2,
        SlaCategory.MATURING: 1,
        SlaCategory.CANCELLED_UNCONFIRMED: 1,
        SlaCategory.UNMEASURED_LEGACY: 1,
    }
    assert sum(category_counts.values()) == len(classifications)


def test_aggregate_uses_decimal_half_up_and_handles_no_data() -> None:
    within = classify_sla(
        _occurrence(confirmed_after=1),
        as_of=CREATED_AT + timedelta(hours=1),
    )
    late = classify_sla(
        _occurrence(confirmed_after=601),
        as_of=CREATED_AT + timedelta(hours=1),
    )

    rounded = aggregate_sla([within] + [late] * 15)
    empty = aggregate_sla([])
    no_sample = aggregate_sla(
        [
            classify_sla(
                _occurrence(status="cancelled"),
                as_of=CREATED_AT + timedelta(hours=1),
            )
        ]
    )

    assert rounded.pct_within_sla == Decimal("6.3")
    assert empty.confirmed_count == empty.sla_eligible_count == 0
    assert empty.average_confirmed_seconds is None
    assert empty.pct_within_sla is None
    assert no_sample.average_confirmed_seconds is None
    assert no_sample.pct_within_sla is None


@pytest.mark.parametrize(
    ("factory", "message"),
    [
        (
            lambda: classify_sla(_occurrence(), as_of=datetime(2026, 1, 10)),
            "as_of",
        ),
        (
            lambda: classify_sla(
                SlaOccurrence(datetime(2026, 1, 10), None, 600, "pending"),
                as_of=CREATED_AT,
            ),
            "created_at",
        ),
        (
            lambda: resolve_sla_period(
                date(2026, 1, 1),
                date(2026, 1, 1),
                "UTC",
                as_of=datetime(2026, 1, 1),
            ),
            "as_of",
        ),
    ],
)
def test_clocks_must_be_timezone_aware(factory, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        factory()


def test_period_uses_clinic_timezone_not_browser_timezone_and_is_half_open() -> None:
    period = resolve_sla_period(
        date(2026, 1, 10),
        date(2026, 1, 12),
        "America/Sao_Paulo",
        as_of=datetime(2026, 1, 13, 9, tzinfo=timezone(timedelta(hours=-3))),
    )

    assert period.start_utc == datetime(2026, 1, 10, 3, tzinfo=UTC)
    assert period.end_exclusive_utc == datetime(2026, 1, 13, 3, tzinfo=UTC)
    assert period.as_of == datetime(2026, 1, 13, 12, tzinfo=UTC)
    # A browser in Tokyo would have produced 15:00 UTC on the previous day;
    # there is deliberately no browser-zone input to override the clinic.
    assert period.start_utc != datetime(2026, 1, 9, 15, tzinfo=UTC)


@pytest.mark.parametrize("clinic_timezone", [None, "Invalid/Timezone"])
def test_period_fallback_and_date_order_validation(
    clinic_timezone: str | None,
) -> None:
    fallback = resolve_sla_period(
        date(2026, 1, 10),
        date(2026, 1, 10),
        clinic_timezone,
        as_of=CREATED_AT,
    )
    assert fallback.start_utc == datetime(2026, 1, 10, 3, tzinfo=UTC)

    with pytest.raises(ValueError, match="end_date"):
        resolve_sla_period(
            date(2026, 1, 11),
            date(2026, 1, 10),
            "UTC",
            as_of=CREATED_AT,
        )


def test_period_rejects_date_max_with_stable_error() -> None:
    with pytest.raises(ValueError, match="^end_date must be earlier than date.max$"):
        resolve_sla_period(
            date.max,
            date.max,
            "UTC",
            as_of=CREATED_AT,
        )


@pytest.mark.parametrize(
    ("day", "expected_hours"),
    [(date(2026, 3, 8), 23), (date(2026, 11, 1), 25)],
)
def test_period_respects_dst_day_lengths(day: date, expected_hours: int) -> None:
    period = resolve_sla_period(
        day,
        day,
        "America/New_York",
        as_of=CREATED_AT,
    )

    assert period.end_exclusive_utc - period.start_utc == timedelta(
        hours=expected_hours
    )
