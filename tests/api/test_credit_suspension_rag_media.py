"""Group 5: a suspended builder's RAG and media are suspended for API keys.

The real application on SQLite.  After ``credit_enforcement_at``, with the
switch effective, an API key caller of a builder whose 72 hours of debt are
over gets ``402 {"code": "credit_suspended"}`` from ``POST
/v1/knowledge/search`` and ``GET /v1/media-assets/{id}/content`` (the gate
records the ``suspended`` transition with the worker's effect id; no byte is
sent, no egress is charged).  Inside the 72 hours, before the enforcement
and with the switch off nothing changes; the export stays available.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
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
from src.models.credit_inventory import CreditEgressDelivery
from src.models.media_asset import MediaAsset
from src.repositories.database import get_db_session
from src.services import credit_inventory, credit_metering
from src.services.credit_switch import CreditSwitch
from tests.api.test_credit_category_gate import World, _enforce, _seed, _seed_media

SUSPENDED = {"detail": {"code": "credit_suspended"}}
UTC = timezone.utc


async def _plant_episode(builder: UUID, *, overdue: bool) -> UUID:
    exhausted = datetime.now(UTC) - (
        timedelta(hours=73) if overdue else timedelta(hours=1)
    )
    episode = uuid4()
    database = get_db_session()
    try:
        async with database.begin():
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


async def _media_id(tenant: UUID) -> UUID:
    database = get_db_session()
    try:
        return UUID(
            str(
                await database.scalar(
                    select(MediaAsset.id).where(MediaAsset.tenant_id == tenant)
                )
            )
        )
    finally:
        await database.close()


@pytest.fixture
def world(client: Any) -> World:
    world = client._submit(_seed())
    client._submit(_seed_media(world, 1_000))
    return world


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    def force(effective: bool) -> None:
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: effective)

    force(True)
    return force


@pytest.fixture(autouse=True)
def _fresh_process_state() -> Iterator[None]:
    credit_inventory.forget_enforcement_seen()
    credit_metering.forget_enforcement_seen()
    yield
    credit_inventory.forget_enforcement_seen()
    credit_metering.forget_enforcement_seen()


def _search(client: Any, world: World) -> Any:
    return client.post(
        "/v1/knowledge/search", json={"query": "horários"}, headers=world.headers
    )


def _download(client: Any, world: World) -> Any:
    media = client._submit(_media_id(world.tenant))
    return client.get(f"/v1/media-assets/{media}/content", headers=world.headers)


def _suspensions(client: Any) -> list[tuple[str, str]]:
    return [
        (row.effect_id, row.source)
        for row in client._submit(_rows(CreditLifecycleTransition))
        if row.transition == "suspended"
    ]


def test_rag_search_of_a_suspended_builder_is_402_credit_suspended(
    client: Any, world: World, switch: Any
) -> None:
    client._submit(_enforce())
    episode = client._submit(_plant_episode(world.builder, overdue=True))
    response = _search(client, world)
    assert (response.status_code, response.json()) == (402, SUSPENDED)
    assert _suspensions(client) == [(f"lifecycle:{episode}:suspended", "effect")]
    # A retry answers the same and records nothing new.
    assert _search(client, world).json() == SUSPENDED
    assert len(_suspensions(client)) == 1


def test_media_download_of_a_suspended_builder_is_402_and_sends_nothing(
    client: Any, world: World, switch: Any
) -> None:
    client._submit(_enforce())
    episode = client._submit(_plant_episode(world.builder, overdue=True))
    response = _download(client, world)
    assert (response.status_code, response.json()) == (402, SUSPENDED)
    assert client._submit(_rows(CreditEgressDelivery)) == []
    assert _suspensions(client) == [(f"lifecycle:{episode}:suspended", "effect")]
    # The export stays available to the suspended builder's tenant.
    assert client.get("/v1/credits/export", headers=world.headers).status_code == 200


def test_inside_its_72_hours_rag_and_media_are_still_served(
    client: Any, world: World, switch: Any
) -> None:
    client._submit(_enforce())
    client._submit(_plant_episode(world.builder, overdue=False))
    assert _search(client, world).status_code == 200
    assert _download(client, world).status_code == 200
    assert _suspensions(client) == []


def test_before_the_enforcement_an_overdue_episode_suspends_nothing(
    client: Any, world: World, switch: Any
) -> None:
    client._submit(_plant_episode(world.builder, overdue=True))
    assert _search(client, world).status_code == 200
    assert _download(client, world).status_code == 200
    assert _suspensions(client) == []


def test_with_the_switch_off_an_overdue_episode_suspends_nothing(
    client: Any, world: World, switch: Callable[[bool], None]
) -> None:
    client._submit(_enforce())
    client._submit(_plant_episode(world.builder, overdue=True))
    switch(False)
    assert _search(client, world).status_code == 200
    assert _download(client, world).status_code == 200
    assert _suspensions(client) == []
