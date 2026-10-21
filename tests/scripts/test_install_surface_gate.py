from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from pathspec import GitIgnoreSpec

ROOT = Path(__file__).resolve().parents[2]
GATES = ROOT / "scripts" / "wish-gates.sh"
ROWS = (
    "poetry-main",
    "poetry-dev",
    "pip-main",
    "pip-dev",
    "compose-api",
    "production-image",
)


def test_docker_context_excludes_nested_node_modules_and_keeps_manifests() -> None:
    dockerignore = GitIgnoreSpec.from_lines(
        (ROOT / ".dockerignore").read_text().splitlines()
    )

    excluded_dependencies = (
        "node_modules/package/index.js",
        "frontend/node_modules/.pnpm/package/index.js",
        "packages/example/node_modules/package/index.js",
    )
    retained_build_inputs = (
        "README.md",
        "pyproject.toml",
        "poetry.lock",
        "frontend/package.json",
        "frontend/pnpm-lock.yaml",
        "packages/neurostack-core/README.md",
        "packages/neurostack-core/pyproject.toml",
    )

    assert all(dockerignore.match_file(path) for path in excluded_dependencies)
    assert not any(dockerignore.match_file(path) for path in retained_build_inputs)


def executable(path: Path, source: str) -> None:
    path.write_text(source)
    path.chmod(0o755)


def command_shims(directory: Path) -> None:
    executable(
        directory / "fake-python",
        """#!/usr/bin/env bash
echo "python $*" >> "$COMMAND_LOG"
case "$*" in *"${FAIL_MATCH:-__never__}"*) exit 1;; esac
# Emulate `python -c "import sys; print(sys.prefix)"`. Real Python derives
# sys.prefix from the venv's pyvenv.cfg / the interpreter's own location,
# NOT from the symlink target. We mirror that by printing two directories
# up from the invoking path ($0), so a symlinked interpreter still reports
# the venv root that owns it. If ESCAPE_SYS_PREFIX is set, we lie and
# report an outside path to prove the escape check still fires.
if [ "$1" = "-c" ] && [ "$#" -ge 2 ]; then
  case "$2" in
    *"sys.prefix"*)
      if [ -n "${ESCAPE_SYS_PREFIX:-}" ]; then
        echo "$ESCAPE_SYS_PREFIX"
      else
        echo "$(dirname "$(dirname "$0")")"
      fi
      exit 0
      ;;
  esac
fi
exit 0
""",
    )
    executable(
        directory / "fake-pip",
        """#!/usr/bin/env bash
echo "pip $*" >> "$COMMAND_LOG"
case "$*" in *"${FAIL_MATCH:-__never__}"*) exit 1;; esac
exit 0
""",
    )
    executable(
        directory / "poetry",
        """#!/usr/bin/env bash
echo "poetry VIRTUAL_ENV=${VIRTUAL_ENV-unset} CONDA_PREFIX=${CONDA_PREFIX-unset} POETRY_ACTIVE=${POETRY_ACTIVE-unset} CREATE=${POETRY_VIRTUALENVS_CREATE-unset} PATH_ROOT=${POETRY_VIRTUALENVS_PATH-unset} $*" >> "$COMMAND_LOG"
case "$*" in *"${FAIL_MATCH:-__never__}"*) exit 1;; esac
case " $* " in
  *" env info --executable "*)
    mkdir -p "$POETRY_VIRTUALENVS_PATH/fake/bin"
    # Real venvs SYMLINK bin/python to the base interpreter (outside the venv),
    # they do not copy. Mirror that so the sys.prefix-based escape check is
    # exercised against realistic conditions.
    ln -sf "$FAKE_PYTHON" "$POETRY_VIRTUALENVS_PATH/fake/bin/python"
    echo "$POETRY_VIRTUALENVS_PATH/fake/bin/python"
    ;;
esac
exit 0
""",
    )
    executable(
        directory / "python3",
        """#!/usr/bin/env bash
echo "python3 $*" >> "$COMMAND_LOG"
case "$*" in *"${FAIL_MATCH:-__never__}"*) exit 1;; esac
if [ "$1 $2" = "-m venv" ]; then
  mkdir -p "$3/bin"
  ln -sf "$FAKE_PYTHON" "$3/bin/python"
  ln -sf "$FAKE_PIP" "$3/bin/pip"
fi
exit 0
""",
    )
    executable(
        directory / "docker",
        """#!/usr/bin/env bash
echo "docker $* [NEUROSTACK_DEV_STACK=${NEUROSTACK_DEV_STACK-unset}]" >> "$COMMAND_LOG"
if [ "${FAIL_MATCH:-}" = compose-api ] && [ "$1" = compose ]; then
  for arg in "$@"; do [ "$arg" = run ] && exit 1; done
fi
if [ "${FAIL_MATCH:-}" = production-image ] && [ "$1 $2 $3" = "build --target production" ]; then
  exit 1
fi
exit 0
""",
    )


