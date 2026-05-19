"""
NeuroStack - ClinicConfig (documentação MVP v0.5)
Estrutura de configuração da clínica para SRM, handoff e políticas de preço.
"""

from enum import Enum
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class PricingDisclosureMode(str, Enum):
    """Modo de divulgação de preços (doc MVP v0.5)."""
    CONSULT_ONLY = "consult_only"
    RANGE_ALLOWED = "range_allowed"
    EXACT_ALLOWED = "exact_allowed"
    NO_PRICES = "no_prices"


class HandoffDefaultMode(str, Enum):
    """Modo padrão de handoff (doc MVP v0.5)."""
    WHATSAPP_HUMAN_TAKEOVER = "whatsapp_human_takeover"
    CRM_TASK_CREATE = "crm_task_create"


class HandoffSLA(str, Enum):
    """SLA por setor (doc MVP v0.5)."""
    IMEDIATO = "imediato"
    PRIORITARIO = "prioritario"
    ROTINA = "rotina"


class ClinicPersona(BaseModel):
    """Persona da clínica/agente (doc MVP v0.5)."""
    clinic_display_name: Optional[str] = None
    agent_display_name: Optional[str] = None
    use_personal_name: bool = False


class ClinicScheduling(BaseModel):
    """Configuração de agendamento (doc MVP v0.5)."""
    has_scheduling_agent: bool = False
    hours_rag_key: Optional[str] = None


class ClinicPricing(BaseModel):
    """Configuração de preços (doc MVP v0.5)."""
    disclosure_mode: PricingDisclosureMode = PricingDisclosureMode.CONSULT_ONLY
    range_table_available: bool = False
    exact_table_available: bool = False
    range_format: Optional[str] = None
    special_conditions_note_enabled: bool = False


class ClinicPostCare(BaseModel):
    """Configuração pós-atendimento (doc MVP v0.5)."""
    standard_instructions_rag_key: Optional[str] = None
    emergency_warning_text_enabled: bool = True


class SectorDefaults(BaseModel):
    """SLA por setor (doc MVP v0.5)."""
    agendamento: HandoffSLA = HandoffSLA.PRIORITARIO
    financeiro: HandoffSLA = HandoffSLA.ROTINA
    administrativo: HandoffSLA = HandoffSLA.ROTINA
    gerencia: HandoffSLA = HandoffSLA.PRIORITARIO
    clinico: HandoffSLA = HandoffSLA.PRIORITARIO
    urgencia: HandoffSLA = HandoffSLA.IMEDIATO
    fidelizacao: HandoffSLA = HandoffSLA.ROTINA


class ClinicHandoff(BaseModel):
    """Configuração de handoff (doc MVP v0.5)."""
    default_mode: HandoffDefaultMode = HandoffDefaultMode.WHATSAPP_HUMAN_TAKEOVER
    sector_defaults: SectorDefaults = Field(default_factory=SectorDefaults)


class ClinicIdentity(BaseModel):
    """Identity bucket — admin panel fields for clinic/agent branding."""
    clinic_name: Optional[str] = None
    main_domain: Optional[str] = None
    agent_name: Optional[str] = None
    agent_presentation: Optional[str] = None
    ai_disclosure: Optional[str] = None
    use_emojis: bool = False
    clinic_history: Optional[str] = None
    clinic_specialties: Optional[list[str]] = None


class ClinicOperations(BaseModel):
    """Operations bucket — address, hours, service formats."""
    full_address: Optional[str] = None
    parking_info: Optional[str] = None
    working_hours: Optional[str] = None
    scheduling_hours: Optional[str] = None
    service_format: Optional[str] = None
    online_services_desc: Optional[str] = None
    presential_services_desc: Optional[str] = None


class ClinicBusinessRules(BaseModel):
    """Business rules bucket — insurance, pricing, language policies."""
    accepts_insurance: bool = False
    insurance_list: Optional[list[str]] = None
    initial_consultation: Optional[str] = None
    provide_prices: bool = False
    differentials: Optional[str] = None
    language_policy: Optional[str] = None
    language_policy_custom: Optional[str] = None
    agent_language: Optional[str] = None  # "pt-BR" | "en" | None → defaults to "pt-BR"


class ClinicLGPD(BaseModel):
    """LGPD bucket — data-protection / compliance fields."""
    lgpd_enabled: bool = False
    lgpd_message: Optional[str] = None
    lgpd_message_en: Optional[str] = None
    lgpd_custom_link: Optional[str] = None
    clinic_legal_name: Optional[str] = None
    clinic_cnpj: Optional[str] = None
    clinic_contact: Optional[str] = None
    dpo_name: Optional[str] = None
    dpo_contact: Optional[str] = None


class ClinicConfig(BaseModel):
    """
    Configuração da clínica (documentação MVP v0.5).
    Armazenada na tabela SQL clinic_configs (modelo fixo); fallback em tenants.config["clinic_config"].
    Usada no pipeline/CTX para SRM e handoff.
    """

    clinic_id: Optional[UUID] = None
    version: int = 1
    persona: ClinicPersona = Field(default_factory=ClinicPersona)
    scheduling: ClinicScheduling = Field(default_factory=ClinicScheduling)
    pricing: ClinicPricing = Field(default_factory=ClinicPricing)
    post_care: ClinicPostCare = Field(default_factory=ClinicPostCare)
    handoff: ClinicHandoff = Field(default_factory=ClinicHandoff)
    # Admin-panel buckets (migration 005)
    identity: ClinicIdentity = Field(default_factory=ClinicIdentity)
    operations: ClinicOperations = Field(default_factory=ClinicOperations)
    business_rules: ClinicBusinessRules = Field(default_factory=ClinicBusinessRules)
    lgpd: ClinicLGPD = Field(default_factory=ClinicLGPD)
    professionals: list[dict[str, Any]] = Field(default_factory=list)
    services: list[dict[str, Any]] = Field(default_factory=list)

    def get_sla_for_sector(self, sector: str) -> str:
        """Retorna o SLA para um setor (doc defaults)."""
        sd = self.handoff.sector_defaults
        return getattr(sd, sector, HandoffSLA.ROTINA).value
