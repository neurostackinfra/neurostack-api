#!/usr/bin/env python3
"""Validate the single sanitized row returned by the Group 5 readiness SQL."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


class ReadinessError(ValueError):
    """The readiness SQL result is missing or failed a required check."""


_ROLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
_FIELDS = {
    "task_ip_count",
    "matched_task_ip_count",
    "runtime_role_count",
    "api_runtime_role",
    "required_columns_present",
    "required_constraints_present",
    "required_privileges_present",
    "row_lock_privilege_present",
    "readiness_ok",
}


def validate_readiness_row(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != _FIELDS:
        raise ReadinessError("readiness_result_shape_invalid")
    counts = (
        raw["task_ip_count"],
        raw["matched_task_ip_count"],
        raw["runtime_role_count"],
    )
    if any(type(value) is not int for value in counts):
        raise ReadinessError("readiness_result_counts_invalid")
    task_count, matched_count, role_count = counts
    role = raw["api_runtime_role"]
    if not (
        task_count > 0
        and matched_count == task_count
        and role_count == 1
        and isinstance(role, str)
        and _ROLE.fullmatch(role)
    ):
        raise ReadinessError("active_task_to_database_role_binding_failed")
    for field in (
        "required_columns_present",
        "required_constraints_present",
        "required_privileges_present",
        "row_lock_privilege_present",
        "readiness_ok",
    ):
        if type(raw[field]) is not bool:
            raise ReadinessError("readiness_result_boolean_invalid")
        if raw[field] is not True:
            raise ReadinessError(f"{field}_failed")
    return {
        "schema": "asaas_registration_database_readiness.v1",
        "task_ip_count": task_count,
        "matched_task_ip_count": matched_count,
        "runtime_role_count": role_count,
        "api_runtime_role": role,
        "required_columns_present": True,
        "required_constraints_present": True,
        "required_privileges_present": True,
        "row_lock_privilege_present": True,
        "readiness": "ready",
    }


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReadinessError("readiness_json_duplicate_key")
        result[key] = value
    return result


def _write_private(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if (
            args.input.is_symlink()
            or not args.input.is_file()
            or args.input.stat().st_size > 16_384
        ):
            raise ReadinessError("readiness_input_unreadable")
        payload = json.loads(
            args.input.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
        _write_private(args.output, validate_readiness_row(payload))
        return 0
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ReadinessError) as exc:
        reason = (
            str(exc)
            if isinstance(exc, ReadinessError)
            else "readiness_input_unreadable"
        )
        print(f"blocked: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
