"""Caller-owned transactional repository for billing state boundaries."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Final, cast
from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.billing import (
    BillingCheckoutSession,
    BillingMigration,
    BillingOutboxEvent,
    BillingPaymentEffect,
    BillingPaymentProjection,
    BillingRolloutControl,
    BillingRolloutReceipt,
    BillingRosterControl,
    BillingSubscription,
    BillingTopupCapabilityReceipt,
    BillingTopupOrder,
    BillingWebhookDelivery,
)

_ENVIRONMENTS: Final = frozenset({"development", "staging", "production", "test"})
_ROLLOUT_STATES: Final = frozenset({"disabled", "canary_allowlist", "general"})
_CANARY_TOPUP_OFFER: Final = "package_credits_500_v1"
_CANARY_SUBSCRIPTION_OFFER: Final = "plan_plus_monthly_v1"
_CANARY_CATALOG: Final = "payment-2026-10-03"
_CANARY_TOPUP_AMOUNT_CENTS: Final = 10_000
_CANARY_TOPUP_CREDIT_GRANT_MC: Final = 500
_CANARY_SUBSCRIPTION_AMOUNT_CENTS: Final = 99_900


class BillingStateUnavailable(RuntimeError):
    """Raised when a required persisted control row is absent."""


class BillingRevisionConflict(RuntimeError):
    """Raised when optimistic state no longer matches the caller's revision."""


class BillingTransitionRejected(RuntimeError):
    """Raised before mutation when a requested transition is not allowed."""


class BillingCapabilityRejected(RuntimeError):
    """Raised when top-up enablement lacks current reconciled evidence."""


class BillingCanaryBudgetExceeded(BillingTransitionRejected):
    """Raised before egress when a persisted canary limit is exhausted."""


@dataclass(frozen=True)
class BillingTransitionEvidence:
    """Sanitized evidence persisted with one state transition."""

    nonce: UUID
    deployment_digest: str
    actor_user_id: UUID
    expires_at: datetime


def billing_advisory_key(environment: str) -> int:
    """Return a stable signed PostgreSQL bigint lock key for an environment."""

    if environment not in _ENVIRONMENTS:
        raise ValueError("unsupported billing environment")
    digest = hashlib.sha256(
        f"neurostack:billing-control:{environment}".encode("ascii")
    ).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def billing_roster_advisory_key() -> int:
    """Return the one global roster lock key used by every environment."""

    digest = hashlib.sha256(b"neurostack:billing-roster-control:global").digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _validate_evidence(evidence: BillingTransitionEvidence) -> None:
    if evidence.expires_at.tzinfo is None:
        raise BillingTransitionRejected("transition evidence must be timezone-aware")
    if evidence.expires_at <= datetime.now(timezone.utc):
        raise BillingTransitionRejected("transition evidence has expired")
    if len(evidence.deployment_digest) != 64 or any(
        character not in "0123456789abcdef" for character in evidence.deployment_digest
    ):
        raise BillingTransitionRejected("invalid deployment digest")


def _capability_is_valid(
    capability: BillingTopupCapabilityReceipt,
    *,
    environment: str,
    now: datetime,
) -> bool:
    return bool(
        capability.environment == environment
        and capability.schema_version == "credit_capability_ready.v1"
        and capability.approved_at <= now
        and capability.expires_at > now
        and capability.reconciliation_complete
        and capability.readback_complete
        and not capability.has_open_gap
        and not capability.has_backlog
        and not capability.has_alert
    )


