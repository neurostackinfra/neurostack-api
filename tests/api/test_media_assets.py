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
from src.api.schemas.media_assets import MediaAssetPatchRequest
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
    payload: bytes = b"%PDF-1.7\nbody",
) -> SimpleNamespace:
    timestamp = created_at or datetime.now(timezone.utc)
    return SimpleNamespace(
        id=media_id or uuid4(),
        tenant_id=tenant_id,
        display_name=display_name,
        original_filename=original_filename,
        category="pdf",
        mime_type="application/pdf",
        size_bytes=len(payload),
        usage_trigger=usage_trigger,
        auto_suggest=auto_suggest,
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
async def test_download_returns_exact_attachment_and_releases_success_permit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    asset = _asset_row(tenant_id)
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
        f'attachment; filename="media-{asset.id}.pdf"; '
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
