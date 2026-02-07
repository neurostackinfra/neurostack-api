"""
NeuroStack - Repository Pattern Implementation
Provides data access layer abstraction for all models.
"""

from src.repositories.base import BaseRepository
from src.repositories.database import (
    get_db_session,
    get_db_session_dependency,
    get_db_engine,
    init_database,
    close_database,
)
from src.repositories.tenant import TenantRepository
from src.repositories.session import SessionRepository
from src.repositories.event import EventRepository
from src.repositories.policy import PolicyRepository
from src.repositories.knowledge import KnowledgeDocumentRepository
from src.repositories.handoff import HandoffRecordRepository

__all__ = [
    # Base
    "BaseRepository",
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
