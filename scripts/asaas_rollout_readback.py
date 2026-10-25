#!/usr/bin/env python3
"""Pure tooling for the post-deploy rollout readback (SELECT only).

The "Rollout fechado" criterion of the asaas-payment-gateway wish needs one
read of the production database after the final deploy: ``alembic_version``
is ``037``; every row of ``billing_rollout_control`` (the four environments)
is ``disabled`` with ``topup_commercial_enabled`` false; the four tables and
ten functions migration 037 dropped are absent; ``billing_subscriptions``
and ``billing_checkout_sessions`` are empty. It shows that state at the
moment of the read, not its history (no rollout revision or receipt is
read). This tool runs that read at any time after the migration windows,
outside them: no migration task, no snapshot, none of a window's ordering
preconditions (pre-read before run, snapshot age, the window's revisions).

It is a sibling of ``scripts/asaas_migration_window.py`` and reuses its read
machinery unchanged: the read program (one ``REPEATABLE READ, READ ONLY``
transaction in a session with ``default_transaction_read_only=on``, never
committed; the SQL carried gzip+base64 and checked against its SHA-256 inside
the task; one JSON line per result), the read family contract
(``neurostack-staging-consulta``, application role), the RunTask hardening,
the log reader, the line parser and its exposure scan, the envelope judge
and the output guard. Its own parts: the SQL
(``scripts/asaas_rollout_readback.sql``), the table it must return
(``EXPECTED_TABLE``), its ``startedBy`` (``neurostack-rollout-readback``),
its ``clientToken`` (``rollout-readback-``, per attempt and UTC day) and its
readback file (schema ``asaas_rollout_readback_file.v1``, one record
``read``).

Like the window tool it never calls AWS: it reads a recorded readback file
(the responses the orchestrator collected with its own tools), renders
requests and verifies results. There is no ``--now``: ``plan`` and
``render`` judge freshness against the system clock; ``verify`` does not
read the clock at all, it judges every time against the recorded reads'
``captured_at_utc`` and the task's own timestamps.

``plan``    the preconditions, from the recorded reads: captured within 10
            minutes; the service on ``--service-task-definition`` with one
            ``PRIMARY`` deployment, ``COMPLETED``, steady, Fargate, private
            awsvpc; the read family revision ``--read-task-definition``
            under the read contract; no task of this ``startedBy``, of the
            read family or of the migrations family running; no read
            recorded yet. Missing reads are ``next_requests`` (``incomplete``).
``render``  the exact ``ecs:RunTask`` of a ready plan (``--attempt``).
``verify``  the recorded RunTask is one ``render`` prints; the stopped task
            has this task definition, ``startedBy`` and exactly the program
            as its one command override (payload digest included), was
            created after the service's deployment and shortly before the
            reads that saw it stop, exited 0; every log page read; the
            program's lines exactly ``begin``, ``version``, the rows,
            ``end`` (numbered without a gap); and the rows exactly
            ``EXPECTED_TABLE``, judged by ``(check_name, subject)``: no row
            missing, none added or repeated, while their order is not
            significant (a reordered read is accepted by design). Exit 0
            ``verified`` (with a ``verdict`` line to record), 3
            ``in_progress`` (send ``next_requests``), 4
            ``ROLLOUT_READBACK_STOPPED``.

Stopping is the default: there is no option to continue anyway.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from scripts import asaas_deploy_hook as hook  # noqa: E402
from scripts import asaas_migration_window as mw  # noqa: E402

# ---------------------------------------------------------------------------
# The read
# ---------------------------------------------------------------------------

STARTED_BY = "neurostack-rollout-readback"
CLIENT_TOKEN_PREFIX = "rollout-readback-"
# The labels the program carries: the window whose schema it reads, and the
# readback's own phase (scripts/asaas_migration_window.py recognizes the
# pair, and only this pair, as a read program).
READ_WINDOW = mw.WINDOWS[mw.ROLLOUT_READ_WINDOW]
READ_PHASE = mw.ROLLOUT_READ_PHASE
SQL_FILE = "scripts/asaas_rollout_readback.sql"
RULE_EXPECTED_TABLE = "expected_table"

CHECK_ROLLOUT_STATE = "rollout state"
CHECK_TOPUP = "rollout topup_commercial_enabled"
CHECK_LEGACY_TABLE = "legacy table"
CHECK_LEGACY_FUNCTION = "legacy function"
CHECK_SUBSCRIPTIONS = "billing_subscriptions rows"
CHECK_CHECKOUTS = "billing_checkout_sessions rows"
CHECK_ROW_SECURITY = "row level security on read tables"
CHECKS = (
    CHECK_ROLLOUT_STATE,
    CHECK_TOPUP,
    CHECK_LEGACY_TABLE,
    CHECK_LEGACY_FUNCTION,
    CHECK_SUBSCRIPTIONS,
    CHECK_CHECKOUTS,
    CHECK_ROW_SECURITY,
)
# The facts that are a single count (no subject).
COUNT_CHECKS = (CHECK_SUBSCRIPTIONS, CHECK_CHECKOUTS, CHECK_ROW_SECURITY)
# The four rows migration 026 seeds (the environment CHECK allows no other).
ROLLOUT_ENVIRONMENTS = ("development", "production", "staging", "test")
# Exactly what migration 037 drops (its DROPPED_TABLES and DROPPED_FUNCTIONS,
# schema-qualified; a test keeps them equal).
LEGACY_TABLES = mw.GONE_037
LEGACY_FUNCTIONS = (
    "public.billing_migration_snapshot_is_valid(jsonb,text,text,text,text,text,bigint)",
    "public.lock_billing_roster_statement()",
    "public.validate_billing_migration_receipt()",
    "public.validate_billing_migration_receipt_commit()",
    "public.protect_bound_billing_migration_target()",
    "public.protect_billing_roster_control()",
    "public.validate_billing_provider_evidence()",
    "public.billing_evidence_timestamp(timestamp with time zone)",
    "public.billing_provider_evidence_digest(text,text,timestamp with time zone,"
    "text,uuid,timestamp with time zone,text,uuid,text,text)",
    "public.validate_billing_migration_absence_read()",
)
# (check, subject, value): the whole table the read must return, 4 + 4 + 4 +
# 10 + 3 = 25 rows, judged by (check, subject) whatever their order.
EXPECTED_TABLE: tuple[tuple[str, str, str], ...] = (
    *((CHECK_ROLLOUT_STATE, env, "disabled") for env in ROLLOUT_ENVIRONMENTS),
    *((CHECK_TOPUP, env, "false") for env in ROLLOUT_ENVIRONMENTS),
    *((CHECK_LEGACY_TABLE, table, "absent") for table in LEGACY_TABLES),
    *((CHECK_LEGACY_FUNCTION, name, "absent") for name in LEGACY_FUNCTIONS),
    *((check, "", "0") for check in COUNT_CHECKS),
)
SPEC = mw.ReadSpec(
    phase=READ_PHASE,
    sql_file=SQL_FILE,
    rule=RULE_EXPECTED_TABLE,
    checks=CHECKS,
    role=mw.READ_ROLE_APP,
    expected_version=READ_WINDOW.after,
)

# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

READBACK_SCHEMA = "asaas_rollout_readback_file.v1"
PLAN_SCHEMA = "asaas_rollout_readback_plan.v1"
RESULT_SCHEMA = "asaas_rollout_readback_result.v1"
RECORD_KEY = "read"
# Responses a readback file may carry (all reads).
CONSUMED_READS = frozenset(
    {
        ("ecs", "DescribeServices"),
        ("ecs", "DescribeTaskDefinition"),
        ("ecs", "ListTasks"),
        ("ecs", "DescribeTasks"),
        ("logs", "GetLogEvents"),
    }
)
OUTCOME_VERIFIED = "verified"
OUTCOME_IN_PROGRESS = mw.OUTCOME_IN_PROGRESS
OUTCOME_STOPPED = "ROLLOUT_READBACK_STOPPED"
EXIT_CODES = MappingProxyType(
    {OUTCOME_VERIFIED: 0, OUTCOME_IN_PROGRESS: 3, OUTCOME_STOPPED: 4}
)
FAILURE_POLICY = mw.FAILURE_POLICY
# A verified read task was created at most this long before the latest
# recorded reads (the ones that saw it stop): a RunTask answered with an
# older task (an idempotency token reused) is not this read.
READ_RECORD_MAX_AGE_SECONDS = 3600
_CLIENT_TOKEN = re.compile(r"^rollout-readback-[0-9a-f]{32}$")
_COUNT = re.compile(r"^(?:0|[1-9][0-9]{0,17})$")


class ReadbackError(mw.WindowError):
    """The readback file, an argument or a rendered request is unsafe."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ReadbackError(reason)


