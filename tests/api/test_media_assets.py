"""Focused CRUD, keyset and download contracts for Media Assets."""

from __future__ import annotations

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import ClientDisconnect, Request
from starlette.types import Message

from src.api.middleware.auth import TenantContext
from src.api.middleware.media_limits import MediaConcurrencyLimiters
from src.api.routes import media_assets as route
from src.api.schemas.media_assets import (
    MediaAssetPatchRequest,
    MediaBindableIntentsResponse,
)
from src.models.tenant import Tenant


def _asset_row(
    tenant_id: UUID,
    *,
    media_id: UUID | None = None,
    created_at: datetime | None = None,
    display_name: str = "  Nome exato  ",
    original_filename: str = "relatório clínico final.pdf",
    usage_trigger: str | None = "quando solicitado",
    auto_suggest: bool = True,
    trigger_intents: tuple[str, ...] = ("detalhes_servicos",),
    payload: bytes = b"%PDF-1.7\nbody",
    category: str = "pdf",
    mime_type: str = "application/pdf",
) -> SimpleNamespace:
    timestamp = created_at or datetime.now(timezone.utc)
    return SimpleNamespace(
        id=media_id or uuid4(),
        tenant_id=tenant_id,
        display_name=display_name,
        original_filename=original_filename,
        category=category,
        mime_type=mime_type,
        size_bytes=len(payload),
        usage_trigger=usage_trigger,
        auto_suggest=auto_suggest,
        trigger_intents=list(trigger_intents),
        created_at=timestamp,
        updated_at=timestamp,
        payload=payload,
    )


def _request(limiters: MediaConcurrencyLimiters | None = None) -> Request:
    app = SimpleNamespace(
        state=SimpleNamespace(
            media_concurrency=limiters
            or MediaConcurrencyLimiters(uploads=4, downloads=8)
        )
    )
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/v1/media-assets",
            "raw_path": b"/v1/media-assets",
            "query_string": b"",
            "headers": [],
            "client": ("test", 1),
            "server": ("test", 80),
            "app": app,
        }
    )


async def _send_response(response: Any, send: Any | None = None) -> bytes:
    messages: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def collect(message: Message) -> None:
        messages.append(message)

    await response(
        _request().scope,
        receive,
        send or collect,
    )
    return b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )


@pytest.mark.asyncio
async def test_list_uses_descending_keyset_without_selecting_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    now = datetime.now(timezone.utc)
    rows = [
        _asset_row(tenant_id, created_at=now - timedelta(seconds=index))
        for index in range(3)
    ]
    statements: list[str] = []

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    class Result:
        def all(self) -> list[SimpleNamespace]:
            return rows

    class Session:
        async def execute(self, statement: Any) -> Result:
            rendered = str(statement).lower()
            statements.append(rendered)
            assert "media_assets.payload" not in rendered
            assert (
                "order by media_assets.created_at desc, media_assets.id desc"
                in rendered
            )
            return Result()

        async def rollback(self) -> None:
            raise AssertionError("successful list must not roll back")

    monkeypatch.setattr(route, "_authorized_target", authorize)
    first = await route.list_media_assets(
        request=_request(),
        tenant_id=None,
        cursor=None,
        page_size=2,
        tenant=TenantContext(tenant_id, api_key="key"),
        session=Session(),  # type: ignore[arg-type]
    )

    assert [item.id for item in first.items] == [rows[0].id, rows[1].id]
    assert first.has_more is True
    assert first.next_cursor is not None
    position = route._decode_cursor(first.next_cursor, tenant_id)
    assert position == (rows[1].created_at, rows[1].id)
    assert len(statements) == 1


def _rendered_list_statement(
    monkeypatch: pytest.MonkeyPatch, **filters: Any
) -> tuple[str, dict[str, Any]]:
    tenant_id = uuid4()
    captured: list[Any] = []

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    class Result:
        def all(self) -> list[SimpleNamespace]:
            return []

    class Session:
        async def execute(self, statement: Any) -> Result:
            captured.append(statement)
            return Result()

        async def rollback(self) -> None:
            raise AssertionError("successful list must not roll back")

    monkeypatch.setattr(route, "_authorized_target", authorize)
    asyncio.run(
        route.list_media_assets(
            request=_request(),
            tenant_id=None,
            cursor=None,
            page_size=25,
            tenant=TenantContext(tenant_id, api_key="key"),
            session=Session(),  # type: ignore[arg-type]
            **filters,
        )
    )
    assert len(captured) == 1
    compiled = captured[0].compile()
    return str(compiled).lower(), dict(compiled.params)


def test_list_without_filters_adds_no_category_or_name_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered, _ = _rendered_list_statement(monkeypatch)

    assert "media_assets.category =" not in rendered
    assert "translate" not in rendered
    assert " like " not in rendered


def test_list_category_filter_is_an_exact_bound_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered, params = _rendered_list_statement(monkeypatch, category="image")

    assert "media_assets.category = :category_1" in rendered
    assert params["category_1"] == "image"
    assert "translate" not in rendered


def test_list_name_filter_searches_the_shown_name_and_escapes_wildcards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rendered, params = _rendered_list_statement(monkeypatch, name="Relatório_50%")

    # The catalog shows, downloads and sends original_filename, so that is the
    # only name a search may match.
    assert "translate(lower(media_assets.original_filename)" in rendered
    assert "translate(lower(media_assets.display_name)" not in rendered
    assert rendered.count("escape '\\'") == 1
    patterns = {value for value in params.values() if str(value).startswith("%")}
    assert patterns == {"%relatorio\\_50\\%%"}
    folds = [value for value in params.values() if str(value).startswith("áà")]
    assert folds == [route._NAME_SEARCH_ACCENTED + route._NAME_SEARCH_COMBINING]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    ["name=%00", "name=a%0Ab", "name=a%0Db", "name=", "category=document"],
)
async def test_list_rejects_unusable_filters_before_any_query(
    monkeypatch: pytest.MonkeyPatch, query: str
) -> None:
    from httpx import ASGITransport, AsyncClient

    from src.api.main import create_app
    from src.repositories import get_db_session_dependency

    class Session:
        async def execute(self, statement: Any) -> Any:
            raise AssertionError("an invalid filter reached the database")

        async def rollback(self) -> None:
            return None

    async def session_dependency() -> Any:
        yield Session()

    app = create_app()
    app.dependency_overrides[get_db_session_dependency] = session_dependency
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        response = await client.get(
            f"/v1/media-assets?{query}", headers={"X-API-Key": "test-api-key"}
        )

    assert response.status_code == 422, response.text


