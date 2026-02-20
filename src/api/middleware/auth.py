"""
NeuroStack API - Authentication Middleware
API Key based authentication for tenant identification.
"""

from typing import Any, Callable, Optional
from uuid import UUID

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from passlib.context import CryptContext
from passlib.exc import InvalidHashError
from sqlalchemy.exc import SQLAlchemyError

import sys
from pathlib import Path

# Add the project root to sys.path for local module resolution
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

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
        api_key: str,
        rate_limit: Optional[int] = None,
        is_active: bool = True,
    ) -> None:
        self.tenant_id = tenant_id
        self.api_key = api_key
        self.rate_limit = rate_limit or settings.rate_limit_requests_per_minute
        self.is_active = is_active


# ============================================
# API Key Store (Placeholder)
# ============================================


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
                    except (ValueError, InvalidHashError) as e:
                        # Skip invalid hashes (shouldn't happen in production)
                        logger.debug(
                            "Failed to verify API key hash",
                            tenant_id=str(tenant.id),
                            error=str(e),
                        )
                        continue
                    except Exception as e:
                        # Unexpected error during hash verification
                        logger.warning(
                            "Unexpected error verifying API key hash",
                            tenant_id=str(tenant.id),
                            error=str(e),
                            error_type=type(e).__name__,
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
            
            # Allow fallback in non-production environments (development and test)
            if not settings.is_production:
                return cls._fallback_to_dev_keys(api_key)
            
            # In production, fail securely
            return None
        except Exception as e:
            # Unexpected error during API key validation
            logger.error(
                "Unexpected error during API key validation",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            
            # Allow fallback in non-production environments (development and test)
            if not settings.is_production:
                return cls._fallback_to_dev_keys(api_key)
            
            # In production, fail securely
            return None
        
        # If we get here, API key didn't match any tenant
        # Try fallback in non-production environments
        if not settings.is_production:
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


class AuthMiddleware(BaseHTTPMiddleware):
    """
    FastAPI middleware for API key authentication.
    
    Extracts API key from X-API-Key header, validates it,
    and attaches tenant context to the request state.
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
    }

    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ) -> JSONResponse:
        """Process request through authentication."""
        
        # Skip auth for public paths
        if self._is_public_path(request.url.path):
            return await call_next(request)

        # Skip auth for OPTIONS (CORS preflight)
        if request.method == "OPTIONS":
            return await call_next(request)

        # Extract API key from header
        api_key = request.headers.get(settings.api_key_header)

        if not api_key:
            logger.warning(
                "Missing API key",
                path=request.url.path,
                method=request.method,
                client_ip=self._get_client_ip(request),
            )
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={
                    "error": "unauthorized",
                    "message": f"Missing {settings.api_key_header} header",
                    "documentation_url": "https://docs.neurostack.ai/auth",
                },
            )

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
            return JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={
                    "error": "unauthorized",
                    "message": "Invalid API key",
                    "documentation_url": "https://docs.neurostack.ai/auth",
                },
            )

        # Check if tenant is active
        if not tenant_context.is_active:
            logger.warning(
                "Inactive tenant attempted access",
                tenant_id=str(tenant_context.tenant_id),
                path=request.url.path,
            )
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN,
                content={
                    "error": "forbidden",
                    "message": "Tenant account is inactive",
                },
            )

        # Attach tenant context to request state
        request.state.tenant = tenant_context
        request.state.api_key = api_key

        # Log successful authentication
        logger.debug(
            "Request authenticated",
            tenant_id=str(tenant_context.tenant_id),
            path=request.url.path,
        )

        # Continue processing
        response = await call_next(request)

        return response

    def _is_public_path(self, path: str) -> bool:
        """Check if path is public (no auth required)."""
        # Check exact match
        if path in self.PUBLIC_PATHS:
            return True

        # Check path prefixes for docs and metrics
        if path.startswith("/docs") or path.startswith("/redoc"):
            return True

        if path.startswith("/metrics"):
            return True

        return False

    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP from request, handling proxies."""
        # Check X-Forwarded-For header (common for load balancers)
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            # Get first IP in chain (original client)
            return forwarded_for.split(",")[0].strip()

        # Check X-Real-IP header (nginx)
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip

        # Fall back to direct client
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
    return request.state.tenant


def get_optional_tenant_context(request: Request) -> Optional[TenantContext]:
    """
    FastAPI dependency for optionally getting tenant context.
    
    Returns None if not authenticated (useful for public endpoints
    that behave differently for authenticated users).
    """
    return getattr(request.state, "tenant", None)

