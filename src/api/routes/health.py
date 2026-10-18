"""
NeuroStack API - Health Check Endpoints

Provides health, readiness, and liveness endpoints for monitoring
and orchestration (e.g., Kubernetes, load balancers).

Endpoints:
- GET /health - Comprehensive health check
- GET /ready - Readiness probe (all dependencies available)
- GET /live - Liveness probe (process is alive)
"""

import asyncio
import time
from datetime import datetime
from typing import Any, cast

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.health import (
    ComponentHealth,
    DependencyStatus,
    HealthResponse,
    LivenessResponse,
    MetricsResponse,
    ReadinessResponse,
)
from src.config.settings import settings
from src.repositories import get_db_session_dependency

router = APIRouter()

# Track startup time for uptime calculation
_startup_time = time.time()


@router.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="Health Check",
    description="""
Comprehensive health check endpoint.

Returns the overall health status of the API along with the status
of individual components (database, Redis, Qdrant).

## Status Values

- `healthy`: All components are working correctly
- `degraded`: Some components have issues but API is functional
- `unhealthy`: Critical components are failing

## Use Cases

- Monitoring dashboards
- Alerting systems
- Debugging connectivity issues
    """,
    tags=["Health"],
)
async def health_check() -> HealthResponse:
    """
    Comprehensive health check endpoint.

    Returns the current health status of the API and all dependencies.
    """
    # Calculate uptime
    uptime_seconds = int(time.time() - _startup_time)

    # Check individual components
    # TODO: Implement actual health checks (Tasks 5.0, 6.0)
    components = [
        ComponentHealth(
            name="api",
            status=DependencyStatus.HEALTHY,
            latency_ms=None,
            message=None,
            details={"version": settings.app_version},
        ),
        ComponentHealth(
            name="database",
            status=DependencyStatus.HEALTHY,
            latency_ms=5,
            message=None,
            details={"pool_size": settings.database_pool_size},
        ),
        ComponentHealth(
            name="redis",
            status=DependencyStatus.HEALTHY,
            latency_ms=1,
            message=None,
            details={"max_connections": settings.redis_max_connections},
        ),
        ComponentHealth(
            name="qdrant",
            status=DependencyStatus.HEALTHY,
            latency_ms=3,
            message=None,
            details={"host": settings.qdrant_host},
        ),
    ]

    # Determine overall status
    unhealthy_count = sum(
        1 for c in components if c.status == DependencyStatus.UNHEALTHY
    )
    degraded_count = sum(1 for c in components if c.status == DependencyStatus.DEGRADED)

    if unhealthy_count > 0:
        overall_status = "unhealthy"
    elif degraded_count > 0:
        overall_status = "degraded"
    else:
        overall_status = "healthy"

    return HealthResponse(
        status=overall_status,
        version=settings.app_version,
        environment=settings.environment,
        timestamp=datetime.utcnow().isoformat(),
        uptime_seconds=uptime_seconds,
        components=components,
    )


async def _database_ready(db: AsyncSession | None) -> bool:
    """Production probe: one bounded ``SELECT 1`` on the request session."""

    if not isinstance(db, AsyncSession):
        return False
    try:
        await asyncio.wait_for(db.execute(text("SELECT 1")), timeout=1.0)
    except Exception:
        return False
    return True


async def _redis_ready() -> bool:
    """Production probe: a bounded Redis ``PING``."""

    try:
        import redis.asyncio as redis

        client = cast(Any, redis.from_url)(settings.redis_url, socket_timeout=1.0)
        try:
            return (await asyncio.wait_for(client.ping(), timeout=1.0)) is True
        finally:
            await client.aclose()
    except Exception:
        return False


async def _qdrant_ready() -> bool:
    """Production probe: Qdrant ``/healthz`` with a one-second budget."""

    try:
        import httpx

        async with httpx.AsyncClient(timeout=1.0) as client:
            response_qdrant = await client.get(
                f"http://{settings.qdrant_host}:{settings.qdrant_port}/healthz"
            )
            return bool(response_qdrant.is_success)
    except Exception:
        return False


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    status_code=status.HTTP_200_OK,
    summary="Readiness Check",
    description="""
Readiness probe for orchestrators and load balancers.

Returns whether the API is ready to accept traffic. This checks that all
critical dependencies (database, Redis, etc.) are available.

## Kubernetes Usage

```yaml
readinessProbe:
  httpGet:
    path: /ready
    port: 8000
  initialDelaySeconds: 5
  periodSeconds: 10
```

## Response

- `ready: true` - All dependencies available, accept traffic
- `ready: false` - Dependencies unavailable, do not route traffic
    """,
    tags=["Health"],
)
async def readiness_check(
    response: Response,
    db: AsyncSession | None = Depends(get_db_session_dependency),
) -> ReadinessResponse:
    """
    Readiness check endpoint.

    Verifies all dependencies (database, redis, etc.) are available.
    """
    if settings.is_production:
        database_ok = await _database_ready(db)
        redis_ok = await _redis_ready()
        qdrant_ok = await _qdrant_ready()
    else:
        # Outside production the readiness contract is unchanged from before
        # Group 5: dependency reachability is not a readiness condition (local
        # and test runs have no Redis/Qdrant).  Production probes them live.
        database_ok = redis_ok = qdrant_ok = True
    checks = {
        "api": True,
        "database": database_ok,
        "redis": redis_ok,
        "qdrant": qdrant_ok,
        # A production payment rollout is not ready when alert delivery could
        # loop locally or when its rule/smoke digests are absent or malformed.
        "billing_observability": not settings.is_production
        or (
            settings.is_billing_observability_compatible
            and getattr(
                settings, "is_billing_runtime_acknowledgement_compatible", False
            )
        ),
    }

    all_ready = all(checks.values())
    if not all_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    message = None if all_ready else "Some dependencies are not available"

    return ReadinessResponse(
        ready=all_ready,
        checks=checks,
        message=message,
    )


@router.get(
    "/live",
    response_model=LivenessResponse,
    status_code=status.HTTP_200_OK,
    summary="Liveness Check",
    description="""
Simple liveness probe for orchestrators.

Returns OK if the process is alive and responding. This does NOT check
dependencies - it only verifies the process hasn't hung.

## Kubernetes Usage

```yaml
livenessProbe:
  httpGet:
    path: /live
    port: 8000
  initialDelaySeconds: 15
  periodSeconds: 20
```

If this endpoint fails, the orchestrator should restart the container.
    """,
    tags=["Health"],
)
async def liveness_check() -> LivenessResponse:
    """
    Liveness check endpoint.

    Simple endpoint that returns OK if the process is alive.
    """
    return LivenessResponse(
        alive=True,
        timestamp=datetime.utcnow().isoformat(),
    )


@router.get(
    "/metrics/summary",
    response_model=MetricsResponse,
    status_code=status.HTTP_200_OK,
    summary="Metrics Summary",
    description="""
Get a summary of key metrics in JSON format.

For full Prometheus-format metrics, use the `/metrics` endpoint.

This endpoint provides a quick overview of:
- Request counts and rates
- Latency percentiles
- Error rates
- Active sessions
    """,
    tags=["Health"],
)
async def metrics_summary() -> MetricsResponse:
    """
    Get a summary of key metrics.
    """
    # TODO: Implement actual metrics collection
    return MetricsResponse(
        requests_total=0,
        requests_per_minute=0.0,
        avg_latency_ms=0.0,
        p95_latency_ms=0.0,
        p99_latency_ms=0.0,
        error_rate=0.0,
        active_sessions=0,
        events_processed_today=0,
    )
