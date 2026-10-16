"""Pure validation and signing rules for canonical producer handoff receipts."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

ENVIRONMENT = "staging"
OPERATIONAL_ROLE = "production-serving"
CLUSTER_ARN = "arn:aws:ecs:us-east-1:388460142431:cluster/neurostack-staging"
SERVICE_ARN = (
    "arn:aws:ecs:us-east-1:388460142431:service/"
    "neurostack-staging/neurostack-staging-app"
)
DEPLOYED_TYPE = "billing_producer_deployed.v1"
VERIFIED_TYPE = "billing_producer_deploy_verified.v1"
CREATE_ACTION = "create_billing_producer_deploy_receipt"
VERIFY_ACTION = "verify_billing_producer_deploy_receipt"
PREFLIGHT_ACTION = "read_billing_producer_pair"
PREFLIGHT_GRANT_TTL = timedelta(minutes=5)
RECEIPT_TTL = timedelta(days=7)
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
SECRET_REFERENCE_NAMES = {
    "DATABASE_URL": "neurostack/staging/database-url",
    "REDIS_URL": "neurostack/staging/redis-url",
    "SECRET_KEY": "neurostack/staging/secret-key",
    "LLM_API_KEY": "neurostack/staging/llm-api-key",
    "BILLING_OPERATION_RECEIPT_SIGNING_KEY": (
        "neurostack/staging/billing-operation-receipt-signing-key"
    ),
}
_SECRET_ARN = re.compile(
    r"^arn:aws:secretsmanager:us-east-1:388460142431:secret:([^:]+)(?::.*)?$"
)
_SECRET_GENERATED_SUFFIX = re.compile(r"^(.*)-[A-Za-z0-9]{6}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_TASK_DEFINITION_ARN = re.compile(
    r"^arn:aws:ecs:us-east-1:388460142431:task-definition/"
    r"neurostack-staging-app:[1-9][0-9]*$"
)
_TASK_ARN = re.compile(
    r"^arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/"
    r"[0-9a-fA-F-]{32,36}$"
)


class ProducerHandoffRejected(ValueError):
    """A producer handoff request failed a closed validation boundary."""


@dataclass(frozen=True)
class ProducerReadback:
    """Metadata-only ECS/runtime evidence supplied by the read-only CLI."""

    cluster_arn: str
    service_arn: str
    deployment_id: str
    deployment_controller: str
    task_definition_arn: str
    task_arns: tuple[str, ...]
    config_generation: str
    compatibility_generation: str
    app_image_digest: str
    ecr_image_digest: str
    bundle_digest: str
    readiness: str
    rollout_state: str
    rollout_control: str
    topup_commercial_enabled: bool
    secret_references: tuple[tuple[str, str, str], ...]

    def as_payload(self) -> dict[str, Any]:
        validate_readback(self)
        return {
            "environment": ENVIRONMENT,
            "operational_role": OPERATIONAL_ROLE,
            "cluster_arn": self.cluster_arn,
            "service_arn": self.service_arn,
            "deployment_id": self.deployment_id,
            "task_set_id": compute_task_set_id(self),
            "deployment_controller": self.deployment_controller,
            "task_definition_arn": self.task_definition_arn,
            "task_arns": sorted(self.task_arns),
            "config_generation": self.config_generation,
            "compatibility_generation": self.compatibility_generation,
            "app_image_digest": self.app_image_digest,
            "ecr_image_digest": self.ecr_image_digest,
            "bundle_digest": self.bundle_digest,
            "readiness": self.readiness,
            "rollout_state": self.rollout_state,
            "rollout_control": self.rollout_control,
            "topup_commercial_enabled": self.topup_commercial_enabled,
            "secret_references": [
                {"name": name, "arn": arn, "version_id": version}
                for name, arn, version in sorted(self.secret_references)
            ],
        }


def canonical_json(value: Any) -> bytes:
    """Return deterministic UTF-8 JSON for signed documents and digests."""

    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def task_cohort_payload(readback: ProducerReadback) -> dict[str, Any]:
    return {
        "schema": "ecs-task-cohort.v1",
        "cluster_arn": readback.cluster_arn,
        "service_arn": readback.service_arn,
        "deployment_id": readback.deployment_id,
        "task_definition_arn": readback.task_definition_arn,
        "running_task_arns": sorted(readback.task_arns),
    }


def compute_task_set_id(readback: ProducerReadback) -> str:
    return f"ecs-task-cohort-sha256:{digest_json(task_cohort_payload(readback))}"


def validate_readback(readback: ProducerReadback) -> None:
    """Validate the complete fixed-target and no-egress runtime snapshot."""

    if readback.cluster_arn != CLUSTER_ARN or readback.service_arn != SERVICE_ARN:
        raise ProducerHandoffRejected(
            "target is outside the production-serving allowlist"
        )
    for value in (
        readback.deployment_id,
        readback.config_generation,
        readback.compatibility_generation,
    ):
        if not value or value.strip() != value or "latest" in value.lower():
            raise ProducerHandoffRejected("runtime identity is missing or mutable")
    if readback.deployment_controller != "ECS":
        raise ProducerHandoffRejected(
            "ECS service deployment controller is not allowlisted"
        )
    if not _TASK_DEFINITION_ARN.fullmatch(readback.task_definition_arn):
        raise ProducerHandoffRejected(
            "task definition is outside the staging service allowlist"
        )
    if not readback.task_arns or len(readback.task_arns) > 1000:
        raise ProducerHandoffRejected("running task readback is incomplete")
    if len(set(readback.task_arns)) != len(readback.task_arns):
        raise ProducerHandoffRejected("running task readback is duplicated")
    if not all(_TASK_ARN.fullmatch(task) for task in readback.task_arns):
        raise ProducerHandoffRejected("task readback target is outside the allowlist")
    if not _HEX_64.fullmatch(compute_task_set_id(readback).rsplit(":", 1)[-1]):
        raise ProducerHandoffRejected("task cohort identity is invalid")
    if not _IMAGE_DIGEST.fullmatch(readback.app_image_digest):
        raise ProducerHandoffRejected("application image digest is invalid")
    if readback.app_image_digest != readback.ecr_image_digest:
        raise ProducerHandoffRejected("running and ECR image digests differ")
    for digest in (readback.bundle_digest,):
        if not _HEX_64.fullmatch(digest):
            raise ProducerHandoffRejected("bundle digest is invalid")
    if readback.readiness != "ready" or readback.rollout_state != "COMPLETED":
        raise ProducerHandoffRejected("runtime is not ready and converged")
    if readback.rollout_control != "disabled" or readback.topup_commercial_enabled:
        raise ProducerHandoffRejected("financial rollout must remain disabled")
    expected_secret_names = set(SECRET_REFERENCE_NAMES)
    actual_secret_names = [name for name, _, _ in readback.secret_references]
    if (
        len(actual_secret_names) != len(expected_secret_names)
        or set(actual_secret_names) != expected_secret_names
    ):
        raise ProducerHandoffRejected("secret references do not match the allowlist")
    for name, arn, version in readback.secret_references:
        match = _SECRET_ARN.fullmatch(arn)
        if name not in SECRET_REFERENCE_NAMES or match is None:
            raise ProducerHandoffRejected(
                "secret metadata must match the staging allowlist"
            )
        secret_name = match.group(1)
        suffix = _SECRET_GENERATED_SUFFIX.fullmatch(secret_name)
        if suffix is not None:
            secret_name = suffix.group(1)
        if secret_name != SECRET_REFERENCE_NAMES[name]:
            raise ProducerHandoffRejected("secret reference name is not allowlisted")
        if not version or any(
            part in version.lower() for part in ("secretvalue", "password=")
        ):
            raise ProducerHandoffRejected("secret metadata is invalid")


def receipt_scope(
    *,
    operation_id: UUID,
    receipt_id: UUID,
    verification_receipt_id: UUID,
    readback: ProducerReadback,
    build_attestation_id: UUID,
    build_attestation_digest: str,
    commit_sha: str,
    tree_sha: str,
    workflow_id: str,
    supersedes: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """Build the grant-bound immutable payload from server policy and readback."""

    readback_payload = readback.as_payload()
    for digest in (build_attestation_digest, commit_sha, tree_sha):
        if not _HEX_64.fullmatch(digest):
            raise ProducerHandoffRejected("build provenance digest is invalid")
    if not workflow_id or workflow_id.strip() != workflow_id:
        raise ProducerHandoffRejected("approved workflow identity is required")
    if supersedes is not None:
        expected_keys = {
            "receipt_id",
            "receipt_digest",
            "verification_receipt_id",
            "verification_receipt_digest",
        }
        if set(supersedes) != expected_keys or not all(supersedes.values()):
            raise ProducerHandoffRejected(
                "refresh must supersede the complete prior pair"
            )
        if not _HEX_64.fullmatch(
            str(supersedes["receipt_digest"])
        ) or not _HEX_64.fullmatch(str(supersedes["verification_receipt_digest"])):
            raise ProducerHandoffRejected("superseded receipt digest is invalid")
    payload: dict[str, Any] = {
        "operation_id": str(operation_id),
        "receipt_id": str(receipt_id),
        "verification_receipt_id": str(verification_receipt_id),
        "receipt_type": DEPLOYED_TYPE,
        "environment": ENVIRONMENT,
        "operational_role": OPERATIONAL_ROLE,
        "readback": readback_payload,
        "readback_digest": digest_json(readback_payload),
        "build_attestation_id": str(build_attestation_id),
        "build_attestation_digest": build_attestation_digest,
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "workflow_id": workflow_id,
        "supersedes": dict(supersedes) if supersedes is not None else None,
    }
    return payload


def grant_claims(grant: Any) -> dict[str, Any]:
    """Return the exact stable claims that a trusted grant issuer signs."""

    return {
        "id": str(grant.id),
        "grant_kind": grant.grant_kind,
        "actor_user_id": str(grant.actor_user_id),
        "operation_id": str(grant.operation_id),
        "environment": grant.environment,
        "operational_role": grant.operational_role,
        "cluster_arn": grant.cluster_arn,
        "service_arn": grant.service_arn,
        "action": grant.action,
        "receipt_id": str(grant.receipt_id) if grant.receipt_id is not None else None,
        "verification_receipt_id": (
            str(grant.verification_receipt_id)
            if grant.verification_receipt_id is not None
            else None
        ),
        "scope_digest": grant.scope_digest,
        "issued_at": grant.issued_at.isoformat(),
        "expires_at": grant.expires_at.isoformat(),
    }


def verify_grant_signature(grant: Any, public_key_pem: str) -> None:
    if not public_key_pem:
        raise ProducerHandoffRejected("authorization verification key is unavailable")
    try:
        key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("unsupported key")
        signature = base64.b64decode(grant.claims_signature, validate=True)
        key.verify(signature, canonical_json(grant_claims(grant)))
    except (ValueError, TypeError, InvalidSignature, binascii.Error):
        raise ProducerHandoffRejected(
            "assignment or authorization signature is invalid"
        ) from None


def sign_receipt(payload: Mapping[str, Any], private_key_pem: str) -> tuple[str, str]:
    if not private_key_pem:
        raise ProducerHandoffRejected("receipt signing key is unavailable")
    try:
        key = serialization.load_pem_private_key(
            private_key_pem.encode("utf-8"), password=None
        )
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("unsupported key")
        document = dict(payload)
        payload_digest = digest_json(document)
        signature = key.sign(canonical_json({"payload_digest": payload_digest}))
    except (ValueError, TypeError):
        raise ProducerHandoffRejected("receipt signing key is invalid") from None
    return payload_digest, base64.b64encode(signature).decode("ascii")


def verify_receipt_signature(
    payload: Mapping[str, Any],
    payload_digest: str,
    signature_b64: str,
    public_key_pem: str,
) -> None:
    if not _HEX_64.fullmatch(payload_digest) or digest_json(payload) != payload_digest:
        raise ProducerHandoffRejected("receipt payload digest is invalid")
    try:
        key = serialization.load_pem_public_key(public_key_pem.encode("utf-8"))
        if not isinstance(key, Ed25519PublicKey):
            raise ValueError("unsupported key")
        key.verify(
            base64.b64decode(signature_b64, validate=True),
            canonical_json({"payload_digest": payload_digest}),
        )
    except (ValueError, TypeError, InvalidSignature, binascii.Error):
        raise ProducerHandoffRejected("receipt signature is invalid") from None


def receipt_expiry(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ProducerHandoffRejected("receipt clock must be timezone-aware")
    return now.astimezone(timezone.utc) + RECEIPT_TTL


def preflight_scope(
    *,
    operation_id: UUID,
    readback: ProducerReadback,
    minimum_validity_seconds: int,
) -> dict[str, Any]:
    return {
        "operation_id": str(operation_id),
        "action": PREFLIGHT_ACTION,
        "environment": ENVIRONMENT,
        "operational_role": OPERATIONAL_ROLE,
        "cluster_arn": CLUSTER_ARN,
        "service_arn": SERVICE_ARN,
        "readback_digest": digest_json(readback.as_payload()),
        "minimum_validity_seconds": minimum_validity_seconds,
    }


def fingerprint(payload: Mapping[str, Any]) -> str:
    readback = payload["readback"]
    fields = (
        "cluster_arn",
        "service_arn",
        "deployment_id",
        "task_set_id",
        "deployment_controller",
        "task_definition_arn",
        "config_generation",
        "compatibility_generation",
        "app_image_digest",
        "bundle_digest",
    )
    return digest_json(
        {
            **{field: readback[field] for field in fields},
            "readback_digest": payload["readback_digest"],
        }
    )
