from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from scripts import asaas_build_release_preflight as preflight
from scripts import asaas_producer_deploy_executor as executor
from scripts.verify_asaas_producer_image_rollout import envelope_digest
from tests.scripts.test_verify_asaas_producer_image_rollout import (
    DIGEST,
    P256Signer,
    _set_group2_key_pins,
    _signed_envelope,
    _task_definition_template,
    _trust_env,
)

TASK_ARN = "arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/task-1"
TASK_DEFINITION_ARN = (
    "arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:8"
)
CREATED_AT = "2026-10-14T22:54:10.831Z"
STARTED_AT = "2026-10-14T22:54:54.110Z"
SECRET_ARN = "arn:aws:secretsmanager:us-east-1:388460142431:secret:neurostack-db-a1b2c3"
VERSION_ID = "12345678-1234-5678-1234-567812345678"
DEPLOY_ROLE_ARN = executor.PINNED_DEPLOY_ROLE_ARN


def _operator_snapshot() -> dict[str, object]:
    return {
        "service": {"desiredCount": 1},
        "tasks": [
            {
                "taskArn": TASK_ARN,
                "taskDefinitionArn": TASK_DEFINITION_ARN,
                "lastStatus": "RUNNING",
                "desiredStatus": "RUNNING",
                "createdAt": CREATED_AT,
                "startedAt": STARTED_AT,
            }
        ],
        "task_definition": {
            "taskDefinitionArn": TASK_DEFINITION_ARN,
            "containerDefinitions": [
                {
                    "name": "app",
                    "secrets": [{"name": "DATABASE_URL", "valueFrom": SECRET_ARN}],
                }
            ],
        },
    }


def _secret_metadata() -> tuple[dict[str, object], dict[str, object]]:
    return (
        {
            "ARN": SECRET_ARN,
            "LastChangedDate": "2026-10-14T22:00:00.000Z",
        },
        {
            "ARN": SECRET_ARN,
            "Versions": [{"VersionId": VERSION_ID, "VersionStages": ["AWSCURRENT"]}],
        },
    )


def test_operator_snapshot_is_source_for_task_cohort_and_secret_metadata() -> None:
    secret, versions = _secret_metadata()
    result = executor._resolve_operator_snapshot_secret_version(
        _operator_snapshot(),
        secret,
        versions,
        expected_secret_arn=SECRET_ARN,
        expected_version_id=VERSION_ID,
    )
    assert result["active_task_count"] == 1
    assert result["active_task_definition_arn"] == TASK_DEFINITION_ARN
    assert result["replacement_value_from"] == f"{SECRET_ARN}:::{VERSION_ID}"


def test_operator_snapshot_fails_closed_on_missing_or_imprecise_task_timestamps() -> (
    None
):
    secret, versions = _secret_metadata()
    source = _operator_snapshot()
    del source["tasks"][0]["startedAt"]
    with pytest.raises(executor.DeployError, match="database_secret_task_started_at"):
        executor._resolve_operator_snapshot_secret_version(
            source,
            secret,
            versions,
            expected_secret_arn=SECRET_ARN,
            expected_version_id=VERSION_ID,
        )


def test_caller_identity_must_match_exact_group2_role_and_run_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {
        "Account": "388460142431",
        "Arn": "arn:aws:sts::388460142431:assumed-role/"
        "neurostack-asaas-producer-release-DeployRole-CU6AueCQCi9i/asaas-producer-12345",
    }
    monkeypatch.setattr(executor, "_aws_json", lambda arguments: expected)
    executor._assert_caller_account(DEPLOY_ROLE_ARN, "12345")

    for identity in (
        {
            **expected,
            "Arn": "arn:aws:sts::388460142431:assumed-role/"
            "neurostack-asaas-producer-release-DeployRole-CU6AueCQCi9i/other-session",
        },
        {
            **expected,
            "Arn": "arn:aws:sts::388460142431:assumed-role/"
            "other-role/asaas-producer-12345",
        },
    ):
        monkeypatch.setattr(
            executor,
            "_aws_json",
            lambda arguments, identity=identity: identity,
        )
        with pytest.raises(executor.DeployError, match="aws_caller_role_mismatch"):
            executor._assert_caller_account(DEPLOY_ROLE_ARN, "12345")

    monkeypatch.setattr(
        executor,
        "_aws_json",
        lambda arguments: {
            "Account": "111122223333",
            "Arn": expected["Arn"],
        },
    )
    with pytest.raises(executor.DeployError, match="aws_caller_account_mismatch"):
        executor._assert_caller_account(DEPLOY_ROLE_ARN, "12345")


