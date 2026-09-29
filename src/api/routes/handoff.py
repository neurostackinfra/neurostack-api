"""
NeuroStack API - Handoff Routes
Endpoints for human handoff management and webhook configuration.
"""

import csv
import io
import time as time_module
from datetime import date, datetime, timezone
from typing import Any, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import (
    TenantContext,
    get_tenant_context,
    resolve_effective_tenant,
)
from src.api.schemas.handoff import (
    HandoffCheckoutResponse,
    HandoffConflictResponse,
    HandoffDetail,
    HandoffListItem,
    HandoffListResponse,
    HandoffNotFoundResponse,
    HandoffQueueItem,
    HandoffQueueResponse,
    HandoffUnavailableResponse,
    HandoffUpdateRequest,
    MessageResponse,
    WebhookConfigCreate,
    WebhookConfigResponse,
    WebhookConfigUpdate,
    WebhookTestRequest,
    WebhookTestResponse,
)
from src.config.logging import get_logger
from src.core.exceptions import (
    HandoffConfigError,
    HandoffNotFoundError,
    HandoffServiceError,
    WebhookDeliveryError,
    WebhookError,
)
from src.handoff.models import (
    HandoffReason,
    HandoffStatus,
    HandoffUrgency,
    WebhookConfig,
)
from src.models.handoff import HandoffRecord
from src.repositories import get_db_session_dependency
from src.repositories.handoff import HandoffSlaCohortRow
from src.services import HandoffService, get_handoff_service
from src.services.handoff_service import (
    HandoffLifecycleError,
    HandoffPostCommitError,
    HandoffSlaPeriodError,
)

logger = get_logger(__name__)

router = APIRouter()

_LEGACY_HANDOFF_INTENTS = frozenset(reason.value for reason in HandoffReason)


# ============================================
# Dependencies
# ============================================


def get_service() -> HandoffService:
    """Get handoff service dependency."""
    return get_handoff_service()


# ============================================
# Queue Endpoints (DB-direct, not HandoffService)
# ============================================


def _handoff_queue_fields(
    record: HandoffSlaCohortRow,
) -> dict[str, Any]:
    """Project the fields shared by queue listing and checkout responses."""
    safety_flag = (
        record.safety_concern
        if type(record.safety_concern) is bool
        else record.reason == HandoffReason.SAFETY_CONCERN.value
    )
    detected_intent = record.intencao_detectada
    if detected_intent in _LEGACY_HANDOFF_INTENTS:
        detected_intent = None

    return {
        "id": record.id,
        "numero_whatsapp": record.numero_whatsapp,
        "session_id": record.session_id,
        "safety_flag": safety_flag,
        "intencao_detectada": detected_intent,
        "estagio_atual": record.estagio_atual,
        "primeira_linha_mensagem": None,
        "created_at": record.created_at,
        "data_hora_saida": record.data_hora_saida,
        "sla_target_seconds": record.sla_target_seconds,
        "status": record.status,
    }


