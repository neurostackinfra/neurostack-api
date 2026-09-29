"""Canonical PostgreSQL-backed human handoff lifecycle."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Optional, TypeVar, cast
from uuid import UUID, uuid4

from prometheus_client import Counter, Histogram
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from neurostack_core.prompt import resolve_clinic_tz
from src.config.logging import get_logger
from src.core.exceptions import HandoffNotFoundError, HandoffServiceError, WebhookError
from src.handoff.models import (
    ConversationSummary,
    HandoffDetail,
    HandoffListItem,
    HandoffReason,
    HandoffRequest,
    HandoffResponse,
    HandoffSector,
    HandoffServiceConfig,
    HandoffSLA,
    HandoffStatus,
    HandoffUpdate,
    HandoffUrgency,
    WebhookConfig,
    WebhookDelivery,
    WebhookStatus,
)
from src.handoff.sla import HandoffStatus as SlaHandoffStatus
from src.handoff.sla import (
    SlaCategory,
    SlaMetrics,
    SlaOccurrence,
    SlaPeriod,
    classify_sla,
    resolve_sla_period,
    resolve_sla_target,
)
from src.handoff.summarizer import ConversationSummarizer, get_summarizer
from src.handoff.webhook import WebhookDispatcher, get_webhook_dispatcher
from src.memory.event_store import (
    EventData,
    EventStore,
    EventType,
    HandoffEventData,
    get_event_store,
)
from src.models.clinic_config_table import ClinicConfigTable
from src.models.handoff import HandoffRecord
from src.models.session import Session as SessionRecord
from src.repositories.database import get_db_session
from src.repositories.handoff import (
    HandoffCasOutcome,
    HandoffCasResult,
    HandoffRecordRepository,
    HandoffSlaCohortRow,
)

logger = get_logger(__name__)


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


SessionFactory = Callable[[], AsyncSession]
_EnumT = TypeVar("_EnumT")
_ACTIVE_STATUSES = (
    HandoffStatus.PENDING.value,
    HandoffStatus.ACCEPTED.value,
    HandoffStatus.IN_PROGRESS.value,
)
_TRANSITIONS: dict[HandoffStatus, frozenset[HandoffStatus]] = {
    HandoffStatus.PENDING: frozenset(
        {
            HandoffStatus.ACCEPTED,
            HandoffStatus.IN_PROGRESS,
            HandoffStatus.CANCELLED,
            HandoffStatus.TIMEOUT,
        }
    ),
    HandoffStatus.ACCEPTED: frozenset(
        {
            HandoffStatus.IN_PROGRESS,
            HandoffStatus.CANCELLED,
            HandoffStatus.TIMEOUT,
        }
    ),
    HandoffStatus.IN_PROGRESS: frozenset(
        {
            HandoffStatus.COMPLETED,
            HandoffStatus.CANCELLED,
            HandoffStatus.TIMEOUT,
        }
    ),
    HandoffStatus.COMPLETED: frozenset(),
    HandoffStatus.CANCELLED: frozenset(),
    HandoffStatus.TIMEOUT: frozenset(),
}


@dataclass(frozen=True)
class HandoffQueueSlaResult:
    """One resolved queue view derived from one persisted SLA cohort."""

    items: tuple[HandoffSlaCohortRow, ...]
    metrics: SlaMetrics
    within_sla_count: int
    confirmed_late_count: int
    unanswered_overdue_count: int
    maturing_count: int
    cancelled_unconfirmed_count: int
    unmeasured_legacy_count: int
    period_timezone: str
    period: SlaPeriod


@dataclass(frozen=True)
class ResolvedHandoffQueuePeriod:
    """Clinic-authoritative civil dates and their resolved UTC interval."""

    from_date: date
    to_date: date
    timezone: str
    period: SlaPeriod


class HandoffLifecycleError(HandoffServiceError):
    """Stable lifecycle rejection exposed to HTTP adapters as a 409 code."""

    def __init__(self, code: str, current_status: str | None = None):
        super().__init__(code)
        self.code = code
        self.current_status = current_status


class HandoffPostCommitError(HandoffServiceError):
    """A required public effect failed after PostgreSQL committed."""


class HandoffSlaPeriodError(HandoffServiceError):
    """The requested civil-date window cannot form an SLA cohort."""


def _default_session_factory() -> AsyncSession:
    return get_db_session()


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _enum_or_default(enum_type: type[_EnumT], value: object, default: _EnumT) -> _EnumT:
    try:
        return enum_type(value)  # type: ignore[call-arg]
    except (TypeError, ValueError):
        return default


def _optional_enum(enum_type: type[_EnumT], value: object) -> _EnumT | None:
    if value is None:
        return None
    try:
        return enum_type(value)  # type: ignore[call-arg]
    except (TypeError, ValueError):
        return None


def _record_context(record: HandoffRecord) -> dict[str, Any]:
    return dict(record.context) if isinstance(record.context, dict) else {}


def _record_key_info(context: Mapping[str, Any]) -> dict[str, Any]:
    key_info = context.get("key_info")
    if isinstance(key_info, dict):
        return dict(key_info)
    reserved = {
        "context_version",
        "sector",
        "sla",
        "verification_status",
        "funnel_stage",
        "turn_count",
        "webhook_status",
        "safety_concern",
    }
    return {key: value for key, value in context.items() if key not in reserved}


def handoff_record_to_detail(
    record: HandoffRecord,
    recent_messages: Sequence[Mapping[str, Any]] = (),
) -> HandoffDetail:
    """Map one persisted record plus a bounded Event Store projection."""

    context = _record_context(record)
    webhook_status = _optional_enum(WebhookStatus, context.get("webhook_status"))
    detail = HandoffDetail(
        id=record.id,
        tenant_id=record.tenant_id,
        session_id=record.session_id,
        status=_enum_or_default(HandoffStatus, record.status, HandoffStatus.PENDING),
        urgency=_enum_or_default(
            HandoffUrgency,
            record.urgency,
            HandoffUrgency.NORMAL,
        ),
        reason=_enum_or_default(
            HandoffReason,
            record.reason,
            HandoffReason.USER_REQUEST,
        ),
        sector=_optional_enum(HandoffSector, context.get("sector")),
        sla=_optional_enum(HandoffSLA, context.get("sla")),
        verification_status=(
            str(context["verification_status"])
            if context.get("verification_status") is not None
            else None
        ),
        summary=record.summary or "",
        key_info=_record_key_info(context),
        recent_messages=[dict(message) for message in recent_messages][-5:],
        assigned_to=record.assigned_to,
        webhook_sent=record.webhook_sent,
        webhook_status=webhook_status,
        webhook_response=(
            dict(record.webhook_response)
            if isinstance(record.webhook_response, dict)
            else None
        ),
        created_at=_aware(record.created_at) or datetime.now(timezone.utc),
        updated_at=_aware(record.updated_at),
        resolved_at=_aware(record.resolved_at),
        data_hora_saida=_aware(record.data_hora_saida),
        resolution_notes=record.resolution_notes,
    )
    context_safety = context.get("safety_concern")
    safety_flag = (
        context_safety
        if type(context_safety) is bool
        else record.reason == HandoffReason.SAFETY_CONCERN.value
    )
    detected_intent = record.intencao_detectada
    if detected_intent in {reason.value for reason in HandoffReason}:
        detected_intent = None
    detail._checkout_compatibility = {
        "numero_whatsapp": record.numero_whatsapp,
        "safety_flag": safety_flag,
        "intencao_detectada": detected_intent,
        "estagio_atual": record.estagio_atual,
        "primeira_linha_mensagem": record.primeira_linha_mensagem,
        "sla_target_seconds": record.sla_target_seconds,
    }
    return detail


def handoff_record_to_list_item(record: HandoffRecord) -> HandoffListItem:
    """Map a record for list responses without loading conversation PII."""

    context = _record_context(record)
    return HandoffListItem(
        id=record.id,
        session_id=record.session_id,
        status=_enum_or_default(HandoffStatus, record.status, HandoffStatus.PENDING),
        urgency=_enum_or_default(
            HandoffUrgency,
            record.urgency,
            HandoffUrgency.NORMAL,
        ),
        reason=_enum_or_default(
            HandoffReason,
            record.reason,
            HandoffReason.USER_REQUEST,
        ),
        sector=_optional_enum(HandoffSector, context.get("sector")),
        sla=_optional_enum(HandoffSLA, context.get("sla")),
        verification_status=(
            str(context["verification_status"])
            if context.get("verification_status") is not None
            else None
        ),
        summary_preview=(record.summary or "")[:100],
        created_at=_aware(record.created_at) or datetime.now(timezone.utc),
        assigned_to=record.assigned_to,
    )


class HandoffService:
    """Stateless orchestrator over a fresh SQLAlchemy unit of work per operation."""

    def __init__(
        self,
        event_store: Optional[EventStore] = None,
        summarizer: Optional[ConversationSummarizer] = None,
        webhook_dispatcher: Optional[WebhookDispatcher] = None,
        config: Optional[HandoffServiceConfig] = None,
        session_factory: Optional[SessionFactory] = None,
    ) -> None:
        self.event_store = event_store
        self.summarizer = summarizer or get_summarizer()
        self.webhook_dispatcher = webhook_dispatcher or get_webhook_dispatcher()
        self.config = config or HandoffServiceConfig()
        self._session_factory = session_factory or _default_session_factory

    async def _resolved_event_store(self) -> EventStore:
        """Resolve the runtime Event Store lazily while preserving injection."""

        if self.event_store is None:
            self.event_store = await get_event_store()
        return self.event_store

    async def initiate_handoff(self, request: HandoffRequest) -> HandoffResponse:
        """Persist first, then preserve Event Store/webhook/metric side effects."""

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
            summary = self._generate_summary(request)
            context = self._build_context(request, summary)
            async with self._session_factory() as session:
                async with session.begin():
                    matching_session = await session.scalar(
                        select(SessionRecord.id).where(
                            SessionRecord.id == request.session_id,
                            SessionRecord.tenant_id == request.tenant_id,
                        )
                    )
                    if matching_session is None:
                        raise HandoffServiceError("handoff_session_not_found")
                    config_result = await session.execute(
                        select(ClinicConfigTable.handoff).where(
                            ClinicConfigTable.tenant_id == request.tenant_id
                        )
                    )
                    raw_config = config_result.scalar_one_or_none()
                    target = resolve_sla_target(
                        raw_config if isinstance(raw_config, dict) else None
                    )
                    if target.used_fallback:
                        logger.warning(
                            "handoff_sla_target_fallback",
                            tenant_id=str(request.tenant_id),
                            reason=target.fallback_reason,
                        )
                    record = await HandoffRecordRepository(session).create(
                        id=handoff_id,
                        tenant_id=request.tenant_id,
                        session_id=request.session_id,
                        reason=request.reason.value,
                        summary=summary.summary_text,
                        context=context,
                        urgency=request.urgency.value,
                        status=HandoffStatus.PENDING.value,
                        numero_whatsapp=self._whatsapp_number(request.key_info),
                        intencao_detectada=request.detected_intent,
                        estagio_atual=request.funnel_stage,
                        primeira_linha_mensagem=self._first_user_message(request),
                        sla_target_seconds=target.seconds,
                    )

            await self._log_handoff_event(
                tenant_id=request.tenant_id,
                session_id=request.session_id,
                handoff_id=handoff_id,
                event_type=EventType.HANDOFF_REQUEST,
                reason=request.reason,
                urgency=request.urgency,
                summary=summary.summary_text,
            )
            webhook_sent, webhook_status = await self._deliver_creation_webhook(
                record,
                summary,
            )

            latency = time.perf_counter() - start_time
            HANDOFF_LATENCY.labels(operation="initiate").observe(latency)
            HANDOFF_OPERATIONS.labels(operation="initiate", status="success").inc()
            HANDOFF_BY_REASON.labels(
                reason=request.reason.value,
                urgency=request.urgency.value,
            ).inc()
            return HandoffResponse(
                id=record.id,
                tenant_id=record.tenant_id,
                session_id=record.session_id,
                status=HandoffStatus.PENDING,
                summary=summary,
                webhook_sent=webhook_sent,
                webhook_status=webhook_status,
                user_message=self._get_handoff_message(request.reason),
                verification_status=request.verification_status,
                created_at=_aware(record.created_at) or datetime.now(timezone.utc),
            )
        except HandoffServiceError:
            HANDOFF_OPERATIONS.labels(operation="initiate", status="error").inc()
            raise
        except Exception as exc:
            HANDOFF_OPERATIONS.labels(operation="initiate", status="error").inc()
            logger.error(
                "Unexpected error initiating handoff",
                handoff_id=str(handoff_id),
                tenant_id=str(request.tenant_id),
                session_id=str(request.session_id),
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
            )
            raise HandoffServiceError(f"Failed to initiate handoff: {exc}") from exc

    def _generate_summary(self, request: HandoffRequest) -> ConversationSummary:
        return self.summarizer.summarize(
            session_id=request.session_id,
            tenant_id=request.tenant_id,
            messages=request.recent_messages,
            reason=request.reason,
            urgency=request.urgency,
            funnel_stage=request.funnel_stage,
            slots=request.key_info,
            sector=request.sector,
            sla=request.sla,
            verification_status=request.verification_status,
        )

    @staticmethod
    def _build_context(
        request: HandoffRequest,
        summary: ConversationSummary,
    ) -> dict[str, Any]:
        key_info = summary.key_info.model_dump()
        key_info.update(request.key_info)
        context: dict[str, Any] = {
            "context_version": 1,
            "sector": request.sector.value if request.sector is not None else None,
            "sla": request.sla.value if request.sla is not None else None,
            "key_info": key_info,
            "verification_status": request.verification_status,
            "funnel_stage": request.funnel_stage,
            "turn_count": summary.turn_count,
            "webhook_status": None,
        }
        if request.safety_concern is not None:
            context["safety_concern"] = request.safety_concern
        return context

    @staticmethod
    def _first_user_message(request: HandoffRequest) -> str | None:
        for message in request.recent_messages:
            content = message.get("content") if isinstance(message, dict) else None
            role = message.get("role", "user") if isinstance(message, dict) else None
            if role == "user" and content:
                return str(content)[:500]
        return None

    @staticmethod
    def _whatsapp_number(key_info: Mapping[str, Any]) -> str | None:
        raw = (
            key_info.get("whatsapp_number")
            or key_info.get("phone")
            or key_info.get("numero_whatsapp")
        )
        return str(raw) if raw is not None else None

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
        try:
            event_store = await self._resolved_event_store()
            event_data = HandoffEventData(
                reason=reason.value,
                summary=summary,
                urgency=urgency.value,
                key_info={"handoff_id": str(handoff_id)},
            )
            await event_store.append(
                EventData(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    event_type=event_type.value,
                    event_data=event_data.model_dump(),
                    event_metadata={"handoff_id": str(handoff_id)},
                )
            )
        except Exception as exc:
            raise HandoffPostCommitError(
                "Handoff committed but Event Store append failed"
            ) from exc

    async def _recent_messages(
        self,
        tenant_id: UUID,
        session_id: UUID,
    ) -> list[dict[str, str]]:
        try:
            event_store = await self._resolved_event_store()
            events = await event_store.get_recent_messages(
                tenant_id,
                session_id,
                limit=5,
            )
        except Exception as exc:
            logger.warning(
                "handoff_recent_messages_unavailable",
                tenant_id=str(tenant_id),
                session_id=str(session_id),
                error_type=type(exc).__name__,
            )
            return []
        projection: list[dict[str, str]] = []
        for event in events[-5:]:
            data = event.event_data if isinstance(event.event_data, dict) else {}
            content = data.get("content")
            role = data.get("role")
            if content is None:
                continue
            if role not in {"user", "assistant"}:
                role = (
                    "user"
                    if event.event_type == EventType.MESSAGE_IN.value
                    else "assistant"
                )
            projection.append({"role": str(role), "content": str(content)})
        return projection[-5:]

    def _get_handoff_message(self, reason: HandoffReason) -> str:
        return self.config.default_handoff_messages.get(
            reason.value,
            "Vou encaminhar você para nossa equipe, que dará continuidade ao seu atendimento.",
        )

    async def _deliver_creation_webhook(
        self,
        record: HandoffRecord,
        summary: ConversationSummary,
    ) -> tuple[bool, WebhookStatus | None]:
        if not self.webhook_dispatcher.is_configured(record.tenant_id):
            return False, None
        recent_messages = await self._recent_messages(
            record.tenant_id, record.session_id
        )
        payload = self.webhook_dispatcher.create_payload(
            handoff_id=record.id,
            tenant_id=record.tenant_id,
            session_id=record.session_id,
            status=HandoffStatus.PENDING,
            urgency=_enum_or_default(
                HandoffUrgency,
                record.urgency,
                HandoffUrgency.NORMAL,
            ),
            reason=_enum_or_default(
                HandoffReason,
                record.reason,
                HandoffReason.USER_REQUEST,
            ),
            summary=record.summary or "",
            key_info=summary.key_info.model_dump(),
            verification_status=summary.verification_status,
            turn_count=summary.turn_count,
            funnel_stage=summary.funnel_stage,
            recent_messages=recent_messages,
            sector=summary.sector,
            sla=summary.sla,
        )
        try:
            delivery = await self.webhook_dispatcher.send(record.tenant_id, payload)
            status = delivery.status
            sent = status == WebhookStatus.SENT
            await self._persist_delivery(record, delivery=delivery)
            return sent, status
        except WebhookError as exc:
            logger.warning(
                "handoff_creation_webhook_delivery_failed",
                handoff_id=str(record.id),
                error_type=type(exc).__name__,
            )
            await self._persist_delivery(record, error=exc)
            return False, WebhookStatus.FAILED

    async def _persist_delivery(
        self,
        record: HandoffRecord,
        *,
        delivery: WebhookDelivery | None = None,
        error: Exception | None = None,
    ) -> None:
        status = delivery.status if delivery is not None else WebhookStatus.FAILED
        response: dict[str, Any] = {
            "status": status.value,
            "attempt": delivery.attempt if delivery is not None else 1,
        }
        if delivery is not None and delivery.response_status is not None:
            response["response_status"] = delivery.response_status
        if status == WebhookStatus.FAILED or error is not None:
            response["error_type"] = "delivery_failed"
        try:
            async with self._session_factory() as session:
                async with session.begin():
                    persisted = await HandoffRecordRepository(
                        session
                    ).update_webhook_delivery(
                        tenant_id=record.tenant_id,
                        handoff_id=record.id,
                        sent=status == WebhookStatus.SENT,
                        status=status.value,
                        response=response,
                    )
                    if not persisted:
                        raise HandoffPostCommitError(
                            "Webhook delivered but its handoff bookkeeping row is missing"
                        )
        except HandoffPostCommitError:
            raise
        except Exception as exc:
            raise HandoffPostCommitError(
                "Webhook delivery bookkeeping could not be persisted"
            ) from exc
        context = _record_context(record)
        context["webhook_status"] = status.value
        record.context = context
        record.webhook_sent = status == WebhookStatus.SENT
        record.webhook_response = response

    async def enrich_scheduling_verification(
        self,
        session_id: UUID,
        verification_status: str,
        handoff_id: Optional[UUID] = None,
        *,
        tenant_id: UUID,
    ) -> Optional[UUID]:
        """CAS-update verification for an active tenant-scoped handoff."""

        async with self._session_factory() as session:
            async with session.begin():
                repository = HandoffRecordRepository(session)
                record = (
                    await repository.get_for_tenant(tenant_id, handoff_id)
                    if handoff_id is not None
                    else await repository.find_active_for_session(tenant_id, session_id)
                )
                if record is None or record.status not in _ACTIVE_STATUSES:
                    logger.warning(
                        "scheduling_link_enrich_no_handoff",
                        tenant_id=str(tenant_id),
                        session_id=str(session_id),
                        verification_status=verification_status,
                    )
                    return None
                context = _record_context(record)
                context["verification_status"] = verification_status
                changed_at = await repository.current_timestamp()
                result = await repository.compare_and_set(
                    tenant_id=tenant_id,
                    handoff_id=record.id,
                    observed_status=record.status,
                    observed_lifecycle_version=record.lifecycle_version,
                    allowed_statuses=_ACTIVE_STATUSES,
                    changed_at=changed_at,
                    values={"context": context},
                )
        updated = self._require_applied(result)
        recent_messages = await self._recent_messages(
            updated.tenant_id,
            updated.session_id,
        )
        try:
            await self._deliver_status_webhook(
                updated,
                HandoffStatus(updated.status),
                recent_messages,
                event_type="handoff.updated",
            )
        except HandoffPostCommitError:
            HANDOFF_OPERATIONS.labels(operation="update", status="error").inc()
            raise
        HANDOFF_OPERATIONS.labels(operation="update", status=updated.status).inc()
        return updated.id

    async def update_handoff(
        self,
        update: HandoffUpdate,
        *,
        tenant_id: UUID,
    ) -> HandoffDetail:
        """Apply the generic update contract through the same lifecycle CAS."""

        if update.status == HandoffStatus.IN_PROGRESS:
            raise HandoffLifecycleError("attendance_confirmation_required")
        return await self._transition(
            tenant_id=tenant_id,
            handoff_id=update.handoff_id,
            target_status=update.status,
            operation="update",
            assigned_to=update.assigned_to,
            notes=update.notes,
            verification_status=update.verification_status,
        )

    async def accept_handoff(
        self,
        handoff_id: UUID,
        agent_id: Optional[str] = None,
        *,
        tenant_id: UUID,
    ) -> HandoffDetail:
        return await self._transition(
            tenant_id=tenant_id,
            handoff_id=handoff_id,
            target_status=HandoffStatus.ACCEPTED,
            operation="accept",
            assigned_to=agent_id,
        )

    async def confirm_handoff(
        self,
        handoff_id: UUID,
        *,
        tenant_id: UUID,
    ) -> HandoffDetail:
        return await self._transition(
            tenant_id=tenant_id,
            handoff_id=handoff_id,
            target_status=HandoffStatus.IN_PROGRESS,
            operation="confirm",
            confirmation=True,
        )

    async def checkout_handoff(
        self,
        handoff_id: UUID,
        *,
        tenant_id: UUID,
    ) -> HandoffDetail:
        """Deprecated semantic alias for attendance confirmation."""

        return await self.confirm_handoff(handoff_id, tenant_id=tenant_id)

    async def complete_handoff(
        self,
        handoff_id: UUID,
        notes: Optional[str] = None,
        *,
        tenant_id: UUID,
    ) -> HandoffDetail:
        return await self._transition(
            tenant_id=tenant_id,
            handoff_id=handoff_id,
            target_status=HandoffStatus.COMPLETED,
            operation="complete",
            notes=notes,
        )

    async def cancel_handoff(
        self,
        handoff_id: UUID,
        notes: Optional[str] = None,
        *,
        tenant_id: UUID,
    ) -> HandoffDetail:
        return await self._transition(
            tenant_id=tenant_id,
            handoff_id=handoff_id,
            target_status=HandoffStatus.CANCELLED,
            operation="cancel",
            notes=notes,
        )

    async def timeout_handoff(
        self,
        handoff_id: UUID,
        *,
        tenant_id: UUID,
    ) -> HandoffDetail:
        return await self._transition(
            tenant_id=tenant_id,
            handoff_id=handoff_id,
            target_status=HandoffStatus.TIMEOUT,
            operation="timeout",
        )

    async def _transition(
        self,
        *,
        tenant_id: UUID,
        handoff_id: UUID,
        target_status: HandoffStatus,
        operation: str,
        assigned_to: str | None = None,
        notes: str | None = None,
        verification_status: str | None = None,
        confirmation: bool = False,
    ) -> HandoffDetail:
        async with self._session_factory() as session:
            async with session.begin():
                repository = HandoffRecordRepository(session)
                record = await repository.get_for_tenant(tenant_id, handoff_id)
                if record is None:
                    raise HandoffNotFoundError(f"Handoff {handoff_id} not found")
                try:
                    current_status = HandoffStatus(record.status)
                except ValueError as exc:
                    raise HandoffLifecycleError(
                        "invalid_handoff_state",
                        record.status,
                    ) from exc
                self._validate_transition(
                    record,
                    current_status=current_status,
                    target_status=target_status,
                    confirmation=confirmation,
                    allow_same_status=operation == "update",
                )
                changed_at = await repository.current_timestamp()
                values = self._transition_values(
                    record,
                    changed_at=changed_at,
                    target_status=target_status,
                    assigned_to=assigned_to,
                    notes=notes,
                    verification_status=verification_status,
                    confirmation=confirmation,
                )
                preconditions = []
                if confirmation:
                    preconditions.append(HandoffRecord.data_hora_saida.is_(None))
                elif target_status == HandoffStatus.COMPLETED:
                    preconditions.append(HandoffRecord.data_hora_saida.is_not(None))
                result = await repository.compare_and_set(
                    tenant_id=tenant_id,
                    handoff_id=handoff_id,
                    observed_status=record.status,
                    observed_lifecycle_version=record.lifecycle_version,
                    allowed_statuses=(record.status,),
                    changed_at=changed_at,
                    values=values,
                    preconditions=preconditions,
                )
        updated = self._require_applied(result)
        recent_messages = await self._after_transition(
            updated,
            old_status=current_status,
            new_status=target_status,
            operation=operation,
        )
        return handoff_record_to_detail(updated, recent_messages)

    @staticmethod
    def _validate_transition(
        record: HandoffRecord,
        *,
        current_status: HandoffStatus,
        target_status: HandoffStatus,
        confirmation: bool,
        allow_same_status: bool,
    ) -> None:
        if confirmation:
            if record.data_hora_saida is not None:
                raise HandoffLifecycleError(
                    "attendance_already_confirmed",
                    current_status.value,
                )
            if current_status not in {
                HandoffStatus.PENDING,
                HandoffStatus.ACCEPTED,
                HandoffStatus.IN_PROGRESS,
            }:
                raise HandoffLifecycleError(
                    "invalid_handoff_state",
                    current_status.value,
                )
            return
        if target_status == current_status:
            if allow_same_status and current_status in {
                HandoffStatus.PENDING,
                HandoffStatus.ACCEPTED,
                HandoffStatus.IN_PROGRESS,
            }:
                return
            raise HandoffLifecycleError(
                "invalid_handoff_state",
                current_status.value,
            )
        if target_status not in _TRANSITIONS[current_status]:
            code = (
                "attendance_not_confirmed"
                if target_status == HandoffStatus.COMPLETED
                and record.data_hora_saida is None
                else "invalid_handoff_state"
            )
            raise HandoffLifecycleError(code, current_status.value)
        if target_status == HandoffStatus.COMPLETED and record.data_hora_saida is None:
            raise HandoffLifecycleError(
                "attendance_not_confirmed",
                current_status.value,
            )

    @staticmethod
    def _transition_values(
        record: HandoffRecord,
        *,
        changed_at: datetime,
        target_status: HandoffStatus,
        assigned_to: str | None,
        notes: str | None,
        verification_status: str | None,
        confirmation: bool,
    ) -> dict[str, Any]:
        values: dict[str, Any] = {"status": target_status.value}
        if assigned_to is not None:
            values["assigned_to"] = assigned_to
        if notes is not None:
            values["resolution_notes"] = notes
        if verification_status is not None:
            context = _record_context(record)
            context["verification_status"] = verification_status
            values["context"] = context
        if confirmation:
            values["data_hora_saida"] = changed_at
        if target_status in {
            HandoffStatus.COMPLETED,
            HandoffStatus.CANCELLED,
            HandoffStatus.TIMEOUT,
        }:
            values["resolved_at"] = changed_at
        return values

    @staticmethod
    def _require_applied(result: HandoffCasResult) -> HandoffRecord:
        if result.outcome == HandoffCasOutcome.APPLIED and result.record is not None:
            return result.record
        if result.outcome == HandoffCasOutcome.NOT_FOUND:
            raise HandoffNotFoundError("Handoff not found")
        if result.outcome == HandoffCasOutcome.CONFLICT:
            raise HandoffLifecycleError("handoff_conflict", result.current_status)
        raise HandoffLifecycleError("invalid_handoff_state", result.current_status)

    async def _after_transition(
        self,
        record: HandoffRecord,
        *,
        old_status: HandoffStatus,
        new_status: HandoffStatus,
        operation: str,
    ) -> list[dict[str, str]]:
        try:
            await self._log_status_change_event(record, old_status, new_status)
            recent_messages = await self._recent_messages(
                record.tenant_id,
                record.session_id,
            )
            await self._deliver_status_webhook(
                record,
                new_status,
                recent_messages,
            )
        except HandoffPostCommitError:
            HANDOFF_OPERATIONS.labels(operation="update", status="error").inc()
            raise
        HANDOFF_OPERATIONS.labels(operation="update", status=new_status.value).inc()
        logger.info(
            "Handoff updated",
            handoff_id=str(record.id),
            old_status=old_status.value,
            new_status=new_status.value,
            operation=operation,
        )
        return recent_messages

    async def _log_status_change_event(
        self,
        record: HandoffRecord,
        old_status: HandoffStatus,
        new_status: HandoffStatus,
    ) -> None:
        event_type_map = {
            HandoffStatus.ACCEPTED: EventType.HANDOFF_ACCEPT,
            HandoffStatus.COMPLETED: EventType.HANDOFF_COMPLETE,
            HandoffStatus.TIMEOUT: EventType.HANDOFF_TIMEOUT,
        }
        event_type = event_type_map.get(new_status)
        if event_type is None:
            return
        try:
            event_store = await self._resolved_event_store()
            await event_store.append(
                EventData(
                    tenant_id=record.tenant_id,
                    session_id=record.session_id,
                    event_type=event_type.value,
                    event_data={
                        "handoff_id": str(record.id),
                        "old_status": old_status.value,
                        "new_status": new_status.value,
                        "assigned_to": record.assigned_to,
                    },
                    event_metadata={},
                )
            )
        except Exception as exc:
            raise HandoffPostCommitError(
                "Handoff transition committed but Event Store append failed"
            ) from exc

    async def _deliver_status_webhook(
        self,
        record: HandoffRecord,
        status: HandoffStatus,
        recent_messages: list[dict[str, str]],
        *,
        event_type: str | None = None,
    ) -> None:
        if not self.webhook_dispatcher.is_configured(record.tenant_id):
            return
        context = _record_context(record)
        payload = self.webhook_dispatcher.create_payload(
            handoff_id=record.id,
            tenant_id=record.tenant_id,
            session_id=record.session_id,
            status=status,
            urgency=_enum_or_default(
                HandoffUrgency,
                record.urgency,
                HandoffUrgency.NORMAL,
            ),
            reason=_enum_or_default(
                HandoffReason,
                record.reason,
                HandoffReason.USER_REQUEST,
            ),
            summary=record.summary or "",
            key_info=_record_key_info(context),
            verification_status=(
                str(context["verification_status"])
                if context.get("verification_status") is not None
                else None
            ),
            turn_count=int(context.get("turn_count") or 0),
            funnel_stage=str(context.get("funnel_stage") or "unknown"),
            recent_messages=recent_messages,
            sector=_optional_enum(HandoffSector, context.get("sector")),
            sla=_optional_enum(HandoffSLA, context.get("sla")),
        )
        if event_type is not None:
            payload.event_type = event_type
        try:
            delivery = await self.webhook_dispatcher.send(record.tenant_id, payload)
            await self._persist_delivery(record, delivery=delivery)
        except WebhookError as exc:
            logger.warning(
                "handoff_status_webhook_delivery_failed",
                handoff_id=str(record.id),
                error_type=type(exc).__name__,
            )
            await self._persist_delivery(record, error=exc)

    async def get_queue_sla(
        self,
        tenant_id: UUID,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
        as_of: datetime | None = None,
    ) -> HandoffQueueSlaResult:
        """Return queue items and radar statistics from one tenant cohort.

        The clock is captured once, clinic-local civil dates are resolved once,
        and the repository executes one tenant/time predicate. The waiting list
        is then a subset of that in-memory cohort while all six SLA categories
        remain exhaustive over the full population.
        """

        resolved_as_of = self._queue_as_of(as_of)

        async with self._session_factory() as session:
            resolved_period = await self._resolve_queue_period(
                session,
                tenant_id=tenant_id,
                from_date=from_date,
                to_date=to_date,
                as_of=resolved_as_of,
            )
            cohort = HandoffRecordRepository(session).stream_sla_cohort(
                tenant_id=tenant_id,
                start_utc=resolved_period.period.start_utc,
                end_exclusive_utc=resolved_period.period.end_exclusive_utc,
            )
            period = resolved_period.period
            counts = {category: 0 for category in SlaCategory}
            waiting_items: list[HandoffSlaCohortRow] = []
            confirmed_elapsed_total = 0.0
            confirmed_count = 0
            eligible_count = 0
            async for record in cohort:
                classification = classify_sla(
                    SlaOccurrence(
                        created_at=_aware(record.created_at) or period.as_of,
                        confirmed_at=_aware(record.data_hora_saida),
                        sla_target_seconds=record.sla_target_seconds,
                        status=cast(SlaHandoffStatus, record.status),
                    ),
                    as_of=period.as_of,
                )
                counts[classification.category] += 1
                if classification.category in {
                    SlaCategory.WITHIN_SLA,
                    SlaCategory.CONFIRMED_LATE,
                    SlaCategory.UNANSWERED_OVERDUE,
                }:
                    eligible_count += 1
                if classification.elapsed_seconds is not None:
                    confirmed_elapsed_total += classification.elapsed_seconds
                    confirmed_count += 1
                if record.status in {
                    HandoffStatus.PENDING.value,
                    HandoffStatus.ACCEPTED.value,
                }:
                    waiting_items.append(record)

        average = confirmed_elapsed_total / confirmed_count if confirmed_count else None
        percentage = (
            (
                Decimal(counts[SlaCategory.WITHIN_SLA])
                * Decimal(100)
                / Decimal(eligible_count)
            ).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
            if eligible_count
            else None
        )
        return HandoffQueueSlaResult(
            items=tuple(waiting_items),
            metrics=SlaMetrics(
                confirmed_count=confirmed_count,
                sla_eligible_count=eligible_count,
                average_confirmed_seconds=average,
                pct_within_sla=percentage,
            ),
            within_sla_count=counts[SlaCategory.WITHIN_SLA],
            confirmed_late_count=counts[SlaCategory.CONFIRMED_LATE],
            unanswered_overdue_count=counts[SlaCategory.UNANSWERED_OVERDUE],
            maturing_count=counts[SlaCategory.MATURING],
            cancelled_unconfirmed_count=counts[SlaCategory.CANCELLED_UNCONFIRMED],
            unmeasured_legacy_count=counts[SlaCategory.UNMEASURED_LEGACY],
            period_timezone=resolved_period.timezone,
            period=period,
        )

    async def resolve_queue_period(
        self,
        tenant_id: UUID,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
        as_of: datetime | None = None,
    ) -> ResolvedHandoffQueuePeriod:
        """Resolve the queue/export entry cohort from clinic-owned timezone data."""

        resolved_as_of = self._queue_as_of(as_of)
        async with self._session_factory() as session:
            return await self._resolve_queue_period(
                session,
                tenant_id=tenant_id,
                from_date=from_date,
                to_date=to_date,
                as_of=resolved_as_of,
            )

    @staticmethod
    def _queue_as_of(as_of: datetime | None) -> datetime:
        resolved_as_of = as_of or datetime.now(timezone.utc)
        if resolved_as_of.tzinfo is None or resolved_as_of.utcoffset() is None:
            raise ValueError("as_of must be timezone-aware")
        return resolved_as_of.astimezone(timezone.utc)

    @staticmethod
    async def _resolve_queue_period(
        session: AsyncSession,
        *,
        tenant_id: UUID,
        from_date: date | None,
        to_date: date | None,
        as_of: datetime,
    ) -> ResolvedHandoffQueuePeriod:
        raw_operations = await session.scalar(
            select(ClinicConfigTable.operations).where(
                ClinicConfigTable.tenant_id == tenant_id
            )
        )
        configured_timezone = None
        if isinstance(raw_operations, dict):
            candidate = raw_operations.get("timezone")
            if isinstance(candidate, str):
                configured_timezone = candidate
        clinic_timezone = resolve_clinic_tz(configured_timezone)
        local_today = as_of.astimezone(clinic_timezone).date()
        effective_from = from_date or local_today - timedelta(days=30)
        effective_to = to_date or local_today
        if (effective_to - effective_from).days > 90:
            raise HandoffSlaPeriodError("max_window_90_days")
        try:
            period = resolve_sla_period(
                effective_from,
                effective_to,
                clinic_timezone.key,
                as_of=as_of,
            )
        except (OverflowError, TypeError, ValueError) as exc:
            raise HandoffSlaPeriodError("invalid_date_range") from exc
        return ResolvedHandoffQueuePeriod(
            from_date=effective_from,
            to_date=effective_to,
            timezone=clinic_timezone.key,
            period=period,
        )

    async def get_handoff(
        self,
        handoff_id: UUID,
        *,
        tenant_id: UUID,
    ) -> HandoffDetail:
        async with self._session_factory() as session:
            record = await HandoffRecordRepository(session).get_for_tenant(
                tenant_id,
                handoff_id,
            )
            if record is None:
                raise HandoffNotFoundError(f"Handoff {handoff_id} not found")
        messages = await self._recent_messages(record.tenant_id, record.session_id)
        return handoff_record_to_detail(record, messages)

    async def list_handoffs(
        self,
        tenant_id: UUID,
        status: Optional[HandoffStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[HandoffListItem]:
        async with self._session_factory() as session:
            records = await HandoffRecordRepository(session).find_by_tenant(
                tenant_id,
                status=status.value if status is not None else None,
                skip=offset,
                limit=limit,
            )
            return [handoff_record_to_list_item(record) for record in records]

    async def get_pending_handoffs(
        self,
        tenant_id: UUID,
    ) -> list[HandoffListItem]:
        return await self.list_handoffs(
            tenant_id=tenant_id,
            status=HandoffStatus.PENDING,
        )

    async def get_active_handoff_for_session(
        self,
        session_id: UUID,
        *,
        tenant_id: UUID,
    ) -> Optional[HandoffDetail]:
        async with self._session_factory() as session:
            record = await HandoffRecordRepository(session).find_active_for_session(
                tenant_id,
                session_id,
            )
            if record is None:
                return None
        messages = await self._recent_messages(record.tenant_id, record.session_id)
        return handoff_record_to_detail(record, messages)

    def configure_webhook(self, tenant_id: UUID, config: WebhookConfig) -> None:
        self.webhook_dispatcher.set_config(tenant_id, config)
        logger.info(
            "Webhook configured",
            tenant_id=str(tenant_id),
            url=config.url,
            enabled=config.enabled,
        )

    def get_webhook_config(self, tenant_id: UUID) -> Optional[WebhookConfig]:
        return self.webhook_dispatcher.get_config(tenant_id)

    def remove_webhook_config(self, tenant_id: UUID) -> bool:
        return self.webhook_dispatcher.remove_config(tenant_id)


_service: Optional[HandoffService] = None


def get_handoff_service(event_store: Optional[EventStore] = None) -> HandoffService:
    """Get the stateless singleton; it stores factories, never sessions."""

    global _service
    if _service is None:
        _service = HandoffService(event_store=event_store)
    elif event_store is not None and _service.event_store is None:
        _service.event_store = event_store
    return _service


async def close_handoff_service() -> None:
    global _service
    service = _service
    _service = None
    if service is not None and service.webhook_dispatcher:
        await service.webhook_dispatcher.close()
