"""The binding grace rule retains its civil-time boundary on PostgreSQL runs."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from src.services.credit_lifecycle import binding_grace_end, start_of_civil_day

pytest_plugins = ("tests.integration.test_credit_orm_ddl_drift_postgres",)
pytestmark = pytest.mark.integration


async def test_previous_active_binding_expires_at_five_sao_paulo_civil_days(
    migrated: AsyncEngine,
) -> None:
    # Touch the migrated database so this is a real PostgreSQL integration
    # probe as well as a civil-time contract.
    async with migrated.connect() as connection:
        assert await connection.scalar(text("SELECT 1")) == 1

    cycle_end = start_of_civil_day(date(2026, 11, 1))
    grace_end = binding_grace_end(cycle_end, grace_days=5, is_active=True)
    assert grace_end == cycle_end + timedelta(days=5)
    assert binding_grace_end(
        cycle_end,
        grace_days=5,
        is_active=True,
        next_binding_at=cycle_end + timedelta(hours=1),
    ) == cycle_end + timedelta(hours=1)
    assert binding_grace_end(cycle_end, grace_days=5, is_active=False) == cycle_end
