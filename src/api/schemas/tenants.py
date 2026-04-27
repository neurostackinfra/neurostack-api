"""
NeuroStack API - Tenants Schemas
Contracts for tenant management endpoints (/v1/tenants).
"""

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from src.models.clinic_config import (
    ClinicConfig,
    ClinicHandoff,
    ClinicPersona,
    ClinicPricing,
    ClinicScheduling,
    ClinicPostCare,
    SectorDefaults,
)


class TenantFeatures(BaseModel):
    """
    Feature flags for a tenant.
    
    Controls which features are enabled for the tenant.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "rag_enabled": True,
                "handoff_enabled": True,
                "safety_strict_mode": False,
                "analytics_enabled": True,
            }
        }
    )

    rag_enabled: bool = Field(
        default=True,
        description="Whether RAG (knowledge retrieval) is enabled",
    )
    handoff_enabled: bool = Field(
        default=True,
        description="Whether human handoff is enabled",
    )
    safety_strict_mode: bool = Field(
        default=False,
        description="Enable strict safety mode (more conservative)",
    )
    analytics_enabled: bool = Field(
        default=True,
        description="Whether analytics collection is enabled",
    )


class TenantBranding(BaseModel):
    """
    Branding configuration for a tenant.
    
    Customizes the AI agent's persona for the tenant.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "assistant_name": "Ana",
                "tone": "professional",
                "greeting": "Olá! Sou a Ana, assistente virtual da Clínica Saúde.",
            }
        }
    )

    assistant_name: Optional[str] = Field(
        default=None,
        max_length=100,
        description="Name of the AI assistant",
    )
    tone: Optional[str] = Field(
        default=None,
        max_length=50,
        description="Tone of voice (professional, friendly, formal)",
    )
    greeting: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Default greeting message",
    )
    farewell: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Default farewell message",
    )


class TenantConfig(BaseModel):
    """
    Complete tenant configuration.
    
    Contains all configurable settings for a tenant including
    rate limits, session settings, features, and branding.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "description": "Clínica de Dermatologia Premium",
                "rate_limit": 100,
                "session_ttl_seconds": 86400,
                "max_sessions_per_user": 10,
                "webhook_url": "https://api.clinic.com/webhooks/neurostack",
                "features": {
                    "rag_enabled": True,
                    "handoff_enabled": True,
                },
                "branding": {
                    "assistant_name": "Ana",
                    "tone": "professional",
                },
            }
        }
    )

    description: str = Field(
        default="",
        max_length=1000,
        description="Tenant description",
    )
    rate_limit: int = Field(
        default=60,
        ge=1,
        le=10000,
        description="Requests per minute limit",
    )
    session_ttl_seconds: int = Field(
        default=86400,
        ge=60,
        le=604800,  # Max 7 days
        description="Session time-to-live in seconds",
    )
    max_sessions_per_user: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Maximum concurrent sessions per user",
    )
    webhook_url: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Webhook URL for handoff and safety notifications",
    )
    features: TenantFeatures = Field(
        default_factory=TenantFeatures,
        description="Feature flags",
    )
    branding: TenantBranding = Field(
        default_factory=TenantBranding,
        description="Branding configuration",
    )
    custom_settings: dict[str, Any] = Field(
        default_factory=dict,
        description="Custom tenant-specific settings",
    )
    clinic_config: Optional[ClinicConfig] = Field(
        default=None,
        description="ClinicConfig (doc MVP v0.5): persona, scheduling, pricing, post_care, handoff sector_defaults",
    )


class TenantResponse(BaseModel):
    """
    Tenant response model.
    
    Complete tenant information returned from API endpoints.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "name": "Clínica Saúde Premium",
                "slug": "clinica-saude-premium",
                "is_active": True,
                "config": {
                    "description": "Clínica de Dermatologia",
                    "rate_limit": 100,
                    "session_ttl_seconds": 86400,
                },
                "created_at": "2025-12-01T10:00:00Z",
                "updated_at": "2025-12-31T10:00:00Z",
                "stats": {
                    "active_sessions": 15,
                    "total_events": 1250,
                    "policies_count": 3,
                },
            }
        }
    )

    id: UUID = Field(
        ...,
        description="Unique tenant identifier",
    )
    name: str = Field(
        ...,
        description="Tenant name",
    )
    slug: Optional[str] = Field(
        default=None,
        description="URL-friendly identifier",
    )
    is_active: bool = Field(
        default=True,
        description="Whether tenant is active",
    )
    config: TenantConfig = Field(
        ...,
        description="Tenant configuration",
    )
    created_at: str = Field(
        ...,
        description="ISO 8601 creation timestamp",
    )
    updated_at: str = Field(
        ...,
        description="ISO 8601 last update timestamp",
    )
    stats: Optional[dict[str, Any]] = Field(
        default=None,
        description="Optional statistics about the tenant",
    )


