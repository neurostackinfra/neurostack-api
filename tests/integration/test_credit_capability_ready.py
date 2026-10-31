"""Group 7 capability readiness on a migrated PostgreSQL schema."""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from src.models.credit import CreditCapabilityReadiness
from src.services.credit_rollout import CreditRolloutService

pytest_plugins = ("tests.integration.test_credit_orm_ddl_drift_postgres",)
pytestmark = pytest.mark.integration


async def test_code_readiness_records_only_calculated_fields(
    migrated: AsyncEngine,
) -> None:
    sessions = async_sessionmaker(migrated, expire_on_commit=False)
    async with sessions() as session:
        service = CreditRolloutService(session)
        readiness = await service.capability_ready()
        row = await service.record_capability_readiness()
        await session.commit()
        stored = await session.scalar(
            select(CreditCapabilityReadiness).where(
                CreditCapabilityReadiness.id == row.id
            )
        )

    assert readiness.ready is True
    assert stored is not None
    assert stored.environment == readiness.environment
    assert stored.billing_fact_schema_digest == readiness.billing_fact_schema_digest
    assert stored.inbox_high_watermark == 0
    assert stored.operation_id is None
    assert stored.deployment_digest is None
    assert stored.credit_deployment_digest is None
    assert stored.producer_compatibility_digest is None
    assert stored.consumer_compatibility_digest is None
