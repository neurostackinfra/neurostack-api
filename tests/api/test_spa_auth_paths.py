"""Group 6c: the SPA auth bypass is exact and agrees with the router.

Every request here is driven as a raw ASGI scope, so the decoded
``scope["path"]`` is exactly what a server such as uvicorn would hand over
(HTTP clients would normalize ``//x``, dot segments or encodings first).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette._utils import get_route_path
from starlette.requests import Request
from starlette.routing import Mount

from src.api.main import create_app
from src.api.middleware.auth import AuthMiddleware
from src.api.spa import SpaRegistry
from tests.api._spa_support import (
    APPROVED_CSP,
    ASSET_FILES,
    INDEX_HTML,
    TOP_LEVEL_FILES,
    AsgiResponse,
    asgi_request,
    bearer_header,
    build_dist,
    configure_spa,
    default_raw_path,
    route_signature,
)

UNAUTHORIZED_BODY = (
    b'{"error":"unauthorized","message":"Missing Authorization or X-API-Key '
    b'header","documentation_url":"https://docs.neurostack.ai/auth"}'
)
ASSET = "/assets/index-AbC123.js"

# (method, decoded scope path, raw_path or None, expected anonymous status)
ATTACK_MATRIX: list[tuple[str, str, bytes | None, int]] = [
    # double slashes and case
    ("GET", "//billing", None, 401),
    ("GET", "//assets/x", None, 401),
    ("GET", "//assets/index-AbC123.js", None, 401),
    ("GET", "/assets//index-AbC123.js", None, 401),
    ("GET", "/billing//", None, 401),
    ("GET", "/Billing", None, 401),
    ("GET", "/BILLING", None, 401),
    ("GET", "/ASSETS/x", None, 401),
    ("GET", "/ASSETS/index-AbC123.js", None, 401),
    ("GET", "/assets/INDEX-ABC123.JS", None, 401),
    ("GET", "/FAVICON.PNG", None, 401),
    # trailing characters
    ("GET", "/billing.", None, 401),
    ("GET", "/billing;x", None, 401),
    ("GET", "/billing;", None, 401),
    ("GET", "/billing ", None, 401),
    ("GET", "/billing\t", None, 401),
    ("GET", "/billing\n", None, 401),
    ("GET", "/billing\r\n", None, 401),
    ("GET", "/billing\x00", b"/billing%00", 401),
    ("GET", "/billing%00", b"/billing%2500", 401),
    ("GET", "/billing\\", None, 401),
    ("GET", "\\billing", None, 401),
    ("GET", "/billing​", None, 401),
    ("GET", "/bılling", None, 401),
    ("GET", "/ｂilling", None, 401),
    ("GET", "/favicon.png/", None, 401),
    ("GET", "/favicon.png.", None, 401),
    ("GET", f"{ASSET}/", None, 401),
    ("GET", f"{ASSET}\n", None, 401),
    ("GET", f"{ASSET}\x00", None, 401),
    # a decoded "?"/"#" truncates request.url.path but not the router path
    ("GET", "/billing?x", b"/billing%3Fx", 401),
    ("GET", "/billing#x", b"/billing%23x", 401),
    ("GET", "/billing?/../v1/admin/clinics", None, 401),
    # dot segments and encodings
    ("GET", "/./billing", None, 401),
    ("GET", "/billing/.", None, 401),
    ("GET", "/billing/..", None, 401),
    ("GET", "/billing/../v1/admin/clinics", None, 401),
    ("GET", "/assets/./index-AbC123.js", None, 401),
    ("GET", "/assets/../v1/admin/clinics", None, 401),
    ("GET", "/assets/../v1/admin/clinics", b"/assets/%2e%2e/v1/admin/clinics", 401),
    ("GET", "/assets/%2e%2e/v1/admin/clinics", None, 401),
    ("GET", "/assets/../v1", b"/assets%2F..%2Fv1", 401),
    ("GET", "/assets%2F..%2Fv1", None, 401),
    ("GET", "/assets/..%2Fv1%2Fadmin", None, 401),
    ("GET", "/assets/index-AbC123.js/../../v1/admin/clinics", None, 401),
    # mount prefix and index
    ("GET", "/assets", None, 401),
    ("GET", "/assets/", None, 401),
    ("GET", "/assets/x", None, 401),
    ("GET", "/index.html", None, 401),
    ("GET", "/assets/index.html", None, 401),
    # the dynamic route
    ("GET", "/traces/", None, 401),
    ("GET", "/traces/.", None, 401),
    ("GET", "/traces/..", None, 401),
    ("GET", "/traces/%2e%2e", None, 401),
    ("GET", "/traces/a/b", None, 401),
    ("GET", "/traces/x\n", None, 401),
    ("GET", "/traces/x\x00", None, 401),
    ("GET", "/traces/abc/", None, 401),
    ("GET", "/traces//abc", None, 401),
    ("GET", "/traces/" + "a" * 129, None, 401),
    ("GET", "/traces/é", None, 401),
    ("GET", "/traces/a.b", None, 401),
    # very long paths
    ("GET", "/" + "a" * 16384, None, 401),
    ("GET", "/assets/" + "a" * 5000 + ".js", None, 401),
    ("GET", "/billing" + "/" * 1000, None, 401),
    # methods
    ("POST", "/billing", None, 401),
    ("PUT", "/billing", None, 401),
    ("PATCH", "/billing", None, 401),
    ("DELETE", "/billing", None, 401),
    ("TRACE", "/billing", None, 401),
    ("CONNECT", "/billing", None, 401),
    ("get", "/billing", None, 401),
    ("POST", ASSET, None, 401),
    ("PUT", ASSET, None, 401),
    ("DELETE", ASSET, None, 401),
    ("POST", "/traces/abc", None, 401),
    ("OPTIONS", "/billing", None, 405),
    ("OPTIONS", ASSET, None, 405),
    # the API keeps its own answer
    ("GET", "/v1/billing/summary", None, 401),
    ("GET", "/v1/billing", None, 401),
    ("GET", "/v1/assets/index-AbC123.js", None, 401),
    ("GET", "/v1/traces/abc", None, 401),
    ("GET", "/api/billing", None, 401),
]

POSITIVE_CONTROLS: list[tuple[str, str, bytes | None]] = [
    ("GET", "/", None),
    ("GET", "/billing", None),
    ("GET", "/billing/", None),
    ("HEAD", "/billing", None),
    ("GET", "/traces/abc", None),
    ("GET", "/traces/" + "a" * 128, None),
    ("GET", ASSET, None),
    ("HEAD", ASSET, None),
    ("GET", "/favicon.png", None),
    # a server decodes these before the app; router and auth both see the
    # decoded exact path, so serving it is consistent
    ("GET", "/billing", b"/b%69lling"),
    ("GET", ASSET, b"/assets%2Findex-AbC123.js"),
]


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    return build_dist(tmp_path)


@pytest.fixture
def spa_app(monkeypatch: pytest.MonkeyPatch, dist: Path) -> FastAPI:
    configure_spa(monkeypatch, dist)
    application = create_app()
    assert application.state.spa.enabled
    return application


def _served_by_spa(response: AsgiResponse) -> bool:
    return (
        response.status in (200, 304)
        and response.header("content-security-policy") == APPROVED_CSP
    )


@pytest.mark.parametrize(
    "method, path, raw_path, expected",
    ATTACK_MATRIX,
    ids=[f"{m} {p[:40]!r}" for m, p, _, _ in ATTACK_MATRIX],
)
async def test_attack_matrix_anonymous(
    spa_app: FastAPI, method: str, path: str, raw_path: bytes | None, expected: int
) -> None:
    response = await asgi_request(spa_app, method, path, raw_path=raw_path)

    assert response.status == expected
    assert not _served_by_spa(response)
    if expected == 401:
        assert response.body == (UNAUTHORIZED_BODY if method != "HEAD" else b"")


@pytest.mark.parametrize(
    "method, path, raw_path",
    POSITIVE_CONTROLS,
    ids=[f"{m} {p[:40]!r} {r!r}" for m, p, r in POSITIVE_CONTROLS],
)
async def test_exact_public_paths_are_served_anonymously(
    spa_app: FastAPI, method: str, path: str, raw_path: bytes | None
) -> None:
    response = await asgi_request(spa_app, method, path, raw_path=raw_path)

    assert response.status == 200
    assert _served_by_spa(response)
    expected = {
        ASSET: ASSET_FILES["index-AbC123.js"],
        "/favicon.png": TOP_LEVEL_FILES["favicon.png"],
    }.get(path, INDEX_HTML)
    assert response.body == (b"" if method == "HEAD" else expected)


async def test_head_billing_has_no_body_and_get_billing_api_is_unchanged(
    spa_app: FastAPI,
) -> None:
    head = await asgi_request(spa_app, "HEAD", "/billing")
    api = await asgi_request(spa_app, "GET", "/v1/billing/summary")

    assert (head.status, head.body) == (200, b"")
    assert head.header("content-length") == str(len(INDEX_HTML))
    assert (api.status, api.body) == (401, UNAUTHORIZED_BODY)
    assert api.header("content-type") == "application/json"


def _probe_paths(registry: SpaRegistry) -> list[tuple[str, bytes | None]]:
    probes: list[tuple[str, bytes | None]] = [
        (path, raw) for method, path, raw, _ in ATTACK_MATRIX if method == "GET"
    ]
    probes += [(path, None) for path in sorted(registry.pages | set(registry.files))]
    probes += [(path, raw) for _, path, raw in POSITIVE_CONTROLS]
    return probes


@pytest.mark.parametrize("authenticated", [False, True])
async def test_served_if_and_only_if_router_path_is_public(
    spa_app: FastAPI, authenticated: bool
) -> None:
    """No differential: auth's public decision == what the SPA serves."""

    registry: SpaRegistry = spa_app.state.spa
    mismatches = []
    for path, raw_path in _probe_paths(registry):
        if authenticated and path.startswith(("/v1", "/api/")):
            continue  # real API handlers (database); covered anonymously
        public = registry.is_public(get_route_path({"path": path, "root_path": ""}))
        headers = [bearer_header()] if authenticated else []
        response = await asgi_request(
            spa_app, "GET", path, raw_path=raw_path, headers=headers
        )
        if _served_by_spa(response) != public:
            mismatches.append((path, response.status, public))
        if not authenticated and not public and response.status != 401:
            mismatches.append((path, response.status, "anonymous non-public not 401"))

    assert mismatches == []


