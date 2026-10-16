from __future__ import annotations

import base64
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

from src.services.billing_producer_grants import (
    ProducerGrantRejected,
    grants_have_minimum_validity,
    preflight_grant_scope,
    producer_pair_preflight_action,
    validate_preflight_grant_pair,
)
from src.services.billing_producer_handoff import (
    CLUSTER_ARN,
    SECRET_REFERENCE_NAMES,
    SERVICE_ARN,
    ProducerReadback,
)
from src.services.billing_signed_envelope import (
    ALGORITHM,
    ENVIRONMENT,
    HEADER_FIELDS,
    PURPOSE_BY_TYPE,
    SCHEMA_VERSION,
    SignedEnvelopeRejected,
    TrustEntry,
    TrustRegistry,
    build_envelope,
    envelope_digest,
    jcs,
    parse_json_no_duplicate_keys,
    payload_digest,
    sha256_hex,
    timestamp_utc,
    validate_attestation_trust,
    validate_ci_claims,
    verify_envelope,
)


class P256TestSigner:
    """Ephemeral test-only signer. No key material is loaded from the environment."""

    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.sign_calls: list[tuple[str, bytes]] = []

    def sign_digest(self, key_arn: str, digest: bytes) -> bytes:
        self.sign_calls.append((key_arn, digest))
        return self.key.sign(digest, ec.ECDSA(utils.Prehashed(hashes.SHA256())))

    @property
    def public_key_pem(self) -> str:
        return (
            self.key.public_key()
            .public_bytes(
                serialization.Encoding.PEM,
                serialization.PublicFormat.SubjectPublicKeyInfo,
            )
            .decode("ascii")
        )


def _entry(signer: P256TestSigner, type_name: str) -> TrustEntry:
    return TrustEntry(
        issuer="test:group7b",
        kid="arn:aws:kms:us-east-1:388460142431:key/01234567-89ab-cdef-0123-456789abcdef",
        environment=ENVIRONMENT,
        audience="test:billing-producer",
        type=type_name,
        purpose=PURPOSE_BY_TYPE[type_name],
        schema_version=SCHEMA_VERSION,
        algorithm=ALGORITHM,
        public_key_pem=signer.public_key_pem,
        repository_id="12345678",
        repository_name="testorg/neurostack-api",
        workflow_path=".github/workflows/asaas-build-attestation.yml",
        workflow_ref="refs/heads/main",
        oidc_issuer="https://token.actions.githubusercontent.com",
        oidc_subject="repo:testorg/neurostack-api:ref:refs/heads/main",
    )


def _assignment_payload(operation_id: str) -> dict[str, object]:
    issued = "2026-10-15T12:00:00.000000Z"
    expires = "2026-10-15T12:05:00.000000Z"
    scope: dict[str, object] = {
        "operation_id": operation_id,
        "action": "read_billing_producer_pair",
        "environment": "staging",
        "operational_role": "production-serving",
        "cluster_arn": "arn:aws:ecs:us-east-1:388460142431:cluster/neurostack-staging",
        "service_arn": "arn:aws:ecs:us-east-1:388460142431:service/neurostack-staging/neurostack-staging-app",
        "readback_digest": "a" * 64,
        "minimum_grant_validity_seconds": 60,
        "minimum_pair_validity_seconds": 300,
    }
    return {
        "id": str(uuid4()),
        "grant_kind": "assignment",
        "issuer_user_id": str(uuid4()),
        "executor_user_id": str(uuid4()),
        "operation_id": operation_id,
        "environment": "staging",
        "operational_role": "production-serving",
        "cluster_arn": "arn:aws:ecs:us-east-1:388460142431:cluster/neurostack-staging",
        "service_arn": "arn:aws:ecs:us-east-1:388460142431:service/neurostack-staging/neurostack-staging-app",
        "action": "read_billing_producer_pair",
        "receipt_id": None,
        "verification_receipt_id": None,
        "scope": scope,
        "scope_digest": hashlib.sha256(jcs(scope)).hexdigest(),
        "issued_at": issued,
        "expires_at": expires,
    }