@router.get(
    "/queue",
    response_model=HandoffQueueResponse,
    summary="Get Handoff Queue",
)
async def get_handoff_queue(
    request: Request,
    tenant_id: UUID = Query(..., description="Tenant to fetch queue for"),
    from_date: Optional[date] = Query(
        None, description="Stats window start (entry date). Defaults to 30 days ago."
    ),
    to_date: Optional[date] = Query(
        None, description="Stats window end (entry date). Defaults to today."
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
    service: HandoffService = Depends(get_service),
) -> HandoffQueueResponse:
    """Return the waiting subset and SLA radar for one entry-date cohort."""
    effective_tenant_id = await resolve_effective_tenant(request, tenant, tenant_id, db)
    try:
        queue = await service.get_queue_sla(
            effective_tenant_id,
            from_date=from_date,
            to_date=to_date,
        )
    except HandoffSlaPeriodError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    average = queue.metrics.average_confirmed_seconds
    percentage = queue.metrics.pct_within_sla
    return HandoffQueueResponse(
        items=[
            HandoffQueueItem(**_handoff_queue_fields(record)) for record in queue.items
        ],
        avg_wait_seconds=round(average, 1) if average is not None else None,
        pct_within_sla=float(percentage) if percentage is not None else None,
        within_sla_count=queue.within_sla_count,
        confirmed_late_count=queue.confirmed_late_count,
        unanswered_overdue_count=queue.unanswered_overdue_count,
        maturing_count=queue.maturing_count,
        cancelled_unconfirmed_count=queue.cancelled_unconfirmed_count,
        unmeasured_legacy_count=queue.unmeasured_legacy_count,
        confirmed_count=queue.metrics.confirmed_count,
        sla_eligible_count=queue.metrics.sla_eligible_count,
        period_timezone=queue.period_timezone,
        period_start=queue.period.start_utc,
        period_end_exclusive=queue.period.end_exclusive_utc,
    )


async def _confirm_queue_handoff(
    *,
    request: Request,
    handoff_id: UUID,
    tenant_id: UUID | None,
    tenant: TenantContext,
    db: AsyncSession,
    service: HandoffService,
) -> HandoffDetail:
    """Authorize and return the service's committed lifecycle representation."""

    effective_tenant_id = await resolve_effective_tenant(request, tenant, tenant_id, db)

    try:
        return await service.confirm_handoff(
            handoff_id,
            tenant_id=effective_tenant_id,
        )
    except HandoffNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="handoff_not_found",
        ) from exc
    except HandoffLifecycleError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.code,
        ) from exc
    except (HandoffPostCommitError, HandoffServiceError) as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="handoff_service_unavailable",
        ) from exc


def _legacy_checkout_response(detail: HandoffDetail) -> HandoffCheckoutResponse:
    """Adapt the committed service result without another persistence read."""

    compatibility = detail._checkout_compatibility
    if compatibility is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="handoff_service_unavailable",
        )
    wait_seconds = None
    if detail.data_hora_saida is not None:
        wait_seconds = (detail.data_hora_saida - detail.created_at).total_seconds()
    return HandoffCheckoutResponse(
        id=detail.id,
        session_id=detail.session_id,
        created_at=detail.created_at,
        data_hora_saida=detail.data_hora_saida,
        status=detail.status,
        wait_seconds=wait_seconds,
        **compatibility,
    )


