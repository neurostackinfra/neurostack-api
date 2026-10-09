"""Contract and transaction tests for Builder-scoped clinic cloning."""

import gc
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, event, func, select
from sqlalchemy.exc import StatementError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.selectable import ForUpdateArg

from src.api.middleware.auth import TenantContext
from src.api.routes import builder as builder_routes
from src.api.schemas.builder import (
    ClinicCloneErrorResponse,
    ClinicClonePreviewResponse,
    ClinicCloneRequest,
    ClinicCloneResponse,
)
from src.config.settings import settings
from src.models.builder import Builder
from src.models.clinic_config_table import ClinicConfigTable
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset
from src.models.tenant import Tenant
from src.models.user import User, UserRole
from src.repositories import get_db_engine, get_db_session
from src.services.clinic_clone_service import MEDIA_ASSET_CLONE_FIELDS
from src.services.media_assets import (
    MediaStorageCapacityExceeded,
    MediaStorageGuardError,
    media_payload_sha256,
)

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "clinic_clone_contract.json"
)
_CONTRACT = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))
_MEDIA_PAYLOADS = (
    b"%PDF-1.7\nMEDIA_PAYLOAD_SENTINEL_1",
    b"%PDF-1.7\nsecond-media",
    b"%PDF-1.7\nthird",
)
_CONTRACT_REQUEST = object()
_ABSENT = object()


def _clone_path(contract_key: str, source_id: UUID) -> str:
    return _CONTRACT[contract_key]["path"].format(source_id=source_id)


async def _request_clone_contract(
    client,
    contract_key: str,
    source_id: UUID,
    headers: dict[str, str],
    *,
    body: Any = _CONTRACT_REQUEST,
):
    """Send a contract request; POST carries the fixture request by default.

    ``body`` may be ``_ABSENT`` (no body bytes at all) or raw bytes sent as-is.
    """
    endpoint = _CONTRACT[contract_key]
    options: dict[str, Any] = {"headers": dict(headers)}
    if body is _CONTRACT_REQUEST:
        if "request" in endpoint:
            options["json"] = endpoint["request"]
    elif body is not _ABSENT:
        options["content"] = body
        options["headers"]["Content-Type"] = "application/json"
    return await client.request(
        endpoint["method"],
        endpoint["path"].format(source_id=source_id),
        **options,
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


def _source_media(source_id: UUID) -> list[MediaAsset]:
    return [
        MediaAsset(
            tenant_id=source_id,
            display_name=f"MEDIA_DISPLAY_SENTINEL {index}",
            original_filename=f"MEDIA_FILENAME_SENTINEL_{index}.pdf",
            category="pdf",
            mime_type="application/pdf",
            size_bytes=len(payload),
            sha256=media_payload_sha256(payload),
            payload=payload,
            usage_trigger=f"MEDIA_TRIGGER_SENTINEL {index}" if index == 0 else None,
            auto_suggest=index == 0,
        )
        for index, payload in enumerate(_MEDIA_PAYLOADS)
    ]


async def _seed_clone_graph(
    *,
    existing_copy: bool = False,
    with_media: bool = True,
) -> _CloneGraph:
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
        if with_media:
            session.add_all(_source_media(source_id))
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


async def _clone_footprint(graph: _CloneGraph) -> tuple[int, int, int, int]:
    """Count tenants/configs/documents/media under the builder except the source."""
    clone_ids = select(Tenant.id).where(
        Tenant.builder_id == graph.builder_id,
        Tenant.id != graph.source_id,
    )
    session = get_db_session()
    try:
        return (
            await session.scalar(
                select(func.count(Tenant.id)).where(Tenant.id.in_(clone_ids))
            ),
            await session.scalar(
                select(func.count(ClinicConfigTable.id)).where(
                    ClinicConfigTable.tenant_id.in_(clone_ids)
                )
            ),
            await session.scalar(
                select(func.count(KnowledgeDocument.id)).where(
                    KnowledgeDocument.tenant_id.in_(clone_ids)
                )
            ),
            await session.scalar(
                select(func.count(MediaAsset.id)).where(
                    MediaAsset.tenant_id.in_(clone_ids)
                )
            ),
        )
    finally:
        await session.close()


@dataclass
class _GuardRecorder:
    """SQLite stand-in for the media storage guard.

    SQLite cannot run ``pg_advisory_xact_lock``; the real guard, its advisory
    key and its lock order are proven in
    ``tests/integration/test_builder_cloning_postgres.py``. This recorder keeps
    the same two-phase protocol so the route's use of it is observable here.
    """

    calls: list[tuple[Any, UUID, UUID | None]] = field(default_factory=list)
    events: list[Any] = field(default_factory=list)
    cap_exceeded: bool = False
    enter_error: BaseException | None = None

    def __call__(
        self,
        session: AsyncSession,
        actor_user: Any,
        subject_tenant: Tenant,
    ) -> "_RecordingGuard":
        self.calls.append((actor_user, subject_tenant.id, subject_tenant.builder_id))
        return _RecordingGuard(self, session)


class _RecordingGuard:
    def __init__(self, recorder: _GuardRecorder, session: Any) -> None:
        self.recorder = recorder
        self.session = session

    async def __aenter__(self) -> "_RecordingGuard":
        self.recorder.events.append("guard_enter")
        if self.recorder.enter_error is not None:
            await self.session.rollback()
            raise self.recorder.enter_error
        return self

    async def validate_projection(self, additional_bytes: int) -> None:
        self.recorder.events.append(("validate_projection", additional_bytes))
        if any(isinstance(item, MediaAsset) for item in self.session.new):
            raise AssertionError("media insert preceded capacity validation")
        if self.recorder.cap_exceeded:
            raise MediaStorageCapacityExceeded("projection exceeds the cap")

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.recorder.events.append("guard_exit")
        if exc_type is not None:
            await self.session.rollback()
        return False


@pytest.fixture(autouse=True)
def capacity_guard(monkeypatch: pytest.MonkeyPatch) -> _GuardRecorder:
    recorder = _GuardRecorder()
    monkeypatch.setattr(builder_routes, "media_storage_capacity_guard", recorder)
    return recorder


def test_shared_contract_fixture_matches_strict_response_schemas() -> None:
    preview = ClinicClonePreviewResponse.model_validate_json(
        json.dumps(_CONTRACT["preview"]["body"])
    )
    request = ClinicCloneRequest.model_validate_json(
        json.dumps(_CONTRACT["success"]["request"])
    )
    success = ClinicCloneResponse.model_validate_json(
        json.dumps(_CONTRACT["success"]["body"])
    )

    assert preview.model_dump(mode="json") == _CONTRACT["preview"]["body"]
    assert request.model_dump(mode="json") == _CONTRACT["success"]["request"]
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
            {**_CONTRACT["success"]["body"], "is_active": True}
        )
    with pytest.raises(ValidationError):
        ClinicCloneResponse.model_validate(
            {**_CONTRACT["success"]["body"], "copied_media_assets": -1}
        )


