"""Pre-parser request caps and fail-fast media concurrency budgets."""

from __future__ import annotations

from threading import Lock
from typing import Final

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.api.routing_paths import decision_path

MEDIA_MULTIPART_MAX_BYTES: Final = 51_000_000


class MediaRequestTooLarge(Exception):
    """Raised before a multipart parser can receive an oversized body."""


class MediaConcurrencyExceeded(Exception):
    """Raised before receive when no media permit is immediately available."""


class ImmediatePermit:
    """An idempotently releasable permit from an :class:`ImmediateLimiter`."""

    def __init__(self, limiter: ImmediateLimiter) -> None:
        self._limiter = limiter
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._limiter._release()


class ImmediateLimiter:
    """Small process-local limiter which never queues callers.

    The lock makes the counter safe even when ASGI applications are driven by
    more than one event-loop thread. A rejected caller never touches receive.
    """

    def __init__(self, limit: int) -> None:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("media concurrency limit must be a positive integer")
        self.limit = limit
        self._active = 0
        self._lock = Lock()

    @property
    def active(self) -> int:
        with self._lock:
            return self._active

    def try_acquire(self) -> ImmediatePermit | None:
        with self._lock:
            if self._active >= self.limit:
                return None
            self._active += 1
        return ImmediatePermit(self)

    def _release(self) -> None:
        with self._lock:
            if self._active < 1:
                raise RuntimeError("media concurrency permit released below zero")
            self._active -= 1


class MediaConcurrencyLimiters:
    """Shared upload/download limiters attached to one application instance."""

    def __init__(self, *, uploads: int, downloads: int) -> None:
        self.uploads = ImmediateLimiter(uploads)
        self.downloads = ImmediateLimiter(downloads)


def _error_response(status_code: int, code: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": {"code": code}})


def _declared_content_length(scope: Scope) -> int | None:
    values = [
        value
        for name, value in scope.get("headers", [])
        if name.lower() == b"content-length"
    ]
    if not values:
        return None
    if len(values) != 1:
        raise ValueError("ambiguous content-length")
    raw = values[0]
    if not raw or not raw.isdigit():
        raise ValueError("invalid content-length")
    return int(raw)


class MediaUploadLimitsMiddleware:
    """Protect the media upload path before multipart parsing starts."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiters: MediaConcurrencyLimiters,
        upload_path: str,
        request_max_bytes: int = MEDIA_MULTIPART_MAX_BYTES,
    ) -> None:
        self.app = app
        self.limiters = limiters
        self.upload_path = upload_path
        self.request_max_bytes = request_max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope.get("method") != "POST"
            # the router's path: "%0a" still routes to the upload endpoint
            or decision_path(scope) != self.upload_path
        ):
            await self.app(scope, receive, send)
            return

        try:
            declared_length = _declared_content_length(scope)
        except ValueError:
            await _error_response(400, "media_content_length_invalid")(
                scope, receive, send
            )
            return
        if declared_length is not None and declared_length > self.request_max_bytes:
            await _error_response(413, "media_request_size_exceeded")(
                scope, receive, send
            )
            return

        received = 0
        permit: ImmediatePermit | None = None

        async def capped_receive() -> Message:
            nonlocal permit, received
            if permit is None:
                permit = self.limiters.uploads.try_acquire()
                if permit is None:
                    raise MediaConcurrencyExceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.request_max_bytes:
                    raise MediaRequestTooLarge
            return message

        try:
            await self.app(scope, capped_receive, send)
        except MediaConcurrencyExceeded:
            await _error_response(503, "media_concurrency_exceeded")(
                scope, receive, send
            )
        except MediaRequestTooLarge:
            await _error_response(413, "media_request_size_exceeded")(
                scope, receive, send
            )
        finally:
            if permit is not None:
                permit.release()