def read_summary() -> dict[str, Any]:
    summary = mw._read_summary(SPEC)
    summary["expected_table"] = [list(row) for row in EXPECTED_TABLE]
    return summary


def watch_policy() -> dict[str, int]:
    return {
        "readback_max_age_seconds": mw.READBACK_MAX_AGE_SECONDS,
        "start_budget_seconds": mw.START_BUDGET_SECONDS,
        "read_watch_deadline_seconds": mw.READ_WATCH_DEADLINE_SECONDS,
        "read_record_max_age_seconds": READ_RECORD_MAX_AGE_SECONDS,
        "poll_interval_seconds": mw.POLL_INTERVAL_SECONDS,
        "log_grace_seconds": mw.LOG_GRACE_SECONDS,
    }


# ---------------------------------------------------------------------------
# Readback file
# ---------------------------------------------------------------------------


def load_readback(path: Path) -> mw.WindowReadback:
    """The recorded reads and the recorded RunTask (record ``read``).

    Returned in the window tool's container type so its reader, freshness
    check and log reader apply unchanged; ``window`` is the program's label.
    """

    try:
        data = path.read_bytes()
    except OSError:
        raise ReadbackError("readback_file_unreadable") from None
    _require(len(data) <= mw.READBACK_MAX_BYTES, "readback_file_too_large")
    try:
        raw = hook._strict_json(data)
    except hook.HookError:
        raise ReadbackError("readback_file_unreadable") from None
    base = {"schema_version", "account_id", "region", "captured_at_utc", "reads"}
    _require(
        isinstance(raw, dict)
        and base <= set(raw) <= base | {RECORD_KEY}
        and raw["schema_version"] == READBACK_SCHEMA
        and raw["account_id"] == mw.ACCOUNT_ID
        and raw["region"] == mw.REGION
        and isinstance(raw["reads"], list)
        and isinstance(raw["captured_at_utc"], str)
        and hook._timestamp(raw["captured_at_utc"]) is not None,
        "readback_file_shape_invalid",
    )
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
    if RECORD_KEY in raw:
        record = raw[RECORD_KEY]
        _require(
            isinstance(record, dict)
            and set(record) == {"params", "response"}
            and isinstance(record["params"], dict)
            and isinstance(record["response"], dict),
            "readback_file_read_record_invalid",
        )
        hook._reject_secret_material(record["response"])
        records[RECORD_KEY] = copy.deepcopy(record)
    return mw.WindowReadback(
        window=READ_WINDOW.name,
        captured_at=captured_at,
        reads=hook.Readback(
            captured_at=captured_at, reads=MappingProxyType(reads), run_task=None
        ),
        records=MappingProxyType(records),
    )


