#!/usr/bin/env python3
"""Fail closed when a deployment can consume 70% of PostgreSQL connections."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, NoReturn

PROFILE_FIELDS = {
    "tasks",
    "workers",
    "pool_size",
    "max_overflow",
    "pool_timeout",
    "max_connections",
    "threshold_percent",
}
APPROVED_PROFILES = {
    "vps-compose": {
        "tasks": 1,
        "workers": 4,
        "pool_size": 5,
        "max_overflow": 0,
        "pool_timeout": 30,
        "max_connections": 100,
        "threshold_percent": 70,
    },
    "ecs-poc": {
        "tasks": 2,
        "workers": 4,
        "pool_size": 5,
        "max_overflow": 0,
        "pool_timeout": 30,
        "max_connections": 100,
        "threshold_percent": 70,
    },
}


class BudgetError(ValueError):
    pass


def _fail(message: str) -> NoReturn:
    raise BudgetError(message)


def _integer(value: Any, field: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or value < minimum:
        _fail(f"{field} must be an integer >= {minimum}")
    return value


def validate_profile(value: Any, name: str) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != PROFILE_FIELDS:
        _fail(f"profile {name!r} must contain exactly {sorted(PROFILE_FIELDS)}")
    profile = {
        field: _integer(
            value[field], f"{name}.{field}", allow_zero=field == "max_overflow"
        )
        for field in PROFILE_FIELDS
    }
    total = (
        profile["tasks"]
        * profile["workers"]
        * (profile["pool_size"] + profile["max_overflow"])
    )
    if 100 * total >= profile["threshold_percent"] * profile["max_connections"]:
        _fail(
            f"{name} reserves {total}/{profile['max_connections']} connections; "
            f"must remain strictly below {profile['threshold_percent']}%"
        )
    return {**profile, "total": total}


def load_profile(manifest: Path, name: str) -> dict[str, int]:
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        document = None
    if document is None:
        _fail("cannot read budget manifest")
    if not isinstance(document, dict) or set(document) != {"profiles"}:
        _fail("manifest must contain only the profiles object")
    profiles = document["profiles"]
    if not isinstance(profiles, dict) or set(profiles) != set(APPROVED_PROFILES):
        _fail("manifest must contain exactly the approved deployment profiles")
    if name not in profiles:
        _fail(f"unknown profile: {name}")
    for profile_name, value in profiles.items():
        validated = validate_profile(value, str(profile_name))
        observed = {field: validated[field] for field in PROFILE_FIELDS}
        if observed != APPROVED_PROFILES[profile_name]:
            _fail(f"profile {profile_name!r} drifted from the approved topology")
    return validate_profile(profiles[name], name)


def _env_values(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        _fail(f"cannot read deploy environment: {type(error).__name__}")
    for number, raw_line in enumerate(lines, 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)=([^\s]*)", line)
        if match is None:
            _fail(f"{path} line {number} is not data-only KEY=value")
        key, value = match.groups()
        if key in result:
            _fail(f"{path} duplicates {key}")
        result[key] = value
    return result


def _compose_document(path: Path, env_file: Path) -> dict[str, Any]:
    """Render the effective Compose model with the production Compose engine."""

    command = [
        "/usr/bin/docker",
        "compose",
        "-f",
        str(path),
        "--env-file",
        str(env_file),
        "config",
        "--format",
        "json",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
            env=os.environ.copy(),
        )
    except (OSError, subprocess.TimeoutExpired):
        completed = None
    if completed is None or completed.returncode != 0:
        _fail("cannot render authoritative Compose structure")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError:
        value = None
    if not isinstance(value, dict) or not isinstance(value.get("services"), dict):
        _fail("rendered Compose must contain a services mapping")
    return value


def parse_worker_count(command: list[str], context: str) -> int:
    """Accept exactly one literal --workers option in either supported form."""

    occurrences: list[tuple[int, str]] = []
    for index, item in enumerate(command):
        if item == "--workers":
            occurrences.append((index, "separate"))
        elif item.startswith("--workers="):
            occurrences.append((index, "equals"))
    if len(occurrences) != 1:
        _fail(f"{context} must declare exactly one unambiguous --workers option")
    index, form = occurrences[0]
    if form == "separate":
        if index + 1 >= len(command):
            _fail(f"{context} --workers value is missing")
        raw = command[index + 1]
    else:
        raw = command[index].split("=", 1)[1]
    if not raw or not raw.isascii() or not raw.isdecimal():
        _fail(f"{context} --workers must be a literal integer")
    return int(raw)


def _production_workers(dockerfile: Path) -> int:
    try:
        docker_text = dockerfile.read_text(encoding="utf-8")
    except OSError as error:
        _fail(f"cannot read Dockerfile: {type(error).__name__}")
    from_matches = list(
        re.finditer(
            r"^FROM\s+(?P<parent>\S+)(?:\s+AS\s+(?P<alias>\S+))?\s*$",
            docker_text,
            re.MULTILINE | re.IGNORECASE,
        )
    )
    stages: dict[str, tuple[str, str]] = {}
    for index, match in enumerate(from_matches):
        alias = match.group("alias")
        if alias is None:
            continue
        start = match.end()
        end = (
            from_matches[index + 1].start()
            if index + 1 < len(from_matches)
            else len(docker_text)
        )
        normalized_alias = alias.lower()
        if normalized_alias in stages:
            _fail("Dockerfile duplicates a named stage")
        stages[normalized_alias] = (
            match.group("parent").lower(),
            docker_text[start:end],
        )
    if "production" not in stages:
        _fail("Dockerfile production stage is missing")

    cache: dict[str, tuple[list[str], list[str]]] = {}

    def resolve(stage_name: str, active: set[str]) -> tuple[list[str], list[str]]:
        if stage_name in cache:
            return cache[stage_name]
        if stage_name in active:
            _fail("Dockerfile stage inheritance is cyclic")
        parent, body = stages[stage_name]
        inherited_entrypoint: list[str] = []
        inherited_cmd: list[str] = []
        if parent in stages:
            inherited_entrypoint, inherited_cmd = resolve(parent, active | {stage_name})
        instructions = re.findall(
            r"^(ENTRYPOINT|CMD)\s+(.+)\s*$", body, re.MULTILINE | re.IGNORECASE
        )
        effective = {
            "ENTRYPOINT": list(inherited_entrypoint),
            "CMD": list(inherited_cmd),
        }
        for kind in ("ENTRYPOINT", "CMD"):
            values = [
                raw for instruction, raw in instructions if instruction.upper() == kind
            ]
            if len(values) > 1:
                _fail(f"Dockerfile {stage_name} stage duplicates {kind}")
            if not values:
                continue
            try:
                parsed = json.loads(values[0])
            except json.JSONDecodeError:
                parsed = None
            if not isinstance(parsed, list) or any(
                not isinstance(item, str) for item in parsed
            ):
                _fail(f"Dockerfile {stage_name} {kind} must be a JSON string array")
            effective[kind] = parsed
        result = effective["ENTRYPOINT"], effective["CMD"]
        cache[stage_name] = result
        return result

    production_body = stages["production"][1]
    production_cmds = re.findall(
        r"^CMD\s+(.+)\s*$", production_body, re.MULTILINE | re.IGNORECASE
    )
    if len(production_cmds) != 1:
        _fail("Dockerfile production stage must contain exactly one JSON CMD")
    entrypoint, cmd = resolve("production", set())
    effective = [*entrypoint, *cmd]
    return parse_worker_count(effective, "Dockerfile production command")


def validate_vps_facts(
    profile: dict[str, int], *, compose: Path, dockerfile: Path, env_file: Path
) -> None:
    env = _env_values(env_file)
    document = _compose_document(compose, env_file)
    workers = _production_workers(dockerfile)
    if workers != profile["workers"]:
        _fail("Dockerfile worker count drifted from vps-compose")

    services = document["services"]
    api = services.get("api")
    postgres = services.get("postgres")
    if not isinstance(api, dict) or not isinstance(postgres, dict):
        _fail("Compose must contain api and postgres services")
    if api.get("command") is not None or api.get("entrypoint") is not None:
        _fail("Compose api must not override the immutable image command")
    cardinalities: list[Any] = []
    if "scale" in api:
        cardinalities.append(api["scale"])
    deploy = api.get("deploy")
    if deploy is not None:
        if not isinstance(deploy, dict) or set(deploy) != {"replicas"}:
            _fail("Compose api deploy topology is ambiguous")
        cardinalities.append(deploy["replicas"])
    if len(cardinalities) > 1:
        _fail("Compose api declares duplicate cardinality sources")
    replicas = cardinalities[0] if cardinalities else 1
    if type(replicas) is not int or replicas != profile["tasks"]:
        _fail("Compose api replica count drifted from vps-compose")
    api_environment = api.get("environment")
    if not isinstance(api_environment, dict):
        _fail("Compose api environment must be a mapping")
    expected_environment = {
        "DATABASE_POOL_SIZE": str(profile["pool_size"]),
        "DATABASE_MAX_OVERFLOW": str(profile["max_overflow"]),
        "DATABASE_POOL_TIMEOUT": str(profile["pool_timeout"]),
    }
    for key, expected in expected_environment.items():
        if api_environment.get(key) != expected:
            _fail(f"Compose must independently propagate {key}")
    if postgres.get("command") != [
        "postgres",
        "-c",
        f"max_connections={profile['max_connections']}",
    ]:
        _fail("Compose PostgreSQL max_connections must be exactly 100")

    env_fields = {
        "DATABASE_POOL_SIZE": "pool_size",
        "DATABASE_MAX_OVERFLOW": "max_overflow",
        "DATABASE_POOL_TIMEOUT": "pool_timeout",
    }
    for key, profile_field in env_fields.items():
        raw = env.get(key)
        if raw is None or not raw.isascii() or not raw.isdecimal():
            _fail(f"{key} must be an unsigned integer in the deploy environment")
        parsed = int(raw)
        if profile_field is not None and parsed != profile[profile_field]:
            _fail(f"{key} drifted from vps-compose")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--verify-vps-compose", action="store_true")
    parser.add_argument("--compose", type=Path)
    parser.add_argument("--dockerfile", type=Path)
    parser.add_argument("--env-file", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        profile = load_profile(args.manifest, args.profile)
        if args.verify_vps_compose:
            if not all((args.compose, args.dockerfile, args.env_file)):
                _fail("VPS verification requires Compose, Dockerfile, and env paths")
            validate_vps_facts(
                profile,
                compose=args.compose,
                dockerfile=args.dockerfile,
                env_file=args.env_file,
            )
    except BudgetError as error:
        print(f"database connection budget rejected: {error}")
        return 1
    print(json.dumps({"profile": args.profile, **profile}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
