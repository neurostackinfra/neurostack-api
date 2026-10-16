"""Integer-only immutable pricing boundary tests."""

from __future__ import annotations

import pytest

from src.services.billing_catalog import BILLING_CATALOG
from src.services.billing_checkout import (
    CheckoutInputRejected,
    checkout_items,
    subscription_pricing_snapshot,
    topup_pricing_snapshot,
)


def test_additional_clinic_pricing_is_integer_only_and_snapshot_bound() -> None:
    offer = BILLING_CATALOG["plan_pro_monthly_v1"]
    snapshot = subscription_pricing_snapshot(offer, active_clinics=5)

    assert snapshot["additional_clinics"] == 2
    assert snapshot["amount_cents"] == 280_000
    assert snapshot["credit_grant_mc"] == offer.credits * 1_000
    assert all(
        isinstance(value, int)
        for value in (snapshot["amount_cents"], snapshot["credit_grant_mc"])
    )
    item = checkout_items(snapshot, offer)[0]
    assert item.unit_amount_cents == 280_000


def test_coupon_snapshot_is_immutable_and_discount_never_becomes_negative() -> None:
    offer = BILLING_CATALOG["plan_plus_monthly_v1"]
    original = subscription_pricing_snapshot(
        offer,
        active_clinics=3,
        coupon_code="WELCOME25",
        coupon_discount_percent=25,
    )
    changed_later = subscription_pricing_snapshot(
        offer,
        active_clinics=10,
        coupon_code="WELCOME25",
        coupon_discount_percent=25,
    )

    assert original["amount_cents"] == 104_925
    assert original["coupon_code"] == "WELCOME25"
    assert original != changed_later
    # Retrying the persisted original must keep its original amount even after
    # the builder adds clinics or a coupon definition changes.
    assert checkout_items(original, offer)[0].unit_amount_cents == 104_925

    with pytest.raises(CheckoutInputRejected):
        subscription_pricing_snapshot(
            offer,
            active_clinics=1,
            coupon_code="FREE",
            coupon_discount_percent=100,
        )


def test_topup_pricing_has_integer_quantity_and_only_allowed_method() -> None:
    offer = BILLING_CATALOG["package_credits_500_v1"]
    snapshot = topup_pricing_snapshot(offer, payment_method="pix")

    assert snapshot["amount_cents"] == 10_000
    assert snapshot["credit_grant_mc"] == 500_000
    assert snapshot["payment_method"] == "pix"
    assert snapshot["lines"] == [
        {
            "line_id": "topup",
            "offer_id": "package_credits_500_v1",
            "kind": "topup",
            "quantity": 1,
            "unit_amount_cents": 10_000,
            "line_amount_cents": 10_000,
            "credit_grant_mc": 500_000,
            "settled_cents": 0,
            "reversed_cents": 0,
            "restored_cents": 0,
            "line_financial_state": "pending",
        }
    ]


_PERSISTED_LINE_KINDS = {"plan", "additional_clinic", "topup", "tax"}


def test_coupon_lines_sum_to_the_integer_net_for_every_offer_and_percent() -> None:
    for offer_id in (
        "plan_plus_monthly_v1",
        "plan_pro_monthly_v1",
        "plan_max_monthly_v1",
    ):
        offer = BILLING_CATALOG[offer_id]
        for active_clinics in range(0, 13):
            gross = offer.amount_cents + max(
                0, active_clinics - offer.included_clinics
            ) * (offer.additional_clinic_amount_cents)
            for percent in range(1, 100):
                snapshot = subscription_pricing_snapshot(
                    offer,
                    active_clinics=active_clinics,
                    coupon_code="PROMO",
                    coupon_discount_percent=percent,
                )
                lines = snapshot["lines"]
                assert isinstance(lines, list)
                assert snapshot["amount_cents"] == gross - gross * percent // 100
                assert sum(line["line_amount_cents"] for line in lines) == (
                    snapshot["amount_cents"]
                )
                for line in lines:
                    assert line["kind"] in _PERSISTED_LINE_KINDS
                    assert type(line["unit_amount_cents"]) is int
                    assert line["unit_amount_cents"] > 0
                    assert line["line_amount_cents"] == (
                        line["quantity"] * line["unit_amount_cents"]
                    )


def test_coupon_discount_requires_a_coupon_code_and_an_integer_percent() -> None:
    offer = BILLING_CATALOG["plan_plus_monthly_v1"]
    with pytest.raises(CheckoutInputRejected):
        subscription_pricing_snapshot(
            offer, active_clinics=1, coupon_discount_percent=10
        )
    with pytest.raises(CheckoutInputRejected):
        subscription_pricing_snapshot(
            offer,
            active_clinics=1,
            coupon_code="PROMO",
            coupon_discount_percent=10.0,  # type: ignore[arg-type]
        )
    plain = subscription_pricing_snapshot(offer, active_clinics=1)
    assert "coupon_code" not in plain
    assert plain["amount_cents"] == 99_900