# ---------------------------------------------------------------------------
# Context and views over the recorded reads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Context:
    service_task_definition: str
    read_task_definition: str


def make_context(*, service_task_definition: str, read_task_definition: str) -> Context:
    return Context(
        service_task_definition=mw.validate_service_arn(service_task_definition),
        read_task_definition=mw.validate_read_arn(read_task_definition),
    )


@dataclass(frozen=True)
class ReadContext:
    network: mw.Network
    target: mw.TaskTarget
    deployment_id: str | None
    deployment_created_at: datetime | None


def _service(
    reads: mw._Reads, context: Context, checks: hook._Checks
) -> tuple[mw.Network | None, str | None, datetime | None]:
    """The hook's service view (one PRIMARY deployment COMPLETED on the
    expected revision, steady, Fargate, private awsvpc) and when that
    deployment started."""

    response = reads.get("ecs", "DescribeServices", mw.describe_services_request())
    deployment_id, subnets, groups, platform = hook._service_view(
        response, context.service_task_definition, checks
    )
    services = response.get("services") if isinstance(response, dict) else None
    service = services[0] if isinstance(services, list) and services else {}
    deployments = service.get("deployments") if isinstance(service, dict) else None
    primary = (
        deployments[0]
        if isinstance(deployments, list)
        and len(deployments) == 1
        and isinstance(deployments[0], dict)
        else {}
    )
    created = hook._timestamp(primary.get("createdAt"))
    checks.check(
        created is not None, "deployment_created_at", "deployment_created_at_invalid"
    )
    network = (
        mw.Network(subnets=subnets, security_groups=groups, platform_version=platform)
        if subnets and groups
        else None
    )
    return network, deployment_id, created