@pytest.mark.parametrize(
    "root_path, path, public",
    [
        ("/prefix", "/prefix/billing", True),
        ("/prefix", "/prefix/assets/index-AbC123.js", True),
        ("/prefix", "/prefix/traces/abc", True),
        ("/prefix", "/billing", True),
        ("/prefix", "/prefixbilling", False),
        ("/prefix", "/prefix/v1/admin/clinics", False),
        ("/prefix", "/prefix//billing", False),
        ("/prefix", "/prefix", False),
        ("/billing", "/billing", False),
        ("/assets", "/assets/index-AbC123.js", False),
    ],
)
@pytest.mark.parametrize("authenticated", [False, True])
async def test_root_path_decisions_follow_the_router(
    spa_app: FastAPI, root_path: str, path: str, public: bool, authenticated: bool
) -> None:
    scope = {"path": path, "root_path": root_path}
    assert spa_app.state.spa.is_public(get_route_path(scope)) is public

    response = await asgi_request(
        spa_app,
        "GET",
        path,
        root_path=root_path,
        headers=[bearer_header()] if authenticated else [],
    )

    assert _served_by_spa(response) is public
    if not public and not authenticated:
        assert response.status == 401


async def test_spa_hook_matches_router_path_not_request_url(
    spa_app: FastAPI,
) -> None:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/billing?x",
        "root_path": "",
        "headers": [(b"host", b"testserver")],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
    }
    # The differential the hook must not follow: request.url.path drops the
    # decoded "?" suffix, the router does not.
    assert Request(scope).url.path == "/billing"
    assert get_route_path(scope) == "/billing?x"

    response = await asgi_request(
        spa_app, "GET", "/billing?x", raw_path=b"/billing%3Fx"
    )

    assert response.status == 401


