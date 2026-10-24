"""Post-deploy ASAAS webhook registration hook: pure planner, renderer, verifier.

Every test runs on recorded readback files built here; nothing reaches AWS,
GitHub or ASAAS.
"""

from __future__ import annotations

import ast
import copy
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
from scripts import verify_asaas_producer_image_rollout as rollout

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "asaas_deploy_hook.py"
T1_CLI = ROOT / "src" / "ops" / "asaas_webhook.py"

ACCOUNT = "388460142431"
REGISTRY = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com"
EXPECTED_ARN = (
    f"arn:aws:ecs:us-east-1:{ACCOUNT}:task-definition/neurostack-staging-app:9"
)
OLD_ARN = EXPECTED_ARN.replace(":9", ":8")
BRIDGE_DIGEST = "sha256:" + "2" * 64
BRIDGE_IMAGE = f"{REGISTRY}/neurostack-staging@{BRIDGE_DIGEST}"
SUBNETS = ["subnet-0a1b2c3d4e5f60718", "subnet-0f1e2d3c4b5a69788"]
SECURITY_GROUPS = ["sg-0123456789abcdef0"]
DEPLOYMENT_ID = "ecs-svc/1234567890123456789"
TASK_ID = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
TASK_ARN = f"arn:aws:ecs:us-east-1:{ACCOUNT}:task/neurostack-staging/{TASK_ID}"
SECRET_PREFIX = f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:neurostack/staging/"
LOG_GROUP = "/ecs/neurostack-staging/app"
# The current PRIMARY deployment started before the registration task.
DEPLOYED_AT = "2026-10-20T00:50:00.000000+00:00"
CREATED_AT = "2026-10-20T01:00:00.000000+00:00"
STOPPED_AT = "2026-10-20T01:03:00.000000+00:00"
CAPTURED_AT = "2026-10-20T01:10:00Z"
# plan freshness is judged against an explicit clock: one minute later.
NOW = "2026-10-20T01:11:00Z"
NOW_DT = datetime(2026, 10, 20, 1, 11, tzinfo=timezone.utc)
NOW_ARGS = ["--now", NOW]
WEBHOOK_DIGEST = "a" * 64
COMMAND = ["python", "-m", "src.ops.asaas_webhook", "reconcile"]
FORCE_COMMAND = [*COMMAND, "--force-rewrite-token"]

# Built by concatenation so repository secret scanners never see a literal.
LEAKED_KEY = "$" + "aact_prod_" + "Zq" * 12
LEAKED_URL = "https" + "://staging.api.neurostack.global/v1/billing/webhook/asaas"
LEAKED_EMAIL = "neurostack.infra" + "@" + "gmail.com"
LEAKED_ID_PATH = "/v3/" + "webhooks/wbh_0123456789"


# ---------------------------------------------------------------------------
# Readback fixtures
# ---------------------------------------------------------------------------


