"""Horizontal tenant-isolation tests for the event ingestion boundary."""

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient

from src.api.middleware.auth import TenantContext, get_tenant_context
from src.api.middleware.tenant_identity import TenantIdentityMiddleware
from src.api.routes import events
from src.config.settings import settings
from src.repositories import get_db_session_dependency


@pytest.mark.asyncio
async def test_authenticated_tenant_cannot_submit_event_for_another_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tenant A plus tenant B identifiers must stop before any service call."""

    authenticated_tenant = UUID("a0000000-0000-0000-0000-000000000001")
    foreign_tenant = UUID("b0000000-0000-0000-0000-000000000002")
    service_calls: list[str] = []

    def forbidden_service_resolution() -> Any:
        service_calls.append("resolved")
        raise AssertionError("cross-tenant input reached the event service")

    async def unused_db() -> AsyncIterator[object]:
        yield object()

    class AuthenticatedTenantMiddleware:
        def __init__(self, application: Any) -> None:
            self.application = application

        async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
            scope.setdefault("state", {})["tenant"] = TenantContext(
                tenant_id=authenticated_tenant,
                api_key="test-tenant-a-key",
            )
            scope["state"]["request_id"] = "test-request-id"
            await self.application(scope, receive, send)

    app = FastAPI()
    app.include_router(events.router, prefix="/v1/events")
    app.dependency_overrides[get_tenant_context] = lambda: TenantContext(
        tenant_id=authenticated_tenant,
        api_key="test-tenant-a-key",
    )
    app.dependency_overrides[get_db_session_dependency] = unused_db
    monkeypatch.setattr(events, "get_event_service", forbidden_service_resolution)
    app.add_middleware(TenantIdentityMiddleware)
    app.add_middleware(AuthenticatedTenantMiddleware)

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/events",
            headers={"X-API-Key": "test-tenant-a-key"},
            json={
                "tenant_id": str(foreign_tenant),
                "session_id": "c0000000-0000-0000-0000-000000000002",
                "event_type": "message.in",
                "payload": {"content": "synthetic isolation probe"},
            },
        )

    assert response.status_code == 403
    assert response.json()["error"] == "tenant_identity_mismatch"
    assert service_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_kind", ["api_key", "jwt"])
async def test_payload_tenant_mismatch_is_403_before_route_for_each_credential(
    credential_kind: str,
    async_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.middleware.auth import APIKeyStore

    authenticated_tenant = uuid4()
    api_key_validation = AsyncMock(
        return_value=TenantContext(
            tenant_id=authenticated_tenant,
            api_key="sensitive-api-key",
        )
    )
    route_service = MagicMock(
        side_effect=AssertionError("identity mismatch reached route service")
    )
    audit = MagicMock()
    monkeypatch.setattr(APIKeyStore, "validate_api_key", api_key_validation)
    monkeypatch.setattr(events, "get_event_service", route_service)
    monkeypatch.setattr(
        "src.api.middleware.tenant_identity._audit_logger.warning", audit
    )

    if credential_kind == "api_key":
        headers = {"X-API-Key": "sensitive-api-key"}
    else:
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "sub": str(uuid4()),
                "tenant_id": str(authenticated_tenant),
                "role": "clinic_admin",
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            settings.secret_key,
            algorithm="HS256",
        )
        headers = {"Authorization": f"Bearer {token}"}

    response = await async_client.post(
        "/v1/events",
        headers=headers,
        json={
            "tenant_id": str(uuid4()),
            "session_id": str(uuid4()),
            "event_type": "message.in",
            "payload": {"content": "sensitive message body"},
        },
    )

    assert response.status_code == 403
    assert response.json()["error"] == "tenant_identity_mismatch"
    route_service.assert_not_called()
    audit.assert_called_once()
    rendered_audit = json.dumps(audit.call_args.kwargs).lower()
    assert "sensitive-api-key" not in rendered_audit
    assert "sensitive message body" not in rendered_audit
    if credential_kind == "jwt":
        assert token.lower() not in rendered_audit
    if credential_kind == "api_key":
        api_key_validation.assert_awaited_once_with("sensitive-api-key")
    else:
        api_key_validation.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("credential_kind", ["api_key", "jwt"])
async def test_auth_middleware_rejects_tenant_a_with_tenant_b_session_unchanged(
    credential_kind: str,
    async_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid A credential cannot read or mutate B through a session id."""

    from sqlalchemy import delete, func, select

    from src.api.middleware.auth import APIKeyStore
    from src.api.middleware.rate_limit import rate_limit_store
    from src.models.event import Event
    from src.models.handoff import HandoffRecord
    from src.models.session import Session
    from src.models.tenant import Tenant
    from src.repositories.database import get_db_session
    from src.repositories.session import SessionRepository

    tenant_a = uuid4()
    tenant_b = uuid4()
    session_b = uuid4()

    async def authenticate_a(_cls: type[APIKeyStore], api_key: str) -> TenantContext:
        assert api_key == "tenant-a-credential"
        return TenantContext(tenant_id=tenant_a, api_key=api_key, is_active=True)

    monkeypatch.setattr(APIKeyStore, "validate_api_key", classmethod(authenticate_a))
    original_lookup = SessionRepository.get_by_tenant_for_update
    tenant_lookup_calls: list[tuple[UUID, UUID]] = []

    async def counted_lookup(
        repository: SessionRepository, session_id: UUID, tenant_id: UUID
    ) -> Any:
        tenant_lookup_calls.append((session_id, tenant_id))
        return await original_lookup(repository, session_id, tenant_id)

    monkeypatch.setattr(SessionRepository, "get_by_tenant_for_update", counted_lookup)
    service = events.get_event_service()
    clinic_config = AsyncMock(
        side_effect=AssertionError("rejected session loaded clinic config")
    )
    session_state = AsyncMock(
        side_effect=AssertionError("rejected session loaded state")
    )
    message_history = AsyncMock(
        side_effect=AssertionError("rejected session loaded message history")
    )
    pipeline = AsyncMock(
        side_effect=AssertionError("rejected session reached cognitive pipeline")
    )
    monkeypatch.setattr(service, "_load_clinic_config", clinic_config)
    monkeypatch.setattr(service, "_load_session_state", session_state)
    monkeypatch.setattr(service, "_load_message_history", message_history)
    monkeypatch.setattr(service._pipeline, "process", pipeline)
    audit = MagicMock()
    monkeypatch.setattr(
        "src.api.middleware.tenant_identity._audit_logger.warning", audit
    )

    database = get_db_session()
    try:
        async with database.begin():
            database.add_all(
                [
                    Tenant(
                        id=tenant_a,
                        name="Isolation tenant A",
                        slug=f"isolation-a-{tenant_a}",
                        api_key_hash=f"isolation-a-{tenant_a}",
                        config={},
                    ),
                    Tenant(
                        id=tenant_b,
                        name="Isolation tenant B",
                        slug=f"isolation-b-{tenant_b}",
                        api_key_hash=f"isolation-b-{tenant_b}",
                        config={},
                    ),
                    Session(
                        id=session_b,
                        tenant_id=tenant_b,
                        channel="test",
                        cognitive_state={"turn_count": 7},
                        session_metadata={"sentinel": "tenant-b"},
                    ),
                    Event(
                        tenant_id=tenant_b,
                        session_id=session_b,
                        event_type="message.in",
                        event_data={"content": "tenant-b-sentinel"},
                        event_metadata={"sentinel": True},
                    ),
                ]
            )
    finally:
        await database.close()

    if credential_kind == "api_key":
        headers = {"X-API-Key": "tenant-a-credential"}
    else:
        now = datetime.now(timezone.utc)
        token = jwt.encode(
            {
                "sub": str(uuid4()),
                "tenant_id": str(tenant_a),
                "role": "clinic_admin",
                "iat": now,
                "exp": now + timedelta(minutes=5),
            },
            settings.secret_key,
            algorithm="HS256",
        )
        headers = {"Authorization": f"Bearer {token}"}

    async def snapshot_b() -> bytes:
        database = get_db_session()
        try:
            session_row = (
                await database.execute(
                    select(Session).where(
                        Session.id == session_b, Session.tenant_id == tenant_b
                    )
                )
            ).scalar_one()
            event_rows = list(
                (
                    await database.execute(
                        select(Event).where(
                            Event.session_id == session_b,
                            Event.tenant_id == tenant_b,
                        )
                    )
                ).scalars()
            )
            event_count = await database.scalar(
                select(func.count(Event.id)).where(Event.tenant_id == tenant_b)
            )
            handoff_count = await database.scalar(
                select(func.count(HandoffRecord.id)).where(
                    HandoffRecord.tenant_id == tenant_b
                )
            )
            value = {
                "session": {
                    "cognitive_state": session_row.cognitive_state,
                    "metadata": session_row.session_metadata,
                    "message_count": session_row.message_count,
                },
                "event_count": event_count,
                "handoff_count": handoff_count,
                "events": [
                    {
                        "id": str(event.id),
                        "data": event.event_data,
                        "metadata": event.event_metadata,
                    }
                    for event in event_rows
                ],
            }
            return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        finally:
            await database.close()

    before = await snapshot_b()
    bucket = rate_limit_store.get_or_create_bucket(str(tenant_a))
    bucket_tokens_before = bucket.tokens
    foreign_response = await async_client.post(
        "/v1/events",
        headers=headers,
        json={
            "tenant_id": str(tenant_a),
            "session_id": str(session_b),
            "event_type": "message.in",
            "payload": {"content": "cross-tenant mutation probe"},
        },
    )
    middle = await snapshot_b()
    nonexistent_session = uuid4()
    nonexistent_response = await async_client.post(
        "/v1/events",
        headers=headers,
        json={
            "session_id": str(nonexistent_session),
            "event_type": "message.in",
            "payload": {"content": "nonexistent mutation probe"},
        },
    )
    after = await snapshot_b()

    assert foreign_response.status_code == 403
    assert nonexistent_response.status_code == 403
    assert (
        foreign_response.json()
        == nonexistent_response.json()
        == {
            "error": "tenant_identity_mismatch",
            "message": "Tenant identity does not match the authenticated request",
        }
    )
    assert middle == before
    assert after == before
    assert tenant_lookup_calls == [
        (session_b, tenant_a),
        (nonexistent_session, tenant_a),
    ]
    assert bucket.tokens == bucket_tokens_before
    clinic_config.assert_not_awaited()
    session_state.assert_not_awaited()
    message_history.assert_not_awaited()
    pipeline.assert_not_awaited()
    assert audit.call_count == 2
    audit_shapes = []
    for call in audit.call_args_list:
        values = dict(call.kwargs)
        assert set(values) == {
            "request_id",
            "route",
            "method",
            "authentication_mode",
            "authenticated_tenant",
            "claimed_tenant",
        }
        values.pop("request_id")
        audit_shapes.append(values)
        rendered = json.dumps(call.kwargs).lower()
        for forbidden in (
            "tenant-a-credential",
            "cross-tenant mutation probe",
            "nonexistent mutation probe",
            "signature",
            "phone",
            "provider",
        ):
            assert forbidden not in rendered
        if credential_kind == "jwt":
            assert token.lower() not in rendered
    assert audit_shapes[0] == audit_shapes[1]

    database = get_db_session()
    try:
        async with database.begin():
            await database.execute(delete(Event).where(Event.tenant_id == tenant_b))
            await database.execute(delete(Session).where(Session.tenant_id == tenant_b))
            await database.execute(
                delete(Tenant).where(Tenant.id.in_([tenant_a, tenant_b]))
            )
    finally:
        await database.close()
        rate_limit_store.clear_tenant(str(tenant_a))