def test_grant_and_pair_freshness_windows_have_independent_boundaries() -> None:
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    assert grants_have_minimum_validity(
        expiries=(now + timedelta(seconds=60), now + timedelta(seconds=90)),
        now=now,
        minimum_grant_validity_seconds=60,
    )
    assert grants_have_minimum_validity(
        expiries=(now + timedelta(seconds=300),),
        now=now,
        minimum_grant_validity_seconds=300,
    )
    assert not grants_have_minimum_validity(
        expiries=(now + timedelta(seconds=59), now + timedelta(seconds=90)),
        now=now,
        minimum_grant_validity_seconds=60,
    )

    assert (
        producer_pair_preflight_action(
            deploy_expires_at=now + timedelta(seconds=301),
            verification_expires_at=now + timedelta(seconds=300),
            now=now,
            minimum_pair_validity_seconds=300,
        )
        == "refresh"
    )
    assert (
        producer_pair_preflight_action(
            deploy_expires_at=now + timedelta(seconds=301),
            verification_expires_at=now + timedelta(seconds=301),
            now=now,
            minimum_pair_validity_seconds=300,
        )
        == "keep"
    )

    with pytest.raises(ProducerGrantRejected, match="pair validity window"):
        producer_pair_preflight_action(
            deploy_expires_at=now + timedelta(seconds=600),
            verification_expires_at=now + timedelta(seconds=600),
            now=now,
            minimum_pair_validity_seconds=299,
        )


def _preflight_readback(*, config_generation: str = "active-v1") -> ProducerReadback:
    return ProducerReadback(
        cluster_arn=CLUSTER_ARN,
        service_arn=SERVICE_ARN,
        deployment_id="ecs-svc/active-v1",
        deployment_controller="ECS",
        task_definition_arn=(
            "arn:aws:ecs:us-east-1:388460142431:task-definition/"
            "neurostack-staging-app:8"
        ),
        task_arns=(
            "arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/"
            f"{uuid4().hex}",
        ),
        config_generation=config_generation,
        compatibility_generation="compat-v1",
        app_image_digest="sha256:" + "a" * 64,
        ecr_image_digest="sha256:" + "a" * 64,
        bundle_digest="b" * 64,
        readiness="ready",
        rollout_state="COMPLETED",
        rollout_control="disabled",
        topup_commercial_enabled=False,
        secret_references=tuple(
            (
                name,
                "arn:aws:secretsmanager:us-east-1:388460142431:secret:"
                f"{secret_path}-AbCdEf",
                "task-definition-unpinned",
            )
            for name, secret_path in SECRET_REFERENCE_NAMES.items()
        ),
    )


def _signed_preflight_grant_pair(
    *,
    signer: P256TestSigner,
    readback: ProducerReadback,
    minimum_grant_validity_seconds: int,
) -> dict[str, object]:
    operation_id = uuid4()
    assignment_id, authorization_id = uuid4(), uuid4()
    issuer_id, executor_id, approver_id = uuid4(), uuid4(), uuid4()
    issued_at = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    expires_at = issued_at + timedelta(minutes=5)
    scope = preflight_grant_scope(
        operation_id=operation_id,
        readback=readback,
        minimum_grant_validity_seconds=minimum_grant_validity_seconds,
        minimum_pair_validity_seconds=300,
    )
    assignment_payload = _assignment_payload(str(operation_id))
    assignment_payload.update(
        {
            "id": str(assignment_id),
            "issuer_user_id": str(issuer_id),
            "executor_user_id": str(executor_id),
            "scope": scope,
            "scope_digest": sha256_hex(jcs(scope)),
            "issued_at": timestamp_utc(issued_at),
            "expires_at": timestamp_utc(expires_at),
        }
    )
    assignment_envelope = build_envelope(
        entry=_entry(signer, "billing_producer_assignment.v1"),
        payload=assignment_payload,
        operation_id=operation_id,
        issued_at=issued_at,
        expires_at=expires_at,
        signer=signer,
    )
    assignment_envelope_digest = envelope_digest(assignment_envelope)
    authorization_payload = {
        **assignment_payload,
        "id": str(authorization_id),
        "grant_kind": "authorization",
        "assignment_id": str(assignment_id),
        "assignment_digest": assignment_envelope_digest,
        "approver_user_id": str(approver_id),
    }
    authorization_envelope = build_envelope(
        entry=_entry(signer, "billing_producer_authorization.v1"),
        payload=authorization_payload,
        operation_id=operation_id,
        issued_at=issued_at,
        expires_at=expires_at,
        signer=signer,
    )
    return {
        "operation_id": operation_id,
        "assignment_id": assignment_id,
        "authorization_id": authorization_id,
        "actor_user_id": executor_id,
        "assignment_envelope": assignment_envelope,
        "authorization_envelope": authorization_envelope,
        "assignment_digest": assignment_envelope_digest,
        "authorization_digest": envelope_digest(authorization_envelope),
        "assignment_trust": _entry(signer, "billing_producer_assignment.v1"),
        "authorization_trust": _entry(signer, "billing_producer_authorization.v1"),
        "now": issued_at + timedelta(minutes=1),
    }


