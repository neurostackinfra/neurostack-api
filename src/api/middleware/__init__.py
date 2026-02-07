"""
NeuroStack API - Middleware Module

This module contains all middleware components for the API:
- AuthMiddleware: API Key authentication and tenant identification
- RateLimitMiddleware: Per-tenant rate limiting with token bucket algorithm
- TracingMiddleware: OpenTelemetry distributed tracing
- RequestIdMiddleware: Request ID generation and correlation
"""

from src.api.middleware.auth import (
    AuthMiddleware,
    AuthenticationError,
    TenantContext,
    APIKeyStore,
    get_tenant_context,
    get_optional_tenant_context,
)
from src.api.middleware.rate_limit import (
    RateLimitMiddleware,
    RateLimitStore,
    TokenBucket,
    rate_limit_store,
    get_rate_limit_info,
)
from src.api.middleware.tracing import (
    RequestIdMiddleware,
    configure_tracing,
    instrument_fastapi,
    instrument_redis,
    instrument_sqlalchemy,
    get_tracer,
    trace_function,
    add_span_attributes,
    record_span_exception,
)

__all__ = [
    # Authentication
    "AuthMiddleware",
    "AuthenticationError",
    "TenantContext",
    "APIKeyStore",
    "get_tenant_context",
    "get_optional_tenant_context",
    # Rate Limiting
    "RateLimitMiddleware",
    "RateLimitStore",
    "TokenBucket",
    "rate_limit_store",
    "get_rate_limit_info",
    # Tracing
    "RequestIdMiddleware",
    "configure_tracing",
    "instrument_fastapi",
    "instrument_redis",
    "instrument_sqlalchemy",
    "get_tracer",
    "trace_function",
    "add_span_attributes",
    "record_span_exception",
]