def test_live_service_snapshot_must_match_primary_deployment_id() -> None:
    expected_snapshot = {
        "task_count": 2,
        "task_definition_arn": TASK_DEFINITION_ARN,
        "primary_deployment_id": "ecs-svc/1234567890123456789",
        "service": {
            "loadBalancers": [
                {"targetGroupArn": "tg", "containerName": "app", "containerPort": 8000}
            ]
        },
    }
    live = {
        "status": "ACTIVE",
        "taskDefinition": TASK_DEFINITION_ARN,
        "desiredCount": 2,
        "runningCount": 2,
        "loadBalancers": expected_snapshot["service"]["loadBalancers"],
        "deployments": [
            {
                "id": "ecs-svc/1234567890123456789",
                "status": "PRIMARY",
                "taskDefinition": TASK_DEFINITION_ARN,
                "rolloutState": "COMPLETED",
            }
        ],
    }
    executor._validate_live_service_snapshot(live, expected_snapshot)
    live["deployments"][0]["id"] = "ecs-svc/changed"
    with pytest.raises(executor.DeployError, match="live_service_rollout_not_complete"):
        executor._validate_live_service_snapshot(live, expected_snapshot)


def test_caller_identity_rejects_a_role_outside_group2_stack_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = False

    def unexpected_call(arguments: list[str]) -> dict[str, str]:
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(executor, "_aws_json", unexpected_call)
    with pytest.raises(executor.DeployError, match="expected_deploy_role_arn_invalid"):
        executor._assert_caller_account(
            "arn:aws:iam::388460142431:role/arbitrary-asaas-role", "12345"
        )
    assert not called


def test_executor_does_not_request_task_definition_or_task_readback_apis() -> None:
    source = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "asaas_producer_deploy_executor.py"
    ).read_text(encoding="utf-8")
    assert '"describe-tasks"' not in source
    assert '"describe-task-definition"' not in source


def test_rollback_waits_for_captured_baseline_desired_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        executor, "_update_service", lambda arn: calls.append(("update", arn))
    )
    monkeypatch.setattr(
        executor,
        "_wait_for_service",
        lambda arn, count: calls.append(("wait", arn, count)),
    )

    executor._rollback(TASK_DEFINITION_ARN, desired_count=2)

    assert calls == [
        ("update", TASK_DEFINITION_ARN),
        ("wait", TASK_DEFINITION_ARN, 2),
    ]


def test_rollback_rejects_missing_or_changed_baseline_count() -> None:
    with pytest.raises(
        executor.DeployError, match="rollback_baseline_desired_count_invalid"
    ):
        executor._rollback(TASK_DEFINITION_ARN, desired_count=0)


def test_runtime_build_trust_reverifies_the_identical_envelope_and_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = {"signed": "same-object"}
    verified = {
        "attestation_id": "89bf1556-9f6d-4f39-b5c8-8dcb041877da",
        "app_image_digest": "sha256:" + "a" * 64,
    }
    env = {"INPUT_APP_IMAGE_DIGEST": verified["app_image_digest"]}
    calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        executor,
        "_fetch_runtime_build_trust",
        lambda _env, *, opener=None: (
            {"runtime": "trust"},
            "b" * 64,
            "2026-10-17T12:00:00Z",
        ),
    )
    monkeypatch.setattr(
        executor,
        "validate_artifact_before_aws",
        lambda actual_env, actual_envelope, trust: calls.append(
            (actual_env, actual_envelope, trust)
        )
        or verified,
    )

    digest, read_at = executor._verify_runtime_build_trust(env, envelope, verified)

    assert calls == [(env, envelope, {"runtime": "trust"})]
    assert digest == "b" * 64
    assert read_at == "2026-10-17T12:00:00Z"


