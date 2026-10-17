#!/usr/bin/env python3
"""Deploy the signed ASAAS producer digest and roll back on failed acceptance.

AWS calls are made only by the protected OIDC deployment job. AWS CLI output is
captured in memory, projected to the fields required for validation, and never
printed or uploaded in raw form. The script never calls GetSecretValue.
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from scripts import asaas_build_release_preflight as github_preflight
from scripts.asaas_database_secret_reference import (
    ReferenceError,
    resolve_active_database_secret,
)
from scripts.asaas_producer_rollout_evidence import (
    EvidenceError,
    _canonical,
    _strict_loads,
    build_replacement_task_definition,
    parse_post_readback_comment,
    validate_before_after,
    validate_operator_readback,
    validate_registered_task_definition,
    validate_task_definition_template,
)
from scripts.asaas_producer_rollout_gate import (
    API_PREFIX,
    API_ROOT,
    DEPLOY_WORKFLOW_PATH,
    EXECUTION_ROLE_ARN,
    REPOSITORY,
    TASK_ROLE_ARN,
    GateError,
    _expected_task_environment,
    _time,
    validate_current_run,
    validate_deployment_history,
    validate_private_before_readback,
    validate_repository_protections,
)
from scripts.verify_asaas_producer_image_rollout import (
    CLUSTER_ARN,
    SERVICE_ARN,
    SERVICE_NAME,
    TASK_FAMILY,
    RolloutError,
    validate_signed_attestation,
)

MAX_POST_READBACK_WAIT_SECONDS = 900
MAX_REGISTRATION_WAIT_SECONDS = 900
AWS_CALL_TIMEOUT_SECONDS = 45
AWS_ACCOUNT_ID = "388460142431"
AWS_REGION = "us-east-1"


class DeployError(ValueError):
    """The deployment operation failed its bounded rollout contract."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise DeployError(reason)


def _aws_json(arguments: list[str], *, timeout: int = AWS_CALL_TIMEOUT_SECONDS) -> Any:
    command = [
        "aws",
        *arguments,
        "--region",
        AWS_REGION,
        "--output",
        "json",
        "--no-cli-pager",
    ]
    environment = dict(os.environ)
    environment["AWS_PAGER"] = ""
    try:
        result = subprocess.run(
            command, check=False, capture_output=True, timeout=timeout, env=environment
        )
    except (OSError, subprocess.TimeoutExpired):
        raise DeployError("aws_readback_failed") from None
    if result.returncode != 0:
        raise DeployError("aws_operation_failed")
    try:
        return _strict_loads(result.stdout, max_bytes=4_000_000)
    except EvidenceError:
        raise DeployError("aws_response_invalid") from None


def _github_get(path: str, token: str) -> Any:
    value, _ = github_preflight.github_get(f"{API_ROOT}{API_PREFIX}{path}", token)
    return value


def _admin_permission(login: str, token: str) -> str:
    value = _github_get(f"/collaborators/{quote(login, safe='')}/permission", token)
    _require(
        isinstance(value, dict) and value.get("permission") == "admin",
        "operator_not_repository_admin",
    )
    return "admin"


def _artifact_tuple(env: dict[str, str]) -> dict[str, str]:
    return {
        "build_workflow_run_id": env["INPUT_BUILD_RUN_ID"],
        "attestation_id": env["INPUT_ATTESTATION_ID"],
        "app_image_digest": env["INPUT_APP_IMAGE_DIGEST"],
        "signed_envelope_sha256": env["INPUT_ENVELOPE_SHA256"],
    }


def _task_env(env: dict[str, str]) -> dict[str, str]:
    return _expected_task_environment(env)


def _build_trust_env(env: dict[str, str]) -> dict[str, str]:
    return {
        "BILLING_PRODUCER_BUILD_ISSUER": env.get("BILLING_PRODUCER_BUILD_ISSUER", ""),
        "BILLING_PRODUCER_BUILD_KMS_KEY_ARN": env.get(
            "BILLING_PRODUCER_BUILD_KMS_KEY_ARN", ""
        ),
        "BILLING_PRODUCER_BUILD_AUDIENCE": env.get(
            "BILLING_PRODUCER_BUILD_AUDIENCE", ""
        ),
        "BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM": env.get(
            "BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM", ""
        ),
        "BILLING_PRODUCER_BUILD_OIDC_SUBJECT": env.get(
            "BILLING_PRODUCER_BUILD_OIDC_SUBJECT", ""
        ),
        "BILLING_PRODUCER_ECR_REPOSITORY_URI": env.get(
            "BILLING_PRODUCER_ECR_REPOSITORY_URI", ""
        ),
        "INPUT_OPERATION_ID": env.get("INPUT_OPERATION_ID", ""),
    }


