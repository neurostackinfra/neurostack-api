"""Real PostgreSQL evidence that knowledge writes persist and stay scoped.

Every request goes through the real application (``src.api.main.app``) with
its real lifespan, middleware and **request session dependency**: nothing
overrides ``get_db_session_dependency``.  The lifespan publishes a pool on a
disposable database migrated to head, so a route that forgot to commit loses
its write exactly as it would in production, and every effect is read back
through a separate connection.

What these tests pin (measured, not assumed):

* A created document survives the request and is readable, listable and
  deletable only by its own tenant; another tenant gets the same 404 as for a
  missing document and cannot delete it.
* Deleting twice answers 204 then 404, and the document is really gone.
* Two simultaneous deletes of one document, both past the lookup, answer
  exactly one 204 and one 404.
* A request that fails after its write was flushed leaves nothing behind.
* An upload into a tenant without a row is a clean 404 ``Tenant not found``
  for every caller allowed to name that tenant (also when the row disappears
  between the check and the insert), while a builder still gets the 403 of the
  ownership check; any other integrity error is not disguised as a 404.
* The list ``total`` counts the filtered set of the authorized tenant only.
* A clone copies the documents created through the API only from the
  builder's own clinic.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from fastapi import HTTPException
from sqlalchemy import delete, func, select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import src.repositories as repositories
from src.api.main import app
from src.api.middleware.performance import response_cache
from src.api.routes import knowledge as knowledge_route
from src.config.settings import settings
from src.models.builder import Builder
from src.models.knowledge import KnowledgeDocument
from src.models.tenant import Tenant
from src.repositories import KnowledgeDocumentRepository
from src.repositories.database import get_db_session_dependency
from tests.conftest import _production_init_database
from tests.integration.test_asaas_billing_postgres import (
    _create_child,
    _drop_child,
    _run_alembic,
    _validated_admin_url,
)
from tests.integration.test_builder_cloning_postgres import (
    _clone,
    _seed_graph,
    _stub_fast_unique_hash,
)

pytestmark = pytest.mark.integration

_DOCUMENTS = "/v1/knowledge/documents"
_TENANT_NOT_FOUND = {"detail": "Tenant not found"}
_TENANT_OVERRIDE_FORBIDDEN = {"detail": {"code": "tenant_override_forbidden"}}
_TASK_TIMEOUT_SECONDS = 15
_TENANT_FK = "knowledge_documents_tenant_id_fkey"
# A NOT NULL violation (SQLSTATE 23502) is an IntegrityError, but not the FK.
_TITLE_NOT_NULL = 'null value in column "title"'


# --------------------------------------------------------------------------
# Disposable database: one migrated template per module, one clone per test.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Template:
    admin_engine: AsyncEngine
    admin_url: URL
    name: str


@pytest.fixture(scope="module")
async def template_database() -> AsyncIterator[_Template]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin_engine = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    name = f"neurostack_asaas_smoke_{uuid4().hex[:12]}"
    created = False
    try:
        await _create_child(admin_engine, name)
        created = True
        await _run_alembic(admin_url.set(database=name), "upgrade", "head")
        yield _Template(admin_engine=admin_engine, admin_url=admin_url, name=name)
    finally:
        if created:
            await _drop_child(admin_engine, name)
        await admin_engine.dispose()


@dataclass(frozen=True)
class _Knowledge:
    client: httpx.AsyncClient
    sessions: async_sessionmaker[AsyncSession]
    engine: AsyncEngine


@pytest.fixture
async def knowledge(
    template_database: _Template,
    alembic_head: str,
    monkeypatch: pytest.MonkeyPatch,
    _inject_isolated_database: None,
) -> AsyncIterator[_Knowledge]:
    """Run the real app lifespan on a fresh migrated database."""
    name = f"neurostack_asaas_smoke_{uuid4().hex[:12]}"
    async with template_database.admin_engine.connect() as connection:
        quote = connection.dialect.identifier_preparer.quote
        await connection.exec_driver_sql(
            f"CREATE DATABASE {quote(name)} "
            f"TEMPLATE {quote(template_database.name)}"
        )
    database_url = template_database.admin_url.set(database=name)
    observer_engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with observer_engine.connect() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
            ) == alembic_head
        # The production lifecycle and session factory, on this database only.
        monkeypatch.setattr(
            repositories,
            "init_database",
            functools.partial(
                _production_init_database,
                database_url=database_url.render_as_string(hide_password=False),
                engine_factory=create_async_engine,
            ),
        )
        assert get_db_session_dependency not in app.dependency_overrides
        response_cache.invalidate()
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(
                app=app, raise_app_exceptions=False, client=("testclient", 50000)
            )
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                yield _Knowledge(
                    client=client,
                    sessions=async_sessionmaker(
                        observer_engine,
                        class_=AsyncSession,
                        expire_on_commit=False,
                        autoflush=False,
                    ),
                    engine=observer_engine,
                )
        assert get_db_session_dependency not in app.dependency_overrides
    finally:
        response_cache.invalidate()
        await observer_engine.dispose()
        await _drop_child(template_database.admin_engine, name)


# --------------------------------------------------------------------------
# Callers, seeding and direct reads (never through the routes under test).
# --------------------------------------------------------------------------


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


def _super_admin() -> dict:
    return _jwt("super_admin", uuid4())


async def _seed_tenants(
    knowledge: _Knowledge, *tenant_ids: UUID, builder_id: UUID | None = None
) -> None:
    async with knowledge.sessions() as session:
        if builder_id is not None:
            session.add(Builder(id=builder_id, name=f"Builder {builder_id.hex[:8]}"))
            await session.flush()
        for tenant_id in tenant_ids:
            session.add(
                Tenant(
                    id=tenant_id,
                    name=f"Clinic {tenant_id.hex[:8]}",
                    slug=f"knowledge-{tenant_id.hex}",
                    api_key_hash=f"hash-{tenant_id.hex}",
                    builder_id=builder_id,
                    config={},
                )
            )
        await session.commit()


async def _stored(knowledge: _Knowledge, document_id: str) -> tuple[UUID, str] | None:
    async with knowledge.sessions() as session:
        row = (
            await session.execute(
                select(KnowledgeDocument.tenant_id, KnowledgeDocument.title).where(
                    KnowledgeDocument.id == UUID(document_id)
                )
            )
        ).one_or_none()
    return None if row is None else (row[0], row[1])


async def _document_count(knowledge: _Knowledge) -> int:
    async with knowledge.sessions() as session:
        count = await session.scalar(
            select(func.count()).select_from(KnowledgeDocument)
        )
    return int(count or 0)


async def _upload(
    knowledge: _Knowledge,
    headers: dict,
    *,
    title: str = "Protocolo",
    category: str | None = "protocolo",
    tenant_id: UUID | None = None,
) -> httpx.Response:
    body: dict[str, Any] = {
        "content": f"Conteudo de {title}",
        "metadata": {"title": title, "category": category, "tags": ["t"]},
    }
    if tenant_id is not None:
        body["tenant_id"] = str(tenant_id)
    return await knowledge.client.post(_DOCUMENTS, json=body, headers=headers)


async def _get(
    knowledge: _Knowledge,
    headers: dict,
    document_id: str,
    tenant_id: UUID | None = None,
) -> httpx.Response:
    params = {} if tenant_id is None else {"tenant_id": str(tenant_id)}
    return await knowledge.client.get(
        f"{_DOCUMENTS}/{document_id}", headers=headers, params=params
    )


async def _delete(
    knowledge: _Knowledge,
    headers: dict,
    document_id: str,
    tenant_id: UUID | None = None,
) -> httpx.Response:
    params = {} if tenant_id is None else {"tenant_id": str(tenant_id)}
    return await knowledge.client.delete(
        f"{_DOCUMENTS}/{document_id}", headers=headers, params=params
    )


async def _list(knowledge: _Knowledge, headers: dict, **params: Any) -> httpx.Response:
    return await knowledge.client.get(
        _DOCUMENTS,
        headers=headers,
        params={k: str(v) for k, v in params.items() if v is not None},
    )


def _not_found(document_id: str) -> dict[str, str]:
    return {"detail": f"Document {document_id} not found"}


# --------------------------------------------------------------------------
# Persistence lifecycle
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_created_document_persists_and_is_scoped_until_deleted(
    knowledge: _Knowledge,
) -> None:
    owner, other = uuid4(), uuid4()
    await _seed_tenants(knowledge, owner, other)

    created = await _upload(knowledge, _clinic_admin(owner), title="Persistente")
    assert created.status_code == 201, created.text
    body = created.json()
    document_id = body["id"]
    assert body["tenant_id"] == str(owner)
    assert body["status"] == "pending"
    assert body["metadata"]["title"] == "Persistente"
    # Read back through another connection: the write outlived the request.
    assert await _stored(knowledge, document_id) == (owner, "Persistente")

    read = await _get(knowledge, _clinic_admin(owner), document_id)
    assert read.status_code == 200, read.text
    assert read.json()["id"] == document_id
    listed = await _list(knowledge, _clinic_admin(owner))
    assert [d["id"] for d in listed.json()["documents"]] == [document_id]
    assert listed.json()["total"] == 1

    # Another tenant: invisible, undeletable, indistinguishable from missing.
    foreign_get = await _get(knowledge, _clinic_admin(other), document_id)
    foreign_delete = await _delete(knowledge, _clinic_admin(other), document_id)
    foreign_list = await _list(knowledge, _clinic_admin(other))
    assert (foreign_get.status_code, foreign_get.json()) == (
        404,
        _not_found(document_id),
    )
    assert (foreign_delete.status_code, foreign_delete.json()) == (
        404,
        _not_found(document_id),
    )
    assert foreign_list.json()["documents"] == []
    assert foreign_list.json()["total"] == 0
    assert await _stored(knowledge, document_id) == (owner, "Persistente")

    first = await _delete(knowledge, _clinic_admin(owner), document_id)
    assert (first.status_code, first.content) == (204, b"")
    assert await _stored(knowledge, document_id) is None
    gone = await _get(knowledge, _clinic_admin(owner), document_id)
    assert (gone.status_code, gone.json()) == (404, _not_found(document_id))
    second = await _delete(knowledge, _clinic_admin(owner), document_id)
    assert (second.status_code, second.json()) == (404, _not_found(document_id))
    assert await _document_count(knowledge) == 0


@pytest.mark.asyncio
async def test_create_failing_after_its_flush_leaves_nothing_behind(
    knowledge: _Knowledge, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = uuid4()
    await _seed_tenants(knowledge, owner)
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

    response = await _upload(knowledge, _clinic_admin(owner))

    assert response.status_code == 500, response.text
    assert len(flushed) == 1, "the failure must happen after the INSERT"
    assert await _stored(knowledge, str(flushed[0])) is None
    assert await _document_count(knowledge) == 0


@pytest.mark.asyncio
async def test_delete_failing_after_its_flush_keeps_the_document(
    knowledge: _Knowledge, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = uuid4()
    await _seed_tenants(knowledge, owner)
    created = await _upload(knowledge, _clinic_admin(owner), title="Sobrevive")
    document_id = created.json()["id"]
    original_delete = KnowledgeDocumentRepository.delete_by_id_and_tenant
    deleted: list[bool] = []

    async def delete_then_fail(
        self: KnowledgeDocumentRepository, document_id: UUID, tenant_id: UUID
    ) -> bool:
        deleted.append(await original_delete(self, document_id, tenant_id))
        raise RuntimeError("failure after the DELETE was flushed")

    monkeypatch.setattr(
        KnowledgeDocumentRepository, "delete_by_id_and_tenant", delete_then_fail
    )

    response = await _delete(knowledge, _clinic_admin(owner), document_id)

    assert response.status_code == 500, response.text
    assert deleted == [True], "the failure must happen after the DELETE"
    assert await _stored(knowledge, document_id) == (owner, "Sobrevive")


@pytest.mark.asyncio
async def test_two_simultaneous_deletes_answer_one_204_and_one_404(
    knowledge: _Knowledge, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = uuid4()
    await _seed_tenants(knowledge, owner)
    created = await _upload(knowledge, _clinic_admin(owner), title="Disputado")
    document_id = created.json()["id"]
    original_lookup = KnowledgeDocumentRepository.get_by_id_and_tenant
    # Both requests must have seen the row before either deletes it.
    both_looked_up = asyncio.Barrier(2)
    seen: list[bool] = []

    async def lookup(
        self: KnowledgeDocumentRepository, document_id: UUID, tenant_id: UUID
    ) -> KnowledgeDocument | None:
        document = await original_lookup(self, document_id, tenant_id)
        seen.append(document is not None)
        await asyncio.wait_for(both_looked_up.wait(), _TASK_TIMEOUT_SECONDS)
        return document

    monkeypatch.setattr(KnowledgeDocumentRepository, "get_by_id_and_tenant", lookup)

    responses = await asyncio.wait_for(
        asyncio.gather(
            _delete(knowledge, _clinic_admin(owner), document_id),
            _delete(knowledge, _clinic_admin(owner), document_id),
        ),
        _TASK_TIMEOUT_SECONDS,
    )

    assert seen == [True, True], "both deletes must race past the lookup"
    assert sorted(r.status_code for r in responses) == [204, 404], [
        (r.status_code, r.text) for r in responses
    ]
    loser = next(r for r in responses if r.status_code == 404)
    assert loser.json() == _not_found(document_id)
    assert await _stored(knowledge, document_id) is None


async def _await_blocked_by(knowledge: _Knowledge, holder_pid: int) -> None:
    """Wait until some backend is waiting on a lock held by ``holder_pid``."""
    deadline = asyncio.get_running_loop().time() + _TASK_TIMEOUT_SECONDS
    while True:
        async with knowledge.engine.connect() as connection:
            blocked = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE CAST(:holder AS integer) = ANY(pg_blocking_pids(pid))"
                ),
                {"holder": holder_pid},
            )
        if blocked:
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("the delete never waited on the row lock")
        await asyncio.sleep(0.05)


@pytest.mark.asyncio
@pytest.mark.parametrize("holder_outcome", ["rollback", "delete_and_commit"])
async def test_delete_waits_for_the_row_lock_holder_and_reports_its_outcome(
    knowledge: _Knowledge, holder_outcome: str
) -> None:
    """A locked row is waited for, never skipped: no false 404 on rollback."""
    owner = uuid4()
    await _seed_tenants(knowledge, owner)
    created = await _upload(knowledge, _clinic_admin(owner), title="Travado")
    document_id = created.json()["id"]
    row_id = UUID(document_id)

    async with knowledge.sessions() as holder:
        await holder.execute(
            select(KnowledgeDocument.id)
            .where(KnowledgeDocument.id == row_id)
            .with_for_update()
        )
        if holder_outcome == "delete_and_commit":
            await holder.execute(
                delete(KnowledgeDocument).where(KnowledgeDocument.id == row_id)
            )
        holder_pid = int(await holder.scalar(text("SELECT pg_backend_pid()")))
        request = asyncio.create_task(
            _delete(knowledge, _clinic_admin(owner), document_id)
        )
        try:
            await _await_blocked_by(knowledge, holder_pid)
            assert not request.done(), "the delete must wait for the lock holder"
        finally:
            if holder_outcome == "rollback":
                await holder.rollback()
            else:
                await holder.commit()
    response = await asyncio.wait_for(request, _TASK_TIMEOUT_SECONDS)

    if holder_outcome == "rollback":
        assert (response.status_code, response.content) == (204, b""), response.text
    else:
        assert (response.status_code, response.json()) == (
            404,
            _not_found(document_id),
        )
    assert await _stored(knowledge, document_id) is None


# --------------------------------------------------------------------------
# Tenant row existence
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_upload_into_a_tenant_without_a_row_is_a_clean_404(
    knowledge: _Knowledge,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = uuid4()
    builder_id = uuid4()
    builder_tenant = uuid4()
    await _seed_tenants(knowledge, builder_tenant, builder_id=builder_id)
    caplog.set_level(logging.DEBUG)
    original_create = KnowledgeDocumentRepository.create
    inserts: list[object] = []

    async def create(
        self: KnowledgeDocumentRepository, **kwargs: object
    ) -> KnowledgeDocument:
        inserts.append(kwargs.get("tenant_id"))
        return await original_create(self, **kwargs)

    monkeypatch.setattr(KnowledgeDocumentRepository, "create", create)

    as_super_admin = await _upload(knowledge, _super_admin(), tenant_id=missing)
    # A JWT whose own tenant row does not exist.
    as_own_tenant = await _upload(knowledge, _clinic_admin(missing))
    # A builder never learns whether a clinic it does not own exists.
    as_builder = await _upload(
        knowledge,
        _jwt("builder", builder_tenant, builder_id=builder_id),
        tenant_id=missing,
    )

    for response in (as_super_admin, as_own_tenant):
        assert (response.status_code, response.json()) == (404, _TENANT_NOT_FOUND)
        assert str(missing) not in response.text
    assert (as_builder.status_code, as_builder.json()) == (
        403,
        _TENANT_OVERRIDE_FORBIDDEN,
    )
    assert await _document_count(knowledge) == 0
    # The explicit check refuses before any INSERT is attempted.
    assert inserts == []
    # No foreign-key traceback naming the caller-supplied tenant.
    assert "ForeignKeyViolation" not in caplog.text
    assert _TENANT_FK not in caplog.text

    # Decided behaviour for reads of a tenant without a row: truthful empties.
    listed = await _list(knowledge, _clinic_admin(missing))
    assert (listed.status_code, listed.json()["documents"], listed.json()["total"]) == (
        200,
        [],
        0,
    )


@pytest.mark.asyncio
async def test_tenant_deleted_between_check_and_insert_is_a_clean_404(
    knowledge: _Knowledge, monkeypatch: pytest.MonkeyPatch
) -> None:
    doomed = uuid4()
    await _seed_tenants(knowledge, doomed)
    original_create = KnowledgeDocumentRepository.create
    raced: list[UUID] = []

    async def create_after_race(
        self: KnowledgeDocumentRepository, **kwargs: object
    ) -> KnowledgeDocument:
        # The tenant row disappears after the route checked it.
        async with knowledge.sessions() as other:
            await other.execute(delete(Tenant).where(Tenant.id == doomed))
            await other.commit()
        raced.append(doomed)
        return await original_create(self, **kwargs)

    monkeypatch.setattr(KnowledgeDocumentRepository, "create", create_after_race)

    response = await _upload(knowledge, _super_admin(), tenant_id=doomed)

    assert raced == [doomed], "the insert must run after the tenant row is gone"
    assert (response.status_code, response.json()) == (404, _TENANT_NOT_FOUND)
    assert await _document_count(knowledge) == 0


@pytest.mark.asyncio
async def test_other_integrity_errors_are_not_disguised_as_tenant_not_found(
    knowledge: _Knowledge, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = uuid4()
    await _seed_tenants(knowledge, owner)
    original_create = KnowledgeDocumentRepository.create
    attempted: list[str] = []

    async def create_violating_not_null(
        self: KnowledgeDocumentRepository, **kwargs: object
    ) -> KnowledgeDocument:
        attempted.append(_TITLE_NOT_NULL)
        return await original_create(self, **{**kwargs, "title": None})

    monkeypatch.setattr(
        KnowledgeDocumentRepository, "create", create_violating_not_null
    )

    response = await _upload(knowledge, _clinic_admin(owner))

    assert attempted == [_TITLE_NOT_NULL]
    assert response.status_code == 500, response.text
    assert response.json() != _TENANT_NOT_FOUND
    assert await _document_count(knowledge) == 0


# --------------------------------------------------------------------------
# List totals
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_total_counts_the_filtered_set_of_the_authorized_tenant(
    knowledge: _Knowledge,
) -> None:
    owner, other = uuid4(), uuid4()
    await _seed_tenants(knowledge, owner, other)
    for index in range(3):
        assert (
            await _upload(knowledge, _clinic_admin(owner), title=f"P{index}")
        ).status_code == 201
    for index in range(2):
        assert (
            await _upload(
                knowledge, _clinic_admin(owner), title=f"F{index}", category="faq"
            )
        ).status_code == 201
    assert (
        await _upload(knowledge, _clinic_admin(owner), title="Sem", category=None)
    ).status_code == 201
    for index in range(4):
        assert (
            await _upload(knowledge, _clinic_admin(other), title=f"O{index}")
        ).status_code == 201

    async def page(**params: Any) -> tuple[list[str], int, int, int]:
        response = await _list(knowledge, _clinic_admin(owner), **params)
        assert response.status_code == 200, response.text
        body = response.json()
        assert all(d["tenant_id"] == str(owner) for d in body["documents"])
        return (
            sorted(d["metadata"]["title"] for d in body["documents"]),
            body["total"],
            body["page"],
            body["page_size"],
        )

    first = await page(category="protocolo", page=1, page_size=2)
    second = await page(category="protocolo", page=2, page_size=2)
    beyond = await page(category="protocolo", page=3, page_size=2)
    assert (len(first[0]), first[1:]) == (2, (3, 1, 2))
    assert (len(second[0]), second[1:]) == (1, (3, 2, 2))
    assert sorted(first[0] + second[0]) == ["P0", "P1", "P2"]
    assert beyond == ([], 3, 3, 2)
    assert await page(category="faq") == (["F0", "F1"], 2, 1, 20)
    assert await page(category="inexistente") == ([], 0, 1, 20)
    unfiltered = await page(page_size=100)
    assert (len(unfiltered[0]), unfiltered[1]) == (6, 6)
    # An empty category does not filter, for the page and its total alike.
    empty_category = await page(category="", page_size=100)
    assert empty_category == unfiltered
    empty_second = await page(category="", page=2, page_size=4)
    assert (len(empty_second[0]), empty_second[1]) == (2, 6)

    # A super admin selecting the other tenant counts that tenant only.
    foreign = await _list(
        knowledge, _super_admin(), tenant_id=other, category="protocolo"
    )
    assert foreign.json()["total"] == 4
    empty = uuid4()
    await _seed_tenants(knowledge, empty)
    nothing = await _list(knowledge, _clinic_admin(empty), category="protocolo")
    assert (nothing.json()["documents"], nothing.json()["total"]) == ([], 0)


# --------------------------------------------------------------------------
# Clone copies only the builder's own clinic
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_clone_copies_api_created_documents_only_from_the_builders_clinic(
    knowledge: _Knowledge, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = await _seed_graph(
        SimpleNamespace(session=knowledge.sessions),  # type: ignore[arg-type]
        source_name="Clinica Conhecimento",
        contents=(),
    )
    foreign_clinic = uuid4()
    async with knowledge.sessions() as session:
        session.add(
            Tenant(
                id=foreign_clinic,
                name="Clinica Alheia",
                slug=f"knowledge-foreign-{foreign_clinic.hex}",
                api_key_hash=f"hash-{foreign_clinic.hex}",
                builder_id=graph.foreign_builder_id,
                config={},
            )
        )
        await session.commit()
    builder_headers = _jwt("builder", graph.auth_tenant_id, builder_id=graph.builder_id)
    for title in ("Proprio A", "Proprio B"):
        response = await _upload(
            knowledge, builder_headers, title=title, tenant_id=graph.source_id
        )
        assert response.status_code == 201, response.text
        assert response.json()["tenant_id"] == str(graph.source_id)
    foreign_upload = await _upload(
        knowledge, _super_admin(), title="Alheio", tenant_id=foreign_clinic
    )
    assert foreign_upload.status_code == 201, foreign_upload.text
    # The builder cannot write into the clinic it does not own.
    denied = await _upload(
        knowledge, builder_headers, title="Plantado", tenant_id=foreign_clinic
    )
    assert (denied.status_code, denied.json()) == (403, _TENANT_OVERRIDE_FORBIDDEN)

    _stub_fast_unique_hash(monkeypatch)
    async with knowledge.sessions() as session:
        cloned = await _clone(session, graph, include_rag=True)
    async with knowledge.sessions() as session:
        with pytest.raises(HTTPException) as foreign_clone:
            await _clone(session, graph, source_id=foreign_clinic, include_rag=True)
    assert foreign_clone.value.status_code == 404

    assert cloned.copied_rag_documents == 2
    async with knowledge.sessions() as session:
        rows = (
            await session.execute(
                select(KnowledgeDocument.tenant_id, KnowledgeDocument.title)
            )
        ).all()
    by_tenant: dict[UUID, list[str]] = {}
    for tenant_id, title in rows:
        by_tenant.setdefault(tenant_id, []).append(title)
    assert {key: sorted(value) for key, value in by_tenant.items()} == {
        graph.source_id: ["Proprio A", "Proprio B"],
        cloned.id: ["Proprio A", "Proprio B"],
        foreign_clinic: ["Alheio"],
    }
