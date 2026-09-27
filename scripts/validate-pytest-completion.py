#!/usr/bin/env python3
"""Validate a current pytest transcript against the archived branch floors."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

OUTCOME_NAMES = ("passed", "failed", "errors", "skipped", "xfailed", "xpassed")


class ValidationError(ValueError):
    pass


def parse(output: str) -> tuple[dict[str, int], tuple[str, ...]]:
    collected_matches = re.findall(r"collected\s+(\d+)\s+items?", output)
    if "test session starts" not in output or not collected_matches:
        raise ValidationError(
            "pytest transcript lacks a complete session/collection header"
        )
    summary = None
    for line in output.splitlines():
        if re.search(
            r"\b\d+\s+(?:passed|failed|errors?|skipped|xfailed|xpassed)\b", line
        ) and re.search(r"\bin\s+\d", line):
            summary = line
    if summary is None:
        raise ValidationError("pytest transcript lacks a completion summary")
    outcomes = {name: 0 for name in OUTCOME_NAMES}
    for count, raw_name in re.findall(
        r"(\d+)\s+(passed|failed|errors?|skipped|xfailed|xpassed)\b", summary
    ):
        outcomes["errors" if raw_name in {"error", "errors"} else raw_name] = int(count)
    if not any(outcomes.values()):
        raise ValidationError("pytest completion summary contains no outcomes")
    failures = tuple(
        sorted(
            {
                f"{kind} {node.strip()}"
                for kind, node in re.findall(
                    r"^(FAILED|ERROR)\s+(.+?)(?:\s+-\s+.*)?$", output, re.MULTILINE
                )
            }
        )
    )
    if sum(item.startswith("FAILED ") for item in failures) != outcomes["failed"]:
        raise ValidationError("FAILED details do not match the completion summary")
    if sum(item.startswith("ERROR ") for item in failures) != outcomes["errors"]:
        raise ValidationError("ERROR details do not match the completion summary")
    selected_matches = re.findall(r"/\s+(\d+)\s+selected\b", output)
    selected = (
        int(selected_matches[-1]) if selected_matches else int(collected_matches[-1])
    )
    completed_tests = sum(
        outcomes[name] for name in ("passed", "failed", "skipped", "xfailed", "xpassed")
    )
    if completed_tests < selected:
        raise ValidationError("pytest completion summary is partial")
    return outcomes, failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path, help="pytest transcript; stdin if omitted"
    )
    parser.add_argument("--floor", type=Path, required=True)
    parser.add_argument("--failures", type=Path, required=True)
    parser.add_argument("--mode", choices=("full", "focused"), required=True)
    args = parser.parse_args()
    output = (
        args.output.read_text(encoding="utf-8") if args.output else sys.stdin.read()
    )
    try:
        outcomes, failures = parse(output)
        floor = int(args.floor.read_text(encoding="utf-8").strip())
        expected_failures = tuple(
            sorted(
                line
                for line in args.failures.read_text(encoding="utf-8").splitlines()
                if line
            )
        )
        if args.mode == "full":
            completed = sum(outcomes.values())
            if completed < floor:
                raise ValidationError(
                    f"completed outcomes {completed} below floor {floor}"
                )
            if failures != expected_failures:
                raise ValidationError(
                    f"FAILED/ERROR set drifted: expected {expected_failures!r}, got {failures!r}"
                )
        else:
            if outcomes["passed"] < floor:
                raise ValidationError(
                    f"focused passes {outcomes['passed']} below floor {floor}"
                )
            if outcomes["failed"] or outcomes["errors"]:
                raise ValidationError("focused parity run has failed/error outcomes")
    except (OSError, TypeError, ValueError) as exc:
        print(f"pytest completion validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"pytest completion valid ({args.mode})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
