"""Focused upload, request-cap and concurrency contracts for Media Assets."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.datastructures import FormData, Headers, UploadFile
from starlette.types import Message, Scope

from src.api.middleware.auth import TenantContext
from src.api.middleware.media_limits import (
    ImmediateLimiter,
    MediaConcurrencyLimiters,
    MediaUploadLimitsMiddleware,
)
from src.api.routes import media_assets as route
from src.models.media_asset import MediaAsset
from src.models.tenant import Tenant
from src.services.media_assets import MediaAssetDomainError


def _scope(*, content_length: int | None = None) -> Scope:
    headers: list[tuple[bytes, bytes]] = []
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode("ascii")))
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/media-assets",
        "raw_path": b"/v1/media-assets",
        "query_string": b"",
        "headers": headers,
        "client": ("test", 1),
        "server": ("test", 80),
    }


async def _response_messages(
    middleware: MediaUploadLimitsMiddleware,
    scope: Scope,
    receive: Callable[[], Any],
) -> list[Message]:
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    await middleware(scope, receive, send)
    return messages


def _response_json(messages: list[Message]) -> dict[str, Any]:
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return json.loads(body)


@pytest.mark.asyncio
async def test_declared_request_above_51mb_is_rejected_without_consuming_body() -> None:
    consumed = False

    async def receive() -> Message:
        nonlocal consumed
        consumed = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        raise AssertionError("oversized request reached parser")

    middleware = MediaUploadLimitsMiddleware(
        app,
        limiters=MediaConcurrencyLimiters(uploads=4, downloads=8),
        upload_path="/v1/media-assets",
    )
    messages = await _response_messages(
        middleware, _scope(content_length=51_000_001), receive
    )

    assert messages[0]["status"] == 413
    assert _response_json(messages)["detail"]["code"] == "media_request_size_exceeded"
    assert consumed is False


@pytest.mark.asyncio
async def test_chunked_request_above_51mb_is_stopped_before_parser_receives_excess() -> (
    None
):
    chunks = iter(
        [
            {"type": "http.request", "body": b"a" * 25_500_000, "more_body": True},
            {"type": "http.request", "body": b"b" * 25_500_001, "more_body": False},
        ]
    )
    delivered = 0

    async def receive() -> Message:
        return next(chunks)  # type: ignore[return-value]

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        nonlocal delivered
        await receive()
        delivered += 1
        await receive()
        delivered += 1

    middleware = MediaUploadLimitsMiddleware(
        app,
        limiters=MediaConcurrencyLimiters(uploads=4, downloads=8),
        upload_path="/v1/media-assets",
    )
    messages = await _response_messages(middleware, _scope(), receive)

    assert messages[0]["status"] == 413
    assert delivered == 1
    assert middleware.limiters.uploads.active == 0


@pytest.mark.asyncio
async def test_fifth_active_upload_fails_immediately_without_receiving_body() -> None:
    entered = 0
    all_entered = asyncio.Event()
    release = asyncio.Event()
    receive_calls = 0

    async def receive() -> Message:
        nonlocal receive_calls
        receive_calls += 1
        return {"type": "http.request", "body": b"x", "more_body": False}

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        nonlocal entered
        await receive()
        entered += 1
        if entered == 4:
            all_entered.set()
        await release.wait()
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    middleware = MediaUploadLimitsMiddleware(
        app,
        limiters=MediaConcurrencyLimiters(uploads=4, downloads=8),
        upload_path="/v1/media-assets",
    )
    sends: list[list[Message]] = [[] for _ in range(5)]

    async def invoke(index: int) -> None:
        async def send(message: Message) -> None:
            sends[index].append(message)

        await middleware(_scope(), receive, send)

    active = [asyncio.create_task(invoke(index)) for index in range(4)]
    await all_entered.wait()
    await invoke(4)

    assert sends[4][0]["status"] == 503
    assert _response_json(sends[4])["detail"]["code"] == "media_concurrency_exceeded"
    assert receive_calls == 4

    release.set()
    await asyncio.gather(*active)
    assert middleware.limiters.uploads.active == 0


@pytest.mark.asyncio
async def test_upload_permit_is_acquired_immediately_before_first_receive() -> None:
    limiter = MediaConcurrencyLimiters(uploads=1, downloads=8)

    async def receive() -> Message:
        assert limiter.uploads.active == 1
        return {"type": "http.request", "body": b"x", "more_body": False}

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        assert limiter.uploads.active == 0
        await receive()
        assert limiter.uploads.active == 1
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    middleware = MediaUploadLimitsMiddleware(
        app,
        limiters=limiter,
        upload_path="/v1/media-assets",
    )
    messages = await _response_messages(middleware, _scope(), receive)

    assert messages[0]["status"] == 201
    assert limiter.uploads.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "validation", "database", "cancel"])
async def test_upload_permit_is_released_for_every_lifecycle(outcome: str) -> None:
    started = asyncio.Event()
    wait = asyncio.Event()

    async def receive() -> Message:
        return {"type": "http.request", "body": b"x", "more_body": False}

    async def app(scope: Scope, receive: Any, send: Any) -> None:
        await receive()
        started.set()
        if outcome == "cancel":
            await wait.wait()
        if outcome in {"validation", "database"}:
            raise RuntimeError(outcome)
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    limiter = MediaConcurrencyLimiters(uploads=1, downloads=8)
    middleware = MediaUploadLimitsMiddleware(
        app,
        limiters=limiter,
        upload_path="/v1/media-assets",
    )

    async def send(message: Message) -> None:
        return None

    task = asyncio.create_task(middleware(_scope(), receive, send))
    await started.wait()
    if outcome == "cancel":
        task.cancel()
    with (
        pytest.raises((RuntimeError, asyncio.CancelledError))
        if outcome != "success"
        else _does_not_raise()
    ):
        await task
    assert limiter.uploads.active == 0
    permit = limiter.uploads.try_acquire()
    assert permit is not None
    permit.release()


class _does_not_raise:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: object) -> bool:
        return False


def test_immediate_limiter_honors_reduced_limit() -> None:
    limiter = ImmediateLimiter(1)
    permit = limiter.try_acquire()
    assert permit is not None
    assert limiter.try_acquire() is None
    permit.release()
    assert limiter.try_acquire() is not None


class _FakeRequest:
    def __init__(self, form: FormData, headers: Headers | None = None) -> None:
        self._form = form
        self.headers = headers or Headers()

    async def form(self, **kwargs: Any) -> FormData:
        return self._form


def _raw_multipart_body(*, boundary: str, charset: str) -> bytes:
    body = bytearray()
    for name, value in (
        ("display_name", "Contrato"),
        ("original_filename", "contrato.pdf"),
        ("auto_suggest", "true"),
    ):
        body.extend(f"--{boundary}\r\n".encode("ascii"))
        body.extend(b'Content-Disposition: form-data; name="')
        body.extend(name.encode(charset))
        body.extend(b'"\r\n\r\n')
        body.extend(value.encode(charset))
        body.extend(b"\r\n")
    body.extend(f"--{boundary}\r\n".encode("ascii"))
    body.extend(b'Content-Disposition: form-data; name="')
    body.extend("file".encode(charset))
    body.extend(b'"; filename="')
    body.extend("contrato.pdf".encode(charset))
    body.extend(b'"\r\nContent-Type: application/pdf\r\n\r\n')
    body.extend(b"%PDF-1.7\nbody\r\n")
    body.extend(f"--{boundary}--\r\n".encode("ascii"))
    return bytes(body)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "mime", "category"),
    [
        (b"%PDF-1.7\nbody", "application/pdf", "pdf"),
        (b"\x89PNG\r\n\x1a\nbody", "image/png", "image"),
        (b"\xff\xd8\xff\xe0jpeg", "image/jpeg", "image"),
        (b"\xff\xfb\x90\x64mp3", "audio/mpeg", "audio"),
        (b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isommp42", "video/mp4", "video"),
    ],
)
async def test_upload_order_and_all_formats_preserve_payload_and_metadata(
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes,
    mime: str,
    category: str,
) -> None:
    order: list[str] = []
    log_entries: list[tuple[str, dict[str, Any]]] = []
    tenant_id = uuid4()
    target = Tenant(
        id=tenant_id,
        name="Clinic",
        api_key_hash="hash",
        is_active=True,
        plan_type="clinic",
        config={},
    )

    class Result:
        def scalar_one_or_none(self) -> Tenant:
            return target

    class Session:
        async def execute(self, statement: Any) -> Result:
            order.append("classify_target")
            return Result()

        def add(self, asset: MediaAsset) -> None:
            order.append("insert")
            self.asset = asset

        async def flush(self) -> None:
            return None

        async def commit(self) -> None:
            order.append("commit")

        async def rollback(self) -> None:
            order.append("rollback")

    class Guard(AbstractAsyncContextManager[Any]):
        async def __aenter__(self) -> Any:
            order.append("guard")
            return self

        async def validate_projection(self, size: int) -> None:
            order.append("projection")
            assert size == len(payload)

        async def __aexit__(self, *args: object) -> None:
            return None

    async def resolve(*args: Any, **kwargs: Any) -> UUID:
        order.append("resolve")
        return tenant_id

    monkeypatch.setattr(route, "resolve_effective_tenant", resolve)
    monkeypatch.setattr(
        route.logger,
        "info",
        lambda event, **values: log_entries.append((event, values)),
    )
    monkeypatch.setattr(
        route,
        "media_storage_capacity_guard",
        lambda *args, **kwargs: Guard(),
    )
    upload = UploadFile(
        BytesIO(payload),
        filename="transport-name.bin",
        headers=Headers({"content-type": mime}),
    )
    request = _FakeRequest(
        FormData(
            [
                ("file", upload),
                ("display_name", "  Nome exato  "),
                ("original_filename", "mídia autoral.bin"),
                ("usage_trigger", "quando solicitado"),
                ("auto_suggest", "true"),
            ]
        )
    )
    session = Session()

    asset = await route.upload_media_asset(
        request=request,  # type: ignore[arg-type]
        tenant_id=None,
        tenant=TenantContext(tenant_id=tenant_id, user_id=uuid4(), role="clinic_admin"),
        session=session,  # type: ignore[arg-type]
    )

    assert order == [
        "resolve",
        "classify_target",
        "guard",
        "projection",
        "insert",
        "commit",
    ]
    assert asset.payload == payload
    assert asset.category == category
    assert asset.mime_type == mime
    assert asset.size_bytes == len(payload)
    assert len(asset.sha256) == 32
    assert asset.display_name == "  Nome exato  "
    assert asset.original_filename == "mídia autoral.bin"
    assert asset.usage_trigger == "quando solicitado"
    assert asset.auto_suggest is True
    assert len(log_entries) == 1
    logged = repr(log_entries)
    assert "Nome exato" not in logged
    assert "mídia autoral" not in logged
    assert "quando solicitado" not in logged
    assert payload.hex() not in logged
    assert asset.sha256.hex() not in logged
    assert set(log_entries[0][1]) == {
        "tenant_id",
        "media_id",
        "category",
        "size_bytes",
        "result",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("forbidden_at", ["resolution", "classification"])
async def test_forbidden_target_returns_403_before_multipart_or_media_access(
    monkeypatch: pytest.MonkeyPatch,
    forbidden_at: str,
) -> None:
    from fastapi import HTTPException

    order: list[str] = []
    tenant_id = uuid4()

    class Request:
        async def form(self, **kwargs: Any) -> FormData:
            raise AssertionError("forbidden target reached multipart parser")

    class Result:
        def scalar_one_or_none(self) -> None:
            return None

    class Session:
        async def execute(self, statement: Any) -> Result:
            order.append("classify_target")
            assert "media_assets" not in str(statement).lower()
            return Result()

        async def rollback(self) -> None:
            order.append("rollback")

    async def resolve(*args: Any, **kwargs: Any) -> UUID:
        order.append("resolve")
        if forbidden_at == "resolution":
            raise HTTPException(status_code=403, detail={"code": "forbidden"})
        return tenant_id

    monkeypatch.setattr(route, "resolve_effective_tenant", resolve)
    session = Session()
    with pytest.raises(HTTPException) as caught:
        await route.upload_media_asset(
            request=Request(),  # type: ignore[arg-type]
            tenant_id=tenant_id,
            tenant=TenantContext(tenant_id),
            session=session,  # type: ignore[arg-type]
        )

    assert caught.value.status_code == 403
    assert order == (
        ["resolve", "rollback"]
        if forbidden_at == "resolution"
        else ["resolve", "classify_target", "rollback"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership_change", ["none", "other"])
async def test_builder_override_revalidates_ownership_after_resolution(
    monkeypatch: pytest.MonkeyPatch,
    ownership_change: str,
) -> None:
    from fastapi import HTTPException

    order: list[str] = []
    caller_tenant_id = uuid4()
    target_tenant_id = uuid4()
    builder_id = uuid4()
    current_owner: UUID | None = builder_id
    target = Tenant(
        id=target_tenant_id,
        name="Clinic",
        api_key_hash="hash",
        builder_id=builder_id,
        is_active=True,
        plan_type="clinic",
        config={},
    )

    class Request:
        async def form(self, **kwargs: Any) -> FormData:
            raise AssertionError("changed ownership reached multipart parser")

    class Result:
        def __init__(self, value: Tenant | None) -> None:
            self.value = value

        def scalar_one_or_none(self) -> Tenant | None:
            return self.value

    class Session:
        async def execute(self, statement: Any) -> Result:
            order.append("classify_target")
            statement_text = str(statement)
            builder_is_revalidated = "tenants.builder_id =" in statement_text
            if not builder_is_revalidated:
                return Result(target)
            assert builder_id in statement.compile().params.values()
            return Result(target if current_owner == builder_id else None)

        def add(self, asset: MediaAsset) -> None:
            raise AssertionError("changed ownership reached insert")

        async def rollback(self) -> None:
            order.append("rollback")

    async def resolve(*args: Any, **kwargs: Any) -> UUID:
        nonlocal current_owner
        order.append("resolve")
        current_owner = None if ownership_change == "none" else uuid4()
        target.builder_id = current_owner
        order.append("ownership_changed")
        return target_tenant_id

    async def forbidden_guard(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("changed ownership reached capacity guard")

    monkeypatch.setattr(route, "resolve_effective_tenant", resolve)
    monkeypatch.setattr(route, "media_storage_capacity_guard", forbidden_guard)

    with pytest.raises(HTTPException) as caught:
        await route.upload_media_asset(
            request=Request(),  # type: ignore[arg-type]
            tenant_id=target_tenant_id,
            tenant=TenantContext(
                caller_tenant_id,
                role="builder",
                builder_id=builder_id,
            ),
            session=Session(),  # type: ignore[arg-type]
        )

    assert caught.value.status_code == 403
    assert caught.value.detail == {"code": "media_tenant_forbidden"}
    assert order == ["resolve", "ownership_changed", "classify_target", "rollback"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "items",
    [
        [
            ("file", UploadFile(BytesIO(b"x"))),
            ("file", UploadFile(BytesIO(b"y"))),
            ("display_name", "x"),
            ("original_filename", "x"),
        ],
        [
            ("file", UploadFile(BytesIO(b"x"))),
            ("display_name", "x"),
            ("original_filename", "x"),
            ("unknown", "x"),
        ],
    ],
)
async def test_parser_rejects_duplicate_file_and_unknown_fields(
    items: list[tuple[str, Any]],
) -> None:
    with pytest.raises(Exception) as caught:
        await route._parse_upload(_FakeRequest(FormData(items)))  # type: ignore[arg-type]
    assert getattr(caught.value, "detail", {})["code"] == "media_multipart_invalid"


@pytest.mark.asyncio
async def test_file_above_50mb_rolls_back_without_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def oversized(stream: Any) -> bytes:
        raise MediaAssetDomainError("media_size_exceeded")

    monkeypatch.setattr(route, "read_media_payload_limited", oversized)
    tenant_id = uuid4()
    target = Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    async def authorized(*args: Any, **kwargs: Any) -> Tenant:
        return target

    monkeypatch.setattr(route, "_authorized_target", authorized)
    upload = UploadFile(
        BytesIO(b"stub"), headers=Headers({"content-type": "application/pdf"})
    )
    request = _FakeRequest(
        FormData(
            [("file", upload), ("display_name", "x"), ("original_filename", "x.pdf")]
        )
    )

    class Session:
        inserted = False

        async def rollback(self) -> None:
            return None

        def add(self, asset: MediaAsset) -> None:
            self.inserted = True

    session = Session()
    with pytest.raises(Exception) as caught:
        await route.upload_media_asset(
            request=request,  # type: ignore[arg-type]
            tenant_id=None,
            tenant=TenantContext(tenant_id),
            session=session,  # type: ignore[arg-type]
        )
    assert caught.value.status_code == 413
    assert session.inserted is False


def test_real_app_registers_upload_and_documents_closed_multipart_shape() -> None:
    from src.api.main import create_app

    app = create_app()
    schema = app.openapi()
    operation = schema["paths"]["/v1/media-assets"]["post"]

    assert operation["requestBody"]["content"]["multipart/form-data"]
    assert app.state.media_concurrency.uploads.limit == 4
    assert app.state.media_concurrency.downloads.limit == 8


@pytest.mark.asyncio
async def test_registered_app_accepts_authenticated_multipart_upload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timezone

    from src.api.main import create_app
    from src.repositories import get_db_session_dependency

    tenant_id = UUID("a0000000-0000-0000-0000-000000000001")
    target = Tenant(
        id=tenant_id,
        name="Real route clinic",
        api_key_hash="not-used-by-development-fallback",
        is_active=True,
        plan_type="clinic",
        config={},
    )

    class Result:
        def scalar_one_or_none(self) -> Tenant:
            return target

    class Session:
        async def execute(self, statement: Any) -> Result:
            return Result()

        def add(self, asset: MediaAsset) -> None:
            self.asset = asset

        async def flush(self) -> None:
            self.asset.id = uuid4()
            self.asset.created_at = datetime.now(timezone.utc)
            self.asset.updated_at = self.asset.created_at

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    session = Session()

    async def session_dependency() -> Any:
        yield session

    class Guard(AbstractAsyncContextManager[Any]):
        async def __aenter__(self) -> Any:
            return self

        async def validate_projection(self, size: int) -> None:
            assert size == len(b"%PDF-1.7\nbody")

        async def __aexit__(self, *args: object) -> None:
            return None

    monkeypatch.setattr(
        route,
        "media_storage_capacity_guard",
        lambda *args, **kwargs: Guard(),
    )
    app = create_app()
    app.dependency_overrides[get_db_session_dependency] = session_dependency
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/media-assets",
            headers={"X-API-Key": "test-api-key"},
            files={"file": ("transport.pdf", b"%PDF-1.7\nbody", "application/pdf")},
            data={
                "display_name": "Contrato real",
                "original_filename": "autoral.pdf",
            },
        )

    assert response.status_code == 201, response.text
    assert response.json()["original_filename"] == "autoral.pdf"
    assert response.json()["auto_suggest"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("charset", ["utf-16", "utf-7"])
async def test_raw_multipart_rejects_non_ascii_compatible_charset_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
    charset: str,
) -> None:
    from datetime import datetime, timezone

    from src.api.main import create_app
    from src.repositories import get_db_session_dependency

    tenant_id = UUID("a0000000-0000-0000-0000-000000000001")
    target = Tenant(
        id=tenant_id,
        name="Real route clinic",
        api_key_hash="not-used-by-development-fallback",
        is_active=True,
        plan_type="clinic",
        config={},
    )

    class Result:
        def scalar_one_or_none(self) -> Tenant:
            return target

    class Session:
        inserted = False

        async def execute(self, statement: Any) -> Result:
            return Result()

        def add(self, asset: MediaAsset) -> None:
            self.inserted = True
            raise AssertionError("invalid charset reached insert")

        async def flush(self) -> None:
            self.asset.id = uuid4()
            self.asset.created_at = datetime.now(timezone.utc)
            self.asset.updated_at = self.asset.created_at

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    session = Session()

    async def session_dependency() -> Any:
        yield session

    def forbidden_guard(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("invalid charset reached capacity guard")

    monkeypatch.setattr(route, "media_storage_capacity_guard", forbidden_guard)
    app = create_app()
    app.dependency_overrides[get_db_session_dependency] = session_dependency
    boundary = "media-raw-boundary"
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.post(
            "/v1/media-assets",
            headers={
                "X-API-Key": "test-api-key",
                "Content-Type": (
                    f"multipart/form-data; boundary={boundary}; charset={charset}"
                ),
            },
            content=_raw_multipart_body(boundary=boundary, charset=charset),
        )

    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "media_multipart_invalid"
    assert session.inserted is False
