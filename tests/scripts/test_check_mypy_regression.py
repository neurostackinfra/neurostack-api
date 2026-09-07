"""Tests for the occurrence-aware differential mypy gate."""

from __future__ import annotations

import subprocess
from collections import Counter
from pathlib import Path

import pytest

from scripts import check_mypy_regression as checker


def _failure_output(*errors: str) -> str:
    count = len(errors)
    noun = "error" if count == 1 else "errors"
    return "\n".join(
        [*errors, f"Found {count} {noun} in 1 file (checked 1 source file)"]
    )


def _run(*diagnostics: tuple[str, str, str, int]) -> checker.MypyRun:
    counter: Counter[checker.Diagnostic] = Counter()
    for file, code, message, count in diagnostics:
        counter[(file, code, message)] = count
    return checker.MypyRun(
        diagnostics=counter,
        raw_error_count=sum(counter.values()),
        output="test output",
    )


def test_distinct_roots_and_shifted_positions_produce_same_signature(
    tmp_path: Path,
) -> None:
    base_root = tmp_path / "base"
    current_root = tmp_path / "current"
    base_root.mkdir()
    current_root.mkdir()
    base_output = _failure_output(
        f"{base_root}/src/example.py:10:2: error: Broken type  [assignment]"
    )
    current_output = _failure_output(
        f"{current_root}/src/example.py:999: error: Broken type  [assignment]"
    )

    base = checker.parse_mypy_output(base_output, base_root, 1)
    current = checker.parse_mypy_output(current_output, current_root, 1)

    assert base.diagnostics == current.diagnostics
    assert base.diagnostics == Counter(
        {("src/example.py", "assignment", "Broken type"): 1}
    )


def test_removed_diagnostic_is_not_a_regression() -> None:
    base = Counter(
        {
            ("src/a.py", "assignment", "Broken type"): 1,
            ("src/b.py", "type-arg", "Missing type arguments"): 1,
        }
    )
    current = Counter({("src/a.py", "assignment", "Broken type"): 1})

    assert checker.new_diagnostics(base, current) == Counter()


def test_new_diagnostic_is_a_regression() -> None:
    base = Counter({("src/a.py", "assignment", "Broken type"): 1})
    current = base + Counter({("src/new.py", "name-defined", "Missing name"): 1})

    assert checker.new_diagnostics(base, current) == Counter(
        {("src/new.py", "name-defined", "Missing name"): 1}
    )


def test_duplicate_signature_growth_is_a_regression() -> None:
    diagnostic = ("src/a.py", "assignment", "Broken type")

    assert checker.new_diagnostics(
        Counter({diagnostic: 1}), Counter({diagnostic: 2})
    ) == Counter({diagnostic: 1})


@pytest.mark.parametrize(
    ("output", "returncode"),
    [
        ("src/a.py:1: error: missing error code\nFound 1 error in 1 file", 1),
        ("Found 1 error in 1 file (checked 1 source file)", 1),
        ("Success: no issues found in 1 source file", 1),
        ("Success: no issues found in 1 source file", 0),
    ],
)
def test_output_contract_accepts_only_complete_consistent_results(
    tmp_path: Path, output: str, returncode: int
) -> None:
    if returncode == 0:
        result = checker.parse_mypy_output(output, tmp_path, returncode)
        assert result.raw_error_count == 0
        return
    with pytest.raises(checker.MypyRegressionError):
        checker.parse_mypy_output(output, tmp_path, returncode)


def test_failing_output_must_report_exact_parsed_total(tmp_path: Path) -> None:
    output = "\n".join(
        [
            "src/a.py:1: error: Broken type  [assignment]",
            "Found 2 errors in 1 file (checked 1 source file)",
        ]
    )

    with pytest.raises(checker.MypyRegressionError, match="inconsistent error total"):
        checker.parse_mypy_output(output, tmp_path, 1)


def test_run_mypy_rejects_subprocess_status_other_than_zero_or_one(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    completed = subprocess.CompletedProcess(
        args=["mypy"],
        returncode=2,
        stdout="mypy failed before checking",
        stderr="",
    )
    monkeypatch.setattr(checker.subprocess, "run", lambda *args, **kwargs: completed)

    with pytest.raises(checker.MypyRegressionError, match="unsupported status 2"):
        checker.run_mypy(tmp_path)


def test_run_mypy_disables_incremental_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorded_command: list[str] = []

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded_command.extend(command)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="Success: no issues found in 1 source file\n",
            stderr="",
        )

    monkeypatch.setattr(checker.subprocess, "run", run)

    checker.run_mypy(tmp_path)

    assert "--no-incremental" in recorded_command


def test_run_mypy_reports_process_start_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fail_to_start(*args: object, **kwargs: object) -> None:
        raise OSError("process unavailable")

    monkeypatch.setattr(checker.subprocess, "run", fail_to_start)

    with pytest.raises(checker.MypyRegressionError, match="could not execute mypy"):
        checker.run_mypy(tmp_path)


def test_validate_regression_requires_exact_frozen_base_count() -> None:
    base = _run(("src/a.py", "assignment", "Broken type", 363))
    current = _run(("src/a.py", "assignment", "Broken type", 363))

    with pytest.raises(checker.MypyRegressionError, match="exactly 364"):
        checker.validate_regression(base, current)


def test_validate_regression_rejects_new_multiplicity() -> None:
    baseline = ("src/a.py", "assignment", "Broken type", 364)
    base = _run(baseline)
    current = _run(("src/a.py", "assignment", "Broken type", 365))

    with pytest.raises(checker.MypyRegressionError, match="new occurrences: 1"):
        checker.validate_regression(base, current)


def test_visible_debt_lists_every_signature_and_multiplicity() -> None:
    run = _run(
        ("src/a.py", "assignment", "Broken type", 2),
        ("src/b.py", "type-arg", "Missing type arguments", 1),
    )

    rendered = checker.format_visible_debt(run)

    assert "current mypy debt: 3 raw errors" in rendered
    assert "src/a.py: [assignment] Broken type (occurrences: 2)" in rendered
    assert "src/b.py: [type-arg] Missing type arguments (occurrences: 1)" in rendered