def _service(**changes: Any) -> dict[str, Any]:
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
        "taskDefinition": EXPECTED_ARN,
        "deployments": [
            {
                "id": DEPLOYMENT_ID,
                "status": "PRIMARY",
                "taskDefinition": EXPECTED_ARN,
                "desiredCount": 2,
                "runningCount": 2,
                "pendingCount": 0,
                "rolloutState": "COMPLETED",
                "createdAt": DEPLOYED_AT,
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


def _app_container(**changes: Any) -> dict[str, Any]:
    container: dict[str, Any] = {
        "name": "app",
        "image": BRIDGE_IMAGE,
        "essential": True,
        "environment": [
            {"name": "ENVIRONMENT", "value": "staging"},
            {"name": "LOG_FORMAT", "value": "json"},
            {"name": "ASAAS_WEBHOOK_URL", "value": "<reviewed parameter>"},
            {"name": "ASAAS_CHECKOUT_RETURN_URL", "value": "<reviewed parameter>"},
            {"name": "ASAAS_WEBHOOK_NOTIFICATION_EMAIL", "value": "<reviewed>"},
            {"name": "BILLING_FACT_WORKER_ENABLED", "value": "false"},
        ],
        "secrets": [
            {
                "name": "DATABASE_URL",
                "valueFrom": SECRET_PREFIX + "database-url-fyOYuj",
            },
            {
                "name": "ASAAS_API_KEY",
                "valueFrom": SECRET_PREFIX + "asaas-api-key-AbC123",
            },
            {
                "name": "ASAAS_WEBHOOK_TOKEN",
                "valueFrom": SECRET_PREFIX + "asaas-webhook-token-DeF456",
            },
            {
                "name": "BILLING_OBSERVABILITY_SMOKE_TOKEN",
                "valueFrom": SECRET_PREFIX + "billing-observability-smoke-token-mSR8Ux",
            },
        ],
        "stopTimeout": 30,
        "logConfiguration": {
            "logDriver": "awslogs",
            "options": {
                "awslogs-group": LOG_GROUP,
                "awslogs-region": "us-east-1",
                "awslogs-stream-prefix": "ecs",
            },
        },
    }
    container.update(changes)
    return container


def _task_definition(
    app: dict[str, Any] | None = None, **changes: Any
) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "taskDefinitionArn": EXPECTED_ARN,
        "family": "neurostack-staging-app",
        "revision": 9,
        "status": "ACTIVE",
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "containerDefinitions": [
            app if app is not None else _app_container(),
            {
                "name": "otel",
                "image": "public.ecr.aws/aws-observability/aws-otel-collector@sha256:"
                + "7" * 64,
                "essential": False,
                "environment": [],
            },
        ],
    }
    definition.update(changes)
    return {"taskDefinition": definition}


def _image_details(digest: str = BRIDGE_DIGEST) -> dict[str, Any]:
    return {
        "imageDetails": [
            {
                "registryId": ACCOUNT,
                "repositoryName": "neurostack-staging",
                "imageDigest": digest,
            }
        ]
    }


def _run_task_response(**changes: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "taskArn": TASK_ARN,
        "clusterArn": rollout.CLUSTER_ARN,
        "taskDefinitionArn": EXPECTED_ARN,
        "startedBy": hook.STARTED_BY,
        "lastStatus": "PROVISIONING",
        "desiredStatus": "RUNNING",
        "createdAt": CREATED_AT,
        "overrides": {
            "containerOverrides": [{"name": "app", "command": list(COMMAND)}]
        },
    }
    task.update(changes)
    return {"tasks": [task], "failures": []}


def _described_task(
    *, command: list[str] | None = None, exit_code: Any = 0, **changes: Any
) -> dict[str, Any]:
    app: dict[str, Any] = {"name": "app", "lastStatus": "STOPPED"}
    if exit_code is not None:
        app["exitCode"] = exit_code
    task: dict[str, Any] = {
        "taskArn": TASK_ARN,
        "clusterArn": rollout.CLUSTER_ARN,
        "taskDefinitionArn": EXPECTED_ARN,
        "startedBy": hook.STARTED_BY,
        "lastStatus": "STOPPED",
        "desiredStatus": "STOPPED",
        "createdAt": CREATED_AT,
        "stoppedAt": STOPPED_AT,
        "stopCode": "EssentialContainerExited",
        "stoppedReason": "Essential container in task exited " + LEAKED_URL,
        "containers": [
            app,
            {"name": "otel", "lastStatus": "STOPPED", "exitCode": 143},
        ],
        "overrides": {
            "containerOverrides": [
                {"name": "app", "command": list(command or COMMAND)}
            ],
            "inferenceAcceleratorOverrides": [],
        },
    }
    task.update(changes)
    return {"tasks": [task], "failures": []}


def result_doc(
    verdict: str = "ready",
    *,
    action: str = "none",
    reasons: tuple[str, ...] = (),
    digest: str | None = WEBHOOK_DIGEST,
    force: bool = False,
    exit_code: int | None = None,
    command: str | None = "reconcile",
    writes: int = 0,
    checks: bool = True,
    allow_enable: bool = False,
    no_resume: bool = False,
) -> dict[str, Any]:
    return {
        "schema": "neurostack.asaas_webhook_registration.v1",
        "command": command,
        "verdict": verdict,
        "action": action,
        "reasons": list(reasons),
        "webhook_id_sha256": digest,
        "counts": {
            "webhooks_listed": 3,
            "candidates": 1,
            "name_matches": 1,
            "url_matches": 1,
            "requests": 4,
            "retries": 0,
            "writes": writes,
        },
        "checks": dict.fromkeys(hook.CLI_CHECK_NAMES, checks),
        "force_rewrite_token": force,
        "allow_enable": allow_enable,
        "no_resume": no_resume,
        "exit_code": hook.CLI_EXIT_CODES[verdict] if exit_code is None else exit_code,
    }


def result_line(doc: dict[str, Any]) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def twin_line(doc: dict[str, Any], **changes: Any) -> str:
    twin = {
        **doc,
        "event": "asaas_webhook_registration",
        "level": "info" if doc["exit_code"] == 0 else "warning",
        "logger": "src.ops.asaas_webhook",
        "timestamp": "2026-10-20T01:02:30.000000+00:00",
        "duration_ms": 812,
        "error_class": None,
    }
    twin.update(changes)
    return json.dumps(twin, sort_keys=True, separators=(",", ":"))


def _events(*messages: str, token: str = "f/1") -> dict[str, Any]:
    return {
        "events": [
            {"timestamp": 1792371750000 + i, "message": m, "ingestionTime": 1}
            for i, m in enumerate(messages)
        ],
        "nextForwardToken": token,
        "nextBackwardToken": "b/1",
    }


def log_request(token: str | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {
        "logGroupName": LOG_GROUP,
        "logStreamName": f"ecs/app/{TASK_ID}",
        "startFromHead": True,
        "limit": hook.LOG_EVENTS_LIMIT,
    }
    if token is not None:
        request["nextToken"] = token
    return request


class World:
    """A recorded readback file, one response per exact request."""

    def __init__(self) -> None:
        self.reads: dict[str, dict[str, Any]] = {}
        self.run_task: dict[str, Any] | None = None
        self.captured_at = CAPTURED_AT
        self.put(
            "ecs",
            "DescribeServices",
            {"cluster": "neurostack-staging", "services": ["neurostack-staging-app"]},
            _service(),
        )
        self.put(
            "ecs",
            "ListTasks",
            {"cluster": "neurostack-staging", "startedBy": hook.STARTED_BY},
            {"taskArns": []},
        )
        self.put(
            "ecs",
            "DescribeTaskDefinition",
            {"taskDefinition": EXPECTED_ARN},
            _task_definition(),
        )
        self.put(
            "ecr",
            "DescribeImages",
            {
                "repositoryName": "neurostack-staging",
                "imageIds": [{"imageDigest": BRIDGE_DIGEST}],
            },
            _image_details(),
        )

    def put(
        self, service: str, operation: str, params: dict[str, Any], response: Any
    ) -> None:
        self.reads[hook._read_key(service, operation, params)] = {
            "service": service,
            "operation": operation,
            "params": params,
            "response": response,
        }

    def drop(self, service: str, operation: str, params: dict[str, Any]) -> None:
        del self.reads[hook._read_key(service, operation, params)]

    def document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schema_version": hook.READBACK_SCHEMA,
            "account_id": ACCOUNT,
            "region": "us-east-1",
            "captured_at_utc": self.captured_at,
            "reads": list(copy.deepcopy(list(self.reads.values()))),
        }
        if self.run_task is not None:
            document["run_task"] = copy.deepcopy(self.run_task)
        return document

    def write(self, path: Path) -> Path:
        path.write_text(json.dumps(self.document()), encoding="utf-8")
        return path

    def record_run(
        self,
        tmp_path: Path,
        *,
        force: bool = False,
        attempt: int = 1,
        params: dict[str, Any] | None = None,
        response: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        rendered = params or render(self, tmp_path, force=force, attempt=attempt)
        command = FORCE_COMMAND if force else COMMAND
        self.run_task = {
            "params": rendered,
            "response": response
            or _run_task_response(
                overrides={
                    "containerOverrides": [{"name": "app", "command": list(command)}]
                }
            ),
        }
        return rendered

    def finish(
        self,
        *messages: str,
        force: bool = False,
        exit_code: Any = 0,
        task: dict[str, Any] | None = None,
        logs: dict[str, Any] | None = None,
    ) -> None:
        self.put(
            "ecs",
            "DescribeTasks",
            {"cluster": "neurostack-staging", "tasks": [TASK_ARN]},
            task
            or _described_task(
                command=FORCE_COMMAND if force else COMMAND, exit_code=exit_code
            ),
        )
        if logs is not None:
            self.put("logs", "GetLogEvents", log_request(), logs)
            return
        self.put_pages([list(messages)])

    def put_pages(self, pages: list[list[str]], *, terminal: bool = True) -> None:
        """A forward chain: page i returns token f/i+1; the end repeats its token."""

        token: str | None = None
        for index, page in enumerate(pages, start=1):
            self.put(
                "logs",
                "GetLogEvents",
                log_request(token),
                _events(*page, token=f"f/{index}"),
            )
            token = f"f/{index}"
        if terminal:
            self.put(
                "logs", "GetLogEvents", log_request(token), _events(token=str(token))
            )


def run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = hook.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _args(world: World, tmp_path: Path, name: str = "readback.json") -> list[str]:
    path = world.write(tmp_path / name)
    return ["--expected-task-definition", EXPECTED_ARN, "--readback-file", str(path)]


def render(
    world: World, tmp_path: Path, *, force: bool = False, attempt: int = 1
) -> dict[str, Any]:
    readback = hook.load_readback(world.write(tmp_path / "render.json"))
    context = hook.plan_context(readback, EXPECTED_ARN, now=NOW_DT)
    return hook.build_run_task_request(
        context, force_rewrite_token=force, attempt=attempt
    )


def plan(world: World, tmp_path: Path, *, now: datetime = NOW_DT) -> dict[str, Any]:
    readback = hook.load_readback(world.write(tmp_path / "plan.json"))
    document, _context = hook.evaluate_plan(readback, EXPECTED_ARN, now=now)
    return document


def verify(
    world: World, tmp_path: Path, *, task_arn: str | None = None
) -> dict[str, Any]:
    readback = hook.load_readback(world.write(tmp_path / "verify.json"))
    return hook.verify_result(readback, EXPECTED_ARN, task_arn=task_arn)


def completed(
    tmp_path: Path, *messages: str, force: bool = False, exit_code: Any = 0
) -> World:
    world = World()
    world.record_run(tmp_path, force=force)
    world.finish(*messages, force=force, exit_code=exit_code)
    return world


def ok_lines(doc: dict[str, Any] | None = None) -> tuple[str, str]:
    doc = doc or result_doc()
    return result_line(doc), twin_line(doc)


# ---------------------------------------------------------------------------
# Static contract
# ---------------------------------------------------------------------------


def test_emitted_operations_are_exactly_run_describe_list_and_log_reads() -> None:
    assert hook.EMITTED_OPERATIONS == frozenset(
        {
            ("ecs", "RunTask"),
            ("ecs", "DescribeTasks"),
            ("ecs", "ListTasks"),
            ("logs", "GetLogEvents"),
        }
    )
    emitted = {operation for _service, operation in hook.EMITTED_OPERATIONS}
    assert not emitted & hook.FORBIDDEN_OPERATIONS
    assert {"UpdateService", "RegisterTaskDefinition", "StopTask"} <= (
        hook.FORBIDDEN_OPERATIONS
    )


def test_consumed_reads_never_include_a_mutation() -> None:
    for _service, operation in hook.CONSUMED_READS:
        assert operation.startswith(("Describe", "List", "Get"))
        assert operation not in hook.FORBIDDEN_OPERATIONS


def test_target_comes_from_the_rollout_validator_pins() -> None:
    assert hook.CLUSTER == "neurostack-staging"
    assert hook.SERVICE == rollout.SERVICE_NAME
    assert hook.TASK_FAMILY == rollout.TASK_FAMILY
    assert hook.APP_CONTAINER == rollout.APP_CONTAINER
    assert hook.ACCOUNT_ID == ACCOUNT
    assert hook.REGION == "us-east-1"


def test_watch_policy_outlasts_the_cli_deadline_with_start_and_stop_budget() -> None:
    assert hook.CLI_DEADLINE_SECONDS == 120
    assert hook.WATCH_DEADLINE_SECONDS > (
        hook.CLI_DEADLINE_SECONDS + hook.START_BUDGET_SECONDS + hook.STOP_BUDGET_SECONDS
    )
    assert hook.WATCH_DEADLINE_SECONDS == 600
    assert 0 < hook.POLL_INTERVAL_SECONDS < hook.WATCH_DEADLINE_SECONDS
    assert hook.LOG_GRACE_SECONDS >= 60


def test_registration_command_is_the_t1_cli_reconcile() -> None:
    assert hook.registration_command(force_rewrite_token=False) == COMMAND
    assert hook.registration_command(force_rewrite_token=True) == FORCE_COMMAND


def test_hook_source_never_reaches_a_network_or_aws_sdk() -> None:
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
    }
    assert "os.system" not in source
    assert not re.search(r"upgrade\s+heads?\b", source, re.IGNORECASE)


def _literal(tree: ast.Module, name: str) -> Any:
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            assert value is not None
            if isinstance(
                value, ast.Call
            ):  # MappingProxyType({...}) / frozenset({...})
                value = value.args[0]
            return ast.literal_eval(value)
    raise AssertionError(f"{name} not found")


def _document_keys(tree: ast.Module) -> set[str]:
    """The keys of the dict ``RunResult.document`` returns."""

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "RunResult":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "document":
                    for statement in ast.walk(item):
                        if isinstance(statement, ast.Return) and isinstance(
                            statement.value, ast.Dict
                        ):
                            return {
                                key.value
                                for key in statement.value.keys
                                if isinstance(key, ast.Constant)
                                and isinstance(key.value, str)
                            }
    raise AssertionError("RunResult.document not found")


@pytest.mark.skipif(
    not T1_CLI.exists(), reason="T1 CLI src/ops/asaas_webhook.py not on this branch"
)
def test_result_contract_matches_the_t1_cli_source() -> None:
    tree = ast.parse(T1_CLI.read_text(encoding="utf-8"))
    assert _literal(tree, "SCHEMA") == hook.CLI_RESULT_SCHEMA
    assert _literal(tree, "LOG_EVENT") == hook.CLI_LOG_EVENT
    assert _literal(tree, "LOGGER_NAME") == hook.CLI_LOGGER
    assert _literal(tree, "EXIT_CODES") == dict(hook.CLI_EXIT_CODES)
    assert tuple(_literal(tree, "COMMANDS")) == hook.CLI_COMMANDS
    assert set(_literal(tree, "ACTIONS")) == set(hook.CLI_ACTIONS)
    assert tuple(_literal(tree, "CHECK_NAMES")) == hook.CLI_CHECK_NAMES
    assert tuple(_literal(tree, "COUNT_NAMES")) == hook.CLI_COUNT_NAMES
    assert set(_literal(tree, "REASONS")) == set(hook.CLI_REASONS)
    assert _literal(tree, "DEFAULT_DEADLINE_SECONDS") == hook.CLI_DEADLINE_SECONDS
    assert _document_keys(tree) == set(hook._CLI_DOCUMENT_KEYS)
    source = T1_CLI.read_text(encoding="utf-8")
    assert '"--force-rewrite-token"' in source
    # The hook never passes these; a run with them is refused by verify-result.
    for flag in hook.CLI_FLAGS_NEVER_PASSED:
        assert f'"{flag}"' in source


def test_t1_contract_helper_reads_the_document_keys() -> None:
    tree = ast.parse(
        "class RunResult:\n"
        "    def document(self):\n"
        "        return {'schema': 1, 'verdict': 2}\n"
    )
    assert _document_keys(tree) == {"schema", "verdict"}


# ---------------------------------------------------------------------------
# plan: preconditions
# ---------------------------------------------------------------------------


def test_plan_is_ready_after_a_completed_deployment(tmp_path: Path) -> None:
    document = plan(World(), tmp_path)
    assert document["status"] == "ready", document["blocks"]
    assert document["blocks"] == []
    assert document["expected_task_definition_arn"] == EXPECTED_ARN
    assert {
        "service_identity",
        "single_primary_deployment",
        "deployment_completed",
        "deployment_on_expected_task_definition",
        "service_steady",
        "no_registration_task_running",
        "network_private_awsvpc",
        "task_definition_active",
        "app_log_configuration",
        "task_definition_has_webhook_wiring",
        "app_image_digest_pinned",
        "app_image_in_ecr",
    } <= set(document["checks"])
    assert document["run_task"]["started_by"] == hook.STARTED_BY
    assert document["run_task"]["command"] == COMMAND
    assert document["watch"]["watch_deadline_seconds"] == 600
    assert document["on_failure"] == hook.FAILURE_POLICY


def _service_with_deployment(**deployment: Any) -> dict[str, Any]:
    response = _service()
    response["services"][0]["deployments"][0].update(deployment)
    return response


SERVICE_STATES: dict[str, tuple[Callable[[], dict[str, Any]], str]] = {
    "rollout_in_progress": (
        lambda: _service_with_deployment(rolloutState="IN_PROGRESS"),
        "deployment_not_completed",
    ),
    "rollout_failed": (
        lambda: _service_with_deployment(rolloutState="FAILED"),
        "deployment_not_completed",
    ),
    "primary_on_wrong_arn": (
        lambda: _service_with_deployment(taskDefinition=OLD_ARN),
        "deployment_task_definition_mismatch",
    ),
    "service_on_wrong_arn": (
        lambda: _service(taskDefinition=OLD_ARN),
        "service_task_definition_mismatch",
    ),
    "running_below_desired": (
        lambda: _service(runningCount=1),
        "service_not_steady",
    ),
    "desired_zero": (
        lambda: _service(desiredCount=0, runningCount=0),
        "service_not_steady",
    ),
    "tasks_pending": (lambda: _service(pendingCount=1), "service_not_steady"),
    "deployment_running_below_desired": (
        lambda: _service_with_deployment(runningCount=1),
        "service_not_steady",
    ),
    "concurrent_deployments": (
        lambda: _service(
            deployments=[
                _service()["services"][0]["deployments"][0],
                {"id": "ecs-svc/2", "status": "ACTIVE", "taskDefinition": OLD_ARN},
            ]
        ),
        "service_has_concurrent_deployments",
    ),
    "not_primary": (
        lambda: _service_with_deployment(status="ACTIVE"),
        "deployment_not_completed",
    ),
    "service_draining": (
        lambda: _service(status="DRAINING"),
        "service_identity_mismatch",
    ),
    "other_service": (
        lambda: _service(serviceArn=rollout.SERVICE_ARN + "-other"),
        "service_identity_mismatch",
    ),
    "subnets_missing": (
        lambda: _service(
            networkConfiguration={
                "awsvpcConfiguration": {
                    "securityGroups": SECURITY_GROUPS,
                    "assignPublicIp": "DISABLED",
                }
            }
        ),
        "network_subnets_missing",
    ),
    "subnets_empty": (
        lambda: _service(
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": [],
                    "securityGroups": SECURITY_GROUPS,
                    "assignPublicIp": "DISABLED",
                }
            }
        ),
        "network_subnets_missing",
    ),
    "subnet_malformed": (
        lambda: _service(
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": ["subnet-xyz"],
                    "securityGroups": SECURITY_GROUPS,
                    "assignPublicIp": "DISABLED",
                }
            }
        ),
        "network_subnets_missing",
    ),
    "security_groups_missing": (
        lambda: _service(
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": SUBNETS,
                    "assignPublicIp": "DISABLED",
                }
            }
        ),
        "network_security_groups_missing",
    ),
    "network_configuration_missing": (
        lambda: _service(networkConfiguration=None),
        "network_subnets_missing",
    ),
    "service_public_ip": (
        lambda: _service(
            networkConfiguration={
                "awsvpcConfiguration": {
                    "subnets": SUBNETS,
                    "securityGroups": SECURITY_GROUPS,
                    "assignPublicIp": "ENABLED",
                }
            }
        ),
        "service_assign_public_ip_not_disabled",
    ),
    "service_on_ec2": (
        lambda: _service(launchType="EC2"),
        "service_launch_type_not_fargate",
    ),
    "service_failures": (
        lambda: {"services": [], "failures": [{"reason": "MISSING"}]},
        "service_readback_unavailable",
    ),
}


