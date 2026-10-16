"""Canonical, fail-closed signing rules for ASAAS producer v1 envelopes.

The billing schemas intentionally use the JSON subset whose RFC 8785 JCS
serialization is unambiguous here: object keys are schema-defined ASCII,
integers are within the interoperable IEEE-754 range, and floating point
values are forbidden. Signers are injected so local tests never contact AWS.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils

ALGORITHM = "ECDSA_SHA_256"
ENVIRONMENT = "staging"
OPERATIONAL_ROLE = "production-serving"
SCHEMA_VERSION = "v1"
CLUSTER_ARN = "arn:aws:ecs:us-east-1:388460142431:cluster/neurostack-staging"
SERVICE_ARN = (
    "arn:aws:ecs:us-east-1:388460142431:service/"
    "neurostack-staging/neurostack-staging-app"
)
HEADER_FIELDS = (
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
ENVELOPE_FIELDS = frozenset((*HEADER_FIELDS, "payload", "signature"))
PURPOSE_BY_TYPE = {
    "billing_producer_assignment.v1": "billing-producer-grant.v1",
    "billing_producer_authorization.v1": "billing-producer-grant.v1",
    "billing_producer_grant_revocation.v1": "billing-producer-grant.v1",
    "billing_build_attestation.v1": "billing-build-attestation.v1",
    "billing_producer_deployed.v1": "billing-producer-receipt.v1",
    "billing_producer_deploy_verified.v1": "billing-producer-receipt.v1",
}
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_BASE64URL = re.compile(r"^[A-Za-z0-9_-]+$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SAFE_INTEGER = (1 << 53) - 1


class SignedEnvelopeRejected(ValueError):
    """An envelope failed canonical schema, trust, or signature validation."""


class DigestSigner(Protocol):
    """Signer interface for an already SHA-256-hashed JCS header."""

    def sign_digest(self, key_arn: str, digest: bytes) -> bytes: ...


@dataclass(frozen=True)
class TrustEntry:
    """One exact issuer/key/schema tuple and its independently pinned public key."""

    issuer: str
    kid: str
    environment: str
    audience: str
    type: str
    purpose: str
    schema_version: str
    algorithm: str
    public_key_pem: str
    repository_id: str | None = None
    repository_name: str | None = None
    workflow_path: str | None = None
    workflow_ref: str | None = None
    oidc_issuer: str | None = None
    oidc_subject: str | None = None

    def __post_init__(self) -> None:
        if self.type not in PURPOSE_BY_TYPE:
            raise SignedEnvelopeRejected("trust entry type is not allowlisted")
        if self.environment != ENVIRONMENT:
            raise SignedEnvelopeRejected("trust entry environment is not allowlisted")
        if self.purpose != PURPOSE_BY_TYPE[self.type]:
            raise SignedEnvelopeRejected("trust entry purpose does not match type")
        if self.schema_version != SCHEMA_VERSION or self.algorithm != ALGORITHM:
            raise SignedEnvelopeRejected(
                "trust entry schema or algorithm is unsupported"
            )
        if not re.fullmatch(
            r"arn:aws:kms:[a-z0-9-]+:[0-9]{12}:key/[A-Za-z0-9-]+", self.kid
        ):
            raise SignedEnvelopeRejected("trust entry KMS key ARN is invalid")
        if not self.issuer or not self.audience or not self.public_key_pem:
            raise SignedEnvelopeRejected("trust entry has incomplete signing pins")

    def tuple_key(self) -> tuple[str, ...]:
        return (
            self.issuer,
            self.kid,
            self.environment,
            self.audience,
            self.type,
            self.purpose,
            self.schema_version,
            self.algorithm,
        )


class TrustRegistry:
    """Exact tuple lookup; callers cannot provide key URLs or select a key."""

    def __init__(self, entries: list[TrustEntry] | tuple[TrustEntry, ...] = ()):
        self._entries: dict[tuple[str, ...], TrustEntry] = {}
        for entry in entries:
            if entry.tuple_key() in self._entries:
                raise SignedEnvelopeRejected("duplicate signing trust tuple")
            self._entries[entry.tuple_key()] = entry

    def resolve(self, envelope: Mapping[str, Any]) -> TrustEntry:
        try:
            key = tuple(
                str(envelope[field])
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
        except (KeyError, TypeError):
            raise SignedEnvelopeRejected("signing trust tuple is incomplete") from None
        entry = self._entries.get(key)
        if entry is None:
            raise SignedEnvelopeRejected("signing trust tuple is not allowlisted")
        return entry


def _sort_key(value: str) -> bytes:
    """RFC 8785 sorts property names by UTF-16 code units."""

    try:
        return value.encode("utf-16be", errors="strict")
    except UnicodeEncodeError:
        raise SignedEnvelopeRejected("invalid Unicode property name") from None


def _validate_jcs_value(value: Any) -> None:
    if value is None or isinstance(value, (str, bool)):
        if isinstance(value, str):
            try:
                value.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                raise SignedEnvelopeRejected("invalid Unicode string") from None
        return
    if isinstance(value, int) and not isinstance(value, bool):
        if abs(value) > _SAFE_INTEGER:
            raise SignedEnvelopeRejected("integer is outside the JCS safe range")
        return
    if isinstance(value, float):
        raise SignedEnvelopeRejected("floating point values are not allowed")
    if isinstance(value, list):
        for item in value:
            _validate_jcs_value(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise SignedEnvelopeRejected("object property names must be strings")
            _sort_key(key)
            _validate_jcs_value(item)
        return
    raise SignedEnvelopeRejected("value is outside the canonical JSON domain")


def jcs(value: Any) -> bytes:
    """Serialize the closed billing JSON schema subset as RFC 8785 UTF-8."""

    _validate_jcs_value(value)

    def canonicalize(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: canonicalize(item[key]) for key in sorted(item, key=_sort_key)}
        if isinstance(item, list):
            return [canonicalize(child) for child in item]
        return item

    ordered = canonicalize(value)
    try:
        return json.dumps(
            ordered,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError):
        raise SignedEnvelopeRejected("document cannot be serialized as JCS") from None


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def payload_digest(payload: Mapping[str, Any]) -> str:
    return sha256_hex(jcs(dict(payload)))


def envelope_digest(envelope: Mapping[str, Any]) -> str:
    validate_envelope_shape(envelope)
    return sha256_hex(jcs(dict(envelope)))


def header_for(envelope: Mapping[str, Any]) -> dict[str, Any]:
    return {field: envelope[field] for field in HEADER_FIELDS}


def validate_envelope_shape(envelope: Mapping[str, Any]) -> None:
    if set(envelope) != ENVELOPE_FIELDS:
        raise SignedEnvelopeRejected("envelope must contain exactly 15 top-level keys")
    if not isinstance(envelope.get("payload"), dict):
        raise SignedEnvelopeRejected("envelope payload must be an object")
    if "unsigned_header" in envelope:
        raise SignedEnvelopeRejected("nested unsigned_header is forbidden")
    for field in HEADER_FIELDS:
        if not isinstance(envelope.get(field), str) and field != "request_id":
            raise SignedEnvelopeRejected(f"envelope {field} must be a string")
    if envelope.get("request_id") is not None and not isinstance(
        envelope.get("request_id"), str
    ):
        raise SignedEnvelopeRejected("envelope request_id must be null or a UUID")
    if not isinstance(envelope.get("signature"), str):
        raise SignedEnvelopeRejected("envelope signature must be a string")
    if envelope.get("algorithm") != ALGORITHM:
        raise SignedEnvelopeRejected("unsupported signing algorithm")
    if envelope.get("environment") != ENVIRONMENT:
        raise SignedEnvelopeRejected("envelope environment is not allowlisted")
    if envelope.get("schema_version") != SCHEMA_VERSION:
        raise SignedEnvelopeRejected("unsupported envelope schema version")
    if PURPOSE_BY_TYPE.get(str(envelope.get("type"))) != envelope.get("purpose"):
        raise SignedEnvelopeRejected("envelope purpose does not match its type")
    if not _HEX_64.fullmatch(str(envelope.get("payload_digest", ""))):
        raise SignedEnvelopeRejected("payload digest must be lowercase SHA-256 hex")
    if payload_digest(envelope["payload"]) != envelope["payload_digest"]:
        raise SignedEnvelopeRejected("payload digest does not match payload")
    _validate_type_payload(str(envelope["type"]), envelope["payload"])
    payload = envelope["payload"]
    if (
        "operation_id" in payload
        and payload["operation_id"] != envelope["operation_id"]
    ):
        raise SignedEnvelopeRejected("header and payload operation IDs differ")
    discriminator = {
        "billing_producer_assignment.v1": "grant_kind",
        "billing_producer_authorization.v1": "grant_kind",
        "billing_producer_grant_revocation.v1": "revocation_type",
        "billing_build_attestation.v1": "attestation_type",
        "billing_producer_deployed.v1": "receipt_type",
        "billing_producer_deploy_verified.v1": "receipt_type",
    }.get(str(envelope["type"]))
    if discriminator == "grant_kind":
        expected_kind = (
            "assignment"
            if envelope["type"].endswith("assignment.v1")
            else "authorization"
        )
        if payload.get("grant_kind") != expected_kind:
            raise SignedEnvelopeRejected(
                "grant discriminator does not match envelope type"
            )
    elif discriminator and payload.get(discriminator) != envelope["type"]:
        raise SignedEnvelopeRejected("header type and payload discriminator differ")
    for field in ("issued_at", "expires_at"):
        if field in payload and payload[field] != envelope[field]:
            raise SignedEnvelopeRejected(f"header and payload {field} differ")
    if envelope.get("request_id") is not None and not _is_uuid(envelope["request_id"]):
        raise SignedEnvelopeRejected("request_id must be a canonical UUID")
    if not _is_uuid(envelope.get("operation_id")):
        raise SignedEnvelopeRejected("operation_id must be a canonical UUID")
    _parse_utc_timestamp(envelope["issued_at"])
    _parse_utc_timestamp(envelope["expires_at"])
    if envelope["expires_at"] <= envelope["issued_at"]:
        raise SignedEnvelopeRejected("envelope expiry must follow issue time")
    issued_at = _parse_utc_timestamp(envelope["issued_at"])
    expires_at = _parse_utc_timestamp(envelope["expires_at"])
    if (
        envelope["type"]
        in {
            "billing_producer_assignment.v1",
            "billing_producer_authorization.v1",
            "billing_producer_grant_revocation.v1",
            "billing_build_attestation.v1",
            "billing_producer_deployed.v1",
        }
        and envelope["request_id"] is not None
    ):
        raise SignedEnvelopeRejected("preparation envelope request_id must be null")
    if (
        envelope["type"] == "billing_producer_deploy_verified.v1"
        and envelope["request_id"] is None
    ):
        raise SignedEnvelopeRejected("verified receipt request_id must be a UUID")
    max_ttl = None
    if envelope["type"] in {
        "billing_producer_assignment.v1",
        "billing_producer_authorization.v1",
        "billing_producer_grant_revocation.v1",
    }:
        max_ttl = 600
        if (
            envelope["type"] == "billing_producer_assignment.v1"
            and envelope["payload"].get("action") == "read_billing_producer_pair"
        ):
            max_ttl = 300
    elif envelope["type"] in {
        "billing_producer_deployed.v1",
        "billing_producer_deploy_verified.v1",
    }:
        max_ttl = 7 * 24 * 60 * 60
    if max_ttl is not None and (expires_at - issued_at).total_seconds() > max_ttl:
        raise SignedEnvelopeRejected("envelope validity exceeds the type limit")
    _validate_jcs_value(dict(envelope))


def _is_uuid(value: Any) -> bool:
    if not isinstance(value, str) or not _UUID.fullmatch(value):
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _parse_utc_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SignedEnvelopeRejected("timestamp must be RFC 3339 UTC ending in Z")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise SignedEnvelopeRejected("timestamp is invalid") from None
    if parsed.tzinfo != timezone.utc or parsed.utcoffset() != timezone.utc.utcoffset(
        parsed
    ):
        raise SignedEnvelopeRejected("timestamp must use UTC")
    return parsed


def _exact_payload(payload: Mapping[str, Any], fields: set[str], label: str) -> None:
    if set(payload) != fields:
        raise SignedEnvelopeRejected(f"{label} payload fields are not exact")


def _validate_supersedes(value: Any) -> None:
    fields = {
        "receipt_id",
        "receipt_digest",
        "verification_receipt_id",
        "verification_receipt_digest",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise SignedEnvelopeRejected("supersedes must contain the complete prior pair")
    _require_uuid(value["receipt_id"], "superseded receipt_id")
    _require_uuid(
        value["verification_receipt_id"], "superseded verification_receipt_id"
    )
    _require_digest(value["receipt_digest"], "superseded receipt_digest")
    _require_digest(
        value["verification_receipt_digest"], "superseded verification_receipt_digest"
    )


def _validate_type_payload(type_name: str, payload: Mapping[str, Any]) -> None:
    if type_name not in PURPOSE_BY_TYPE:
        raise SignedEnvelopeRejected("unsupported envelope type")
    if type_name == "billing_build_attestation.v1":
        fields = {
            "attestation_type",
            "attestation_id",
            "repository_id",
            "repository_name",
            "workflow_path",
            "workflow_ref",
            "workflow_run_id",
            "app_image_digest",
            "bundle_digest",
            "commit_sha",
            "tree_sha",
            "workflow_id",
            "immutable_artifact_reference",
        }
        _exact_payload(payload, fields, "build attestation")
        _require_uuid(payload["attestation_id"], "attestation_id")
        if not isinstance(payload["repository_id"], str) or not re.fullmatch(
            r"[1-9][0-9]*", payload["repository_id"]
        ):
            raise SignedEnvelopeRejected(
                "repository_id must be a positive decimal string"
            )
        if not isinstance(payload["repository_name"], str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", payload["repository_name"]
        ):
            raise SignedEnvelopeRejected("repository_name is invalid")
        if not isinstance(payload["workflow_path"], str) or not re.fullmatch(
            r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", payload["workflow_path"]
        ):
            raise SignedEnvelopeRejected("workflow_path is invalid")
        if (
            not isinstance(payload["workflow_ref"], str)
            or not re.fullmatch(
                r"refs/(heads|tags)/[A-Za-z0-9._/-]+", payload["workflow_ref"]
            )
            or any(
                part in {"", ".", ".."} for part in payload["workflow_ref"].split("/")
            )
        ):
            raise SignedEnvelopeRejected("workflow_ref is invalid")
        if (
            type(payload["workflow_run_id"]) is not int
            or payload["workflow_run_id"] <= 0
        ):
            raise SignedEnvelopeRejected("workflow_run_id must be a positive integer")
        digest = payload["app_image_digest"]
        if not isinstance(digest, str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", digest
        ):
            raise SignedEnvelopeRejected("app image digest is invalid")
        for name in ("bundle_digest", "commit_sha", "tree_sha"):
            if not isinstance(payload[name], str) or not _HEX_64.fullmatch(
                payload[name]
            ):
                raise SignedEnvelopeRejected(f"{name} is invalid")
        expected_workflow_id = (
            f"github-actions:{payload['repository_id']}:{payload['workflow_path']}"
        )
        if payload["workflow_id"] != expected_workflow_id:
            raise SignedEnvelopeRejected(
                "workflow_id is not derived from trusted provenance"
            )
        artifact = payload["immutable_artifact_reference"]
        if (
            not isinstance(artifact, str)
            or not re.fullmatch(
                r"[0-9]{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com/[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}",
                artifact,
            )
            or not artifact.endswith(f"@{digest}")
        ):
            raise SignedEnvelopeRejected(
                "immutable artifact reference must pin the app digest"
            )
        return

    if type_name in {
        "billing_producer_assignment.v1",
        "billing_producer_authorization.v1",
    }:
        fields = {
            "id",
            "grant_kind",
            "issuer_user_id",
            "executor_user_id",
            "operation_id",
            "environment",
            "operational_role",
            "cluster_arn",
            "service_arn",
            "action",
            "receipt_id",
            "verification_receipt_id",
            "scope",
            "scope_digest",
            "issued_at",
            "expires_at",
        }
        if type_name == "billing_producer_authorization.v1":
            fields |= {"assignment_id", "assignment_digest", "approver_user_id"}
        _exact_payload(payload, fields, "producer grant")
        for name in ("id", "issuer_user_id", "executor_user_id", "operation_id"):
            _require_uuid(payload[name], name)
        if type_name.endswith("authorization.v1"):
            for name in ("assignment_id", "approver_user_id"):
                _require_uuid(payload[name], name)
            _require_digest(payload["assignment_digest"], "assignment_digest")
        if (
            payload["environment"] != ENVIRONMENT
            or payload["operational_role"] != "production-serving"
            or payload["cluster_arn"] != CLUSTER_ARN
            or payload["service_arn"] != SERVICE_ARN
        ):
            raise SignedEnvelopeRejected(
                "grant target is not the fixed staging service"
            )
        if payload["action"] not in {
            "read_billing_producer_pair",
            "create_billing_producer_deploy_receipt",
            "verify_billing_producer_deploy_receipt",
        }:
            raise SignedEnvelopeRejected("grant action is not allowlisted")
        if payload["grant_kind"] != (
            "assignment" if type_name.endswith("assignment.v1") else "authorization"
        ):
            raise SignedEnvelopeRejected("grant_kind does not match type")
        if payload["issuer_user_id"] == payload["executor_user_id"]:
            raise SignedEnvelopeRejected("issuer and executor must be distinct")
        if type_name.endswith("authorization.v1") and (
            payload["approver_user_id"] == payload["issuer_user_id"]
            or payload["approver_user_id"] == payload["executor_user_id"]
        ):
            raise SignedEnvelopeRejected(
                "approver and issuer/executor must be distinct"
            )
        if payload["receipt_id"] is not None:
            _require_uuid(payload["receipt_id"], "receipt_id")
        if payload["verification_receipt_id"] is not None:
            _require_uuid(payload["verification_receipt_id"], "verification_receipt_id")
        if (payload["action"] == "read_billing_producer_pair") != (
            payload["receipt_id"] is None and payload["verification_receipt_id"] is None
        ):
            raise SignedEnvelopeRejected("grant receipt IDs do not match its action")
        if (
            payload["receipt_id"] is not None
            and payload["receipt_id"] == payload["verification_receipt_id"]
        ):
            raise SignedEnvelopeRejected("grant receipt IDs must be distinct")
        _require_digest(payload["scope_digest"], "scope_digest")
        if sha256_hex(jcs(payload["scope"])) != payload["scope_digest"]:
            raise SignedEnvelopeRejected("scope digest does not match scope")
        _validate_grant_scope(payload)
        return

    if type_name == "billing_producer_grant_revocation.v1":
        fields = {
            "revocation_type",
            "revocation_id",
            "operation_id",
            "grant_type",
            "grant_id",
            "grant_digest",
            "revoker_user_id",
            "reason_code",
            "revoked_at",
        }
        _exact_payload(payload, fields, "grant revocation")
        for name in ("revocation_id", "operation_id", "grant_id", "revoker_user_id"):
            _require_uuid(payload[name], name)
        if payload["grant_type"] not in {"assignment", "authorization"}:
            raise SignedEnvelopeRejected("grant_type is invalid")
        if payload["reason_code"] not in {
            "operator_requested",
            "identity_or_scope_error",
            "key_compromise",
            "superseded",
            "incident_response",
        }:
            raise SignedEnvelopeRejected("revocation reason is invalid")
        _require_digest(payload["grant_digest"], "grant_digest")
        _parse_utc_timestamp(payload["revoked_at"])
        return

    if type_name == "billing_producer_deployed.v1":
        fields = {
            "operation_id",
            "receipt_id",
            "verification_receipt_id",
            "receipt_type",
            "executor_user_id",
            "environment",
            "operational_role",
            "readback",
            "readback_digest",
            "build_attestation_id",
            "build_attestation_digest",
            "commit_sha",
            "tree_sha",
            "workflow_id",
            "supersedes",
        }
        _exact_payload(payload, fields, "deploy receipt")
        for name in (
            "operation_id",
            "receipt_id",
            "verification_receipt_id",
            "executor_user_id",
            "build_attestation_id",
        ):
            _require_uuid(payload[name], name)
        _require_digest(payload["readback_digest"], "readback_digest")
        _require_digest(payload["build_attestation_digest"], "build_attestation_digest")
        for name in ("commit_sha", "tree_sha"):
            _require_digest(payload[name], name)
        if (
            payload["receipt_type"] != type_name
            or payload["environment"] != ENVIRONMENT
            or payload["operational_role"] != "production-serving"
        ):
            raise SignedEnvelopeRejected(
                "deploy receipt discriminator or target is invalid"
            )
        if sha256_hex(jcs(payload["readback"])) != payload["readback_digest"]:
            raise SignedEnvelopeRejected("readback digest does not match readback")
        if payload["supersedes"] is not None:
            _validate_supersedes(payload["supersedes"])
        return

    if type_name == "billing_producer_deploy_verified.v1":
        fields = {
            "operation_id",
            "receipt_type",
            "receipt_id",
            "source_receipt_id",
            "source_receipt_digest",
            "environment",
            "operational_role",
            "cluster_arn",
            "service_arn",
            "deployment_id",
            "task_set_id",
            "config_generation",
            "compatibility_generation",
            "app_digest",
            "bundle_digest",
            "readback_digest",
            "build_attestation_id",
            "build_attestation_digest",
            "commit_sha",
            "tree_sha",
            "workflow_id",
            "attestor_user_id",
            "verifier_user_id",
            "supersedes",
            "issued_at",
            "expires_at",
        }
        _exact_payload(payload, fields, "verified receipt")
        for name in (
            "operation_id",
            "receipt_id",
            "source_receipt_id",
            "build_attestation_id",
            "attestor_user_id",
            "verifier_user_id",
        ):
            _require_uuid(payload[name], name)
        for name in (
            "source_receipt_digest",
            "bundle_digest",
            "readback_digest",
            "build_attestation_digest",
            "commit_sha",
            "tree_sha",
        ):
            _require_digest(payload[name], name)
        if not isinstance(payload["app_digest"], str) or not re.fullmatch(
            r"sha256:[0-9a-f]{64}", payload["app_digest"]
        ):
            raise SignedEnvelopeRejected("verified receipt app digest is invalid")
        if (
            payload["receipt_type"] != type_name
            or payload["environment"] != ENVIRONMENT
            or payload["operational_role"] != "production-serving"
        ):
            raise SignedEnvelopeRejected(
                "verified receipt discriminator or target is invalid"
            )
        if payload["attestor_user_id"] == payload["verifier_user_id"]:
            raise SignedEnvelopeRejected("attestor and verifier must be distinct")
        if payload["supersedes"] is not None:
            _validate_supersedes(payload["supersedes"])
        _parse_utc_timestamp(payload["issued_at"])
        _parse_utc_timestamp(payload["expires_at"])


def _require_uuid(value: Any, name: str) -> None:
    if not _is_uuid(value):
        raise SignedEnvelopeRejected(f"{name} must be a canonical UUID")


def _require_digest(value: Any, name: str) -> None:
    if not isinstance(value, str) or not _HEX_64.fullmatch(value):
        raise SignedEnvelopeRejected(f"{name} must be lowercase SHA-256 hex")


def _validate_grant_scope(payload: Mapping[str, Any]) -> None:
    scope = payload.get("scope")
    action = payload.get("action")
    if not isinstance(scope, dict):
        raise SignedEnvelopeRejected("grant scope must be an object")
    if action == "read_billing_producer_pair":
        expected = {
            "operation_id",
            "action",
            "environment",
            "operational_role",
            "cluster_arn",
            "service_arn",
            "readback_digest",
            "minimum_grant_validity_seconds",
            "minimum_pair_validity_seconds",
        }
        _exact_payload(scope, expected, "read grant scope")
        _require_digest(scope["readback_digest"], "readback_digest")
        if type(scope["minimum_grant_validity_seconds"]) is not int or not (
            60 <= scope["minimum_grant_validity_seconds"] <= 300
        ):
            raise SignedEnvelopeRejected("minimum grant validity is invalid")
        if type(scope["minimum_pair_validity_seconds"]) is not int or (
            scope["minimum_pair_validity_seconds"] != 300
        ):
            raise SignedEnvelopeRejected("minimum pair validity is invalid")
        if any(
            scope.get(field) != payload.get(field)
            for field in (
                "operation_id",
                "action",
                "environment",
                "operational_role",
                "cluster_arn",
                "service_arn",
            )
        ):
            raise SignedEnvelopeRejected("read scope differs from its grant")
        return
    if action == "create_billing_producer_deploy_receipt":
        expected = {
            "operation_id",
            "action",
            "environment",
            "operational_role",
            "cluster_arn",
            "service_arn",
            "receipt_id",
            "verification_receipt_id",
            "supersedes",
        }
        _exact_payload(scope, expected, "create grant scope")
        for name in ("operation_id", "receipt_id", "verification_receipt_id"):
            _require_uuid(scope[name], name)
        if scope["receipt_id"] == scope["verification_receipt_id"]:
            raise SignedEnvelopeRejected("create receipt IDs must be distinct")
        if scope["supersedes"] is not None:
            _validate_supersedes(scope["supersedes"])
        if any(
            scope.get(field) != payload.get(field)
            for field in (
                "operation_id",
                "action",
                "environment",
                "operational_role",
                "cluster_arn",
                "service_arn",
                "receipt_id",
                "verification_receipt_id",
            )
        ):
            raise SignedEnvelopeRejected("create scope differs from its grant")
        return
    if action == "verify_billing_producer_deploy_receipt":
        expected = {
            "operation_id",
            "receipt_id",
            "receipt_digest",
            "verification_receipt_id",
            "readback_digest",
            "supersedes",
        }
        _exact_payload(scope, expected, "verify grant scope")
        for name in ("operation_id", "receipt_id", "verification_receipt_id"):
            _require_uuid(scope[name], name)
        for name in ("receipt_digest", "readback_digest"):
            _require_digest(scope[name], name)
        if scope["supersedes"] is not None:
            _validate_supersedes(scope["supersedes"])
        if any(
            scope.get(field) != payload.get(field)
            for field in ("operation_id", "receipt_id", "verification_receipt_id")
        ):
            raise SignedEnvelopeRejected("verify scope differs from its grant")
        return
    raise SignedEnvelopeRejected("grant scope action is unsupported")


def timestamp_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SignedEnvelopeRejected("timestamp must be timezone-aware")
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def build_envelope(
    *,
    entry: TrustEntry,
    payload: Mapping[str, Any],
    operation_id: UUID,
    issued_at: datetime,
    expires_at: datetime,
    signer: DigestSigner,
    request_id: UUID | None = None,
) -> dict[str, Any]:
    type_name = entry.type
    header = {
        "issuer": entry.issuer,
        "kid": entry.kid,
        "algorithm": entry.algorithm,
        "audience": entry.audience,
        "type": type_name,
        "purpose": entry.purpose,
        "schema_version": entry.schema_version,
        "environment": entry.environment,
        "operation_id": str(operation_id),
        "request_id": str(request_id) if request_id is not None else None,
        "issued_at": timestamp_utc(issued_at),
        "expires_at": timestamp_utc(expires_at),
        "payload_digest": payload_digest(payload),
    }
    document: dict[str, Any] = {**header, "payload": dict(payload)}
    digest = hashlib.sha256(jcs(header)).digest()
    signature = signer.sign_digest(entry.kid, digest)
    document["signature"] = (
        base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
    )
    validate_envelope_shape(document)
    return document


def verify_envelope(
    envelope: Mapping[str, Any],
    registry: TrustRegistry,
    *,
    expected_type: str | None = None,
) -> TrustEntry:
    validate_envelope_shape(envelope)
    if expected_type is not None and envelope["type"] != expected_type:
        raise SignedEnvelopeRejected("unexpected envelope type")
    entry = registry.resolve(envelope)
    if entry.algorithm != ALGORITHM or not entry.public_key_pem:
        raise SignedEnvelopeRejected("trusted verification key is unavailable")
    signature_text = str(envelope["signature"])
    if not _BASE64URL.fullmatch(signature_text):
        raise SignedEnvelopeRejected("signature is not unpadded base64url")
    try:
        signature = base64.urlsafe_b64decode(
            signature_text + "=" * ((4 - len(signature_text) % 4) % 4)
        )
        if (
            base64.urlsafe_b64encode(signature).decode("ascii").rstrip("=")
            != signature_text
        ):
            raise ValueError("non-canonical base64url")
        key = serialization.load_pem_public_key(entry.public_key_pem.encode("ascii"))
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise ValueError("untrusted key type")
        r, s = utils.decode_dss_signature(signature)
        order = int(
            "FFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551", 16
        )
        if not (1 <= r < order and 1 <= s < order):
            raise ValueError("ECDSA values are out of range")
        if utils.encode_dss_signature(r, s) != signature:
            raise ValueError("ECDSA signature is not canonical DER")
        header_digest = hashlib.sha256(jcs(header_for(envelope))).digest()
        key.verify(
            signature,
            header_digest,
            ec.ECDSA(utils.Prehashed(hashes.SHA256())),
        )
    except (
        ValueError,
        TypeError,
        InvalidSignature,
        binascii.Error,
        UnicodeEncodeError,
    ):
        raise SignedEnvelopeRejected("envelope signature is invalid") from None
    return entry


class Boto3KmsDigestSigner:
    """Thin KMS adapter. Construct with an injected client; never created by tests."""

    def __init__(self, kms_client: Any):
        self._kms = kms_client

    def sign_digest(self, key_arn: str, digest: bytes) -> bytes:
        if not re.fullmatch(
            r"arn:aws:kms:[a-z0-9-]+:[0-9]{12}:key/[0-9a-f-]+", key_arn
        ):
            raise SignedEnvelopeRejected("KMS key ARN is not a pinned key identifier")
        if len(digest) != hashlib.sha256().digest_size:
            raise SignedEnvelopeRejected("KMS input must be a SHA-256 digest")
        try:
            from botocore.exceptions import BotoCoreError
        except ImportError:
            raise SignedEnvelopeRejected("AWS SDK for Python is unavailable") from None
        try:
            result = self._kms.sign(
                KeyId=key_arn,
                Message=digest,
                MessageType="DIGEST",
                SigningAlgorithm="ECDSA_SHA_256",
            )
        except BotoCoreError:
            raise SignedEnvelopeRejected("trusted KMS signing failed") from None
        signature = result.get("Signature") if isinstance(result, dict) else None
        if not isinstance(signature, bytes):
            raise SignedEnvelopeRejected("trusted KMS returned no signature")
        return signature


def parse_json_no_duplicate_keys(raw: bytes) -> dict[str, Any]:
    """Parse a JSON object and reject duplicate names before Python collapses them."""

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise SignedEnvelopeRejected("duplicate JSON object key")
            result[key] = value
        return result

    try:

        def reject_float(_: str) -> None:
            raise SignedEnvelopeRejected("floating point values are not allowed")

        value = json.loads(
            raw,
            object_pairs_hook=no_duplicates,
            parse_float=reject_float,
        )
    except SignedEnvelopeRejected:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise SignedEnvelopeRejected(
            "request body is not valid canonical JSON"
        ) from None
    if not isinstance(value, dict):
        raise SignedEnvelopeRejected("request body must be an object")
    _validate_jcs_value(value)
    return value


def validate_ci_claims(entry: TrustEntry, claims: Mapping[str, Any]) -> None:
    """Validate OIDC claim values against administrator-pinned CI identity."""

    expected = {
        "iss": entry.oidc_issuer,
        "aud": entry.audience,
        "sub": entry.oidc_subject,
        "repository_id": entry.repository_id,
        "repository": entry.repository_name,
        "workflow_ref": entry.workflow_ref,
    }
    if any(value is None for value in expected.values()):
        raise SignedEnvelopeRejected("CI OIDC trust pins are incomplete")
    if any(claims.get(name) != value for name, value in expected.items()):
        raise SignedEnvelopeRejected("CI OIDC claims do not match pinned trust")


def validate_attestation_trust(entry: TrustEntry, envelope: Mapping[str, Any]) -> None:
    """Bind the signed build payload to all administrator-pinned repository data."""

    if entry.type != "billing_build_attestation.v1":
        raise SignedEnvelopeRejected("attestation trust entry has the wrong type")
    expected = {
        "repository_id": entry.repository_id,
        "repository_name": entry.repository_name,
        "workflow_path": entry.workflow_path,
        "workflow_ref": entry.workflow_ref,
    }
    if any(value is None or not value for value in expected.values()):
        raise SignedEnvelopeRejected("administrator CI provenance pins are incomplete")
    payload = envelope["payload"]
    if any(payload.get(field) != value for field, value in expected.items()):
        raise SignedEnvelopeRejected("build provenance differs from pinned repository")
    if not entry.oidc_issuer or not entry.oidc_subject:
        raise SignedEnvelopeRejected(
            "administrator OIDC issuer/subject pins are incomplete"
        )
    if (
        payload.get("workflow_id")
        != f"github-actions:{entry.repository_id}:{entry.workflow_path}"
    ):
        raise SignedEnvelopeRejected(
            "build workflow identity is not derived from pinned trust"
        )
