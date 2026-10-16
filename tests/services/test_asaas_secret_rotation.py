"""Secret rotation accepts only signed, identity-bound local evidence."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.services.asaas_secret_rotation import (
    AsaasSecretRotationProof,
    AsaasSecretRotationReceipt,
    AsaasSecretRotationRejected,
    secret_rotation_receipt_digest,
    sign_secret_rotation_receipt,
    verify_next_to_current,
)

_PRIVATE_KEY = bytes(range(1, 33))
_PUBLIC_KEY = (
    Ed25519PrivateKey.from_private_bytes(_PRIVATE_KEY).public_key().public_bytes_raw()
)


def _receipt(
    kind: str,
    *,
    operation_id,
    deployment_digest: str,
    actor_user_id,
    observed_at: datetime,
    **overrides: object,
) -> AsaasSecretRotationReceipt:
    values: dict[str, object] = {
        "receipt_id": uuid4(),
        "operation_id": operation_id,
        "deployment_digest": deployment_digest,
        "actor_user_id": actor_user_id,
        "kind": kind,
        "evidence_digest": "",
        "observed_at": observed_at,
        "expires_at": observed_at + timedelta(minutes=10),
        "signature": "",
    }
    values.update(overrides)
    unsigned = AsaasSecretRotationReceipt(**values)  # type: ignore[arg-type]
    signed_content = replace(
        unsigned, evidence_digest=secret_rotation_receipt_digest(unsigned)
    )
    return replace(
        signed_content,
        signature=sign_secret_rotation_receipt(signed_content, _PRIVATE_KEY),
    )


def _proof(
    **overrides: object,
) -> tuple[AsaasSecretRotationProof, bytes, object, object, object]:
    now = datetime.now(timezone.utc)
    operation_id = uuid4()
    actor_id = uuid4()
    deployment_digest = "d" * 64
    current = "a" * 64
    next_fingerprint = "b" * 64
    candidate_base = _receipt(
        "candidate_validation",
        operation_id=operation_id,
        deployment_digest=deployment_digest,
        actor_user_id=actor_id,
        observed_at=now - timedelta(minutes=9),
        candidate_fingerprint=next_fingerprint,
    )
    candidate_digest = candidate_base.evidence_digest
    candidate = replace(candidate_base)
    overlap = _receipt(
        "webhook_overlap",
        operation_id=operation_id,
        deployment_digest=deployment_digest,
        actor_user_id=actor_id,
        observed_at=now - timedelta(minutes=7),
        candidate_fingerprint=next_fingerprint,
        # Legal sequence: the overlap opens after the candidate key is proven
        # and is closed by the revocation readback below (7 minutes).
        webhook_overlap_started_at=now - timedelta(minutes=8),
        webhook_overlap_ended_at=now - timedelta(minutes=1),
    )
    promotion = _receipt(
        "promotion",
        operation_id=operation_id,
        deployment_digest=deployment_digest,
        actor_user_id=actor_id,
        observed_at=now - timedelta(minutes=4),
        current_fingerprint=next_fingerprint,
        candidate_fingerprint=next_fingerprint,
    )
    revocation = _receipt(
        "revocation",
        operation_id=operation_id,
        deployment_digest=deployment_digest,
        actor_user_id=actor_id,
        observed_at=now - timedelta(minutes=1),
        current_fingerprint=current,
    )
    recovery = _receipt(
        "compromise_recovery",
        operation_id=operation_id,
        deployment_digest=deployment_digest,
        actor_user_id=actor_id,
        observed_at=now - timedelta(seconds=30),
        rollout_disabled_for_recovery=True,
        reconciled_from=now - timedelta(minutes=20),
    )
    proof = AsaasSecretRotationProof(
        current_fingerprint=current,
        next_fingerprint=next_fingerprint,
        deployed_current_fingerprint=next_fingerprint,
        smoke_digest=candidate_digest,
        observed_at=now,
        incident_open=False,
        operation_id=operation_id,
        deployment_digest=deployment_digest,
        actor_user_id=actor_id,
        candidate_validation_receipt=candidate,
        webhook_overlap_receipt=overlap,
        promotion_receipt=promotion,
        revocation_receipt=revocation,
        compromise_recovery_receipt=recovery,
    )
    values = {
        "proof": proof,
        "public_key": _PUBLIC_KEY,
        "operation_id": operation_id,
        "actor_id": actor_id,
    }
    values.update(overrides)
    return (
        values["proof"],
        values["public_key"],
        values["operation_id"],
        actor_id,
        deployment_digest,
    )


def test_rotation_requires_signed_evidence_for_every_boundary() -> None:
    proof, public_key, operation_id, actor_id, deployment_digest = _proof()

    verify_next_to_current(
        proof,
        evidence_public_key=public_key,
        expected_operation_id=operation_id,
        expected_deployment_digest=deployment_digest,
        expected_actor_user_id=actor_id,
    )


def test_caller_booleans_and_opaque_ids_cannot_replace_receipts() -> None:
    proof, public_key, operation_id, actor_id, deployment_digest = _proof()
    incomplete = replace(proof, candidate_validation_receipt=None)

    with pytest.raises(AsaasSecretRotationRejected, match="evidence_incomplete"):
        verify_next_to_current(
            incomplete,
            evidence_public_key=public_key,
            expected_operation_id=operation_id,
            expected_deployment_digest=deployment_digest,
            expected_actor_user_id=actor_id,
        )


def test_rotation_rejects_tampered_or_cross_operation_receipts() -> None:
    proof, public_key, operation_id, actor_id, deployment_digest = _proof()
    tampered = replace(
        proof,
        promotion_receipt=replace(proof.promotion_receipt, signature="0" * 128),
    )
    with pytest.raises(AsaasSecretRotationRejected, match="receipt_unverified"):
        verify_next_to_current(
            tampered,
            evidence_public_key=public_key,
            expected_operation_id=operation_id,
            expected_deployment_digest=deployment_digest,
            expected_actor_user_id=actor_id,
        )

    with pytest.raises(AsaasSecretRotationRejected, match="receipt_invalid"):
        verify_next_to_current(
            proof,
            evidence_public_key=public_key,
            expected_operation_id=uuid4(),
            expected_deployment_digest=deployment_digest,
            expected_actor_user_id=actor_id,
        )


def test_rotation_rejects_overlap_longer_than_fifteen_minutes() -> None:
    proof, public_key, operation_id, actor_id, deployment_digest = _proof()
    overlap = proof.webhook_overlap_receipt
    assert overlap is not None
    expired_overlap = replace(
        overlap,
        webhook_overlap_started_at=overlap.webhook_overlap_ended_at
        - timedelta(minutes=16),
    )
    with pytest.raises(AsaasSecretRotationRejected, match="receipt_invalid"):
        verify_next_to_current(
            replace(proof, webhook_overlap_receipt=expired_overlap),
            evidence_public_key=public_key,
            expected_operation_id=operation_id,
            expected_deployment_digest=deployment_digest,
            expected_actor_user_id=actor_id,
        )


# ---------------------------------------------------------------------------
# Fix cycle 7 (HIGH-3): one rotation, one legal order, a receipt-measured window.
# ---------------------------------------------------------------------------


def _sequence(
    *,
    candidate: float = -14,
    overlap: float = -12,
    started: float = -13,
    ended: float = -1,
    promotion: float = -6,
    revocation: float = -1,
    recovery: float = -0.5,
    reconciled_from: float = -30,
    foreign_kind: str | None = None,
) -> tuple[AsaasSecretRotationProof, object, object, str]:
    """A fully re-signed bundle; offsets are minutes relative to now."""

    now = datetime.now(timezone.utc)
    at = lambda minutes: now + timedelta(minutes=minutes)  # noqa: E731
    operation_id, actor_id, deployment_digest = uuid4(), uuid4(), "d" * 64
    current, next_fingerprint = "a" * 64, "b" * 64

    def signed(
        kind: str, minutes: float, **extra: object
    ) -> AsaasSecretRotationReceipt:
        return _receipt(
            kind,
            operation_id=uuid4() if kind == foreign_kind else operation_id,
            deployment_digest=deployment_digest,
            actor_user_id=actor_id,
            observed_at=at(minutes),
            expires_at=now + timedelta(minutes=30),
            **extra,
        )

    candidate_receipt = signed(
        "candidate_validation", candidate, candidate_fingerprint=next_fingerprint
    )
    proof = AsaasSecretRotationProof(
        current_fingerprint=current,
        next_fingerprint=next_fingerprint,
        deployed_current_fingerprint=next_fingerprint,
        smoke_digest=candidate_receipt.evidence_digest,
        observed_at=now,
        incident_open=False,
        operation_id=operation_id,
        deployment_digest=deployment_digest,
        actor_user_id=actor_id,
        candidate_validation_receipt=candidate_receipt,
        webhook_overlap_receipt=signed(
            "webhook_overlap",
            overlap,
            candidate_fingerprint=next_fingerprint,
            webhook_overlap_started_at=at(started),
            webhook_overlap_ended_at=at(ended),
        ),
        promotion_receipt=signed(
            "promotion",
            promotion,
            current_fingerprint=next_fingerprint,
            candidate_fingerprint=next_fingerprint,
        ),
        revocation_receipt=signed(
            "revocation", revocation, current_fingerprint=current
        ),
        compromise_recovery_receipt=signed(
            "compromise_recovery",
            recovery,
            rollout_disabled_for_recovery=True,
            reconciled_from=at(reconciled_from),
        ),
    )
    return proof, operation_id, actor_id, deployment_digest


def _verify(bundle: tuple[AsaasSecretRotationProof, object, object, str]) -> None:
    proof, operation_id, actor_id, deployment_digest = bundle
    verify_next_to_current(
        proof,
        evidence_public_key=_PUBLIC_KEY,
        expected_operation_id=operation_id,  # type: ignore[arg-type]
        expected_deployment_digest=deployment_digest,
        expected_actor_user_id=actor_id,  # type: ignore[arg-type]
    )


def test_legal_rotation_sequence_is_accepted() -> None:
    _verify(_sequence())


@pytest.mark.parametrize(
    "offsets",
    [
        # Reordered: promotion observed before the overlap readback.
        {"promotion": -12.5},
        # Equal instants are not an order.
        {"overlap": -14, "started": -14},
        {"revocation": -6, "ended": -1},
        # Revocation before promotion.
        {"revocation": -7, "ended": -1},
        # Overlap opened before the new key was proven.
        {"started": -14.5},
        # Overlap readback claims a start after its own observation.
        {"started": -11},
        # Recovery observed before revocation, and reconciliation that starts
        # after the rotation already began.
        {"recovery": -2},
        {"reconciled_from": -13},
    ],
)
def test_out_of_order_rotation_steps_are_rejected(offsets: dict[str, float]) -> None:
    with pytest.raises(
        AsaasSecretRotationRejected, match="^asaas_secret_rotation_order_invalid$"
    ):
        _verify(_sequence(**offsets))


def test_overlap_window_is_measured_from_receipts_at_most_fifteen_minutes() -> None:
    # Window = revocation readback - overlap start: exactly 15:00 is legal.
    _verify(
        _sequence(candidate=-17, started=-16, overlap=-15.5, ended=-1, revocation=-1)
    )
    # 15:01 measured by the receipts although the signed claim says 15:00.
    with pytest.raises(
        AsaasSecretRotationRejected, match="^asaas_secret_rotation_overlap_unproven$"
    ):
        _verify(
            _sequence(
                candidate=-17,
                started=-(16 + 1 / 60),
                overlap=-15.5,
                ended=-(1 + 1 / 60),
                revocation=-1,
            )
        )
    # The signed overlap claim itself is 15:01.
    with pytest.raises(
        AsaasSecretRotationRejected, match="^asaas_secret_rotation_overlap_unproven$"
    ):
        _verify(
            _sequence(
                candidate=-17,
                started=-(16 + 1 / 60),
                overlap=-15.5,
                ended=-1,
                revocation=-1.5,
            )
        )
    # Current revoked only after the signed overlap end.
    with pytest.raises(
        AsaasSecretRotationRejected, match="^asaas_secret_rotation_overlap_unproven$"
    ):
        _verify(_sequence(ended=-2, revocation=-1))


def test_missing_or_duplicated_steps_are_rejected() -> None:
    proof, operation_id, actor_id, deployment_digest = _sequence()
    with pytest.raises(
        AsaasSecretRotationRejected, match="^asaas_secret_rotation_evidence_incomplete$"
    ):
        _verify(
            (
                replace(proof, candidate_validation_receipt=None),
                operation_id,
                actor_id,
                deployment_digest,
            )
        )
    with pytest.raises(
        AsaasSecretRotationRejected, match="^asaas_secret_rotation_receipt_reused$"
    ):
        _verify(
            (
                replace(proof, promotion_receipt=proof.webhook_overlap_receipt),
                operation_id,
                actor_id,
                deployment_digest,
            )
        )


@pytest.mark.parametrize(
    "kind",
    [
        "candidate_validation",
        "webhook_overlap",
        "promotion",
        "revocation",
        "compromise_recovery",
    ],
)
def test_a_receipt_from_another_rotation_is_rejected(kind: str) -> None:
    with pytest.raises(
        AsaasSecretRotationRejected, match="^asaas_secret_rotation_receipt_invalid$"
    ):
        _verify(_sequence(foreign_kind=kind))
