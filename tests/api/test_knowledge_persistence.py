"""Knowledge writes persist through the shipped request session.

Unlike ``test_knowledge_tenant_authorization.py`` this module keeps the real
``get_db_session_dependency``, which only closes the session: a route that
does not commit loses its write here.  Effects are read back through a fresh
session, never through the routes under test.  PostgreSQL-only behaviour
(foreign keys, row locks, concurrent deletes) is pinned by
``tests/integration/test_knowledge_persistence_postgres.py``.
"""

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.performance import response_cache
from src.api.routes import knowledge as knowledge_route
from src.config.settings import settings
from src.models.builder import Builder
from src.models.knowledge import KnowledgeDocument
from src.models.tenant import Tenant
from src.repositories import KnowledgeDocumentRepository, get_db_session_dependency
from src.repositories.database import get_db_session
from tests.conftest import LifespanTestClient

DOCUMENTS = "/v1/knowledge/documents"
TENANT_NOT_FOUND = {"detail": "Tenant not found"}


@pytest.fixture(autouse=True)
def _real_session_dependency(test_app: Any) -> Iterator[None]:
    """Fail loudly if another module left a session override installed."""
    assert get_db_session_dependency not in test_app.dependency_overrides
    response_cache.invalidate()
    yield
    response_cache.invalidate()
    assert get_db_session_dependency not in test_app.dependency_overrides


