#!/usr/bin/env python3
"""Validate private, run-bound operator evidence for the ASAAS ECS rollout.

Task-definition inputs and readback snapshots are supplied only through the
protected deployment environment. The CLI emits fixed status markers and
digests; it never prints their contents or AWS credential material.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import ipaddress
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from scripts.asaas_database_secret_reference import ReferenceError  # noqa: E402
from scripts.verify_asaas_producer_image_rollout import (  # noqa: E402
    APP_CONTAINER,
    TASK_FAMILY,
    RolloutError,
    compare_rollout_readbacks,
    validate_deployment_snapshot,
)

REPOSITORY = "neurostackinfra/neurostack-api"
REPOSITORY_ID = "1152335815"
OWNER_ID = "340698182"
RELEASE_REF = "refs/heads/main"
DEPLOY_WORKFLOW_PATH = ".github/workflows/asaas-producer-image-deploy.yml"
DEPLOY_ENVIRONMENT = "asaas-production-deployment"
TASK_ROLE_ARN = "arn:aws:iam::388460142431:role/neurostack-staging-ecs-task"
EXECUTION_ROLE_ARN = "arn:aws:iam::388460142431:role/neurostack-staging-ecs-execution"
TASK_DEFINITION_TEMPLATE_SCHEMA = "asaas_producer_task_definition_template.v1"
READBACK_SCHEMA = "asaas_producer_run_bound_readback.v1"
POST_COMMENT_SCHEMA = "asaas_producer_post_readback_encrypted.v1"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_TASK_DEFINITION_ARN = re.compile(
    r"^arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:[1-9][0-9]*$"
)
_SECRET_ARN = re.compile(
    r"^arn:aws:secretsmanager:us-east-1:388460142431:secret:[A-Za-z0-9/_+=.@-]+$"
)
_BASE_ENV_NAMES = frozenset(
    {"ENVIRONMENT", "HOST", "LLM_MODEL", "LOG_FORMAT", "PORT", "QDRANT_HOST"}
)
_EXPECTED_APP_SECRET_NAMES = frozenset(
    {
        "DATABASE_URL",
        "REDIS_URL",
        "SECRET_KEY",
        "LLM_API_KEY",
        "BILLING_OPERATION_RECEIPT_SIGNING_KEY",
    }
)
_TRUST_KINDS = ("GRANT", "BUILD", "RECEIPT")
_EXPECTED_DEFINITION_FIELDS = frozenset(
    {
        "family",
        "taskRoleArn",
        "executionRoleArn",
        "networkMode",
        "containerDefinitions",
        "volumes",
        "placementConstraints",
        "requiresCompatibilities",
        "cpu",
        "memory",
        "pidMode",
        "ipcMode",
        "proxyConfiguration",
        "inferenceAccelerators",
        "ephemeralStorage",
        "runtimePlatform",
        "tags",
    }
)
_RESPONSE_ONLY_FIELDS = frozenset(
    {
        "taskDefinitionArn",
        "revision",
        "status",
        "requiresAttributes",
        "compatibilities",
        "registeredAt",
        "registeredBy",
        "deregisteredAt",
    }
)


class EvidenceError(ValueError):
    """Private evidence is unsafe, stale, ambiguous, or not bound to this run."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise EvidenceError(reason)


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _strict_loads(raw: str | bytes, *, max_bytes: int = 2_000_000) -> Any:
    if isinstance(raw, str):
        encoded = raw.encode("utf-8")
    else:
        encoded = raw
    _require(len(encoded) <= max_bytes, "private_evidence_too_large")

    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in rows:
            _require(key not in output, "private_evidence_duplicate_key")
            output[key] = value
        return output

    def reject_nonfinite(value: str) -> None:
        _require(False, "private_evidence_nonfinite_number")

    try:
        return json.loads(
            encoded, object_pairs_hook=pairs, parse_constant=reject_nonfinite
        )
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise EvidenceError("private_evidence_json_invalid") from None


def _reject_sensitive_value_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            _require(
                key
                not in {
                    "SecretString",
                    "SecretBinary",
                    "secret_value",
                    "database_url",
                    "private_key",
                    "token",
                    "password",
                },
                "secret_value_field_forbidden",
            )
            _reject_sensitive_value_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_sensitive_value_fields(nested)


