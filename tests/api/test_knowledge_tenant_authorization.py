"""Tenant authorization contract for the ``/v1/knowledge`` routes.

Every knowledge route resolves exactly one authorized tenant through the
canonical ``resolve_effective_tenant`` policy before it touches the knowledge
repository: API keys and clinic administrators stay inside their own tenant,
builders may only select clinics they own, super administrators may select a
clinic explicitly, and every other JWT role is denied.  A document requested by
ID is looked up by ID *and* authorized tenant together, so another tenant's
document is indistinguishable from a missing one.

The knowledge routes commit their own successful writes, but a write that
skipped that commit would hide a cross-tenant effect behind a rollback.  This
module therefore replaces the session dependency with one that commits every
successful request: a cross-tenant create or delete that slipped past the
tenant scoping would persist here and fail the test whether or not the route
committed.  (Persistence through the real, non-committing dependency is pinned
by ``tests/api/test_knowledge_persistence.py`` and, on PostgreSQL, by
``tests/integration/test_knowledge_persistence_postgres.py``.)
A denied request still rolls back, and SQLite does not enforce foreign keys,
so a write issued *before* the tenant check would leave no trace in the table;
``write_spy`` therefore observes the write attempt itself.
"""

import logging
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from uuid import UUID, uuid4

import jwt
import pytest
from httpx import Response
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session as OrmSession

from src.api.middleware.auth import require_builder_clinic_access
from src.api.middleware.performance import response_cache
from src.config.settings import settings
from src.models.builder import Builder
from src.models.knowledge import KnowledgeDocument
from src.models.tenant import Tenant
from src.repositories import KnowledgeDocumentRepository, get_db_session_dependency
from src.repositories.database import get_db_session
from tests.conftest import LifespanTestClient

API_KEY_TENANT = UUID("a0000000-0000-0000-0000-000000000001")  # "test-api-key"
API_KEY_TENANT_B = UUID("a0000000-0000-0000-0000-000000000002")
API_KEY_B = {"X-API-Key": "ns_dev_builder_key_67890"}

TITLE = "Protocolo interno exclusivo"
CATEGORY = "protocolo"
MARKER = "segredo-exclusivo-da-base"
BODY_MARKER = "conteudo-confidencial-do-documento"
PLANTED_TITLE = "Planted by another tenant"

ROUTES = ("upload", "search", "list", "get", "delete")
BY_ID_ROUTES = ("get", "delete")
SUCCESS_STATUS = {
    "upload": 201,
    "search": 200,
    "list": 200,
    "get": 200,
    "delete": 204,
}
TENANT_OVERRIDE_FORBIDDEN = {"code": "tenant_override_forbidden"}
CALLER_ROLE_FORBIDDEN = {"code": "caller_role_forbidden"}


# ============================================
# Fixtures and helpers
# ============================================


@pytest.fixture(autouse=True)
def _isolated_response_cache() -> Iterator[None]:
    """Start and finish every test with an empty process-wide response cache."""
    response_cache.invalidate()
    yield
    response_cache.invalidate()


@pytest.fixture(autouse=True)
def _committing_request_sessions(test_app: Any) -> Iterator[None]:
    """Commit every successful request so a leaked write would really persist."""

    async def _committing_session() -> AsyncGenerator[AsyncSession, None]:
        session = get_db_session()
        try:
            yield session
            await session.commit()
        except BaseException:
            await session.rollback()
            raise
        finally:
            await session.close()

    assert get_db_session_dependency not in test_app.dependency_overrides
    test_app.dependency_overrides[get_db_session_dependency] = _committing_session
    try:
        yield
    finally:
        test_app.dependency_overrides.pop(get_db_session_dependency, None)


class _WriteSpy:
    """Record repository and session calls made while ``watching``."""

    WRITE_KINDS = frozenset(
        {
            "repository.create",
            "session.add",
            "session.flush",
            "session.delete",
            "session.execute_dml",
            "orm.before_flush",
        }
    )

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self._armed = False

    def record(self, kind: str, detail: Any = None) -> None:
        if self._armed:
            self.calls.append((kind, detail))

    @contextmanager
    def watching(self) -> Iterator["_WriteSpy"]:
        self.calls.clear()
        self._armed = True
        try:
            yield self
        finally:
            self._armed = False

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]

    def writes(self) -> list[tuple[str, Any]]:
        return [call for call in self.calls if call[0] in self.WRITE_KINDS]

    def first(self, kind: str) -> int:
        return self.kinds().index(kind)


@pytest.fixture
def write_spy(monkeypatch: pytest.MonkeyPatch) -> Iterator[_WriteSpy]:
    """Observe every path a knowledge write can take, plus the scoped lookup.

    Repository ``create``, the async session's ``add``/``flush``/``delete`` and
    DML ``execute``, and any ORM flush carrying pending objects are recorded,
    so a write cannot hide behind the rollback of a denied request.
    """
    spy = _WriteSpy()
    original_create = KnowledgeDocumentRepository.create
    original_lookup = KnowledgeDocumentRepository.get_by_id_and_tenant
    original_add = AsyncSession.add
    original_flush = AsyncSession.flush
    original_delete = AsyncSession.delete
    original_execute = AsyncSession.execute

    async def create(
        self: KnowledgeDocumentRepository, **kwargs: object
    ) -> KnowledgeDocument:
        spy.record("repository.create", kwargs.get("tenant_id"))
        return await original_create(self, **kwargs)

    async def lookup(
        self: KnowledgeDocumentRepository, document_id: UUID, tenant_id: UUID
    ) -> KnowledgeDocument | None:
        spy.record("repository.get_by_id_and_tenant", tenant_id)
        return await original_lookup(self, document_id, tenant_id)

    def add(self: AsyncSession, instance: object, *args: Any, **kwargs: Any) -> None:
        spy.record("session.add", instance)
        original_add(self, instance, *args, **kwargs)

    async def flush(self: AsyncSession, *args: Any, **kwargs: Any) -> None:
        spy.record("session.flush")
        await original_flush(self, *args, **kwargs)

    async def delete(self: AsyncSession, instance: object) -> None:
        spy.record("session.delete", instance)
        await original_delete(self, instance)

    async def execute(
        self: AsyncSession, statement: Any, *args: Any, **kwargs: Any
    ) -> Any:
        if getattr(statement, "is_dml", False):
            spy.record("session.execute_dml", statement)
        return await original_execute(self, statement, *args, **kwargs)

    def before_flush(session: OrmSession, _context: Any, _instances: Any) -> None:
        pending = [*session.new, *session.dirty, *session.deleted]
        if pending:
            spy.record("orm.before_flush", sorted(type(o).__name__ for o in pending))

    monkeypatch.setattr(KnowledgeDocumentRepository, "create", create)
    monkeypatch.setattr(KnowledgeDocumentRepository, "get_by_id_and_tenant", lookup)
    monkeypatch.setattr(AsyncSession, "add", add)
    monkeypatch.setattr(AsyncSession, "flush", flush)
    monkeypatch.setattr(AsyncSession, "delete", delete)
    monkeypatch.setattr(AsyncSession, "execute", execute)
    event.listen(OrmSession, "before_flush", before_flush)
    try:
        yield spy
    finally:
        event.remove(OrmSession, "before_flush", before_flush)


