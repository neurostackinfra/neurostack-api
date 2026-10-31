"""Hosted checkout creation reads the F2 financial egress result end to end.

``billing_checkout`` only consumes ``require_financial_egress``; these tests
leave that symbol and ``authorize_creation_egress`` real, so the decision
comes from ``egress_policy`` -> ``AppConfigDataFlagReader`` with a fake
AppConfig Data client (no AWS, no network).
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from src.config.settings import settings
from src.infrastructure.appconfig import AppConfigDataFlagReader
from src.services import asaas as asaas_module
from src.services import billing_checkout as checkout_module
from src.services import egress_policy
from src.services.billing_checkout import BillingCheckoutService, CheckoutUnavailable
from tests.infrastructure.test_appconfig import _Clock, _FakeAppConfigData, _flags
from tests.services.test_billing_checkout import (
    _canary_world,
    _install_world,
    _RecordingClient,
    _Session,
)


def _arrange(
    monkeypatch: pytest.MonkeyPatch, *, source: str, enabled: bool, document: bytes
) -> tuple[AppConfigDataFlagReader, _Clock, _FakeAppConfigData]:
    monkeypatch.setattr(settings, "appconfig_source", source)
    monkeypatch.setattr(settings, "appconfig_enabled", enabled)
    monkeypatch.setattr(settings, "appconfig_application", "neurostack")
    monkeypatch.setattr(settings, "appconfig_environment", "production")
    monkeypatch.setattr(settings, "appconfig_profile", "egress-flags")
    clock = _Clock()
    fake = _FakeAppConfigData([document, RuntimeError("appconfig down")])
    reader = AppConfigDataFlagReader(
        settings, client_factory=lambda: fake, monotonic=clock
    )
    monkeypatch.setattr(egress_policy, "APPCONFIG_DATA_READER", reader)
    return reader, clock, fake


def _real_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    # ``_install_world`` stubs the permit; restore the real one so the real
    # ``require_financial_egress`` that billing_checkout imported decides.
    monkeypatch.setattr(
        checkout_module,
        "authorize_creation_egress",
        asaas_module.authorize_creation_egress,
    )
    assert checkout_module.require_financial_egress is (
        egress_policy.require_financial_egress
    )


@pytest.mark.asyncio
async def test_open_and_fresh_financial_flag_reaches_the_provider_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    builder_id = uuid4()
    world = _canary_world(builder_id)
    _install_world(monkeypatch, world)
    _real_gate(monkeypatch)
    reader, _, fake = _arrange(
        monkeypatch,
        source="sdk",
        enabled=True,
        document=_flags(financial_egress_enabled=True, uazapi_egress_enabled=False),
    )
    assert await reader.refresh_now() is True
    client = _RecordingClient()

    view = await BillingCheckoutService(
        _Session([None, None]), client_factory=lambda: client
    ).create_checkout(
        builder_id=builder_id,
        offer_id="plan_plus_monthly_v1",
        payment_method="credit_card",
    )

    assert view.state == "active"
    assert len(client.requests) == 1
    _, permit = client.requests[0]  # type: ignore[misc]
    assert isinstance(permit, asaas_module.CreationEgressPermit)
    assert len(fake.gets) == 1  # request-time reads never call AppConfig


@pytest.mark.parametrize(
    "case",
    ["closed", "absent", "stale", "error", "never_read", "sdk_disabled", "agent_off"],
)
@pytest.mark.asyncio
async def test_anything_but_open_and_fresh_sends_nothing_and_reserves_nothing(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    builder_id = uuid4()
    world = _canary_world(builder_id)
    _install_world(monkeypatch, world)
    _real_gate(monkeypatch)
    document = {
        "closed": _flags(financial_egress_enabled=False, uazapi_egress_enabled=True),
        "absent": _flags(uazapi_egress_enabled=True),
    }.get(case, _flags(financial_egress_enabled=True))
    reader, clock, fake = _arrange(
        monkeypatch,
        source="agent" if case == "agent_off" else "sdk",
        enabled=case not in ("sdk_disabled", "agent_off"),
        document=document,
    )
    if case not in ("never_read", "sdk_disabled", "agent_off"):
        assert await reader.refresh_now() is True
    if case == "stale":
        clock.now += 3 * reader.poll_interval
    if case == "error":
        assert await reader.refresh_now() is False
    if case in ("sdk_disabled", "agent_off"):
        assert await reader.refresh_now() is False
        assert fake.sessions == []
    client = _RecordingClient()
    db = _Session([None])

    with pytest.raises(CheckoutUnavailable):
        await BillingCheckoutService(db, client_factory=lambda: client).create_checkout(
            builder_id=builder_id,
            offer_id="plan_plus_monthly_v1",
            payment_method="credit_card",
        )

    assert client.requests == []
    assert world.reservations == []
    assert world.lock_calls == 0
    assert db.commits == 0
    assert world.control.canary_subscription_checkout_id is None
