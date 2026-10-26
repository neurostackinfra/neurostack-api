from __future__ import annotations

import asyncio
import hashlib

import pytest
from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from starlette.types import Message, Scope

from src.api.middleware.auth import AuthMiddleware
from src.api.routes import billing_producer_setup
from src.config.settings import settings
from src.services.billing_signed_envelope import (
    ALGORITHM,
    ENVIRONMENT,
    PURPOSE_BY_TYPE,
    SCHEMA_VERSION,
)


def _configured_build_key(monkeypatch: pytest.MonkeyPatch) -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1()).public_key()
    pem = key.public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    values = {
        "environment": ENVIRONMENT,
        "billing_producer_build_issuer": "github-actions:neurostack-build",
        "billing_producer_build_kid": (
            "arn:aws:kms:us-east-1:388460142431:key/"
            "01234567-89ab-cdef-0123-456789abcdef"
        ),
        "billing_producer_build_audience": "billing-api:producer-build-attestation",
        "billing_producer_build_public_key": pem,
        "billing_producer_build_repository_id": "1152335815",
        "billing_producer_build_repository_name": "neurostackinfra/neurostack-api",
        "billing_producer_build_workflow_path": (
            ".github/workflows/asaas-build-attestation.yml"
        ),
        "billing_producer_build_workflow_ref": "refs/heads/main",
        "billing_producer_build_oidc_issuer": (
            "https://token.actions.githubusercontent.com"
        ),
        "billing_producer_build_oidc_subject": (
            "repo:neurostackinfra@340698182/neurostack-api@1152335815:"
            "environment:asaas-build:ref:refs/heads/main:"
            "workflow_ref:neurostackinfra/neurostack-api/.github/workflows/"
            "asaas-build-attestation.yml@refs/heads/main"
        ),
    }
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)
    return pem


@pytest.mark.asyncio
async def test_build_trust_readback_returns_only_public_canonical_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pem = _configured_build_key(monkeypatch)
    response = Response()

    result = await billing_producer_setup.read_producer_build_trust(response)

    assert result == {
        "schema": "billing_producer_build_trust_readback.v1",
        "schema_version": SCHEMA_VERSION,
        "environment": ENVIRONMENT,
        "type": "billing_build_attestation.v1",
        "purpose": PURPOSE_BY_TYPE["billing_build_attestation.v1"],
        "algorithm": ALGORITHM,
        "issuer": "github-actions:neurostack-build",
        "kid": (
            "arn:aws:kms:us-east-1:388460142431:key/"
            "01234567-89ab-cdef-0123-456789abcdef"
        ),
        "audience": "billing-api:producer-build-attestation",
        "public_key_fingerprint_sha256": hashlib.sha256(
            serialization.load_pem_public_key(pem.encode("ascii")).public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).hexdigest(),
        "repository_id": "1152335815",
        "repository_name": "neurostackinfra/neurostack-api",
        "workflow_path": ".github/workflows/asaas-build-attestation.yml",
        "workflow_ref": "refs/heads/main",
        "oidc_issuer": "https://token.actions.githubusercontent.com",
        "oidc_subject": settings.billing_producer_build_oidc_subject,
    }
    assert "public_key_pem" not in result
    assert all(
        "private" not in key.lower() and "secret" not in key.lower() for key in result
    )
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


@pytest.mark.asyncio
async def test_build_trust_readback_fails_closed_for_missing_or_invalid_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured_build_key(monkeypatch)
    monkeypatch.setattr(settings, "billing_producer_build_oidc_subject", "")
    with pytest.raises(HTTPException) as missing:
        await billing_producer_setup.read_producer_build_trust(Response())
    assert getattr(missing.value, "status_code", None) == 503

    _configured_build_key(monkeypatch)
    monkeypatch.setattr(settings, "billing_producer_build_public_key", "not a key")
    with pytest.raises(HTTPException) as invalid:
        await billing_producer_setup.read_producer_build_trust(Response())
    assert getattr(invalid.value, "status_code", None) == 503


