"""
NeuroStack API - Demo Leads Endpoints
Lead capture for the investor demo gate form.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import TenantContext, get_tenant_context
from src.api.schemas.demo_leads import DemoLeadCreate, DemoLeadResponse
from src.models.demo_lead import DemoLead
from src.repositories import get_db_session_dependency

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "",
    response_model=DemoLeadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create Demo Lead",
    description="Capture a new lead from the demo gate form. Public endpoint.",
    tags=["Demo Leads"],
)
async def create_demo_lead(
    body: DemoLeadCreate,
    session: AsyncSession = Depends(get_db_session_dependency),
) -> DemoLeadResponse:
    """Create a new demo lead from the gate form submission."""
    lead = DemoLead(
        name=body.name,
        email=body.email,
        company=body.company,
        phone=body.phone,
    )
    session.add(lead)
    await session.flush()
    await session.commit()

    logger.info(
        "Demo lead created", extra={"lead_id": str(lead.id), "email": lead.email}
    )
    return DemoLeadResponse.model_validate(lead)


@router.get(
    "",
    response_model=list[DemoLeadResponse],
    summary="List Demo Leads",
    description="List all demo leads ordered by creation date (admin/super_admin only).",
    tags=["Demo Leads"],
)
async def list_demo_leads(
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> list[DemoLeadResponse]:
    """List all demo leads. Restricted to admin and super_admin roles."""
    if tenant.role not in ("admin", "super_admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin or super admin access required",
        )

    stmt = select(DemoLead).order_by(DemoLead.created_at.desc())
    result = await session.execute(stmt)
    leads = result.scalars().all()

    return [DemoLeadResponse.model_validate(lead) for lead in leads]
