"""Unit proofs of ``python -m src.ops.credit_enforcement`` (Group 6).

What needs no database: the argument contract (every malformed invocation
is ``unconfigured``/``usage`` before any session or identity call), the
closed result vocabulary, the ECS task identity read, the switch-off path
doing no database work, the mapping of every refusal the service can raise,
and the real ``python -m`` entry point printing exactly one stdout line.

The module's contract against the database (switch refresh, database clock,
``super_admin``, digest inside the arming transaction, read-only status) is
proven only by ``tests/integration/test_credit_enforcement_ops_postgres.py``.
"""

from __future__ import annotations

import ast
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from src.config.settings import settings
from src.ops import credit_enforcement as ops
from src.services.credit_switch import stop_credit_switch

ROOT = Path(__file__).resolve().parents[2]
DIGEST = "a" * 64
TENANT = "3f1d0c4e-5a6b-4c7d-8e9f-0a1b2c3d4e5f"
BUILDER = "4a2e1d5f-6b7c-4d8e-9f0a-1b2c3d4e5f60"
AUTHOR = "5b3f2e60-7c8d-4e9f-8a1b-2c3d4e5f6071"
TASK_ARN = (
    "arn:aws:ecs:us-east-1:123456789012:task/neurostack-staging/"
    "0123456789abcdef0123456789abcdef"
)
METADATA = "http://169.254.170.2/v4/0123456789abcdef-1234567890"


def _accept(**overrides: str) -> list[str]:
    values = {
        "--report-digest": DIGEST,
        "--tenant-id": TENANT,
        "--builder-id": BUILDER,
        "--reason-code": "builder_without_coverage",
        "--author-user-id": AUTHOR,
        "--rationale": "Owner accepted after review (runbook row 3).",
        **overrides,
    }
    argv = ["accept"]
    for flag, value in values.items():
        if value != "<omit>":
            argv += [flag, value]
    return argv


class _Untouchable:
    """A session factory / identity that fails the test when used."""

    def __call__(self, *args: object, **kwargs: object) -> Any:
        raise AssertionError("no database or identity work was allowed here")


async def _invoke(argv: list[str], **injections: Any) -> tuple[int, dict[str, Any]]:
    out, err = io.StringIO(), io.StringIO()
    code = await ops.amain(
        argv,
        session_factory=injections.pop("session_factory", _Untouchable()),
        task_identity=injections.pop("task_identity", _Untouchable()),
        stdout=out,
        stderr=err,
    )
    (line,) = out.getvalue().splitlines()
    document = json.loads(line)
    assert document["exit_code"] == code
    return code, document


# ---------------------------------------------------------------------------
# Argument contract.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["disarm"],
        ["--help"],
        ["status", "--report-digest", DIGEST],
        ["report", "extra"],
        ["arm"],
        ["arm", "--report-digest", DIGEST.upper()],
        ["arm", "--report-digest", "a" * 63],
        ["arm", "--report-digest", DIGEST, "--at", "2026-11-01T00:00:00Z"],
        ["arm", "--report-digest", DIGEST, "--set-by", "someone"],
        ["arm", "--report", DIGEST],
        _accept(**{"--rationale": "<omit>"}),
        _accept(**{"--rationale": "   "}),
        _accept(**{"--rationale": "line one\nline two"}),
        _accept(**{"--rationale": "x" * 1001}),
        _accept(**{"--rationale": "Approved, ask owner@example.com for details"}),
        _accept(**{"--rationale": "contato: first.last@clinic.invalid"}),
        _accept(**{"--reason-code": "legacy_exemption"}),
        _accept(**{"--reason-code": "Standalone"}),
        _accept(**{"--tenant-id": TENANT.upper()}),
        _accept(**{"--tenant-id": "{" + TENANT + "}"}),
        _accept(**{"--builder-id": "null"}),
        _accept(**{"--builder-id": "<omit>"}),
        _accept(**{"--author-user-id": "owner@example.com"}),
        _accept(**{"--report-digest": "<omit>"}),
    ],
    ids=lambda argv: " ".join(argv)[:60] or "empty",
)
async def test_a_malformed_invocation_is_usage_before_any_work(
    argv: list[str],
) -> None:
    code, document = await _invoke(argv)
    assert code == ops.EXIT_CODES["unconfigured"] == 30
    assert document["verdict"] == "unconfigured"
    assert document["reasons"] == ["usage"]
    assert document["command"] is None


