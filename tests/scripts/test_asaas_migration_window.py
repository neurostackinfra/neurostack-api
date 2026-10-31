"""Production migration windows 035, 036 and 037: pure planner, renderer, verifier.

Every test runs on recorded readback files built here; nothing reaches AWS or
a database. The Alembic and read-program output the verifiers classify is
real: ``tests/fixtures/asaas_migration_window/*.json`` holds what the
alembic CLI and the rendered read program printed against disposable
PostgreSQL 16 (success per window, 55000 refusals, a 55P03 lock timeout, a
40P01 deadlock and an unpinned ``upgrade head``).
"""

from __future__ import annotations

import argparse
import ast
import copy
import dataclasses
import json
import re
import subprocess
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts import asaas_deploy_hook as hook
from scripts import asaas_migration_window as mw
from scripts import asaas_production_wiring as wiring
from scripts import verify_asaas_producer_image_rollout as rollout

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "asaas_migration_window.py"
FIXTURES = ROOT / "tests" / "fixtures" / "asaas_migration_window"
MANIFEST = ROOT / wiring.MANIFEST_RELATIVE_PATH
RUNBOOK = ROOT / "docs" / "runbooks" / "asaas-deployment.md"

ACCOUNT = "388460142431"
REGISTRY = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com"
TD_PREFIX = f"arn:aws:ecs:us-east-1:{ACCOUNT}:task-definition/"
SERVICE_TD_8 = TD_PREFIX + "neurostack-staging-app:8"
SERVICE_TD_9 = TD_PREFIX + "neurostack-staging-app:9"
SERVICE_TD_10 = TD_PREFIX + "neurostack-staging-app:10"
MIGRATIONS_TD = {
    "035": TD_PREFIX + "neurostack-staging-migrations:4",
    "036": TD_PREFIX + "neurostack-staging-migrations:5",
    "037": TD_PREFIX + "neurostack-staging-migrations:6",
}
READ_TD = TD_PREFIX + "neurostack-staging-consulta:26"
# Window 037's dedicated read revision (planner --purpose read).
MIGRATIONS_READ_TD = TD_PREFIX + "neurostack-staging-migrations:7"
BRIDGE_DIGEST = "sha256:" + "2" * 64
BRIDGE_IMAGE = f"{REGISTRY}/neurostack-staging@{BRIDGE_DIGEST}"
OLD_TAG = "asaas-gateway-staging-20261014-cfcb7f0b"
OLD_IMAGE = f"{REGISTRY}/neurostack-staging:{OLD_TAG}"
OLD_DIGEST = "sha256:" + "8" * 64
SUBNETS = ["subnet-0a1b2c3d4e5f60718", "subnet-0f1e2d3c4b5a69788"]
SECURITY_GROUPS = ["sg-0123456789abcdef0"]
SECRET_PREFIX = f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:neurostack/staging/"
MIGRATIONS_DB_ARN = SECRET_PREFIX + "database-url-migrations-Mg7x2Q"
APP_DB_ARN = SECRET_PREFIX + "database-url-fyOYuj"
TASK_PREFIX = f"arn:aws:ecs:us-east-1:{ACCOUNT}:task/neurostack-staging/"
SERVICE_TASKS = [TASK_PREFIX + "a" * 32, TASK_PREFIX + "b" * 32]
PRE_TASK_ID = "1" * 32
RUN_TASK_ID = "3" * 32
POST_TASK_ID = "5" * 32
PRE_TASK = TASK_PREFIX + PRE_TASK_ID
RUN_TASK = TASK_PREFIX + RUN_TASK_ID
POST_TASK = TASK_PREFIX + POST_TASK_ID
SNAPSHOT_CREATED = "2026-10-22T10:00:00+00:00"
PRE_CREATED = "2026-10-22T10:20:00+00:00"
PRE_STOPPED = "2026-10-22T10:22:00+00:00"
CAPTURED_PLAN = "2026-10-22T10:29:00Z"
NOW = "2026-10-22T10:30:00Z"
NOW_DT = datetime(2026, 10, 22, 10, 30, tzinfo=timezone.utc)
RUN_CREATED = "2026-10-22T10:31:00+00:00"
RUN_STOPPED = "2026-10-22T10:33:00+00:00"
CAPTURED_VERIFY = "2026-10-22T10:36:00Z"
POST_CREATED = "2026-10-22T10:38:00+00:00"
POST_STOPPED = "2026-10-22T10:40:00+00:00"
CAPTURED_POST = "2026-10-22T10:41:00Z"
NOW_POST = "2026-10-22T10:42:00Z"
DEPLOYMENT_ID = "ecs-svc/1234567890123456789"
SNAPSHOT_ID = {
    "035": "pre-033-035-2026-10-22",
    "036": "pre-036-2026-10-22",
    "037": "pre-037-2026-10-22",
}
UPGRADE_HEAD = re.compile(r"upgrade\s+heads?\b", re.IGNORECASE)
# Built by concatenation so repository secret scanners never see a literal.
LEAKED_URL = "postgresql+asyncpg" + "://neurostack_owner:pw@db.internal:5432/app"
LEAKED_HOST = "neurostack-staging-db.abc123." + "us-east-1.rds.amazonaws.com"


def _golden(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return dict(document["runs"])


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI's only time source is mw._clock (no --now flag): pin it."""

    monkeypatch.setattr(mw, "_clock", lambda: NOW_DT)


ALEMBIC = _golden("alembic_runs.json")
READS = _golden("read_runs.json")
GOOD_PRE_READ = {
    "035": "pre_035_at_032",
    "036": "pre_036_at_035",
    "037": "pre_037_at_036_migration_role",
}
GOOD_POST_READ = {
    "035": "post_035_at_035",
    "036": "post_036_at_036",
    "037": "post_037_at_037",
}


# ---------------------------------------------------------------------------
# Readback fixtures
# ---------------------------------------------------------------------------


def _service(task_definition: str, **changes: Any) -> dict[str, Any]:
    service: dict[str, Any] = {
        "serviceArn": rollout.SERVICE_ARN,
        "serviceName": "neurostack-staging-app",
        "clusterArn": rollout.CLUSTER_ARN,
        "status": "ACTIVE",
        "desiredCount": 2,
        "runningCount": 2,
        "pendingCount": 0,
        "launchType": "FARGATE",
        "platformVersion": "LATEST",
        "taskDefinition": task_definition,
        "deployments": [
            {
                "id": DEPLOYMENT_ID,
                "status": "PRIMARY",
                "taskDefinition": task_definition,
                "desiredCount": 2,
                "runningCount": 2,
                "pendingCount": 0,
                "rolloutState": "COMPLETED",
                "createdAt": "2026-10-21T10:00:00+00:00",
            }
        ],
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": list(SUBNETS),
                "securityGroups": list(SECURITY_GROUPS),
                "assignPublicIp": "DISABLED",
            }
        },
    }
    service.update(changes)
    return {"services": [service], "failures": []}


def _app_definition(arn: str, image: str, **changes: Any) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "taskDefinitionArn": arn,
        "family": "neurostack-staging-app",
        "revision": int(arn.rsplit(":", 1)[1]),
        "status": "ACTIVE",
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "containerDefinitions": [
            {"name": "app", "image": image, "essential": True},
            {
                "name": "otel",
                "image": "public.ecr.aws/aws-observability/aws-otel-collector@sha256:"
                + "7" * 64,
                "essential": False,
            },
        ],
    }
    definition.update(changes)
    return {"taskDefinition": definition}


def _one_container_definition(
    arn: str, family: str, container: dict[str, Any], **changes: Any
) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "taskDefinitionArn": arn,
        "family": family,
        "revision": int(arn.rsplit(":", 1)[1]),
        "status": "ACTIVE",
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "executionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/neurostack-staging-ecs-execution",
        "containerDefinitions": [container],
    }
    definition.update(changes)
    return {"taskDefinition": definition}


def _migrations_container(window: str, **changes: Any) -> dict[str, Any]:
    container: dict[str, Any] = {
        "name": "migrations",
        "image": BRIDGE_IMAGE,
        "essential": True,
        "command": ["alembic", "upgrade", window],
        "environment": [{"name": "ENVIRONMENT", "value": "staging"}],
        "secrets": [{"name": "DATABASE_URL", "valueFrom": MIGRATIONS_DB_ARN}],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": "/ecs/neurostack-staging/migrations",
                "awslogs-region": "us-east-1",
                "awslogs-stream-prefix": "migrations",
            },
        },
    }
    container.update(changes)
    return container


def _read_container(**changes: Any) -> dict[str, Any]:
    container: dict[str, Any] = {
        "name": "consulta",
        "image": OLD_IMAGE,
        "essential": True,
        "secrets": [{"name": "DATABASE_URL", "valueFrom": APP_DB_ARN}],
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": "/ecs/neurostack-staging/migrations",
                "awslogs-region": "us-east-1",
                "awslogs-stream-prefix": "consulta",
            },
        },
    }
    container.update(changes)
    return container


def _snapshot(snapshot_id: str, **changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "DBSnapshotIdentifier": snapshot_id,
        "DBInstanceIdentifier": "neurostack-staging-db",
        "SnapshotType": "manual",
        "Engine": "postgres",
        "Status": "available",
        "SnapshotCreateTime": SNAPSHOT_CREATED,
        "PercentProgress": 100,
        "Encrypted": True,
    }
    row.update(changes)
    return {"DBSnapshots": [row]}


def _running_task(
    arn: str, task_definition: str, digest: str, **changes: Any
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "taskArn": arn,
        "taskDefinitionArn": task_definition,
        "lastStatus": "RUNNING",
        "desiredStatus": "RUNNING",
        "startedBy": DEPLOYMENT_ID,
        "containers": [
            {"name": "app", "imageDigest": digest, "lastStatus": "RUNNING"},
            {
                "name": "otel",
                "imageDigest": "sha256:" + "7" * 64,
                "lastStatus": "RUNNING",
            },
        ],
    }
    task.update(changes)
    return task


def _events(*messages: str, token: str = "f/1") -> dict[str, Any]:
    return {
        "events": [
            {"timestamp": 1792371750000 + i, "message": m, "ingestionTime": 1}
            for i, m in enumerate(messages)
        ],
        "nextForwardToken": token,
        "nextBackwardToken": "b/1",
    }


def _read_revision_container(**changes: Any) -> dict[str, Any]:
    """Window 037's read revision: the migrations container, readback baked."""

    container = _migrations_container(
        "037",
        command=[
            "python",
            "-c",
            mw.read_program(mw.WINDOW_037, mw.WINDOW_037.pre_read),
        ],
    )
    container.update(changes)
    return container


def _read_revision_definition(**changes: Any) -> dict[str, Any]:
    return _one_container_definition(
        MIGRATIONS_READ_TD,
        "neurostack-staging-migrations",
        _read_revision_container(**changes),
    )


