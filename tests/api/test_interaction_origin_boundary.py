"""Trusted-origin boundary of ``/v1/events`` (wish interaction-origin, Group 3).

Covers, at the HTTP boundary:

* the fixed error order: JWT role allowlist (``403 caller_role_forbidden``,
  rate-limit slot consumed) → reserved ``interaction_origin`` (``422``) → the
  #68 session check (``403 tenant_identity_mismatch``, refunded);
* origin per credential × ``channel`` (API key → production, operational JWT →
  simulator, inconsistent context → unknown) and ONE context object across
  ``run_pipeline``, ``finish_response``, the bundle and ``initiate_handoff``;
* tenant-scoped detail/list reads (no cross-tenant enumeration) exposing
  ``interaction_origin``;
* the Group 2 ``/v1/sessions`` coverage gap (builder and API key);
* no context field on any data model.
"""

from collections.abc import AsyncIterator, Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from src.api.middleware.auth import APIKeyStore, TenantContext, get_tenant_context
from src.api.middleware.rate_limit import rate_limit_store
from src.api.routes import events as events_route
from src.api.schemas.events import CanonicalEventRequest, EventPayload, EventRequest
from src.config.settings import settings
from src.core.pipeline import PipelineInput, PipelineResult
from src.handoff.models import HandoffRequest
from src.interaction_context import InteractionOrigin, ResolvedInteractionContext
from src.memory.event_store import EventStore
from src.repositories import get_db_session_dependency
from src.repositories.session import SessionRepository

SERVER_MANAGED = {"detail": {"code": "interaction_origin_server_managed"}}
ROLE_FORBIDDEN = {"detail": {"code": "caller_role_forbidden"}}
TENANT_MISMATCH = {
    "error": "tenant_identity_mismatch",
    "message": "Tenant identity does not match the authenticated request",
}
OPERATIONAL_ROLES = ("clinic_admin", "builder", "super_admin")
CHANNELS = ("whatsapp", "web", "simulator", "api", "telegram", "unknown")


def _shadowed_methods() -> set[str]:
    """Instance attributes of the process singletons that hide a class method."""
    import src.services.event_service as event_service_module
    from src.core import pipeline as pipeline_module

    shadowed: set[str] = set()
    for singleton in (event_service_module._event_service, pipeline_module._pipeline):
        if singleton is None:
            continue
        for name in vars(singleton):
            if callable(getattr(type(singleton), name, None)):
                shadowed.add(f"{type(singleton).__name__}.{name}")
    return shadowed


@pytest.fixture(autouse=True, scope="module")
def _no_singleton_method_shadowing() -> Any:
    # Webhook suites patch these classes; a leftover instance attribute on the
    # shared singleton silently bypasses their patches in later files. Module
    # scope: it must run after every function-scoped monkeypatch undo.
    before = _shadowed_methods()
    yield
    assert _shadowed_methods() <= before


# ============================================
# Helpers
# ============================================


