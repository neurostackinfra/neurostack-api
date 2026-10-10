"""Contracts for the outermost JSON ingress body cap."""

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from src.api.main import app as main_app
from src.api.main import create_app
from src.api.middleware.auth import APIKeyStore, TenantContext
from src.api.middleware.ingress_body_limit import (
    MAX_INGRESS_BODY_BYTES,
    IngressBodyLimitMiddleware,
)
from src.api.middleware.rate_limit import rate_limit_store
from src.api.routes import events
from src.repositories import get_db_session_dependency


async def _invoke(
    path: str,
    messages: list[Message],
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[list[Message], list[Message]]:
    replayed: list[Message] = []
    sent: list[Message] = []
    queued = list(messages)

    async def receive() -> Message:
        if queued:
            return queued.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    async def downstream(scope: Scope, receive: Any, send: Any) -> None:
        while True:
            message = await receive()
            replayed.append(message)
            if message["type"] != "http.request" or not message.get("more_body"):
                break
        await Response(status_code=204)(scope, receive, send)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "client": ("test", 1),
        "server": ("test", 80),
    }
    await IngressBodyLimitMiddleware(downstream)(scope, receive, send)
    return replayed, sent


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/events", "/v1/webhook/meta"])
async def test_declared_overflow_rejects_without_reading_or_downstream(
    path: str,
) -> None:
    replayed, sent = await _invoke(
        path,
        [{"type": "http.request", "body": b"must-not-be-read", "more_body": False}],
        headers=[("content-length".encode(), str(MAX_INGRESS_BODY_BYTES + 1).encode())],
    )

    assert replayed == []
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_disconnect_after_partial_body_never_reaches_downstream() -> None:
    replayed, sent = await _invoke(
        "/v1/events",
        [
            {"type": "http.request", "body": b'{"valid":', "more_body": True},
            {"type": "http.disconnect"},
        ],
    )

    assert replayed == []
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [1, 3])
async def test_content_length_must_equal_complete_stream(declared: int) -> None:
    replayed, sent = await _invoke(
        "/v1/events",
        [{"type": "http.request", "body": b"xx", "more_body": False}],
        headers=[(b"content-length", str(declared).encode())],
    )

    assert replayed == []
    assert sent[0]["status"] == 400


@pytest.mark.asyncio
async def test_content_length_plus_transfer_encoding_fails_closed() -> None:
    replayed, sent = await _invoke(
        "/v1/events",
        [{"type": "http.request", "body": b"x", "more_body": False}],
        headers=[(b"content-length", b"1"), (b"transfer-encoding", b"chunked")],
    )

    assert replayed == []
    assert sent[0]["status"] == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/events", "/v1/webhook/meta"])
async def test_streamed_overflow_rejects_before_downstream(path: str) -> None:
    replayed, sent = await _invoke(
        path,
        [
            {
                "type": "http.request",
                "body": b"a" * MAX_INGRESS_BODY_BYTES,
                "more_body": True,
            },
            {"type": "http.request", "body": b"b", "more_body": False},
        ],
    )

    assert replayed == []
    assert sent[0]["status"] == 413


@pytest.mark.asyncio
async def test_accepted_asgi_bytes_are_replayed_exactly_with_bounded_frames() -> None:
    original: list[Message] = [
        {"type": "http.request", "body": b'{"payload":"a', "more_body": True},
        {"type": "http.request", "body": b'b\\u00e7"}', "more_body": False},
    ]
    replayed, sent = await _invoke("/v1/events", original)

    assert replayed == [
        {
            "type": "http.request",
            "body": b'{"payload":"ab\\u00e7"}',
            "more_body": False,
        }
    ]
    assert sent[0]["status"] == 204


@pytest.mark.asyncio
async def test_many_tiny_and_empty_chunks_replay_as_one_bounded_message() -> None:
    chunks = [
        {"type": "http.request", "body": b"x" if index % 2 else b"", "more_body": True}
        for index in range(10_000)
    ]
    chunks.append({"type": "http.request", "body": b"z", "more_body": False})

    replayed, sent = await _invoke("/v1/events", chunks)

    assert len(replayed) == 1
    assert replayed[0]["body"] == b"x" * 5_000 + b"z"
    assert sent[0]["status"] == 204


