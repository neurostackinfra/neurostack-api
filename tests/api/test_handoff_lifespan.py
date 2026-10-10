"""Application teardown coverage for handoff-owned shared resources."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import src.api.main as main_module
import src.repositories as repositories_module
import src.services.handoff_service as handoff_service_module


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_stage",
    [None, "rlhf", "handoff", "database"],
)
async def test_lifespan_runs_every_cleanup_in_dependency_order(
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str | None,
) -> None:
    order: list[str] = []

    async def step(name: str) -> None:
        order.append(name)
        if failure_stage == name:
            raise RuntimeError(f"{name} shutdown failed")

    class Dispatcher:
        async def close(self) -> None:
            await step("handoff")

    async def init_database() -> None:
        order.append("database_init")

    async def close_database() -> None:
        await step("database")

    async def shutdown_rlhf() -> None:
        await step("rlhf")

    service = SimpleNamespace(webhook_dispatcher=Dispatcher())
    monkeypatch.setattr(main_module.settings, "metrics_enabled", False)
    monkeypatch.setattr(main_module, "configure_tracing", MagicMock())
    monkeypatch.setattr(main_module, "instrument_redis", MagicMock())
    monkeypatch.setattr(main_module, "instrument_sqlalchemy", MagicMock())
    monkeypatch.setattr(repositories_module, "init_database", init_database)
    monkeypatch.setattr(repositories_module, "close_database", close_database)
    monkeypatch.setattr(main_module.rlhf, "shutdown", shutdown_rlhf)
    monkeypatch.setattr(handoff_service_module, "_service", service)

    async def run_lifespan() -> None:
        async with main_module.lifespan(main_module.app):
            assert handoff_service_module._service is service

    if failure_stage is None:
        await run_lifespan()
    else:
        with pytest.raises(RuntimeError, match=f"{failure_stage} shutdown failed"):
            await run_lifespan()

    assert order == [
        "database_init",
        "rlhf",
        "handoff",
        "database",
    ]
    assert handoff_service_module._service is None
