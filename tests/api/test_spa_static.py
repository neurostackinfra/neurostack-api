"""Group 6c: the built frontend is served from exact, startup-computed paths."""

from __future__ import annotations

import errno
import json
import os
import re
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from pathlib import Path
from types import MethodType
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI, WebSocket
from fastapi.routing import APIRoute
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Host, Match, Mount, Route, Router

from src.api import main as main_module
from src.api import spa
from src.api.main import create_app
from src.api.routes import health
from src.api.spa import (
    SPA_CLIENT_ROUTES,
    TRACE_DETAIL_PATTERN,
    SpaRegistry,
    SpaUnavailable,
    install_spa,
    load_build,
)
from tests.api._spa_support import (
    APPROVED_ASSET_CACHE,
    APPROVED_CSP,
    APPROVED_INDEX_CACHE,
    APPROVED_SECURITY_HEADERS,
    APPROVED_TOP_LEVEL_CACHE,
    ASSET_FILES,
    EXPECTED_MEDIA_TYPES,
    HOSTILE_ASSET_PATHS,
    HOSTILE_TOP_LEVEL_PATHS,
    INDEX_HTML,
    TOP_LEVEL_FILES,
    AsgiResponse,
    asgi_lifespan,
    asgi_request,
    bearer_header,
    build_dist,
    configure_spa,
    etag_of,
    route_signature,
)

ROOT = Path(__file__).resolve().parents[2]
APP_TSX = ROOT / "frontend" / "artifacts" / "neurostack" / "src" / "App.tsx"
ROUTES_FIXTURE = ROOT / "tests" / "fixtures" / "spa_client_routes.json"
REAL_DIST_ENV = "SPA_REAL_DIST_DIR"

STATIC_ROUTES = tuple(route for route in SPA_CLIENT_ROUTES if ":" not in route)
TWINS = tuple(route + "/" for route in STATIC_ROUTES if route not in {"/", "/traces"})
TRACE_IDS = ("abc", "6f1c2a4e-1b2c-4d5e-8f90-123456789abc", "A_b-9", "x" * 128)
SPA_PAGES = STATIC_ROUTES + TWINS + tuple(f"/traces/{tid}" for tid in TRACE_IDS)


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    return build_dist(tmp_path)


@pytest.fixture
def spa_app(monkeypatch: pytest.MonkeyPatch, dist: Path) -> FastAPI:
    configure_spa(monkeypatch, dist)
    application = create_app()
    assert application.state.spa.enabled
    return application


@pytest.fixture
def off_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    configure_spa(monkeypatch, None)
    return create_app()


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def _record(self, level: str, event: str, **fields: Any) -> None:
        self.calls.append((level, event, fields))

    def info(self, event: str, **fields: Any) -> None:
        self._record("info", event, **fields)

    def warning(self, event: str, **fields: Any) -> None:
        self._record("warning", event, **fields)

    def error(self, event: str, **fields: Any) -> None:
        self._record("error", event, **fields)

    def levels(self, level: str) -> list[tuple[str, dict[str, Any]]]:
        return [(event, fields) for lvl, event, fields in self.calls if lvl == level]


