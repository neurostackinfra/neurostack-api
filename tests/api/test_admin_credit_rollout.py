"""Read-only Group 7 rollout evidence surface.

All four ``/v1/admin/credits/rollout/*`` GET routes: a super-admin JWT reads
them without writing ``credit_enforcement_state``; with the credit switch
off each answers 503 ``credit_unavailable``; a builder JWT, a clinic-admin
JWT and a tenant API key are refused (403) on every one of them.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from uuid import uuid4

import jwt
import pytest
from sqlalchemy import func, select

from src.config.settings import settings
from src.models.credit import (
    CreditCapabilityReadiness,
    CreditEffect,
    CreditEnforcementState,
)
from src.models.credit_enforcement import CreditCutoffAcceptance
from src.repositories.database import get_db_session
from tests.api.test_credit_operational_authorization import ADMIN, World

pytest_plugins = ["tests.api.test_credit_operational_authorization"]

ROUTES = (
    "/rollout/reconciliation",
    "/rollout/readiness",
    "/rollout/status",
    "/rollout/bindings/{builder}",
)


async def _written_rows() -> dict[str, int]:
    """Rows of every table a rollout read could be tempted to write."""

    session = get_db_session()
    try:
        return {
            model.__tablename__: int(
                await session.scalar(select(func.count()).select_from(model)) or 0
            )
            for model in (
                CreditEnforcementState,
                CreditEffect,
                CreditCapabilityReadiness,
                CreditCutoffAcceptance,
            )
        }
    finally:
        await session.close()


NOTHING_WRITTEN = {
    "credit_enforcement_state": 0,
    "credit_effects": 0,
    "credit_capability_readiness": 0,
    "credit_cutoff_acceptances": 0,
}


def _path(route: str, world: World) -> str:
    return ADMIN + route.format(builder=world.builder)


def _builder_bearer(world: World) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(world.tenant),
            "role": "builder",
            "builder_id": str(world.builder),
            "iat": now,
            "exp": now + timedelta(hours=1),
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return {"Authorization": f"Bearer {token}"}


def test_rollout_evidence_is_super_admin_read_only(client: Any, world: World) -> None:
    headers = world.bearer("admin_a")
    reconciliation = client.get(f"{ADMIN}/rollout/reconciliation", headers=headers)
    readiness = client.get(f"{ADMIN}/rollout/readiness", headers=headers)
    status = client.get(f"{ADMIN}/rollout/status", headers=headers)
    binding = client.get(f"{ADMIN}/rollout/bindings/{uuid4()}", headers=headers)

    assert reconciliation.status_code == 200
    assert readiness.status_code == 200
    assert status.status_code == 200
    assert binding.status_code == 200
    assert status.json()["enforcement_at"] is None
    # A builder with no funded source is allowed to reach the debt lifecycle;
    # only an unresolved source is ``credit_pending``.
    assert binding.json()["usable"] is True
    assert binding.json()["reconciliation_failure"] is False
    assert client._submit(_written_rows()) == NOTHING_WRITTEN


@pytest.mark.parametrize("route", ROUTES)
def test_switch_off_answers_credit_unavailable_on_every_rollout_route(
    client: Any, world: World, switch: Callable[[bool], None], route: str
) -> None:
    switch(False)
    response = client.get(_path(route, world), headers=world.bearer("admin_a"))
    assert response.status_code == 503
    assert response.json() == {"code": "credit_unavailable"}
    assert client._submit(_written_rows()) == NOTHING_WRITTEN


@pytest.mark.parametrize("caller", ["builder", "clinic_admin", "api_key"])
@pytest.mark.parametrize("route", ROUTES)
def test_rollout_routes_refuse_every_non_super_admin_caller(
    client: Any, world: World, route: str, caller: str
) -> None:
    headers = {
        "builder": _builder_bearer(world),
        "clinic_admin": world.bearer("officer_1"),
        "api_key": {"X-API-Key": world.api_key},
    }[caller]
    response = client.get(_path(route, world), headers=headers)
    assert response.status_code == 403, response.text
    assert client._submit(_written_rows()) == NOTHING_WRITTEN
