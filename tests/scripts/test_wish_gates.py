from __future__ import annotations

import os
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