class TenantCreateRequest(BaseModel):
    """
    Request to create a new tenant.
    
    Tenants are builders or clinics that use NeuroStack.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "Clínica Saúde Premium",
                "slug": "clinica-saude-premium",
                "config": {
                    "description": "Clínica de Dermatologia Premium",
                    "rate_limit": 100,
                    "webhook_url": "https://api.clinic.com/webhooks/neurostack",
                },
            }
        }
    )

    name: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Tenant name",
    )
    slug: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9-]+$",
        description="URL-friendly identifier (lowercase alphanumeric with hyphens)",
    )
    config: TenantConfig = Field(
        default_factory=TenantConfig,
        description="Tenant configuration",
    )


class TenantUpdateRequest(BaseModel):
    """
    Request to update an existing tenant.
    
    All fields are optional - only provided fields will be updated.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "name": "New Tenant Name",
                "config": {
                    "rate_limit": 200,
                },
                "is_active": True,
            }
        }
    )

    name: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=255,
        description="New tenant name",
    )
    slug: Optional[str] = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9-]+$",
        description="New slug",
    )
    config: Optional[TenantConfig] = Field(
        default=None,
        description="Updated configuration (merged with existing)",
    )
    is_active: Optional[bool] = Field(
        default=None,
        description="Activate/deactivate tenant",
    )


class TenantListResponse(BaseModel):
    """
    Response for tenant list with pagination.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "tenants": [],
                "total": 25,
                "page": 1,
                "page_size": 20,
                "has_next": True,
                "has_previous": False,
            }
        }
    )

    tenants: list[TenantResponse] = Field(
        ...,
        description="List of tenants",
    )
    total: int = Field(
        ...,
        ge=0,
        description="Total number of tenants",
    )
    page: int = Field(
        ...,
        ge=1,
        description="Current page number",
    )
    page_size: int = Field(
        ...,
        ge=1,
        description="Number of items per page",
    )
    has_next: bool = Field(
        ...,
        description="Whether there is a next page",
    )
    has_previous: bool = Field(
        ...,
        description="Whether there is a previous page",
    )


class TenantStatsResponse(BaseModel):
    """
    Detailed statistics for a tenant.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "sessions": {
                    "active": 15,
                    "total": 1250,
                    "avg_duration_seconds": 180,
                },
                "events": {
                    "total": 5000,
                    "messages": 4000,
                    "safety_triggers": 50,
                    "handoffs": 25,
                },
                "funnel": {
                    "awareness": 500,
                    "consideration": 400,
                    "decision": 200,
                    "retention": 150,
                },
                "period": "last_30_days",
                "computed_at": "2025-12-31T10:00:00Z",
            }
        }
    )

    tenant_id: UUID = Field(..., description="Tenant identifier")
    sessions: dict[str, Any] = Field(..., description="Session statistics")
    events: dict[str, Any] = Field(..., description="Event statistics")
    funnel: dict[str, int] = Field(..., description="Funnel stage distribution")
    period: str = Field(..., description="Time period for statistics")
    computed_at: str = Field(..., description="When statistics were computed")


class APIKeyResponse(BaseModel):
    """
    Response containing API key (only shown on creation).
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "api_key": "ns_live_abc123xyz789...",
                "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
                "created_at": "2025-12-31T10:00:00Z",
                "warning": "Store this key securely. It will not be shown again.",
            }
        }
    )

    api_key: str = Field(
        ...,
        description="The API key (only shown once)",
    )
    tenant_id: UUID = Field(
        ...,
        description="Tenant this key belongs to",
    )
    created_at: str = Field(
        ...,
        description="When the key was created",
    )
    warning: str = Field(
        default="Store this key securely. It will not be shown again.",
        description="Security warning",
    )


class APIKeyRotateRequest(BaseModel):
    """
    Request to rotate (regenerate) an API key.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "revoke_previous": True,
                "reason": "Scheduled rotation",
            }
        }
    )

    revoke_previous: bool = Field(
        default=True,
        description="Whether to immediately revoke the previous key",
    )
    reason: Optional[str] = Field(
        default=None,
        max_length=500,
        description="Reason for rotation (for audit)",
    )

