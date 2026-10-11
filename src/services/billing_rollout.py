"""Fail-closed persisted rollout guard for billing checkout creation."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.models.billing import BillingCheckoutSession, BillingRolloutControl
from src.repositories.billing import BillingRepository, BillingTransitionEvidence

BILLING_CHECKOUT_UNAVAILABLE = "billing_checkout_unavailable"


class BillingCheckoutUnavailable(RuntimeError):
    """Raised when persisted rollout does not authorize this checkout."""


class BillingTopupUnavailable(BillingCheckoutUnavailable):
    """Raised when rollout/capability does not authorize a top-up."""


async def require_billing_checkout_rollout(
    db: AsyncSession,
    builder_id: UUID,
) -> None:
    """Authorize from the canonical persisted row, denying every uncertainty.

    Group 1 creates and bootstraps this table as ``disabled``.  Until that
    migration exists, the query fails and this guard intentionally denies.
    No environment variable, header, or request field can replace the row.
    """

    if not settings.is_asaas_configuration_compatible:
        raise BillingCheckoutUnavailable(BILLING_CHECKOUT_UNAVAILABLE)

    try:
        result = await db.execute(
            text("""
                SELECT state, canary_builder_id
                FROM billing_rollout_control
                WHERE environment = :environment
                LIMIT 1
                """),
            {"environment": settings.environment},
        )
        row = result.mappings().one_or_none()
    except Exception as exc:
        raise BillingCheckoutUnavailable(BILLING_CHECKOUT_UNAVAILABLE) from exc

    if row is None:
        raise BillingCheckoutUnavailable(BILLING_CHECKOUT_UNAVAILABLE)
    state = row.get("state")
    canary_builder_id = row.get("canary_builder_id")
    if state == "general":
        return
    if state == "canary_allowlist" and str(canary_builder_id) == str(builder_id):
        return
    raise BillingCheckoutUnavailable(BILLING_CHECKOUT_UNAVAILABLE)


async def require_billing_topup_rollout(
    db: AsyncSession,
    *,
    builder_id: UUID,
    topup_order_id: UUID,
    checkout_session_id: UUID,
    attempt_no: int = 1,
) -> BillingCheckoutSession:
    """Atomically authorize and reserve a top-up before provider egress.

    The caller owns commit/rollback and must commit this reservation before it
    invokes ASAAS.  This is deliberately one operation; callers must not split
    rollout reads from session creation.
    """

    if not settings.is_asaas_configuration_compatible:
        raise BillingTopupUnavailable(BILLING_CHECKOUT_UNAVAILABLE)
    try:
        return await BillingRepository(db).authorize_and_reserve_topup_checkout(
            environment=settings.environment,
            builder_id=builder_id,
            topup_order_id=topup_order_id,
            checkout_session_id=checkout_session_id,
            attempt_no=attempt_no,
        )
    except Exception as exc:
        raise BillingTopupUnavailable(BILLING_CHECKOUT_UNAVAILABLE) from exc


async def transition_billing_rollout(
    db: AsyncSession,
    *,
    expected_revision: int,
    to_state: str,
    evidence: BillingTransitionEvidence,
    canary_builder_id: UUID | None = None,
) -> BillingRolloutControl:
    """Transition the current environment without committing the caller's unit."""

    return await BillingRepository(db).transition_rollout(
        environment=settings.environment,
        expected_revision=expected_revision,
        to_state=to_state,
        evidence=evidence,
        canary_builder_id=canary_builder_id,
    )


async def set_billing_topup_gate(
    db: AsyncSession,
    *,
    expected_revision: int,
    enabled: bool,
    evidence: BillingTransitionEvidence,
    capability_receipt_id: UUID | None = None,
) -> BillingRolloutControl:
    """Change the current environment's gate independently from rollout."""

    return await BillingRepository(db).set_topup_commercial_enabled(
        environment=settings.environment,
        expected_revision=expected_revision,
        enabled=enabled,
        evidence=evidence,
        capability_receipt_id=capability_receipt_id,
    )


async def rebase_legacy_billing_migration(
    db: AsyncSession,
    *,
    migration_id: UUID,
    expected_source_revision: int,
) -> int:
    """Refresh a cancelled saga to the current global roster revision."""

    return await BillingRepository(db).rebase_legacy_migration(
        migration_id=migration_id,
        expected_source_revision=expected_source_revision,
    )