@pytest.fixture
def spa_log(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    recorder = _Recorder()
    monkeypatch.setattr(spa, "logger", recorder)
    return recorder


def _assert_spa_headers(
    response: AsgiResponse, cache_control: str, body: bytes
) -> None:
    assert response.header("cache-control") == cache_control
    assert response.header("etag") == etag_of(body)
    for name, value in APPROVED_SECURITY_HEADERS.items():
        assert response.header(name) == value, name


# --------------------------------------------------------------------------
# Client-route registry
# --------------------------------------------------------------------------


def _app_tsx_routes() -> tuple[str, ...]:
    source = APP_TSX.read_text(encoding="utf-8")
    paths = re.findall(r'<Route\s+path="([^"]+)"', source)
    return tuple(path for path in paths if path != "/privacidade/:clinicId")


def test_client_routes_are_rederived_from_app_tsx() -> None:
    derived = _app_tsx_routes()

    assert derived == SPA_CLIENT_ROUTES
    assert "/privacidade/:clinicId" not in SPA_CLIENT_ROUTES
    assert [route for route in SPA_CLIENT_ROUTES if ":" in route] == ["/traces/:id"]


def test_client_routes_match_shared_fixture() -> None:
    loaded = json.loads(ROUTES_FIXTURE.read_text(encoding="utf-8"))
    assert sorted(loaded) == ["description", "dynamic", "excluded", "static"]

    dynamic = loaded["dynamic"]
    assert [entry["route"] for entry in dynamic] == ["/traces/:id"]
    assert dynamic[0]["pattern"] == TRACE_DETAIL_PATTERN.pattern

    declared = [*loaded["static"], *(entry["route"] for entry in dynamic)]
    assert len(declared) == len(set(declared))
    assert set(declared) == set(SPA_CLIENT_ROUTES)
    assert len(declared) == len(SPA_CLIENT_ROUTES)

    # Routes the API serves itself must never become SPA routes.
    assert loaded["excluded"] == ["/privacidade/:clinicId"]
    assert not set(loaded["excluded"]) & set(SPA_CLIENT_ROUTES)


def test_registry_is_exactly_the_client_routes_twins_and_clean_build_files(
    spa_app: FastAPI,
) -> None:
    registry: SpaRegistry = spa_app.state.spa

    assert registry.pages == frozenset(STATIC_ROUTES + TWINS)
    assert "/traces/" not in registry.pages
    assert sorted(registry.files) == sorted(EXPECTED_MEDIA_TYPES)
    assert "/index.html" not in registry.files


# --------------------------------------------------------------------------
# Pages, assets and top-level files
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", SPA_PAGES)
async def test_client_routes_serve_index_from_memory_with_headers(
    spa_app: FastAPI, dist: Path, path: str
) -> None:
    (dist / "index.html").write_bytes(b"changed on disk after startup")

    response = await asgi_request(spa_app, "GET", path)

    assert response.status == 200
    assert response.body == INDEX_HTML
    assert response.header("content-type") == "text/html; charset=utf-8"
    _assert_spa_headers(response, APPROVED_INDEX_CACHE, INDEX_HTML)


@pytest.mark.parametrize(
    "if_none_match",
    [
        etag_of(INDEX_HTML),
        "W/" + etag_of(INDEX_HTML),
        '"other", ' + etag_of(INDEX_HTML),
        "*",
    ],
)
async def test_index_revalidation_returns_304(
    spa_app: FastAPI, if_none_match: str
) -> None:
    response = await asgi_request(
        spa_app,
        "GET",
        "/billing",
        headers=[(b"if-none-match", if_none_match.encode("ascii"))],
    )

    assert response.status == 304
    assert response.body == b""
    _assert_spa_headers(response, APPROVED_INDEX_CACHE, INDEX_HTML)


async def test_stale_etag_gets_full_index(spa_app: FastAPI) -> None:
    response = await asgi_request(
        spa_app, "GET", "/billing", headers=[(b"if-none-match", b'"stale"')]
    )

    assert response.status == 200
    assert response.body == INDEX_HTML


@pytest.mark.parametrize("path", sorted(EXPECTED_MEDIA_TYPES))
async def test_build_files_are_served_with_exact_types_and_cache(
    spa_app: FastAPI, path: str
) -> None:
    name = path.rsplit("/", 1)[1]
    body = ASSET_FILES.get(name) if path.startswith("/assets/") else None
    body = body if body is not None else TOP_LEVEL_FILES[name]
    cache = (
        APPROVED_ASSET_CACHE
        if path.startswith("/assets/")
        else APPROVED_TOP_LEVEL_CACHE
    )

    response = await asgi_request(spa_app, "GET", path)
    revalidated = await asgi_request(
        spa_app,
        "GET",
        path,
        headers=[(b"if-none-match", etag_of(body).encode("ascii"))],
    )

    assert response.status == 200
    assert response.body == body
    assert response.header("content-type") == EXPECTED_MEDIA_TYPES[path]
    _assert_spa_headers(response, cache, body)
    assert revalidated.status == 304
    assert revalidated.body == b""


@pytest.mark.parametrize(
    "path, revalidated, immutable",
    [
        ("/billing", True, False),
        ("/assets/index-AbC123.js", False, True),
        ("/favicon.svg", False, False),
    ],
)
async def test_cache_policy_matches_design_intent(
    spa_app: FastAPI, path: str, revalidated: bool, immutable: bool
) -> None:
    """index.html revalidates, hashed assets are immutable, top-level is not."""

    cache = (await asgi_request(spa_app, "GET", path)).header("cache-control")

    assert cache is not None
    directives = {part.strip() for part in cache.split(",")}
    assert ("no-cache" in directives or "no-store" in directives) is revalidated
    assert ("immutable" in directives) is immutable
    if revalidated:
        assert not any(d.startswith(("max-age", "s-maxage")) for d in directives)
    if immutable:
        assert "max-age=31536000" in directives


@pytest.mark.parametrize(
    "path, body",
    [
        ("/billing", INDEX_HTML),
        ("/assets/index-AbC123.js", ASSET_FILES["index-AbC123.js"]),
        ("/favicon.svg", TOP_LEVEL_FILES["favicon.svg"]),
    ],
)
async def test_head_returns_headers_without_body(
    spa_app: FastAPI, path: str, body: bytes
) -> None:
    response = await asgi_request(spa_app, "HEAD", path)

    assert response.status == 200
    assert response.body == b""
    assert response.header("content-length") == str(len(body))
    assert response.header("etag") == etag_of(body)


@pytest.mark.parametrize("path", HOSTILE_ASSET_PATHS + HOSTILE_TOP_LEVEL_PATHS)
async def test_hostile_build_entries_are_never_served(
    spa_app: FastAPI, path: str
) -> None:
    anonymous = await asgi_request(spa_app, "GET", path)
    authenticated = await asgi_request(spa_app, "GET", path, headers=[bearer_header()])

    assert not spa_app.state.spa.is_public(path)
    assert anonymous.status == 401
    assert authenticated.status == 404
    assert b"secret" not in authenticated.body


def test_each_symlink_layer_refuses_links_on_its_own(tmp_path: Path) -> None:
    """The scan (lstat) and the read (O_NOFOLLOW + fstat) both refuse links."""

    (tmp_path / "real.js").write_bytes(b"real")
    (tmp_path / "link.js").symlink_to(tmp_path / "real.js")
    dir_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        servable, skipped = spa._scan_files(dir_fd)
        assert (servable, skipped) == (["real.js"], ["link.js"])
        assert spa._read_regular_file("link.js", dir_fd) is None
        assert spa._read_regular_file("real.js", dir_fd) == b"real"
    finally:
        os.close(dir_fd)


async def test_skipped_build_entries_are_logged_once(
    monkeypatch: pytest.MonkeyPatch, dist: Path, spa_log: _Recorder
) -> None:
    configure_spa(monkeypatch, dist)
    create_app()

    skipped = spa_log.levels("warning")
    assert [event for event, _ in skipped] == ["spa_build_entries_skipped"]
    assert skipped[0][1]["entries"] == sorted(
        [
            ".env",
            "linked.png",
            "notes.md",
            "assets/.hidden.js",
            "assets/LOGO.PNG",
            "assets/bad name.js",
            "assets/index-AbC123.js.map",
            "assets/linked.js",
            "assets/page.html",
            "assets/pipe.js",
            "assets/üni.js",
            "assets/nested",
            "other",
        ]
    )
    assert [event for event, _ in spa_log.levels("info")] == ["spa_installed"]


@pytest.mark.parametrize(
    "path, authenticated_status",
    [
        ("/index.html", 404),
        ("/assets/unknown-0000.js", 404),
        ("/assets/", 404),
        ("/assets", 307),
        ("/traces/", 307),
    ],
)
async def test_unregistered_paths_stay_behind_auth(
    spa_app: FastAPI, path: str, authenticated_status: int
) -> None:
    anonymous = await asgi_request(spa_app, "GET", path)
    authenticated = await asgi_request(spa_app, "GET", path, headers=[bearer_header()])

    assert anonymous.status == 401
    assert json.loads(anonymous.body)["error"] == "unauthorized"
    assert authenticated.status == authenticated_status
    assert authenticated.header("content-security-policy") is None


async def test_authenticated_writes_to_assets_get_405(spa_app: FastAPI) -> None:
    response = await asgi_request(
        spa_app, "POST", "/assets/index-AbC123.js", headers=[bearer_header()]
    )

    assert response.status == 405
    assert response.header("allow") == "GET, HEAD"


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/docs"),
        ("GET", "/redoc"),
        ("GET", "/openapi.json"),
        ("GET", "/v1/openapi.json"),
        ("GET", "/v1/billing/summary"),
        ("POST", "/health"),
    ],
)
async def test_spa_headers_never_reach_api_or_docs(
    spa_app: FastAPI, method: str, path: str
) -> None:
    response = await asgi_request(spa_app, method, path)

    for name in ("content-security-policy", "x-frame-options", "referrer-policy"):
        assert response.header(name) is None
    assert response.header("etag") is None


