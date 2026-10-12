"""Top-up cumulative financial snapshot boundaries."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.services.billing_outbox import BillingFactViolation, validate_fact

_FIXTURE = (
    Path(__file__).parents[1]
    / "fixtures/contracts/billing_fact_v1/topup_payment_restored.json"
)


def _fact() -> dict[str, object]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def test_topup_restoration_keeps_cumulative_line_lattice() -> None:
    fact = _fact()
    validate_fact(fact)
    assert (fact["settled_cents"], fact["reversed_cents"], fact["restored_cents"]) == (
        10_000,
        5_000,
        2_500,
    )


def test_topup_rejects_restoration_above_reversal() -> None:
    fact = copy.deepcopy(_fact())
    fact["restored_cents"] = 5_001
    with pytest.raises(BillingFactViolation):
        validate_fact(fact)
