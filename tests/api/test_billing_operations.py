"""Backend authorization boundary used by the staging billing rehearsal.

The production billing routes remain outside this Group 7 change set.  These
tests pin the contract consumed from the backend: the client supplies a JWT,
while the backend supplies the actor and atomically consumes the assignment.
"""

from __future__ import annotations

import base64
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from pydantic import ValidationError

from scripts.staging_rehearsal_common import (
    STAGING_SCOPE,
    ActorSeparation,
    AuthorizationRequest,
    RehearsalError,
    SingleUseAuthorization,
)
from src.api.middleware.auth import TenantContext
from src.api.routes.billing_producer import (
    ProducerCreateRequest,
    ProducerReadbackRequest,
    ProducerVerifyRequest,
    _expected_current,
    _require_short_staging_jwt,
)
from src.config.settings import settings
from src.models.billing_producer import (
    BillingProducerActivePair,
    BillingProducerBuildAttestation,
    BillingProducerGrant,
    BillingProducerReceipt,
)
from src.repositories.billing_producer import (
    BillingProducerHandoffService,
    BillingProducerReceiptRepository,
)
from src.services.billing_producer_handoff import (
    CLUSTER_ARN,
    CREATE_ACTION,
    ENVIRONMENT,
    OPERATIONAL_ROLE,
    PREFLIGHT_ACTION,
    SECRET_REFERENCE_NAMES,
    SERVICE_ARN,
    VERIFY_ACTION,
    ProducerHandoffRejected,
    ProducerReadback,
    canonical_json,
    compute_task_set_id,
    digest_json,
    fingerprint,
    grant_claims,
    preflight_scope,
    receipt_expiry,
    receipt_scope,
    sign_receipt,
    verify_receipt_signature,
)


def _request() -> AuthorizationRequest:
    return AuthorizationRequest(
        environment="staging",
        scope=STAGING_SCOPE,
        operation_id=str(uuid4()),
        assignment_id=str(uuid4()),
        authorization_id=str(uuid4()),
    )


class Backend:
    def __init__(self, actor: str = "backend-derived-actor") -> None:
        self.actor = actor
        self.consumed: set[tuple[str, str]] = set()

    def consume(
        self, jwt_token: str, request: AuthorizationRequest
    ) -> dict[str, object]:
        assert jwt_token.startswith("ey")
        key = (request.assignment_id, request.authorization_id)
        if key in self.consumed:
            raise RehearsalError("assignment reused")
        self.consumed.add(key)
        return {
            "actor_id": self.actor,
            "environment": request.environment,
            "scope": request.scope,
            "operation_id": request.operation_id,
            "assignment_id": request.assignment_id,
            "authorization_id": request.authorization_id,
            "expires_at": (
                datetime.now(timezone.utc) + timedelta(minutes=2)
            ).isoformat(),
            "single_use": True,
        }


def test_staging_derived_actor_and_single_use_backend_contract() -> None:
    request = _request()
    authorizer = SingleUseAuthorization(Backend())
    grant = authorizer.consume("ey.short.jwt", request)
    assert grant.actor_id == "backend-derived-actor"
    with pytest.raises(RehearsalError, match="already consumed"):
        authorizer.consume("ey.short.jwt", request)


def test_actor_equality_is_rejected_for_separation_of_duties() -> None:
    request = _request()
    authorizer = SingleUseAuthorization(Backend(actor="one"))
    separation = ActorSeparation()
    deploy = authorizer.consume("ey.deploy", request)
    separation.bind("deploy", deploy)
    second_request = _request()
    attestation = authorizer.consume("ey.attestation", second_request)
    with pytest.raises(RehearsalError, match="distinct"):
        separation.bind("attestation", attestation)


@pytest.mark.parametrize(
    ("environment", "scope"),
    [("production", STAGING_SCOPE), ("staging", "billing.producer.production")],
)
def test_staging_auth_rejects_cross_environment_and_scope(
    environment: str, scope: str
) -> None:
    request = _request()
    invalid = AuthorizationRequest(
        environment=environment,
        scope=scope,
        operation_id=request.operation_id,
        assignment_id=request.assignment_id,
        authorization_id=request.authorization_id,
    )
    with pytest.raises(RehearsalError):
        invalid.validate()


def test_backend_contract_has_no_egress_or_mutator_authorization_actions() -> None:
    request = _request()
    backend = Backend()
    grant = SingleUseAuthorization(backend).consume("ey.short.jwt", request)
    assert grant.environment == "staging"
    assert not hasattr(backend, "charge")
    assert not hasattr(backend, "update_service")


def _producer_key_pair() -> tuple[Ed25519PrivateKey, str]:
    private_key = Ed25519PrivateKey.generate()
    public_pem = (
        private_key.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )
    return private_key, public_pem


def _private_key_pem(private_key: Ed25519PrivateKey) -> str:
    return private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def _signed_producer_grant(
    private_key: Ed25519PrivateKey,
    *,
    kind: str,
    actor_id: UUID,
    operation_id: UUID,
    scope_digest: str,
    action: str = CREATE_ACTION,
    issued_at: datetime | None = None,
    receipt_id: UUID | None = None,
    verification_receipt_id: UUID | None = None,
) -> BillingProducerGrant:
    issued_at = issued_at or datetime.now(timezone.utc)
    preflight = action == PREFLIGHT_ACTION
    grant = BillingProducerGrant(
        id=uuid4(),
        grant_kind=kind,
        actor_user_id=actor_id,
        operation_id=operation_id,
        environment=ENVIRONMENT,
        operational_role=OPERATIONAL_ROLE,
        cluster_arn=CLUSTER_ARN,
        service_arn=SERVICE_ARN,
        action=action,
        receipt_id=(None if preflight else receipt_id or uuid4()),
        verification_receipt_id=(
            None if preflight else verification_receipt_id or uuid4()
        ),
        scope_digest=scope_digest,
        issued_at=issued_at,
        expires_at=issued_at + timedelta(minutes=2),
        claims_signature="",
    )
    grant.claims_signature = base64.b64encode(
        private_key.sign(canonical_json(grant_claims(grant)))
    ).decode("ascii")
    return grant


