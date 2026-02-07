"""
NeuroStack API - Session Service
Business logic for session management.
"""

from typing import Optional
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.sessions import (
    CognitiveState,
    FunnelStage,
    SessionCreateRequest,
    SessionEventResponse,
    SessionEventsResponse,
    SessionListResponse,
    SessionResponse,
    SessionUpdateRequest,
)
from src.models.session import CognitiveStateData, Session
from src.repositories import (
    EventRepository,
    SessionRepository,
    TenantRepository,
)


class SessionService:
    """Service for managing sessions."""

    def __init__(
        self,
        session_repo: Optional[SessionRepository] = None,
        tenant_repo: Optional[TenantRepository] = None,
        event_repo: Optional[EventRepository] = None,
    ):
        """
        Initialize the session service.

        Args:
            session_repo: Session repository instance
            tenant_repo: Tenant repository instance
            event_repo: Event repository instance
        """
        self._session_repo = session_repo
        self._tenant_repo = tenant_repo
        self._event_repo = event_repo

    def _session_to_response(self, session: Session) -> SessionResponse:
        """
        Convert Session ORM model to SessionResponse schema.

        Args:
            session: Session model

        Returns:
            Session response
        """
        cognitive_state_data = session.get_cognitive_state()
        try:
            fs = FunnelStage(cognitive_state_data.funnel_stage)
        except ValueError:
            fs = FunnelStage.LEAD_NOVO

        return SessionResponse(
            id=session.id,
            tenant_id=session.tenant_id,
            user_id=session.user_id,
            channel=session.channel,
            cognitive_state=CognitiveState(
                funnel_stage=fs,
                intent_history=cognitive_state_data.intent_history,
                slots=cognitive_state_data.slots,
                flags=cognitive_state_data.flags,
                turn_count=cognitive_state_data.turn_count,
                safety_triggers=cognitive_state_data.safety_triggers,
                last_intent=cognitive_state_data.last_intent,
                last_action=getattr(cognitive_state_data, "last_action", None),
                confidence_avg=cognitive_state_data.confidence_avg,
                current_mode=getattr(cognitive_state_data, "current_mode", "normal"),
                lead=getattr(cognitive_state_data, "lead", {}),
                sales=getattr(cognitive_state_data, "sales", {}),
                last=getattr(cognitive_state_data, "last", {}),
                first_contact_at=getattr(cognitive_state_data, "first_contact_at", None),
                pos_atendimento_started_at=getattr(cognitive_state_data, "pos_atendimento_started_at", None),
                last_message_at=getattr(cognitive_state_data, "last_message_at", None),
            ),
            is_active=session.is_active,
            started_at=session.started_at.isoformat(),
            last_activity_at=session.last_activity_at.isoformat(),
            message_count=session.message_count,
            handoff_status=session.handoff_status,
            metadata=session.session_metadata,
        )

    async def create_session(
        self, request: SessionCreateRequest, db: AsyncSession
    ) -> SessionResponse:
        """
        Create a new conversation session.

        Args:
            request: Session create request
            db: Database session

        Returns:
            Created session response

        Raises:
            ValueError: If tenant not found or not active
        """
        if not self._tenant_repo:
            self._tenant_repo = TenantRepository(db)

        # Validate tenant exists
        tenant = await self._tenant_repo.get_by_id(request.tenant_id)

        if not tenant:
            raise ValueError(f"Tenant {request.tenant_id} not found")

        if not tenant.is_active:
            raise ValueError(f"Tenant {request.tenant_id} is not active")

        if not self._session_repo:
            self._session_repo = SessionRepository(db)

        # Create initial cognitive state
        initial_state = CognitiveStateData(
            funnel_stage="lead_novo",
            intent_history=[],
            slots=request.initial_context,
            flags={},
            turn_count=0,
            safety_triggers=0,
        )

        # Create session
        new_session = await self._session_repo.create(
            tenant_id=request.tenant_id,
            user_id=request.user_id,
            channel=request.channel,
            cognitive_state=initial_state.to_dict(),
            session_metadata=request.metadata or {},
        )

        return self._session_to_response(new_session)

    async def get_session(
        self, session_id: UUID, db: AsyncSession
    ) -> SessionResponse:
        """
        Retrieve session details by ID.

        Args:
            session_id: Session ID
            db: Database session

        Returns:
            Session response

        Raises:
            ValueError: If session not found
        """
        if not self._session_repo:
            self._session_repo = SessionRepository(db)

        db_session = await self._session_repo.get_by_id(session_id)

        if not db_session:
            raise ValueError(f"Session {session_id} not found")

        return self._session_to_response(db_session)

    async def list_sessions(
        self,
        tenant_id: UUID,
        db: AsyncSession,
        page: int = 1,
        page_size: int = 20,
        active_only: bool = True,
        channel: Optional[str] = None,
    ) -> SessionListResponse:
        """
        List sessions for a tenant.

        Args:
            tenant_id: Tenant ID
            db: Database session
            page: Page number
            page_size: Items per page
            active_only: Only return active sessions
            channel: Optional channel filter

        Returns:
            Session list response
        """
        if not self._session_repo:
            self._session_repo = SessionRepository(db)

        skip = (page - 1) * page_size

        if channel:
            sessions_list = await self._session_repo.find_by_channel(
                tenant_id=tenant_id,
                channel=channel,
                active_only=active_only,
                skip=skip,
                limit=page_size,
            )
            total = await self._session_repo.count_by_tenant(
                tenant_id, active_only=active_only
            )
        else:
            sessions_list = await self._session_repo.find_by_tenant(
                tenant_id=tenant_id,
                active_only=active_only,
                skip=skip,
                limit=page_size,
            )
            total = await self._session_repo.count_by_tenant(
                tenant_id, active_only=active_only
            )

        return SessionListResponse(
            sessions=[self._session_to_response(s) for s in sessions_list],
            total=total,
            page=page,
            page_size=page_size,
            has_next=(skip + page_size) < total,
            has_previous=page > 1,
        )

    async def update_session(
        self,
        session_id: UUID,
        request: SessionUpdateRequest,
        db: AsyncSession,
    ) -> SessionResponse:
        """
        Update session metadata or status.

        Args:
            session_id: Session ID
            request: Update request
            db: Database session

        Returns:
            Updated session response

        Raises:
            ValueError: If session not found
        """
        if not self._session_repo:
            self._session_repo = SessionRepository(db)

        update_data = {}
        if request.metadata is not None:
            update_data["session_metadata"] = request.metadata
        if request.is_active is not None:
            update_data["is_active"] = request.is_active

        updated_session = await self._session_repo.update(session_id, **update_data)

        if not updated_session:
            raise ValueError(f"Session {session_id} not found")

        return self._session_to_response(updated_session)

    async def end_session(
        self, session_id: UUID, db: AsyncSession
    ) -> None:
        """
        End a session (mark as inactive).

        Args:
            session_id: Session ID
            db: Database session

        Raises:
            ValueError: If session not found
        """
        if not self._session_repo:
            self._session_repo = SessionRepository(db)

        success = await self._session_repo.end_session(session_id)

        if not success:
            raise ValueError(f"Session {session_id} not found")

    async def get_session_events(
        self,
        session_id: UUID,
        tenant_id: UUID,
        db: AsyncSession,
        limit: int = 50,
        offset: int = 0,
    ) -> SessionEventsResponse:
        """
        Retrieve events for a session.

        Args:
            session_id: Session ID
            tenant_id: Tenant ID
            db: Database session
            limit: Maximum events to return
            offset: Offset for pagination

        Returns:
            Session events response
        """
        if not self._event_repo:
            self._event_repo = EventRepository(db)

        events_list = await self._event_repo.find_by_session(
            tenant_id=tenant_id,
            session_id=session_id,
            skip=offset,
            limit=limit,
        )
        total = await self._event_repo.count_by_session(tenant_id, session_id)

        event_responses = [
            SessionEventResponse(
                event_id=event.id,
                event_type=event.event_type,
                payload=event.event_data,
                decision=event.event_data.get("decision"),
                created_at=event.created_at.isoformat(),
            )
            for event in events_list
        ]

        return SessionEventsResponse(
            session_id=session_id,
            events=event_responses,
            total=total,
            limit=limit,
            offset=offset,
        )

    async def get_session_state(
        self, session_id: UUID, db: AsyncSession
    ) -> CognitiveState:
        """
        Get the cognitive state of a session.

        Args:
            session_id: Session ID
            db: Database session

        Returns:
            Cognitive state

        Raises:
            ValueError: If session not found
        """
        if not self._session_repo:
            self._session_repo = SessionRepository(db)

        db_session = await self._session_repo.get_by_id(session_id)

        if not db_session:
            raise ValueError(f"Session {session_id} not found")

        cognitive_state_data = db_session.get_cognitive_state()
        try:
            fs = FunnelStage(cognitive_state_data.funnel_stage)
        except ValueError:
            fs = FunnelStage.LEAD_NOVO

        return CognitiveState(
            funnel_stage=fs,
            intent_history=cognitive_state_data.intent_history,
            slots=cognitive_state_data.slots,
            flags=cognitive_state_data.flags,
            turn_count=cognitive_state_data.turn_count,
            safety_triggers=cognitive_state_data.safety_triggers,
            last_intent=cognitive_state_data.last_intent,
            last_action=getattr(cognitive_state_data, "last_action", None),
            confidence_avg=cognitive_state_data.confidence_avg,
            current_mode=getattr(cognitive_state_data, "current_mode", "normal"),
            lead=getattr(cognitive_state_data, "lead", {}),
            sales=getattr(cognitive_state_data, "sales", {}),
            last=getattr(cognitive_state_data, "last", {}),
            first_contact_at=getattr(cognitive_state_data, "first_contact_at", None),
            pos_atendimento_started_at=getattr(cognitive_state_data, "pos_atendimento_started_at", None),
            last_message_at=getattr(cognitive_state_data, "last_message_at", None),
        )


# Singleton instance
_session_service: Optional[SessionService] = None


def get_session_service() -> SessionService:
    """Get or create the global SessionService instance."""
    global _session_service
    if _session_service is None:
        _session_service = SessionService()
    return _session_service
