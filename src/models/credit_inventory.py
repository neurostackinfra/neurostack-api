"""Credit inventory, allocations and media egress deliveries (Group 4a).

Migration 039 (``alembic/versions/20261030_000001_039_credit_inventory.py``)
is the schema authority; these mappers mirror it column for column,
constraint for constraint (``tests/integration/test_credit_orm_ddl_drift_postgres.py``).
All three tables are append-only by trigger.  No mapper declares
``relationship()``, and no existing model gains one.  Amounts are integer
millicredits (1 credit = 1,000 mc); ``units`` are characters (RAG) or bytes
(media, egress).  Nothing in this module reads ``tests/`` at import.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base
from src.models.credit import EFFECT_ID_MAX_LENGTH, SHA256_PATTERN

INVENTORY_CATEGORIES: Final = ("rag", "media")
ALLOCATION_CATEGORIES: Final = ("rag", "media", "egress")
ALLOCATION_KINDS: Final = (
    "baseline",
    "maintenance",
    "reservation",
    "reprocessing",
    "egress",
)
EGRESS_MODES: Final = ("observational", "enforced")
_OPENING_KINDS = "allocation_kind IN ('baseline', 'maintenance')"


def _in(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


class CreditInventoryVersion(Base):
    """One version of one stored object as the credit readback counted it."""

    __tablename__ = "credit_inventory_versions"
    __table_args__ = (
        CheckConstraint(
            _in("category", INVENTORY_CATEGORIES),
            name="ck_credit_inventory_versions_category",
        ),
        CheckConstraint("version > 0", name="ck_credit_inventory_versions_version"),
        CheckConstraint("units >= 0", name="ck_credit_inventory_versions_units"),
        UniqueConstraint(
            "tenant_id",
            "category",
            "object_id",
            "version",
            name="uq_credit_inventory_versions_object_version",
        ),
        Index("ix_credit_inventory_versions_builder", "builder_id", "category"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    category: Mapped[str] = mapped_column(String(10), nullable=False)
    object_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditAllocation(Base):
    """What one accounting window of a builder covers in one category."""

    __tablename__ = "credit_allocations"
    __table_args__ = (
        CheckConstraint(
            _in("category", ALLOCATION_CATEGORIES),
            name="ck_credit_allocations_category",
        ),
        CheckConstraint(
            "(category = 'rag' AND allocation_kind IN "
            "('baseline', 'maintenance', 'reservation', 'reprocessing')) "
            "OR (category = 'media' AND allocation_kind IN "
            "('baseline', 'maintenance', 'reservation')) "
            "OR (category = 'egress' AND allocation_kind = 'egress')",
            name="ck_credit_allocations_kind",
        ),
        CheckConstraint(
            "window_start < window_end", name="ck_credit_allocations_window"
        ),
        CheckConstraint(
            "units >= 0 AND target_mc >= 0 AND charged_mc >= 0 "
            "AND charged_mc <= target_mc",
            name="ck_credit_allocations_amounts",
        ),
        CheckConstraint(
            "(charged_mc = 0 AND effect_id IS NULL) "
            "OR (charged_mc > 0 AND effect_id IS NOT NULL)",
            name="ck_credit_allocations_effect",
        ),
        CheckConstraint(
            "allocation_kind <> 'baseline' OR charged_mc = 0",
            name="ck_credit_allocations_baseline_free",
        ),
        CheckConstraint(
            "length(source_key) > 0", name="ck_credit_allocations_source_key"
        ),
        CheckConstraint(
            f"snapshot_digest IS NULL OR snapshot_digest ~ '{SHA256_PATTERN}'",
            name="ck_credit_allocations_snapshot_digest",
        ).ddl_if(dialect="postgresql"),
        ForeignKeyConstraint(
            ["builder_id", "account_id"],
            ["credit_accounts.builder_id", "credit_accounts.id"],
            ondelete="RESTRICT",
            name="fk_credit_allocations_account",
        ),
        ForeignKeyConstraint(
            ["builder_id", "effect_id"],
            ["credit_effects.builder_id", "credit_effects.effect_id"],
            ondelete="RESTRICT",
            name="fk_credit_allocations_effect",
        ),
        UniqueConstraint(
            "builder_id", "source_key", name="uq_credit_allocations_source"
        ),
        UniqueConstraint(
            "builder_id", "effect_id", name="uq_credit_allocations_effect"
        ),
        Index(
            "ix_credit_allocations_builder_window",
            "builder_id",
            "window_start",
            "category",
        ),
        Index(
            "uq_credit_allocations_window_opening",
            "builder_id",
            "window_start",
            "category",
            unique=True,
            postgresql_where=text(_OPENING_KINDS),
            sqlite_where=text(_OPENING_KINDS),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    window_start: Mapped[date] = mapped_column(Date, nullable=False)
    window_end: Mapped[date] = mapped_column(Date, nullable=False)
    category: Mapped[str] = mapped_column(String(10), nullable=False)
    allocation_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    source_key: Mapped[str] = mapped_column(
        String(EFFECT_ID_MAX_LENGTH), nullable=False
    )
    units: Mapped[int] = mapped_column(BigInteger, nullable=False)
    target_mc: Mapped[int] = mapped_column(BigInteger, nullable=False)
    charged_mc: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effect_id: Mapped[str | None] = mapped_column(
        String(EFFECT_ID_MAX_LENGTH), nullable=True
    )
    snapshot_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditEgressDelivery(Base):
    """One served media download (``delivery_id`` generated by the server)."""

    __tablename__ = "credit_egress_deliveries"
    __table_args__ = (
        CheckConstraint("bytes > 0", name="ck_credit_egress_deliveries_bytes"),
        CheckConstraint(
            _in("mode", EGRESS_MODES), name="ck_credit_egress_deliveries_mode"
        ),
        UniqueConstraint("delivery_id", name="uq_credit_egress_deliveries_delivery"),
        Index(
            "ix_credit_egress_deliveries_builder_window",
            "builder_id",
            "window_start",
            "mode",
        ),
        Index(
            "ix_credit_egress_deliveries_tenant_window",
            "tenant_id",
            "window_start",
            "mode",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    builder_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    media_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    delivery_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    window_start: Mapped[date] = mapped_column(Date, nullable=False)
    bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "ALLOCATION_CATEGORIES",
    "ALLOCATION_KINDS",
    "EGRESS_MODES",
    "INVENTORY_CATEGORIES",
    "CreditAllocation",
    "CreditEgressDelivery",
    "CreditInventoryVersion",
]