@router.post(
    "/queue/{handoff_id}/confirm",
    response_model=HandoffDetail,
    summary="Confirm Handoff Attendance",
    responses={
        404: {"model": HandoffNotFoundResponse},
        409: {"model": HandoffConflictResponse},
        503: {"model": HandoffUnavailableResponse},
    },
)
async def confirm_queue_handoff(
    request: Request,
    handoff_id: UUID,
    tenant_id: UUID | None = Query(
        None,
        description="Tenant that owns the handoff for an authorized override",
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
    service: HandoffService = Depends(get_service),
) -> HandoffDetail:
    """Persist the first human-response timestamp without completing handoff."""

    return await _confirm_queue_handoff(
        request=request,
        handoff_id=handoff_id,
        tenant_id=tenant_id,
        tenant=tenant,
        db=db,
        service=service,
    )


@router.post(
    "/queue/{handoff_id}/checkout",
    response_model=HandoffCheckoutResponse,
    summary="Checkout Handoff",
    deprecated=True,
    responses={
        404: {"model": HandoffNotFoundResponse},
        409: {"model": HandoffConflictResponse},
        503: {"model": HandoffUnavailableResponse},
    },
)
async def checkout_handoff(
    request: Request,
    handoff_id: UUID,
    tenant_id: UUID | None = Query(
        None,
        description="Tenant that owns the handoff for an authorized override",
    ),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
    service: HandoffService = Depends(get_service),
) -> HandoffCheckoutResponse:
    """Deprecated HTTP alias for attendance confirmation."""

    return _legacy_checkout_response(
        await _confirm_queue_handoff(
            request=request,
            handoff_id=handoff_id,
            tenant_id=tenant_id,
            tenant=tenant,
            db=db,
            service=service,
        )
    )


@router.get(
    "/queue/export",
    summary="Export Handoffs CSV",
    description=(
        "Export handoffs whose created_at is within the requested clinic-local-date "
        "window and whose current status is pending, accepted, completed with a "
        "confirmation timestamp, or confirmed in_progress. The UTF-8 CSV columns, "
        "in order, are numero_whatsapp, session_id, created_at, data_hora_saida, "
        "tempo_espera_segundos, status, and exported_at. For pending and accepted "
        "rows, data_hora_saida is empty and tempo_espera_segundos runs from "
        "created_at to exported_at. For confirmed completed and in_progress rows, "
        "tempo_espera_segundos runs from created_at to data_hora_saida. Timestamps "
        "are emitted in UTC and exports over 10,000 rows are rejected."
    ),
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "UTF-8 CSV attachment containing the seven documented columns.",
            "content": {"text/csv": {"schema": {"type": "string"}}},
        },
        400: {
            "description": (
                "Invalid request: max_window_90_days, invalid_date_range, "
                "invalid_handoff_timestamps, or export_limit_10000_exceeded."
            )
        },
    },
)
async def export_handoffs_csv(
    request: Request,
    tenant_id: UUID = Query(..., description="Tenant ID"),
    from_date: Optional[date] = Query(
        None, description="Start date (default: 30 days ago)"
    ),
    to_date: Optional[date] = Query(None, description="End date (default: today)"),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
    service: HandoffService = Depends(get_service),
) -> StreamingResponse:
    """Export the handoff entry cohort, including open and completed waits.

    The requested inclusive dates filter created_at after conversion from the
    clinic's IANA timezone to a semi-open UTC window. Current pending and accepted
    states are open and use one export instant for their wait; confirmed completed
    and in_progress rows use their recorded confirmation timestamp. Invalid requests
    return max_window_90_days, invalid_date_range, invalid_handoff_timestamps,
    or export_limit_10000_exceeded.
    """
    effective_tenant_id = await resolve_effective_tenant(request, tenant, tenant_id, db)
    try:
        resolved_period = await service.resolve_queue_period(
            effective_tenant_id,
            from_date=from_date,
            to_date=to_date,
        )
    except HandoffSlaPeriodError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    q = (
        select(
            HandoffRecord.numero_whatsapp,
            HandoffRecord.session_id,
            HandoffRecord.created_at,
            HandoffRecord.data_hora_saida,
            HandoffRecord.status,
        )
        .where(
            HandoffRecord.tenant_id == effective_tenant_id,
            HandoffRecord.created_at >= resolved_period.period.start_utc,
            HandoffRecord.created_at < resolved_period.period.end_exclusive_utc,
            HandoffRecord.status.in_(["pending", "accepted"])
            | (
                HandoffRecord.status.in_(["completed", "in_progress"])
                & HandoffRecord.data_hora_saida.isnot(None)
            ),
        )
        .order_by(HandoffRecord.created_at.desc())
        .limit(10001)
    )
    result = await db.execute(q)
    records: list[tuple[Optional[str], UUID, datetime, Optional[datetime], str]] = list(
        result.tuples().all()
    )
    if len(records) == 10001:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="export_limit_10000_exceeded",
        )

    exported_at = datetime.now(timezone.utc)

    def as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    for _, _, created_at_value, checked_out_at_value, handoff_status in records:
        created_at = as_utc(created_at_value)
        if handoff_status in {"pending", "accepted"}:
            invalid_timestamp_order = created_at > exported_at
        else:
            invalid_timestamp_order = (
                checked_out_at_value is not None
                and as_utc(checked_out_at_value) < created_at
            )
        if invalid_timestamp_order:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="invalid_handoff_timestamps",
            )

    output = io.StringIO()
    output.write("﻿")  # UTF-8 BOM
    writer = csv.writer(output)
    writer.writerow(
        [
            "numero_whatsapp",
            "session_id",
            "created_at",
            "data_hora_saida",
            "tempo_espera_segundos",
            "status",
            "exported_at",
        ]
    )
    for (
        phone,
        session_id,
        created_at_value,
        checked_out_at_value,
        handoff_status,
    ) in records:
        created_at = as_utc(created_at_value)
        is_open = handoff_status in {"pending", "accepted"}
        checked_out_at = None
        if not is_open and checked_out_at_value is not None:
            checked_out_at = as_utc(checked_out_at_value)
        if is_open:
            wait = str(round((exported_at - created_at).total_seconds(), 1))
        elif checked_out_at is not None:
            wait = str(round((checked_out_at - created_at).total_seconds(), 1))
        else:
            wait = ""
        safe_phone = phone or ""
        if safe_phone[:1] in {
            "=",
            "+",
            "-",
            "@",
            "\t",
            "\r",
            "\n",
            "＝",
            "＋",
            "－",
            "＠",
        }:
            safe_phone = f"'{safe_phone}"
        writer.writerow(
            [
                safe_phone,
                str(session_id),
                created_at.isoformat(),
                checked_out_at.isoformat() if checked_out_at else "",
                wait,
                handoff_status,
                exported_at.isoformat(),
            ]
        )

    output.seek(0)
    filename = f"handoffs_{resolved_period.from_date}_{resolved_period.to_date}.csv"

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ============================================
# Webhook Configuration Endpoints
# ============================================


