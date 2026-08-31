"""
NeuroStack - Session Repository
Data access layer for Session model.
"""

from datetime import datetime, timedelta
from typing import Any, Literal, Optional
from uuid import UUID

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import ColumnElement

from src.config.logging import get_logger
from src.models.session import Session
from src.repositories.base import BaseRepository

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

    _REACTIVATION_MAX_ATTEMPTS = 3

    def __init__(self, session: AsyncSession):
        super().__init__(session, Session)

    @staticmethod
    def _effective_order() -> tuple[ColumnElement[Any], ColumnElement[Any]]:
        """Ordering shared by every effective-active-session lookup."""
        return (Session.last_activity_at.desc(), Session.id.desc())

    @staticmethod
    def _effective_active_condition() -> ColumnElement[bool]:
        """Select the effective active row for each runtime identity.

        A non-null user is grouped by tenant and user. A null user has no
        shared routing identity, so every active row is effective by itself.
        """
        newer = aliased(Session)
        newer_active_row = (
            select(newer.id)
            .where(
                newer.tenant_id == Session.tenant_id,
                newer.user_id == Session.user_id,
                newer.is_active.is_(True),
                or_(
                    newer.last_activity_at > Session.last_activity_at,
                    and_(
                        newer.last_activity_at == Session.last_activity_at,
                        newer.id > Session.id,
                    ),
                ),
            )
            .correlate(Session)
            .exists()
        )
        return and_(
            Session.is_active.is_(True),
            or_(Session.user_id.is_(None), ~newer_active_row),
        )

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
        filters: dict[str, Any] = {"tenant_id": tenant_id}
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
        filters: dict[str, Any] = {"is_active": True}
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
        stmt = (
            select(Session)
            .where(
                Session.tenant_id == tenant_id,
                Session.user_id == user_id,
            )
            .order_by(*self._effective_order())
        )
        if active_only:
            stmt = stmt.where(Session.is_active.is_(True))
        if skip > 0:
            stmt = stmt.offset(skip)
        if limit is not None:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def find_inactive_effective(
        self,
        tenant_id: UUID,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[Session]:
        """List effective active sessions that are permanently paused."""
        stmt = (
            select(Session)
            .where(
                Session.tenant_id == tenant_id,
                self._effective_active_condition(),
                Session.is_paused.is_(True),
                Session.paused_until.is_(None),
            )
            .order_by(*self._effective_order())
        )
        if skip > 0:
            stmt = stmt.offset(skip)
        if limit is not None:
            stmt = stmt.limit(limit)

        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def count_inactive_effective(self, tenant_id: UUID) -> int:
        """Count effective active sessions that are permanently paused."""
        stmt = (
            select(func.count())
            .select_from(Session)
            .where(
                Session.tenant_id == tenant_id,
                self._effective_active_condition(),
                Session.is_paused.is_(True),
                Session.paused_until.is_(None),
            )
        )
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def reactivate_effective(
        self,
        tenant_id: UUID,
        session_id: UUID,
    ) -> Literal[
        "reactivated",
        "already_active",
        "superseded_session",
        "temporary_pause",
        "reactivation_conflict",
        "not_found",
    ]:
        """Atomically clear a permanent pause on the effective active row."""
        stmt = (
            update(Session)
            .where(
                Session.id == session_id,
                Session.tenant_id == tenant_id,
                self._effective_active_condition(),
                Session.is_paused.is_(True),
                Session.paused_until.is_(None),
            )
            .values(is_paused=False, paused_until=None)
            .execution_options(synchronize_session=False)
        )
        target = aliased(Session)
        effective = aliased(Session)
        effective_id = (
            select(effective.id)
            .where(
                effective.tenant_id == tenant_id,
                effective.user_id == target.user_id,
                effective.is_active.is_(True),
            )
            .order_by(effective.last_activity_at.desc(), effective.id.desc())
            .limit(1)
            .correlate(target)
            .scalar_subquery()
        )
        classification_stmt = select(
            target.id.label("id"),
            target.user_id.label("user_id"),
            target.is_active.label("is_active"),
            target.is_paused.label("is_paused"),
            target.paused_until.label("paused_until"),
            effective_id.label("effective_id"),
        ).where(
            target.id == session_id,
            target.tenant_id == tenant_id,
        )

        for _ in range(self._REACTIVATION_MAX_ATTEMPTS):
            result = await self.session.execute(stmt)
            if getattr(result, "rowcount", 0) == 1:
                await self.session.flush()
                return "reactivated"

            snapshot_result = await self.session.execute(classification_stmt)
            snapshot = snapshot_result.one_or_none()
            if snapshot is None or not snapshot.is_active:
                return "not_found"
            if snapshot.user_id is not None and snapshot.effective_id != snapshot.id:
                return "superseded_session"
            if snapshot.is_paused and snapshot.paused_until is not None:
                return "temporary_pause"
            if not snapshot.is_paused:
                return "already_active"

        return "reactivation_conflict"

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

        stmt = (
            select(Session)
            .where(
                and_(
                    Session.tenant_id == tenant_id,
                    Session.last_activity_at >= cutoff_time,
                )
            )
            .order_by(Session.last_activity_at.desc())
        )

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
        stmt = (
            select(func.count())
            .select_from(Session)
            .where(Session.tenant_id == tenant_id)
        )
        if active_only:
            stmt = stmt.where(Session.is_active.is_(True))

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
