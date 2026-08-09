"""
NeuroStack API - RLHF Endpoints
Validate feedback suggestions against SRM methodology and persist approved feedback.
Also captures per-response ratings (1-5) with async AI analysis for low scores.
"""

import asyncio
import json
import logging
import re
from typing import Any, Literal, Optional
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import (
    TenantContext,
    get_tenant_context,
    require_super_admin,
)
from src.llm import get_llm_client, get_tenant_llm_client
from src.models.event import Event
from src.models.tenant import Tenant
from src.observability.pii import mask_carteirinha
from src.policy.engine import get_policy_engine
from src.repositories import get_db_session, get_db_session_dependency

logger = logging.getLogger(__name__)

router = APIRouter()


# ============================================
# Schemas
# ============================================


class ValidateRequest(BaseModel):
    suggestion: str = Field(..., min_length=1)
    original_message: Optional[str] = None
    agent_response: Optional[str] = None


class ViolationItem(BaseModel):
    rule: str
    message: str
    category: str


class ValidateResponse(BaseModel):
    status: str  # "aligned" | "violation"
    violations: list[ViolationItem] = []


class FeedbackRequest(BaseModel):
    session_id: UUID
    user_message: str
    agent_response: str
    suggested_response: str
    status: str = Field(..., description="aligned | forced")


class FeedbackResponse(BaseModel):
    ok: bool = True
    event_id: str


class RatingRequest(BaseModel):
    session_id: str
    message_id: str
    rating: int = Field(..., ge=1, le=5)
    reason: Optional[str] = None
    agent_response: str
    user_message: str
    clinic_tenant_id: Optional[str] = None


class RatingResponse(BaseModel):
    ok: bool = True
    event_id: str


class RatingEntry(BaseModel):
    id: str
    ts: str
    tenant_id: str
    rating: int
    reason: Optional[str] = None
    agent_response: Optional[str] = None
    user_message: Optional[str] = None
    submitted_by: Optional[str] = None
    ai_analysis: Optional[dict[str, Any]] = None
    ai_analysis_status: Optional[str] = None
    approved_for_training: bool = False
    ignored: bool = False


class RatingActionRequest(BaseModel):
    action: Literal["approve", "ignore"]


# ============================================
# Endpoints (existing)
# ============================================


@router.post("/validate", response_model=ValidateResponse)
async def validate_suggestion(
    body: ValidateRequest,
    tenant: TenantContext = Depends(get_tenant_context),
):
    """Validate a feedback suggestion against SRM methodology rules."""
    engine = get_policy_engine()
    result = await engine.validate_response(
        tenant_id=tenant.tenant_id,
        response=body.suggestion,
    )

    if result.passed:
        return ValidateResponse(status="aligned")

    violations = [
        ViolationItem(
            rule=v.rule_name,
            message=v.message,
            category=(
                v.category.value if hasattr(v.category, "value") else str(v.category)
            ),
        )
        for v in result.violations
    ]
    return ValidateResponse(status="violation", violations=violations)


