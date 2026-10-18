"""Shared helpers for the SPA serving tests (not collected by pytest)."""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import jwt
import pytest
from fastapi import FastAPI
from starlette.types import Message, Scope

from src.config.settings import settings

INDEX_HTML = (
    b"<!DOCTYPE html><html><head>"
    b'<script type="module" crossorigin src="/assets/index-AbC123.js"></script>'
    b'<link rel="stylesheet" crossorigin href="/assets/index-DeF456.css">'
    b'</head><body><div id="root"></div></body></html>'
)
ASSET_FILES: dict[str, bytes] = {
    "index-AbC123.js": b"export const spa = 1;\n",
    "index-DeF456.css": b"body{margin:0}\n",
    "logo_png_1775411054369-mcl0XYek.png": b"\x89PNG\r\n\x1a\nlogo",
}
TOP_LEVEL_FILES: dict[str, bytes] = {
    "favicon.png": b"\x89PNG\r\n\x1a\nfavicon",
    "favicon.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>",
    "opengraph.jpg": b"\xff\xd8\xffopengraph",
}
EXPECTED_MEDIA_TYPES = {
    "/assets/index-AbC123.js": "text/javascript; charset=utf-8",
    "/assets/index-DeF456.css": "text/css; charset=utf-8",
    "/assets/logo_png_1775411054369-mcl0XYek.png": "image/png",
    "/favicon.png": "image/png",
    "/favicon.svg": "image/svg+xml",
    "/opengraph.jpg": "image/jpeg",
}
# Entries a build directory may contain that must never become public.
HOSTILE_ASSET_PATHS = (
    "/assets/linked.js",  # symlink to a file outside the build
    "/assets/.hidden.js",  # dotfile
    "/assets/bad name.js",  # outside [A-Za-z0-9._-]
    "/assets/üni.js",  # non-ASCII name
    "/assets/index-AbC123.js.map",  # source map
    "/assets/page.html",  # other HTML document
    "/assets/nested/deep.js",  # nested directory
    "/assets/nested",
    "/assets/pipe.js",  # FIFO (must neither hang nor be served)
    "/assets/LOGO.PNG",  # extension outside the exact map
)
HOSTILE_TOP_LEVEL_PATHS = (
    "/.env",
    "/notes.md",
    "/linked.png",
    "/other/file.js",
    "/index.html",
)
# The approved header values, written out literally so that weakening the
# constants in src/api/spa.py fails the suite instead of moving both sides.
APPROVED_CSP = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com data:; "
    "img-src 'self' data: blob: https:; media-src 'self' blob:; "
    "connect-src 'self'; object-src 'none'; base-uri 'self'; "
    "form-action 'self'; frame-ancestors 'none'"
)
# index.html: always revalidated (ETag/304), never reused unchecked.
APPROVED_INDEX_CACHE = "no-cache"
# Content-hashed /assets/*: long-lived and immutable.
APPROVED_ASSET_CACHE = "public, max-age=31536000, immutable"
# Unhashed top-level files: a day, never immutable.
APPROVED_TOP_LEVEL_CACHE = "public, max-age=86400"
APPROVED_SECURITY_HEADERS = {
    "content-security-policy": APPROVED_CSP,
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "x-frame-options": "DENY",
}
# Per-request id, timing and the wall-clock rate-limit window end.
VOLATILE_HEADERS = frozenset({"x-request-id", "x-response-time", "x-ratelimit-reset"})


def build_dist(root: Path, *, hostile: bool = True) -> Path:
    """Write a synthetic Vite ``dist/public`` tree under ``root``."""

    dist = root / "dist" / "public"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_bytes(INDEX_HTML)
    for name, body in ASSET_FILES.items():
        (assets / name).write_bytes(body)
    for name, body in TOP_LEVEL_FILES.items():
        (dist / name).write_bytes(body)
    if not hostile:
        return dist
    outside = root / "outside-secret.js"
    outside.write_bytes(b"secret")
    (assets / "linked.js").symlink_to(outside)
    (assets / ".hidden.js").write_bytes(b"hidden")
    (assets / "bad name.js").write_bytes(b"bad")
    (assets / "üni.js").write_bytes(b"unicode")
    (assets / "index-AbC123.js.map").write_bytes(b"{}")
    (assets / "page.html").write_bytes(b"<html></html>")
    (assets / "nested").mkdir()
    (assets / "nested" / "deep.js").write_bytes(b"deep")
    (assets / "LOGO.PNG").write_bytes(b"upper")
    if hasattr(os, "mkfifo"):
        os.mkfifo(assets / "pipe.js")
    (dist / ".env").write_bytes(b"SECRET=1")
    (dist / "notes.md").write_bytes(b"# notes")
    (dist / "linked.png").symlink_to(outside)
    (dist / "other").mkdir()
    (dist / "other" / "file.js").write_bytes(b"other")
    return dist


