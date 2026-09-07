"""
NeuroStack - Human Handoff Service
Handoff orchestration, summarization, and webhook dispatch.

This module provides:
- HandoffService: Main service for handoff orchestration
- ConversationSummarizer: Generates structured conversation summaries
- WebhookDispatcher: Sends webhook notifications to external systems

Usage:
    from src.handoff import get_handoff_service, HandoffRequest

    service = get_handoff_service()
    response = await service.initiate_handoff(HandoffRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        reason=HandoffReason.USER_REQUEST,
    ))
"""

from src.core.exceptions import (
    HandoffNotFoundError,
    HandoffServiceError,
    WebhookConfigError,
    WebhookDeliveryError,
    WebhookError,
)
from src.handoff.models import (
    ConversationMessage,
    ConversationSummary,
    HandoffDetail,
    HandoffListItem,
    HandoffReason,
    HandoffRequest,
    HandoffResponse,
    HandoffSector,
    HandoffServiceConfig,
    HandoffSLA,
    HandoffStatus,
    HandoffUpdate,
    HandoffUrgency,
    KeyInformation,
    WebhookConfig,
    WebhookConfigCreate,
    WebhookConfigResponse,
    WebhookConfigUpdate,
    WebhookDelivery,
    WebhookPayload,
    WebhookStatus,
)
from src.handoff.summarizer import ConversationSummarizer, get_summarizer
from src.handoff.webhook import (
    WebhookDispatcher,
    close_webhook_dispatcher,
    get_webhook_dispatcher,
)

# Lazy import to avoid circular dependency
# HandoffService, close_handoff_service, and get_handoff_service are imported
# lazily via __getattr__ to break the circular import chain:
# services -> event_service -> api.schemas -> handoff -> services


def __getattr__(name: str) -> object:
    """Lazy import for HandoffService and related functions to avoid circular imports."""
    if name == "HandoffService":
        from src.services import HandoffService

        return HandoffService
    elif name == "close_handoff_service":
        from src.services import close_handoff_service

        return close_handoff_service
    elif name == "get_handoff_service":
        from src.services import get_handoff_service

        return get_handoff_service
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Models
    "ConversationMessage",
    "ConversationSummary",
    "HandoffDetail",
    "HandoffListItem",
    "HandoffReason",
    "HandoffRequest",
    "HandoffResponse",
    "HandoffServiceConfig",
    "HandoffSector",
    "HandoffSLA",
    "HandoffStatus",
    "HandoffUpdate",
    "HandoffUrgency",
    "KeyInformation",
    "WebhookConfig",
    "WebhookConfigCreate",
    "WebhookConfigResponse",
    "WebhookConfigUpdate",
    "WebhookDelivery",
    "WebhookPayload",
    "WebhookStatus",
    # Service
    "HandoffNotFoundError",
    "HandoffService",
    "HandoffServiceError",
    "close_handoff_service",
    "get_handoff_service",
    # Summarizer
    "ConversationSummarizer",
    "get_summarizer",
    # Webhook
    "WebhookConfigError",
    "WebhookDeliveryError",
    "WebhookDispatcher",
    "WebhookError",
    "close_webhook_dispatcher",
    "get_webhook_dispatcher",
]