def test_clone_request_schema_is_strict_with_false_defaults() -> None:
    assert ClinicCloneRequest().model_dump() == {
        "include_rag": False,
        "include_media": False,
    }
    assert ClinicCloneRequest.model_validate_json("{}") == ClinicCloneRequest()
    for invalid in (
        "null",
        "[]",
        '"x"',
        "1",
        '{"include_config": true}',
        '{"include_rag": "true"}',
        '{"include_media": 1}',
    ):
        with pytest.raises(ValidationError):
            ClinicCloneRequest.model_validate_json(invalid)


class _RawBodyRequest:
    """Minimal request double: headers plus a chunked body stream."""

    def __init__(
        self,
        raw_body: bytes,
        *,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.headers = headers or {}
        self.chunks = [raw_body] if chunks is None else chunks
        self.chunks_read = 0

    async def stream(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.chunks_read += 1
            yield chunk
        # Starlette ends every body stream with an empty chunk.
        yield b""


def _padded_json(document: str, size: int) -> bytes:
    """Valid JSON padded with trailing whitespace to exactly ``size`` bytes."""
    encoded = document.encode()
    return encoded + b" " * (size - len(encoded))


@pytest.mark.asyncio
async def test_declared_oversized_body_is_rejected_without_reading() -> None:
    limit = builder_routes._CLONE_REQUEST_MAX_BYTES
    for declared in (str(limit + 1), "not-a-number"):
        request = _RawBodyRequest(
            b"{}", headers={"content-length": declared}, chunks=[b"{}"]
        )
        with pytest.raises(HTTPException) as raised:
            await builder_routes._clone_request_body(request)  # type: ignore[arg-type]
        assert raised.value.status_code == _CONTRACT["errors"]["422"]["status"]
        assert {"detail": raised.value.detail} == _CONTRACT["errors"]["422"]["body"]
        assert request.chunks_read == 0


@pytest.mark.asyncio
async def test_streamed_oversized_body_is_abandoned_once_over_the_limit() -> None:
    limit = builder_routes._CLONE_REQUEST_MAX_BYTES
    request = _RawBodyRequest(b"", chunks=[b" " * 600] * 10)

    with pytest.raises(HTTPException) as raised:
        await builder_routes._clone_request_body(request)  # type: ignore[arg-type]

    assert raised.value.status_code == _CONTRACT["errors"]["422"]["status"]
    assert {"detail": raised.value.detail} == _CONTRACT["errors"]["422"]["body"]
    # 600 + 600 > limit: the third chunk is never pulled.
    assert 600 * 2 > limit
    assert request.chunks_read == 2


@pytest.mark.asyncio
async def test_body_size_boundary_is_inclusive_of_the_limit() -> None:
    limit = builder_routes._CLONE_REQUEST_MAX_BYTES
    at_limit = _padded_json('{"include_rag": true}', limit)
    over_limit = _padded_json('{"include_rag": true}', limit + 1)

    for headers in ({}, {"content-length": str(limit)}):
        parsed = await builder_routes._clone_request_body(
            _RawBodyRequest(at_limit, headers=headers)  # type: ignore[arg-type]
        )
        assert parsed == ClinicCloneRequest(include_rag=True)
    with pytest.raises(HTTPException) as raised:
        await builder_routes._clone_request_body(
            _RawBodyRequest(over_limit)  # type: ignore[arg-type]
        )
    assert raised.value.status_code == _CONTRACT["errors"]["422"]["status"]
    # Zero bytes, declared or not, is still an absent body.
    for headers in ({}, {"content-length": "0"}):
        absent = await builder_routes._clone_request_body(
            _RawBodyRequest(b"", headers=headers)  # type: ignore[arg-type]
        )
        assert absent == ClinicCloneRequest()


@pytest.mark.asyncio
async def test_absent_body_is_detected_before_any_model_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_parse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("an absent body must not reach model validation")

    monkeypatch.setattr(ClinicCloneRequest, "model_validate_json", forbidden_parse)

    request = await builder_routes._clone_request_body(
        _RawBodyRequest(b"")  # type: ignore[arg-type]
    )

    assert request == ClinicCloneRequest()
    with pytest.raises(AssertionError, match="absent body"):
        await builder_routes._clone_request_body(
            _RawBodyRequest(b"null")  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_preview_returns_only_fixture_contract_after_current_authority(
    async_client,
    capacity_guard: _GuardRecorder,
) -> None:
    graph = await _seed_clone_graph(existing_copy=True)

    response = await _request_clone_contract(
        async_client, "preview", graph.source_id, graph.headers()
    )

    assert response.status_code == _CONTRACT["preview"]["status"]
    assert response.json() == _CONTRACT["preview"]["body"]
    assert sum(len(payload) for payload in _MEDIA_PAYLOADS) == (
        _CONTRACT["preview"]["body"]["media_total_bytes"]
    )
    assert capacity_guard.calls == []


@pytest.mark.asyncio
async def test_preview_aggregates_are_zero_safe_and_never_select_content(
    async_client,
) -> None:
    graph = await _seed_clone_graph()
    empty_graph = await _seed_clone_graph(with_media=False)
    session = get_db_session()
    try:
        await session.execute(
            delete(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == empty_graph.source_id
            )
        )
        await session.commit()
    finally:
        await session.close()

    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(" ".join(statement.lower().split()))

    engine = get_db_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        populated = await _request_clone_contract(
            async_client, "preview", graph.source_id, graph.headers()
        )
        empty = await _request_clone_contract(
            async_client, "preview", empty_graph.source_id, empty_graph.headers()
        )
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert populated.json()["document_count"] == 2
    assert populated.json()["total_bytes"] == len(b"KNOWLEDGE_SENTINEL") + len(
        b"second-doc!!!"
    )
    assert populated.json()["media_asset_count"] == len(_MEDIA_PAYLOADS)
    assert empty.status_code == 200
    assert {
        key: value for key, value in empty.json().items() if key != "suggested_name"
    } == {
        "document_count": 0,
        "total_bytes": 0,
        "media_asset_count": 0,
        "media_total_bytes": 0,
    }
    aggregate_sql = [
        sql
        for sql in statements
        if "from knowledge_documents" in sql or "from media_assets" in sql
    ]
    assert len(aggregate_sql) == 4
    assert all("coalesce(" in sql for sql in aggregate_sql)
    for sql in aggregate_sql:
        assert "payload" not in sql
        assert "knowledge_documents.content" not in sql.replace(
            "octet_length(knowledge_documents.content)", ""
        )
    assert not any("media_assets.payload" in sql for sql in statements)
    assert not any("pg_advisory" in sql for sql in statements)


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
    capacity_guard: _GuardRecorder,
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
    assert capacity_guard.calls == []


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
    capacity_guard: _GuardRecorder,
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
        for body in (_CONTRACT_REQUEST, _ABSENT):
            response = await _request_clone_contract(
                async_client,
                contract_key,
                requested_source_id,
                graph.headers(),
                body=body,
            )
            assert response.status_code == _CONTRACT["errors"]["404"]["status"]
            assert response.json() == _CONTRACT["errors"]["404"]["body"]
    assert capacity_guard.calls == []


@pytest.mark.asyncio
async def test_post_persists_atomic_redacted_reset_clone(
    async_client,
    monkeypatch,
    caplog,
    capacity_guard: _GuardRecorder,
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
    assert capacity_guard.calls == [(graph.user_id, graph.source_id, graph.builder_id)]
    exposed = response.text + caplog.text
    for sentinel in (
        "PLAINTEXT_CREDENTIAL_SENTINEL",
        "CREDENTIAL_SENTINEL",
        "CONFIG_SENTINEL",
        "KNOWLEDGE_SENTINEL",
        "METADATA_SENTINEL",
        "SOURCE_VECTOR_SENTINEL",
        "MEDIA_PAYLOAD_SENTINEL",
        "MEDIA_FILENAME_SENTINEL",
        "MEDIA_TRIGGER_SENTINEL",
        "MEDIA_DISPLAY_SENTINEL",
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
        media_count = await session.scalar(
            select(func.count(MediaAsset.id)).where(MediaAsset.tenant_id == expected_id)
        )
        assert media_count == _CONTRACT["success"]["body"]["copied_media_assets"]
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        _ABSENT,
        b"",
        b"{}",
        b'{"include_rag": false, "include_media": false}',
        b'{"include_rag": false}',
        b'{"include_media": false}',
    ],
    ids=[
        "no-body",
        "zero-bytes",
        "empty-object",
        "both-false",
        "rag-false",
        "media-false",
    ],
)
async def test_absent_empty_or_false_body_creates_configuration_only_clone(
    async_client,
    capacity_guard: _GuardRecorder,
    body: Any,
) -> None:
    """Regression: a bodyless POST no longer copies RAG (or media) by default."""
    graph = await _seed_clone_graph()

    response = await _request_clone_contract(
        async_client, "success", graph.source_id, graph.headers(), body=body
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["is_active"] is False
    assert payload["copied_rag_documents"] == 0
    assert payload["copied_media_assets"] == 0
    assert await _clone_footprint(graph) == (1, 1, 0, 0)
    assert capacity_guard.calls == []
    session = get_db_session()
    try:
        config = (
            await session.execute(
                select(ClinicConfigTable).where(
                    ClinicConfigTable.tenant_id == UUID(payload["id"])
                )
            )
        ).scalar_one()
        assert config.identity["clinic_name"] == payload["name"]
        assert config.identity["clinic_history"] == "CONFIG_SENTINEL"
        assert config.persona["agent_display_name"] == "Lia"
        assert config.api_credentials == {}
        assert config.scheduling["scheduling_link_enabled"] is False
        assert config.scheduling["scheduling_link_url"] is None
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        b"null",
        b'"include_rag"',
        b"1",
        b"true",
        b"false",
        b"[]",
        b'[{"include_rag": true}]',
        b'{"unexpected": true}',
        b'{"include_config": false}',
        b'{"include_rag": true, "config": {"clinic_name": "CONFIG_SENTINEL"}}',
        b'{"include_rag": "true"}',
        b'{"include_rag": 1}',
        b'{"include_rag": null}',
        b'{"include_media": 0}',
        b'{"include_media": "false"}',
        b'{"include_media": null}',
        b"{",
        b" ",
    ],
    ids=[
        "null",
        "string",
        "number",
        "true",
        "false",
        "array",
        "array-of-object",
        "extra-field",
        "include-config",
        "config-control",
        "rag-string",
        "rag-number",
        "rag-null",
        "media-number",
        "media-string",
        "media-null",
        "malformed",
        "whitespace",
    ],
)
async def test_present_invalid_json_body_returns_same_redacted_422_without_records(
    async_client,
    capacity_guard: _GuardRecorder,
    body: bytes,
) -> None:
    graph = await _seed_clone_graph()

    response = await _request_clone_contract(
        async_client, "success", graph.source_id, graph.headers(), body=body
    )

    assert response.status_code == _CONTRACT["errors"]["422"]["status"]
    assert response.json() == _CONTRACT["errors"]["422"]["body"]
    assert "CONFIG_SENTINEL" not in response.text
    assert await _clone_footprint(graph) == (0, 0, 0, 0)
    assert capacity_guard.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("framing", ["content-length", "chunked"])
async def test_oversized_body_returns_same_422_and_never_attempts_the_clone(
    async_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    capacity_guard: _GuardRecorder,
    framing: str,
) -> None:
    graph = await _seed_clone_graph()

    async def forbidden_clone(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("an oversized body must not reach the clone")

    monkeypatch.setattr(builder_routes, "_current_clone_builder", forbidden_clone)
    oversized = _padded_json("{}", 4 * builder_routes._CLONE_REQUEST_MAX_BYTES)

    async def chunked_body() -> AsyncIterator[bytes]:
        for start in range(0, len(oversized), 512):
            yield oversized[start : start + 512]

    body: Any = oversized if framing == "content-length" else chunked_body()
    response = await async_client.post(
        _clone_path("success", graph.source_id),
        headers={**graph.headers(), "Content-Type": "application/json"},
        content=body,
    )

    assert response.request.headers.get("content-length") == (
        str(len(oversized)) if framing == "content-length" else None
    )
    assert response.status_code == _CONTRACT["errors"]["422"]["status"]
    assert response.json() == _CONTRACT["errors"]["422"]["body"]
    assert await _clone_footprint(graph) == (0, 0, 0, 0)
    assert capacity_guard.calls == []


@pytest.mark.asyncio
async def test_body_of_exactly_the_size_limit_is_still_accepted(
    async_client: Any,
) -> None:
    graph = await _seed_clone_graph()
    body = _padded_json(
        '{"include_rag": true, "include_media": false}',
        builder_routes._CLONE_REQUEST_MAX_BYTES,
    )

    response = await _request_clone_contract(
        async_client, "success", graph.source_id, graph.headers(), body=body
    )

    assert response.status_code == 201
    assert response.json()["copied_rag_documents"] == 2
    assert response.json()["copied_media_assets"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("include_rag", "include_media"),
    [(False, False), (True, False), (False, True), (True, True)],
    ids=["config-only", "rag-only", "media-only", "rag-and-media"],
)
async def test_each_flag_combination_copies_exactly_the_selected_sets(
    async_client,
    capacity_guard: _GuardRecorder,
    include_rag: bool,
    include_media: bool,
) -> None:
    graph = await _seed_clone_graph()
    body = json.dumps(
        {"include_rag": include_rag, "include_media": include_media}
    ).encode()

    response = await _request_clone_contract(
        async_client, "success", graph.source_id, graph.headers(), body=body
    )

    assert response.status_code == 201
    payload = response.json()
    expected_documents = 2 if include_rag else 0
    expected_media = len(_MEDIA_PAYLOADS) if include_media else 0
    assert payload["copied_rag_documents"] == expected_documents
    assert payload["copied_media_assets"] == expected_media
    assert await _clone_footprint(graph) == (1, 1, expected_documents, expected_media)
    if include_media:
        assert capacity_guard.calls == [
            (graph.user_id, graph.source_id, graph.builder_id)
        ]
        assert capacity_guard.events == [
            "guard_enter",
            ("validate_projection", sum(len(item) for item in _MEDIA_PAYLOADS)),
            "guard_exit",
        ]
    else:
        assert capacity_guard.calls == []

    clone_id = UUID(payload["id"])
    session = get_db_session()
    try:
        source_media = list(
            (
                await session.execute(
                    select(MediaAsset)
                    .where(MediaAsset.tenant_id == graph.source_id)
                    .order_by(MediaAsset.display_name)
                )
            )
            .scalars()
            .all()
        )
        cloned_media = list(
            (
                await session.execute(
                    select(MediaAsset)
                    .where(MediaAsset.tenant_id == clone_id)
                    .order_by(MediaAsset.display_name)
                )
            )
            .scalars()
            .all()
        )
        assert len(source_media) == len(_MEDIA_PAYLOADS)
        if include_media:
            for source, copied in zip(source_media, cloned_media, strict=True):
                assert copied.id != source.id
                assert copied.tenant_id == clone_id
                for column in MEDIA_ASSET_CLONE_FIELDS:
                    assert getattr(copied, column) == getattr(source, column)
        else:
            assert cloned_media == []
    finally:
        await session.close()


@pytest.mark.asyncio
async def test_media_cap_exceeded_returns_409_without_any_record(
    async_client,
    capacity_guard: _GuardRecorder,
) -> None:
    graph = await _seed_clone_graph()
    capacity_guard.cap_exceeded = True

    response = await _request_clone_contract(
        async_client, "success", graph.source_id, graph.headers()
    )

    error = _CONTRACT["errors"]["409_media_storage_cap"]
    assert response.status_code == error["status"]
    assert response.json() == error["body"]
    assert capacity_guard.events == [
        "guard_enter",
        ("validate_projection", sum(len(item) for item in _MEDIA_PAYLOADS)),
        "guard_exit",
    ]
    assert await _clone_footprint(graph) == (0, 0, 0, 0)


@pytest.mark.asyncio
async def test_media_guard_ownership_change_is_the_same_non_enumerable_404(
    async_client,
    capacity_guard: _GuardRecorder,
) -> None:
    graph = await _seed_clone_graph()
    capacity_guard.enter_error = MediaStorageGuardError(
        "storage owner changed during locking"
    )

    response = await _request_clone_contract(
        async_client, "success", graph.source_id, graph.headers()
    )

    assert response.status_code == _CONTRACT["errors"]["404"]["status"]
    assert response.json() == _CONTRACT["errors"]["404"]["body"]
    assert "owner" not in response.text
    assert await _clone_footprint(graph) == (0, 0, 0, 0)


class _LogRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event_name: str, **fields: Any) -> None:
        self.events.append((event_name, fields))

    def warning(self, event_name: str, **fields: Any) -> None:
        self.events.append((event_name, fields))


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["validate_projection", "guard_exit"])
async def test_guard_error_after_entry_is_a_logged_redacted_500(
    async_client: Any,
    monkeypatch: pytest.MonkeyPatch,
    capacity_guard: _GuardRecorder,
    phase: str,
) -> None:
    graph = await _seed_clone_graph()
    sentinel = "GUARD_MISUSE_SENTINEL"
    recorder = _LogRecorder()
    monkeypatch.setattr(builder_routes, "logger", recorder)

    if phase == "validate_projection":

        async def misuse(_guard: Any, _additional_bytes: int) -> None:
            raise MediaStorageGuardError(sentinel)

        monkeypatch.setattr(_RecordingGuard, "validate_projection", misuse)
    else:
        original_exit = _RecordingGuard.__aexit__

        async def misuse_exit(guard: Any, exc_type: Any, exc: Any, tb: Any) -> bool:
            await original_exit(guard, exc_type, exc, tb)
            if exc_type is None:
                await guard.session.rollback()
                raise MediaStorageGuardError(sentinel)
            return False

        monkeypatch.setattr(_RecordingGuard, "__aexit__", misuse_exit)

    response = await _request_clone_contract(
        async_client, "success", graph.source_id, graph.headers()
    )

    assert response.status_code == _CONTRACT["errors"]["500"]["status"]
    assert response.json() == _CONTRACT["errors"]["500"]["body"]
    assert sentinel not in response.text
    assert await _clone_footprint(graph) == (0, 0, 0, 0)
    assert recorder.events == [
        (
            "clinic_clone_failed",
            {
                "reason": "guard_unexpected_error",
                "builder_id": str(graph.builder_id),
                "source_tenant_id": str(graph.source_id),
            },
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("include_rag", "include_media"),
    [(False, False), (True, True)],
    ids=["config-only", "rag-and-media"],
)
async def test_clone_log_contains_only_ids_flags_and_final_counts(
    async_client,
    monkeypatch: pytest.MonkeyPatch,
    include_rag: bool,
    include_media: bool,
) -> None:
    graph = await _seed_clone_graph()
    recorder = _LogRecorder()
    monkeypatch.setattr(builder_routes, "logger", recorder)
    body = json.dumps(
        {"include_rag": include_rag, "include_media": include_media}
    ).encode()

    response = await _request_clone_contract(
        async_client, "success", graph.source_id, graph.headers(), body=body
    )

    assert response.status_code == 201
    assert recorder.events == [
        (
            "clinic_clone_created",
            {
                "builder_id": str(graph.builder_id),
                "source_tenant_id": str(graph.source_id),
                "clone_tenant_id": response.json()["id"],
                "include_rag": include_rag,
                "include_media": include_media,
                "copied_rag_documents": response.json()["copied_rag_documents"],
                "copied_media_assets": response.json()["copied_media_assets"],
            },
        )
    ]


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
    assert await _clone_footprint(graph) == (0, 0, 0, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [_ABSENT, _CONTRACT_REQUEST], ids=["config", "all"])
async def test_invalid_clone_config_returns_redacted_409_without_partial_tenant(
    async_client,
    body: Any,
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
        await session.commit()
    finally:
        await session.close()

    response = await _request_clone_contract(
        async_client, "success", graph.source_id, graph.headers(), body=body
    )

    assert response.status_code == _CONTRACT["errors"]["409"]["status"]
    assert response.json() == _CONTRACT["errors"]["409"]["body"]
    assert "CONFIG_VALIDATION_SENTINEL" not in response.text
    assert await _clone_footprint(graph) == (0, 0, 0, 0)


class _FakeResult:
    def __init__(self, *, one: Any = None, many: list[Any] | None = None) -> None:
        self._one = one
        self._many = many or []

    def scalar_one_or_none(self) -> Any:
        return self._one

    def one_or_none(self) -> Any:
        return self._one

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list[Any]:
        return self._many


@dataclass(frozen=True)
class _FakeMediaRow:
    id: UUID
    display_name: str
    original_filename: str
    category: str
    mime_type: str
    size_bytes: int
    sha256: bytes
    payload: bytes
    usage_trigger: str | None
    auto_suggest: bool

    def _asdict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in MEDIA_ASSET_CLONE_FIELDS}


@dataclass(frozen=True)
class _StabilizedRow:
    id: UUID
    size_bytes: int


def _fake_media_row(payload: bytes) -> _FakeMediaRow:
    return _FakeMediaRow(
        id=uuid4(),
        display_name="Ordered asset",
        original_filename="ordered.pdf",
        category="pdf",
        mime_type="application/pdf",
        size_bytes=len(payload),
        sha256=media_payload_sha256(payload),
        payload=payload,
        usage_trigger=None,
        auto_suggest=False,
    )


def _lock_mode(statement: Any) -> str | None:
    for_update: ForUpdateArg | None = getattr(statement, "_for_update_arg", None)
    if for_update is None:
        return None
    return "FOR SHARE" if for_update.read else "FOR UPDATE"


class _OrderedSession:
    def __init__(
        self,
        results: list[_FakeResult],
        *,
        flush_error: Exception | None = None,
        rollback_error: Exception | None = None,
    ) -> None:
        self.results = iter(results)
        self.executions: list[Any] = []
        self.statements: list[tuple[Any, _FakeResult]] = []
        self.added: list[Any] = []
        self.flush_error = flush_error
        self.rollback_error = rollback_error
        self.committed = False
        self.rolled_back = False

    @property
    def new(self) -> list[Any]:
        return list(self.added)

    async def execute(self, statement) -> _FakeResult:
        entity = statement.column_descriptions[0].get("entity")
        entity_name = entity.__name__ if entity is not None else "unknown"
        self.executions.append((entity_name, _lock_mode(statement)))
        result = next(self.results)
        self.statements.append((statement, result))
        return result

    def add(self, value: Any) -> None:
        self.added.append(value)

    def add_all(self, values: list[Any]) -> None:
        self.added.extend(values)

    def expunge(self, value: Any) -> None:
        self.executions.append(("expunge", type(value).__name__))

    async def flush(self) -> None:
        self.executions.append("flush")
        if self.flush_error is not None:
            raise self.flush_error
        self.added.clear()

    async def commit(self) -> None:
        self.executions.append("commit")
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True
        if self.rollback_error is not None:
            raise self.rollback_error


class _SequencedGuard:
    """Write guard phases into the ordered session's execution log."""

    def __init__(self, session: _OrderedSession, *, cap_exceeded: bool = False):
        self.session = session
        self.cap_exceeded = cap_exceeded
        self.subjects: list[UUID] = []

    def __call__(self, session: Any, actor_user: Any, subject: Tenant) -> Any:
        assert session is self.session
        self.subjects.append(subject.id)
        return self

    async def __aenter__(self) -> "_SequencedGuard":
        self.session.executions.append("guard_enter")
        return self

    async def validate_projection(self, additional_bytes: int) -> None:
        self.session.executions.append(("validate_projection", additional_bytes))
        if self.cap_exceeded:
            raise MediaStorageCapacityExceeded("projection exceeds the cap")

    async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.session.executions.append("guard_exit")
        return False


def _ordered_session(
    *,
    include_rag: bool = False,
    include_media: bool = False,
    media_rows: tuple[_FakeMediaRow | None, ...] = (),
    stabilized_sizes: tuple[int, ...] | None = None,
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
    results = [_FakeResult(one=user), _FakeResult(one=builder)]
    if include_media:
        sizes = stabilized_sizes or tuple(
            row.size_bytes for row in media_rows if row is not None
        )
        results += [
            # Unlocked owner resolution, locked re-read, then the FOR SHARE set.
            _FakeResult(one=source),
            _FakeResult(one=source),
            _FakeResult(many=[_StabilizedRow(uuid4(), size) for size in sizes]),
        ]
    else:
        results.append(_FakeResult(one=source))
    results += [_FakeResult(many=[source.name]), _FakeResult(one=config)]
    if include_rag:
        results.append(_FakeResult(many=[]))
    results += [_FakeResult(one=row) for row in media_rows]
    fake = _OrderedSession(
        results,
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


async def _call_clone(
    session: _OrderedSession,
    context: TenantContext,
    source_id: UUID,
    *,
    include_rag: bool = False,
    include_media: bool = False,
) -> ClinicCloneResponse:
    return await builder_routes.clone_clinic(
        source_id=str(source_id),
        clone_request=ClinicCloneRequest(
            include_rag=include_rag, include_media=include_media
        ),
        tenant=context,
        session=session,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("include_rag", [False, True], ids=["config-only", "rag-only"])
async def test_post_locks_user_builder_source_before_clone_reads(
    monkeypatch,
    include_rag: bool,
) -> None:
    session, context, source_id = _ordered_session(include_rag=include_rag)
    monkeypatch.setattr(builder_routes.pwd_context, "hash", lambda _secret: "safe-hash")

    def forbidden_guard(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("non-media clone entered the media storage guard")

    monkeypatch.setattr(builder_routes, "media_storage_capacity_guard", forbidden_guard)

    response = await _call_clone(session, context, source_id, include_rag=include_rag)

    assert response.is_active is False
    assert response.copied_media_assets == 0
    assert session.executions[:3] == [
        ("User", "FOR UPDATE"),
        ("Builder", "FOR UPDATE"),
        ("Tenant", "FOR UPDATE"),
    ]
    expected_reads: list[Any] = [("Tenant", None), ("ClinicConfigTable", None)]
    if include_rag:
        expected_reads.append(("KnowledgeDocument", None))
    assert session.executions[3:] == [*expected_reads, "flush", "commit"]
    assert not any(
        isinstance(item, tuple) and item[0] == "MediaAsset"
        for item in session.executions
    )
    assert session.committed is True
    assert session.rolled_back is False


@pytest.mark.asyncio
@pytest.mark.parametrize("include_rag", [False, True], ids=["media-only", "all"])
async def test_media_branch_enters_guard_before_any_media_query_and_copies_one_at_a_time(
    monkeypatch,
    include_rag: bool,
) -> None:
    rows = (_fake_media_row(b"%PDF-first"), _fake_media_row(b"%PDF-second!"))
    session, context, source_id = _ordered_session(
        include_rag=include_rag,
        include_media=True,
        media_rows=rows,
    )
    guard = _SequencedGuard(session)
    monkeypatch.setattr(builder_routes, "media_storage_capacity_guard", guard)
    monkeypatch.setattr(builder_routes.pwd_context, "hash", lambda _secret: "safe-hash")

    response = await _call_clone(
        session, context, source_id, include_rag=include_rag, include_media=True
    )

    assert response.copied_media_assets == 2
    assert guard.subjects == [source_id]
    expected_reads: list[Any] = [("Tenant", None), ("ClinicConfigTable", None)]
    if include_rag:
        expected_reads.append(("KnowledgeDocument", None))
    assert session.executions == [
        ("User", "FOR UPDATE"),
        ("Builder", "FOR UPDATE"),
        ("Tenant", None),
        "guard_enter",
        ("Tenant", "FOR UPDATE"),
        ("MediaAsset", "FOR SHARE"),
        ("validate_projection", sum(row.size_bytes for row in rows)),
        *expected_reads,
        "flush",
        ("MediaAsset", None),
        "flush",
        ("expunge", "MediaAsset"),
        ("MediaAsset", None),
        "flush",
        ("expunge", "MediaAsset"),
        "guard_exit",
        "commit",
    ]
    # Every per-row payload read is pinned to both the stabilized id and the
    # source tenant (not only the id).
    stabilized_ids = [
        row.id
        for statement, result in session.statements
        if _lock_mode(statement) == "FOR SHARE"
        for row in result.all()
    ]
    payload_reads = [
        statement
        for statement, _result in session.statements
        if statement.column_descriptions[0].get("entity") is MediaAsset
        and _lock_mode(statement) is None
    ]
    assert [_where_predicates(statement) for statement in payload_reads] == [
        {("id", media_id), ("tenant_id", source_id)} for media_id in stabilized_ids
    ]


def _where_predicates(statement: Any) -> set[tuple[str, Any]]:
    """Return the ``column == value`` predicates of a statement's WHERE clause."""
    where = statement.whereclause
    clauses = getattr(where, "clauses", [where])
    return {(clause.left.name, clause.right.value) for clause in clauses}


class _TrackedPayload(bytes):
    """A payload that reports its index when it is garbage-collected."""

    position: int
    released: list[int]

    def __del__(self) -> None:
        self.released.append(self.position)


class _PayloadTrackingSession:
    """Serves one fresh tracked payload per fetch and keeps no reference to it."""

    def __init__(self, payload_count: int) -> None:
        self.payload_count = payload_count
        self.fetches = 0
        self.released: list[int] = []
        self.released_at_fetch: list[list[int]] = []

    @staticmethod
    def payload_bytes(index: int) -> bytes:
        return b"%PDF-tracked-" + str(index).encode()

    async def execute(self, _statement: Any) -> _FakeResult:
        gc.collect()
        index = self.fetches
        self.fetches += 1
        self.released_at_fetch.append(sorted(self.released))
        payload = _TrackedPayload(self.payload_bytes(index))
        payload.position = index
        payload.released = self.released
        return _FakeResult(one=_fake_media_row(payload))

    def add(self, _value: Any) -> None:
        pass

    async def flush(self) -> None:
        pass

    def expunge(self, _value: Any) -> None:
        pass


@pytest.mark.asyncio
async def test_copy_source_media_releases_each_payload_before_the_next_fetch() -> None:
    count = 3
    session = _PayloadTrackingSession(count)
    source_media = tuple(
        (uuid4(), len(session.payload_bytes(index))) for index in range(count)
    )

    copied = await builder_routes._copy_source_media(
        session,  # type: ignore[arg-type]
        uuid4(),
        uuid4(),
        source_media,
    )
    gc.collect()

    assert copied == count
    # When fetch N+1 runs, payloads 0..N are already gone: never two at once.
    assert session.released_at_fetch == [list(range(index)) for index in range(count)]
    assert session.released == list(range(count))


@pytest.mark.asyncio
@pytest.mark.parametrize("drift", ["missing", "resized"])
async def test_locked_media_drift_is_a_sanitized_persistence_failure(
    monkeypatch,
    drift: str,
) -> None:
    row = _fake_media_row(b"%PDF-drift")
    session, context, source_id = _ordered_session(
        include_media=True,
        media_rows=(None if drift == "missing" else row,),
        stabilized_sizes=(row.size_bytes + (0 if drift == "missing" else 1),),
    )
    monkeypatch.setattr(
        builder_routes, "media_storage_capacity_guard", _SequencedGuard(session)
    )
    monkeypatch.setattr(builder_routes.pwd_context, "hash", lambda _secret: "safe-hash")
    recorder = _LogRecorder()
    monkeypatch.setattr(builder_routes, "logger", recorder)

    with pytest.raises(HTTPException) as raised:
        await _call_clone(session, context, source_id, include_media=True)

    assert raised.value.status_code == _CONTRACT["errors"]["500"]["status"]
    assert {"detail": raised.value.detail} == _CONTRACT["errors"]["500"]["body"]
    assert session.rolled_back is True
    assert session.committed is False
    assert recorder.events == [
        (
            "clinic_clone_failed",
            {
                "reason": "source_media_drift",
                "builder_id": str(context.builder_id),
                "source_tenant_id": str(source_id),
            },
        )
    ]


@pytest.mark.asyncio
async def test_cap_rejection_happens_before_any_clone_record_is_built(
    monkeypatch,
) -> None:
    rows = (_fake_media_row(b"%PDF-cap"),)
    session, context, source_id = _ordered_session(
        include_media=True,
        media_rows=rows,
    )
    monkeypatch.setattr(
        builder_routes,
        "media_storage_capacity_guard",
        _SequencedGuard(session, cap_exceeded=True),
    )

    with pytest.raises(HTTPException) as raised:
        await _call_clone(session, context, source_id, include_media=True)

    error = _CONTRACT["errors"]["409_media_storage_cap"]
    assert raised.value.status_code == error["status"]
    assert {"detail": raised.value.detail} == error["body"]
    # The 409 is raised before the clone name, source config or RAG are read and
    # before anything is flushed.
    assert session.executions == [
        ("User", "FOR UPDATE"),
        ("Builder", "FOR UPDATE"),
        ("Tenant", None),
        "guard_enter",
        ("Tenant", "FOR UPDATE"),
        ("MediaAsset", "FOR SHARE"),
        ("validate_projection", rows[0].size_bytes),
        "guard_exit",
    ]
    assert "flush" not in session.executions
    assert ("ClinicConfigTable", None) not in session.executions
    assert session.rolled_back is True
    assert session.committed is False


@pytest.mark.asyncio
async def test_injected_persistence_failure_rolls_back_once(monkeypatch) -> None:
    session, context, source_id = _ordered_session(
        flush_error=RuntimeError("injected persistence failure")
    )
    monkeypatch.setattr(builder_routes.pwd_context, "hash", lambda _secret: "safe-hash")

    with pytest.raises(RuntimeError, match="injected persistence failure"):
        await _call_clone(session, context, source_id)

    assert session.rolled_back is True
    assert session.committed is False


@pytest.mark.asyncio
async def test_sqlalchemy_failure_and_rollback_failure_stay_sanitized(
    monkeypatch,
) -> None:
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
        await _call_clone(session, context, source_id)

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
        async_client, "success", graph.source_id, graph.headers(), body=_ABSENT
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


def test_clone_openapi_documents_optional_strict_request_and_counts(test_app) -> None:
    test_app.openapi_schema = None
    schema = test_app.openapi()
    components = schema["components"]["schemas"]
    endpoint = _CONTRACT["success"]
    operation = schema["paths"][endpoint["path"]]["post"]

    request_body = operation["requestBody"]
    assert request_body["required"] is False
    request_schema = request_body["content"]["application/json"]["schema"]
    assert request_schema["type"] == "object"
    assert request_schema["additionalProperties"] is False
    assert set(request_schema["properties"]) == {"include_rag", "include_media"}
    for flag in ("include_rag", "include_media"):
        assert request_schema["properties"][flag]["type"] == "boolean"
        assert request_schema["properties"][flag]["default"] is False
    assert "include_config" not in json.dumps(request_schema)

    for status_code in ("409", "422", "500"):
        error_schema = operation["responses"][status_code]["content"][
            "application/json"
        ]["schema"]
        assert error_schema["$ref"].endswith("/ClinicCloneErrorResponse")
    assert "clinic_clone_media_storage_cap_exceeded" in (
        operation["responses"]["409"]["description"]
    )
    error_codes = components["ClinicCloneErrorDetail"]["properties"]["error"]["enum"]
    assert {
        error["body"]["detail"]["error"] for error in _CONTRACT["errors"].values()
    } == set(error_codes)

    success_ref = operation["responses"]["201"]["content"]["application/json"][
        "schema"
    ]["$ref"]
    assert success_ref.endswith("/ClinicCloneResponse")
    response_schema = components["ClinicCloneResponse"]
    for count in ("copied_rag_documents", "copied_media_assets"):
        assert count in response_schema["required"]
        assert response_schema["properties"][count]["minimum"] == 0
    preview_schema = components["ClinicClonePreviewResponse"]
    for aggregate in (
        "document_count",
        "total_bytes",
        "media_asset_count",
        "media_total_bytes",
    ):
        assert aggregate in preview_schema["required"]
        assert preview_schema["properties"][aggregate]["minimum"] == 0


@pytest.mark.asyncio
async def test_neighboring_builder_routes_remain_available(async_client) -> None:
    graph = await _seed_clone_graph()

    profile = await async_client.get("/v1/builder/me", headers=graph.headers())
    clinics = await async_client.get("/v1/builder/clinics", headers=graph.headers())

    assert profile.status_code == 200
    assert clinics.status_code == 200
    assert any(clinic["id"] == str(graph.source_id) for clinic in clinics.json())
