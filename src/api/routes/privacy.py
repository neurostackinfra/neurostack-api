"""
NeuroStack API - Privacy Page Endpoints
- router: HTML page at /privacidade/{id} (legacy, server-rendered)
- public_router: JSON endpoint at /api/public/privacidade/{id} (consumed by React frontend)
"""

import html
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.clinic_config_table import ClinicConfigTable
from src.models.tenant import Tenant
from src.repositories import get_db_session_dependency

router = APIRouter()
public_router = APIRouter()


class ClinicPrivData(BaseModel):
    clinic_id: str
    clinic_name: str
    clinic_legal_name: Optional[str] = None
    clinic_cnpj: Optional[str] = None
    clinic_contact: Optional[str] = None
    dpo_name: Optional[str] = None
    dpo_contact: Optional[str] = None
    lgpd_enabled: Optional[str] = None
    lgpd_custom_link: Optional[str] = None
    slm_training_consent: bool = False


_SHIELD_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 24 24" '
    'fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10"/></svg>'
)


def _shield(size: int = 20, color: str = "#7c3aed") -> str:
    return _SHIELD_SVG.format(size=size, color=color)


def _esc(value: object) -> str:
    if value is None or value == "":
        return "Não informado"
    return html.escape(str(value))


def _esc_or_none(value: object) -> Optional[str]:
    if value is None or str(value).strip() == "":
        return None
    return html.escape(str(value))


