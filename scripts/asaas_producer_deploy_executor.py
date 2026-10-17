#!/usr/bin/env python3
"""Deploy the signed ASAAS producer digest and roll back on failed acceptance.

AWS calls are made only by the protected OIDC deployment job. AWS CLI output is
captured in memory, projected to the fields required for validation, and never
printed or uploaded in raw form. The script never calls GetSecretValue.
"""

from __future__ import annotations

import os
import re
import stat
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
    ENCRYPTED_READBACK_MARKER,
    EvidenceError,
    _canonical,
    _strict_loads,
    build_replacement_task_definition,
    canonical_sha256,
    parse_post_readback_comment,
    validate_before_after,
    validate_operator_readback,
    validate_pre_oidc_cohort_confirmation,
    validate_registered_task_definition,
    validate_task_definition_template,
)
from scripts.asaas_producer_rollout_gate import (
    API_PREFIX,
    API_ROOT,
    DEPLOY_ROLE_VARIABLE,
    DEPLOY_WORKFLOW_PATH,
    EXECUTION_ROLE_ARN,
    PINNED_DEPLOY_ROLE_ARN,
    REPOSITORY,
    TASK_ROLE_ARN,
    GateError,
    _expected_task_environment,
    _time,
    validate_current_run,
    validate_deployment_history,
    validate_private_before_readback,
    validate_repository_protections,
    validate_runtime_deploy_role_variable,
)
from scripts.verify_asaas_producer_image_rollout import (
    BUILD_WORKFLOW_PATH,
    BUILD_WORKFLOW_REF,
    CLUSTER_ARN,
    REPOSITORY_ID,
    SERVICE_ARN,
    SERVICE_NAME,
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


def _fetch_runtime_build_trust(
    env: dict[str, str], *, opener=None
) -> tuple[dict[str, str], str, str]:
    """Read and validate the trust served by the deployed public API."""
    from scripts.asaas_build_release_preflight import (
        BOOTSTRAP_BACKEND_ORIGIN,
        fetch_backend_trust_readback,
    )

    trust_env = _build_trust_env(env)
    pins = {
        "PIN_BACKEND_BASE_URL": BOOTSTRAP_BACKEND_ORIGIN,
        "ASAAS_PREFLIGHT_BACKEND_ORIGIN": BOOTSTRAP_BACKEND_ORIGIN,
        "PIN_REPOSITORY_ID": REPOSITORY_ID,
        "PIN_REPOSITORY_NAME": REPOSITORY,
        "PIN_WORKFLOW_PATH": BUILD_WORKFLOW_PATH,
        "PIN_WORKFLOW_REF": BUILD_WORKFLOW_REF,
        "PIN_KMS_KEY_ARN": trust_env["BILLING_PRODUCER_BUILD_KMS_KEY_ARN"],
        "PIN_BUILD_AUDIENCE": trust_env["BILLING_PRODUCER_BUILD_AUDIENCE"],
        "PIN_ISSUER": trust_env["BILLING_PRODUCER_BUILD_ISSUER"],
        "PIN_OIDC_ISSUER": "https://token.actions.githubusercontent.com",
        "PIN_OIDC_SUBJECT": trust_env["BILLING_PRODUCER_BUILD_OIDC_SUBJECT"],
        "BUILD_PUBLIC_KEY": trust_env["BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM"],
    }
    try:
        readback, digest = fetch_backend_trust_readback(
            pins,
            opener=opener,
        )
    except (GateError, github_preflight.GateError) as exc:
        # The preflight fetcher raises its own GateError class, distinct from the
        # rollout gate's; translate both so the sanitized reason is preserved.
        raise DeployError(str(exc)) from None
    # Timestamp freshness is validated by the fetcher; omit it from the trust
    # entry consumed by the signature verifier, which checks the exact pin tuple.
    trust = {key: value for key, value in readback.items() if key != "read_at_utc"}
    _require(
        set(readback) == set(trust) | {"read_at_utc"},
        "runtime_build_trust_shape_invalid",
    )
    return trust, digest, readback["read_at_utc"]


def _verify_runtime_build_trust(
    env: dict[str, str],
    signed_envelope: Any,
    expected_verified: dict[str, str],
    *,
    opener=None,
) -> tuple[str, str]:
    trust, digest, read_at = _fetch_runtime_build_trust(env, opener=opener)
    verified = validate_artifact_before_aws(env, signed_envelope, trust)
    _require(
        verified == expected_verified
        and verified["app_image_digest"] == env["INPUT_APP_IMAGE_DIGEST"],
        "runtime_build_trust_attestation_mismatch",
    )
    return digest, read_at


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
            "{services:services[].{serviceArn:serviceArn,serviceName:serviceName,status:status,desiredCount:desiredCount,runningCount:runningCount,taskDefinition:taskDefinition,deployments:deployments[].{id:id,status:status,taskDefinition:taskDefinition,rolloutState:rolloutState},loadBalancers:loadBalancers[].{targetGroupArn:targetGroupArn,containerName:containerName,containerPort:containerPort}},failures:failures}",
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


def _validate_live_service_snapshot(
    service: dict[str, Any], snapshot: dict[str, Any]
) -> None:
    """Bind live DescribeServices metadata to the authenticated operator snapshot."""

    expected_count = snapshot.get("task_count")
    expected_task_definition = snapshot.get("task_definition_arn")
    expected_service = snapshot.get("service")
    _require(
        type(expected_count) is int
        and expected_count > 0
        and isinstance(expected_task_definition, str)
        and isinstance(expected_service, dict),
        "operator_service_snapshot_invalid",
    )
    _require(service.get("status") == "ACTIVE", "live_service_not_active")
    _require(
        service.get("taskDefinition") == expected_task_definition,
        "live_service_task_definition_mismatch",
    )
    _require(
        service.get("desiredCount") == expected_count
        and service.get("runningCount") == expected_count,
        "live_service_cohort_count_mismatch",
    )
    _require(
        service.get("loadBalancers") == expected_service.get("loadBalancers"),
        "live_service_load_balancer_mismatch",
    )
    deployments = service.get("deployments")
    primary = [
        item
        for item in deployments or []
        if isinstance(item, dict) and item.get("status") == "PRIMARY"
    ]
    _require(
        len(primary) == 1
        and primary[0].get("id") == snapshot.get("primary_deployment_id")
        and primary[0].get("taskDefinition") == expected_task_definition
        and primary[0].get("rolloutState") == "COMPLETED",
        "live_service_rollout_not_complete",
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


def _assert_caller_account(expected_role_arn: str, run_id: str) -> None:
    _require(
        expected_role_arn == PINNED_DEPLOY_ROLE_ARN, "expected_deploy_role_arn_invalid"
    )
    _require(re.fullmatch(r"[1-9][0-9]*", run_id) is not None, "actions_run_id_invalid")
    identity = _aws_json(
        ["sts", "get-caller-identity", "--query", "{Account:Account,Arn:Arn}"]
    )
    _require(
        isinstance(identity, dict) and identity.get("Account") == AWS_ACCOUNT_ID,
        "aws_caller_account_mismatch",
    )
    arn = identity.get("Arn")
    role_name = expected_role_arn.split("/", maxsplit=1)[1]
    expected_session_arn = (
        f"arn:aws:sts::388460142431:assumed-role/"
        f"{role_name}/asaas-producer-{run_id}"
    )
    _require(
        arn == expected_session_arn,
        "aws_caller_role_mismatch",
    )


def _resolve_operator_snapshot_secret_version(
    source_snapshot: dict[str, Any],
    secret_metadata: dict[str, Any],
    secret_versions: dict[str, Any],
    *,
    expected_secret_arn: str,
    expected_version_id: str,
) -> dict[str, Any]:
    service = source_snapshot.get("service")
    tasks = source_snapshot.get("tasks")
    task_definition = source_snapshot.get("task_definition")
    _require(
        isinstance(service, dict)
        and isinstance(tasks, list)
        and isinstance(task_definition, dict),
        "operator_task_readback_missing",
    )
    try:
        resolved = resolve_active_database_secret(
            {
                "desired_count": service.get("desiredCount"),
                "active_tasks": tasks,
                "task_definitions": [task_definition],
                "secret_description": secret_metadata,
                "secret_versions": secret_versions,
            }
        )
    except ReferenceError as exc:
        raise DeployError(f"database_secret_{exc}") from None
    _require(
        resolved["database_secret_arn"] == expected_secret_arn
        and resolved["resolved_version_id"] == expected_version_id,
        "database_secret_live_metadata_mismatch",
    )
    return resolved


def _validate_live_deployment_metadata(
    validated_snapshot: dict[str, Any],
    source_snapshot: dict[str, Any],
    *,
    expected_digest: str,
    image_uri: str,
    secret_arn: str,
) -> None:
    service = _live_service()
    _validate_live_service_snapshot(service, validated_snapshot)
    ecr = _ecr_metadata(expected_digest, image_uri)
    image_rows = [
        row
        for row in ecr["imageDetails"]
        if isinstance(row, dict) and row.get("imageDigest") == expected_digest
    ]
    _require(len(image_rows) == 1, "ecr_digest_not_found_or_ambiguous")
    secret, versions = _secret_metadata(secret_arn)
    _resolve_operator_snapshot_secret_version(
        source_snapshot,
        secret,
        versions,
        expected_secret_arn=secret_arn,
        expected_version_id=validated_snapshot["database_secret_version_id"],
    )


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


def _deployment_job_context(run_id: str, token: str) -> dict[str, str]:
    jobs = github_preflight.github_collection(
        f"{API_ROOT}{API_PREFIX}/actions/runs/{run_id}/jobs?per_page=100&page=1",
        token,
        list_field="jobs",
    )
    matches = [
        row for row in jobs if isinstance(row, dict) and row.get("name") == "deploy"
    ]
    _require(
        len(matches) == 1
        and type(matches[0].get("id")) is int
        and matches[0]["id"] > 0
        and matches[0].get("status") == "in_progress"
        and isinstance(matches[0].get("started_at"), str),
        "deployment_job_start_unreadable",
    )
    return {
        "job_id": str(matches[0]["id"]),
        "job_started_at": matches[0]["started_at"],
    }


def _deployment_job_started_at(run_id: str, token: str) -> datetime:
    context = _deployment_job_context(run_id, token)
    return _time(context["job_started_at"], "deployment_job_started_at")


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
    job = _deployment_job_context(env["GITHUB_RUN_ID"], token)
    _require(
        _time(job["job_started_at"], "deployment_job_started_at") == deploy_started_at,
        "deployment_job_context_changed",
    )
    context = {
        "run_id": env["GITHUB_RUN_ID"],
        "run_attempt": env["GITHUB_RUN_ATTEMPT"],
        "operation_id": env["INPUT_OPERATION_ID"],
        "phase": "after",
        "artifact_sha256": env["INPUT_ENVELOPE_SHA256"],
        "ref": env["GITHUB_REF"],
        "sha": env["GITHUB_SHA"],
        **job,
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
            marker = f"<!-- {ENCRYPTED_READBACK_MARKER}\n"
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
    after_snapshot_source = parse_post_readback_comment(
        matching[0]["body"],
        author_login=author["login"],
        author_permission=_admin_permission(author["login"], token),
        expected_operator_login=before["operator_login"],
        private_pem=private_pem,
        expected_context=context,
    )
    artifact_tuple = _artifact_tuple(env)
    after_operator = validate_operator_readback(
        after_snapshot_source,
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
    comment_created_at = _time(
        matching[0].get("created_at"), "post_readback_comment_created_at"
    )
    _require(
        comment_created_at >= checked_at and comment_created_at >= deploy_started_at,
        "post_readback_comment_time_invalid",
    )
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
    _validate_live_deployment_metadata(
        after_snapshot,
        after_snapshot_source["snapshot"],
        expected_digest=verified_attestation["app_image_digest"],
        image_uri=env["BILLING_PRODUCER_ECR_REPOSITORY_URI"],
        secret_arn=after_snapshot["database_secret_arn"],
    )
    return after_operator, {**verified, "live_readback": "verified"}


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


def _validate_pre_oidc_cohort_evidence(
    env: dict[str, str],
    run: dict[str, Any],
    before: dict[str, Any],
    template_data: dict[str, Any],
) -> dict[str, Any]:
    runner_temp = Path(env["RUNNER_TEMP"])
    path = runner_temp / "asaas-producer-pre-oidc-cohort.json"
    try:
        mode = stat.S_IMODE(path.lstat().st_mode)
        _require(
            stat.S_ISREG(path.lstat().st_mode) and mode & 0o077 == 0,
            "cohort_evidence_file_permissions_invalid",
        )
        evidence = _strict_loads(path.read_bytes(), max_bytes=1_500_000)
    except (OSError, EvidenceError):
        raise DeployError("cohort_evidence_file_unreadable") from None
    fields = {
        "schema",
        "run_id",
        "run_attempt",
        "ref",
        "sha",
        "operation_id",
        "artifact_sha256",
        "deployment_job_id",
        "deployment_job_started_at",
        "comment_created_at",
        "checked_at_utc",
        "operator_login",
        "encrypted_snapshot_sha256",
        "operator_readback",
        "validated_cohort",
        "verification",
        "evidence_sha256",
    }
    _require(
        isinstance(evidence, dict) and set(evidence) == fields,
        "cohort_evidence_shape_invalid",
    )
    evidence_digest = evidence["evidence_sha256"]
    payload = {
        key: value for key, value in evidence.items() if key != "evidence_sha256"
    }
    actual_digest = __import__("hashlib").sha256(_canonical(payload)).hexdigest()
    _require(
        isinstance(evidence_digest, str)
        and evidence_digest == actual_digest
        and evidence_digest == env.get("ASAAS_PRE_OIDC_COHORT_SHA256"),
        "cohort_evidence_hash_mismatch",
    )
    expected_values = {
        "schema": "asaas_producer_pre_oidc_cohort_confirmation.v1",
        "run_id": run["run_id"],
        "run_attempt": run["run_attempt"],
        "ref": run["ref"],
        "sha": run["sha"],
        "operation_id": env["INPUT_OPERATION_ID"],
        "artifact_sha256": env["INPUT_ENVELOPE_SHA256"],
        "operator_login": before["operator_login"],
    }
    _require(
        all(evidence.get(key) == value for key, value in expected_values.items()),
        "cohort_evidence_run_binding_mismatch",
    )
    job_context = _deployment_job_context(run["run_id"], env["GH_TOKEN"])
    _require(
        evidence.get("deployment_job_id") == job_context["job_id"]
        and evidence.get("deployment_job_started_at") == job_context["job_started_at"],
        "cohort_evidence_job_binding_mismatch",
    )
    job_started_at = _time(job_context["job_started_at"], "deployment_job_started_at")
    now = datetime.now(timezone.utc)
    checked_at = _time(evidence.get("checked_at_utc"), "cohort_checked_at")
    comment_created_at = _time(
        evidence.get("comment_created_at"), "cohort_comment_created_at"
    )
    _require(
        job_started_at <= checked_at <= comment_created_at <= now
        and now - checked_at <= timedelta(minutes=5),
        "cohort_evidence_stale_or_out_of_order",
    )
    raw = evidence.get("operator_readback")
    _require(isinstance(raw, dict), "cohort_operator_readback_invalid")
    _require(
        canonical_sha256(raw) == evidence.get("encrypted_snapshot_sha256"),
        "cohort_encrypted_snapshot_hash_mismatch",
    )
    _require(
        raw.get("operator_login") == before["operator_login"],
        "cohort_operator_mismatch",
    )
    permission = _admin_permission(before["operator_login"], env["GH_TOKEN"])
    try:
        cohort = validate_operator_readback(
            raw,
            phase="cohort",
            expected_run_id=run["run_id"],
            expected_run_attempt=run["run_attempt"],
            expected_sha=run["sha"],
            expected_operation_id=env["INPUT_OPERATION_ID"],
            mode=env["INPUT_MODE"],
            artifact_tuple=_artifact_tuple(env),
            template_sha256=before["template_sha256"],
            producer_environment_sha256=before["producer_environment_sha256"],
            expected_base_environment_sha256=template_data["base_environment_sha256"],
            expected_operator_login=before["operator_login"],
            operator_permission=permission,
            expected_environment=_task_env(env),
            expected_task_role_arn=TASK_ROLE_ARN,
            expected_execution_role_arn=EXECUTION_ROLE_ARN,
            expected_image_uri=env["BILLING_PRODUCER_ECR_REPOSITORY_URI"],
            expected_secret_arn=before["validated_snapshot"]["database_secret_arn"],
            expected_secret_json_key=before["validated_snapshot"][
                "database_secret_json_key"
            ],
            expected_secret_version_id=before["database_secret_version_id"],
            earliest_allowed_at=job_started_at,
            latest_allowed_at=now,
        )
        verification = validate_pre_oidc_cohort_confirmation(before, cohort)
    except (EvidenceError, RolloutError, ReferenceError) as exc:
        raise DeployError(str(exc)) from None
    _require(
        cohort == evidence["validated_cohort"]
        and verification == evidence["verification"],
        "cohort_evidence_validation_changed",
    )
    return {
        **evidence,
        "operator_readback": raw,
        "validated_cohort": cohort,
        "verification": verification,
    }


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
    validate_runtime_deploy_role_variable(
        env.get(DEPLOY_ROLE_VARIABLE), protections["deploy_role_arn"]
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
    cohort = _validate_pre_oidc_cohort_evidence(
        env,
        run,
        readback,
        validate_task_definition_template(template),
    )
    return {
        **readback,
        "template": template,
        "template_data": template_data,
        "deploy_role_arn": protections["deploy_role_arn"],
        "cohort": cohort,
    }


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
    signed_envelope = _strict_loads(
        Path(env["ASAAS_SIGNED_ENVELOPE_PATH"]).read_bytes()
    )
    verified_attestation = validate_artifact_before_aws(
        env,
        signed_envelope,
        _strict_loads(env.get("ASAAS_PRODUCER_BUILD_TRUST_READBACK_JSON", "")),
    )
    _require(
        verified_attestation["app_image_digest"] == env["INPUT_APP_IMAGE_DIGEST"],
        "app_image_digest_mismatch",
    )
    _assert_caller_account(private_before["deploy_role_arn"], run["run_id"])
    cohort_evidence = private_before["cohort"]
    cohort = cohort_evidence["validated_cohort"]
    cohort_snapshot = cohort["snapshot"]
    source_before = cohort_evidence["operator_readback"]
    _require(
        isinstance(source_before, dict)
        and isinstance(source_before.get("snapshot"), dict),
        "operator_task_readback_missing",
    )
    previous_task_definition_arn = cohort_snapshot["task_definition_arn"]
    desired_count = cohort_snapshot["task_count"]
    current_service = _live_service()
    _validate_live_service_snapshot(current_service, cohort_snapshot)
    template_definition = private_before["template"]["task_definition"]
    db_source = next(
        row["valueFrom"]
        for container in template_definition["containerDefinitions"]
        for row in container.get("secrets", [])
        if row.get("name") == "DATABASE_URL"
    )
    secret_metadata, secret_versions = _secret_metadata(
        cohort_snapshot["database_secret_arn"]
    )
    database_binding = _resolve_operator_snapshot_secret_version(
        source_before["snapshot"],
        secret_metadata,
        secret_versions,
        expected_secret_arn=cohort_snapshot["database_secret_arn"],
        expected_version_id=cohort_snapshot["database_secret_version_id"],
    )
    source_parts = db_source.split(":")
    source_arn = ":".join(source_parts[:7])
    source_json_key = source_parts[7] if len(source_parts) == 10 else ""
    _require(
        source_arn == database_binding["database_secret_arn"]
        and source_json_key == database_binding["database_secret_json_key"]
        and database_binding["active_task_count"] == cohort_snapshot["task_count"]
        and database_binding["active_task_definition_arn"]
        == cohort_snapshot["task_definition_arn"],
        "database_secret_reference_mismatch",
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
        rollout_before = {
            **private_before,
            "phase": "before",
            "operator_login": cohort["operator_login"],
            "operator_permission": cohort["operator_permission"],
            "base_environment_sha256": cohort["base_environment_sha256"],
            "target_group": cohort["target_group"],
            "readiness_probe": cohort["readiness_probe"],
            "before_snapshot_sha256": cohort["snapshot_sha256"],
            "validated_snapshot": cohort_snapshot,
            "database_secret_version_id": cohort_snapshot["database_secret_version_id"],
        }
        private_key_path = (
            Path(env["RUNNER_TEMP"]) / "asaas-producer-post-readback-private.pem"
        )
        # The one-run private key was created before OIDC and remains on this
        # runner for the later post-deployment confirmation.
        if not private_key_path.exists():
            raise DeployError("post_readback_private_key_missing")
        after_operator, accepted = _wait_for_encrypted_post_readback(
            env=env,
            private_key_path=private_key_path,
            before=rollout_before,
            verified_attestation=verified_attestation,
            deploy_started_at=deploy_started_at,
            expected_protection_hash=env["ASAAS_PREFLIGHT_PROTECTION_SHA256"],
        )
        runtime_trust_sha256, runtime_trust_read_at = _verify_runtime_build_trust(
            env, signed_envelope, verified_attestation
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
            "runtime_build_trust_sha256": runtime_trust_sha256,
            "runtime_build_trust_read_at_utc": runtime_trust_read_at,
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
        coded = isinstance(
            exc,
            (
                DeployError,
                GateError,
                github_preflight.GateError,
                EvidenceError,
                RolloutError,
                ReferenceError,
            ),
        )
        if update_attempted:
            # Keep the sanitized failure code that triggered rollback visible;
            # uncoded exceptions may carry paths or values, so they stay generic.
            reason = str(exc) if coded else "rollout_post_mutation_failed"
            try:
                _rollback(previous_task_definition_arn, desired_count)
            except (DeployError, OSError, ValueError):
                raise DeployError(
                    f"rollout_failed_and_rollback_failed:{reason}"
                ) from None
            raise DeployError(f"rollout_failed_rolled_back:{reason}") from None
        if coded:
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
            summary.write(
                f"- Runtime trust readback SHA-256: `{result['runtime_build_trust_sha256']}`\n"
            )
            summary.write(
                f"- Runtime trust readback time: `{result['runtime_build_trust_read_at_utc']}`\n"
            )
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
