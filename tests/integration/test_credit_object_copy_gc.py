"""PostgreSQL/S3 lifecycle proof for Group 4b source collection.

The storage copy is intentionally additive: this module creates a real source
row, lets the durable worker register an exact S3 version, deletes the source,
then proves the registry carries the outcome after all four deletion paths.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.config.settings import settings
from src.models.builder import Builder
from src.models.credit_object_copy import CreditObjectCopy, CreditObjectCopyJob
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset
from src.models.tenant import Tenant
from src.services import credit_inventory
from src.storage.credit_object_store import StoredCopy, set_legal_hold
from tests.integration.test_asaas_billing_postgres import _validated_admin_url
from tests.integration.test_migration_036_postgres import _Cluster, _migrate

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _Database:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]


def _required_storage_environment() -> tuple[str, str, str, str]:
    names = (
        "CREDIT_TEST_S3_ENDPOINT",
        "CREDIT_TEST_S3_BUCKET",
        "CREDIT_TEST_S3_ACCESS_KEY_ID",
        "CREDIT_TEST_S3_SECRET_ACCESS_KEY",
    )
    missing = [name for name in names if not os.getenv(name)]
    if missing:
        pytest.skip("S3 copy test configuration is absent: " + ", ".join(missing))
    return tuple(os.environ[name] for name in names)  # type: ignore[return-value]


@pytest.fixture(scope="module")
async def database() -> AsyncIterator[_Database]:
    endpoint, bucket, access_key, secret_key = _required_storage_environment()
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    cluster = _Cluster(admin_url=admin_url, admin=admin, created=[])
    engine: AsyncEngine | None = None
    try:
        name = await cluster.fresh()
        await _migrate(cluster.url(name), "upgrade", "head")
        engine = create_async_engine(cluster.url(name), poolclass=NullPool)
        yield _Database(
            engine=engine,
            sessions=async_sessionmaker(engine, expire_on_commit=False),
        )
    finally:
        if engine is not None:
            await engine.dispose()
        for name in list(reversed(cluster.created)):
            await cluster.drop(name)
        await admin.dispose()


@pytest.fixture(autouse=True)
def _enable_only_the_copy_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoint, bucket, access_key, secret_key = _required_storage_environment()
    monkeypatch.setattr(settings, "environment", "test")
    monkeypatch.setattr(settings, "credit_object_store_endpoint", endpoint)
    monkeypatch.setattr(settings, "credit_object_store_bucket", bucket)
    monkeypatch.setattr(settings, "credit_object_store_access_key_id", access_key)
    monkeypatch.setattr(
        settings, "credit_object_store_secret_access_key", SecretStr(secret_key)
    )
    monkeypatch.setattr(
        credit_inventory, "object_copy_runtime_enabled", lambda *_: True
    )
    # This file proves S3 lifecycle deterministically.  Qdrant's independent
    # real integration is tested in test_credit_qdrant_rebuild.py.
    monkeypatch.setattr(credit_inventory, "qdrant_runtime_enabled", lambda *_: False)


async def _seed_source(
    database: _Database, source_kind: str
) -> tuple[UUID, UUID, UUID, UUID]:
    builder_id, tenant_id, source_id = uuid4(), uuid4(), uuid4()
    async with database.sessions() as session:
        session.add(Builder(id=builder_id, name="Group 4b builder"))
        session.add(
            Tenant(
                id=tenant_id,
                name="Group 4b clinic",
                slug=f"g4b-{tenant_id.hex[:16]}",
                api_key_hash=f"g4b-{tenant_id.hex}",
                builder_id=builder_id,
                config={},
            )
        )
        if source_kind == "media":
            payload = b"group-4b-media-payload"
            session.add(
                MediaAsset(
                    id=source_id,
                    tenant_id=tenant_id,
                    display_name="Original display name",
                    original_filename="group4b.pdf",
                    category="pdf",
                    mime_type="application/pdf",
                    size_bytes=len(payload),
                    sha256=hashlib.sha256(payload).digest(),
                    payload=payload,
                    usage_trigger=None,
                    auto_suggest=False,
                )
            )
        else:
            session.add(
                KnowledgeDocument(
                    id=source_id,
                    tenant_id=tenant_id,
                    title="Group 4b document",
                    content="group 4b knowledge source",
                    content_type="text",
                    document_metadata={},
                    is_indexed=False,
                    chunk_count=0,
                )
            )
        await session.commit()
        await credit_inventory.enqueue_object_copy(
            session,
            source_kind=source_kind,  # type: ignore[arg-type]
            source_id=source_id,
            tenant_id=tenant_id,
        )
    assert await credit_inventory.run_credit_object_copy_once(database.sessions) == 1
    return builder_id, tenant_id, source_id, uuid4()


async def _copy(database: _Database, source_id: UUID) -> CreditObjectCopy:
    async with database.sessions() as session:
        copy = await session.scalar(
            select(CreditObjectCopy).where(CreditObjectCopy.source_id == source_id)
        )
        assert copy is not None and copy.state == "ready"
        return copy


@pytest.mark.parametrize(
    ("deletion_path", "source_kind"),
    [
        ("media", "media"),
        ("knowledge", "knowledge"),
        ("clinic_cascade", "media"),
        ("builder_cascade", "knowledge"),
    ],
)
async def test_each_source_delete_path_collects_the_registered_exact_version(
    database: _Database, deletion_path: str, source_kind: str
) -> None:
    builder_id, tenant_id, source_id, _ = await _seed_source(database, source_kind)
    copy = await _copy(database, source_id)

    async with database.sessions() as session:
        if deletion_path == "media":
            await session.execute(delete(MediaAsset).where(MediaAsset.id == source_id))
            await session.commit()
            await credit_inventory.enqueue_source_collection(
                session,
                source_kind="media",
                source_id=source_id,
                tenant_id=tenant_id,
            )
        elif deletion_path == "knowledge":
            await session.execute(
                delete(KnowledgeDocument).where(KnowledgeDocument.id == source_id)
            )
            await session.commit()
            await credit_inventory.enqueue_source_collection(
                session,
                source_kind="knowledge",
                source_id=source_id,
                tenant_id=tenant_id,
            )
        elif deletion_path == "clinic_cascade":
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await session.commit()
        else:
            # This mirrors the destructive part of the admin builder flow:
            # its own tenant is removed, then the builder record is removed.
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await session.execute(delete(Builder).where(Builder.id == builder_id))
            await session.commit()

    assert await credit_inventory.run_credit_object_copy_once(database.sessions) == 1
    async with database.sessions() as session:
        state = await session.scalar(
            select(CreditObjectCopy.state).where(CreditObjectCopy.id == copy.id)
        )
        gc_state = await session.scalar(
            select(CreditObjectCopyJob.state)
            .where(
                CreditObjectCopyJob.source_id == source_id,
                CreditObjectCopyJob.operation == "gc",
            )
            .order_by(CreditObjectCopyJob.created_at.desc())
        )
    assert state == "deleted"
    assert gc_state == "completed"


async def test_media_metadata_patch_does_not_enqueue_or_change_a_copy(
    database: _Database,
) -> None:
    _builder_id, _tenant_id, source_id, _ = await _seed_source(database, "media")
    before = await _copy(database, source_id)
    async with database.sessions() as session:
        asset = await session.get(MediaAsset, source_id)
        assert asset is not None
        asset.display_name = "Renamed only"
        await session.commit()

    assert await credit_inventory.run_credit_object_copy_once(database.sessions) == 0
    after = await _copy(database, source_id)
    assert (after.object_key, after.version_id, after.content_sha256) == (
        before.object_key,
        before.version_id,
        before.content_sha256,
    )


async def test_legal_hold_records_a_held_gc_outcome(
    database: _Database,
) -> None:
    _builder_id, tenant_id, source_id, _ = await _seed_source(database, "media")
    copy = await _copy(database, source_id)
    stored = StoredCopy(
        copy.object_key, copy.version_id, copy.etag, copy.content_sha256
    )
    set_legal_hold(copy=stored, enabled=True)
    try:
        async with database.sessions() as session:
            await session.execute(delete(MediaAsset).where(MediaAsset.id == source_id))
            await session.commit()
            await credit_inventory.enqueue_source_collection(
                session,
                source_kind="media",
                source_id=source_id,
                tenant_id=tenant_id,
            )
        assert (
            await credit_inventory.run_credit_object_copy_once(database.sessions) == 1
        )
        async with database.sessions() as session:
            state = await session.scalar(
                select(CreditObjectCopy.state).where(CreditObjectCopy.id == copy.id)
            )
            job_state = await session.scalar(
                select(CreditObjectCopyJob.state)
                .where(
                    CreditObjectCopyJob.source_id == source_id,
                    CreditObjectCopyJob.operation == "gc",
                )
                .order_by(CreditObjectCopyJob.created_at.desc())
            )
        assert (state, job_state) == ("held", "held")
    finally:
        set_legal_hold(copy=stored, enabled=False)