class _FlushOnlySession:
    def add(self, _object: object) -> None:
        return None

    async def flush(self, _objects: object = None) -> None:
        return None


class _GrantRepository(BillingProducerReceiptRepository):
    def __init__(self, grants: list[BillingProducerGrant]):
        self.session = _FlushOnlySession()  # type: ignore[assignment]
        self.grants = {grant.id: grant for grant in grants}

    async def get_grants(
        self, grant_ids: tuple[UUID, UUID]
    ) -> tuple[BillingProducerGrant, BillingProducerGrant]:
        if grant_ids[0] == grant_ids[1]:
            raise ProducerHandoffRejected(
                "assignment and authorization IDs must differ"
            )
        try:
            return self.grants[grant_ids[0]], self.grants[grant_ids[1]]
        except KeyError:
            raise ProducerHandoffRejected(
                "active assignment or authorization is unavailable"
            ) from None


class _HandoffSession(_FlushOnlySession):
    def __init__(self, repository: "_HandoffRepository") -> None:
        self.repository = repository

    def add(self, obj: object) -> None:
        if isinstance(obj, BillingProducerReceipt):
            self.repository.receipts[obj.id] = obj
        elif isinstance(obj, BillingProducerActivePair):
            self.repository.pointer = obj


class _HandoffRepository(_GrantRepository):
    def __init__(
        self,
        grants: list[BillingProducerGrant],
        *,
        attestation: BillingProducerBuildAttestation,
    ) -> None:
        super().__init__(grants)
        self.session = _HandoffSession(self)  # type: ignore[assignment]
        self.pointer: BillingProducerActivePair | None = None
        self.receipts: dict[UUID, BillingProducerReceipt] = {}
        self.attestation = attestation

    async def get_active_pair(
        self, *, environment: str = ENVIRONMENT, for_update: bool = False
    ) -> BillingProducerActivePair | None:
        return self.pointer

    async def get_build_attestation(
        self, *, attestation_id: UUID, attestation_digest: str
    ) -> BillingProducerBuildAttestation | None:
        if (
            attestation_id != self.attestation.id
            or attestation_digest != self.attestation.payload_digest
        ):
            return None
        return self.attestation

    async def get_receipt(
        self,
        *,
        receipt_id: UUID,
        operation_id: UUID,
        receipt_type: str | None = None,
    ) -> BillingProducerReceipt | None:
        receipt = self.receipts.get(receipt_id)
        if (
            receipt is None
            or receipt.operation_id != operation_id
            or (receipt_type is not None and receipt.receipt_type != receipt_type)
        ):
            return None
        return receipt

    async def save_deploy_receipt(self, **kwargs: Any) -> BillingProducerReceipt:
        return await super().save_deploy_receipt(**kwargs)  # type: ignore[arg-type]

    async def activate_verified_pair(self, **kwargs: Any) -> BillingProducerActivePair:
        return await super().activate_verified_pair(**kwargs)  # type: ignore[arg-type]


def _grant_pair(
    private_key: Ed25519PrivateKey,
    *,
    actor_id: UUID,
    operation_id: UUID,
    scope_digest: str = "a" * 64,
    action: str = CREATE_ACTION,
    issued_at: datetime | None = None,
    receipt_id: UUID | None = None,
    verification_receipt_id: UUID | None = None,
) -> list[BillingProducerGrant]:
    receipt_id = receipt_id or uuid4()
    verification_receipt_id = verification_receipt_id or uuid4()
    return [
        _signed_producer_grant(
            private_key,
            kind=kind,
            actor_id=actor_id,
            operation_id=operation_id,
            scope_digest=scope_digest,
            action=action,
            issued_at=issued_at,
            receipt_id=receipt_id,
            verification_receipt_id=verification_receipt_id,
        )
        for kind in ("assignment", "authorization")
    ]


def _add_grant_pair(
    repository: _HandoffRepository,
    private_key: Ed25519PrivateKey,
    *,
    actor_id: UUID,
    operation_id: UUID,
    action: str,
    scope_digest: str,
    receipt_id: UUID,
    verification_receipt_id: UUID,
    issued_at: datetime,
) -> list[BillingProducerGrant]:
    grants = _grant_pair(
        private_key,
        actor_id=actor_id,
        operation_id=operation_id,
        action=action,
        scope_digest=scope_digest,
        issued_at=issued_at,
        receipt_id=receipt_id,
        verification_receipt_id=verification_receipt_id,
    )
    repository.grants.update({grant.id: grant for grant in grants})
    return grants