@router.post("/feedback", response_model=FeedbackResponse)
async def save_feedback(
    body: FeedbackRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    """Persist RLHF feedback as an event in the events table."""
    event_id = uuid4()

    event = Event(
        id=event_id,
        tenant_id=tenant.tenant_id,
        session_id=body.session_id if body.session_id else None,
        event_type="rlhf.feedback",
        event_data={
            "user_message": body.user_message,
            "agent_response": body.agent_response,
            "suggested_response": body.suggested_response,
            "status": body.status,
            "submitted_by": str(tenant.user_id) if tenant.user_id else None,
            "role": tenant.role,
        },
        event_metadata={
            "source": "chat_simulator",
        },
    )

    session.add(event)
    await session.flush()
    await session.commit()

    logger.info("RLHF feedback saved: event_id=%s status=%s", event_id, body.status)
    return FeedbackResponse(event_id=str(event_id))


# ============================================
# Rating Endpoints
# ============================================


def _build_analysis_prompt(
    rating: int, reason: str, agent_response: str, user_message: str
) -> str:
    # Use f-string so braces inside user-provided values are interpolated safely
    # and never treated as .format() placeholders (which would raise KeyError).
    return (
        f"Contexto: o agente respondeu '{agent_response}' à mensagem '{user_message}'. "
        f"O usuário avaliou com nota {rating}/5 e justificou: '{reason}'. "
        "Com base na metodologia SRM NeuroStack, essa insatisfação é legítima? "
        'Responda em JSON: {"status": "agreed" | "disagreed", "justification": "<máx 2 frases>"}'
    )


def _parse_analysis_content(content: str) -> Optional[dict[str, str]]:
    """Best-effort JSON extraction from LLM response."""
    if not content:
        return None

    # Try direct JSON first
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        parsed = None

    # Fallback: extract first JSON object substring
    if parsed is None:
        match = re.search(r"\{.*\}", content, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except (json.JSONDecodeError, TypeError):
                parsed = None

    if not isinstance(parsed, dict):
        return None

    status_value = str(parsed.get("status", "")).lower().strip()
    if status_value not in {"agreed", "disagreed"}:
        return None

    justification = str(parsed.get("justification", "")).strip()
    return {"status": status_value, "justification": justification}


async def _analyze_rating_async(
    event_id: UUID,
    rating: int,
    reason: str,
    agent_response: str,
    user_message: str,
    tenant_id: UUID,
    message_id: Optional[str] = None,
    clinic_tenant_id: Optional[str] = None,
) -> None:
    """Run LLM analysis for a low-rating event and persist the result.

    Uses a dedicated prompt (NOT the SRM policy engine) since we're
    evaluating a user's critique, not an agent response. Opens its own
    DB session because the request-scoped one is closed by the time this
    background task runs.

    When message_id is a valid UUID that matches an Event row (e.g. a trace
    event from the Histórico de Traces), we use that event's tenant_id to
    resolve LLM credentials — because the runtime cache only stores the
    non-secret projection, so raw key resolution runs DB-only against the
    CLINIC's tenant, not the builder's. Falls back to the rating's own
    tenant_id (simulator flow, where message_id is a frontend UUID).
    """
    from src.services.event_service import _load_runtime_llm_credentials

    # Resolve which tenant's credentials to use for LLM.
    # Priority: explicit clinic_tenant_id (from frontend) > message_id event lookup > submitter's tenant.
    resolve_tenant_id = tenant_id
    if clinic_tenant_id:
        try:
            resolve_tenant_id = UUID(clinic_tenant_id)
            logger.info(
                "Rating analysis using explicit clinic_tenant_id event_id=%s clinic_tenant=%s",
                event_id,
                resolve_tenant_id,
            )
        except ValueError:
            pass
    elif message_id:
        try:
            msg_uuid = UUID(message_id)
            async with get_db_session() as db:
                original = (
                    await db.execute(select(Event).where(Event.id == msg_uuid))
                ).scalar_one_or_none()
                if original is not None:
                    resolve_tenant_id = original.tenant_id
                    logger.info(
                        "Rating analysis using clinic tenant event_id=%s clinic_tenant=%s",
                        event_id,
                        resolve_tenant_id,
                    )
                else:
                    logger.info(
                        "Rating analysis message_id not found in events, using submitter tenant event_id=%s message_id=%s",
                        event_id,
                        message_id,
                    )
        except Exception as exc:
            logger.warning(
                "Rating analysis message_id lookup failed event_id=%s message_id=%s err=%s",
                event_id,
                message_id,
                exc,
            )

    # DB-only resolver: returns ONLY the four LLM overlay fields (raw key held
    # in-memory for this call), builder-owner composition already applied.
    api_credentials: dict = {}
    try:
        api_credentials = await _load_runtime_llm_credentials(resolve_tenant_id)
    except Exception:
        logger.warning(
            "Could not load tenant LLM credentials for rating analysis event_id=%s",
            event_id,
        )

    tenant_model: Optional[str] = api_credentials.get("llm_model_name") or None
    # Structured log MUST NOT contain any secret material or prefix — only the
    # public presence bit and the non-secret model id are safe to emit.
    logger.info(
        "Rating analysis LLM resolution event_id=%s tenant_id=%s model=%s credentials_present=%s",
        event_id,
        resolve_tenant_id,
        tenant_model,
        bool(api_credentials),
    )
    llm_client = get_tenant_llm_client(resolve_tenant_id, api_credentials)
    if llm_client is None:
        logger.warning(
            "LLM client unavailable; marking rating analysis as failed event_id=%s",
            event_id,
        )
        await _update_rating_status(event_id, status_="failed")
        return

    prompt = _build_analysis_prompt(rating, reason, agent_response, user_message)

    try:
        result = await llm_client.generate(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Você é um auditor de qualidade de respostas de agentes "
                        "de saúde. Responda somente em JSON."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            model=tenant_model,
        )
        parsed = _parse_analysis_content(result.get("content", ""))

        if not parsed:
            logger.warning(
                "Rating analysis returned unparseable content event_id=%s",
                event_id,
            )
            await _update_rating_status(event_id, status_="failed")
            return

        await _update_rating_status(
            event_id,
            status_="done",
            analysis=parsed,
        )
        logger.info(
            "Rating analysis complete event_id=%s status=%s",
            event_id,
            parsed["status"],
        )
    except Exception as exc:  # noqa: BLE001 — background task must swallow
        logger.error(
            "Rating analysis failed event_id=%s error=%s",
            event_id,
            str(exc),
            exc_info=True,
        )
        await _update_rating_status(event_id, status_="failed")


async def _update_rating_status(
    event_id: UUID,
    status_: str,
    analysis: Optional[dict[str, str]] = None,
) -> None:
    """Update the rating event's ai_analysis_status (and optionally ai_analysis)."""
    try:
        async with get_db_session() as session:
            result = await session.execute(select(Event).where(Event.id == event_id))
            event = result.scalar_one_or_none()
            if event is None:
                logger.warning(
                    "Rating event vanished before analysis update event_id=%s",
                    event_id,
                )
                return

            data = dict(event.event_data or {})
            data["ai_analysis_status"] = status_
            if analysis is not None:
                data["ai_analysis"] = analysis
            event.event_data = data
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception(
            "Failed to persist rating analysis status event_id=%s status=%s",
            event_id,
            status_,
        )


@router.post("/rating", response_model=RatingResponse)
async def submit_rating(
    body: RatingRequest,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
):
    """Persist a rating (1-5) for an agent response.

    For ratings <= 2 with a reason, a background LLM analysis is triggered
    to evaluate whether the user critique is legitimate. Status is tracked
    via `ai_analysis_status` (pending/done/failed).
    """
    event_id = uuid4()
    reason = (body.reason or "").strip() or None
    triggers_analysis = body.rating <= 2 and bool(reason)

    event_data: dict[str, Any] = {
        "message_id": body.message_id,
        "rating": body.rating,
        "reason": reason,
        "agent_response": body.agent_response,
        "user_message": body.user_message,
        "submitted_by": str(tenant.user_id) if tenant.user_id else None,
        "role": tenant.role,
    }
    if triggers_analysis:
        event_data["ai_analysis_status"] = "pending"

    session_uuid: Optional[UUID] = None
    if body.session_id:
        try:
            session_uuid = UUID(body.session_id)
        except (ValueError, AttributeError):
            session_uuid = None

    event = Event(
        id=event_id,
        tenant_id=tenant.tenant_id,
        session_id=session_uuid,
        event_type="rlhf.rating",
        event_data=event_data,
        event_metadata={"source": "chat_simulator"},
    )

    session.add(event)
    await session.flush()
    await session.commit()

    if triggers_analysis:
        asyncio.create_task(
            _analyze_rating_async(
                event_id=event_id,
                rating=body.rating,
                reason=reason,
                agent_response=body.agent_response,
                user_message=body.user_message,
                tenant_id=tenant.tenant_id,
                message_id=body.message_id,
                clinic_tenant_id=body.clinic_tenant_id,
            )
        )

    logger.info(
        "RLHF rating saved: event_id=%s rating=%s analysis=%s",
        event_id,
        body.rating,
        triggers_analysis,
    )
    return RatingResponse(event_id=str(event_id))


def _serialize_rating_event(event: Event) -> dict[str, Any]:
    data = event.event_data or {}
    # Mask carteirinha PII on the training-lake export surface: rating
    # rows flagged as approved_for_training feed the SLM fine-tuning
    # dataset, so the raw number must never leave the event store here.
    return {
        "id": str(event.id),
        "ts": event.created_at.isoformat() if event.created_at else "",
        "tenant_id": str(event.tenant_id),
        "rating": int(data.get("rating", 0)),
        "reason": mask_carteirinha(data.get("reason")),
        "agent_response": mask_carteirinha(data.get("agent_response")),
        "user_message": mask_carteirinha(data.get("user_message")),
        "submitted_by": data.get("submitted_by"),
        "ai_analysis": data.get("ai_analysis"),
        "ai_analysis_status": data.get("ai_analysis_status"),
        "approved_for_training": bool(data.get("approved_for_training", False)),
        "ignored": bool(data.get("ignored", False)),
    }


@router.get("/ratings")
async def list_ratings(
    builder_id: Optional[UUID] = Query(None),
    tenant_id: Optional[UUID] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> list[dict[str, Any]]:
    """List rating events. Super admin may filter by builder_id / tenant_id.

    For non-super-admin callers, `builder_id` and `tenant_id` are ignored
    silently: results are always scoped to the caller's own tenant.
    """
    is_super = tenant.role == "super_admin"

    stmt = select(Event).where(Event.event_type == "rlhf.rating")

    if is_super:
        if tenant_id is not None:
            stmt = stmt.where(Event.tenant_id == tenant_id)
        elif builder_id is not None:
            # Join through tenants to filter by builder
            stmt = stmt.join(Tenant, Tenant.id == Event.tenant_id).where(
                Tenant.builder_id == builder_id
            )
        # else: return all tenants
    else:
        # Non-super-admin: ignore filters silently, scope to own tenant
        stmt = stmt.where(Event.tenant_id == tenant.tenant_id)

    stmt = stmt.order_by(Event.created_at.desc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    events = result.scalars().all()

    return [_serialize_rating_event(e) for e in events]


@router.patch("/ratings/{event_id}")
async def update_rating_action(
    event_id: UUID,
    body: RatingActionRequest,
    tenant: TenantContext = Depends(require_super_admin),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    """Approve or ignore a rating entry for dataset curation.

    Super admin only — approvals are training-dataset gatekeepers.
    """
    result = await session.execute(
        select(Event).where(
            Event.id == event_id,
            Event.event_type == "rlhf.rating",
        )
    )
    event = result.scalar_one_or_none()
    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Rating not found",
        )

    data = dict(event.event_data or {})
    if body.action == "approve":
        data["approved_for_training"] = True
        data.pop("ignored", None)
    else:
        data["ignored"] = True
        data.pop("approved_for_training", None)

    event.event_data = data
    await session.commit()

    logger.info(
        "RLHF rating action: event_id=%s action=%s by=%s",
        event_id,
        body.action,
        tenant.user_id,
    )
    return {"ok": True, "id": str(event_id), "action": body.action}