@pytest.mark.parametrize(
    "headers",
    [
        [(b"content-length", b"garbage")],
        [(b"content-length", b"-1")],
        [(b"content-length", b"1"), (b"content-length", b"1")],
        [(b"content-length", b"1, 1")],
    ],
)
@pytest.mark.asyncio
async def test_malformed_or_duplicate_content_length_fails_closed(
    headers: list[tuple[bytes, bytes]],
) -> None:
    replayed, sent = await _invoke(
        "/v1/events",
        [{"type": "http.request", "body": b"x", "more_body": False}],
        headers=headers,
    )

    assert replayed == []
    assert sent[0]["status"] == 400


def test_body_cap_remains_outermost_when_tracing_is_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.api.main.settings.tracing_enabled", True)
    traced_app = create_app()
    stack = traced_app.build_middleware_stack()

    assert isinstance(stack, IngressBodyLimitMiddleware)

    middleware_types: list[type[Any]] = []
    current: Any = stack
    while hasattr(current, "app"):
        middleware_types.append(type(current))
        current = current.app

    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

    assert middleware_types.index(IngressBodyLimitMiddleware) < middleware_types.index(
        OpenTelemetryMiddleware
    )


@pytest.mark.asyncio
async def test_oversized_bodies_create_no_spans_before_outermost_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def instrument_with_test_exporter(application: FastAPI) -> None:
        FastAPIInstrumentor.instrument_app(
            application,
            excluded_urls="health,ready,live,metrics,docs,redoc",
            tracer_provider=provider,
        )

    monkeypatch.setattr("src.api.main.settings.tracing_enabled", True)
    monkeypatch.setattr(
        "src.api.main.instrument_fastapi", instrument_with_test_exporter
    )
    traced_app = create_app()
    stack = traced_app.build_middleware_stack()

    async def invoke(
        messages: list[Message], headers: list[tuple[bytes, bytes]]
    ) -> list[Message]:
        queued = list(messages)
        sent: list[Message] = []

        async def receive() -> Message:
            if queued:
                return queued.pop(0)
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            sent.append(message)

        scope: Scope = {
            "type": "http",
            "app": traced_app,
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/events",
            "raw_path": b"/v1/events",
            "query_string": b"",
            "headers": headers,
            "client": ("test", 1),
            "server": ("test", 80),
        }
        await stack(scope, receive, send)
        return sent

    accepted = await invoke(
        [{"type": "http.request", "body": b"{}", "more_body": False}],
        [(b"content-length", b"2")],
    )
    assert accepted[0]["status"] == 401
    assert exporter.get_finished_spans()
    exporter.clear()

    declared = await invoke(
        [{"type": "http.request", "body": b"unread", "more_body": False}],
        [(b"content-length", str(MAX_INGRESS_BODY_BYTES + 1).encode())],
    )
    assert declared[0]["status"] == 413
    assert exporter.get_finished_spans() == ()

    chunked = await invoke(
        [
            {
                "type": "http.request",
                "body": b"a" * MAX_INGRESS_BODY_BYTES,
                "more_body": True,
            },
            {"type": "http.request", "body": b"b", "more_body": False},
        ],
        [],
    )
    assert chunked[0]["status"] == 413
    assert exporter.get_finished_spans() == ()


