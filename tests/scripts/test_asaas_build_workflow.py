from __future__ import annotations

import copy
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

_WORKFLOW = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "asaas-build-attestation.yml"
)
_REFERENCE = (
    "asaas-producer-image-rollout/WISH.md#"
    "2026-10-15-separate-build-and-rollout-plan-approved"
)
_EXPECTED_ACTION_PINS = {
    "actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683",
    "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
    "aws-actions/configure-aws-credentials@7474bc4690e29a8392af63c5b98e7449536d5c3a",
    "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
}


def _workflow() -> tuple[str, dict]:
    source = _WORKFLOW.read_text(encoding="utf-8")
    return source, yaml.safe_load(source)


def _gate_namespace() -> dict:
    _, workflow = _workflow()
    preflight = workflow["jobs"]["preflight"]
    gate_step = next(step for step in preflight["steps"] if step.get("id") == "gate")
    match = re.search(r"python3 - <<'PY'\n(?P<script>.*?)\nPY", gate_step["run"], re.S)
    assert match is not None
    namespace = {"__name__": "asaas_build_workflow_test"}
    exec(compile(match.group("script"), str(_WORKFLOW), "exec"), namespace)
    return namespace


def _valid_scope() -> dict[str, str]:
    repository = "neurostackinfra/neurostack-api"
    workflow_path = ".github/workflows/asaas-build-attestation.yml"
    workflow_ref = "refs/heads/main"
    return {
        "INPUT_OPERATION_ID": "89bf1556-9f6d-4f39-b5c8-8dcb041877da",
        "INPUT_WISH": "asaas-producer-image-rollout",
        "INPUT_GROUP": "3",
        "INPUT_AUTHORIZATION_REFERENCE": _REFERENCE,
        "GITHUB_REPOSITORY": repository,
        "GITHUB_REPOSITORY_ID": "1152335815",
        "GITHUB_REPOSITORY_OWNER_ID": "340698182",
        "GITHUB_REF": workflow_ref,
        "GITHUB_WORKFLOW_REF": f"{repository}/{workflow_path}@{workflow_ref}",
        "PIN_REPOSITORY_ID": "1152335815",
        "PIN_REPOSITORY_NAME": repository,
        "PIN_WORKFLOW_PATH": workflow_path,
        "PIN_WORKFLOW_REF": workflow_ref,
        "PIN_ROLE_ARN": "arn:aws:iam::388460142431:role/asaas-build",
        "PIN_KMS_KEY_ARN": (
            "arn:aws:kms:us-east-1:388460142431:key/"
            "01234567-89ab-cdef-0123-456789abcdef"
        ),
        "PIN_AUDIENCE": "sts.amazonaws.com",
        "PIN_ISSUER": "asaas-build-test-issuer",
        "PIN_OIDC_ISSUER": "https://token.actions.githubusercontent.com",
        "PIN_OIDC_SUBJECT": (
            "repo:neurostackinfra@340698182/neurostack-api@1152335815:environment:"
            "asaas-build:ref:refs/heads/main:workflow_ref:"
            "neurostackinfra/neurostack-api/.github/workflows/"
            "asaas-build-attestation.yml@refs/heads/main"
        ),
        "PIN_ECR_URI": ("388460142431.dkr.ecr.us-east-1.amazonaws.com/asaas-producer"),
        "BUILD_PUBLIC_KEY": "pinned-public-key",
    }


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("INPUT_WISH", "", "wish_scope_invalid"),
        ("INPUT_WISH", "asaas-payment-gateway", "wish_scope_invalid"),
        ("INPUT_WISH", "asaas-gateway-payment", "wish_scope_invalid"),
        ("INPUT_GROUP", "7b", "group_scope_invalid"),
        ("INPUT_GROUP", "", "group_scope_invalid"),
        ("INPUT_AUTHORIZATION_REFERENCE", "", "authorization_reference_unrecorded"),
        (
            "INPUT_AUTHORIZATION_REFERENCE",
            "different-wish",
            "authorization_reference_unrecorded",
        ),
        ("INPUT_OPERATION_ID", "", "operation_id_invalid"),
    ],
)
def test_scope_gate_rejects_missing_invalid_and_non_rollout_scope(
    field: str, value: str, reason: str
) -> None:
    namespace = _gate_namespace()
    env = _valid_scope()
    env[field] = value

    with pytest.raises(namespace["GateError"], match=reason):
        namespace["validate_scope"](env)


