"""Persisted billing aggregates and financial idempotency boundaries."""

from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models.base import Base, TimestampMixin

if TYPE_CHECKING:
    from src.models.builder import Builder
    from src.models.transaction import Transaction


_PROVIDERS = "'abacatepay', 'asaas'"
_LIVE_CHECKOUT_STATES = (
    "'creating', 'active', 'paid_pending_subscription', "
    "'reconciliation_required', 'terminal_pending_readback'"
)
_SHA256_CHECK = "^[0-9a-f]{64}$"
_EXTERNAL_ID_CHECK = "^[A-Za-z0-9._:-]+$"


def _postgresql_check(expression: str, *, name: str) -> CheckConstraint:
    """Keep PostgreSQL-only validation visible without breaking SQLite tests."""

    return CheckConstraint(expression, name=name).ddl_if(dialect="postgresql")


class BillingSubscription(Base, TimestampMixin):
    """Canonical active-provider binding and historical entitlement snapshot."""

    __tablename__ = "billing_subscriptions"
    __table_args__ = (
        CheckConstraint(
            f"provider IS NULL OR provider IN ({_PROVIDERS})",
            name="ck_billing_subscriptions_provider",
        ),
        CheckConstraint(
            "char_length(external_subscription_id) BETWEEN 1 AND 160 "
            "AND external_subscription_id ~ '^[A-Za-z0-9._:-]+$'",
            name="ck_billing_subscriptions_external_id_length",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "length(plan) BETWEEN 1 AND 100",
            name="ck_billing_subscriptions_plan_length",
        ),
        CheckConstraint(
            "(provider = 'asaas' AND billing_cycle = 'monthly') OR "
            "(provider = 'abacatepay' AND billing_cycle IN ('monthly', 'annual')) "
            "OR (provider IS NULL AND billing_cycle IN ('monthly', 'annual'))",
            name="ck_billing_subscriptions_cycle",
        ),
        CheckConstraint(
            "status IN ('active', 'inactive', 'cancelled', "
            "'active_linked', 'readback_required', "
            "'non_billable_pending_readback', "
            "'legacy_cancel_pending', 'legacy_cancelled', "
            "'provider_unknown', 'manual_intervention')",
            name="ck_billing_subscriptions_status",
        ),
        CheckConstraint(
            "(status IN ('active', 'active_linked', 'readback_required') "
            "AND is_active = true) OR (status NOT IN "
            "('active', 'active_linked', 'readback_required') "
            "AND is_active = false)",
            name="ck_billing_subscriptions_active_status",
        ),
        CheckConstraint(
            "(provider IS NULL AND is_active = false "
            "AND status IN ('legacy_cancel_pending', 'legacy_cancelled', "
            "'provider_unknown', 'manual_intervention')) OR "
            "(provider IS NOT NULL AND status NOT IN "
            "('legacy_cancel_pending', 'legacy_cancelled', "
            "'provider_unknown', 'manual_intervention'))",
            name="ck_billing_subscriptions_unknown_provider",
        ),
        CheckConstraint(
            "amount_cents IS NULL OR amount_cents >= 0",
            name="ck_billing_subscriptions_amount_cents",
        ),
        UniqueConstraint(
            "provider",
            "external_subscription_id",
            name="uq_billing_subscriptions_provider_external",
        ),
        UniqueConstraint(
            "builder_id",
            "id",
            name="uq_billing_subscriptions_builder_id_id",
        ),
        UniqueConstraint(
            "builder_id",
            "provider",
            "external_subscription_id",
            name="uq_billing_subscriptions_builder_provider_external",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("builders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    external_subscription_id: Mapped[str] = mapped_column(String(160), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    plan: Mapped[str] = mapped_column(String(100), nullable=False)
    billing_cycle: Mapped[str] = mapped_column(
        String(20), nullable=False, default="monthly", server_default="monthly"
    )
    coupon_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    catalog_version_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    offer_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    amount_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        String(40), nullable=False, default="active", server_default="active"
    )

    builder: Mapped["Builder"] = relationship(
        "Builder", back_populates="billing_subscriptions"
    )
    payment_projections: Mapped[list["BillingPaymentProjection"]] = relationship(
        "BillingPaymentProjection", back_populates="subscription", viewonly=True
    )
    outbox_events: Mapped[list["BillingOutboxEvent"]] = relationship(
        "BillingOutboxEvent", back_populates="subscription", viewonly=True
    )
    financial_cycles: Mapped[list["BillingSubscriptionCycle"]] = relationship(
        "BillingSubscriptionCycle", viewonly=True
    )


class BillingTopupOrder(Base, TimestampMixin):
    """Builder-owned detached top-up financial aggregate."""

    __tablename__ = "billing_topup_orders"
    __table_args__ = (
        CheckConstraint("provider = 'asaas'", name="ck_billing_topup_orders_provider"),
        CheckConstraint(
            "payment_method IN ('credit_card', 'pix')",
            name="ck_billing_topup_orders_payment_method",
        ),
        CheckConstraint(
            "state IN ('pending', 'settled', 'partially_reversed', 'reversed', "
            "'restored', 'failed', 'cancelled', 'quarantined')",
            name="ck_billing_topup_orders_state",
        ),
        CheckConstraint(
            "amount_cents > 0 AND credit_grant_mc > 0",
            name="ck_billing_topup_orders_positive_amounts",
        ),
        CheckConstraint(
            "financial_revision >= 0",
            name="ck_billing_topup_orders_financial_revision",
        ),
        CheckConstraint(
            "settled_cents >= 0 AND reversed_cents >= 0 AND restored_cents >= 0 "
            "AND restored_cents <= reversed_cents "
            "AND reversed_cents <= settled_cents "
            "AND settled_cents <= amount_cents",
            name="ck_billing_topup_orders_financial_totals",
        ),
        _postgresql_check(
            "jsonb_typeof(pricing_snapshot) = 'object' "
            "AND pricing_snapshot - ARRAY['catalog_version_id', 'offer_id', "
            "'amount_cents', 'credit_grant_mc', 'payment_method', 'lines'] "
            "= '{}'::jsonb "
            "AND billing_snapshot_has_no_pii(pricing_snapshot) "
            "AND (NOT pricing_snapshot ? 'catalog_version_id' OR "
            "pricing_snapshot ->> 'catalog_version_id' = catalog_version_id) "
            "AND (NOT pricing_snapshot ? 'offer_id' OR "
            "pricing_snapshot ->> 'offer_id' = offer_id) "
            "AND (NOT pricing_snapshot ? 'amount_cents' OR "
            "(pricing_snapshot ->> 'amount_cents')::bigint = amount_cents) "
            "AND (NOT pricing_snapshot ? 'credit_grant_mc' OR "
            "(pricing_snapshot ->> 'credit_grant_mc')::bigint = credit_grant_mc) "
            "AND (NOT pricing_snapshot ? 'payment_method' OR "
            "pricing_snapshot ->> 'payment_method' = payment_method)",
            name="ck_billing_topup_orders_snapshot_allowlist",
        ),
        UniqueConstraint(
            "provider",
            "external_order_id",
            name="uq_billing_topup_orders_provider_external",
        ),
        UniqueConstraint(
            "builder_id", "id", name="uq_billing_topup_orders_builder_id_id"
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("builders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(
        String(20), nullable=False, default="asaas", server_default="asaas"
    )
    external_order_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    catalog_version_id: Mapped[str] = mapped_column(String(80), nullable=False)
    offer_id: Mapped[str] = mapped_column(String(100), nullable=False)
    payment_method: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credit_grant_mc: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pricing_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(
        String(40), nullable=False, default="pending", server_default="pending"
    )
    financial_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    settled_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    reversed_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    restored_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    # The immutable pricing snapshot remains the commercial source of truth.
    # This separate snapshot records only the producer's cumulative financial
    # view, so a fact can be rebuilt without consulting a projection label.
    financial_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'")
    )

    builder: Mapped["Builder"] = relationship(
        "Builder", back_populates="billing_topup_orders"
    )
    checkout_sessions: Mapped[list["BillingCheckoutSession"]] = relationship(
        "BillingCheckoutSession", back_populates="topup_order", viewonly=True
    )
    payment_projections: Mapped[list["BillingPaymentProjection"]] = relationship(
        "BillingPaymentProjection", back_populates="topup_order", viewonly=True
    )
    outbox_events: Mapped[list["BillingOutboxEvent"]] = relationship(
        "BillingOutboxEvent", back_populates="topup_order", viewonly=True
    )


class BillingSubscriptionCycle(Base, TimestampMixin):
    """One immutable-priced, cumulatively accounted monthly payment cycle."""

    __tablename__ = "billing_subscription_cycles"
    __table_args__ = (
        CheckConstraint(
            "provider = 'asaas'",
            name="ck_billing_subscription_cycles_provider",
        ),
        CheckConstraint(
            "state IN ('pending', 'settled', 'reversed', 'restored', 'quarantined')",
            name="ck_billing_subscription_cycles_state",
        ),
        CheckConstraint(
            "financial_revision >= 0",
            name="ck_billing_subscription_cycles_financial_revision",
        ),
        CheckConstraint(
            "amount_cents > 0 AND credit_grant_mc >= 0",
            name="ck_billing_subscription_cycles_positive_amounts",
        ),
        CheckConstraint(
            "settled_cents >= 0 AND reversed_cents >= 0 AND restored_cents >= 0 "
            "AND restored_cents <= reversed_cents "
            "AND reversed_cents <= settled_cents "
            "AND settled_cents <= amount_cents",
            name="ck_billing_subscription_cycles_financial_totals",
        ),
        CheckConstraint(
            "cycle_start < cycle_end",
            name="ck_billing_subscription_cycles_dates",
        ),
        ForeignKeyConstraint(
            ["builder_id", "subscription_id"],
            ["billing_subscriptions.builder_id", "billing_subscriptions.id"],
            ondelete="RESTRICT",
            name="fk_billing_subscription_cycles_builder_subscription",
        ),
        UniqueConstraint(
            "provider",
            "payment_id",
            name="uq_billing_subscription_cycles_provider_payment",
        ),
        UniqueConstraint(
            "builder_id",
            "id",
            name="uq_billing_subscription_cycles_builder_id_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("builders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    subscription_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(
        String(20), nullable=False, default="asaas", server_default="asaas"
    )
    payment_id: Mapped[str] = mapped_column(String(160), nullable=False)
    catalog_version_id: Mapped[str] = mapped_column(String(80), nullable=False)
    offer_id: Mapped[str] = mapped_column(String(100), nullable=False)
    cycle_start: Mapped[date] = mapped_column(Date, nullable=False)
    cycle_end: Mapped[date] = mapped_column(Date, nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credit_grant_mc: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pricing_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    financial_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    financial_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    settled_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    reversed_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    restored_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )


class BillingCheckoutSession(Base, TimestampMixin):
    """Persisted checkout attempt created before any provider egress."""

    __tablename__ = "billing_checkout_sessions"
    __table_args__ = (
        CheckConstraint(
            "intent_kind IN ('subscription', 'topup')",
            name="ck_billing_checkout_sessions_intent_kind",
        ),
        CheckConstraint(
            "provider = 'asaas'",
            name="ck_billing_checkout_sessions_provider",
        ),
        _postgresql_check(
            f"(external_checkout_id IS NULL OR external_checkout_id ~ "
            f"'{_EXTERNAL_ID_CHECK}') AND "
            "(resulting_external_subscription_id IS NULL OR "
            "(intent_kind = 'subscription' AND "
            f"resulting_external_subscription_id ~ '{_EXTERNAL_ID_CHECK}'))",
            name="ck_billing_checkout_sessions_external_ids",
        ),
        CheckConstraint(
            "state IN ('creating', 'active', 'paid_pending_subscription', "
            "'reconciliation_required', 'terminal_pending_readback', 'completed', "
            "'abandoned', 'cancelled', 'expired', 'failed')",
            name="ck_billing_checkout_sessions_state",
        ),
        CheckConstraint(
            "attempt_no > 0 AND amount_cents > 0 " "AND reconciliation_attempts >= 0",
            name="ck_billing_checkout_sessions_attempt_amount",
        ),
        CheckConstraint(
            "(intent_kind = 'subscription' AND topup_order_id IS NULL) OR "
            "(intent_kind = 'topup' AND topup_order_id IS NOT NULL "
            "AND intent_id = topup_order_id)",
            name="ck_billing_checkout_sessions_intent_target",
        ),
        _postgresql_check(
            "jsonb_typeof(pricing_snapshot) = 'object' "
            "AND pricing_snapshot - ARRAY['catalog_version_id', 'offer_id', "
            "'amount_cents', 'credit_grant_mc', 'included_clinics', "
            "'additional_clinics', 'billing_cycle', 'coupon_code', "
            "'payment_method', 'lines'] = '{}'::jsonb "
            "AND billing_snapshot_has_no_pii(pricing_snapshot) "
            "AND (NOT pricing_snapshot ? 'catalog_version_id' OR "
            "pricing_snapshot ->> 'catalog_version_id' = catalog_version_id) "
            "AND (NOT pricing_snapshot ? 'offer_id' OR "
            "pricing_snapshot ->> 'offer_id' = offer_id) "
            "AND (NOT pricing_snapshot ? 'amount_cents' OR "
            "(pricing_snapshot ->> 'amount_cents')::bigint = amount_cents)",
            name="ck_billing_checkout_sessions_snapshot_allowlist",
        ),
        CheckConstraint(
            "(canary_environment IS NULL AND canary_slot IS NULL) OR "
            "(canary_environment IS NOT NULL AND canary_environment IN "
            "('development', 'staging', 'production', 'test') AND "
            "canary_slot IN ('subscription', 'topup'))",
            name="ck_billing_checkout_sessions_canary_pair",
        ),
        _postgresql_check(
            "canary_slot IS NULL OR (provider = 'asaas' AND attempt_no = 1 AND "
            "catalog_version_id = 'payment-2026-10-03' AND "
            "((canary_slot = 'subscription' AND intent_kind = 'subscription' "
            "AND topup_order_id IS NULL AND "
            "next_due_date IS NOT NULL AND "
            "offer_id = 'plan_plus_monthly_v1' AND amount_cents = 99900 AND "
            "pricing_snapshot ->> 'catalog_version_id' = 'payment-2026-10-03' AND "
            "pricing_snapshot ->> 'offer_id' = 'plan_plus_monthly_v1' AND "
            "pricing_snapshot ->> 'amount_cents' = '99900' AND "
            "pricing_snapshot ->> 'payment_method' = 'credit_card' AND "
            "pricing_snapshot ->> 'billing_cycle' = 'monthly' AND "
            "(pricing_snapshot = jsonb_build_object("
            "'catalog_version_id', 'payment-2026-10-03', "
            "'offer_id', 'plan_plus_monthly_v1', 'amount_cents', 99900, "
            "'credit_grant_mc', 5000000, 'included_clinics', 1, "
            "'billing_cycle', 'monthly', 'payment_method', 'credit_card', "
            "'lines', jsonb_build_array(jsonb_build_object("
            "'line_id', 'plan', 'offer_id', 'plan_plus_monthly_v1', "
            "'kind', 'plan', 'quantity', 1, 'unit_amount_cents', 99900, "
            "'line_amount_cents', 99900, 'credit_grant_mc', 5000000, "
            "'settled_cents', 0, 'reversed_cents', 0, 'restored_cents', 0, "
            "'line_financial_state', 'pending'))) OR "
            "pricing_snapshot = jsonb_build_object("
            "'catalog_version_id', 'payment-2026-10-03', "
            "'offer_id', 'plan_plus_monthly_v1', 'amount_cents', 99900, "
            "'credit_grant_mc', 5000, 'included_clinics', 1, "
            "'billing_cycle', 'monthly', 'payment_method', 'credit_card', "
            "'lines', jsonb_build_array(jsonb_build_object("
            "'line_id', 'plan', 'offer_id', 'plan_plus_monthly_v1', "
            "'kind', 'plan', 'quantity', 1, 'unit_amount_cents', 99900, "
            "'line_amount_cents', 99900, 'credit_grant_mc', 5000, "
            "'settled_cents', 0, 'reversed_cents', 0, 'restored_cents', 0, "
            "'line_financial_state', 'pending'))))) OR "
            "(canary_slot = 'topup' AND intent_kind = 'topup' AND "
            "topup_order_id IS NOT NULL AND "
            "intent_id = topup_order_id AND "
            "offer_id = 'package_credits_500_v1' AND amount_cents = 10000 AND "
            "pricing_snapshot ->> 'catalog_version_id' = 'payment-2026-10-03' AND "
            "pricing_snapshot ->> 'offer_id' = 'package_credits_500_v1' AND "
            "pricing_snapshot ->> 'amount_cents' = '10000' AND "
            "pricing_snapshot ->> 'credit_grant_mc' = '500000')))",
            name="ck_billing_checkout_sessions_canary_exact",
        ),
        ForeignKeyConstraint(
            ["builder_id", "topup_order_id"],
            ["billing_topup_orders.builder_id", "billing_topup_orders.id"],
            ondelete="RESTRICT",
            name="fk_billing_checkout_sessions_builder_topup",
        ),
        ForeignKeyConstraint(
            ["builder_id", "provider", "resulting_external_subscription_id"],
            [
                "billing_subscriptions.builder_id",
                "billing_subscriptions.provider",
                "billing_subscriptions.external_subscription_id",
            ],
            ondelete="RESTRICT",
            name="fk_billing_checkout_sessions_result_subscription",
        ),
        UniqueConstraint(
            "provider",
            "external_checkout_id",
            name="uq_billing_checkout_sessions_provider_external",
        ),
        UniqueConstraint(
            "builder_id",
            "intent_kind",
            "intent_id",
            "attempt_no",
            name="uq_billing_checkout_sessions_attempt",
        ),
        UniqueConstraint(
            "builder_id",
            "id",
            name="uq_billing_checkout_sessions_builder_id_id",
        ),
        UniqueConstraint(
            "builder_id",
            "id",
            "canary_environment",
            "canary_slot",
            name="uq_billing_checkout_sessions_canary_identity",
        ),
        Index(
            "uq_billing_checkout_sessions_live_intent",
            "builder_id",
            "intent_kind",
            "intent_id",
            unique=True,
            postgresql_where=text(f"state IN ({_LIVE_CHECKOUT_STATES})"),
            sqlite_where=text(f"state IN ({_LIVE_CHECKOUT_STATES})"),
        ),
        Index(
            "uq_billing_checkout_sessions_canary_slot",
            "canary_environment",
            "canary_slot",
            unique=True,
            postgresql_where=text("canary_environment IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("builders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    intent_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    intent_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    topup_order_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(
        String(20), nullable=False, default="asaas", server_default="asaas"
    )
    external_checkout_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    resulting_external_subscription_id: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )
    state: Mapped[str] = mapped_column(
        String(40), nullable=False, default="creating", server_default="creating"
    )
    catalog_version_id: Mapped[str] = mapped_column(String(80), nullable=False)
    offer_id: Mapped[str] = mapped_column(String(100), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    pricing_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    canary_environment: Mapped[str | None] = mapped_column(String(20), nullable=True)
    canary_slot: Mapped[str | None] = mapped_column(String(20), nullable=True)
    next_due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    reconciliation_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    builder: Mapped["Builder"] = relationship(
        "Builder", back_populates="billing_checkout_sessions"
    )
    topup_order: Mapped["BillingTopupOrder | None"] = relationship(
        "BillingTopupOrder", back_populates="checkout_sessions", viewonly=True
    )
    payment_projections: Mapped[list["BillingPaymentProjection"]] = relationship(
        "BillingPaymentProjection",
        primaryjoin="and_(BillingCheckoutSession.builder_id == "
        "BillingPaymentProjection.builder_id, BillingCheckoutSession.id == "
        "BillingPaymentProjection.checkout_session_id)",
        foreign_keys="[BillingPaymentProjection.builder_id, "
        "BillingPaymentProjection.checkout_session_id]",
        back_populates="checkout_session",
        viewonly=True,
    )


class BillingWebhookDelivery(Base):
    """Minimal non-PII webhook envelope and delivery idempotency key."""

    __tablename__ = "billing_webhook_deliveries"
    __table_args__ = (
        CheckConstraint(
            f"provider IN ({_PROVIDERS})",
            name="ck_billing_webhook_deliveries_provider",
        ),
        _postgresql_check(
            "event_id ~ '^[A-Za-z0-9._:-]+$' "
            "AND event_name ~ '^[A-Z][A-Z0-9_]{0,99}$'",
            name="ck_billing_webhook_deliveries_event_fields",
        ),
        CheckConstraint(
            "resource_kind IS NULL OR resource_kind IN "
            "('checkout_session', 'subscription', 'topup_order', 'payment')",
            name="ck_billing_webhook_deliveries_resource_kind",
        ),
        _postgresql_check(
            "resource_id IS NULL OR resource_id ~ '^[A-Za-z0-9._:-]+$'",
            name="ck_billing_webhook_deliveries_resource_id",
        ),
        CheckConstraint(
            "body_bytes >= 2 AND body_bytes <= 262144",
            name="ck_billing_webhook_deliveries_body_bytes",
        ),
        _postgresql_check(
            f"body_sha256 ~ '{_SHA256_CHECK}'",
            name="ck_billing_webhook_deliveries_sha256",
        ),
        CheckConstraint(
            "status IN ('received', 'processed', 'unknown', 'readback_required')",
            name="ck_billing_webhook_deliveries_status",
        ),
        UniqueConstraint(
            "provider", "event_id", name="uq_billing_webhook_deliveries_event"
        ),
        UniqueConstraint(
            "builder_id",
            "id",
            name="uq_billing_webhook_deliveries_builder_id_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("builders.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    event_id: Mapped[str] = mapped_column(String(160), nullable=False)
    event_name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    resource_kind: Mapped[str | None] = mapped_column(String(30), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    body_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    body_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="received", server_default="received"
    )

    payment_effects: Mapped[list["BillingPaymentEffect"]] = relationship(
        "BillingPaymentEffect", back_populates="delivery", viewonly=True
    )


class BillingPaymentProjection(Base, TimestampMixin):
    """One convergent normalized projection for each provider payment."""

    __tablename__ = "billing_payment_projections"
    __table_args__ = (
        CheckConstraint(
            f"provider IN ({_PROVIDERS})",
            name="ck_billing_payment_projections_provider",
        ),
        _postgresql_check(
            "payment_id ~ '^[A-Za-z0-9._:-]+$'",
            name="ck_billing_payment_projections_payment_id",
        ),
        CheckConstraint(
            "aggregate_kind IN ('subscription', 'topup')",
            name="ck_billing_payment_projections_aggregate_kind",
        ),
        CheckConstraint(
            "(aggregate_kind = 'subscription' AND subscription_id IS NOT NULL "
            "AND topup_order_id IS NULL) OR "
            "(aggregate_kind = 'topup' AND topup_order_id IS NOT NULL "
            "AND subscription_id IS NULL)",
            name="ck_billing_payment_projections_aggregate_target",
        ),
        CheckConstraint(
            "status IN ('pending', 'failed', 'overdue', 'settled', "
            "'reversal_pending', 'partially_reversed', 'reversed', "
            "'chargeback_requested', 'chargeback_dispute', "
            "'restoration_pending', 'deleted', 'readback_required', 'restored', "
            "'refunded_before_settlement')",
            name="ck_billing_payment_projections_status",
        ),
        CheckConstraint(
            "amount_cents >= 0 AND settled_cents >= 0 AND reversed_cents >= 0 "
            "AND restored_cents >= 0 AND restored_cents <= reversed_cents "
            "AND reversed_cents <= settled_cents AND settled_cents <= amount_cents",
            name="ck_billing_payment_projections_totals",
        ),
        CheckConstraint(
            "(pre_settlement_refund_cents IS NULL AND "
            "pre_settlement_refund_status IS NULL AND "
            "pre_settlement_refund_observed_at IS NULL) OR "
            "(status = 'refunded_before_settlement' AND settled_cents = 0 AND "
            "pre_settlement_refund_cents > 0 AND "
            "pre_settlement_refund_cents <= amount_cents AND "
            "pre_settlement_refund_status IN ('PENDING', "
            "'AWAITING_RISK_ANALYSIS', 'OVERDUE', 'CONFIRMED', "
            "'RECEIVED', 'RECEIVED_IN_CASH', 'REFUND_REQUESTED', "
            "'REFUND_IN_PROGRESS', "
            "'REFUNDED', 'CHARGEBACK_REQUESTED', 'CHARGEBACK_DISPUTE', "
            "'AWAITING_CHARGEBACK_REVERSAL') AND "
            "pre_settlement_refund_observed_at IS NOT NULL)",
            name="ck_billing_payment_projections_pre_settlement_refund_evidence",
        ),
        CheckConstraint(
            "restoration_epoch >= 0",
            name="ck_billing_payment_projections_restoration_epoch",
        ),
        CheckConstraint(
            "(canary_environment IS NULL AND canary_slot IS NULL) OR "
            "(canary_environment IS NOT NULL AND "
            "canary_slot IN ('subscription', 'topup'))",
            name="ck_billing_payment_projections_canary_pair",
        ),
        CheckConstraint(
            "canary_slot IS NULL OR (provider = 'asaas' AND "
            "checkout_session_id IS NOT NULL AND "
            "((canary_slot = 'subscription' AND "
            "aggregate_kind = 'subscription' AND subscription_id IS NOT NULL "
            "AND topup_order_id IS NULL AND amount_cents = 99900) OR "
            "(canary_slot = 'topup' AND aggregate_kind = 'topup' AND "
            "topup_order_id IS NOT NULL AND subscription_id IS NULL "
            "AND amount_cents = 10000)))",
            name="ck_billing_payment_projections_canary_exact",
        ),
        ForeignKeyConstraint(
            ["builder_id", "subscription_id"],
            ["billing_subscriptions.builder_id", "billing_subscriptions.id"],
            ondelete="RESTRICT",
            name="fk_billing_payment_projections_builder_subscription",
        ),
        ForeignKeyConstraint(
            ["builder_id", "topup_order_id"],
            ["billing_topup_orders.builder_id", "billing_topup_orders.id"],
            ondelete="RESTRICT",
            name="fk_billing_payment_projections_builder_topup",
        ),
        ForeignKeyConstraint(
            ["builder_id", "subscription_cycle_id"],
            [
                "billing_subscription_cycles.builder_id",
                "billing_subscription_cycles.id",
            ],
            ondelete="RESTRICT",
            name="fk_billing_payment_projections_builder_cycle",
        ),
        ForeignKeyConstraint(
            ["builder_id", "checkout_session_id"],
            ["billing_checkout_sessions.builder_id", "billing_checkout_sessions.id"],
            ondelete="RESTRICT",
            name="fk_billing_payment_projections_builder_checkout",
        ),
        ForeignKeyConstraint(
            [
                "builder_id",
                "checkout_session_id",
                "canary_environment",
                "canary_slot",
            ],
            [
                "billing_checkout_sessions.builder_id",
                "billing_checkout_sessions.id",
                "billing_checkout_sessions.canary_environment",
                "billing_checkout_sessions.canary_slot",
            ],
            ondelete="RESTRICT",
            name="fk_billing_payment_projections_canary_checkout",
        ),
        UniqueConstraint(
            "provider", "payment_id", name="uq_billing_payment_projections_payment"
        ),
        UniqueConstraint(
            "builder_id",
            "id",
            name="uq_billing_payment_projections_builder_id_id",
        ),
        UniqueConstraint(
            "builder_id",
            "id",
            "provider",
            "payment_id",
            name="uq_billing_payment_projections_identity",
        ),
        UniqueConstraint(
            "builder_id",
            "id",
            "provider",
            "payment_id",
            "canary_environment",
            "canary_slot",
            name="uq_billing_payment_projections_canary_identity",
        ),
        Index(
            "uq_billing_payment_projections_canary_slot",
            "canary_environment",
            "canary_slot",
            unique=True,
            postgresql_where=text("canary_environment IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("builders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    payment_id: Mapped[str] = mapped_column(String(160), nullable=False)
    aggregate_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    subscription_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    topup_order_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    subscription_cycle_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    checkout_session_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    canary_environment: Mapped[str | None] = mapped_column(String(20), nullable=True)
    canary_slot: Mapped[str | None] = mapped_column(String(20), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    settled_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    reversed_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    restored_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    restoration_epoch: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    # ASAAS's civil settlement date; never derived from event or processing time.
    paid_at: Mapped[date | None] = mapped_column(Date, nullable=True)
    pre_settlement_refund_cents: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    pre_settlement_refund_status: Mapped[str | None] = mapped_column(
        String(40), nullable=True
    )
    pre_settlement_refund_observed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ordering_key: Mapped[str] = mapped_column(String(200), nullable=False)
    last_event_id: Mapped[str] = mapped_column(String(160), nullable=False)

    subscription: Mapped["BillingSubscription | None"] = relationship(
        "BillingSubscription", back_populates="payment_projections", viewonly=True
    )
    topup_order: Mapped["BillingTopupOrder | None"] = relationship(
        "BillingTopupOrder", back_populates="payment_projections", viewonly=True
    )
    checkout_session: Mapped["BillingCheckoutSession | None"] = relationship(
        "BillingCheckoutSession",
        primaryjoin="and_(BillingPaymentProjection.builder_id == "
        "BillingCheckoutSession.builder_id, "
        "BillingPaymentProjection.checkout_session_id == "
        "BillingCheckoutSession.id)",
        foreign_keys="[BillingPaymentProjection.builder_id, "
        "BillingPaymentProjection.checkout_session_id]",
        back_populates="payment_projections",
        viewonly=True,
    )
    effects: Mapped[list["BillingPaymentEffect"]] = relationship(
        "BillingPaymentEffect",
        primaryjoin="and_(BillingPaymentProjection.builder_id == "
        "BillingPaymentEffect.builder_id, BillingPaymentProjection.id == "
        "BillingPaymentEffect.projection_id, BillingPaymentProjection.provider == "
        "BillingPaymentEffect.provider, BillingPaymentProjection.payment_id == "
        "BillingPaymentEffect.payment_id)",
        foreign_keys="[BillingPaymentEffect.builder_id, "
        "BillingPaymentEffect.projection_id, BillingPaymentEffect.provider, "
        "BillingPaymentEffect.payment_id]",
        back_populates="projection",
        viewonly=True,
    )


class BillingPaymentEffect(Base):
    """One commercial transition for a payment and cumulative source revision."""

    __tablename__ = "billing_payment_effects"
    __table_args__ = (
        CheckConstraint(
            f"provider IN ({_PROVIDERS})",
            name="ck_billing_payment_effects_provider",
        ),
        CheckConstraint(
            "effect_kind IN ('settlement', 'reversal', 'restoration')",
            name="ck_billing_payment_effects_kind",
        ),
        CheckConstraint(
            "amount_cents >= 0",
            name="ck_billing_payment_effects_amount",
        ),
        CheckConstraint(
            "(canary_environment IS NULL AND canary_slot IS NULL) OR "
            "(canary_environment IS NOT NULL AND "
            "canary_slot IN ('subscription', 'topup'))",
            name="ck_billing_payment_effects_canary_pair",
        ),
        CheckConstraint(
            "canary_slot IS NULL OR (effect_kind = 'settlement' AND "
            "source_revision = 'settlement:v1' AND "
            "((canary_slot = 'subscription' AND amount_cents = 99900) OR "
            "(canary_slot = 'topup' AND amount_cents = 10000)))",
            name="ck_billing_payment_effects_canary_exact",
        ),
        _postgresql_check(
            "char_length(source_revision) BETWEEN 1 AND 100 "
            "AND source_revision !~ '[[\\:cntrl\\:]]'",
            name="ck_billing_payment_effects_source_revision",
        ),
        ForeignKeyConstraint(
            ["builder_id", "projection_id", "provider", "payment_id"],
            [
                "billing_payment_projections.builder_id",
                "billing_payment_projections.id",
                "billing_payment_projections.provider",
                "billing_payment_projections.payment_id",
            ],
            ondelete="RESTRICT",
            name="fk_billing_payment_effects_builder_projection",
        ),
        ForeignKeyConstraint(
            ["builder_id", "delivery_id"],
            ["billing_webhook_deliveries.builder_id", "billing_webhook_deliveries.id"],
            ondelete="RESTRICT",
            name="fk_billing_payment_effects_builder_delivery",
        ),
        ForeignKeyConstraint(
            [
                "builder_id",
                "projection_id",
                "provider",
                "payment_id",
                "canary_environment",
                "canary_slot",
            ],
            [
                "billing_payment_projections.builder_id",
                "billing_payment_projections.id",
                "billing_payment_projections.provider",
                "billing_payment_projections.payment_id",
                "billing_payment_projections.canary_environment",
                "billing_payment_projections.canary_slot",
            ],
            ondelete="RESTRICT",
            name="fk_billing_payment_effects_canary_projection",
        ),
        UniqueConstraint(
            "provider",
            "payment_id",
            "effect_kind",
            "source_revision",
            name="uq_billing_payment_effects_idempotency",
        ),
        UniqueConstraint(
            "builder_id", "id", name="uq_billing_payment_effects_builder_id_id"
        ),
        UniqueConstraint(
            "builder_id",
            "id",
            "provider",
            "payment_id",
            name="uq_billing_payment_effects_identity",
        ),
        Index(
            "uq_billing_payment_effects_canary_slot",
            "canary_environment",
            "canary_slot",
            unique=True,
            postgresql_where=text(
                "canary_environment IS NOT NULL AND effect_kind = 'settlement'"
            ),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False, index=True
    )
    projection_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    delivery_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    payment_id: Mapped[str] = mapped_column(String(160), nullable=False)
    effect_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    source_revision: Mapped[str] = mapped_column(String(100), nullable=False)
    amount_cents: Mapped[int] = mapped_column(BigInteger, nullable=False)
    canary_environment: Mapped[str | None] = mapped_column(String(20), nullable=True)
    canary_slot: Mapped[str | None] = mapped_column(String(20), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    projection: Mapped["BillingPaymentProjection"] = relationship(
        "BillingPaymentProjection",
        primaryjoin="and_(BillingPaymentEffect.builder_id == "
        "BillingPaymentProjection.builder_id, BillingPaymentEffect.projection_id == "
        "BillingPaymentProjection.id, BillingPaymentEffect.provider == "
        "BillingPaymentProjection.provider, BillingPaymentEffect.payment_id == "
        "BillingPaymentProjection.payment_id)",
        foreign_keys="[BillingPaymentEffect.builder_id, "
        "BillingPaymentEffect.projection_id, BillingPaymentEffect.provider, "
        "BillingPaymentEffect.payment_id]",
        back_populates="effects",
        viewonly=True,
    )
    delivery: Mapped["BillingWebhookDelivery"] = relationship(
        "BillingWebhookDelivery", back_populates="payment_effects", viewonly=True
    )
    transaction: Mapped["Transaction | None"] = relationship(
        "Transaction", back_populates="billing_payment_effect", uselist=False
    )


class BillingTopupCapabilityReceipt(Base):
    """Append-only validated minimum needed to authorize the top-up gate."""

    __tablename__ = "billing_topup_capability_receipts"
    __table_args__ = (
        CheckConstraint(
            "environment IN ('development', 'staging', 'production', 'test')",
            name="ck_billing_topup_capability_environment",
        ),
        CheckConstraint(
            "schema_version = 'credit_capability_ready.v1'",
            name="ck_billing_topup_capability_schema",
        ),
        CheckConstraint(
            "inbox_high_watermark >= 0",
            name="ck_billing_topup_capability_watermark",
        ),
        _postgresql_check(
            f"billing_fact_schema_digest ~ '{_SHA256_CHECK}' "
            f"AND producer_compatibility_digest ~ '{_SHA256_CHECK}' "
            f"AND consumer_compatibility_digest ~ '{_SHA256_CHECK}'",
            name="ck_billing_topup_capability_digests",
        ),
        CheckConstraint(
            "expires_at > approved_at",
            name="ck_billing_topup_capability_expiry",
        ),
        UniqueConstraint(
            "environment",
            "request_id",
            name="uq_billing_topup_capability_environment_request",
        ),
        UniqueConstraint(
            "environment",
            "id",
            name="uq_billing_topup_capability_environment_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    request_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    operation_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    deployment_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    credit_deployment_digest: Mapped[str] = mapped_column(String(100), nullable=False)
    billing_fact_schema_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    producer_compatibility_digest: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    consumer_compatibility_digest: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    inbox_high_watermark: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reconciliation_receipt_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    approved_by: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    approved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    reconciliation_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    readback_complete: Mapped[bool] = mapped_column(Boolean, nullable=False)
    has_open_gap: Mapped[bool] = mapped_column(Boolean, nullable=False)
    has_backlog: Mapped[bool] = mapped_column(Boolean, nullable=False)
    has_alert: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BillingOperationAssignment(Base, TimestampMixin):
    """One active duty in an auditable three-person billing operation.

    Assignments do not contain a token, provider payload, customer attribute,
    or display name.  They only bind an authenticated administrator to a
    bounded, expiring operation.  A receipt consumes the assignment once.
    """

    __tablename__ = "billing_operation_assignments"
    __table_args__ = (
        CheckConstraint(
            "environment IN ('development', 'staging', 'production', 'test')",
            name="ck_billing_operation_assignments_environment",
        ),
        CheckConstraint(
            "scope IN ('migration', 'rollout', 'topup_capability', 'incident')",
            name="ck_billing_operation_assignments_scope",
        ),
        CheckConstraint(
            "role IN ('deployer', 'approver', 'reviewer')",
            name="ck_billing_operation_assignments_role",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_billing_operation_assignments_expiry",
        ),
        _postgresql_check(
            f"payload_digest ~ '{_SHA256_CHECK}'",
            name="ck_billing_operation_assignments_digest",
        ),
        UniqueConstraint(
            "operation_id",
            "actor_user_id",
            name="uq_billing_operation_assignments_actor",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    operation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    scope: Mapped[str] = mapped_column(String(30), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class BillingTopupCapabilityBinding(Base):
    """One-use cross-service proof bound to a top-up capability receipt."""

    __tablename__ = "billing_topup_capability_bindings"
    __table_args__ = (
        CheckConstraint(
            "environment IN ('development', 'staging', 'production', 'test')",
            name="ck_billing_topup_capability_bindings_environment",
        ),
        _postgresql_check(
            f"producer_digest ~ '{_SHA256_CHECK}' "
            f"AND credit_digest ~ '{_SHA256_CHECK}' "
            f"AND reconciliation_digest ~ '{_SHA256_CHECK}' "
            f"AND verification_digest ~ '{_SHA256_CHECK}'",
            name="ck_billing_topup_capability_bindings_digests",
        ),
        ForeignKeyConstraint(
            ["environment", "capability_receipt_id"],
            [
                "billing_topup_capability_receipts.environment",
                "billing_topup_capability_receipts.id",
            ],
            ondelete="RESTRICT",
            name="fk_billing_topup_capability_bindings_capability",
        ),
        ForeignKeyConstraint(
            ["environment", "producer_receipt_id"],
            ["billing_operation_receipts.environment", "billing_operation_receipts.id"],
            ondelete="RESTRICT",
            name="fk_billing_topup_bindings_producer_receipt",
        ),
        ForeignKeyConstraint(
            ["environment", "credit_receipt_id"],
            ["billing_operation_receipts.environment", "billing_operation_receipts.id"],
            ondelete="RESTRICT",
            name="fk_billing_topup_bindings_credit_receipt",
        ),
        ForeignKeyConstraint(
            ["environment", "reconciliation_receipt_id"],
            ["billing_operation_receipts.environment", "billing_operation_receipts.id"],
            ondelete="RESTRICT",
            name="fk_billing_topup_bindings_reconciliation_receipt",
        ),
        ForeignKeyConstraint(
            ["environment", "verification_receipt_id"],
            ["billing_operation_receipts.environment", "billing_operation_receipts.id"],
            ondelete="RESTRICT",
            name="fk_billing_topup_bindings_verification_receipt",
        ),
        UniqueConstraint(
            "environment",
            "capability_receipt_id",
            name="uq_billing_topup_capability_bindings_capability",
        ),
        UniqueConstraint(
            "environment",
            "producer_receipt_id",
            name="uq_billing_topup_bindings_producer",
        ),
        UniqueConstraint(
            "environment", "credit_receipt_id", name="uq_billing_topup_bindings_credit"
        ),
        UniqueConstraint(
            "environment",
            "reconciliation_receipt_id",
            name="uq_billing_topup_bindings_reconciliation",
        ),
        UniqueConstraint(
            "environment",
            "verification_receipt_id",
            name="uq_billing_topup_bindings_verification",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    capability_receipt_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    producer_receipt_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    credit_receipt_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    reconciliation_receipt_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    verification_receipt_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    producer_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    credit_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    reconciliation_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    producer_actor_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    credit_actor_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    reconciliation_actor_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    verification_actor_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BillingOperationReceipt(Base):
    """Append-only signed evidence, consumed once by a control transition."""

    __tablename__ = "billing_operation_receipts"
    __table_args__ = (
        CheckConstraint(
            "environment IN ('development', 'staging', 'production', 'test')",
            name="ck_billing_operation_receipts_environment",
        ),
        CheckConstraint(
            "scope IN ('migration', 'rollout', 'topup_capability', 'incident')",
            name="ck_billing_operation_receipts_scope",
        ),
        CheckConstraint(
            "role IN ('deployer', 'approver', 'reviewer')",
            name="ck_billing_operation_receipts_role",
        ),
        CheckConstraint(
            "evidence_type IS NULL OR evidence_type IN "
            "('producer', 'credit', 'reconciliation', 'verification')",
            name="ck_billing_operation_receipts_evidence_type",
        ),
        CheckConstraint(
            "inbox_high_watermark IS NULL OR inbox_high_watermark >= 0",
            name="ck_billing_operation_receipts_watermark",
        ),
        _postgresql_check(
            f"billing_fact_schema_digest IS NULL OR billing_fact_schema_digest ~ '{_SHA256_CHECK}'",
            name="ck_billing_operation_receipts_fact_schema_digest",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_billing_operation_receipts_expiry",
        ),
        _postgresql_check(
            f"payload_digest ~ '{_SHA256_CHECK}' "
            f"AND deployment_digest ~ '{_SHA256_CHECK}' "
            f"AND budget_digest ~ '{_SHA256_CHECK}' "
            f"AND signature_sha256 ~ '{_SHA256_CHECK}'",
            name="ck_billing_operation_receipts_digests",
        ),
        UniqueConstraint("nonce", name="uq_billing_operation_receipts_nonce"),
        UniqueConstraint(
            "environment", "id", name="uq_billing_operation_receipts_environment_id"
        ),
        UniqueConstraint(
            "assignment_id", name="uq_billing_operation_receipts_assignment"
        ),
        ForeignKeyConstraint(
            ["assignment_id"],
            ["billing_operation_assignments.id"],
            ondelete="RESTRICT",
            name="fk_billing_operation_receipts_assignment",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    assignment_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    operation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    scope: Mapped[str] = mapped_column(String(30), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    nonce: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    payload_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    deployment_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    budget_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    from_state: Mapped[str] = mapped_column(String(40), nullable=False)
    to_state: Mapped[str] = mapped_column(String(40), nullable=False)
    evidence_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    request_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    inbox_high_watermark: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Typed capability attestations carry the deployed fact-contract digest
    # independently of the operation payload/deployment identities.  It is
    # nullable for the ordinary rollout/migration quorum rows.
    billing_fact_schema_digest: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    signature_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BillingRolloutReadinessReceipt(Base):
    """Fresh, exact backend readiness evidence required for general rollout."""

    __tablename__ = "billing_rollout_readiness_receipts"
    __table_args__ = (
        CheckConstraint(
            "environment IN ('development', 'staging', 'production', 'test')",
            name="ck_billing_rollout_readiness_environment",
        ),
        CheckConstraint(
            "receipt_kind IN ('readiness', 'backlog', 'provisioning', 'smoke')",
            name="ck_billing_rollout_readiness_kind",
        ),
        CheckConstraint(
            "status = 'passed'", name="ck_billing_rollout_readiness_status"
        ),
        _postgresql_check(
            f"content_sha256 ~ '{_SHA256_CHECK}' "
            f"AND rule_digest ~ '{_SHA256_CHECK}' "
            f"AND bundle_digest ~ '{_SHA256_CHECK}' "
            f"AND app_digest ~ '{_SHA256_CHECK}'",
            name="ck_billing_rollout_readiness_digest",
        ),
        CheckConstraint(
            "expires_at > observed_at",
            name="ck_billing_rollout_readiness_fresh",
        ),
        UniqueConstraint(
            "environment",
            "receipt_kind",
            "rollout_revision",
            name="uq_billing_rollout_readiness_revision",
        ),
        ForeignKeyConstraint(
            ["environment", "operation_receipt_id"],
            ["billing_operation_receipts.environment", "billing_operation_receipts.id"],
            ondelete="RESTRICT",
            name="fk_billing_rollout_readiness_operation_receipt",
        ),
        ForeignKeyConstraint(
            ["environment", "provider_receipt_id"],
            ["billing_operation_receipts.environment", "billing_operation_receipts.id"],
            ondelete="RESTRICT",
            name="fk_billing_rollout_readiness_provider_receipt",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    receipt_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    rollout_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    operation_receipt_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    provider_receipt_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), nullable=False
    )
    rule_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    app_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class BillingRolloutControl(Base):
    """Per-environment commercial rollout and independent top-up gate."""

    __tablename__ = "billing_rollout_control"
    __table_args__ = (
        CheckConstraint(
            "environment IN ('development', 'staging', 'production', 'test')",
            name="ck_billing_rollout_control_environment",
        ),
        CheckConstraint(
            "state IN ('disabled', 'canary_allowlist', 'general')",
            name="ck_billing_rollout_control_state",
        ),
        CheckConstraint(
            "rollout_revision >= 0 AND topup_revision >= 0",
            name="ck_billing_rollout_control_revisions",
        ),
        CheckConstraint(
            "((rollout_revision = 0 AND rollout_receipt_id IS NULL) OR "
            "(rollout_revision > 0 AND rollout_receipt_id IS NOT NULL)) AND "
            "((topup_revision = 0 AND topup_receipt_id IS NULL) OR "
            "(topup_revision > 0 AND topup_receipt_id IS NOT NULL))",
            name="ck_billing_rollout_control_receipt_pointers",
        ),
        CheckConstraint(
            "(state = 'canary_allowlist' AND canary_builder_id IS NOT NULL) OR "
            "(state <> 'canary_allowlist' AND canary_builder_id IS NULL)",
            name="ck_billing_rollout_control_canary_builder",
        ),
        CheckConstraint(
            "canary_checkout_limit = 2 AND canary_settlement_limit = 2 "
            "AND canary_amount_limit_cents = 109900",
            name="ck_billing_rollout_control_canary_budget",
        ),
        CheckConstraint(
            "canary_checkout_count = "
            "CAST(canary_subscription_checkout_id IS NOT NULL AS INTEGER) + "
            "CAST(canary_topup_checkout_id IS NOT NULL AS INTEGER) AND "
            "canary_settlement_count = "
            "CAST(canary_subscription_effect_id IS NOT NULL AS INTEGER) + "
            "CAST(canary_topup_effect_id IS NOT NULL AS INTEGER) AND "
            "canary_amount_reserved_cents = "
            "CASE WHEN canary_subscription_checkout_id IS NULL "
            "THEN 0 ELSE 99900 END + "
            "CASE WHEN canary_topup_checkout_id IS NULL THEN 0 ELSE 10000 END AND "
            "(canary_subscription_effect_id IS NULL OR "
            "canary_subscription_checkout_id IS NOT NULL) AND "
            "(canary_topup_effect_id IS NULL OR "
            "canary_topup_checkout_id IS NOT NULL)",
            name="ck_billing_rollout_control_canary_usage",
        ),
        CheckConstraint(
            "(topup_commercial_enabled = false "
            "AND active_topup_capability_id IS NULL) OR "
            "(topup_commercial_enabled = true "
            "AND active_topup_capability_id IS NOT NULL)",
            name="ck_billing_rollout_control_topup_capability",
        ),
        ForeignKeyConstraint(
            ["environment", "active_topup_capability_id"],
            [
                "billing_topup_capability_receipts.environment",
                "billing_topup_capability_receipts.id",
            ],
            ondelete="RESTRICT",
            name="fk_billing_rollout_control_topup_capability",
        ),
        ForeignKeyConstraint(
            ["rollout_receipt_id"],
            ["billing_rollout_receipts.id"],
            ondelete="RESTRICT",
            name="fk_billing_rollout_control_rollout_receipt",
        ),
        ForeignKeyConstraint(
            ["topup_receipt_id"],
            ["billing_rollout_receipts.id"],
            ondelete="RESTRICT",
            name="fk_billing_rollout_control_topup_receipt",
        ),
        UniqueConstraint(
            "rollout_receipt_id",
            name="uq_billing_rollout_control_rollout_receipt",
        ),
        UniqueConstraint(
            "topup_receipt_id",
            name="uq_billing_rollout_control_topup_receipt",
        ),
        ForeignKeyConstraint(
            ["canary_subscription_checkout_id"],
            ["billing_checkout_sessions.id"],
            ondelete="RESTRICT",
            name="fk_billing_rollout_subscription_checkout",
        ),
        ForeignKeyConstraint(
            ["canary_topup_checkout_id"],
            ["billing_checkout_sessions.id"],
            ondelete="RESTRICT",
            name="fk_billing_rollout_topup_checkout",
        ),
        ForeignKeyConstraint(
            ["canary_subscription_effect_id"],
            ["billing_payment_effects.id"],
            ondelete="RESTRICT",
            name="fk_billing_rollout_subscription_effect",
        ),
        ForeignKeyConstraint(
            ["canary_topup_effect_id"],
            ["billing_payment_effects.id"],
            ondelete="RESTRICT",
            name="fk_billing_rollout_topup_effect",
        ),
    )

    environment: Mapped[str] = mapped_column(String(20), primary_key=True)
    state: Mapped[str] = mapped_column(
        String(30), nullable=False, default="disabled", server_default="disabled"
    )
    canary_builder_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("builders.id", ondelete="RESTRICT"),
        nullable=True,
    )
    rollout_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    rollout_receipt_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    topup_commercial_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    topup_revision: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    topup_receipt_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    active_topup_capability_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    canary_checkout_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    canary_settlement_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=2, server_default="2"
    )
    canary_amount_limit_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=109900, server_default="109900"
    )
    canary_checkout_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    canary_settlement_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    canary_amount_reserved_cents: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    canary_subscription_checkout_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    canary_topup_checkout_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    canary_subscription_effect_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    canary_topup_effect_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BillingRolloutReceipt(Base):
    """Append-only sanitized evidence for a rollout or gate transition."""

    __tablename__ = "billing_rollout_receipts"
    __table_args__ = (
        CheckConstraint(
            "environment IN ('development', 'staging', 'production', 'test')",
            name="ck_billing_rollout_receipts_environment",
        ),
        CheckConstraint(
            "receipt_kind IN ('rollout_transition', 'topup_gate_transition')",
            name="ck_billing_rollout_receipts_kind",
        ),
        CheckConstraint(
            "(receipt_kind = 'rollout_transition' AND "
            "from_state IN ('disabled', 'canary_allowlist', 'general') AND "
            "to_state IN ('disabled', 'canary_allowlist', 'general') AND "
            "capability_receipt_id IS NULL) OR "
            "(receipt_kind = 'topup_gate_transition' AND "
            "from_state IN ('topup_disabled', 'topup_enabled') AND "
            "to_state IN ('topup_disabled', 'topup_enabled') AND "
            "from_state <> to_state AND "
            "((to_state = 'topup_enabled' AND capability_receipt_id IS NOT NULL) "
            "OR (to_state = 'topup_disabled' AND capability_receipt_id IS NULL)))",
            name="ck_billing_rollout_receipts_states",
        ),
        CheckConstraint(
            "resulting_revision = expected_revision + 1",
            name="ck_billing_rollout_receipts_revision",
        ),
        _postgresql_check(
            f"deployment_digest ~ '{_SHA256_CHECK}'",
            name="ck_billing_rollout_receipts_deployment_digest",
        ),
        UniqueConstraint("nonce", name="uq_billing_rollout_receipts_nonce"),
        UniqueConstraint(
            "environment",
            "receipt_kind",
            "resulting_revision",
            name="uq_billing_rollout_receipts_revision",
        ),
        ForeignKeyConstraint(
            ["environment", "capability_receipt_id"],
            [
                "billing_topup_capability_receipts.environment",
                "billing_topup_capability_receipts.id",
            ],
            ondelete="RESTRICT",
            name="fk_billing_rollout_receipts_capability",
        ),
        Index(
            "uq_billing_rollout_receipts_capability_consumption",
            "environment",
            "capability_receipt_id",
            unique=True,
            postgresql_where=text("capability_receipt_id IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    environment: Mapped[str] = mapped_column(String(20), nullable=False)
    receipt_kind: Mapped[str] = mapped_column(String(40), nullable=False)
    nonce: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    deployment_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    from_state: Mapped[str] = mapped_column(String(30), nullable=False)
    to_state: Mapped[str] = mapped_column(String(30), nullable=False)
    expected_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    resulting_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    capability_receipt_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    actor_user_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BillingOutboxEvent(Base):
    """Immutable canonical fact queued in the same transaction as its aggregate."""

    __tablename__ = "billing_outbox"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'billing.fact.v1'",
            name="ck_billing_outbox_schema_version",
        ),
        CheckConstraint(
            "event_type IN ('subscription.cycle_settled.v1', "
            "'subscription.cycle_reversed.v1', "
            "'subscription.cycle_restored.v1', 'topup.payment_settled.v1', "
            "'topup.payment_reversed.v1', 'topup.payment_restored.v1')",
            name="ck_billing_outbox_event_type",
        ),
        CheckConstraint(
            "aggregate_kind IN ('subscription_cycle', 'topup_order')",
            name="ck_billing_outbox_aggregate_kind",
        ),
        CheckConstraint(
            "financial_revision > 0",
            name="ck_billing_outbox_financial_revision",
        ),
        CheckConstraint(
            "(aggregate_kind = 'subscription_cycle' AND subscription_id IS NOT NULL "
            "AND topup_order_id IS NULL AND "
            "((subscription_cycle_id IS NOT NULL AND aggregate_id = subscription_cycle_id) "
            "OR (subscription_cycle_id IS NULL AND aggregate_id = subscription_id))) OR "
            "(aggregate_kind = 'topup_order' AND topup_order_id IS NOT NULL "
            "AND subscription_id IS NULL AND subscription_cycle_id IS NULL "
            "AND aggregate_id = topup_order_id)",
            name="ck_billing_outbox_aggregate_target",
        ),
        _postgresql_check(
            f"payload_sha256 ~ '{_SHA256_CHECK}'",
            name="ck_billing_outbox_payload_sha256",
        ),
        ForeignKeyConstraint(
            ["builder_id", "subscription_id"],
            ["billing_subscriptions.builder_id", "billing_subscriptions.id"],
            ondelete="RESTRICT",
            name="fk_billing_outbox_builder_subscription",
        ),
        ForeignKeyConstraint(
            ["builder_id", "topup_order_id"],
            ["billing_topup_orders.builder_id", "billing_topup_orders.id"],
            ondelete="RESTRICT",
            name="fk_billing_outbox_builder_topup",
        ),
        ForeignKeyConstraint(
            ["builder_id", "subscription_cycle_id"],
            [
                "billing_subscription_cycles.builder_id",
                "billing_subscription_cycles.id",
            ],
            ondelete="RESTRICT",
            name="fk_billing_outbox_builder_cycle",
        ),
        ForeignKeyConstraint(
            ["payment_effect_id"],
            ["billing_payment_effects.id"],
            ondelete="RESTRICT",
            name="fk_billing_outbox_payment_effect",
        ),
        UniqueConstraint(
            "aggregate_kind",
            "aggregate_id",
            "financial_revision",
            name="uq_billing_outbox_aggregate_revision",
        ),
        UniqueConstraint("payment_effect_id", name="uq_billing_outbox_payment_effect"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    builder_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("builders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    aggregate_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    aggregate_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    subscription_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    topup_order_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    subscription_cycle_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    payment_effect_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), nullable=True
    )
    financial_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    deployment_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    subscription: Mapped["BillingSubscription | None"] = relationship(
        "BillingSubscription", back_populates="outbox_events", viewonly=True
    )
    topup_order: Mapped["BillingTopupOrder | None"] = relationship(
        "BillingTopupOrder", back_populates="outbox_events", viewonly=True
    )


class BillingFactDelivery(Base, TimestampMixin):
    """Relational retry record; an acknowledged fact is never discarded."""

    __tablename__ = "billing_fact_deliveries"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'awaiting_ack', 'acknowledged', 'blocked')",
            name="ck_billing_fact_deliveries_state",
        ),
        CheckConstraint(
            "attempt_count >= 0",
            name="ck_billing_fact_deliveries_attempt_count",
        ),
        ForeignKeyConstraint(
            ["outbox_event_id"],
            ["billing_outbox.id"],
            ondelete="RESTRICT",
            name="fk_billing_fact_deliveries_outbox",
        ),
        UniqueConstraint("outbox_event_id", name="uq_billing_fact_deliveries_outbox"),
        UniqueConstraint("event_id", name="uq_billing_fact_deliveries_event_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    outbox_event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class BillingFactHandoff(Base):
    """Append-only producer evidence binding one fact to its contract bundle."""

    __tablename__ = "billing_fact_handoffs"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'billing.fact.v1'",
            name="ck_billing_fact_handoffs_schema_version",
        ),
        _postgresql_check(
            f"bundle_digest ~ '{_SHA256_CHECK}' AND deployment_digest ~ '{_SHA256_CHECK}'",
            name="ck_billing_fact_handoffs_digests",
        ),
        ForeignKeyConstraint(
            ["outbox_event_id"],
            ["billing_outbox.id"],
            ondelete="RESTRICT",
            name="fk_billing_fact_handoffs_outbox",
        ),
        ForeignKeyConstraint(
            ["delivery_id"],
            ["billing_fact_deliveries.id"],
            ondelete="RESTRICT",
            name="fk_billing_fact_handoffs_delivery",
        ),
        UniqueConstraint("outbox_event_id", name="uq_billing_fact_handoffs_outbox"),
        UniqueConstraint("delivery_id", name="uq_billing_fact_handoffs_delivery"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    outbox_event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    delivery_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    bundle_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    deployment_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    handed_off_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class BillingFactAck(Base):
    """Consumer-owned append-only acknowledgement read by this producer only."""

    __tablename__ = "billing_fact_acks"
    __table_args__ = (
        CheckConstraint(
            "schema_version = 'billing.fact.ack.v1'",
            name="ck_billing_fact_acks_schema_version",
        ),
        CheckConstraint(
            "outcome IN ('accepted', 'rejected')",
            name="ck_billing_fact_acks_outcome",
        ),
        _postgresql_check(
            f"fact_sha256 ~ '{_SHA256_CHECK}' AND bundle_digest ~ '{_SHA256_CHECK}' "
            f"AND deployment_digest ~ '{_SHA256_CHECK}' AND effects_digest ~ '{_SHA256_CHECK}'",
            name="ck_billing_fact_acks_digests",
        ),
        ForeignKeyConstraint(
            ["outbox_event_id"],
            ["billing_outbox.id"],
            ondelete="RESTRICT",
            name="fk_billing_fact_acks_outbox",
        ),
        UniqueConstraint("outbox_event_id", name="uq_billing_fact_acks_outbox"),
        UniqueConstraint("event_id", name="uq_billing_fact_acks_event_id"),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    outbox_event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(40), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    inbox_event_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    fact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    bundle_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    deployment_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    effects_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    acknowledged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class BillingFactReconciliationAlert(Base):
    """Append-only sanitized evidence of a producer/consumer reconciliation gap."""

    __tablename__ = "billing_fact_reconciliation_alerts"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('backlog', 'collision', 'schema_mismatch', 'digest_mismatch', "
            "'gap', 'partial_credit_settlement', 'ambiguous_line_allocation')",
            name="ck_billing_fact_reconciliation_alerts_kind",
        ),
        _postgresql_check(
            f"detail_sha256 ~ '{_SHA256_CHECK}'",
            name="ck_billing_fact_reconciliation_alerts_digest",
        ),
        ForeignKeyConstraint(
            ["outbox_event_id"],
            ["billing_outbox.id"],
            ondelete="RESTRICT",
            name="fk_billing_fact_reconciliation_alerts_outbox",
        ),
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    outbox_event_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    detail_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
