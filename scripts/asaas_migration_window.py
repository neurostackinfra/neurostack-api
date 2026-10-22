#!/usr/bin/env python3
"""Pure run tooling for the production migration windows 035, 036 and 037.

Three windows, each run only on its own explicit owner instruction, each with
one entry in ``WINDOWS`` (the single source of truth; the planner
``scripts/asaas_production_wiring.py`` validates the manifest steps and
renders the migrations task definition from the same table):

=======  =========================  ======  =====  ========================
window   baked command              before  after  RDS snapshot
=======  =========================  ======  =====  ========================
``035``  ``alembic upgrade 035``    032     035    ``pre-033-035-<date>``
``036``  ``alembic upgrade 036``    035     036    ``pre-036-<date>``
``037``  ``alembic upgrade 037``    036     037    ``pre-037-<date>``
=======  =========================  ======  =====  ========================

Like ``scripts/asaas_deploy_hook.py`` (whose readback, log-paging and
service building blocks it reuses) this tool never calls AWS. It reads a
recorded readback file (the responses the orchestrator collected with its own
tools), renders requests and verifies results. Every subcommand needs an
explicit ``--window`` (given once); there is no default window, and time is
always the system clock (no ``--now``).

``windows``                   the window table.
``plan-snapshot`` / ``render-snapshot-requests``
                              the deploy state the window needs, cross-checked
                              on the readback, then ``rds:CreateDBSnapshot``
                              (name ``<prefix><UTC date>``, no tags) and the
                              ``DescribeDBSnapshots`` poll.
``plan-pre-read`` / ``render-pre-read`` / ``verify-pre-read``
                              the SELECT-only readback right before the run,
                              as a one-off read task with a command override
                              (a python program carrying the SQL gzip+base64,
                              under the 8192-character override limit).
``plan`` / ``render-run-task``
                              every precondition of the run, then the exact
                              ``ecs:RunTask`` of the window's migrations
                              revision: no command override (the command is
                              baked), no ``propagateTags``.
``render-verify-reads`` / ``verify-result``
                              the stopped migration task: exit code, every log
                              page, the Alembic upgrade lines of exactly this
                              window, SQLSTATE 55P03 / 40P01 / 55000.
``plan-post-read`` / ``render-post-read`` / ``verify-post-read``
                              ``alembic_version`` read back after the run; it
                              must be exactly the window's ``after``.

The only requests it renders are ``ecs:RunTask``, ``DescribeTasks``,
``ListTasks``, ``DescribeServices``, ``DescribeTaskDefinition``,
``logs:GetLogEvents``, ``rds:CreateDBSnapshot``, ``DescribeDBSnapshots`` and
``ecr:DescribeImages``. Stopping is the default: every anomaly is a blocked
plan or the alert ``MIGRATION_WINDOW_STOPPED`` with its reasons, and there is
no option to continue anyway. ``_emit`` refuses output that names a URL, an
e-mail address, a key, a password, a service or task-definition mutation, a
task stop, a delete, a reverting step or an unpinned upgrade.
"""

from __future__ import annotations

import argparse
import base64
import copy
import gzip
import hashlib
import json
import re
import sys
import zlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from scripts import asaas_deploy_hook as hook  # noqa: E402
from scripts import verify_asaas_producer_image_rollout as rollout  # noqa: E402

# ---------------------------------------------------------------------------
# Target: the hook's (derived from the rollout validator's pins)
# ---------------------------------------------------------------------------

REGION = hook.REGION
ACCOUNT_ID = hook.ACCOUNT_ID
CLUSTER = hook.CLUSTER
SERVICE = hook.SERVICE
APP_TASK_FAMILY = hook.TASK_FAMILY
APP_CONTAINER = hook.APP_CONTAINER
ECR_REGISTRY = hook.ECR_REGISTRY
# The one-off migrations family. Revision 3 bakes the unpinned upgrade: it is
# never run and never a valid window revision (only revisions above it are).
MIGRATIONS_FAMILY = "neurostack-staging-migrations"
MIGRATIONS_SOURCE_REVISION = 3
MIGRATIONS_DATABASE_SECRET = "neurostack/staging/database-url-migrations"
# The read family the orchestrator ran a SELECT-only query on (2026-10-20,
# revision 26): the application role (secret database-url), log group
# /ecs/neurostack-staging/migrations, stream consulta/consulta/<task id>.
READ_FAMILY = "neurostack-staging-consulta"
APP_DATABASE_SECRET = "neurostack/staging/database-url"
# The production database (staging discovery of 2026-10-10; not read by this
# tool: CreateDBSnapshot fails on another name, DescribeDBSnapshots must name
# it as the snapshot's source).
RDS_INSTANCE = "neurostack-staging-db"

# ---------------------------------------------------------------------------
# The window table (single source of truth)
# ---------------------------------------------------------------------------

READ_ROLE_APP = "app"
READ_ROLE_MIGRATIONS = "migrations"
RULE_VERSION_ONLY = "version_only"
RULE_ZERO_COUNTS = "zero_counts"
RULE_VERDICT_OK = "verdict_ok"
RULE_RELATIONS_GONE = "relations_gone"
# 035: the service may still run the tagged revision 8 (expand-only window);
# the migrations revision carries the bridge image. 036 and 037: the image
# containing the migration runs on every service task and is the migrations
# revision's image, by digest.
IMAGE_RULE_BRIDGE = "migrations_revision_on_bridge_image"
IMAGE_RULE_DEPLOYED = "service_runs_the_migrations_image"
PHASES = ("register", "snapshot", "pre_read", "run", "verify", "post_read")


@dataclass(frozen=True)
class ReadSpec:
    """One SELECT-only read, run by a read task, never by the migration."""

    phase: str
    sql_file: str | None
    rule: str
    checks: tuple[str, ...]
    role: str
    expected_version: str


@dataclass(frozen=True)
class MigrationWindow:
    """One separately approved production migration window."""

    name: str
    manifest_step: str
    before: str
    after: str
    chain: tuple[str, ...]
    command: tuple[str, ...]
    snapshot_prefix: str
    image_rule: str
    rollback_target_revision: int | None
    started_by: str
    pre_read: ReadSpec
    post_read: ReadSpec
    plan_steps: tuple[tuple[str, str], ...]

    @property
    def upgrades(self) -> tuple[tuple[str, str], ...]:
        return tuple(zip(self.chain, self.chain[1:]))

    @property
    def command_text(self) -> str:
        return " ".join(self.command)

    @property
    def snapshot_pattern(self) -> str:
        return f"{self.snapshot_prefix}<date>"

    @property
    def verification(self) -> str:
        return f"read back alembic_version == {self.after}"

    def read(self, phase: str) -> ReadSpec:
        _require(phase in ("pre", "post"), "read_phase_invalid")
        return self.pre_read if phase == "pre" else self.post_read

    def read_started_by(self, phase: str) -> str:
        return f"neurostack-migration-read-{self.name}-{phase}"


ZERO_READS_035 = (
    "transactions",
    "billing_subscriptions",
    "billing_checkout_sessions",
    "billing_webhook_deliveries",
    "billing_migrations",
    "billing_migration_receipts",
    "billing_migration_absence_reads",
    "roster revision not 0",
    "rollout not disabled",
    "row level security on counted tables",
)
PREFLIGHT_036 = (
    "billing_migrations",
    "billing_migration_receipts",
    "billing_migration_absence_reads",
    "roster revision not 0",
    "subscriptions not asaas",
    "subscriptions roster_revision not 1",
    "checkouts with migration_id",
    "readiness roster_revision not 0",
    "unexpired readiness",
    "row level security on counted tables",
)
READBACK_037 = (
    "revision",
    "billing_migrations rows",
    "billing_migration_receipts rows",
    "billing_migration_absence_reads rows",
    "roster revision",
    "functions to drop exist",
    "catalog dependents",
    "function sources",
    "columns",
    "owners and privileges",
    "comments",
)
GONE_037 = (
    "public.billing_migration_absence_reads",
    "public.billing_migration_receipts",
    "public.billing_migrations",
    "public.billing_roster_control",
)

WINDOW_035 = MigrationWindow(
    name="035",
    manifest_step="migrations_033_035",
    before="032",
    after="035",
    chain=("032", "033", "034", "035"),
    command=("alembic", "upgrade", "035"),
    snapshot_prefix="pre-033-035-",
    image_rule=IMAGE_RULE_BRIDGE,
    rollback_target_revision=None,
    started_by="neurostack-migration-window-035",
    pre_read=ReadSpec(
        phase="pre",
        sql_file="scripts/migration_033_035_zero_reads.sql",
        rule=RULE_ZERO_COUNTS,
        checks=ZERO_READS_035,
        role=READ_ROLE_APP,
        expected_version="032",
    ),
    post_read=ReadSpec(
        phase="post",
        sql_file=None,
        rule=RULE_VERSION_ONLY,
        checks=(),
        role=READ_ROLE_APP,
        expected_version="035",
    ),
    plan_steps=(
        ("register", "S10"),
        ("snapshot", "S11"),
        ("pre_read", "S12"),
        ("run", "S12"),
        ("verify", "S13"),
        ("post_read", "S13"),
    ),
)
WINDOW_036 = MigrationWindow(
    name="036",
    manifest_step="migrations_036",
    before="035",
    after="036",
    chain=("035", "036"),
    command=("alembic", "upgrade", "036"),
    snapshot_prefix="pre-036-",
    image_rule=IMAGE_RULE_DEPLOYED,
    rollback_target_revision=9,
    started_by="neurostack-migration-window-036",
    pre_read=ReadSpec(
        phase="pre",
        sql_file="scripts/migration_036_preflight.sql",
        rule=RULE_ZERO_COUNTS,
        checks=PREFLIGHT_036,
        role=READ_ROLE_APP,
        expected_version="035",
    ),
    post_read=ReadSpec(
        phase="post",
        sql_file=None,
        rule=RULE_VERSION_ONLY,
        checks=(),
        role=READ_ROLE_APP,
        expected_version="036",
    ),
    plan_steps=(
        ("register", "S20"),
        ("snapshot", "S20"),
        ("pre_read", "S21"),
        ("run", "S21"),
        ("verify", "S22"),
        ("post_read", "S22"),
    ),
)
WINDOW_037 = MigrationWindow(
    name="037",
    manifest_step="migrations_037",
    before="036",
    after="037",
    chain=("036", "037"),
    command=("alembic", "upgrade", "037"),
    snapshot_prefix="pre-037-",
    image_rule=IMAGE_RULE_DEPLOYED,
    rollback_target_revision=9,
    started_by="neurostack-migration-window-037",
    # The runbook requires the migration role for this readback (as the
    # application role its privilege row answers for that role): it runs on
    # the window's own migrations revision, with the read program as the
    # command override.
    pre_read=ReadSpec(
        phase="pre",
        sql_file="scripts/migration_037_readback.sql",
        rule=RULE_VERDICT_OK,
        checks=READBACK_037,
        role=READ_ROLE_MIGRATIONS,
        expected_version="036",
    ),
    post_read=ReadSpec(
        phase="post",
        sql_file="scripts/migration_037_post_read.sql",
        rule=RULE_RELATIONS_GONE,
        checks=GONE_037,
        role=READ_ROLE_APP,
        expected_version="037",
    ),
    plan_steps=(
        ("register", "S23"),
        ("snapshot", "S23"),
        ("pre_read", "S24"),
        ("run", "S24"),
        ("verify", "S24"),
        ("post_read", "S24"),
    ),
)
WINDOWS: Mapping[str, MigrationWindow] = MappingProxyType(
    {window.name: window for window in (WINDOW_035, WINDOW_036, WINDOW_037)}
)
# The window the VPS deploy pin (scripts/deploy-vps.sh APPROVED_ALEMBIC_REVISION)
# and docs/configuration.md name: the first production window. It is NOT a
# default of any subcommand here or in the planner: both require --window.
PINNED_WINDOW = "035"

_REVISION = re.compile(r"^[0-9]{3}$")
_SNAPSHOT_PREFIX = re.compile(r"^pre-(?:[0-9]{3}-)?[0-9]{3}-$")


