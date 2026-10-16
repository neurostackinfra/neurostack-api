"""Public request/response boundary tests for hosted ASAAS checkout."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from src.api.routes import billing
from src.api.schemas.billing import CheckoutRequest
from src.services.billing_checkout import (
    CheckoutCouponRejected,
    CheckoutNotFound,
    CheckoutView,
)


@pytest.mark.parametrize(
    "payload",
    [
        {"offer_id": "plan_plus_monthly_v1", "method": "pix"},
        {
            "offer_id": "plan_plus_monthly_v1",
            "method": "credit_card",
            "amount_cents": 1,
        },
        {
            "offer_id": "plan_plus_monthly_v1",
            "method": "credit_card",
            "next_due_date": "2030-01-01",
        },
        {
            "offer_id": "plan_plus_monthly_v1",
            "method": "credit_card",
            "cycle": "annual",
        },
        {
            "offer_id": "plan_plus_monthly_v1",
            "method": "credit_card",
            "tenant_id": str(uuid4()),
        },
    ],
)
def test_checkout_schema_rejects_browser_price_tenant_date_and_invalid_method(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CheckoutRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_route_passes_only_offer_and_allowed_method_to_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_id = uuid4()
    checkout_id = uuid4()
    captured: dict[str, object] = {}

    class Service:
        def __init__(
            self, db: object, *, client_factory: object, **_kwargs: object
        ) -> None:
            captured["db"] = db
            captured["factory"] = client_factory

        async def create_checkout(self, **kwargs: object) -> CheckoutView:
            captured.update(kwargs)
            return CheckoutView(
                checkout_session_id=checkout_id,
                kind="topup",
                offer_id="package_credits_500_v1",
                state="active",
                checkout_url="https://asaas.com/checkoutSession/show?id=chk_fixture",
            )

    monkeypatch.setattr(billing, "BillingCheckoutService", Service)
    response = await billing.create_checkout(
        CheckoutRequest(offer_id="package_credits_500_v1", method="pix"),
        builder_id,
        object(),
    )

    assert captured["builder_id"] == builder_id
    assert captured["offer_id"] == "package_credits_500_v1"
    assert captured["payment_method"] == "pix"
    assert response.model_dump() == {
        "ok": True,
        "checkout_session_id": str(checkout_id),
        "kind": "topup",
        "state": "active",
        "checkout_url": "https://asaas.com/checkoutSession/show?id=chk_fixture",
    }


@pytest.mark.asyncio
async def test_foreign_checkout_id_has_the_same_404_as_an_absent_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Service:
        def __init__(self, db: object, *, client_factory: object) -> None:
            del db, client_factory

        async def get_checkout(self, **kwargs: object) -> CheckoutView:
            del kwargs
            raise CheckoutNotFound()

    monkeypatch.setattr(billing, "BillingCheckoutService", Service)
    with pytest.raises(HTTPException) as exc_info:
        await billing.get_checkout(uuid4(), uuid4(), object())

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Checkout not found"


@pytest.mark.parametrize(
    "payload",
    [
        {
            "offer_id": "package_credits_500_v1",
            "method": "pix",
            "coupon_code": "PROMO10",
        },
        {
            "offer_id": "package_credits_500_v1",
            "method": "credit_card",
            "coupon_code": "PROMO10",
        },
        {
            "offer_id": "plan_plus_monthly_v1",
            "method": "credit_card",
            "coupon_code": "",
        },
        {
            "offer_id": "plan_plus_monthly_v1",
            "method": "credit_card",
            "coupon_code": "A" * 51,
        },
        {
            "offer_id": "plan_plus_monthly_v1",
            "method": "credit_card",
            "coupon_code": "PROMO10",
            "discount_percent": 100,
        },
        {
            "offer_id": "plan_plus_monthly_v1",
            "method": "credit_card",
            "coupon_code": "PROMO10",
            "quantity": 3,
        },
        {
            "offer_id": "plan_plus_monthly_v1",
            "method": "credit_card",
            "coupon_code": "PROMO10",
            "builder_id": str(uuid4()),
        },
    ],
    ids=[
        "topup-pix-coupon",
        "topup-card-coupon",
        "empty-coupon",
        "long-coupon",
        "browser-discount",
        "browser-quantity",
        "browser-builder",
    ],
)
def test_checkout_schema_accepts_coupon_only_as_a_subscription_selector(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        CheckoutRequest.model_validate(payload)


def test_checkout_schema_accepts_optional_subscription_coupon_selector() -> None:
    body = CheckoutRequest.model_validate(
        {
            "offer_id": "plan_plus_monthly_v1",
            "method": "credit_card",
            "coupon_code": "promo10",
        }
    )
    assert body.coupon_code == "promo10"
    assert (
        CheckoutRequest(
            offer_id="plan_pro_monthly_v1", method="credit_card"
        ).coupon_code
        is None
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code", ["billing_checkout_coupon_invalid", "billing_checkout_coupon_not_eligible"]
)
async def test_route_forwards_coupon_selector_and_maps_rejection_to_fixed_400(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
) -> None:
    captured: dict[str, object] = {}

    class Service:
        def __init__(
            self, db: object, *, client_factory: object, **_kwargs: object
        ) -> None:
            del db, client_factory

        async def create_checkout(self, **kwargs: object) -> CheckoutView:
            captured.update(kwargs)
            raise CheckoutCouponRejected(code)

    monkeypatch.setattr(billing, "BillingCheckoutService", Service)
    with pytest.raises(HTTPException) as exc_info:
        await billing.create_checkout(
            CheckoutRequest(
                offer_id="plan_plus_monthly_v1",
                method="credit_card",
                coupon_code="promo10",
            ),
            uuid4(),
            object(),
        )

    assert captured["coupon_code"] == "promo10"
    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == code