class World:
    """A recorded readback file for one window, one response per exact request."""

    def __init__(self, window: str = "036") -> None:
        self.name = window
        self.window = mw.WINDOWS[window]
        self.reads: dict[str, dict[str, Any]] = {}
        self.records: dict[str, dict[str, Any]] = {}
        self.captured_at = CAPTURED_PLAN
        self.service_td = SERVICE_TD_8 if window == "035" else SERVICE_TD_10
        self.migrations_td = MIGRATIONS_TD[window]
        self.snapshot_id = SNAPSHOT_ID[window]
        service_image = OLD_IMAGE if window == "035" else BRIDGE_IMAGE
        self.digest = OLD_DIGEST if window == "035" else BRIDGE_DIGEST
        self.put(
            "ecs",
            "DescribeServices",
            mw.describe_services_request(),
            _service(self.service_td),
        )
        self.put(
            "ecs",
            "ListTasks",
            mw.list_service_tasks_request(),
            {"taskArns": list(SERVICE_TASKS)},
        )
        self.put(
            "ecs",
            "DescribeTasks",
            mw.describe_tasks_request(SERVICE_TASKS),
            {
                "tasks": [
                    _running_task(arn, self.service_td, self.digest)
                    for arn in SERVICE_TASKS
                ],
                "failures": [],
            },
        )
        self.put(
            "ecs",
            "ListTasks",
            mw.list_service_tasks_request("STOPPED"),
            {"taskArns": []},
        )
        self.put(
            "ecs",
            "DescribeTaskDefinition",
            mw.describe_task_definition_request(self.service_td),
            _app_definition(self.service_td, service_image),
        )
        if window == "035":
            self.put(
                "ecr",
                "DescribeImages",
                mw.describe_image_by_tag_request("neurostack-staging", OLD_TAG),
                {
                    "imageDetails": [
                        {
                            "repositoryName": "neurostack-staging",
                            "imageDigest": OLD_DIGEST,
                        }
                    ]
                },
            )
        self.put(
            "ecs",
            "DescribeTaskDefinition",
            mw.describe_task_definition_request(self.migrations_td),
            _one_container_definition(
                self.migrations_td,
                "neurostack-staging-migrations",
                _migrations_container(window),
            ),
        )
        self.put(
            "ecr",
            "DescribeImages",
            mw.describe_image_by_digest_request("neurostack-staging", BRIDGE_DIGEST),
            {
                "imageDetails": [
                    {
                        "repositoryName": "neurostack-staging",
                        "imageDigest": BRIDGE_DIGEST,
                    }
                ]
            },
        )
        self.put(
            "ecs",
            "DescribeTaskDefinition",
            mw.describe_task_definition_request(READ_TD),
            _one_container_definition(
                READ_TD, "neurostack-staging-consulta", _read_container()
            ),
        )
        self.read_revision = MIGRATIONS_READ_TD if window == "037" else None
        if self.read_revision is not None:
            self.put(
                "ecs",
                "DescribeTaskDefinition",
                mw.describe_task_definition_request(self.read_revision),
                _read_revision_definition(),
            )
        self.put(
            "rds",
            "DescribeDBSnapshots",
            mw.describe_snapshot_request(self.snapshot_id),
            _snapshot(self.snapshot_id),
        )
        for params in (
            mw.list_family_tasks_request(mw.MIGRATIONS_FAMILY),
            mw.list_family_tasks_request(mw.MIGRATIONS_FAMILY, "STOPPED"),
            mw.list_family_tasks_request(mw.READ_FAMILY),
            mw.list_started_by_request(self.window.started_by),
            mw.list_started_by_request(self.window.read_started_by("pre")),
            mw.list_started_by_request(self.window.read_started_by("post")),
        ):
            self.put("ecs", "ListTasks", params, {"taskArns": []})

    # -- plumbing ----------------------------------------------------------

    def put(
        self, service: str, operation: str, params: dict[str, Any], response: Any
    ) -> None:
        self.reads[hook._read_key(service, operation, params)] = {
            "service": service,
            "operation": operation,
            "params": copy.deepcopy(params),
            "response": copy.deepcopy(response),
        }

    def drop(self, service: str, operation: str, params: dict[str, Any]) -> None:
        del self.reads[hook._read_key(service, operation, params)]

    def response(self, service: str, operation: str, params: dict[str, Any]) -> Any:
        return self.reads[hook._read_key(service, operation, params)]["response"]

    def document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schema_version": mw.READBACK_SCHEMA,
            "account_id": ACCOUNT,
            "region": "us-east-1",
            "window": self.name,
            "captured_at_utc": self.captured_at,
            "reads": copy.deepcopy(list(self.reads.values())),
        }
        document.update(copy.deepcopy(self.records))
        return document

    def write(self, path: Path) -> Path:
        path.write_text(json.dumps(self.document()), encoding="utf-8")
        return path

    def readback(
        self, tmp_path: Path, name: str = "readback.json"
    ) -> mw.WindowReadback:
        return mw.load_readback(self.write(tmp_path / name), self.window)

    def context(self, **changes: Any) -> mw.Context:
        arguments: dict[str, Any] = {
            "service_task_definition": self.service_td,
            "migrations_task_definition": self.migrations_td,
            "read_task_definition": READ_TD,
            "snapshot_id": self.snapshot_id,
            "migrations_read_task_definition": self.read_revision,
        }
        arguments.update(changes)
        return mw.make_context(self.window, **arguments)

    def args(self, tmp_path: Path, name: str = "readback.json") -> list[str]:
        path = self.write(tmp_path / name)
        return [
            "--window",
            self.name,
            "--readback-file",
            str(path),
            "--service-task-definition",
            self.service_td,
            "--migrations-task-definition",
            self.migrations_td,
            "--read-task-definition",
            READ_TD,
            "--snapshot-id",
            self.snapshot_id,
            *(
                ["--migrations-read-task-definition", self.read_revision]
                if self.read_revision is not None
                else []
            ),
        ]

    def put_pages(
        self,
        log: hook.LogTarget,
        task_id: str,
        pages: list[list[str]],
        *,
        terminal: bool = True,
    ) -> None:
        token: str | None = None
        for index, page in enumerate(pages, start=1):
            self.put(
                "logs",
                "GetLogEvents",
                hook.log_events_request(log, task_id, token),
                _events(*page, token=f"f/{index}"),
            )
            token = f"f/{index}"
        if terminal:
            self.put(
                "logs",
                "GetLogEvents",
                hook.log_events_request(log, task_id, token),
                _events(token=str(token)),
            )

    # -- recorded runs -----------------------------------------------------

    def read_context(self, tmp_path: Path, phase: str) -> mw.ReadContext:
        document, context = mw.evaluate_read_plan(
            self.readback(tmp_path, f"plan-{phase}.json"),
            self.context(),
            phase,
            now=NOW_DT,
        )
        assert context is not None, document
        return context

    def record_read(
        self,
        tmp_path: Path,
        phase: str,
        lines: list[str] | None = None,
        *,
        exit_code: Any = 0,
        attempt: int = 1,
        params: dict[str, Any] | None = None,
        created: str | None = None,
        stopped: str | None = None,
        task: dict[str, Any] | None = None,
        pages: list[list[str]] | None = None,
    ) -> dict[str, Any]:
        context = self.read_context(tmp_path, phase)
        rendered = params or mw.build_read_request(
            self.window, phase, context.network, context.target, attempt=attempt
        )
        arn = PRE_TASK if phase == "pre" else POST_TASK
        task_id = PRE_TASK_ID if phase == "pre" else POST_TASK_ID
        created = created or (PRE_CREATED if phase == "pre" else POST_CREATED)
        stopped = stopped or (PRE_STOPPED if phase == "pre" else POST_STOPPED)
        started_by = self.window.read_started_by(phase)
        self.records[f"{phase}_read"] = {
            "params": rendered,
            "response": {
                "tasks": [
                    {
                        "taskArn": arn,
                        "taskDefinitionArn": context.target.task_definition_arn,
                        "startedBy": started_by,
                        "lastStatus": "PROVISIONING",
                        "createdAt": created,
                    }
                ],
                "failures": [],
            },
        }
        container: dict[str, Any] = {
            "name": context.target.container,
            "lastStatus": "STOPPED",
        }
        if exit_code is not None:
            container["exitCode"] = exit_code
        described = task or {
            "taskArn": arn,
            "taskDefinitionArn": context.target.task_definition_arn,
            "startedBy": started_by,
            "lastStatus": "STOPPED",
            "desiredStatus": "STOPPED",
            "createdAt": created,
            "stoppedAt": stopped,
            "stopCode": "EssentialContainerExited",
            "containers": [container],
            "overrides": {
                "containerOverrides": [
                    (
                        {"name": context.target.container}
                        if context.target.baked
                        else {
                            "name": context.target.container,
                            "command": rendered["overrides"]["containerOverrides"][0][
                                "command"
                            ],
                        }
                    )
                ]
            },
        }
        self.put(
            "ecs",
            "DescribeTasks",
            mw.describe_tasks_request([arn]),
            {"tasks": [described], "failures": []},
        )
        if lines is None:
            key = (
                GOOD_PRE_READ[self.name]
                if phase == "pre"
                else GOOD_POST_READ[self.name]
            )
            lines = READS[key]["lines"]
        self.put_pages(
            context.target.log, task_id, pages if pages is not None else [list(lines)]
        )
        return rendered

    def record_run(
        self,
        tmp_path: Path,
        lines: list[str] | None = None,
        *,
        exit_code: Any = 0,
        attempt: int = 1,
        params: dict[str, Any] | None = None,
        task: dict[str, Any] | None = None,
        pages: list[list[str]] | None = None,
        response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """A ready plan, then the migration run (default: the window's success log)."""

        rendered = params or render_run(self, tmp_path, attempt=attempt)
        self.records["migration"] = {
            "params": rendered,
            "response": response
            or {
                "tasks": [
                    {
                        "taskArn": RUN_TASK,
                        "taskDefinitionArn": self.migrations_td,
                        "startedBy": self.window.started_by,
                        "lastStatus": "PROVISIONING",
                        "createdAt": RUN_CREATED,
                    }
                ],
                "failures": [],
            },
        }
        self.finish_run(lines, exit_code=exit_code, task=task, pages=pages)
        return rendered

    def finish_run(
        self,
        lines: list[str] | None = None,
        *,
        exit_code: Any = 0,
        task: dict[str, Any] | None = None,
        pages: list[list[str]] | None = None,
    ) -> None:
        self.captured_at = CAPTURED_VERIFY
        container: dict[str, Any] = {"name": "migrations", "lastStatus": "STOPPED"}
        if exit_code is not None:
            container["exitCode"] = exit_code
        described = task or {
            "taskArn": RUN_TASK,
            "taskDefinitionArn": self.migrations_td,
            "startedBy": self.window.started_by,
            "lastStatus": "STOPPED",
            "desiredStatus": "STOPPED",
            "createdAt": RUN_CREATED,
            "stoppedAt": RUN_STOPPED,
            "stopCode": "EssentialContainerExited",
            "containers": [container],
            "overrides": {
                "containerOverrides": [{"name": "migrations"}],
                "inferenceAcceleratorOverrides": [],
            },
        }
        self.put(
            "ecs",
            "DescribeTasks",
            mw.describe_tasks_request([RUN_TASK]),
            {"tasks": [described], "failures": []},
        )
        if lines is None:
            lines = ALEMBIC[f"success_{self.name}"]["lines"]
        self.put_pages(
            MIGRATIONS_LOG, RUN_TASK_ID, pages if pages is not None else [list(lines)]
        )


MIGRATIONS_LOG = hook.LogTarget(
    group="/ecs/neurostack-staging/migrations",
    stream_prefix="migrations",
    container="migrations",
)


# The live bridge revision 9 the reviewed manifest first re-pinned the
# rollback target to (read back 2026-10-22), and its app image digest.
LIVE_REVISION_9 = SERVICE_TD_9
LIVE_REVISION_9_IMAGE = (
    f"{REGISTRY}/neurostack-staging@sha256:"
    "18358ce728c465ffd0d9cf6d0e9cf4641c32f6bab9dfa2905729c5495bcf7f24"
)
# The worker-flag revision 10 it is re-pinned to since (revision 9 with the
# worker flag 'true', on the same bridge image digest).
LIVE_REVISION_10 = SERVICE_TD_10
LIVE_REVISION_10_IMAGE = LIVE_REVISION_9_IMAGE


def initial_manifest(tmp_path: Path) -> Path:
    """The manifest as it read before the re-pin: revision 8 with its note.

    The reviewed manifest itself is re-pinned to revision 10 (8 to 9, then 9
    to 10; no ``repin`` block), so every 036 and 037 plan below reads it
    through the default.
    """

    text = MANIFEST.read_text(encoding="utf-8")
    start = text.index("rollback_target:\n")
    end = text.index("\nsecrets:\n")
    initial = (
        "rollback_target:\n"
        "  revision: 8\n"
        "  effective_after: initial_d3_planning_2026_10_18\n"
        "  repin:\n"
        "    revision: 9\n"
        "    effective_after: bridge_revision_9_serving_verified_and_observation_closed\n"
        "    before: migration_036\n"
        "    why: Revision 8 reads legacy columns that migration 036 drops.\n"
    )
    path = tmp_path / "manifest-initial.yaml"
    path.write_text(text[:start] + initial + text[end:], encoding="utf-8")
    return path


def bridge_manifest(tmp_path: Path) -> Path:
    """The manifest at revision 9 with its pending re-pin to revision 10."""

    text = MANIFEST.read_text(encoding="utf-8")
    start = text.index("rollback_target:\n")
    end = text.index("\nsecrets:\n")
    bridge = (
        "rollback_target:\n"
        "  revision: 9\n"
        "  effective_after: bridge_revision_9_serving_verified_and_observation_closed\n"
        "  repin:\n"
        "    revision: 10\n"
        "    effective_after: "
        "worker_flag_revision_10_serving_verified_and_observation_closed\n"
        "    before: app_image_bump_deployment\n"
        "    why: Revision 9 carries BILLING_FACT_WORKER_ENABLED 'false'.\n"
    )
    path = tmp_path / "manifest-bridge.yaml"
    path.write_text(text[:start] + bridge + text[end:], encoding="utf-8")
    return path


def plan(
    world: World,
    tmp_path: Path,
    *,
    now: datetime = NOW_DT,
    manifest: Path | None = None,
) -> dict[str, Any]:
    document, _context = mw.evaluate_plan(
        world.readback(tmp_path, "plan.json"),
        world.context(),
        now=now,
        manifest=manifest,
    )
    return document


def render_run(world: World, tmp_path: Path, *, attempt: int = 1) -> dict[str, Any]:
    run = mw.run_context_for(
        world.readback(tmp_path, "render.json"), world.context(), now=NOW_DT
    )
    return mw.build_run_task_request(
        world.window,
        run.network,
        run.migrations_task_definition,
        run.snapshot_id,
        attempt=attempt,
    )


def ready_world(tmp_path: Path, window: str = "036") -> World:
    world = World(window)
    world.record_read(tmp_path, "pre")
    return world


def verify(
    world: World, tmp_path: Path, *, task_arn: str | None = None
) -> dict[str, Any]:
    return mw.verify_result(
        world.readback(tmp_path, "verify.json"), world.context(), task_arn=task_arn
    )


def verify_read(world: World, tmp_path: Path, phase: str) -> dict[str, Any]:
    return mw.verify_read(
        world.readback(tmp_path, f"verify-{phase}.json"), world.context(), phase
    )


def run_cli(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    code = mw.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def completed(
    tmp_path: Path, window: str = "036", lines: list[str] | None = None, **run: Any
) -> World:
    world = ready_world(tmp_path, window)
    world.record_run(tmp_path, lines, **run)
    return world


def _version_lines(lines: list[str], version: str) -> list[str]:
    out = []
    for line in lines:
        document = json.loads(line)
        if document["kind"] == "version":
            document["values"] = [version]
        out.append(json.dumps(document, separators=(",", ":")))
    return out


def _rows_changed(lines: list[str], index: int, values: list[Any]) -> list[str]:
    out = []
    seen = -1
    for line in lines:
        document = json.loads(line)
        if document["kind"] == "row":
            seen += 1
            if seen == index:
                document["values"] = values
        out.append(json.dumps(document, separators=(",", ":")))
    return out


# ---------------------------------------------------------------------------
# Static contract: the window table, emitted operations, purity
# ---------------------------------------------------------------------------


def test_one_constant_per_window_and_nothing_else() -> None:
    assert list(mw.WINDOWS) == ["035", "036", "037"]
    assert mw.WINDOWS["035"] is mw.WINDOW_035
    assert mw.WINDOWS["036"] is mw.WINDOW_036
    assert mw.WINDOWS["037"] is mw.WINDOW_037
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    constructed = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "MigrationWindow"
    ]
    assert len(constructed) == 3
    with pytest.raises(TypeError):
        mw.WINDOWS["038"] = mw.WINDOW_037  # type: ignore[index]


@pytest.mark.parametrize(
    "name,before,after,chain,prefix,sql,rule,role,steps",
    [
        (
            "035",
            "032",
            "035",
            ("032", "033", "034", "035"),
            "pre-033-035-",
            "scripts/migration_033_035_zero_reads.sql",
            "zero_counts",
            "app",
            {
                "register": "S10",
                "snapshot": "S11",
                "pre_read": "S12",
                "run": "S12",
                "verify": "S13",
                "post_read": "S13",
            },
        ),
        (
            "036",
            "035",
            "036",
            ("035", "036"),
            "pre-036-",
            "scripts/migration_036_preflight.sql",
            "zero_counts",
            "app",
            {
                "register": "S20",
                "snapshot": "S20",
                "pre_read": "S21",
                "run": "S21",
                "verify": "S22",
                "post_read": "S22",
            },
        ),
        (
            "037",
            "036",
            "037",
            ("036", "037"),
            "pre-037-",
            "scripts/migration_037_readback.sql",
            "verdict_ok",
            "migrations",
            {
                "register": "S23",
                "snapshot": "S23",
                "pre_read": "S24",
                "run": "S24",
                "verify": "S24",
                "post_read": "S24",
            },
        ),
    ],
)
def test_each_window_pins_its_command_versions_snapshot_readback_and_steps(
    name: str,
    before: str,
    after: str,
    chain: tuple[str, ...],
    prefix: str,
    sql: str,
    rule: str,
    role: str,
    steps: dict[str, str],
) -> None:
    window = mw.WINDOWS[name]
    assert window.command == ("alembic", "upgrade", after)
    assert window.before == before and window.after == after
    assert window.chain == chain
    assert window.upgrades == tuple(zip(chain, chain[1:]))
    assert window.snapshot_prefix == prefix
    assert window.snapshot_pattern == prefix + "<date>"
    assert window.pre_read.sql_file == sql
    assert window.pre_read.rule == rule
    assert window.pre_read.role == role
    assert window.pre_read.expected_version == before
    assert window.post_read.expected_version == after
    assert window.post_read.role == "app"
    assert dict(window.plan_steps) == steps
    assert window.started_by == f"neurostack-migration-window-{name}"
    assert window.image_rule == (
        mw.IMAGE_RULE_BRIDGE if name == "035" else mw.IMAGE_RULE_DEPLOYED
    )
    assert window.rollback_target_revision == (None if name == "035" else 10)
    assert (ROOT / sql).is_file()


def test_the_pinned_window_is_035_and_no_subcommand_defaults_a_window() -> None:
    assert mw.PINNED_WINDOW == "035"
    assert mw.WINDOWS[mw.PINNED_WINDOW].after == "035"
    parser = mw._parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    for name, sub in subparsers.choices.items():
        windows = [a for a in sub._actions if "--window" in a.option_strings]
        if name == "windows":
            assert windows == []
            continue
        assert len(windows) == 1, name
        assert windows[0].required is True, name
        assert windows[0].default is None, name
        assert tuple(windows[0].choices) == ("035", "036", "037"), name


def test_the_window_rows_agree_with_the_alembic_revision_chain() -> None:
    declared: dict[str, str] = {}
    for path in (ROOT / "alembic" / "versions").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        revision = re.search(r'^revision: str = "([^"]+)"$', text, re.M)
        down = re.search(r'^down_revision: Union\[str, None\] = "([^"]+)"$', text, re.M)
        if revision and down:
            declared[revision.group(1)] = down.group(1)
    for window in mw.WINDOWS.values():
        for older, newer in window.upgrades:
            assert declared[newer] == older, (window.name, older, newer)
    # 038 (the additive credit schema, neurostack-credit-system Group 1a;
    # owner sign-off 2026-10-26), 039 (inventory), 040 (additional copies) and
    # 041 (lifecycle, legal hold, purge and roles, Group 5) are the last
    # written revisions and have no production window yet: their ASAAS
    # migration window is an entry condition of that wish's Group 8.
    # No later revision can exist unseen.
    assert declared["038"] == "037"
    assert declared["039"] == "038"
    assert declared["040"] == "039"
    assert declared["041"] == "040"
    assert "042" not in declared
    assert not [rev for rev, down in declared.items() if down == "041"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("command", ("alembic", "upgrade", "head")),
        ("command", ("alembic", "upgrade", "heads")),
        ("command", ("alembic", "upgrade", "035:037")),
        ("command", ("alembic", "upgrade", "+1")),
        ("command", ("alembic", "upgrade", "037")),
        ("before", "033"),
        ("after", "036"),
        ("chain", ("032", "033", "035")),
        ("snapshot_prefix", "pre-035-"),
        ("snapshot_prefix", "pre-033-035"),
        ("started_by", "neurostack-migration-window-036"),
        ("image_rule", mw.IMAGE_RULE_DEPLOYED),
        ("rollback_target_revision", 9),
        ("plan_steps", (("register", "S10"),)),
    ],
)
def test_an_inconsistent_window_table_cannot_load(
    field: str, value: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = dataclasses.replace(mw.WINDOW_035, **{field: value})
    table = dict(mw.WINDOWS) | {"035": broken}
    monkeypatch.setattr(mw, "WINDOWS", table)
    with pytest.raises(mw.WindowError, match="window_table"):
        mw._assert_window_table()


def test_a_default_or_extra_window_cannot_load(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mw, "WINDOWS", dict(mw.WINDOWS) | {"head": mw.WINDOW_037})
    with pytest.raises(mw.WindowError, match="window_table_names"):
        mw._assert_window_table()
    monkeypatch.setattr(mw, "WINDOWS", {"035": mw.WINDOW_035, "037": mw.WINDOW_037})
    with pytest.raises(mw.WindowError, match="window_table_names"):
        mw._assert_window_table()


def test_emitted_operations_are_exactly_the_brief_and_never_a_mutation() -> None:
    assert mw.EMITTED_OPERATIONS == frozenset(
        {
            ("ecs", "RunTask"),
            ("ecs", "DescribeTasks"),
            ("ecs", "ListTasks"),
            ("ecs", "DescribeServices"),
            ("ecs", "DescribeTaskDefinition"),
            ("logs", "GetLogEvents"),
            ("rds", "CreateDBSnapshot"),
            ("rds", "DescribeDBSnapshots"),
            ("ecr", "DescribeImages"),
        }
    )
    emitted = {operation for _service, operation in mw.EMITTED_OPERATIONS}
    assert not emitted & mw.FORBIDDEN_OPERATIONS
    assert {
        "UpdateService",
        "RegisterTaskDefinition",
        "DeregisterTaskDefinition",
        "StopTask",
        "DeleteDBSnapshot",
        "RestoreDBInstanceFromDBSnapshot",
    } <= mw.FORBIDDEN_OPERATIONS
    for _service, operation in mw.CONSUMED_READS:
        assert operation.startswith(("Describe", "List", "Get"))


@pytest.mark.parametrize(
    "service,operation",
    [
        ("ecs", "UpdateService"),
        ("ecs", "RegisterTaskDefinition"),
        ("ecs", "StopTask"),
        ("rds", "DeleteDBSnapshot"),
        ("rds", "RestoreDBInstanceFromDBSnapshot"),
        ("ecs", "CreateService"),
        ("secretsmanager", "GetSecretValue"),
    ],
)
def test_a_mutation_can_never_be_rendered(service: str, operation: str) -> None:
    with pytest.raises(mw.WindowError, match="operation_not_emittable"):
        mw._request_row(service, operation, {})


def test_module_source_never_reaches_a_network_or_aws_sdk() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not imported & {
        "boto3",
        "botocore",
        "subprocess",
        "socket",
        "urllib",
        "http",
        "httpx",
        "requests",
        "src",
        "asyncpg",
        "sqlalchemy",
    }
    assert "os.system" not in source
    assert not UPGRADE_HEAD.search(source)
    # The read program imports asyncpg and SQLAlchemy only inside the task.
    assert "import asyncpg" in mw._READ_PROGRAM_BODY


def test_target_comes_from_the_hook_and_the_planner_shares_the_family() -> None:
    assert mw.CLUSTER == hook.CLUSTER == "neurostack-staging"
    assert mw.SERVICE == hook.SERVICE
    assert mw.REGION == "us-east-1" and mw.ACCOUNT_ID == ACCOUNT
    assert (
        wiring.MIGRATIONS_FAMILY
        == mw.MIGRATIONS_FAMILY
        == "neurostack-staging-migrations"
    )
    assert wiring.MIGRATIONS_TASK_SOURCE_REVISION == mw.MIGRATIONS_SOURCE_REVISION == 3
    assert wiring.MIGRATIONS_DATABASE_SECRET_NAME == mw.MIGRATIONS_DATABASE_SECRET
    assert wiring.MIGRATION_WINDOWS is mw.WINDOWS
    assert wiring.WORKER_FLAG_REVISION == mw.WINDOW_036.rollback_target_revision
    assert wiring.WORKER_FLAG_REVISION == mw.WINDOW_037.rollback_target_revision


def test_policy_values_and_watch_budgets() -> None:
    assert mw.READBACK_MAX_AGE_SECONDS == 600
    assert mw.READBACK_MAX_FUTURE_SECONDS == 60
    assert mw.SNAPSHOT_MAX_AGE_SECONDS == 3600
    assert mw.PRE_READ_MAX_AGE_SECONDS == 900
    assert mw.MIGRATION_WATCH_DEADLINE_SECONDS > (
        mw.START_BUDGET_SECONDS
        + mw.MIGRATION_RUN_BUDGET_SECONDS
        + mw.STOP_BUDGET_SECONDS
    )
    assert mw.READ_WATCH_DEADLINE_SECONDS > (
        mw.START_BUDGET_SECONDS
        + mw.READ_CONNECT_TIMEOUT_SECONDS
        + mw.READ_STATEMENT_TIMEOUT_SECONDS
        + mw.STOP_BUDGET_SECONDS
    )
    assert mw.OVERRIDES_BUDGET_CHARS < mw.OVERRIDES_MAX_CHARS == 8192
    assert mw.MAX_ATTEMPT == 20


def test_windows_subcommand_prints_the_table(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run_cli(["windows"], capsys)
    assert code == 0, err
    rows = json.loads(out)["windows"]
    assert [row["window"] for row in rows] == ["035", "036", "037"]
    assert [row["command"] for row in rows] == [
        ["alembic", "upgrade", "035"],
        ["alembic", "upgrade", "036"],
        ["alembic", "upgrade", "037"],
    ]
    assert [row["snapshot_name_pattern"] for row in rows] == [
        "pre-033-035-<date>",
        "pre-036-<date>",
        "pre-037-<date>",
    ]
    assert UPGRADE_HEAD.search(out) is None


# ---------------------------------------------------------------------------
# plan: ready per window, every refusal
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window", ["035", "036", "037"])
def test_plan_is_ready_for_each_window(window: str, tmp_path: Path) -> None:
    world = ready_world(tmp_path, window)
    document = plan(world, tmp_path)
    assert document["status"] == "ready", document["blocks"]
    assert document["blocks"] == [] and document["next_requests"] == []
    assert document["window"] == window
    assert document["pre_read"]["alembic_version"] == [mw.WINDOWS[window].before]
    assert f"alembic_version_is_{mw.WINDOWS[window].before}" in document["checks"]
    assert document["expected_image_digest"] == (
        OLD_DIGEST if window == "035" else BRIDGE_DIGEST
    )
    assert document["run_task"]["command_override"] == "none"
    assert document["run_task"]["baked_command"] == ["alembic", "upgrade", window]
    for check in (
        "readback_fresh",
        "deployment_completed",
        "service_tasks_all_on_expected_image_digest",
        "no_service_task_still_stopping",
        "migrations_command_is_this_window",
        "migrations_image_in_ecr",
        "snapshot_available",
        "snapshot_recent",
        "no_migrations_task_running",
        "no_window_task_running",
        "no_read_family_task_running",
        "pre_read_ok",
        "pre_read_fresh",
        "pre_read_after_snapshot",
    ):
        assert check in document["checks"], check
    if window != "035":
        assert "migrations_image_is_the_deployed_image" in document["checks"]
        assert "rollback_target_repinned" in document["checks"]
        assert "rollback_target_revision_active_digest_pinned" in document["checks"]
    else:
        assert "service_image_tag_resolved_in_ecr" in document["checks"]


def _blocks(world: World, tmp_path: Path, **kwargs: Any) -> list[str]:
    document = plan(world, tmp_path, **kwargs)
    assert document["status"] != "ready"
    return list(document["blocks"])


@pytest.mark.parametrize("window", ["035", "036", "037"])
def test_plan_refuses_a_wrong_before_version(window: str, tmp_path: Path) -> None:
    for version in ("032", "035", "036", "037", "034"):
        if version == mw.WINDOWS[window].before:
            continue
        world = World(window)
        lines = _version_lines(READS[GOOD_PRE_READ[window]]["lines"], version)
        world.record_read(tmp_path, "pre", lines)
        blocks = _blocks(world, tmp_path)
        assert "pre_read_not_ok" in blocks, (version, blocks)


@pytest.mark.parametrize(
    "case,snapshot,reason",
    [
        ("creating", {"Status": "creating"}, "snapshot_not_available"),
        ("failed", {"Status": "failed"}, "snapshot_not_available"),
        (
            "too_old",
            {"SnapshotCreateTime": "2026-10-22T09:29:59+00:00"},
            "snapshot_too_old",
        ),
        (
            "future",
            {"SnapshotCreateTime": "2026-10-22T10:31:01+00:00"},
            "snapshot_too_old",
        ),
        ("no_time", {"SnapshotCreateTime": None}, "snapshot_create_time_invalid"),
        (
            "other_db",
            {"DBInstanceIdentifier": "other-db"},
            "snapshot_identity_mismatch",
        ),
        ("automated", {"SnapshotType": "automated"}, "snapshot_identity_mismatch"),
        ("engine", {"Engine": "mysql"}, "snapshot_identity_mismatch"),
        (
            "other_name",
            {"DBSnapshotIdentifier": "pre-036-2026-10-21"},
            "snapshot_identity_mismatch",
        ),
    ],
)
def test_plan_refuses_an_unusable_snapshot(
    case: str, snapshot: dict[str, Any], reason: str, tmp_path: Path
) -> None:
    world = ready_world(tmp_path)
    world.put(
        "rds",
        "DescribeDBSnapshots",
        mw.describe_snapshot_request(world.snapshot_id),
        _snapshot(world.snapshot_id, **snapshot),
    )
    assert reason in _blocks(world, tmp_path)


def test_plan_accepts_the_snapshot_age_boundary(tmp_path: Path) -> None:
    world = ready_world(tmp_path)
    world.put(
        "rds",
        "DescribeDBSnapshots",
        mw.describe_snapshot_request(world.snapshot_id),
        _snapshot(world.snapshot_id, SnapshotCreateTime="2026-10-22T09:30:00+00:00"),
    )
    document = plan(world, tmp_path)
    assert "snapshot_too_old" not in document["blocks"]
    assert "snapshot_recent" in document["checks"]


@pytest.mark.parametrize(
    "response,reason",
    [
        ({"DBSnapshots": []}, "snapshot_not_found"),
        ({"__error_code__": "DBSnapshotNotFound"}, "snapshot_not_found"),
        ({"DBSnapshots": [{}, {}]}, "snapshot_not_found"),
        ({**_snapshot("pre-036-2026-10-22"), "Marker": "next"}, "snapshot_not_found"),
    ],
)
def test_plan_refuses_a_missing_snapshot(
    response: dict[str, Any], reason: str, tmp_path: Path
) -> None:
    world = ready_world(tmp_path)
    world.put(
        "rds",
        "DescribeDBSnapshots",
        mw.describe_snapshot_request(world.snapshot_id),
        response,
    )
    assert reason in _blocks(world, tmp_path)


@pytest.mark.parametrize(
    "window,value",
    [
        ("036", "pre-033-035-2026-10-22"),
        ("036", "pre-037-2026-10-22"),
        ("036", "pre-036-2026-13-01"),
        ("036", "pre-036-2026-10-22-2"),
        ("036", "pre-036-20261022"),
        ("036", "PRE-036-2026-10-22"),
        ("035", "pre-035-2026-10-22"),
        ("035", "pre-033-2026-10-22"),
        ("037", "pre-036-2026-10-22"),
        ("037", "snapshot"),
        ("037", ""),
    ],
)
def test_a_snapshot_name_outside_the_window_pattern_is_refused(
    window: str, value: str
) -> None:
    with pytest.raises(mw.WindowError, match="snapshot_id_invalid"):
        mw.validate_snapshot_id(mw.WINDOWS[window], value)


@pytest.mark.parametrize("name_day", ["2026-10-20", "2026-10-23"])
def test_a_snapshot_named_for_another_day_is_refused(
    name_day: str, tmp_path: Path
) -> None:
    world = ready_world(tmp_path)
    world.snapshot_id = f"pre-036-{name_day}"
    world.put(
        "rds",
        "DescribeDBSnapshots",
        mw.describe_snapshot_request(world.snapshot_id),
        _snapshot(world.snapshot_id),
    )
    assert "snapshot_name_date_mismatch" in _blocks(world, tmp_path)


def test_a_snapshot_named_the_day_before_its_creation_is_accepted(
    tmp_path: Path,
) -> None:
    """Rendered at 23:59 UTC, created after midnight."""

    world = ready_world(tmp_path)
    world.snapshot_id = "pre-036-2026-10-21"
    world.put(
        "rds",
        "DescribeDBSnapshots",
        mw.describe_snapshot_request(world.snapshot_id),
        _snapshot(world.snapshot_id),
    )
    assert plan(world, tmp_path)["status"] == "ready"


@pytest.mark.parametrize(
    "params_of,name",
    [
        (
            lambda w: mw.list_family_tasks_request(mw.MIGRATIONS_FAMILY),
            "migrations_task_running",
        ),
        (
            lambda w: mw.list_started_by_request(w.window.started_by),
            "window_task_running",
        ),
        (
            lambda w: mw.list_family_tasks_request(mw.READ_FAMILY),
            "read_family_task_running",
        ),
    ],
)
def test_single_flight_refuses_while_a_task_may_run(
    params_of: Callable[[World], dict[str, Any]], name: str, tmp_path: Path
) -> None:
    world = ready_world(tmp_path)
    world.put("ecs", "ListTasks", params_of(world), {"taskArns": [RUN_TASK]})
    document = plan(world, tmp_path)
    assert name in document["blocks"]
    world.put(
        "ecs", "ListTasks", params_of(world), {"taskArns": [], "nextToken": "more"}
    )
    assert name.replace("_running", "_listing_incomplete") in _blocks(world, tmp_path)
    world.put(
        "ecs",
        "ListTasks",
        params_of(world),
        {"__error_code__": "ClusterNotFoundException"},
    )
    assert name.replace("_running", "_listing_unreadable") in _blocks(world, tmp_path)


def test_single_flight_listings_use_the_documented_filters() -> None:
    assert mw.list_started_by_request("x") == {
        "cluster": "neurostack-staging",
        "startedBy": "x",
    }
    assert mw.list_family_tasks_request(mw.MIGRATIONS_FAMILY) == {
        "cluster": "neurostack-staging",
        "family": "neurostack-staging-migrations",
    }
    assert mw.list_service_tasks_request() == {
        "cluster": "neurostack-staging",
        "serviceName": "neurostack-staging-app",
    }


@pytest.mark.parametrize("window", ["035", "036", "037"])
@pytest.mark.parametrize("which", [0, 1])
def test_plan_refuses_an_image_digest_mismatch_on_one_task(
    window: str, which: int, tmp_path: Path
) -> None:
    world = ready_world(tmp_path, window)
    tasks = [
        _running_task(arn, world.service_td, world.digest) for arn in SERVICE_TASKS
    ]
    tasks[which]["containers"][0]["imageDigest"] = "sha256:" + "9" * 64
    world.put(
        "ecs",
        "DescribeTasks",
        mw.describe_tasks_request(SERVICE_TASKS),
        {"tasks": tasks, "failures": []},
    )
    assert "service_task_image_digest_mismatch" in _blocks(world, tmp_path)


@pytest.mark.parametrize(
    "change,reason",
    [
        (
            {"taskDefinitionArn": SERVICE_TD_8.replace(":8", ":7")},
            "service_task_not_on_expected_revision",
        ),
        ({"lastStatus": "PENDING"}, "service_task_not_on_expected_revision"),
        ({"desiredStatus": "STOPPED"}, "service_task_not_on_expected_revision"),
        (
            {"containers": [{"name": "otel", "imageDigest": BRIDGE_DIGEST}]},
            "service_task_image_digest_mismatch",
        ),
    ],
)
def test_plan_refuses_a_service_task_off_the_expected_revision(
    change: dict[str, Any], reason: str, tmp_path: Path
) -> None:
    world = ready_world(tmp_path)
    tasks = [
        _running_task(arn, world.service_td, world.digest) for arn in SERVICE_TASKS
    ]
    tasks[1].update(change)
    world.put(
        "ecs",
        "DescribeTasks",
        mw.describe_tasks_request(SERVICE_TASKS),
        {"tasks": tasks, "failures": []},
    )
    assert reason in _blocks(world, tmp_path)


def test_plan_refuses_a_task_count_that_is_not_the_desired_count(
    tmp_path: Path,
) -> None:
    world = ready_world(tmp_path)
    extra = SERVICE_TASKS + [TASK_PREFIX + "c" * 32]
    world.put("ecs", "ListTasks", mw.list_service_tasks_request(), {"taskArns": extra})
    world.put(
        "ecs",
        "DescribeTasks",
        mw.describe_tasks_request(extra),
        {
            "tasks": [_running_task(a, world.service_td, world.digest) for a in extra],
            "failures": [],
        },
    )
    assert "service_task_count_mismatch" in _blocks(world, tmp_path)


def test_plan_refuses_an_old_task_still_stopping(tmp_path: Path) -> None:
    world = ready_world(tmp_path)
    old = TASK_PREFIX + "d" * 32
    world.put(
        "ecs",
        "ListTasks",
        mw.list_service_tasks_request("STOPPED"),
        {"taskArns": [old]},
    )
    stopping = _running_task(
        old,
        SERVICE_TD_8,
        OLD_DIGEST,
        lastStatus="DEACTIVATING",
        desiredStatus="STOPPED",
    )
    world.put(
        "ecs",
        "DescribeTasks",
        mw.describe_tasks_request([old]),
        {"tasks": [stopping], "failures": []},
    )
    assert "service_task_still_stopping" in _blocks(world, tmp_path)
    stopping["lastStatus"] = "STOPPED"
    world.put(
        "ecs",
        "DescribeTasks",
        mw.describe_tasks_request([old]),
        {"tasks": [stopping], "failures": []},
    )
    assert plan(world, tmp_path)["status"] == "ready"


@pytest.mark.parametrize("window", ["036", "037"])
def test_036_and_037_need_the_deployed_image_in_the_migrations_revision(
    window: str, tmp_path: Path
) -> None:
    world = ready_world(tmp_path, window)
    other = "sha256:" + "4" * 64
    container = _migrations_container(
        window, image=f"{REGISTRY}/neurostack-staging@{other}"
    )
    world.put(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(world.migrations_td),
        _one_container_definition(
            world.migrations_td, "neurostack-staging-migrations", container
        ),
    )
    world.put(
        "ecr",
        "DescribeImages",
        mw.describe_image_by_digest_request("neurostack-staging", other),
        {"imageDetails": [{"imageDigest": other}]},
    )
    blocks = _blocks(world, tmp_path)
    assert "migrations_image_differs_from_deployed_image" in blocks
    assert "service_task_image_digest_mismatch" in blocks


@pytest.mark.parametrize("window", ["036", "037"])
def test_036_and_037_refuse_a_service_image_by_tag(window: str, tmp_path: Path) -> None:
    world = ready_world(tmp_path, window)
    world.put(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(world.service_td),
        _app_definition(world.service_td, OLD_IMAGE),
    )
    assert "service_image_not_digest_pinned" in _blocks(world, tmp_path)


def test_035_resolves_the_tagged_service_image_and_refuses_an_unknown_tag(
    tmp_path: Path,
) -> None:
    world = ready_world(tmp_path, "035")
    params = mw.describe_image_by_tag_request("neurostack-staging", OLD_TAG)
    world.put(
        "ecr", "DescribeImages", params, {"__error_code__": "ImageNotFoundException"}
    )
    assert "service_image_tag_not_found_in_ecr" in _blocks(world, tmp_path)
    world.put(
        "ecr",
        "DescribeImages",
        params,
        {"imageDetails": [{"imageDigest": "sha256:" + "9" * 64}]},
    )
    assert "service_task_image_digest_mismatch" in _blocks(world, tmp_path)


def test_plan_refuses_a_migrations_image_gone_from_ecr(tmp_path: Path) -> None:
    world = ready_world(tmp_path, "035")
    world.put(
        "ecr",
        "DescribeImages",
        mw.describe_image_by_digest_request("neurostack-staging", BRIDGE_DIGEST),
        {"__error_code__": "ImageNotFoundException"},
    )
    assert "migrations_image_not_found_in_ecr" in _blocks(world, tmp_path)


def _migrations_definition_with(world: World, **changes: Any) -> None:
    container = _migrations_container(world.name, **changes.pop("container", {}))
    response = _one_container_definition(
        world.migrations_td, "neurostack-staging-migrations", container
    )
    response["taskDefinition"].update(changes)
    world.put(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(world.migrations_td),
        response,
    )


@pytest.mark.parametrize(
    "changes,reason",
    [
        (
            {"container": {"command": ["alembic", "upgrade", "head"]}},
            "migrations_command_not_this_window",
        ),
        (
            {"container": {"command": ["alembic", "upgrade", "035"]}},
            "migrations_command_not_this_window",
        ),
        (
            {"container": {"command": ["alembic", "upgrade", "037"]}},
            "migrations_command_not_this_window",
        ),
        (
            {"container": {"command": ["sh", "-c", "alembic upgrade 036"]}},
            "migrations_command_not_this_window",
        ),
        ({"container": {"command": None}}, "migrations_command_not_this_window"),
        (
            {"container": {"entryPoint": ["sh", "-c"]}},
            "migrations_container_entry_point_unexpected",
        ),
        (
            {
                "container": {
                    "image": BRIDGE_IMAGE.replace("@sha256:" + "2" * 64, ":latest")
                }
            },
            "migrations_image_not_digest_pinned",
        ),
        (
            {
                "container": {
                    "secrets": [{"name": "DATABASE_URL", "valueFrom": APP_DB_ARN}]
                }
            },
            "migrations_database_secret_mismatch",
        ),
        (
            {"container": {"environment": [{"name": "DATABASE_URL", "value": "x"}]}},
            "migrations_database_secret_mismatch",
        ),
        (
            {"container": {"logConfiguration": {"logDriver": "json-file"}}},
            "migrations_log_configuration_unusable",
        ),
        ({"container": {"essential": False}}, "migrations_container_not_essential"),
        ({"status": "INACTIVE"}, "migrations_task_definition_not_active"),
        ({"networkMode": "bridge"}, "migrations_task_definition_not_fargate_awsvpc"),
        ({"family": "other"}, "migrations_task_definition_identity_mismatch"),
        ({"revision": 99}, "migrations_task_definition_identity_mismatch"),
    ],
)
def test_plan_refuses_a_migrations_revision_not_made_for_this_window(
    changes: dict[str, Any], reason: str, tmp_path: Path
) -> None:
    world = ready_world(tmp_path)
    _migrations_definition_with(world, **changes)
    assert reason in _blocks(world, tmp_path)


@pytest.mark.parametrize(
    "value",
    [
        TD_PREFIX + "neurostack-staging-migrations:3",
        TD_PREFIX + "neurostack-staging-migrations:2",
        TD_PREFIX + "neurostack-staging-migrations",
        TD_PREFIX + "neurostack-staging-app:9",
        TD_PREFIX.replace(ACCOUNT, "488460142431") + "neurostack-staging-migrations:5",
        "neurostack-staging-migrations:5",
    ],
)
def test_the_migrations_argument_is_never_revision_3_or_older_or_another_family(
    value: str,
) -> None:
    with pytest.raises(mw.WindowError, match="migrations_task_definition_invalid"):
        mw.validate_migrations_arn(value)


def test_plan_refuses_a_stale_or_future_capture(tmp_path: Path) -> None:
    world = ready_world(tmp_path)
    world.captured_at = "2026-10-22T10:19:59Z"
    assert "readback_stale" in _blocks(world, tmp_path)
    world.captured_at = "2026-10-22T10:31:01Z"
    assert "readback_captured_in_future" in _blocks(world, tmp_path)
    world.captured_at = "2026-10-22T10:20:00Z"
    assert plan(world, tmp_path)["status"] == "ready"


def test_plan_refuses_a_stale_pre_read_or_one_before_the_snapshot(
    tmp_path: Path,
) -> None:
    world = World()
    world.record_read(
        tmp_path,
        "pre",
        created="2026-10-22T10:10:00+00:00",
        stopped="2026-10-22T10:14:59+00:00",
    )
    assert "pre_read_stale" in _blocks(world, tmp_path)
    world = World()
    world.record_read(
        tmp_path,
        "pre",
        created="2026-10-22T09:59:59+00:00",
        stopped="2026-10-22T10:25:00+00:00",
    )
    assert "pre_read_before_snapshot" in _blocks(world, tmp_path)
    world = World()
    world.record_read(
        tmp_path,
        "pre",
        created="2026-10-22T10:00:00+00:00",
        stopped="2026-10-22T10:15:00+00:00",
    )
    assert plan(world, tmp_path)["status"] == "ready"


def test_plan_refuses_without_a_recorded_pre_read(tmp_path: Path) -> None:
    world = World()
    document = plan(world, tmp_path)
    assert document["status"] == "blocked"
    assert "pre_read_not_recorded" in document["blocks"]


@pytest.mark.parametrize("window", ["036", "037"])
def test_036_and_037_refuse_a_rollback_target_not_repinned(
    window: str, tmp_path: Path
) -> None:
    world = ready_world(tmp_path, window)
    # The manifest as it read before the first re-pin: revision 8 with its
    # note, and before the second: revision 9 with its note (pending re-pin
    # to revision 10 before the image bump).
    for manifest in (initial_manifest(tmp_path), bridge_manifest(tmp_path)):
        blocks = _blocks(world, tmp_path, manifest=manifest)
        assert "rollback_target_not_repinned" in blocks


def test_the_reviewed_manifest_is_re_pinned_to_revision_10() -> None:
    raw = wiring.load_yaml(MANIFEST)
    assert raw["rollback_target"] == {
        "revision": 10,
        "effective_after": wiring.ROLLBACK_EFFECTIVE_WORKER_FLAG,
    }
    pins = wiring.manifest_pins(wiring.validate_manifest(raw))
    assert pins.rollback_revision == mw.WINDOW_036.rollback_target_revision
    assert pins.rollback_revision == mw.WINDOW_037.rollback_target_revision
    assert pins.rollback_repin_before is None
    assert pins.rollback_task_definition_arn == LIVE_REVISION_10


@pytest.mark.parametrize("window", ["036", "037"])
def test_036_and_037_accept_the_live_revision_10_by_digest(
    window: str, tmp_path: Path
) -> None:
    """The reviewed manifest, read by default: revision 10 ACTIVE by digest.

    The service runs a later revision (the image bump, revision 11) with the
    image containing the window's migration; the rollback target is the live
    worker-flag revision 10 on the bridge image.
    """

    world = ready_world(tmp_path, window)
    later = TD_PREFIX + "neurostack-staging-app:11"
    world.service_td = later
    world.put(
        "ecs", "DescribeServices", mw.describe_services_request(), _service(later)
    )
    world.put(
        "ecs",
        "DescribeTasks",
        mw.describe_tasks_request(SERVICE_TASKS),
        {
            "tasks": [_running_task(arn, later, world.digest) for arn in SERVICE_TASKS],
            "failures": [],
        },
    )
    world.put(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(later),
        _app_definition(later, BRIDGE_IMAGE),
    )
    world.put(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(LIVE_REVISION_10),
        _app_definition(LIVE_REVISION_10, LIVE_REVISION_10_IMAGE),
    )
    document = plan(world, tmp_path)
    assert document["status"] == "ready", document["blocks"]
    assert document["manifest"] == {"rollback_target_revision": 10}
    assert "rollback_target_repinned" in document["checks"]
    assert "rollback_target_revision_active_digest_pinned" in document["checks"]
    tagged = LIVE_REVISION_10_IMAGE.split("@", 1)[0] + ":" + OLD_TAG
    world.put(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(LIVE_REVISION_10),
        _app_definition(LIVE_REVISION_10, tagged),
    )
    assert "rollback_target_revision_unusable" in _blocks(world, tmp_path)


@pytest.mark.parametrize(
    "change",
    [
        {"status": "INACTIVE"},
        {"containerDefinitions": [{"name": "app", "image": OLD_IMAGE}]},
    ],
)
def test_036_refuses_an_unusable_revision_10(
    change: dict[str, Any], tmp_path: Path
) -> None:
    world = ready_world(tmp_path, "036")
    world.put(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(SERVICE_TD_10),
        _app_definition(SERVICE_TD_10, BRIDGE_IMAGE, **change),
    )
    assert "rollback_target_revision_unusable" in _blocks(world, tmp_path)


def test_036_refuses_an_invalid_manifest(tmp_path: Path) -> None:
    world = ready_world(tmp_path, "036")
    broken = tmp_path / "broken.yaml"
    broken.write_text(
        MANIFEST.read_text(encoding="utf-8").replace(
            "`alembic upgrade 036`", "`alembic upgrade head`"
        ),
        encoding="utf-8",
    )
    assert "manifest_invalid" in _blocks(world, tmp_path, manifest=broken)


def test_035_does_not_read_the_rollback_target(tmp_path: Path) -> None:
    world = ready_world(tmp_path, "035")
    document = plan(world, tmp_path, manifest=MANIFEST)
    assert document["status"] == "ready"
    assert document["manifest"] == {}


def test_a_recorded_migration_run_blocks_a_second_plan(tmp_path: Path) -> None:
    world = completed(tmp_path)
    world.captured_at = CAPTURED_PLAN
    assert "migration_run_already_recorded" in _blocks(world, tmp_path)


def test_a_missing_read_is_incomplete_with_the_exact_request(tmp_path: Path) -> None:
    world = ready_world(tmp_path)
    world.drop(
        "rds", "DescribeDBSnapshots", mw.describe_snapshot_request(world.snapshot_id)
    )
    world.drop("ecs", "ListTasks", mw.list_family_tasks_request(mw.READ_FAMILY))
    document = plan(world, tmp_path)
    assert document["status"] == "incomplete"
    assert {
        "service": "rds",
        "operation": "DescribeDBSnapshots",
        "params": {"DBSnapshotIdentifier": world.snapshot_id},
    } in document["next_requests"]
    assert {
        "service": "ecs",
        "operation": "ListTasks",
        "params": mw.list_family_tasks_request(mw.READ_FAMILY),
    } in document["next_requests"]
    with pytest.raises(mw.WindowError, match="plan_blocked"):
        render_run(world, tmp_path)


def test_plan_names_the_service_task_reads_it_still_needs(tmp_path: Path) -> None:
    world = ready_world(tmp_path)
    world.drop("ecs", "DescribeTasks", mw.describe_tasks_request(SERVICE_TASKS))
    document = plan(world, tmp_path)
    assert document["status"] == "incomplete"
    assert {
        "service": "ecs",
        "operation": "DescribeTasks",
        "params": {"cluster": "neurostack-staging", "tasks": sorted(SERVICE_TASKS)},
    } in document["next_requests"]


@pytest.mark.parametrize(
    "service_change,reason",
    [
        ({"runningCount": 1}, "service_not_steady"),
        ({"taskDefinition": SERVICE_TD_8}, "service_task_definition_mismatch"),
        ({"deployments": []}, "deployment_not_completed"),
        (
            {
                "networkConfiguration": {
                    "awsvpcConfiguration": {
                        "subnets": SUBNETS,
                        "securityGroups": SECURITY_GROUPS,
                        "assignPublicIp": "ENABLED",
                    }
                }
            },
            "service_assign_public_ip_not_disabled",
        ),
    ],
)
def test_plan_reuses_the_hook_service_view(
    service_change: dict[str, Any], reason: str, tmp_path: Path
) -> None:
    world = ready_world(tmp_path)
    world.put(
        "ecs",
        "DescribeServices",
        mw.describe_services_request(),
        _service(world.service_td, **service_change),
    )
    assert reason in _blocks(world, tmp_path)


def test_plan_cli_exit_codes_and_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = ready_world(tmp_path)
    code, out, err = run_cli(["plan", *world.args(tmp_path)], capsys)
    assert code == 0, err
    assert json.loads(out)["status"] == "ready"
    world.put(
        "ecs",
        "ListTasks",
        mw.list_family_tasks_request(mw.MIGRATIONS_FAMILY),
        {"taskArns": [RUN_TASK]},
    )
    code, out, _err = run_cli(["plan", *world.args(tmp_path)], capsys)
    assert code == 2
    assert "migrations_task_running" in json.loads(out)["blocks"]


# ---------------------------------------------------------------------------
# render-run-task: the exact RunTask, accepted live, never widened
# ---------------------------------------------------------------------------

LIVE_ACCEPTED_RUN_TASK_KEYS = {
    "cluster",
    "taskDefinition",
    "launchType",
    "count",
    "startedBy",
    "enableExecuteCommand",
    "networkConfiguration",
    "clientToken",
    "enableECSManagedTags",
}


@pytest.mark.parametrize("window", ["035", "036", "037"])
def test_render_run_task_prints_the_exact_request(window: str, tmp_path: Path) -> None:
    world = ready_world(tmp_path, window)
    request = render_run(world, tmp_path)
    assert set(request) == LIVE_ACCEPTED_RUN_TASK_KEYS | {"platformVersion"}
    assert "propagateTags" not in request
    assert "overrides" not in request
    assert request["cluster"] == "neurostack-staging"
    assert request["taskDefinition"] == MIGRATIONS_TD[window]
    assert request["launchType"] == "FARGATE"
    assert request["count"] == 1
    assert request["startedBy"] == f"neurostack-migration-window-{window}"
    assert request["enableExecuteCommand"] is False
    assert request["enableECSManagedTags"] is False
    assert request["platformVersion"] == "LATEST"
    assert request["networkConfiguration"] == {
        "awsvpcConfiguration": {
            "subnets": SUBNETS,
            "securityGroups": SECURITY_GROUPS,
            "assignPublicIp": "DISABLED",
        }
    }
    assert re.fullmatch(
        rf"migration-window-{window}-run-[0-9a-f]{{32}}", request["clientToken"]
    )
    assert len(request["clientToken"]) <= 64
    text = json.dumps(request)
    assert "migrations:3" not in text and UPGRADE_HEAD.search(text) is None


def test_platform_version_is_copied_only_when_the_service_has_one(
    tmp_path: Path,
) -> None:
    world = ready_world(tmp_path)
    service = _service(world.service_td)
    del service["services"][0]["platformVersion"]
    world.put("ecs", "DescribeServices", mw.describe_services_request(), service)
    world.records.clear()
    world.record_read(tmp_path, "pre")
    request = render_run(world, tmp_path)
    assert set(request) == LIVE_ACCEPTED_RUN_TASK_KEYS


def test_client_token_is_deterministic_per_window_snapshot_and_attempt(
    tmp_path: Path,
) -> None:
    world = ready_world(tmp_path)
    first = render_run(world, tmp_path)
    assert render_run(world, tmp_path) == first
    second = render_run(world, tmp_path, attempt=2)
    assert second["clientToken"] != first["clientToken"]
    assert {k: v for k, v in second.items() if k != "clientToken"} == {
        k: v for k, v in first.items() if k != "clientToken"
    }
    run = mw.RunContext(
        network=mw.Network(tuple(SUBNETS), tuple(SECURITY_GROUPS), "LATEST"),
        migrations_task_definition=MIGRATIONS_TD["036"],
        snapshot_id="pre-036-2026-10-23",
    )
    other_day = mw.build_run_task_request(
        mw.WINDOW_036,
        run.network,
        run.migrations_task_definition,
        run.snapshot_id,
        attempt=1,
    )
    assert other_day["clientToken"] != first["clientToken"]
    for attempt in (0, 21, True, "1"):
        with pytest.raises(mw.WindowError, match="attempt_out_of_range"):
            mw.build_run_task_request(mw.WINDOW_036, run.network, run.migrations_task_definition, run.snapshot_id, attempt=attempt)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda r: r.__setitem__("propagateTags", "NONE"), "propagate_tags"),
        (lambda r: r.__setitem__("propagateTags", "TASK_DEFINITION"), "propagate_tags"),
        (
            lambda r: r.__setitem__(
                "overrides",
                {
                    "containerOverrides": [
                        {
                            "name": "migrations",
                            "command": ["alembic", "upgrade", "head"],
                        }
                    ]
                },
            ),
            "command_override_forbidden",
        ),
        (lambda r: r.__setitem__("overrides", {}), "command_override_forbidden"),
        (lambda r: r.__setitem__("tags", []), "key_set"),
        (lambda r: r.pop("clientToken"), "key_set"),
        (lambda r: r.__setitem__("count", 2), "run_task_request_invariant"),
        (lambda r: r.__setitem__("launchType", "EC2"), "run_task_request_invariant"),
        (
            lambda r: r.__setitem__("enableExecuteCommand", True),
            "run_task_request_invariant",
        ),
        (
            lambda r: r.__setitem__("enableECSManagedTags", True),
            "run_task_request_invariant",
        ),
        (
            lambda r: r.__setitem__("startedBy", "neurostack-migration-window-037"),
            "run_task_request_invariant",
        ),
        (
            lambda r: r.__setitem__(
                "taskDefinition", TD_PREFIX + "neurostack-staging-migrations:3"
            ),
            "run_task_request_invariant",
        ),
        (
            lambda r: r["networkConfiguration"]["awsvpcConfiguration"].__setitem__(
                "assignPublicIp", "ENABLED"
            ),
            "network",
        ),
        (
            lambda r: r["networkConfiguration"]["awsvpcConfiguration"].__setitem__(
                "subnets", []
            ),
            "network",
        ),
        (lambda r: r["networkConfiguration"].__setitem__("extra", {}), "network"),
        (lambda r: r.__setitem__("clientToken", "x"), "run_task_request_invariant"),
        (
            lambda r: r.__setitem__("platformVersion", "1.4.0; rm"),
            "run_task_request_invariant",
        ),
    ],
)
def test_run_task_invariant_refuses_any_widening(
    mutate: Callable[[dict[str, Any]], Any], reason: str, tmp_path: Path
) -> None:
    world = ready_world(tmp_path)
    request = render_run(world, tmp_path)
    mutate(request)
    with pytest.raises(mw.WindowError, match=reason):
        mw.assert_run_task_invariants(request, mw.WINDOW_036, MIGRATIONS_TD["036"])