class WindowError(hook.HookError):
    """The window, the readback or a rendered request is unsafe or ambiguous."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise WindowError(reason)


def _assert_window_table() -> None:
    """Import-time guard: a widened or inconsistent table cannot load."""

    rows = list(WINDOWS.values())
    _require(
        [row.name for row in rows] == ["035", "036", "037"]
        and PINNED_WINDOW in WINDOWS,
        "window_table_names",
    )
    names: set[str] = set()
    for index, row in enumerate(rows):
        _require(
            all(_REVISION.fullmatch(rev) for rev in row.chain)
            and len(row.chain) >= 2
            and row.chain[0] == row.before
            and row.chain[-1] == row.after
            and row.name == row.after
            and all(int(newer) == int(older) + 1 for older, newer in row.upgrades),
            f"window_table_chain:{row.name}",
        )
        _require(
            row.command == ("alembic", "upgrade", row.after),
            f"window_table_command:{row.name}",
        )
        span = row.after if len(row.chain) == 2 else f"{row.chain[1]}-{row.after}"
        _require(
            row.snapshot_prefix == f"pre-{span}-"
            and _SNAPSHOT_PREFIX.fullmatch(row.snapshot_prefix) is not None,
            f"window_table_snapshot:{row.name}",
        )
        _require(
            row.pre_read.phase == "pre"
            and row.post_read.phase == "post"
            and row.pre_read.expected_version == row.before
            and row.post_read.expected_version == row.after
            and row.pre_read.rule != RULE_VERSION_ONLY
            and row.pre_read.sql_file is not None,
            f"window_table_reads:{row.name}",
        )
        _require(
            [phase for phase, _label in row.plan_steps] == list(PHASES)
            and all(
                re.fullmatch(r"S[0-9]{1,2}", label) for _p, label in row.plan_steps
            ),
            f"window_table_plan_steps:{row.name}",
        )
        _require(
            (row.image_rule == IMAGE_RULE_BRIDGE) == (index == 0)
            and (row.rollback_target_revision is None) == (index == 0),
            f"window_table_prerequisites:{row.name}",
        )
        if index:
            _require(rows[index - 1].after == row.before, "window_table_not_contiguous")
        markers = {
            row.started_by,
            row.read_started_by("pre"),
            row.read_started_by("post"),
        }
        _require(
            len(markers) == 3
            and not markers & names
            and all(len(marker) <= 64 for marker in markers),
            "window_table_started_by",
        )
        names |= markers


_assert_window_table()


def window_named(value: Any) -> MigrationWindow:
    _require(isinstance(value, str) and value in WINDOWS, "window_unknown")
    return WINDOWS[str(value)]


def window_summary(window: MigrationWindow) -> dict[str, Any]:
    """The table row as printed by ``windows`` and every plan."""

    return {
        "window": window.name,
        "manifest_step": window.manifest_step,
        "command": list(window.command),
        "alembic_version_before": window.before,
        "alembic_version_after": window.after,
        "upgrades": [f"{older} -> {newer}" for older, newer in window.upgrades],
        "snapshot_name_pattern": window.snapshot_pattern,
        "pre_read": _read_summary(window.pre_read),
        "post_read": _read_summary(window.post_read),
        "image_prerequisite": window.image_rule,
        "rollback_target_must_be_revision": window.rollback_target_revision,
        "started_by": window.started_by,
        "plan_steps": dict(window.plan_steps),
    }


def _read_summary(spec: ReadSpec) -> dict[str, Any]:
    return {
        "sql_file": spec.sql_file,
        "rule": spec.rule,
        "checks": list(spec.checks),
        "role": spec.role,
        "expected_alembic_version": spec.expected_version,
    }


# ---------------------------------------------------------------------------
# Run policy
# ---------------------------------------------------------------------------

# Requests this tool may ever render (print as a payload to send).
EMITTED_OPERATIONS = frozenset(
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
# Responses a readback file may carry (all reads).
CONSUMED_READS = frozenset(
    {
        ("ecs", "DescribeServices"),
        ("ecs", "DescribeTaskDefinition"),
        ("ecs", "ListTasks"),
        ("ecs", "DescribeTasks"),
        ("ecr", "DescribeImages"),
        ("rds", "DescribeDBSnapshots"),
        ("logs", "GetLogEvents"),
    }
)
FORBIDDEN_OPERATIONS = hook.FORBIDDEN_OPERATIONS | frozenset(
    {
        "DeleteDBSnapshot",
        "DeleteDBInstance",
        "ModifyDBInstance",
        "RestoreDBInstanceFromDBSnapshot",
        "RebootDBInstance",
    }
)
# RunTask top-level keys. Live evidence (2026-10-20): RunTask accepted
# cluster, taskDefinition, launchType FARGATE, count 1, startedBy,
# enableExecuteCommand false, networkConfiguration.awsvpcConfiguration
# {subnets, securityGroups, assignPublicIp DISABLED} and a container command
# override, and REJECTED propagateTags (any value: omitted means none).
# clientToken, enableECSManagedTags and platformVersion are documented
# RunTask parameters the hook renders too; the runbook stops on any
# InvalidParameterException. The migration run has no overrides at all.
RUN_TASK_KEYS = frozenset(
    {
        "cluster",
        "taskDefinition",
        "count",
        "launchType",
        "networkConfiguration",
        "startedBy",
        "enableExecuteCommand",
        "enableECSManagedTags",
        "clientToken",
    }
)
READ_TASK_KEYS = RUN_TASK_KEYS | {"overrides"}
OPTIONAL_TASK_KEYS = frozenset({"platformVersion"})
# Freshness. The plan reads describe the world only for a short while (the
# hook's 10 minutes, 60 s of clock skew forward); the snapshot is the restore
# point of the window, so it must be recent (60 minutes from its creation);
# the readback is "immediately before the run" (15 minutes from the read
# task's stop) and must be taken after the snapshot started.
READBACK_MAX_AGE_SECONDS = hook.READBACK_MAX_AGE_SECONDS
READBACK_MAX_FUTURE_SECONDS = hook.READBACK_MAX_FUTURE_SECONDS
SNAPSHOT_MAX_AGE_SECONDS = 3600
PRE_READ_MAX_AGE_SECONDS = 900
SNAPSHOT_POLL_INTERVAL_SECONDS = 30
# Watches (the tool cannot render StopTask: past a deadline the orchestrator
# stops waiting, raises the alert and starts nothing else). Migration: Fargate
# start (ENI, image pull, imports) 180 s + Alembic itself (startup, at most
# 5 s per lock statement, the DDL of 033-035) 300 s + stop 60 s = 540 s, so
# 900 s. Read: 180 s + connect 30 s + statement 60 s + 60 s = 330 s, so 600 s.
START_BUDGET_SECONDS = hook.START_BUDGET_SECONDS
STOP_BUDGET_SECONDS = hook.STOP_BUDGET_SECONDS
MIGRATION_RUN_BUDGET_SECONDS = 300
MIGRATION_WATCH_DEADLINE_SECONDS = 900
READ_CONNECT_TIMEOUT_SECONDS = 30
READ_STATEMENT_TIMEOUT_SECONDS = 60
READ_LOCK_TIMEOUT_SECONDS = 3
READ_WATCH_DEADLINE_SECONDS = 600
POLL_INTERVAL_SECONDS = hook.POLL_INTERVAL_SECONDS
LOG_GRACE_SECONDS = hook.LOG_GRACE_SECONDS
MAX_ATTEMPT = hook.MAX_ATTEMPT
# RunTask overrides: "A total of 8192 characters are allowed for overrides.
# This limit includes the JSON formatting characters" (ECS API). The read
# override must fit with a margin for any difference in how AWS counts.
OVERRIDES_MAX_CHARS = 8192
OVERRIDES_BUDGET_CHARS = 7680
READ_PAYLOAD_MAX_BYTES = 1_000_000

OUTCOME_APPLIED = "applied"
OUTCOME_READ_OK = "read_ok"
OUTCOME_VERIFIED = "verified"
OUTCOME_IN_PROGRESS = "in_progress"
OUTCOME_STOPPED = "MIGRATION_WINDOW_STOPPED"
EXIT_CODES = MappingProxyType(
    {
        OUTCOME_APPLIED: 0,
        OUTCOME_READ_OK: 0,
        OUTCOME_VERIFIED: 0,
        OUTCOME_IN_PROGRESS: 3,
        OUTCOME_STOPPED: 4,
    }
)
FAILURE_POLICY = "stop_and_report"

READBACK_SCHEMA = "asaas_migration_window_readback.v1"
READBACK_MAX_BYTES = 8_000_000
RECORD_KEYS = ("pre_read", "migration", "post_read")
PLAN_SCHEMA = "asaas_migration_window_plan.v1"
READ_PLAN_SCHEMA = "asaas_migration_window_read_plan.v1"
SNAPSHOT_SCHEMA = "asaas_migration_window_snapshot.v1"
SNAPSHOT_PLAN_SCHEMA = "asaas_migration_window_snapshot_plan.v1"
VERIFY_READS_SCHEMA = "asaas_migration_window_verify_reads.v1"
RESULT_SCHEMA = "asaas_migration_window_result.v1"
READ_RESULT_SCHEMA = "asaas_migration_window_read_result.v1"
WINDOWS_SCHEMA = "asaas_migration_window_table.v1"
READ_LINE_SCHEMA = "asaas_migration_window_read.v1"

_TASK_ARN = hook._TASK_ARN
_IMAGE = hook._IMAGE
_TAGGED_IMAGE = re.compile(
    rf"^{re.escape(ECR_REGISTRY)}/"
    rf"(?P<repository>{rollout._ECR_REPOSITORY_NAME})"
    r":(?P<tag>[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})$"
)
_REGISTRY_IMAGE = re.compile(
    rf"^{re.escape(ECR_REGISTRY)}/{rollout._ECR_REPOSITORY_NAME}"
    r"(?::[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}|@sha256:[0-9a-f]{64})$"
)
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def _family_arn(family: str) -> re.Pattern[str]:
    return re.compile(
        rf"^arn:aws:ecs:{re.escape(REGION)}:{ACCOUNT_ID}:task-definition/"
        rf"{re.escape(family)}:(?P<revision>[1-9][0-9]*)$"
    )


_MIGRATIONS_ARN = _family_arn(MIGRATIONS_FAMILY)
_READ_ARN = _family_arn(READ_FAMILY)


def _secret_arn(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"^arn:aws:secretsmanager:{re.escape(REGION)}:{ACCOUNT_ID}:secret:"
        rf"{re.escape(name)}-[A-Za-z0-9]{{6}}$"
    )


_DATABASE_SECRETS = MappingProxyType(
    {
        READ_ROLE_APP: _secret_arn(APP_DATABASE_SECRET),
        READ_ROLE_MIGRATIONS: _secret_arn(MIGRATIONS_DATABASE_SECRET),
    }
)
_CLIENT_TOKEN = re.compile(
    r"^migration-(?:window|read)-[0-9]{3}-(?:run|pre|post)-[0-9a-f]{32}$"
)
# Any of these in a task's log line means it printed something it must never
# print: a URL or DSN, user@host, an address or a database host, a password
# or token, a key. The line is never echoed; the result is the alert.
_LOG_EXPOSURE = re.compile(
    r"://|[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+|[$]aact_|-----BEGIN"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}|aws_secret_access_key|authToken|access_token"
    r"|Bearer\s|password|passwd|pwd=|token=|DATABASE_URL"
    r"|amazonaws\.com|\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b"
    r"|neurostack\.global|asaas\.com",
    re.IGNORECASE,
)
# The one URL-bearing line SQLAlchemy prints after every DBAPI error.
_SQLALCHEMY_BACKGROUND = re.compile(
    r"^\(Background on this error at: https://sqlalche\.me/e/[0-9]+/[a-z0-9]+\)$"
)
# Last-line guard on everything this tool prints.
_FORBIDDEN_OUTPUT = re.compile(
    r"://|[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+|[$]aact_|-----BEGIN"
    r"|(?:AKIA|ASIA)[0-9A-Z]{16}|authToken|password|rollback(?!_target_)"
    r"|roll\s+back|update.?service|register.?task.?definition|deregister"
    r"|stop.?task|delete|create.?service|propagateTags|upgrade\s+heads?\b"
    r"|upgrade\s+[+-]",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Readback file
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowReadback:
    """Recorded reads (one response per exact request) and recorded RunTasks."""

    window: str
    captured_at: datetime
    reads: hook.Readback
    records: Mapping[str, dict[str, Any]]


def load_readback(path: Path, window: MigrationWindow) -> WindowReadback:
    try:
        data = path.read_bytes()
    except OSError:
        raise WindowError("readback_file_unreadable") from None
    _require(len(data) <= READBACK_MAX_BYTES, "readback_file_too_large")
    try:
        raw = hook._strict_json(data)
    except hook.HookError:
        raise WindowError("readback_file_unreadable") from None
    base = {
        "schema_version",
        "account_id",
        "region",
        "window",
        "captured_at_utc",
        "reads",
    }
    _require(
        isinstance(raw, dict)
        and base <= set(raw) <= base | set(RECORD_KEYS)
        and raw["schema_version"] == READBACK_SCHEMA
        and raw["account_id"] == ACCOUNT_ID
        and raw["region"] == REGION
        and isinstance(raw["reads"], list)
        and isinstance(raw["captured_at_utc"], str)
        and hook._timestamp(raw["captured_at_utc"]) is not None,
        "readback_file_shape_invalid",
    )
    _require(raw["window"] == window.name, "readback_file_window_mismatch")
    captured_at = hook._timestamp(raw["captured_at_utc"])
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
        hook._reject_secret_material(row["response"])
        key = hook._read_key(row["service"], row["operation"], row["params"])
        _require(key not in reads, "readback_file_duplicate_read")
        reads[key] = row["response"]
    records: dict[str, dict[str, Any]] = {}
    for name in RECORD_KEYS:
        if name not in raw:
            continue
        record = raw[name]
        _require(
            isinstance(record, dict)
            and set(record) == {"params", "response"}
            and isinstance(record["params"], dict)
            and isinstance(record["response"], dict),
            f"readback_file_{name}_invalid",
        )
        hook._reject_secret_material(record["response"])
        records[name] = copy.deepcopy(record)
    return WindowReadback(
        window=window.name,
        captured_at=captured_at,
        reads=hook.Readback(
            captured_at=captured_at, reads=MappingProxyType(reads), run_task=None
        ),
        records=MappingProxyType(records),
    )


def _request_row(
    service: str, operation: str, params: dict[str, Any]
) -> dict[str, Any]:
    _require(
        (service, operation) in EMITTED_OPERATIONS
        and operation not in FORBIDDEN_OPERATIONS,
        "operation_not_emittable",
    )
    return {"service": service, "operation": operation, "params": copy.deepcopy(params)}


class _Reads:
    """Readback access that records a missing read instead of guessing it."""

    def __init__(self, readback: WindowReadback) -> None:
        self.readback = readback
        self.missing: list[dict[str, Any]] = []
        self.misses = 0

    def get(self, service: str, operation: str, params: dict[str, Any]) -> Any:
        if self.readback.reads.has(service, operation, params):
            return self.readback.reads.get(service, operation, params)
        self.misses += 1
        row = _request_row(service, operation, params)
        if row not in self.missing:
            self.missing.append(row)
        return None


# ---------------------------------------------------------------------------
# Requests (pure builders)
# ---------------------------------------------------------------------------


def describe_services_request() -> dict[str, Any]:
    return hook.describe_services_request()


def list_service_tasks_request(desired_status: str | None = None) -> dict[str, Any]:
    request: dict[str, Any] = {"cluster": CLUSTER, "serviceName": SERVICE}
    if desired_status is not None:
        request["desiredStatus"] = desired_status
    return request


def list_family_tasks_request(
    family: str, desired_status: str | None = None
) -> dict[str, Any]:
    # Default desired status RUNNING: provisioning, pending and running tasks;
    # STOPPED: tasks told to stop or stopped (ECS keeps them listed a while).
    request: dict[str, Any] = {"cluster": CLUSTER, "family": family}
    if desired_status is not None:
        request["desiredStatus"] = desired_status
    return request


def list_started_by_request(started_by: str) -> dict[str, Any]:
    # ECS ListTasks: startedBy must be the only filter.
    return {"cluster": CLUSTER, "startedBy": started_by}


def describe_tasks_request(task_arns: Sequence[str]) -> dict[str, Any]:
    return {"cluster": CLUSTER, "tasks": sorted(task_arns)}


def describe_task_definition_request(arn: str) -> dict[str, Any]:
    return {"taskDefinition": arn}


def describe_image_by_digest_request(repository: str, digest: str) -> dict[str, Any]:
    return {"repositoryName": repository, "imageIds": [{"imageDigest": digest}]}


def describe_image_by_tag_request(repository: str, tag: str) -> dict[str, Any]:
    return {"repositoryName": repository, "imageIds": [{"imageTag": tag}]}


def describe_snapshot_request(snapshot_id: str) -> dict[str, Any]:
    return {"DBSnapshotIdentifier": snapshot_id}


def create_snapshot_request(snapshot_id: str) -> dict[str, Any]:
    # No Tags: the owner decision names the snapshot, nothing else.
    return {"DBInstanceIdentifier": RDS_INSTANCE, "DBSnapshotIdentifier": snapshot_id}


def snapshot_identifier(window: MigrationWindow, day: date) -> str:
    return f"{window.snapshot_prefix}{day.isoformat()}"


def validate_snapshot_id(window: MigrationWindow, value: Any) -> tuple[str, date]:
    match = (
        re.fullmatch(
            re.escape(window.snapshot_prefix) + r"([0-9]{4}-[0-9]{2}-[0-9]{2})", value
        )
        if isinstance(value, str)
        else None
    )
    _require(match is not None, "snapshot_id_invalid")
    assert match is not None
    try:
        day = date.fromisoformat(match.group(1))
    except ValueError:
        raise WindowError("snapshot_id_invalid") from None
    return str(value), day


def validate_service_arn(value: Any) -> str:
    try:
        return hook.validate_expected_arn(value)
    except hook.HookError:
        raise WindowError("service_task_definition_invalid") from None


def validate_migrations_arn(value: Any) -> str:
    match = _MIGRATIONS_ARN.fullmatch(value) if isinstance(value, str) else None
    _require(
        match is not None and int(match.group("revision")) > MIGRATIONS_SOURCE_REVISION,
        "migrations_task_definition_invalid",
    )
    return str(value)


def validate_read_arn(value: Any) -> str:
    _require(
        isinstance(value, str) and _READ_ARN.fullmatch(value) is not None,
        "read_task_definition_invalid",
    )
    return str(value)


def validate_task_arn(value: Any) -> str:
    try:
        return hook.validate_task_arn(value)
    except hook.HookError:
        raise WindowError("task_arn_invalid") from None


@dataclass(frozen=True)
class Context:
    """The arguments every readback subcommand takes, validated."""

    window: MigrationWindow
    service_task_definition: str
    migrations_task_definition: str
    read_task_definition: str
    snapshot_id: str
    snapshot_day: date
    # Window 037 only: the dedicated read-only revision of the migrations
    # family (migration role) whose baked command is the readback program.
    migrations_read_task_definition: str | None = None


def needs_read_revision(window: MigrationWindow) -> bool:
    """Whether a read of this window runs as the migration role."""

    return READ_ROLE_MIGRATIONS in (window.pre_read.role, window.post_read.role)


def make_context(
    window: MigrationWindow,
    *,
    service_task_definition: str,
    migrations_task_definition: str,
    read_task_definition: str,
    snapshot_id: str,
    migrations_read_task_definition: str | None = None,
) -> Context:
    snapshot, day = validate_snapshot_id(window, snapshot_id)
    migrate = validate_migrations_arn(migrations_task_definition)
    read_revision = None
    if needs_read_revision(window):
        _require(
            migrations_read_task_definition is not None,
            "migrations_read_task_definition_required",
        )
        read_revision = validate_migrations_arn(migrations_read_task_definition)
        # One revision migrates, the other only reads: never the same one.
        _require(read_revision != migrate, "read_and_migrate_revisions_must_differ")
    else:
        _require(
            migrations_read_task_definition is None,
            "migrations_read_task_definition_not_used",
        )
    return Context(
        window=window,
        service_task_definition=validate_service_arn(service_task_definition),
        migrations_task_definition=migrate,
        read_task_definition=validate_read_arn(read_task_definition),
        snapshot_id=snapshot,
        snapshot_day=day,
        migrations_read_task_definition=read_revision,
    )


# ---------------------------------------------------------------------------
# Views over the recorded reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Network:
    subnets: tuple[str, ...]
    security_groups: tuple[str, ...]
    platform_version: str | None


@dataclass(frozen=True)
class TaskTarget:
    """A one-container task definition a RunTask of this tool starts."""

    task_definition_arn: str
    container: str
    log: hook.LogTarget
    image: str
    # True for window 037's read revision: the readback program is baked, so
    # its RunTask carries no override at all.
    baked: bool = False


def _service(
    reads: _Reads, context: Context, checks: hook._Checks
) -> tuple[Network | None, int | None, str | None]:
    """The service view of the hook (steady, COMPLETED, private awsvpc)."""

    response = reads.get("ecs", "DescribeServices", describe_services_request())
    deployment_id, subnets, groups, platform = hook._service_view(
        response, context.service_task_definition, checks
    )
    services = response.get("services") if isinstance(response, dict) else None
    service = services[0] if isinstance(services, list) and services else {}
    desired = service.get("desiredCount") if isinstance(service, dict) else None
    network = (
        Network(subnets=subnets, security_groups=groups, platform_version=platform)
        if subnets and groups
        else None
    )
    return network, desired if type(desired) is int else None, deployment_id


def _listing(
    reads: _Reads, params: dict[str, Any], checks: hook._Checks, name: str
) -> list[str] | None:
    """Task ARNs of one complete ListTasks page; None when unusable."""

    response = reads.get("ecs", "ListTasks", params)
    arns = response.get("taskArns") if isinstance(response, dict) else None
    if not checks.check(
        hook._error_code(response) is None
        and isinstance(arns, list)
        and all(isinstance(arn, str) and _TASK_ARN.fullmatch(arn) for arn in arns),
        f"{name}_listing_readable",
        f"{name}_listing_unreadable",
    ):
        return None
    assert isinstance(response, dict) and isinstance(arns, list)
    if not checks.check(
        not response.get("nextToken"),
        f"{name}_listing_complete",
        f"{name}_listing_incomplete",
    ):
        return None
    return [str(arn) for arn in arns]


def _single_flight(
    reads: _Reads, checks: hook._Checks, params: dict[str, Any], name: str
) -> list[str]:
    arns = _listing(reads, params, checks, name)
    if arns is not None:
        checks.check(arns == [], f"no_{name}_running", f"{name}_running")
    return arns or []


def _described(
    reads: _Reads, arns: list[str], checks: hook._Checks, name: str
) -> list[dict[str, Any]] | None:
    if not arns:
        return []
    if not checks.check(
        len(arns) <= 100, f"{name}_count_describable", f"{name}_too_many"
    ):
        return None
    response = reads.get("ecs", "DescribeTasks", describe_tasks_request(arns))
    tasks = response.get("tasks") if isinstance(response, dict) else None
    if not checks.check(
        isinstance(response, dict)
        and response.get("failures") == []
        and isinstance(tasks, list)
        and len(tasks) == len(arns)
        and all(isinstance(task, dict) for task in tasks)
        and sorted(str(task.get("taskArn")) for task in tasks) == sorted(arns),
        f"{name}_described",
        f"{name}_describe_invalid",
    ):
        return None
    assert isinstance(tasks, list)
    return [dict(task) for task in tasks]


def _container(task: dict[str, Any], name: str) -> dict[str, Any] | None:
    rows = task.get("containers")
    matches = [
        row
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict) and row.get("name") == name
    ]
    return matches[0] if len(matches) == 1 else None


def _service_tasks(
    reads: _Reads,
    context: Context,
    checks: hook._Checks,
    desired: int | None,
    expected_digest: str | None,
) -> dict[str, Any]:
    """Every task the service runs is on the expected revision and digest."""

    running = _listing(reads, list_service_tasks_request(), checks, "service_tasks")
    info: dict[str, Any] = {"running": len(running) if running is not None else None}
    if running is not None:
        checks.check(
            desired is not None and len(running) == desired,
            "service_task_count_is_desired",
            "service_task_count_mismatch",
        )
        tasks = _described(reads, running, checks, "service_tasks")
        if tasks is not None and running:
            checks.check(
                all(
                    task.get("lastStatus") == "RUNNING"
                    and task.get("desiredStatus") == "RUNNING"
                    and task.get("taskDefinitionArn") == context.service_task_definition
                    for task in tasks
                ),
                "service_tasks_running_on_expected_revision",
                "service_task_not_on_expected_revision",
            )
            digests = [
                (_container(task, APP_CONTAINER) or {}).get("imageDigest")
                for task in tasks
            ]
            info["image_digests"] = sorted({str(d) for d in digests})
            checks.check(
                expected_digest is not None
                and all(digest == expected_digest for digest in digests),
                "service_tasks_all_on_expected_image_digest",
                "service_task_image_digest_mismatch",
            )
    stopping = _listing(
        reads, list_service_tasks_request("STOPPED"), checks, "service_stopped_tasks"
    )
    if stopping is not None:
        tasks = _described(reads, stopping, checks, "service_stopped_tasks")
        if tasks is not None:
            # A task told to stop that still runs is a task of an earlier
            # image the window must not run next to.
            checks.check(
                all(task.get("lastStatus") == "STOPPED" for task in tasks),
                "no_service_task_still_stopping",
                "service_task_still_stopping",
            )
    return info


def _definition(response: Any, arn: str, family: str) -> dict[str, Any] | None:
    definition = response.get("taskDefinition") if isinstance(response, dict) else None
    if not (
        isinstance(definition, dict)
        and definition.get("taskDefinitionArn") == arn
        and definition.get("family") == family
        and definition.get("revision") == int(arn.rsplit(":", 1)[1])
    ):
        return None
    return definition


def _service_image(
    reads: _Reads, context: Context, checks: hook._Checks
) -> tuple[str | None, str | None]:
    """(the app image of the service revision, the digest it runs as)."""

    arn = context.service_task_definition
    definition = _definition(
        reads.get(
            "ecs", "DescribeTaskDefinition", describe_task_definition_request(arn)
        ),
        arn,
        APP_TASK_FAMILY,
    )
    if not checks.check(
        definition is not None,
        "service_task_definition_identity",
        "service_task_definition_identity_mismatch",
    ):
        return None, None
    assert definition is not None
    checks.check(
        definition.get("status") == "ACTIVE",
        "service_task_definition_active",
        "service_task_definition_not_active",
    )
    rows = definition.get("containerDefinitions")
    apps = [
        row
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict) and row.get("name") == APP_CONTAINER
    ]
    if not checks.check(
        len(apps) == 1, "service_app_container", "service_app_container_missing"
    ):
        return None, None
    image = apps[0].get("image")
    pinned = _IMAGE.fullmatch(image) if isinstance(image, str) else None
    if pinned is not None:
        checks.passed.append("service_image_digest_pinned")
        return str(image), pinned.group("digest")
    tagged = _TAGGED_IMAGE.fullmatch(image) if isinstance(image, str) else None
    if not checks.check(
        tagged is not None and context.window.image_rule == IMAGE_RULE_BRIDGE,
        "service_image_tag_resolvable",
        "service_image_not_digest_pinned",
    ):
        return None, None
    assert tagged is not None
    # Window 035 only: the expand-only window may run while the service still
    # runs revision 8, whose image is an immutable-repository tag; its tasks
    # must all run the digest the tag resolves to.
    response = reads.get(
        "ecr",
        "DescribeImages",
        describe_image_by_tag_request(tagged.group("repository"), tagged.group("tag")),
    )
    details = response.get("imageDetails") if isinstance(response, dict) else None
    digest = (
        details[0].get("imageDigest")
        if isinstance(details, list)
        and len(details) == 1
        and isinstance(details[0], dict)
        else None
    )
    if not checks.check(
        hook._error_code(response) is None
        and isinstance(digest, str)
        and _DIGEST.fullmatch(digest) is not None,
        "service_image_tag_resolved_in_ecr",
        "service_image_tag_not_found_in_ecr",
    ):
        return str(image), None
    return str(image), str(digest)


def _one_container_target(
    definition: dict[str, Any], checks: hook._Checks, prefix: str, role: str
) -> TaskTarget | None:
    """Shared shape of the migrations and read families."""

    checks.check(
        definition.get("status") == "ACTIVE",
        f"{prefix}_task_definition_active",
        f"{prefix}_task_definition_not_active",
    )
    checks.check(
        definition.get("networkMode") == "awsvpc"
        and "FARGATE" in (definition.get("requiresCompatibilities") or []),
        f"{prefix}_task_definition_fargate_awsvpc",
        f"{prefix}_task_definition_not_fargate_awsvpc",
    )
    rows = definition.get("containerDefinitions")
    if not checks.check(
        isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict),
        f"{prefix}_task_definition_one_container",
        f"{prefix}_task_definition_container_count_invalid",
    ):
        return None
    assert isinstance(rows, list)
    container = rows[0]
    name = container.get("name")
    checks.check(
        isinstance(name, str)
        and re.fullmatch(r"[A-Za-z0-9_-]{1,255}", name) is not None,
        f"{prefix}_container_named",
        f"{prefix}_container_name_invalid",
    )
    checks.check(
        container.get("essential", True) is True,
        f"{prefix}_container_essential",
        f"{prefix}_container_not_essential",
    )
    checks.check(
        not container.get("entryPoint"),
        f"{prefix}_container_has_no_entry_point",
        f"{prefix}_container_entry_point_unexpected",
    )
    secrets = container.get("secrets")
    database = [
        row
        for row in (secrets if isinstance(secrets, list) else [])
        if isinstance(row, dict) and row.get("name") == "DATABASE_URL"
    ]
    checks.check(
        "DATABASE_URL" not in hook._names(container, "environment")
        and len(database) == 1
        and isinstance(database[0].get("valueFrom"), str)
        and _DATABASE_SECRETS[role].fullmatch(database[0]["valueFrom"]) is not None,
        f"{prefix}_database_secret_is_the_{role}_role",
        f"{prefix}_database_secret_mismatch",
    )
    log_configuration = container.get("logConfiguration")
    options = (
        log_configuration.get("options")
        if isinstance(log_configuration, dict)
        else None
    )
    options = options if isinstance(options, dict) else {}
    group = options.get("awslogs-group")
    stream_prefix = options.get("awslogs-stream-prefix")
    if not checks.check(
        isinstance(log_configuration, dict)
        and log_configuration.get("logDriver") == "awslogs"
        and options.get("awslogs-region") == REGION
        and isinstance(group, str)
        and hook._LOG_GROUP.fullmatch(group) is not None
        and isinstance(stream_prefix, str)
        and hook._LOG_PREFIX.fullmatch(stream_prefix) is not None,
        f"{prefix}_log_configuration",
        f"{prefix}_log_configuration_unusable",
    ):
        return None
    image = container.get("image")
    if not isinstance(name, str) or not isinstance(image, str):
        return None
    assert isinstance(group, str) and isinstance(stream_prefix, str)
    return TaskTarget(
        task_definition_arn=str(definition.get("taskDefinitionArn")),
        container=name,
        log=hook.LogTarget(group=group, stream_prefix=stream_prefix, container=name),
        image=image,
    )


def _migrations_target(
    reads: _Reads, context: Context, checks: hook._Checks
) -> TaskTarget | None:
    """The migrations revision registered for THIS window, command baked."""

    arn = context.migrations_task_definition
    definition = _definition(
        reads.get(
            "ecs", "DescribeTaskDefinition", describe_task_definition_request(arn)
        ),
        arn,
        MIGRATIONS_FAMILY,
    )
    if not checks.check(
        definition is not None,
        "migrations_task_definition_identity",
        "migrations_task_definition_identity_mismatch",
    ):
        return None
    assert definition is not None
    target = _one_container_target(
        definition, checks, "migrations", READ_ROLE_MIGRATIONS
    )
    rows = definition.get("containerDefinitions")
    container = rows[0] if isinstance(rows, list) and len(rows) == 1 else {}
    command = container.get("command") if isinstance(container, dict) else None
    checks.check(
        normalized_read_command(command) is None,
        "migrations_revision_is_not_a_read_revision",
        "migrations_revision_is_a_read_revision",
    )
    checks.check(
        command == list(context.window.command),
        "migrations_command_is_this_window",
        "migrations_command_not_this_window",
    )
    if target is None:
        return None
    if not checks.check(
        _IMAGE.fullmatch(target.image) is not None,
        "migrations_image_digest_pinned",
        "migrations_image_not_digest_pinned",
    ):
        return None
    return target


def _names_migration_tool(container: dict[str, Any]) -> bool:
    """Whether the container's command, entry point or environment mention
    the migration tool (case-insensitive)."""

    return any(
        _MIGRATION_TOOL.search(json.dumps(container.get(field))) is not None
        for field in ("command", "entryPoint", "environment")
    )


def _migrations_read_target(
    reads: _Reads, context: Context, spec: ReadSpec, checks: hook._Checks
) -> TaskTarget | None:
    """Window 037's dedicated read revision (rendered by the planner with
    ``--purpose read``): the migrations family, image, roles and secret, with
    the readback program baked as its command and no trace of the migration
    tool in its command or environment. Never the revision that migrates."""

    arn = context.migrations_read_task_definition
    if not checks.check(
        arn is not None, "read_revision_named", "read_revision_missing"
    ):
        return None
    assert arn is not None
    definition = _definition(
        reads.get(
            "ecs", "DescribeTaskDefinition", describe_task_definition_request(arn)
        ),
        arn,
        MIGRATIONS_FAMILY,
    )
    if not checks.check(
        definition is not None,
        "read_revision_identity",
        "read_revision_identity_mismatch",
    ):
        return None
    assert definition is not None
    target = _one_container_target(
        definition, checks, "read_revision", READ_ROLE_MIGRATIONS
    )
    rows = definition.get("containerDefinitions")
    container = rows[0] if isinstance(rows, list) and len(rows) == 1 else {}
    container = container if isinstance(container, dict) else {}
    command = container.get("command")
    expected = normalized_read_command(
        ["python", "-c", read_program(context.window, spec)]
    )
    command_ok = checks.check(
        normalized_read_command(command) is not None
        and normalized_read_command(command) == expected,
        "read_revision_command_is_the_readback",
        "read_revision_command_not_the_readback",
    )
    tool_free = checks.check(
        not _names_migration_tool(container),
        "read_revision_names_no_migration_tool",
        "read_revision_names_the_migration_tool",
    )
    if target is None or not command_ok or not tool_free:
        return None
    if not checks.check(
        _IMAGE.fullmatch(target.image) is not None,
        "read_revision_image_digest_pinned",
        "read_revision_image_not_digest_pinned",
    ):
        return None
    return replace(target, baked=True)


def _image_in_ecr(reads: _Reads, image: str, checks: hook._Checks) -> None:
    match = _IMAGE.fullmatch(image)
    assert match is not None
    response = reads.get(
        "ecr",
        "DescribeImages",
        describe_image_by_digest_request(
            match.group("repository"), match.group("digest")
        ),
    )
    rows = response.get("imageDetails") if isinstance(response, dict) else None
    checks.check(
        hook._error_code(response) is None
        and isinstance(rows, list)
        and len(rows) == 1
        and isinstance(rows[0], dict)
        and rows[0].get("imageDigest") == match.group("digest"),
        "migrations_image_in_ecr",
        "migrations_image_not_found_in_ecr",
    )


def _read_target(
    reads: _Reads, context: Context, spec: ReadSpec, checks: hook._Checks
) -> TaskTarget | None:
    """Where the read runs: the read family (app role) or window 037's read
    revision (migration role, readback baked)."""

    if spec.role == READ_ROLE_MIGRATIONS:
        read_target = _migrations_read_target(reads, context, spec, checks)
        migrate = _migrations_target(reads, context, checks)
        if read_target is not None and migrate is not None:
            # Same family, image, role and secret as the revision that
            # migrates; only the baked command differs.
            checks.check(
                read_target.image == migrate.image,
                "read_revision_image_is_the_migrations_image",
                "read_revision_image_differs",
            )
        return read_target
    arn = context.read_task_definition
    definition = _definition(
        reads.get(
            "ecs", "DescribeTaskDefinition", describe_task_definition_request(arn)
        ),
        arn,
        READ_FAMILY,
    )
    if not checks.check(
        definition is not None,
        "read_task_definition_identity",
        "read_task_definition_identity_mismatch",
    ):
        return None
    assert definition is not None
    target = _one_container_target(definition, checks, "read", READ_ROLE_APP)
    rows = definition.get("containerDefinitions")
    container = rows[0] if isinstance(rows, list) and len(rows) == 1 else {}
    # The read family's own baked command runs if a RunTask ever loses its
    # override: it must never be (or point at) a migration.
    if not checks.check(
        isinstance(container, dict) and not _names_migration_tool(container),
        "read_family_names_no_migration_tool",
        "read_family_names_the_migration_tool",
    ):
        return None
    if target is None:
        return None
    # The read program needs python, SQLAlchemy and asyncpg: an application
    # image of this registry (by tag or digest).
    if not checks.check(
        _REGISTRY_IMAGE.fullmatch(target.image) is not None,
        "read_image_in_registry",
        "read_image_not_an_application_image",
    ):
        return None
    return target


def _snapshot(
    reads: _Reads, context: Context, checks: hook._Checks, now: datetime
) -> tuple[datetime | None, dict[str, Any]]:
    """The window's manual snapshot: available, recent, of the database."""

    response = reads.get(
        "rds", "DescribeDBSnapshots", describe_snapshot_request(context.snapshot_id)
    )
    rows = response.get("DBSnapshots") if isinstance(response, dict) else None
    info: dict[str, Any] = {"identifier": context.snapshot_id, "status": None}
    if not checks.check(
        hook._error_code(response) is None
        and isinstance(response, dict)
        and not response.get("Marker")
        and isinstance(rows, list)
        and len(rows) == 1
        and isinstance(rows[0], dict),
        "snapshot_readable",
        "snapshot_not_found",
    ):
        return None, info
    assert isinstance(rows, list)
    snapshot = rows[0]
    status = snapshot.get("Status")
    info["status"] = status if status in ("available", "creating") else "other"
    checks.check(
        snapshot.get("DBSnapshotIdentifier") == context.snapshot_id
        and snapshot.get("DBInstanceIdentifier") == RDS_INSTANCE
        and snapshot.get("SnapshotType") == "manual"
        and snapshot.get("Engine") == "postgres",
        "snapshot_identity",
        "snapshot_identity_mismatch",
    )
    checks.check(status == "available", "snapshot_available", "snapshot_not_available")
    created = hook._timestamp(snapshot.get("SnapshotCreateTime"))
    if not checks.check(
        created is not None, "snapshot_create_time", "snapshot_create_time_invalid"
    ):
        return None, info
    assert created is not None
    age = (now - created).total_seconds()
    info["age_seconds"] = int(age)
    checks.check(
        -READBACK_MAX_FUTURE_SECONDS <= age <= SNAPSHOT_MAX_AGE_SECONDS,
        "snapshot_recent",
        "snapshot_too_old",
    )
    created_day = created.astimezone(timezone.utc).date()
    checks.check(
        context.snapshot_day in (created_day, created_day - timedelta(days=1)),
        "snapshot_name_date_matches_creation",
        "snapshot_name_date_mismatch",
    )
    return created, info