def test_runtime_build_trust_rejects_a_different_signature_or_image_digest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified = {
        "attestation_id": "89bf1556-9f6d-4f39-b5c8-8dcb041877da",
        "app_image_digest": "sha256:" + "a" * 64,
    }
    monkeypatch.setattr(
        executor,
        "_fetch_runtime_build_trust",
        lambda _env, *, opener=None: (
            {"runtime": "trust"},
            "b" * 64,
            "2026-10-17T12:00:00Z",
        ),
    )
    monkeypatch.setattr(
        executor,
        "validate_artifact_before_aws",
        lambda *_args: {**verified, "app_image_digest": "sha256:" + "c" * 64},
    )

    with pytest.raises(
        executor.DeployError, match="runtime_build_trust_attestation_mismatch"
    ):
        executor._verify_runtime_build_trust(
            {"INPUT_APP_IMAGE_DIGEST": verified["app_image_digest"]},
            {"signed": "same-object"},
            verified,
        )


@pytest.mark.parametrize("failure_stage", ["readback", "verification"])
def test_execute_rollout_rolls_back_after_runtime_trust_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure_stage: str
) -> None:
    envelope_path = tmp_path / "envelope.json"
    envelope_path.write_text('{"signed":"same-envelope"}', encoding="utf-8")
    (tmp_path / "asaas-producer-post-readback-private.pem").touch()
    digest = "sha256:" + "a" * 64
    env = {
        "GH_TOKEN": "fixture-token",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_ID": "12345",
        "GITHUB_WORKFLOW_REF": (
            f"{executor.REPOSITORY}/{executor.DEPLOY_WORKFLOW_PATH}@refs/heads/main"
        ),
        "ASAAS_SIGNED_ENVELOPE_PATH": str(envelope_path),
        "ASAAS_PRODUCER_BUILD_TRUST_READBACK_JSON": '{"initial":true}',
        "INPUT_APP_IMAGE_DIGEST": digest,
        "BILLING_PRODUCER_ECR_REPOSITORY_URI": "fixture-repository",
        "RUNNER_TEMP": str(tmp_path),
        "ASAAS_PREFLIGHT_PROTECTION_SHA256": "b" * 64,
        "INPUT_MODE": "bootstrap",
    }
    snapshot = {
        "task_definition_arn": TASK_DEFINITION_ARN,
        "task_count": 1,
        "database_secret_arn": SECRET_ARN,
        "database_secret_version_id": VERSION_ID,
    }
    cohort = {
        "snapshot": snapshot,
        "operator_login": "operator",
        "operator_permission": "admin",
        "base_environment_sha256": "c" * 64,
        "target_group": {},
        "readiness_probe": {},
        "snapshot_sha256": "d" * 64,
    }
    before = {
        "deploy_role_arn": DEPLOY_ROLE_ARN,
        "cohort": {
            "validated_cohort": cohort,
            "operator_readback": {"snapshot": _operator_snapshot()},
        },
        "template": {"task_definition": _operator_snapshot()["task_definition"]},
    }
    events = []
    new_task_definition = TASK_DEFINITION_ARN.rsplit(":", 1)[0] + ":9"
    monkeypatch.setattr(
        executor, "validate_current_run", lambda *_a, **_k: {"run_id": "12345"}
    )
    monkeypatch.setattr(executor, "_private_template_and_before", lambda *_a: before)
    monkeypatch.setattr(executor, "_assert_caller_account", lambda *_a: None)
    monkeypatch.setattr(executor, "_live_service", lambda: {})
    monkeypatch.setattr(executor, "_validate_live_service_snapshot", lambda *_a: None)
    monkeypatch.setattr(executor, "_secret_metadata", lambda *_a: _secret_metadata())
    monkeypatch.setattr(executor, "_ecr_metadata", lambda *_a: None)
    monkeypatch.setattr(
        executor, "build_replacement_task_definition", lambda *_a, **_k: {}
    )
    monkeypatch.setattr(
        executor,
        "_register_task_definition",
        lambda *_a: (new_task_definition, {"revision": 9}),
    )
    monkeypatch.setattr(
        executor, "_update_service", lambda arn: events.append(("update", arn))
    )
    monkeypatch.setattr(
        executor,
        "_wait_for_service",
        lambda arn, count: events.append(("healthy", arn, count)),
    )
    monkeypatch.setattr(executor, "_deployment_job_started_at", lambda *_a: STARTED_AT)
    monkeypatch.setattr(
        executor, "_wait_for_encrypted_post_readback", lambda **_k: ({}, {})
    )
    monkeypatch.setattr(
        executor,
        "_rollback",
        lambda arn, count: events.append(("rollback", arn, count)),
    )

    def validate_artifact(actual_env, envelope, trust):
        assert envelope == {"signed": "same-envelope"}
        if trust == {"runtime": True}:
            events.append(("verification_failure",))
            raise executor.GateError("runtime_trust_verification_failed")
        return {"app_image_digest": digest}

    def fetch_runtime_trust(_env, *, opener=None):
        if failure_stage == "readback":
            events.append(("readback_failure",))
            raise executor.DeployError("runtime_trust_readback_failed")
        return {"runtime": True}, "e" * 64, STARTED_AT

    monkeypatch.setattr(executor, "validate_artifact_before_aws", validate_artifact)
    monkeypatch.setattr(executor, "_fetch_runtime_build_trust", fetch_runtime_trust)

    with pytest.raises(
        executor.DeployError,
        match=f"^rollout_failed_rolled_back:runtime_trust_{failure_stage}_failed$",
    ):
        executor.execute_rollout(env)
    assert events == [
        ("update", new_task_definition),
        ("healthy", new_task_definition, snapshot["task_count"]),
        (f"{failure_stage}_failure",),
        ("rollback", snapshot["task_definition_arn"], snapshot["task_count"]),
    ]


