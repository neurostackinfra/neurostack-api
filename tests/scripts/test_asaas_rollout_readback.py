"""Post-deploy rollout readback: pure planner, renderer and verifier.

Every test runs on recorded readback files built here; nothing reaches AWS or
a database. The read program output the verifier classifies is real:
``tests/fixtures/asaas_rollout_readback/read_runs.json`` holds what the
rendered program printed against a disposable PostgreSQL 16 cluster (at 036,
at 037, row-level security on, one row opened to ``general``).
"""

from __future__ import annotations

import base64
import copy
import gzip
import hashlib
import json
import re
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scripts import asaas_deploy_hook as hook
from scripts import asaas_migration_window as mw
from scripts import asaas_rollout_readback as rr
from scripts import verify_asaas_producer_image_rollout as rollout

ROOT = Path(__file__).resolve().parents[2]
SQL = ROOT / "scripts" / "asaas_rollout_readback.sql"
FIXTURE = ROOT / "tests" / "fixtures" / "asaas_rollout_readback" / "read_runs.json"
RUNBOOK = ROOT / "docs" / "runbooks" / "asaas-deployment.md"

ACCOUNT = "388460142431"
REGISTRY = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com"
TD_PREFIX = f"arn:aws:ecs:us-east-1:{ACCOUNT}:task-definition/"
SERVICE_TD = TD_PREFIX + "neurostack-staging-app:11"
SERVICE_TD_10 = TD_PREFIX + "neurostack-staging-app:10"
READ_TD = TD_PREFIX + "neurostack-staging-consulta:28"
BRIDGE_IMAGE = f"{REGISTRY}/neurostack-staging@sha256:" + "2" * 64
SECRET_PREFIX = f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:neurostack/staging/"
APP_DB_ARN = SECRET_PREFIX + "database-url-fyOYuj"
MIGRATIONS_DB_ARN = SECRET_PREFIX + "database-url-migrations-Mg7x2Q"
SUBNETS = ["subnet-0a1b2c3d4e5f60718", "subnet-0f1e2d3c4b5a69788"]
SECURITY_GROUPS = ["sg-0123456789abcdef0"]
TASK_PREFIX = f"arn:aws:ecs:us-east-1:{ACCOUNT}:task/neurostack-staging/"
READ_TASK_ID = "7" * 32
READ_TASK = TASK_PREFIX + READ_TASK_ID
OTHER_TASK = TASK_PREFIX + "9" * 32
DEPLOYMENT_ID = "ecs-svc/5006169001774609810"
DEPLOYMENT_CREATED = "2026-10-25T15:00:00+00:00"
CAPTURED_PLAN = "2026-10-25T16:00:00Z"
NOW_DT = datetime(2026, 10, 25, 16, 1, tzinfo=timezone.utc)
TASK_CREATED = "2026-10-25T16:02:00+00:00"
TASK_STOPPED = "2026-10-25T16:03:30+00:00"
CAPTURED_VERIFY = "2026-10-25T16:05:00Z"
READ_LOG = hook.LogTarget(
    group="/ecs/neurostack-staging/migrations",
    stream_prefix="consulta",
    container="consulta",
)
# Built by concatenation so repository secret scanners never see a literal.
LEAKED_URL = "postgresql+asyncpg" + "://neurostack_app:pw@db.internal:5432/app"
LEAKED_HOST = "neurostack-staging-db.abc123." + "us-east-1.rds.amazonaws.com"
LEAKS = [
    LEAKED_URL,
    LEAKED_HOST,
    "pass" + "word=hunter2",
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "DATABASE" + "_URL=whatever",
    "10.0.12.34",
]

RUNS: dict[str, Any] = json.loads(FIXTURE.read_text(encoding="utf-8"))["runs"]


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CLI's only time source is rr._clock (no --now flag): pin it."""

    monkeypatch.setattr(rr, "_clock", lambda: NOW_DT)


# ---------------------------------------------------------------------------
# Recorded responses
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
                "createdAt": DEPLOYMENT_CREATED,
                "updatedAt": "2026-10-25T15:06:57+00:00",
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