def _rollback_target(
    reads: _Reads,
    window: MigrationWindow,
    checks: hook._Checks,
    manifest: Path | None,
) -> dict[str, Any]:
    """036 and 037: the manifest's rollback_target re-pinned, revision ACTIVE.

    Read through the planner (its strict loader and full manifest validation),
    imported here only: the planner imports this module's window table.
    """

    if window.rollback_target_revision is None:
        return {}
    from scripts import asaas_production_wiring as wiring

    try:
        pins = wiring.manifest_pins(
            wiring.validate_manifest(
                wiring.load_yaml(
                    manifest if manifest is not None else wiring.DEFAULT_MANIFEST
                )
            )
        )
    except wiring.WiringError:
        checks.check(False, "manifest_valid", "manifest_invalid")
        return {}
    info = {"rollback_target_revision": pins.rollback_revision}
    if not checks.check(
        pins.rollback_revision == window.rollback_target_revision
        and pins.rollback_repin_before is None,
        "rollback_target_repinned",
        "rollback_target_not_repinned",
    ):
        return info
    arn = pins.rollback_task_definition_arn
    definition = _definition(
        reads.get(
            "ecs", "DescribeTaskDefinition", describe_task_definition_request(arn)
        ),
        arn,
        APP_TASK_FAMILY,
    )
    rows = definition.get("containerDefinitions") if definition else None
    apps = [
        row
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict) and row.get("name") == APP_CONTAINER
    ]
    checks.check(
        definition is not None
        and definition.get("status") == "ACTIVE"
        and len(apps) == 1
        and isinstance(apps[0].get("image"), str)
        and _IMAGE.fullmatch(apps[0]["image"]) is not None,
        "rollback_target_revision_active_digest_pinned",
        "rollback_target_revision_unusable",
    )
    return info