def run_gate(
    tmp_path: Path,
    fail_match: str = "",
    phase: str = "foundation",
    escape_sys_prefix: str = "",
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    command_shims(tmp_path)
    command_log = tmp_path / "commands.log"
    env = {
        **{k: v for k, v in os.environ.items() if k != "NEUROSTACK_DEV_STACK"},
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "COMMAND_LOG": str(command_log),
        "FAKE_PYTHON": str(tmp_path / "fake-python"),
        "FAKE_PIP": str(tmp_path / "fake-pip"),
        "FAIL_MATCH": fail_match,
        "ESCAPE_SYS_PREFIX": escape_sys_prefix,
        "VIRTUAL_ENV": "/hostile/venv",
        "CONDA_PREFIX": "/hostile/conda",
        "POETRY_ACTIVE": "1",
    }
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{GATES}"; install_surface_gate --phase "$1"',
            "gate-test",
            phase,
        ],
        text=True,
        capture_output=True,
        env=env,
        check=False,
    )
    commands = command_log.read_text().splitlines() if command_log.exists() else []
    return result, commands


def test_install_gate_executes_real_commands_for_all_six_rows(tmp_path: Path) -> None:
    result, commands = run_gate(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    for row in ROWS:
        assert f"PASS {row}" in result.stdout
    joined = "\n".join(commands)
    assert "poetry-main" in joined and "poetry-dev" in joined
    assert "pip-main" in joined and "pip-dev" in joined
    assert "docker compose" in joined and " run --build --rm --no-deps " in joined
    assert "docker build --target production" in joined
    assert "docker run --rm -i" in joined
    # docker-compose.yml aborts without the dev-stack flag; the gate sets it
    # for its own disposable-project compose commands only.
    compose_calls = [line for line in commands if line.startswith("docker compose")]
    assert compose_calls
    assert all("[NEUROSTACK_DEV_STACK=1]" in line for line in compose_calls)
    other_docker = [
        line
        for line in commands
        if line.startswith("docker ") and not line.startswith("docker compose")
    ]
    assert all("[NEUROSTACK_DEV_STACK=unset]" in line for line in other_docker)
    oracle_calls = [
        line for line in commands if "assert-neurostack-core-origin.py" in line
    ]
    stdin_oracles = [line for line in commands if "python - --mode editable" in line]
    assert len(oracle_calls) == 4
    assert len(stdin_oracles) == 2
    poetry_calls = [line for line in commands if line.startswith("poetry ")]
    assert poetry_calls
    assert all("VIRTUAL_ENV=unset" in line for line in poetry_calls)
    assert all("CONDA_PREFIX=unset" in line for line in poetry_calls)
    assert all("POETRY_ACTIVE=unset" in line for line in poetry_calls)
    assert all("CREATE=true" in line for line in poetry_calls)


@pytest.mark.parametrize(
    ("fail_match", "row"),
    [
        ("poetry-main", "poetry-main"),
        ("poetry-dev", "poetry-dev"),
        ("pip-main", "pip-main"),
        ("pip-dev", "pip-dev"),
        ("compose-api", "compose-api"),
        ("production-image", "production-image"),
    ],
)
def test_each_real_row_failure_is_reported_after_all_rows_run(
    tmp_path: Path, fail_match: str, row: str
) -> None:
    result, _commands = run_gate(tmp_path, fail_match=fail_match)
    assert result.returncode == 1
    assert f"FAIL {row}" in result.stderr
    for expected in ROWS:
        assert f"==> install surface: {expected}" in result.stdout


@pytest.mark.parametrize("phase", ["", "diagnostics", "Foundation", "other"])
def test_rejects_every_phase_outside_the_exact_contract(
    tmp_path: Path, phase: str
) -> None:
    result, _commands = run_gate(tmp_path, phase=phase)
    assert result.returncode == 2


@pytest.mark.parametrize("row", ["poetry-main", "poetry-dev"])
def test_escaped_poetry_venv_fails_with_escape_message(
    tmp_path: Path, row: str
) -> None:
    """A venv whose sys.prefix lies outside the temp workspace must be rejected.

    This preserves Re-review #2 gap 3's coverage: a poetry env that legitimately
    (or maliciously) reuses an ambient/global interpreter root must still trip
    the escape guard, even though its bin/python is a symlink like a real venv.
    """
    result, _commands = run_gate(tmp_path, escape_sys_prefix="/hostile/ambient/venv")
    assert result.returncode == 1
    assert "escaped temporary root" in result.stderr
    assert f"FAIL {row}" in result.stderr


def test_cleanup_removes_owned_temp_directory(tmp_path: Path) -> None:
    result, commands = run_gate(tmp_path)
    assert result.returncode == 0
    poetry_command = next(line for line in commands if "poetry-main/project" in line)
    temporary = Path(poetry_command.split("/poetry-main/project", 1)[0].split()[-1])
    assert not temporary.exists()
