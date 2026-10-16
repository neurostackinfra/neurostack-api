"""Authenticated producer receipt operations for the production-serving target."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import TenantContext, require_super_admin
from src.config.settings import settings
from src.models.billing_producer_signed import (
    BillingProducerV1ActivePair,
    BillingProducerV1BuildAttestation,
    BillingProducerV1Grant,
    BillingProducerV1GrantConsumption,
    BillingProducerV1GrantRevocation,
    BillingProducerV1Receipt,
)
from src.models.user import User, UserRole
from src.repositories import get_db_session_dependency
from src.repositories.billing_producer import (
    BillingProducerHandoffService,
    BillingProducerReceiptRepository,
)
from src.services.billing_producer_grants import (
    ProducerGrantRejected,
    grants_have_minimum_validity,
    preflight_grant_scope,
    producer_pair_preflight_action,
    validate_preflight_grant_pair,
)
from src.services.billing_producer_handoff import (
    CLUSTER_ARN,
    CREATE_ACTION,
    DEPLOYED_TYPE,
    ENVIRONMENT,
    OPERATIONAL_ROLE,
    SERVICE_ARN,
    VERIFIED_TYPE,
    VERIFY_ACTION,
    ProducerHandoffRejected,
    ProducerReadback,
    validate_readback,
)
from src.services.billing_signed_envelope import (
    ALGORITHM,
    PURPOSE_BY_TYPE,
    SCHEMA_VERSION,
    Boto3KmsDigestSigner,
    SignedEnvelopeRejected,
    TrustEntry,
    TrustRegistry,
    build_envelope,
    envelope_digest,
    jcs,
    payload_digest,
    sha256_hex,
    timestamp_utc,
    validate_attestation_trust,
    verify_envelope,
)

router = APIRouter()


class SecretReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)
    arn: str = Field(min_length=1, max_length=512)
    version_id: str = Field(min_length=1, max_length=200)


class ProducerReadbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cluster_arn: str
    service_arn: str
    deployment_id: str
    deployment_controller: str
    task_definition_arn: str
    task_arns: list[str] = Field(min_length=1, max_length=1000)
    config_generation: str
    compatibility_generation: str
    app_image_digest: str
    ecr_image_digest: str
    bundle_digest: str
    readiness: str
    rollout_state: str
    rollout_control: str
    topup_commercial_enabled: bool
    secret_references: list[SecretReference] = Field(
        default_factory=list, max_length=64
    )

    def as_domain(self) -> ProducerReadback:
        return ProducerReadback(
            cluster_arn=self.cluster_arn,
            service_arn=self.service_arn,
            deployment_id=self.deployment_id,
            deployment_controller=self.deployment_controller,
            task_definition_arn=self.task_definition_arn,
            task_arns=tuple(self.task_arns),
            config_generation=self.config_generation,
            compatibility_generation=self.compatibility_generation,
            app_image_digest=self.app_image_digest,
            ecr_image_digest=self.ecr_image_digest,
            bundle_digest=self.bundle_digest,
            readiness=self.readiness,
            rollout_state=self.rollout_state,
            rollout_control=self.rollout_control,
            topup_commercial_enabled=self.topup_commercial_enabled,
            secret_references=tuple(
                (reference.name, reference.arn, reference.version_id)
                for reference in self.secret_references
            ),
        )


class ProducerPreflightRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    assignment_id: UUID
    authorization_receipt_id: UUID
    readback: ProducerReadbackRequest
    minimum_grant_validity_seconds: int = Field(default=60, ge=60, le=300, strict=True)
    minimum_pair_validity_seconds: int = Field(default=300, ge=300, le=300, strict=True)


class ProducerCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    receipt_id: UUID
    verification_receipt_id: UUID
    assignment_id: UUID
    authorization_receipt_id: UUID
    readback: ProducerReadbackRequest
    build_attestation_id: UUID
    build_attestation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    commit_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    tree_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    workflow_id: str = Field(min_length=1, max_length=255)
    expected_no_current_active_pair: bool = False
    expected_current_receipt_id: UUID | None = None
    expected_current_receipt_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    expected_current_verification_receipt_id: UUID | None = None
    expected_current_verification_receipt_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )


class ProducerVerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation_id: UUID
    receipt_id: UUID
    verification_receipt_id: UUID
    assignment_id: UUID
    authorization_receipt_id: UUID
    readback: ProducerReadbackRequest
    expected_current_receipt_id: UUID | None = None
    expected_current_receipt_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    expected_current_verification_receipt_id: UUID | None = None
    expected_current_verification_receipt_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    expected_no_current_active_pair: bool = False


def _service(db: AsyncSession) -> BillingProducerHandoffService:
    return BillingProducerHandoffService(
        BillingProducerReceiptRepository(db),
        authorization_public_key=settings.billing_deploy_authorization_public_key,
        receipt_signing_key=settings.billing_receipt_signing_key.get_secret_value(),
        receipt_public_key=settings.billing_receipt_verification_public_key,
        build_attestation_public_key=settings.billing_build_attestation_public_key,
    )


def _require_short_staging_jwt(request: Request, tenant: TenantContext) -> UUID:
    if (
        settings.environment != "staging"
        or not getattr(request.state, "staging_rehearsal_token", False)
        or tenant.authentication_mode != "jwt"
        or tenant.user_id is None
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"error": "staging_producer_jwt_required"},
        )
    return tenant.user_id


def _receipt_response(receipt: Any) -> dict[str, Any]:
    return {
        "receipt_id": str(receipt.id),
        "receipt_type": receipt.receipt_type,
        "operation_id": str(receipt.operation_id),
        "payload": receipt.payload,
        "payload_digest": receipt.payload_digest,
        "signature": receipt.signature,
    }


def _expected_current(
    body: ProducerCreateRequest | ProducerVerifyRequest,
) -> dict[str, str] | None:
    values = (
        body.expected_current_receipt_id,
        body.expected_current_receipt_digest,
        body.expected_current_verification_receipt_id,
        body.expected_current_verification_receipt_digest,
    )
    if body.expected_no_current_active_pair and all(value is None for value in values):
        return None
    if body.expected_no_current_active_pair or any(value is None for value in values):
        raise HTTPException(
            status_code=422, detail={"error": "invalid_active_pair_cas"}
        )
    return {
        "receipt_id": str(body.expected_current_receipt_id),
        "receipt_digest": str(body.expected_current_receipt_digest),
        "verification_receipt_id": str(body.expected_current_verification_receipt_id),
        "verification_receipt_digest": str(
            body.expected_current_verification_receipt_digest
        ),
    }


def _v1_trust_entry(type_name: str) -> TrustEntry:
    if type_name == "billing_build_attestation.v1":
        values = (
            settings.billing_producer_build_issuer,
            settings.billing_producer_build_kid,
            settings.billing_producer_build_audience,
            settings.billing_producer_build_public_key,
        )
        extra = {
            "repository_id": settings.billing_producer_build_repository_id,
            "repository_name": settings.billing_producer_build_repository_name,
            "workflow_path": settings.billing_producer_build_workflow_path,
            "workflow_ref": settings.billing_producer_build_workflow_ref,
            "oidc_issuer": settings.billing_producer_build_oidc_issuer,
            "oidc_subject": settings.billing_producer_build_oidc_subject,
        }
    elif type_name in {
        "billing_producer_assignment.v1",
        "billing_producer_authorization.v1",
        "billing_producer_grant_revocation.v1",
    }:
        values = (
            settings.billing_producer_grant_issuer,
            settings.billing_producer_grant_kid,
            settings.billing_producer_grant_audience,
            settings.billing_producer_grant_public_key,
        )
        extra = {}
    elif type_name in {DEPLOYED_TYPE, VERIFIED_TYPE}:
        values = (
            settings.billing_producer_receipt_issuer,
            settings.billing_producer_receipt_kid,
            settings.billing_producer_receipt_audience,
            settings.billing_producer_receipt_public_key,
        )
        extra = {}
    else:
        raise ProducerHandoffRejected("producer artifact type is not allowlisted")
    if any(not value for value in (*values, *extra.values())):
        raise ProducerHandoffRejected("canonical producer trust pins are unavailable")
    return TrustEntry(
        issuer=values[0],
        kid=values[1],
        environment=ENVIRONMENT,
        audience=values[2],
        type=type_name,
        purpose=PURPOSE_BY_TYPE[type_name],
        schema_version=SCHEMA_VERSION,
        algorithm=ALGORITHM,
        public_key_pem=values[3],
        **extra,
    )


def _v1_grant_row_is_valid(grant: BillingProducerV1Grant) -> bool:
    type_name = (
        "billing_producer_assignment.v1"
        if grant.grant_kind == "assignment"
        else "billing_producer_authorization.v1"
    )
    try:
        entry = _v1_trust_entry(type_name)
        verify_envelope(grant.envelope, TrustRegistry([entry]), expected_type=type_name)
        payload = grant.envelope["payload"]
        return (
            envelope_digest(grant.envelope) == grant.envelope_digest
            and payload_digest(payload) == grant.payload_digest
            and payload.get("id") == str(grant.id)
            and payload.get("grant_kind") == grant.grant_kind
            and payload.get("issuer_user_id") == str(grant.issuer_user_id)
            and payload.get("executor_user_id") == str(grant.executor_user_id)
            and payload.get("operation_id") == str(grant.operation_id)
            and payload.get("action") == grant.action
            and payload.get("scope") == grant.scope
            and payload.get("scope_digest") == grant.scope_digest
            and payload.get("assignment_id")
            == (str(grant.assignment_id) if grant.assignment_id else None)
            and payload.get("assignment_digest") == grant.assignment_digest
            and payload.get("approver_user_id")
            == (str(grant.approver_user_id) if grant.approver_user_id else None)
            and grant.envelope.get("operation_id") == str(grant.operation_id)
            and grant.envelope.get("issued_at")
            == grant.issued_at.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
            and grant.envelope.get("expires_at")
            == grant.expires_at.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    except (ProducerHandoffRejected, SignedEnvelopeRejected, KeyError, TypeError):
        return False


def _attestation_is_unexpired(envelope: dict[str, Any], *, now: datetime) -> bool:
    try:
        expires_at = datetime.fromisoformat(
            envelope["expires_at"].replace("Z", "+00:00")
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        return False
    return expires_at.tzinfo is not None and expires_at > now


def _v1_receipt_row_is_valid(
    receipt: BillingProducerV1Receipt,
    *,
    expected_type: str,
    trust: TrustEntry,
) -> bool:
    try:
        verify_envelope(
            receipt.envelope,
            TrustRegistry([trust]),
            expected_type=expected_type,
        )
        payload = receipt.envelope["payload"]
        expected_actor_field = (
            "executor_user_id"
            if expected_type == "billing_producer_deployed.v1"
            else "verifier_user_id"
        )
        expected_actor = (
            receipt.executor_user_id
            if expected_actor_field == "executor_user_id"
            else receipt.verifier_user_id
        )
        duplicated = (
            payload.get("readback", {})
            if expected_type == "billing_producer_deployed.v1"
            else payload
        )
        return (
            expected_actor is not None
            and envelope_digest(receipt.envelope) == receipt.envelope_digest
            and payload_digest(payload) == receipt.payload_digest
            and payload.get("operation_id") == str(receipt.operation_id)
            and payload.get("receipt_type") == receipt.receipt_type == expected_type
            and payload.get("environment") == receipt.environment == ENVIRONMENT
            and payload.get("operational_role")
            == receipt.operational_role
            == OPERATIONAL_ROLE
            and payload.get(expected_actor_field) == str(expected_actor)
            and duplicated.get("deployment_id") == receipt.deployment_id
            and duplicated.get("task_set_id") == receipt.task_set_id
            and duplicated.get("config_generation") == receipt.config_generation
            and duplicated.get("compatibility_generation")
            == receipt.compatibility_generation
            and duplicated.get("app_image_digest", duplicated.get("app_digest"))
            == receipt.app_digest
            and duplicated.get("bundle_digest") == receipt.bundle_digest
            and payload.get("readback_digest") == receipt.readback_digest
            and payload.get("build_attestation_id") == str(receipt.build_attestation_id)
            and payload.get("build_attestation_digest")
            == receipt.build_attestation_digest
            and receipt.envelope.get("operation_id") == str(receipt.operation_id)
            and receipt.envelope.get("issued_at")
            == receipt.issued_at.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
            and receipt.envelope.get("expires_at")
            == receipt.expires_at.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
    except (ProducerHandoffRejected, SignedEnvelopeRejected, KeyError, TypeError):
        return False


def _receipt_signer() -> Boto3KmsDigestSigner:
    try:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "kms",
            region_name="us-east-1",
            config=Config(
                connect_timeout=2,
                read_timeout=5,
                retries={"mode": "standard", "total_max_attempts": 2},
            ),
        )
    except ImportError:
        raise ProducerHandoffRejected("AWS SDK for Python is unavailable") from None
    return Boto3KmsDigestSigner(client)


def _active_pair_snapshot(
    pair: BillingProducerV1ActivePair | None,
) -> dict[str, str] | None:
    if pair is None:
        return None
    return {
        "receipt_id": str(pair.receipt_id),
        "receipt_digest": pair.receipt_digest,
        "verification_receipt_id": str(pair.verification_receipt_id),
        "verification_receipt_digest": pair.verification_receipt_digest,
    }


async def _locked_active_pair(
    db: AsyncSession,
    *,
    operation_id: UUID,
    expected: dict[str, str] | None,
) -> BillingProducerV1ActivePair | None:
    pair = await db.scalar(
        select(BillingProducerV1ActivePair)
        .where(BillingProducerV1ActivePair.environment == ENVIRONMENT)
        .with_for_update()
    )
    if pair is not None and pair.operation_id != operation_id:
        raise ProducerHandoffRejected("another operation owns the active pair")
    if _active_pair_snapshot(pair) != expected:
        raise ProducerHandoffRejected("active pair changed before compare-and-swap")
    return pair


async def _consume_v1_grant_pair(
    db: AsyncSession,
    *,
    assignment_id: UUID,
    authorization_id: UUID,
    actor_user_id: UUID,
    operation_id: UUID,
    action: str,
    receipt_id: UUID,
    verification_receipt_id: UUID,
    scope: dict[str, Any],
    now: datetime,
) -> tuple[BillingProducerV1Grant, BillingProducerV1Grant]:
    rows = (
        (
            await db.execute(
                select(BillingProducerV1Grant)
                .where(BillingProducerV1Grant.id.in_((assignment_id, authorization_id)))
                .order_by(BillingProducerV1Grant.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    grants = {grant.id: grant for grant in rows}
    assignment = grants.get(assignment_id)
    authorization = grants.get(authorization_id)
    if assignment is None or authorization is None or assignment is authorization:
        raise ProducerHandoffRejected("active signed grant pair is unavailable")
    if not _v1_grant_row_is_valid(assignment) or not _v1_grant_row_is_valid(
        authorization
    ):
        raise ProducerHandoffRejected("signed grant pair failed readback validation")
    if (
        assignment.grant_kind != "assignment"
        or authorization.grant_kind != "authorization"
        or assignment.assignment_id is not None
        or assignment.assignment_digest is not None
        or authorization.assignment_id != assignment.id
        or authorization.assignment_digest != assignment.envelope_digest
        or assignment.action != action
        or authorization.action != action
        or assignment.operation_id != operation_id
        or authorization.operation_id != operation_id
        or assignment.executor_user_id != actor_user_id
        or authorization.executor_user_id != actor_user_id
        or assignment.receipt_id != receipt_id
        or assignment.verification_receipt_id != verification_receipt_id
        or authorization.receipt_id != receipt_id
        or authorization.verification_receipt_id != verification_receipt_id
        or assignment.scope != scope
        or authorization.scope != scope
        or assignment.scope_digest != sha256_hex(jcs(scope))
        or authorization.scope_digest != assignment.scope_digest
        or assignment.issuer_user_id != authorization.issuer_user_id
        or assignment.executor_user_id != authorization.executor_user_id
        or authorization.approver_user_id
        in {assignment.issuer_user_id, assignment.executor_user_id}
        or assignment.issued_at > now
        or authorization.issued_at > now
        or assignment.expires_at <= now
        or authorization.expires_at <= now
        or authorization.expires_at > assignment.expires_at
    ):
        raise ProducerHandoffRejected("signed grant pair is inactive or out of scope")
    actor_ids = {
        assignment.issuer_user_id,
        assignment.executor_user_id,
        authorization.approver_user_id,
    }
    if None in actor_ids or len(actor_ids) != 3:
        raise ProducerHandoffRejected("grant actors must be distinct")
    users = (
        (await db.execute(select(User).where(User.id.in_(actor_ids)).with_for_update()))
        .scalars()
        .all()
    )
    if len(users) != 3 or any(
        not user.is_active or user.role != UserRole.super_admin for user in users
    ):
        raise ProducerHandoffRejected(
            "all grant actors must remain active super_admins"
        )
    prior = (
        (
            await db.execute(
                select(BillingProducerV1GrantConsumption.grant_id).where(
                    BillingProducerV1GrantConsumption.grant_id.in_(
                        (assignment.id, authorization.id)
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    revoked = (
        (
            await db.execute(
                select(BillingProducerV1GrantRevocation.grant_id).where(
                    BillingProducerV1GrantRevocation.grant_id.in_(
                        (assignment.id, authorization.id)
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    if prior or revoked:
        raise ProducerHandoffRejected("grant pair was already consumed or revoked")
    return assignment, authorization


async def _load_verified_attestation(
    db: AsyncSession,
    *,
    operation_id: UUID,
    attestation_id: UUID,
    attestation_digest: str,
    now: datetime,
) -> tuple[BillingProducerV1BuildAttestation, dict[str, Any]]:
    attestation = await db.scalar(
        select(BillingProducerV1BuildAttestation).where(
            BillingProducerV1BuildAttestation.id == attestation_id,
            BillingProducerV1BuildAttestation.operation_id == operation_id,
            BillingProducerV1BuildAttestation.envelope_digest == attestation_digest,
        )
    )
    if attestation is None:
        raise ProducerHandoffRejected("signed build attestation is unavailable")
    try:
        trust = _v1_trust_entry("billing_build_attestation.v1")
        verified = verify_envelope(
            attestation.envelope,
            TrustRegistry([trust]),
            expected_type="billing_build_attestation.v1",
        )
        validate_attestation_trust(verified, attestation.envelope)
        payload = attestation.envelope["payload"]
        expires_at = datetime.fromisoformat(
            attestation.envelope["expires_at"].replace("Z", "+00:00")
        )
        if (
            envelope_digest(attestation.envelope) != attestation.envelope_digest
            or payload_digest(payload) != attestation.payload_digest
            or payload.get("attestation_id") != str(attestation.id)
            or expires_at <= now
        ):
            raise SignedEnvelopeRejected("attestation row differs from signed envelope")
    except (SignedEnvelopeRejected, KeyError, TypeError, ValueError):
        raise ProducerHandoffRejected(
            "build attestation signature is invalid"
        ) from None
    return attestation, payload


def _v1_artifact_response(identifier: UUID, envelope: dict[str, Any]) -> dict[str, Any]:
    return {
        "receipt_id": str(identifier),
        "receipt_digest": envelope_digest(envelope),
        "envelope": envelope,
    }


def _create_scope(
    *,
    operation_id: UUID,
    receipt_id: UUID,
    verification_receipt_id: UUID,
    supersedes: dict[str, str] | None,
) -> dict[str, Any]:
    return {
        "operation_id": str(operation_id),
        "action": CREATE_ACTION,
        "environment": ENVIRONMENT,
        "operational_role": OPERATIONAL_ROLE,
        "cluster_arn": CLUSTER_ARN,
        "service_arn": SERVICE_ARN,
        "receipt_id": str(receipt_id),
        "verification_receipt_id": str(verification_receipt_id),
        "supersedes": supersedes,
    }


async def _append_consumption_events(
    db: AsyncSession,
    *,
    assignment: BillingProducerV1Grant,
    authorization: BillingProducerV1Grant,
    actor_user_id: UUID,
    operation_id: UUID,
    result_digest: str,
    now: datetime,
) -> None:
    request_id = uuid4()
    for grant in (assignment, authorization):
        db.add(
            BillingProducerV1GrantConsumption(
                id=uuid4(),
                grant_id=grant.id,
                grant_digest=grant.envelope_digest,
                operation_id=operation_id,
                request_id=request_id,
                executor_user_id=actor_user_id,
                result_digest=result_digest,
                consumed_at=now,
            )
        )
    await db.flush()


async def _create_v1_deploy_receipt(
    db: AsyncSession,
    *,
    body: ProducerCreateRequest,
    actor_user_id: UUID,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    readback = body.readback.as_domain()
    try:
        validate_readback(readback)
    except ProducerHandoffRejected as exc:
        raise ProducerHandoffRejected(str(exc)) from None
    readback_payload = readback.as_payload()
    readback_digest = sha256_hex(jcs(readback_payload))
    expected_current = _expected_current(body)
    current = await _locked_active_pair(
        db, operation_id=body.operation_id, expected=expected_current
    )
    supersedes = _active_pair_snapshot(current)
    scope = _create_scope(
        operation_id=body.operation_id,
        receipt_id=body.receipt_id,
        verification_receipt_id=body.verification_receipt_id,
        supersedes=supersedes,
    )
    assignment, authorization = await _consume_v1_grant_pair(
        db,
        assignment_id=body.assignment_id,
        authorization_id=body.authorization_receipt_id,
        actor_user_id=actor_user_id,
        operation_id=body.operation_id,
        action=CREATE_ACTION,
        receipt_id=body.receipt_id,
        verification_receipt_id=body.verification_receipt_id,
        scope=scope,
        now=now,
    )
    attestation, attestation_payload = await _load_verified_attestation(
        db,
        operation_id=body.operation_id,
        attestation_id=body.build_attestation_id,
        attestation_digest=body.build_attestation_digest,
        now=now,
    )
    if (
        attestation_payload.get("app_image_digest")
        != readback_payload["app_image_digest"]
        or attestation_payload.get("app_image_digest")
        != readback_payload["ecr_image_digest"]
        or attestation_payload.get("bundle_digest") != readback_payload["bundle_digest"]
        or attestation_payload.get("commit_sha") != body.commit_sha
        or attestation_payload.get("tree_sha") != body.tree_sha
        or attestation_payload.get("workflow_id") != body.workflow_id
    ):
        raise ProducerHandoffRejected("readback differs from signed build provenance")
    existing = await db.scalar(
        select(BillingProducerV1Receipt).where(
            BillingProducerV1Receipt.id.in_(
                (body.receipt_id, body.verification_receipt_id)
            )
        )
    )
    if existing is not None:
        raise ProducerHandoffRejected("producer receipt ID already exists")
    payload = {
        "operation_id": str(body.operation_id),
        "receipt_id": str(body.receipt_id),
        "verification_receipt_id": str(body.verification_receipt_id),
        "receipt_type": DEPLOYED_TYPE,
        "executor_user_id": str(actor_user_id),
        "environment": ENVIRONMENT,
        "operational_role": OPERATIONAL_ROLE,
        "readback": readback_payload,
        "readback_digest": readback_digest,
        "build_attestation_id": str(attestation.id),
        "build_attestation_digest": attestation.envelope_digest,
        "commit_sha": attestation_payload["commit_sha"],
        "tree_sha": attestation_payload["tree_sha"],
        "workflow_id": attestation_payload["workflow_id"],
        "supersedes": supersedes,
    }
    expires_at = now + timedelta(days=7)
    trust = _v1_trust_entry(DEPLOYED_TYPE)
    envelope = build_envelope(
        entry=trust,
        payload=payload,
        operation_id=body.operation_id,
        issued_at=now,
        expires_at=expires_at,
        signer=_receipt_signer(),
    )
    verify_envelope(envelope, TrustRegistry([trust]), expected_type=DEPLOYED_TYPE)
    digest = envelope_digest(envelope)
    receipt = BillingProducerV1Receipt(
        id=body.receipt_id,
        operation_id=body.operation_id,
        receipt_type=DEPLOYED_TYPE,
        environment=ENVIRONMENT,
        operational_role=OPERATIONAL_ROLE,
        deployment_id=readback_payload["deployment_id"],
        task_set_id=readback_payload["task_set_id"],
        config_generation=readback_payload["config_generation"],
        compatibility_generation=readback_payload["compatibility_generation"],
        app_digest=readback_payload["app_image_digest"],
        bundle_digest=readback_payload["bundle_digest"],
        readback_digest=readback_digest,
        build_attestation_id=attestation.id,
        build_attestation_digest=attestation.envelope_digest,
        executor_user_id=actor_user_id,
        verifier_user_id=None,
        envelope=envelope,
        envelope_digest=digest,
        payload_digest=payload_digest(payload),
        issued_at=now,
        expires_at=expires_at,
    )
    db.add(receipt)
    await db.flush([receipt])
    await _append_consumption_events(
        db,
        assignment=assignment,
        authorization=authorization,
        actor_user_id=actor_user_id,
        operation_id=body.operation_id,
        result_digest=digest,
        now=now,
    )
    return _v1_artifact_response(receipt.id, envelope)


async def _verify_v1_deploy_receipt(
    db: AsyncSession,
    *,
    body: ProducerVerifyRequest,
    actor_user_id: UUID,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    readback = body.readback.as_domain()
    try:
        validate_readback(readback)
    except ProducerHandoffRejected as exc:
        raise ProducerHandoffRejected(str(exc)) from None
    readback_payload = readback.as_payload()
    readback_digest = sha256_hex(jcs(readback_payload))
    expected_current = _expected_current(body)
    current = await _locked_active_pair(
        db, operation_id=body.operation_id, expected=expected_current
    )
    supersedes = _active_pair_snapshot(current)
    deploy = await db.scalar(
        select(BillingProducerV1Receipt)
        .where(
            BillingProducerV1Receipt.id == body.receipt_id,
            BillingProducerV1Receipt.operation_id == body.operation_id,
            BillingProducerV1Receipt.receipt_type == DEPLOYED_TYPE,
        )
        .with_for_update()
    )
    if deploy is None or deploy.expires_at <= now:
        raise ProducerHandoffRejected("deploy receipt is unavailable or expired")
    deploy_trust = _v1_trust_entry(DEPLOYED_TYPE)
    if not _v1_receipt_row_is_valid(
        deploy, expected_type=DEPLOYED_TYPE, trust=deploy_trust
    ):
        raise ProducerHandoffRejected("deploy receipt signature is invalid")
    deploy_payload = deploy.envelope["payload"]
    if (
        deploy_payload.get("receipt_id") != str(body.receipt_id)
        or deploy_payload.get("verification_receipt_id")
        != str(body.verification_receipt_id)
        or deploy_payload.get("readback") != readback_payload
        or deploy_payload.get("readback_digest") != readback_digest
        or deploy.executor_user_id == actor_user_id
    ):
        raise ProducerHandoffRejected(
            "independent readback differs from deploy receipt"
        )
    attestation, attestation_payload = await _load_verified_attestation(
        db,
        operation_id=body.operation_id,
        attestation_id=deploy.build_attestation_id,
        attestation_digest=deploy.build_attestation_digest,
        now=now,
    )
    if (
        attestation_payload.get("attestation_id") != str(attestation.id)
        or attestation_payload.get("app_image_digest") != deploy.app_digest
        or attestation_payload.get("bundle_digest") != deploy.bundle_digest
        or attestation_payload.get("commit_sha") != deploy_payload.get("commit_sha")
        or attestation_payload.get("tree_sha") != deploy_payload.get("tree_sha")
        or attestation_payload.get("workflow_id") != deploy_payload.get("workflow_id")
    ):
        raise ProducerHandoffRejected("deploy receipt differs from build attestation")
    scope = {
        "operation_id": str(body.operation_id),
        "receipt_id": str(body.receipt_id),
        "receipt_digest": deploy.envelope_digest,
        "verification_receipt_id": str(body.verification_receipt_id),
        "readback_digest": readback_digest,
        "supersedes": supersedes,
    }
    assignment, authorization = await _consume_v1_grant_pair(
        db,
        assignment_id=body.assignment_id,
        authorization_id=body.authorization_receipt_id,
        actor_user_id=actor_user_id,
        operation_id=body.operation_id,
        action=VERIFY_ACTION,
        receipt_id=body.receipt_id,
        verification_receipt_id=body.verification_receipt_id,
        scope=scope,
        now=now,
    )
    if assignment.executor_user_id == deploy.executor_user_id:
        raise ProducerHandoffRejected("verifier must differ from deploy executor")
    verifier = await db.scalar(
        select(User).where(User.id == actor_user_id).with_for_update()
    )
    attestor = await db.scalar(
        select(User).where(User.id == deploy.executor_user_id).with_for_update()
    )
    if any(
        user is None or not user.is_active or user.role != UserRole.super_admin
        for user in (verifier, attestor)
    ):
        raise ProducerHandoffRejected(
            "verifier and attestor must remain active super_admins"
        )
    existing = await db.scalar(
        select(BillingProducerV1Receipt).where(
            BillingProducerV1Receipt.id == body.verification_receipt_id
        )
    )
    if existing is not None:
        raise ProducerHandoffRejected("verification receipt ID already exists")
    expires_at = now + timedelta(days=7)
    payload = {
        "operation_id": str(body.operation_id),
        "receipt_type": VERIFIED_TYPE,
        "receipt_id": str(body.verification_receipt_id),
        "source_receipt_id": str(deploy.id),
        "source_receipt_digest": deploy.envelope_digest,
        "environment": ENVIRONMENT,
        "operational_role": OPERATIONAL_ROLE,
        "cluster_arn": CLUSTER_ARN,
        "service_arn": SERVICE_ARN,
        "deployment_id": readback_payload["deployment_id"],
        "task_set_id": readback_payload["task_set_id"],
        "config_generation": readback_payload["config_generation"],
        "compatibility_generation": readback_payload["compatibility_generation"],
        "app_digest": readback_payload["app_image_digest"],
        "bundle_digest": readback_payload["bundle_digest"],
        "readback_digest": readback_digest,
        "build_attestation_id": str(attestation.id),
        "build_attestation_digest": attestation.envelope_digest,
        "commit_sha": attestation_payload["commit_sha"],
        "tree_sha": attestation_payload["tree_sha"],
        "workflow_id": attestation_payload["workflow_id"],
        "attestor_user_id": str(deploy.executor_user_id),
        "verifier_user_id": str(actor_user_id),
        "supersedes": supersedes,
        "issued_at": timestamp_utc(now),
        "expires_at": timestamp_utc(expires_at),
    }
    trust = _v1_trust_entry(VERIFIED_TYPE)
    envelope = build_envelope(
        entry=trust,
        payload=payload,
        operation_id=body.operation_id,
        issued_at=now,
        expires_at=expires_at,
        request_id=uuid4(),
        signer=_receipt_signer(),
    )
    verify_envelope(envelope, TrustRegistry([trust]), expected_type=VERIFIED_TYPE)
    digest = envelope_digest(envelope)
    receipt = BillingProducerV1Receipt(
        id=body.verification_receipt_id,
        operation_id=body.operation_id,
        receipt_type=VERIFIED_TYPE,
        environment=ENVIRONMENT,
        operational_role=OPERATIONAL_ROLE,
        deployment_id=readback_payload["deployment_id"],
        task_set_id=readback_payload["task_set_id"],
        config_generation=readback_payload["config_generation"],
        compatibility_generation=readback_payload["compatibility_generation"],
        app_digest=readback_payload["app_image_digest"],
        bundle_digest=readback_payload["bundle_digest"],
        readback_digest=readback_digest,
        build_attestation_id=attestation.id,
        build_attestation_digest=attestation.envelope_digest,
        executor_user_id=actor_user_id,
        verifier_user_id=actor_user_id,
        envelope=envelope,
        envelope_digest=digest,
        payload_digest=payload_digest(payload),
        issued_at=now,
        expires_at=expires_at,
    )
    db.add(receipt)
    await db.flush([receipt])
    await _append_consumption_events(
        db,
        assignment=assignment,
        authorization=authorization,
        actor_user_id=actor_user_id,
        operation_id=body.operation_id,
        result_digest=digest,
        now=now,
    )
    if current is None:
        current = BillingProducerV1ActivePair(
            environment=ENVIRONMENT,
            operation_id=body.operation_id,
            receipt_id=deploy.id,
            receipt_digest=deploy.envelope_digest,
            verification_receipt_id=receipt.id,
            verification_receipt_digest=digest,
            revision=1,
        )
        db.add(current)
    else:
        current.receipt_id = deploy.id
        current.receipt_digest = deploy.envelope_digest
        current.verification_receipt_id = receipt.id
        current.verification_receipt_digest = digest
        current.revision += 1
        current.updated_at = now
    await db.flush()
    return _v1_artifact_response(receipt.id, envelope)


async def _canonical_preflight(
    *,
    db: AsyncSession,
    actor_user_id: UUID,
    body: ProducerPreflightRequest,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    if body.readback.secret_references == []:
        raise ProducerHandoffRejected("readback secret metadata is incomplete")
    readback = body.readback.as_domain()
    scope = preflight_grant_scope(
        operation_id=body.operation_id,
        readback=readback,
        minimum_grant_validity_seconds=body.minimum_grant_validity_seconds,
        minimum_pair_validity_seconds=body.minimum_pair_validity_seconds,
    )
    grants_result = await db.execute(
        select(BillingProducerV1Grant)
        .where(
            BillingProducerV1Grant.id.in_(
                (body.assignment_id, body.authorization_receipt_id)
            )
        )
        .order_by(BillingProducerV1Grant.id)
        .with_for_update()
    )
    grants = {grant.id: grant for grant in grants_result.scalars().all()}
    assignment = grants.get(body.assignment_id)
    authorization = grants.get(body.authorization_receipt_id)
    if assignment is None or authorization is None:
        raise ProducerHandoffRejected("active signed assignment pair is unavailable")
    if not _v1_grant_row_is_valid(assignment) or not _v1_grant_row_is_valid(
        authorization
    ):
        raise ProducerHandoffRejected(
            "signed assignment pair failed readback validation"
        )

    user_ids = {
        assignment.issuer_user_id,
        assignment.executor_user_id,
        authorization.approver_user_id,
    }
    if (
        None in user_ids
        or len(user_ids) != 3
        or actor_user_id != assignment.executor_user_id
    ):
        raise ProducerHandoffRejected(
            "assignment actors are not distinct or authorized"
        )
    users = (
        (await db.execute(select(User).where(User.id.in_(user_ids)).with_for_update()))
        .scalars()
        .all()
    )
    if len(users) != 3 or any(
        not user.is_active or user.role != UserRole.super_admin for user in users
    ):
        raise ProducerHandoffRejected(
            "all grant actors must remain active super_admins"
        )

    try:
        validate_preflight_grant_pair(
            assignment_envelope=assignment.envelope,
            authorization_envelope=authorization.envelope,
            assignment_digest=assignment.envelope_digest,
            authorization_digest=authorization.envelope_digest,
            assignment_id=assignment.id,
            authorization_id=authorization.id,
            actor_user_id=actor_user_id,
            operation_id=body.operation_id,
            readback=readback,
            minimum_grant_validity_seconds=body.minimum_grant_validity_seconds,
            minimum_pair_validity_seconds=body.minimum_pair_validity_seconds,
            assignment_trust=_v1_trust_entry("billing_producer_assignment.v1"),
            authorization_trust=_v1_trust_entry("billing_producer_authorization.v1"),
            now=now,
        )
    except ProducerGrantRejected as exc:
        if str(exc) == "grant validity window expired":
            raise ProducerHandoffRejected("grant validity window expired") from None
        raise ProducerHandoffRejected(
            "grant pair does not match this exact readback"
        ) from None
    except (ProducerHandoffRejected, SignedEnvelopeRejected):
        raise ProducerHandoffRejected(
            "grant pair does not match this exact readback"
        ) from None
    grant_expiries = tuple(
        datetime.fromisoformat(grant.envelope["expires_at"].replace("Z", "+00:00"))
        for grant in (assignment, authorization)
    )
    if not grants_have_minimum_validity(
        expiries=grant_expiries,
        now=now,
        minimum_grant_validity_seconds=body.minimum_grant_validity_seconds,
    ):
        raise ProducerHandoffRejected("grant pair lacks the requested validity window")

    prior_events = (
        (
            await db.execute(
                select(BillingProducerV1GrantConsumption.grant_id).where(
                    BillingProducerV1GrantConsumption.grant_id.in_(
                        (assignment.id, authorization.id)
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    revocations = (
        (
            await db.execute(
                select(BillingProducerV1GrantRevocation.grant_id).where(
                    BillingProducerV1GrantRevocation.grant_id.in_(
                        (assignment.id, authorization.id)
                    )
                )
            )
        )
        .scalars()
        .all()
    )
    if prior_events or revocations:
        raise ProducerHandoffRejected("assignment pair was already consumed or revoked")

    active = await db.scalar(
        select(BillingProducerV1ActivePair)
        .where(BillingProducerV1ActivePair.environment == ENVIRONMENT)
        .with_for_update()
    )
    if active is None:
        result: dict[str, Any] = {"action": "create", "current_active_pair": None}
    else:
        if active.operation_id != body.operation_id:
            raise ProducerHandoffRejected("another operation owns the active pair")
        deploy = await db.scalar(
            select(BillingProducerV1Receipt).where(
                BillingProducerV1Receipt.id == active.receipt_id,
                BillingProducerV1Receipt.operation_id == body.operation_id,
                BillingProducerV1Receipt.envelope_digest == active.receipt_digest,
            )
        )
        verified = await db.scalar(
            select(BillingProducerV1Receipt).where(
                BillingProducerV1Receipt.id == active.verification_receipt_id,
                BillingProducerV1Receipt.operation_id == body.operation_id,
                BillingProducerV1Receipt.envelope_digest
                == active.verification_receipt_digest,
            )
        )
        if deploy is None or verified is None:
            raise ProducerHandoffRejected("canonical active pair is incomplete")
        receipt_trust = _v1_trust_entry("billing_producer_deployed.v1")
        verification_trust = _v1_trust_entry("billing_producer_deploy_verified.v1")
        if not _v1_receipt_row_is_valid(
            deploy, expected_type="billing_producer_deployed.v1", trust=receipt_trust
        ) or not _v1_receipt_row_is_valid(
            verified,
            expected_type="billing_producer_deploy_verified.v1",
            trust=verification_trust,
        ):
            raise ProducerHandoffRejected(
                "canonical active pair signatures are invalid"
            )
        expected_readback = readback.as_payload()
        deploy_payload = deploy.envelope["payload"]
        verify_payload = verified.envelope["payload"]
        attestation = await db.scalar(
            select(BillingProducerV1BuildAttestation).where(
                BillingProducerV1BuildAttestation.id == deploy.build_attestation_id,
                BillingProducerV1BuildAttestation.envelope_digest
                == deploy.build_attestation_digest,
            )
        )
        if attestation is None:
            raise ProducerHandoffRejected("active build attestation is unavailable")
        try:
            attestation_trust = _v1_trust_entry("billing_build_attestation.v1")
            verified_attestation_trust = verify_envelope(
                attestation.envelope,
                TrustRegistry([attestation_trust]),
                expected_type="billing_build_attestation.v1",
            )
            validate_attestation_trust(verified_attestation_trust, attestation.envelope)
            attestation_payload = attestation.envelope["payload"]
            attestation_valid = (
                _attestation_is_unexpired(attestation.envelope, now=now)
                and envelope_digest(attestation.envelope) == attestation.envelope_digest
                and payload_digest(attestation_payload) == attestation.payload_digest
                and attestation_payload.get("attestation_id") == str(attestation.id)
                and attestation_payload.get("app_image_digest") == deploy.app_digest
                and attestation_payload.get("bundle_digest") == deploy.bundle_digest
                and attestation_payload.get("commit_sha")
                == deploy_payload.get("commit_sha")
                and attestation_payload.get("tree_sha")
                == deploy_payload.get("tree_sha")
                and attestation_payload.get("workflow_id")
                == deploy_payload.get("workflow_id")
                and attestation.operation_id == body.operation_id
            )
        except (SignedEnvelopeRejected, KeyError, TypeError):
            attestation_valid = False
        if (
            not attestation_valid
            or deploy_payload.get("readback") != expected_readback
            or deploy_payload.get("readback_digest") != scope["readback_digest"]
            or verify_payload.get("source_receipt_id") != str(deploy.id)
            or verify_payload.get("source_receipt_digest") != deploy.envelope_digest
            or verify_payload.get("readback_digest")
            != deploy_payload.get("readback_digest")
            or verify_payload.get("deployment_id") != deploy.deployment_id
            or verify_payload.get("task_set_id") != deploy.task_set_id
            or verify_payload.get("config_generation") != deploy.config_generation
            or verify_payload.get("compatibility_generation")
            != deploy.compatibility_generation
            or verify_payload.get("app_digest") != deploy.app_digest
            or verify_payload.get("bundle_digest") != deploy.bundle_digest
            or verify_payload.get("build_attestation_id")
            != str(deploy.build_attestation_id)
            or verify_payload.get("build_attestation_digest")
            != deploy.build_attestation_digest
            or deploy.executor_user_id == verified.verifier_user_id
        ):
            raise ProducerHandoffRejected(
                "active pair does not match current readback/provenance"
            )
        current_pair = {
            "receipt_id": str(deploy.id),
            "receipt_digest": deploy.envelope_digest,
            "verification_receipt_id": str(verified.id),
            "verification_receipt_digest": verified.envelope_digest,
        }
        result = {
            "action": producer_pair_preflight_action(
                deploy_expires_at=deploy.expires_at,
                verification_expires_at=verified.expires_at,
                now=now,
                minimum_pair_validity_seconds=body.minimum_pair_validity_seconds,
            ),
            "current_active_pair": current_pair,
        }

    result_digest = sha256_hex(jcs(result))
    request_id = uuid4()
    consumed_at = now
    for grant in (assignment, authorization):
        db.add(
            BillingProducerV1GrantConsumption(
                id=uuid4(),
                grant_id=grant.id,
                grant_digest=grant.envelope_digest,
                operation_id=body.operation_id,
                request_id=request_id,
                executor_user_id=actor_user_id,
                result_digest=result_digest,
                consumed_at=consumed_at,
            )
        )
    await db.flush()
    return result


@router.post("/preflight", summary="Resolve the active producer pair server-side")
async def producer_pair_preflight(
    body: ProducerPreflightRequest,
    request: Request,
    tenant: TenantContext = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    actor_user_id = _require_short_staging_jwt(request, tenant)
    try:
        async with db.begin():
            result = await _canonical_preflight(
                db=db,
                actor_user_id=actor_user_id,
                body=body,
            )
        return result
    except (ProducerHandoffRejected, IntegrityError) as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "producer_pair_blocked",
                "reason": (
                    str(exc)
                    if isinstance(exc, ProducerHandoffRejected)
                    else "grant_event_conflict"
                ),
            },
        ) from None


@router.post(
    "/create",
    status_code=status.HTTP_201_CREATED,
    summary="Append a signed producer deployment receipt",
)
async def create_producer_receipt(
    body: ProducerCreateRequest,
    request: Request,
    tenant: TenantContext = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    actor_user_id = _require_short_staging_jwt(request, tenant)
    try:
        async with db.begin():
            result = await _create_v1_deploy_receipt(
                db, body=body, actor_user_id=actor_user_id
            )
        return result
    except ProducerHandoffRejected as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "producer_receipt_rejected", "reason": str(exc)},
        ) from None
    except SignedEnvelopeRejected:
        raise HTTPException(
            status_code=503,
            detail={"error": "producer_receipt_signing_unavailable"},
        ) from None
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail={"error": "producer_receipt_conflict"},
        ) from None


@router.post("/verify", summary="Independently verify and CAS-activate a producer pair")
async def verify_producer_receipt(
    body: ProducerVerifyRequest,
    request: Request,
    tenant: TenantContext = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    actor_user_id = _require_short_staging_jwt(request, tenant)
    try:
        async with db.begin():
            result = await _verify_v1_deploy_receipt(
                db, body=body, actor_user_id=actor_user_id
            )
        return result
    except ProducerHandoffRejected as exc:
        raise HTTPException(
            status_code=409,
            detail={"error": "producer_verification_rejected", "reason": str(exc)},
        ) from None
    except SignedEnvelopeRejected:
        raise HTTPException(
            status_code=503,
            detail={"error": "producer_verification_signing_unavailable"},
        ) from None
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail={"error": "producer_pair_cas_conflict"},
        ) from None