def test_render_run_task_refuses_unless_plan_is_ready(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World()
    code, out, err = run_cli(
        [
            "render-run-task",
            *world.args(tmp_path),
        ],
        capsys,
    )
    assert code == 2 and out == ""
    assert "plan_blocked" in err and "pre_read_not_recorded" in err
    world.record_read(tmp_path, "pre")
    code, out, err = run_cli(
        [
            "render-run-task",
            *world.args(tmp_path),
        ],
        capsys,
    )
    assert code == 0, err
    assert json.loads(out) == render_run(world, tmp_path)
    monkeypatch.setattr(
        mw, "_clock", lambda: datetime(2026, 10, 22, 10, 40, tzinfo=timezone.utc)
    )
    code, out, err = run_cli(
        [
            "render-run-task",
            *world.args(tmp_path),
        ],
        capsys,
    )
    assert code == 2 and out == ""
    assert "readback_stale" in err
    code, out, err = run_cli(
        ["render-run-task", *world.args(tmp_path), "--now", NOW], capsys
    )
    assert code == 2 and out == "" and "blocked: usage" in err


# ---------------------------------------------------------------------------
# Read tasks: rendering
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window", ["035", "036", "037"])
@pytest.mark.parametrize("phase", ["pre", "post"])
def test_read_request_is_the_read_program_on_the_right_family(
    window: str, phase: str, tmp_path: Path
) -> None:
    import hashlib

    world = World(window)
    context = world.read_context(tmp_path, phase)
    request = mw.build_read_request(
        world.window, phase, context.network, context.target, attempt=1
    )
    assert "propagateTags" not in request
    spec = world.window.read(phase)
    if spec.role == "migrations":
        # Window 037's readback: the dedicated read revision, readback baked,
        # no override at all (a request without one cannot migrate).
        assert (window, phase) == ("037", "pre")
        assert context.target.baked is True
        assert request["taskDefinition"] == MIGRATIONS_READ_TD
        assert set(request) == LIVE_ACCEPTED_RUN_TASK_KEYS | {"platformVersion"}
        response = world.response(
            "ecs",
            "DescribeTaskDefinition",
            mw.describe_task_definition_request(MIGRATIONS_READ_TD),
        )
        command = response["taskDefinition"]["containerDefinitions"][0]["command"]
    else:
        assert context.target.baked is False
        assert request["taskDefinition"] == READ_TD
        assert set(request) == LIVE_ACCEPTED_RUN_TASK_KEYS | {
            "overrides",
            "platformVersion",
        }
        (override,) = request["overrides"]["containerOverrides"]
        assert set(override) == {"name", "command"} and override["name"] == "consulta"
        command = override["command"]
        size = len(json.dumps(request["overrides"], separators=(",", ":")))
        assert size <= mw.OVERRIDES_BUDGET_CHARS < mw.OVERRIDES_MAX_CHARS
    assert command[:2] == ["python", "-c"]
    program = command[2]
    assert program.endswith(mw._READ_PROGRAM_BODY)
    assert "readonly=True" in program and 'isolation="repeatable_read"' in program
    assert "await t.rollback()" in program
    assert "alembic" not in program.lower()
    header = mw._READ_HEADER.match(program)
    assert header is not None
    assert header.group("window") == window and header.group("phase") == phase
    payload = mw._decompressed(header.group("payload"))
    assert payload == mw.read_payload(spec)
    assert header.group("sha") == hashlib.sha256(payload).hexdigest()
    assert json.loads(payload) == {"sql": mw.read_sql(spec), "version": mw.VERSION_SQL}
    assert request["startedBy"] == f"neurostack-migration-read-{window}-{phase}"
    assert re.fullmatch(
        rf"migration-read-{window}-{phase}-[0-9a-f]{{32}}", request["clientToken"]
    )
    assert UPGRADE_HEAD.search(json.dumps(request)) is None


def test_the_037_readback_is_verbatim_in_its_read_revision() -> None:
    sql = mw.read_sql(mw.WINDOW_037.pre_read)
    assert sql == (ROOT / "scripts" / "migration_037_readback.sql").read_text(
        encoding="utf-8"
    )
    program = mw.read_program(mw.WINDOW_037, mw.WINDOW_037.pre_read)
    header = mw._READ_HEADER.match(program)
    assert header is not None
    assert json.loads(mw._decompressed(header.group("payload")) or b"")["sql"] == sql
    # Baked in a task definition: far below the 64 KiB a task definition allows.
    assert len(program) < 16_000


def test_a_read_too_large_for_the_override_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = World("036")
    context = world.read_context(tmp_path, "pre")
    big = (
        "SELECT 1 AS check_name, 0 AS found -- "
        + "".join(f"{i:x}" for i in range(4000))
        + "\n"
    )
    monkeypatch.setattr(mw, "read_sql", lambda spec: big)
    with pytest.raises(mw.WindowError, match="overrides_too_large"):
        mw.build_read_request(
            world.window, "pre", context.network, context.target, attempt=1
        )


def test_read_normalization_ignores_the_gzip_bytes_but_not_the_sql() -> None:
    import base64
    import gzip as gz

    program = mw.read_program(mw.WINDOW_036, mw.WINDOW_036.pre_read)
    header = mw._READ_HEADER.match(program)
    assert header is not None
    payload = mw.read_payload(mw.WINDOW_036.pre_read)
    recompressed = base64.b64encode(
        gz.compress(payload, compresslevel=1, mtime=0)
    ).decode()
    assert recompressed != header.group("payload")
    other = program.replace(header.group("payload"), recompressed)
    assert mw.normalized_read_command(
        ["python", "-c", other]
    ) == mw.normalized_read_command(["python", "-c", program])
    tampered_payload = base64.b64encode(
        gz.compress(payload.replace(b"count(*)", b"1", 1), mtime=0)
    ).decode()
    tampered = program.replace(header.group("payload"), tampered_payload)
    assert mw.normalized_read_command(["python", "-c", tampered]) is None
    assert mw.normalized_read_command(["python", "-c", program + "\nimport os"]) is None
    assert (
        mw.normalized_read_command(
            ["python", "-c", program.replace("readonly=True", "readonly=False")]
        )
        is None
    )
    assert mw.normalized_read_command(["sh", "-c", program]) is None
    assert mw.normalized_read_command(["alembic", "upgrade", "036"]) is None


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda r: r.__setitem__("propagateTags", "NONE"), "propagate_tags"),
        (lambda r: r.pop("overrides"), "key_set"),
        (
            lambda r: r["overrides"]["containerOverrides"][0].__setitem__(
                "command", ["alembic", "upgrade", "036"]
            ),
            "read_request_invariant",
        ),
        (
            lambda r: r["overrides"]["containerOverrides"][0].__setitem__(
                "environment", []
            ),
            "read_request_invariant",
        ),
        (
            lambda r: r["overrides"]["containerOverrides"].append(
                {"name": "x", "command": ["true"]}
            ),
            "read_request_invariant",
        ),
        (
            lambda r: r["overrides"]["containerOverrides"][0].__setitem__(
                "name", "app"
            ),
            "read_request_invariant",
        ),
        (
            lambda r: r.__setitem__("startedBy", "neurostack-migration-window-036"),
            "read_request_invariant",
        ),
        (
            lambda r: r.__setitem__("taskDefinition", MIGRATIONS_TD["036"]),
            "run_task_request_invariant",
        ),
        (
            lambda r: r["networkConfiguration"]["awsvpcConfiguration"].__setitem__(
                "assignPublicIp", "ENABLED"
            ),
            "network",
        ),
    ],
)
def test_read_request_invariant_refuses_any_widening(
    mutate: Callable[[dict[str, Any]], Any], reason: str, tmp_path: Path
) -> None:
    world = World("036")
    context = world.read_context(tmp_path, "pre")
    request = mw.build_read_request(
        world.window, "pre", context.network, context.target, attempt=1
    )
    mutate(request)
    with pytest.raises(mw.WindowError, match=reason):
        mw.assert_read_request_invariants(request, world.window, "pre", context.target)