_ROLLOUT_DIGEST = "sha256:" + "a" * 64
_PREVIOUS_DEPLOYMENT_ID = "ecs-svc/1111111111111111111"
_NEW_TASK_DEFINITION_ARN = TASK_DEFINITION_ARN.rsplit(":", 1)[0] + ":9"
_LOAD_BALANCERS = [
    {
        "targetGroupArn": (
            "arn:aws:elasticloadbalancing:us-east-1:388460142431:"
            "targetgroup/neurostack-staging/0123456789abcdef"
        ),
        "containerName": "app",
        "containerPort": 8000,
    }
]


class _FakeAws:
    """Stateful stand-in for the AWS CLI boundary used by the executor."""

    def __init__(self, *, fail_update_to: str | None = None) -> None:
        self.task_definition = TASK_DEFINITION_ARN
        self.deployment_id = _PREVIOUS_DEPLOYMENT_ID
        self.fail_update_to = fail_update_to
        self.mutations: list[tuple[str, str]] = []

    def __call__(self, arguments: list[str], *, timeout: int = 45) -> object:
        command = tuple(arguments[:2])
        if command == ("sts", "get-caller-identity"):
            role_name = executor.PINNED_DEPLOY_ROLE_ARN.split("/", 1)[1]
            return {
                "Account": executor.AWS_ACCOUNT_ID,
                "Arn": (
                    "arn:aws:sts::388460142431:assumed-role/"
                    f"{role_name}/asaas-producer-12345"
                ),
            }
        if command == ("ecs", "describe-services"):
            return {
                "failures": [],
                "services": [
                    {
                        "serviceArn": executor.SERVICE_ARN,
                        "serviceName": executor.SERVICE_NAME,
                        "status": "ACTIVE",
                        "desiredCount": 1,
                        "runningCount": 1,
                        "taskDefinition": self.task_definition,
                        "deployments": [
                            {
                                "id": self.deployment_id,
                                "status": "PRIMARY",
                                "taskDefinition": self.task_definition,
                                "rolloutState": "COMPLETED",
                            }
                        ],
                        "loadBalancers": _LOAD_BALANCERS,
                    }
                ],
            }
        if command == ("secretsmanager", "describe-secret"):
            return _secret_metadata()[0]
        if command == ("secretsmanager", "list-secret-version-ids"):
            return _secret_metadata()[1]
        if command == ("ecr", "describe-images"):
            return {"imageDetails": [{"imageDigest": _ROLLOUT_DIGEST}]}
        if command == ("ecs", "register-task-definition"):
            source = arguments[arguments.index("--cli-input-json") + 1]
            request = json.loads(Path(source.removeprefix("file://")).read_text())
            self.mutations.append(("register", _NEW_TASK_DEFINITION_ARN))
            return {
                **request,
                "taskDefinitionArn": _NEW_TASK_DEFINITION_ARN,
                "revision": 9,
                "status": "ACTIVE",
            }
        if command == ("ecs", "update-service"):
            target = arguments[arguments.index("--task-definition") + 1]
            self.mutations.append(("update-service", target))
            if target == self.fail_update_to:
                raise executor.DeployError("aws_operation_failed")
            self.task_definition = target
            self.deployment_id = f"ecs-svc/{len(self.mutations)}"
            return {"taskDefinition": target}
        raise AssertionError(f"unexpected AWS call: {command}")