@pytest.mark.parametrize(
    "host, path",
    [
        (b"evil/billing?", "/v1/admin/clinics"),
        (b"evil/billing#", "/v1/x"),
        (b"evil/assets/index-AbC123.js?", "/v1/billing/summary"),
        (b"testserver/billing", "/v1/admin/clinics"),
    ],
)
async def test_host_header_cannot_select_a_public_path(
    spa_app: FastAPI, host: bytes, path: str
) -> None:
    response = await asgi_request(spa_app, "GET", path, host=host)

    assert (response.status, response.body) == (401, UNAUTHORIZED_BODY)


# --------------------------------------------------------------------------
# The auth surface itself is unchanged
# --------------------------------------------------------------------------


def test_public_paths_and_prefix_rules_are_unchanged(spa_app: FastAPI) -> None:
    assert AuthMiddleware.PUBLIC_PATHS == {
        "/",
        "/health",
        "/ready",
        "/live",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/v1/openapi.json",
        "/metrics",
        "/v1/auth/login",
    }
    middleware = AuthMiddleware(spa_app)
    registry: SpaRegistry = spa_app.state.spa
    for path in sorted(registry.pages | set(registry.files)) + ["/traces/abc"]:
        assert middleware._is_public_path(path) is (path == "/"), path


def _bare_auth_app() -> FastAPI:
    application = FastAPI()

    async def billing() -> dict[str, str]:
        return {"protected": "yes"}

    application.add_api_route("/billing", billing, methods=["GET", "HEAD"])
    application.add_middleware(AuthMiddleware)
    return application


async def test_hook_is_inert_without_an_installed_registry() -> None:
    application = _bare_auth_app()

    response = await asgi_request(application, "GET", "/billing")

    assert response.status == 401


async def test_hook_ignores_a_state_object_that_is_not_the_registry() -> None:
    class AlwaysPublic:
        def is_public(self, path: str) -> bool:
            return True

    application = _bare_auth_app()
    application.state.spa = AlwaysPublic()

    response = await asgi_request(application, "GET", "/billing")

    assert response.status == 401


