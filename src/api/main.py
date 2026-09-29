"""
NeuroStack API - Main FastAPI Application
Entry point for the cognitive infrastructure API
"""

# Standard library imports
import json
import re
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional

# Third-party imports
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import make_asgi_app

from neurostack_core import enable_metrics

# Local application imports
from src.api.middleware.auth import AuthMiddleware
from src.api.middleware.performance import (
    CachingMiddleware,
    ConnectionOptimizationMiddleware,
    PerformanceMiddleware,
)
from src.api.middleware.rate_limit import RateLimitMiddleware
from src.api.middleware.tracing import (
    RequestIdMiddleware,
    configure_tracing,
    instrument_fastapi,
    instrument_redis,
    instrument_sqlalchemy,
)
from src.api.routes import (
    admin,
    auth,
    billing,
    builder,
    config,
    demo_leads,
    events,
    handoff,
    health,
    knowledge,
    policies,
    privacy,
    rlhf,
    sessions,
    support,
    traces,
    webhook,
)
from src.config.logging import configure_logging, get_logger
from src.config.settings import settings
from src.core.exceptions import ConfigurationError

# Configure logging on module load
configure_logging()
logger = get_logger(__name__)


def _redact_capability_token(path: str) -> str:
    """Redact the Z-API webhook capability token from a raw path.

    Used as a fallback for exception logging when no route template
    resolved (e.g. an unmatched 404). Every other observability sink
    prefers ``request.scope["route"].path`` directly.
    """

    return re.sub(
        r"(/v1/webhook/zapi)/[A-Za-z0-9_-]{20,}",
        r"\1/{capability_token}",
        path,
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler for startup and shutdown events."""
    # Startup - Validate configuration
    try:
        # Settings validation happens during instantiation, but we log it here
        logger.info(
            "Starting NeuroStack API",
            version=settings.app_version,
            environment=settings.environment,
            debug=settings.debug,
            tracing_enabled=settings.tracing_enabled,
            rate_limit_enabled=settings.rate_limit_enabled,
        )

        # Additional runtime validation
        if settings.is_production:
            logger.info("Running in PRODUCTION mode - security validations enforced")
    except ConfigurationError as e:
        logger.critical(
            "Configuration validation failed",
            error=str(e),
            exc_info=True,
        )
        raise
    except Exception as e:
        logger.critical(
            "Unexpected error during startup validation",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        raise ConfigurationError(f"Startup validation failed: {e}") from e

    if settings.metrics_enabled:
        enable_metrics()

    # Configure OpenTelemetry tracing
    configure_tracing()
    instrument_redis()
    instrument_sqlalchemy()

    # Initialize database connection
    from src.repositories import init_database

    await init_database()

    # Initialize other connections (will be implemented in later tasks)
    # await init_redis()
    # await init_qdrant()

    try:
        yield
    finally:
        # Shutdown shared services before their database dependency. Each
        # boundary remains eligible for cleanup even when an earlier one fails.
        logger.info("Shutting down NeuroStack API")
        from src.memory.event_store import close_event_store
        from src.repositories import close_database
        from src.services.handoff_service import close_handoff_service

        try:
            await rlhf.shutdown()
        finally:
            try:
                await close_handoff_service()
            finally:
                try:
                    await close_event_store()
                finally:
                    await close_database()
    # await close_redis()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""

    docs_enabled = settings.docs_enabled

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description=(
            "NeuroStack - Cognitive Infrastructure for AI Agents in Healthcare. "
            "Provides deterministic reasoning, memory, and policy enforcement for AI agents."
        ),
        docs_url="/docs" if docs_enabled else None,
        redoc_url="/redoc" if docs_enabled else None,
        redoc_js_url=settings.redoc_js_url,
        # Prefer versioned OpenAPI URL so /redoc resolves behind API prefixes
        openapi_url=f"{settings.api_prefix}/openapi.json" if docs_enabled else None,
        lifespan=lifespan,
    )

    def _is_public_path(path: str) -> bool:
        """Check if a path is public (doesn't require authentication)."""
        public_paths = [
            "/",
            "/health",
            "/ready",
            "/live",
            "/docs",
            "/redoc",
            "/openapi.json",
            f"{settings.api_prefix}/openapi.json",
            "/metrics",
        ]
        return path in public_paths or any(
            path.startswith(p) for p in ["/docs", "/redoc", "/metrics"]
        )

    def _ensure_openapi_structure(schema: dict[str, Any]) -> dict[str, Any]:
        """Ensure OpenAPI schema has required structure."""
        # Use OpenAPI 3.0.3 for better ReDoc compatibility
        if "openapi" not in schema:
            schema["openapi"] = "3.0.3"
        elif schema.get("openapi") == "3.1.0":
            schema["openapi"] = "3.0.3"

        if "info" not in schema:
            schema["info"] = {
                "title": settings.app_name,
                "version": settings.app_version,
                "description": (
                    "NeuroStack - Cognitive Infrastructure for AI Agents in Healthcare. "
                    "Provides deterministic reasoning, memory, and policy enforcement for AI agents."
                ),
            }

        if "paths" not in schema:
            schema["paths"] = {}

        return schema

    def _add_header_parameter(
        operation: dict[str, Any],
        header_name: str,
        required: bool,
        description: str,
        schema_type: str = "string",
        schema_format: Optional[str] = None,
    ) -> None:
        """Add a header parameter to an operation if it doesn't exist."""
        if "parameters" not in operation:
            operation["parameters"] = []

        # Check if header already exists
        header_exists = any(
            isinstance(param, dict) and param.get("name") == header_name
            for param in operation.get("parameters", [])
        )

        if not header_exists:
            param_schema = {"type": schema_type, "description": description}
            if schema_format:
                param_schema["format"] = schema_format

            operation["parameters"].append(
                {
                    "name": header_name,
                    "in": "header",
                    "required": required,
                    "schema": param_schema,
                    "description": description,
                }
            )

    def _add_headers_to_operation(operation: dict[str, Any]) -> None:
        """Add X-API-Key and X-Request-ID headers to an operation."""
        _add_header_parameter(
            operation,
            header_name=settings.api_key_header,
            required=True,
            description="Your API key for authenticating requests to the NeuroStack API.",
            schema_type="string",
        )

        _add_header_parameter(
            operation,
            header_name="X-Request-ID",
            required=False,
            description=(
                "Optional unique identifier for request correlation and tracing. "
                "If omitted, the API will generate one automatically."
            ),
            schema_type="string",
            schema_format="uuid",
        )

    def _add_headers_to_paths(schema: dict[str, Any]) -> None:
        """Add authentication headers to all non-public path operations."""
        bearer_only_operations = {
            (f"{settings.api_prefix}/auth/me/password", "patch"),
            (f"{settings.api_prefix}/builder/clinics/{{clinic_id}}/password", "patch"),
            (
                f"{settings.api_prefix}/builder/clinics/{{source_id}}/clone-preview",
                "get",
            ),
            (f"{settings.api_prefix}/builder/clinics/{{source_id}}/clone", "post"),
        }
        for path, path_item in schema.get("paths", {}).items():
            if _is_public_path(path):
                continue

            if not isinstance(path_item, dict):
                continue

            for method, operation in path_item.items():
                if method not in [
                    "get",
                    "post",
                    "put",
                    "patch",
                    "delete",
                    "options",
                    "head",
                ]:
                    continue

                if not isinstance(operation, dict):
                    continue

                if (path, method) in bearer_only_operations:
                    # These credential mutations are intentionally JWT-only. Keep
                    # request correlation documentation, but do not advertise the
                    # global API key as required or sufficient for either route.
                    _add_header_parameter(
                        operation,
                        header_name="X-Request-ID",
                        required=False,
                        description=(
                            "Optional unique identifier for request correlation and "
                            "tracing. If omitted, the API will generate one automatically."
                        ),
                        schema_type="string",
                        schema_format="uuid",
                    )
                    operation["security"] = [{"BearerAuth": []}]
                    continue

                _add_headers_to_operation(operation)

    def custom_openapi() -> dict[str, Any]:
        """
        Custom OpenAPI schema that includes X-API-Key and X-Request-ID headers.

        Adds these headers to all endpoints in the Swagger documentation:
        - X-API-Key: Required header for API authentication
        - X-Request-ID: Optional header for request correlation
        """
        if app.openapi_schema:
            return app.openapi_schema

        try:
            from fastapi.openapi.utils import get_openapi

            openapi_schema = get_openapi(
                title=settings.app_name,
                version=settings.app_version,
                description=(
                    "NeuroStack - Cognitive Infrastructure for AI Agents in Healthcare. "
                    "Provides deterministic reasoning, memory, and policy enforcement for AI agents."
                ),
                routes=app.routes,
            )

            # Ensure basic structure
            openapi_schema = _ensure_openapi_structure(openapi_schema)

            # Log schema generation for debugging
            logger.debug(
                "Generating OpenAPI schema",
                paths_count=len(openapi_schema.get("paths", {})),
                routes_count=len(app.routes),
            )

            # Add headers to all path operations
            _add_headers_to_paths(openapi_schema)

            # Add security schemes (X-API-Key and Bearer JWT)
            if "components" not in openapi_schema:
                openapi_schema["components"] = {}
            if "securitySchemes" not in openapi_schema["components"]:
                openapi_schema["components"]["securitySchemes"] = {}
            openapi_schema["components"]["securitySchemes"]["ApiKeyAuth"] = {
                "type": "apiKey",
                "in": "header",
                "name": settings.api_key_header,
                "description": "API key passed via X-API-Key header.",
            }
            openapi_schema["components"]["securitySchemes"]["BearerAuth"] = {
                "type": "http",
                "scheme": "bearer",
                "bearerFormat": "JWT",
                "description": "JWT token obtained from the /v1/auth/login endpoint.",
            }
            # Apply both security schemes globally
            openapi_schema["security"] = [
                {"ApiKeyAuth": []},
                {"BearerAuth": []},
            ]

            # Validate schema has required fields
            if (
                not openapi_schema.get("openapi")
                or not openapi_schema.get("info")
                or not openapi_schema.get("paths")
            ):
                logger.warning(
                    "OpenAPI schema missing required fields",
                    has_openapi="openapi" in openapi_schema,
                    has_info="info" in openapi_schema,
                    has_paths="paths" in openapi_schema,
                )

            app.openapi_schema = openapi_schema
            logger.debug(
                "OpenAPI schema generated successfully",
                paths_count=len(openapi_schema.get("paths", {})),
            )
            return app.openapi_schema

        except Exception as e:
            logger.error(
                "Failed to generate custom OpenAPI schema",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            # Fallback to default schema if custom generation fails
            from fastapi.openapi.utils import get_openapi

            app.openapi_schema = get_openapi(
                title=settings.app_name,
                version=settings.app_version,
                description=(
                    "NeuroStack - Cognitive Infrastructure for AI Agents in Healthcare. "
                    "Provides deterministic reasoning, memory, and policy enforcement for AI agents."
                ),
                routes=app.routes,
            )
            return app.openapi_schema

    # Configure CORS (must be first/outermost middleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Add Request ID middleware (for tracing correlation)
    app.add_middleware(RequestIdMiddleware)

    # Add Performance monitoring middleware
    app.add_middleware(PerformanceMiddleware)

    # Add Connection optimization middleware
    app.add_middleware(ConnectionOptimizationMiddleware)

    # Add Response caching middleware for GET requests
    app.add_middleware(CachingMiddleware)

    # Add Rate Limiting middleware (before auth to protect from abuse)
    if settings.rate_limit_enabled:
        app.add_middleware(RateLimitMiddleware)

    # Add Authentication middleware
    app.add_middleware(AuthMiddleware)

    # Instrument FastAPI with OpenTelemetry
    if settings.tracing_enabled:
        instrument_fastapi(app)

    # Add Prometheus metrics endpoint
    if settings.metrics_enabled:
        metrics_app = make_asgi_app()
        app.mount("/metrics", metrics_app)

    # Register routers
    app.include_router(health.router, tags=["Health"])
    app.include_router(
        events.router,
        prefix=f"{settings.api_prefix}/events",
        tags=["Events"],
    )
    app.include_router(
        sessions.router,
        prefix=f"{settings.api_prefix}/sessions",
        tags=["Sessions"],
    )
    app.include_router(
        policies.router,
        prefix=f"{settings.api_prefix}/policies",
        tags=["Policies"],
    )
    app.include_router(
        knowledge.router,
        prefix=f"{settings.api_prefix}/knowledge",
        tags=["Knowledge"],
    )
    app.include_router(
        handoff.router,
        prefix=f"{settings.api_prefix}/handoffs",
        tags=["Handoffs"],
    )
    app.include_router(
        auth.router,
        prefix=f"{settings.api_prefix}/auth",
        tags=["Auth"],
    )
    app.include_router(
        traces.router,
        prefix=f"{settings.api_prefix}/traces",
        tags=["Traces"],
    )
    app.include_router(
        config.router,
        prefix=f"{settings.api_prefix}/config",
        tags=["Config"],
    )
    app.include_router(
        support.router,
        prefix=f"{settings.api_prefix}/support",
        tags=["Support"],
    )
    app.include_router(
        builder.router,
        prefix=f"{settings.api_prefix}/builder",
        tags=["Builder"],
    )
    app.include_router(
        billing.router,
        prefix=f"{settings.api_prefix}/billing",
        tags=["Billing"],
    )
    app.include_router(
        admin.router,
        prefix=f"{settings.api_prefix}/admin",
        tags=["Admin"],
    )
    app.include_router(
        rlhf.router,
        prefix=f"{settings.api_prefix}/rlhf",
        tags=["RLHF"],
    )
    app.include_router(
        webhook.router,
        prefix=f"{settings.api_prefix}/webhook",
        tags=["Webhook"],
    )
    app.include_router(
        demo_leads.router,
        prefix=f"{settings.api_prefix}/demo-leads",
        tags=["Demo Leads"],
    )
    app.include_router(
        privacy.router,
        tags=["Privacy"],
    )
    app.include_router(
        privacy.public_router,
        prefix="/api/public",
        tags=["Privacy"],
    )

    # Override OpenAPI schema to include headers (after all routers are registered)
    # This must be done after all routers are registered so routes are available
    setattr(app, "openapi", custom_openapi)

    # Add alternative OpenAPI JSON endpoint for compatibility (root path)
    @app.get("/openapi.json", include_in_schema=False)
    async def alternative_openapi_json() -> Response:
        """Alternative OpenAPI JSON endpoint for compatibility."""
        schema = app.openapi()
        return Response(
            content=json.dumps(schema, indent=2),
            media_type="application/json",
        )

    # Global exception handler
    @app.exception_handler(Exception)
    async def global_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        # Route template first — never the raw path, so capability tokens
        # from /v1/webhook/zapi/{capability_token} cannot leak into error
        # logs or exception-reporting sinks (Sentry-shaped ``extra``).
        route = request.scope.get("route") if hasattr(request, "scope") else None
        template = getattr(route, "path", None) if route is not None else None
        endpoint = (
            template
            if isinstance(template, str) and template
            else _redact_capability_token(request.url.path)
        )
        logger.error(
            "Unhandled exception",
            endpoint=endpoint,
            method=request.method,
            error_type=type(exc).__name__,
            exc_info=True,
        )
        return JSONResponse(
            status_code=500,
            content={
                "error": "internal_server_error",
                "message": "An unexpected error occurred",
            },
        )

    return app


# Create app instance
app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "src.api.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.reload,
        workers=settings.workers if not settings.reload else 1,
    )
