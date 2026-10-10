"""Post-auth canonical tenant identity middleware contracts."""

import hashlib
import hmac
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from src.api.middleware.auth import TenantContext
from src.api.middleware.rate_limit import RateLimitMiddleware, rate_limit_store
from src.api.middleware.tenant_identity import TenantIdentityMiddleware


class _AuthenticatedTenant:
    def __init__(self, app: Any, *, tenant: TenantContext) -> None:
        self.app = app
        self.tenant = tenant

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        scope.setdefault("state", {})["tenant"] = self.tenant
        scope["state"]["request_id"] = "server-request-id"
        await self.app(scope, receive, send)


def _event_app(tenant_id: UUID, *, with_rate_limit: bool = False) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/events")
    async def endpoint(request: Request) -> dict[str, Any]:
        raw = await request.body()
        return {
            "raw": raw.decode("utf-8"),
            "tenant_id": str(request.state.canonical_tenant_id),
        }

    if with_rate_limit:
        app.add_middleware(RateLimitMiddleware)
    app.add_middleware(TenantIdentityMiddleware)
    app.add_middleware(
        _AuthenticatedTenant,
        tenant=TenantContext(tenant_id=tenant_id, api_key="not-a-real-key"),
    )
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("assertion", [None, "same"])
async def test_event_missing_or_same_assertion_preserves_bytes_and_identity(
    assertion: str | None,
) -> None:
    tenant_id = uuid4()
    body: dict[str, Any] = {
        "session_id": str(uuid4()),
        "payload": {"content": "olá"},
    }
    if assertion == "same":
        body["tenant_id"] = str(tenant_id)
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()

    async with AsyncClient(
        transport=ASGITransport(app=_event_app(tenant_id)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/events", content=raw, headers={"content-type": "application/json"}
        )

    assert response.status_code == 200
    assert response.json() == {"raw": raw.decode(), "tenant_id": str(tenant_id)}


@pytest.mark.asyncio
async def test_event_mismatch_audits_once_before_exhausted_rate_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    bucket = rate_limit_store.get_or_create_bucket(str(tenant_id), rate_limit=1)
    while bucket.consume():
        pass
    bucket_tokens_before = bucket.tokens
    audit = MagicMock()
    claimed_tenant = uuid4()
    monkeypatch.setattr(
        "src.api.middleware.tenant_identity._audit_logger.warning", audit
    )

    async with AsyncClient(
        transport=ASGITransport(app=_event_app(tenant_id, with_rate_limit=True)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/events",
            json={
                "tenant_id": str(claimed_tenant),
                "session_id": str(uuid4()),
                "payload": {"content": "secret message"},
            },
        )

    assert response.status_code == 403
    assert response.json()["error"] == "tenant_identity_mismatch"
    audit.assert_called_once()
    event, kwargs = audit.call_args.args[0], audit.call_args.kwargs
    assert event == "tenant_identity_mismatch"
    assert kwargs == {
        "request_id": "server-request-id",
        "route": "/v1/events",
        "method": "POST",
        "authentication_mode": "api_key",
        "authenticated_tenant": str(tenant_id),
        "claimed_tenant": str(claimed_tenant),
    }
    assert bucket.tokens == bucket_tokens_before
    rendered = json.dumps(kwargs)
    for forbidden in (
        "not-a-real-key",
        "secret message",
        "authorization",
        "signature",
        "phone",
        "provider",
    ):
        assert forbidden not in rendered.lower()


def _meta_body(phone_ids: list[str], *, tenant_id: str | None = None) -> bytes:
    body: dict[str, Any] = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": phone_id},
                            "statuses": [{"id": f"status-{index}"}],
                        },
                    }
                ]
            }
            for index, phone_id in enumerate(phone_ids)
        ],
    }
    if tenant_id is not None:
        body["tenant_id"] = tenant_id
    return json.dumps(body, separators=(",", ":")).encode()


def _meta_app() -> FastAPI:
    app = FastAPI()

    @app.post("/v1/webhook/meta")
    async def endpoint(request: Request) -> dict[str, str]:
        return {
            "tenant_id": str(request.state.tenant.tenant_id),
            "phone_id": request.state.meta_phone_number_id,
            "raw_sha256": hashlib.sha256(await request.body()).hexdigest(),
        }

    app.add_middleware(TenantIdentityMiddleware)
    return app


