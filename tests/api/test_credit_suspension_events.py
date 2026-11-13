"""Group 5: ``POST /v1/events`` answers a suspended builder ``402
credit_suspended`` (SQLite, the real application).

The suspension is decided by the request's own pre-check: past the 72 h
deadline it evaluates the builder's episode under the account lock and
records the ``suspended`` transition (the worker's effect id) before
answering -- no LLM, no pipeline, no claim.  Before the deadline the
builder is still served as debt; after paying it is served again; and the
whole gate is inert before ``credit_enforcement_at`` and with the switch off
(``main``'s answer, nothing recorded).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from src.models.content_retention import (
    CreditLifecycleEpisode,
    CreditLifecycleTransition,
)
from src.models.credit import (
    CreditAccount,
    CreditEffect,
    CreditEnforcementState,
    CreditIngressClaim,
    CreditLedgerEntry,
    CreditObservationalEffect,
)
from src.repositories.database import get_db_session
from tests.api.test_events_credit_enforced import (
    EVENTS,
    World,
    _body,
    _FakeLLM,
    _forbid_pipeline,
    _grant,
    _jwt,
    _post,
)

pytest_plugins = ["tests.api.test_events_credit_enforced"]

UTC = timezone.utc


async def _enforce_at(instant: datetime) -> None:
    database = get_db_session()
    try:
        async with database.begin():
            database.add(
                CreditEnforcementState(
                    id=1, credit_enforcement_at=instant, set_by="test", set_at=instant
                )
            )
    finally:
        await database.close()


async def _plant_episode(builder: UUID, *, overdue: bool) -> UUID:
    """An open exhaustion episode of a builder with no capacity: overdue
    (its 72 h are over) or still in its debt window."""

    exhausted = datetime.now(UTC) - (
        timedelta(hours=73) if overdue else timedelta(hours=1)
    )
    episode = uuid4()
    database = get_db_session()
    try:
        async with database.begin():
            account = await database.scalar(
                select(CreditAccount.id).where(CreditAccount.builder_id == builder)
            )
            if account is None:
                account = uuid4()
                database.add(CreditAccount(id=account, builder_id=builder))
                await database.flush()
            database.add(
                CreditLifecycleEpisode(
                    id=episode,
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
    return episode


async def _rows(model: Any) -> list[Any]:
    database = get_db_session()
    try:
        return list((await database.execute(select(model))).scalars())
    finally:
        await database.close()


def _transitions(client: Any) -> list[tuple[str, str]]:
    return [
        (row.transition, row.source)
        for row in client._submit(_rows(CreditLifecycleTransition))
    ]


@pytest.fixture
def enforced_long_ago(client: Any, world: World) -> World:
    client._submit(_enforce_at(datetime.now(UTC) - timedelta(days=10)))
    return world


def test_a_suspended_builder_is_402_credit_suspended_and_not_served(
    client: Any,
    enforced_long_ago: World,
    switch: Any,
    llm: _FakeLLM,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = enforced_long_ago
    episode = client._submit(_plant_episode(world.builder, overdue=True))
    _forbid_pipeline(monkeypatch)
    response = _post(client, world, key="suspended-1")
    assert response.status_code == 402
    assert response.json() == {"code": "credit_suspended"}
    assert llm.calls == 0
    assert client._submit(_rows(CreditIngressClaim)) == []
    [suspended] = [
        row
        for row in client._submit(_rows(CreditLifecycleTransition))
        if row.transition == "suspended"
    ]
    assert suspended.episode_id == episode
    assert suspended.source == "effect"
    assert suspended.effect_id == f"lifecycle:{episode}:suspended"
    # A retry is answered the same way and records nothing new.
    again = _post(client, world, key="suspended-2")
    assert again.json() == {"code": "credit_suspended"}
    assert _transitions(client).count(("suspended", "effect")) == 1


def test_a_builder_still_inside_its_72_hours_is_served_as_debt(
    client: Any, enforced_long_ago: World, switch: Any, llm: _FakeLLM
) -> None:
    world = enforced_long_ago
    client._submit(_plant_episode(world.builder, overdue=False))
    response = _post(client, world, key="grace-1")
    assert response.status_code == 200, response.text
    assert llm.calls == 1
    assert "suspended" not in [t for t, _ in _transitions(client)]


def test_a_builder_that_paid_is_reactivated_and_served_again(
    client: Any, enforced_long_ago: World, switch: Any, llm: _FakeLLM
) -> None:
    world = enforced_long_ago
    client._submit(_plant_episode(world.builder, overdue=True))
    assert _post(client, world, key="before-pay").json() == {"code": "credit_suspended"}
    client._submit(_grant(world.builder, 10_000))
    response = _post(client, world, key="after-pay")
    assert response.status_code == 200, response.text
    assert llm.calls == 1
    assert ("reactivated", "effect") in _transitions(client)


def test_before_the_enforcement_an_overdue_episode_changes_nothing(
    client: Any, world: World, switch: Any, llm: _FakeLLM
) -> None:
    client._submit(_plant_episode(world.builder, overdue=True))
    response = _post(client, world, key=None)
    assert response.status_code == 200
    assert llm.calls == 1
    assert _transitions(client) == []


def test_with_the_switch_off_an_overdue_episode_changes_nothing(
    client: Any,
    enforced_long_ago: World,
    switch: Callable[[bool], None],
    llm: _FakeLLM,
) -> None:
    switch(False)
    client._submit(_plant_episode(enforced_long_ago.builder, overdue=True))
    response = _post(client, enforced_long_ago, key=None)
    assert response.status_code == 200
    assert llm.calls == 1
    assert _transitions(client) == []


# ---------------------------------------------------------------------------
# debito-cortesia-automatico Group 2: ``POST /v1/events`` under the courtesy
# rule, with NO ``credit_enforcement_at``.  A blocked builder is 402
# ``credit_suspended`` with nothing recorded; a charged one is served even
# with an overdue episode left from a mark era.
# ---------------------------------------------------------------------------


@pytest.fixture
def rule_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process reading at this image's head (046 and 047 gates open)."""

    from src.services.credit_switch import (
        CreditSwitch,
        CreditSwitchReading,
        load_image_migration_chain,
    )

    reading = CreditSwitchReading(
        True, "schema_ready", load_image_migration_chain()[-1]
    )
    monkeypatch.setattr(CreditSwitch, "reading", lambda _self: reading)


