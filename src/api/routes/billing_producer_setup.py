"""Human-authorized registration of CI-signed ASAAS build attestations."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, ValidationError
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
from src.services.billing_producer_grants import preflight_grant_scope
from src.services.billing_producer_handoff import (
    ProducerHandoffRejected,
    ProducerReadback,
)
from src.services.billing_signed_envelope import (
    ALGORITHM,
    CLUSTER_ARN,
    ENVIRONMENT,
    OPERATIONAL_ROLE,
    PURPOSE_BY_TYPE,
    SCHEMA_VERSION,
    SERVICE_ARN,
    Boto3KmsDigestSigner,
    SignedEnvelopeRejected,
    TrustEntry,
    TrustRegistry,
    build_envelope,
    envelope_digest,
    jcs,
    parse_json_no_duplicate_keys,
    payload_digest,
    sha256_hex,
    validate_attestation_trust,
    verify_envelope,
)

router = APIRouter()
public_router = APIRouter()


class BuildAttestationRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["billing_build_attestation_registration.v1"]
    envelope: dict[str, Any]


class ProducerSecretReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    arn: str
    version_id: str


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
    secret_references: list[ProducerSecretReference] = Field(
        min_length=1, max_length=64
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
                (item.name, item.arn, item.version_id)
                for item in self.secret_references
            ),
        )


class ProducerAssignmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["billing_producer_assignment_request.v1"]
    assignment_id: UUID
    operation_id: UUID
    executor_user_id: UUID
    action: Literal[
        "read_billing_producer_pair",
        "create_billing_producer_deploy_receipt",
        "verify_billing_producer_deploy_receipt",
    ]
    receipt_ids: list[UUID] = Field(max_length=2)
    ttl_seconds: int = Field(ge=1, le=600)
    readback: ProducerReadbackRequest | None = None
    minimum_grant_validity_seconds: int = Field(default=60, ge=60, le=300, strict=True)
    minimum_pair_validity_seconds: Literal[300] = 300


class ProducerAuthorizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["billing_producer_authorization_request.v1"]
    authorization_id: UUID
    assignment_id: UUID
    assignment_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    ttl_seconds: int = Field(ge=1, le=600)


class ProducerRevocationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["billing_producer_revocation_request.v1"]
    revocation_id: UUID
    grant_type: Literal["assignment", "authorization"]
    grant_id: UUID
    grant_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason_code: Literal[
        "operator_requested",
        "identity_or_scope_error",
        "key_compromise",
        "superseded",
        "incident_response",
    ]


def _build_attestation_entry() -> TrustEntry:
    values = {
        "issuer": settings.billing_producer_build_issuer,
        "kid": settings.billing_producer_build_kid,
        "audience": settings.billing_producer_build_audience,
        "public_key_pem": settings.billing_producer_build_public_key,
        "repository_id": settings.billing_producer_build_repository_id,
        "repository_name": settings.billing_producer_build_repository_name,
        "workflow_path": settings.billing_producer_build_workflow_path,
        "workflow_ref": settings.billing_producer_build_workflow_ref,
        "oidc_issuer": settings.billing_producer_build_oidc_issuer,
        "oidc_subject": settings.billing_producer_build_oidc_subject,
    }
    if any(not value for value in values.values()):
        raise SignedEnvelopeRejected("administrator build trust pins are unavailable")
    return TrustEntry(
        issuer=values["issuer"],
        kid=values["kid"],
        environment=ENVIRONMENT,
        audience=values["audience"],
        type="billing_build_attestation.v1",
        purpose=PURPOSE_BY_TYPE["billing_build_attestation.v1"],
        schema_version=SCHEMA_VERSION,
        algorithm=ALGORITHM,
        public_key_pem=values["public_key_pem"],
        repository_id=values["repository_id"],
        repository_name=values["repository_name"],
        workflow_path=values["workflow_path"],
        workflow_ref=values["workflow_ref"],
        oidc_issuer=values["oidc_issuer"],
        oidc_subject=values["oidc_subject"],
    )


def _build_attestation_public_key_fingerprint(entry: TrustEntry) -> str:
    """Return the SHA-256 fingerprint of the configured DER public key."""
    try:
        from cryptography.exceptions import UnsupportedAlgorithm
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        public_key = serialization.load_pem_public_key(
            entry.public_key_pem.encode("ascii")
        )
        if not isinstance(public_key, ec.EllipticCurvePublicKey) or not isinstance(
            public_key.curve, ec.SECP256R1
        ):
            raise ValueError("unsupported build verification key")
        der = public_key.public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    except ImportError:
        raise SignedEnvelopeRejected(
            "administrator build verification key is invalid"
        ) from None
    except (UnsupportedAlgorithm, ValueError, UnicodeEncodeError):
        raise SignedEnvelopeRejected(
            "administrator build verification key is invalid"
        ) from None
    return hashlib.sha256(der).hexdigest()


@router.get(
    "/producer-build-trust",
    summary="Read the active public trust for CI build attestations",
)
async def read_producer_build_trust(response: Response) -> dict[str, str]:
    """Expose the active staging trust tuple needed by protected CI preflight."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    if settings.environment != ENVIRONMENT:
        raise HTTPException(status_code=404, detail="build_trust_unavailable")
    try:
        entry = _build_attestation_entry()
        fingerprint = _build_attestation_public_key_fingerprint(entry)
    except (SignedEnvelopeRejected, ValueError):
        raise HTTPException(status_code=503, detail="build_trust_unavailable") from None
    return {
        "schema": "billing_producer_build_trust_readback.v1",
        "schema_version": entry.schema_version,
        "environment": entry.environment,
        "type": entry.type,
        "purpose": entry.purpose,
        "algorithm": entry.algorithm,
        "issuer": entry.issuer,
        "kid": entry.kid,
        "audience": entry.audience,
        "public_key_fingerprint_sha256": fingerprint,
        "repository_id": entry.repository_id or "",
        "repository_name": entry.repository_name or "",
        "workflow_path": entry.workflow_path or "",
        "workflow_ref": entry.workflow_ref or "",
        "oidc_issuer": entry.oidc_issuer or "",
        "oidc_subject": entry.oidc_subject or "",
    }