def test_scope_gate_accepts_only_documented_rollout_group_3_or_4() -> None:
    namespace = _gate_namespace()
    for group in ("3", "4"):
        env = _valid_scope()
        env["INPUT_GROUP"] = group
        namespace["validate_scope"](env)


def _ruleset() -> dict:
    return {
        "id": 123,
        "name": "asaas-producer-release-main",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/heads/main"], "exclude": []}},
        "bypass_actors": [],
        "rules": [
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 1,
                    "dismiss_stale_reviews_on_push": True,
                    "require_last_push_approval": True,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [
                        {"context": "ASAAS Release Preflight", "integration_id": None}
                    ],
                },
            },
            {"type": "deletion"},
            {"type": "non_fast_forward"},
        ],
    }


def test_ruleset_gate_rejects_omitted_bypass_and_weakened_controls() -> None:
    namespace = _gate_namespace()
    cases = []
    missing_bypass = _ruleset()
    missing_bypass.pop("bypass_actors")
    cases.append((missing_bypass, "ruleset_bypass_not_empty_or_unreadable"))
    allowed_bypass = _ruleset()
    allowed_bypass["bypass_actors"] = [{"actor_id": 1, "actor_type": "Organization"}]
    cases.append((allowed_bypass, "ruleset_bypass_not_empty_or_unreadable"))
    wrong_ref = _ruleset()
    wrong_ref["conditions"]["ref_name"]["include"].append("refs/heads/release/*")
    cases.append((wrong_ref, "ruleset_ref_scope_mismatch"))
    weak_pr = _ruleset()
    weak_pr["rules"][0]["parameters"]["require_last_push_approval"] = False
    cases.append((weak_pr, "ruleset_review_controls_weak"))
    no_checks = _ruleset()
    no_checks["rules"][1]["parameters"]["required_status_checks"] = []
    cases.append((no_checks, "ruleset_named_checks_missing"))
    no_deletion = _ruleset()
    no_deletion["rules"].remove({"type": "deletion"})
    cases.append((no_deletion, "ruleset_required_controls_missing"))

    for ruleset, reason in cases:
        namespace["github_get"] = lambda _url, _token, item=ruleset: (item, None)
        with pytest.raises(namespace["GateError"], match=reason):
            namespace["validate_ruleset"](
                [{"id": 123, "name": "asaas-producer-release-main"}], "masked"
            )


def _environment(name: str = "asaas-build") -> dict:
    return {
        "name": name,
        "can_admins_bypass": False,
        "protection_rules": [
            {
                "type": "required_reviewers",
                "reviewers": [{"type": "User", "id": 42, "name": "felps-json"}],
                "prevent_self_review": True,
            }
        ],
        "deployment_branch_policy": {
            "protected_branches": False,
            "custom_branch_policies": True,
        },
    }


def test_environment_gate_accepts_documented_branch_policy_response_shape() -> None:
    namespace = _gate_namespace()
    environment = _environment()
    namespace["github_get"] = lambda _url, _token: (environment, None)
    namespace["github_collection"] = lambda *_args, **_kwargs: [
        {"id": 12345, "node_id": "BEP_kwDOABC123", "name": "main"}
    ]
    namespace["validate_environment"]("asaas-build", "masked")


