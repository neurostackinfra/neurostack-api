"""Tenant-scoped lookups of ``KnowledgeDocumentRepository`` under real commits.

The HTTP routes resolve one authorized tenant and then reach documents only
through these scoped methods.  Each test commits, expunges and reloads, so it
proves the scoping itself and not an uncommitted unit of work hides a write.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import suppress
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.models as orm_models
from src.models.knowledge import KnowledgeDocument
from src.models.tenant import Tenant
from src.repositories import KnowledgeDocumentRepository
from src.repositories.database import _patch_jsonb_for_sqlite
from tests.conftest import _keep_event_loop_responsive


@pytest.fixture
async def knowledge_db() -> AsyncGenerator[AsyncSession, None]:
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


async def _seed(session: AsyncSession) -> tuple[UUID, UUID, UUID]:
    """Commit two tenants and one document owned by the first."""
    owner, other = uuid4(), uuid4()
    for tenant_id in (owner, other):
        session.add(
            Tenant(
                id=tenant_id,
                name=f"Clinic {tenant_id}",
                slug=f"clinic-{tenant_id.hex}",
                api_key_hash=f"hash-{tenant_id.hex}",
                config={},
            )
        )
    document = KnowledgeDocument(
        tenant_id=owner, title="Owner protocol", content="Owner-only content"
    )
    session.add(document)
    await session.commit()
    session.expunge_all()
    return owner, other, document.id


@pytest.mark.asyncio
async def test_get_by_id_and_tenant_returns_only_the_owning_tenants_document(
    knowledge_db: AsyncSession,
) -> None:
    owner, other, document_id = await _seed(knowledge_db)
    repo = KnowledgeDocumentRepository(knowledge_db)

    owned = await repo.get_by_id_and_tenant(document_id, owner)
    foreign = await repo.get_by_id_and_tenant(document_id, other)
    missing = await repo.get_by_id_and_tenant(uuid4(), owner)

    assert owned is not None
    assert (owned.id, owned.tenant_id) == (document_id, owner)
    assert foreign is None
    assert missing is None


@pytest.mark.asyncio
async def test_delete_by_id_and_tenant_never_deletes_another_tenants_document(
    knowledge_db: AsyncSession,
) -> None:
    owner, other, document_id = await _seed(knowledge_db)
    repo = KnowledgeDocumentRepository(knowledge_db)

    assert await repo.delete_by_id_and_tenant(document_id, other) is False
    assert await repo.delete_by_id_and_tenant(uuid4(), owner) is False
    await knowledge_db.commit()
    knowledge_db.expunge_all()

    survivor = await knowledge_db.get(KnowledgeDocument, document_id)
    assert survivor is not None
    assert survivor.tenant_id == owner


@pytest.mark.asyncio
async def test_delete_by_id_and_tenant_deletes_the_owned_document(
    knowledge_db: AsyncSession,
) -> None:
    owner, _other, document_id = await _seed(knowledge_db)
    repo = KnowledgeDocumentRepository(knowledge_db)

    assert await repo.delete_by_id_and_tenant(document_id, owner) is True
    await knowledge_db.commit()
    knowledge_db.expunge_all()

    assert await knowledge_db.get(KnowledgeDocument, document_id) is None
    assert await repo.delete_by_id_and_tenant(document_id, owner) is False


@pytest.mark.asyncio
async def test_unscoped_lookups_keep_their_existing_behaviour(
    knowledge_db: AsyncSession,
) -> None:
    """The scoped methods are additions; the inherited ones are not loosened."""
    owner, _other, document_id = await _seed(knowledge_db)
    repo = KnowledgeDocumentRepository(knowledge_db)

    found = await repo.get_by_id(document_id)

    assert found is not None
    assert found.tenant_id == owner
    assert [d.id for d in await repo.find_by_tenant(owner)] == [document_id]
    assert await repo.count_by_tenant(owner) == 1


@pytest.mark.asyncio
async def test_count_by_category_counts_only_the_tenants_category(
    knowledge_db: AsyncSession,
) -> None:
    owner, other, _document_id = await _seed(knowledge_db)
    for tenant_id, category in (
        (owner, "protocolo"),
        (owner, "protocolo"),
        (owner, "faq"),
        (other, "protocolo"),
    ):
        knowledge_db.add(
            KnowledgeDocument(
                tenant_id=tenant_id,
                title=f"{category} document",
                content="Categorised content",
                document_metadata={"category": category},
            )
        )
    await knowledge_db.commit()
    repo = KnowledgeDocumentRepository(knowledge_db)

    assert await repo.count_by_category(owner, "protocolo") == 2
    assert await repo.count_by_category(owner, "faq") == 1
    assert await repo.count_by_category(other, "protocolo") == 1
    assert await repo.count_by_category(owner, "missing") == 0
    assert await repo.count_by_category(owner, "protocolo", indexed_only=True) == 0
    assert len(await repo.find_by_category(owner, "protocolo")) == 2


@pytest.mark.asyncio
async def test_delete_by_id_and_tenant_rechecks_a_row_gone_after_the_lookup(
    knowledge_db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent delete that won the race is reported, not repeated."""
    owner, _other, document_id = await _seed(knowledge_db)
    repo = KnowledgeDocumentRepository(knowledge_db)
    stale = await repo.get_by_id_and_tenant(document_id, owner)
    assert stale is not None
    # Another transaction deleted the row after this request's lookup.
    await knowledge_db.execute(
        delete(KnowledgeDocument)
        .where(KnowledgeDocument.id == document_id)
        .execution_options(synchronize_session=False)
    )

    async def stale_lookup(
        _self: KnowledgeDocumentRepository, _document_id: UUID, _tenant_id: UUID
    ) -> KnowledgeDocument:
        return stale

    monkeypatch.setattr(
        KnowledgeDocumentRepository, "get_by_id_and_tenant", stale_lookup
    )

    assert await repo.delete_by_id_and_tenant(document_id, owner) is False
    assert stale not in knowledge_db.deleted