async def _courtesy(builder: UUID, *, keep: bool) -> None:
    from src.services import credit_courtesy
    from src.services.credit_ledger import CreditLedger

    database = get_db_session()
    try:
        async with database.begin():
            granted = await credit_courtesy.grant(
                database,
                builder_id=builder,
                credits=5,
                reason="courtesy rule",
                actor_user_id=uuid4(),
                idempotency_key=f"rule-{uuid4()}",
                ledger=CreditLedger(database),
            )
        if not keep:
            async with database.begin():
                await credit_courtesy.remove(
                    database,
                    lot_id=granted.lot_id,
                    remove_all=True,
                    reason="courtesy rule",
                    actor_user_id=uuid4(),
                    idempotency_key=f"rule-removal-{uuid4()}",
                    ledger=CreditLedger(database),
                )
    finally:
        await database.close()


def test_a_blocked_rule_builder_is_402_credit_suspended_and_not_served(
    client: Any,
    world: World,
    switch: Any,
    llm: _FakeLLM,
    rule_reading: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client._submit(_courtesy(world.builder, keep=False))
    _forbid_pipeline(monkeypatch)
    response = _post(client, world, key="rule-blocked-1")
    assert response.status_code == 402
    assert response.json() == {"code": "credit_suspended"}
    assert llm.calls == 0
    assert client._submit(_rows(CreditIngressClaim)) == []
    assert _transitions(client) == []
    # DESIGN Risk 4 (accepted): the rule is the enforced path, so a call
    # without an Idempotency-Key is refused before the block.
    keyless = _post(client, world, key=None)
    assert keyless.status_code == 400
    assert keyless.json() == {"code": "idempotency_key_required"}


def test_the_panel_simulator_is_served_free_for_a_rule_blocked_builder(
    client: Any, world: World, switch: Any, llm: _FakeLLM, rule_reading: None
) -> None:
    """The runbook promises the panel's simulator stays open for a builder
    the courtesy rule BLOCKS: only origin ``production`` is ever enforced
    on ``/v1/events`` -- ``capture_events_enforcement`` returns for any
    other origin before it reads the builder's mode -- so a panel (JWT,
    ``simulator``) turn is served without an ``Idempotency-Key``, charges
    nothing and suspends nothing, while the same builder's API key is
    refused."""

    client._submit(_courtesy(world.builder, keep=False))
    # (a) The block is in effect for this builder, under the RULE (no
    # global, no mark).
    assert client._submit(_rows(CreditEnforcementState)) == []
    blocked = _post(client, world, key="rule-blocked-panel-1")
    assert blocked.status_code == 402
    assert blocked.json() == {"code": "credit_suspended"}
    assert llm.calls == 0
    effects = len(client._submit(_rows(CreditEffect)))
    entries = len(client._submit(_rows(CreditLedgerEntry)))
    observed = {row.id for row in client._submit(_rows(CreditObservationalEffect))}
    # (b) The panel turn, with no Idempotency-Key, is served.
    response = client.post(
        EVENTS, json=_body(world.jwt_session), headers=_jwt(world.jwt_tenant)
    )
    assert response.status_code == 200, response.text
    assert llm.calls == 1
    # (c) Nothing claimed, charged or suspended.
    assert client._submit(_rows(CreditIngressClaim)) == []
    assert len(client._submit(_rows(CreditEffect))) == effects
    assert len(client._submit(_rows(CreditLedgerEntry))) == entries
    assert _transitions(client) == []
    # (d) Its observational row (the global is unset, so the writer records
    # it; the ``switch`` fixture gives that write a generous budget) says
    # simulator and would debit nothing.
    (row,) = [
        row
        for row in client._submit(_rows(CreditObservationalEffect))
        if row.id not in observed
    ]
    assert (row.origin, row.would_debit_mc) == ("simulator", 0)
    assert row.tenant_id == world.jwt_tenant


def test_a_charged_rule_builder_with_a_stale_overdue_episode_is_served(
    client: Any, world: World, switch: Any, llm: _FakeLLM, rule_reading: None
) -> None:
    client._submit(_courtesy(world.builder, keep=True))
    client._submit(_plant_episode(world.builder, overdue=True))  # a mark era's
    response = _post(client, world, key="rule-charged-1")
    assert response.status_code == 200, response.text
    assert llm.calls == 1
    assert _transitions(client) == []  # never suspended by that episode