def _handoff_setup(now: datetime) -> dict[str, Any]:
    private_key, public_key = _producer_key_pair()
    private_pem = _private_key_pem(private_key)
    operation_id = uuid4()
    receipt_id, verification_receipt_id = uuid4(), uuid4()
    actor_id = uuid4()
    readback = _producer_readback()
    build_id = uuid4()
    commit_sha, tree_sha, workflow_id = "c" * 64, "d" * 64, "workflow-123"
    attestation_payload = {
        "attestation_type": "billing_build_attestation.v1",
        "app_image_digest": readback.app_image_digest,
        "bundle_digest": readback.bundle_digest,
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "workflow_id": workflow_id,
        "immutable_artifact_reference": (
            "388460142431.dkr.ecr.us-east-1.amazonaws.com/neurostack-staging@"
            + readback.app_image_digest
        ),
    }
    attestation_digest, attestation_signature = sign_receipt(
        attestation_payload, private_pem
    )
    attestation = BillingProducerBuildAttestation(
        id=build_id,
        payload=attestation_payload,
        payload_digest=attestation_digest,
        signature=attestation_signature,
    )
    scope = receipt_scope(
        operation_id=operation_id,
        receipt_id=receipt_id,
        verification_receipt_id=verification_receipt_id,
        readback=readback,
        build_attestation_id=build_id,
        build_attestation_digest=attestation_digest,
        commit_sha=commit_sha,
        tree_sha=tree_sha,
        workflow_id=workflow_id,
    )
    grants = _grant_pair(
        private_key,
        actor_id=actor_id,
        operation_id=operation_id,
        action=CREATE_ACTION,
        scope_digest=digest_json(scope),
        issued_at=now - timedelta(seconds=30),
        receipt_id=receipt_id,
        verification_receipt_id=verification_receipt_id,
    )
    repository = _HandoffRepository(grants, attestation=attestation)
    service = BillingProducerHandoffService(
        repository,
        authorization_public_key=public_key,
        receipt_signing_key=private_pem,
        receipt_public_key=public_key,
        build_attestation_public_key=public_key,
    )
    return {
        "private_key": private_key,
        "public_key": public_key,
        "private_pem": private_pem,
        "operation_id": operation_id,
        "receipt_id": receipt_id,
        "verification_receipt_id": verification_receipt_id,
        "actor_id": actor_id,
        "readback": readback,
        "build_id": build_id,
        "attestation_digest": attestation_digest,
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "workflow_id": workflow_id,
        "repository": repository,
        "service": service,
        "scope": scope,
    }


def _verification_scope(
    *,
    operation_id: UUID,
    deploy: BillingProducerReceipt,
    verification_receipt_id: UUID,
    readback: ProducerReadback,
    supersedes: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "operation_id": str(operation_id),
        "receipt_id": str(deploy.id),
        "receipt_digest": deploy.payload_digest,
        "verification_receipt_id": str(verification_receipt_id),
        "readback_digest": digest_json(readback.as_payload()),
        "deployment_fingerprint": fingerprint(deploy.payload),
        "supersedes": supersedes,
    }


@pytest.mark.asyncio
async def test_producer_grants_are_signed_scoped_and_single_use() -> None:
    private_key, public_key = _producer_key_pair()
    actor_id, operation_id = uuid4(), uuid4()
    grants = _grant_pair(private_key, actor_id=actor_id, operation_id=operation_id)
    repository = _GrantRepository(grants)

    await repository.validate_and_consume_grants(
        assignment_id=grants[0].id,
        authorization_id=grants[1].id,
        actor_user_id=actor_id,
        operation_id=operation_id,
        action=CREATE_ACTION,
        receipt_id=grants[0].receipt_id,
        verification_receipt_id=grants[0].verification_receipt_id,
        scope_digest="a" * 64,
        now=datetime.now(timezone.utc),
        authorization_public_key=public_key,
    )
    assert all(grant.consumed_at is not None for grant in grants)
    with pytest.raises(
        ProducerHandoffRejected, match="inactive or outside exact scope"
    ):
        await repository.validate_and_consume_grants(
            assignment_id=grants[0].id,
            authorization_id=grants[1].id,
            actor_user_id=actor_id,
            operation_id=operation_id,
            action=CREATE_ACTION,
            receipt_id=grants[0].receipt_id,
            verification_receipt_id=grants[0].verification_receipt_id,
            scope_digest="a" * 64,
            now=datetime.now(timezone.utc),
            authorization_public_key=public_key,
        )


@pytest.mark.asyncio
async def test_read_only_preflight_grants_are_single_use_and_have_no_receipt_scope() -> (
    None
):
    private_key, public_key = _producer_key_pair()
    actor_id, operation_id = uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    grants = [
        _signed_producer_grant(
            private_key,
            kind=kind,
            actor_id=actor_id,
            operation_id=operation_id,
            scope_digest="a" * 64,
            action=PREFLIGHT_ACTION,
            issued_at=now,
        )
        for kind in ("assignment", "authorization")
    ]
    repository = _GrantRepository(grants)
    await repository.validate_and_consume_grants(
        assignment_id=grants[0].id,
        authorization_id=grants[1].id,
        actor_user_id=actor_id,
        operation_id=operation_id,
        action=PREFLIGHT_ACTION,
        receipt_id=None,
        verification_receipt_id=None,
        scope_digest="a" * 64,
        now=now + timedelta(seconds=1),
        authorization_public_key=public_key,
    )
    assert all(grant.receipt_id is None for grant in grants)
    assert all(grant.verification_receipt_id is None for grant in grants)
    assert all(grant.consumed_at == now + timedelta(seconds=1) for grant in grants)
    with pytest.raises(
        ProducerHandoffRejected, match="inactive or outside exact scope"
    ):
        await repository.validate_and_consume_grants(
            assignment_id=grants[0].id,
            authorization_id=grants[1].id,
            actor_user_id=actor_id,
            operation_id=operation_id,
            action=PREFLIGHT_ACTION,
            receipt_id=None,
            verification_receipt_id=None,
            scope_digest="a" * 64,
            now=now + timedelta(seconds=2),
            authorization_public_key=public_key,
        )

    long_lived = [
        _signed_producer_grant(
            private_key,
            kind=kind,
            actor_id=actor_id,
            operation_id=operation_id,
            scope_digest="a" * 64,
            action=PREFLIGHT_ACTION,
            issued_at=now,
        )
        for kind in ("assignment", "authorization")
    ]
    long_lived[0].expires_at = now + timedelta(minutes=6)
    long_lived[0].claims_signature = base64.b64encode(
        private_key.sign(canonical_json(grant_claims(long_lived[0])))
    ).decode("ascii")
    long_lived_repository = _GrantRepository(long_lived)
    with pytest.raises(
        ProducerHandoffRejected, match="inactive or outside exact scope"
    ):
        await long_lived_repository.validate_and_consume_grants(
            assignment_id=long_lived[0].id,
            authorization_id=long_lived[1].id,
            actor_user_id=actor_id,
            operation_id=operation_id,
            action=PREFLIGHT_ACTION,
            receipt_id=None,
            verification_receipt_id=None,
            scope_digest="a" * 64,
            now=now + timedelta(seconds=1),
            authorization_public_key=public_key,
        )


