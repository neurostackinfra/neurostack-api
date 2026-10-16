"""persist provider-confirmed ASAAS civil settlement date

Revision ID: 035
Revises: 034
Create Date: 2026-10-15 00:00:03
"""

from typing import Union

import sqlalchemy as sa

from alembic import op

revision: str = "035"
down_revision: Union[str, None] = "034"
branch_labels = None
depends_on = None


def _canary_subscription_snapshot(grant: int) -> str:
    return (
        "pricing_snapshot = jsonb_build_object("
        "'catalog_version_id', 'payment-2026-10-03', "
        "'offer_id', 'plan_plus_monthly_v1', 'amount_cents', 99900, "
        f"'credit_grant_mc', {grant}, 'included_clinics', 1, "
        "'billing_cycle', 'monthly', 'payment_method', 'credit_card', "
        "'lines', jsonb_build_array(jsonb_build_object("
        "'line_id', 'plan', 'offer_id', 'plan_plus_monthly_v1', "
        "'kind', 'plan', 'quantity', 1, 'unit_amount_cents', 99900, "
        f"'line_amount_cents', 99900, 'credit_grant_mc', {grant}, "
        "'settled_cents', 0, 'reversed_cents', 0, 'restored_cents', 0, "
        "'line_financial_state', 'pending')))"
    )


def _canary_check(*, allow_legacy_credit_snapshot: bool) -> str:
    snapshots = (
        [_canary_subscription_snapshot(5_000_000), _canary_subscription_snapshot(5_000)]
        if allow_legacy_credit_snapshot
        else [_canary_subscription_snapshot(5_000)]
    )
    exact_subscription = "(" + " OR ".join(snapshots) + ")"
    return (
        "canary_slot IS NULL OR (provider = 'asaas' AND attempt_no = 1 AND "
        "catalog_version_id = 'payment-2026-10-03' AND "
        "((canary_slot = 'subscription' AND intent_kind = 'subscription' "
        "AND migration_id IS NULL AND topup_order_id IS NULL "
        "AND next_due_date IS NOT NULL AND offer_id = 'plan_plus_monthly_v1' "
        "AND amount_cents = 99900 AND "
        "pricing_snapshot ->> 'catalog_version_id' = 'payment-2026-10-03' AND "
        "pricing_snapshot ->> 'offer_id' = 'plan_plus_monthly_v1' AND "
        "pricing_snapshot ->> 'amount_cents' = '99900' AND "
        "pricing_snapshot ->> 'payment_method' = 'credit_card' AND "
        "pricing_snapshot ->> 'billing_cycle' = 'monthly' AND "
        f"{exact_subscription}) OR "
        "(canary_slot = 'topup' AND intent_kind = 'topup' "
        "AND migration_id IS NULL AND topup_order_id IS NOT NULL "
        "AND intent_id = topup_order_id "
        "AND offer_id = 'package_credits_500_v1' AND amount_cents = 10000 "
        "AND pricing_snapshot ->> 'catalog_version_id' = 'payment-2026-10-03' "
        "AND pricing_snapshot ->> 'offer_id' = 'package_credits_500_v1' "
        "AND pricing_snapshot ->> 'amount_cents' = '10000' "
        "AND pricing_snapshot ->> 'credit_grant_mc' = '500000')))"
    )


def _replace_projection_status_constraint(*, allow_refund_marker: bool) -> None:
    op.drop_constraint(
        "ck_billing_payment_projections_status",
        "billing_payment_projections",
        type_="check",
    )
    statuses = (
        "'pending', 'failed', 'overdue', 'settled', 'reversal_pending', "
        "'partially_reversed', 'reversed', 'chargeback_requested', "
        "'chargeback_dispute', 'restoration_pending', 'deleted', "
        "'readback_required', 'restored'"
    )
    if allow_refund_marker:
        statuses += ", 'refunded_before_settlement'"
    op.create_check_constraint(
        "ck_billing_payment_projections_status",
        "billing_payment_projections",
        f"status IN ({statuses})",
    )


