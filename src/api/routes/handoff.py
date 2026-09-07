"""
NeuroStack API - Handoff Routes
Endpoints for human handoff management and webhook configuration.
"""

import csv
import io
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func as sa_func, select
from sqlalchemy.ext.asyncio import AsyncSession

from fastapi import Request
from src.api.middleware.auth import TenantContext, get_tenant_context, require_builder_clinic_access
from src.api.schemas.handoff import (
    HandoffCheckoutResponse,
    HandoffDetail,
    HandoffListItem,
    HandoffListResponse,
    HandoffQueueItem,
    HandoffQueueResponse,
    HandoffUpdateRequest,
    MessageResponse,
    WebhookConfigCreate,
    WebhookConfigResponse,
    WebhookConfigUpdate,
    WebhookTestRequest,
    WebhookTestResponse,
)
from src.handoff.models import HandoffReason, HandoffStatus, WebhookConfig
from src.models.handoff import HandoffRecord
from src.repositories import get_db_session_dependency
from src.services import (
    HandoffService,
    get_handoff_service,
)
from src.core.exceptions import (
    HandoffNotFoundError,
    HandoffServiceError,
    HandoffConfigError,
    WebhookError,
    WebhookDeliveryError,
)
from src.config.logging import get_logger

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
# Tenant resolution (builder / super-admin override)
# ============================================

import jwt as pyjwt
from src.config.settings import settings as _settings


async def _resolve_queue_tenant(
    request: Request,
    tenant_id: UUID,
    tenant: TenantContext,
    db: AsyncSession,
) -> UUID:
    """Resolve effective tenant_id for queue endpoints.

    - Clinic/API-key callers: tenant_id must match their token.
    - Super-admins: may pass any tenant_id freely.
    - Builders: may pass a clinic they own (validated via require_builder_clinic_access).
    """
    if tenant_id == tenant.tenant_id:
        return tenant_id
    if tenant.role == "super_admin":
        return tenant_id
    if tenant.role != "builder":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="tenant_mismatch")

    # Extract builder_id from JWT
    auth_header = request.headers.get("Authorization", "")
    builder_id_str: Optional[str] = None
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        try:
            payload = pyjwt.decode(
                token,
                _settings.secret_key,
                algorithms=["HS256"],
                options={"require": ["sub", "tenant_id", "exp"]},
            )
            builder_id_str = payload.get("builder_id")
        except (pyjwt.InvalidTokenError, KeyError, ValueError):
            pass

    if not builder_id_str:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="builder_id_not_found_in_token")

    builder_id = UUID(builder_id_str)
    return await require_builder_clinic_access(tenant_id, builder_id, db)


# ============================================
# Queue Endpoints (DB-direct, not HandoffService)
# ============================================


