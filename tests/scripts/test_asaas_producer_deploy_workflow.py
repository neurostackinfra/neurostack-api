from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/asaas-producer-image-deploy.yml"
RUNBOOK = ROOT / "docs/runbooks/asaas-producer-image-rollout-group5.md"
ENVIRONMENT = "asaas-production-deployment"
_SENSITIVE_NAMES = {
    "ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_JSON",
    "ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_SHA256",
    "ASAAS_PRODUCER_PREDEPLOY_READBACK_JSON",
}


def _workflow() -> dict[str, object]:
    parsed = yaml.load(WORKFLOW.read_text(encoding="utf-8"), Loader=yaml.BaseLoader)
    assert isinstance(parsed, dict)
    return parsed


def _walk(value: object):
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _walk(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _walk(nested)


def test_workflow_has_exact_manual_inputs_and_serialized_execution() -> None:
    workflow = _workflow()
    assert workflow["concurrency"] == {
        "group": "asaas-producer-deployment",
        "cancel-in-progress": "false",
    }
    inputs = workflow["on"]["workflow_dispatch"]["inputs"]
    assert set(inputs) == {
        "mode",
        "operation_id",
        "build_run_id",
        "attestation_id",
        "app_image_digest",
        "envelope_sha256",
        "evidence_issue_number",
        "registration_attestation_id",
        "registration_digest",
        "registered_at_utc",
    }
    assert not any(
        "task" in key or "secret" in key or "snapshot" in key for key in inputs
    )
    assert workflow["permissions"] == {}
    assert set(workflow["on"]) == {"workflow_dispatch"}


def test_only_deploy_job_can_request_oidc_and_it_is_after_all_no_oidc_gates() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    oidc_permissions = [
        (name, job.get("permissions", {}).get("id-token"))
        for name, job in jobs.items()
        if "id-token" in job.get("permissions", {})
    ]
    assert oidc_permissions == [("deploy", "write")]
    assert workflow["permissions"] == {}
    allowed = {"actions", "contents", "id-token"}
    assert all(
        set(job.get("permissions", {})) <= allowed
        for job in jobs.values()
    )
    assert all("issues" not in job.get("permissions", {}) for job in jobs.values())
    assert jobs["preflight"].get("environment") is None
    assert jobs["verify-predeploy-evidence"]["environment"] == ENVIRONMENT
    assert jobs["deploy"]["environment"] == ENVIRONMENT
    assert jobs["deploy"]["needs"] == ["preflight", "verify-predeploy-evidence"]
    assert jobs["deploy"]["if"] == (
        "needs.preflight.result == 'success' && "
        "needs.verify-predeploy-evidence.result == 'success'"
    )
    assert "id-token" not in jobs["preflight"].get("permissions", {})
    assert "id-token" not in jobs["verify-predeploy-evidence"].get("permissions", {})
    assert not any(
        "BILLING_PRODUCER_DEPLOY_ROLE_ARN" in node.get("env", {})
        for node in _walk(jobs["preflight"])
        if isinstance(node, dict)
    )

    deploy_steps = jobs["deploy"]["steps"]
    aws_steps = [
        index
        for index, step in enumerate(deploy_steps)
        if step.get("uses", "").startswith("aws-actions/configure-aws-credentials@")
    ]
    gate_steps = [
        index
        for index, step in enumerate(deploy_steps)
        if step.get("name")
        == "Verify current approval, environment secrets, artifact and readbacks before OIDC"
    ]
    key_steps = [
        index
        for index, step in enumerate(deploy_steps)
        if step.get("name")
        == "Generate one-run key and publish pre-OIDC cohort instructions"
    ]
    cohort_steps = [
        index
        for index, step in enumerate(deploy_steps)
        if step.get("name") == "Validate fresh run-bound cohort confirmation before OIDC"
    ]
    freshness_steps = [
        index
        for index, step in enumerate(deploy_steps)
        if step.get("name")
        == "Recheck administrator UI evidence freshness at OIDC boundary"
    ]
    assert len(aws_steps) == len(gate_steps) == len(key_steps) == len(cohort_steps) == len(freshness_steps) == 1
    assert gate_steps[0] < key_steps[0] < cohort_steps[0] < freshness_steps[0] < aws_steps[0]
    assert deploy_steps[aws_steps[0]]["with"]["role-to-assume"] == (
        "${{ env.BILLING_PRODUCER_DEPLOY_ROLE_ARN }}"
    )
    assert "BILLING_PRODUCER_DEPLOY_ROLE_ARN" in jobs["deploy"]["env"]
    assert "BILLING_PRODUCER_DEPLOY_ROLE_ARN" in deploy_steps[gate_steps[0]]["env"]
    assert "ASAAS_PRE_OIDC_COHORT_PATH" in deploy_steps[-1]["env"]
    assert "ASAAS_PRE_OIDC_COHORT_SHA256" in deploy_steps[-1]["env"]
    assert deploy_steps[cohort_steps[0]]["id"] == "cohort_confirmation"
    assert "--phase pre-oidc-cohort" in deploy_steps[cohort_steps[0]]["run"]
    assert deploy_steps[freshness_steps[0]]["env"]["UI_CHECKED_AT_UTC"] == (
        "${{ steps.pre_oidc_gate.outputs.ui_checked_at_utc }}"
    )
    assert "asaas_oidc_ui_freshness.py" in deploy_steps[freshness_steps[0]]["run"]
    assert "--checked-at-utc" in deploy_steps[freshness_steps[0]]["run"]
    assert all(
        not step.get("uses", "").startswith("aws-actions/")
        for step in deploy_steps[: aws_steps[0]]
    )
    assert (
        sum(
            1
            for node in _walk(workflow)
            if isinstance(node, dict)
            and node.get("uses", "").startswith(
                "aws-actions/configure-aws-credentials@"
            )
        )
        == 1
    )

    serialized = WORKFLOW.read_text(encoding="utf-8")
    assert serialized.count("id-token: write") == 1
    runbook = RUNBOOK.read_text(encoding="utf-8").lower()
    assert "job-scoped, not step-scoped" in runbook
    assert (
        "approved group 2 output readback for stack "
        "`neurostack-asaas-producer-release` records `deployrolearn=" in runbook
    )
    assert "neurostack-asaas-producer-release-deployrole-cu6auecqci9i" in runbook


def test_private_secrets_are_scoped_to_protected_environment_steps_only() -> None:
    workflow = _workflow()
    jobs = workflow["jobs"]
    occurrences: dict[str, list[str]] = {name: [] for name in _SENSITIVE_NAMES}
    for job_name, job in jobs.items():
        for node in _walk(job):
            env = node.get("env")
            if isinstance(env, dict):
                for name in _SENSITIVE_NAMES & env.keys():
                    occurrences[name].append(job_name)
    assert all(
        set(job_names) <= {"verify-predeploy-evidence", "deploy"}
        for job_names in occurrences.values()
    )
    assert all(occurrences.values())
    assert "ASAAS_PRODUCER_TASK_DEFINITION_TEMPLATE_JSON" not in str(
        workflow["on"]["workflow_dispatch"]["inputs"]
    )
    assert "ASAAS_PRODUCER_PREDEPLOY_READBACK_JSON" not in str(
        workflow["on"]["workflow_dispatch"]["inputs"]
    )


def test_every_action_is_pinned_and_workflow_never_uploads_raw_evidence() -> None:
    workflow = _workflow()
    uses = [
        node["uses"]
        for node in _walk(workflow)
        if isinstance(node, dict) and "uses" in node
    ]
    assert uses
    assert all(re.search(r"@[0-9a-f]{40}$", action) for action in uses)
    serialized = WORKFLOW.read_text(encoding="utf-8")
    assert "actions/upload-artifact" not in serialized
    assert "GetSecretValue" not in serialized
    assert "JWT" not in serialized
    assert "task_definition" not in str(workflow["on"]["workflow_dispatch"]["inputs"])


def test_evidence_cli_imports_when_run_by_repository_relative_path() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/asaas_producer_rollout_evidence.py"),
            "--help",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert "Validate private, run-bound operator evidence" in completed.stdout
    assert completed.stderr == ""
