#!/usr/bin/env python3
"""No-write GitHub protection, run-history, and operator-evidence gate."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from uuid import UUID

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
    canonical_sha256,
    parse_post_readback_comment,
    validate_before_after,
    validate_pre_oidc_cohort_confirmation,
    validate_operator_readback,
    validate_task_definition_template,
)
from scripts.verify_asaas_build_environment_approval import (
    _fetch_github_attachment,
    _validate_attachment_ref,
    _validate_png,
)
from scripts.verify_asaas_producer_image_rollout import (
    RolloutError,
    _public_key_fingerprint,
    validate_signed_attestation,
)

REPOSITORY = "neurostackinfra/neurostack-api"
REPOSITORY_ID = 1152335815
OWNER_ID = 340698182
RELEASE_REF = "refs/heads/main"
EXPECTED_RULESET = "asaas-producer-release-main"
API_ROOT = "https://api.github.com"
API_PREFIX = f"/repos/{REPOSITORY}"
DEPLOY_ENVIRONMENT = "asaas-production-deployment"
DEPLOY_WORKFLOW_PATH = ".github/workflows/asaas-producer-image-deploy.yml"
DEPLOY_ROLE_VARIABLE = "BILLING_PRODUCER_DEPLOY_ROLE_ARN"
# This exact value was read from the approved Group 2 CloudFormation output
# DeployRoleArn and roles.DeployRole.arn on 2026-10-16. A stack recreation must
# update this reviewed pin from a new authenticated Group 2 output readback.
PINNED_DEPLOY_ROLE_ARN = (
    "arn:aws:iam::388460142431:role/"
    "neurostack-asaas-producer-release-DeployRole-CU6AueCQCi9i"
)
DEPLOY_ROLE_OUTPUT_READ_AT_UTC = "2026-10-16T23:02:35.755467Z"
DEPLOY_ROLE_OUTPUT_READBACK_SHA256 = (
    "765b3174d6a1b28205f0185f535fc6cdf2fa8b369c8af6e7583524aa437d7fb9"
)
TASK_ROLE_ARN = "arn:aws:iam::388460142431:role/neurostack-staging-ecs-task"
EXECUTION_ROLE_ARN = "arn:aws:iam::388460142431:role/neurostack-staging-ecs-execution"
HISTORY_RUN_NAME = re.compile(
    r"^ASAAS producer image (?P<mode>bootstrap|registered) "
    r"operation=(?P<operation>[0-9a-f-]{36}) "
    r"build_run=(?P<run>[1-9][0-9]*) "
    r"attestation=(?P<attestation>[0-9a-f-]{36}) "
    r"digest=(?P<digest>sha256:[0-9a-f]{64}) "
    r"envelope=(?P<envelope>[0-9a-f]{64})$"
)
MAX_RUN_AGE = timedelta(hours=1)
MAX_BEFORE_READBACK_AGE = timedelta(minutes=5)
MAX_AFTER_READBACK_AGE = timedelta(minutes=15)
MAX_PRE_OIDC_COHORT_WAIT_SECONDS = 900
MAX_COHORT_READBACK_AGE = timedelta(minutes=5)
UI_COMMENT_SCHEMA = "asaas_producer_deployment_ui_attestation.v1"


class GateError(ValueError):
    """A workflow, environment, or rollout artifact did not meet its contract."""


def validate_deploy_role_environment_variable(raw: Any) -> str:
    """Accept only the exact role ARN in the reviewed Group 2 output readback."""

    _require(
        isinstance(raw, dict)
        and raw.get("name") == DEPLOY_ROLE_VARIABLE
        and raw.get("value") == PINNED_DEPLOY_ROLE_ARN,
        "protected_deploy_role_readback_invalid",
    )
    return raw["value"]


def validate_runtime_deploy_role_variable(
    environment_value: Any, protected_role_arn: Any
) -> str:
    """Require the job's protected vars value to equal the hashed API readback."""

    expected = validate_deploy_role_environment_variable(
        {"name": DEPLOY_ROLE_VARIABLE, "value": protected_role_arn}
    )
    _require(
        isinstance(environment_value, str) and environment_value == expected,
        "protected_deploy_role_variable_mismatch",
    )
    return expected


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise GateError(reason)