# --------------------------------------------------------------------------
# Fail closed at startup
# --------------------------------------------------------------------------


def _write_index_symlink(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    target = tmp_path / "real-index.html"
    target.write_bytes(INDEX_HTML)
    (dist / "index.html").symlink_to(target)
    return dist


def _write_empty_index(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_bytes(b"")
    return dist


def _write_no_index(tmp_path: Path) -> Path:
    dist = build_dist(tmp_path, hostile=False)
    (dist / "index.html").unlink()
    return dist


def _write_file_not_dir(tmp_path: Path) -> Path:
    path = tmp_path / "dist"
    path.write_bytes(INDEX_HTML)
    return path


def _write_no_assets_dir(tmp_path: Path) -> Path:
    dist = build_dist(tmp_path, hostile=False)
    shutil.rmtree(dist / "assets")
    return dist


def _write_assets_file_not_dir(tmp_path: Path) -> Path:
    dist = build_dist(tmp_path, hostile=False)
    shutil.rmtree(dist / "assets")
    (dist / "assets").write_bytes(b"not a directory")
    return dist


def _write_index_with_missing_asset(tmp_path: Path) -> Path:
    dist = build_dist(tmp_path, hostile=False)
    (dist / "index.html").write_bytes(
        INDEX_HTML.replace(
            b"</head>",
            b'<link rel="modulepreload" href="/assets/missing-0000.js"></head>',
        )
    )
    return dist


def _write_referenced_asset_as_symlink(tmp_path: Path) -> Path:
    dist = build_dist(tmp_path, hostile=False)
    target = tmp_path / "real.js"
    target.write_bytes(ASSET_FILES["index-AbC123.js"])
    (dist / "assets" / "index-AbC123.js").unlink()
    (dist / "assets" / "index-AbC123.js").symlink_to(target)
    return dist


def _write_assets_symlink(tmp_path: Path) -> Path:
    dist = build_dist(tmp_path, hostile=False)
    (dist / "assets").rename(tmp_path / "real-assets")
    (dist / "assets").symlink_to(tmp_path / "real-assets", target_is_directory=True)
    return dist


def _write_non_utf8_index(tmp_path: Path) -> Path:
    dist = build_dist(tmp_path, hostile=False)
    (dist / "index.html").write_bytes(INDEX_HTML.replace(b"<head>", b"<head>\xff\xfe"))
    return dist


@pytest.mark.parametrize(
    "enabled, make_dist, reason",
    [
        (False, lambda tmp: build_dist(tmp), "SPA_ENABLED is false"),
        (True, lambda tmp: None, "spa_dist_dir is empty"),
        (True, lambda tmp: tmp / "missing", "build directory unavailable"),
        (True, _write_file_not_dir, "build directory unavailable"),
        (True, _write_no_index, "index.html missing"),
        (True, _write_empty_index, "index.html missing"),
        (True, _write_index_symlink, "index.html missing"),
        (
            True,
            _write_no_assets_dir,
            "assets directory unavailable (No such file or directory)",
        ),
        (True, _write_assets_file_not_dir, "assets directory unavailable"),
        (
            True,
            _write_index_with_missing_asset,
            "index.html references missing build files: ['/assets/missing-0000.js']",
        ),
        (
            True,
            _write_referenced_asset_as_symlink,
            "index.html references missing build files: ['/assets/index-AbC123.js']",
        ),
        (True, _write_assets_symlink, "assets directory unavailable"),
        (True, _write_non_utf8_index, "index.html is not valid UTF-8"),
    ],
)
async def test_unavailable_build_registers_nothing_with_one_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    off_app: FastAPI,
    spa_log: _Recorder,
    enabled: bool,
    make_dist: Any,
    reason: str,
) -> None:
    configure_spa(monkeypatch, make_dist(tmp_path), enabled=enabled)
    spa_log.calls.clear()

    application = create_app()

    warnings = spa_log.levels("warning")
    assert len(warnings) == 1
    assert warnings[0][0] == "spa_not_installed"
    assert warnings[0][1]["reason"].startswith(reason)
    assert spa_log.levels("error") == []
    assert application.state.spa.enabled is False
    assert not application.state.spa.is_public("/")
    assert [route_signature(r) for r in application.routes] == [
        route_signature(r) for r in off_app.routes
    ]
    for method, path in [("GET", "/billing"), ("GET", "/assets/index-AbC123.js")]:
        assert (await asgi_request(application, method, path)).status == 401
    root = await asgi_request(application, "GET", "/")
    assert (root.status, root.body) == (404, b'{"detail":"Not Found"}')


@pytest.mark.parametrize(
    "make_dist, message",
    [
        (
            _write_no_assets_dir,
            r"^assets directory unavailable \(No such file or directory\)$",
        ),
        (
            _write_assets_file_not_dir,
            r"^assets directory unavailable \(Not a directory\)$",
        ),
        (
            _write_index_with_missing_asset,
            r"^index\.html references missing build files: "
            r"\['/assets/missing-0000\.js'\]$",
        ),
        (
            _write_referenced_asset_as_symlink,
            r"^index\.html references missing build files: "
            r"\['/assets/index-AbC123\.js'\]$",
        ),
        (_write_assets_symlink, r"^assets directory unavailable \(.+\)$"),
        (_write_non_utf8_index, r"^index\.html is not valid UTF-8 \(.+\)$"),
    ],
)
def test_broken_assets_raise_spa_unavailable(
    tmp_path: Path, make_dist: Any, message: str
) -> None:
    with pytest.raises(SpaUnavailable, match=message):
        load_build(str(make_dist(tmp_path)))


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root ignores directory permission bits",
)
async def test_unreadable_assets_dir_registers_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    off_app: FastAPI,
    spa_log: _Recorder,
) -> None:
    dist = build_dist(tmp_path, hostile=False)
    assets = dist / "assets"
    assets.chmod(0)
    try:
        with pytest.raises(
            SpaUnavailable,
            match=r"^assets directory unavailable \(Permission denied\)$",
        ):
            load_build(str(dist))
        configure_spa(monkeypatch, dist)
        spa_log.calls.clear()

        application = create_app()

        assert [event for event, _ in spa_log.levels("warning")] == [
            "spa_not_installed"
        ]
        assert application.state.spa.enabled is False
        assert [route_signature(r) for r in application.routes] == [
            route_signature(r) for r in off_app.routes
        ]
        assert (await asgi_request(application, "GET", "/billing")).status == 401
    finally:
        assets.chmod(0o755)


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    reason="root ignores directory permission bits",
)
def test_unreadable_assets_dir_fails_closed_even_without_references(
    tmp_path: Path,
) -> None:
    """Only a *missing* unreferenced assets/ is tolerated, never an unreadable one."""

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_bytes(b"<!DOCTYPE html><title>x</title>")
    (dist / "assets").chmod(0)
    try:
        with pytest.raises(
            SpaUnavailable,
            match=r"^assets directory unavailable \(Permission denied\)$",
        ):
            load_build(str(dist))
    finally:
        (dist / "assets").chmod(0o755)


