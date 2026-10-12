"""ASAAS webhook transport boundary, public-path exception and route contract.

Every rejection here must happen before any downstream code (so before any
delivery is reserved or persisted) and must never echo the token, headers or
body.  The Abacate boundary is asserted unchanged and unable to govern (or be
governed by) the new path.  No network: provider calls are fakes.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import Response
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from pydantic import SecretStr
from starlette.types import Message, Scope

from src.api.main import app as main_app
from src.api.middleware.auth import AuthMiddleware
from src.api.middleware.ingress_body_limit import IngressBodyLimitMiddleware
from src.api.routes import billing
from src.config.settings import settings
from src.repositories import get_db_session_dependency
from src.services import billing_checkout as checkout_module
from src.services.billing_webhook import (
    ASAAS_INGRESS_BODY_STATE,
    ASAAS_INGRESS_PAYLOAD_STATE,
    ASAAS_WEBHOOK_BODY_BYTES,
    ASAAS_WEBHOOK_PATH,
    ASAAS_WEBHOOK_READINESS,
    WebhookNotReady,
    WebhookProcessingUnavailable,
    WebhookReadiness,
    _Outcome,
)
from src.services.egress_policy import EgressPolicy
from tests.api.test_billing_checkout_fail_closed import (
    _checkout,
    _checkout_body,
    _FlagReader,
    _install_general_reservation,
    _Session,
)

TOKEN = "asaas-fixture-token-" + "x" * 24
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "asaas" / "webhook"


def _metric(name: str, **labels: str) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "asaas_webhook_token", SecretStr(TOKEN))
    monkeypatch.setattr(
        settings, "abacatepay_webhook_secret", SecretStr("legacy-fixture-secret")
    )


def _scope(
    path: str, headers: list[tuple[bytes, bytes]], method: str = "POST"
) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers,
        "client": ("test", 1),
        "server": ("test", 80),
    }


def _headers(
    *,
    token: str | None = TOKEN,
    content_type: str | None = "application/json",
    extra: list[tuple[bytes, bytes]] | None = None,
) -> list[tuple[bytes, bytes]]:
    headers: list[tuple[bytes, bytes]] = []
    if token is not None:
        headers.append((b"asaas-access-token", token.encode()))
    if content_type is not None:
        headers.append((b"content-type", content_type.encode()))
    return headers + (extra or [])


async def _invoke(
    messages: list[Message],
    headers: list[tuple[bytes, bytes]],
    *,
    path: str = ASAAS_WEBHOOK_PATH,
    method: str = "POST",
    timeout: float | None = None,
    delay: float = 0.0,
) -> tuple[list[Scope], list[Message], list[str]]:
    reached: list[Scope] = []
    sent: list[Message] = []
    receives: list[str] = []
    queued = list(messages)

    async def receive() -> Message:
        receives.append("receive")
        if delay:
            await asyncio.sleep(delay)
        if queued:
            return queued.pop(0)
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        sent.append(message)

    async def downstream(scope: Scope, inner_receive: Any, inner_send: Any) -> None:
        reached.append(scope)
        while True:
            message = await inner_receive()
            if message["type"] != "http.request" or not message.get("more_body"):
                break
        await Response(status_code=204)(scope, inner_receive, inner_send)

    kwargs = {} if timeout is None else {"asaas_read_timeout_seconds": timeout}
    middleware = IngressBodyLimitMiddleware(downstream, **kwargs)
    await middleware(_scope(path, headers, method), receive, send)
    return reached, sent, receives


def _status(sent: list[Message]) -> int:
    return int(sent[0]["status"])


def _response_text(sent: list[Message]) -> str:
    return b"".join(
        m.get("body", b"") for m in sent if m["type"] == "http.response.body"
    ).decode()


def _payload_bytes() -> bytes:
    return (FIXTURES / "payment_event.json").read_bytes()


# ---------------------------------------------------------------------------
# Authentication before the body.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        _headers(token=None),
        _headers(token="wrong-token"),
        _headers(token=""),
        _headers(token=TOKEN.upper()),
        _headers(extra=[(b"asaas-access-token", TOKEN.encode())]),  # repeated
        _headers(token=None, extra=[(b"x-webhook-signature", b"abc")]),
    ],
    ids=["missing", "wrong", "empty", "case", "repeated", "abacate-signature-only"],
)
async def test_invalid_auth_rejects_before_reading_any_body(
    headers: list[tuple[bytes, bytes]],
) -> None:
    before = _metric(
        "neurostack_billing_webhook_rejections_total", reason="unauthorized"
    )
    reached, sent, receives = await _invoke(
        [{"type": "http.request", "body": _payload_bytes(), "more_body": False}],
        headers,
    )

    assert reached == [] and receives == []
    assert _status(sent) == 401
    text = _response_text(sent)
    assert TOKEN not in text and "pay_000000000000001" not in text
    assert (
        _metric("neurostack_billing_webhook_rejections_total", reason="unauthorized")
        == before + 1
    )


async def test_unconfigured_token_fails_closed_without_reading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "asaas_webhook_token", SecretStr(""))
    reached, sent, receives = await _invoke(
        [{"type": "http.request", "body": b"{}", "more_body": False}], _headers()
    )
    assert (reached, receives, _status(sent)) == ([], [], 503)


async def test_non_json_media_type_is_rejected_before_reading() -> None:
    reached, sent, receives = await _invoke(
        [{"type": "http.request", "body": b"{}", "more_body": False}],
        _headers(content_type="text/plain"),
    )
    assert (reached, receives, _status(sent)) == ([], [], 415)


# ---------------------------------------------------------------------------
# Size, time and framing.
# ---------------------------------------------------------------------------


async def test_declared_oversize_is_413_without_reading() -> None:
    reached, sent, receives = await _invoke(
        [{"type": "http.request", "body": b"{}", "more_body": False}],
        _headers(
            extra=[(b"content-length", str(ASAAS_WEBHOOK_BODY_BYTES + 1).encode())]
        ),
    )
    assert (reached, receives, _status(sent)) == ([], [], 413)


async def test_chunked_excess_is_413_and_never_parsed() -> None:
    chunk = b" " * 65_536
    messages: list[Message] = [
        {"type": "http.request", "body": chunk, "more_body": True} for _ in range(4)
    ] + [{"type": "http.request", "body": b"{}", "more_body": False}]
    reached, sent, _receives = await _invoke(messages, _headers())
    assert reached == []
    assert _status(sent) == 413


async def test_body_exactly_at_the_limit_is_accepted() -> None:
    body = b'{"pad":"' + b"a" * (ASAAS_WEBHOOK_BODY_BYTES - 10) + b'"}'
    assert len(body) == ASAAS_WEBHOOK_BODY_BYTES
    reached, sent, _ = await _invoke(
        [
            {"type": "http.request", "body": body[:100_000], "more_body": True},
            {"type": "http.request", "body": body[100_000:], "more_body": False},
        ],
        _headers(),
    )
    assert len(reached) == 1 and _status(sent) == 204


async def test_slow_body_times_out_with_408_and_no_downstream() -> None:
    before = _metric("neurostack_billing_webhook_rejections_total", reason="timeout")
    reached, sent, _ = await _invoke(
        [
            {"type": "http.request", "body": b'{"id":', "more_body": True},
            {"type": "http.request", "body": b'"x"}', "more_body": False},
        ],
        _headers(),
        timeout=0.05,
        delay=0.2,
    )
    assert reached == []
    assert _status(sent) == 408
    assert len([m for m in sent if m["type"] == "http.response.start"]) == 1
    assert _metric("neurostack_billing_webhook_rejections_total", reason="timeout") == (
        before + 1
    )


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"a": NaN}',
        b'{"a": Infinity}',
        b'{"id": "x", "id": "y"}',
        b"\xff\xfe{}",
        b"",
    ],
    ids=["garbage", "nan", "infinity", "duplicate-key", "not-utf8", "empty"],
)
async def test_non_json_body_is_rejected_without_downstream(body: bytes) -> None:
    reached, sent, _ = await _invoke(
        [{"type": "http.request", "body": body, "more_body": False}], _headers()
    )
    assert reached == []
    assert _status(sent) == 400


async def test_framing_errors_fail_closed() -> None:
    mismatch = await _invoke(
        [{"type": "http.request", "body": b"{}", "more_body": False}],
        _headers(extra=[(b"content-length", b"3")]),
    )
    ambiguous = await _invoke(
        [{"type": "http.request", "body": b"{}", "more_body": False}],
        _headers(extra=[(b"content-length", b"2"), (b"transfer-encoding", b"chunked")]),
    )
    assert (mismatch[0], _status(mismatch[1])) == ([], 400)
    assert (ambiguous[0], _status(ambiguous[1])) == ([], 400)


async def test_disconnect_never_reaches_downstream() -> None:
    reached, sent, _ = await _invoke(
        [
            {"type": "http.request", "body": b'{"id":', "more_body": True},
            {"type": "http.disconnect"},
        ],
        _headers(),
    )
    assert reached == [] and sent == []


async def test_accepted_body_is_replayed_and_strictly_parsed() -> None:
    body = _payload_bytes()
    reached, sent, _ = await _invoke(
        [
            {"type": "http.request", "body": body[:50], "more_body": True},
            {"type": "http.request", "body": body[50:], "more_body": False},
        ],
        _headers(),
    )
    assert _status(sent) == 204
    state = reached[0]["state"]
    assert state[ASAAS_INGRESS_BODY_STATE] == body
    payload = state[ASAAS_INGRESS_PAYLOAD_STATE]
    assert payload["payment"]["value"] == Decimal("999.00")


# ---------------------------------------------------------------------------
# The two webhook boundaries never govern each other.
# ---------------------------------------------------------------------------


async def test_abacate_settings_never_govern_the_asaas_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "abacatepay_webhook_secret", SecretStr(""))
    reached, sent, _ = await _invoke(
        [{"type": "http.request", "body": b"{}", "more_body": False}], _headers()
    )
    assert len(reached) == 1 and _status(sent) == 204


async def test_asaas_token_never_satisfies_the_abacate_rule() -> None:
    reached, sent, receives = await _invoke(
        [{"type": "http.request", "body": b"{}", "more_body": False}],
        _headers(),
        path="/v1/billing/webhook",
    )
    assert (reached, receives, _status(sent)) == ([], [], 400)
    assert json.loads(_response_text(sent)) == {"detail": "Invalid signature"}


@pytest.mark.parametrize(
    "path",
    [
        "/v1/billing/webhook/asaas/",
        "/v1/billing/webhook/asaasx",
        "/v1/billing/webhook/asaas/extra",
        "/V1/billing/webhook/asaas",
    ],
)
async def test_near_miss_paths_are_neither_public_nor_bounded_here(path: str) -> None:
    assert AuthMiddleware(MagicMock())._is_public_path(path) is False
    reached, _sent, _ = await _invoke(
        [{"type": "http.request", "body": b"x", "more_body": False}],
        _headers(token=None),
        path=path,
    )
    assert len(reached) == 1  # passes through to the app (and auth) untouched


def test_exact_asaas_path_is_the_only_new_public_exception() -> None:
    auth = AuthMiddleware(MagicMock())
    assert auth._is_public_path(ASAAS_WEBHOOK_PATH) is True
    assert auth._is_public_path("/v1/billing/webhook") is True
    assert auth._is_public_path("/v1/billing/checkout") is False
    assert auth._is_public_path("/v1/billing/summary") is False


# ---------------------------------------------------------------------------
# Route contract.
# ---------------------------------------------------------------------------


class _FakeRequest:
    def __init__(self, state: dict[str, Any]) -> None:
        self.scope = {"state": state}


async def test_route_without_boundary_state_fails_closed() -> None:
    db = MagicMock()
    with pytest.raises(HTTPException) as exc_info:
        await billing.asaas_webhook(_FakeRequest({}), MagicMock(), db)  # type: ignore[arg-type]
    assert exc_info.value.status_code == 503


async def test_route_rejects_event_without_identity_before_the_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = MagicMock(side_effect=AssertionError("service reached"))
    monkeypatch.setattr(billing, "BillingWebhookService", service)
    payload = {"event": "PAYMENT_CONFIRMED", "dateCreated": "2026-10-11 10:00:00"}
    state = {
        ASAAS_INGRESS_BODY_STATE: json.dumps(payload).encode(),
        ASAAS_INGRESS_PAYLOAD_STATE: payload,
    }
    with pytest.raises(HTTPException) as exc_info:
        await billing.asaas_webhook(_FakeRequest(state), MagicMock(), MagicMock())  # type: ignore[arg-type]
    assert exc_info.value.status_code == 400
    service.assert_not_called()


async def test_route_maps_transient_failure_to_retryable_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Failing:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def receive(self, _envelope: object) -> object:
            raise WebhookProcessingUnavailable()

    monkeypatch.setattr(billing, "BillingWebhookService", Failing)
    payload = json.loads(_payload_bytes())
    state = {
        ASAAS_INGRESS_BODY_STATE: _payload_bytes(),
        ASAAS_INGRESS_PAYLOAD_STATE: payload,
    }
    with pytest.raises(HTTPException) as exc_info:
        await billing.asaas_webhook(_FakeRequest(state), MagicMock(), MagicMock())  # type: ignore[arg-type]
    assert exc_info.value.status_code == 503


def test_full_app_accepts_authenticated_delivery_without_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[object] = []
    readbacks: list[UUID] = []
    delivery_id = uuid4()

    class Service:
        def __init__(self, _db: object, *, environment: str) -> None:
            assert environment == settings.environment

        async def receive(self, envelope: object) -> _Outcome:
            received.append(envelope)
            return _Outcome("payment", "readback_required", delivery_id, True)

    async def readback(delivery: UUID, **_kwargs: object) -> None:
        readbacks.append(delivery)

    async def db_dependency() -> AsyncIterator[object]:
        yield object()

    monkeypatch.setattr(billing, "BillingWebhookService", Service)
    monkeypatch.setattr(billing, "run_delivery_readback", readback)
    main_app.dependency_overrides[get_db_session_dependency] = db_dependency
    try:
        with TestClient(main_app) as client:
            accepted = client.post(
                ASAAS_WEBHOOK_PATH,
                content=_payload_bytes(),
                headers={
                    "asaas-access-token": TOKEN,
                    "Content-Type": "application/json",
                },
            )
            refused = client.post(
                ASAAS_WEBHOOK_PATH,
                content=_payload_bytes(),
                headers={
                    "asaas-access-token": "nope",
                    "Content-Type": "application/json",
                },
            )
            slash = client.post(
                ASAAS_WEBHOOK_PATH + "/",
                content=_payload_bytes(),
                headers={
                    "asaas-access-token": TOKEN,
                    "Content-Type": "application/json",
                },
            )
    finally:
        main_app.dependency_overrides.pop(get_db_session_dependency, None)

    assert accepted.status_code == 200 and accepted.json() == {"ok": True}
    assert refused.status_code == 401
    assert slash.status_code == 401  # trailing slash is not the public exception
    assert len(received) == 1
    assert readbacks == [delivery_id]
    assert TOKEN not in accepted.text + refused.text + slash.text


# ---------------------------------------------------------------------------
# Checkout stays blocked until the webhook configuration and token read back.
# ---------------------------------------------------------------------------


@pytest.fixture
def _closed_gate() -> Any:
    ASAAS_WEBHOOK_READINESS.reset()
    yield ASAAS_WEBHOOK_READINESS
    ASAAS_WEBHOOK_READINESS.reset()


async def test_creation_client_is_refused_until_webhook_is_ready(
    monkeypatch: pytest.MonkeyPatch, _closed_gate: Any
) -> None:
    sentinel = object()
    monkeypatch.setattr(billing, "_asaas_client", lambda: sentinel)
    refreshes: list[int] = []

    async def drifted() -> WebhookReadiness:
        refreshes.append(1)
        return WebhookReadiness(False, ("url",))

    monkeypatch.setattr(billing, "_read_back_webhook_readiness", drifted)
    with pytest.raises(WebhookNotReady):
        billing._asaas_checkout_client()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert refreshes == [1]
    with pytest.raises(WebhookNotReady):
        billing._asaas_checkout_client()  # drift keeps it closed

    _closed_gate.record(WebhookReadiness(True, ()))
    assert billing._asaas_checkout_client() is sentinel


async def test_checkout_with_unready_webhook_has_no_row_and_no_post(
    monkeypatch: pytest.MonkeyPatch, _closed_gate: Any
) -> None:
    builder_id = uuid4()
    db = _Session([None, None, 0])
    _install_general_reservation(monkeypatch, _checkout(builder_id))
    reader = _FlagReader(True)
    provider = MagicMock(side_effect=AssertionError("provider built"))
    monkeypatch.setattr(
        checkout_module,
        "require_financial_egress",
        EgressPolicy(reader).require_financial_egress,
    )
    monkeypatch.setattr(billing, "_asaas_client", provider)

    async def drifted() -> WebhookReadiness:
        return WebhookReadiness(False, ("events",))

    monkeypatch.setattr(billing, "_read_back_webhook_readiness", drifted)
    with pytest.raises(HTTPException) as exc_info:
        await billing.create_checkout(_checkout_body(), builder_id, db)  # type: ignore[arg-type]

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "billing_checkout_unavailable"
    assert db.commits == 0
    provider.assert_not_called()


async def test_readback_uses_the_ungated_client_and_desired_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={"hasMore": False, "data": []})

    from src.services.asaas import AsaasClient

    monkeypatch.setattr(
        billing,
        "_asaas_client",
        lambda: AsaasClient(
            api_key=SecretStr("$aact_prod_fixture"),
            user_agent="NeuroStack/test",
            transport=httpx.MockTransport(handler),
        ),
    )
    monkeypatch.setattr(
        settings,
        "asaas_webhook_url",
        "https://api.example.test/v1/billing/webhook/asaas",
    )
    monkeypatch.setattr(
        settings, "asaas_webhook_notification_email", "ops@example.test"
    )
    verdict = await billing._read_back_webhook_readiness()
    assert verdict.ready is False and verdict.reasons == ("absent",)
    assert seen == ["GET /v3/webhooks"]


# ---------------------------------------------------------------------------
# MEDIUM 1: the readiness verdict is refreshed without waiting for traffic.
# ---------------------------------------------------------------------------


async def test_idle_expired_gate_refreshes_before_the_checkout_takes_any_lock(
    monkeypatch: pytest.MonkeyPatch, _closed_gate: Any
) -> None:
    clock = [1_000.0]
    monkeypatch.setattr(_closed_gate, "_monotonic", lambda: clock[0])
    _closed_gate.record(WebhookReadiness(True, ()))
    clock[0] += 3_600  # a long idle period: the verdict expired

    builder_id = uuid4()
    db = _Session([None, None, 0])
    _install_general_reservation(monkeypatch, _checkout(builder_id))
    monkeypatch.setattr(
        checkout_module,
        "require_financial_egress",
        EgressPolicy(_FlagReader(True)).require_financial_egress,
    )
    from src.services.asaas import CheckoutCreated

    client = SimpleNamespace(
        create_checkout=AsyncMock(return_value=CheckoutCreated("chk_000000000000009"))
    )
    monkeypatch.setattr(billing, "_asaas_client", MagicMock(return_value=client))
    refreshes: list[int] = []

    async def ready() -> WebhookReadiness:
        refreshes.append(1)
        return WebhookReadiness(True, ())

    monkeypatch.setattr(billing, "_read_back_webhook_readiness", ready)
    response = await billing.create_checkout(_checkout_body(), builder_id, db)  # type: ignore[arg-type]

    assert refreshes == [1]
    assert response.checkout_url is not None
    client.create_checkout.assert_awaited_once()


async def test_failed_refresh_after_idle_keeps_checkout_closed(
    monkeypatch: pytest.MonkeyPatch, _closed_gate: Any
) -> None:
    clock = [1_000.0]
    monkeypatch.setattr(_closed_gate, "_monotonic", lambda: clock[0])
    _closed_gate.record(WebhookReadiness(True, ()))
    clock[0] += 3_600
    builder_id = uuid4()
    db = _Session([None, None, 0])
    _install_general_reservation(monkeypatch, _checkout(builder_id))
    monkeypatch.setattr(
        checkout_module,
        "require_financial_egress",
        EgressPolicy(_FlagReader(True)).require_financial_egress,
    )
    provider = MagicMock(side_effect=AssertionError("provider built"))
    monkeypatch.setattr(billing, "_asaas_client", provider)

    async def failing() -> WebhookReadiness:
        raise RuntimeError("provider down")

    monkeypatch.setattr(billing, "_read_back_webhook_readiness", failing)
    with pytest.raises(HTTPException) as exc_info:
        await billing.create_checkout(_checkout_body(), builder_id, db)  # type: ignore[arg-type]
    assert exc_info.value.status_code == 503
    assert db.commits == 0
    provider.assert_not_called()
    assert _closed_gate.is_ready() is False


async def test_refresher_keeps_failures_closed_with_bounded_backoff() -> None:
    from src.services.billing_webhook import WebhookReadinessGate

    gate = WebhookReadinessGate(soft_ttl=300, hard_ttl=900, retry_interval=30)
    sleeps: list[float] = []
    verdicts = [False, False, False, False, True, False]

    async def refresh() -> WebhookReadiness:
        ok = verdicts.pop(0)
        if not ok:
            raise RuntimeError("provider down")
        return WebhookReadiness(True, ())

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        assert gate.is_ready() is (len(sleeps) == 5)
        if not verdicts:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await gate.run_refresher(refresh, max_backoff=120, sleep=fake_sleep)
    assert sleeps == [30, 60, 120, 120, 300, 30]
    assert gate.is_ready() is False


def test_lifespan_starts_the_refresher_only_when_asaas_is_configured(
    monkeypatch: pytest.MonkeyPatch, _closed_gate: Any
) -> None:
    from src.api import main as main_module

    assert main_module._start_webhook_readiness_refresher() is None  # unconfigured

    async def ready() -> WebhookReadiness:
        return WebhookReadiness(True, ())

    monkeypatch.setattr(settings, "asaas_api_key", SecretStr("$aact_prod_fixture"))
    monkeypatch.setattr(
        settings,
        "asaas_webhook_url",
        "https://api.example.test/v1/billing/webhook/asaas",
    )
    monkeypatch.setattr(billing, "_read_back_webhook_readiness", ready)

    async def db_dependency() -> AsyncIterator[object]:
        yield object()

    main_app.dependency_overrides[get_db_session_dependency] = db_dependency
    try:
        with TestClient(main_app) as client:
            client.get("/health/live")
            assert _closed_gate.is_ready() is True  # warmed at startup, no checkout
    finally:
        main_app.dependency_overrides.pop(get_db_session_dependency, None)


# ---------------------------------------------------------------------------
# C-1: documented event ids (``evt_<hex>&<digits>``) through ingress + route.
# ---------------------------------------------------------------------------


def _post_all(
    monkeypatch: pytest.MonkeyPatch, bodies: list[bytes]
) -> tuple[list[int], list[Any]]:
    received: list[Any] = []

    class Service:
        def __init__(self, _db: object, *, environment: str) -> None:
            del environment

        async def receive(self, envelope: Any) -> _Outcome:
            received.append(envelope)
            return _Outcome("payment", "processed", uuid4(), False)

    async def db_dependency() -> AsyncIterator[object]:
        yield object()

    monkeypatch.setattr(billing, "BillingWebhookService", Service)
    main_app.dependency_overrides[get_db_session_dependency] = db_dependency
    try:
        with TestClient(main_app) as client:
            statuses = [
                client.post(
                    ASAAS_WEBHOOK_PATH,
                    content=body,
                    headers={
                        "asaas-access-token": TOKEN,
                        "Content-Type": "application/json",
                    },
                ).status_code
                for body in bodies
            ]
    finally:
        main_app.dependency_overrides.pop(get_db_session_dependency, None)
    return statuses, received


def test_documented_event_ids_pass_the_boundary_and_route_for_every_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services.billing_webhook import event_key

    names = ("checkout_event.json", "subscription_event.json", "payment_event.json")
    bodies = [(FIXTURES / name).read_bytes() for name in names]
    statuses, received = _post_all(monkeypatch, bodies)

    assert statuses == [200, 200, 200]
    for name, envelope in zip(names, received):
        raw = json.loads((FIXTURES / name).read_text())["id"]
        assert "&" in raw
        assert envelope.event_id == event_key(raw)


@pytest.mark.parametrize(
    ("raw", "accepted"),
    [
        ("evt_\u0001bad&1", False),
        ("evt_\u0085bad&1", False),  # C1 control character
        ("evt_" + "a" * 600, False),
        ("", False),
        ("evt_açaí_€&368604920", True),  # hashed, never pattern-rejected
    ],
    ids=["c0-control", "c1-control", "overlong", "empty", "unicode"],
)
def test_pathological_event_ids_are_rejected_or_hashed(
    monkeypatch: pytest.MonkeyPatch, raw: str, accepted: bool
) -> None:
    payload = json.loads((FIXTURES / "payment_event.json").read_text())
    payload["id"] = raw
    statuses, received = _post_all(monkeypatch, [json.dumps(payload).encode()])
    assert statuses == ([200] if accepted else [400])
    assert len(received) == (1 if accepted else 0)
