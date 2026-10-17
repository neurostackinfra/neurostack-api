#!/usr/bin/env python3
"""Resolve the active DATABASE_URL selector to metadata-only version evidence.

The input is a sanitized projection of ECS and Secrets Manager readbacks. It must
not contain database URLs or secret values. This program never retrieves a
Secrets Manager value; it only resolves version identifiers and reference text.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class ReferenceError(ValueError):
    """Input metadata is missing, ambiguous, stale, or unsafe."""


_VERSION_ID = re.compile(r"^[A-Za-z0-9_-]{8,256}$")
_TIMESTAMP = re.compile(
    r"^(?P<base>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"
    r"\.(?P<fraction>\d{3,6})(?P<zone>Z|[+-]\d{2}:\d{2})$"
)


@dataclass(frozen=True)
class Selector:
    secret_arn: str
    json_key: str
    stage: str
    version_id: str


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ReferenceError(reason)


def _timestamp(value: object, *, field: str) -> datetime:
    _require(isinstance(value, str), f"{field}_missing_or_invalid")
    match = _TIMESTAMP.fullmatch(value)
    _require(match is not None, f"{field}_precision_ambiguous")
    try:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        result = datetime.fromisoformat(normalized)
    except ValueError:
        raise ReferenceError(f"{field}_invalid") from None
    _require(result.tzinfo is not None, f"{field}_timezone_missing")
    return result.astimezone(timezone.utc)


def _parse_selector(value_from: object, secret_arn: str) -> Selector:
    _require(
        isinstance(value_from, str) and value_from, "database_url_reference_missing"
    )
    _require(value_from.startswith(secret_arn), "database_url_secret_arn_mismatch")
    suffix = value_from[len(secret_arn) :]
    if not suffix:
        parts = ["", "", ""]
    else:
        _require(suffix.startswith(":"), "database_url_selector_invalid")
        parts = suffix[1:].split(":")
        _require(len(parts) <= 3, "database_url_selector_invalid")
        parts += [""] * (3 - len(parts))
    json_key, stage, version_id = parts
    _require(not (stage and version_id), "database_url_selector_ambiguous")
    if version_id:
        _require(
            _VERSION_ID.fullmatch(version_id) is not None,
            "database_url_version_id_invalid",
        )
    if stage:
        _require(
            bool(re.fullmatch(r"[A-Za-z0-9_-]{1,256}", stage)),
            "database_url_stage_invalid",
        )
    _require("\x00" not in json_key, "database_url_json_key_invalid")
    return Selector(secret_arn, json_key, stage, version_id)


def _find_secret_reference(
    task_definition: dict[str, Any], secret_arn: str
) -> Selector:
    containers = task_definition.get("containerDefinitions")
    _require(
        isinstance(containers, list) and containers,
        "task_definition_containers_missing",
    )
    matches: list[Selector] = []
    for container in containers:
        _require(isinstance(container, dict), "task_definition_container_invalid")
        secrets = container.get("secrets", [])
        _require(isinstance(secrets, list), "task_definition_secrets_invalid")
        for item in secrets:
            _require(isinstance(item, dict), "task_definition_secret_invalid")
            if item.get("name") == "DATABASE_URL":
                matches.append(_parse_selector(item.get("valueFrom"), secret_arn))
    _require(len(matches) == 1, "database_url_reference_count_mismatch")
    return matches[0]


def _resolve_version(
    selector: Selector, secret: dict[str, Any], versions: dict[str, Any]
) -> str:
    _require(secret.get("ARN") == selector.secret_arn, "secret_metadata_arn_mismatch")
    _require(versions.get("ARN") == selector.secret_arn, "secret_version_arn_mismatch")
    entries = versions.get("Versions")
    _require(isinstance(entries, list) and entries, "secret_versions_unreadable")
    version_ids: list[str] = []
    for item in entries:
        _require(isinstance(item, dict), "secret_version_metadata_invalid")
        version_id = item.get("VersionId")
        stages = item.get("VersionStages", [])
        _require(
            isinstance(version_id, str)
            and _VERSION_ID.fullmatch(version_id) is not None
            and isinstance(stages, list)
            and all(isinstance(stage, str) for stage in stages),
            "secret_version_metadata_invalid",
        )
        version_ids.append(version_id)
    _require(len(version_ids) == len(set(version_ids)), "secret_version_id_duplicate")
    if selector.version_id:
        _require(selector.version_id in version_ids, "explicit_secret_version_missing")
        return selector.version_id

    stage = selector.stage or "AWSCURRENT"
    selected = [
        item["VersionId"] for item in entries if stage in item.get("VersionStages", [])
    ]
    _require(len(selected) == 1, "secret_stage_resolution_ambiguous")
    return selected[0]


def resolve_active_database_secret(input_data: dict[str, Any]) -> dict[str, Any]:
    """Validate one stable active cohort and return a sanitized pinned reference."""

    _require(isinstance(input_data, dict), "input_invalid")

    def reject_secret_value_fields(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                _require(
                    key
                    not in {
                        "SecretString",
                        "SecretBinary",
                        "secret_value",
                        "database_url",
                    },
                    "secret_value_field_forbidden",
                )
                reject_secret_value_fields(nested)
        elif isinstance(value, list):
            for nested in value:
                reject_secret_value_fields(nested)

    reject_secret_value_fields(input_data)
    tasks = input_data.get("active_tasks")
    desired_count = input_data.get("desired_count")
    _require(type(desired_count) is int and desired_count > 0, "desired_count_invalid")
    _require(
        isinstance(tasks, list) and len(tasks) == desired_count,
        "active_task_cohort_mismatch",
    )

    task_arns: list[str] = []
    definition_arns: list[str] = []
    created: list[datetime] = []
    started: list[datetime] = []
    for task in tasks:
        _require(isinstance(task, dict), "active_task_metadata_invalid")
        task_arn = task.get("taskArn")
        definition_arn = task.get("taskDefinitionArn")
        _require(isinstance(task_arn, str) and task_arn, "active_task_arn_missing")
        _require(
            isinstance(definition_arn, str) and definition_arn,
            "active_task_definition_missing",
        )
        _require(
            task.get("lastStatus") == "RUNNING"
            and task.get("desiredStatus") == "RUNNING",
            "active_task_not_running",
        )
        task_arns.append(task_arn)
        definition_arns.append(definition_arn)
        created.append(_timestamp(task.get("createdAt"), field="task_created_at"))
        started.append(_timestamp(task.get("startedAt"), field="task_started_at"))
    _require(len(task_arns) == len(set(task_arns)), "active_task_arn_duplicate")
    _require(len(set(definition_arns)) == 1, "active_task_definitions_mixed")
    _require(
        all(start >= create for start, create in zip(started, created)),
        "task_time_order_invalid",
    )

    definitions = input_data.get("task_definitions")
    _require(isinstance(definitions, list), "task_definition_metadata_missing")
    matching_defs = [
        item
        for item in definitions
        if isinstance(item, dict)
        and item.get("taskDefinitionArn") == definition_arns[0]
    ]
    _require(len(matching_defs) == 1, "active_task_definition_unreadable")
    secret = input_data.get("secret_description")
    versions = input_data.get("secret_versions")
    _require(
        isinstance(secret, dict) and isinstance(versions, dict),
        "secret_metadata_unreadable",
    )
    secret_arn = secret.get("ARN")
    _require(
        isinstance(secret_arn, str)
        and re.fullmatch(
            r"arn:aws:secretsmanager:us-east-1:388460142431:secret:[A-Za-z0-9/_+=.@-]+",
            secret_arn,
        )
        is not None,
        "secret_arn_invalid",
    )
    selector = _find_secret_reference(matching_defs[0], secret_arn)
    version_id = _resolve_version(selector, secret, versions)

    last_changed = secret.get("LastChangedDate")
    last_changed_utc = _timestamp(last_changed, field="secret_last_changed")
    earliest_created = min(created)
    earliest_started = min(started)
    if not selector.version_id:
        _require(
            last_changed_utc + timedelta(seconds=60) <= earliest_created,
            "floating_secret_mapping_not_stable_before_task_pending",
        )
        _require(
            last_changed_utc < earliest_started, "secret_changed_during_task_startup"
        )

    json_key = selector.json_key
    pinned_value_from = f"{secret_arn}:{json_key}::{version_id}"
    _require(len(pinned_value_from) <= 2048, "replacement_secret_reference_too_long")
    return {
        "schema": "asaas_database_secret_reference.v1",
        "database_secret_arn": secret_arn,
        "database_secret_json_key": json_key,
        "source_selector_kind": (
            "version_id"
            if selector.version_id
            else ("stage" if selector.stage else "default")
        ),
        "source_selector_stage": selector.stage
        or ("AWSCURRENT" if not selector.version_id else ""),
        "resolved_version_id": version_id,
        "replacement_value_from": pinned_value_from,
        "active_task_count": len(tasks),
        "active_task_definition_arn": definition_arns[0],
        "earliest_task_created_at_utc": earliest_created.isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z"),
        "earliest_task_started_at_utc": earliest_started.isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z"),
        "secret_last_changed_at_utc": last_changed_utc.isoformat(
            timespec="microseconds"
        ).replace("+00:00", "Z"),
        "floating_selector_stability_seconds": 60 if not selector.version_id else 0,
    }


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReferenceError("input_json_duplicate_key")
        result[key] = value
    return result


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.exists() and not path.is_file():
        raise ReferenceError("output_path_unsafe")
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as output:
        output.write(raw)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        required=True,
        type=Path,
        help="sanitized ECS and Secrets Manager metadata JSON",
    )
    parser.add_argument(
        "--output", required=True, type=Path, help="new private output path"
    )
    args = parser.parse_args(argv)
    try:
        if args.input.is_symlink() or not args.input.is_file():
            raise ReferenceError("input_path_unsafe")
        raw = args.input.read_bytes()
        if len(raw) > 2 * 1024 * 1024:
            raise ReferenceError("input_too_large")
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
        result = resolve_active_database_secret(payload)
        _write_json(args.output, result)
        return 0
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ReferenceError) as exc:
        reason = (
            str(exc) if isinstance(exc, ReferenceError) else "metadata_input_unreadable"
        )
        print(f"blocked: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
