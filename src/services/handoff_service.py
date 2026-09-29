"""Stateless orchestration for the canonical PostgreSQL handoff lifecycle."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Optional, TypeVar, cast, overload
from uuid import UUID, uuid4

from prometheus_client import Counter, Histogram
from sqlalchemy.ext.asyncio import AsyncSession

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
from src.handoff.sla import (
    MAX_SLA_RANGE_ELAPSED_DAYS,
    CivilDateRange,
    SlaCategory,
    classify_handoff_sla,
    resolve_clinic_civil_date_range,
    resolve_sla_target_seconds,
    round_half_up_percentage,
)
from src.handoff.summarizer import ConversationSummarizer, get_summarizer
from src.handoff.webhook import WebhookDispatcher, get_webhook_dispatcher
from src.memory import event_store as event_store_module
from src.memory.event_store import EventData, EventStore, EventType, HandoffEventData
from src.models.handoff import HandoffRecord
from src.repositories.database import get_db_engine, get_db_session
from src.repositories.handoff import (
    HandoffCasOutcome,
    HandoffCasResult,
    HandoffLookupOutcome,
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
RepositoryFactory = Callable[[AsyncSession], HandoffRecordRepository]
_DEFAULT_EVENT_STORE = object()
_EnumT = TypeVar("_EnumT", bound=Enum)
_INTERNAL_CONTEXT_KEYS = frozenset(
    {
        "context_version",
        "funnel_stage",
        "safety_concern",
        "sector",
        "sla",
        "summary_snapshot",
        "turn_count",
        "verification_status",
        "webhook_status",
    }
)


class HandoffPersistenceError(HandoffServiceError):
    """The canonical PostgreSQL mutation could not be committed."""


class HandoffConflictError(HandoffServiceError):
    """The observed status/version lost a compare-and-set race."""


class HandoffTransitionError(HandoffServiceError):
    """The requested transition is invalid for the observed lifecycle state."""


class HandoffPostCommitError(HandoffServiceError):
    """A required public effect failed after PostgreSQL committed."""


class HandoffSlaPeriodError(HandoffServiceError):
    """The requested civil-date window cannot form an SLA cohort."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class HandoffSlaDataError(HandoffServiceError):
    """Persisted SLA occurrence facts violate the domain's data contract."""


@dataclass(frozen=True)
class HandoffSlaMetrics:
    """Aggregate SLA facts derived from classified persisted snapshots."""

    confirmed_count: int
    sla_eligible_count: int
    average_confirmed_seconds: float | None
    pct_within_sla: Decimal | None


@dataclass(frozen=True)
class HandoffQueueSlaResult:
    """One queue view and radar derived from the same persisted cohort."""

    items: tuple[HandoffSlaCohortRow, ...]
    metrics: HandoffSlaMetrics
    within_sla_count: int
    confirmed_late_count: int
    unanswered_overdue_count: int
    maturing_count: int
    cancelled_unconfirmed_count: int
    unmeasured_legacy_count: int
    period: CivilDateRange


@dataclass(frozen=True)
class HandoffQueueConfirmation:
    """Committed queue projection returned without a post-commit requery."""

    id: UUID
    numero_whatsapp: str | None
    session_id: UUID
    safety_concern: bool | None
    reason: str
    intencao_detectada: str | None
    estagio_atual: str | None
    primeira_linha_mensagem: str | None
    created_at: datetime
    data_hora_saida: datetime | None
    sla_target_seconds: int | None
    status: str


@dataclass(frozen=True)
class _HandoffTransitionResult:
    """Canonical lifecycle result retained for specialized projections."""

    detail: HandoffDetail
    record: HandoffRecord


@dataclass(frozen=True)
class RecentMessageProjection:
    """Tenant-scoped, ephemeral Event Store projection for a public DTO."""

    messages: tuple[dict[str, Any], ...] = ()


class _Mutation(str, Enum):
    ACCEPT = "accept"
    CONFIRM = "confirm"
    COMPLETE = "complete"
    CANCEL = "cancel"
    TIMEOUT = "timeout"


def _enum_or_default(
    enum_type: type[_EnumT],
    value: object,
    default: _EnumT,
) -> _EnumT:
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return default


def _optional_enum(enum_type: type[Enum], value: object) -> Any:
    if value is None:
        return None
    try:
        return enum_type(value)
    except (TypeError, ValueError):
        return None


def _context_dict(record: HandoffRecord) -> dict[str, Any]:
    return dict(record.context) if isinstance(record.context, dict) else {}


def _record_safety_concern(record: HandoffRecord) -> bool | None:
    value = _context_dict(record).get("safety_concern")
    return value if type(value) is bool else None


@overload
def _aware_utc(value: datetime) -> datetime: ...


@overload
def _aware_utc(value: None) -> None: ...


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _webhook_status(record: HandoffRecord) -> WebhookStatus | None:
    response = record.webhook_response
    if not isinstance(response, dict):
        return None
    try:
        return WebhookStatus(response.get("status"))
    except (TypeError, ValueError):
        return None


