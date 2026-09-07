"""
NeuroStack API - Health Check Schemas
Contracts for health, readiness, and liveness endpoints.
"""

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class DependencyStatus(str, Enum):
    """
    Status of a dependency.

    - healthy: Dependency is working correctly
    - degraded: Dependency is working but with issues
    - unhealthy: Dependency is not working
    - unknown: Status could not be determined
    """

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class ComponentHealth(BaseModel):
    """
    Health status of a single component/dependency.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "database",
                "status": "healthy",
                "latency_ms": 5,
                "message": None,
                "details": {"connection_pool": 5, "active_connections": 2},
            }
        }
    )

    name: str = Field(
        ...,
        description="Component/dependency name",
        json_schema_extra={"example": "database"},
    )
    status: DependencyStatus = Field(
        ...,
        description="Health status",
        json_schema_extra={"example": "healthy"},
    )
    latency_ms: Optional[int] = Field(
        default=None,
        ge=0,
        description="Response latency in milliseconds",
        json_schema_extra={"example": 5},
    )
    message: Optional[str] = Field(
        default=None,
        description="Optional status message",
    )
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional health details",
    )


class HealthResponse(BaseModel):
    """
    Comprehensive health check response.

    Returns overall system health status with details
    about individual components.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "healthy",
                "version": "0.1.0",
                "environment": "development",
                "timestamp": "2025-12-31T10:30:00Z",
                "uptime_seconds": 3600,
                "components": [
                    {"name": "api", "status": "healthy", "latency_ms": None},
                    {"name": "database", "status": "healthy", "latency_ms": 5},
                    {"name": "redis", "status": "healthy", "latency_ms": 1},
                    {"name": "qdrant", "status": "healthy", "latency_ms": 3},
                ],
            }
        }
    )

    status: str = Field(
        ...,
        description="Overall health status (healthy, degraded, unhealthy)",
        json_schema_extra={"example": "healthy"},
    )
    version: str = Field(
        ...,
        description="API version",
        json_schema_extra={"example": "0.1.0"},
    )
    environment: str = Field(
        ...,
        description="Environment (development, staging, production)",
        json_schema_extra={"example": "development"},
    )
    timestamp: str = Field(
        ...,
        description="ISO 8601 timestamp of the health check",
        json_schema_extra={"example": "2025-12-31T10:30:00Z"},
    )
    uptime_seconds: Optional[int] = Field(
        default=None,
        ge=0,
        description="API uptime in seconds",
        json_schema_extra={"example": 3600},
    )
    components: list[ComponentHealth] = Field(
        default_factory=list,
        description="Health status of individual components",
    )


class ReadinessResponse(BaseModel):
    """
    Readiness check response.

    Indicates whether the API is ready to accept traffic.
    Used by load balancers and orchestrators (e.g., Kubernetes).
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "ready": True,
                "checks": {
                    "api": True,
                    "database": True,
                    "redis": True,
                    "qdrant": True,
                },
                "message": None,
            }
        }
    )

    ready: bool = Field(
        ...,
        description="Whether the API is ready to accept traffic",
    )
    checks: dict[str, bool] = Field(
        ...,
        description="Status of individual dependency checks",
        json_schema_extra={
            "example": {
                "api": True,
                "database": True,
                "redis": True,
                "qdrant": True,
            }
        },
    )
    message: Optional[str] = Field(
        default=None,
        description="Optional message about readiness status",
    )


class LivenessResponse(BaseModel):
    """
    Liveness check response.

    Simple check that the process is alive.
    Used by orchestrators to detect hung processes.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "status": "alive",
                "alive": True,
                "timestamp": "2025-12-31T10:30:00Z",
            }
        }
    )

    status: str = Field(
        default="alive",
        description="Status string for compatibility",
    )
    alive: bool = Field(
        default=True,
        description="Whether the process is alive",
    )
    timestamp: str = Field(
        ...,
        description="ISO 8601 timestamp",
        json_schema_extra={"example": "2025-12-31T10:30:00Z"},
    )


class MetricsResponse(BaseModel):
    """
    Basic metrics response.

    For detailed metrics, use the /metrics endpoint (Prometheus format).
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "requests_total": 10000,
                "requests_per_minute": 150,
                "avg_latency_ms": 45,
                "p95_latency_ms": 120,
                "p99_latency_ms": 180,
                "error_rate": 0.001,
                "active_sessions": 250,
                "events_processed_today": 5000,
            }
        }
    )

    requests_total: int = Field(
        default=0,
        ge=0,
        description="Total requests processed since startup",
    )
    requests_per_minute: float = Field(
        default=0.0,
        ge=0.0,
        description="Current requests per minute",
    )
    avg_latency_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="Average response latency in milliseconds",
    )
    p95_latency_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="95th percentile latency",
    )
    p99_latency_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="99th percentile latency",
    )
    error_rate: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Error rate (0.0 to 1.0)",
    )
    active_sessions: int = Field(
        default=0,
        ge=0,
        description="Number of active sessions",
    )
    events_processed_today: int = Field(
        default=0,
        ge=0,
        description="Events processed since midnight UTC",
    )
