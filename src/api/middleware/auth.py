"""
NeuroStack API - Authentication Middleware
JWT and API Key based authentication for tenant identification.
"""

from typing import Any, Literal, Optional, cast
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import ASGIApp, Receive, Scope, Send

from src.config.logging import get_logger
from src.config.settings import settings
from src.repositories import TenantRepository, get_db_session

logger = get_logger(__name__)

# Password hashing context for API key validation
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


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

    Validates API keys against the database using TenantRepository.
    Falls back to development mode behavior if database validation fails.
    """

    # Development API keys (fallback for development/testing)
    # These are only used if database lookup fails in development mode
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

        First attempts to validate against database by checking all tenants
        and comparing API key hash. Falls back to development keys if
        database validation fails in development mode.

        Args:
            api_key: The API key to validate

        Returns:
            TenantContext if valid, None otherwise
        """
        if not api_key:
            return None

        try:
            # Try to validate against database
            async with get_db_session() as session:
                repo = TenantRepository(session)

                # Get all active tenants
                # NOTE: bcrypt uses salt, so we can't do direct hash lookup.
                # TODO: Optimize with Redis cache for validated API keys in production
                # Cache key: f"api_key:{api_key}" -> tenant_id with TTL
                tenants = await repo.find_active(limit=1000)

                # Check each tenant's API key hash
                for tenant in tenants:
                    try:
                        # Verify API key against stored hash using bcrypt
                        if pwd_context.verify(api_key, tenant.api_key_hash):
                            # API key matches this tenant
                            config = tenant.get_config()
                            logger.debug(
                                "API key validated successfully",
                                tenant_id=str(tenant.id),
                            )
                            return TenantContext(
                                tenant_id=tenant.id,
                                api_key=api_key,
                                rate_limit=config.rate_limit,
                                is_active=tenant.is_active,
                            )
                    except ValueError as e:
                        # Skip invalid hashes (shouldn't happen in production)
                        logger.debug(
                            "Failed to verify API key hash",
                            tenant_id=str(tenant.id),
                            error=str(e),
                        )
                        continue
                # No commit needed - this is a read-only operation

        except SQLAlchemyError as e:
            # Database error - fall back to development mode if enabled
            logger.warning(
                "Database validation failed, falling back to dev keys",
                error=str(e),
                error_type=type(e).__name__,
            )

            # Allow fallback only in development and test
            if _dev_api_keys_allowed():
                return cls._fallback_to_dev_keys(api_key)

            # In staging and production, fail securely
            return None
        except Exception as e:
            # Unexpected error during API key validation
            logger.error(
                "Unexpected error during API key validation",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )

            # Allow fallback only in development and test
            if _dev_api_keys_allowed():
                return cls._fallback_to_dev_keys(api_key)

            # In staging and production, fail securely
            return None

        # If we get here, API key didn't match any tenant
        # Try fallback only in development and test
        if _dev_api_keys_allowed():
            return cls._fallback_to_dev_keys(api_key)

        return None

    @classmethod
    def _fallback_to_dev_keys(cls, api_key: str) -> Optional[TenantContext]:
        """
        Fallback to development keys if database validation fails.

        Only used in development mode.
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
        "/v1/auth",
        "/v1/auth/login",
    }

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive, send)

        # Skip auth for public paths and OPTIONS
        if self._is_public_path(request.url.path) or request.method == "OPTIONS":
            await self.app(scope, receive, send)
            return

        # POST /v1/demo-leads is public (lead capture form)
        if request.url.path == "/v1/demo-leads" and request.method == "POST":
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

    def _is_public_path(self, path: str) -> bool:
        """Check if path is public (no auth required)."""
        if path in self.PUBLIC_PATHS:
            return True
        if path.startswith("/docs") or path.startswith("/redoc"):
            return True
        if path.startswith("/metrics"):
            return True
        if path.startswith("/v1/webhook/"):
            return True
        if path == "/v1/billing/webhook":
            return True
        if path.startswith("/privacidade/"):
            return True
        if path.startswith("/api/public/privacidade/"):
            return True
        return False

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