def test_list_never_logs_the_searched_name(monkeypatch: pytest.MonkeyPatch) -> None:
    log_entries: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        route.logger,
        "info",
        lambda event, **values: log_entries.append((event, values)),
    )

    _rendered_list_statement(
        monkeypatch, category="pdf", name="Paciente Joana Prontuário"
    )

    assert [event for event, _ in log_entries] == ["media_asset_list"]
    logged = repr(log_entries).lower()
    assert "joana" not in logged
    assert "prontu" not in logged


@pytest.mark.parametrize(
    ("typed", "pattern"),
    [
        ("ANTES E DEPOIS", "%antes e depois%"),
        ("Orientações", "%orientacoes%"),
        ("ÇÃO", "%cao%"),
        ("100%", "%100\\%%"),
        ("a_b", "%a\\_b%"),
        ("c:\\x", "%c:\\\\x%"),
        # macOS hands file names over decomposed (NFD): the marks are dropped.
        ("Relato\u0301rio", "%relatorio%"),
        ("ORIENTAC\u0327O\u0303ES", "%orientacoes%"),
    ],
)
def test_name_search_pattern_matches_sql_folding(typed: str, pattern: str) -> None:
    assert route._name_search_pattern(typed) == pattern


def test_name_search_fold_strings_pair_each_letter_with_its_base() -> None:
    import unicodedata

    assert len(route._NAME_SEARCH_ACCENTED) == len(route._NAME_SEARCH_PLAIN)
    for accented, plain in zip(
        route._NAME_SEARCH_ACCENTED, route._NAME_SEARCH_PLAIN, strict=True
    ):
        assert unicodedata.normalize("NFD", accented)[0].lower() == plain


def test_cursor_codec_accepts_canonical_round_trip() -> None:
    tenant_id = UUID("00000000-0000-0000-0000-000000000001")
    media_id = UUID("00000000-0000-0000-0000-000000000002")
    created_at = datetime(2026, 10, 8, 12, 34, 56, 123456, timezone.utc)

    encoded = route._encode_cursor(tenant_id, created_at, media_id)

    assert route._decode_cursor(encoded, tenant_id) == (created_at, media_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["noncanonical", "tampered", "other_tenant"])
async def test_invalid_or_cross_tenant_cursor_fails_before_media_query(
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    tenant_id = uuid4()
    created_at = datetime(2026, 10, 8, 12, 34, 56, 123456, timezone.utc)
    encoded = route._encode_cursor(
        uuid4() if kind == "other_tenant" else tenant_id,
        created_at,
        uuid4(),
    )
    if kind == "noncanonical":
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
        alias_index = alphabet.index(encoded[-1]) | 1
        alias = encoded[:-1] + alphabet[alias_index]
        padding = "=" * (-len(encoded) % 4)
        assert alias != encoded
        assert base64.urlsafe_b64decode(alias + padding) == base64.urlsafe_b64decode(
            encoded + padding
        )
        encoded = alias
    elif kind == "tampered":
        tampered = "A" if encoded[-2] != "A" else "B"
        tampered_encoded = encoded[:-2] + tampered + encoded[-1]
        padding = "=" * (-len(encoded) % 4)
        assert base64.urlsafe_b64decode(
            tampered_encoded + padding
        ) != base64.urlsafe_b64decode(encoded + padding)
        encoded = tampered_encoded

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    class Session:
        async def execute(self, statement: Any) -> None:
            raise AssertionError("invalid cursor reached media query")

        async def rollback(self) -> None:
            return None

    monkeypatch.setattr(route, "_authorized_target", authorize)
    with pytest.raises(HTTPException) as caught:
        await route.list_media_assets(
            request=_request(),
            tenant_id=None,
            cursor=encoded,
            page_size=25,
            tenant=TenantContext(tenant_id),
            session=Session(),  # type: ignore[arg-type]
        )

    assert caught.value.status_code == 400
    assert getattr(caught.value, "detail") == {"code": "media_cursor_invalid"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "mime_type", "payload", "original_filename", "extension"),
    [
        ("pdf", "application/pdf", b"%PDF-1.7\nbody", "relatório final.pdf", ".pdf"),
        # The fallback name is the server's own: ``.jpeg`` from the shared
        # table, whatever the author called the file.
        (
            "image",
            "image/jpeg",
            b"\xff\xd8\xff\xe0body",
            "foto da clínica.jpg",
            ".jpeg",
        ),
    ],
    ids=["pdf", "jpeg"],
)
async def test_download_returns_exact_attachment_and_releases_success_permit(
    monkeypatch: pytest.MonkeyPatch,
    category: str,
    mime_type: str,
    payload: bytes,
    original_filename: str,
    extension: str,
) -> None:
    tenant_id = uuid4()
    asset = _asset_row(
        tenant_id,
        category=category,
        mime_type=mime_type,
        payload=payload,
        original_filename=original_filename,
    )
    limiters = MediaConcurrencyLimiters(uploads=4, downloads=1)
    request = _request(limiters)

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    class Result:
        def one_or_none(self) -> SimpleNamespace:
            return asset

    class Session:
        async def execute(self, statement: Any) -> Result:
            assert limiters.downloads.active == 1
            assert "media_assets.payload" in str(statement).lower()
            return Result()

        async def rollback(self) -> None:
            return None

    monkeypatch.setattr(route, "_authorized_target", authorize)
    response = await route.download_media_asset(
        media_id=asset.id,
        request=request,
        tenant_id=None,
        tenant=TenantContext(tenant_id),
        session=Session(),  # type: ignore[arg-type]
    )

    assert limiters.downloads.active == 1
    assert response.media_type == asset.mime_type
    assert response.headers["content-length"] == str(len(asset.payload))
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == (
        f'attachment; filename="media-{asset.id}{extension}"; '
        f"filename*=UTF-8''{quote(asset.original_filename, safe='')}"
    )
    assert await _send_response(response) == asset.payload
    assert limiters.downloads.active == 0