@public_router.get(
    "/producer-build-trust",
    summary="Read public trust pins for the protected ASAAS build workflow",
)
async def read_public_producer_build_trust(response: Response) -> dict[str, str]:
    """Expose only non-secret verifier pins to the protected build workflow."""
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    if settings.environment != ENVIRONMENT:
        raise HTTPException(status_code=404, detail="build_trust_unavailable")
    try:
        entry = _build_attestation_entry()
        fingerprint = _build_attestation_public_key_fingerprint(entry)
    except (SignedEnvelopeRejected, ValueError):
        raise HTTPException(
            status_code=503,
            detail={"error": "producer_build_trust_unavailable"},
        ) from None
    return {
        "schema": "billing_producer_build_trust_readback.v1",
        "schema_version": entry.schema_version,
        "environment": entry.environment,
        "type": entry.type,
        "purpose": entry.purpose,
        "algorithm": entry.algorithm,
        "issuer": entry.issuer,
        "kid": entry.kid,
        "audience": entry.audience,
        "public_key_fingerprint_sha256": fingerprint,
        "repository_id": entry.repository_id or "",
        "repository_name": entry.repository_name or "",
        "workflow_path": entry.workflow_path or "",
        "workflow_ref": entry.workflow_ref or "",
        "oidc_issuer": entry.oidc_issuer or "",
        "oidc_subject": entry.oidc_subject or "",
        "read_at_utc": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }


def _require_staging_super_admin(request: Request, tenant: TenantContext) -> UUID:
    if (
        settings.environment != ENVIRONMENT
        or not getattr(request.state, "staging_rehearsal_token", False)
        or tenant.authentication_mode != "jwt"
        or tenant.user_id is None
    ):
        raise HTTPException(
            status_code=403,
            detail={"error": "staging_super_admin_jwt_required"},
        )
    return tenant.user_id


def _grant_trust_entry(type_name: str) -> TrustEntry:
    values = (
        settings.billing_producer_grant_issuer,
        settings.billing_producer_grant_kid,
        settings.billing_producer_grant_audience,
        settings.billing_producer_grant_public_key,
    )
    if any(not value for value in values):
        raise SignedEnvelopeRejected("administrator grant trust pins are unavailable")
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
    )


def _grant_pins_present() -> bool:
    return all(
        (
            settings.billing_producer_grant_issuer,
            settings.billing_producer_grant_kid,
            settings.billing_producer_grant_audience,
            settings.billing_producer_grant_public_key,
        )
    )


def _grant_signer() -> Boto3KmsDigestSigner:
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
        raise SignedEnvelopeRejected("AWS SDK for Python is unavailable") from None
    return Boto3KmsDigestSigner(client)


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SignedEnvelopeRejected("timestamp must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _artifact_response(
    *, identifier_name: str, identifier: UUID, envelope: dict[str, Any]
) -> dict[str, Any]:
    digest = envelope_digest(envelope)
    return {
        identifier_name: str(identifier),
        identifier_name.replace("_id", "_digest"): digest,
        "envelope": envelope,
    }


async def _load_active_super_admins(
    db: AsyncSession, user_ids: set[UUID]
) -> dict[UUID, User]:
    users: dict[UUID, User] = {}
    for user_id in sorted(user_ids, key=str):
        user = await db.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None or not user.is_active or user.role != UserRole.super_admin:
            raise HTTPException(
                status_code=403,
                detail={"error": "active_super_admin_required"},
            )
        users[user_id] = user
    return users


def _canonical_uuid_list(values: Any) -> bool:
    if not isinstance(values, list):
        return False
    for value in values:
        if not isinstance(value, str):
            return False
        try:
            identifier = UUID(value)
        except ValueError:
            return False
        if str(identifier) != value:
            return False
    return True


