"""
NeuroStack - Session Repository
Data access layer for Session model.
"""

from datetime import datetime, timedelta
from typing import Optional
from uuid import UUID

from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.session import Session
from src.repositories.base import BaseRepository
from src.config.logging import get_logger

logger = get_logger(__name__)


class SessionRepository(BaseRepository[Session]):
    """
    Repository for Session model operations.
    
    Provides methods for:
    - CRUD operations (inherited from BaseRepository)
    - Finding sessions by tenant
    - Finding active sessions
    - Finding sessions by user
    - Finding sessions by channel
    - Session activity queries
    """
    
    def __init__(self, session: AsyncSession):
        super().__init__(session, Session)
    
    async def find_by_tenant(
        self,
        tenant_id: UUID,
        active_only: bool = False,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[Session]:
        """
        Find sessions by tenant.
        
        Args:
            tenant_id: Tenant ID
            active_only: Only return active sessions
            skip: Number of records to skip
            limit: Maximum number of records to return
            
        Returns:
            List of session instances
        """
        filters = {"tenant_id": tenant_id}
        if active_only:
            filters["is_active"] = True
        
        return await self.find_many(
            skip=skip,
            limit=limit,
            order_by="-last_activity_at",
            **filters,
        )
    
    async def find_active(
        self,
        tenant_id: Optional[UUID] = None,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[Session]:
        """
        Find active sessions.
        
        Args:
            tenant_id: Optional tenant ID filter
            skip: Number of records to skip
            limit: Maximum number of records to return
            
        Returns:
            List of active session instances
        """
        filters = {"is_active": True}
        if tenant_id:
            filters["tenant_id"] = tenant_id
        
        return await self.find_many(
            skip=skip,
            limit=limit,
            order_by="-last_activity_at",
            **filters,
        )
    
    async def find_by_user(
        self,
        tenant_id: UUID,
        user_id: str,
        active_only: bool = False,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[Session]:
        """
        Find sessions by user.
        
        Args:
            tenant_id: Tenant ID
            user_id: User ID
            active_only: Only return active sessions
            skip: Number of records to skip
            limit: Maximum number of records to return
            
        Returns:
            List of session instances
        """
        filters = {
            "tenant_id": tenant_id,
            "user_id": user_id,
        }
        if active_only:
            filters["is_active"] = True
        
        return await self.find_many(
            skip=skip,
            limit=limit,
            order_by="-last_activity_at",
            **filters,
        )
    
    async def find_by_channel(
        self,
        tenant_id: UUID,
        channel: str,
        active_only: bool = False,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[Session]:
        """
        Find sessions by channel.
        
        Args:
            tenant_id: Tenant ID
            channel: Channel name
            active_only: Only return active sessions
            skip: Number of records to skip
            limit: Maximum number of records to return
            
        Returns:
            List of session instances
        """
        filters = {
            "tenant_id": tenant_id,
            "channel": channel,
        }
        if active_only:
            filters["is_active"] = True
        
        return await self.find_many(
            skip=skip,
            limit=limit,
            order_by="-last_activity_at",
            **filters,
        )
    
    async def find_recently_active(
        self,
        tenant_id: UUID,
        hours: int = 24,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[Session]:
        """
        Find sessions active within the last N hours.
        
        Args:
            tenant_id: Tenant ID
            hours: Number of hours to look back
            skip: Number of records to skip
            limit: Maximum number of records to return
            
        Returns:
            List of session instances
        """
        cutoff_time = datetime.utcnow() - timedelta(hours=hours)
        
        stmt = select(Session).where(
            and_(
                Session.tenant_id == tenant_id,
                Session.last_activity_at >= cutoff_time,
            )
        ).order_by(Session.last_activity_at.desc())
        
        if skip > 0:
            stmt = stmt.offset(skip)
        if limit is not None:
            stmt = stmt.limit(limit)
        
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
    
    async def count_by_tenant(self, tenant_id: UUID, active_only: bool = False) -> int:
        """
        Count sessions for a tenant.
        
        Args:
            tenant_id: Tenant ID
            active_only: Only count active sessions
            
        Returns:
            Number of sessions
        """
        stmt = select(func.count()).select_from(Session).where(Session.tenant_id == tenant_id)
        if active_only:
            stmt = stmt.where(Session.is_active == True)
        
        result = await self.session.execute(stmt)
        return result.scalar() or 0
    
    async def end_session(self, session_id: UUID) -> bool:
        """
        End a session (mark as inactive).
        
        Args:
            session_id: Session ID
            
        Returns:
            True if updated, False if not found
        """
        instance = await self.get_by_id(session_id)
        if instance is None:
            return False
        
        instance.end_session()
        await self.session.flush()
        await self.session.refresh(instance)
        logger.debug("Ended session", session_id=str(session_id))
        return True
    
    async def update_activity(self, session_id: UUID) -> bool:
        """
        Update session activity timestamp.
        
        Args:
            session_id: Session ID
            
        Returns:
            True if updated, False if not found
        """
        instance = await self.get_by_id(session_id)
        if instance is None:
            return False
        
        instance.update_activity()
        await self.session.flush()
        await self.session.refresh(instance)
        return True
