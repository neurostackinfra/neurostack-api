"""
NeuroStack API - OpenTelemetry Tracing Middleware
Distributed tracing for observability.
"""

import uuid
from typing import Callable, Optional

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

import sys
from pathlib import Path

# Add the project root to sys.path for local module resolution
project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from src.config.logging import get_logger, bind_context, clear_context
from src.config.settings import settings

logger = get_logger(__name__)


# ============================================
# OpenTelemetry Configuration
# ============================================


def configure_tracing() -> None:
    """
    Configure OpenTelemetry tracing for the application.
    
    This sets up:
    - Trace provider with configurable sampling
    - FastAPI instrumentation
    - Redis instrumentation
    - SQLAlchemy instrumentation
    """
    if not settings.tracing_enabled:
        logger.info("Tracing is disabled")
        return

    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        # Create resource with service information
        resource = Resource.create({
            "service.name": settings.app_name,
            "service.version": settings.app_version,
            "deployment.environment": settings.environment,
        })

        # Configure sampler based on settings
        sampler = TraceIdRatioBased(settings.tracing_sample_rate)

        # Create and set tracer provider
        provider = TracerProvider(
            resource=resource,
            sampler=sampler,
        )
        trace.set_tracer_provider(provider)

        logger.info(
            "OpenTelemetry tracing configured",
            sample_rate=settings.tracing_sample_rate,
            environment=settings.environment,
        )

    except ImportError as e:
        logger.warning(
            "OpenTelemetry packages not installed, tracing disabled",
            error=str(e),
        )
    except Exception as e:
        logger.error(
            "Failed to configure OpenTelemetry tracing",
            error=str(e),
            exc_info=True,
        )


def instrument_fastapi(app) -> None:
    """
    Instrument FastAPI application with OpenTelemetry.
    
    Args:
        app: FastAPI application instance
    """
    if not settings.tracing_enabled:
        return

    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            excluded_urls="health,ready,live,metrics,docs,redoc",
        )

        logger.info("FastAPI instrumented with OpenTelemetry")

    except ImportError:
        logger.warning("OpenTelemetry FastAPI instrumentation not available")
    except Exception as e:
        logger.error(
            "Failed to instrument FastAPI",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )


def instrument_redis() -> None:
    """Instrument Redis client with OpenTelemetry."""
    if not settings.tracing_enabled:
        return

    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor

        RedisInstrumentor().instrument()
        logger.info("Redis instrumented with OpenTelemetry")

    except ImportError:
        logger.debug("OpenTelemetry Redis instrumentation not available")
    except Exception as e:
        logger.error(
            "Failed to instrument Redis",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )


def instrument_sqlalchemy() -> None:
    """Instrument SQLAlchemy with OpenTelemetry."""
    if not settings.tracing_enabled:
        return

    try:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument()
        logger.info("SQLAlchemy instrumented with OpenTelemetry")

    except ImportError:
        logger.debug("OpenTelemetry SQLAlchemy instrumentation not available")
    except Exception as e:
        logger.error(
            "Failed to instrument SQLAlchemy",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )


# ============================================
# Request ID Middleware
# ============================================


class RequestIdMiddleware(BaseHTTPMiddleware):
    """
    Middleware to add unique request ID to each request.
    
    The request ID is:
    - Generated for each request (or extracted from X-Request-ID header)
    - Added to request state for access in handlers
    - Added to response headers
    - Bound to logging context for correlation
    """

    HEADER_NAME = "X-Request-ID"

    async def dispatch(
        self,
        request: Request,
        call_next: Callable,
    ):
        """Add request ID to request and response."""
        
        # Get existing request ID from header or generate new one
        request_id = request.headers.get(self.HEADER_NAME)
        if not request_id:
            request_id = str(uuid.uuid4())

        # Store in request state
        request.state.request_id = request_id

        # Bind to logging context
        bind_context(request_id=request_id)

        try:
            # Process request
            response = await call_next(request)

            # Add request ID to response headers
            response.headers[self.HEADER_NAME] = request_id

            return response

        finally:
            # Clear logging context
            clear_context()


# ============================================
# Span Helpers
# ============================================


def get_tracer(name: str = "neurostack"):
    """
    Get a tracer instance for creating spans.
    
    Args:
        name: Name for the tracer (module name recommended)
        
    Returns:
        OpenTelemetry tracer or NoOpTracer if tracing disabled
    """
    if not settings.tracing_enabled:
        return NoOpTracer()

    try:
        from opentelemetry import trace
        return trace.get_tracer(name, settings.app_version)
    except ImportError:
        return NoOpTracer()


class NoOpSpan:
    """No-operation span for when tracing is disabled."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def set_attribute(self, key: str, value) -> None:
        pass

    def set_status(self, status) -> None:
        pass

    def record_exception(self, exception: Exception) -> None:
        pass

    def add_event(self, name: str, attributes: Optional[dict] = None) -> None:
        pass


class NoOpTracer:
    """No-operation tracer for when tracing is disabled."""

    def start_as_current_span(self, name: str, **kwargs):
        return NoOpSpan()

    def start_span(self, name: str, **kwargs):
        return NoOpSpan()


# ============================================
# Tracing Decorators
# ============================================


def trace_function(name: Optional[str] = None):
    """
    Decorator to trace a function with OpenTelemetry.
    
    Usage:
        @trace_function("process_event")
        async def process_event(event):
            ...
    """
    def decorator(func):
        span_name = name or func.__name__

        if settings.tracing_enabled:
            try:
                from opentelemetry import trace
                from functools import wraps
                import asyncio

                tracer = trace.get_tracer("neurostack")

                @wraps(func)
                async def async_wrapper(*args, **kwargs):
                    with tracer.start_as_current_span(span_name):
                        return await func(*args, **kwargs)

                @wraps(func)
                def sync_wrapper(*args, **kwargs):
                    with tracer.start_as_current_span(span_name):
                        return func(*args, **kwargs)

                if asyncio.iscoroutinefunction(func):
                    return async_wrapper
                return sync_wrapper

            except ImportError:
                return func
        return func

    return decorator


# ============================================
# Span Context Helpers
# ============================================


def add_span_attributes(**attributes) -> None:
    """
    Add attributes to the current span.
    
    Usage:
        add_span_attributes(
            tenant_id=str(tenant.id),
            event_type="message.in",
        )
    """
    if not settings.tracing_enabled:
        return

    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, str(value) if not isinstance(value, (str, int, float, bool)) else value)

    except ImportError:
        pass
    except Exception as e:
        # Silently ignore errors in span attribute setting (non-critical)
        logger.debug(
            "Failed to set span attribute",
            error=str(e),
            error_type=type(e).__name__,
        )


def record_span_exception(exception: Exception) -> None:
    """Record an exception in the current span."""
    if not settings.tracing_enabled:
        return

    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        span.record_exception(exception)

    except ImportError:
        pass
    except Exception as e:
        # Silently ignore errors in exception recording (non-critical)
        logger.debug(
            "Failed to record exception in span",
            error=str(e),
            error_type=type(e).__name__,
        )

