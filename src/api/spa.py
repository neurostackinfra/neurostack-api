"""Serve the built frontend (single-page app) from the API with exact routes.

Security contract (wish asaas-payment-gateway, Group 6c):

* Only an exact set of paths, computed once at startup, is served: the client
  routes of ``frontend/artifacts/neurostack/src/App.tsx`` (plus trailing-slash
  twins), one dynamic route ``/traces/<id>`` matched with ``re.fullmatch``,
  the regular files of the flat ``assets/`` build directory and the top-level
  build files.  ``index.html`` is held in memory and never served at its own
  path.  Nothing is ever path-joined from a request: every lookup is an exact
  key lookup in the precomputed registry.
* ``SpaRegistry.is_public`` is the single decision used both by the auth
  middleware and by every SPA endpoint, and both evaluate it on the path the
  Starlette router matches (``get_route_path(scope)``, the function FastAPI's
  router itself uses), never on ``request.url.path`` (which is rebuilt from
  the Host header and truncated at a decoded ``?``/``#``).  An endpoint serves
  a request if and only if ``is_public`` holds for it, so the auth bypass and
  what is served cannot diverge.
* Startup fails closed: an absent, empty, non-UTF-8 or index-less build, an
  unreadable ``assets/`` directory (only its absence is tolerated, and only
  when ``index.html`` references no ``/assets/`` file), an ``index.html``
  that references an ``/assets/`` file the registry does not hold,
  ``SPA_ENABLED`` false, any registered path under a reserved API prefix, or
  any collision with an existing route registers nothing.  Collisions are
  exact probes for the exact paths and, for ``/traces/<id>``, a structural
  check of every effective route in the table, included routers resolved to
  the route FastAPI dispatches (see ``_effective_route`` and
  ``_shadows_trace_detail``); a route class with its own ``matches`` cannot
  be analyzed and always counts as a collision.
* SPA responses alone carry the cache and security headers below; ``/v1``,
  ``/docs`` and ``/redoc`` responses are untouched.
"""

from __future__ import annotations

import errno
import hashlib
import os
import re
import stat
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.routing import (
    APIRoute,
    APIWebSocketRoute,
    RouteContext,
    iter_route_contexts,
)
from starlette._utils import get_route_path
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Host, Match, Mount, Route, WebSocketRoute
from starlette.types import Receive, Scope, Send
from starlette.websockets import WebSocketClose

from src.config.logging import get_logger
from src.config.settings import settings

logger = get_logger(__name__)

# Client routes of frontend/artifacts/neurostack/src/App.tsx, in source order,
# without "/privacidade/:clinicId" (the API serves that page itself as
# server-rendered HTML).  tests/api/test_spa_static.py re-derives this list
# from App.tsx and, once present, compares the shared fixture
# tests/fixtures/spa_client_routes.json with it.
SPA_CLIENT_ROUTES: Final[tuple[str, ...]] = (
    "/",
    "/dashboard",
    "/traces",
    "/traces/:id",
    "/config",
    "/agents",
    "/crm",
    "/knowledge-base",
    "/master-admin",
    "/rlhf-audit",
    "/builder",
    "/onboarding",
    "/seguranca",
    "/slm-cortex",
    "/consultoria",
    "/midias",
    "/billing",
    "/voice-ai",
    "/suporte",
    "/portal-parceiros",
    "/handoff-queue",
    "/inactive-queue",
)

# The only dynamic client route.  ``fullmatch`` (never ``match`` with ``$``,
# which also accepts a trailing newline) and an explicit ASCII class.
TRACE_DETAIL_ROUTE: Final = "/traces/:id"
TRACE_DETAIL_PATTERN: Final = re.compile(r"/traces/[A-Za-z0-9_-]{1,128}")
_TRACE_DETAIL_STARLETTE_PATH: Final = "/traces/{trace_id}"
_TRACE_DETAIL_PREFIX: Final = TRACE_DETAIL_ROUTE.split(":", 1)[0]
# Backstop only: one id also goes through every route's own ``matches``.
_TRACE_DETAIL_SAMPLE: Final = _TRACE_DETAIL_PREFIX + "0"
# The structural check reads ``path``; it is only sound for route classes
# whose matching is the stock, path-derived one.  Any other ``matches`` (a
# subclass overriding it) is unanalyzable and counts as a collision.
_STOCK_MATCHES: Final = frozenset(
    cls.matches
    for cls in (Route, Mount, Host, WebSocketRoute, APIRoute, APIWebSocketRoute)
)