@pytest.mark.asyncio
async def test_existing_tenant_session_uses_one_lookup_and_processes_normally(
    async_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tenant-owned session crosses the same guard with one scoped lookup."""

    from sqlalchemy import delete

    from src.api.middleware.auth import APIKeyStore
    from src.models.session import Session
    from src.models.tenant import Tenant
    from src.repositories.database import get_db_session
    from src.repositories.session import SessionRepository

    tenant_id = uuid4()
    session_id = uuid4()

    async def authenticate(_cls: type[APIKeyStore], api_key: str) -> TenantContext:
        assert api_key == "existing-session-credential"
        return TenantContext(tenant_id=tenant_id, api_key=api_key, is_active=True)

    monkeypatch.setattr(APIKeyStore, "validate_api_key", classmethod(authenticate))
    original_lookup = SessionRepository.get_by_tenant_for_update
    tenant_lookup_calls: list[tuple[UUID, UUID]] = []

    async def counted_lookup(
        repository: SessionRepository, requested_session: UUID, requested_tenant: UUID
    ) -> Any:
        tenant_lookup_calls.append((requested_session, requested_tenant))
        return await original_lookup(repository, requested_session, requested_tenant)

    monkeypatch.setattr(SessionRepository, "get_by_tenant_for_update", counted_lookup)

    database = get_db_session()
    try:
        async with database.begin():
            database.add(
                Tenant(
                    id=tenant_id,
                    name="Existing session tenant",
                    slug=f"existing-{tenant_id}",
                    api_key_hash=f"existing-{tenant_id}",
                    config={},
                )
            )
            database.add(
                Session(
                    id=session_id,
                    tenant_id=tenant_id,
                    channel="test",
                    cognitive_state={},
                    session_metadata={"sentinel": "owned"},
                )
            )
    finally:
        await database.close()

    from src.core.pipeline import PipelineResult
    from src.services.event_service import EventService

    pipeline_result = PipelineResult(
        tenant_id=tenant_id,
        session_id=session_id,
        intent="greeting",
        intent_category="greeting",
        funnel_stage="lead_novo",
        action="respond",
        confidence=0.99,
        safety_concern=False,
        safety_category="none",
        needs_rag=False,
        handoff_required=False,
        system_prompt="system",
        instructions="respond normally",
    )
    pipeline = MagicMock()
    pipeline.process = AsyncMock(return_value=pipeline_result)
    service = EventService(pipeline=pipeline)
    clinic_config = AsyncMock(return_value=None)
    message_history = AsyncMock(return_value=[])
    finish_response = AsyncMock(
        return_value=JSONResponse(status_code=200, content={"ok": True})
    )
    monkeypatch.setattr(service, "_load_clinic_config", clinic_config)
    monkeypatch.setattr(service, "_load_message_history", message_history)
    monkeypatch.setattr(service, "finish_response", finish_response)
    monkeypatch.setattr(events, "get_event_service", lambda: service)

    response = await async_client.post(
        "/v1/events",
        headers={"X-API-Key": "existing-session-credential"},
        json={
            "tenant_id": str(tenant_id),
            "session_id": str(session_id),
            "event_type": "message.in",
            "payload": {"content": "normal owned-session processing"},
        },
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert tenant_lookup_calls == [(session_id, tenant_id)]
    pipeline.process.assert_awaited_once()
    clinic_config.assert_awaited_once_with(tenant_id)
    message_history.assert_awaited_once()
    finish_response.assert_awaited_once()

    database = get_db_session()
    try:
        async with database.begin():
            await database.execute(delete(Session).where(Session.id == session_id))
            await database.execute(delete(Tenant).where(Tenant.id == tenant_id))
    finally:
        await database.close()
