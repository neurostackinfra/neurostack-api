from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GATES = ROOT / "scripts" / "wish-gates.sh"


def run_pytest_gate(
    tmp_path: Path,
    mode: str,
    transcript: str,
    pytest_status: int,
    *,
    shell_options: str = "",
) -> subprocess.CompletedProcess[str]:
    floor = tmp_path / "floor.txt"
    failures = tmp_path / "failures.txt"
    floor.write_text("2\n" if mode == "focused" else "3\n")
    failures.write_text(
        "FAILED tests/test_x.py::test_x\n" "FAILED tests/test_y.py::test_y\n"
    )
    poetry = tmp_path / "poetry"
    poetry.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1 $2" = "run pytest" ]; then\n'
        '  cat "$PYTEST_TRANSCRIPT"\n'
        '  exit "$PYTEST_STATUS"\n'
        "fi\n"
        'if [ "$1 $2" = "run python" ]; then\n'
        '  echo "validator invoked" >&2\n'
        "  shift 2\n"
        '  exec "$REAL_PYTHON" "$@"\n'
        "fi\n"
        "exit 99\n"
    )
    poetry.chmod(0o755)
    transcript_path = tmp_path / "transcript.txt"
    transcript_path.write_text(transcript)
    function = "core_suite_gate" if mode == "focused" else "full_suite_gate"
    command = (
        f'{shell_options} source "{GATES}"; '
        f'_WISH_PASS_FLOOR="{floor}"; _WISH_OUTCOME_FLOOR="{floor}"; '
        f'_WISH_FAILURES="{failures}"; {function}'
    )
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "PYTEST_TRANSCRIPT": str(transcript_path),
        "PYTEST_STATUS": str(pytest_status),
        "REAL_PYTHON": sys.executable,
    }
    return subprocess.run(
        ["bash", "-c", command],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )


FOCUSED_PASS = """============================= test session starts ==============================
collected 2 items
tests/test_x.py ..
============================== 2 passed in 0.10s ==============================
"""

FULL_EXPECTED = """============================= test session starts ==============================
collected 3 items
tests/test_x.py FF.
FAILED tests/test_x.py::test_x - AssertionError
FAILED tests/test_y.py::test_y - AssertionError
========================= 2 failed, 1 passed in 0.10s =========================
"""


def test_full_gate_accepts_expected_failures_from_errexit_pipefail_caller(
    tmp_path: Path,
) -> None:
    result = run_pytest_gate(
        tmp_path,
        "full",
        FULL_EXPECTED,
        1,
        shell_options="set -eo pipefail;",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "validator invoked" in result.stderr


@pytest.mark.parametrize("status", [2, 3, 4, 5])
def test_full_gate_rejects_incomplete_exit_from_errexit_pipefail_caller(
    tmp_path: Path, status: int
) -> None:
    result = run_pytest_gate(
        tmp_path,
        "full",
        FULL_EXPECTED,
        status,
        shell_options="set -eo pipefail;",
    )
    assert result.returncode == 1
    assert f"pytest exited {status}" in result.stderr
    assert "validator invoked" not in result.stderr


@pytest.mark.parametrize("status", [1, 4])
def test_actual_focused_gate_rejects_every_nonzero_pytest_exit(
    tmp_path: Path, status: int
) -> None:
    result = run_pytest_gate(
        tmp_path,
        "focused",
        FOCUSED_PASS,
        status,
        shell_options="set -eo pipefail;",
    )
    assert result.returncode == 1
    assert f"pytest exited {status}" in result.stderr


def test_actual_focused_gate_rejects_in_scope_parity_failure(tmp_path: Path) -> None:
    transcript = (
        FOCUSED_PASS.replace("collected 2 items", "collected 3 items")
        .replace(
            "tests/test_x.py ..\n",
            "tests/test_x.py F..\nFAILED tests/test_x.py::test_x - AssertionError\n",
        )
        .replace("2 passed", "1 failed, 2 passed")
    )
    result = run_pytest_gate(tmp_path, "focused", transcript, 0)
    assert result.returncode == 1
    assert "focused parity run has failed/error outcomes" in result.stderr


def test_actual_full_gate_rejects_extra_failure(tmp_path: Path) -> None:
    transcript = (
        FULL_EXPECTED.replace("collected 3 items", "collected 4 items")
        .replace(
            "tests/test_x.py FF.",
            "tests/test_x.py FFF.\nFAILED tests/test_z.py::test_z - AssertionError",
        )
        .replace("2 failed, 1 passed", "3 failed, 1 passed")
    )
    result = run_pytest_gate(tmp_path, "full", transcript, 1)
    assert result.returncode == 1
    assert "FAILED/ERROR set drifted" in result.stderr


@pytest.mark.parametrize(
    ("transcript", "reason"),
    [
        (
            "test session starts\ncollected 3 items\ntests/test_x.py ...\n",
            "lacks a completion summary",
        ),
        (
            "test session starts\ncollected 3 items\ntests/test_x.py ..\n2 passed in 0.1s\n",
            "completion summary is partial",
        ),
    ],
)
def test_actual_full_gate_rejects_incomplete_terminal_evidence(
    tmp_path: Path, transcript: str, reason: str
) -> None:
    result = run_pytest_gate(tmp_path, "full", transcript, 0)
    assert result.returncode == 1
    assert reason in result.stderr


# ── asaas_release_candidate_gate ─────────────────────────────────────────────

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
}
# Assembled at runtime so this file never contains the scanned literal.
_PLANTED_PII = "cpf" + "Cnpj"
_PLANTED_BEARER = "Bear" + "er abc.def.ghi"
_FAKE_COMPOSE_ENV = ROOT / "tests" / "fixtures" / "compose" / "production-synthetic.env"
_FRONTEND_SRC = Path("frontend/artifacts/neurostack/src")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        env={**os.environ, **_GIT_ENV},
        check=True,
        capture_output=True,
    )


def _candidate_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "wish-gates.sh").write_text(GATES.read_text())
    validator = ROOT / "scripts" / "validate-conventional-commits.py"
    (repo / "scripts" / "validate-conventional-commits.py").write_text(
        validator.read_text()
    )
    for directory in ("src", "tests", "config", "docs"):
        (repo / directory).mkdir()
        (repo / directory / "README.txt").write_text("fixture\n")
    for name in (
        "docker-compose.yml",
        "docker-compose.production.yml",
        ".env.example",
        "env.example",
    ):
        (repo / name).write_text("# fixture\n")
    (repo / "docs" / "api-contracts.md").write_text("# contracts\n")
    (repo / _FRONTEND_SRC / "lib").mkdir(parents=True)
    (repo / _FRONTEND_SRC / "lib" / "api.ts").write_text("export const api = 1;\n")
    fixture = repo / "tests" / "fixtures" / "compose" / "production-synthetic.env"
    fixture.parent.mkdir(parents=True)
    fixture.write_text(_FAKE_COMPOSE_ENV.read_text())
    # A finding that predates BASE_REF: reported for review, never fatal.
    (repo / "tests" / "legacy_fixture.py").write_text(f"FIELD = '{_PLANTED_PII}'\n")
    (repo / ".gitignore").write_text(".env\n*.local\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: base")
    _git(repo, "tag", "base")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    (bin_dir / "poetry").write_text(
        "#!/usr/bin/env bash\n"
        'echo "poetry $*" >>"$CALL_LOG"\n'
        '[ -z "${CWD_LOG:-}" ] || echo "$PWD" >>"$CWD_LOG"\n'
        'if [ "$1 $2" = "run pytest" ]; then\n'
        '  case "$*" in *"${FAIL_PYTEST_ON:-@none@}"*) exit 1 ;; esac\n'
        "  exit 0\n"
        "fi\n"
        'if [ "$1 $2" = "run python" ]; then shift 2; exec "$REAL_PYTHON" "$@"; fi\n'
        "exit 99\n"
    )
    # The gate runs docker under ``env -i``, so the log path is baked in.
    (bin_dir / "docker").write_text(
        "#!/usr/bin/env bash\n"
        f'echo "docker $*" >>"{log}"\n'
        'echo "docker-env SECRET_KEY=${SECRET_KEY-unset}'
        " ASAAS_API_KEY=${ASAAS_API_KEY-unset}"
        " CALL_LOG=${CALL_LOG-unset}"
        " NEUROSTACK_DEV_STACK=${NEUROSTACK_DEV_STACK-unset}"
        f' OPERATOR_ONLY=${{OPERATOR_ONLY-unset}}" >>"{log}"\n'
        f'if [ -f "{tmp_path / "docker-fails"}" ]; then exit 17; fi\n'
        "exit 0\n"
    )
    for tool in ("poetry", "docker"):
        (bin_dir / tool).chmod(0o755)
    return repo, log


_GATE_AND = "source scripts/wish-gates.sh && asaas_release_candidate_gate"
# bash ignores ``set -e`` inside a function run in an ``||``, ``if`` or ``!``
# context, so the gate must fail through explicit statuses in every form.
_GATE_FORMS = {
    "and": _GATE_AND,
    "or": (
        "source scripts/wish-gates.sh\n"
        "asaas_release_candidate_gate || { echo GATE-FAILED; exit 1; }\n"
        "echo GATE-PASSED\n"
    ),
    "if-not": (
        "source scripts/wish-gates.sh\n"
        "if ! asaas_release_candidate_gate; then echo GATE-FAILED; exit 1; fi\n"
        "echo GATE-PASSED\n"
    ),
}