def _handoff_queue_fields(
    record: HandoffRecord,
    *,
    expose_first_message: bool,
) -> dict[str, Any]:
    """Project the fields shared by queue listing and checkout responses."""
    context_safety = (
        record.context.get("safety_concern")
        if isinstance(record.context, dict)
        else None
    )
    safety_flag = (
        context_safety
        if type(context_safety) is bool
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
        "primeira_linha_mensagem": (
            record.primeira_linha_mensagem if expose_first_message else None
        ),
        "created_at": record.created_at,
        "data_hora_saida": record.data_hora_saida,
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
    from_date: Optional[date] = Query(None, description="Stats window start (entry date). Defaults to 30 days ago."),
    to_date: Optional[date] = Query(None, description="Stats window end (entry date). Defaults to today."),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> HandoffQueueResponse:
    """Active handoffs (pending/accepted) with aggregate stats.

    Stats (avg wait + % within SLA) are computed over handoffs whose *entry*
    (created_at) falls inside the requested window, so the KPIs match the
    date-filtered list and stale/old handoffs don't skew the average.
    """
    effective_tenant_id = await _resolve_queue_tenant(request, tenant_id, tenant, db)

    # Active items
    q = (
        select(HandoffRecord)
        .where(
            HandoffRecord.tenant_id == effective_tenant_id,
            HandoffRecord.status.in_(["pending", "accepted"]),
        )
        .order_by(HandoffRecord.created_at.asc())
    )
    result = await db.execute(q)
    records = result.scalars().all()

    # Privacy: never expose the customer's first WhatsApp line (often their name)
    items = [
        HandoffQueueItem(**_handoff_queue_fields(r, expose_first_message=False))
        for r in records
    ]

    # Stats window: follow the requested date range (entry date), default last 30 days.
    now_utc = datetime.now(timezone.utc)
    if from_date or to_date:
        window_start = datetime.combine(from_date or now_utc.date(), time.min, tzinfo=timezone.utc)
        window_end = datetime.combine(to_date or now_utc.date(), time.max, tzinfo=timezone.utc)
    else:
        window_start = now_utc - timedelta(days=30)
        window_end = now_utc

    # Stats: avg wait + pct within SLA over completed handoffs created in the window
    stats_q = (
        select(
            sa_func.avg(
                sa_func.extract("epoch", HandoffRecord.data_hora_saida - HandoffRecord.created_at)
            ).label("avg_wait"),
            sa_func.count().label("total"),
        )
        .where(
            HandoffRecord.tenant_id == effective_tenant_id,
            HandoffRecord.status == "completed",
            HandoffRecord.data_hora_saida.isnot(None),
            HandoffRecord.created_at >= window_start,
            HandoffRecord.created_at <= window_end,
        )
    )
    stats_result = await db.execute(stats_q)
    row = stats_result.one()
    avg_wait = float(row.avg_wait or 0)
    total_completed = int(row.total or 0)

    # Get meta_minutos from clinic config
    meta_minutes = 10
    try:
        from src.models.clinic_config_table import ClinicConfigTable
        cfg_q = select(ClinicConfigTable).where(ClinicConfigTable.tenant_id == effective_tenant_id)
        cfg_result = await db.execute(cfg_q)
        cfg_row = cfg_result.scalar_one_or_none()
        if cfg_row and cfg_row.handoff:
            meta_minutes = cfg_row.handoff.get("meta_minutos_atendimento", 10) if isinstance(cfg_row.handoff, dict) else 10
    except Exception:
        pass

    # pct within SLA
    pct_within_sla = 0.0
    if total_completed > 0:
        sla_seconds = meta_minutes * 60
        sla_q = (
            select(sa_func.count())
            .select_from(HandoffRecord)
            .where(
                HandoffRecord.tenant_id == effective_tenant_id,
                HandoffRecord.status == "completed",
                HandoffRecord.data_hora_saida.isnot(None),
                HandoffRecord.created_at >= window_start,
                HandoffRecord.created_at <= window_end,
                sa_func.extract("epoch", HandoffRecord.data_hora_saida - HandoffRecord.created_at) <= sla_seconds,
            )
        )
        sla_result = await db.execute(sla_q)
        within_count = sla_result.scalar() or 0
        pct_within_sla = round((within_count / total_completed) * 100, 1)

    return HandoffQueueResponse(
        items=items,
        avg_wait_seconds=round(avg_wait, 1),
        pct_within_sla=pct_within_sla,
    )


@router.post(
    "/queue/{handoff_id}/checkout",
    response_model=HandoffCheckoutResponse,
    summary="Checkout Handoff",
)
async def checkout_handoff(
    request: Request,
    handoff_id: UUID,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> HandoffCheckoutResponse:
    """Mark a handoff as attended. Returns 409 if already completed."""
    q = select(HandoffRecord).where(HandoffRecord.id == handoff_id)
    result = await db.execute(q)
    record = result.scalar_one_or_none()

    if not record:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="handoff_not_found")

    # Allow builder/super-admin to check out handoffs for clinics they manage
    await _resolve_queue_tenant(request, record.tenant_id, tenant, db)

    if record.data_hora_saida is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="already_completed")

    record.checkout()
    await db.commit()
    await db.refresh(record)

    wait_seconds = None
    if record.data_hora_saida and record.created_at:
        wait_seconds = (record.data_hora_saida - record.created_at).total_seconds()

    return HandoffCheckoutResponse(
        **_handoff_queue_fields(record, expose_first_message=True),
        wait_seconds=wait_seconds,
    )