@router.post(
    "/webhooks",
    response_model=WebhookConfigResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Configure Webhook",
    description="Configure webhook URL for handoff notifications",
)
async def configure_webhook(
    config: WebhookConfigCreate,
    tenant: TenantContext = Depends(get_tenant_context),
    service: HandoffService = Depends(get_service),
) -> WebhookConfigResponse:
    """
    Configure or update webhook for handoff notifications.

    The webhook will be called when:
    - A new handoff is created
    - Handoff status changes (accepted, completed, timeout)

    The webhook payload includes:
    - Handoff details (ID, session ID, urgency, reason)
    - Conversation summary
    - Key information extracted
    - Recent messages

    Security:
    - If a secret is provided, requests will include X-Webhook-Signature header
    - Signature is HMAC-SHA256 of the payload
    """
    try:
        webhook_config = WebhookConfig(
            url=config.url,
            secret=config.secret,
            headers=config.headers,
            timeout_seconds=config.timeout_seconds,
            retry_count=config.retry_count,
            enabled=config.enabled,
        )

        service.configure_webhook(tenant.tenant_id, webhook_config)

        from datetime import datetime, timezone

        return WebhookConfigResponse(
            tenant_id=tenant.tenant_id,
            url=config.url,
            timeout_seconds=config.timeout_seconds,
            retry_count=config.retry_count,
            enabled=config.enabled,
            created_at=datetime.now(timezone.utc),
            has_secret=config.secret is not None,
        )

    except HandoffConfigError as e:
        logger.warning(
            "Invalid webhook configuration",
            tenant_id=str(tenant.tenant_id),
            error=str(e),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid webhook configuration: {str(e)}",
        )
    except HandoffServiceError as e:
        logger.error(
            "Handoff service error configuring webhook",
            tenant_id=str(tenant.tenant_id),
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to configure webhook: {str(e)}",
        )
    except Exception as e:
        logger.error(
            "Unexpected error configuring webhook",
            tenant_id=str(tenant.tenant_id),
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to configure webhook",
        )


