"""
NeuroStack API - Rate Limiting Middleware
Token bucket rate limiting per tenant.
"""

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

import sys
from pathlib import Path

# Add the project root to sys.path for local module resolution
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from src.config.logging import get_logger
from src.config.settings import settings

logger = get_logger(__name__)


# ============================================
# Token Bucket Implementation
# ============================================


@dataclass
class TokenBucket:
    """
    Token bucket for rate limiting.
    
    Implements a token bucket algorithm where tokens are added at a fixed rate
    and consumed by requests. When the bucket is empty, requests are rejected.
    """

    capacity: int  # Maximum tokens in bucket
    refill_rate: float  # Tokens per second
    tokens: float = field(default=0.0)
    last_refill: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        """Initialize with full bucket."""
        self.tokens = float(self.capacity)

    def consume(self, tokens: int = 1) -> bool:
        """
        Attempt to consume tokens from the bucket.
        
        Args:
            tokens: Number of tokens to consume
            
        Returns:
            True if tokens were consumed, False if bucket is empty
        """
        self._refill()

        if self.tokens >= tokens:
            self.tokens -= tokens
            return True

        return False

    def get_remaining(self) -> int:
        """Get remaining tokens (after refill)."""
        self._refill()
        return int(self.tokens)

    def get_reset_time(self) -> int:
        """Get Unix timestamp when bucket will be full."""
        self._refill()
        tokens_needed = self.capacity - self.tokens
        seconds_to_full = tokens_needed / self.refill_rate
        return int(time.time() + seconds_to_full)

    def _refill(self) -> None:
        """Refill tokens based on elapsed time."""
        now = time.time()
        elapsed = now - self.last_refill

        # Add tokens based on elapsed time
        new_tokens = elapsed * self.refill_rate
        self.tokens = min(self.capacity, self.tokens + new_tokens)
        self.last_refill = now


# ============================================
# Rate Limit Store
# ============================================


class RateLimitStore:
    """
    In-memory rate limit store.
    
    TODO: Replace with Redis-backed implementation for distributed rate limiting
    in Task 5.0. This in-memory implementation works for single-instance deployments.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, TokenBucket] = {}
        self._tenant_limits: dict[str, int] = {}

    def get_or_create_bucket(
        self,
        tenant_id: str,
        rate_limit: Optional[int] = None,
    ) -> TokenBucket:
        """
        Get or create a token bucket for a tenant.
        
        Args:
            tenant_id: Unique tenant identifier
            rate_limit: Custom rate limit (requests per minute)
            
        Returns:
            TokenBucket for the tenant
        """
        if tenant_id not in self._buckets:
            limit = rate_limit or settings.rate_limit_requests_per_minute
            self._buckets[tenant_id] = TokenBucket(
                capacity=limit + settings.rate_limit_burst,
                refill_rate=limit / 60.0,  # Convert to per-second
            )
            self._tenant_limits[tenant_id] = limit

        return self._buckets[tenant_id]

    def get_limit(self, tenant_id: str) -> int:
        """Get the rate limit for a tenant."""
        return self._tenant_limits.get(
            tenant_id,
            settings.rate_limit_requests_per_minute,
        )

    def clear_tenant(self, tenant_id: str) -> None:
        """Clear rate limit data for a tenant (useful for testing)."""
        self._buckets.pop(tenant_id, None)
        self._tenant_limits.pop(tenant_id, None)


# Global rate limit store instance
rate_limit_store = RateLimitStore()


# ============================================
# Rate Limiting Middleware
# ============================================


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    FastAPI middleware for rate limiting.
    
    Implements per-tenant rate limiting using token bucket algorithm.
    Rate limits are configurable per tenant and include burst allowance.
    """

    # Paths exempt from rate limiting
    EXEMPT_PATHS: set[str] = {
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
        """Process request through rate limiting."""

        # Skip rate limiting if disabled
        if not settings.rate_limit_enabled:
            return await call_next(request)

        # Skip rate limiting for exempt paths
        if self._is_exempt_path(request.url.path):
            return await call_next(request)

        # Skip rate limiting for OPTIONS (CORS preflight)
        if request.method == "OPTIONS":
            return await call_next(request)

        # Get tenant context (set by AuthMiddleware)
        tenant_context = getattr(request.state, "tenant", None)

        if tenant_context is None:
            # No tenant context = request will be rejected by auth middleware
            # Just pass through
            return await call_next(request)

        tenant_id = str(tenant_context.tenant_id)
        rate_limit = tenant_context.rate_limit

        # Get or create token bucket for tenant
        bucket = rate_limit_store.get_or_create_bucket(tenant_id, rate_limit)

        # Try to consume a token
        if not bucket.consume():
            logger.warning(
                "Rate limit exceeded",
                tenant_id=tenant_id,
                path=request.url.path,
                method=request.method,
            )

            reset_time = bucket.get_reset_time()
            retry_after = max(1, reset_time - int(time.time()))

            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "error": "rate_limited",
                    "message": f"Rate limit exceeded. Try again in {retry_after} seconds.",
                    "retry_after": retry_after,
                },
                headers={
                    "X-RateLimit-Limit": str(rate_limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(reset_time),
                    "Retry-After": str(retry_after),
                },
            )

        # Process request
        response = await call_next(request)

        # Add rate limit headers to response
        remaining = bucket.get_remaining()
        reset_time = bucket.get_reset_time()

        response.headers["X-RateLimit-Limit"] = str(rate_limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        response.headers["X-RateLimit-Reset"] = str(reset_time)

        return response

    def _is_exempt_path(self, path: str) -> bool:
        """Check if path is exempt from rate limiting."""
        if path in self.EXEMPT_PATHS:
            return True

        if path.startswith("/docs") or path.startswith("/redoc"):
            return True

        if path.startswith("/metrics"):
            return True

        return False


# ============================================
# Utility Functions
# ============================================


def get_rate_limit_info(request: Request) -> dict:
    """
    Get rate limit information for the current request.
    
    Useful for exposing rate limit status in API responses.
    """
    tenant_context = getattr(request.state, "tenant", None)

    if tenant_context is None:
        return {
            "limit": settings.rate_limit_requests_per_minute,
            "remaining": "unknown",
            "reset": "unknown",
        }

    tenant_id = str(tenant_context.tenant_id)
    bucket = rate_limit_store.get_or_create_bucket(
        tenant_id,
        tenant_context.rate_limit,
    )

    return {
        "limit": tenant_context.rate_limit,
        "remaining": bucket.get_remaining(),
        "reset": bucket.get_reset_time(),
    }