@pytest.mark.parametrize("state", sorted(SERVICE_STATES))
def test_plan_refuses_every_unsafe_service_state(
    state: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make, reason = SERVICE_STATES[state]
    world = World()
    world.put(
        "ecs",
        "DescribeServices",
        {"cluster": "neurostack-staging", "services": ["neurostack-staging-app"]},
        make(),
    )
    document = plan(world, tmp_path)
    assert document["status"] == "blocked"
    assert reason in document["blocks"], document["blocks"]
    code, out, err = run(
        ["render-run-task", *_args(world, tmp_path), *NOW_ARGS], capsys
    )
    assert code == 2
    assert out == ""
    assert "blocked: plan_blocked:" in err and reason in err


def test_capacity_provider_fargate_is_accepted(tmp_path: Path) -> None:
    world = World()
    service = _service(capacityProviderStrategy=[{"capacityProvider": "FARGATE"}])
    del service["services"][0]["launchType"]
    world.put(
        "ecs",
        "DescribeServices",
        {"cluster": "neurostack-staging", "services": ["neurostack-staging-app"]},
        service,
    )
    assert plan(world, tmp_path)["status"] == "ready"
    service["services"][0]["capacityProviderStrategy"] = [
        {"capacityProvider": "FARGATE_SPOT"}
    ]
    world.put(
        "ecs",
        "DescribeServices",
        {"cluster": "neurostack-staging", "services": ["neurostack-staging-app"]},
        service,
    )
    assert "service_launch_type_not_fargate" in plan(world, tmp_path)["blocks"]


@pytest.mark.parametrize(
    "listing,reason",
    [
        ({"taskArns": [TASK_ARN]}, "registration_task_running"),
        ({"taskArns": [], "nextToken": "more"}, "registration_task_listing_incomplete"),
        (
            {"__error_code__": "AccessDeniedException"},
            "registration_task_listing_unreadable",
        ),
        ({"taskArns": "x"}, "registration_task_listing_unreadable"),
    ],
)
def test_single_flight_refuses_while_a_registration_task_may_run(
    listing: dict[str, Any], reason: str, tmp_path: Path
) -> None:
    world = World()
    world.put(
        "ecs",
        "ListTasks",
        {"cluster": "neurostack-staging", "startedBy": hook.STARTED_BY},
        listing,
    )
    document = plan(world, tmp_path)
    assert document["status"] == "blocked"
    assert reason in document["blocks"]


def test_single_flight_listing_uses_started_by_as_the_only_filter() -> None:
    # ECS ListTasks: startedBy must be the only filter; the default desired
    # status RUNNING covers PROVISIONING/PENDING/RUNNING tasks.
    assert hook.list_registration_tasks_request() == {
        "cluster": "neurostack-staging",
        "startedBy": hook.STARTED_BY,
    }
    assert re.fullmatch(r"[A-Za-z0-9_/-]{1,128}", hook.STARTED_BY)


def test_a_recorded_run_blocks_a_second_plan(tmp_path: Path) -> None:
    world = World()
    world.record_run(tmp_path)
    document = plan(world, tmp_path)
    assert "run_task_already_recorded" in document["blocks"]


def _definition_with(**app_changes: Any) -> dict[str, Any]:
    return _task_definition(_app_container(**app_changes))


TASK_DEFINITION_STATES: dict[str, tuple[Callable[[], dict[str, Any]], str]] = {
    "inactive": (
        lambda: _task_definition(status="INACTIVE"),
        "task_definition_not_active",
    ),
    "other_family": (
        lambda: _task_definition(family="neurostack-staging-migrations"),
        "task_definition_identity_mismatch",
    ),
    "other_arn": (
        lambda: _task_definition(taskDefinitionArn=OLD_ARN),
        "task_definition_identity_mismatch",
    ),
    "no_app": (
        lambda: _task_definition(_app_container(name="web")),
        "task_definition_app_container_missing",
    ),
    "entry_point": (
        lambda: _definition_with(entryPoint=["sh", "-c"]),
        "app_entry_point_unexpected",
    ),
    "not_essential": (
        lambda: _definition_with(essential=False),
        "app_container_not_essential",
    ),
    "log_driver": (
        lambda: _definition_with(logConfiguration={"logDriver": "json-file"}),
        "app_log_configuration_unusable",
    ),
    "log_prefix_missing": (
        lambda: _definition_with(
            logConfiguration={
                "logDriver": "awslogs",
                "options": {"awslogs-group": LOG_GROUP, "awslogs-region": "us-east-1"},
            }
        ),
        "app_log_configuration_unusable",
    ),
    "log_other_region": (
        lambda: _definition_with(
            logConfiguration={
                "logDriver": "awslogs",
                "options": {
                    "awslogs-group": LOG_GROUP,
                    "awslogs-region": "us-east-2",
                    "awslogs-stream-prefix": "ecs",
                },
            }
        ),
        "app_log_configuration_unusable",
    ),
    "image_tag": (
        lambda: _definition_with(image=f"{REGISTRY}/neurostack-staging:bridge-1"),
        "app_image_not_digest_pinned",
    ),
    "image_other_registry": (
        lambda: _definition_with(image="docker.io/library/python@" + BRIDGE_DIGEST),
        "app_image_not_digest_pinned",
    ),
    "no_webhook_secret": (
        lambda: _definition_with(
            secrets=[
                {"name": "DATABASE_URL", "valueFrom": SECRET_PREFIX + "database-url-x"}
            ]
        ),
        "task_definition_missing_webhook_wiring",
    ),
    "no_webhook_url": (
        lambda: _definition_with(environment=[{"name": "ENVIRONMENT", "value": "x"}]),
        "task_definition_missing_webhook_wiring",
    ),
}


@pytest.mark.parametrize("state", sorted(TASK_DEFINITION_STATES))
def test_plan_refuses_an_unusable_task_definition(state: str, tmp_path: Path) -> None:
    make, reason = TASK_DEFINITION_STATES[state]
    world = World()
    world.put("ecs", "DescribeTaskDefinition", {"taskDefinition": EXPECTED_ARN}, make())
    document = plan(world, tmp_path)
    assert document["status"] == "blocked"
    assert reason in document["blocks"], document["blocks"]


@pytest.mark.parametrize(
    "response",
    [
        {"__error_code__": "ImageNotFoundException"},
        {"imageDetails": []},
        _image_details("sha256:" + "3" * 64),
    ],
)
def test_plan_refuses_an_image_that_is_gone_from_ecr(
    response: dict[str, Any], tmp_path: Path
) -> None:
    world = World()
    world.put(
        "ecr",
        "DescribeImages",
        {
            "repositoryName": "neurostack-staging",
            "imageIds": [{"imageDigest": BRIDGE_DIGEST}],
        },
        response,
    )
    assert "app_image_not_found_in_ecr" in plan(world, tmp_path)["blocks"]


@pytest.mark.parametrize(
    "operation,params",
    [
        (
            "DescribeServices",
            {"cluster": "neurostack-staging", "services": ["neurostack-staging-app"]},
        ),
        ("ListTasks", {"cluster": "neurostack-staging", "startedBy": hook.STARTED_BY}),
        ("DescribeTaskDefinition", {"taskDefinition": EXPECTED_ARN}),
    ],
)
def test_a_missing_read_fails_closed(
    operation: str,
    params: dict[str, Any],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    world = World()
    world.drop("ecs", operation, params)
    for command in ("plan", "render-run-task"):
        code, out, err = run([command, *_args(world, tmp_path), *NOW_ARGS], capsys)
        assert code == 2
        assert out == ""
        assert f"blocked: readback_missing:ecs.{operation}" in err


@pytest.mark.parametrize(
    "arn",
    [
        OLD_ARN.replace("neurostack-staging-app", "neurostack-staging-migrations"),
        EXPECTED_ARN.replace(ACCOUNT, "111111111111"),
        EXPECTED_ARN.replace("us-east-1", "us-east-2"),
        EXPECTED_ARN + "0x",
        "neurostack-staging-app:9",
        EXPECTED_ARN.replace(":9", ":0"),
    ],
)
def test_expected_task_definition_argument_is_an_exact_app_revision_arn(
    arn: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = World().write(tmp_path / "readback.json")
    code, out, err = run(
        [
            "plan",
            "--expected-task-definition",
            arn,
            "--readback-file",
            str(path),
            *NOW_ARGS,
        ],
        capsys,
    )
    assert code == 2
    assert out == ""
    assert "blocked: expected_task_definition_invalid" in err


READBACK_MUTATIONS: dict[str, tuple[Callable[[dict[str, Any]], None], str]] = {
    "account": (
        lambda raw: raw.__setitem__("account_id", "111111111111"),
        "readback_file_shape_invalid",
    ),
    "region": (
        lambda raw: raw.__setitem__("region", "us-east-2"),
        "readback_file_shape_invalid",
    ),
    "schema": (
        lambda raw: raw.__setitem__("schema_version", "other.v1"),
        "readback_file_shape_invalid",
    ),
    "extra_key": (
        lambda raw: raw.__setitem__("note", "x"),
        "readback_file_shape_invalid",
    ),
    "captured_at": (
        lambda raw: raw.__setitem__("captured_at_utc", "yesterday"),
        "readback_file_shape_invalid",
    ),
    "naive_captured_at": (
        lambda raw: raw.__setitem__("captured_at_utc", "2026-10-20T01:10:00"),
        "readback_file_shape_invalid",
    ),
    "mutation_recorded_as_read": (
        lambda raw: raw["reads"].append(
            {
                "service": "ecs",
                "operation": "UpdateService",
                "params": {},
                "response": {},
            }
        ),
        "readback_file_operation_not_allowed",
    ),
    "secret_material": (
        lambda raw: raw["reads"][0]["response"].__setitem__("SecretString", "x"),
        "secret_material_in_readback",
    ),
    "duplicate_read": (
        lambda raw: raw["reads"].append(copy.deepcopy(raw["reads"][0])),
        "readback_file_duplicate_read",
    ),
    "read_shape": (
        lambda raw: raw["reads"][0].pop("params"),
        "readback_file_read_invalid",
    ),
    "run_task_shape": (
        lambda raw: raw.__setitem__("run_task", {"params": {}}),
        "readback_file_run_task_invalid",
    ),
}


@pytest.mark.parametrize("state", sorted(READBACK_MUTATIONS))
def test_readback_file_is_validated(state: str, tmp_path: Path) -> None:
    mutate, reason = READBACK_MUTATIONS[state]
    raw = World().document()
    mutate(raw)
    path = tmp_path / "readback.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(hook.HookError, match=reason):
        hook.load_readback(path)


@pytest.mark.parametrize(
    "text",
    [
        '{"a": 1, "a": 2}',
        '{"x": NaN}',
        "not json",
        "[]",
    ],
)
def test_readback_file_json_is_strict(text: str, tmp_path: Path) -> None:
    path = tmp_path / "readback.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(hook.HookError):
        hook.load_readback(path)


# ---------------------------------------------------------------------------
# render-run-task: the exact RunTask request
# ---------------------------------------------------------------------------


def test_render_prints_the_exact_run_task_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    code, out, err = run(
        ["render-run-task", *_args(world, tmp_path), *NOW_ARGS], capsys
    )
    assert code == 0, err
    request = json.loads(out)
    token = request.pop("clientToken")
    assert request == {
        "cluster": "neurostack-staging",
        "taskDefinition": EXPECTED_ARN,
        "count": 1,
        "launchType": "FARGATE",
        "platformVersion": "LATEST",
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": SUBNETS,
                "securityGroups": SECURITY_GROUPS,
                "assignPublicIp": "DISABLED",
            }
        },
        "overrides": {"containerOverrides": [{"name": "app", "command": COMMAND}]},
        "startedBy": hook.STARTED_BY,
        "enableECSManagedTags": False,
        "enableExecuteCommand": False,
    }
    assert "propagateTags" not in out
    assert re.fullmatch(r"asaas-webhook-[0-9a-f]{40}", token)
    assert len(token) <= 64


# The live RunTask API accepted exactly these keys (2026-10-20, staging):
LIVE_ACCEPTED_RUN_TASK_KEYS = {
    "cluster",
    "taskDefinition",
    "launchType",
    "count",
    "startedBy",
    "enableExecuteCommand",
    "networkConfiguration",
    "overrides",
}
# Documented RunTask parameters the hook also sends; unverified live until the
# first real registration run (runbook: stop and report the exact message on
# InvalidParameterException).
UNVERIFIED_LIVE_RUN_TASK_KEYS = {
    "clientToken",
    "platformVersion",
    "enableECSManagedTags",
}


def test_rendered_run_task_key_set_is_pinned(tmp_path: Path) -> None:
    """Nothing new reaches RunTask unnoticed; propagateTags never does."""

    request = render(World(), tmp_path)
    assert set(request) == LIVE_ACCEPTED_RUN_TASK_KEYS | UNVERIFIED_LIVE_RUN_TASK_KEYS
    assert set(request["networkConfiguration"]) == {"awsvpcConfiguration"}
    assert set(request["networkConfiguration"]["awsvpcConfiguration"]) == {
        "subnets",
        "securityGroups",
        "assignPublicIp",
    }
    assert set(request["overrides"]) == {"containerOverrides"}
    assert [set(row) for row in request["overrides"]["containerOverrides"]] == [
        {"name", "command"}
    ]
    assert hook.RUN_TASK_REQUIRED_KEYS == frozenset(
        LIVE_ACCEPTED_RUN_TASK_KEYS | {"clientToken", "enableECSManagedTags"}
    )
    assert hook.RUN_TASK_OPTIONAL_KEYS == frozenset({"platformVersion"})
    assert (
        "propagateTags" not in hook.RUN_TASK_REQUIRED_KEYS | hook.RUN_TASK_OPTIONAL_KEYS
    )
    forced = render(World(), tmp_path, force=True, attempt=2)
    assert set(forced) == set(request)


@pytest.mark.parametrize("value", ["NONE", "TASK_DEFINITION", "SERVICE", None, ""])
def test_run_task_invariant_refuses_propagate_tags_with_any_value(
    value: Any, tmp_path: Path
) -> None:
    request = render(World(), tmp_path)
    request["propagateTags"] = value
    with pytest.raises(
        hook.HookError,
        match="^run_task_request_invariant:propagate_tags_rejected_by_run_task_api$",
    ):
        hook.assert_run_task_invariants(request, EXPECTED_ARN)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.__setitem__("tags", [{"key": "a", "value": "b"}]),
        lambda r: r.__setitem__("group", "family:x"),
        lambda r: r.__setitem__("capacityProviderStrategy", []),
        lambda r: r.pop("clientToken"),
        lambda r: r.__setitem__("clientToken", "x" * 64),
        lambda r: r.pop("enableECSManagedTags"),
        lambda r: r["networkConfiguration"].__setitem__("extra", {}),
        lambda r: r["networkConfiguration"]["awsvpcConfiguration"].__setitem__(
            "extra", "x"
        ),
    ],
)
def test_run_task_invariant_refuses_a_changed_key_set(
    mutate: Callable[[dict[str, Any]], None], tmp_path: Path
) -> None:
    request = render(World(), tmp_path)
    mutate(request)
    with pytest.raises(hook.HookError, match="^run_task_request_invariant"):
        hook.assert_run_task_invariants(request, EXPECTED_ARN)


