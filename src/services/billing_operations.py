"""Typed, signed operator evidence for billing control-plane actions.

The payment provider is deliberately absent from this module.  These values
are the local, auditable boundary that must be satisfied before a caller can
change a persisted billing state.  Keeping the signing payload closed avoids
accidentally turning an operator receipt into a PII or secret transport.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Final, Literal, Sequence, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.billing import BillingOperationAssignment, BillingOperationReceipt
from src.observability.metrics import BILLING_OPERATION_RECEIPTS_TOTAL
from src.repositories.billing import billing_advisory_key

OperationScope = Literal["migration", "rollout", "topup_capability", "incident"]
OperatorRole = Literal["deployer", "approver", "reviewer"]

OPERATOR_ROLES: Final[frozenset[str]] = frozenset({"deployer", "approver", "reviewer"})
OPERATION_SCOPES: Final[frozenset[str]] = frozenset(
    {"migration", "rollout", "topup_capability", "incident"}
)
_SHA256_HEX: Final = frozenset("0123456789abcdef")


class BillingOperationRejected(RuntimeError):
    """A fixed, non-sensitive administrative-operation failure."""

    code = "billing_operation_rejected"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(self.code)


def is_sha256_digest(value: object) -> bool:
    """Return whether *value* is the canonical lowercase SHA-256 encoding."""

    return bool(
        isinstance(value, str)
        and len(value) == 64
        and all(character in _SHA256_HEX for character in value)
    )


def require_aware_future(value: datetime, *, now: datetime | None = None) -> None:
    """Reject naive, expired, and zero-lifetime operator evidence."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise BillingOperationRejected("billing_operation_expiry_invalid")
    current = now or datetime.now(timezone.utc)
    if value <= current:
        raise BillingOperationRejected("billing_operation_expired")