@router.get(
    "/webhooks",
    response_model=WebhookConfigResponse,
    summary="Get Webhook Configuration",
    description="Get current webhook configuration",
)
async def get_webhook_config(
    tenant: TenantContext = Depends(get_tenant_context),
    service: HandoffService = Depends(get_service),
) -> WebhookConfigResponse:
    """Get current webhook configuration for the tenant."""
    config = service.get_webhook_config(tenant.tenant_id)

    if not config:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook not configured",
        )

    from datetime import datetime, timezone

    return WebhookConfigResponse(
        tenant_id=tenant.tenant_id,
        url=config.url,
        timeout_seconds=config.timeout_seconds,
        retry_count=config.retry_count,
        enabled=config.enabled,
        created_at=datetime.now(timezone.utc),  # Would come from DB in real impl
        has_secret=config.secret is not None,
    )


@router.patch(
    "/webhooks",
    response_model=WebhookConfigResponse,
    summary="Update Webhook Configuration",
    description="Partially update webhook configuration",
)
async def update_webhook_config(
    update: WebhookConfigUpdate,
    tenant: TenantContext = Depends(get_tenant_context),
    service: HandoffService = Depends(get_service),
) -> WebhookConfigResponse:
    """Update specific fields of webhook configuration."""
    current = service.get_webhook_config(tenant.tenant_id)

    if not current:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook not configured",
        )

    # Update only provided fields
    new_config = WebhookConfig(
        url=update.url if update.url is not None else current.url,
        secret=update.secret if update.secret is not None else current.secret,
        headers=update.headers if update.headers is not None else current.headers,
        timeout_seconds=(
            update.timeout_seconds
            if update.timeout_seconds is not None
            else current.timeout_seconds
        ),
        retry_count=(
            update.retry_count
            if update.retry_count is not None
            else current.retry_count
        ),
        enabled=update.enabled if update.enabled is not None else current.enabled,
    )

    service.configure_webhook(tenant.tenant_id, new_config)

    from datetime import datetime, timezone

    return WebhookConfigResponse(
        tenant_id=tenant.tenant_id,
        url=new_config.url,
        timeout_seconds=new_config.timeout_seconds,
        retry_count=new_config.retry_count,
        enabled=new_config.enabled,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        has_secret=new_config.secret is not None,
    )


@router.delete(
    "/webhooks",
    response_model=MessageResponse,
    summary="Remove Webhook Configuration",
    description="Remove webhook configuration",
)
async def remove_webhook_config(
    tenant: TenantContext = Depends(get_tenant_context),
    service: HandoffService = Depends(get_service),
) -> MessageResponse:
    """Remove webhook configuration for the tenant."""
    removed = service.remove_webhook_config(tenant.tenant_id)

    if not removed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Webhook not configured",
        )

    return MessageResponse(message="Webhook configuration removed")


