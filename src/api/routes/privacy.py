"""
NeuroStack API - Privacy Page Endpoint
Public HTML privacy notice rendered from clinic LGPD fields.
"""

import html
from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.clinic_config_table import ClinicConfigTable
from src.models.tenant import Tenant
from src.repositories import get_db_session_dependency

router = APIRouter()


def _esc(value: object) -> str:
    if value is None or value == "":
        return "—"
    return html.escape(str(value))


def _render_page(
    title: str,
    legal_name: str,
    cnpj: str,
    dpo_name: str,
    dpo_contact: str,
    clinic_contact: str,
    lgpd_message: str,
) -> str:
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Política de Privacidade — {title}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; line-height: 1.6; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 1.5rem; }}
  h2 {{ font-size: 1.1rem; color: #555; margin-top: 2rem; }}
  p {{ margin: 0.5rem 0; }}
  hr {{ margin: 1.5rem 0; border: 0; border-top: 1px solid #e5e5e5; }}
  .message {{ white-space: pre-wrap; }}
</style>
</head>
<body>
<h1>Política de Privacidade — {title}</h1>
<p><strong>CNPJ:</strong> {cnpj}</p>
<p><strong>Responsável pelo Tratamento de Dados:</strong> {dpo_name}</p>
<p><strong>Contato DPO:</strong> {dpo_contact}</p>
<p><strong>Contato da Clínica:</strong> {clinic_contact}</p>
</body>
</html>
"""


def _render_not_found(clinic_id: str) -> str:
    safe_id = html.escape(clinic_id)
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<title>Clínica não encontrada</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; max-width: 720px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }}
</style>
</head>
<body>
<h1>Clínica não encontrada</h1>
<p>Nenhuma clínica com o identificador <code>{safe_id}</code> foi localizada.</p>
</body>
</html>
"""


@router.get(
    "/privacidade/{clinic_id}",
    response_class=HTMLResponse,
    include_in_schema=False,
)
async def get_privacy_page(
    clinic_id: str,
    session: AsyncSession = Depends(get_db_session_dependency),
) -> HTMLResponse:
    """Public privacy notice page for a clinic."""
    try:
        clinic_uuid = UUID(clinic_id)
    except (ValueError, AttributeError):
        return HTMLResponse(
            content=_render_not_found(clinic_id),
            status_code=status.HTTP_404_NOT_FOUND,
        )

    stmt = select(ClinicConfigTable).where(ClinicConfigTable.tenant_id == clinic_uuid)
    result = await session.execute(stmt)
    row = result.scalar_one_or_none()

    if row is None:
        return HTMLResponse(
            content=_render_not_found(str(clinic_id)),
            status_code=status.HTTP_404_NOT_FOUND,
        )

    lgpd = row.lgpd or {}
    business_rules = row.business_rules or {}
    agent_language = business_rules.get("agent_language") or "pt-BR"

    tenant_row = await session.execute(
        select(Tenant.name).where(Tenant.id == clinic_uuid)
    )
    tenant_name = tenant_row.scalar_one_or_none()

    legal_name = lgpd.get("clinic_legal_name") or tenant_name or "Clínica"

    # Build the resolved privacy link (same logic as webhook)
    from src.config.settings import settings
    custom_link = (lgpd.get("lgpd_custom_link") or "").strip()
    if custom_link:
        privacy_link = custom_link
    else:
        base_url = (settings.public_base_url or "").rstrip("/")
        privacy_link = f"{base_url}/privacidade/{clinic_id}" if base_url else f"/privacidade/{clinic_id}"

    # Pick PT or EN message based on agent language
    if agent_language == "en":
        raw_message = lgpd.get("lgpd_message_en") or ""
    else:
        raw_message = lgpd.get("lgpd_message") or ""
    # Replace placeholders in message before escaping
    resolved_message = raw_message.replace("{{lgpd_link}}", privacy_link)
    resolved_message = resolved_message.replace("{{clinic_name}}", legal_name)
    resolved_message = resolved_message.replace("[Nome da Clínica]", legal_name)

    html_body = _render_page(
        title=_esc(legal_name),
        legal_name=_esc(legal_name),
        cnpj=_esc(lgpd.get("clinic_cnpj")),
        dpo_name=_esc(lgpd.get("dpo_name")),
        dpo_contact=_esc(lgpd.get("dpo_contact")),
        clinic_contact=_esc(lgpd.get("clinic_contact")),
        lgpd_message=_esc(resolved_message),
    )
    return HTMLResponse(content=html_body, status_code=200)
