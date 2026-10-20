#!/usr/bin/env python3
"""Fail-closed Group 5 attestation, deployment, and readback validator.

The AWS CLI readbacks supplied to this tool are private runner-temporary JSON.
Only a fixed allowlist of non-secret fields is copied to the 0600 evidence file.
It never prints raw AWS responses, task environment values, tokens, or URLs.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import ipaddress
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

# Running this file directly sets sys.path[0] to scripts/; keep imports stable
# for both the CLI workflow and repository-root test invocation.
_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from scripts.asaas_database_secret_reference import (  # noqa: E402
    ReferenceError,
    resolve_active_database_secret,
)
from src.services.billing_signed_envelope import (  # noqa: E402
    SignedEnvelopeRejected,
    TrustEntry,
    TrustRegistry,
    envelope_digest,
    validate_attestation_trust,
    verify_envelope,
)

CLUSTER_ARN = "arn:aws:ecs:us-east-1:388460142431:cluster/neurostack-staging"
SERVICE_ARN = "arn:aws:ecs:us-east-1:388460142431:service/neurostack-staging/neurostack-staging-app"
SERVICE_NAME = "neurostack-staging-app"
TASK_FAMILY = "neurostack-staging-app"
APP_CONTAINER = "app"
REPOSITORY = "neurostackinfra/neurostack-api"
REPOSITORY_ID = "1152335815"
BUILD_WORKFLOW_PATH = ".github/workflows/asaas-build-attestation.yml"
BUILD_WORKFLOW_REF = "refs/heads/main"
DEPLOY_WORKFLOW_PATH = ".github/workflows/asaas-producer-image-deploy.yml"
TRUST_SCHEMA = "billing_producer_build_trust_readback.v1"
TRUST_FIELDS = {
    "schema",
    "schema_version",
    "environment",
    "type",
    "purpose",
    "algorithm",
    "issuer",
    "kid",
    "audience",
    "public_key_fingerprint_sha256",
    "repository_id",
    "repository_name",
    "workflow_path",
    "workflow_ref",
    "oidc_issuer",
    "oidc_subject",
}
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARN = re.compile(
    r"^arn:aws:secretsmanager:us-east-1:388460142431:secret:[A-Za-z0-9/_+=.@-]+$"
)
ECR_REGISTRY = "388460142431.dkr.ecr.us-east-1.amazonaws.com"
# Path A (docs/runbooks/asaas-deployment.md, F1): the serving revision, and the
# protected template captured from it, may run a hand-built bridge image from
# this repository, pinned by digest. It is only ever a deployment source; the
# attested target is always another repository (the producer repository).
SOURCE_IMAGE_REPOSITORY = f"{ECR_REGISTRY}/neurostack-staging"
# The ECR repository-name grammar: lowercase alphanumeric components joined by
# a single ".", "_" or "-", and path segments joined by a single "/"; no
# leading, trailing or doubled separator.
_ECR_REPOSITORY_NAME = r"(?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*[a-z0-9]+(?:[._-][a-z0-9]+)*"
_ECR_REPOSITORY_URI = re.compile(
    r"388460142431\.dkr\.ecr\.us-east-1\.amazonaws\.com/" + _ECR_REPOSITORY_NAME
)
_SOURCE_IMAGE_REFERENCE = re.compile(
    r"388460142431\.dkr\.ecr\.us-east-1\.amazonaws\.com/neurostack-staging"
    r"@sha256:[0-9a-f]{64}"
)
_TARGET_IMAGE_SUFFIX = re.compile(
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?(?:@sha256:[0-9a-f]{64})?"
)
# The six revision 8 app environment names whose values are fingerprinted.
BASE_ENVIRONMENT_NAMES = frozenset(
    {"ENVIRONMENT", "HOST", "LLM_MODEL", "LOG_FORMAT", "PORT", "QDRANT_HOST"}
)
# Exactly the Group 9 D3 wiring: infra/production/asaas-production-wiring.yaml
# task_definition.app_environment_added and app_secrets_added (the same names as
# ADDED_ENVIRONMENT and ADDED_SECRETS in scripts/asaas_production_wiring.py; a
# test pins all three). The D3 environment values are not secret and are
# fingerprinted like the base values; the secrets are ARN references only.
D3_ENVIRONMENT_NAMES = frozenset(
    {
        "ASAAS_CHECKOUT_RETURN_URL",
        "ASAAS_WEBHOOK_NOTIFICATION_EMAIL",
        "ASAAS_WEBHOOK_URL",
        "BILLING_FACT_WORKER_ENABLED",
    }
)
D3_SECRET_NAMES = frozenset(
    {"ASAAS_API_KEY", "ASAAS_WEBHOOK_TOKEN", "BILLING_OBSERVABILITY_SMOKE_TOKEN"}
)
FINGERPRINTED_ENVIRONMENT_NAMES = BASE_ENVIRONMENT_NAMES | D3_ENVIRONMENT_NAMES
# A template or serving revision carries the D3 wiring completely or not at all.
ENVIRONMENT_NAME_SETS = (BASE_ENVIRONMENT_NAMES, FINGERPRINTED_ENVIRONMENT_NAMES)


class RolloutError(ValueError):
    """A rollout artifact or deployment readback did not meet its contract."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise RolloutError(reason)


def validate_target_image_repository(target_repository: Any) -> str:
    """The attested target: an exact ECR repository URI, never the source."""

    _require(
        isinstance(target_repository, str)
        and _ECR_REPOSITORY_URI.fullmatch(target_repository) is not None
        and target_repository != SOURCE_IMAGE_REPOSITORY,
        "target_image_repository_invalid",
    )
    return str(target_repository)