@pytest.mark.parametrize(
    "failure", ["revoked", "cross_scope", "wrong_actor", "expired", "wrong_action"]
)
@pytest.mark.asyncio
async def test_producer_grants_reject_revocation_and_cross_scope_replay(
    failure: str,
) -> None:
    private_key, public_key = _producer_key_pair()
    actor_id, operation_id = uuid4(), uuid4()
    now = datetime.now(timezone.utc)
    grants = _grant_pair(
        private_key,
        actor_id=actor_id,
        operation_id=operation_id,
        issued_at=now - timedelta(minutes=1),
    )
    if failure == "revoked":
        grants[0].revoked_at = now - timedelta(seconds=1)
    elif failure == "cross_scope":
        grants[1].scope_digest = "b" * 64
        grants[1].claims_signature = base64.b64encode(
            private_key.sign(canonical_json(grant_claims(grants[1])))
        ).decode("ascii")
    elif failure == "wrong_actor":
        grants[1].actor_user_id = uuid4()
        grants[1].claims_signature = base64.b64encode(
            private_key.sign(canonical_json(grant_claims(grants[1])))
        ).decode("ascii")
    elif failure == "expired":
        grants[0].expires_at = now - timedelta(seconds=1)
        grants[0].claims_signature = base64.b64encode(
            private_key.sign(canonical_json(grant_claims(grants[0])))
        ).decode("ascii")
    elif failure == "wrong_action":
        grants[1].action = VERIFY_ACTION
        grants[1].claims_signature = base64.b64encode(
            private_key.sign(canonical_json(grant_claims(grants[1])))
        ).decode("ascii")

    repository = _GrantRepository(grants)
    with pytest.raises(ProducerHandoffRejected):
        await repository.validate_and_consume_grants(
            assignment_id=grants[0].id,
            authorization_id=grants[1].id,
            actor_user_id=actor_id,
            operation_id=operation_id,
            action=CREATE_ACTION,
            receipt_id=grants[0].receipt_id,
            verification_receipt_id=grants[0].verification_receipt_id,
            scope_digest="a" * 64,
            now=now,
            authorization_public_key=public_key,
        )


