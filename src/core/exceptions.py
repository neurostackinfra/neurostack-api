"""
NeuroStack - Centralized Exception Hierarchy
All exceptions used across the application.
"""

from typing import Optional


# ============================================
# Base Exceptions
# ============================================


class NeuroStackError(Exception):
    """Base exception for all NeuroStack errors."""
    pass


class ServiceError(NeuroStackError):
    """Base exception for service layer errors."""
    pass


class RepositoryError(NeuroStackError):
    """Base exception for repository/data layer errors."""
    pass


class ValidationError(NeuroStackError):
    """Base exception for validation errors."""
    pass


class ConfigurationError(NeuroStackError):
    """Base exception for configuration errors."""
    pass


# ============================================
# Service Layer Exceptions
# ============================================


class HandoffServiceError(ServiceError):
    """Base exception for handoff service errors."""
    pass


class HandoffNotFoundError(HandoffServiceError):
    """Raised when handoff is not found."""
    pass


class HandoffConfigError(HandoffServiceError):
    """Raised when handoff configuration is invalid."""
    pass


class KnowledgeServiceError(ServiceError):
    """Base exception for knowledge service errors."""
    pass


class EventServiceError(ServiceError):
    """Base exception for event service errors."""
    pass


class SessionServiceError(ServiceError):
    """Base exception for session service errors."""
    pass


class PolicyServiceError(ServiceError):
    """Base exception for policy service errors."""
    pass


# ============================================
# Repository Layer Exceptions
# ============================================


class EventStoreError(RepositoryError):
    """Base exception for Event Store errors."""
    pass


class EventStoreConnectionError(EventStoreError):
    """Raised when connection to Event Store fails."""
    pass


class StateStoreError(RepositoryError):
    """Base exception for State Store errors."""
    pass


class StateNotFoundError(StateStoreError):
    """Raised when a session state is not found."""
    pass


class StateStoreConnectionError(StateStoreError):
    """Raised when connection to State Store fails."""
    pass


# ============================================
# Webhook Exceptions
# ============================================


class WebhookError(ServiceError):
    """Base exception for webhook errors."""
    pass


class WebhookConfigError(WebhookError):
    """Raised when webhook configuration is invalid."""
    pass


class WebhookDeliveryError(WebhookError):
    """Raised when webhook delivery fails."""
    
    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.response_body = response_body


__all__ = [
    # Base
    "NeuroStackError",
    "ServiceError",
    "RepositoryError",
    "ValidationError",
    "ConfigurationError",
    # Service
    "HandoffServiceError",
    "HandoffNotFoundError",
    "HandoffConfigError",
    "KnowledgeServiceError",
    "EventServiceError",
    "SessionServiceError",
    "PolicyServiceError",
    # Repository
    "EventStoreError",
    "EventStoreConnectionError",
    "StateStoreError",
    "StateNotFoundError",
    "StateStoreConnectionError",
    # Webhook
    "WebhookError",
    "WebhookConfigError",
    "WebhookDeliveryError",
]