def _time(value: Any, field: str) -> datetime:
    _require(isinstance(value, str), f"{field}_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise GateError(f"{field}_invalid") from None
    _require(parsed.tzinfo is not None, f"{field}_timezone_missing")
    return parsed.astimezone(timezone.utc)


def _run_title(env: dict[str, str]) -> str:
    mode = env.get("INPUT_MODE", "")
    _require(mode in {"bootstrap", "registered"}, "rollout_mode_invalid")
    try:
        operation_id = str(UUID(env.get("INPUT_OPERATION_ID", "")))
        attestation_id = str(UUID(env.get("INPUT_ATTESTATION_ID", "")))
    except (ValueError, TypeError):
        raise GateError("rollout_uuid_invalid") from None
    _require(
        operation_id == env.get("INPUT_OPERATION_ID"), "operation_id_not_canonical"
    )
    _require(
        attestation_id == env.get("INPUT_ATTESTATION_ID"),
        "attestation_id_not_canonical",
    )
    run_id = env.get("INPUT_BUILD_RUN_ID", "")
    digest = env.get("INPUT_APP_IMAGE_DIGEST", "")
    envelope_sha = env.get("INPUT_ENVELOPE_SHA256", "")
    _require(re.fullmatch(r"[1-9][0-9]*", run_id) is not None, "build_run_id_invalid")
    _require(
        re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is not None, "app_digest_invalid"
    )
    _require(
        re.fullmatch(r"[0-9a-f]{64}", envelope_sha) is not None,
        "envelope_sha256_invalid",
    )
    return (
        f"ASAAS producer image {mode} operation={operation_id} build_run={run_id} "
        f"attestation={attestation_id} digest={digest} envelope={envelope_sha}"
    )


def validate_current_run(
    env: dict[str, str],
    token: str,
    *,
    github_get: Callable[[str, str], tuple[Any, Any]] | None = None,
    github_collection: Callable[..., list[Any]] | None = None,
    now: datetime | None = None,
    stage: str = "preflight",
) -> dict[str, Any]:
    get = github_preflight.github_get if github_get is None else github_get
    collect = (
        github_preflight.github_collection
        if github_collection is None
        else github_collection
    )
    run_id = env.get("GITHUB_RUN_ID", "")
    attempt = env.get("GITHUB_RUN_ATTEMPT", "")
    _require(re.fullmatch(r"[1-9][0-9]*", run_id) is not None, "actions_run_id_invalid")
    _require(attempt == "1", "rollout_run_attempt_must_be_first")
    _require(
        env.get("GITHUB_EVENT_NAME") == "workflow_dispatch", "rollout_event_invalid"
    )
    _require(env.get("GITHUB_REPOSITORY") == REPOSITORY, "repository_name_mismatch")
    _require(
        env.get("GITHUB_REPOSITORY_ID") == str(REPOSITORY_ID), "repository_id_mismatch"
    )
    _require(
        env.get("GITHUB_REPOSITORY_OWNER_ID") == str(OWNER_ID),
        "repository_owner_id_mismatch",
    )
    _require(env.get("GITHUB_REF") == RELEASE_REF, "release_ref_mismatch")
    _require(
        env.get("GITHUB_WORKFLOW_REF")
        == f"{REPOSITORY}/{DEPLOY_WORKFLOW_PATH}@{RELEASE_REF}",
        "workflow_ref_mismatch",
    )
    expected_title = _run_title(env)
    run, _ = get(f"{API_ROOT}{API_PREFIX}/actions/runs/{run_id}", token)
    _require(isinstance(run, dict), "actions_run_readback_invalid")
    _require(run.get("id") == int(run_id), "actions_run_id_mismatch")
    _require(
        run.get("event") == "workflow_dispatch" and run.get("status") == "in_progress",
        "actions_run_not_current",
    )
    _require(
        run.get("run_attempt") == 1
        and run.get("path") == f"{DEPLOY_WORKFLOW_PATH}@{RELEASE_REF}",
        "actions_run_workflow_mismatch",
    )
    _require(
        run.get("head_sha") == env.get("GITHUB_SHA")
        and re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", "")),
        "actions_run_sha_mismatch",
    )
    _require(
        run.get("head_branch") == "main" and run.get("display_title") == expected_title,
        "actions_run_identity_mismatch",
    )
    actor = run.get("actor")
    _require(
        isinstance(actor, dict) and type(actor.get("id")) is int,
        "actions_run_actor_missing",
    )
    repository = run.get("repository")
    owner = repository.get("owner") if isinstance(repository, dict) else None
    head_repository = run.get("head_repository")
    _require(
        isinstance(repository, dict)
        and repository.get("full_name") == REPOSITORY
        and repository.get("id") == REPOSITORY_ID
        and isinstance(owner, dict)
        and owner.get("id") == OWNER_ID
        and isinstance(head_repository, dict)
        and head_repository.get("id") == REPOSITORY_ID,
        "actions_run_repository_mismatch",
    )
    created_at = _time(run.get("created_at"), "actions_run_created_at")
    observed_at = (
        datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    )
    _require(
        created_at <= observed_at + timedelta(minutes=5)
        and observed_at - created_at <= MAX_RUN_AGE,
        "actions_run_stale",
    )
    exact_ref, _ = get(f"{API_ROOT}{API_PREFIX}/git/ref/heads/main", token)
    ref_object = exact_ref.get("object") if isinstance(exact_ref, dict) else None
    _require(
        isinstance(exact_ref, dict)
        and exact_ref.get("ref") == RELEASE_REF
        and isinstance(ref_object, dict)
        and ref_object.get("type") == "commit"
        and ref_object.get("sha") == run.get("head_sha"),
        "actions_run_ref_no_longer_exact",
    )
    jobs = collect(
        f"{API_ROOT}{API_PREFIX}/actions/runs/{run_id}/jobs?per_page=100&page=1",
        token,
        list_field="jobs",
    )
    _require(
        stage in {"preflight", "predeploy", "deploy", "pre-oidc-cohort"},
        "workflow_stage_invalid",
    )
    jobs_by_name: dict[str, list[dict[str, Any]]] = {}
    for job in jobs:
        if isinstance(job, dict) and isinstance(job.get("name"), str):
            jobs_by_name.setdefault(job["name"], []).append(job)
    expected_jobs = {"preflight": "in_progress"}
    if stage in {"predeploy", "deploy"}:
        expected_jobs["preflight"] = "completed_success"
        expected_jobs["verify-predeploy-evidence"] = (
            "in_progress" if stage == "predeploy" else "completed_success"
        )
    if stage in {"deploy", "pre-oidc-cohort"}:
        expected_jobs["deploy"] = "in_progress"
    for job_name, expected_state in expected_jobs.items():
        rows = jobs_by_name.get(job_name, [])
        _require(
            len(rows) == 1 and rows[0].get("head_sha") == run.get("head_sha"),
            f"actions_{job_name.replace('-', '_')}_job_invalid",
        )
        if expected_state == "in_progress":
            _require(
                rows[0].get("status") == "in_progress",
                f"actions_{job_name.replace('-', '_')}_job_invalid",
            )
        else:
            _require(
                rows[0].get("status") == "completed"
                and rows[0].get("conclusion") == "success",
                f"actions_{job_name.replace('-', '_')}_job_invalid",
            )
    return {
        "run_id": run_id,
        "run_attempt": attempt,
        "workflow_path": DEPLOY_WORKFLOW_PATH,
        "sha": run["head_sha"],
        "ref": RELEASE_REF,
        "created_at_utc": run["created_at"],
        "display_title": expected_title,
        "actor_id": actor["id"],
    }


def validate_repository_protections(
    token: str,
    *,
    github_get: Callable[[str, str], tuple[Any, Any]] | None = None,
    github_collection: Callable[..., list[Any]] | None = None,
) -> dict[str, Any]:
    get = github_preflight.github_get if github_get is None else github_get
    collect = (
        github_preflight.github_collection
        if github_collection is None
        else github_collection
    )
    repository, _ = get(f"{API_ROOT}{API_PREFIX}", token)
    owner = repository.get("owner") if isinstance(repository, dict) else None
    _require(
        isinstance(repository, dict)
        and repository.get("full_name") == REPOSITORY
        and repository.get("id") == REPOSITORY_ID
        and isinstance(owner, dict)
        and owner.get("id") == OWNER_ID
        and repository.get("default_branch") == "main",
        "repository_readback_mismatch",
    )
    rulesets = collect(
        f"{API_ROOT}{API_PREFIX}/rulesets?includes_parents=true&per_page=100&page=1",
        token,
    )
    ruleset = github_preflight.validate_ruleset(rulesets, token, github_get_fn=get)
    environment = github_preflight.validate_environment(
        DEPLOY_ENVIRONMENT,
        token,
        github_get_fn=get,
        github_collection_fn=collect,
    )
    role_readback, _ = get(
        f"{API_ROOT}{API_PREFIX}/environments/"
        f"{quote(DEPLOY_ENVIRONMENT, safe='')}/variables/{DEPLOY_ROLE_VARIABLE}",
        token,
    )
    deploy_role_arn = validate_deploy_role_environment_variable(role_readback)
    return {
        "repository": {
            "full_name": repository["full_name"],
            "id": repository["id"],
            "owner_id": owner["id"],
            "default_branch": repository["default_branch"],
        },
        "ruleset": ruleset,
        "environment": environment,
        "deploy_role_variable": DEPLOY_ROLE_VARIABLE,
        "deploy_role_arn": deploy_role_arn,
        "deploy_role_output_pin": {
            "cloudformation_stack": "neurostack-asaas-producer-release",
            "output_key": "DeployRoleArn",
            "read_at_utc": DEPLOY_ROLE_OUTPUT_READ_AT_UTC,
            "readback_sha256": DEPLOY_ROLE_OUTPUT_READBACK_SHA256,
        },
    }


def _validate_ui_approval_comment(
    comment: Any,
    *,
    run: dict[str, Any],
    reviewer_login: str,
    preflight_observed_at: datetime,
    approval_job_started_at: datetime,
    template_sha256: str,
    screenshot_fetcher: Callable[[str], bytes] = _fetch_github_attachment,
) -> dict[str, Any]:
    required = {
        "schema_version",
        "run_id",
        "run_attempt",
        "sha",
        "ref",
        "workflow_path",
        "environment",
        "reviewer_login",
        "checked_at_utc",
        "ruleset_bypass_empty",
        "environment_admin_bypass_disabled",
        "sole_branch_policy",
        "screenshots_sha256",
        "task_definition_template_sha256",
    }
    _require(
        isinstance(comment, str) and len(comment.encode("utf-8")) <= 4096,
        "ui_approval_comment_missing",
    )
    try:
        raw = json.loads(comment)
    except json.JSONDecodeError:
        raise GateError("ui_approval_comment_invalid") from None
    _require(
        isinstance(raw, dict) and set(raw) == required,
        "ui_approval_comment_fields_invalid",
    )
    expected = {
        "schema_version": UI_COMMENT_SCHEMA,
        "run_id": run["run_id"],
        "run_attempt": run["run_attempt"],
        "sha": run["sha"],
        "ref": run["ref"],
        "workflow_path": run["workflow_path"],
        "environment": DEPLOY_ENVIRONMENT,
        "reviewer_login": reviewer_login,
        "ruleset_bypass_empty": True,
        "environment_admin_bypass_disabled": True,
        "sole_branch_policy": "main",
        "task_definition_template_sha256": template_sha256,
    }
    _require(
        all(raw.get(key) == value for key, value in expected.items()),
        "ui_approval_comment_mismatch",
    )
    screenshots = raw.get("screenshots_sha256")
    _require(
        isinstance(screenshots, dict)
        and set(screenshots) == {"ruleset", "deployment_environment"},
        "ui_approval_screenshot_hashes_invalid",
    )
    verified_screenshots: dict[str, dict[str, str]] = {}
    seen_refs: set[str] = set()
    seen_hashes: set[str] = set()
    for label, screenshot in screenshots.items():
        _require(
            isinstance(screenshot, dict) and set(screenshot) == {"ref", "sha256"},
            "ui_approval_screenshot_hashes_invalid",
        )
        reference = screenshot.get("ref")
        digest = screenshot.get("sha256")
        try:
            _validate_attachment_ref(reference)
        except ValueError:
            raise GateError("ui_approval_screenshot_reference_invalid") from None
        _require(
            isinstance(digest, str)
            and re.fullmatch(r"[0-9a-f]{64}", digest) is not None,
            "ui_approval_screenshot_hashes_invalid",
        )
        _require(
            reference not in seen_refs and digest not in seen_hashes,
            "ui_approval_screenshot_duplicate",
        )
        try:
            contents = screenshot_fetcher(reference)
            _validate_png(contents)
        except (OSError, ValueError):
            raise GateError("ui_approval_screenshot_invalid") from None
        _require(
            hashlib.sha256(contents).hexdigest() == digest,
            "ui_approval_screenshot_sha256_mismatch",
        )
        seen_refs.add(reference)
        seen_hashes.add(digest)
        verified_screenshots[label] = {
            "ref_sha256": hashlib.sha256(reference.encode()).hexdigest(),
            "sha256": digest,
        }
    checked_at = _time(raw.get("checked_at_utc"), "ui_approval_checked_at")
    _require(
        preflight_observed_at <= checked_at <= approval_job_started_at,
        "ui_approval_timestamp_order_invalid",
    )
    _require(
        approval_job_started_at - checked_at <= MAX_BEFORE_READBACK_AGE,
        "ui_approval_timestamp_stale",
    )
    return {
        "checked_at_utc": raw["checked_at_utc"],
        "screenshots": dict(sorted(verified_screenshots.items())),
    }


def validate_environment_approval(
    *,
    token: str,
    run: dict[str, Any],
    phase: str,
    expected_reviewer_login: str,
    actor_id: int,
    template_sha256: str,
    preflight_observed_at: datetime,
    github_get: Callable[[str, str], tuple[Any, Any]] | None = None,
    github_collection: Callable[..., list[Any]] | None = None,
    now: datetime | None = None,
    screenshot_fetcher: Callable[[str], bytes] = _fetch_github_attachment,
) -> dict[str, Any]:
    get = github_preflight.github_get if github_get is None else github_get
    collect = (
        github_preflight.github_collection
        if github_collection is None
        else github_collection
    )
    _require(phase in {"predeploy", "deploy"}, "environment_approval_phase_invalid")
    run_id = run["run_id"]
    approvals, _ = get(f"{API_ROOT}{API_PREFIX}/actions/runs/{run_id}/approvals", token)
    _require(isinstance(approvals, list), "environment_approval_history_invalid")
    matching = [
        row
        for row in approvals
        if isinstance(row, dict)
        and isinstance(row.get("environments"), list)
        and any(
            isinstance(item, dict) and item.get("name") == DEPLOY_ENVIRONMENT
            for item in row["environments"]
        )
    ]
    expected_count = 1 if phase == "predeploy" else 2
    _require(len(matching) == expected_count, "environment_approval_count_invalid")
    _require(
        all(row.get("state") == "approved" for row in matching),
        "environment_approval_not_approved",
    )
    users = [row.get("user") for row in matching]
    _require(
        all(
            isinstance(user, dict)
            and type(user.get("id")) is int
            and isinstance(user.get("login"), str)
            and user.get("login") == expected_reviewer_login
            for user in users
        ),
        "environment_approval_reviewer_mismatch",
    )
    _require(
        all(user["id"] != actor_id for user in users),
        "environment_approval_self_review",
    )
    permission, _ = get(
        f"{API_ROOT}{API_PREFIX}/collaborators/{quote(expected_reviewer_login, safe='')}/permission",
        token,
    )
    _require(
        isinstance(permission, dict) and permission.get("permission") == "admin",
        "environment_reviewer_not_admin",
    )
    jobs = collect(
        f"{API_ROOT}{API_PREFIX}/actions/runs/{run_id}/jobs?per_page=100&page=1",
        token,
        list_field="jobs",
    )
    observed = (
        datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    )
    comments = [row.get("comment") for row in matching]
    _require(
        all(isinstance(comment, str) for comment in comments),
        "ui_approval_comment_mismatch",
    )

    def exactly_one_job(name: str) -> dict[str, Any]:
        rows = [
            job for job in jobs if isinstance(job, dict) and job.get("name") == name
        ]
        _require(len(rows) == 1, "environment_approval_job_start_missing")
        return rows[0]

    deploy_job_metadata: dict[str, str] | None = None
    if phase == "predeploy":
        target_job = exactly_one_job("verify-predeploy-evidence")
        _require(
            isinstance(target_job.get("started_at"), str),
            "environment_approval_job_start_missing",
        )
        job_started = _time(
            target_job["started_at"], "environment_approval_job_started_at"
        )
        ui = _validate_ui_approval_comment(
            comments[0],
            run=run,
            reviewer_login=expected_reviewer_login,
            preflight_observed_at=preflight_observed_at,
            approval_job_started_at=job_started,
            template_sha256=template_sha256,
            screenshot_fetcher=screenshot_fetcher,
        )
        checked_times = [ui["checked_at_utc"]]
        screenshots = [ui["screenshots"]]
    else:
        predeploy_job = exactly_one_job("verify-predeploy-evidence")
        deploy_job = exactly_one_job("deploy")
        _require(
            isinstance(predeploy_job.get("started_at"), str)
            and isinstance(predeploy_job.get("completed_at"), str)
            and isinstance(deploy_job.get("started_at"), str),
            "environment_approval_job_start_missing",
        )
        predeploy_started = _time(
            predeploy_job["started_at"], "predeploy_job_started_at"
        )
        predeploy_completed = _time(
            predeploy_job["completed_at"], "predeploy_job_completed_at"
        )
        deploy_started = _time(deploy_job["started_at"], "deploy_job_started_at")
        _require(
            type(deploy_job.get("id")) is int and deploy_job["id"] > 0,
            "deployment_job_id_unreadable",
        )
        deploy_job_metadata = {
            "deployment_job_id": str(deploy_job["id"]),
            "deployment_job_started_at": deploy_job["started_at"],
        }
        _require(
            predeploy_completed <= deploy_started, "environment_approval_order_invalid"
        )
        # The API gives no approval-click timestamp. The independently authored
        # checked_at values tie one fresh UI inspection to each protected job.
        decoded: list[tuple[datetime, str]] = []
        for comment in comments:
            try:
                value = json.loads(comment)
            except json.JSONDecodeError:
                raise GateError("ui_approval_comment_invalid") from None
            _require(isinstance(value, dict), "ui_approval_comment_invalid")
            decoded.append(
                (_time(value.get("checked_at_utc"), "ui_approval_checked_at"), comment)
            )
        first = [
            item
            for item in decoded
            if preflight_observed_at <= item[0] <= predeploy_started
        ]
        second = [
            item for item in decoded if predeploy_completed <= item[0] <= deploy_started
        ]
        _require(
            len(first) == 1 and len(second) == 1 and first[0][1] != second[0][1],
            "ui_approval_comments_not_separated_by_jobs",
        )
        first_ui = _validate_ui_approval_comment(
            first[0][1],
            run=run,
            reviewer_login=expected_reviewer_login,
            preflight_observed_at=preflight_observed_at,
            approval_job_started_at=predeploy_started,
            template_sha256=template_sha256,
            screenshot_fetcher=screenshot_fetcher,
        )
        second_ui = _validate_ui_approval_comment(
            second[0][1],
            run=run,
            reviewer_login=expected_reviewer_login,
            preflight_observed_at=predeploy_completed,
            approval_job_started_at=deploy_started,
            template_sha256=template_sha256,
            screenshot_fetcher=screenshot_fetcher,
        )
        _require(
            _time(second_ui["checked_at_utc"], "ui_approval_checked_at")
            > _time(first_ui["checked_at_utc"], "ui_approval_checked_at"),
            "ui_approval_timestamp_not_increasing",
        )
        checked_times = [first_ui["checked_at_utc"], second_ui["checked_at_utc"]]
        screenshots = [first_ui["screenshots"], second_ui["screenshots"]]
        job_started = deploy_started

    _require(observed >= job_started, "environment_approval_observation_order_invalid")
    return {
        "reviewer_login": expected_reviewer_login,
        "approval_observed_at_utc": observed.isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        ),
        "ui_checked_at_utc": checked_times[-1],
        "ui_checks": [
            {"checked_at_utc": checked, "screenshots": captured}
            for checked, captured in zip(checked_times, screenshots)
        ],
        **(deploy_job_metadata or {}),
    }


