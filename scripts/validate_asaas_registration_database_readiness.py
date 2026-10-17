#!/usr/bin/env python3
"""Validate the single sanitized row returned by the Group 5 readiness SQL."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


class ReadinessError(ValueError):
    """The readiness SQL result is missing or failed a required check."""


_REQUIRED_SQL_FRAGMENTS = (
    "('billing_producer_v1_build_attestations','billing_producer_v1_build_attestations_pkey','p',array['id']::text[],null,null,null,null,null)",
    "('billing_producer_v1_build_attestations','ck_billing_producer_v1_attestation_envelope_digest','c',array['envelope_digest']::text[],'lengthenvelope_digest=64andenvelope_digest~''^[0-9a-f]{64}$''',null,null,null,null)",
    "('billing_producer_v1_build_attestations','ck_billing_producer_v1_attestation_payload_digest','c',array['payload_digest']::text[],'lengthpayload_digest=64andpayload_digest~''^[0-9a-f]{64}$''',null,null,null,null)",
    "('billing_producer_v1_build_attestations','uq_billing_producer_v1_attestation_digest','u',array['id','envelope_digest']::text[],null,null,null,null,null)",
    "('billing_producer_v1_build_attestations','billing_producer_v1_build_attestations_registered_by_user_id_fkey','f',array['registered_by_user_id']::text[],null,'public','users',array['id']::text[],'r')",
)
_REQUIRED_SQL_SEMANTICS = (
    "array(selecthost(task_ip.address)fromunnest(supplied.task_ips)astask_ip(address)orderbyhost(task_ip.address))astask_ips",
    "to_json(binding.task_ips)astask_ips",
    "pg_catalog.pg_get_expr(constraint_row.conbin,constraint_row.conrelid,true)",
    "=expected.check_expression",
    "=expected.key_columns",
    "constraint_row.confrelid=pg_catalog.to_regclass(",
    "=expected.referenced_columns",
    "constraint_row.confdeltype=expected.delete_action",
    "constraint_row.convalidated",
)


def validate_readiness_sql_contract(sql_text: str) -> None:
    """Check that the operator-facing SQL still measures migration semantics."""

    if not isinstance(sql_text, str):
        raise ReadinessError("readiness_sql_contract_invalid")
    uncommented = re.sub(r"/\*.*?\*/|--[^\n]*", "", sql_text, flags=re.DOTALL)
    normalized = re.sub(r"\s+", "", uncommented).lower()
    if not all(fragment in normalized for fragment in _REQUIRED_SQL_FRAGMENTS):
        raise ReadinessError("readiness_sql_constraint_contract_invalid")
    if not all(fragment in normalized for fragment in _REQUIRED_SQL_SEMANTICS):
        raise ReadinessError("readiness_sql_semantic_check_missing")


_ROLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")
_FIELDS = {
    "task_ips",
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
    task_ips = raw["task_ips"]
    if not isinstance(task_ips, list) or not task_ips:
        raise ReadinessError("readiness_result_task_ips_invalid")
    try:
        parsed_task_ips = [ipaddress.ip_address(value) for value in task_ips]
    except (TypeError, ValueError):
        raise ReadinessError("readiness_result_task_ips_invalid") from None
    if (
        any(address.version != 4 for address in parsed_task_ips)
        or any(str(address) != value for address, value in zip(parsed_task_ips, task_ips))
        or len(set(parsed_task_ips)) != len(parsed_task_ips)
    ):
        raise ReadinessError("readiness_result_task_ips_invalid")
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
        and len(task_ips) == task_count
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
        "task_ips": task_ips,
        "task_ip_count": task_count,
        "matched_task_ip_count": matched_count,
        "runtime_role_count": role_count,
        "api_runtime_role": role,
        "required_columns_present": True,
        "required_constraints_present": True,
        "required_privileges_present": True,
        "row_lock_privilege_present": True,
        "readiness_ok": True,
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
