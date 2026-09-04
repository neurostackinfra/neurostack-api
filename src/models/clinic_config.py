"""
NeuroStack - ClinicConfig (documentação MVP v0.5)
Estrutura de configuração da clínica para SRM, handoff e políticas de preço.
"""

import re
from enum import Enum
from typing import Any, Optional
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


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
    # Scheduling-link stopgap (pre-ERP). When enabled, on positive
    # confirmation the agent delivers `scheduling_link_message` +
    # `scheduling_link_url` verbatim to the patient and creates a pending
    # handoff for human verification. See wish scheduling-link-handoff.
    scheduling_link_enabled: bool = False
    scheduling_link_url: Optional[str] = None
    scheduling_link_message: Optional[str] = None

    @field_validator("scheduling_link_url")
    @classmethod
    def _validate_https_url(cls, v: Optional[str]) -> Optional[str]:
        """Require https:// scheme. Reject http:// and any other scheme.

        The clinic-controlled URL is delivered verbatim to every patient,
        so we treat it as untrusted egress: https-only reduces the
        phishing/downgrade surface. `HttpUrl` alone would accept `http`,
        so we validate explicitly.
        """
        if v is None:
            return v
        if not isinstance(v, str):
            raise ValueError("scheduling_link_url must be a string")
        candidate = v.strip()
        if candidate == "":
            # Treat empty string as "unset" — mirrors the frontend clearing
            # the field via patch. Persist as None so `flatten_config`
            # exposes it consistently.
            return None
        parsed = urlparse(candidate)
        if parsed.scheme != "https":
            raise ValueError(
                "scheduling_link_url must use https:// scheme "
                f"(got {parsed.scheme or 'no scheme'!r})"
            )
        if not parsed.netloc:
            raise ValueError("scheduling_link_url must include a host")
        return candidate


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
    meta_minutos_atendimento: int = 10


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


_VALID_DAY_CODES = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
_BUSINESS_HOUR_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


class BusinessHoursWindow(BaseModel):
    """A single same-day open/close window for one weekday.

    ``open`` and ``close`` are ``HH:MM`` strings in the clinic's local time.
    ``close`` must be strictly after ``open`` (compared as minutes-of-day);
    overnight windows are not representable and must be modelled as a
    "closed" day for the wrap-around.
    """

    open: str
    close: str

    @field_validator("open", "close")
    @classmethod
    def _validate_hhmm(cls, value: str) -> str:
        if not isinstance(value, str) or _BUSINESS_HOUR_PATTERN.fullmatch(value) is None:
            raise ValueError("business hours must use HH:MM (00:00-23:59)")
        return value

    @model_validator(mode="after")
    def _validate_close_after_open(self) -> "BusinessHoursWindow":
        open_minutes = _hhmm_to_minutes(self.open)
        close_minutes = _hhmm_to_minutes(self.close)
        if close_minutes <= open_minutes:
            raise ValueError("business hours close must be strictly greater than open")
        return self


def _hhmm_to_minutes(value: str) -> int:
    hour, minute = value.split(":")
    return int(hour) * 60 + int(minute)


class ClinicOperations(BaseModel):
    """Operations bucket — address, hours, service formats.

    Business hours are a per-weekday map (``mon..sun``) of
    ``BusinessHoursWindow`` entries.  Days omitted from the map are treated
    as closed.  ``working_hours`` remains as the free-text fallback used by
    the prompt pipeline when a structured schedule is not configured.
    """

    full_address: Optional[str] = None
    parking_info: Optional[str] = None
    working_hours: Optional[str] = None
    scheduling_hours: Optional[str] = None
    timezone: Optional[str] = None
    business_hours: Optional[dict[str, BusinessHoursWindow]] = None
    service_format: Optional[str] = None
    online_services_desc: Optional[str] = None
    presential_services_desc: Optional[str] = None

    @field_validator("business_hours", mode="before")
    @classmethod
    def _validate_business_hours(cls, value: Any) -> Any:
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError("business_hours must be a mapping of day → window")
        if not value:
            return None
        normalized: dict[str, Any] = {}
        for day, window in value.items():
            if not isinstance(day, str):
                raise ValueError(
                    "business_hours keys must be mon, tue, wed, thu, fri, sat, or sun"
                )
            key = day.lower()
            if key not in _VALID_DAY_CODES:
                raise ValueError(
                    "business_hours keys must be mon, tue, wed, thu, fri, sat, or sun"
                )
            normalized[key] = window
        return normalized


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
