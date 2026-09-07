"""
NeuroStack API - Centralized Schemas
All API contracts and data models for the NeuroStack API.

This module exports all schema classes for use in API routes and documentation.
"""

from src.api.schemas.admin import (
    AdminStatsResponse,
    BuilderClinicResponse,
    BuilderWithClinicsResponse,
    ClinicSlotResponse,
    ClinicUserResponse,
    CreateBuilderClinicRequest,
    CreateBuilderRequest,
    CreateClinicRequest,
    UpdateBuilderRequest,
)
from src.api.schemas.base import (
    BaseResponse,
    ErrorDetail,
    ErrorResponse,
    PaginatedResponse,
    PaginationParams,
    TimestampMixin,
)
from src.api.schemas.builder import BuilderClinicResponse as BuilderPanelClinicResponse
from src.api.schemas.builder import BuilderProfileResponse, BuilderStatsResponse
from src.api.schemas.builder import (
    CreateBuilderClinicRequest as CreateBuilderPanelClinicRequest,
)
from src.api.schemas.builder import ToggleClinicStatusRequest
from src.api.schemas.events import (
    ContextOutput,
    EventPayload,
    EventRequest,
    EventResponse,
    EventType,
    FlowMode,
    PromptOutput,
    SRMDecision,
    StatePatch,
)
from src.api.schemas.handoff import (
    HandoffDetail,
    HandoffListItem,
    HandoffListResponse,
    HandoffUpdateRequest,
    MessageResponse,
    WebhookConfigCreate,
    WebhookConfigResponse,
    WebhookConfigUpdate,
    WebhookTestRequest,
    WebhookTestResponse,
)
from src.api.schemas.health import (
    ComponentHealth,
    DependencyStatus,
    HealthResponse,
    LivenessResponse,
    ReadinessResponse,
)
from src.api.schemas.knowledge import (
    DocumentListResponse,
    DocumentMetadata,
    DocumentResponse,
    DocumentUploadRequest,
    SearchRequest,
    SearchResponse,
)
from src.api.schemas.policies import (
    PolicyCreateRequest,
    PolicyListResponse,
    PolicyResponse,
    PolicyRule,
    PolicyType,
    PolicyUpdateRequest,
    RuleAction,
    RuleSeverity,
)
from src.api.schemas.sessions import (
    CognitiveState,
    FunnelStage,
    SessionCreateRequest,
    SessionListResponse,
    SessionResponse,
    SessionUpdateRequest,
)
from src.api.schemas.tenants import (
    APIKeyResponse,
    APIKeyRotateRequest,
    TenantBranding,
    TenantConfig,
    TenantCreateRequest,
    TenantFeatures,
    TenantListResponse,
    TenantResponse,
    TenantStatsResponse,
    TenantUpdateRequest,
)

__all__ = [
    # Builder (panel)
    "BuilderPanelClinicResponse",
    "BuilderProfileResponse",
    "BuilderStatsResponse",
    "CreateBuilderPanelClinicRequest",
    "ToggleClinicStatusRequest",
    # Admin
    "AdminStatsResponse",
    "BuilderClinicResponse",
    "BuilderWithClinicsResponse",
    "ClinicSlotResponse",
    "ClinicUserResponse",
    "CreateBuilderClinicRequest",
    "CreateBuilderRequest",
    "CreateClinicRequest",
    "UpdateBuilderRequest",
    # Base
    "BaseResponse",
    "ErrorDetail",
    "ErrorResponse",
    "PaginatedResponse",
    "PaginationParams",
    "TimestampMixin",
    # Events
    "ContextOutput",
    "EventPayload",
    "EventRequest",
    "EventResponse",
    "EventType",
    "FlowMode",
    "PromptOutput",
    "SRMDecision",
    "StatePatch",
    # Handoff
    "HandoffDetail",
    "HandoffListItem",
    "HandoffListResponse",
    "HandoffUpdateRequest",
    "MessageResponse",
    "WebhookConfigCreate",
    "WebhookConfigResponse",
    "WebhookConfigUpdate",
    "WebhookTestRequest",
    "WebhookTestResponse",
    # Health
    "ComponentHealth",
    "DependencyStatus",
    "HealthResponse",
    "LivenessResponse",
    "ReadinessResponse",
    # Knowledge
    "DocumentListResponse",
    "DocumentMetadata",
    "DocumentResponse",
    "DocumentUploadRequest",
    "SearchRequest",
    "SearchResponse",
    # Policies
    "PolicyCreateRequest",
    "PolicyListResponse",
    "PolicyResponse",
    "PolicyRule",
    "PolicyType",
    "PolicyUpdateRequest",
    "RuleAction",
    "RuleSeverity",
    # Sessions
    "CognitiveState",
    "FunnelStage",
    "SessionCreateRequest",
    "SessionListResponse",
    "SessionResponse",
    "SessionUpdateRequest",
    # Tenants
    "APIKeyResponse",
    "APIKeyRotateRequest",
    "TenantBranding",
    "TenantConfig",
    "TenantCreateRequest",
    "TenantFeatures",
    "TenantListResponse",
    "TenantResponse",
    "TenantStatsResponse",
    "TenantUpdateRequest",
]