def _jwt_headers(
    role: str | None,
    tenant_id: UUID,
    *,
    builder_id: UUID | None = None,
    expired: bool = False,
) -> dict[str, str]:
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": str(uuid4()),
        "tenant_id": str(tenant_id),
        "role": role,
        "exp": now - timedelta(minutes=5) if expired else now + timedelta(hours=1),
    }
    if builder_id is not None:
        payload["builder_id"] = str(builder_id)
    token = jwt.encode(payload, settings.secret_key, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _clinic_admin(tenant_id: UUID) -> dict[str, str]:
    return _jwt_headers("clinic_admin", tenant_id)


def _seed_tenants(
    client: LifespanTestClient,
    *,
    tenants: list[tuple[UUID, UUID | None]],
    builder_ids: list[UUID] | None = None,
) -> None:
    async def _run() -> None:
        db = get_db_session()
        try:
            for builder_id in builder_ids or []:
                db.add(Builder(id=builder_id, name=f"Builder {builder_id}"))
            for tenant_id, owner_id in tenants:
                db.add(
                    Tenant(
                        id=tenant_id,
                        name=f"Clinic {tenant_id}",
                        slug=f"clinic-{tenant_id}",
                        api_key_hash=f"hash-{tenant_id}",
                        builder_id=owner_id,
                        config={},
                    )
                )
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())


def _seed_document(client: LifespanTestClient, tenant_id: UUID) -> str:
    """Persist one document for ``tenant_id`` outside the routes under test.

    The tenant row is created when missing, as PostgreSQL's foreign key
    requires: an upload into a tenant without a row is a 404 by contract, so a
    successful upload needs the row even where SQLite would not enforce it.
    """
    document_id = uuid4()

    async def _run() -> None:
        db = get_db_session()
        try:
            if await db.get(Tenant, tenant_id) is None:
                db.add(
                    Tenant(
                        id=tenant_id,
                        name=f"Clinic {tenant_id}",
                        slug=f"clinic-{tenant_id}",
                        api_key_hash=f"hash-{tenant_id}",
                        config={},
                    )
                )
                await db.flush()
            db.add(
                KnowledgeDocument(
                    id=document_id,
                    tenant_id=tenant_id,
                    title=TITLE,
                    content=f"{BODY_MARKER} {tenant_id}",
                    content_type="text",
                    document_metadata={
                        "title": TITLE,
                        "category": CATEGORY,
                        "tags": ["interno"],
                        "internal_code": MARKER,
                    },
                    is_indexed=False,
                    chunk_count=0,
                    size_bytes=42,
                )
            )
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())
    return str(document_id)


def _stored_document(
    client: LifespanTestClient, document_id: str
) -> tuple[UUID, str] | None:
    """Read the table directly (no route, no cache) as ``(tenant_id, title)``."""

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


def _stored_titles(client: LifespanTestClient, tenant_id: UUID) -> list[str]:
    async def _run() -> list[str]:
        db = get_db_session()
        try:
            rows = await db.execute(
                select(KnowledgeDocument.title).where(
                    KnowledgeDocument.tenant_id == tenant_id
                )
            )
            return sorted(rows.scalars().all())
        finally:
            await db.close()

    titles: list[str] = client._submit(_run())
    return titles


def _assert_untouched(
    client: LifespanTestClient, tenant_id: UUID, document_id: str
) -> None:
    assert _stored_document(client, document_id) == (
        tenant_id,
        TITLE,
    ), "the victim document was deleted or moved"
    assert _stored_titles(client, tenant_id) == [TITLE], "a document was planted"


def _assert_no_leak(response: Response, document_id: str) -> None:
    for secret in (TITLE, MARKER, BODY_MARKER, CATEGORY, document_id):
        assert secret not in response.text, secret


def _call(
    client: LifespanTestClient,
    route: str,
    headers: dict[str, str],
    *,
    tenant_id: UUID | str | None,
    document_id: str,
) -> Response:
    """Issue one knowledge call; ``tenant_id=None`` omits the field entirely.

    Writes carry the tenant in the body, reads and deletes in the query.
    """
    target = {} if tenant_id is None else {"tenant_id": str(tenant_id)}
    by_id_path = f"/v1/knowledge/documents/{document_id}"
    calls: dict[str, tuple[str, str, dict[str, Any]]] = {
        "upload": (
            "POST",
            "/v1/knowledge/documents",
            {
                "json": {
                    **target,
                    "content": "Conteudo plantado por outro tenant",
                    "metadata": {"title": PLANTED_TITLE, "category": "plantado"},
                }
            },
        ),
        "search": (
            "POST",
            "/v1/knowledge/search",
            {"json": {**target, "query": "horario de funcionamento"}},
        ),
        "list": ("GET", "/v1/knowledge/documents", {"params": target}),
        "get": ("GET", by_id_path, {"params": target}),
        "delete": ("DELETE", by_id_path, {"params": target}),
    }
    method, path, kwargs = calls[route]
    response: Response = client.request(method, path, headers=headers, **kwargs)
    return response


