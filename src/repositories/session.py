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

    async def get_by_tenant(
        self, session_id: UUID, tenant_id: UUID
    ) -> Optional[Session]:
        """Get a session only when it belongs to the authorized tenant."""
        result = await self.session.execute(
            select(Session).where(
                Session.id == session_id,
                Session.tenant_id == tenant_id,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_tenant_for_update(
        self, session_id: UUID, tenant_id: UUID
    ) -> Optional[Session]:
        """Lock and return one tenant-scoped session for the current transaction.

        PostgreSQL holds the row lock until the caller commits or rolls back.
        Keeping the tenant predicate in the locking statement prevents an
        untrusted session UUID from locking or exposing another tenant's row.
        """
        result = await self.session.execute(
            select(Session)
            .where(
                Session.id == session_id,
                Session.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        return result.scalar_one_or_none()

    async def update_for_tenant(
        self,
        session_id: UUID,
        tenant_id: UUID,
        **kwargs: Any,
    ) -> Optional[Session]:
        """Update a session after a tenant-scoped lookup."""
        instance = await self.get_by_tenant(session_id, tenant_id)
        if instance is None:
            return None
        for key, value in kwargs.items():
            if hasattr(instance, key):
                setattr(instance, key, value)
        await self.session.flush()
        await self.session.refresh(instance)
        return instance

    async def end_for_tenant(self, session_id: UUID, tenant_id: UUID) -> bool:
        """End a session after a tenant-scoped lookup."""
        instance = await self.get_by_tenant(session_id, tenant_id)
        if instance is None:
            return False
        instance.end_session()
        await self.session.flush()
        await self.session.refresh(instance)
        return True

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

    def _reactivation_classification_stmt(
        self,
        tenant_id: UUID,
        session_id: UUID,
    ) -> Any:
        """Build the per-row classification snapshot used by both reactivate CAS variants.

        Returns a SELECT that joins the target row against a scalar subquery
        resolving the effective-active row id for the same ``(tenant_id, user_id)``
        identity. Callers compare ``snapshot.effective_id`` to ``snapshot.id`` to
        decide whether the target is still the effective row.
        """
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
        return select(
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
        """Atomically clear a permanent pause on the effective active row.

        Legacy single-call variant that does NOT touch ``cognitive_state``.
        New call sites must prefer :meth:`reactivate_effective_manual` (operator
        resume) or :meth:`reactivate_effective_auto` (expired-deadline resume),
        both of which also project the fresh cognitive state atomically.
        """
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
        classification_stmt = self._reactivation_classification_stmt(
            tenant_id, session_id
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

    async def pause_effective(
        self,
        tenant_id: UUID,
        session_id: UUID,
        cognitive_state: dict[str, Any],
        paused_until: Optional[datetime],
    ) -> Literal[
        "paused",
        "already_paused",
        "superseded_session",
        "not_found",
    ]:
        """Atomically pause the effective active row AND rewrite its cognitive state.

        This is the single tenant-scoped CAS that any caller wanting to pause a
        session must go through. It takes an **already-merged** ``cognitive_state``
        dict (serialized form, as produced by
        :meth:`CognitiveStateData.to_dict`) and writes it together with
        ``is_paused=True`` and the resolved ``paused_until`` in **one** UPDATE —
        callers must never write ``is_paused`` / ``paused_until`` / ``cognitive_state``
        separately against the ORM, or they re-open the A2 hazard where a losing
        CAS attempt would still clobber the winner's state.

        Preconditions:
            ``is_paused IS False`` on the effective active row for
            ``(tenant_id, session_id)``.

        Outcomes:
            * ``paused`` — this call wrote the row (the winner).
            * ``already_paused`` — the row was already paused (concurrent winner
              wrote first); the caller must NOT re-apply the write and MUST
              drop its locally-merged cognitive state.
            * ``superseded_session`` — a newer effective-active row exists for the
              same ``(tenant_id, user_id)`` identity; the caller must not pause
              this stale row.
            * ``not_found`` — the row does not exist, is inactive, or belongs to
              a different tenant.

        .. note::
            **Caller refresh contract (A1):** callers that already hold an
            ORM-loaded ``Session`` for this row in the current ``AsyncSession``'s
            identity map MUST call
            ``await session.refresh(session_row, attribute_names=['cognitive_state',
            'is_paused', 'paused_until'])`` immediately after this returns
            ``"paused"``. ``expire_on_commit=False`` means the identity-mapped
            object will NOT otherwise see the raw UPDATE's values and the caller
            will read stale attributes for the remainder of the request.
        """
        stmt = (
            update(Session)
            .where(
                Session.id == session_id,
                Session.tenant_id == tenant_id,
                self._effective_active_condition(),
                Session.is_paused.is_(False),
            )
            .values(
                is_paused=True,
                paused_until=paused_until,
                cognitive_state=cognitive_state,
            )
            .execution_options(synchronize_session=False)
        )
        classification_stmt = self._reactivation_classification_stmt(
            tenant_id, session_id
        )

        result = await self.session.execute(stmt)
        if getattr(result, "rowcount", 0) == 1:
            await self.session.flush()
            return "paused"

        snapshot_result = await self.session.execute(classification_stmt)
        snapshot = snapshot_result.one_or_none()
        if snapshot is None or not snapshot.is_active:
            return "not_found"
        if snapshot.user_id is not None and snapshot.effective_id != snapshot.id:
            return "superseded_session"
        if snapshot.is_paused:
            # Loser: a concurrent writer paused first. Do NOT re-apply the write
            # and do NOT merge our candidate state — the winner's state is now
            # authoritative.
            return "already_paused"
        # Row is unpaused but the UPDATE still missed. Treat as a transient
        # mismatch, surfaced as not_found rather than silently retrying — the
        # caller can decide whether to re-read and retry at a higher level.
        return "not_found"

    async def reactivate_effective_manual(
        self,
        tenant_id: UUID,
        session_id: UUID,
        new_cognitive_state: dict[str, Any],
        new_episode_id: UUID,
    ) -> Literal[
        "reactivated",
        "already_active",
        "superseded_session",
        "temporary_pause",
        "reactivation_conflict",
        "not_found",
    ]:
        """Operator-triggered reactivation CAS (permanent-pause precondition).

        Same precondition as :meth:`reactivate_effective` (``is_paused=True AND
        paused_until IS NULL``), but the UPDATE also rewrites ``cognitive_state``
        to the caller-projected resume state in the same statement. The caller
        must build the resume projection via
        :func:`src.handoff.episode_context.project_resume_state`, bake
        ``new_episode_id`` into ``new_cognitive_state["context_episode_id"]``
        before invoking this method, and generate ``new_episode_id`` exactly once
        outside any retry loop so retry attempts reuse the same episode id (A3).

        Outcomes mirror :meth:`reactivate_effective` so the HTTP response /
        ``SessionReactivationError`` mapping stays identical for the manual path.

        .. note::
            **Caller refresh contract (A1):** callers holding an ORM-loaded
            ``Session`` for this row in the current ``AsyncSession``'s identity
            map MUST ``await session.refresh(session_row,
            attribute_names=['cognitive_state','is_paused','paused_until'])``
            after a winning ``"reactivated"`` result, because
            ``expire_on_commit=False`` leaves the identity-mapped attributes
            stale otherwise.
        """
        stmt = (
            update(Session)
            .where(
                Session.id == session_id,
                Session.tenant_id == tenant_id,
                self._effective_active_condition(),
                Session.is_paused.is_(True),
                Session.paused_until.is_(None),
            )
            .values(
                is_paused=False,
                paused_until=None,
                cognitive_state=new_cognitive_state,
            )
            .execution_options(synchronize_session=False)
        )
        classification_stmt = self._reactivation_classification_stmt(
            tenant_id, session_id
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

    async def reactivate_effective_auto(
        self,
        tenant_id: UUID,
        session_id: UUID,
        new_cognitive_state: dict[str, Any],
        new_episode_id: UUID,
        *,
        now: datetime,
    ) -> Literal[
        "reactivated",
        "already_active",
        "superseded_session",
        "not_yet_expired",
        "reactivation_conflict",
        "not_found",
    ]:
        """Expired-deadline reactivation CAS (temporary-pause precondition).

        Precondition: ``is_paused=True AND paused_until IS NOT NULL AND
        paused_until <= :now`` — i.e. the temporary pause's deadline has elapsed.
        As with :meth:`reactivate_effective_manual`, the UPDATE rewrites
        ``cognitive_state`` to the caller-projected resume state in a single
        statement and the caller must generate ``new_episode_id`` once outside
        any retry loop.

        Classification literals (distinct from the manual variant because the
        expired-deadline precondition has a different failure surface):

        * ``reactivated`` — this call cleared the expired pause (the winner).
        * ``already_active`` — the row is already unpaused.
        * ``superseded_session`` — a newer effective-active row exists for the
          same ``(tenant_id, user_id)`` identity.
        * ``not_yet_expired`` — the row is paused with a deadline strictly in
          the future. This classification does **not** exist for the manual
          variant (which operates on permanent pauses where ``paused_until`` is
          ``NULL``) and is why the auto variant publishes its own table.
        * ``reactivation_conflict`` — the UPDATE missed
          ``_REACTIVATION_MAX_ATTEMPTS`` times without the classifier matching
          a terminal outcome.
        * ``not_found`` — the row does not exist, is inactive, or belongs to a
          different tenant.

        .. note::
            **Caller refresh contract (A1):** callers holding an ORM-loaded
            ``Session`` for this row MUST ``await session.refresh(session_row,
            attribute_names=['cognitive_state','is_paused','paused_until'])``
            after a winning ``"reactivated"`` result — see
            :meth:`reactivate_effective_manual` for the rationale.
        """
        stmt = (
            update(Session)
            .where(
                Session.id == session_id,
                Session.tenant_id == tenant_id,
                self._effective_active_condition(),
                Session.is_paused.is_(True),
                Session.paused_until.is_not(None),
                Session.paused_until <= now,
            )
            .values(
                is_paused=False,
                paused_until=None,
                cognitive_state=new_cognitive_state,
            )
            .execution_options(synchronize_session=False)
        )
        classification_stmt = self._reactivation_classification_stmt(
            tenant_id, session_id
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
            if not snapshot.is_paused:
                return "already_active"
            if snapshot.paused_until is not None and snapshot.paused_until > now:
                return "not_yet_expired"

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
