"""The real route table cannot reopen an auth/router path disagreement.

AuthMiddleware decides on path strings (``src/api/routing_paths.py``); the
router decides by matching routes.  Two route-table shapes would let them
disagree again even though both read the same path:

(a) a parameter route whose literal prefix is a prefix of an exact public
    path, a rehearsal target, a guarded ingress path or a public prefix: its
    parameter can produce that string, so auth would judge the public path
    while the router dispatches the private route (``/v1/{x}`` against
    ``/v1/openapi.json``; Starlette's ``[^/]+`` also matches a trailing
    newline, so equality with the literal prefix counts too);
(b) a route path containing characters that make ``request.url.path`` and
    the router path differ (tab, CR, LF, ``?``, ``#``, other C0 controls) or
    that is not origin-form.

The enumeration covers every effective route: included routers resolved to
what FastAPI dispatches, mounts (and mounted sub-routers), hosts, websockets
and the SPA routes, with the SPA off and on.  The public paths come from
AuthMiddleware itself, the single source.  Each check is also run against a
synthetic application with known offenders, so a check that silently stops
seeing a route kind fails here too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from starlette.requests import Request
from starlette.routing import Route, Router

from src.api.main import create_app
from src.api.middleware.auth import (
    PUBLIC_ASAAS_WEBHOOK_PATH,
    PUBLIC_DEMO_LEADS_PATH,
    PUBLIC_TRUST_READBACK_PATHS,
    STAGING_REHEARSAL_TARGETS,
    STAGING_REHEARSAL_TOKEN_PATH,
    AuthMiddleware,
)
from src.api.middleware.ingress_body_limit import _FIXED_LIMITED_ENDPOINTS
from src.api.routing_paths import decision_path, route_path
from src.api.spa import SpaRegistry
from src.config.settings import settings
from src.services.billing_webhook import ASAAS_WEBHOOK_PATH
from tests.api._route_path_support import (
    EffectiveRoute,
    anonymous_public,
    effective_routes,
)
from tests.api._spa_support import build_dist, configure_spa

# Characters ``urllib.parse`` drops or splits on while building
# ``request.url.path``, plus every other C0 control and DEL.
_DIVERGING = frozenset("?#") | frozenset(chr(c) for c in (*range(0x20), 0x7F))

Offender = tuple[Any, ...]


@pytest.fixture(params=["spa_off", "spa_on"])
def app(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> FastAPI:
    if request.param == "spa_on":
        configure_spa(monkeypatch, build_dist(tmp_path))
    else:
        configure_spa(monkeypatch, None)
    application = create_app()
    assert application.state.spa.enabled is (request.param == "spa_on")
    return application


def _routes(app: FastAPI) -> list[EffectiveRoute]:
    return list(effective_routes(app))


def _spa_owned(route: EffectiveRoute) -> bool:
    """SPA endpoints re-check the exact router path against the registry the
    auth hook uses, so what they serve and what auth opens cannot diverge."""

    return (
        getattr(route.endpoint, "__qualname__", "")
        == ("_page_endpoint.<locals>.serve_spa")
        or type(route.endpoint).__name__ == "_AssetsApp"
    )


def _exact_public_targets(app: FastAPI) -> set[str]:
    """Every exact string an auth or ingress decision grants or guards."""

    registry: SpaRegistry = app.state.spa
    return {
        *AuthMiddleware.PUBLIC_PATHS,
        PUBLIC_ASAAS_WEBHOOK_PATH,
        *PUBLIC_TRUST_READBACK_PATHS,
        PUBLIC_DEMO_LEADS_PATH,
        STAGING_REHEARSAL_TOKEN_PATH,
        *(path for _method, path in STAGING_REHEARSAL_TARGETS),
        ASAAS_WEBHOOK_PATH,
        *_FIXED_LIMITED_ENDPOINTS,
        f"{settings.api_prefix}/media-assets",
        *registry.pages,
        *registry.files,
    }


def _literal_prefix(route: EffectiveRoute) -> str | None:
    """Literal text a route's matcher requires before any free part."""

    if not isinstance(route.path, str):
        return None
    if route.kind == "mount":
        return route.path + "/"  # Mount matches "<path>/{path:path}"
    if "{" in route.path:
        return route.path.split("{", 1)[0]
    return None


def _public_by_design(route: EffectiveRoute, literal: str) -> bool:
    return _spa_owned(route) or literal.startswith(AuthMiddleware.PUBLIC_PATH_PREFIXES)


def exact_public_offenders(app: FastAPI) -> list[Offender]:
    """(a) parameter routes that can produce an exact public/guarded path."""

    targets = _exact_public_targets(app)
    offenders: list[Offender] = []
    for route in _routes(app):
        literal = _literal_prefix(route)
        if literal is None or _public_by_design(route, literal):
            continue
        offenders += [
            (route.path, target) for target in targets if target.startswith(literal)
        ]
    return offenders


