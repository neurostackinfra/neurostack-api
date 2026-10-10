"""Early request-body limits for tenant-authoritative JSON ingress."""

from collections.abc import Awaitable, Callable

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_INGRESS_BODY_BYTES = 1024 * 1024
_FIXED_LIMITED_ENDPOINTS = frozenset(
    {
        "/v1/events",
        "/v1/webhook/meta",
    }
)


class IngressBodyLimitMiddleware:
    """Reject oversized event and Meta bodies before any other middleware.

    The middleware buffers only the two bounded JSON endpoints. Accepted ASGI
    request messages are replayed unchanged, including their original chunk
    boundaries and ``more_body`` flags, so downstream HMAC verification sees
    the exact bytes sent by the caller.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int = MAX_INGRESS_BODY_BYTES,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if not self._is_limited_request(scope):
            await self.app(scope, receive, send)
            return

        declared_length, valid_length = self._content_length(scope)
        if not valid_length or self._has_ambiguous_framing(scope):
            await self._reject_malformed(scope, receive, send)
            return
        if declared_length is not None and declared_length > self.max_body_bytes:
            await self._reject(scope, receive, send)
            return

        body = bytearray()
        terminal: Message | None = None
        received = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                # A disconnect is not a completed request body. Do not turn
                # accumulated bytes into a synthetic terminal request frame.
                return
            chunk = message.get("body", b"")
            received += len(chunk)
            if received > self.max_body_bytes:
                await self._reject(scope, receive, send)
                return
            body.extend(chunk)
            if not message.get("more_body", False):
                break

        if declared_length is not None and received != declared_length:
            await self._reject_malformed(scope, receive, send)
            return

        accepted_body = bytes(body)
        del body
        scope.setdefault("state", {})["bounded_ingress_body"] = accepted_body
        replay = _replay_body(accepted_body, terminal, receive)
        await self.app(scope, replay, send)

    @staticmethod
    def _is_limited_request(scope: Scope) -> bool:
        path = scope.get("path", "")
        return (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and path in _FIXED_LIMITED_ENDPOINTS
        )

    @staticmethod
    def _content_length(scope: Scope) -> tuple[int | None, bool]:
        values = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == b"content-length"
        ]
        if not values:
            return None, True
        if len(values) != 1 or b"," in values[0]:
            return None, False
        try:
            rendered = values[0].decode("ascii")
            length = int(rendered)
        except (UnicodeDecodeError, ValueError):
            return None, False
        if length < 0 or rendered.strip() != rendered or not rendered.isdecimal():
            return None, False
        return length, True

    @staticmethod
    def _has_ambiguous_framing(scope: Scope) -> bool:
        names = [name.lower() for name, _value in scope.get("headers", [])]
        return b"content-length" in names and b"transfer-encoding" in names

    async def _reject(self, scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={
                "error": "request_body_too_large",
                "message": "Request body exceeds the 1 MiB limit",
            },
        )
        await response(scope, receive, send)

    async def _reject_malformed(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        response = JSONResponse(
            status_code=400,
            content={
                "error": "malformed_request_body",
                "message": "Request body framing is invalid or incomplete",
            },
        )
        await response(scope, receive, send)


def _replay_body(
    body: bytes, terminal: Message | None, fallback_receive: Receive
) -> Callable[[], Awaitable[Message]]:
    """Replay exact accepted bytes with constant message-frame overhead."""

    index = 0

    async def replay() -> Message:
        nonlocal index
        if index == 0:
            index = 1
            return {"type": "http.request", "body": body, "more_body": False}
        if index == 1 and terminal is not None:
            index = 2
            return terminal
        return await fallback_receive()

    return replay