def _read_context(
    reads: mw._Reads, context: Context, checks: hook._Checks
) -> ReadContext | None:
    network, deployment_id, created = _service(reads, context, checks)
    target = mw.read_family_target(reads, context.read_task_definition, checks)
    if network is None or target is None:
        return None
    return ReadContext(
        network=network,
        target=target,
        deployment_id=deployment_id,
        deployment_created_at=created,
    )


# ---------------------------------------------------------------------------
# The program and the RunTask
# ---------------------------------------------------------------------------


def read_payload() -> bytes:
    return mw.read_payload(SPEC)


def payload_sha256() -> str:
    return hashlib.sha256(read_payload()).hexdigest()


def read_program() -> str:
    return mw.program_for_payload(READ_WINDOW, READ_PHASE, read_payload())


def read_command() -> list[str]:
    return ["python", "-c", read_program()]


def _utc_day(moment: datetime) -> date:
    return moment.astimezone(timezone.utc).date()


def build_request(
    network: mw.Network, target: mw.TaskTarget, *, attempt: int, day: date
) -> dict[str, Any]:
    """The readback RunTask: the read family, the program as its one command
    override, this tool's ``startedBy``; the ``clientToken`` is derived from
    the attempt, the UTC day and the request (another day is another run)."""

    _require(
        type(attempt) is int and 1 <= attempt <= mw.MAX_ATTEMPT, "attempt_out_of_range"
    )
    _require(type(day) is date, "day_invalid")
    request: dict[str, Any] = {
        "cluster": mw.CLUSTER,
        "taskDefinition": target.task_definition_arn,
        "count": 1,
        "launchType": "FARGATE",
        "networkConfiguration": mw._network(network),
        "startedBy": STARTED_BY,
        "enableECSManagedTags": False,
        "enableExecuteCommand": False,
        "overrides": {
            "containerOverrides": [
                {"name": target.container, "command": read_command()}
            ]
        },
    }
    if network.platform_version is not None:
        request["platformVersion"] = network.platform_version
    request["clientToken"] = mw._client_token(
        CLIENT_TOKEN_PREFIX,
        {
            "attempt": attempt,
            "day": day.isoformat(),
            "request": mw._comparable_read_request(request),
        },
    )
    assert_request_invariants(request, target)
    return request