@pytest.mark.parametrize(
    "change,reason",
    [
        (
            {"secrets": [{"name": "DATABASE_URL", "valueFrom": MIGRATIONS_DB_ARN}]},
            "read_database_secret_mismatch",
        ),
        ({"secrets": []}, "read_database_secret_mismatch"),
        (
            {"environment": [{"name": "DATABASE_URL", "value": "x"}]},
            "read_database_secret_mismatch",
        ),
        ({"entryPoint": ["sh"]}, "read_container_entry_point_unexpected"),
        (
            {"image": "docker.io/library/postgres:16"},
            "read_image_not_an_application_image",
        ),
        ({"logConfiguration": None}, "read_log_configuration_unusable"),
    ],
)
def test_read_plan_refuses_an_unusable_read_family(
    change: dict[str, Any], reason: str, tmp_path: Path
) -> None:
    world = World("036")
    world.put(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(READ_TD),
        _one_container_definition(
            READ_TD, "neurostack-staging-consulta", _read_container(**change)
        ),
    )
    document, context = mw.evaluate_read_plan(
        world.readback(tmp_path), world.context(), "pre", now=NOW_DT
    )
    assert context is None and document["status"] == "blocked"
    assert reason in document["blocks"]


def _read_plan_for(world: World, tmp_path: Path, container: dict[str, Any]) -> Any:
    world.put(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(READ_TD),
        _one_container_definition(READ_TD, "neurostack-staging-consulta", container),
    )
    return mw.evaluate_read_plan(
        world.readback(tmp_path), world.context(), "pre", now=NOW_DT
    )


def test_the_live_read_family_revision_26_is_refused(tmp_path: Path) -> None:
    """consulta:26 (2026-10-20): Alpine, ``sh -c`` with psql, the app role
    through APP_URL. The live plan-pre-read of 2026-10-22 blocked it."""

    container = _read_container(
        image="public.ecr.aws/docker/library/alpine:3.20",
        entryPoint=["sh", "-c"],
        command=['psql "$APP_URL" -c "select 1"'],
        secrets=[{"name": "APP_URL", "valueFrom": APP_DB_ARN}],
    )
    document, context = _read_plan_for(World("035"), tmp_path, container)
    assert context is None and document["status"] == "blocked"
    for reason in (
        "read_container_entry_point_unexpected",
        "read_database_secret_mismatch",
        "read_image_not_an_application_image",
    ):
        assert reason in document["blocks"], reason


@pytest.mark.parametrize("window", ["035", "036"])
def test_the_live_read_family_revision_28_satisfies_the_contract(
    window: str, tmp_path: Path
) -> None:
    """consulta:28 (2026-10-22): the bridge image by digest, no entry point,
    the app DATABASE_URL secret and a harmless baked command. Its image need
    not be the deployed or the migrations image: any image of the registry."""

    container = _read_container(
        image=LIVE_REVISION_9_IMAGE,
        command=[
            "python",
            "-c",
            "raise SystemExit('read revision: a command override is required')",
        ],
    )
    world = World(window)
    assert LIVE_REVISION_9_IMAGE not in (BRIDGE_IMAGE, OLD_IMAGE)
    document, context = _read_plan_for(world, tmp_path, container)
    assert context is not None and document["status"] == "ready", document["blocks"]
    for check in (
        "read_container_has_no_entry_point",
        "read_database_secret_is_the_app_role",
        "read_image_in_registry",
        "read_family_names_no_migration_tool",
    ):
        assert check in document["checks"], check


def test_the_037_readback_never_runs_as_the_application_role(tmp_path: Path) -> None:
    world = World("037")
    container = _migrations_container(
        "037", secrets=[{"name": "DATABASE_URL", "valueFrom": APP_DB_ARN}]
    )
    world.put(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(world.migrations_td),
        _one_container_definition(
            world.migrations_td, "neurostack-staging-migrations", container
        ),
    )
    document, context = mw.evaluate_read_plan(
        world.readback(tmp_path), world.context(), "pre", now=NOW_DT
    )
    assert context is None
    assert "migrations_database_secret_mismatch" in document["blocks"]


@pytest.mark.parametrize(
    "params_of,reason",
    [
        (
            lambda w: mw.list_started_by_request(w.window.read_started_by("pre")),
            "read_task_running",
        ),
        (
            lambda w: mw.list_family_tasks_request(mw.MIGRATIONS_FAMILY),
            "migrations_task_running",
        ),
    ],
)
def test_read_plan_is_single_flight(
    params_of: Callable[[World], dict[str, Any]], reason: str, tmp_path: Path
) -> None:
    world = World("036")
    world.put("ecs", "ListTasks", params_of(world), {"taskArns": [PRE_TASK]})
    document, context = mw.evaluate_read_plan(
        world.readback(tmp_path), world.context(), "pre", now=NOW_DT
    )
    assert context is None and reason in document["blocks"]


def test_pre_read_plan_needs_the_snapshot_first(tmp_path: Path) -> None:
    world = World("036")
    world.put(
        "rds",
        "DescribeDBSnapshots",
        mw.describe_snapshot_request(world.snapshot_id),
        _snapshot(world.snapshot_id, Status="creating"),
    )
    document, context = mw.evaluate_read_plan(
        world.readback(tmp_path), world.context(), "pre", now=NOW_DT
    )
    assert context is None and "snapshot_not_available" in document["blocks"]
    # The post-read does not depend on the snapshot.
    _document, context = mw.evaluate_read_plan(
        world.readback(tmp_path), world.context(), "post", now=NOW_DT
    )
    assert context is not None


def test_a_recorded_read_is_never_rendered_again(tmp_path: Path) -> None:
    world = ready_world(tmp_path)
    document, context = mw.evaluate_read_plan(
        world.readback(tmp_path), world.context(), "pre", now=NOW_DT
    )
    assert context is None and "pre_read_already_recorded" in document["blocks"]


def test_render_pre_and_post_read_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World("037")
    code, out, err = run_cli(["render-pre-read", *world.args(tmp_path)], capsys)
    assert code == 0, err
    request = json.loads(out)
    assert request["taskDefinition"] == MIGRATIONS_READ_TD
    assert "overrides" not in request
    assert request["startedBy"] == "neurostack-migration-read-037-pre"
    code, out, err = run_cli(
        ["render-post-read", *world.args(tmp_path), "--attempt", "2"],
        capsys,
    )
    assert code == 0, err
    request = json.loads(out)
    assert request["taskDefinition"] == READ_TD
    assert request["startedBy"] == "neurostack-migration-read-037-post"
    code, out, err = run_cli(["plan-pre-read", *world.args(tmp_path)], capsys)
    assert code == 0, err
    assert json.loads(out)["status"] == "ready"
    world.drop(
        "ecs", "DescribeTaskDefinition", mw.describe_task_definition_request(READ_TD)
    )
    code, out, err = run_cli(["plan-post-read", *world.args(tmp_path)], capsys)
    assert code == 2
    document = json.loads(out)
    assert document["status"] == "incomplete"
    assert document["next_requests"] == [
        {
            "service": "ecs",
            "operation": "DescribeTaskDefinition",
            "params": {"taskDefinition": READ_TD},
        }
    ]
    code, out, err = run_cli(["render-post-read", *world.args(tmp_path)], capsys)
    assert code == 2 and out == "" and "read_plan_blocked" in err


