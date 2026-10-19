"""Boundary tests for signed, separated, single-use operator evidence."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from src.models.billing import BillingOperationReceipt, BillingRolloutReadinessReceipt
from src.services.billing_operations import (
    OPERATION_RECEIPT_SIGNATURE_CONTEXT,
    BillingOperationAuthority,
    BillingOperationRejected,
    OperationMaterial,
    OperatorReceipt,
    canary_budget_digest,
    operation_receipt_message,
    operation_receipt_signature,
    require_operator_quorum,
    require_quorum_after,
    require_receipt_lifetime,
    signs_rollout_revision,
    verify_operation_receipt_signature,
)
from src.services.billing_rollout import (
    _readiness_content_digest,
    _readiness_evidence_is_bound,
)

_KEY = b"k" * 32


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


def _stored_receipt(**overrides: object) -> BillingOperationReceipt:
    """A fully populated receipt row, as the backend persists one."""

    values: dict[str, object] = {
        "id": uuid4(),
        "assignment_id": UUID("00000000-0000-4000-8000-000000000001"),
        "operation_id": UUID("00000000-0000-4000-8000-000000000002"),
        "environment": "test",
        "scope": "rollout",
        "role": "deployer",
        "actor_user_id": UUID("00000000-0000-4000-8000-000000000003"),
        "nonce": UUID("00000000-0000-4000-8000-000000000004"),
        "payload_digest": "f" * 64,
        "deployment_digest": "b" * 64,
        "budget_digest": "a" * 64,
        "from_state": "disabled",
        "to_state": "canary_allowlist",
        "evidence_type": None,
        "request_id": None,
        "inbox_high_watermark": None,
        "billing_fact_schema_digest": None,
        "expires_at": datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc),
        "applied_at": None,
    }
    values.update(overrides)
    row = BillingOperationReceipt(**values)  # type: ignore[arg-type]
    row.signature_sha256 = operation_receipt_signature(
        row, rollout_revision=7, signing_key=_KEY
    )
    return row


def test_receipt_signing_message_encoding_is_pinned() -> None:
    """The exact bytes the backend MACs: a context string plus every stored
    assertion of the receipt, as sorted, compact, ASCII JSON."""

    row = _stored_receipt()
    expected = (
        '{"actor_user_id":"00000000-0000-4000-8000-000000000003",'
        '"assignment_id":"00000000-0000-4000-8000-000000000001",'
        '"billing_fact_schema_digest":null,'
        f'"budget_digest":"{"a" * 64}",'
        '"context":"neurostack.billing.operation_receipt.v2",'
        f'"deployment_digest":"{"b" * 64}",'
        '"environment":"test",'
        '"evidence_type":null,'
        '"expires_at":"2026-10-17T12:00:00+00:00",'
        '"from_state":"disabled",'
        '"inbox_high_watermark":null,'
        '"nonce":"00000000-0000-4000-8000-000000000004",'
        '"operation_id":"00000000-0000-4000-8000-000000000002",'
        '"request_id":null,'
        '"role":"deployer",'
        '"rollout_revision":7,'
        '"scope":"rollout",'
        '"to_state":"canary_allowlist"}'
    ).encode("ascii")

    assert OPERATION_RECEIPT_SIGNATURE_CONTEXT == (
        "neurostack.billing.operation_receipt.v2"
    )
    assert operation_receipt_message(row, rollout_revision=7) == expected
    assert row.signature_sha256 == (
        hmac.new(_KEY, expected, hashlib.sha256).hexdigest()
    )
    verify_operation_receipt_signature(row, rollout_revision=7, signing_key=_KEY)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("role", "approver"),
        ("actor_user_id", UUID("00000000-0000-4000-8000-0000000000aa")),
        ("assignment_id", UUID("00000000-0000-4000-8000-0000000000bb")),
        ("nonce", UUID("00000000-0000-4000-8000-0000000000cc")),
        ("scope", "incident"),
        ("operation_id", UUID("00000000-0000-4000-8000-0000000000dd")),
        ("environment", "staging"),
        ("from_state", "general"),
        ("to_state", "general"),
        ("budget_digest", "c" * 64),
        ("deployment_digest", "d" * 64),
        ("expires_at", datetime(2026, 10, 17, 12, 0, 0, 1, tzinfo=timezone.utc)),
        ("evidence_type", "verification"),
        ("request_id", UUID("00000000-0000-4000-8000-0000000000ee")),
        ("inbox_high_watermark", 0),
        ("billing_fact_schema_digest", "e" * 64),
    ],
)
def test_receipt_signature_binds_every_stored_assertion(
    column: str, value: object
) -> None:
    row = _stored_receipt()
    setattr(row, column, value)

    with pytest.raises(
        BillingOperationRejected, match="^billing_operation_signature_invalid$"
    ):
        verify_operation_receipt_signature(row, rollout_revision=7, signing_key=_KEY)


@pytest.mark.parametrize("revision", [6, 8, 0, None])
def test_receipt_signature_binds_the_signed_rollout_revision(
    revision: int | None,
) -> None:
    with pytest.raises(
        BillingOperationRejected, match="^billing_operation_signature_invalid$"
    ):
        verify_operation_receipt_signature(
            _stored_receipt(), rollout_revision=revision, signing_key=_KEY
        )


def test_receipt_signature_is_keyed_and_not_the_old_payload_hmac() -> None:
    row = _stored_receipt()
    with pytest.raises(
        BillingOperationRejected, match="^billing_operation_signature_invalid$"
    ):
        verify_operation_receipt_signature(
            row, rollout_revision=7, signing_key=b"j" * 32
        )
    with pytest.raises(
        BillingOperationRejected, match="^billing_operation_signing_unavailable$"
    ):
        verify_operation_receipt_signature(row, rollout_revision=7, signing_key=b"")
    with pytest.raises(
        BillingOperationRejected, match="^billing_operation_signing_unavailable$"
    ):
        operation_receipt_signature(row, rollout_revision=7, signing_key=b"")
    # The pre-D2b client HMAC over the shared payload digest is not accepted.
    row.signature_sha256 = hmac.new(
        _KEY, row.payload_digest.encode("ascii"), hashlib.sha256
    ).hexdigest()
    with pytest.raises(
        BillingOperationRejected, match="^billing_operation_signature_invalid$"
    ):
        verify_operation_receipt_signature(row, rollout_revision=7, signing_key=_KEY)


@pytest.mark.parametrize(
    ("scope", "to_state", "bound"),
    [
        ("rollout", "canary_allowlist", True),
        ("rollout", "general", True),
        ("rollout", "disabled", False),
        ("incident", "disabled", False),
        ("topup_capability", "topup_enabled", False),
    ],
)
def test_only_opening_rollout_moves_sign_the_rollout_revision(
    scope: str, to_state: str, bound: bool
) -> None:
    assert signs_rollout_revision(scope=scope, to_state=to_state) is bound


def test_canary_budget_digest_encoding_is_pinned() -> None:
    """The bytes an operator tool hashes for a canary ``budget_digest``."""

    builder = UUID("00000000-0000-4000-8000-0000000000ff")
    expected = hashlib.sha256(
        b'{"canary_amount_limit_cents":109900,'
        b'"canary_builder_id":"00000000-0000-4000-8000-0000000000ff",'
        b'"canary_checkout_limit":2,"canary_settlement_limit":2,'
        b'"environment":"staging"}'
    ).hexdigest()

    assert canary_budget_digest(environment="staging", canary_builder_id=builder) == (
        expected
    )
    assert canary_budget_digest(
        environment="production", canary_builder_id=builder
    ) != (expected)
    assert canary_budget_digest(environment="staging", canary_builder_id=uuid4()) != (
        expected
    )


def test_receipt_lifetime_cap_boundary_is_inclusive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.config.settings import settings

    monkeypatch.setattr(settings, "billing_operation_receipt_max_ttl_seconds", 86400)
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)

    require_receipt_lifetime(now + timedelta(seconds=86400), now=now)
    with pytest.raises(
        BillingOperationRejected, match="^billing_operation_expiry_too_long$"
    ):
        require_receipt_lifetime(
            now + timedelta(seconds=86400, microseconds=1), now=now
        )
    with pytest.raises(
        BillingOperationRejected, match="^billing_operation_expiry_invalid$"
    ):
        require_receipt_lifetime(datetime(2026, 10, 18, 11, 0), now=now)
    with pytest.raises(BillingOperationRejected, match="^billing_operation_expired$"):
        require_receipt_lifetime(now, now=now)


def test_ten_year_receipt_and_assignment_are_refused_before_any_database_use() -> None:
    """No session is needed to refuse it (``None`` would fail on any use)."""

    now = datetime.now(timezone.utc)
    material = OperationMaterial(
        scope="rollout",
        operation_id=uuid4(),
        environment="test",
        from_state="disabled",
        to_state="canary_allowlist",
        budget_digest="a" * 64,
        deployment_digest="b" * 64,
        expires_at=now + timedelta(days=3650),
    )
    authority = BillingOperationAuthority(
        None, signing_key=_KEY, clock=lambda: now  # type: ignore[arg-type]
    )

    async def probe() -> None:
        with pytest.raises(
            BillingOperationRejected, match="^billing_operation_expiry_too_long$"
        ):
            await authority.assign(
                material=material, role="deployer", actor_user_id=uuid4()
            )
        with pytest.raises(
            BillingOperationRejected, match="^billing_operation_expiry_too_long$"
        ):
            await authority.record_receipt(
                assignment_id=uuid4(),
                actor_user_id=uuid4(),
                nonce=uuid4(),
                material=material,
            )

    asyncio.run(probe())


@pytest.mark.parametrize(
    ("offsets", "stale"),
    [
        ((1, 1, 1), False),
        ((1, 1, 0), True),
        ((-1, 1, 1), True),
        ((1, -1, -1), True),
        ((0, 0, 0), True),
        ((), False),
    ],
)
def test_quorum_is_fresh_only_if_every_row_is_strictly_newer_than_the_anchor(
    offsets: tuple[int, ...], stale: bool
) -> None:
    anchor = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    rows = [
        BillingOperationReceipt(created_at=anchor + timedelta(microseconds=offset))
        for offset in offsets
    ]
    if not stale:
        require_quorum_after(rows, anchor)
        return
    with pytest.raises(
        BillingOperationRejected, match="^billing_rollout_quorum_stale$"
    ):
        require_quorum_after(rows, anchor)


def test_receipt_request_schema_refuses_a_client_signature() -> None:
    from pydantic import ValidationError

    from src.api.schemas.admin import BillingOperationReceiptRequest

    body = {
        "scope": "rollout",
        "operation_id": str(uuid4()),
        "environment": "test",
        "from_state": "disabled",
        "to_state": "canary_allowlist",
        "budget_digest": "a" * 64,
        "deployment_digest": "b" * 64,
        "expires_at": "2026-10-17T12:00:00+00:00",
        "assignment_id": str(uuid4()),
        "nonce": str(uuid4()),
    }
    assert "signature" not in BillingOperationReceiptRequest.model_fields
    BillingOperationReceiptRequest.model_validate(body)
    with pytest.raises(ValidationError, match="signature\n  Extra inputs"):
        BillingOperationReceiptRequest.model_validate({**body, "signature": "a" * 64})


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
        # Readiness names the quorum the general transition has yet to spend.
        "applied_at": None,
        "created_at": now - timedelta(minutes=10),
        "nonce": uuid4(),
        "assignment_id": uuid4(),
        "actor_user_id": uuid4(),
        "signature_sha256": "c" * 64,
    }
    deployer = BillingOperationReceipt(id=deployer_id, role="deployer", **common)
    reviewer = BillingOperationReceipt(
        id=reviewer_id,
        role="reviewer",
        **{
            **common,
            "nonce": uuid4(),
            "assignment_id": uuid4(),
            "actor_user_id": uuid4(),
        },
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


def _is_bound(
    row: BillingRolloutReadinessReceipt,
    evidence: dict[UUID, BillingOperationReceipt],
    *,
    now: datetime,
) -> bool:
    return _readiness_evidence_is_bound(
        row,
        evidence,
        operation_id=row.operation_id,
        payload_digest="a" * 64,
        deployment_digest=row.app_digest,
        rule_digest="r" * 64,
        bundle_digest="s" * 64,
        now=now,
        current_roster_revision=row.roster_revision,
    )


def test_readiness_fixture_is_bound_so_each_rejection_is_meaningful() -> None:
    now = datetime.now(timezone.utc)
    row, evidence = _readiness_fixture(
        now=now, operation_expires_at=now + timedelta(minutes=10)
    )

    assert _is_bound(row, evidence, now=now) is True


@pytest.mark.parametrize("spent", ["operation_receipt_id", "provider_receipt_id"])
def test_readiness_rejects_spent_operation_evidence(spent: str) -> None:
    now = datetime.now(timezone.utc)
    row, evidence = _readiness_fixture(
        now=now, operation_expires_at=now + timedelta(minutes=10)
    )
    evidence[getattr(row, spent)].applied_at = now - timedelta(seconds=1)

    assert _is_bound(row, evidence, now=now) is False


def test_readiness_rejects_one_actor_as_deployer_and_reviewer() -> None:
    now = datetime.now(timezone.utc)
    row, evidence = _readiness_fixture(
        now=now, operation_expires_at=now + timedelta(minutes=10)
    )
    deployer = evidence[row.operation_receipt_id]
    evidence[row.provider_receipt_id].actor_user_id = deployer.actor_user_id

    assert _is_bound(row, evidence, now=now) is False


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
    row, evidence = _readiness_fixture(
        now=now, operation_expires_at=now + timedelta(minutes=10)
    )
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
