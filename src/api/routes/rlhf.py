"""
NeuroStack API - RLHF Endpoints
Validate feedback suggestions against SRM methodology and persist approved feedback.
"""

import logging
from typing import Any, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import TenantContext, get_tenant_context
from src.models.event import Event
from src.policy.engine import get_policy_engine
from src.repositories import get_db_session_dependency

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
    session_id: str
    user_message: str
    agent_response: str
    suggested_response: str
    status: str = Field(..., description="aligned | forced")


class FeedbackResponse(BaseModel):
    ok: bool = True
    event_id: str


# ============================================
# Endpoints
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
            category=v.category.value if hasattr(v.category, "value") else str(v.category),
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
