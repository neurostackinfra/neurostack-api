"""Focused tests for the editable/source public-facade checker."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "packages" / "neurostack-core" / "scripts" / "check_public_api.py"
EXPECTED = ROOT / "tests" / "baselines" / "neurostack-core-extraction-public-api.json"


def run_checker(
    fixture: Path, *, source: bool = False
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, str(SCRIPT), "--expected", str(fixture)]
    if source:
        command.append("--source")
    return subprocess.run(
        command,
        cwd=ROOT,
        env=os.environ.copy(),
        text=True,
        capture_output=True,
        check=False,
    )


def test_source_facades_match_captured_fixture() -> None:
    result = run_checker(EXPECTED, source=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "public API matches" in result.stdout


def test_poetry_editable_facades_use_the_same_contract() -> None:
    result = run_checker(EXPECTED)
    assert result.returncode == 0, result.stdout + result.stderr


def test_exact_facade_mismatch_is_rejected(tmp_path: Path) -> None:
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    expected["neurostack_core.flow"] = expected["neurostack_core.flow"][:-1]
    fixture = tmp_path / "expected.json"
    fixture.write_text(json.dumps(expected), encoding="utf-8")

    result = run_checker(fixture, source=True)

    assert result.returncode == 1
    assert "public facade mismatch" in result.stderr


def test_every_declared_export_must_be_importable(tmp_path: Path) -> None:
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    expected["neurostack_core.flow"].append("Missing")
    fixture = tmp_path / "expected.json"
    fixture.write_text(json.dumps(expected), encoding="utf-8")

    result = run_checker(fixture, source=True)

    assert result.returncode == 1
    assert "does not define expected symbol Missing" in result.stderr