@pytest.mark.parametrize("builder", [BUILDER, "none"])
def test_a_well_formed_accept_parses_to_its_finding(builder: str) -> None:
    args = ops.parse_arguments(_accept(**{"--builder-id": builder}))
    assert args.command == "accept"
    assert str(args.tenant_id) == TENANT
    assert (None if args.builder_id is None else str(args.builder_id)) == (
        None if builder == "none" else builder
    )
    assert args.reason_code == "builder_without_coverage"
    assert args.report_digest == DIGEST


def test_the_reason_codes_are_exactly_the_cutoff_report_literals() -> None:
    """Acceptances are keyed by a reason CODE of the report, never free text."""

    path = ROOT / "src/services/credit_rollout.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    emitted: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "CutoffFinding"
            and len(node.args) == 3
            and isinstance(node.args[2], ast.Constant)
        ):
            emitted.add(node.args[2].value)
    assert emitted and ops.CUTOFF_REASON_CODES == frozenset(emitted)


def test_every_refusal_the_service_can_raise_has_a_reason() -> None:
    path = ROOT / "src/services/credit_rollout.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    messages: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "CreditEnforcementRejected"
        ):
            (argument,) = node.args
            assert isinstance(argument, ast.Constant)
            messages.add(argument.value)
    # The boundary refusal of a malformed confirmed digest (before the lock).
    assert "expected_report_digest is not a lowercase SHA-256" in messages
    assert messages == set(ops.REJECTION_REASONS)
    assert set(ops.REJECTION_REASONS.values()) <= ops.REASONS
    assert (
        ops.REJECTION_REASONS["expected_report_digest is not a lowercase SHA-256"]
        == "report_digest_malformed"
    )


# ---------------------------------------------------------------------------
# Result contract.
# ---------------------------------------------------------------------------


def test_the_result_vocabulary_is_closed() -> None:
    with pytest.raises(ValueError):
        ops.RunResult(command="arm", verdict="maybe")
    with pytest.raises(ValueError):
        ops.RunResult(command="arm", verdict="refused", reasons=("free text",))
    with pytest.raises(ValueError):
        ops.RunResult(command="disarm", verdict="refused")
    with pytest.raises(ValueError):
        ops.RunResult(command="status", verdict="armed")
    with pytest.raises(ValueError):
        ops.RunResult(command="arm", verdict="accepted")
    assert ops.RunResult(command="arm", verdict="already_armed").exit_code == 10


def test_a_rationale_with_an_at_sign_but_no_address_is_accepted() -> None:
    args = ops.parse_arguments(
        _accept(**{"--rationale": "Owner @ review: accepted, builder pending (L10)."})
    )
    assert args.rationale == "Owner @ review: accepted, builder pending (L10)."


def test_the_exit_codes_are_the_published_literals() -> None:
    """The runbook and the operator's alarms read these numbers."""

    assert dict(ops.EXIT_CODES) == {
        "ok": 0,
        "accepted": 0,
        "armed": 0,
        "already_armed": 10,
        "refused": 20,
        "switch_off": 21,
        "unconfigured": 30,
        "readback_mismatch": 40,
        "error": 50,
    }


def test_each_service_refusal_maps_to_its_published_reason() -> None:
    assert dict(ops.REJECTION_REASONS) == {
        "credit_enforcement_at cannot be backdated before the database clock": (
            "clock_refused"
        ),
        "credit_enforcement_at cannot be future-dated past the database clock": (
            "clock_refused"
        ),
        "expected_report_digest is not a lowercase SHA-256": (
            "report_digest_malformed"
        ),
        "credit orphan reconciliation is incomplete": "orphans_open",
        "credit readiness is incomplete": "readiness_incomplete",
        "tenant cutoff report differs from the confirmed digest": (
            "report_digest_mismatch"
        ),
        "tenant cutoff report is unaccepted": "cutoff_unaccepted",
        "credit enforcement is already armed": "already_armed",
        "cutoff acceptance author is not an active super_admin": (
            "author_not_active_super_admin"
        ),
    }