def map_handoff_record(
    record: HandoffRecord,
    recent_messages: RecentMessageProjection,
) -> HandoffDetail:
    """Map canonical persistence plus ephemeral messages to the public DTO.

    ``lifecycle_version`` is intentionally absent. Recent messages are supplied
    only by the Event Store projection and are never read from JSONB.
    """
    context = _context_dict(record)
    key_info = context.get("key_info")
    if not isinstance(key_info, dict):
        # Pre-canonical rows stored request key_info directly in context.
        key_info = {
            key: value
            for key, value in context.items()
            if key not in _INTERNAL_CONTEXT_KEYS
        }

    sector = _optional_enum(HandoffSector, context.get("sector"))
    sla = _optional_enum(HandoffSLA, context.get("sla"))
    return HandoffDetail(
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
        sector=sector,
        sla=sla,
        verification_status=(
            str(context["verification_status"])
            if context.get("verification_status") is not None
            else None
        ),
        summary=record.summary or "",
        key_info=key_info,
        recent_messages=list(recent_messages.messages),
        assigned_to=record.assigned_to,
        webhook_sent=record.webhook_sent,
        webhook_status=_webhook_status(record),
        webhook_response=(
            dict(record.webhook_response)
            if isinstance(record.webhook_response, dict)
            else None
        ),
        created_at=_aware_utc(record.created_at),
        updated_at=_aware_utc(record.updated_at),
        data_hora_saida=_aware_utc(record.data_hora_saida),
        resolved_at=_aware_utc(record.resolved_at),
        sla_target_seconds=record.sla_target_seconds,
        resolution_notes=record.resolution_notes,
    )


def _map_list_item(record: HandoffRecord) -> HandoffListItem:
    context = _context_dict(record)
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
        created_at=_aware_utc(record.created_at),
        assigned_to=record.assigned_to,
    )