def validate_artifact_before_aws(
    env: dict[str, str], envelope: Any, trust: Any
) -> dict[str, str]:
    expected = _artifact_tuple(env)
    verified = validate_signed_attestation(
        envelope,
        trust_readback=trust,
        expected_build_run_id=expected["build_workflow_run_id"],
        expected_attestation_id=expected["attestation_id"],
        expected_digest=expected["app_image_digest"],
        expected_envelope_sha256=expected["signed_envelope_sha256"],
        env=_build_trust_env(env),
    )
    _require(
        envelope.get("operation_id") != env.get("INPUT_OPERATION_ID"),
        "deployment_operation_id_reused",
    )
    registration = None
    if env.get("INPUT_MODE") == "registered":
        registration = {
            "attestation_id": env.get("INPUT_REGISTRATION_ATTESTATION_ID", ""),
            "attestation_digest": env.get("INPUT_REGISTRATION_DIGEST", ""),
            "registered_at": env.get("INPUT_REGISTERED_AT_UTC", ""),
        }
    elif env.get("INPUT_MODE") != "bootstrap":
        raise DeployError("rollout_mode_invalid")
    from scripts.verify_asaas_producer_image_rollout import validate_registration

    validate_registration(registration, verified, mode=env["INPUT_MODE"])
    return verified


def _live_service() -> dict[str, Any]:
    raw = _aws_json(
        [
            "ecs",
            "describe-services",
            "--cluster",
            CLUSTER_ARN,
            "--services",
            SERVICE_ARN,
            "--query",
            "{services:services[].{serviceArn:serviceArn,serviceName:serviceName,status:status,desiredCount:desiredCount,runningCount:runningCount,taskDefinition:taskDefinition,deployments:deployments[].{status:status,taskDefinition:taskDefinition,rolloutState:rolloutState},loadBalancers:loadBalancers[].{targetGroupArn:targetGroupArn,containerName:containerName,containerPort:containerPort}},failures:failures}",
        ]
    )
    _require(
        isinstance(raw, dict) and raw.get("failures") == [],
        "ecs_service_readback_failed",
    )
    services = raw.get("services")
    _require(
        isinstance(services, list) and len(services) == 1,
        "ecs_service_readback_ambiguous",
    )
    service = services[0]
    _require(
        service.get("serviceArn") == SERVICE_ARN
        and service.get("serviceName") == SERVICE_NAME,
        "ecs_service_identity_mismatch",
    )
    return service


def _live_tasks(task_arns: list[str]) -> list[dict[str, Any]]:
    _require(
        task_arns and len(task_arns) <= 100 and len(task_arns) == len(set(task_arns)),
        "ecs_task_arn_list_invalid",
    )
    for arn in task_arns:
        _require(
            isinstance(arn, str)
            and re.fullmatch(
                r"arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/[A-Za-z0-9-]+",
                arn,
            ),
            "ecs_task_arn_invalid",
        )
    raw = _aws_json(
        [
            "ecs",
            "describe-tasks",
            "--cluster",
            CLUSTER_ARN,
            "--tasks",
            *task_arns,
            "--query",
            "{tasks:tasks[].{taskArn:taskArn,taskDefinitionArn:taskDefinitionArn,lastStatus:lastStatus,desiredStatus:desiredStatus,createdAt:createdAt,startedAt:startedAt,privateIpDetails:attachments[].details[?name=='privateIPv4Address'].value,containers:containers[].{name:name,lastStatus:lastStatus,healthStatus:healthStatus,imageDigest:imageDigest}},failures:failures}",
        ]
    )
    _require(
        isinstance(raw, dict) and raw.get("failures") == [], "ecs_task_readback_failed"
    )
    tasks = raw.get("tasks")
    _require(
        isinstance(tasks, list) and len(tasks) == len(task_arns),
        "ecs_task_cohort_readback_mismatch",
    )
    projected: list[dict[str, Any]] = []
    for task in tasks:
        values = task.get("privateIpDetails")
        pending = list(values) if isinstance(values, list) else []
        ips: list[str] = []
        while pending:
            value = pending.pop()
            if isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, str):
                ips.append(value)
        _require(len(ips) == 1, "ecs_task_private_ip_readback_invalid")
        try:
            address = ipaddress.ip_address(ips[0])
        except ValueError:
            raise DeployError("ecs_task_private_ip_invalid") from None
        _require(
            address.version == 4 and address.is_private, "ecs_task_private_ip_invalid"
        )
        projected.append(
            {key: value for key, value in task.items() if key != "privateIpDetails"}
            | {"privateIp": ips[0]}
        )
    return projected