def validate_deployment_history(
    token: str,
    *,
    current_run_id: str,
    operation_id: str,
    mode: str,
    github_collection: Callable[..., list[Any]] | None = None,
) -> dict[str, Any]:
    collect = (
        github_preflight.github_collection
        if github_collection is None
        else github_collection
    )
    runs = collect(
        f"{API_ROOT}{API_PREFIX}/actions/workflows/asaas-producer-image-deploy.yml/runs?event=workflow_dispatch&per_page=100&page=1",
        token,
        list_field="workflow_runs",
    )
    current = None
    started_bootstrap_ids: list[str] = []
    prior_operations: set[str] = set()
    for run in runs:
        _require(isinstance(run, dict), "rollout_history_invalid")
        run_id = run.get("id")
        title = run.get("display_title")
        _require(
            type(run_id) is int and run_id > 0 and isinstance(title, str),
            "rollout_history_invalid",
        )
        if run_id == int(current_run_id):
            current = run
            continue
        _require(
            run.get("event") == "workflow_dispatch"
            and run.get("path") == f"{DEPLOY_WORKFLOW_PATH}@{RELEASE_REF}"
            and run.get("head_branch") == "main"
            and isinstance(run.get("head_sha"), str)
            and re.fullmatch(r"[0-9a-f]{40}", run["head_sha"]) is not None,
            "rollout_history_run_identity_invalid",
        )
        match = HISTORY_RUN_NAME.fullmatch(title)
        _require(match is not None, "rollout_history_unattributable")
        historic_operation = match.group("operation")
        try:
            canonical_operation = str(UUID(historic_operation))
            canonical_attestation = str(UUID(match.group("attestation")))
        except ValueError:
            raise GateError("rollout_history_artifact_tuple_invalid") from None
        _require(
            canonical_operation == historic_operation
            and canonical_attestation == match.group("attestation")
            and int(match.group("run")) > 0,
            "rollout_history_artifact_tuple_invalid",
        )
        _require(historic_operation != operation_id, "operation_id_already_used")
        prior_operations.add(historic_operation)
        if match.group("mode") != "bootstrap":
            continue
        jobs = collect(
            f"{API_ROOT}{API_PREFIX}/actions/runs/{run_id}/jobs?per_page=100&page=1",
            token,
            list_field="jobs",
        )
        deploy_jobs = [
            job for job in jobs if isinstance(job, dict) and job.get("name") == "deploy"
        ]
        _require(len(deploy_jobs) <= 1, "rollout_history_deploy_job_ambiguous")
        _require(
            all(job.get("head_sha") == run.get("head_sha") for job in deploy_jobs),
            "rollout_history_job_sha_mismatch",
        )
        if deploy_jobs and deploy_jobs[0].get("started_at") is not None:
            started_bootstrap_ids.append(str(run_id))
    _require(current is not None, "current_rollout_missing_from_history")
    if mode == "bootstrap":
        _require(not started_bootstrap_ids, "bootstrap_slot_already_consumed")
    return {
        "current_run_present": True,
        "prior_operation_count": len(prior_operations),
        "started_bootstrap_run_ids": started_bootstrap_ids,
        "bootstrap_slot": "available" if not started_bootstrap_ids else "consumed",
    }


