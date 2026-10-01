#!/usr/bin/env python3
"""Extract marked README examples and run each one in a fresh subprocess.

Examples live inside the neurostack-core README between HTML comment markers
of the form:

    <!-- readme-example: NAME -->
    ```python
    ...example body...
    ```
    <!-- end-readme-example -->

The runner takes a Python interpreter (typically the one from a clean-venv
install of the built wheel) and executes each example once, in a fresh
subprocess so no preset selection leaks between examples.  Any non-zero
subprocess exit is reported with the offending stdout and stderr before
the runner fails.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_README = (
    REPO_ROOT / "packages" / "neurostack-core" / "README.md"
)

_EXAMPLE_RE = re.compile(
    r"<!--\s*readme-example:\s*(?P<name>[A-Za-z0-9_\-]+)\s*-->\s*"
    r"```python\n(?P<body>.*?)```\s*<!--\s*end-readme-example\s*-->",
    re.DOTALL,
)


@dataclass(frozen=True)
class Example:
    name: str
    body: str


def extract_examples(readme: Path) -> list[Example]:
    text = readme.read_text(encoding="utf-8")
    examples = [
        Example(name=match["name"], body=match["body"])
        for match in _EXAMPLE_RE.finditer(text)
    ]
    seen: set[str] = set()
    for example in examples:
        if example.name in seen:
            raise RuntimeError(f"duplicate readme-example name: {example.name}")
        seen.add(example.name)
    return examples


def run_example(
    python: Path,
    workdir: Path,
    example: Example,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    env = {"LC_ALL": "C.UTF-8", "PATH": os.environ.get("PATH", "")}
    return subprocess.run(
        [str(python), "-c", example.body],
        cwd=str(workdir),
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        type=Path,
        required=True,
        help="Python interpreter to run each example with (installed-wheel venv).",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="Working directory for each subprocess; defaults to the current dir.",
    )
    parser.add_argument(
        "--readme",
        type=Path,
        default=DEFAULT_README,
        help="Path to the README file to scan for marked examples.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=30,
        help="Per-example subprocess timeout in seconds (default: 30).",
    )
    args = parser.parse_args(argv)

    if not args.python.exists():
        print(f"python interpreter not found: {args.python}", file=sys.stderr)
        return 2
    workdir = args.workdir or Path.cwd()
    if not workdir.is_dir():
        print(f"workdir does not exist or is not a directory: {workdir}", file=sys.stderr)
        return 2
    if not args.readme.is_file():
        print(f"readme not found: {args.readme}", file=sys.stderr)
        return 2

    try:
        examples = extract_examples(args.readme)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if not examples:
        print(f"no marked examples found in {args.readme}", file=sys.stderr)
        return 1

    failed: list[tuple[str, subprocess.CompletedProcess[str]]] = []
    for example in examples:
        print(f"==> running example: {example.name}")
        try:
            completed = run_example(args.python, workdir, example, args.timeout)
        except subprocess.TimeoutExpired:
            print(f"FAIL {example.name}: timed out after {args.timeout}s", file=sys.stderr)
            return 1
        if completed.returncode != 0:
            print(f"FAIL {example.name}: exit {completed.returncode}", file=sys.stderr)
            if completed.stdout:
                print("--- stdout ---", file=sys.stderr)
                print(completed.stdout, file=sys.stderr)
            if completed.stderr:
                print("--- stderr ---", file=sys.stderr)
                print(completed.stderr, file=sys.stderr)
            failed.append((example.name, completed))
            continue
        if completed.stdout:
            sys.stdout.write(completed.stdout)
            if not completed.stdout.endswith("\n"):
                sys.stdout.write("\n")

    if failed:
        print(f"{len(failed)} example(s) failed", file=sys.stderr)
        return 1
    print(f"all {len(examples)} example(s) passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