def test_os_error_while_reading_the_build_registers_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    spa_log: _Recorder,
) -> None:
    def failing_scan(*args: Any, **kwargs: Any) -> Any:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(spa, "_scan_files", failing_scan)
    configure_spa(monkeypatch, build_dist(tmp_path, hostile=False))

    application = create_app()

    assert application.state.spa.enabled is False
    warnings = spa_log.levels("warning")
    assert [event for event, _ in warnings] == ["spa_not_installed"]
    assert warnings[0][1]["reason"] == "[Errno 5] Input/output error"


def test_build_without_asset_references_needs_no_assets_dir(tmp_path: Path) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_bytes(b"<!DOCTYPE html><title>x</title>")

    registry = load_build(str(dist))

    assert registry.enabled
    assert registry.files == {}


@pytest.mark.parametrize(
    "top_level_name",
    [
        "health.png",
        "healthz.txt",
        "live.svg",
        "ready.ico",
        "docs.png",
        "redoc.css",
        "metrics.json",
        "openapi.json",
        "v1.png",
    ],
)
def test_reserved_build_file_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    spa_log: _Recorder,
    top_level_name: str,
) -> None:
    dist = build_dist(tmp_path, hostile=False)
    (dist / top_level_name).write_bytes(b"x")
    configure_spa(monkeypatch, dist)

    application = create_app()

    assert application.state.spa.enabled is False
    errors = spa_log.levels("error")
    assert [event for event, _ in errors] == ["spa_not_installed"]
    assert f"reserved:/{top_level_name}" in errors[0][1]["conflicts"]
    assert not any(
        getattr(route, "endpoint", None) is not None
        and getattr(route.endpoint, "__qualname__", "").startswith("_page_endpoint")
        for route in application.routes
    )


@pytest.mark.parametrize(
    "client_route",
    [
        "/v1",
        "/v1/billing",
        "/v1beta",
        "/api",
        "/api/x",
        "/privacidade",
        "/privacidade/x",
        "/docs/x",
        "/metrics/summary",
        "/openapi.json",
        "/live",
    ],
)
def test_reserved_client_route_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    spa_log: _Recorder,
    client_route: str,
) -> None:
    monkeypatch.setattr(spa, "SPA_CLIENT_ROUTES", (*SPA_CLIENT_ROUTES, client_route))
    configure_spa(monkeypatch, build_dist(tmp_path, hostile=False))

    application = create_app()

    assert application.state.spa.enabled is False
    conflicts = spa_log.levels("error")[0][1]["conflicts"]
    assert f"reserved:{client_route}" in conflicts


def test_unsupported_dynamic_client_route_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(spa, "SPA_CLIENT_ROUTES", (*SPA_CLIENT_ROUTES, "/x/:id"))

    with pytest.raises(
        SpaUnavailable, match=r"unsupported dynamic client route '/x/:id'"
    ):
        load_build(str(build_dist(tmp_path, hostile=False)))


def test_build_file_shadowing_a_client_route_fails_closed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dist = build_dist(tmp_path, hostile=False)
    (dist / "robots.txt").write_bytes(b"User-agent: *")
    monkeypatch.setattr(spa, "SPA_CLIENT_ROUTES", (*SPA_CLIENT_ROUTES, "/robots.txt"))

    with pytest.raises(
        SpaUnavailable, match=r"build files shadow client routes: \['/robots.txt'\]"
    ):
        load_build(str(dist))


def _bare_app_with(route_setup: Any) -> FastAPI:
    application = FastAPI()
    route_setup(application)
    return application


def _get_route(app: FastAPI, path: str) -> None:
    @app.get(path)
    async def existing() -> dict[str, str]:
        return {"ok": "yes"}


def _post_route(app: FastAPI, path: str) -> None:
    @app.post(path)
    async def existing() -> dict[str, str]:
        return {"ok": "yes"}


def _included_router(app: FastAPI) -> None:
    router = APIRouter()

    @router.get("/agents")
    async def agents() -> dict[str, str]:
        return {"ok": "yes"}

    app.include_router(router)


def _included_traces_router(app: FastAPI) -> None:
    router = APIRouter()

    @router.get("/stats")
    async def stats() -> dict[str, str]:
        return {"ok": "yes"}

    app.include_router(router, prefix="/traces")


async def _plain(request: Request) -> PlainTextResponse:
    return PlainTextResponse("api handler")


def _included_plain_route(app: FastAPI, prefix: str, path: str) -> None:
    router = APIRouter()
    router.add_route(path, _plain, methods=["GET"])
    app.include_router(router, prefix=prefix)


def _nested_plain_route(app: FastAPI) -> None:
    inner = APIRouter()
    inner.add_route("/{tid}", _plain, methods=["GET"])
    outer = APIRouter()
    outer.include_router(inner, prefix="/traces")
    app.include_router(outer)


def _included_host(app: FastAPI) -> None:
    router = APIRouter()
    router.routes.append(
        Host("x.test", app=Router(routes=[Route("/{p:path}", _plain)]))
    )
    app.include_router(router, prefix="/v1")


def _included_mount(app: FastAPI) -> None:
    router = APIRouter()
    router.mount("/static", FastAPI())
    app.include_router(router, prefix="/v1")


class _SelectiveRoute(Route):
    """A route whose ``path`` does not describe what it matches."""

    def matches(self, scope: Any) -> tuple[Match, dict[str, Any]]:
        if scope["type"] == "http" and scope["path"] == "/traces/stats":
            return Match.FULL, {"endpoint": self.endpoint, "path_params": {}}
        return Match.NONE, {}


