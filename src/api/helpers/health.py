"""
NeuroStack API - Shared Health Helpers
Reusable functions for computing clinic health status, config completeness,
alerts, and time-series data.  Used by both builder and admin routes.
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import cast, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.types import String

from src.api.schemas.builder import BuilderAlertResponse, TraceTimeSeriesPoint
from src.models.clinic_config_table import ClinicConfigTable
from src.models.event import Event
from src.models.tenant import Tenant
from src.services.api_credentials import credential_status

# Short capability names surfaced in the api_error message. Kept local because
# they are UI copy for the master dashboard, not part of the credential policy.
_CAPABILITY_LABEL_WHATSAPP = "API WhatsApp"
_CAPABILITY_LABEL_LLM = "API LLM"
_CAPABILITY_LABEL_STT = "API STT"
_STT_INCOMPLETE_MESSAGE = f"{_CAPABILITY_LABEL_STT} incompleta: falta URL ou modelo"
_STT_CONFIG_ONLY_MESSAGE = (
    f"{_CAPABILITY_LABEL_STT} incompleta: provedor nao homologado"
)
_WHATSAPP_SIGNATURE_MESSAGE = (
    f"{_CAPABILITY_LABEL_WHATSAPP} incompleta: falta o App Secret"
)

# Explicit list of config buckets used for completeness calculation.
# Must stay in sync with ClinicConfigTable fields — update this list when
# adding new required config sections.
CONFIG_COMPLETENESS_BUCKETS = [
    "identity",
    "operations",
    "business_rules",
    "persona",
    "services",
    "professionals",
]


def _compute_health_status(last_trace_at: Optional[datetime], is_active: bool) -> str:
    """Compute 3-tier health status from last trace timestamp and active flag."""
    if not is_active:
        return "critical"
    if last_trace_at is None:
        return "critical"
    now = datetime.now(timezone.utc)
    days_since = (now - last_trace_at).total_seconds() / 86400
    if days_since <= 3:
        return "healthy"
    if days_since <= 7:
        return "warning"
    return "critical"


def _compute_config_completeness(config_row: Optional[ClinicConfigTable]) -> int:
    """Compute config completeness as percentage (0-100)."""
    if config_row is None:
        return 0
    filled = 0
    for bucket_name in CONFIG_COMPLETENESS_BUCKETS:
        val = getattr(config_row, bucket_name, None)
        if val is not None and val != {} and val != []:
            filled += 1
    return int(filled / len(CONFIG_COMPLETENESS_BUCKETS) * 100)


async def _get_time_series(
    tenant_ids: list, session: AsyncSession, days: int = 30
) -> list:
    """Get N-day daily trace counts, filling gaps with 0."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days)

    if not tenant_ids:
        # Return days+1 days of zeros
        return [
            TraceTimeSeriesPoint(
                date=(now - timedelta(days=i)).strftime("%Y-%m-%d"), count=0
            )
            for i in range(days, -1, -1)
        ]

    # Query daily counts
    stmt = (
        select(
            func.date_trunc("day", Event.created_at).label("day"),
            func.count(Event.id).label("count"),
        )
        .where(
            Event.tenant_id.in_(tenant_ids),
            Event.event_type == "message.in",
            Event.created_at >= start,
        )
        .group_by(text("1"))
        .order_by(text("1"))
    )
    result = await session.execute(stmt)
    rows = result.all()

    # Build date -> count map
    count_map: dict = {}
    for row in rows:
        if row.day:
            day_str = row.day.strftime("%Y-%m-%d")
            count_map[day_str] = row.count

    # Fill gaps
    series = []
    for i in range(days, -1, -1):
        d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
        series.append(TraceTimeSeriesPoint(date=d, count=count_map.get(d, 0)))
    return series


async def _compute_alerts(
    builder_id: Optional[UUID], session: AsyncSession
) -> list:
    """Compute 3 alert types from existing data.

    When *builder_id* is ``None`` all clinics are considered (admin mode).
    When a UUID is supplied only that builder's clinics are returned.
    """
    now = datetime.now(timezone.utc)
    seven_days_ago = now - timedelta(days=7)

    # Fetch clinics (including inactive) with config — skip admin HQ tenants
    stmt = (
        select(Tenant)
        .options(selectinload(Tenant.clinic_config_row))
        .where(Tenant.plan_type != "admin")
    )
    if builder_id is not None:
        stmt = stmt.where(Tenant.builder_id == builder_id)
    result = await session.execute(stmt)
    tenants = result.scalars().all()

    if not tenants:
        return []

    tenant_ids = [t.id for t in tenants]

    # Last trace (processed inbound message) per tenant
    last_event_stmt = (
        select(
            Event.tenant_id,
            func.max(Event.created_at).label("last_event_at"),
        )
        .where(
            Event.tenant_id.in_(tenant_ids),
            Event.event_type == "message.in",
        )
        .group_by(Event.tenant_id)
    )
    last_event_result = await session.execute(last_event_stmt)
    last_event_map = {row.tenant_id: row.last_event_at for row in last_event_result}

    alerts: list = []

    for t in tenants:
        # Type 1: deactivated (critical)
        if not t.is_active:
            alerts.append(
                BuilderAlertResponse(
                    type="deactivated",
                    severity="critical",
                    clinic_id=t.id,
                    clinic_name=t.name,
                    message=f"Clinica {t.name} esta desativada",
                    detected_at=now,
                )
            )
            continue  # Don't double-alert on deactivated clinics

        # Type 2: inactivity (warning)
        last_event = last_event_map.get(t.id)
        if last_event is None or last_event < seven_days_ago:
            alerts.append(
                BuilderAlertResponse(
                    type="inactivity",
                    severity="warning",
                    clinic_id=t.id,
                    clinic_name=t.name,
                    message=f"Clinica {t.name} sem traces ha mais de 7 dias",
                    detected_at=now,
                )
            )

        # Type 3: api_error (info). At most one alert per clinic, derived from
        # the shared readiness contract so that meta_cloud/uazapi/zapi clinics
        # are all evaluated by the same rule.
        config_row = t.clinic_config_row
        creds = (config_row.api_credentials or {}) if config_row else {}
        status = credential_status(creds)
        whatsapp_readiness = status["whatsapp_readiness"]
        llm_readiness = status["llm_readiness"]
        stt_readiness = status["stt_readiness"]
        stt_key_set = status["stt_api_key_set"]

        missing_names: list = []
        if whatsapp_readiness == "incomplete":
            missing_names.append(_CAPABILITY_LABEL_WHATSAPP)
        if llm_readiness == "incomplete":
            missing_names.append(_CAPABILITY_LABEL_LLM)

        clauses: list = []
        if missing_names:
            if len(missing_names) == 1:
                joined = missing_names[0]
            else:
                joined = ", ".join(missing_names[:-1]) + " e " + missing_names[-1]
            clauses.append(f"sem {joined}")
        if whatsapp_readiness == "signature_pending":
            clauses.append(_WHATSAPP_SIGNATURE_MESSAGE)
        if stt_key_set and stt_readiness != "active":
            if stt_readiness == "incomplete":
                clauses.append(_STT_INCOMPLETE_MESSAGE)
            elif stt_readiness == "configuration_only":
                clauses.append(_STT_CONFIG_ONLY_MESSAGE)

        if clauses:
            alerts.append(
                BuilderAlertResponse(
                    type="api_error",
                    severity="info",
                    clinic_id=t.id,
                    clinic_name=t.name,
                    message="; ".join(clauses),
                    detected_at=now,
                )
            )

    return alerts
