"""Persistence boundary for the canonical PostgreSQL handoff lifecycle."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.clinic_config_table import ClinicConfigTable
from src.models.handoff import HandoffRecord, HandoffStatus
from src.models.session import Session as SessionRecord
from src.models.tenant import Tenant
from src.repositories.base import BaseRepository


class HandoffLookupOutcome(str, Enum):
    """Exhaustive outcomes for a handoff lookup."""

    FOUND = "found"
    NOT_FOUND = "not_found"


@dataclass(frozen=True)
class HandoffLookupResult:
    """Typed lookup result; callers never infer absence from exceptions."""

    outcome: HandoffLookupOutcome
    record: HandoffRecord | None = None


class HandoffCasOutcome(str, Enum):
    """Exhaustive outcomes for a single compare-and-set attempt."""

    APPLIED = "applied"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class HandoffCasResult:
    """Result of one non-retrying lifecycle compare-and-set."""

    outcome: HandoffCasOutcome
    record: HandoffRecord | None = None


@dataclass(frozen=True)
class HandoffSlaConfiguration:
    """Raw tenant configuration used to capture an immutable SLA snapshot."""

    configured_minutes: object
    source: str


@dataclass(frozen=True)
class HandoffSlaCohortRow:
    """Narrow persisted projection needed by the queue and SLA radar."""

    id: UUID
    numero_whatsapp: str | None
    session_id: UUID
    safety_concern: bool | None
    reason: str
    intencao_detectada: str | None
    estagio_atual: str | None
    created_at: datetime
    data_hora_saida: datetime | None
    sla_target_seconds: int | None
    status: str
    # Listing intentionally never selects the persisted private first message.
    primeira_linha_mensagem: str | None = None


class HandoffRecordRepository(BaseRepository[HandoffRecord]):
    """Tenant-scoped reads and non-retrying lifecycle CAS operations."""

    _ACTIVE_STATUSES = (
        HandoffStatus.PENDING.value,
        HandoffStatus.ACCEPTED.value,
        HandoffStatus.IN_PROGRESS.value,
    )

    def __init__(self, session: AsyncSession):
        super().__init__(session, HandoffRecord)

    async def lookup(
        self,
        tenant_id: UUID,
        handoff_id: UUID,
    ) -> HandoffLookupResult:
        """Look up one handoff without crossing the tenant boundary."""
        result = await self.session.execute(
            select(HandoffRecord).where(
                HandoffRecord.id == handoff_id,
                HandoffRecord.tenant_id == tenant_id,
            )
        )
        record = result.scalar_one_or_none()
        if record is None:
            return HandoffLookupResult(HandoffLookupOutcome.NOT_FOUND)
        return HandoffLookupResult(HandoffLookupOutcome.FOUND, record)

    async def session_belongs_to_tenant(
        self,
        tenant_id: UUID,
        session_id: UUID,
    ) -> bool:
        """Return whether the session exists inside the requested tenant."""
        result = await self.session.execute(
            select(SessionRecord.id)
            .where(
                SessionRecord.id == session_id,
                SessionRecord.tenant_id == tenant_id,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None

    async def find_by_tenant(
        self,
        tenant_id: UUID,
        status: Optional[str] = None,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[HandoffRecord]:
        """List tenant handoffs newest first."""
        statement = select(HandoffRecord).where(HandoffRecord.tenant_id == tenant_id)
        if status is not None:
            statement = statement.where(HandoffRecord.status == status)
        statement = statement.order_by(
            HandoffRecord.created_at.desc(),
            HandoffRecord.id.desc(),
        )
        if skip > 0:
            statement = statement.offset(skip)
        if limit is not None:
            statement = statement.limit(limit)
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    async def find_by_session(
        self,
        tenant_id: UUID,
        session_id: UUID,
        status: Optional[str] = None,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[HandoffRecord]:
        """List tenant/session handoffs newest first."""
        statement = select(HandoffRecord).where(
            HandoffRecord.tenant_id == tenant_id,
            HandoffRecord.session_id == session_id,
        )
        if status is not None:
            statement = statement.where(HandoffRecord.status == status)
        statement = statement.order_by(
            HandoffRecord.created_at.desc(),
            HandoffRecord.id.desc(),
        )
        if skip > 0:
            statement = statement.offset(skip)
        if limit is not None:
            statement = statement.limit(limit)
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    async def find_active(
        self,
        tenant_id: UUID,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[HandoffRecord]:
        """List active handoffs within one tenant."""
        statement = select(HandoffRecord).where(
            HandoffRecord.tenant_id == tenant_id,
            HandoffRecord.status.in_(self._ACTIVE_STATUSES),
        )
        statement = statement.order_by(
            HandoffRecord.urgency.desc(),
            HandoffRecord.created_at.asc(),
        )
        if skip > 0:
            statement = statement.offset(skip)
        if limit is not None:
            statement = statement.limit(limit)
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    async def find_active_for_session(
        self,
        tenant_id: UUID,
        session_id: UUID,
    ) -> HandoffLookupResult:
        """Find the newest active row for a tenant/session pair."""
        statement = select(HandoffRecord).where(
            HandoffRecord.tenant_id == tenant_id,
            HandoffRecord.session_id == session_id,
            HandoffRecord.status.in_(self._ACTIVE_STATUSES),
        )
        statement = statement.order_by(
            HandoffRecord.created_at.desc(),
            HandoffRecord.id.desc(),
        ).limit(1)
        result = await self.session.execute(statement)
        record = result.scalar_one_or_none()
        if record is None:
            return HandoffLookupResult(HandoffLookupOutcome.NOT_FOUND)
        return HandoffLookupResult(HandoffLookupOutcome.FOUND, record)

    async def find_by_status(
        self,
        tenant_id: UUID,
        status: str,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[HandoffRecord]:
        """List tenant handoffs with one status."""
        return await self.find_by_tenant(tenant_id, status, skip, limit)

    async def find_by_urgency(
        self,
        tenant_id: UUID,
        urgency: str,
        active_only: bool = True,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[HandoffRecord]:
        """List tenant handoffs with one urgency."""
        statement = select(HandoffRecord).where(
            HandoffRecord.tenant_id == tenant_id,
            HandoffRecord.urgency == urgency,
        )
        if active_only:
            statement = statement.where(HandoffRecord.status.in_(self._ACTIVE_STATUSES))
        statement = statement.order_by(HandoffRecord.created_at.asc())
        if skip > 0:
            statement = statement.offset(skip)
        if limit is not None:
            statement = statement.limit(limit)
        result = await self.session.execute(statement)
        return list(result.scalars().all())

    async def count_by_tenant(
        self,
        tenant_id: UUID,
        status: Optional[str] = None,
    ) -> int:
        """Count tenant handoffs, optionally by status."""
        statement = (
            select(func.count())
            .select_from(HandoffRecord)
            .where(HandoffRecord.tenant_id == tenant_id)
        )
        if status is not None:
            statement = statement.where(HandoffRecord.status == status)
        result = await self.session.execute(statement)
        return result.scalar() or 0

    def _sla_cohort_statement(
        self,
        *,
        tenant_id: UUID,
        start_utc: datetime,
        end_exclusive_utc: datetime,
    ) -> Any:
        """Build the bounded, tenant-scoped narrow projection statement."""
        context_safety = HandoffRecord.context["safety_concern"]
        if self.session.get_bind().dialect.name == "sqlite":
            safety_concern = case(
                (
                    func.json_type(HandoffRecord.context, "$.safety_concern") == "true",
                    True,
                ),
                (
                    func.json_type(HandoffRecord.context, "$.safety_concern")
                    == "false",
                    False,
                ),
                else_=None,
            ).label("safety_concern")
        else:
            safety_concern = case(
                (
                    func.jsonb_typeof(context_safety) == "boolean",
                    context_safety.as_boolean(),
                ),
                else_=None,
            ).label("safety_concern")
        return (
            select(
                HandoffRecord.id,
                HandoffRecord.numero_whatsapp,
                HandoffRecord.session_id,
                safety_concern,
                HandoffRecord.reason,
                HandoffRecord.intencao_detectada,
                HandoffRecord.estagio_atual,
                HandoffRecord.created_at,
                HandoffRecord.data_hora_saida,
                HandoffRecord.sla_target_seconds,
                HandoffRecord.status,
            )
            .where(
                HandoffRecord.tenant_id == tenant_id,
                HandoffRecord.created_at >= start_utc,
                HandoffRecord.created_at < end_exclusive_utc,
            )
            .order_by(HandoffRecord.created_at.asc(), HandoffRecord.id.asc())
        )

    async def stream_sla_cohort(
        self,
        *,
        tenant_id: UUID,
        start_utc: datetime,
        end_exclusive_utc: datetime,
    ) -> AsyncIterator[HandoffSlaCohortRow]:
        """Stream one bounded SLA cohort without hydrating full ORM entities."""
        statement = self._sla_cohort_statement(
            tenant_id=tenant_id,
            start_utc=start_utc,
            end_exclusive_utc=end_exclusive_utc,
        )
        result = await self.session.stream(statement)
        async for row in result:
            yield HandoffSlaCohortRow(*row)

    async def load_clinic_timezone(self, tenant_id: UUID) -> object:
        """Load the raw clinic timezone without interpreting legacy JSON."""
        result = await self.session.execute(
            select(ClinicConfigTable.operations).where(
                ClinicConfigTable.tenant_id == tenant_id
            )
        )
        operations = result.scalar_one_or_none()
        if not isinstance(operations, dict):
            return None
        return operations.get("timezone")

    async def load_sla_configuration(
        self,
        tenant_id: UUID,
    ) -> HandoffSlaConfiguration:
        """Load canonical then legacy SLA configuration in the current UoW."""
        result = await self.session.execute(
            select(ClinicConfigTable.handoff, Tenant.config)
            .select_from(Tenant)
            .outerjoin(
                ClinicConfigTable,
                ClinicConfigTable.tenant_id == Tenant.id,
            )
            .where(Tenant.id == tenant_id)
        )
        row = result.one_or_none()
        if row is None:
            return HandoffSlaConfiguration(None, "tenant_missing")

        canonical = row.handoff if isinstance(row.handoff, dict) else {}
        if "meta_minutos_atendimento" in canonical:
            return HandoffSlaConfiguration(
                canonical.get("meta_minutos_atendimento"),
                "clinic_configs",
            )

        tenant_config = row.config if isinstance(row.config, dict) else {}
        legacy_clinic = tenant_config.get("clinic_config")
        legacy_handoff = (
            legacy_clinic.get("handoff") if isinstance(legacy_clinic, dict) else None
        )
        if isinstance(legacy_handoff, dict):
            return HandoffSlaConfiguration(
                legacy_handoff.get("meta_minutos_atendimento"),
                "tenants.config",
            )
        return HandoffSlaConfiguration(None, "missing")

    async def create_record(self, record: HandoffRecord) -> HandoffRecord:
        """Insert a canonical row; transaction ownership remains with caller."""
        self.session.add(record)
        await self.session.flush()
        await self.session.refresh(record)
        return record

    async def current_timestamp(self) -> datetime:
        """Return the database clock normalized to UTC within the current UoW."""
        result = await self.session.execute(select(func.now()))
        value = result.scalar_one()
        if not isinstance(value, datetime):
            raise TypeError("Database current timestamp is not a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    async def accept_handoff(
        self,
        *,
        tenant_id: UUID,
        handoff_id: UUID,
        observed_status: str,
        observed_lifecycle_version: int,
        agent_id: Optional[str],
        changed_at: datetime,
    ) -> HandoffCasResult:
        """Move one observed pending row to accepted."""
        return await self._compare_and_set(
            tenant_id=tenant_id,
            handoff_id=handoff_id,
            observed_status=observed_status,
            observed_lifecycle_version=observed_lifecycle_version,
            changed_at=changed_at,
            values={
                "status": HandoffStatus.ACCEPTED.value,
                "assigned_to": agent_id,
            },
        )

    async def confirm_handoff(
        self,
        *,
        tenant_id: UUID,
        handoff_id: UUID,
        observed_status: str,
        observed_lifecycle_version: int,
        agent_id: Optional[str],
        confirmed_at: datetime,
    ) -> HandoffCasResult:
        """Write first response exactly once and keep the row in progress."""
        values: dict[str, Any] = {
            "status": HandoffStatus.IN_PROGRESS.value,
            "data_hora_saida": confirmed_at,
        }
        if agent_id is not None:
            values["assigned_to"] = agent_id
        return await self._compare_and_set(
            tenant_id=tenant_id,
            handoff_id=handoff_id,
            observed_status=observed_status,
            observed_lifecycle_version=observed_lifecycle_version,
            changed_at=confirmed_at,
            values=values,
            extra_conditions=(HandoffRecord.data_hora_saida.is_(None),),
        )

    async def complete_handoff(
        self,
        *,
        tenant_id: UUID,
        handoff_id: UUID,
        observed_status: str,
        observed_lifecycle_version: int,
        notes: Optional[str],
        changed_at: datetime,
    ) -> HandoffCasResult:
        """Complete an observed, previously confirmed row."""
        return await self._compare_and_set(
            tenant_id=tenant_id,
            handoff_id=handoff_id,
            observed_status=observed_status,
            observed_lifecycle_version=observed_lifecycle_version,
            changed_at=changed_at,
            values={
                "status": HandoffStatus.COMPLETED.value,
                "resolved_at": changed_at,
                "resolution_notes": notes,
            },
            extra_conditions=(HandoffRecord.data_hora_saida.is_not(None),),
        )

    async def cancel_handoff(
        self,
        *,
        tenant_id: UUID,
        handoff_id: UUID,
        observed_status: str,
        observed_lifecycle_version: int,
        notes: Optional[str],
        changed_at: datetime,
    ) -> HandoffCasResult:
        """Cancel one observed active row."""
        return await self._compare_and_set(
            tenant_id=tenant_id,
            handoff_id=handoff_id,
            observed_status=observed_status,
            observed_lifecycle_version=observed_lifecycle_version,
            changed_at=changed_at,
            values={
                "status": HandoffStatus.CANCELLED.value,
                "resolved_at": changed_at,
                "resolution_notes": notes,
            },
        )

    async def timeout_handoff(
        self,
        *,
        tenant_id: UUID,
        handoff_id: UUID,
        observed_status: str,
        observed_lifecycle_version: int,
        changed_at: datetime,
    ) -> HandoffCasResult:
        """Time out one observed active row."""
        return await self._compare_and_set(
            tenant_id=tenant_id,
            handoff_id=handoff_id,
            observed_status=observed_status,
            observed_lifecycle_version=observed_lifecycle_version,
            changed_at=changed_at,
            values={
                "status": HandoffStatus.TIMEOUT.value,
                "resolved_at": changed_at,
            },
        )

    async def update_verification(
        self,
        *,
        tenant_id: UUID,
        handoff_id: UUID,
        observed_status: str,
        observed_lifecycle_version: int,
        context: dict[str, Any],
        changed_at: datetime,
    ) -> HandoffCasResult:
        """CAS an allowed enrichment while preserving lifecycle status."""
        return await self._compare_and_set(
            tenant_id=tenant_id,
            handoff_id=handoff_id,
            observed_status=observed_status,
            observed_lifecycle_version=observed_lifecycle_version,
            changed_at=changed_at,
            values={"context": context},
        )

    async def record_webhook_delivery(
        self,
        *,
        tenant_id: UUID,
        handoff_id: UUID,
        sent: bool,
        response: dict[str, Any],
    ) -> bool:
        """Persist sanitized delivery bookkeeping without touching lifecycle CAS."""
        result = await self.session.execute(
            update(HandoffRecord)
            .where(
                HandoffRecord.id == handoff_id,
                HandoffRecord.tenant_id == tenant_id,
            )
            .values(webhook_sent=sent, webhook_response=response)
            .execution_options(synchronize_session=False)
        )
        await self.session.flush()
        return getattr(result, "rowcount", 0) == 1

    async def _compare_and_set(
        self,
        *,
        tenant_id: UUID,
        handoff_id: UUID,
        observed_status: str,
        observed_lifecycle_version: int,
        changed_at: datetime,
        values: dict[str, Any],
        extra_conditions: tuple[Any, ...] = (),
    ) -> HandoffCasResult:
        """Execute exactly one UPDATE ... RETURNING against the observed snapshot."""
        statement = (
            update(HandoffRecord)
            .where(
                HandoffRecord.id == handoff_id,
                HandoffRecord.tenant_id == tenant_id,
                HandoffRecord.status == observed_status,
                HandoffRecord.lifecycle_version == observed_lifecycle_version,
                *extra_conditions,
            )
            .values(
                **values,
                lifecycle_version=HandoffRecord.lifecycle_version + 1,
                updated_at=changed_at,
            )
            .returning(HandoffRecord)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        result = await self.session.execute(statement)
        record = result.scalar_one_or_none()
        if record is None:
            # PostgreSQL re-evaluates the predicate after a lock wait. A zero-row
            # result is the final conflict outcome; deliberately do not re-read.
            return HandoffCasResult(HandoffCasOutcome.CONFLICT)
        await self.session.flush()
        return HandoffCasResult(HandoffCasOutcome.APPLIED, record)
