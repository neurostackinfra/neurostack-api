"""Tests for builder subscription usage aggregation."""

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.models  # noqa: F401 - register every model with Base.metadata
from src.models.base import Base
from src.models.builder import Builder
from src.models.event import Event
from src.models.knowledge import KnowledgeDocument
from src.models.tenant import Tenant
from src.repositories.database import _patch_jsonb_for_sqlite
from src.services.billing_usage_service import (
    BYTES_PER_MB,
    PlanUsageLimits,
    get_builder_billing_usage,
)


@pytest.fixture
async def usage_db() -> AsyncGenerator[AsyncSession, None]:
    _patch_jsonb_for_sqlite()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session

    await engine.dispose()


def _tenant(builder_id: UUID) -> Tenant:
    return Tenant(
        id=uuid4(),
        name=f"Clinic {uuid4().hex[:6]}",
        slug=f"clinic-{uuid4().hex}",
        api_key_hash=f"hash-{uuid4().hex}",
        builder_id=builder_id,
        config={},
    )


def _event(tenant_id: UUID, event_type: str, created_at: datetime) -> Event:
    return Event(
        tenant_id=tenant_id,
        event_type=event_type,
        event_data={},
        event_metadata={},
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_aggregates_only_current_month_message_traces_and_rag_bytes(
    usage_db,
) -> None:
    builder_id = uuid4()
    other_builder_id = uuid4()
    usage_db.add_all(
        [
            Builder(id=builder_id, name="Builder A"),
            Builder(id=other_builder_id, name="Builder B"),
        ]
    )
    clinic_a = _tenant(builder_id)
    clinic_b = _tenant(builder_id)
    other_clinic = _tenant(other_builder_id)
    usage_db.add_all([clinic_a, clinic_b, other_clinic])

    month_start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    next_month = datetime(2026, 9, 1, tzinfo=timezone.utc)
    usage_db.add_all(
        [
            _event(clinic_a.id, "message.in", month_start),
            _event(
                clinic_b.id, "message.in", datetime(2026, 8, 20, tzinfo=timezone.utc)
            ),
            _event(
                clinic_a.id, "srm.decision", datetime(2026, 8, 20, tzinfo=timezone.utc)
            ),
            _event(
                clinic_a.id,
                "prompt.generated",
                datetime(2026, 8, 20, tzinfo=timezone.utc),
            ),
            _event(clinic_a.id, "message.in", next_month),
            _event(
                other_clinic.id,
                "message.in",
                datetime(2026, 8, 20, tzinfo=timezone.utc),
            ),
        ]
    )

    unicode_content = "Clínica São José"
    usage_db.add_all(
        [
            KnowledgeDocument(
                tenant_id=clinic_a.id,
                title="Unicode",
                content=unicode_content,
                document_metadata={},
            ),
            KnowledgeDocument(
                tenant_id=clinic_b.id,
                title="ASCII",
                content="abc",
                document_metadata={},
            ),
            KnowledgeDocument(
                tenant_id=other_clinic.id,
                title="Other builder",
                content="must not be counted",
                document_metadata={},
            ),
        ]
    )
    await usage_db.commit()

    usage = await get_builder_billing_usage(
        usage_db,
        builder_id,
        "start",
        PlanUsageLimits(
            monthly_traces=3,
            rag_storage_mb=1,
            media_storage_mb=2,
        ),
        now=datetime(2026, 8, 10, 12, tzinfo=timezone.utc),
    )

    assert usage["trace_period"] == {
        "start": month_start,
        "end": next_month,
        "timezone": "UTC",
    }
    assert usage["traces"] == {
        "used": 2,
        "included": 3,
        "extra_credits": 0,
        "available": 1,
        "overage": 0,
        "percent_used": 66.67,
    }

    expected_rag_bytes = len(unicode_content.encode("utf-8")) + 3
    assert usage["storage"]["rag"]["used_bytes"] == expected_rag_bytes
    assert usage["storage"]["rag"]["limit_bytes"] == BYTES_PER_MB
    assert usage["storage"]["rag"]["available_bytes"] == (
        BYTES_PER_MB - expected_rag_bytes
    )
    assert usage["storage"]["media"]["used_bytes"] == 0
    assert usage["storage"]["media"]["available_mb"] == 2.0
    assert usage["credits"] == {
        "purchase_enabled": False,
        "coming_soon": True,
    }


@pytest.mark.asyncio
async def test_clamps_available_and_reports_overage(usage_db) -> None:
    builder_id = uuid4()
    usage_db.add(Builder(id=builder_id, name="Over limit"))
    clinic = _tenant(builder_id)
    usage_db.add(clinic)
    now = datetime(2026, 8, 10, tzinfo=timezone.utc)
    usage_db.add_all(
        [
            _event(clinic.id, "message.in", now),
            _event(clinic.id, "message.in", now),
            KnowledgeDocument(
                tenant_id=clinic.id,
                title="Stored without entitlement",
                content="á",
                document_metadata={},
            ),
        ]
    )
    await usage_db.commit()

    usage = await get_builder_billing_usage(
        usage_db,
        builder_id,
        None,
        PlanUsageLimits(
            monthly_traces=1,
            rag_storage_mb=0,
            media_storage_mb=0,
        ),
        now=now,
    )

    assert usage["traces"]["available"] == 0
    assert usage["traces"]["overage"] == 1
    assert usage["traces"]["percent_used"] == 200.0
    assert usage["storage"]["rag"]["used_bytes"] == 2
    assert usage["storage"]["rag"]["available_bytes"] == 0
    assert usage["storage"]["rag"]["overage_bytes"] == 2
    assert usage["storage"]["rag"]["percent_used"] == 100.0
