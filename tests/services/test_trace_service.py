"""Unit tests for trace service boundaries."""

from datetime import date, timedelta, timezone

import pytest

from src.services.trace_service import (
    _is_conversational_trace,
    _llm_usage_for_stats,
    daily_utc_window,
)


def test_decision_key_with_empty_object_is_conversational() -> None:
    assert _is_conversational_trace({"decision": {}}, {}) is True


def test_unknown_intent_is_conversational() -> None:
    assert _is_conversational_trace({"intent": "unknown"}, {}) is True


def test_content_only_event_is_not_conversational() -> None:
    assert _is_conversational_trace({"content": "Olá"}, {}) is False


def test_metadata_only_marker_is_conversational() -> None:
    assert (
        _is_conversational_trace(
            {"content": "Olá"},
            {"intent_category": "scheduling"},
        )
        is True
    )


def test_metadata_only_decision_is_not_conversational() -> None:
    assert (
        _is_conversational_trace(
            {"content": "Olá"},
            {"decision": {}},
        )
        is False
    )


@pytest.mark.parametrize("marker", ["funnel_stage", "action", "confidence"])
def test_non_intent_positive_markers_are_conversational(marker: str) -> None:
    assert _is_conversational_trace({marker: None}, {}) is True


def test_llm_stats_uses_total_and_attempts_without_legacy_double_count() -> None:
    total, breakdown = _llm_usage_for_stats(
        {
            "llm_usage_total": {"total_tokens": 30},
            "llm_usage": {"total_tokens": 999},
            "llm_model": "legacy-should-not-count",
            "llm_attempts": [
                {
                    "usage_status": "reported",
                    "usage": {"total_tokens": 10},
                    "reported_model": "provider-model",
                    "requested_model": "requested-model",
                },
                {
                    "usage_status": "reported",
                    "usage": {"total_tokens": 20},
                    "reported_model": "",
                    "requested_model": "fallback-requested",
                },
            ],
        }
    )

    assert total == 30
    assert breakdown == [("provider-model", 10), ("fallback-requested", 20)]


def test_llm_stats_uses_legacy_usage_exactly_once_when_new_fields_absent() -> None:
    assert _llm_usage_for_stats(
        {"llm_usage": {"total_tokens": 7}, "llm_model": "legacy-model"}
    ) == (7, [("legacy-model", 7)])


@pytest.mark.parametrize(
    ("day", "expected_hours"),
    [
        (date(2026, 3, 8), 23),
        (date(2026, 11, 1), 25),
    ],
)
def test_daily_utc_window_respects_dst(day: date, expected_hours: int) -> None:
    start, end = daily_utc_window(day, "America/New_York")

    assert end - start == timedelta(hours=expected_hours)


@pytest.mark.parametrize("timezone_name", ["UTC", "America/Sao_Paulo"])
def test_daily_utc_window_accepts_iana_timezones(timezone_name: str) -> None:
    start, end = daily_utc_window(date(2026, 9, 28), timezone_name)

    assert start.tzinfo is timezone.utc
    assert end - start == timedelta(hours=24)


@pytest.mark.parametrize("timezone_name", ["localtime", "posixrules"])
def test_daily_utc_window_rejects_host_pseudo_timezones(timezone_name: str) -> None:
    with pytest.raises(KeyError):
        daily_utc_window(date(2026, 9, 28), timezone_name)