_READBACK_REQUEST_FIELDS = frozenset(
    {
        "cluster_arn",
        "service_arn",
        "deployment_id",
        "deployment_controller",
        "task_definition_arn",
        "task_arns",
        "config_generation",
        "compatibility_generation",
        "app_image_digest",
        "ecr_image_digest",
        "bundle_digest",
        "readiness",
        "rollout_state",
        "rollout_control",
        "topup_commercial_enabled",
        "secret_references",
    }
)


def _validate_readback_request_shape(raw: Any) -> None:
    if not isinstance(raw, dict) or set(raw) != _READBACK_REQUEST_FIELDS:
        raise HTTPException(
            status_code=422,
            detail={"error": "readback_fields_are_not_exact"},
        )
    string_fields = _READBACK_REQUEST_FIELDS - {
        "task_arns",
        "topup_commercial_enabled",
        "secret_references",
    }
    if any(not isinstance(raw.get(name), str) for name in string_fields):
        raise HTTPException(status_code=422, detail={"error": "invalid_readback_types"})
    if type(raw.get("topup_commercial_enabled")) is not bool:
        raise HTTPException(status_code=422, detail={"error": "invalid_readback_types"})
    task_arns = raw.get("task_arns")
    if (
        not isinstance(task_arns, list)
        or any(not isinstance(value, str) for value in task_arns)
        or task_arns != sorted(task_arns)
    ):
        raise HTTPException(
            status_code=422, detail={"error": "invalid_readback_task_arns"}
        )
    refs = raw.get("secret_references")
    if not isinstance(refs, list) or any(
        not isinstance(item, dict)
        or set(item) != {"name", "arn", "version_id"}
        or any(
            not isinstance(item.get(name), str)
            for name in ("name", "arn", "version_id")
        )
        for item in refs
    ):
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_readback_secret_references"},
        )
    if refs != sorted(
        refs, key=lambda item: (item["name"], item["arn"], item["version_id"])
    ):
        raise HTTPException(
            status_code=422,
            detail={"error": "readback_secret_references_not_canonical"},
        )


def _parse_model(
    raw: bytes, model: type[BaseModel], *, label: str
) -> tuple[dict[str, Any], Any]:
    try:
        document = parse_json_no_duplicate_keys(raw)
        parsed = model.model_validate(document)
    except (SignedEnvelopeRejected, ValidationError):
        raise HTTPException(
            status_code=422,
            detail={"error": f"invalid_{label}_request"},
        ) from None
    return document, parsed


def _require_canonical_uuid(document: dict[str, Any], name: str) -> UUID:
    value = document.get(name)
    try:
        identifier = UUID(value) if isinstance(value, str) else None
    except ValueError:
        identifier = None
    if identifier is None or str(identifier) != value:
        raise HTTPException(
            status_code=422,
            detail={"error": "uuid_must_be_canonical"},
        )
    return identifier


