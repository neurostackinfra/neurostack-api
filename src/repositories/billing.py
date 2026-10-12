"""Caller-owned transactional repository for billing state boundaries."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Final, cast
from uuid import UUID

from sqlalchemy import and_, case, func, or_, select, update
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
from src.models.coupon import Coupon
from src.models.transaction import Transaction

_ENVIRONMENTS: Final = frozenset({"development", "staging", "production", "test"})
_ROLLOUT_STATES: Final = frozenset({"disabled", "canary_allowlist", "general"})
_CANARY_TOPUP_OFFER: Final = "package_credits_500_v1"
_CANARY_SUBSCRIPTION_OFFER: Final = "plan_plus_monthly_v1"
_CANARY_CATALOG: Final = "payment-2026-10-03"
_CANARY_TOPUP_AMOUNT_CENTS: Final = 10_000
_CANARY_TOPUP_CREDIT_GRANT_MC: Final = 500
_CANARY_SUBSCRIPTION_AMOUNT_CENTS: Final = 99_900
_LIVE_CHECKOUT_STATES: Final = (
    "creating",
    "active",
    "paid_pending_subscription",
    "reconciliation_required",
    "terminal_pending_readback",
)
_TERMINAL_SUBSCRIPTION_STATUSES: Final = ("inactive", "cancelled")


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
            # The engine session keeps instances across commits
            # (expire_on_commit=False); a locked read must never return a
            # still-referenced pre-commit copy of the control row.
            .execution_options(populate_existing=True)
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

    async def lock_rollout_control(self, *, environment: str) -> BillingRolloutControl:
        """Serialize checkout eligibility for one environment.

        Takes the same advisory+row lock as every rollout transition and
        reservation, so eligibility reads, live-session reuse and the insert
        of a ``creating`` attempt form one serialized decision.
        """

        return await self._lock_rollout(environment)

    async def reserve_general_subscription_checkout(
        self,
        *,
        environment: str,
        builder_id: UUID,
        intent_id: UUID,
        migration_id: UUID | None,
        checkout_session_id: UUID,
        attempt_no: int,
        catalog_version_id: str,
        offer_id: str,
        amount_cents: int,
        pricing_snapshot: dict[str, object],
        next_due_date: date,
    ) -> BillingCheckoutSession:
        """Insert one ``creating`` subscription attempt under ``general`` rollout.

        ``disabled`` and ``canary_allowlist`` are rejected here; the canary has
        its own budgeted reservation (``reserve_canary_subscription_checkout``).
        The caller commits before any provider egress.
        """

        control = await self._lock_rollout(environment)
        if control.state != "general":
            raise BillingTransitionRejected(
                "subscription checkout requires general rollout"
            )
        if migration_id is not None and intent_id != migration_id:
            raise BillingTransitionRejected("migration attempt must reuse its intent")
        checkout = BillingCheckoutSession(
            id=checkout_session_id,
            builder_id=builder_id,
            intent_kind="subscription",
            intent_id=intent_id,
            migration_id=migration_id,
            topup_order_id=None,
            attempt_no=attempt_no,
            provider="asaas",
            state="creating",
            catalog_version_id=catalog_version_id,
            offer_id=offer_id,
            amount_cents=amount_cents,
            pricing_snapshot=pricing_snapshot,
            canary_environment=None,
            canary_slot=None,
            next_due_date=next_due_date,
        )
        self.session.add(checkout)
        await self.session.flush()
        return checkout

    async def lock_checkout_session(
        self,
        *,
        builder_id: UUID,
        checkout_session_id: UUID,
    ) -> BillingCheckoutSession | None:
        """Row-lock one checkout attempt inside its builder's tenant scope."""

        result = await self.session.execute(
            select(BillingCheckoutSession)
            .where(
                BillingCheckoutSession.id == checkout_session_id,
                BillingCheckoutSession.builder_id == builder_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def subscription_checkout_blocker(self, *, builder_id: UUID) -> str | None:
        """Return why a NEW subscription checkout is ineligible, else ``None``.

        The caller must hold the environment rollout lock
        (``lock_rollout_control``) until its reservation commits: every
        checkout creation takes that lock first, so two creations for the same
        builder serialize and the second reads the first's committed attempt.
        Only id columns are selected, so no stale identity-map copy is read and
        no row (the builder's or anyone else's) is locked.

        Blockers, in order: an unresolved provider row (``provider IS NULL``:
        legacy cancel/unknown/manual states), an active or otherwise
        non-terminal AbacatePay or ASAAS row (any status except
        ``inactive``/``cancelled``, or ``is_active``), any
        ``billing_migrations`` row (adoption of ``pending``/
        ``recreate_pending`` arrives in Group 5, so every migration state
        fails closed here), and any live subscription checkout attempt.
        """

        subscription = (
            await self.session.execute(
                select(BillingSubscription.id, BillingSubscription.provider)
                .where(
                    BillingSubscription.builder_id == builder_id,
                    or_(
                        BillingSubscription.provider.is_(None),
                        BillingSubscription.is_active.is_(True),
                        BillingSubscription.status.not_in(
                            _TERMINAL_SUBSCRIPTION_STATUSES
                        ),
                    ),
                )
                .order_by(BillingSubscription.id)
                .limit(1)
            )
        ).first()
        if subscription is not None:
            provider = subscription[1]
            return f"{provider}_subscription" if provider else "unresolved_subscription"
        migration = await self.session.scalar(
            select(BillingMigration.id)
            .where(BillingMigration.builder_id == builder_id)
            .limit(1)
        )
        if migration is not None:
            return "migration"
        live = await self.session.scalar(
            select(BillingCheckoutSession.id)
            .where(
                BillingCheckoutSession.builder_id == builder_id,
                BillingCheckoutSession.intent_kind == "subscription",
                BillingCheckoutSession.state.in_(_LIVE_CHECKOUT_STATES),
            )
            .limit(1)
        )
        if live is not None:
            return "live_subscription_checkout"
        return None

    async def authorize_reserved_checkout_egress(
        self,
        *,
        environment: str,
        builder_id: UUID,
        checkout_session_id: UUID,
    ) -> BillingCheckoutSession:
        """Re-authorize one committed ``creating`` attempt before its POST.

        Takes the rollout advisory and row locks, then the attempt's row lock,
        and re-applies the reservation's authorization to the current control
        row: a canary attempt still needs the active canary for its builder
        and to own its lifetime slot; any other attempt needs ``general`` and,
        for a top-up, the open gate with a currently valid capability and a
        still-pending order.  Raises without mutating anything; the caller
        owns the transaction and must release it before network I/O.
        """

        control = await self._lock_rollout(environment)
        checkout = await self.lock_checkout_session(
            builder_id=builder_id, checkout_session_id=checkout_session_id
        )
        if (
            checkout is None
            or checkout.state != "creating"
            or checkout.external_checkout_id is not None
        ):
            raise BillingTransitionRejected("checkout attempt is not awaiting egress")

        if checkout.canary_slot is not None:
            slot_checkout_id = {
                "subscription": control.canary_subscription_checkout_id,
                "topup": control.canary_topup_checkout_id,
            }.get(checkout.canary_slot)
            if (
                control.state != "canary_allowlist"
                or control.canary_builder_id != builder_id
                or checkout.canary_environment != environment
                or slot_checkout_id != checkout.id
            ):
                raise BillingTransitionRejected(
                    "canary reservation is no longer authorized"
                )
            return checkout

        if control.state != "general":
            raise BillingTransitionRejected("checkout egress requires general rollout")
        if checkout.intent_kind == "subscription":
            return checkout
        if checkout.intent_kind != "topup" or checkout.topup_order_id is None:
            raise BillingTransitionRejected("unsupported checkout intent")
        if (
            not control.topup_commercial_enabled
            or control.active_topup_capability_id is None
        ):
            raise BillingCapabilityRejected("topup capability is required")
        capability = await self.session.scalar(
            select(BillingTopupCapabilityReceipt).where(
                BillingTopupCapabilityReceipt.environment == environment,
                BillingTopupCapabilityReceipt.id == control.active_topup_capability_id,
            )
        )
        if capability is None or not _capability_is_valid(
            capability,
            environment=environment,
            now=datetime.now(timezone.utc),
        ):
            raise BillingCapabilityRejected("topup capability is not valid")
        order_state = await self.session.scalar(
            select(BillingTopupOrder.state).where(
                BillingTopupOrder.id == checkout.topup_order_id,
                BillingTopupOrder.builder_id == builder_id,
            )
        )
        if order_state != "pending":
            raise BillingTransitionRejected("canonical topup order is unavailable")
        return checkout

    async def consume_checkout_coupon(
        self,
        *,
        builder_id: UUID,
        transaction_id: UUID,
    ) -> str | None:
        """Consume the checkout snapshot's coupon once for a settled session.

        Group 3 calls this in the caller-owned unit that records the settlement
        ``Transaction`` (``coupon_code`` still NULL). The originating checkout
        session row is locked first, so concurrent settlements of the same
        session serialize; only the first Transaction of a session receives the
        coupon, and the compare-and-set on ``transactions.coupon_code`` makes a
        replay of the same call a no-op. The coupon counter follows the legacy
        settlement semantics: ``uses_count + 1`` and deactivation at
        ``max_uses``, applied atomically in SQL without a read-modify-write.
        """

        result = await self.session.execute(
            select(Transaction, BillingCheckoutSession)
            .join(
                BillingPaymentEffect,
                and_(
                    BillingPaymentEffect.builder_id == Transaction.builder_id,
                    BillingPaymentEffect.id == Transaction.billing_payment_effect_id,
                    BillingPaymentEffect.provider == Transaction.provider,
                    BillingPaymentEffect.payment_id == Transaction.provider_payment_id,
                ),
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
            .where(
                Transaction.id == transaction_id,
                Transaction.builder_id == builder_id,
                BillingPaymentEffect.effect_kind == "settlement",
                BillingPaymentEffect.source_revision == "settlement:v1",
            )
        )
        row = result.one_or_none()
        if row is None:
            raise BillingTransitionRejected("settled checkout transaction unavailable")
        checkout = cast(BillingCheckoutSession, row[1])
        locked = await self.lock_checkout_session(
            builder_id=builder_id, checkout_session_id=checkout.id
        )
        if locked is None:
            raise BillingTransitionRejected("settled checkout transaction unavailable")
        snapshot_coupon = (locked.pricing_snapshot or {}).get("coupon_code")
        if not isinstance(snapshot_coupon, str) or not snapshot_coupon:
            return None

        session_transactions = (
            select(Transaction.id)
            .join(
                BillingPaymentEffect,
                and_(
                    BillingPaymentEffect.builder_id == Transaction.builder_id,
                    BillingPaymentEffect.id == Transaction.billing_payment_effect_id,
                ),
            )
            .join(
                BillingPaymentProjection,
                and_(
                    BillingPaymentProjection.builder_id
                    == BillingPaymentEffect.builder_id,
                    BillingPaymentProjection.id == BillingPaymentEffect.projection_id,
                ),
            )
            .where(
                Transaction.builder_id == builder_id,
                BillingPaymentProjection.checkout_session_id == locked.id,
                Transaction.coupon_code.is_not(None),
            )
            .limit(1)
        )
        if (await self.session.execute(session_transactions)).first() is not None:
            return None

        marked = await self.session.execute(
            update(Transaction)
            .where(
                Transaction.id == transaction_id,
                Transaction.builder_id == builder_id,
                Transaction.coupon_code.is_(None),
            )
            .values(coupon_code=snapshot_coupon)
            .returning(Transaction.id)
        )
        if marked.first() is None:
            return None
        await self.session.execute(
            update(Coupon)
            .where(Coupon.code == snapshot_coupon.upper())
            .values(
                uses_count=Coupon.uses_count + 1,
                is_active=case(
                    (
                        and_(
                            Coupon.max_uses.is_not(None),
                            Coupon.uses_count + 1 >= Coupon.max_uses,
                        ),
                        False,
                    ),
                    else_=Coupon.is_active,
                ),
            )
        )
        return snapshot_coupon

    # ------------------------------------------------------------------
    # Group 3 webhook/projection primitives (additive).  Every method runs in
    # the caller-owned transaction and never commits.
    # ------------------------------------------------------------------

    def _dialect_name(self) -> str:
        return str(self.session.get_bind().dialect.name)

    def _insert(self, model: type[object]) -> Any:
        if self._dialect_name() == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as postgresql_insert

            return postgresql_insert(model)
        # SQLite is the unit-test dialect; both support ON CONFLICT DO NOTHING.
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        return sqlite_insert(model)

    async def reserve_webhook_delivery(self, values: dict[str, object]) -> bool:
        """Insert one delivery envelope unless ``(provider, event_id)`` exists.

        ``ON CONFLICT DO NOTHING`` makes a concurrent identical delivery wait on
        the unique index until the first transaction ends and then insert
        nothing, so exactly one transaction owns each provider event.
        """

        statement = (
            self._insert(BillingWebhookDelivery)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["provider", "event_id"])
            .returning(BillingWebhookDelivery.id)
        )
        result = await self.session.execute(statement)
        return result.scalar_one_or_none() is not None

    async def lock_webhook_delivery(
        self, *, provider: str, event_id: str
    ) -> BillingWebhookDelivery | None:
        """Row-lock one delivery by its idempotency key."""

        result = await self.session.execute(
            select(BillingWebhookDelivery)
            .where(
                BillingWebhookDelivery.provider == provider,
                BillingWebhookDelivery.event_id == event_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def lock_webhook_delivery_by_id(
        self, *, delivery_id: UUID
    ) -> BillingWebhookDelivery | None:
        """Row-lock one delivery by its internal identifier."""

        result = await self.session.execute(
            select(BillingWebhookDelivery)
            .where(BillingWebhookDelivery.id == delivery_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def lock_billing_roster(self) -> int:
        """Take the global roster fence and return the current revision.

        Same advisory key and row lock as every roster writer and the
        statement-level roster trigger, so a subscription row created here is
        serialized against roster imports, legacy cancellation and flips.
        """

        if self._dialect_name() == "postgresql":
            await self.session.execute(
                select(func.pg_advisory_xact_lock(billing_roster_advisory_key()))
            )
        result = await self.session.execute(
            select(BillingRosterControl)
            .where(BillingRosterControl.singleton.is_(True))
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        control = result.scalar_one_or_none()
        if control is None:
            raise BillingStateUnavailable("billing roster control is unavailable")
        return int(control.billing_roster_revision)

    async def lock_subscription(
        self, *, builder_id: UUID, subscription_id: UUID
    ) -> BillingSubscription | None:
        """Row-lock one canonical subscription inside its builder scope."""

        result = await self.session.execute(
            select(BillingSubscription)
            .where(
                BillingSubscription.id == subscription_id,
                BillingSubscription.builder_id == builder_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def lock_topup_order(
        self, *, builder_id: UUID, order_id: UUID
    ) -> BillingTopupOrder | None:
        """Row-lock one top-up order inside its builder scope."""

        result = await self.session.execute(
            select(BillingTopupOrder)
            .where(
                BillingTopupOrder.id == order_id,
                BillingTopupOrder.builder_id == builder_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def insert_payment_projection_if_absent(
        self, values: dict[str, object]
    ) -> None:
        """Create the one projection of a provider payment unless it exists."""

        statement = (
            self._insert(BillingPaymentProjection)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["provider", "payment_id"])
        )
        await self.session.execute(statement)

    async def lock_payment_projection(
        self, *, provider: str, payment_id: str
    ) -> BillingPaymentProjection | None:
        """Row-lock the projection of one provider payment."""

        result = await self.session.execute(
            select(BillingPaymentProjection)
            .where(
                BillingPaymentProjection.provider == provider,
                BillingPaymentProjection.payment_id == payment_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def find_payment_effect(
        self,
        *,
        provider: str,
        payment_id: str,
        effect_kind: str,
        source_revision: str,
    ) -> BillingPaymentEffect | None:
        """Read one effect by its ``(provider, payment, kind, revision)`` key."""

        result = await self.session.execute(
            select(BillingPaymentEffect)
            .where(
                BillingPaymentEffect.provider == provider,
                BillingPaymentEffect.payment_id == payment_id,
                BillingPaymentEffect.effect_kind == effect_kind,
                BillingPaymentEffect.source_revision == source_revision,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()
