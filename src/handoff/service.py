"""Compatibility re-exports for the canonical PostgreSQL handoff service."""

from src.core.exceptions import (
    HandoffConfigError,
    HandoffNotFoundError,
    HandoffServiceError,
)
from src.services.handoff_service import (
    HANDOFF_BY_REASON,
    HANDOFF_LATENCY,
    HANDOFF_OPERATIONS,
    HandoffLifecycleError,
    HandoffService,
    close_handoff_service,
    get_handoff_service,
    handoff_record_to_detail,
    handoff_record_to_list_item,
)

__all__ = [
    "HANDOFF_BY_REASON",
    "HANDOFF_LATENCY",
    "HANDOFF_OPERATIONS",
    "HandoffConfigError",
    "HandoffLifecycleError",
    "HandoffNotFoundError",
    "HandoffService",
    "HandoffServiceError",
    "close_handoff_service",
    "get_handoff_service",
    "handoff_record_to_detail",
    "handoff_record_to_list_item",
]
