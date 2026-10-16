from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from fastapi import HTTPException
from starlette.requests import Request

from src.api.middleware.auth import TenantContext
from src.api.routes import billing_producer as routes
from src.config.settings import settings
from src.models.billing_producer_signed import (
    BillingProducerV1BuildAttestation,
    BillingProducerV1Grant,
    BillingProducerV1GrantConsumption,
    BillingProducerV1GrantRevocation,
    BillingProducerV1Receipt,
)
from src.models.user import UserRole
from src.services.billing_producer_handoff import (
    CLUSTER_ARN,
    SECRET_REFERENCE_NAMES,
    SERVICE_ARN,
    ProducerReadback,
)
from src.services.billing_signed_envelope import (
    ALGORITHM,
    ENVIRONMENT,
    PURPOSE_BY_TYPE,
    SCHEMA_VERSION,
    TrustEntry,
    TrustRegistry,
    build_envelope,
    envelope_digest,
    jcs,
    payload_digest,
    sha256_hex,
    verify_envelope,
)


class P256TestSigner:
    def __init__(self) -> None:
        self.key = ec.generate_private_key(ec.SECP256R1())

    def sign_digest(self, key_arn: str, digest: bytes) -> bytes:
        del key_arn
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


class _Begin(AbstractAsyncContextManager[None]):
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False


class _Result:
    def __init__(self, values: list[Any]):
        self._values = values

    def scalars(self) -> _Result:
        return self

    def all(self) -> list[Any]:
        return self._values


class FakeSession:
    def __init__(self, users: dict[UUID, Any]):
        self.users = users
        self.grants: dict[UUID, BillingProducerV1Grant] = {}
        self.attestations: dict[UUID, BillingProducerV1BuildAttestation] = {}
        self.receipts: dict[UUID, BillingProducerV1Receipt] = {}
        self.consumptions: list[BillingProducerV1GrantConsumption] = []
        self.revocations: list[BillingProducerV1GrantRevocation] = []
        self.active_pair = None
        self.added: list[Any] = []

    def begin(self) -> _Begin:
        return _Begin()

    @staticmethod
    def _uuid_params(statement: Any) -> set[UUID]:
        return set(FakeSession._ordered_uuid_params(statement))

    @staticmethod
    def _ordered_uuid_params(statement: Any) -> list[UUID]:
        def visit(value: Any) -> list[UUID]:
            if isinstance(value, UUID):
                return [value]
            if isinstance(value, dict):
                return [item for nested in value.values() for item in visit(nested)]
            if isinstance(value, (list, tuple, set, frozenset)):
                return [item for nested in value for item in visit(nested)]
            return []

        return [
            identifier
            for value in statement.compile().params.values()
            for identifier in visit(value)
        ]

    async def scalar(self, statement: Any) -> Any:
        entity = statement.column_descriptions[0]["entity"]
        ids = self._ordered_uuid_params(statement)
        identifier = ids[0] if ids else None
        if entity is routes.BillingProducerV1ActivePair:
            return self.active_pair
        if entity is routes.BillingProducerV1BuildAttestation:
            return self.attestations.get(identifier) if identifier else None
        if entity is routes.BillingProducerV1Receipt:
            return self.receipts.get(identifier) if identifier else None
        if entity is routes.User:
            return self.users.get(identifier) if identifier else None
        if entity is routes.BillingProducerV1Grant:
            return self.grants.get(identifier) if identifier else None
        return None

    async def execute(self, statement: Any) -> _Result:
        entity = statement.column_descriptions[0]["entity"]
        ids = self._uuid_params(statement)
        if entity is routes.BillingProducerV1Grant:
            return _Result([self.grants[item] for item in ids if item in self.grants])
        if entity is routes.User:
            return _Result([self.users[item] for item in ids if item in self.users])
        if entity is routes.BillingProducerV1GrantConsumption.grant_id:
            return _Result(
                [item.grant_id for item in self.consumptions if item.grant_id in ids]
            )
        if entity is routes.BillingProducerV1GrantRevocation.grant_id:
            return _Result(
                [item.grant_id for item in self.revocations if item.grant_id in ids]
            )
        return _Result([])

    def add(self, value: Any) -> None:
        self.added.append(value)
        if isinstance(value, BillingProducerV1Receipt):
            self.receipts[value.id] = value
        elif isinstance(value, BillingProducerV1GrantConsumption):
            self.consumptions.append(value)
        elif value.__class__.__name__ == "BillingProducerV1ActivePair":
            self.active_pair = value

    async def flush(self, values: Any = None) -> None:
        del values