@pytest.mark.asyncio
async def test_ninth_download_is_rejected_before_payload_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    limiters = MediaConcurrencyLimiters(uploads=4, downloads=8)
    request = _request(limiters)
    asset = _asset_row(tenant_id)
    query_count = 0

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    class Result:
        def one_or_none(self) -> SimpleNamespace:
            return asset

    class Session:
        async def execute(self, statement: Any) -> Result:
            nonlocal query_count
            query_count += 1
            return Result()

        async def rollback(self) -> None:
            return None

    monkeypatch.setattr(route, "_authorized_target", authorize)
    responses = [
        await route.download_media_asset(
            media_id=asset.id,
            request=request,
            tenant_id=None,
            tenant=TenantContext(tenant_id),
            session=Session(),  # type: ignore[arg-type]
        )
        for _ in range(8)
    ]
    assert limiters.downloads.active == 8

    with pytest.raises(HTTPException) as caught:
        await route.download_media_asset(
            media_id=asset.id,
            request=request,
            tenant_id=None,
            tenant=TenantContext(tenant_id),
            session=Session(),  # type: ignore[arg-type]
        )
    assert caught.value.status_code == 503
    assert getattr(caught.value, "detail") == {"code": "media_concurrency_exceeded"}
    assert query_count == 8

    for response in responses:
        await _send_response(response)
    assert limiters.downloads.active == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["database", "cancel"])
