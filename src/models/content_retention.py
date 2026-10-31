"""Credit lifecycle, legal hold and purge rows (credit Group 5, migration 041).

Migration ``alembic/versions/20261030_000003_041_credit_retention.py`` is the
schema authority; these mappers mirror it column for column, constraint for
constraint (``tests/integration/test_credit_orm_ddl_drift_postgres.py``).

* :class:`CreditLifecycleEpisode` -- one exhaustion episode; at most one open
  per builder; its guard trigger lets the binding grace and the suspension
  deadline only shorten and freezes the row once reactivated.
* :class:`CreditLifecycleTransition`, :class:`CreditThresholdAlert` --
  append-only, idempotent by their unique keys.
* :class:`CreditHoldGeneration` (``generation`` grows by exactly one per hold
  event) and :class:`CreditHoldEvent` (the append-only stream).
* :class:`CreditPurgeFence`, :class:`CreditRetentionTombstone`,
  :class:`CreditPurgeJob`, :class:`CreditPurgeResult`,
  :class:`CreditPurgeReceipt` -- append-only.

No mapper declares ``relationship()`` and no existing model gains one.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from src.models.base import Base

LIFECYCLE_TRANSITIONS: Final = (
    "opened",
    "grace_shortened",
    "suspended",
    "deletion_due",
    "reactivated",
)
LIFECYCLE_SOURCES: Final = ("effect", "worker")
THRESHOLDS: Final = (50, 90, 100)
HOLD_EVENT_TYPES: Final = ("hold_requested", "hold_activated", "hold_released")
PURGE_JOB_STATES: Final = ("prepared", "storage_applied", "purge_fenced")
PURGE_OUTCOMES: Final = ("deleted", "held", "failed", "blocked")
PURGE_RECEIPT_STATUSES: Final = ("completed", "partial", "blocked")
SOURCE_KINDS: Final = ("media", "knowledge")
SCOPE_PATTERN: Final = (
    "^tenant:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
_SHA256: Final = "'^[0-9a-f]{64}$'"


def _in(column: str, values: tuple[Any, ...]) -> str:
    return f"{column} IN ({', '.join(repr(value) for value in values)})"


def _postgresql_check(expression: str, *, name: str) -> CheckConstraint:
    """PostgreSQL-only validation (regex, intervals) SQLite cannot evaluate."""

    return CheckConstraint(expression, name=name).ddl_if(dialect="postgresql")


def _scope_check(name: str) -> CheckConstraint:
    return _postgresql_check(
        f"(scope = 'builder' OR scope ~ '{SCOPE_PATTERN}')", name=name
    )


class CreditLifecycleEpisode(Base):
    """One exhaustion episode: ``exhausted_at`` and its two deadlines."""

    __tablename__ = "credit_lifecycle_episodes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["builder_id", "account_id"],
            ["credit_accounts.builder_id", "credit_accounts.id"],
            ondelete="RESTRICT",
            name="fk_credit_lifecycle_episodes_account",
        ),
        CheckConstraint(
            "length(opened_effect_id) > 0",
            name="ck_credit_lifecycle_episodes_effect",
        ),
        _postgresql_check(
            "suspension_due_at >= exhausted_at + interval '72 hours' "
            "AND deletion_due_at > exhausted_at",
            name="ck_credit_lifecycle_episodes_deadlines",
        ),
        CheckConstraint(
            "grace_ends_at IS NULL OR cycle_end_at IS NOT NULL",
            name="ck_credit_lifecycle_episodes_grace",
        ),
        CheckConstraint(
            "reactivated_at IS NULL OR reactivated_at >= exhausted_at",
            name="ck_credit_lifecycle_episodes_reactivated",
        ),
        Index(
            "uq_credit_lifecycle_episodes_open",
            "builder_id",
            unique=True,
            postgresql_where=text("reactivated_at IS NULL"),
            sqlite_where=text("reactivated_at IS NULL"),
        ),
        Index(
            "ix_credit_lifecycle_episodes_due",
            "suspension_due_at",
            postgresql_where=text("reactivated_at IS NULL"),
            sqlite_where=text("reactivated_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    account_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    opened_effect_id: Mapped[str] = mapped_column(String(512), nullable=False)
    exhausted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    cycle_end_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    grace_ends_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    observed_inactive_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_binding_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    suspension_due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    deletion_due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    reactivated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditLifecycleTransition(Base):
    """One recorded transition, idempotent by ``(builder_id, effect_id)``."""

    __tablename__ = "credit_lifecycle_transitions"
    __table_args__ = (
        CheckConstraint(
            _in("transition", LIFECYCLE_TRANSITIONS),
            name="ck_credit_lifecycle_transitions_transition",
        ),
        CheckConstraint(
            _in("source", LIFECYCLE_SOURCES),
            name="ck_credit_lifecycle_transitions_source",
        ),
        CheckConstraint(
            "length(effect_id) > 0", name="ck_credit_lifecycle_transitions_effect"
        ),
        UniqueConstraint(
            "builder_id", "effect_id", name="uq_credit_lifecycle_transitions_effect"
        ),
        Index("ix_credit_lifecycle_transitions_episode", "episode_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    episode_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("credit_lifecycle_episodes.id", ondelete="RESTRICT"),
        nullable=False,
    )
    transition: Mapped[str] = mapped_column(String(30), nullable=False)
    effect_id: Mapped[str] = mapped_column(String(512), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditThresholdAlert(Base):
    """A 50/90/100 alert, once per ``arm_key`` (episode generation, window)."""

    __tablename__ = "credit_threshold_alerts"
    __table_args__ = (
        CheckConstraint(
            _in("threshold", THRESHOLDS),
            name="ck_credit_threshold_alerts_threshold",
        ),
        CheckConstraint(
            "capacity_mc >= 0 AND available_mc >= 0",
            name="ck_credit_threshold_alerts_amounts",
        ),
        UniqueConstraint(
            "builder_id",
            "arm_key",
            "threshold",
            name="uq_credit_threshold_alerts_arm",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    arm_key: Mapped[str] = mapped_column(String(120), nullable=False)
    threshold: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    capacity_mc: Mapped[int] = mapped_column(BigInteger, nullable=False)
    available_mc: Mapped[int] = mapped_column(BigInteger, nullable=False)
    effect_id: Mapped[str] = mapped_column(String(512), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditHoldGeneration(Base):
    """The ``(builder_id, scope)`` row every hold event locks and bumps."""

    __tablename__ = "credit_hold_generations"
    __table_args__ = (
        _scope_check("ck_credit_hold_generations_scope"),
        CheckConstraint(
            "generation >= 0", name="ck_credit_hold_generations_generation"
        ),
    )

    builder_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    scope: Mapped[str] = mapped_column(String(200), primary_key=True)
    generation: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditHoldEvent(Base):
    """One event of the exact stream ``hold_requested|hold_activated|
    hold_released``; ``hold_released`` is also the release receipt."""

    __tablename__ = "credit_hold_events"
    __table_args__ = (
        CheckConstraint(
            _in("event_type", HOLD_EVENT_TYPES), name="ck_credit_hold_events_type"
        ),
        _scope_check("ck_credit_hold_events_scope"),
        CheckConstraint("length(reason_code) > 0", name="ck_credit_hold_events_reason"),
        CheckConstraint("generation > 0", name="ck_credit_hold_events_generation"),
        _postgresql_check(f"digest ~ {_SHA256}", name="ck_credit_hold_events_digest"),
        CheckConstraint(
            "(event_type = 'hold_requested' AND previous_event_id IS NULL) "
            "OR (event_type <> 'hold_requested' AND previous_event_id IS NOT NULL)",
            name="ck_credit_hold_events_chain",
        ),
        CheckConstraint(
            "event_type <> 'hold_released' "
            "OR (requested_by IS NOT NULL AND activated_by IS NOT NULL "
            "AND justification IS NOT NULL AND length(justification) > 0 "
            "AND actor_user_id <> requested_by AND actor_user_id <> activated_by)",
            name="ck_credit_hold_events_release",
        ),
        UniqueConstraint(
            "intent_id", "event_type", name="uq_credit_hold_events_intent_type"
        ),
        UniqueConstraint(
            "authorization_id", name="uq_credit_hold_events_authorization"
        ),
        UniqueConstraint(
            "builder_id",
            "scope",
            "generation",
            name="uq_credit_hold_events_generation",
        ),
        Index("ix_credit_hold_events_scope", "builder_id", "scope", "generation"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    intent_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    builder_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    scope: Mapped[str] = mapped_column(String(200), nullable=False)
    event_type: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(80), nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    previous_event_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("credit_hold_events.id", ondelete="RESTRICT"),
        nullable=True,
    )
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    authorization_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("credit_operational_authorizations.id", ondelete="RESTRICT"),
        nullable=False,
    )
    requested_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    activated_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    justification: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditPurgeFence(Base):
    """The CAS of one purge: one fence per ``(builder_id, scope, generation)``."""

    __tablename__ = "credit_purge_fences"
    __table_args__ = (
        _scope_check("ck_credit_purge_fences_scope"),
        CheckConstraint("generation >= 0", name="ck_credit_purge_fences_generation"),
        UniqueConstraint(
            "builder_id",
            "scope",
            "generation",
            name="uq_credit_purge_fences_generation",
        ),
        UniqueConstraint("operation_id", name="uq_credit_purge_fences_operation"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    scope: Mapped[str] = mapped_column(String(200), nullable=False)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    operation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditRetentionTombstone(Base):
    """What a purge prepared for one object: the token, the observed hold
    generation and the exact stored identities (bytes hash, S3 version,
    Qdrant points) a restore or a purge must match."""

    __tablename__ = "credit_retention_tombstones"
    __table_args__ = (
        _scope_check("ck_credit_retention_tombstones_scope"),
        CheckConstraint(
            _in("source_kind", SOURCE_KINDS),
            name="ck_credit_retention_tombstones_source",
        ),
        _postgresql_check(
            f"content_sha256 ~ {_SHA256}", name="ck_credit_retention_tombstones_sha"
        ),
        CheckConstraint("size_bytes >= 0", name="ck_credit_retention_tombstones_size"),
        CheckConstraint(
            "(s3_object_key IS NULL) = (s3_version_id IS NULL)",
            name="ck_credit_retention_tombstones_copy",
        ),
        CheckConstraint(
            "observed_generation >= 0",
            name="ck_credit_retention_tombstones_generation",
        ),
        UniqueConstraint("token", name="uq_credit_retention_tombstones_token"),
        UniqueConstraint(
            "operation_id",
            "source_kind",
            "source_id",
            name="uq_credit_retention_tombstones_item",
        ),
        Index("ix_credit_retention_tombstones_source", "source_kind", "source_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    token: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    operation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    builder_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    tenant_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    scope: Mapped[str] = mapped_column(String(200), nullable=False)
    source_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    s3_object_key: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    s3_version_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    s3_etag: Mapped[str | None] = mapped_column(String(512), nullable=True)
    qdrant_point_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    observed_generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditPurgeJob(Base):
    """One state reached by a tombstone's job (append-only, one row per
    state): ``prepared`` -> ``storage_applied`` -> ``purge_fenced``."""

    __tablename__ = "credit_purge_jobs"
    __table_args__ = (
        CheckConstraint(
            _in("state", PURGE_JOB_STATES), name="ck_credit_purge_jobs_state"
        ),
        CheckConstraint(
            "(state = 'purge_fenced') = (fence_id IS NOT NULL)",
            name="ck_credit_purge_jobs_fence",
        ),
        UniqueConstraint("token", "state", name="uq_credit_purge_jobs_state"),
        Index("ix_credit_purge_jobs_claim", "state", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    token: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("credit_retention_tombstones.token", ondelete="RESTRICT"),
        nullable=False,
    )
    operation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    fence_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("credit_purge_fences.id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditPurgeResult(Base):
    """One observed outcome of a tombstone; at most one final
    (``deleted|held|blocked``), never a false ``deleted``."""

    __tablename__ = "credit_purge_results"
    __table_args__ = (
        CheckConstraint(
            _in("outcome", PURGE_OUTCOMES), name="ck_credit_purge_results_outcome"
        ),
        CheckConstraint("attempt > 0", name="ck_credit_purge_results_attempt"),
        UniqueConstraint("token", "attempt", name="uq_credit_purge_results_attempt"),
        Index(
            "uq_credit_purge_results_final",
            "token",
            unique=True,
            postgresql_where=text("outcome IN ('deleted', 'held', 'blocked')"),
            sqlite_where=text("outcome IN ('deleted', 'held', 'blocked')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    token: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("credit_retention_tombstones.token", ondelete="RESTRICT"),
        nullable=False,
    )
    operation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(10), nullable=False)
    detail: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CreditPurgeReceipt(Base):
    """The operation's final receipt, written after every observed result."""

    __tablename__ = "credit_purge_receipts"
    __table_args__ = (
        _scope_check("ck_credit_purge_receipts_scope"),
        CheckConstraint(
            _in("status", PURGE_RECEIPT_STATUSES),
            name="ck_credit_purge_receipts_status",
        ),
        CheckConstraint(
            "deleted_count >= 0 AND held_count >= 0 "
            "AND failed_count >= 0 AND blocked_count >= 0",
            name="ck_credit_purge_receipts_counts",
        ),
        _postgresql_check(
            f"digest ~ {_SHA256}", name="ck_credit_purge_receipts_digest"
        ),
    )

    operation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    builder_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    scope: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    deleted_count: Mapped[int] = mapped_column(Integer, nullable=False)
    held_count: Mapped[int] = mapped_column(Integer, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    blocked_count: Mapped[int] = mapped_column(Integer, nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


__all__ = [
    "HOLD_EVENT_TYPES",
    "LIFECYCLE_TRANSITIONS",
    "PURGE_JOB_STATES",
    "PURGE_OUTCOMES",
    "THRESHOLDS",
    "CreditHoldEvent",
    "CreditHoldGeneration",
    "CreditLifecycleEpisode",
    "CreditLifecycleTransition",
    "CreditPurgeFence",
    "CreditPurgeJob",
    "CreditPurgeReceipt",
    "CreditPurgeResult",
    "CreditRetentionTombstone",
    "CreditThresholdAlert",
]