def prefix_public_offenders(app: FastAPI) -> list[Offender]:
    """(a) parameter routes that can produce a path in a public namespace."""

    offenders: list[Offender] = []
    for route in _routes(app):
        literal = _literal_prefix(route)
        if literal is None or _public_by_design(route, literal):
            continue
        offenders += [
            (route.path, prefix)
            for prefix in AuthMiddleware.PUBLIC_PATH_PREFIXES
            if prefix.startswith(literal)
        ]
    return offenders


def diverging_path_offenders(app: FastAPI) -> list[Offender]:
    """(b) route paths auth could misjudge through ``request.url.path``."""

    offenders: list[Offender] = []
    for route in _routes(app):
        path = route.path
        if not isinstance(path, str):
            offenders.append((route.kind, None, "no analyzable path"))
            continue
        if route.kind == "mount" and path == "":
            offenders.append((route.kind, path, "root mount"))
            continue
        if not path.startswith("/") or path.startswith("//"):
            offenders.append((route.kind, path, "not origin-form"))
        if _DIVERGING & set(path):
            offenders.append((route.kind, path, "diverging character"))
    return offenders


def round_trip_offenders(app: FastAPI) -> list[Offender]:
    """(b) a sample of each route path reads the same through
    ``request.url.path`` and both helpers, so the three can only differ on
    input the router does not dispatch to that route."""

    offenders: list[Offender] = []
    for route in _routes(app):
        if not isinstance(route.path, str):
            continue
        sample = route.path.replace("{", "x").replace("}", "")
        if route.kind == "mount":
            sample += "/x"
        scope = {
            "type": "http",
            "path": sample,
            "root_path": "",
            "headers": [(b"host", b"testserver")],
            "query_string": b"",
            "scheme": "http",
            "server": ("testserver", 80),
        }
        observed = {Request(scope).url.path, route_path(scope), decision_path(scope)}
        if observed != {sample}:
            offenders.append((route.path, sorted(observed)))
    return offenders


def websocket_offenders(app: FastAPI) -> list[Offender]:
    """AuthMiddleware passes non-HTTP scopes through unauthenticated."""

    return [(route.path,) for route in _routes(app) if route.kind == "websocket"]


# --------------------------------------------------------------------------
# The real application
# --------------------------------------------------------------------------


def test_enumeration_reaches_included_routers_mounts_and_spa(app: FastAPI) -> None:
    routes = _routes(app)
    paths = {route.path for route in routes}

    # included routers are resolved: far more routes than top-level entries
    assert len(routes) > 2 * len(app.router.routes)
    assert {
        "/v1/admin/clinics/{clinic_id}",
        "/v1/webhook/zapi/{capability_token}",
        "/v1/billing/webhook/asaas",
        "/privacidade/{clinic_id}",
        "/docs",
        "/openapi.json",
    } <= paths
    assert ("mount", "/metrics") in {(r.kind, r.path) for r in routes}
    if app.state.spa.enabled:
        assert ("mount", "/assets") in {(r.kind, r.path) for r in routes}
        assert "/traces/{trace_id}" in paths


def test_no_parameter_route_can_produce_an_exact_public_path(app: FastAPI) -> None:
    assert exact_public_offenders(app) == []


def test_no_parameter_route_can_produce_a_public_prefix(app: FastAPI) -> None:
    assert prefix_public_offenders(app) == []


def test_route_paths_cannot_split_url_path_from_router_path(app: FastAPI) -> None:
    assert diverging_path_offenders(app) == []


def test_route_paths_round_trip_through_request_url(app: FastAPI) -> None:
    assert round_trip_offenders(app) == []


def test_no_websocket_route_exists_without_its_own_authentication(
    app: FastAPI,
) -> None:
    """A websocket route needs its own auth review before it may appear."""

    assert websocket_offenders(app) == []


# Every route an anonymous caller may reach, as AuthMiddleware decides it.
# A new entry here is a new unauthenticated surface: review it, then pin it.
EXPECTED_ANONYMOUS_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/public/privacidade/{clinic_id}"),
        ("GET", "/docs"),
        ("HEAD", "/docs"),
        ("GET", "/docs/oauth2-redirect"),
        ("HEAD", "/docs/oauth2-redirect"),
        ("GET", "/health"),
        ("GET", "/live"),
        ("*", "/metrics"),
        ("GET", "/metrics/summary"),
        ("GET", "/openapi.json"),
        ("GET", "/privacidade/{clinic_id}"),
        ("GET", "/ready"),
        ("GET", "/redoc"),
        ("HEAD", "/redoc"),
        ("GET", "/v1/admin/billing/producer-build-trust"),
        ("POST", "/v1/auth/login"),
        ("POST", "/v1/auth/staging-rehearsal-token"),
        ("GET", "/v1/billing/producer-build-trust"),
        ("POST", "/v1/billing/webhook/asaas"),
        ("POST", "/v1/demo-leads"),
        ("GET", "/v1/openapi.json"),
        ("HEAD", "/v1/openapi.json"),
        ("POST", "/v1/webhook/evolution"),
        ("GET", "/v1/webhook/meta"),
        ("POST", "/v1/webhook/meta"),
        ("POST", "/v1/webhook/uazapi"),
        ("POST", "/v1/webhook/zapi"),
        ("POST", "/v1/webhook/zapi/{capability_token}"),
    }
)