def test_preflight_validator_rejects_signed_mismatched_validity_window() -> None:
    signer = P256TestSigner()
    readback = _preflight_readback()
    grants = _signed_preflight_grant_pair(
        signer=signer,
        readback=readback,
        minimum_grant_validity_seconds=120,
    )

    with pytest.raises(
        ProducerGrantRejected,
        match="readback or validity window differs from grant scope",
    ):
        validate_preflight_grant_pair(
            **grants,
            readback=readback,
            minimum_grant_validity_seconds=60,
            minimum_pair_validity_seconds=300,
        )


def test_preflight_validator_rejects_signed_different_readback_snapshot() -> None:
    signer = P256TestSigner()
    signed_readback = _preflight_readback(config_generation="active-v1")
    grants = _signed_preflight_grant_pair(
        signer=signer,
        readback=signed_readback,
        minimum_grant_validity_seconds=60,
    )
    current_readback = _preflight_readback(config_generation="active-v2")

    with pytest.raises(
        ProducerGrantRejected,
        match="readback or validity window differs from grant scope",
    ):
        validate_preflight_grant_pair(
            **grants,
            readback=current_readback,
            minimum_grant_validity_seconds=60,
            minimum_pair_validity_seconds=300,
        )


def _attestation_payload() -> dict[str, object]:
    repository_id = "12345678"
    workflow_path = ".github/workflows/asaas-build-attestation.yml"
    app_digest = "sha256:" + "1" * 64
    return {
        "attestation_type": "billing_build_attestation.v1",
        "attestation_id": str(uuid4()),
        "repository_id": repository_id,
        "repository_name": "testorg/neurostack-api",
        "workflow_path": workflow_path,
        "workflow_ref": "refs/heads/main",
        "workflow_run_id": 7123456789,
        "app_image_digest": app_digest,
        "bundle_digest": "2" * 64,
        "commit_sha": "3" * 64,
        "tree_sha": "4" * 64,
        "workflow_id": f"github-actions:{repository_id}:{workflow_path}",
        "immutable_artifact_reference": (
            "388460142431.dkr.ecr.us-east-1.amazonaws.com/neurostack/api@" + app_digest
        ),
    }


def test_jcs_serializes_nested_objects_by_utf16_key_order_and_rejects_floats() -> None:
    assert (
        jcs({"z": {"b": 2, "a": 1}, "a": "é"}) == b'{"a":"\xc3\xa9","z":{"a":1,"b":2}}'
    )
    with pytest.raises(SignedEnvelopeRejected, match="floating point"):
        jcs({"amount": 0.1})
    with pytest.raises(SignedEnvelopeRejected, match="safe range"):
        jcs({"large": 2**53})


def test_flattened_envelope_signs_only_exact_thirteen_key_header() -> None:
    signer = P256TestSigner()
    entry = _entry(signer, "billing_producer_assignment.v1")
    operation_id = uuid4()
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    payload = _assignment_payload(str(operation_id))
    envelope = build_envelope(
        entry=entry,
        payload=payload,
        operation_id=operation_id,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
        signer=signer,
    )

    assert set(envelope) == {
        "issuer",
        "kid",
        "algorithm",
        "audience",
        "type",
        "purpose",
        "schema_version",
        "environment",
        "operation_id",
        "request_id",
        "issued_at",
        "expires_at",
        "payload_digest",
        "payload",
        "signature",
    }
    assert envelope["request_id"] is None
    assert payload_digest(payload) == envelope["payload_digest"]
    assert envelope_digest(envelope) == hashlib.sha256(jcs(envelope)).hexdigest()
    assert signer.sign_calls == [
        (
            entry.kid,
            hashlib.sha256(
                jcs(
                    {
                        key: envelope[key]
                        for key in (
                            "issuer",
                            "kid",
                            "algorithm",
                            "audience",
                            "type",
                            "purpose",
                            "schema_version",
                            "environment",
                            "operation_id",
                            "request_id",
                            "issued_at",
                            "expires_at",
                            "payload_digest",
                        )
                    }
                )
            ).digest(),
        )
    ]

    registry = TrustRegistry([entry])
    assert verify_envelope(envelope, registry) == entry


