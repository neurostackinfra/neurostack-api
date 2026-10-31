"""Read-only Group 7 rollout evidence surface."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import func, select

from src.models.credit import CreditEnforcementState
from src.repositories.database import get_db_session
from tests.api.test_credit_operational_authorization import ADMIN, World

pytest_plugins = ["tests.api.test_credit_operational_authorization"]


async def _enforcement_rows() -> int:
    session = get_db_session()
    try:
        return int(
            await session.scalar(
                select(func.count()).select_from(CreditEnforcementState)
            )
            or 0
        )
    finally:
        await session.close()


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
    assert client._submit(_enforcement_rows()) == 0


def test_rollout_evidence_refuses_non_super_admin(client: Any, world: World) -> None:
    response = client.get(f"{ADMIN}/rollout/status", headers=world.bearer("officer_1"))
    assert response.status_code == 403
    assert client._submit(_enforcement_rows()) == 0