def _freshness(readback: WindowReadback, checks: hook._Checks, now: datetime) -> int:
    _require(now.tzinfo is not None, "now_invalid")
    age = (now - readback.captured_at).total_seconds()
    checks.check(age <= READBACK_MAX_AGE_SECONDS, "readback_fresh", "readback_stale")
    checks.check(
        age >= -READBACK_MAX_FUTURE_SECONDS,
        "readback_not_in_future",
        "readback_captured_in_future",
    )
    return int(age)


# ---------------------------------------------------------------------------
# The read program (SELECT-only, READ ONLY transaction, always rolled back)
# ---------------------------------------------------------------------------

# It prints only JSON lines it builds itself (never the URL, a host, a server
# message): begin, version, one line per row, end. On any error the end line
# carries the exception class and SQLSTATE only. The connection arguments come
# from DATABASE_URL exactly as the application's SQLAlchemy asyncpg dialect
# derives them; the session starts with default_transaction_read_only=on and
# the work runs in a REPEATABLE READ, READ ONLY transaction that is always
# rolled back, with search_path=public. Every SQL text it runs (the revision
# read of the alembic_version table and the readback file) travels in the
# payload, checked against its SHA-256. The program never invokes the
# migration tool: it only reads the alembic_version table. Spelling checks
# (no "alembic" in the program text, nor in window 037's read revision's
# command or environment) are a backstop for that, not the guarantee; the
# guarantee is that the command is exactly this program (normalized_read_command)
# and that PostgreSQL refuses anything but a read in the session.
_READ_PROGRAM_BODY = """\
import asyncio,base64,gzip,hashlib,json,os,sys,warnings
warnings.simplefilter("ignore")
n=0
def o(k,**v):
 global n
 print(json.dumps({"schema":S,"seq":n,"kind":k,**v},separators=(",",":"),default=str),flush=True)
 n+=1
async def m():
 import asyncpg
 from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
 from sqlalchemy.engine import make_url
 p=gzip.decompress(base64.b64decode(D))
 if hashlib.sha256(p).hexdigest()!=H:raise ValueError()
 j=json.loads(p)
 o("begin",window=W,phase=P,payload_sha256=H)
 a,k=PGDialect_asyncpg().create_connect_args(make_url(os.environ["DATABASE_URL"]))
 for z in ("async_fallback","async_creator_fn","prepared_statement_cache_size","prepared_statement_name_func"):k.pop(z,None)
 s=dict(k.pop("server_settings",None) or {});s["default_transaction_read_only"]="on"
 c=await asyncpg.connect(*a,timeout=T,server_settings=s,**k)
 try:
  t=c.transaction(isolation="repeatable_read",readonly=True)
  await t.start()
  try:
   await c.execute(L)
   o("version",values=sorted(r[0] for r in await c.fetch(j["version"])))
   rows=await c.fetch(j["sql"]) if j["sql"] else []
   for r in rows:o("row",values=list(r))
   o("end",status="ok",rows=len(rows))
  finally:
   await t.rollback()
 finally:
  await c.close()
try:
 asyncio.run(m());x=0
except BaseException as e:
 o("end",status="error",error_class=type(e).__name__,sqlstate=getattr(e,"sqlstate",None));x=3
sys.exit(x)
"""
_READ_HEADER = re.compile(
    r"^S='(?P<schema>[a-z_.0-9]+)';W='(?P<window>[0-9]{3})';P='(?P<phase>pre|post)';"
    r"H='(?P<sha>[0-9a-f]{64})';D='(?P<payload>[A-Za-z0-9+/=]+)';"
    r"T=(?P<connect>[0-9]+);L=\"(?P<settings>[^\"\n]+)\"\n"
)


# The revision read: every read reports what the alembic_version table holds.
VERSION_SQL = "SELECT version_num FROM public.alembic_version"
# Any mention of the migration tool (case-insensitive): never in a read
# program's text, nor in the command, entry point or environment of a revision
# a read task runs on (window 037's read revision, the read family's
# revision). A backstop by spelling: the program only reads the
# alembic_version table and never invokes the tool.
_MIGRATION_TOOL = re.compile(r"alembic", re.IGNORECASE)


def _read_settings() -> str:
    return (
        f"SET LOCAL lock_timeout='{READ_LOCK_TIMEOUT_SECONDS}s';"
        f"SET LOCAL statement_timeout='{READ_STATEMENT_TIMEOUT_SECONDS}s';"
        "SET LOCAL search_path=public"
    )


def read_sql(spec: ReadSpec) -> str:
    """The read's SQL, verbatim from its repository file ('' for none)."""

    if spec.sql_file is None:
        return ""
    path = _REPOSITORY_ROOT / spec.sql_file
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise WindowError(f"read_sql_unreadable:{spec.sql_file}") from None
    _require(text.isascii() and text.strip() != "", f"read_sql_invalid:{spec.sql_file}")
    return text