# ---------------------------------------------------------------------------
# verify-result: the stopped migration task, on the real Alembic output
# ---------------------------------------------------------------------------

FORBIDDEN_IN_OUTPUT = re.compile(
    r"rollback(?!_target_)|roll back|UpdateService|RegisterTaskDefinition|"
    r"StopTask|DeleteDBSnapshot|upgrade\s+heads?\b|propagateTags",
    re.IGNORECASE,
)


def _assert_printable(document: dict[str, Any]) -> None:
    mw._emit(document)  # the output guard accepts it
    assert FORBIDDEN_IN_OUTPUT.search(json.dumps(document)) is None


@pytest.mark.parametrize("window", ["035", "036", "037"])
def test_the_window_success_log_is_applied(window: str, tmp_path: Path) -> None:
    world = completed(tmp_path, window)
    result = verify(world, tmp_path)
    assert result["outcome"] == "applied", result["reasons"]
    assert result["alert"] is False and result["reasons"] == []
    assert result["container_exit_code"] == 0
    assert result["classification"] is None
    assert (
        result["upgrades"]
        == result["expected_upgrades"]
        == [f"{a} -> {b}" for a, b in mw.WINDOWS[window].upgrades]
    )
    assert result["attempt"] == 1 and result["recovered_without_run_record"] is False
    assert result["next"].startswith("render-post-read")
    _assert_printable(result)


@pytest.mark.parametrize(
    "window,case,classification,refusal",
    [
        (
            "036",
            "refused_036",
            "preflight_refused_55000_nothing_changed",
            "migration 036 refused (nothing was changed): unexpected objects depend on the dropped columns: index g10_t8_probe",
        ),
        (
            "037",
            "refused_037",
            "preflight_refused_55000_nothing_changed",
            "migration 037 refused (nothing was changed): comments the downgrade would not restore: table public.billing_migrations",
        ),
        ("036", "lock_timeout_036", "lock_timeout_55P03_nothing_changed", None),
        ("036", "deadlock_036", "deadlock_40P01_nothing_changed", None),
    ],
)
def test_sqlstate_failures_are_nothing_changed_alerts(
    window: str, case: str, classification: str, refusal: str | None, tmp_path: Path
) -> None:
    golden = ALEMBIC[case]
    assert golden["exit_code"] == 1
    world = completed(tmp_path, window, golden["lines"], exit_code=golden["exit_code"])
    result = verify(world, tmp_path)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert result["alert"] is True
    assert result["classification"] == classification
    assert result["alert_kinds"] == ["nothing_changed"]
    assert result["reasons"] == [classification]
    assert result["refusal_lines"] == ([refusal] if refusal else [])
    assert result["refusal_lines_withheld"] == 0
    assert "log_exposure_suspected" not in result["reasons"]
    assert f"still reads {mw.WINDOWS[window].before}" in result["next"]
    _assert_printable(result)


@pytest.mark.parametrize("exit_code", [0, 1])
def test_55000_is_never_success_even_with_exit_zero(
    exit_code: int, tmp_path: Path
) -> None:
    world = completed(
        tmp_path, "036", ALEMBIC["refused_036"]["lines"], exit_code=exit_code
    )
    result = verify(world, tmp_path)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    if exit_code:
        assert "preflight_refused_55000_nothing_changed" in result["reasons"]
    else:
        # Exit 0 with a SQLSTATE marker: inconsistent, never "nothing changed".
        assert result["classification"] == "migration_log_inconsistent"
        assert "nothing_changed" not in result["alert_kinds"]
        assert "preflight_refused_55000_nothing_changed" not in result["reasons"]


def test_the_unpinned_head_run_is_schema_beyond_the_window(tmp_path: Path) -> None:
    world = completed(tmp_path, "035", ALEMBIC["head_from_032"]["lines"])
    result = verify(world, tmp_path)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert "upgrade_outside_window" in result["reasons"]
    assert "schema_beyond_window" in result["alert_kinds"]
    assert result["upgrades"][-2:] == ["035 -> 036", "036 -> 037"]


@pytest.mark.parametrize(
    "window,case",
    [
        ("035", "success_036"),
        ("036", "success_037"),
        ("036", "success_035"),
        ("037", "success_036"),
    ],
)
def test_another_windows_upgrade_lines_are_refused(
    window: str, case: str, tmp_path: Path
) -> None:
    world = completed(tmp_path, window, ALEMBIC[case]["lines"])
    result = verify(world, tmp_path)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert "upgrade_outside_window" in result["reasons"]


@pytest.mark.parametrize(
    "lines,reason",
    [
        (ALEMBIC["success_035"]["lines"][:-1], "upgrade_lines_mismatch"),
        (ALEMBIC["success_035"]["lines"][:2], "upgrade_lines_mismatch"),
        (
            ALEMBIC["success_035"]["lines"] + [ALEMBIC["success_035"]["lines"][-1]],
            "upgrade_lines_mismatch",
        ),
        (
            ALEMBIC["success_035"]["lines"]
            + ["INFO  [alembic.runtime.migration] Running downgrade 035 -> 034, x"],
            "alembic_operation_unexpected",
        ),
        (
            ALEMBIC["success_035"]["lines"]
            + ["INFO  [alembic.runtime.migration] Running stamp_revision 034 -> 035"],
            "alembic_operation_unexpected",
        ),
        (
            ALEMBIC["success_035"]["lines"]
            + ["Traceback (most recent call last):", "RuntimeError: boom"],
            "migration_failed_unclassified",
        ),
        (
            ALEMBIC["success_035"]["lines"]
            + ["FAILED: Can't locate revision identified by '035'"],
            "migration_failed_unclassified",
        ),
        (
            ALEMBIC["success_035"]["lines"] + ["ERROR [alembic.util.messaging] boom"],
            "migration_failed_unclassified",
        ),
    ],
)
def test_unexpected_upgrade_lines_or_errors_with_exit_zero_stop(
    lines: list[str], reason: str, tmp_path: Path
) -> None:
    world = completed(tmp_path, "035", lines)
    result = verify(world, tmp_path)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert reason in result["reasons"], result["reasons"]


@pytest.mark.parametrize("exit_code", [1, 2, 137, -1])
def test_a_nonzero_exit_code_is_never_applied(exit_code: int, tmp_path: Path) -> None:
    world = completed(tmp_path, "036", exit_code=exit_code)
    result = verify(world, tmp_path)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert result["classification"] == "migration_failed_unclassified"
    assert result["container_exit_code"] == exit_code


@pytest.mark.parametrize("exit_code", [None, "0", True])
def test_a_stopped_task_without_an_exit_code_stops(
    exit_code: Any, tmp_path: Path
) -> None:
    world = completed(tmp_path, "036", exit_code=exit_code)
    result = verify(world, tmp_path)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert "migration_stopped_without_exit_code" in result["reasons"]


@pytest.mark.parametrize(
    "leak",
    [
        LEAKED_URL,
        LEAKED_HOST,
        "connection to server at 10.0.12.34 failed",
        'FATAL:  password authentication failed for user "neurostack_owner"',
        "DATABASE_URL is not set",
        "neurostack_owner" + "@" + "db.internal.example",
        "Bearer abc",
        "https" + "://sqlalche.me/e/20/dbapi",
    ],
)
def test_exposure_in_any_log_line_is_an_alert_and_never_echoed(
    leak: str, tmp_path: Path
) -> None:
    lines = list(ALEMBIC["success_036"]["lines"])
    lines.insert(1, leak)
    world = completed(tmp_path, "036", lines)
    result = verify(world, tmp_path)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert "log_exposure_suspected" in result["reasons"]
    assert "exposure_suspected" in result["alert_kinds"]
    assert leak not in json.dumps(result)


def test_the_sqlalchemy_background_line_alone_is_not_exposure() -> None:
    log = mw.classify_migration_log(
        ["(Background on this error at: https://sqlalche.me/e/20/dbapi)"]
    )
    assert log.exposure is False
    log = mw.classify_migration_log(
        ["(Background on this error at: https://sqlalche.me/e/20/dbapi) and more"]
    )
    assert log.exposure is True


def test_the_refusal_comes_from_the_exception_line_never_the_echoed_sql() -> None:
    echo = "        RAISE EXCEPTION 'migration 036 refused (nothing was changed): %',"
    log = mw.classify_migration_log([echo])
    assert log.refusals == [] and log.sqlstates == []
    log = mw.classify_migration_log(ALEMBIC["lock_timeout_036"]["lines"])
    assert log.refusals == [] and log.sqlstates == ["55P03"]


def test_a_refusal_line_that_would_trip_the_guard_is_withheld(tmp_path: Path) -> None:
    lines = [
        *ALEMBIC["success_036"]["lines"],
        "asyncpg.exceptions.ObjectNotInPrerequisiteStateError: migration 036 refused (nothing was changed): call UpdateService first",
    ]
    world = completed(tmp_path, "036", lines, exit_code=1)
    result = verify(world, tmp_path)
    assert result["refusal_lines"] == [] and result["refusal_lines_withheld"] == 1
    _assert_printable(result)


def test_two_sqlstates_in_one_log_are_ambiguous(tmp_path: Path) -> None:
    lines = ALEMBIC["lock_timeout_036"]["lines"] + [
        "asyncpg.exceptions.DeadlockDetectedError: deadlock detected"
    ]
    world = completed(tmp_path, "036", lines, exit_code=1)
    result = verify(world, tmp_path)
    assert result["classification"] == "ambiguous"
    assert "migration_error_ambiguous" in result["reasons"]


def test_lines_are_read_across_every_page(tmp_path: Path) -> None:
    lines = ALEMBIC["success_035"]["lines"]
    world = completed(tmp_path, "035", pages=[lines[:2], lines[2:4], lines[4:]])
    assert verify(world, tmp_path)["outcome"] == "applied"
    world = completed(tmp_path, "035", pages=[lines[:2], lines[2:4]])
    result = verify(world, tmp_path)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert "upgrade_lines_mismatch" in result["reasons"]


def test_a_leak_on_an_early_page_alerts(tmp_path: Path) -> None:
    lines = ALEMBIC["success_036"]["lines"]
    world = completed(tmp_path, "036", pages=[[LEAKED_HOST], list(lines)])
    result = verify(world, tmp_path)
    assert "log_exposure_suspected" in result["reasons"]


def test_an_unread_next_page_is_in_progress_with_its_request(tmp_path: Path) -> None:
    world = completed(tmp_path, "036")
    world.drop(
        "logs",
        "GetLogEvents",
        hook.log_events_request(MIGRATIONS_LOG, RUN_TASK_ID, "f/1"),
    )
    result = verify(world, tmp_path)
    assert result["outcome"] == "in_progress"
    assert result["reasons"] == ["log_events_incomplete"]
    assert result["next_requests"] == [
        {
            "service": "logs",
            "operation": "GetLogEvents",
            "params": hook.log_events_request(MIGRATIONS_LOG, RUN_TASK_ID, "f/1"),
        }
    ]


def test_a_page_without_a_forward_token_is_not_the_end(tmp_path: Path) -> None:
    world = ready_world(tmp_path, "036")
    world.record_run(tmp_path, pages=[[]])
    page = _events(*ALEMBIC["success_036"]["lines"])
    del page["nextForwardToken"]
    world.put(
        "logs",
        "GetLogEvents",
        hook.log_events_request(MIGRATIONS_LOG, RUN_TASK_ID),
        page,
    )
    result = verify(world, tmp_path)
    assert result["outcome"] == "in_progress"
    assert result["reasons"] == ["log_pages_incomplete"]


def test_a_chain_longer_than_the_page_cap_stops(tmp_path: Path) -> None:
    world = completed(
        tmp_path,
        "036",
        pages=[["INFO  [alembic.runtime.migration] Context impl PostgresqlImpl."]] * 21,
    )
    result = verify(world, tmp_path)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert "log_events_truncated" in result["reasons"]


def test_logs_not_yet_delivered_are_in_progress_then_empty_stops(
    tmp_path: Path,
) -> None:
    world = completed(tmp_path, "036", pages=[[]])
    world.captured_at = "2026-10-22T10:34:00Z"
    result = verify(world, tmp_path)
    assert result["outcome"] == "in_progress" and result["reasons"] == [
        "log_events_not_delivered"
    ]
    world.captured_at = "2026-10-22T10:36:00Z"
    result = verify(world, tmp_path)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert "migration_log_empty" in result["reasons"]


def test_a_missing_first_page_is_in_progress(tmp_path: Path) -> None:
    world = completed(tmp_path, "036")
    world.drop(
        "logs", "GetLogEvents", hook.log_events_request(MIGRATIONS_LOG, RUN_TASK_ID)
    )
    result = verify(world, tmp_path)
    assert result["outcome"] == "in_progress"
    assert result["reasons"] == ["log_events_not_read"]


def test_a_running_task_is_in_progress_until_the_watch_deadline(tmp_path: Path) -> None:
    world = ready_world(tmp_path, "036")
    running = {
        "taskArn": RUN_TASK,
        "taskDefinitionArn": world.migrations_td,
        "startedBy": world.window.started_by,
        "lastStatus": "RUNNING",
        "createdAt": RUN_CREATED,
        "containers": [{"name": "migrations", "lastStatus": "RUNNING"}],
    }
    world.record_run(tmp_path, task=running)
    world.captured_at = "2026-10-22T10:45:59Z"
    result = verify(world, tmp_path)
    assert result["outcome"] == "in_progress" and result["reasons"] == [
        "migration_task_not_stopped"
    ]
    assert [row["operation"] for row in result["next_requests"]] == [
        "DescribeTasks",
        "GetLogEvents",
    ]
    world.captured_at = "2026-10-22T10:46:01Z"
    result = verify(world, tmp_path)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert result["reasons"] == ["migration_watch_deadline_exceeded"]
    assert result["next_requests"] == []


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"taskDefinitionArn": MIGRATIONS_TD["035"]}, "task_identity_mismatch"),
        ({"startedBy": "someone"}, "task_identity_mismatch"),
        (
            {
                "overrides": {
                    "containerOverrides": [
                        {
                            "name": "migrations",
                            "command": ["alembic", "upgrade", "head"],
                        }
                    ]
                }
            },
            "task_overrides_present",
        ),
    ],
)
def test_the_described_task_must_be_the_window_task(
    change: dict[str, Any], reason: str, tmp_path: Path
) -> None:
    world = completed(tmp_path, "036")
    response = world.response(
        "ecs", "DescribeTasks", mw.describe_tasks_request([RUN_TASK])
    )
    response["tasks"][0].update(change)
    world.put("ecs", "DescribeTasks", mw.describe_tasks_request([RUN_TASK]), response)
    result = verify(world, tmp_path)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert reason in result["reasons"] and "evidence_mismatch" in result["alert_kinds"]


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (
            lambda p: p.__setitem__("propagateTags", "NONE"),
            "run_task_request_propagate_tags_present",
        ),
        (
            lambda p: p.__setitem__(
                "overrides",
                {
                    "containerOverrides": [
                        {"name": "migrations", "command": ["alembic", "upgrade", "036"]}
                    ]
                },
            ),
            "run_task_request_command_override_present",
        ),
        (
            lambda p: p.__setitem__("count", 2),
            "run_task_request_not_rendered_by_this_tool",
        ),
        (
            lambda p: p.__setitem__(
                "clientToken", "migration-window-036-run-" + "0" * 32
            ),
            "run_task_request_not_rendered_by_this_tool",
        ),
    ],
)
def test_a_recorded_run_the_tool_did_not_render_stops(
    mutate: Callable[[dict[str, Any]], Any], reason: str, tmp_path: Path
) -> None:
    world = completed(tmp_path, "036")
    mutate(world.records["migration"]["params"])
    result = verify(world, tmp_path)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert reason in result["reasons"]


def test_a_later_attempt_is_recognised(tmp_path: Path) -> None:
    world = ready_world(tmp_path, "036")
    world.record_run(tmp_path, attempt=3)
    result = verify(world, tmp_path)
    assert result["outcome"] == "applied" and result["attempt"] == 3


@pytest.mark.parametrize(
    "response",
    [
        {"tasks": [], "failures": [{"reason": "RESOURCE:ENI"}]},
        {"tasks": [{"taskArn": "not-an-arn"}], "failures": []},
        {"tasks": [{"taskArn": RUN_TASK}, {"taskArn": PRE_TASK}], "failures": []},
    ],
)
def test_run_task_that_started_nothing_stops(
    response: dict[str, Any], tmp_path: Path
) -> None:
    world = ready_world(tmp_path, "036")
    world.record_run(tmp_path, response=response)
    result = verify(world, tmp_path)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert result["reasons"] == ["run_task_failed"]


def test_run_task_response_for_another_revision_is_evidence_mismatch(
    tmp_path: Path,
) -> None:
    world = ready_world(tmp_path, "036")
    world.record_run(
        tmp_path,
        response={
            "tasks": [
                {
                    "taskArn": RUN_TASK,
                    "taskDefinitionArn": MIGRATIONS_TD["035"],
                    "startedBy": "neurostack-migration-window-036",
                }
            ],
            "failures": [],
        },
    )
    result = verify(world, tmp_path)
    assert "run_task_response_invalid" in result["reasons"]


def test_crash_recovery_verifies_a_named_task_without_a_run_record(
    tmp_path: Path,
) -> None:
    world = completed(tmp_path, "036")
    del world.records["migration"]
    with pytest.raises(mw.WindowError, match="migration_run_not_recorded"):
        verify(world, tmp_path)
    result = verify(world, tmp_path, task_arn=RUN_TASK)
    assert result["outcome"] == "applied"
    assert result["recovered_without_run_record"] is True and result["attempt"] is None
    response = world.response(
        "ecs", "DescribeTasks", mw.describe_tasks_request([RUN_TASK])
    )
    response["tasks"][0]["createdAt"] = "2026-10-22T09:59:59+00:00"
    world.put("ecs", "DescribeTasks", mw.describe_tasks_request([RUN_TASK]), response)
    result = verify(world, tmp_path, task_arn=RUN_TASK)
    assert "recovered_task_predates_snapshot" in result["reasons"]
    for bad in ("x", TASK_PREFIX + "Z" * 32):
        with pytest.raises(mw.WindowError, match="task_arn_invalid"):
            verify(world, tmp_path, task_arn=bad)


def test_a_named_task_must_match_the_recorded_run(tmp_path: Path) -> None:
    world = completed(tmp_path, "036")
    with pytest.raises(mw.WindowError, match="task_arn_does_not_match_recorded_run"):
        verify(world, tmp_path, task_arn=PRE_TASK)
    assert verify(world, tmp_path, task_arn=RUN_TASK)["outcome"] == "applied"