def validate_build_artifact_tuple(
    env: dict[str, str], envelope: Any, trust: Any
) -> dict[str, str]:
    build_run_id = env.get("INPUT_BUILD_RUN_ID", "")
    attestation_id = env.get("INPUT_ATTESTATION_ID", "")
    digest = env.get("INPUT_APP_IMAGE_DIGEST", "")
    envelope_sha = env.get("INPUT_ENVELOPE_SHA256", "")
    pins = {
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
    }
    verified = validate_signed_attestation(
        envelope,
        trust_readback=trust,
        expected_build_run_id=build_run_id,
        expected_attestation_id=attestation_id,
        expected_digest=digest,
        expected_envelope_sha256=envelope_sha,
        env=pins,
    )
    return {
        "build_workflow_run_id": verified["build_workflow_run_id"],
        "attestation_id": verified["attestation_id"],
        "attestation_digest": verified["attestation_digest"],
        "app_image_digest": verified["app_image_digest"],
        "signed_envelope_sha256": verified["signed_envelope_sha256"],
    }


def validate_canonical_registration_comment(
    comments: list[Any],
    *,
    token: str,
    run_id: str,
    operation_id: str,
    attestation: dict[str, str],
    github_get: Callable[[str, str], tuple[Any, Any]] | None = None,
) -> dict[str, str]:
    get = github_preflight.github_get if github_get is None else github_get
    marker = "<!-- asaas-producer-registration.v1\n"
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for row in comments:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("body"), str)
            or not row["body"].startswith(marker)
        ):
            continue
        body = row["body"]
        _require(body.endswith("\n-->"), "canonical_registration_comment_invalid")
        try:
            value = _strict_loads(body[len(marker) : -4], max_bytes=8_192)
        except EvidenceError:
            raise GateError("canonical_registration_comment_invalid") from None
        _require(isinstance(value, dict), "canonical_registration_comment_invalid")
        if value.get("run_id") == run_id and value.get("operation_id") == operation_id:
            matches.append((row, value))
    _require(len(matches) == 1, "canonical_registration_evidence_count_invalid")
    comment, registration = matches[0]
    required = {
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
        set(registration) == required, "canonical_registration_evidence_shape_invalid"
    )
    _require(
        registration.get("schema_version") == "asaas_producer_registration.v1",
        "canonical_registration_schema_invalid",
    )
    _require(
        registration.get("attestation_id") == attestation["attestation_id"],
        "canonical_registration_attestation_id_mismatch",
    )
    _require(
        registration.get("attestation_digest") == attestation["signed_envelope_sha256"],
        "canonical_registration_digest_mismatch",
    )
    _require(
        registration.get("registrar_role") == "super_admin",
        "canonical_registration_super_admin_required",
    )
    _require(
        isinstance(registration.get("registrar_login"), str)
        and re.fullmatch(r"[A-Za-z0-9._@-]{1,128}", registration["registrar_login"]),
        "canonical_registration_registrar_invalid",
    )
    user = comment.get("user")
    _require(
        isinstance(user, dict) and isinstance(user.get("login"), str),
        "canonical_registration_author_invalid",
    )
    permission, _ = get(
        f"{API_ROOT}{API_PREFIX}/collaborators/{quote(user['login'], safe='')}/permission",
        token,
    )
    _require(
        isinstance(permission, dict) and permission.get("permission") == "admin",
        "canonical_registration_author_not_admin",
    )
    _require(
        registration.get("registrar_login") == user["login"],
        "canonical_registration_registrar_author_mismatch",
    )
    from scripts.verify_asaas_producer_image_rollout import validate_registration

    try:
        return validate_registration(
            {
                key: registration[key]
                for key in ("attestation_id", "attestation_digest", "registered_at")
            },
            attestation,
            mode="registered",
        )
    except RolloutError as exc:
        raise GateError(str(exc)) from None