async def _assignment_scope(
    db: AsyncSession, body: ProducerAssignmentRequest
) -> dict[str, Any]:
    if body.action == "read_billing_producer_pair":
        if body.receipt_ids or body.readback is None:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_read_assignment_request"},
            )
        _validate_readback_request_shape(body.readback.model_dump(mode="python"))
        if body.ttl_seconds > 300:
            raise HTTPException(
                status_code=422,
                detail={"error": "read_assignment_ttl_exceeds_300_seconds"},
            )
        try:
            return preflight_grant_scope(
                operation_id=body.operation_id,
                readback=body.readback.as_domain(),
                minimum_grant_validity_seconds=(body.minimum_grant_validity_seconds),
                minimum_pair_validity_seconds=(body.minimum_pair_validity_seconds),
            )
        except ProducerHandoffRejected:
            raise HTTPException(
                status_code=422,
                detail={"error": "invalid_readback_scope"},
            ) from None

    if body.readback is not None or {
        "minimum_grant_validity_seconds",
        "minimum_pair_validity_seconds",
    }.intersection(body.model_fields_set):
        raise HTTPException(
            status_code=422,
            detail={"error": "receipt_assignment_cannot_include_readback"},
        )
    if (
        len(body.receipt_ids) != 2
        or body.receipt_ids[0] == body.receipt_ids[1]
        or body.ttl_seconds > 600
    ):
        raise HTTPException(status_code=422, detail={"error": "invalid_receipt_ids"})
    receipt_id, verification_receipt_id = body.receipt_ids
    active = await db.scalar(
        select(BillingProducerV1ActivePair)
        .where(BillingProducerV1ActivePair.environment == ENVIRONMENT)
        .with_for_update()
    )
    supersedes = None
    if active is not None:
        if active.operation_id != body.operation_id:
            raise HTTPException(
                status_code=409, detail={"error": "another_operation_owns_active_pair"}
            )
        supersedes = {
            "receipt_id": str(active.receipt_id),
            "receipt_digest": active.receipt_digest,
            "verification_receipt_id": str(active.verification_receipt_id),
            "verification_receipt_digest": active.verification_receipt_digest,
        }

    if body.action == "create_billing_producer_deploy_receipt":
        return {
            "operation_id": str(body.operation_id),
            "action": body.action,
            "environment": ENVIRONMENT,
            "operational_role": OPERATIONAL_ROLE,
            "cluster_arn": CLUSTER_ARN,
            "service_arn": SERVICE_ARN,
            "receipt_id": str(receipt_id),
            "verification_receipt_id": str(verification_receipt_id),
            "supersedes": supersedes,
        }

    receipt = await db.scalar(
        select(BillingProducerV1Receipt).where(
            BillingProducerV1Receipt.id == receipt_id,
            BillingProducerV1Receipt.operation_id == body.operation_id,
            BillingProducerV1Receipt.receipt_type == "billing_producer_deployed.v1",
        )
    )
    if receipt is None or receipt.expires_at <= datetime.now(timezone.utc):
        raise HTTPException(
            status_code=409, detail={"error": "deploy_receipt_unavailable"}
        )
    try:
        trust = TrustEntry(
            issuer=settings.billing_producer_receipt_issuer,
            kid=settings.billing_producer_receipt_kid,
            environment=ENVIRONMENT,
            audience=settings.billing_producer_receipt_audience,
            type="billing_producer_deployed.v1",
            purpose=PURPOSE_BY_TYPE["billing_producer_deployed.v1"],
            schema_version=SCHEMA_VERSION,
            algorithm=ALGORITHM,
            public_key_pem=settings.billing_producer_receipt_public_key,
        )
        verify_envelope(
            receipt.envelope,
            TrustRegistry([trust]),
            expected_type="billing_producer_deployed.v1",
        )
        if envelope_digest(receipt.envelope) != receipt.envelope_digest:
            raise SignedEnvelopeRejected("deploy receipt digest differs")
        payload = receipt.envelope["payload"]
        if payload.get("receipt_id") != str(receipt_id):
            raise SignedEnvelopeRejected("deploy receipt ID differs")
        return {
            "operation_id": str(body.operation_id),
            "receipt_id": str(receipt_id),
            "receipt_digest": receipt.envelope_digest,
            "verification_receipt_id": str(verification_receipt_id),
            "readback_digest": receipt.readback_digest,
            "supersedes": supersedes,
        }
    except SignedEnvelopeRejected:
        raise HTTPException(
            status_code=409, detail={"error": "deploy_receipt_signature_invalid"}
        ) from None


def _grant_row_matches_envelope(grant: BillingProducerV1Grant) -> bool:
    try:
        entry = _grant_trust_entry(
            "billing_producer_assignment.v1"
            if grant.grant_kind == "assignment"
            else "billing_producer_authorization.v1"
        )
        verified_entry = verify_envelope(
            grant.envelope,
            TrustRegistry([entry]),
            expected_type=entry.type,
        )
        payload = grant.envelope["payload"]
        return (
            verified_entry == entry
            and envelope_digest(grant.envelope) == grant.envelope_digest
            and payload_digest(payload) == grant.payload_digest
            and payload.get("id") == str(grant.id)
            and payload.get("grant_kind") == grant.grant_kind
            and payload.get("issuer_user_id") == str(grant.issuer_user_id)
            and payload.get("executor_user_id") == str(grant.executor_user_id)
            and payload.get("operation_id") == str(grant.operation_id)
            and payload.get("environment") == grant.environment
            and payload.get("operational_role") == grant.operational_role
            and payload.get("cluster_arn") == grant.cluster_arn
            and payload.get("service_arn") == grant.service_arn
            and payload.get("action") == grant.action
            and payload.get("receipt_id")
            == (str(grant.receipt_id) if grant.receipt_id is not None else None)
            and payload.get("verification_receipt_id")
            == (
                str(grant.verification_receipt_id)
                if grant.verification_receipt_id is not None
                else None
            )
            and payload.get("scope") == grant.scope
            and payload.get("scope_digest") == grant.scope_digest
            and grant.envelope.get("operation_id") == str(grant.operation_id)
            and grant.envelope.get("issued_at") == _timestamp(grant.issued_at)
            and grant.envelope.get("expires_at") == _timestamp(grant.expires_at)
            and payload.get("issued_at") == grant.envelope.get("issued_at")
            and payload.get("expires_at") == grant.envelope.get("expires_at")
            and payload.get("assignment_id")
            == (str(grant.assignment_id) if grant.assignment_id is not None else None)
            and payload.get("assignment_digest") == grant.assignment_digest
            and payload.get("approver_user_id")
            == (
                str(grant.approver_user_id)
                if grant.approver_user_id is not None
                else None
            )
        )
    except (SignedEnvelopeRejected, KeyError, TypeError):
        return False