def test_render_verify_reads_names_the_task_and_its_stream(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = completed(tmp_path, "036")
    code, out, err = run_cli(
        ["render-verify-reads", *world.args(tmp_path), "--target", "migration"], capsys
    )
    assert code == 0, err
    document = json.loads(out)
    assert document["task_arn"] == RUN_TASK and document["from_recorded_run"] is True
    assert document["requests"] == [
        {
            "service": "ecs",
            "operation": "DescribeTasks",
            "params": {"cluster": "neurostack-staging", "tasks": [RUN_TASK]},
        },
        {
            "service": "logs",
            "operation": "GetLogEvents",
            "params": {
                "logGroupName": "/ecs/neurostack-staging/migrations",
                "logStreamName": f"migrations/migrations/{RUN_TASK_ID}",
                "startFromHead": True,
                "limit": 500,
            },
        },
    ]
    code, out, err = run_cli(
        ["render-verify-reads", *world.args(tmp_path), "--target", "pre-read"], capsys
    )
    assert code == 0, err
    document = json.loads(out)
    assert document["task_arn"] == PRE_TASK
    assert (
        document["requests"][1]["params"]["logStreamName"]
        == f"consulta/consulta/{PRE_TASK_ID}"
    )
    code, out, err = run_cli(
        ["render-verify-reads", *world.args(tmp_path), "--target", "post-read"], capsys
    )
    assert code == 2 and "post_read_not_recorded" in err
    code, out, err = run_cli(
        [
            "render-verify-reads",
            *world.args(tmp_path),
            "--target",
            "pre-read",
            "--task-arn",
            PRE_TASK,
        ],
        capsys,
    )
    assert code == 2 and "task_arn_only_for_the_migration" in err


@pytest.mark.parametrize(
    "case,code",
    [
        ("success_036", 0),
        ("refused_036", 4),
        ("lock_timeout_036", 4),
        ("deadlock_036", 4),
    ],
)
def test_verify_result_cli_exit_codes(
    case: str, code: int, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    golden = ALEMBIC[case]
    world = completed(tmp_path, "036", golden["lines"], exit_code=golden["exit_code"])
    exit_code, out, err = run_cli(["verify-result", *world.args(tmp_path)], capsys)
    assert exit_code == code, err
    assert FORBIDDEN_IN_OUTPUT.search(out) is None


# ---------------------------------------------------------------------------
# verify-pre-read / verify-post-read, on the real read-program output
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("window", ["035", "036", "037"])
def test_the_real_pre_and_post_reads_verify(window: str, tmp_path: Path) -> None:
    world = World(window)
    world.record_read(tmp_path, "pre")
    result = verify_read(world, tmp_path, "pre")
    assert result["outcome"] == "read_ok", result["reasons"]
    assert result["alembic_version"] == [mw.WINDOWS[window].before]
    world.record_read(tmp_path, "post")
    result = verify_read(world, tmp_path, "post")
    assert result["outcome"] == "verified", result["reasons"]
    assert result["alembic_version"] == [mw.WINDOWS[window].after]
    _assert_printable(result)


@pytest.mark.parametrize(
    "window,phase,case,reasons",
    [
        (
            "036",
            "post",
            "post_036_after_refusal",
            ["post_read_version_is_before_nothing_applied"],
        ),
        (
            "036",
            "post",
            "post_036_at_035",
            ["post_read_version_is_before_nothing_applied"],
        ),
        ("036", "pre", "pre_036_at_036", ["read_failed"]),
        ("037", "pre", "pre_037_at_037_migration_role", ["read_failed"]),
        (
            "037",
            "pre",
            "pre_037_at_036_app_role",
            ["read_verdict_not_ok:owners_and_privileges"],
        ),
        ("037", "pre", "pre_037_comment_red", ["read_verdict_not_ok:comments"]),
        (
            "036",
            "pre",
            "pre_035_at_032",
            ["read_alembic_version_mismatch", "read_begin_mismatch"],
        ),
        (
            "035",
            "post",
            "post_036_at_036",
            ["read_alembic_version_mismatch", "read_begin_mismatch"],
        ),
    ],
)
def test_real_reads_that_are_not_ok_stop_the_window(
    window: str, phase: str, case: str, reasons: list[str], tmp_path: Path
) -> None:
    golden = READS[case]
    world = World(window)
    world.record_read(tmp_path, phase, golden["lines"], exit_code=golden["exit_code"])
    result = verify_read(world, tmp_path, phase)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    for reason in reasons:
        assert reason in result["reasons"], result["reasons"]
    _assert_printable(result)


def test_a_failed_read_reports_only_the_class_and_sqlstate(tmp_path: Path) -> None:
    golden = READS["pre_036_at_036"]
    world = World("036")
    world.record_read(tmp_path, "pre", golden["lines"], exit_code=golden["exit_code"])
    result = verify_read(world, tmp_path, "pre")
    assert (
        result["error_class"] == "UndefinedColumnError"
        and result["sqlstate"] == "42703"
    )
    assert "read_exit_code_not_zero" in result["reasons"]


def test_a_red_037_verdict_is_reported_verbatim(tmp_path: Path) -> None:
    golden = READS["pre_037_comment_red"]
    world = World("037")
    world.record_read(tmp_path, "pre", golden["lines"])
    result = verify_read(world, tmp_path, "pre")
    red = [row for row in result["rows"] if row["verdict"] != "ok"]
    assert red == [
        {
            "check_no": 11,
            "check": "comments",
            "verdict": "comments the downgrade would not restore: table public.billing_migrations",
            "withheld": False,
        }
    ]


@pytest.mark.parametrize("index", range(9))
def test_any_nonzero_count_stops_the_036_preflight(index: int, tmp_path: Path) -> None:
    lines = READS["pre_036_at_035"]["lines"]
    name = mw.PREFLIGHT_036[index]
    world = World("036")
    world.record_read(tmp_path, "pre", _rows_changed(lines, index, [name, 1]))
    result = verify_read(world, tmp_path, "pre")
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert f"read_count_not_zero:{mw._slug(name)}" in result["reasons"]


@pytest.mark.parametrize("index", range(9))
def test_any_nonzero_count_stops_the_035_zero_reads(index: int, tmp_path: Path) -> None:
    lines = READS["pre_035_at_032"]["lines"]
    name = mw.ZERO_READS_035[index]
    world = World("035")
    world.record_read(tmp_path, "pre", _rows_changed(lines, index, [name, 2]))
    result = verify_read(world, tmp_path, "pre")
    assert f"read_count_not_zero:{mw._slug(name)}" in result["reasons"]


def test_a_037_table_that_still_exists_stops_the_post_read(tmp_path: Path) -> None:
    lines = READS["post_037_at_037"]["lines"]
    world = World("037")
    world.record_read(
        tmp_path, "post", _rows_changed(lines, 2, ["public.billing_migrations", False])
    )
    result = verify_read(world, tmp_path, "post")
    assert "read_relation_still_exists:public_billing_migrations" in result["reasons"]


def _line(seq: int, kind: str, **values: Any) -> str:
    return json.dumps(
        {
            "schema": "asaas_migration_window_read.v1",
            "seq": seq,
            "kind": kind,
            **values,
        },
        separators=(",", ":"),
    )


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda ls: ls + ["some foreign line"], "read_log_foreign_line"),
        (
            lambda ls: ls[:1]
            + ["{" + '"schema":"asaas_migration_window_read.v1",' + "oops"]
            + ls[1:],
            "read_line_malformed",
        ),
        (lambda ls: [ls[0], ls[2], ls[1], *ls[3:]], "read_sequence_broken"),
        (lambda ls: ls[:-1], "read_end_missing"),
        (lambda ls: ls[:2] + ls[3:], "read_sequence_broken"),
        (
            lambda ls: [ls[0].replace('"phase":"pre"', '"phase":"post"'), *ls[1:]],
            "read_begin_mismatch",
        ),
        (
            lambda ls: [
                re.sub(
                    r'"payload_sha256":"[0-9a-f]+"',
                    '"payload_sha256":"' + "0" * 64 + '"',
                    ls[0],
                ),
                *ls[1:],
            ],
            "read_begin_mismatch",
        ),
        (lambda ls: ls + [LEAKED_URL], "read_log_exposure_suspected"),
        (
            lambda ls: ls[:-1] + [ls[-1].replace('"rows":10', '"rows":9')],
            "read_row_count_mismatch",
        ),
        (lambda ls: ls[:-1] + [ls[-2]] + [ls[-1]], "read_sequence_broken"),
    ],
)
def test_malformed_read_output_stops(
    mutate: Callable[[list[str]], list[str]], reason: str, tmp_path: Path
) -> None:
    lines = mutate(list(READS["pre_036_at_035"]["lines"]))
    world = World("036")
    world.record_read(tmp_path, "pre", lines)
    result = verify_read(world, tmp_path, "pre")
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert reason in result["reasons"], result["reasons"]


def test_a_read_task_that_ran_another_command_stops(tmp_path: Path) -> None:
    # The read family: the task must have run exactly the read program.
    world = World("036")
    world.record_read(tmp_path, "pre")
    response = world.response(
        "ecs", "DescribeTasks", mw.describe_tasks_request([PRE_TASK])
    )
    response["tasks"][0]["overrides"] = {"containerOverrides": [{"name": "consulta"}]}
    world.put("ecs", "DescribeTasks", mw.describe_tasks_request([PRE_TASK]), response)
    result = verify_read(world, tmp_path, "pre")
    assert "read_task_command_mismatch" in result["reasons"]
    # Window 037's read revision: any command override replaced the readback.
    world = World("037")
    world.record_read(tmp_path, "pre")
    response = world.response(
        "ecs", "DescribeTasks", mw.describe_tasks_request([PRE_TASK])
    )
    response["tasks"][0]["overrides"] = {
        "containerOverrides": [
            {"name": "migrations", "command": ["alembic", "upgrade", "037"]}
        ]
    }
    world.put("ecs", "DescribeTasks", mw.describe_tasks_request([PRE_TASK]), response)
    result = verify_read(world, tmp_path, "pre")
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert "read_task_overrides_present" in result["reasons"]


def test_a_read_the_tool_did_not_render_stops(tmp_path: Path) -> None:
    world = World("036")
    world.record_read(tmp_path, "pre")
    world.records["pre_read"]["params"]["startedBy"] = "other"
    result = verify_read(world, tmp_path, "pre")
    assert "read_request_not_rendered_by_this_tool" in result["reasons"]
    world.records["pre_read"]["params"]["propagateTags"] = "NONE"
    assert (
        "run_task_request_propagate_tags_present"
        in verify_read(world, tmp_path, "pre")["reasons"]
    )


def test_a_read_task_still_running_is_in_progress_until_its_deadline(
    tmp_path: Path,
) -> None:
    world = World("036")
    world.record_read(tmp_path, "pre")
    response = world.response(
        "ecs", "DescribeTasks", mw.describe_tasks_request([PRE_TASK])
    )
    response["tasks"][0]["lastStatus"] = "RUNNING"
    world.put("ecs", "DescribeTasks", mw.describe_tasks_request([PRE_TASK]), response)
    world.captured_at = "2026-10-22T10:29:00Z"
    assert verify_read(world, tmp_path, "pre")["outcome"] == "in_progress"
    world.captured_at = "2026-10-22T10:30:01Z"
    result = verify_read(world, tmp_path, "pre")
    assert result["reasons"] == ["read_watch_deadline_exceeded"]


def test_verify_read_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    world = World("036")
    world.record_read(tmp_path, "pre")
    code, out, err = run_cli(["verify-pre-read", *world.args(tmp_path)], capsys)
    assert code == 0, err
    assert json.loads(out)["outcome"] == "read_ok"
    code, out, err = run_cli(["verify-post-read", *world.args(tmp_path)], capsys)
    assert code == 2 and "post_read_not_recorded" in err
    world.record_read(tmp_path, "post", READS["post_036_at_035"]["lines"])
    code, out, err = run_cli(["verify-post-read", *world.args(tmp_path)], capsys)
    assert code == 4
    assert "post_read_version_is_before_nothing_applied" in json.loads(out)["reasons"]


# ---------------------------------------------------------------------------
# render-snapshot-requests
# ---------------------------------------------------------------------------


def _snapshot_args(world: World, tmp_path: Path) -> list[str]:
    args = world.args(tmp_path, "snapshot.json")
    index = args.index("--snapshot-id")
    return args[:index] + args[index + 2 :]


@pytest.mark.parametrize("window", ["035", "036", "037"])
def test_snapshot_requests_name_the_window_and_carry_no_tags(
    window: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(window)
    code, out, err = run_cli(
        ["plan-snapshot", *_snapshot_args(world, tmp_path)], capsys
    )
    assert code == 0, (out, err)
    assert json.loads(out)["status"] == "ready"
    code, out, err = run_cli(
        ["render-snapshot-requests", *_snapshot_args(world, tmp_path)],
        capsys,
    )
    assert code == 0, err
    document = json.loads(out)
    name = SNAPSHOT_ID[window]
    assert document["snapshot_identifier"] == name
    assert document["requests"] == [
        {
            "service": "rds",
            "operation": "CreateDBSnapshot",
            "params": {
                "DBInstanceIdentifier": "neurostack-staging-db",
                "DBSnapshotIdentifier": name,
            },
        },
        {
            "service": "rds",
            "operation": "DescribeDBSnapshots",
            "params": {"DBSnapshotIdentifier": name},
        },
    ]
    assert "Tags" not in out
    assert document["poll"]["until_status"] == "available"
    assert "Delete" not in out


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (
            lambda w: w.put(
                "ecs",
                "ListTasks",
                mw.list_family_tasks_request(mw.MIGRATIONS_FAMILY),
                {"taskArns": [RUN_TASK]},
            ),
            "migrations_task_running",
        ),
        (
            lambda w: w.put(
                "ecs",
                "DescribeServices",
                mw.describe_services_request(),
                _service(w.service_td, runningCount=1),
            ),
            "service_not_steady",
        ),
        (
            lambda w: w.put(
                "ecs",
                "DescribeTaskDefinition",
                mw.describe_task_definition_request(w.migrations_td),
                _one_container_definition(
                    w.migrations_td,
                    "neurostack-staging-migrations",
                    _migrations_container("037"),
                ),
            ),
            "migrations_command_not_this_window",
        ),
        (lambda w: setattr(w, "captured_at", "2026-10-22T10:00:00Z"), "readback_stale"),
    ],
)
def test_the_snapshot_step_cross_checks_the_deploy_state(
    mutate: Callable[[World], Any],
    reason: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world = World("036")
    mutate(world)
    args = _snapshot_args(world, tmp_path)
    code, out, err = run_cli(["plan-snapshot", *args], capsys)
    assert code == 2 and reason in json.loads(out)["blocks"]
    code, out, err = run_cli(["render-snapshot-requests", *args], capsys)
    assert code == 2 and out == "" and "snapshot_plan_blocked" in err


def test_the_snapshot_step_refuses_after_a_pre_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = ready_world(tmp_path, "036")
    args = _snapshot_args(world, tmp_path)
    code, out, _err = run_cli(["plan-snapshot", *args], capsys)
    assert code == 2
    assert "pre_read_recorded_before_the_snapshot" in json.loads(out)["blocks"]


def test_the_snapshot_name_follows_the_utc_date() -> None:
    late = datetime.fromisoformat("2026-10-22T23:59:59-03:00")
    document = mw.render_snapshot_requests(mw.WINDOW_036, now=late)
    assert document["snapshot_identifier"] == "pre-036-2026-10-23"
    with pytest.raises(mw.WindowError, match="now_invalid"):
        mw.render_snapshot_requests(mw.WINDOW_036, now=datetime(2026, 10, 22))


def test_snapshot_requests_need_an_explicit_window(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rest = _snapshot_args(World("036"), tmp_path)[2:]
    for argv in (
        ["render-snapshot-requests", *rest],
        ["render-snapshot-requests", "--window", "038", *rest],
        ["render-snapshot-requests", "--window", "head", *rest],
        ["render-snapshot-requests", "--window", "036", *rest, "--snapshot-id", "x"],
        ["render-snapshot-requests", "--window", "036", "--window", "036", *rest],
    ):
        code, out, err = run_cli(argv, capsys)
        assert code == 2 and out == "" and "blocked: usage" in err


# ---------------------------------------------------------------------------
# Readback file, arguments, output guard, standalone run
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (
            lambda d: d.__setitem__("schema_version", "asaas_deploy_hook_readback.v1"),
            "readback_file_shape_invalid",
        ),
        (
            lambda d: d.__setitem__("account_id", "488460142431"),
            "readback_file_shape_invalid",
        ),
        (lambda d: d.__setitem__("region", "us-east-2"), "readback_file_shape_invalid"),
        (
            lambda d: d.__setitem__("captured_at_utc", "2026-10-22T10:29:00"),
            "readback_file_shape_invalid",
        ),
        (lambda d: d.__setitem__("window", "037"), "readback_file_window_mismatch"),
        (lambda d: d.pop("window"), "readback_file_shape_invalid"),
        (lambda d: d.__setitem__("run_task", {}), "readback_file_shape_invalid"),
        (lambda d: d.__setitem__("migration", None), "readback_file_migration_invalid"),
        (
            lambda d: d.__setitem__("pre_read", {"params": {}}),
            "readback_file_pre_read_invalid",
        ),
        (
            lambda d: d["reads"].append(
                {
                    "service": "ecs",
                    "operation": "UpdateService",
                    "params": {},
                    "response": {},
                }
            ),
            "readback_file_operation_not_allowed",
        ),
        (
            lambda d: d["reads"].append(
                {
                    "service": "secretsmanager",
                    "operation": "GetSecretValue",
                    "params": {},
                    "response": {},
                }
            ),
            "readback_file_operation_not_allowed",
        ),
        (
            lambda d: d["reads"].append(copy.deepcopy(d["reads"][0])),
            "readback_file_duplicate_read",
        ),
        (
            lambda d: d["reads"].append(
                {
                    "service": "ecs",
                    "operation": "ListTasks",
                    "params": {"x": 1},
                    "response": {"SecretString": "x"},
                }
            ),
            "secret_material_in_readback",
        ),
        (lambda d: d["reads"][0].__setitem__("extra", 1), "readback_file_read_invalid"),
    ],
)
def test_the_readback_file_is_strict(
    mutate: Callable[[dict[str, Any]], Any], reason: str, tmp_path: Path
) -> None:
    world = World("036")
    document = world.document()
    mutate(document)
    path = tmp_path / "readback.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(hook.HookError, match=reason):
        mw.load_readback(path, mw.WINDOW_036)


@pytest.mark.parametrize("text", ['{"a": 1, "a": 2}', "[NaN]", "not json", ""])
def test_the_readback_json_is_strict(text: str, tmp_path: Path) -> None:
    path = tmp_path / "readback.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(mw.WindowError, match="readback_file_unreadable"):
        mw.load_readback(path, mw.WINDOW_036)


@pytest.mark.parametrize(
    "field,value,reason",
    [
        (
            "service_task_definition",
            TD_PREFIX + "neurostack-staging-migrations:5",
            "service_task_definition_invalid",
        ),
        (
            "migrations_task_definition",
            TD_PREFIX + "neurostack-staging-migrations:3",
            "migrations_task_definition_invalid",
        ),
        (
            "read_task_definition",
            TD_PREFIX + "neurostack-staging-migrations:5",
            "read_task_definition_invalid",
        ),
        ("snapshot_id", "pre-037-2026-10-22", "snapshot_id_invalid"),
    ],
)
def test_context_arguments_are_validated(field: str, value: str, reason: str) -> None:
    with pytest.raises(mw.WindowError, match=reason):
        World("036").context(**{field: value})


