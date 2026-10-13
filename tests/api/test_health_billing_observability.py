"""Readiness must fail closed when billing observability evidence is invalid."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from fastapi import Response, status

from src.api.routes import health


def test_production_readiness_rejects_invalid_billing_observability(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        health,
        "settings",
        SimpleNamespace(
            is_production=True,
            abacatepay_webhook_secret=SimpleNamespace(
                get_secret_value=lambda: "present"
            ),
            is_billing_observability_compatible=False,
        ),
    )
    response = Response()

    result = asyncio.run(health.readiness_check(response))

    assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
    assert result.ready is False
    assert result.checks["billing_observability"] is False