# No registered path may equal or start with any of these.  The check is a
# plain ``startswith`` (so "/healthz" or "/docs.png" are refused too), plus
# equality with the bare form of slash-terminated prefixes ("/api").
RESERVED_PREFIXES: Final[tuple[str, ...]] = (
    "/v1",
    "/api/",
    "/docs",
    "/redoc",
    "/openapi.json",
    "/metrics",
    "/health",
    "/ready",
    "/live",
    "/privacidade/",
)

ASSETS_PREFIX: Final = "/assets"
ASSET_NAME_PATTERN: Final = re.compile(r"[A-Za-z0-9._-]+")
INDEX_FILE: Final = "index.html"
_ASSETS_DIR: Final = "assets"

# Media types by exact (lower-case) extension.  Deterministic instead of the
# host's mimetypes database, so nosniff can never meet a wrong type.  No HTML
# besides the in-memory index and no source maps are ever served.
_MEDIA_TYPES: Final[Mapping[str, str]] = {
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".webmanifest": "application/manifest+json",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".avif": "image/avif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".otf": "font/otf",
    ".wasm": "application/wasm",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}
_HTML_MEDIA_TYPE: Final = "text/html; charset=utf-8"

INDEX_CACHE_CONTROL: Final = "no-cache"
ASSET_CACHE_CONTROL: Final = "public, max-age=31536000, immutable"
TOP_LEVEL_CACHE_CONTROL: Final = "public, max-age=86400"

# Enforced on SPA responses only.  Checked against a real build of
# frontend/artifacts/neurostack (vite 7.3.2, `BASE_PATH=/`, 2026-10-18):
# index.html has no inline script or handler, one module script and one
# stylesheet from /assets, and the Google Fonts stylesheet; the CSS has a
# single @import (Google Fonts) and no url(); every fetch() is same-origin.
CONTENT_SECURITY_POLICY: Final = "; ".join(
    (
        # Fallback for the fetch directives not listed (frame, worker,
        # manifest, child): this origin only.
        "default-src 'self'",
        # One external module script (/assets/index-*.js); no inline script
        # and no eval at runtime (lodash's Function("return this") fallback
        # is short-circuited by `self` in browsers).
        "script-src 'self'",
        # index.html <link> and the CSS @import load the Google Fonts
        # stylesheet; Radix ScrollArea/Select render inline <style> elements
        # and react-remove-scroll injects <style> tags, hence 'unsafe-inline'
        # (owner accepted).
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
        # The Google Fonts stylesheet serves Inter from fonts.gstatic.com.
        # data: comes from the approved design; the current bundle has none.
        "font-src 'self' https://fonts.gstatic.com data:",
        # Logo and favicon come from this origin.  data:, blob: and https:
        # come from the approved design; the current bundle uses none.
        "img-src 'self' data: blob: https:",
        # Approved design; the current bundle has no audio/video element.
        "media-src 'self' blob:",
        # Every fetch() targets /v1 or /api/public on this origin; downloads
        # are blob: <a download> navigations, which CSP does not govern.
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        # No cross-origin form posts.  The hosted checkout redirect is a
        # top-level window.location.assign, which CSP does not govern.
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)

SECURITY_HEADERS: Final[Mapping[str, str]] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
}

_SERVED_METHODS: Final = frozenset({"GET", "HEAD"})
_ROUTE_METHODS: Final = ["GET", "HEAD"]


@dataclass(frozen=True)
class SpaFile:
    """One immutable in-memory build file and its response metadata."""

    body: bytes
    media_type: str
    cache_control: str
    etag: str