@router.post(
    "/webhooks/test",
    response_model=WebhookTestResponse,
    summary="Test Webhook",
    description="Send a test webhook to verify configuration",
)
async def test_webhook(
    request: WebhookTestRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    service: HandoffService = Depends(get_service),
) -> WebhookTestResponse:
    """
    Send a test webhook notification.

    Uses the configured webhook URL unless a different URL is provided.
    """
    config = service.get_webhook_config(tenant.tenant_id)

    if not config and not request.url:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No webhook configured and no URL provided",
        )

    # Create test payload
    test_payload = service.webhook_dispatcher.create_payload(
        handoff_id=uuid4(),
        tenant_id=tenant.tenant_id,
        session_id=uuid4(),
        status=HandoffStatus.PENDING,
        urgency=HandoffUrgency.NORMAL,
        reason=HandoffReason.USER_REQUEST,
        summary="This is a test webhook notification",
        key_info={"test": True},
        turn_count=1,
        funnel_stage="test",
        recent_messages=[{"role": "user", "content": "Test message"}],
    )

    # Override URL if provided
    test_url = request.url or (config.url if config is not None else "")
    test_config = WebhookConfig(
        url=test_url,
        secret=config.secret if config else None,
        timeout_seconds=config.timeout_seconds if config else 10,
        retry_count=0,  # No retries for test
        enabled=True,
    )

    # Temporarily set test config
    original_config = service.webhook_dispatcher.get_config(tenant.tenant_id)
    service.webhook_dispatcher.set_config(tenant.tenant_id, test_config)

    start = time_module.perf_counter()
    try:
        delivery = await service.webhook_dispatcher.send(tenant.tenant_id, test_payload)
        latency = (time_module.perf_counter() - start) * 1000

        return WebhookTestResponse(
            success=delivery.status.value == "sent",
            message=(
                "Webhook test successful"
                if delivery.status.value == "sent"
                else f"Webhook test failed: {delivery.error_message}"
            ),
            status_code=delivery.response_status,
            latency_ms=round(latency, 2),
        )
    except WebhookDeliveryError as e:
        latency = (time_module.perf_counter() - start) * 1000
        logger.warning(
            "Webhook test delivery failed",
            tenant_id=str(tenant.tenant_id),
            status_code=e.status_code,
            error=str(e),
        )
        return WebhookTestResponse(
            success=False,
            message=f"Webhook test failed: {str(e)}",
            status_code=e.status_code,
            latency_ms=round(latency, 2),
        )
    except WebhookError as e:
        latency = (time_module.perf_counter() - start) * 1000
        logger.warning(
            "Webhook test error",
            tenant_id=str(tenant.tenant_id),
            error=str(e),
        )
        return WebhookTestResponse(
            success=False,
            message=f"Webhook test failed: {str(e)}",
            latency_ms=round(latency, 2),
        )
    except Exception as e:
        latency = (time_module.perf_counter() - start) * 1000
        logger.error(
            "Unexpected error during webhook test",
            tenant_id=str(tenant.tenant_id),
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        return WebhookTestResponse(
            success=False,
            message="Webhook test failed due to unexpected error",
            latency_ms=round(latency, 2),
        )
    finally:
        # Restore original config
        if original_config:
            service.webhook_dispatcher.set_config(tenant.tenant_id, original_config)
        else:
            service.webhook_dispatcher.remove_config(tenant.tenant_id)


# ============================================
# Handoff Management Endpoints
# ============================================


@router.get(
    "",
    response_model=HandoffListResponse,
    summary="List Handoffs",
    description="List all handoffs for the tenant",
)
async def list_handoffs(
    status: Optional[HandoffStatus] = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=100, description="Maximum records"),
    offset: int = Query(0, ge=0, description="Skip records"),
    tenant: TenantContext = Depends(get_tenant_context),
    service: HandoffService = Depends(get_service),
) -> HandoffListResponse:
    """
    List handoffs for the current tenant.

    Supports filtering by status and pagination.
    """
    handoffs = await service.list_handoffs(
        tenant_id=tenant.tenant_id,
        status=status,
        limit=limit,
        offset=offset,
    )

    return HandoffListResponse(
        handoffs=handoffs,
        total=len(handoffs),  # Would be total count from DB
        limit=limit,
        offset=offset,
    )


@router.get(
    "/pending",
    response_model=list[HandoffListItem],
    summary="Get Pending Handoffs",
    description="Get all pending handoffs waiting for agent",
)
async def get_pending_handoffs(
    tenant: TenantContext = Depends(get_tenant_context),
    service: HandoffService = Depends(get_service),
) -> list[HandoffListItem]:
    """Get all pending handoffs for the tenant."""
    return await service.get_pending_handoffs(tenant.tenant_id)


@router.get(
    "/{handoff_id}",
    response_model=HandoffDetail,
    summary="Get Handoff Details",
    description="Get detailed information about a specific handoff",
)
async def get_handoff(
    handoff_id: UUID,
    tenant: TenantContext = Depends(get_tenant_context),
    service: HandoffService = Depends(get_service),
) -> HandoffDetail:
    """Get detailed handoff information."""
    try:
        return await service.get_handoff(
            handoff_id,
            tenant_id=tenant.tenant_id,
        )
    except HandoffNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Handoff not found",
        )


