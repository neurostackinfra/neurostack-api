"""Every request-path decision is taken on the path the router matches.

``request.url.path`` and the router path (``get_route_path(scope)``) differ
for decoded ``?``/``#``, tab/CR/LF, a missing leading slash and
``root_path``.  These tests drive an attack corpus (``_route_path_support``)
through the real application and through each path-deciding middleware and
prove that the auth decision, the ingress/identity guards, the cache and the
rate limiter judge the same path the router dispatches, that anonymous
requests never reach a protected handler and that public paths are still
served.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import jwt
import pytest
from fastapi import FastAPI, Request
from pydantic import SecretStr
from starlette._utils import get_route_path
from starlette.requests import Request as StarletteRequest
from starlette.routing import compile_path

from src.api.main import create_app
from src.api.middleware.auth import (
    PUBLIC_ASAAS_WEBHOOK_PATH,
    PUBLIC_DEMO_LEADS_PATH,
    PUBLIC_TRUST_READBACK_PATHS,
    STAGING_REHEARSAL_TARGETS,
    STAGING_REHEARSAL_TOKEN_ENVIRONMENT,
    STAGING_REHEARSAL_TOKEN_PATH,
    STAGING_REHEARSAL_TOKEN_SCOPE,
    STAGING_REHEARSAL_TOKEN_USE,
    AuthMiddleware,
    TenantContext,
)
from src.api.middleware.ingress_body_limit import (
    _FIXED_LIMITED_ENDPOINTS,
    MAX_INGRESS_BODY_BYTES,
    STAGING_REHEARSAL_TOKEN_BODY_BYTES,
)
from src.api.middleware.media_limits import (
    MediaConcurrencyLimiters,
    MediaUploadLimitsMiddleware,
)
from src.api.middleware.performance import CachingMiddleware, response_cache
from src.api.middleware.rate_limit import RateLimitMiddleware, rate_limit_store
from src.api.middleware.tenant_identity import TenantIdentityMiddleware
from src.api.routing_paths import decision_path, route_path
from src.api.spa import SpaRegistry, is_public_spa_path
from src.config.settings import settings
from src.services.billing_webhook import ASAAS_WEBHOOK_PATH
from tests.api._route_path_support import (
    ATTACK_CORPUS,
    MISSING_CREDENTIALS_BODY,
    PUBLIC_CONTROLS,
    SPA_PUBLIC_CONTROLS,
    Outcome,
    Probe,
    anonymous_public,
    drive,
    effective_routes,
)
from tests.api._spa_support import build_dist, configure_spa


@pytest.fixture(autouse=True)
def isolated_process_state() -> Iterator[None]:
    """The response cache and rate-limit buckets are process-global."""

    response_cache.invalidate()
    rate_limit_store._buckets.clear()
    rate_limit_store._tenant_limits.clear()
    yield
    response_cache.invalidate()
    rate_limit_store._buckets.clear()
    rate_limit_store._tenant_limits.clear()


def _scope(probe: Probe) -> dict[str, Any]:
    return {
        "type": "http",
        "method": probe.method,
        "path": probe.path,
        "root_path": probe.root_path,
        "headers": [(b"host", b"testserver")],
        "query_string": b"",
        "scheme": "http",
        "server": ("testserver", 80),
    }


# Every exact literal any middleware decides on.  The fold must be exactly
# the one Starlette's literal matcher applies to each of them.
DECISION_LITERALS: frozenset[str] = frozenset(
    {
        *AuthMiddleware.PUBLIC_PATHS,
        PUBLIC_ASAAS_WEBHOOK_PATH,
        *PUBLIC_TRUST_READBACK_PATHS,
        PUBLIC_DEMO_LEADS_PATH,
        STAGING_REHEARSAL_TOKEN_PATH,
        *(path for _method, path in STAGING_REHEARSAL_TARGETS),
        ASAAS_WEBHOOK_PATH,
        *_FIXED_LIMITED_ENDPOINTS,
        "/v1/media-assets",
        *RateLimitMiddleware.EXEMPT_PATHS,
        *CachingMiddleware.CACHEABLE_PATHS,
        *CachingMiddleware.NEVER_CACHE,
    }
)


def _fold_probes() -> list[Probe]:
    probes = list(ATTACK_CORPUS)
    for literal in sorted(DECISION_LITERALS):
        for variant in (
            literal,
            literal + "\n",
            literal + "\n\n",
            literal + "\r\n",
            literal + "\r",
            literal + "\t",
            literal + "?",
            literal + "#",
            literal + "/",
            "\n" + literal,
            literal[:-1] + "\n" + literal[-1:],
        ):
            probes.append(Probe("fold", "GET", variant))
            probes.append(
                Probe("fold", "GET", "/prefix" + variant, root_path="/prefix")
            )
    return probes


FOLD_PROBES = _fold_probes()


def test_corpus_exercises_every_known_url_path_divergence() -> None:
    """The corpus stays meaningful: each divergence class is present."""

    divergent = {
        probe.label
        for probe in ATTACK_CORPUS
        if StarletteRequest(_scope(probe)).url.path != route_path(_scope(probe))
    }

    assert {"lf", "tab", "cr", "qmark", "hash", "absolute", "root"} <= divergent


@pytest.mark.parametrize("literal", sorted(DECISION_LITERALS))
def test_decision_path_folds_exactly_like_the_literal_route_matcher(
    literal: str,
) -> None:
    """``decision_path(scope) == L`` iff the router's matcher for L accepts it."""

    regex, _format, _convertors = compile_path(literal)
    mismatches = []
    for probe in FOLD_PROBES:
        scope = _scope(probe)
        routed = get_route_path(scope)
        if route_path(scope) != routed:
            mismatches.append((probe.id, "route_path is not the router path"))
        if (decision_path(scope) == literal) is not bool(regex.match(routed)):
            mismatches.append((probe.id, decision_path(scope)))

    assert mismatches == []


