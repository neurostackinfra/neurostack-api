"""Top-up capability evidence remains false until every pair is complete."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from src.models.billing import BillingOperationReceipt
from src.services.billing_capability import (
    BillingCapabilityRejected,
    TopupCapabilityEvidence,
    validate_capability_receipt_binding,
    validate_capability_evidence,
)
from src.services.billing_outbox import billing_fact_schema_digest


def _evidence(**overrides: object) -> TopupCapabilityEvidence:
    values: dict[str, object] = {
        "environment": "test",
        "capability_receipt_id": uuid4(),
        "producer_receipt_id": uuid4(),
        "credit_receipt_id": uuid4(),
        "reconciliation_receipt_id": uuid4(),
        "verification_receipt_id": uuid4(),
        "producer_digest": "a" * 64,
        "credit_digest": "b" * 64,
        "reconciliation_digest": "c" * 64,
        "verification_digest": "d" * 64,
        "billing_fact_schema_digest": billing_fact_schema_digest(),
        "producer_actor_id": uuid4(),
        "credit_actor_id": uuid4(),
        "reconciliation_actor_id": uuid4(),
        "verification_actor_id": uuid4(),
        "verified_at": datetime.now(timezone.utc),
        "operation_id": uuid4(),
        "deployment_digest": "e" * 64,
        "request_id": uuid4(),
        "inbox_high_watermark": 42,
    }
    values.update(overrides)
    return TopupCapabilityEvidence(**values)  # type: ignore[arg-type]


def test_complete_evidence_is_accepted() -> None:
    validate_capability_evidence(_evidence())


def test_duplicate_receipt_pair_fails_closed() -> None:
    receipt_id = uuid4()
    with pytest.raises(BillingCapabilityRejected, match="receipt_conflict"):
        validate_capability_evidence(
            _evidence(capability_receipt_id=receipt_id, producer_receipt_id=receipt_id)
        )


def test_duplicate_actor_or_bad_digest_fails_closed() -> None:
    actor_id = uuid4()
    with pytest.raises(BillingCapabilityRejected, match="actor_conflict"):
        validate_capability_evidence(
            _evidence(producer_actor_id=actor_id, credit_actor_id=actor_id)
        )
    with pytest.raises(BillingCapabilityRejected, match="digest_invalid"):
        validate_capability_evidence(_evidence(verification_digest="wrong"))


def test_missing_request_or_watermark_fails_closed() -> None:
    with pytest.raises(BillingCapabilityRejected, match="request_invalid"):
        validate_capability_evidence(_evidence(request_id=None))
    with pytest.raises(BillingCapabilityRejected, match="watermark_invalid"):
        validate_capability_evidence(_evidence(inbox_high_watermark=None))


def test_operation_identity_and_deployment_digest_are_required() -> None:
    with pytest.raises(BillingCapabilityRejected, match="operation_invalid"):
        validate_capability_evidence(_evidence(operation_id=None))
    with pytest.raises(BillingCapabilityRejected, match="digest_invalid"):
        validate_capability_evidence(_evidence(deployment_digest="stale"))


@pytest.mark.parametrize(
    "digest",
    [None, "a" * 64, "f" * 64],
    ids=["missing", "stale", "wrong"],
)
def test_fact_schema_digest_is_authoritative(digest: str | None) -> None:
    with pytest.raises(
        BillingCapabilityRejected,
        match="(?:digest_invalid|fact_schema_unbound)",
    ):
        validate_capability_evidence(_evidence(billing_fact_schema_digest=digest))


def _typed_receipt(
    evidence: TopupCapabilityEvidence, **overrides: object
) -> BillingOperationReceipt:
    now = datetime.now(timezone.utc)
    values: dict[str, object] = {
        "id": evidence.producer_receipt_id,
        "assignment_id": uuid4(),
        "operation_id": evidence.operation_id,
        "environment": evidence.environment,
        "scope": "topup_capability",
        "role": "deployer",
        "actor_user_id": evidence.producer_actor_id,
        "nonce": uuid4(),
        "payload_digest": evidence.producer_digest,
        "deployment_digest": evidence.deployment_digest,
        "budget_digest": "f" * 64,
        "from_state": "topup_disabled",
        "to_state": "topup_enabled",
        "evidence_type": "producer",
        "request_id": evidence.request_id,
        "inbox_high_watermark": evidence.inbox_high_watermark,
        "billing_fact_schema_digest": evidence.billing_fact_schema_digest,
        "expires_at": now + timedelta(minutes=5),
        "signature_sha256": "e" * 64,
        "applied_at": None,
        "created_at": now - timedelta(minutes=1),
    }
    values.update(overrides)
    return BillingOperationReceipt(**values)  # type: ignore[arg-type]


def test_successful_enablement_pair_uses_payload_digest_and_current_bindings() -> None:
    evidence = _evidence()
    receipt = _typed_receipt(evidence)

    validate_capability_receipt_binding(
        receipt,
        environment=evidence.environment,
        operation_id=evidence.operation_id,
        request_id=evidence.request_id,
        inbox_high_watermark=evidence.inbox_high_watermark,
        deployment_digest=evidence.deployment_digest,
        billing_fact_schema_digest=evidence.billing_fact_schema_digest,
        evidence_type="producer",
        digest=evidence.producer_digest,
        actor_id=evidence.producer_actor_id,
        now=datetime.now(timezone.utc),
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"payload_digest": "b" * 64},
        {"deployment_digest": "a" * 64},
        {"request_id": uuid4()},
        {"inbox_high_watermark": 43},
        {"billing_fact_schema_digest": "f" * 64},
        {"expires_at": datetime.now(timezone.utc) - timedelta(seconds=1)},
    ],
    ids=[
        "swapped-digest",
        "mismatched-deployment",
        "stale-request",
        "stale-watermark",
        "wrong-schema",
        "expired",
    ],
)
def test_enablement_rejects_swapped_stale_or_mismatched_pairs(
    overrides: dict[str, object],
) -> None:
    evidence = _evidence()
    receipt = _typed_receipt(evidence, **overrides)

    with pytest.raises(BillingCapabilityRejected, match="receipt_unverified"):
        validate_capability_receipt_binding(
            receipt,
            environment=evidence.environment,
            operation_id=evidence.operation_id,
            request_id=evidence.request_id,
            inbox_high_watermark=evidence.inbox_high_watermark,
            deployment_digest=evidence.deployment_digest,
            billing_fact_schema_digest=evidence.billing_fact_schema_digest,
            evidence_type="producer",
            digest=evidence.producer_digest,
            actor_id=evidence.producer_actor_id,
            now=datetime.now(timezone.utc),
        )