def test_run_task_invariant_allows_an_absent_platform_version(tmp_path: Path) -> None:
    request = render(World(), tmp_path)
    request.pop("platformVersion")
    hook.assert_run_task_invariants(request, EXPECTED_ARN)


@pytest.mark.parametrize("value", ["NONE", "TASK_DEFINITION", "SERVICE"])
def test_a_recorded_run_with_propagate_tags_is_pending(
    value: str, tmp_path: Path
) -> None:
    world = World()
    params = render(world, tmp_path)
    params["propagateTags"] = value
    world.record_run(tmp_path, params=params)
    world.finish(*ok_lines())
    result = verify(world, tmp_path)
    assert result["outcome"] == "DEPLOYED_WEBHOOK_PENDING"
    assert "run_task_request_propagate_tags_present" in result["reasons"]
    assert "run_task_request_not_rendered_by_hook" in result["reasons"]


def test_runbook_says_propagate_tags_is_omitted_on_purpose() -> None:
    text = " ".join(
        (ROOT / "docs/runbooks/asaas-deployment.md").read_text(encoding="utf-8").split()
    )
    assert "`propagateTags` `NONE`" not in text
    assert "`propagateTags` is omitted on purpose" in text
    assert "rejects `NONE`" in text
    assert "run_task_request_propagate_tags_present" in text
    assert "InvalidParameterException" in text
    for key in sorted(UNVERIFIED_LIVE_RUN_TASK_KEYS):
        assert f"`{key}`" in text


def test_force_rewrite_token_is_explicit_and_off_by_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    _code, out, _err = run(
        ["render-run-task", *_args(world, tmp_path), *NOW_ARGS], capsys
    )
    plain = json.loads(out)
    code, out, err = run(
        [
            "render-run-task",
            *_args(world, tmp_path),
            *NOW_ARGS,
            "--force-rewrite-token",
        ],
        capsys,
    )
    assert code == 0, err
    forced = json.loads(out)
    assert forced["overrides"]["containerOverrides"] == [
        {"name": "app", "command": FORCE_COMMAND}
    ]
    assert plain["overrides"]["containerOverrides"][0]["command"] == COMMAND
    assert forced["clientToken"] != plain["clientToken"]
    without_command = {
        k: v for k, v in forced.items() if k not in {"overrides", "clientToken"}
    }
    assert without_command == {
        k: v for k, v in plain.items() if k not in {"overrides", "clientToken"}
    }


def test_client_token_is_deterministic_per_deployment_command_and_attempt(
    tmp_path: Path,
) -> None:
    world = World()
    first = render(world, tmp_path)
    assert render(world, tmp_path) == first
    second = render(world, tmp_path, attempt=2)
    assert second["clientToken"] != first["clientToken"]
    assert {k: v for k, v in second.items() if k != "clientToken"} == {
        k: v for k, v in first.items() if k != "clientToken"
    }
    world.put(
        "ecs",
        "DescribeServices",
        {"cluster": "neurostack-staging", "services": ["neurostack-staging-app"]},
        _service_with_deployment(id="ecs-svc/999"),
    )
    assert render(world, tmp_path)["clientToken"] != first["clientToken"]


@pytest.mark.parametrize("attempt", ["0", "21", "x", "-1"])
def test_attempt_is_bounded(
    attempt: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    code, out, _err = run(
        ["render-run-task", *_args(world, tmp_path), *NOW_ARGS, "--attempt", attempt],
        capsys,
    )
    assert code == 2
    assert out == ""


def test_platform_version_is_copied_only_when_valid(tmp_path: Path) -> None:
    world = World()
    world.put(
        "ecs",
        "DescribeServices",
        {"cluster": "neurostack-staging", "services": ["neurostack-staging-app"]},
        _service(platformVersion="1.4.0"),
    )
    assert render(world, tmp_path)["platformVersion"] == "1.4.0"
    service = _service()
    del service["services"][0]["platformVersion"]
    world.put(
        "ecs",
        "DescribeServices",
        {"cluster": "neurostack-staging", "services": ["neurostack-staging-app"]},
        service,
    )
    assert "platformVersion" not in render(world, tmp_path)
    world.put(
        "ecs",
        "DescribeServices",
        {"cluster": "neurostack-staging", "services": ["neurostack-staging-app"]},
        _service(platformVersion="1.4.0; rm"),
    )
    assert "service_platform_version_invalid" in plan(world, tmp_path)["blocks"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.__setitem__("count", 2),
        lambda r: r["networkConfiguration"]["awsvpcConfiguration"].__setitem__(
            "assignPublicIp", "ENABLED"
        ),
        lambda r: r.__setitem__("overrides", {"containerOverrides": []}),
        lambda r: r["overrides"]["containerOverrides"][0].__setitem__(
            "command", ["alembic", "upgrade", "035"]
        ),
        lambda r: r["overrides"]["containerOverrides"][0].__setitem__("name", "otel"),
        lambda r: r.__setitem__("launchType", "EC2"),
        lambda r: r.__setitem__("propagateTags", "TASK_DEFINITION"),
        lambda r: r.__setitem__("propagateTags", "NONE"),
        lambda r: r.__setitem__("startedBy", "ecs-svc/1"),
        lambda r: r.__setitem__("cluster", "other"),
        lambda r: r.__setitem__("enableExecuteCommand", True),
        lambda r: r["networkConfiguration"]["awsvpcConfiguration"].__setitem__(
            "subnets", []
        ),
        lambda r: r.__setitem__("taskDefinition", OLD_ARN),
        lambda r: r["overrides"]["containerOverrides"].append(
            {"name": "app", "environment": [{"name": "X", "value": "y"}]}
        ),
    ],
)
def test_run_task_invariant_guard_refuses_any_widening(
    mutate: Callable[[dict[str, Any]], None], tmp_path: Path
) -> None:
    request = render(World(), tmp_path)
    hook.assert_run_task_invariants(request, EXPECTED_ARN)
    mutate(request)
    with pytest.raises(hook.HookError, match="run_task_request_invariant"):
        hook.assert_run_task_invariants(request, EXPECTED_ARN)


# ---------------------------------------------------------------------------
# render-verify-reads
# ---------------------------------------------------------------------------


