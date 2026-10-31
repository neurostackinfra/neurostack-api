"""
NeuroStack API - Main FastAPI Application
Entry point for the cognitive infrastructure API
"""

# Standard library imports
import asyncio
import json
import re
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncGenerator, Optional

# Third-party imports
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import make_asgi_app
from redis.asyncio import Redis

from neurostack_core import enable_metrics

# Local application imports
from src.api.middleware.auth import AuthMiddleware
from src.api.middleware.ingress_body_limit import IngressBodyLimitMiddleware
from src.api.middleware.media_limits import (
    MediaConcurrencyLimiters,
    MediaUploadLimitsMiddleware,
)
from src.api.middleware.performance import (
    CachingMiddleware,
    ConnectionOptimizationMiddleware,
    PerformanceMiddleware,
)
from src.api.middleware.rate_limit import RateLimitMiddleware
from src.api.middleware.tenant_identity import TenantIdentityMiddleware
from src.api.middleware.tracing import (
    RequestIdMiddleware,
    configure_tracing,
    instrument_fastapi,
    instrument_redis,
    instrument_sqlalchemy,
)
from src.api.routes import (
    admin,
    admin_credits,
    auth,
    billing,
    billing_producer,
    billing_producer_setup,
    builder,
    config,
    credit_export,
    credits,
    demo_leads,
    events,
    handoff,
    health,
    knowledge,
    media_assets,
    policies,
    privacy,
    rlhf,
    sessions,
    support,
    traces,
    webhook,
)
from src.api.spa import install_spa
from src.config.logging import configure_logging, get_logger
from src.config.settings import settings
from src.core.exceptions import ConfigurationError
from src.services.billing_fact_worker import (
    BillingFactWorker,
    start_billing_fact_worker,
)
from src.services.billing_webhook import ASAAS_WEBHOOK_READINESS
from src.services.credit_inventory import (
    start_credit_object_workers,
    stop_credit_object_workers,
)
from src.services.credit_lifecycle import (
    start_credit_lifecycle_worker,
    stop_credit_lifecycle_worker,
)
from src.services.credit_metering import (
    start_credit_response_publisher,
    start_credit_retention_sweeper,
    stop_credit_response_publisher,
    stop_credit_retention_sweeper,
)
from src.services.credit_rollout import (
    start_credit_orphan_reconciliation_worker,
    stop_credit_orphan_reconciliation_worker,
)
from src.services.credit_switch import (
    CreditUnavailableError,
    credit_unavailable_exception_handler,
    refresh_credit_switch,
    start_credit_switch_refresher,
    stop_credit_switch,
)

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


def _start_webhook_readiness_refresher() -> "asyncio.Task[None] | None":
    """Keep this worker's ASAAS webhook verdict warm (Group 3, fail-closed).

    Started only when ASAAS is configured and a webhook URL is set; each
    worker process refreshes its own gate.  Hosted checkout stays blocked
    whenever this worker's last readback failed or is too old.
    """

    if not (settings.is_asaas_configuration_compatible and settings.asaas_webhook_url):
        return None
    return asyncio.create_task(
        ASAAS_WEBHOOK_READINESS.run_refresher(billing._read_back_webhook_readiness),
        name="asaas-webhook-readiness",
    )


def _start_billing_fact_worker() -> BillingFactWorker | None:
    """Deliver and escalate ``billing.fact.v1`` rows (Group 9), off by default.

    ``BILLING_FACT_WORKER_ENABLED=false`` returns before touching the pool.
    No credit consumer exists yet, so no transport is wired: facts stay
    pending/retriable and the stale alert fires instead of a presentation.
    """

    from src.repositories.database import get_db_session_factory

    return start_billing_fact_worker(settings, get_db_session_factory, transport=None)