@pytest.mark.asyncio
async def test_public_build_trust_readback_is_separate_and_uses_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pem = _configured_build_key(monkeypatch)
    response = Response()

    result = await billing_producer_setup.read_public_producer_build_trust(response)

    assert result["schema"] == "billing_producer_build_trust_readback.v1"
    assert (
        result["public_key_fingerprint_sha256"]
        == hashlib.sha256(
            serialization.load_pem_public_key(pem.encode("ascii")).public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).hexdigest()
    )
    assert "read_at_utc" in result
    assert "public_key_pem" not in result
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def test_admin_build_trust_http_contract_remains_public_and_unchanged(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    pem = _configured_build_key(monkeypatch)
    response = client.get("/v1/admin/billing/producer-build-trust")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema"] == "billing_producer_build_trust_readback.v1"
    assert (
        body["public_key_fingerprint_sha256"]
        == hashlib.sha256(
            serialization.load_pem_public_key(pem.encode("ascii")).public_bytes(
                serialization.Encoding.DER,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        ).hexdigest()
    )
    assert "read_at_utc" not in body
    assert "public_key_pem" not in body
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"


def test_public_build_trust_unavailable_http_contract(client, monkeypatch) -> None:
    _configured_build_key(monkeypatch)
    monkeypatch.setattr(settings, "billing_producer_build_public_key", "")

    response = client.get("/v1/billing/producer-build-trust")

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"detail": {"error": "producer_build_trust_unavailable"}}
    # The bootstrap probe compares these exact bytes; bind both sides.
    from scripts.asaas_build_release_preflight import BOOTSTRAP_UNAVAILABLE_RESPONSE

    assert response.content == BOOTSTRAP_UNAVAILABLE_RESPONSE
    # The exact GET exception wraps error responses with no-store as well.
    assert response.headers["cache-control"] == "no-store"


def test_build_trust_route_is_exact_get_without_route_auth_dependency() -> None:
    app = FastAPI()
    app.include_router(billing_producer_setup.router, prefix="/v1/admin/billing")
    app.include_router(billing_producer_setup.public_router, prefix="/v1/billing")
    paths = app.openapi()["paths"]
    path = "/v1/admin/billing/producer-build-trust"
    assert set(paths[path]) == {"get"}
    assert set(paths["/v1/billing/producer-build-trust"]) == {"get"}
    route = next(
        route
        for route in billing_producer_setup.router.routes
        if route.path == "/producer-build-trust"
    )
    assert route.methods == {"GET"}
    assert route.dependant.dependencies == []


def test_public_build_trust_openapi_omits_api_key_but_protected_billing_keeps_it() -> (
    None
):
    from src.api.main import create_app

    schema = create_app().openapi()
    paths = schema["paths"]
    public_operation = paths[f"{settings.api_prefix}/billing/producer-build-trust"][
        "get"
    ]
    protected_operation = paths[f"{settings.api_prefix}/billing/summary"]["get"]

    assert not any(
        parameter.get("in") == "header" and parameter.get("name") == "X-API-Key"
        for parameter in public_operation.get("parameters", [])
    )
    assert public_operation["security"] == []
    assert protected_operation.get("security", schema["security"]) == [
        {"ApiKeyAuth": []},
        {"BearerAuth": []},
    ]
    assert any(
        parameter.get("in") == "header" and parameter.get("name") == "X-API-Key"
        for parameter in protected_operation.get("parameters", [])
    )


def _http_scope(path: str, *, method: str = "GET") -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("test", 1),
        "server": ("test", 443),
    }


def test_auth_middleware_exempts_only_exact_trust_get_paths() -> None:
    async def exercise(path: str, *, method: str = "GET"):
        started: list[Message] = []

        async def downstream(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            started.append(message)

        await AuthMiddleware(downstream)(
            _http_scope(path, method=method), receive, send
        )
        return started

    messages = asyncio.run(exercise("/v1/billing/producer-build-trust"))
    response_start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    assert response_start["status"] == 200

    admin = asyncio.run(exercise("/v1/admin/billing/producer-build-trust"))
    admin_start = next(
        message for message in admin if message["type"] == "http.response.start"
    )
    assert admin_start["status"] == 200
    assert (b"cache-control", b"no-store") in admin_start["headers"]

    adjacent = asyncio.run(exercise("/v1/billing/producer-build-trust/other"))
    adjacent_start = next(
        message for message in adjacent if message["type"] == "http.response.start"
    )
    assert adjacent_start["status"] == 401

    admin_adjacent = asyncio.run(
        exercise("/v1/admin/billing/producer-build-trust/other")
    )
    admin_adjacent_start = next(
        message
        for message in admin_adjacent
        if message["type"] == "http.response.start"
    )
    assert admin_adjacent_start["status"] == 401

    public_put = asyncio.run(exercise("/v1/billing/producer-build-trust", method="PUT"))
    public_put_start = next(
        message for message in public_put if message["type"] == "http.response.start"
    )
    assert public_put_start["status"] == 401

    admin_put = asyncio.run(
        exercise("/v1/admin/billing/producer-build-trust", method="PUT")
    )
    admin_put_start = next(
        message for message in admin_put if message["type"] == "http.response.start"
    )
    assert admin_put_start["status"] == 401


def test_build_trust_schema_version_matches_signed_envelope_protocol() -> None:
    assert SCHEMA_VERSION == "v1"
    assert (
        PURPOSE_BY_TYPE["billing_build_attestation.v1"]
        == "billing-build-attestation.v1"
    )
