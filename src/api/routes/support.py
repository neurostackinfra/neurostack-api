"""
NeuroStack API - Support Endpoint
Receive support tickets and send them via email (SMTP).
"""

import base64
import logging
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from src.api.middleware.auth import TenantContext, get_tenant_context
from src.config.settings import settings

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


TIPO_LABELS = {
    "solicitacao": "Solicitação",
    "feedback": "Feedback",
    "financeiro": "Financeiro",
}


@router.post("/tickets", response_model=SupportTicketResponse)
async def create_support_ticket(
    body: SupportTicketRequest,
    request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
):
    # Extract email from JWT payload (stored during auth middleware)
    sender_email = "unknown"
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
            sender_email = payload.get("email", "unknown")
        except Exception:
            pass

    tipo_label = TIPO_LABELS.get(body.tipo, body.tipo)

    # Check if SMTP is configured
    if not settings.smtp_host:
        logger.warning(
            "SMTP not configured — support ticket from %s not emailed (tipo=%s)",
            sender_email, body.tipo,
        )
        return SupportTicketResponse(email_sent=False)

    # Build email
    msg = MIMEMultipart()
    msg["From"] = settings.smtp_user or f"noreply@{settings.smtp_host}"
    msg["To"] = settings.support_email_to
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

    # Attach file if present
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

    # Send via SMTP
    try:
        import aiosmtplib
        await aiosmtplib.send(
            msg,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user or None,
            password=settings.smtp_password or None,
            start_tls=True,
        )
        logger.info("Support ticket emailed to %s from %s", settings.support_email_to, sender_email)
        return SupportTicketResponse(email_sent=True)
    except Exception as exc:
        logger.error("Failed to send support email: %s", exc, exc_info=True)
        return SupportTicketResponse(email_sent=False)
