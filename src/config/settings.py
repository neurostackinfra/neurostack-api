"""
NeuroStack API - Settings Configuration
Centralized configuration using Pydantic Settings
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.exceptions import ConfigurationError


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
    docs_enabled: bool = True
    redoc_js_url: str = "https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js"

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
        default="dev-secret-key-change-in-production-min-32-chars-required",
        min_length=32,
        description="Secret key for JWT signing",
    )
    api_key_header: str = "X-API-Key"
    allowed_origins: list[str] = ["*"]
    allowed_hosts: list[str] = ["*"]

    # ============================================
    # Database (PostgreSQL)
    # ============================================
    database_url: str = (
        "postgresql+asyncpg://neurostack:neurostack@localhost:5434/neurostack"
    )
    database_pool_size: int = Field(default=5, ge=1, le=100)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    database_pool_timeout: int = Field(default=30, ge=1)

    # ============================================
    # Redis (State Store)
    # ============================================
    redis_url: str = "redis://localhost:6380/0"
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

    # ============================================
    # SMTP (Support Email)
    # ============================================
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    support_email_to: str = "info@neurostack.global"

    # ============================================
    # Public base URL (for LGPD privacy links, etc.)
    # ============================================
    public_base_url: str = ""

    # ============================================
    # AbacatePay (Payment Gateway)
    # ============================================
    abacatepay_api_key: str = ""
    # ABACATEPAY_WEBHOOK_SECRET: string gerada pela AbacatePay ao cadastrar o endpoint
    # webhook em app.abacatepay.com → Webhooks → Criar endpoint → copiar o "Secret"
    # NÃO colocar a URL do webhook aqui — apenas o token/secret HMAC.
    abacatepay_webhook_secret: str = ""

    # ============================================
    # Subscription plan usage limits
    # ============================================
    # The product currently sells only the Start plan. Keep its entitlements
    # configurable so new commercial limits do not require a code release.
    start_plan_monthly_trace_limit: int = Field(default=10_000, ge=0)
    start_plan_rag_storage_limit_mb: int = Field(default=500, ge=0)
    start_plan_media_storage_limit_mb: int = Field(default=500, ge=0)

    # ============================================
    # LLM Provider (OpenAI-compatible)
    # ============================================
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = "o4-mini"
    llm_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    llm_max_tokens: int = Field(default=4096, ge=64, le=16384)

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        """Ensure database URL uses asyncpg driver."""
        if not v.startswith("postgresql+asyncpg://"):
            v = v.replace("postgresql://", "postgresql+asyncpg://")
        return v

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key(cls, v: str, info: ValidationInfo) -> str:
        """Validate secret key strength."""
        # Get environment from info if available
        environment = (
            info.data.get("environment", "development")
            if hasattr(info, "data")
            else "development"
        )

        if environment == "production":
            if len(v) < 32:
                raise ValueError(
                    "SECRET_KEY must be at least 32 characters in production. "
                    "Generate a secure key using: python -c 'import secrets; print(secrets.token_urlsafe(32))'"
                )
            if v == "dev-secret-key-change-in-production-min-32-chars-required":
                raise ValueError(
                    "SECRET_KEY must be changed from default value in production. "
                    "Set a secure SECRET_KEY environment variable."
                )
        return v

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        """Validate critical settings for production environment."""
        if self.environment == "production":
            errors = []

            # Check secret key
            if (
                self.secret_key
                == "dev-secret-key-change-in-production-min-32-chars-required"
            ):
                errors.append(
                    "SECRET_KEY must be changed from default value in production"
                )

            # Check database URL is not default
            if "localhost" in self.database_url or "127.0.0.1" in self.database_url:
                errors.append(
                    "DATABASE_URL should not point to localhost in production"
                )

            # Check debug is disabled
            if self.debug:
                errors.append("DEBUG must be False in production")

            # Check allowed origins
            if "*" in self.allowed_origins:
                errors.append(
                    "ALLOWED_ORIGINS should not contain '*' in production for security"
                )

            if errors:
                error_msg = "Production configuration validation failed:\n" + "\n".join(
                    f"  - {e}" for e in errors
                )
                raise ConfigurationError(error_msg)

        return self

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