def configure_spa(
    monkeypatch: pytest.MonkeyPatch, dist: Path | str | None, enabled: bool = True
) -> None:
    monkeypatch.setattr(settings, "spa_enabled", enabled)
    monkeypatch.setattr(settings, "spa_dist_dir", "" if dist is None else str(dist))


def etag_of(body: bytes) -> str:
    return '"' + hashlib.sha256(body).hexdigest() + '"'


def bearer_header() -> tuple[bytes, bytes]:
    """A valid HS256 JWT for a fresh tenant (own rate-limit bucket)."""

    now = int(time.time())
    token = jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(uuid4()),
            "role": "clinic_admin",
            "iat": now,
            "exp": now + 600,
        },
        settings.secret_key,
        algorithm="HS256",
    )
    return (b"authorization", f"Bearer {token}".encode("ascii"))


@dataclass
class AsgiResponse:
    status: int
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""

    def header(self, name: str) -> str | None:
        values = [v for k, v in self.headers if k == name.lower()]
        assert len(values) <= 1, f"duplicate {name} header: {values}"
        return values[0] if values else None

    def stable(self) -> tuple[int, list[tuple[str, str]], bytes]:
        headers = sorted((k, v) for k, v in self.headers if k not in VOLATILE_HEADERS)
        return self.status, headers, self.body


def default_raw_path(path: str) -> bytes:
    return quote(path, safe="/:@!$&'()*+,;=-._~", errors="strict").encode("ascii")


async def asgi_request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    raw_path: bytes | None = None,
    headers: list[tuple[bytes, bytes]] | None = None,
    root_path: str = "",
    query_string: bytes = b"",
    host: bytes = b"testserver",
) -> AsgiResponse:
    """Drive the full ASGI app with an exact ``scope['path']``.

    HTTP clients normalize paths (``//x`` becomes a host); the attack matrix
    needs the decoded path a server such as uvicorn would hand to the app.
    """

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": default_raw_path(path) if raw_path is None else raw_path,
        "root_path": root_path,
        "query_string": query_string,
        "headers": [(b"host", host), *(headers or [])],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    queued: list[Message] = [{"type": "http.request", "body": b"", "more_body": False}]
    response = AsgiResponse(status=0)

    async def receive() -> Message:
        if queued:
            return queued.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.start":
            response.status = message["status"]
            response.headers = [
                (k.decode("latin-1").lower(), v.decode("latin-1"))
                for k, v in message.get("headers", [])
            ]
        elif message["type"] == "http.response.body":
            response.body += message.get("body", b"")

    await app(scope, receive, send)
    return response


async def _keep_loop_responsive() -> None:
    while True:
        await asyncio.sleep(0.01)


@asynccontextmanager
async def asgi_lifespan(app: FastAPI) -> AsyncIterator[dict[str, Any]]:
    """Run the ASGI lifespan protocol the way a server (uvicorn) does.

    Yields the lifespan ``state`` dict the server would copy into requests.
    """

    to_app: asyncio.Queue[Message] = asyncio.Queue()
    from_app: asyncio.Queue[Message] = asyncio.Queue()
    state: dict[str, Any] = {}
    scope: Scope = {
        "type": "lifespan",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "state": state,
    }
    # aiosqlite wakes the loop from a thread; keep it ticking (tests/conftest.py).
    heartbeat = asyncio.create_task(_keep_loop_responsive())
    task = asyncio.create_task(app(scope, to_app.get, from_app.put))
    try:
        await to_app.put({"type": "lifespan.startup"})
        started = await from_app.get()
        if started["type"] != "lifespan.startup.complete":
            await task  # surfaces the startup error
            raise AssertionError(started)
        try:
            yield state
        finally:
            await to_app.put({"type": "lifespan.shutdown"})
            stopped = await from_app.get()
            await task
            assert stopped["type"] == "lifespan.shutdown.complete", stopped
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat


def route_signature(route: Any) -> tuple[Any, ...]:
    """Comparable identity of a top-level route across two app instances."""

    included = getattr(route, "original_router", None)
    endpoint = getattr(route, "endpoint", None)
    mounted = getattr(route, "app", None)
    return (
        type(route).__name__,
        getattr(route, "path", None),
        tuple(sorted(getattr(route, "methods", None) or ())),
        getattr(endpoint, "__qualname__", None),
        type(mounted).__name__ if mounted is not None else None,
        id(included) if included is not None else None,
        getattr(getattr(route, "include_context", None), "prefix", None),
    )