def _traces_websocket(app: FastAPI) -> None:
    @app.websocket("/traces/live")
    async def live(websocket: WebSocket) -> None:
        await websocket.close()


@pytest.mark.parametrize(
    "path, is_mount, shadows",
    [
        (None, False, True),  # Host and other path-less routes
        ("", False, True),  # placeholder path of an unresolved route context
        ("", True, True),  # root mount
        ("/traces", True, True),
        ("/traces", False, False),  # exact page, covered by the exact probes
        ("/traces/stats", False, True),
        ("/{x}", False, True),
        ("/tracesx/{y}", False, False),
        ("/v1/static", True, False),
    ],
)
def test_trace_detail_shadowing_rule(
    path: str | None, is_mount: bool, shadows: bool
) -> None:
    assert spa._shadows_trace_detail(path, is_mount) is shadows


@pytest.mark.parametrize(
    "setup",
    [
        lambda app: _get_route(app, "/tracesx/{rest}"),
        lambda app: _get_route(app, "/v1/traces/{trace_id}"),
        lambda app: _get_route(app, "/v1/{anything}"),
        lambda app: _get_route(app, "/privacidade/{clinic_id}"),
        lambda app: app.mount("/tracesx", FastAPI()),
        _included_mount,
        lambda app: _included_plain_route(app, "/v1", "/traces/stats"),
    ],
)
def test_routes_outside_the_spa_namespaces_do_not_collide(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, setup: Any
) -> None:
    configure_spa(monkeypatch, build_dist(tmp_path, hostile=False))

    assert install_spa(_bare_app_with(setup)).enabled


@pytest.mark.parametrize(
    "setup, colliding",
    [
        (lambda app: _get_route(app, "/billing"), "GET /billing"),
        (lambda app: _post_route(app, "/crm"), "GET /crm"),
        (lambda app: _get_route(app, "/{anything}"), "GET /dashboard"),
        (lambda app: _get_route(app, "/traces/{trace_id}"), "/traces/:id"),
        (lambda app: _get_route(app, "/assets/{name}"), "GET /assets/index-AbC123.js"),
        (lambda app: _get_route(app, "/favicon.png"), "GET /favicon.png"),
        (lambda app: app.mount("/dashboard", FastAPI()), "GET /dashboard/"),
        (_included_router, "GET /agents"),
        # The dynamic /traces/<id> namespace is checked against the route
        # table itself, not against sample ids.
        (lambda app: _get_route(app, "/traces/stats"), "/traces/:id"),
        (lambda app: _post_route(app, "/traces/export"), "/traces/:id"),
        (lambda app: _get_route(app, "/traces/stats/{day}"), "/traces/:id"),
        (lambda app: _get_route(app, "/{section}/stats"), "/traces/:id"),
        (lambda app: _get_route(app, "/{anything}"), "/traces/:id"),
        (lambda app: _get_route(app, "/trace{rest:path}"), "/traces/:id"),
        (lambda app: app.mount("/traces/sub", FastAPI()), "/traces/:id"),
        (lambda app: app.mount("/", FastAPI()), "/traces/:id"),
        (_included_traces_router, "/traces/:id"),
        (_traces_websocket, "/traces/:id"),
        (lambda app: app.mount("/traces", FastAPI()), "/traces/:id"),
        # Plain (non-API) routes inside included routers: the effective path
        # lives on the wrapped Starlette route.
        (lambda app: _included_plain_route(app, "", "/traces/stats"), "/traces/:id"),
        (lambda app: _included_plain_route(app, "/traces", "/stats"), "/traces/:id"),
        (_nested_plain_route, "/traces/:id"),
        (_included_host, "/traces/:id"),
        (
            lambda app: app.router.routes.append(_SelectiveRoute("/lying", _plain)),
            "/traces/:id",
        ),
        (
            lambda app: app.router.routes.append(Host("x.test", FastAPI())),
            "/traces/:id",
        ),
    ],
)
def test_collision_with_existing_route_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    spa_log: _Recorder,
    setup: Any,
    colliding: str,
) -> None:
    configure_spa(monkeypatch, build_dist(tmp_path, hostile=False))
    application = _bare_app_with(setup)
    before = [route_signature(route) for route in application.routes]

    registry = install_spa(application)

    assert registry.enabled is False
    assert application.state.spa is registry
    assert [route_signature(route) for route in application.routes] == before
    conflicts = spa_log.levels("error")[0][1]["conflicts"]
    assert any(c.startswith(f"collision:{colliding} -> ") for c in conflicts), conflicts


@pytest.mark.parametrize(
    "path, cache_control",
    [
        ("/billing", APPROVED_INDEX_CACHE),
        ("/assets/index-AbC123.js", APPROVED_ASSET_CACHE),
        ("/favicon.svg", APPROVED_TOP_LEVEL_CACHE),
    ],
)
async def test_spa_sets_its_own_headers_without_app_middleware(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, path: str, cache_control: str
) -> None:
    """The app's global middleware also sends nosniff; the SPA must not rely on it."""

    configure_spa(monkeypatch, build_dist(tmp_path, hostile=False))
    application = FastAPI()
    assert install_spa(application).enabled

    response = await asgi_request(application, "GET", path)

    assert response.status == 200
    assert response.header("cache-control") == cache_control
    for name, value in APPROVED_SECURITY_HEADERS.items():
        assert response.header(name) == value, name


def test_install_on_clean_app_appends_only_spa_routes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure_spa(monkeypatch, build_dist(tmp_path, hostile=False))
    application = FastAPI()
    before = len(application.routes)

    registry = install_spa(application)

    added = application.routes[before:]
    assert registry.enabled
    assert isinstance(added[-1], Mount) and added[-1].path == "/assets"
    route_paths = [getattr(route, "path", None) for route in added[:-1]]
    assert route_paths == sorted(registry.pages) + [
        "/favicon.png",
        "/favicon.svg",
        "/opengraph.jpg",
        "/traces/{trace_id}",
    ]
    for route in added[:-1]:
        assert set(getattr(route, "methods", ())) == {"GET", "HEAD"}
        assert getattr(route, "include_in_schema", True) is False