class SpaUnavailable(Exception):
    """The build cannot be served; nothing is registered."""


class SpaRegistry:
    """The exact, startup-computed set of public SPA paths.

    ``lookup`` and ``is_public`` are exact: a ``frozenset``/dict membership
    test, or ``TRACE_DETAIL_PATTERN.fullmatch``.  Callers must pass the path
    the router matches (see ``router_path``).
    """

    def __init__(
        self,
        *,
        index: SpaFile | None,
        pages: Iterable[str] = (),
        files: Mapping[str, SpaFile] | None = None,
    ) -> None:
        self._index = index
        self._pages: frozenset[str] = frozenset(pages) if index else frozenset()
        self._files: dict[str, SpaFile] = dict(files or {}) if index else {}

    @classmethod
    def disabled(cls) -> SpaRegistry:
        return cls(index=None)

    @property
    def enabled(self) -> bool:
        return self._index is not None

    @property
    def pages(self) -> frozenset[str]:
        """Exact client-route paths (including trailing-slash twins)."""
        return self._pages

    @property
    def files(self) -> Mapping[str, SpaFile]:
        """Exact build-file paths (``/assets/<name>`` and ``/<name>``)."""
        return dict(self._files)

    def lookup(self, path: str) -> SpaFile | None:
        """Return what is served for exactly ``path``, or ``None``."""
        if self._index is None:
            return None
        file = self._files.get(path)
        if file is not None:
            return file
        if path in self._pages or TRACE_DETAIL_PATTERN.fullmatch(path):
            return self._index
        return None

    def is_public(self, path: str) -> bool:
        """True only for an exact registered path (the auth bypass)."""
        return self.lookup(path) is not None


def router_path(scope: Scope) -> str:
    """The exact path string Starlette's router matches for ``scope``."""
    return get_route_path(scope)


def is_public_spa_path(scope: Scope) -> bool:
    """Auth hook: is the router path of ``scope`` an exact public SPA path?

    The caller restricts the method to GET/HEAD.  An application without an
    installed registry (``app.state.spa``) has no public SPA path.
    """
    registry = getattr(getattr(scope.get("app"), "state", None), "spa", None)
    if not isinstance(registry, SpaRegistry):
        return False
    return registry.is_public(router_path(scope))


