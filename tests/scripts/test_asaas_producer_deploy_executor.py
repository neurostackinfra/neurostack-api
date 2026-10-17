from __future__ import annotations

import pytest

from scripts import asaas_producer_deploy_executor as executor

TASK_ARN = "arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/task-1"
TASK_DEFINITION_ARN = (
    "arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:8"
)
CREATED_AT = "2026-10-14T22:54:10.831Z"
STARTED_AT = "2026-10-14T22:54:54.110Z"
IMAGE_DIGEST = "sha256:" + "a" * 64


def _snapshot() -> dict[str, object]:
    return {
        "task_arns": [TASK_ARN],
        "task_private_ips": ["10.0.1.7"],
        "task_timestamps": [
            {
                "task_arn": TASK_ARN,
                "created_at": CREATED_AT,
                "started_at": STARTED_AT,
            }
        ],
    }


def _live_task() -> dict[str, object]:
    return {
        "taskArn": TASK_ARN,
        "taskDefinitionArn": TASK_DEFINITION_ARN,
        "lastStatus": "RUNNING",
        "desiredStatus": "RUNNING",
        "createdAt": CREATED_AT,
        "startedAt": STARTED_AT,
        "privateIp": "10.0.1.7",
        "containers": [
            {"name": "app", "lastStatus": "RUNNING", "imageDigest": IMAGE_DIGEST}
        ],
    }


def test_authenticated_predeploy_readback_binds_exact_cohort_and_task_times() -> None:
    executor._validate_predeployment_live_cohort(
        _snapshot(),
        [_live_task()],
        task_definition_arn=TASK_DEFINITION_ARN,
        app_image_digest=IMAGE_DIGEST,
    )


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "createdAt",
            "2026-10-14T22:54:10.832Z",
            "active_task_timestamps_changed_after_preflight",
        ),
        ("startedAt", None, "active_task_timestamps_changed_after_preflight"),
        (
            "taskDefinitionArn",
            "arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:9",
            "active_task_definition_changed_after_preflight",
        ),
        (
            "taskArn",
            "arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/task-2",
            "active_task_cohort_changed_after_preflight",
        ),
    ],
)
def test_predeploy_readback_rejects_cohort_or_timestamp_drift(
    field: str, value: object, reason: str
) -> None:
    task = _live_task()
    task[field] = value
    with pytest.raises(executor.DeployError, match=reason):
        executor._validate_predeployment_live_cohort(
            _snapshot(),
            [task],
            task_definition_arn=TASK_DEFINITION_ARN,
            app_image_digest=IMAGE_DIGEST,
        )


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