def assert_request_invariants(request: Any, target: mw.TaskTarget) -> None:
    """Defence in depth: the readback can never run anything but the program,
    on anything but the read family, nor widen."""

    _require(
        not (isinstance(request, dict) and "propagateTags" in request),
        "run_task_request_invariant:propagate_tags_rejected_by_run_task_api",
    )
    _require(
        isinstance(request, dict)
        and mw.READ_TASK_KEYS
        <= set(request)
        <= mw.READ_TASK_KEYS | mw.OPTIONAL_TASK_KEYS,
        "read_request_invariant:key_set",
    )
    mw._assert_common_invariants(request, target.task_definition_arn)
    _require(
        mw._READ_ARN.fullmatch(target.task_definition_arn) is not None,
        "read_request_invariant:override_only_on_the_read_family",
    )
    expected = mw.normalized_read_command(read_command())
    _require(expected is not None, "read_request_invariant:program_not_recognized")
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
        and mw.normalized_read_command(rows[0]["command"]) == expected
        and request["startedBy"] == STARTED_BY
        and isinstance(request.get("clientToken"), str)
        and _CLIENT_TOKEN.fullmatch(request["clientToken"]) is not None,
        "read_request_invariant",
    )
    size = len(json.dumps(overrides, separators=(",", ":"), ensure_ascii=True))
    _require(
        size <= mw.OVERRIDES_BUDGET_CHARS, "read_request_invariant:overrides_too_large"
    )


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def evaluate_plan(
    readback: mw.WindowReadback, context: Context, *, now: datetime
) -> tuple[dict[str, Any], ReadContext | None]:
    """Pure precondition check; the read context is returned only when ready."""

    checks = hook._Checks()
    reads = mw._Reads(readback)
    age = mw._freshness(readback, checks, now)
    checks.check(
        RECORD_KEY not in readback.records, "no_read_recorded", "read_already_recorded"
    )
    read_context = _read_context(reads, context, checks)
    running = {
        "readback": mw._single_flight(
            reads, checks, mw.list_started_by_request(STARTED_BY), "readback_task"
        ),
        "read_family": mw._single_flight(
            reads,
            checks,
            mw.list_family_tasks_request(mw.READ_FAMILY),
            "read_family_task",
        ),
        "migrations_family": mw._single_flight(
            reads,
            checks,
            mw.list_family_tasks_request(mw.MIGRATIONS_FAMILY),
            "migrations_task",
        ),
    }
    if read_context is not None:
        try:
            build_request(
                read_context.network, read_context.target, attempt=1, day=_utc_day(now)
            )
        except mw.WindowError as exc:
            checks.check(False, "read_request_renderable", str(exc))
    status = (
        "incomplete" if reads.missing else ("blocked" if checks.blocks else "ready")
    )
    document = {
        "schema": PLAN_SCHEMA,
        "status": status,
        "readback_age_seconds": age,
        "checks": list(dict.fromkeys(checks.passed)),
        "blocks": checks.blocks,
        "next_requests": reads.missing,
        "read": read_summary(),
        "service_task_definition_arn": context.service_task_definition,
        "read_task_definition_arn": context.read_task_definition,
        "deployment_id": read_context.deployment_id if read_context else None,
        "run_task": {
            "started_by": STARTED_BY,
            "command_override": "the read program, one container",
            "payload_sha256": payload_sha256(),
            "count": 1,
            "assign_public_ip": "DISABLED",
        },
        "running": running,
        "watch": watch_policy(),
        "on_failure": FAILURE_POLICY,
    }
    return document, read_context if status == "ready" else None


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def _count(value: Any) -> int | None:
    return int(value) if isinstance(value, str) and _COUNT.fullmatch(value) else None


_FACT_REASONS = MappingProxyType(
    {
        CHECK_ROLLOUT_STATE: "rollout_state_not_disabled",
        CHECK_TOPUP: "topup_commercial_enabled_not_false",
        CHECK_LEGACY_TABLE: "legacy_table_present",
        CHECK_LEGACY_FUNCTION: "legacy_function_present",
        CHECK_SUBSCRIPTIONS: "billing_subscriptions_not_empty",
        CHECK_CHECKOUTS: "billing_checkout_sessions_not_empty",
        CHECK_ROW_SECURITY: "row_level_security_on_read_tables",
    }
)


def _fact_reason(check: str, subject: str) -> str:
    """The stop reason of one fact: an environment, a legacy object's name
    (without schema and argument list), or nothing for the counts."""

    if not subject:
        return _FACT_REASONS[check]
    name = subject.split("(", 1)[0].removeprefix("public.")
    return f"{_FACT_REASONS[check]}:{mw._slug(name)}"


