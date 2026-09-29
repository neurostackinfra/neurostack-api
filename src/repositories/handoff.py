"""Tenant-scoped persistence and optimistic CAS for handoff records."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional, Sequence
from uuid import UUID

from sqlalchemy import ColumnElement, and_, case, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.handoff import HandoffRecord, HandoffStatus
from src.repositories.base import BaseRepository


class HandoffCasOutcome(str, Enum):
    """Exhaustive outcomes of a lifecycle compare-and-set."""

    APPLIED = "applied"
    NOT_FOUND = "not_found"
    INVALID_STATE = "invalid_state"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class HandoffCasResult:
    """Typed repository result; only ``applied`` carries a mutated record."""

    outcome: HandoffCasOutcome
    record: HandoffRecord | None = None
    current_status: str | None = None

    @classmethod
    def applied(cls, record: HandoffRecord) -> "HandoffCasResult":
        return cls(HandoffCasOutcome.APPLIED, record=record)

    @classmethod
    def not_found(cls) -> "HandoffCasResult":
        return cls(HandoffCasOutcome.NOT_FOUND)

    @classmethod
    def invalid_state(cls, current_status: str) -> "HandoffCasResult":
        return cls(
            HandoffCasOutcome.INVALID_STATE,
            current_status=current_status,
        )

    @classmethod
    def conflict(cls, current_status: str | None = None) -> "HandoffCasResult":
        return cls(HandoffCasOutcome.CONFLICT, current_status=current_status)


@dataclass(frozen=True)
class HandoffSlaCohortRow:
    """Minimum persisted projection needed by the queue radar and cards."""

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


class HandoffRecordRepository(BaseRepository[HandoffRecord]):
    """Data access for handoffs, including the canonical lifecycle CAS."""

    def __init__(self, session: AsyncSession):
        super().__init__(session, HandoffRecord)

    async def get_for_tenant(
        self,
        tenant_id: UUID,
        handoff_id: UUID,
    ) -> HandoffRecord | None:
        """Return one handoff only when it belongs to ``tenant_id``."""

        statement = (
            select(HandoffRecord)
            .where(
                HandoffRecord.id == handoff_id,
                HandoffRecord.tenant_id == tenant_id,
            )
            .execution_options(populate_existing=True)
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()

    async def find_by_tenant(
        self,
        tenant_id: UUID,
        status: Optional[str] = None,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[HandoffRecord]:
        filters: dict[str, object] = {"tenant_id": tenant_id}
        if status:
            filters["status"] = status
        return await self.find_many(
            skip=skip,
            limit=limit,
            order_by="-created_at",
            **filters,
        )

    async def find_by_session(
        self,
        tenant_id: UUID,
        session_id: UUID,
        status: Optional[str] = None,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[HandoffRecord]:
        filters: dict[str, object] = {
            "tenant_id": tenant_id,
            "session_id": session_id,
        }
        if status:
            filters["status"] = status
        return await self.find_many(
            skip=skip,
            limit=limit,
            order_by="-created_at",
            **filters,
        )

    async def find_active(
        self,
        tenant_id: UUID,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[HandoffRecord]:
        stmt = (
            select(HandoffRecord)
            .where(
                HandoffRecord.tenant_id == tenant_id,
                HandoffRecord.status.in_(
                    [
                        HandoffStatus.PENDING.value,
                        HandoffStatus.ACCEPTED.value,
                        HandoffStatus.IN_PROGRESS.value,
                    ]
                ),
            )
            .order_by(HandoffRecord.urgency.desc(), HandoffRecord.created_at.asc())
        )
        if skip > 0:
            stmt = stmt.offset(skip)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def find_active_for_session(
        self,
        tenant_id: UUID,
        session_id: UUID,
    ) -> HandoffRecord | None:
        result = await self.session.execute(
            select(HandoffRecord)
            .where(
                HandoffRecord.tenant_id == tenant_id,
                HandoffRecord.session_id == session_id,
                HandoffRecord.status.in_(
                    [
                        HandoffStatus.PENDING.value,
                        HandoffStatus.ACCEPTED.value,
                        HandoffStatus.IN_PROGRESS.value,
                    ]
                ),
            )
            .order_by(HandoffRecord.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def find_by_status(
        self,
        tenant_id: UUID,
        status: str,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[HandoffRecord]:
        return await self.find_by_tenant(
            tenant_id=tenant_id,
            status=status,
            skip=skip,
            limit=limit,
        )

    async def find_by_urgency(
        self,
        tenant_id: UUID,
        urgency: str,
        active_only: bool = True,
        skip: int = 0,
        limit: Optional[int] = None,
    ) -> list[HandoffRecord]:
        filters = {"tenant_id": tenant_id, "urgency": urgency}
        if active_only:
            stmt = (
                select(HandoffRecord)
                .where(
                    and_(
                        HandoffRecord.tenant_id == tenant_id,
                        HandoffRecord.urgency == urgency,
                        HandoffRecord.status.in_(
                            [
                                HandoffStatus.PENDING.value,
                                HandoffStatus.ACCEPTED.value,
                                HandoffStatus.IN_PROGRESS.value,
                            ]
                        ),
                    )
                )
                .order_by(HandoffRecord.created_at.asc())
            )
            if skip > 0:
                stmt = stmt.offset(skip)
            if limit is not None:
                stmt = stmt.limit(limit)
            result = await self.session.execute(stmt)
            return list(result.scalars().all())
        return await self.find_many(
            skip=skip,
            limit=limit,
            order_by="created_at",
            **filters,
        )

    async def count_by_tenant(
        self,
        tenant_id: UUID,
        status: Optional[str] = None,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(HandoffRecord)
            .where(HandoffRecord.tenant_id == tenant_id)
        )
        if status:
            stmt = stmt.where(HandoffRecord.status == status)
        result = await self.session.execute(stmt)
        return result.scalar() or 0

    async def stream_sla_cohort(
        self,
        *,
        tenant_id: UUID,
        start_utc: datetime,
        end_exclusive_utc: datetime,
    ) -> AsyncIterator[HandoffSlaCohortRow]:
        """Stream a narrow tenant SLA cohort through one half-open predicate.

        Consumers must derive both queue items and every aggregate from this
        population. The projection deliberately excludes summary, context,
        and first-message payloads so the radar path cannot load private text.
        """

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

        statement = (
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
        result = await self.session.stream(statement)
        async for row in result.tuples():
            yield HandoffSlaCohortRow(
                id=row[0],
                numero_whatsapp=row[1],
                session_id=row[2],
                safety_concern=row[3],
                reason=row[4],
                intencao_detectada=row[5],
                estagio_atual=row[6],
                created_at=row[7],
                data_hora_saida=row[8],
                sla_target_seconds=row[9],
                status=row[10],
            )

    async def current_timestamp(self) -> datetime:
        """Return the current database clock as an aware UTC timestamp."""

        result = await self.session.execute(select(func.now()))
        value = result.scalar_one()
        if not isinstance(value, datetime):
            raise TypeError("Database current timestamp is not a datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    async def compare_and_set(
        self,
        *,
        tenant_id: UUID,
        handoff_id: UUID,
        observed_status: str,
        observed_lifecycle_version: int,
        allowed_statuses: Sequence[str],
        changed_at: datetime,
        values: Mapping[str, Any],
        preconditions: Sequence[ColumnElement[bool]] = (),
    ) -> HandoffCasResult:
        """Apply one domain mutation against the exact observed state/version.

        PostgreSQL re-evaluates the frozen predicate after a row-lock wait. A
        loser therefore returns zero rows and is diagnosed without retrying the
        operation against state produced by the winner.
        """

        if observed_status not in allowed_statuses:
            return HandoffCasResult.invalid_state(observed_status)

        lifecycle_values = dict(values)
        lifecycle_values.update(
            updated_at=changed_at,
            lifecycle_version=HandoffRecord.lifecycle_version + 1,
        )
        statement = (
            update(HandoffRecord)
            .where(
                HandoffRecord.id == handoff_id,
                HandoffRecord.tenant_id == tenant_id,
                HandoffRecord.status == observed_status,
                HandoffRecord.lifecycle_version == observed_lifecycle_version,
                *preconditions,
            )
            .values(**lifecycle_values)
            .returning(HandoffRecord)
            .execution_options(synchronize_session=False)
        )
        applied = (await self.session.execute(statement)).scalar_one_or_none()
        if applied is not None:
            await self.session.refresh(applied)
            return HandoffCasResult.applied(applied)

        current = await self.get_for_tenant(tenant_id, handoff_id)
        if current is None:
            return HandoffCasResult.not_found()
        if (
            current.lifecycle_version != observed_lifecycle_version
            or current.status != observed_status
        ):
            return HandoffCasResult.conflict(current.status)
        return HandoffCasResult.invalid_state(current.status)

    async def update_webhook_delivery(
        self,
        *,
        tenant_id: UUID,
        handoff_id: UUID,
        sent: bool,
        status: str,
        response: Mapping[str, Any] | None,
    ) -> bool:
        """Persist delivery bookkeeping without mutating lifecycle metadata."""

        record = await self.get_for_tenant(tenant_id, handoff_id)
        if record is None:
            return False
        context = dict(record.context or {})
        context["webhook_status"] = status
        record.context = context
        record.webhook_sent = sent
        record.webhook_response = dict(response) if response is not None else None
        await self.session.flush()
        return True