def _producer_readback(**changes: Any) -> ProducerReadback:
    values: dict[str, Any] = {
        "cluster_arn": CLUSTER_ARN,
        "service_arn": SERVICE_ARN,
        "deployment_id": "ecs-svc/active-deployment",
        "deployment_controller": "ECS",
        "task_definition_arn": "arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:8",
        "task_arns": (
            "arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/" + "1" * 32,
            "arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/" + "2" * 32,
        ),
        "config_generation": "cfg-8",
        "compatibility_generation": "compat-7",
        "app_image_digest": "sha256:" + "a" * 64,
        "ecr_image_digest": "sha256:" + "a" * 64,
        "bundle_digest": "b" * 64,
        "readiness": "ready",
        "rollout_state": "COMPLETED",
        "rollout_control": "disabled",
        "topup_commercial_enabled": False,
        "secret_references": tuple(
            (
                name,
                "arn:aws:secretsmanager:us-east-1:388460142431:secret:"
                f"{path}-AbCdEf",
                "AWSCURRENT",
            )
            for name, path in SECRET_REFERENCE_NAMES.items()
        ),
    }
    values.update(changes)
    return ProducerReadback(**values)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_producer_initial_pair_preflight_refresh_and_competing_cas() -> None:
    now = datetime.now(timezone.utc)
    fixture = _handoff_setup(now)
    repository = fixture["repository"]
    service = fixture["service"]
    operation_id = fixture["operation_id"]
    readback = fixture["readback"]
    actor_id = fixture["actor_id"]
    receipt_id = fixture["receipt_id"]
    verification_receipt_id = fixture["verification_receipt_id"]

    deploy = await service.create(
        actor_user_id=actor_id,
        operation_id=operation_id,
        receipt_id=receipt_id,
        verification_receipt_id=verification_receipt_id,
        assignment_id=next(
            grant.id
            for grant in repository.grants.values()
            if grant.grant_kind == "assignment" and grant.action == CREATE_ACTION
        ),
        authorization_id=next(
            grant.id
            for grant in repository.grants.values()
            if grant.grant_kind == "authorization" and grant.action == CREATE_ACTION
        ),
        readback=readback,
        build_attestation_id=fixture["build_id"],
        build_attestation_digest=fixture["attestation_digest"],
        commit_sha=fixture["commit_sha"],
        tree_sha=fixture["tree_sha"],
        workflow_id=fixture["workflow_id"],
        expected_current=None,
        now=now,
    )
    assert repository.pointer is None
    assert deploy.task_set_id == compute_task_set_id(readback)

    verifier_actor = uuid4()
    verify_scope = _verification_scope(
        operation_id=operation_id,
        deploy=deploy,
        verification_receipt_id=verification_receipt_id,
        readback=readback,
    )
    verifier_grants = _add_grant_pair(
        repository,
        fixture["private_key"],
        actor_id=verifier_actor,
        operation_id=operation_id,
        action=VERIFY_ACTION,
        scope_digest=digest_json(verify_scope),
        receipt_id=receipt_id,
        verification_receipt_id=verification_receipt_id,
        issued_at=now,
    )
    changed_cohort = replace(readback, task_arns=readback.task_arns[:1])
    with pytest.raises(ProducerHandoffRejected, match="live readback differs"):
        await service.verify(
            actor_user_id=verifier_actor,
            operation_id=operation_id,
            receipt_id=receipt_id,
            verification_receipt_id=verification_receipt_id,
            assignment_id=verifier_grants[0].id,
            authorization_id=verifier_grants[1].id,
            readback=changed_cohort,
            expected_current=None,
            now=now + timedelta(seconds=1),
        )
    missing_snapshot = {
        "receipt_id": str(uuid4()),
        "receipt_digest": "a" * 64,
        "verification_receipt_id": str(uuid4()),
        "verification_receipt_digest": "b" * 64,
    }
    with pytest.raises(ProducerHandoffRejected, match="active pair disappeared"):
        await service.verify(
            actor_user_id=verifier_actor,
            operation_id=operation_id,
            receipt_id=receipt_id,
            verification_receipt_id=verification_receipt_id,
            assignment_id=verifier_grants[0].id,
            authorization_id=verifier_grants[1].id,
            readback=readback,
            expected_current=missing_snapshot,
            now=now + timedelta(seconds=1),
        )
    verified = await service.verify(
        actor_user_id=verifier_actor,
        operation_id=operation_id,
        receipt_id=receipt_id,
        verification_receipt_id=verification_receipt_id,
        assignment_id=verifier_grants[0].id,
        authorization_id=verifier_grants[1].id,
        readback=readback,
        expected_current=None,
        now=now + timedelta(seconds=1),
    )
    assert repository.pointer is not None
    assert repository.pointer.receipt_id == deploy.id
    assert repository.pointer.verification_receipt_id == verified.id
    assert verified.task_set_id == deploy.task_set_id
    active_before = {
        "receipt_id": str(repository.pointer.receipt_id),
        "receipt_digest": repository.pointer.receipt_digest,
        "verification_receipt_id": str(repository.pointer.verification_receipt_id),
        "verification_receipt_digest": repository.pointer.verification_receipt_digest,
    }
    receipts_before_preflight = len(repository.receipts)
    pointer_before_preflight = repository.pointer

    drift_cases = {
        "deployment_id": replace(readback, deployment_id="ecs-svc/other-deployment"),
        "task_set_id": replace(
            readback,
            task_arns=(
                "arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/"
                + "3" * 32,
            ),
        ),
        "config_generation": replace(readback, config_generation="cfg-9"),
        "compatibility_generation": replace(
            readback, compatibility_generation="compat-8"
        ),
        "app_digest": replace(
            readback,
            app_image_digest="sha256:" + "c" * 64,
            ecr_image_digest="sha256:" + "c" * 64,
        ),
        "bundle_digest": replace(readback, bundle_digest="c" * 64),
    }
    for drift_name, drifted_readback in drift_cases.items():
        drifted_readback.as_payload()
        if drift_name == "task_set_id":
            assert compute_task_set_id(drifted_readback) != compute_task_set_id(
                readback
            )
        drift_scope = preflight_scope(
            operation_id=operation_id,
            readback=drifted_readback,
            minimum_validity_seconds=300,
        )
        drift_grants = _add_grant_pair(
            repository,
            fixture["private_key"],
            actor_id=uuid4(),
            operation_id=operation_id,
            action=PREFLIGHT_ACTION,
            scope_digest=digest_json(drift_scope),
            receipt_id=uuid4(),
            verification_receipt_id=uuid4(),
            issued_at=now - timedelta(seconds=10),
        )
        with pytest.raises(ProducerHandoffRejected, match="active pair"):
            await service.preflight(
                actor_user_id=drift_grants[0].actor_user_id,
                operation_id=operation_id,
                assignment_id=drift_grants[0].id,
                authorization_id=drift_grants[1].id,
                readback=drifted_readback,
                minimum_validity_seconds=300,
                now=now,
            )
        with pytest.raises(ProducerHandoffRejected, match="live readback"):
            await service.verify(
                actor_user_id=verifier_actor,
                operation_id=operation_id,
                receipt_id=receipt_id,
                verification_receipt_id=verification_receipt_id,
                assignment_id=verifier_grants[0].id,
                authorization_id=verifier_grants[1].id,
                readback=drifted_readback,
                expected_current=active_before,
                now=now + timedelta(seconds=1),
            )
        assert repository.pointer is pointer_before_preflight

    keep_time = now + timedelta(seconds=2)
    keep_scope = preflight_scope(
        operation_id=operation_id,
        readback=readback,
        minimum_validity_seconds=300,
    )
    keep_grants = _add_grant_pair(
        repository,
        fixture["private_key"],
        actor_id=uuid4(),
        operation_id=operation_id,
        action=PREFLIGHT_ACTION,
        scope_digest=digest_json(keep_scope),
        receipt_id=uuid4(),
        verification_receipt_id=uuid4(),
        issued_at=keep_time - timedelta(seconds=10),
    )
    keep = await service.preflight(
        actor_user_id=keep_grants[0].actor_user_id,
        operation_id=operation_id,
        assignment_id=keep_grants[0].id,
        authorization_id=keep_grants[1].id,
        readback=readback,
        minimum_validity_seconds=300,
        now=keep_time,
    )
    assert keep == {"action": "keep", "current_active_pair": active_before}
    assert repository.pointer is pointer_before_preflight
    assert len(repository.receipts) == receipts_before_preflight

    stale_snapshot = dict(active_before)
    stale_snapshot["receipt_digest"] = "0" * 64
    with pytest.raises(ProducerHandoffRejected, match="changed before create"):
        await service.create(
            actor_user_id=uuid4(),
            operation_id=operation_id,
            receipt_id=uuid4(),
            verification_receipt_id=uuid4(),
            assignment_id=uuid4(),
            authorization_id=uuid4(),
            readback=readback,
            build_attestation_id=fixture["build_id"],
            build_attestation_digest=fixture["attestation_digest"],
            commit_sha=fixture["commit_sha"],
            tree_sha=fixture["tree_sha"],
            workflow_id=fixture["workflow_id"],
            expected_current=stale_snapshot,
            now=keep_time,
        )
    assert repository.pointer is pointer_before_preflight

    refresh_time = now + timedelta(days=7, seconds=-100)
    refresh_scope = preflight_scope(
        operation_id=operation_id,
        readback=readback,
        minimum_validity_seconds=300,
    )
    preflight_grants = _add_grant_pair(
        repository,
        fixture["private_key"],
        actor_id=uuid4(),
        operation_id=operation_id,
        action=PREFLIGHT_ACTION,
        scope_digest=digest_json(refresh_scope),
        receipt_id=uuid4(),
        verification_receipt_id=uuid4(),
        issued_at=refresh_time - timedelta(seconds=10),
    )
    refresh_preflight = await service.preflight(
        actor_user_id=preflight_grants[0].actor_user_id,
        operation_id=operation_id,
        assignment_id=preflight_grants[0].id,
        authorization_id=preflight_grants[1].id,
        readback=readback,
        minimum_validity_seconds=300,
        now=refresh_time,
    )
    assert refresh_preflight == {
        "action": "refresh",
        "current_active_pair": active_before,
    }
    assert repository.pointer is pointer_before_preflight
    assert len(repository.receipts) == receipts_before_preflight

    refresh_receipt_id, refresh_verification_id = uuid4(), uuid4()
    refresh_actor = uuid4()
    refresh_payload_scope = receipt_scope(
        operation_id=operation_id,
        receipt_id=refresh_receipt_id,
        verification_receipt_id=refresh_verification_id,
        readback=readback,
        build_attestation_id=fixture["build_id"],
        build_attestation_digest=fixture["attestation_digest"],
        commit_sha=fixture["commit_sha"],
        tree_sha=fixture["tree_sha"],
        workflow_id=fixture["workflow_id"],
        supersedes=active_before,
    )
    create_grants = _add_grant_pair(
        repository,
        fixture["private_key"],
        actor_id=refresh_actor,
        operation_id=operation_id,
        action=CREATE_ACTION,
        scope_digest=digest_json(refresh_payload_scope),
        receipt_id=refresh_receipt_id,
        verification_receipt_id=refresh_verification_id,
        issued_at=refresh_time - timedelta(seconds=10),
    )
    refresh_deploy = await service.create(
        actor_user_id=refresh_actor,
        operation_id=operation_id,
        receipt_id=refresh_receipt_id,
        verification_receipt_id=refresh_verification_id,
        assignment_id=create_grants[0].id,
        authorization_id=create_grants[1].id,
        readback=readback,
        build_attestation_id=fixture["build_id"],
        build_attestation_digest=fixture["attestation_digest"],
        commit_sha=fixture["commit_sha"],
        tree_sha=fixture["tree_sha"],
        workflow_id=fixture["workflow_id"],
        expected_current=active_before,
        now=refresh_time,
    )
    assert refresh_deploy.payload["supersedes"] == active_before
    assert refresh_deploy.id != deploy.id
    assert refresh_deploy.task_set_id == deploy.task_set_id
    assert repository.pointer is pointer_before_preflight

    refresh_verifier = uuid4()
    refresh_verify_scope = _verification_scope(
        operation_id=operation_id,
        deploy=refresh_deploy,
        verification_receipt_id=refresh_verification_id,
        readback=readback,
        supersedes=active_before,
    )
    refresh_verify_grants = _add_grant_pair(
        repository,
        fixture["private_key"],
        actor_id=refresh_verifier,
        operation_id=operation_id,
        action=VERIFY_ACTION,
        scope_digest=digest_json(refresh_verify_scope),
        receipt_id=refresh_receipt_id,
        verification_receipt_id=refresh_verification_id,
        issued_at=refresh_time,
    )
    with pytest.raises(
        ProducerHandoffRejected, match="changed before compare-and-swap"
    ):
        await service.verify(
            actor_user_id=refresh_verifier,
            operation_id=operation_id,
            receipt_id=refresh_receipt_id,
            verification_receipt_id=refresh_verification_id,
            assignment_id=refresh_verify_grants[0].id,
            authorization_id=refresh_verify_grants[1].id,
            readback=readback,
            expected_current=stale_snapshot,
            now=refresh_time + timedelta(seconds=1),
        )
    assert repository.pointer is pointer_before_preflight
    assert all(grant.consumed_at is None for grant in refresh_verify_grants)

    refresh_verified = await service.verify(
        actor_user_id=refresh_verifier,
        operation_id=operation_id,
        receipt_id=refresh_receipt_id,
        verification_receipt_id=refresh_verification_id,
        assignment_id=refresh_verify_grants[0].id,
        authorization_id=refresh_verify_grants[1].id,
        readback=readback,
        expected_current=active_before,
        now=refresh_time + timedelta(seconds=1),
    )
    assert repository.pointer.receipt_id == refresh_receipt_id
    assert repository.pointer.verification_receipt_id == refresh_verified.id
    assert repository.pointer.revision == 2
    assert str(repository.pointer.receipt_id) != active_before["receipt_id"]
    assert refresh_verified.payload["supersedes"] == active_before