@pytest.mark.parametrize("command", mw._CONTEXT_COMMANDS)
def test_every_readback_subcommand_requires_an_explicit_window(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World("036")
    args = world.args(tmp_path)
    without = args[2:]
    extra = ["--target", "migration"] if command == "render-verify-reads" else []
    code, out, err = run_cli([command, *without, *extra], capsys)
    assert code == 2 and out == "" and "blocked: usage" in err
    for bad in ("", "038", "head", "035:037", "35"):
        code, out, err = run_cli([command, "--window", bad, *without, *extra], capsys)
        assert code == 2 and out == "" and "blocked: usage" in err


def test_a_readback_of_another_window_is_refused_by_the_cli(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = ready_world(tmp_path, "036")
    args = world.args(tmp_path)
    args[1] = "037"
    args[args.index("--migrations-task-definition") + 1] = MIGRATIONS_TD["037"]
    args[args.index("--snapshot-id") + 1] = SNAPSHOT_ID["037"]
    code, out, err = run_cli(["plan", *args], capsys)
    assert code == 2 and out == ""
    assert "migrations_read_task_definition_required" in err
    args += ["--migrations-read-task-definition", MIGRATIONS_READ_TD]
    code, out, err = run_cli(["plan", *args], capsys)
    assert code == 2 and out == "" and "readback_file_window_mismatch" in err


@pytest.mark.parametrize(
    "payload",
    [
        {"x": "rollback"},
        {"x": "roll back to revision 8"},
        {"x": "UpdateService"},
        {"x": "update-service"},
        {"x": "RegisterTaskDefinition"},
        {"x": "DeregisterTaskDefinition"},
        {"x": "StopTask"},
        {"x": "DeleteDBSnapshot"},
        {"x": "propagateTags"},
        {"x": "alembic upgrade head"},
        {"x": "alembic upgrade +1"},
        {"x": "password"},
        {"x": LEAKED_URL},
        {"command": ["python", "-c", "import os; os.system('x')  # rollback"]},
        {
            "command": [
                "python",
                "-c",
                mw.read_program(mw.WINDOW_036, mw.WINDOW_036.pre_read).replace(
                    "60s", "61s"
                ),
            ]
        },
    ],
)
def test_the_output_guard_refuses_forbidden_output(payload: dict[str, Any]) -> None:
    with pytest.raises(mw.WindowError, match="output_forbidden"):
        mw._emit(payload)


def test_the_output_guard_exempts_only_the_exact_read_program(
    capsys: pytest.CaptureFixture[str],
) -> None:
    program = mw.read_program(mw.WINDOW_037, mw.WINDOW_037.pre_read)
    mw._emit({"command": ["python", "-c", program], "rollback_target_revision": 9})
    assert "t.rollback()" in capsys.readouterr().out


def test_script_runs_standalone_without_repository_on_the_path(tmp_path: Path) -> None:
    def run(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-I", "-B", str(SCRIPT), *argv],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=120,
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        )

    windows = run("windows")
    assert windows.returncode == 0, windows.stderr
    assert [row["window"] for row in json.loads(windows.stdout)["windows"]] == [
        "035",
        "036",
        "037",
    ]
    # The real clock judges the fixture's capture (2026-10-22): the plan runs
    # standalone (manifest read through the planner) and refuses on freshness
    # unless the system clock happens to sit inside the window.
    world = ready_world(tmp_path, "036")
    plan_run = run("plan", *world.args(tmp_path))
    document = json.loads(plan_run.stdout)
    assert plan_run.returncode == (0 if document["status"] == "ready" else 2)
    assert document["window"] == "036"
    assert (
        ("readback_stale" in document["blocks"])
        or ("readback_captured_in_future" in document["blocks"])
        or document["status"] == "ready"
    )
    refused = run("plan", *world.args(tmp_path), "--now", NOW)
    assert refused.returncode == 2 and refused.stdout == ""
    assert "blocked: usage" in refused.stderr


# ---------------------------------------------------------------------------
# Golden renderings and single sources
# ---------------------------------------------------------------------------

GOLDEN_REQUESTS = FIXTURES / "rendered_requests.json"


def _normalized(request: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(request)
    for row in (out.get("overrides") or {}).get("containerOverrides", []):
        row["command"] = mw.normalized_read_command(row["command"])
    return out


def rendered_golden(tmp_path: Path) -> dict[str, Any]:
    golden: dict[str, Any] = {}
    for name in mw.WINDOWS:
        world = ready_world(tmp_path, name)
        post = world.read_context(tmp_path, "post")
        pre_world = World(name)
        pre = pre_world.read_context(tmp_path, "pre")
        golden[name] = {
            "snapshot": mw.render_snapshot_requests(world.window, now=NOW_DT),
            "pre_read": _normalized(
                mw.build_read_request(
                    world.window, "pre", pre.network, pre.target, attempt=1
                )
            ),
            "run_task": render_run(world, tmp_path),
            "post_read": _normalized(
                mw.build_read_request(
                    world.window, "post", post.network, post.target, attempt=1
                )
            ),
        }
        if mw.needs_read_revision(world.window):
            # The command window 037's read revision bakes (planner
            # --purpose read), normalized like every read command.
            golden[name]["read_revision_command"] = mw.normalized_read_command(
                ["python", "-c", mw.read_program(world.window, world.window.pre_read)]
            )
    return golden


def test_rendered_requests_match_the_golden_fixture(tmp_path: Path) -> None:
    expected = json.loads(GOLDEN_REQUESTS.read_text(encoding="utf-8"))
    assert rendered_golden(tmp_path) == expected["windows"]


def test_golden_fixture_requests_hold_the_invariants() -> None:
    expected = json.loads(GOLDEN_REQUESTS.read_text(encoding="utf-8"))["windows"]
    for name, rows in expected.items():
        run = rows["run_task"]
        assert set(run) == LIVE_ACCEPTED_RUN_TASK_KEYS | {"platformVersion"}
        assert "overrides" not in run and "propagateTags" not in run
        assert run["taskDefinition"] == MIGRATIONS_TD[name]
        for phase in ("pre_read", "post_read"):
            assert "propagateTags" not in rows[phase]
            if (name, phase) == ("037", "pre_read"):
                assert "overrides" not in rows[phase]
                assert rows[phase]["taskDefinition"] == MIGRATIONS_READ_TD
                command = rows["read_revision_command"]
            else:
                assert rows[phase]["taskDefinition"] == READ_TD
                command = rows[phase]["overrides"]["containerOverrides"][0]["command"]
            assert command[:2] == ["python", "-c"] and "<payload:" in command[2]
            assert "alembic" not in command[2].lower()


def _runbook_block(after: str) -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    start = text.index("```sql\n", text.index(after)) + len("```sql\n")
    return text[start : text.index("```", start)]


def _sql_without_comments(path: str) -> str:
    lines = (ROOT / path).read_text(encoding="utf-8").splitlines(keepends=True)
    return "".join(line for line in lines if not line.startswith("--"))


def test_the_036_preflight_file_is_the_runbook_query() -> None:
    block = _runbook_block("### Preflight (SELECT only, immediately before the run)")
    assert block == _sql_without_comments("scripts/migration_036_preflight.sql")


def test_the_035_zero_reads_file_is_the_runbook_query() -> None:
    block = _runbook_block(
        "The zero reads are `scripts/migration_033_035_zero_reads.sql`"
    )
    dedented = "".join(
        line[3:] if line.startswith("   ") else line
        for line in block.splitlines(keepends=True)
    )
    assert dedented == _sql_without_comments("scripts/migration_033_035_zero_reads.sql")


@pytest.mark.parametrize(
    "spec",
    [
        mw.WINDOW_035.pre_read,
        mw.WINDOW_036.pre_read,
        mw.WINDOW_037.pre_read,
        mw.WINDOW_037.post_read,
    ],
)
def test_each_read_file_names_exactly_the_checks_the_verifier_expects(
    spec: mw.ReadSpec,
) -> None:
    sql = _sql_without_comments(str(spec.sql_file))
    if spec.rule == mw.RULE_VERDICT_OK:
        names = re.findall(r"^    \(\d+, '([a-z_ ]+)'", sql, re.M)
        assert tuple(names) == spec.checks
    elif spec.rule == mw.RULE_RELATIONS_GONE:
        assert sorted(re.findall(r"'(public\.[a-z_]+)'", sql)) == list(spec.checks)
    else:
        assert tuple(re.findall(r"SELECT '([a-z_ 0-9]+)'", sql)) == spec.checks


# Any statement word other than a read, case-insensitive.
_NOT_A_READ = re.compile(
    r"\b(?:insert|update|delete|merge|drop|alter|create|truncate|grant|revoke|"
    r"comment|copy|call|do|execute|prepare|set|reset|lock|vacuum|analyze|"
    r"reindex|cluster|refresh|listen|notify|discard|begin|commit|rollback|"
    r"savepoint|release|security|into)\b",
    re.IGNORECASE,
)


def _statement_text(sql: str) -> str:
    """The SQL without comments and string literals (what PostgreSQL parses
    as keywords); literals and comments may say anything."""

    without_comments = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"'(?:[^']|'')*'", "''", without_comments)


@pytest.mark.parametrize(
    "window,phase",
    [(w, p) for w in ("035", "036", "037") for p in ("pre", "post")],
)
def test_every_decoded_payload_is_one_select_only_statement(
    window: str, phase: str
) -> None:
    """Semantic, on what the task actually runs: the decoded payload of the
    rendered program is a SELECT (or WITH ... SELECT) and nothing else, and
    the program wraps it in a READ ONLY transaction that is rolled back."""

    spec = mw.WINDOWS[window].read(phase)
    program = mw.read_program(mw.WINDOWS[window], spec)
    header = mw._READ_HEADER.match(program)
    assert header is not None
    payload = json.loads(mw._decompressed(header.group("payload")) or b"")
    assert set(payload) == {"sql", "version"}
    for sql in (payload["version"], payload["sql"]):
        if not sql:
            continue
        statement = _statement_text(sql).strip()
        assert re.match(r"^(?:select|with)\b", statement, re.IGNORECASE), statement[:80]
        assert _NOT_A_READ.search(statement) is None, _NOT_A_READ.search(statement)
        # One statement: at most a trailing semicolon.
        assert ";" not in statement.rstrip().rstrip(";")
    assert payload["version"] == "SELECT version_num FROM public.alembic_version"
    body = program[header.end() :]
    assert 'c.transaction(isolation="repeatable_read",readonly=True)' in body
    assert "await t.rollback()" in body and "commit" not in body.lower()
    assert 's["default_transaction_read_only"]="on"' in body
    assert header.group("settings").endswith("SET LOCAL search_path=public")


@pytest.mark.parametrize("spec", [mw.WINDOW_035.pre_read, mw.WINDOW_036.pre_read])
def test_the_counted_tables_are_schema_qualified_and_rls_is_counted(
    spec: mw.ReadSpec,
) -> None:
    statement = _statement_text(_sql_without_comments(str(spec.sql_file)))
    tables = re.findall(r"\bFROM\s+([a-z_.]+)", statement, re.IGNORECASE)
    assert tables and all(
        name.startswith(("public.", "pg_catalog.")) for name in tables
    ), tables
    sql = _sql_without_comments(str(spec.sql_file))
    counted = {name for name in tables if name.startswith("public.")}
    rls = re.search(r"oid IN \(([^)]*)\)", sql)
    assert rls is not None
    assert set(re.findall(r"'(public\.[a-z_]+)'::regclass", rls.group(1))) == counted
    assert "relrowsecurity OR relforcerowsecurity" in sql
    assert spec.checks[-1] == "row level security on counted tables"


def test_rls_on_a_counted_table_stops_the_035_pre_read(tmp_path: Path) -> None:
    golden = READS["pre_035_rls_red"]
    world = World("035")
    world.record_read(tmp_path, "pre", golden["lines"])
    result = verify_read(world, tmp_path, "pre")
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert result["reasons"] == [
        "read_count_not_zero:row_level_security_on_counted_tables"
    ]


def test_the_runbook_window_table_is_the_module_table() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    section = text[
        text.index("## Migration windows: the run tooling") : text.index(
            "## Production wiring (Group 9 D3/D4)"
        )
    ]
    rows = [
        line for line in section.splitlines() if re.match(r"^\| `03[567]` \|", line)
    ]
    assert len(rows) == 3
    for row, window in zip(rows, mw.WINDOWS.values()):
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        labels = [label for _phase, label in window.plan_steps]
        assert cells[0] == f"`{window.name}`"
        assert cells[1] == f"`{window.manifest_step}`"
        assert cells[2] == f"{labels[0]}-{labels[-1]}"
        assert cells[3] == f"`{window.command_text}`"
        assert cells[4] == f"`{window.before}`, `{window.after}`"
        assert cells[5] == f"`{window.snapshot_pattern}`"
        assert cells[6].startswith(f"`{window.pre_read.sql_file}`")
        role = (
            "**migration role**"
            if window.pre_read.role == "migrations"
            else "application role"
        )
        assert role in cells[6]
    sequence = [
        line
        for line in section.splitlines()
        if re.match(r"^\| (register|snapshot|pre-read|run|verify|post-read) \|", line)
    ]
    assert len(sequence) == 6
    for line, phase in zip(sequence, mw.PHASES):
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        assert cells[1] == " / ".join(
            dict(w.plan_steps)[phase] for w in mw.WINDOWS.values()
        )
    for subcommand in (
        "render-snapshot-requests",
        "plan-pre-read",
        "render-pre-read",
        "verify-pre-read",
        "`plan`",
        "render-run-task",
        "render-verify-reads",
        "verify-result",
        "plan-post-read",
        "render-post-read",
        "verify-post-read",
    ):
        assert subcommand in section, subcommand
    assert UPGRADE_HEAD.search(section) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.__setitem__("overrides", []),
        lambda p: p.__setitem__("overrides", [1, {"containerOverrides": []}]),
        lambda p: p.__setitem__("overrides", {"containerOverrides": "x"}),
        lambda p: p.__setitem__("overrides", {"containerOverrides": [None, 1, "x"]}),
        lambda p: p.__setitem__("networkConfiguration", None),
        lambda p: p.clear(),
    ],
)
@pytest.mark.parametrize("record", ["pre_read", "migration"])
def test_malformed_recorded_requests_never_crash_a_verifier(
    mutate: Callable[[dict[str, Any]], Any], record: str, tmp_path: Path
) -> None:
    world = completed(tmp_path, "036")
    mutate(world.records[record]["params"])
    result = (
        verify_read(world, tmp_path, "pre")
        if record == "pre_read"
        else verify(world, tmp_path)
    )
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert "evidence_mismatch" in result["alert_kinds"]


@pytest.mark.parametrize(
    "response",
    [
        {"tasks": None, "failures": []},
        {
            "tasks": [{"taskArn": RUN_TASK, "containers": "x", "overrides": []}],
            "failures": [],
        },
        {
            "tasks": [
                {"taskArn": RUN_TASK, "lastStatus": "STOPPED", "containers": [None]}
            ],
            "failures": [],
        },
        {"__error_code__": "AccessDeniedException"},
    ],
)
def test_malformed_described_tasks_never_crash_verify_result(
    response: dict[str, Any], tmp_path: Path
) -> None:
    world = completed(tmp_path, "036")
    world.put("ecs", "DescribeTasks", mw.describe_tasks_request([RUN_TASK]), response)
    result = verify(world, tmp_path)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"


def test_read_log_pages_are_followed_and_a_missing_page_is_in_progress(
    tmp_path: Path,
) -> None:
    lines = READS["pre_036_at_035"]["lines"]
    world = World("036")
    world.record_read(tmp_path, "pre", pages=[lines[:4], lines[4:9], lines[9:]])
    assert verify_read(world, tmp_path, "pre")["outcome"] == "read_ok"
    log = hook.LogTarget(
        group="/ecs/neurostack-staging/migrations",
        stream_prefix="consulta",
        container="consulta",
    )
    world.drop("logs", "GetLogEvents", hook.log_events_request(log, PRE_TASK_ID, "f/2"))
    result = verify_read(world, tmp_path, "pre")
    assert result["outcome"] == "in_progress"
    assert result["reasons"] == ["log_events_incomplete"]
    assert result["next_requests"] == [
        {
            "service": "logs",
            "operation": "GetLogEvents",
            "params": hook.log_events_request(log, PRE_TASK_ID, "f/2"),
        }
    ]
    world = World("036")
    world.record_read(tmp_path, "pre", pages=[lines[:4], lines[4:9]])
    result = verify_read(world, tmp_path, "pre")
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert "read_end_missing" in result["reasons"]


# ---------------------------------------------------------------------------
# Window 037's dedicated read revision: the clean boundary
# ---------------------------------------------------------------------------


def test_only_037_takes_a_read_revision_and_never_the_migrate_one() -> None:
    assert [name for name, w in mw.WINDOWS.items() if mw.needs_read_revision(w)] == [
        "037"
    ]
    world = World("037")
    with pytest.raises(
        mw.WindowError, match="migrations_read_task_definition_required"
    ):
        world.context(migrations_read_task_definition=None)
    with pytest.raises(mw.WindowError, match="read_and_migrate_revisions_must_differ"):
        world.context(migrations_read_task_definition=MIGRATIONS_TD["037"])
    for bad in (TD_PREFIX + "neurostack-staging-migrations:3", READ_TD):
        with pytest.raises(mw.WindowError, match="migrations_task_definition_invalid"):
            world.context(migrations_read_task_definition=bad)
    for name in ("035", "036"):
        with pytest.raises(
            mw.WindowError, match="migrations_read_task_definition_not_used"
        ):
            World(name).context(migrations_read_task_definition=MIGRATIONS_READ_TD)


def _plan_pre_read(world: World, tmp_path: Path) -> dict[str, Any]:
    document, _context = mw.evaluate_read_plan(
        world.readback(tmp_path), world.context(), "pre", now=NOW_DT
    )
    return document


@pytest.mark.parametrize(
    "change,reason",
    [
        (
            {"environment": [{"name": "ALEMBIC_CONFIG", "value": "/app/alembic.ini"}]},
            "read_revision_names_the_migration_tool",
        ),
        (
            {"environment": [{"name": "MODE", "value": "alembic"}]},
            "read_revision_names_the_migration_tool",
        ),
        ({"entryPoint": ["alembic"]}, "read_revision_names_the_migration_tool"),
        (
            {"command": ["alembic", "upgrade", "037"]},
            "read_revision_command_not_the_readback",
        ),
        (
            {
                "command": [
                    "python",
                    "-c",
                    mw.read_program(mw.WINDOW_036, mw.WINDOW_036.pre_read),
                ]
            },
            "read_revision_command_not_the_readback",
        ),
        (
            {"secrets": [{"name": "DATABASE_URL", "value": "x"}]},
            "read_revision_database_secret_mismatch",
        ),
        (
            {"secrets": [{"name": "DATABASE_URL", "valueFrom": APP_DB_ARN}]},
            "read_revision_database_secret_mismatch",
        ),
        (
            {"image": f"{REGISTRY}/neurostack-staging@sha256:" + "4" * 64},
            "read_revision_image_differs",
        ),
        (
            {"image": OLD_IMAGE},
            "read_revision_image_not_digest_pinned",
        ),
    ],
)
def test_the_037_pre_read_refuses_a_read_revision_that_is_not_read_only(
    change: dict[str, Any], reason: str, tmp_path: Path
) -> None:
    world = World("037")
    world.put(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(MIGRATIONS_READ_TD),
        _read_revision_definition(**change),
    )
    document = _plan_pre_read(world, tmp_path)
    assert document["status"] == "blocked"
    assert reason in document["blocks"], document["blocks"]


def test_swapped_purposes_are_refused_on_both_sides(tmp_path: Path) -> None:
    world = World("037")
    migrate = world.response(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(MIGRATIONS_TD["037"]),
    )
    read = world.response(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(MIGRATIONS_READ_TD),
    )

    def swapped(response: dict[str, Any], arn: str) -> dict[str, Any]:
        out = copy.deepcopy(response)
        out["taskDefinition"]["taskDefinitionArn"] = arn
        out["taskDefinition"]["revision"] = int(arn.rsplit(":", 1)[1])
        return out

    world.put(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(MIGRATIONS_TD["037"]),
        swapped(read, MIGRATIONS_TD["037"]),
    )
    world.put(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(MIGRATIONS_READ_TD),
        swapped(migrate, MIGRATIONS_READ_TD),
    )
    blocks = _plan_pre_read(world, tmp_path)["blocks"]
    assert "read_revision_command_not_the_readback" in blocks
    assert "read_revision_names_the_migration_tool" in blocks
    assert "migrations_revision_is_a_read_revision" in blocks
    assert "migrations_command_not_this_window" in blocks


def test_the_run_refuses_the_read_revision_as_the_migration(tmp_path: Path) -> None:
    world = ready_world(tmp_path, "037")
    world.put(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(MIGRATIONS_TD["037"]),
        _one_container_definition(
            MIGRATIONS_TD["037"],
            "neurostack-staging-migrations",
            _read_revision_container(),
        ),
    )
    blocks = _blocks(world, tmp_path)
    assert "migrations_revision_is_a_read_revision" in blocks
    assert "migrations_command_not_this_window" in blocks
    with pytest.raises(mw.WindowError, match="plan_blocked"):
        render_run(world, tmp_path)


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (
            lambda r: r.__setitem__(
                "overrides",
                {
                    "containerOverrides": [
                        {
                            "name": "migrations",
                            "command": ["python", "-c", "print(1)"],
                        }
                    ]
                },
            ),
            "override_forbidden_on_the_read_revision",
        ),
        (
            lambda r: r.__setitem__("overrides", {"containerOverrides": []}),
            "override_forbidden_on_the_read_revision",
        ),
        (lambda r: r.__setitem__("propagateTags", "NONE"), "propagate_tags"),
        (lambda r: r.__setitem__("taskDefinition", MIGRATIONS_TD["037"]), "invariant"),
        (
            lambda r: r.__setitem__("startedBy", "neurostack-migration-window-037"),
            "read_request_invariant",
        ),
        (lambda r: r.__setitem__("tags", []), "key_set"),
    ],
)
def test_the_037_pre_read_request_never_carries_an_override(
    mutate: Callable[[dict[str, Any]], Any], reason: str, tmp_path: Path
) -> None:
    world = World("037")
    context = world.read_context(tmp_path, "pre")
    request = mw.build_read_request(
        world.window, "pre", context.network, context.target, attempt=1
    )
    assert "overrides" not in request
    mutate(request)
    with pytest.raises(mw.WindowError, match=reason):
        mw.assert_read_request_invariants(request, world.window, "pre", context.target)


def test_an_override_on_a_read_family_target_needs_the_read_family(
    tmp_path: Path,
) -> None:
    world = World("036")
    context = world.read_context(tmp_path, "pre")
    misplaced = mw.TaskTarget(
        task_definition_arn=MIGRATIONS_TD["036"],
        container=context.target.container,
        log=context.target.log,
        image=context.target.image,
    )
    with pytest.raises(mw.WindowError, match="override_only_on_the_read_family"):
        mw.build_read_request(
            world.window, "pre", context.network, misplaced, attempt=1
        )


def test_a_recorded_037_pre_read_with_an_override_is_not_this_tools(
    tmp_path: Path,
) -> None:
    world = World("037")
    world.record_read(tmp_path, "pre")
    world.records["pre_read"]["params"]["overrides"] = {
        "containerOverrides": [
            {"name": "migrations", "command": ["alembic", "upgrade", "037"]}
        ]
    }
    result = verify_read(world, tmp_path, "pre")
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert "read_request_not_rendered_by_this_tool" in result["reasons"]


def test_the_read_program_never_names_the_migration_tool() -> None:
    assert "alembic" not in mw._READ_PROGRAM_BODY.lower()
    for window in mw.WINDOWS.values():
        for spec in (window.pre_read, window.post_read):
            program = mw.read_program(window, spec)
            assert "alembic" not in program.lower()
            assert json.loads(mw.read_payload(spec))["version"] == mw.VERSION_SQL


# ---------------------------------------------------------------------------
# Review fix loop: F1 a run that already stopped is not a fresh start
# ---------------------------------------------------------------------------

EARLIER_RUN = TASK_PREFIX + "7" * 32


def _stopped_run(
    world: World,
    *,
    stopped_at: Any,
    started_by: str | None = None,
    exit_code: Any = 1,
) -> None:
    world.put(
        "ecs",
        "ListTasks",
        mw.list_family_tasks_request(mw.MIGRATIONS_FAMILY, "STOPPED"),
        {"taskArns": [EARLIER_RUN]},
    )
    task: dict[str, Any] = {
        "taskArn": EARLIER_RUN,
        "taskDefinitionArn": world.migrations_td,
        "startedBy": started_by or world.window.started_by,
        "lastStatus": "STOPPED",
        "desiredStatus": "STOPPED",
        "createdAt": "2026-10-22T10:05:00+00:00",
        "containers": [{"name": "migrations", "exitCode": exit_code}],
    }
    if stopped_at is not None:
        task["stoppedAt"] = stopped_at
    world.put(
        "ecs",
        "DescribeTasks",
        mw.describe_tasks_request([EARLIER_RUN]),
        {"tasks": [task], "failures": []},
    )


@pytest.mark.parametrize(
    "stopped_at",
    ["2026-10-22T10:21:00+00:00", "2026-10-22T10:20:00+00:00", None],
)
def test_a_window_run_that_stopped_after_the_pre_read_blocks_a_new_run(
    stopped_at: Any, tmp_path: Path
) -> None:
    """Repro E1: the first run failed (55P03), its record was lost; the plan
    must not be ready again on the same pre-read, so attempt 2 is never
    rendered from the old evidence."""

    world = ready_world(tmp_path, "036")
    _stopped_run(world, stopped_at=stopped_at)
    document = plan(world, tmp_path)
    assert document["status"] == "blocked"
    assert "window_task_stopped_after_pre_read" in document["blocks"]
    assert [row["task_arn"] for row in document["earlier_window_tasks"]] == [
        EARLIER_RUN
    ]
    with pytest.raises(mw.WindowError, match="window_task_stopped_after_pre_read"):
        render_run(world, tmp_path, attempt=2)