def test_environment_gate_rejects_missing_bypass_readback_and_non_branch_policy() -> (
    None
):
    namespace = _gate_namespace()
    environment = _environment()
    for mutation, reason in (
        (
            lambda value: value.pop("can_admins_bypass"),
            "environment_admin_bypass_enabled_or_unreadable",
        ),
        (
            lambda value: value.update(can_admins_bypass=True),
            "environment_admin_bypass_enabled_or_unreadable",
        ),
        (
            lambda value: value.update(
                deployment_branch_policy={
                    "protected_branches": True,
                    "custom_branch_policies": False,
                }
            ),
            "environment_branch_policy_unrestricted_or_mismatched",
        ),
    ):
        changed = copy.deepcopy(environment)
        mutation(changed)
        namespace["github_get"] = lambda _url, _token, item=changed: (item, None)
        with pytest.raises(namespace["GateError"], match=reason):
            namespace["validate_environment"]("asaas-build", "masked")

    namespace["github_get"] = lambda _url, _token: (_environment(), None)
    for bad_rows in (
        [{"id": 1, "node_id": "BEP_main", "name": "release"}],
        [{"id": 1, "node_id": "BEP_main", "name": "main", "type": "branch"}],
        [
            {"id": 1, "node_id": "BEP_main", "name": "main"},
            {"id": 2, "node_id": "BEP_extra", "name": "release"},
        ],
        [{"id": 1, "node_id": "BEP_wildcard", "name": "release/*"}],
        [{"id": 1, "node_id": "BEP_tag", "name": "main", "type": "tag"}],
        [{"name": "main", "type": "tag"}],
        [{"name": "main", "type": "branch"}, {"name": "release/*", "type": "branch"}],
    ):
        namespace["github_collection"] = lambda *_args, rows=bad_rows, **_kwargs: rows
        with pytest.raises(namespace["GateError"]):
            namespace["validate_environment"]("asaas-build", "masked")


def test_run_bound_ui_evidence_is_blocked_without_a_trusted_channel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    namespace = _gate_namespace()
    context = {
        "run_id": "7123456789",
        "workflow_path": ".github/workflows/asaas-build-attestation.yml",
        "sha": "a" * 40,
        "ref": "refs/heads/main",
    }
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    with pytest.raises(
        namespace["GateError"], match="run_bound_admin_ui_evidence_unavailable"
    ):
        namespace["require_run_bound_admin_ui_evidence"](context, "asaas-build")
    text = summary.read_text(encoding="utf-8")
    assert "BLOCKED: run-bound administrator UI evidence unavailable" in text
    assert "No OIDC-enabled job will start" in text
    assert "Run ID: `7123456789`" in text
    assert "Workflow: `.github/workflows/asaas-build-attestation.yml`" in text
    assert f"SHA: `{'a' * 40}`" in text
    assert "Ref: `refs/heads/main`" in text
    assert "Protected environment requiring approval: `asaas-build`" in text

    for invalid, environment in (
        ({**context, "run_id": ""}, "asaas-build"),
        ({**context, "workflow_path": "other.yml"}, "asaas-build"),
        ({**context, "sha": "short"}, "asaas-build"),
        ({**context, "ref": "refs/heads/release"}, "asaas-build"),
        (context, "asaas-production-deployment"),
        (None, "asaas-build"),
    ):
        with pytest.raises(
            namespace["GateError"], match="environment_approval_context_missing"
        ):
            namespace["require_run_bound_admin_ui_evidence"](invalid, environment)

    monkeypatch.delenv("GITHUB_STEP_SUMMARY")
    with pytest.raises(
        namespace["GateError"], match="environment_approval_context_missing"
    ):
        namespace["require_run_bound_admin_ui_evidence"](context, "asaas-build")


