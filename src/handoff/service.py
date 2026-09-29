"""Compatibility imports for the canonical PostgreSQL handoff service.

The implementation and singleton live exclusively in
``src.services.handoff_service``.  This module remains importable for callers
using the historical path, but deliberately owns no lifecycle state.
"""

from src.core.exceptions import (
    HandoffConfigError,
    HandoffNotFoundError,
    HandoffServiceError,
)
from src.services.handoff_service import (
    HANDOFF_BY_REASON,
    HANDOFF_LATENCY,
    HANDOFF_OPERATIONS,
    HandoffConflictError,
    HandoffPersistenceError,
    HandoffPostCommitError,
    HandoffService,
    HandoffTransitionError,
    RecentMessageProjection,
    close_handoff_service,
    get_handoff_service,
    map_handoff_record,
)

__all__ = [
    "HANDOFF_BY_REASON",
    "HANDOFF_LATENCY",
    "HANDOFF_OPERATIONS",
    "HandoffConfigError",
    "HandoffConflictError",
    "HandoffNotFoundError",
    "HandoffPersistenceError",
    "HandoffPostCommitError",
    "HandoffService",
    "HandoffServiceError",
    "HandoffTransitionError",
    "RecentMessageProjection",
    "close_handoff_service",
    "get_handoff_service",
    "map_handoff_record",
]