async def test_download_query_failure_or_cancellation_releases_permit(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    tenant_id = uuid4()
    limiters = MediaConcurrencyLimiters(uploads=4, downloads=1)
    request = _request(limiters)
    started = asyncio.Event()
    wait = asyncio.Event()

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    class Session:
        async def execute(self, statement: Any) -> None:
            started.set()
            if failure == "database":
                raise SQLAlchemyError("database unavailable")
            await wait.wait()

        async def rollback(self) -> None:
            return None

    monkeypatch.setattr(route, "_authorized_target", authorize)
    task = asyncio.create_task(
        route.download_media_asset(
            media_id=uuid4(),
            request=request,
            tenant_id=None,
            tenant=TenantContext(tenant_id),
            session=Session(),  # type: ignore[arg-type]
        )
    )
    await started.wait()
    if failure == "cancel":
        task.cancel()
    with pytest.raises(
        HTTPException if failure == "database" else asyncio.CancelledError
    ):
        await task
    assert limiters.downloads.active == 0


@pytest.mark.asyncio
async def test_download_disconnect_releases_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    limiters = MediaConcurrencyLimiters(uploads=4, downloads=1)
    asset = _asset_row(tenant_id)

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    class Result:
        def one_or_none(self) -> SimpleNamespace:
            return asset

    class Session:
        async def execute(self, statement: Any) -> Result:
            return Result()

        async def rollback(self) -> None:
            return None

    monkeypatch.setattr(route, "_authorized_target", authorize)
    response = await route.download_media_asset(
        media_id=asset.id,
        request=_request(limiters),
        tenant_id=None,
        tenant=TenantContext(tenant_id),
        session=Session(),  # type: ignore[arg-type]
    )

    async def disconnect(message: Message) -> None:
        if message["type"] == "http.response.body":
            raise ConnectionError("client disconnected")

    with pytest.raises(ClientDisconnect):
        await _send_response(response, disconnect)
    assert limiters.downloads.active == 0


@pytest.mark.asyncio
async def test_patch_roundtrips_metadata_without_loading_payload_and_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    current = _asset_row(tenant_id)
    updated = _asset_row(
        tenant_id,
        media_id=current.id,
        display_name=" Novo nome exato ",
        original_filename=current.original_filename,
        usage_trigger=None,
        auto_suggest=False,
    )
    statements: list[str] = []
    committed = False

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    class Result:
        def __init__(self, row: SimpleNamespace) -> None:
            self.row = row

        def one_or_none(self) -> SimpleNamespace:
            return self.row

        def one(self) -> SimpleNamespace:
            return self.row

    class Session:
        async def execute(self, statement: Any) -> Result:
            rendered = str(statement).lower()
            statements.append(rendered)
            assert "media_assets.payload" not in rendered
            return Result(current if len(statements) == 1 else updated)

        async def commit(self) -> None:
            nonlocal committed
            committed = True

        async def rollback(self) -> None:
            return None

    monkeypatch.setattr(route, "_authorized_target", authorize)
    result = await route.patch_media_asset(
        media_id=current.id,
        patch=MediaAssetPatchRequest(
            display_name=" Novo nome exato ",
            usage_trigger=None,
            auto_suggest=False,
        ),
        request=_request(),
        tenant_id=None,
        tenant=TenantContext(tenant_id),
        session=Session(),  # type: ignore[arg-type]
    )

    assert result.display_name == " Novo nome exato "
    assert result.original_filename == current.original_filename
    assert result.usage_trigger is None
    assert result.auto_suggest is False
    assert committed is True
    assert statements[0].startswith("select")
    assert statements[1].startswith("update")


@pytest.mark.asyncio
async def test_patch_invalid_metadata_rolls_back_before_update(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    current = _asset_row(tenant_id)
    calls = 0
    rolled_back = False

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    class Result:
        def one_or_none(self) -> SimpleNamespace:
            return current

    class Session:
        async def execute(self, statement: Any) -> Result:
            nonlocal calls
            calls += 1
            return Result()

        async def rollback(self) -> None:
            nonlocal rolled_back
            rolled_back = True

    monkeypatch.setattr(route, "_authorized_target", authorize)
    with pytest.raises(HTTPException) as caught:
        await route.patch_media_asset(
            media_id=current.id,
            patch=MediaAssetPatchRequest(display_name="line\nbreak"),
            request=_request(),
            tenant_id=None,
            tenant=TenantContext(tenant_id),
            session=Session(),  # type: ignore[arg-type]
        )
    assert caught.value.status_code == 422
    assert getattr(caught.value, "detail") == {"code": "media_metadata_invalid"}
    assert calls == 1
    assert rolled_back is True


@pytest.mark.asyncio
async def test_delete_commits_and_redacts_author_metadata_from_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    asset = _asset_row(tenant_id)
    committed = False
    log_entries: list[tuple[str, dict[str, Any]]] = []

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    class Result:
        def one_or_none(self) -> SimpleNamespace:
            return asset

    class Session:
        async def execute(self, statement: Any) -> Result:
            rendered = str(statement).lower()
            assert rendered.startswith("delete")
            assert "media_assets.payload" not in rendered
            return Result()

        async def commit(self) -> None:
            nonlocal committed
            committed = True

        async def rollback(self) -> None:
            return None

    monkeypatch.setattr(route, "_authorized_target", authorize)
    monkeypatch.setattr(
        route.logger,
        "info",
        lambda event, **values: log_entries.append((event, values)),
    )
    response = await route.delete_media_asset(
        media_id=asset.id,
        request=_request(),
        tenant_id=None,
        tenant=TenantContext(tenant_id),
        session=Session(),  # type: ignore[arg-type]
    )

    assert response.status_code == 204
    assert committed is True
    logged = repr(log_entries)
    assert asset.display_name not in logged
    assert asset.original_filename not in logged
    assert asset.usage_trigger not in logged
    assert asset.payload.hex() not in logged


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["patch", "delete"])
async def test_mutation_commit_failure_rolls_back_completely(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    tenant_id = uuid4()
    asset = _asset_row(tenant_id)
    rolled_back = False
    execute_calls = 0

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    class Result:
        def one_or_none(self) -> SimpleNamespace:
            return asset

        def one(self) -> SimpleNamespace:
            return asset

    class Session:
        async def execute(self, statement: Any) -> Result:
            nonlocal execute_calls
            execute_calls += 1
            return Result()

        async def commit(self) -> None:
            raise SQLAlchemyError("commit failed")

        async def rollback(self) -> None:
            nonlocal rolled_back
            rolled_back = True

    monkeypatch.setattr(route, "_authorized_target", authorize)
    with pytest.raises(HTTPException) as caught:
        if operation == "patch":
            await route.patch_media_asset(
                media_id=asset.id,
                patch=MediaAssetPatchRequest(display_name="updated"),
                request=_request(),
                tenant_id=None,
                tenant=TenantContext(tenant_id),
                session=Session(),  # type: ignore[arg-type]
            )
        else:
            await route.delete_media_asset(
                media_id=asset.id,
                request=_request(),
                tenant_id=None,
                tenant=TenantContext(tenant_id),
                session=Session(),  # type: ignore[arg-type]
            )

    assert caught.value.status_code == 503
    assert getattr(caught.value, "detail") == {"code": "media_storage_unavailable"}
    assert execute_calls == (2 if operation == "patch" else 1)
    assert rolled_back is True


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["download", "patch", "delete"])
async def test_missing_and_cross_tenant_asset_share_the_same_404(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    tenant_id = uuid4()

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    class Result:
        def one_or_none(self) -> None:
            return None

    class Session:
        async def execute(self, statement: Any) -> Result:
            rendered = str(statement).lower()
            assert "media_assets.id" in rendered
            assert "media_assets.tenant_id" in rendered
            return Result()

        async def rollback(self) -> None:
            return None

    monkeypatch.setattr(route, "_authorized_target", authorize)
    with pytest.raises(HTTPException) as caught:
        if operation == "download":
            await route.download_media_asset(
                media_id=uuid4(),
                request=_request(),
                tenant_id=None,
                tenant=TenantContext(tenant_id),
                session=Session(),  # type: ignore[arg-type]
            )
        elif operation == "patch":
            await route.patch_media_asset(
                media_id=uuid4(),
                patch=MediaAssetPatchRequest(display_name="new"),
                request=_request(),
                tenant_id=None,
                tenant=TenantContext(tenant_id),
                session=Session(),  # type: ignore[arg-type]
            )
        else:
            await route.delete_media_asset(
                media_id=uuid4(),
                request=_request(),
                tenant_id=None,
                tenant=TenantContext(tenant_id),
                session=Session(),  # type: ignore[arg-type]
            )

    assert caught.value.status_code == 404
    assert getattr(caught.value, "detail") == {"code": "media_asset_not_found"}


# ---------------------------------------------------------------------------
# envio-midia-agente Group 2: trigger_intents on PATCH, and bindable-intents.
# ---------------------------------------------------------------------------


class _PatchSession:
    """First execute: the locked current row; second: the UPDATE."""

    def __init__(self, current: SimpleNamespace, updated: SimpleNamespace) -> None:
        self.current = current
        self.updated = updated
        self.statements: list[Any] = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, statement: Any) -> Any:
        self.statements.append(statement)
        row = self.current if len(self.statements) == 1 else self.updated
        return SimpleNamespace(one_or_none=lambda: row)

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


def _updated_trigger_intents(statement: Any) -> Any:
    from sqlalchemy.dialects import postgresql

    compiled = statement.compile(dialect=postgresql.dialect())
    return compiled.params["trigger_intents"]


async def _patch(
    monkeypatch: pytest.MonkeyPatch,
    session: _PatchSession,
    patch: MediaAssetPatchRequest,
) -> Any:
    tenant_id = session.current.tenant_id

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    monkeypatch.setattr(route, "_authorized_target", authorize)
    return await route.patch_media_asset(
        media_id=session.current.id,
        patch=patch,
        request=_request(),
        tenant_id=None,
        tenant=TenantContext(tenant_id),
        session=session,  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_patch_replaces_trigger_intents_deduplicated_in_canonical_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    current = _asset_row(tenant_id, trigger_intents=("detalhes_servicos",))
    updated = _asset_row(
        tenant_id, media_id=current.id, trigger_intents=("agendar", "pre_atendimento")
    )
    session = _PatchSession(current, updated)

    result = await _patch(
        monkeypatch,
        session,
        MediaAssetPatchRequest.model_validate_json(
            '{"trigger_intents": ["pre_atendimento", "agendar", "pre_atendimento"]}'
        ),
    )

    assert _updated_trigger_intents(session.statements[1]) == [
        "agendar",
        "pre_atendimento",
    ]
    assert result.trigger_intents == ["agendar", "pre_atendimento"]
    assert session.committed is True


@pytest.mark.asyncio
async def test_patch_without_trigger_intents_keeps_them_and_empty_unbinds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    current = _asset_row(tenant_id, trigger_intents=("pedido_preco", "agendar"))
    session = _PatchSession(current, current)
    await _patch(monkeypatch, session, MediaAssetPatchRequest(display_name="novo"))
    assert _updated_trigger_intents(session.statements[1]) == [
        "pedido_preco",
        "agendar",
    ]

    session = _PatchSession(current, _asset_row(tenant_id, trigger_intents=()))
    result = await _patch(
        monkeypatch,
        session,
        MediaAssetPatchRequest.model_validate_json('{"trigger_intents": []}'),
    )
    assert _updated_trigger_intents(session.statements[1]) == []
    assert result.trigger_intents == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "code"),
    [
        ('{"trigger_intents": ["reagendar"]}', "media_trigger_intent_unknown"),
        (
            '{"trigger_intents": ["agendar", "cancelar"]}',
            "media_trigger_intent_unknown",
        ),
        ('{"trigger_intents": ["fora_do_horario"]}', "media_trigger_intent_unknown"),
        ('{"trigger_intents": ["reclamacao"]}', "media_trigger_intent_unknown"),
        # "Outras" is implied, never stored.
        ('{"trigger_intents": ["saudacao"]}', "media_trigger_intent_unknown"),
        ('{"trigger_intents": ["faq"]}', "media_trigger_intent_unknown"),
        ('{"trigger_intents": ["Agendar"]}', "media_trigger_intent_unknown"),
        ('{"trigger_intents": [""]}', "media_trigger_intent_unknown"),
        ('{"trigger_intents": null}', "media_metadata_invalid"),
        (
            '{"trigger_intents": ["faq", "faq", "faq", "faq", "faq", "faq", "faq", '
            '"faq", "faq", "faq", "faq", "faq"]}',
            "media_metadata_invalid",
        ),
    ],
)
async def test_patch_refuses_unknown_or_null_trigger_intents_before_update(
    monkeypatch: pytest.MonkeyPatch, body: str, code: str
) -> None:
    current = _asset_row(uuid4())
    session = _PatchSession(current, current)

    with pytest.raises(HTTPException) as caught:
        await _patch(
            monkeypatch, session, MediaAssetPatchRequest.model_validate_json(body)
        )

    assert caught.value.status_code == 422
    assert getattr(caught.value, "detail") == {"code": code}
    assert len(session.statements) == 1  # the locked read; no UPDATE
    assert session.rolled_back is True
    assert session.committed is False


