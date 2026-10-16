"""Closed contract and cumulative-money boundary tests for Group 4."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.services.billing_outbox import BillingFactViolation, validate_fact

_FIXTURES = Path(__file__).parents[1] / "fixtures/contracts/billing_fact_v1"


def _fixture(name: str) -> dict[str, object]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


def test_all_six_canonical_fixtures_are_closed_and_valid() -> None:
    event_types: set[str] = set()
    for fixture in sorted(_FIXTURES.glob("*.json")):
        fact = _fixture(fixture.name)
        validate_fact(fact)
        event_types.add(str(fact["event_type"]))

    assert event_types == {
        "subscription.cycle_settled.v1",
        "subscription.cycle_reversed.v1",
        "subscription.cycle_restored.v1",
        "topup.payment_settled.v1",
        "topup.payment_reversed.v1",
        "topup.payment_restored.v1",
    }


def test_fact_rejects_pii_and_line_equation_drift() -> None:
    fact = _fixture("topup_payment_settled.json")
    with_pii = copy.deepcopy(fact)
    with_pii["email"] = "not-allowed@example.test"
    with pytest.raises(BillingFactViolation):
        validate_fact(with_pii)

    wrong_amount = copy.deepcopy(fact)
    lines = wrong_amount["lines"]
    assert isinstance(lines, list)
    assert isinstance(lines[0], dict)
    lines[0]["line_amount_cents"] = 9_999
    with pytest.raises(BillingFactViolation):
        validate_fact(wrong_amount)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda fact: fact.update(paid_at=None),
        lambda fact: fact.update(order_id="30000000-0000-4000-8000-000000000002"),
        lambda fact: fact.update(
            subscription_id="50000000-0000-4000-8000-000000000001"
        ),
        lambda fact: fact["lines"][0].update(offer_id="another_offer"),
        lambda fact: fact["lines"][0].update(financial_state="pending"),
        lambda fact: fact["lines"][0].update(credit_grant_mc=499_999),
    ],
)
def test_fact_rejects_settlement_identity_and_credit_drift(mutate) -> None:
    fact = _fixture("topup_payment_settled.json")
    mutate(fact)
    with pytest.raises(BillingFactViolation):
        validate_fact(fact)


def test_subscription_cycle_identity_is_distinct_from_cycle_aggregate() -> None:
    fact = _fixture("subscription_cycle_settled.json")
    validate_fact(fact)
    assert fact["subscription_id"] != fact["aggregate_id"]
    assert fact["order_id"] is None
