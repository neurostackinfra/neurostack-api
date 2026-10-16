from __future__ import annotations

import json
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

from src.api.middleware import auth as auth_middleware
from src.api.middleware.auth import TenantContext
from src.api.routes import billing_producer_setup as routes
from src.config.settings import settings
from src.models.billing_producer_signed import (
    BillingProducerV1ActivePair,
    BillingProducerV1Grant,
    BillingProducerV1GrantRevocation,
)
from src.models.user import UserRole
from src.services.billing_producer_handoff import (
    CLUSTER_ARN,
    SECRET_REFERENCE_NAMES,
    SERVICE_ARN,
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
    sha256_hex,
    verify_envelope,
)


def test_staging_jwt_method_path_allowlist_is_exact() -> None:
    assert auth_middleware._STAGING_REHEARSAL_TARGETS == frozenset(
        {
            ("POST", "/v1/admin/billing/producer-grants/assignments"),
            ("POST", "/v1/admin/billing/producer-grants/authorizations"),
            ("POST", "/v1/admin/billing/producer-grants/revocations"),
            ("POST", "/v1/admin/billing/producer-build-attestations"),
            ("GET", "/v1/admin/billing/rollout/readback"),
            ("POST", "/v1/admin/billing/producer-receipts/preflight"),
            ("POST", "/v1/admin/billing/producer-receipts/create"),
            ("POST", "/v1/admin/billing/producer-receipts/verify"),
        }
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


class FakeSession:
    def __init__(self, users: dict[UUID, Any]):
        self.users = users
        self.grants: dict[UUID, BillingProducerV1Grant] = {}
        self.revocations: dict[UUID, BillingProducerV1GrantRevocation] = {}
        self.attestations: dict[UUID, Any] = {}
        self.receipts: dict[UUID, Any] = {}
        self.active_pair: BillingProducerV1ActivePair | None = None
        self.added: list[Any] = []
        self.scalar_calls = 0

    def begin(self) -> _Begin:
        return _Begin()

    async def scalar(self, statement: Any) -> Any:
        self.scalar_calls += 1
        entity = statement.column_descriptions[0]["entity"]
        params = statement.compile().params.values()
        uuids = [value for value in params if isinstance(value, UUID)]
        if entity is routes.User:
            return (
                self.users.get(uuids[0]) if uuids else next(iter(self.users.values()))
            )
        if entity is BillingProducerV1Grant:
            return self.grants.get(uuids[0]) if uuids else None
        if entity is BillingProducerV1GrantRevocation:
            return self.revocations.get(uuids[0]) if uuids else None
        if entity is routes.BillingProducerV1Receipt:
            return self.receipts.get(uuids[0]) if uuids else None
        if entity is routes.BillingProducerV1ActivePair:
            return self.active_pair
        # Scalar selects of a single ID column are used only for prior event checks.
        return None

    async def get(self, model: type[Any], key: UUID) -> Any:
        if model.__name__ == "BillingProducerV1BuildAttestation":
            return self.attestations.get(key)
        return None

    def add(self, value: Any) -> None:
        self.added.append(value)
        if isinstance(value, BillingProducerV1Grant):
            self.grants[value.id] = value
        elif isinstance(value, BillingProducerV1GrantRevocation):
            self.revocations[value.id] = value
        elif value.__class__.__name__ == "BillingProducerV1BuildAttestation":
            self.attestations[value.id] = value

    async def flush(self, values: Any = None) -> None:
        del values


def _request(document: dict[str, Any]) -> Request:
    body = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/admin/billing/producer-grants/assignments",
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


def _readback() -> dict[str, Any]:
    task_id = uuid4().hex
    return {
        "cluster_arn": CLUSTER_ARN,
        "service_arn": SERVICE_ARN,
        "deployment_id": "ecs-svc/active-test",
        "deployment_controller": "ECS",
        "task_definition_arn": (
            "arn:aws:ecs:us-east-1:388460142431:task-definition/"
            "neurostack-staging-app:8"
        ),
        "task_arns": [
            "arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/" f"{task_id}"
        ],
        "config_generation": "cfg-test",
        "compatibility_generation": "compat-test",
        "app_image_digest": "sha256:" + "a" * 64,
        "ecr_image_digest": "sha256:" + "a" * 64,
        "bundle_digest": "b" * 64,
        "readiness": "ready",
        "rollout_state": "COMPLETED",
        "rollout_control": "disabled",
        "topup_commercial_enabled": False,
        "secret_references": sorted(
            [
                {
                    "name": name,
                    "arn": (
                        "arn:aws:secretsmanager:us-east-1:388460142431:secret:"
                        f"{path}-AbCdEf"
                    ),
                    "version_id": "task-definition-unpinned",
                }
                for name, path in SECRET_REFERENCE_NAMES.items()
            ],
            key=lambda item: (item["name"], item["arn"], item["version_id"]),
        ),
    }


def _trust_entry(type_name: str, signer: P256TestSigner) -> TrustEntry:
    return TrustEntry(
        issuer="test:billing-producer",
        kid=(
            "arn:aws:kms:us-east-1:388460142431:key/"
            "01234567-89ab-cdef-0123-456789abcdef"
        ),
        environment=ENVIRONMENT,
        audience="test:billing-producer",
        type=type_name,
        purpose=PURPOSE_BY_TYPE[type_name],
        schema_version=SCHEMA_VERSION,
        algorithm=ALGORITHM,
        public_key_pem=signer.public_key_pem,
    )


def _configure_grant_trust(
    monkeypatch: pytest.MonkeyPatch, signer: P256TestSigner
) -> None:
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(
        settings, "billing_producer_grant_issuer", "test:billing-producer"
    )
    monkeypatch.setattr(
        settings,
        "billing_producer_grant_kid",
        "arn:aws:kms:us-east-1:388460142431:key/01234567-89ab-cdef-0123-456789abcdef",
    )
    monkeypatch.setattr(
        settings, "billing_producer_grant_audience", "test:billing-producer"
    )
    monkeypatch.setattr(
        settings, "billing_producer_grant_public_key", signer.public_key_pem
    )
    monkeypatch.setattr(routes, "_grant_signer", lambda: signer)


@pytest.mark.asyncio
async def test_assignment_authorization_and_revocation_are_signed_with_distinct_humans(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = P256TestSigner()
    _configure_grant_trust(monkeypatch, signer)
    issuer_id, executor_id, approver_id, revoker_id = (uuid4() for _ in range(4))
    users = {
        user_id: SimpleNamespace(is_active=True, role=UserRole.super_admin)
        for user_id in (issuer_id, executor_id, approver_id, revoker_id)
    }
    db = FakeSession(users)
    operation_id = uuid4()
    assignment_id = uuid4()
    assignment_request = {
        "schema_version": "billing_producer_assignment_request.v1",
        "assignment_id": str(assignment_id),
        "operation_id": str(operation_id),
        "executor_user_id": str(executor_id),
        "action": "read_billing_producer_pair",
        "receipt_ids": [],
        "ttl_seconds": 300,
        "minimum_grant_validity_seconds": 240,
        "minimum_pair_validity_seconds": 300,
        "readback": _readback(),
    }
    assignment_result = await routes.issue_producer_assignment(
        _request(assignment_request), _tenant(issuer_id), db
    )
    assignment_envelope = assignment_result["envelope"]
    assignment_entry = _trust_entry("billing_producer_assignment.v1", signer)
    assert (
        verify_envelope(
            assignment_envelope,
            TrustRegistry([assignment_entry]),
            expected_type=assignment_entry.type,
        )
        == assignment_entry
    )
    assert assignment_envelope["payload"]["issuer_user_id"] == str(issuer_id)
    assert assignment_envelope["payload"]["executor_user_id"] == str(executor_id)
    assert (
        assignment_envelope["payload"]["scope"]["minimum_grant_validity_seconds"] == 240
    )
    assert (
        assignment_envelope["payload"]["scope"]["minimum_pair_validity_seconds"] == 300
    )
    assert assignment_result["assignment_digest"] == envelope_digest(
        assignment_envelope
    )

    authorization_id = uuid4()
    authorization_request = {
        "schema_version": "billing_producer_authorization_request.v1",
        "authorization_id": str(authorization_id),
        "assignment_id": str(assignment_id),
        "assignment_digest": assignment_result["assignment_digest"],
        "ttl_seconds": 120,
    }
    authorization_result = await routes.issue_producer_authorization(
        _request(authorization_request), _tenant(approver_id), db
    )
    authorization_envelope = authorization_result["envelope"]
    authorization_entry = _trust_entry("billing_producer_authorization.v1", signer)
    assert (
        verify_envelope(
            authorization_envelope,
            TrustRegistry([authorization_entry]),
            expected_type=authorization_entry.type,
        )
        == authorization_entry
    )
    authorization_payload = authorization_envelope["payload"]
    assert (
        authorization_payload["assignment_digest"]
        == assignment_result["assignment_digest"]
    )
    assert authorization_payload["approver_user_id"] == str(approver_id)
    assert authorization_payload["issuer_user_id"] == str(issuer_id)
    assert authorization_payload["executor_user_id"] == str(executor_id)

    revocation_id = uuid4()
    revocation_result = await routes.revoke_producer_grant(
        _request(
            {
                "schema_version": "billing_producer_revocation_request.v1",
                "revocation_id": str(revocation_id),
                "grant_type": "authorization",
                "grant_id": str(authorization_id),
                "grant_digest": authorization_result["authorization_digest"],
                "reason_code": "operator_requested",
            }
        ),
        _tenant(revoker_id),
        db,
    )
    revocation_entry = _trust_entry("billing_producer_grant_revocation.v1", signer)
    assert (
        verify_envelope(
            revocation_result["envelope"],
            TrustRegistry([revocation_entry]),
            expected_type=revocation_entry.type,
        )
        == revocation_entry
    )
    assert revocation_result["envelope"]["payload"]["revoker_user_id"] == str(
        revoker_id
    )
    assert len(db.added) == 3

    deploy_receipt_id, verify_receipt_id = uuid4(), uuid4()
    create_assignment = await routes.issue_producer_assignment(
        _request(
            {
                "schema_version": "billing_producer_assignment_request.v1",
                "assignment_id": str(uuid4()),
                "operation_id": str(operation_id),
                "executor_user_id": str(executor_id),
                "action": "create_billing_producer_deploy_receipt",
                "receipt_ids": [str(deploy_receipt_id), str(verify_receipt_id)],
                "ttl_seconds": 600,
            }
        ),
        _tenant(issuer_id),
        db,
    )
    assert create_assignment["envelope"]["payload"]["receipt_id"] == str(
        deploy_receipt_id
    )
    assert create_assignment["envelope"]["payload"]["verification_receipt_id"] == str(
        verify_receipt_id
    )

    readback = _readback()
    from src.services.billing_producer_handoff import ProducerReadback

    domain_readback = ProducerReadback(
        cluster_arn=readback["cluster_arn"],
        service_arn=readback["service_arn"],
        deployment_id=readback["deployment_id"],
        deployment_controller=readback["deployment_controller"],
        task_definition_arn=readback["task_definition_arn"],
        task_arns=tuple(readback["task_arns"]),
        config_generation=readback["config_generation"],
        compatibility_generation=readback["compatibility_generation"],
        app_image_digest=readback["app_image_digest"],
        ecr_image_digest=readback["ecr_image_digest"],
        bundle_digest=readback["bundle_digest"],
        readiness=readback["readiness"],
        rollout_state=readback["rollout_state"],
        rollout_control=readback["rollout_control"],
        topup_commercial_enabled=readback["topup_commercial_enabled"],
        secret_references=tuple(
            (item["name"], item["arn"], item["version_id"])
            for item in readback["secret_references"]
        ),
    )
    receipt_signer = P256TestSigner()
    receipt_entry = _trust_entry("billing_producer_deployed.v1", receipt_signer)
    receipt_now = datetime.now(timezone.utc)
    deployed_payload = {
        "operation_id": str(operation_id),
        "receipt_id": str(deploy_receipt_id),
        "verification_receipt_id": str(verify_receipt_id),
        "receipt_type": "billing_producer_deployed.v1",
        "executor_user_id": str(executor_id),
        "environment": "staging",
        "operational_role": "production-serving",
        "readback": domain_readback.as_payload(),
        "readback_digest": sha256_hex(jcs(domain_readback.as_payload())),
        "build_attestation_id": str(uuid4()),
        "build_attestation_digest": "b" * 64,
        "commit_sha": "c" * 64,
        "tree_sha": "d" * 64,
        "workflow_id": "github-actions:12345678:.github/workflows/asaas-build-attestation.yml",
        "supersedes": None,
    }
    deployed_envelope = build_envelope(
        entry=receipt_entry,
        payload=deployed_payload,
        operation_id=operation_id,
        issued_at=receipt_now,
        expires_at=receipt_now + timedelta(days=1),
        signer=receipt_signer,
    )
    receipt_digest = envelope_digest(deployed_envelope)
    db.receipts[deploy_receipt_id] = SimpleNamespace(
        id=deploy_receipt_id,
        operation_id=operation_id,
        receipt_type="billing_producer_deployed.v1",
        envelope=deployed_envelope,
        envelope_digest=receipt_digest,
        readback_digest=deployed_payload["readback_digest"],
        expires_at=receipt_now + timedelta(days=1),
    )
    monkeypatch.setattr(
        settings, "billing_producer_receipt_issuer", receipt_entry.issuer
    )
    monkeypatch.setattr(settings, "billing_producer_receipt_kid", receipt_entry.kid)
    monkeypatch.setattr(
        settings, "billing_producer_receipt_audience", receipt_entry.audience
    )
    monkeypatch.setattr(
        settings, "billing_producer_receipt_public_key", receipt_entry.public_key_pem
    )
    verify_assignment = await routes.issue_producer_assignment(
        _request(
            {
                "schema_version": "billing_producer_assignment_request.v1",
                "assignment_id": str(uuid4()),
                "operation_id": str(operation_id),
                "executor_user_id": str(executor_id),
                "action": "verify_billing_producer_deploy_receipt",
                "receipt_ids": [str(deploy_receipt_id), str(verify_receipt_id)],
                "ttl_seconds": 600,
            }
        ),
        _tenant(issuer_id),
        db,
    )
    verify_scope = verify_assignment["envelope"]["payload"]["scope"]
    assert verify_scope == {
        "operation_id": str(operation_id),
        "receipt_id": str(deploy_receipt_id),
        "receipt_digest": receipt_digest,
        "verification_receipt_id": str(verify_receipt_id),
        "readback_digest": deployed_payload["readback_digest"],
        "supersedes": None,
    }


@pytest.mark.asyncio
async def test_read_assignment_defaults_to_short_grant_and_fixed_pair_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = P256TestSigner()
    _configure_grant_trust(monkeypatch, signer)
    issuer_id, executor_id = uuid4(), uuid4()
    db = FakeSession(
        {
            issuer_id: SimpleNamespace(is_active=True, role=UserRole.super_admin),
            executor_id: SimpleNamespace(is_active=True, role=UserRole.super_admin),
        }
    )
    result = await routes.issue_producer_assignment(
        _request(
            {
                "schema_version": "billing_producer_assignment_request.v1",
                "assignment_id": str(uuid4()),
                "operation_id": str(uuid4()),
                "executor_user_id": str(executor_id),
                "action": "read_billing_producer_pair",
                "receipt_ids": [],
                "ttl_seconds": 300,
                "readback": _readback(),
            }
        ),
        _tenant(issuer_id),
        db,
    )

    scope = result["envelope"]["payload"]["scope"]
    assert scope["minimum_grant_validity_seconds"] == 60
    assert scope["minimum_pair_validity_seconds"] == 300


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("minimum_grant_validity_seconds", 59),
        ("minimum_grant_validity_seconds", 301),
        ("minimum_grant_validity_seconds", True),
        ("minimum_pair_validity_seconds", 299),
    ],
)
@pytest.mark.asyncio
async def test_read_assignment_rejects_invalid_freshness_windows_before_db_mutation(
    monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    signer = P256TestSigner()
    _configure_grant_trust(monkeypatch, signer)
    issuer_id, executor_id = uuid4(), uuid4()
    db = FakeSession({})
    document = {
        "schema_version": "billing_producer_assignment_request.v1",
        "assignment_id": str(uuid4()),
        "operation_id": str(uuid4()),
        "executor_user_id": str(executor_id),
        "action": "read_billing_producer_pair",
        "receipt_ids": [],
        "ttl_seconds": 300,
        "minimum_grant_validity_seconds": 60,
        "minimum_pair_validity_seconds": 300,
        "readback": _readback(),
    }
    document[field] = value

    with pytest.raises(HTTPException) as error:
        await routes.issue_producer_assignment(
            _request(document), _tenant(issuer_id), db
        )

    assert error.value.status_code == 422
    assert db.added == []
    assert db.scalar_calls == 0


@pytest.mark.asyncio
async def test_assignment_rejects_noncanonical_actor_and_extra_fields_without_db_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = P256TestSigner()
    _configure_grant_trust(monkeypatch, signer)
    issuer_id = uuid4()
    db = FakeSession({})
    invalid = {
        "schema_version": "billing_producer_assignment_request.v1",
        "assignment_id": str(uuid4()),
        "operation_id": str(uuid4()),
        "executor_user_id": str(issuer_id),
        "action": "read_billing_producer_pair",
        "receipt_ids": [],
        "ttl_seconds": 300,
        "minimum_grant_validity_seconds": 240,
        "minimum_pair_validity_seconds": 300,
        "readback": _readback(),
        "scope_digest": "caller-controlled",
    }
    with pytest.raises(HTTPException) as error:
        await routes.issue_producer_assignment(
            _request(invalid), _tenant(issuer_id), db
        )
    assert error.value.status_code == 422
    assert db.added == []
    assert db.scalar_calls == 0

    invalid.pop("scope_digest")
    with pytest.raises(HTTPException) as error:
        await routes.issue_producer_assignment(
            _request(invalid), _tenant(issuer_id), db
        )
    assert error.value.status_code == 403
    assert db.added == []
    assert db.scalar_calls == 0


@pytest.mark.parametrize(
    "invalid_user",
    [
        SimpleNamespace(is_active=False, role=UserRole.super_admin),
        SimpleNamespace(is_active=True, role=UserRole.clinic_admin),
    ],
)
@pytest.mark.asyncio
async def test_assignment_requires_every_actor_to_remain_active_super_admin(
    monkeypatch: pytest.MonkeyPatch, invalid_user: Any
) -> None:
    signer = P256TestSigner()
    _configure_grant_trust(monkeypatch, signer)
    issuer_id, executor_id = uuid4(), uuid4()
    db = FakeSession(
        {
            issuer_id: invalid_user,
            executor_id: SimpleNamespace(is_active=True, role=UserRole.super_admin),
        }
    )
    document = {
        "schema_version": "billing_producer_assignment_request.v1",
        "assignment_id": str(uuid4()),
        "operation_id": str(uuid4()),
        "executor_user_id": str(executor_id),
        "action": "read_billing_producer_pair",
        "receipt_ids": [],
        "ttl_seconds": 300,
        "minimum_grant_validity_seconds": 240,
        "minimum_pair_validity_seconds": 300,
        "readback": _readback(),
    }

    with pytest.raises(HTTPException) as error:
        await routes.issue_producer_assignment(
            _request(document), _tenant(issuer_id), db
        )

    assert error.value.status_code == 403
    assert db.added == []


@pytest.mark.asyncio
async def test_build_attestation_registration_requires_exact_pinned_ci_trust(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    signer = P256TestSigner()
    attestation_type = "billing_build_attestation.v1"
    entry = TrustEntry(
        **{
            **_trust_entry(attestation_type, signer).__dict__,
            "repository_id": "12345678",
            "repository_name": "testorg/neurostack-api",
            "workflow_path": ".github/workflows/asaas-build-attestation.yml",
            "workflow_ref": "refs/heads/main",
            "oidc_issuer": "https://token.actions.githubusercontent.com",
            "oidc_subject": "repo:testorg/neurostack-api:ref:refs/heads/main",
        }
    )
    pins = {
        "billing_producer_build_issuer": entry.issuer,
        "billing_producer_build_kid": entry.kid,
        "billing_producer_build_audience": entry.audience,
        "billing_producer_build_public_key": entry.public_key_pem,
        "billing_producer_build_repository_id": entry.repository_id,
        "billing_producer_build_repository_name": entry.repository_name,
        "billing_producer_build_workflow_path": entry.workflow_path,
        "billing_producer_build_workflow_ref": entry.workflow_ref,
        "billing_producer_build_oidc_issuer": entry.oidc_issuer,
        "billing_producer_build_oidc_subject": entry.oidc_subject,
    }
    monkeypatch.setattr(settings, "environment", "staging")
    for name, value in pins.items():
        monkeypatch.setattr(settings, name, value)
    attestor_id = uuid4()
    db = FakeSession(
        {attestor_id: SimpleNamespace(is_active=True, role=UserRole.super_admin)}
    )
    operation_id = uuid4()
    attestation_id = uuid4()
    issued_at = datetime.now(timezone.utc)
    payload = {
        "attestation_type": attestation_type,
        "attestation_id": str(attestation_id),
        "repository_id": entry.repository_id,
        "repository_name": entry.repository_name,
        "workflow_path": entry.workflow_path,
        "workflow_ref": entry.workflow_ref,
        "workflow_run_id": 123456,
        "app_image_digest": "sha256:" + "a" * 64,
        "bundle_digest": "b" * 64,
        "commit_sha": "c" * 64,
        "tree_sha": "d" * 64,
        "workflow_id": f"github-actions:{entry.repository_id}:{entry.workflow_path}",
        "immutable_artifact_reference": (
            "388460142431.dkr.ecr.us-east-1.amazonaws.com/neurostack/api@"
            "sha256:" + "a" * 64
        ),
    }
    envelope = build_envelope(
        entry=entry,
        payload=payload,
        operation_id=operation_id,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(days=7),
        signer=signer,
    )
    body = {
        "schema_version": "billing_build_attestation_registration.v1",
        "envelope": envelope,
    }
    result = await routes.register_build_attestation(
        _request(body), _tenant(attestor_id), db
    )
    assert result["attestation_id"] == str(attestation_id)
    assert result["attestation_digest"] == envelope_digest(envelope)
    assert len(db.added) == 1

    mismatched = {
        **payload,
        "repository_id": "99999999",
        "workflow_id": f"github-actions:99999999:{entry.workflow_path}",
    }
    bad_envelope = build_envelope(
        entry=entry,
        payload=mismatched,
        operation_id=operation_id,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(days=7),
        signer=signer,
    )
    before_calls = db.scalar_calls
    with pytest.raises(HTTPException) as error:
        await routes.register_build_attestation(
            _request(
                {
                    "schema_version": "billing_build_attestation_registration.v1",
                    "envelope": bad_envelope,
                }
            ),
            _tenant(attestor_id),
            db,
        )
    assert error.value.status_code == 422
    assert db.scalar_calls == before_calls
    assert len(db.added) == 1
