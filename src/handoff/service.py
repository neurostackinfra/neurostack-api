"""
NeuroStack - Human Handoff Service
Main service for orchestrating human handoff operations.
"""

import time
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from prometheus_client import Counter, Histogram

from config.logging import get_logger
from src.handoff.models import (
    ConversationSummary,
    HandoffDetail,
    HandoffListItem,
    HandoffReason,
    HandoffRequest,
    HandoffResponse,
    HandoffServiceConfig,
    HandoffStatus,
    HandoffUpdate,
    HandoffUrgency,
    WebhookConfig,
    WebhookPayload,
    WebhookStatus,
)
from src.handoff.summarizer import ConversationSummarizer, get_summarizer
from src.handoff.webhook import (
    WebhookDispatcher,
    WebhookError,
    get_webhook_dispatcher,
)
from src.memory.event_store import (
    EventData,
    EventStore,
    EventType,
    HandoffEventData,
)


logger = get_logger(__name__)


# ============================================
# Prometheus Metrics
# ============================================

HANDOFF_OPERATIONS = Counter(
    "neurostack_handoff_operations_total",
    "Total number of handoff operations",
    ["operation", "status"],
)

HANDOFF_BY_REASON = Counter(
    "neurostack_handoff_by_reason_total",
    "Total handoffs by reason",
    ["reason", "urgency"],
)