def read_payload(spec: ReadSpec) -> bytes:
    """Every SQL text one read runs: the revision read and the readback file."""

    return json.dumps(
        {"sql": read_sql(spec), "version": VERSION_SQL},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")


def program_for_payload(window: MigrationWindow, phase: str, raw: bytes) -> str:
    """The read program carrying ``raw`` (``read_payload`` for a real read)."""

    payload = base64.b64encode(gzip.compress(raw, mtime=0)).decode("ascii")
    header = (
        f"S='{READ_LINE_SCHEMA}';W='{window.name}';P='{phase}';"
        f"H='{hashlib.sha256(raw).hexdigest()}';D='{payload}';"
        f"T={READ_CONNECT_TIMEOUT_SECONDS};"
        f'L="{_read_settings()}"\n'
    )
    program = header + _READ_PROGRAM_BODY
    _require(
        _MIGRATION_TOOL.search(program) is None, "read_program_names_the_migration_tool"
    )
    return program


def read_program(window: MigrationWindow, spec: ReadSpec) -> str:
    return program_for_payload(window, spec.phase, read_payload(spec))


def _decompressed(payload: str) -> bytes | None:
    try:
        raw = base64.b64decode(payload, validate=True)
        inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
        data = inflater.decompress(raw, READ_PAYLOAD_MAX_BYTES)
    except (ValueError, zlib.error):
        return None
    if inflater.unconsumed_tail or not inflater.eof or inflater.unused_data:
        return None
    return data


def normalized_read_command(command: Any) -> list[str] | None:
    """A read command with its compressed payload replaced by its SHA-256.

    Two renderings of the same read can differ only in the gzip bytes (zlib
    builds differ); the digest the program itself checks is what counts.
    """

    if not (
        isinstance(command, list)
        and len(command) == 3
        and command[:2] == ["python", "-c"]
        and isinstance(command[2], str)
    ):
        return None
    program = command[2]
    match = _READ_HEADER.match(program)
    if (
        match is None
        or program[match.end() :] != _READ_PROGRAM_BODY
        or match.group("schema") != READ_LINE_SCHEMA
        or match.group("window") not in WINDOWS
        or match.group("connect") != str(READ_CONNECT_TIMEOUT_SECONDS)
        or match.group("settings") != _read_settings()
    ):
        return None
    payload = match.group("payload")
    data = _decompressed(payload)
    if data is None or hashlib.sha256(data).hexdigest() != match.group("sha"):
        return None
    start, end = match.span("payload")
    return [
        "python",
        "-c",
        program[:start] + f"<payload:{match.group('sha')}>" + program[end:],
    ]


# ---------------------------------------------------------------------------
# RunTask renderings
# ---------------------------------------------------------------------------


def _network(network: Network) -> dict[str, Any]:
    return {
        "awsvpcConfiguration": {
            "subnets": list(network.subnets),
            "securityGroups": list(network.security_groups),
            "assignPublicIp": "DISABLED",
        }
    }


def _client_token(prefix: str, material: dict[str, Any]) -> str:
    """Idempotency token (ECS: at most 64 characters, 33-126)."""

    return prefix + hook._sha256(material)[:32]


def _comparable_read_request(request: dict[str, Any]) -> dict[str, Any]:
    comparable = copy.deepcopy(request)
    comparable.pop("clientToken", None)
    overrides = comparable.get("overrides")
    rows = overrides.get("containerOverrides") if isinstance(overrides, dict) else None
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and "command" in row:
                row["command"] = normalized_read_command(row["command"])
    return comparable


def build_read_request(
    window: MigrationWindow,
    phase: str,
    network: Network,
    target: TaskTarget,
    *,
    attempt: int,
) -> dict[str, Any]:
    _require(
        type(attempt) is int and 1 <= attempt <= MAX_ATTEMPT, "attempt_out_of_range"
    )
    spec = window.read(phase)
    request: dict[str, Any] = {
        "cluster": CLUSTER,
        "taskDefinition": target.task_definition_arn,
        "count": 1,
        "launchType": "FARGATE",
        "networkConfiguration": _network(network),
        "startedBy": window.read_started_by(phase),
        "enableECSManagedTags": False,
        "enableExecuteCommand": False,
    }
    if not target.baked:
        # The read family's command is replaced by the read program; window
        # 037's read revision bakes it and gets no override at all.
        request["overrides"] = {
            "containerOverrides": [
                {
                    "name": target.container,
                    "command": ["python", "-c", read_program(window, spec)],
                }
            ]
        }
    if network.platform_version is not None:
        request["platformVersion"] = network.platform_version
    request["clientToken"] = _client_token(
        f"migration-read-{window.name}-{phase}-",
        {"attempt": attempt, "request": _comparable_read_request(request)},
    )
    assert_read_request_invariants(request, window, phase, target)
    return request


def assert_read_request_invariants(
    request: Any, window: MigrationWindow, phase: str, target: TaskTarget
) -> None:
    """Defence in depth: a read task can never become a migration or widen."""

    _require(
        not (isinstance(request, dict) and "propagateTags" in request),
        "run_task_request_invariant:propagate_tags_rejected_by_run_task_api",
    )
    if target.baked:
        _require(
            not (isinstance(request, dict) and "overrides" in request),
            "read_request_invariant:override_forbidden_on_the_read_revision",
        )
        _require(
            isinstance(request, dict)
            and RUN_TASK_KEYS <= set(request) <= RUN_TASK_KEYS | OPTIONAL_TASK_KEYS,
            "read_request_invariant:key_set",
        )
        _assert_common_invariants(request, target.task_definition_arn)
        _require(
            _MIGRATIONS_ARN.fullmatch(target.task_definition_arn) is not None
            and request["startedBy"] == window.read_started_by(phase)
            and isinstance(request.get("clientToken"), str)
            and request["clientToken"].startswith(
                f"migration-read-{window.name}-{phase}-"
            )
            and _CLIENT_TOKEN.fullmatch(request["clientToken"]) is not None,
            "read_request_invariant",
        )
        return
    _require(
        isinstance(request, dict)
        and READ_TASK_KEYS <= set(request) <= READ_TASK_KEYS | OPTIONAL_TASK_KEYS,
        "read_request_invariant:key_set",
    )
    _assert_common_invariants(request, target.task_definition_arn)
    _require(
        _READ_ARN.fullmatch(target.task_definition_arn) is not None,
        "read_request_invariant:override_only_on_the_read_family",
    )
    overrides = request["overrides"]
    rows = overrides.get("containerOverrides") if isinstance(overrides, dict) else None
    _require(
        isinstance(overrides, dict)
        and set(overrides) == {"containerOverrides"}
        and isinstance(rows, list)
        and len(rows) == 1
        and isinstance(rows[0], dict)
        and set(rows[0]) == {"name", "command"}
        and rows[0]["name"] == target.container
        and normalized_read_command(rows[0]["command"])
        == normalized_read_command(
            ["python", "-c", read_program(window, window.read(phase))]
        )
        and normalized_read_command(rows[0]["command"]) is not None
        and request["startedBy"] == window.read_started_by(phase)
        and isinstance(request.get("clientToken"), str)
        and request["clientToken"].startswith(f"migration-read-{window.name}-{phase}-")
        and _CLIENT_TOKEN.fullmatch(request["clientToken"]) is not None,
        "read_request_invariant",
    )
    size = len(json.dumps(overrides, separators=(",", ":"), ensure_ascii=True))
    _require(
        size <= OVERRIDES_BUDGET_CHARS, "read_request_invariant:overrides_too_large"
    )


def _assert_common_invariants(request: dict[str, Any], expected_arn: str) -> None:
    network = request.get("networkConfiguration")
    vpc = network.get("awsvpcConfiguration") if isinstance(network, dict) else None
    _require(
        isinstance(network, dict)
        and set(network) == {"awsvpcConfiguration"}
        and isinstance(vpc, dict)
        and set(vpc) == {"subnets", "securityGroups", "assignPublicIp"}
        and vpc["assignPublicIp"] == "DISABLED"
        and isinstance(vpc["subnets"], list)
        and bool(vpc["subnets"])
        and isinstance(vpc["securityGroups"], list)
        and bool(vpc["securityGroups"]),
        "run_task_request_invariant:network",
    )
    _require(
        request.get("cluster") == CLUSTER
        and request.get("taskDefinition") == expected_arn
        and request.get("count") == 1
        and request.get("launchType") == "FARGATE"
        and request.get("enableExecuteCommand") is False
        and request.get("enableECSManagedTags") is False
        and (
            "platformVersion" not in request
            or (
                isinstance(request["platformVersion"], str)
                and hook._PLATFORM_VERSION.fullmatch(request["platformVersion"])
                is not None
            )
        ),
        "run_task_request_invariant",
    )


def build_run_task_request(
    window: MigrationWindow,
    network: Network,
    migrations_task_definition: str,
    snapshot_id: str,
    *,
    attempt: int,
) -> dict[str, Any]:
    """The migration RunTask: the baked command, never an override."""

    _require(
        type(attempt) is int and 1 <= attempt <= MAX_ATTEMPT, "attempt_out_of_range"
    )
    request: dict[str, Any] = {
        "cluster": CLUSTER,
        "taskDefinition": migrations_task_definition,
        "count": 1,
        "launchType": "FARGATE",
        "networkConfiguration": _network(network),
        "startedBy": window.started_by,
        # No ``propagateTags``: RunTask rejects every value; absent = none.
        "enableECSManagedTags": False,
        "enableExecuteCommand": False,
    }
    if network.platform_version is not None:
        request["platformVersion"] = network.platform_version
    # One token per window, snapshot (the restore point of this attempt),
    # attempt and request: a retried call cannot start a second migration.
    request["clientToken"] = _client_token(
        f"migration-window-{window.name}-run-",
        {"snapshot": snapshot_id, "attempt": attempt, "request": request},
    )
    assert_run_task_invariants(request, window, migrations_task_definition)
    return request


def assert_run_task_invariants(
    request: Any, window: MigrationWindow, migrations_task_definition: str
) -> None:
    """Defence in depth: the migration RunTask can never widen."""

    _require(
        not (isinstance(request, dict) and "propagateTags" in request),
        "run_task_request_invariant:propagate_tags_rejected_by_run_task_api",
    )
    _require(
        not (isinstance(request, dict) and "overrides" in request),
        "run_task_request_invariant:command_override_forbidden",
    )
    _require(
        isinstance(request, dict)
        and RUN_TASK_KEYS <= set(request) <= RUN_TASK_KEYS | OPTIONAL_TASK_KEYS,
        "run_task_request_invariant:key_set",
    )
    _require(
        _MIGRATIONS_ARN.fullmatch(migrations_task_definition) is not None,
        "run_task_request_invariant:task_definition",
    )
    _assert_common_invariants(request, migrations_task_definition)
    _require(
        request["startedBy"] == window.started_by
        and isinstance(request.get("clientToken"), str)
        and request["clientToken"].startswith(f"migration-window-{window.name}-run-")
        and _CLIENT_TOKEN.fullmatch(request["clientToken"]) is not None,
        "run_task_request_invariant",
    )


def watch_policy() -> dict[str, int]:
    return {
        "readback_max_age_seconds": READBACK_MAX_AGE_SECONDS,
        "snapshot_max_age_seconds": SNAPSHOT_MAX_AGE_SECONDS,
        "snapshot_poll_interval_seconds": SNAPSHOT_POLL_INTERVAL_SECONDS,
        "pre_read_max_age_seconds": PRE_READ_MAX_AGE_SECONDS,
        "start_budget_seconds": START_BUDGET_SECONDS,
        "migration_run_budget_seconds": MIGRATION_RUN_BUDGET_SECONDS,
        "stop_budget_seconds": STOP_BUDGET_SECONDS,
        "migration_watch_deadline_seconds": MIGRATION_WATCH_DEADLINE_SECONDS,
        "read_watch_deadline_seconds": READ_WATCH_DEADLINE_SECONDS,
        "poll_interval_seconds": POLL_INTERVAL_SECONDS,
        "log_grace_seconds": LOG_GRACE_SECONDS,
    }


# ---------------------------------------------------------------------------
# render-snapshot-requests
# ---------------------------------------------------------------------------


def render_snapshot_requests(
    window: MigrationWindow, *, now: datetime
) -> dict[str, Any]:
    _require(now.tzinfo is not None, "now_invalid")
    snapshot_id = snapshot_identifier(window, now.astimezone(timezone.utc).date())
    validate_snapshot_id(window, snapshot_id)
    return {
        "schema": SNAPSHOT_SCHEMA,
        "window": window.name,
        "snapshot_identifier": snapshot_id,
        "requests": [
            _request_row(
                "rds", "CreateDBSnapshot", create_snapshot_request(snapshot_id)
            ),
            _request_row(
                "rds", "DescribeDBSnapshots", describe_snapshot_request(snapshot_id)
            ),
        ],
        "poll": {
            "request": 1,
            "interval_seconds": SNAPSHOT_POLL_INTERVAL_SECONDS,
            "until_status": "available",
            "max_age_seconds_at_plan": SNAPSHOT_MAX_AGE_SECONDS,
        },
        "on_failure": FAILURE_POLICY,
    }


# ---------------------------------------------------------------------------
# Read tasks: plan, render, verify
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReadContext:
    network: Network
    target: TaskTarget


def _read_context(
    reads: _Reads, context: Context, phase: str, checks: hook._Checks
) -> ReadContext | None:
    network, _desired, _deployment = _service(reads, context, checks)
    target = _read_target(reads, context, context.window.read(phase), checks)
    if network is None or target is None:
        return None
    return ReadContext(network=network, target=target)


def evaluate_read_plan(
    readback: WindowReadback,
    context: Context,
    phase: str,
    *,
    now: datetime,
) -> tuple[dict[str, Any], ReadContext | None]:
    """Preconditions of one read task; the context only when ready."""

    window = context.window
    spec = window.read(phase)
    checks = hook._Checks()
    reads = _Reads(readback)
    age = _freshness(readback, checks, now)
    record = f"{phase}_read"
    checks.check(
        record not in readback.records,
        f"no_{record}_recorded",
        f"{record}_already_recorded",
    )
    if phase == "pre":
        checks.check(
            "migration" not in readback.records,
            "no_migration_run_recorded",
            "migration_run_already_recorded",
        )
    read_context = _read_context(reads, context, phase, checks)
    snapshot_info: dict[str, Any] = {}
    if phase == "pre":
        # The documented order: snapshot available, then the readback.
        _created, snapshot_info = _snapshot(reads, context, checks, now)
    running = {
        "read": _single_flight(
            reads,
            checks,
            list_started_by_request(window.read_started_by(phase)),
            "read_task",
        ),
        "migrations_family": _single_flight(
            reads,
            checks,
            list_family_tasks_request(MIGRATIONS_FAMILY),
            "migrations_task",
        ),
    }
    if read_context is not None:
        try:
            build_read_request(
                window, phase, read_context.network, read_context.target, attempt=1
            )
        except WindowError as exc:
            checks.check(False, "read_request_renderable", str(exc))
    status = (
        "incomplete" if reads.missing else ("blocked" if checks.blocks else "ready")
    )
    document = {
        "schema": READ_PLAN_SCHEMA,
        "window": window.name,
        "phase": phase,
        "status": status,
        "readback_age_seconds": age,
        "checks": list(dict.fromkeys(checks.passed)),
        "blocks": checks.blocks,
        "next_requests": reads.missing,
        "read": _read_summary(spec),
        "read_task_definition_arn": (
            read_context.target.task_definition_arn if read_context else None
        ),
        "snapshot": snapshot_info,
        "running": running,
        "watch": watch_policy(),
        "on_failure": FAILURE_POLICY,
    }
    return document, read_context if status == "ready" else None


class _ReadLines:
    def __init__(self) -> None:
        self.began: dict[str, Any] | None = None
        self.version: list[str] | None = None
        self.rows: list[list[Any]] = []
        self.end: dict[str, Any] | None = None
        self.malformed = 0
        self.foreign = 0
        self.exposure = False
        self.sequence_broken = False


_READ_SCALARS = (str, int, bool, type(None))


def _scalar(value: Any) -> bool:
    return isinstance(value, _READ_SCALARS) and not (
        isinstance(value, str) and len(value) > 8000
    )


def parse_read_lines(messages: list[str]) -> _ReadLines:
    """The read program's JSON lines; anything else is foreign or malformed."""

    lines = _ReadLines()
    expected = 0
    for message in messages:
        if _LOG_EXPOSURE.search(message):
            lines.exposure = True
        if READ_LINE_SCHEMA not in message:
            lines.foreign += 1
            continue
        try:
            document = hook._strict_json(message)
        except hook.HookError:
            lines.malformed += 1
            continue
        if not (
            isinstance(document, dict)
            and document.get("schema") == READ_LINE_SCHEMA
            and type(document.get("seq")) is int
            and document.get("kind") in ("begin", "version", "row", "end")
        ):
            lines.malformed += 1
            continue
        if document["seq"] != expected or lines.end is not None:
            lines.sequence_broken = True
        expected = document["seq"] + 1
        kind = document["kind"]
        keys = set(document)
        if kind == "begin" and keys == {
            "schema",
            "seq",
            "kind",
            "window",
            "phase",
            "payload_sha256",
        }:
            if lines.began is not None or document["seq"] != 0:
                lines.sequence_broken = True
            lines.began = document
        elif (
            kind == "version"
            and keys == {"schema", "seq", "kind", "values"}
            and isinstance(document["values"], list)
            and all(
                isinstance(v, str) and re.fullmatch(r"[0-9a-z_]{1,32}", v)
                for v in document["values"]
            )
        ):
            if lines.version is not None:
                lines.sequence_broken = True
            lines.version = list(document["values"])
        elif (
            kind == "row"
            and keys == {"schema", "seq", "kind", "values"}
            and isinstance(document["values"], list)
            and all(_scalar(v) for v in document["values"])
        ):
            lines.rows.append(list(document["values"]))
        elif kind == "end" and (
            (
                keys == {"schema", "seq", "kind", "status", "rows"}
                and document["status"] == "ok"
                and type(document["rows"]) is int
            )
            or (
                keys == {"schema", "seq", "kind", "status", "error_class", "sqlstate"}
                and document["status"] == "error"
            )
        ):
            lines.end = document
        else:
            lines.malformed += 1
    return lines


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _safe_text(value: Any) -> str | None:
    """Database text that may be printed: no exposure, nothing the guard refuses."""

    if not isinstance(value, str) or len(value) > 4000:
        return None
    if _LOG_EXPOSURE.search(value) or _FORBIDDEN_OUTPUT.search(value):
        return None
    return value


def judge_read(
    lines: _ReadLines, window: MigrationWindow, spec: ReadSpec
) -> tuple[list[str], dict[str, Any]]:
    """(reasons, report) for one read's lines; no reasons means the read is ok."""

    reasons: list[str] = []
    report: dict[str, Any] = {"alembic_version": lines.version, "rows": None}

    def fail(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    if lines.exposure:
        fail("read_log_exposure_suspected")
    if lines.foreign:
        fail("read_log_foreign_line")
    if lines.malformed:
        fail("read_line_malformed")
    if lines.sequence_broken:
        fail("read_sequence_broken")
    end = lines.end
    if end is None:
        fail("read_end_missing")
        return reasons, report
    if end["status"] != "ok":
        error_class = end.get("error_class")
        sqlstate = end.get("sqlstate")
        report["error_class"] = (
            error_class
            if isinstance(error_class, str)
            and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", error_class)
            else None
        )
        report["sqlstate"] = (
            sqlstate
            if isinstance(sqlstate, str) and re.fullmatch(r"[0-9A-Z]{5}", sqlstate)
            else None
        )
        fail("read_failed")
        return reasons, report
    began = lines.began
    if not (
        isinstance(began, dict)
        and began.get("window") == window.name
        and began.get("phase") == spec.phase
        and began.get("payload_sha256")
        == hashlib.sha256(read_payload(spec)).hexdigest()
    ):
        fail("read_begin_mismatch")
    if end["rows"] != len(lines.rows):
        fail("read_row_count_mismatch")
    if lines.version != [spec.expected_version]:
        if spec.phase == "post" and lines.version == [window.before]:
            fail("post_read_version_is_before_nothing_applied")
        else:
            fail("read_alembic_version_mismatch")
    rows = lines.rows
    if spec.rule == RULE_VERSION_ONLY:
        if rows:
            fail("read_rows_unexpected")
        report["rows"] = []
        return reasons, report
    if spec.rule == RULE_ZERO_COUNTS:
        names = [row[0] if len(row) == 2 else None for row in rows]
        if sorted(map(str, names)) != sorted(spec.checks) or len(set(names)) != len(
            rows
        ):
            fail("read_checks_mismatch")
        counts = {
            str(row[0]): row[1]
            for row in rows
            if len(row) == 2 and isinstance(row[0], str)
        }
        for name in spec.checks:
            count = counts.get(name)
            if not (type(count) is int and count == 0):
                fail(f"read_count_not_zero:{_slug(name)}")
        report["rows"] = [
            {
                "check": name,
                "count": counts.get(name) if type(counts.get(name)) is int else None,
            }
            for name in spec.checks
        ]
        return reasons, report
    if spec.rule == RULE_VERDICT_OK:
        expected = [[index, name] for index, name in enumerate(spec.checks, start=1)]
        if [row[:2] for row in rows] != expected or any(len(row) != 3 for row in rows):
            fail("read_checks_mismatch")
        verdicts: list[dict[str, Any]] = []
        for row in rows:
            if len(row) != 3:
                continue
            verdict = row[2]
            if verdict != "ok":
                fail(f"read_verdict_not_ok:{_slug(str(row[1]))}")
            verdicts.append(
                {
                    "check_no": row[0] if type(row[0]) is int else None,
                    "check": (
                        row[1]
                        if isinstance(row[1], str) and row[1] in spec.checks
                        else None
                    ),
                    "verdict": _safe_text(verdict) if verdict != "ok" else "ok",
                    "withheld": verdict != "ok" and _safe_text(verdict) is None,
                }
            )
        report["rows"] = verdicts
        return reasons, report
    # RULE_RELATIONS_GONE
    names = [row[0] if len(row) == 2 else None for row in rows]
    if sorted(map(str, names)) != sorted(spec.checks) or len(set(names)) != len(rows):
        fail("read_checks_mismatch")
    for row in rows:
        if len(row) == 2 and row[1] is not True:
            fail(f"read_relation_still_exists:{_slug(str(row[0]))}")
    report["rows"] = [
        {"relation": row[0], "gone": row[1]}
        for row in rows
        if len(row) == 2 and row[0] in spec.checks
    ]
    return reasons, report


class _Verdict:
    def __init__(self) -> None:
        self.reasons: list[str] = []
        self.kinds: set[str] = set()
        self.in_progress: list[str] = []
        self.next_requests: list[dict[str, Any]] = []

    def fail(self, reason: str, kind: str = "failed") -> None:
        if reason not in self.reasons:
            self.reasons.append(reason)
        self.kinds.add(kind)

    def wait(self, reason: str, requests: list[dict[str, Any]]) -> None:
        if reason not in self.in_progress:
            self.in_progress.append(reason)
        self.next_requests.extend(r for r in requests if r not in self.next_requests)

    def outcome(self, success: str) -> str:
        if self.reasons:
            return OUTCOME_STOPPED
        if self.in_progress:
            return OUTCOME_IN_PROGRESS
        return success


def _task_reads(task_arn: str, log: hook.LogTarget) -> list[dict[str, Any]]:
    match = _TASK_ARN.fullmatch(task_arn)
    assert match is not None
    return [
        _request_row("ecs", "DescribeTasks", describe_tasks_request([task_arn])),
        _request_row(
            "logs", "GetLogEvents", hook.log_events_request(log, match.group("id"))
        ),
    ]


def _started_task(response: Any) -> dict[str, Any] | None:
    tasks = response.get("tasks") if isinstance(response, dict) else None
    if not (
        isinstance(response, dict)
        and response.get("failures") == []
        and isinstance(tasks, list)
        and len(tasks) == 1
        and isinstance(tasks[0], dict)
        and isinstance(tasks[0].get("taskArn"), str)
        and _TASK_ARN.fullmatch(tasks[0]["taskArn"]) is not None
    ):
        return None
    return dict(tasks[0])


def _override_command(holder: Any, container: str) -> list[str] | None:
    overrides = holder.get("overrides") if isinstance(holder, dict) else None
    rows = overrides.get("containerOverrides") if isinstance(overrides, dict) else None
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("name") == container and "command" in row:
            command = row.get("command")
            return list(command) if isinstance(command, list) else []
    return None


# DescribeTasks reports a task started without overrides as
# {"containerOverrides": [{"name": <container>}], "inferenceAcceleratorOverrides": []}.
_TASK_OVERRIDE_KEYS = frozenset({"containerOverrides", "inferenceAcceleratorOverrides"})


def overrides_problem(task: Any, container: str, command: list[str] | None) -> bool:
    """Whether a described task carries any override this tool did not render.

    Allowed: no task-level override (no environment, roles, cpu, memory,
    storage, accelerators) and, per container, only ``{"name"}``; for a read
    on the read family the read program as that container's one command.
    """

    overrides = task.get("overrides") if isinstance(task, dict) else None
    if overrides is None:
        return False
    if not isinstance(overrides, dict) or not set(overrides) <= _TASK_OVERRIDE_KEYS:
        return True
    if overrides.get("inferenceAcceleratorOverrides", []) != []:
        return True
    rows = overrides.get("containerOverrides", [])
    if not isinstance(rows, list):
        return True
    expected = normalized_read_command(command) if command is not None else None
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("name"), str):
            return True
        if set(row) == {"name"}:
            continue
        if (
            expected is not None
            and set(row) == {"name", "command"}
            and row["name"] == container
            and normalized_read_command(row["command"]) == expected
        ):
            continue
        return True
    return False


def _exit_code(task: dict[str, Any], container: str) -> int | None:
    code = (_container(task, container) or {}).get("exitCode")
    return code if type(code) is int else None


def _read_chain(
    readback: WindowReadback,
    verdict: _Verdict,
    task: dict[str, Any],
    log: hook.LogTarget,
    task_arn: str,
) -> list[str] | None:
    """Every recorded log page of a stopped task, or None when not usable yet."""

    match = _TASK_ARN.fullmatch(task_arn)
    assert match is not None
    try:
        chain = hook.read_log_chain(readback.reads, log, match.group("id"))
    except hook.HookError:
        verdict.wait("log_events_not_read", _task_reads(task_arn, log)[1:])
        return None
    if chain.problem is not None:
        verdict.fail(chain.problem, "evidence_mismatch")
        return chain.messages
    if not chain.complete:
        assert chain.next_request is not None
        verdict.wait(
            chain.pending, [_request_row("logs", "GetLogEvents", chain.next_request)]
        )
        return None
    if not chain.messages:
        stopped = hook._timestamp(task.get("stoppedAt"))
        if (
            stopped is not None
            and (readback.captured_at - stopped).total_seconds() <= LOG_GRACE_SECONDS
        ):
            verdict.wait(
                "log_events_not_delivered",
                [
                    _request_row(
                        "logs",
                        "GetLogEvents",
                        hook.log_events_request(log, match.group("id")),
                    )
                ],
            )
            return None
    return chain.messages


def verify_read(
    readback: WindowReadback,
    context: Context,
    phase: str,
    *,
    reads: _Reads | None = None,
) -> dict[str, Any]:
    """Classify a finished read task. Pure; a failing read stops the window.

    ``reads`` shares the caller's missing-read list (``plan``).
    """

    window = context.window
    spec = window.read(phase)
    verdict = _Verdict()
    record = readback.records.get(f"{phase}_read")
    _require(record is not None, f"{phase}_read_not_recorded")
    assert record is not None
    checks = hook._Checks()
    reads = reads if reads is not None else _Reads(readback)
    misses_before = reads.misses
    read_context = _read_context(reads, context, phase, checks)
    attempt = None
    task_arn = None
    report: dict[str, Any] = {}
    exit_code = None
    times: dict[str, str | None] = {"created_at": None, "stopped_at": None}
    if read_context is None and reads.misses > misses_before:
        verdict.wait("readback_incomplete", list(reads.missing))
    elif read_context is None:
        verdict.fail(
            "read_context_unusable:" + ",".join(checks.blocks), "evidence_mismatch"
        )
    else:
        params = record["params"]
        if isinstance(params, dict) and "propagateTags" in params:
            verdict.fail("run_task_request_propagate_tags_present", "evidence_mismatch")
        comparable = (
            _comparable_read_request(params) if isinstance(params, dict) else None
        )
        for candidate in range(1, MAX_ATTEMPT + 1):
            rendered = build_read_request(
                window,
                phase,
                read_context.network,
                read_context.target,
                attempt=candidate,
            )
            if (
                comparable == _comparable_read_request(rendered)
                and params.get("clientToken") == rendered["clientToken"]
            ):
                attempt = candidate
                break
        if attempt is None:
            verdict.fail("read_request_not_rendered_by_this_tool", "evidence_mismatch")
        started = _started_task(record["response"])
        if started is None:
            verdict.fail("read_run_task_failed")
        else:
            task_arn = str(started["taskArn"])
            target = read_context.target
            if started.get(
                "taskDefinitionArn", target.task_definition_arn
            ) != target.task_definition_arn or started.get(
                "startedBy", window.read_started_by(phase)
            ) != window.read_started_by(
                phase
            ):
                verdict.fail("read_run_task_response_invalid", "evidence_mismatch")
            described = reads.get(
                "ecs", "DescribeTasks", describe_tasks_request([task_arn])
            )
            task = _started_task(described)
            if described is None:
                verdict.wait(
                    "read_task_not_described", _task_reads(task_arn, target.log)[:1]
                )
            elif task is None or task.get("taskArn") != task_arn:
                verdict.fail("read_task_describe_invalid", "evidence_mismatch")
            else:
                times = {
                    "created_at": (
                        task.get("createdAt")
                        if isinstance(task.get("createdAt"), str)
                        else None
                    ),
                    "stopped_at": (
                        task.get("stoppedAt")
                        if isinstance(task.get("stoppedAt"), str)
                        else None
                    ),
                }
                if task.get(
                    "taskDefinitionArn"
                ) != target.task_definition_arn or task.get(
                    "startedBy"
                ) != window.read_started_by(
                    phase
                ):
                    verdict.fail("read_task_identity_mismatch", "evidence_mismatch")
                program = ["python", "-c", read_program(window, spec)]
                if overrides_problem(
                    task, target.container, None if target.baked else program
                ):
                    # Only the rendered override (the read family's one
                    # command) or none at all (the read revision).
                    verdict.fail("read_task_overrides_present", "evidence_mismatch")
                if not target.baked:
                    observed = normalized_read_command(
                        _override_command(task, target.container)
                    )
                    if observed is None or observed != normalized_read_command(
                        ["python", "-c", read_program(window, spec)]
                    ):
                        verdict.fail("read_task_command_mismatch", "evidence_mismatch")
                if task.get("lastStatus") != "STOPPED":
                    created = hook._timestamp(task.get("createdAt"))
                    if created is None:
                        verdict.fail("read_task_timestamps_invalid")
                    elif (
                        readback.captured_at - created
                    ).total_seconds() > READ_WATCH_DEADLINE_SECONDS:
                        verdict.fail("read_watch_deadline_exceeded", "watch_deadline")
                    else:
                        verdict.wait(
                            "read_task_not_stopped", _task_reads(task_arn, target.log)
                        )
                else:
                    exit_code = _exit_code(task, target.container)
                    messages = _read_chain(
                        readback, verdict, task, target.log, task_arn
                    )
                    if messages is not None:
                        lines = parse_read_lines(messages)
                        reasons, report = judge_read(lines, window, spec)
                        for reason in reasons:
                            kind = (
                                "exposure_suspected"
                                if reason == "read_log_exposure_suspected"
                                else "read_not_ok"
                            )
                            verdict.fail(reason, kind)
                        if exit_code != 0:
                            verdict.fail("read_exit_code_not_zero")
    outcome = verdict.outcome(OUTCOME_VERIFIED if phase == "post" else OUTCOME_READ_OK)
    return {
        "schema": READ_RESULT_SCHEMA,
        "window": window.name,
        "phase": phase,
        "outcome": outcome,
        "alert": outcome == OUTCOME_STOPPED,
        "alert_kinds": sorted(verdict.kinds) if verdict.reasons else [],
        "reasons": verdict.reasons if verdict.reasons else verdict.in_progress,
        "on_failure": FAILURE_POLICY,
        "task_arn": task_arn,
        "attempt": attempt,
        "container_exit_code": exit_code,
        "task_times": times,
        "expected_alembic_version": spec.expected_version,
        "alembic_version": report.get("alembic_version"),
        "rows": report.get("rows"),
        "error_class": report.get("error_class"),
        "sqlstate": report.get("sqlstate"),
        "next_requests": (
            verdict.next_requests if outcome == OUTCOME_IN_PROGRESS else []
        ),
    }


# ---------------------------------------------------------------------------
# plan: every precondition of the run
# ---------------------------------------------------------------------------


def _shared_preconditions(
    readback: WindowReadback,
    context: Context,
    reads: _Reads,
    checks: hook._Checks,
    now: datetime,
    manifest: Path | None,
) -> dict[str, Any]:
    """What the snapshot step and the run both require: fresh reads, no run
    recorded, the deployed service and images, this window's migrations
    revision (and 037's read revision), single flight, the rollback target."""

    window = context.window
    age = _freshness(readback, checks, now)
    checks.check(
        "migration" not in readback.records,
        "no_migration_run_recorded",
        "migration_run_already_recorded",
    )
    network, desired, _deployment = _service(reads, context, checks)
    service_image, service_digest = _service_image(reads, context, checks)
    target = _migrations_target(reads, context, checks)
    expected_digest = service_digest
    if target is not None:
        _image_in_ecr(reads, target.image, checks)
        if window.image_rule == IMAGE_RULE_DEPLOYED:
            # The image containing the migration is the deployed one: the
            # service revision runs it by digest and the migrations revision
            # carries exactly the same reference.
            checks.check(
                service_image is not None and service_image == target.image,
                "migrations_image_is_the_deployed_image",
                "migrations_image_differs_from_deployed_image",
            )
            match = _IMAGE.fullmatch(target.image)
            expected_digest = match.group("digest") if match else None
    if needs_read_revision(window):
        _read_target(reads, context, window.pre_read, checks)
    tasks_info = _service_tasks(reads, context, checks, desired, expected_digest)
    running = {
        "migrations_family": _single_flight(
            reads,
            checks,
            list_family_tasks_request(MIGRATIONS_FAMILY),
            "migrations_task",
        ),
        "window": _single_flight(
            reads, checks, list_started_by_request(window.started_by), "window_task"
        ),
        "read_family": _single_flight(
            reads, checks, list_family_tasks_request(READ_FAMILY), "read_family_task"
        ),
    }
    return {
        "age": age,
        "network": network,
        "target": target,
        "expected_digest": expected_digest,
        "service_tasks": tasks_info,
        "running": running,
        "manifest": _rollback_target(reads, window, checks, manifest),
    }


def evaluate_snapshot_plan(
    readback: WindowReadback,
    context: Context,
    *,
    now: datetime,
    manifest: Path | None = None,
) -> dict[str, Any]:
    """The snapshot is the window's restore point: take it only once the
    deploy state the run needs holds (same reads as ``plan`` minus the
    snapshot and the pre-read, which come after it)."""

    checks = hook._Checks()
    reads = _Reads(readback)
    shared = _shared_preconditions(readback, context, reads, checks, now, manifest)
    checks.check(
        "pre_read" not in readback.records,
        "no_pre_read_recorded",
        "pre_read_recorded_before_the_snapshot",
    )
    status = (
        "incomplete" if reads.missing else ("blocked" if checks.blocks else "ready")
    )
    return {
        "schema": SNAPSHOT_PLAN_SCHEMA,
        "window": context.window.name,
        "status": status,
        "readback_age_seconds": shared["age"],
        "checks": list(dict.fromkeys(checks.passed)),
        "blocks": checks.blocks,
        "next_requests": reads.missing,
        "snapshot_identifier": context.snapshot_id,
        "expected_image_digest": shared["expected_digest"],
        "running": shared["running"],
        "manifest": shared["manifest"],
        "on_failure": FAILURE_POLICY,
    }


def _run_exit_codes(task: dict[str, Any]) -> list[int | None]:
    """Each container's integer exit code (None when absent or not an int)."""

    rows = task.get("containers")
    codes: list[int | None] = []
    for row in rows if isinstance(rows, list) else []:
        code = row.get("exitCode") if isinstance(row, dict) else None
        codes.append(code if type(code) is int else None)
    return codes


def _earlier_run_summary(task: dict[str, Any]) -> dict[str, Any]:
    stopped = hook._timestamp(task.get("stoppedAt"))
    arn = task.get("taskArn")
    return {
        "task_arn": arn if isinstance(arn, str) and _TASK_ARN.fullmatch(arn) else None,
        "exit_codes": _run_exit_codes(task),
        "stopped_at": stopped.isoformat() if stopped is not None else None,
    }


def _stopped_at_or_after(task: dict[str, Any], moment: datetime | None) -> bool:
    """True unless the task provably stopped before ``moment``."""

    stopped = hook._timestamp(task.get("stoppedAt"))
    return moment is None or stopped is None or stopped >= moment


@dataclass(frozen=True)
class RunContext:
    network: Network
    migrations_task_definition: str
    snapshot_id: str


def evaluate_plan(
    readback: WindowReadback,
    context: Context,
    *,
    now: datetime,
    manifest: Path | None = None,
) -> tuple[dict[str, Any], RunContext | None]:
    """Pure precondition check; the run context is returned only when ready."""

    window = context.window
    checks = hook._Checks()
    reads = _Reads(readback)
    shared = _shared_preconditions(readback, context, reads, checks, now, manifest)
    age, network, target = shared["age"], shared["network"], shared["target"]
    expected_digest, tasks_info = shared["expected_digest"], shared["service_tasks"]
    running, rollback_info = shared["running"], shared["manifest"]
    created, snapshot_info = _snapshot(reads, context, checks, now)
    stopped_listing = _listing(
        reads,
        list_family_tasks_request(MIGRATIONS_FAMILY, "STOPPED"),
        checks,
        "migrations_stopped_tasks",
    )
    stopped_tasks = (
        _described(reads, stopped_listing, checks, "migrations_stopped_tasks")
        if stopped_listing is not None
        else None
    )
    earlier_runs = [
        task
        for task in stopped_tasks or []
        if task.get("startedBy") == window.started_by
    ]
    pre_read_created: datetime | None = None
    pre_read: dict[str, Any] = {"outcome": None}
    if checks.check(
        "pre_read" in readback.records, "pre_read_recorded", "pre_read_not_recorded"
    ):
        result = verify_read(readback, context, "pre", reads=reads)
        pre_read = {
            "outcome": result["outcome"],
            "reasons": result["reasons"],
            "alembic_version": result["alembic_version"],
            "task_arn": result["task_arn"],
        }
        for row in result["next_requests"]:
            if row not in reads.missing:
                reads.missing.append(row)
        if checks.check(
            result["outcome"] == OUTCOME_READ_OK,
            "pre_read_ok",
            "pre_read_not_ok",
        ):
            checks.passed.append(f"alembic_version_is_{window.before}")
            stopped = hook._timestamp(result["task_times"]["stopped_at"])
            started = hook._timestamp(result["task_times"]["created_at"])
            checks.check(
                stopped is not None
                and 0 <= (now - stopped).total_seconds() + READBACK_MAX_FUTURE_SECONDS
                and (now - stopped).total_seconds() <= PRE_READ_MAX_AGE_SECONDS,
                "pre_read_fresh",
                "pre_read_stale",
            )
            checks.check(
                started is not None and created is not None and started >= created,
                "pre_read_after_snapshot",
                "pre_read_before_snapshot",
            )
            pre_read_created = started
    if stopped_tasks is not None:
        # A run of this window that already stopped (failed with 55P03, say)
        # is not single-flight's business but a new run's: a retry is a new
        # owner instruction, and its pre-read must start after the earlier
        # task stopped (the database it reads is the one that task left).
        later = [
            task
            for task in earlier_runs
            if _stopped_at_or_after(task, pre_read_created)
        ]
        checks.check(
            not later,
            "no_window_task_stopped_after_pre_read",
            "window_task_stopped_after_pre_read",
        )
        # A run of this window that exited 0 already applied the window (or
        # was restored away): a re-run is not a recovery and this tool does
        # not support it; it needs a new owner decision and a fresh plan.
        checks.check(
            not [task for task in earlier_runs if 0 in _run_exit_codes(task)],
            "no_clean_window_run_listed",
            "window_already_ran_cleanly",
        )
    run_context = None
    if network is not None and target is not None:
        try:
            build_run_task_request(
                window,
                network,
                context.migrations_task_definition,
                context.snapshot_id,
                attempt=1,
            )
            run_context = RunContext(
                network=network,
                migrations_task_definition=context.migrations_task_definition,
                snapshot_id=context.snapshot_id,
            )
        except WindowError as exc:
            checks.check(False, "run_task_renderable", str(exc))
    status = (
        "incomplete" if reads.missing else ("blocked" if checks.blocks else "ready")
    )
    document = {
        "schema": PLAN_SCHEMA,
        "window": window.name,
        "status": status,
        "readback_age_seconds": age,
        "checks": list(dict.fromkeys(checks.passed)),
        "blocks": checks.blocks,
        "next_requests": reads.missing,
        "window_table": window_summary(window),
        "service_task_definition_arn": context.service_task_definition,
        "migrations_task_definition_arn": context.migrations_task_definition,
        "read_task_definition_arn": context.read_task_definition,
        "expected_image_digest": expected_digest,
        "service_tasks": tasks_info,
        "snapshot": snapshot_info,
        "pre_read": pre_read,
        "running": running,
        "earlier_window_tasks": sorted(
            (_earlier_run_summary(task) for task in earlier_runs),
            key=lambda row: str(row["task_arn"]),
        ),
        "manifest": rollback_info,
        "run_task": {
            "started_by": window.started_by,
            "baked_command": list(window.command),
            "command_override": "none",
            "count": 1,
            "assign_public_ip": "DISABLED",
            "subnet_count": len(network.subnets) if network else 0,
            "security_group_count": len(network.security_groups) if network else 0,
        },
        "watch": watch_policy(),
        "on_failure": FAILURE_POLICY,
    }
    return document, run_context if status == "ready" else None


def run_context_for(
    readback: WindowReadback,
    context: Context,
    *,
    now: datetime,
    manifest: Path | None = None,
) -> RunContext:
    document, run_context = evaluate_plan(readback, context, now=now, manifest=manifest)
    _require(
        run_context is not None and document["status"] == "ready",
        "plan_blocked:" + ",".join(document["blocks"] or ["readback_incomplete"]),
    )
    assert run_context is not None
    return run_context


# ---------------------------------------------------------------------------
# verify-result: the stopped migration task
# ---------------------------------------------------------------------------

_UPGRADE_LINE = re.compile(
    r"Running upgrade (?P<older>[0-9a-z]+) -> (?P<newer>[0-9a-z]+)(?:,|$)"
)
_OTHER_OPERATION = re.compile(r"Running (?!upgrade )[a-z_]+ ")
_SQLSTATE_MARKERS = (
    (
        "55P03",
        re.compile(
            r"LockNotAvailableError|canceling statement due to lock timeout|\b55P03\b"
        ),
    ),
    ("40P01", re.compile(r"DeadlockDetectedError|deadlock detected|\b40P01\b")),
    ("55000", re.compile(r"ObjectNotInPrerequisiteStateError|\b55000\b")),
)
_REFUSAL = re.compile(
    r"(?P<message>migration [0-9]{3} refused \(nothing was changed\): .+)$"
)
_FAILURE_LINE = re.compile(
    r"^(?:Traceback \(most recent call last\):|FAILED:|ERROR\b|CRITICAL\b)"
)
NOTHING_CHANGED = MappingProxyType(
    {
        "55P03": "lock_timeout_55P03_nothing_changed",
        "40P01": "deadlock_40P01_nothing_changed",
        "55000": "preflight_refused_55000_nothing_changed",
    }
)


@dataclass
class MigrationLog:
    upgrades: list[tuple[str, str]]
    other_operations: int
    failure_lines: int
    sqlstates: list[str]
    refusals: list[str]
    refusals_withheld: int
    exposure: bool
    lines: int


def classify_migration_log(messages: list[str]) -> MigrationLog:
    log = MigrationLog([], 0, 0, [], [], 0, False, len(messages))
    for message in messages:
        if _LOG_EXPOSURE.search(message) and not _SQLALCHEMY_BACKGROUND.fullmatch(
            message.strip()
        ):
            log.exposure = True
        upgrade = _UPGRADE_LINE.search(message)
        if upgrade is not None:
            log.upgrades.append((upgrade.group("older"), upgrade.group("newer")))
        elif "alembic" in message and _OTHER_OPERATION.search(message):
            log.other_operations += 1
        if _FAILURE_LINE.search(message.strip()):
            log.failure_lines += 1
        for sqlstate, marker in _SQLSTATE_MARKERS:
            if marker.search(message) and sqlstate not in log.sqlstates:
                log.sqlstates.append(sqlstate)
        if "ObjectNotInPrerequisiteStateError" in message:
            refusal = _REFUSAL.search(message)
            if refusal is not None:
                text = _safe_text(refusal.group("message").rstrip())
                if text is None:
                    log.refusals_withheld += 1
                elif text not in log.refusals:
                    log.refusals.append(text)
    return log


def judge_migration(
    verdict: _Verdict, window: MigrationWindow, log: MigrationLog, exit_code: int | None
) -> str | None:
    """The classification name, or None for a clean run."""

    if log.exposure:
        verdict.fail("log_exposure_suspected", "exposure_suspected")
    beyond = [pair for pair in log.upgrades if pair not in window.upgrades]
    if beyond:
        verdict.fail("upgrade_outside_window", "schema_beyond_window")
    if log.other_operations:
        verdict.fail("alembic_operation_unexpected")
    # A missing exit code was already refused by verify_result.
    failed_exit = type(exit_code) is int and exit_code != 0
    classification = None
    if len(log.sqlstates) > 1:
        verdict.fail("migration_error_ambiguous")
        classification = "ambiguous"
    elif log.sqlstates and failed_exit:
        # "Nothing changed" only when the process failed: Alembic's single
        # transaction was rolled back with that SQLSTATE.
        classification = NOTHING_CHANGED[log.sqlstates[0]]
        verdict.fail(classification, "nothing_changed")
        if log.sqlstates == ["55000"] and not (log.refusals or log.refusals_withheld):
            verdict.fail("preflight_refusal_message_missing")
    elif log.sqlstates:
        # A SQLSTATE marker while the exit code is 0 (or unknown): the log and
        # the process disagree; never "nothing changed".
        classification = "migration_log_inconsistent"
        verdict.fail(classification)
    elif log.failure_lines or failed_exit:
        classification = "migration_failed_unclassified"
        verdict.fail(classification)
    if classification is None and tuple(log.upgrades) != window.upgrades:
        verdict.fail("upgrade_lines_mismatch")
    return classification


def _migration_target_for_verify(
    readback: WindowReadback, context: Context
) -> tuple[Network | None, TaskTarget | None]:
    checks = hook._Checks()
    reads = _Reads(readback)
    network, _desired, _deployment = _service(reads, context, checks)
    return network, _migrations_target(reads, context, checks)


def _snapshot_created(readback: WindowReadback, context: Context) -> datetime | None:
    reads = _Reads(readback)
    response = reads.get(
        "rds", "DescribeDBSnapshots", describe_snapshot_request(context.snapshot_id)
    )
    rows = response.get("DBSnapshots") if isinstance(response, dict) else None
    if isinstance(rows, list) and len(rows) == 1 and isinstance(rows[0], dict):
        return hook._timestamp(rows[0].get("SnapshotCreateTime"))
    return None


def _migration_task_arn(
    readback: WindowReadback, task_arn: str | None
) -> tuple[str | None, bool]:
    if task_arn is not None:
        task_arn = validate_task_arn(task_arn)
    record = readback.records.get("migration")
    if record is None:
        _require(task_arn is not None, "migration_run_not_recorded")
        return task_arn, False
    started = _started_task(record["response"])
    started_arn = str(started["taskArn"]) if started else None
    if task_arn is not None:
        _require(started_arn == task_arn, "task_arn_does_not_match_recorded_run")
    return started_arn, True


def render_verify_reads(
    readback: WindowReadback,
    context: Context,
    target: str,
    *,
    task_arn: str | None = None,
) -> dict[str, Any]:
    """DescribeTasks and the first log page of one task this tool started."""

    _require(target in ("pre-read", "migration", "post-read"), "verify_target_invalid")
    checks = hook._Checks()
    reads = _Reads(readback)
    if target == "migration":
        arn, recorded = _migration_task_arn(readback, task_arn)
        task = _migrations_target(reads, context, checks)
    else:
        _require(task_arn is None, "task_arn_only_for_the_migration")
        phase = "pre" if target == "pre-read" else "post"
        record = readback.records.get(f"{phase}_read")
        _require(record is not None, f"{phase}_read_not_recorded")
        assert record is not None
        started = _started_task(record["response"])
        arn, recorded = (str(started["taskArn"]) if started else None), True
        task = _read_target(reads, context, context.window.read(phase), checks)
    _require(arn is not None, "run_task_started_no_task")
    _require(
        task is not None,
        "task_definition_unusable:" + ",".join(checks.blocks or ["missing_reads"]),
    )
    assert arn is not None and task is not None
    return {
        "schema": VERIFY_READS_SCHEMA,
        "window": context.window.name,
        "target": target,
        "task_arn": arn,
        "from_recorded_run": recorded,
        "requests": _task_reads(arn, task.log),
        "log_pages": (
            "send the first page; while a response's nextForwardToken differs "
            "from the token sent, send the same request with nextToken set to it"
        ),
        "watch": watch_policy(),
    }


def verify_result(
    readback: WindowReadback, context: Context, *, task_arn: str | None = None
) -> dict[str, Any]:
    """Classify a finished migration run. Pure; stopping is the only failure path."""

    window = context.window
    verdict = _Verdict()
    arn, recorded = _migration_task_arn(readback, task_arn)
    network, target = _migration_target_for_verify(readback, context)
    attempt = None
    if recorded:
        record = readback.records["migration"]
        params = record["params"]
        if isinstance(params, dict) and "propagateTags" in params:
            verdict.fail("run_task_request_propagate_tags_present", "evidence_mismatch")
        if isinstance(params, dict) and "overrides" in params:
            verdict.fail(
                "run_task_request_command_override_present", "evidence_mismatch"
            )
        if network is not None:
            for candidate in range(1, MAX_ATTEMPT + 1):
                if params == build_run_task_request(
                    window,
                    network,
                    context.migrations_task_definition,
                    context.snapshot_id,
                    attempt=candidate,
                ):
                    attempt = candidate
                    break
        if attempt is None:
            verdict.fail(
                "run_task_request_not_rendered_by_this_tool", "evidence_mismatch"
            )
    document: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "window": window.name,
        "task_definition_arn": context.migrations_task_definition,
        "task_arn": arn,
        "recovered_without_run_record": not recorded,
        "attempt": attempt,
        "container_exit_code": None,
        "stop_code": None,
        "classification": None,
        "upgrades": [],
        "expected_upgrades": [f"{a} -> {b}" for a, b in window.upgrades],
        "refusal_lines": [],
        "refusal_lines_withheld": 0,
        "log_lines": 0,
        "on_failure": FAILURE_POLICY,
    }

    def finish() -> dict[str, Any]:
        outcome = verdict.outcome(OUTCOME_APPLIED)
        document.update(
            {
                "outcome": outcome,
                "alert": outcome == OUTCOME_STOPPED,
                "alert_kinds": sorted(verdict.kinds) if verdict.reasons else [],
                "reasons": verdict.reasons if verdict.reasons else verdict.in_progress,
                "next_requests": (
                    verdict.next_requests if outcome == OUTCOME_IN_PROGRESS else []
                ),
                "next": _next_step(window, outcome, document.get("classification")),
            }
        )
        return document

    if arn is None:
        verdict.fail("run_task_failed")
        return finish()
    if recorded:
        started = _started_task(readback.records["migration"]["response"])
        assert started is not None
        if (
            started.get("taskDefinitionArn") != context.migrations_task_definition
            or started.get("startedBy", window.started_by) != window.started_by
        ):
            verdict.fail("run_task_response_invalid", "evidence_mismatch")
    if target is None:
        verdict.fail("migrations_task_definition_unusable", "evidence_mismatch")
        return finish()
    reads = _Reads(readback)
    described = reads.get("ecs", "DescribeTasks", describe_tasks_request([arn]))
    task = _started_task(described)
    if described is None:
        verdict.wait("migration_task_not_described", _task_reads(arn, target.log)[:1])
        return finish()
    if task is None or task.get("taskArn") != arn:
        verdict.fail("describe_tasks_invalid", "evidence_mismatch")
        return finish()
    if (
        task.get("taskDefinitionArn") != context.migrations_task_definition
        or task.get("startedBy") != window.started_by
    ):
        verdict.fail("task_identity_mismatch", "evidence_mismatch")
    if overrides_problem(task, target.container, None):
        # The command is baked: no container command, environment or
        # resource override and no task-level override (roles, cpu, memory).
        verdict.fail("task_overrides_present", "evidence_mismatch")
    if not recorded:
        # Crash recovery: only time ties a task to this window attempt.
        created = hook._timestamp(task.get("createdAt"))
        snapshot = _snapshot_created(readback, context)
        if created is None or snapshot is None:
            verdict.fail("recovered_task_not_tied_to_window", "evidence_mismatch")
        elif created < snapshot:
            verdict.fail("recovered_task_predates_snapshot", "evidence_mismatch")
    if task.get("lastStatus") != "STOPPED":
        created = hook._timestamp(task.get("createdAt"))
        if created is None:
            verdict.fail("task_timestamps_invalid")
        elif (
            readback.captured_at - created
        ).total_seconds() > MIGRATION_WATCH_DEADLINE_SECONDS:
            verdict.fail("migration_watch_deadline_exceeded", "watch_deadline")
        else:
            verdict.wait("migration_task_not_stopped", _task_reads(arn, target.log))
        return finish()
    stop_code = task.get("stopCode")
    document["stop_code"] = (
        stop_code if stop_code in hook._STOP_CODES else ("other" if stop_code else None)
    )
    exit_code = _exit_code(task, target.container)
    document["container_exit_code"] = exit_code
    if exit_code is None:
        # A stopped task without an exit code never ran Alembic to the end
        # (failed start, crash): final, whatever its log holds.
        verdict.fail("migration_stopped_without_exit_code")
    messages = _read_chain(readback, verdict, task, target.log, arn)
    if messages is None:
        return finish()
    log = classify_migration_log(messages)
    document["upgrades"] = [f"{a} -> {b}" for a, b in log.upgrades]
    document["refusal_lines"] = log.refusals
    document["refusal_lines_withheld"] = log.refusals_withheld
    document["log_lines"] = log.lines
    stopped = hook._timestamp(task.get("stoppedAt"))
    if (
        exit_code == 0
        and not verdict.reasons
        and messages
        and not (log.exposure or log.sqlstates or log.failure_lines)
        and not log.other_operations
        and len(log.upgrades) < len(window.upgrades)
        and tuple(log.upgrades) == window.upgrades[: len(log.upgrades)]
        and stopped is not None
        and (readback.captured_at - stopped).total_seconds() <= LOG_GRACE_SECONDS
    ):
        # A clean exit whose last upgrade lines have not reached CloudWatch
        # yet: read the stream again before judging it.
        match = _TASK_ARN.fullmatch(arn)
        assert match is not None
        verdict.wait(
            "log_events_not_delivered",
            [
                _request_row(
                    "logs",
                    "GetLogEvents",
                    hook.log_events_request(target.log, match.group("id")),
                )
            ],
        )
        return finish()
    if not messages:
        verdict.fail("migration_log_empty")
    document["classification"] = judge_migration(verdict, window, log, exit_code)
    return finish()


