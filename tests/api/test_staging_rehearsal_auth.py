"""Issuer, Redis gate, and outer ingress boundaries for staging rehearsal JWTs."""

from __future__ import annotations

import asyncio
import json
import time
from collections import Counter
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import jwt
import pytest
from fastapi import FastAPI

from src.api.middleware.auth import (
    STAGING_REHEARSAL_GLOBAL_LIMIT,
    STAGING_REHEARSAL_REDIS_TIMEOUT_SECONDS,
    STAGING_REHEARSAL_TOKEN_PATH,
    STAGING_REHEARSAL_TOKEN_SCOPE,
    STAGING_REHEARSAL_TOKEN_USE,
    AuthMiddleware,
)
from src.api.middleware.ingress_body_limit import (
    STAGING_REHEARSAL_TOKEN_BODY_BYTES,
    IngressBodyLimitMiddleware,
)
from src.api.routes import auth as auth_routes
from src.api.schemas.auth import LoginRequest, StagingRehearsalTokenRequest
from src.config.settings import settings
from src.models.user import UserRole


class AtomicRedis:
    """Small atomic eval stand-in for ordering and window boundary tests."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.calls: list[tuple[str, int]] = []
        self.lock = asyncio.Lock()
        self.delay = 0.0
        self.fail_accounts = False

    async def eval(self, script: str, key_count: int, key: str, ttl: int) -> int:
        assert key_count == 1
        assert "INCR" in script and "EXPIRE" in script
        self.calls.append((key, ttl))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail_accounts and ":account:" in key:
            raise RuntimeError("redis unavailable")
        async with self.lock:
            self.counts[key] += 1
            return self.counts[key]


def _build_issuer_app(redis: AtomicRedis) -> IngressBodyLimitMiddleware:
    app = FastAPI()
    app.state.redis = redis
    app.include_router(auth_routes.router, prefix="/v1/auth")
    app.add_middleware(AuthMiddleware)
    return IngressBodyLimitMiddleware(app)


def _install_user_db(
    monkeypatch: pytest.MonkeyPatch,
    *,
    user: Any | None,
    password_matches: bool = True,
) -> Any:
    class Result:
        def scalar_one_or_none(self) -> Any | None:
            return user

    session = SimpleNamespace(execute=AsyncMock(return_value=Result()))

    @asynccontextmanager
    async def session_scope():
        yield session

    monkeypatch.setattr(auth_routes, "get_db_session", session_scope)
    monkeypatch.setattr(
        auth_routes.pwd_context,
        "verify",
        lambda password, _password_hash: password_matches,
    )
    return session


def _user(*, role: UserRole = UserRole.super_admin, active: bool = True) -> Any:
    return SimpleNamespace(
        id=uuid4(),
        tenant_id=uuid4(),
        email="admin@example.com",
        password_hash="hashed-password",
        role=role,
        is_active=active,
        builder_id=None,
    )


def _json_body(email: str = "admin@example.com", password: str = "correct") -> bytes:
    return json.dumps({"email": email, "password": password}).encode()


@pytest.mark.asyncio
async def test_issuer_derives_exact_ten_minute_claims_and_preserves_login_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "environment", "staging")
    user = _user()
    session = _install_user_db(monkeypatch, user=user)
    redis = AtomicRedis()
    app = _build_issuer_app(redis)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        response = await client.post(
            STAGING_REHEARSAL_TOKEN_PATH,
            content=_json_body(" ADMIN@EXAMPLE.COM "),
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = jwt.decode(
        response.json()["token"], settings.secret_key, algorithms=["HS256"]
    )
    assert set(payload) == {
        "sub",
        "tenant_id",
        "role",
        "email",
        "iat",
        "exp",
        "jti",
        "token_use",
        "token_environment",
        "scope",
    }
    assert payload["sub"] == str(user.id)
    assert payload["tenant_id"] == str(user.tenant_id)
    assert payload["role"] == "super_admin"
    assert payload["email"] == user.email
    assert payload["token_use"] == STAGING_REHEARSAL_TOKEN_USE
    assert payload["token_environment"] == "staging"
    assert payload["scope"] == STAGING_REHEARSAL_TOKEN_SCOPE
    assert payload["exp"] - payload["iat"] == 600
    assert response.json()["expires_in"] == 600
    assert response.json()["token_type"] == "Bearer"
    session.execute.assert_awaited_once()

    @asynccontextmanager
    async def login_session_scope():
        login_session = SimpleNamespace(
            execute=AsyncMock(
                side_effect=[
                    SimpleNamespace(scalar_one_or_none=lambda: user),
                    SimpleNamespace(scalar_one_or_none=lambda: None),
                ]
            )
        )
        yield login_session

    monkeypatch.setattr(auth_routes, "get_db_session", login_session_scope)
    login = await auth_routes.login(LoginRequest(email=user.email, password="correct"))
    normal_payload = jwt.decode(login.token, settings.secret_key, algorithms=["HS256"])
    assert normal_payload["exp"] - normal_payload["iat"] == 24 * 60 * 60
    assert "token_use" not in normal_payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("active", "role", "password_matches", "expected"),
    [
        (True, UserRole.super_admin, False, 401),
        (False, UserRole.super_admin, True, 403),
        (True, UserRole.clinic_admin, True, 403),
        (True, UserRole.builder, True, 403),
    ],
)
async def test_issuer_denies_bad_password_inactive_and_non_admin(
    monkeypatch: pytest.MonkeyPatch,
    active: bool,
    role: UserRole,
    password_matches: bool,
    expected: int,
) -> None:
    monkeypatch.setattr(settings, "environment", "staging")
    _install_user_db(
        monkeypatch,
        user=_user(role=role, active=active),
        password_matches=password_matches,
    )
    app = _build_issuer_app(AtomicRedis())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        response = await client.post(
            STAGING_REHEARSAL_TOKEN_PATH,
            content=_json_body(),
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == expected
    assert response.headers["cache-control"] == "no-store"
    assert "token" not in response.text


@pytest.mark.asyncio
async def test_issuer_requires_staging_and_rejects_bearer_or_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_user_db(monkeypatch, user=_user())
    redis = AtomicRedis()
    app = _build_issuer_app(redis)
    monkeypatch.setattr(settings, "environment", "production")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        outside_staging = await client.post(
            STAGING_REHEARSAL_TOKEN_PATH,
            content=_json_body(),
            headers={"Content-Type": "application/json"},
        )
        monkeypatch.setattr(settings, "environment", "staging")
        with_credentials = await client.post(
            STAGING_REHEARSAL_TOKEN_PATH,
            content=_json_body(),
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer unrelated",
            },
        )
    assert outside_staging.status_code == 404
    assert outside_staging.headers["cache-control"] == "no-store"
    assert with_credentials.status_code == 400
    assert with_credentials.headers["cache-control"] == "no-store"
    assert len(redis.calls) == 2
    assert all(":account:" in key or ":global:" in key for key, _ in redis.calls)


@pytest.mark.asyncio
async def test_malformed_schema_consumes_global_only_before_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "environment", "staging")
    session = _install_user_db(monkeypatch, user=_user())
    redis = AtomicRedis()
    app = _build_issuer_app(redis)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        response = await client.post(
            STAGING_REHEARSAL_TOKEN_PATH,
            content=b'{"email":"admin@example.com","password":"x","role":"super_admin"}',
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    assert len(redis.calls) == 1
    assert ":global:" in redis.calls[0][0]
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_valid_wrong_password_consumes_account_bucket_and_global_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "environment", "staging")
    session = _install_user_db(monkeypatch, user=_user(), password_matches=False)
    redis = AtomicRedis()
    app = _build_issuer_app(redis)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        responses = [
            await client.post(
                STAGING_REHEARSAL_TOKEN_PATH,
                content=_json_body(),
                headers={"Content-Type": "application/json"},
            )
            for _ in range(6)
        ]
    assert [response.status_code for response in responses] == [401] * 5 + [429]
    assert all(
        response.headers["cache-control"] == "no-store" for response in responses
    )
    assert sum(":global:" in key for key, _ttl in redis.calls) == 6
    account_keys = [key for key, _ttl in redis.calls if ":account:" in key]
    assert len(account_keys) == 6
    assert all("admin@example.com" not in key for key in account_keys)
    assert all(len(key.split(":")[-2]) == 64 for key in account_keys)
    assert session.execute.await_count == 5


@pytest.mark.asyncio
async def test_global_limit_precedes_json_parse_and_account_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "environment", "staging")
    session = _install_user_db(monkeypatch, user=_user())
    redis = AtomicRedis()
    global_key, _ttl = AuthMiddleware._fixed_window_key("global", int(time.time()))
    redis.counts[global_key] = STAGING_REHEARSAL_GLOBAL_LIMIT
    app = _build_issuer_app(redis)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        response = await client.post(
            STAGING_REHEARSAL_TOKEN_PATH,
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 429
    assert response.headers["cache-control"] == "no-store"
    assert len(redis.calls) == 1
    session.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_global_concurrent_admission_is_atomic_across_distinct_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "environment", "staging")
    _install_user_db(monkeypatch, user=None)
    redis = AtomicRedis()
    app = _build_issuer_app(redis)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        responses = await asyncio.gather(
            *(
                client.post(
                    STAGING_REHEARSAL_TOKEN_PATH,
                    content=_json_body(f"person-{index}@example.com"),
                    headers={"Content-Type": "application/json"},
                )
                for index in range(32)
            )
        )
    assert Counter(response.status_code for response in responses) == {
        401: STAGING_REHEARSAL_GLOBAL_LIMIT,
        429: 32 - STAGING_REHEARSAL_GLOBAL_LIMIT,
    }
    assert sum(":global:" in key for key, _ttl in redis.calls) == 32
    assert sum(":account:" in key for key, _ttl in redis.calls) == 30


@pytest.mark.asyncio
async def test_redis_failure_or_timeout_fails_closed_and_preserves_global_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "environment", "staging")
    _install_user_db(monkeypatch, user=_user())
    redis = AtomicRedis()
    redis.fail_accounts = True
    app = _build_issuer_app(redis)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        account_failure = await client.post(
            STAGING_REHEARSAL_TOKEN_PATH,
            content=_json_body(),
            headers={"Content-Type": "application/json"},
        )
    assert account_failure.status_code == 503
    assert ":global:" in redis.calls[0][0]
    assert ":account:" in redis.calls[1][0]

    timeout_redis = AtomicRedis()
    timeout_redis.delay = STAGING_REHEARSAL_REDIS_TIMEOUT_SECONDS * 2
    app = _build_issuer_app(timeout_redis)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        timed_out = await client.post(
            STAGING_REHEARSAL_TOKEN_PATH,
            content=_json_body(),
            headers={"Content-Type": "application/json"},
        )
    assert timed_out.status_code == 503
    assert timed_out.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_outer_ingress_rejects_oversize_before_redis_or_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "environment", "staging")
    redis = AtomicRedis()
    _install_user_db(monkeypatch, user=_user())
    app = _build_issuer_app(redis)
    body = b"x" * (STAGING_REHEARSAL_TOKEN_BODY_BYTES + 1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://testserver"
    ) as client:
        response = await client.post(
            STAGING_REHEARSAL_TOKEN_PATH,
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert response.headers["cache-control"] == "no-store"
    assert not redis.calls


@pytest.mark.asyncio
async def test_outer_ingress_body_timeout_rejects_before_redis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api.middleware import ingress_body_limit

    monkeypatch.setattr(
        ingress_body_limit, "STAGING_REHEARSAL_TOKEN_READ_TIMEOUT_SECONDS", 0.01
    )
    downstream_calls: list[bool] = []
    sent: list[dict[str, Any]] = []

    async def downstream(_scope: Any, _receive: Any, _send: Any) -> None:
        downstream_calls.append(True)

    async def receive() -> dict[str, Any]:
        await asyncio.sleep(0.05)
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": STAGING_REHEARSAL_TOKEN_PATH,
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
        "server": ("testserver", 443),
        "client": ("test", 1),
    }
    await IngressBodyLimitMiddleware(downstream)(scope, receive, send)
    assert sent[0]["status"] == 408
    assert (b"cache-control", b"no-store") in sent[0]["headers"]
    assert not downstream_calls


def test_staging_schema_is_strict_and_normalizes_email() -> None:
    parsed = StagingRehearsalTokenRequest.model_validate(
        {"email": " ADMIN@EXAMPLE.COM ", "password": " secret "}
    )
    assert str(parsed.email) == "admin@example.com"
    assert parsed.password == " secret "
    for value in (
        {"email": "admin@example.com", "password": "x", "role": "super_admin"},
        {"email": "admin@example.com", "password": 1},
        {"email": "invalid", "password": "x"},
    ):
        with pytest.raises(Exception):
            StagingRehearsalTokenRequest.model_validate(value)
