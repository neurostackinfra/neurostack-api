"""
NeuroStack - Handoff Record Repository
Data access layer for HandoffRecord model.
"""

from typing import Optional
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.logging import get_logger
from src.models.handoff import HandoffRecord, HandoffStatus
from src.repositories.base import BaseRepository

logger = get_logger(__name__)


class HandoffRecordRepository(BaseRepository[HandoffRecord]):
    """
    Repository for HandoffRecord model operations.

    Provides methods for:
    - CRUD operations (inherited from BaseRepository)
    - Finding handoffs by tenant
    - Finding handoffs by session
    - Finding handoffs by status
    - Finding active handoffs
    - Finding handoffs by urgency
    """

    def __init__(self, session: AsyncSession):
        super().__init__(session, HandoffRecord)

    async def find_by_tenant(
        self,
        tenant_id: UUID,
        status: Optional[str] = None,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[HandoffRecord]:
        """
        Find handoffs for a tenant.

        Args:
            tenant_id: Tenant ID
            status: Optional status filter
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of handoff instances (ordered by created_at DESC)
        """
        filters: dict[str, object] = {"tenant_id": tenant_id}
        if status:
            filters["status"] = status

        return await self.find_many(
            skip=skip,
            limit=limit,
            order_by="-created_at",
            **filters,
        )

    async def find_by_session(
        self,
        tenant_id: UUID,
        session_id: UUID,
        status: Optional[str] = None,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[HandoffRecord]:
        """
        Find handoffs for a session.

        Args:
            tenant_id: Tenant ID
            session_id: Session ID
            status: Optional status filter
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of handoff instances (ordered by created_at DESC)
        """
        filters: dict[str, object] = {
            "tenant_id": tenant_id,
            "session_id": session_id,
        }
        if status:
            filters["status"] = status

        return await self.find_many(
            skip=skip,
            limit=limit,
            order_by="-created_at",
            **filters,
        )

    async def find_active(
        self,
        tenant_id: Optional[UUID] = None,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[HandoffRecord]:
        """
        Find active handoffs (pending, accepted, or in_progress).

        Args:
            tenant_id: Optional tenant ID filter
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of active handoff instances (ordered by urgency and created_at)
        """
        stmt = select(HandoffRecord).where(
            HandoffRecord.status.in_(
                [
                    HandoffStatus.PENDING.value,
                    HandoffStatus.ACCEPTED.value,
                    HandoffStatus.IN_PROGRESS.value,
                ]
            )
        )

        if tenant_id:
            stmt = stmt.where(HandoffRecord.tenant_id == tenant_id)

        # Order by urgency (critical first) then created_at
        stmt = stmt.order_by(
            HandoffRecord.urgency.desc(),
            HandoffRecord.created_at.asc(),
        )

        if skip > 0:
            stmt = stmt.offset(skip)
        if limit is not None:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def find_by_status(
        self,
        tenant_id: UUID,
        status: str,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[HandoffRecord]:
        """
        Find handoffs by status.

        Args:
            tenant_id: Tenant ID
            status: Handoff status
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of handoff instances
        """
        return await self.find_by_tenant(
            tenant_id=tenant_id,
            status=status,
            skip=skip,
            limit=limit,
        )

    async def find_by_urgency(
        self,
        tenant_id: UUID,
        urgency: str,
        active_only: bool = True,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[HandoffRecord]:
        """
        Find handoffs by urgency level.

        Args:
            tenant_id: Tenant ID
            urgency: Urgency level
            active_only: Only return active handoffs
            skip: Number of records to skip
            limit: Maximum number of records to return

        Returns:
            List of handoff instances
        """
        filters = {
            "tenant_id": tenant_id,
            "urgency": urgency,
        }
        if active_only:
            # Active statuses
            stmt = select(HandoffRecord).where(
                and_(
                    HandoffRecord.tenant_id == tenant_id,
                    HandoffRecord.urgency == urgency,
                    HandoffRecord.status.in_(
                        [
                            HandoffStatus.PENDING.value,
                            HandoffStatus.ACCEPTED.value,
                            HandoffStatus.IN_PROGRESS.value,
                        ]
                    ),
                )
            )
            stmt = stmt.order_by(HandoffRecord.created_at.asc())

            if skip > 0:
                stmt = stmt.offset(skip)
            if limit is not None:
                stmt = stmt.limit(limit)

            result = await self.session.execute(stmt)
            return list(result.scalars().all())

        return await self.find_many(
            skip=skip,
            limit=limit,
            order_by="created_at",
            **filters,
        )

    async def count_by_tenant(
        self,
        tenant_id: UUID,
        status: Optional[str] = None,
    ) -> int:
        """
        Count handoffs for a tenant.

        Args:
            tenant_id: Tenant ID
            status: Optional status filter

        Returns:
            Number of handoffs
        """
        stmt = (
            select(func.count())
            .select_from(HandoffRecord)
            .where(HandoffRecord.tenant_id == tenant_id)
        )
        if status:
            stmt = stmt.where(HandoffRecord.status == status)

        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def accept_handoff(
        self,
        handoff_id: UUID,
        agent_id: Optional[str] = None,
    ) -> bool:
        """
        Accept a handoff.

        Args:
            handoff_id: Handoff ID
            agent_id: Optional agent ID

        Returns:
            True if updated, False if not found
        """
        instance = await self.get_by_id(handoff_id)
        if instance is None:
            return False

        instance.accept(agent_id)
        await self.session.flush()
        await self.session.refresh(instance)
        logger.debug("Accepted handoff", handoff_id=str(handoff_id))
        return True

    async def complete_handoff(
        self,
        handoff_id: UUID,
        notes: Optional[str] = None,
    ) -> bool:
        """
        Complete a handoff.

        Args:
            handoff_id: Handoff ID
            notes: Optional resolution notes

        Returns:
            True if updated, False if not found
        """
        instance = await self.get_by_id(handoff_id)
        if instance is None:
            return False

        instance.complete(notes)
        await self.session.flush()
        await self.session.refresh(instance)
        logger.debug("Completed handoff", handoff_id=str(handoff_id))
        return True

    async def cancel_handoff(
        self,
        handoff_id: UUID,
        notes: Optional[str] = None,
    ) -> bool:
        """
        Cancel a handoff.

        Args:
            handoff_id: Handoff ID
            notes: Optional cancellation notes

        Returns:
            True if updated, False if not found
        """
        instance = await self.get_by_id(handoff_id)
        if instance is None:
            return False

        instance.cancel(notes)
        await self.session.flush()
        await self.session.refresh(instance)
        logger.debug("Cancelled handoff", handoff_id=str(handoff_id))
        return True
