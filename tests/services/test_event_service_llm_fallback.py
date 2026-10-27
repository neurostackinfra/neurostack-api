"""LLM routing + attempts/total accounting tests (WISH Group 3).

These tests exercise the primary → multimodal-text fallback chain and the
canonical ``llm_attempts`` / ``llm_usage_total`` contract implemented in
``src.services.event_service``. The LLM factories are monkeypatched so a
scripted fake client controls exactly what the routing loop observes for
each attempt.

Contract under test (see WISH Group 3):

  * Primary is called at most twice on unusable content, at most once when
    the first primary attempt raises (JSON malformed / transport / HTTP).
  * Multimodal-text fallback is called at most once, with the SAME
    ``messages`` and ``max_tokens=300`` the primary used.
  * ``llm_attempts`` records one entry per initiated call (even when the
    await ends in an exception). Fields: ``tier``, ``attempt``, ``provider``,
    ``requested_model``, ``reported_model``, ``outcome``, ``accepted``,
    ``usage_status``, ``usage``. ``accepted`` is separate from ``outcome``
    and at most one entry across the list is ``True``.
  * ``_normalize_llm_usage`` accepts ONLY integer, non-negative, non-bool
    ``prompt_tokens`` / ``completion_tokens`` / ``total_tokens``. Missing
    fields are NEVER synthesised (``total_tokens`` is never computed from
    ``prompt_tokens + completion_tokens``). Invalid shape → ``invalid``;
    missing/exception → ``unavailable``.
  * ``llm_usage_total`` is derived exclusively as a deterministic fold over
    the ``reported`` attempts and is computed exactly once.
  * Prior behaviour is preserved exactly when the primary client / opt-in /
    operational bundle are absent (zero fallback calls).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

import src.services.event_service as event_service
from src.core.pipeline import PipelineResult
from src.services.event_service import (
    EventService,
    _fold_llm_usage_total,
    _is_usable_llm_content,
    _normalize_llm_usage,
)

# ============================================
# Pure-function tests: predicate, normalizer, fold
# ============================================


class TestIsUsableLLMContent:
    def test_non_mapping_is_unusable(self) -> None:
        assert _is_usable_llm_content(None) is False
        assert _is_usable_llm_content("string not a mapping") is False
        assert _is_usable_llm_content(42) is False
        assert _is_usable_llm_content([{"content": "hi"}]) is False

    def test_missing_content_is_unusable(self) -> None:
        assert _is_usable_llm_content({"usage": {}}) is False

    def test_non_string_content_is_unusable(self) -> None:
        assert _is_usable_llm_content({"content": None}) is False
        assert _is_usable_llm_content({"content": 42}) is False
        assert _is_usable_llm_content({"content": ["hi"]}) is False

    def test_empty_or_whitespace_string_is_unusable(self) -> None:
        assert _is_usable_llm_content({"content": ""}) is False
        assert _is_usable_llm_content({"content": "   "}) is False
        assert _is_usable_llm_content({"content": "\n\t "}) is False

    def test_non_empty_stripped_string_is_usable(self) -> None:
        assert _is_usable_llm_content({"content": "Olá!"}) is True
        assert _is_usable_llm_content({"content": "  hello  "}) is True


class TestNormalizeLLMUsage:
    def test_none_is_unavailable(self) -> None:
        status, normalized = _normalize_llm_usage(None)
        assert status == "unavailable"
        assert normalized == {}

    def test_present_non_mapping_is_invalid(self) -> None:
        # ``unavailable`` means the provider omitted usage. A supplied
        # scalar/list payload crossed the provider boundary but is malformed.
        assert _normalize_llm_usage("not a mapping")[0] == "invalid"
        assert _normalize_llm_usage(42)[0] == "invalid"
        assert _normalize_llm_usage([])[0] == "invalid"

    def test_empty_mapping_is_unavailable(self) -> None:
        status, normalized = _normalize_llm_usage({})
        assert status == "unavailable"
        assert normalized == {}

    def test_mapping_with_only_non_canonical_fields_is_unavailable(self) -> None:
        # `completion_tokens_details` etc. are provider-specific — not fields
        # we sum, so a mapping that lacks all three canonical fields is
        # treated as no reported usage at all.
        status, normalized = _normalize_llm_usage({"completion_tokens_details": {}})
        assert status == "unavailable"
        assert normalized == {}

    def test_all_three_fields_valid(self) -> None:
        status, normalized = _normalize_llm_usage(
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        )
        assert status == "reported"
        assert normalized == {
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
        }

    def test_missing_total_tokens_is_never_synthesised(self) -> None:
        # If total_tokens is missing, it stays missing — never computed from
        # prompt_tokens + completion_tokens.
        status, normalized = _normalize_llm_usage(
            {"prompt_tokens": 10, "completion_tokens": 5}
        )
        assert status == "reported"
        assert normalized == {"prompt_tokens": 10, "completion_tokens": 5}
        assert "total_tokens" not in normalized

    def test_only_total_tokens_present(self) -> None:
        status, normalized = _normalize_llm_usage({"total_tokens": 15})
        assert status == "reported"
        assert normalized == {"total_tokens": 15}

    def test_bool_true_is_rejected_as_invalid(self) -> None:
        # Python bool is a subclass of int — must be rejected explicitly.
        status, normalized = _normalize_llm_usage({"prompt_tokens": True})
        assert status == "invalid"
        assert normalized == {}

    def test_bool_false_is_rejected_as_invalid(self) -> None:
        status, normalized = _normalize_llm_usage({"total_tokens": False})
        assert status == "invalid"
        assert normalized == {}

    def test_float_is_rejected_as_invalid(self) -> None:
        status, normalized = _normalize_llm_usage({"prompt_tokens": 10.5})
        assert status == "invalid"
        assert normalized == {}

    def test_string_is_rejected_as_invalid(self) -> None:
        status, normalized = _normalize_llm_usage({"prompt_tokens": "10"})
        assert status == "invalid"
        assert normalized == {}

    def test_negative_is_rejected_as_invalid(self) -> None:
        status, normalized = _normalize_llm_usage({"prompt_tokens": -1})
        assert status == "invalid"
        assert normalized == {}

    def test_zero_is_accepted(self) -> None:
        status, normalized = _normalize_llm_usage(
            {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        )
        assert status == "reported"
        assert normalized == {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }


class TestFoldLLMUsageTotal:
    def test_empty_returns_empty(self) -> None:
        assert _fold_llm_usage_total([]) == {}

    def test_only_unavailable_entries_folds_to_empty(self) -> None:
        attempts = [
            {"usage_status": "unavailable", "usage": {}},
            {"usage_status": "unavailable", "usage": {"prompt_tokens": 99}},
        ]
        assert _fold_llm_usage_total(attempts) == {}

    def test_only_invalid_entries_folds_to_empty(self) -> None:
        attempts = [{"usage_status": "invalid", "usage": {"prompt_tokens": 5}}]
        assert _fold_llm_usage_total(attempts) == {}

    def test_reported_entries_are_summed(self) -> None:
        attempts = [
            {
                "usage_status": "reported",
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            },
            {
                "usage_status": "reported",
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 7,
                    "total_tokens": 10,
                },
            },
        ]
        assert _fold_llm_usage_total(attempts) == {
            "prompt_tokens": 13,
            "completion_tokens": 12,
            "total_tokens": 25,
        }

    def test_partial_fields_across_reported_entries_sum_independently(self) -> None:
        attempts = [
            {
                "usage_status": "reported",
                "usage": {"prompt_tokens": 5, "completion_tokens": 3},
            },
            {
                "usage_status": "reported",
                "usage": {"total_tokens": 20},
            },
        ]
        # total_tokens is NEVER inferred from prompt+completion, so it only
        # picks up the 20 that was actually reported.
        assert _fold_llm_usage_total(attempts) == {
            "prompt_tokens": 5,
            "completion_tokens": 3,
            "total_tokens": 20,
        }

    def test_invalid_and_reported_mixed(self) -> None:
        attempts = [
            {"usage_status": "reported", "usage": {"prompt_tokens": 4}},
            {"usage_status": "invalid", "usage": {}},
            {"usage_status": "reported", "usage": {"prompt_tokens": 6}},
        ]
        assert _fold_llm_usage_total(attempts) == {"prompt_tokens": 10}


# ============================================
# Attempt observability
# ============================================


@pytest.mark.parametrize("outcome", ["response", "empty", "invalid", "error"])
def test_closed_attempt_emits_one_safe_metric_and_event(
    monkeypatch: pytest.MonkeyPatch, outcome: str
) -> None:
    """Every terminal path has exactly one bounded counter and safe event."""
    metric_calls: list[dict[str, str]] = []
    log_calls: list[tuple[str, dict[str, Any]]] = []

    class _MetricChild:
        def inc(self) -> None:
            metric_calls.append({"incremented": "true"})

    class _Metric:
        def labels(self, **labels: str) -> _MetricChild:
            metric_calls.append(labels)
            return _MetricChild()

    class _Logger:
        def info(self, name: str, **fields: Any) -> None:
            log_calls.append((name, fields))

    monkeypatch.setattr(event_service, "LLM_GENERATION_ATTEMPTS_TOTAL", _Metric())
    monkeypatch.setattr(event_service, "logger", _Logger())
    attempts: list[dict[str, Any]] = []
    event_service._append_llm_attempt(
        attempts,
        {
            "tier": "primary",
            "attempt": 2,
            "provider": "openai",
            "requested_model": "gpt-requested",
            "reported_model": "gpt-reported",
            "outcome": outcome,
            "accepted": outcome == "response",
            "usage_status": "reported",
            "usage": {"total_tokens": 3},
        },
    )

    assert len(attempts) == 1
    assert metric_calls == [
        {"tier": "primary", "outcome": outcome},
        {"incremented": "true"},
    ]
    assert log_calls == [
        (
            "llm_generation_attempt",
            {
                "tier": "primary",
                "ordinal": 2,
                "provider": "openai",
                "requested_model": "gpt-requested",
                "reported_model": "gpt-reported",
                "outcome": outcome,
                "accepted": outcome == "response",
                "usage_status": "reported",
            },
        )
    ]
    serialized_fields = repr(log_calls[0][1]).lower()
    assert "prompt" not in serialized_fields
    assert "secret" not in serialized_fields
    assert "api_key" not in serialized_fields
    assert "response" not in log_calls[0][1]
    assert "Authorization" not in log_calls[0][1]


# ============================================
# Routing tests: end-to-end via _convert_pipeline_result_to_response
# ============================================


class _ScriptedLLMClient:
    """Fake LLMClient that returns a scripted sequence of results.

    Each entry in ``script`` is either a dict (returned as-is from
    ``generate``) or an exception instance (raised). Calls beyond the
    script length raise AssertionError so an over-invocation is loud.
    """

    def __init__(
        self,
        script: list[Any],
        *,
        label: str = "primary",
        recorded_calls: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        self._script = list(script)
        self._label = label
        self.calls: list[dict[str, Any]] = (
            recorded_calls if recorded_calls is not None else []
        )

    async def generate(
        self,
        messages: list[dict[str, str]],
        model: Optional[str] = None,
        max_tokens: Optional[int] = None,
        **_kwargs: Any,
    ) -> dict:
        # Record the exact args crossing the transport boundary so tests
        # can assert identical messages/max_tokens across primary+fallback.
        self.calls.append(
            {
                "label": self._label,
                "messages": messages,
                "model": model,
                "max_tokens": max_tokens,
            }
        )
        if not self._script:
            raise AssertionError(
                f"{self._label} client called more times than scripted"
            )
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _make_result() -> PipelineResult:
    return PipelineResult(
        tenant_id=uuid4(),
        session_id=uuid4(),
        intent="fallback_test",
        intent_category="scheduling",
        funnel_stage="consideration",
        action="respond",
        confidence=0.9,
        safety_concern=False,
        safety_category="none",
        needs_rag=False,
        handoff_required=False,
        system_prompt="You are a helpful assistant.",
        instructions="",
        mode="normal",
        state_patch={"slots": {}, "flags": {}, "last_intent": "consulta"},
    )


def _install_primary_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
    primary_script: Optional[list[Any]] = None,
    fallback_script: Optional[list[Any]] = None,
    *,
    primary_creds: Optional[dict] = None,
    fallback_creds: Optional[dict] = None,
) -> tuple[Optional[_ScriptedLLMClient], Optional[_ScriptedLLMClient]]:
    """Wire fake credentials + factories into the routing hot path.

    ``primary_script=None`` → the primary factory returns ``None`` so no
    primary attempts happen at all (used to exercise the "no primary
    client" preservation case).
    ``fallback_script=None`` → the multimodal-text factory returns ``None``
    so the fallback is silently unavailable (opt-in absent / operational
    bundle missing). No fallback attempts are recorded in that case.
    """

    async def _fake_primary_creds(_tenant_id: Any) -> dict:
        return primary_creds or {
            "llm_provider": "openai",
            "llm_api_url": "https://api.openai.com/v1",
            "llm_api_key": "PRIMARY-KEY",
            "llm_model_name": "gpt-primary",
        }

    async def _fake_fallback_creds(_tenant_id: Any) -> dict:
        return fallback_creds or {
            "multimodal_provider": "openrouter",
            "multimodal_api_url": "https://openrouter.ai/api/v1",
            "multimodal_api_key": "FALLBACK-KEY",
            "multimodal_model_name": "google/gemini-2.5-flash",
            "multimodal_text_fallback_enabled": True,
        }

    monkeypatch.setattr(
        event_service, "_load_runtime_llm_credentials", _fake_primary_creds
    )
    monkeypatch.setattr(
        event_service,
        "_load_multimodal_fallback_credentials",
        _fake_fallback_creds,
    )

    primary_client = (
        _ScriptedLLMClient(primary_script, label="primary")
        if primary_script is not None
        else None
    )
    fallback_client = (
        _ScriptedLLMClient(fallback_script, label="fallback")
        if fallback_script is not None
        else None
    )

    def _fake_get_tenant_llm_client(_tenant_id: Any, _creds: dict) -> Any:
        return primary_client

    def _fake_get_multimodal_text_llm_client(_tenant_id: Any, _creds: dict) -> Any:
        return fallback_client

    # The routing code imports these inside the function body, so patch the
    # module they live in.
    monkeypatch.setattr(
        "src.llm.client.get_tenant_llm_client", _fake_get_tenant_llm_client
    )
    monkeypatch.setattr(
        "src.llm.client.get_multimodal_text_llm_client",
        _fake_get_multimodal_text_llm_client,
    )
    return primary_client, fallback_client


@pytest.fixture
def service() -> EventService:
    return EventService(
        pipeline=SimpleNamespace(
            check_response_scheduling_gate=lambda *args, **kwargs: (None, None)
        )
    )


@pytest.mark.asyncio
async def test_primary_usable_first_attempt_zero_fallback_calls(
    service: EventService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Happy path: usable primary content → 1 primary attempt, 0 fallback."""
    primary, fallback = _install_primary_and_fallback(
        monkeypatch,
        primary_script=[
            {
                "content": "Olá! Tudo bem?",
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "total_tokens": 16,
                },
                "model": "gpt-primary-reported",
            }
        ],
        fallback_script=[],  # empty script → asserts if invoked
    )
    (
        response,
        llm_usage,
        llm_model,
        llm_attempts,
        llm_usage_total,
    ) = await service._convert_pipeline_result_to_response(
        _make_result(),
        user_message="Olá",
        clinic_config={},
        tenant_id=uuid4(),
    )
    assert response.prompt.suggested_response == "Olá! Tudo bem?"
    assert llm_model == "gpt-primary-reported"
    assert llm_usage == {
        "prompt_tokens": 12,
        "completion_tokens": 4,
        "total_tokens": 16,
    }
    assert len(llm_attempts) == 1
    entry = llm_attempts[0]
    assert entry["tier"] == "primary"
    assert entry["attempt"] == 1
    assert entry["outcome"] == "response"
    assert entry["accepted"] is True
    assert entry["usage_status"] == "reported"
    assert entry["reported_model"] == "gpt-primary-reported"
    assert entry["requested_model"] == "gpt-primary"
    # Fold matches — zero double-count from anywhere.
    assert llm_usage_total == {
        "prompt_tokens": 12,
        "completion_tokens": 4,
        "total_tokens": 16,
    }
    # Exactly one primary call, zero fallback calls.
    assert primary is not None and len(primary.calls) == 1
    assert fallback is not None and fallback.calls == []


