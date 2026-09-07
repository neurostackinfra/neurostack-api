"""
NeuroStack - Core Module
Central orchestration, pipeline management, and shared utilities.
"""

from src.core.exceptions import (
    ConfigurationError,
    EventServiceError,
    EventStoreConnectionError,
    EventStoreError,
    HandoffConfigError,
    HandoffNotFoundError,
    HandoffServiceError,
    KnowledgeServiceError,
    NeuroStackError,
    PolicyServiceError,
    RepositoryError,
    ServiceError,
    SessionServiceError,
    StateNotFoundError,
    StateStoreConnectionError,
    StateStoreError,
    ValidationError,
    WebhookConfigError,
    WebhookDeliveryError,
    WebhookError,
)
from src.core.pipeline import (
    CognitivePipeline,
    PipelineResult,
    get_cognitive_pipeline,
    process_event,
)

__all__ = [
    # Pipeline
    "CognitivePipeline",
    "PipelineResult",
    "get_cognitive_pipeline",
    "process_event",
    # Exceptions - Base
    "NeuroStackError",
    "ServiceError",
    "RepositoryError",
    "ValidationError",
    "ConfigurationError",
    # Exceptions - Service
    "HandoffServiceError",
    "HandoffNotFoundError",
    "HandoffConfigError",
    "KnowledgeServiceError",
    "EventServiceError",
    "SessionServiceError",
    "PolicyServiceError",
    # Exceptions - Repository
    "EventStoreError",
    "EventStoreConnectionError",
    "StateStoreError",
    "StateNotFoundError",
    "StateStoreConnectionError",
    # Exceptions - Webhook
    "WebhookError",
    "WebhookConfigError",
    "WebhookDeliveryError",
]
