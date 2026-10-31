"""Group 5: the Z-API and Meta webhooks skip a suspended builder (SQLite).

A builder past its 72 h of debt service answers ``{"ok": true, "skipped":
"credit_suspended"}`` from the adapter's pre-check: no session row, no LGPD
notice, no LLM, no reply to the patient (on Z-API only what already runs
before the pre-check by D4/OWNER-c -- the mark-as-read).  The same overdue
episode changes nothing before ``credit_enforcement_at`` nor with the
switch off: ``main``'s answer and no transition.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from src.models.content_retention import (
    CreditLifecycleEpisode,
    CreditLifecycleTransition,
)
from src.models.credit import CreditAccount
from src.models.tenant import Tenant
from src.repositories.database import get_db_session
from tests.services.test_credit_metering import (
    Providers,
    World,
    _meta,
    _meta_text,
    _patient_sessions,
    _sends,
    _zapi,
    _zapi_body,
)

pytest_plugins = ["tests.services.test_credit_metering"]

UTC = timezone.utc


async def _suspend(tenant_id: UUID) -> UUID:
    """Plant an overdue episode for the tenant's builder (no capacity)."""

    exhausted = datetime.now(UTC) - timedelta(hours=73)
    database = get_db_session()
    try:
        async with database.begin():
            builder = await database.scalar(
                select(Tenant.builder_id).where(Tenant.id == tenant_id)
            )
            assert builder is not None
            account = await database.scalar(
                select(CreditAccount.id).where(CreditAccount.builder_id == builder)
            )
            if account is None:
                account = uuid4()
                database.add(CreditAccount(id=account, builder_id=builder))
                await database.flush()
            database.add(
                CreditLifecycleEpisode(
                    id=uuid4(),
                    builder_id=builder,
                    account_id=account,
                    opened_effect_id=f"lifecycle:{builder}:episode:0:opened",
                    exhausted_at=exhausted,
                    suspension_due_at=exhausted + timedelta(hours=72),
                    deletion_due_at=exhausted + timedelta(days=30),
                )
            )
    finally:
        await database.close()
    return builder


async def _transitions() -> list[str]:
    database = get_db_session()
    try:
        rows = await database.execute(select(CreditLifecycleTransition.transition))
        return [str(value) for (value,) in rows]
    finally:
        await database.close()


def _send(client: Any, world: World, channel: str, phone: str, message_id: str) -> Any:
    if channel == "zapi":
        return _zapi(client, world, _zapi_body(world, message_id, phone=phone))
    return _meta(client, world, _meta_text(message_id, "Olá, quero agendar", phone))


@pytest.mark.parametrize("channel", ["zapi", "meta"])
def test_a_suspended_builder_is_skipped_with_no_patient_message(
    client: Any, enforced: World, providers: Providers, channel: str
) -> None:
    world = enforced
    tenant = world.zapi_tenant if channel == "zapi" else world.meta_tenant
    client._submit(_suspend(tenant))
    phone = "5511900009101"
    response = _send(client, world, channel, phone, f"{channel}-suspended-1")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "skipped": "credit_suspended"}
    sender = "send_text_message" if channel == "zapi" else "send_meta_cloud_text"
    assert _sends(providers, sender, phone) == []
    assert client._submit(_patient_sessions(tenant, phone)) == []
    providers.llm.assert_not_called()
    if channel == "zapi":
        # D4/OWNER-c: Z-API marks the message read BEFORE the pre-check.
        assert [name for name, _ in providers.sent] == ["mark_message_read"]
    else:
        assert providers.sent == []
    assert client._submit(_transitions()) == ["suspended"]


@pytest.mark.parametrize("channel", ["zapi", "meta"])
def test_before_the_enforcement_an_overdue_episode_changes_nothing(
    client: Any, world: World, providers: Providers, force_switch: Any, channel: str
) -> None:
    tenant = world.zapi_tenant if channel == "zapi" else world.meta_tenant
    client._submit(_suspend(tenant))
    response = _send(client, world, channel, "5511900009201", f"{channel}-main-1")
    assert response.status_code == 200
    assert response.json().get("skipped") != "credit_suspended"
    assert response.json().get("replied") is True
    assert client._submit(_transitions()) == []


@pytest.mark.parametrize("channel", ["zapi", "meta"])
def test_with_the_switch_off_an_overdue_episode_changes_nothing(
    client: Any, enforced: World, providers: Providers, force_switch: Any, channel: str
) -> None:
    force_switch(False)
    world = enforced
    tenant = world.zapi_tenant if channel == "zapi" else world.meta_tenant
    client._submit(_suspend(tenant))
    response = _send(client, world, channel, "5511900009301", f"{channel}-off-1")
    assert response.status_code == 200
    assert response.json().get("replied") is True
    assert client._submit(_transitions()) == []