@pytest.mark.asyncio
async def test_registered_app_patch_answers_the_stable_unknown_intent_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from httpx import ASGITransport, AsyncClient

    from src.api.main import create_app
    from src.repositories import get_db_session_dependency

    tenant_id = UUID("a0000000-0000-0000-0000-000000000001")
    current = _asset_row(tenant_id)
    session = _PatchSession(current, current)

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    async def session_dependency() -> Any:
        yield session

    monkeypatch.setattr(route, "_authorized_target", authorize)
    app = create_app()
    app.dependency_overrides[get_db_session_dependency] = session_dependency
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        refused = await client.patch(
            f"/v1/media-assets/{current.id}",
            headers={"X-API-Key": "test-api-key"},
            json={"trigger_intents": ["saudacao", "cancelar"]},
        )
    assert refused.status_code == 422, refused.text
    assert refused.json() == {"detail": {"code": "media_trigger_intent_unknown"}}
    assert len(session.statements) == 1


# The credential bundles of the readiness matrix (Decision 8).
_META_READY = {
    "whatsapp_provider": "meta_cloud",
    "meta_phone_number_id": "1234567890",
    "meta_permanent_token": "meta-permanent-token",
    "meta_app_secret": "meta-app-secret",
}
_ZAPI_READY = {
    "whatsapp_provider": "zapi",
    "zapi_instance_id": "zapi-instance",
    "zapi_token": "zapi-token",
    "zapi_inbound_token_digest": "ab" * 32,
}
_UAZAPI_COMPLETE = {
    "whatsapp_provider": "uazapi",
    "uazapi_api_url": "https://uazapi.example",
    "uazapi_instance_name": "clinic",
    "uazapi_token": "uazapi-token",
}
_EVOLUTION_COMPLETE = {
    "whatsapp_provider": "evolution",
    "evolution_api_url": "https://evolution.example",
    "evolution_instance_name": "clinic",
    "evolution_api_key": "evolution-key",
}
_PROVIDER_CASES: dict[str, tuple[dict[str, Any] | None, bool]] = {
    "meta_with_app_secret": (_META_READY, True),
    "meta_without_app_secret": (
        {k: v for k, v in _META_READY.items() if k != "meta_app_secret"},
        False,
    ),
    "zapi_with_inbound_token": (_ZAPI_READY, True),
    "zapi_without_inbound_token": (
        {k: v for k, v in _ZAPI_READY.items() if k != "zapi_inbound_token_digest"},
        False,
    ),
    "uazapi_complete": (_UAZAPI_COMPLETE, False),
    "evolution_complete": (_EVOLUTION_COMPLETE, False),
    "no_clinic_config": (None, False),
}


class _CredentialsSession:
    def __init__(self, credentials: dict[str, Any] | None) -> None:
        self.credentials = credentials
        self.statements: list[str] = []
        self.rolled_back = False

    async def execute(self, statement: Any) -> Any:
        rendered = str(statement).lower()
        self.statements.append(rendered)
        assert "media_assets" not in rendered
        return SimpleNamespace(scalar_one_or_none=lambda: self.credentials)

    async def rollback(self) -> None:
        self.rolled_back = True