def validate_private_before_readback(
    env: dict[str, str],
    *,
    expected_run: dict[str, Any],
    artifact_tuple: dict[str, str],
    template_json: str,
    readback_json: str,
    operator_login: str,
    operator_permission: str,
    observed_at: datetime | None = None,
) -> dict[str, Any]:
    template = _strict_loads(template_json)
    template_result = validate_task_definition_template(template)
    expected_template_hash = env.get(
        "ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_SHA256", ""
    )
    _require(
        template_result["template_sha256"] == expected_template_hash,
        "task_definition_template_sha256_mismatch",
    )
    raw = _strict_loads(readback_json, max_bytes=1_000_000)
    _require(isinstance(raw, dict), "operator_readback_shape_invalid")
    observed = (
        datetime.now(timezone.utc)
        if observed_at is None
        else observed_at.astimezone(timezone.utc)
    )
    run_created = _time(expected_run["created_at_utc"], "actions_run_created_at")
    latest = observed
    earliest = max(run_created, observed - MAX_BEFORE_READBACK_AGE)
    expected_env = _expected_task_environment(env)
    template_definition = template["task_definition"]
    app_containers = [
        row
        for row in template_definition["containerDefinitions"]
        if row.get("name") == "app"
    ]
    _require(len(app_containers) == 1, "database_url_template_container_ambiguous")
    application_secrets = [
        row for row in app_containers[0]["secrets"] if row["name"] == "DATABASE_URL"
    ]
    _require(len(application_secrets) == 1, "database_url_template_reference_ambiguous")
    template_source_reference = application_secrets[0]["valueFrom"]
    source_snapshot = raw.get("snapshot") if isinstance(raw, dict) else None
    _require(isinstance(source_snapshot, dict), "operator_snapshot_invalid")
    source_definition = source_snapshot.get("task_definition")
    _require(
        isinstance(source_definition, dict)
        and source_definition.get("taskDefinitionArn")
        == template["source_task_definition_arn"],
        "source_task_definition_template_mismatch",
    )
    source_containers = source_definition.get("containerDefinitions")
    _require(
        isinstance(source_containers, list), "source_task_definition_container_invalid"
    )
    source_apps = [
        row
        for row in source_containers
        if isinstance(row, dict) and row.get("name") == "app"
    ]
    _require(
        len(source_apps) == 1 and isinstance(source_apps[0].get("secrets"), list),
        "source_task_definition_container_invalid",
    )
    source_database_refs = [
        row.get("valueFrom")
        for row in source_apps[0]["secrets"]
        if isinstance(row, dict) and row.get("name") == "DATABASE_URL"
    ]
    _require(
        len(source_database_refs) == 1
        and source_database_refs[0] == template_source_reference,
        "database_url_source_reference_mismatch",
    )
    source_reference = source_database_refs[0]
    base_arn = ":".join(source_reference.split(":")[:7])
    ref_metadata = {
        "desired_count": source_snapshot.get("service", {}).get("desiredCount"),
        "active_tasks": source_snapshot.get("tasks"),
        "task_definitions": [source_definition],
        "secret_description": source_snapshot.get("secret_description"),
        "secret_versions": source_snapshot.get("secret_versions"),
    }
    try:
        database = resolve_active_database_secret(ref_metadata)
    except ReferenceError as exc:
        raise GateError(f"database_secret_{exc}") from None
    _require(
        database["database_secret_arn"] == base_arn,
        "database_secret_template_arn_mismatch",
    )
    source_parts = source_reference.split(":")
    source_json_key = source_parts[7] if len(source_parts) == 10 else ""
    _require(
        database["database_secret_json_key"] == source_json_key,
        "database_secret_template_key_mismatch",
    )
    fields = _readback_call_fields(
        env,
        expected_run,
        artifact_tuple,
        template_result,
        expected_env,
        operator_login,
        operator_permission,
        earliest,
        latest,
        database["database_secret_arn"],
        database["database_secret_json_key"],
        database["resolved_version_id"],
    )
    try:
        checked = validate_operator_readback(raw, **fields)
    except (EvidenceError, RolloutError) as exc:
        raise GateError(str(exc)) from None
    return {
        "phase": "before",
        "template_sha256": template_result["template_sha256"],
        "producer_environment_sha256": template_result["producer_environment_sha256"],
        "base_environment_sha256": template_result["base_environment_sha256"],
        "before_snapshot_sha256": checked["snapshot_sha256"],
        "previous_task_definition_arn": checked["snapshot"]["task_definition_arn"],
        "previous_image_digest": checked["snapshot"]["app_image_digest"],
        "desired_count": checked["snapshot"]["task_count"],
        "database_secret_version_id": checked["snapshot"]["database_secret_version_id"],
        "operator_login": checked["operator_login"],
        "operator_permission": checked["operator_permission"],
        "target_group": checked["target_group"],
        "readiness_probe": checked["readiness_probe"],
        "validated_snapshot": checked["snapshot"],
    }


