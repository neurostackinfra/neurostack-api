"""Fail-closed top-up capability activation and revocation.

This module is the only local control-plane path that can request
``topup_commercial_enabled: false -> true``.  It binds producer, credit,
reconciliation, capability and verification evidence once, then delegates the
final gate mutation to the existing repository transaction boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Final
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.billing import (
    BillingOperationReceipt,
    BillingRolloutControl,
    BillingTopupCapabilityBinding,
    BillingTopupCapabilityReceipt,
)
from src.observability.metrics import BILLING_TOPUP_CAPABILITY_TRANSITIONS_TOTAL
from src.repositories.billing import BillingRepository, BillingTransitionEvidence
from src.services.billing_operations import (
    BillingOperationAuthority,
    BillingOperationRejected,
    OperationMaterial,
    is_sha256_digest,
)
from src.services.billing_outbox import authoritative_billing_fact_schema_digest

_ENVIRONMENTS: Final = frozenset({"development", "staging", "production", "test"})
# The role-specific capability digest is the signed operation payload digest.
# ``deployment_digest`` remains a separate binding for the deployed artifact;
# it must never be substituted for this attestation identity.
AUTHORITATIVE_CAPABILITY_DIGEST: Final[str] = "payload_digest"


class BillingCapabilityRejected(RuntimeError):
    """A fixed, safe top-up capability failure code."""

    code = "billing_topup_capability_rejected"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(self.code)


def authoritative_capability_digest(receipt: BillingOperationReceipt) -> str:
    """Return the one digest semantic used for typed capability attestations."""

    return receipt.payload_digest


@dataclass(frozen=True, slots=True)
class TopupCapabilityEvidence:
    """Opaque cross-service receipt identities and their binding digests."""

    environment: str
    capability_receipt_id: UUID
    producer_receipt_id: UUID
    credit_receipt_id: UUID
    reconciliation_receipt_id: UUID
    verification_receipt_id: UUID
    producer_digest: str
    credit_digest: str
    reconciliation_digest: str
    verification_digest: str
    billing_fact_schema_digest: str
    producer_actor_id: UUID
    credit_actor_id: UUID
    reconciliation_actor_id: UUID
    verification_actor_id: UUID
    verified_at: datetime
    operation_id: UUID
    deployment_digest: str
    request_id: UUID | None = None
    inbox_high_watermark: int | None = None


def validate_capability_evidence(evidence: TopupCapabilityEvidence) -> None:
    """Reject incomplete, cross-environment, reused-shaped evidence early."""

    if evidence.environment not in _ENVIRONMENTS:
        raise BillingCapabilityRejected("billing_topup_capability_environment_invalid")
    if (
        len(
            {
                evidence.capability_receipt_id,
                evidence.producer_receipt_id,
                evidence.credit_receipt_id,
                evidence.reconciliation_receipt_id,
                evidence.verification_receipt_id,
            }
        )
        != 5
    ):
        raise BillingCapabilityRejected("billing_topup_capability_receipt_conflict")
    if not all(
        is_sha256_digest(value)
        for value in (
            evidence.producer_digest,
            evidence.credit_digest,
            evidence.reconciliation_digest,
            evidence.verification_digest,
            evidence.billing_fact_schema_digest,
        )
    ):
        raise BillingCapabilityRejected("billing_topup_capability_digest_invalid")
    try:
        authoritative_schema_digest = authoritative_billing_fact_schema_digest(
            evidence.environment
        )
    except Exception as exc:
        raise BillingCapabilityRejected(
            "billing_topup_capability_fact_schema_unavailable"
        ) from exc
    if evidence.billing_fact_schema_digest != authoritative_schema_digest:
        raise BillingCapabilityRejected("billing_topup_capability_fact_schema_unbound")
    if (
        len(
            {
                evidence.producer_actor_id,
                evidence.credit_actor_id,
                evidence.reconciliation_actor_id,
                evidence.verification_actor_id,
            }
        )
        != 4
    ):
        raise BillingCapabilityRejected("billing_topup_capability_actor_conflict")
    if evidence.verified_at.tzinfo is None or evidence.verified_at.utcoffset() is None:
        raise BillingCapabilityRejected("billing_topup_capability_time_invalid")
    if not isinstance(evidence.operation_id, UUID):
        raise BillingCapabilityRejected("billing_topup_capability_operation_invalid")
    if not is_sha256_digest(evidence.deployment_digest):
        raise BillingCapabilityRejected("billing_topup_capability_digest_invalid")
    if not isinstance(evidence.request_id, UUID):
        raise BillingCapabilityRejected("billing_topup_capability_request_invalid")
    if (
        type(evidence.inbox_high_watermark) is not int
        or evidence.inbox_high_watermark < 0
    ):
        raise BillingCapabilityRejected("billing_topup_capability_watermark_invalid")


def _typed_receipt_pairs(
    evidence: TopupCapabilityEvidence,
) -> tuple[tuple[UUID, str, str, UUID], ...]:
    """Return the closed producer/verification evidence contract.

    Receipt IDs are not interchangeable slots.  Each is paired with its
    producer type, digest and actor before any database row can authorize the
    gate; a generic operation receipt therefore has no valid pair.
    """

    return (
        (
            evidence.producer_receipt_id,
            "producer",
            evidence.producer_digest,
            evidence.producer_actor_id,
        ),
        (
            evidence.credit_receipt_id,
            "credit",
            evidence.credit_digest,
            evidence.credit_actor_id,
        ),
        (
            evidence.reconciliation_receipt_id,
            "reconciliation",
            evidence.reconciliation_digest,
            evidence.reconciliation_actor_id,
        ),
        (
            evidence.verification_receipt_id,
            "verification",
            evidence.verification_digest,
            evidence.verification_actor_id,
        ),
    )


def validate_capability_receipt_binding(
    receipt: BillingOperationReceipt,
    *,
    environment: str,
    operation_id: UUID,
    request_id: UUID,
    inbox_high_watermark: int,
    deployment_digest: str,
    billing_fact_schema_digest: str,
    evidence_type: str,
    digest: str,
    actor_id: UUID,
    now: datetime,
) -> None:
    """Validate one typed producer/verification pair at the active gate."""

    if (
        receipt.environment != environment
        or receipt.operation_id != operation_id
        or receipt.evidence_type != evidence_type
        or authoritative_capability_digest(receipt) != digest
        or receipt.actor_user_id != actor_id
        or receipt.request_id != request_id
        or receipt.inbox_high_watermark != inbox_high_watermark
        or receipt.billing_fact_schema_digest != billing_fact_schema_digest
        or receipt.deployment_digest != deployment_digest
        or receipt.expires_at <= now
        or receipt.applied_at is not None
    ):
        raise BillingCapabilityRejected("billing_topup_capability_receipt_unverified")


class BillingTopupCapabilityService:
    """Atomic capability binding plus the two allowed top-up gate directions."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        signing_key: bytes,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._repository = BillingRepository(session)
        self._authority = BillingOperationAuthority(
            session, signing_key=signing_key, clock=clock
        )
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def enable(
        self,
        *,
        expected_revision: int,
        material: OperationMaterial,
        evidence: TopupCapabilityEvidence,
    ) -> BillingRolloutControl:
        """The only false-to-true top-up route; caller commits its unit."""

        validate_capability_evidence(evidence)
        try:
            authoritative_fact_schema_digest = authoritative_billing_fact_schema_digest(
                evidence.environment
            )
        except Exception as exc:
            raise BillingCapabilityRejected(
                "billing_topup_capability_fact_schema_unavailable"
            ) from exc
        if evidence.billing_fact_schema_digest != authoritative_fact_schema_digest:
            raise BillingCapabilityRejected(
                "billing_topup_capability_fact_schema_unbound"
            )
        if (
            material.scope != "topup_capability"
            or material.environment != evidence.environment
            or material.operation_id != evidence.operation_id
            or material.deployment_digest != evidence.deployment_digest
            or material.from_state != "topup_disabled"
            or material.to_state != "topup_enabled"
        ):
            raise BillingCapabilityRejected(
                "billing_topup_capability_transition_invalid"
            )
        try:
            receipts = await self._authority.consume_quorum(material=material)
        except BillingOperationRejected as exc:
            raise BillingCapabilityRejected(exc.code) from exc
        capability_result = await self._session.execute(
            select(BillingTopupCapabilityReceipt)
            .where(
                BillingTopupCapabilityReceipt.environment == evidence.environment,
                BillingTopupCapabilityReceipt.id == evidence.capability_receipt_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        capability = capability_result.scalar_one_or_none()
        now = self._clock()
        if (
            capability is None
            or capability.schema_version != "credit_capability_ready.v1"
            or evidence.request_id is None
            or capability.request_id != evidence.request_id
            or evidence.inbox_high_watermark is None
            or capability.inbox_high_watermark != evidence.inbox_high_watermark
            or capability.credit_deployment_digest != evidence.credit_digest
            or capability.producer_compatibility_digest != evidence.producer_digest
            or capability.consumer_compatibility_digest
            != evidence.reconciliation_digest
            or capability.billing_fact_schema_digest != authoritative_fact_schema_digest
            or capability.billing_fact_schema_digest
            != evidence.billing_fact_schema_digest
            or capability.operation_id != evidence.operation_id
            or capability.deployment_digest != evidence.deployment_digest
            or capability.reconciliation_receipt_id
            != evidence.reconciliation_receipt_id
            or capability.approved_by != evidence.verification_actor_id
            or capability.approved_at > now
            or capability.expires_at <= now
            or evidence.verified_at > now
            or evidence.verified_at >= capability.expires_at
            or not capability.reconciliation_complete
            or not capability.readback_complete
            or capability.has_open_gap
            or capability.has_backlog
            or capability.has_alert
        ):
            raise BillingCapabilityRejected("billing_topup_capability_incomplete")
        # IDs, digests and actors are claims, not authority.  Lock and verify
        # the four backend-issued, single-use receipts before creating the
        # binding.  The composite foreign keys make the relationship durable;
        # these checks also prevent a receipt from another operation or role
        # from being smuggled in with a valid-looking UUID/digest.
        receipt_ids = (
            evidence.producer_receipt_id,
            evidence.credit_receipt_id,
            evidence.reconciliation_receipt_id,
            evidence.verification_receipt_id,
        )
        result = await self._session.execute(
            select(BillingOperationReceipt)
            .where(
                BillingOperationReceipt.environment == evidence.environment,
                BillingOperationReceipt.id.in_(receipt_ids),
                BillingOperationReceipt.operation_id == material.operation_id,
                BillingOperationReceipt.scope == "topup_capability",
                BillingOperationReceipt.applied_at.is_(None),
            )
            .with_for_update()
        )
        receipts_by_id = {receipt.id: receipt for receipt in result.scalars()}
        expected = _typed_receipt_pairs(evidence)
        if len(receipts_by_id) != 4:
            raise BillingCapabilityRejected(
                "billing_topup_capability_receipt_unverified"
            )
        if evidence.request_id is None or evidence.inbox_high_watermark is None:
            raise BillingCapabilityRejected("billing_topup_capability_incomplete")
        for receipt_id, evidence_type, digest, actor_id in expected:
            receipt = receipts_by_id.get(receipt_id)
            if receipt is None:
                raise BillingCapabilityRejected(
                    "billing_topup_capability_receipt_unverified"
                )
            validate_capability_receipt_binding(
                receipt,
                environment=evidence.environment,
                operation_id=evidence.operation_id,
                request_id=evidence.request_id,
                inbox_high_watermark=evidence.inbox_high_watermark,
                deployment_digest=evidence.deployment_digest,
                billing_fact_schema_digest=authoritative_fact_schema_digest,
                evidence_type=evidence_type,
                digest=digest,
                actor_id=actor_id,
                now=now,
            )
        binding = BillingTopupCapabilityBinding(
            environment=evidence.environment,
            capability_receipt_id=evidence.capability_receipt_id,
            producer_receipt_id=evidence.producer_receipt_id,
            credit_receipt_id=evidence.credit_receipt_id,
            reconciliation_receipt_id=evidence.reconciliation_receipt_id,
            verification_receipt_id=evidence.verification_receipt_id,
            producer_digest=evidence.producer_digest,
            credit_digest=evidence.credit_digest,
            reconciliation_digest=evidence.reconciliation_digest,
            verification_digest=evidence.verification_digest,
            producer_actor_id=evidence.producer_actor_id,
            credit_actor_id=evidence.credit_actor_id,
            reconciliation_actor_id=evidence.reconciliation_actor_id,
            verification_actor_id=evidence.verification_actor_id,
            verified_at=evidence.verified_at,
        )
        self._session.add(binding)
        # Production sessions do not autoflush: the repository gate re-reads
        # this binding by SELECT, so it must be in the database first.
        await self._session.flush()
        deployer = next(receipt for receipt in receipts if receipt.role == "deployer")
        control = await self._repository.set_topup_commercial_enabled(
            environment=evidence.environment,
            expected_revision=expected_revision,
            enabled=True,
            evidence=BillingTransitionEvidence(
                nonce=deployer.nonce,
                deployment_digest=material.deployment_digest,
                actor_user_id=deployer.actor_user_id,
                expires_at=material.expires_at,
            ),
            capability_receipt_id=evidence.capability_receipt_id,
        )
        await self._session.flush()
        BILLING_TOPUP_CAPABILITY_TRANSITIONS_TOTAL.labels(
            direction="enable", outcome="accepted"
        ).inc()
        return control

    async def disable(
        self,
        *,
        expected_revision: int,
        material: OperationMaterial,
        reason: str,
    ) -> BillingRolloutControl:
        """Health/revocation is the only true-to-false top-up path."""

        if reason not in {"health", "revocation"}:
            raise BillingCapabilityRejected("billing_topup_capability_reason_invalid")
        if (
            material.scope != "topup_capability"
            or material.from_state != "topup_enabled"
            or material.to_state != "topup_disabled"
        ):
            raise BillingCapabilityRejected(
                "billing_topup_capability_transition_invalid"
            )
        try:
            receipts = await self._authority.consume_quorum(material=material)
        except BillingOperationRejected as exc:
            raise BillingCapabilityRejected(exc.code) from exc
        deployer = next(receipt for receipt in receipts if receipt.role == "deployer")
        control = await self._repository.set_topup_commercial_enabled(
            environment=material.environment,
            expected_revision=expected_revision,
            enabled=False,
            evidence=BillingTransitionEvidence(
                nonce=deployer.nonce,
                deployment_digest=material.deployment_digest,
                actor_user_id=deployer.actor_user_id,
                expires_at=material.expires_at,
            ),
            capability_receipt_id=None,
            disable_reason=reason,
        )
        BILLING_TOPUP_CAPABILITY_TRANSITIONS_TOTAL.labels(
            direction="disable", outcome="accepted"
        ).inc()
        return control