def _install_flag_document(monkeypatch: pytest.MonkeyPatch, flag: str) -> list[str]:
    """The real AppConfig Agent client (``APPCONFIG_SOURCE=agent``) answering
    one fake flag document over a mock transport; records each read.

    The Agent client parses with the same fail-closed reader as the SDK one
    and never logs, so no test here becomes the first user of the AppConfig
    module logger (structlog caches loggers on first use, and a later
    ``configure_logging`` would leave a cached one outside ``capture_logs``).
    """

    import httpx

    from src.config.settings import settings
    from src.services import egress_policy

    documents: dict[str, dict[str, Any]] = {
        "on": {"agent_media_send_enabled": {"enabled": True}},
        "off": {"agent_media_send_enabled": {"enabled": False}},
        # Every other flag open: only the media flag decides.
        "absent": {
            "financial_egress_enabled": {"enabled": True},
            "uazapi_egress_enabled": {"enabled": True},
        },
        # uazapi-webhook-auth: what a Uazapi clinic also needs.
        "uazapi_open": {
            "uazapi_webhook_auth_enforced": {"enabled": True},
            "uazapi_egress_enabled": {"enabled": True},
            "agent_media_send_enabled": {"enabled": True},
        },
        "uazapi_egress_closed": {
            "uazapi_webhook_auth_enforced": {"enabled": True},
            "uazapi_egress_enabled": {"enabled": False},
            "agent_media_send_enabled": {"enabled": True},
        },
        "uazapi_shadow": {
            "uazapi_webhook_auth_enforced": {"enabled": False},
            "uazapi_egress_enabled": {"enabled": True},
            "agent_media_send_enabled": {"enabled": True},
        },
    }
    monkeypatch.setattr(settings, "appconfig_source", "agent")
    monkeypatch.setattr(settings, "appconfig_enabled", True)
    monkeypatch.setattr(settings, "appconfig_origin", "http://127.0.0.1:2772")
    monkeypatch.setattr(settings, "appconfig_application", "neurostack")
    monkeypatch.setattr(settings, "appconfig_environment", "production")
    monkeypatch.setattr(settings, "appconfig_profile", "egress-flags")
    reads: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        reads.append(request.url.path)
        return httpx.Response(200, json=documents[flag])

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    original = egress_policy.AppConfigClient
    monkeypatch.setattr(
        egress_policy,
        "AppConfigClient",
        lambda config: original(config, client_factory=factory),
    )
    return reads


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["on", "off", "absent"])
@pytest.mark.parametrize("provider_case", sorted(_PROVIDER_CASES))
async def test_bindable_intents_delivery_enabled_follows_flag_and_readiness(
    monkeypatch: pytest.MonkeyPatch, provider_case: str, flag: str
) -> None:
    credentials, ready = _PROVIDER_CASES[provider_case]
    reads = _install_flag_document(monkeypatch, flag)
    tenant_id = uuid4()
    target = Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return target

    monkeypatch.setattr(route, "_authorized_target", authorize)
    session = _CredentialsSession(credentials)

    response = await route.get_media_bindable_intents(
        request=_request(),
        tenant_id=None,
        tenant=TenantContext(tenant_id, api_key="key"),
        session=session,  # type: ignore[arg-type]
    )

    assert response.delivery_enabled is (ready and flag == "on")
    # The flag is consulted (one Agent read) only for a provider that
    # delivers production turns.  A Uazapi clinic first reads its webhook
    # authentication flag (uazapi-webhook-auth), closed in these documents,
    # so nothing else is read for it.
    assert reads == (
        [
            "/applications/neurostack/environments/production"
            "/configurations/egress-flags"
        ]
        if ready or provider_case == "uazapi_complete"
        else []
    )
    assert response.intents == [
        "interesse_tratamento",
        "pedido_preco",
        "comparacao_tratamentos",
        "objecao_vou_pensar",
        "agendar",
        "detalhes_servicos",
        "pre_atendimento",
        "cultura_historia",
        "qualificacao_publico",
        "informacoes_profissionais",
        "pos_atendimento_duvida",
    ]
    # The "Quando enviar" checkboxes, in screen order, and the implied
    # "Outras".
    assert [topic.model_dump() for topic in response.topics] == [
        {"id": "pedido_preco", "intents": ["pedido_preco"]},
        {"id": "agendar", "intents": ["agendar", "pre_atendimento"]},
        {
            "id": "interesse_tratamento",
            "intents": [
                "interesse_tratamento",
                "comparacao_tratamentos",
                "objecao_vou_pensar",
                "detalhes_servicos",
                "cultura_historia",
                "qualificacao_publico",
                "informacoes_profissionais",
            ],
        },
        {"id": "pos_atendimento_duvida", "intents": ["pos_atendimento_duvida"]},
    ]
    assert response.implicit_intents == ["saudacao", "faq"]
    assert response.max_bytes_by_category.model_dump() == {
        "video": 16_000_000,
        "audio": 16_000_000,
        "pdf": 20_000_000,
        "image": 5_000_000,
    }
    # One read of the target's credentials, revalidated in the same statement.
    assert len(session.statements) == 1
    assert "from clinic_configs" in session.statements[0]
    assert "clinic_configs.api_credentials" in session.statements[0]
    assert "exists" in session.statements[0]
    assert session.rolled_back is True  # the read ends before the flag read


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_case", sorted(_PROVIDER_CASES))
async def test_bindable_intents_flag_is_read_only_after_the_transaction_ends(
    monkeypatch: pytest.MonkeyPatch, provider_case: str
) -> None:
    """The flag read (an Agent HTTP call) never happens while the session
    holds a connection: the read transaction ends first, and a provider that
    is not ready never reads the flag at all."""

    credentials, ready = _PROVIDER_CASES[provider_case]
    # One timeline: the Agent transport appends each flag read to it, and
    # the session appends its statements and its rollback.
    timeline = _install_flag_document(monkeypatch, "on")
    tenant_id = uuid4()
    target = Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return target

    class Session(_CredentialsSession):
        async def execute(self, statement: Any) -> Any:
            timeline.append("execute")
            return await super().execute(statement)

        async def rollback(self) -> None:
            timeline.append("rollback")
            await super().rollback()

    monkeypatch.setattr(route, "_authorized_target", authorize)
    response = await route.get_media_bindable_intents(
        request=_request(),
        tenant_id=None,
        tenant=TenantContext(tenant_id, api_key="key"),
        session=Session(credentials),  # type: ignore[arg-type]
    )

    flag_read = (
        "/applications/neurostack/environments/production/configurations/egress-flags"
    )
    # A Uazapi clinic reads its (closed) webhook authentication flag instead.
    assert timeline == (
        ["execute", "rollback", flag_read]
        if ready or provider_case == "uazapi_complete"
        else ["execute", "rollback"]
    )
    assert response.delivery_enabled is ready


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flag", "reads", "enabled"),
    [
        ("uazapi_open", 3, True),
        ("uazapi_egress_closed", 2, False),
        ("uazapi_shadow", 1, False),
    ],
)
async def test_bindable_intents_promise_uazapi_only_enforced_and_with_open_egress(
    monkeypatch: pytest.MonkeyPatch, flag: str, reads: int, enabled: bool
) -> None:
    """uazapi-webhook-auth: a Uazapi clinic's verified turns are production
    only while its webhook authentication is enforced, and its file needs
    open egress.  Every flag is read after the read transaction ended, each
    only when the previous one is open, the media flag last."""

    timeline = _install_flag_document(monkeypatch, flag)
    tenant_id = uuid4()
    target = Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return target

    class Session(_CredentialsSession):
        async def execute(self, statement: Any) -> Any:
            timeline.append("execute")
            return await super().execute(statement)

        async def rollback(self) -> None:
            timeline.append("rollback")
            await super().rollback()

    monkeypatch.setattr(route, "_authorized_target", authorize)
    response = await route.get_media_bindable_intents(
        request=_request(),
        tenant_id=None,
        tenant=TenantContext(tenant_id, api_key="key"),
        session=Session(_UAZAPI_COMPLETE),  # type: ignore[arg-type]
    )

    flag_read = (
        "/applications/neurostack/environments/production/configurations/egress-flags"
    )
    assert timeline == ["execute", "rollback", *[flag_read] * reads]
    assert response.delivery_enabled is enabled
    assert response.sendable_mime_types == _SENDABLE_BY_PROVIDER_CASE["uazapi_complete"]


