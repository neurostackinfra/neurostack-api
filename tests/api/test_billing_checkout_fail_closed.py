"""Fail-closed integration tests for the authoritative hosted checkout path."""

from __future__ import annotations

import json
import socket
from datetime import date
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from src.api.routes import billing
from src.api.schemas.billing import CheckoutRequest
from src.models.billing import BillingCheckoutSession
from src.services import billing_checkout as checkout_module
from src.services.asaas import CheckoutCreated
from src.services.egress_policy import EgressPolicy


class _FlagReader:
    def __init__(self, result: object = False, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    async def is_enabled(self, flag_name: str) -> bool:
        self.calls.append(flag_name)
        if self.error is not None:
            raise self.error
        return self.result  # type: ignore[return-value]


class _Result:
    def __init__(self, value: object) -> None:
        self.value = value

    def scalar_one_or_none(self) -> object:
        return self.value

    def scalar_one(self) -> object:
        return self.value


class _Session:
    def __init__(self, results: list[object]) -> None:
        self.results = results
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, _statement: object) -> _Result:
        return _Result(self.results.pop(0))

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1

    async def flush(self) -> None:
        return None

    def add(self, _value: object) -> None:
        return None


def _checkout(builder_id: UUID) -> BillingCheckoutSession:
    return BillingCheckoutSession(
        id=uuid4(),
        builder_id=builder_id,
        intent_kind="subscription",
        intent_id=uuid4(),
        topup_order_id=None,
        attempt_no=1,
        provider="asaas",
        state="creating",
        catalog_version_id="payment-2026-10-03",
        offer_id="plan_plus_monthly_v1",
        amount_cents=99_900,
        pricing_snapshot={
            "catalog_version_id": "payment-2026-10-03",
            "offer_id": "plan_plus_monthly_v1",
            "amount_cents": 99_900,
            "credit_grant_mc": 5_000,
            "included_clinics": 1,
            "additional_clinics": 0,
            "billing_cycle": "monthly",
            "payment_method": "credit_card",
            "lines": [],
        },
        next_due_date=date(2026, 10, 12),
        reconciliation_attempts=0,
    )


def _checkout_body() -> CheckoutRequest:
    return CheckoutRequest(
        offer_id="plan_plus_monthly_v1",
        method="credit_card",
    )