class _FakeTrustResponse:
    def __init__(self, url: str, body: bytes) -> None:
        self.status = 200
        self.headers = {"Content-Type": "application/json"}
        self._url = url
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self) -> str:
        return self._url

    def read(self, _limit: int) -> bytes:
        return self._body


def _runtime_trust_readback(trust: dict[str, str], **changes: str) -> bytes:
    served = {
        **trust,
        "read_at_utc": datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
    }
    served.update(changes)
    return json.dumps(served, separators=(",", ":")).encode("utf-8")


def _rollout_fixture(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    runtime_changes: dict[str, str],
    fail_rollback: bool = False,
) -> tuple[dict[str, str], _FakeAws, dict[str, list[object]]]:
    assert DIGEST == _ROLLOUT_DIGEST
    signer = P256Signer()
    _set_group2_key_pins(monkeypatch, signer)
    trust_env, trust, entry = _trust_env(signer)
    envelope = _signed_envelope(signer, entry)
    envelope_path = tmp_path / "asaas-build-attestation.json"
    envelope_path.write_text(json.dumps(envelope), encoding="utf-8")
    (tmp_path / "asaas-producer-post-readback-private.pem").touch()
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    env = {
        **trust_env,
        "GH_TOKEN": "fixture-token",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_RUN_ID": "12345",
        "GITHUB_WORKFLOW_REF": (
            f"{executor.REPOSITORY}/{executor.DEPLOY_WORKFLOW_PATH}@refs/heads/main"
        ),
        "INPUT_MODE": "bootstrap",
        "INPUT_BUILD_RUN_ID": "8123456789",
        "INPUT_ATTESTATION_ID": envelope["payload"]["attestation_id"],
        "INPUT_APP_IMAGE_DIGEST": DIGEST,
        "INPUT_ENVELOPE_SHA256": envelope_digest(envelope),
        "ASAAS_SIGNED_ENVELOPE_PATH": str(envelope_path),
        "ASAAS_PRODUCER_BUILD_TRUST_READBACK_JSON": json.dumps(trust),
        "ASAAS_PREFLIGHT_PROTECTION_SHA256": "b" * 64,
        "RUNNER_TEMP": str(tmp_path),
    }
    snapshot = {
        "task_definition_arn": TASK_DEFINITION_ARN,
        "task_count": 1,
        "database_secret_arn": SECRET_ARN,
        "database_secret_version_id": VERSION_ID,
        "service": {"loadBalancers": _LOAD_BALANCERS},
        "primary_deployment_id": _PREVIOUS_DEPLOYMENT_ID,
    }
    before = {
        "deploy_role_arn": DEPLOY_ROLE_ARN,
        "template": _task_definition_template(signer),
        "cohort": {
            "validated_cohort": {
                "snapshot": snapshot,
                "operator_login": "operator",
                "operator_permission": "admin",
                "base_environment_sha256": "c" * 64,
                "target_group": {},
                "readiness_probe": {},
                "snapshot_sha256": "d" * 64,
            },
            "operator_readback": {"snapshot": _operator_snapshot()},
        },
    }
    aws = _FakeAws(fail_update_to=TASK_DEFINITION_ARN if fail_rollback else None)
    calls: dict[str, list[object]] = {"trust_requests": [], "registration": []}

    def open_runtime_trust(request, timeout):
        assert request.get_method() == "GET"
        assert request.get_header("Authorization") is None
        calls["trust_requests"].append(request.full_url)
        return _FakeTrustResponse(
            request.full_url, _runtime_trust_readback(trust, **runtime_changes)
        )

    class _Opener:
        open = staticmethod(open_runtime_trust)

    # GitHub-facing gates are exercised by their own suites; these fakes return
    # the already-validated pre-mutation context and post-deploy readback.
    monkeypatch.setattr(
        executor, "validate_current_run", lambda *_a, **_k: {"run_id": "12345"}
    )
    monkeypatch.setattr(executor, "_private_template_and_before", lambda *_a: before)
    monkeypatch.setattr(
        executor,
        "_deployment_job_started_at",
        lambda *_a: datetime.now(timezone.utc),
    )
    monkeypatch.setattr(
        executor,
        "_wait_for_encrypted_post_readback",
        lambda **_k: (
            {"checked_at_utc": datetime.now(timezone.utc).isoformat()},
            {"registration": "canonical_response_supplied_by_operator"},
        ),
    )

    def wait_for_registration(**kwargs):
        calls["registration"].append(kwargs["verified_attestation"]["attestation_id"])
        return {
            "registration_attestation_id": kwargs["verified_attestation"][
                "attestation_id"
            ]
        }

    monkeypatch.setattr(
        executor, "_wait_for_canonical_registration", wait_for_registration
    )
    monkeypatch.setattr(executor, "_aws_json", aws)
    monkeypatch.setattr(preflight, "build_opener", lambda *_handlers: _Opener())
    return env, aws, calls


