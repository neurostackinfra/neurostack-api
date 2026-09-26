#!/usr/bin/env python3
"""Extract the first Python fence following a README Quickstart heading."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

QUICKSTART = re.compile(r"^##[ \t]+Quickstart[ \t]*$")
SECTION_END = re.compile(r"^#{1,2}[ \t]+")
FENCE = re.compile(r"^```(?P<info>[^`]*)$")


def extract_quickstart(readme: Path) -> str:
    """Return the first Python code fence after the Quickstart heading."""
    lines = readme.read_text(encoding="utf-8").splitlines(keepends=True)
    heading_index = next(
        (
            index
            for index, line in enumerate(lines)
            if QUICKSTART.fullmatch(line.rstrip("\r\n"))
        ),
        None,
    )
    if heading_index is None:
        raise ValueError("README has no '## Quickstart' heading")

    section = []
    for line in lines[heading_index + 1 :]:
        if SECTION_END.match(line):
            break
        section.append(line)

    index = 0
    while index < len(section):
        line = section[index].rstrip("\r\n")
        fence = FENCE.fullmatch(line)
        if fence is None:
            if line.startswith("```"):
                raise ValueError("README Quickstart has a malformed code fence")
            index += 1
            continue

        info = fence.group("info").strip()
        if info.startswith("python") and info != "python":
            raise ValueError("README Quickstart has a malformed Python code fence")
        close_index = next(
            (
                candidate
                for candidate in range(index + 1, len(section))
                if section[candidate].rstrip("\r\n").strip() == "```"
            ),
            None,
        )
        if close_index is None:
            raise ValueError("README Quickstart has an unclosed code fence")
        if info == "python":
            return "".join(section[index + 1 : close_index])
        index = close_index + 1

    raise ValueError(
        "README Quickstart has no following Python code fence in its section"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("readme", type=Path)
    args = parser.parse_args()
    try:
        code = extract_quickstart(args.readme)
    except (OSError, ValueError) as exc:
        print(f"quickstart extraction failed: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(code)
    if code and not code.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
