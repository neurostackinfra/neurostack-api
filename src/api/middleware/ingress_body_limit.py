"""Early authentication and request-body limits for JSON ingress."""

import asyncio
import hashlib
import hmac
import json
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.api.routing_paths import decision_path
from src.config.settings import settings
from src.services.billing_webhook import (
    ASAAS_INGRESS_BODY_STATE,
    ASAAS_INGRESS_PAYLOAD_STATE,
    ASAAS_WEBHOOK_BODY_BYTES,
    ASAAS_WEBHOOK_PATH,
    ASAAS_WEBHOOK_READ_TIMEOUT_SECONDS,
    ASAAS_WEBHOOK_TOKEN_HEADER,
    record_rejection,
)

MAX_INGRESS_BODY_BYTES = 1024 * 1024
STAGING_REHEARSAL_TOKEN_BODY_BYTES = 2 * 1024
STAGING_REHEARSAL_TOKEN_READ_TIMEOUT_SECONDS = 2.0
_STAGING_REHEARSAL_TOKEN_PATH = "/v1/auth/staging-rehearsal-token"
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
        asaas_read_timeout_seconds: float = ASAAS_WEBHOOK_READ_TIMEOUT_SECONDS,
    ) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes
        self.asaas_read_timeout_seconds = asaas_read_timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Paths are judged as the router matches them (routing_paths): a
        # trailing "%0a" still routes to the exact endpoint, so it must not
        # skip the token check or the body bound here.
        is_staging_rehearsal_token = (
            scope.get("type") == "http"
            and scope.get("method") == "POST"
            and decision_path(scope) == _STAGING_REHEARSAL_TOKEN_PATH
        )
        if is_staging_rehearsal_token:
            send = self._no_store_send(send)
        if self._is_asaas_webhook(scope):
            # Separate, exact-path boundary: the generic 1 MiB cap below never
            # sees this path, and this branch never sees the generic paths.
            await self._asaas_webhook(scope, receive, send)
            return
        if not self._is_limited_request(scope):
            await self.app(scope, receive, send)
            return

        body_limit = (
            STAGING_REHEARSAL_TOKEN_BODY_BYTES
            if is_staging_rehearsal_token
            else self.max_body_bytes
        )

        declared_length, valid_length = self._content_length(scope)
        if not valid_length or self._has_ambiguous_framing(scope):
            await self._reject_malformed(scope, receive, send)
            return
        if declared_length is not None and declared_length > body_limit:
            await self._reject(scope, receive, send)
            return

        body = bytearray()
        terminal: Message | None = None
        received = 0
        try:
            async with asyncio.timeout(
                STAGING_REHEARSAL_TOKEN_READ_TIMEOUT_SECONDS
                if is_staging_rehearsal_token
                else None
            ):
                while True:
                    message = await receive()
                    if message["type"] != "http.request":
                        # A disconnect is not a completed request body. Do not turn
                        # accumulated bytes into a synthetic terminal request frame.
                        return
                    chunk = message.get("body", b"")
                    received += len(chunk)
                    if received > body_limit:
                        await self._reject(scope, receive, send)
                        return
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
        except TimeoutError:
            await self._reject_body_timeout(scope, receive, send)
            return

        if declared_length is not None and received != declared_length:
            await self._reject_malformed(scope, receive, send)
            return

        accepted_body = bytes(body)
        del body
        scope.setdefault("state", {})["bounded_ingress_body"] = accepted_body
        replay = _replay_body(accepted_body, terminal, receive)
        await self.app(scope, replay, send)

    @staticmethod
    def _is_asaas_webhook(scope: Scope) -> bool:
        return (
            scope["type"] == "http"
            and scope.get("method") == "POST"
            and decision_path(scope) == ASAAS_WEBHOOK_PATH
        )

    async def _asaas_webhook(self, scope: Scope, receive: Receive, send: Send) -> None:
        """Authenticate, bound and strictly parse one ASAAS delivery.

        Order is the contract: configured token, then the ``asaas-access-token``
        header compared in constant time, then media type and framing, and
        only then the body, read within ``ASAAS_WEBHOOK_BODY_BYTES`` and
        ``asaas_read_timeout_seconds`` measured from the first receive through
        the JSON parse (chunked included).  Every rejection happens before any
        downstream code runs, so nothing is reserved or persisted, and no
        response echoes the token, headers or body.
        """

        expected = settings.asaas_webhook_token.get_secret_value()
        if not expected:
            await self._asaas_reject(scope, receive, send, 503, "unavailable")
            return
        presented = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == ASAAS_WEBHOOK_TOKEN_HEADER.encode("ascii")
        ]
        # Fixed-length digests keep the comparison constant-time regardless of
        # the presented length; a repeated header is ambiguous and refused.
        valid = len(presented) == 1 and hmac.compare_digest(
            hashlib.sha256(presented[0]).digest(),
            hashlib.sha256(expected.encode("utf-8")).digest(),
        )
        if not valid:
            await self._asaas_reject(scope, receive, send, 401, "unauthorized")
            return
        if not self._is_json_media_type(scope):
            await self._asaas_reject(
                scope, receive, send, 415, "unsupported_media_type"
            )
            return
        declared_length, valid_length = self._content_length(scope)
        if not valid_length or self._has_ambiguous_framing(scope):
            await self._asaas_reject(scope, receive, send, 400, "malformed")
            return
        if declared_length is not None and declared_length > ASAAS_WEBHOOK_BODY_BYTES:
            await self._asaas_reject(scope, receive, send, 413, "too_large")
            return

        outcome = "ok"
        body = bytearray()
        payload: object = None
        try:
            async with asyncio.timeout(self.asaas_read_timeout_seconds):
                while True:
                    message = await receive()
                    if message["type"] != "http.request":
                        return  # disconnect: never a completed body
                    chunk = message.get("body", b"")
                    if len(body) + len(chunk) > ASAAS_WEBHOOK_BODY_BYTES:
                        outcome = "too_large"
                        break
                    body.extend(chunk)
                    if not message.get("more_body", False):
                        break
                if outcome == "ok":
                    if declared_length is not None and len(body) != declared_length:
                        outcome = "malformed"
                    else:
                        payload = _strict_json(bytes(body))
                        if payload is None:
                            outcome = "invalid_json"
        except TimeoutError:
            outcome = "timeout"
        # Responses are sent outside the deadline scope so a late deadline can
        # never interrupt (or double-start) a response already being sent.
        if outcome != "ok":
            del body
            status = {"too_large": 413, "timeout": 408}.get(outcome, 400)
            await self._asaas_reject(scope, receive, send, status, outcome)
            return
        accepted = bytes(body)
        del body
        state = scope.setdefault("state", {})
        state[ASAAS_INGRESS_BODY_STATE] = accepted
        state[ASAAS_INGRESS_PAYLOAD_STATE] = payload
        await self.app(scope, _replay_body(accepted, None, receive), send)

    @staticmethod
    def _is_json_media_type(scope: Scope) -> bool:
        values = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == b"content-type"
        ]
        if len(values) != 1:
            return False
        media_type: bytes = values[0].split(b";", 1)[0].strip().lower()
        return bool(media_type == b"application/json")

    @staticmethod
    async def _asaas_reject(
        scope: Scope, receive: Receive, send: Send, status: int, reason: str
    ) -> None:
        record_rejection(reason)
        detail = {
            401: "unauthorized",
            408: "request_body_timeout",
            413: "request_body_too_large",
            415: "unsupported_media_type",
            503: "billing_webhook_unavailable",
        }.get(status, "invalid_request")
        await JSONResponse(status_code=status, content={"detail": detail})(
            scope, receive, send
        )

    @staticmethod
    def _is_limited_request(scope: Scope) -> bool:
        if scope["type"] != "http" or scope.get("method") != "POST":
            return False
        path = decision_path(scope)
        return path in _FIXED_LIMITED_ENDPOINTS or path == _STAGING_REHEARSAL_TOKEN_PATH

    @staticmethod
    def _no_store_send(send: Send) -> Send:
        async def send_no_store(message: Message) -> None:
            if message.get("type") == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        return send_no_store

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
                "message": "Request body exceeds the configured limit",
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

    @staticmethod
    async def _reject_body_timeout(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=408,
            content={"error": "request_body_timeout"},
        )
        await response(scope, receive, send)


def _reject_constant(_constant: str) -> Any:
    raise ValueError("non-finite JSON constant")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def _strict_json(body: bytes) -> object | None:
    """Parse UTF-8 JSON without floats, NaN/Infinity or duplicate keys."""

    try:
        parsed: object = json.loads(
            body.decode("utf-8"),
            parse_float=Decimal,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object,
        )
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    return parsed


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