def _validate_predeployment_live_cohort(
    snapshot: dict[str, Any],
    live_tasks: list[dict[str, Any]],
    *,
    task_definition_arn: str,
    app_image_digest: str,
) -> None:
    """Bind authenticated task metadata to the exact operator-attested cohort."""

    expected_arns = snapshot.get("task_arns")
    timestamps = snapshot.get("task_timestamps")
    _require(
        isinstance(expected_arns, list)
        and expected_arns
        and all(isinstance(arn, str) for arn in expected_arns)
        and len(expected_arns) == len(set(expected_arns)),
        "predeploy_task_arn_snapshot_invalid",
    )
    _require(
        isinstance(timestamps, list)
        and len(timestamps) == len(expected_arns)
        and all(
            isinstance(row, dict)
            and set(row) == {"task_arn", "created_at", "started_at"}
            and isinstance(row.get("task_arn"), str)
            and isinstance(row.get("created_at"), str)
            and isinstance(row.get("started_at"), str)
            for row in timestamps
        ),
        "predeploy_task_timestamps_missing_or_invalid",
    )
    _require(
        len(live_tasks) == len(expected_arns)
        and {task.get("taskArn") for task in live_tasks} == set(expected_arns),
        "active_task_cohort_changed_after_preflight",
    )
    expected_timestamps = {row["task_arn"]: row for row in timestamps}
    _require(
        len(expected_timestamps) == len(timestamps),
        "predeploy_task_timestamp_arn_duplicate",
    )
    expected_ips = snapshot.get("task_private_ips")
    _require(
        isinstance(expected_ips, list) and len(expected_ips) == len(expected_arns),
        "predeploy_task_ip_snapshot_invalid",
    )
    _require(
        {task.get("privateIp") for task in live_tasks} == set(expected_ips),
        "active_task_target_cohort_changed_after_preflight",
    )
    for task in live_tasks:
        task_arn = task["taskArn"]
        expected = expected_timestamps[task_arn]
        _require(
            task.get("taskDefinitionArn") == task_definition_arn,
            "active_task_definition_changed_after_preflight",
        )
        _require(
            task.get("createdAt") == expected["created_at"]
            and task.get("startedAt") == expected["started_at"],
            "active_task_timestamps_changed_after_preflight",
        )
        containers = [
            row
            for row in task.get("containers", [])
            if isinstance(row, dict) and row.get("name") == "app"
        ]
        _require(
            len(containers) == 1
            and containers[0].get("imageDigest") == app_image_digest,
            "active_task_digest_changed_after_preflight",
        )


def _secret_metadata(secret_arn: str) -> tuple[dict[str, Any], dict[str, Any]]:
    _require(
        re.fullmatch(
            r"arn:aws:secretsmanager:us-east-1:388460142431:secret:[A-Za-z0-9/_+=.@-]+",
            secret_arn,
        )
        is not None,
        "database_secret_arn_invalid",
    )
    secret = _aws_json(
        [
            "secretsmanager",
            "describe-secret",
            "--secret-id",
            secret_arn,
            "--query",
            "{ARN:ARN,LastChangedDate:LastChangedDate}",
        ]
    )
    versions = _aws_json(
        [
            "secretsmanager",
            "list-secret-version-ids",
            "--secret-id",
            secret_arn,
            "--include-deprecated",
            "--query",
            "{ARN:ARN,Versions:Versions[].{VersionId:VersionId,VersionStages:VersionStages}}",
        ]
    )
    _require(
        isinstance(secret, dict) and isinstance(versions, dict),
        "database_secret_metadata_unavailable",
    )
    return secret, versions


def _ecr_metadata(digest: str, repository_uri: str) -> dict[str, Any]:
    _require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is not None,
        "app_image_digest_invalid",
    )
    repository_name = repository_uri.split("/", 1)[1]
    raw = _aws_json(
        [
            "ecr",
            "describe-images",
            "--repository-name",
            repository_name,
            "--image-ids",
            f"imageDigest={digest}",
            "--query",
            "{imageDetails:imageDetails[].{imageDigest:imageDigest}}",
        ]
    )
    _require(
        isinstance(raw, dict) and isinstance(raw.get("imageDetails"), list),
        "ecr_digest_readback_failed",
    )
    return raw


def _assert_caller_account() -> None:
    identity = _aws_json(
        ["sts", "get-caller-identity", "--query", "{Account:Account,Arn:Arn}"]
    )
    _require(
        isinstance(identity, dict) and identity.get("Account") == AWS_ACCOUNT_ID,
        "aws_caller_account_mismatch",
    )
    arn = identity.get("Arn")
    _require(
        isinstance(arn, str) and ":assumed-role/" in arn and "asaas" in arn.lower(),
        "aws_caller_role_mismatch",
    )


def _service_for_snapshot(service: dict[str, Any]) -> dict[str, Any]:
    return service


