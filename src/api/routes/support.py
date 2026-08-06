"""
NeuroStack API - Support Endpoint
Receive support tickets, persist to DB, and send them via email (SMTP).
"""

import base64
import logging
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import aiosmtplib
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import String, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import TenantContext, get_tenant_context
from src.config.settings import settings
from src.models.builder import Builder
from src.models.support_ticket import SupportTicket
from src.models.tenant import Tenant
from src.models.user import User
from src.repositories import get_db_session_dependency

logger = logging.getLogger(__name__)

router = APIRouter()


class SupportTicketRequest(BaseModel):
    tipo: str = Field(..., description="Ticket type: solicitacao, feedback, financeiro")
    mensagem: str = Field(..., min_length=1, description="Ticket message body")
    filename: Optional[str] = None
    file_type: Optional[str] = None
    file_data: Optional[str] = None  # base64-encoded


class SupportTicketResponse(BaseModel):
    ok: bool = True
    email_sent: bool
    ticket_id: Optional[int] = None


class SupportTicketOut(BaseModel):
    id: int
    clinic_id: Optional[str] = None
    clinic_name: Optional[str] = None
    email: str
    tipo: str
    mensagem: str
    filename: Optional[str] = None
    file_type: Optional[str] = None
    status: str
    created_at: str
    routed_to: Optional[str] = None

    class Config:
        from_attributes = True


TIPO_LABELS = {
    "solicitacao": "Solicitação",
    "feedback": "Feedback",
    "financeiro": "Financeiro",
}


def _extract_jwt_info(request: Request) -> dict:
    """Extract sender email, role, and builder_id from JWT token."""
    info = {"email": "unknown", "role": None, "builder_id": None}
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        import jwt as pyjwt
        try:
            payload = pyjwt.decode(
                auth_header[7:],
                settings.secret_key,
                algorithms=["HS256"],
                options={"verify_exp": False},
            )
            info["email"] = payload.get("email", "unknown")
            info["role"] = payload.get("role")
            info["builder_id"] = payload.get("builder_id")
        except Exception:
            pass
    return info