@router.post(
    "/producer-grants/assignments",
    status_code=201,
    summary="Issue a signed producer assignment",
)
async def issue_producer_assignment(
    request: Request,
    tenant: TenantContext = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    issuer_user_id = _require_staging_super_admin(request, tenant)
    document, body = _parse_model(
        await request.body(), ProducerAssignmentRequest, label="producer_assignment"
    )
    assignment_id = _require_canonical_uuid(document, "assignment_id")
    operation_id = _require_canonical_uuid(document, "operation_id")
    executor_user_id = _require_canonical_uuid(document, "executor_user_id")
    if type(document.get("ttl_seconds")) is not int:
        raise HTTPException(status_code=422, detail={"error": "invalid_ttl_seconds"})
    if any(
        field in document and type(document.get(field)) is not int
        for field in (
            "minimum_grant_validity_seconds",
            "minimum_pair_validity_seconds",
        )
    ):
        raise HTTPException(
            status_code=422,
            detail={"error": "invalid_preflight_validity_windows"},
        )
    receipt_ids_raw = document.get("receipt_ids")
    if not _canonical_uuid_list(receipt_ids_raw):
        raise HTTPException(status_code=422, detail={"error": "invalid_receipt_ids"})
    if issuer_user_id == executor_user_id:
        raise HTTPException(
            status_code=403, detail={"error": "grant_actors_must_differ"}
        )
    if not _grant_pins_present():
        raise HTTPException(
            status_code=503,
            detail={"error": "producer_grant_trust_unavailable"},
        )

    now = datetime.now(timezone.utc)
    issued_at = _timestamp(now)
    expires = now + timedelta(seconds=body.ttl_seconds)
    expires_at = _timestamp(expires)
    try:
        entry = _grant_trust_entry("billing_producer_assignment.v1")
        async with db.begin():
            await _load_active_super_admins(db, {issuer_user_id, executor_user_id})
            scope = await _assignment_scope(db, body)
            scope_digest = sha256_hex(jcs(scope))
            existing = await db.scalar(
                select(BillingProducerV1Grant)
                .where(BillingProducerV1Grant.id == assignment_id)
                .with_for_update()
            )
            if existing is not None:
                try:
                    prior_issued = datetime.fromisoformat(
                        existing.envelope["issued_at"].replace("Z", "+00:00")
                    )
                    prior_expiry = datetime.fromisoformat(
                        existing.envelope["expires_at"].replace("Z", "+00:00")
                    )
                except (KeyError, AttributeError, TypeError, ValueError):
                    prior_issued = prior_expiry = now
                prior_payload = existing.envelope.get("payload", {})
                if (
                    existing.grant_kind != "assignment"
                    or not _grant_row_matches_envelope(existing)
                    or existing.issuer_user_id != issuer_user_id
                    or existing.executor_user_id != executor_user_id
                    or existing.operation_id != operation_id
                    or existing.action != body.action
                    or existing.scope_digest != scope_digest
                    or int((prior_expiry - prior_issued).total_seconds())
                    != body.ttl_seconds
                    or prior_payload.get("scope") != scope
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={"error": "assignment_id_conflict"},
                    )
                return _artifact_response(
                    identifier_name="assignment_id",
                    identifier=existing.id,
                    envelope=existing.envelope,
                )

            receipt_id = body.receipt_ids[0] if body.receipt_ids else None
            verification_receipt_id = body.receipt_ids[1] if body.receipt_ids else None
            payload = {
                "id": str(assignment_id),
                "grant_kind": "assignment",
                "issuer_user_id": str(issuer_user_id),
                "executor_user_id": str(executor_user_id),
                "operation_id": str(operation_id),
                "environment": ENVIRONMENT,
                "operational_role": OPERATIONAL_ROLE,
                "cluster_arn": CLUSTER_ARN,
                "service_arn": SERVICE_ARN,
                "action": body.action,
                "receipt_id": str(receipt_id) if receipt_id is not None else None,
                "verification_receipt_id": (
                    str(verification_receipt_id)
                    if verification_receipt_id is not None
                    else None
                ),
                "scope": scope,
                "scope_digest": scope_digest,
                "issued_at": issued_at,
                "expires_at": expires_at,
            }
            envelope = build_envelope(
                entry=entry,
                payload=payload,
                operation_id=operation_id,
                issued_at=now,
                expires_at=expires,
                signer=_grant_signer(),
            )
            verify_envelope(
                envelope,
                TrustRegistry([entry]),
                expected_type="billing_producer_assignment.v1",
            )
            record = BillingProducerV1Grant(
                id=assignment_id,
                grant_kind="assignment",
                envelope=envelope,
                envelope_digest=envelope_digest(envelope),
                payload_digest=payload_digest(payload),
                issuer_user_id=issuer_user_id,
                executor_user_id=executor_user_id,
                approver_user_id=None,
                assignment_id=None,
                assignment_digest=None,
                operation_id=operation_id,
                environment=ENVIRONMENT,
                operational_role=OPERATIONAL_ROLE,
                cluster_arn=CLUSTER_ARN,
                service_arn=SERVICE_ARN,
                action=body.action,
                receipt_id=receipt_id,
                verification_receipt_id=verification_receipt_id,
                scope=scope,
                scope_digest=scope_digest,
                issued_at=now,
                expires_at=now + timedelta(seconds=body.ttl_seconds),
            )
            db.add(record)
            await db.flush([record])
            return _artifact_response(
                identifier_name="assignment_id",
                identifier=assignment_id,
                envelope=envelope,
            )
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail={"error": "assignment_id_conflict"},
        ) from None
    except SignedEnvelopeRejected:
        raise HTTPException(
            status_code=503,
            detail={"error": "producer_grant_signing_unavailable"},
        ) from None


