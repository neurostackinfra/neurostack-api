"""Cryptographic, local-only evidence for an ASAAS secret rotation.

This module never reads or changes a secret and never contacts ASAAS.  It
accepts only signed, typed operation/readback receipts.  The booleans and
opaque identifiers used by the earlier proof shape remain deliberately
insufficient to authorize a rotation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Final, Literal
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from src.services.billing_operations import is_sha256_digest

RotationReceiptKind = Literal[
    "candidate_validation",
    "webhook_overlap",
    "promotion",
    "revocation",
    "compromise_recovery",
]
_RECEIPT_KINDS: Final[frozenset[str]] = frozenset(
    {
        "candidate_validation",
        "webhook_overlap",
        "promotion",
        "revocation",
        "compromise_recovery",
    }
)


# DESIGN: the webhook accepts ``current`` and ``next`` for at most 15 minutes.
MAX_WEBHOOK_OVERLAP: Final = timedelta(minutes=15)
# The only legal order of a rotation's receipts (strictly increasing
# ``observed_at``): prove the new key, open the overlap, promote next->current,
# revoke/remove current, then reconcile from the last trusted instant.
LEGAL_RECEIPT_ORDER: Final[tuple[RotationReceiptKind, ...]] = (
    "candidate_validation",
    "webhook_overlap",
    "promotion",
    "revocation",
    "compromise_recovery",
)


class AsaasSecretRotationRejected(RuntimeError):
    """Fixed failure for incomplete, stale, or unauthenticated evidence."""

    code = "asaas_secret_rotation_rejected"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class AsaasSecretRotationReceipt:
    """One immutable signed operation/readback fact; never contains a secret."""

    receipt_id: UUID
    operation_id: UUID
    deployment_digest: str
    actor_user_id: UUID
    kind: RotationReceiptKind
    evidence_digest: str
    observed_at: datetime
    expires_at: datetime
    signature: str
    current_fingerprint: str | None = None
    candidate_fingerprint: str | None = None
    webhook_overlap_started_at: datetime | None = None
    webhook_overlap_ended_at: datetime | None = None
    rollout_disabled_for_recovery: bool = False
    reconciled_from: datetime | None = None


def _receipt_payload(receipt: AsaasSecretRotationReceipt) -> dict[str, object]:
    def _timestamp(value: datetime | None) -> str | None:
        return value.astimezone(timezone.utc).isoformat() if value is not None else None

    return {
        "actor_user_id": str(receipt.actor_user_id),
        "candidate_fingerprint": receipt.candidate_fingerprint,
        "current_fingerprint": receipt.current_fingerprint,
        "deployment_digest": receipt.deployment_digest,
        "evidence_digest": receipt.evidence_digest,
        "expires_at": _timestamp(receipt.expires_at),
        "kind": receipt.kind,
        "observed_at": _timestamp(receipt.observed_at),
        "operation_id": str(receipt.operation_id),
        "receipt_id": str(receipt.receipt_id),
        "reconciled_from": _timestamp(receipt.reconciled_from),
        "rollout_disabled_for_recovery": receipt.rollout_disabled_for_recovery,
        "webhook_overlap_ended_at": _timestamp(receipt.webhook_overlap_ended_at),
        "webhook_overlap_started_at": _timestamp(receipt.webhook_overlap_started_at),
    }


def _receipt_content_payload(receipt: AsaasSecretRotationReceipt) -> dict[str, object]:
    payload = _receipt_payload(receipt)
    payload.pop("evidence_digest")
    return payload


def secret_rotation_receipt_digest(receipt: AsaasSecretRotationReceipt) -> str:
    """Return the digest of the exact signed, non-secret receipt payload."""

    encoded = json.dumps(
        _receipt_content_payload(receipt), sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def sign_secret_rotation_receipt(
    receipt: AsaasSecretRotationReceipt, signing_key: bytes
) -> str:
    """Create a fixture/operator signature; the verifier uses only the public key."""

    if len(signing_key) != 32:
        raise AsaasSecretRotationRejected("asaas_secret_rotation_key_invalid")
    return Ed25519PrivateKey.from_private_bytes(signing_key).sign(
        json.dumps(_receipt_payload(receipt), sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        )
    ).hex()


def _public_key(value: bytes | str) -> Ed25519PublicKey:
    if isinstance(value, str):
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise AsaasSecretRotationRejected("asaas_secret_rotation_key_invalid")
        value = bytes.fromhex(value)
    if not isinstance(value, bytes) or len(value) != 32:
        raise AsaasSecretRotationRejected("asaas_secret_rotation_key_invalid")
    try:
        return Ed25519PublicKey.from_public_bytes(value)
    except ValueError as exc:
        raise AsaasSecretRotationRejected("asaas_secret_rotation_key_invalid") from exc


def _verify_receipt(
    receipt: AsaasSecretRotationReceipt,
    *,
    public_key: Ed25519PublicKey,
    operation_id: UUID,
    deployment_digest: str,
    actor_user_id: UUID,
    now: datetime,
) -> None:
    if (
        receipt.kind not in _RECEIPT_KINDS
        or receipt.operation_id != operation_id
        or receipt.deployment_digest != deployment_digest
        or receipt.actor_user_id != actor_user_id
        or not is_sha256_digest(receipt.deployment_digest)
        or not is_sha256_digest(receipt.evidence_digest)
        or receipt.observed_at.tzinfo is None
        or receipt.expires_at.tzinfo is None
        or receipt.observed_at > now
        or receipt.expires_at <= now
        or receipt.expires_at <= receipt.observed_at
        or not re.fullmatch(r"[0-9a-f]{128}", receipt.signature)
        or secret_rotation_receipt_digest(receipt) != receipt.evidence_digest
    ):
        raise AsaasSecretRotationRejected("asaas_secret_rotation_receipt_invalid")
    try:
        public_key.verify(
            bytes.fromhex(receipt.signature),
            json.dumps(
                _receipt_payload(receipt), sort_keys=True, separators=(",", ":")
            ).encode("ascii"),
        )
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise AsaasSecretRotationRejected(
            "asaas_secret_rotation_receipt_unverified"
        ) from exc


@dataclass(frozen=True, slots=True)
class AsaasSecretRotationProof:
    """A rotation bundle whose every material claim is signed evidence."""

    current_fingerprint: str
    next_fingerprint: str
    deployed_current_fingerprint: str
    smoke_digest: str
    observed_at: datetime
    incident_open: bool
    operation_id: UUID | None = None
    deployment_digest: str | None = None
    actor_user_id: UUID | None = None
    candidate_validation_receipt: AsaasSecretRotationReceipt | None = None
    webhook_overlap_receipt: AsaasSecretRotationReceipt | None = None
    promotion_receipt: AsaasSecretRotationReceipt | None = None
    revocation_receipt: AsaasSecretRotationReceipt | None = None
    compromise_recovery_receipt: AsaasSecretRotationReceipt | None = None


def verify_next_to_current(
    proof: AsaasSecretRotationProof,
    *,
    evidence_public_key: bytes | str,
    expected_operation_id: UUID | None = None,
    expected_deployment_digest: str | None = None,
    expected_actor_user_id: UUID | None = None,
) -> None:
    """Verify a complete signed rotation/readback bundle before any swap.

    Expected identity values are supplied by the authenticated operation
    boundary.  Values merely asserted inside the bundle cannot become
    authority.  The function is intentionally pure and performs no rotation.
    """

    if not all(
        is_sha256_digest(value)
        for value in (
            proof.current_fingerprint,
            proof.next_fingerprint,
            proof.deployed_current_fingerprint,
            proof.smoke_digest,
        )
    ):
        raise AsaasSecretRotationRejected("asaas_secret_rotation_digest_invalid")
    if proof.observed_at.tzinfo is None or proof.observed_at > datetime.now(timezone.utc):
        raise AsaasSecretRotationRejected("asaas_secret_rotation_time_invalid")
    if proof.incident_open:
        raise AsaasSecretRotationRejected("asaas_secret_rotation_incident_open")
    if proof.current_fingerprint == proof.next_fingerprint:
        raise AsaasSecretRotationRejected("asaas_secret_rotation_not_staged")
    operation_id = expected_operation_id or proof.operation_id
    deployment_digest = expected_deployment_digest or proof.deployment_digest
    actor_user_id = expected_actor_user_id or proof.actor_user_id
    if (
        not isinstance(operation_id, UUID)
        or not isinstance(actor_user_id, UUID)
        or not is_sha256_digest(deployment_digest)
    ):
        raise AsaasSecretRotationRejected("asaas_secret_rotation_identity_unbound")
    assert isinstance(deployment_digest, str)
    public_key = _public_key(evidence_public_key)
    receipts = (
        proof.candidate_validation_receipt,
        proof.webhook_overlap_receipt,
        proof.promotion_receipt,
        proof.revocation_receipt,
        proof.compromise_recovery_receipt,
    )
    if any(receipt is None for receipt in receipts):
        raise AsaasSecretRotationRejected("asaas_secret_rotation_evidence_incomplete")
    typed_receipts = tuple(receipt for receipt in receipts if receipt is not None)
    if len({receipt.receipt_id for receipt in typed_receipts}) != len(typed_receipts):
        raise AsaasSecretRotationRejected("asaas_secret_rotation_receipt_reused")
    now = datetime.now(timezone.utc)
    for receipt in typed_receipts:
        _verify_receipt(
            receipt,
            public_key=public_key,
            operation_id=operation_id,
            deployment_digest=deployment_digest,
            actor_user_id=actor_user_id,
            now=now,
        )
    candidate, overlap, promotion, revocation, recovery = typed_receipts
    if (
        candidate.kind != "candidate_validation"
        or candidate.candidate_fingerprint != proof.next_fingerprint
        or candidate.evidence_digest != proof.smoke_digest
    ):
        raise AsaasSecretRotationRejected("asaas_secret_rotation_candidate_unverified")
    if (
        overlap.kind != "webhook_overlap"
        or overlap.candidate_fingerprint != proof.next_fingerprint
        or overlap.webhook_overlap_started_at is None
        or overlap.webhook_overlap_ended_at is None
        or overlap.webhook_overlap_started_at.tzinfo is None
        or overlap.webhook_overlap_ended_at.tzinfo is None
        or overlap.webhook_overlap_ended_at < overlap.webhook_overlap_started_at
        or overlap.webhook_overlap_ended_at - overlap.webhook_overlap_started_at
        > MAX_WEBHOOK_OVERLAP
    ):
        raise AsaasSecretRotationRejected("asaas_secret_rotation_overlap_unproven")
    if (
        promotion.kind != "promotion"
        or promotion.current_fingerprint != proof.next_fingerprint
        or promotion.candidate_fingerprint != proof.next_fingerprint
        or proof.deployed_current_fingerprint != proof.next_fingerprint
    ):
        raise AsaasSecretRotationRejected("asaas_secret_rotation_promotion_unproven")
    if (
        revocation.kind != "revocation"
        or revocation.current_fingerprint != proof.current_fingerprint
    ):
        raise AsaasSecretRotationRejected("asaas_secret_rotation_revocation_unproven")
    if (
        recovery.kind != "compromise_recovery"
        or not recovery.rollout_disabled_for_recovery
        or recovery.reconciled_from is None
        or recovery.reconciled_from > proof.observed_at
    ):
        raise AsaasSecretRotationRejected(
            "asaas_secret_rotation_compromise_recovery_unproven"
        )
    _verify_rotation_sequence(candidate, overlap, promotion, revocation, recovery)


def _verify_rotation_sequence(
    candidate: AsaasSecretRotationReceipt,
    overlap: AsaasSecretRotationReceipt,
    promotion: AsaasSecretRotationReceipt,
    revocation: AsaasSecretRotationReceipt,
    recovery: AsaasSecretRotationReceipt,
) -> None:
    """Require the legal order and an overlap window measured by the receipts.

    Each receipt was already verified individually (signature, the single
    rotation ``operation_id``, deployment and actor).  Here the bundle must
    also be one sequence: every step observed strictly after the previous
    one, so a missing, reordered, duplicated or same-instant step cannot pass.

    The overlap is bounded by evidence, not by its own claim alone: it may
    start only after the candidate key was proven and no later than its own
    readback; the readback of the revocation of ``current`` closes it and must
    fall inside the signed overlap end.  The resulting window
    ``revocation.observed_at - overlap start`` is at most 15 minutes.
    """

    sequence = (candidate, overlap, promotion, revocation, recovery)
    if tuple(receipt.kind for receipt in sequence) != LEGAL_RECEIPT_ORDER or any(
        later.observed_at <= earlier.observed_at
        for earlier, later in zip(sequence, sequence[1:])
    ):
        raise AsaasSecretRotationRejected("asaas_secret_rotation_order_invalid")
    started = overlap.webhook_overlap_started_at
    ended = overlap.webhook_overlap_ended_at
    assert started is not None and ended is not None  # checked by the caller
    if not candidate.observed_at <= started <= overlap.observed_at:
        raise AsaasSecretRotationRejected("asaas_secret_rotation_order_invalid")
    if (
        revocation.observed_at > ended
        or revocation.observed_at - started > MAX_WEBHOOK_OVERLAP
    ):
        raise AsaasSecretRotationRejected("asaas_secret_rotation_overlap_unproven")
    # Reconciliation starts from the last trusted instant, before the
    # rotation itself began.
    assert recovery.reconciled_from is not None  # checked by the caller
    if recovery.reconciled_from > candidate.observed_at:
        raise AsaasSecretRotationRejected("asaas_secret_rotation_order_invalid")
