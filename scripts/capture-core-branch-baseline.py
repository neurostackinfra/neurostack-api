#!/usr/bin/env python3
"""Capture deterministic pytest floors from an already archived source tree."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

BRANCH_FILE = "neurostack-core-extraction-branch.txt"
FAILURES_FILE = "neurostack-core-extraction-known-failures.txt"
OUTCOME_FLOOR_FILE = "neurostack-core-extraction-outcome-floor.txt"
PASS_FLOOR_FILE = "neurostack-core-extraction-pass-floor.txt"
FOCUSED_TARGETS = ("tests/srm", "tests/flow", "tests/policy", "tests/prompt")
OUTCOME_NAMES = ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")


class CaptureError(ValueError):
    """A baseline run was not a complete, trustworthy pytest run."""


@dataclass(frozen=True)
class PytestResult:
    outcomes: dict[str, int]
    failures: tuple[str, ...]

    @property
    def completed(self) -> int:
        return sum(self.outcomes.values())


def parse_pytest_output(output: str) -> PytestResult:
    if "test session starts" not in output:
        raise CaptureError("pytest session header is missing")
    collected_matches = re.findall(r"collected\s+(\d+)\s+items?", output)
    if not collected_matches:
        raise CaptureError("pytest collection summary is missing")
    summary: str | None = None
    for line in output.splitlines():
        if re.search(
            r"\b\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed)\b", line
        ) and re.search(r"\bin\s+\d", line):
            summary = line
    if summary is None:
        raise CaptureError("pytest completion summary is missing")
    outcomes = {name: 0 for name in OUTCOME_NAMES}
    for count, raw_name in re.findall(
        r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed)\b", summary
    ):
        outcomes["errors" if raw_name in {"error", "errors"} else raw_name] = int(count)
    if not any(outcomes.values()):
        raise CaptureError("pytest completion summary has no outcomes")
    failures = tuple(
        sorted(
            {
                f"{kind} {node.strip()}"
                for kind, node in re.findall(
                    r"^(FAILED|ERROR)\s+(\S+)", output, re.MULTILINE
                )
            }
        )
    )
    if sum(item.startswith("FAILED ") for item in failures) != outcomes["failed"]:
        raise CaptureError("pytest FAILED detail does not match its completion summary")
    if sum(item.startswith("ERROR ") for item in failures) != outcomes["errors"]:
        raise CaptureError("pytest ERROR detail does not match its completion summary")
    selected_matches = re.findall(r"/\s+(\d+)\s+selected\b", output)
    selected = (
        int(selected_matches[-1]) if selected_matches else int(collected_matches[-1])
    )
    terminal = sum(outcomes[name] for name in OUTCOME_NAMES if name != "errors")
    if terminal < selected:
        raise CaptureError("pytest completion summary is partial")
    return PytestResult(outcomes=outcomes, failures=failures)


def run_pytest(python: str, tree: Path, targets: tuple[str, ...]) -> PytestResult:
    result = subprocess.run(
        [python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets],
        cwd=tree,
        text=True,
        capture_output=True,
        check=False,
    )
    output = result.stdout + result.stderr
    if result.returncode not in {0, 1}:
        raise CaptureError(
            f"pytest exited {result.returncode}; only complete 0/1 runs are accepted\n{output}"
        )
    parsed = parse_pytest_output(output)
    has_failures = bool(parsed.outcomes["failed"] or parsed.outcomes["errors"])
    if result.returncode == 0 and has_failures:
        raise CaptureError("pytest exit 0 contradicts failed/error outcomes")
    if result.returncode == 1 and not has_failures:
        raise CaptureError("pytest exit 1 lacks a failed/error outcome")
    return parsed


def capture(tree: Path, python: str) -> tuple[PytestResult, PytestResult]:
    if not tree.is_dir():
        raise CaptureError(f"archive tree is missing: {tree}")
    missing = [target for target in FOCUSED_TARGETS if not (tree / target).is_dir()]
    if missing:
        raise CaptureError(f"focused target missing from archive: {', '.join(missing)}")
    return (
        run_pytest(python, tree, ("tests",)),
        run_pytest(python, tree, FOCUSED_TARGETS),
    )


def write_baseline(
    output: Path, base_sha: str, full: PytestResult, focused: PytestResult
) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", base_sha):
        raise CaptureError("--base-sha must be a full lowercase commit SHA")
    output.mkdir(parents=True, exist_ok=True)
    (output / BRANCH_FILE).write_text(base_sha + "\n", encoding="utf-8")
    failures = "\n".join(full.failures)
    (output / FAILURES_FILE).write_text(
        failures + ("\n" if failures else ""), encoding="utf-8"
    )
    (output / OUTCOME_FLOOR_FILE).write_text(f"{full.completed}\n", encoding="utf-8")
    (output / PASS_FLOOR_FILE).write_text(
        f"{focused.outcomes['passed']}\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    try:
        full, focused = capture(args.tree.resolve(), args.python)
        write_baseline(args.output, args.base_sha, full, focused)
    except CaptureError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
