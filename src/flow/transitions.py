"""
NeuroStack - Flow Manager - Transition Logger
Handles logging of state transitions to Event Store.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

from src.flow.models import (
    FlowMode,
    FlowTransition,
    TransitionResult,
    TransitionTrigger,
)

logger = logging.getLogger(__name__)


class TransitionLogger:
    """
    Logs state transitions to the Event Store.

    Provides audit trail and analytics for flow transitions.
    """

    def __init__(self) -> None:
        """Initialize the transition logger."""
        self._event_buffer: list[dict[str, Any]] = []
        self._buffer_size = 100

    async def log_transition(
        self,
        transition: FlowTransition,
        result: TransitionResult,
        additional_data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Log a state transition.

        Args:
            transition: The transition that occurred
            result: Result of the transition
            additional_data: Additional data to log

        Returns:
            Event data that was logged
        """
        event = {
            "event_type": "flow.transition",
            "timestamp": transition.timestamp.isoformat(),
            "session_id": str(transition.session_id) if transition.session_id else None,
            "tenant_id": str(transition.tenant_id) if transition.tenant_id else None,
            "data": {
                "from_mode": transition.from_mode.value,
                "to_mode": transition.to_mode.value,
                "trigger": transition.trigger.value,
                "result": result.value,
                "reason": transition.reason,
                **(additional_data or {}),
            },
        }

        # Buffer the event
        self._event_buffer.append(event)

        # Flush if buffer is full
        if len(self._event_buffer) >= self._buffer_size:
            await self.flush_events()

        logger.info(
            f"Transition logged: {transition.from_mode.value} -> {transition.to_mode.value} "
            f"via {transition.trigger.value} ({result.value})"
        )

        return event

    async def log_mode_entry(
        self,
        session_id: UUID,
        tenant_id: UUID,
        mode: FlowMode,
        reason: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Log entry into a mode.

        Args:
            session_id: Session ID
            tenant_id: Tenant ID
            mode: Mode being entered
            reason: Reason for entering
            metadata: Additional metadata

        Returns:
            Event data
        """
        event = {
            "event_type": f"flow.mode.{mode.value}.entry",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": str(session_id),
            "tenant_id": str(tenant_id),
            "data": {
                "mode": mode.value,
                "reason": reason,
                **(metadata or {}),
            },
        }

        self._event_buffer.append(event)

        logger.debug(f"Mode entry logged: {mode.value}")

        return event

    async def log_mode_exit(
        self,
        session_id: UUID,
        tenant_id: UUID,
        mode: FlowMode,
        duration_seconds: float,
        reason: str = "",
        metadata: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Log exit from a mode.

        Args:
            session_id: Session ID
            tenant_id: Tenant ID
            mode: Mode being exited
            duration_seconds: Time spent in mode
            reason: Reason for exiting
            metadata: Additional metadata

        Returns:
            Event data
        """
        event = {
            "event_type": f"flow.mode.{mode.value}.exit",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": str(session_id),
            "tenant_id": str(tenant_id),
            "data": {
                "mode": mode.value,
                "duration_seconds": duration_seconds,
                "reason": reason,
                **(metadata or {}),
            },
        }

        self._event_buffer.append(event)

        logger.debug(f"Mode exit logged: {mode.value} after {duration_seconds:.2f}s")

        return event

    async def log_handler_result(
        self,
        session_id: UUID,
        tenant_id: UUID,
        mode: FlowMode,
        success: bool,
        processing_time_ms: float,
        events_to_log: list[dict[str, Any]],
    ) -> None:
        """
        Log handler result events.

        Args:
            session_id: Session ID
            tenant_id: Tenant ID
            mode: Current mode
            success: Whether handler succeeded
            processing_time_ms: Processing time
            events_to_log: Events from handler
        """
        # Add base event
        base_event = {
            "event_type": f"flow.handler.{mode.value}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "session_id": str(session_id),
            "tenant_id": str(tenant_id),
            "data": {
                "success": success,
                "processing_time_ms": processing_time_ms,
            },
        }
        self._event_buffer.append(base_event)

        # Add handler-specific events
        for event in events_to_log:
            enriched_event = {
                **event,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "session_id": str(session_id),
                "tenant_id": str(tenant_id),
            }
            self._event_buffer.append(enriched_event)

    async def flush_events(self) -> int:
        """
        Flush buffered events to Event Store.

        This is a placeholder - would integrate with actual Event Store.

        Returns:
            Number of events flushed
        """
        if not self._event_buffer:
            return 0

        count = len(self._event_buffer)

        # Placeholder - would send to Event Store
        # await self._event_store.batch_write(self._event_buffer)

        logger.debug(f"Flushed {count} events to Event Store")

        self._event_buffer = []
        return count

    def get_buffered_events(self) -> list[dict[str, Any]]:
        """Get currently buffered events."""
        return self._event_buffer.copy()


class TransitionAnalytics:
    """
    Provides analytics on state transitions.
    """

    def __init__(self) -> None:
        """Initialize analytics."""
        self._transition_counts: dict[str, int] = {}

    def record_transition(
        self,
        from_mode: FlowMode,
        to_mode: FlowMode,
        trigger: TransitionTrigger,
    ) -> None:
        """Record a transition for analytics."""
        key = f"{from_mode.value}:{to_mode.value}:{trigger.value}"
        self._transition_counts[key] = self._transition_counts.get(key, 0) + 1

    def get_transition_stats(self) -> dict[str, Any]:
        """Get transition statistics."""
        total = sum(self._transition_counts.values())

        return {
            "total_transitions": total,
            "by_transition": self._transition_counts,
            "most_common": max(
                self._transition_counts.items(), key=lambda x: x[1], default=("none", 0)
            ),
        }


# ============================================
# Singleton Instances
# ============================================

_transition_logger: Optional[TransitionLogger] = None
_transition_analytics: Optional[TransitionAnalytics] = None


def get_transition_logger() -> TransitionLogger:
    """Get or create the global TransitionLogger instance."""
    global _transition_logger
    if _transition_logger is None:
        _transition_logger = TransitionLogger()
    return _transition_logger


def get_transition_analytics() -> TransitionAnalytics:
    """Get or create the global TransitionAnalytics instance."""
    global _transition_analytics
    if _transition_analytics is None:
        _transition_analytics = TransitionAnalytics()
    return _transition_analytics