def _timestamp(value: Any, field: str) -> datetime:
    _require(isinstance(value, str), f"{field}_invalid")
    _require(value.endswith("Z"), f"{field}_timezone_invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        raise EvidenceError(f"{field}_invalid") from None
    _require(parsed.tzinfo is not None, f"{field}_timezone_invalid")
    return parsed.astimezone(timezone.utc)


def _canonical_uuid(value: Any, field: str) -> str:
    _require(isinstance(value, str) and _UUID.fullmatch(value) is not None, field)
    try:
        canonical = str(UUID(value))
    except ValueError:
        raise EvidenceError(field) from None
    _require(canonical == value, field)
    return canonical


def _task_definition_arn(value: Any) -> str:
    _require(
        isinstance(value, str) and _TASK_DEFINITION_ARN.fullmatch(value) is not None,
        "source_task_definition_arn_invalid",
    )
    return value


def _secret_reference(value: Any) -> str:
    _require(isinstance(value, str) and value, "task_secret_reference_invalid")
    # Secrets Manager references may add JSON-key/stage/version selectors to
    # the ARN. The reference itself is allowed in the private input; values are
    # never accepted or returned.
    parts = value.split(":")
    _require(len(parts) in {7, 10}, "task_secret_reference_invalid")
    arn = ":".join(parts[:7])
    _require(_SECRET_ARN.fullmatch(arn) is not None, "task_secret_reference_invalid")
    if len(parts) == 10:
        _require(
            all("\x00" not in part for part in parts[7:]),
            "task_secret_selector_invalid",
        )
        _require(not (parts[8] and parts[9]), "task_secret_selector_ambiguous")
    return value


def _container_map(task_definition: dict[str, Any]) -> dict[str, dict[str, Any]]:
    containers = task_definition.get("containerDefinitions")
    _require(
        isinstance(containers, list) and len(containers) == 2,
        "task_definition_container_count_invalid",
    )
    mapped: dict[str, dict[str, Any]] = {}
    for container in containers:
        _require(isinstance(container, dict), "task_definition_container_invalid")
        name = container.get("name")
        _require(
            name in {"app", "otel"} and name not in mapped,
            "task_definition_container_names_invalid",
        )
        mapped[name] = container
    _require(set(mapped) == {"app", "otel"}, "task_definition_container_names_invalid")
    return mapped


def _env_map(container: dict[str, Any]) -> dict[str, str]:
    entries = container.get("environment", [])
    _require(isinstance(entries, list), "task_environment_invalid")
    values: dict[str, str] = {}
    for row in entries:
        _require(
            isinstance(row, dict)
            and set(row) == {"name", "value"}
            and isinstance(row.get("name"), str)
            and isinstance(row.get("value"), str),
            "task_environment_invalid",
        )
        name = row["name"]
        _require(name not in values and name, "task_environment_duplicate")
        values[name] = row["value"]
    return values


def _secret_map(container: dict[str, Any]) -> dict[str, str]:
    entries = container.get("secrets", [])
    _require(isinstance(entries, list), "task_secrets_invalid")
    values: dict[str, str] = {}
    for row in entries:
        _require(
            isinstance(row, dict)
            and set(row) == {"name", "valueFrom"}
            and isinstance(row.get("name"), str),
            "task_secret_entry_invalid",
        )
        name = row["name"]
        _require(name not in values and name, "task_secret_duplicate")
        values[name] = _secret_reference(row.get("valueFrom"))
    return values


def _pin_env_values(environment: dict[str, str]) -> dict[str, str]:
    output: dict[str, str] = {}
    for kind in _TRUST_KINDS:
        arn_name = f"BILLING_PRODUCER_{kind}_KMS_KEY_ARN"
        pem_name = f"BILLING_PRODUCER_{kind}_PUBLIC_KEY_PEM"
        kid = os.environ.get(arn_name, "")
        pem = os.environ.get(pem_name, "")
        _require(
            re.fullmatch(r"arn:aws:kms:us-east-1:388460142431:key/[A-Za-z0-9-]+", kid)
            is not None,
            "task_definition_kms_pin_invalid",
        )
        _require(
            pem.startswith("-----BEGIN PUBLIC KEY-----\n")
            and pem.rstrip().endswith("-----END PUBLIC KEY-----"),
            "task_definition_public_key_pin_invalid",
        )
        output[f"BILLING_PRODUCER_{kind}_KID"] = kid
        output[f"BILLING_PRODUCER_{kind}_PUBLIC_KEY"] = pem
    output["BILLING_ROLLOUT_STATE"] = "disabled"
    output["BILLING_TOPUP_COMMERCIAL_ENABLED"] = "false"
    _require(
        environment.get("BILLING_ROLLOUT_STATE", "disabled") == "disabled"
        and environment.get("BILLING_TOPUP_COMMERCIAL_ENABLED", "false") == "false",
        "commercial_gates_must_remain_disabled",
    )
    return output


def validate_task_definition_template(raw: Any) -> dict[str, Any]:
    """Validate the protected template and create the exact replacement request.

    The returned request still contains the private source environment values;
    callers must write it only to a mode-0600 runner-temporary file.
    """

    _reject_sensitive_value_fields(raw)
    _require(
        isinstance(raw, dict)
        and set(raw)
        == {"schema_version", "source_task_definition_arn", "task_definition"}
        and raw.get("schema_version") == TASK_DEFINITION_TEMPLATE_SCHEMA,
        "task_definition_template_shape_invalid",
    )
    source_arn = _task_definition_arn(raw["source_task_definition_arn"])
    definition = raw["task_definition"]
    _require(isinstance(definition, dict), "task_definition_template_invalid")
    _require(
        set(definition) <= _EXPECTED_DEFINITION_FIELDS,
        "task_definition_response_field_forbidden",
    )
    _require(
        {
            "family",
            "taskRoleArn",
            "executionRoleArn",
            "containerDefinitions",
            "requiresCompatibilities",
        }
        <= set(definition),
        "task_definition_required_fields_missing",
    )
    _require(definition.get("family") == TASK_FAMILY, "task_definition_family_mismatch")
    _require(
        definition.get("taskRoleArn") == TASK_ROLE_ARN,
        "task_definition_task_role_mismatch",
    )
    _require(
        definition.get("executionRoleArn") == EXECUTION_ROLE_ARN,
        "task_definition_execution_role_mismatch",
    )
    containers = _container_map(definition)
    app = containers[APP_CONTAINER]
    current_env = _env_map(app)
    _require(
        set(current_env) == _BASE_ENV_NAMES,
        "task_definition_base_environment_names_mismatch",
    )
    producer_pins = _pin_env_values(current_env)
    app_secrets = _secret_map(app)
    _require(
        set(app_secrets) == _EXPECTED_APP_SECRET_NAMES,
        "task_definition_secret_names_mismatch",
    )
    _require("DATABASE_URL" not in current_env, "database_url_must_be_secret_reference")
    _require(
        _SECRET_ARN.fullmatch(app_secrets["DATABASE_URL"]) is not None,
        "database_url_source_selector_must_be_default",
    )
    _require(_env_map(containers["otel"]) == {}, "otel_environment_must_be_empty")
    _require(_secret_map(containers["otel"]) == {}, "otel_secrets_must_be_empty")
    _require(
        "image" in app and isinstance(app["image"], str) and app["image"].strip(),
        "task_definition_app_image_invalid",
    )
    _require(
        "image" in containers["otel"] and isinstance(containers["otel"]["image"], str),
        "task_definition_otel_image_invalid",
    )

    request = copy.deepcopy(definition)
    return {
        "source_task_definition_arn": source_arn,
        "request": request,
        "template_sha256": canonical_sha256(raw),
        "producer_environment_sha256": canonical_sha256(producer_pins),
        "app_environment_names": sorted(current_env),
        "app_secret_names": sorted(app_secrets),
        "base_environment_sha256": {
            name: hashlib.sha256(value.encode("utf-8")).hexdigest()
            for name, value in sorted(current_env.items())
        },
    }


def validate_registered_task_definition(
    response: Any,
    expected_request: dict[str, Any],
    *,
    expected_task_definition_arn: str | None = None,
    expected_digest: str,
) -> dict[str, Any]:
    """Check the RegisterTaskDefinition response without returning its values."""

    _require(
        isinstance(response, dict) and isinstance(response.get("taskDefinition"), dict),
        "task_definition_register_response_invalid",
    )
    definition = response["taskDefinition"]
    arn = definition.get("taskDefinitionArn")
    _require(_task_definition_arn(arn) == arn, "registered_task_definition_arn_invalid")
    if expected_task_definition_arn is not None:
        _require(
            arn == expected_task_definition_arn,
            "registered_task_definition_arn_mismatch",
        )
    _require(
        definition.get("family") == TASK_FAMILY
        and definition.get("status") == "ACTIVE",
        "registered_task_definition_identity_invalid",
    )
    _require(
        definition.get("taskRoleArn") == TASK_ROLE_ARN
        and definition.get("executionRoleArn") == EXECUTION_ROLE_ARN,
        "registered_task_definition_role_mismatch",
    )
    expected_containers = _container_map(expected_request)
    observed_containers = _container_map(definition)
    _require(
        set(expected_containers) == set(observed_containers),
        "registered_task_definition_containers_mismatch",
    )
    for name in expected_containers:
        expected = expected_containers[name]
        observed = observed_containers[name]
        _require(
            observed.get("image") == expected.get("image"),
            "registered_task_definition_image_mismatch",
        )
        _require(
            _env_map(observed) == _env_map(expected),
            "registered_task_definition_environment_mismatch",
        )
        _require(
            _secret_map(observed) == _secret_map(expected),
            "registered_task_definition_secrets_mismatch",
        )
    app = observed_containers[APP_CONTAINER]
    digest = f"sha256:{expected_digest.removeprefix('sha256:')}"
    _require(
        app.get("image", "").endswith("@" + digest),
        "registered_task_definition_digest_mismatch",
    )
    for field in _EXPECTED_DEFINITION_FIELDS - {"containerDefinitions", "tags"}:
        if field in expected_request:
            _require(
                definition.get(field) == expected_request[field],
                "registered_task_definition_runtime_settings_mismatch",
            )
    _require(
        isinstance(definition.get("revision"), int)
        and definition["revision"] > 0
        and arn.endswith(f":{definition['revision']}"),
        "registered_task_definition_revision_invalid",
    )
    return {
        "task_definition_arn": arn,
        "revision": definition["revision"],
        "app_image_digest": digest,
        "container_names": sorted(observed_containers),
        "environment_names": sorted(_env_map(app)),
        "secret_names": sorted(_secret_map(app)),
    }


def build_replacement_task_definition(
    template: Any,
    *,
    image_uri: str,
    app_digest: str,
    pinned_database_value_from: str,
) -> dict[str, Any]:
    """Apply only the immutable app digest, trust pins, gates, and DB version."""

    validated = validate_task_definition_template(template)
    _require(_DIGEST.fullmatch(app_digest) is not None, "app_image_digest_invalid")
    _require(
        re.fullmatch(
            r"388460142431\.dkr\.ecr\.us-east-1\.amazonaws\.com/[A-Za-z0-9._/-]+",
            image_uri,
        )
        is not None,
        "app_image_uri_invalid",
    )
    request = validated["request"]
    containers = _container_map(request)
    app = containers[APP_CONTAINER]
    original_image = app["image"]
    _require(original_image.startswith(image_uri), "source_image_repository_mismatch")
    app["image"] = f"{image_uri}@{app_digest}"

    current_environment = _env_map(app)
    pins = _pin_env_values(current_environment)
    for name, value in pins.items():
        _require(
            name not in current_environment,
            "task_definition_rollout_environment_duplicate",
        )
        current_environment[name] = value
    app["environment"] = [
        {"name": name, "value": value}
        for name, value in sorted(current_environment.items())
    ]

    secrets = _secret_map(app)
    source_db_arn = ":".join(secrets["DATABASE_URL"].split(":")[:7])
    _require(
        isinstance(pinned_database_value_from, str),
        "pinned_database_secret_reference_invalid",
    )
    _secret_reference(pinned_database_value_from)
    _require(
        ":".join(pinned_database_value_from.split(":")[:7]) == source_db_arn,
        "pinned_database_secret_reference_invalid",
    )
    for row in app["secrets"]:
        if row["name"] == "DATABASE_URL":
            row["valueFrom"] = pinned_database_value_from
    return request


def _target_group_check(
    value: Any,
    *,
    desired_count: int,
    target_group_arn: str,
    container_port: int,
    expected_task_ips: list[str],
    readback_checked_at: datetime,
    earliest_allowed_at: datetime,
    latest_allowed_at: datetime,
) -> dict[str, Any]:
    required = {
        "target_group_arn",
        "source_apis",
        "checked_at_utc",
        "health_check_enabled",
        "health_check_path",
        "healthy_count",
        "unhealthy_count",
        "targets",
    }
    _require(
        isinstance(value, dict) and set(value) == required,
        "target_group_readiness_invalid",
    )
    arn = value["target_group_arn"]
    _require(
        isinstance(arn, str)
        and re.fullmatch(
            r"arn:aws:elasticloadbalancing:us-east-1:388460142431:targetgroup/[A-Za-z0-9-]+/[A-Fa-f0-9]+",
            arn,
        ),
        "target_group_arn_invalid",
    )
    _require(
        value.get("health_check_path") in {"/health", "/ready"},
        "target_group_health_check_path_invalid",
    )
    _require(
        value.get("source_apis") == ["DescribeTargetGroups", "DescribeTargetHealth"],
        "target_group_source_invalid",
    )
    _require(
        value.get("health_check_enabled") is True, "target_group_health_check_disabled"
    )
    checked_at = _timestamp(value.get("checked_at_utc"), "target_group_checked_at")
    _require(
        earliest_allowed_at <= checked_at <= latest_allowed_at,
        "target_group_readback_stale_or_future",
    )
    _require(
        abs(readback_checked_at - checked_at) <= timedelta(minutes=5),
        "target_group_readback_time_mismatch",
    )
    _require(arn == target_group_arn, "target_group_service_binding_mismatch")
    targets = value.get("targets")
    _require(isinstance(targets, list) and targets, "target_group_targets_missing")
    healthy: list[str] = []
    health_state_counts = {"healthy": 0, "draining": 0, "unused": 0}
    seen: set[tuple[str, int]] = set()
    for target in targets:
        _require(
            isinstance(target, dict) and set(target) == {"id", "port", "health_state"},
            "target_group_target_invalid",
        )
        target_id = target.get("id")
        port = target.get("port")
        state = target.get("health_state")
        _require(isinstance(target_id, str), "target_group_target_id_invalid")
        try:
            address = ipaddress.ip_address(target_id)
        except ValueError:
            raise EvidenceError("target_group_target_id_invalid") from None
        _require(
            address.version == 4 and address.is_private,
            "target_group_target_id_invalid",
        )
        _require(
            type(port) is int and port == container_port,
            "target_group_target_port_mismatch",
        )
        _require(
            state in {"healthy", "draining", "unused"}, "target_group_target_unhealthy"
        )
        health_state_counts[state] += 1
        _require((target_id, port) not in seen, "target_group_target_duplicate")
        seen.add((target_id, port))
        if state == "healthy":
            healthy.append(target_id)
        else:
            _require(
                target_id not in expected_task_ips, "active_task_target_not_healthy"
            )
    _require(
        type(value.get("healthy_count")) is int
        and value["healthy_count"] == health_state_counts["healthy"] == desired_count,
        "target_group_unhealthy",
    )
    _require(
        type(value.get("unhealthy_count")) is int and value["unhealthy_count"] == 0,
        "target_group_unhealthy",
    )
    _require(
        len(healthy) == desired_count and set(healthy) == set(expected_task_ips),
        "target_group_cohort_mismatch",
    )
    _require(
        len(expected_task_ips) == desired_count
        and len(set(expected_task_ips)) == desired_count,
        "task_private_ip_cohort_invalid",
    )
    return {
        "target_group_arn": arn,
        "source_apis": ["DescribeTargetGroups", "DescribeTargetHealth"],
        "checked_at_utc": value["checked_at_utc"],
        "health_check_enabled": True,
        "health_check_path": value["health_check_path"],
        "target_count": len(targets),
        "healthy_count": desired_count,
        "unhealthy_count": 0,
        "health_state_counts": health_state_counts,
    }


def _task_readiness_check(
    value: Any,
    *,
    expected_task_ips: list[str],
    readback_checked_at: datetime,
    earliest_allowed_at: datetime,
    latest_allowed_at: datetime,
) -> dict[str, Any]:
    required = {"source", "method", "path", "checked_at_utc", "targets"}
    _require(
        isinstance(value, dict) and set(value) == required,
        "task_readiness_probe_invalid",
    )
    _require(
        value.get("source") == "operator_attested_vpc_http_probe",
        "task_readiness_probe_source_invalid",
    )
    _require(
        value.get("method") == "GET" and value.get("path") == "/ready",
        "task_readiness_probe_path_invalid",
    )
    checked_at = _timestamp(
        value.get("checked_at_utc"), "task_readiness_probe_checked_at"
    )
    _require(
        earliest_allowed_at <= checked_at <= latest_allowed_at,
        "task_readiness_probe_stale_or_future",
    )
    _require(
        abs(readback_checked_at - checked_at) <= timedelta(minutes=5),
        "task_readiness_probe_time_mismatch",
    )
    targets = value.get("targets")
    _require(
        isinstance(targets, list) and targets, "task_readiness_probe_targets_missing"
    )
    observed: list[str] = []
    for target in targets:
        _require(
            isinstance(target, dict)
            and set(target) == {"id", "status_code", "ready"}
            and isinstance(target.get("id"), str)
            and type(target.get("status_code")) is int
            and type(target.get("ready")) is bool,
            "task_readiness_probe_target_invalid",
        )
        _require(
            target["status_code"] == 200 and target["ready"] is True,
            "task_readiness_probe_failed",
        )
        observed.append(target["id"])
    _require(
        len(observed) == len(expected_task_ips)
        and len(set(observed)) == len(observed)
        and set(observed) == set(expected_task_ips),
        "task_readiness_probe_cohort_mismatch",
    )
    return {
        "source": "operator_attested_vpc_http_probe",
        "method": "GET",
        "path": "/ready",
        "checked_at_utc": value["checked_at_utc"],
        "target_count": len(observed),
        "ready_count": len(observed),
        "status_code_counts": {"200": len(observed)},
    }


def validate_operator_readback(
    raw: Any,
    *,
    phase: str,
    expected_run_id: str,
    expected_run_attempt: str,
    expected_sha: str,
    expected_operation_id: str,
    mode: str,
    artifact_tuple: dict[str, str],
    template_sha256: str,
    producer_environment_sha256: str,
    expected_base_environment_sha256: dict[str, str],
    expected_operator_login: str,
    operator_permission: str,
    expected_environment: dict[str, str],
    expected_task_role_arn: str,
    expected_execution_role_arn: str,
    expected_image_uri: str,
    expected_secret_arn: str,
    expected_secret_json_key: str,
    expected_secret_version_id: str | None,
    earliest_allowed_at: datetime,
    latest_allowed_at: datetime,
) -> dict[str, Any]:
    """Validate and reduce a secret-stored operator readback to safe metadata."""

    _reject_sensitive_value_fields(raw)
    fields = {
        "schema_version",
        "phase",
        "run_id",
        "run_attempt",
        "workflow_path",
        "ref",
        "commit_sha",
        "operation_id",
        "mode",
        "checked_at_utc",
        "operator_login",
        "template_sha256",
        "producer_environment_sha256",
        "artifact",
        "database_readiness",
        "target_group",
        "readiness_probe",
        "snapshot",
    }
    _require(
        isinstance(raw, dict) and set(raw) == fields, "operator_readback_shape_invalid"
    )
    _require(
        raw.get("schema_version") == READBACK_SCHEMA, "operator_readback_schema_invalid"
    )
    _require(raw.get("phase") == phase, "operator_readback_phase_mismatch")
    _require(
        raw.get("run_id") == expected_run_id
        and re.fullmatch(r"[1-9][0-9]*", expected_run_id),
        "operator_readback_run_mismatch",
    )
    _require(
        raw.get("run_attempt") == expected_run_attempt == "1",
        "operator_readback_attempt_mismatch",
    )
    _require(
        raw.get("workflow_path") == DEPLOY_WORKFLOW_PATH,
        "operator_readback_workflow_mismatch",
    )
    _require(raw.get("ref") == RELEASE_REF, "operator_readback_ref_mismatch")
    _require(
        raw.get("commit_sha") == expected_sha
        and re.fullmatch(r"[0-9a-f]{40}", expected_sha),
        "operator_readback_commit_mismatch",
    )
    _canonical_uuid(raw.get("operation_id"), "operator_readback_operation_id_invalid")
    _require(
        raw.get("operation_id") == expected_operation_id,
        "operator_readback_operation_mismatch",
    )
    _require(raw.get("mode") == mode, "operator_readback_mode_mismatch")
    _require(
        raw.get("template_sha256") == template_sha256
        and _HEX64.fullmatch(template_sha256),
        "operator_readback_template_mismatch",
    )
    _require(
        raw.get("producer_environment_sha256") == producer_environment_sha256
        and _HEX64.fullmatch(producer_environment_sha256),
        "operator_readback_producer_environment_mismatch",
    )
    operator = raw.get("operator_login")
    _require(
        isinstance(operator, str) and re.fullmatch(r"[A-Za-z0-9-]{1,39}", operator),
        "operator_readback_operator_invalid",
    )
    _require(
        operator == expected_operator_login and operator_permission == "admin",
        "operator_readback_admin_not_verified",
    )
    checked_at = _timestamp(raw.get("checked_at_utc"), "operator_readback_time")
    _require(
        earliest_allowed_at <= checked_at <= latest_allowed_at,
        "operator_readback_stale_or_future",
    )
    _require(
        raw.get("artifact") == artifact_tuple,
        "operator_readback_artifact_tuple_mismatch",
    )

    readiness = raw.get("database_readiness")
    readiness_fields = {
        "schema",
        "task_ip_count",
        "matched_task_ip_count",
        "runtime_role_count",
        "api_runtime_role",
        "required_columns_present",
        "required_constraints_present",
        "required_privileges_present",
        "row_lock_privilege_present",
        "readiness_ok",
    }
    _require(
        isinstance(readiness, dict) and set(readiness) == readiness_fields,
        "database_readiness_shape_invalid",
    )
    _require(
        readiness.get("schema") == "asaas_registration_database_readiness.v1",
        "database_readiness_schema_invalid",
    )
    count = readiness.get("task_ip_count")
    _require(
        type(count) is int
        and count > 0
        and readiness.get("matched_task_ip_count") == count,
        "database_task_ip_binding_failed",
    )
    _require(
        readiness.get("runtime_role_count") == 1
        and isinstance(readiness.get("api_runtime_role"), str),
        "database_runtime_role_binding_failed",
    )
    _require(
        all(
            readiness.get(field) is True
            for field in (
                "required_columns_present",
                "required_constraints_present",
                "required_privileges_present",
                "row_lock_privilege_present",
                "readiness_ok",
            )
        ),
        "database_readiness_failed",
    )

    snapshot = raw.get("snapshot")
    _require(isinstance(snapshot, dict), "operator_snapshot_invalid")
    service = snapshot.get("service")
    _require(isinstance(service, dict), "operator_snapshot_service_invalid")
    load_balancers = service.get("loadBalancers")
    _require(
        isinstance(load_balancers, list) and len(load_balancers) == 1,
        "service_load_balancer_binding_invalid",
    )
    load_balancer = load_balancers[0]
    _require(
        isinstance(load_balancer, dict)
        and load_balancer.get("containerName") == APP_CONTAINER
        and type(load_balancer.get("containerPort")) is int,
        "service_load_balancer_binding_invalid",
    )
    snapshot_tasks = snapshot.get("tasks")
    _require(isinstance(snapshot_tasks, list), "operator_snapshot_tasks_invalid")
    private_ips = [
        task.get("privateIp") for task in snapshot_tasks if isinstance(task, dict)
    ]
    target = _target_group_check(
        raw.get("target_group"),
        desired_count=service.get("desiredCount", -1),
        target_group_arn=load_balancer.get("targetGroupArn"),
        container_port=load_balancer["containerPort"],
        expected_task_ips=private_ips,
        readback_checked_at=checked_at,
        earliest_allowed_at=earliest_allowed_at,
        latest_allowed_at=latest_allowed_at,
    )
    readiness_probe = _task_readiness_check(
        raw.get("readiness_probe"),
        expected_task_ips=private_ips,
        readback_checked_at=checked_at,
        earliest_allowed_at=earliest_allowed_at,
        latest_allowed_at=latest_allowed_at,
    )
    expected_digest = (
        "current" if phase == "before" else artifact_tuple["app_image_digest"]
    )
    try:
        checked_snapshot = validate_deployment_snapshot(
            snapshot,
            expected_digest=expected_digest,
            expected_image_uri=expected_image_uri,
            expected_task_role_arn=expected_task_role_arn,
            expected_execution_role_arn=expected_execution_role_arn,
            expected_secret_arn=expected_secret_arn,
            expected_secret_json_key=expected_secret_json_key,
            expected_secret_version_id=expected_secret_version_id,
            expected_task_environment=expected_environment,
            expected_base_environment_sha256=expected_base_environment_sha256,
            phase="before" if phase == "before" else "after",
            external_readiness_verified=True,
        )
    except (RolloutError, ReferenceError) as exc:
        raise EvidenceError(f"operator_snapshot_{exc}") from None
    checked_snapshot["readiness"] = "healthy"
    return {
        "schema_version": READBACK_SCHEMA,
        "phase": phase,
        "operator_login": operator,
        "operator_permission": operator_permission,
        "checked_at_utc": raw["checked_at_utc"],
        "template_sha256": template_sha256,
        "producer_environment_sha256": producer_environment_sha256,
        "base_environment_sha256": dict(
            sorted(expected_base_environment_sha256.items())
        ),
        "database_runtime_role": readiness["api_runtime_role"],
        "readiness": "ready",
        "readiness_probe": readiness_probe,
        "target_group": target,
        "snapshot": checked_snapshot,
        "snapshot_sha256": canonical_sha256(checked_snapshot),
    }


def validate_before_after(
    before: dict[str, Any], after: dict[str, Any], *, expected_digest: str
) -> dict[str, Any]:
    _require(
        before.get("phase") == "before" and after.get("phase") == "after",
        "operator_readback_order_invalid",
    )
    _require(
        before.get("operator_login")
        == after.get("operator_login")
        == before.get("operator_permission")
        == after.get("operator_permission"),
        "operator_readback_admin_mismatch",
    )
    _require(
        before.get("template_sha256") == after.get("template_sha256"),
        "operator_readback_template_changed",
    )
    _require(
        before.get("base_environment_sha256") == after.get("base_environment_sha256"),
        "base_task_environment_changed",
    )
    _require(
        before.get("target_group", {}).get("target_group_arn")
        == after.get("target_group", {}).get("target_group_arn"),
        "target_group_changed",
    )
    _require(
        before.get("target_group", {}).get("health_check_path")
        == after.get("target_group", {}).get("health_check_path"),
        "target_group_health_check_path_changed",
    )
    _require(
        before.get("readiness_probe", {}).get("ready_count")
        == before.get("snapshot", {}).get("task_count"),
        "pre_task_readiness_probe_failed",
    )
    _require(
        after.get("readiness_probe", {}).get("ready_count")
        == after.get("snapshot", {}).get("task_count"),
        "post_task_readiness_probe_failed",
    )
    try:
        checked = compare_rollout_readbacks(
            before["snapshot"], after["snapshot"], expected_digest=expected_digest
        )
    except RolloutError as exc:
        raise EvidenceError(f"rollout_readback_{exc}") from None
    _require(
        before["target_group"].get("healthy_count")
        == before["snapshot"].get("task_count"),
        "pre_target_group_unhealthy",
    )
    _require(
        after["target_group"].get("healthy_count")
        == after["snapshot"].get("task_count"),
        "post_target_group_unhealthy",
    )
    return {
        "schema_version": "asaas_producer_image_rollout_operator_verification.v1",
        "app_image_digest": expected_digest,
        "task_count": after["snapshot"]["task_count"],
        "previous_task_definition_arn": before["snapshot"]["task_definition_arn"],
        "active_task_definition_arn": after["snapshot"]["task_definition_arn"],
        "base_environment_sha256": after["base_environment_sha256"],
        "database_secret_version_id": after["snapshot"]["database_secret_version_id"],
        "readiness": "healthy",
        "target_group_health_check_path": after["target_group"]["health_check_path"],
        "task_readiness_probe": after["readiness_probe"],
        "commercial_gates": "disabled",
        "before_snapshot_sha256": before["snapshot_sha256"],
        "after_snapshot_sha256": after["snapshot_sha256"],
        "verified": checked["schema"] == "asaas_producer_image_rollout_verification.v1",
    }


def generate_post_readback_key() -> tuple[bytes, bytes]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


def encrypt_operator_snapshot(
    snapshot: Any, public_pem: bytes, context: dict[str, str]
) -> dict[str, str]:
    """Encrypt a post-readback JSON for a public, run-bound GitHub comment."""

    _reject_sensitive_value_fields(snapshot)
    _require(
        set(context)
        == {"run_id", "run_attempt", "operation_id", "phase", "artifact_sha256"},
        "encryption_context_invalid",
    )
    _require(context.get("phase") == "after", "post_snapshot_phase_invalid")
    try:
        public_key = serialization.load_pem_public_key(public_pem)
    except (ValueError, TypeError):
        raise EvidenceError("post_snapshot_public_key_invalid") from None
    _require(
        isinstance(public_key, rsa.RSAPublicKey) and public_key.key_size >= 3072,
        "post_snapshot_public_key_invalid",
    )
    public_fingerprint = hashlib.sha256(public_pem).hexdigest()
    symmetric_key = os.urandom(32)
    nonce = os.urandom(12)
    payload = _canonical(snapshot)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    associated_data = _canonical(context)
    ciphertext = AESGCM(symmetric_key).encrypt(nonce, payload, associated_data)
    wrapped_key = public_key.encrypt(
        symmetric_key,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return {
        "schema_version": POST_COMMENT_SCHEMA,
        **context,
        "recipient_public_key_sha256": public_fingerprint,
        "wrapped_key_b64": base64.b64encode(wrapped_key).decode("ascii"),
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
    }


def decrypt_operator_snapshot(
    envelope: Any, private_pem: bytes, *, expected_context: dict[str, str]
) -> Any:
    expected_fields = {
        "schema_version",
        "run_id",
        "run_attempt",
        "operation_id",
        "phase",
        "artifact_sha256",
        "recipient_public_key_sha256",
        "wrapped_key_b64",
        "nonce_b64",
        "ciphertext_b64",
    }
    _require(
        isinstance(envelope, dict) and set(envelope) == expected_fields,
        "post_snapshot_envelope_shape_invalid",
    )
    _require(
        envelope.get("schema_version") == POST_COMMENT_SCHEMA,
        "post_snapshot_envelope_schema_invalid",
    )
    context = {
        field: envelope[field]
        for field in (
            "run_id",
            "run_attempt",
            "operation_id",
            "phase",
            "artifact_sha256",
        )
    }
    _require(context == expected_context, "post_snapshot_envelope_context_mismatch")
    try:
        private_key = serialization.load_pem_private_key(private_pem, password=None)
    except (ValueError, TypeError):
        raise EvidenceError("post_snapshot_private_key_invalid") from None
    _require(
        isinstance(private_key, rsa.RSAPrivateKey) and private_key.key_size >= 3072,
        "post_snapshot_private_key_invalid",
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    _require(
        hashlib.sha256(public_pem).hexdigest()
        == envelope["recipient_public_key_sha256"],
        "post_snapshot_key_fingerprint_mismatch",
    )
    try:
        wrapped = base64.b64decode(envelope["wrapped_key_b64"], validate=True)
        nonce = base64.b64decode(envelope["nonce_b64"], validate=True)
        ciphertext = base64.b64decode(envelope["ciphertext_b64"], validate=True)
        symmetric_key = private_key.decrypt(
            wrapped,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None,
            ),
        )
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        plaintext = AESGCM(symmetric_key).decrypt(
            nonce, ciphertext, _canonical(context)
        )
    except (InvalidTag, ValueError, TypeError):
        raise EvidenceError("post_snapshot_decryption_failed") from None
    value = _strict_loads(plaintext, max_bytes=1_000_000)
    _reject_sensitive_value_fields(value)
    return value


def parse_post_readback_comment(
    body: Any,
    *,
    author_login: str,
    author_permission: str,
    expected_operator_login: str,
    private_pem: bytes,
    expected_context: dict[str, str],
) -> Any:
    """Accept exactly one encrypted marker from an authenticated repository admin."""

    _require(
        author_login == expected_operator_login and author_permission == "admin",
        "post_readback_author_not_admin",
    )
    _require(
        isinstance(body, str) and len(body.encode("utf-8")) <= 64_000,
        "post_readback_comment_invalid",
    )
    marker_start = "<!-- asaas-producer-post-readback.v1\n"
    marker_end = "\n-->"
    _require(
        body.startswith(marker_start) and body.endswith(marker_end),
        "post_readback_comment_marker_invalid",
    )
    encoded = body[len(marker_start) : -len(marker_end)]
    _require(
        encoded and "\n" not in encoded and "\r" not in encoded,
        "post_readback_comment_payload_invalid",
    )
    envelope = _strict_loads(encoded, max_bytes=48_000)
    return decrypt_operator_snapshot(
        envelope, private_pem, expected_context=expected_context
    )


def _private_file(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(_canonical(value) + b"\n")
        handle.flush()
        os.fsync(handle.fileno())


def _private_bytes(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(value)
        handle.flush()
        os.fsync(handle.fileno())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate-template")
    keypair = commands.add_parser("new-post-readback-key")
    keypair.add_argument("--private-output", type=Path, required=True)
    keypair.add_argument("--public-output", type=Path, required=True)
    build = commands.add_parser("prepare-task-definition")
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--app-digest", required=True)
    build.add_argument("--image-uri", required=True)
    build.add_argument("--database-value-from", required=True)
    encrypt = commands.add_parser("encrypt-post-readback")
    encrypt.add_argument("--public-key", type=Path, required=True)
    encrypt.add_argument("--snapshot", type=Path, required=True)
    encrypt.add_argument("--output", type=Path, required=True)
    encrypt.add_argument("--run-id", required=True)
    encrypt.add_argument("--run-attempt", required=True)
    encrypt.add_argument("--operation-id", required=True)
    encrypt.add_argument("--artifact-sha256", required=True)
    args = parser.parse_args(argv)
    try:
        template = None
        if args.command in {"validate-template", "prepare-task-definition"}:
            template = _strict_loads(
                os.environ.get("ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_JSON", "")
            )
        if args.command == "new-post-readback-key":
            private_pem, public_pem = generate_post_readback_key()
            _private_bytes(args.private_output, private_pem)
            _private_bytes(args.public_output, public_pem)
            print("post_readback_key: ready")
        elif args.command == "validate-template":
            _require(template is not None, "task_definition_template_missing")
            validated = validate_task_definition_template(template)
            expected = os.environ.get(
                "ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_SHA256", ""
            )
            _require(
                _HEX64.fullmatch(expected) is not None
                and validated["template_sha256"] == expected,
                "task_definition_template_sha256_mismatch",
            )
            print("task_definition_template: ready")
        elif args.command == "prepare-task-definition":
            _require(template is not None, "task_definition_template_missing")
            request = build_replacement_task_definition(
                template,
                image_uri=args.image_uri,
                app_digest=args.app_digest,
                pinned_database_value_from=args.database_value_from,
            )
            _private_file(args.output, request)
            print("task_definition_request: ready")
        else:
            raw_snapshot = args.snapshot.read_bytes()
            snapshot = _strict_loads(raw_snapshot, max_bytes=1_000_000)
            context = {
                "run_id": args.run_id,
                "run_attempt": args.run_attempt,
                "operation_id": _canonical_uuid(
                    args.operation_id, "operation_id_invalid"
                ),
                "phase": "after",
                "artifact_sha256": args.artifact_sha256,
            }
            _require(
                _HEX64.fullmatch(args.artifact_sha256) is not None,
                "artifact_sha256_invalid",
            )
            public_key = args.public_key.read_bytes()
            encrypted = encrypt_operator_snapshot(snapshot, public_key, context)
            _private_file(args.output, encrypted)
            print("encrypted_post_readback: ready")
        return 0
    except (OSError, EvidenceError, RolloutError, ReferenceError) as exc:
        reason = (
            str(exc)
            if isinstance(exc, (EvidenceError, RolloutError, ReferenceError))
            else "private_input_unreadable"
        )
        print(f"blocked: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