@router.post("/tickets", response_model=SupportTicketResponse)
async def create_support_ticket(
    body: SupportTicketRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
):
    jwt_info = _extract_jwt_info(request)
    sender_email = jwt_info["email"]
    tipo_label = TIPO_LABELS.get(body.tipo, body.tipo)

    # Resolve display name: builder name for builders, clinic name for clinics
    clinic_name = None
    try:
        if jwt_info["builder_id"]:
            from uuid import UUID as PyUUID
            builder_stmt = select(Builder.name).where(
                Builder.id == PyUUID(jwt_info["builder_id"])
            )
            builder_result = await db.execute(builder_stmt)
            builder_name = builder_result.scalar_one_or_none()
            if builder_name:
                clinic_name = builder_name
        if not clinic_name:
            from src.models.clinic_config_table import ClinicConfigTable
            stmt = select(ClinicConfigTable).where(
                ClinicConfigTable.tenant_id == str(tenant.tenant_id)
            )
            result = await db.execute(stmt)
            cfg = result.scalar_one_or_none()
            if cfg and cfg.identity:
                clinic_name = cfg.identity.get("clinic_name")
    except Exception:
        pass

    # Resolve routing destination: builder email vs central support inbox.
    # Rules:
    #  - financeiro tickets always go to central
    #  - clinics whose tenant has a builder_id AND that builder is active
    #    AND there's a role=builder user record for that builder → route to
    #    that user's email; else central fallback
    recipient_email = settings.support_email_to
    routed_to = "central"
    routed_to_builder_id = None
    routing_reason = "default"

    if body.tipo == "financeiro":
        routing_reason = "financeiro ticket"
    else:
        tenant_row = (
            await db.execute(select(Tenant).where(Tenant.id == tenant.tenant_id))
        ).scalar_one_or_none()

        if tenant_row is None or tenant_row.builder_id is None:
            routing_reason = "no builder linked"
        else:
            builder_status = (
                await db.execute(
                    select(Builder.status).where(Builder.id == tenant_row.builder_id)
                )
            ).scalar_one_or_none()

            if builder_status is None:
                routing_reason = "builder not found"
            elif builder_status != "active":
                routing_reason = "builder inactive"
            else:
                builder_user = (
                    await db.execute(
                        select(User).where(
                            User.builder_id == tenant_row.builder_id,
                            cast(User.role, String) == "builder",
                        )
                    )
                ).scalar_one_or_none()

                if builder_user is None:
                    routing_reason = "no builder user found"
                else:
                    recipient_email = builder_user.email
                    routed_to = "builder"
                    routed_to_builder_id = tenant_row.builder_id
                    routing_reason = "active builder"

    if routed_to == "builder":
        logger.info(
            "Support ticket routing: builder (%s → %s)",
            routing_reason, recipient_email,
        )
    else:
        logger.info(
            "Support ticket routing: central (%s → %s)",
            routing_reason, recipient_email,
        )

    # Persist ticket to DB
    ticket = SupportTicket(
        tenant_id=tenant.tenant_id,
        user_id=tenant.user_id,
        clinic_name=clinic_name,
        email=sender_email,
        tipo=body.tipo,
        mensagem=body.mensagem,
        filename=body.filename,
        file_type=body.file_type,
        status="open",
        routed_to=routed_to,
        routed_to_builder_id=routed_to_builder_id,
    )
    db.add(ticket)
    await db.flush()
    await db.commit()
    ticket_id = ticket.id
    logger.info("Support ticket #%s created by %s", ticket_id, sender_email)

    # Send email (best-effort, ticket is already saved)
    email_sent = False
    if settings.smtp_host:
        try:
            msg = MIMEMultipart()
            msg["From"] = settings.smtp_user or f"noreply@{settings.smtp_host}"
            msg["To"] = recipient_email
            msg["Subject"] = f"[NeuroStack Suporte] {tipo_label} — {sender_email}"

            email_body = (
                f"Tipo: {tipo_label}\n"
                f"De: {sender_email}\n"
                f"Tenant: {tenant.tenant_id}\n"
                f"User ID: {tenant.user_id}\n"
                f"Role: {tenant.role or 'N/A'}\n"
                f"\n---\n\n"
                f"{body.mensagem}"
            )
            msg.attach(MIMEText(email_body, "plain", "utf-8"))

            if body.file_data and body.filename:
                try:
                    file_bytes = base64.b64decode(body.file_data)
                    attachment = MIMEApplication(file_bytes)
                    attachment.add_header(
                        "Content-Disposition", "attachment", filename=body.filename
                    )
                    if body.file_type:
                        attachment.set_type(body.file_type)
                    msg.attach(attachment)
                except Exception:
                    logger.warning("Failed to decode file attachment, skipping")

            await aiosmtplib.send(
                msg,
                hostname=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_user or None,
                password=settings.smtp_password or None,
                start_tls=True,
            )
            email_sent = True
            logger.info("Support ticket emailed to %s from %s", recipient_email, sender_email)
        except Exception as exc:
            logger.error("Failed to send support email: %s", exc, exc_info=True)
    else:
        logger.warning(
            "SMTP not configured — support ticket #%s from %s not emailed",
            ticket_id, sender_email,
        )

    return SupportTicketResponse(email_sent=email_sent, ticket_id=ticket_id)


@router.get("/tickets", response_model=list[SupportTicketOut])
async def list_support_tickets(
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
):
    """List all support tickets. Restricted to super_admin."""
    if tenant.role not in ("super_admin", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    stmt = select(SupportTicket).order_by(SupportTicket.created_at.desc())
    result = await db.execute(stmt)
    tickets = result.scalars().all()

    return [
        SupportTicketOut(
            id=t.id,
            clinic_id=str(t.tenant_id) if t.tenant_id else None,
            clinic_name=t.clinic_name,
            email=t.email,
            tipo=t.tipo,
            mensagem=t.mensagem,
            filename=t.filename,
            file_type=t.file_type,
            status=t.status,
            created_at=t.created_at.isoformat(),
            routed_to=t.routed_to,
        )
        for t in tickets
    ]


@router.patch("/tickets/{ticket_id}/status")
async def update_ticket_status(
    ticket_id: int,
    body: dict,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
):
    """Update ticket status (open/closed). Restricted to super_admin."""
    if tenant.role not in ("super_admin", "admin"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    new_status = body.get("status")
    if new_status not in ("open", "closed"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Status must be 'open' or 'closed'",
        )

    stmt = select(SupportTicket).where(SupportTicket.id == ticket_id)
    result = await db.execute(stmt)
    ticket = result.scalar_one_or_none()

    if not ticket:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Ticket not found",
        )

    ticket.status = new_status
    await db.commit()

    return {"ok": True, "id": ticket_id, "status": new_status}