def classify_app_image(reference: Any, *, target_repository: Any) -> str:
    """Classify a pre-deployment app image reference; anything else raises.

    ``"target"``: ``<target repository>[:<tag>][@sha256:<digest>]`` (the rule
    the protected path always had, now with an exact repository match).
    ``"source"``: exactly ``SOURCE_IMAGE_REPOSITORY@sha256:<64 hex>``, in this
    account and region, with no tag. Used for the protected template's image
    and the serving (before/cohort) snapshot only; the after snapshot must run
    exactly ``<target>@<attested digest>``.
    """

    target = validate_target_image_repository(target_repository)
    _require(isinstance(reference, str), "app_image_reference_invalid")
    repository = re.split(r"[:@]", reference, maxsplit=1)[0]
    if repository == SOURCE_IMAGE_REPOSITORY:
        _require(
            _SOURCE_IMAGE_REFERENCE.fullmatch(reference) is not None,
            "source_image_reference_not_digest_pinned",
        )
        return "source"
    _require(repository == target, "app_image_repository_not_allowed")
    _require(
        _TARGET_IMAGE_SUFFIX.fullmatch(reference[len(target) :]) is not None,
        "app_image_reference_invalid",
    )
    return "target"


def _strict_json(path: Path, *, max_bytes: int = 4_000_000) -> Any:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
        raise RolloutError("input_file_unreadable")

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RolloutError("input_json_duplicate_key")
            result[key] = value
        return result

    try:
        return json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=object_pairs
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RolloutError("input_json_invalid") from None


def _public_key_fingerprint(public_key_pem: str) -> str:
    _require(isinstance(public_key_pem, str), "build_public_key_pin_invalid")
    lines = public_key_pem.strip().splitlines()
    _require(
        len(lines) >= 3
        and lines[0] == "-----BEGIN PUBLIC KEY-----"
        and lines[-1] == "-----END PUBLIC KEY-----",
        "build_public_key_pin_invalid",
    )
    try:
        der = base64.b64decode("".join(lines[1:-1]), validate=True)
    except (ValueError, TypeError):
        raise RolloutError("build_public_key_pin_invalid") from None
    return hashlib.sha256(der).hexdigest()


def validate_backend_trust(raw: Any, env: dict[str, str]) -> dict[str, str]:
    _require(
        isinstance(raw, dict) and set(raw) == TRUST_FIELDS,
        "backend_trust_shape_invalid",
    )
    expected = {
        "schema": TRUST_SCHEMA,
        "schema_version": "v1",
        "environment": "staging",
        "type": "billing_build_attestation.v1",
        "purpose": "billing-build-attestation.v1",
        "algorithm": "ECDSA_SHA_256",
        "issuer": env.get("BILLING_PRODUCER_BUILD_ISSUER", ""),
        "kid": env.get("BILLING_PRODUCER_BUILD_KMS_KEY_ARN", ""),
        "audience": env.get("BILLING_PRODUCER_BUILD_AUDIENCE", ""),
        "repository_id": REPOSITORY_ID,
        "repository_name": REPOSITORY,
        "workflow_path": BUILD_WORKFLOW_PATH,
        "workflow_ref": BUILD_WORKFLOW_REF,
        "oidc_issuer": "https://token.actions.githubusercontent.com",
        "oidc_subject": env.get("BILLING_PRODUCER_BUILD_OIDC_SUBJECT", ""),
    }
    _require(
        all(
            isinstance(value, str) and value and value.strip() == value
            for value in raw.values()
        ),
        "backend_trust_incomplete",
    )
    _require(
        all(raw.get(key) == value for key, value in expected.items()),
        "backend_trust_pin_mismatch",
    )
    _require(
        raw.get("public_key_fingerprint_sha256")
        == _public_key_fingerprint(
            env.get("BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM", "")
        ),
        "backend_trust_public_key_mismatch",
    )
    return {key: raw[key] for key in sorted(TRUST_FIELDS)}


def validate_signed_attestation(
    envelope: Any,
    *,
    trust_readback: Any,
    expected_build_run_id: str,
    expected_attestation_id: str,
    expected_digest: str,
    expected_envelope_sha256: str,
    env: dict[str, str],
) -> dict[str, str]:
    _require(isinstance(envelope, dict), "signed_envelope_invalid")
    _require(
        _DIGEST.fullmatch(expected_digest) is not None, "expected_app_digest_invalid"
    )
    _require(
        _HEX64.fullmatch(expected_envelope_sha256) is not None,
        "expected_envelope_digest_invalid",
    )
    trust = validate_backend_trust(trust_readback, env)
    entry = TrustEntry(
        issuer=trust["issuer"],
        kid=trust["kid"],
        environment="staging",
        audience=trust["audience"],
        type="billing_build_attestation.v1",
        purpose="billing-build-attestation.v1",
        schema_version="v1",
        algorithm="ECDSA_SHA_256",
        public_key_pem=env["BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM"],
        repository_id=REPOSITORY_ID,
        repository_name=REPOSITORY,
        workflow_path=BUILD_WORKFLOW_PATH,
        workflow_ref=BUILD_WORKFLOW_REF,
        oidc_issuer=trust["oidc_issuer"],
        oidc_subject=trust["oidc_subject"],
    )
    try:
        verified = verify_envelope(
            envelope,
            TrustRegistry([entry]),
            expected_type="billing_build_attestation.v1",
        )
        validate_attestation_trust(verified, envelope)
        digest = envelope_digest(envelope)
    except (SignedEnvelopeRejected, KeyError, TypeError, ValueError):
        raise RolloutError("signed_envelope_signature_or_schema_invalid") from None
    payload = envelope["payload"]
    _require(digest == expected_envelope_sha256, "signed_envelope_sha256_mismatch")
    try:
        canonical_attestation_id = str(UUID(expected_attestation_id))
    except (TypeError, ValueError):
        raise RolloutError("expected_attestation_id_invalid") from None
    _require(
        canonical_attestation_id == expected_attestation_id,
        "expected_attestation_id_invalid",
    )
    _require(
        re.fullmatch(r"[1-9][0-9]*", expected_build_run_id) is not None,
        "expected_build_run_id_invalid",
    )
    _require(
        payload.get("attestation_id") == expected_attestation_id,
        "attestation_id_mismatch",
    )
    _require(
        str(payload.get("workflow_run_id")) == expected_build_run_id,
        "build_run_id_mismatch",
    )
    _require(
        payload.get("app_image_digest") == expected_digest, "app_image_digest_mismatch"
    )
    expected_image = (
        f"{env.get('BILLING_PRODUCER_ECR_REPOSITORY_URI', '')}@{expected_digest}"
    )
    _require(
        payload.get("immutable_artifact_reference") == expected_image,
        "immutable_image_reference_mismatch",
    )
    _require(
        envelope.get("operation_id") != env.get("INPUT_OPERATION_ID"),
        "operation_id_reused",
    )
    return {
        "attestation_id": expected_attestation_id,
        "attestation_digest": digest,
        "app_image_digest": expected_digest,
        "build_workflow_run_id": expected_build_run_id,
        "signed_envelope_sha256": digest,
    }