def _selective_matches(self: Any, scope: Any) -> tuple[Match, dict[str, Any]]:
    """An instance-level ``matches`` that ``path`` does not describe."""

    if scope["type"] == "http" and scope["path"] == "/traces/stats":
        return Match.FULL, {"endpoint": self.endpoint, "path_params": {}}
    return Match.NONE, {}


def _instance_matches_top_level(app: FastAPI) -> None:
    @app.get("/lying")
    async def lying() -> dict[str, str]:
        return {"ok": "yes"}

    route = app.router.routes[-1]
    assert isinstance(route, APIRoute)
    route.matches = MethodType(_selective_matches, route)  # type: ignore[method-assign]


def _instance_matches_included(app: FastAPI) -> None:
    router = APIRouter()

    @router.get("/lying")
    async def lying() -> dict[str, str]:
        return {"ok": "yes"}

    route = router.routes[-1]
    assert isinstance(route, APIRoute)
    route.matches = MethodType(_selective_matches, route)  # type: ignore[method-assign]
    app.include_router(router)


@pytest.mark.parametrize(
    "setup", [_instance_matches_top_level, _instance_matches_included]
)
def test_instance_level_matches_override_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    spa_log: _Recorder,
    setup: Any,
) -> None:
    """``type(route).matches`` is stock here; only ``vars(route)`` shows it."""

    configure_spa(monkeypatch, build_dist(tmp_path, hostile=False))

    registry = install_spa(_bare_app_with(setup))

    assert registry.enabled is False
    conflicts = spa_log.levels("error")[0][1]["conflicts"]
    assert conflicts == ["collision:/traces/:id -> custom matches APIRoute"]


async def test_utf8_index_with_non_ascii_text_is_served_byte_identical(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dist = build_dist(tmp_path, hostile=False)
    index = INDEX_HTML.replace(
        b"<head>", "<head><title>Clínica São João</title>".encode("utf-8")
    )
    assert not index.isascii()
    (dist / "index.html").write_bytes(index)
    configure_spa(monkeypatch, dist)

    application = create_app()
    response = await asgi_request(application, "GET", "/billing")

    assert application.state.spa.enabled
    assert response.status == 200
    assert response.body == index
    assert response.header("content-type") == "text/html; charset=utf-8"
    assert response.header("etag") == etag_of(index)


# --------------------------------------------------------------------------
# Route table re-checked once every startup hook has run
# --------------------------------------------------------------------------

_LATE_PATH = "/traces/{late_id}"


@pytest.fixture
def restore_health_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """``health.router`` is module-global: give it a copy of its route list."""

    monkeypatch.setattr(health.router, "routes", list(health.router.routes))


def _late_route_on_included_router(app: FastAPI) -> None:
    # health.router is included without a prefix, before install_spa.
    health.router.add_api_route(_LATE_PATH, _plain, methods=["GET"])


def _late_route_at_the_top_of_the_table(app: FastAPI) -> None:
    app.router.routes.insert(0, APIRoute(_LATE_PATH, _plain, methods=["GET"]))


@pytest.mark.usefixtures("restore_health_routes")
@pytest.mark.parametrize(
    "add_late_route",
    [_late_route_on_included_router, _late_route_at_the_top_of_the_table],
)
async def test_route_added_during_startup_turns_the_spa_off(
    monkeypatch: pytest.MonkeyPatch,
    spa_log: _Recorder,
    spa_app: FastAPI,
    add_late_route: Any,
) -> None:
    registry: SpaRegistry = spa_app.state.spa

    def startup_hook() -> None:
        # Stands in for any startup work inside the real lifespan.
        add_late_route(spa_app)

    monkeypatch.setattr(main_module, "_start_billing_fact_worker", startup_hook)
    assert registry.is_public("/traces/abc")

    async with asgi_lifespan(spa_app):
        assert spa_app.state.spa is registry
        assert registry.enabled is False
        assert (registry.pages, registry.files) == (frozenset(), {})
        assert not registry.is_public("/billing")
        for method, path in [
            ("GET", "/traces/abc"),
            ("GET", "/billing"),
            ("HEAD", "/billing"),
            ("GET", "/assets/index-AbC123.js"),
            ("GET", "/favicon.svg"),
        ]:
            anonymous = await asgi_request(spa_app, method, path)
            assert anonymous.status == 401, (method, path)
            assert anonymous.header("content-security-policy") is None
        # "/" stays in AuthMiddleware.PUBLIC_PATHS, as with the SPA off.
        root = await asgi_request(spa_app, "GET", "/")
        assert (root.status, root.body) == (404, b'{"detail":"Not Found"}')
        late = await asgi_request(
            spa_app, "GET", "/traces/abc", headers=[bearer_header()]
        )
        assert (late.status, late.body) == (200, b"api handler")
        for path in ("/billing", "/assets/index-AbC123.js", "/favicon.svg"):
            served = await asgi_request(spa_app, "GET", path, headers=[bearer_header()])
            assert served.status == 404, path
            assert served.header("content-security-policy") is None

    errors = spa_log.levels("error")
    assert [event for event, _ in errors] == ["spa_conflict_after_startup"]
    assert f"collision:/traces/:id -> {_LATE_PATH}" in errors[0][1]["conflicts"]


def _bare_app_with_late_route(hook: str) -> FastAPI:
    """An included router that adds a /traces route from its own startup hook."""

    @asynccontextmanager
    async def router_lifespan(_: Any) -> AsyncIterator[None]:
        router.add_api_route(_LATE_PATH, _plain, methods=["GET"])
        yield

    def on_startup() -> None:
        router.add_api_route(_LATE_PATH, _plain, methods=["GET"])

    if hook == "router-lifespan":
        router = APIRouter(lifespan=router_lifespan)
    else:
        router = APIRouter(on_startup=[on_startup])
    application = FastAPI()
    application.include_router(router)
    return application


@pytest.mark.parametrize("hook", ["router-lifespan", "on-startup"])
@pytest.mark.parametrize("driver", ["asgi", "lifespan-context"])
async def test_recheck_runs_after_every_startup_hook(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    spa_log: _Recorder,
    hook: str,
    driver: str,
) -> None:
    """Nested router lifespans and on_startup handlers run before the check."""

    configure_spa(monkeypatch, build_dist(tmp_path, hostile=False))
    application = _bare_app_with_late_route(hook)
    registry = install_spa(application)
    assert registry.enabled

    for _ in range(2):  # a second startup logs nothing new
        lifespan = (
            asgi_lifespan(application)
            if driver == "asgi"
            else application.router.lifespan_context(application)
        )
        async with lifespan:
            assert registry.enabled is False
            late = await asgi_request(application, "GET", "/traces/abc")
            assert (late.status, late.body) == (200, b"api handler")
            assert (await asgi_request(application, "GET", "/billing")).status == 404

    errors = spa_log.levels("error")
    assert [event for event, _ in errors] == ["spa_conflict_after_startup"]
    assert f"collision:/traces/:id -> {_LATE_PATH}" in errors[0][1]["conflicts"]


async def test_clean_startup_keeps_the_spa_and_logs_nothing_new(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spa_log: _Recorder
) -> None:
    started: list[bool] = []

    @asynccontextmanager
    async def app_lifespan(_: Any) -> AsyncIterator[dict[str, str]]:
        yield {"from_app": "kept"}

    @asynccontextmanager
    async def router_lifespan(_: Any) -> AsyncIterator[None]:
        started.append(True)
        yield

    configure_spa(monkeypatch, build_dist(tmp_path, hostile=False))
    application = FastAPI(lifespan=app_lifespan)
    application.include_router(APIRouter(lifespan=router_lifespan))
    registry = install_spa(application)
    calls_after_install = list(spa_log.calls)

    async with asgi_lifespan(application) as state:
        # The wrapper passes the inner lifespan state through unchanged.
        assert state == {"from_app": "kept"}
        assert started == [True]
        assert registry.enabled
        page = await asgi_request(application, "GET", "/traces/abc")
        assert (page.status, page.body) == (200, INDEX_HTML)

    assert spa_log.calls == calls_after_install


async def test_real_app_still_serves_the_spa_after_its_lifespan(
    spa_log: _Recorder, spa_app: FastAPI
) -> None:
    calls_after_install = list(spa_log.calls)

    async with asgi_lifespan(spa_app):
        assert spa_app.state.spa.enabled
        for path in ("/billing", "/traces/abc", "/"):
            page = await asgi_request(spa_app, "GET", path)
            assert (page.status, page.body) == (200, INDEX_HTML), path
        asset = await asgi_request(spa_app, "GET", "/assets/index-AbC123.js")
        assert asset.status == 200

    assert spa_log.calls == calls_after_install


async def test_failing_recheck_turns_the_spa_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spa_log: _Recorder
) -> None:
    configure_spa(monkeypatch, build_dist(tmp_path, hostile=False))
    application = FastAPI()
    registry = install_spa(application)

    def broken(*args: Any, **kwargs: Any) -> list[str]:
        raise RuntimeError("route table unreadable")

    monkeypatch.setattr(spa, "_find_conflicts", broken)

    async with asgi_lifespan(application):
        assert registry.enabled is False
        assert (await asgi_request(application, "GET", "/billing")).status == 404

    errors = spa_log.levels("error")
    assert [event for event, _ in errors] == ["spa_conflict_after_startup"]
    assert errors[0][1]["conflicts"] == ["recheck_failed:RuntimeError"]


