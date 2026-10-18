"""Readiness must fail closed when billing observability evidence is invalid."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import Response, status

from src.api.routes import health

READINESS_CHECKS = {"api", "database", "redis", "qdrant", "billing_observability"}


def test_production_readiness_rejects_invalid_billing_observability(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        health,
        "settings",
        SimpleNamespace(
            is_production=True,
            is_billing_observability_compatible=False,
        ),
    )
    response = Response()

    result = asyncio.run(health.readiness_check(response))

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert result.ready is False
    assert result.checks["billing_observability"] is False


def _live_production_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    async def reachable(*_args: object) -> bool:
        return True

    for probe in ("_database_ready", "_redis_ready", "_qdrant_ready"):
        monkeypatch.setattr(health, probe, reachable)


@pytest.mark.asyncio
async def test_production_readiness_accepts_complete_billing_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A production readiness pass needs the Group 5 billing observability
    # evidence and reachable dependencies; provide a complete, validated
    # production configuration instead of flipping one field.
    from tests.config.test_asaas_settings import _production_settings

    monkeypatch.setattr(health, "settings", _production_settings())
    _live_production_dependencies(monkeypatch)
    response = Response()

    result = await health.readiness_check(response)

    assert response.status_code == 200
    assert result.ready is True
    # The retired legacy gateway check is gone; no other check was added.
    assert set(result.checks) == READINESS_CHECKS
    assert all(result.checks.values())


@pytest.mark.asyncio
async def test_non_production_readiness_reports_the_same_check_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health.settings, "environment", "test")
    response = Response()

    result = await health.readiness_check(response)

    assert result.ready is True
    assert set(result.checks) == READINESS_CHECKS


@pytest.mark.asyncio
@pytest.mark.parametrize("probe", ["_database_ready", "_redis_ready", "_qdrant_ready"])
async def test_production_readiness_still_requires_live_dependencies(
    monkeypatch: pytest.MonkeyPatch, probe: str
) -> None:
    from tests.config.test_asaas_settings import _production_settings

    monkeypatch.setattr(health, "settings", _production_settings())
    _live_production_dependencies(monkeypatch)

    async def unreachable(*_args: object) -> bool:
        return False

    monkeypatch.setattr(health, probe, unreachable)
    response = Response()

    result = await health.readiness_check(response)

    assert response.status_code == 503
    assert result.ready is False
    failed = {name for name, ok in result.checks.items() if not ok}
    assert failed == {probe.removeprefix("_").removesuffix("_ready")}


@pytest.mark.asyncio
async def test_production_readiness_rejects_missing_runtime_acknowledgement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.config.test_asaas_settings import _production_settings

    configured = _production_settings()
    monkeypatch.setattr(
        health,
        "settings",
        configured.model_copy(update={"billing_observability_acknowledgement": ""}),
    )
    _live_production_dependencies(monkeypatch)
    response = Response()

    result = await health.readiness_check(response)

    assert response.status_code == 503
    assert {name for name, ok in result.checks.items() if not ok} == {
        "billing_observability"
    }