@router.post(
    "/producer-grants/authorizations",
    status_code=201,
    summary="Approve an active producer assignment",
)
async def issue_producer_authorization(
    request: Request,
    tenant: TenantContext = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    approver_user_id = _require_staging_super_admin(request, tenant)
    document, body = _parse_model(
        await request.body(),
        ProducerAuthorizationRequest,
        label="producer_authorization",
    )
    authorization_id = _require_canonical_uuid(document, "authorization_id")
    assignment_id = _require_canonical_uuid(document, "assignment_id")
    if type(document.get("ttl_seconds")) is not int:
        raise HTTPException(status_code=422, detail={"error": "invalid_ttl_seconds"})
    if not _grant_pins_present():
        raise HTTPException(
            status_code=503,
            detail={"error": "producer_grant_trust_unavailable"},
        )

    now = datetime.now(timezone.utc)
    try:
        authorization_entry = _grant_trust_entry("billing_producer_authorization.v1")
        async with db.begin():
            assignment = await db.scalar(
                select(BillingProducerV1Grant)
                .where(BillingProducerV1Grant.id == assignment_id)
                .with_for_update()
            )
            if (
                assignment is None
                or assignment.grant_kind != "assignment"
                or assignment.envelope_digest != body.assignment_digest
                or not _grant_row_matches_envelope(assignment)
                or assignment.expires_at <= now
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"error": "assignment_unavailable_or_mismatched"},
                )
            if (
                await db.scalar(
                    select(BillingProducerV1GrantRevocation.id).where(
                        BillingProducerV1GrantRevocation.grant_id == assignment.id
                    )
                )
                is not None
                or await db.scalar(
                    select(BillingProducerV1GrantConsumption.id).where(
                        BillingProducerV1GrantConsumption.grant_id == assignment.id
                    )
                )
                is not None
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"error": "assignment_inactive"},
                )

            assignment_payload = assignment.envelope["payload"]
            issuer_user_id = UUID(assignment_payload["issuer_user_id"])
            executor_user_id = UUID(assignment_payload["executor_user_id"])
            if approver_user_id in {issuer_user_id, executor_user_id}:
                raise HTTPException(
                    status_code=403,
                    detail={"error": "grant_actors_must_be_distinct"},
                )
            await _load_active_super_admins(
                db, {approver_user_id, issuer_user_id, executor_user_id}
            )

            prior_authorization = await db.scalar(
                select(BillingProducerV1Grant)
                .where(
                    BillingProducerV1Grant.id == authorization_id,
                    BillingProducerV1Grant.grant_kind == "authorization",
                )
                .with_for_update()
            )
            if prior_authorization is not None:
                prior_payload = prior_authorization.envelope.get("payload", {})
                try:
                    prior_issued = datetime.fromisoformat(
                        prior_authorization.envelope["issued_at"].replace("Z", "+00:00")
                    )
                    prior_expiry = datetime.fromisoformat(
                        prior_authorization.envelope["expires_at"].replace(
                            "Z", "+00:00"
                        )
                    )
                except (KeyError, AttributeError, TypeError, ValueError):
                    prior_issued = prior_expiry = now
                if (
                    not _grant_row_matches_envelope(prior_authorization)
                    or prior_authorization.assignment_id != assignment_id
                    or prior_authorization.assignment_digest != body.assignment_digest
                    or prior_authorization.approver_user_id != approver_user_id
                    or int((prior_expiry - prior_issued).total_seconds())
                    != body.ttl_seconds
                    or prior_payload.get("executor_user_id") != str(executor_user_id)
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={"error": "authorization_id_conflict"},
                    )
                return _artifact_response(
                    identifier_name="authorization_id",
                    identifier=prior_authorization.id,
                    envelope=prior_authorization.envelope,
                )

            expires = now + timedelta(seconds=body.ttl_seconds)
            if expires > assignment.expires_at:
                raise HTTPException(
                    status_code=422,
                    detail={"error": "authorization_ttl_exceeds_assignment"},
                )
            issued_at_text = _timestamp(now)
            expires_at_text = _timestamp(expires)
            payload = {
                "id": str(authorization_id),
                "grant_kind": "authorization",
                "issuer_user_id": str(issuer_user_id),
                "executor_user_id": str(executor_user_id),
                "operation_id": str(assignment.operation_id),
                "environment": assignment.environment,
                "operational_role": assignment.operational_role,
                "cluster_arn": assignment.cluster_arn,
                "service_arn": assignment.service_arn,
                "action": assignment.action,
                "receipt_id": (
                    str(assignment.receipt_id)
                    if assignment.receipt_id is not None
                    else None
                ),
                "verification_receipt_id": (
                    str(assignment.verification_receipt_id)
                    if assignment.verification_receipt_id is not None
                    else None
                ),
                "scope": assignment.scope,
                "scope_digest": assignment.scope_digest,
                "issued_at": issued_at_text,
                "expires_at": expires_at_text,
                "assignment_id": str(assignment_id),
                "assignment_digest": assignment.envelope_digest,
                "approver_user_id": str(approver_user_id),
            }
            envelope = build_envelope(
                entry=authorization_entry,
                payload=payload,
                operation_id=assignment.operation_id,
                issued_at=now,
                expires_at=expires,
                signer=_grant_signer(),
            )
            verify_envelope(
                envelope,
                TrustRegistry([authorization_entry]),
                expected_type="billing_producer_authorization.v1",
            )
            record = BillingProducerV1Grant(
                id=authorization_id,
                grant_kind="authorization",
                envelope=envelope,
                envelope_digest=envelope_digest(envelope),
                payload_digest=payload_digest(payload),
                issuer_user_id=issuer_user_id,
                executor_user_id=executor_user_id,
                approver_user_id=approver_user_id,
                assignment_id=assignment_id,
                assignment_digest=assignment.envelope_digest,
                operation_id=assignment.operation_id,
                environment=assignment.environment,
                operational_role=assignment.operational_role,
                cluster_arn=assignment.cluster_arn,
                service_arn=assignment.service_arn,
                action=assignment.action,
                receipt_id=assignment.receipt_id,
                verification_receipt_id=assignment.verification_receipt_id,
                scope=assignment.scope,
                scope_digest=assignment.scope_digest,
                issued_at=now,
                expires_at=expires,
            )
            db.add(record)
            await db.flush([record])
            return _artifact_response(
                identifier_name="authorization_id",
                identifier=authorization_id,
                envelope=envelope,
            )
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail={"error": "authorization_id_conflict"},
        ) from None
    except SignedEnvelopeRejected:
        raise HTTPException(
            status_code=503,
            detail={"error": "producer_grant_signing_unavailable"},
        ) from None


