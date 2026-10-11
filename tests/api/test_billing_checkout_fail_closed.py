"""Fail-closed coverage for the AbacatePay checkout choke point."""

from __future__ import annotations

import socket
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from src.api.routes import billing
from src.api.schemas.billing import CheckoutRequest
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


class _BuilderResult:
    def scalar_one_or_none(self) -> SimpleNamespace:
        return SimpleNamespace(name="Checkout Builder")


def _checkout_body() -> CheckoutRequest:
    return CheckoutRequest(plan="start", cycle="monthly", method="pix")


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
    provider_factory = MagicMock(side_effect=AssertionError("provider client reached"))
    network_calls: list[str] = []

    def forbidden_dns(*_args: object, **_kwargs: object) -> Any:
        network_calls.append("dns")
        raise AssertionError("provider DNS reached")

    def forbidden_http(*_args: object, **_kwargs: object) -> Any:
        network_calls.append("http")
        raise AssertionError("provider HTTP reached")

    def forbidden_socket(*_args: object, **_kwargs: object) -> Any:
        network_calls.append("socket")
        raise AssertionError("provider socket reached")

    monkeypatch.setattr(
        billing,
        "require_billing_checkout_rollout",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        billing,
        "require_financial_egress",
        EgressPolicy(reader).require_financial_egress,
    )
    monkeypatch.setattr(billing, "_get_client", provider_factory)
    monkeypatch.setattr(billing, "_count_active_clinics", AsyncMock(return_value=0))
    monkeypatch.setattr(socket, "getaddrinfo", forbidden_dns)
    monkeypatch.setattr(socket, "socket", forbidden_socket)
    monkeypatch.setattr(httpx, "post", forbidden_http)
    db = SimpleNamespace(execute=AsyncMock(return_value=_BuilderResult()))

    with pytest.raises(HTTPException) as exc_info:
        await billing.create_checkout(_checkout_body(), uuid4(), db)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "financial_egress_disabled"
    assert reader.calls == ["financial_egress_enabled"]
    provider_factory.assert_not_called()
    assert network_calls == []


@pytest.mark.asyncio
async def test_financial_gate_is_independent_from_uazapi_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IndependentFlags:
        calls: list[str] = []

        async def is_enabled(self, flag_name: str) -> bool:
            self.calls.append(flag_name)
            return {"financial_egress_enabled": False, "uazapi_egress_enabled": True}[
                flag_name
            ]

    reader = IndependentFlags()
    provider_factory = MagicMock()
    monkeypatch.setattr(
        billing,
        "require_billing_checkout_rollout",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        billing,
        "require_financial_egress",
        EgressPolicy(reader).require_financial_egress,
    )
    monkeypatch.setattr(billing, "_get_client", provider_factory)
    monkeypatch.setattr(billing, "_count_active_clinics", AsyncMock(return_value=0))
    db = SimpleNamespace(execute=AsyncMock(return_value=_BuilderResult()))

    with pytest.raises(HTTPException) as exc_info:
        await billing.create_checkout(_checkout_body(), uuid4(), db)

    assert exc_info.value.detail == "financial_egress_disabled"
    assert reader.calls == ["financial_egress_enabled"]
    provider_factory.assert_not_called()


@pytest.mark.asyncio
async def test_enabled_financial_gate_preserves_checkout_provider_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = _FlagReader(True)
    provider = MagicMock()
    provider.create_pix_charge.return_value = {
        "qr_code": "pix-code",
        "qr_code_url": "https://pay.example.invalid/qr",
        "charge_id": "charge-1",
    }
    provider_factory = MagicMock(return_value=provider)
    monkeypatch.setattr(
        billing,
        "require_billing_checkout_rollout",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        billing,
        "require_financial_egress",
        EgressPolicy(reader).require_financial_egress,
    )
    monkeypatch.setattr(billing, "_get_client", provider_factory)
    monkeypatch.setattr(billing, "_count_active_clinics", AsyncMock(return_value=0))
    builder_id = uuid4()
    db = SimpleNamespace(execute=AsyncMock(return_value=_BuilderResult()))

    response = await billing.create_checkout(_checkout_body(), builder_id, db)

    assert response.method == "pix"
    assert response.qr_code == "pix-code"
    assert response.subscription_id == "charge-1"
    assert reader.calls == ["financial_egress_enabled"]
    provider_factory.assert_called_once_with()
    provider.create_pix_charge.assert_called_once_with(
        amount_cents=200_000,
        description="NeuroStack Start - Mensal (PIX)",
        metadata={
            "builder_id": str(builder_id),
            "plan": "start",
            "cycle": "monthly",
        },
    )


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
    provider_factory = MagicMock(side_effect=AssertionError("Abacate client reached"))
    asaas_factory = MagicMock(side_effect=AssertionError("ASAAS client reached"))
    network_calls: list[str] = []

    def forbidden_network(*_args: object, **_kwargs: object) -> Any:
        network_calls.append("network")
        raise AssertionError("network reached")

    if failure == "missing-table":
        execute = AsyncMock(side_effect=RuntimeError("relation does not exist"))
    else:
        execute = AsyncMock(return_value=_MissingRolloutRow())
    db = SimpleNamespace(execute=execute)

    monkeypatch.setattr(billing, "_get_client", provider_factory)
    monkeypatch.setattr(billing, "AsaasClient", asaas_factory, raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden_network)
    monkeypatch.setattr(socket, "socket", forbidden_network)
    monkeypatch.setattr(httpx, "post", forbidden_network)

    with pytest.raises(HTTPException) as exc_info:
        await billing.create_checkout(_checkout_body(), uuid4(), db)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "billing_checkout_unavailable"
    provider_factory.assert_not_called()
    asaas_factory.assert_not_called()
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
    monkeypatch.setattr(billing, "_get_client", provider_factory)

    with pytest.raises(HTTPException) as exc_info:
        await billing.create_checkout(_checkout_body(), uuid4(), db)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "billing_checkout_unavailable"
    db.execute.assert_not_awaited()
    provider_factory.assert_not_called()