def test_verify_reads_name_the_task_and_its_app_log_stream(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    world.record_run(tmp_path)
    code, out, err = run(["render-verify-reads", *_args(world, tmp_path)], capsys)
    assert code == 0, err
    document = json.loads(out)
    assert document["requests"] == [
        {
            "service": "ecs",
            "operation": "DescribeTasks",
            "params": {"cluster": "neurostack-staging", "tasks": [TASK_ARN]},
        },
        {
            "service": "logs",
            "operation": "GetLogEvents",
            "params": {
                "logGroupName": LOG_GROUP,
                "logStreamName": f"ecs/app/{TASK_ID}",
                "startFromHead": True,
                "limit": hook.LOG_EVENTS_LIMIT,
            },
        },
    ]
    assert {
        (row["service"], row["operation"]) for row in document["requests"]
    } <= hook.EMITTED_OPERATIONS


def test_verify_reads_refuse_without_a_recorded_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(["render-verify-reads", *_args(World(), tmp_path)], capsys)
    assert code == 2
    assert out == ""
    assert "blocked: run_task_not_recorded" in err


def test_verify_reads_refuse_when_run_task_started_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    world.record_run(
        tmp_path,
        response={"tasks": [], "failures": [{"reason": "RESOURCE:ENI"}]},
    )
    code, out, err = run(["render-verify-reads", *_args(world, tmp_path)], capsys)
    assert code == 2
    assert out == ""
    assert "blocked: run_task_started_no_task" in err


# ---------------------------------------------------------------------------
# verify-result
# ---------------------------------------------------------------------------


def test_exit_zero_with_verdict_ready_is_registered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = completed(tmp_path, "starting", *ok_lines())
    code, out, err = run(["verify-result", *_args(world, tmp_path)], capsys)
    assert code == 0, err
    result = json.loads(out)
    assert result["outcome"] == "registered"
    assert result["alert"] is False
    assert result["reasons"] == []
    assert result["container_exit_code"] == 0
    assert result["registration"] == result_doc()
    assert result["log_lines"] == {"result": 1, "log_twin": 1, "foreign": 1, "pages": 2}
    assert result["task_arn"] == TASK_ARN
    assert result["attempt"] == 1


def test_the_log_twin_is_optional(tmp_path: Path) -> None:
    world = completed(tmp_path, result_line(result_doc()))
    assert verify(world, tmp_path)["outcome"] == "registered"


def test_ready_after_a_create_with_the_force_flag_is_registered(tmp_path: Path) -> None:
    doc = result_doc(action="created", writes=1, force=True)
    world = completed(tmp_path, *ok_lines(doc), force=True)
    result = verify(world, tmp_path)
    assert result["outcome"] == "registered", result["reasons"]
    assert result["registration"]["force_rewrite_token"] is True


@pytest.mark.parametrize(
    "verdict",
    [
        "absent",
        "drift",
        "interrupted",
        "disabled",
        "ambiguous",
        "unconfigured",
        "error",
    ],
)
def test_every_non_ready_cli_verdict_is_an_alert_never_a_rollback(
    verdict: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    doc = result_doc(verdict, reasons=("transient",) if verdict == "error" else ())
    exit_code = hook.CLI_EXIT_CODES[verdict]
    assert exit_code in {10, 11, 12, 13, 20, 30, 40}
    world = completed(tmp_path, *ok_lines(doc), exit_code=exit_code)
    code, out, _err = run(["verify-result", *_args(world, tmp_path)], capsys)
    assert code == 4
    result = json.loads(out)
    assert result["outcome"] == "DEPLOYED_WEBHOOK_PENDING"
    assert result["alert"] is True
    assert result["cli_verdict"] == verdict
    assert result["container_exit_code"] == exit_code
    assert "cli_verdict_not_ready" in result["reasons"]
    assert result["on_failure"] == hook.FAILURE_POLICY
    _assert_alert_only(out)


def _assert_alert_only(text: str) -> None:
    lowered = text.lower()
    for word in (
        "updateservice",
        "update-service",
        "update_service",
        "registertaskdefinition",
        "register-task-definition",
        "register_task_definition",
        "deregister",
        "stoptask",
        "rollback",
    ):
        assert word not in lowered, word
    assert "://" not in text
    assert "@" not in text.replace("@sha256:", "")
    assert "$aact" not in text


PENDING_CASES: dict[str, tuple[Callable[[Path], World], str]] = {
    "exit_zero_verdict_drift": (
        lambda p: completed(p, result_line(result_doc("drift", exit_code=0))),
        "result_line_malformed",
    ),
    "exit_zero_doc_says_drift": (
        lambda p: completed(p, result_line(result_doc("drift")), exit_code=0),
        "exit_code_verdict_mismatch",
    ),
    "exit_nonzero_verdict_ready": (
        lambda p: completed(p, result_line(result_doc()), exit_code=11),
        "exit_code_verdict_mismatch",
    ),
    "malformed_line": (
        lambda p: completed(p, result_line(result_doc())[:-5], twin_line(result_doc())),
        "result_line_malformed",
    ),
    "malformed_twin": (
        lambda p: completed(p, result_line(result_doc()), twin_line(result_doc())[:-2]),
        "result_line_malformed",
    ),
    "two_result_lines": (
        lambda p: completed(p, result_line(result_doc()), result_line(result_doc())),
        "result_line_duplicate",
    ),
    "two_twins": (
        lambda p: completed(
            p,
            result_line(result_doc()),
            twin_line(result_doc()),
            twin_line(result_doc()),
        ),
        "log_twin_duplicate",
    ),
    "twin_disagrees": (
        lambda p: completed(
            p,
            result_line(result_doc()),
            twin_line(result_doc(action="updated", writes=1)),
        ),
        "log_twin_disagrees",
    ),
    "no_result_line": (
        lambda p: completed(p, "starting", twin_line(result_doc())),
        "result_line_missing",
    ),
    "unknown_verdict": (
        lambda p: completed(
            p, result_line({**result_doc(), "verdict": "registered", "exit_code": 0})
        ),
        "result_line_malformed",
    ),
    "unknown_reason": (
        lambda p: completed(p, result_line(result_doc(reasons=("all_good",)))),
        "result_line_malformed",
    ),
    "extra_key": (
        lambda p: completed(p, result_line({**result_doc(), "webhook_url": "x"})),
        "result_line_malformed",
    ),
    "bool_count": (
        lambda p: completed(
            p,
            result_line(
                {**result_doc(), "counts": {**result_doc()["counts"], "writes": True}}
            ),
        ),
        "result_line_malformed",
    ),
    "digest_not_hex": (
        lambda p: completed(p, result_line(result_doc(digest="wbh_0123456789"))),
        "result_line_malformed",
    ),
    "other_schema": (
        lambda p: completed(
            p,
            result_line(
                {**result_doc(), "schema": "neurostack.asaas_webhook_registration.v2"}
            ),
        ),
        "result_line_malformed",
    ),
    "not_an_object": (
        lambda p: completed(p, '["neurostack.asaas_webhook_registration.v1"]'),
        "result_line_malformed",
    ),
    "readback_command": (
        lambda p: completed(p, result_line(result_doc(command="readback"))),
        "result_command_mismatch",
    ),
    "force_flag_mismatch": (
        lambda p: completed(p, *ok_lines(result_doc(force=True))),
        "result_force_flag_mismatch",
    ),
    "allow_enable_set": (
        lambda p: completed(p, *ok_lines(result_doc(allow_enable=True))),
        "result_options_mismatch",
    ),
    "no_resume_set": (
        lambda p: completed(p, *ok_lines(result_doc(no_resume=True))),
        "result_options_mismatch",
    ),
    "options_not_bool": (
        lambda p: completed(p, result_line({**result_doc(), "no_resume": "no"})),
        "result_line_malformed",
    ),
    "force_not_applied": (
        lambda p: completed(p, *ok_lines(result_doc(force=True)), force=True),
        "force_rewrite_not_applied",
    ),
    "ready_without_digest": (
        lambda p: completed(p, *ok_lines(result_doc(digest=None))),
        "result_ready_inconsistent",
    ),
    "ready_with_failed_check": (
        lambda p: completed(p, *ok_lines(result_doc(checks=False))),
        "result_ready_inconsistent",
    ),
    "ready_with_two_writes": (
        lambda p: completed(p, *ok_lines(result_doc(action="updated", writes=2))),
        "result_ready_inconsistent",
    ),
    "ready_without_action_but_two_writes": (
        lambda p: completed(p, *ok_lines(result_doc(action="none", writes=2))),
        "result_ready_inconsistent",
    ),
    "created_without_write": (
        lambda p: completed(p, *ok_lines(result_doc(action="created", writes=0))),
        "result_ready_inconsistent",
    ),
    "crash_exit_code": (
        lambda p: completed(p, "Traceback (most recent call last):", exit_code=1),
        "cli_exit_code_unknown",
    ),
    "sigkill": (
        lambda p: completed(p, *ok_lines(), exit_code=137),
        "cli_exit_code_unknown",
    ),
    "url_in_foreign_line": (
        lambda p: completed(p, "GET " + LEAKED_URL, *ok_lines()),
        "log_forbidden_material",
    ),
    "email_in_line": (
        lambda p: completed(p, "notify " + LEAKED_EMAIL, *ok_lines()),
        "log_forbidden_material",
    ),
    "key_in_line": (
        lambda p: completed(p, "key=" + LEAKED_KEY, *ok_lines()),
        "log_forbidden_material",
    ),
    "webhook_path_in_line": (
        lambda p: completed(p, "PUT " + LEAKED_ID_PATH, *ok_lines()),
        "log_forbidden_material",
    ),
    "auth_token_in_line": (
        lambda p: completed(p, '{"authToken": "x"}', *ok_lines()),
        "log_forbidden_material",
    ),
    "private_key_in_line": (
        lambda p: completed(p, "-----" + "BEGIN PRIVATE KEY-----", *ok_lines()),
        "log_forbidden_material",
    ),
    "url_inside_the_result_line": (
        lambda p: completed(
            p,
            result_line(result_doc())[:-1] + ',"x":"' + LEAKED_URL + '"}',
        ),
        "log_forbidden_material",
    ),
}


@pytest.mark.parametrize("case", sorted(PENDING_CASES))
def test_every_contract_violation_is_the_pending_alert(
    case: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make, reason = PENDING_CASES[case]
    world = make(tmp_path)
    code, out, err = run(["verify-result", *_args(world, tmp_path)], capsys)
    assert code == 4, err
    result = json.loads(out)
    assert result["outcome"] == "DEPLOYED_WEBHOOK_PENDING"
    assert result["alert"] is True
    assert reason in result["reasons"], result["reasons"]
    _assert_alert_only(out + err)
    for leaked in (LEAKED_URL, LEAKED_EMAIL, LEAKED_KEY, LEAKED_ID_PATH, "wbh_"):
        assert leaked not in out + err


def test_forbidden_material_is_flagged_as_a_suspected_exposure(tmp_path: Path) -> None:
    world = completed(tmp_path, "key=" + LEAKED_KEY, *ok_lines())
    result = verify(world, tmp_path)
    assert "secret_exposure_suspected" in result["alert_kinds"]
    assert result["registration"] == result_doc()


def test_stopped_reason_free_text_is_never_printed(tmp_path: Path) -> None:
    world = completed(tmp_path, *ok_lines())
    text = json.dumps(verify(world, tmp_path))
    assert "Essential container" not in text
    assert "://" not in text


def test_a_running_task_inside_the_watch_window_is_in_progress(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    world.record_run(tmp_path)
    running = _described_task(exit_code=None, lastStatus="RUNNING")
    del running["tasks"][0]["stoppedAt"]
    world.finish(task=running)
    world.captured_at = "2026-10-20T01:05:00Z"
    code, out, _err = run(["verify-result", *_args(world, tmp_path)], capsys)
    assert code == 3
    result = json.loads(out)
    assert result["outcome"] == "in_progress"
    assert result["alert"] is False
    assert result["reasons"] == ["task_not_stopped"]
    assert {(r["service"], r["operation"]) for r in result["next_requests"]} == {
        ("ecs", "DescribeTasks"),
        ("logs", "GetLogEvents"),
    }
    world.captured_at = "2026-10-20T01:10:01Z"
    result = verify(world, tmp_path)
    assert result["outcome"] == "DEPLOYED_WEBHOOK_PENDING"
    assert "task_watch_deadline_exceeded" in result["reasons"]
    assert result["next_requests"] == []


def test_logs_not_yet_delivered_are_in_progress_then_missing(tmp_path: Path) -> None:
    world = completed(tmp_path)
    world.captured_at = "2026-10-20T01:04:00Z"
    result = verify(world, tmp_path)
    assert result["outcome"] == "in_progress"
    assert result["reasons"] == ["log_events_not_delivered"]
    world.captured_at = "2026-10-20T01:05:01Z"
    result = verify(world, tmp_path)
    assert result["outcome"] == "DEPLOYED_WEBHOOK_PENDING"
    assert "result_line_missing" in result["reasons"]


def test_missing_log_stream_after_grace_is_pending(tmp_path: Path) -> None:
    world = World()
    world.record_run(tmp_path)
    world.finish(logs={"__error_code__": "ResourceNotFoundException"})
    result = verify(world, tmp_path)
    assert result["outcome"] == "DEPLOYED_WEBHOOK_PENDING"
    assert "result_line_missing" in result["reasons"]


def test_task_that_never_started_is_pending_with_its_stop_code(tmp_path: Path) -> None:
    world = World()
    world.record_run(tmp_path)
    failed = _described_task(exit_code=None, stopCode="TaskFailedToStart")
    world.finish(task=failed, logs={"__error_code__": "ResourceNotFoundException"})
    result = verify(world, tmp_path)
    assert result["outcome"] == "DEPLOYED_WEBHOOK_PENDING"
    assert "task_stopped_without_app_exit_code" in result["reasons"]
    assert result["stop_code"] == "TaskFailedToStart"
    assert result["container_exit_code"] is None


def test_unknown_stop_code_is_not_echoed(tmp_path: Path) -> None:
    world = World()
    world.record_run(tmp_path)
    world.finish(*ok_lines(), task=_described_task(stopCode="Weird " + LEAKED_URL))
    result = verify(world, tmp_path)
    assert result["stop_code"] == "other"
    assert "://" not in json.dumps(result)


def test_run_task_failure_is_pending_without_any_task_read(tmp_path: Path) -> None:
    world = World()
    world.record_run(
        tmp_path, response={"tasks": [], "failures": [{"reason": "RESOURCE:ENI"}]}
    )
    result = verify(world, tmp_path)
    assert result["outcome"] == "DEPLOYED_WEBHOOK_PENDING"
    assert result["reasons"] == ["run_task_failed"]
    assert "RESOURCE" not in json.dumps(result)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.__setitem__("count", 2),
        lambda r: r["networkConfiguration"]["awsvpcConfiguration"].__setitem__(
            "assignPublicIp", "ENABLED"
        ),
        lambda r: r.pop("overrides"),
        lambda r: r["overrides"]["containerOverrides"][0].__setitem__(
            "command", ["python", "-m", "src.ops.asaas_webhook", "readback"]
        ),
        lambda r: r.__setitem__("clientToken", "asaas-webhook-" + "0" * 40),
        lambda r: r.__setitem__("taskDefinition", OLD_ARN),
    ],
)
def test_a_run_the_hook_did_not_render_is_pending(
    mutate: Callable[[dict[str, Any]], None], tmp_path: Path
) -> None:
    world = World()
    params = render(world, tmp_path)
    mutate(params)
    world.record_run(tmp_path, params=params)
    world.finish(*ok_lines())
    result = verify(world, tmp_path)
    assert result["outcome"] == "DEPLOYED_WEBHOOK_PENDING"
    assert "run_task_request_not_rendered_by_hook" in result["reasons"]


def test_a_later_attempt_is_recognised(tmp_path: Path) -> None:
    world = World()
    world.record_run(tmp_path, attempt=3)
    world.finish(*ok_lines())
    result = verify(world, tmp_path)
    assert result["outcome"] == "registered"
    assert result["attempt"] == 3


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"taskArn": TASK_ARN.replace("0f1e", "ffff")}, "task_identity_mismatch"),
        ({"taskDefinitionArn": OLD_ARN}, "task_identity_mismatch"),
        ({"startedBy": "ecs-svc/1"}, "task_identity_mismatch"),
        (
            {
                "overrides": {
                    "containerOverrides": [
                        {"name": "app", "command": ["python", "-m", "x"]}
                    ]
                }
            },
            "task_command_override_mismatch",
        ),
        ({"overrides": {}}, "task_command_override_mismatch"),
    ],
)
def test_described_task_must_be_the_task_the_hook_started(
    changes: dict[str, Any], reason: str, tmp_path: Path
) -> None:
    world = World()
    world.record_run(tmp_path)
    world.finish(*ok_lines(), task=_described_task(**changes))
    result = verify(world, tmp_path)
    assert result["outcome"] == "DEPLOYED_WEBHOOK_PENDING"
    assert reason in result["reasons"]


def test_a_log_chain_longer_than_the_page_cap_is_pending(tmp_path: Path) -> None:
    world = World()
    world.record_run(tmp_path)
    world.finish()
    world.put_pages([["noise"]] * (hook.MAX_LOG_PAGES + 1))
    result = verify(world, tmp_path)
    assert result["outcome"] == "DEPLOYED_WEBHOOK_PENDING"
    assert "log_events_truncated" in result["reasons"]


def test_a_page_with_more_events_than_the_limit_is_invalid(tmp_path: Path) -> None:
    world = World()
    world.record_run(tmp_path)
    world.finish()
    world.put_pages([["noise"] * (hook.LOG_EVENTS_LIMIT + 1)])
    assert "log_events_invalid" in verify(world, tmp_path)["reasons"]


def test_verify_refuses_without_the_task_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    world.record_run(tmp_path)
    code, out, err = run(["verify-result", *_args(world, tmp_path)], capsys)
    assert code == 2
    assert out == ""
    assert "blocked: readback_missing:ecs.DescribeTasks" in err
    code, out, err = run(["verify-result", *_args(World(), tmp_path)], capsys)
    assert code == 2
    assert "blocked: run_task_not_recorded" in err