def _request() -> Request:
    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/admin/billing/producer-receipts/create",
            "headers": [],
            "state": {"staging_rehearsal_token": True},
        },
        receive,
    )


def _tenant(user_id: UUID) -> TenantContext:
    return TenantContext(
        tenant_id=uuid4(),
        user_id=user_id,
        role="super_admin",
        authentication_mode="jwt",
    )


def _readback() -> ProducerReadback:
    task = uuid4().hex
    return ProducerReadback(
        cluster_arn=CLUSTER_ARN,
        service_arn=SERVICE_ARN,
        deployment_id="ecs-svc/active-v1-test",
        deployment_controller="ECS",
        task_definition_arn=(
            "arn:aws:ecs:us-east-1:388460142431:task-definition/"
            "neurostack-staging-app:8"
        ),
        task_arns=(
            "arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/" f"{task}",
        ),
        config_generation="cfg-v1-test",
        compatibility_generation="compat-v1-test",
        app_image_digest="sha256:" + "a" * 64,
        ecr_image_digest="sha256:" + "a" * 64,
        bundle_digest="b" * 64,
        readiness="ready",
        rollout_state="COMPLETED",
        rollout_control="disabled",
        topup_commercial_enabled=False,
        secret_references=tuple(
            sorted(
                (
                    name,
                    f"arn:aws:secretsmanager:us-east-1:388460142431:secret:{path}-AbCdEf",
                    "task-definition-version",
                )
                for name, path in SECRET_REFERENCE_NAMES.items()
            )
        ),
    )


def _entry(
    signer: P256TestSigner,
    type_name: str,
    *,
    key_id: str,
    repository_pins: bool = False,
    issuer: str | None = None,
    audience: str | None = None,
) -> TrustEntry:
    extra = (
        {
            "repository_id": "12345678",
            "repository_name": "testorg/neurostack-api",
            "workflow_path": ".github/workflows/asaas-build-attestation.yml",
            "workflow_ref": "refs/heads/main",
            "oidc_issuer": "https://token.actions.githubusercontent.com",
            "oidc_subject": "repo:testorg/neurostack-api:environment:billing-producer-build-attestation",
        }
        if repository_pins
        else {}
    )
    return TrustEntry(
        issuer=issuer or f"test:{type_name}",
        kid=f"arn:aws:kms:us-east-1:388460142431:key/{key_id}",
        environment=ENVIRONMENT,
        audience=audience or f"test:{type_name}",
        type=type_name,
        purpose=PURPOSE_BY_TYPE[type_name],
        schema_version=SCHEMA_VERSION,
        algorithm=ALGORITHM,
        public_key_pem=signer.public_key_pem,
        **extra,
    )