def judge_rows(rows: list[list[Any]]) -> tuple[list[str], dict[str, Any]]:
    """(reasons, facts): the rows must be exactly ``EXPECTED_TABLE``, keyed by
    ``(check_name, subject)``: none missing, added or repeated, in any order
    (the SQL orders them for reading only)."""

    reasons: list[str] = []

    def fail(reason: str) -> None:
        if reason not in reasons:
            reasons.append(reason)

    observed: dict[tuple[str, str], str] = {}
    well_formed = True
    for row in rows:
        if not (len(row) == 3 and all(isinstance(value, str) for value in row)):
            well_formed = False
            continue
        key = (row[0], row[1])
        if key in observed:
            well_formed = False
        observed[key] = row[2]
    expected = {(check, subject): value for check, subject, value in EXPECTED_TABLE}
    if not well_formed or set(observed) != set(expected):
        fail("read_checks_mismatch")
    for (check, subject), value in expected.items():
        got = observed.get((check, subject))
        if got is None:
            fail("read_fact_missing:" + _fact_reason(check, subject))
        elif got != value:
            fail(_fact_reason(check, subject))

    def value_of(check: str, subject: str) -> str | None:
        return mw._safe_text(observed.get((check, subject)))

    facts: dict[str, Any] = {
        "billing_rollout_control": [
            {
                "environment": env,
                "state": value_of(CHECK_ROLLOUT_STATE, env),
                "topup_commercial_enabled": value_of(CHECK_TOPUP, env),
            }
            for env in ROLLOUT_ENVIRONMENTS
        ],
        "legacy_tables_absent": sum(
            observed.get((CHECK_LEGACY_TABLE, table)) == "absent"
            for table in LEGACY_TABLES
        ),
        "legacy_tables_expected": len(LEGACY_TABLES),
        "legacy_functions_absent": sum(
            observed.get((CHECK_LEGACY_FUNCTION, name)) == "absent"
            for name in LEGACY_FUNCTIONS
        ),
        "legacy_functions_expected": len(LEGACY_FUNCTIONS),
        "billing_subscriptions_rows": _count(observed.get((CHECK_SUBSCRIPTIONS, ""))),
        "billing_checkout_sessions_rows": _count(observed.get((CHECK_CHECKOUTS, ""))),
        "row_level_security_on_read_tables": _count(
            observed.get((CHECK_ROW_SECURITY, ""))
        ),
        "unexpected_rows": len(set(observed) - set(expected)),
    }
    return reasons, facts


def judge(lines: mw._ReadLines) -> tuple[list[str], dict[str, Any]]:
    """The window tool's envelope judge (exposure, foreign and malformed
    lines, sequence, end, begin labels and payload digest, row count,
    ``alembic_version`` 037), then the rows."""

    reasons, report, rows_judgeable = mw.judge_read_envelope(lines, READ_WINDOW, SPEC)
    report["facts"] = None
    if rows_judgeable:
        row_reasons, report["facts"] = judge_rows(lines.rows)
        reasons.extend(reason for reason in row_reasons if reason not in reasons)
    return reasons, report


def _rendered_match(
    params: Any, read_context: ReadContext, created: datetime | None
) -> int | None:
    """The attempt whose rendering (on the task's UTC day or the day before)
    is exactly the recorded request; None when it is not this tool's."""

    if not isinstance(params, dict) or created is None:
        return None
    comparable = mw._comparable_read_request(params)
    day = _utc_day(created)
    for candidate_day in (day, day - timedelta(days=1)):
        for attempt in range(1, mw.MAX_ATTEMPT + 1):
            rendered = build_request(
                read_context.network,
                read_context.target,
                attempt=attempt,
                day=candidate_day,
            )
            if (
                comparable == mw._comparable_read_request(rendered)
                and params.get("clientToken") == rendered["clientToken"]
            ):
                return attempt
    return None


def _family_revision(arn: str | None) -> str | None:
    return arn.rsplit("/", 1)[1] if isinstance(arn, str) else None


def _verdict_line(document: dict[str, Any]) -> str:
    facts = document["facts"]
    rows = "; ".join(
        f"{row['environment']} {row['state']}, topup_commercial_enabled "
        f"{row['topup_commercial_enabled']}"
        for row in facts["billing_rollout_control"]
    )
    times = document["task_times"]
    return (
        "Rollout readback verified (scripts/asaas_rollout_readback.py verify): "
        f"read task {document['task_arn'].rsplit('/', 1)[1]} on "
        f"{_family_revision(document['read_task_definition_arn'])}, startedBy "
        f"{STARTED_BY}, created {times['created_at']}, stopped "
        f"{times['stopped_at']}, container exit 0; one REPEATABLE READ, READ ONLY "
        f"transaction, payload sha256 {document['payload_sha256']}; service "
        f"{_family_revision(document['service_task_definition_arn'])}, deployment "
        f"{document['deployment_id']} COMPLETED. Read in that transaction: "
        "alembic_version 037. "
        f"billing_rollout_control {len(facts['billing_rollout_control'])} rows: "
        f"{rows}. Legacy objects of migration 037 absent: "
        f"{facts['legacy_tables_absent']} of {facts['legacy_tables_expected']} "
        f"tables, {facts['legacy_functions_absent']} of "
        f"{facts['legacy_functions_expected']} functions. billing_subscriptions "
        f"{facts['billing_subscriptions_rows']} rows. billing_checkout_sessions "
        f"{facts['billing_checkout_sessions_rows']} rows. Row level security on the "
        f"read tables: {facts['row_level_security_on_read_tables']}. This is the "
        "state at the time of the read, not its history: it does not show that "
        "no rollout transition ever happened (no rollout revision or receipt was "
        "read)."
    )