def test_verify_never_emits_a_service_update_or_registration_in_any_outcome(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenarios = []
    for name, (make, _reason) in PENDING_CASES.items():
        (tmp_path / name).mkdir()
        scenarios.append(make(tmp_path / name))
    (tmp_path / "ok").mkdir()
    scenarios.append(completed(tmp_path / "ok", *ok_lines()))
    for index, world in enumerate(scenarios):
        directory = tmp_path / f"s{index}"
        directory.mkdir(exist_ok=True)
        code, out, err = run(["verify-result", *_args(world, directory)], capsys)
        assert code in {0, 3, 4}
        result = json.loads(out)
        _assert_alert_only(out + err)
        for request in result["next_requests"]:
            assert (request["service"], request["operation"]) in hook.EMITTED_OPERATIONS


@pytest.mark.parametrize(
    "payload",
    [
        {"next": "UpdateService"},
        {"next": "RegisterTaskDefinition"},
        {"action": "rollback"},
        {"x": LEAKED_URL},
        {"x": LEAKED_EMAIL},
        {"x": LEAKED_KEY},
        {"x": "StopTask"},
    ],
)
def test_emit_guard_refuses_forbidden_output(payload: dict[str, Any]) -> None:
    with pytest.raises(hook.HookError, match="output_forbidden"):
        hook._emit(payload)


def test_script_runs_standalone_without_repository_on_the_path(tmp_path: Path) -> None:
    world = World()
    path = world.write(tmp_path / "readback.json")
    completed_process = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            str(SCRIPT),
            "plan",
            "--expected-task-definition",
            EXPECTED_ARN,
            "--readback-file",
            str(path),
            *NOW_ARGS,
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert completed_process.returncode == 0, completed_process.stderr
    assert json.loads(completed_process.stdout)["status"] == "ready"


# ---------------------------------------------------------------------------
# Fix loop 1: T1's real output (golden lines from PR #120 tip a13fa43f)
# ---------------------------------------------------------------------------

GOLDEN = ROOT / "tests/fixtures/asaas_deploy_hook/t1_registration_golden.json"
T1_CLI_TESTS = ROOT / "tests" / "ops" / "test_asaas_webhook_cli.py"
PENDING = "DEPLOYED_WEBHOOK_PENDING"
# case -> (hook outcome, command verdict)
GOLDEN_OUTCOMES = {
    "created": ("registered", "ready"),
    "exact_no_write": ("registered", "ready"),
    "force_rewrite_token": ("registered", "ready"),
    "interrupted_queue_resumed": ("registered", "ready"),
    "ambiguous": (PENDING, "ambiguous"),
    "error_auth": (PENDING, "error"),
    "disabled_by_operator": (PENDING, "disabled"),
    "unconfigured": (PENDING, "unconfigured"),
    "drift_requires_recreate": (PENDING, "drift"),
    "interrupted_not_converged": (PENDING, "interrupted"),
}


def _golden() -> dict[str, Any]:
    document: dict[str, Any] = json.loads(GOLDEN.read_text(encoding="utf-8"))
    return document


def test_golden_fixture_covers_every_outcome_the_hook_can_see() -> None:
    golden = _golden()
    assert golden["source"]["commit"].startswith("a13fa43f")
    assert golden["source"]["repository_path"] == "src/ops/asaas_webhook.py"
    assert set(golden["cases"]) == set(GOLDEN_OUTCOMES)
    codes = {case["exit_code"] for case in golden["cases"].values()}
    # absent (10) cannot end a reconcile: it creates the webhook first.
    assert codes == set(hook.CLI_EXIT_CODES.values()) - {10}


@pytest.mark.parametrize("name", sorted(GOLDEN_OUTCOMES))
def test_golden_lines_match_the_mirrored_contract_exactly(name: str) -> None:
    case = _golden()["cases"][name]
    document = json.loads(case["stdout"])
    twin = json.loads(case["stderr"])
    assert set(document) == set(hook._CLI_DOCUMENT_KEYS)
    assert set(document["checks"]) == set(hook.CLI_CHECK_NAMES)
    assert set(document["counts"]) == set(hook.CLI_COUNT_NAMES)
    assert hook.validate_cli_document(document) == document
    assert set(twin) == set(hook._CLI_DOCUMENT_KEYS) | set(hook._CLI_LOG_KEYS)
    assert {k: twin[k] for k in hook._CLI_DOCUMENT_KEYS} == document
    assert twin["event"] == hook.CLI_LOG_EVENT and twin["logger"] == hook.CLI_LOGGER
    assert case["exit_code"] == hook.CLI_EXIT_CODES[document["verdict"]]


@pytest.mark.parametrize("name", sorted(GOLDEN_OUTCOMES))
def test_real_t1_output_is_classified_without_a_contract_violation(
    name: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    case = _golden()["cases"][name]
    outcome, cli_verdict = GOLDEN_OUTCOMES[name]
    force = "--force-rewrite-token" in case["argv"]
    assert set(case["argv"]) <= {"reconcile", "--force-rewrite-token"}
    world = completed(
        tmp_path,
        case["stdout"],
        case["stderr"],
        force=force,
        exit_code=case["exit_code"],
    )
    code, out, err = run(["verify-result", *_args(world, tmp_path)], capsys)
    result = json.loads(out)
    assert result["outcome"] == outcome, result["reasons"]
    assert result["cli_verdict"] == cli_verdict
    assert result["registration"] == json.loads(case["stdout"])
    assert result["log_lines"]["result"] == 1 and result["log_lines"]["log_twin"] == 1
    if outcome == "registered":
        assert code == 0 and result["reasons"] == []
    else:
        assert code == 4
        # Only the verdict itself: no contract violation on real output.
        assert result["reasons"] == ["cli_verdict_not_ready"]


def _t1_contract_mismatches(source: str) -> list[str]:
    """Every difference between T1's source and the hook's mirror."""

    tree = ast.parse(source)
    expected = {
        "SCHEMA": hook.CLI_RESULT_SCHEMA,
        "LOG_EVENT": hook.CLI_LOG_EVENT,
        "LOGGER_NAME": hook.CLI_LOGGER,
        "EXIT_CODES": dict(hook.CLI_EXIT_CODES),
        "COMMANDS": hook.CLI_COMMANDS,
        "ACTIONS": set(hook.CLI_ACTIONS),
        "CHECK_NAMES": hook.CLI_CHECK_NAMES,
        "COUNT_NAMES": hook.CLI_COUNT_NAMES,
        "REASONS": set(hook.CLI_REASONS),
        "DEFAULT_DEADLINE_SECONDS": hook.CLI_DEADLINE_SECONDS,
    }
    mismatches = []
    for name, value in expected.items():
        try:
            found = _literal(tree, name)
        except AssertionError:
            mismatches.append(f"{name}:missing")
            continue
        if isinstance(value, set):
            found = set(found)
        elif isinstance(value, tuple):
            found = tuple(found)
        if found != value:
            mismatches.append(name)
    try:
        if _document_keys(tree) != set(hook._CLI_DOCUMENT_KEYS):
            mismatches.append("document_keys")
    except AssertionError:
        mismatches.append("document_keys:missing")
    for flag in (hook.FORCE_REWRITE_TOKEN_FLAG, *hook.CLI_FLAGS_NEVER_PASSED):
        if f'"{flag}"' not in source:
            mismatches.append(f"flag:{flag}")
    for key in hook._CLI_LOG_KEYS - {"event", "level", "logger"}:
        if f'"{key}"' not in source:
            mismatches.append(f"log_key:{key}")
    levels = {
        level
        for line in source.splitlines()
        if re.search(r'"level":\s*"', line)
        for level in re.findall(r'"([a-z]+)"', line)
        if level != "level"
    }
    if levels != set(hook.CLI_LOG_LEVELS):
        mismatches.append("log_levels")
    return mismatches


def _synthetic_t1_source(**overrides: Any) -> str:
    values: dict[str, Any] = {
        "SCHEMA": hook.CLI_RESULT_SCHEMA,
        "LOG_EVENT": hook.CLI_LOG_EVENT,
        "LOGGER_NAME": hook.CLI_LOGGER,
        "EXIT_CODES": dict(hook.CLI_EXIT_CODES),
        "COMMANDS": hook.CLI_COMMANDS,
        "ACTIONS": sorted(hook.CLI_ACTIONS),
        "CHECK_NAMES": hook.CLI_CHECK_NAMES,
        "COUNT_NAMES": hook.CLI_COUNT_NAMES,
        "REASONS": sorted(hook.CLI_REASONS),
        "DEFAULT_DEADLINE_SECONDS": hook.CLI_DEADLINE_SECONDS,
    }
    values.update(overrides)
    keys = sorted(hook._CLI_DOCUMENT_KEYS)
    lines = [f"{name} = {value!r}" for name, value in values.items()]
    lines.append("class RunResult:")
    lines.append("    def document(self):")
    lines.append("        return {" + ", ".join(f"{k!r}: 0" for k in keys) + "}")
    flags = [hook.FORCE_REWRITE_TOKEN_FLAG, *hook.CLI_FLAGS_NEVER_PASSED]
    lines.append("FLAGS = (" + ", ".join(f'"{f}"' for f in flags) + ",)")
    lines.append(
        "LOG = {" + ", ".join(f'"{k}": 0' for k in sorted(hook._CLI_LOG_KEYS)) + "}"
    )
    lines.append('LEVEL = {"level": "info" if True else "warning"}')
    return "\n".join(lines) + "\n"


def test_t1_comparator_fails_loudly_on_any_difference() -> None:
    assert _t1_contract_mismatches(_synthetic_t1_source()) == []
    short = tuple(n for n in hook.CLI_CHECK_NAMES if n != "webhook_url_allowed")
    assert _t1_contract_mismatches(_synthetic_t1_source(CHECK_NAMES=short)) == [
        "CHECK_NAMES"
    ]
    fewer = sorted(hook.CLI_REASONS - {"webhook_url_not_allowed"})
    assert _t1_contract_mismatches(_synthetic_t1_source(REASONS=fewer)) == ["REASONS"]
    assert _t1_contract_mismatches(_synthetic_t1_source(SCHEMA="x.v2")) == ["SCHEMA"]


@pytest.mark.skipif(
    not T1_CLI.exists(), reason="T1 CLI src/ops/asaas_webhook.py not in this tree"
)
def test_mirror_equals_the_t1_source_in_the_tree() -> None:
    assert _t1_contract_mismatches(T1_CLI.read_text(encoding="utf-8")) == []


@pytest.mark.skipif(
    not (T1_CLI.exists() and T1_CLI_TESTS.exists()),
    reason="T1 CLI and its tests not in this tree",
)
def test_live_t1_main_output_verifies_as_registered(tmp_path: Path) -> None:
    from tests.ops import test_asaas_webhook_cli as t1

    code, out, err = t1.invoke(["reconcile"], fake=t1.FakeAsaas())
    assert code == 0
    golden = json.loads(_golden()["cases"]["created"]["stdout"])
    live = json.loads(out)
    assert set(live) == set(golden)
    assert set(live["checks"]) == set(golden["checks"])
    world = completed(tmp_path, *[line for line in (out + err).splitlines() if line])
    assert verify(world, tmp_path)["outcome"] == "registered"


# ---------------------------------------------------------------------------
# Fix loop 1: stricter ready
# ---------------------------------------------------------------------------


def _doc_with(**changes: Any) -> dict[str, Any]:
    doc = result_doc()
    counts = changes.pop("counts", {})
    doc.update(changes)
    doc["counts"] = {**doc["counts"], **counts}
    return doc


@pytest.mark.parametrize(
    "doc,reason",
    [
        (_doc_with(counts={"candidates": 0}), "result_ready_inconsistent"),
        (
            _doc_with(
                action="updated", reasons=["webhook_reenabled"], counts={"writes": 1}
            ),
            "result_reason_contradicts_options",
        ),
        (
            _doc_with(
                action="updated",
                no_resume=True,
                reasons=["queue_resumed"],
                counts={"writes": 1},
            ),
            "result_reason_contradicts_options",
        ),
        (_doc_with(counts={"writes": hook.CLI_MAX_COUNT + 1}), "result_line_malformed"),
        (_doc_with(counts={"retries": -1}), "result_line_malformed"),
        (_doc_with(reasons=["token_rewritten"] * 65), "result_line_malformed"),
        (_doc_with(reasons=["settings_invalid:Bad-Field"]), "result_line_malformed"),
        (_doc_with(reasons=["settings_invalid:" + "a" * 65]), "result_line_malformed"),
    ],
)
def test_ready_documents_that_contradict_themselves_alert(
    doc: dict[str, Any], reason: str, tmp_path: Path
) -> None:
    world = completed(tmp_path, result_line(doc))
    result = verify(world, tmp_path)
    assert result["outcome"] == PENDING
    assert reason in result["reasons"]


CLI_MAX = hook.CLI_MAX_COUNT


def test_a_settings_field_reason_at_the_limits_is_accepted(tmp_path: Path) -> None:
    doc = _doc_with(
        verdict="unconfigured",
        exit_code=30,
        reasons=["settings_invalid:" + "a" * 64, "token_rewritten"],
    )
    world = completed(tmp_path, result_line(doc), exit_code=30)
    result = verify(world, tmp_path)
    assert result["reasons"] == ["cli_verdict_not_ready"]
    big = _doc_with(counts={"requests": CLI_MAX}, reasons=["transient"] * 64)
    assert verify(completed(tmp_path, result_line(big)), tmp_path)["outcome"] == (
        "registered"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"logger": "src.other"},
        {"level": "debug"},
        {"level": "error"},
        {"event": "other_event"},
    ],
)
def test_the_twin_line_must_be_the_command_log_line(
    changes: dict[str, Any], tmp_path: Path
) -> None:
    doc = result_doc()
    world = completed(tmp_path, result_line(doc), twin_line(doc, **changes))
    result = verify(world, tmp_path)
    assert result["outcome"] == PENDING
    assert "result_line_malformed" in result["reasons"]


@pytest.mark.parametrize("level", ["info", "warning"])
def test_both_twin_levels_are_accepted(level: str, tmp_path: Path) -> None:
    doc = result_doc()
    world = completed(tmp_path, result_line(doc), twin_line(doc, level=level))
    assert verify(world, tmp_path)["outcome"] == "registered"


@pytest.mark.parametrize(
    "line",
    [
        "POST /v1/billing/webhook/asaas",
        "host staging.api.neurostack.global",
        "api.asaas.com",
        "Authorization: Bearer abc",
        "access_token=abc",
        "AKIA" + "ABCDEFGHIJKLMNOP",
        "aws_secret_access_key=x",
        "asaas-access-token: x",
    ],
)
def test_host_path_and_credential_patterns_are_exposure(
    line: str, tmp_path: Path
) -> None:
    world = completed(tmp_path, line, *ok_lines())
    result = verify(world, tmp_path)
    assert result["outcome"] == PENDING
    assert "log_forbidden_material" in result["reasons"]
    assert line not in json.dumps(result)