def _etag(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest() + '"'


def _not_modified(if_none_match: str | None, etag: str) -> bool:
    """RFC 9110 If-None-Match with weak comparison."""
    if not if_none_match:
        return False
    for candidate in if_none_match.split(","):
        tag = candidate.strip()
        if tag == "*":
            return True
        if tag.startswith("W/"):
            tag = tag[2:]
        if tag == etag:
            return True
    return False


def _file_response(file: SpaFile, method: str, if_none_match: str | None) -> Response:
    headers = {
        "Cache-Control": file.cache_control,
        "ETag": file.etag,
        **SECURITY_HEADERS,
    }
    if _not_modified(if_none_match, file.etag):
        return Response(status_code=304, headers=headers)
    if method == "HEAD":
        headers["Content-Length"] = str(len(file.body))
        return Response(content=b"", media_type=file.media_type, headers=headers)
    return Response(content=file.body, media_type=file.media_type, headers=headers)


class _AssetsApp:
    """ASGI app behind ``Mount("/assets")``: exact registry lookup only."""

    def __init__(self, registry: SpaRegistry) -> None:
        self._registry = registry

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await WebSocketClose()(scope, receive, send)
            return
        # Mount sets root_path = parent root_path + the matched "/assets"; the
        # parent root_path rebuilds the path the top-level router (and the
        # auth hook) matched.  Any other shape (e.g. Mount's "$ before a
        # trailing newline" match) is not an exact asset path.
        root_path = scope.get("root_path", "")
        if not root_path.endswith(ASSETS_PREFIX):
            raise HTTPException(status_code=404)
        parent_root = root_path[: -len(ASSETS_PREFIX)]
        path = get_route_path({"path": scope["path"], "root_path": parent_root})
        file = self._registry.lookup(path)
        if file is None or not path.startswith(ASSETS_PREFIX + "/"):
            raise HTTPException(status_code=404)
        method = scope["method"]
        if method not in _SERVED_METHODS:
            raise HTTPException(status_code=405, headers={"Allow": "GET, HEAD"})
        request = Request(scope, receive)
        response = _file_response(file, method, request.headers.get("if-none-match"))
        await response(scope, receive, send)


def _page_endpoint(
    registry: SpaRegistry,
) -> Callable[[Request], Awaitable[Response]]:
    async def serve_spa(request: Request) -> Response:
        file = registry.lookup(router_path(request.scope))
        if file is None:
            raise HTTPException(status_code=404)
        return _file_response(
            file, request.method, request.headers.get("if-none-match")
        )

    return serve_spa


def _open_flags() -> int:
    return os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _read_regular_file(name: str, dir_fd: int) -> bytes | None:
    """Read ``name`` in ``dir_fd`` only if it is a regular file (no symlink)."""
    try:
        fd = os.open(name, _open_flags(), dir_fd=dir_fd)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        chunks: list[bytes] = []
        while chunk := os.read(fd, 1 << 20):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _scan_files(
    dir_fd: int, ignored: frozenset[str] = frozenset()
) -> tuple[list[str], list[str]]:
    """Names of servable regular files in ``dir_fd`` and of skipped entries.

    ``ignored`` names (``index.html`` and ``assets`` at the top level) are
    handled by the caller; every other non-servable entry, directories
    included, is reported as skipped.
    """
    servable: list[str] = []
    skipped: list[str] = []
    with os.scandir(dir_fd) as entries:
        for entry in entries:
            name = entry.name
            if name in ignored:
                continue
            if (
                entry.is_dir(follow_symlinks=False)
                or entry.is_symlink()
                or not entry.is_file(follow_symlinks=False)
                or name.startswith(".")
                or not ASSET_NAME_PATTERN.fullmatch(name)
                or os.path.splitext(name)[1] not in _MEDIA_TYPES
            ):
                skipped.append(name)
                continue
            servable.append(name)
    return sorted(servable), sorted(skipped)


def _load_files(
    dir_fd: int,
    prefix: str,
    cache_control: str,
    ignored: frozenset[str] = frozenset(),
) -> tuple[dict[str, SpaFile], list[str]]:
    names, skipped = _scan_files(dir_fd, ignored)
    files: dict[str, SpaFile] = {}
    for name in names:
        body = _read_regular_file(name, dir_fd)
        if body is None:
            skipped.append(name)
            continue
        files[f"{prefix}/{name}"] = SpaFile(
            body=body,
            media_type=_MEDIA_TYPES[os.path.splitext(name)[1]],
            cache_control=cache_control,
            etag=_etag(body),
        )
    return files, sorted(skipped)


def _client_pages(routes: Iterable[str]) -> list[str]:
    """Static client routes plus trailing-slash twins.

    A twin is withheld when it opens a dynamic route's namespace: "/traces/"
    stays private so that only "/traces/<1-128 id chars>" is public there.
    """
    routes = tuple(routes)
    for route in routes:
        if ":" in route and route != TRACE_DETAIL_ROUTE:
            raise SpaUnavailable(f"unsupported dynamic client route {route!r}")
    dynamic_prefixes = tuple(route.split(":", 1)[0] for route in routes if ":" in route)
    pages: list[str] = []
    for route in routes:
        if ":" in route:
            continue
        pages.append(route)
        twin = route.rstrip("/") + "/"
        if twin != route and not any(p.startswith(twin) for p in dynamic_prefixes):
            pages.append(twin)
    return pages


class _AssetReferences(HTMLParser):
    """Every ``src``/``href`` in ``index.html`` that names an ``/assets/`` file."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.paths: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name in ("src", "href") and value:
                path = value.split("#", 1)[0].split("?", 1)[0]
                if path.startswith(ASSETS_PREFIX + "/"):
                    self.paths.add(path)


def _asset_references(index_body: bytes) -> frozenset[str]:
    # Served as "text/html; charset=utf-8": a Vite build is always UTF-8, so
    # anything else is a broken build, not something to serve.
    try:
        text = index_body.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SpaUnavailable(f"index.html is not valid UTF-8 ({error})") from error
    parser = _AssetReferences()
    parser.feed(text)
    parser.close()
    return frozenset(parser.paths)


def _load_assets(
    root_fd: int, referenced: frozenset[str]
) -> tuple[dict[str, SpaFile], list[str]]:
    """Load ``assets/``; only a missing, unreferenced directory is tolerated."""
    try:
        assets_fd = os.open(
            _ASSETS_DIR,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root_fd,
        )
    except OSError as error:
        if error.errno == errno.ENOENT and not referenced:
            return {}, []
        raise SpaUnavailable(
            f"assets directory unavailable ({error.strerror})"
        ) from error
    try:
        return _load_files(assets_fd, ASSETS_PREFIX, ASSET_CACHE_CONTROL)
    finally:
        os.close(assets_fd)


def load_build(dist_dir: str) -> SpaRegistry:
    """Build the registry from ``dist_dir``; raise ``SpaUnavailable``."""
    if not dist_dir.strip():
        raise SpaUnavailable("spa_dist_dir is empty")
    root = Path(dist_dir)
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as error:
        raise SpaUnavailable(
            f"build directory unavailable ({error.strerror})"
        ) from error
    try:
        index_body = _read_regular_file(INDEX_FILE, root_fd)
        if not index_body:
            raise SpaUnavailable("index.html missing, empty or not a regular file")
        index = SpaFile(
            body=index_body,
            media_type=_HTML_MEDIA_TYPE,
            cache_control=INDEX_CACHE_CONTROL,
            etag=_etag(index_body),
        )
        referenced = _asset_references(index_body)
        files, skipped = _load_files(
            root_fd,
            "",
            TOP_LEVEL_CACHE_CONTROL,
            ignored=frozenset({INDEX_FILE, _ASSETS_DIR}),
        )
        assets, skipped_assets = _load_assets(root_fd, referenced)
        files.update(assets)
        skipped.extend(f"{_ASSETS_DIR}/{name}" for name in skipped_assets)
    finally:
        os.close(root_fd)
    missing = sorted(referenced - files.keys())
    if missing:
        raise SpaUnavailable(f"index.html references missing build files: {missing}")
    if skipped:
        logger.warning("spa_build_entries_skipped", entries=sorted(skipped))
    pages = _client_pages(SPA_CLIENT_ROUTES)
    duplicates = sorted(set(pages) & set(files))
    if duplicates:
        raise SpaUnavailable(f"build files shadow client routes: {duplicates}")
    return SpaRegistry(index=index, pages=pages, files=files)


def _is_reserved(path: str) -> bool:
    prefixes = (*RESERVED_PREFIXES, settings.api_prefix)
    return any(
        path.startswith(prefix) or path == prefix.rstrip("/") for prefix in prefixes
    )


def _effective_route(context: RouteContext) -> tuple[object, BaseRoute]:
    """The routed path and route type FastAPI dispatches for ``context``.

    An included non-API route (Starlette ``Route``, ``Mount``, ``Host``,
    websocket) is dispatched through a re-built, prefixed Starlette route
    (``starlette_route``); its context's own ``path`` is a placeholder
    ``""``.  Included ``APIRoute`` contexts and top-level routes carry their
    effective path on the context itself.
    """
    starlette_route = getattr(context, "starlette_route", None)
    if isinstance(starlette_route, BaseRoute):
        return getattr(starlette_route, "path", None), starlette_route
    return context.path, context.original_route


def _shadows_trace_detail(path: object, is_mount: bool) -> bool:
    """Could a route with this (effective) path match some ``/traces/<id>``?

    A sound over-approximation over the route table, independent of sample
    ids: a route without a string path (e.g. ``Host``), or a non-mount with
    an empty one, is unanalyzable and counts; a templated path counts when its literal text before the first
    ``{`` is a prefix of ``/traces/`` (``/{x}``, ``/{x}/stats``, ``/tr{p}``)
    or lies under it; a literal path counts when it lies under ``/traces/``,
    and a mount also when it encloses ``/traces/`` (``""``, ``/traces``).
    """
    if not isinstance(path, str) or (path == "" and not is_mount):
        # No path, or an empty one no plain route can have: unanalyzable.
        return True
    literal = path.split("{", 1)[0]
    if literal.startswith(_TRACE_DETAIL_PREFIX):
        return True
    if "{" in path:
        return _TRACE_DETAIL_PREFIX.startswith(literal)
    return is_mount and _TRACE_DETAIL_PREFIX.startswith(path + "/")


def find_conflicts(app: FastAPI, registry: SpaRegistry) -> list[str]:
    """Reserved-prefix hits and collisions with routes already registered."""
    probes = sorted({*registry.pages, *registry.files})
    conflicts = [f"reserved:{path}" for path in probes if _is_reserved(path)]
    for context in iter_route_contexts(app.router.routes):
        path, route = _effective_route(context)
        custom = [
            candidate
            for candidate in (context.original_route, route)
            if type(candidate).matches not in _STOCK_MATCHES
        ]
        if custom:
            name = type(custom[0]).__name__
            conflicts.append(f"collision:{TRACE_DETAIL_ROUTE} -> custom matches {name}")
        elif _shadows_trace_detail(path, isinstance(route, Mount)):
            owner = path if isinstance(path, str) else None
            owner = owner or type(route).__name__
            conflicts.append(f"collision:{TRACE_DETAIL_ROUTE} -> {owner}")
    for prefix in (ASSETS_PREFIX + "/", _TRACE_DETAIL_PREFIX):
        if _is_reserved(prefix):
            conflicts.append(f"reserved:{prefix}")
    for path in (*probes, _TRACE_DETAIL_SAMPLE):
        for method in _ROUTE_METHODS:
            scope: Scope = {
                "type": "http",
                "method": method,
                "path": path,
                "root_path": "",
                "headers": [],
                "query_string": b"",
            }
            for route in list(app.router.routes):
                match, _ = route.matches(scope)
                if match is not Match.NONE:
                    owner = getattr(route, "path", None) or type(route).__name__
                    conflicts.append(f"collision:{method} {path} -> {owner}")
    return sorted(set(conflicts))


def install_spa(app: FastAPI) -> SpaRegistry:
    """Register the exact SPA routes; must run after every API route."""
    disabled = SpaRegistry.disabled()
    app.state.spa = disabled
    if not settings.spa_enabled:
        logger.warning("spa_not_installed", reason="SPA_ENABLED is false")
        return disabled
    try:
        registry = load_build(settings.spa_dist_dir)
    except (SpaUnavailable, OSError) as error:
        logger.warning(
            "spa_not_installed",
            reason=str(error),
            spa_dist_dir=settings.spa_dist_dir,
        )
        return disabled
    conflicts = find_conflicts(app, registry)
    if conflicts:
        logger.error(
            "spa_not_installed",
            reason="registered path reserved or colliding",
            conflicts=conflicts,
        )
        return disabled

    serve = _page_endpoint(registry)
    exact_paths = sorted(registry.pages) + sorted(
        path for path in registry.files if not path.startswith(ASSETS_PREFIX + "/")
    )
    for path in exact_paths:
        app.add_route(path, serve, methods=_ROUTE_METHODS, include_in_schema=False)
    app.add_route(
        _TRACE_DETAIL_STARLETTE_PATH,
        serve,
        methods=_ROUTE_METHODS,
        include_in_schema=False,
    )
    if any(path.startswith(ASSETS_PREFIX + "/") for path in registry.files):
        app.mount(ASSETS_PREFIX, _AssetsApp(registry))
    app.state.spa = registry
    logger.info(
        "spa_installed",
        pages=len(registry.pages),
        files=len(registry.files),
        spa_dist_dir=settings.spa_dist_dir,
    )
    return registry