HANDOFF_LATENCY = Histogram(
    "neurostack_handoff_latency_seconds",
    "Handoff operation latency in seconds",
    ["operation"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)


# ============================================
# Exceptions
# ============================================


class HandoffServiceError(Exception):
    """Base exception for handoff service errors."""
    pass


class HandoffNotFoundError(HandoffServiceError):
    """Raised when handoff is not found."""
    pass


class HandoffConfigError(HandoffServiceError):
    """Raised when configuration is invalid."""
    pass


# ============================================
# Handoff Service
# ============================================


class HandoffService:
    """
    Main service for human handoff operations.
    
    Provides:
    - Handoff initiation with summary generation
    - Webhook notifications to external systems
    - Handoff status management
    - Event logging for audit
    
    Usage:
        service = HandoffService(event_store=event_store)
        response = await service.initiate_handoff(request)
    """

    def __init__(
        self,
        event_store: Optional[EventStore] = None,
        summarizer: Optional[ConversationSummarizer] = None,
        webhook_dispatcher: Optional[WebhookDispatcher] = None,
        config: Optional[HandoffServiceConfig] = None,
    ):
        """
        Initialize the handoff service.
        
        Args:
            event_store: Event store for logging
            summarizer: Conversation summarizer
            webhook_dispatcher: Webhook dispatcher
            config: Service configuration
        """
        self.event_store = event_store
        self.summarizer = summarizer or get_summarizer()
        self.webhook_dispatcher = webhook_dispatcher or get_webhook_dispatcher()
        self.config = config or HandoffServiceConfig()
        
        # In-memory handoff store (should be replaced with persistent storage)
        self._handoffs: dict[UUID, dict[str, Any]] = {}

    # ============================================
    # Handoff Initiation
    # ============================================

    async def initiate_handoff(
        self,
        request: HandoffRequest,
    ) -> HandoffResponse:
        """
        Initiate a human handoff.
        
        This method:
        1. Generates a conversation summary
        2. Creates a handoff record
        3. Logs the event
        4. Sends webhook notification
        5. Returns user-facing message
        
        Args:
            request: Handoff request with session details
            
        Returns:
            HandoffResponse with status and user message
        """
        start_time = time.perf_counter()
        handoff_id = uuid4()
        
        logger.info(
            "Initiating handoff",
            handoff_id=str(handoff_id),
            tenant_id=str(request.tenant_id),
            session_id=str(request.session_id),
            reason=request.reason.value,
            urgency=request.urgency.value,
        )
        
        try:
            # Generate summary
            summary = self._generate_summary(request)
            
            # Create handoff record
            handoff_record = {
                "id": handoff_id,
                "tenant_id": request.tenant_id,
                "session_id": request.session_id,
                "status": HandoffStatus.PENDING,
                "urgency": request.urgency,
                "reason": request.reason,
                "summary": summary,
                "webhook_sent": False,
                "webhook_status": None,
                "assigned_to": None,
                "created_at": datetime.now(timezone.utc),
                "updated_at": None,
                "resolved_at": None,
                "resolution_notes": None,
            }
            self._handoffs[handoff_id] = handoff_record
            
            # Log event to Event Store
            await self._log_handoff_event(
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                handoff_id=handoff_id,
                event_type=EventType.HANDOFF_REQUEST,
                reason=request.reason,
                urgency=request.urgency,
                summary=summary.summary_text,
            )
            
            # Send webhook notification
            webhook_sent = False
            webhook_status = None
            
            if self.webhook_dispatcher.is_configured(request.tenant_id):
                try:
                    delivery = await self._send_webhook(
                        handoff_id=handoff_id,
                        request=request,
                        summary=summary,
                        status=HandoffStatus.PENDING,
                    )
                    webhook_sent = delivery.status == WebhookStatus.SENT
                    webhook_status = delivery.status
                    handoff_record["webhook_sent"] = webhook_sent
                    handoff_record["webhook_status"] = webhook_status
                except WebhookError as e:
                    logger.warning(
                        "Webhook delivery failed",
                        handoff_id=str(handoff_id),
                        error=str(e),
                    )
                    webhook_status = WebhookStatus.FAILED
            
            # Get user message
            user_message = self._get_handoff_message(request.reason)
            
            # Record metrics
            latency = time.perf_counter() - start_time
            HANDOFF_LATENCY.labels(operation="initiate").observe(latency)
            HANDOFF_OPERATIONS.labels(operation="initiate", status="success").inc()
            HANDOFF_BY_REASON.labels(
                reason=request.reason.value,
                urgency=request.urgency.value,
            ).inc()
            
            logger.info(
                "Handoff initiated successfully",
                handoff_id=str(handoff_id),
                latency_ms=round(latency * 1000, 2),
                webhook_sent=webhook_sent,
            )
            
            return HandoffResponse(
                id=handoff_id,
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                status=HandoffStatus.PENDING,
                summary=summary,
                webhook_sent=webhook_sent,
                webhook_status=webhook_status,
                user_message=user_message,
            )

        except Exception as e:
            HANDOFF_OPERATIONS.labels(operation="initiate", status="error").inc()
            logger.error(
                "Failed to initiate handoff",
                handoff_id=str(handoff_id),
                error=str(e),
                exc_info=True,
            )
            raise HandoffServiceError(f"Failed to initiate handoff: {e}") from e

    def _generate_summary(self, request: HandoffRequest) -> ConversationSummary:
        """Generate a conversation summary from the request."""
        return self.summarizer.summarize(
            session_id=request.session_id,
            tenant_id=request.tenant_id,
            messages=request.recent_messages,
            reason=request.reason,
            urgency=request.urgency,
            funnel_stage=request.funnel_stage,
            slots=request.key_info,
        )

    async def _log_handoff_event(
        self,
        tenant_id: UUID,
        session_id: UUID,
        handoff_id: UUID,
        event_type: EventType,
        reason: HandoffReason,
        urgency: HandoffUrgency,
        summary: str,
    ) -> None:
        """Log handoff event to Event Store."""
        if not self.event_store:
            return
        
        event_data = HandoffEventData(
            reason=reason.value,
            summary=summary,
            urgency=urgency.value,
            key_info={"handoff_id": str(handoff_id)},
        )
        
        event = EventData(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type=event_type.value,
            event_data=event_data.model_dump(),
            event_metadata={"handoff_id": str(handoff_id)},
        )
        
        await self.event_store.append(event)

    async def _send_webhook(
        self,
        handoff_id: UUID,
        request: HandoffRequest,
        summary: ConversationSummary,
        status: HandoffStatus,
    ) -> Any:
        """Send webhook notification."""
        payload = self.webhook_dispatcher.create_payload(
            handoff_id=handoff_id,
            tenant_id=request.tenant_id,
            session_id=request.session_id,
            status=status,
            urgency=request.urgency,
            reason=request.reason,
            summary=summary.summary_text,
            key_info=summary.key_info.model_dump(),
            turn_count=summary.turn_count,
            funnel_stage=summary.funnel_stage,
            recent_messages=[
                {"role": m.role, "content": m.content}
                for m in summary.recent_messages
            ],
        )
        
        return await self.webhook_dispatcher.send(request.tenant_id, payload)

    def _get_handoff_message(self, reason: HandoffReason) -> str:
        """Get user-facing message for handoff."""
        return self.config.default_handoff_messages.get(
            reason.value,
            "Vou transferir você para um atendente humano.",
        )

    # ============================================
    # Handoff Status Management
    # ============================================

    async def update_handoff(
        self,
        update: HandoffUpdate,
    ) -> HandoffDetail:
        """
        Update handoff status.
        
        Args:
            update: Update with new status
            
        Returns:
            Updated handoff detail
            
        Raises:
            HandoffNotFoundError: If handoff not found
        """
        handoff = self._handoffs.get(update.handoff_id)
        if not handoff:
            raise HandoffNotFoundError(f"Handoff {update.handoff_id} not found")
        
        old_status = handoff["status"]
        handoff["status"] = update.status
        handoff["updated_at"] = update.updated_at
        
        if update.assigned_to:
            handoff["assigned_to"] = update.assigned_to
        
        if update.notes:
            handoff["resolution_notes"] = update.notes
        
        # Set resolved_at for terminal states
        if update.status in (
            HandoffStatus.COMPLETED,
            HandoffStatus.CANCELLED,
            HandoffStatus.TIMEOUT,
        ):
            handoff["resolved_at"] = update.updated_at
        
        # Log status change event
        await self._log_status_change_event(
            handoff=handoff,
            old_status=old_status,
            new_status=update.status,
        )
        
        # Send webhook for status change
        if self.webhook_dispatcher.is_configured(handoff["tenant_id"]):
            try:
                await self._send_status_webhook(handoff, update.status)
            except WebhookError as e:
                logger.warning(
                    "Status webhook failed",
                    handoff_id=str(update.handoff_id),
                    error=str(e),
                )
        
        HANDOFF_OPERATIONS.labels(
            operation="update",
            status=update.status.value,
        ).inc()
        
        logger.info(
            "Handoff updated",
            handoff_id=str(update.handoff_id),
            old_status=old_status.value if hasattr(old_status, 'value') else str(old_status),
            new_status=update.status.value,
        )
        
        return self._to_detail(handoff)

    async def accept_handoff(
        self,
        handoff_id: UUID,
        agent_id: Optional[str] = None,
    ) -> HandoffDetail:
        """Accept a handoff and assign to an agent."""
        return await self.update_handoff(HandoffUpdate(
            handoff_id=handoff_id,
            status=HandoffStatus.ACCEPTED,
            assigned_to=agent_id,
        ))

    async def complete_handoff(
        self,
        handoff_id: UUID,
        notes: Optional[str] = None,
    ) -> HandoffDetail:
        """Mark a handoff as completed."""
        return await self.update_handoff(HandoffUpdate(
            handoff_id=handoff_id,
            status=HandoffStatus.COMPLETED,
            notes=notes,
        ))

    async def cancel_handoff(
        self,
        handoff_id: UUID,
        notes: Optional[str] = None,
    ) -> HandoffDetail:
        """Cancel a handoff."""
        return await self.update_handoff(HandoffUpdate(
            handoff_id=handoff_id,
            status=HandoffStatus.CANCELLED,
            notes=notes,
        ))

    async def timeout_handoff(self, handoff_id: UUID) -> HandoffDetail:
        """Mark a handoff as timed out."""
        return await self.update_handoff(HandoffUpdate(
            handoff_id=handoff_id,
            status=HandoffStatus.TIMEOUT,
        ))

    async def _log_status_change_event(
        self,
        handoff: dict[str, Any],
        old_status: HandoffStatus,
        new_status: HandoffStatus,
    ) -> None:
        """Log status change to Event Store."""
        if not self.event_store:
            return
        
        # Map status to event type
        event_type_map = {
            HandoffStatus.ACCEPTED: EventType.HANDOFF_ACCEPT,
            HandoffStatus.COMPLETED: EventType.HANDOFF_COMPLETE,
            HandoffStatus.TIMEOUT: EventType.HANDOFF_TIMEOUT,
        }
        
        event_type = event_type_map.get(new_status)
        if not event_type:
            return
        
        event = EventData(
            tenant_id=handoff["tenant_id"],
            session_id=handoff["session_id"],
            event_type=event_type.value,
            event_data={
                "handoff_id": str(handoff["id"]),
                "old_status": old_status.value if hasattr(old_status, 'value') else str(old_status),
                "new_status": new_status.value,
                "assigned_to": handoff.get("assigned_to"),
            },
            event_metadata={},
        )
        
        await self.event_store.append(event)

    async def _send_status_webhook(
        self,
        handoff: dict[str, Any],
        status: HandoffStatus,
    ) -> None:
        """Send webhook for status change."""
        summary = handoff.get("summary")
        if not summary:
            return
        
        payload = self.webhook_dispatcher.create_payload(
            handoff_id=handoff["id"],
            tenant_id=handoff["tenant_id"],
            session_id=handoff["session_id"],
            status=status,
            urgency=handoff["urgency"],
            reason=handoff["reason"],
            summary=summary.summary_text if hasattr(summary, 'summary_text') else str(summary),
            key_info={},
            turn_count=0,
            funnel_stage="unknown",
        )
        
        await self.webhook_dispatcher.send(handoff["tenant_id"], payload)

    # ============================================
    # Queries
    # ============================================

    async def get_handoff(self, handoff_id: UUID) -> HandoffDetail:
        """
        Get handoff details.
        
        Args:
            handoff_id: Handoff identifier
            
        Returns:
            HandoffDetail
            
        Raises:
            HandoffNotFoundError: If not found
        """
        handoff = self._handoffs.get(handoff_id)
        if not handoff:
            raise HandoffNotFoundError(f"Handoff {handoff_id} not found")
        return self._to_detail(handoff)

    async def list_handoffs(
        self,
        tenant_id: UUID,
        status: Optional[HandoffStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[HandoffListItem]:
        """
        List handoffs for a tenant.
        
        Args:
            tenant_id: Tenant identifier
            status: Optional status filter
            limit: Maximum records
            offset: Skip records
            
        Returns:
            List of handoff items
        """
        handoffs = [
            h for h in self._handoffs.values()
            if h["tenant_id"] == tenant_id
        ]
        
        if status:
            handoffs = [h for h in handoffs if h["status"] == status]
        
        # Sort by created_at descending
        handoffs.sort(key=lambda x: x["created_at"], reverse=True)
        
        # Apply pagination
        handoffs = handoffs[offset:offset + limit]
        
        return [self._to_list_item(h) for h in handoffs]

    async def get_pending_handoffs(
        self,
        tenant_id: UUID,
    ) -> list[HandoffListItem]:
        """Get all pending handoffs for a tenant."""
        return await self.list_handoffs(
            tenant_id=tenant_id,
            status=HandoffStatus.PENDING,
        )

    async def get_active_handoff_for_session(
        self,
        session_id: UUID,
    ) -> Optional[HandoffDetail]:
        """Get active handoff for a session if any."""
        for handoff in self._handoffs.values():
            if (
                handoff["session_id"] == session_id
                and handoff["status"] in (
                    HandoffStatus.PENDING,
                    HandoffStatus.ACCEPTED,
                    HandoffStatus.IN_PROGRESS,
                )
            ):
                return self._to_detail(handoff)
        return None

    def _to_detail(self, handoff: dict[str, Any]) -> HandoffDetail:
        """Convert internal record to HandoffDetail."""
        summary = handoff.get("summary")
        return HandoffDetail(
            id=handoff["id"],
            tenant_id=handoff["tenant_id"],
            session_id=handoff["session_id"],
            status=handoff["status"],
            urgency=handoff["urgency"],
            reason=handoff["reason"],
            summary=summary.summary_text if hasattr(summary, 'summary_text') else "",
            key_info=summary.key_info.model_dump() if summary and hasattr(summary, 'key_info') else {},
            recent_messages=[
                m.model_dump() 
                for m in (summary.recent_messages if summary and hasattr(summary, 'recent_messages') else [])
            ],
            assigned_to=handoff.get("assigned_to"),
            webhook_sent=handoff.get("webhook_sent", False),
            webhook_status=handoff.get("webhook_status"),
            created_at=handoff["created_at"],
            updated_at=handoff.get("updated_at"),
            resolved_at=handoff.get("resolved_at"),
            resolution_notes=handoff.get("resolution_notes"),
        )

    def _to_list_item(self, handoff: dict[str, Any]) -> HandoffListItem:
        """Convert internal record to HandoffListItem."""
        summary = handoff.get("summary")
        summary_preview = ""
        if summary:
            if hasattr(summary, 'summary_text'):
                summary_preview = summary.summary_text[:100]
            else:
                summary_preview = str(summary)[:100]
        
        return HandoffListItem(
            id=handoff["id"],
            session_id=handoff["session_id"],
            status=handoff["status"],
            urgency=handoff["urgency"],
            reason=handoff["reason"],
            summary_preview=summary_preview,
            created_at=handoff["created_at"],
            assigned_to=handoff.get("assigned_to"),
        )

    # ============================================
    # Webhook Configuration
    # ============================================

    def configure_webhook(
        self,
        tenant_id: UUID,
        config: WebhookConfig,
    ) -> None:
        """Configure webhook for a tenant."""
        self.webhook_dispatcher.set_config(tenant_id, config)
        logger.info(
            "Webhook configured",
            tenant_id=str(tenant_id),
            url=config.url,
            enabled=config.enabled,
        )

    def get_webhook_config(self, tenant_id: UUID) -> Optional[WebhookConfig]:
        """Get webhook configuration for a tenant."""
        return self.webhook_dispatcher.get_config(tenant_id)

    def remove_webhook_config(self, tenant_id: UUID) -> bool:
        """Remove webhook configuration for a tenant."""
        return self.webhook_dispatcher.remove_config(tenant_id)


# ============================================
# Singleton Instance
# ============================================

_service: Optional[HandoffService] = None


def get_handoff_service(
    event_store: Optional[EventStore] = None,
) -> HandoffService:
    """Get or create the global HandoffService instance."""
    global _service
    if _service is None:
        _service = HandoffService(event_store=event_store)
    return _service


async def close_handoff_service() -> None:
    """Close the global HandoffService instance."""
    global _service
    if _service is not None:
        if _service.webhook_dispatcher:
            await _service.webhook_dispatcher.close()
        _service = None