def upgrade() -> None:
    op.add_column(
        "billing_payment_projections", sa.Column("paid_at", sa.Date(), nullable=True)
    )
    op.add_column(
        "billing_payment_projections",
        sa.Column("pre_settlement_refund_cents", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "billing_payment_projections",
        sa.Column("pre_settlement_refund_status", sa.String(40), nullable=True),
    )
    op.add_column(
        "billing_payment_projections",
        sa.Column(
            "pre_settlement_refund_observed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_check_constraint(
        "ck_billing_payment_projections_pre_settlement_refund_evidence",
        "billing_payment_projections",
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
    )
    _replace_projection_status_constraint(allow_refund_marker=True)
    op.drop_constraint(
        "ck_billing_fact_reconciliation_alerts_kind",
        "billing_fact_reconciliation_alerts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_billing_fact_reconciliation_alerts_kind",
        "billing_fact_reconciliation_alerts",
        "kind IN ('backlog', 'collision', 'schema_mismatch', "
        "'digest_mismatch', 'gap', 'partial_credit_settlement', "
        "'ambiguous_line_allocation')",
    )

    # Existing 5,000-unit canary snapshots remain valid historical evidence.
    # New producer code accepts only the corrected 5,000,000-unit snapshot.
    op.drop_constraint(
        "ck_billing_checkout_sessions_canary_exact",
        "billing_checkout_sessions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_billing_checkout_sessions_canary_exact",
        "billing_checkout_sessions",
        _canary_check(allow_legacy_credit_snapshot=True),
    )


def downgrade() -> None:
    # Refuse loss of terminal refund history or rows that cannot satisfy the
    # old canary contract. This keeps downgrade failure explicit and atomic.
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM billing_payment_projections "
        "WHERE status = 'refunded_before_settlement') THEN "
        "RAISE EXCEPTION 'cannot downgrade 035: refunded-before-settlement "
        "markers exist; reconcile them before downgrade'; END IF; "
        "IF EXISTS (SELECT 1 FROM billing_checkout_sessions "
        "WHERE canary_slot = 'subscription' "
        "AND pricing_snapshot ->> 'credit_grant_mc' = '5000000') THEN "
        "RAISE EXCEPTION 'cannot downgrade 035: corrected canary credit "
        "snapshots exist; reconcile them before downgrade'; END IF; "
        "IF EXISTS (SELECT 1 FROM billing_fact_reconciliation_alerts "
        "WHERE kind = 'ambiguous_line_allocation') THEN "
        "RAISE EXCEPTION 'cannot downgrade 035: ambiguous line allocation "
        "alerts exist; reconcile them before downgrade'; END IF; END $$"
    )
    op.drop_constraint(
        "ck_billing_payment_projections_pre_settlement_refund_evidence",
        "billing_payment_projections",
        type_="check",
    )
    op.drop_column("billing_payment_projections", "pre_settlement_refund_observed_at")
    op.drop_column("billing_payment_projections", "pre_settlement_refund_status")
    op.drop_column("billing_payment_projections", "pre_settlement_refund_cents")
    op.drop_constraint(
        "ck_billing_checkout_sessions_canary_exact",
        "billing_checkout_sessions",
        type_="check",
    )
    op.create_check_constraint(
        "ck_billing_checkout_sessions_canary_exact",
        "billing_checkout_sessions",
        _canary_check(allow_legacy_credit_snapshot=False),
    )
    op.drop_constraint(
        "ck_billing_fact_reconciliation_alerts_kind",
        "billing_fact_reconciliation_alerts",
        type_="check",
    )
    op.create_check_constraint(
        "ck_billing_fact_reconciliation_alerts_kind",
        "billing_fact_reconciliation_alerts",
        "kind IN ('backlog', 'collision', 'schema_mismatch', "
        "'digest_mismatch', 'gap', 'partial_credit_settlement')",
    )
    _replace_projection_status_constraint(allow_refund_marker=False)
    op.drop_column("billing_payment_projections", "paid_at")