def _jwt_headers(
    role: Optional[str],
    tenant_id: UUID,
    *,
    builder_id: Optional[UUID] = None,
) -> dict[str, str]:
    payload: dict[str, Any] = {
        "sub": str(uuid4()),
        "tenant_id": str(tenant_id),
        "exp": datetime.now(timezone.utc) + timedelta(hours=1),
    }
    if role is not None:
        payload["role"] = role
    if builder_id is not None:
        payload["builder_id"] = str(builder_id)
    token = jwt.encode(payload, settings.secret_key, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _accept_api_key(
    monkeypatch: pytest.MonkeyPatch, tenant_id: UUID, key: str = "origin-boundary-key"
) -> dict[str, str]:
    async def authenticate(_cls: type[APIKeyStore], api_key: str) -> TenantContext:
        assert api_key == key
        return TenantContext(tenant_id=tenant_id, api_key=api_key, is_active=True)

    monkeypatch.setattr(APIKeyStore, "validate_api_key", classmethod(authenticate))
    return {"X-API-Key": key}


def _credential_headers(
    monkeypatch: pytest.MonkeyPatch, credential: str, tenant_id: UUID
) -> dict[str, str]:
    if credential == "api_key":
        return _accept_api_key(monkeypatch, tenant_id)
    role = credential.removeprefix("jwt-")
    builder_id = uuid4() if role == "builder" else None
    return _jwt_headers(role, tenant_id, builder_id=builder_id)


def _event_body(
    session_id: UUID,
    *,
    event_type: str = "message.in",
    channel: str = "whatsapp",
    metadata: Optional[dict[str, Any]] = None,
    tenant_id: Optional[UUID] = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "session_id": str(session_id),
        "event_type": event_type,
        "payload": {
            "content": "origin boundary probe",
            "channel": channel,
            "metadata": metadata or {},
        },
    }
    if tenant_id is not None:
        body["tenant_id"] = str(tenant_id)
    return body


class _SpyService:
    """Records the context handed to each phase; never touches the database."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, CanonicalEventRequest, Any]] = []

    async def run_pipeline(
        self,
        request: CanonicalEventRequest,
        session: Any = None,
        *,
        context: Any = None,
    ) -> tuple[Any, Any, list[Any]]:
        self.calls.append(("run_pipeline", request, context))
        return object(), None, []

    async def finish_response(
        self,
        request: CanonicalEventRequest,
        result: Any,
        clinic_config: Any,
        message_history: Any,
        *,
        context: Any = None,
    ) -> JSONResponse:
        self.calls.append(("finish_response", request, context))
        return JSONResponse(status_code=200, content={"ok": True})


def _forbid_effects(monkeypatch: pytest.MonkeyPatch) -> dict[str, MagicMock]:
    """Spies proving that nothing past the boundary runs."""

    service = MagicMock(side_effect=AssertionError("route reached the event service"))
    lookup = AsyncMock(side_effect=AssertionError("route looked up the session"))
    append = AsyncMock(side_effect=AssertionError("route wrote an event"))
    monkeypatch.setattr(events_route, "get_event_service", service)
    monkeypatch.setattr(SessionRepository, "get_by_tenant_for_update", lookup)
    monkeypatch.setattr(EventStore, "append_batch", append)
    monkeypatch.setattr(EventStore, "append", append)
    return {"service": service, "lookup": lookup, "append": append}


def _assert_no_effects(spies: dict[str, MagicMock]) -> None:
    spies["service"].assert_not_called()
    spies["lookup"].assert_not_awaited()
    spies["append"].assert_not_awaited()


def _seed(client: TestClient, *rows: Any, builders: tuple[UUID, ...] = ()) -> None:
    from src.models.builder import Builder
    from src.repositories.database import get_db_session

    async def _run() -> None:
        db = get_db_session()
        try:
            for builder_id in builders:
                db.add(Builder(id=builder_id, name=f"Builder {builder_id}"))
            await db.flush()
            db.add_all(list(rows))
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]


def _tenant(tenant_id: UUID, builder_id: Optional[UUID] = None) -> Any:
    from src.models.tenant import Tenant

    return Tenant(
        id=tenant_id,
        name=f"Origin clinic {tenant_id}",
        slug=f"origin-{tenant_id}",
        api_key_hash=f"origin-{tenant_id}",
        builder_id=builder_id,
        config={},
    )


def _session_row(tenant_id: UUID, session_id: Optional[UUID] = None) -> Any:
    from src.models.session import Session

    return Session(
        id=session_id or uuid4(),
        tenant_id=tenant_id,
        channel="whatsapp",
        cognitive_state={},
        session_metadata={},
    )


def _event_row(
    tenant_id: UUID,
    session_id: UUID,
    *,
    event_type: str = "message.in",
    origin: str = "production",
) -> Any:
    from src.models.event import Event

    return Event(
        id=uuid4(),
        tenant_id=tenant_id,
        session_id=session_id,
        event_type=event_type,
        interaction_origin=origin,
        event_data={"content": "origin row"},
        event_metadata={},
    )


# ============================================
# Error order on POST /v1/events
# ============================================


@pytest.mark.parametrize("role", ["investor", "unrecognized", None])
def test_forbidden_jwt_role_is_403_before_any_effect_and_consumes_the_slot(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, role: Optional[str]
) -> None:
    tenant_id = uuid4()
    spies = _forbid_effects(monkeypatch)
    bucket = rate_limit_store.get_or_create_bucket(str(tenant_id))
    tokens_before = bucket.tokens
    try:
        response = client.post(
            "/v1/events",
            headers=_jwt_headers(role, tenant_id),
            json=_event_body(uuid4()),
        )
        assert response.status_code == 403
        assert response.json() == ROLE_FORBIDDEN
        _assert_no_effects(spies)
        # Q3: the slot is consumed and NOT refunded (refunds skip these headers).
        assert "X-RateLimit-Remaining" in response.headers
        assert bucket.tokens < tokens_before
    finally:
        rate_limit_store.clear_tenant(str(tenant_id))


@pytest.mark.parametrize("location", ["body", "payload", "metadata"])
def test_forbidden_role_is_checked_before_the_reserved_field(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    spies = _forbid_effects(monkeypatch)
    body = _event_body(uuid4())
    _place_reserved_field(body, location, "production")

    response = client.post(
        "/v1/events", headers=_jwt_headers("investor", uuid4()), json=body
    )

    assert response.status_code == 403
    assert response.json() == ROLE_FORBIDDEN
    _assert_no_effects(spies)


def _place_reserved_field(body: dict[str, Any], location: str, value: Any) -> None:
    if location == "body":
        body["interaction_origin"] = value
    elif location == "payload":
        body["payload"]["interaction_origin"] = value
    else:
        body["payload"]["metadata"]["interaction_origin"] = value


@pytest.mark.parametrize("location", ["body", "payload", "metadata"])
@pytest.mark.parametrize("value", ["production", "simulator", None])
@pytest.mark.parametrize("credential", ["api_key", "jwt-clinic_admin", "jwt-builder"])
def test_reserved_field_is_422_before_session_lookup_or_pipeline(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
    value: Optional[str],
    credential: str,
) -> None:
    tenant_id = uuid4()
    headers = _credential_headers(monkeypatch, credential, tenant_id)
    spies = _forbid_effects(monkeypatch)
    # A foreign/absent session would be the #68 403; the 422 must come first.
    body = _event_body(uuid4())
    _place_reserved_field(body, location, value)

    response = client.post("/v1/events", headers=headers, json=body)

    assert response.status_code == 422
    assert response.json() == SERVER_MANAGED
    _assert_no_effects(spies)


def test_openapi_422_documents_validation_error_and_server_managed_shape() -> None:
    from src.api.main import create_app

    schema = create_app().openapi()
    operation = schema["paths"][f"{settings.api_prefix}/events"]["post"]
    content = operation["responses"]["422"]["content"]["application/json"]

    variants = content["schema"]["oneOf"]
    assert {"$ref": "#/components/schemas/HTTPValidationError"} in variants
    assert "HTTPValidationError" in schema["components"]["schemas"]
    server_managed = next(v for v in variants if "$ref" not in v)
    assert server_managed["properties"]["detail"]["properties"]["code"]["enum"] == [
        SERVER_MANAGED["detail"]["code"]
    ]
    examples = content["examples"]
    assert examples["interaction_origin_server_managed"]["value"] == SERVER_MANAGED
    assert isinstance(examples["validation_error"]["value"]["detail"], list)

    detail_schema = schema["components"]["schemas"]["EventDetailResponse"]
    assert "interaction_origin" in detail_schema["required"]


def test_adapter_built_models_are_not_affected_by_the_reserved_field() -> None:
    """Only the public raw body is checked; adapters build these models."""

    payload = EventPayload(content="x", metadata={"interaction_origin": "production"})
    canonical = CanonicalEventRequest(
        tenant_id=uuid4(), session_id=uuid4(), payload=payload
    )
    public = EventRequest.model_validate(
        {
            "session_id": str(uuid4()),
            "interaction_origin": "production",
            "payload": {"content": "x", "interaction_origin": "production"},
        }
    )
    assert canonical.payload.metadata == {"interaction_origin": "production"}
    assert "interaction_origin" not in public.model_dump()


# ============================================
# Origin per credential and one context object
# ============================================


@pytest.mark.parametrize("channel", CHANNELS)
@pytest.mark.parametrize(
    ("credential", "expected"),
    [
        ("api_key", InteractionOrigin.PRODUCTION),
        *[(f"jwt-{role}", InteractionOrigin.SIMULATOR) for role in OPERATIONAL_ROLES],
    ],
)
def test_origin_follows_the_credential_never_the_channel(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
    credential: str,
    expected: InteractionOrigin,
) -> None:
    tenant_id = uuid4()
    session_id = uuid4()
    headers = _credential_headers(monkeypatch, credential, tenant_id)
    headers.update({"X-Interaction-Origin": "production", "X-Channel": "simulator"})
    spy = _SpyService()
    monkeypatch.setattr(events_route, "get_event_service", lambda: spy)

    response = client.post(
        "/v1/events",
        headers=headers,
        json=_event_body(
            session_id,
            channel=channel,
            metadata={"origin": "production", "source": "simulator"},
        ),
    )

    assert response.status_code == 200
    (run_name, run_request, context), (finish_name, finish_request, finished) = (
        spy.calls
    )
    assert (run_name, finish_name) == ("run_pipeline", "finish_response")
    assert finished is context
    assert finish_request is run_request
    assert type(context) is ResolvedInteractionContext
    assert context.origin is expected
    assert context.effective_tenant_id == tenant_id == run_request.tenant_id
    assert context.session_id == session_id == run_request.session_id
    assert context.interaction_event_id is not None


def test_route_builds_the_context_once_with_the_single_constructor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant_id, session_id = uuid4(), uuid4()
    spy = _SpyService()
    monkeypatch.setattr(events_route, "get_event_service", lambda: spy)
    built: list[tuple[dict[str, Any], ResolvedInteractionContext]] = []
    real_constructor = events_route.build_interaction_context

    def constructor_spy(**kwargs: Any) -> ResolvedInteractionContext:
        context = real_constructor(**kwargs)
        built.append((kwargs, context))
        return context

    monkeypatch.setattr(events_route, "build_interaction_context", constructor_spy)

    response = client.post(
        "/v1/events",
        headers=_accept_api_key(monkeypatch, tenant_id),
        json=_event_body(session_id),
    )

    assert response.status_code == 200
    ((kwargs, context),) = built
    assert kwargs == {
        "effective_tenant_id": tenant_id,
        "session_id": session_id,
        "origin": InteractionOrigin.PRODUCTION,
        "event_type": "message.in",
    }
    assert [call[2] for call in spy.calls] == [context, context]
    assert all(call[2] is context for call in spy.calls)


@pytest.mark.parametrize("event_type", ["session.start", "handoff.complete"])
def test_non_message_in_context_has_no_interaction_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, event_type: str
) -> None:
    tenant_id = uuid4()
    spy = _SpyService()
    monkeypatch.setattr(events_route, "get_event_service", lambda: spy)

    response = client.post(
        "/v1/events",
        headers=_accept_api_key(monkeypatch, tenant_id),
        json=_event_body(uuid4(), event_type=event_type),
    )

    assert response.status_code == 200
    context = spy.calls[0][2]
    assert context.origin is InteractionOrigin.PRODUCTION
    assert context.interaction_event_id is None


def _bare_events_app(tenant: TenantContext, spy: _SpyService) -> FastAPI:
    async def unused_db() -> AsyncIterator[object]:
        yield MagicMock(commit=AsyncMock(), rollback=AsyncMock())

    app = FastAPI()
    app.include_router(events_route.router, prefix="/v1/events")
    app.dependency_overrides[get_tenant_context] = lambda: tenant
    app.dependency_overrides[get_db_session_dependency] = unused_db
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tenant_factory",
    [
        lambda t: TenantContext(tenant_id=t, api_key="k", user_id=uuid4()),
        lambda t: TenantContext(
            tenant_id=t,
            api_key="k",
            user_id=uuid4(),
            role="clinic_admin",
            authentication_mode="jwt",
        ),
        lambda t: TenantContext(
            tenant_id=t, api_key="k", authentication_mode="meta_hmac"
        ),
    ],
    ids=["api-key-with-user", "jwt-with-api-key", "meta-with-api-key"],
)
async def test_inconsistent_authenticated_context_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tenant_factory: Callable[[UUID], TenantContext],
) -> None:
    tenant_id = uuid4()
    spy = _SpyService()
    monkeypatch.setattr(events_route, "get_event_service", lambda: spy)
    app = _bare_events_app(tenant_factory(tenant_id), spy)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as http:
        response = await http.post("/v1/events", json=_event_body(uuid4()))

    assert response.status_code == 200
    assert spy.calls[0][2].origin is InteractionOrigin.UNKNOWN
    assert spy.calls[1][2] is spy.calls[0][2]


@pytest.mark.asyncio
async def test_one_context_object_crosses_phases_bundle_and_handoff(
    async_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Route → real EventService → real pipeline → handoff seam and bundle."""

    import src.services.event_service as event_service_module
    from src.core import pipeline as pipeline_module
    from src.models.event import Event
    from src.models.session import Session
    from src.models.tenant import Tenant
    from src.repositories.database import get_db_session
    from src.services.event_service import EventService

    tenant_id, session_id = uuid4(), uuid4()
    headers = _accept_api_key(monkeypatch, tenant_id)
    # A throwaway service in the singleton slot: undoing an instance-level
    # monkeypatch re-sets the captured bound method as an instance attribute,
    # which would shadow later class-level patches on the shared singleton.
    service = EventService()
    monkeypatch.setattr(event_service_module, "_event_service", service)
    seen: dict[str, Any] = {}

    class _HandoffSpy:
        async def initiate_handoff(
            self, request: HandoffRequest, *, context: Any = None
        ) -> None:
            seen["handoff"] = context
            seen["handoff_request"] = request
            return None

    decision = pipeline_module.SRMDecisionModel(
        intent="atendimento_humano",
        intent_category="information",
        funnel_stage="em_qualificacao",
        action="handoff_administrativo",
        confidence=0.99,
        handoff_required=True,
        handoff_reason="user_request",
    )
    monkeypatch.setattr(service._pipeline, "_handoff", _HandoffSpy())
    monkeypatch.setattr(
        type(service._pipeline), "_run_srm", AsyncMock(return_value=decision)
    )
    real_run, real_finish = service.run_pipeline, service.finish_response
    real_builder = event_service_module.build_interaction_event_bundle

    async def run_spy(*args: Any, **kwargs: Any) -> Any:
        seen["run_pipeline"] = kwargs["context"]
        return await real_run(*args, **kwargs)

    async def finish_spy(*args: Any, **kwargs: Any) -> Any:
        seen["finish_response"] = kwargs["context"]
        return await real_finish(*args, **kwargs)

    def builder_spy(*args: Any, **kwargs: Any) -> Any:
        seen["bundle"] = args[2]
        return real_builder(*args, **kwargs)

    monkeypatch.setattr(service, "run_pipeline", run_spy)
    monkeypatch.setattr(service, "finish_response", finish_spy)
    monkeypatch.setattr(
        event_service_module, "build_interaction_event_bundle", builder_spy
    )

    database = get_db_session()
    try:
        async with database.begin():
            database.add(_tenant(tenant_id))
        async with database.begin():
            database.add(_session_row(tenant_id, session_id))
    finally:
        await database.close()

    try:
        response = await async_client.post(
            "/v1/events",
            headers=headers,
            json=_event_body(session_id, channel="web"),
        )
        assert response.status_code == 200
        body = response.json()

        context = seen["run_pipeline"]
        assert type(context) is ResolvedInteractionContext
        for phase in ("finish_response", "bundle", "handoff"):
            assert seen[phase] is context, phase
        assert context.origin is InteractionOrigin.PRODUCTION
        assert "context" not in seen["handoff_request"].model_dump()

        database = get_db_session()
        try:
            rows = list(
                (
                    await database.scalars(
                        select(Event).where(
                            Event.tenant_id == tenant_id,
                            Event.session_id == session_id,
                        )
                    )
                ).all()
            )
        finally:
            await database.close()
        by_type = {row.event_type: row for row in rows}
        assert body["interaction_event_id"] == str(context.interaction_event_id)
        assert str(by_type["message.in"].id) == body["interaction_event_id"]
        assert str(by_type["srm.decision"].id) == body["event_id"]
        assert {row.interaction_origin for row in rows} == {"production"}
    finally:
        database = get_db_session()
        try:
            async with database.begin():
                await database.execute(
                    delete(Event).where(Event.tenant_id == tenant_id)
                )
                await database.execute(
                    delete(Session).where(Session.tenant_id == tenant_id)
                )
                await database.execute(delete(Tenant).where(Tenant.id == tenant_id))
        finally:
            await database.close()
        rate_limit_store.clear_tenant(str(tenant_id))


# ============================================
# #68 codes kept (builder included)
# ============================================


def test_builder_cannot_select_an_owned_clinic_in_the_event_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    builder_id, own_tenant, clinic = uuid4(), uuid4(), uuid4()
    _seed(
        client,
        _tenant(own_tenant, builder_id),
        _tenant(clinic, builder_id),
        builders=(builder_id,),
    )
    spies = _forbid_effects(monkeypatch)

    response = client.post(
        "/v1/events",
        headers=_jwt_headers("builder", own_tenant, builder_id=builder_id),
        json=_event_body(uuid4(), tenant_id=clinic),
    )

    assert response.status_code == 403
    assert response.json() == TENANT_MISMATCH
    _assert_no_effects(spies)


@pytest.mark.parametrize("credential", ["api_key", "jwt-builder", "jwt-clinic_admin"])
def test_foreign_session_keeps_the_68_code_and_refund(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, credential: str
) -> None:
    own_tenant, other_tenant = uuid4(), uuid4()
    foreign_session = _session_row(other_tenant)
    _seed(client, _tenant(own_tenant), _tenant(other_tenant), foreign_session)
    headers = _credential_headers(monkeypatch, credential, own_tenant)
    pipeline = AsyncMock(side_effect=AssertionError("foreign session reached LLM"))
    from src.core.pipeline import CognitivePipeline

    # Class-level: an instance-level patch on the shared singleton would leave
    # the bound method behind as an instance attribute after undo.
    monkeypatch.setattr(CognitivePipeline, "process", pipeline)
    bucket = rate_limit_store.get_or_create_bucket(str(own_tenant))
    tokens_before = bucket.tokens
    try:
        response = client.post(
            "/v1/events", headers=headers, json=_event_body(foreign_session.id)
        )

        assert response.status_code == 403
        assert response.json() == TENANT_MISMATCH
        pipeline.assert_not_awaited()
        assert bucket.tokens == tokens_before
    finally:
        rate_limit_store.clear_tenant(str(own_tenant))


# ============================================
# GET detail / list stay inside the authenticated tenant
# ============================================


@pytest.mark.parametrize("credential", ["api_key", "jwt-clinic_admin", "jwt-builder"])
def test_event_reads_never_enumerate_another_tenant(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, credential: str
) -> None:
    builder_id = uuid4()
    own_tenant, other_tenant = uuid4(), uuid4()
    own_session, other_session = _session_row(own_tenant), _session_row(other_tenant)
    own_event = _event_row(own_tenant, own_session.id, origin="simulator")
    other_event = _event_row(other_tenant, other_session.id, origin="production")
    # The builder even owns the other clinic: /v1/events has no clinic selection.
    _seed(
        client,
        _tenant(own_tenant, builder_id),
        _tenant(other_tenant, builder_id),
        own_session,
        other_session,
        own_event,
        other_event,
        builders=(builder_id,),
    )
    if credential == "api_key":
        headers = _accept_api_key(monkeypatch, own_tenant)
    else:
        role = credential.removeprefix("jwt-")
        headers = _jwt_headers(
            role, own_tenant, builder_id=builder_id if role == "builder" else None
        )
    missing_id = uuid4()

    foreign_detail = client.get(f"/v1/events/{other_event.id}", headers=headers)
    missing_detail = client.get(f"/v1/events/{missing_id}", headers=headers)
    foreign_list = client.get(
        "/v1/events", params={"tenant_id": str(other_tenant)}, headers=headers
    )
    foreign_session_list = client.get(
        "/v1/events",
        params={"tenant_id": str(own_tenant), "session_id": str(other_session.id)},
        headers=headers,
    )
    own_detail = client.get(f"/v1/events/{own_event.id}", headers=headers)
    own_list = client.get(
        "/v1/events", params={"tenant_id": str(own_tenant)}, headers=headers
    )

    assert foreign_detail.status_code == missing_detail.status_code == 404
    assert foreign_detail.json()["detail"] == f"Event {other_event.id} not found"
    assert missing_detail.json()["detail"] == f"Event {missing_id} not found"
    assert foreign_list.status_code == 404
    assert foreign_list.json() == {"detail": "Tenant or session not found"}
    assert foreign_session_list.status_code == 200
    assert foreign_session_list.json() == []
    for response in (foreign_detail, foreign_list):
        assert "production" not in response.text
    assert own_detail.status_code == 200
    assert own_detail.json()["interaction_origin"] == "simulator"
    assert own_list.status_code == 200
    assert [item["event_id"] for item in own_list.json()] == [str(own_event.id)]
    assert own_list.json()[0]["interaction_origin"] == "simulator"


# ============================================
# Group 2 gap: /v1/sessions for builder and API key
# ============================================


def test_builder_foreign_session_is_404_and_unowned_override_is_403(
    client: TestClient,
) -> None:
    builder_id = uuid4()
    builder_home, owned_clinic, unowned_clinic = uuid4(), uuid4(), uuid4()
    owned_session = _session_row(owned_clinic)
    unowned_session = _session_row(unowned_clinic)
    _seed(
        client,
        _tenant(builder_home, builder_id),
        _tenant(owned_clinic, builder_id),
        _tenant(unowned_clinic),
        owned_session,
        unowned_session,
        builders=(builder_id,),
    )
    headers = _jwt_headers("builder", builder_home, builder_id=builder_id)

    owned = client.get(
        f"/v1/sessions/{owned_session.id}",
        params={"tenant_id": str(owned_clinic)},
        headers=headers,
    )
    foreign_in_owned = client.get(
        f"/v1/sessions/{unowned_session.id}",
        params={"tenant_id": str(owned_clinic)},
        headers=headers,
    )
    foreign_without_override = client.get(
        f"/v1/sessions/{owned_session.id}", headers=headers
    )
    unowned_override = client.get(
        f"/v1/sessions/{unowned_session.id}",
        params={"tenant_id": str(unowned_clinic)},
        headers=headers,
    )

    assert owned.status_code == 200
    for response in (foreign_in_owned, foreign_without_override):
        assert response.status_code == 404
        assert response.json() == {"detail": {"code": "session_not_found"}}
    assert unowned_override.status_code == 403
    assert unowned_override.json() == {"detail": {"code": "tenant_override_forbidden"}}


def test_api_key_foreign_session_is_404_and_override_is_403(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    own_tenant, other_tenant = uuid4(), uuid4()
    own_session, other_session = _session_row(own_tenant), _session_row(other_tenant)
    _seed(
        client,
        _tenant(own_tenant),
        _tenant(other_tenant),
        own_session,
        other_session,
    )
    headers = _accept_api_key(monkeypatch, own_tenant)

    own = client.get(f"/v1/sessions/{own_session.id}", headers=headers)
    foreign = client.get(f"/v1/sessions/{other_session.id}", headers=headers)
    override = client.get(
        f"/v1/sessions/{other_session.id}",
        params={"tenant_id": str(other_tenant)},
        headers=headers,
    )

    assert own.status_code == 200
    assert foreign.status_code == 404
    assert foreign.json() == {"detail": {"code": "session_not_found"}}
    assert override.status_code == 403
    assert override.json() == {"detail": {"code": "tenant_override_forbidden"}}


# ============================================
# No trust object in any data model
# ============================================


@pytest.mark.parametrize(
    "model",
    [
        HandoffRequest,
        EventRequest,
        EventPayload,
        CanonicalEventRequest,
        PipelineInput,
        PipelineResult,
    ],
)
def test_no_data_model_carries_a_context_field(model: Any) -> None:
    for name, field in model.model_fields.items():
        assert name not in {
            "context",
            "interaction_context",
            "interaction_origin",
            "interaction_event_id",
            "origin",
        }, name
        annotation = repr(field.annotation)
        assert "ResolvedInteractionContext" not in annotation, name
        assert "InteractionOrigin" not in annotation, name
