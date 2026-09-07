"""Persistence contracts for KnowledgeDocument JSON metadata helpers."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import suppress
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.models as orm_models
from src.models.knowledge import KnowledgeDocument
from src.models.tenant import Tenant
from src.repositories.database import _patch_jsonb_for_sqlite
from tests.conftest import _keep_event_loop_responsive


@pytest.fixture
async def metadata_db() -> AsyncGenerator[AsyncSession, None]:
    _patch_jsonb_for_sqlite()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    heartbeat = asyncio.create_task(_keep_event_loop_responsive())
    try:
        async with engine.begin() as connection:
            await connection.run_sync(orm_models.Base.metadata.create_all)

        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as session:
            yield session
    finally:
        try:
            await engine.dispose()
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat


def _tenant() -> Tenant:
    return Tenant(
        id=uuid4(),
        name="Metadata Clinic",
        slug=f"metadata-{uuid4().hex}",
        api_key_hash=f"hash-{uuid4().hex}",
        config={},
    )


def _document(tenant_id, metadata=None) -> KnowledgeDocument:
    values = {
        "tenant_id": tenant_id,
        "title": "Metadata contract",
        "content": "Persistence evidence",
    }
    if metadata is not None:
        values["document_metadata"] = metadata
    return KnowledgeDocument(**values)


@pytest.mark.asyncio
async def test_metadata_mutators_persist_copy_on_write_after_reload(
    metadata_db,
) -> None:
    tenant = _tenant()
    add_document = _document(
        tenant.id,
        {"tags": ["existing"], "nested": {"preserved": True}},
    )
    remove_document = _document(
        tenant.id,
        {"tags": ["remove", "keep"], "source": "seed"},
    )
    category_document = _document(
        tenant.id,
        {"category": "old", "unrelated": 7},
    )
    metadata_db.add_all([tenant, add_document, remove_document, category_document])
    await metadata_db.commit()

    document_ids = [
        add_document.id,
        remove_document.id,
        category_document.id,
    ]
    metadata_db.expunge_all()
    loaded = [
        await metadata_db.get(KnowledgeDocument, document_id)
        for document_id in document_ids
    ]
    assert all(document is not None for document in loaded)
    loaded_add, loaded_remove, loaded_category = loaded
    assert loaded_add is not None
    assert loaded_remove is not None
    assert loaded_category is not None

    loaded_add.add_tag("new")
    assert loaded_remove.remove_tag("remove") is True
    loaded_category.set_category("new")
    assert all(
        document in metadata_db.dirty
        for document in (loaded_add, loaded_remove, loaded_category)
    )
    await metadata_db.commit()

    metadata_db.expunge_all()
    persisted_add = await metadata_db.get(KnowledgeDocument, add_document.id)
    persisted_remove = await metadata_db.get(KnowledgeDocument, remove_document.id)
    persisted_category = await metadata_db.get(KnowledgeDocument, category_document.id)
    assert persisted_add is not None
    assert persisted_remove is not None
    assert persisted_category is not None
    assert persisted_add.document_metadata == {
        "tags": ["existing", "new"],
        "nested": {"preserved": True},
    }
    assert persisted_remove.document_metadata == {
        "tags": ["keep"],
        "source": "seed",
    }
    assert persisted_category.document_metadata == {
        "category": "new",
        "unrelated": 7,
    }


@pytest.mark.asyncio
async def test_metadata_mutators_handle_preflush_default_and_malformed_values(
    metadata_db,
) -> None:
    tenant = _tenant()
    default_document = _document(tenant.id)
    assert default_document.document_metadata is None
    default_document.add_tag("first")
    default_document.set_category("fresh")

    malformed_document = _document(
        tenant.id,
        {"tags": "not-a-list", "category": 9, "preserved": "yes"},
    )
    metadata_db.add_all([tenant, default_document, malformed_document])
    await metadata_db.commit()

    metadata_db.expunge_all()
    loaded_default = await metadata_db.get(KnowledgeDocument, default_document.id)
    loaded_malformed = await metadata_db.get(KnowledgeDocument, malformed_document.id)
    assert loaded_default is not None
    assert loaded_malformed is not None
    assert loaded_default.document_metadata == {
        "tags": ["first"],
        "category": "fresh",
    }

    assert loaded_malformed.remove_tag("missing") is False
    loaded_malformed.add_tag("valid")
    loaded_malformed.set_category("repaired")
    assert loaded_malformed in metadata_db.dirty
    await metadata_db.commit()

    metadata_db.expunge_all()
    persisted = await metadata_db.get(KnowledgeDocument, malformed_document.id)
    assert persisted is not None
    assert persisted.document_metadata == {
        "tags": ["valid"],
        "category": "repaired",
        "preserved": "yes",
    }
