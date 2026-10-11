"""Protocol and containment tests for the hardened ASAAS client."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from uuid import UUID

import httpx
import pytest
from pydantic import SecretStr

from src.services.asaas import (
    _PERMIT_SEAL,
    AsaasClient,
    AsaasOutcomeUnknown,
    CheckoutCallback,
    CheckoutCreateRequest,
    CheckoutItem,
    CreationEgressPermit,
    brl_amount,
)


def _request(kind: str = "subscription") -> CheckoutCreateRequest:
    return CheckoutCreateRequest(
        kind=kind,  # type: ignore[arg-type]
        external_reference=UUID("00000000-0000-4000-8000-000000000001"),
        items=(
            CheckoutItem(
                name="NeuroStack Plus",
                description="Hosted checkout contract",
                quantity=1,
                unit_amount_cents=99_900,
            ),
        ),
        callback=CheckoutCallback.from_return_url("https://app.example.test/billing"),
        next_due_date=date(2026, 10, 12) if kind == "subscription" else None,
    )


def test_exact_brl_encoding_never_uses_float() -> None:
    assert brl_amount(99_900) == Decimal("999.00")
    assert brl_amount(250_000) == Decimal("2500.00")
    assert brl_amount(500_000) == Decimal("5000.00")
    assert b'"value":999.00' in _request().body()


def test_closed_provider_contract_has_no_annual_or_subscription_pix() -> None:
    subscription = json.loads(_request().body())
    detached = json.loads(_request("topup").body())

    assert subscription["chargeTypes"] == ["RECURRENT"]
    assert subscription["billingTypes"] == ["CREDIT_CARD"]
    assert subscription["subscription"]["cycle"] == "MONTHLY"
    assert "annual" not in json.dumps(subscription).lower()
    assert detached["chargeTypes"] == ["DETACHED"]
    assert detached["billingTypes"] == ["CREDIT_CARD", "PIX"]
    assert "subscription" not in detached


@pytest.mark.asyncio
async def test_client_uses_fixed_origin_redacts_bad_response_and_validates_id() -> None:
    observed: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["token"] = request.headers["access_token"]
        observed["user_agent"] = request.headers["user-agent"]
        observed["body"] = request.content
        return httpx.Response(
            201,
            json={
                "id": "4111/111111111111",
                "externalReference": "00000000-0000-4000-8000-000000000001",
                "status": "ACTIVE",
                "customer": {"email": "patient@example.test", "cvv": "123"},
            },
        )

    client = AsaasClient(
        api_key=SecretStr("$aact_prod_secret-never-log"),
        user_agent="NeuroStack/test",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(AsaasOutcomeUnknown) as exc_info:
        await client.create_checkout(
            _request(), permit=CreationEgressPermit(_PERMIT_SEAL)
        )

    assert observed["url"] == "https://api.asaas.com/v3/checkouts"
    assert observed["token"] == "$aact_prod_secret-never-log"
    assert observed["user_agent"] == "NeuroStack/test"
    for secret in (
        "$aact_prod_secret-never-log",
        "4111/111111111111",
        "patient@example.test",
        "123",
    ):
        assert secret not in str(exc_info.value)
        assert secret not in repr(exc_info.value)
        assert secret not in repr(client)


@pytest.mark.asyncio
async def test_read_timeout_is_ambiguous_and_not_exposed() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("4111111111111111 cvv=123")

    client = AsaasClient(
        api_key=SecretStr("$aact_prod_timeout-secret"),
        user_agent="NeuroStack/test",
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(AsaasOutcomeUnknown) as exc_info:
        await client.create_checkout(
            _request(), permit=CreationEgressPermit(_PERMIT_SEAL)
        )

    assert str(exc_info.value) == "asaas_transport_unknown"
    assert "4111111111111111" not in repr(exc_info.value)
