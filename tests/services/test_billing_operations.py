"""Boundary tests for signed, separated, single-use operator evidence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from src.services.billing_operations import (
    BillingOperationRejected,
    OperatorReceipt,
    OperationMaterial,
    operation_payload_digest,
    require_operator_quorum,
    sign_operation_payload,
    verify_operation_signature,
)
from src.models.billing import BillingOperationReceipt, BillingRolloutReadinessReceipt
from src.services.billing_rollout import (
    _readiness_content_digest,
    _readiness_evidence_is_bound,
)


def _material() -> OperationMaterial:
    return OperationMaterial(
        scope="rollout",
        operation_id=uuid4(),
        environment="test",
        from_state="disabled",
        to_state="canary_allowlist",
        budget_digest="a" * 64,
        deployment_digest="b" * 64,
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )


def _receipt(
    material: OperationMaterial, role: str, **overrides: object
) -> OperatorReceipt:
    values: dict[str, object] = {
        "id": uuid4(),
        "assignment_id": uuid4(),
        "operation_id": material.operation_id,
        "scope": material.scope,
        "role": role,
        "actor_user_id": uuid4(),
        "environment": material.environment,
        "nonce": uuid4(),
        "payload_digest": material.payload_digest,
        "deployment_digest": material.deployment_digest,
        "budget_digest": material.budget_digest,
        "from_state": material.from_state,
        "to_state": material.to_state,
        "expires_at": material.expires_at,
        "applied_at": None,
    }
    values.update(overrides)
    return OperatorReceipt(**values)  # type: ignore[arg-type]


def test_signature_binds_all_closed_operation_material() -> None:
    material = _material()
    signature = sign_operation_payload(material.payload_digest, b"k" * 32)

    verify_operation_signature(
        payload_digest=material.payload_digest,
        signature=signature,
        signing_key=b"k" * 32,
    )
    altered = operation_payload_digest(
        scope=material.scope,
        operation_id=material.operation_id,
        environment=material.environment,
        from_state=material.from_state,
        to_state="general",
        budget_digest=material.budget_digest,
        deployment_digest=material.deployment_digest,
        expires_at=material.expires_at,
    )
    with pytest.raises(BillingOperationRejected, match="signature_invalid"):
        verify_operation_signature(
            payload_digest=altered, signature=signature, signing_key=b"k" * 32
        )


def test_quorum_requires_three_distinct_roles_actors_and_nonces() -> None:
    material = _material()
    receipts = tuple(
        _receipt(material, role) for role in ("deployer", "approver", "reviewer")
    )

    require_operator_quorum(
        receipts,
        scope=material.scope,
        operation_id=material.operation_id,
        environment=material.environment,
        payload_digest=material.payload_digest,
    )

    duplicate_actor = _receipt(
        material, "reviewer", actor_user_id=receipts[0].actor_user_id
    )
    with pytest.raises(BillingOperationRejected, match="actor_conflict"):
        require_operator_quorum(
            (receipts[0], receipts[1], duplicate_actor),
            scope=material.scope,
            operation_id=material.operation_id,
            environment=material.environment,
            payload_digest=material.payload_digest,
        )


def test_quorum_rejects_applied_or_late_receipts() -> None:
    material = _material()
    applied = _receipt(material, "deployer", applied_at=datetime.now(timezone.utc))
    with pytest.raises(BillingOperationRejected, match="receipt_invalid"):
        require_operator_quorum(
            (applied, _receipt(material, "approver"), _receipt(material, "reviewer")),
            scope=material.scope,
            operation_id=material.operation_id,
            environment=material.environment,
            payload_digest=material.payload_digest,
            material=material,
        )


def test_quorum_rejects_duplicate_nonce_or_cross_receipt_material() -> None:
    material = _material()
    deployer = _receipt(material, "deployer")
    approver = _receipt(material, "approver", nonce=deployer.nonce)
    reviewer = _receipt(material, "reviewer")

    with pytest.raises(BillingOperationRejected, match="nonce_conflict"):
        require_operator_quorum(
            (deployer, approver, reviewer),
            scope=material.scope,
            operation_id=material.operation_id,
            environment=material.environment,
            payload_digest=material.payload_digest,
            material=material,
        )

    cross_receipt = _receipt(
        material,
        "reviewer",
        budget_digest="f" * 64,
        from_state="general",
    )
    with pytest.raises(BillingOperationRejected, match="receipt_invalid"):
        require_operator_quorum(
            (deployer, _receipt(material, "approver"), cross_receipt),
            scope=material.scope,
            operation_id=material.operation_id,
            environment=material.environment,
            payload_digest=material.payload_digest,
            material=material,
        )


def _readiness_fixture(
    *,
    now: datetime,
    operation_expires_at: datetime,
    app_digest: str = "d" * 64,
    roster_revision: int = 7,
) -> tuple[BillingRolloutReadinessReceipt, dict[UUID, BillingOperationReceipt]]:
    operation_id = uuid4()
    deployer_id = uuid4()
    reviewer_id = uuid4()
    observed_at = now - timedelta(minutes=1)
    expires_at = now + timedelta(minutes=5)
    common = {
        "operation_id": operation_id,
        "environment": "test",
        "scope": "rollout",
        "payload_digest": "a" * 64,
        "deployment_digest": app_digest,
        "budget_digest": "b" * 64,
        "from_state": "canary_allowlist",
        "to_state": "general",
        "expires_at": operation_expires_at,
        "applied_at": observed_at,
        "created_at": now - timedelta(minutes=10),
        "nonce": uuid4(),
        "assignment_id": uuid4(),
        "actor_user_id": uuid4(),
        "signature_sha256": "c" * 64,
    }
    deployer = BillingOperationReceipt(
        id=deployer_id, role="deployer", **common
    )
    reviewer = BillingOperationReceipt(
        id=reviewer_id,
        role="reviewer",
        **{**common, "nonce": uuid4(), "assignment_id": uuid4(), "actor_user_id": uuid4()},
    )
    row = BillingRolloutReadinessReceipt(
        id=uuid4(),
        environment="test",
        receipt_kind="readiness",
        rollout_revision=0,
        roster_revision=roster_revision,
        status="passed",
        operation_id=operation_id,
        operation_receipt_id=deployer_id,
        provider_receipt_id=reviewer_id,
        rule_digest="r" * 64,
        bundle_digest="s" * 64,
        app_digest=app_digest,
        observed_at=observed_at,
        expires_at=expires_at,
        content_sha256=_readiness_content_digest(
            environment="test",
            receipt_kind="readiness",
            rollout_revision=0,
            roster_revision=roster_revision,
            operation_id=operation_id,
            operation_receipt_id=deployer_id,
            provider_receipt_id=reviewer_id,
            rule_digest="r" * 64,
            bundle_digest="s" * 64,
            app_digest=app_digest,
        ),
    )
    return row, {deployer_id: deployer, reviewer_id: reviewer}


def test_readiness_rejects_expired_referenced_operation_evidence() -> None:
    now = datetime.now(timezone.utc)
    row, evidence = _readiness_fixture(
        now=now, operation_expires_at=now - timedelta(seconds=1)
    )

    assert (
        _readiness_evidence_is_bound(
            row,
            evidence,
            operation_id=row.operation_id,
            payload_digest="a" * 64,
            deployment_digest=row.app_digest,
            rule_digest="r" * 64,
            bundle_digest="s" * 64,
            now=now,
        )
        is False
    )


def test_readiness_rejects_fresh_row_with_mismatched_deployment_evidence() -> None:
    now = datetime.now(timezone.utc)
    row, evidence = _readiness_fixture(now=now, operation_expires_at=now + timedelta(minutes=10))
    row.app_digest = "e" * 64

    assert (
        _readiness_evidence_is_bound(
            row,
            evidence,
            operation_id=row.operation_id,
            payload_digest="a" * 64,
            deployment_digest="d" * 64,
            rule_digest="r" * 64,
            bundle_digest="s" * 64,
            now=now,
        )
        is False
    )


def test_readiness_rejects_receipt_from_another_operation_or_roster() -> None:
    now = datetime.now(timezone.utc)
    row, evidence = _readiness_fixture(
        now=now, operation_expires_at=now + timedelta(minutes=10)
    )

    assert (
        _readiness_evidence_is_bound(
            row,
            evidence,
            operation_id=uuid4(),
            payload_digest="a" * 64,
            deployment_digest="d" * 64,
            rule_digest="r" * 64,
            bundle_digest="s" * 64,
            now=now,
        )
        is False
    )
    assert (
        _readiness_evidence_is_bound(
            row,
            evidence,
            operation_id=row.operation_id,
            payload_digest="a" * 64,
            deployment_digest="d" * 64,
            rule_digest="r" * 64,
            bundle_digest="s" * 64,
            now=now,
            current_roster_revision=row.roster_revision + 1,
        )
        is False
    )
    assert (
        _readiness_evidence_is_bound(
            row,
            evidence,
            operation_id=row.operation_id,
            payload_digest="f" * 64,
            deployment_digest="d" * 64,
            rule_digest="r" * 64,
            bundle_digest="s" * 64,
            now=now,
        )
        is False
    )
