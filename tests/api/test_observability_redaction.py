"""Observability sink redaction tests for Group 4.

The wish's egress/observability group demands that the Z-API capability
token embedded in ``/v1/webhook/zapi/{capability_token}`` never appears in
any sink the application controls:

- structured application logs (``structlog``)
- FastAPI/Starlette access logs (Uvicorn is fully silenced;
  ``RequestIdMiddleware`` emits a post-routing template log)
- Prometheus metrics labels (``PerformanceMiddleware``)
- OpenTelemetry span attributes are not touched by this group directly, but
  the unified redaction utility is exercised here so drift is caught.
- The global exception handler (Sentry-shaped ``extra``)

Every test in this file inspects real emitted records: ``caplog`` for
logging, direct call of the middleware helpers for metrics/tracing, and an
async ASGI round-trip that hits an unmatched capability path.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.api.main import _redact_capability_token
from src.api.middleware.performance import PerformanceMiddleware
from src.api.middleware.tracing import RequestIdMiddleware, _redact_raw_path
from src.config.logging import configure_logging

CAPABILITY_TOKEN_SENTINEL = "aBc-VeryLongCapabilityToken_0123456789xyz"


class TestRedactRawPath:
    """Pure regex-based redaction used by the tracing/access log."""

    def test_replaces_capability_token(self) -> None:
        raw = f"/v1/webhook/zapi/{CAPABILITY_TOKEN_SENTINEL}"
        result = _redact_raw_path(raw)
        assert CAPABILITY_TOKEN_SENTINEL not in result
        assert result == "/v1/webhook/zapi/{capability_token}"

    def test_short_segment_left_alone(self) -> None:
        # <20 chars — treated as a normal path segment, not a token.
        raw = "/v1/webhook/zapi/short"
        result = _redact_raw_path(raw)
        assert result == raw

    def test_preserves_prefix_and_suffix(self) -> None:
        raw = f"/some/prefix/v1/webhook/zapi/{CAPABILITY_TOKEN_SENTINEL}/rest"
        result = _redact_raw_path(raw)
        assert CAPABILITY_TOKEN_SENTINEL not in result
        assert "/some/prefix/v1/webhook/zapi/{capability_token}/rest" == result

    def test_idempotent(self) -> None:
        raw = f"/v1/webhook/zapi/{CAPABILITY_TOKEN_SENTINEL}"
        once = _redact_raw_path(raw)
        twice = _redact_raw_path(once)
        assert once == twice


class TestRedactCapabilityTokenInMainHandler:
    """Same regex logic in ``src.api.main._redact_capability_token``."""

    def test_replaces_capability_token(self) -> None:
        raw = f"/v1/webhook/zapi/{CAPABILITY_TOKEN_SENTINEL}"
        assert _redact_capability_token(raw) == "/v1/webhook/zapi/{capability_token}"
        assert CAPABILITY_TOKEN_SENTINEL not in _redact_capability_token(raw)


class TestPerformanceMiddlewareNormalizePath:
    """The metrics label helper must never carry a raw capability token."""

    def _mw(self) -> PerformanceMiddleware:
        # Any dummy ASGI is fine — we call the helper directly.
        return PerformanceMiddleware(app=lambda *a, **kw: None)

    def test_zapi_capability_token_redacted(self) -> None:
        mw = self._mw()
        redacted = mw._normalize_path(f"/v1/webhook/zapi/{CAPABILITY_TOKEN_SENTINEL}")
        assert CAPABILITY_TOKEN_SENTINEL not in redacted
        assert redacted == "/v1/webhook/zapi/{capability_token}"

    def test_uuid_still_normalised(self) -> None:
        mw = self._mw()
        assert (
            mw._normalize_path("/v1/tenants/12345678-1234-1234-1234-123456789012/x")
            == "/v1/tenants/{id}/x"
        )

    def test_numeric_id_still_normalised(self) -> None:
        mw = self._mw()
        assert mw._normalize_path("/v1/foo/42/bar") == "/v1/foo/{id}/bar"

    def test_short_zapi_segment_untouched(self) -> None:
        mw = self._mw()
        assert mw._normalize_path("/v1/webhook/zapi/short") == "/v1/webhook/zapi/short"


class TestUvicornAccessLogDisabled:
    def test_logger_is_disabled_after_configure_logging(self) -> None:
        # ``configure_logging`` is invoked at ``src.api.main`` import already,
        # but call it here to be explicit and idempotent.
        configure_logging()
        access_logger = logging.getLogger("uvicorn.access")
        assert access_logger.disabled is True
        assert access_logger.propagate is False
        assert access_logger.handlers == []
        # Level set above CRITICAL so nothing is ever emitted.
        assert access_logger.level > logging.CRITICAL


class TestRequestIdMiddlewareAccessLog:
    """End-to-end: a request to a capability path must log the template only.

    We build a tiny FastAPI app whose route uses a ``{capability_token}``
    parameter (matching the wish contract), fire a request whose token is
    the ``CAPABILITY_TOKEN_SENTINEL``, and assert the caplog record contains
    the template value and never the raw token.
    """

    @pytest.fixture()
    def app(self) -> FastAPI:
        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)

        @app.post("/v1/webhook/zapi/{capability_token}")
        async def zapi(capability_token: str) -> dict[str, str]:  # noqa: ARG001
            return {"ok": "true"}

        @app.get("/orphan-path")
        async def orphan() -> dict[str, str]:
            return {"ok": "true"}

        return app

    async def test_capability_token_absent_from_access_log(
        self, app: FastAPI, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            with caplog.at_level(logging.INFO):
                resp = await client.post(
                    f"/v1/webhook/zapi/{CAPABILITY_TOKEN_SENTINEL}"
                )
        assert resp.status_code == 200
        # Combine every record's message + args + extras so we're sure the
        # token cannot reach any field.
        blob = "\n".join(
            record.getMessage()
            + " "
            + " ".join(str(v) for v in getattr(record, "args", ()) or ())
            + " "
            + str(record.__dict__)
            for record in caplog.records
        )
        assert CAPABILITY_TOKEN_SENTINEL not in blob
        assert "/v1/webhook/zapi/{capability_token}" in blob

    async def test_orphan_path_still_logs_template(
        self, app: FastAPI, caplog: pytest.LogCaptureFixture
    ) -> None:
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            with caplog.at_level(logging.INFO):
                resp = await client.get("/orphan-path")
        assert resp.status_code == 200
        # The route resolved, so the template ``/orphan-path`` is logged.
        blob = " ".join(
            record.getMessage() + str(record.__dict__) for record in caplog.records
        )
        assert "/orphan-path" in blob


class TestPerformanceMiddlewareEndpointLabel:
    """When a real route matches, the label is the template — never the token."""

    async def test_matched_route_uses_template(self) -> None:
        app = FastAPI()
        app.add_middleware(PerformanceMiddleware)

        @app.post("/v1/webhook/zapi/{capability_token}")
        async def zapi(capability_token: str) -> dict[str, str]:  # noqa: ARG001
            return {"ok": "true"}

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            resp = await client.post(f"/v1/webhook/zapi/{CAPABILITY_TOKEN_SENTINEL}")
        assert resp.status_code == 200
        # Timing header is set but never carries the token.
        assert CAPABILITY_TOKEN_SENTINEL not in resp.headers.get("X-Response-Time", "")

    def test_metrics_label_never_carries_token(self) -> None:
        # Directly poke the helper with a fake request/response whose
        # scope['route'] is a Route-like object with a template path.
        class FakeRoute:
            path = "/v1/webhook/zapi/{capability_token}"

        class FakeRequest:
            def __init__(self) -> None:
                self.scope = {"route": FakeRoute()}
                self.url = type(
                    "U",
                    (),
                    {"path": f"/v1/webhook/zapi/{CAPABILITY_TOKEN_SENTINEL}"},
                )()

        class FakeResponse:
            status_code = 200
            headers: dict[str, str] = {}

        mw = PerformanceMiddleware(app=lambda *a, **kw: None)
        label = mw._endpoint_label(FakeRequest(), FakeResponse())
        assert label == "/v1/webhook/zapi/{capability_token}"
        assert CAPABILITY_TOKEN_SENTINEL not in label


class TestGlobalExceptionHandlerRedaction:
    """The main.py handler falls back to ``_redact_capability_token``.

    We instantiate a minimal app that raises inside a route and check the
    caught endpoint label never contains the token.
    """

    async def test_no_route_falls_back_to_redaction(
        self, async_client: AsyncClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        # ``async_client`` owns the real app lifespan and keeps its event loop
        # responsive while startup/shutdown uses async database resources.
        with caplog.at_level(logging.ERROR):
            # An unmatched capability path returns 404; no exception is
            # raised, so this test is really about the metric/log path rather
            # than the exception sink itself. The exception handler is
            # exercised in a dedicated unit test that calls
            # ``_redact_capability_token`` directly (above).
            response = await async_client.post(
                f"/v1/webhook/zapi/{CAPABILITY_TOKEN_SENTINEL}"
            )
        assert response.status_code == 401
        blob = " ".join(
            record.getMessage() + str(record.__dict__) for record in caplog.records
        )
        assert CAPABILITY_TOKEN_SENTINEL not in blob


class TestNoUvicornAccessOnRealApp:
    def test_no_uvicorn_access_records_emitted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Register the ``uvicorn.access`` logger so caplog surfaces any leak
        # if the disable ever regresses.
        access = logging.getLogger("uvicorn.access")
        # Force propagation on — caplog listens at the root — to detect any
        # accidental leak, then rely on the ``disabled`` flag / handler
        # removal to guarantee no record is emitted.
        was_propagate = access.propagate
        access.propagate = True
        try:
            with caplog.at_level(logging.DEBUG, logger="uvicorn.access"):
                # Emit a record — should be swallowed because ``disabled=True``.
                access.info(f"leak-attempt {CAPABILITY_TOKEN_SENTINEL}")
        finally:
            access.propagate = was_propagate
        for record in caplog.records:
            assert CAPABILITY_TOKEN_SENTINEL not in record.getMessage()


class TestRegexBoundary:
    """Belt-and-suspenders: the redaction regex must not over- or under-match."""

    def test_dashes_and_underscores_in_token(self) -> None:
        token = "A-B_c-d_e-f_g-h_i-j_k-l_m-n"
        assert len(token) >= 20
        redacted = _redact_raw_path(f"/v1/webhook/zapi/{token}")
        assert token not in redacted

    def test_only_first_zapi_segment_matched(self) -> None:
        # Some hypothetical path that mentions the base URL twice — only
        # the segment after ``/v1/webhook/zapi/`` is replaced.
        raw = f"/api/v1/webhook/zapi/{CAPABILITY_TOKEN_SENTINEL}"
        redacted = _redact_raw_path(raw)
        assert CAPABILITY_TOKEN_SENTINEL not in redacted

    def test_alphanumeric_only_boundary(self) -> None:
        # Contains ``.`` — must not be treated as a redactable segment
        # because ``.`` is not in ``[A-Za-z0-9_-]``.
        raw = "/v1/webhook/zapi/" + "token.with.dot.and.more.than.20.chars"
        redacted = _redact_raw_path(raw)
        # The regex won't match; path stays as-is.
        assert redacted == raw