def test_a_retry_with_a_pre_read_taken_after_the_stop_is_allowed(
    tmp_path: Path,
) -> None:
    world = ready_world(tmp_path, "036")
    _stopped_run(world, stopped_at="2026-10-22T10:19:59+00:00")
    document = plan(world, tmp_path)
    assert document["status"] == "ready", document["blocks"]
    assert "no_window_task_stopped_after_pre_read" in document["checks"]
    assert [row["task_arn"] for row in document["earlier_window_tasks"]] == [
        EARLIER_RUN
    ]


def test_a_stopped_read_task_of_the_family_is_not_an_earlier_run(
    tmp_path: Path,
) -> None:
    world = ready_world(tmp_path, "037")
    _stopped_run(
        world,
        stopped_at="2026-10-22T10:22:00+00:00",
        started_by=world.window.read_started_by("pre"),
    )
    assert plan(world, tmp_path)["status"] == "ready"


@pytest.mark.parametrize(
    "response,reason",
    [
        (
            {"taskArns": [], "nextToken": "more"},
            "migrations_stopped_tasks_listing_incomplete",
        ),
        (
            {"__error_code__": "AccessDeniedException"},
            "migrations_stopped_tasks_listing_unreadable",
        ),
    ],
)
def test_the_stopped_listing_must_be_complete(
    response: dict[str, Any], reason: str, tmp_path: Path
) -> None:
    world = ready_world(tmp_path, "036")
    world.put(
        "ecs",
        "ListTasks",
        mw.list_family_tasks_request(mw.MIGRATIONS_FAMILY, "STOPPED"),
        response,
    )
    assert reason in _blocks(world, tmp_path)


def test_the_plan_names_the_stopped_reads_it_needs(tmp_path: Path) -> None:
    world = ready_world(tmp_path, "036")
    _stopped_run(world, stopped_at="2026-10-22T10:19:00+00:00")
    world.drop("ecs", "DescribeTasks", mw.describe_tasks_request([EARLIER_RUN]))
    document = plan(world, tmp_path)
    assert document["status"] == "incomplete"
    assert {
        "service": "ecs",
        "operation": "DescribeTasks",
        "params": mw.describe_tasks_request([EARLIER_RUN]),
    } in document["next_requests"]


# ---------------------------------------------------------------------------
# F2 the read family's own command is never a migration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change",
    [
        {"command": ["alembic", "upgrade", "head"]},
        {"command": ["sh", "-c", "ALEMBIC upgrade heads"]},
        {"entryPoint": ["alembic"]},
        {"environment": [{"name": "RUN", "value": "alembic upgrade head"}]},
    ],
)
def test_a_read_family_that_bakes_a_migration_is_refused_everywhere(
    change: dict[str, Any], tmp_path: Path
) -> None:
    """Repro exp2: consulta baking ``alembic upgrade head`` would run it if a
    RunTask lost its override."""

    world = World("036")
    if "entryPoint" in change:
        bad = _read_container(**change)
    else:
        bad = _read_container(**change)
    world.put(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(READ_TD),
        _one_container_definition(READ_TD, "neurostack-staging-consulta", bad),
    )
    for phase in ("pre", "post"):
        document, context = mw.evaluate_read_plan(
            world.readback(tmp_path), world.context(), phase, now=NOW_DT
        )
        assert context is None
        assert "read_family_names_the_migration_tool" in document["blocks"]
    good = World("036")
    good.record_read(tmp_path, "pre")
    good.put(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(READ_TD),
        _one_container_definition(READ_TD, "neurostack-staging-consulta", bad),
    )
    result = verify_read(good, tmp_path, "pre")
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert any("read_family_names_the_migration_tool" in r for r in result["reasons"])
    assert "pre_read_not_ok" in _blocks(good, tmp_path)


def test_a_037_read_revision_with_the_migration_tool_is_unusable(
    tmp_path: Path,
) -> None:
    world = World("037")
    world.record_read(tmp_path, "pre")
    world.put(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(MIGRATIONS_READ_TD),
        _read_revision_definition(environment=[{"name": "X", "value": "alembic"}]),
    )
    result = verify_read(world, tmp_path, "pre")
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"


# ---------------------------------------------------------------------------
# L2 "nothing changed" needs a failed exit; L4 late upgrade lines
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("marker", ["55P03", "40P01", "55000"])
def test_exit_zero_with_all_lines_and_a_sqlstate_marker_is_inconsistent(
    marker: str, tmp_path: Path
) -> None:
    """Repro E3."""

    lines = ALEMBIC["success_036"]["lines"] + [f"WARNING something mentions {marker}"]
    world = completed(tmp_path, "036", lines)
    result = verify(world, tmp_path)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert result["classification"] == "migration_log_inconsistent"
    assert "nothing_changed" not in result["alert_kinds"]
    assert "still reads" not in result["next"]


def test_a_sqlstate_marker_without_an_exit_code_is_not_nothing_changed(
    tmp_path: Path,
) -> None:
    world = completed(
        tmp_path, "036", ALEMBIC["lock_timeout_036"]["lines"], exit_code=None
    )
    result = verify(world, tmp_path)
    assert "migration_stopped_without_exit_code" in result["reasons"]
    assert "lock_timeout_55P03_nothing_changed" not in result["reasons"]
    assert "nothing_changed" not in result["alert_kinds"]


@pytest.mark.parametrize("delivered", [0, 1, 2])
def test_upgrade_lines_still_on_their_way_are_in_progress(
    delivered: int, tmp_path: Path
) -> None:
    """Repro E6: exit 0, the last upgrade line not yet delivered."""

    lines = ALEMBIC["success_035"]["lines"]
    world = completed(tmp_path, "035", lines[: 2 + delivered])
    world.captured_at = "2026-10-22T10:34:30Z"  # 90 s after stoppedAt
    result = verify(world, tmp_path)
    assert result["outcome"] == "in_progress", result["reasons"]
    assert result["reasons"] == ["log_events_not_delivered"]
    assert result["next_requests"] == [
        {
            "service": "logs",
            "operation": "GetLogEvents",
            "params": hook.log_events_request(MIGRATIONS_LOG, RUN_TASK_ID),
        }
    ]
    world.captured_at = "2026-10-22T10:35:01Z"  # past the grace
    result = verify(world, tmp_path)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert "upgrade_lines_mismatch" in result["reasons"]


@pytest.mark.parametrize(
    "lines,exit_code",
    [
        # not a prefix: a later line without the first one
        (["INFO  [alembic.runtime.migration] Running upgrade 033 -> 034, x"], 0),
        # a failed exit is never "still on its way"
        (ALEMBIC["success_035"]["lines"][:3], 1),
    ],
)
def test_only_a_clean_prefix_waits_for_late_lines(
    lines: list[str], exit_code: int, tmp_path: Path
) -> None:
    world = completed(tmp_path, "035", lines, exit_code=exit_code)
    world.captured_at = "2026-10-22T10:34:00Z"
    assert verify(world, tmp_path)["outcome"] == "MIGRATION_WINDOW_STOPPED"


# ---------------------------------------------------------------------------
# L3 any override on a described task is an alert
# ---------------------------------------------------------------------------

TASK_LEVEL_OVERRIDES: list[Any] = [
    {
        "containerOverrides": [
            {
                "name": "migrations",
                "environment": [{"name": "DATABASE_URL", "value": "x"}],
            }
        ]
    },
    {
        "containerOverrides": [{"name": "migrations"}],
        "taskRoleArn": "arn:aws:iam::1:role/x",
    },
    {
        "containerOverrides": [{"name": "migrations"}],
        "executionRoleArn": "arn:aws:iam::1:role/x",
    },
    {"containerOverrides": [{"name": "migrations", "cpu": 4096}]},
    {"containerOverrides": [{"name": "migrations"}], "memory": "30720"},
    {
        "containerOverrides": [{"name": "migrations"}],
        "inferenceAcceleratorOverrides": [{"deviceName": "x"}],
    },
    {
        "containerOverrides": [{"name": "migrations"}],
        "ephemeralStorage": {"sizeInGiB": 40},
    },
    {
        "containerOverrides": [
            {"name": "migrations", "command": ["alembic", "upgrade", "036"]}
        ]
    },
    {"containerOverrides": [{"name": "migrations", "environmentFiles": []}]},
    {"containerOverrides": "x"},
    "x",
]


@pytest.mark.parametrize("overrides", TASK_LEVEL_OVERRIDES)
@pytest.mark.parametrize("recovered", [False, True])
def test_any_override_on_the_migration_task_is_an_alert(
    overrides: Any, recovered: bool, tmp_path: Path
) -> None:
    """Repro E4 (crash recovery) and E5 (recorded run)."""

    world = completed(tmp_path, "036")
    key = mw.describe_tasks_request([RUN_TASK])
    task = world.response("ecs", "DescribeTasks", key)["tasks"][0]
    task["overrides"] = overrides
    world.put("ecs", "DescribeTasks", key, {"tasks": [task], "failures": []})
    if recovered:
        del world.records["migration"]
    result = verify(world, tmp_path, task_arn=RUN_TASK if recovered else None)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert "task_overrides_present" in result["reasons"]
    assert "evidence_mismatch" in result["alert_kinds"]


def test_the_aws_shape_of_no_override_is_accepted(tmp_path: Path) -> None:
    world = completed(tmp_path, "036")
    key = mw.describe_tasks_request([RUN_TASK])
    task = world.response("ecs", "DescribeTasks", key)["tasks"][0]
    shapes: list[dict[str, Any] | None] = [
        {
            "containerOverrides": [{"name": "migrations"}],
            "inferenceAcceleratorOverrides": [],
        },
        {"containerOverrides": []},
        None,
    ]
    for shape in shapes:
        if shape is None:
            task.pop("overrides", None)
        else:
            task["overrides"] = shape
        world.put("ecs", "DescribeTasks", key, {"tasks": [task], "failures": []})
        assert verify(world, tmp_path)["outcome"] == "applied"


@pytest.mark.parametrize(
    "extra",
    [
        {"environment": [{"name": "DATABASE_URL", "value": "x"}]},
        {"cpu": 1024},
    ],
)
def test_any_extra_override_on_a_read_task_is_an_alert(
    extra: dict[str, Any], tmp_path: Path
) -> None:
    world = World("036")
    world.record_read(tmp_path, "pre")
    key = mw.describe_tasks_request([PRE_TASK])
    task = world.response("ecs", "DescribeTasks", key)["tasks"][0]
    task["overrides"]["containerOverrides"][0].update(extra)
    world.put("ecs", "DescribeTasks", key, {"tasks": [task], "failures": []})
    result = verify_read(world, tmp_path, "pre")
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert "read_task_overrides_present" in result["reasons"]
    world = World("036")
    world.record_read(tmp_path, "pre")
    task = world.response("ecs", "DescribeTasks", key)["tasks"][0]
    task["overrides"]["taskRoleArn"] = "arn:aws:iam::1:role/x"
    world.put("ecs", "DescribeTasks", key, {"tasks": [task], "failures": []})
    assert (
        "read_task_overrides_present" in verify_read(world, tmp_path, "pre")["reasons"]
    )


# ---------------------------------------------------------------------------
# L7: the reviewer's surviving mutants
# ---------------------------------------------------------------------------


def test_a_stop_reason_outranks_an_in_progress_wait(tmp_path: Path) -> None:
    """M04: a final reason (no exit code) with a page still missing is a stop."""

    world = completed(tmp_path, "036", exit_code=None)
    world.drop(
        "logs",
        "GetLogEvents",
        hook.log_events_request(MIGRATIONS_LOG, RUN_TASK_ID, "f/1"),
    )
    result = verify(world, tmp_path)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert result["next_requests"] == []
    verdict = mw._Verdict()
    verdict.wait("x", [])
    verdict.fail("y")
    assert verdict.outcome(mw.OUTCOME_APPLIED) == mw.OUTCOME_STOPPED


@pytest.mark.parametrize(
    "phase,window", [("post", "036"), ("pre", "036"), ("post", "037")]
)
def test_two_alembic_version_rows_are_never_the_expected_revision(
    phase: str, window: str, tmp_path: Path
) -> None:
    """M19 / E10."""

    world = World(window)
    key = GOOD_POST_READ[window] if phase == "post" else GOOD_PRE_READ[window]
    lines: list[str] = []
    for line in READS[key]["lines"]:
        document = json.loads(line)
        if document["kind"] == "version":
            document["values"] = sorted({*document["values"], "037", "036"})
        lines.append(json.dumps(document, separators=(",", ":")))
    world.record_read(tmp_path, phase, lines)
    result = verify_read(world, tmp_path, phase)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert "read_alembic_version_mismatch" in result["reasons"]


@pytest.mark.parametrize("value", [False, 0.0, "0", None])
def test_a_count_that_is_not_the_integer_zero_stops(value: Any, tmp_path: Path) -> None:
    """M20: False == 0 in Python; only the integer 0 counts as zero."""

    lines = READS["pre_036_at_035"]["lines"]
    world = World("036")
    world.record_read(
        tmp_path, "pre", _rows_changed(lines, 0, ["billing_migrations", value])
    )
    result = verify_read(world, tmp_path, "pre")
    assert "read_count_not_zero:billing_migrations" in result["reasons"]


def test_trailing_data_after_the_gzip_payload_is_refused() -> None:
    """M33."""

    import base64
    import gzip

    program = mw.read_program(mw.WINDOW_036, mw.WINDOW_036.pre_read)
    header = mw._READ_HEADER.match(program)
    assert header is not None
    raw = mw.read_payload(mw.WINDOW_036.pre_read)
    for tail in (b"junk", gzip.compress(b"more", mtime=0)):
        padded = base64.b64encode(gzip.compress(raw, mtime=0) + tail).decode()
        assert mw._decompressed(padded) is None
        tampered = program.replace(header.group("payload"), padded)
        assert mw.normalized_read_command(["python", "-c", tampered]) is None


@pytest.mark.parametrize("phase", ["pre", "post"])
def test_a_read_without_its_version_line_stops(phase: str, tmp_path: Path) -> None:
    """M42: drop the version line and renumber, so the sequence stays whole."""

    key = GOOD_PRE_READ["036"] if phase == "pre" else GOOD_POST_READ["036"]
    lines: list[str] = []
    for line in READS[key]["lines"]:
        document = json.loads(line)
        if document["kind"] == "version":
            continue
        document["seq"] = len(lines)
        lines.append(json.dumps(document, separators=(",", ":")))
    world = World("036")
    world.record_read(tmp_path, phase, lines)
    result = verify_read(world, tmp_path, phase)
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert result["alembic_version"] is None
    assert "read_sequence_broken" not in result["reasons"]
    assert "read_alembic_version_mismatch" in result["reasons"]


def test_a_pre_read_still_running_is_not_ok_for_the_plan(tmp_path: Path) -> None:
    """M29: in progress is not read_ok."""

    world = World("036")
    world.record_read(tmp_path, "pre")
    key = mw.describe_tasks_request([PRE_TASK])
    task = world.response("ecs", "DescribeTasks", key)["tasks"][0]
    task["lastStatus"] = "RUNNING"
    world.put("ecs", "DescribeTasks", key, {"tasks": [task], "failures": []})
    document = plan(world, tmp_path)
    assert document["pre_read"]["outcome"] == "in_progress"
    assert "pre_read_not_ok" in document["blocks"]


def test_a_pending_re_pin_is_never_a_re_pinned_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M30: revision 9 that still names a re-pin is not re-pinned."""

    world = ready_world(tmp_path, "036")
    real = wiring.manifest_pins

    def pending(manifest: dict[str, Any]) -> Any:
        return dataclasses.replace(
            real(manifest), rollback_repin_before="migration_036"
        )

    monkeypatch.setattr(wiring, "manifest_pins", pending)
    assert "rollback_target_not_repinned" in _blocks(world, tmp_path)


@pytest.mark.parametrize(
    "command",
    ["plan", "render-pre-read", "verify-result", "render-verify-reads"],
)
def test_a_repeated_option_is_refused(
    command: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = ready_world(tmp_path, "036")
    extra = ["--target", "migration"] if command == "render-verify-reads" else []
    for repeated in (
        ["--window", "036"],
        ["--snapshot-id", SNAPSHOT_ID["036"]],
        ["--migrations-task-definition", MIGRATIONS_TD["036"]],
    ):
        code, out, err = run_cli(
            [command, *world.args(tmp_path), *extra, *repeated], capsys
        )
        assert code == 2 and out == "" and "blocked: usage" in err


# ---------------------------------------------------------------------------
# Verification loop: a clean earlier run, late-line grace, 037 read override
# ---------------------------------------------------------------------------


def test_an_earlier_clean_run_of_this_window_blocks_a_re_run(tmp_path: Path) -> None:
    """Repro exp5: an earlier run of 036 exited 0 and stopped before a fresh
    pre-read: a re-run (after a restore, say) is not a recovery."""

    world = ready_world(tmp_path, "036")
    _stopped_run(world, stopped_at="2026-10-22T10:15:00+00:00", exit_code=0)
    document = plan(world, tmp_path)
    assert document["status"] == "blocked"
    assert document["blocks"] == ["window_already_ran_cleanly"]
    assert document["earlier_window_tasks"] == [
        {
            "task_arn": EARLIER_RUN,
            "exit_codes": [0],
            "stopped_at": "2026-10-22T10:15:00+00:00",
        }
    ]
    with pytest.raises(mw.WindowError, match="window_already_ran_cleanly"):
        render_run(world, tmp_path, attempt=2)


@pytest.mark.parametrize("exit_code", [1, 2, None, "0", False])
def test_an_earlier_failed_run_before_the_pre_read_is_reported_not_blocking(
    exit_code: Any, tmp_path: Path
) -> None:
    world = ready_world(tmp_path, "036")
    _stopped_run(world, stopped_at="2026-10-22T10:15:00+00:00", exit_code=exit_code)
    document = plan(world, tmp_path)
    assert document["status"] == "ready", document["blocks"]
    assert "no_clean_window_run_listed" in document["checks"]
    (row,) = document["earlier_window_tasks"]
    assert row["exit_codes"] == [exit_code if type(exit_code) is int else None]
    assert row["stopped_at"] == "2026-10-22T10:15:00+00:00"


def test_a_clean_exit_of_any_container_of_an_earlier_run_blocks(
    tmp_path: Path,
) -> None:
    """Fail closed: the family has one container; a run whose container rows
    look different still counts as clean when any of them exited 0."""

    world = ready_world(tmp_path, "036")
    _stopped_run(world, stopped_at="2026-10-22T10:15:00+00:00")
    key = mw.describe_tasks_request([EARLIER_RUN])
    task = world.response("ecs", "DescribeTasks", key)["tasks"][0]
    task["containers"] = [
        {"name": "sidecar", "exitCode": 1},
        {"name": "migrations", "exitCode": 0},
    ]
    world.put("ecs", "DescribeTasks", key, {"tasks": [task], "failures": []})
    document = plan(world, tmp_path)
    assert document["blocks"] == ["window_already_ran_cleanly"]
    assert document["earlier_window_tasks"][0]["exit_codes"] == [1, 0]


@pytest.mark.parametrize(
    "window,started_by",
    [
        ("036", "neurostack-migration-window-035"),
        ("037", "neurostack-migration-window-036"),
        ("037", "neurostack-migration-read-037-pre"),
        ("037", "neurostack-migration-read-037-post"),
    ],
)
def test_a_clean_task_of_another_window_or_a_read_never_blocks(
    window: str, started_by: str, tmp_path: Path
) -> None:
    world = ready_world(tmp_path, window)
    _stopped_run(
        world,
        stopped_at="2026-10-22T10:25:00+00:00",
        started_by=started_by,
        exit_code=0,
    )
    document = plan(world, tmp_path)
    assert document["status"] == "ready", document["blocks"]
    assert document["earlier_window_tasks"] == []


def test_a_complete_clean_run_inside_the_grace_is_applied_at_once(
    tmp_path: Path,
) -> None:
    """N17: the late-line wait is only for a strict beginning of the lines."""

    for window in ("035", "036", "037"):
        world = completed(tmp_path, window)
        world.captured_at = "2026-10-22T10:33:30Z"  # 30 s after stoppedAt
        result = verify(world, tmp_path)
        assert result["outcome"] == "applied", (window, result["reasons"])
        assert result["next_requests"] == []


def test_the_exact_read_program_as_an_override_on_the_037_read_revision_is_refused(
    tmp_path: Path,
) -> None:
    """N27: the recorded request is clean, the described task carries the
    exact read program as an override: the override check itself refuses it
    (the read revision bakes the command; no override is ever rendered)."""

    world = World("037")
    world.record_read(tmp_path, "pre")
    key = mw.describe_tasks_request([PRE_TASK])
    task = world.response("ecs", "DescribeTasks", key)["tasks"][0]
    program = mw.read_program(mw.WINDOW_037, mw.WINDOW_037.pre_read)
    task["overrides"] = {
        "containerOverrides": [
            {"name": "migrations", "command": ["python", "-c", program]}
        ]
    }
    world.put("ecs", "DescribeTasks", key, {"tasks": [task], "failures": []})
    result = verify_read(world, tmp_path, "pre")
    assert result["outcome"] == "MIGRATION_WINDOW_STOPPED"
    assert result["reasons"] == ["read_task_overrides_present"]
    assert mw.overrides_problem(task, "migrations", None) is True
    # The same override is the rendered one on the read family.
    assert mw.overrides_problem(task, "migrations", ["python", "-c", program]) is False