class BillingRepository:
    """Serialize billing transitions without owning commit or rollback."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def _lock_environment(self, environment: str) -> None:
        await self.session.execute(
            select(func.pg_advisory_xact_lock(billing_advisory_key(environment)))
        )

    async def _lock_rollout(self, environment: str) -> BillingRolloutControl:
        await self._lock_environment(environment)
        result = await self.session.execute(
            select(BillingRolloutControl)
            .where(BillingRolloutControl.environment == environment)
            .with_for_update()
        )
        control = result.scalar_one_or_none()
        if control is None:
            raise BillingStateUnavailable("billing rollout control is unavailable")
        return control

    async def transition_rollout(
        self,
        *,
        environment: str,
        expected_revision: int,
        to_state: str,
        evidence: BillingTransitionEvidence,
        canary_builder_id: UUID | None = None,
    ) -> BillingRolloutControl:
        """Move rollout state under advisory/row locks and optimistic revision."""

        if to_state not in _ROLLOUT_STATES:
            raise BillingTransitionRejected("unsupported rollout state")
        _validate_evidence(evidence)
        control = await self._lock_rollout(environment)
        if control.rollout_revision != expected_revision:
            raise BillingRevisionConflict("rollout revision changed")

        allowed = (
            (control.state == "disabled" and to_state == "canary_allowlist")
            or (control.state == "canary_allowlist" and to_state == "general")
            or (control.state != "disabled" and to_state == "disabled")
        )
        if not allowed:
            raise BillingTransitionRejected("rollout transition is not allowed")
        if to_state == "canary_allowlist" and canary_builder_id is None:
            raise BillingTransitionRejected("canary transition requires a builder")
        if to_state != "canary_allowlist" and canary_builder_id is not None:
            raise BillingTransitionRejected("builder is only valid for canary")
        if to_state == "canary_allowlist":
            for checkout_id in (
                control.canary_subscription_checkout_id,
                control.canary_topup_checkout_id,
            ):
                if checkout_id is None:
                    continue
                checkout = await self.session.get(BillingCheckoutSession, checkout_id)
                if checkout is None or checkout.builder_id != canary_builder_id:
                    raise BillingTransitionRejected(
                        "canary lifetime slots belong to another builder"
                    )

        from_state = control.state
        new_revision = expected_revision + 1
        receipt = BillingRolloutReceipt(
            environment=environment,
            receipt_kind="rollout_transition",
            nonce=evidence.nonce,
            deployment_digest=evidence.deployment_digest,
            from_state=from_state,
            to_state=to_state,
            expected_revision=expected_revision,
            resulting_revision=new_revision,
            actor_user_id=evidence.actor_user_id,
            expires_at=evidence.expires_at,
        )
        self.session.add(receipt)
        await self.session.flush([receipt])
        control.state = to_state
        control.canary_builder_id = canary_builder_id
        control.rollout_revision = new_revision
        control.rollout_receipt_id = receipt.id
        control.updated_at = func.now()
        await self.session.flush()
        return control

    async def set_topup_commercial_enabled(
        self,
        *,
        environment: str,
        expected_revision: int,
        enabled: bool,
        evidence: BillingTransitionEvidence,
        capability_receipt_id: UUID | None = None,
    ) -> BillingRolloutControl:
        """Change only the independent top-up gate with validated capability."""

        _validate_evidence(evidence)
        control = await self._lock_rollout(environment)
        if control.topup_revision != expected_revision:
            raise BillingRevisionConflict("topup revision changed")
        if control.topup_commercial_enabled == enabled:
            raise BillingTransitionRejected("topup gate is already in requested state")

        capability: BillingTopupCapabilityReceipt | None = None
        if enabled:
            if capability_receipt_id is None:
                raise BillingCapabilityRejected("topup capability is required")
            result = await self.session.execute(
                select(BillingTopupCapabilityReceipt)
                .where(
                    BillingTopupCapabilityReceipt.environment == environment,
                    BillingTopupCapabilityReceipt.id == capability_receipt_id,
                )
                .with_for_update()
            )
            capability = result.scalar_one_or_none()
            if capability is None or not _capability_is_valid(
                capability,
                environment=environment,
                now=datetime.now(timezone.utc),
            ):
                raise BillingCapabilityRejected("topup capability is not valid")
            consumed = await self.session.scalar(
                select(func.count(BillingRolloutReceipt.id)).where(
                    BillingRolloutReceipt.environment == environment,
                    BillingRolloutReceipt.capability_receipt_id
                    == capability_receipt_id,
                )
            )
            if consumed:
                raise BillingCapabilityRejected("topup capability was already consumed")
        elif capability_receipt_id is not None:
            raise BillingTransitionRejected(
                "disabled topup gate cannot retain a capability"
            )

        from_state = (
            "topup_enabled" if control.topup_commercial_enabled else "topup_disabled"
        )
        to_state = "topup_enabled" if enabled else "topup_disabled"
        new_revision = expected_revision + 1
        receipt = BillingRolloutReceipt(
            environment=environment,
            receipt_kind="topup_gate_transition",
            nonce=evidence.nonce,
            deployment_digest=evidence.deployment_digest,
            from_state=from_state,
            to_state=to_state,
            expected_revision=expected_revision,
            resulting_revision=new_revision,
            capability_receipt_id=(capability.id if capability is not None else None),
            actor_user_id=evidence.actor_user_id,
            expires_at=evidence.expires_at,
        )
        self.session.add(receipt)
        await self.session.flush([receipt])
        control.topup_commercial_enabled = enabled
        control.active_topup_capability_id = (
            capability.id if capability is not None else None
        )
        control.topup_revision = new_revision
        control.topup_receipt_id = receipt.id
        control.updated_at = func.now()
        await self.session.flush()
        return control

    async def rebase_legacy_migration(
        self,
        *,
        migration_id: UUID,
        expected_source_revision: int,
    ) -> int:
        """Rebase one cancelled legacy saga; provider flip owns advancement."""
        await self.session.execute(
            select(func.pg_advisory_xact_lock(billing_roster_advisory_key()))
        )
        roster_result = await self.session.execute(
            select(BillingRosterControl).with_for_update()
        )
        control = roster_result.scalar_one_or_none()
        if control is None:
            raise BillingStateUnavailable("billing roster control is unavailable")
        migration_result = await self.session.execute(
            select(BillingMigration)
            .where(BillingMigration.id == migration_id)
            .with_for_update()
        )
        migration = migration_result.scalar_one_or_none()
        if migration is None or migration.state != "legacy_cancelled":
            raise BillingStateUnavailable("legacy migration is unavailable")
        if migration.source_roster_revision != expected_source_revision:
            raise BillingRevisionConflict("migration source revision changed")
        subscription_result = await self.session.execute(
            select(BillingSubscription)
            .where(BillingSubscription.id == migration.subscription_id)
            .with_for_update()
        )
        subscription = subscription_result.scalar_one_or_none()
        if (
            subscription is None
            or subscription.roster_revision != expected_source_revision
        ):
            raise BillingRevisionConflict("subscription source revision changed")
        subscription.roster_revision = control.billing_roster_revision
        await self.session.flush()
        return subscription.roster_revision

    async def authorize_and_reserve_topup_checkout(
        self,
        *,
        environment: str,
        builder_id: UUID,
        topup_order_id: UUID,
        checkout_session_id: UUID,
        attempt_no: int = 1,
    ) -> BillingCheckoutSession:
        """Authorize and reserve one top-up attempt before provider egress.

        Group 2 must call this inside its caller-owned transaction and commit the
        returned ``creating`` session before performing any network operation.
        The rollout lock, canonical order validation, canary budget reservation,
        and live-attempt insertion are one atomic database unit.
        """

        control = await self._lock_rollout(environment)
        if control.state == "disabled":
            raise BillingTransitionRejected("billing rollout is disabled")

        order_result = await self.session.execute(
            select(BillingTopupOrder)
            .where(
                BillingTopupOrder.id == topup_order_id,
                BillingTopupOrder.builder_id == builder_id,
            )
            .with_for_update()
        )
        order = order_result.scalar_one_or_none()
        if order is None or order.state != "pending":
            raise BillingTransitionRejected("canonical topup order is unavailable")

        if control.state == "canary_allowlist":
            if control.canary_builder_id != builder_id:
                raise BillingTransitionRejected("builder is not canary-authorized")
            if (
                order.catalog_version_id != _CANARY_CATALOG
                or order.offer_id != _CANARY_TOPUP_OFFER
                or order.amount_cents != _CANARY_TOPUP_AMOUNT_CENTS
                or order.credit_grant_mc != _CANARY_TOPUP_CREDIT_GRANT_MC
            ):
                raise BillingTransitionRejected("topup is outside the canary offer")
            if (
                control.canary_topup_checkout_id is not None
                and control.canary_topup_checkout_id != checkout_session_id
            ):
                raise BillingCanaryBudgetExceeded("canary topup slot is consumed")
            if control.canary_topup_checkout_id == checkout_session_id:
                existing = await self.session.get(
                    BillingCheckoutSession, checkout_session_id
                )
                if existing is None or existing.topup_order_id != topup_order_id:
                    raise BillingTransitionRejected("canary topup identity mismatch")
                return existing
            control.updated_at = func.now()
        elif control.state == "general":
            if (
                not control.topup_commercial_enabled
                or control.active_topup_capability_id is None
            ):
                raise BillingCapabilityRejected("topup capability is required")
            capability_result = await self.session.execute(
                select(BillingTopupCapabilityReceipt)
                .where(
                    BillingTopupCapabilityReceipt.environment == environment,
                    BillingTopupCapabilityReceipt.id
                    == control.active_topup_capability_id,
                )
                .with_for_update()
            )
            capability = capability_result.scalar_one_or_none()
            if capability is None or not _capability_is_valid(
                capability,
                environment=environment,
                now=datetime.now(timezone.utc),
            ):
                raise BillingCapabilityRejected("topup capability is not valid")
        else:
            raise BillingTransitionRejected("unsupported rollout state")

        checkout = BillingCheckoutSession(
            id=checkout_session_id,
            builder_id=builder_id,
            intent_kind="topup",
            intent_id=order.id,
            migration_id=None,
            topup_order_id=order.id,
            attempt_no=attempt_no,
            provider="asaas",
            state="creating",
            catalog_version_id=order.catalog_version_id,
            offer_id=order.offer_id,
            amount_cents=order.amount_cents,
            pricing_snapshot=order.pricing_snapshot,
            canary_environment=(
                environment if control.state == "canary_allowlist" else None
            ),
            canary_slot=("topup" if control.state == "canary_allowlist" else None),
        )
        self.session.add(checkout)
        await self.session.flush()
        if control.state == "canary_allowlist":
            control.canary_topup_checkout_id = checkout.id
            control.canary_checkout_count += 1
            control.canary_amount_reserved_cents += order.amount_cents
            await self.session.flush()
        return checkout

    async def reserve_canary_subscription_checkout(
        self,
        *,
        environment: str,
        builder_id: UUID,
        intent_id: UUID,
        checkout_session_id: UUID,
        next_due_date: date,
    ) -> BillingCheckoutSession:
        """Create and reserve the exact Plus canary checkout atomically."""

        control = await self._lock_rollout(environment)
        if (
            control.state != "canary_allowlist"
            or control.canary_builder_id != builder_id
        ):
            raise BillingTransitionRejected(
                "subscription is outside the active canary offer"
            )
        if control.canary_subscription_checkout_id is not None:
            if control.canary_subscription_checkout_id != checkout_session_id:
                raise BillingCanaryBudgetExceeded(
                    "canary subscription slot is consumed"
                )
            existing = await self.session.get(
                BillingCheckoutSession, checkout_session_id
            )
            if (
                existing is None
                or existing.intent_id != intent_id
                or existing.next_due_date != next_due_date
            ):
                raise BillingTransitionRejected("canary subscription identity mismatch")
            return existing

        checkout = BillingCheckoutSession(
            id=checkout_session_id,
            builder_id=builder_id,
            intent_kind="subscription",
            intent_id=intent_id,
            migration_id=None,
            topup_order_id=None,
            attempt_no=1,
            provider="asaas",
            state="creating",
            catalog_version_id=_CANARY_CATALOG,
            offer_id=_CANARY_SUBSCRIPTION_OFFER,
            amount_cents=_CANARY_SUBSCRIPTION_AMOUNT_CENTS,
            pricing_snapshot={
                "catalog_version_id": _CANARY_CATALOG,
                "offer_id": _CANARY_SUBSCRIPTION_OFFER,
                "amount_cents": _CANARY_SUBSCRIPTION_AMOUNT_CENTS,
                "credit_grant_mc": 5_000,
                "included_clinics": 1,
                "billing_cycle": "monthly",
                "payment_method": "credit_card",
                "lines": [
                    {
                        "line_id": "plan",
                        "offer_id": _CANARY_SUBSCRIPTION_OFFER,
                        "kind": "plan",
                        "quantity": 1,
                        "unit_amount_cents": _CANARY_SUBSCRIPTION_AMOUNT_CENTS,
                        "line_amount_cents": _CANARY_SUBSCRIPTION_AMOUNT_CENTS,
                        "credit_grant_mc": 5_000,
                        "settled_cents": 0,
                        "reversed_cents": 0,
                        "restored_cents": 0,
                        "line_financial_state": "pending",
                    }
                ],
            },
            canary_environment=environment,
            canary_slot="subscription",
            next_due_date=next_due_date,
        )
        self.session.add(checkout)
        await self.session.flush()
        control.canary_subscription_checkout_id = checkout.id
        control.canary_checkout_count += 1
        control.canary_amount_reserved_cents += _CANARY_SUBSCRIPTION_AMOUNT_CENTS
        control.updated_at = func.now()
        await self.session.flush()
        return checkout

    async def reserve_canary_settlement(
        self,
        *,
        environment: str,
        payment_effect_id: UUID,
    ) -> BillingPaymentEffect:
        """Bind one persisted canonical settlement to its typed canary slot."""

        control = await self._lock_rollout(environment)
        result = await self.session.execute(
            select(
                BillingPaymentEffect,
                BillingPaymentProjection,
                BillingCheckoutSession,
            )
            .join(
                BillingPaymentProjection,
                and_(
                    BillingPaymentProjection.builder_id
                    == BillingPaymentEffect.builder_id,
                    BillingPaymentProjection.id == BillingPaymentEffect.projection_id,
                    BillingPaymentProjection.provider == BillingPaymentEffect.provider,
                    BillingPaymentProjection.payment_id
                    == BillingPaymentEffect.payment_id,
                ),
            )
            .join(
                BillingCheckoutSession,
                and_(
                    BillingCheckoutSession.builder_id
                    == BillingPaymentProjection.builder_id,
                    BillingCheckoutSession.id
                    == BillingPaymentProjection.checkout_session_id,
                ),
            )
            .where(BillingPaymentEffect.id == payment_effect_id)
            .with_for_update()
        )
        row = result.one_or_none()
        if row is None:
            raise BillingTransitionRejected("canonical settlement is unavailable")
        effect = cast(BillingPaymentEffect, row[0])
        projection = cast(BillingPaymentProjection, row[1])
        checkout = cast(BillingCheckoutSession, row[2])
        slot = effect.canary_slot
        if slot is None:
            raise BillingTransitionRejected("settlement is not part of active canary")
        expected_amount = {
            "subscription": _CANARY_SUBSCRIPTION_AMOUNT_CENTS,
            "topup": _CANARY_TOPUP_AMOUNT_CENTS,
        }.get(slot)
        checkout_slot_attribute = f"canary_{slot}_checkout_id"
        expected_checkout_id = getattr(control, checkout_slot_attribute, None)
        if (
            effect.effect_kind != "settlement"
            or effect.source_revision != "settlement:v1"
            or effect.canary_environment != environment
            or projection.canary_environment != environment
            or projection.canary_slot != slot
            or projection.status != "settled"
            or projection.settled_cents != projection.amount_cents
            or projection.reversed_cents != 0
            or projection.restored_cents != 0
            or checkout.canary_environment != environment
            or checkout.canary_slot != slot
            or expected_checkout_id != checkout.id
            or expected_amount is None
            or effect.amount_cents != expected_amount
            or projection.amount_cents != expected_amount
            or checkout.amount_cents != expected_amount
        ):
            raise BillingTransitionRejected("settlement is not part of active canary")
        slot_attribute = f"canary_{slot}_effect_id"
        existing_effect_id = getattr(control, slot_attribute)
        if existing_effect_id == effect.id:
            return effect
        if existing_effect_id is not None:
            raise BillingCanaryBudgetExceeded("canary settlement slot is consumed")
        setattr(control, slot_attribute, effect.id)
        control.canary_settlement_count += 1
        control.updated_at = func.now()
        await self.session.flush()
        return effect

    async def record_payment_effect(
        self,
        *,
        delivery: BillingWebhookDelivery,
        projection: BillingPaymentProjection,
        effect: BillingPaymentEffect,
    ) -> None:
        """Flush delivery, projection, and effect as one caller-owned unit."""

        self.session.add_all((delivery, projection, effect))
        await self.session.flush()

    async def record_topup_outbox(
        self,
        *,
        order: BillingTopupOrder,
        outbox_event: BillingOutboxEvent,
    ) -> None:
        """Flush order and immutable fact as one caller-owned unit."""

        self.session.add_all((order, outbox_event))
        await self.session.flush()