def validate_registration(
    registration: Any,
    attestation: dict[str, str],
    *,
    mode: str,
) -> dict[str, str]:
    if mode == "bootstrap":
        _require(registration is None, "bootstrap_must_not_claim_pre_registration")
        return {"registration": "pending_human_registration"}
    _require(mode == "registered", "rollout_mode_invalid")
    fields = {"attestation_id", "attestation_digest", "registered_at"}
    _require(
        isinstance(registration, dict) and set(registration) == fields,
        "registration_response_shape_invalid",
    )
    _require(
        registration["attestation_id"] == attestation["attestation_id"],
        "registration_attestation_id_mismatch",
    )
    _require(
        registration["attestation_digest"] == attestation["attestation_digest"],
        "registration_attestation_digest_mismatch",
    )
    registered_at = registration["registered_at"]
    _require(isinstance(registered_at, str), "registration_time_invalid")
    try:
        parsed = datetime.fromisoformat(registered_at.replace("Z", "+00:00"))
    except ValueError:
        raise RolloutError("registration_time_invalid") from None
    _require(
        parsed.tzinfo is not None and registered_at.endswith("Z"),
        "registration_time_invalid",
    )
    return {
        "registration": "canonical_response_supplied_by_operator",
        "registration_attestation_id": registration["attestation_id"],
        "registration_attestation_digest": registration["attestation_digest"],
        "registered_at_utc": registered_at,
    }


def _environment_map(definition: dict[str, Any]) -> dict[str, str]:
    containers = definition.get("containerDefinitions")
    _require(isinstance(containers, list), "task_definition_containers_invalid")
    apps = [
        item
        for item in containers
        if isinstance(item, dict) and item.get("name") == APP_CONTAINER
    ]
    _require(len(apps) == 1, "app_container_count_mismatch")
    rows = apps[0].get("environment", [])
    _require(isinstance(rows, list), "task_environment_invalid")
    values: dict[str, str] = {}
    for item in rows:
        _require(
            isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and isinstance(item.get("value"), str),
            "task_environment_invalid",
        )
        name = item["name"]
        _require(name not in values, "task_environment_duplicate")
        value = item["value"]
        if name.startswith("BILLING_PRODUCER_") and name.endswith("_PUBLIC_KEY"):
            if value.startswith("sha256:") and _HEX64.fullmatch(value[7:]):
                values[name] = value
            else:
                values[name] = (
                    "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
                )
        else:
            values[name] = value
    _require("DATABASE_URL" not in values, "database_url_must_be_secret_reference")
    _require(
        not any("PRIVATE_KEY" in name or "SECRET_VALUE" in name for name in values),
        "private_key_or_secret_env_forbidden",
    )
    return values


def _app_container(definition: dict[str, Any]) -> dict[str, Any]:
    containers = definition.get("containerDefinitions")
    _require(isinstance(containers, list), "task_definition_containers_invalid")
    matches = [
        item
        for item in containers
        if isinstance(item, dict) and item.get("name") == APP_CONTAINER
    ]
    _require(len(matches) == 1, "app_container_count_mismatch")
    return matches[0]


def _environment_hash_map(definition: dict[str, Any]) -> dict[str, str]:
    rows = _app_container(definition).get("environment", [])
    _require(isinstance(rows, list), "task_environment_invalid")
    result: dict[str, str] = {}
    for item in rows:
        _require(
            isinstance(item, dict) and isinstance(item.get("name"), str),
            "task_environment_invalid",
        )
        name = item["name"]
        if isinstance(item.get("value_sha256"), str):
            digest = item["value_sha256"]
        elif isinstance(item.get("value"), str):
            digest = hashlib.sha256(item["value"].encode("utf-8")).hexdigest()
        else:
            raise RolloutError("task_environment_fingerprint_missing")
        _require(
            _HEX64.fullmatch(digest) is not None and name not in result,
            "task_environment_fingerprint_invalid",
        )
        result[name] = digest
    return result