def _live_snapshot(
    operator_snapshot: dict[str, Any],
    *,
    task_definition: dict[str, Any],
    expected_digest: str,
    image_uri: str,
    secret_arn: str,
) -> dict[str, Any]:
    service = _live_service()
    task_arns = [item["taskArn"] for item in operator_snapshot["tasks"]]
    tasks = _live_tasks(task_arns)
    secret, versions = _secret_metadata(secret_arn)
    ecr = _ecr_metadata(expected_digest, image_uri)
    return {
        "service": _service_for_snapshot(service),
        "tasks": tasks,
        "task_definition": task_definition,
        "ecr": ecr,
        "secret_description": secret,
        "secret_versions": versions,
    }


def _canonical_registration_comment(
    comments: list[Any],
    *,
    token: str,
    env: dict[str, str],
    verified_attestation: dict[str, str],
    not_before: datetime | None = None,
) -> dict[str, str] | None:
    marker = "<!-- asaas-producer-registration.v1\n"
    matching: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in comments:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("body"), str)
            or not row["body"].startswith(marker)
        ):
            continue
        body = row["body"]
        _require(body.endswith("\n-->"), "canonical_registration_evidence_invalid")
        try:
            candidate = _strict_loads(body[len(marker) : -4], max_bytes=8_192)
        except EvidenceError:
            raise DeployError("canonical_registration_evidence_invalid") from None
        if (
            isinstance(candidate, dict)
            and candidate.get("run_id") == env["GITHUB_RUN_ID"]
            and candidate.get("operation_id") == env["INPUT_OPERATION_ID"]
        ):
            matching.append((row, candidate))
    _require(len(matching) <= 1, "canonical_registration_evidence_count_invalid")
    if not matching:
        return None
    row, registration = matching[0]
    user = row.get("user")
    _require(
        isinstance(user, dict) and isinstance(user.get("login"), str),
        "registration_operator_invalid",
    )
    _require(
        registration.get("registrar_login") == user["login"],
        "canonical_registration_registrar_author_mismatch",
    )
    if not_before is not None:
        _require(
            _time(row.get("created_at"), "registration_comment_created_at")
            >= not_before,
            "registration_comment_precedes_post_readback",
        )
    _admin_permission(user["login"], token)
    expected_fields = {
        "schema_version",
        "run_id",
        "operation_id",
        "attestation_id",
        "attestation_digest",
        "registered_at",
        "registrar_login",
        "registrar_role",
    }
    _require(
        isinstance(registration, dict) and set(registration) == expected_fields,
        "canonical_registration_evidence_shape_invalid",
    )
    _require(
        registration.get("schema_version") == "asaas_producer_registration.v1",
        "canonical_registration_evidence_schema_invalid",
    )
    _require(
        registration.get("run_id") == env["GITHUB_RUN_ID"],
        "canonical_registration_run_mismatch",
    )
    _require(
        registration.get("operation_id") == env["INPUT_OPERATION_ID"],
        "canonical_registration_operation_mismatch",
    )
    _require(
        registration.get("attestation_id") == verified_attestation["attestation_id"],
        "canonical_registration_attestation_id_mismatch",
    )
    _require(
        registration.get("attestation_digest")
        == verified_attestation["attestation_digest"],
        "canonical_registration_digest_mismatch",
    )
    _require(
        registration.get("registrar_role") == "super_admin",
        "canonical_registration_super_admin_required",
    )
    _require(
        isinstance(registration.get("registrar_login"), str),
        "canonical_registration_registrar_invalid",
    )
    from scripts.verify_asaas_producer_image_rollout import validate_registration

    return validate_registration(
        {
            key: registration[key]
            for key in ("attestation_id", "attestation_digest", "registered_at")
        },
        verified_attestation,
        mode="registered",
    )


def _wait_for_canonical_registration(
    *,
    token: str,
    env: dict[str, str],
    verified_attestation: dict[str, str],
    not_before: datetime,
) -> dict[str, str]:
    deadline = time.monotonic() + MAX_REGISTRATION_WAIT_SECONDS
    while time.monotonic() < deadline:
        comments = _collect_comments(env.get("INPUT_EVIDENCE_ISSUE_NUMBER", ""), token)
        registration = _canonical_registration_comment(
            comments,
            token=token,
            env=env,
            verified_attestation=verified_attestation,
            not_before=not_before,
        )
        if registration is not None:
            return registration
        time.sleep(10)
    raise DeployError("canonical_registration_evidence_timeout")


def _collect_comments(issue_number: str, token: str) -> list[Any]:
    _require(
        re.fullmatch(r"[1-9][0-9]*", issue_number) is not None,
        "evidence_issue_number_invalid",
    )
    return github_preflight.github_collection(
        f"{API_ROOT}{API_PREFIX}/issues/{issue_number}/comments?per_page=100&page=1",
        token,
    )


