from __future__ import annotations

from pathlib import Path

import pytest

from scripts import asaas_producer_deploy_executor as executor

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
    with pytest.raises(
        executor.DeployError, match="live_service_rollout_not_complete"
    ):
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