def _render_page(
    title: str,
    legal_name: str,
    cnpj: str,
    dpo_name: str,
    dpo_contact: str,
    clinic_contact: str,
    lgpd_message: str,
    slm_training: bool = False,
) -> str:
    from datetime import date
    today = date.today().strftime("%d/%m/%Y")
    shield_header = _shield(20, "#7c3aed")
    shield_footer = _shield(14, "#a78bfa")
    slm_section = ""
    if slm_training:
        slm_section = """
  <section class="section">
    <h2>Adendo Especial: Uso de Dados Anonimizados para Treinamento de IA (SLM)</h2>
    <p>Com o objetivo exclusivo de especializar o atendimento, aumentar a precisão das respostas
    médicas/odontológicas e reduzir os custos operacionais da plataforma, os dados de interações
    e históricos de conversas poderão ser utilizados para o treinamento, ajuste fino (fine-tuning)
    e calibração de Modelos de Linguagem (Small Language Models - SLMs) e motores cognitivos de
    inteligência artificial.</p>
    <p style="margin-top:0.75rem"><strong>Garantia de Anonimização Absoluta:</strong> Para que ocorra este processo
    sem qualquer risco à sua privacidade, todos os dados transitados passam por um rigoroso processo
    de descaracterização e anonimização irreversível. Isso significa que nomes, números de telefone,
    CPFs, documentos, fotos e qualquer outra informação que possa identificar direta ou indiretamente
    o paciente são sumariamente removidos antes de qualquer processamento por estes motores de IA.</p>
    <p style="margin-top:0.75rem">Conforme o artigo 12 da Lei Geral de Proteção de Dados (LGPD - Lei nº 13.709/18), os dados
    anonimizados perdem a natureza de dados pessoais para fins legais. Dessa forma, garantimos que
    o seu histórico de atendimento seja tratado puramente como um padrão estatístico e de linguagem,
    totalmente desvinculado da sua identidade e focado apenas na evolução contínua da qualidade do
    atendimento em saúde.</p>
  </section>"""
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Política de Privacidade — {title}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif; background: #fff; color: #1e293b; line-height: 1.65; font-size: 15px; }}
  a {{ color: inherit; }}
  header {{ background: #f8fafc; border-bottom: 1px solid #e2e8f0; padding: 16px 24px; }}
  .header-inner {{ max-width: 768px; margin: 0 auto; display: flex; align-items: center; gap: 12px; }}
  .header-label {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; color: #64748b; }}
  .header-name {{ font-size: 14px; font-weight: 600; color: #1e293b; }}
  main {{ max-width: 768px; margin: 0 auto; padding: 40px 24px; }}
  h1 {{ font-size: 24px; font-weight: 700; color: #0f172a; margin-bottom: 16px; }}
  .intro-sub {{ color: #64748b; margin-bottom: 16px; }}
  .intro-p {{ color: #334155; margin-bottom: 16px; }}
  section {{ margin-bottom: 32px; }}
  h2 {{ font-size: 17px; font-weight: 600; color: #0f172a; border-bottom: 1px solid #f1f5f9; padding-bottom: 4px; margin-bottom: 10px; }}
  .section-body {{ color: #334155; }}
  .section-body p {{ margin-bottom: 12px; }}
  ul {{ padding-left: 20px; margin: 8px 0 12px; }}
  ul li {{ margin-bottom: 4px; color: #334155; }}
  .info-list {{ list-style: none; padding: 0; margin: 12px 0; }}
  .info-list li {{ margin-bottom: 4px; }}
  footer {{ border-top: 1px solid #e2e8f0; padding: 24px 0 32px; margin-top: 8px; display: flex; align-items: center; gap: 8px; color: #94a3b8; font-size: 12px; }}
  footer strong {{ color: #64748b; }}
</style>
</head>
<body>

<header>
  <div class="header-inner">
    {shield_header}
    <div>
      <div class="header-label">Política de Privacidade</div>
      <div class="header-name">{legal_name}</div>
    </div>
  </div>
</header>

<main>
  <h1>Política de Privacidade do Atendimento por Chatbot</h1>
  <p class="intro-sub">A sua privacidade é importante para nós.</p>
  <p class="intro-p">
    Este chatbot é utilizado para facilitar o atendimento da clínica, incluindo dúvidas iniciais,
    informações sobre serviços, encaminhamento para equipe humana, agendamentos, confirmações de
    contato e registro do histórico de atendimento.
  </p>
  <p class="intro-p">
    Ao utilizar este canal, seus dados pessoais poderão ser tratados para viabilizar o atendimento
    solicitado, melhorar a comunicação com a clínica, cumprir obrigações legais e regulatórias e manter
    a segurança das interações, nos termos da Lei Geral de Proteção de Dados Pessoais, Lei nº 13.709/2018.
  </p>

  <section>
    <h2>1. Quem são os responsáveis pelo tratamento dos dados</h2>
    <div class="section-body">
      <p>O controlador dos seus dados pessoais é a clínica responsável por este atendimento:</p>
      <ul class="info-list">
        <li><strong>{legal_name}</strong></li>
        <li>CNPJ: <strong>{cnpj}</strong></li>
        <li>Contato: <strong>{clinic_contact}</strong></li>
      </ul>
      <p>
        A NeuroStack atua como provedora da tecnologia utilizada neste chatbot, podendo operar dados
        pessoais em nome da clínica para viabilizar o funcionamento da solução, conforme as instruções da
        própria clínica e os limites da legislação aplicável. A LGPD diferencia os papéis de controlador
        e operador no tratamento de dados pessoais.
      </p>
    </div>
  </section>

  <section>
    <h2>2. Quais dados podem ser tratados</h2>
    <div class="section-body">
      <p>Dependendo da sua interação, este canal pode tratar dados como:</p>
      <ul>
        <li>nome</li>
        <li>telefone</li>
        <li>e-mail</li>
        <li>CPF, quando necessário para identificação ou agendamento</li>
        <li>informações fornecidas por você durante a conversa</li>
        <li>dados de atendimento, como data, horário e unidade</li>
        <li>histórico de mensagens trocadas neste canal</li>
      </ul>
      <p>
        Em alguns casos, especialmente em contextos de saúde, podem ser tratados dados pessoais sensíveis,
        como informações relacionadas à sua saúde, quando isso for necessário para o atendimento solicitado.
        A LGPD trata dados de saúde como dados pessoais sensíveis, sujeitos a proteção reforçada.
      </p>
    </div>
  </section>

  <section>
    <h2>3. Para quais finalidades usamos os dados</h2>
    <div class="section-body">
      <ul>
        <li>realizar atendimento inicial e responder suas solicitações</li>
        <li>direcionar você para a equipe da clínica</li>
        <li>realizar ou apoiar agendamentos, confirmações e remarcações</li>
        <li>registrar o histórico do atendimento</li>
        <li>melhorar a qualidade, segurança e organização do serviço</li>
        <li>cumprir obrigações legais, regulatórias e de auditoria</li>
        <li>prevenir fraudes, abusos e uso indevido do canal</li>
      </ul>
    </div>
  </section>

  <section>
    <h2>4. Com quem os dados podem ser compartilhados</h2>
    <div class="section-body">
      <ul>
        <li>profissionais e equipes autorizadas da própria clínica</li>
        <li>prestadores de tecnologia e hospedagem envolvidos na operação do chatbot</li>
        <li>sistemas integrados da clínica, como agenda, CRM ou prontuário, quando aplicável</li>
        <li>autoridades públicas, quando houver obrigação legal ou regulatória</li>
      </ul>
      <p>
        O compartilhamento será realizado apenas quando necessário para as finalidades informadas e dentro
        dos limites da legislação aplicável.
      </p>
    </div>
  </section>

  <section>
    <h2>5. Por quanto tempo os dados são armazenados</h2>
    <div class="section-body">
      <p>
        Os dados serão armazenados pelo tempo necessário para cumprir as finalidades deste atendimento,
        atender exigências legais, regulatórias e contratuais e resguardar direitos da clínica, do
        paciente e dos envolvidos no atendimento.
      </p>
      <p>
        Quando possível e aplicável, os dados serão eliminados, anonimizados ou mantidos de forma segura
        conforme os prazos legais e a necessidade operacional.
      </p>
    </div>
  </section>

  <section>
    <h2>6. Como protegemos os dados</h2>
    <div class="section-body">
      <p>
        Adotamos medidas técnicas e organizacionais de segurança para proteger os dados pessoais contra
        acessos não autorizados, perda, alteração, divulgação indevida ou qualquer forma de tratamento
        inadequado.
      </p>
      <p>
        Ainda assim, nenhum sistema é totalmente livre de riscos. Caso ocorra incidente de segurança com
        risco ou dano relevante, as medidas cabíveis serão adotadas conforme a legislação.
      </p>
    </div>
  </section>

  <section>
    <h2>7. Seus direitos como titular de dados</h2>
    <div class="section-body">
      <p>Nos termos da LGPD, você pode solicitar, quando aplicável:</p>
      <ul>
        <li>confirmação da existência de tratamento</li>
        <li>acesso aos seus dados</li>
        <li>correção de dados incompletos, inexatos ou desatualizados</li>
        <li>anonimização, bloqueio ou eliminação de dados desnecessários, excessivos ou tratados de forma inadequada</li>
        <li>portabilidade, quando cabível</li>
        <li>informação sobre compartilhamentos realizados</li>
        <li>revisão de decisões, quando aplicável</li>
        <li>informações sobre a possibilidade de não fornecer consentimento, quando essa base legal for utilizada</li>
        <li>revogação do consentimento, quando o tratamento depender dele</li>
      </ul>
    </div>
  </section>

  <section>
    <h2>8. Como exercer seus direitos ou tirar dúvidas</h2>
    <div class="section-body">
      <p>Para solicitações relacionadas aos seus dados pessoais, entre em contato com:</p>
      <ul class="info-list">
        <li><strong>{dpo_name}</strong></li>
        <li>Contato/E-mail: <strong>{dpo_contact}</strong></li>
      </ul>
    </div>
  </section>

  <section>
    <h2>9. Sobre o uso deste canal</h2>
    <div class="section-body">
      <p>
        Este chatbot é um canal de apoio ao atendimento. Em situações específicas, você poderá ser
        direcionado para um atendente humano da clínica.
      </p>
      <p>
        Recomendamos que você não envie, por este canal, informações desnecessárias ou excessivas. Caso
        precise de atendimento clínico urgente, procure imediatamente o canal adequado da clínica ou o
        serviço de urgência mais próximo.
      </p>
    </div>
  </section>

  <section>
    <h2>10. Atualizações desta política</h2>
    <div class="section-body">
      <p>
        Esta Política de Privacidade poderá ser atualizada a qualquer momento para refletir melhorias no
        serviço, mudanças operacionais ou exigências legais.
      </p>
      <p><strong>Última atualização: {today}</strong></p>
    </div>
  </section>

{slm_section}
  <footer>
    {shield_footer}
    <span>Página gerada pela plataforma <strong>NeuroStack</strong> — infraestrutura cognitiva para agentes de IA em saúde.</span>
  </footer>
</main>

</body>
</html>
"""


def _render_not_found(clinic_id: str) -> str:
    safe_id = html.escape(clinic_id)
    shield = _shield(32, "#f59e0b")
    return f"""<!DOCTYPE html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Clínica não encontrada</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #fff; display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 100vh; gap: 12px; color: #334155; }}
  p {{ font-weight: 500; text-align: center; }}
</style>
</head>
<body>
  {shield}
  <p>Política de privacidade não encontrada.<br><small style="font-weight:400;color:#94a3b8">{safe_id}</small></p>
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

    slm_training_raw = lgpd.get("builder_slm_training")
    slm_training_flag = slm_training_raw is True or str(slm_training_raw).lower() in ("true", "yes", "1") if slm_training_raw is not None else False

    html_body = _render_page(
        title=_esc(legal_name),
        legal_name=_esc(legal_name),
        cnpj=_esc(lgpd.get("clinic_cnpj")),
        dpo_name=_esc(lgpd.get("dpo_name")),
        dpo_contact=_esc(lgpd.get("dpo_contact")),
        clinic_contact=_esc(lgpd.get("clinic_contact")),
        lgpd_message=_esc(resolved_message),
        slm_training=slm_training_flag,
    )
    return HTMLResponse(content=html_body, status_code=200)


@public_router.get(
    "/privacidade/{clinic_id}",
    response_model=ClinicPrivData,
    include_in_schema=False,
)
async def get_privacy_data(
    clinic_id: str,
    session: AsyncSession = Depends(get_db_session_dependency),
) -> ClinicPrivData:
    """JSON data for the React privacy page frontend."""
    try:
        clinic_uuid = UUID(clinic_id)
    except (ValueError, AttributeError):
        return JSONResponse(
            content={"detail": "Clínica não encontrada"},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    tenant_row = await session.execute(
        select(Tenant).where(Tenant.id == clinic_uuid)
    )
    tenant = tenant_row.scalar_one_or_none()
    if tenant is None:
        return JSONResponse(
            content={"detail": "Clínica não encontrada"},
            status_code=status.HTTP_404_NOT_FOUND,
        )

    config_row = await session.execute(
        select(ClinicConfigTable).where(ClinicConfigTable.tenant_id == clinic_uuid)
    )
    config = config_row.scalar_one_or_none()
    lgpd = (config.lgpd or {}) if config else {}

    lgpd_enabled_val = lgpd.get("lgpd_enabled")
    if isinstance(lgpd_enabled_val, bool):
        lgpd_enabled_str: Optional[str] = str(lgpd_enabled_val).lower()
    elif lgpd_enabled_val is not None:
        lgpd_enabled_str = str(lgpd_enabled_val)
    else:
        lgpd_enabled_str = None

    slm_raw = lgpd.get("builder_slm_training")
    slm_consent = slm_raw is True or str(slm_raw).lower() in ("true", "yes", "1") if slm_raw is not None else False

    return ClinicPrivData(
        clinic_id=str(clinic_uuid),
        clinic_name=tenant.name,
        clinic_legal_name=lgpd.get("clinic_legal_name") or None,
        clinic_cnpj=lgpd.get("clinic_cnpj") or None,
        clinic_contact=lgpd.get("clinic_contact") or None,
        dpo_name=lgpd.get("dpo_name") or None,
        dpo_contact=lgpd.get("dpo_contact") or None,
        lgpd_enabled=lgpd_enabled_str,
        lgpd_custom_link=lgpd.get("lgpd_custom_link") or None,
        slm_training_consent=slm_consent,
    )