def _grant(
    *,
    signer: P256TestSigner,
    entry: TrustEntry,
    grant_id: UUID,
    kind: str,
    operation_id: UUID,
    actor_ids: tuple[UUID, UUID, UUID],
    action: str,
    receipt_id: UUID,
    verification_receipt_id: UUID,
    scope: dict[str, Any],
    assignment: BillingProducerV1Grant | None = None,
    issued_at: datetime,
) -> BillingProducerV1Grant:
    issuer_id, executor_id, approver_id = actor_ids
    kind_name = "assignment" if kind == "assignment" else "authorization"
    payload = {
        "id": str(grant_id),
        "grant_kind": kind_name,
        "issuer_user_id": str(issuer_id),
        "executor_user_id": str(executor_id),
        "operation_id": str(operation_id),
        "environment": ENVIRONMENT,
        "operational_role": "production-serving",
        "cluster_arn": CLUSTER_ARN,
        "service_arn": SERVICE_ARN,
        "action": action,
        "receipt_id": str(receipt_id),
        "verification_receipt_id": str(verification_receipt_id),
        "scope": scope,
        "scope_digest": sha256_hex(jcs(scope)),
        "issued_at": issued_at.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        "expires_at": (issued_at + timedelta(minutes=5))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z"),
    }
    if assignment is not None:
        payload.update(
            assignment_id=str(assignment.id),
            assignment_digest=assignment.envelope_digest,
            approver_user_id=str(approver_id),
        )
    envelope = build_envelope(
        entry=entry,
        payload=payload,
        operation_id=operation_id,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=5),
        signer=signer,
    )
    return BillingProducerV1Grant(
        id=grant_id,
        grant_kind=kind_name,
        envelope=envelope,
        envelope_digest=envelope_digest(envelope),
        payload_digest=payload_digest(payload),
        issuer_user_id=issuer_id,
        executor_user_id=executor_id,
        approver_user_id=approver_id if assignment is not None else None,
        assignment_id=assignment.id if assignment is not None else None,
        assignment_digest=(
            assignment.envelope_digest if assignment is not None else None
        ),
        operation_id=operation_id,
        environment=ENVIRONMENT,
        operational_role="production-serving",
        cluster_arn=CLUSTER_ARN,
        service_arn=SERVICE_ARN,
        action=action,
        receipt_id=receipt_id,
        verification_receipt_id=verification_receipt_id,
        scope=scope,
        scope_digest=sha256_hex(jcs(scope)),
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=5),
    )