def _run_release_gate(
    repo: Path, log: Path, *, command: str = _GATE_AND, **env: str
) -> subprocess.CompletedProcess[str]:
    inherited = {
        key: value
        for key, value in os.environ.items()
        if key not in {"BASE_REF", "POSTGRES_SMOKE_DATABASE_URL"}
    }
    return subprocess.run(
        ["bash", "-c", command],
        cwd=repo,
        env={
            **inherited,
            **_GIT_ENV,
            "PATH": f"{log.parent / 'bin'}:{os.environ['PATH']}",
            "CALL_LOG": str(log),
            "REAL_PYTHON": sys.executable,
            **env,
        },
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"POSTGRES_SMOKE_DATABASE_URL": "postgresql://x"}, "set BASE_REF"),
        ({"BASE_REF": "base"}, "set POSTGRES_SMOKE_DATABASE_URL"),
        (
            {
                "BASE_REF": "no-such-ref",
                "POSTGRES_SMOKE_DATABASE_URL": "postgresql://x",
            },
            "does not name a commit",
        ),
    ],
)
def test_release_gate_fails_fast_without_explicit_inputs(
    tmp_path: Path, env: dict[str, str], message: str
) -> None:
    repo, log = _candidate_repo(tmp_path)

    result = _run_release_gate(repo, log, **env)

    assert result.returncode != 0
    assert message in result.stderr
    assert not log.exists()