def test_preflight_does_not_emit_validated_output_without_admin_ui_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    namespace = _gate_namespace()
    now = datetime.now(timezone.utc)
    run_id = "7123456789"
    sha = "a" * 40
    repository = {
        "full_name": "neurostackinfra/neurostack-api",
        "id": 1152335815,
        "owner": {"id": 340698182},
        "default_branch": "main",
    }
    run = {
        "id": int(run_id),
        "event": "workflow_dispatch",
        "status": "in_progress",
        "head_sha": sha,
        "head_branch": "main",
        "path": ".github/workflows/asaas-build-attestation.yml@refs/heads/main",
        "repository": {"full_name": "neurostackinfra/neurostack-api", "id": 1152335815},
        "head_repository": {"id": 1152335815},
        "created_at": now.isoformat().replace("+00:00", "Z"),
    }

    def github_get(url: str, _token: str) -> tuple[dict, None]:
        if url.endswith("/rulesets/123"):
            return _ruleset(), None
        if "/environments/" in url:
            name = url.rsplit("/", 1)[-1]
            return _environment(name), None
        if url.endswith(f"/actions/runs/{run_id}"):
            return run, None
        return repository, None

    def github_collection(url: str, _token: str, *, list_field=None) -> list[dict]:
        if "/rulesets?" in url:
            return [{"id": 123, "name": "asaas-producer-release-main"}]
        if "deployment-branch-policies" in url:
            return [{"id": 12345, "node_id": "BEP_kwDOABC123", "name": "main"}]
        assert list_field == "jobs"
        return [{"name": "preflight", "head_sha": sha, "status": "in_progress"}]

    namespace["github_get"] = github_get
    namespace["github_collection"] = github_collection
    for key, value in _valid_scope().items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GH_TOKEN", "masked")
    monkeypatch.setenv("GITHUB_RUN_ID", run_id)
    monkeypatch.setenv("GITHUB_SHA", sha)
    output = tmp_path / "github-output"
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    with pytest.raises(
        namespace["GateError"], match="run_bound_admin_ui_evidence_unavailable"
    ):
        namespace["main"]()

    assert not output.exists()
    assert (
        "BLOCKED: run-bound administrator UI evidence unavailable"
        in summary.read_text(encoding="utf-8")
    )


def test_actions_run_gate_binds_current_run_workflow_ref_and_sha() -> None:
    namespace = _gate_namespace()
    now = datetime.now(timezone.utc)
    run = {
        "id": 7123456789,
        "event": "workflow_dispatch",
        "status": "in_progress",
        "head_sha": "a" * 40,
        "head_branch": "main",
        "path": (".github/workflows/asaas-build-attestation.yml@refs/heads/main"),
        "repository": {
            "full_name": "neurostackinfra/neurostack-api",
            "id": 1152335815,
        },
        "head_repository": {"id": 1152335815},
        "created_at": now.isoformat().replace("+00:00", "Z"),
    }
    env = {"GITHUB_RUN_ID": "7123456789", "GITHUB_SHA": "a" * 40}
    urls: list[str] = []
    namespace["github_get"] = lambda url, _token: urls.append(url) or (run, None)
    namespace["github_collection"] = lambda *_args, **_kwargs: [
        {"name": "preflight", "head_sha": "a" * 40, "status": "in_progress"}
    ]

    namespace["validate_current_run"]("masked", env)
    assert urls == [
        "https://api.github.com/repos/neurostackinfra/neurostack-api/"
        "actions/runs/7123456789"
    ]
    assert namespace["validate_current_run"]("masked", env) == {
        "run_id": "7123456789",
        "workflow_path": ".github/workflows/asaas-build-attestation.yml",
        "sha": "a" * 40,
        "ref": "refs/heads/main",
    }

    mutations = (
        lambda value: value.update(id=7123456790),
        lambda value: value.update(head_sha="b" * 40),
        lambda value: value.update(path=".github/workflows/other.yml@refs/heads/main"),
        lambda value: value.update(head_branch="release"),
        lambda value: value.update(created_at=(now - timedelta(hours=2)).isoformat()),
    )
    for mutation in mutations:
        invalid = copy.deepcopy(run)
        mutation(invalid)
        namespace["github_get"] = lambda _url, _token, item=invalid: (item, None)
        with pytest.raises(namespace["GateError"]):
            namespace["validate_current_run"]("masked", env)