def verify(readback: mw.WindowReadback, context: Context) -> dict[str, Any]:
    """Classify the finished readback task. Pure; any anomaly stops."""

    verdict = mw._Verdict()
    record = readback.records.get(RECORD_KEY)
    _require(record is not None, "read_not_recorded")
    assert record is not None
    checks = hook._Checks()
    reads = mw._Reads(readback)
    read_context = _read_context(reads, context, checks)
    attempt = None
    task_arn = None
    exit_code = None
    report: dict[str, Any] = {}
    times: dict[str, str | None] = {"created_at": None, "stopped_at": None}
    if reads.missing:
        verdict.wait("readback_incomplete", list(reads.missing))
    elif read_context is None or checks.blocks:
        # The same service and read family checks as the plan, on what the
        # file records now: the claim "after the deploy" rests on them.
        verdict.fail(
            "read_context_unusable:" + ",".join(checks.blocks), "evidence_mismatch"
        )
    else:
        target = read_context.target
        params = record["params"]
        if isinstance(params, dict) and "propagateTags" in params:
            verdict.fail("run_task_request_propagate_tags_present", "evidence_mismatch")
        started = mw._started_task(record["response"])
        if started is None:
            verdict.fail("read_run_task_failed")
        else:
            task_arn = str(started["taskArn"])
            if (
                started.get("taskDefinitionArn", target.task_definition_arn)
                != target.task_definition_arn
                or started.get("startedBy", STARTED_BY) != STARTED_BY
            ):
                verdict.fail("read_run_task_response_invalid", "evidence_mismatch")
            described = reads.get(
                "ecs", "DescribeTasks", mw.describe_tasks_request([task_arn])
            )
            task = mw._started_task(described)
            # The day the token was rendered on: the task's creation (from
            # the RunTask response, else from DescribeTasks).
            created_hint = hook._timestamp(started.get("createdAt")) or (
                hook._timestamp(task.get("createdAt")) if task else None
            )
            if created_hint is not None or described is not None:
                attempt = _rendered_match(params, read_context, created_hint)
                if attempt is None:
                    verdict.fail(
                        "read_request_not_rendered_by_this_tool", "evidence_mismatch"
                    )
            if described is None:
                verdict.wait(
                    "read_task_not_described", mw._task_reads(task_arn, target.log)[:1]
                )
            elif task is None or task.get("taskArn") != task_arn:
                verdict.fail("read_task_describe_invalid", "evidence_mismatch")
            else:
                times = {
                    key: task.get(field) if isinstance(task.get(field), str) else None
                    for key, field in (
                        ("created_at", "createdAt"),
                        ("stopped_at", "stoppedAt"),
                    )
                }
                if (
                    task.get("taskDefinitionArn") != target.task_definition_arn
                    or task.get("startedBy") != STARTED_BY
                ):
                    verdict.fail("read_task_identity_mismatch", "evidence_mismatch")
                if mw.overrides_problem(task, target.container, read_command()):
                    verdict.fail("read_task_overrides_present", "evidence_mismatch")
                observed = mw.normalized_read_command(
                    mw._override_command(task, target.container)
                )
                if observed is None or observed != mw.normalized_read_command(
                    read_command()
                ):
                    verdict.fail("read_task_command_mismatch", "evidence_mismatch")
                created = hook._timestamp(task.get("createdAt"))
                age: float | None = None
                if created is None:
                    verdict.fail("read_task_timestamps_invalid")
                else:
                    deployed = read_context.deployment_created_at
                    if deployed is None or created <= deployed:
                        verdict.fail(
                            "read_task_not_after_the_deployment", "evidence_mismatch"
                        )
                    age = (readback.captured_at - created).total_seconds()
                    if not (
                        -mw.READBACK_MAX_FUTURE_SECONDS
                        <= age
                        <= READ_RECORD_MAX_AGE_SECONDS
                    ):
                        verdict.fail("read_task_not_recent", "evidence_mismatch")
                if task.get("lastStatus") != "STOPPED":
                    if age is not None and age > mw.READ_WATCH_DEADLINE_SECONDS:
                        verdict.fail("read_watch_deadline_exceeded", "watch_deadline")
                    elif age is not None:
                        verdict.wait(
                            "read_task_not_stopped",
                            mw._task_reads(task_arn, target.log),
                        )
                else:
                    exit_code = mw._exit_code(task, target.container)
                    messages = mw._read_chain(
                        readback, verdict, task, target.log, task_arn
                    )
                    if messages is not None:
                        reasons, report = judge(mw.parse_read_lines(messages))
                        for reason in reasons:
                            verdict.fail(
                                reason,
                                (
                                    "exposure_suspected"
                                    if reason == "read_log_exposure_suspected"
                                    else "read_not_ok"
                                ),
                            )
                        if exit_code != 0:
                            verdict.fail("read_exit_code_not_zero")
    outcome = verdict.outcome(OUTCOME_VERIFIED)
    if outcome == mw.OUTCOME_STOPPED:
        outcome = OUTCOME_STOPPED
    document: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "outcome": outcome,
        "alert": outcome == OUTCOME_STOPPED,
        "alert_kinds": sorted(verdict.kinds) if verdict.reasons else [],
        "reasons": verdict.reasons if verdict.reasons else verdict.in_progress,
        "on_failure": FAILURE_POLICY,
        "task_arn": task_arn,
        "attempt": attempt,
        "container_exit_code": exit_code,
        "task_times": times,
        "service_task_definition_arn": context.service_task_definition,
        "deployment_id": read_context.deployment_id if read_context else None,
        "read_task_definition_arn": context.read_task_definition,
        "started_by": STARTED_BY,
        "payload_sha256": payload_sha256(),
        "expected_alembic_version": SPEC.expected_version,
        "alembic_version": report.get("alembic_version"),
        "facts": report.get("facts"),
        "error_class": report.get("error_class"),
        "sqlstate": report.get("sqlstate"),
        "next_requests": (
            verdict.next_requests if outcome == OUTCOME_IN_PROGRESS else []
        ),
    }
    if outcome == OUTCOME_VERIFIED:
        document["verdict"] = _verdict_line(document)
    elif outcome == OUTCOME_STOPPED:
        document["verdict"] = "Rollout readback stopped: " + ", ".join(verdict.reasons)
    else:
        document["verdict"] = None
    return document


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _clock() -> datetime:
    """The system clock: the only time source of the CLI (no ``--now``).
    Tests replace it."""

    return datetime.now(timezone.utc)


