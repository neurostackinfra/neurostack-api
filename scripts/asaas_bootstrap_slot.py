#!/usr/bin/env python3
"""Fail-closed helpers for the single-use ASAAS bootstrap S3 claims.

The bucket is the durable consumption authority. Call :func:`claim_bootstrap_slot`
only after all non-S3 identity/history checks have passed. This function performs
read-only bucket-control and full version-history checks, then makes exactly one
release-side AWS write: ``PutObject`` with ``IfNoneMatch="*"``.

Claim JSON is canonical UTF-8 JSON (sorted keys, compact separators) with the
closed ``asaas_producer_bootstrap_slot_claim.v1`` schema. Build claims bind the
commit, tree, and source-bundle digest. Deployment claims bind the exact signed
build artifact tuple. Operation IDs are run metadata and never enter the signed
build-attestation envelope.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

CLAIM_SCHEMA_VERSION = "asaas_producer_bootstrap_slot_claim.v1"
BUILD_SLOT = "build"
DEPLOYMENT_SLOT = "deployment"
BUILD_KEY = "slots/build-bootstrap.json"
DEPLOYMENT_KEY = "slots/deployment-bootstrap.json"
BUILD_ENVIRONMENT = "asaas-build"
DEPLOYMENT_ENVIRONMENT = "asaas-production-deployment"

_SLOT_CONFIG: dict[str, tuple[str, str, tuple[str, ...]]] = {
    BUILD_SLOT: (
        BUILD_KEY,
        BUILD_ENVIRONMENT,
        ("bundle_digest", "commit_sha", "tree_sha"),
    ),
    DEPLOYMENT_SLOT: (
        DEPLOYMENT_KEY,
        DEPLOYMENT_ENVIRONMENT,
        (
            "app_image_digest",
            "attestation_id",
            "signed_envelope_sha256",
            "workflow_run_id",
        ),
    ),
}
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_REPOSITORY_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_WORKFLOW_PATH = re.compile(r"^\.github/workflows/[^/]+\.ya?ml$")
_REF_SEGMENT = re.compile(r"^[^/.][^/]*$")


class BootstrapSlotError(RuntimeError):
    """A bootstrap slot cannot be proven safe to consume or was not claimed."""


@dataclass(frozen=True)
class ClaimReceipt:
    """Sanitized result of a successful conditional claim write."""

    slot: str
    key: str
    version_id: str
    claim_sha256: str
    version_pages_read: int


def _error_code(error: Exception) -> str | None:
    """Extract an SDK error code without formatting or exposing its message."""

    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    detail = response.get("Error")
    if not isinstance(detail, Mapping):
        return None
    code = detail.get("Code")
    return code if isinstance(code, str) else None


def _read(client: Any, method: str, stage: str, **kwargs: Any) -> Any:
    try:
        return getattr(client, method)(**kwargs)
    except Exception:
        # SDK exceptions can contain request parameters or other sensitive data.
        raise BootstrapSlotError(f"{stage}: read failed") from None


def _expected_policy(bucket_arn: str) -> dict[str, Any]:
    build_arn = f"{bucket_arn}/{BUILD_KEY}"
    deployment_arn = f"{bucket_arn}/{DEPLOYMENT_KEY}"
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "DenyInsecureTransport",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:*",
                "Resource": [bucket_arn, build_arn, deployment_arn],
                "Condition": {"Bool": {"aws:SecureTransport": "false"}},
            },
            {
                "Sid": "RequireBuildClaimIfNoneMatchHeader",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:PutObject",
                "Resource": build_arn,
                "Condition": {"Null": {"s3:if-none-match": "true"}},
            },
            {
                "Sid": "RequireExactBuildClaimIfNoneMatchValue",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:PutObject",
                "Resource": build_arn,
                "Condition": {"StringNotEquals": {"s3:if-none-match": "*"}},
            },
            {
                "Sid": "RequireDeploymentClaimIfNoneMatchHeader",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:PutObject",
                "Resource": deployment_arn,
                "Condition": {"Null": {"s3:if-none-match": "true"}},
            },
            {
                "Sid": "RequireExactDeploymentClaimIfNoneMatchValue",
                "Effect": "Deny",
                "Principal": "*",
                "Action": "s3:PutObject",
                "Resource": deployment_arn,
                "Condition": {"StringNotEquals": {"s3:if-none-match": "*"}},
            },
            {
                "Sid": "DenyBuildClaimDeletion",
                "Effect": "Deny",
                "Principal": "*",
                "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
                "Resource": build_arn,
            },
            {
                "Sid": "DenyDeploymentClaimDeletion",
                "Effect": "Deny",
                "Principal": "*",
                "Action": ["s3:DeleteObject", "s3:DeleteObjectVersion"],
                "Resource": deployment_arn,
            },
        ],
    }


def _normalize_policy(policy: Any) -> dict[str, Any] | None:
    """Normalize only semantically order-insensitive IAM action/resource lists."""

    if not isinstance(policy, Mapping) or set(policy) != {"Version", "Statement"}:
        return None
    statements = policy.get("Statement")
    if not isinstance(statements, list):
        return None
    normalized: list[dict[str, Any]] = []
    for statement in statements:
        if not isinstance(statement, Mapping):
            return None
        item = dict(statement)
        for field in ("Action", "Resource"):
            value = item.get(field)
            if isinstance(value, list):
                if not all(isinstance(entry, str) for entry in value):
                    return None
                item[field] = sorted(value)
        normalized.append(item)
    normalized.sort(key=lambda item: str(item.get("Sid", "")))
    return {"Version": policy["Version"], "Statement": normalized}


def verify_bucket_controls(
    s3_client: Any, *, bucket_name: str, expected_bucket_arn: str | None = None
) -> None:
    """Verify the exact retained-ledger controls before inspecting or claiming it."""

    if not isinstance(bucket_name, str) or not bucket_name.strip():
        raise BootstrapSlotError("bucket identity: missing bucket name")
    bucket_arn = f"arn:aws:s3:::{bucket_name}"
    if expected_bucket_arn is not None and expected_bucket_arn != bucket_arn:
        raise BootstrapSlotError("bucket identity: ARN mismatch")

    versioning = _read(
        s3_client, "get_bucket_versioning", "versioning", Bucket=bucket_name
    )
    if not isinstance(versioning, Mapping) or versioning.get("Status") != "Enabled":
        raise BootstrapSlotError("versioning: must be enabled")
    if versioning.get("MFADelete") not in (None, "Disabled"):
        raise BootstrapSlotError("versioning: unexpected MFA-delete state")

    lock = _read(
        s3_client,
        "get_object_lock_configuration",
        "object lock",
        Bucket=bucket_name,
    )
    lock_configuration = (
        lock.get("ObjectLockConfiguration") if isinstance(lock, Mapping) else None
    )
    rule = (
        lock_configuration.get("Rule")
        if isinstance(lock_configuration, Mapping)
        else None
    )
    retention = rule.get("DefaultRetention") if isinstance(rule, Mapping) else None
    if (
        not isinstance(lock_configuration, Mapping)
        or lock_configuration.get("ObjectLockEnabled") != "Enabled"
        or not isinstance(retention, Mapping)
        or retention.get("Mode") != "COMPLIANCE"
        or retention.get("Years") != 100
        or "Days" in retention
    ):
        raise BootstrapSlotError("object lock: expected 100-year COMPLIANCE retention")

    encryption = _read(
        s3_client, "get_bucket_encryption", "encryption", Bucket=bucket_name
    )
    rules = (
        encryption.get("ServerSideEncryptionConfiguration", {}).get("Rules")
        if isinstance(encryption, Mapping)
        else None
    )
    if (
        not isinstance(rules, list)
        or len(rules) != 1
        or not isinstance(rules[0], Mapping)
        or rules[0].get("ApplyServerSideEncryptionByDefault")
        != {"SSEAlgorithm": "AES256"}
    ):
        raise BootstrapSlotError("encryption: expected a single AES256 default")

    access = _read(
        s3_client,
        "get_public_access_block",
        "public access block",
        Bucket=bucket_name,
    )
    configuration = (
        access.get("PublicAccessBlockConfiguration")
        if isinstance(access, Mapping)
        else None
    )
    expected_access = {
        "BlockPublicAcls": True,
        "BlockPublicPolicy": True,
        "IgnorePublicAcls": True,
        "RestrictPublicBuckets": True,
    }
    if not isinstance(configuration, Mapping) or dict(configuration) != expected_access:
        raise BootstrapSlotError(
            "public access block: all four controls must be enabled"
        )

    policy_response = _read(
        s3_client, "get_bucket_policy", "bucket policy", Bucket=bucket_name
    )
    policy_text = (
        policy_response.get("Policy") if isinstance(policy_response, Mapping) else None
    )
    try:
        policy = json.loads(policy_text) if isinstance(policy_text, str) else None
    except (TypeError, ValueError):
        policy = None
    if _normalize_policy(policy) != _normalize_policy(_expected_policy(bucket_arn)):
        raise BootstrapSlotError(
            "bucket policy: required exact deny controls are missing or changed"
        )

    try:
        lifecycle = s3_client.get_bucket_lifecycle_configuration(Bucket=bucket_name)
    except Exception as error:
        if _error_code(error) != "NoSuchLifecycleConfiguration":
            raise BootstrapSlotError("lifecycle: read failed") from None
        lifecycle = {"Rules": []}
    if not isinstance(lifecycle, Mapping) or lifecycle.get("Rules") != []:
        raise BootstrapSlotError("lifecycle: expiration or other rules are not allowed")


def _cursor_key(
    key_marker: str | None, version_marker: str | None
) -> tuple[str | None, str | None]:
    return key_marker, version_marker


def read_empty_slot(s3_client: Any, *, bucket_name: str, slot: str) -> int:
    """Fully paginate a fixed key's versions and delete markers; require it empty."""

    try:
        key, _environment, _artifact_fields = _SLOT_CONFIG[slot]
    except (KeyError, TypeError):
        raise BootstrapSlotError("slot: unsupported fixed slot") from None
    key_marker: str | None = None
    version_marker: str | None = None
    seen_cursors = {_cursor_key(key_marker, version_marker)}
    pages = 0
    found_version = False
    found_delete_marker = False

    while True:
        arguments: dict[str, str] = {"Bucket": bucket_name, "Prefix": key}
        if key_marker is not None:
            arguments["KeyMarker"] = key_marker
        if version_marker is not None:
            arguments["VersionIdMarker"] = version_marker
        page = _read(s3_client, "list_object_versions", "version history", **arguments)
        pages += 1
        if not isinstance(page, Mapping) or not isinstance(
            page.get("IsTruncated"), bool
        ):
            raise BootstrapSlotError("version history: incomplete page")

        for field, marker_field in (
            ("Versions", "version"),
            ("DeleteMarkers", "delete marker"),
        ):
            items = page.get(field, [])
            if not isinstance(items, list):
                raise BootstrapSlotError("version history: malformed page")
            for item in items:
                if not isinstance(item, Mapping) or item.get("Key") != key:
                    raise BootstrapSlotError(
                        "version history: unexpected key or malformed item"
                    )
                if marker_field == "version":
                    found_version = True
                else:
                    found_delete_marker = True

        if not page["IsTruncated"]:
            break

        next_key = page.get("NextKeyMarker")
        next_version = page.get("NextVersionIdMarker")
        if (
            not isinstance(next_key, str)
            or not next_key
            or (
                next_version is not None
                and (not isinstance(next_version, str) or not next_version)
            )
        ):
            raise BootstrapSlotError(
                "version history: truncated page has invalid next cursor"
            )
        cursor = _cursor_key(next_key, next_version)
        if cursor in seen_cursors:
            raise BootstrapSlotError("version history: repeated pagination cursor")
        seen_cursors.add(cursor)
        key_marker, version_marker = cursor

    if found_version or found_delete_marker:
        raise BootstrapSlotError(
            "slot is permanently consumed by prior version history"
        )
    return pages


