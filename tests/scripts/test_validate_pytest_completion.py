from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "validate-pytest-completion.py"


def invoke(
    tmp_path: Path, transcript: str, mode: str = "full"
) -> subprocess.CompletedProcess[str]:
    output = tmp_path / "pytest.txt"
    floor = tmp_path / "floor.txt"
    failures = tmp_path / "failures.txt"
    output.write_text(transcript)
    floor.write_text("3\n" if mode == "full" else "2\n")
    failures.write_text("FAILED tests/test_x.py::test_x\n")
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--output",
            str(output),
            "--floor",
            str(floor),
            "--failures",
            str(failures),
            "--mode",
            mode,
        ],
        text=True,
        capture_output=True,
        check=False,
    )


VALID = """============================= test session starts ==============================
collected 3 items
tests/test_x.py F..
FAILED tests/test_x.py::test_x - AssertionError
========================= 1 failed, 2 passed in 0.10s =========================
"""


def test_accepts_exact_failure_set_and_completion_floor(tmp_path: Path) -> None:
    assert invoke(tmp_path, VALID).returncode == 0


@pytest.mark.parametrize(
    "transcript",
    [
        "collected 3 items\n1 failed, 2 passed in 0.1s\n",
        "test session starts\ncollected 3 items\n",
        VALID.replace("FAILED tests/test_x.py::test_x - AssertionError\n", ""),
        VALID.replace("collected 3 items", "collected 4 items"),
    ],
)
def test_rejects_missing_or_partial_evidence(tmp_path: Path, transcript: str) -> None:
    assert invoke(tmp_path, transcript).returncode == 1


def test_focused_mode_enforces_pass_floor_and_zero_failures(tmp_path: Path) -> None:
    good = (
        VALID.replace("collected 3 items", "collected 2 items")
        .replace(
            "tests/test_x.py F..\nFAILED tests/test_x.py::test_x - AssertionError\n",
            "tests/test_x.py ..\n",
        )
        .replace("1 failed, 2 passed", "2 passed")
    )
    assert invoke(tmp_path, good, "focused").returncode == 0
    assert invoke(tmp_path, VALID, "focused").returncode == 1