# ---------------------------------------------------------------------------
# Fix loop 1: readback strictness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (
            lambda raw: raw.__setitem__("run_task", None),
            "readback_file_run_task_invalid",
        ),
        (
            lambda raw: raw.__setitem__("captured_at_utc", 1792371750),
            "readback_file_shape_invalid",
        ),
        (
            lambda raw: raw.__setitem__(
                "run_task",
                {"params": {}, "response": {"tasks": [{"SecretString": "x"}]}},
            ),
            "secret_material_in_readback",
        ),
    ],
)
def test_readback_strictness(
    mutate: Callable[[dict[str, Any]], None], reason: str, tmp_path: Path
) -> None:
    raw = World().document()
    mutate(raw)
    path = tmp_path / "readback.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(hook.HookError, match=reason):
        hook.load_readback(path)


def test_duplicate_keys_in_an_otherwise_valid_readback_are_refused(
    tmp_path: Path,
) -> None:
    text = json.dumps(World().document())
    assert text.startswith('{"schema_version"')
    doubled = text.replace(
        '"region": "us-east-1"', '"region": "us-east-2", "region": "us-east-1"', 1
    )
    assert doubled != text
    path = tmp_path / "readback.json"
    path.write_text(doubled, encoding="utf-8")
    with pytest.raises(hook.HookError, match="readback_file_unreadable"):
        hook.load_readback(path)


# ---------------------------------------------------------------------------
# Fix loop 1: plan freshness, exact containers and the reviewer's survivors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "now,reason",
    [
        (datetime(2026, 10, 20, 1, 20, 1, tzinfo=timezone.utc), "readback_stale"),
        (
            datetime(2026, 10, 20, 1, 8, 59, tzinfo=timezone.utc),
            "readback_captured_in_future",
        ),
    ],
)
def test_plan_refuses_a_stale_or_future_readback(
    now: datetime, reason: str, tmp_path: Path
) -> None:
    document = plan(World(), tmp_path, now=now)
    assert document["status"] == "blocked"
    assert reason in document["blocks"]


@pytest.mark.parametrize(
    "now",
    [
        datetime(2026, 10, 20, 1, 20, 0, tzinfo=timezone.utc),
        datetime(2026, 10, 20, 1, 9, 0, tzinfo=timezone.utc),
    ],
)
def test_plan_accepts_the_freshness_boundaries(now: datetime, tmp_path: Path) -> None:
    assert plan(World(), tmp_path, now=now)["status"] == "ready"


@pytest.mark.parametrize("value", ["2026-10-20T01:11:00", "soon", ""])
def test_now_must_be_an_aware_timestamp(
    value: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(["plan", *_args(World(), tmp_path), "--now", value], capsys)
    assert code == 2 and out == ""
    assert "blocked: usage" in err


def test_plan_without_now_uses_the_system_clock(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The fixture was captured in the past: by the real clock it is stale.
    code, out, _err = run(["plan", *_args(World(), tmp_path)], capsys)
    assert code == 2
    assert "readback_stale" in json.loads(out)["blocks"]


def test_plan_exits_2_and_prints_the_blocked_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    world.put(
        "ecs",
        "ListTasks",
        {"cluster": "neurostack-staging", "startedBy": hook.STARTED_BY},
        {"taskArns": [TASK_ARN]},
    )
    code, out, _err = run(["plan", *_args(world, tmp_path), *NOW_ARGS], capsys)
    assert code == 2
    document = json.loads(out)
    assert document["status"] == "blocked"
    assert document["running_registration_tasks"] == [TASK_ARN]
    code, out, _err = run(["plan", *_args(World(), tmp_path), *NOW_ARGS], capsys)
    assert code == 0
    assert json.loads(out)["running_registration_tasks"] == []


def _with_containers(*extra: dict[str, Any]) -> dict[str, Any]:
    definition = _task_definition()
    definition["taskDefinition"]["containerDefinitions"].extend(extra)
    return definition


@pytest.mark.parametrize(
    "response,reason",
    [
        (
            _with_containers(
                {
                    "name": "sidecar",
                    "image": BRIDGE_IMAGE,
                    "essential": True,
                    "entryPoint": ["sh"],
                }
            ),
            "task_definition_containers_not_app_and_otel",
        ),
        (
            _task_definition(networkMode="bridge"),
            "task_definition_not_fargate_awsvpc",
        ),
        (
            _task_definition(requiresCompatibilities=["EC2"]),
            "task_definition_not_fargate_awsvpc",
        ),
        (
            _task_definition(
                _app_container(
                    environment=[
                        row
                        for row in _app_container()["environment"]
                        if row["name"] != "ENVIRONMENT"
                    ]
                )
            ),
            "task_definition_missing_webhook_wiring",
        ),
    ],
)
def test_plan_requires_exactly_app_and_otel_on_awsvpc_with_the_wiring(
    response: dict[str, Any], reason: str, tmp_path: Path
) -> None:
    world = World()
    world.put(
        "ecs", "DescribeTaskDefinition", {"taskDefinition": EXPECTED_ARN}, response
    )
    document = plan(world, tmp_path)
    assert document["status"] == "blocked"
    assert reason in document["blocks"]


def _put_service(world: World, response: dict[str, Any]) -> None:
    world.put(
        "ecs",
        "DescribeServices",
        {"cluster": "neurostack-staging", "services": ["neurostack-staging-app"]},
        response,
    )


def _zero_desired() -> dict[str, Any]:
    response = _service(desiredCount=0, runningCount=0)
    response["services"][0]["deployments"][0].update(desiredCount=0, runningCount=0)
    return response


@pytest.mark.parametrize(
    "response,reason",
    [
        (_service_with_deployment(pendingCount=1), "service_not_steady"),
        (_service_with_deployment(desiredCount=3), "service_not_steady"),
        (_zero_desired(), "service_not_steady"),
        (
            _service(
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": [SUBNETS[0], SUBNETS[0]],
                        "securityGroups": SECURITY_GROUPS,
                        "assignPublicIp": "DISABLED",
                    }
                }
            ),
            "network_subnets_missing",
        ),
        (
            _service(
                networkConfiguration={
                    "awsvpcConfiguration": {
                        "subnets": SUBNETS,
                        "securityGroups": SECURITY_GROUPS * 2,
                        "assignPublicIp": "DISABLED",
                    }
                }
            ),
            "network_security_groups_missing",
        ),
        (
            _service(clusterArn=rollout.CLUSTER_ARN.replace("staging", "other")),
            "service_identity_mismatch",
        ),
    ],
)
def test_plan_survivor_states_are_refused(
    response: dict[str, Any], reason: str, tmp_path: Path
) -> None:
    world = World()
    _put_service(world, response)
    document = plan(world, tmp_path)
    assert document["status"] == "blocked"
    assert reason in document["blocks"], document["blocks"]


def test_capacity_provider_must_be_only_fargate(tmp_path: Path) -> None:
    world = World()
    service = _service(
        capacityProviderStrategy=[
            {"capacityProvider": "FARGATE"},
            {"capacityProvider": "FARGATE_SPOT"},
        ]
    )
    del service["services"][0]["launchType"]
    _put_service(world, service)
    assert "service_launch_type_not_fargate" in plan(world, tmp_path)["blocks"]


@pytest.mark.parametrize(
    "response",
    [
        {"__error_code__": "Throttling", **_image_details()},
        {"imageDetails": _image_details()["imageDetails"] * 2},
    ],
)
def test_ecr_readback_must_be_one_clean_match(
    response: dict[str, Any], tmp_path: Path
) -> None:
    world = World()
    world.put(
        "ecr",
        "DescribeImages",
        {
            "repositoryName": "neurostack-staging",
            "imageIds": [{"imageDigest": BRIDGE_DIGEST}],
        },
        response,
    )
    assert "app_image_not_found_in_ecr" in plan(world, tmp_path)["blocks"]


def test_run_task_invariant_refuses_an_extra_override_key(tmp_path: Path) -> None:
    request = render(World(), tmp_path)
    request["overrides"]["taskRoleArn"] = "arn:aws:iam::" + ACCOUNT + ":role/admin"
    with pytest.raises(hook.HookError, match="run_task_request_invariant"):
        hook.assert_run_task_invariants(request, EXPECTED_ARN)
    request = render(World(), tmp_path)
    request["overrides"]["containerOverrides"][0]["environment"] = []
    with pytest.raises(hook.HookError, match="run_task_request_invariant"):
        hook.assert_run_task_invariants(request, EXPECTED_ARN)


@pytest.mark.parametrize(
    "status",
    [
        "PROVISIONING",
        "PENDING",
        "ACTIVATING",
        "RUNNING",
        "DEACTIVATING",
        "STOPPING",
        "DEPROVISIONING",
    ],
)
def test_every_status_but_stopped_is_in_progress(status: str, tmp_path: Path) -> None:
    world = World()
    world.record_run(tmp_path)
    task = _described_task(exit_code=None, lastStatus=status)
    del task["tasks"][0]["stoppedAt"]
    world.finish(task=task)
    world.captured_at = "2026-10-20T01:05:00Z"
    result = verify(world, tmp_path)
    assert result["outcome"] == "in_progress"
    assert result["reasons"] == ["task_not_stopped"]


def test_described_task_failures_are_evidence_mismatch(tmp_path: Path) -> None:
    world = World()
    world.record_run(tmp_path)
    described = _described_task()
    described["failures"] = [{"arn": TASK_ARN, "reason": "MISSING"}]
    world.finish(*ok_lines(), task=described)
    result = verify(world, tmp_path)
    assert result["reasons"] == ["describe_tasks_invalid"]


def test_run_task_response_for_another_task_definition_is_evidence_mismatch(
    tmp_path: Path,
) -> None:
    world = World()
    world.record_run(tmp_path, response=_run_task_response(taskDefinitionArn=OLD_ARN))
    world.finish(*ok_lines())
    result = verify(world, tmp_path)
    assert "run_task_response_invalid" in result["reasons"]


def test_run_task_response_task_in_another_cluster_is_a_failed_run(
    tmp_path: Path,
) -> None:
    world = World()
    world.record_run(
        tmp_path,
        response=_run_task_response(
            taskArn=TASK_ARN.replace("/neurostack-staging/", "/other/")
        ),
    )
    result = verify(world, tmp_path)
    assert result["reasons"] == ["run_task_failed"]


def test_the_log_grace_never_hides_another_reason(tmp_path: Path) -> None:
    world = World()
    params = render(world, tmp_path)
    params["clientToken"] = "asaas-webhook-" + "0" * 40
    world.record_run(tmp_path, params=params)
    world.finish()
    world.captured_at = "2026-10-20T01:03:30Z"
    result = verify(world, tmp_path)
    assert result["outcome"] == PENDING
    # The grace never short-cuts the judgement: every reason is reported.
    assert result["reasons"] == [
        "run_task_request_not_rendered_by_hook",
        "result_line_missing",
    ]
    assert result["next_requests"] == []


def test_a_stopped_task_without_stopped_at_gets_no_grace(tmp_path: Path) -> None:
    world = World()
    world.record_run(tmp_path)
    task = _described_task()
    del task["tasks"][0]["stoppedAt"]
    world.finish(task=task)
    world.captured_at = "2026-10-20T01:03:30Z"
    result = verify(world, tmp_path)
    assert result["outcome"] == PENDING
    assert "result_line_missing" in result["reasons"]


def test_an_alert_never_carries_next_requests(tmp_path: Path) -> None:
    world = World()
    params = render(world, tmp_path)
    params["clientToken"] = "asaas-webhook-" + "0" * 40
    world.record_run(tmp_path, params=params)
    running = _described_task(exit_code=None, lastStatus="RUNNING")
    del running["tasks"][0]["stoppedAt"]
    world.finish(task=running)
    world.captured_at = "2026-10-20T01:05:00Z"
    result = verify(world, tmp_path)
    assert result["outcome"] == PENDING
    assert result["next_requests"] == []


def test_infrastructure_health_is_a_known_stop_code(tmp_path: Path) -> None:
    world = World()
    world.record_run(tmp_path)
    world.finish(task=_described_task(exit_code=None, stopCode="InfrastructureHealth"))
    result = verify(world, tmp_path)
    assert result["stop_code"] == "InfrastructureHealth"
    assert "task_stopped_without_app_exit_code" in result["reasons"]


# ---------------------------------------------------------------------------
# Fix loop 1: log pages
# ---------------------------------------------------------------------------


def test_lines_are_read_across_every_page(tmp_path: Path) -> None:
    doc = result_doc()
    world = World()
    world.record_run(tmp_path)
    world.finish()
    world.put_pages([["starting"], [result_line(doc)], [twin_line(doc)]])
    result = verify(world, tmp_path)
    assert result["outcome"] == "registered"
    assert result["log_lines"] == {"result": 1, "log_twin": 1, "foreign": 1, "pages": 4}


def test_an_unread_next_page_is_in_progress_with_its_request(tmp_path: Path) -> None:
    doc = result_doc()
    world = World()
    world.record_run(tmp_path)
    world.finish()
    world.put_pages([[result_line(doc)], [twin_line(doc)]], terminal=False)
    result = verify(world, tmp_path)
    assert result["outcome"] == "in_progress"
    assert result["reasons"] == ["log_events_incomplete"]
    assert result["next_requests"] == [
        {"service": "logs", "operation": "GetLogEvents", "params": log_request("f/2")}
    ]


