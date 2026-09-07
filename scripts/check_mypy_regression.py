"""Fail when the current tree introduces mypy diagnostics beyond a frozen base."""

from __future__ import annotations

import argparse
import io
import re
import subprocess
import sys
import tarfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

BASELINE_ERROR_COUNT = 364
Diagnostic = tuple[str, str, str]

_ERROR_LINE = re.compile(
    r"^(?P<file>.+?):(?P<line>\d+)(?::(?P<column>\d+))?: "
    r"error: (?P<message>.+)  \[(?P<code>[^\]]+)\]$"
)
_NOTE_LINE = re.compile(r"^(?:(?P<file>.+?):(?P<line>\d+): )?note: .+$")
_FAILURE_SUMMARY = re.compile(
    r"^Found (?P<count>\d+) errors? in \d+ files? " r"\(checked \d+ source files?\)$"
)
_SUCCESS_SUMMARY = re.compile(r"^Success: no issues found in \d+ source files?$")


class MypyRegressionError(RuntimeError):
    """The differential gate could not establish a regression-free result."""


@dataclass(frozen=True)
class MypyRun:
    diagnostics: Counter[Diagnostic]
    raw_error_count: int
    output: str


def _relative_file(raw_file: str, root: Path) -> str:
    root = root.resolve()
    path = Path(raw_file)
    candidate = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        return candidate.relative_to(root).as_posix()
    except ValueError as error:
        raise MypyRegressionError(
            f"diagnostic path is outside its mypy root: {raw_file}"
        ) from error


def parse_mypy_output(output: str, root: Path, returncode: int) -> MypyRun:
    """Parse one complete mypy run without retaining source positions."""
    if returncode not in {0, 1}:
        raise MypyRegressionError(f"mypy exited with unsupported status {returncode}")

    diagnostics: Counter[Diagnostic] = Counter()
    failure_count: int | None = None
    saw_success = False

    for line in output.splitlines():
        if not line:
            continue
        error_match = _ERROR_LINE.fullmatch(line)
        if error_match is not None:
            diagnostic = (
                _relative_file(error_match.group("file"), root),
                error_match.group("code"),
                error_match.group("message"),
            )
            diagnostics[diagnostic] += 1
            continue
        if _NOTE_LINE.fullmatch(line) is not None:
            continue
        failure_match = _FAILURE_SUMMARY.fullmatch(line)
        if failure_match is not None:
            if failure_count is not None or saw_success:
                raise MypyRegressionError("mypy output contains conflicting summaries")
            failure_count = int(failure_match.group("count"))
            continue
        if _SUCCESS_SUMMARY.fullmatch(line) is not None:
            if failure_count is not None or saw_success:
                raise MypyRegressionError("mypy output contains conflicting summaries")
            saw_success = True
            continue
        raise MypyRegressionError(f"unparseable mypy output line: {line}")

    parsed_count = sum(diagnostics.values())
    if returncode == 0:
        if not saw_success or failure_count is not None or parsed_count:
            raise MypyRegressionError(
                "successful mypy output is incomplete or inconsistent"
            )
    elif failure_count is None or saw_success or failure_count != parsed_count:
        raise MypyRegressionError(
            "failing mypy output is incomplete or has an inconsistent error total"
        )

    return MypyRun(
        diagnostics=diagnostics,
        raw_error_count=parsed_count,
        output=output,
    )


def run_mypy(root: Path) -> MypyRun:
    """Run mypy in one tree and return its validated diagnostic multiset."""
    command = [
        sys.executable,
        "-m",
        "mypy",
        "src",
        "--ignore-missing-imports",
        "--show-error-codes",
        "--no-incremental",
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as error:
        raise MypyRegressionError(f"could not execute mypy: {error}") from error

    output = completed.stdout
    if completed.stderr:
        output += completed.stderr
    return parse_mypy_output(output, root, completed.returncode)


def export_base(repo_root: Path, base: str, destination: Path) -> None:
    """Materialize a committed base without mutating repository worktrees."""
    try:
        archive = subprocess.run(
            ["git", "archive", "--format=tar", base],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise MypyRegressionError(f"could not execute git archive: {error}") from error
    if archive.returncode != 0:
        detail = archive.stderr.decode(errors="replace").strip()
        raise MypyRegressionError(
            f"git archive failed with status {archive.returncode}: {detail}"
        )

    try:
        with tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:") as source:
            source.extractall(destination)
    except (OSError, tarfile.TarError) as error:
        raise MypyRegressionError(f"could not extract base archive: {error}") from error


def new_diagnostics(
    base: Counter[Diagnostic], current: Counter[Diagnostic]
) -> Counter[Diagnostic]:
    """Return new signatures and multiplicity growth in the current tree."""
    return current - base


def validate_regression(base: MypyRun, current: MypyRun) -> None:
    if base.raw_error_count != BASELINE_ERROR_COUNT:
        raise MypyRegressionError(
            "frozen base must contain exactly "
            f"{BASELINE_ERROR_COUNT} raw errors, found {base.raw_error_count}"
        )
    added = new_diagnostics(base.diagnostics, current.diagnostics)
    if added:
        lines = [
            f"{file}: [{code}] {message} (new occurrences: {count})"
            for (file, code, message), count in sorted(added.items())
        ]
        raise MypyRegressionError("new mypy diagnostics:\n" + "\n".join(lines))


def format_visible_debt(run: MypyRun) -> str:
    lines = [f"current mypy debt: {run.raw_error_count} raw errors"]
    lines.extend(
        f"{file}: [{code}] {message} (occurrences: {count})"
        for (file, code, message), count in sorted(run.diagnostics.items())
    )
    return "\n".join(lines)


def check_mypy_regression(repo_root: Path, base: str) -> None:
    with TemporaryDirectory(prefix="neurostack-mypy-base-") as temporary:
        base_root = Path(temporary)
        export_base(repo_root, base, base_root)
        base_run = run_mypy(base_root)
    current_run = run_mypy(repo_root)
    validate_regression(base_run, current_run)
    print(format_visible_debt(current_run))
    print(
        "mypy regression gate passed: "
        f"base={base_run.raw_error_count}, current={current_run.raw_error_count}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    args = parser.parse_args()
    repo_root = Path(__file__).resolve().parents[1]
    try:
        check_mypy_regression(repo_root, args.base)
    except MypyRegressionError as error:
        print(f"mypy regression gate failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