_RUNTIME_TRUST_URL = (
    "https://staging.api.neurostack.global/v1/billing/producer-build-trust"
)


def test_execute_rollout_accepts_matching_runtime_trust_without_rollback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env, aws, calls = _rollout_fixture(monkeypatch, tmp_path, runtime_changes={})

    result = executor.execute_rollout(env)

    assert result["rollback"] == "not_required"
    assert result["app_image_digest"] == env["INPUT_APP_IMAGE_DIGEST"]
    assert len(result["runtime_build_trust_sha256"]) == 64
    assert calls["trust_requests"] == [_RUNTIME_TRUST_URL]
    assert calls["registration"] == [env["INPUT_ATTESTATION_ID"]]
    assert aws.mutations == [
        ("register", _NEW_TASK_DEFINITION_ARN),
        ("update-service", _NEW_TASK_DEFINITION_ARN),
    ]


@pytest.mark.parametrize(
    "runtime_changes",
    [
        # The deployed runtime serves a different verification key than the one
        # that signed the envelope.
        {"public_key_fingerprint_sha256": "f" * 64},
        # The deployed runtime names a different signing key ID.
        {"kid": "arn:aws:kms:us-east-1:388460142431:key/" + "0" * 8 + "-other"},
    ],
    ids=["fingerprint", "kid"],
)
def test_execute_rollout_rolls_back_when_runtime_trust_mismatches_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, runtime_changes: dict[str, str]
) -> None:
    env, aws, calls = _rollout_fixture(
        monkeypatch, tmp_path, runtime_changes=runtime_changes
    )

    with pytest.raises(
        executor.DeployError,
        match="^rollout_failed_rolled_back:backend_trust_pin_mismatch$",
    ):
        executor.execute_rollout(env)

    assert calls["trust_requests"] == [_RUNTIME_TRUST_URL]
    # The real rollback path restored the recorded prior task definition, and
    # no human registration was awaited or accepted for the mismatched runtime.
    assert aws.mutations == [
        ("register", _NEW_TASK_DEFINITION_ARN),
        ("update-service", _NEW_TASK_DEFINITION_ARN),
        ("update-service", TASK_DEFINITION_ARN),
    ]
    assert aws.task_definition == TASK_DEFINITION_ARN
    assert calls["registration"] == []