def validate_post_readback_comment(
    *,
    comments: list[Any],
    token: str,
    private_key_pem: bytes,
    expected_context: dict[str, str],
    expected_run: dict[str, Any],
    expected_mode: str,
    expected_operation_id: str,
    artifact_tuple: dict[str, str],
    expected_template_sha256: str,
    expected_producer_environment_sha256: str,
    expected_operator_login: str,
    before_snapshot: dict[str, Any],
    expected_task_environment: dict[str, str],
    expected_image_uri: str,
    deploy_job_started_at: datetime,
    github_get: Callable[[str, str], tuple[Any, Any]] | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    get = github_preflight.github_get if github_get is None else github_get
    candidates = [
        row
        for row in comments
        if isinstance(row, dict)
        and isinstance(row.get("body"), str)
        and row["body"].startswith(f"<!-- {ENCRYPTED_READBACK_MARKER}\n")
    ]
    _require(len(candidates) == 1, "post_readback_comment_count_invalid")
    comment = candidates[0]
    author = comment.get("user")
    _require(
        isinstance(author, dict) and isinstance(author.get("login"), str),
        "post_readback_comment_author_invalid",
    )
    author_login = author["login"]
    created_at = _time(comment.get("created_at"), "post_readback_comment_created_at")
    _require(
        created_at >= deploy_job_started_at, "post_readback_comment_precedes_deployment"
    )
    permission, _ = get(
        f"{API_ROOT}{API_PREFIX}/collaborators/{quote(author_login, safe='')}/permission",
        token,
    )
    _require(
        isinstance(permission, dict) and permission.get("permission") == "admin",
        "post_readback_author_not_admin",
    )
    try:
        raw = parse_post_readback_comment(
            comment["body"],
            author_login=author_login,
            author_permission=permission["permission"],
            expected_operator_login=expected_operator_login,
            private_pem=private_key_pem,
            expected_context=expected_context,
        )
    except EvidenceError as exc:
        raise GateError(str(exc)) from None
    _require(
        raw.get("operation_id") == expected_operation_id
        and raw.get("mode") == expected_mode,
        "post_readback_run_tuple_mismatch",
    )
    _require(
        raw.get("run_id") == expected_run["run_id"]
        and raw.get("run_attempt") == expected_run["run_attempt"],
        "post_readback_run_tuple_mismatch",
    )
    expected_run_fields = {
        "build_workflow_run_id": expected_run["build_workflow_run_id"],
        "attestation_id": expected_run["attestation_id"],
        "app_image_digest": expected_run["app_image_digest"],
        "signed_envelope_sha256": expected_run["signed_envelope_sha256"],
    }
    _require(
        artifact_tuple == expected_run_fields, "post_readback_expected_artifact_invalid"
    )
    latest = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    try:
        after = validate_operator_readback(
            raw,
            phase="after",
            expected_run_id=expected_run["run_id"],
            expected_run_attempt=expected_run["run_attempt"],
            expected_sha=expected_run["sha"],
            expected_operation_id=expected_operation_id,
            mode=expected_mode,
            artifact_tuple=artifact_tuple,
            template_sha256=expected_template_sha256,
            producer_environment_sha256=expected_producer_environment_sha256,
            expected_base_environment_sha256=before_snapshot["base_environment_sha256"],
            expected_operator_login=expected_operator_login,
            operator_permission=permission["permission"],
            expected_environment=expected_task_environment,
            expected_task_role_arn=TASK_ROLE_ARN,
            expected_execution_role_arn=EXECUTION_ROLE_ARN,
            expected_image_uri=expected_image_uri,
            expected_secret_arn=before_snapshot["database_secret_arn"],
            expected_secret_json_key=before_snapshot["database_secret_json_key"],
            expected_secret_version_id=before_snapshot["database_secret_version_id"],
            earliest_allowed_at=deploy_job_started_at,
            latest_allowed_at=latest,
        )
        result = validate_before_after(
            {
                "phase": "before",
                "operator_login": expected_operator_login,
                "operator_permission": permission["permission"],
                "template_sha256": expected_template_sha256,
                "base_environment_sha256": before_snapshot["base_environment_sha256"],
                "target_group": before_snapshot["target_group"],
                "readiness_probe": before_snapshot["readiness_probe"],
                "snapshot": before_snapshot["validated_snapshot"],
                "snapshot_sha256": before_snapshot["before_snapshot_sha256"],
            },
            after,
            expected_digest=artifact_tuple["app_image_digest"],
        )
    except (EvidenceError, RolloutError) as exc:
        raise GateError(str(exc)) from None
    return after, result


def _expected_task_environment(env: dict[str, str]) -> dict[str, str]:
    expected = {
        "BILLING_ROLLOUT_STATE": "disabled",
        "BILLING_TOPUP_COMMERCIAL_ENABLED": "false",
    }
    for kind in ("GRANT", "BUILD", "RECEIPT"):
        expected[f"BILLING_PRODUCER_{kind}_KID"] = env.get(
            f"BILLING_PRODUCER_{kind}_KMS_KEY_ARN", ""
        )
        expected[f"BILLING_PRODUCER_{kind}_PUBLIC_KEY"] = env.get(
            f"BILLING_PRODUCER_{kind}_PUBLIC_KEY_PEM", ""
        )
    return expected


def _readback_call_fields(
    env: dict[str, str],
    expected_run: dict[str, Any],
    artifact_tuple: dict[str, str],
    template_result: dict[str, Any],
    expected_environment: dict[str, str],
    operator_login: str,
    operator_permission: str,
    earliest: datetime,
    latest: datetime,
    database_secret_arn: str,
    database_secret_json_key: str,
    database_secret_version_id: str,
    *,
    phase: str = "before",
) -> dict[str, Any]:
    return {
        "phase": phase,
        "expected_run_id": expected_run["run_id"],
        "expected_run_attempt": expected_run["run_attempt"],
        "expected_sha": expected_run["sha"],
        "expected_operation_id": env["INPUT_OPERATION_ID"],
        "mode": env["INPUT_MODE"],
        "artifact_tuple": artifact_tuple,
        "template_sha256": template_result["template_sha256"],
        "producer_environment_sha256": template_result["producer_environment_sha256"],
        "expected_base_environment_sha256": template_result["base_environment_sha256"],
        "expected_operator_login": operator_login,
        "operator_permission": operator_permission,
        "expected_environment": expected_environment,
        "expected_task_role_arn": TASK_ROLE_ARN,
        "expected_execution_role_arn": EXECUTION_ROLE_ARN,
        "expected_image_uri": env.get("BILLING_PRODUCER_ECR_REPOSITORY_URI", ""),
        "expected_secret_arn": database_secret_arn,
        "expected_secret_json_key": database_secret_json_key,
        "expected_secret_version_id": database_secret_version_id,
        "earliest_allowed_at": earliest,
        "latest_allowed_at": latest,
    }


def _deployment_job_context(token: str, run_id: str) -> dict[str, str]:
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
        "deployment_job_context_unavailable",
    )
    started_at = matches[0]["started_at"]
    _time(started_at, "deployment_job_started_at")
    return {"job_id": str(matches[0]["id"]), "job_started_at": started_at}


def _pre_oidc_cohort_context(
    env: dict[str, str], run: dict[str, Any], job: dict[str, str]
) -> dict[str, str]:
    return {
        "run_id": run["run_id"],
        "run_attempt": run["run_attempt"],
        "operation_id": env["INPUT_OPERATION_ID"],
        "phase": "cohort",
        "artifact_sha256": env["INPUT_ENVELOPE_SHA256"],
        "ref": run["ref"],
        "sha": run["sha"],
        **job,
    }