def test_the_runbook_exit_code_table_matches_the_module() -> None:
    runbook = ROOT / "docs/runbooks/credit-enforcement-arming.md"
    published: dict[str, int] = {}
    for line in runbook.read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 2 and cells[1].isdigit() and cells[0].startswith("`"):
            for verdict in cells[0].split("/"):
                published[verdict.strip().strip("`")] = int(cells[1])
    assert published == dict(ops.EXIT_CODES)


def test_already_armed_has_its_own_exit_code() -> None:
    codes = dict(ops.EXIT_CODES)
    already = codes.pop("already_armed")
    assert already not in codes.values() and already != 0
    assert {codes["ok"], codes["accepted"], codes["armed"]} == {0}
    others = [v for k, v in codes.items() if k not in {"ok", "accepted", "armed"}]
    assert 0 not in others and len(set(others)) == len(others)


# ---------------------------------------------------------------------------
# Switch off: refused with no database work and no identity call.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [["status"], ["report"], ["arm", "--report-digest", DIGEST], _accept()],
    ids=["status", "report", "arm", "accept"],
)
async def test_with_the_configuration_off_nothing_reaches_the_database(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    monkeypatch.setattr(settings, "credit_hot_path_enabled", False)
    try:
        code, document = await _invoke(argv)
    finally:
        await stop_credit_switch(None)
    assert code == ops.EXIT_CODES["switch_off"]
    assert document["verdict"] == "switch_off"
    assert document["switch"] == {
        "effective": False,
        "reason": "config_disabled",
        "revision": None,
    }


# ---------------------------------------------------------------------------
# The ECS task identity (set_by).
# ---------------------------------------------------------------------------


def _metadata(status: int = 200, body: object = None) -> httpx.MockTransport:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        payload = {"TaskARN": TASK_ARN, "Cluster": "neurostack-staging"}
        return httpx.Response(status, json=payload if body is None else body)

    transport = httpx.MockTransport(handler)
    transport.seen = seen  # type: ignore[attr-defined]
    return transport


async def test_the_task_arn_comes_from_the_ecs_metadata_endpoint() -> None:
    transport = _metadata()
    arn = await ops.ecs_task_arn(
        environ={"ECS_CONTAINER_METADATA_URI_V4": METADATA}, transport=transport
    )
    assert arn == TASK_ARN
    (request,) = transport.seen  # type: ignore[attr-defined]
    assert str(request.url) == METADATA + "/task"
    assert request.method == "GET"


@pytest.mark.parametrize(
    ("environ", "status", "body"),
    [
        ({}, 200, None),
        ({"ECS_CONTAINER_METADATA_URI_V4": ""}, 200, None),
        ({"ECS_CONTAINER_METADATA_URI_V4": "https://169.254.170.2/v4/x"}, 200, None),
        ({"ECS_CONTAINER_METADATA_URI_V4": "http://evil.example/v4/x"}, 200, None),
        ({"ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2.evil/v4"}, 200, None),
        ({"ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2:8080/v4"}, 200, None),
        ({"ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2:0/v4"}, 200, None),
        ({"ECS_CONTAINER_METADATA_URI_V4": METADATA}, 404, None),
        ({"ECS_CONTAINER_METADATA_URI_V4": METADATA}, 200, {"TaskARN": 7}),
        ({"ECS_CONTAINER_METADATA_URI_V4": METADATA}, 200, {"Cluster": "x"}),
        ({"ECS_CONTAINER_METADATA_URI_V4": METADATA}, 200, ["not", "a", "map"]),
        (
            {"ECS_CONTAINER_METADATA_URI_V4": METADATA},
            200,
            {"TaskARN": "arn:aws:ecs:us-east-1:123456789012:task/0123abcd"},
        ),
        (
            {"ECS_CONTAINER_METADATA_URI_V4": METADATA},
            200,
            {"TaskARN": TASK_ARN.replace(":task/", ":service/")},
        ),
        (
            {"ECS_CONTAINER_METADATA_URI_V4": METADATA},
            200,
            {"TaskARN": TASK_ARN + "\n"},
        ),
        (
            {"ECS_CONTAINER_METADATA_URI_V4": METADATA},
            200,
            {"TaskARN": TASK_ARN.replace("neurostack-staging", "c" * 250)},
        ),
    ],
)
async def test_an_unusable_task_identity_is_none(
    environ: dict[str, str], status: int, body: object
) -> None:
    transport = _metadata(status, body)
    assert await ops.ecs_task_arn(environ=environ, transport=transport) is None
    if not environ.get("ECS_CONTAINER_METADATA_URI_V4", "").startswith(
        "http://169.254.170.2/"
    ):
        assert transport.seen == []  # type: ignore[attr-defined]


async def test_the_default_port_is_the_only_port_read() -> None:
    transport = _metadata()
    arn = await ops.ecs_task_arn(
        environ={"ECS_CONTAINER_METADATA_URI_V4": "http://169.254.170.2:80/v4/x"},
        transport=transport,
    )
    assert arn == TASK_ARN


@pytest.mark.parametrize(
    "value",
    [METADATA[:-4] + "\t" + METADATA[-4:], METADATA[:-4] + "\r\n" + METADATA[-4:]],
    ids=["tab", "crlf"],
)
async def test_a_metadata_variable_with_a_control_character_is_none(
    value: str,
) -> None:
    """``urlsplit`` drops the character (the host check passes); httpx then
    raises ``InvalidURL``, which is not an ``HTTPError``."""

    transport = _metadata()
    assert (
        await ops.ecs_task_arn(
            environ={ops.ECS_METADATA_ENV: value}, transport=transport
        )
        is None
    )
    assert transport.seen == []  # type: ignore[attr-defined]


async def test_a_control_character_in_the_metadata_variable_is_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Through the write path: ``task_identity_unavailable`` (30), before any
    session is opened -- not ``error``/``unexpected`` (50)."""

    monkeypatch.setenv(ops.ECS_METADATA_ENV, METADATA[:-4] + "\t" + METADATA[-4:])
    for argv in (_accept(), ["arm", "--report-digest", DIGEST]):
        args = ops.parse_arguments(argv)
        handler = ops._accept if args.command == "accept" else ops._arm
        result = await handler(args, _Untouchable(), ops.ecs_task_arn, {})
        assert (result.verdict, result.reasons, result.exit_code) == (
            "unconfigured",
            ("task_identity_unavailable",),
            30,
        )


async def test_the_metadata_client_ignores_the_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An injected transport bypasses httpx's proxy mounts, so the client's
    own options are what is pinned: no proxy/netrc/CA variables (``trust_env``)
    and no redirect."""

    seen: list[dict[str, object]] = []
    real = httpx.AsyncClient

    def spy(**kwargs: Any) -> httpx.AsyncClient:
        seen.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(ops.httpx, "AsyncClient", spy)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    arn = await ops.ecs_task_arn(
        environ={ops.ECS_METADATA_ENV: METADATA}, transport=_metadata()
    )
    assert arn == TASK_ARN
    ((options),) = seen
    assert options["trust_env"] is False
    assert options["follow_redirects"] is False
    assert options["timeout"] == ops.METADATA_TIMEOUT_SECONDS


async def test_a_metadata_transport_failure_is_none() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    arn = await ops.ecs_task_arn(
        environ={"ECS_CONTAINER_METADATA_URI_V4": METADATA},
        transport=httpx.MockTransport(handler),
    )
    assert arn is None


# ---------------------------------------------------------------------------
# The real entry point, as the one-off ECS task runs it.
# ---------------------------------------------------------------------------


def _subprocess(
    tmp_path: Path, argv: list[str], **overrides: str
) -> subprocess.CompletedProcess[str]:
    """``python -m src.ops.credit_enforcement`` from a directory with no
    ``.env`` (``src`` linked in) and a database URL nothing listens on."""

    workdir = tmp_path / "app"
    workdir.mkdir()
    (workdir / "src").symlink_to(ROOT / "src", target_is_directory=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "ENVIRONMENT": "staging",
        "SECRET_KEY": "s" * 48,
        "DATABASE_URL": "postgresql+asyncpg://nobody@127.0.0.1:9/none",
        "CREDIT_HOT_PATH_ENABLED": "false",
        **overrides,
    }
    return subprocess.run(
        [sys.executable, "-m", "src.ops.credit_enforcement", *argv],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


@pytest.mark.parametrize(
    ("argv", "verdict"),
    [(["arm"], "unconfigured"), (["status"], "switch_off")],
)
def test_the_entry_point_prints_one_document(
    tmp_path: Path, argv: list[str], verdict: str
) -> None:
    result = _subprocess(tmp_path, argv)
    assert result.returncode == ops.EXIT_CODES[verdict], result.stderr[-2000:]
    (line,) = result.stdout.splitlines()
    document = json.loads(line)
    assert document["schema"] == ops.SCHEMA
    assert document["verdict"] == verdict
    logs = [
        json.loads(raw)
        for raw in result.stderr.splitlines()
        if raw.startswith("{") and json.loads(raw).get("event") == ops.LOG_EVENT
    ]
    assert len(logs) == 1 and logs[0]["verdict"] == verdict


def _closed_loopback_port() -> int:
    """A port that was just free on 127.0.0.1 (refused at once, unlike a
    low port a firewall may silently drop)."""

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_an_unreachable_database_never_prints_its_password(tmp_path: Path) -> None:
    """The credit switch on and the database unreachable (wrong subnet or
    security group in RunTask): ``init_database`` logs its failure with
    ``exc_info``.  Under structlog's defaults (or ``LOG_FORMAT=console``)
    with ``rich`` importable, that traceback prints its local variables, the
    resolved database URL among them.  The task forces JSON tracebacks."""

    # The premise: the leak needs ``rich`` importable by the subprocess.
    # Without it this proof would pass vacuously, so it fails instead.
    premise = subprocess.run(
        [sys.executable, "-c", "import rich"], capture_output=True, check=False
    )
    assert premise.returncode == 0, (
        "rich is not importable: this proof is vacuous without it; install "
        "the dev dependencies or rebuild the premise (structlog's console "
        "renderer with locals)"
    )
    password = "Zq9-Recognisable-Pw-7731"
    result = _subprocess(
        tmp_path,
        ["status"],
        DATABASE_URL=(
            f"postgresql+asyncpg://neurostack:{password}@127.0.0.1:"
            f"{_closed_loopback_port()}/neurostack"
        ),
        CREDIT_HOT_PATH_ENABLED="true",
        LOG_FORMAT="console",
    )
    assert password not in result.stdout
    assert password not in result.stderr
    (line,) = result.stdout.splitlines()
    document = json.loads(line)
    assert (document["verdict"], document["reasons"]) == ("error", ["unexpected"])
    assert result.returncode == ops.EXIT_CODES["error"]
    # Not vacuous: the failure WAS logged, as JSON, with its traceback.
    records = [
        json.loads(raw) for raw in result.stderr.splitlines() if raw.startswith("{")
    ]
    (failure,) = [
        r for r in records if r.get("event") == "Failed to initialize database"
    ]
    assert failure["level"] == "error"
    assert "Traceback" in failure["exception"]
    assert all(raw.startswith("{") for raw in result.stderr.splitlines() if raw)


# ---------------------------------------------------------------------------
# The runbook's RunTask snippets, run verbatim with a stand-in ``aws``.
# ---------------------------------------------------------------------------

RUNBOOK = ROOT / "docs/runbooks/credit-enforcement-arming.md"
# An apostrophe, double quotes and a backslash: what breaks shell quoting.
AWKWARD_RATIONALE = 'Owner\'s "pilot" tenant, path C:\\credit kept; accepted (L10).'
_FAKE_AWS = """#!/usr/bin/env python3
import json, pathlib, sys
pathlib.Path("aws-args.json").write_text(json.dumps(sys.argv[1:]))
"""


def _bash_blocks() -> list[str]:
    text = RUNBOOK.read_text(encoding="utf-8")
    return re.findall(r"```bash\n(.*?)```", text, flags=re.DOTALL)


def _run_runbook_step(
    tmp_path: Path, step: str, **values: str
) -> subprocess.CompletedProcess[str]:
    blocks = _bash_blocks()
    (helper,) = [block for block in blocks if "run_step() {" in block]
    (snippet,) = [
        block
        for block in blocks
        if block.startswith("jq -n") and f"&& run_step {step}\n" in block
    ]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "aws"
    fake.write_text(_FAKE_AWS, encoding="utf-8")
    fake.chmod(0o755)
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "TASK_DEFINITION": "arn:aws:ecs:us-east-1:123456789012:task-definition/x:7",
        "SUBNETS": "subnet-a,subnet-b",
        "SECURITY_GROUPS": "sg-a",
        **values,
    }
    return subprocess.run(
        ["bash", "-c", helper + "\n" + snippet],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


_VALUES = {
    "DIGEST": DIGEST,
    "TENANT": TENANT,
    "BUILDER": BUILDER,
    "REASON": "builder_without_coverage",
    "AUTHOR": AUTHOR,
    "RATIONALE": AWKWARD_RATIONALE,
}
_EXPECTED_ARGV = {
    "status": ["status"],
    "report": ["report"],
    "accept": [
        "accept",
        "--report-digest",
        DIGEST,
        "--tenant-id",
        TENANT,
        "--builder-id",
        BUILDER,
        "--reason-code",
        "builder_without_coverage",
        "--author-user-id",
        AUTHOR,
        "--rationale",
        AWKWARD_RATIONALE,
    ],
    "arm": ["arm", "--report-digest", DIGEST],
}


def _need_jq_and_bash() -> None:
    missing = [tool for tool in ("jq", "bash") if shutil.which(tool) is None]
    if missing:
        pytest.skip(f"the runbook snippets need {' and '.join(missing)}")


@pytest.mark.parametrize("step", sorted(_EXPECTED_ARGV))
def test_each_runbook_snippet_builds_the_exact_override(
    tmp_path: Path, step: str
) -> None:
    _need_jq_and_bash()
    result = _run_runbook_step(tmp_path, step, **_VALUES)
    assert result.returncode == 0, result.stderr
    override = json.loads((tmp_path / "overrides.json").read_text(encoding="utf-8"))
    assert override == {
        "containerOverrides": [
            {
                "name": "app",
                "environment": [{"name": "DEBUG", "value": "false"}],
                "command": [
                    "python",
                    "-m",
                    "src.ops.credit_enforcement",
                    *_EXPECTED_ARGV[step],
                ],
            }
        ]
    }
    argv = json.loads((tmp_path / "aws-args.json").read_text(encoding="utf-8"))
    assert argv[:2] == ["ecs", "run-task"]
    assert argv[argv.index("--overrides") + 1] == "file://overrides.json"
    assert argv[argv.index("--started-by") + 1] == (
        f"neurostack-credit-enforcement-{step}"
    )
    # And the task itself parses what arrives (accept: the awkward rationale).
    command = override["containerOverrides"][0]["command"][3:]
    parsed = ops.parse_arguments(command)
    if step == "accept":
        assert parsed.rationale == AWKWARD_RATIONALE


@pytest.mark.parametrize("step", ["accept", "arm"])
def test_a_runbook_step_with_an_unset_value_never_calls_run_task(
    tmp_path: Path, step: str
) -> None:
    _need_jq_and_bash()
    values = {key: value for key, value in _VALUES.items() if key != "DIGEST"}
    result = _run_runbook_step(tmp_path, step, **values)
    assert result.returncode != 0
    assert "DIGEST" in result.stderr
    assert not (tmp_path / "aws-args.json").exists()


def test_run_step_refuses_an_empty_or_foreign_override(tmp_path: Path) -> None:
    _need_jq_and_bash()
    (helper,) = [block for block in _bash_blocks() if "run_step() {" in block]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "aws").write_text(_FAKE_AWS, encoding="utf-8")
    (bin_dir / "aws").chmod(0o755)
    for content in ("", '{"containerOverrides": [{"name": "app", "command": []}]}'):
        (tmp_path / "overrides.json").write_text(content, encoding="utf-8")
        result = subprocess.run(
            ["bash", "-c", helper + "\nrun_step arm"],
            cwd=tmp_path,
            env={
                "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
                "TASK_DEFINITION": "x",
                "SUBNETS": "s",
                "SECURITY_GROUPS": "g",
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode != 0
        assert not (tmp_path / "aws-args.json").exists()