def _signature(raw: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_meta_status_only_receiver_is_hmac_bound_before_tenant_publish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    phone_id = "receiver-1"
    secret = "candidate-secret"
    raw = _meta_body([phone_id])
    candidates = AsyncMock(
        return_value=[
            (
                phone_id,
                tenant_id,
                {"meta_phone_number_id": phone_id, "meta_app_secret": secret},
            )
        ]
    )
    monkeypatch.setattr(
        "src.api.middleware.tenant_identity._load_meta_candidates", candidates
    )

    async with AsyncClient(
        transport=ASGITransport(app=_meta_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/webhook/meta",
            content=raw,
            headers={"X-Hub-Signature-256": _signature(raw, secret)},
        )

    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": str(tenant_id),
        "phone_id": phone_id,
        "raw_sha256": hashlib.sha256(raw).hexdigest(),
    }
    candidates.assert_awaited_once_with({phone_id})


@pytest.mark.asyncio
async def test_meta_valid_receiver_plus_empty_entry_is_one_audited_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    phone_id = "receiver-1"
    secret = "candidate-secret"
    body = json.loads(_meta_body([phone_id]))
    body["entry"].append({"changes": []})
    raw = json.dumps(body, separators=(",", ":")).encode()
    candidates = AsyncMock(
        return_value=[
            (
                phone_id,
                tenant_id,
                {"meta_phone_number_id": phone_id, "meta_app_secret": secret},
            )
        ]
    )
    audit = MagicMock()
    monkeypatch.setattr(
        "src.api.middleware.tenant_identity._load_meta_candidates", candidates
    )
    monkeypatch.setattr(
        "src.api.middleware.tenant_identity._audit_logger.warning", audit
    )

    async with AsyncClient(
        transport=ASGITransport(app=_meta_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/webhook/meta",
            content=raw,
            headers={"X-Hub-Signature-256": _signature(raw, secret)},
        )

    assert response.status_code == 403
    assert response.json()["error"] == "tenant_identity_mismatch"
    audit.assert_called_once()
    assert audit.call_args.kwargs == {
        "request_id": "unavailable",
        "route": "/v1/webhook/meta",
        "method": "POST",
        "authentication_mode": "meta_hmac",
        "authenticated_tenant": str(tenant_id),
        "claimed_tenant": "unresolved",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed", [123, True, ["receiver-1"], {"id": "receiver-1"}, None]
)
async def test_meta_signed_malformed_receiver_is_one_audited_403(
    malformed: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    phone_id = "receiver-1"
    secret = "candidate-secret"
    body = json.loads(_meta_body([phone_id]))
    malformed_change = json.loads(_meta_body([phone_id]))["entry"][0]["changes"][0]
    if malformed is None:
        malformed_change["value"]["metadata"].pop("phone_number_id")
    else:
        malformed_change["value"]["metadata"]["phone_number_id"] = malformed
    body["entry"].append({"changes": [malformed_change]})
    raw = json.dumps(body, separators=(",", ":")).encode()
    candidates = AsyncMock(
        return_value=[
            (
                phone_id,
                tenant_id,
                {"meta_phone_number_id": phone_id, "meta_app_secret": secret},
            )
        ]
    )
    audit = MagicMock()
    monkeypatch.setattr(
        "src.api.middleware.tenant_identity._load_meta_candidates", candidates
    )
    monkeypatch.setattr(
        "src.api.middleware.tenant_identity._audit_logger.warning", audit
    )

    async with AsyncClient(
        transport=ASGITransport(app=_meta_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/webhook/meta",
            content=raw,
            headers={"X-Hub-Signature-256": _signature(raw, secret)},
        )

    assert response.status_code == 403
    assert response.json() == {
        "error": "tenant_identity_mismatch",
        "message": "Tenant identity does not match the authenticated request",
    }
    candidates.assert_awaited_once_with({phone_id})
    audit.assert_called_once()
    assert set(audit.call_args.kwargs) == {
        "request_id",
        "route",
        "method",
        "authentication_mode",
        "authenticated_tenant",
        "claimed_tenant",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("receiver", ["missing", "empty", "non_string"])
async def test_meta_signed_invalid_only_receiver_authenticates_then_audits_403(
    receiver: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    phone_id = "receiver-1"
    secret = "candidate-secret"
    body = json.loads(_meta_body([phone_id]))
    metadata = body["entry"][0]["changes"][0]["value"]["metadata"]
    if receiver == "missing":
        metadata.pop("phone_number_id")
    elif receiver == "empty":
        metadata["phone_number_id"] = ""
    else:
        metadata["phone_number_id"] = 123
    raw = json.dumps(body, separators=(",", ":")).encode()
    candidates = AsyncMock(
        return_value=[
            (
                phone_id,
                tenant_id,
                {"meta_phone_number_id": phone_id, "meta_app_secret": secret},
            )
        ]
    )
    verifier = MagicMock(
        side_effect=lambda signature, body_bytes, app_secret: signature
        == _signature(body_bytes, app_secret)
    )
    audit = MagicMock()
    monkeypatch.setattr(
        "src.api.middleware.tenant_identity._load_meta_candidates", candidates
    )
    monkeypatch.setattr(
        "src.api.middleware.tenant_identity._verify_meta_signature", verifier
    )
    monkeypatch.setattr(
        "src.api.middleware.tenant_identity._audit_logger.warning", audit
    )

    async with AsyncClient(
        transport=ASGITransport(app=_meta_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/webhook/meta",
            content=raw,
            headers={"X-Hub-Signature-256": _signature(raw, secret)},
        )

    assert response.status_code == 403
    assert response.json()["error"] == "tenant_identity_mismatch"
    candidates.assert_awaited_once_with(set())
    verifier.assert_called_once_with(_signature(raw, secret), raw, secret)
    audit.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        b'{"payload":',
        b"[" * 2_000 + b"]" * 2_000,
        b'{"pathological":' + b"9" * 5_000 + b"}",
    ],
)
async def test_unparseable_event_json_stops_before_rate_and_route(
    raw: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    rate = MagicMock(side_effect=AssertionError("rate limit touched"))
    monkeypatch.setattr(rate_limit_store, "get_or_create_bucket", rate)

    async with AsyncClient(
        transport=ASGITransport(app=_event_app(tenant_id, with_rate_limit=True)),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/v1/events",
            content=raw,
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 400
    assert response.json()["error"] == "invalid_json"
    rate.assert_not_called()


@pytest.mark.asyncio
async def test_meta_shared_secret_never_selects_between_signed_receivers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "shared-secret"
    first, second = "receiver-a", "receiver-b"
    raw = _meta_body([first, second])
    candidates = AsyncMock(
        return_value=[
            (
                first,
                uuid4(),
                {"meta_phone_number_id": first, "meta_app_secret": secret},
            ),
            (
                second,
                uuid4(),
                {"meta_phone_number_id": second, "meta_app_secret": secret},
            ),
        ]
    )
    audit = MagicMock()
    monkeypatch.setattr(
        "src.api.middleware.tenant_identity._load_meta_candidates", candidates
    )
    monkeypatch.setattr(
        "src.api.middleware.tenant_identity._audit_logger.warning", audit
    )

    async with AsyncClient(
        transport=ASGITransport(app=_meta_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/webhook/meta",
            content=raw,
            headers={"X-Hub-Signature-256": _signature(raw, secret)},
        )

    assert response.status_code == 403
    assert response.json()["error"] == "tenant_identity_mismatch"
    audit.assert_called_once()


@pytest.mark.asyncio
async def test_meta_multiple_distinct_valid_secrets_is_generic_401(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    phone_id = "receiver-a"
    raw = _meta_body([phone_id])
    candidates = AsyncMock(
        return_value=[
            (
                phone_id,
                uuid4(),
                {"meta_phone_number_id": phone_id, "meta_app_secret": "a"},
            ),
            (
                phone_id,
                uuid4(),
                {"meta_phone_number_id": phone_id, "meta_app_secret": "b"},
            ),
        ]
    )
    monkeypatch.setattr(
        "src.api.middleware.tenant_identity._load_meta_candidates", candidates
    )
    monkeypatch.setattr(
        "src.api.middleware.tenant_identity._verify_meta_signature",
        lambda *_args: True,
    )

    async with AsyncClient(
        transport=ASGITransport(app=_meta_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/webhook/meta",
            content=raw,
            headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
        )

    assert response.status_code == 401
    assert response.json() == {"error": "invalid_webhook_auth"}


@pytest.mark.asyncio
async def test_meta_declared_tenant_mismatch_is_audited_403(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    phone_id = "receiver-1"
    secret = "candidate-secret"
    raw = _meta_body([phone_id], tenant_id=str(uuid4()))
    monkeypatch.setattr(
        "src.api.middleware.tenant_identity._load_meta_candidates",
        AsyncMock(
            return_value=[
                (
                    phone_id,
                    tenant_id,
                    {"meta_phone_number_id": phone_id, "meta_app_secret": secret},
                )
            ]
        ),
    )
    audit = MagicMock()
    monkeypatch.setattr(
        "src.api.middleware.tenant_identity._audit_logger.warning", audit
    )

    async with AsyncClient(
        transport=ASGITransport(app=_meta_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/v1/webhook/meta",
            content=raw,
            headers={"X-Hub-Signature-256": _signature(raw, secret)},
        )

    assert response.status_code == 403
    assert response.json()["error"] == "tenant_identity_mismatch"
    audit.assert_called_once()