def _install_general_reservation(
    monkeypatch: pytest.MonkeyPatch,
    checkout: BillingCheckoutSession,
) -> None:
    class Repository:
        def __init__(self, _db: _Session) -> None:
            return None

        async def lock_rollout_control(self, *, environment: str) -> object:
            assert environment == "test"
            return SimpleNamespace(state="general")

        async def reserve_general_subscription_checkout(
            self, **kwargs: object
        ) -> BillingCheckoutSession:
            assert "migration_id" not in kwargs  # the saga was removed (G10 E2)
            # Mirror the repository: the persisted attempt is the planned one.
            checkout.id = kwargs["checkout_session_id"]  # type: ignore[assignment]
            checkout.next_due_date = kwargs["next_due_date"]  # type: ignore[assignment]
            checkout.amount_cents = kwargs["amount_cents"]  # type: ignore[assignment]
            return checkout

        async def subscription_checkout_blocker(
            self, *, builder_id: UUID
        ) -> str | None:
            return None

        async def authorize_reserved_checkout_egress(
            self, *, environment: str, builder_id: UUID, checkout_session_id: UUID
        ) -> BillingCheckoutSession:
            assert environment == "test"
            assert builder_id == checkout.builder_id
            assert checkout_session_id == checkout.id
            return checkout

        async def lock_checkout_session(
            self, *, builder_id: UUID, checkout_session_id: UUID
        ) -> BillingCheckoutSession | None:
            if builder_id == checkout.builder_id and checkout_session_id == checkout.id:
                return checkout
            return None

    monkeypatch.setattr(checkout_module, "BillingRepository", Repository)
    monkeypatch.setattr(
        checkout_module,
        "require_billing_checkout_rollout",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(checkout_module.settings, "environment", "test")
    monkeypatch.setattr(
        checkout_module.settings,
        "asaas_checkout_return_url",
        "https://app.example.test/billing",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reader",
    [
        _FlagReader(False),
        _FlagReader("true"),
        _FlagReader(error=RuntimeError("agent_unavailable")),
    ],
    ids=["false", "invalid", "agent-error"],
)
async def test_checkout_fails_closed_before_provider_or_network(
    monkeypatch: pytest.MonkeyPatch,
    reader: _FlagReader,
) -> None:
    builder_id = uuid4()
    db = _Session([None, None, 0])
    _install_general_reservation(monkeypatch, _checkout(builder_id))
    provider_factory = MagicMock(side_effect=AssertionError("ASAAS client reached"))
    network_calls: list[str] = []

    def forbidden_network(*_args: object, **_kwargs: object) -> Any:
        network_calls.append("network")
        raise AssertionError("provider network reached")

    monkeypatch.setattr(
        checkout_module,
        "require_financial_egress",
        EgressPolicy(reader).require_financial_egress,
    )
    monkeypatch.setattr(billing, "_asaas_client", provider_factory)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden_network)
    monkeypatch.setattr(socket, "socket", forbidden_network)
    monkeypatch.setattr(httpx, "post", forbidden_network)

    with pytest.raises(HTTPException) as exc_info:
        await billing.create_checkout(_checkout_body(), builder_id, db)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "billing_checkout_unavailable"
    assert reader.calls == ["financial_egress_enabled"]
    provider_factory.assert_not_called()
    assert network_calls == []


@pytest.mark.asyncio
async def test_enabled_financial_gate_uses_only_hosted_asaas_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_id = uuid4()
    persisted = _checkout(builder_id)
    db = _Session([None, None, 0])
    _install_general_reservation(monkeypatch, persisted)
    reader = _FlagReader(True)
    client = SimpleNamespace(
        create_checkout=AsyncMock(return_value=CheckoutCreated("chk_000000000000001"))
    )
    factory = MagicMock(return_value=client)
    monkeypatch.setattr(
        checkout_module,
        "require_financial_egress",
        EgressPolicy(reader).require_financial_egress,
    )
    monkeypatch.setattr(billing, "_asaas_client", factory)
    # Group 3: hosted checkout requires a converged webhook/token readback.
    billing.ASAAS_WEBHOOK_READINESS.record(billing.WebhookReadiness(True, ()))

    try:
        response = await billing.create_checkout(_checkout_body(), builder_id, db)  # type: ignore[arg-type]
    finally:
        billing.ASAAS_WEBHOOK_READINESS.reset()

    request = client.create_checkout.await_args.args[0]
    payload = json.loads(request.body())
    assert response.checkout_url == (
        "https://asaas.com/checkoutSession/show?id=chk_000000000000001"
    )
    # Pre-reservation gate read, then the permit-minting read before the POST;
    # never the uazapi flag.
    assert reader.calls == ["financial_egress_enabled", "financial_egress_enabled"]
    assert factory.call_count == 1
    assert payload["chargeTypes"] == ["RECURRENT"]
    assert payload["billingTypes"] == ["CREDIT_CARD"]
    assert payload["subscription"]["cycle"] == "MONTHLY"
    assert "customer" not in payload
    assert "amount_cents" not in payload
    # reservation, pre-POST rollout re-authorization, provider identity
    assert db.commits == 3


class _MissingRolloutRow:
    class _Mappings:
        @staticmethod
        def one_or_none() -> None:
            return None

    @staticmethod
    def mappings() -> "_MissingRolloutRow._Mappings":
        return _MissingRolloutRow._Mappings()