@pytest.mark.asyncio
async def test_bindable_intents_limits_and_intents_come_from_the_single_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from neurostack_core.protocol import MEDIA_BINDABLE_INTENT_SEQUENCE
    from src.services.media_assets import MEDIA_CATEGORY_MAX_BYTES

    tenant_id = uuid4()

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    monkeypatch.setattr(route, "_authorized_target", authorize)
    response = await route.get_media_bindable_intents(
        request=_request(),
        tenant_id=None,
        tenant=TenantContext(tenant_id, api_key="key"),
        session=_CredentialsSession(None),  # type: ignore[arg-type]
    )

    assert response.intents == list(MEDIA_BINDABLE_INTENT_SEQUENCE)
    assert response.max_bytes_by_category.model_dump() == dict(MEDIA_CATEGORY_MAX_BYTES)
    # AppConfig is unconfigured in the test settings: the flag reads closed.
    assert response.delivery_enabled is False
    # No clinic config, so no provider: nothing is sendable.
    assert response.sendable_mime_types == []


_SENT_BY_EVERY_PROVIDER = [
    "application/pdf",
    "audio/mpeg",
    "image/jpeg",
    "image/png",
    "video/mp4",
]
_META_SENDABLE = sorted([*_SENT_BY_EVERY_PROVIDER, "audio/ogg", "audio/mp4"])
# The formats the clinic's provider sends, whatever its readiness: the screen
# shows the format warning only while delivery_enabled is true.
_SENDABLE_BY_PROVIDER_CASE: dict[str, list[str]] = {
    "meta_with_app_secret": _META_SENDABLE,
    "meta_without_app_secret": _META_SENDABLE,
    "zapi_with_inbound_token": _SENT_BY_EVERY_PROVIDER,
    "zapi_without_inbound_token": _SENT_BY_EVERY_PROVIDER,
    "uazapi_complete": _SENT_BY_EVERY_PROVIDER,
    "evolution_complete": _SENT_BY_EVERY_PROVIDER,
    "no_clinic_config": [],
}


