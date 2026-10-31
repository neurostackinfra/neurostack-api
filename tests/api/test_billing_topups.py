"""Top-up purchase boundary seen from the credits (credit Group 6), SQLite.

Credits sell nothing: a package is bought only through the ASAAS-owned
``POST /v1/billing/checkout`` (``offer_id`` and ``method``), which this
group does not edit.  Pinned here, through the real application:

* manipulating the price, the credits, a quantity, the builder, the
  validity or a callback in the purchase changes no snapshot and no ledger:
  the body is refused (422) and no order, checkout session or credit row
  exists afterwards;
* the subscription is card only: PIX, an annual cycle or installments are
  refused;
* with ``topup_commercial_enabled`` false on the server (its default) a
  clean top-up is refused by the server and nothing is written -- no client
  value turns it on;
* a forged ASAAS callback creates no balance, and a checkout-return marker
  on a credit read changes no figure: the balance comes only from a grant
  the ledger applied;
* the credit API itself has no purchase surface (no order route, GET only).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import func, select

from src.models.billing import BillingCheckoutSession, BillingTopupOrder
from src.models.credit import CreditEffect, CreditLedgerEntry, CreditLot
from src.repositories.database import get_db_session
from src.services.credit_switch import CreditSwitch
from tests.api.test_credits import CREDITS, Builders, _seed

CHECKOUT = "/v1/billing/checkout"
TOPUP = "package_credits_500_v1"
_FINANCIAL = (BillingTopupOrder, BillingCheckoutSession, CreditLot, CreditEffect)


@pytest.fixture
def world(client: Any) -> Builders:
    seeded = Builders()
    client._submit(_seed(seeded))
    return seeded


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    def force(effective: bool) -> None:
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: effective)

    force(True)
    return force


async def _counts() -> dict[str, int]:
    database = get_db_session()
    try:
        return {
            model.__tablename__: int(
                await database.scalar(select(func.count()).select_from(model)) or 0
            )
            for model in (*_FINANCIAL, CreditLedgerEntry)
        }
    finally:
        await database.close()


@pytest.mark.parametrize(
    "tampering",
    [
        {"amount_cents": 1},
        {"credits": 4_000},
        {"credit_grant_mc": 4_000_000},
        {"quantity": 8},
        {"builder_id": "00000000-0000-4000-8000-000000000000"},
        {"tenant_id": "00000000-0000-4000-8000-000000000000"},
        {"expires_at": "2099-01-01T00:00:00Z"},
        {"validity_months": 120},
        {"callback_url": "https://attacker.invalid/paid"},
        {"success_url": "https://attacker.invalid/paid"},
        {"payment_status": "CONFIRMED"},
        {"coupon_code": "FREE100"},
    ],
)
def test_a_manipulated_top_up_changes_no_snapshot_and_no_ledger(
    client: Any,
    world: Builders,
    switch: Callable[[bool], None],
    tampering: dict[str, Any],
) -> None:
    before = client._submit(_counts())
    response = client.post(
        CHECKOUT,
        headers=world.rich_jwt,
        content=json.dumps({"offer_id": TOPUP, "method": "pix", **tampering}),
    )
    assert response.status_code == 422, response.text
    assert client._submit(_counts()) == before


@pytest.mark.parametrize(
    "body",
    [
        {"offer_id": "plan_plus_monthly_v1", "method": "pix"},
        {
            "offer_id": "plan_plus_monthly_v1",
            "method": "credit_card",
            "cycle": "annual",
        },
        {
            "offer_id": "plan_plus_monthly_v1",
            "method": "credit_card",
            "installments": 12,
        },
        {"offer_id": "plan_plus_monthly_v1", "method": "pix_automatico"},
        {"offer_id": TOPUP, "method": "boleto"},
    ],
)
def test_the_subscription_is_card_only_and_top_ups_card_or_pix(
    client: Any, world: Builders, switch: Callable[[bool], None], body: dict[str, Any]
) -> None:
    before = client._submit(_counts())
    response = client.post(CHECKOUT, headers=world.rich_jwt, json=body)
    assert response.status_code == 422, response.text
    assert client._submit(_counts()) == before


@pytest.mark.parametrize("method", ["pix", "credit_card"])
def test_with_commercial_top_ups_closed_the_server_refuses_and_writes_nothing(
    client: Any, world: Builders, switch: Callable[[bool], None], method: str
) -> None:
    """``topup_commercial_enabled`` is false by default: the billing summary
    says so, and a clean top-up is refused by the server, not by the UI."""

    summary = client.get("/v1/billing/summary", headers=world.rich_jwt)
    assert summary.status_code == 200, summary.text
    assert summary.json()["topup_commercial_enabled"] is False
    before = client._submit(_counts())
    balance = client.get(f"{CREDITS}/balance", headers=world.rich_jwt).json()

    response = client.post(
        CHECKOUT, headers=world.rich_jwt, json={"offer_id": TOPUP, "method": method}
    )
    assert response.status_code in {409, 503}, response.text
    assert "checkout_url" not in response.text
    assert client._submit(_counts()) == before
    after = client.get(f"{CREDITS}/balance", headers=world.rich_jwt).json()
    assert after["available_mc"] == balance["available_mc"]


def test_a_forged_callback_creates_no_balance(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    before = client._submit(_counts())
    balance = client.get(f"{CREDITS}/balance", headers=world.rich_jwt).json()
    forged = {
        "id": "evt_forged",
        "event": "PAYMENT_CONFIRMED",
        "payment": {
            "id": "pay_forged",
            "status": "CONFIRMED",
            "value": 800.0,
            "externalReference": str(world.rich),
        },
    }
    for headers in ({}, {"asaas-access-token": "forged-token"}):
        response = client.post(
            "/v1/billing/webhook/asaas", json=forged, headers=headers
        )
        assert response.status_code in {400, 401, 403, 503}, response.text
    assert client._submit(_counts()) == before
    after = client.get(f"{CREDITS}/balance", headers=world.rich_jwt).json()
    assert after["available_mc"] == balance["available_mc"]


def test_a_checkout_return_marker_never_anticipates_a_grant(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    clean = client.get(f"{CREDITS}/balance", headers=world.rich_jwt).json()
    for marker in ("success", "paid", "pix_qr_shown"):
        returned = client.get(
            f"{CREDITS}/balance",
            params={"checkout": marker, "offer_id": "package_credits_4000_v1"},
            headers=world.rich_jwt,
        ).json()
        assert {k: v for k, v in returned.items() if k != "as_of"} == {
            k: v for k, v in clean.items() if k != "as_of"
        }


def test_the_credit_api_has_no_purchase_surface(
    client: Any, world: Builders, switch: Callable[[bool], None]
) -> None:
    from src.api.routes import credit_export, credits

    for module in (credits, credit_export):
        for route in module.router.routes:
            assert getattr(route, "methods", set()) <= {"GET", "HEAD"}, route
            path = getattr(route, "path", "")
            assert "order" not in path and "checkout" not in path, path
    paths = client.get("/openapi.json").json()["paths"]
    credit_paths = {p: ops for p, ops in paths.items() if p.startswith(f"{CREDITS}/")}
    assert len(credit_paths) >= 10
    assert {method for ops in credit_paths.values() for method in ops} == {"get"}
    for path in ("orders", "checkout", "topups", "purchase"):
        response = client.post(
            f"{CREDITS}/{path}",
            headers=world.rich_jwt,
            json={"offer_id": TOPUP, "method": "pix"},
        )
        assert response.status_code in {404, 405}, (path, response.status_code)