@pytest.mark.parametrize("path", ["/v1/events", "/v1/webhook/meta"])
def test_oversized_main_app_request_short_circuits_all_ingress_effects(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = AsyncMock(side_effect=AssertionError("API key validation ran"))
    bindings = AsyncMock(side_effect=AssertionError("Meta binding lookup ran"))
    rate = MagicMock(side_effect=AssertionError("rate limiting ran"))
    service = MagicMock(side_effect=AssertionError("route service ran"))
    dependency_calls: list[str] = []

    async def forbidden_db_dependency() -> AsyncIterator[object]:
        dependency_calls.append("db")
        yield object()

    monkeypatch.setattr(APIKeyStore, "validate_api_key", auth)
    monkeypatch.setattr(
        "src.api.middleware.tenant_identity._load_meta_candidates", bindings
    )
    monkeypatch.setattr(rate_limit_store, "get_or_create_bucket", rate)
    monkeypatch.setattr(events, "get_event_service", service)
    main_app.dependency_overrides[get_db_session_dependency] = forbidden_db_dependency
    try:
        with TestClient(main_app) as client:
            response = client.post(
                path,
                content=b"x" * (MAX_INGRESS_BODY_BYTES + 1),
                headers={"X-API-Key": "oversized-api-key"},
            )
    finally:
        main_app.dependency_overrides.pop(get_db_session_dependency, None)

    assert response.status_code == 413
    assert response.json()["error"] == "request_body_too_large"
    assert "X-Audit-Request-ID" not in response.headers
    auth.assert_not_awaited()
    bindings.assert_not_awaited()
    rate.assert_not_called()
    service.assert_not_called()
    assert dependency_calls == []


@pytest.mark.parametrize(
    ("path", "expected_status", "expected_error"),
    [
        ("/v1/events", 400, "invalid_json"),
        ("/v1/webhook/meta", 401, "invalid_webhook_auth"),
    ],
)
def test_pathological_numeric_json_stops_at_identity_boundary(
    path: str,
    expected_status: int,
    expected_error: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = UUID("a0000000-0000-0000-0000-000000000001")
    auth = AsyncMock(
        return_value=TenantContext(
            tenant_id=tenant_id,
            api_key="pathological-api-key",
            is_active=True,
        )
    )
    bindings = AsyncMock(return_value=[])
    rate = MagicMock(side_effect=AssertionError("rate limiting ran"))
    service = MagicMock(side_effect=AssertionError("route service ran"))
    replay = MagicMock(side_effect=AssertionError("replay claim ran"))
    session = AsyncMock(side_effect=AssertionError("session create/mutate ran"))
    resolve = AsyncMock(side_effect=AssertionError("media resolve ran"))
    download = AsyncMock(side_effect=AssertionError("media download ran"))
    meta_service = MagicMock(side_effect=AssertionError("Meta pipeline service ran"))
    reply = AsyncMock(side_effect=AssertionError("reply ran"))
    handoff = AsyncMock(side_effect=AssertionError("handoff ran"))
    dependency_calls: list[str] = []

    async def forbidden_db_dependency() -> AsyncIterator[object]:
        dependency_calls.append("db")
        yield object()

    monkeypatch.setattr(APIKeyStore, "validate_api_key", auth)
    monkeypatch.setattr(
        "src.api.middleware.tenant_identity._load_meta_candidates", bindings
    )
    monkeypatch.setattr(rate_limit_store, "get_or_create_bucket", rate)
    monkeypatch.setattr(events, "get_event_service", service)
    monkeypatch.setattr("src.api.routes.webhook._claim_incoming_message", replay)
    monkeypatch.setattr("src.api.routes.webhook._get_or_create_session", session)
    monkeypatch.setattr("src.api.routes.webhook.resolve_meta_media", resolve)
    monkeypatch.setattr("src.api.routes.webhook.download_meta_media_audio", download)
    monkeypatch.setattr("src.api.routes.webhook.get_event_service", meta_service)
    monkeypatch.setattr("src.api.routes.webhook.send_meta_cloud_text", reply)
    monkeypatch.setattr("src.api.routes.webhook._meta_initiate_file_handoff", handoff)
    main_app.dependency_overrides[get_db_session_dependency] = forbidden_db_dependency
    try:
        with TestClient(main_app) as client:
            response = client.post(
                path,
                content=b'{"pathological":' + b"9" * 5_000 + b"}",
                headers={
                    "Content-Type": "application/json",
                    "X-API-Key": "pathological-api-key",
                },
            )
    finally:
        main_app.dependency_overrides.pop(get_db_session_dependency, None)

    assert response.status_code == expected_status
    assert response.json()["error"] == expected_error
    if path == "/v1/events":
        auth.assert_awaited_once_with("pathological-api-key")
        bindings.assert_not_awaited()
    else:
        auth.assert_not_awaited()
        bindings.assert_awaited_once_with(set())
    rate.assert_not_called()
    service.assert_not_called()
    replay.assert_not_called()
    session.assert_not_awaited()
    resolve.assert_not_awaited()
    download.assert_not_awaited()
    meta_service.assert_not_called()
    reply.assert_not_awaited()
    handoff.assert_not_awaited()
    assert dependency_calls == []


def test_openapi_marks_event_tenant_assertion_optional_and_deprecated() -> None:
    schema = main_app.openapi()
    event_schema = schema["components"]["schemas"]["EventRequest"]

    assert "tenant_id" not in event_schema.get("required", [])
    assert event_schema["properties"]["tenant_id"]["deprecated"] is True