def test_attestation_workflow_gates_all_aws_and_push_steps_after_live_preflight():
    source, workflow = _workflow()
    jobs = workflow["jobs"]
    preflight = jobs["preflight"]
    build = jobs["build-attestation"]
    preflight_step = next(
        step for step in preflight["steps"] if step.get("id") == "gate"
    )
    gate_run = preflight_step["run"]

    assert workflow["permissions"] == {}
    assert preflight["permissions"] == {"actions": "read", "administration": "read"}
    assert "id-token" not in preflight["permissions"]
    assert "aws-actions/configure-aws-credentials" not in gate_run
    assert "docker buildx build --push" not in gate_run
    assert gate_run.index("validate_scope(env)") < gate_run.index(
        "repository, _ = github_get("
    )
    assert "actions/runs/{run_id}" in gate_run
    assert "head_sha" in gate_run and "head_branch" in gate_run
    assert 'environment.get("can_admins_bypass") is False' in gate_run
    assert "deployment-branch-policies" in gate_run
    assert gate_run.index("validate_current_run(token, env)") < gate_run.index(
        'require_run_bound_admin_ui_evidence(run_context, "asaas-build")'
    )
    assert "GITHUB_STEP_SUMMARY" in gate_run
    assert 'raise GateError("run_bound_admin_ui_evidence_unavailable")' in gate_run
    assert gate_run.index(
        'require_run_bound_admin_ui_evidence(run_context, "asaas-build")'
    ) < gate_run.index('output.write("rollout_scope=validated')
    assert "ASAAS_PRODUCER_OPERATIONAL_AUTHORIZATION_REFERENCE" not in source
    assert _gate_namespace()["OPERATIONAL_AUTHORIZATION_REFERENCE"] == _REFERENCE

    assert build["needs"] == "preflight"
    assert "needs.preflight.outputs.rollout_scope == 'validated'" in build["if"]
    assert build["environment"] == "asaas-build"
    assert build["permissions"] == {"contents": "read", "id-token": "write"}
    assert "id-token: write" not in source.split("jobs:", 1)[0]
    assert source.count("id-token: write") == 1
    action_steps = [
        step for job in jobs.values() for step in job["steps"] if "uses" in step
    ]
    assert action_steps
    assert {step["uses"] for step in action_steps} == _EXPECTED_ACTION_PINS

    steps = build["steps"]
    setup_python = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Set up pinned CPython 3.12"
    )
    install = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Install signing dependencies"
    )
    pins = next(
        index
        for index, step in enumerate(steps)
        if step.get("name") == "Validate signer pins before requesting OIDC credentials"
    )
    credentials = next(
        index
        for index, step in enumerate(steps)
        if "aws-actions/configure-aws-credentials@" in step.get("uses", "")
    )
    push = next(
        index
        for index, step in enumerate(steps)
        if "docker buildx build --push" in step.get("run", "")
    )
    assert setup_python < install < pins < credentials < push
    assert (
        steps[setup_python]["uses"]
        == "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065"
    )
    assert steps[setup_python]["with"]["python-version"] == "3.12"
    assert "asaas-rollout-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}" in steps[push]["run"]
    assert "group7b-" not in source
    assert "aws ecs" not in source.lower()
    assert "refund payment" not in source.lower()


def test_attestation_workflow_installs_signer_from_hash_locked_requirements():
    workflow, document = _workflow()
    build = document["jobs"]["build-attestation"]
    install = next(
        step
        for step in build["steps"]
        if step.get("name") == "Install signing dependencies"
    )
    assert "--require-hashes" in install["run"]
    assert "-r scripts/requirements-asaas-build-attestation.txt" in install["run"]
    lock = _WORKFLOW.parents[2] / "scripts" / "requirements-asaas-build-attestation.txt"
    lock_text = lock.read_text(encoding="utf-8")
    assert lock_text.count("--hash=sha256:") == 10
    requirement_lines = [
        line
        for line in lock_text.splitlines()
        if line and not line.startswith(("#", " "))
    ]
    assert all("==" in line for line in requirement_lines)
    assert ">=" not in lock_text


def test_attestation_workflow_is_not_ignored_by_git() -> None:
    result = subprocess.run(
        ["git", "check-ignore", "--quiet", str(_WORKFLOW)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1, result.stderr