def _parser() -> mw._Parser:
    parser = mw._Parser(prog="asaas_rollout_readback.py", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("plan", "render", "verify"):
        sub = subparsers.add_parser(name)
        add = sub.add_argument
        add("--readback-file", type=Path, required=True, action=mw._Once)
        add("--service-task-definition", required=True, action=mw._Once)
        add("--read-task-definition", required=True, action=mw._Once)
        if name == "render":
            add("--attempt", type=mw._attempt, default=1, action=mw._Once)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
        now = _clock()
        _require(now.tzinfo is not None, "now_invalid")
        context = make_context(
            service_task_definition=args.service_task_definition,
            read_task_definition=args.read_task_definition,
        )
        readback = load_readback(args.readback_file)
        if args.command in ("plan", "render"):
            document, read_context = evaluate_plan(readback, context, now=now)
            if args.command == "plan":
                mw._emit(document)
                return 0 if document["status"] == "ready" else 2
            _require(
                read_context is not None,
                "plan_blocked:"
                + ",".join(document["blocks"] or ["readback_incomplete"]),
            )
            assert read_context is not None
            mw._emit(
                build_request(
                    read_context.network,
                    read_context.target,
                    attempt=args.attempt,
                    day=_utc_day(now),
                )
            )
            return 0
        result = verify(readback, context)
        mw._emit(result)
        return EXIT_CODES[result["outcome"]]
    except hook.HookError as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