def _next_step(window: MigrationWindow, outcome: str, classification: Any) -> str:
    if outcome == OUTCOME_IN_PROGRESS:
        return "poll: send next_requests, update the readback file, verify again"
    if outcome == OUTCOME_APPLIED:
        return f"render-post-read: alembic_version must read {window.after}"
    if isinstance(classification, str) and classification in NOTHING_CHANGED.values():
        return (
            f"report to the owner and stop; render-post-read confirms "
            f"alembic_version still reads {window.before}; no new run without "
            "the owner's instruction"
        )
    return "report to the owner and stop; start nothing else"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _without_read_programs(value: Any) -> Any:
    """The payload with every exact read-program command replaced by a marker.

    The read program is a fixed constant (its SQL transaction ends in a
    ``rollback()``, its payload is arbitrary base64); only a command that
    ``normalized_read_command`` accepts is exempt from the guard.
    """

    if isinstance(value, dict):
        return {
            key: (
                ["python", "-c", "<read program>"]
                if key == "command" and normalized_read_command(nested) is not None
                else _without_read_programs(nested)
            )
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_without_read_programs(nested) for nested in value]
    return value


def _emit(payload: Any) -> None:
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True)
    guarded = json.dumps(
        _without_read_programs(payload), sort_keys=True, ensure_ascii=True
    )
    _require(_FORBIDDEN_OUTPUT.search(guarded) is None, "output_forbidden")
    print(text)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise WindowError("usage")


