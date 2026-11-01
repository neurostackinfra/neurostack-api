"""
NeuroStack API - Settings Configuration
Centralized configuration using Pydantic Settings
"""

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from datetime import datetime, timezone
from functools import lru_cache
from ipaddress import ip_address
from pathlib import Path
from typing import Final, Literal
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.core.exceptions import ConfigurationError

ASAAS_API_BASE_URL: Final = "https://api.asaas.com/v3"
ASAAS_AUTH_HEADER: Final = "access_token"
ASAAS_DEFAULT_USER_AGENT: Final = "NeuroStack/asaas-payment-gateway"


def _is_loopback_http_origin(value: str) -> bool:
    """Return whether a URL is a plain HTTP loopback origin."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        return False
    if port is not None and not 1 <= port <= 65535:
        return False
    hostname = parsed.hostname.lower()
    if hostname == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def _is_plain_https_page_url(value: str) -> bool:
    """Return whether a URL is an absolute HTTPS page with no hidden parts."""

    if (
        len(value) > 500
        or not value.isascii()
        or any(ord(character) <= 32 or ord(character) == 127 for character in value)
    ):
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "?" in value
        or "#" in value
    ):
        return False
    return port is None or 1 <= port <= 65535


def _is_safe_https_receiver(value: str) -> bool:
    """Return whether an Alertmanager receiver is HTTPS and non-loopback."""

    if not value or len(value) > 500 or not value.isascii():
        return False
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port is not None
        and not 1 <= port <= 65535
    ):
        return False
    hostname = parsed.hostname.lower()
    if hostname == "localhost":
        return False
    try:
        return not ip_address(hostname).is_loopback
    except ValueError:
        return True


def _is_kms_key_arn(value: str) -> bool:
    """Accept immutable KMS key ARNs, never aliases or arbitrary strings."""

    parts = value.split(":", 5)
    if len(parts) != 6:
        return False
    arn, partition, service, region, account_id, resource = parts
    return (
        arn == "arn"
        and partition in {"aws", "aws-us-gov", "aws-cn"}
        and service == "kms"
        and bool(region)
        and len(account_id) == 12
        and account_id.isdecimal()
        and resource.startswith("key/")
        and bool(resource.removeprefix("key/"))
        and not resource.startswith("key/alias/")
    )


def _is_sha256_digest(value: str) -> bool:
    return len(value) == 64 and all(
        character in "0123456789abcdef" for character in value
    )


def _file_matches_sha256(path: Path, expected_digest: str) -> bool:
    """Bind readiness evidence to a real local regular file, fail closed."""

    if not _is_sha256_digest(expected_digest):
        return False
    try:
        metadata = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            return False
        if metadata.st_size <= 0 or metadata.st_size > 2_000_000:
            return False
        observed = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return False
    return observed == expected_digest


def runtime_acknowledgement_challenge_digest(
    *, nonce: str, receiver_url: str, rules_digest: str, smoke_digest: str
) -> str:
    """Digest the backend-issued challenge that the receiver must sign."""

    payload = {
        "nonce": nonce,
        "receiver_url": receiver_url,
        "rules_digest": rules_digest,
        "smoke_digest": smoke_digest,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _runtime_acknowledgement_signed_bytes(evidence: dict[str, object]) -> bytes:
    signed = {
        "acknowledged": evidence["acknowledged"],
        "challenge_digest": evidence["challenge_digest"],
        "nonce": evidence["nonce"],
        "observed_at": evidence["observed_at"],
        "receipt_id": evidence["receipt_id"],
        "receiver_url": evidence["receiver_url"],
        "rules_digest": evidence["rules_digest"],
        "smoke_digest": evidence["smoke_digest"],
    }
    return json.dumps(signed, sort_keys=True, separators=(",", ":")).encode("ascii")


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        hide_input_in_errors=True,
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
    # Built frontend served by the API at exact paths (src/api/spa.py). An
    # empty/absent directory or one without index.html serves nothing.
    spa_enabled: bool = True
    spa_dist_dir: str = ""

    # ============================================
    # Server
    # ============================================
    host: str = "0.0.0.0"
    port: int = 8000
    workers: int = Field(default=1, ge=1, le=32)
    reload: bool = False

    # ============================================
    # AWS AppConfig Agent (localhost cache)
    # ============================================
    appconfig_enabled: bool = False
    appconfig_origin: str = "http://127.0.0.1:2772"
    appconfig_application: str = ""
    appconfig_environment: str = ""
    appconfig_profile: str = ""
    appconfig_token: SecretStr = SecretStr("")
    # Where the egress flags are read: ``agent`` is the localhost Agent above;
    # ``sdk`` reads AppConfig Data in-process (F2; ORIGIN and TOKEN unused).
    appconfig_source: Literal["agent", "sdk"] = "agent"

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
    database_max_overflow: int = Field(default=0, ge=0, le=100)
    database_pool_timeout: int = Field(default=30, ge=1)

    @field_validator(
        "database_pool_size",
        "database_max_overflow",
        "database_pool_timeout",
        mode="before",
    )
    @classmethod
    def validate_database_pool_integer(cls, value: object) -> object:
        """Reject booleans, floats, signs, whitespace, and other coercions."""
        if type(value) is int:
            return value
        if isinstance(value, str) and value and value.isascii() and value.isdecimal():
            return value
        raise ValueError("database pool settings must be unsigned base-10 integers")

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
    # Media resource budgets (per process)
    # ============================================
    # These are intentionally reduction-only knobs. Raising either ceiling
    # changes the reviewed memory/database budget and requires a code change.
    media_max_concurrent_uploads: int = Field(default=4, ge=1, le=4)
    media_max_concurrent_downloads: int = Field(default=8, ge=1, le=8)

    @field_validator(
        "media_max_concurrent_uploads",
        "media_max_concurrent_downloads",
        mode="before",
    )
    @classmethod
    def validate_media_concurrency_limit(cls, value: object) -> int:
        """Accept integer environment text without Pydantic's float coercion."""
        if isinstance(value, bool):
            raise ValueError("media concurrency limit must be an integer")
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isascii() and value.isdecimal():
            return int(value)
        raise ValueError("media concurrency limit must be an integer")

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
    # ASAAS (Payment Gateway)
    # ============================================
    # The production API origin and authentication header are code constants;
    # deployment input cannot redirect payment credentials to another host.
    asaas_api_key: SecretStr = SecretStr("")
    # Staged rotation material is never selected automatically.  A local
    # receipt/proof must demonstrate next->current before an operator changes
    # the deployment secret outside this codebase.
    asaas_api_key_next: SecretStr = SecretStr("")
    asaas_webhook_token: SecretStr = SecretStr("")
    asaas_user_agent: str = ASAAS_DEFAULT_USER_AGENT
    # Browser return page for hosted Checkout callbacks. Navigation only: it
    # never changes financial state. Empty keeps checkout creation unavailable.
    asaas_checkout_return_url: str = ""
    # Public HTTPS URL of ``/v1/billing/webhook/asaas`` and the operator address
    # ASAAS notifies about queue problems.  Both only drive the provider-side
    # webhook provisioning/readback; empty keeps hosted checkout unavailable
    # because the webhook can never read back as ready.
    asaas_webhook_url: str = ""
    asaas_webhook_notification_email: str = ""
    # Protected switch for automatic ``abandoned`` of ambiguous checkouts.
    # Default off: the evidence (empty reads by our session UUID as ASAAS
    # ``externalReference``) is only sound once Group 8 proves by readback
    # that ASAAS copies a Checkout's externalReference to the subscription and
    # payments it creates.  Off keeps such sessions blocking (operator action).
    asaas_auto_abandon_enabled: bool = False
    # Local control-plane authentication.  Only the backend holds it: it
    # signs each operator receipt it records (operators never send or see a
    # signature), never provider payloads, customer data, or API credentials.
    billing_operation_receipt_signing_key: SecretStr = SecretStr("")
    # Longest lifetime of an operator assignment/receipt, measured from its
    # creation (``expires_at - now``; exactly the cap is accepted).  Readiness
    # rows cannot outlive the receipts they name, so they inherit it.
    billing_operation_receipt_max_ttl_seconds: int = Field(
        default=86400, gt=0, le=604800
    )

    @field_validator("billing_operation_receipt_max_ttl_seconds", mode="before")
    @classmethod
    def validate_receipt_ttl_integer(cls, value: object) -> object:
        """Accept a base-10 integer only (no bool, float, sign or blank)."""
        if type(value) is int:
            return value
        if isinstance(value, str) and value and value.isascii() and value.isdecimal():
            return value
        raise ValueError("must be an unsigned base-10 integer of seconds")

    # Group 7b canonical producer signing and verification configuration.
    billing_receipt_signing_key: SecretStr = SecretStr("")
    billing_receipt_verification_public_key: str = ""
    billing_deploy_authorization_public_key: str = ""
    billing_build_attestation_public_key: str = ""
    billing_producer_grant_issuer: str = ""
    billing_producer_grant_kid: str = ""
    billing_producer_grant_audience: str = ""
    billing_producer_grant_public_key: str = ""
    billing_producer_build_issuer: str = ""
    billing_producer_build_kid: str = ""
    billing_producer_build_audience: str = ""
    billing_producer_build_public_key: str = ""
    billing_producer_build_repository_id: str = ""
    billing_producer_build_repository_name: str = ""
    billing_producer_build_workflow_path: str = ""
    billing_producer_build_workflow_ref: str = ""
    billing_producer_build_oidc_issuer: str = ""
    billing_producer_build_oidc_subject: str = ""
    billing_producer_receipt_issuer: str = ""
    billing_producer_receipt_kid: str = ""
    billing_producer_receipt_audience: str = ""
    billing_producer_receipt_public_key: str = ""
    billing_fact_schema_digest: str = ""
    billing_prometheus_rules_digest: str = ""
    billing_prometheus_rules_path: Path = Path("config/prometheus_rules.yml")
    billing_observability_smoke_digest: str = ""
    billing_observability_smoke_path: Path = Path(
        "config/billing-observability-smoke.json"
    )
    billing_observability_smoke_token: SecretStr = SecretStr("")
    billing_alert_receiver_url: str = ""
    billing_observability_acknowledgement: str = ""
    billing_observability_acknowledgement_path: Path = Path("")
    billing_observability_acknowledgement_digest: str = ""
    # The challenge is backend/deployment-owned; the receiver signs a receipt
    # for this exact nonce and digest.  It is not accepted from the receipt.
    billing_observability_challenge_nonce: str = ""
    billing_observability_challenge_digest: str = ""
    billing_observability_acknowledgement_public_key: str = ""
    # billing.fact.v1 outbox worker (Group 9).  Off by default: when false no
    # loop, task or database work is started.  When true every API worker
    # process runs one loop; rows are claimed FOR UPDATE SKIP LOCKED and the
    # reconcile/escalation phase is serialized by a PostgreSQL advisory lock.
    billing_fact_worker_enabled: bool = False
    billing_fact_worker_interval_seconds: int = Field(default=30, ge=5, le=300)
    billing_fact_worker_batch_size: int = Field(default=50, ge=1, le=500)
    # Presentations without an ack before the delivery becomes ``blocked``.
    billing_fact_worker_max_attempts: int = Field(default=10, ge=1, le=50)
    # A pending/awaiting_ack fact older than this raises the stale alert.
    billing_fact_worker_stale_after_seconds: int = Field(default=900, ge=60, le=86_400)
    # The worker's consumer transport (asaas-rollout-opening Group 2c).  Off by
    # default: the worker gets transport=None and the credit consumer module
    # is never imported.  On, with the worker on, src/api/main.py hands it the
    # credit consumer behind a readiness hook: while the credit switch is not
    # effective or the image fails the consumer's contract check, a
    # presentation is turned away without spending an attempt.
    billing_fact_transport_enabled: bool = False
    # Credit dominance switch (neurostack-credit-system Group 1b).  Off by
    # default: every credit surface stays inert and no refresh task starts.
    # When true it is EFFECTIVE only while alembic_version holds one row at
    # or after the image's highest credit migration, read at start-up and
    # every credit_switch_refresh_seconds; any read error keeps it off
    # (src/services/credit_switch.py).  On is observational only.
    credit_hot_path_enabled: bool = False
    credit_switch_refresh_seconds: int = Field(default=60, ge=1, le=300)
    # How far a credit ledger writer's clock may stray from the database
    # clock (owner decisions, 2026-10-26: 5 minutes, at most 10): further
    # ahead acts at database clock + tolerance (with a warning), further
    # behind at database clock - tolerance; a builder whose ledger clock is
    # already beyond database clock + tolerance is refused without writing
    # anything until the database clock catches up
    # (src/services/credit_ledger.py, CreditLedger).
    credit_clock_tolerance_seconds: int = Field(default=300, ge=1, le=600)
    # How long the stored answer of an enforced POST /v1/events decision
    # stays replayable (owner decision OWNER-d, 2026-10-27: 24 hours by
    # default, at most 168).  An older response-outbox row is read as an
    # empty payload and a retry answers 409 duplicate_event
    # (src/services/credit_metering.py); validated at start-up.
    credit_replay_retention_hours: int = Field(default=24, ge=1, le=168)
    # Group 4b's additional (never authoritative) development/test object
    # copy.  Empty values intentionally disable the workers even when the
    # credit dominance switch is effective.  Production is rejected by the
    # worker predicate, not by a caller-provided flag.
    credit_object_store_endpoint: str = ""
    credit_object_store_bucket: str = ""
    credit_object_store_access_key_id: str = ""
    credit_object_store_secret_access_key: SecretStr = SecretStr("")
    credit_object_worker_interval_seconds: int = Field(default=15, ge=1, le=300)
    # Group 5 lifecycle authority.  The signing key is deliberately server
    # configuration only: neither a request nor a persisted record can select
    # or replace it.  Empty/short keys fail closed in the lifecycle service.
    credit_authorization_signing_key: SecretStr = SecretStr("")
    # A settled subscription remains covered only for this bounded renewal
    # interval.  The lifecycle helper, rather than callers, owns its meaning.
    credit_binding_grace_days: int = Field(default=5, ge=0, le=31)
    credit_lifecycle_worker_interval_seconds: int = Field(default=60, ge=5, le=3600)
    # Group 7, deliverable 9: the ONLY source of the credit
    # ``production-serving`` role -- exact (environment, ECS cluster ARN, ECS
    # service ARN) triples injected by configuration (env JSON, e.g.
    # ``[["staging", "arn:...:cluster/x", "arn:...:service/x/y"]]``).  Empty
    # by default: nothing derives.  Never read from AppConfig and never from
    # ``is_production`` (false in real production, ENVIRONMENT=staging).  The
    # real triples are pinned by the rollout-opening wish, not here.
    credit_production_serving_allowlist: tuple[tuple[str, str, str], ...] = ()

    @field_validator("credit_production_serving_allowlist")
    @classmethod
    def validate_credit_production_serving_allowlist(
        cls, value: tuple[tuple[str, str, str], ...]
    ) -> tuple[tuple[str, str, str], ...]:
        """Exact, well-formed triples only; a malformed one fails start-up."""

        for environment, cluster_arn, service_arn in value:
            if environment not in ("development", "staging", "production", "test"):
                raise ValueError(
                    "CREDIT_PRODUCTION_SERVING_ALLOWLIST environment is unknown"
                )
            if not cluster_arn.startswith("arn:") or ":cluster/" not in cluster_arn:
                raise ValueError(
                    "CREDIT_PRODUCTION_SERVING_ALLOWLIST needs an ECS cluster ARN"
                )
            if not service_arn.startswith("arn:") or ":service/" not in service_arn:
                raise ValueError(
                    "CREDIT_PRODUCTION_SERVING_ALLOWLIST needs an ECS service ARN"
                )
        return value

    @field_validator("asaas_webhook_url")
    @classmethod
    def validate_asaas_webhook_url(cls, value: str) -> str:
        """Accept only an absent value or the exact public receiver path."""

        if not value:
            return value
        if not _is_plain_https_page_url(value) or not value.endswith(
            "/v1/billing/webhook/asaas"
        ):
            raise ValueError(
                "ASAAS_WEBHOOK_URL must be the HTTPS URL of /v1/billing/webhook/asaas"
            )
        return value

    @field_validator("asaas_checkout_return_url")
    @classmethod
    def validate_asaas_checkout_return_url(cls, value: str) -> str:
        """Accept only an absent value or a plain absolute HTTPS page URL."""

        if not value:
            return value
        if not _is_plain_https_page_url(value):
            raise ValueError(
                "ASAAS_CHECKOUT_RETURN_URL must be an absolute HTTPS URL without "
                "userinfo, query, or fragment"
            )
        return value

    @field_validator("asaas_api_key")
    @classmethod
    def validate_asaas_api_key(cls, value: SecretStr) -> SecretStr:
        """Accept only an absent key or the ASAAS production-key namespace."""

        raw_value = value.get_secret_value()
        if raw_value and not raw_value.startswith("$aact_prod_"):
            raise ValueError("ASAAS_API_KEY must use the production key namespace")
        return value

    @field_validator("asaas_webhook_token")
    @classmethod
    def validate_asaas_webhook_token(cls, value: SecretStr) -> SecretStr:
        """Reject unsafe token bytes without disclosing the token."""

        if any(ord(character) < 32 for character in value.get_secret_value()):
            raise ValueError("ASAAS_WEBHOOK_TOKEN contains invalid control characters")
        return value

    @field_validator("billing_alert_receiver_url")
    @classmethod
    def validate_billing_alert_receiver_url(cls, value: str) -> str:
        """Reject loopback and non-HTTPS alert targets before readiness."""

        if value and not _is_safe_https_receiver(value):
            raise ValueError(
                "BILLING_ALERT_RECEIVER_URL must be a non-loopback HTTPS URL"
            )
        return value

    @field_validator("asaas_user_agent")
    @classmethod
    def validate_asaas_user_agent(cls, value: str) -> str:
        """Require a stable, identifiable HTTP User-Agent value."""

        if (
            not value.strip()
            or "/" not in value
            or any(ord(character) < 32 or ord(character) > 126 for character in value)
        ):
            raise ValueError("ASAAS_USER_AGENT must be an identifiable ASCII product")
        return value

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
        elif environment == "staging" and v == (
            "dev-secret-key-change-in-production-min-32-chars-required"
        ):
            raise ValueError(
                "SECRET_KEY must be changed from the default value in staging. "
                "Set a secure SECRET_KEY environment variable."
            )
        return v

    @model_validator(mode="after")
    def validate_production_settings(self) -> "Settings":
        """Validate critical settings for production environment."""
        producer_key_arns = (
            self.billing_producer_grant_kid,
            self.billing_producer_build_kid,
            self.billing_producer_receipt_kid,
        )
        configured_producer_keys = tuple(key for key in producer_key_arns if key)
        if configured_producer_keys and (
            len(configured_producer_keys) != len(producer_key_arns)
            or len(set(configured_producer_keys)) != len(producer_key_arns)
            or any(not _is_kms_key_arn(key) for key in configured_producer_keys)
        ):
            raise ValueError(
                "producer grant, build and receipt KMS key ARNs must be configured "
                "and distinct"
            )

        if self.environment == "production":
            errors = []

            if self.appconfig_enabled:
                missing_appconfig = [
                    name
                    for name, value in (
                        ("APPCONFIG_APPLICATION", self.appconfig_application),
                        ("APPCONFIG_ENVIRONMENT", self.appconfig_environment),
                        ("APPCONFIG_PROFILE", self.appconfig_profile),
                    )
                    if not value.strip()
                ]
                if missing_appconfig:
                    errors.append(
                        "AppConfig Agent requires " + ", ".join(missing_appconfig)
                    )
                if not _is_loopback_http_origin(self.appconfig_origin):
                    errors.append(
                        "APPCONFIG_ORIGIN must be an HTTP loopback origin without "
                        "userinfo, path, query, or fragment"
                    )
                if any(
                    ord(character) < 32
                    for character in self.appconfig_token.get_secret_value()
                ):
                    errors.append("APPCONFIG_TOKEN contains invalid control characters")

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

            if not self.is_billing_observability_compatible:
                errors.append(
                    "billing observability requires non-loopback HTTPS receiver, "
                    "rules/smoke digests, and fresh authenticated runtime acknowledgement"
                )
            if len(self.billing_operation_receipt_signing_key.get_secret_value()) < 32:
                errors.append(
                    "BILLING_OPERATION_RECEIPT_SIGNING_KEY must be at least 32 bytes "
                    "in production"
                )
            if not self.billing_observability_smoke_token.get_secret_value().strip():
                errors.append(
                    "BILLING_OBSERVABILITY_SMOKE_TOKEN is required in production"
                )

            if errors:
                error_msg = "Production configuration validation failed:\n" + "\n".join(
                    f"  - {e}" for e in errors
                )
                raise ConfigurationError(error_msg)

        return self

    def validate_runtime_environment(
        self,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        """Require staging's JWT signing key to come from runtime injection."""

        if self.environment != "staging":
            return

        if self.api_prefix != "/v1":
            raise ConfigurationError(
                "Invalid staging API_PREFIX; the canonical prefix is /v1."
            )

        source = os.environ if environ is None else environ
        missing = sorted(
            name
            for name in ("ENVIRONMENT", "SECRET_KEY")
            if not source.get(name, "").strip()
        )
        if missing:
            raise ConfigurationError(
                "Missing required staging runtime settings: " + ", ".join(missing)
            )

        runtime_secret_key = source["SECRET_KEY"]
        if (
            source["ENVIRONMENT"] != "staging"
            or len(runtime_secret_key) < 32
            or runtime_secret_key
            == "dev-secret-key-change-in-production-min-32-chars-required"
            or runtime_secret_key != self.secret_key
        ):
            raise ConfigurationError(
                "Invalid staging SECRET_KEY runtime injection; provide a "
                "non-default key of at least 32 characters matching the "
                "loaded setting."
            )

    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        """Check if running in development environment."""
        return self.environment == "development"

    @property
    def is_asaas_configuration_compatible(self) -> bool:
        """Return whether required ASAAS settings are complete and compatible."""

        api_key = self.asaas_api_key.get_secret_value()
        webhook_token = self.asaas_webhook_token.get_secret_value()
        return bool(
            api_key.startswith("$aact_prod_")
            and webhook_token
            and self.asaas_user_agent.strip()
            and ASAAS_API_BASE_URL == "https://api.asaas.com/v3"
            and ASAAS_AUTH_HEADER == "access_token"
        )

    @property
    def is_billing_observability_compatible(self) -> bool:
        """Readiness input for the production billing control plane."""

        return bool(
            _is_safe_https_receiver(self.billing_alert_receiver_url)
            and _file_matches_sha256(
                self.billing_prometheus_rules_path,
                self.billing_prometheus_rules_digest,
            )
            and _file_matches_sha256(
                self.billing_observability_smoke_path,
                self.billing_observability_smoke_digest,
            )
            and bool(self.billing_observability_smoke_token.get_secret_value().strip())
            and self._runtime_acknowledgement_is_valid()
        )

    @property
    def is_billing_runtime_acknowledgement_compatible(self) -> bool:
        """Expose the dynamic receiver proof as a separate readiness input."""

        return self._runtime_acknowledgement_is_valid()

    def _runtime_acknowledgement_is_valid(self) -> bool:
        """Require a fresh receiver-signed receipt for a live backend challenge."""

        raw: bytes
        try:
            if self.billing_observability_acknowledgement.strip():
                raw = self.billing_observability_acknowledgement.encode("utf-8")
            else:
                path = self.billing_observability_acknowledgement_path
                metadata = path.lstat()
                if (
                    path.is_symlink()
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                    or metadata.st_size <= 0
                    or metadata.st_size > 16_384
                ):
                    return False
                raw = path.read_bytes()
        except (OSError, UnicodeError):
            return False
        if (
            not _is_sha256_digest(self.billing_observability_challenge_digest)
            or not self.billing_observability_challenge_nonce
            or hashlib.sha256(raw).hexdigest()
            != self.billing_observability_acknowledgement_digest
        ):
            return False
        try:
            evidence = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            return False
        if not isinstance(evidence, dict):
            return False
        nonce = evidence.get("nonce")
        observed_at = evidence.get("observed_at")
        signature = evidence.get("signature")
        receipt_id = evidence.get("receipt_id")
        challenge_digest = evidence.get("challenge_digest")
        if (
            not isinstance(nonce, str)
            or nonce != self.billing_observability_challenge_nonce
            or not isinstance(observed_at, str)
            or not isinstance(receipt_id, str)
            or not receipt_id
            or not isinstance(challenge_digest, str)
            or challenge_digest != self.billing_observability_challenge_digest
            or not isinstance(signature, str)
            or len(signature) != 128
            or any(character not in "0123456789abcdef" for character in signature)
            or evidence.get("acknowledged") is not True
            or evidence.get("receiver_url") != self.billing_alert_receiver_url
            or evidence.get("rules_digest") != self.billing_prometheus_rules_digest
            or evidence.get("smoke_digest") != self.billing_observability_smoke_digest
            or challenge_digest
            != runtime_acknowledgement_challenge_digest(
                nonce=nonce,
                receiver_url=self.billing_alert_receiver_url,
                rules_digest=self.billing_prometheus_rules_digest,
                smoke_digest=self.billing_observability_smoke_digest,
            )
        ):
            return False
        try:
            timestamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        now = datetime.now(timezone.utc)
        if (
            timestamp.tzinfo is None
            or timestamp > now
            or (now - timestamp).total_seconds() > 300
        ):
            return False
        signed = dict(evidence)
        signed["observed_at"] = timestamp.astimezone(timezone.utc).isoformat()
        public_key = self.billing_observability_acknowledgement_public_key
        if len(public_key) != 64 or any(
            character not in "0123456789abcdef" for character in public_key
        ):
            return False
        try:
            Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key)).verify(
                bytes.fromhex(signature), _runtime_acknowledgement_signed_bytes(signed)
            )
        except (InvalidSignature, ValueError, TypeError):
            return False
        return True


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


# Global settings instance
settings = get_settings()