def _jwt(role: str, tenant_id: UUID, *, builder_id: UUID | None = None) -> dict:
    payload: dict[str, Any] = {
        "sub": str(uuid4()),
        "tenant_id": str(tenant_id),
        "role": role,
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    if builder_id is not None:
        payload["builder_id"] = str(builder_id)
    token = jwt.encode(payload, settings.secret_key, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _clinic_admin(tenant_id: UUID) -> dict:
    return _jwt("clinic_admin", tenant_id)


def _seed_tenants(
    client: LifespanTestClient, *tenant_ids: UUID, builder_id: UUID | None = None
) -> None:
    async def _run() -> None:
        db = get_db_session()
        try:
            if builder_id is not None:
                db.add(Builder(id=builder_id, name=f"Builder {builder_id}"))
            for tenant_id in tenant_ids:
                db.add(
                    Tenant(
                        id=tenant_id,
                        name=f"Clinic {tenant_id}",
                        slug=f"clinic-{tenant_id}",
                        api_key_hash=f"hash-{tenant_id}",
                        builder_id=builder_id,
                        config={},
                    )
                )
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())


def _stored(client: LifespanTestClient, document_id: str) -> tuple[UUID, str] | None:
    async def _run() -> tuple[UUID, str] | None:
        db = get_db_session()
        try:
            row = (
                await db.execute(
                    select(KnowledgeDocument.tenant_id, KnowledgeDocument.title).where(
                        KnowledgeDocument.id == UUID(document_id)
                    )
                )
            ).one_or_none()
            return None if row is None else (row[0], row[1])
        finally:
            await db.close()

    result: tuple[UUID, str] | None = client._submit(_run())
    return result


def _count(client: LifespanTestClient, tenant_id: UUID) -> int:
    async def _run() -> int:
        db = get_db_session()
        try:
            value = await db.scalar(
                select(func.count())
                .select_from(KnowledgeDocument)
                .where(KnowledgeDocument.tenant_id == tenant_id)
            )
            return int(value or 0)
        finally:
            await db.close()

    count: int = client._submit(_run())
    return count


def _upload(
    client: LifespanTestClient,
    headers: dict,
    *,
    title: str = "Protocolo",
    category: str | None = "protocolo",
    tenant_id: UUID | None = None,
) -> Any:
    body: dict[str, Any] = {
        "content": f"Conteudo de {title}",
        "metadata": {"title": title, "category": category},
    }
    if tenant_id is not None:
        body["tenant_id"] = str(tenant_id)
    return client.post(DOCUMENTS, json=body, headers=headers)


def _not_found(document_id: str) -> dict[str, str]:
    return {"detail": f"Document {document_id} not found"}


class _CommitSpy:
    """Order of authorization, repository writes and commits per request."""

    def __init__(self) -> None:
        self.events: list[str] = []


@pytest.fixture
def commit_spy(monkeypatch: pytest.MonkeyPatch) -> _CommitSpy:
    spy = _CommitSpy()
    original_resolve = knowledge_route.resolve_effective_tenant
    original_create = KnowledgeDocumentRepository.create
    original_delete = KnowledgeDocumentRepository.delete_by_id_and_tenant
    original_commit = AsyncSession.commit

    async def resolve(*args: Any, **kwargs: Any) -> UUID:
        spy.events.append("resolve")
        tenant_id: UUID = await original_resolve(*args, **kwargs)
        return tenant_id

    async def create(
        self: KnowledgeDocumentRepository, **kwargs: object
    ) -> KnowledgeDocument:
        spy.events.append("create")
        return await original_create(self, **kwargs)

    async def delete(
        self: KnowledgeDocumentRepository, document_id: UUID, tenant_id: UUID
    ) -> bool:
        spy.events.append("delete")
        return await original_delete(self, document_id, tenant_id)

    async def commit(self: AsyncSession) -> None:
        spy.events.append("commit")
        await original_commit(self)

    monkeypatch.setattr(knowledge_route, "resolve_effective_tenant", resolve)
    monkeypatch.setattr(KnowledgeDocumentRepository, "create", create)
    monkeypatch.setattr(KnowledgeDocumentRepository, "delete_by_id_and_tenant", delete)
    monkeypatch.setattr(AsyncSession, "commit", commit)
    return spy


# ============================================
# Persistence through the real session
# ============================================


def test_created_document_persists_and_double_delete_is_204_then_404(
    client: LifespanTestClient,
) -> None:
    owner, other = uuid4(), uuid4()
    _seed_tenants(client, owner, other)

    created = _upload(client, _clinic_admin(owner), title="Persistente")
    assert created.status_code == 201, created.text
    document_id = created.json()["id"]
    assert created.json()["tenant_id"] == str(owner)
    assert _stored(client, document_id) == (owner, "Persistente")

    read = client.get(f"{DOCUMENTS}/{document_id}", headers=_clinic_admin(owner))
    assert (read.status_code, read.json()["id"]) == (200, document_id)
    foreign = client.get(f"{DOCUMENTS}/{document_id}", headers=_clinic_admin(other))
    assert (foreign.status_code, foreign.json()) == (404, _not_found(document_id))
    foreign_delete = client.delete(
        f"{DOCUMENTS}/{document_id}", headers=_clinic_admin(other)
    )
    assert foreign_delete.status_code == 404
    assert _stored(client, document_id) == (owner, "Persistente")

    first = client.delete(f"{DOCUMENTS}/{document_id}", headers=_clinic_admin(owner))
    assert (first.status_code, first.content) == (204, b"")
    assert _stored(client, document_id) is None
    second = client.delete(f"{DOCUMENTS}/{document_id}", headers=_clinic_admin(owner))
    assert (second.status_code, second.json()) == (404, _not_found(document_id))
    gone = client.get(f"{DOCUMENTS}/{document_id}", headers=_clinic_admin(owner))
    assert gone.status_code == 404


def test_create_failing_after_the_insert_leaves_nothing_behind(
    client: LifespanTestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = uuid4()
    _seed_tenants(client, owner)
    flushed: list[UUID] = []
    original_create = KnowledgeDocumentRepository.create

    async def create(
        self: KnowledgeDocumentRepository, **kwargs: object
    ) -> KnowledgeDocument:
        document = await original_create(self, **kwargs)
        flushed.append(document.id)
        return document

    def broken_response(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("response assembly failed after the insert")

    monkeypatch.setattr(KnowledgeDocumentRepository, "create", create)
    monkeypatch.setattr(knowledge_route, "DocumentResponse", broken_response)

    with pytest.raises(RuntimeError, match="after the insert"):
        _upload(client, _clinic_admin(owner))

    assert len(flushed) == 1
    assert _stored(client, str(flushed[0])) is None
    assert _count(client, owner) == 0


def test_commits_follow_authorization_and_the_write_and_only_on_success(
    client: LifespanTestClient, commit_spy: _CommitSpy
) -> None:
    owner, other = uuid4(), uuid4()
    _seed_tenants(client, owner, other)
    commit_spy.events.clear()  # the seeding commit is not a request's

    created = _upload(client, _clinic_admin(owner))
    assert created.status_code == 201, created.text
    assert commit_spy.events == ["resolve", "create", "commit"]
    document_id = created.json()["id"]

    commit_spy.events.clear()
    denied = _upload(client, _clinic_admin(owner), tenant_id=other)
    assert denied.status_code == 403
    assert commit_spy.events == ["resolve"]

    commit_spy.events.clear()
    missing_tenant = _upload(client, _clinic_admin(uuid4()))
    assert (missing_tenant.status_code, missing_tenant.json()) == (
        404,
        TENANT_NOT_FOUND,
    )
    assert commit_spy.events == ["resolve"]

    commit_spy.events.clear()
    foreign = client.delete(f"{DOCUMENTS}/{document_id}", headers=_clinic_admin(other))
    assert foreign.status_code == 404
    assert commit_spy.events == ["resolve", "delete"]

    commit_spy.events.clear()
    for path in (DOCUMENTS, f"{DOCUMENTS}/{document_id}"):
        assert client.get(path, headers=_clinic_admin(owner)).status_code == 200
    assert commit_spy.events == ["resolve", "resolve"], "reads never commit"

    commit_spy.events.clear()
    deleted = client.delete(f"{DOCUMENTS}/{document_id}", headers=_clinic_admin(owner))
    assert deleted.status_code == 204
    assert commit_spy.events == ["resolve", "delete", "commit"]


# ============================================
# Tenant without a row
# ============================================


def test_upload_into_a_tenant_without_a_row_is_404_for_every_caller(
    client: LifespanTestClient,
) -> None:
    missing, builder_id, builder_tenant = uuid4(), uuid4(), uuid4()
    _seed_tenants(client, builder_tenant, builder_id=builder_id)

    as_super_admin = _upload(client, _jwt("super_admin", uuid4()), tenant_id=missing)
    as_own_tenant = _upload(client, _clinic_admin(missing))
    as_builder = _upload(
        client,
        _jwt("builder", builder_tenant, builder_id=builder_id),
        tenant_id=missing,
    )

    for response in (as_super_admin, as_own_tenant):
        assert (response.status_code, response.json()) == (404, TENANT_NOT_FOUND)
    # The builder's ownership check answers first, so existence never leaks.
    assert as_builder.status_code == 403
    assert as_builder.json() == {"detail": {"code": "tenant_override_forbidden"}}
    assert _count(client, missing) == 0

    # Reads of a tenant without a row stay truthful empties.
    listed = client.get(DOCUMENTS, headers=_clinic_admin(missing))
    assert (listed.status_code, listed.json()["total"]) == (200, 0)


# ============================================
# List totals
# ============================================


def test_list_total_counts_the_filtered_set_of_the_authorized_tenant(
    client: LifespanTestClient,
) -> None:
    owner, other = uuid4(), uuid4()
    _seed_tenants(client, owner, other)
    for index in range(3):
        assert _upload(client, _clinic_admin(owner), title=f"P{index}").status_code
    for index in range(2):
        _upload(client, _clinic_admin(owner), title=f"F{index}", category="faq")
    for index in range(4):
        _upload(client, _clinic_admin(other), title=f"O{index}")

    def page(**params: Any) -> tuple[int, int]:
        response = client.get(
            DOCUMENTS,
            headers=_clinic_admin(owner),
            params={k: str(v) for k, v in params.items()},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert all(d["tenant_id"] == str(owner) for d in body["documents"])
        return len(body["documents"]), body["total"]

    assert page(category="protocolo", page=1, page_size=2) == (2, 3)
    assert page(category="protocolo", page=2, page_size=2) == (1, 3)
    assert page(category="protocolo", page=3, page_size=2) == (0, 3)
    assert page(category="faq") == (2, 2)
    assert page(category="inexistente") == (0, 0)
    assert page() == (5, 5)
    # An empty category does not filter, for the page and its total alike.
    assert page(category="") == (5, 5)
    assert page(category="", page=2, page_size=3) == (2, 5)