def _read_container(**changes: Any) -> dict[str, Any]:
    """``neurostack-staging-consulta:28`` as registered on 2026-10-22."""

    container: dict[str, Any] = {
        "name": "consulta",
        "image": BRIDGE_IMAGE,
        "essential": True,
        "command": [
            "python",
            "-c",
            "raise SystemExit('read revision: a command override is required')",
        ],
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


def _read_definition(
    container: dict[str, Any] | None = None, **changes: Any
) -> dict[str, Any]:
    definition: dict[str, Any] = {
        "taskDefinitionArn": READ_TD,
        "family": "neurostack-staging-consulta",
        "revision": 28,
        "status": "ACTIVE",
        "networkMode": "awsvpc",
        "requiresCompatibilities": ["FARGATE"],
        "cpu": "512",
        "memory": "1024",
        "executionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/neurostack-staging-ecs-execution",
        "containerDefinitions": [container or _read_container()],
    }
    definition.update(changes)
    return {"taskDefinition": definition}


def _events(*messages: str, token: str = "f/1") -> dict[str, Any]:
    return {
        "events": [
            {"timestamp": 1792951350000 + i, "message": m, "ingestionTime": 1}
            for i, m in enumerate(messages)
        ],
        "nextForwardToken": token,
        "nextBackwardToken": "b/1",
    }


def _lines(name: str = "rollout_at_037") -> list[str]:
    return list(RUNS[name]["lines"])


def _documents(lines: list[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in lines]


def _dump(documents: list[dict[str, Any]], *, renumber: bool = True) -> list[str]:
    """Lines again; renumbered (seq, the end line's row count) so that only
    the change under test differs from a well-formed read."""

    if renumber:
        rows = sum(1 for document in documents if document["kind"] == "row")
        for seq, document in enumerate(documents):
            document["seq"] = seq
            if document["kind"] == "end" and document.get("status") == "ok":
                document["rows"] = rows
    return [json.dumps(document, separators=(",", ":")) for document in documents]


def _with_value(lines: list[str], check: str, subject: str, value: str) -> list[str]:
    documents = _documents(lines)
    hits = 0
    for document in documents:
        if document["kind"] == "row" and document["values"][:2] == [check, subject]:
            document["values"][2] = value
            hits += 1
    assert hits == 1, (check, subject)
    return _dump(documents)


def _without_row(lines: list[str], check: str, subject: str) -> list[str]:
    documents = [
        document
        for document in _documents(lines)
        if not (
            document["kind"] == "row" and document["values"][:2] == [check, subject]
        )
    ]
    return _dump(documents)


class World:
    """A recorded readback file, one response per exact request."""

    def __init__(
        self, *, captured_at: str = CAPTURED_PLAN, now: datetime = NOW_DT
    ) -> None:
        self.reads: dict[str, dict[str, Any]] = {}
        self.records: dict[str, Any] = {}
        self.extra: dict[str, Any] = {}
        self.captured_at = captured_at
        self.now = now
        self.service_td = SERVICE_TD
        self.read_td = READ_TD
        self.put(
            "ecs",
            "DescribeServices",
            mw.describe_services_request(),
            _service(SERVICE_TD),
        )
        self.put(
            "ecs",
            "DescribeTaskDefinition",
            mw.describe_task_definition_request(READ_TD),
            _read_definition(),
        )
        for params in self.listings():
            self.put("ecs", "ListTasks", params, {"taskArns": []})

    @staticmethod
    def listings() -> list[dict[str, Any]]:
        return [
            mw.list_started_by_request(rr.STARTED_BY),
            mw.list_family_tasks_request(mw.READ_FAMILY),
            mw.list_family_tasks_request(mw.MIGRATIONS_FAMILY),
        ]

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

    def document(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schema_version": rr.READBACK_SCHEMA,
            "account_id": ACCOUNT,
            "region": "us-east-1",
            "captured_at_utc": self.captured_at,
            "reads": copy.deepcopy(list(self.reads.values())),
        }
        document.update(copy.deepcopy(self.records))
        document.update(copy.deepcopy(self.extra))
        return document

    def write(self, path: Path) -> Path:
        path.write_text(json.dumps(self.document()), encoding="utf-8")
        return path

    def readback(self, tmp_path: Path, name: str = "readback.json") -> Any:
        return rr.load_readback(self.write(tmp_path / name))

    def context(self) -> rr.Context:
        return rr.make_context(
            service_task_definition=self.service_td,
            read_task_definition=self.read_td,
        )

    def args(self, tmp_path: Path, name: str = "readback.json") -> list[str]:
        return [
            "--readback-file",
            str(self.write(tmp_path / name)),
            "--service-task-definition",
            self.service_td,
            "--read-task-definition",
            self.read_td,
        ]

    def plan(self, tmp_path: Path) -> dict[str, Any]:
        document, _context = rr.evaluate_plan(
            self.readback(tmp_path, "plan.json"), self.context(), now=self.now
        )
        return document

    def render(self, tmp_path: Path, *, attempt: int = 1) -> dict[str, Any]:
        document, context = rr.evaluate_plan(
            self.readback(tmp_path, "render.json"), self.context(), now=self.now
        )
        assert context is not None, document
        return rr.build_request(
            context.network,
            context.target,
            attempt=attempt,
            day=self.now.astimezone(timezone.utc).date(),
        )

    def put_pages(self, pages: list[list[str]], *, terminal: bool = True) -> None:
        token: str | None = None
        for index, page in enumerate(pages, start=1):
            self.put(
                "logs",
                "GetLogEvents",
                hook.log_events_request(READ_LOG, READ_TASK_ID, token),
                _events(*page, token=f"f/{index}"),
            )
            token = f"f/{index}"
        if terminal:
            self.put(
                "logs",
                "GetLogEvents",
                hook.log_events_request(READ_LOG, READ_TASK_ID, token),
                _events(token=str(token)),
            )

    def described(
        self,
        command: list[str],
        *,
        exit_code: Any = 0,
        created: str = TASK_CREATED,
        stopped: str | None = TASK_STOPPED,
        status: str = "STOPPED",
        **changes: Any,
    ) -> dict[str, Any]:
        container: dict[str, Any] = {"name": "consulta", "lastStatus": status}
        if exit_code is not None:
            container["exitCode"] = exit_code
        task: dict[str, Any] = {
            "taskArn": READ_TASK,
            "taskDefinitionArn": READ_TD,
            "startedBy": rr.STARTED_BY,
            "lastStatus": status,
            "desiredStatus": "STOPPED" if status == "STOPPED" else "RUNNING",
            "createdAt": created,
            "containers": [container],
            "overrides": {
                "containerOverrides": [{"name": "consulta", "command": command}],
                "inferenceAcceleratorOverrides": [],
            },
        }
        if stopped is not None and status == "STOPPED":
            task["stoppedAt"] = stopped
            task["stopCode"] = "EssentialContainerExited"
        task.update(changes)
        return task

    def record_read(
        self,
        tmp_path: Path,
        lines: list[str] | None = None,
        *,
        params: dict[str, Any] | None = None,
        response: dict[str, Any] | None = None,
        task: dict[str, Any] | None = None,
        pages: list[list[str]] | None = None,
        created: str = TASK_CREATED,
        captured: str = CAPTURED_VERIFY,
        describe: bool = True,
        logs: bool = True,
        **described: Any,
    ) -> dict[str, Any]:
        rendered = params or self.render(tmp_path)
        self.records["read"] = {
            "params": rendered,
            "response": response
            or {
                "tasks": [
                    {
                        "taskArn": READ_TASK,
                        "taskDefinitionArn": READ_TD,
                        "startedBy": rr.STARTED_BY,
                        "lastStatus": "PROVISIONING",
                        "createdAt": created,
                    }
                ],
                "failures": [],
            },
        }
        command = rendered["overrides"]["containerOverrides"][0]["command"]
        if describe:
            self.put(
                "ecs",
                "DescribeTasks",
                mw.describe_tasks_request([READ_TASK]),
                {
                    "tasks": [
                        task or self.described(command, created=created, **described)
                    ],
                    "failures": [],
                },
            )
        if logs:
            self.put_pages(pages if pages is not None else [lines or _lines()])
        self.captured_at = captured
        return rendered

    def verify(self, tmp_path: Path) -> dict[str, Any]:
        return rr.verify(self.readback(tmp_path, "verify.json"), self.context())


def recorded(tmp_path: Path, lines: list[str] | None = None, **record: Any) -> World:
    world = World()
    world.record_read(tmp_path, lines, **record)
    return world


def run_cli(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    code = rr.main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _stopped_with(result: dict[str, Any], *reasons: str) -> None:
    assert result["outcome"] == rr.OUTCOME_STOPPED, result
    assert result["alert"] is True
    for reason in reasons:
        assert reason in result["reasons"], result["reasons"]
    assert result["verdict"].startswith("Rollout readback stopped: ")


# ---------------------------------------------------------------------------
# The read: SQL, program, labels
# ---------------------------------------------------------------------------

# Words that would make a statement write, lock, change the session or run
# anything but a plain read; none may appear in the readback's SQL.
_FORBIDDEN_SQL_WORDS = frozenset("""
    insert update delete merge upsert create alter drop truncate grant revoke
    copy call do execute prepare deallocate lock vacuum analyze reindex cluster
    refresh comment security label notify listen unlisten discard reset set
    begin start commit rollback savepoint release abort into returning for
    nowait skip load import
    """.split())
# The only functions it may call: lookups by name and a count.
_ALLOWED_SQL_FUNCTIONS = frozenset(
    {"unnest", "to_regclass", "to_regprocedure", "count"}
)
# Keywords a parenthesis may follow without being a call.
_SQL_KEYWORDS_BEFORE_PARENTHESIS = frozenset(
    {"from", "where", "in", "array", "and", "or", "not", "exists", "on", "values"}
)


def _sql_tokens(sql: str) -> list[str]:
    text = re.sub(r"--[^\n]*", " ", sql)
    text = re.sub(r"'(?:[^']|'')*'", " '' ", text)
    text = re.sub(r'"[^"]*"', ' "" ', text)
    return re.findall(r"[A-Za-z_][A-Za-z0-9_]*|::|\S", text)


def test_the_rendered_program_is_select_only_and_read_only(tmp_path: Path) -> None:
    request = World().render(tmp_path)
    (override,) = request["overrides"]["containerOverrides"]
    command = override["command"]
    assert command[:2] == ["python", "-c"]
    program = command[2]
    # The program is the window tool's, byte for byte, behind its header.
    assert program.endswith(mw._READ_PROGRAM_BODY)
    header = mw._READ_HEADER.match(program)
    assert header is not None
    assert program[header.end() :] == mw._READ_PROGRAM_BODY
    assert (header.group("window"), header.group("phase")) == ("037", "rollout")
    assert header.group("settings") == (
        "SET LOCAL lock_timeout='3s';SET LOCAL statement_timeout='60s';"
        "SET LOCAL search_path=public"
    )
    # Read only twice over (the session default and the transaction), one
    # REPEATABLE READ transaction, always ended by a rollback, never committed.
    assert 's["default_transaction_read_only"]="on"' in program
    assert 't=c.transaction(isolation="repeatable_read",readonly=True)' in program
    assert "await t.rollback()" in program
    assert "commit" not in program.lower()
    assert "alembic" not in program.lower()
    # The payload is exactly the SQL file and the revision read, by digest.
    payload = mw._decompressed(header.group("payload"))
    assert payload == rr.read_payload()
    assert header.group("sha") == hashlib.sha256(payload).hexdigest()
    assert json.loads(payload) == {
        "sql": SQL.read_text(encoding="utf-8"),
        "version": "SELECT version_num FROM public.alembic_version",
    }
    for sql in json.loads(payload).values():
        tokens = _sql_tokens(sql)
        words = [token.lower() for token in tokens if re.match(r"[A-Za-z_]", token)]
        assert words[0] == "select", sql
        assert not set(words) & _FORBIDDEN_SQL_WORDS, set(words) & _FORBIDDEN_SQL_WORDS
        # One statement: no semicolon but an optional final one.
        assert ";" not in tokens[:-1]
        calls = {
            tokens[i].lower()
            for i in range(1, len(tokens) - 1)
            if tokens[i + 1] == "("
            and re.match(r"[A-Za-z_]", tokens[i])
            and tokens[i - 1].lower() != "as"
            and tokens[i].lower() not in _SQL_KEYWORDS_BEFORE_PARENTHESIS
        }
        assert calls <= _ALLOWED_SQL_FUNCTIONS, calls


def test_the_token_scan_would_see_a_write() -> None:
    """The scan above is not vacuous: each forbidden shape is caught."""

    for sql in (
        "SELECT 1; DELETE FROM public.billing_rollout_control",
        "SELECT * INTO public.copy FROM public.billing_rollout_control",
        "SELECT state FROM public.billing_rollout_control FOR UPDATE",
        "UPDATE public.billing_rollout_control SET state = 'general'",
    ):
        tokens = _sql_tokens(sql)
        words = {token.lower() for token in tokens if re.match(r"[A-Za-z_]", token)}
        assert words & _FORBIDDEN_SQL_WORDS or ";" in tokens[:-1], sql
    tokens = _sql_tokens("SELECT pg_advisory_lock(1)")
    assert "pg_advisory_lock" not in _ALLOWED_SQL_FUNCTIONS and "(" in tokens


def test_the_legacy_names_are_exactly_what_migration_037_drops() -> None:
    from scripts.migration_037_readback import load_migration

    migration = load_migration()
    assert rr.LEGACY_TABLES == tuple(
        sorted(f"public.{table}" for table in migration.DROPPED_TABLES)
    )
    assert rr.LEGACY_FUNCTIONS == tuple(
        f"public.{function}" for function in migration.DROPPED_FUNCTIONS
    )
    sql = SQL.read_text(encoding="utf-8")
    body = re.sub(r"--[^\n]*", "", sql)
    literals = re.findall(r"'(public\.[^']+)'", body)
    tables, functions = literals[:4], literals[4:14]
    assert tables == list(rr.LEGACY_TABLES)
    assert functions == list(rr.LEGACY_FUNCTIONS)
    # The remaining literals are the three RLS-checked tables, nothing else:
    # the three tables the read counts or lists.
    assert literals[14:] == [
        "public.billing_rollout_control",
        "public.billing_subscriptions",
        "public.billing_checkout_sessions",
    ]
    for table in literals[14:]:
        assert f"FROM {table}\n" in body


def test_the_expected_table_is_the_criterion() -> None:
    # 4 states + 4 top-up flags + 4 tables + 10 functions + 3 counts.
    assert len(rr.EXPECTED_TABLE) == 4 + 4 + 4 + 10 + 3 == 25
    assert len({row[:2] for row in rr.EXPECTED_TABLE}) == 25
    assert [row for row in rr.EXPECTED_TABLE if row[1] == ""] == [
        ("billing_subscriptions rows", "", "0"),
        ("billing_checkout_sessions rows", "", "0"),
        ("row level security on read tables", "", "0"),
    ]
    rollout_rows = [row for row in rr.EXPECTED_TABLE if row[0] == "rollout state"]
    assert rollout_rows == [
        ("rollout state", env, "disabled")
        for env in ("development", "production", "staging", "test")
    ]
    assert [row for row in rr.EXPECTED_TABLE if row[0].endswith("enabled")] == [
        ("rollout topup_commercial_enabled", env, "false")
        for env in ("development", "production", "staging", "test")
    ]
    assert rr.SPEC.expected_version == "037" and rr.SPEC.role == "app"


def test_the_fixture_was_captured_with_this_payload() -> None:
    """Changing the SQL without recapturing the fixture fails here."""

    for name, run in RUNS.items():
        begin = json.loads(run["lines"][0])
        assert begin["payload_sha256"] == rr.payload_sha256(), name
        assert (begin["window"], begin["phase"]) == ("037", "rollout")


def test_the_rollout_label_is_recognized_only_with_window_037() -> None:
    program = rr.read_program()
    assert mw.normalized_read_command(["python", "-c", program]) is not None
    for window in ("035", "036"):
        relabelled = program.replace("W='037'", f"W='{window}'", 1)
        assert mw.normalized_read_command(["python", "-c", relabelled]) is None
    unknown = program.replace("P='rollout'", "P='other'", 1)
    assert mw.normalized_read_command(["python", "-c", unknown]) is None
    # The windows' own programs are still recognized, unchanged.
    for window in mw.WINDOWS.values():
        for spec in (window.pre_read, window.post_read):
            command = ["python", "-c", mw.read_program(window, spec)]
            assert mw.normalized_read_command(command) is not None


def test_a_tampered_payload_is_not_the_program() -> None:
    program = rr.read_program()
    header = mw._READ_HEADER.match(program)
    assert header is not None
    tampered = base64.b64encode(
        gzip.compress(rr.read_payload().replace(b"disabled", b"general"), mtime=0)
    ).decode()
    other = program.replace(header.group("payload"), tampered)
    assert mw.normalized_read_command(["python", "-c", other]) is None


# ---------------------------------------------------------------------------
# Readback file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda d: d.__setitem__("schema_version", mw.READBACK_SCHEMA), "shape"),
        (lambda d: d.__setitem__("account_id", "111111111111"), "shape"),
        (lambda d: d.__setitem__("region", "us-east-2"), "shape"),
        (lambda d: d.__setitem__("window", "037"), "shape"),
        (lambda d: d.__setitem__("captured_at_utc", "2026-10-25T16:00:00"), "shape"),
        (
            lambda d: d["reads"].append(
                {
                    "service": "rds",
                    "operation": "DescribeDBSnapshots",
                    "params": {},
                    "response": {},
                }
            ),
            "operation_not_allowed",
        ),
        (lambda d: d["reads"].append(copy.deepcopy(d["reads"][0])), "duplicate_read"),
        (
            lambda d: d["reads"][0]["response"].__setitem__("SecretString", "x"),
            "secret_material",
        ),
        (lambda d: d.__setitem__("read", {"params": {}}), "read_record_invalid"),
    ],
)
def test_the_readback_file_is_strict(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None], reason: str
) -> None:
    document = World().document()
    mutate(document)
    path = tmp_path / "readback.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(hook.HookError, match=reason):
        rr.load_readback(path)


