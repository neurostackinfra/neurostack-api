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


def _client(handler: object) -> AsaasClient:
    return AsaasClient(
        api_key=SecretStr("$aact_prod_fixture-key"),
        user_agent="NeuroStack/test",
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_connect_error_is_conclusively_not_sent() -> None:
    from src.services.asaas import AsaasRequestRejected

    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure for 4111111111111111")

    with pytest.raises(AsaasRequestRejected) as exc_info:
        await _client(handler).create_checkout(
            _request(), permit=CreationEgressPermit(_PERMIT_SEAL)
        )
    assert str(exc_info.value) == "asaas_request_not_sent"
    assert "4111111111111111" not in repr(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "conclusive"),
    [(400, True), (422, True), (409, False), (500, False), (502, False), (302, False)],
)
async def test_only_listed_statuses_are_conclusive_rejections(
    status: int, conclusive: bool
) -> None:
    from src.services.asaas import AsaasRequestRejected

    async def handler(_request: httpx.Request) -> httpx.Response:
        headers = {"location": "https://evil.example.test/"} if status == 302 else {}
        return httpx.Response(status, json={"errors": []}, headers=headers)

    expected = AsaasRequestRejected if conclusive else AsaasOutcomeUnknown
    with pytest.raises(expected) as exc_info:
        await _client(handler).create_checkout(
            _request(), permit=CreationEgressPermit(_PERMIT_SEAL)
        )
    if not conclusive:
        assert type(exc_info.value) is AsaasOutcomeUnknown


@pytest.mark.asyncio
async def test_redirect_is_never_followed() -> None:
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://evil.example.test/x"})

    with pytest.raises(AsaasOutcomeUnknown):
        await _client(handler).create_checkout(
            _request(), permit=CreationEgressPermit(_PERMIT_SEAL)
        )
    assert seen == ["https://api.asaas.com/v3/checkouts"]


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [True, False])
async def test_oversized_response_is_ambiguous_and_never_parsed(declared: bool) -> None:
    from src.services.asaas import ASAAS_MAX_RESPONSE_BYTES

    body = b'{"id":"' + b"a" * (ASAAS_MAX_RESPONSE_BYTES + 1) + b'"}'

    async def handler(_request: httpx.Request) -> httpx.Response:
        if declared:
            return httpx.Response(201, content=body)

        async def chunks():  # type: ignore[no-untyped-def]
            for start in range(0, len(body), 8192):
                yield body[start : start + 8192]

        return httpx.Response(201, content=chunks())  # no content-length

    with pytest.raises(AsaasOutcomeUnknown) as exc_info:
        await _client(handler).create_checkout(
            _request(), permit=CreationEgressPermit(_PERMIT_SEAL)
        )
    assert str(exc_info.value) == "asaas_response_too_large"


@pytest.mark.asyncio
async def test_containment_takes_no_permit_and_ignores_the_creation_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import inspect

    from src.services import asaas as asaas_module
    from src.services import egress_policy

    async def closed_flag() -> None:
        raise AssertionError("containment consulted the creation flag")

    monkeypatch.setattr(egress_policy, "require_financial_egress", closed_flag)
    monkeypatch.setattr(asaas_module, "authorize_creation_egress", closed_flag)

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/cancel"):
            return httpx.Response(200, json={"id": "chk_1", "status": "CANCELED"})
        if path.endswith("/refund"):
            return httpx.Response(200, json={"id": "pay_1", "status": "REFUNDED"})
        return httpx.Response(200, json={"id": "sub_1", "deleted": True})

    client = _client(handler)
    for method in (
        client.cancel_checkout,
        client.refund_payment,
        client.delete_subscription,
    ):
        assert "permit" not in inspect.signature(method).parameters
    assert (await client.cancel_checkout("chk_1")).status == "CANCELED"
    assert (await client.refund_payment("pay_1")).status == "REFUNDED"
    assert await client.delete_subscription("sub_1") is True


@pytest.mark.parametrize("kind", ["subscription", "topup"])
def test_committed_request_fixtures_are_the_exact_bytes_the_service_sends(
    kind: str,
) -> None:
    from pathlib import Path

    from src.services.billing_catalog import BILLING_CATALOG
    from src.services.billing_checkout import (
        BillingCheckoutService,
        subscription_pricing_snapshot,
        topup_pricing_snapshot,
    )

    callback = CheckoutCallback.from_return_url("https://app.example.test/billing")
    if kind == "subscription":
        offer = BILLING_CATALOG["plan_plus_monthly_v1"]
        snapshot = subscription_pricing_snapshot(offer, active_clinics=1)
        reference, due = UUID("00000000-0000-4000-8000-000000000001"), date(
            2026, 10, 11
        )
    else:
        offer = BILLING_CATALOG["package_credits_500_v1"]
        snapshot = topup_pricing_snapshot(offer, payment_method="pix")
        reference, due = UUID("00000000-0000-4000-8000-000000000002"), None
    request = BillingCheckoutService._build_request(
        kind=kind,  # type: ignore[arg-type]
        checkout_session_id=reference,
        snapshot=snapshot,
        offer=offer,
        next_due_date=due,
        callback=callback,
    )
    fixture = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "asaas"
        / "checkout"
        / f"{kind}_request.json"
    )
    assert fixture.read_bytes() == request.body() + b"\n"
    parsed = json.loads(fixture.read_bytes(), parse_float=Decimal)
    assert isinstance(parsed["items"][0]["value"], Decimal)  # never a float