@pytest.mark.asyncio
async def test_primary_empty_retry_then_fallback_success_folds_all_three(
    service: EventService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """primary empty+usage → primary retry empty+usage → fallback usable+usage.

    The full-matrix pillar case: 3 attempts recorded in order; the fold
    sums ALL three reported usages exactly once; only the fallback entry
    is ``accepted``; identical messages + max_tokens across all calls.
    """
    primary, fallback = _install_primary_and_fallback(
        monkeypatch,
        primary_script=[
            # Attempt 1 — empty content but usage is reported.
            {
                "content": "",
                "usage": {
                    "prompt_tokens": 5,
                    "completion_tokens": 0,
                    "total_tokens": 5,
                },
                "model": "gpt-primary-reported",
            },
            # Attempt 2 — still empty; usage reported again.
            {
                "content": "   ",
                "usage": {
                    "prompt_tokens": 6,
                    "completion_tokens": 0,
                    "total_tokens": 6,
                },
                "model": "gpt-primary-reported",
            },
        ],
        fallback_script=[
            {
                "content": "Aqui vai uma resposta útil.",
                "usage": {
                    "prompt_tokens": 12,
                    "completion_tokens": 8,
                    "total_tokens": 20,
                },
                "model": "gemini-2.5-flash-reported",
            }
        ],
    )
    (
        response,
        llm_usage,
        llm_model,
        llm_attempts,
        llm_usage_total,
    ) = await service._convert_pipeline_result_to_response(
        _make_result(),
        user_message="preciso de ajuda",
        clinic_config={},
        tenant_id=uuid4(),
    )
    # Accepted trio comes from the fallback, not the primary attempts.
    assert response.prompt.suggested_response == "Aqui vai uma resposta útil."
    assert llm_model == "gemini-2.5-flash-reported"
    assert llm_usage == {
        "prompt_tokens": 12,
        "completion_tokens": 8,
        "total_tokens": 20,
    }
    # Three attempts in strict order: primary/1, primary/2, fallback/1.
    assert [
        (a["tier"], a["attempt"], a["outcome"], a["accepted"], a["usage_status"])
        for a in llm_attempts
    ] == [
        ("primary", 1, "empty", False, "reported"),
        ("primary", 2, "empty", False, "reported"),
        ("multimodal_fallback", 1, "response", True, "reported"),
    ]
    # Exactly one accepted across the whole list.
    assert sum(1 for a in llm_attempts if a["accepted"]) == 1
    # Fold sums ALL three reported usages exactly once. total_tokens is a
    # straight sum of reported values (5 + 6 + 20 = 31), never inferred.
    assert llm_usage_total == {
        "prompt_tokens": 5 + 6 + 12,
        "completion_tokens": 0 + 0 + 8,
        "total_tokens": 5 + 6 + 20,
    }
    # Identical messages + max_tokens crossed the boundary each time.
    assert primary is not None and fallback is not None
    assert len(primary.calls) == 2
    assert len(fallback.calls) == 1
    primary_msgs = primary.calls[0]["messages"]
    assert primary.calls[1]["messages"] is primary_msgs  # same list object
    assert fallback.calls[0]["messages"] is primary_msgs
    for call in (*primary.calls, *fallback.calls):
        assert call["max_tokens"] == 300


@pytest.mark.asyncio
async def test_primary_exception_goes_straight_to_fallback_once(
    service: EventService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exception on the FIRST primary attempt skips any extra primary retry."""
    primary, fallback = _install_primary_and_fallback(
        monkeypatch,
        primary_script=[
            RuntimeError("simulated transport failure"),
        ],
        fallback_script=[
            {
                "content": "Fallback ok",
                "usage": {"total_tokens": 3},
                "model": "gemini-reported",
            }
        ],
    )
    (
        response,
        llm_usage,
        llm_model,
        llm_attempts,
        llm_usage_total,
    ) = await service._convert_pipeline_result_to_response(
        _make_result(),
        user_message="preciso",
        clinic_config={},
        tenant_id=uuid4(),
    )
    assert response.prompt.suggested_response == "Fallback ok"
    # Exactly two attempts: primary/1 (error, unavailable), fallback/1 (accepted).
    assert [
        (a["tier"], a["attempt"], a["outcome"], a["accepted"], a["usage_status"])
        for a in llm_attempts
    ] == [
        ("primary", 1, "error", False, "unavailable"),
        ("multimodal_fallback", 1, "response", True, "reported"),
    ]
    # No tokens invented for the primary error entry.
    assert llm_attempts[0]["usage"] == {}
    # Fold picks up only the fallback's reported usage.
    assert llm_usage_total == {"total_tokens": 3}
    assert llm_usage == {"total_tokens": 3}
    assert llm_model == "gemini-reported"
    # Exactly one primary call (the exception one), then exactly one fallback.
    assert primary is not None and len(primary.calls) == 1
    assert fallback is not None and len(fallback.calls) == 1


@pytest.mark.asyncio
async def test_fallback_timeout_is_recorded_with_unavailable_usage(
    service: EventService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every started call survives accounting, including fallback timeouts."""
    primary, fallback = _install_primary_and_fallback(
        monkeypatch,
        primary_script=[
            {"content": "", "usage": {"total_tokens": 2}},
            {"content": "", "usage": {"total_tokens": 3}},
        ],
        fallback_script=[TimeoutError("fallback timed out")],
    )
    (
        response,
        llm_usage,
        llm_model,
        llm_attempts,
        llm_usage_total,
    ) = await service._convert_pipeline_result_to_response(
        _make_result(),
        user_message="oi",
        clinic_config={},
        tenant_id=uuid4(),
    )

    assert response.prompt.suggested_response is None
    assert llm_usage == {}
    assert llm_model == ""
    assert [
        (a["tier"], a["attempt"], a["outcome"], a["accepted"], a["usage_status"])
        for a in llm_attempts
    ] == [
        ("primary", 1, "empty", False, "reported"),
        ("primary", 2, "empty", False, "reported"),
        ("multimodal_fallback", 1, "error", False, "unavailable"),
    ]
    assert llm_attempts[-1]["usage"] == {}
    assert llm_usage_total == {"total_tokens": 5}
    assert primary is not None and fallback is not None
    assert len(primary.calls) == 2
    assert len(fallback.calls) == 1


@pytest.mark.asyncio
async def test_fallback_invalid_with_usage_preserves_usage_but_not_accepted(
    service: EventService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fallback returns unusable content + reported usage.

    Final degradation: no accepted trio, but the reported usage still
    enters the breakdown and the fold.
    """
    primary, fallback = _install_primary_and_fallback(
        monkeypatch,
        primary_script=[
            {"content": "", "usage": {"prompt_tokens": 4}, "model": "gpt-primary"},
            {"content": "", "usage": {"prompt_tokens": 4}, "model": "gpt-primary"},
        ],
        fallback_script=[
            # Invalid shape: content is not a string. Still carries usage.
            {
                "content": None,
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 2,
                    "total_tokens": 5,
                },
                "model": "gemini-reported",
            }
        ],
    )
    (
        response,
        llm_usage,
        llm_model,
        llm_attempts,
        llm_usage_total,
    ) = await service._convert_pipeline_result_to_response(
        _make_result(),
        user_message="oi",
        clinic_config={},
        tenant_id=uuid4(),
    )
    # No accepted trio → suggested_response stays None; legacy trio empty.
    assert response.prompt.suggested_response is None
    assert llm_usage == {}
    assert llm_model == ""
    # Three attempts, ZERO accepted.
    assert sum(1 for a in llm_attempts if a["accepted"]) == 0
    tiers = [(a["tier"], a["attempt"], a["outcome"]) for a in llm_attempts]
    assert tiers == [
        ("primary", 1, "empty"),
        ("primary", 2, "empty"),
        ("multimodal_fallback", 1, "invalid"),
    ]
    # Fallback usage is REPORTED even though the content was invalid.
    assert llm_attempts[2]["usage_status"] == "reported"
    assert llm_attempts[2]["usage"] == {
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
    }
    # Fold sums all three reported entries.
    assert llm_usage_total == {
        "prompt_tokens": 4 + 4 + 3,
        "completion_tokens": 2,
        "total_tokens": 5,
    }
    assert primary is not None and len(primary.calls) == 2
    assert fallback is not None and len(fallback.calls) == 1


@pytest.mark.asyncio
async def test_missing_usage_field_is_never_synthesised_in_fold(
    service: EventService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fold never derives ``total_tokens`` from prompt+completion."""
    _install_primary_and_fallback(
        monkeypatch,
        primary_script=[
            {
                "content": "resposta",
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                "model": "gpt-primary",
            }
        ],
        fallback_script=[],
    )
    (
        _response,
        _llm_usage,
        _llm_model,
        llm_attempts,
        llm_usage_total,
    ) = await service._convert_pipeline_result_to_response(
        _make_result(),
        user_message="oi",
        clinic_config={},
        tenant_id=uuid4(),
    )
    assert llm_attempts[0]["usage_status"] == "reported"
    assert llm_attempts[0]["usage"] == {"prompt_tokens": 10, "completion_tokens": 5}
    assert "total_tokens" not in llm_usage_total
    assert llm_usage_total == {"prompt_tokens": 10, "completion_tokens": 5}


@pytest.mark.asyncio
async def test_usage_with_bool_value_is_marked_invalid(
    service: EventService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bool in a canonical field must NOT slip in as int==1 or int==0."""
    _install_primary_and_fallback(
        monkeypatch,
        primary_script=[
            {
                "content": "usable",
                "usage": {"prompt_tokens": True, "completion_tokens": 5},
                "model": "gpt-primary",
            }
        ],
        fallback_script=[],
    )
    (
        _response,
        llm_usage,
        _llm_model,
        llm_attempts,
        llm_usage_total,
    ) = await service._convert_pipeline_result_to_response(
        _make_result(),
        user_message="oi",
        clinic_config={},
        tenant_id=uuid4(),
    )
    # Content itself is usable — accepted trio populated with empty usage.
    assert llm_attempts[0]["accepted"] is True
    assert llm_attempts[0]["usage_status"] == "invalid"
    assert llm_attempts[0]["usage"] == {}
    # Invalid usage never rejected the textual acceptance.
    assert llm_usage == {}
    # Fold ignores the invalid entry.
    assert llm_usage_total == {}


@pytest.mark.asyncio
async def test_missing_usage_marks_unavailable_when_content_usable(
    service: EventService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent usage → ``unavailable``. Usable content still accepted."""
    _install_primary_and_fallback(
        monkeypatch,
        primary_script=[
            {"content": "hello", "model": "gpt-primary"},  # no `usage` key
        ],
        fallback_script=[],
    )
    (
        _response,
        llm_usage,
        _llm_model,
        llm_attempts,
        llm_usage_total,
    ) = await service._convert_pipeline_result_to_response(
        _make_result(),
        user_message="oi",
        clinic_config={},
        tenant_id=uuid4(),
    )
    assert llm_attempts[0]["accepted"] is True
    assert llm_attempts[0]["usage_status"] == "unavailable"
    assert llm_attempts[0]["usage"] == {}
    assert llm_usage == {}
    assert llm_usage_total == {}


@pytest.mark.asyncio
async def test_no_primary_client_preserves_prior_behavior(
    service: EventService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No primary client → no primary attempts, no fallback attempts.

    This matches the pre-fallback behaviour exactly. The routing block
    only enters the primary loop when ``llm and user_message`` are truthy.
    """
    _, fallback = _install_primary_and_fallback(
        monkeypatch,
        primary_script=None,  # factory returns None
        fallback_script=[],  # would assert if invoked
    )
    (
        response,
        llm_usage,
        llm_model,
        llm_attempts,
        llm_usage_total,
    ) = await service._convert_pipeline_result_to_response(
        _make_result(),
        user_message="oi",
        clinic_config={},
        tenant_id=uuid4(),
    )
    assert response.prompt.suggested_response is None
    assert llm_usage == {}
    assert llm_model == ""
    assert llm_attempts == []
    assert llm_usage_total == {}
    assert fallback is not None and fallback.calls == []


@pytest.mark.asyncio
async def test_fallback_unavailable_skips_silently_after_primary_failure(
    service: EventService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No opt-in / incomplete bundle → fallback factory returns None.

    The primary attempts are recorded normally; the multimodal-text
    factory returns ``None`` (fail-closed) and no fallback attempt is
    ever appended to ``llm_attempts``.
    """
    primary, fallback = _install_primary_and_fallback(
        monkeypatch,
        primary_script=[
            {"content": "", "usage": {"prompt_tokens": 2}, "model": "gpt-primary"},
            {"content": "", "usage": {"prompt_tokens": 2}, "model": "gpt-primary"},
        ],
        fallback_script=None,  # factory returns None
    )
    (
        response,
        llm_usage,
        llm_model,
        llm_attempts,
        llm_usage_total,
    ) = await service._convert_pipeline_result_to_response(
        _make_result(),
        user_message="oi",
        clinic_config={},
        tenant_id=uuid4(),
    )
    # Legacy trio empty — nothing was accepted.
    assert response.prompt.suggested_response is None
    assert llm_usage == {}
    assert llm_model == ""
    # Only the two primary attempts are recorded — fallback factory
    # refused so there is no third entry.
    assert [(a["tier"], a["attempt"]) for a in llm_attempts] == [
        ("primary", 1),
        ("primary", 2),
    ]
    assert primary is not None and len(primary.calls) == 2
    assert fallback is None  # no fallback client instantiated
    # Fold still folds the primary reported usage.
    assert llm_usage_total == {"prompt_tokens": 4}


@pytest.mark.asyncio
async def test_primary_usable_with_reported_model_prefers_reported_for_breakdown(
    service: EventService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``reported_model`` (valid) wins; ``requested_model`` is the fallback."""
    _install_primary_and_fallback(
        monkeypatch,
        primary_script=[
            {
                "content": "hi",
                "usage": {"total_tokens": 1},
                "model": "  gpt-fancy-server-reported  ",
            }
        ],
        fallback_script=[],
    )
    _r, _u, llm_model, llm_attempts, _t = (
        await service._convert_pipeline_result_to_response(
            _make_result(),
            user_message="oi",
            clinic_config={},
            tenant_id=uuid4(),
        )
    )
    # Reported model wins (even with leading/trailing whitespace).
    assert llm_model == "  gpt-fancy-server-reported  "
    assert llm_attempts[0]["reported_model"] == "  gpt-fancy-server-reported  "
    assert llm_attempts[0]["requested_model"] == "gpt-primary"


@pytest.mark.asyncio
async def test_missing_reported_model_falls_back_to_requested(
    service: EventService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the provider omits ``model``, breakdown uses ``requested_model``."""
    _install_primary_and_fallback(
        monkeypatch,
        primary_script=[
            {"content": "hi", "usage": {"total_tokens": 1}},  # no model
        ],
        fallback_script=[],
    )
    _r, _u, llm_model, llm_attempts, _t = (
        await service._convert_pipeline_result_to_response(
            _make_result(),
            user_message="oi",
            clinic_config={},
            tenant_id=uuid4(),
        )
    )
    assert llm_attempts[0]["reported_model"] is None
    assert llm_model == "gpt-primary"


@pytest.mark.asyncio
async def test_whitespace_reported_model_falls_back_to_requested(
    service: EventService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A whitespace-only provider model is not valid breakdown attribution."""
    _install_primary_and_fallback(
        monkeypatch,
        primary_script=[
            {"content": "hi", "usage": {"total_tokens": 1}, "model": "   "},
        ],
        fallback_script=[],
    )
    _r, _u, llm_model, llm_attempts, _t = (
        await service._convert_pipeline_result_to_response(
            _make_result(),
            user_message="oi",
            clinic_config={},
            tenant_id=uuid4(),
        )
    )
    assert llm_attempts[0]["reported_model"] is None
    assert llm_model == "gpt-primary"


# ============================================
# Persistence — message.in carries llm_attempts + llm_usage_total
# ============================================


@pytest.mark.asyncio
async def test_message_in_metadata_carries_new_fields(
    service: EventService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The trio-owning event (``message.in``) also carries the new fields."""
    from src.api.schemas.events import EventPayload, EventRequest, EventType

    request = EventRequest(
        tenant_id=uuid4(),
        session_id=uuid4(),
        event_type=EventType.MESSAGE_IN,
        payload=EventPayload(content="oi", channel="whatsapp", user_id="u"),
    )
    result = _make_result()
    result.tenant_id = request.tenant_id
    result.session_id = request.session_id

    attempts = [
        {
            "tier": "primary",
            "attempt": 1,
            "provider": "openai",
            "requested_model": "gpt-primary",
            "reported_model": "gpt-primary-reported",
            "outcome": "response",
            "accepted": True,
            "usage_status": "reported",
            "usage": {"prompt_tokens": 3, "total_tokens": 3},
        }
    ]
    total = {"prompt_tokens": 3, "total_tokens": 3}

    store = SimpleNamespace(append_batch=AsyncMock())
    with patch(
        "src.services.event_service.get_event_store",
        AsyncMock(return_value=store),
    ):
        await service._save_events_to_store(
            request,
            result,
            suggested_response="hi",
            llm_usage={"prompt_tokens": 3, "total_tokens": 3},
            llm_model="gpt-primary-reported",
            llm_attempts=attempts,
            llm_usage_total=total,
        )

    store.append_batch.assert_awaited_once()
    events = store.append_batch.await_args.args[0]
    message_in = next(e for e in events if e.event_type == "message.in")
    md = message_in.event_metadata
    # Legacy trio preserved byte-for-byte.
    assert md["llm_model"] == "gpt-primary-reported"
    assert md["llm_usage"] == {"prompt_tokens": 3, "total_tokens": 3}
    # New fields present, non-aliased copies (defensive dict()/list()).
    assert md["llm_attempts"] == attempts
    assert md["llm_attempts"] is not attempts
    assert md["llm_usage_total"] == total
    assert md["llm_usage_total"] is not total


@pytest.mark.asyncio
async def test_prompt_generated_remains_owner_of_response(
    service: EventService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``prompt.generated`` keeps ``suggested_response`` and does NOT gain
    the attempts/total fields."""
    from src.api.schemas.events import EventPayload, EventRequest, EventType

    request = EventRequest(
        tenant_id=uuid4(),
        session_id=uuid4(),
        event_type=EventType.MESSAGE_IN,
        payload=EventPayload(content="oi", channel="whatsapp", user_id="u"),
    )
    result = _make_result()
    result.tenant_id = request.tenant_id
    result.session_id = request.session_id

    store = SimpleNamespace(append_batch=AsyncMock())
    with patch(
        "src.services.event_service.get_event_store",
        AsyncMock(return_value=store),
    ):
        await service._save_events_to_store(
            request,
            result,
            suggested_response="olá",
            llm_usage={},
            llm_model="",
            llm_attempts=[],
            llm_usage_total={},
        )
    store.append_batch.assert_awaited_once()
    events = store.append_batch.await_args.args[0]
    prompt_ev = next(e for e in events if e.event_type == "prompt.generated")
    assert prompt_ev.event_data["suggested_response"] == "olá"
    # New attempts/total live on message.in only — prompt.generated does
    # not carry them so the trio/breakdown responsibilities stay split.
    assert "llm_attempts" not in prompt_ev.event_data
    assert "llm_usage_total" not in prompt_ev.event_data
    assert "llm_attempts" not in prompt_ev.event_metadata
    assert "llm_usage_total" not in prompt_ev.event_metadata


# ============================================
# Wish ``handoff-contextual-resumption`` Group 3 —
# ``prior_episode_context`` framing complement
# ============================================
#
# The full "same messages, same max_tokens, INCLUDING the policy framing
# + adversarial JSON payload" AC is proven in
# ``tests/srm/test_handoff_context_injection.py`` so this file does not
# duplicate it. The complement below proves the negative — when the
# session has NO stored ``prior_episode_context`` (first contact / legacy
# turn), the outbound array stays at 2 messages: main ``system`` + the
# current-turn ``user``. If the framing were accidentally injected
# unconditionally, THIS test would catch it as a regression.


@pytest.mark.asyncio
async def test_outbound_array_omits_policy_framing_when_no_prior_episode(
    service: EventService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy / first-contact turn — no ``prior_episode_context`` on the
    result → outbound array has NO policy framing message and NO JSON
    payload message (only main system + current-turn user)."""
    from src.services.event_service import PRIOR_EPISODE_CONTEXT_POLICY_MESSAGE

    primary, _fallback = _install_primary_and_fallback(
        monkeypatch,
        primary_script=[
            {
                "content": "resposta curta",
                "usage": {"total_tokens": 2},
                "model": "gpt-primary-reported",
            }
        ],
        fallback_script=[],
    )
    result = _make_result()
    # Explicit: no prior_episode_context on the result (first-contact).
    result.prior_episode_context = None

    await service._convert_pipeline_result_to_response(
        result,
        user_message="oi",
        clinic_config={},
        tenant_id=uuid4(),
    )

    assert primary is not None and len(primary.calls) == 1
    messages = primary.calls[0]["messages"]
    # Two messages: main system + current-turn user. The intent here is
    # ``consulta`` so no saudacao-reinforcement system message is
    # appended either.
    assert len(messages) == 2, (
        f"expected 2 messages when no prior_episode_context, got "
        f"{len(messages)}: {[m['role'] for m in messages]}"
    )
    assert messages[0]["role"] == "system"
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == "oi"
    # The policy framing message MUST NOT appear anywhere.
    for msg in messages:
        assert msg["content"] != PRIOR_EPISODE_CONTEXT_POLICY_MESSAGE
        assert "memória resumida do episódio" not in msg["content"]