@pytest.mark.parametrize(
    "pages,reason",
    [
        (
            [[result_line(result_doc())], [result_line(result_doc())]],
            "result_line_duplicate",
        ),
        (
            [[result_line(result_doc())], ["key=" + LEAKED_KEY]],
            "log_forbidden_material",
        ),
    ],
)
def test_checks_run_over_all_pages(
    pages: list[list[str]], reason: str, tmp_path: Path
) -> None:
    world = World()
    world.record_run(tmp_path)
    world.finish()
    world.put_pages(pages)
    result = verify(world, tmp_path)
    assert result["outcome"] == PENDING
    assert reason in result["reasons"]


def test_a_leak_on_an_early_page_alerts_even_before_the_chain_ends(
    tmp_path: Path,
) -> None:
    world = World()
    world.record_run(tmp_path)
    world.finish()
    world.put_pages([["key=" + LEAKED_KEY], ["more"]], terminal=False)
    result = verify(world, tmp_path)
    assert result["outcome"] == PENDING
    assert "log_forbidden_material" in result["reasons"]


def test_an_empty_first_page_is_in_progress_until_the_grace_elapses(
    tmp_path: Path,
) -> None:
    world = World()
    world.record_run(tmp_path)
    world.finish()
    world.put_pages([[]])
    world.captured_at = "2026-10-20T01:04:00Z"
    result = verify(world, tmp_path)
    assert result["outcome"] == "in_progress"
    assert result["reasons"] == ["log_events_not_delivered"]
    assert result["next_requests"][0]["params"] == log_request()
    world.captured_at = "2026-10-20T01:05:01Z"
    result = verify(world, tmp_path)
    assert result["outcome"] == PENDING
    assert "result_line_missing" in result["reasons"]


def test_a_malformed_page_token_is_invalid(tmp_path: Path) -> None:
    world = World()
    world.record_run(tmp_path)
    world.finish(logs=_events(*ok_lines(), token="bad token with spaces"))
    assert "log_events_invalid" in verify(world, tmp_path)["reasons"]


# ---------------------------------------------------------------------------
# Fix loop 1: crash recovery (RunTask sent, not recorded)
# ---------------------------------------------------------------------------


def _unrecorded_world() -> World:
    world = World()
    world.finish(*ok_lines())
    return world


def test_verify_reads_for_a_named_task_without_a_recorded_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(
        [
            "render-verify-reads",
            *_args(_unrecorded_world(), tmp_path),
            "--task-arn",
            TASK_ARN,
        ],
        capsys,
    )
    assert code == 0, err
    document = json.loads(out)
    assert document["task_arn"] == TASK_ARN
    assert document["from_recorded_run"] is False
    assert document["requests"][1]["params"] == log_request()


def test_verify_result_for_a_named_task_without_a_recorded_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(
        [
            "verify-result",
            *_args(_unrecorded_world(), tmp_path),
            "--task-arn",
            TASK_ARN,
        ],
        capsys,
    )
    assert code == 0, err
    result = json.loads(out)
    assert result["outcome"] == "registered"
    assert result["recovered_without_run_record"] is True
    assert result["attempt"] is None
    assert result["force_rewrite_token"] is False


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"startedBy": "ecs-svc/1"}, "task_identity_mismatch"),
        (
            {"overrides": {"containerOverrides": [{"name": "app", "command": ["sh"]}]}},
            "task_command_override_mismatch",
        ),
    ],
)
def test_a_recovered_task_must_still_be_a_hook_registration_task(
    changes: dict[str, Any], reason: str, tmp_path: Path
) -> None:
    world = World()
    world.finish(*ok_lines(), task=_described_task(**changes))
    result = verify(world, tmp_path, task_arn=TASK_ARN)
    assert result["outcome"] == PENDING
    assert reason in result["reasons"]


def test_task_arn_must_match_a_recorded_run_and_be_well_formed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = completed(tmp_path, *ok_lines())
    other = TASK_ARN.replace("0f1e", "ffff")
    for command in ("render-verify-reads", "verify-result"):
        code, out, err = run(
            [command, *_args(world, tmp_path), "--task-arn", other], capsys
        )
        assert code == 2 and out == ""
        assert "blocked: task_arn_does_not_match_recorded_run" in err
        code, out, err = run(
            [command, *_args(world, tmp_path), "--task-arn", "arn:aws:ecs:x"], capsys
        )
        assert code == 2 and "blocked: task_arn_invalid" in err
    code, out, err = run(
        ["verify-result", *_args(world, tmp_path), "--task-arn", TASK_ARN], capsys
    )
    assert code == 0
    assert json.loads(out)["recovered_without_run_record"] is False


def test_recovery_flags_are_only_on_the_verify_subcommands(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    for command in ("plan", "render-run-task"):
        code, _out, err = run(
            [command, *_args(World(), tmp_path), *NOW_ARGS, "--task-arn", TASK_ARN],
            capsys,
        )
        assert code == 2 and "blocked: usage" in err
    code, _out, err = run(
        ["verify-result", *_args(World(), tmp_path), *NOW_ARGS], capsys
    )
    assert code == 2 and "blocked: usage" in err


def test_t1_comparator_pins_the_log_levels() -> None:
    source = _synthetic_t1_source()
    assert _t1_contract_mismatches(source) == []
    widened = source.replace(
        '"info" if True else "warning"', '"info" if True else "error"'
    )
    assert _t1_contract_mismatches(widened) == ["log_levels"]
    assert hook.CLI_LOG_LEVELS == frozenset({"info", "warning"})


# ---------------------------------------------------------------------------
# Final loop: a recovered task must belong to the current deployment
# ---------------------------------------------------------------------------


def _recovered(created_at: str, **service_changes: Any) -> World:
    world = World()
    service = _service()
    service["services"][0]["deployments"][0].update(service_changes)
    _put_service(world, service)
    world.finish(*ok_lines(), task=_described_task(createdAt=created_at))
    return world


def test_a_twelve_day_old_task_is_not_this_deployments_registration(
    tmp_path: Path,
) -> None:
    world = _recovered("2026-10-08T01:00:00.000000+00:00")
    result = verify(world, tmp_path, task_arn=TASK_ARN)
    assert result["outcome"] == PENDING
    assert result["reasons"] == ["recovered_task_predates_deployment"]
    assert result["alert_kinds"] == ["evidence_mismatch"]


@pytest.mark.parametrize(
    "created_at,outcome",
    [
        (DEPLOYED_AT, "registered"),
        ("2026-10-20T00:49:59.999000+00:00", PENDING),
        ("2026-10-20T00:50:00.001000+00:00", "registered"),
    ],
)
def test_the_deployment_boundary_is_inclusive(
    created_at: str, outcome: str, tmp_path: Path
) -> None:
    result = verify(_recovered(created_at), tmp_path, task_arn=TASK_ARN)
    assert result["outcome"] == outcome, result["reasons"]


@pytest.mark.parametrize(
    "changes",
    [{"taskDefinition": OLD_ARN}, {"createdAt": None}, {"status": "ACTIVE"}],
)
def test_a_recovered_task_needs_a_primary_deployment_on_the_expected_revision(
    changes: dict[str, Any], tmp_path: Path
) -> None:
    world = _recovered(CREATED_AT, **changes)
    result = verify(world, tmp_path, task_arn=TASK_ARN)
    assert result["outcome"] == PENDING
    assert "recovered_task_not_tied_to_deployment" in result["reasons"]


def test_a_recovered_task_of_another_revision_is_refused(tmp_path: Path) -> None:
    world = World()
    world.finish(*ok_lines(), task=_described_task(taskDefinitionArn=OLD_ARN))
    result = verify(world, tmp_path, task_arn=TASK_ARN)
    assert result["outcome"] == PENDING
    assert "task_identity_mismatch" in result["reasons"]


def test_a_recorded_run_is_not_judged_by_task_age(tmp_path: Path) -> None:
    world = completed(tmp_path, *ok_lines())
    assert verify(world, tmp_path)["outcome"] == "registered"


# ---------------------------------------------------------------------------
# Final loop: the end of the log stream must be explicit
# ---------------------------------------------------------------------------


def test_a_page_without_a_forward_token_is_not_the_end(tmp_path: Path) -> None:
    world = World()
    world.record_run(tmp_path)
    page = _events(*ok_lines())
    del page["nextForwardToken"]
    world.finish(logs=page)
    result = verify(world, tmp_path)
    assert result["outcome"] == "in_progress"
    assert result["reasons"] == ["log_pages_incomplete"]
    assert result["next_requests"] == [
        {"service": "logs", "operation": "GetLogEvents", "params": log_request()}
    ]


def test_a_full_page_without_a_token_is_not_the_end(tmp_path: Path) -> None:
    world = World()
    world.record_run(tmp_path)
    page = _events(*(["noise"] * (hook.LOG_EVENTS_LIMIT - 2) + list(ok_lines())))
    del page["nextForwardToken"]
    world.finish(logs=page)
    assert verify(world, tmp_path)["reasons"] == ["log_pages_incomplete"]


@pytest.mark.parametrize(
    "error", ["ThrottlingException", "ResourceNotFoundException", "AccessDenied"]
)
def test_an_error_on_a_later_page_is_never_the_end_of_the_stream(
    error: str, tmp_path: Path
) -> None:
    world = World()
    world.record_run(tmp_path)
    world.finish()
    world.put_pages([list(ok_lines())], terminal=False)
    world.put("logs", "GetLogEvents", log_request("f/1"), {"__error_code__": error})
    result = verify(world, tmp_path)
    assert result["outcome"] == PENDING
    assert "log_events_invalid" in result["reasons"]


def test_the_page_cap_is_twenty() -> None:
    assert hook.MAX_LOG_PAGES == 20
    assert hook.LOG_EVENTS_LIMIT == 500


def test_render_run_task_rechecks_freshness_itself(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = datetime(2026, 10, 20, 1, 20, 1, tzinfo=timezone.utc)
    readback = hook.load_readback(World().write(tmp_path / "stale.json"))
    with pytest.raises(hook.HookError, match="plan_blocked:readback_stale"):
        hook.plan_context(readback, EXPECTED_ARN, now=stale)
    code, out, err = run(
        ["render-run-task", *_args(World(), tmp_path), "--now", "2026-10-20T01:20:01Z"],
        capsys,
    )
    assert code == 2 and out == ""
    assert "readback_stale" in err
    # Without --now (a live run) the system clock decides: this capture is old.
    code, out, err = run(["render-run-task", *_args(World(), tmp_path)], capsys)
    assert code == 2 and out == ""
    assert "readback_stale" in err


# ---------------------------------------------------------------------------
# E7-B0: the outbox worker flag is a reviewed parameter ('false' or 'true')
# ---------------------------------------------------------------------------


def _with_worker_flag(*rows: dict[str, Any]) -> dict[str, Any]:
    environment = [
        row
        for row in _app_container()["environment"]
        if row["name"] != hook.WORKER_FLAG
    ]
    return _definition_with(environment=[*environment, *rows])


@pytest.mark.parametrize("value", ["false", "true"])
def test_plan_accepts_a_deployed_revision_with_either_reviewed_worker_flag(
    value: str, tmp_path: Path
) -> None:
    world = World()
    world.put(
        "ecs",
        "DescribeTaskDefinition",
        {"taskDefinition": EXPECTED_ARN},
        _with_worker_flag({"name": hook.WORKER_FLAG, "value": value}),
    )
    document = plan(world, tmp_path)
    assert document["status"] == "ready", document["blocks"]
    assert "billing_fact_worker_flag_reviewed" in document["checks"]
    assert "task_definition_has_webhook_wiring" in document["checks"]


@pytest.mark.parametrize(
    "rows",
    [
        [{"name": "BILLING_FACT_WORKER_ENABLED", "value": value}]
        for value in ("yes", "True", "TRUE", "1", "on", "", " true", "false\n")
    ]
    + [
        [{"name": "BILLING_FACT_WORKER_ENABLED"}],
        [{"name": "BILLING_FACT_WORKER_ENABLED", "value": ["true"]}],
        [{"name": "BILLING_FACT_WORKER_ENABLED", "value": True}],
        [
            {"name": "BILLING_FACT_WORKER_ENABLED", "value": "false"},
            {"name": "BILLING_FACT_WORKER_ENABLED", "value": "true"},
        ],
        [],
    ],
)
def test_plan_refuses_a_worker_flag_the_planner_never_renders(
    rows: list[dict[str, Any]], tmp_path: Path
) -> None:
    world = World()
    world.put(
        "ecs",
        "DescribeTaskDefinition",
        {"taskDefinition": EXPECTED_ARN},
        _with_worker_flag(*rows),
    )
    document = plan(world, tmp_path)
    assert document["status"] == "blocked"
    assert "billing_fact_worker_flag_unreviewed" in document["blocks"]
    assert "billing_fact_worker_flag_reviewed" not in document["checks"]


def test_the_hook_worker_flag_is_the_planner_parameter() -> None:
    from scripts import asaas_production_wiring as wiring

    assert hook.WORKER_FLAG == wiring.WORKER_FLAG_PARAMETER
    assert hook.WORKER_FLAG_VALUES == wiring.WORKER_FLAG_VALUES == ("false", "true")
    assert hook.WORKER_FLAG in rollout.D3_ENVIRONMENT_NAMES
    assert wiring.ADDED_ENVIRONMENT[hook.WORKER_FLAG] == (
        "parameter",
        wiring.WORKER_FLAG_PARAMETER,
    )