@router.get(
    "/queue/export",
    summary="Export Handoffs CSV",
)
async def export_handoffs_csv(
    request: Request,
    tenant_id: UUID = Query(..., description="Tenant ID"),
    from_date: Optional[date] = Query(None, description="Start date (default: 30 days ago)"),
    to_date: Optional[date] = Query(None, description="End date (default: today)"),
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> StreamingResponse:
    """Export completed handoffs as CSV with UTF-8 BOM."""
    effective_tenant_id = await _resolve_queue_tenant(request, tenant_id, tenant, db)

    today = date.today()
    effective_to = to_date or today
    effective_from = from_date or (today - timedelta(days=30))

    if (effective_to - effective_from).days > 90:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="max_window_90_days",
        )

    q = (
        select(HandoffRecord)
        .where(
            HandoffRecord.tenant_id == effective_tenant_id,
            HandoffRecord.status == "completed",
            HandoffRecord.data_hora_saida.isnot(None),
            sa_func.date(HandoffRecord.data_hora_saida) >= effective_from,
            sa_func.date(HandoffRecord.data_hora_saida) <= effective_to,
        )
        .order_by(HandoffRecord.data_hora_saida.desc())
        .limit(10000)
    )
    result = await db.execute(q)
    records = result.scalars().all()

    output = io.StringIO()
    output.write("﻿")  # UTF-8 BOM
    writer = csv.writer(output)
    writer.writerow(["numero_whatsapp", "session_id", "created_at", "data_hora_saida", "tempo_espera_segundos"])
    for r in records:
        wait = ""
        if r.data_hora_saida and r.created_at:
            wait = str(round((r.data_hora_saida - r.created_at).total_seconds(), 1))
        writer.writerow([
            r.numero_whatsapp or "",
            str(r.session_id),
            r.created_at.isoformat() if r.created_at else "",
            r.data_hora_saida.isoformat() if r.data_hora_saida else "",
            wait,
        ])

    output.seek(0)
    filename = f"handoffs_{effective_from}_{effective_to}.csv"

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
        timeout_seconds=update.timeout_seconds if update.timeout_seconds is not None else current.timeout_seconds,
        retry_count=update.retry_count if update.retry_count is not None else current.retry_count,
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
    import time
    from uuid import uuid4
    
    from src.handoff.models import HandoffReason, WebhookPayload
    
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
    test_url = request.url or config.url
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
    
    start = time.perf_counter()
    try:
        delivery = await service.webhook_dispatcher.send(tenant.tenant_id, test_payload)
        latency = (time.perf_counter() - start) * 1000
        
        return WebhookTestResponse(
            success=delivery.status.value == "sent",
            message="Webhook test successful" if delivery.status.value == "sent" else f"Webhook test failed: {delivery.error_message}",
            status_code=delivery.response_status,
            latency_ms=round(latency, 2),
        )
    except WebhookDeliveryError as e:
        latency = (time.perf_counter() - start) * 1000
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
        latency = (time.perf_counter() - start) * 1000
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
        latency = (time.perf_counter() - start) * 1000
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
        handoff = await service.get_handoff(handoff_id)
        
        # Verify tenant ownership
        if handoff.tenant_id != tenant.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Handoff not found",
            )
        
        return handoff
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
    - accepted → in_progress (agent starts working)
    - in_progress → completed (agent finishes)
    - any → cancelled (cancel the handoff)
    """
    try:
        # First verify ownership
        handoff = await service.get_handoff(handoff_id)
        if handoff.tenant_id != tenant.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Handoff not found",
            )
        
        from src.handoff.models import HandoffUpdate as HandoffUpdateModel
        
        return await service.update_handoff(HandoffUpdateModel(
            handoff_id=handoff_id,
            status=update.status,
            assigned_to=update.assigned_to,
            notes=update.notes,
        ))
        
    except HandoffNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Handoff not found",
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
        handoff = await service.get_handoff(handoff_id)
        if handoff.tenant_id != tenant.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Handoff not found",
            )
        
        return await service.accept_handoff(handoff_id, agent_id)
        
    except HandoffNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Handoff not found",
        )


@router.post(
    "/{handoff_id}/complete",
    response_model=HandoffDetail,
    summary="Complete Handoff",
    description="Mark a handoff as completed",
)
async def complete_handoff(
    handoff_id: UUID,
    notes: Optional[str] = Query(None, description="Resolution notes"),
    tenant: TenantContext = Depends(get_tenant_context),
    service: HandoffService = Depends(get_service),
) -> HandoffDetail:
    """Mark a handoff as completed with optional notes."""
    try:
        handoff = await service.get_handoff(handoff_id)
        if handoff.tenant_id != tenant.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Handoff not found",
            )
        
        return await service.complete_handoff(handoff_id, notes)
        
    except HandoffNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Handoff not found",
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
        handoff = await service.get_handoff(handoff_id)
        if handoff.tenant_id != tenant.tenant_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Handoff not found",
            )
        
        return await service.cancel_handoff(handoff_id, notes)
        
    except HandoffNotFoundError:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Handoff not found",
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
    handoff = await service.get_active_handoff_for_session(session_id)
    
    if handoff and handoff.tenant_id != tenant.tenant_id:
        return None
    
    return handoff