def test_verification_rejects_noncanonical_or_untrusted_envelopes() -> None:
    signer = P256TestSigner()
    entry = _entry(signer, "billing_producer_assignment.v1")
    operation_id = uuid4()
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    envelope = build_envelope(
        entry=entry,
        payload=_assignment_payload(str(operation_id)),
        operation_id=operation_id,
        issued_at=now,
        expires_at=now + timedelta(minutes=5),
        signer=signer,
    )
    registry = TrustRegistry([entry])

    extra = dict(envelope, extra="no")
    with pytest.raises(SignedEnvelopeRejected, match="exactly 15"):
        verify_envelope(extra, registry)
    nested = dict(envelope, unsigned_header={})
    with pytest.raises(SignedEnvelopeRejected, match="exactly 15"):
        verify_envelope(nested, registry)
    bad_payload = dict(envelope, payload={**envelope["payload"], "extra": True})
    with pytest.raises(SignedEnvelopeRejected, match="payload digest"):
        verify_envelope(bad_payload, registry)
    bad_purpose = dict(envelope, purpose="billing-producer-receipt.v1")
    with pytest.raises(SignedEnvelopeRejected, match="purpose"):
        verify_envelope(bad_purpose, registry)
    bad_audience = dict(envelope, audience="other")
    with pytest.raises(SignedEnvelopeRejected, match="trust tuple"):
        verify_envelope(bad_audience, registry)


def test_build_attestation_binds_admin_pinned_repository_workflow_and_oidc() -> None:
    signer = P256TestSigner()
    entry = _entry(signer, "billing_build_attestation.v1")
    operation_id = uuid4()
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    payload = _attestation_payload()
    envelope = build_envelope(
        entry=entry,
        payload=payload,
        operation_id=operation_id,
        issued_at=now,
        expires_at=now + timedelta(days=7),
        signer=signer,
    )
    verified = verify_envelope(
        envelope,
        TrustRegistry([entry]),
        expected_type="billing_build_attestation.v1",
    )
    validate_attestation_trust(verified, envelope)
    validate_ci_claims(
        entry,
        {
            "iss": entry.oidc_issuer,
            "aud": entry.audience,
            "sub": entry.oidc_subject,
            "repository_id": entry.repository_id,
            "repository": entry.repository_name,
            "workflow_ref": entry.workflow_ref,
        },
    )
    with pytest.raises(SignedEnvelopeRejected, match="claims"):
        validate_ci_claims(entry, {"iss": entry.oidc_issuer})

    tampered = dict(envelope, payload={**payload, "repository_id": "999999"})
    with pytest.raises(SignedEnvelopeRejected, match="payload digest"):
        verify_envelope(tampered, TrustRegistry([entry]))


def test_duplicate_json_keys_and_noncanonical_signature_encoding_are_rejected() -> None:
    with pytest.raises(SignedEnvelopeRejected, match="duplicate"):
        parse_json_no_duplicate_keys(b'{"schema_version":"x","schema_version":"y"}')
    with pytest.raises(SignedEnvelopeRejected, match="floating point"):
        parse_json_no_duplicate_keys(b'{"x":1.25}')

    signer = P256TestSigner()
    entry = _entry(signer, "billing_build_attestation.v1")
    now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
    envelope = build_envelope(
        entry=entry,
        payload=_attestation_payload(),
        operation_id=uuid4(),
        issued_at=now,
        expires_at=now + timedelta(days=7),
        signer=signer,
    )
    malformed = dict(envelope)
    malformed["signature"] = base64.b64encode(b"der").decode("ascii") + "="
    with pytest.raises(SignedEnvelopeRejected, match="base64url"):
        verify_envelope(malformed, TrustRegistry([entry]))


def test_shared_conformance_fixture_matches_exact_envelope_and_signed_header() -> None:
    fixture_path = (
        Path(__file__).parents[1]
        / "fixtures"
        / "billing"
        / "producer-envelope-conformance.v1.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert fixture["fixture_schema_version"] == (
        "billing_producer_envelope_conformance.v1"
    )
    envelope = fixture["envelope"]
    trust = TrustEntry(**fixture["trust_entry"])
    exact_envelope_bytes = jcs(envelope)
    unsigned_header_bytes = jcs({field: envelope[field] for field in HEADER_FIELDS})

    assert set(envelope) == {
        *HEADER_FIELDS,
        "payload",
        "signature",
    }
    assert base64.b64decode(fixture["envelope_jcs_base64"], validate=True) == (
        exact_envelope_bytes
    )
    assert (
        base64.b64decode(fixture["unsigned_header_jcs_base64"], validate=True)
        == unsigned_header_bytes
    )
    assert envelope["signature"] == fixture["signature"]
    assert payload_digest(envelope["payload"]) == fixture["payload_digest"]
    assert envelope_digest(envelope) == fixture["envelope_digest"]
    assert (
        verify_envelope(
            envelope,
            TrustRegistry([trust]),
            expected_type="billing_producer_assignment.v1",
        )
        == trust
    )

    with pytest.raises(SignedEnvelopeRejected, match="exactly 15"):
        verify_envelope({**envelope, "unsigned_header": {}}, TrustRegistry([trust]))
