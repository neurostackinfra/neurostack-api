"""Credit consumer inbox of ``billing.fact.v1`` facts and revision gaps.

Named ``credit_fact_inbox`` (not ``billing_*``) so it stays outside the ASAAS
producer set, which never imports credit code.  Migration 038 is the schema
authority; the consumer's ack is the existing ``BillingFactAck``
(``billing_fact_acks``), whose ``inbox_event_id`` is this row's ``id``.

* ``credit_fact_inbox`` is append-only by trigger.  ``inbox_seq`` is a
  ``BIGINT GENERATED ALWAYS AS IDENTITY``: its maximum is the readiness
  ``inbox_high_watermark`` (0 when empty).  A fact is unique per
  ``(producer, event_id, schema_version)`` and per
  ``(producer, aggregate_kind, aggregate_id, financial_revision)``.
* ``credit_revision_gaps`` rows are never deleted; ``closed_at`` is set once.

Nothing in this module reads ``tests/`` at import.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Identity,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base

INBOX_OUTCOMES: Final = ("accepted", "rejected")
AGGREGATE_KINDS: Final = ("subscription_cycle", "topup_order")
_AGGREGATE_KIND_CHECK: Final = "aggregate_kind IN ('subscription_cycle', 'topup_order')"
_SHA256: Final = "'^[0-9a-f]{64}$'"


class CreditFactInbox(Base):
    """One fact the consumer accepted or rejected (rejected = problem in the
    fact itself; it still gets this row and a ``rejected`` ack)."""

    __tablename__ = "credit_fact_inbox"
    __table_args__ = (
        CheckConstraint(
            _AGGREGATE_KIND_CHECK, name="ck_credit_fact_inbox_aggregate_kind"
        ),
        CheckConstraint(
            "financial_revision > 0", name="ck_credit_fact_inbox_financial_revision"
        ),
        CheckConstraint(
            "outcome IN ('accepted', 'rejected')", name="ck_credit_fact_inbox_outcome"
        ),
        CheckConstraint(
            f"fact_sha256 ~ {_SHA256} AND bundle_digest ~ {_SHA256} "
            f"AND schema_digest ~ {_SHA256}",
            name="ck_credit_fact_inbox_digests",
        ).ddl_if(dialect="postgresql"),
        UniqueConstraint("inbox_seq", name="uq_credit_fact_inbox_seq"),
        UniqueConstraint(
            "producer",
            "event_id",
            "schema_version",
            name="uq_credit_fact_inbox_event",
        ),
        UniqueConstraint(
            "producer",
            "aggregate_kind",
            "aggregate_id",
            "financial_revision",
            name="uq_credit_fact_inbox_revision",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    inbox_seq: Mapped[int] = mapped_column(
        BigInteger, Identity(always=True), nullable=False
    )
    producer: Mapped[str] = mapped_column(String(80), nullable=False)
    event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    aggregate_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    financial_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    fact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditRevisionGap(Base):
    """A ``financial_revision`` jump: opened with the applied snapshot,
    closed once the reconciler proves the missing revisions."""

    __tablename__ = "credit_revision_gaps"
    __table_args__ = (
        CheckConstraint(
            _AGGREGATE_KIND_CHECK, name="ck_credit_revision_gaps_aggregate_kind"
        ),
        CheckConstraint(
            "first_missing_revision > 0", name="ck_credit_revision_gaps_revision"
        ),
        CheckConstraint(
            "closed_at IS NULL OR closed_at >= opened_at",
            name="ck_credit_revision_gaps_closed",
        ),
    )

    aggregate_kind: Mapped[str] = mapped_column(String(30), primary_key=True)
    aggregate_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    first_missing_revision: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=False
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