def _assert_success_effect(
    client: LifespanTestClient,
    route: str,
    response: Response,
    *,
    tenant_id: UUID,
    document_id: str,
) -> None:
    """Prove a successful call acted on ``tenant_id`` and only on it."""
    assert response.status_code == SUCCESS_STATUS[route], response.text
    if route == "upload":
        body = response.json()
        assert body["tenant_id"] == str(tenant_id)
        assert body["metadata"]["title"] == PLANTED_TITLE
        assert _stored_document(client, body["id"]) == (tenant_id, PLANTED_TITLE)
        assert _stored_titles(client, tenant_id) == sorted([PLANTED_TITLE, TITLE])
    elif route == "search":
        assert response.json()["results"] == []
        assert response.json()["total"] == 0
        _assert_untouched(client, tenant_id, document_id)
    elif route == "list":
        body = response.json()
        assert [d["id"] for d in body["documents"]] == [document_id]
        assert body["documents"][0]["tenant_id"] == str(tenant_id)
        assert body["documents"][0]["metadata"]["custom"] == {"internal_code": MARKER}
        assert body["total"] == 1
    elif route == "get":
        body = response.json()
        assert body["id"] == document_id
        assert body["tenant_id"] == str(tenant_id)
        assert body["metadata"]["title"] == TITLE
    elif route == "delete":
        assert response.content == b""
        assert _stored_document(client, document_id) is None
        assert _stored_titles(client, tenant_id) == []


def _not_found_body(document_id: str) -> dict[str, str]:
    return {"detail": f"Document {document_id} not found"}


# ============================================
# Cross-tenant access (failing first)
# ============================================