@pytest.mark.asyncio
async def test_create_and_verify_issue_canonical_receipts_and_consume_v1_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    grant_signer = P256TestSigner()
    build_signer = P256TestSigner()
    receipt_signer = P256TestSigner()
    grant_key_id = "11111111-1111-4111-8111-111111111111"
    build_key_id = "22222222-2222-4222-8222-222222222222"
    receipt_key_id = "33333333-3333-4333-8333-333333333333"
    grant_entry = _entry(
        grant_signer, "billing_producer_assignment.v1", key_id=grant_key_id
    )
    authorization_entry = _entry(
        grant_signer,
        "billing_producer_authorization.v1",
        key_id=grant_key_id,
        issuer=grant_entry.issuer,
        audience=grant_entry.audience,
    )
    build_entry = _entry(
        build_signer,
        "billing_build_attestation.v1",
        key_id=build_key_id,
        repository_pins=True,
    )
    deploy_entry = _entry(
        receipt_signer, "billing_producer_deployed.v1", key_id=receipt_key_id
    )
    verify_entry = _entry(
        receipt_signer,
        "billing_producer_deploy_verified.v1",
        key_id=receipt_key_id,
        issuer=deploy_entry.issuer,
        audience=deploy_entry.audience,
    )
    monkeypatch.setattr(settings, "environment", "staging")
    for suffix, entry in (
        ("grant", grant_entry),
        ("build", build_entry),
        ("receipt", deploy_entry),
    ):
        monkeypatch.setattr(settings, f"billing_producer_{suffix}_issuer", entry.issuer)
        monkeypatch.setattr(settings, f"billing_producer_{suffix}_kid", entry.kid)
        monkeypatch.setattr(
            settings, f"billing_producer_{suffix}_audience", entry.audience
        )
        monkeypatch.setattr(
            settings, f"billing_producer_{suffix}_public_key", entry.public_key_pem
        )
    for name, value in (
        ("billing_producer_build_repository_id", build_entry.repository_id),
        ("billing_producer_build_repository_name", build_entry.repository_name),
        ("billing_producer_build_workflow_path", build_entry.workflow_path),
        ("billing_producer_build_workflow_ref", build_entry.workflow_ref),
        ("billing_producer_build_oidc_issuer", build_entry.oidc_issuer),
        ("billing_producer_build_oidc_subject", build_entry.oidc_subject),
    ):
        monkeypatch.setattr(settings, name, value)
    signer_factory_calls = 0

    def test_receipt_signer() -> P256TestSigner:
        nonlocal signer_factory_calls
        signer_factory_calls += 1
        return receipt_signer

    monkeypatch.setattr(routes, "_receipt_signer", test_receipt_signer)

    operation_id, deploy_id, verify_id = uuid4(), uuid4(), uuid4()
    issuer_id, executor_id, approver_id = uuid4(), uuid4(), uuid4()
    verifier_issuer_id, verifier_id, verifier_approver_id = uuid4(), uuid4(), uuid4()
    ids = {
        issuer_id,
        executor_id,
        approver_id,
        verifier_issuer_id,
        verifier_id,
        verifier_approver_id,
    }
    users = {
        user_id: SimpleNamespace(is_active=True, role=UserRole.super_admin)
        for user_id in ids
    }
    db = FakeSession(users)
    readback = _readback()
    readback_payload = readback.as_payload()
    readback_digest = sha256_hex(jcs(readback_payload))
    commit_sha, tree_sha = "c" * 64, "d" * 64
    workflow_id = (
        f"github-actions:{build_entry.repository_id}:{build_entry.workflow_path}"
    )
    attestation_id = uuid4()
    attestation_payload = {
        "attestation_type": "billing_build_attestation.v1",
        "attestation_id": str(attestation_id),
        "repository_id": build_entry.repository_id,
        "repository_name": build_entry.repository_name,
        "workflow_path": build_entry.workflow_path,
        "workflow_ref": build_entry.workflow_ref,
        "workflow_run_id": 7123456789,
        "app_image_digest": readback.app_image_digest,
        "bundle_digest": readback.bundle_digest,
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "workflow_id": workflow_id,
        "immutable_artifact_reference": (
            "388460142431.dkr.ecr.us-east-1.amazonaws.com/neurostack/api@"
            + readback.app_image_digest
        ),
    }
    now = datetime.now(timezone.utc)
    attestation_envelope = build_envelope(
        entry=build_entry,
        payload=attestation_payload,
        operation_id=operation_id,
        issued_at=now,
        expires_at=now + timedelta(days=3),
        signer=build_signer,
    )
    attestation = BillingProducerV1BuildAttestation(
        id=attestation_id,
        operation_id=operation_id,
        envelope=attestation_envelope,
        envelope_digest=envelope_digest(attestation_envelope),
        payload_digest=payload_digest(attestation_payload),
        registered_by_user_id=issuer_id,
        registered_at=now,
    )
    db.attestations[attestation_id] = attestation

    create_scope = routes._create_scope(
        operation_id=operation_id,
        receipt_id=deploy_id,
        verification_receipt_id=verify_id,
        supersedes=None,
    )
    assignment = _grant(
        signer=grant_signer,
        entry=grant_entry,
        grant_id=uuid4(),
        kind="assignment",
        operation_id=operation_id,
        actor_ids=(issuer_id, executor_id, approver_id),
        action="create_billing_producer_deploy_receipt",
        receipt_id=deploy_id,
        verification_receipt_id=verify_id,
        scope=create_scope,
        issued_at=now,
    )
    authorization = _grant(
        signer=grant_signer,
        entry=authorization_entry,
        grant_id=uuid4(),
        kind="authorization",
        operation_id=operation_id,
        actor_ids=(issuer_id, executor_id, approver_id),
        action="create_billing_producer_deploy_receipt",
        receipt_id=deploy_id,
        verification_receipt_id=verify_id,
        scope=create_scope,
        assignment=assignment,
        issued_at=now,
    )
    db.grants.update({assignment.id: assignment, authorization.id: authorization})
    test_grant_trust = routes._v1_trust_entry("billing_producer_assignment.v1")
    assert test_grant_trust.tuple_key() == tuple(
        str(assignment.envelope[field])
        for field in (
            "issuer",
            "kid",
            "environment",
            "audience",
            "type",
            "purpose",
            "schema_version",
            "algorithm",
        )
    )
    routes.verify_envelope(
        assignment.envelope,
        routes.TrustRegistry([test_grant_trust]),
        expected_type="billing_producer_assignment.v1",
    )
    routes.verify_envelope(
        authorization.envelope,
        routes.TrustRegistry(
            [routes._v1_trust_entry("billing_producer_authorization.v1")]
        ),
        expected_type="billing_producer_authorization.v1",
    )
    assert routes._v1_grant_row_is_valid(assignment)
    assert routes._v1_grant_row_is_valid(authorization)
    create_body = routes.ProducerCreateRequest(
        operation_id=operation_id,
        receipt_id=deploy_id,
        verification_receipt_id=verify_id,
        assignment_id=assignment.id,
        authorization_receipt_id=authorization.id,
        readback=routes.ProducerReadbackRequest.model_validate(
            {
                key: value
                for key, value in readback_payload.items()
                if key not in {"environment", "operational_role", "task_set_id"}
            }
        ),
        build_attestation_id=attestation_id,
        build_attestation_digest=attestation.envelope_digest,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        workflow_id=workflow_id,
        expected_no_current_active_pair=True,
    )
    mismatched_body = create_body.model_copy(update={"commit_sha": "e" * 64})
    with pytest.raises(HTTPException) as mismatch:
        await routes.create_producer_receipt(
            mismatched_body, _request(), _tenant(executor_id), db
        )
    assert mismatch.value.status_code == 409
    assert db.receipts == {}
    assert db.consumptions == []
    assert signer_factory_calls == 0

    created = await routes.create_producer_receipt(
        create_body, _request(), _tenant(executor_id), db
    )
    assert created["receipt_id"] == str(deploy_id)
    assert created["receipt_digest"] == envelope_digest(created["envelope"])
    assert (
        verify_envelope(
            created["envelope"],
            TrustRegistry([deploy_entry]),
            expected_type=deploy_entry.type,
        )
        == deploy_entry
    )
    assert signer_factory_calls == 1
    assert len(db.consumptions) == 2
    deploy = db.receipts[deploy_id]

    verify_scope = {
        "operation_id": str(operation_id),
        "receipt_id": str(deploy_id),
        "receipt_digest": deploy.envelope_digest,
        "verification_receipt_id": str(verify_id),
        "readback_digest": readback_digest,
        "supersedes": None,
    }
    verify_assignment = _grant(
        signer=grant_signer,
        entry=grant_entry,
        grant_id=uuid4(),
        kind="assignment",
        operation_id=operation_id,
        actor_ids=(verifier_issuer_id, verifier_id, verifier_approver_id),
        action="verify_billing_producer_deploy_receipt",
        receipt_id=deploy_id,
        verification_receipt_id=verify_id,
        scope=verify_scope,
        issued_at=now,
    )
    verify_authorization = _grant(
        signer=grant_signer,
        entry=authorization_entry,
        grant_id=uuid4(),
        kind="authorization",
        operation_id=operation_id,
        actor_ids=(verifier_issuer_id, verifier_id, verifier_approver_id),
        action="verify_billing_producer_deploy_receipt",
        receipt_id=deploy_id,
        verification_receipt_id=verify_id,
        scope=verify_scope,
        assignment=verify_assignment,
        issued_at=now,
    )
    db.grants.update(
        {
            verify_assignment.id: verify_assignment,
            verify_authorization.id: verify_authorization,
        }
    )
    verify_body = routes.ProducerVerifyRequest(
        operation_id=operation_id,
        receipt_id=deploy_id,
        verification_receipt_id=verify_id,
        assignment_id=verify_assignment.id,
        authorization_receipt_id=verify_authorization.id,
        readback=create_body.readback,
        expected_no_current_active_pair=True,
    )
    verified = await routes.verify_producer_receipt(
        verify_body, _request(), _tenant(verifier_id), db
    )
    assert verified["receipt_id"] == str(verify_id)
    assert verified["receipt_digest"] == envelope_digest(verified["envelope"])
    assert (
        verify_envelope(
            verified["envelope"],
            TrustRegistry([verify_entry]),
            expected_type=verify_entry.type,
        )
        == verify_entry
    )
    assert signer_factory_calls == 2
    assert (
        verified["envelope"]["payload"]["source_receipt_digest"]
        == deploy.envelope_digest
    )
    assert db.active_pair.receipt_id == deploy_id
    assert db.active_pair.verification_receipt_id == verify_id
    assert len(db.consumptions) == 4
