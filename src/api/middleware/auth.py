"""
NeuroStack API - Authentication Middleware
JWT and API Key based authentication for tenant identification.
"""

import asyncio
import hashlib
import hmac
import json
import time
from typing import Any, Literal, Optional, cast
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Receive, Scope, Send

from src.api.routing_paths import decision_path
from src.api.schemas.auth import StagingRehearsalTokenRequest
from src.api.spa import is_public_spa_path
from src.config.logging import get_logger
from src.config.settings import settings

logger = get_logger(__name__)

STAGING_REHEARSAL_TOKEN_PATH = "/v1/auth/staging-rehearsal-token"
STAGING_REHEARSAL_TOKEN_USE = "asaas_staging_rehearsal"
STAGING_REHEARSAL_TOKEN_ENVIRONMENT = "staging"
STAGING_REHEARSAL_TOKEN_SCOPE = "billing.producer.staging.rehearsal"
STAGING_REHEARSAL_TOKEN_TTL_SECONDS = 600
STAGING_REHEARSAL_GLOBAL_LIMIT = 30
STAGING_REHEARSAL_ACCOUNT_LIMIT = 5
STAGING_REHEARSAL_WINDOW_SECONDS = 900
STAGING_REHEARSAL_REDIS_TIMEOUT_SECONDS = 0.1
STAGING_REHEARSAL_READBACK_PATH = "/v1/admin/billing/rollout/readback"
STAGING_PRODUCER_RECEIPT_PATH = "/v1/admin/billing/producer-receipts"
PUBLIC_PRODUCER_BUILD_TRUST_PATH = "/v1/billing/producer-build-trust"
PRODUCER_BUILD_TRUST_READBACK_PATH = "/v1/admin/billing/producer-build-trust"
# Exact GET-only public trust readbacks (served with Cache-Control: no-store).
PUBLIC_TRUST_READBACK_PATHS = frozenset(
    {PUBLIC_PRODUCER_BUILD_TRUST_PATH, PRODUCER_BUILD_TRUST_READBACK_PATH}
)
# Exact POST-only public lead-capture form.
PUBLIC_DEMO_LEADS_PATH = "/v1/demo-leads"
# ASAAS receiver: public here because the ingress boundary authenticates it.
PUBLIC_ASAAS_WEBHOOK_PATH = "/v1/billing/webhook/asaas"
_STAGING_REHEARSAL_PURPOSE_CLAIMS = frozenset(
    {"token_use", "token_environment", "scope"}
)
_STAGING_REHEARSAL_CLAIMS = frozenset(
    {
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
)
_STAGING_REHEARSAL_TARGETS = frozenset(
    {
        ("POST", "/v1/admin/billing/producer-grants/assignments"),
        ("POST", "/v1/admin/billing/producer-grants/authorizations"),
        ("POST", "/v1/admin/billing/producer-grants/revocations"),
        ("POST", "/v1/admin/billing/producer-build-attestations"),
        ("GET", STAGING_REHEARSAL_READBACK_PATH),
        ("POST", f"{STAGING_PRODUCER_RECEIPT_PATH}/preflight"),
        ("POST", f"{STAGING_PRODUCER_RECEIPT_PATH}/create"),
        ("POST", f"{STAGING_PRODUCER_RECEIPT_PATH}/verify"),
    }
)
# Exported for the route-table invariant tests (single source).
STAGING_REHEARSAL_TARGETS = _STAGING_REHEARSAL_TARGETS
_STAGING_REHEARSAL_RATE_LIMIT_LUA = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""

# ============================================
# API Key Authentication
# ============================================


class AuthenticationError(Exception):
    """Custom exception for authentication errors."""

    def __init__(self, message: str, error_code: str = "unauthorized") -> None:
        self.message = message
        self.error_code = error_code
        super().__init__(message)


class TenantContext:
    """Context holder for authenticated tenant information."""

    def __init__(
        self,
        tenant_id: UUID,
        api_key: str = "",
        rate_limit: Optional[int] = None,
        is_active: bool = True,
        user_id: Optional[UUID] = None,
        role: Optional[str] = None,
        builder_id: Optional[UUID] = None,
        authentication_mode: Literal["jwt", "api_key", "meta_hmac"] | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.api_key = api_key
        self.rate_limit = rate_limit or settings.rate_limit_requests_per_minute
        self.is_active = is_active
        self.user_id = user_id
        self.role = role
        self.builder_id = builder_id
        self.authentication_mode = authentication_mode or (
            "api_key" if api_key else "jwt"
        )


# ============================================
# API Key Store (Placeholder)
# ============================================


def _dev_api_keys_allowed() -> bool:
    """Development API keys never authenticate in staging or production."""
    return settings.environment in ("development", "test")


class APIKeyStore:
    """
    API Key store for validating and retrieving tenant information.

    No tenant API key is issued yet (tenant creation stores a hash of a
    discarded random value), so only the development keys below authenticate,
    and only in development and test. Any other environment rejects every key
    without a database read or a bcrypt check, so an unauthenticated request
    cannot spend server time on key verification.
    """

    # Development API keys (development and test only)
    _dev_keys: dict[str, dict[str, Any]] = {
        "ns_dev_test_key_12345": {
            "tenant_id": UUID("a0000000-0000-0000-0000-000000000001"),
            "rate_limit": 100,
            "is_active": True,
        },
        "ns_dev_builder_key_67890": {
            "tenant_id": UUID("a0000000-0000-0000-0000-000000000002"),
            "rate_limit": 60,
            "is_active": True,
        },
        "test-api-key": {
            "tenant_id": UUID("a0000000-0000-0000-0000-000000000001"),
            "rate_limit": 100,
            "is_active": True,
        },
    }

    @classmethod
    async def validate_api_key(cls, api_key: str) -> Optional[TenantContext]:
        """
        Validate API key and return tenant context.

        Args:
            api_key: The API key to validate

        Returns:
            TenantContext for a development key in development or test,
            None otherwise
        """
        if not api_key or not _dev_api_keys_allowed():
            return None

        return cls._fallback_to_dev_keys(api_key)

    @classmethod
    def _fallback_to_dev_keys(cls, api_key: str) -> Optional[TenantContext]:
        """
        Resolve a development key to its fixed development tenant.

        Only used in development and test.
        """
        # Check known development keys
        if api_key in cls._dev_keys:
            key_data = cls._dev_keys[api_key]
            return TenantContext(
                tenant_id=key_data["tenant_id"],
                api_key=api_key,
                rate_limit=key_data["rate_limit"],
                is_active=key_data["is_active"],
            )

        # Development/test mode: accept known prefixes used in tests and builders
        if api_key.startswith("ns_dev_"):
            return TenantContext(
                tenant_id=UUID("a0000000-0000-0000-0000-000000000001"),
                api_key=api_key,
                rate_limit=settings.rate_limit_requests_per_minute,
                is_active=True,
            )

        # Accept test-api-key variants used in tests (e.g. test-api-key-12345)
        if api_key.startswith("test-api-key"):
            return TenantContext(
                tenant_id=UUID("a0000000-0000-0000-0000-000000000001"),
                api_key=api_key,
                rate_limit=settings.rate_limit_requests_per_minute,
                is_active=True,
            )

        return None


# ============================================
# Authentication Middleware
# ============================================


class AuthMiddleware:
    """
    Pure ASGI middleware for authentication.

    Uses the raw ASGI interface instead of BaseHTTPMiddleware so that
    FastAPI async-generator dependencies (like get_db_session_dependency)
    can properly execute their post-yield cleanup (commit / rollback).
    BaseHTTPMiddleware is known to break this lifecycle.
    """

    # Paths that don't require authentication
    PUBLIC_PATHS: set[str] = {
        "/",
        "/health",
        "/ready",
        "/live",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/v1/openapi.json",
        "/metrics",
        "/v1/auth/login",
    }
    # Every path starting with one of these is public (plain ``startswith``).
    PUBLIC_PATH_PREFIXES: tuple[str, ...] = (
        "/docs",
        "/redoc",
        "/metrics",
        "/v1/webhook/",
        "/privacidade/",
        "/api/public/privacidade/",
    )

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive, send)
        # Every path decision below is taken on the path the router matches
        # (src/api/routing_paths.py), never on request.url.path, which drops
        # tab/CR/LF, cuts at a decoded "?"/"#" and keeps root_path.  Logs
        # keep request.url.path.
        path = decision_path(scope)

        if request.method == "POST" and path == STAGING_REHEARSAL_TOKEN_PATH:
            if settings.environment != STAGING_REHEARSAL_TOKEN_ENVIRONMENT:
                await self._reject_staging_issuer(scope, receive, send, 404)
                return
            admitted = await self._admit_staging_issuer(scope, request)
            if admitted is not None:
                status_code, error = admitted
                await self._reject_staging_issuer(
                    scope, receive, send, status_code, error
                )
                return
            await self.app(scope, receive, self._no_store_send(send))
            return

        # Exact GET-only trust readbacks are public; other methods and adjacent
        # paths continue through normal authentication.
        if request.method == "GET" and path in PUBLIC_TRUST_READBACK_PATHS:
            await self.app(scope, receive, self._no_store_send(send))
            return

        # Exact GET/HEAD-only frontend paths, decided on the router's own path
        # string (src/api/spa.py); everything else keeps normal authentication.
        if request.method in ("GET", "HEAD") and is_public_spa_path(scope):
            await self.app(scope, receive, send)
            return

        is_rehearsal_target = (request.method, path) in _STAGING_REHEARSAL_TARGETS

        # Skip auth for public paths and OPTIONS
        if self._is_public_path(path) or request.method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        if is_rehearsal_target and not request.headers.get(
            "Authorization", ""
        ).startswith("Bearer "):
            await self._reject_staging_issuer(scope, receive, send, 401, "unauthorized")
            return

        # POST /v1/demo-leads is public (lead capture form)
        if path == PUBLIC_DEMO_LEADS_PATH and request.method == "POST":
            await self.app(scope, receive, send)
            return

        # --- Try JWT (Bearer) first, then fall back to API key ---
        tenant_context: Optional[TenantContext] = None
        auth_header = request.headers.get("Authorization", "")

        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            try:
                payload = jwt.decode(
                    token,
                    settings.secret_key,
                    algorithms=["HS256"],
                    options={"require": ["sub", "tenant_id", "exp"]},
                )
                has_purpose = bool(
                    _STAGING_REHEARSAL_PURPOSE_CLAIMS.intersection(payload)
                )
                if has_purpose:
                    if not self._valid_staging_rehearsal_payload(
                        payload, request.method, path
                    ):
                        await self._reject_staging_issuer(
                            scope, receive, send, 401, "unauthorized"
                        )
                        return
                elif is_rehearsal_target:
                    await self._reject_staging_issuer(
                        scope, receive, send, 401, "unauthorized"
                    )
                    return
                tenant_context = TenantContext(
                    tenant_id=UUID(payload["tenant_id"]),
                    user_id=UUID(payload["sub"]),
                    role=payload.get("role"),
                    builder_id=(
                        UUID(payload["builder_id"])
                        if payload.get("builder_id") is not None
                        else None
                    ),
                    is_active=True,
                    authentication_mode="jwt",
                )
            except jwt.ExpiredSignatureError:
                response = JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={
                        "error": "unauthorized",
                        "message": "Token has expired",
                        "documentation_url": "https://docs.neurostack.ai/auth",
                    },
                )
                await response(scope, receive, send)
                return
            except (jwt.InvalidTokenError, KeyError, ValueError):
                response = JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={
                        "error": "unauthorized",
                        "message": "Invalid token",
                        "documentation_url": "https://docs.neurostack.ai/auth",
                    },
                )
                await response(scope, receive, send)
                return

        # Fall back to API key if no Bearer token
        if tenant_context is None:
            api_key = request.headers.get(settings.api_key_header)

            if not api_key:
                logger.warning(
                    "Missing credentials",
                    path=request.url.path,
                    method=request.method,
                    client_ip=self._get_client_ip(request),
                )
                response = JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={
                        "error": "unauthorized",
                        "message": f"Missing Authorization or {settings.api_key_header} header",
                        "documentation_url": "https://docs.neurostack.ai/auth",
                    },
                )
                await response(scope, receive, send)
                return

            # Validate API key
            tenant_context = await APIKeyStore.validate_api_key(api_key)

            if tenant_context is None:
                logger.warning(
                    "Invalid API key",
                    path=request.url.path,
                    method=request.method,
                    client_ip=self._get_client_ip(request),
                    api_key_prefix=api_key[:8] + "..." if len(api_key) > 8 else "***",
                )
                response = JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={
                        "error": "unauthorized",
                        "message": "Invalid API key",
                        "documentation_url": "https://docs.neurostack.ai/auth",
                    },
                )
                await response(scope, receive, send)
                return

        # Check if tenant is active
        if not tenant_context.is_active:
            logger.warning(
                "Inactive tenant attempted access",
                tenant_id=str(tenant_context.tenant_id),
                path=request.url.path,
            )
            response = JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "error": "forbidden",
                    "message": "Tenant account is inactive",
                },
            )
            await response(scope, receive, send)
            return

        # Attach tenant context to request state
        request.state.tenant = tenant_context
        if is_rehearsal_target:
            request.state.staging_rehearsal_token = True
        tenant_context.authentication_mode = (
            "jwt" if auth_header.startswith("Bearer ") else "api_key"
        )
        request.state.api_key = tenant_context.api_key

        # Log successful authentication
        logger.debug(
            "Request authenticated",
            tenant_id=str(tenant_context.tenant_id),
            path=request.url.path,
        )

        # Continue processing — pure ASGI passthrough
        await self.app(scope, receive, send)

    async def _admit_staging_issuer(
        self, scope: Scope, request: Request
    ) -> tuple[int, str] | None:
        """Reserve both independent fixed-window limits before auth or DB work."""
        now = int(time.time())
        global_key, global_ttl = self._fixed_window_key("global", now)
        global_allowed = await self._reserve_staging_slot(scope, global_key, global_ttl)
        if global_allowed is None:
            return status.HTTP_503_SERVICE_UNAVAILABLE, "issuer_unavailable"
        if not global_allowed:
            return status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited"

        state = cast(dict[str, Any], scope.get("state", {}))
        body = state.get("bounded_ingress_body")
        if not isinstance(body, bytes):
            return status.HTTP_503_SERVICE_UNAVAILABLE, "issuer_unavailable"

        content_type = request.headers.get("content-type", "")
        if content_type.split(";", 1)[0].strip().lower() != "application/json":
            return status.HTTP_400_BAD_REQUEST, "invalid_request"

        try:
            decoded = json.loads(body, object_pairs_hook=self._reject_duplicate_keys)
            credentials = StagingRehearsalTokenRequest.model_validate(decoded)
        except (json.JSONDecodeError, TypeError, ValueError, ValidationError):
            return status.HTTP_400_BAD_REQUEST, "invalid_request"

        normalized_email = str(credentials.email)
        account_digest = hmac.new(
            settings.secret_key.encode("utf-8"),
            normalized_email.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        account_key, account_ttl = self._fixed_window_key(
            f"account:{account_digest}", int(time.time())
        )
        account_allowed = await self._reserve_staging_slot(
            scope, account_key, account_ttl
        )
        if account_allowed is None:
            return status.HTTP_503_SERVICE_UNAVAILABLE, "issuer_unavailable"
        if not account_allowed:
            return status.HTTP_429_TOO_MANY_REQUESTS, "rate_limited"

        if request.headers.get("Authorization") or request.headers.get(
            settings.api_key_header
        ):
            return status.HTTP_400_BAD_REQUEST, "invalid_request"

        state["staging_rehearsal_issuer_admitted"] = True
        return None

    @staticmethod
    def _fixed_window_key(bucket: str, now: int) -> tuple[str, int]:
        window = STAGING_REHEARSAL_WINDOW_SECONDS
        bucket_number = now // window
        ttl = window - (now % window)
        return f"auth:staging-rehearsal:{bucket}:{bucket_number}", max(ttl, 1)

    @staticmethod
    async def _reserve_staging_slot(scope: Scope, key: str, ttl: int) -> bool | None:
        app = scope.get("app")
        state = getattr(app, "state", None)
        redis_client = getattr(state, "redis", None)
        if redis_client is None:
            return None
        try:
            async with asyncio.timeout(STAGING_REHEARSAL_REDIS_TIMEOUT_SECONDS):
                count = await redis_client.eval(
                    _STAGING_REHEARSAL_RATE_LIMIT_LUA,
                    1,
                    key,
                    ttl,
                )
            limit = (
                STAGING_REHEARSAL_GLOBAL_LIMIT
                if ":global:" in key
                else STAGING_REHEARSAL_ACCOUNT_LIMIT
            )
            return int(count) <= limit
        except Exception:
            return None

    @staticmethod
    def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    @staticmethod
    def _valid_staging_rehearsal_payload(
        payload: dict[str, Any], method: str, path: str
    ) -> bool:
        if set(payload) != _STAGING_REHEARSAL_CLAIMS:
            return False
        if (
            settings.environment != STAGING_REHEARSAL_TOKEN_ENVIRONMENT
            or (method, path) not in _STAGING_REHEARSAL_TARGETS
            or payload.get("token_use") != STAGING_REHEARSAL_TOKEN_USE
            or payload.get("token_environment") != STAGING_REHEARSAL_TOKEN_ENVIRONMENT
            or payload.get("scope") != STAGING_REHEARSAL_TOKEN_SCOPE
            or payload.get("role") != "super_admin"
            or not isinstance(payload.get("email"), str)
            or not isinstance(payload.get("jti"), str)
            or not payload.get("jti")
        ):
            return False
        issued_at = payload.get("iat")
        expires_at = payload.get("exp")
        if type(issued_at) is not int or type(expires_at) is not int:
            return False
        lifetime = expires_at - issued_at
        return 0 < lifetime <= STAGING_REHEARSAL_TOKEN_TTL_SECONDS

    @staticmethod
    def _no_store_send(send: Send) -> Send:
        async def send_no_store(message: Any) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                headers = [
                    (name, value)
                    for name, value in headers
                    if name.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        return send_no_store

    @staticmethod
    async def _reject_staging_issuer(
        scope: Scope,
        receive: Receive,
        send: Send,
        status_code: int,
        error: str = "invalid_request",
    ) -> None:
        await JSONResponse(
            status_code=status_code,
            content={"error": error},
            headers={"Cache-Control": "no-store"},
        )(scope, receive, send)

    def _is_public_path(self, path: str) -> bool:
        """Check if path is public (no auth required)."""
        if path in self.PUBLIC_PATHS:
            return True
        if path.startswith(self.PUBLIC_PATH_PREFIXES):
            return True
        # ASAAS receiver: exact match only (no prefix, no trailing slash).  It
        # authenticates its own token at the ingress boundary.
        return path == PUBLIC_ASAAS_WEBHOOK_PATH

    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP from request, handling proxies."""
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip
        if request.client:
            return request.client.host
        return "unknown"


# ============================================
# Dependency Injection
# ============================================


def get_tenant_context(request: Request) -> TenantContext:
    """
    FastAPI dependency for getting authenticated tenant context.

    Use this in route handlers to access tenant information:

        @router.get("/my-endpoint")
        async def my_endpoint(tenant: TenantContext = Depends(get_tenant_context)):
            # Access tenant information
            tenant_id = tenant.tenant_id
            return {"tenant_id": str(tenant_id)}
    """
    if not hasattr(request.state, "tenant"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    return cast(TenantContext, request.state.tenant)


def require_credentials_jwt(request: Request) -> TenantContext:
    """Require a JWT role authorized to read or mutate provider credentials."""

    tenant = get_tenant_context(request)
    if tenant.api_key or tenant.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "credentials_jwt_required"},
        )
    if tenant.role not in {"clinic_admin", "builder", "super_admin"}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "credentials_role_forbidden"},
        )
    return tenant


def require_super_admin(request: Request) -> TenantContext:
    """
    FastAPI dependency that enforces super_admin role.
    Raises 403 if the authenticated user is not a super_admin.
    """
    tenant = get_tenant_context(request)
    if tenant.role != "super_admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Super admin access required",
        )
    return tenant


def require_super_admin_jwt(request: Request) -> TenantContext:
    """Require a JWT-authenticated super administrator for billing operations.

    API keys must never authorize cross-builder billing migration, rollout, or
    receipt consumption.  This uses the already verified request context and
    never re-emits the raw bearer token.
    """

    tenant = require_super_admin(request)
    if tenant.authentication_mode != "jwt" or tenant.api_key or tenant.user_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "billing_super_admin_jwt_required"},
        )
    return tenant


def require_builder(request: Request) -> UUID:
    """
    FastAPI dependency that enforces builder role and extracts builder_id from JWT.
    Raises 403 if the authenticated user does not have a builder_id claim.
    Returns the builder_id UUID for use in route handlers.
    """
    tenant = get_tenant_context(request)

    # Extract builder_id from JWT claims (stored in request state during middleware)
    # The middleware decodes the JWT and puts role in TenantContext, but builder_id
    # is an extra claim. We re-decode from the Authorization header.
    auth_header = request.headers.get("Authorization", "")
    builder_id_str: Optional[str] = None
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        try:
            payload = jwt.decode(
                token,
                settings.secret_key,
                algorithms=["HS256"],
                options={"require": ["sub", "tenant_id", "exp"]},
            )
            builder_id_str = payload.get("builder_id")
        except (jwt.InvalidTokenError, KeyError, ValueError):
            pass

    if not builder_id_str or tenant.role != "builder":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "detail": "Builder profile not found. Contact admin to enable builder access.",
                "error_code": "BUILDER_NOT_CONFIGURED",
            },
        )

    return UUID(builder_id_str)


async def require_builder_clinic_access(
    tenant_id: UUID,
    builder_id: UUID,
    db: AsyncSession,
) -> UUID:
    """
    Validate that a builder owns the requested clinic tenant.
    Returns tenant_id on success, raises 403 on failure.
    Never returns 404 to avoid leaking clinic existence.
    """
    from src.models.tenant import Tenant

    result = await db.execute(
        select(Tenant.id).where(
            Tenant.id == tenant_id,
            Tenant.builder_id == builder_id,
        )
    )
    if not result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Builder does not manage this clinic",
        )
    return tenant_id


_OPERATIONAL_JWT_ROLES = {"clinic_admin", "builder", "super_admin"}


def _tenant_resolution_error(code: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail={"code": code},
    )


async def resolve_effective_tenant(
    request: Request,
    tenant: TenantContext,
    requested_tenant_id: Optional[UUID],
    db: AsyncSession,
) -> UUID:
    """Resolve one authorized tenant before any tenant-scoped operation.

    API keys are tenant-scoped. JWT callers are deny-by-default; clinic
    administrators remain scoped to their token, builders must prove ownership
    for clinic overrides, and super administrators may select a clinic.
    """
    if tenant.api_key:
        if requested_tenant_id is None or requested_tenant_id == tenant.tenant_id:
            return tenant.tenant_id
        raise _tenant_resolution_error("tenant_override_forbidden")

    if tenant.role not in _OPERATIONAL_JWT_ROLES:
        raise _tenant_resolution_error("caller_role_forbidden")

    if requested_tenant_id is None or requested_tenant_id == tenant.tenant_id:
        return tenant.tenant_id

    if tenant.role == "clinic_admin":
        raise _tenant_resolution_error("tenant_override_forbidden")

    if tenant.role == "super_admin":
        return requested_tenant_id

    if tenant.builder_id is None:
        raise _tenant_resolution_error("tenant_override_forbidden")

    logger.info(
        "builder_clinic_override",
        extra={
            "builder_id": str(tenant.builder_id),
            "tenant_id": str(requested_tenant_id),
            "endpoint": request.url.path,
        },
    )
    try:
        return await require_builder_clinic_access(
            requested_tenant_id,
            tenant.builder_id,
            db,
        )
    except HTTPException as error:
        if error.status_code == status.HTTP_403_FORBIDDEN:
            raise _tenant_resolution_error("tenant_override_forbidden") from error
        raise


async def resolve_builder_tenant_override(
    request: Request,
    override_tenant_id: Optional[UUID] = Query(None, alias="tenant_id"),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(lambda: None),  # placeholder — wired at endpoint level
) -> UUID:
    """
    Resolve effective tenant_id for endpoints supporting builder override.
    - Builder + tenant_id param: validate ownership, audit log, return clinic tenant_id
    - Non-builder + tenant_id param: return 400 (fail loud)
    - No tenant_id param: return JWT tenant_id
    """
    if override_tenant_id is None:
        return tenant.tenant_id

    if tenant.role != "builder":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"tenant_id param not allowed for role '{tenant.role}'",
        )

    # Extract builder_id from JWT
    auth_header = request.headers.get("Authorization", "")
    builder_id_str: Optional[str] = None
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        try:
            payload = jwt.decode(
                token,
                settings.secret_key,
                algorithms=["HS256"],
                options={"require": ["sub", "tenant_id", "exp"]},
            )
            builder_id_str = payload.get("builder_id")
        except (jwt.InvalidTokenError, KeyError, ValueError):
            pass

    if not builder_id_str:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Builder ID not found in token",
        )

    builder_id = UUID(builder_id_str)

    # Audit log before query
    logger.info(
        "builder_clinic_override",
        extra={
            "builder_id": str(builder_id),
            "tenant_id": str(override_tenant_id),
            "endpoint": request.url.path,
        },
    )

    # Validate ownership
    return await require_builder_clinic_access(override_tenant_id, builder_id, db)


def get_optional_tenant_context(request: Request) -> Optional[TenantContext]:
    """
    FastAPI dependency for optionally getting tenant context.

    Returns None if not authenticated (useful for public endpoints
    that behave differently for authenticated users).
    """
    return getattr(request.state, "tenant", None)