def test_create_app_leaves_the_spa_lifespan_outermost(spa_app: FastAPI) -> None:
    """Nothing after install_spa in create_app may replace its lifespan."""

    lifespan = spa_app.router.lifespan_context
    assert getattr(lifespan, "__name__", None) == "spa_checked_lifespan"


def _router_inserting_a_late_route() -> APIRouter:
    @asynccontextmanager
    async def router_lifespan(app: Any) -> AsyncIterator[None]:
        # Runs after the SPA check: the merged lifespan enters it last.
        app.router.routes.insert(0, APIRoute(_LATE_PATH, _plain, methods=["GET"]))
        yield

    return APIRouter(lifespan=router_lifespan)


@pytest.mark.parametrize(
    "make_router", [_router_inserting_a_late_route, APIRouter], ids=["hook", "plain"]
)
async def test_router_included_after_install_spa_turns_the_spa_off(
    spa_log: _Recorder, spa_app: FastAPI, make_router: Any
) -> None:
    registry: SpaRegistry = spa_app.state.spa
    spa_app.include_router(make_router())
    assert registry.is_public("/traces/abc")

    async with asgi_lifespan(spa_app):
        assert registry.enabled is False
        assert not registry.is_public("/traces/abc")
        anonymous = await asgi_request(spa_app, "GET", "/traces/abc")
        assert anonymous.status == 401
        page = await asgi_request(spa_app, "GET", "/billing", headers=[bearer_header()])
        assert page.status == 404

    errors = spa_log.levels("error")
    assert [event for event, _ in errors] == ["spa_conflict_after_startup"]
    assert errors[0][1]["conflicts"] == [spa.LIFESPAN_REPLACED]


def _app_whose_lifespan_adds(path: str, *, first: bool) -> FastAPI:
    @asynccontextmanager
    async def app_lifespan(app: Any) -> AsyncIterator[None]:
        route = APIRoute(path, _plain, methods=["GET"])
        if first:
            app.router.routes.insert(0, route)
        else:
            app.router.routes.append(route)
        yield

    return FastAPI(lifespan=app_lifespan)


@pytest.mark.parametrize("first", [True, False], ids=["insert-0", "append"])
@pytest.mark.parametrize(
    "path", ["/billing", "/favicon.svg", "/assets/index-AbC123.js"]
)
async def test_late_route_on_an_exact_spa_path_turns_the_spa_off(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    spa_log: _Recorder,
    path: str,
    first: bool,
) -> None:
    """Only the SPA's own route objects are skipped, not their paths."""

    configure_spa(monkeypatch, build_dist(tmp_path, hostile=False))
    application = _app_whose_lifespan_adds(path, first=first)
    registry = install_spa(application)
    assert registry.is_public(path)

    async with asgi_lifespan(application):
        assert registry.enabled is False
        assert not registry.is_public(path)

    errors = spa_log.levels("error")
    assert [event for event, _ in errors] == ["spa_conflict_after_startup"]
    assert f"collision:GET {path} -> {path}" in errors[0][1]["conflicts"]


async def test_late_route_is_caught_for_a_build_without_assets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, spa_log: _Recorder
) -> None:
    dist = build_dist(tmp_path, hostile=False)
    shutil.rmtree(dist / "assets")
    (dist / "index.html").write_bytes(
        b'<!DOCTYPE html><html><head></head><body><div id="root"></div></body></html>'
    )
    configure_spa(monkeypatch, dist)
    application = _app_whose_lifespan_adds(_LATE_PATH, first=True)
    registry = install_spa(application)
    assert registry.enabled
    assert not any(path.startswith("/assets/") for path in registry.files)

    async with asgi_lifespan(application):
        assert registry.enabled is False

    errors = spa_log.levels("error")
    assert [event for event, _ in errors] == ["spa_conflict_after_startup"]
    assert f"collision:/traces/:id -> {_LATE_PATH}" in errors[0][1]["conflicts"]