def _task_definition_runtime_fingerprint(definition: dict[str, Any]) -> str:
    """Hash runtime settings while normalizing only approved image/env/DB changes.

    Environment values are hashed for the base and D3 names; the trust pins
    and commercial gates the rollout itself adds are left out. DATABASE_URL
    binds its secret ARN and JSON key only (a plain ARN has an empty key).
    """
    normalized = {
        key: value
        for key, value in definition.items()
        if key
        not in {
            "taskDefinitionArn",
            "revision",
            "status",
            "requiresAttributes",
            "compatibilities",
            "registeredAt",
            "registeredBy",
            "deregisteredAt",
        }
    }
    containers = definition.get("containerDefinitions")
    _require(isinstance(containers, list), "task_definition_containers_missing")
    normalized_containers: list[dict[str, Any]] = []
    for container in containers:
        _require(isinstance(container, dict), "task_definition_container_invalid")
        name = container.get("name")
        _require(isinstance(name, str), "task_definition_container_invalid")
        row = {
            key: value
            for key, value in container.items()
            if key not in {"image", "environment", "secrets"}
        }
        row["image_fingerprint"] = (
            "app-image-is-verified-separately"
            if name == APP_CONTAINER
            else hashlib.sha256(
                str(container.get("image", "")).encode("utf-8")
            ).hexdigest()
        )
        environment = container.get("environment", [])
        _require(isinstance(environment, list), "task_definition_environment_invalid")
        env_rows: list[dict[str, str]] = []
        for item in environment:
            _require(
                isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and isinstance(item.get("value"), str),
                "task_definition_environment_invalid",
            )
            if item["name"] in FINGERPRINTED_ENVIRONMENT_NAMES:
                env_rows.append(
                    {
                        "name": item["name"],
                        "value_sha256": hashlib.sha256(
                            item["value"].encode("utf-8")
                        ).hexdigest(),
                    }
                )
        row["environment"] = sorted(env_rows, key=lambda item: item["name"])
        secret_rows = container.get("secrets", [])
        _require(isinstance(secret_rows, list), "task_definition_secrets_invalid")
        normalized_secrets: list[dict[str, str]] = []
        for secret in secret_rows:
            _require(
                isinstance(secret, dict)
                and isinstance(secret.get("name"), str)
                and isinstance(secret.get("valueFrom"), str),
                "task_definition_secret_invalid",
            )
            if secret["name"] == "DATABASE_URL":
                parts = secret["valueFrom"].split(":")
                _require(len(parts) in {7, 10}, "database_url_reference_invalid")
                # A plain ARN (7 parts) is the empty JSON key at the default
                # version, so it must hash like ``<ARN>:::<VersionId>`` (the
                # pin build_replacement_task_definition applies): bind the ARN
                # and the JSON key, never the version stage or id.
                json_key = parts[7] if len(parts) == 10 else ""
                arn_and_key = ":".join([*parts[:7], json_key])
                normalized_secrets.append(
                    {
                        "name": secret["name"],
                        "arn_and_json_key_sha256": hashlib.sha256(
                            arn_and_key.encode("utf-8")
                        ).hexdigest(),
                    }
                )
            else:
                normalized_secrets.append(
                    {
                        "name": secret["name"],
                        "reference_sha256": hashlib.sha256(
                            secret["valueFrom"].encode("utf-8")
                        ).hexdigest(),
                    }
                )
        row["secrets"] = sorted(normalized_secrets, key=lambda item: item["name"])
        normalized_containers.append(row)
    normalized["containerDefinitions"] = sorted(
        normalized_containers, key=lambda item: item.get("name", "")
    )
    encoded = json.dumps(
        normalized, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sanitize_aws_readbacks(raw: Any) -> dict[str, Any]:
    """Project authenticated AWS CLI responses to the reviewed evidence fields."""
    required = {
        "service_response",
        "tasks_response",
        "task_definition_response",
        "ecr_response",
        "secret_description",
        "secret_versions",
    }
    _require(
        isinstance(raw, dict) and set(raw) == required,
        "aws_readback_input_shape_invalid",
    )

    def reject_secret_material(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                _require(
                    key
                    not in {
                        "SecretString",
                        "SecretBinary",
                        "secret_value",
                        "database_url",
                    },
                    "secret_value_field_forbidden",
                )
                reject_secret_material(nested)
        elif isinstance(value, list):
            for nested in value:
                reject_secret_material(nested)

    reject_secret_material(raw)
    service_response = raw["service_response"]
    tasks_response = raw["tasks_response"]
    definition_response = raw["task_definition_response"]
    ecr_response = raw["ecr_response"]
    _require(
        isinstance(service_response, dict) and service_response.get("failures") == [],
        "service_readback_unavailable",
    )
    _require(
        isinstance(tasks_response, dict) and tasks_response.get("failures") == [],
        "task_readback_unavailable",
    )
    services = service_response.get("services")
    tasks = tasks_response.get("tasks")
    definition = (
        definition_response.get("taskDefinition")
        if isinstance(definition_response, dict)
        else None
    )
    _require(
        isinstance(services, list) and len(services) == 1, "service_readback_ambiguous"
    )
    _require(isinstance(tasks, list) and tasks, "task_readback_empty")
    _require(isinstance(definition, dict), "task_definition_readback_unavailable")
    _require(
        isinstance(ecr_response, dict)
        and isinstance(ecr_response.get("imageDetails"), list),
        "ecr_readback_unavailable",
    )

    service = services[0]
    safe_service = {
        key: service.get(key)
        for key in (
            "serviceArn",
            "serviceName",
            "status",
            "desiredCount",
            "runningCount",
            "taskDefinition",
        )
    }
    load_balancers = service.get("loadBalancers")
    _require(isinstance(load_balancers, list), "service_load_balancers_unreadable")
    safe_service["loadBalancers"] = [
        {
            key: row.get(key)
            for key in ("targetGroupArn", "containerName", "containerPort")
        }
        for row in load_balancers
        if isinstance(row, dict)
    ]
    deployments = service.get("deployments")
    _require(isinstance(deployments, list), "service_deployments_unreadable")
    safe_service["deployments"] = [
        {
            key: row.get(key)
            for key in ("id", "status", "taskDefinition", "rolloutState")
        }
        for row in deployments
        if isinstance(row, dict)
    ]

    safe_tasks = []
    for task in tasks:
        _require(isinstance(task, dict), "active_task_metadata_invalid")
        containers = task.get("containers")
        _require(
            isinstance(containers, list) and containers,
            "active_task_containers_missing",
        )
        attachments = task.get("attachments")
        _require(isinstance(attachments, list), "active_task_attachments_missing")
        private_ips = [
            detail.get("value")
            for attachment in attachments
            if isinstance(attachment, dict)
            for detail in attachment.get("details", [])
            if isinstance(detail, dict)
            if detail.get("name") == "privateIPv4Address"
        ]
        _require(
            len(private_ips) == 1 and isinstance(private_ips[0], str),
            "active_task_private_ip_missing",
        )
        try:
            address = ipaddress.ip_address(private_ips[0])
        except ValueError:
            raise RolloutError("active_task_private_ip_invalid") from None
        _require(
            address.version == 4 and address.is_private,
            "active_task_private_ip_invalid",
        )
        safe_tasks.append(
            {
                key: task.get(key)
                for key in (
                    "taskArn",
                    "taskDefinitionArn",
                    "lastStatus",
                    "desiredStatus",
                    "createdAt",
                    "startedAt",
                )
            }
            | {"privateIp": private_ips[0]}
            | {
                "containers": [
                    {
                        key: container.get(key)
                        for key in ("name", "lastStatus", "healthStatus", "imageDigest")
                    }
                    for container in containers
                    if isinstance(container, dict)
                ]
            }
        )

    safe_containers = []
    containers = definition.get("containerDefinitions")
    _require(
        isinstance(containers, list) and containers,
        "task_definition_containers_missing",
    )
    for container in containers:
        _require(
            isinstance(container, dict) and isinstance(container.get("name"), str),
            "task_definition_container_invalid",
        )
        env_rows = container.get("environment", [])
        _require(isinstance(env_rows, list), "task_definition_environment_invalid")
        seen_names = set()
        safe_environment = []
        for row in env_rows:
            _require(
                isinstance(row, dict)
                and isinstance(row.get("name"), str)
                and isinstance(row.get("value"), str),
                "task_definition_environment_invalid",
            )
            name = row["name"]
            _require(name not in seen_names, "task_environment_duplicate")
            seen_names.add(name)
            _require(
                name != "DATABASE_URL"
                and "PRIVATE_KEY" not in name
                and "SECRET_VALUE" not in name,
                "secret_or_private_value_in_environment",
            )
            if name.startswith("BILLING_PRODUCER_") and name.endswith("_PUBLIC_KEY"):
                value = (
                    "sha256:" + hashlib.sha256(row["value"].encode("utf-8")).hexdigest()
                )
            elif name in {
                "BILLING_ROLLOUT_STATE",
                "BILLING_TOPUP_COMMERCIAL_ENABLED",
                "BILLING_PRODUCER_GRANT_KID",
                "BILLING_PRODUCER_BUILD_KID",
                "BILLING_PRODUCER_RECEIPT_KID",
            }:
                value = row["value"]
            else:
                value = "<redacted>"
            safe_row = {"name": name, "value": value}
            if name in FINGERPRINTED_ENVIRONMENT_NAMES:
                safe_row["value_sha256"] = hashlib.sha256(
                    row["value"].encode("utf-8")
                ).hexdigest()
            safe_environment.append(safe_row)
        secret_rows = container.get("secrets", [])
        _require(isinstance(secret_rows, list), "task_definition_secrets_invalid")
        safe_secrets = [
            {"name": row["name"], "valueFrom": row.get("valueFrom")}
            for row in secret_rows
            if isinstance(row, dict) and row.get("name") == "DATABASE_URL"
        ]
        health = container.get("healthCheck")
        safe_health = None
        if health is not None:
            command = health.get("command") if isinstance(health, dict) else None
            _require(
                isinstance(command, list)
                and any("/ready" in str(item) for item in command),
                "app_readiness_probe_missing",
            )
            safe_health = {"command": ["/ready"]}
        safe_containers.append(
            {
                "name": container["name"],
                "image": container.get("image"),
                "environment": safe_environment,
                "secrets": safe_secrets,
                "healthCheck": safe_health,
            }
        )
    safe_definition = {
        key: definition.get(key)
        for key in (
            "taskDefinitionArn",
            "family",
            "status",
            "taskRoleArn",
            "executionRoleArn",
        )
    } | {
        "containerDefinitions": safe_containers,
        "runtime_fingerprint_sha256": _task_definition_runtime_fingerprint(definition),
    }
    description = raw["secret_description"]
    versions = raw["secret_versions"]
    _require(
        isinstance(description, dict) and isinstance(versions, dict),
        "secret_metadata_unreadable",
    )
    safe_description = {key: description.get(key) for key in ("ARN", "LastChangedDate")}
    safe_versions = {
        "ARN": versions.get("ARN"),
        "Versions": [
            {
                "VersionId": row.get("VersionId"),
                "VersionStages": row.get("VersionStages"),
            }
            for row in versions.get("Versions", [])
            if isinstance(row, dict)
        ],
    }
    return {
        "service": safe_service,
        "tasks": safe_tasks,
        "task_definition": safe_definition,
        "ecr": {
            "imageDetails": [
                {"imageDigest": row.get("imageDigest")}
                for row in ecr_response["imageDetails"]
                if isinstance(row, dict)
            ]
        },
        "secret_description": safe_description,
        "secret_versions": safe_versions,
    }


def _database_reference(definition: dict[str, Any]) -> str:
    app = _app_container(definition)
    secrets = app.get("secrets", [])
    _require(isinstance(secrets, list), "task_definition_secrets_invalid")
    matches = [
        item
        for item in secrets
        if isinstance(item, dict) and item.get("name") == "DATABASE_URL"
    ]
    _require(len(matches) == 1, "database_url_reference_count_mismatch")
    value_from = matches[0].get("valueFrom")
    _require(isinstance(value_from, str), "database_url_reference_invalid")
    reference_parts = value_from.split(":")
    _require(len(reference_parts) in {7, 10}, "database_url_reference_invalid")
    secret_arn = ":".join(reference_parts[:7])
    _require(_ARN.fullmatch(secret_arn) is not None, "database_url_reference_invalid")
    if len(reference_parts) == 10:
        _require(
            all(":" not in part for part in reference_parts[7:]),
            "database_url_reference_invalid",
        )
    # Return only the ECS Secrets Manager reference; never inspect or return a value.
    return value_from


def _readiness_marker(definition: dict[str, Any]) -> str:
    health = _app_container(definition).get("healthCheck")
    _require(isinstance(health, dict), "app_health_check_missing")
    command = health.get("command")
    _require(isinstance(command, list) and command, "app_health_check_missing")
    joined = " ".join(str(item) for item in command)
    _require("/ready" in joined, "app_readiness_probe_missing")
    return "container_healthcheck_ready_endpoint"


def validate_deployment_snapshot(
    raw: Any,
    *,
    expected_digest: str,
    expected_image_uri: str,
    expected_task_role_arn: str,
    expected_execution_role_arn: str,
    expected_secret_arn: str,
    expected_secret_json_key: str,
    expected_secret_version_id: str | None,
    expected_task_environment: dict[str, str],
    phase: str,
    expected_base_environment_sha256: dict[str, str] | None = None,
    external_readiness_verified: bool = False,
) -> dict[str, Any]:
    _require(phase in {"before", "after"}, "readback_phase_invalid")
    _require(
        isinstance(raw, dict)
        and set(raw)
        == {
            "service",
            "tasks",
            "task_definition",
            "ecr",
            "secret_description",
            "secret_versions",
        },
        "readback_input_shape_invalid",
    )
    service = raw["service"]
    _require(isinstance(service, dict), "service_readback_invalid")
    _require(
        service.get("serviceArn") == SERVICE_ARN
        and service.get("serviceName") == SERVICE_NAME
        and service.get("status") == "ACTIVE",
        "service_identity_mismatch",
    )
    desired = service.get("desiredCount")
    _require(
        type(desired) is int and desired > 0 and service.get("runningCount") == desired,
        "service_running_count_mismatch",
    )
    service_task_definition = service.get("taskDefinition")
    _require(
        isinstance(service_task_definition, str)
        and service_task_definition.startswith(
            f"arn:aws:ecs:us-east-1:388460142431:task-definition/{TASK_FAMILY}:"
        ),
        "service_task_definition_mismatch",
    )
    load_balancers = service.get("loadBalancers")
    _require(
        isinstance(load_balancers, list)
        and len(load_balancers) == 1
        and isinstance(load_balancers[0], dict)
        and isinstance(load_balancers[0].get("targetGroupArn"), str)
        and load_balancers[0].get("containerName") == APP_CONTAINER
        and type(load_balancers[0].get("containerPort")) is int,
        "service_load_balancer_binding_invalid",
    )
    deployments = service.get("deployments")
    _require(
        isinstance(deployments, list) and deployments,
        "service_deployment_readback_invalid",
    )
    primary = [
        item
        for item in deployments
        if isinstance(item, dict) and item.get("status") == "PRIMARY"
    ]
    _require(
        len(primary) == 1
        and isinstance(primary[0].get("id"), str)
        and primary[0]["id"]
        and primary[0].get("taskDefinition") == service_task_definition,
        "service_primary_deployment_mismatch",
    )
    _require(primary[0].get("rolloutState") != "FAILED", "service_deployment_failed")

    tasks = raw["tasks"]
    _require(
        isinstance(tasks, list) and len(tasks) == desired, "active_task_cohort_mismatch"
    )
    task_arns: list[str] = []
    observed_digests: list[str] = []
    for task in tasks:
        _require(isinstance(task, dict), "active_task_metadata_invalid")
        _require(
            task.get("lastStatus") == "RUNNING"
            and task.get("desiredStatus") == "RUNNING",
            "active_task_not_running",
        )
        _require(
            task.get("taskDefinitionArn") == service_task_definition,
            "active_task_definition_mismatch",
        )
        arn = task.get("taskArn")
        _require(
            isinstance(arn, str)
            and arn.startswith(
                "arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/"
            ),
            "active_task_arn_invalid",
        )
        _require(arn not in task_arns, "active_task_arn_duplicate")
        task_arns.append(arn)
        private_ip = task.get("privateIp")
        _require(isinstance(private_ip, str), "active_task_private_ip_missing")
        try:
            address = ipaddress.ip_address(private_ip)
        except ValueError:
            raise RolloutError("active_task_private_ip_invalid") from None
        _require(
            address.version == 4 and address.is_private,
            "active_task_private_ip_invalid",
        )
        app_containers = [
            item
            for item in task.get("containers", [])
            if isinstance(item, dict) and item.get("name") == APP_CONTAINER
        ]
        _require(
            len(app_containers) == 1
            and app_containers[0].get("lastStatus") == "RUNNING",
            "app_container_not_running",
        )
        if not external_readiness_verified:
            _require(
                app_containers[0].get("healthStatus") == "HEALTHY",
                "app_container_not_healthy",
            )
        else:
            _require(
                app_containers[0].get("healthStatus") in {"UNKNOWN", "HEALTHY"},
                "app_container_unhealthy",
            )
        task_digest = app_containers[0].get("imageDigest")
        _require(
            isinstance(task_digest, str) and _DIGEST.fullmatch(task_digest) is not None,
            "active_task_digest_invalid",
        )
        observed_digests.append(task_digest)

    if phase == "before" and expected_digest == "current":
        _require(len(set(observed_digests)) == 1, "active_task_digest_mixed")
        expected_digest = observed_digests[0]
    _require(
        all(digest == expected_digest for digest in observed_digests),
        "active_task_digest_mismatch",
    )

    definition = raw["task_definition"]
    _require(
        isinstance(definition, dict)
        and definition.get("taskDefinitionArn") == service_task_definition
        and definition.get("family") == TASK_FAMILY
        and definition.get("status") == "ACTIVE",
        "task_definition_readback_mismatch",
    )
    _require(
        definition.get("taskRoleArn") == expected_task_role_arn, "task_role_mismatch"
    )
    _require(
        definition.get("executionRoleArn") == expected_execution_role_arn,
        "execution_role_mismatch",
    )
    runtime_fingerprint = definition.get("runtime_fingerprint_sha256")
    if runtime_fingerprint is None:
        runtime_fingerprint = _task_definition_runtime_fingerprint(definition)
    _require(
        isinstance(runtime_fingerprint, str)
        and _HEX64.fullmatch(runtime_fingerprint) is not None,
        "task_definition_runtime_fingerprint_invalid",
    )
    app = _app_container(definition)
    image_reference = app.get("image")
    validate_target_image_repository(expected_image_uri)
    _require(isinstance(image_reference, str), "task_definition_image_mismatch")
    if phase == "after":
        # The deployed revision runs exactly the attested target digest.
        _require(
            image_reference == f"{expected_image_uri}@{expected_digest}",
            "task_definition_image_mismatch",
        )
    else:
        # The serving revision runs the target or a digest-pinned source image.
        try:
            classify_app_image(image_reference, target_repository=expected_image_uri)
        except RolloutError as exc:
            raise RolloutError(f"task_definition_image_mismatch:{exc}") from None
        if "@" in image_reference:
            _require(
                image_reference.endswith(f"@{expected_digest}"),
                "task_definition_image_mismatch",
            )
    env = _environment_map(definition)
    # The base names, plus the D3 names all or none; their values are
    # fingerprinted together as the "base environment" of the template.
    base_names = frozenset(env) & FINGERPRINTED_ENVIRONMENT_NAMES
    _require(base_names in ENVIRONMENT_NAME_SETS, "task_environment_name_set_mismatch")
    expected_env_names = (
        base_names if phase == "before" else base_names | set(expected_task_environment)
    )
    _require(set(env) == expected_env_names, "task_environment_name_set_mismatch")
    all_env_hashes = _environment_hash_map(definition)
    base_environment_hashes = {
        name: all_env_hashes[name] for name in base_names if name in all_env_hashes
    }
    _require(
        set(base_environment_hashes) == base_names,
        "base_task_environment_fingerprint_missing",
    )
    if expected_base_environment_sha256 is not None:
        _require(
            base_environment_hashes == expected_base_environment_sha256,
            "base_task_environment_changed",
        )
    required_env = {
        "BILLING_ROLLOUT_STATE": "disabled",
        "BILLING_TOPUP_COMMERCIAL_ENABLED": "false",
    }
    for kind in ("GRANT", "BUILD", "RECEIPT"):
        required_env[f"BILLING_PRODUCER_{kind}_KID"] = expected_task_environment.get(
            f"BILLING_PRODUCER_{kind}_KID", ""
        )
        public_key = expected_task_environment.get(
            f"BILLING_PRODUCER_{kind}_PUBLIC_KEY", ""
        )
        if public_key and not public_key.startswith("sha256:"):
            public_key = (
                "sha256:" + hashlib.sha256(public_key.encode("utf-8")).hexdigest()
            )
        required_env[f"BILLING_PRODUCER_{kind}_PUBLIC_KEY"] = public_key
    if phase == "after":
        _require(
            all(value and env.get(key) == value for key, value in required_env.items()),
            "task_trust_or_commercial_gate_mismatch",
        )
    else:
        _require(
            all(
                env.get(key, expected) == expected
                for key, expected in {
                    "BILLING_ROLLOUT_STATE": "disabled",
                    "BILLING_TOPUP_COMMERCIAL_ENABLED": "false",
                }.items()
            ),
            "commercial_gate_baseline_mismatch",
        )
        present_trust = {
            key: value
            for key, value in required_env.items()
            if key.startswith("BILLING_PRODUCER_") and key in env
        }
        _require(
            all(env[key] == value for key, value in present_trust.items() if value),
            "task_trust_baseline_mismatch",
        )
    if not external_readiness_verified:
        _readiness_marker(definition)

    _database_reference(definition)
    ref_input = {
        "desired_count": desired,
        "active_tasks": tasks,
        "task_definitions": [definition],
        "secret_description": raw["secret_description"],
        "secret_versions": raw["secret_versions"],
    }
    try:
        secret_evidence = resolve_active_database_secret(ref_input)
    except ReferenceError as exc:
        raise RolloutError(f"database_secret_{exc}") from None
    _require(
        secret_evidence["database_secret_arn"] == expected_secret_arn,
        "database_secret_arn_mismatch",
    )
    _require(
        secret_evidence["database_secret_json_key"] == expected_secret_json_key,
        "database_secret_json_key_mismatch",
    )
    if expected_secret_version_id is not None:
        _require(
            secret_evidence["resolved_version_id"] == expected_secret_version_id,
            "database_secret_version_mismatch",
        )

    ecr = raw["ecr"]
    _require(
        isinstance(ecr, dict) and isinstance(ecr.get("imageDetails"), list),
        "ecr_image_readback_invalid",
    )
    image_rows = [
        row
        for row in ecr["imageDetails"]
        if isinstance(row, dict) and row.get("imageDigest") == expected_digest
    ]
    _require(len(image_rows) == 1, "ecr_digest_not_found_or_ambiguous")
    return {
        "schema": "asaas_producer_image_rollout_readback.v1",
        "phase": phase,
        "cluster_arn": CLUSTER_ARN,
        "service_arn": SERVICE_ARN,
        "task_definition_arn": service_task_definition,
        "primary_deployment_id": primary[0]["id"],
        "task_count": desired,
        "task_arns": sorted(task_arns),
        "task_private_ips": sorted(task.get("privateIp") for task in tasks),
        "task_timestamps": sorted(
            (
                {
                    "task_arn": task["taskArn"],
                    "created_at": task["createdAt"],
                    "started_at": task["startedAt"],
                }
                for task in tasks
            ),
            key=lambda row: row["task_arn"],
        ),
        "runtime_fingerprint_sha256": runtime_fingerprint,
        "base_environment_sha256": dict(sorted(base_environment_hashes.items())),
        "app_image_digest": expected_digest,
        "task_role_arn": expected_task_role_arn,
        "execution_role_arn": expected_execution_role_arn,
        "database_secret_arn": secret_evidence["database_secret_arn"],
        "database_secret_json_key": secret_evidence["database_secret_json_key"],
        "database_secret_version_id": secret_evidence["resolved_version_id"],
        "readiness": "healthy",
        "commercial_gates": "disabled",
        "trust_pins": (
            (
                {
                    key: required_env[key]
                    for key in sorted(required_env)
                    if "PUBLIC_KEY" not in key
                }
                | {
                    key: hashlib.sha256(value.encode("utf-8")).hexdigest()
                    for key, value in required_env.items()
                    if "PUBLIC_KEY" in key
                }
            )
            if phase == "after"
            else {
                key: env[key]
                for key in required_env
                if key.startswith("BILLING_PRODUCER_") and key in env
            }
        ),
    }


def compare_rollout_readbacks(
    before: Any, after: Any, *, expected_digest: str
) -> dict[str, Any]:
    required = {
        "schema",
        "phase",
        "cluster_arn",
        "service_arn",
        "task_definition_arn",
        "primary_deployment_id",
        "task_count",
        "task_arns",
        "app_image_digest",
        "task_role_arn",
        "task_private_ips",
        "task_timestamps",
        "runtime_fingerprint_sha256",
        "base_environment_sha256",
        "execution_role_arn",
        "database_secret_arn",
        "database_secret_json_key",
        "database_secret_version_id",
        "readiness",
        "commercial_gates",
        "trust_pins",
    }
    _require(
        isinstance(before, dict) and set(before) == required,
        "before_readback_shape_invalid",
    )
    _require(
        isinstance(after, dict) and set(after) == required,
        "after_readback_shape_invalid",
    )
    _require(
        before.get("phase") == "before" and after.get("phase") == "after",
        "readback_phase_order_invalid",
    )
    _require(
        all(
            isinstance(row.get("primary_deployment_id"), str)
            and row["primary_deployment_id"]
            for row in (before, after)
        ),
        "readback_primary_deployment_id_invalid",
    )
    _require(
        before.get("schema")
        == after.get("schema")
        == "asaas_producer_image_rollout_readback.v1",
        "readback_schema_mismatch",
    )
    for field in (
        "cluster_arn",
        "service_arn",
        "task_count",
        "task_role_arn",
        "execution_role_arn",
        "database_secret_arn",
        "database_secret_json_key",
        "database_secret_version_id",
        "commercial_gates",
        "runtime_fingerprint_sha256",
        "base_environment_sha256",
    ):
        _require(before.get(field) == after.get(field), f"readback_{field}_changed")
    _require(after.get("app_image_digest") == expected_digest, "after_digest_mismatch")
    _require(
        before.get("readiness") == "healthy" and after.get("readiness") == "healthy",
        "readback_health_failed",
    )
    _require(
        after.get("commercial_gates") == "disabled", "commercial_gates_not_disabled"
    )
    return {
        "schema": "asaas_producer_image_rollout_verification.v1",
        "cluster_arn": after["cluster_arn"],
        "service_arn": after["service_arn"],
        "previous_task_definition_arn": before["task_definition_arn"],
        "active_task_definition_arn": after["task_definition_arn"],
        "previous_primary_deployment_id": before["primary_deployment_id"],
        "active_primary_deployment_id": after["primary_deployment_id"],
        "task_definition_runtime_sha256": after["runtime_fingerprint_sha256"],
        "base_environment_sha256": after["base_environment_sha256"],
        "previous_image_digest": before["app_image_digest"],
        "active_image_digest": after["app_image_digest"],
        "task_count": after["task_count"],
        "database_secret_arn": after["database_secret_arn"],
        "database_secret_json_key": after["database_secret_json_key"],
        "database_secret_version_id": after["database_secret_version_id"],
        "readiness": "healthy",
        "commercial_gates": "disabled",
        "trust_pins": after["trust_pins"],
    }


def _write_private(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    sanitize = subparsers.add_parser("sanitize-readbacks")
    sanitize.add_argument("--input", required=True, type=Path)
    sanitize.add_argument("--output", required=True, type=Path)
    attest = subparsers.add_parser("validate-attestation")
    attest.add_argument("--envelope", required=True, type=Path)
    attest.add_argument("--trust", required=True, type=Path)
    attest.add_argument("--output", required=True, type=Path)
    attest.add_argument("--build-run-id", required=True)
    attest.add_argument("--attestation-id", required=True)
    attest.add_argument("--app-digest", required=True)
    attest.add_argument("--envelope-sha256", required=True)
    attest.add_argument("--mode", required=True, choices=("bootstrap", "registered"))
    attest.add_argument("--registration", type=Path)
    snap = subparsers.add_parser("validate-snapshot")
    snap.add_argument("--input", required=True, type=Path)
    snap.add_argument("--output", required=True, type=Path)
    snap.add_argument("--app-digest", required=True)
    snap.add_argument("--image-uri", required=True)
    snap.add_argument("--task-role-arn", required=True)
    snap.add_argument("--execution-role-arn", required=True)
    snap.add_argument("--secret-arn", required=True)
    snap.add_argument("--secret-json-key", required=True)
    snap.add_argument("--secret-version-id")
    snap.add_argument("--expected-task-environment", required=True, type=Path)
    snap.add_argument("--phase", required=True, choices=("before", "after"))
    compare = subparsers.add_parser("compare-readbacks")
    compare.add_argument("--before", required=True, type=Path)
    compare.add_argument("--after", required=True, type=Path)
    compare.add_argument("--app-digest", required=True)
    compare.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "sanitize-readbacks":
            result = sanitize_aws_readbacks(_strict_json(args.input))
        elif args.command == "validate-attestation":
            env = dict(os.environ)
            trust = _strict_json(args.trust)
            envelope = _strict_json(args.envelope)
            attestation = validate_signed_attestation(
                envelope,
                trust_readback=trust,
                expected_build_run_id=args.build_run_id,
                expected_attestation_id=args.attestation_id,
                expected_digest=args.app_digest,
                expected_envelope_sha256=args.envelope_sha256,
                env=env,
            )
            registration = (
                _strict_json(args.registration) if args.registration else None
            )
            result = {
                **attestation,
                **validate_registration(registration, attestation, mode=args.mode),
            }
        elif args.command == "compare-readbacks":
            result = compare_rollout_readbacks(
                _strict_json(args.before),
                _strict_json(args.after),
                expected_digest=args.app_digest,
            )
        else:
            expected_digest = args.app_digest
            if expected_digest == "current":
                current_input = _strict_json(args.input)
                task_rows = (
                    current_input.get("tasks")
                    if isinstance(current_input, dict)
                    else None
                )
                digests = (
                    {
                        container.get("imageDigest")
                        for task in task_rows
                        if isinstance(task, dict)
                        for container in task.get("containers", [])
                        if isinstance(container, dict)
                        and container.get("name") == APP_CONTAINER
                    }
                    if isinstance(task_rows, list)
                    else set()
                )
                if len(digests) != 1:
                    raise RolloutError("active_task_digest_mixed")
                expected_digest = next(iter(digests))
            result = validate_deployment_snapshot(
                _strict_json(args.input),
                expected_digest=expected_digest,
                expected_image_uri=args.image_uri,
                expected_task_role_arn=args.task_role_arn,
                expected_execution_role_arn=args.execution_role_arn,
                expected_secret_arn=args.secret_arn,
                expected_secret_json_key=args.secret_json_key,
                expected_secret_version_id=args.secret_version_id,
                expected_task_environment=_strict_json(args.expected_task_environment),
                phase=args.phase,
            )
        _write_private(args.output, result)
        print("readback: ready")
        return 0
    except (OSError, RolloutError, ReferenceError, SignedEnvelopeRejected) as exc:
        reason = (
            str(exc)
            if isinstance(exc, (RolloutError, ReferenceError))
            else "attestation_validation_failed"
        )
        print(f"blocked: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
