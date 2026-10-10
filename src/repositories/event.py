"""
NeuroStack - Event Repository
Data access layer for Event model (append-only event store).
"""

from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.logging import get_logger
from src.models.event import Event
from src.repositories.base import BaseRepository

logger = get_logger(__name__)


class EventRepository(BaseRepository[Event]):
    """
    Repository for Event model operations.

    Events are append-only, so this repository focuses on:
    - Creating events (no updates/deletes)
    - Querying events by various criteria
    - Event aggregation and analytics
    """

    def __init__(self, session: AsyncSession):
        super().__init__(session, Event)

    async def get_for_tenant(self, event_id: UUID, tenant_id: UUID) -> Optional[Event]:
        """Return an event only inside the authenticated tenant boundary."""
        result = await self.session.execute(
            select(Event).where(Event.id == event_id, Event.tenant_id == tenant_id)
        )
        return result.scalar_one_or_none()

    async def find_by_session(
        self,
        tenant_id: UUID,
        session_id: UUID,
        skip: int = 0,
        limit: Optional[int] = None,
        event_type: Optional[str] = None,
    ) -> list[Event]:
        """
        Find events for a session.

        Args:
            tenant_id: Tenant ID
            session_id: Session ID
            skip: Number of records to skip
            limit: Maximum number of records to return
            event_type: Optional event type filter

        Returns:
            List of event instances (ordered by created_at DESC)
        """
        filters: dict[str, object] = {
            "tenant_id": tenant_id,
            "session_id": session_id,
        }
        if event_type:
            filters["event_type"] = event_type

        return await self.find_many(
            skip=skip,
            limit=limit,
            order_by="-created_at",
            **filters,
        )

    async def find_by_tenant(
        self,
        tenant_id: UUID,
        skip: int = 0,
        limit: Optional[int] = None,
        event_type: Optional[str] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> list[Event]:
        """
        Find events for a tenant.

        Args:
            tenant_id: Tenant ID
            skip: Number of records to skip
            limit: Maximum number of records to return
            event_type: Optional event type filter
            start_time: Optional start time filter
            end_time: Optional end time filter

        Returns:
            List of event instances (ordered by created_at DESC)
        """
        stmt = select(Event).where(Event.tenant_id == tenant_id)

        if event_type:
            stmt = stmt.where(Event.event_type == event_type)
        if start_time:
            stmt = stmt.where(Event.created_at >= start_time)
        if end_time:
            stmt = stmt.where(Event.created_at <= end_time)

        stmt = stmt.order_by(Event.created_at.desc())

        if skip > 0:
            stmt = stmt.offset(skip)
        if limit is not None:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def find_by_type(
        self,
        tenant_id: UUID,
        event_type: str,
        skip: int = 0,
        limit: Optional[int] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> list[Event]:
        """
        Find events by type.

        Args:
            tenant_id: Tenant ID
            event_type: Event type
            skip: Number of records to skip
            limit: Maximum number of records to return
            start_time: Optional start time filter
            end_time: Optional end time filter

        Returns:
            List of event instances
        """
        return await self.find_by_tenant(
            tenant_id=tenant_id,
            skip=skip,
            limit=limit,
            event_type=event_type,
            start_time=start_time,
            end_time=end_time,
        )

    async def find_recent(
        self,
        tenant_id: UUID,
        hours: int = 24,
        event_type: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[Event]:
        """
        Find recent events within the last N hours.

        Args:
            tenant_id: Tenant ID
            hours: Number of hours to look back
            event_type: Optional event type filter
            limit: Maximum number of records to return

        Returns:
            List of event instances
        """
        start_time = datetime.utcnow() - timedelta(hours=hours)
        return await self.find_by_tenant(
            tenant_id=tenant_id,
            limit=limit,
            event_type=event_type,
            start_time=start_time,
        )

    async def count_by_session(
        self,
        tenant_id: UUID,
        session_id: UUID,
        event_type: Optional[str] = None,
    ) -> int:
        """
        Count events for a session.

        Args:
            tenant_id: Tenant ID
            session_id: Session ID
            event_type: Optional event type filter

        Returns:
            Number of events
        """
        stmt = (
            select(func.count())
            .select_from(Event)
            .where(
                and_(
                    Event.tenant_id == tenant_id,
                    Event.session_id == session_id,
                )
            )
        )
        if event_type:
            stmt = stmt.where(Event.event_type == event_type)

        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def count_by_type(
        self,
        tenant_id: UUID,
        event_type: str,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None,
    ) -> int:
        """
        Count events by type.

        Args:
            tenant_id: Tenant ID
            event_type: Event type
            start_time: Optional start time filter
            end_time: Optional end time filter

        Returns:
            Number of events
        """
        stmt = (
            select(func.count())
            .select_from(Event)
            .where(
                and_(
                    Event.tenant_id == tenant_id,
                    Event.event_type == event_type,
                )
            )
        )
        if start_time:
            stmt = stmt.where(Event.created_at >= start_time)
        if end_time:
            stmt = stmt.where(Event.created_at <= end_time)

        result = await self.session.execute(stmt)
        return result.scalar() or 0

    # Note: Events are append-only, so we don't implement update/delete methods
    # The base repository's update/delete methods are not applicable here
