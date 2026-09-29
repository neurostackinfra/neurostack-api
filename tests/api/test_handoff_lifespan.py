"""Application teardown coverage for handoff-owned shared resources."""

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import src.api.main as main_module
import src.memory.event_store as event_store_module
import src.repositories as repositories_module
import src.services.handoff_service as handoff_service_module


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown_fails", [False, True])
async def test_lifespan_closes_handoff_event_store_and_database_in_order(
    monkeypatch: pytest.MonkeyPatch,
    shutdown_fails: bool,
) -> None:
    order: list[str] = []

    class Dispatcher:
        async def close(self) -> None:
            order.append("handoff_dispatcher")

    class Store:
        async def disconnect(self) -> None:
            order.append("event_store")

    async def init_database() -> None:
        order.append("database_init")

    async def close_database() -> None:
        order.append("database")

    async def shutdown_rlhf() -> None:
        order.append("rlhf")
        if shutdown_fails:
            raise RuntimeError("rlhf shutdown failed")

    service = SimpleNamespace(webhook_dispatcher=Dispatcher())
    store = Store()
    monkeypatch.setattr(main_module.settings, "metrics_enabled", False)
    monkeypatch.setattr(main_module, "configure_tracing", MagicMock())
    monkeypatch.setattr(main_module, "instrument_redis", MagicMock())
    monkeypatch.setattr(main_module, "instrument_sqlalchemy", MagicMock())
    monkeypatch.setattr(repositories_module, "init_database", init_database)
    monkeypatch.setattr(repositories_module, "close_database", close_database)
    monkeypatch.setattr(main_module.rlhf, "shutdown", shutdown_rlhf)
    monkeypatch.setattr(handoff_service_module, "_service", service)
    monkeypatch.setattr(event_store_module, "_event_store", store)
    monkeypatch.setattr(
        event_store_module,
        "_event_store_lifecycle_lock",
        asyncio.Lock(),
    )

    async def run_lifespan() -> None:
        async with main_module.lifespan(main_module.app):
            assert handoff_service_module._service is service
            assert event_store_module._event_store is store

    if shutdown_fails:
        with pytest.raises(RuntimeError, match="rlhf shutdown failed"):
            await run_lifespan()
    else:
        await run_lifespan()

    assert order == [
        "database_init",
        "rlhf",
        "handoff_dispatcher",
        "event_store",
        "database",
    ]
    assert handoff_service_module._service is None
    assert event_store_module._event_store is None