@router.post(
    "/producer-grants/revocations",
    status_code=201,
    summary="Append a signed producer grant revocation",
)
async def revoke_producer_grant(
    request: Request,
    tenant: TenantContext = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    revoker_user_id = _require_staging_super_admin(request, tenant)
    document, body = _parse_model(
        await request.body(), ProducerRevocationRequest, label="producer_revocation"
    )
    revocation_id = _require_canonical_uuid(document, "revocation_id")
    grant_id = _require_canonical_uuid(document, "grant_id")
    if not _grant_pins_present():
        raise HTTPException(
            status_code=503,
            detail={"error": "producer_grant_trust_unavailable"},
        )
    now = datetime.now(timezone.utc)
    try:
        revocation_entry = _grant_trust_entry("billing_producer_grant_revocation.v1")
        async with db.begin():
            await _load_active_super_admins(db, {revoker_user_id})
            grant = await db.scalar(
                select(BillingProducerV1Grant)
                .where(BillingProducerV1Grant.id == grant_id)
                .with_for_update()
            )
            if (
                grant is None
                or grant.grant_kind != body.grant_type
                or grant.envelope_digest != body.grant_digest
                or not _grant_row_matches_envelope(grant)
            ):
                raise HTTPException(
                    status_code=409,
                    detail={"error": "grant_digest_or_type_mismatch"},
                )

            existing_by_id = await db.scalar(
                select(BillingProducerV1GrantRevocation)
                .where(BillingProducerV1GrantRevocation.id == revocation_id)
                .with_for_update()
            )
            existing_for_grant = await db.scalar(
                select(BillingProducerV1GrantRevocation)
                .where(BillingProducerV1GrantRevocation.grant_id == grant_id)
                .with_for_update()
            )
            if existing_by_id is not None or existing_for_grant is not None:
                if existing_by_id is None:
                    assert existing_for_grant is not None
                    existing = existing_for_grant
                else:
                    existing = existing_by_id
                if (
                    existing.id != revocation_id
                    or existing.grant_id != grant_id
                    or existing.grant_digest != body.grant_digest
                    or existing.actor_user_id != revoker_user_id
                    or existing.reason_code != body.reason_code
                ):
                    raise HTTPException(
                        status_code=409,
                        detail={"error": "grant_already_revoked_or_id_conflict"},
                    )
                try:
                    verify_envelope(
                        existing.envelope,
                        TrustRegistry([revocation_entry]),
                        expected_type="billing_producer_grant_revocation.v1",
                    )
                except SignedEnvelopeRejected:
                    raise HTTPException(
                        status_code=409,
                        detail={"error": "stored_revocation_is_invalid"},
                    ) from None
                if envelope_digest(existing.envelope) != existing.envelope_digest:
                    raise HTTPException(
                        status_code=409,
                        detail={"error": "stored_revocation_is_invalid"},
                    )
                return _artifact_response(
                    identifier_name="revocation_id",
                    identifier=existing.id,
                    envelope=existing.envelope,
                )

            issued_at = _timestamp(now)
            payload = {
                "revocation_type": "billing_producer_grant_revocation.v1",
                "revocation_id": str(revocation_id),
                "operation_id": str(grant.operation_id),
                "grant_type": body.grant_type,
                "grant_id": str(grant_id),
                "grant_digest": grant.envelope_digest,
                "revoker_user_id": str(revoker_user_id),
                "reason_code": body.reason_code,
                "revoked_at": issued_at,
            }
            envelope = build_envelope(
                entry=revocation_entry,
                payload=payload,
                operation_id=grant.operation_id,
                issued_at=now,
                expires_at=now + timedelta(minutes=10),
                signer=_grant_signer(),
            )
            verify_envelope(
                envelope,
                TrustRegistry([revocation_entry]),
                expected_type="billing_producer_grant_revocation.v1",
            )
            record = BillingProducerV1GrantRevocation(
                id=revocation_id,
                grant_id=grant_id,
                grant_digest=grant.envelope_digest,
                operation_id=grant.operation_id,
                actor_user_id=revoker_user_id,
                reason_code=body.reason_code,
                envelope=envelope,
                envelope_digest=envelope_digest(envelope),
                payload_digest=payload_digest(payload),
                revoked_at=now,
            )
            db.add(record)
            await db.flush([record])
            return _artifact_response(
                identifier_name="revocation_id",
                identifier=revocation_id,
                envelope=envelope,
            )
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail={"error": "grant_already_revoked_or_id_conflict"},
        ) from None
    except SignedEnvelopeRejected:
        raise HTTPException(
            status_code=503,
            detail={"error": "producer_grant_signing_unavailable"},
        ) from None


