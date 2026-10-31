"""Deleted tenant effects close only through an append-only credit effect."""

from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from src.models.builder import Builder
from src.models.credit import CreditEffect
from src.models.tenant import Tenant
from src.repositories.credit import CreditRepository
from src.services.credit_rollout import CreditRolloutService

pytest_plugins = ("tests.integration.test_credit_orm_ddl_drift_postgres",)
pytestmark = pytest.mark.integration


async def test_deleted_tenant_effect_gets_an_idempotent_digest_closure(
    migrated: AsyncEngine,
) -> None:
    builder_id, tenant_id = uuid4(), uuid4()
    effect_id = f"trace:{tenant_id}:{uuid4()}:decision:2"
    digest = "a" * 64
    sessions = async_sessionmaker(migrated, expire_on_commit=False)
    async with sessions() as session:
        session.add(Builder(id=builder_id, name="orphan test builder"))
        session.add(
            Tenant(
                id=tenant_id,
                name="orphan test tenant",
                slug=f"orphan-{tenant_id.hex}",
                api_key_hash=f"hash-{tenant_id.hex}",
                builder_id=builder_id,
                config={},
            )
        )
        await session.flush()
        repository = CreditRepository(session)
        await repository.ensure_account(builder_id)
        assert await repository.insert_effect(
            builder_id=builder_id,
            tenant_id=tenant_id,
            effect_id=effect_id,
            effect_kind="trace_no_response",
            decision_digest=digest,
        )
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
        first = await CreditRolloutService(session).reconcile_orphans()
        second = await CreditRolloutService(session).reconcile_orphans()
        closures = list(
            (
                await session.execute(
                    select(CreditEffect).where(
                        CreditEffect.effect_kind == "deletion_closure"
                    )
                )
            ).scalars()
        )
        await session.commit()

    assert first.effect_without_decision == 0
    assert second.effect_without_decision == 0
    assert len(closures) == 1
    assert closures[0].decision_digest == digest
