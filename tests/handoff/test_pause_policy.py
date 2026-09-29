"""Contract matrix for the shared post-handoff pause resolver."""

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from src.handoff.pause_policy import (
    PauseAction,
    parse_legacy_cycle_days,
    resolve_handoff_pause,
)

NOW = datetime(2026, 9, 29, 12, 30, tzinfo=timezone.utc)
LONG_DIGIT_DAYS = "9" * 5000

RUNTIME_POLICY_CASES = [
    (None, PauseAction.PAUSE_FOREVER, None, ()),
    ("not-an-object", PauseAction.PAUSE_FOREVER, None, ()),
    ({}, PauseAction.PAUSE_FOREVER, None, ()),
    (
        {"handoff_active": " no ", "standby_mode": "inactive"},
        PauseAction.NO_PAUSE,
        None,
        (),
    ),
    (
        {"handoff_active": "yes", "standby_mode": "unknown"},
        PauseAction.PAUSE_FOREVER,
        None,
        ("invalid_enum",),
    ),
    (
        {
            "handoff_active": " YES ",
            "standby_mode": " INACTIVE ",
            "handoff_period": "cycle",
            "handoff_inactive_default_hours": 3,
            "handoff_cycle_hours": 2,
            "handoff_cycle_days": 9,
        },
        PauseAction.PAUSE_UNTIL,
        timedelta(hours=24),
        (),
    ),
    (
        {
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": "permanent",
            "handoff_cycle_hours": 2,
        },
        PauseAction.PAUSE_FOREVER,
        None,
        (),
    ),
    (
        {"handoff_active": "yes", "standby_mode": "active"},
        PauseAction.PAUSE_FOREVER,
        None,
        (),
    ),
    (
        {
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": [],
        },
        PauseAction.PAUSE_FOREVER,
        None,
        ("invalid_enum",),
    ),
    (
        {
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": "cycle",
            "handoff_cycle_hours": 6,
        },
        PauseAction.PAUSE_UNTIL,
        timedelta(hours=6),
        (),
    ),
    (
        {
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": "cycle",
            "handoff_cycle_days": "12",
        },
        PauseAction.PAUSE_UNTIL,
        timedelta(days=12),
        (),
    ),
    (
        {
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": "cycle",
        },
        PauseAction.PAUSE_UNTIL,
        timedelta(days=7),
        ("fallback_seven_days",),
    ),
    (
        {
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": "cycle",
            "handoff_cycle_hours": 5,
            "handoff_cycle_days": 9,
        },
        PauseAction.PAUSE_UNTIL,
        timedelta(hours=5),
        ("multiple_durations",),
    ),
    (
        {
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": "cycle",
            "handoff_cycle_hours": "5",
            "handoff_cycle_days": 9,
        },
        PauseAction.PAUSE_UNTIL,
        timedelta(days=9),
        ("multiple_durations",),
    ),
    (
        {
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": "cycle",
            "handoff_cycle_hours": True,
            "handoff_cycle_days": " 9 ",
        },
        PauseAction.PAUSE_UNTIL,
        timedelta(days=7),
        (
            "multiple_durations",
            "invalid_duration",
            "invalid_duration",
            "fallback_seven_days",
        ),
    ),
    (
        {
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": "cycle",
            "handoff_cycle_days": LONG_DIGIT_DAYS,
        },
        PauseAction.PAUSE_UNTIL,
        timedelta(days=7),
        ("invalid_duration", "fallback_seven_days"),
    ),
]
RUNTIME_POLICY_BUCKETS = [case[0] for case in RUNTIME_POLICY_CASES]


@pytest.mark.parametrize(
    ("bucket", "action", "delta", "notice_reasons"),
    RUNTIME_POLICY_CASES,
)
def test_runtime_resolver_matrix(
    bucket: Any,
    action: PauseAction,
    delta: timedelta | None,
    notice_reasons: tuple[str, ...],
) -> None:
    decision = resolve_handoff_pause(bucket, NOW)

    assert decision.action is action
    assert decision.should_pause is (action is not PauseAction.NO_PAUSE)
    assert decision.paused_until == (NOW + delta if delta is not None else None)
    assert tuple(notice.reason for notice in decision.notices) == notice_reasons


@pytest.mark.parametrize(
    "invalid_days",
    [" 7", "7 ", "+7", "7.0", True, 0, 366, 1.5, "１２"],
)
def test_only_ascii_digit_legacy_days_are_coerced(invalid_days: Any) -> None:
    decision = resolve_handoff_pause(
        {
            "handoff_active": "yes",
            "standby_mode": "active",
            "handoff_period": "cycle",
            "handoff_cycle_days": invalid_days,
        },
        NOW,
    )

    assert decision.paused_until == NOW + timedelta(days=7)
    assert decision.notices[-1].reason == "fallback_seven_days"


@pytest.mark.parametrize("active", [None, True, 1, "unknown", [], {}])
def test_any_non_yes_active_value_disables_pause(active: Any) -> None:
    decision = resolve_handoff_pause(
        {
            "handoff_active": active,
            "standby_mode": "inactive",
            "handoff_period": "cycle",
        },
        NOW,
    )

    assert decision.action is PauseAction.NO_PAUSE


@pytest.mark.parametrize(
    ("value", "expected"),
    [(1, 1), (365, 365), ("1", 1), ("0001", 1), ("000365", 365)],
)
def test_bounded_legacy_days_parser_accepts_canonical_values(
    value: Any, expected: int
) -> None:
    assert parse_legacy_cycle_days(value) == expected


@pytest.mark.parametrize(
    "value",
    [0, 366, True, 1.5, "0", "366", " 1", "+1", "1.0", "１２", LONG_DIGIT_DAYS],
)
def test_bounded_legacy_days_parser_rejects_invalid_values(value: Any) -> None:
    assert parse_legacy_cycle_days(value) is None