def _configure_asaas(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        billing.settings,
        "asaas_api_key",
        SecretStr("$aact_prod_fixture"),
    )
    monkeypatch.setattr(
        billing.settings,
        "asaas_webhook_token",
        SecretStr("fixture-webhook-token"),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing-table", "missing-row"])
async def test_missing_rollout_schema_or_row_denies_before_any_egress(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    _configure_asaas(monkeypatch)
    provider_factory = MagicMock(side_effect=AssertionError("ASAAS client reached"))
    network_calls: list[str] = []

    def forbidden_network(*_args: object, **_kwargs: object) -> Any:
        network_calls.append("network")
        raise AssertionError("network reached")

    if failure == "missing-table":
        execute = AsyncMock(side_effect=RuntimeError("relation does not exist"))
    else:
        execute = AsyncMock(return_value=_MissingRolloutRow())
    db = SimpleNamespace(execute=execute)

    monkeypatch.setattr(billing, "_asaas_client", provider_factory)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden_network)
    monkeypatch.setattr(socket, "socket", forbidden_network)
    monkeypatch.setattr(httpx, "post", forbidden_network)

    with pytest.raises(HTTPException) as exc_info:
        await billing.create_checkout(_checkout_body(), uuid4(), db)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "billing_checkout_unavailable"
    provider_factory.assert_not_called()
    assert network_calls == []
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_incompatible_asaas_configuration_denies_before_database_or_egress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(billing.settings, "asaas_api_key", SecretStr(""))
    monkeypatch.setattr(billing.settings, "asaas_webhook_token", SecretStr(""))
    db = SimpleNamespace(execute=AsyncMock())
    provider_factory = MagicMock(side_effect=AssertionError("provider reached"))
    monkeypatch.setattr(billing, "_asaas_client", provider_factory)

    with pytest.raises(HTTPException) as exc_info:
        await billing.create_checkout(_checkout_body(), uuid4(), db)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "billing_checkout_unavailable"
    db.execute.assert_not_awaited()
    provider_factory.assert_not_called()


@pytest.mark.asyncio
async def test_uazapi_flag_true_never_opens_the_financial_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IndependentFlags:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def is_enabled(self, flag_name: str) -> bool:
            self.calls.append(flag_name)
            return {
                "financial_egress_enabled": False,
                "uazapi_egress_enabled": True,
            }[flag_name]

    builder_id = uuid4()
    db = _Session([None])
    _install_general_reservation(monkeypatch, _checkout(builder_id))
    reader = IndependentFlags()
    provider_factory = MagicMock(side_effect=AssertionError("ASAAS client reached"))
    monkeypatch.setattr(
        checkout_module,
        "require_financial_egress",
        EgressPolicy(reader).require_financial_egress,
    )
    monkeypatch.setattr(billing, "_asaas_client", provider_factory)

    with pytest.raises(HTTPException) as exc_info:
        await billing.create_checkout(_checkout_body(), builder_id, db)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "billing_checkout_unavailable"
    # Only the financial flag is consulted; the open messaging flag is never
    # read as a substitute and nothing is reserved or sent.
    assert reader.calls == ["financial_egress_enabled"]
    provider_factory.assert_not_called()
    assert db.commits == 0


@pytest.mark.asyncio
async def test_ineligible_subscription_maps_to_one_fixed_409(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.billing_checkout import CheckoutNotEligible

    class Service:
        def __init__(
            self, db: object, *, client_factory: object, **_kwargs: object
        ) -> None:
            del db, client_factory

        async def create_checkout(self, **kwargs: object) -> object:
            del kwargs
            raise CheckoutNotEligible()

    monkeypatch.setattr(billing, "BillingCheckoutService", Service)
    with pytest.raises(HTTPException) as exc_info:
        await billing.create_checkout(_checkout_body(), uuid4(), object())  # type: ignore[arg-type]

    assert exc_info.value.status_code == 409
    assert exc_info.value.detail == "billing_checkout_not_eligible"