@pytest.mark.parametrize(
    ("change", "value"),
    [
        (
            "cluster_arn",
            "arn:aws:ecs:us-east-1:999999999999:cluster/neurostack-staging",
        ),
        (
            "service_arn",
            "arn:aws:ecs:us-east-1:388460142431:service/neurostack-staging/other",
        ),
        (
            "task_definition_arn",
            "arn:aws:ecs:us-east-1:999999999999:task-definition/neurostack-staging-app:8",
        ),
        (
            "task_definition_arn",
            "arn:aws:ecs:us-east-1:388460142431:task-definition/other:8",
        ),
        (
            "task_arns",
            ("arn:aws:ecs:us-east-1:999999999999:task/neurostack-staging/" + "1" * 32,),
        ),
        ("config_generation", "latest"),
        ("compatibility_generation", ""),
        ("app_image_digest", "sha256:" + "c" * 64),
        ("ecr_image_digest", "sha256:" + "c" * 64),
        ("bundle_digest", "not-a-digest"),
        ("readiness", "not-ready"),
        ("rollout_control", "enabled"),
        ("deployment_controller", "CODE_DEPLOY"),
        ("topup_commercial_enabled", True),
        (
            "secret_references",
            (
                (
                    "ASAAS_API_KEY",
                    "arn:aws:secretsmanager:us-east-1:999999999999:secret:key",
                    "AWSCURRENT",
                ),
            ),
        ),
    ],
)
def test_producer_readback_rejects_target_digest_and_generation_mismatch(
    change: str, value: Any
) -> None:
    with pytest.raises(ProducerHandoffRejected):
        replace(_producer_readback(), **{change: value}).as_payload()


