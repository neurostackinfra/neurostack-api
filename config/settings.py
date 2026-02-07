"""
NeuroStack API - Settings Configuration
Centralized configuration using Pydantic Settings
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ============================================
    # Application
    # ============================================
    app_name: str = "NeuroStack API"
    app_version: str = "0.1.0"
    environment: Literal["development", "staging", "production", "test"] = "development"
    debug: bool = False
    api_prefix: str = "/v1"

    # ============================================
    # Server
    # ============================================
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = Field(default=1, ge=1, le=32)
    reload: bool = False

    # ============================================
    # Security
    # ============================================
    secret_key: str = Field(
        default="CHANGE_ME_IN_PRODUCTION",
        min_length=32,
        description="Secret key for JWT signing",
    )
    api_key_header: str = "X-API-Key"
    allowed_origins: list[str] = ["*"]
    allowed_hosts: list[str] = ["*"]

    # ============================================
    # Database (PostgreSQL)
    # ============================================
    database_url: str = "postgresql+asyncpg://neurostack:neurostack@localhost:5432/neurostack"
    database_pool_size: int = Field(default=5, ge=1, le=100)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    database_pool_timeout: int = Field(default=30, ge=1)

    # ============================================
    # Redis (State Store)
    # ============================================
    redis_url: str = "redis://localhost:6379/0"
    redis_max_connections: int = Field(default=10, ge=1, le=100)
    redis_socket_timeout: float = Field(default=5.0, ge=0.1)

    # ============================================
    # Qdrant (Vector Database)
    # ============================================
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_grpc_port: int = 6334
    qdrant_collection_name: str = "neurostack_knowledge"

    # ============================================
    # Session Configuration
    # ============================================
    session_ttl_seconds: int = Field(default=86400, ge=60)  # 24 hours
    session_max_messages: int = Field(default=50, ge=10, le=200)

    # ============================================
    # Rate Limiting
    # ============================================
    rate_limit_enabled: bool = True
    rate_limit_requests_per_minute: int = Field(default=60, ge=1)
    rate_limit_burst: int = Field(default=10, ge=1)

    # ============================================
    # Logging
    # ============================================
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_format: Literal["json", "console"] = "json"
    log_include_request_body: bool = False

    # ============================================
    # Observability
    # ============================================
    tracing_enabled: bool = False
    tracing_sample_rate: float = Field(default=0.1, ge=0.0, le=1.0)
    metrics_enabled: bool = True
    metrics_port: int = 9090

    # ============================================
    # SRM Configuration
    # ============================================
    srm_confidence_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    srm_safety_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    srm_handoff_max_turns: int = Field(default=10, ge=1, le=50)

    # ============================================
    # Prompt Engine
    # ============================================
    prompt_max_context_tokens: int = Field(default=4000, ge=100)
    prompt_max_recent_messages: int = Field(default=10, ge=1, le=50)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        """Ensure database URL uses asyncpg driver."""
        if not v.startswith("postgresql+asyncpg://"):
            v = v.replace("postgresql://", "postgresql+asyncpg://")
        return v

    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        """Check if running in development environment."""
        return self.environment == "development"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


# Global settings instance
settings = get_settings()