def test_anonymous_route_surface_is_pinned(app: FastAPI) -> None:
    surface: set[tuple[str, str]] = set()
    for route in _routes(app):
        if not isinstance(route.path, str) or _spa_owned(route):
            continue
        if route.kind == "mount":
            if (route.path + "/").startswith(AuthMiddleware.PUBLIC_PATH_PREFIXES):
                surface.add(("*", route.path))
            continue
        literal = route.path.split("{", 1)[0]
        for method in route.methods:
            # The one shared oracle (``_route_path_support.anonymous_public``);
            # for a parameter route only its literal prefix can be public.
            public = (
                literal.startswith(AuthMiddleware.PUBLIC_PATH_PREFIXES)
                if "{" in route.path
                else anonymous_public(method, route.path)
            )
            if public:
                surface.add((method, route.path))

    assert surface == EXPECTED_ANONYMOUS_ROUTES


def test_public_path_sources_are_unchanged() -> None:
    """The exported sources keep the pre-refactor values exactly."""

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
    assert AuthMiddleware.PUBLIC_PATH_PREFIXES == (
        "/docs",
        "/redoc",
        "/metrics",
        "/v1/webhook/",
        "/privacidade/",
        "/api/public/privacidade/",
    )
    assert (
        PUBLIC_ASAAS_WEBHOOK_PATH == ASAAS_WEBHOOK_PATH == "/v1/billing/webhook/asaas"
    )
    assert PUBLIC_TRUST_READBACK_PATHS == frozenset(
        {"/v1/billing/producer-build-trust", "/v1/admin/billing/producer-build-trust"}
    )
    assert PUBLIC_DEMO_LEADS_PATH == "/v1/demo-leads"
    assert STAGING_REHEARSAL_TOKEN_PATH == "/v1/auth/staging-rehearsal-token"


# --------------------------------------------------------------------------
# The checks see every route kind (synthetic offenders)
# --------------------------------------------------------------------------


async def _endpoint() -> dict[str, str]:
    return {}


async def _ws_endpoint(websocket: Any) -> None:
    return None


def _app_with_offenders() -> FastAPI:
    """Every offender lives below the top level, where only a complete
    enumeration (included routers, nested includes, mounted sub-routers,
    hosts, websockets) can see it."""

    application = FastAPI()
    application.state.spa = SpaRegistry.disabled()
    api = APIRouter()
    api.add_api_route("/{anything}", _endpoint, methods=["DELETE"])
    api.add_api_route("/demo-leads{suffix}", _endpoint, methods=["PUT"])
    api.add_api_route("/odd\tpath", _endpoint, methods=["GET"])
    api.add_api_websocket_route("/socket", _ws_endpoint)
    nested = APIRouter()
    nested.add_api_route("/{step}/readback", _endpoint, methods=["GET"])
    api.include_router(nested, prefix="/admin/billing/rollout")
    application.include_router(api, prefix="/v1")
    application.mount(
        "/v2",
        Router(routes=[Route("/mounted?child", _endpoint), Route("/ok", _endpoint)]),
    )
    application.host("internal.example", Router(routes=[]))
    return application


def test_checks_find_offenders_in_included_routers() -> None:
    application = _app_with_offenders()

    exact = exact_public_offenders(application)
    prefix = prefix_public_offenders(application)

    assert ("/v1/{anything}", "/v1/openapi.json") in exact
    assert ("/v1/demo-leads{suffix}", "/v1/demo-leads") in exact  # equality
    assert (
        "/v1/admin/billing/rollout/{step}/readback",
        "/v1/admin/billing/rollout/readback",
    ) in exact  # nested include, rehearsal target
    assert ("/v1/{anything}", "/v1/webhook/") in prefix


def test_checks_find_diverging_paths_in_mounts_and_hosts() -> None:
    application = _app_with_offenders()

    diverging = diverging_path_offenders(application)
    round_trip = {path for path, *_ in round_trip_offenders(application)}

    assert ("http", "/v1/odd\tpath", "diverging character") in diverging
    assert ("http", "/v2/mounted?child", "diverging character") in diverging
    assert ("host", None, "no analyzable path") in diverging
    assert {"/v1/odd\tpath", "/v2/mounted?child"} <= round_trip
    assert "/v2/ok" not in round_trip


def test_checks_find_websockets_in_included_routers() -> None:
    assert websocket_offenders(_app_with_offenders()) == [("/v1/socket",)]