@router.post(
    "/producer-build-attestations",
    status_code=201,
    summary="Register CI-signed immutable build provenance",
)
async def register_build_attestation(
    request: Request,
    tenant: TenantContext = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> dict[str, Any]:
    actor_user_id = _require_staging_super_admin(request, tenant)
    try:
        raw = await request.body()
        request_body = BuildAttestationRegistrationRequest.model_validate(
            parse_json_no_duplicate_keys(raw)
        )
        entry = _build_attestation_entry()
        registry = TrustRegistry([entry])
        envelope = request_body.envelope
        verified_entry = verify_envelope(
            envelope,
            registry,
            expected_type="billing_build_attestation.v1",
        )
        validate_attestation_trust(verified_entry, envelope)
    except (SignedEnvelopeRejected, ValidationError):
        raise HTTPException(
            status_code=503 if not _build_pins_present() else 422,
            detail={"error": "build_attestation_rejected"},
        ) from None

    signed_payload = envelope["payload"]
    attestation_id = UUID(signed_payload["attestation_id"])
    operation_id = UUID(envelope["operation_id"])
    attestation_digest = envelope_digest(envelope)
    signed_payload_digest = payload_digest(signed_payload)
    try:
        async with db.begin():
            user = await db.scalar(
                select(User).where(User.id == actor_user_id).with_for_update()
            )
            if user is None or not user.is_active or user.role != UserRole.super_admin:
                raise HTTPException(
                    status_code=403,
                    detail={"error": "active_super_admin_required"},
                )

            existing = await db.get(BillingProducerV1BuildAttestation, attestation_id)
            if existing is not None:
                if existing.envelope_digest != attestation_digest:
                    raise HTTPException(
                        status_code=409,
                        detail={"error": "build_attestation_id_conflict"},
                    )
                return {
                    "attestation_id": str(existing.id),
                    "attestation_digest": existing.envelope_digest,
                    "registered_at": existing.registered_at.astimezone(timezone.utc)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z"),
                }

            record = BillingProducerV1BuildAttestation(
                id=attestation_id,
                operation_id=operation_id,
                envelope=dict(envelope),
                envelope_digest=attestation_digest,
                payload_digest=signed_payload_digest,
                registered_by_user_id=actor_user_id,
                registered_at=datetime.now(timezone.utc),
            )
            db.add(record)
            await db.flush([record])
            return {
                "attestation_id": str(record.id),
                "attestation_digest": record.envelope_digest,
                "registered_at": record.registered_at.astimezone(timezone.utc)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z"),
            }
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail={"error": "build_attestation_id_conflict"},
        ) from None


def _build_pins_present() -> bool:
    return all(
        (
            settings.billing_producer_build_issuer,
            settings.billing_producer_build_kid,
            settings.billing_producer_build_audience,
            settings.billing_producer_build_public_key,
            settings.billing_producer_build_repository_id,
            settings.billing_producer_build_repository_name,
            settings.billing_producer_build_workflow_path,
            settings.billing_producer_build_workflow_ref,
            settings.billing_producer_build_oidc_issuer,
            settings.billing_producer_build_oidc_subject,
        )
    )