def _deployment_job_started_at(run_id: str, token: str) -> datetime:
    jobs = github_preflight.github_collection(
        f"{API_ROOT}{API_PREFIX}/actions/runs/{run_id}/jobs?per_page=100&page=1",
        token,
        list_field="jobs",
    )
    matches = [
        row for row in jobs if isinstance(row, dict) and row.get("name") == "deploy"
    ]
    _require(
        len(matches) == 1 and isinstance(matches[0].get("started_at"), str),
        "deployment_job_start_unreadable",
    )
    return _time(matches[0]["started_at"], "deployment_job_started_at")


def _wait_for_encrypted_post_readback(
    *,
    env: dict[str, str],
    private_key_path: Path,
    before: dict[str, Any],
    verified_attestation: dict[str, str],
    deploy_started_at: datetime,
    expected_protection_hash: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    token = env.get("GH_TOKEN", "")
    _require(bool(token), "github_read_token_unavailable")
    context = {
        "run_id": env["GITHUB_RUN_ID"],
        "run_attempt": env["GITHUB_RUN_ATTEMPT"],
        "operation_id": env["INPUT_OPERATION_ID"],
        "phase": "after",
        "artifact_sha256": env["INPUT_ENVELOPE_SHA256"],
    }
    deadline = time.monotonic() + MAX_POST_READBACK_WAIT_SECONDS
    comments: list[Any] = []
    matching: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        comments = _collect_comments(env.get("INPUT_EVIDENCE_ISSUE_NUMBER", ""), token)
        matching = []
        for row in comments:
            if not isinstance(row, dict) or not isinstance(row.get("body"), str):
                continue
            marker = "<!-- asaas-producer-post-readback.v1\n"
            body = row["body"]
            if not body.startswith(marker) or not body.endswith("\n-->"):
                continue
            try:
                envelope = _strict_loads(body[len(marker) : -4], max_bytes=48_000)
            except EvidenceError:
                continue
            if isinstance(envelope, dict) and all(
                envelope.get(key) == value for key, value in context.items()
            ):
                matching.append(row)
        if matching:
            break
        time.sleep(10)
    _require(bool(matching), "post_readback_timeout")
    _require(len(matching) == 1, "post_readback_comment_ambiguous")
    private_pem = private_key_path.read_bytes()
    author = matching[0].get("user")
    _require(
        isinstance(author, dict) and isinstance(author.get("login"), str),
        "post_readback_author_invalid",
    )
    after_operator = parse_post_readback_comment(
        matching[0]["body"],
        author_login=author["login"],
        author_permission=_admin_permission(author["login"], token),
        expected_operator_login=before["operator_login"],
        private_pem=private_pem,
        expected_context=context,
    )
    artifact_tuple = _artifact_tuple(env)
    after_operator = validate_operator_readback(
        after_operator,
        phase="after",
        expected_run_id=env["GITHUB_RUN_ID"],
        expected_run_attempt=env["GITHUB_RUN_ATTEMPT"],
        expected_sha=env["GITHUB_SHA"],
        expected_operation_id=env["INPUT_OPERATION_ID"],
        mode=env["INPUT_MODE"],
        artifact_tuple=artifact_tuple,
        template_sha256=before["template_sha256"],
        producer_environment_sha256=before["producer_environment_sha256"],
        expected_base_environment_sha256=before["base_environment_sha256"],
        expected_operator_login=before["operator_login"],
        operator_permission="admin",
        expected_environment=_task_env(env),
        expected_task_role_arn=TASK_ROLE_ARN,
        expected_execution_role_arn=EXECUTION_ROLE_ARN,
        expected_image_uri=env["BILLING_PRODUCER_ECR_REPOSITORY_URI"],
        expected_secret_arn=before["validated_snapshot"]["database_secret_arn"],
        expected_secret_json_key=before["validated_snapshot"][
            "database_secret_json_key"
        ],
        expected_secret_version_id=before["database_secret_version_id"],
        earliest_allowed_at=deploy_started_at,
        latest_allowed_at=datetime.now(timezone.utc),
    )
    now = datetime.now(timezone.utc)
    checked_at = _time(after_operator["checked_at_utc"], "post_readback_checked_at")
    _require(now - checked_at <= timedelta(minutes=15), "post_readback_snapshot_stale")
    verified = validate_before_after(
        {
            "phase": "before",
            "operator_login": before["operator_login"],
            "operator_permission": "admin",
            "template_sha256": before["template_sha256"],
            "base_environment_sha256": before["base_environment_sha256"],
            "target_group": before["target_group"],
            "readiness_probe": before["readiness_probe"],
            "snapshot": before["validated_snapshot"],
            "snapshot_sha256": before["before_snapshot_sha256"],
        },
        after_operator,
        expected_digest=verified_attestation["app_image_digest"],
    )
    after_snapshot = after_operator["snapshot"]
    task_definition = _readback_task_definition_from_template(env)
    task_definition["taskDefinitionArn"] = after_snapshot["task_definition_arn"]
    task_definition["family"] = TASK_FAMILY
    task_definition["status"] = "ACTIVE"
    live = _live_snapshot(
        after_snapshot,
        task_definition=task_definition,
        expected_digest=verified_attestation["app_image_digest"],
        image_uri=env["BILLING_PRODUCER_ECR_REPOSITORY_URI"],
        secret_arn=after_snapshot["database_secret_arn"],
    )
    # Runner-authenticated service and task metadata independently binds the
    # operator attestation to this target without ListTasks/DescribeTaskDefinition.
    _require(
        live["service"]["taskDefinition"] == after_snapshot["task_definition_arn"],
        "live_service_task_definition_mismatch",
    )
    _require(
        live["service"]["desiredCount"] == after_snapshot["task_count"],
        "live_service_desired_count_mismatch",
    )
    _require(
        all(
            task.get("taskDefinitionArn") == after_snapshot["task_definition_arn"]
            for task in live["tasks"]
        ),
        "live_task_definition_mismatch",
    )
    _require(
        {task.get("privateIp") for task in live["tasks"]}
        == set(after_snapshot.get("task_private_ips", [])),
        "live_task_target_cohort_mismatch",
    )
    _require(
        all(
            container.get("imageDigest") == verified_attestation["app_image_digest"]
            for task in live["tasks"]
            for container in task.get("containers", [])
            if container.get("name") == "app"
        ),
        "live_task_digest_mismatch",
    )
    return after_operator, {**verified, "live_readback": "verified"}


def _readback_task_definition_from_template(env: dict[str, str]) -> dict[str, Any]:
    template = _strict_loads(
        env.get("ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_JSON", "")
    )
    definition = dict(template["task_definition"])
    # The registered request is separately validated and returned as a safe
    # summary; this local projection adds response identity only.
    definition["containerDefinitions"] = [
        dict(row) for row in definition["containerDefinitions"]
    ]
    return definition


def _wait_for_service(task_definition_arn: str, desired_count: int) -> dict[str, Any]:
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        service = _live_service()
        deployments = service.get("deployments")
        primary = [row for row in deployments or [] if row.get("status") == "PRIMARY"]
        _require(len(primary) == 1, "ecs_primary_deployment_ambiguous")
        state = primary[0].get("rolloutState")
        if state == "FAILED":
            raise DeployError("ecs_rollout_failed")
        if (
            service.get("taskDefinition") == task_definition_arn
            and service.get("desiredCount") == desired_count
            and service.get("runningCount") == desired_count
            and primary[0].get("taskDefinition") == task_definition_arn
            and state == "COMPLETED"
        ):
            return service
        time.sleep(10)
    raise DeployError("ecs_rollout_timeout")


def _update_service(task_definition_arn: str) -> None:
    _aws_json(
        [
            "ecs",
            "update-service",
            "--cluster",
            CLUSTER_ARN,
            "--service",
            SERVICE_ARN,
            "--task-definition",
            task_definition_arn,
            "--query",
            "{serviceArn:service.serviceArn,taskDefinition:service.taskDefinition,desiredCount:service.desiredCount}",
        ]
    )


def _rollback(previous_task_definition_arn: str, desired_count: int) -> None:
    _require(
        type(desired_count) is int and desired_count > 0,
        "rollback_baseline_desired_count_invalid",
    )
    _update_service(previous_task_definition_arn)
    _wait_for_service(previous_task_definition_arn, desired_count)


def _register_task_definition(
    request: dict[str, Any], expected_digest: str
) -> tuple[str, dict[str, Any]]:
    temporary = (
        Path(os.environ["RUNNER_TEMP"]) / "asaas-producer-register-task-definition.json"
    )
    from scripts.asaas_producer_rollout_gate import write_private_json

    write_private_json(temporary, request)
    response = _aws_json(
        [
            "ecs",
            "register-task-definition",
            "--cli-input-json",
            f"file://{temporary}",
            "--query",
            "taskDefinition",
        ],
        timeout=120,
    )
    summary = validate_registered_task_definition(
        {"taskDefinition": response},
        request,
        expected_digest=expected_digest,
    )
    return summary["task_definition_arn"], summary


def _private_template_and_before(
    env: dict[str, str], run: dict[str, Any]
) -> dict[str, Any]:
    token = env.get("GH_TOKEN", "")
    before_raw = _strict_loads(
        env.get("ASAAS_PRODUCER_PREDEPLOY_READBACK_JSON", ""), max_bytes=1_000_000
    )
    _require(
        isinstance(before_raw, dict)
        and isinstance(before_raw.get("operator_login"), str),
        "predeploy_operator_invalid",
    )
    operator_login = before_raw["operator_login"]
    operator_permission = _admin_permission(operator_login, token)
    protections = validate_repository_protections(token)
    protection_digest = (
        __import__("hashlib").sha256(_canonical(protections)).hexdigest()
    )
    _require(
        protection_digest == env.get("ASAAS_PREFLIGHT_PROTECTION_SHA256"),
        "github_controls_changed_after_preflight",
    )
    history = validate_deployment_history(
        token,
        current_run_id=run["run_id"],
        operation_id=env["INPUT_OPERATION_ID"],
        mode=env["INPUT_MODE"],
    )
    _require(history["current_run_present"], "current_rollout_missing_from_history")
    template_json = env.get("ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_JSON", "")
    template = _strict_loads(template_json)
    template_data = validate_task_definition_template(template)
    _require(
        template_data["template_sha256"]
        == env.get("ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_SHA256"),
        "task_definition_template_sha256_mismatch",
    )
    readback = validate_private_before_readback(
        env,
        expected_run=run,
        artifact_tuple=_artifact_tuple(env),
        template_json=template_json,
        readback_json=env.get("ASAAS_PRODUCER_PREDEPLOY_READBACK_JSON", ""),
        operator_login=operator_login,
        operator_permission=operator_permission,
    )
    _require(
        readback["before_snapshot_sha256"]
        == env.get("ASAAS_PREDEPLOY_READBACK_SHA256"),
        "predeploy_readback_changed_after_approval",
    )
    _require(
        readback["producer_environment_sha256"]
        == env.get("ASAAS_PREDEPLOY_PRODUCER_ENVIRONMENT_SHA256"),
        "producer_environment_changed_after_approval",
    )
    return {**readback, "template": template, "template_data": template_data}


def execute_rollout(env: dict[str, str]) -> dict[str, Any]:
    token = env.get("GH_TOKEN", "")
    _require(bool(token), "github_read_token_unavailable")
    _require(env.get("GITHUB_RUN_ATTEMPT") == "1", "rollout_run_attempt_must_be_first")
    run = validate_current_run(env, token, stage="deploy")
    _require(
        env.get("GITHUB_WORKFLOW_REF")
        == f"{REPOSITORY}/{DEPLOY_WORKFLOW_PATH}@refs/heads/main",
        "workflow_ref_mismatch",
    )
    _require(run["run_id"] == env["GITHUB_RUN_ID"], "actions_run_id_mismatch")

    private_before = _private_template_and_before(env, run)
    verified_attestation = validate_artifact_before_aws(
        env,
        _strict_loads(Path(env["ASAAS_SIGNED_ENVELOPE_PATH"]).read_bytes()),
        _strict_loads(env.get("ASAAS_PRODUCER_BUILD_TRUST_READBACK_JSON", "")),
    )
    _require(
        verified_attestation["app_image_digest"] == env["INPUT_APP_IMAGE_DIGEST"],
        "app_image_digest_mismatch",
    )
    _assert_caller_account()
    before_snapshot = private_before["validated_snapshot"]
    previous_task_definition_arn = before_snapshot["task_definition_arn"]
    desired_count = before_snapshot["task_count"]
    current_service = _live_service()
    _require(
        current_service.get("taskDefinition") == previous_task_definition_arn,
        "active_task_definition_changed_after_preflight",
    )
    _require(
        current_service.get("desiredCount") == desired_count
        and current_service.get("runningCount") == desired_count,
        "active_service_cohort_changed_after_preflight",
    )
    pre_live_tasks = _live_tasks(before_snapshot["task_arns"])
    _validate_predeployment_live_cohort(
        before_snapshot,
        pre_live_tasks,
        task_definition_arn=previous_task_definition_arn,
        app_image_digest=before_snapshot["app_image_digest"],
    )
    template_definition = private_before["template"]["task_definition"]
    db_source = next(
        row["valueFrom"]
        for container in template_definition["containerDefinitions"]
        for row in container.get("secrets", [])
        if row.get("name") == "DATABASE_URL"
    )
    secret_metadata, secret_versions = _secret_metadata(
        before_snapshot["database_secret_arn"]
    )
    try:
        database_binding = resolve_active_database_secret(
            {
                "desired_count": desired_count,
                "active_tasks": pre_live_tasks,
                "task_definitions": [
                    {
                        "taskDefinitionArn": private_before["template"][
                            "source_task_definition_arn"
                        ],
                        **template_definition,
                    }
                ],
                "secret_description": secret_metadata,
                "secret_versions": secret_versions,
            }
        )
    except ReferenceError as exc:
        raise DeployError(f"database_secret_{exc}") from None
    _require(
        db_source == before_snapshot["database_secret_arn"],
        "database_secret_reference_mismatch",
    )
    _require(
        database_binding["resolved_version_id"]
        == before_snapshot["database_secret_version_id"],
        "database_secret_version_changed_after_preflight",
    )
    _ecr_metadata(
        verified_attestation["app_image_digest"],
        env["BILLING_PRODUCER_ECR_REPOSITORY_URI"],
    )

    pinned_value_from = database_binding["replacement_value_from"]
    request = build_replacement_task_definition(
        private_before["template"],
        image_uri=env["BILLING_PRODUCER_ECR_REPOSITORY_URI"],
        app_digest=verified_attestation["app_image_digest"],
        pinned_database_value_from=pinned_value_from,
    )
    new_task_definition_arn = ""
    update_attempted = False
    try:
        new_task_definition_arn, task_definition_summary = _register_task_definition(
            request,
            verified_attestation["app_image_digest"],
        )
        update_attempted = True
        _update_service(new_task_definition_arn)
        _wait_for_service(new_task_definition_arn, desired_count)
        deploy_started_at = _deployment_job_started_at(run["run_id"], token)
        private_key_path = (
            Path(env["RUNNER_TEMP"]) / "asaas-producer-post-readback-private.pem"
        )
        # The preapproval job already validated the base task spec. The key is
        # generated only after OIDC role assumption and stays on this runner.
        if not private_key_path.exists():
            raise DeployError("post_readback_private_key_missing")
        after_operator, accepted = _wait_for_encrypted_post_readback(
            env=env,
            private_key_path=private_key_path,
            before=private_before,
            verified_attestation=verified_attestation,
            deploy_started_at=deploy_started_at,
            expected_protection_hash=env["ASAAS_PREFLIGHT_PROTECTION_SHA256"],
        )
        if env.get("INPUT_MODE") == "bootstrap":
            registration = _wait_for_canonical_registration(
                token=token,
                env=env,
                verified_attestation=verified_attestation,
                not_before=_time(
                    after_operator["checked_at_utc"], "post_readback_checked_at"
                ),
            )
            _require(
                registration["registration_attestation_id"]
                == verified_attestation["attestation_id"],
                "canonical_registration_attestation_mismatch",
            )
        return {
            "schema_version": "asaas_producer_deploy_result.v1",
            "operation_id": env["INPUT_OPERATION_ID"],
            "mode": env["INPUT_MODE"],
            "attestation_id": verified_attestation["attestation_id"],
            "attestation_digest": verified_attestation["attestation_digest"],
            "app_image_digest": verified_attestation["app_image_digest"],
            "task_definition_revision": task_definition_summary["revision"],
            "target_group_health": "healthy",
            "readiness": "healthy",
            "registration": accepted.get(
                "registration", "canonical_response_supplied_by_operator"
            ),
            "rollback": "not_required",
        }
    except (
        DeployError,
        EvidenceError,
        GateError,
        RolloutError,
        ReferenceError,
        OSError,
        ValueError,
        KeyError,
    ) as exc:
        if update_attempted:
            try:
                _rollback(previous_task_definition_arn, desired_count)
            except (DeployError, OSError, ValueError):
                raise DeployError("rollout_failed_and_rollback_failed") from None
            raise DeployError("rollout_failed_rolled_back") from None
        if isinstance(
            exc, (DeployError, GateError, EvidenceError, RolloutError, ReferenceError)
        ):
            raise DeployError(str(exc)) from None
        raise DeployError("rollout_pre_mutation_failed") from None


def main() -> int:
    try:
        result = execute_rollout(dict(os.environ))
        # Only non-secret, low-risk operation state is printed or written to
        # the summary; no ARN, host, env value, token, or secret reference.
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write("## ASAAS producer deployment verified\n\n")
            summary.write(f"- Mode: `{result['mode']}`\n")
            summary.write(f"- Operation: `{result['operation_id']}`\n")
            summary.write(f"- Attestation: `{result['attestation_id']}`\n")
            summary.write(f"- Image digest: `{result['app_image_digest']}`\n")
            summary.write("- Cohort/readiness: healthy\n")
            summary.write("- Commercial gates: disabled\n")
            summary.write("- Registration: canonical response verified\n")
            summary.write("- Rollback: not required\n")
        print("ASAAS producer deployment: verified")
        return 0
    except (
        DeployError,
        GateError,
        EvidenceError,
        RolloutError,
        ReferenceError,
        OSError,
        ValueError,
        KeyError,
    ) as exc:
        reason = (
            str(exc)
            if isinstance(
                exc,
                (DeployError, GateError, EvidenceError, RolloutError, ReferenceError),
            )
            else "rollout_failed"
        )
        print(f"ASAAS producer deployment blocked: {reason}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