def _attempt(value: str) -> int:
    if not (value.isascii() and value.isdecimal()):
        raise argparse.ArgumentTypeError("attempt")
    number = int(value)
    if not 1 <= number <= MAX_ATTEMPT:
        raise argparse.ArgumentTypeError("attempt")
    return number


def _clock() -> datetime:
    """The system clock: the only time source of the CLI (no ``--now`` flag,
    so a caller can never move the freshness windows). Tests replace it."""

    return datetime.now(timezone.utc)


class _Once(argparse.Action):
    """Store an option given at most once (a repeated option is refused)."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: Any,
        option_string: str | None = None,
    ) -> None:
        seen: set[str] = getattr(namespace, "_given_once", set())
        if self.dest in seen:
            parser.error(f"{option_string} given twice")
        seen.add(self.dest)
        setattr(namespace, "_given_once", seen)
        setattr(namespace, self.dest, values)


_SNAPSHOT_COMMANDS = ("plan-snapshot", "render-snapshot-requests")
_CONTEXT_COMMANDS = (
    "plan-pre-read",
    "render-pre-read",
    "verify-pre-read",
    "plan",
    "render-run-task",
    "render-verify-reads",
    "verify-result",
    "plan-post-read",
    "render-post-read",
    "verify-post-read",
)


def _parser() -> _Parser:
    parser = _Parser(prog="asaas_migration_window.py", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("windows")
    for name in (*_SNAPSHOT_COMMANDS, *_CONTEXT_COMMANDS):
        sub = subparsers.add_parser(name)
        add = sub.add_argument
        # Required, no default, given once: a window is never chosen by
        # accident (nor the last of two).
        add("--window", required=True, choices=tuple(WINDOWS), action=_Once)
        add("--readback-file", type=Path, required=True, action=_Once)
        add("--service-task-definition", required=True, action=_Once)
        add("--migrations-task-definition", required=True, action=_Once)
        add("--read-task-definition", required=True, action=_Once)
        if name not in _SNAPSHOT_COMMANDS:
            # The snapshot step names the snapshot itself (today, UTC).
            add("--snapshot-id", required=True, action=_Once)
        # Window 037 only (required there, refused elsewhere): the dedicated
        # read revision whose baked command is the readback.
        add("--migrations-read-task-definition", default=None, action=_Once)
        if name in ("render-pre-read", "render-post-read", "render-run-task"):
            add("--attempt", type=_attempt, default=1, action=_Once)
        if name in ("plan", "render-run-task", *_SNAPSHOT_COMMANDS):
            add("--manifest", type=Path, default=None, action=_Once)
        if name == "render-verify-reads":
            add(
                "--target",
                required=True,
                choices=("pre-read", "migration", "post-read"),
                action=_Once,
            )
        if name in ("render-verify-reads", "verify-result"):
            # Crash recovery: the migration task when no RunTask was recorded.
            add("--task-arn", default=None, action=_Once)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
        if args.command == "windows":
            _emit(
                {
                    "schema": WINDOWS_SCHEMA,
                    "windows": [window_summary(window) for window in WINDOWS.values()],
                }
            )
            return 0
        window = window_named(args.window)
        now = _clock()
        _require(now.tzinfo is not None, "now_invalid")
        command = args.command
        snapshot_id = (
            snapshot_identifier(window, now.astimezone(timezone.utc).date())
            if command in _SNAPSHOT_COMMANDS
            else args.snapshot_id
        )
        context = make_context(
            window,
            service_task_definition=args.service_task_definition,
            migrations_task_definition=args.migrations_task_definition,
            read_task_definition=args.read_task_definition,
            snapshot_id=snapshot_id,
            migrations_read_task_definition=args.migrations_read_task_definition,
        )
        readback = load_readback(args.readback_file, window)
        if command in _SNAPSHOT_COMMANDS:
            document = evaluate_snapshot_plan(
                readback, context, now=now, manifest=args.manifest
            )
            if command == "plan-snapshot":
                _emit(document)
                return 0 if document["status"] == "ready" else 2
            _require(
                document["status"] == "ready",
                "snapshot_plan_blocked:"
                + ",".join(document["blocks"] or ["readback_incomplete"]),
            )
            _emit(render_snapshot_requests(window, now=now))
            return 0
        if command in (
            "plan-pre-read",
            "plan-post-read",
            "render-pre-read",
            "render-post-read",
        ):
            phase = "pre" if "-pre-" in command else "post"
            document, read_context = evaluate_read_plan(
                readback, context, phase, now=now
            )
            if command.startswith("plan"):
                _emit(document)
                return 0 if document["status"] == "ready" else 2
            _require(
                read_context is not None,
                "read_plan_blocked:"
                + ",".join(document["blocks"] or ["readback_incomplete"]),
            )
            assert read_context is not None
            _emit(
                build_read_request(
                    window,
                    phase,
                    read_context.network,
                    read_context.target,
                    attempt=args.attempt,
                )
            )
            return 0
        if command in ("verify-pre-read", "verify-post-read"):
            result = verify_read(
                readback, context, "pre" if "-pre-" in command else "post"
            )
            _emit(result)
            return EXIT_CODES[result["outcome"]]
        if command == "plan":
            document, _run = evaluate_plan(
                readback, context, now=now, manifest=args.manifest
            )
            _emit(document)
            return 0 if document["status"] == "ready" else 2
        if command == "render-run-task":
            run = run_context_for(readback, context, now=now, manifest=args.manifest)
            _emit(
                build_run_task_request(
                    window,
                    run.network,
                    run.migrations_task_definition,
                    run.snapshot_id,
                    attempt=args.attempt,
                )
            )
            return 0
        if command == "render-verify-reads":
            _emit(
                render_verify_reads(
                    readback, context, args.target, task_arn=args.task_arn
                )
            )
            return 0
        result = verify_result(readback, context, task_arn=args.task_arn)
        _emit(result)
        return EXIT_CODES[result["outcome"]]
    except hook.HookError as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