class _Boom(Exception):
    pass


def _app_whose_lifespan_fails(stage: str, seen: list[BaseException]) -> FastAPI:
    @asynccontextmanager
    async def app_lifespan(_: Any) -> AsyncIterator[None]:
        if stage == "startup":
            raise _Boom("startup boom")
        try:
            yield
        except BaseException as error:
            seen.append(error)
            raise
        if stage == "teardown":
            raise _Boom("teardown boom")

    return FastAPI(lifespan=app_lifespan)


@pytest.mark.parametrize("stage", ["startup", "teardown"])
@pytest.mark.parametrize("driver", ["asgi", "lifespan-context"])
async def test_inner_lifespan_errors_surface_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    spa_log: _Recorder,
    stage: str,
    driver: str,
) -> None:
    configure_spa(monkeypatch, build_dist(tmp_path, hostile=False))
    application = _app_whose_lifespan_fails(stage, [])
    registry = install_spa(application)
    calls_after_install = list(spa_log.calls)
    lifespan = (
        asgi_lifespan(application)
        if driver == "asgi"
        else application.router.lifespan_context(application)
    )

    with pytest.raises(_Boom, match=f"^{stage} boom$"):
        async with lifespan:
            pass

    if stage == "teardown":
        assert registry.enabled
    assert spa_log.calls == calls_after_install


async def test_body_error_reaches_the_inner_lifespan_and_the_caller(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``lifespan_context`` called directly (tests/conftest.py does) keeps errors."""

    configure_spa(monkeypatch, build_dist(tmp_path, hostile=False))
    seen: list[BaseException] = []
    application = _app_whose_lifespan_fails("body", seen)
    install_spa(application)

    with pytest.raises(_Boom, match="^body boom$"):
        async with application.router.lifespan_context(application):
            raise _Boom("body boom")

    assert [str(error) for error in seen] == ["body boom"]


# --------------------------------------------------------------------------
# Real build (optional): `pnpm --filter @workspace/neurostack run build`
# --------------------------------------------------------------------------


class _DocumentScan(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inline_scripts: list[str] = []
        self.inline_handlers: list[str] = []
        self.inline_styles: int = 0
        self.urls: list[tuple[str, str, str]] = []
        self._in_script_without_src = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value or "" for name, value in attrs}
        self.inline_handlers += [name for name in values if name.startswith("on")]
        if "style" in values:
            self.inline_styles += 1
        if tag == "script":
            self._in_script_without_src = "src" not in values
            if "src" in values:
                self.urls.append((tag, values.get("rel", ""), values["src"]))
        if tag == "link":
            self.urls.append((tag, values.get("rel", ""), values.get("href", "")))
        if tag == "style":
            self.inline_styles += 1

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script_without_src = False

    def handle_data(self, data: str) -> None:
        if self._in_script_without_src and data.strip():
            self.inline_scripts.append(data)


def _csp_sources(directive: str) -> list[str]:
    for part in APPROVED_CSP.split("; "):
        name, *sources = part.split(" ")
        if name == directive:
            return sources
    raise AssertionError(f"CSP has no {directive}")


def _origin(url: str) -> str:
    match = re.match(r"(https?://[^/]+)", url)
    return match.group(1) if match else "'self'"


@pytest.fixture
def real_dist(tmp_path: Path) -> Path:
    source = os.environ.get(REAL_DIST_ENV, "")
    if not source or not (Path(source) / "index.html").is_file():
        pytest.skip(
            f"{REAL_DIST_ENV} does not point at a built dist/public "
            "(run the frontend build and export its path to enable)"
        )
    copy = tmp_path / "public"
    shutil.copytree(source, copy, symlinks=True)
    return copy


async def test_real_bundle_is_served_and_compatible_with_csp(
    monkeypatch: pytest.MonkeyPatch, real_dist: Path
) -> None:
    configure_spa(monkeypatch, real_dist)
    application = create_app()
    index = (real_dist / "index.html").read_bytes()
    document = _DocumentScan()
    document.feed(index.decode("utf-8"))

    page = await asgi_request(application, "GET", "/billing")
    assert page.status == 200 and page.body == index
    assert page.header("content-security-policy") == APPROVED_CSP

    # index.html: no inline script, handler or style attribute; every
    # referenced resource is either served exactly or allowed by the CSP.
    assert document.inline_scripts == []
    assert document.inline_handlers == []
    assert document.inline_styles == 0
    for tag, rel, url in document.urls:
        if url.startswith("/"):
            served = await asgi_request(application, "GET", url)
            assert served.status == 200, url
            assert served.body == (real_dist / url.lstrip("/")).read_bytes()
            assert served.header("content-type") is not None
            continue
        if rel == "preconnect":
            continue  # resource hint, not a fetch governed by CSP
        directive = "script-src" if tag == "script" else "style-src"
        assert _origin(url) in _csp_sources(directive), (tag, url)

    css_files = sorted((real_dist / "assets").glob("*.css"))
    js_files = sorted((real_dist / "assets").glob("*.js"))
    assert css_files and js_files
    for css in css_files:
        text = css.read_text(encoding="utf-8")
        for target in re.findall(r'@import\s*(?:url\()?["\']?([^"\')\s;]+)', text):
            assert _origin(target) in _csp_sources("style-src"), target
        for target in re.findall(r"url\(\s*['\"]?([^'\")]+)", text):
            if target.startswith("data:"):
                assert "data:" in _csp_sources("img-src") + _csp_sources("font-src")
            else:
                assert _origin(target) == "'self'", target
    for path in sorted(application.state.spa.files):
        served = await asgi_request(application, "GET", path)
        assert served.status == 200, path
    for js in js_files:
        text = js.read_text(encoding="utf-8")
        # connect-src 'self' and script-src without 'unsafe-eval' hold only
        # while the bundle opens no socket/stream and evaluates no strings.
        assert "new WebSocket" not in text and "EventSource" not in text
        assert re.search(r"(?<![\w$.])eval\(", text) is None
        assert "new Function" not in text