def test_execute_rollout_preserves_preflight_gate_reason_after_mutation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    real_job_started_at = executor._deployment_job_started_at
    env, aws, calls = _rollout_fixture(monkeypatch, tmp_path, runtime_changes={})

    def job_history_unreadable(*_args: object, **_kwargs: object) -> None:
        # The deployment-job readback uses the preflight GitHub collector, whose
        # GateError class is distinct from the rollout gate's.
        raise executor.github_preflight.GateError("github_pagination_incomplete")

    # Drive the real post-mutation job-start readback into the preflight error.
    monkeypatch.setattr(executor, "_deployment_job_started_at", real_job_started_at)
    monkeypatch.setattr(
        executor.github_preflight, "github_collection", job_history_unreadable
    )

    with pytest.raises(
        executor.DeployError,
        match="^rollout_failed_rolled_back:github_pagination_incomplete$",
    ):
        executor.execute_rollout(env)

    assert aws.mutations == [
        ("register", _NEW_TASK_DEFINITION_ARN),
        ("update-service", _NEW_TASK_DEFINITION_ARN),
        ("update-service", TASK_DEFINITION_ARN),
    ]
    assert aws.task_definition == TASK_DEFINITION_ARN
    assert calls["trust_requests"] == []
    assert calls["registration"] == []


def test_execute_rollout_reads_runtime_trust_after_healthy_readback_and_before_registration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env, aws, calls = _rollout_fixture(monkeypatch, tmp_path, runtime_changes={})
    order: list[str] = []
    wrapped_post_readback = executor._wait_for_encrypted_post_readback
    wrapped_registration = executor._wait_for_canonical_registration
    wrapped_trust = executor._verify_runtime_build_trust

    def post_readback(**kwargs: object) -> object:
        order.append("healthy_readback")
        return wrapped_post_readback(**kwargs)

    def runtime_trust(*args: object, **kwargs: object) -> object:
        order.append("runtime_trust")
        return wrapped_trust(*args, **kwargs)

    def registration(**kwargs: object) -> object:
        order.append("registration")
        return wrapped_registration(**kwargs)

    monkeypatch.setattr(executor, "_wait_for_encrypted_post_readback", post_readback)
    monkeypatch.setattr(executor, "_verify_runtime_build_trust", runtime_trust)
    monkeypatch.setattr(executor, "_wait_for_canonical_registration", registration)

    executor.execute_rollout(env)

    assert order == ["healthy_readback", "runtime_trust", "registration"]
    assert calls["trust_requests"] == [_RUNTIME_TRUST_URL]


def test_executor_main_exits_non_zero_with_original_runtime_trust_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env, aws, calls = _rollout_fixture(
        monkeypatch,
        tmp_path,
        runtime_changes={"public_key_fingerprint_sha256": "f" * 64},
    )
    summary = tmp_path / "step-summary.md"
    for key, value in {**env, "GITHUB_STEP_SUMMARY": str(summary)}.items():
        monkeypatch.setenv(key, value)

    assert executor.main() == 1

    assert capsys.readouterr().err == (
        "ASAAS producer deployment blocked: "
        "rollout_failed_rolled_back:backend_trust_pin_mismatch\n"
    )
    assert aws.mutations[-1] == ("update-service", TASK_DEFINITION_ARN)
    assert calls["registration"] == []
    assert not summary.exists()


def test_execute_rollout_preserves_runtime_trust_failure_when_rollback_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    env, aws, calls = _rollout_fixture(
        monkeypatch,
        tmp_path,
        runtime_changes={"public_key_fingerprint_sha256": "f" * 64},
        fail_rollback=True,
    )

    with pytest.raises(
        executor.DeployError,
        match="^rollout_failed_and_rollback_failed:backend_trust_pin_mismatch$",
    ):
        executor.execute_rollout(env)

    assert aws.mutations[-1] == ("update-service", TASK_DEFINITION_ARN)
    assert calls["registration"] == []