@router.patch(
    "/{handoff_id}",
    response_model=HandoffDetail,
    summary="Update Handoff",
    description="Update handoff status",
    responses={409: {"description": "Typed lifecycle conflict"}},
)
async def update_handoff(
    handoff_id: UUID,
    update: HandoffUpdateRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    service: HandoffService = Depends(get_service),
) -> HandoffDetail:
    """
    Update handoff status.

    Valid transitions:
    - pending → accepted (agent accepts)
    - attendance confirmation uses POST /queue/{handoff_id}/confirm
    - in_progress → completed (agent finishes after confirmation)
    - any → cancelled (cancel the handoff)
    """
    try:
        from src.handoff.models import HandoffUpdate as HandoffUpdateModel

        return await service.update_handoff(
            HandoffUpdateModel(
                handoff_id=handoff_id,
                status=update.status,
                assigned_to=update.assigned_to,
                notes=update.notes,
            ),
            tenant_id=tenant.tenant_id,
        )

    except HandoffNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Handoff not found",
        )
    except HandoffLifecycleError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.code,
        )
    except HandoffServiceError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        )


@router.post(
    "/{handoff_id}/accept",
    response_model=HandoffDetail,
    summary="Accept Handoff",
    description="Accept a pending handoff",
)
async def accept_handoff(
    handoff_id: UUID,
    agent_id: Optional[str] = Query(None, description="Agent identifier"),
    tenant: TenantContext = Depends(get_tenant_context),
    service: HandoffService = Depends(get_service),
) -> HandoffDetail:
    """Accept a handoff and optionally assign to an agent."""
    try:
        return await service.accept_handoff(
            handoff_id,
            agent_id,
            tenant_id=tenant.tenant_id,
        )

    except HandoffNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Handoff not found",
        )
    except HandoffLifecycleError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.code,
        )


@router.post(
    "/{handoff_id}/complete",
    response_model=HandoffDetail,
    summary="Complete Handoff",
    description="Mark a handoff as completed",
    responses={409: {"description": "Typed lifecycle conflict"}},
)
async def complete_handoff(
    handoff_id: UUID,
    notes: Optional[str] = Query(None, description="Resolution notes"),
    tenant: TenantContext = Depends(get_tenant_context),
    service: HandoffService = Depends(get_service),
) -> HandoffDetail:
    """Mark a handoff as completed with optional notes."""
    try:
        return await service.complete_handoff(
            handoff_id,
            notes,
            tenant_id=tenant.tenant_id,
        )

    except HandoffNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Handoff not found",
        )
    except HandoffLifecycleError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.code,
        )


@router.post(
    "/{handoff_id}/cancel",
    response_model=HandoffDetail,
    summary="Cancel Handoff",
    description="Cancel a handoff",
)
async def cancel_handoff(
    handoff_id: UUID,
    notes: Optional[str] = Query(None, description="Cancellation reason"),
    tenant: TenantContext = Depends(get_tenant_context),
    service: HandoffService = Depends(get_service),
) -> HandoffDetail:
    """Cancel a handoff with optional notes."""
    try:
        return await service.cancel_handoff(
            handoff_id,
            notes,
            tenant_id=tenant.tenant_id,
        )

    except HandoffNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Handoff not found",
        )
    except HandoffLifecycleError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.code,
        )


@router.get(
    "/session/{session_id}",
    response_model=Optional[HandoffDetail],
    summary="Get Session Handoff",
    description="Get active handoff for a session if any",
)
async def get_session_handoff(
    session_id: UUID,
    tenant: TenantContext = Depends(get_tenant_context),
    service: HandoffService = Depends(get_service),
) -> Optional[HandoffDetail]:
    """Get active handoff for a session."""
    return await service.get_active_handoff_for_session(
        session_id,
        tenant_id=tenant.tenant_id,
    )
