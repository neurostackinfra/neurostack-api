"""Caller-owned persistence primitives of the credit inventory (Group 4a).

Thin, like :mod:`src.repositories.credit`: every method runs in the caller's
transaction (reads, inserts, flushes; never a commit, rollback or nested
transaction) and holds no business rule -- the rules are
:mod:`src.services.credit_allocations` and the orchestration
:mod:`src.services.credit_inventory`.

Lock order.  These primitives take NO lock of their own.  The readback
(:meth:`CreditInventoryRepository.builder_sources`) and the inventory,
allocation and delivery inserts run after the builder's account lock (step 3
of the order documented in :mod:`src.repositories.credit`), as the inserts
of step 5; the readback is a plain read of ``media_assets`` and
``knowledge_documents`` joined to ``tenants`` (no ``FOR SHARE`` on the
sibling tenants of the builder: taken after the account lock it would
invert the order).  The observational writer reads the same sources under
the triggering tenant's ``FOR SHARE`` only.

Scope.  The builder is the one the caller derived from the authenticated
tenant (``tenants.builder_id`` re-read); every read below is filtered by it
(or by the tenant, for a standalone tenant).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.credit import CreditLot, CreditObservationalEffect
from src.models.credit_inventory import (
    CreditAllocation,
    CreditEgressDelivery,
    CreditInventoryVersion,
)
from src.models.knowledge import KnowledgeDocument
from src.models.media_asset import MediaAsset
from src.models.tenant import Tenant


@dataclass(frozen=True)
class SourceRow:
    """One stored object as the source tables hold it now."""

    category: str
    tenant_id: UUID
    object_id: UUID
    units: int


@dataclass(frozen=True)
class InventoryRow:
    """The latest inventory version of one object."""

    category: str
    tenant_id: UUID
    object_id: UUID
    version: int
    builder_id: UUID | None
    units: int


@dataclass(frozen=True)
class AllocationRow:
    """One allocation of a window, as the window state reads it."""

    category: str
    allocation_kind: str
    source_key: str
    units: int
    target_mc: int
    charged_mc: int


def _uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


class CreditInventoryRepository:
    """Reads and idempotent appends of the inventory, allocation and
    delivery tables (migration 039)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    def _postgresql(self) -> bool:
        return bool(self.session.get_bind().dialect.name == "postgresql")

    def _characters(self) -> Any:
        """``char_length(content)`` (SQLite: ``length``, also characters)."""

        if self._postgresql():
            return func.char_length(KnowledgeDocument.content)
        return func.length(KnowledgeDocument.content)

    # -- the readback (sources) -----------------------------------------------

    async def _sources(self, *conditions: Any) -> list[SourceRow]:
        media = (
            await self.session.execute(
                select(MediaAsset.tenant_id, MediaAsset.id, MediaAsset.size_bytes)
                .join(Tenant, Tenant.id == MediaAsset.tenant_id)
                .where(*conditions)
            )
        ).all()
        documents = (
            await self.session.execute(
                select(
                    KnowledgeDocument.tenant_id,
                    KnowledgeDocument.id,
                    self._characters(),
                )
                .join(Tenant, Tenant.id == KnowledgeDocument.tenant_id)
                .where(*conditions)
            )
        ).all()
        rows = [
            SourceRow("media", _uuid(tenant_id), _uuid(object_id), int(units))
            for tenant_id, object_id, units in media
        ]
        rows.extend(
            SourceRow(
                "rag",
                _uuid(tenant_id),
                _uuid(object_id),
                -1 if units is None else int(units),
            )
            for tenant_id, object_id, units in documents
        )
        return rows

    async def builder_sources(self, builder_id: UUID) -> list[SourceRow]:
        """Every media asset (bytes) and knowledge document (characters) of
        the builder's tenants, read now (a plain read)."""

        return await self._sources(Tenant.builder_id == builder_id)

    async def tenant_sources(self, tenant_id: UUID) -> list[SourceRow]:
        """The same for one (standalone) tenant."""

        return await self._sources(Tenant.id == tenant_id)

    # -- inventory versions ---------------------------------------------------

    async def latest_versions(
        self, builder_id: UUID, tenant_ids: Iterable[UUID]
    ) -> list[InventoryRow]:
        """The latest version (over every builder) of every object ever
        counted under the builder or stored in one of ``tenant_ids``."""

        version = CreditInventoryVersion
        tenants = sorted(set(tenant_ids))
        owned = version.builder_id == builder_id
        scope = owned if not tenants else (owned | version.tenant_id.in_(tenants))
        keys = (
            select(version.tenant_id, version.category, version.object_id)
            .where(scope)
            .distinct()
            .subquery()
        )
        latest = (
            select(
                version.tenant_id,
                version.category,
                version.object_id,
                func.max(version.version).label("latest"),
            )
            .join(
                keys,
                (keys.c.tenant_id == version.tenant_id)
                & (keys.c.category == version.category)
                & (keys.c.object_id == version.object_id),
            )
            .group_by(version.tenant_id, version.category, version.object_id)
            .subquery()
        )
        rows = (
            await self.session.execute(
                select(
                    version.category,
                    version.tenant_id,
                    version.object_id,
                    version.version,
                    version.builder_id,
                    version.units,
                ).join(
                    latest,
                    (latest.c.tenant_id == version.tenant_id)
                    & (latest.c.category == version.category)
                    & (latest.c.object_id == version.object_id)
                    & (latest.c.latest == version.version),
                )
            )
        ).all()
        return [
            InventoryRow(
                category=str(category),
                tenant_id=_uuid(tenant_id),
                object_id=_uuid(object_id),
                version=int(number),
                builder_id=None if owner is None else _uuid(owner),
                units=int(units),
            )
            for category, tenant_id, object_id, number, owner, units in rows
        ]

    async def insert_versions(self, rows: Sequence[Mapping[str, Any]]) -> int:
        """Append versions; a version number already taken (a concurrent
        readback of the same object under another builder: a reparent race)
        is skipped and corrected by the next readback.  Returns how many
        were inserted."""

        inserted = 0
        for row in rows:
            result = await self.session.execute(
                self._insert_version()
                .values(**row)
                .on_conflict_do_nothing()
                .returning(CreditInventoryVersion.id)
            )
            inserted += int(result.first() is not None)
        return inserted

    def _insert_version(self) -> Any:
        if self._postgresql():
            from sqlalchemy.dialects.postgresql import insert as postgresql_insert

            return postgresql_insert(CreditInventoryVersion)
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        return sqlite_insert(CreditInventoryVersion)

    # -- allocations ----------------------------------------------------------

    async def window_allocations(
        self, builder_id: UUID, window_start: date
    ) -> list[AllocationRow]:
        allocation = CreditAllocation
        rows = (
            await self.session.execute(
                select(
                    allocation.category,
                    allocation.allocation_kind,
                    allocation.source_key,
                    allocation.units,
                    allocation.target_mc,
                    allocation.charged_mc,
                ).where(
                    allocation.builder_id == builder_id,
                    allocation.window_start == window_start,
                )
            )
        ).all()
        return [
            AllocationRow(
                category=str(category),
                allocation_kind=str(kind),
                source_key=str(source_key),
                units=int(units),
                target_mc=int(target),
                charged_mc=int(charged),
            )
            for category, kind, source_key, units, target, charged in rows
        ]

    async def allocation_exists(self, builder_id: UUID, source_key: str) -> bool:
        found = (
            await self.session.execute(
                select(CreditAllocation.id).where(
                    CreditAllocation.builder_id == builder_id,
                    CreditAllocation.source_key == source_key,
                )
            )
        ).first()
        return found is not None

    async def insert_allocations(self, rows: Sequence[Mapping[str, Any]]) -> None:
        if rows:
            await self.session.execute(insert(CreditAllocation), list(rows))

    # -- egress deliveries -----------------------------------------------------

    async def delivered_bytes(
        self,
        *,
        window_start: date,
        mode: str,
        builder_id: UUID | None,
        tenant_id: UUID | None = None,
    ) -> int:
        """Bytes served in a window: the builder's (or, with no builder, the
        tenant's) deliveries of ``mode``."""

        delivery = CreditEgressDelivery
        scope = (
            delivery.builder_id == builder_id
            if builder_id is not None
            else delivery.tenant_id == tenant_id
        )
        value = (
            await self.session.execute(
                select(func.coalesce(func.sum(delivery.bytes), 0)).where(
                    scope,
                    delivery.window_start == window_start,
                    delivery.mode == mode,
                )
            )
        ).scalar_one()
        return int(value)

    async def insert_delivery(
        self,
        *,
        tenant_id: UUID,
        builder_id: UUID | None,
        media_id: UUID,
        delivery_id: UUID,
        window_start: date,
        size_bytes: int,
        mode: str,
    ) -> None:
        await self.session.execute(
            insert(CreditEgressDelivery).values(
                id=uuid4(),
                tenant_id=tenant_id,
                builder_id=builder_id,
                media_id=media_id,
                delivery_id=delivery_id,
                window_start=window_start,
                bytes=size_bytes,
                mode=mode,
            )
        )

    # -- reads of the observational writer --------------------------------------

    async def monthly_cycles(self, builder_id: UUID) -> list[tuple[date, date]]:
        """The builder's monthly lots' cycles (a plain read, no lock)."""

        rows = (
            await self.session.execute(
                select(CreditLot.cycle_start, CreditLot.cycle_end).where(
                    CreditLot.builder_id == builder_id,
                    CreditLot.lot_kind == "monthly",
                )
            )
        ).all()
        return [
            (start, end) for start, end in rows if start is not None and end is not None
        ]

    async def observational_effect_recorded(
        self, *, effect_id: str, builder_id: UUID | None, tenant_id: UUID
    ) -> bool:
        """Whether a builder-level (or, standalone, tenant-level)
        observational effect already exists under any of its tenants."""

        effect = CreditObservationalEffect
        scope = (
            effect.builder_id == builder_id
            if builder_id is not None
            else effect.tenant_id == tenant_id
        )
        found = (
            await self.session.execute(
                select(effect.id).where(scope, effect.effect_id == effect_id)
            )
        ).first()
        return found is not None


__all__ = [
    "AllocationRow",
    "CreditInventoryRepository",
    "InventoryRow",
    "SourceRow",
]