async def _bindable_for(
    monkeypatch: pytest.MonkeyPatch, credentials: Any
) -> MediaBindableIntentsResponse:
    tenant_id = uuid4()

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    monkeypatch.setattr(route, "_authorized_target", authorize)
    return await route.get_media_bindable_intents(
        request=_request(),
        tenant_id=None,
        tenant=TenantContext(tenant_id, api_key="key"),
        session=_CredentialsSession(credentials),  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_case", sorted(_PROVIDER_CASES))
async def test_bindable_intents_sendable_mime_types_follow_the_clinic_provider(
    monkeypatch: pytest.MonkeyPatch, provider_case: str
) -> None:
    """Meta sends OGG-Opus and M4A; Z-API, uazapi and Evolution do not
    (Z-API documents only audio/mpeg); no provider sends nothing."""

    from src.services.media_send_body import sendable_mime_types

    assert set(_SENDABLE_BY_PROVIDER_CASE) == set(_PROVIDER_CASES)
    credentials, _ready = _PROVIDER_CASES[provider_case]
    response = await _bindable_for(monkeypatch, credentials)

    assert response.sendable_mime_types == _SENDABLE_BY_PROVIDER_CASE[provider_case]
    if credentials is not None:
        delivery_name = {"meta_cloud": "meta"}.get(
            credentials["whatsapp_provider"], credentials["whatsapp_provider"]
        )
        # The same set the provider's sender validates against.
        assert response.sendable_mime_types == sorted(
            sendable_mime_types(delivery_name)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credentials",
    [
        {"whatsapp_provider": "dialog360"},
        {"whatsapp_provider": "twilio"},
        {"whatsapp_provider": "meta"},
        {"whatsapp_provider": None},
        {"whatsapp_provider": ["zapi"]},
        {},
        ["zapi"],
    ],
    ids=["dialog360", "twilio", "delivery-name", "null", "list", "empty", "not-a-map"],
)
async def test_bindable_intents_sendable_mime_types_empty_without_a_media_provider(
    monkeypatch: pytest.MonkeyPatch, credentials: Any
) -> None:
    response = await _bindable_for(monkeypatch, credentials)

    assert response.sendable_mime_types == []
    assert response.delivery_enabled is False


@pytest.mark.asyncio
async def test_bindable_intents_storage_failure_is_503_and_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    rolled_back: list[bool] = []

    async def authorize(*args: Any, **kwargs: Any) -> Tenant:
        return Tenant(id=tenant_id, name="Clinic", api_key_hash="h", config={})

    class Session:
        async def execute(self, statement: Any) -> Any:
            raise SQLAlchemyError("database down")

        async def rollback(self) -> None:
            rolled_back.append(True)

    monkeypatch.setattr(route, "_authorized_target", authorize)
    with pytest.raises(HTTPException) as caught:
        await route.get_media_bindable_intents(
            request=_request(),
            tenant_id=None,
            tenant=TenantContext(tenant_id, api_key="key"),
            session=Session(),  # type: ignore[arg-type]
        )
    assert caught.value.status_code == 503
    assert getattr(caught.value, "detail") == {"code": "media_storage_unavailable"}
    assert rolled_back == [True]


@pytest.mark.parametrize("egress", [False, True], ids=["egress_closed", "egress_open"])
@pytest.mark.parametrize("enforced", [False, True], ids=["shadow", "enforced"])
@pytest.mark.parametrize("case", sorted(_PROVIDER_CASES))
def test_media_delivery_provider_ready_matrix(
    case: str, enforced: bool, egress: bool
) -> None:
    from src.services.api_credentials import (
        credential_status,
        media_delivery_provider_ready,
    )

    credentials, ready = _PROVIDER_CASES[case]
    # The Uazapi flags decide for a Uazapi clinic alone: its verified turns
    # are production only while enforced, and its file needs open egress.
    if case == "uazapi_complete":
        ready = enforced and egress
    assert (
        media_delivery_provider_ready(
            credentials or {},
            uazapi_auth_enforced=enforced,
            uazapi_egress_enabled=egress,
        )
        is ready
    )
    # Derived from the readiness the credentials panel shows, never wider.
    if ready:
        assert credential_status(credentials or {})["whatsapp_readiness"] == "active"


@pytest.mark.parametrize(
    "credentials",
    [
        None,
        [],
        "meta_cloud",
        {**_META_READY, "whatsapp_provider": ["meta_cloud"]},
        {**_META_READY, "whatsapp_provider": "meta"},
        {**_META_READY, "meta_app_secret": ""},
        {**_META_READY, "meta_permanent_token": None},
        {**_ZAPI_READY, "zapi_inbound_token_digest": ""},
        {**_ZAPI_READY, "zapi_instance_id": ""},
        {**_ZAPI_READY, "whatsapp_provider": "ZAPI"},
        {"whatsapp_provider": "dialog360"},
        {"whatsapp_provider": "twilio"},
        {**_UAZAPI_COMPLETE, "uazapi_token": ""},
        {**_UAZAPI_COMPLETE, "uazapi_api_url": None},
        {**_UAZAPI_COMPLETE, "uazapi_instance_name": ""},
        {**_UAZAPI_COMPLETE, "whatsapp_provider": "UAZAPI"},
        _EVOLUTION_COMPLETE,
    ],
)
def test_media_delivery_provider_ready_is_false_for_anything_else(
    credentials: Any,
) -> None:
    """Even with both Uazapi flags open."""

    from src.services.api_credentials import media_delivery_provider_ready

    assert (
        media_delivery_provider_ready(
            credentials, uazapi_auth_enforced=True, uazapi_egress_enabled=True
        )
        is False
    )


@pytest.mark.parametrize("value", [1, "true", None])
def test_media_delivery_provider_ready_needs_exactly_true_flags(value: Any) -> None:
    from src.services.api_credentials import media_delivery_provider_ready

    for enforced, egress in ((value, True), (True, value)):
        assert (
            media_delivery_provider_ready(
                _UAZAPI_COMPLETE,
                uazapi_auth_enforced=enforced,
                uazapi_egress_enabled=egress,
            )
            is False
        )


def test_the_ready_providers_are_the_authenticated_webhook_kinds() -> None:
    """Drift alarm: the providers media may use are exactly the ingress kinds
    the credit principal counts as authenticated (Meta HMAC, Z-API capability
    token), plus the kinds it counts as authenticated per request only
    (``inbound_verified``: a Uazapi envelope token matching the stored
    instance token), which are ready only behind their flags.  When another
    provider is authenticated this fails and the readiness single point must
    be revisited with it."""

    from src.services import api_credentials, credit_principal

    provider_of_kind = {"meta": "meta_cloud", "zapi": "zapi", "uazapi": "uazapi"}
    authenticated_webhook_kinds = {"meta", "zapi", "uazapi", "evolution"} - set(
        credit_principal._UNAUTHENTICATED_KINDS
    )
    assert {
        provider_of_kind[kind] for kind in authenticated_webhook_kinds
    } == api_credentials._PRODUCTION_ORIGIN_WHATSAPP_PROVIDERS
    assert {
        provider_of_kind[kind] for kind in credit_principal._INBOUND_VERIFIABLE_KINDS
    } == api_credentials._VERIFIED_ORIGIN_WHATSAPP_PROVIDERS
    assert not (
        api_credentials._VERIFIED_ORIGIN_WHATSAPP_PROVIDERS
        & api_credentials._PRODUCTION_ORIGIN_WHATSAPP_PROVIDERS
    )