class TestCrossTenantKnowledgeAccess:
    """Tenant B must never read, delete, probe or plant data in tenant A."""

    @pytest.mark.parametrize("route", ROUTES)
    def test_other_clinic_admin_is_forbidden(
        self, client: LifespanTestClient, route: str
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        _seed_tenants(client, tenants=[(tenant_a, None), (tenant_b, None)])
        document_id = _seed_document(client, tenant_a)

        response = _call(
            client,
            route,
            _clinic_admin(tenant_b),
            tenant_id=tenant_a,
            document_id=document_id,
        )

        assert response.status_code == 403, response.text
        assert response.json() == {"detail": TENANT_OVERRIDE_FORBIDDEN}
        _assert_no_leak(response, document_id)
        _assert_untouched(client, tenant_a, document_id)
        assert _stored_titles(client, tenant_b) == []

    @pytest.mark.parametrize("explicit_own_tenant", [True, False])
    @pytest.mark.parametrize("route", BY_ID_ROUTES)
    def test_foreign_document_id_inside_own_scope_is_not_found(
        self, client: LifespanTestClient, route: str, explicit_own_tenant: bool
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        document_id = _seed_document(client, tenant_a)

        response = _call(
            client,
            route,
            _clinic_admin(tenant_b),
            tenant_id=tenant_b if explicit_own_tenant else None,
            document_id=document_id,
        )

        assert response.status_code == 404, response.text
        assert response.json() == _not_found_body(document_id)
        for secret in (TITLE, MARKER, BODY_MARKER, str(tenant_a)):
            assert secret not in response.text
        _assert_untouched(client, tenant_a, document_id)

    @pytest.mark.parametrize("route", BY_ID_ROUTES)
    @pytest.mark.parametrize(
        "caller", ["clinic_admin", "api_key", "super_admin_default", "builder_default"]
    )
    def test_foreign_and_missing_ids_are_indistinguishable(
        self, client: LifespanTestClient, route: str, caller: str
    ) -> None:
        victim, caller_tenant = uuid4(), uuid4()
        foreign_id = _seed_document(client, victim)
        missing_id = str(uuid4())
        headers = {
            "clinic_admin": _clinic_admin(caller_tenant),
            "api_key": {"X-API-Key": "test-api-key"},
            "super_admin_default": _jwt_headers("super_admin", caller_tenant),
            "builder_default": _jwt_headers(
                "builder", caller_tenant, builder_id=uuid4()
            ),
        }[caller]

        foreign = _call(client, route, headers, tenant_id=None, document_id=foreign_id)
        missing = _call(client, route, headers, tenant_id=None, document_id=missing_id)

        assert foreign.status_code == missing.status_code == 404, foreign.text
        assert foreign.json() == _not_found_body(foreign_id)
        assert missing.json() == _not_found_body(missing_id)
        assert foreign.text.replace(foreign_id, "<id>") == missing.text.replace(
            missing_id, "<id>"
        )
        assert foreign.headers.get("content-type") == missing.headers.get(
            "content-type"
        )
        _assert_untouched(client, victim, foreign_id)

    def test_api_key_cannot_select_another_tenant(
        self, client: LifespanTestClient, api_headers: dict[str, str]
    ) -> None:
        tenant_a = uuid4()
        document_id = _seed_document(client, tenant_a)

        for route in ROUTES:
            response = _call(
                client, route, api_headers, tenant_id=tenant_a, document_id=document_id
            )
            assert response.status_code == 403, (route, response.text)
            assert response.json() == {"detail": TENANT_OVERRIDE_FORBIDDEN}, route
            _assert_no_leak(response, document_id)
        _assert_untouched(client, tenant_a, document_id)

    def test_api_key_of_another_tenant_cannot_reach_the_document(
        self, client: LifespanTestClient
    ) -> None:
        document_id = _seed_document(client, API_KEY_TENANT)

        for route in ROUTES:
            explicit = _call(
                client,
                route,
                API_KEY_B,
                tenant_id=API_KEY_TENANT,
                document_id=document_id,
            )
            assert explicit.status_code == 403, (route, explicit.text)
            assert explicit.json() == {"detail": TENANT_OVERRIDE_FORBIDDEN}
        for route in BY_ID_ROUTES:
            defaulted = _call(
                client, route, API_KEY_B, tenant_id=None, document_id=document_id
            )
            assert defaulted.status_code == 404, (route, defaulted.text)
            assert defaulted.json() == _not_found_body(document_id)
        listed = _call(client, "list", API_KEY_B, tenant_id=None, document_id="")
        assert listed.status_code == 200, listed.text
        assert listed.json()["documents"] == []
        assert listed.json()["total"] == 0
        _assert_untouched(client, API_KEY_TENANT, document_id)
        assert _stored_titles(client, API_KEY_TENANT_B) == []


# ============================================
# Own-tenant access
# ============================================


class TestOwnTenantKnowledgeAccess:
    @pytest.mark.parametrize("explicit_tenant", [True, False])
    @pytest.mark.parametrize("route", ROUTES)
    def test_clinic_admin_acts_on_own_tenant(
        self, client: LifespanTestClient, route: str, explicit_tenant: bool
    ) -> None:
        tenant = uuid4()
        document_id = _seed_document(client, tenant)

        response = _call(
            client,
            route,
            _clinic_admin(tenant),
            tenant_id=tenant if explicit_tenant else None,
            document_id=document_id,
        )

        _assert_success_effect(
            client, route, response, tenant_id=tenant, document_id=document_id
        )

    @pytest.mark.parametrize("explicit_tenant", [True, False])
    @pytest.mark.parametrize("route", ROUTES)
    def test_api_key_acts_on_own_tenant(
        self,
        client: LifespanTestClient,
        api_headers: dict[str, str],
        route: str,
        explicit_tenant: bool,
    ) -> None:
        document_id = _seed_document(client, API_KEY_TENANT)

        response = _call(
            client,
            route,
            api_headers,
            tenant_id=API_KEY_TENANT if explicit_tenant else None,
            document_id=document_id,
        )

        _assert_success_effect(
            client, route, response, tenant_id=API_KEY_TENANT, document_id=document_id
        )

    def test_list_without_tenant_id_returns_only_own_documents(
        self, client: LifespanTestClient
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        own = _seed_document(client, tenant_a)
        foreign = _seed_document(client, tenant_b)

        response = client.get(
            "/v1/knowledge/documents", headers=_clinic_admin(tenant_a)
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert [d["id"] for d in body["documents"]] == [own]
        assert body["total"] == 1
        assert foreign not in response.text

    def test_list_pagination_stays_inside_the_tenant(
        self, client: LifespanTestClient
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        own = {_seed_document(client, tenant_a) for _ in range(3)}
        for _ in range(4):
            _seed_document(client, tenant_b)
        headers = _clinic_admin(tenant_a)

        pages = [
            client.get(
                "/v1/knowledge/documents",
                headers=headers,
                params={"page": page, "page_size": 2},
            ).json()
            for page in (1, 2, 3)
        ]

        listed = [d["id"] for page in pages for d in page["documents"]]
        assert sorted(listed) == sorted(own)
        assert [page["total"] for page in pages] == [3, 3, 3]
        assert [len(page["documents"]) for page in pages] == [2, 1, 0]

    def test_category_filter_stays_inside_the_tenant(
        self, client: LifespanTestClient
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        own = _seed_document(client, tenant_a)
        foreign = _seed_document(client, tenant_b)

        defaulted = client.get(
            "/v1/knowledge/documents",
            headers=_clinic_admin(tenant_a),
            params={"category": CATEGORY},
        )
        cross = client.get(
            "/v1/knowledge/documents",
            headers=_clinic_admin(tenant_a),
            params={"category": CATEGORY, "tenant_id": str(tenant_b)},
        )

        assert defaulted.status_code == 200, defaulted.text
        assert [d["id"] for d in defaulted.json()["documents"]] == [own]
        assert foreign not in defaulted.text
        assert cross.status_code == 403, cross.text
        assert cross.json() == {"detail": TENANT_OVERRIDE_FORBIDDEN}


# ============================================
# Privileged tenant selection (canonical convention)
# ============================================


class TestPrivilegedTenantSelection:
    @pytest.mark.parametrize("route", ROUTES)
    def test_super_admin_may_select_any_tenant(
        self, client: LifespanTestClient, route: str
    ) -> None:
        clinic, admin_tenant = uuid4(), uuid4()
        document_id = _seed_document(client, clinic)

        response = _call(
            client,
            route,
            _jwt_headers("super_admin", admin_tenant),
            tenant_id=clinic,
            document_id=document_id,
        )

        _assert_success_effect(
            client, route, response, tenant_id=clinic, document_id=document_id
        )
        assert _stored_titles(client, admin_tenant) == []

    @pytest.mark.parametrize("route", ROUTES)
    def test_super_admin_without_tenant_id_stays_in_own_tenant(
        self, client: LifespanTestClient, route: str
    ) -> None:
        clinic, admin_tenant = uuid4(), uuid4()
        document_id = _seed_document(client, clinic)
        # The caller's own tenant needs a row for its upload to succeed.
        _seed_tenants(client, tenants=[(admin_tenant, None)])

        response = _call(
            client,
            route,
            _jwt_headers("super_admin", admin_tenant),
            tenant_id=None,
            document_id=document_id,
        )

        if route in BY_ID_ROUTES:
            assert response.status_code == 404, response.text
            assert response.json() == _not_found_body(document_id)
        elif route == "list":
            assert response.status_code == 200, response.text
            assert response.json()["documents"] == []
        elif route == "upload":
            assert response.status_code == 201, response.text
            assert response.json()["tenant_id"] == str(admin_tenant)
            assert _stored_titles(client, admin_tenant) == [PLANTED_TITLE]
        else:
            assert response.status_code == 200, response.text
        _assert_untouched(client, clinic, document_id)

    @pytest.mark.parametrize("route", ROUTES)
    def test_linked_builder_may_act_on_owned_clinic(
        self, client: LifespanTestClient, route: str
    ) -> None:
        builder_id, builder_tenant, clinic = uuid4(), uuid4(), uuid4()
        _seed_tenants(client, tenants=[(clinic, builder_id)], builder_ids=[builder_id])
        document_id = _seed_document(client, clinic)

        response = _call(
            client,
            route,
            _jwt_headers("builder", builder_tenant, builder_id=builder_id),
            tenant_id=clinic,
            document_id=document_id,
        )

        _assert_success_effect(
            client, route, response, tenant_id=clinic, document_id=document_id
        )
        assert _stored_titles(client, builder_tenant) == []

    def test_builder_cannot_act_on_unlinked_clinic(
        self, client: LifespanTestClient
    ) -> None:
        builder_id, other_builder = uuid4(), uuid4()
        foreign_clinic, orphan_clinic = uuid4(), uuid4()
        _seed_tenants(
            client,
            tenants=[(foreign_clinic, other_builder), (orphan_clinic, None)],
            builder_ids=[builder_id, other_builder],
        )
        headers = _jwt_headers("builder", uuid4(), builder_id=builder_id)

        for clinic in (foreign_clinic, orphan_clinic, uuid4()):
            document_id = _seed_document(client, clinic)
            for route in ROUTES:
                response = _call(
                    client, route, headers, tenant_id=clinic, document_id=document_id
                )
                assert response.status_code == 403, (route, response.text)
                assert response.json() == {"detail": TENANT_OVERRIDE_FORBIDDEN}
                _assert_no_leak(response, document_id)
            _assert_untouched(client, clinic, document_id)

    def test_builder_without_builder_claim_cannot_select_a_clinic(
        self, client: LifespanTestClient
    ) -> None:
        builder_id, clinic = uuid4(), uuid4()
        _seed_tenants(client, tenants=[(clinic, builder_id)], builder_ids=[builder_id])
        document_id = _seed_document(client, clinic)

        for route in ROUTES:
            response = _call(
                client,
                route,
                _jwt_headers("builder", uuid4()),
                tenant_id=clinic,
                document_id=document_id,
            )
            assert response.status_code == 403, (route, response.text)
            assert response.json() == {"detail": TENANT_OVERRIDE_FORBIDDEN}
        _assert_untouched(client, clinic, document_id)

    @pytest.mark.parametrize("route", ROUTES)
    def test_builder_without_builder_claim_cannot_select_a_builderless_clinic(
        self, client: LifespanTestClient, route: str
    ) -> None:
        """Only the claim guard stops ``builder_id IS NULL`` matching the clinic."""
        clinic = uuid4()
        _seed_tenants(client, tenants=[(clinic, None)])
        document_id = _seed_document(client, clinic)

        async def _ownership_query_without_claim() -> UUID:
            db = get_db_session()
            try:
                return await require_builder_clinic_access(clinic, cast(UUID, None), db)
            finally:
                await db.close()

        # Pre-registered: the ownership query alone would grant this clinic.
        assert client._submit(_ownership_query_without_claim()) == clinic

        response = _call(
            client,
            route,
            _jwt_headers("builder", uuid4()),
            tenant_id=clinic,
            document_id=document_id,
        )

        assert response.status_code == 403, response.text
        assert response.json() == {"detail": TENANT_OVERRIDE_FORBIDDEN}
        _assert_no_leak(response, document_id)
        _assert_untouched(client, clinic, document_id)

    @pytest.mark.parametrize("route", BY_ID_ROUTES)
    def test_builder_without_tenant_id_stays_in_token_tenant(
        self, client: LifespanTestClient, route: str
    ) -> None:
        builder_id, builder_tenant, clinic = uuid4(), uuid4(), uuid4()
        _seed_tenants(client, tenants=[(clinic, builder_id)], builder_ids=[builder_id])
        document_id = _seed_document(client, clinic)

        response = _call(
            client,
            route,
            _jwt_headers("builder", builder_tenant, builder_id=builder_id),
            tenant_id=None,
            document_id=document_id,
        )

        assert response.status_code == 404, response.text
        assert response.json() == _not_found_body(document_id)
        _assert_untouched(client, clinic, document_id)

    @pytest.mark.parametrize("explicit_tenant", [True, False])
    @pytest.mark.parametrize("role", ["investor", "unrecognized", None])
    def test_non_operational_roles_are_denied_even_for_own_tenant(
        self,
        client: LifespanTestClient,
        role: str | None,
        explicit_tenant: bool,
    ) -> None:
        tenant = uuid4()
        document_id = _seed_document(client, tenant)
        headers = _jwt_headers(role, tenant)

        for route in ROUTES:
            response = _call(
                client,
                route,
                headers,
                tenant_id=tenant if explicit_tenant else None,
                document_id=document_id,
            )
            assert response.status_code == 403, (route, response.text)
            assert response.json() == {"detail": CALLER_ROLE_FORBIDDEN}, route
            _assert_no_leak(response, document_id)
        for route in BY_ID_ROUTES:
            missing = _call(
                client, route, headers, tenant_id=None, document_id=str(uuid4())
            )
            assert missing.status_code == 403, (route, missing.text)
            assert missing.json() == {"detail": CALLER_ROLE_FORBIDDEN}
        _assert_untouched(client, tenant, document_id)


# ============================================
# Authorization precedes every write
# ============================================

DENIALS = (
    "foreign_tenant",
    "forbidden_role",
    "nonexistent_tenant_clinic_admin",
    "nonexistent_tenant_api_key",
    "nonexistent_clinic_builder",
    "builderless_clinic_without_claim",
)


def _denied_request(
    client: LifespanTestClient, denial: str, *, victim: UUID
) -> tuple[dict[str, str], UUID | None, dict[str, str]]:
    """Return the caller headers, requested tenant and expected 403 detail."""
    if denial == "foreign_tenant":
        return _clinic_admin(uuid4()), victim, TENANT_OVERRIDE_FORBIDDEN
    if denial == "forbidden_role":
        return _jwt_headers("investor", victim), None, CALLER_ROLE_FORBIDDEN
    if denial == "nonexistent_tenant_clinic_admin":
        return _clinic_admin(uuid4()), uuid4(), TENANT_OVERRIDE_FORBIDDEN
    if denial == "nonexistent_tenant_api_key":
        return {"X-API-Key": "test-api-key"}, uuid4(), TENANT_OVERRIDE_FORBIDDEN
    if denial == "nonexistent_clinic_builder":
        builder_id = uuid4()
        _seed_tenants(client, tenants=[], builder_ids=[builder_id])
        headers = _jwt_headers("builder", uuid4(), builder_id=builder_id)
        return headers, uuid4(), TENANT_OVERRIDE_FORBIDDEN
    if denial == "builderless_clinic_without_claim":
        return _jwt_headers("builder", uuid4()), victim, TENANT_OVERRIDE_FORBIDDEN
    raise AssertionError(f"unknown denial {denial}")


class TestAuthorizationPrecedesWrites:
    """A denied request must not attempt a write, not merely roll one back."""

    @pytest.mark.parametrize("route", ["upload", "delete"])
    @pytest.mark.parametrize("denial", DENIALS)
    def test_denied_request_attempts_no_lookup_or_write(
        self,
        client: LifespanTestClient,
        write_spy: _WriteSpy,
        route: str,
        denial: str,
    ) -> None:
        victim = uuid4()
        _seed_tenants(client, tenants=[(victim, None)])
        document_id = _seed_document(client, victim)
        headers, target, detail = _denied_request(client, denial, victim=victim)

        with write_spy.watching():
            response = _call(
                client, route, headers, tenant_id=target, document_id=document_id
            )

        assert response.status_code == 403, response.text
        assert response.json() == {"detail": detail}
        assert write_spy.calls == [], write_spy.calls
        _assert_untouched(client, victim, document_id)

    def test_foreign_document_delete_is_looked_up_in_scope_and_never_deleted(
        self, client: LifespanTestClient, write_spy: _WriteSpy
    ) -> None:
        victim, caller = uuid4(), uuid4()
        document_id = _seed_document(client, victim)

        with write_spy.watching():
            response = _call(
                client,
                "delete",
                _clinic_admin(caller),
                tenant_id=None,
                document_id=document_id,
            )

        assert response.status_code == 404, response.text
        assert response.json() == _not_found_body(document_id)
        assert write_spy.calls == [("repository.get_by_id_and_tenant", caller)]
        _assert_untouched(client, victim, document_id)

    def test_spy_observes_the_authorized_upload_write(
        self, client: LifespanTestClient, write_spy: _WriteSpy
    ) -> None:
        """Pre-registered positive control: the spy sees a real write."""
        tenant = uuid4()
        document_id = _seed_document(client, tenant)

        with write_spy.watching():
            response = _call(
                client,
                "upload",
                _clinic_admin(tenant),
                tenant_id=None,
                document_id=document_id,
            )

        assert response.status_code == 201, response.text
        assert write_spy.calls[0] == ("repository.create", tenant)
        added = [detail for kind, detail in write_spy.calls if kind == "session.add"]
        assert [type(instance) for instance in added] == [KnowledgeDocument]
        assert cast(KnowledgeDocument, added[0]).tenant_id == tenant
        assert (
            write_spy.first("repository.create")
            < write_spy.first("session.add")
            < write_spy.first("session.flush")
        )
        assert ("orm.before_flush", ["KnowledgeDocument"]) in write_spy.calls

    def test_spy_observes_the_scoped_lookup_before_the_authorized_delete(
        self, client: LifespanTestClient, write_spy: _WriteSpy
    ) -> None:
        """Pre-registered positive control: lookup in scope, then delete."""
        tenant = uuid4()
        document_id = _seed_document(client, tenant)

        with write_spy.watching():
            response = _call(
                client,
                "delete",
                _clinic_admin(tenant),
                tenant_id=None,
                document_id=document_id,
            )

        assert response.status_code == 204, response.text
        assert write_spy.calls[0] == ("repository.get_by_id_and_tenant", tenant)
        deleted = [
            cast(KnowledgeDocument, detail)
            for kind, detail in write_spy.calls
            if kind == "session.delete"
        ]
        assert [(d.id, d.tenant_id) for d in deleted] == [(UUID(document_id), tenant)]
        assert write_spy.first("repository.get_by_id_and_tenant") < write_spy.first(
            "session.delete"
        )
        assert _stored_document(client, document_id) is None


# ============================================
# Authentication boundary
# ============================================


class TestKnowledgeAuthentication:
    @pytest.mark.parametrize("credential", ["missing", "invalid", "expired", "api"])
    def test_unauthenticated_calls_are_rejected(
        self, client: LifespanTestClient, credential: str
    ) -> None:
        tenant = uuid4()
        document_id = _seed_document(client, tenant)
        headers = {
            "missing": {},
            "invalid": {"Authorization": "Bearer not-a-jwt"},
            "expired": _jwt_headers("super_admin", tenant, expired=True),
            "api": {"X-API-Key": "not-a-known-key"},
        }[credential]

        for route in ROUTES:
            for target in (tenant, None):
                response = _call(
                    client, route, headers, tenant_id=target, document_id=document_id
                )
                assert response.status_code == 401, (route, response.text)
                _assert_no_leak(response, document_id)
        _assert_untouched(client, tenant, document_id)


# ============================================
# Identifier formats and tenant sources
# ============================================


class TestIdentifierFormats:
    @staticmethod
    def _variants(value: UUID) -> list[str]:
        text = str(value)
        return [text.upper(), value.hex, f"{{{text}}}", f"urn:uuid:{text}"]

    @pytest.mark.parametrize("route", ROUTES)
    def test_own_tenant_in_any_uuid_spelling_is_the_own_tenant(
        self, client: LifespanTestClient, route: str
    ) -> None:
        tenant = UUID(f"abcdef00-0000-4000-8000-{uuid4().hex[:12]}")
        document_id = _seed_document(client, tenant)
        headers = _clinic_admin(tenant)

        for spelling in self._variants(tenant):
            response = _call(
                client, route, headers, tenant_id=spelling, document_id=document_id
            )
            if route == "delete":
                _assert_success_effect(
                    client, route, response, tenant_id=tenant, document_id=document_id
                )
                document_id = _seed_document(client, tenant)
            elif route == "upload":
                assert response.status_code == 201, (spelling, response.text)
                assert response.json()["tenant_id"] == str(tenant)
            else:
                _assert_success_effect(
                    client, route, response, tenant_id=tenant, document_id=document_id
                )

    @pytest.mark.parametrize("route", ROUTES)
    def test_foreign_tenant_in_any_uuid_spelling_is_forbidden(
        self, client: LifespanTestClient, route: str
    ) -> None:
        victim = UUID(f"abcdef00-0000-4000-8000-{uuid4().hex[:12]}")
        document_id = _seed_document(client, victim)
        headers = _clinic_admin(uuid4())

        for spelling in self._variants(victim):
            response = _call(
                client, route, headers, tenant_id=spelling, document_id=document_id
            )
            assert response.status_code == 403, (spelling, response.text)
            assert response.json() == {"detail": TENANT_OVERRIDE_FORBIDDEN}
        _assert_untouched(client, victim, document_id)

    @pytest.mark.parametrize("route", BY_ID_ROUTES)
    def test_document_id_spellings_resolve_to_the_same_scoped_lookup(
        self, client: LifespanTestClient, route: str
    ) -> None:
        tenant, other = uuid4(), uuid4()
        document_id = _seed_document(client, tenant)
        canonical = UUID(document_id)

        for spelling in (document_id.upper(), canonical.hex, f"urn:uuid:{document_id}"):
            foreign = _call(
                client,
                route,
                _clinic_admin(other),
                tenant_id=None,
                document_id=spelling,
            )
            assert foreign.status_code == 404, (spelling, foreign.text)
            assert foreign.json() == _not_found_body(document_id)
        _assert_untouched(client, tenant, document_id)

        own = _call(
            client,
            route,
            _clinic_admin(tenant),
            tenant_id=None,
            document_id=canonical.hex.upper(),
        )
        _assert_success_effect(
            client, route, own, tenant_id=tenant, document_id=document_id
        )

    @pytest.mark.parametrize("route", ROUTES)
    def test_malformed_tenant_id_is_rejected_without_data(
        self, client: LifespanTestClient, route: str
    ) -> None:
        tenant = uuid4()
        document_id = _seed_document(client, tenant)

        for malformed in ("not-a-uuid", f" {tenant}", str(tenant)[:-1], ""):
            response = _call(
                client,
                route,
                _clinic_admin(tenant),
                tenant_id=malformed,
                document_id=document_id,
            )
            assert response.status_code == 422, (repr(malformed), response.text)
            for secret in (TITLE, MARKER, BODY_MARKER):
                assert secret not in response.text
        _assert_untouched(client, tenant, document_id)

    @pytest.mark.parametrize("route", BY_ID_ROUTES)
    def test_malformed_document_id_is_rejected(
        self, client: LifespanTestClient, route: str
    ) -> None:
        tenant = uuid4()
        document_id = _seed_document(client, tenant)

        response = _call(
            client,
            route,
            _clinic_admin(tenant),
            tenant_id=None,
            document_id=document_id[:-1],
        )

        assert response.status_code == 422, response.text
        _assert_untouched(client, tenant, document_id)


class TestTenantSourceMismatch:
    """Each route reads its tenant from one place; other places never select."""

    @pytest.mark.parametrize("route", ["upload", "search"])
    def test_body_tenant_governs_writes_even_with_own_tenant_in_query(
        self, client: LifespanTestClient, route: str
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        document_id = _seed_document(client, tenant_a)
        path = (
            "/v1/knowledge/documents" if route == "upload" else "/v1/knowledge/search"
        )
        body: dict[str, Any] = (
            {"content": "Conteudo plantado", "metadata": {"title": PLANTED_TITLE}}
            if route == "upload"
            else {"query": "horario de funcionamento"}
        )

        response = client.post(
            path,
            headers=_clinic_admin(tenant_b),
            params={"tenant_id": str(tenant_b)},
            json={**body, "tenant_id": str(tenant_a)},
        )

        assert response.status_code == 403, response.text
        assert response.json() == {"detail": TENANT_OVERRIDE_FORBIDDEN}
        _assert_untouched(client, tenant_a, document_id)
        assert _stored_titles(client, tenant_b) == []

    def test_query_tenant_cannot_redirect_an_upload(
        self, client: LifespanTestClient
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        document_id = _seed_document(client, tenant_a)
        # The caller's own tenant needs a row for its upload to succeed.
        _seed_tenants(client, tenants=[(tenant_b, None)])

        response = client.post(
            "/v1/knowledge/documents",
            headers=_clinic_admin(tenant_b),
            params={"tenant_id": str(tenant_a)},
            json={"content": "Conteudo plantado", "metadata": {"title": PLANTED_TITLE}},
        )

        assert response.status_code == 201, response.text
        assert response.json()["tenant_id"] == str(tenant_b)
        assert _stored_titles(client, tenant_b) == [PLANTED_TITLE]
        _assert_untouched(client, tenant_a, document_id)

    @pytest.mark.parametrize("route", BY_ID_ROUTES)
    def test_body_tenant_cannot_select_for_by_id_routes(
        self, client: LifespanTestClient, route: str
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        document_id = _seed_document(client, tenant_a)

        response = client.request(
            "GET" if route == "get" else "DELETE",
            f"/v1/knowledge/documents/{document_id}",
            headers=_clinic_admin(tenant_b),
            json={"tenant_id": str(tenant_a)},
        )

        assert response.status_code == 404, response.text
        assert response.json() == _not_found_body(document_id)
        _assert_untouched(client, tenant_a, document_id)

    @pytest.mark.parametrize("route", ROUTES)
    def test_spoofed_tenant_header_never_selects_a_tenant(
        self, client: LifespanTestClient, route: str
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        document_id = _seed_document(client, tenant_a)
        # The caller's own tenant needs a row for its upload to succeed.
        _seed_tenants(client, tenants=[(tenant_b, None)])
        headers = {**_clinic_admin(tenant_b), "X-Tenant-ID": str(tenant_a)}

        response = _call(
            client, route, headers, tenant_id=None, document_id=document_id
        )

        if route in BY_ID_ROUTES:
            assert response.status_code == 404, response.text
            assert response.json() == _not_found_body(document_id)
        else:
            assert response.status_code == SUCCESS_STATUS[route], response.text
            _assert_no_leak(response, document_id)
        if route == "list":
            assert response.json()["documents"] == []
        if route == "upload":
            assert response.json()["tenant_id"] == str(tenant_b)
        _assert_untouched(client, tenant_a, document_id)


# ============================================
# Caching and error hygiene
# ============================================


class TestKnowledgeResponseCaching:
    def test_knowledge_reads_never_enter_the_response_cache(
        self, client: LifespanTestClient
    ) -> None:
        tenant = uuid4()
        document_id = _seed_document(client, tenant)
        headers = _clinic_admin(tenant)
        response_cache.invalidate()

        responses = [
            client.get("/v1/knowledge/documents", headers=headers),
            client.get("/v1/knowledge/documents", headers=headers),
            client.get(f"/v1/knowledge/documents/{document_id}", headers=headers),
            client.get(f"/v1/knowledge/documents/{document_id}", headers=headers),
        ]

        assert [r.status_code for r in responses] == [200, 200, 200, 200]
        assert [r.headers.get("x-cache") for r in responses] == ["BYPASS"] * 4
        assert response_cache.size == 0

    def test_a_tenant_read_is_never_replayed_to_another_tenant(
        self, client: LifespanTestClient
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        document_id = _seed_document(client, tenant_a)
        spoof = {"X-Tenant-ID": str(tenant_a)}
        path = f"/v1/knowledge/documents/{document_id}"

        warmed = client.get(path, headers={**_clinic_admin(tenant_a), **spoof})
        replayed = client.get(path, headers={**_clinic_admin(tenant_b), **spoof})
        listed = client.get(
            "/v1/knowledge/documents",
            params={"tenant_id": str(tenant_a)},
            headers={**_clinic_admin(tenant_b), **spoof},
        )

        assert warmed.status_code == 200, warmed.text
        assert replayed.status_code == 404, replayed.text
        assert replayed.json() == _not_found_body(document_id)
        assert listed.status_code == 403, listed.text
        _assert_no_leak(listed, document_id)

    @pytest.mark.parametrize("route", ROUTES)
    def test_knowledge_responses_forbid_downstream_storage(
        self, client: LifespanTestClient, route: str
    ) -> None:
        tenant = uuid4()
        document_id = _seed_document(client, tenant)

        response = _call(
            client,
            route,
            _clinic_admin(tenant),
            tenant_id=None,
            document_id=document_id,
        )

        assert response.status_code == SUCCESS_STATUS[route], response.text
        assert response.headers.get("cache-control") == "no-store"


class TestKnowledgeErrorHygiene:
    @pytest.mark.parametrize("route", ROUTES)
    def test_denials_log_no_document_data(
        self,
        client: LifespanTestClient,
        caplog: pytest.LogCaptureFixture,
        route: str,
    ) -> None:
        tenant_a, tenant_b = uuid4(), uuid4()
        document_id = _seed_document(client, tenant_a)
        caplog.set_level(logging.DEBUG)

        responses = [
            _call(
                client,
                route,
                _clinic_admin(tenant_b),
                tenant_id=tenant_a,
                document_id=document_id,
            ),
            _call(
                client,
                route,
                _jwt_headers("investor", tenant_a),
                tenant_id=None,
                document_id=document_id,
            ),
        ]
        if route in BY_ID_ROUTES:
            responses.append(
                _call(
                    client,
                    route,
                    _clinic_admin(tenant_b),
                    tenant_id=None,
                    document_id=document_id,
                )
            )

        assert [r.status_code for r in responses][:2] == [403, 403]
        for secret in (TITLE, MARKER, BODY_MARKER):
            assert secret not in caplog.text
            assert all(secret not in r.text for r in responses)
        assert "Traceback" not in caplog.text


# ============================================
# OpenAPI contract
# ============================================


class TestKnowledgeOpenAPI:
    @staticmethod
    def _operation(client: LifespanTestClient, path: str, method: str) -> Any:
        schema = client.get("/v1/openapi.json").json()
        return schema["paths"][f"/v1/knowledge{path}"][method]

    @staticmethod
    def _tenant_query(operation: Any) -> Any:
        params = [
            p
            for p in operation.get("parameters", [])
            if p["name"] == "tenant_id" and p["in"] == "query"
        ]
        assert len(params) == 1, operation.get("parameters")
        return params[0]

    @pytest.mark.parametrize(
        ("path", "method"),
        [
            ("/documents", "get"),
            ("/documents/{document_id}", "get"),
            ("/documents/{document_id}", "delete"),
        ],
    )
    def test_query_tenant_is_optional(
        self, client: LifespanTestClient, path: str, method: str
    ) -> None:
        operation = self._operation(client, path, method)

        assert self._tenant_query(operation).get("required", False) is False
        assert "403" in operation["responses"]

    @pytest.mark.parametrize(
        ("path", "schema_name"),
        [("/documents", "DocumentUploadRequest"), ("/search", "SearchRequest")],
    )
    def test_body_tenant_is_optional(
        self, client: LifespanTestClient, path: str, schema_name: str
    ) -> None:
        schema = client.get("/v1/openapi.json").json()
        body = schema["components"]["schemas"][schema_name]
        operation = schema["paths"][f"/v1/knowledge{path}"]["post"]

        assert "tenant_id" in body["properties"]
        assert "tenant_id" not in body.get("required", [])
        assert "403" in operation["responses"]