# --------------------------------------------------------------------------
# The real application, SPA off and on
# --------------------------------------------------------------------------


@pytest.fixture(params=["spa_off", "spa_on"])
def real_app(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> FastAPI:
    if request.param == "spa_on":
        configure_spa(monkeypatch, build_dist(tmp_path))
    else:
        configure_spa(monkeypatch, None)
    application = create_app()
    assert application.state.spa.enabled is (request.param == "spa_on")
    return application


def _spa_owned(route: Any) -> bool:
    endpoint = getattr(route, "endpoint", None)
    app = getattr(route, "app", None)
    return getattr(endpoint, "__qualname__", "") == (
        "_page_endpoint.<locals>.serve_spa"
    ) or (type(app).__name__ == "_AssetsApp")


_ALL_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")


def public_dispatch_targets(app: FastAPI) -> set[tuple[int, str]]:
    """(endpoint id, method) of every route an anonymous caller may reach."""

    targets: set[tuple[int, str]] = set()
    for route in effective_routes(app):
        path = route.path
        if not isinstance(path, str):
            continue
        if route.kind == "mount":
            literal = path + "/"
            if _spa_owned(route.route) or literal.startswith(
                AuthMiddleware.PUBLIC_PATH_PREFIXES
            ):
                targets |= {(id(route.endpoint), m) for m in _ALL_METHODS}
            continue
        for method in route.methods:
            if _spa_owned(route.route):
                public = True
            elif "{" in path:
                public = path.split("{", 1)[0].startswith(
                    AuthMiddleware.PUBLIC_PATH_PREFIXES
                )
            else:
                public = anonymous_public(method, path)
            if public:
                targets.add((id(route.endpoint), method))
    return targets


def _reached(outcome: Outcome) -> bool:
    """The router dispatched to a route and its method matched."""

    return outcome.endpoint is not None and outcome.status != 405


async def test_anonymous_corpus_auth_and_router_agree(
    real_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Auth judges the router path; no anonymous probe reaches a private route."""

    judged: list[str] = []
    original = AuthMiddleware._is_public_path

    def recording(self: AuthMiddleware, path: str) -> bool:
        judged.append(path)
        return original(self, path)

    monkeypatch.setattr(AuthMiddleware, "_is_public_path", recording)
    public_targets = public_dispatch_targets(real_app)
    violations: list[tuple[str, str]] = []
    for probe in ATTACK_CORPUS:
        judged.clear()
        outcome = await drive(real_app, probe)
        scope = _scope(probe)
        routed = decision_path(scope)
        # (A Mount legitimately rewrites root_path; nothing rewrites "path".)
        if outcome.scope["path"] != probe.path:
            violations.append((probe.id, "a middleware rewrote the routed path"))
        for path in judged:
            if path != routed:
                violations.append(
                    (probe.id, f"auth judged {path!r}, router {routed!r}")
                )
        method = "GET" if probe.method == "HEAD" else probe.method
        if _reached(outcome) and (
            (id(outcome.endpoint), probe.method) not in public_targets
            and (id(outcome.endpoint), method) not in public_targets
        ):
            violations.append(
                (probe.id, f"anonymous request reached {outcome.endpoint!r}")
            )
        if (
            _reached(outcome)
            and not anonymous_public(probe.method, routed)
            and not is_public_spa_path({**scope, "app": real_app})
        ):
            violations.append((probe.id, "dispatched although auth must deny it"))

    assert violations == []


def _endpoint_for(app: FastAPI, path: str, method: str) -> Any:
    for route in effective_routes(app):
        if route.path == path and (route.kind == "mount" or method in route.methods):
            return route.endpoint
    raise AssertionError(f"no route {method} {path}")


async def test_public_controls_are_still_served(real_app: FastAPI) -> None:
    controls = list(PUBLIC_CONTROLS)
    if real_app.state.spa.enabled:
        controls += SPA_PUBLIC_CONTROLS
    failures = []
    for probe, owner in controls:
        response_cache.invalidate()  # served by the route, not a cached copy
        outcome = await drive(real_app, probe)
        expected = _endpoint_for(real_app, owner, probe.method)
        if outcome.body == MISSING_CREDENTIALS_BODY or outcome.endpoint is not expected:
            failures.append((probe.id, outcome.status, outcome.error))

    assert failures == []


# (method, path) an anonymous caller must NOT reach: AuthMiddleware returns
# its own 401 and the router never dispatches.  Pinned to the observed
# response, not to any helper, so a middleware that loosens the prefix check
# to a substring (R15) or widens a per-method rule (R19/R20) fails here.
BLOCKED_BY_AUTH: list[tuple[str, str]] = [
    # a public-prefix string inside a protected parameter route
    ("GET", "/v1/sessions/metrics"),
    ("GET", "/v1/sessions/docs"),
    ("GET", "/v1/handoffs/docs"),
    ("GET", "/v1/knowledge/documents/metrics"),
    ("GET", "/v1/traces/redoc"),
    ("DELETE", "/v1/admin/clinics/redoc"),
    ("DELETE", "/v1/admin/clinics/metrics"),
    # the GET-only trust readbacks, reached by another method
    ("HEAD", "/v1/billing/producer-build-trust"),
    ("HEAD", "/v1/admin/billing/producer-build-trust"),
    # the POST-only demo-leads form, reached by GET
    ("GET", "/v1/demo-leads"),
    # HEAD is not OPTIONS: a protected GET route reached by HEAD stays closed
    ("HEAD", "/v1/admin/clinics"),
    ("HEAD", "/v1/billing/summary"),
]

# (method, path, must-not-be-405) an anonymous caller MAY reach: the one
# positive per public rule, so the rules are not narrowed to nothing.
REACHES_PUBLIC: list[tuple[str, str, bool]] = [
    ("GET", "/v1/billing/producer-build-trust", True),
    ("GET", "/v1/admin/billing/producer-build-trust", True),
    ("POST", "/v1/demo-leads", True),
    ("GET", "/health", True),
    ("GET", "/v1/webhook/meta", True),  # public via the /v1/webhook/ prefix
    ("OPTIONS", "/v1/admin/clinics", False),  # CORS preflight: 405, but reached
]


async def test_blocked_rules_pinned_by_observed_response(real_app: FastAPI) -> None:
    """Each blocked (method, path) gets AuthMiddleware's own 401 and is never
    dispatched -- observed directly, independent of the test oracle."""

    failures = []
    for method, path in BLOCKED_BY_AUTH:
        body = b"{}" if method in ("POST", "PUT", "PATCH") else b""
        outcome = await drive(real_app, Probe("blocked", method, path, body=body))
        if (outcome.status, outcome.body, outcome.endpoint) != (
            401,
            MISSING_CREDENTIALS_BODY,
            None,
        ):
            failures.append((method, path, outcome.status, outcome.endpoint))

    assert failures == []
    # The oracle agrees these are not public (so the corpus test's expected
    # set is faithful), by its own logic rather than by calling the middleware.
    assert not any(anonymous_public(method, path) for method, path in BLOCKED_BY_AUTH)


async def test_public_rules_pinned_by_observed_response(real_app: FastAPI) -> None:
    """Each public rule still lets its one positive through: AuthMiddleware
    does not 401, and the router dispatches it (so no rule is narrowed away)."""

    failures = []
    for method, path, not_405 in REACHES_PUBLIC:
        response_cache.invalidate()
        body = b"{}" if method == "POST" else b""
        outcome = await drive(real_app, Probe("public", method, path, body=body))
        reached = outcome.endpoint is not None
        passed_auth = outcome.body != MISSING_CREDENTIALS_BODY and outcome.status != 401
        if not (reached and passed_auth and (outcome.status != 405 or not not_405)):
            failures.append((method, path, outcome.status, outcome.endpoint))

    assert failures == []
    assert all(anonymous_public(method, path) for method, path, _ in REACHES_PUBLIC)


async def test_asaas_receiver_with_trailing_newline_is_authenticated_at_ingress(
    real_app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The router dispatches ``.../asaas\\n`` to the receiver, so ingress must
    authenticate it like the exact path (before: no token check, handler ran).
    """

    monkeypatch.setattr(settings, "asaas_webhook_token", SecretStr("expected"))

    outcome = await drive(
        real_app, Probe("lf", "POST", ASAAS_WEBHOOK_PATH + "\n", body=b"{}")
    )

    assert (outcome.status, outcome.body) == (401, b'{"detail":"unauthorized"}')
    assert outcome.endpoint is None and outcome.error is None


async def test_meta_webhook_with_trailing_newline_keeps_the_ingress_cap(
    real_app: FastAPI,
) -> None:
    """Anonymous ``POST /v1/webhook/meta%0a`` used to bypass the 1 MiB cap and
    the HMAC identity guard and reach the handler."""

    body = b'{"pad":"' + b"a" * MAX_INGRESS_BODY_BYTES + b'"}'

    outcome = await drive(
        real_app, Probe("lf", "POST", "/v1/webhook/meta\n", body=body)
    )

    assert outcome.status == 413
    assert outcome.endpoint is None and outcome.error is None


async def test_staging_issuer_with_trailing_newline_keeps_its_own_bound(
    real_app: FastAPI,
) -> None:
    """The issuer's 2 KiB bound and no-store wrapper apply to the routed
    ``%0a`` twin exactly as to the exact path (not the generic 1 MiB cap)."""

    body = b'{"pad":"' + b"a" * STAGING_REHEARSAL_TOKEN_BODY_BYTES + b'"}'

    outcome = await drive(
        real_app, Probe("lf", "POST", STAGING_REHEARSAL_TOKEN_PATH + "\n", body=body)
    )

    assert outcome.status == 413
    assert outcome.header("cache-control") == "no-store"
    assert outcome.endpoint is None


async def test_decoded_query_marker_is_not_a_public_path(real_app: FastAPI) -> None:
    """``/health%3Fx`` was public for auth (``url.path`` == ``/health``)."""

    await drive(real_app, Probe("warm", "GET", "/health"))  # primes the cache

    outcome = await drive(real_app, Probe("qmark", "GET", "/health?x", b"/health%3Fx"))

    assert (outcome.status, outcome.body) == (401, MISSING_CREDENTIALS_BODY)
    assert outcome.header("x-cache") is None


# --------------------------------------------------------------------------
# Each path-deciding middleware in isolation
# --------------------------------------------------------------------------


class _Tenant:
    """Stand-in for AuthMiddleware: publishes one authenticated tenant."""

    def __init__(self, app: Any, *, tenant: TenantContext) -> None:
        self.app = app
        self.tenant = tenant

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            scope.setdefault("state", {})["tenant"] = self.tenant
        await self.app(scope, receive, send)


def _counting_app(*paths: str, methods: tuple[str, ...] = ("GET",)) -> FastAPI:
    application = FastAPI()
    application.state.calls = 0

    async def endpoint(request: Request) -> dict[str, Any]:
        request.app.state.calls += 1
        return {"calls": request.app.state.calls}

    for path in paths:
        application.add_api_route(path, endpoint, methods=list(methods))
    return application


# ``/health`` is the stable cacheable example (10 s TTL): unlike
# ``/v1/policies`` it is not touched by the policies-tenant-authorization
# branch, so these routing tests do not depend on that path's cacheability.
async def test_cache_keys_on_the_router_path() -> None:
    """A path the router does not dispatch to the cacheable ``/health`` route
    never gets its cached response; one it does dispatch there gets its own
    entry, keyed by the exact router path."""

    application = _counting_app("/health")
    application.add_middleware(CachingMiddleware)

    warm = await drive(application, Probe("warm", "GET", "/health"))
    query = await drive(application, Probe("qmark", "GET", "/health?x"))
    tab = await drive(application, Probe("tab", "GET", "/hea\tlth"))
    newline = await drive(application, Probe("lf", "GET", "/health\n"))
    newline_again = await drive(application, Probe("lf", "GET", "/health\n"))

    assert (warm.status, warm.header("x-cache")) == (200, "MISS")
    assert (query.status, query.header("x-cache")) == (404, "BYPASS")
    assert (tab.status, tab.header("x-cache")) == (404, "BYPASS")
    # the %0a twin routes to the same literal route: cacheable (TTL on the
    # folded path), under its own key (the exact router path)
    assert (newline.status, newline.header("x-cache")) == (200, "MISS")
    assert newline.body == b'{"calls":2}'
    assert (newline_again.status, newline_again.header("x-cache")) == (200, "HIT")


async def test_cache_ttl_rule_strips_root_path_like_the_router() -> None:
    """Under ``root_path`` the router dispatches ``/prefix/health`` to
    ``/health``; the TTL rule judges that same path (before: the root-prefixed
    ``request.url.path`` matched no rule)."""

    application = _counting_app("/health")
    application.add_middleware(CachingMiddleware)
    probe = Probe("root", "GET", "/prefix/health", root_path="/prefix")

    first = await drive(application, probe)
    second = await drive(application, probe)

    assert (first.status, first.header("x-cache")) == (200, "MISS")
    assert (second.status, second.header("x-cache")) == (200, "HIT")


def test_cache_rule_for_v1_policies_follows_the_router_path() -> None:
    """The cache decision for ``/v1/policies`` is a pure function of the
    folded router path, so its ``%0a`` twin and its ``root_path`` spelling get
    the SAME decision (never-cache flag and TTL) as the exact path -- whatever
    that decision is.

    This is branch-independent: the policies-tenant-authorization branch makes
    ``/v1/policies`` never-cached, and when it does both sides move together,
    so the twin and the ``root_path`` spelling become never-cached too rather
    than diverging from the exact path.
    """

    middleware = CachingMiddleware(FastAPI())

    def cache_decision(path: str, root: str = "") -> tuple[bool, float]:
        folded = decision_path({"type": "http", "path": path, "root_path": root})
        # Exactly the two checks CachingMiddleware.dispatch makes on the path.
        never_cached = any(folded.startswith(p) for p in middleware.NEVER_CACHE)
        return never_cached, middleware._get_ttl_for_path(folded)

    exact = cache_decision("/v1/policies")
    assert cache_decision("/v1/policies\n") == exact  # the %0a twin
    assert cache_decision("/prefix/v1/policies", "/prefix") == exact  # root_path


async def test_cache_never_cache_rule_follows_the_router_path() -> None:
    application = _counting_app("/v1/events")
    application.add_middleware(CachingMiddleware)

    first = await drive(application, Probe("lf", "GET", "/v1/events\n"))
    second = await drive(application, Probe("lf", "GET", "/v1/events\n"))

    assert first.status == second.status == 200
    assert first.header("x-cache") is None and second.header("x-cache") is None
    assert second.body == b'{"calls":2}'


async def test_rate_limit_exemption_follows_the_router_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    tenant = TenantContext(tenant_id=uuid4(), api_key="k", rate_limit=1)
    application = _counting_app("/health", "/v1/x")
    application.add_middleware(RateLimitMiddleware)
    application.add_middleware(_Tenant, tenant=tenant)
    bucket = rate_limit_store.get_or_create_bucket(str(tenant.tenant_id), 1)
    while bucket.consume():
        pass

    exempt = await drive(application, Probe("exact", "GET", "/health"))
    exempt_newline = await drive(application, Probe("lf", "GET", "/health\n"))
    not_exempt = await drive(application, Probe("qmark", "GET", "/health?x"))
    tab = await drive(application, Probe("tab", "GET", "/hea\tlth"))

    assert exempt.status == exempt_newline.status == 200
    assert not_exempt.status == tab.status == 429


async def test_meta_rate_bucket_follows_the_router_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "rate_limit_enabled", True)
    tenant = TenantContext(tenant_id=uuid4(), api_key="k", rate_limit=1)
    application = _counting_app("/v1/webhook/meta", methods=("POST",))
    application.add_middleware(RateLimitMiddleware)
    application.add_middleware(_Tenant, tenant=tenant)
    tenant_bucket = rate_limit_store.get_or_create_bucket(str(tenant.tenant_id), 1)
    while tenant_bucket.consume():
        pass

    routed = await drive(application, Probe("lf", "POST", "/v1/webhook/meta\n"))
    other = await drive(application, Probe("qmark", "POST", "/v1/webhook/meta?x"))

    # the meta bucket is still full; the tenant bucket is empty
    assert routed.status == 200
    assert other.status == 429


async def test_tenant_identity_guard_follows_the_router_path() -> None:
    """``POST /v1/events%0a`` reaches the events route; the identity assertion
    must still be enforced (before: skipped)."""

    tenant_id = uuid4()
    application = _counting_app("/v1/events", methods=("POST",))
    application.add_middleware(TenantIdentityMiddleware)
    application.add_middleware(
        _Tenant, tenant=TenantContext(tenant_id=tenant_id, api_key="k")
    )
    body = ('{"tenant_id":"%s"}' % uuid4()).encode()

    outcome = await drive(application, Probe("lf", "POST", "/v1/events\n", body=body))

    assert outcome.status == 403
    assert application.state.calls == 0


async def test_media_upload_cap_follows_the_router_path() -> None:
    application = _counting_app("/v1/media-assets", methods=("POST",))
    application.add_middleware(
        MediaUploadLimitsMiddleware,
        limiters=MediaConcurrencyLimiters(uploads=1, downloads=1),
        upload_path="/v1/media-assets",
        request_max_bytes=4,
    )

    outcome = await drive(
        application, Probe("lf", "POST", "/v1/media-assets\n", body=b"12345")
    )

    assert outcome.status == 413
    assert application.state.calls == 0


def _auth_app(path: str, method: str) -> FastAPI:
    application = _counting_app(path, methods=(method,))
    application.add_middleware(AuthMiddleware)
    return application


def _super_admin_jwt() -> tuple[bytes, bytes]:
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(uuid4()),
            "role": "super_admin",
            "exp": 4102444800,
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return (b"authorization", f"Bearer {token}".encode("ascii"))


def _rehearsal_jwt() -> tuple[bytes, bytes]:
    """A well-formed staging rehearsal-purpose token (exact claim set)."""

    now = int(time.time())
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(uuid4()),
            "role": "super_admin",
            "email": "rehearsal@example.com",
            "iat": now,
            "exp": now + 300,
            "jti": str(uuid4()),
            "token_use": STAGING_REHEARSAL_TOKEN_USE,
            "token_environment": STAGING_REHEARSAL_TOKEN_ENVIRONMENT,
            "scope": STAGING_REHEARSAL_TOKEN_SCOPE,
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return (b"authorization", f"Bearer {token}".encode("ascii"))


def _routed_twins(target: str) -> list[Probe]:
    """Requests the router dispatches to ``target`` although
    ``request.url.path`` differs from it (or is the same for ``%0a``)."""

    return [
        Probe("lf", "GET", target + "\n"),
        Probe("root", "GET", "/prefix" + target, root_path="/prefix"),
    ]


@pytest.mark.parametrize("method, target", sorted(STAGING_REHEARSAL_TARGETS))
async def test_rehearsal_rule_follows_the_router_path(
    monkeypatch: pytest.MonkeyPatch, method: str, target: str
) -> None:
    """Where the router dispatches to a target, an ordinary JWT is refused
    and a rehearsal token is accepted; where it does not (decoded ``?``),
    neither rule applies."""

    monkeypatch.setattr(settings, "environment", "staging")
    application = _auth_app(target, method)
    results = []
    for twin in _routed_twins(target):
        probe = Probe(twin.label, method, twin.path, root_path=twin.root_path)
        ordinary = await drive(application, probe, headers=[_super_admin_jwt()])
        rehearsal = await drive(application, probe, headers=[_rehearsal_jwt()])
        results.append((probe.id, ordinary.status, ordinary.body, rehearsal.status))
    not_routed = Probe("qmark", method, target + "?x")
    ordinary = await drive(application, not_routed, headers=[_super_admin_jwt()])
    rehearsal = await drive(application, not_routed, headers=[_rehearsal_jwt()])

    refused = b'{"error":"unauthorized"}'
    assert [(status, body, ok) for _id, status, body, ok in results] == [
        (401, refused, 200)
    ] * len(results), results
    assert application.state.calls == len(results)  # the rehearsal requests
    # not a target for auth either: ordinary JWT passes (router 404), the
    # purpose token is refused outside its targets
    assert ordinary.status == 404
    assert (rehearsal.status, rehearsal.body) == (401, b'{"error":"unauthorized"}')


@pytest.mark.parametrize(
    "probe, status",
    [
        (Probe("lf", "POST", STAGING_REHEARSAL_TOKEN_PATH + "\n"), 404),
        (
            Probe(
                "root",
                "POST",
                "/prefix" + STAGING_REHEARSAL_TOKEN_PATH,
                root_path="/prefix",
            ),
            404,
        ),
        (Probe("qmark", "POST", STAGING_REHEARSAL_TOKEN_PATH + "?x"), 401),
    ],
    ids=["lf", "root", "qmark"],
)
async def test_staging_issuer_path_rule_follows_the_router_path(
    probe: Probe, status: int
) -> None:
    """Outside staging the issuer path is refused (404, no-store) before any
    credential check; a path the router does not dispatch there is ordinary
    (anonymous: 401)."""

    application = _auth_app(STAGING_REHEARSAL_TOKEN_PATH, "POST")

    outcome = await drive(application, probe)

    assert outcome.status == status
    if status == 404:
        assert outcome.header("cache-control") == "no-store"
    assert application.state.calls == 0


@pytest.mark.parametrize("path", sorted(PUBLIC_TRUST_READBACK_PATHS))
async def test_public_trust_readback_rule_follows_the_router_path(path: str) -> None:
    application = _auth_app(path, "GET")

    routed = await drive(application, Probe("lf", "GET", path + "\n"))
    not_routed = await drive(application, Probe("qmark", "GET", path + "?x"))

    assert routed.status == 200
    assert routed.header("cache-control") == "no-store"
    assert (not_routed.status, not_routed.body) == (401, MISSING_CREDENTIALS_BODY)


async def test_public_demo_lead_rule_follows_the_router_path() -> None:
    application = _auth_app(PUBLIC_DEMO_LEADS_PATH, "POST")

    routed = await drive(
        application, Probe("lf", "POST", PUBLIC_DEMO_LEADS_PATH + "\n")
    )
    not_routed = await drive(
        application, Probe("qmark", "POST", PUBLIC_DEMO_LEADS_PATH + "?x")
    )

    assert routed.status == 200
    assert (not_routed.status, not_routed.body) == (401, MISSING_CREDENTIALS_BODY)


@pytest.mark.parametrize(
    "root_path, path, status",
    [
        ("/prefix", "/prefix/health", 200),
        ("/prefix", "/prefix/v1/x", 401),
        ("/prefix", "/health", 200),
        ("/prefix", "/prefixhealth", 401),
    ],
)
async def test_auth_strips_root_path_like_the_router(
    root_path: str, path: str, status: int
) -> None:
    application = _counting_app("/health", "/v1/x")
    application.add_middleware(AuthMiddleware)

    outcome = await drive(application, Probe("root", "GET", path, root_path=root_path))

    assert outcome.status == status


@pytest.mark.parametrize("probe", ATTACK_CORPUS, ids=[p.id for p in ATTACK_CORPUS])
def test_spa_hook_judges_the_same_exact_router_path(probe: Probe) -> None:
    """The SPA decision keeps the exact (unfolded) router path because its
    endpoint re-checks that same string; it comes from the shared helper."""

    seen: list[str] = []

    class Recording(SpaRegistry):
        def is_public(self, path: str) -> bool:
            seen.append(path)
            return False

    application = FastAPI()
    application.state.spa = Recording(index=None)

    assert is_public_spa_path({**_scope(probe), "app": application}) is False
    assert seen == [route_path(_scope(probe))]
