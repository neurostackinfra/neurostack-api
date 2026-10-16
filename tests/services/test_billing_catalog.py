"""Release-contract tests for the immutable billing catalog."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from src.services.billing_catalog import (
    BILLING_CATALOG,
    CATALOG_SOURCE_PAGES,
    CATALOG_SOURCE_SHA256,
    CATALOG_VERSION_ID,
    catalog_snapshot_bytes,
    validate_billing_catalog,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "asaas"


def test_catalog_snapshot_matches_release_fixture_byte_for_byte() -> None:
    expected = (FIXTURES / "catalog" / "payment-2026-10-03.json").read_bytes()

    assert catalog_snapshot_bytes() == expected
    assert CATALOG_VERSION_ID == "payment-2026-10-03"
    assert CATALOG_SOURCE_PAGES == 6
    assert CATALOG_SOURCE_SHA256 == (
        "58e71858ae25d12b76db4c72aa7f6838cb760187ecdb841fdf56d729aca86aaf"
    )
    validate_billing_catalog()


def test_catalog_has_seven_immutable_offers_and_zero_clinic_credit_grant() -> None:
    assert len(BILLING_CATALOG) == 7
    assert all(
        offer.additional_clinic_credit_grant == 0 for offer in BILLING_CATALOG.values()
    )

    with pytest.raises(TypeError):
        BILLING_CATALOG["new_offer"] = BILLING_CATALOG["plan_plus_monthly_v1"]  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        BILLING_CATALOG["plan_plus_monthly_v1"].credits = 1  # type: ignore[misc]


@pytest.mark.parametrize(
    ("relative_path", "expected"),
    [
        (
            "checkout/subscription_request.json",
            {"billingTypes": ["CREDIT_CARD"], "chargeTypes": ["RECURRENT"]},
        ),
        (
            "checkout/topup_request.json",
            {
                "billingTypes": ["CREDIT_CARD", "PIX"],
                "chargeTypes": ["DETACHED"],
            },
        ),
        ("checkout/create_response.json", {"status": "ACTIVE"}),
        ("subscription/readback_response.json", {"cycle": "MONTHLY"}),
        ("payment/payment_confirmed_event.json", {"event": "PAYMENT_CONFIRMED"}),
        ("readback/payment_response.json", {"status": "CONFIRMED"}),
    ],
)
def test_offline_asaas_contract_fixtures(
    relative_path: str,
    expected: dict[str, object],
) -> None:
    payload = json.loads((FIXTURES / relative_path).read_text(encoding="utf-8"))

    assert isinstance(payload, dict)
    for key, value in expected.items():
        assert payload[key] == value
    rendered = json.dumps(payload)
    assert "$aact_prod_" not in rendered
    assert "asaas-access-token" not in rendered
