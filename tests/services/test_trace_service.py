"""Unit tests for trace classification boundaries."""

import pytest

from src.services.trace_service import _is_conversational_trace, _llm_usage_for_stats


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