@pytest.mark.parametrize("field", ["SecretString", "SecretBinary", "RandomPassword"])
def test_secret_material_in_the_recorded_read_is_refused(
    tmp_path: Path, field: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """M12: the recorded RunTask response is scanned like every read."""

    world = recorded(tmp_path)
    world.records["read"]["response"]["tasks"][0]["overrides"] = {
        "containerOverrides": [{"name": "consulta", field: "x"}]
    }
    with pytest.raises(hook.HookError, match="secret_material_in_readback"):
        world.readback(tmp_path)
    code, out, err = run_cli(["verify", *world.args(tmp_path)], capsys)
    assert (code, out, err.strip()) == (2, "", "blocked: secret_material_in_readback")


def test_a_dsn_in_the_recorded_read_is_never_echoed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = recorded(tmp_path)
    world.records["read"]["response"]["tasks"][0]["overrides"] = {
        "containerOverrides": [
            {
                "name": "consulta",
                "environment": [{"name": "DATABASE_URL", "value": LEAKED_URL}],
            }
        ]
    }
    world.records["read"]["params"]["overrides"]["containerOverrides"][0][
        "environment"
    ] = [{"name": "DATABASE_URL", "value": LEAKED_URL}]
    code, out, err = run_cli(["verify", *world.args(tmp_path)], capsys)
    assert code == 4 and err == ""
    assert LEAKED_URL not in out and "db.internal" not in out
    assert "read_request_not_rendered_by_this_tool" in json.loads(out)["reasons"]


def test_arguments_are_validated() -> None:
    with pytest.raises(mw.WindowError, match="service_task_definition_invalid"):
        rr.make_context(service_task_definition=READ_TD, read_task_definition=READ_TD)
    with pytest.raises(mw.WindowError, match="read_task_definition_invalid"):
        rr.make_context(
            service_task_definition=SERVICE_TD,
            read_task_definition=TD_PREFIX + "neurostack-staging-migrations:7",
        )


def test_each_option_is_given_once_and_there_is_no_now(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = World().args(tmp_path)
    code, out, err = run_cli(["plan", *args, "--read-task-definition", READ_TD], capsys)
    assert (code, out, err.strip()) == (2, "", "blocked: usage")
    code, out, err = run_cli(["plan", *args, "--now", CAPTURED_PLAN], capsys)
    assert (code, err.strip()) == (2, "blocked: usage")


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def test_plan_is_ready_on_the_recorded_reads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    document = world.plan(tmp_path)
    assert document["status"] == "ready", document["blocks"]
    assert document["blocks"] == [] and document["next_requests"] == []
    for name in (
        "readback_fresh",
        "no_read_recorded",
        "service_identity",
        "deployment_completed",
        "deployment_on_expected_task_definition",
        "service_on_expected_task_definition",
        "service_steady",
        "network_private_awsvpc",
        "deployment_created_at",
        "read_task_definition_identity",
        "read_task_definition_active",
        "read_database_secret_is_the_app_role",
        "read_family_names_no_migration_tool",
        "read_image_in_registry",
        "no_readback_task_running",
        "no_read_family_task_running",
        "no_migrations_task_running",
    ):
        assert name in document["checks"], name
    assert document["deployment_id"] == DEPLOYMENT_ID
    assert document["run_task"]["started_by"] == "neurostack-rollout-readback"
    assert document["run_task"]["payload_sha256"] == rr.payload_sha256()
    assert document["read"]["sql_file"] == "scripts/asaas_rollout_readback.sql"
    assert document["read"]["expected_alembic_version"] == "037"
    code, out, err = run_cli(["plan", *world.args(tmp_path)], capsys)
    assert code == 0 and err == ""
    assert json.loads(out)["status"] == "ready"


def test_plan_lists_the_missing_reads(tmp_path: Path) -> None:
    world = World()
    world.drop("ecs", "DescribeServices", mw.describe_services_request())
    world.drop("ecs", "ListTasks", mw.list_family_tasks_request(mw.MIGRATIONS_FAMILY))
    document = world.plan(tmp_path)
    assert document["status"] == "incomplete"
    assert {
        (row["operation"], json.dumps(row["params"], sort_keys=True))
        for row in document["next_requests"]
    } == {
        (
            "DescribeServices",
            json.dumps(mw.describe_services_request(), sort_keys=True),
        ),
        (
            "ListTasks",
            json.dumps(
                mw.list_family_tasks_request(mw.MIGRATIONS_FAMILY), sort_keys=True
            ),
        ),
    }


@pytest.mark.parametrize(
    "captured,reason",
    [
        ("2026-10-25T15:50:59Z", "readback_stale"),
        ("2026-10-25T16:02:01Z", "readback_captured_in_future"),
    ],
)
def test_plan_blocks_a_stale_readback(
    tmp_path: Path, captured: str, reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World(captured_at=captured)
    document = world.plan(tmp_path)
    assert document["status"] == "blocked" and reason in document["blocks"]
    code, out, err = run_cli(["render", *world.args(tmp_path)], capsys)
    assert code == 2 and out == "" and reason in err


def test_plan_blocks_the_wrong_service_revision(tmp_path: Path) -> None:
    world = World()
    world.put(
        "ecs",
        "DescribeServices",
        mw.describe_services_request(),
        _service(SERVICE_TD_10),
    )
    document = world.plan(tmp_path)
    assert document["status"] == "blocked"
    assert "deployment_task_definition_mismatch" in document["blocks"]
    assert "service_task_definition_mismatch" in document["blocks"]


@pytest.mark.parametrize(
    "changes,reason",
    [
        (
            {"rolloutState": "IN_PROGRESS"},
            "deployment_not_completed",
        ),
        ({"createdAt": None}, "deployment_created_at_invalid"),
    ],
)
def test_plan_blocks_a_deployment_not_completed(
    tmp_path: Path, changes: dict[str, Any], reason: str
) -> None:
    world = World()
    response = _service(SERVICE_TD)
    response["services"][0]["deployments"][0].update(changes)
    world.put("ecs", "DescribeServices", mw.describe_services_request(), response)
    document = world.plan(tmp_path)
    assert document["status"] == "blocked" and reason in document["blocks"]


def test_plan_blocks_a_second_deployment(tmp_path: Path) -> None:
    world = World()
    response = _service(SERVICE_TD)
    second = dict(response["services"][0]["deployments"][0], status="ACTIVE")
    second["taskDefinition"] = SERVICE_TD_10
    response["services"][0]["deployments"].append(second)
    world.put("ecs", "DescribeServices", mw.describe_services_request(), response)
    document = world.plan(tmp_path)
    assert "service_has_concurrent_deployments" in document["blocks"]


@pytest.mark.parametrize(
    "definition,reason",
    [
        (_read_definition(status="INACTIVE"), "read_task_definition_not_active"),
        (
            _read_definition(_read_container(entryPoint=["sh", "-c"])),
            "read_container_entry_point_unexpected",
        ),
        (
            _read_definition(
                _read_container(
                    secrets=[{"name": "DATABASE_URL", "valueFrom": MIGRATIONS_DB_ARN}]
                )
            ),
            "read_database_secret_mismatch",
        ),
        (
            _read_definition(
                _read_container(
                    environment=[{"name": "DATABASE_URL", "value": "x"}],
                )
            ),
            "read_database_secret_mismatch",
        ),
        (
            _read_definition(
                _read_container(image="public.ecr.aws/docker/postgres:16")
            ),
            "read_image_not_an_application_image",
        ),
        (
            _read_definition(_read_container(command=["alembic", "upgrade", "037"])),
            "read_family_names_the_migration_tool",
        ),
        (
            _read_definition(
                _read_container(
                    logConfiguration={"logDriver": "awslogs", "options": {}}
                )
            ),
            "read_log_configuration_unusable",
        ),
        (
            _read_definition(networkMode="bridge"),
            "read_task_definition_not_fargate_awsvpc",
        ),
        (
            {
                "taskDefinition": dict(
                    _read_definition()["taskDefinition"],
                    family="neurostack-staging-migrations",
                )
            },
            "read_task_definition_identity_mismatch",
        ),
    ],
)
def test_plan_blocks_read_family_drift(
    tmp_path: Path, definition: dict[str, Any], reason: str
) -> None:
    world = World()
    world.put(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(READ_TD),
        definition,
    )
    document = world.plan(tmp_path)
    assert document["status"] == "blocked" and reason in document["blocks"], document[
        "blocks"
    ]


def test_plan_blocks_a_second_container_on_the_read_family(tmp_path: Path) -> None:
    world = World()
    definition = _read_definition()
    definition["taskDefinition"]["containerDefinitions"].append(
        _read_container(name="sidecar")
    )
    world.put(
        "ecs",
        "DescribeTaskDefinition",
        mw.describe_task_definition_request(READ_TD),
        definition,
    )
    document = world.plan(tmp_path)
    assert "read_task_definition_container_count_invalid" in document["blocks"]


@pytest.mark.parametrize(
    "index,reason",
    [
        (0, "readback_task_running"),
        (1, "read_family_task_running"),
        (2, "migrations_task_running"),
    ],
)
def test_plan_blocks_while_a_task_runs(tmp_path: Path, index: int, reason: str) -> None:
    world = World()
    world.put("ecs", "ListTasks", World.listings()[index], {"taskArns": [OTHER_TASK]})
    document = world.plan(tmp_path)
    assert document["status"] == "blocked" and document["blocks"] == [reason]


def test_plan_blocks_an_incomplete_listing(tmp_path: Path) -> None:
    world = World()
    world.put(
        "ecs",
        "ListTasks",
        World.listings()[1],
        {"taskArns": [], "nextToken": "more"},
    )
    assert "read_family_task_listing_incomplete" in world.plan(tmp_path)["blocks"]


def test_plan_blocks_once_a_read_is_recorded(tmp_path: Path) -> None:
    world = World()
    world.record_read(tmp_path, captured=CAPTURED_PLAN)
    document = world.plan(tmp_path)
    assert document["status"] == "blocked"
    assert "read_already_recorded" in document["blocks"]


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def test_render_prints_the_hardened_run_task(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    code, out, err = run_cli(["render", *world.args(tmp_path)], capsys)
    assert code == 0 and err == ""
    request = json.loads(out)
    assert request == world.render(tmp_path)
    assert set(request) == {
        "cluster",
        "taskDefinition",
        "count",
        "launchType",
        "networkConfiguration",
        "startedBy",
        "enableECSManagedTags",
        "enableExecuteCommand",
        "overrides",
        "platformVersion",
        "clientToken",
    }
    assert request["cluster"] == "neurostack-staging"
    assert request["taskDefinition"] == READ_TD
    assert request["count"] == 1 and request["launchType"] == "FARGATE"
    assert request["networkConfiguration"] == {
        "awsvpcConfiguration": {
            "subnets": SUBNETS,
            "securityGroups": SECURITY_GROUPS,
            "assignPublicIp": "DISABLED",
        }
    }
    assert request["startedBy"] == "neurostack-rollout-readback"
    assert request["enableECSManagedTags"] is False
    assert request["enableExecuteCommand"] is False
    assert request["platformVersion"] == "LATEST"
    assert "propagateTags" not in request
    (override,) = request["overrides"]["containerOverrides"]
    assert set(request["overrides"]) == {"containerOverrides"}
    assert set(override) == {"name", "command"} and override["name"] == "consulta"
    assert mw.normalized_read_command(
        override["command"]
    ) == mw.normalized_read_command(rr.read_command())
    assert re.fullmatch(r"rollout-readback-[0-9a-f]{32}", request["clientToken"])
    size = len(json.dumps(request["overrides"], separators=(",", ":")))
    assert size <= mw.OVERRIDES_BUDGET_CHARS


def test_the_client_token_changes_with_attempt_and_day(tmp_path: Path) -> None:
    world = World()
    first = world.render(tmp_path)
    second = world.render(tmp_path, attempt=2)
    tomorrow = World(captured_at="2026-10-26T16:00:00Z", now=NOW_DT + timedelta(days=1))
    third = tomorrow.render(tmp_path)
    tokens = {first["clientToken"], second["clientToken"], third["clientToken"]}
    assert len(tokens) == 3
    for request in (second, third):
        assert {k: v for k, v in request.items() if k != "clientToken"} == {
            k: v for k, v in first.items() if k != "clientToken"
        }


def _replace_override(request: dict[str, Any], **row: Any) -> None:
    request["overrides"]["containerOverrides"][0].update(row)


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda r: r.__setitem__("propagateTags", "NONE"), "propagate_tags"),
        (lambda r: r.__setitem__("tags", []), "key_set"),
        (lambda r: r.pop("overrides"), "key_set"),
        (lambda r: r.__setitem__("count", 2), "run_task_request_invariant"),
        (lambda r: r.__setitem__("enableExecuteCommand", True), "invariant"),
        (lambda r: r.__setitem__("cluster", "other"), "invariant"),
        (
            lambda r: r["networkConfiguration"]["awsvpcConfiguration"].__setitem__(
                "assignPublicIp", "ENABLED"
            ),
            "network",
        ),
        (
            lambda r: r.__setitem__("startedBy", "someone-else"),
            "read_request_invariant",
        ),
        (lambda r: r.__setitem__("clientToken", "x" * 40), "read_request_invariant"),
        (
            lambda r: _replace_override(r, command=["python", "-c", "print(1)"]),
            "read_request_invariant",
        ),
        (
            lambda r: _replace_override(
                r, environment=[{"name": "DATABASE_URL", "value": "x"}]
            ),
            "read_request_invariant",
        ),
        (lambda r: _replace_override(r, name="app"), "read_request_invariant"),
        (
            lambda r: r["overrides"].__setitem__(
                "taskRoleArn", "arn:aws:iam::1:role/x"
            ),
            "read_request_invariant",
        ),
        (
            lambda r: r["overrides"]["containerOverrides"].append(
                {"name": "consulta", "command": ["true"]}
            ),
            "read_request_invariant",
        ),
    ],
)
def test_the_request_invariants_refuse_any_widening(
    tmp_path: Path, mutate: Callable[[dict[str, Any]], None], reason: str
) -> None:
    world = World()
    document, context = rr.evaluate_plan(
        world.readback(tmp_path), world.context(), now=NOW_DT
    )
    assert context is not None
    request = world.render(tmp_path)
    mutate(request)
    with pytest.raises(mw.WindowError, match=reason):
        rr.assert_request_invariants(request, context.target)


def test_the_request_invariants_refuse_another_family(tmp_path: Path) -> None:
    world = World()
    _document, context = rr.evaluate_plan(
        world.readback(tmp_path), world.context(), now=NOW_DT
    )
    assert context is not None
    request = world.render(tmp_path)
    migrations = TD_PREFIX + "neurostack-staging-migrations:7"
    request["taskDefinition"] = migrations
    target = mw.TaskTarget(
        task_definition_arn=migrations,
        container="consulta",
        log=READ_LOG,
        image=BRIDGE_IMAGE,
    )
    with pytest.raises(mw.WindowError, match="override_only_on_the_read_family"):
        rr.assert_request_invariants(request, target)


# ---------------------------------------------------------------------------
# verify: success and polling
# ---------------------------------------------------------------------------


def test_verify_on_the_postgresql_capture_is_verified(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = recorded(tmp_path)
    result = world.verify(tmp_path)
    assert result["outcome"] == "verified", result["reasons"]
    assert result["reasons"] == [] and result["alert"] is False
    assert result["task_arn"] == READ_TASK and result["attempt"] == 1
    assert result["container_exit_code"] == 0
    assert result["alembic_version"] == ["037"]
    assert result["facts"] == {
        "billing_rollout_control": [
            {
                "environment": env,
                "state": "disabled",
                "topup_commercial_enabled": "false",
            }
            for env in ("development", "production", "staging", "test")
        ],
        "legacy_tables_absent": 4,
        "legacy_tables_expected": 4,
        "legacy_functions_absent": 10,
        "legacy_functions_expected": 10,
        "billing_subscriptions_rows": 0,
        "billing_checkout_sessions_rows": 0,
        "row_level_security_on_read_tables": 0,
        "unexpected_rows": 0,
    }
    verdict = result["verdict"]
    for fragment in (
        f"read task {READ_TASK_ID} on neurostack-staging-consulta:28",
        "startedBy neurostack-rollout-readback",
        f"created {TASK_CREATED}",
        "container exit 0",
        "REPEATABLE READ, READ ONLY",
        f"payload sha256 {rr.payload_sha256()}",
        f"service neurostack-staging-app:11, deployment {DEPLOYMENT_ID} COMPLETED",
        "Read in that transaction: alembic_version 037",
        "billing_rollout_control 4 rows: development disabled, "
        "topup_commercial_enabled false; production disabled",
        "4 of 4 tables, 10 of 10 functions",
        "billing_subscriptions 0 rows",
        "billing_checkout_sessions 0 rows",
        "Row level security on the read tables: 0.",
    ):
        assert fragment in verdict, fragment
    # What the read proves: the current state, not the history. The verdict
    # ends with that sentence, whole.
    assert verdict.endswith(
        " Row level security on the read tables: 0. This is the state at the "
        "time of the read, not its history: it does not show that no rollout "
        "transition ever happened (no rollout revision or receipt was read)."
    )
    code, out, err = run_cli(["verify", *world.args(tmp_path)], capsys)
    assert code == 0 and err == ""
    assert json.loads(out) == result


def test_verify_accepts_the_second_capture_too(tmp_path: Path) -> None:
    result = recorded(tmp_path, _lines("rollout_at_037_again")).verify(tmp_path)
    assert result["outcome"] == "verified", result["reasons"]


def test_verify_polls_until_the_task_stopped_and_every_page_is_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    rendered = world.record_read(tmp_path, describe=False, logs=False)
    result = world.verify(tmp_path)
    assert result["outcome"] == "in_progress"
    assert result["reasons"] == ["read_task_not_described"]
    assert [row["operation"] for row in result["next_requests"]] == ["DescribeTasks"]
    code, _out, _err = run_cli(["verify", *world.args(tmp_path)], capsys)
    assert code == 3
    command = rendered["overrides"]["containerOverrides"][0]["command"]
    world.put(
        "ecs",
        "DescribeTasks",
        mw.describe_tasks_request([READ_TASK]),
        {"tasks": [world.described(command, status="RUNNING")], "failures": []},
    )
    result = world.verify(tmp_path)
    assert result["reasons"] == ["read_task_not_stopped"]
    assert [row["operation"] for row in result["next_requests"]] == [
        "DescribeTasks",
        "GetLogEvents",
    ]
    world.put(
        "ecs",
        "DescribeTasks",
        mw.describe_tasks_request([READ_TASK]),
        {"tasks": [world.described(command)], "failures": []},
    )
    result = world.verify(tmp_path)
    assert result["reasons"] == ["log_events_not_read"]
    assert result["next_requests"] == [
        {
            "service": "logs",
            "operation": "GetLogEvents",
            "params": hook.log_events_request(READ_LOG, READ_TASK_ID),
        }
    ]
    lines = _lines()
    world.put_pages([lines[:10], lines[10:]], terminal=False)
    result = world.verify(tmp_path)
    assert result["outcome"] == "in_progress"
    assert result["next_requests"][0]["params"]["nextToken"] == "f/2"
    world.put_pages([lines[:10], lines[10:]])
    assert world.verify(tmp_path)["outcome"] == "verified"


def test_verify_requires_the_recorded_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = World()
    with pytest.raises(mw.WindowError, match="read_not_recorded"):
        world.verify(tmp_path)
    code, out, err = run_cli(["verify", *world.args(tmp_path)], capsys)
    assert (code, out, err.strip()) == (2, "", "blocked: read_not_recorded")


def test_verify_accepts_a_token_rendered_the_day_before(tmp_path: Path) -> None:
    late = datetime(2026, 10, 25, 23, 59, 30, tzinfo=timezone.utc)
    world = World(captured_at="2026-10-25T23:59:00Z", now=late)
    world.record_read(
        tmp_path,
        created="2026-10-26T00:00:20+00:00",
        stopped="2026-10-26T00:01:40+00:00",
        captured="2026-10-26T00:03:00Z",
    )
    result = world.verify(tmp_path)
    assert result["outcome"] == "verified", result["reasons"]


def test_verify_refuses_a_token_from_an_older_day(tmp_path: Path) -> None:
    world = World()
    world.record_read(
        tmp_path,
        created="2026-10-27T16:02:00+00:00",
        stopped="2026-10-27T16:03:30+00:00",
        captured="2026-10-27T16:05:00Z",
    )
    _stopped_with(world.verify(tmp_path), "read_request_not_rendered_by_this_tool")


# ---------------------------------------------------------------------------
# verify: the task
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exit_code", [1, 3, None, "0"])
def test_verify_stops_on_a_wrong_exit_code(tmp_path: Path, exit_code: Any) -> None:
    result = recorded(tmp_path, exit_code=exit_code).verify(tmp_path)
    _stopped_with(result, "read_exit_code_not_zero")
    assert result["container_exit_code"] == (
        exit_code if type(exit_code) is int else None
    )


def test_verify_stops_on_a_failed_read(tmp_path: Path) -> None:
    lines = _lines()[:2] + [
        json.dumps(
            {
                "schema": mw.READ_LINE_SCHEMA,
                "seq": 2,
                "kind": "end",
                "status": "error",
                "error_class": "InsufficientPrivilegeError",
                "sqlstate": "42501",
            },
            separators=(",", ":"),
        )
    ]
    result = recorded(tmp_path, lines, exit_code=3).verify(tmp_path)
    _stopped_with(result, "read_failed", "read_exit_code_not_zero")
    assert result["sqlstate"] == "42501"
    assert result["error_class"] == "InsufficientPrivilegeError"
    assert result["facts"] is None


def _drop_kind(kind: str) -> Callable[[list[str]], list[str]]:
    def mutate(lines: list[str]) -> list[str]:
        return _dump([d for d in _documents(lines) if d["kind"] != kind])

    return mutate


def _duplicate_row(lines: list[str]) -> list[str]:
    documents = _documents(lines)
    documents.insert(3, copy.deepcopy(documents[2]))
    return _dump(documents)


def _extra_row(lines: list[str]) -> list[str]:
    documents = _documents(lines)
    row = copy.deepcopy(documents[2])
    row["values"] = ["rollout state", "preview", "disabled"]
    documents.insert(3, row)
    return _dump(documents)


@pytest.mark.parametrize(
    "mutate,reasons",
    [
        (_drop_kind("end"), ["read_end_missing"]),
        (_drop_kind("begin"), ["read_begin_mismatch"]),
        (_drop_kind("version"), ["read_alembic_version_mismatch"]),
        (
            lambda ls: _without_row(ls, "rollout state", "production"),
            [
                "read_checks_mismatch",
                "read_fact_missing:rollout_state_not_disabled:production",
            ],
        ),
        (
            lambda ls: _without_row(ls, "billing_subscriptions rows", ""),
            [
                "read_checks_mismatch",
                "read_fact_missing:billing_subscriptions_not_empty",
            ],
        ),
        (_duplicate_row, ["read_checks_mismatch"]),
        (_extra_row, ["read_checks_mismatch"]),
        (lambda ls: ls + ["INFO something else"], ["read_log_foreign_line"]),
        (lambda ls: ls + [ls[-1]], ["read_sequence_broken"]),
        (
            lambda ls: ls[:5] + ['{"schema":"' + mw.READ_LINE_SCHEMA + '",'] + ls[5:],
            ["read_line_malformed"],
        ),
        (
            lambda ls: _dump(_documents(ls), renumber=False)[:-1]
            + [ls[-1].replace('"rows":25', '"rows":24')],
            ["read_row_count_mismatch"],
        ),
    ],
)
def test_verify_stops_on_a_missing_or_extra_result_line(
    tmp_path: Path,
    mutate: Callable[[list[str]], list[str]],
    reasons: list[str],
) -> None:
    result = recorded(tmp_path, mutate(_lines())).verify(tmp_path)
    _stopped_with(result, *reasons)


def test_an_empty_log_waits_out_the_grace_then_stops(tmp_path: Path) -> None:
    within = recorded(tmp_path, pages=[[]], captured="2026-10-25T16:05:30Z")
    result = within.verify(tmp_path)
    assert result["outcome"] == "in_progress"
    assert result["reasons"] == ["log_events_not_delivered"]
    after = recorded(tmp_path, pages=[[]], captured="2026-10-25T16:05:31Z")
    _stopped_with(after.verify(tmp_path), "read_end_missing")


def test_verify_stops_when_the_program_hash_differs(tmp_path: Path) -> None:
    # The task ran another payload than the one rendered.
    world = World()
    rendered = world.render(tmp_path)
    program = rendered["overrides"]["containerOverrides"][0]["command"][2]
    header = mw._READ_HEADER.match(program)
    assert header is not None
    raw = rr.read_payload().replace(
        b"IS NULL THEN 'absent'", b"IS NOT NULL THEN 'absent'"
    )
    assert raw != rr.read_payload()
    other = mw.program_for_payload(rr.READ_WINDOW, rr.READ_PHASE, raw)
    assert mw.normalized_read_command(["python", "-c", other]) is not None
    world.record_read(
        tmp_path,
        params=rendered,
        task=world.described(["python", "-c", other]),
    )
    _stopped_with(
        world.verify(tmp_path),
        "read_task_overrides_present",
        "read_task_command_mismatch",
    )


def test_verify_stops_when_the_begin_line_names_another_payload(
    tmp_path: Path,
) -> None:
    documents = _documents(_lines())
    documents[0]["payload_sha256"] = "0" * 64
    _stopped_with(
        recorded(tmp_path, _dump(documents)).verify(tmp_path), "read_begin_mismatch"
    )


@pytest.mark.parametrize(
    "window,phase", [("037", "post"), ("036", "rollout"), ("037", "pre")]
)
def test_verify_stops_when_the_begin_line_has_other_labels(
    tmp_path: Path, window: str, phase: str
) -> None:
    documents = _documents(_lines())
    documents[0]["window"], documents[0]["phase"] = window, phase
    _stopped_with(
        recorded(tmp_path, _dump(documents)).verify(tmp_path), "read_begin_mismatch"
    )


def test_verify_stops_on_a_request_this_tool_did_not_render(tmp_path: Path) -> None:
    world = World()
    rendered = world.render(tmp_path)
    tampered = copy.deepcopy(rendered)
    tampered["overrides"]["containerOverrides"][0]["command"] = [
        "python",
        "-c",
        mw.program_for_payload(
            rr.READ_WINDOW, rr.READ_PHASE, b'{"sql":"","version":""}'
        ),
    ]
    world.record_read(
        tmp_path,
        params=tampered,
        task=world.described(rendered["overrides"]["containerOverrides"][0]["command"]),
    )
    _stopped_with(world.verify(tmp_path), "read_request_not_rendered_by_this_tool")
    world.record_read(tmp_path, params=dict(rendered, propagateTags="NONE"))
    _stopped_with(
        world.verify(tmp_path),
        "run_task_request_propagate_tags_present",
        "read_request_not_rendered_by_this_tool",
    )


@pytest.mark.parametrize(
    "changes,reason",
    [
        (
            {"taskDefinitionArn": TD_PREFIX + "neurostack-staging-consulta:29"},
            "read_task_identity_mismatch",
        ),
        (
            {"startedBy": "neurostack-migration-read-037-post"},
            "read_task_identity_mismatch",
        ),
        (
            {
                "overrides": {
                    "containerOverrides": [
                        {
                            "name": "consulta",
                            "command": rr.read_command(),
                            "environment": [{"name": "X", "value": "1"}],
                        }
                    ],
                    "inferenceAcceleratorOverrides": [],
                }
            },
            "read_task_overrides_present",
        ),
        (
            {
                "overrides": {
                    "containerOverrides": [
                        {"name": "consulta", "command": rr.read_command()}
                    ],
                    "taskRoleArn": f"arn:aws:iam::{ACCOUNT}:role/other",
                }
            },
            "read_task_overrides_present",
        ),
        (
            {"overrides": {"containerOverrides": [{"name": "consulta"}]}},
            "read_task_command_mismatch",
        ),
    ],
)
def test_verify_stops_on_the_task_identity_or_overrides(
    tmp_path: Path, changes: dict[str, Any], reason: str
) -> None:
    world = World()
    rendered = world.render(tmp_path)
    command = rendered["overrides"]["containerOverrides"][0]["command"]
    world.record_read(
        tmp_path, params=rendered, task=world.described(command, **changes)
    )
    _stopped_with(world.verify(tmp_path), reason)


def test_verify_stops_on_a_failed_run_task(tmp_path: Path) -> None:
    world = recorded(
        tmp_path,
        response={"tasks": [], "failures": [{"reason": "RESOURCE:MEMORY"}]},
    )
    _stopped_with(world.verify(tmp_path), "read_run_task_failed")


def test_verify_stops_on_a_task_before_the_deployment(tmp_path: Path) -> None:
    world = World()
    response = _service(SERVICE_TD)
    response["services"][0]["deployments"][0]["createdAt"] = "2026-10-25T16:02:30+00:00"
    world.record_read(tmp_path)
    world.put("ecs", "DescribeServices", mw.describe_services_request(), response)
    _stopped_with(world.verify(tmp_path), "read_task_not_after_the_deployment")


def test_verify_stops_on_an_old_task(tmp_path: Path) -> None:
    """An idempotent replay hands back an older task: not this read."""

    world = recorded(tmp_path, captured="2026-10-25T17:02:01Z")
    _stopped_with(world.verify(tmp_path), "read_task_not_recent")
    at_limit = recorded(tmp_path, captured="2026-10-25T17:02:00Z")
    assert at_limit.verify(tmp_path)["outcome"] == "verified"


def test_verify_stops_on_a_task_created_after_the_reads(tmp_path: Path) -> None:
    """M17: a task created more than 60 s (the clock-skew allowance) after
    the latest recorded reads cannot be what those reads saw stop."""

    late = recorded(tmp_path, captured="2026-10-25T16:00:59Z")
    _stopped_with(late.verify(tmp_path), "read_task_not_recent")
    skew = recorded(tmp_path, captured="2026-10-25T16:01:00Z")
    assert skew.verify(tmp_path)["outcome"] == "verified"


@pytest.mark.parametrize(
    "changes",
    [
        {"taskDefinitionArn": TD_PREFIX + "neurostack-staging-consulta:29"},
        {"startedBy": "neurostack-migration-read-037-post"},
    ],
)
def test_verify_stops_on_a_run_task_response_of_another_task(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    """M20: the RunTask response names another revision or startedBy, while
    the described task is the right one."""

    response = {
        "tasks": [
            {
                "taskArn": READ_TASK,
                "taskDefinitionArn": READ_TD,
                "startedBy": rr.STARTED_BY,
                "lastStatus": "PROVISIONING",
                "createdAt": TASK_CREATED,
                **changes,
            }
        ],
        "failures": [],
    }
    result = recorded(tmp_path, response=response).verify(tmp_path)
    _stopped_with(result, "read_run_task_response_invalid")
    assert result["reasons"] == ["read_run_task_response_invalid"]


def test_verify_stops_past_the_watch_deadline(tmp_path: Path) -> None:
    world = recorded(tmp_path, captured="2026-10-25T16:12:01Z", status="RUNNING")
    result = world.verify(tmp_path)
    _stopped_with(result, "read_watch_deadline_exceeded")
    assert result["alert_kinds"] == ["watch_deadline"]


def test_verify_stops_when_the_service_moved(tmp_path: Path) -> None:
    world = recorded(tmp_path)
    world.put(
        "ecs",
        "DescribeServices",
        mw.describe_services_request(),
        _service(SERVICE_TD_10),
    )
    result = world.verify(tmp_path)
    _stopped_with(result)
    assert result["reasons"][0].startswith("read_context_unusable:")
    assert "service_task_definition_mismatch" in result["reasons"][0]


# ---------------------------------------------------------------------------
# verify: the database state
# ---------------------------------------------------------------------------

ENVIRONMENTS = ("development", "production", "staging", "test")


@pytest.mark.parametrize("environment", ENVIRONMENTS)
@pytest.mark.parametrize("state", ["canary_allowlist", "general", "Disabled", ""])
def test_verify_stops_when_a_rollout_row_is_not_disabled(
    tmp_path: Path, environment: str, state: str
) -> None:
    lines = _with_value(_lines(), "rollout state", environment, state)
    result = recorded(tmp_path, lines).verify(tmp_path)
    _stopped_with(result, f"rollout_state_not_disabled:{environment}")
    assert len(result["reasons"]) == 1
    (row,) = [
        r
        for r in result["facts"]["billing_rollout_control"]
        if r["environment"] == environment
    ]
    assert row["state"] == state


def test_verify_stops_on_the_captured_general_row(tmp_path: Path) -> None:
    result = recorded(tmp_path, _lines("rollout_production_general_red")).verify(
        tmp_path
    )
    _stopped_with(result, "rollout_state_not_disabled:production")
    assert result["reasons"] == ["rollout_state_not_disabled:production"]


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_verify_stops_when_topup_commercial_enabled_is_true(
    tmp_path: Path, environment: str
) -> None:
    lines = _with_value(
        _lines(), "rollout topup_commercial_enabled", environment, "true"
    )
    result = recorded(tmp_path, lines).verify(tmp_path)
    _stopped_with(result, f"topup_commercial_enabled_not_false:{environment}")
    assert len(result["reasons"]) == 1


@pytest.mark.parametrize(
    "version", [["036"], ["038"], [], ["036", "037"], ["037", "037a"]]
)
def test_verify_stops_when_alembic_version_is_not_037(
    tmp_path: Path, version: list[str]
) -> None:
    documents = _documents(_lines())
    documents[1]["values"] = version
    result = recorded(tmp_path, _dump(documents)).verify(tmp_path)
    _stopped_with(result, "read_alembic_version_mismatch")
    assert result["alembic_version"] == version


def test_verify_stops_on_the_capture_at_036(tmp_path: Path) -> None:
    result = recorded(tmp_path, _lines("rollout_at_036")).verify(tmp_path)
    _stopped_with(
        result,
        "read_alembic_version_mismatch",
        "legacy_table_present:billing_migrations",
        "legacy_table_present:billing_roster_control",
        "legacy_function_present:lock_billing_roster_statement",
        "legacy_function_present:billing_provider_evidence_digest",
    )
    assert len(result["reasons"]) == 1 + 4 + 10
    assert result["facts"]["legacy_tables_absent"] == 0
    assert result["facts"]["legacy_functions_absent"] == 0


@pytest.mark.parametrize(
    "check,subject,reason",
    [
        (
            "legacy table",
            "public.billing_migration_receipts",
            "legacy_table_present:billing_migration_receipts",
        ),
        (
            "legacy function",
            "public.billing_evidence_timestamp(timestamp with time zone)",
            "legacy_function_present:billing_evidence_timestamp",
        ),
        (
            "legacy function",
            "public.validate_billing_migration_absence_read()",
            "legacy_function_present:validate_billing_migration_absence_read",
        ),
    ],
)
def test_verify_stops_when_a_legacy_object_is_present(
    tmp_path: Path, check: str, subject: str, reason: str
) -> None:
    lines = _with_value(_lines(), check, subject, "present")
    result = recorded(tmp_path, lines).verify(tmp_path)
    _stopped_with(result, reason)
    assert result["reasons"] == [reason]


@pytest.mark.parametrize("value", ["1", "12", "", "0 "])
@pytest.mark.parametrize(
    "check,reason",
    [
        ("billing_subscriptions rows", "billing_subscriptions_not_empty"),
        ("billing_checkout_sessions rows", "billing_checkout_sessions_not_empty"),
    ],
)
def test_verify_stops_when_a_counted_table_is_not_empty(
    tmp_path: Path, check: str, reason: str, value: str
) -> None:
    lines = _with_value(_lines(), check, "", value)
    result = recorded(tmp_path, lines).verify(tmp_path)
    _stopped_with(result, reason)
    assert result["reasons"] == [reason]


def test_a_checkout_session_present_stops_with_its_own_reason(
    tmp_path: Path,
) -> None:
    lines = _with_value(_lines(), "billing_checkout_sessions rows", "", "1")
    result = recorded(tmp_path, lines).verify(tmp_path)
    _stopped_with(result, "billing_checkout_sessions_not_empty")
    assert result["reasons"] == ["billing_checkout_sessions_not_empty"]
    assert result["facts"]["billing_checkout_sessions_rows"] == 1
    assert result["facts"]["billing_subscriptions_rows"] == 0
    assert result["verdict"] == (
        "Rollout readback stopped: billing_checkout_sessions_not_empty"
    )


def test_verify_stops_on_row_level_security_on_billing_checkout_sessions(
    tmp_path: Path,
) -> None:
    """Captured: RLS hides every checkout from the reading role, so the count
    still reads 0; only the RLS row stops it."""

    result = recorded(tmp_path, _lines("rollout_rls_checkouts_red")).verify(tmp_path)
    _stopped_with(result, "row_level_security_on_read_tables")
    assert result["reasons"] == ["row_level_security_on_read_tables"]
    assert result["facts"]["billing_checkout_sessions_rows"] == 0
    assert result["facts"]["row_level_security_on_read_tables"] == 1


def test_a_reordered_read_is_accepted_by_design(tmp_path: Path) -> None:
    """Rows are judged by (check_name, subject): their order is not part of
    the contract (the SQL orders them for reading only). The line numbering
    must still be whole."""

    documents = _documents(_lines())
    rows = [d for d in documents if d["kind"] == "row"]
    reordered = [documents[0], documents[1], *reversed(rows), documents[-1]]
    result = recorded(tmp_path, _dump(reordered)).verify(tmp_path)
    assert result["outcome"] == "verified", result["reasons"]
    gapped = _dump(reordered)
    gapped[5] = gapped[5].replace('"seq":5,', '"seq":50,')
    _stopped_with(recorded(tmp_path, gapped).verify(tmp_path), "read_sequence_broken")


def test_verify_stops_on_the_captured_row_level_security(tmp_path: Path) -> None:
    result = recorded(tmp_path, _lines("rollout_rls_red")).verify(tmp_path)
    _stopped_with(
        result,
        "read_checks_mismatch",
        "row_level_security_on_read_tables",
        "read_fact_missing:rollout_state_not_disabled:development",
        "read_fact_missing:topup_commercial_enabled_not_false:test",
    )
    assert result["facts"]["row_level_security_on_read_tables"] == 1


def test_verify_stops_on_a_row_of_another_shape(tmp_path: Path) -> None:
    documents = _documents(_lines())
    documents[2]["values"] = ["rollout state", "development", "disabled", "extra"]
    _stopped_with(
        recorded(tmp_path, _dump(documents)).verify(tmp_path),
        "read_checks_mismatch",
    )
    documents = _documents(_lines())
    documents[24]["values"] = ["billing_subscriptions rows", "", 0]
    _stopped_with(
        recorded(tmp_path, _dump(documents)).verify(tmp_path),
        "read_checks_mismatch",
    )


# ---------------------------------------------------------------------------
# Exposure
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("leak", LEAKS)
@pytest.mark.parametrize("where", ["foreign_line", "row_value", "end_line"])
def test_verify_stops_on_a_secret_looking_string_and_never_echoes_it(
    tmp_path: Path, leak: str, where: str, capsys: pytest.CaptureFixture[str]
) -> None:
    lines = _lines()
    if where == "foreign_line":
        lines = lines[:3] + [f"Traceback: {leak}"] + lines[3:]
    elif where == "row_value":
        lines = _with_value(lines, "rollout state", "staging", leak)
    else:
        lines = lines[:-1] + [lines[-1][:-1] + f',"note":"{leak}"' + "}"]
    world = recorded(tmp_path, lines)
    result = world.verify(tmp_path)
    _stopped_with(result, "read_log_exposure_suspected")
    assert "exposure_suspected" in result["alert_kinds"]
    code, out, err = run_cli(["verify", *world.args(tmp_path)], capsys)
    assert code == 4 and err == ""
    assert leak not in out
    if where == "row_value":
        (row,) = [
            r
            for r in json.loads(out)["facts"]["billing_rollout_control"]
            if r["environment"] == "staging"
        ]
        assert row["state"] is None


def test_every_output_passes_the_window_tool_guard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run_cli(["plan", *World().args(tmp_path, "plan.json")], capsys)
    assert code == 0 and err == ""
    assert mw._FORBIDDEN_OUTPUT.search(out) is None
    world = recorded(tmp_path)
    code, out, err = run_cli(["verify", *world.args(tmp_path)], capsys)
    assert code == 0 and err == ""
    guarded = json.dumps(mw._without_read_programs(json.loads(out)))
    assert mw._FORBIDDEN_OUTPUT.search(guarded) is None
    assert mw._LOG_EXPOSURE.search(json.dumps(json.loads(out)["facts"])) is None


def test_the_output_guard_still_refuses_what_it_refuses(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(mw.WindowError, match="output_forbidden"):
        mw._emit({"verdict": "stop task now", "command": ["python", "-c", "x"]})
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# Runbook
# ---------------------------------------------------------------------------


def _runbook_section() -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    start = text.index("## Post-deploy rollout readback\n")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def test_the_runbook_section_matches_the_tool() -> None:
    section = _runbook_section()
    flat = " ".join(section.split())
    for fragment in (
        "scripts/asaas_rollout_readback.py",
        "scripts/asaas_rollout_readback.sql",
        "neurostack-rollout-readback",
        "neurostack-staging-consulta:28",
        rr.READBACK_SCHEMA,
        "plan",
        "render",
        "verify",
        "ROLLOUT_READBACK_STOPPED",
        "rollout-readback-",
        "table of 25 rows (4 + 4 + 4 + 10 + 3)",
        "a reordered read is accepted by design",
        "`plan` and `render` judge freshness against the system clock; "
        "`verify` does not read the clock at all",
        "send `ecs:DescribeServices` and `ecs:DescribeTaskDefinition` again",
        "`verify` exits 2 (`read_not_recorded`) until `read` is in the file",
        "It does **not** show the history",
        "billing_checkout_sessions_not_empty",
        "tests/integration/test_rollout_readback_postgres.py",
    ):
        assert fragment in flat, fragment
    # The superseded claim that the system clock judges every subcommand.
    assert "(the system clock judges freshness)" not in flat
    for check, subject, value in rr.EXPECTED_TABLE:
        assert f"| `{check}` |" in section, check
    assert re.search(r"upgrade\s+heads?\b", section, re.IGNORECASE) is None
    # Its own section, after the window tooling and before the wiring: the
    # state section at the top stays untouched by it.
    text = RUNBOOK.read_text(encoding="utf-8")
    assert (
        text.index("## Migration windows: the run tooling")
        < text.index("## Post-deploy rollout readback")
        < text.index("## Production wiring (Group 9 D3/D4)")
    )
