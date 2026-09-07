"""Contract and transaction tests for Builder-scoped clinic cloning."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import TenantContext
from src.api.schemas.builder import (
    ClinicCloneErrorResponse,
    ClinicClonePreviewResponse,
    ClinicCloneResponse,
)
from src.config.settings import settings
from src.models.builder import Builder
from src.models.clinic_config_table import ClinicConfigTable
from src.models.knowledge import KnowledgeDocument
from src.models.tenant import Tenant
from src.models.user import User, UserRole
from src.repositories import get_db_engine, get_db_session

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "clinic_clone_contract.json"
)
_CONTRACT = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))


def _clone_path(contract_key: str, source_id: UUID) -> str:
    return _CONTRACT[contract_key]["path"].format(source_id=source_id)


async def _request_clone_contract(
    client,
    contract_key: str,
    source_id: UUID,
    headers: dict[str, str],
):
    endpoint = _CONTRACT[contract_key]
    return await client.request(
        endpoint["method"],
        endpoint["path"].format(source_id=source_id),
        headers=headers,
    )


@dataclass(frozen=True)
class _CloneGraph:
    builder_id: UUID
    auth_tenant_id: UUID
    user_id: UUID
    source_id: UUID

    def headers(
        self,
        *,
        builder_id: UUID | None = None,
        tenant_id: UUID | None = None,
    ) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "sub": str(self.user_id),
                "tenant_id": str(tenant_id or self.auth_tenant_id),
                "role": "builder",
                "email": "clone-builder@test.local",
                "builder_id": str(builder_id or self.builder_id),
                "iat": now,
                "exp": now + timedelta(hours=1),
                "jti": str(uuid4()),
            },
            settings.secret_key,
            algorithm="HS256",
        )
        return {"Authorization": f"Bearer {token}"}


async def _seed_clone_graph(*, existing_copy: bool = False) -> _CloneGraph:
    session = get_db_session()
    try:
        builder_id = uuid4()
        auth_tenant_id = uuid4()
        user_id = uuid4()
        source_id = uuid4()
        session.add_all(
            [
                Builder(
                    id=builder_id,
                    name=f"Clone Builder {builder_id.hex[:8]}",
                    status="active",
                    is_active=True,
                ),
                Tenant(
                    id=auth_tenant_id,
                    name="Builder login tenant",
                    slug=f"clone-auth-{auth_tenant_id.hex}",
                    api_key_hash=f"auth-hash-{auth_tenant_id.hex}",
                    is_active=True,
                    plan_type="admin",
                    config={},
                ),
                Tenant(
                    id=source_id,
                    name="Clínica Ocular",
                    slug=f"clone-source-{source_id.hex}",
                    api_key_hash=f"source-hash-{source_id.hex}",
                    is_active=True,
                    plan_type="clinic",
                    builder_id=builder_id,
                    contact_phone="5511999999999",
                    config={"CONFIG_SENTINEL": "must-not-copy"},
                ),
            ]
        )
        session.add(
            User(
                id=user_id,
                tenant_id=auth_tenant_id,
                email=f"clone-builder-{user_id.hex}@test.local",
                password_hash="not-a-real-password-hash",
                role=UserRole.builder,
                builder_id=builder_id,
                is_active=True,
            )
        )
        session.add(
            ClinicConfigTable(
                tenant_id=source_id,
                version=1,
                persona={"agent_display_name": "Lia"},
                scheduling={
                    "has_scheduling_agent": True,
                    "scheduling_link_enabled": True,
                    "scheduling_link_url": "https://scheduling-sentinel.test/secret",
                    "scheduling_link_message": "Agende",
                },
                pricing={},
                post_care={},
                handoff={},
                identity={
                    "clinic_name": "SOURCE_CONFIG_SENTINEL",
                    "clinic_history": "CONFIG_SENTINEL",
                },
                operations={},
                business_rules={},
                lgpd={},
                professionals=[],
                services=[],
                api_credentials={"llm_api_key": "CREDENTIAL_SENTINEL"},
            )
        )
        session.add_all(
            [
                KnowledgeDocument(
                    tenant_id=source_id,
                    title="Private authored title",
                    content="KNOWLEDGE_SENTINEL",
                    content_type="text",
                    document_metadata={"private": "METADATA_SENTINEL"},
                    vector_id="SOURCE_VECTOR_SENTINEL",
                    is_indexed=True,
                    chunk_count=7,
                    size_bytes=999,
                    index_error="SOURCE_INDEX_ERROR",
                ),
                KnowledgeDocument(
                    tenant_id=source_id,
                    title="Second authored title",
                    content="second-doc!!!",
                    content_type="markdown",
                    document_metadata={"kind": "authored"},
                    vector_id="another-source-vector",
                    is_indexed=True,
                    chunk_count=2,
                    size_bytes=999,
                ),
            ]
        )
        if existing_copy:
            copy_id = uuid4()
            session.add(
                Tenant(
                    id=copy_id,
                    name="Clínica Ocular cópia 1",
                    slug=f"existing-clone-{copy_id.hex}",
                    api_key_hash=f"existing-clone-hash-{copy_id.hex}",
                    is_active=False,
                    plan_type="clinic",
                    builder_id=builder_id,
                    config={},
                )
            )
        await session.commit()
        return _CloneGraph(builder_id, auth_tenant_id, user_id, source_id)
    finally:
        await session.close()


def test_shared_contract_fixture_matches_strict_response_schemas() -> None:
    preview = ClinicClonePreviewResponse.model_validate_json(
        json.dumps(_CONTRACT["preview"]["body"])
    )
    success = ClinicCloneResponse.model_validate_json(
        json.dumps(_CONTRACT["success"]["body"])
    )

    assert preview.model_dump(mode="json") == _CONTRACT["preview"]["body"]
    assert success.model_dump(mode="json") == _CONTRACT["success"]["body"]
    assert _CONTRACT["preview"]["method"] == "GET"
    assert _CONTRACT["success"]["method"] == "POST"
    for error in _CONTRACT["errors"].values():
        parsed_error = ClinicCloneErrorResponse.model_validate(error["body"])
        assert parsed_error.model_dump(mode="json") == error["body"]
    with pytest.raises(ValidationError):
        ClinicClonePreviewResponse.model_validate(
            {**_CONTRACT["preview"]["body"], "config": "CONFIG_SENTINEL"}
        )
    with pytest.raises(ValidationError):
        ClinicCloneResponse.model_validate(
            {
                "id": UUID(_CONTRACT["success"]["body"]["id"]),
                "name": _CONTRACT["success"]["body"]["name"],
                "is_active": True,
            }
        )


@pytest.mark.asyncio
async def test_preview_returns_only_fixture_contract_after_current_authority(
    async_client,
) -> None:
    graph = await _seed_clone_graph(existing_copy=True)

    response = await _request_clone_contract(
        async_client, "preview", graph.source_id, graph.headers()
    )

    assert response.status_code == _CONTRACT["preview"]["status"]
    assert response.json() == _CONTRACT["preview"]["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authority_mutation",
    [
        "inactive_user",
        "deleted_user",
        "wrong_role",
        "cleared_user_builder",
        "reassigned_user_builder",
        "inactive_builder",
        "inactive_builder_status",
        "missing_builder",
        "stale_tenant",
        "stale_link",
    ],
)
async def test_clone_routes_revalidate_stale_authority_with_exact_403(
    async_client,
    authority_mutation: str,
) -> None:
    graph = await _seed_clone_graph()
    session = get_db_session()
    try:
        if authority_mutation in {
            "inactive_user",
            "deleted_user",
            "wrong_role",
            "cleared_user_builder",
            "reassigned_user_builder",
        }:
            user = await session.get(User, graph.user_id)
            assert user is not None
            if authority_mutation == "inactive_user":
                user.is_active = False
            elif authority_mutation == "deleted_user":
                await session.delete(user)
            elif authority_mutation == "cleared_user_builder":
                user.builder_id = None
            elif authority_mutation == "reassigned_user_builder":
                replacement = Builder(
                    id=uuid4(), name="Replacement", status="active", is_active=True
                )
                session.add(replacement)
                user.builder_id = replacement.id
            else:
                user.role = UserRole.clinic_admin
        elif authority_mutation in {
            "inactive_builder",
            "inactive_builder_status",
            "missing_builder",
        }:
            builder = await session.get(Builder, graph.builder_id)
            assert builder is not None
            builder.is_active = False
            if authority_mutation == "inactive_builder_status":
                builder.is_active = True
                builder.status = "suspended"
            elif authority_mutation == "missing_builder":
                await session.delete(builder)
        await session.commit()
    finally:
        await session.close()

    headers = graph.headers(
        tenant_id=uuid4() if authority_mutation == "stale_tenant" else None,
        builder_id=uuid4() if authority_mutation == "stale_link" else None,
    )
    for contract_key in ("preview", "success"):
        response = await _request_clone_contract(
            async_client, contract_key, graph.source_id, headers
        )
        assert response.status_code == _CONTRACT["errors"]["403"]["status"]
        assert response.json() == _CONTRACT["errors"]["403"]["body"]


@pytest.mark.asyncio
async def test_clone_routes_reject_api_key_caller_with_exact_403(async_client) -> None:
    graph = await _seed_clone_graph()

    for contract_key in ("preview", "success"):
        response = await _request_clone_contract(
            async_client,
            contract_key,
            graph.source_id,
            {"X-API-Key": "test-api-key"},
        )
        assert response.status_code == _CONTRACT["errors"]["403"]["status"]
        assert response.json() == _CONTRACT["errors"]["403"]["body"]


@pytest.mark.asyncio
@pytest.mark.parametrize("source_state", ["missing", "foreign", "unlinked", "deleted"])
async def test_inaccessible_source_states_share_exact_404(
    async_client,
    source_state: str,
) -> None:
    graph = await _seed_clone_graph()
    requested_source_id = graph.source_id
    session = get_db_session()
    try:
        source = await session.get(Tenant, graph.source_id)
        assert source is not None
        if source_state == "missing":
            requested_source_id = uuid4()
        elif source_state == "foreign":
            foreign_builder = Builder(
                id=uuid4(), name="Foreign Builder", status="active", is_active=True
            )
            session.add(foreign_builder)
            source.builder_id = foreign_builder.id
        elif source_state == "unlinked":
            source.builder_id = None
        else:
            await session.delete(source)
        await session.commit()
    finally:
        await session.close()

    for contract_key in ("preview", "success"):
        response = await _request_clone_contract(
            async_client, contract_key, requested_source_id, graph.headers()
        )
        assert response.status_code == _CONTRACT["errors"]["404"]["status"]
        assert response.json() == _CONTRACT["errors"]["404"]["body"]


@pytest.mark.asyncio
async def test_post_persists_atomic_redacted_reset_clone(
    async_client,
    monkeypatch,
    caplog,
) -> None:
    graph = await _seed_clone_graph(existing_copy=True)
    expected_id = UUID(_CONTRACT["success"]["body"]["id"])
    generated_ids = iter([expected_id, uuid4()])
    secret_calls: list[int] = []

    monkeypatch.setattr(
        "src.api.routes.builder.uuid4",
        lambda: next(generated_ids),
    )

    def _secret(byte_count: int) -> str:
        secret_calls.append(byte_count)
        return "PLAINTEXT_CREDENTIAL_SENTINEL_0123456789"

    monkeypatch.setattr("src.api.routes.builder.secrets.token_urlsafe", _secret)

    response = await _request_clone_contract(
        async_client, "success", graph.source_id, graph.headers()
    )

    assert response.status_code == _CONTRACT["success"]["status"]
    assert response.json() == _CONTRACT["success"]["body"]
    assert secret_calls == [32]
    exposed = response.text + caplog.text
    for sentinel in (
        "PLAINTEXT_CREDENTIAL_SENTINEL",
        "CREDENTIAL_SENTINEL",
        "CONFIG_SENTINEL",
        "KNOWLEDGE_SENTINEL",
        "METADATA_SENTINEL",
        "SOURCE_VECTOR_SENTINEL",
    ):
        assert sentinel not in exposed

    session = get_db_session()
    try:
        tenant = await session.get(Tenant, expected_id)
        assert tenant is not None
        assert tenant.name == _CONTRACT["success"]["body"]["name"]
        assert tenant.builder_id == graph.builder_id
        assert tenant.is_active is False
        assert tenant.plan_type == "clinic"
        assert tenant.config == {}
        assert tenant.contact_phone is None
        assert tenant.api_key_hash != "PLAINTEXT_CREDENTIAL_SENTINEL_0123456789"
        assert "PLAINTEXT_CREDENTIAL_SENTINEL" not in tenant.api_key_hash

        config_result = await session.execute(
            select(ClinicConfigTable).where(ClinicConfigTable.tenant_id == expected_id)
        )
        config = config_result.scalar_one()
        assert config.identity["clinic_name"] == tenant.name
        assert config.api_credentials == {}
        assert config.scheduling["scheduling_link_enabled"] is False
        assert config.scheduling["scheduling_link_url"] is None

        user_count = await session.scalar(
            select(func.count(User.id)).where(User.tenant_id == expected_id)
        )
        assert user_count == 0
        documents_result = await session.execute(
            select(KnowledgeDocument)
            .where(KnowledgeDocument.tenant_id == expected_id)
            .order_by(KnowledgeDocument.title)
        )
        documents = list(documents_result.scalars().all())
        assert len(documents) == 2
        assert {document.content for document in documents} == {
            "KNOWLEDGE_SENTINEL",
            "second-doc!!!",
        }
        assert all(document.vector_id is None for document in documents)
        assert all(document.is_indexed is False for document in documents)
        assert all(document.chunk_count == 0 for document in documents)
        assert all(document.index_error is None for document in documents)
        assert all(
            document.size_bytes == len(document.content.encode("utf-8"))
            for document in documents
        )
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_post_recomputes_authoritative_name_after_stale_preview(
    async_client,
) -> None:
    graph = await _seed_clone_graph()
    preview = await _request_clone_contract(
        async_client, "preview", graph.source_id, graph.headers()
    )
    assert preview.json()["suggested_name"] == "Clínica Ocular cópia 1"

    session = get_db_session()
    try:
        competing_id = uuid4()
        session.add(
            Tenant(
                id=competing_id,
                name="Clínica Ocular cópia 1",
                slug=f"competing-clone-{competing_id.hex}",
                api_key_hash=f"competing-hash-{competing_id.hex}",
                is_active=False,
                plan_type="clinic",
                builder_id=graph.builder_id,
                config={},
            )
        )
        await session.commit()
    finally:
        await session.close()

    response = await _request_clone_contract(
        async_client, "success", graph.source_id, graph.headers()
    )
    assert response.status_code == 201
    assert response.json()["name"] == "Clínica Ocular cópia 2"

    session = get_db_session()
    try:
        config_result = await session.execute(
            select(ClinicConfigTable).where(
                ClinicConfigTable.tenant_id == UUID(response.json()["id"])
            )
        )
        assert (
            config_result.scalar_one().identity["clinic_name"]
            == response.json()["name"]
        )
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_name", ["   ", "x" * 255])
async def test_blank_and_over_limit_clone_names_share_redacted_409(
    async_client,
    invalid_name: str,
) -> None:
    graph = await _seed_clone_graph()
    session = get_db_session()
    try:
        source = await session.get(Tenant, graph.source_id)
        assert source is not None
        source.name = invalid_name
        await session.commit()
    finally:
        await session.close()

    for contract_key in ("preview", "success"):
        response = await _request_clone_contract(
            async_client, contract_key, graph.source_id, graph.headers()
        )
        assert response.status_code == _CONTRACT["errors"]["409"]["status"]
        assert response.json() == _CONTRACT["errors"]["409"]["body"]
        if invalid_name.strip():
            assert invalid_name.strip() not in response.text


@pytest.mark.asyncio
async def test_invalid_clone_config_returns_redacted_409_without_partial_tenant(
    async_client,
) -> None:
    graph = await _seed_clone_graph()
    session = get_db_session()
    try:
        config_result = await session.execute(
            select(ClinicConfigTable).where(
                ClinicConfigTable.tenant_id == graph.source_id
            )
        )
        config = config_result.scalar_one()
        config.operations = {
            "business_hours": {"mon": {"open": "18:00", "close": "09:00"}},
            "CONFIG_VALIDATION_SENTINEL": "must-not-leak",
        }
        count_before = await session.scalar(
            select(func.count(Tenant.id)).where(Tenant.builder_id == graph.builder_id)
        )
        await session.commit()
    finally:
        await session.close()

    response = await _request_clone_contract(
        async_client, "success", graph.source_id, graph.headers()
    )

    assert response.status_code == _CONTRACT["errors"]["409"]["status"]
    assert response.json() == _CONTRACT["errors"]["409"]["body"]
    assert "CONFIG_VALIDATION_SENTINEL" not in response.text
    session = get_db_session()
    try:
        count_after = await session.scalar(
            select(func.count(Tenant.id)).where(Tenant.builder_id == graph.builder_id)
        )
        assert count_after == count_before
    finally:
        await session.close()


class _FakeResult:
    def __init__(self, *, one: Any = None, many: list[Any] | None = None) -> None:
        self._one = one
        self._many = many or []

    def scalar_one_or_none(self) -> Any:
        return self._one

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list[Any]:
        return self._many


class _OrderedSession:
    def __init__(
        self,
        results: list[_FakeResult],
        *,
        flush_error: Exception | None = None,
        rollback_error: Exception | None = None,
    ) -> None:
        self.results = iter(results)
        self.executions: list[tuple[str, bool]] = []
        self.added: list[Any] = []
        self.flush_error = flush_error
        self.rollback_error = rollback_error
        self.committed = False
        self.rolled_back = False

    async def execute(self, statement) -> _FakeResult:
        entity = statement.column_descriptions[0].get("entity")
        entity_name = entity.__name__ if entity is not None else "unknown"
        self.executions.append(
            (entity_name, getattr(statement, "_for_update_arg", None) is not None)
        )
        return next(self.results)

    def add(self, value: Any) -> None:
        self.added.append(value)

    def add_all(self, values: list[Any]) -> None:
        self.added.extend(values)

    async def flush(self) -> None:
        if self.flush_error is not None:
            raise self.flush_error

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True
        if self.rollback_error is not None:
            raise self.rollback_error


def _ordered_session(
    *,
    flush_error: Exception | None = None,
    rollback_error: Exception | None = None,
) -> tuple[_OrderedSession, TenantContext, UUID]:
    builder_id = uuid4()
    auth_tenant_id = uuid4()
    user_id = uuid4()
    source_id = uuid4()
    user = User(
        id=user_id,
        tenant_id=auth_tenant_id,
        email="ordered-builder@test.local",
        password_hash="unused",
        role=UserRole.builder,
        builder_id=builder_id,
        is_active=True,
    )
    builder = Builder(id=builder_id, name="Ordered", status="active", is_active=True)
    source = Tenant(
        id=source_id,
        name="Ordered Clinic",
        slug="ordered-clinic",
        api_key_hash="unused-hash",
        is_active=True,
        builder_id=builder_id,
        config={},
    )
    config = ClinicConfigTable(
        tenant_id=source_id,
        persona={},
        scheduling={},
        pricing={},
        post_care={},
        handoff={},
        identity={},
        operations={},
        business_rules={},
        lgpd={},
        professionals=[],
        services=[],
        api_credentials={},
    )
    fake = _OrderedSession(
        [
            _FakeResult(one=user),
            _FakeResult(one=builder),
            _FakeResult(one=source),
            _FakeResult(many=[source.name]),
            _FakeResult(one=config),
            _FakeResult(many=[]),
        ],
        flush_error=flush_error,
        rollback_error=rollback_error,
    )
    context = TenantContext(
        tenant_id=auth_tenant_id,
        user_id=user_id,
        role="builder",
        builder_id=builder_id,
    )
    return fake, context, source_id


@pytest.mark.asyncio
async def test_post_locks_user_builder_source_before_clone_reads(monkeypatch) -> None:
    from src.api.routes import builder as builder_routes

    session, context, source_id = _ordered_session()
    monkeypatch.setattr(builder_routes.pwd_context, "hash", lambda _secret: "safe-hash")

    response = await builder_routes.clone_clinic(
        str(source_id),
        context,
        session,
    )

    assert response.is_active is False
    assert session.executions[:3] == [
        ("User", True),
        ("Builder", True),
        ("Tenant", True),
    ]
    assert session.executions[3:] == [
        ("Tenant", False),
        ("ClinicConfigTable", False),
        ("KnowledgeDocument", False),
    ]
    assert session.committed is True
    assert session.rolled_back is False


@pytest.mark.asyncio
async def test_injected_persistence_failure_rolls_back_once(monkeypatch) -> None:
    from src.api.routes import builder as builder_routes

    session, context, source_id = _ordered_session(
        flush_error=RuntimeError("injected persistence failure")
    )
    monkeypatch.setattr(builder_routes.pwd_context, "hash", lambda _secret: "safe-hash")

    with pytest.raises(RuntimeError, match="injected persistence failure"):
        await builder_routes.clone_clinic(
            str(source_id),
            context,
            session,
        )

    assert session.rolled_back is True
    assert session.committed is False


@pytest.mark.asyncio
async def test_sqlalchemy_failure_and_rollback_failure_stay_sanitized(
    monkeypatch,
) -> None:
    from src.api.routes import builder as builder_routes

    sentinel = "PERSISTENCE_AND_ROLLBACK_SENTINEL"
    session, context, source_id = _ordered_session(
        flush_error=StatementError(
            "insert failed",
            "INSERT INTO tenant VALUES (:config)",
            {"config": sentinel},
            RuntimeError("driver failure"),
        ),
        rollback_error=RuntimeError(sentinel),
    )
    monkeypatch.setattr(builder_routes.pwd_context, "hash", lambda _secret: "safe-hash")

    with pytest.raises(HTTPException) as raised:
        await builder_routes.clone_clinic(str(source_id), context, session)

    assert raised.value.status_code == _CONTRACT["errors"]["500"]["status"]
    assert {"detail": raised.value.detail} == _CONTRACT["errors"]["500"]["body"]
    assert raised.value.__suppress_context__ is True
    assert sentinel not in str(raised.value)
    assert session.rolled_back is True
    assert session.committed is False


@pytest.mark.asyncio
async def test_request_sqlalchemy_failure_is_redacted_and_atomic(
    async_client,
    monkeypatch,
    caplog,
) -> None:
    assert get_db_engine().sync_engine.hide_parameters is True
    graph = await _seed_clone_graph()
    session = get_db_session()
    try:
        count_before = await session.scalar(
            select(func.count(Tenant.id)).where(Tenant.builder_id == graph.builder_id)
        )
    finally:
        await session.close()

    sentinel = "SQL_PARAMETER_SENTINEL_MUST_NOT_LEAK"
    rollback_calls = 0
    original_rollback = AsyncSession.rollback

    async def fail_flush(_session, *_args, **_kwargs) -> None:
        raise StatementError(
            "insert failed",
            "INSERT INTO tenant VALUES (:config)",
            {"config": sentinel},
            RuntimeError("driver failure"),
        )

    async def track_rollback(session, *_args, **_kwargs) -> None:
        nonlocal rollback_calls
        rollback_calls += 1
        await original_rollback(session)

    monkeypatch.setattr(AsyncSession, "flush", fail_flush)
    monkeypatch.setattr(AsyncSession, "rollback", track_rollback)

    response = await _request_clone_contract(
        async_client, "success", graph.source_id, graph.headers()
    )

    assert response.status_code == _CONTRACT["errors"]["500"]["status"]
    assert response.json() == _CONTRACT["errors"]["500"]["body"]
    assert rollback_calls == 1
    assert sentinel not in response.text + caplog.text

    session = get_db_session()
    try:
        count_after = await session.scalar(
            select(func.count(Tenant.id)).where(Tenant.builder_id == graph.builder_id)
        )
        assert count_after == count_before
    finally:
        await session.close()


def test_clone_openapi_is_bearer_only_and_documents_redacted_errors(test_app) -> None:
    test_app.openapi_schema = None
    schema = test_app.openapi()

    for contract_key in ("preview", "success"):
        endpoint = _CONTRACT[contract_key]
        operation = schema["paths"][endpoint["path"]][endpoint["method"].lower()]
        assert operation["security"] == [{"BearerAuth": []}]
        assert all(
            parameter.get("name") != "X-API-Key"
            for parameter in operation.get("parameters", [])
        )
        for status_code in ("403", "404", "409"):
            error_schema = operation["responses"][status_code]["content"][
                "application/json"
            ]["schema"]
            assert error_schema["$ref"].endswith("/ClinicCloneErrorResponse")


@pytest.mark.asyncio
async def test_neighboring_builder_routes_remain_available(async_client) -> None:
    graph = await _seed_clone_graph()

    profile = await async_client.get("/v1/builder/me", headers=graph.headers())
    clinics = await async_client.get("/v1/builder/clinics", headers=graph.headers())

    assert profile.status_code == 200
    assert clinics.status_code == 200
    assert any(clinic["id"] == str(graph.source_id) for clinic in clinics.json())
