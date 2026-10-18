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
    assert "tests/integration/test_asaas_billing_postgres.py" in calls
    assert "docker compose" in calls
    assert "added-line secret/PII scan: no findings" in result.stdout
    # The pre-existing fixture hit is shown for review but does not fail.
    assert "baseline secret/PII findings (review required)" in result.stdout
    assert "tests/legacy_fixture.py" in result.stdout
    assert "tests/integration/test_billing_producer_handoff_postgres.py" in calls
    assert "tests/integration/test_asaas_subscription_triggers_postgres.py" in calls
    assert "conventional commits: 1 checked" in result.stdout


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