def _install_outermost_ingress_body_limit(app: FastAPI) -> None:
    """Keep the ingress cap outside wrappers installed by instrumentation."""

    build_instrumented_stack = app.build_middleware_stack

    def build_middleware_stack() -> IngressBodyLimitMiddleware:
        return IngressBodyLimitMiddleware(build_instrumented_stack())

    app.build_middleware_stack = build_middleware_stack  # type: ignore[method-assign]


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan handler for startup and shutdown events."""
    # Startup - Validate configuration
    try:
        settings.validate_runtime_environment()
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

    from src.repositories import init_database

    await init_database()
    # Credit dominance switch (neurostack-credit-system Group 1b): read once
    # here, then refreshed in the background only when the config is on.  It
    # fails closed and never raises; with the config off it reads nothing.
    await refresh_credit_switch()
    credit_switch_refresher: asyncio.Task[None] | None = None
    credit_publisher: asyncio.Task[None] | None = None
    credit_sweeper: asyncio.Task[None] | None = None
    credit_object_workers: asyncio.Task[None] | None = None
    credit_lifecycle_worker: asyncio.Task[None] | None = None
    credit_orphan_worker: asyncio.Task[None] | None = None
    webhook_refresher = _start_webhook_readiness_refresher()
    fact_worker: BillingFactWorker | None = None
    redis_client: Redis | None = None
    try:
        credit_switch_refresher = start_credit_switch_refresher()
        # The response-outbox publisher (neurostack-credit-system Group 3b-2):
        # only with the credit config on; it claims nothing while the switch
        # is not effective nor before credit_enforcement_at.  The send
        # adapter comes from the webhook route (no import cycle, LOW-13).
        credit_publisher = start_credit_response_publisher(webhook.CREDIT_REPLY_SENDER)
        # The retention sweep of the outbox's personal data runs config on or
        # off (OD-3: the one exception to the dominance rule, and only to
        # ERASE expired payloads; it never sends).
        credit_sweeper = start_credit_retention_sweeper()
        # Group 4b: external copies are additive and worker-owned.  The loop
        # remains inert unless the effective switch and dev/test store config
        # are both present; it is cancelled before the shared pool closes.
        credit_object_workers = start_credit_object_workers()
        # Group 5: the exhaustion lifecycle worker (72 h / 30 d transitions,
        # reactivation at an allowance's start).  Only with the credit config
        # on; every tick claims nothing while the switch is not effective or
        # before credit_enforcement_at.
        credit_lifecycle_worker = start_credit_lifecycle_worker()
        # Group 7: post-enforcement reconciliation exposes only bounded
        # orphan evidence.  It remains dormant until the immutable fence is
        # set and cannot activate that fence or any ASAAS rollout control.
        credit_orphan_worker = start_credit_orphan_reconciliation_worker()
        fact_worker = _start_billing_fact_worker()
        if settings.environment == "staging":
            redis_client = Redis.from_url(
                settings.redis_url,
                max_connections=settings.redis_max_connections,
                socket_timeout=settings.redis_socket_timeout,
                decode_responses=True,
            )
        app.state.redis = redis_client
        yield
    finally:
        # First, and it never raises: off (and the refresher cancelled) before
        # anything else stops and before the pool closes.
        await stop_credit_switch(credit_switch_refresher)
        # Before the pool closes; it never raises.  A reply cut mid-send
        # stays ``sending`` and is reclaimed after its lease.
        await stop_credit_response_publisher(credit_publisher)
        await stop_credit_retention_sweeper(credit_sweeper)
        await stop_credit_object_workers(credit_object_workers)
        await stop_credit_lifecycle_worker(credit_lifecycle_worker)
        await stop_credit_orphan_reconciliation_worker(credit_orphan_worker)
        if webhook_refresher is not None:
            webhook_refresher.cancel()
            with suppress(asyncio.CancelledError):
                await webhook_refresher
        if fact_worker is not None:
            # Before the pool closes: the in-flight fact commits or rolls back.
            await fact_worker.stop()
        # Shutdown shared services before their database dependency. Each
        # boundary remains eligible for cleanup even when an earlier one fails.
        logger.info("Shutting down NeuroStack API")
        from src.repositories import close_database
        from src.services.handoff_service import close_handoff_service

        try:
            await rlhf.shutdown()
        finally:
            try:
                await close_handoff_service()
            finally:
                try:
                    if redis_client is not None:
                        await redis_client.aclose()
                finally:
                    app.state.redis = None
                    await close_database()


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

    media_limiters = MediaConcurrencyLimiters(
        uploads=settings.media_max_concurrent_uploads,
        downloads=settings.media_max_concurrent_downloads,
    )
    app.state.media_concurrency = media_limiters

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
            f"{settings.api_prefix}/billing/producer-build-trust",
            "/metrics",
        ]
        return path in public_paths or any(
            path.startswith(p) for p in ["/docs", "/redoc", "/metrics"]
        )

    def _ensure_openapi_structure(schema: dict[str, Any]) -> dict[str, Any]:
        """Ensure OpenAPI schema has required structure."""
        if "openapi" not in schema:
            schema["openapi"] = "3.1.0"

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
            (f"{settings.api_prefix}/builder/clinics/{{clinic_id}}/email", "patch"),
            (
                f"{settings.api_prefix}/builder/clinics/{{source_id}}/clone-preview",
                "get",
            ),
            (f"{settings.api_prefix}/builder/clinics/{{source_id}}/clone", "post"),
        }
        for path, path_item in schema.get("paths", {}).items():
            if _is_public_path(path):
                if path == f"{settings.api_prefix}/billing/producer-build-trust":
                    operation = (
                        path_item.get("get") if isinstance(path_item, dict) else None
                    )
                    if isinstance(operation, dict):
                        # OpenAPI's global security otherwise makes this no-auth
                        # runtime route appear authenticated to generated clients.
                        operation["security"] = []
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

                if "security" in operation:
                    # Operations with an explicit security contract already declare
                    # which credential alternatives are accepted. Do not also render
                    # X-API-Key as an independently required parameter.
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
                openapi_version="3.1.0",
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
                openapi_version="3.1.0",
                description=(
                    "NeuroStack - Cognitive Infrastructure for AI Agents in Healthcare. "
                    "Provides deterministic reasoning, memory, and policy enforcement for AI agents."
                ),
                routes=app.routes,
            )
            return app.openapi_schema

    # Starlette wraps middleware in reverse registration order. Keep ancillary
    # response/performance middleware inside the security boundary below.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Add Performance monitoring middleware
    app.add_middleware(PerformanceMiddleware)

    # Add Connection optimization middleware
    app.add_middleware(ConnectionOptimizationMiddleware)

    # Add Response caching middleware for GET requests
    app.add_middleware(CachingMiddleware)

    # Media concurrency remains inside auth and the identity/rate boundary.
    app.add_middleware(
        MediaUploadLimitsMiddleware,
        limiters=media_limiters,
        upload_path=f"{settings.api_prefix}/media-assets",
    )

    # Install tracing first so later security middleware remains outside it.
    if settings.tracing_enabled:
        instrument_fastapi(app)

    # Security order is explicit. Because registration order is reversed at
    # runtime, these calls produce:
    # auth -> request ID -> tenant identity -> rate limit -> routes.
    if settings.rate_limit_enabled:
        app.add_middleware(RateLimitMiddleware)
    app.add_middleware(TenantIdentityMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(AuthMiddleware)

    # OpenTelemetry replaces ``build_middleware_stack`` and wraps its result,
    # so registering the body cap as ordinary middleware would still leave
    # tracing outside it. Wrap the instrumented builder itself to guarantee:
    # body cap -> tracing -> auth -> request ID -> tenant identity -> rate limit.
    _install_outermost_ingress_body_limit(app)

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
        media_assets.router,
        prefix=f"{settings.api_prefix}/media-assets",
        tags=["Media Assets"],
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
        billing_producer.router,
        prefix=f"{settings.api_prefix}/admin/billing/producer-receipts",
        tags=["Billing Producer"],
    )
    app.include_router(
        billing_producer_setup.router,
        prefix=f"{settings.api_prefix}/admin/billing",
        tags=["Billing Producer Setup"],
    )
    app.include_router(
        billing_producer_setup.public_router,
        prefix=f"{settings.api_prefix}/billing",
        tags=["Billing Producer Trust"],
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
    # Credit Group 5: the tenant export (a credit surface: 503
    # credit_unavailable while the switch is off).
    app.include_router(
        credit_export.router,
        prefix=f"{settings.api_prefix}/credits",
        tags=["Credits"],
    )
    # Credit Group 6: the read API (catalog, balance, lots, expiries, debt,
    # reservations, thresholds, state, consumption); a credit surface too.
    app.include_router(
        credits.router,
        prefix=f"{settings.api_prefix}/credits",
        tags=["Credits"],
    )
    # Credit Group 5: operational roles, signed authorizations, legal hold
    # and the (disabled) purge.
    app.include_router(
        admin_credits.router,
        prefix=f"{settings.api_prefix}/admin/credits",
        tags=["Credits Admin"],
    )

    # Credit surfaces (neurostack-credit-system Group 1b): a refusal raised by
    # ``require_credit_switch`` answers 503 {"code": "credit_unavailable"}.
    app.add_exception_handler(
        CreditUnavailableError, credit_unavailable_exception_handler
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

    # Built frontend at exact paths. Must stay after every API route and
    # include_router: its collision check only sees routes registered before
    # it, and it wraps the lifespan merged so far to re-check the route table
    # once every startup hook has run. A router included later would start
    # after that re-check, so a lifespan replaced after this call turns the
    # SPA off at startup (pinned by test_spa_static).
    install_spa(app)

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