def wait_for_pre_oidc_cohort_confirmation(
    *,
    env: dict[str, str],
    token: str,
    run: dict[str, Any],
    artifact_tuple: dict[str, str],
    template_result: dict[str, Any],
    expected_environment: dict[str, str],
    operator_login: str,
    operator_permission: str,
    before: dict[str, Any],
    private_key_path: Path,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Wait for a fresh encrypted operator snapshot after deploy approval."""

    _require(private_key_path.is_file(), "cohort_confirmation_private_key_missing")
    job = _deployment_job_context(token, run["run_id"])
    context = _pre_oidc_cohort_context(env, run, job)
    deadline = time.monotonic() + MAX_PRE_OIDC_COHORT_WAIT_SECONDS
    marker = f"<!-- {ENCRYPTED_READBACK_MARKER}\n"
    matching: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        comments = github_preflight.github_collection(
            f"{API_ROOT}{API_PREFIX}/issues/{env.get('INPUT_EVIDENCE_ISSUE_NUMBER', '')}/comments?per_page=100&page=1",
            token,
        )
        matching = []
        for row in comments:
            if not isinstance(row, dict) or not isinstance(row.get("body"), str):
                continue
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
    _require(bool(matching), "pre_oidc_cohort_confirmation_timeout")
    _require(len(matching) == 1, "pre_oidc_cohort_confirmation_ambiguous")
    comment = matching[0]
    author = comment.get("user")
    _require(
        isinstance(author, dict) and isinstance(author.get("login"), str),
        "cohort_confirmation_author_invalid",
    )
    permission, _ = github_preflight.github_get(
        f"{API_ROOT}{API_PREFIX}/collaborators/{quote(author['login'], safe='')}/permission",
        token,
    )
    _require(
        isinstance(permission, dict) and permission.get("permission") == "admin",
        "cohort_confirmation_author_not_admin",
    )
    try:
        raw = parse_post_readback_comment(
            comment["body"],
            author_login=author["login"],
            author_permission=permission["permission"],
            expected_operator_login=operator_login,
            private_pem=private_key_path.read_bytes(),
            expected_context=context,
        )
    except (EvidenceError, OSError) as exc:
        if isinstance(exc, OSError):
            raise GateError("cohort_confirmation_private_key_unreadable") from None
        raise GateError(str(exc)) from None
    _require(
        isinstance(raw, dict), "cohort_confirmation_payload_invalid"
    )
    job_started_at = _time(job["job_started_at"], "deployment_job_started_at")
    observed = datetime.now(timezone.utc) if now is None else now.astimezone(timezone.utc)
    created_at = _time(comment.get("created_at"), "cohort_confirmation_comment_time")
    _require(
        created_at >= job_started_at and created_at >= _time(
            raw.get("checked_at_utc"), "cohort_confirmation_checked_at"
        ),
        "cohort_confirmation_comment_time_invalid",
    )
    checked_at = _time(raw.get("checked_at_utc"), "cohort_confirmation_checked_at")
    _require(
        observed - checked_at <= MAX_COHORT_READBACK_AGE,
        "cohort_confirmation_snapshot_stale",
    )
    fields = _readback_call_fields(
        env,
        run,
        artifact_tuple,
        template_result,
        expected_environment,
        operator_login,
        permission["permission"],
        job_started_at,
        observed,
        before["validated_snapshot"]["database_secret_arn"],
        before["validated_snapshot"]["database_secret_json_key"],
        before["database_secret_version_id"],
        phase="cohort",
    )
    try:
        cohort = validate_operator_readback(raw, **fields)
        verification = validate_pre_oidc_cohort_confirmation(before, cohort)
    except (EvidenceError, RolloutError, ReferenceError) as exc:
        raise GateError(str(exc)) from None
    evidence = {
        "schema": "asaas_producer_pre_oidc_cohort_confirmation.v1",
        "run_id": run["run_id"],
        "run_attempt": run["run_attempt"],
        "ref": run["ref"],
        "sha": run["sha"],
        "operation_id": env["INPUT_OPERATION_ID"],
        "artifact_sha256": env["INPUT_ENVELOPE_SHA256"],
        "deployment_job_id": job["job_id"],
        "deployment_job_started_at": job["job_started_at"],
        "comment_created_at": comment["created_at"],
        "checked_at_utc": raw["checked_at_utc"],
        "operator_login": operator_login,
        "encrypted_snapshot_sha256": canonical_sha256(raw),
        "operator_readback": raw,
        "validated_cohort": cohort,
        "verification": verification,
    }
    evidence["evidence_sha256"] = hashlib.sha256(_canonical(evidence)).hexdigest()
    return evidence


def write_private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as output:
        output.write(_canonical(value) + b"\n")
        output.flush()
        os.fsync(output.fileno())


def prepare_group2_build_trust_readback(env: dict[str, str]) -> str:
    required_env = (
        "BILLING_PRODUCER_BUILD_ISSUER",
        "BILLING_PRODUCER_BUILD_KMS_KEY_ARN",
        "BILLING_PRODUCER_BUILD_AUDIENCE",
        "BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM",
        "BILLING_PRODUCER_BUILD_OIDC_SUBJECT",
    )
    for name in required_env:
        _require(bool(env.get(name, "").strip()), "build_trust_pin_missing")
    trust = {
        "schema": "billing_producer_build_trust_readback.v1",
        "schema_version": "v1",
        "environment": "staging",
        "type": "billing_build_attestation.v1",
        "purpose": "billing-build-attestation.v1",
        "algorithm": "ECDSA_SHA_256",
        "issuer": env["BILLING_PRODUCER_BUILD_ISSUER"],
        "kid": env["BILLING_PRODUCER_BUILD_KMS_KEY_ARN"],
        "audience": env["BILLING_PRODUCER_BUILD_AUDIENCE"],
        "public_key_fingerprint_sha256": _public_key_fingerprint(
            env["BILLING_PRODUCER_BUILD_PUBLIC_KEY_PEM"]
        ),
        "repository_id": str(REPOSITORY_ID),
        "repository_name": REPOSITORY,
        "workflow_path": ".github/workflows/asaas-build-attestation.yml",
        "workflow_ref": "refs/heads/main",
        "oidc_issuer": "https://token.actions.githubusercontent.com",
        "oidc_subject": env["BILLING_PRODUCER_BUILD_OIDC_SUBJECT"],
    }
    _require(
        bool(env.get("BILLING_PRODUCER_ECR_REPOSITORY_URI", "")),
        "build_ecr_repository_pin_missing",
    )
    encoded = _canonical(trust).decode("utf-8")
    runner_temp = env.get("RUNNER_TEMP")
    if runner_temp:
        path = Path(runner_temp) / "asaas-producer-build-trust.json"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(encoded + "\n")
    env_file = env.get("GITHUB_ENV")
    if env_file:
        with open(env_file, "a", encoding="utf-8") as output:
            output.write(f"ASAAS_PRODUCER_BUILD_TRUST_READBACK_JSON={encoded}\n")
    return encoded


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=(
            "prepare-trust",
            "preflight",
            "predeploy",
            "deploy",
            "pre-oidc-cohort",
        ),
        default="preflight",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        env = dict(os.environ)
        if args.phase == "prepare-trust":
            prepare_group2_build_trust_readback(env)
            print("Build trust readback: ready")
            return 0
        token = env.get("GH_TOKEN", "")
        _require(bool(token), "github_read_token_unavailable")
        protections = validate_repository_protections(token)
        run = validate_current_run(env, token, stage=args.phase)
        history = validate_deployment_history(
            token,
            current_run_id=run["run_id"],
            operation_id=env["INPUT_OPERATION_ID"],
            mode=env["INPUT_MODE"],
        )
        protection_hash = hashlib.sha256(_canonical(protections)).hexdigest()
        if args.phase != "preflight":
            validate_runtime_deploy_role_variable(
                env.get(DEPLOY_ROLE_VARIABLE), protections["deploy_role_arn"]
            )
            expected_protection_hash = env.get("ASAAS_PREFLIGHT_PROTECTION_SHA256", "")
            _require(
                re.fullmatch(r"[0-9a-f]{64}", expected_protection_hash) is not None
                and protection_hash == expected_protection_hash,
                "github_controls_changed_after_preflight",
            )
            template = _strict_loads(
                env.get("ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_JSON", "")
            )
            template_result = validate_task_definition_template(template)
            expected_template_hash = env.get(
                "ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_SHA256", ""
            )
            _require(
                template_result["template_sha256"] == expected_template_hash,
                "task_definition_template_sha256_mismatch",
            )
            before_value = _strict_loads(
                env.get("ASAAS_PRODUCER_PREDEPLOY_READBACK_JSON", ""),
                max_bytes=1_000_000,
            )
            operator_login = (
                before_value.get("operator_login")
                if isinstance(before_value, dict)
                else None
            )
            _require(
                isinstance(operator_login, str), "operator_readback_operator_invalid"
            )
            envelope = _strict_loads(
                Path(env["ASAAS_SIGNED_ENVELOPE_PATH"]).read_bytes()
            )
            trust = _strict_loads(
                env.get("ASAAS_PRODUCER_BUILD_TRUST_READBACK_JSON", "")
            )
            verified_artifact = validate_build_artifact_tuple(env, envelope, trust)
            if env.get("INPUT_MODE") == "registered":
                comments = github_preflight.github_collection(
                    f"{API_ROOT}{API_PREFIX}/issues/{env.get('INPUT_EVIDENCE_ISSUE_NUMBER', '')}/comments?per_page=100&page=1",
                    token,
                )
                registration = validate_canonical_registration_comment(
                    comments,
                    token=token,
                    run_id=run["run_id"],
                    operation_id=env["INPUT_OPERATION_ID"],
                    attestation=verified_artifact,
                )
                _require(
                    registration["registration_attestation_id"]
                    == env.get("INPUT_REGISTRATION_ATTESTATION_ID"),
                    "registration_input_attestation_mismatch",
                )
                _require(
                    registration["registration_attestation_digest"]
                    == env.get("INPUT_REGISTRATION_DIGEST"),
                    "registration_input_digest_mismatch",
                )
                _require(
                    registration["registered_at_utc"]
                    == env.get("INPUT_REGISTERED_AT_UTC"),
                    "registration_input_time_mismatch",
                )
            else:
                _require(env.get("INPUT_MODE") == "bootstrap", "rollout_mode_invalid")
            approval = validate_environment_approval(
                token=token,
                run=run,
                phase="predeploy" if args.phase == "predeploy" else "deploy",
                expected_reviewer_login=operator_login,
                actor_id=run["actor_id"],
                template_sha256=template_result["template_sha256"],
                preflight_observed_at=_time(
                    env.get("ASAAS_PREFLIGHT_OBSERVED_AT"), "preflight_observed_at"
                ),
            )
            permission, _ = github_preflight.github_get(
                f"{API_ROOT}{API_PREFIX}/collaborators/{quote(operator_login, safe='')}/permission",
                token,
            )
            _require(
                isinstance(permission, dict)
                and permission.get("permission") == "admin",
                "operator_readback_admin_not_verified",
            )
            artifact_tuple = {
                "build_workflow_run_id": env["INPUT_BUILD_RUN_ID"],
                "attestation_id": env["INPUT_ATTESTATION_ID"],
                "app_image_digest": env["INPUT_APP_IMAGE_DIGEST"],
                "signed_envelope_sha256": env["INPUT_ENVELOPE_SHA256"],
            }
            before = validate_private_before_readback(
                env,
                expected_run=run,
                artifact_tuple=artifact_tuple,
                template_json=env.get(
                    "ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_JSON", ""
                ),
                readback_json=env.get("ASAAS_PRODUCER_PREDEPLOY_READBACK_JSON", ""),
                operator_login=operator_login,
                operator_permission=permission["permission"],
            )
            if args.phase in {"deploy", "pre-oidc-cohort"}:
                _require(
                    before["before_snapshot_sha256"]
                    == env.get("ASAAS_PREDEPLOY_READBACK_SHA256"),
                    "predeploy_readback_changed_after_approval",
                )
                _require(
                    before["producer_environment_sha256"]
                    == env.get("ASAAS_PREDEPLOY_PRODUCER_ENVIRONMENT_SHA256"),
                    "producer_environment_changed_after_approval",
                )
            cohort_evidence = None
            if args.phase == "pre-oidc-cohort":
                _require(args.output is not None, "cohort_confirmation_output_required")
                cohort_evidence = wait_for_pre_oidc_cohort_confirmation(
                    env=env,
                    token=token,
                    run=run,
                    artifact_tuple=artifact_tuple,
                    template_result=template_result,
                    expected_environment=_expected_task_environment(env),
                    operator_login=operator_login,
                    operator_permission=permission["permission"],
                    before=before,
                    private_key_path=(
                        Path(env["RUNNER_TEMP"])
                        / "asaas-producer-post-readback-private.pem"
                    ),
                )
                latest_protections = validate_repository_protections(token)
                latest_protection_hash = hashlib.sha256(
                    _canonical(latest_protections)
                ).hexdigest()
                _require(
                    latest_protection_hash
                    == env.get("ASAAS_PREFLIGHT_PROTECTION_SHA256"),
                    "github_controls_changed_during_cohort_confirmation",
                )
                validate_runtime_deploy_role_variable(
                    env.get(DEPLOY_ROLE_VARIABLE),
                    latest_protections["deploy_role_arn"],
                )
                latest_run = validate_current_run(
                    env, token, stage="pre-oidc-cohort"
                )
                _require(
                    latest_run == run,
                    "actions_run_changed_during_cohort_confirmation",
                )
                validate_deployment_history(
                    token,
                    current_run_id=run["run_id"],
                    operation_id=env["INPUT_OPERATION_ID"],
                    mode=env["INPUT_MODE"],
                )
                validate_environment_approval(
                    token=token,
                    run=run,
                    phase="deploy",
                    expected_reviewer_login=operator_login,
                    actor_id=run["actor_id"],
                    template_sha256=template_result["template_sha256"],
                    preflight_observed_at=_time(
                        env.get("ASAAS_PREFLIGHT_OBSERVED_AT"),
                        "preflight_observed_at",
                    ),
                )
                current_job = _deployment_job_context(token, run["run_id"])
                _require(
                    current_job["job_id"]
                    == cohort_evidence["deployment_job_id"]
                    and current_job["job_started_at"]
                    == cohort_evidence["deployment_job_started_at"],
                    "deployment_job_changed_during_cohort_confirmation",
                )
            if args.output is not None:
                if args.phase == "pre-oidc-cohort":
                    write_private_json(args.output, cohort_evidence)
                else:
                    write_private_json(
                        args.output,
                        (
                            before
                            if args.phase == "predeploy"
                            else {
                                **before,
                                "approval_observed_at_utc": approval[
                                    "approval_observed_at_utc"
                                ],
                            }
                        ),
                    )
        else:
            preflight_observed_at = datetime.now(timezone.utc)
            if args.output is not None:
                write_private_json(
                    args.output,
                    {
                        "run_id": run["run_id"],
                        "protection_sha256": protection_hash,
                        "history": history,
                        "observed_at_utc": preflight_observed_at.isoformat(
                            timespec="microseconds"
                        ).replace("+00:00", "Z"),
                    },
                )
        print("GitHub rollout protections and run history: ready")
        if args.phase == "predeploy":
            print("Protected task definition and database readiness: ready")
        if args.phase == "pre-oidc-cohort":
            print("Fresh pre-OIDC task cohort and readiness confirmation: ready")
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                output.write(f"run_id={run['run_id']}\n")
                output.write(f"protection_sha256={protection_hash}\n")
                if args.phase == "preflight":
                    output.write(
                        f"preflight_observed_at={preflight_observed_at.isoformat(timespec='microseconds').replace('+00:00', 'Z')}\n"
                    )
                if args.phase == "predeploy":
                    output.write(f"operator_login={operator_login}\n")
                    output.write(
                        f"task_definition_template_sha256={template_result['template_sha256']}\n"
                    )
                    output.write(
                        f"predeploy_readback_sha256={before['before_snapshot_sha256']}\n"
                    )
                    output.write(
                        f"predeploy_producer_environment_sha256={before['producer_environment_sha256']}\n"
                    )
                if args.phase == "deploy":
                    output.write(f"operator_login={operator_login}\n")
                    output.write(
                        f"deployment_job_id={approval['deployment_job_id']}\n"
                    )
                    output.write(
                        "deployment_job_started_at="
                        f"{approval['deployment_job_started_at']}\n"
                    )
                if args.phase == "pre-oidc-cohort":
                    output.write(
                        f"cohort_evidence_path={args.output}\n"
                    )
                    output.write(
                        "cohort_evidence_sha256="
                        f"{cohort_evidence['evidence_sha256']}\n"
                    )
                if args.output is not None and args.phase != "pre-oidc-cohort":
                    output.write(f"predeploy_readback_path={args.output}\n")
        return 0
    except (
        GateError,
        EvidenceError,
        RolloutError,
        ReferenceError,
        KeyError,
        TypeError,
        ValueError,
        OSError,
    ) as exc:
        reason = (
            str(exc)
            if isinstance(exc, (GateError, EvidenceError, RolloutError, ReferenceError))
            else "rollout_preflight_failed"
        )
        print(f"blocked: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
