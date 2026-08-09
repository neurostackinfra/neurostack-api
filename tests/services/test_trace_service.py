"""Unit tests for trace classification boundaries."""

import pytest

from src.services.trace_service import _is_conversational_trace


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
