"""Boundary tests for the read-only producer handoff client."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "asaas-producer-handoff.py"
SPEC = importlib.util.spec_from_file_location("asaas_producer_handoff", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
handoff = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(handoff)

APP_DIGEST = "sha256:" + "a" * 64
BUNDLE_DIGEST = "b" * 64
TASK_DEFINITION = (
    "arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:8"
)
TASKS = [
    "arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/" + "1" * 32,
    "arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/" + "2" * 32,
]


@pytest.mark.parametrize(
    ("detail", "retry_guidance"),
    [
        ({"error": "authorization_ttl_exceeds_assignment"}, True),
        (
            {
                "error": "producer_pair_blocked",
                "reason": "grant pair lacks the requested validity window",
            },
            True,
        ),
        (
            {
                "error": "producer_pair_blocked",
                "reason": "grant validity window expired",
            },
            True,
        ),
        ({"error": "assignment_unavailable_or_mismatched"}, False),
    ],
)
def test_grant_retry_guidance_is_limited_to_ttl_and_window_failures(
    monkeypatch: pytest.MonkeyPatch, detail: dict[str, str], retry_guidance: bool
) -> None:
    body = json.dumps({"detail": detail}).encode()

    def fail_request(*_args: Any, **_kwargs: Any) -> Any:
        raise handoff.HTTPError(
            "https://api.neurostack.global/endpoint",
            409,
            "rejected",
            hdrs=None,
            fp=io.BytesIO(body),
        )

    monkeypatch.setattr(handoff, "urlopen", fail_request)
    with pytest.raises(handoff.HandoffCliError) as error:
        handoff._api_post_endpoint(
            "/v1/admin/billing/producer-grants/authorizations",
            {},
            "token-is-never-in-the-error",
            error_label="producer authorization",
        )

    assert ("fresh metadata readback" in str(error.value)) is retry_guidance
    assert "token-is-never-in-the-error" not in str(error.value)


@pytest.mark.parametrize("command", [[], ["grant"], ["receipt"]])
def test_cli_help_runs_without_services_package_import(command):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *command, "--help"],
        cwd=SCRIPT.parents[1],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "usage:" in result.stdout.lower()


def _aws_payloads(*, app_digest: str = APP_DIGEST) -> list[Any]:
    app_uri = (
        "388460142431.dkr.ecr.us-east-1.amazonaws.com/"
        "neurostack-staging:release-20261014"
    )
    service = {
        "serviceArn": handoff.SERVICE_ARN,
        "status": "ACTIVE",
        "deploymentController": "ECS",
        "desiredCount": 2,
        "runningCount": 2,
        "pendingCount": 0,
        "taskDefinition": TASK_DEFINITION,
        "deployments": [
            {
                "id": "ecs-svc/active-deployment",
                "status": "PRIMARY",
                "taskDefinition": TASK_DEFINITION,
                "desiredCount": 2,
                "runningCount": 2,
                "rolloutState": "COMPLETED",
            }
        ],
    }
    tasks = [
        {
            "taskArn": arn,
            "taskDefinitionArn": TASK_DEFINITION,
            "lastStatus": "RUNNING",
            "desiredStatus": "RUNNING",
            "containers": [
                {
                    "name": "app",
                    "image": app_uri,
                    "imageDigest": app_digest,
                    "lastStatus": "RUNNING",
                }
            ],
        }
        for arn in TASKS
    ]
    task_definition = {
        "family": "neurostack-staging-app",
        "revision": 8,
        "containerDefinitions": [
            {
                "name": "app",
                "image": app_uri,
                "environment": [
                    {"name": "BILLING_CONFIG_GENERATION", "value": "cfg-8"},
                    {"name": "BILLING_COMPATIBILITY_GENERATION", "value": "compat-7"},
                    {"name": "BILLING_BUNDLE_DIGEST", "value": BUNDLE_DIGEST},
                    {"name": "BILLING_ROLLOUT_STATE", "value": "disabled"},
                    {"name": "BILLING_TOPUP_COMMERCIAL_ENABLED", "value": "false"},
                ],
                "secrets": [
                    {
                        "name": name,
                        "valueFrom": (
                            "arn:aws:secretsmanager:us-east-1:388460142431:secret:"
                            f"{path}-AbCdEf"
                        ),
                    }
                    for name, path in handoff.SECRET_REFERENCE_NAMES.items()
                ],
            }
        ],
    }
    return [
        [{"clusterArn": handoff.CLUSTER_ARN, "status": "ACTIVE"}],
        [service],
        TASKS,
        tasks,
        [{"imageDigest": app_digest, "imageTags": ["release-20261014"]}],
        task_definition,
    ]


def _runner(payloads: list[Any]):
    values = iter(payloads)

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps(next(values)), "")

    return run


def _readback_snapshot() -> dict[str, Any]:
    readback = handoff._aws_readback(
        expected_app_digest=APP_DIGEST,
        expected_bundle_digest=BUNDLE_DIGEST,
        runner=_runner(_aws_payloads()),
        readiness_probe=lambda: True,
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
    return {"status": "ready", "readback": readback}


def _pipe_json(value: Any) -> int:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, json.dumps(value, sort_keys=True).encode())
    os.close(write_fd)
    return read_fd


class _FixedDigestSigner:
    def sign_digest(self, _key_arn: str, _digest: bytes) -> bytes:
        return b"test-signature"


def _build_attestation_envelope() -> dict[str, Any]:
    domain = handoff._load_signed_envelope_domain()
    workflow_path = ".github/workflows/asaas-build-attestation.yml"
    repository_id = "1152335815"
    app_digest = "sha256:" + "a" * 64
    payload = {
        "attestation_type": "billing_build_attestation.v1",
        "attestation_id": "77777777-7777-4777-8777-777777777777",
        "repository_id": repository_id,
        "repository_name": "neurostackinfra/neurostack-api",
        "workflow_path": workflow_path,
        "workflow_ref": "refs/heads/main",
        "workflow_run_id": 123456789,
        "app_image_digest": app_digest,
        "bundle_digest": "b" * 64,
        "commit_sha": "c" * 64,
        "tree_sha": "d" * 64,
        "workflow_id": f"github-actions:{repository_id}:{workflow_path}",
        "immutable_artifact_reference": (
            "388460142431.dkr.ecr.us-east-1.amazonaws.com/asaas-producer@"
            f"{app_digest}"
        ),
    }
    key_arn = (
        "arn:aws:kms:us-east-1:388460142431:key/01234567-89ab-cdef-0123-456789abcdef"
    )
    entry = domain.TrustEntry(
        issuer="test-asaas-build",
        kid=key_arn,
        environment=domain.ENVIRONMENT,
        audience="sts.amazonaws.com",
        type="billing_build_attestation.v1",
        purpose=domain.PURPOSE_BY_TYPE["billing_build_attestation.v1"],
        schema_version=domain.SCHEMA_VERSION,
        algorithm=domain.ALGORITHM,
        public_key_pem="test public key placeholder",
        repository_id=repository_id,
        repository_name="neurostackinfra/neurostack-api",
        workflow_path=workflow_path,
        workflow_ref="refs/heads/main",
    )
    issued_at = datetime(2026, 10, 15, tzinfo=timezone.utc)
    return domain.build_envelope(
        entry=entry,
        payload=payload,
        operation_id=UUID("88888888-8888-4888-8888-888888888888"),
        issued_at=issued_at,
        expires_at=issued_at + timedelta(days=7),
        signer=_FixedDigestSigner(),
    )


def test_register_build_attestation_posts_closed_canonical_request_with_fd_jwt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    envelope = _build_attestation_envelope()
    artifact = tmp_path / "asaas-build-attestation.json"
    artifact.write_text(json.dumps(envelope, sort_keys=True), encoding="utf-8")
    token = "eyJheader.eyJclaims.signature"
    auth_fd, write_fd = os.pipe()
    os.write(write_fd, token.encode("ascii"))
    os.close(write_fd)

    domain = handoff._load_signed_envelope_domain()
    response_payload = {
        "attestation_id": envelope["payload"]["attestation_id"],
        "attestation_digest": domain.envelope_digest(envelope),
        "registered_at": "2026-10-15T23:45:12.123456Z",
    }
    requests: list[Any] = []

    class Response:
        status = 201

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return json.dumps(response_payload).encode("utf-8")

    def fake_urlopen(request: Any, *, timeout: int) -> Response:
        requests.append(request)
        assert timeout == 15
        return Response()

    monkeypatch.setattr(handoff, "urlopen", fake_urlopen)
    args = handoff._parser().parse_args(
        [
            "register-build-attestation",
            "--artifact",
            str(artifact),
            "--auth-fd",
            str(auth_fd),
        ]
    )

    assert handoff._run(args) == 0
    output = capsys.readouterr()
    assert token not in output.out + output.err
    assert json.loads(output.out) == response_payload
    assert len(requests) == 1
    request = requests[0]
    assert request.full_url == (
        handoff.API_BASE_URL + handoff.BUILD_ATTESTATION_REGISTRATION_PATH
    )
    assert request.method == "POST"
    assert request.get_header("Authorization") == f"Bearer {token}"
    assert json.loads(request.data) == {
        "schema_version": handoff.BUILD_ATTESTATION_REGISTRATION_SCHEMA,
        "envelope": envelope,
    }
    assert set(json.loads(request.data)) == {"schema_version", "envelope"}
    with pytest.raises(OSError):
        os.fstat(auth_fd)


@pytest.mark.parametrize(
    ("response_change", "message"),
    [
        ("id", "response ID"),
        ("digest", "response digest"),
        ("timestamp", "response timestamp"),
        ("extra", "missing or extra fields"),
    ],
)
def test_register_build_attestation_rejects_mismatched_response_without_token_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    response_change: str,
    message: str,
) -> None:
    envelope = _build_attestation_envelope()
    artifact = tmp_path / "asaas-build-attestation.json"
    artifact.write_text(json.dumps(envelope), encoding="utf-8")
    token = "eyJheader.eyJclaims.signature"
    auth_fd, write_fd = os.pipe()
    os.write(write_fd, token.encode("ascii"))
    os.close(write_fd)
    domain = handoff._load_signed_envelope_domain()
    response_payload = {
        "attestation_id": envelope["payload"]["attestation_id"],
        "attestation_digest": domain.envelope_digest(envelope),
        "registered_at": "2026-10-15T23:45:12.123456Z",
    }
    if response_change == "id":
        response_payload["attestation_id"] = "99999999-9999-4999-8999-999999999999"
    elif response_change == "digest":
        response_payload["attestation_digest"] = "0" * 64
    elif response_change == "timestamp":
        response_payload["registered_at"] = "not-a-time"
    else:
        response_payload["unexpected"] = "field"

    class Response:
        status = 201

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return json.dumps(response_payload).encode("utf-8")

    monkeypatch.setattr(handoff, "urlopen", lambda *_args, **_kwargs: Response())
    args = handoff._parser().parse_args(
        [
            "register-build-attestation",
            "--artifact",
            str(artifact),
            "--auth-fd",
            str(auth_fd),
        ]
    )

    with pytest.raises(handoff.HandoffCliError, match=message):
        handoff._run(args)
    output = capsys.readouterr()
    assert token not in output.out + output.err


def test_register_build_attestation_rejects_invalid_artifact_before_reading_jwt(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "invalid-attestation.json"
    artifact.write_text('{"duplicate":1,"duplicate":2}', encoding="utf-8")
    auth_fd, write_fd = os.pipe()
    os.write(write_fd, b"eyJheader.eyJclaims.signature")
    os.close(write_fd)
    args = handoff._parser().parse_args(
        [
            "register-build-attestation",
            "--artifact",
            str(artifact),
            "--auth-fd",
            str(auth_fd),
        ]
    )

    with pytest.raises(handoff.HandoffCliError, match="duplicate fields"):
        handoff._run(args)
    os.fstat(auth_fd)
    os.close(auth_fd)


def test_auth_fd_reads_and_closes_only_inherited_pipe() -> None:
    read_fd, write_fd = os.pipe()
    token = "eyJheader.eyJclaims.signature"
    os.write(write_fd, token.encode())
    os.close(write_fd)

    assert handoff.read_auth_fd(read_fd) == token
    with pytest.raises(OSError):
        os.fstat(read_fd)


def test_auth_fd_rejects_regular_files_and_closes_the_rejected_fd(
    tmp_path: Path,
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("eyJheader.eyJclaims.signature", encoding="ascii")
    fd = os.open(token_file, os.O_RDONLY)
    assert stat.S_ISREG(os.fstat(fd).st_mode)

    with pytest.raises(handoff.HandoffCliError, match="pipe or socket"):
        handoff.read_auth_fd(fd)
    with pytest.raises(OSError):
        os.fstat(fd)


def test_auth_fd_rejects_standard_streams_before_reading() -> None:
    with pytest.raises(handoff.HandoffCliError, match="stdin, stdout, or stderr"):
        handoff.read_auth_fd(0)


def test_auth_fd_rejects_invalid_jwt_and_does_not_include_token_in_error() -> None:
    read_fd, write_fd = os.pipe()
    token = b"not-a-jwt-secret"
    os.write(write_fd, token)
    os.close(write_fd)

    with pytest.raises(handoff.HandoffCliError) as error:
        handoff.read_auth_fd(read_fd)
    assert token.decode() not in str(error.value)
    with pytest.raises(OSError):
        os.fstat(read_fd)


def test_readback_accepts_only_fixed_target_and_metadata_only_aws_calls() -> None:
    values = iter(_aws_payloads())
    commands: list[list[str]] = []

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, json.dumps(next(values)), "")

    result = handoff._aws_readback(
        expected_app_digest=APP_DIGEST,
        expected_bundle_digest=BUNDLE_DIGEST,
        runner=runner,
        readiness_probe=lambda: True,
    )

    assert result["task_arns"] == sorted(TASKS)
    cohort_payload = {
        "schema": "ecs-task-cohort.v1",
        "cluster_arn": handoff.CLUSTER_ARN,
        "service_arn": handoff.SERVICE_ARN,
        "deployment_id": "ecs-svc/active-deployment",
        "task_definition_arn": TASK_DEFINITION,
        "running_task_arns": sorted(TASKS),
    }
    cohort_digest = hashlib.sha256(
        json.dumps(
            cohort_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert result["task_set_id"] == f"ecs-task-cohort-sha256:{cohort_digest}"
    assert result["deployment_controller"] == "ECS"
    assert result["app_image_digest"] == APP_DIGEST
    assert result["rollout_control"] == "disabled"
    assert result["topup_commercial_enabled"] is False
    assert {item["name"] for item in result["secret_references"]} == set(
        handoff.SECRET_REFERENCE_NAMES
    )
    assert all(
        item["version_id"] == "task-definition-unpinned"
        for item in result["secret_references"]
    )
    assert all("secretvalue" not in " ".join(command).lower() for command in commands)
    assert all(
        not any(
            mutator in command
            for mutator in ("update-service", "register-task-definition", "run-task")
        )
        for command in commands
    )
    list_tasks = next(command for command in commands if "list-tasks" in command)
    assert "--page-size" in list_tasks
    assert "--no-paginate" not in list_tasks
    assert "task_set_id" not in handoff._api_readback(result)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("controller", "fully converged"),
        ("duplicate_list", "task set does not match"),
        ("mismatched_describe", "task readback is incomplete"),
    ],
)
def test_readback_rejects_incomplete_or_non_ecs_task_cohorts(
    mutation: str, message: str
) -> None:
    payloads = _aws_payloads()
    if mutation == "controller":
        payloads[1][0]["deploymentController"] = "CODE_DEPLOY"
    elif mutation == "duplicate_list":
        payloads[2] = [TASKS[0], TASKS[0]]
    else:
        payloads[3] = [payloads[3][0], dict(payloads[3][1], taskArn=TASKS[0])]
    values = iter(payloads)

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps(next(values)), "")

    with pytest.raises(handoff.HandoffCliError, match=message):
        handoff._aws_readback(
            expected_app_digest=APP_DIGEST,
            expected_bundle_digest=BUNDLE_DIGEST,
            runner=runner,
            readiness_probe=lambda: True,
        )


def test_readback_only_labels_an_explicit_secret_version_id() -> None:
    payloads = _aws_payloads()
    payloads[-1]["containerDefinitions"][0]["secrets"][0][
        "valueFrom"
    ] += "::AWSCURRENT:version-123"
    values = iter(payloads)

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps(next(values)), "")

    result = handoff._aws_readback(
        expected_app_digest=APP_DIGEST,
        expected_bundle_digest=BUNDLE_DIGEST,
        runner=runner,
        readiness_probe=lambda: True,
    )
    assert result["secret_references"][0]["version_id"] == "version-123"


@pytest.mark.parametrize("case", ["missing", "extra", "remapped"])
def test_readback_requires_exact_secret_reference_mapping(case: str) -> None:
    payloads = _aws_payloads()
    secrets = payloads[-1]["containerDefinitions"][0]["secrets"]
    if case == "missing":
        secrets.pop()
    elif case == "extra":
        secrets.append(
            {
                "name": "EXTRA_SECRET",
                "valueFrom": (
                    "arn:aws:secretsmanager:us-east-1:388460142431:secret:"
                    "neurostack/staging/secret-key-XyZ123"
                ),
            }
        )
    else:
        secrets[0]["valueFrom"] = (
            "arn:aws:secretsmanager:us-east-1:388460142431:secret:"
            "neurostack/staging/secret-key-XyZ123"
        )

    with pytest.raises(handoff.HandoffCliError, match="secret"):
        handoff._aws_readback(
            expected_app_digest=APP_DIGEST,
            expected_bundle_digest=BUNDLE_DIGEST,
            runner=_runner(payloads),
            readiness_probe=lambda: True,
        )


def test_readback_rejects_digest_mismatch_before_signing() -> None:
    values = iter(_aws_payloads(app_digest="sha256:" + "c" * 64))

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps(next(values)), "")

    with pytest.raises(handoff.HandoffCliError, match="approved digest"):
        handoff._aws_readback(
            expected_app_digest=APP_DIGEST,
            expected_bundle_digest=BUNDLE_DIGEST,
            runner=runner,
            readiness_probe=lambda: True,
        )


def test_readback_fails_closed_if_rollout_or_topup_is_not_disabled() -> None:
    payloads = _aws_payloads()
    payloads[-1]["containerDefinitions"][0]["environment"][-1]["value"] = "true"
    values = iter(payloads)

    def runner(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 0, json.dumps(next(values)), "")

    with pytest.raises(handoff.HandoffCliError, match="disabled gates"):
        handoff._aws_readback(
            expected_app_digest=APP_DIGEST,
            expected_bundle_digest=BUNDLE_DIGEST,
            runner=runner,
            readiness_probe=lambda: True,
        )


def test_invalid_scope_and_target_are_rejected_before_aws_call() -> None:
    parser = handoff._parser()
    args = parser.parse_args(
        [
            "preflight",
            "--environment",
            "production",
            "--operational-role",
            "production-serving",
            "--account-id",
            "388460142431",
            "--region",
            "us-east-1",
            "--cluster-arn",
            handoff.CLUSTER_ARN,
            "--service-arn",
            handoff.SERVICE_ARN,
            "--expected-app-digest",
            APP_DIGEST,
            "--expected-bundle-digest",
            BUNDLE_DIGEST,
            "--read-only",
            "--require-disabled",
        ]
    )
    with pytest.raises(handoff.HandoffCliError, match="allowlisted"):
        handoff._assert_target(args)


def test_receipt_preflight_consumes_read_only_grant_and_prints_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    readback_fd = _pipe_json(_readback_snapshot())
    parser = handoff._parser()
    args = parser.parse_args(
        [
            "receipt",
            "preflight",
            "--environment",
            "staging",
            "--operational-role",
            "production-serving",
            "--account-id",
            "388460142431",
            "--region",
            "us-east-1",
            "--cluster-arn",
            handoff.CLUSTER_ARN,
            "--service-arn",
            handoff.SERVICE_ARN,
            "--operation-id",
            "89bf1556-9f6d-4f39-b5c8-8dcb041877da",
            "--auth-fd",
            "9",
            "--readback-fd",
            str(readback_fd),
            "--assignment-id",
            "11111111-1111-4111-8111-111111111111",
            "--authorization-receipt-id",
            "22222222-2222-4222-8222-222222222222",
            "--authorization-scope",
            "read_billing_producer_pair",
            "--max-authorization-ttl-seconds",
            "300",
            "--expected-app-digest",
            APP_DIGEST,
            "--expected-bundle-digest",
            BUNDLE_DIGEST,
            "--resolve-current-pair-server-side",
            "--require-active-target-and-provenance",
            "--consume-read-only-authorization",
            "--no-receipt-or-pointer-mutation",
            "--print-json",
        ]
    )
    calls: list[tuple[str, dict[str, Any], str]] = []
    monkeypatch.setattr(
        handoff,
        "_aws_readback",
        lambda **_kwargs: pytest.fail(
            "receipt preflight must reuse the supplied snapshot"
        ),
    )
    monkeypatch.setattr(
        handoff, "read_auth_fd", lambda _fd: "eyheader.eyclaims.signature"
    )
    monkeypatch.setattr(
        handoff,
        "_api_post",
        lambda path, body, token: calls.append((path, body, token))
        or {"action": "create", "current_active_pair": None},
    )

    assert handoff._run(args) == 0
    assert calls[0][0] == "preflight"
    assert calls[0][1]["assignment_id"] == args.assignment_id
    assert calls[0][1]["authorization_receipt_id"] == args.authorization_receipt_id
    assert calls[0][1]["readback"]["deployment_controller"] == "ECS"
    assert "task_set_id" not in calls[0][1]["readback"]
    assert "environment" not in calls[0][1]["readback"]
    assert "operational_role" not in calls[0][1]["readback"]
    assert calls[0][1]["readback"]["task_arns"] == sorted(TASKS)
    assert calls[0][2] == "eyheader.eyclaims.signature"
    assert json.loads(capsys.readouterr().out) == {
        "action": "create",
        "current_active_pair": None,
    }


def test_read_grant_assignment_and_preflight_reuse_same_snapshot_and_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = _readback_snapshot()
    assignment_readback_fd = _pipe_json(snapshot)
    assignment_parser = handoff._parser()
    assignment_args = assignment_parser.parse_args(
        [
            "grant",
            "assignment",
            "--environment",
            "staging",
            "--operational-role",
            "production-serving",
            "--account-id",
            "388460142431",
            "--region",
            "us-east-1",
            "--cluster-arn",
            handoff.CLUSTER_ARN,
            "--service-arn",
            handoff.SERVICE_ARN,
            "--expected-app-digest",
            APP_DIGEST,
            "--expected-bundle-digest",
            BUNDLE_DIGEST,
            "--auth-fd",
            "9",
            "--readback-fd",
            str(assignment_readback_fd),
            "--assignment-id",
            "11111111-1111-4111-8111-111111111111",
            "--operation-id",
            "89bf1556-9f6d-4f39-b5c8-8dcb041877da",
            "--executor-user-id",
            "33333333-3333-4333-8333-333333333333",
            "--action",
            "read_billing_producer_pair",
            "--ttl-seconds",
            "300",
            "--minimum-grant-validity-seconds",
            "240",
            "--minimum-pair-validity-seconds",
            "300",
        ]
    )

    assignment_calls: list[tuple[str, dict[str, Any], str]] = []
    aws_calls: list[bool] = []
    monkeypatch.setattr(
        handoff,
        "_aws_readback",
        lambda **_kwargs: aws_calls.append(True)
        or pytest.fail("read-grant assignment must not reread AWS"),
    )
    monkeypatch.setattr(
        handoff, "read_auth_fd", lambda _fd: "eyJheader.eyJclaims.signature"
    )
    monkeypatch.setattr(
        handoff,
        "_api_post_endpoint",
        lambda path, body, token, **_kwargs: assignment_calls.append(
            (path, body, token)
        )
        or {"assignment_id": body["assignment_id"]},
    )

    assert handoff._run(assignment_args) == 0
    assert assignment_calls[0][0] == "/v1/admin/billing/producer-grants/assignments"
    assignment_body = assignment_calls[0][1]
    assert assignment_body["action"] == "read_billing_producer_pair"
    assert assignment_body["receipt_ids"] == []
    assert assignment_body["minimum_grant_validity_seconds"] == 240
    assert assignment_body["minimum_pair_validity_seconds"] == 300
    assert assignment_body["readback"] == handoff._api_readback(snapshot["readback"])

    preflight_readback_fd = _pipe_json(snapshot)
    preflight_args = assignment_parser.parse_args(
        [
            "receipt",
            "preflight",
            "--environment",
            "staging",
            "--operational-role",
            "production-serving",
            "--account-id",
            "388460142431",
            "--region",
            "us-east-1",
            "--cluster-arn",
            handoff.CLUSTER_ARN,
            "--service-arn",
            handoff.SERVICE_ARN,
            "--operation-id",
            "89bf1556-9f6d-4f39-b5c8-8dcb041877da",
            "--minimum-grant-validity-seconds",
            "240",
            "--minimum-pair-validity-seconds",
            "300",
            "--auth-fd",
            "9",
            "--readback-fd",
            str(preflight_readback_fd),
            "--assignment-id",
            "11111111-1111-4111-8111-111111111111",
            "--authorization-receipt-id",
            "22222222-2222-4222-8222-222222222222",
            "--authorization-scope",
            "read_billing_producer_pair",
            "--max-authorization-ttl-seconds",
            "300",
            "--expected-app-digest",
            APP_DIGEST,
            "--expected-bundle-digest",
            BUNDLE_DIGEST,
            "--resolve-current-pair-server-side",
            "--require-active-target-and-provenance",
            "--consume-read-only-authorization",
            "--no-receipt-or-pointer-mutation",
            "--print-json",
        ]
    )
    preflight_calls: list[tuple[str, dict[str, Any], str]] = []
    monkeypatch.setattr(
        handoff,
        "_api_post",
        lambda path, body, token: preflight_calls.append((path, body, token))
        or {"action": "create", "current_active_pair": None},
    )
    assert handoff._run(preflight_args) == 0
    assert preflight_calls[0][1]["readback"] == assignment_body["readback"]
    assert (
        preflight_calls[0][1]["minimum_grant_validity_seconds"]
        == assignment_body["minimum_grant_validity_seconds"]
    )
    assert preflight_calls[0][1]["minimum_pair_validity_seconds"] == 300
    assert aws_calls == []


@pytest.mark.parametrize(
    "mutation",
    [
        lambda document: document["readback"].update(extra="reject"),
        lambda document: document["readback"].update(bundle_digest="c" * 64),
        lambda document: document["readback"].update(
            task_set_id="ecs-task-cohort-sha256:" + "0" * 64
        ),
        lambda document: document.update(status="blocked"),
    ],
)
def test_readback_fd_rejects_malformed_or_divergent_snapshot(
    mutation: Any,
) -> None:
    document = _readback_snapshot()
    mutation(document)
    fd = _pipe_json(document)
    with pytest.raises(handoff.HandoffCliError):
        handoff._read_cli_readback(
            fd,
            expected_app_digest=APP_DIGEST,
            expected_bundle_digest=BUNDLE_DIGEST,
        )
    with pytest.raises(OSError):
        os.fstat(fd)


def test_readback_fd_rejects_regular_file_and_closes_it(tmp_path: Path) -> None:
    path = tmp_path / "readback.json"
    path.write_text(json.dumps(_readback_snapshot()), encoding="utf-8")
    fd = os.open(path, os.O_RDONLY)
    with pytest.raises(handoff.HandoffCliError, match="pipe or socket"):
        handoff._read_cli_readback(
            fd,
            expected_app_digest=APP_DIGEST,
            expected_bundle_digest=BUNDLE_DIGEST,
        )
    with pytest.raises(OSError):
        os.fstat(fd)


def test_command_surface_excludes_deploy_and_financial_egress() -> None:
    source = SCRIPT.read_text(encoding="utf-8").lower()
    for forbidden in (
        "update-service",
        "register-task-definition",
        "asaas checkout",
        "refund payment",
        "top-up",
    ):
        assert forbidden not in source
