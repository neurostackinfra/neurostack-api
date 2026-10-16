"""
NeuroStack - Repository Pattern Implementation
Provides data access layer abstraction for all models.
"""

from src.repositories.base import BaseRepository
from src.repositories.billing import (
    BillingCapabilityRejected,
    BillingRepository,
    BillingRevisionConflict,
    BillingStateUnavailable,
    BillingTransitionEvidence,
    BillingTransitionRejected,
    billing_advisory_key,
)
from src.repositories.database import (
    close_database,
    get_db_engine,
    get_db_session,
    get_db_session_dependency,
    init_database,
)
from src.repositories.event import EventRepository
from src.repositories.handoff import HandoffRecordRepository
from src.repositories.knowledge import KnowledgeDocumentRepository
from src.repositories.policy import PolicyRepository
from src.repositories.session import SessionRepository
from src.repositories.tenant import TenantRepository

__all__ = [
    # Base
    "BaseRepository",
    "BillingRepository",
    "BillingTransitionEvidence",
    "BillingStateUnavailable",
    "BillingRevisionConflict",
    "BillingTransitionRejected",
    "BillingCapabilityRejected",
    "billing_advisory_key",
    # Database
    "get_db_session",
    "get_db_session_dependency",
    "get_db_engine",
    "init_database",
    "close_database",
    # Repositories
    "TenantRepository",
    "SessionRepository",
    "EventRepository",
    "PolicyRepository",
    "KnowledgeDocumentRepository",
    "HandoffRecordRepository",
]