def test_producer_receipt_expiry_is_exactly_seven_days() -> None:
    now = datetime.now(timezone.utc)
    assert receipt_expiry(now) == now + timedelta(days=7)


@pytest.mark.parametrize("case", ["missing", "extra", "remapped"])
def test_producer_readback_requires_exact_secret_reference_mapping(case: str) -> None:
    references = list(_producer_readback().secret_references)
    if case == "missing":
        references.pop()
    elif case == "extra":
        references.append(
            (
                "EXTRA_SECRET",
                "arn:aws:secretsmanager:us-east-1:388460142431:secret:"
                "neurostack/staging/secret-key-AbCdEf",
                "AWSCURRENT",
            )
        )
    else:
        name = references[0][0]
        references[0] = (
            name,
            "arn:aws:secretsmanager:us-east-1:388460142431:secret:"
            "neurostack/staging/secret-key-AbCdEf",
            "AWSCURRENT",
        )

    with pytest.raises(ProducerHandoffRejected, match="secret reference"):
        replace(_producer_readback(), secret_references=tuple(references)).as_payload()


def test_producer_receipt_signature_rejects_payload_mutation() -> None:
    private_key, public_key = _producer_key_pair()
    payload = {"receipt_type": "billing_producer_deployed.v1", "readback": {"a": 1}}
    digest, signature = sign_receipt(payload, _private_key_pem(private_key))
    verify_receipt_signature(payload, digest, signature, public_key)
    with pytest.raises(ProducerHandoffRejected, match="payload digest is invalid"):
        verify_receipt_signature(
            {**payload, "readback": {"a": 2}}, digest, signature, public_key
        )


def test_billing_producer_append_only_migration_installs_immutable_triggers() -> None:
    migration = (
        Path(__file__).parents[2]
        / "alembic"
        / "versions"
        / "20261015_000001_033_asaas_producer_handoff.py"
    ).read_text(encoding="utf-8")
    assert "billing producer receipts are append-only" in migration
    assert "trg_billing_producer_receipts_immutable" in migration
    assert "BEFORE UPDATE OR DELETE ON billing_producer_receipts" in migration


def test_producer_readback_rejects_no_latest_runtime_identity() -> None:
    with pytest.raises(ProducerHandoffRejected, match="mutable"):
        replace(_producer_readback(), config_generation="latest").as_payload()


def test_staging_production_serving_target_is_fixed() -> None:
    assert ENVIRONMENT == "staging"
    assert OPERATIONAL_ROLE == "production-serving"
    assert CLUSTER_ARN.endswith(":cluster/neurostack-staging")
    assert SERVICE_ARN.endswith("/neurostack-staging/neurostack-staging-app")