async def test_hook_only_opens_get_and_head(monkeypatch: pytest.MonkeyPatch) -> None:
    application = _bare_auth_app()
    application.state.spa = SpaRegistry.disabled()
    monkeypatch.setattr(SpaRegistry, "is_public", lambda self, path: True)

    results = {
        method: (await asgi_request(application, method, "/billing")).status
        for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE")
    }

    assert results == {
        "GET": 200,
        "HEAD": 200,
        "POST": 401,
        "PUT": 401,
        "PATCH": 401,
        "DELETE": 401,
    }


# --------------------------------------------------------------------------
# /v1 is byte-for-byte unchanged with the SPA on
# --------------------------------------------------------------------------

API_MATRIX: list[tuple[str, str, bool]] = [
    ("GET", "/v1/billing/summary", False),
    ("HEAD", "/v1/billing/summary", False),
    ("OPTIONS", "/v1/billing/summary", False),
    ("POST", "/v1/billing/summary", False),
    ("GET", "/v1/x", False),
    ("GET", "/v1/x", True),
    ("GET", "/v1/admin/clinics", False),
    ("DELETE", "/v1/admin/clinics", False),
    ("GET", "/v1/billing/producer-build-trust", False),
    ("POST", "/v1/billing/producer-build-trust", False),
    ("POST", "/v1/auth/staging-rehearsal-token", False),
    ("GET", "/v1/billing/webhook/asaas", False),
    ("POST", "/health", False),
    ("PUT", "/health", False),
    ("GET", "/docs", False),
    ("GET", "/docs/oauth2-redirect", False),
    ("GET", "/redoc", False),
    ("GET", "/openapi.json", False),
    ("GET", "/v1/openapi.json", False),
    ("GET", "/api/public/x", False),
]


@pytest.fixture
def on_off_apps(monkeypatch: pytest.MonkeyPatch, dist: Path) -> tuple[FastAPI, FastAPI]:
    configure_spa(monkeypatch, None)
    off = create_app()
    configure_spa(monkeypatch, dist)
    on = create_app()
    assert not off.state.spa.enabled and on.state.spa.enabled
    return off, on


async def test_api_responses_are_byte_identical_with_spa_on(
    on_off_apps: tuple[FastAPI, FastAPI],
) -> None:
    off, on = on_off_apps
    differences = []
    for method, path, authenticated in API_MATRIX:
        # A fresh tenant per request: the in-memory rate-limit bucket is
        # process-global, so a shared one would differ in "remaining".
        before = await asgi_request(
            off, method, path, headers=[bearer_header()] if authenticated else []
        )
        after = await asgi_request(
            on, method, path, headers=[bearer_header()] if authenticated else []
        )
        if before.stable() != after.stable():
            differences.append((method, path, before.stable(), after.stable()))

    assert differences == []


async def test_openapi_documents_are_identical_with_spa_on(
    on_off_apps: tuple[FastAPI, FastAPI],
) -> None:
    off, on = on_off_apps
    for path in ("/openapi.json", "/v1/openapi.json"):
        before = await asgi_request(off, "GET", path)
        after = await asgi_request(on, "GET", path)
        assert before.status == after.status == 200
        assert before.body == after.body
    paths = json.loads(after.body)["paths"]
    assert not any(p in paths for p in ("/billing", "/", "/traces/{trace_id}"))


def test_route_table_is_the_off_table_plus_appended_spa_routes(
    on_off_apps: tuple[FastAPI, FastAPI],
) -> None:
    off, on = on_off_apps
    before = [route_signature(route) for route in off.routes]
    after = [route_signature(route) for route in on.routes]
    registry: SpaRegistry = on.state.spa

    assert after[: len(before)] == before
    appended = on.routes[len(before) :]
    top_level_files = [p for p in registry.files if not p.startswith("/assets/")]
    assert len(appended) == len(registry.pages) + len(top_level_files) + 2
    for route in appended[:-1]:
        assert getattr(route, "endpoint").__qualname__ == (
            "_page_endpoint.<locals>.serve_spa"
        )
        assert set(getattr(route, "methods")) == {"GET", "HEAD"}
    assert isinstance(appended[-1], Mount)
    assert appended[-1].path == "/assets"
    assert type(appended[-1].app).__name__ == "_AssetsApp"


async def test_raw_path_differences_do_not_change_decisions(spa_app: FastAPI) -> None:
    """The decision uses scope["path"]; raw_path is never consulted."""

    for raw in (b"/billing", b"/%62illing", b"/v1/admin/clinics", b"//billing"):
        response = await asgi_request(spa_app, "GET", "/billing", raw_path=raw)
        assert response.status == 200
    for raw in (b"/billing", default_raw_path("/v1/x")):
        response = await asgi_request(spa_app, "GET", "/v1/x", raw_path=raw)
        assert response.status == 401