def _canonical_uuid(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise BootstrapSlotError(f"claim: invalid {field}")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError):
        raise BootstrapSlotError(f"claim: invalid {field}") from None
    if str(parsed) != value:
        raise BootstrapSlotError(f"claim: invalid {field}")
    return value


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise BootstrapSlotError(f"claim: invalid {field}")
    return value


def _validate_artifact(slot: str, artifact: Mapping[str, Any]) -> dict[str, Any]:
    expected_fields = set(_SLOT_CONFIG[slot][2])
    if set(artifact) != expected_fields:
        raise BootstrapSlotError("claim: artifact fields do not match the slot schema")
    normalized = dict(artifact)
    if slot == BUILD_SLOT:
        for field in ("bundle_digest", "commit_sha", "tree_sha"):
            if not isinstance(normalized[field], str) or not _HEX_64.fullmatch(
                normalized[field]
            ):
                raise BootstrapSlotError(f"claim: invalid artifact {field}")
    else:
        normalized["attestation_id"] = _canonical_uuid(
            normalized["attestation_id"], "artifact attestation_id"
        )
        if not isinstance(
            normalized["app_image_digest"], str
        ) or not _IMAGE_DIGEST.fullmatch(normalized["app_image_digest"]):
            raise BootstrapSlotError("claim: invalid artifact app_image_digest")
        if not isinstance(
            normalized["signed_envelope_sha256"], str
        ) or not _HEX_64.fullmatch(normalized["signed_envelope_sha256"]):
            raise BootstrapSlotError("claim: invalid artifact signed_envelope_sha256")
        normalized["workflow_run_id"] = _positive_int(
            normalized["workflow_run_id"], "artifact workflow_run_id"
        )
    return normalized


