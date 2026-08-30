"""Bounded Prometheus contract for LLM generation attempts."""

from src.observability.metrics import LLM_GENERATION_ATTEMPTS_TOTAL


def test_llm_generation_attempt_counter_has_only_tier_and_outcome_labels() -> None:
    assert LLM_GENERATION_ATTEMPTS_TOTAL._labelnames == ("tier", "outcome")
