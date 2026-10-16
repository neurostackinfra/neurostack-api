#!/usr/bin/env python3
"""Read back the fixed ECS target and call the signed producer receipt API."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import select
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import UUID

if TYPE_CHECKING:
    from src.services.billing_producer_handoff import (
        ProducerHandoffRejected,
        ProducerReadback,
        compute_task_set_id,
        validate_readback,
    )

try:
    from scripts.staging_rehearsal_common import (
        CLUSTER_ARN,
        SECRET_REFERENCE_NAMES,
        SERVICE_ARN,
    )
except ImportError:
    from staging_rehearsal_common import (  # type: ignore[no-redef]
        CLUSTER_ARN,
        SECRET_REFERENCE_NAMES,
        SERVICE_ARN,
    )


def _load_producer_domain() -> None:
    """Load pure producer validation without importing the eager services facade."""

    if "ProducerReadback" in globals():
        return
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "services"
        / "billing_producer_handoff.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_asaas_billing_producer_handoff", source
    )
    if spec is None or spec.loader is None:
        raise HandoffCliError("producer validation module is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise HandoffCliError("producer validation module is unavailable") from None
    globals().update(
        {
            name: getattr(module, name)
            for name in (
                "ProducerHandoffRejected",
                "ProducerReadback",
                "compute_task_set_id",
                "validate_readback",
            )
        }
    )


API_BASE_URL = "https://staging.api.neurostack.global"
API_HOST = "staging.api.neurostack.global"
APP_CONTAINER = "app"
MAX_JWT_BYTES = 8192
MAX_READBACK_BYTES = 262144
MAX_BUILD_ATTESTATION_BYTES = 1_048_576
BUILD_ATTESTATION_REGISTRATION_PATH = "/v1/admin/billing/producer-build-attestations"
BUILD_ATTESTATION_REGISTRATION_SCHEMA = "billing_build_attestation_registration.v1"
BUILD_ATTESTATION_RESPONSE_FIELDS = frozenset(
    {"attestation_id", "attestation_digest", "registered_at"}
)
_JWT = re.compile(rb"^eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
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
_READBACK_SNAPSHOT_FIELDS = _READBACK_REQUEST_FIELDS | {
    "environment",
    "operational_role",
    "task_set_id",
}


class HandoffCliError(ValueError):
    """A sanitized CLI validation or preflight failure."""


def _load_signed_envelope_domain() -> Any:
    """Load canonical-envelope shape/digest checks without importing app settings."""

    if "_signed_envelope_domain" in globals():
        return globals()["_signed_envelope_domain"]
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "services"
        / "billing_signed_envelope.py"
    )
    spec = importlib.util.spec_from_file_location(
        "_asaas_billing_signed_envelope", source
    )
    if spec is None or spec.loader is None:
        raise HandoffCliError("canonical build attestation validator is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise HandoffCliError(
            "canonical build attestation validator is unavailable"
        ) from None
    globals()["_signed_envelope_domain"] = module
    return module


def _aws_json(
    args: Sequence[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Any:
    command = ["aws", *args, "--region", "us-east-1", "--output", "json"]
    try:
        result = runner(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=25,
        )
        return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        raise HandoffCliError("read-only AWS metadata request failed") from None


def _aws_readback(
    *,
    expected_app_digest: str,
    expected_bundle_digest: str,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    readiness_probe: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Build an allowlisted metadata snapshot without reading secret values."""

    if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_app_digest):
        raise HandoffCliError("expected application digest is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_bundle_digest):
        raise HandoffCliError("expected bundle digest is invalid")
    cluster = _aws_json(
        [
            "ecs",
            "describe-clusters",
            "--clusters",
            CLUSTER_ARN,
            "--query",
            "clusters[].{clusterArn:clusterArn,status:status}",
        ],
        runner=runner,
    )
    if len(cluster) != 1 or cluster[0].get("status") != "ACTIVE":
        raise HandoffCliError("allowlisted ECS cluster is not active")

    services = _aws_json(
        [
            "ecs",
            "describe-services",
            "--cluster",
            CLUSTER_ARN,
            "--services",
            SERVICE_ARN,
            "--query",
            "services[].{serviceArn:serviceArn,status:status,"
            "deploymentController:deploymentController.type,desiredCount:desiredCount,"
            "runningCount:runningCount,pendingCount:pendingCount,taskDefinition:taskDefinition,"
            "deployments:deployments[].{id:id,status:status,taskDefinition:taskDefinition,"
            "desiredCount:desiredCount,runningCount:runningCount,rolloutState:rolloutState}}",
        ],
        runner=runner,
    )
    if len(services) != 1:
        raise HandoffCliError("allowlisted ECS service is unavailable")
    service = services[0]
    if (
        service.get("serviceArn") != SERVICE_ARN
        or service.get("status") != "ACTIVE"
        or service.get("deploymentController") != "ECS"
        or service.get("desiredCount") != service.get("runningCount")
        or service.get("pendingCount") != 0
        or service.get("desiredCount", 0) < 1
    ):
        raise HandoffCliError("ECS service is not fully converged")
    active_deployments = [
        item
        for item in service.get("deployments", [])
        if item.get("runningCount", 0) > 0
    ]
    primary = [item for item in active_deployments if item.get("status") == "PRIMARY"]
    if (
        len(primary) != 1
        or len(active_deployments) != 1
        or primary[0].get("rolloutState") != "COMPLETED"
        or primary[0].get("desiredCount") != service["desiredCount"]
        or primary[0].get("runningCount") != service["runningCount"]
        or primary[0].get("taskDefinition") != service.get("taskDefinition")
    ):
        raise HandoffCliError("ECS deployment is not a single completed primary")

    task_arns = _aws_json(
        [
            "ecs",
            "list-tasks",
            "--cluster",
            CLUSTER_ARN,
            "--service-name",
            SERVICE_ARN,
            "--desired-status",
            "RUNNING",
            "--page-size",
            "100",
            "--query",
            "taskArns",
        ],
        runner=runner,
    )
    if (
        not isinstance(task_arns, list)
        or len(set(task_arns)) != len(task_arns)
        or len(task_arns) != service["runningCount"]
        or len(task_arns) != service["desiredCount"]
        or len(task_arns) > 1000
    ):
        raise HandoffCliError("running ECS task set does not match service count")
    tasks_result: list[dict[str, Any]] = []
    for index in range(0, len(task_arns), 100):
        tasks_result.extend(
            _aws_json(
                [
                    "ecs",
                    "describe-tasks",
                    "--cluster",
                    CLUSTER_ARN,
                    "--tasks",
                    *task_arns[index : index + 100],
                    "--query",
                    "tasks[].{taskArn:taskArn,taskDefinitionArn:taskDefinitionArn,lastStatus:lastStatus,"
                    "desiredStatus:desiredStatus,containers:containers[].{name:name,image:image,"
                    "imageDigest:imageDigest,lastStatus:lastStatus}}",
                ],
                runner=runner,
            )
        )
    described_arns = [task.get("taskArn") for task in tasks_result]
    if (
        len(tasks_result) != len(task_arns)
        or len(set(described_arns)) != len(described_arns)
        or set(described_arns) != set(task_arns)
    ):
        raise HandoffCliError("ECS task readback is incomplete")
    if any(
        task.get("lastStatus") != "RUNNING"
        or task.get("desiredStatus") != "RUNNING"
        or task.get("taskDefinitionArn") != service.get("taskDefinition")
        for task in tasks_result
    ):
        raise HandoffCliError("running ECS task does not match the active deployment")

    app_images: set[str] = set()
    app_image_uris: set[str] = set()
    for task in tasks_result:
        app = [
            container
            for container in task.get("containers", [])
            if container.get("name") == APP_CONTAINER
        ]
        if len(app) != 1 or app[0].get("lastStatus") != "RUNNING":
            raise HandoffCliError("running app container readback is incomplete")
        app_images.add(app[0].get("imageDigest", ""))
        app_image_uris.add(app[0].get("image", ""))
    if len(app_images) != 1 or len(app_image_uris) != 1:
        raise HandoffCliError("running app tasks do not share one immutable image")
    app_digest = next(iter(app_images))
    image_uri = next(iter(app_image_uris))
    if app_digest != expected_app_digest or "latest" in image_uri.lower():
        raise HandoffCliError("running app digest differs from the approved digest")
    image_parts = image_uri.split("/", 1)
    if len(image_parts) != 2 or ":" not in image_parts[1]:
        raise HandoffCliError("app image reference is not an ECR tag")
    registry = image_parts[0]
    repository, tag = image_parts[1].rsplit(":", 1)
    if registry != "388460142431.dkr.ecr.us-east-1.amazonaws.com" or not tag:
        raise HandoffCliError("app image reference is outside the ECR allowlist")
    ecr = _aws_json(
        [
            "ecr",
            "describe-images",
            "--repository-name",
            repository,
            "--image-ids",
            f"imageTag={tag}",
            "--query",
            "imageDetails[].{imageDigest:imageDigest,imageTags:imageTags}",
        ],
        runner=runner,
    )
    if len(ecr) != 1 or ecr[0].get("imageDigest") != app_digest:
        raise HandoffCliError("ECR tag does not resolve to the running image digest")

    task_definition = _aws_json(
        [
            "ecs",
            "describe-task-definition",
            "--task-definition",
            service["taskDefinition"],
            "--query",
            "taskDefinition.{family:family,revision:revision,containerDefinitions:"
            "containerDefinitions[].{name:name,image:image,environment:environment[?name=='BILLING_CONFIG_GENERATION' "
            "|| name=='BILLING_COMPATIBILITY_GENERATION' || name=='BILLING_BUNDLE_DIGEST' "
            "|| name=='BILLING_ROLLOUT_STATE' || name=='BILLING_TOPUP_COMMERCIAL_ENABLED' "
            "|| name=='NEUROSTACK_READINESS'],secrets:secrets[].{name:name,valueFrom:valueFrom}}}",
        ],
        runner=runner,
    )
    containers = task_definition.get("containerDefinitions", [])
    app_definitions = [item for item in containers if item.get("name") == APP_CONTAINER]
    if len(app_definitions) != 1:
        raise HandoffCliError("task definition app metadata is unavailable")
    if app_definitions[0].get("image") != image_uri:
        raise HandoffCliError(
            "running app image differs from the active task definition"
        )
    env_values = {
        item["name"]: item.get("value", "")
        for item in app_definitions[0].get("environment", [])
    }
    bundle_digest = env_values.get("BILLING_BUNDLE_DIGEST", "")
    config_generation = env_values.get("BILLING_CONFIG_GENERATION", "")
    compatibility_generation = env_values.get("BILLING_COMPATIBILITY_GENERATION", "")
    if (
        bundle_digest != expected_bundle_digest
        or not config_generation
        or not compatibility_generation
        or env_values.get("BILLING_ROLLOUT_STATE") != "disabled"
        or env_values.get("BILLING_TOPUP_COMMERCIAL_ENABLED") != "false"
    ):
        raise HandoffCliError(
            "task definition does not prove approved bundle and disabled gates"
        )

    secret_references = []
    for definition in containers:
        for reference in definition.get("secrets", []):
            # Only name and secret ARN are returned; Value/SecretString is never requested.
            value_from = reference.get("valueFrom", "")
            secret_name = reference.get("name", "")
            expected_secret = SECRET_REFERENCE_NAMES.get(secret_name)
            arn_match = re.fullmatch(
                r"arn:aws:secretsmanager:us-east-1:388460142431:secret:([^:]+)(?::.*)?",
                value_from,
            )
            if expected_secret is None or arn_match is None:
                raise HandoffCliError("secret reference metadata is not allowlisted")
            arn_secret_name = arn_match.group(1)
            generated_suffix = re.fullmatch(r"(.+)-[A-Za-z0-9]{6}", arn_secret_name)
            if generated_suffix is not None:
                arn_secret_name = generated_suffix.group(1)
            if arn_secret_name != expected_secret:
                raise HandoffCliError(
                    "secret reference name does not match its environment"
                )
            # ECS Secrets Manager references may end with :json-key:stage:version-id.
            # A base ARN ends in the generated secret-name suffix, which is not a
            # version ID. Mark an unpinned reference explicitly instead of
            # misreporting that suffix as runtime provenance.
            reference_parts = value_from.split(":")
            version = (
                reference_parts[-1]
                if len(reference_parts) >= 10 and reference_parts[-1]
                else "task-definition-unpinned"
            )
            secret_references.append(
                {
                    "name": secret_name,
                    "arn": value_from,
                    "version_id": version,
                }
            )
    if len(secret_references) != len(SECRET_REFERENCE_NAMES) or {
        reference["name"] for reference in secret_references
    } != set(SECRET_REFERENCE_NAMES):
        raise HandoffCliError("task definition secret references are incomplete")
    ready = readiness_probe() if readiness_probe is not None else _readiness_probe()
    if not ready:
        raise HandoffCliError("readiness probe failed")

    task_cohort = {
        "schema": "ecs-task-cohort.v1",
        "cluster_arn": CLUSTER_ARN,
        "service_arn": SERVICE_ARN,
        "deployment_id": primary[0]["id"],
        "task_definition_arn": service["taskDefinition"],
        "running_task_arns": sorted(task_arns),
    }
    task_set_id = (
        "ecs-task-cohort-sha256:"
        + hashlib.sha256(
            json.dumps(
                task_cohort,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
    )
    return {
        "cluster_arn": CLUSTER_ARN,
        "service_arn": SERVICE_ARN,
        "deployment_id": primary[0]["id"],
        "task_set_id": task_set_id,
        "deployment_controller": service["deploymentController"],
        "task_definition_arn": service["taskDefinition"],
        "task_arns": sorted(task_arns),
        "config_generation": config_generation,
        "compatibility_generation": compatibility_generation,
        "app_image_digest": app_digest,
        "ecr_image_digest": ecr[0]["imageDigest"],
        "bundle_digest": bundle_digest,
        "readiness": "ready",
        "rollout_state": "COMPLETED",
        "rollout_control": "disabled",
        "topup_commercial_enabled": False,
        "secret_references": secret_references,
    }


def _readiness_probe() -> bool:
    request = Request(f"{API_BASE_URL}/ready", method="GET")
    try:
        with urlopen(request, timeout=5) as response:
            if response.status != 200:
                return False
            result = json.loads(response.read(4096))
        return result.get("ready") is True
    except (OSError, URLError, HTTPError, json.JSONDecodeError):
        return False


def _read_inherited_fd(fd: int, *, label: str, limit: int) -> bytes:
    """Consume and close one non-standard inherited anonymous pipe/socket."""

    if fd in (0, 1, 2):
        raise HandoffCliError(f"{label} FD must not be stdin, stdout, or stderr")
    try:
        descriptor_stat = os.fstat(fd)
    except OSError:
        raise HandoffCliError(f"{label} FD is unavailable") from None
    chunks: list[bytes] = []
    total = 0
    try:
        if stat.S_ISREG(descriptor_stat.st_mode) or os.isatty(fd):
            raise HandoffCliError(f"{label} FD must be an inherited pipe or socket")
        if not stat.S_ISFIFO(descriptor_stat.st_mode) and not stat.S_ISSOCK(
            descriptor_stat.st_mode
        ):
            raise HandoffCliError(f"{label} FD must be an inherited pipe or socket")
        while True:
            readable, _, _ = select.select([fd], [], [], 3)
            if not readable:
                raise HandoffCliError(f"{label} FD read timed out")
            chunk = os.read(fd, min(8192, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise HandoffCliError(f"{label} exceeds the size limit")
    except OSError:
        raise HandoffCliError(f"{label} FD could not be consumed") from None
    finally:
        os.close(fd)
    return b"".join(chunks).strip()


def read_auth_fd(fd: int) -> str:
    """Consume and close one inherited anonymous pipe/socket containing a JWT."""

    token_bytes = _read_inherited_fd(fd, label="authentication", limit=MAX_JWT_BYTES)
    if not _JWT.fullmatch(token_bytes):
        raise HandoffCliError("authentication FD did not contain a compact JWT")
    try:
        return token_bytes.decode("ascii")
    except UnicodeDecodeError:
        raise HandoffCliError("authentication token encoding is invalid") from None


def _strict_json_object(raw: bytes, *, label: str) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise HandoffCliError(f"{label} contains duplicate fields")
            result[key] = value
        return result

    def reject_number(_value: str) -> Any:
        raise HandoffCliError(f"{label} contains an unsupported number")

    try:
        value = json.loads(
            raw,
            object_pairs_hook=object_pairs,
            parse_float=reject_number,
            parse_constant=reject_number,
        )
    except HandoffCliError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise HandoffCliError(f"{label} is invalid JSON") from None
    if not isinstance(value, dict):
        raise HandoffCliError(f"{label} must be a JSON object")
    return value


def _read_build_attestation(path: Path) -> tuple[dict[str, Any], str, str]:
    """Read and validate one downloaded canonical build-attestation artifact."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise HandoffCliError(
            "build attestation artifact could not be opened"
        ) from None
    try:
        metadata = os.fstat(fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_size > MAX_BUILD_ATTESTATION_BYTES
        ):
            raise HandoffCliError(
                "build attestation artifact is not a bounded regular file"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, min(65536, MAX_BUILD_ATTESTATION_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > MAX_BUILD_ATTESTATION_BYTES:
                raise HandoffCliError(
                    "build attestation artifact exceeds the size limit"
                )
    except OSError:
        raise HandoffCliError("build attestation artifact could not be read") from None
    finally:
        os.close(fd)

    envelope = _strict_json_object(b"".join(chunks), label="build attestation artifact")
    domain = _load_signed_envelope_domain()
    try:
        domain.validate_envelope_shape(envelope)
        if envelope.get("type") != "billing_build_attestation.v1":
            raise ValueError("unexpected envelope type")
        payload = envelope["payload"]
        attestation_id = payload["attestation_id"]
        if str(UUID(attestation_id)) != attestation_id:
            raise ValueError("noncanonical attestation ID")
        digest = domain.envelope_digest(envelope)
    except (KeyError, TypeError, ValueError):
        # Trust/signature acceptance remains the backend's responsibility.
        raise HandoffCliError(
            "build attestation artifact failed schema validation"
        ) from None
    return envelope, attestation_id, digest


def _read_cli_readback(
    fd: int,
    *,
    expected_app_digest: str,
    expected_bundle_digest: str,
) -> dict[str, Any]:
    """Read and validate the exact structured snapshot emitted by `preflight`."""

    _load_producer_domain()
    document = _strict_json_object(
        _read_inherited_fd(fd, label="readback", limit=MAX_READBACK_BYTES),
        label="readback",
    )
    if set(document) != {"status", "readback"} or document.get("status") != "ready":
        raise HandoffCliError("readback document does not match the preflight schema")
    raw_readback = document.get("readback")
    if (
        not isinstance(raw_readback, dict)
        or set(raw_readback) != _READBACK_SNAPSHOT_FIELDS
    ):
        raise HandoffCliError("readback snapshot has missing or extra fields")
    if (
        raw_readback.get("environment") != "staging"
        or raw_readback.get("operational_role") != "production-serving"
    ):
        raise HandoffCliError("readback target is outside the approved scope")
    if (
        raw_readback.get("app_image_digest") != expected_app_digest
        or raw_readback.get("bundle_digest") != expected_bundle_digest
    ):
        raise HandoffCliError("readback differs from the approved app or bundle digest")
    task_arns = raw_readback.get("task_arns")
    secret_references = raw_readback.get("secret_references")
    if not isinstance(task_arns, list) or not all(
        isinstance(value, str) for value in task_arns
    ):
        raise HandoffCliError("readback task cohort is invalid")
    if not isinstance(secret_references, list) or not all(
        isinstance(reference, dict)
        and set(reference) == {"name", "arn", "version_id"}
        and all(isinstance(reference[field], str) for field in reference)
        for reference in secret_references
    ):
        raise HandoffCliError("readback secret-reference metadata is invalid")
    if task_arns != sorted(task_arns) or secret_references != sorted(
        secret_references,
        key=lambda item: (item["name"], item["arn"], item["version_id"]),
    ):
        raise HandoffCliError("readback snapshot is not in canonical list order")
    scalar_fields = _READBACK_REQUEST_FIELDS - {
        "task_arns",
        "secret_references",
        "topup_commercial_enabled",
    }
    if any(
        not isinstance(raw_readback.get(field), str) for field in scalar_fields
    ) or not isinstance(raw_readback.get("topup_commercial_enabled"), bool):
        raise HandoffCliError("readback snapshot fields have invalid types")

    try:
        readback = ProducerReadback(
            cluster_arn=raw_readback["cluster_arn"],
            service_arn=raw_readback["service_arn"],
            deployment_id=raw_readback["deployment_id"],
            deployment_controller=raw_readback["deployment_controller"],
            task_definition_arn=raw_readback["task_definition_arn"],
            task_arns=tuple(task_arns),
            config_generation=raw_readback["config_generation"],
            compatibility_generation=raw_readback["compatibility_generation"],
            app_image_digest=raw_readback["app_image_digest"],
            ecr_image_digest=raw_readback["ecr_image_digest"],
            bundle_digest=raw_readback["bundle_digest"],
            readiness=raw_readback["readiness"],
            rollout_state=raw_readback["rollout_state"],
            rollout_control=raw_readback["rollout_control"],
            topup_commercial_enabled=raw_readback["topup_commercial_enabled"],
            secret_references=tuple(
                (item["name"], item["arn"], item["version_id"])
                for item in secret_references
            ),
        )
        validate_readback(readback)
    except (ProducerHandoffRejected, KeyError, TypeError):
        raise HandoffCliError("readback snapshot failed validation") from None
    if raw_readback.get("task_set_id") != compute_task_set_id(readback):
        raise HandoffCliError("readback task cohort digest does not match its snapshot")
    return readback.as_payload()


def _api_post(path: str, body: dict[str, Any], token: str) -> dict[str, Any]:
    return _api_post_endpoint(
        f"/v1/admin/billing/producer-receipts/{path}",
        body,
        token,
        error_label="producer receipt",
    )


def _api_post_endpoint(
    path: str,
    body: dict[str, Any],
    token: str,
    *,
    error_label: str,
    expected_status: int | None = None,
) -> dict[str, Any]:
    request = Request(
        f"{API_BASE_URL}{path}",
        data=json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            if (
                expected_status is not None
                and getattr(response, "status", None) != expected_status
            ):
                raise HandoffCliError(
                    f"{error_label} API returned an unexpected status"
                )
            payload = json.loads(response.read(32768))
    except HTTPError as exc:
        if _is_grant_window_rejection(exc):
            raise HandoffCliError(
                "grant TTL/window rejected; abandon the reserved assignment and "
                "authorization IDs, take a fresh metadata readback, and retry "
                "assignment plus authorization with new IDs"
            ) from None
        raise HandoffCliError(f"{error_label} API request failed") from None
    except (URLError, OSError, json.JSONDecodeError):
        # Never expose transport exception text, which may include request metadata.
        raise HandoffCliError(f"{error_label} API request failed") from None
    if not isinstance(payload, dict):
        raise HandoffCliError(f"{error_label} API returned an invalid response")
    return payload


def _is_grant_window_rejection(error: HTTPError) -> bool:
    """Recognize only backend-defined TTL/window failures for safe retry guidance."""

    try:
        raw = error.read(8193)
        if len(raw) > 8192:
            return False
        body = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(body, dict) or not isinstance(body.get("detail"), dict):
        return False
    detail = body["detail"]
    if detail.get("error") == "authorization_ttl_exceeds_assignment":
        return True
    return detail.get("error") == "producer_pair_blocked" and detail.get("reason") in {
        "grant validity window expired",
        "grant pair lacks the requested validity window",
    }


def _target_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--environment", required=True)
    parser.add_argument("--operational-role", required=True)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--cluster-arn", required=True)
    parser.add_argument("--service-arn", required=True)


def _assert_target(args: argparse.Namespace) -> None:
    if (
        args.environment != "staging"
        or args.operational_role != "production-serving"
        or args.account_id != "388460142431"
        or args.region != "us-east-1"
        or args.cluster_arn != CLUSTER_ARN
        or args.service_arn != SERVICE_ARN
    ):
        raise HandoffCliError(
            "target is outside the allowlisted staging production-serving scope"
        )


def _add_readback_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--expected-app-digest", required=True)
    parser.add_argument("--expected-bundle-digest", required=True)


def _add_readback_fd(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--readback-fd", type=int, required=True)


def _register_build_attestation(args: argparse.Namespace) -> int:
    envelope, expected_id, expected_digest = _read_build_attestation(args.artifact)
    token = read_auth_fd(args.auth_fd)
    response = _api_post_endpoint(
        BUILD_ATTESTATION_REGISTRATION_PATH,
        {
            "schema_version": BUILD_ATTESTATION_REGISTRATION_SCHEMA,
            "envelope": envelope,
        },
        token,
        error_label="build attestation registration",
        expected_status=201,
    )
    if set(response) != BUILD_ATTESTATION_RESPONSE_FIELDS:
        raise HandoffCliError(
            "build attestation registration response has missing or extra fields"
        )
    registered_id = response.get("attestation_id")
    try:
        canonical_id = str(UUID(registered_id))
    except (TypeError, ValueError):
        raise HandoffCliError("build attestation response ID is invalid") from None
    if registered_id != expected_id or registered_id != canonical_id:
        raise HandoffCliError("build attestation response ID does not match artifact")
    registered_digest = response.get("attestation_digest")
    if (
        not isinstance(registered_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", registered_digest)
        or registered_digest != expected_digest
    ):
        raise HandoffCliError(
            "build attestation response digest does not match artifact"
        )
    registered_at = response.get("registered_at")
    if not isinstance(registered_at, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z",
        registered_at,
    ):
        raise HandoffCliError("build attestation response timestamp is invalid")
    try:
        timestamp = datetime.fromisoformat(registered_at[:-1] + "+00:00")
    except ValueError:
        raise HandoffCliError(
            "build attestation response timestamp is invalid"
        ) from None
    if timestamp.utcoffset() != timezone.utc.utcoffset(timestamp):
        raise HandoffCliError("build attestation response timestamp is not UTC")
    print(
        json.dumps(
            {
                "attestation_id": registered_id,
                "attestation_digest": registered_digest,
                "registered_at": registered_at,
            },
            sort_keys=True,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_subparsers(dest="command", required=True)

    register_attestation = command.add_parser(
        "register-build-attestation",
        help="register one downloaded canonical build-attestation artifact",
    )
    register_attestation.add_argument("--artifact", type=Path, required=True)
    register_attestation.add_argument("--auth-fd", type=int, required=True)

    preflight = command.add_parser("preflight")
    _target_args(preflight)
    _add_readback_args(preflight)
    preflight.add_argument("--read-only", action="store_true", required=True)
    preflight.add_argument("--require-disabled", action="store_true", required=True)

    receipt = command.add_parser("receipt")
    receipt_commands = receipt.add_subparsers(dest="receipt_command", required=True)

    pair = receipt_commands.add_parser("preflight")
    _target_args(pair)
    pair.add_argument("--operation-id", required=True)
    pair.add_argument("--minimum-grant-validity-seconds", type=int, default=60)
    pair.add_argument("--minimum-pair-validity-seconds", type=int, default=300)
    pair.add_argument("--auth-fd", type=int, required=True)
    _add_readback_fd(pair)
    pair.add_argument("--assignment-id", required=True)
    pair.add_argument("--authorization-receipt-id", required=True)
    pair.add_argument("--authorization-scope", required=True)
    pair.add_argument("--max-authorization-ttl-seconds", type=int, required=True)
    pair.add_argument(
        "--resolve-current-pair-server-side", action="store_true", required=True
    )
    pair.add_argument(
        "--require-active-target-and-provenance", action="store_true", required=True
    )
    pair.add_argument(
        "--consume-read-only-authorization", action="store_true", required=True
    )
    pair.add_argument(
        "--no-receipt-or-pointer-mutation", action="store_true", required=True
    )
    pair.add_argument("--print-json", action="store_true", required=True)
    _add_readback_args(pair)

    grant = command.add_parser("grant")
    grant_commands = grant.add_subparsers(dest="grant_command", required=True)
    assignment = grant_commands.add_parser("assignment")
    _target_args(assignment)
    _add_readback_args(assignment)
    assignment.add_argument("--auth-fd", type=int, required=True)
    _add_readback_fd(assignment)
    assignment.add_argument("--assignment-id", required=True)
    assignment.add_argument("--operation-id", required=True)
    assignment.add_argument("--executor-user-id", required=True)
    assignment.add_argument(
        "--action",
        choices=("read_billing_producer_pair",),
        required=True,
    )
    assignment.add_argument("--ttl-seconds", type=int, required=True)
    assignment.add_argument("--minimum-grant-validity-seconds", type=int, default=60)
    assignment.add_argument("--minimum-pair-validity-seconds", type=int, default=300)

    authorization = grant_commands.add_parser("authorization")
    authorization.add_argument("--auth-fd", type=int, required=True)
    authorization.add_argument("--authorization-id", required=True)
    authorization.add_argument("--assignment-id", required=True)
    authorization.add_argument("--assignment-digest", required=True)
    authorization.add_argument("--ttl-seconds", type=int, required=True)

    create = receipt_commands.add_parser("create")
    create.add_argument("--auth-fd", type=int, required=True)
    create.add_argument("--assignment-id", required=True)
    create.add_argument("--authorization-receipt-id", required=True)
    create.add_argument("--authorization-scope", required=True)
    _target_args(create)
    create.add_argument("--operation-id", required=True)
    create.add_argument("--receipt-id", required=True)
    create.add_argument("--expected-verification-receipt-id", required=True)
    create.add_argument("--receipt-ttl-seconds", type=int, required=True)
    _add_readback_args(create)
    create.add_argument("--build-attestation-id", required=True)
    create.add_argument("--build-attestation-digest", required=True)
    create.add_argument("--commit", required=True)
    create.add_argument("--tree", required=True)
    create.add_argument("--workflow-id", required=True)
    for flag in (
        "--resolve-ecr-digest",
        "--require-image-digest-readback",
        "--require-live-readback",
        "--require-disabled",
        "--append-only",
        "--do-not-update-active-pair-pointer",
    ):
        create.add_argument(flag, action="store_true", required=True)
    create.add_argument("--expected-no-current-active-pair", action="store_true")
    create.add_argument("--expected-current-active-receipt-id")
    create.add_argument("--expected-current-active-receipt-digest")
    create.add_argument("--expected-current-active-verification-receipt-id")
    create.add_argument("--expected-current-active-verification-receipt-digest")

    verify = receipt_commands.add_parser("verify")
    verify.add_argument("--auth-fd", type=int, required=True)
    verify.add_argument("--assignment-id", required=True)
    verify.add_argument("--authorization-receipt-id", required=True)
    verify.add_argument("--authorization-scope", required=True)
    _target_args(verify)
    verify.add_argument("--operation-id", required=True)
    verify.add_argument("--receipt-id", required=True)
    verify.add_argument("--verification-receipt-id", required=True)
    _add_readback_args(verify)
    verify.add_argument("--build-attestation-id", required=True)
    verify.add_argument("--build-attestation-digest", required=True)
    verify.add_argument("--commit", required=True)
    verify.add_argument("--tree", required=True)
    verify.add_argument("--workflow-id", required=True)
    for flag in (
        "--resolve-ecr-digest",
        "--require-image-digest-readback",
        "--independent-readback",
        "--require-independent-live-readback-and-provenance",
        "--require-same-deployment-taskset-generations-app-bundle-digests",
        "--activate-current-pair-pointer-cas",
        "--reject-if-runtime-changed",
        "--reject-if-current-pointer-or-runtime-changed",
        "--reject-actor-equality",
    ):
        verify.add_argument(flag, action="store_true", required=True)
    verify.add_argument("--expected-no-current-active-pair", action="store_true")
    verify.add_argument("--expected-current-active-receipt-id")
    verify.add_argument("--expected-current-active-receipt-digest")
    verify.add_argument("--expected-current-active-verification-receipt-id")
    verify.add_argument("--expected-current-active-verification-receipt-digest")
    return parser


def _validate_common(args: argparse.Namespace) -> None:
    _assert_target(args)
    try:
        UUID(args.operation_id)
    except ValueError:
        raise HandoffCliError("operation ID must be a UUID") from None


def _load_cli_readback(args: argparse.Namespace) -> dict[str, Any]:
    _validate_common(args)
    return _aws_readback(
        expected_app_digest=args.expected_app_digest,
        expected_bundle_digest=args.expected_bundle_digest,
    )


def _api_readback(readback: dict[str, Any]) -> dict[str, Any]:
    """Send only request fields; the backend derives fixed scope and cohort ID."""

    return {
        key: value
        for key, value in readback.items()
        if key not in {"environment", "operational_role", "task_set_id"}
    }


def _assert_distinct_fds(auth_fd: int, readback_fd: int) -> None:
    if auth_fd == readback_fd:
        raise HandoffCliError("authentication and readback FDs must be distinct")


def _load_fd_readback(args: argparse.Namespace) -> dict[str, Any]:
    _assert_target(args)
    return _read_cli_readback(
        args.readback_fd,
        expected_app_digest=args.expected_app_digest,
        expected_bundle_digest=args.expected_bundle_digest,
    )


def _run(args: argparse.Namespace) -> int:
    if args.command == "register-build-attestation":
        return _register_build_attestation(args)

    _load_producer_domain()
    if args.command == "preflight":
        _assert_target(args)
        readback = _aws_readback(
            expected_app_digest=args.expected_app_digest,
            expected_bundle_digest=args.expected_bundle_digest,
        )
        readback.update(
            {
                "environment": "staging",
                "operational_role": "production-serving",
            }
        )
        readback["secret_references"] = sorted(
            readback["secret_references"],
            key=lambda item: (item["name"], item["arn"], item["version_id"]),
        )
        print(json.dumps({"status": "ready", "readback": readback}, sort_keys=True))
        return 0

    if args.command == "grant" and args.grant_command == "assignment":
        _validate_common(args)
        _assert_distinct_fds(args.auth_fd, args.readback_fd)
        if args.action != "read_billing_producer_pair":
            raise HandoffCliError("assignment action is not allowlisted")
        if (
            args.ttl_seconds < 1
            or args.ttl_seconds > 300
            or not 60 <= args.minimum_grant_validity_seconds <= 300
            or args.minimum_pair_validity_seconds != 300
        ):
            raise HandoffCliError("read-only assignment validity is invalid")
        for value, label in (
            (args.assignment_id, "assignment"),
            (args.executor_user_id, "executor"),
        ):
            try:
                UUID(value)
            except ValueError:
                raise HandoffCliError(f"{label} ID must be a UUID") from None
        readback = _load_fd_readback(args)
        token = read_auth_fd(args.auth_fd)
        response = _api_post_endpoint(
            "/v1/admin/billing/producer-grants/assignments",
            {
                "schema_version": "billing_producer_assignment_request.v1",
                "assignment_id": args.assignment_id,
                "operation_id": args.operation_id,
                "executor_user_id": args.executor_user_id,
                "action": args.action,
                "receipt_ids": [],
                "ttl_seconds": args.ttl_seconds,
                "readback": _api_readback(readback),
                "minimum_grant_validity_seconds": (args.minimum_grant_validity_seconds),
                "minimum_pair_validity_seconds": (args.minimum_pair_validity_seconds),
            },
            token,
            error_label="producer assignment",
        )
        print(json.dumps(response, sort_keys=True))
        return 0

    if args.command == "grant" and args.grant_command == "authorization":
        for value, label in (
            (args.authorization_id, "authorization"),
            (args.assignment_id, "assignment"),
        ):
            try:
                UUID(value)
            except ValueError:
                raise HandoffCliError(f"{label} ID must be a UUID") from None
        if not re.fullmatch(r"[0-9a-f]{64}", args.assignment_digest):
            raise HandoffCliError("assignment digest must be lowercase SHA-256 hex")
        if args.ttl_seconds < 1 or args.ttl_seconds > 600:
            raise HandoffCliError("authorization TTL must be at most 600 seconds")
        token = read_auth_fd(args.auth_fd)
        response = _api_post_endpoint(
            "/v1/admin/billing/producer-grants/authorizations",
            {
                "schema_version": "billing_producer_authorization_request.v1",
                "authorization_id": args.authorization_id,
                "assignment_id": args.assignment_id,
                "assignment_digest": args.assignment_digest,
                "ttl_seconds": args.ttl_seconds,
            },
            token,
            error_label="producer authorization",
        )
        print(json.dumps(response, sort_keys=True))
        return 0

    if args.receipt_command == "preflight":
        _assert_target(args)
        _assert_distinct_fds(args.auth_fd, args.readback_fd)
        if (
            args.authorization_scope != "read_billing_producer_pair"
            or args.max_authorization_ttl_seconds != 300
            or not 60 <= args.minimum_grant_validity_seconds <= 300
            or args.minimum_pair_validity_seconds != 300
        ):
            raise HandoffCliError("read-only preflight authorization scope is invalid")
        for value, label in (
            (args.assignment_id, "assignment"),
            (args.authorization_receipt_id, "authorization"),
        ):
            try:
                UUID(value)
            except ValueError:
                raise HandoffCliError(f"{label} ID must be a UUID") from None
        readback = _load_fd_readback(args)
        token = read_auth_fd(args.auth_fd)
        response = _api_post(
            "preflight",
            {
                "operation_id": args.operation_id,
                "assignment_id": args.assignment_id,
                "authorization_receipt_id": args.authorization_receipt_id,
                "readback": _api_readback(readback),
                "minimum_grant_validity_seconds": (args.minimum_grant_validity_seconds),
                "minimum_pair_validity_seconds": (args.minimum_pair_validity_seconds),
            },
            token,
        )
        action = response.get("action")
        active = response.get("current_active_pair")
        if action == "create" and active is not None:
            raise HandoffCliError("backend returned an inconsistent create preflight")
        if action in {"keep", "refresh"}:
            if not isinstance(active, dict) or set(active) != {
                "receipt_id",
                "receipt_digest",
                "verification_receipt_id",
                "verification_receipt_digest",
            }:
                raise HandoffCliError("backend returned an invalid active pair")
            try:
                UUID(active["receipt_id"])
                UUID(active["verification_receipt_id"])
            except (ValueError, TypeError):
                raise HandoffCliError(
                    "backend returned an invalid active pair"
                ) from None
            if not all(
                isinstance(active[key], str)
                and re.fullmatch(r"[0-9a-f]{64}", active[key])
                for key in ("receipt_digest", "verification_receipt_digest")
            ):
                raise HandoffCliError("backend returned an invalid active pair")
        if action not in {"create", "keep", "refresh"}:
            raise HandoffCliError("backend returned a blocked producer pair")
        print(
            json.dumps(
                {"action": action, "current_active_pair": active}, sort_keys=True
            )
        )
        return 0

    _validate_common(args)
    if args.receipt_ttl_seconds if args.receipt_command == "create" else False:
        if args.receipt_ttl_seconds != 604800:
            raise HandoffCliError("producer receipt TTL must be exactly seven days")
    expected_scope = (
        "create_billing_producer_deploy_receipt"
        if args.receipt_command == "create"
        else "verify_billing_producer_deploy_receipt"
    )
    if args.authorization_scope != expected_scope:
        raise HandoffCliError("authorization scope does not match the operation")
    if args.build_attestation_id and args.commit:
        try:
            UUID(args.build_attestation_id)
        except ValueError:
            raise HandoffCliError("build attestation ID must be a UUID") from None
    if not re.fullmatch(r"[0-9a-f]{64}", args.build_attestation_digest):
        raise HandoffCliError("build attestation digest is invalid")
    if not re.fullmatch(r"[0-9a-f]{64}", args.commit) or not re.fullmatch(
        r"[0-9a-f]{64}", args.tree
    ):
        raise HandoffCliError("approved commit and tree must be SHA-256 hex digests")
    readback = _load_cli_readback(args)
    token = read_auth_fd(args.auth_fd)
    if args.receipt_command == "create":
        current_values = (
            args.expected_current_active_receipt_id,
            args.expected_current_active_receipt_digest,
            args.expected_current_active_verification_receipt_id,
            args.expected_current_active_verification_receipt_digest,
        )
        if args.expected_no_current_active_pair:
            if any(value is not None for value in current_values):
                raise HandoffCliError(
                    "initial create cannot include an old active pair"
                )
        elif not all(current_values):
            raise HandoffCliError(
                "refresh create requires a complete active pair snapshot"
            )
        if not args.expected_no_current_active_pair:
            for value in (
                args.expected_current_active_receipt_id,
                args.expected_current_active_verification_receipt_id,
            ):
                try:
                    UUID(value)
                except (ValueError, TypeError):
                    raise HandoffCliError("active receipt IDs must be UUIDs") from None
            if not all(
                re.fullmatch(r"[0-9a-f]{64}", value or "")
                for value in (
                    args.expected_current_active_receipt_digest,
                    args.expected_current_active_verification_receipt_digest,
                )
            ):
                raise HandoffCliError("active receipt digests are invalid")
        body = {
            "operation_id": args.operation_id,
            "receipt_id": args.receipt_id,
            "verification_receipt_id": args.expected_verification_receipt_id,
            "assignment_id": args.assignment_id,
            "authorization_receipt_id": args.authorization_receipt_id,
            "readback": _api_readback(readback),
            "build_attestation_id": args.build_attestation_id,
            "build_attestation_digest": args.build_attestation_digest,
            "commit_sha": args.commit,
            "tree_sha": args.tree,
            "workflow_id": args.workflow_id,
        }
        if args.expected_no_current_active_pair:
            body["expected_no_current_active_pair"] = True
        else:
            body.update(
                {
                    "expected_current_receipt_id": args.expected_current_active_receipt_id,
                    "expected_current_receipt_digest": args.expected_current_active_receipt_digest,
                    "expected_current_verification_receipt_id": args.expected_current_active_verification_receipt_id,
                    "expected_current_verification_receipt_digest": args.expected_current_active_verification_receipt_digest,
                }
            )
        response = _api_post("create", body, token)
    else:
        current_values = (
            args.expected_current_active_receipt_id,
            args.expected_current_active_receipt_digest,
            args.expected_current_active_verification_receipt_id,
            args.expected_current_active_verification_receipt_digest,
        )
        if args.expected_no_current_active_pair and any(current_values):
            raise HandoffCliError(
                "initial verification cannot include an old active pair"
            )
        if any(current_values) and not all(current_values):
            raise HandoffCliError(
                "refresh verification requires complete active pair CAS values"
            )
        if not args.expected_no_current_active_pair and not any(current_values):
            raise HandoffCliError(
                "verification requires an initial-empty or explicit refresh CAS"
            )
        body = {
            "operation_id": args.operation_id,
            "receipt_id": args.receipt_id,
            "verification_receipt_id": args.verification_receipt_id,
            "assignment_id": args.assignment_id,
            "authorization_receipt_id": args.authorization_receipt_id,
            "readback": _api_readback(readback),
            "expected_no_current_active_pair": args.expected_no_current_active_pair,
        }
        if all(current_values):
            body.update(
                {
                    "expected_current_receipt_id": args.expected_current_active_receipt_id,
                    "expected_current_receipt_digest": args.expected_current_active_receipt_digest,
                    "expected_current_verification_receipt_id": args.expected_current_active_verification_receipt_id,
                    "expected_current_verification_receipt_digest": args.expected_current_active_verification_receipt_digest,
                }
            )
        response = _api_post("verify", body, token)
    safe_result = {
        key: response.get(key)
        for key in ("receipt_id", "receipt_type", "operation_id", "payload_digest")
        if key in response
    }
    print(json.dumps(safe_result, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    try:
        args = parser.parse_args(argv)
        return _run(args)
    except HandoffCliError as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 2
    except (ValueError, OSError):
        print("blocked: producer handoff input is invalid", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