def build_claim_document(
    *,
    slot: str,
    repository_id: str,
    repository_name: str,
    workflow_path: str,
    workflow_ref: str,
    workflow_run_id: int,
    workflow_run_attempt: int,
    operation_id: str,
    environment: str,
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate and build the single closed schema shared by both bootstrap slots."""

    try:
        _key, expected_environment, _artifact_fields = _SLOT_CONFIG[slot]
    except (KeyError, TypeError):
        raise BootstrapSlotError("claim: unsupported fixed slot") from None
    if environment != expected_environment:
        raise BootstrapSlotError("claim: environment does not match the fixed slot")
    if (
        not isinstance(repository_id, str)
        or not repository_id.isdecimal()
        or int(repository_id) <= 0
    ):
        raise BootstrapSlotError("claim: invalid repository_id")
    if not isinstance(repository_name, str) or not _REPOSITORY_NAME.fullmatch(
        repository_name
    ):
        raise BootstrapSlotError("claim: invalid repository_name")
    if not isinstance(workflow_path, str) or not _WORKFLOW_PATH.fullmatch(
        workflow_path
    ):
        raise BootstrapSlotError("claim: invalid workflow_path")
    if not isinstance(workflow_ref, str) or not workflow_ref.startswith("refs/heads/"):
        raise BootstrapSlotError("claim: workflow_ref must be a release branch")
    ref_path = workflow_ref.removeprefix("refs/heads/")
    segments = ref_path.split("/")
    if not ref_path or any(
        segment in ("", ".", "..") or not _REF_SEGMENT.fullmatch(segment)
        for segment in segments
    ):
        raise BootstrapSlotError("claim: invalid workflow_ref")
    workflow_run_id = _positive_int(workflow_run_id, "workflow_run_id")
    workflow_run_attempt = _positive_int(workflow_run_attempt, "workflow_run_attempt")
    operation_id = _canonical_uuid(operation_id, "operation_id")
    if not isinstance(artifact, Mapping):
        raise BootstrapSlotError("claim: artifact must be an object")
    normalized_artifact = _validate_artifact(slot, artifact)

    return {
        "artifact": normalized_artifact,
        "environment": environment,
        "mode": "bootstrap",
        "operation_id": operation_id,
        "repository_id": repository_id,
        "repository_name": repository_name,
        "schema_version": CLAIM_SCHEMA_VERSION,
        "slot": slot,
        "workflow_id": f"github-actions:{repository_id}:{workflow_path}",
        "workflow_path": workflow_path,
        "workflow_ref": workflow_ref,
        "workflow_run_attempt": workflow_run_attempt,
        "workflow_run_id": workflow_run_id,
    }


def claim_bootstrap_slot(
    s3_client: Any,
    *,
    bucket_name: str,
    slot: str,
    repository_id: str,
    repository_name: str,
    workflow_path: str,
    workflow_ref: str,
    workflow_run_id: int,
    workflow_run_attempt: int,
    operation_id: str,
    environment: str,
    artifact: Mapping[str, Any],
) -> ClaimReceipt:
    """Verify controls and an empty fixed key, then atomically consume its slot.

    This function never retries a failed or ambiguous conditional write. A failed
    response may follow a committed S3 write, so the caller must treat the slot as
    consumed unless a separately authorized fresh run re-proves an empty key.
    """

    claim = build_claim_document(
        slot=slot,
        repository_id=repository_id,
        repository_name=repository_name,
        workflow_path=workflow_path,
        workflow_ref=workflow_ref,
        workflow_run_id=workflow_run_id,
        workflow_run_attempt=workflow_run_attempt,
        operation_id=operation_id,
        environment=environment,
        artifact=artifact,
    )
    key, _expected_environment, _artifact_fields = _SLOT_CONFIG[slot]
    body = json.dumps(
        claim, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    verify_bucket_controls(s3_client, bucket_name=bucket_name)
    page_count = read_empty_slot(s3_client, bucket_name=bucket_name, slot=slot)
    try:
        response = s3_client.put_object(
            Bucket=bucket_name,
            Key=key,
            Body=body,
            ContentType="application/json",
            # Object Lock uploads require Content-MD5 or an SDK checksum header.
            # This digest is an upload-integrity check, not an authenticity claim.
            ContentMD5=base64.b64encode(
                hashlib.md5(body, usedforsecurity=False).digest()
            ).decode("ascii"),
            IfNoneMatch="*",
        )
    except Exception:
        # Do not expose SDK messages and do not automatically retry an ambiguous write.
        raise BootstrapSlotError(
            "claim write failed or was ambiguous; slot may be consumed"
        ) from None
    version_id = response.get("VersionId") if isinstance(response, Mapping) else None
    if not isinstance(version_id, str) or not version_id or version_id == "null":
        raise BootstrapSlotError(
            "claim write returned no immutable version id; slot may be consumed"
        )
    return ClaimReceipt(
        slot=slot,
        key=key,
        version_id=version_id,
        claim_sha256=hashlib.sha256(body).hexdigest(),
        version_pages_read=page_count,
    )