def operation_payload_digest(
    *,
    scope: OperationScope,
    operation_id: UUID,
    environment: str,
    from_state: str,
    to_state: str,
    budget_digest: str,
    deployment_digest: str,
    expires_at: datetime,
) -> str:
    """Hash the closed receipt payload without encoding identities or secrets."""

    if scope not in OPERATION_SCOPES:
        raise BillingOperationRejected("billing_operation_scope_invalid")
    if not environment or len(environment) > 20:
        raise BillingOperationRejected("billing_operation_environment_invalid")
    if not from_state or not to_state or len(from_state) > 40 or len(to_state) > 40:
        raise BillingOperationRejected("billing_operation_state_invalid")
    if not is_sha256_digest(budget_digest) or not is_sha256_digest(deployment_digest):
        raise BillingOperationRejected("billing_operation_digest_invalid")
    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise BillingOperationRejected("billing_operation_expiry_invalid")
    canonical = {
        "budget_digest": budget_digest,
        "deployment_digest": deployment_digest,
        "environment": environment,
        "expires_at": expires_at.astimezone(timezone.utc).isoformat(),
        "from_state": from_state,
        "operation_id": str(operation_id),
        "scope": scope,
        "to_state": to_state,
    }
    encoded = json.dumps(
        canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def sign_operation_payload(payload_digest: str, signing_key: bytes) -> str:
    """Create an HMAC proof; callers must not log either input."""

    if not is_sha256_digest(payload_digest) or not signing_key:
        raise BillingOperationRejected("billing_operation_signature_invalid")
    return hmac.new(
        signing_key, payload_digest.encode("ascii"), hashlib.sha256
    ).hexdigest()


def verify_operation_signature(
    *, payload_digest: str, signature: str, signing_key: bytes
) -> None:
    """Fail closed when a receipt cannot be authenticated."""

    if not is_sha256_digest(signature):
        raise BillingOperationRejected("billing_operation_signature_invalid")
    expected = sign_operation_payload(payload_digest, signing_key)
    if not hmac.compare_digest(expected, signature):
        raise BillingOperationRejected("billing_operation_signature_invalid")


@dataclass(frozen=True, slots=True)
class OperatorReceipt:
    """One already-authenticated receipt used in a three-person quorum."""

    id: UUID
    assignment_id: UUID
    operation_id: UUID
    scope: OperationScope
    role: OperatorRole
    actor_user_id: UUID
    environment: str
    nonce: UUID
    payload_digest: str
    deployment_digest: str
    budget_digest: str
    from_state: str
    to_state: str
    expires_at: datetime
    applied_at: datetime | None = None


def require_operator_quorum(
    receipts: Sequence[OperatorReceipt],
    *,
    scope: OperationScope,
    operation_id: UUID,
    environment: str,
    payload_digest: str,
    material: "OperationMaterial | None" = None,
    now: datetime | None = None,
) -> None:
    """Require exactly deployer/approver/reviewer, each a distinct actor.

    The caller locks receipt rows before invoking this pure check.  The
    function intentionally refuses extras, reused receipts, differing
    operation material, expired evidence, and an actor holding more than one
    duty; those are all ambiguous authorization states.
    """

    current = now or datetime.now(timezone.utc)
    if len(receipts) != 3:
        raise BillingOperationRejected("billing_operation_quorum_incomplete")
    roles = {receipt.role for receipt in receipts}
    if roles != OPERATOR_ROLES:
        raise BillingOperationRejected("billing_operation_roles_invalid")
    if len({receipt.actor_user_id for receipt in receipts}) != 3:
        raise BillingOperationRejected("billing_operation_actor_conflict")
    if len({receipt.nonce for receipt in receipts}) != 3:
        raise BillingOperationRejected("billing_operation_nonce_conflict")
    for receipt in receipts:
        if (
            receipt.scope != scope
            or receipt.operation_id != operation_id
            or receipt.environment != environment
            or receipt.payload_digest != payload_digest
            or receipt.applied_at is not None
            or receipt.expires_at.tzinfo is None
            or receipt.expires_at <= current
            or (
                material is not None
                and (
                    receipt.deployment_digest != material.deployment_digest
                    or receipt.budget_digest != material.budget_digest
                    or receipt.from_state != material.from_state
                    or receipt.to_state != material.to_state
                    or receipt.expires_at != material.expires_at
                )
            )
        ):
            raise BillingOperationRejected("billing_operation_receipt_invalid")


@dataclass(frozen=True, slots=True)
class OperationMaterial:
    """The common, closed values that every operator signs for one action."""

    scope: OperationScope
    operation_id: UUID
    environment: str
    from_state: str
    to_state: str
    budget_digest: str
    deployment_digest: str
    expires_at: datetime

    @property
    def payload_digest(self) -> str:
        return operation_payload_digest(
            scope=self.scope,
            operation_id=self.operation_id,
            environment=self.environment,
            from_state=self.from_state,
            to_state=self.to_state,
            budget_digest=self.budget_digest,
            deployment_digest=self.deployment_digest,
            expires_at=self.expires_at,
        )


class BillingOperationAuthority:
    """Persist and consume signed operator evidence without committing.

    Every caller owns its transaction.  A rollout/capability service invokes
    ``consume_quorum`` while holding this environment lock, then performs its
    protected state transition under the same PostgreSQL advisory lock.
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        signing_key: bytes,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._signing_key = signing_key
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def assign(
        self,
        *,
        material: OperationMaterial,
        role: OperatorRole,
        actor_user_id: UUID,
    ) -> BillingOperationAssignment:
        """Create one assignment; database uniqueness enforces separation."""

        if role not in OPERATOR_ROLES:
            raise BillingOperationRejected("billing_operation_role_invalid")
        require_aware_future(material.expires_at, now=self._clock())
        assignment = BillingOperationAssignment(
            operation_id=material.operation_id,
            environment=material.environment,
            scope=material.scope,
            role=role,
            actor_user_id=actor_user_id,
            payload_digest=material.payload_digest,
            expires_at=material.expires_at,
            active=True,
        )
        self._session.add(assignment)
        await self._session.flush()
        return assignment

    async def record_receipt(
        self,
        *,
        assignment_id: UUID,
        actor_user_id: UUID,
        nonce: UUID,
        material: OperationMaterial,
        signature: str,
        evidence_type: str | None = None,
        request_id: UUID | None = None,
        inbox_high_watermark: int | None = None,
    ) -> BillingOperationReceipt:
        """Verify and append one single-use receipt for its assigned actor."""

        if not self._signing_key:
            raise BillingOperationRejected("billing_operation_signing_unavailable")
        if evidence_type not in {
            None,
            "producer",
            "credit",
            "reconciliation",
            "verification",
        }:
            raise BillingOperationRejected("billing_operation_evidence_type_invalid")
        if inbox_high_watermark is not None and inbox_high_watermark < 0:
            raise BillingOperationRejected("billing_operation_watermark_invalid")
        fact_schema_digest: str | None = None
        if evidence_type is not None:
            # The database/service boundary derives this from the deployed
            # contract.  A caller can provide the role payload digest, but it
            # cannot choose which fact schema the receipt claims to attest.
            from src.services.billing_outbox import (
                authoritative_billing_fact_schema_digest,
            )

            try:
                fact_schema_digest = authoritative_billing_fact_schema_digest(
                    material.environment
                )
            except Exception as exc:
                raise BillingOperationRejected(
                    "billing_operation_fact_schema_unavailable"
                ) from exc
        require_aware_future(material.expires_at, now=self._clock())
        result = await self._session.execute(
            select(BillingOperationAssignment)
            .where(BillingOperationAssignment.id == assignment_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assignment = result.scalar_one_or_none()
        if (
            assignment is None
            or not assignment.active
            or assignment.consumed_at is not None
            or assignment.actor_user_id != actor_user_id
            or assignment.operation_id != material.operation_id
            or assignment.environment != material.environment
            or assignment.scope != material.scope
            or assignment.payload_digest != material.payload_digest
            or assignment.expires_at != material.expires_at
        ):
            raise BillingOperationRejected("billing_operation_assignment_invalid")
        verify_operation_signature(
            payload_digest=material.payload_digest,
            signature=signature,
            signing_key=self._signing_key,
        )
        receipt = BillingOperationReceipt(
            assignment_id=assignment.id,
            operation_id=material.operation_id,
            environment=material.environment,
            scope=material.scope,
            role=assignment.role,
            actor_user_id=actor_user_id,
            nonce=nonce,
            payload_digest=material.payload_digest,
            deployment_digest=material.deployment_digest,
            budget_digest=material.budget_digest,
            from_state=material.from_state,
            to_state=material.to_state,
            evidence_type=evidence_type,
            request_id=request_id,
            inbox_high_watermark=inbox_high_watermark,
            billing_fact_schema_digest=fact_schema_digest,
            expires_at=material.expires_at,
            signature_sha256=signature,
        )
        self._session.add(receipt)
        await self._session.flush()
        BILLING_OPERATION_RECEIPTS_TOTAL.labels(
            role=assignment.role, outcome="accepted"
        ).inc()
        return receipt

    async def revoke_assignment(self, *, assignment_id: UUID) -> None:
        """Revoke an unused assignment without rewriting its audit history."""

        result = await self._session.execute(
            select(BillingOperationAssignment)
            .where(BillingOperationAssignment.id == assignment_id)
            .with_for_update()
        )
        assignment = result.scalar_one_or_none()
        if assignment is None:
            raise BillingOperationRejected("billing_operation_assignment_invalid")
        if assignment.consumed_at is not None:
            raise BillingOperationRejected("billing_operation_assignment_consumed")
        assignment.active = False
        await self._session.flush()

    async def consume_quorum(
        self, *, material: OperationMaterial
    ) -> tuple[OperatorReceipt, OperatorReceipt, OperatorReceipt]:
        """Lock, validate, and consume the exact three receipts once."""

        if self._session.get_bind().dialect.name == "postgresql":
            await self._session.execute(
                select(
                    func.pg_advisory_xact_lock(
                        billing_advisory_key(material.environment)
                    )
                )
            )
        result = await self._session.execute(
            select(BillingOperationReceipt)
            .where(
                BillingOperationReceipt.operation_id == material.operation_id,
                BillingOperationReceipt.environment == material.environment,
                BillingOperationReceipt.scope == material.scope,
                BillingOperationReceipt.payload_digest == material.payload_digest,
                # Typed capability attestations share the operation identity
                # but are not part of the three-person quorum itself.
                BillingOperationReceipt.evidence_type.is_(None),
            )
            .order_by(BillingOperationReceipt.role)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        rows = result.scalars().all()
        receipts = tuple(
            OperatorReceipt(
                id=row.id,
                assignment_id=row.assignment_id,
                operation_id=row.operation_id,
                scope=cast(OperationScope, row.scope),
                role=cast(OperatorRole, row.role),
                actor_user_id=row.actor_user_id,
                environment=row.environment,
                nonce=row.nonce,
                payload_digest=row.payload_digest,
                deployment_digest=row.deployment_digest,
                budget_digest=row.budget_digest,
                from_state=row.from_state,
                to_state=row.to_state,
                expires_at=row.expires_at,
                applied_at=row.applied_at,
            )
            for row in rows
        )
        require_operator_quorum(
            receipts,
            scope=material.scope,
            operation_id=material.operation_id,
            environment=material.environment,
            payload_digest=material.payload_digest,
            material=material,
            now=self._clock(),
        )
        applied_at = self._clock()
        for row in rows:
            row.applied_at = applied_at
        await self._session.flush()
        for receipt in receipts:
            BILLING_OPERATION_RECEIPTS_TOTAL.labels(
                role=receipt.role, outcome="consumed"
            ).inc()
        if len(receipts) != 3:  # Narrow tuple shape for strict callers.
            raise BillingOperationRejected("billing_operation_quorum_incomplete")
        return receipts[0], receipts[1], receipts[2]