def test_release_gate_passes_clean_conventional_candidate(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    (repo / "docs" / "api-contracts.md").write_text("# contracts\n\nclean line\n")
    _git(repo, "commit", "-q", "-am", "docs(billing): describe checkout")

    result = _run_release_gate(
        repo, log, BASE_REF="base", POSTGRES_SMOKE_DATABASE_URL="postgresql://x"
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text()
    assert _POSTGRES_SUITES_CALL in calls.splitlines()
    assert "docker compose" in calls
    assert "added-line secret/PII scan: no findings" in result.stdout
    # The pre-existing fixture hit is shown for review but does not fail.
    assert "baseline secret/PII findings (review required)" in result.stdout
    assert "tests/legacy_fixture.py" in result.stdout
    assert "conventional commits: 1 checked" in result.stdout


_SUITES_AND = "source scripts/wish-gates.sh && asaas_release_candidate_postgres_suites"
# The release-candidate PostgreSQL suites, pinned: dropping or adding one is a
# deliberate change to this test, not a silent shrink of the gate and CI job.
_RELEASE_CANDIDATE_POSTGRES_SUITES = (
    "tests/integration/test_asaas_billing_postgres.py",
    "tests/integration/test_asaas_checkout_service_postgres.py",
    "tests/integration/test_asaas_webhook_postgres.py",
    "tests/integration/test_asaas_subscription_triggers_postgres.py",
    "tests/integration/test_billing_producer_handoff_postgres.py",
    "tests/integration/test_migration_036_postgres.py",
    "tests/integration/test_migration_037_postgres.py",
    "tests/integration/test_migration_window_read_postgres.py",
)
_POSTGRES_SUITES_CALL = (
    "poetry run pytest -p no:cacheprovider -rs "
    + " ".join(_RELEASE_CANDIDATE_POSTGRES_SUITES)
    + " -v"
)


def test_postgres_suites_require_the_smoke_url(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)

    result = _run_release_gate(repo, log, command=_SUITES_AND)

    assert result.returncode != 0
    assert "set POSTGRES_SMOKE_DATABASE_URL" in result.stderr
    assert not log.exists()


def test_postgres_suites_run_the_release_candidate_list_from_the_root(
    tmp_path: Path,
) -> None:
    repo, log = _candidate_repo(tmp_path)
    (repo / "elsewhere").mkdir()
    cwd_log = tmp_path / "cwd.log"

    result = _run_release_gate(
        repo,
        log,
        command=(
            "cd elsewhere && source ../scripts/wish-gates.sh && "
            "asaas_release_candidate_postgres_suites && pwd"
        ),
        POSTGRES_SMOKE_DATABASE_URL="postgresql://x",
        CWD_LOG=str(cwd_log),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    # The function runs in a subshell: the caller's directory is unchanged.
    assert result.stdout.strip().endswith("/elsewhere")
    assert log.read_text().splitlines() == [_POSTGRES_SUITES_CALL]
    # ...but pytest itself runs from the repository root.
    assert [os.path.realpath(line) for line in cwd_log.read_text().splitlines()] == [
        os.path.realpath(repo)
    ]


@pytest.mark.parametrize("form", ["and", "or"])
def test_postgres_suites_fail_in_every_calling_context(
    tmp_path: Path, form: str
) -> None:
    repo, log = _candidate_repo(tmp_path)
    command = {
        "and": _SUITES_AND,
        "or": (
            "source scripts/wish-gates.sh\n"
            "asaas_release_candidate_postgres_suites || { echo FAILED; exit 1; }\n"
            "echo PASSED\n"
        ),
    }[form]

    result = _run_release_gate(
        repo,
        log,
        command=command,
        POSTGRES_SMOKE_DATABASE_URL="postgresql://x",
        FAIL_PYTEST_ON="test_billing_producer_handoff_postgres.py",
    )

    assert result.returncode != 0
    assert "release-candidate PostgreSQL suites failed" in result.stderr
    assert "PASSED" not in result.stdout


@pytest.mark.parametrize(
    ("relative", "content", "committed"),
    [
        ("src/billing_payload.py", f"payload = {{'{_PLANTED_PII}': value}}\n", True),
        (
            str(_FRONTEND_SRC / "lib" / "api.ts"),
            f"const header = '{_PLANTED_BEARER}';\n",
            True,
        ),
        ("src/new_module.py", f"FIELD = '{_PLANTED_PII}'\n", False),
        (
            str(_FRONTEND_SRC / "pages" / "new-page.tsx"),
            f"const header = '{_PLANTED_BEARER}';\n",
            False,
        ),
        ("scripts/helper.sh", f"curl -H '{_PLANTED_BEARER}'\n", False),
        ("config/settings.yml", f"field: {_PLANTED_PII}\n", True),
    ],
)
def test_release_gate_rejects_new_hits_across_scanned_roots(
    tmp_path: Path, relative: str, content: str, committed: bool
) -> None:
    repo, log = _candidate_repo(tmp_path)
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    if committed:
        _git(repo, "add", "-A")
        _git(repo, "commit", "-q", "-m", "feat: add file")

    result = _run_release_gate(
        repo, log, BASE_REF="base", POSTGRES_SMOKE_DATABASE_URL="postgresql://x"
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "secret/PII pattern found in added lines" in result.stderr
    assert relative in result.stdout
    assert "conventional commits" not in result.stdout


def test_release_gate_renders_compose_with_synthetic_env_only(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    (repo / "docs" / "api-contracts.md").write_text("# contracts\n\nclean line\n")
    _git(repo, "commit", "-q", "-am", "docs(billing): describe checkout")

    result = _run_release_gate(
        repo,
        log,
        BASE_REF="base",
        POSTGRES_SMOKE_DATABASE_URL="postgresql://x",
        SECRET_KEY="real-operator-secret",
        ASAAS_API_KEY="real-operator-asaas-key",
        OPERATOR_ONLY="leaked",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text()
    fixture = repo / "tests" / "fixtures" / "compose" / "production-synthetic.env"
    assert (
        f"docker compose --env-file {fixture} -f docker-compose.yml "
        "-f docker-compose.production.yml config --quiet"
    ) in calls
    assert f"SECRET_KEY={'S' * 48}" in calls
    assert "ASAAS_API_KEY=test-asaas-api-key" in calls
    assert "OPERATOR_ONLY=unset" in calls
    assert "CALL_LOG=unset" in calls
    # The merged render parses the dev file, which requires the dev-stack flag.
    assert "NEUROSTACK_DEV_STACK=1" in calls
    assert "real-operator" not in calls


def test_release_gate_fails_when_docker_is_unavailable(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    (log.parent / "bin" / "docker").unlink()
    isolated = tmp_path / "isolated-bin"
    isolated.mkdir()
    for tool in ("dirname",):
        found = shutil.which(tool)
        assert found
        (isolated / tool).symlink_to(found)
    (isolated / "poetry").symlink_to(log.parent / "bin" / "poetry")
    bash = shutil.which("bash")
    assert bash

    result = subprocess.run(
        [bash, "-c", "source scripts/wish-gates.sh && asaas_release_candidate_gate"],
        cwd=repo,
        env={
            "PATH": str(isolated),
            "CALL_LOG": str(log),
            "BASE_REF": "base",
            "POSTGRES_SMOKE_DATABASE_URL": "postgresql://x",
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "docker CLI unavailable" in result.stderr
    assert not log.exists()


@pytest.mark.parametrize("committed", [True, False])
def test_release_gate_rejects_added_pii_pattern(
    tmp_path: Path, committed: bool
) -> None:
    repo, log = _candidate_repo(tmp_path)
    (repo / "docs" / "api-contracts.md").write_text(
        f"# contracts\n\nfield {_PLANTED_PII}\n"
    )
    if committed:
        _git(repo, "commit", "-q", "-am", "docs: leak")

    result = _run_release_gate(
        repo, log, BASE_REF="base", POSTGRES_SMOKE_DATABASE_URL="postgresql://x"
    )

    assert result.returncode == 1
    assert "secret/PII pattern found in added lines" in result.stderr
    assert "conventional commits" not in result.stdout


def test_release_gate_rejects_non_conventional_commit(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    (repo / "docs" / "api-contracts.md").write_text("# contracts\n\nclean\n")
    _git(repo, "commit", "-q", "-am", "Update docs")

    result = _run_release_gate(
        repo, log, BASE_REF="base", POSTGRES_SMOKE_DATABASE_URL="postgresql://x"
    )

    assert result.returncode == 1
    assert "non-conventional commit subjects" in result.stdout


_GATE_INPUTS = {"BASE_REF": "base", "POSTGRES_SMOKE_DATABASE_URL": "postgresql://x"}
# Assembled at runtime: neither this file nor the gate's own scan matches them.
_SENTINEL = "SENTINEL" + "VALUE123"
_PLANTED_KEY = "$" + "aact_prod_" + _SENTINEL


def _commit_clean_change(repo: Path) -> None:
    (repo / "docs" / "api-contracts.md").write_text("# contracts\n\nclean line\n")
    _git(repo, "commit", "-q", "-am", "docs(billing): describe checkout")


def _break_pytest_contracts(repo: Path, tmp_path: Path) -> dict[str, str]:
    return {"FAIL_PYTEST_ON": "test_billing_fact_producer_consumer.py"}


def _break_pytest_postgres(repo: Path, tmp_path: Path) -> dict[str, str]:
    return {"FAIL_PYTEST_ON": "test_asaas_billing_postgres.py"}


def _break_docker(repo: Path, tmp_path: Path) -> dict[str, str]:
    (tmp_path / "docker-fails").write_text("")
    return {}


def _break_commit_subject(repo: Path, tmp_path: Path) -> dict[str, str]:
    (repo / "docs" / "README.txt").write_text("fixture\nmore\n")
    _git(repo, "commit", "-q", "-am", "Update docs")
    return {}


def _break_scan(repo: Path, tmp_path: Path) -> dict[str, str]:
    (repo / "docs" / "README.txt").write_text(f"fixture\n{_PLANTED_PII}\n")
    _git(repo, "commit", "-q", "-am", "docs: leak")
    return {}


@pytest.mark.parametrize("form", sorted(_GATE_FORMS))
@pytest.mark.parametrize(
    "breaker",
    [
        _break_pytest_contracts,
        _break_pytest_postgres,
        _break_docker,
        _break_commit_subject,
        _break_scan,
    ],
    ids=["pytest-contracts", "pytest-postgres", "docker", "commits", "scan"],
)
def test_release_gate_fails_in_every_calling_context(
    tmp_path: Path, form: str, breaker
) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    extra = breaker(repo, tmp_path)

    result = _run_release_gate(
        repo, log, command=_GATE_FORMS[form], **_GATE_INPUTS, **extra
    )

    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "GATE-PASSED" not in output
    if form != "and":
        assert "GATE-FAILED" in output


@pytest.mark.parametrize("form", sorted(_GATE_FORMS))
def test_release_gate_passes_clean_candidate_in_every_calling_context(
    tmp_path: Path, form: str
) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)

    result = _run_release_gate(repo, log, command=_GATE_FORMS[form], **_GATE_INPUTS)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "GATE-FAILED" not in result.stdout


@pytest.mark.parametrize("form", sorted(_GATE_FORMS))
def test_release_gate_stops_at_the_first_failing_step(
    tmp_path: Path, form: str
) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)

    result = _run_release_gate(
        repo,
        log,
        command=_GATE_FORMS[form],
        **_GATE_INPUTS,
        FAIL_PYTEST_ON="test_billing_fact_producer_consumer.py",
    )

    assert result.returncode != 0
    calls = log.read_text()
    assert "test_asaas_billing_postgres.py" not in calls
    assert "docker compose" not in calls
    assert "secret/PII" not in result.stdout
    assert "conventional commits" not in result.stdout


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        ("src/leak.py", f"KEY = '{_PLANTED_KEY}'\n"),
        ("tests/leak_test.py", f"KEY = '{_PLANTED_KEY}'\n"),
        ("scripts/leak.sh", f"KEY='{_PLANTED_KEY}'\n"),
        ("config/leak.yml", f"key: {_PLANTED_KEY}\n"),
        ("docs/leak.md", f"key {_PLANTED_KEY}\n"),
        ("docker-compose.yml", f"# fixture\n# {_PLANTED_KEY}\n"),
        (".env.example", f"# fixture\nASAAS_API_KEY={_PLANTED_KEY}\n"),
        ("env.example", f"# fixture\nASAAS_API_KEY={_PLANTED_KEY}\n"),
        (str(_FRONTEND_SRC / "lib" / "leak.ts"), f"const k = '{_PLANTED_KEY}';\n"),
    ],
)
def test_release_gate_reports_committed_hit_in_each_root_without_its_value(
    tmp_path: Path, relative: str, content: str
) -> None:
    repo, log = _candidate_repo(tmp_path)
    target = repo / relative
    target.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "feat: add file")
    line = content.count("\n")

    result = _run_release_gate(repo, log, **_GATE_INPUTS)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "secret/PII pattern found in added lines" in result.stderr
    assert f"{relative}:{line}: asaas_api_key" in result.stdout
    assert _SENTINEL not in output
    assert "aact_prod" not in output
    assert "conventional commits" not in result.stdout


def test_release_gate_never_prints_matched_line_content(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)

    result = _run_release_gate(repo, log, **_GATE_INPUTS)

    assert result.returncode == 0, result.stdout + result.stderr
    # The pre-existing fixture hit is listed by path, line and rule only.
    assert "tests/legacy_fixture.py:1: forbidden_cpf_field" in result.stdout
    assert "FIELD =" not in result.stdout + result.stderr
    assert "not scanned" in result.stdout


def test_release_gate_neither_reads_nor_prints_ignored_files(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    # Ignored by the candidate's .gitignore (*.local): a local credential file.
    (repo / "src" / "creds.local").write_text(f"KEY={_PLANTED_KEY}\n")
    (repo / "docs" / "notes.local").write_text(f"{_PLANTED_PII}\n")

    result = _run_release_gate(repo, log, **_GATE_INPUTS)

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert _SENTINEL not in output
    assert "creds.local" not in output
    assert "notes.local" not in output


def test_release_gate_scans_untracked_non_ignored_files(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    (repo / "src" / "creds.local").write_text(f"KEY={_PLANTED_KEY}\n")
    (repo / "src" / "creds.txt").write_text(f"\nKEY={_PLANTED_KEY}\n")

    result = _run_release_gate(repo, log, **_GATE_INPUTS)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "src/creds.txt:2: asaas_api_key" in result.stdout
    assert "creds.local" not in output
    assert _SENTINEL not in output


def test_release_gate_skips_untracked_symlinks_by_path(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    (repo / ".env").write_text(f"ASAAS_API_KEY={_PLANTED_KEY}\n")
    outside = tmp_path / "outside.txt"
    outside.write_text(f"ASAAS_API_KEY={_PLANTED_KEY}\n")
    (repo / "src" / "env-link").symlink_to("../.env")
    (repo / "docs" / "outside-link").symlink_to(outside)

    result = _run_release_gate(repo, log, **_GATE_INPUTS)

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert _SENTINEL not in output
    assert "skipped non-regular untracked path: src/env-link" in result.stdout
    assert "skipped non-regular untracked path: docs/outside-link" in result.stdout


@pytest.mark.parametrize(
    "added",
    [
        f"++ {_PLANTED_BEARER}",
        f"+++ {_PLANTED_BEARER}",
        f"++ b/docs/other.md {_PLANTED_BEARER}",
    ],
)
def test_release_gate_parses_added_lines_that_look_like_headers(
    tmp_path: Path, added: str
) -> None:
    repo, log = _candidate_repo(tmp_path)
    (repo / "docs" / "api-contracts.md").write_text(f"# contracts\n\n{added}\n")
    _git(repo, "commit", "-q", "-am", "docs: header-like line")

    result = _run_release_gate(repo, log, **_GATE_INPUTS)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "docs/api-contracts.md:3: bearer_token" in result.stdout
    assert "abc.def.ghi" not in output


def test_release_gate_ignores_diff_noprefix_configuration(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    _git(repo, "config", "diff.noprefix", "true")
    _git(repo, "config", "diff.mnemonicPrefix", "true")
    (repo / "docs" / "api-contracts.md").write_text(f"# contracts\n{_PLANTED_PII}\n")
    _git(repo, "commit", "-q", "-am", "docs: leak")

    result = _run_release_gate(repo, log, **_GATE_INPUTS)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "docs/api-contracts.md:2: forbidden_cpf_field" in result.stdout


def test_release_gate_keeps_renamed_preexisting_hits_review_only(
    tmp_path: Path,
) -> None:
    repo, log = _candidate_repo(tmp_path)
    _git(repo, "config", "diff.renames", "false")
    _git(repo, "mv", "tests/legacy_fixture.py", "src/legacy_fixture.py")
    _git(repo, "commit", "-q", "-m", "refactor: move fixture")

    result = _run_release_gate(repo, log, **_GATE_INPUTS)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "  src/legacy_fixture.py:1: forbidden_cpf_field" in result.stdout
    assert "added-line secret/PII scan: no findings" in result.stdout


def test_release_gate_rejects_hit_added_to_a_renamed_file(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    # Large enough that git still pairs the move as a rename after the edit.
    body = "".join(f"VALUE_{index} = {index}\n" for index in range(20))
    (repo / "tests" / "big_fixture.py").write_text(f"FIELD = '{_PLANTED_PII}'\n{body}")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "test: fixture")
    _git(repo, "tag", "-f", "base")
    _git(repo, "mv", "tests/big_fixture.py", "src/legacy_fixture.py")
    (repo / "src" / "legacy_fixture.py").write_text(
        f"FIELD = '{_PLANTED_PII}'\n\nKEY = '{_PLANTED_KEY}'\n{body}"
    )
    _git(repo, "commit", "-q", "-am", "refactor: move fixture")

    result = _run_release_gate(repo, log, **_GATE_INPUTS)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "\nsrc/legacy_fixture.py:3: asaas_api_key" in result.stdout
    assert "\nsrc/legacy_fixture.py:1:" not in result.stdout
    assert _SENTINEL not in output


def test_release_gate_scans_patch_content_not_patch_headers(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    # The rule literal is in the new file's name only; the header block
    # (`+++ b/...`) before the first hunk is never scanned as content.
    (repo / "docs" / f"{_PLANTED_PII}-notes.md").write_text("clean\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "docs: add notes")

    result = _run_release_gate(repo, log, **_GATE_INPUTS)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "added-line secret/PII scan: no findings" in result.stdout


@pytest.mark.parametrize(
    ("failing", "message"),
    [
        ("grep --untracked", "baseline secret/PII scan failed"),
        ("--numstat", "added-line secret/PII scan failed"),
        ("--unified=0", "added-line secret/PII scan failed"),
        ("ls-files -z --others", "added-line secret/PII scan failed"),
    ],
)
def test_release_gate_fails_when_a_scan_cannot_run(
    tmp_path: Path, failing: str, message: str
) -> None:
    repo, log = _candidate_repo(tmp_path)
    (repo / "docs" / "README.txt").write_text("fixture\nclean\n")
    _git(repo, "commit", "-q", "-am", "docs: clean")
    real_git = shutil.which("git")
    assert real_git
    wrapper = log.parent / "bin" / "git"
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        'case " $* " in *" $FAIL_GIT_ON "*) exit 128 ;; esac\n'
        f'exec "{real_git}" "$@"\n'
    )
    wrapper.chmod(0o755)

    for form in sorted(_GATE_FORMS):
        result = _run_release_gate(
            repo, log, command=_GATE_FORMS[form], **_GATE_INPUTS, FAIL_GIT_ON=failing
        )

        assert result.returncode != 0, result.stdout + result.stderr
        assert message in result.stderr
        assert "GATE-PASSED" not in result.stdout
        assert "conventional commits" not in result.stdout


# ── secret/PII scan waivers (owner decision 2026-10-18) ─────────────────────
# The release gate waives a closed list of exact synthetic lines in the
# sandbox card capture tool's files only, and audits them as a whole. These
# tests drive the mechanism through a test-owned table injected by
# redefining `_asaas_scan_waivers`; the real table is checked structurally
# below and against the tool's bytes by the gate itself. Rule literals are
# assembled at runtime so this module never matches the scan itself.
_CPF_FIELD = "cpf" + "Cnpj"
_CARD_FIELD = "credit" + "Card"
_KEY_HMLG = "$" + "aact_hmlg_"
_KEY_PROD = "$" + "aact_prod_"
_CVV_TERM = "c" + "vv"
_CCV_TERM = "c" + "cv"
_ACCESS_HEADER = "asaas-access-" + "token: abc"
_CAPTURE_TOOL = "scripts/asaas_sandbox_card_capture.py"
_CAPTURE_TESTS = "tests/scripts/test_asaas_sandbox_card_capture.py"
_CAPTURE_RUNBOOK = "docs/runbooks/asaas-sandbox-card-capture.md"
_DECISION = "owner decision 2026-10-18, wish asaas-payment-gateway"
_WAIVED = f"waived ({_DECISION}): "
_FAKE_KEY_TAIL = "FAKEcapture-test-key-0001"
_REAL_LOOKING_TAIL = "Zx9Qm2LwPb7RtY4uVk8NcJ3hGs6FdA1eXo5iTyMn"
_OTHER_PAN = "5162306219378829"
_MALFORMED = "malformed scan rules or waiver table"
# (line, rules) fixture lines: the rules each line hits, all of them waived;
# () marks a pinned line that hits no rule.
_TOOL_LINES: list[tuple[str, tuple[str, ...]]] = [
    (f'        "{_CPF_FIELD}": identity.cpf,', ("forbidden_cpf_field",)),
    (f'        "{_CARD_FIELD}": {{', ("forbidden_card_field",)),
    ('            "number": TEST_CARD_NUMBER,', ()),
    (f'            "{_CCV_TERM}": TEST_CARD_CCV,', ("payment_security_term",)),
    (f'        "{_CARD_FIELD}HolderInfo": {{', ("forbidden_card_field",)),
    ('TEST_CARD_NUMBER = "4444444444444444"', ()),
    ('TEST_CARD_CCV = "123"', ()),
]
_TEST_LINES: list[tuple[str, tuple[str, ...]]] = [
    (f'SANDBOX_KEY = "{_KEY_HMLG}{_FAKE_KEY_TAIL}"', ("asaas_api_key",)),
    (f'PROD_KEY = "{_KEY_PROD}{_FAKE_KEY_TAIL}"', ("asaas_api_key",)),
    (
        f'    assert payload["{_CARD_FIELD}"]["number"] == "4444444444444444"',
        ("forbidden_card_field",),
    ),
    (
        f'    assert payload["{_CARD_FIELD}"]["{_CCV_TERM}"] == "123"',
        ("forbidden_card_field", "payment_security_term"),
    ),
]
_RUNBOOK_PIN = "  `4444 4444 4444 4444`, with any future expiry and CCV `123`"
_RUNBOOK_LINES: list[tuple[str, tuple[str, ...]]] = [(_RUNBOOK_PIN, ())]
_WAIVED_COUNT = sum(len(rules) for _, rules in _TOOL_LINES + _TEST_LINES)
_TEST_TABLE = [
    (
        _CAPTURE_TOOL,
        "forbidden_cpf_field",
        1,
        '[[:space:]]*"cpf[C]npj": identity[.]cpf,',
    ),
    (_CAPTURE_TOOL, "forbidden_card_field", 1, '[[:space:]]*"credit[C]ard": [{]'),
    (_CAPTURE_TOOL, "pinned_line", 1, '[[:space:]]*"number": TEST_CARD_NUMBER,'),
    (_CAPTURE_TOOL, "pinned_line", 1, '[[:space:]]*"c[c]v": TEST_CARD_CCV,'),
    (
        _CAPTURE_TOOL,
        "forbidden_card_field",
        1,
        '[[:space:]]*"credit[C]ardHolderInfo": [{]',
    ),
    (_CAPTURE_TOOL, "pinned_line", 1, 'TEST_CARD_NUMBER = "4444444444444444"'),
    (_CAPTURE_TOOL, "pinned_line", 1, 'TEST_CARD_CCV = "123"'),
    (
        _CAPTURE_TESTS,
        "asaas_api_key",
        1,
        'SANDBOX_KEY = "[$]aact_hmlg_FAKEcapture-test-key-0001"',
    ),
    (
        _CAPTURE_TESTS,
        "asaas_api_key",
        1,
        'PROD_KEY = "[$]aact_prod_FAKEcapture-test-key-0001"',
    ),
    (
        _CAPTURE_TESTS,
        "forbidden_card_field",
        1,
        '[[:space:]]*assert payload[[]"credit[C]ard"[]][[]"number"[]] == "4444444444444444"',
    ),
    (
        _CAPTURE_TESTS,
        "forbidden_card_field",
        1,
        '[[:space:]]*assert payload[[]"credit[C]ard"[]][[]"c[c]v"[]] == "123"',
    ),
    # The security-code rule on lines already pinned or waived for another rule.
    (
        _CAPTURE_TOOL,
        "payment_security_term",
        1,
        '[[:space:]]*"c[c]v": TEST_CARD_CCV,',
    ),
    (
        _CAPTURE_TESTS,
        "payment_security_term",
        1,
        '[[:space:]]*assert payload[[]"credit[C]ard"[]][[]"c[c]v"[]] == "123"',
    ),
    (
        _CAPTURE_RUNBOOK,
        "pinned_line",
        1,
        "[[:space:]]*`4444 4444 4444 4444`, with any future expiry and CCV `123`",
    ),
]
# Entry numbers in _TEST_TABLE (1-based) for targeted assertions.
_ENTRY_TOOL_CARD_OPENER = 2
_ENTRY_TOOL_CCV_PIN = 7
_ENTRY_TESTS_PROD_KEY = 9
_ENTRY_TOOL_CCV_TERM = 12
_ENTRY_RUNBOOK_PIN = 14


def _table_override(rows: list[tuple[str, str, int, str]]) -> str:
    body = "".join(f"{_DECISION}\t{p}\t{r}\t{c}\t{e}\n" for p, r, c, e in rows)
    return f"_asaas_scan_waivers() {{\ncat <<'TABLE'\n{body}TABLE\n}}\n"


def _raw_table_override(lines: list[str]) -> str:
    body = "".join(f"{line}\n" for line in lines)
    return f"_asaas_scan_waivers() {{\ncat <<'TABLE'\n{body}TABLE\n}}\n"


def _run_waiver_gate(
    repo: Path,
    log: Path,
    rows: list[tuple[str, str, int, str]] | None = None,
    *,
    override: str | None = None,
    **env: str,
) -> subprocess.CompletedProcess[str]:
    table = override if override is not None else _table_override(rows or _TEST_TABLE)
    command = f"source scripts/wish-gates.sh\n{table}asaas_release_candidate_gate"
    return _run_release_gate(repo, log, command=command, **_GATE_INPUTS, **env)


def _write_lines(
    repo: Path, relative: str, lines: list[str], newline: str = "\n"
) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    # Line 1 is filler so reported numbers start at 2.
    text = "# synthetic\n" + "".join(f"{line}{newline}" for line in lines)
    target.write_bytes(text.encode())


def _capture_lines(
    fixture: list[tuple[str, tuple[str, ...]]], replace: dict[str, str]
) -> list[str]:
    lines = [replace.get(line, line) for line, _ in fixture]
    assert all(old in [line for line, _ in fixture] for old in replace), replace
    return lines


def _write_capture_files(
    repo: Path,
    *,
    tool_path: str = _CAPTURE_TOOL,
    test_path: str = _CAPTURE_TESTS,
    runbook_path: str | None = _CAPTURE_RUNBOOK,
    extra_tool: tuple[str, ...] = (),
    extra_test: tuple[str, ...] = (),
    extra_runbook: tuple[str, ...] = (),
    replace: dict[str, str] | None = None,
    newline: str = "\n",
) -> None:
    replace = replace or {}
    tool_replace = {k: v for k, v in replace.items() if k in dict(_TOOL_LINES)}
    test_replace = {k: v for k, v in replace.items() if k in dict(_TEST_LINES)}
    book_replace = {k: v for k, v in replace.items() if k in dict(_RUNBOOK_LINES)}
    assert len(tool_replace) + len(test_replace) + len(book_replace) == len(
        replace
    ), replace
    tool = _capture_lines(_TOOL_LINES, tool_replace) + list(extra_tool)
    tests = _capture_lines(_TEST_LINES, test_replace) + list(extra_test)
    if runbook_path is not None:
        book = _capture_lines(_RUNBOOK_LINES, book_replace) + list(extra_runbook)
        _write_lines(repo, runbook_path, book, newline)
    if tool_path == test_path:
        _write_lines(repo, tool_path, tool + tests, newline)
        return
    _write_lines(repo, tool_path, tool, newline)
    _write_lines(repo, test_path, tests, newline)


def _commit_all(repo: Path, message: str = "test(billing): add capture tool") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _waived_lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if _WAIVED in line]


def _stale_lines(stderr: str) -> list[str]:
    return [
        line
        for line in stderr.splitlines()
        if line.startswith("warning: stale secret/PII waiver")
    ]


def _expected_waived() -> list[str]:
    expected = [
        f"{_CAPTURE_TOOL}:{n} rule={rule}"
        for n, (_, rules) in enumerate(_TOOL_LINES, 2)
        for rule in rules
    ]
    expected += [
        f"{_CAPTURE_TESTS}:{n} rule={rule}"
        for n, (_, rules) in enumerate(_TEST_LINES, 2)
        for rule in rules
    ]
    return expected


@pytest.mark.parametrize("committed", [True, False], ids=["committed", "untracked"])
def test_waiver_passes_and_prints_exact_synthetic_lines(
    tmp_path: Path, committed: bool
) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    _write_capture_files(repo)
    if committed:
        _commit_all(repo)

    result = _run_waiver_gate(repo, log)

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "added-line secret/PII scan: no findings" in result.stdout
    assert "waived-path audit: no findings" in result.stdout
    for item in _expected_waived():
        assert f"\n{_WAIVED}{item}\n" in result.stdout, item
    assert f"added-line secret/PII waived: {_WAIVED_COUNT}\n" in result.stdout
    assert not _stale_lines(result.stderr)
    # The matched text and line content are never printed.
    assert "4444444444444444" not in output
    assert _FAKE_KEY_TAIL not in output
    assert "identity.cpf" not in output


@pytest.mark.parametrize(
    "other_path",
    [
        "scripts/asaas_sandbox_card_capture_v2.py",  # copy / rename target
        "scripts/asaas_sandbox_card_capture.py.orig",  # prefix of a waived path
        "tests/scripts/asaas_sandbox_card_capture.py",  # waived path as suffix
        "src/tests/scripts/test_asaas_sandbox_card_capture.py",  # as suffix
        "scripts/ASAAS_sandbox_card_capture.py",  # case variant
        "scripts/asaas_sandbox_card_captureXpy",  # `.` read as a regex
        "docs/asaas-sandbox-card-capture.md",
    ],
)
@pytest.mark.parametrize("committed", [True, False], ids=["committed", "untracked"])
def test_waived_lines_fail_in_any_other_path(
    tmp_path: Path, other_path: str, committed: bool
) -> None:
    repo, log = _candidate_repo(tmp_path)
    _write_capture_files(repo, tool_path=other_path, test_path=other_path)
    if committed:
        _commit_all(repo)

    result = _run_waiver_gate(repo, log)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "secret/PII pattern found in added lines" in result.stderr
    for number, (_, rules) in enumerate(_TOOL_LINES + _TEST_LINES, 2):
        for rule in rules:
            assert f"\n{other_path}:{number}: {rule}\n" in result.stdout, rule
    assert not [line for line in _waived_lines(output) if other_path in line]


_OTHER_VALUES = [
    # A different card number on an otherwise waived line shape.
    (
        _CAPTURE_TESTS,
        f'    assert payload["{_CARD_FIELD}"]["number"] == "{_OTHER_PAN}"',
        "forbidden_card_field",
    ),
    # A different security code.
    (
        _CAPTURE_TESTS,
        f'    assert payload["{_CARD_FIELD}"]["{_CCV_TERM}"] == "987"',
        "forbidden_card_field",
    ),
    # A key without the synthetic tail.
    (_CAPTURE_TESTS, f'PROD_KEY = "{_KEY_PROD}{_REAL_LOOKING_TAIL}"', "asaas_api_key"),
    # A real-looking tail appended to the synthetic key.
    (
        _CAPTURE_TESTS,
        f'PROD_KEY = "{_KEY_PROD}{_FAKE_KEY_TAIL}{_REAL_LOOKING_TAIL}"',
        "asaas_api_key",
    ),
    # A carriage return inside the pinned text is not the pinned line.
    (
        _CAPTURE_TESTS,
        f'PROD_KEY = "{_KEY_PROD}FAKE\rcapture-test-key-0001"',
        "asaas_api_key",
    ),
    # A live key prepended to, or appended after, a waived line.
    (
        _CAPTURE_TESTS,
        f'LIVE = "{_KEY_PROD}{_REAL_LOOKING_TAIL}"; '
        f'PROD_KEY = "{_KEY_PROD}{_FAKE_KEY_TAIL}"',
        "asaas_api_key",
    ),
    (
        _CAPTURE_TESTS,
        f'PROD_KEY = "{_KEY_PROD}{_FAKE_KEY_TAIL}"; '
        f'LIVE = "{_KEY_PROD}{_REAL_LOOKING_TAIL}"',
        "asaas_api_key",
    ),
    # An email riding on a waived field line.
    (
        _CAPTURE_TOOL,
        f'        "{_CPF_FIELD}": identity.cpf, "email": "maria.silva@gmail.com",',
        "forbidden_cpf_field",
    ),
    # A CPF-looking value that is not the generated identity.
    (_CAPTURE_TOOL, f'        "{_CPF_FIELD}": "39053344705",', "forbidden_cpf_field"),
    # A private key block on a waived rule's line.
    (
        _CAPTURE_TOOL,
        f'        "{_CARD_FIELD}": "-----BEGIN PRIVATE KEY-----MIIEvQIBADANBgkq",',
        "forbidden_card_field",
    ),
    # Rules with no waiver at all still run on the waived paths.
    (_CAPTURE_TESTS, f"AUTH = '{_PLANTED_BEARER}'", "bearer_token"),
    (_CAPTURE_TOOL, f"HEADER = '{_ACCESS_HEADER}'", "asaas_access_token_header"),
    (_CAPTURE_TOOL, f"TERM = '{_CVV_TERM}'", "payment_security_term"),
    # The provider's security-code field on a line that is not pinned.
    (_CAPTURE_TOOL, f'            "{_CCV_TERM}": "987",', "payment_security_term"),
    (_CAPTURE_TESTS, f'CARD = {{"{_CCV_TERM}": "987"}}', "payment_security_term"),
    (_CAPTURE_RUNBOOK, f'Send `"{_CCV_TERM}": "987"` instead', "payment_security_term"),
    (_CAPTURE_RUNBOOK, f"AUTH = '{_PLANTED_BEARER}'", "bearer_token"),
]


@pytest.mark.parametrize(
    ("relative", "offending", "rule"),
    _OTHER_VALUES,
    ids=[f"{rule}-{index}" for index, (_, _, rule) in enumerate(_OTHER_VALUES)],
)
@pytest.mark.parametrize("committed", [True, False], ids=["committed", "untracked"])
def test_waiver_does_not_cover_other_values_in_waived_paths(
    tmp_path: Path, relative: str, offending: str, rule: str, committed: bool
) -> None:
    repo, log = _candidate_repo(tmp_path)
    if relative == _CAPTURE_TOOL:
        _write_capture_files(repo, extra_tool=(offending,))
        number = len(_TOOL_LINES) + 2
    elif relative == _CAPTURE_RUNBOOK:
        _write_capture_files(repo, extra_runbook=(offending,))
        number = len(_RUNBOOK_LINES) + 2
    else:
        _write_capture_files(repo, extra_test=(offending,))
        number = len(_TEST_LINES) + 2
    if committed:
        _commit_all(repo)

    result = _run_waiver_gate(repo, log)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "secret/PII pattern found in added lines" in result.stderr
    assert f"\n{relative}:{number}: {rule}\n" in result.stdout
    assert f"{relative}:{number} rule=" not in output
    # The genuine synthetic lines are still waived, and still printed.
    assert f"added-line secret/PII waived: {_WAIVED_COUNT}\n" in result.stdout
    assert _REAL_LOOKING_TAIL not in output
    assert _OTHER_PAN not in output
    assert "maria.silva" not in output


_TOOL_NUMBER_PIN = 'TEST_CARD_NUMBER = "4444444444444444"'
_TOOL_CCV_PIN = 'TEST_CARD_CCV = "123"'
_PAYLOAD_NUMBER = '            "number": TEST_CARD_NUMBER,'
_PAYLOAD_CCV = f'            "{_CCV_TERM}": TEST_CARD_CCV,'
_SPACED_PAN = "5162 3062 1937 8829"
_DASHED_PAN = "5162-3062-1937-8829"
_UNDERSCORED_PAN = "5162_3062_1937_8829"
_DOTTED_PAN = "5162.3062.1937.8829"
# (id, file edits, expected stdout fragments) for the waived-path audit.
_CARD_CHANGES = [
    (
        "number-constant",
        {"replace": {_TOOL_NUMBER_PIN: f'TEST_CARD_NUMBER = "{_OTHER_PAN}"'}},
        [f"\n{_CAPTURE_TOOL}:7: card_number_guard\n", "pinned line entry 6 matches 0"],
    ),
    (
        "security-code-constant",
        {"replace": {_TOOL_CCV_PIN: 'TEST_CARD_CCV = "999"'}},
        ["pinned line entry 7 matches 0 lines, expected 1"],
    ),
    (
        "payload-number",
        {"replace": {_PAYLOAD_NUMBER: f'            "number": "{_SPACED_PAN}",'}},
        [f"\n{_CAPTURE_TOOL}:4: card_number_guard\n", "pinned line entry 3 matches 0"],
    ),
    (
        "payload-security-code",
        {"replace": {_PAYLOAD_CCV: f'            "{_CCV_TERM}": "999",'}},
        ["pinned line entry 4 matches 0 lines, expected 1"],
    ),
    (
        "copied-opener-block",
        {
            "extra_tool": (
                f'        "{_CARD_FIELD}": {{',
                f'            "number": "{_OTHER_PAN}",',
                "        },",
            )
        },
        [
            f"{_CAPTURE_TOOL}: waiver entry {_ENTRY_TOOL_CARD_OPENER} "
            "rule=forbidden_card_field matches 2 lines, more than the 1 recorded",
            f"\n{_CAPTURE_TOOL}:10: card_number_guard\n",
        ],
    ),
    (
        "copied-pin",
        {"extra_tool": (_PAYLOAD_CCV,)},
        ["pinned line entry 4 matches 2 lines, expected 1"],
    ),
    (
        "bare-number-in-tests",
        {"extra_test": (f'CARD = "{_DASHED_PAN}"',)},
        [f"\n{_CAPTURE_TESTS}:6: card_number_guard\n"],
    ),
    (
        "thirteen-digits-in-tests",
        {"extra_test": ("VALUE = 1000000000000",)},
        [f"\n{_CAPTURE_TESTS}:6: card_number_guard\n"],
    ),
    (
        "longer-run-in-tests",
        {"extra_test": ("VALUE = 44444444444444441",)},
        [f"\n{_CAPTURE_TESTS}:6: card_number_guard\n"],
    ),
    (
        "underscored-number-in-tests",
        {"extra_test": (f"CARD = {_UNDERSCORED_PAN}",)},
        [f"\n{_CAPTURE_TESTS}:6: card_number_guard\n"],
    ),
    (
        "dotted-number-in-tests",
        {"extra_test": (f'CARD = "{_DOTTED_PAN}"',)},
        [f"\n{_CAPTURE_TESTS}:6: card_number_guard\n"],
    ),
    # The second digit run on a line is checked too ("2" comes first here).
    (
        "second-run-on-line",
        {"extra_test": (f'CARD_V2 = "{_OTHER_PAN}"',)},
        [f"\n{_CAPTURE_TESTS}:6: card_number_guard\n"],
    ),
    (
        "seventeen-fours-in-tests",
        {"extra_test": ("VALUE = 44444444444444444",)},
        [f"\n{_CAPTURE_TESTS}:6: card_number_guard\n"],
    ),
    (
        "thirteen-fours-in-tests",
        {"extra_test": ("VALUE = 4444444444444",)},
        [f"\n{_CAPTURE_TESTS}:6: card_number_guard\n"],
    ),
    # N27: a copied waived line on its own, with no card number on it.
    (
        "copied-opener-only",
        {"extra_tool": (f'        "{_CARD_FIELD}": {{',)},
        [
            f"{_CAPTURE_TOOL}: waiver entry {_ENTRY_TOOL_CARD_OPENER} "
            "rule=forbidden_card_field matches 2 lines, more than the 1 recorded"
        ],
    ),
    # The tool's runbook is audited like the tool.
    (
        "runbook-card-number",
        {"extra_runbook": (f"Use `{_SPACED_PAN}` for declines",)},
        [f"\n{_CAPTURE_RUNBOOK}:3: card_number_guard\n"],
    ),
    (
        "runbook-pin-changed",
        {"replace": {_RUNBOOK_PIN: _RUNBOOK_PIN.replace("`123`", "`999`")}},
        [f"pinned line entry {_ENTRY_RUNBOOK_PIN} matches 0 lines, expected 1"],
    ),
    (
        "runbook-pin-copied",
        {"extra_runbook": (_RUNBOOK_PIN,)},
        [f"pinned line entry {_ENTRY_RUNBOOK_PIN} matches 2 lines, expected 1"],
    ),
    # A copied security-code payload line is over its recorded count.
    (
        "security-code-line-copied",
        {"extra_tool": (_PAYLOAD_CCV,)},
        [
            f"{_CAPTURE_TOOL}: waiver entry {_ENTRY_TOOL_CCV_TERM} "
            "rule=payment_security_term matches 2 lines, more than the 1 recorded"
        ],
    ),
]


@pytest.mark.parametrize(
    ("edits", "expected"),
    [(edits, expected) for _, edits, expected in _CARD_CHANGES],
    ids=[name for name, _, _ in _CARD_CHANGES],
)
@pytest.mark.parametrize("committed", [True, False], ids=["committed", "untracked"])
def test_waived_path_audit_rejects_any_other_card_data(
    tmp_path: Path, edits: dict, expected: list[str], committed: bool
) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    _write_capture_files(repo, **edits)
    if committed:
        _commit_all(repo)

    result = _run_waiver_gate(repo, log)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "secret/PII waiver audit failed" in result.stderr
    for fragment in expected:
        assert fragment in output, fragment
    for digits in (
        _OTHER_PAN,
        _SPACED_PAN,
        _DASHED_PAN,
        _UNDERSCORED_PAN,
        _DOTTED_PAN,
        "1000000000000",
        "3062",
    ):
        assert digits not in output
    assert "conventional commits" not in result.stdout


def test_waived_path_audit_runs_even_when_the_files_predate_base(
    tmp_path: Path,
) -> None:
    repo, log = _candidate_repo(tmp_path)
    _write_capture_files(repo, extra_test=(f'CARD = "{_OTHER_PAN}"',))
    _commit_all(repo)
    _git(repo, "tag", "-f", "base")
    _commit_clean_change(repo)

    result = _run_waiver_gate(repo, log)

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"\n{_CAPTURE_TESTS}:6: card_number_guard\n" in result.stdout
    assert "added-line secret/PII scan: no findings" in result.stdout


def test_waived_path_audit_allows_the_public_test_card_forms(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    _write_capture_files(
        repo,
        extra_tool=(
            "# card 4444 4444 4444 4444 or 4444-4444-4444-4444",
            "# also 4444_4444_4444_4444 and 4444.4444.4444.4444",
            "VERSION = 1.2.3, HOST = 198.51.100.7, RATE = 0.125",
        ),
        extra_test=('PHONE = "4738010919"', 'CPF = "52998224725"'),
    )
    _commit_all(repo)

    result = _run_waiver_gate(repo, log)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "card_number_guard" not in result.stdout


def test_card_guard_covers_waived_paths_only(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    _write_capture_files(repo)
    (repo / "src" / "orders.py").write_text(f'REF = "{_OTHER_PAN}"\n')
    _commit_all(repo)

    result = _run_waiver_gate(repo, log)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "card_number_guard" not in result.stdout


@pytest.mark.parametrize(
    ("kind", "message"),
    [
        ("binary", "waived path contains a NUL byte and cannot be audited"),
        ("symlink", "waived path is not a regular file and cannot be audited"),
    ],
)
@pytest.mark.parametrize("relative", [_CAPTURE_TESTS, _CAPTURE_RUNBOOK])
def test_waived_path_that_cannot_be_audited_fails(
    tmp_path: Path, kind: str, message: str, relative: str
) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    _write_capture_files(repo)
    target = repo / relative
    if kind == "binary":
        with target.open("ab") as handle:
            handle.write(b"\x00binary\n")
    else:
        moved = target.parent / f"elsewhere{target.suffix}"
        target.rename(moved)
        target.symlink_to(moved.name)
    _commit_all(repo)

    result = _run_waiver_gate(repo, log)

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"{message}: {relative}" in result.stdout
    assert "secret/PII waiver audit failed" in result.stderr


@pytest.mark.parametrize("awk", ["gawk", "mawk"])
def test_waiver_never_matches_across_an_embedded_nul(tmp_path: Path, awk: str) -> None:
    found = shutil.which(awk)
    if found is None:
        pytest.skip(f"{awk} is not installed")
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    # Past git's 8000-byte binary probe the file is diffed as text, so the
    # line with the NUL reaches the scanner.
    filler = tuple(f"# filler line {index:04d} " + "x" * 60 for index in range(150))
    planted = f'PROD_KEY = "{_KEY_PROD}{_FAKE_KEY_TAIL}"\0; LIVE = "{_KEY_PROD}{_REAL_LOOKING_TAIL}"'
    _write_capture_files(repo, extra_test=filler + (planted,))
    _commit_all(repo)
    awk_dir = tmp_path / "awk-bin"
    awk_dir.mkdir()
    (awk_dir / "awk").symlink_to(found)

    result = _run_waiver_gate(
        repo, log, PATH=f"{awk_dir}:{log.parent / 'bin'}:{os.environ['PATH']}"
    )

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    number = len(_TEST_LINES) + len(filler) + 2
    assert f"\n{_CAPTURE_TESTS}:{number}: asaas_api_key\n" in result.stdout
    assert f"{_CAPTURE_TESTS}:{number} rule=" not in output
    assert _REAL_LOOKING_TAIL not in output


def test_waiver_does_not_follow_a_rename(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    filler = [f"VALUE_{index} = {index}" for index in range(30)]
    _write_capture_files(repo, extra_tool=tuple(filler))
    _commit_all(repo)
    _git(repo, "tag", "-f", "base")
    moved = "scripts/asaas_card_capture_moved.py"
    _git(repo, "mv", _CAPTURE_TOOL, moved)
    with (repo / moved).open("a") as handle:
        handle.write(f"{_TOOL_LINES[0][0]}\n")
    _commit_all(repo, "refactor: move capture tool")

    result = _run_waiver_gate(repo, log)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    number = len(_TOOL_LINES) + len(filler) + 2
    assert f"\n{moved}:{number}: forbidden_cpf_field\n" in result.stdout
    # Only the added line is new; the moved lines stay review-only.
    assert f"\n{moved}:2: forbidden_cpf_field\n" not in result.stdout


def test_stale_waiver_entries_warn_without_failing(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    # The tool lost its holder-info line; the tests file is gone entirely.
    _write_lines(
        repo,
        _CAPTURE_TOOL,
        [line for line, _ in _TOOL_LINES[:4]] + [_TOOL_NUMBER_PIN, _TOOL_CCV_PIN],
    )
    _commit_all(repo)

    result = _run_waiver_gate(repo, log)

    assert result.returncode == 0, result.stdout + result.stderr
    stale = _stale_lines(result.stderr)
    assert all(_DECISION in line for line in stale)
    assert [
        x
        for x in stale
        if "entry 5 " in x
        and f"{_CAPTURE_TOOL} rule=forbidden_card_field matches 0 of 1" in x
    ]
    tests_entries = [row for row in _TEST_TABLE if row[0] == _CAPTURE_TESTS]
    assert len([x for x in stale if f"{_CAPTURE_TESTS} rule=" in x]) == len(
        tests_entries
    )
    assert not [x for x in stale if f"{_CAPTURE_TOOL} rule=forbidden_cpf_field" in x]
    assert (
        f"waived path is not in the tree; its entries are stale: {_CAPTURE_TESTS}"
        in result.stdout
    )
    assert f"{_WAIVED}{_CAPTURE_TOOL}:2 rule=forbidden_cpf_field" in result.stdout


def test_partially_stale_entry_warns(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    _write_capture_files(repo)
    _commit_all(repo)
    rows = list(_TEST_TABLE)
    path, rule, _, pattern = rows[0]
    rows[0] = (path, rule, 2, pattern)

    result = _run_waiver_gate(repo, log, rows)

    assert result.returncode == 0, result.stdout + result.stderr
    stale = _stale_lines(result.stderr)
    assert len(stale) == 1
    assert (
        f"entry 1 ({_DECISION}): {_CAPTURE_TOOL} rule=forbidden_cpf_field matches 1 of 2"
        in stale[0]
    )


def test_missing_waived_path_only_warns_for_its_pins(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)

    result = _run_waiver_gate(repo, log)

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(_stale_lines(result.stderr)) == len(_TEST_TABLE)
    assert "waived-path audit: no findings" in result.stdout


def test_baseline_scan_prints_and_counts_waived_lines(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    offending = f'    assert payload["{_CARD_FIELD}"]["number"] == "1234"'
    _write_capture_files(repo, extra_test=(offending,))
    _commit_all(repo)
    # The files predate BASE_REF: only the review-only baseline sees them.
    _git(repo, "tag", "-f", "base")
    _commit_clean_change(repo)

    result = _run_waiver_gate(repo, log)

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "baseline secret/PII findings (review required)" in result.stdout
    for item in _expected_waived():
        assert f"\n  {_WAIVED}{item}\n" in result.stdout, item
        path, rest = item.split(":", 1)
        number = rest.split(" ", 1)[0]
        assert f"  {path}:{number}: " not in result.stdout
    number = len(_TEST_LINES) + 2
    assert f"\n  {_CAPTURE_TESTS}:{number}: forbidden_card_field\n" in result.stdout
    assert "\n  tests/legacy_fixture.py:1: forbidden_cpf_field\n" in result.stdout
    assert f"baseline secret/PII waived: {_WAIVED_COUNT}\n" in result.stdout
    assert "added-line secret/PII waived: 0\n" in result.stdout
    assert "added-line secret/PII scan: no findings" in result.stdout


def test_baseline_scan_reports_waived_only_tree(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    (repo / "tests" / "legacy_fixture.py").unlink()
    _write_capture_files(repo)
    _commit_all(repo)
    _git(repo, "tag", "-f", "base")
    _commit_clean_change(repo)

    result = _run_waiver_gate(repo, log)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "baseline secret/PII scan: no findings" in result.stdout
    assert f"\n  {_WAIVED}{_CAPTURE_TOOL}:2 rule=forbidden_cpf_field\n" in result.stdout
    assert f"baseline secret/PII waived: {_WAIVED_COUNT}\n" in result.stdout


@pytest.mark.parametrize("committed", [True, False], ids=["committed", "untracked"])
def test_waiver_matches_crlf_lines(tmp_path: Path, committed: bool) -> None:
    repo, log = _candidate_repo(tmp_path)
    _write_capture_files(
        repo,
        extra_test=(f'PROD_KEY = "{_KEY_PROD}{_REAL_LOOKING_TAIL}"',),
        newline="\r\n",
    )
    if committed:
        _commit_all(repo)

    result = _run_waiver_gate(repo, log)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert f"added-line secret/PII waived: {_WAIVED_COUNT}\n" in result.stdout
    number = len(_TEST_LINES) + 2
    assert f"\n{_CAPTURE_TESTS}:{number}: asaas_api_key\n" in result.stdout
    assert "waived-path audit: no findings" in result.stdout
    assert "scan failed" not in result.stderr


def test_waiver_rejects_a_very_long_line(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    long_line = f'PROD_KEY = "{_KEY_PROD}{_FAKE_KEY_TAIL}"' + " " * 200_000 + "#"
    _write_capture_files(repo, extra_test=(long_line,))
    _commit_all(repo)

    result = _run_waiver_gate(repo, log)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "scan failed" not in result.stderr
    number = len(_TEST_LINES) + 2
    assert f"\n{_CAPTURE_TESTS}:{number}: asaas_api_key\n" in result.stdout
    assert f"added-line secret/PII waived: {_WAIVED_COUNT}\n" in result.stdout


def _waiver_table() -> list[list[str]]:
    result = subprocess.run(
        ["bash", "-c", f'source "{GATES}" && _asaas_scan_waivers'],
        text=True,
        capture_output=True,
        check=True,
    )
    return [line.split("\t") for line in result.stdout.splitlines() if line]


def test_waiver_table_is_closed_and_recorded() -> None:
    table = _waiver_table()

    assert table
    for entry in table:
        assert len(entry) == 5, entry
        decision, path, rule, count, pattern = entry
        assert decision == _DECISION
        assert path in {_CAPTURE_TOOL, _CAPTURE_TESTS, _CAPTURE_RUNBOOK}
        assert rule in {
            "asaas_api_key",
            "forbidden_cpf_field",
            "forbidden_card_field",
            "payment_security_term",
            "pinned_line",
        }
        assert count.isdigit() and int(count) >= 1, entry
        # The gate anchors every pattern to the whole line itself.
        assert not pattern.startswith("^") and not pattern.endswith("$"), pattern
    assert len({(e[1], e[2], e[4]) for e in table}) == len(table)
    pins = {e[4] for e in table if e[1] == _CAPTURE_TOOL and e[2] == "pinned_line"}
    assert 'TEST_CARD_NUMBER = "4444444444444444"' in pins
    assert 'TEST_CARD_CCV = "123"' in pins
    # The runbook holds only its pinned test-card line.
    assert [e[2] for e in table if e[1] == _CAPTURE_RUNBOOK] == ["pinned_line"]
    # The security-code rule is waived only on lines that are also pinned or
    # waived for the card field.
    security = [e[4] for e in table if e[2] == "payment_security_term"]
    others = {e[4] for e in table if e[2] != "payment_security_term"}
    assert security and all(pattern in others for pattern in security)
    assert {e[1] for e in table if e[2] == "payment_security_term"} == {
        _CAPTURE_TOOL,
        _CAPTURE_TESTS,
    }


def test_real_waiver_table_passes_the_gate_validation(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)

    result = _run_release_gate(repo, log, **_GATE_INPUTS)

    assert result.returncode == 0, result.stdout + result.stderr
    assert _MALFORMED not in result.stderr


_TOOL = _CAPTURE_TOOL
_BAD_ENTRIES = [
    ("unknown-rule", f"{_DECISION}\t{_TOOL}\tno_such_rule\t1\tx"),
    ("missing-pattern", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1"),
    ("empty-pattern", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\t"),
    ("missing-decision", f"\t{_TOOL}\tforbidden_cpf_field\t1\tx"),
    ("four-fields", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\tx"),
    ("six-fields", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\tx\ty"),
    ("zero-count", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t0\tx"),
    ("text-count", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\tone\tx"),
    ("alternation-escape", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\tx)|(.+"),
    ("alternation", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\ta|b"),
    ("group", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\t(x)"),
    ("dot", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\tidentity.cpf"),
    ("star", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\tx*"),
    ("bracket-star", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\ta[x]*"),
    ("plus", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\t[a-z]+"),
    ("question", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\tx?"),
    ("interval", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\t[0-9]{{16}}"),
    ("range-class", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\t[a-z]"),
    ("negated-class", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\t[^x]"),
    ("print-class", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\t[[:print:]]*x"),
    (
        "space-class-inside",
        f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\tx[[:space:]]*y",
    ),
    (
        "only-space-prefix",
        f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\t[[:space:]]*",
    ),
    ("backslash-escape", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\tx\\.y"),
    ("backreference", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\t(a)\\1"),
    ("bare-backreference", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\ta\\1"),
    ("word-boundary-escape", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\tx\\y"),
    ("negated-bracket-tokens", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\t[^][]]"),
    ("caret", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\t^x"),
    ("dollar", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\tx$"),
    ("unterminated-bracket", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\tx[y"),
    ("brace", f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\tx{{"),
    (
        "duplicate",
        f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\tx\n"
        f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t2\tx",
    ),
]


@pytest.mark.parametrize(
    "entry", [entry for _, entry in _BAD_ENTRIES], ids=[n for n, _ in _BAD_ENTRIES]
)
def test_malformed_waiver_table_fails_closed(tmp_path: Path, entry: str) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)

    result = _run_waiver_gate(repo, log, override=_raw_table_override([entry]))

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"secret/PII scan failed: {_MALFORMED}" in result.stderr
    assert "conventional commits" not in result.stdout


def test_literal_waiver_shapes_are_accepted(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    rows = [
        (_TOOL, "forbidden_cpf_field", 1, "[[:space:]]*a[.]b[[][]][\\\\][$][(][)]á-x"),
        (_TOOL, "pinned_line", 1, "y"),
    ]

    result = _run_waiver_gate(repo, log, rows)

    assert result.returncode == 0, result.stdout + result.stderr
    assert len(_stale_lines(result.stderr)) == 2


def test_malformed_waiver_table_fails_even_when_nothing_is_scanned(
    tmp_path: Path,
) -> None:
    repo, log = _candidate_repo(tmp_path)
    (repo / "tests" / "legacy_fixture.py").unlink()
    _commit_all(repo, "test: drop legacy fixture")
    _git(repo, "tag", "-f", "base")
    # A change outside every scan root: no file reaches the scanner.
    (repo / "NOTES.txt").write_text("outside the scan roots\n")
    _commit_all(repo, "docs: add notes")
    entry = f"{_DECISION}\t{_TOOL}\tforbidden_cpf_field\t1\tidentity.cpf"

    result = _run_waiver_gate(repo, log, override=_raw_table_override([entry]))

    assert result.returncode == 1, result.stdout + result.stderr
    assert _MALFORMED in result.stderr
    assert "conventional commits" not in result.stdout


@pytest.mark.parametrize(
    ("line", "waived_rule", "pattern", "other_rule"),
    [
        # Waived rule sorts after the other one.
        (
            f"FIELD = '{_CPF_FIELD}' + '{_PLANTED_BEARER}'",
            "forbidden_cpf_field",
            "FIELD = 'cpf[C]npj' [+] 'Bear[e]r abc[.]def[.]ghi'",
            "bearer_token",
        ),
        # Waived rule sorts before the other one.
        (
            f"KEY = '{_KEY_PROD}{_FAKE_KEY_TAIL}' + '{_CVV_TERM}'",
            "asaas_api_key",
            "KEY = '[$]aact_prod_FAKEcapture-test-key-0001' [+] 'c[v]v'",
            "payment_security_term",
        ),
    ],
    ids=["waived-rule-last", "waived-rule-first"],
)
def test_waiver_for_one_rule_never_waives_another_rule_on_the_line(
    tmp_path: Path, line: str, waived_rule: str, pattern: str, other_rule: str
) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    _write_lines(repo, _CAPTURE_TOOL, [line])
    _commit_all(repo)

    result = _run_waiver_gate(repo, log, [(_CAPTURE_TOOL, waived_rule, 1, pattern)])

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert f"{_WAIVED}{_CAPTURE_TOOL}:2 rule={waived_rule}" in result.stdout
    assert f"\n{_CAPTURE_TOOL}:2: {other_rule}\n" in result.stdout
    assert "abc.def.ghi" not in output


def test_baseline_hit_the_rules_cannot_reproduce_fails(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    real_git = shutil.which("git")
    assert real_git
    wrapper = log.parent / "bin" / "git"
    # git grep reports a hit on a line no scan rule matches.
    wrapper.write_text(
        "#!/usr/bin/env bash\n"
        'case " $* " in *" grep --untracked "*)\n'
        "  printf '%s\\0%s\\0%s\\n' docs/README.txt 1 'clean line'; exit 0 ;;\n"
        "esac\n"
        f'exec "{real_git}" "$@"\n'
    )
    wrapper.chmod(0o755)

    result = _run_release_gate(repo, log, **_GATE_INPUTS)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "baseline hit not reproduced by the scan rules" in result.stderr
    assert "baseline secret/PII scan failed" in result.stderr


def test_waived_hit_prints_its_own_entry_decision(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    _write_capture_files(repo)
    _commit_all(repo)
    other = "owner decision 2099-01-01, wish test-only"
    # Relabel only the card-field entry of the tests file's security-code line.
    relabel = 10
    rows = [
        "\t".join([other if index == relabel else _DECISION, *map(str, row)])
        for index, row in enumerate(_TEST_TABLE)
    ]

    result = _run_waiver_gate(repo, log, override=_raw_table_override(rows))

    assert result.returncode == 0, result.stdout + result.stderr
    assert _TEST_TABLE[relabel][1] == "forbidden_card_field"
    last = f"{_CAPTURE_TESTS}:{len(_TEST_LINES) + 1}"
    assert f"\nwaived ({other}): {last} rule=forbidden_card_field\n" in result.stdout
    assert f"{_WAIVED}{last} rule=forbidden_card_field" not in result.stdout
    assert f"\n{_WAIVED}{last} rule=payment_security_term\n" in result.stdout
    assert f"\n{_WAIVED}{_CAPTURE_TOOL}:2 rule=forbidden_cpf_field\n" in result.stdout


def test_entry_whose_rule_never_matches_its_line_is_stale(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    _write_lines(repo, _CAPTURE_TOOL, ["plain line"])
    _commit_all(repo)

    result = _run_waiver_gate(
        repo, log, [(_CAPTURE_TOOL, "forbidden_cpf_field", 1, "plain line")]
    )

    assert result.returncode == 0, result.stdout + result.stderr
    stale = _stale_lines(result.stderr)
    assert len(stale) == 1
    assert "rule=forbidden_cpf_field matches 0 of 1" in stale[0]


def test_pinned_line_counts_whole_lines_only(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    _write_capture_files(
        repo,
        extra_tool=(
            f"# was: {_TOOL_CCV_PIN} (kept for history)",
            f"x = {{{_PAYLOAD_NUMBER}}}",
        ),
    )
    _commit_all(repo)

    result = _run_waiver_gate(repo, log)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "pinned line entry" not in result.stdout


def test_pinned_line_never_waives_a_rule_on_its_line(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    line = f'TEST_CARD_CCV = "123"  # {_PLANTED_BEARER}'
    _write_lines(repo, _CAPTURE_TOOL, [line])
    _commit_all(repo)
    pin = 'TEST_CARD_CCV = "123"  # Bear[e]r abc[.]def[.]ghi'

    result = _run_waiver_gate(repo, log, [(_CAPTURE_TOOL, "pinned_line", 1, pin)])

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert f"\n{_CAPTURE_TOOL}:2: bearer_token\n" in result.stdout
    assert not _waived_lines(output)
    assert "pinned line entry" not in result.stdout
    assert "abc.def.ghi" not in output


@pytest.mark.parametrize(
    "term", [_CCV_TERM, _CVV_TERM], ids=["provider-field", "generic-term"]
)
@pytest.mark.parametrize("committed", [True, False], ids=["committed", "untracked"])
def test_security_code_term_fails_outside_the_tool(
    tmp_path: Path, term: str, committed: bool
) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    _write_capture_files(repo)
    # The exact pinned payload line, but in another file.
    (repo / "src" / "checkout.py").write_text(
        f'payload = {{\n    "{term}": TEST_CARD_CCV,\n}}\n'
    )
    if committed:
        _commit_all(repo)

    result = _run_waiver_gate(repo, log)

    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "\nsrc/checkout.py:2: payment_security_term\n" in result.stdout
    assert "src/checkout.py:2 rule=" not in output


def test_security_code_rule_is_waived_only_on_its_pinned_lines(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    _write_capture_files(repo)
    _commit_all(repo)

    result = _run_waiver_gate(repo, log)

    assert result.returncode == 0, result.stdout + result.stderr
    # Added-line scan output only (the baseline repeats it, indented).
    waived = [x for x in result.stdout.splitlines() if x.startswith(_WAIVED)]
    security = [line for line in waived if "rule=payment_security_term" in line]
    assert security == [
        f"{_WAIVED}{_CAPTURE_TOOL}:5 rule=payment_security_term",
        f"{_WAIVED}{_CAPTURE_TESTS}:5 rule=payment_security_term",
    ]
    # The test line is waived for both rules it hits, each by its own entry.
    assert f"{_WAIVED}{_CAPTURE_TESTS}:5 rule=forbidden_card_field" in waived
    assert not [line for line in waived if _CAPTURE_RUNBOOK in line]


def test_pinned_line_alone_does_not_waive_the_security_code_rule(
    tmp_path: Path,
) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    _write_capture_files(repo)
    _commit_all(repo)
    rows = [row for row in _TEST_TABLE if row[1] != "payment_security_term"]

    result = _run_waiver_gate(repo, log, rows)

    assert result.returncode == 1, result.stdout + result.stderr
    assert f"\n{_CAPTURE_TOOL}:5: payment_security_term\n" in result.stdout
    assert f"\n{_CAPTURE_TESTS}:5: payment_security_term\n" in result.stdout
    assert f"{_WAIVED}{_CAPTURE_TESTS}:5 rule=forbidden_card_field" in result.stdout


def test_runbook_pinned_line_passes_and_is_audited(tmp_path: Path) -> None:
    repo, log = _candidate_repo(tmp_path)
    _commit_clean_change(repo)
    _write_capture_files(repo, extra_runbook=("Any future expiry works.",))
    _commit_all(repo)

    result = _run_waiver_gate(repo, log)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "waived-path audit: no findings" in result.stdout
    assert not [x for x in _stale_lines(result.stderr) if _CAPTURE_RUNBOOK in x]


def test_real_table_matches_the_real_tool_files(tmp_path: Path) -> None:
    """The committed table against the committed tool: every scan and the audit."""
    repo, log = _candidate_repo(tmp_path)
    for relative in (_CAPTURE_TOOL, _CAPTURE_TESTS, _CAPTURE_RUNBOOK):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ROOT / relative).read_bytes())
    _commit_all(repo)
    # The security-code hits, found by text so line drift does not matter.
    term = f'"{_CCV_TERM}"'
    tool_code_lines = [
        n
        for n, line in enumerate((ROOT / _CAPTURE_TOOL).read_text().splitlines(), 1)
        if f"{term}: TEST_CARD_CCV," in line
    ]
    tests_code_lines = [
        n
        for n, line in enumerate((ROOT / _CAPTURE_TESTS).read_text().splitlines(), 1)
        if f'["{_CARD_FIELD}"][{term}] == "123"' in line
    ]
    table = _waiver_table()

    result = _run_release_gate(repo, log, **_GATE_INPUTS)

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "waived-path audit: no findings" in result.stdout
    assert "added-line secret/PII scan: no findings" in result.stdout
    assert not _stale_lines(result.stderr)
    lines = result.stdout.splitlines()
    added = [x for x in lines if x.startswith(_WAIVED)]
    baseline = [x[2:] for x in lines if x.startswith(f"  {_WAIVED}")]
    assert sorted(added) == sorted(baseline)
    per_file = {
        path: len([x for x in added if f"): {path}:" in x])
        for path in (_CAPTURE_TOOL, _CAPTURE_TESTS, _CAPTURE_RUNBOOK)
    }
    assert per_file == {_CAPTURE_TOOL: 5, _CAPTURE_TESTS: 21, _CAPTURE_RUNBOOK: 0}
    assert len(added) == 26
    assert "added-line secret/PII waived: 26\n" in result.stdout
    assert "baseline secret/PII waived: 26\n" in result.stdout
    security = sorted(x for x in added if x.endswith("rule=payment_security_term"))
    assert len(tool_code_lines) == 1 and len(tests_code_lines) == 1
    assert security == sorted(
        [
            f"{_WAIVED}{_CAPTURE_TOOL}:{tool_code_lines[0]} rule=payment_security_term",
            f"{_WAIVED}{_CAPTURE_TESTS}:{tests_code_lines[0]} rule=payment_security_term",
        ]
    )
    # Four card pins in the tool plus the runbook's test-card pin; a passing
    # audit means each matched exactly its recorded count.
    pins = [(e[1], e[3]) for e in table if e[2] == "pinned_line"]
    assert sorted(pins) == sorted(
        [(_CAPTURE_TOOL, "1")] * 4 + [(_CAPTURE_RUNBOOK, "1")]
    )
