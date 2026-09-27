#!/usr/bin/env python3
"""Fail when the standalone core package reaches back into the API tree."""

from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

from _forbidden_imports import find_forbidden_imports


def violations(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return [f"{path}: cannot parse: {exc}"]
    problems: list[str] = []
    for occurrence in find_forbidden_imports(tree):
        if occurrence.category == "static-src-import":
            detail = f"static src import {occurrence.spelling!r}"
        else:
            detail = f"reserved dynamic-import syntax {occurrence.spelling!r}"
        problems.append(f"{path}:{occurrence.lineno}: {detail}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        default=[Path("packages/neurostack-core/neurostack_core")],
    )
    args = parser.parse_args()
    files: list[Path] = []
    for path in args.paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("*.py")))
        elif path.is_file():
            files.append(path)
        else:
            print(f"missing boundary path: {path}", file=sys.stderr)
            return 2
    problems = [problem for path in sorted(files) for problem in violations(path)]
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    print(f"package boundary clean ({len(files)} Python files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