@pytest.mark.asyncio
async def test_producer_preflight_rejects_duplicate_active_pair_rows() -> None:
    class _ScalarResult:
        def scalars(self) -> "_ScalarResult":
            return self

        def all(self) -> list[object]:
            return [SimpleNamespace(), SimpleNamespace()]

    class _DuplicateSession:
        async def execute(self, _query: object) -> _ScalarResult:
            return _ScalarResult()

    repository = BillingProducerReceiptRepository(_DuplicateSession())  # type: ignore[arg-type]
    with pytest.raises(ProducerHandoffRejected, match="pointer is duplicated"):
        await repository.get_active_pair()


def test_producer_task_cohort_identity_is_server_derived_and_order_independent() -> (
    None
):
    readback = _producer_readback()
    reversed_readback = replace(readback, task_arns=tuple(reversed(readback.task_arns)))
    assert compute_task_set_id(readback) == compute_task_set_id(reversed_readback)
    assert compute_task_set_id(readback).startswith("ecs-task-cohort-sha256:")


def test_producer_readback_request_rejects_caller_supplied_task_set_id() -> None:
    with pytest.raises(ValidationError) as error:
        ProducerReadbackRequest.model_validate(
            {
                "cluster_arn": CLUSTER_ARN,
                "service_arn": SERVICE_ARN,
                "deployment_id": "ecs-svc/active-deployment",
                "deployment_controller": "ECS",
                "task_set_id": "caller-value",
                "task_definition_arn": "arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:8",
                "task_arns": [
                    "arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/"
                    + "1" * 32
                ],
                "config_generation": "cfg-8",
                "compatibility_generation": "compat-7",
                "app_image_digest": "sha256:" + "a" * 64,
                "ecr_image_digest": "sha256:" + "a" * 64,
                "bundle_digest": "b" * 64,
                "readiness": "ready",
                "rollout_state": "COMPLETED",
                "rollout_control": "disabled",
                "topup_commercial_enabled": False,
            }
        )
    assert any(item["type"] == "extra_forbidden" for item in error.value.errors())


def test_producer_create_and_verify_require_explicit_active_pair_cas() -> None:
    pair_values = {
        "expected_current_receipt_id": uuid4(),
        "expected_current_receipt_digest": "a" * 64,
        "expected_current_verification_receipt_id": uuid4(),
        "expected_current_verification_receipt_digest": "b" * 64,
    }
    with pytest.raises(HTTPException):
        _expected_current(ProducerCreateRequest.model_construct())
    empty_create = ProducerCreateRequest.model_construct(
        expected_no_current_active_pair=True
    )
    assert _expected_current(empty_create) is None
    current_create = ProducerCreateRequest.model_construct(**pair_values)
    assert _expected_current(current_create) is not None
    current_verify = ProducerVerifyRequest.model_construct(**pair_values)
    assert _expected_current(current_verify) == {
        "receipt_id": str(pair_values["expected_current_receipt_id"]),
        "receipt_digest": "a" * 64,
        "verification_receipt_id": str(
            pair_values["expected_current_verification_receipt_id"]
        ),
        "verification_receipt_digest": "b" * 64,
    }


def test_producer_actor_is_derived_from_short_staging_jwt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(settings, "environment", "staging")
    actor_id = uuid4()
    request = SimpleNamespace(state=SimpleNamespace(staging_rehearsal_token=True))
    tenant = TenantContext(
        tenant_id=uuid4(),
        user_id=actor_id,
        authentication_mode="jwt",
    )
    assert _require_short_staging_jwt(request, tenant) == actor_id  # type: ignore[arg-type]


def test_producer_create_request_rejects_caller_supplied_actor() -> None:
    with pytest.raises(ValidationError) as error:
        ProducerCreateRequest.model_validate({"actor_user_id": str(uuid4())})
    assert any(
        item["type"] == "extra_forbidden" and item["loc"] == ("actor_user_id",)
        for item in error.value.errors()
    )


def test_producer_actor_boundary_rejects_api_key_authentication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(settings, "environment", "staging")
    request = SimpleNamespace(state=SimpleNamespace(staging_rehearsal_token=True))
    tenant = TenantContext(
        tenant_id=uuid4(),
        user_id=uuid4(),
        authentication_mode="api_key",
    )
    with pytest.raises(HTTPException) as error:
        _require_short_staging_jwt(request, tenant)  # type: ignore[arg-type]
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_build_attestation_registration_requires_signed_immutable_image_reference() -> (
    None
):
    private_key, public_key = _producer_key_pair()
    image_digest = "sha256:" + "a" * 64
    payload = {
        "attestation_type": "billing_build_attestation.v1",
        "app_image_digest": image_digest,
        "bundle_digest": "b" * 64,
        "commit_sha": "c" * 64,
        "tree_sha": "d" * 64,
        "workflow_id": "build-123",
        "immutable_artifact_reference": (
            "388460142431.dkr.ecr.us-east-1.amazonaws.com/neurostack-staging@"
            + image_digest
        ),
    }
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")
    payload_digest, signature = sign_receipt(payload, private_pem)
    attestation = BillingProducerBuildAttestation(
        id=uuid4(),
        payload=payload,
        payload_digest=payload_digest,
        signature=signature,
    )
    session = _RecordingSession()

    class _AttestationRepository(BillingProducerReceiptRepository):
        def __init__(self) -> None:
            self.session = session  # type: ignore[assignment]

        async def get_build_attestation(
            self, *, attestation_id: object, attestation_digest: str
        ) -> None:
            return None

    repository = _AttestationRepository()
    await repository.register_build_attestation(
        attestation,
        build_attestation_public_key=public_key,
    )
    assert attestation in session.added


class _RecordingSession(_FlushOnlySession):
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)
