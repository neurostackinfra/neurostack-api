"""Shared helpers for the router-path decision tests (not collected by pytest).

Requests are driven as raw ASGI scopes so the decoded ``scope["path"]`` is
exactly what a server such as uvicorn hands over: HTTP clients would
normalize ``//x``, dot segments, encodings or a raw newline first.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from fastapi.routing import iter_route_contexts
from starlette.routing import BaseRoute, Host, Mount, WebSocketRoute
from starlette.types import Message, Scope

from src.api.middleware.auth import (
    PUBLIC_ASAAS_WEBHOOK_PATH,
    PUBLIC_DEMO_LEADS_PATH,
    PUBLIC_TRUST_READBACK_PATHS,
    STAGING_REHEARSAL_TOKEN_PATH,
    AuthMiddleware,
)
from tests.api._spa_support import default_raw_path

# The AuthMiddleware's own 401 for a request without credentials.
MISSING_CREDENTIALS_BODY = (
    b'{"error":"unauthorized","message":"Missing Authorization or X-API-Key '
    b'header","documentation_url":"https://docs.neurostack.ai/auth"}'
)


def anonymous_public(method: str, path: str) -> bool:
    """The one oracle for AuthMiddleware's anonymous-access rules.

    Both router-path test modules import this (no second copy to drift from).
    It is derived from the exported sources, and it re-states the per-method
    rules (GET-only trust readbacks, POST-only demo-leads, OPTIONS) and the
    prefix rule *independently of* ``_is_public_path`` -- a prefix check, not
    a substring one -- so that a middleware that widens any of those rules
    disagrees with this oracle instead of co-mutating with it.  The exact
    public paths are the only part read straight from the middleware, because
    an exact-set membership has no narrower/wider variant to drift from.
    """

    if method == "OPTIONS":
        return True  # every OPTIONS passes auth (CORS preflight)
    if method == "POST" and path == STAGING_REHEARSAL_TOKEN_PATH:
        return True  # own admission: throttle, then the issuer's credentials
    if method == "GET" and path in PUBLIC_TRUST_READBACK_PATHS:
        return True
    if method == "POST" and path == PUBLIC_DEMO_LEADS_PATH:
        return True
    if path in AuthMiddleware.PUBLIC_PATHS or path == PUBLIC_ASAAS_WEBHOOK_PATH:
        return True
    return path.startswith(AuthMiddleware.PUBLIC_PATH_PREFIXES)


@dataclass(frozen=True)
class Probe:
    """One request: decoded ``scope["path"]``, as an ASGI server delivers it."""

    label: str
    method: str
    path: str
    raw_path: bytes | None = None
    root_path: str = ""
    body: bytes = b""

    @property
    def id(self) -> str:
        root = f" root={self.root_path!r}" if self.root_path else ""
        return f"{self.label}: {self.method} {self.path[:48]!r}{root}"


def _probes(label: str, method: str, *paths: str) -> list[Probe]:
    return [Probe(label, method, path) for path in paths]


# Every category of path where request.url.path and the router path can
# differ, against public exact paths, public prefixes, ingress-guarded paths,
# rehearsal targets and protected routes.
ATTACK_CORPUS: list[Probe] = [
    # percent-encoding: the server decodes before the app sees the path
    Probe("pct", "GET", "/health", b"/h%65alth"),
    Probe("pct", "GET", "/v1/admin/clinics", b"/v1/%61dmin/clinics"),
    Probe("pct", "GET", "/health%2F", b"/health%252F"),
    Probe("pct", "GET", "/v1/auth/login%00", b"/v1/auth/login%2500"),
    Probe("pct", "GET", "/v1/webhook/../admin/clinics", b"/v1/webhook%2F..%2Fadmin"),
    Probe("pct", "GET", "/docs/../v1/admin/clinics", b"/docs/%2e%2e/v1/admin/clinics"),
    # %0a: removed anywhere by request.url.path; one trailing one still routes
    *_probes(
        "lf",
        "GET",
        "/\n",
        "/health\n",
        "/hea\nlth",
        "/health\n\n",
        "\n/health",
        "/v1/auth/login\n",
        "/v1/auth/log\nin",
        "/v1/openapi.json\n",
        "/v1/admin/clinics\n",
        "/v1/admin/clinics\n/x",
        "/v1/billing/producer-build-trust\n",
        "/v1/admin/billing/producer-build-trust\n",
        "/v1/admin/billing/rollout/readback\n",
        "/docs\n",
        "/metrics\n",
        "/privacidade/x\n",
    ),
    *_probes(
        "lf",
        "POST",
        "/v1/billing/webhook/asaas\n",
        "/v1/webhook/meta\n",
        "/v1/demo-leads\n",
        "/v1/auth/staging-rehearsal-token\n",
        "/v1/events\n",
        "/v1/media-assets\n",
        "/v1/admin/billing/producer-grants/assignments\n",
    ),
    # %09 and %0d
    *_probes(
        "tab",
        "GET",
        "/health\t",
        "/he\talth",
        "\t/health",
        "/v1/auth/login\t",
        "/v1/admin/clin\tics",
    ),
    *_probes(
        "cr",
        "GET",
        "/health\r",
        "/health\r\n",
        "/v1/auth/login\r\n",
        "/v1/admin/clinics\r",
    ),
    Probe("tab", "POST", "/v1/billing/webhook/as\taas"),
    Probe("cr", "POST", "/v1/webhook/me\rta"),
    # a decoded "?" or "#" truncates request.url.path, not the router path
    Probe("qmark", "GET", "/health?x", b"/health%3Fx"),
    Probe("qmark", "GET", "/?x", b"/%3Fx"),
    Probe("qmark", "GET", "/v1/auth/login?x", b"/v1/auth/login%3Fx"),
    Probe("qmark", "GET", "/v1/openapi.json?/../admin/clinics"),
    Probe("qmark", "GET", "/v1/admin/clinics?x", b"/v1/admin/clinics%3Fx"),
    Probe("qmark", "GET", "/v1/billing/producer-build-trust?x"),
    Probe("qmark", "POST", "/v1/billing/webhook/asaas?x"),
    Probe("qmark", "POST", "/v1/demo-leads?x"),
    Probe("qmark", "POST", "/v1/webhook/meta?x"),
    Probe("hash", "GET", "/health#x", b"/health%23x"),
    Probe("hash", "GET", "/#", b"/%23"),
    Probe("hash", "GET", "/v1/auth/login#/../admin/clinics"),
    Probe("hash", "GET", "/v1/admin/billing/producer-build-trust#x"),
    Probe("hash", "POST", "/v1/billing/webhook/asaas#x"),
    # double slashes
    *_probes(
        "slashes",
        "GET",
        "//health",
        "///health",
        "//v1/admin/clinics",
        "/v1//admin/clinics",
        "/v1/webhook//meta",
    ),
    Probe("slashes", "POST", "//v1/webhook/meta"),
    Probe("slashes", "POST", "//v1/billing/webhook/asaas"),
    # dot segments (the router never normalizes them)
    *_probes(
        "dots",
        "GET",
        "/./health",
        "/health/.",
        "/v1/admin/../auth/login",
        "/v1/auth/login/../../admin/clinics",
        "/privacidade/../v1/admin/clinics",
        "/api/public/privacidade/../../../v1/admin/clinics",
        "/metrics/../v1/admin/clinics",
    ),
    # trailing slash
    *_probes("slash", "GET", "/health/", "/v1/admin/clinics/", "/docs/"),
    Probe("slash", "POST", "/v1/auth/login/"),
    Probe("slash", "POST", "/v1/billing/webhook/asaas/"),
    Probe("slash", "POST", "/v1/demo-leads/"),
    # case
    *_probes("case", "GET", "/HEALTH", "/Health", "/V1/AUTH/LOGIN", "/DOCS"),
    Probe("case", "POST", "/v1/WEBHOOK/meta"),
    Probe("case", "POST", "/V1/billing/webhook/asaas"),
    # ;params
    *_probes(
        "params", "GET", "/health;x", "/v1/auth/login;x", "/v1/admin/clinics;/health"
    ),
    Probe("params", "POST", "/v1/webhook/meta;x"),
    Probe("params", "POST", "/v1/billing/webhook/asaas;x"),
    # absolute-form and other non-origin-form targets (uvicorn's h11 backend
    # hands the whole target over as the path)
    *_probes(
        "absolute",
        "GET",
        "http://testserver/health",
        "http://evil/v1/admin/clinics",
        "https://testserver/v1/auth/login",
        "health",
        "v1/auth/login",
        "*",
        "",
    ),
    Probe("absolute", "POST", "http://testserver/v1/billing/webhook/asaas"),
    # NUL, spaces
    *_probes(
        "other", "GET", "/health\x00", "/health ", " /health", "/v1/auth/login\x00"
    ),
    # methods
    Probe("method", "HEAD", "/v1/admin/clinics"),
    Probe("method", "HEAD", "/health?x"),
    Probe("method", "OPTIONS", "/v1/admin/clinics"),
    Probe("method", "DELETE", "/v1/admin/clinics/abc"),
    # a public-prefix string appearing INSIDE a protected parameter route:
    # a prefix decision is not a substring one.
    Probe("inside", "GET", "/v1/sessions/metrics"),
    Probe("inside", "GET", "/v1/sessions/docs"),
    Probe("inside", "GET", "/v1/handoffs/docs"),
    Probe("inside", "GET", "/v1/knowledge/documents/metrics"),
    Probe("inside", "GET", "/v1/traces/redoc"),
    Probe("inside", "DELETE", "/v1/admin/clinics/redoc"),
    Probe("inside", "DELETE", "/v1/admin/clinics/metrics"),
    Probe("inside", "GET", "/v1/builder/clinics/docs/clone-preview"),
    # per-method public rules exercised on the wrong method
    Probe("method", "HEAD", "/v1/billing/producer-build-trust"),
    Probe("method", "HEAD", "/v1/admin/billing/producer-build-trust"),
    Probe("method", "GET", "/v1/demo-leads"),
    Probe("method", "HEAD", "/v1/billing/summary"),
    # root_path: the router strips it, request.url.path keeps it
    Probe("root", "GET", "/prefix/health", root_path="/prefix"),
    Probe("root", "GET", "/prefix/v1/admin/clinics", root_path="/prefix"),
    Probe("root", "GET", "/prefix/v1/auth/login", root_path="/prefix"),
    Probe("root", "GET", "/health", root_path="/prefix"),
    Probe("root", "GET", "/prefixhealth", root_path="/prefix"),
    Probe("root", "GET", "/prefix", root_path="/prefix"),
    Probe("root", "POST", "/prefix/v1/billing/webhook/asaas\n", root_path="/prefix"),
    Probe("root", "GET", "/v1/admin/clinics", root_path="/v1/admin"),
]

# Anonymous requests that must still be served by their own public route.
PUBLIC_CONTROLS: list[tuple[Probe, str]] = [
    (Probe("public", "GET", "/health"), "/health"),
    (Probe("public", "GET", "/live"), "/live"),
    (Probe("public", "GET", "/docs"), "/docs"),
    (Probe("public", "GET", "/docs/oauth2-redirect"), "/docs/oauth2-redirect"),
    (Probe("public", "GET", "/redoc"), "/redoc"),
    (Probe("public", "GET", "/openapi.json"), "/openapi.json"),
    (Probe("public", "GET", "/v1/openapi.json"), "/v1/openapi.json"),
    (Probe("public", "GET", "/metrics/"), "/metrics"),
    (Probe("public", "POST", "/v1/auth/login", body=b"{}"), "/v1/auth/login"),
    (Probe("public", "POST", "/v1/demo-leads", body=b"{}"), "/v1/demo-leads"),
    (
        Probe("public", "GET", "/v1/billing/producer-build-trust"),
        "/v1/billing/producer-build-trust",
    ),
    (
        Probe("public", "GET", "/v1/admin/billing/producer-build-trust"),
        "/v1/admin/billing/producer-build-trust",
    ),
    (Probe("public", "GET", "/privacidade/abc"), "/privacidade/{clinic_id}"),
    (
        Probe("public", "GET", "/api/public/privacidade/abc"),
        "/api/public/privacidade/{clinic_id}",
    ),
    # the router strips root_path; the public decision follows it
    (Probe("public", "GET", "/prefix/health", root_path="/prefix"), "/health"),
    (Probe("public", "GET", "/prefix/docs", root_path="/prefix"), "/docs"),
]

SPA_PUBLIC_CONTROLS: list[tuple[Probe, str]] = [
    (Probe("spa", "GET", "/"), "/"),
    (Probe("spa", "GET", "/billing"), "/billing"),
    (Probe("spa", "HEAD", "/billing/"), "/billing/"),
    (Probe("spa", "GET", "/traces/abc"), "/traces/{trace_id}"),
    (Probe("spa", "GET", "/assets/index-AbC123.js"), "/assets"),
    (Probe("spa", "GET", "/favicon.png"), "/favicon.png"),
]


@dataclass
class Outcome:
    status: int | None = None
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""
    error: str | None = None
    scope: dict[str, Any] = field(default_factory=dict)

    @property
    def endpoint(self) -> Any:
        """What the router dispatched to (route endpoint or mounted app)."""
        return self.scope.get("endpoint")

    def header(self, name: str) -> str | None:
        values = [v for k, v in self.headers if k == name.lower()]
        return values[0] if values else None


async def drive(
    app: Any,
    probe: Probe,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Outcome:
    """Run ``probe`` through the full ASGI ``app``; never raises."""

    request_headers = [(b"host", b"testserver"), *(headers or [])]
    if probe.body:
        request_headers += [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(probe.body)).encode("ascii")),
        ]
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": probe.method,
        "scheme": "http",
        "path": probe.path,
        "raw_path": (
            default_raw_path(probe.path) if probe.raw_path is None else probe.raw_path
        ),
        "root_path": probe.root_path,
        "query_string": b"",
        "headers": request_headers,
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    queued: list[Message] = [
        {"type": "http.request", "body": probe.body, "more_body": False}
    ]
    outcome = Outcome(scope=scope)

    async def receive() -> Message:
        if queued:
            return queued.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            outcome.status = message["status"]
            outcome.headers = [
                (k.decode("latin-1").lower(), v.decode("latin-1"))
                for k, v in message.get("headers", [])
            ]
        elif message["type"] == "http.response.body":
            outcome.body += message.get("body", b"")

    try:
        await app(scope, receive, send)
    except Exception as error:  # a reached handler without a database
        outcome.error = type(error).__name__
    return outcome


@dataclass(frozen=True)
class EffectiveRoute:
    """One route as the top-level router dispatches it."""

    kind: str  # "http", "mount", "websocket", "host"
    path: object  # effective path (None for a Host)
    methods: frozenset[str]
    endpoint: Any  # route endpoint, or the mounted app
    route: BaseRoute


def effective_routes(app: FastAPI) -> Iterator[EffectiveRoute]:
    """Every effective route, included routers resolved to what is dispatched.

    An included non-API route is dispatched through a re-built, prefixed
    Starlette route (``starlette_route``); included ``APIRoute`` contexts
    carry their effective (prefixed) path on the context itself.  Mounted
    sub-routers are walked with their prefix.
    """

    for context in iter_route_contexts(app.router.routes):
        starlette_route = getattr(context, "starlette_route", None)
        if isinstance(starlette_route, BaseRoute):
            route: BaseRoute = starlette_route
            path = getattr(starlette_route, "path", None)
        else:
            route = context.original_route
            path = context.path
        yield from _expand(route, path, getattr(context, "methods", None))


def _expand(route: BaseRoute, path: object, methods: Any) -> Iterator[EffectiveRoute]:
    if isinstance(route, Host):
        # No path to analyze: reported as such (fails closed), not descended.
        yield EffectiveRoute("host", None, frozenset(), route.app, route)
        return
    if isinstance(route, Mount):
        yield EffectiveRoute("mount", path, frozenset(), route.app, route)
        for child in getattr(route, "routes", None) or ():
            child_path = getattr(child, "path", None)
            joined = f"{path}{child_path}" if isinstance(child_path, str) else None
            yield from _expand(child, joined, getattr(child, "methods", None))
        return
    endpoint = getattr(route, "endpoint", None)
    if isinstance(route, WebSocketRoute):  # APIWebSocketRoute included
        yield EffectiveRoute("websocket", path, frozenset(), endpoint, route)
        return
    yield EffectiveRoute(
        "http",
        path,
        frozenset(methods or getattr(route, "methods", None) or ()),
        endpoint,
        route,
    )
