#!/usr/bin/env python3
"""Pure post-deploy hook for the ASAAS webhook registration (owner decision A2).

Owner decision A2 (2026-10-19): the ASAAS webhook is registered automatically
at deploy. After an approved service deployment reaches ``COMPLETED``, one
one-off ECS task, started from the service's own task definition, runs::

    python -m src.ops.asaas_webhook reconcile

so the ASAAS API key and the webhook token never leave the task.

This tool never calls AWS. Like ``scripts/asaas_production_wiring.py`` it reads
a recorded readback file (the responses the orchestrator collected with its own
tools) and only renders requests and verifies results:

``plan``
    Preconditions: a readback captured in the last 10 minutes; one PRIMARY
    deployment ``COMPLETED`` on the expected task definition,
    ``runningCount == desiredCount``; private awsvpc networking; exactly the
    containers ``app`` and ``otel``; a usable app log configuration; a
    digest-pinned image still in ECR; and no registration task already running
    (single flight, by ``startedBy``).
``render-run-task``
    The exact ``ecs:RunTask`` request, printed only when ``plan`` is ready.
``render-verify-reads``
    The ``ecs:DescribeTasks`` request and the first ``logs:GetLogEvents`` page
    for the task the recorded RunTask started (or, for crash recovery, the task
    named by ``--task-arn``).
``verify-result``
    The ``app`` container's exit code plus its one JSON result line, read over
    every recorded log page. ``registered`` only for exit 0 with verdict
    ``ready``. Every other outcome is the alert ``DEPLOYED_WEBHOOK_PENDING``:
    never a rollback, never a service update, never a new task definition.

The only requests this tool renders are RunTask, DescribeTasks, ListTasks and
GetLogEvents. ``_emit`` is a last-line guard on everything printed: it refuses
output containing a URL scheme, an e-mail address, an ASAAS key prefix, a PEM
header, an AWS access key ID, ``authToken``, the word rollback, or the names of
the ECS service and task-definition mutations (update, create or delete a
service, register or deregister a task definition, stop a task). It does not
detect other free text; the output is built from ARNs, digests, counts and
closed vocabularies only, and the webhook ID appears only as the SHA-256 the
command itself prints.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from scripts import verify_asaas_producer_image_rollout as rollout  # noqa: E402

# ---------------------------------------------------------------------------
# Target: derived from the rollout validator's pins, no new literals
# ---------------------------------------------------------------------------

_CLUSTER_ARN_PARTS = rollout.CLUSTER_ARN.split(":")
REGION = _CLUSTER_ARN_PARTS[3]
ACCOUNT_ID = _CLUSTER_ARN_PARTS[4]
CLUSTER = rollout.CLUSTER_ARN.rsplit("/", 1)[1]
SERVICE = rollout.SERVICE_NAME
TASK_FAMILY = rollout.TASK_FAMILY
APP_CONTAINER = rollout.APP_CONTAINER
OTEL_CONTAINER = "otel"
ECR_REGISTRY = rollout.ECR_REGISTRY
# The D3 outbox worker flag and its only reviewed values (the parameter
# BILLING_FACT_WORKER_ENABLED of scripts/asaas_production_wiring.py, which this
# pure tool must not import; a test pins the two equal). A deployed revision
# may carry either value; any other value was never rendered by the planner.
WORKER_FLAG = "BILLING_FACT_WORKER_ENABLED"
WORKER_FLAG_VALUES = ("false", "true")

# ---------------------------------------------------------------------------
# The registration command's result contract (src/ops/asaas_webhook.py, T1,
# PR #120 tip a13fa43f). Mirrored here because the hook must not import
# application code. tests/fixtures/asaas_deploy_hook/t1_registration_golden.json
# holds lines that T1's main() printed; the tests verify them, and compare this
# mirror with T1's source whenever src/ops/asaas_webhook.py is in the tree.
# ---------------------------------------------------------------------------

REGISTRATION_COMMAND = ("python", "-m", "src.ops.asaas_webhook", "reconcile")
FORCE_REWRITE_TOKEN_FLAG = "--force-rewrite-token"
# reconcile options the hook never passes: re-enabling a webhook an operator
# paused is an owner decision, and an interrupted queue is resumed by default.
CLI_FLAGS_NEVER_PASSED = ("--allow-enable", "--no-resume")
CLI_RESULT_SCHEMA = "neurostack.asaas_webhook_registration.v1"
CLI_LOG_EVENT = "asaas_webhook_registration"
CLI_LOGGER = "src.ops.asaas_webhook"
CLI_LOG_LEVELS = frozenset({"info", "warning"})
CLI_DEADLINE_SECONDS = 120
CLI_EXIT_CODES: Mapping[str, int] = MappingProxyType(
    {
        "ready": 0,
        "absent": 10,
        "drift": 11,
        "interrupted": 12,
        "disabled": 13,
        "ambiguous": 20,
        "unconfigured": 30,
        "error": 40,
    }
)
CLI_COMMANDS = ("check-config", "readback", "reconcile", "smoke")
CLI_ACTIONS = frozenset({"none", "created", "updated"})
CLI_CHECK_NAMES = (
    "settings_loaded",
    "imports_ok",
    "api_key_production_namespace",
    "webhook_token_valid",
    "webhook_url_valid",
    "webhook_host_allowed",
    "webhook_url_allowed",
    "notification_email_valid",
    "environment_deployable",
    "user_agent_valid",
)
CLI_COUNT_NAMES = (
    "webhooks_listed",
    "candidates",
    "name_matches",
    "url_matches",
    "requests",
    "retries",
    "writes",
)
CLI_REASONS = frozenset(
    {
        "usage",
        "import_failed",
        "settings_invalid",
        "api_key_missing",
        "api_key_invalid",
        "webhook_token_missing",
        "webhook_token_invalid",
        "webhook_url_missing",
        "webhook_url_invalid",
        "webhook_host_not_allowed",
        "webhook_url_not_allowed",
        "notification_email_missing",
        "notification_email_invalid",
        "environment_not_deployable",
        "user_agent_invalid",
        "api_client_invalid",
        "absent",
        "multiple_owned",
        "foreign_name_on_url",
        "duplicate_detected",
        "owned_id_changed",
        "listing_inconsistent",
        "name",
        "url",
        "api_version",
        "send_type",
        "disabled",
        "interrupted",
        "events",
        "token_absent",
        "requires_recreate",
        "api_version_unreported",
        "webhook_disabled_by_operator",
        "webhook_reenabled",
        "queue_resumed",
        "queue_not_resumed",
        "create_rejected",
        "create_unconfirmed",
        "update_failed",
        "token_rewritten",
        "auth",
        "transient",
        "deadline_exceeded",
        "invalid_response",
        "not_found",
        "provider_rejected",
        "guard_refused",
        "unexpected",
    }
)
_CLI_SETTINGS_REASON = re.compile(r"^settings_invalid:[a-z][a-z0-9_]{0,63}$")
CLI_MAX_REASONS = 64
CLI_MAX_COUNT = 1_000_000
_CLI_DOCUMENT_KEYS = frozenset(
    {
        "schema",
        "command",
        "verdict",
        "action",
        "reasons",
        "webhook_id_sha256",
        "counts",
        "checks",
        "force_rewrite_token",
        "allow_enable",
        "no_resume",
        "exit_code",
    }
)
_CLI_LOG_KEYS = frozenset(
    {"event", "level", "logger", "timestamp", "duration_ms", "error_class"}
)

# ---------------------------------------------------------------------------
# Run policy
# ---------------------------------------------------------------------------

# Fixed ``startedBy`` marker: the single-flight key (ListTasks filters on it)
# and the identity check on the described task.
STARTED_BY = "neurostack-asaas-webhook-reconcile"
# The exact RunTask top-level key set (checked by assert_run_task_invariants).
# Live evidence (staging, 2026-10-20): a RunTask with exactly cluster,
# taskDefinition, launchType FARGATE, count 1, startedBy, enableExecuteCommand
# false, networkConfiguration.awsvpcConfiguration {subnets, securityGroups,
# assignPublicIp DISABLED} and overrides.containerOverrides [{name, command}]
# was accepted. clientToken, enableECSManagedTags and platformVersion (FARGATE
# only, copied from the service) were accepted live by ECS on 2026-10-22, in
# the first real registration run; on InvalidParameterException the operator
# stops and reports the exact message (runbook). ``propagateTags`` is omitted
# on purpose: the live RunTask API rejects ``NONE`` ("Invalid value for
# propagateTags") although the botocore model lists it; omitting the parameter
# means no propagation.
RUN_TASK_REQUIRED_KEYS = frozenset(
    {
        "cluster",
        "taskDefinition",
        "count",
        "launchType",
        "networkConfiguration",
        "overrides",
        "startedBy",
        "enableExecuteCommand",
        "enableECSManagedTags",
        "clientToken",
    }
)
RUN_TASK_OPTIONAL_KEYS = frozenset({"platformVersion"})
# ECS has no maximum runtime for a RunTask task, the container ``stopTimeout``
# (at most 120 s on Fargate) cannot be overridden in RunTask, and StopTask is
# outside what this hook may emit. The run is bounded by the command's own
# deadline (120 s for all provider I/O, asyncio.wait_for); the watch below only
# decides when the orchestrator stops waiting and raises the alert. It must
# outlast the deadline plus Fargate start (ENI, ~1 GB image pull, Python
# imports: START_BUDGET) and stop (essential container exit, otel SIGTERM and
# its 30 s stop timeout: STOP_BUDGET): 120 + 180 + 60 = 360 s, so 600 s.
START_BUDGET_SECONDS = 180
STOP_BUDGET_SECONDS = 60
WATCH_DEADLINE_SECONDS = 600
POLL_INTERVAL_SECONDS = 15
# CloudWatch Logs delivery after the task stopped; an empty stream younger
# than this is still in progress, older is a missing result line.
LOG_GRACE_SECONDS = 120
# One GetLogEvents page; pages are followed through nextForwardToken.
LOG_EVENTS_LIMIT = 500
MAX_LOG_PAGES = 20
# plan reads describe the world only for a short while: capture them right
# before RunTask (10 minutes old at most, 60 s of clock skew forward).
READBACK_MAX_AGE_SECONDS = 600
READBACK_MAX_FUTURE_SECONDS = 60
MAX_ATTEMPT = 20
FAILURE_POLICY = "alert_only"

OUTCOME_REGISTERED = "registered"
OUTCOME_PENDING = "DEPLOYED_WEBHOOK_PENDING"
OUTCOME_IN_PROGRESS = "in_progress"
EXIT_CODES = MappingProxyType(
    {OUTCOME_REGISTERED: 0, OUTCOME_IN_PROGRESS: 3, OUTCOME_PENDING: 4}
)

READBACK_SCHEMA = "asaas_deploy_hook_readback.v1"
READBACK_MAX_BYTES = 8_000_000
PLAN_SCHEMA = "asaas_deploy_hook_plan.v1"
VERIFY_READS_SCHEMA = "asaas_deploy_hook_verify_reads.v1"
RESULT_SCHEMA = "asaas_deploy_hook_result.v1"

# Requests this hook may ever render (print as a payload to send).
EMITTED_OPERATIONS = frozenset(
    {
        ("ecs", "RunTask"),
        ("ecs", "DescribeTasks"),
        ("ecs", "ListTasks"),
        ("logs", "GetLogEvents"),
    }
)
# Responses a readback file may carry (all reads).
CONSUMED_READS = frozenset(
    {
        ("ecs", "DescribeServices"),
        ("ecs", "DescribeTaskDefinition"),
        ("ecs", "ListTasks"),
        ("ecs", "DescribeTasks"),
        ("ecr", "DescribeImages"),
        ("logs", "GetLogEvents"),
    }
)
FORBIDDEN_OPERATIONS = frozenset(
    {
        "UpdateService",
        "CreateService",
        "DeleteService",
        "RegisterTaskDefinition",
        "DeregisterTaskDefinition",
        "StopTask",
        "ExecuteCommand",
        "UpdateServicePrimaryTaskSet",
        "TagResource",
    }
)
_STOP_CODES = frozenset(
    {
        "TaskFailedToStart",
        "EssentialContainerExited",
        "UserInitiated",
        "ServiceSchedulerInitiated",
        "SpotInterruption",
        "TerminationNotice",
        "InfrastructureHealth",
    }
)

_EXPECTED_ARN = re.compile(
    rf"^arn:aws:ecs:{re.escape(REGION)}:{ACCOUNT_ID}:task-definition/"
    rf"{re.escape(TASK_FAMILY)}:[1-9][0-9]*$"
)
_TASK_ARN = re.compile(
    rf"^arn:aws:ecs:{re.escape(REGION)}:{ACCOUNT_ID}:task/{re.escape(CLUSTER)}/"
    r"(?P<id>[0-9a-f]{32})$"
)
_SUBNET = re.compile(r"^subnet-[0-9a-f]{8,17}$")
_SECURITY_GROUP = re.compile(r"^sg-[0-9a-f]{8,17}$")
_PLATFORM_VERSION = re.compile(r"^(?:LATEST|[0-9]+\.[0-9]+\.[0-9]+)$")
_DEPLOYMENT_ID = re.compile(r"^ecs-svc/[0-9]{1,32}$")
_IMAGE = re.compile(
    rf"^{re.escape(ECR_REGISTRY)}/"
    rf"(?P<repository>{rollout._ECR_REPOSITORY_NAME})"
    r"@(?P<digest>sha256:[0-9a-f]{64})$"
)
_LOG_GROUP = re.compile(r"^[A-Za-z0-9_./#-]{1,512}$")
_LOG_PREFIX = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_LOG_TOKEN = re.compile(r"^[A-Za-z0-9/_+=.-]{1,1024}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_SECRET_RESPONSE_FIELDS = frozenset(
    {"SecretString", "SecretBinary", "SecretValues", "RandomPassword"}
)
_ERROR_KEY = "__error_code__"
# Any of these in a log line means the task printed something it must never
# print (URLs, hosts, e-mail addresses, keys, the token field, webhook paths
# that carry the ID). The line is never echoed; the result is the alert.
_FORBIDDEN_LOG_MATERIAL = re.compile(
    r"://|[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+|[$]aact_|-----BEGIN"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}|aws_secret_access_key|authToken|access_token"
    r"|asaas-access-token|Bearer\s|/v3/webhooks|/v1/billing/webhook"
    r"|asaas\.com|neurostack\.global",
    re.IGNORECASE,
)
# Last-line guard on everything this tool prints (see the module docstring).
_FORBIDDEN_OUTPUT = re.compile(
    r"://|[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+|[$]aact_|-----BEGIN"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}|authToken|rollback|update.?service"
    r"|register.?task.?definition|deregister|stop.?task|delete.?service"
    r"|create.?service",
    re.IGNORECASE,
)


class HookError(ValueError):
    """The readback, the arguments or a rendered request is unsafe or ambiguous."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise HookError(reason)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _read_key(service: str, operation: str, params: dict[str, Any]) -> str:
    return _canonical([service, operation, params])