class HandoffService:
    """Stateless handoff service; only immutable dependencies are retained."""

    def __init__(
        self,
        event_store: EventStore | None | object = _DEFAULT_EVENT_STORE,
        summarizer: Optional[ConversationSummarizer] = None,
        webhook_dispatcher: Optional[WebhookDispatcher] = None,
        config: Optional[HandoffServiceConfig] = None,
        session_factory: Optional[SessionFactory] = None,
        repository_factory: Optional[RepositoryFactory] = None,
    ) -> None:
        self._resolve_default_event_store = event_store is _DEFAULT_EVENT_STORE
        self.event_store = (
            None
            if self._resolve_default_event_store
            else cast(Optional[EventStore], event_store)
        )
        self.summarizer = summarizer or get_summarizer()
        self.webhook_dispatcher = webhook_dispatcher or get_webhook_dispatcher()
        self.config = config or HandoffServiceConfig()
        # Keep the default provider lazy. Tests and application startup can replace
        # the module-level provider without contaminating a pre-existing singleton.
        self._session_factory = session_factory
        self._repository_factory = repository_factory or HandoffRecordRepository

    def _open_session(self) -> AsyncSession:
        return (self._session_factory or get_db_session)()

    async def _event_store_for_operation(self) -> EventStore | None:
        if self._resolve_default_event_store:
            try:
                application_engine = get_db_engine()
            except RuntimeError:
                application_engine = None
            if (
                application_engine is not None
                and application_engine.dialect.name != "postgresql"
            ):
                # EventStore is PostgreSQL-only. The application database uses
                # SQLite solely in the in-process test runtime, where there is
                # no truthful production Event Store to resolve.
                return None
            return await event_store_module.get_event_store()
        return self.event_store

    async def initiate_handoff(self, request: HandoffRequest) -> HandoffResponse:
        """Commit the PostgreSQL source of truth before any public effects."""
        started_at = time.perf_counter()
        handoff_id = uuid4()
        summary = self._generate_summary(request)
        logger.info(
            "Initiating handoff",
            handoff_id=str(handoff_id),
            tenant_id=str(request.tenant_id),
            session_id=str(request.session_id),
            reason=request.reason.value,
            urgency=request.urgency.value,
        )

        try:
            async with self._open_session() as session:
                repository = self._repository_factory(session)
                if not await repository.session_belongs_to_tenant(
                    request.tenant_id,
                    request.session_id,
                ):
                    raise HandoffServiceError("handoff_session_not_found")
                sla_configuration = await repository.load_sla_configuration(
                    request.tenant_id
                )
                target_seconds = resolve_sla_target_seconds(
                    sla_configuration.configured_minutes
                )
                if not self._is_valid_sla_minutes(sla_configuration.configured_minutes):
                    logger.warning(
                        "handoff_sla_configuration_fallback",
                        tenant_id=str(request.tenant_id),
                        source=sla_configuration.source,
                        fallback_seconds=target_seconds,
                    )

                record = self._build_record(
                    handoff_id=handoff_id,
                    request=request,
                    summary=summary,
                    target_seconds=target_seconds,
                )
                try:
                    record = await repository.create_record(record)
                    await session.commit()
                except Exception as exc:
                    await session.rollback()
                    raise HandoffPersistenceError(
                        "Failed to persist handoff in PostgreSQL"
                    ) from exc

            await self._emit_handoff_request(record, summary)
            webhook_sent, webhook_status = await self._dispatch_creation_webhook(
                record,
                request,
                summary,
            )

            elapsed = time.perf_counter() - started_at
            HANDOFF_LATENCY.labels(operation="initiate").observe(elapsed)
            HANDOFF_OPERATIONS.labels(operation="initiate", status="success").inc()
            HANDOFF_BY_REASON.labels(
                reason=request.reason.value,
                urgency=request.urgency.value,
            ).inc()
            logger.info(
                "Handoff initiated successfully",
                handoff_id=str(handoff_id),
                latency_ms=round(elapsed * 1000, 2),
                webhook_sent=webhook_sent,
            )
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
                created_at=_aware_utc(record.created_at),
            )
        except HandoffServiceError:
            HANDOFF_OPERATIONS.labels(operation="initiate", status="error").inc()
            raise
        except Exception as exc:
            HANDOFF_OPERATIONS.labels(operation="initiate", status="error").inc()
            raise HandoffServiceError(f"Failed to initiate handoff: {exc}") from exc

    def _build_record(
        self,
        *,
        handoff_id: UUID,
        request: HandoffRequest,
        summary: ConversationSummary,
        target_seconds: int,
    ) -> HandoffRecord:
        first_message: str | None = None
        for message in request.recent_messages:
            if message.get("role", "user") == "user" and message.get("content"):
                first_message = str(message["content"])[:500]
                break

        whatsapp_number: str | None = None
        if request.key_info:
            raw_number = (
                request.key_info.get("whatsapp_number")
                or request.key_info.get("phone")
                or request.key_info.get("numero_whatsapp")
            )
            if raw_number is not None:
                whatsapp_number = str(raw_number)

        context: dict[str, Any] = {
            "context_version": 1,
            "key_info": summary.key_info.model_dump(mode="json"),
            "funnel_stage": summary.funnel_stage,
            "turn_count": summary.turn_count,
            "summary_snapshot": {
                "duration_seconds": summary.duration_seconds,
                "generated_at": summary.generated_at.isoformat(),
                "intents_detected": summary.intents_detected,
                "safety_flags": summary.safety_flags,
                "total_messages": summary.total_messages,
            },
        }
        if request.sector is not None:
            context["sector"] = request.sector.value
        if request.sla is not None:
            context["sla"] = request.sla.value
        if request.verification_status is not None:
            context["verification_status"] = request.verification_status
        if request.safety_concern is not None:
            context["safety_concern"] = request.safety_concern

        return HandoffRecord(
            id=handoff_id,
            tenant_id=request.tenant_id,
            session_id=request.session_id,
            reason=request.reason.value,
            summary=summary.summary_text,
            context=context,
            urgency=request.urgency.value,
            status=HandoffStatus.PENDING.value,
            webhook_sent=False,
            numero_whatsapp=whatsapp_number,
            intencao_detectada=request.detected_intent,
            estagio_atual=(str(request.funnel_stage) if request.funnel_stage else None),
            primeira_linha_mensagem=first_message,
            sla_target_seconds=target_seconds,
            lifecycle_version=0,
        )

    @staticmethod
    def _is_valid_sla_minutes(value: object) -> bool:
        return (
            isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 120
        )

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

    async def _emit_handoff_request(
        self,
        record: HandoffRecord,
        summary: ConversationSummary,
    ) -> None:
        try:
            event_store = await self._event_store_for_operation()
            if event_store is None:
                return
            event_data = HandoffEventData(
                reason=record.reason,
                summary=summary.summary_text,
                urgency=record.urgency,
                key_info={"handoff_id": str(record.id)},
            )
            event = EventData(
                tenant_id=record.tenant_id,
                session_id=record.session_id,
                event_type=EventType.HANDOFF_REQUEST.value,
                event_data=event_data.model_dump(),
                event_metadata={"handoff_id": str(record.id)},
            )
            await event_store.append(event)
        except Exception as exc:
            raise HandoffPostCommitError(
                "Handoff committed but Event Store append failed"
            ) from exc

    async def _dispatch_creation_webhook(
        self,
        record: HandoffRecord,
        request: HandoffRequest,
        summary: ConversationSummary,
    ) -> tuple[bool, WebhookStatus | None]:
        if not self.webhook_dispatcher.is_configured(record.tenant_id):
            return False, None
        payload = self.webhook_dispatcher.create_payload(
            handoff_id=record.id,
            tenant_id=record.tenant_id,
            session_id=record.session_id,
            status=HandoffStatus.PENDING,
            urgency=request.urgency,
            reason=request.reason,
            summary=summary.summary_text,
            key_info=summary.key_info.model_dump(),
            verification_status=summary.verification_status,
            turn_count=summary.turn_count,
            funnel_stage=summary.funnel_stage,
            recent_messages=[
                {"role": message.role, "content": message.content}
                for message in summary.recent_messages
            ],
            sector=request.sector,
            sla=request.sla,
        )
        delivery = await self._deliver_webhook(record, payload)
        return delivery.status == WebhookStatus.SENT, delivery.status

    async def enrich_scheduling_verification(
        self,
        session_id: UUID,
        verification_status: str,
        handoff_id: Optional[UUID] = None,
        *,
        tenant_id: UUID,
    ) -> Optional[UUID]:
        """CAS the allowed scheduling enrichment and emit effects after commit."""
        async with self._open_session() as session:
            repository = self._repository_factory(session)
            if handoff_id is not None:
                lookup = await repository.lookup(tenant_id, handoff_id)
            else:
                lookup = await repository.find_active_for_session(
                    tenant_id,
                    session_id,
                )
            record = lookup.record
            if (
                lookup.outcome is HandoffLookupOutcome.NOT_FOUND
                or record is None
                or record.session_id != session_id
                or record.status
                not in {
                    HandoffStatus.PENDING.value,
                    HandoffStatus.ACCEPTED.value,
                    HandoffStatus.IN_PROGRESS.value,
                }
            ):
                logger.warning(
                    "scheduling_link_enrich_no_handoff",
                    tenant_id=str(tenant_id),
                    session_id=str(session_id),
                    verification_status=verification_status,
                )
                return None

            context = _context_dict(record)
            context["verification_status"] = verification_status
            changed_at = await repository.current_timestamp()
            result = await repository.update_verification(
                tenant_id=record.tenant_id,
                handoff_id=record.id,
                observed_status=record.status,
                observed_lifecycle_version=record.lifecycle_version,
                context=context,
                changed_at=changed_at,
            )
            if result.outcome is HandoffCasOutcome.CONFLICT or result.record is None:
                await session.rollback()
                HANDOFF_OPERATIONS.labels(operation="update", status="conflict").inc()
                raise HandoffConflictError("Handoff changed during verification update")
            try:
                await session.commit()
            except Exception as exc:
                await session.rollback()
                HANDOFF_OPERATIONS.labels(operation="update", status="error").inc()
                raise HandoffPersistenceError(
                    "Failed to persist handoff verification"
                ) from exc
            record = result.record

        projection = await self._project_recent_messages(record)
        try:
            await self._dispatch_status_webhook(
                record,
                projection,
                event_type="handoff.updated",
            )
        except HandoffPostCommitError:
            HANDOFF_OPERATIONS.labels(operation="update", status="error").inc()
            raise
        HANDOFF_OPERATIONS.labels(operation="update", status=record.status).inc()
        return record.id

    async def update_handoff(
        self,
        update: HandoffUpdate,
        *,
        tenant_id: UUID,
    ) -> HandoffDetail:
        """Compatibility dispatcher for the single canonical lifecycle."""
        if update.status is HandoffStatus.ACCEPTED:
            return await self.accept_handoff(
                update.handoff_id,
                update.assigned_to,
                tenant_id=tenant_id,
            )
        if update.status is HandoffStatus.COMPLETED:
            return await self.complete_handoff(
                update.handoff_id,
                update.notes,
                tenant_id=tenant_id,
            )
        if update.status is HandoffStatus.CANCELLED:
            return await self.cancel_handoff(
                update.handoff_id,
                update.notes,
                tenant_id=tenant_id,
            )
        if update.status is HandoffStatus.TIMEOUT:
            return await self.timeout_handoff(update.handoff_id, tenant_id=tenant_id)

        HANDOFF_OPERATIONS.labels(operation="update", status="rejected").inc()
        if update.status is HandoffStatus.IN_PROGRESS:
            raise HandoffTransitionError(
                "Generic update cannot confirm a handoff; use confirm_handoff"
            )
        raise HandoffTransitionError("Generic update cannot reset a handoff to pending")

    async def accept_handoff(
        self,
        handoff_id: UUID,
        agent_id: Optional[str] = None,
        *,
        tenant_id: UUID,
    ) -> HandoffDetail:
        return await self._transition(
            mutation=_Mutation.ACCEPT,
            handoff_id=handoff_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )

    async def confirm_handoff(
        self,
        handoff_id: UUID,
        agent_id: Optional[str] = None,
        *,
        tenant_id: UUID,
    ) -> HandoffDetail:
        return await self._transition(
            mutation=_Mutation.CONFIRM,
            handoff_id=handoff_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )

    async def confirm_queue_handoff(
        self,
        handoff_id: UUID,
        agent_id: Optional[str] = None,
        *,
        tenant_id: UUID,
    ) -> HandoffQueueConfirmation:
        """Confirm and return the committed queue projection from the same result."""
        result = await self._transition_result(
            mutation=_Mutation.CONFIRM,
            handoff_id=handoff_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
        )
        record = result.record
        return HandoffQueueConfirmation(
            id=record.id,
            numero_whatsapp=record.numero_whatsapp,
            session_id=record.session_id,
            safety_concern=_record_safety_concern(record),
            reason=record.reason,
            intencao_detectada=record.intencao_detectada,
            estagio_atual=record.estagio_atual,
            primeira_linha_mensagem=record.primeira_linha_mensagem,
            created_at=_aware_utc(record.created_at),
            data_hora_saida=_aware_utc(record.data_hora_saida),
            sla_target_seconds=record.sla_target_seconds,
            status=record.status,
        )

    async def complete_handoff(
        self,
        handoff_id: UUID,
        notes: Optional[str] = None,
        *,
        tenant_id: UUID,
    ) -> HandoffDetail:
        return await self._transition(
            mutation=_Mutation.COMPLETE,
            handoff_id=handoff_id,
            tenant_id=tenant_id,
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
            mutation=_Mutation.CANCEL,
            handoff_id=handoff_id,
            tenant_id=tenant_id,
            notes=notes,
        )

    async def timeout_handoff(
        self,
        handoff_id: UUID,
        *,
        tenant_id: UUID,
    ) -> HandoffDetail:
        return await self._transition(
            mutation=_Mutation.TIMEOUT,
            handoff_id=handoff_id,
            tenant_id=tenant_id,
        )

    async def get_queue_sla(
        self,
        tenant_id: UUID,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
        as_of: datetime | None = None,
    ) -> HandoffQueueSlaResult:
        """Build queue items and the SLA radar from one tenant/time cohort."""
        resolved_as_of = as_of or datetime.now(timezone.utc)
        async with self._open_session() as session:
            repository = self._repository_factory(session)
            configured_timezone = await repository.load_clinic_timezone(tenant_id)
            period = self._resolve_queue_period(
                configured_timezone=configured_timezone,
                as_of=resolved_as_of,
                from_date=from_date,
                to_date=to_date,
            )

            counts = {category: 0 for category in SlaCategory}
            waiting_items: list[HandoffSlaCohortRow] = []
            confirmed_count = 0
            confirmed_wait_total = 0.0
            waiting_statuses = {
                HandoffStatus.PENDING.value,
                HandoffStatus.ACCEPTED.value,
            }
            async for record in repository.stream_sla_cohort(
                tenant_id=tenant_id,
                start_utc=period.start_utc,
                end_exclusive_utc=period.end_exclusive_utc,
            ):
                created_at = self._persisted_utc(record.created_at)
                confirmed_at = (
                    self._persisted_utc(record.data_hora_saida)
                    if record.data_hora_saida is not None
                    else None
                )
                try:
                    category = classify_handoff_sla(
                        created_at=created_at,
                        confirmed_at=confirmed_at,
                        target_seconds=record.sla_target_seconds,
                        status=record.status,
                        as_of=period.as_of,
                    )
                except (OverflowError, TypeError, ValueError) as exc:
                    logger.error(
                        "handoff_sla_invalid_persisted_occurrence",
                        tenant_id=str(tenant_id),
                        handoff_id=str(record.id),
                        error_type=type(exc).__name__,
                    )
                    raise HandoffSlaDataError("invalid_handoff_timestamps") from exc
                counts[category] += 1
                if record.status in waiting_statuses:
                    waiting_items.append(record)
                if category in {
                    SlaCategory.WITHIN_SLA,
                    SlaCategory.CONFIRMED_LATE,
                }:
                    assert confirmed_at is not None
                    confirmed_count += 1
                    confirmed_wait_total += (confirmed_at - created_at).total_seconds()

        eligible_count = (
            counts[SlaCategory.WITHIN_SLA]
            + counts[SlaCategory.CONFIRMED_LATE]
            + counts[SlaCategory.UNANSWERED_OVERDUE]
        )
        return HandoffQueueSlaResult(
            items=tuple(waiting_items),
            metrics=HandoffSlaMetrics(
                confirmed_count=confirmed_count,
                sla_eligible_count=eligible_count,
                average_confirmed_seconds=(
                    confirmed_wait_total / confirmed_count if confirmed_count else None
                ),
                pct_within_sla=round_half_up_percentage(
                    counts[SlaCategory.WITHIN_SLA],
                    eligible_count,
                ),
            ),
            within_sla_count=counts[SlaCategory.WITHIN_SLA],
            confirmed_late_count=counts[SlaCategory.CONFIRMED_LATE],
            unanswered_overdue_count=counts[SlaCategory.UNANSWERED_OVERDUE],
            maturing_count=counts[SlaCategory.MATURING],
            cancelled_unconfirmed_count=counts[SlaCategory.CANCELLED_UNCONFIRMED],
            unmeasured_legacy_count=counts[SlaCategory.UNMEASURED_LEGACY],
            period=period,
        )

    async def resolve_queue_period(
        self,
        tenant_id: UUID,
        *,
        from_date: date | None = None,
        to_date: date | None = None,
        as_of: datetime | None = None,
    ) -> CivilDateRange:
        """Resolve the shared bounded clinic-civil queue/export window."""
        resolved_as_of = as_of or datetime.now(timezone.utc)
        async with self._open_session() as session:
            repository = self._repository_factory(session)
            configured_timezone = await repository.load_clinic_timezone(tenant_id)
        return self._resolve_queue_period(
            configured_timezone=configured_timezone,
            as_of=resolved_as_of,
            from_date=from_date,
            to_date=to_date,
        )

    @staticmethod
    def _resolve_queue_period(
        *,
        configured_timezone: object,
        as_of: datetime,
        from_date: date | None,
        to_date: date | None,
    ) -> CivilDateRange:
        try:
            period = resolve_clinic_civil_date_range(
                clinic_timezone=configured_timezone,
                as_of=as_of,
                start_date=from_date,
                end_date=to_date,
            )
        except (OverflowError, TypeError, ValueError) as exc:
            raise HandoffSlaPeriodError("invalid_date_range") from exc
        if (period.end_date - period.start_date).days > MAX_SLA_RANGE_ELAPSED_DAYS:
            raise HandoffSlaPeriodError("max_window_90_days")
        return period

    @staticmethod
    def _persisted_utc(value: datetime) -> datetime:
        """Normalize SQLite-naive test rows and PostgreSQL-aware rows to UTC."""
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    async def _transition(
        self,
        *,
        mutation: _Mutation,
        handoff_id: UUID,
        tenant_id: UUID,
        agent_id: str | None = None,
        notes: str | None = None,
    ) -> HandoffDetail:
        result = await self._transition_result(
            mutation=mutation,
            handoff_id=handoff_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            notes=notes,
        )
        return result.detail

    async def _transition_result(
        self,
        *,
        mutation: _Mutation,
        handoff_id: UUID,
        tenant_id: UUID,
        agent_id: str | None = None,
        notes: str | None = None,
    ) -> _HandoffTransitionResult:
        async with self._open_session() as session:
            repository = self._repository_factory(session)
            lookup = await repository.lookup(tenant_id, handoff_id)
            record = lookup.record
            if lookup.outcome is HandoffLookupOutcome.NOT_FOUND or record is None:
                HANDOFF_OPERATIONS.labels(operation="update", status="not_found").inc()
                raise HandoffNotFoundError(f"Handoff {handoff_id} not found")

            self._validate_transition(mutation, record)
            changed_at = await repository.current_timestamp()
            old_status = HandoffStatus(record.status)
            result = await self._apply_transition(
                repository=repository,
                mutation=mutation,
                record=record,
                agent_id=agent_id,
                notes=notes,
                changed_at=changed_at,
            )
            if result.outcome is HandoffCasOutcome.CONFLICT or result.record is None:
                await session.rollback()
                HANDOFF_OPERATIONS.labels(operation="update", status="conflict").inc()
                raise HandoffConflictError(
                    "Handoff changed after its lifecycle snapshot was observed"
                )
            try:
                await session.commit()
            except Exception as exc:
                await session.rollback()
                HANDOFF_OPERATIONS.labels(operation="update", status="error").inc()
                raise HandoffPersistenceError(
                    "Failed to commit handoff lifecycle transition"
                ) from exc
            record = result.record

        new_status = HandoffStatus(record.status)
        try:
            await self._emit_status_change(record, old_status, new_status)
        except HandoffPostCommitError:
            HANDOFF_OPERATIONS.labels(operation="update", status="error").inc()
            raise
        projection = await self._project_recent_messages(record)
        try:
            await self._dispatch_status_webhook(record, projection)
        except HandoffPostCommitError:
            HANDOFF_OPERATIONS.labels(operation="update", status="error").inc()
            raise
        HANDOFF_OPERATIONS.labels(
            operation="update",
            status=new_status.value,
        ).inc()
        return _HandoffTransitionResult(
            detail=map_handoff_record(record, projection),
            record=record,
        )

    @staticmethod
    def _validate_transition(mutation: _Mutation, record: HandoffRecord) -> None:
        status = HandoffStatus(record.status)
        active = {
            HandoffStatus.PENDING,
            HandoffStatus.ACCEPTED,
            HandoffStatus.IN_PROGRESS,
        }
        allowed = False
        if mutation is _Mutation.ACCEPT:
            allowed = status is HandoffStatus.PENDING
        elif mutation is _Mutation.CONFIRM:
            # The SQL ``IS NULL`` precondition owns write-once semantics. This
            # deliberately lets a repeated confirmation reach CAS and return a
            # typed conflict, and supports legacy in_progress rows with NULL.
            allowed = status in {
                HandoffStatus.PENDING,
                HandoffStatus.ACCEPTED,
                HandoffStatus.IN_PROGRESS,
            }
        elif mutation is _Mutation.COMPLETE:
            allowed = (
                status is HandoffStatus.IN_PROGRESS
                and record.data_hora_saida is not None
            )
        elif mutation in {_Mutation.CANCEL, _Mutation.TIMEOUT}:
            allowed = status in active
        if not allowed:
            HANDOFF_OPERATIONS.labels(operation="update", status="rejected").inc()
            raise HandoffTransitionError(
                f"Cannot {mutation.value} handoff from status {status.value}"
            )

    @staticmethod
    async def _apply_transition(
        *,
        repository: HandoffRecordRepository,
        mutation: _Mutation,
        record: HandoffRecord,
        agent_id: str | None,
        notes: str | None,
        changed_at: datetime,
    ) -> HandoffCasResult:
        if mutation is _Mutation.ACCEPT:
            return await repository.accept_handoff(
                tenant_id=record.tenant_id,
                handoff_id=record.id,
                observed_status=record.status,
                observed_lifecycle_version=record.lifecycle_version,
                agent_id=agent_id,
                changed_at=changed_at,
            )
        if mutation is _Mutation.CONFIRM:
            return await repository.confirm_handoff(
                tenant_id=record.tenant_id,
                handoff_id=record.id,
                observed_status=record.status,
                observed_lifecycle_version=record.lifecycle_version,
                agent_id=agent_id,
                confirmed_at=changed_at,
            )
        if mutation is _Mutation.COMPLETE:
            return await repository.complete_handoff(
                tenant_id=record.tenant_id,
                handoff_id=record.id,
                observed_status=record.status,
                observed_lifecycle_version=record.lifecycle_version,
                notes=notes,
                changed_at=changed_at,
            )
        if mutation is _Mutation.CANCEL:
            return await repository.cancel_handoff(
                tenant_id=record.tenant_id,
                handoff_id=record.id,
                observed_status=record.status,
                observed_lifecycle_version=record.lifecycle_version,
                notes=notes,
                changed_at=changed_at,
            )
        return await repository.timeout_handoff(
            tenant_id=record.tenant_id,
            handoff_id=record.id,
            observed_status=record.status,
            observed_lifecycle_version=record.lifecycle_version,
            changed_at=changed_at,
        )

    async def _emit_status_change(
        self,
        record: HandoffRecord,
        old_status: HandoffStatus,
        new_status: HandoffStatus,
    ) -> None:
        event_type = {
            HandoffStatus.ACCEPTED: EventType.HANDOFF_ACCEPT,
            HandoffStatus.COMPLETED: EventType.HANDOFF_COMPLETE,
            HandoffStatus.TIMEOUT: EventType.HANDOFF_TIMEOUT,
        }.get(new_status)
        if event_type is None:
            return
        event = EventData(
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
        try:
            event_store = await self._event_store_for_operation()
            if event_store is None:
                return
            await event_store.append(event)
        except Exception as exc:
            raise HandoffPostCommitError(
                "Handoff transition committed but Event Store append failed"
            ) from exc

    async def _dispatch_status_webhook(
        self,
        record: HandoffRecord,
        projection: RecentMessageProjection,
        *,
        event_type: str | None = None,
    ) -> None:
        if not self.webhook_dispatcher.is_configured(record.tenant_id):
            return
        detail = map_handoff_record(record, projection)
        context = _context_dict(record)
        payload = self.webhook_dispatcher.create_payload(
            handoff_id=record.id,
            tenant_id=record.tenant_id,
            session_id=record.session_id,
            status=detail.status,
            urgency=detail.urgency,
            reason=detail.reason,
            summary=detail.summary,
            key_info=detail.key_info,
            verification_status=detail.verification_status,
            turn_count=int(context.get("turn_count", 0) or 0),
            funnel_stage=str(context.get("funnel_stage", "unknown")),
            recent_messages=[
                {
                    "role": str(message.get("role", "user")),
                    "content": str(message.get("content", "")),
                }
                for message in detail.recent_messages
            ],
            sector=detail.sector,
            sla=detail.sla,
        )
        if event_type is not None:
            payload.event_type = event_type
        await self._deliver_webhook(record, payload)

    async def _deliver_webhook(
        self,
        record: HandoffRecord,
        payload: Any,
    ) -> WebhookDelivery:
        try:
            delivery = await self.webhook_dispatcher.send(record.tenant_id, payload)
        except WebhookError as exc:
            delivery = WebhookDelivery(
                handoff_id=record.id,
                tenant_id=record.tenant_id,
                url="",
                status=WebhookStatus.FAILED,
                error_message=type(exc).__name__,
            )
            logger.warning(
                "handoff_webhook_delivery_failed",
                handoff_id=str(record.id),
                tenant_id=str(record.tenant_id),
                error_type=type(exc).__name__,
            )

        sanitized: dict[str, Any] = {
            "status": delivery.status.value,
            "attempt": delivery.attempt,
        }
        if delivery.response_status is not None:
            sanitized["response_status"] = delivery.response_status
        if delivery.error_message is not None:
            # Dispatcher errors may include response bodies or remote text.
            # Persist only a stable classification, never that content.
            sanitized["error_type"] = "delivery_failed"
        sent = delivery.status is WebhookStatus.SENT
        try:
            async with self._open_session() as session:
                repository = self._repository_factory(session)
                persisted = await repository.record_webhook_delivery(
                    tenant_id=record.tenant_id,
                    handoff_id=record.id,
                    sent=sent,
                    response=sanitized,
                )
                if not persisted:
                    await session.rollback()
                    raise HandoffPostCommitError(
                        "Webhook delivered but its handoff bookkeeping row is missing"
                    )
                await session.commit()
        except HandoffPostCommitError:
            raise
        except Exception as exc:
            raise HandoffPostCommitError(
                "Webhook delivery bookkeeping could not be persisted"
            ) from exc
        record.webhook_sent = sent
        record.webhook_response = sanitized
        return delivery

    async def get_handoff(
        self,
        handoff_id: UUID,
        tenant_id: UUID,
    ) -> HandoffDetail:
        async with self._open_session() as session:
            repository = self._repository_factory(session)
            lookup = await repository.lookup(tenant_id, handoff_id)
            record = lookup.record
            if lookup.outcome is HandoffLookupOutcome.NOT_FOUND or record is None:
                raise HandoffNotFoundError(f"Handoff {handoff_id} not found")
        projection = await self._project_recent_messages(record)
        return map_handoff_record(record, projection)

    async def list_handoffs(
        self,
        tenant_id: UUID,
        status: Optional[HandoffStatus] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[HandoffListItem]:
        async with self._open_session() as session:
            repository = self._repository_factory(session)
            records = await repository.find_by_tenant(
                tenant_id=tenant_id,
                status=status.value if status is not None else None,
                skip=offset,
                limit=limit,
            )
        return [_map_list_item(record) for record in records]

    async def get_pending_handoffs(
        self,
        tenant_id: UUID,
    ) -> list[HandoffListItem]:
        return await self.list_handoffs(tenant_id, HandoffStatus.PENDING)

    async def get_active_handoff_for_session(
        self,
        session_id: UUID,
        *,
        tenant_id: UUID,
    ) -> Optional[HandoffDetail]:
        async with self._open_session() as session:
            repository = self._repository_factory(session)
            lookup = await repository.find_active_for_session(tenant_id, session_id)
            record = lookup.record
            if lookup.outcome is HandoffLookupOutcome.NOT_FOUND or record is None:
                return None
        projection = await self._project_recent_messages(record)
        return map_handoff_record(record, projection)

    async def _project_recent_messages(
        self,
        record: HandoffRecord,
    ) -> RecentMessageProjection:
        try:
            event_store = await self._event_store_for_operation()
            if event_store is None:
                return RecentMessageProjection()
            events = await event_store.get_recent_messages(
                record.tenant_id,
                record.session_id,
                limit=min(self.config.max_recent_messages, 5),
            )
        except Exception as exc:
            logger.warning(
                "handoff_recent_messages_projection_unavailable",
                tenant_id=str(record.tenant_id),
                session_id=str(record.session_id),
                error_type=type(exc).__name__,
            )
            return RecentMessageProjection()

        messages: list[dict[str, Any]] = []
        for event in events[:5]:
            data = event.event_data if isinstance(event.event_data, dict) else {}
            content = data.get("content")
            if not isinstance(content, str):
                continue
            role = data.get("role")
            if not isinstance(role, str):
                role = (
                    "assistant"
                    if event.event_type == EventType.MESSAGE_OUT.value
                    else "user"
                )
            message: dict[str, Any] = {
                "role": role,
                "content": content,
                "timestamp": event.created_at,
            }
            if isinstance(data.get("intent"), str):
                message["intent"] = data["intent"]
            messages.append(message)
        return RecentMessageProjection(tuple(messages))

    def _get_handoff_message(self, reason: HandoffReason) -> str:
        return self.config.default_handoff_messages.get(
            reason.value,
            "Vou encaminhar você para nossa equipe, que dará continuidade ao seu atendimento.",
        )

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
    """Return a stateless singleton containing dependency objects only."""
    global _service
    if _service is None:
        _service = (
            HandoffService()
            if event_store is None
            else HandoffService(event_store=event_store)
        )
    return _service


async def close_handoff_service() -> None:
    global _service
    service = _service
    _service = None
    if service is not None:
        await service.webhook_dispatcher.close()
