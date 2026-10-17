from __future__ import annotations

import copy

import pytest

from scripts.asaas_database_secret_reference import (
    ReferenceError,
    resolve_active_database_secret,
)

SECRET_ARN = "arn:aws:secretsmanager:us-east-1:388460142431:secret:neurostack-db-a1b2c3"
VERSION_ID = "12345678-1234-5678-1234-567812345678"
OLD_VERSION_ID = "87654321-4321-8765-4321-876543218765"


def _input(*, value_from: str | None = None) -> dict[str, object]:
    value_from = value_from or f"{SECRET_ARN}:database_url:AWSCURRENT:"
    task_definition_arn = (
        "arn:aws:ecs:us-east-1:388460142431:task-definition/neurostack-staging-app:12"
    )
    return {
        "desired_count": 2,
        "active_tasks": [
            {
                "taskArn": "arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/one",
                "taskDefinitionArn": task_definition_arn,
                "lastStatus": "RUNNING",
                "desiredStatus": "RUNNING",
                "createdAt": "2026-10-16T10:02:00.000Z",
                "startedAt": "2026-10-16T10:02:05.000Z",
            },
            {
                "taskArn": "arn:aws:ecs:us-east-1:388460142431:task/neurostack-staging/two",
                "taskDefinitionArn": task_definition_arn,
                "lastStatus": "RUNNING",
                "desiredStatus": "RUNNING",
                "createdAt": "2026-10-16T10:03:00.000Z",
                "startedAt": "2026-10-16T10:03:05.000Z",
            },
        ],
        "task_definitions": [
            {
                "taskDefinitionArn": task_definition_arn,
                "containerDefinitions": [
                    {
                        "name": "app",
                        "secrets": [{"name": "DATABASE_URL", "valueFrom": value_from}],
                    }
                ],
            }
        ],
        "secret_description": {
            "ARN": SECRET_ARN,
            "LastChangedDate": "2026-10-16T10:00:00.000Z",
        },
        "secret_versions": {
            "ARN": SECRET_ARN,
            "Versions": [
                {"VersionId": VERSION_ID, "VersionStages": ["AWSCURRENT"]},
                {"VersionId": OLD_VERSION_ID, "VersionStages": ["AWSPREVIOUS"]},
            ],
        },
    }


def test_floating_stage_resolves_stable_version_and_pins_replacement_reference() -> (
    None
):
    result = resolve_active_database_secret(_input())
    assert result["schema"] == "asaas_database_secret_reference.v1"
    assert result["database_secret_arn"] == SECRET_ARN
    assert result["database_secret_json_key"] == "database_url"
    assert result["resolved_version_id"] == VERSION_ID
    assert (
        result["replacement_value_from"] == f"{SECRET_ARN}:database_url::{VERSION_ID}"
    )
    assert result["active_task_count"] == 2
    assert "SecretString" not in result


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda data: data["active_tasks"][1].update({"taskDefinitionArn": "mixed"}),
            "active_task_definitions_mixed",
        ),
        (lambda data: data["active_tasks"].pop(), "active_task_cohort_mismatch"),
        (
            lambda data: data["secret_versions"]["Versions"].append(
                {
                    "VersionId": "11111111-1111-1111-1111-111111111111",
                    "VersionStages": ["AWSCURRENT"],
                }
            ),
            "secret_stage_resolution_ambiguous",
        ),
        (
            lambda data: data["secret_description"].update(
                {"LastChangedDate": "2026-10-16T10:01:01.000Z"}
            ),
            "floating_secret_mapping_not_stable_before_task_pending",
        ),
        (
            lambda data: data["active_tasks"][0].update(
                {"createdAt": "2026-10-16T10:02:00Z"}
            ),
            "task_created_at_precision_ambiguous",
        ),
        (
            lambda data: data["active_tasks"][0].update(
                {"startedAt": "2026-10-16T10:01:59.000Z"}
            ),
            "task_time_order_invalid",
        ),
        (
            lambda data: data["secret_description"].update(
                {"SecretString": "must-never-be-present"}
            ),
            "secret_value_field_forbidden",
        ),
    ],
)
def test_ambiguous_or_changed_metadata_fails_closed(mutate, reason: str) -> None:
    data = copy.deepcopy(_input())
    mutate(data)
    with pytest.raises(ReferenceError, match=reason):
        resolve_active_database_secret(data)


def test_floating_secret_changed_after_created_at_and_before_started_at_fails() -> None:
    data = _input()
    data["secret_description"]["LastChangedDate"] = "2026-10-16T10:02:02.000Z"
    with pytest.raises(
        ReferenceError, match="floating_secret_mapping_not_stable_before_task_pending"
    ):
        resolve_active_database_secret(data)


def test_explicit_version_must_exist_and_is_not_reinterpreted_as_stage() -> None:
    data = _input(value_from=f"{SECRET_ARN}:database_url::{VERSION_ID}")
    result = resolve_active_database_secret(data)
    assert result["source_selector_kind"] == "version_id"
    assert result["resolved_version_id"] == VERSION_ID
    data = _input(value_from=f"{SECRET_ARN}:database_url::missing-version")
    with pytest.raises(ReferenceError, match="explicit_secret_version_missing"):
        resolve_active_database_secret(data)


def test_arn_and_duplicate_reference_mismatches_fail_closed() -> None:
    data = _input(
        value_from=(
            "arn:aws:secretsmanager:us-east-1:388460142431:secret:"
            "other-b1c2d3:database_url:AWSCURRENT:"
        )
    )
    with pytest.raises(ReferenceError, match="database_url_secret_arn_mismatch"):
        resolve_active_database_secret(data)
    data = _input()
    data["task_definitions"][0]["containerDefinitions"][0]["secrets"].append(
        {"name": "DATABASE_URL", "valueFrom": f"{SECRET_ARN}:database_url:AWSCURRENT:"}
    )
    with pytest.raises(ReferenceError, match="database_url_reference_count_mismatch"):
        resolve_active_database_secret(data)