def _strict_json(text: str | bytes) -> Any:
    def pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in rows:
            if key in output:
                raise HookError("json_duplicate_key")
            output[key] = value
        return output

    def reject_constant(_value: str) -> NoReturn:
        raise HookError("json_nonfinite_number")

    try:
        return json.loads(text, object_pairs_hook=pairs, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise HookError("json_invalid") from None


def _timestamp(value: Any) -> datetime | None:
    """An aware timestamp from an ISO string or epoch seconds; None otherwise."""

    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _error_code(response: Any) -> str | None:
    if isinstance(response, dict) and isinstance(response.get(_ERROR_KEY), str):
        return str(response[_ERROR_KEY])
    return None


def _reject_secret_material(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            _require(key not in _SECRET_RESPONSE_FIELDS, "secret_material_in_readback")
            _reject_secret_material(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_secret_material(nested)


# ---------------------------------------------------------------------------
# Readback file
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Readback:
    """Recorded responses, one per exact request, plus the one recorded RunTask."""

    captured_at: datetime
    reads: Mapping[str, dict[str, Any]]
    run_task: dict[str, Any] | None

    def get(self, service: str, operation: str, params: dict[str, Any]) -> Any:
        key = _read_key(service, operation, params)
        _require(key in self.reads, f"readback_missing:{service}.{operation}")
        return copy.deepcopy(self.reads[key])

    def has(self, service: str, operation: str, params: dict[str, Any]) -> bool:
        return _read_key(service, operation, params) in self.reads


def load_readback(path: Path) -> Readback:
    try:
        data = path.read_bytes()
    except OSError:
        raise HookError("readback_file_unreadable") from None
    _require(len(data) <= READBACK_MAX_BYTES, "readback_file_too_large")
    try:
        raw = _strict_json(data)
    except HookError:
        raise HookError("readback_file_unreadable") from None
    base = {"schema_version", "account_id", "region", "captured_at_utc", "reads"}
    _require(
        isinstance(raw, dict)
        and set(raw) in (base, base | {"run_task"})
        and raw["schema_version"] == READBACK_SCHEMA
        and raw["account_id"] == ACCOUNT_ID
        and raw["region"] == REGION
        and isinstance(raw["reads"], list)
        and isinstance(raw["captured_at_utc"], str)
        and _timestamp(raw["captured_at_utc"]) is not None,
        "readback_file_shape_invalid",
    )
    captured_at = _timestamp(raw["captured_at_utc"])
    assert captured_at is not None
    reads: dict[str, dict[str, Any]] = {}
    for row in raw["reads"]:
        _require(
            isinstance(row, dict)
            and set(row) == {"service", "operation", "params", "response"}
            and isinstance(row["params"], dict)
            and isinstance(row["response"], dict),
            "readback_file_read_invalid",
        )
        _require(
            (row["service"], row["operation"]) in CONSUMED_READS,
            "readback_file_operation_not_allowed",
        )
        _reject_secret_material(row["response"])
        key = _read_key(row["service"], row["operation"], row["params"])
        _require(key not in reads, "readback_file_duplicate_read")
        reads[key] = row["response"]
    run_task = None
    if "run_task" in raw:
        # Present means recorded: null or a partial record is malformed.
        run_task = raw["run_task"]
        _require(
            isinstance(run_task, dict)
            and set(run_task) == {"params", "response"}
            and isinstance(run_task["params"], dict)
            and isinstance(run_task["response"], dict),
            "readback_file_run_task_invalid",
        )
        _reject_secret_material(run_task["response"])
    return Readback(
        captured_at=captured_at,
        reads=MappingProxyType(reads),
        run_task=copy.deepcopy(run_task),
    )


# ---------------------------------------------------------------------------
# Requests (pure builders)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LogTarget:
    group: str
    stream_prefix: str
    # The awslogs stream is <prefix>/<container>/<task id>. The hook reads the
    # ``app`` container; scripts/asaas_migration_window.py reuses the reader
    # for the one-container migrations and read task families.
    container: str = APP_CONTAINER


def validate_expected_arn(value: Any) -> str:
    _require(
        isinstance(value, str) and _EXPECTED_ARN.fullmatch(value) is not None,
        "expected_task_definition_invalid",
    )
    return str(value)


def validate_task_arn(value: Any) -> str:
    _require(
        isinstance(value, str) and _TASK_ARN.fullmatch(value) is not None,
        "task_arn_invalid",
    )
    return str(value)


def registration_command(*, force_rewrite_token: bool) -> list[str]:
    command = list(REGISTRATION_COMMAND)
    if force_rewrite_token:
        command.append(FORCE_REWRITE_TOKEN_FLAG)
    return command


def describe_services_request() -> dict[str, Any]:
    return {"cluster": CLUSTER, "services": [SERVICE]}


def list_registration_tasks_request() -> dict[str, Any]:
    # ECS ListTasks: startedBy must be the only filter. The default desired
    # status (RUNNING) covers PROVISIONING, PENDING and RUNNING tasks.
    return {"cluster": CLUSTER, "startedBy": STARTED_BY}


def describe_task_definition_request(task_definition_arn: str) -> dict[str, Any]:
    return {"taskDefinition": task_definition_arn}


def describe_image_request(repository: str, digest: str) -> dict[str, Any]:
    return {"repositoryName": repository, "imageIds": [{"imageDigest": digest}]}


def describe_tasks_request(task_arn: str) -> dict[str, Any]:
    return {"cluster": CLUSTER, "tasks": [task_arn]}


def log_events_request(
    log: LogTarget, task_id: str, next_token: str | None = None
) -> dict[str, Any]:
    """One page of the task's container stream (``app`` here), oldest first.

    The first page has no ``nextToken``. Each later page sends the previous
    response's ``nextForwardToken``; the stream is read to its end when a
    response returns the token that was sent.
    """

    request: dict[str, Any] = {
        "logGroupName": log.group,
        "logStreamName": f"{log.stream_prefix}/{log.container}/{task_id}",
        "startFromHead": True,
        "limit": LOG_EVENTS_LIMIT,
    }
    if next_token is not None:
        request["nextToken"] = next_token
    return request


def _request_row(
    service: str, operation: str, params: dict[str, Any]
) -> dict[str, Any]:
    _require((service, operation) in EMITTED_OPERATIONS, "operation_not_emittable")
    return {"service": service, "operation": operation, "params": params}


# ---------------------------------------------------------------------------
# plan: preconditions
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PlanContext:
    """What rendering needs, extracted only from a ready plan's reads."""

    task_definition_arn: str
    deployment_id: str
    subnets: tuple[str, ...]
    security_groups: tuple[str, ...]
    platform_version: str | None
    log: LogTarget


class _Checks:
    def __init__(self) -> None:
        self.passed: list[str] = []
        self.blocks: list[str] = []

    def check(self, condition: bool, name: str, reason: str) -> bool:
        if condition:
            self.passed.append(name)
        elif reason not in self.blocks:
            self.blocks.append(reason)
        return condition


def _is_fargate(service: dict[str, Any]) -> bool:
    if "launchType" in service:
        return bool(service.get("launchType") == "FARGATE")
    strategy = service.get("capacityProviderStrategy")
    return (
        isinstance(strategy, list)
        and bool(strategy)
        and all(
            isinstance(row, dict) and row.get("capacityProvider") == "FARGATE"
            for row in strategy
        )
    )


def _service_view(
    response: Any, expected_arn: str, checks: _Checks
) -> tuple[str | None, tuple[str, ...], tuple[str, ...], str | None]:
    services = response.get("services") if isinstance(response, dict) else None
    if not checks.check(
        isinstance(response, dict)
        and response.get("failures") == []
        and isinstance(services, list)
        and len(services) == 1
        and isinstance(services[0], dict),
        "service_readable",
        "service_readback_unavailable",
    ):
        return None, (), (), None
    assert isinstance(services, list)
    service = services[0]
    checks.check(
        service.get("serviceArn") == rollout.SERVICE_ARN
        and service.get("serviceName") == SERVICE
        and service.get("status") == "ACTIVE"
        and service.get("clusterArn", rollout.CLUSTER_ARN) == rollout.CLUSTER_ARN,
        "service_identity",
        "service_identity_mismatch",
    )
    deployments = service.get("deployments")
    deployments = deployments if isinstance(deployments, list) else []
    checks.check(
        len(deployments) <= 1,
        "single_primary_deployment",
        "service_has_concurrent_deployments",
    )
    primary = deployments[0] if len(deployments) == 1 else None
    primary = primary if isinstance(primary, dict) else {}
    checks.check(
        primary.get("status") == "PRIMARY"
        and primary.get("rolloutState") == "COMPLETED",
        "deployment_completed",
        "deployment_not_completed",
    )
    checks.check(
        primary.get("taskDefinition") == expected_arn,
        "deployment_on_expected_task_definition",
        "deployment_task_definition_mismatch",
    )
    checks.check(
        service.get("taskDefinition") == expected_arn,
        "service_on_expected_task_definition",
        "service_task_definition_mismatch",
    )
    desired = service.get("desiredCount")
    steady = (
        type(desired) is int
        and desired > 0
        and service.get("runningCount") == desired
        and service.get("pendingCount", 0) == 0
        and primary.get("runningCount", desired) == desired
        and primary.get("desiredCount", desired) == desired
        and primary.get("pendingCount", 0) == 0
    )
    checks.check(steady, "service_steady", "service_not_steady")
    checks.check(
        _is_fargate(service), "service_fargate", "service_launch_type_not_fargate"
    )
    deployment_id = primary.get("id")
    checks.check(
        isinstance(deployment_id, str)
        and _DEPLOYMENT_ID.fullmatch(deployment_id) is not None,
        "deployment_id",
        "deployment_id_invalid",
    )
    network = service.get("networkConfiguration")
    vpc = network.get("awsvpcConfiguration") if isinstance(network, dict) else None
    vpc = vpc if isinstance(vpc, dict) else {}
    subnets = vpc.get("subnets")
    groups = vpc.get("securityGroups")
    subnets_ok = checks.check(
        isinstance(subnets, list)
        and bool(subnets)
        and len(set(subnets)) == len(subnets)
        and all(isinstance(s, str) and _SUBNET.fullmatch(s) for s in subnets),
        "network_subnets",
        "network_subnets_missing",
    )
    groups_ok = checks.check(
        isinstance(groups, list)
        and bool(groups)
        and len(set(groups)) == len(groups)
        and all(isinstance(g, str) and _SECURITY_GROUP.fullmatch(g) for g in groups),
        "network_security_groups",
        "network_security_groups_missing",
    )
    public_ok = checks.check(
        vpc.get("assignPublicIp") == "DISABLED",
        "service_assign_public_ip_disabled",
        "service_assign_public_ip_not_disabled",
    )
    if subnets_ok and groups_ok and public_ok:
        checks.passed.append("network_private_awsvpc")
    platform = service.get("platformVersion")
    checks.check(
        platform is None
        or (
            isinstance(platform, str)
            and _PLATFORM_VERSION.fullmatch(platform) is not None
        ),
        "service_platform_version",
        "service_platform_version_invalid",
    )
    return (
        deployment_id if isinstance(deployment_id, str) else None,
        tuple(subnets) if subnets_ok and isinstance(subnets, list) else (),
        tuple(groups) if groups_ok and isinstance(groups, list) else (),
        platform if isinstance(platform, str) else None,
    )


def _single_flight(response: Any, checks: _Checks) -> list[str]:
    """Registration task ARNs ListTasks returned (printed to resume a run)."""

    arns = response.get("taskArns") if isinstance(response, dict) else None
    if not checks.check(
        _error_code(response) is None and isinstance(arns, list),
        "registration_task_listing_readable",
        "registration_task_listing_unreadable",
    ):
        return []
    assert isinstance(arns, list)
    checks.check(
        not response.get("nextToken"),
        "registration_task_listing_complete",
        "registration_task_listing_incomplete",
    )
    checks.check(
        arns == [],
        "no_registration_task_running",
        "registration_task_running",
    )
    return [arn for arn in arns if isinstance(arn, str) and _TASK_ARN.fullmatch(arn)]


def _names(container: dict[str, Any], field: str) -> set[str]:
    rows = container.get(field)
    if not isinstance(rows, list):
        return set()
    return {str(row.get("name")) for row in rows if isinstance(row, dict)}


def _task_definition_view(
    response: Any, expected_arn: str, checks: _Checks
) -> tuple[LogTarget | None, tuple[str, str] | None]:
    definition = response.get("taskDefinition") if isinstance(response, dict) else None
    if not checks.check(
        isinstance(definition, dict)
        and definition.get("taskDefinitionArn") == expected_arn
        and definition.get("family") == TASK_FAMILY,
        "task_definition_identity",
        "task_definition_identity_mismatch",
    ):
        return None, None
    assert isinstance(definition, dict)
    checks.check(
        definition.get("status") == "ACTIVE",
        "task_definition_active",
        "task_definition_not_active",
    )
    checks.check(
        definition.get("networkMode") == "awsvpc"
        and "FARGATE" in (definition.get("requiresCompatibilities") or []),
        "task_definition_fargate_awsvpc",
        "task_definition_not_fargate_awsvpc",
    )
    containers = definition.get("containerDefinitions")
    rows = containers if isinstance(containers, list) else []
    names = [row.get("name") for row in rows if isinstance(row, dict)]
    checks.check(
        len(names) == len(rows)
        and sorted(map(str, names)) == [APP_CONTAINER, OTEL_CONTAINER],
        "task_definition_app_and_otel_only",
        "task_definition_containers_not_app_and_otel",
    )
    apps = [
        row
        for row in rows
        if isinstance(row, dict) and row.get("name") == APP_CONTAINER
    ]
    if not checks.check(
        len(apps) == 1,
        "task_definition_app_container",
        "task_definition_app_container_missing",
    ):
        return None, None
    app = apps[0]
    checks.check(
        app.get("essential", True) is True,
        "app_container_essential",
        "app_container_not_essential",
    )
    # The override replaces the image CMD; an entry point would reinterpret it.
    checks.check(
        not app.get("entryPoint"),
        "app_has_no_entry_point",
        "app_entry_point_unexpected",
    )
    checks.check(
        rollout.D3_ENVIRONMENT_NAMES | {"ENVIRONMENT"} <= _names(app, "environment")
        and rollout.D3_SECRET_NAMES <= _names(app, "secrets"),
        "task_definition_has_webhook_wiring",
        "task_definition_missing_webhook_wiring",
    )
    environment = app.get("environment")
    flag_values = [
        row.get("value")
        for row in (environment if isinstance(environment, list) else [])
        if isinstance(row, dict) and row.get("name") == WORKER_FLAG
    ]
    checks.check(
        len(flag_values) == 1 and flag_values[0] in WORKER_FLAG_VALUES,
        "billing_fact_worker_flag_reviewed",
        "billing_fact_worker_flag_unreviewed",
    )
    log_configuration = app.get("logConfiguration")
    options = (
        log_configuration.get("options")
        if isinstance(log_configuration, dict)
        else None
    )
    options = options if isinstance(options, dict) else {}
    group = options.get("awslogs-group")
    prefix = options.get("awslogs-stream-prefix")
    log: LogTarget | None = None
    if checks.check(
        isinstance(log_configuration, dict)
        and log_configuration.get("logDriver") == "awslogs"
        and options.get("awslogs-region") == REGION
        and isinstance(group, str)
        and _LOG_GROUP.fullmatch(group) is not None
        and isinstance(prefix, str)
        and _LOG_PREFIX.fullmatch(prefix) is not None,
        "app_log_configuration",
        "app_log_configuration_unusable",
    ):
        assert isinstance(group, str) and isinstance(prefix, str)
        log = LogTarget(group=group, stream_prefix=prefix)
    image = app.get("image")
    match = _IMAGE.fullmatch(image) if isinstance(image, str) else None
    image_ref = None
    if checks.check(
        match is not None, "app_image_digest_pinned", "app_image_not_digest_pinned"
    ):
        assert match is not None
        image_ref = (match.group("repository"), match.group("digest"))
    return log, image_ref


def _image_present(response: Any, digest: str, checks: _Checks) -> None:
    rows = response.get("imageDetails") if isinstance(response, dict) else None
    checks.check(
        _error_code(response) is None
        and isinstance(rows, list)
        and len(rows) == 1
        and isinstance(rows[0], dict)
        and rows[0].get("imageDigest") == digest,
        "app_image_in_ecr",
        "app_image_not_found_in_ecr",
    )


def evaluate_plan(
    readback: Readback, expected_arn: str, *, now: datetime
) -> tuple[dict[str, Any], PlanContext | None]:
    """Pure precondition check; the context is returned only when ready."""

    expected_arn = validate_expected_arn(expected_arn)
    _require(now.tzinfo is not None, "now_invalid")
    checks = _Checks()
    age = (now - readback.captured_at).total_seconds()
    checks.check(age <= READBACK_MAX_AGE_SECONDS, "readback_fresh", "readback_stale")
    checks.check(
        age >= -READBACK_MAX_FUTURE_SECONDS,
        "readback_not_in_future",
        "readback_captured_in_future",
    )
    checks.check(
        readback.run_task is None, "no_run_task_recorded", "run_task_already_recorded"
    )
    deployment_id, subnets, groups, platform = _service_view(
        readback.get("ecs", "DescribeServices", describe_services_request()),
        expected_arn,
        checks,
    )
    running = _single_flight(
        readback.get("ecs", "ListTasks", list_registration_tasks_request()), checks
    )
    log, image = _task_definition_view(
        readback.get(
            "ecs",
            "DescribeTaskDefinition",
            describe_task_definition_request(expected_arn),
        ),
        expected_arn,
        checks,
    )
    if image is not None:
        _image_present(
            readback.get("ecr", "DescribeImages", describe_image_request(*image)),
            image[1],
            checks,
        )
    ready = not checks.blocks
    context = None
    if ready:
        assert deployment_id is not None and log is not None
        context = PlanContext(
            task_definition_arn=expected_arn,
            deployment_id=deployment_id,
            subnets=subnets,
            security_groups=groups,
            platform_version=platform,
            log=log,
        )
    document: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "status": "ready" if ready else "blocked",
        "expected_task_definition_arn": expected_arn,
        "readback_age_seconds": int(age),
        "checks": checks.passed,
        "blocks": checks.blocks,
        "running_registration_tasks": running,
        "app_image_digest": image[1] if image else None,
        "run_task": {
            "started_by": STARTED_BY,
            "command": registration_command(force_rewrite_token=False),
            "force_rewrite_token_flag": FORCE_REWRITE_TOKEN_FLAG,
            "count": 1,
            "assign_public_ip": "DISABLED",
            "subnet_count": len(subnets),
            "security_group_count": len(groups),
        },
        "watch": watch_policy(),
        "on_failure": FAILURE_POLICY,
    }
    return document, context


def watch_policy() -> dict[str, int]:
    return {
        "cli_deadline_seconds": CLI_DEADLINE_SECONDS,
        "start_budget_seconds": START_BUDGET_SECONDS,
        "stop_budget_seconds": STOP_BUDGET_SECONDS,
        "watch_deadline_seconds": WATCH_DEADLINE_SECONDS,
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "log_grace_seconds": LOG_GRACE_SECONDS,
        "readback_max_age_seconds": READBACK_MAX_AGE_SECONDS,
    }


def plan_context(
    readback: Readback, expected_arn: str, *, now: datetime
) -> PlanContext:
    document, context = evaluate_plan(readback, expected_arn, now=now)
    _require(
        context is not None and document["status"] == "ready",
        "plan_blocked:" + ",".join(document["blocks"]),
    )
    assert context is not None
    return context


# ---------------------------------------------------------------------------
# render-run-task
# ---------------------------------------------------------------------------


def _client_token(context: PlanContext, request: dict[str, Any], attempt: int) -> str:
    """Idempotency token: one RunTask per deployment, command and attempt.

    A retried RunTask call with the same rendered request cannot start a
    second task (while ECS still honours the token); a deliberate re-run
    passes a new ``--attempt``.
    """

    digest = _sha256(
        {"deployment_id": context.deployment_id, "attempt": attempt, "request": request}
    )
    return "asaas-webhook-" + digest[:40]


def build_run_task_request(
    context: PlanContext, *, force_rewrite_token: bool, attempt: int
) -> dict[str, Any]:
    _require(
        type(attempt) is int and 1 <= attempt <= MAX_ATTEMPT, "attempt_out_of_range"
    )
    request: dict[str, Any] = {
        "cluster": CLUSTER,
        "taskDefinition": context.task_definition_arn,
        "count": 1,
        "launchType": "FARGATE",
        "networkConfiguration": {
            "awsvpcConfiguration": {
                "subnets": list(context.subnets),
                "securityGroups": list(context.security_groups),
                "assignPublicIp": "DISABLED",
            }
        },
        "overrides": {
            "containerOverrides": [
                {
                    "name": APP_CONTAINER,
                    "command": registration_command(
                        force_rewrite_token=force_rewrite_token
                    ),
                }
            ]
        },
        "startedBy": STARTED_BY,
        # No ``propagateTags``: RunTask rejects NONE; absent = no propagation.
        "enableECSManagedTags": False,
        "enableExecuteCommand": False,
    }
    if context.platform_version is not None:
        request["platformVersion"] = context.platform_version
    request["clientToken"] = _client_token(context, request, attempt)
    assert_run_task_invariants(request, context.task_definition_arn)
    return request


def assert_run_task_invariants(request: Any, expected_arn: str) -> None:
    """Defence in depth: the rendered RunTask can never widen."""

    _require(
        not (isinstance(request, dict) and "propagateTags" in request),
        "run_task_request_invariant:propagate_tags_rejected_by_run_task_api",
    )
    _require(
        isinstance(request, dict)
        and RUN_TASK_REQUIRED_KEYS
        <= set(request)
        <= RUN_TASK_REQUIRED_KEYS | RUN_TASK_OPTIONAL_KEYS,
        "run_task_request_invariant:key_set",
    )
    network = request.get("networkConfiguration")
    _require(
        isinstance(network, dict)
        and set(network) == {"awsvpcConfiguration"}
        and isinstance(network["awsvpcConfiguration"], dict)
        and set(network["awsvpcConfiguration"])
        == {"subnets", "securityGroups", "assignPublicIp"},
        "run_task_request_invariant:network_key_set",
    )
    vpc = network["awsvpcConfiguration"]
    overrides = request.get("overrides") if isinstance(request, dict) else None
    container_overrides = (
        overrides.get("containerOverrides") if isinstance(overrides, dict) else None
    )
    _require(
        isinstance(request, dict)
        and request.get("cluster") == CLUSTER
        and request.get("taskDefinition") == expected_arn
        and request.get("count") == 1
        and request.get("launchType") == "FARGATE"
        and request.get("startedBy") == STARTED_BY
        and request.get("enableExecuteCommand") is False
        and request.get("enableECSManagedTags") is False
        and isinstance(request.get("clientToken"), str)
        and re.fullmatch(r"asaas-webhook-[0-9a-f]{40}", request["clientToken"])
        is not None
        and vpc.get("assignPublicIp") == "DISABLED"
        and bool(vpc.get("subnets"))
        and bool(vpc.get("securityGroups"))
        and isinstance(container_overrides, list)
        and len(container_overrides) == 1
        and isinstance(container_overrides[0], dict)
        and container_overrides[0]
        == {
            "name": APP_CONTAINER,
            "command": container_overrides[0].get("command"),
        }
        and container_overrides[0].get("command")
        in (
            registration_command(force_rewrite_token=False),
            registration_command(force_rewrite_token=True),
        )
        and isinstance(overrides, dict)
        and set(overrides) == {"containerOverrides"},
        "run_task_request_invariant",
    )


# ---------------------------------------------------------------------------
# verify-result
# ---------------------------------------------------------------------------


def _override_command(holder: Any) -> list[str] | None:
    """The ``app`` command override of a RunTask request or a described task."""

    overrides = holder.get("overrides") if isinstance(holder, dict) else None
    rows = overrides.get("containerOverrides") if isinstance(overrides, dict) else None
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("name") == APP_CONTAINER:
            command = row.get("command")
            return list(command) if isinstance(command, list) else None
    return None


def _force_flag_of(command: list[str] | None) -> bool | None:
    for force in (False, True):
        if command == registration_command(force_rewrite_token=force):
            return force
    return None


def _verify_context(
    readback: Readback, expected_arn: str
) -> tuple[PlanContext | None, LogTarget | None]:
    """The rendering context and log target, rebuilt from the plan reads."""

    checks = _Checks()
    deployment_id, subnets, groups, platform = _service_view(
        readback.get("ecs", "DescribeServices", describe_services_request()),
        expected_arn,
        checks,
    )
    log, _image = _task_definition_view(
        readback.get(
            "ecs",
            "DescribeTaskDefinition",
            describe_task_definition_request(expected_arn),
        ),
        expected_arn,
        checks,
    )
    if deployment_id is None or log is None or not subnets or not groups:
        return None, log
    context = PlanContext(
        task_definition_arn=expected_arn,
        deployment_id=deployment_id,
        subnets=subnets,
        security_groups=groups,
        platform_version=platform,
        log=log,
    )
    return context, log


def _rendered_variant(
    context: PlanContext | None, params: dict[str, Any]
) -> tuple[bool | None, int | None]:
    """Which rendering (force flag, attempt) recorded RunTask params equal."""

    if context is None:
        return None, None
    for force in (False, True):
        for attempt in range(1, MAX_ATTEMPT + 1):
            rendered = build_run_task_request(
                context, force_rewrite_token=force, attempt=attempt
            )
            if rendered == params:
                return force, attempt
    return None, None


def _primary_deployment(readback: Readback) -> dict[str, Any] | None:
    """The service's single PRIMARY deployment, from the required plan read."""

    response = readback.get("ecs", "DescribeServices", describe_services_request())
    services = response.get("services") if isinstance(response, dict) else None
    if not (isinstance(services, list) and len(services) == 1):
        return None
    service = services[0]
    deployments = service.get("deployments") if isinstance(service, dict) else None
    primary = [
        row
        for row in (deployments if isinstance(deployments, list) else [])
        if isinstance(row, dict) and row.get("status") == "PRIMARY"
    ]
    return primary[0] if len(primary) == 1 else None


def _tie_to_deployment(
    verdict: _Verdict,
    task: dict[str, Any],
    deployment: dict[str, Any] | None,
    expected_arn: str,
) -> None:
    """Crash recovery: the task must have been started for this deployment.

    Without a recorded RunTask (whose client token binds the deployment) the
    only proof is time: a registration task created before the current
    PRIMARY deployment (on the expected revision) belongs to an earlier run,
    for example one that wrote an old token.
    """

    deployed_at = _timestamp((deployment or {}).get("createdAt"))
    created = _timestamp(task.get("createdAt"))
    if (
        deployment is None
        or deployment.get("taskDefinition") != expected_arn
        or deployed_at is None
        or created is None
    ):
        verdict.fail("recovered_task_not_tied_to_deployment", "evidence_mismatch")
    elif created < deployed_at:
        verdict.fail("recovered_task_predates_deployment", "evidence_mismatch")


def _started_task_arn(response: Any) -> str | None:
    tasks = response.get("tasks") if isinstance(response, dict) else None
    if not (
        isinstance(response, dict)
        and response.get("failures") == []
        and isinstance(tasks, list)
        and len(tasks) == 1
        and isinstance(tasks[0], dict)
    ):
        return None
    arn = tasks[0].get("taskArn")
    return arn if isinstance(arn, str) and _TASK_ARN.fullmatch(arn) else None


def _task_reads(task_arn: str, log: LogTarget) -> list[dict[str, Any]]:
    match = _TASK_ARN.fullmatch(task_arn)
    assert match is not None
    return [
        _request_row("ecs", "DescribeTasks", describe_tasks_request(task_arn)),
        _request_row(
            "logs", "GetLogEvents", log_events_request(log, match.group("id"))
        ),
    ]


def _task_arn_for(readback: Readback, task_arn: str | None) -> tuple[str | None, bool]:
    """(the task to verify, whether it comes from a recorded run).

    With a recorded RunTask the task is the one it started; ``--task-arn``
    must then name the same task. Without a recorded RunTask (crash recovery)
    ``--task-arn`` is required.
    """

    if task_arn is not None:
        task_arn = validate_task_arn(task_arn)
    if readback.run_task is None:
        _require(task_arn is not None, "run_task_not_recorded")
        return task_arn, False
    started = _started_task_arn(readback.run_task["response"])
    if task_arn is not None:
        _require(started == task_arn, "task_arn_does_not_match_recorded_run")
    return started, True


def render_verify_reads(
    readback: Readback, expected_arn: str, *, task_arn: str | None = None
) -> dict[str, Any]:
    expected_arn = validate_expected_arn(expected_arn)
    target, recorded = _task_arn_for(readback, task_arn)
    _require(target is not None, "run_task_started_no_task")
    assert target is not None
    checks = _Checks()
    log, _image = _task_definition_view(
        readback.get(
            "ecs",
            "DescribeTaskDefinition",
            describe_task_definition_request(expected_arn),
        ),
        expected_arn,
        checks,
    )
    _require(log is not None, "app_log_configuration_unusable")
    assert log is not None
    return {
        "schema": VERIFY_READS_SCHEMA,
        "task_arn": target,
        "from_recorded_run": recorded,
        "requests": _task_reads(target, log),
        "log_pages": (
            "send the first page; while a response's nextForwardToken differs "
            "from the token sent, send the same request with nextToken set to it"
        ),
        "watch": watch_policy(),
    }


class _Lines:
    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []
        self.twins: list[dict[str, Any]] = []
        self.malformed = 0
        self.foreign = 0
        self.forbidden = False


def validate_cli_document(document: Any) -> dict[str, Any]:
    """The command's result document, closed schema; anything else raises."""

    _require(
        isinstance(document, dict) and set(document) == _CLI_DOCUMENT_KEYS,
        "result_line_malformed",
    )
    assert isinstance(document, dict)
    verdict = document["verdict"]
    reasons = document["reasons"]
    counts = document["counts"]
    checks = document["checks"]
    digest = document["webhook_id_sha256"]
    _require(
        document["schema"] == CLI_RESULT_SCHEMA
        and (document["command"] is None or document["command"] in CLI_COMMANDS)
        and isinstance(verdict, str)
        and verdict in CLI_EXIT_CODES
        and document["action"] in CLI_ACTIONS
        and isinstance(reasons, list)
        and len(reasons) <= CLI_MAX_REASONS
        and all(
            isinstance(reason, str)
            and (
                reason in CLI_REASONS
                or _CLI_SETTINGS_REASON.fullmatch(reason) is not None
            )
            for reason in reasons
        )
        and (
            digest is None
            or (isinstance(digest, str) and _SHA256_HEX.fullmatch(digest) is not None)
        )
        and isinstance(counts, dict)
        and set(counts) == set(CLI_COUNT_NAMES)
        and all(
            type(value) is int and 0 <= value <= CLI_MAX_COUNT
            for value in counts.values()
        )
        and isinstance(checks, dict)
        and set(checks) == set(CLI_CHECK_NAMES)
        and all(type(value) is bool for value in checks.values())
        and type(document["force_rewrite_token"]) is bool
        and type(document["allow_enable"]) is bool
        and type(document["no_resume"]) is bool
        and type(document["exit_code"]) is int
        and document["exit_code"] == CLI_EXIT_CODES[verdict],
        "result_line_malformed",
    )
    return {key: copy.deepcopy(document[key]) for key in sorted(_CLI_DOCUMENT_KEYS)}


def _classify_lines(messages: list[str]) -> _Lines:
    lines = _Lines()
    for message in messages:
        if _FORBIDDEN_LOG_MATERIAL.search(message):
            lines.forbidden = True
        if CLI_RESULT_SCHEMA not in message and CLI_LOG_EVENT not in message:
            lines.foreign += 1
            continue
        try:
            document = _strict_json(message)
        except HookError:
            lines.malformed += 1
            continue
        if (
            not isinstance(document, dict)
            or document.get("schema") != CLI_RESULT_SCHEMA
        ):
            lines.malformed += 1
            continue
        if "event" in document:
            core = {
                key: value
                for key, value in document.items()
                if key in _CLI_DOCUMENT_KEYS
            }
            try:
                _require(
                    set(document) == _CLI_DOCUMENT_KEYS | _CLI_LOG_KEYS
                    and document["event"] == CLI_LOG_EVENT
                    and document["logger"] == CLI_LOGGER
                    and document["level"] in CLI_LOG_LEVELS,
                    "result_line_malformed",
                )
                lines.twins.append(validate_cli_document(core))
            except HookError:
                lines.malformed += 1
            continue
        try:
            lines.results.append(validate_cli_document(document))
        except HookError:
            lines.malformed += 1
    return lines


def _page_messages(response: Any) -> list[str] | None:
    """One page's messages, ``[]`` for an absent stream, None for an invalid page."""

    if _error_code(response) == "ResourceNotFoundException":
        return []
    events = response.get("events") if isinstance(response, dict) else None
    if (
        _error_code(response) is not None
        or not isinstance(events, list)
        or len(events) > LOG_EVENTS_LIMIT
    ):
        return None
    messages: list[str] = []
    for event in events:
        if not (isinstance(event, dict) and isinstance(event.get("message"), str)):
            return None
        messages.append(event["message"].rstrip("\r\n"))
    return messages


@dataclass(frozen=True)
class _LogChain:
    messages: list[str]
    complete: bool
    next_request: dict[str, Any] | None
    problem: str | None
    pages: int
    pending: str = "log_events_incomplete"


def read_log_chain(readback: Readback, log: LogTarget, task_id: str) -> _LogChain:
    """Every recorded page of the stream, following ``nextForwardToken``.

    Complete only on the end-of-stream signal AWS uses: a page that returns
    the token it was sent (or a first page saying the stream does not exist).
    A page without ``nextForwardToken`` proves nothing about later events
    (a trimmed recording), so it is ``log_pages_incomplete``; a missing next
    page is ``log_events_incomplete``. ``next_request`` is the read to send.
    """

    messages: list[str] = []
    token: str | None = None
    for pages in range(MAX_LOG_PAGES):
        request = log_events_request(log, task_id, token)
        if not readback.has("logs", "GetLogEvents", request):
            _require(token is not None, "readback_missing:logs.GetLogEvents")
            return _LogChain(messages, False, request, None, pages)
        response = readback.get("logs", "GetLogEvents", request)
        page = _page_messages(response)
        if page is None:
            return _LogChain(messages, False, None, "log_events_invalid", pages + 1)
        messages.extend(page)
        if _error_code(response) == "ResourceNotFoundException":
            if token is None:
                return _LogChain(messages, True, None, None, pages + 1)
            return _LogChain(messages, False, None, "log_events_invalid", pages + 1)
        forward = response.get("nextForwardToken")
        if forward is None:
            return _LogChain(
                messages, False, request, None, pages + 1, "log_pages_incomplete"
            )
        if forward == token:
            return _LogChain(messages, True, None, None, pages + 1)
        if not (isinstance(forward, str) and _LOG_TOKEN.fullmatch(forward)):
            return _LogChain(messages, False, None, "log_events_invalid", pages + 1)
        token = forward
    return _LogChain(messages, False, None, "log_events_truncated", MAX_LOG_PAGES)


def _app_exit_code(task: dict[str, Any]) -> int | None:
    containers = task.get("containers")
    apps = [
        row
        for row in (containers if isinstance(containers, list) else [])
        if isinstance(row, dict) and row.get("name") == APP_CONTAINER
    ]
    if len(apps) != 1:
        return None
    code = apps[0].get("exitCode")
    return code if type(code) is int else None


class _Verdict:
    def __init__(self, expected_arn: str) -> None:
        self.expected_arn = expected_arn
        self.reasons: list[str] = []
        self.alert_kinds: set[str] = set()
        self.in_progress: list[str] = []
        self.task_arn: str | None = None
        self.recovered = False
        self.attempt: int | None = None
        self.force: bool | None = None
        self.exit_code: int | None = None
        self.stop_code: str | None = None
        self.cli_verdict: str | None = None
        self.registration: dict[str, Any] | None = None
        self.log_lines = {"result": 0, "log_twin": 0, "foreign": 0, "pages": 0}
        self.next_requests: list[dict[str, Any]] = []

    def fail(self, reason: str, kind: str = "webhook_pending") -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
        self.alert_kinds.add(kind)

    def document(self) -> dict[str, Any]:
        if self.reasons:
            outcome = OUTCOME_PENDING
            self.next_requests = []
        elif self.in_progress:
            outcome = OUTCOME_IN_PROGRESS
        else:
            outcome = OUTCOME_REGISTERED
        return {
            "schema": RESULT_SCHEMA,
            "outcome": outcome,
            "alert": outcome == OUTCOME_PENDING,
            "alert_kinds": sorted(self.alert_kinds) if self.reasons else [],
            "reasons": self.reasons if self.reasons else self.in_progress,
            "on_failure": FAILURE_POLICY,
            "task_definition_arn": self.expected_arn,
            "task_arn": self.task_arn,
            "recovered_without_run_record": self.recovered,
            "attempt": self.attempt,
            "force_rewrite_token": self.force,
            "container_exit_code": self.exit_code,
            "stop_code": self.stop_code,
            "cli_verdict": self.cli_verdict,
            "registration": self.registration,
            "log_lines": self.log_lines,
            "next_requests": self.next_requests,
        }


def verify_result(
    readback: Readback, expected_arn: str, *, task_arn: str | None = None
) -> dict[str, Any]:
    """Classify a finished registration run. Pure; never decides a rollback.

    ``task_arn`` without a recorded RunTask is crash recovery: the task is
    identified by its ``startedBy``, task definition and command override.
    """

    expected_arn = validate_expected_arn(expected_arn)
    verdict = _Verdict(expected_arn)
    target, recorded = _task_arn_for(readback, task_arn)
    context, log = _verify_context(readback, expected_arn)
    if recorded:
        assert readback.run_task is not None
        params = readback.run_task["params"]
        response = readback.run_task["response"]
        if isinstance(params, dict) and "propagateTags" in params:
            # The live RunTask API rejects propagateTags=NONE; the hook never
            # renders the key, so a recorded one is foreign evidence.
            verdict.fail("run_task_request_propagate_tags_present", "evidence_mismatch")
        verdict.force, verdict.attempt = _rendered_variant(context, params)
        if verdict.attempt is None:
            verdict.fail("run_task_request_not_rendered_by_hook", "evidence_mismatch")
        if target is None:
            verdict.fail("run_task_failed")
            return verdict.document()
        started = response["tasks"][0]
        if (
            started.get("taskDefinitionArn") != expected_arn
            or started.get("startedBy", STARTED_BY) != STARTED_BY
        ):
            verdict.fail("run_task_response_invalid", "evidence_mismatch")
        expected_command = _override_command(params)
        created_fallback = _timestamp(started.get("createdAt"))
    else:
        verdict.recovered = True
        expected_command = None
        created_fallback = None
        deployment = _primary_deployment(readback)
    assert target is not None
    verdict.task_arn = target
    described = readback.get("ecs", "DescribeTasks", describe_tasks_request(target))
    tasks = described.get("tasks") if isinstance(described, dict) else None
    if not (
        isinstance(described, dict)
        and described.get("failures") == []
        and isinstance(tasks, list)
        and len(tasks) == 1
        and isinstance(tasks[0], dict)
    ):
        verdict.fail("describe_tasks_invalid", "evidence_mismatch")
        return verdict.document()
    task = tasks[0]
    if (
        task.get("taskArn") != target
        or task.get("taskDefinitionArn") != expected_arn
        or task.get("startedBy") != STARTED_BY
    ):
        verdict.fail("task_identity_mismatch", "evidence_mismatch")
    observed_command = _override_command(task)
    if not recorded:
        _tie_to_deployment(verdict, task, deployment, expected_arn)
        verdict.force = _force_flag_of(observed_command)
        expected_command = observed_command if verdict.force is not None else None
    if expected_command is None or observed_command != expected_command:
        verdict.fail("task_command_override_mismatch", "evidence_mismatch")

    if task.get("lastStatus") != "STOPPED":
        created = _timestamp(task.get("createdAt")) or created_fallback
        if created is None:
            verdict.fail("task_timestamps_invalid")
        elif (readback.captured_at - created).total_seconds() > WATCH_DEADLINE_SECONDS:
            verdict.fail("task_watch_deadline_exceeded")
        else:
            verdict.in_progress.append("task_not_stopped")
            if log is not None:
                verdict.next_requests = _task_reads(target, log)
        return verdict.document()

    stop_code = task.get("stopCode")
    verdict.stop_code = (
        stop_code if stop_code in _STOP_CODES else ("other" if stop_code else None)
    )
    verdict.exit_code = _app_exit_code(task)
    if log is None:
        verdict.fail("app_log_configuration_unusable")
        return verdict.document()
    match = _TASK_ARN.fullmatch(target)
    assert match is not None
    chain = read_log_chain(readback, log, match.group("id"))
    lines = _classify_lines(chain.messages)
    verdict.log_lines = {
        "result": len(lines.results),
        "log_twin": len(lines.twins),
        "foreign": lines.foreign,
        "pages": chain.pages,
    }
    if lines.forbidden:
        verdict.fail("log_forbidden_material", "secret_exposure_suspected")
    if chain.problem is not None:
        verdict.fail(chain.problem)
    if verdict.exit_code is None:
        verdict.fail("task_stopped_without_app_exit_code")
        return verdict.document()
    if not chain.complete and chain.problem is None:
        if not verdict.reasons:
            verdict.in_progress.append(chain.pending)
            assert chain.next_request is not None
            verdict.next_requests = [
                _request_row("logs", "GetLogEvents", chain.next_request)
            ]
        return verdict.document()
    if not chain.messages and not verdict.reasons:
        stopped = _timestamp(task.get("stoppedAt"))
        if (
            stopped is not None
            and (readback.captured_at - stopped).total_seconds() <= LOG_GRACE_SECONDS
        ):
            verdict.in_progress.append("log_events_not_delivered")
            verdict.next_requests = [
                _request_row(
                    "logs", "GetLogEvents", log_events_request(log, match.group("id"))
                )
            ]
            return verdict.document()
    _judge(verdict, lines)
    return verdict.document()


def _judge(verdict: _Verdict, lines: _Lines) -> None:
    exit_code = verdict.exit_code
    if exit_code not in set(CLI_EXIT_CODES.values()):
        verdict.fail("cli_exit_code_unknown")
    if lines.malformed:
        verdict.fail("result_line_malformed")
    if len(lines.twins) > 1:
        verdict.fail("log_twin_duplicate")
    if not lines.results:
        verdict.fail("result_line_missing")
        return
    if len(lines.results) > 1:
        verdict.fail("result_line_duplicate")
        return
    result = lines.results[0]
    verdict.registration = result
    verdict.cli_verdict = result["verdict"]
    if len(lines.twins) == 1 and lines.twins[0] != result:
        verdict.fail("log_twin_disagrees")
    if result["allow_enable"] or result["no_resume"]:
        # The hook never passes --allow-enable or --no-resume.
        verdict.fail("result_options_mismatch")
    reasons = set(result["reasons"])
    if ("webhook_reenabled" in reasons and not result["allow_enable"]) or (
        "queue_resumed" in reasons and result["no_resume"]
    ):
        verdict.fail("result_reason_contradicts_options")
    if result["command"] != "reconcile":
        verdict.fail("result_command_mismatch")
    if verdict.force is not None and result["force_rewrite_token"] != verdict.force:
        verdict.fail("result_force_flag_mismatch")
    if exit_code != result["exit_code"]:
        verdict.fail("exit_code_verdict_mismatch")
    if result["verdict"] != "ready":
        verdict.fail("cli_verdict_not_ready")
        return
    counts = result["counts"]
    writes = counts["writes"]
    if (
        result["webhook_id_sha256"] is None
        or not all(result["checks"].values())
        or counts["candidates"] < 1
        or writes > 1
        or (result["action"] != "none" and writes != 1)
    ):
        verdict.fail("result_ready_inconsistent")
    if result["force_rewrite_token"] and result["action"] == "none":
        # The new token reaches ASAAS only through a POST or a PUT.
        verdict.fail("force_rewrite_not_applied")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _emit(payload: Any) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True)
    _require(_FORBIDDEN_OUTPUT.search(text) is None, "output_forbidden")
    print(text)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise HookError("usage")


def _attempt(value: str) -> int:
    if not (value.isascii() and value.isdecimal()):
        raise argparse.ArgumentTypeError("attempt")
    number = int(value)
    if not 1 <= number <= MAX_ATTEMPT:
        raise argparse.ArgumentTypeError("attempt")
    return number


def _now(value: str) -> datetime:
    parsed = _timestamp(value) if isinstance(value, str) else None
    if parsed is None:
        raise argparse.ArgumentTypeError("now")
    return parsed


def _parser() -> _Parser:
    parser = _Parser(prog="asaas_deploy_hook.py", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "render-run-task", "render-verify-reads", "verify-result"):
        sub = subparsers.add_parser(name)
        sub.add_argument("--expected-task-definition", required=True)
        sub.add_argument("--readback-file", type=Path, required=True)
        if name in {"plan", "render-run-task"}:
            # Default: the system clock. Tests and replays pass it explicitly.
            sub.add_argument("--now", type=_now, default=None)
        if name == "render-run-task":
            sub.add_argument("--force-rewrite-token", action="store_true")
            sub.add_argument("--attempt", type=_attempt, default=1)
        if name in {"render-verify-reads", "verify-result"}:
            # Crash recovery: the task to verify when no RunTask was recorded.
            sub.add_argument("--task-arn", default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
        expected = validate_expected_arn(args.expected_task_definition)
        readback = load_readback(args.readback_file)
        if args.command in {"plan", "render-run-task"}:
            now = args.now if args.now is not None else datetime.now(timezone.utc)
            if args.command == "plan":
                document, _context = evaluate_plan(readback, expected, now=now)
                _emit(document)
                return 0 if document["status"] == "ready" else 2
            context = plan_context(readback, expected, now=now)
            _emit(
                build_run_task_request(
                    context,
                    force_rewrite_token=args.force_rewrite_token,
                    attempt=args.attempt,
                )
            )
            return 0
        if args.command == "render-verify-reads":
            _emit(render_verify_reads(readback, expected, task_arn=args.task_arn))
            return 0
        result = verify_result(readback, expected, task_arn=args.task_arn)
        _emit(result)
        return EXIT_CODES[result["outcome"]]
    except HookError as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
