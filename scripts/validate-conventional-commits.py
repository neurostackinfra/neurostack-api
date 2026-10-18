#!/usr/bin/env python3
"""Validate Conventional Commit subjects in a git revision range.

Usage: ``validate-conventional-commits.py --range BASE..HEAD``.  Merge commits
are skipped.  An empty range fails unless ``--allow-empty`` is given, so a
mistyped range cannot pass vacuously.

Git's default revert subject (``Revert "feat: ..."``, as written by
``git revert``) is rejected: reword it to ``revert: <original subject>``
before the range is validated.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

CONVENTIONAL_SUBJECT = re.compile(
    r"(?:revert: )?(?:build|chore|ci|docs|feat|fix|perf|refactor|revert|style|test)"
    r"(?:\([^()\s]+\))?!?: \S.*"
)


def invalid_subjects(log_lines: list[str]) -> list[str]:
    """Return ``"<sha> <subject>"`` for every non-conventional subject."""

    invalid = []
    for line in log_lines:
        commit, _, subject = line.partition("\t")
        if not CONVENTIONAL_SUBJECT.fullmatch(subject):
            invalid.append(f"{commit} {subject}")
    return invalid


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--range", dest="revision_range", required=True)
    parser.add_argument("--allow-empty", action="store_true")
    args = parser.parse_args(argv)
    if args.revision_range.startswith("-"):
        print("revision range must not start with '-'", file=sys.stderr)
        return 2
    result = subprocess.run(
        ["git", "log", "--format=%H%x09%s", "--no-merges", args.revision_range, "--"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        print(result.stderr, end="", file=sys.stderr)
        return result.returncode
    lines = result.stdout.splitlines()
    if not lines and not args.allow_empty:
        print(f"no commits in range {args.revision_range}", file=sys.stderr)
        return 1
    invalid = invalid_subjects(lines)
    if invalid:
        print("non-conventional commit subjects:")
        print("\n".join(invalid))
        return 1
    print(f"conventional commits: {len(lines)} checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
