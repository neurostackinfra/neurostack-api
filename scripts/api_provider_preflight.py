#!/usr/bin/env python3
"""Operational evidence collector and release gate for API provider rollout.

Raw evidence is deliberately separated from the repository.  Credentials and
live endpoints are accepted only through environment variables; CLI arguments
are limited to non-sensitive selectors and aggregate values.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import http.client
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import ssl
import stat
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

SCHEMA_VERSION = "1.0"
RAW_KIND = "api-provider-preflight-raw"
ATTESTATION_KIND = "api-provider-preflight-attestation"
WINDOW_DAYS = 31
CAPACITY_MULTIPLIER = 1.5
MAX_MEMORY_FRACTION = 0.70
MAX_EVIDENCE_AGE = timedelta(hours=24)
MAX_REDIS_CLAIMS = 100_000
REDIS_PIPELINE_SIZE = 500
MAX_SMOKE_RESPONSE_BYTES = 64 * 1024
MAX_TRACKED_FILE_BYTES = 4 * 1024 * 1024
MAX_TRACKED_TOTAL_BYTES = 64 * 1024 * 1024
REDIS_CAPACITY_FORMULA = "peak+p95_daily*31*p95_claim*1.5<=0.70*maxmemory"
AUTH_FIELD = "evidence_auth"
AUTH_FIELDS = {"algorithm", "domain", "key_id", "signature"}
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TRACKED_ATTESTATION = REPOSITORY_ROOT / (
    ".genie/wishes/api-provider-settings/evidence/" "wave0-development.attestation.json"
)
TRACKED_ATTESTATIONS = {
    TRACKED_ATTESTATION,
    REPOSITORY_ROOT
    / ".genie/wishes/api-provider-settings/evidence/release-development.attestation.json",
    REPOSITORY_ROOT
    / ".genie/wishes/api-provider-settings/evidence/release-production.attestation.json",
}

SOURCE_NAMES = {
    "postgresql",
    "redis",
    "deployment-runtime",
    "message-observability",
    "controlled-zapi-collector",
    "external-edge-observability",
    "application-observability",
    "change-management",
}
SINK_NAMES = {"edge-logs", "application-logs", "metrics", "traces"}
BASELINE_LEAKS = {
    "application-log-raw-request-target",
    "application-metric-raw-request-target",
    "application-span-raw-request-target",
}
ROLLBACK_CHECKS = {
    "edge_closed",
    "legacy_workers_stopped",
    "v2_workers_only",
    "config_cache_only_deleted",
    "legacy_webhook_not_reopened",
    "forward_fix_selected",
}
FORBIDDEN_ATTESTATION_KEYS = re.compile(
    r"(?:tenant[_-]?id|patient|paciente|phone(?:[_-]?(?:number|id))?|email|"
    r"(?:callback|collector|provider)[_-]?url|raw[_-]?(?:path|target|value|evidence)|"
    r"(?:token|secret|credential)[_-]?(?:value|raw|material)|message[_-]?id|"
    r"collector[_-]?receipt|owner[_-]?name)",
    re.IGNORECASE,
)
ALLOWED_IDENTIFIER_KEYS = {"deployment_id", "reference_id"}
SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
CHANGE_REFERENCE = re.compile(r"^(?:CAB|CHG|INC|OPS)-[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SENTINEL = re.compile(r"^[A-Za-z0-9._~-]{16,128}$")
PLACEHOLDER_MARKERS = (
    "example",
    "placeholder",
    "your-",
    "your_",
    "test-",
    "invalid",
    "securepassword",
    "demo",
    "masked",
    "redacted",
    "changeme",
    "raw_",
    "_token_",
    "_secret_",
    "_value_",
    "<",
    "{",
    "...",
)

ATTESTATION_FIELDS = {
    "schema_version",
    "kind",
    "phase",
    "environment",
    "deployment_id",
    "collected_at",
    "window",
    "source_inventory",
    "sink_inventory",
    "aggregates",
    "threshold_results",
    "approvals",
    "decision",
    "design_return_criteria",
    AUTH_FIELD,
}
LIVE_RAW_FIELDS = {
    "schema_version",
    "kind",
    "phase",
    "environment",
    "representative",
    "deployment_id",
    "collected_at",
    "window",
    "source_mode",
    "source_inventory",
    "sink_inventory",
    "provenance",
    "aggregates",
    "redis",
    "capability",
    "application_redaction_baseline",
    "webhook_smoke",
    "approvals",
    "source_receipts",
    AUTH_FIELD,
}


class PreflightError(RuntimeError):
    """Expected operational input or validation failure."""


def _expected_sources(
    missing_meta: int, phase: str, active_zapi_count: int | None = None
) -> set[str]:
    expected = set(SOURCE_NAMES)
    if not missing_meta:
        expected.remove("change-management")
    if phase == "pre-cutover":
        expected.remove("controlled-zapi-collector")
    if active_zapi_count == 0 and phase != "pre-cutover":
        expected.difference_update(
            {
                "controlled-zapi-collector",
                "external-edge-observability",
                "application-observability",
            }
        )
    return expected


def _expected_sinks(phase: str, active_zapi_count: int) -> set[str]:
    if active_zapi_count == 0 and phase != "pre-cutover":
        return set()
    return set(SINK_NAMES)


def _utc_now(*, allow_override: bool = True) -> datetime:
    override = os.environ.get("API_PROVIDER_PREFLIGHT_NOW")
    if override:
        if not allow_override:
            raise PreflightError(
                "API_PROVIDER_PREFLIGHT_NOW is forbidden for live/release evidence"
            )
        value = datetime.fromisoformat(override.replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise PreflightError("API_PROVIDER_PREFLIGHT_NOW must include timezone")
        return value.astimezone(timezone.utc)
    return datetime.now(timezone.utc)


def _canonical_bytes(value: Mapping[str, Any], *, include_auth: bool = False) -> bytes:
    unsigned = dict(value)
    if not include_auth:
        unsigned.pop(AUTH_FIELD, None)
    return json.dumps(
        unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _artifact_role(value: Mapping[str, Any]) -> tuple[str, str]:
    kind = value.get("kind")
    if kind == RAW_KIND:
        return "collector", "collection"
    if kind == ATTESTATION_KIND:
        return "attestation-verifier", "attestation"
    artifacts = {
        "api-provider-webhook-smoke-raw": "webhook-smoke",
        "api-provider-redis-load-raw": "redis-rehearsal",
        "api-provider-rollback-rehearsal-raw": "rollback-rehearsal",
    }
    if kind in artifacts:
        return "receipt", artifacts[str(kind)]
    raise PreflightError("evidence kind has no signing role")


def _key_variable(role: str, artifact: str, environment: str, private: bool) -> str:
    suffix = "PRIVATE_KEY" if private else "PUBLIC_KEY"
    normalized = re.sub(r"[^A-Za-z0-9]", "_", artifact).upper()
    environment = re.sub(r"[^A-Za-z0-9]", "_", environment).upper()
    if role == "collector":
        return f"API_PROVIDER_COLLECTOR_{environment}_{suffix}"
    if role == "attestation-verifier":
        return f"API_PROVIDER_ATTESTATION_{environment}_{suffix}"
    return f"API_PROVIDER_RECEIPT_{normalized}_{environment}_{suffix}"


def _domain(role: str, artifact: str, environment: str) -> str:
    return f"api-provider-preflight/v1/{role}/{artifact}/{environment}"


def _decode_key(variable: str, private: bool) -> Ed25519PrivateKey | Ed25519PublicKey:
    encoded = os.environ.get(variable)
    if not encoded:
        raise PreflightError(f"{variable} must be injected by the secret manager")
    try:
        raw = base64.b64decode(encoded, validate=True)
        return (
            Ed25519PrivateKey.from_private_bytes(raw)
            if private
            else Ed25519PublicKey.from_public_bytes(raw)
        )
    except (ValueError, TypeError) as exc:
        raise PreflightError(f"{variable} is not a valid Ed25519 key") from exc


def _public_bytes(key: Ed25519PrivateKey | Ed25519PublicKey) -> bytes:
    public = key.public_key() if isinstance(key, Ed25519PrivateKey) else key
    return public.public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )


def _sign_evidence(value: dict[str, Any]) -> None:
    if os.environ.get("API_PROVIDER_EVIDENCE_HMAC_KEY"):
        raise PreflightError("legacy shared HMAC key is forbidden")
    role, artifact = _artifact_role(value)
    environment = value.get("environment")
    if not isinstance(environment, str):
        provenance = _mapping(value.get("provenance"), "provenance")
        environment = provenance.get("environment")
    if not isinstance(environment, str):
        raise PreflightError("signed evidence environment is required")
    key = _decode_key(_key_variable(role, artifact, environment, True), True)
    assert isinstance(key, Ed25519PrivateKey)
    public = _public_bytes(key)
    value[AUTH_FIELD] = {
        "algorithm": "Ed25519",
        "domain": _domain(role, artifact, environment),
        "key_id": hashlib.sha256(public).hexdigest()[:32],
        "signature": base64.b64encode(key.sign(_canonical_bytes(value))).decode(
            "ascii"
        ),
    }


def _verify_evidence_signature(value: Mapping[str, Any], field: str) -> None:
    if os.environ.get("API_PROVIDER_EVIDENCE_HMAC_KEY"):
        raise PreflightError("legacy shared HMAC key is forbidden")
    auth = _closed_mapping(value.get(AUTH_FIELD), field, AUTH_FIELDS)
    role, artifact = _artifact_role(value)
    environment = value.get("environment")
    if not isinstance(environment, str):
        environment = _mapping(value.get("provenance"), "provenance").get("environment")
    if not isinstance(environment, str):
        raise PreflightError(f"{field} environment is invalid")
    key = _decode_key(_key_variable(role, artifact, environment, False), False)
    assert isinstance(key, Ed25519PublicKey)
    expected_id = hashlib.sha256(_public_bytes(key)).hexdigest()[:32]
    if (
        auth.get("algorithm") != "Ed25519"
        or auth.get("domain") != _domain(role, artifact, environment)
        or auth.get("key_id") != expected_id
    ):
        raise PreflightError(f"{field} domain or key id is invalid")
    try:
        signature = base64.b64decode(str(auth.get("signature")), validate=True)
        key.verify(signature, _canonical_bytes(value))
    except (ValueError, InvalidSignature) as exc:
        raise PreflightError(f"{field} does not authenticate the evidence") from exc


def _signed_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value, include_auth=True)).hexdigest()


def _verify_receipt_digest(
    receipts: Mapping[str, Any], name: str, evidence: Mapping[str, Any], field: str
) -> None:
    if receipts.get(name) != _signed_digest(evidence):
        raise PreflightError(f"{field} does not match the signed raw digest")


def _iso(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _json_load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError(f"cannot read JSON evidence: {exc}") from exc
    if not isinstance(value, dict):
        raise PreflightError("evidence root must be a JSON object")
    return value


def _reject_symlink_ancestors(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.absolute().parts[1:]:
        current /= part
        if not current.exists() and not current.is_symlink():
            continue
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise PreflightError(f"cannot inspect path ancestor: {exc}") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise PreflightError("evidence paths must not contain symlink ancestors")


def _secure_json_under(
    root: Path,
    path: os.PathLike[str] | str,
    *,
    expected_mode: int = 0o600,
) -> dict[str, Any]:
    path_text = os.fspath(path)
    normalized = path_text.replace("\\", "/")
    parts = normalized.split("/")
    if (
        Path(path_text).is_absolute()
        or normalized.startswith("//")
        or re.match(r"^[A-Za-z]:/", normalized)
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise PreflightError(
            "evidence input must be a safe relative path without lexical escapes"
        )
    root = root.absolute()
    _reject_symlink_ancestors(root)
    relative = Path(*parts)
    directory_fd = _open_directory_fd(root)
    try:
        for part in relative.parts[:-1]:
            next_fd = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )
            os.close(directory_fd)
            directory_fd = next_fd
        file_fd = os.open(
            relative.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd
        )
        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise PreflightError("evidence input must be a regular file")
            if (
                metadata.st_uid != os.getuid()
                or stat.S_IMODE(metadata.st_mode) != expected_mode
            ):
                raise PreflightError(
                    f"evidence input must be owned and mode {expected_mode:04o}"
                )
            with os.fdopen(file_fd, "rb", closefd=False) as handle:
                encoded = handle.read()
        finally:
            os.close(file_fd)
    except OSError as exc:
        raise PreflightError(f"cannot securely open evidence input: {exc}") from exc
    finally:
        os.close(directory_fd)
    try:
        value = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PreflightError(f"cannot read JSON evidence: {exc}") from exc
    if not isinstance(value, dict):
        raise PreflightError("evidence root must be a JSON object")
    return value


def _closed_mapping(
    value: Any, field: str, required: set[str], optional: set[str] | None = None
) -> Mapping[str, Any]:
    result = _mapping(value, field)
    optional = optional or set()
    missing = required - set(result)
    extra = set(result) - required - optional
    if missing:
        raise PreflightError(f"{field} is missing fields: {', '.join(sorted(missing))}")
    if extra:
        raise PreflightError(
            f"{field} contains unknown fields: {', '.join(sorted(extra))}"
        )
    return result


def _timestamp(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise PreflightError(f"{field} must be a timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PreflightError(f"{field} must be a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PreflightError(f"{field} must include timezone")
    return parsed.astimezone(timezone.utc)


def _safe_existing_directory(directory: Path) -> None:
    try:
        metadata = directory.lstat()
    except OSError as exc:
        raise PreflightError(f"cannot inspect evidence directory: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise PreflightError(
            "evidence directory must be a real directory, not a symlink"
        )
    if metadata.st_uid != os.getuid():
        raise PreflightError("evidence directory must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise PreflightError("existing evidence directory must have mode 0700")


def _safe_existing_file(path: Path, expected_mode: int = 0o600) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise PreflightError(f"cannot inspect evidence file: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise PreflightError("evidence file must be a regular file, not a symlink")
    if metadata.st_uid != os.getuid():
        raise PreflightError("evidence file must be owned by the current user")
    if stat.S_IMODE(metadata.st_mode) != expected_mode:
        raise PreflightError(f"evidence file must have mode {expected_mode:04o}")


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _open_directory_fd(directory: Path) -> int:
    absolute = directory.absolute()
    descriptor = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in absolute.parts[1:]:
            next_descriptor = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor
    except OSError as exc:
        os.close(descriptor)
        raise PreflightError(f"cannot securely open directory: {exc}") from exc


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _reject_unsafe_lexical_path(path_text: str, field: str) -> None:
    if not path_text or "\x00" in path_text:
        raise PreflightError(f"{field} is empty or invalid")
    normalized = path_text.replace("\\", "/")
    segments = normalized.split("/")
    relative_segments = segments[1:] if normalized.startswith("/") else segments
    if any(part in {"", ".", ".."} for part in relative_segments):
        raise PreflightError(f"{field} contains unsafe lexical path segments")


def _evidence_dir() -> Path:
    configured = os.environ.get("API_PROVIDER_EVIDENCE_DIR")
    if not configured:
        raise PreflightError("API_PROVIDER_EVIDENCE_DIR is required")
    directory = Path(configured).expanduser().absolute()
    _reject_symlink_ancestors(directory.parent)
    if _is_relative_to(directory.resolve(strict=False), REPOSITORY_ROOT.resolve()):
        raise PreflightError("API_PROVIDER_EVIDENCE_DIR must be outside the worktree")
    if directory.exists() or directory.is_symlink():
        _safe_existing_directory(directory)
    else:
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        _safe_existing_directory(directory)
    return directory


def _raw_output(path_text: str) -> Path:
    _reject_unsafe_lexical_path(path_text, "raw output")
    directory = _evidence_dir()
    path = Path(path_text).expanduser().absolute()
    if not _is_relative_to(path, directory) or path == directory:
        raise PreflightError("raw output must be inside API_PROVIDER_EVIDENCE_DIR")
    relative_parent = path.parent.relative_to(directory)
    current = directory
    for part in relative_parent.parts:
        current /= part
        if current.exists() or current.is_symlink():
            _safe_existing_directory(current)
        else:
            current.mkdir(mode=0o700)
            _safe_existing_directory(current)
    if path.exists() or path.is_symlink():
        _safe_existing_file(path)
    return path


def _atomic_write(path: Path, value: Mapping[str, Any], mode: int) -> None:
    _reject_symlink_ancestors(path.parent)
    _safe_existing_directory(path.parent) if mode == 0o600 else None
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise PreflightError("output must be a regular file, not a symlink")
        if metadata.st_uid != os.getuid():
            raise PreflightError("output must be owned by the current user")
    directory_fd = _open_directory_fd(path.parent)
    temporary_name = f".{path.name}.{secrets.token_hex(12)}"
    fd = os.open(
        temporary_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        mode,
        dir_fd=directory_fd,
    )
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        if stat.S_IMODE(path.lstat().st_mode) != mode:
            raise PreflightError(f"output file must have mode {mode:04o}")
    finally:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _write_raw(path_text: str, value: Mapping[str, Any]) -> None:
    _atomic_write(_raw_output(path_text), value, 0o600)


def _int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise PreflightError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PreflightError(f"{field} must be an integer") from exc
    if parsed < 0:
        raise PreflightError(f"{field} must be non-negative")
    return parsed


def _strict_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PreflightError(f"{field} must be an integer")
    if value < 0:
        raise PreflightError(f"{field} must be non-negative")
    return cast(int, value)


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise PreflightError(f"{field} must be boolean")
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise PreflightError(f"{field} must be an object")
    return value


def _sequence(value: Any, field: str) -> Sequence[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise PreflightError(f"{field} must be an array")
    return cast(Sequence[Any], value)


def _percentile_95(values: Sequence[int]) -> int:
    if not values:
        raise PreflightError("31 daily unique-message counts are required")
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _load_fixture() -> dict[str, Any] | None:
    configured = os.environ.get("API_PROVIDER_PREFLIGHT_FIXTURE")
    if not configured:
        return None
    fixture = Path(configured).expanduser().resolve()
    if not fixture.is_file():
        raise PreflightError("API_PROVIDER_PREFLIGHT_FIXTURE does not exist")
    return _json_load(fixture)


def _normalize_database_url(value: str) -> str:
    return value.replace("postgresql+asyncpg://", "postgresql://", 1)


async def _collect_postgres() -> dict[str, Any]:
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        raise PreflightError("DATABASE_URL is required for live collection")
    try:
        import asyncpg
    except ImportError as exc:  # pragma: no cover - dependency is repository-owned
        raise PreflightError("asyncpg is unavailable") from exc

    connection = await asyncpg.connect(_normalize_database_url(database_url))
    try:
        transaction = connection.transaction(
            isolation="repeatable_read", readonly=True, deferrable=True
        )
        async with transaction:
            row = await connection.fetchrow("""
                SELECT
                  count(*) FILTER (
                    WHERE api_credentials->>'whatsapp_provider' = 'meta_cloud'
                      AND coalesce(api_credentials->>'meta_phone_number_id', '') <> ''
                      AND coalesce(api_credentials->>'meta_permanent_token', '') <> ''
                  ) AS active_meta,
                  count(*) FILTER (
                    WHERE api_credentials->>'whatsapp_provider' = 'meta_cloud'
                      AND coalesce(api_credentials->>'meta_phone_number_id', '') <> ''
                      AND coalesce(api_credentials->>'meta_permanent_token', '') <> ''
                      AND coalesce(api_credentials->>'meta_app_secret', '') = ''
                  ) AS meta_missing_secret,
                  count(*) FILTER (
                    WHERE coalesce(api_credentials->>'whatsapp_provider', 'zapi') = 'zapi'
                      AND coalesce(api_credentials->>'zapi_instance_id', '') <> ''
                      AND coalesce(api_credentials->>'zapi_token', '') <> ''
                  ) AS active_zapi
                FROM clinic_configs
                """)
    finally:
        await connection.close()
    if row is None:
        raise PreflightError("PostgreSQL inventory query returned no row")
    return {
        "active_meta_tenants": int(row["active_meta"]),
        "meta_missing_app_secret": int(row["meta_missing_secret"]),
        "active_zapi_tenants": int(row["active_zapi"]),
    }


async def _collect_redis() -> dict[str, Any]:
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        raise PreflightError("REDIS_URL is required for live collection")
    try:
        from redis.asyncio import Redis
    except ImportError as exc:  # pragma: no cover - dependency is repository-owned
        raise PreflightError("redis client is unavailable") from exc

    client = Redis.from_url(redis_url, decode_responses=True)
    try:
        memory = await client.info("memory")
        stats = await client.info("stats")
        policy = await client.config_get("maxmemory-policy")
        maxmemory = await client.config_get("maxmemory")
    finally:
        await client.aclose()
    return {
        "used_memory_peak_bytes": _int(
            memory.get("used_memory_peak", memory.get("used_memory", 0)),
            "redis.used_memory_peak_bytes",
        ),
        "maxmemory_bytes": _int(maxmemory.get("maxmemory", 0), "redis.maxmemory_bytes"),
        "maxmemory_policy": str(policy.get("maxmemory-policy", "unknown")),
        "evicted_keys": _int(stats.get("evicted_keys", 0), "redis.evicted_keys"),
    }


def _validate_provenance(
    value: Any,
    field: str,
    environment: str,
    deployment_id: str,
    *,
    now: datetime,
) -> Mapping[str, Any]:
    provenance = _closed_mapping(
        value,
        field,
        {"source", "environment", "deployment_id", "observed_at"},
    )
    source = provenance.get("source")
    if not isinstance(source, str) or source not in SOURCE_NAMES:
        raise PreflightError(f"{field}.source is unknown")
    if provenance.get("environment") != environment:
        raise PreflightError(f"{field}.environment does not match collection")
    if provenance.get("deployment_id") != deployment_id:
        raise PreflightError(f"{field}.deployment_id does not match collection")
    observed_at = _timestamp(provenance.get("observed_at"), f"{field}.observed_at")
    if observed_at > now or now - observed_at > MAX_EVIDENCE_AGE:
        raise PreflightError(f"{field}.observed_at is stale or in the future")
    return provenance


def _message_volume_from_env(
    environment: str, deployment_id: str, now: datetime
) -> dict[str, Any]:
    value = _external_evidence_json(
        "API_PROVIDER_MESSAGE_COUNTS_FILE",
        "31 daily unique-ID counts are required; the application database does not "
        "persist callback message IDs",
    )
    daily = [
        _int(item, "daily_unique_messages")
        for item in _sequence(value.get("daily"), "daily")
    ]
    if len(daily) != WINDOW_DAYS:
        raise PreflightError(
            "message-count evidence must contain exactly 31 daily values"
        )
    claim_bytes = _int(value.get("p95_claim_bytes"), "p95_claim_bytes")
    if claim_bytes == 0:
        raise PreflightError("p95_claim_bytes must be positive")
    window = _closed_mapping(
        value.get("window"), "message_volume.window", {"start", "end"}
    )
    start = _timestamp(window.get("start"), "message_volume.window.start")
    end = _timestamp(window.get("end"), "message_volume.window.end")
    if end - start != timedelta(days=WINDOW_DAYS) or end > now:
        raise PreflightError(
            "message-count evidence must cover one real 31-day interval"
        )
    provenance = _validate_provenance(
        value.get("provenance"),
        "message_volume.provenance",
        environment,
        deployment_id,
        now=now,
    )
    return {
        "p95_unique_messages_day": _percentile_95(daily),
        "p95_claim_bytes": claim_bytes,
        "observed_days": len(daily),
        "window": {"start": _iso(start), "end": _iso(end)},
        "source": provenance["source"],
    }


def _external_evidence_json(environment_name: str, detail: str) -> dict[str, Any]:
    configured = os.environ.get(environment_name)
    if not configured:
        raise PreflightError(f"{environment_name} is required: {detail}")
    _reject_unsafe_lexical_path(configured, environment_name)
    path = Path(configured).expanduser().absolute()
    directory = _evidence_dir()
    if not _is_relative_to(path, directory) or path == directory:
        raise PreflightError(
            f"{environment_name} must point inside API_PROVIDER_EVIDENCE_DIR"
        )
    return _secure_json_under(directory, path.relative_to(directory))


def _safe_fixture_observations(fixture: Mapping[str, Any]) -> dict[str, Any]:
    postgres = _mapping(fixture.get("postgresql"), "postgresql")
    redis = _mapping(fixture.get("redis"), "redis")
    volume = _mapping(fixture.get("message_volume"), "message_volume")
    capability = _mapping(fixture.get("capability"), "capability")
    baseline = _mapping(
        fixture.get("application_redaction_baseline"), "application_redaction_baseline"
    )
    deployment = _mapping(fixture.get("deployment"), "deployment")
    approvals = _mapping(fixture.get("approvals", {}), "approvals")
    daily = [
        _int(item, "daily")
        for item in _sequence(volume.get("daily"), "message_volume.daily")
    ]
    if len(daily) != WINDOW_DAYS:
        raise PreflightError("fixture message volume must have exactly 31 days")
    return {
        "aggregates": {
            "active_meta_tenants": _int(
                postgres.get("active_meta_tenants"), "active_meta_tenants"
            ),
            "active_zapi_tenants": _int(
                postgres.get("active_zapi_tenants"), "active_zapi_tenants"
            ),
            "meta_missing_app_secret": _int(
                postgres.get("meta_missing_app_secret"), "meta_missing_app_secret"
            ),
            "workers": _int(deployment.get("workers"), "workers"),
            "p95_unique_messages_day": _percentile_95(daily),
            "p95_claim_bytes": _int(volume.get("p95_claim_bytes"), "p95_claim_bytes"),
        },
        "redis": {
            "used_memory_peak_bytes": _int(
                redis.get("used_memory_peak_bytes"), "used_memory_peak_bytes"
            ),
            "maxmemory_bytes": _int(redis.get("maxmemory_bytes"), "maxmemory_bytes"),
            "maxmemory_policy": str(redis.get("maxmemory_policy", "unknown")),
            "evicted_keys": _int(redis.get("evicted_keys"), "evicted_keys"),
        },
        "capability": {
            "zapi_path_roundtrip_pass": _bool(
                capability.get("zapi_path_roundtrip_pass"), "zapi_path_roundtrip_pass"
            ),
            "external_edge_redaction_capable": _bool(
                capability.get("external_edge_redaction_capable"),
                "external_edge_redaction_capable",
            ),
        },
        "application_redaction_baseline": {
            "status": str(baseline.get("status", "")),
            "complete_redaction_pass": _bool(
                baseline.get("complete_redaction_pass"), "complete_redaction_pass"
            ),
            "leaks": sorted(
                str(item) for item in _sequence(baseline.get("leaks"), "leaks")
            ),
        },
        "webhook_smoke": {
            "status": "fixture_nonrepresentative",
            "active_zapi_count": _int(
                postgres.get("active_zapi_tenants"), "active_zapi_tenants"
            ),
            "readiness": False,
            "activation": False,
            "requires_fresh_smoke_before_activation": True,
            "collect_raw_digest": "0" * 64,
        },
        "approvals": dict(approvals),
    }


def _collect_command(args: argparse.Namespace) -> int:
    if args.window_days != WINDOW_DAYS:
        raise PreflightError("window-days must be exactly 31")
    if not SAFE_ID.fullmatch(args.deployment_id):
        raise PreflightError("deployment-id must be an opaque safe reference")
    fixture = _load_fixture()
    now = _utc_now(allow_override=fixture is not None)
    if fixture is not None:
        if os.environ.get("API_PROVIDER_EVIDENCE_HMAC_KEY"):
            raise PreflightError(
                "API_PROVIDER_EVIDENCE_HMAC_KEY must be unavailable in fixture mode"
            )
        observations = _safe_fixture_observations(fixture)
        source_mode = "fixture"
    else:
        runtime_environment = os.environ.get("API_PROVIDER_RUNTIME_ENV")
        runtime_deployment = os.environ.get("DEPLOYMENT_ID")
        if runtime_environment != args.environment:
            raise PreflightError(
                "API_PROVIDER_RUNTIME_ENV must match the requested live environment"
            )
        if runtime_deployment != args.deployment_id:
            raise PreflightError(
                "DEPLOYMENT_ID must match the requested live deployment"
            )
        postgres = asyncio.run(_collect_postgres())
        redis = asyncio.run(_collect_redis())
        message_volume = _message_volume_from_env(
            args.environment, args.deployment_id, now
        )
        if args.environment == "production" and args.phase != "pre-cutover":
            raise PreflightError(
                "production collection is allowed only for pre-cutover"
            )
        observation_variable = (
            "API_PROVIDER_PASSIVE_OBSERVABILITY_FILE"
            if args.phase == "pre-cutover"
            else "API_PROVIDER_WEBHOOK_SMOKE_FILE"
        )
        pre_smoke_collection = (
            args.environment == "development" and args.phase != "pre-cutover"
        )
        smoke: Mapping[str, Any]
        if pre_smoke_collection:
            smoke = {"source_inventory": [], "sink_inventory": []}
        else:
            smoke = _external_evidence_json(
                observation_variable,
                (
                    "passive observability evidence is required"
                    if args.phase == "pre-cutover"
                    else "controlled callback and edge evidence are required"
                ),
            )
        if args.phase != "pre-cutover" and not pre_smoke_collection:
            if smoke.get("kind") != "api-provider-webhook-smoke-raw":
                raise PreflightError("webhook smoke receipt has the wrong kind")
            _validate_receipt_shape(smoke, "webhook_smoke")
            _verify_evidence_signature(smoke, "webhook_smoke signature")
            smoke_state = _webhook_smoke_state(smoke, postgres["active_zapi_tenants"])
        smoke_provenance = (
            {
                "source": "deployment-runtime",
                "environment": args.environment,
                "deployment_id": args.deployment_id,
                "observed_at": _iso(now),
            }
            if pre_smoke_collection
            else _validate_provenance(
                smoke.get("provenance"),
                "webhook_smoke.provenance",
                args.environment,
                args.deployment_id,
                now=now,
            )
        )
        smoke_capability: Mapping[str, Any]
        smoke_baseline: Mapping[str, Any]
        if args.phase == "pre-cutover":
            smoke_capability = {
                "zapi_path_roundtrip_pass": False,
                "external_edge_redaction_capable": True,
            }
            smoke_baseline = {
                "status": "not-run-pre-cutover",
                "complete_redaction_pass": False,
                "leaks": [],
            }
            leaks: list[str] = []
            baseline_complete = False
            smoke_state = {
                "status": "not-run-pre-cutover",
                "active_zapi_count": postgres["active_zapi_tenants"],
                "readiness": False,
                "activation": False,
                "requires_fresh_smoke_before_activation": True,
                "collect_raw_digest": "0" * 64,
            }
        elif pre_smoke_collection:
            smoke_capability = {
                "zapi_path_roundtrip_pass": False,
                "external_edge_redaction_capable": False,
            }
            smoke_baseline = {
                "status": "pending_real_smoke",
                "complete_redaction_pass": False,
                "leaks": [],
            }
            leaks = []
            baseline_complete = False
            smoke_state = {
                "status": "pending_real_smoke",
                "active_zapi_count": postgres["active_zapi_tenants"],
                "readiness": False,
                "activation": False,
                "requires_fresh_smoke_before_activation": True,
                "collect_raw_digest": "0" * 64,
            }
        else:
            smoke_capability = _mapping(smoke.get("capability"), "capability")
            smoke_baseline = _mapping(
                smoke.get("application_redaction_baseline"),
                "application_redaction_baseline",
            )
            leaks = sorted(
                str(item)
                for item in _sequence(smoke_baseline.get("leaks"), "baseline.leaks")
            )
            if not set(leaks).issubset(BASELINE_LEAKS):
                raise PreflightError("application leak categories are not allowlisted")
            baseline_complete = _bool(
                smoke_baseline.get("complete_redaction_pass"),
                "complete_redaction_pass",
            )
            no_active_channel = (
                smoke_state["status"] == "not_applicable_no_active_channel"
            )
            if (
                args.phase == "wave0"
                and not no_active_channel
                and (baseline_complete or not leaks)
            ):
                raise PreflightError(
                    "observed application leak categories are required for Wave 0"
                )
            if (
                args.phase == "release"
                and not no_active_channel
                and (not baseline_complete or leaks)
            ):
                raise PreflightError(
                    "release requires observed complete application redaction"
                )
        observed_sources = {
            "postgresql",
            "redis",
            "deployment-runtime",
            str(message_volume["source"]),
            str(smoke_provenance["source"]),
        }
        observed_sources.update(
            str(item)
            for item in _sequence(smoke.get("source_inventory"), "source_inventory")
        )
        observed_sinks = {
            str(item)
            for item in _sequence(smoke.get("sink_inventory"), "sink_inventory")
        }
        approval_evidence: Mapping[str, Any] | None = None
        if os.environ.get("API_PROVIDER_META_APPROVAL_FILE"):
            approval_evidence = _external_evidence_json(
                "API_PROVIDER_META_APPROVAL_FILE",
                "Meta interruption acceptance is required when affected count is nonzero",
            )
            approval_provenance = _validate_provenance(
                approval_evidence.get("provenance"),
                "approval.provenance",
                args.environment,
                args.deployment_id,
                now=now,
            )
            if approval_provenance.get("source") != "change-management":
                raise PreflightError("approval provenance must be change-management")
            observed_sources.add("change-management")
        expected_sources = _expected_sources(
            postgres["meta_missing_app_secret"],
            args.phase,
            postgres["active_zapi_tenants"],
        )
        expected_sinks = _expected_sinks(args.phase, postgres["active_zapi_tenants"])
        if not pre_smoke_collection and (
            observed_sources != expected_sources or observed_sinks != expected_sinks
        ):
            raise PreflightError("observed source/sink inventory is incomplete")
        observations = {
            "aggregates": {
                **postgres,
                "workers": _int(
                    os.environ.get("API_PROVIDER_WORKERS"), "API_PROVIDER_WORKERS"
                ),
                "p95_unique_messages_day": message_volume["p95_unique_messages_day"],
                "p95_claim_bytes": message_volume["p95_claim_bytes"],
            },
            "redis": redis,
            "capability": smoke_capability,
            "application_redaction_baseline": {
                "status": smoke_baseline.get("status"),
                "complete_redaction_pass": baseline_complete,
                "leaks": leaks,
            },
            "webhook_smoke": smoke_state,
            "approvals": (
                dict(
                    _mapping(
                        approval_evidence.get("approvals"),
                        "approval_evidence.approvals",
                    )
                )
                if approval_evidence is not None
                else {}
            ),
            "source_receipts": {
                "postgresql": hashlib.sha256(_canonical_bytes(postgres)).hexdigest(),
                "redis": hashlib.sha256(_canonical_bytes(redis)).hexdigest(),
                "message_observability": hashlib.sha256(
                    _canonical_bytes(message_volume)
                ).hexdigest(),
                "observability": (
                    _signed_digest(smoke)
                    if args.phase != "pre-cutover" and not pre_smoke_collection
                    else hashlib.sha256(_canonical_bytes(smoke)).hexdigest()
                ),
                **(
                    {"smoke_collect_input": str(smoke_state["collect_raw_digest"])}
                    if args.phase != "pre-cutover"
                    else {}
                ),
            },
        }
        observations["capability"] = {
            "zapi_path_roundtrip_pass": _bool(
                observations["capability"].get("zapi_path_roundtrip_pass"),
                "zapi_path_roundtrip_pass",
            ),
            "external_edge_redaction_capable": _bool(
                observations["capability"].get("external_edge_redaction_capable"),
                "external_edge_redaction_capable",
            ),
        }
        source_mode = "live-read-only"

    start = now - timedelta(days=WINDOW_DAYS)
    representative = (
        args.environment in {"development", "production"}
        and source_mode == "live-read-only"
    )
    raw = {
        "schema_version": SCHEMA_VERSION,
        "kind": RAW_KIND,
        "phase": args.phase,
        "environment": args.environment,
        "representative": representative,
        "deployment_id": args.deployment_id,
        "collected_at": _iso(now),
        "window": {"days": WINDOW_DAYS, "start": _iso(start), "end": _iso(now)},
        "source_mode": source_mode,
        "source_inventory": (
            [] if source_mode == "fixture" else sorted(observed_sources)
        ),
        "sink_inventory": ([] if source_mode == "fixture" else sorted(observed_sinks)),
        "provenance": {
            "mode": source_mode,
            "environment": args.environment,
            "deployment_id": args.deployment_id,
        },
        **observations,
    }
    if source_mode == "live-read-only":
        _sign_evidence(raw)
    _write_raw(args.raw_output, raw)
    print(f"raw evidence written outside worktree ({source_mode})")
    return 0


def _approval_valid(
    approvals: Mapping[str, Any], missing_count: int
) -> tuple[bool, str]:
    if set(approvals) - {"meta_interruption_acceptance"}:
        return False, "approvals contains unknown fields"
    acceptance = approvals.get("meta_interruption_acceptance")
    if acceptance is None:
        return (
            (True, "not-required")
            if missing_count == 0
            else (False, "missing Meta interruption acceptance")
        )
    if not isinstance(acceptance, Mapping):
        return False, "Meta interruption acceptance must be an object"
    allowed = {
        "aggregate_count",
        "product_owner_approved",
        "production_operations_owner_approved",
        "reference_id",
        "approved_at",
    }
    if set(acceptance) != allowed:
        return False, "approval must contain exactly the allowlisted fields"
    try:
        aggregate_count = _strict_int(
            acceptance.get("aggregate_count"), "aggregate_count"
        )
    except PreflightError as exc:
        return False, str(exc)
    if aggregate_count != missing_count:
        return False, "Meta acceptance aggregate count does not match affected count"
    if acceptance.get("product_owner_approved") is not True:
        return False, "Product Owner approval is required"
    if acceptance.get("production_operations_owner_approved") is not True:
        return False, "Production Operations Owner approval is required"
    reference = acceptance.get("reference_id")
    if not isinstance(reference, str) or not CHANGE_REFERENCE.fullmatch(reference):
        return False, "an opaque approval reference id is required"
    timestamp = acceptance.get("approved_at")
    if not isinstance(timestamp, str):
        return False, "approval timestamp is required"
    try:
        _timestamp(timestamp, "approved_at")
    except PreflightError as exc:
        return False, str(exc)
    return True, "approved"


def _evaluate(
    raw: Mapping[str, Any], phase: str, *, verify_signature: bool = True
) -> tuple[dict[str, Any], list[str]]:
    if verify_signature:
        _verify_evidence_signature(raw, "raw evidence signature")
    if set(raw) != LIVE_RAW_FIELDS:
        raise PreflightError("live raw evidence must use the closed schema")
    if raw.get("schema_version") != SCHEMA_VERSION or raw.get("kind") != RAW_KIND:
        raise PreflightError("unsupported raw evidence schema")
    if raw.get("phase") != phase:
        raise PreflightError("requested phase does not match raw evidence")
    if phase not in {"wave0", "release", "pre-cutover"}:
        raise PreflightError("unsupported phase")
    now = _utc_now(allow_override=False)
    collected_at = _timestamp(raw.get("collected_at"), "collected_at")
    if collected_at > now or now - collected_at > MAX_EVIDENCE_AGE:
        raise PreflightError("raw evidence is stale or in the future")
    window = _mapping(raw.get("window"), "window")
    window_start = _timestamp(window.get("start"), "window.start")
    window_end = _timestamp(window.get("end"), "window.end")
    if (
        window.get("days") != WINDOW_DAYS
        or window_end - window_start != timedelta(days=WINDOW_DAYS)
        or window_end != collected_at
    ):
        raise PreflightError(
            "window must be one real 31-day interval ending at collection"
        )
    aggregates = _mapping(raw.get("aggregates"), "aggregates")
    redis = _mapping(raw.get("redis"), "redis")
    capability = _mapping(raw.get("capability"), "capability")
    baseline = _mapping(
        raw.get("application_redaction_baseline"), "application_redaction_baseline"
    )
    smoke_state = _closed_mapping(
        raw.get("webhook_smoke"),
        "webhook_smoke",
        {
            "status",
            "active_zapi_count",
            "readiness",
            "activation",
            "requires_fresh_smoke_before_activation",
            "collect_raw_digest",
        },
    )
    smoke_active_count = _strict_int(
        smoke_state.get("active_zapi_count"), "webhook_smoke.active_zapi_count"
    )
    smoke_readiness = _bool(smoke_state.get("readiness"), "webhook_smoke.readiness")
    smoke_activation = _bool(smoke_state.get("activation"), "webhook_smoke.activation")
    smoke_fresh_required = _bool(
        smoke_state.get("requires_fresh_smoke_before_activation"),
        "webhook_smoke.requires_fresh_smoke_before_activation",
    )
    approvals = _mapping(raw.get("approvals", {}), "approvals")
    receipt_names = {
        "postgresql",
        "redis",
        "message_observability",
        "observability",
    }
    if raw.get("environment") == "development":
        receipt_names.add("smoke_collect_input")
        if phase == "release":
            receipt_names.add("redis_rehearsal")
    receipts = _closed_mapping(
        raw.get("source_receipts"), "source_receipts", receipt_names
    )
    if any(
        not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
        for value in receipts.values()
    ):
        raise PreflightError("source receipts must be canonical SHA-256 digests")
    provenance = _closed_mapping(
        raw.get("provenance"),
        "provenance",
        {"mode", "environment", "deployment_id"},
    )
    if (
        provenance.get("mode") != "live-read-only"
        or provenance.get("environment") != raw.get("environment")
        or provenance.get("deployment_id") != raw.get("deployment_id")
    ):
        raise PreflightError("raw provenance is not representative live evidence")

    p95_messages = _int(
        aggregates.get("p95_unique_messages_day"), "p95_unique_messages_day"
    )
    claim_bytes = _int(aggregates.get("p95_claim_bytes"), "p95_claim_bytes")
    used_peak = _int(redis.get("used_memory_peak_bytes"), "used_memory_peak_bytes")
    maxmemory = _int(redis.get("maxmemory_bytes"), "maxmemory_bytes")
    evictions = _int(redis.get("evicted_keys"), "evicted_keys")
    projected = math.ceil(
        used_peak + p95_messages * WINDOW_DAYS * claim_bytes * CAPACITY_MULTIPLIER
    )
    limit = math.floor(maxmemory * MAX_MEMORY_FRACTION)
    missing_meta = _int(
        aggregates.get("meta_missing_app_secret"), "meta_missing_app_secret"
    )
    active_zapi = _strict_int(
        aggregates.get("active_zapi_tenants"), "active_zapi_tenants"
    )
    approval_pass, approval_reason = _approval_valid(approvals, missing_meta)
    workers = _strict_int(aggregates.get("workers"), "workers")
    if workers == 0:
        raise PreflightError("workers must be positive")

    baseline_leaks = baseline.get("leaks")
    wave0_baseline_valid = (
        baseline.get("status") == "expected-fail"
        and baseline.get("complete_redaction_pass") is False
        and isinstance(baseline_leaks, list)
        and bool(baseline_leaks)
        and set(baseline_leaks).issubset(BASELINE_LEAKS)
    )
    release_baseline_valid = (
        baseline.get("status") == "pass"
        and baseline.get("complete_redaction_pass") is True
        and baseline_leaks == []
    )
    pre_cutover_baseline_valid = (
        baseline.get("status") == "not-run-pre-cutover"
        and baseline.get("complete_redaction_pass") is False
        and baseline_leaks == []
    )
    no_active_channel = (
        raw.get("environment") == "development"
        and active_zapi == 0
        and smoke_state.get("status") == "not_applicable_no_active_channel"
        and smoke_active_count == 0
        and not smoke_readiness
        and not smoke_activation
        and smoke_fresh_required
        and capability.get("zapi_path_roundtrip_pass") is False
        and capability.get("external_edge_redaction_capable") is False
        and baseline.get("status") == "not_applicable_no_active_channel"
        and baseline.get("complete_redaction_pass") is False
        and baseline_leaks == []
    )
    active_smoke_bound = (
        raw.get("environment") == "development"
        and active_zapi > 0
        and smoke_state.get("status")
        in {"passed_real_callback", "failed_real_callback"}
        and smoke_active_count == active_zapi
        and smoke_readiness
        == (
            capability.get("zapi_path_roundtrip_pass") is True
            and capability.get("external_edge_redaction_capable") is True
        )
        and smoke_activation == smoke_readiness
        and smoke_fresh_required == (not smoke_readiness)
        and smoke_state.get("status")
        == ("passed_real_callback" if smoke_readiness else "failed_real_callback")
    )
    production_passive = (
        raw.get("environment") == "production"
        and phase == "pre-cutover"
        and smoke_state.get("status") == "not-run-pre-cutover"
        and smoke_active_count == active_zapi
        and not smoke_readiness
        and not smoke_activation
        and smoke_fresh_required
    )
    digest = smoke_state.get("collect_raw_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PreflightError("webhook smoke collect digest is invalid")
    if raw.get("environment") == "development" and (
        receipts.get("smoke_collect_input") != digest
        or not (no_active_channel or active_smoke_bound)
    ):
        raise PreflightError("development webhook smoke binding is invalid")
    if raw.get("environment") == "production" and not production_passive:
        raise PreflightError("production passive smoke state is invalid")
    baseline_valid = (
        (wave0_baseline_valid or no_active_channel)
        if phase == "wave0"
        else (
            pre_cutover_baseline_valid
            if phase == "pre-cutover"
            else (release_baseline_valid or no_active_channel)
        )
    )
    representative = (
        raw.get("representative") is True
        and raw.get("source_mode") == "live-read-only"
        and raw.get("environment") in {"development", "production"}
        and set(_sequence(raw.get("source_inventory"), "source_inventory"))
        == _expected_sources(missing_meta, phase, active_zapi)
        and set(_sequence(raw.get("sink_inventory"), "sink_inventory"))
        == _expected_sinks(phase, active_zapi)
    )
    results: dict[str, dict[str, Any]] = {
        "representative_evidence": {
            "pass": representative,
            "reason": "development-or-production-live-evidence-required",
        },
        "fixed_window": {
            "pass": window.get("days") == WINDOW_DAYS,
            "observed_days": window.get("days"),
            "required_days": WINDOW_DAYS,
        },
        "zapi_path_roundtrip": {
            "pass": (
                no_active_channel
                or (
                    active_smoke_bound
                    and capability.get("zapi_path_roundtrip_pass") is True
                )
            ),
            **(
                {"status": "not-run-pre-cutover"}
                if phase == "pre-cutover"
                else {
                    "status": smoke_state.get("status"),
                    "active_zapi_count": active_zapi,
                    "readiness": smoke_state.get("readiness"),
                    "activation": smoke_state.get("activation"),
                    "requires_fresh_smoke_before_activation": smoke_state.get(
                        "requires_fresh_smoke_before_activation"
                    ),
                }
            ),
        },
        "external_edge_redaction": {
            "pass": no_active_channel
            or (
                active_smoke_bound
                and capability.get("external_edge_redaction_capable") is True
            )
            or production_passive,
        },
        "redis_capacity": {
            "pass": maxmemory > 0 and projected <= limit,
            "projected_bytes": projected,
            "limit_bytes": limit,
            "formula_version": REDIS_CAPACITY_FORMULA,
        },
        "redis_evictions": {
            "pass": evictions == 0,
            "observed": evictions,
            "required": 0,
        },
        "meta_app_secret_readiness": {
            "pass": approval_pass,
            "affected_count": missing_meta,
            "reason": approval_reason,
        },
        "application_redaction_baseline": {
            "status": baseline.get("status"),
            "pass": (
                release_baseline_valid or no_active_channel
                if phase == "release"
                else False
            ),
            "recorded": baseline_valid,
            "leak_categories": (
                sorted(baseline_leaks) if isinstance(baseline_leaks, list) else []
            ),
        },
    }
    failures = [
        name
        for name, result in results.items()
        if not (
            (phase == "wave0" and name == "application_redaction_baseline")
            or (
                phase == "pre-cutover"
                and name in {"zapi_path_roundtrip", "application_redaction_baseline"}
            )
        )
        and result.get("pass") is not True
    ]
    if not baseline_valid:
        failures.append(
            "application_redaction_baseline_not_recorded_as_expected-fail"
            if phase == "wave0"
            else "application_redaction_not_observed_complete"
        )
    return results, failures


def _sanitized_approvals(raw_approvals: Mapping[str, Any]) -> dict[str, Any]:
    acceptance = raw_approvals.get("meta_interruption_acceptance")
    if not isinstance(acceptance, Mapping):
        return {}
    return {
        "meta_interruption_acceptance": {
            "aggregate_count": acceptance.get("aggregate_count"),
            "product_owner_approved": acceptance.get("product_owner_approved") is True,
            "production_operations_owner_approved": acceptance.get(
                "production_operations_owner_approved"
            )
            is True,
            "reference_id": acceptance.get("reference_id"),
            "approved_at": acceptance.get("approved_at"),
        }
    }


def _looks_secret(value: str) -> bool:
    if re.search(r"(?i)(?:^|\s)(?:bearer\s+|sk-[A-Za-z0-9_-]{12,})", value):
        return True
    if re.fullmatch(r"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", value):
        return True
    if re.search(r"(?i)(?:token|secret|password|api[_-]?key)\s*[:=]\s*\S+", value):
        return True
    if (
        len(value) >= 40
        and re.fullmatch(r"[A-Za-z0-9]+", value)
        and re.search(r"[A-Za-z]", value)
        and re.search(r"[0-9]", value)
    ):
        frequencies = {character: value.count(character) for character in set(value)}
        entropy = -sum(
            (count / len(value)) * math.log2(count / len(value))
            for count in frequencies.values()
        )
        if entropy >= 4.25 and not re.search(r"[\s/:]", value):
            return True
    return False


def _assert_attestation_safe(value: Any, key: str = "") -> None:
    if (
        key
        and key not in ALLOWED_IDENTIFIER_KEYS
        and FORBIDDEN_ATTESTATION_KEYS.search(key)
    ):
        raise PreflightError(f"attestation contains forbidden field: {key}")
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            _assert_attestation_safe(child, str(child_key))
    elif isinstance(value, list):
        for child in value:
            _assert_attestation_safe(child, key)
    elif isinstance(value, str):
        if key == "algorithm":
            if value != "Ed25519":
                raise PreflightError("attestation signature algorithm is invalid")
            return
        if key == "domain":
            if not re.fullmatch(
                r"api-provider-preflight/v1/attestation-verifier/attestation/"
                r"(?:development|production)",
                value,
            ):
                raise PreflightError("attestation signature domain is invalid")
            return
        if key == "key_id":
            if not re.fullmatch(r"[0-9a-f]{32}", value):
                raise PreflightError("attestation key id is invalid")
            return
        if key == "signature":
            try:
                if len(base64.b64decode(value, validate=True)) != 64:
                    raise ValueError
            except ValueError as exc:
                raise PreflightError("attestation signature is invalid") from exc
            return
        if re.search(r"https?://|/[A-Za-z0-9._~-]+", value):
            raise PreflightError("attestation contains URL or path-like value")
        if key == "deployment_id" and (
            not SAFE_ID.fullmatch(value) or _looks_secret(value)
        ):
            raise PreflightError("attestation deployment id is unsafe")
        if key == "reference_id" and not CHANGE_REFERENCE.fullmatch(value):
            raise PreflightError("attestation approval reference is unsafe")
        if key in {"collected_at", "approved_at", "start", "end"}:
            _timestamp(value, key)
        if key not in {"signature", "key_id"} and _looks_secret(value):
            raise PreflightError("attestation contains a probable secret string")


def _validate_attestation_shape(attestation: Mapping[str, Any]) -> None:
    if set(attestation) != ATTESTATION_FIELDS:
        raise PreflightError("attestation must use the closed top-level schema")
    _closed_mapping(attestation.get(AUTH_FIELD), AUTH_FIELD, AUTH_FIELDS)
    _closed_mapping(attestation.get("window"), "window", {"days", "start", "end"})
    _closed_mapping(
        attestation.get("aggregates"),
        "aggregates",
        {
            "active_meta_count",
            "active_zapi_count",
            "meta_missing_app_secret_count",
            "worker_count",
            "p95_unique_messages_day",
            "p95_claim_bytes",
        },
    )
    _closed_mapping(
        attestation.get("approvals"),
        "approvals",
        set(),
        {"meta_interruption_acceptance"},
    )
    approvals = _mapping(attestation.get("approvals"), "approvals")
    if "meta_interruption_acceptance" in approvals:
        _closed_mapping(
            approvals["meta_interruption_acceptance"],
            "approvals.meta_interruption_acceptance",
            {
                "aggregate_count",
                "product_owner_approved",
                "production_operations_owner_approved",
                "reference_id",
                "approved_at",
            },
        )
    thresholds = _closed_mapping(
        attestation.get("threshold_results"),
        "threshold_results",
        {
            "representative_evidence",
            "fixed_window",
            "zapi_path_roundtrip",
            "external_edge_redaction",
            "redis_capacity",
            "redis_evictions",
            "meta_app_secret_readiness",
            "application_redaction_baseline",
        },
    )
    production_skip = (
        attestation.get("environment") == "production"
        and attestation.get("phase") == "pre-cutover"
    )
    threshold_fields = {
        "representative_evidence": {"pass", "reason"},
        "fixed_window": {"pass", "observed_days", "required_days"},
        "zapi_path_roundtrip": (
            {"pass", "status"}
            if production_skip
            else {
                "pass",
                "status",
                "active_zapi_count",
                "readiness",
                "activation",
                "requires_fresh_smoke_before_activation",
            }
        ),
        "external_edge_redaction": {"pass"},
        "redis_capacity": {
            "pass",
            "projected_bytes",
            "limit_bytes",
            "formula_version",
        },
        "redis_evictions": {"pass", "observed", "required"},
        "meta_app_secret_readiness": {"pass", "affected_count", "reason"},
        "application_redaction_baseline": {
            "status",
            "pass",
            "recorded",
            "leak_categories",
        },
    }
    for name, fields in threshold_fields.items():
        _closed_mapping(thresholds.get(name), f"threshold_results.{name}", fields)


def _validate_receipt_shape(receipt: Mapping[str, Any], field: str) -> None:
    kind = receipt.get("kind")
    common = {"schema_version", "kind", "collected_at", "provenance", AUTH_FIELD}
    specific = {
        "api-provider-webhook-smoke-raw": {
            "source_inventory",
            "sink_inventory",
            "capability",
            "application_redaction_baseline",
            "status",
            "collect_binding",
            "readiness",
            "activation",
        },
        "api-provider-redis-load-raw": {
            "representative",
            "environment",
            "deployment_id",
            "input",
            "result",
        },
        "api-provider-rollback-rehearsal-raw": {
            "environment",
            "deployment_id",
            "checks",
            "pass",
            "collect_binding",
        },
    }
    if (
        kind == "api-provider-redis-load-raw"
        and receipt.get("environment") == "development"
    ):
        specific[str(kind)].add("collect_binding")
    if kind not in specific or set(receipt) != common | specific[str(kind)]:
        raise PreflightError(f"{field} must use its closed receipt schema")
    if receipt.get("schema_version") != SCHEMA_VERSION:
        raise PreflightError(f"{field} has unsupported schema version")
    _closed_mapping(receipt.get(AUTH_FIELD), f"{field}.{AUTH_FIELD}", AUTH_FIELDS)
    _closed_mapping(
        receipt.get("provenance"),
        f"{field}.provenance",
        {"source", "environment", "deployment_id", "observed_at"},
    )
    if kind == "api-provider-redis-load-raw":
        _closed_mapping(
            receipt.get("input"), f"{field}.input", {"claims", "claim_bytes"}
        )
        _closed_mapping(
            receipt.get("result"),
            f"{field}.result",
            {
                "attempted_claims",
                "claims_per_second",
                "memory_delta_bytes",
                "evicted_keys_delta",
                "inserted",
                "cleaned",
                "residual",
            },
        )
        if receipt.get("environment") == "development":
            _closed_mapping(
                receipt.get("collect_binding"),
                f"{field}.collect_binding",
                {"signed_raw_digest", "active_zapi_count"},
            )
    elif kind == "api-provider-webhook-smoke-raw":
        _closed_mapping(
            receipt.get("collect_binding"),
            f"{field}.collect_binding",
            {"signed_raw_digest", "active_zapi_count"},
        )
        _closed_mapping(
            receipt.get("readiness"),
            f"{field}.readiness",
            {"pass", "reason"},
        )
        _closed_mapping(
            receipt.get("activation"),
            f"{field}.activation",
            {"allowed", "requires_fresh_smoke_before_activation"},
        )
        _closed_mapping(
            receipt.get("capability"),
            f"{field}.capability",
            {"zapi_path_roundtrip_pass", "external_edge_redaction_capable"},
        )
        _closed_mapping(
            receipt.get("application_redaction_baseline"),
            f"{field}.application_redaction_baseline",
            {"status", "complete_redaction_pass", "leaks"},
        )
    elif kind == "api-provider-rollback-rehearsal-raw":
        _closed_mapping(
            receipt.get("collect_binding"),
            f"{field}.collect_binding",
            {"signed_raw_digest", "active_zapi_count"},
        )
        _closed_mapping(receipt.get("checks"), f"{field}.checks", ROLLBACK_CHECKS)


def _webhook_smoke_state(
    receipt: Mapping[str, Any], expected_active_count: int
) -> dict[str, Any]:
    binding = _mapping(receipt.get("collect_binding"), "collect_binding")
    readiness = _mapping(receipt.get("readiness"), "readiness")
    activation = _mapping(receipt.get("activation"), "activation")
    capability = _mapping(receipt.get("capability"), "capability")
    baseline = _mapping(
        receipt.get("application_redaction_baseline"),
        "application_redaction_baseline",
    )
    active_count = _strict_int(
        binding.get("active_zapi_count"), "collect_binding.active_zapi_count"
    )
    digest = binding.get("signed_raw_digest")
    if active_count != expected_active_count:
        raise PreflightError("webhook smoke active count does not match collection")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise PreflightError("webhook smoke collect digest is invalid")
    status = receipt.get("status")
    readiness_pass = _bool(readiness.get("pass"), "readiness.pass")
    activation_allowed = _bool(activation.get("allowed"), "activation.allowed")
    fresh_smoke_required = _bool(
        activation.get("requires_fresh_smoke_before_activation"),
        "activation.requires_fresh_smoke_before_activation",
    )
    path_pass = _bool(
        capability.get("zapi_path_roundtrip_pass"), "zapi_path_roundtrip_pass"
    )
    edge_pass = _bool(
        capability.get("external_edge_redaction_capable"),
        "external_edge_redaction_capable",
    )
    if active_count == 0:
        valid = (
            status == "not_applicable_no_active_channel"
            and readiness.get("reason") == "no-active-channel"
            and not readiness_pass
            and not activation_allowed
            and fresh_smoke_required
            and not path_pass
            and not edge_pass
            and baseline.get("status") == "not_applicable_no_active_channel"
            and baseline.get("complete_redaction_pass") is False
            and baseline.get("leaks") == []
        )
    else:
        successful = path_pass and edge_pass
        valid = (
            status == ("passed_real_callback" if successful else "failed_real_callback")
            and readiness.get("reason") == "real-callback-smoke"
            and readiness_pass is successful
            and activation_allowed is successful
            and fresh_smoke_required is (not successful)
        )
    if not valid:
        raise PreflightError("webhook smoke status/count/readiness matrix is invalid")
    return {
        "status": status,
        "active_zapi_count": active_count,
        "readiness": readiness_pass,
        "activation": activation_allowed,
        "requires_fresh_smoke_before_activation": fresh_smoke_required,
        "collect_raw_digest": digest,
    }


def _verify_command(args: argparse.Namespace) -> int:
    operational_evidence: list[tuple[str, str, Mapping[str, Any]]] = []
    if args.input_dir:
        _reject_unsafe_lexical_path(args.input_dir, "input-dir")
        input_directory = Path(args.input_dir).expanduser().absolute()
        raw_name = {
            "wave0": "wave0-development.raw.json",
            "release": "release-development.raw.json",
            "pre-cutover": "release-production.raw.json",
        }.get(args.phase)
        if raw_name is None:
            raise PreflightError("unsupported phase")
        input_path = input_directory / raw_name
        required_operational = (
            {
                "webhook-smoke.raw.json": "api-provider-webhook-smoke-raw",
            }
            if args.phase == "wave0"
            else (
                {
                    "redis-load.raw.json": "api-provider-redis-load-raw",
                    "webhook-smoke.raw.json": "api-provider-webhook-smoke-raw",
                    "rollback.raw.json": "api-provider-rollback-rehearsal-raw",
                }
                if args.phase == "release"
                else {}
            )
        )
        for filename, kind in required_operational.items():
            evidence: Mapping[str, Any] = _secure_json_under(
                input_path.parent, Path(filename)
            )
            if evidence.get("kind") != kind:
                raise PreflightError(f"{filename} has the wrong evidence kind")
            _validate_receipt_shape(evidence, filename)
            _verify_evidence_signature(evidence, f"{filename} signature")
            operational_evidence.append((filename, kind, evidence))
            if (
                kind == "api-provider-rollback-rehearsal-raw"
                and evidence.get("pass") is not True
            ):
                raise PreflightError("rollback rehearsal did not pass")
            if kind == "api-provider-redis-load-raw":
                result = _mapping(evidence.get("result"), "redis load result")
                if result.get("evicted_keys_delta") != 0:
                    raise PreflightError("Redis rehearsal observed evictions")
    else:
        _reject_unsafe_lexical_path(args.input, "input")
        input_path = Path(args.input).expanduser().absolute()
    evidence_directory = _evidence_dir()
    if (
        not _is_relative_to(input_path, evidence_directory)
        or input_path == evidence_directory
    ):
        raise PreflightError(
            "raw verifier input must be inside API_PROVIDER_EVIDENCE_DIR"
        )
    raw = _secure_json_under(
        evidence_directory, input_path.relative_to(evidence_directory)
    )
    runtime_environment = os.environ.get("API_PROVIDER_RUNTIME_ENV")
    runtime_deployment = os.environ.get("DEPLOYMENT_ID")
    if runtime_environment != raw.get("environment"):
        raise PreflightError("API_PROVIDER_RUNTIME_ENV does not match signed raw")
    if (
        not runtime_deployment
        or not SAFE_ID.fullmatch(runtime_deployment)
        or runtime_deployment != raw.get("deployment_id")
    ):
        raise PreflightError("DEPLOYMENT_ID does not match signed raw")
    if operational_evidence:
        _verify_evidence_signature(raw, "raw evidence signature")
        now = _utc_now(allow_override=False)
        collect_digest = _signed_digest(raw)
        combined_value = json.loads(json.dumps(raw))
        if not isinstance(combined_value, dict):
            raise PreflightError("signed raw copy is not an object")
        combined: dict[str, Any] = combined_value
        receipts = dict(_mapping(combined.get("source_receipts"), "source_receipts"))
        aggregates = _mapping(combined.get("aggregates"), "aggregates")
        active_count = _strict_int(
            aggregates.get("active_zapi_tenants"), "active_zapi_tenants"
        )
        for index, (filename, expected_kind, evidence) in enumerate(
            operational_evidence, start=1
        ):
            provenance = _validate_provenance(
                evidence.get("provenance"),
                f"operational_evidence_{index}.provenance",
                str(raw.get("environment")),
                str(raw.get("deployment_id")),
                now=now,
            )
            if provenance.get("source") not in {
                "redis",
                "controlled-zapi-collector",
                "deployment-runtime",
            }:
                raise PreflightError("operational evidence provenance is invalid")
            binding = _closed_mapping(
                evidence.get("collect_binding"),
                f"{filename}.collect_binding",
                {"signed_raw_digest", "active_zapi_count"},
            )
            if (
                binding.get("signed_raw_digest") != collect_digest
                or binding.get("active_zapi_count") != active_count
            ):
                raise PreflightError(f"{filename} does not bind the signed collect")
            if expected_kind == "api-provider-webhook-smoke-raw":
                smoke_state = _webhook_smoke_state(evidence, active_count)
                combined["webhook_smoke"] = smoke_state
                combined["capability"] = dict(
                    _mapping(evidence.get("capability"), "capability")
                )
                combined["application_redaction_baseline"] = dict(
                    _mapping(
                        evidence.get("application_redaction_baseline"),
                        "application_redaction_baseline",
                    )
                )
                combined["source_inventory"] = sorted(
                    set(_sequence(combined.get("source_inventory"), "source_inventory"))
                    | set(
                        _sequence(evidence.get("source_inventory"), "source_inventory")
                    )
                )
                combined["sink_inventory"] = sorted(
                    _sequence(evidence.get("sink_inventory"), "sink_inventory")
                )
                receipts["observability"] = _signed_digest(evidence)
                receipts["smoke_collect_input"] = collect_digest
            elif expected_kind == "api-provider-redis-load-raw":
                rehearsal_input = _mapping(evidence.get("input"), "redis load input")
                rehearsal_result = _mapping(evidence.get("result"), "redis load result")
                expected_claims = (
                    _strict_int(
                        aggregates.get("p95_unique_messages_day"),
                        "p95_unique_messages_day",
                    )
                    * WINDOW_DAYS
                )
                if (
                    rehearsal_input.get("claims") != expected_claims
                    or rehearsal_input.get("claim_bytes")
                    != aggregates.get("p95_claim_bytes")
                    or rehearsal_result.get("inserted") != expected_claims
                    or rehearsal_result.get("cleaned") != expected_claims
                    or rehearsal_result.get("residual") != 0
                ):
                    raise PreflightError("Redis rehearsal receipt is incoherent")
                receipts["redis_rehearsal"] = _signed_digest(evidence)
        combined["source_receipts"] = receipts
        raw = combined
        results, failures = _evaluate(raw, args.phase, verify_signature=False)
    else:
        results, failures = _evaluate(raw, args.phase)
    aggregates = _mapping(raw.get("aggregates"), "aggregates")
    attestation = {
        "schema_version": SCHEMA_VERSION,
        "kind": ATTESTATION_KIND,
        "phase": args.phase,
        "environment": raw.get("environment"),
        "deployment_id": raw.get("deployment_id"),
        "collected_at": raw.get("collected_at"),
        "window": {
            "days": _mapping(raw.get("window"), "window").get("days"),
            "start": _mapping(raw.get("window"), "window").get("start"),
            "end": _mapping(raw.get("window"), "window").get("end"),
        },
        "source_inventory": sorted(
            str(item)
            for item in _sequence(raw.get("source_inventory"), "source_inventory")
        ),
        "sink_inventory": sorted(
            str(item) for item in _sequence(raw.get("sink_inventory"), "sink_inventory")
        ),
        "aggregates": {
            "active_meta_count": _int(
                aggregates.get("active_meta_tenants"), "active_meta_tenants"
            ),
            "active_zapi_count": _int(
                aggregates.get("active_zapi_tenants"), "active_zapi_tenants"
            ),
            "meta_missing_app_secret_count": _int(
                aggregates.get("meta_missing_app_secret"), "meta_missing_app_secret"
            ),
            "worker_count": _int(aggregates.get("workers"), "workers"),
            "p95_unique_messages_day": _int(
                aggregates.get("p95_unique_messages_day"), "p95_unique_messages_day"
            ),
            "p95_claim_bytes": _int(
                aggregates.get("p95_claim_bytes"), "p95_claim_bytes"
            ),
        },
        "threshold_results": results,
        "approvals": _sanitized_approvals(
            _mapping(raw.get("approvals", {}), "approvals")
        ),
        "decision": "go" if not failures else "no-go",
        "design_return_criteria": sorted(failures),
    }
    if not isinstance(attestation["deployment_id"], str) or not SAFE_ID.fullmatch(
        attestation["deployment_id"]
    ):
        raise PreflightError("deployment id is missing or unsafe")
    expected_sources = _expected_sources(
        attestation["aggregates"]["meta_missing_app_secret_count"],
        args.phase,
        attestation["aggregates"]["active_zapi_count"],
    )
    if set(attestation["source_inventory"]) != expected_sources:
        raise PreflightError(
            "source inventory is incomplete or contains unknown sources"
        )
    if set(attestation["sink_inventory"]) != _expected_sinks(
        args.phase, attestation["aggregates"]["active_zapi_count"]
    ):
        raise PreflightError("sink inventory is incomplete or contains unknown sinks")
    _sign_evidence(attestation)
    _validate_attestation_shape(attestation)
    _assert_attestation_safe(attestation)
    output = Path(args.attestation).expanduser().absolute()
    _reject_symlink_ancestors(output.parent)
    if _is_relative_to(output, REPOSITORY_ROOT.absolute()) and output not in {
        path.absolute() for path in TRACKED_ATTESTATIONS
    }:
        raise PreflightError(
            "only allowlisted development or production attestations may be written in the worktree"
        )
    if not output.parent.exists():
        output.parent.mkdir(parents=True)
    _atomic_write(output, attestation, 0o644)
    if failures:
        print(
            "no-go: return to DESIGN; failed criteria: " + ", ".join(sorted(failures))
        )
        return 2
    print(f"go: {args.phase} external preconditions are satisfied")
    return 0


def _index_contents(root: Path) -> dict[str, bytes]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "-s", "-z"],
        cwd=root,
        check=False,
        capture_output=True,
    )
    if result.returncode != 0:
        raise PreflightError("cannot enumerate staged blobs")
    paths: dict[str, str] = {}
    for record in result.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, encoded_path = record.split(b"\t", 1)
            mode, object_id, stage = metadata.split(b" ", 2)
        except ValueError as exc:
            raise PreflightError("Git index entry framing is invalid") from exc
        if stage != b"0" or not re.fullmatch(rb"[0-9a-f]{40,64}", object_id):
            raise PreflightError("Git index contains an unsupported entry")
        if not re.fullmatch(rb"[0-7]{6}", mode):
            raise PreflightError("Git index mode is invalid")
        relative = encoded_path.decode("utf-8", errors="surrogateescape")
        paths[relative] = object_id.decode("ascii")
    object_ids = sorted(set(paths.values()))
    if not object_ids:
        return {}
    request = ("\n".join(object_ids) + "\n").encode("ascii")
    checked = subprocess.run(
        ["git", "cat-file", "--batch-check"],
        cwd=root,
        input=request,
        check=False,
        capture_output=True,
    )
    if checked.returncode != 0:
        raise PreflightError("cannot inspect staged blob sizes")
    sizes: dict[str, int] = {}
    total_size = 0
    for line in checked.stdout.splitlines():
        fields = line.split(b" ")
        if len(fields) != 3 or fields[1] != b"blob":
            raise PreflightError("Git index object is not a blob")
        checked_id = fields[0].decode("ascii")
        try:
            size = int(fields[2])
        except ValueError as exc:
            raise PreflightError("Git blob size is invalid") from exc
        if size < 0 or size > MAX_TRACKED_FILE_BYTES:
            raise PreflightError("tracked staged blob exceeds scan size limit")
        total_size += size
        if total_size > MAX_TRACKED_TOTAL_BYTES:
            raise PreflightError("tracked staged blobs exceed aggregate scan limit")
        sizes[checked_id] = size
    blobs = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=root,
        input=request,
        check=False,
        capture_output=True,
    )
    if blobs.returncode != 0:
        raise PreflightError("cannot read staged blobs")
    contents_by_id: dict[str, bytes] = {}
    offset = 0
    for requested_id in object_ids:
        newline = blobs.stdout.find(b"\n", offset)
        if newline < 0:
            raise PreflightError("Git blob batch header is truncated")
        header = blobs.stdout[offset:newline].split(b" ")
        if len(header) != 3 or header[1] != b"blob":
            raise PreflightError("Git blob batch header is invalid")
        response_id = header[0].decode("ascii")
        response_size = sizes.get(response_id)
        if (
            response_id != requested_id
            or response_size is None
            or int(header[2]) != response_size
        ):
            raise PreflightError("Git blob batch response does not match request")
        start = newline + 1
        end = start + response_size
        if end >= len(blobs.stdout) or blobs.stdout[end : end + 1] != b"\n":
            raise PreflightError("Git blob batch body is truncated")
        contents_by_id[response_id] = blobs.stdout[start:end]
        offset = end + 1
    if offset != len(blobs.stdout):
        raise PreflightError("Git blob batch response has trailing data")
    return {path: contents_by_id[object_id] for path, object_id in paths.items()}


def _safe_worktree_bytes(root: Path, relative: str) -> bytes | None:
    if "\\" in relative:
        raise PreflightError("tracked worktree path contains a literal backslash")
    parts = relative.split("/")
    if (
        Path(relative).is_absolute()
        or relative.startswith("//")
        or re.match(r"^[A-Za-z]:/", relative)
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise PreflightError("tracked worktree path contains unsafe components")
    directory_fd = _open_directory_fd(root)
    try:
        for part in parts[:-1]:
            try:
                next_fd = os.open(
                    part,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                return None
            os.close(directory_fd)
            directory_fd = next_fd
        try:
            file_fd = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory_fd
            )
        except FileNotFoundError:
            return None
        try:
            metadata = os.fstat(file_fd)
            if not stat.S_ISREG(metadata.st_mode):
                raise PreflightError("tracked worktree path is not a regular file")
            if metadata.st_size > MAX_TRACKED_FILE_BYTES:
                raise PreflightError("tracked worktree file exceeds scan size limit")
            with os.fdopen(file_fd, "rb", closefd=False) as handle:
                content = handle.read(MAX_TRACKED_FILE_BYTES + 1)
            if len(content) > MAX_TRACKED_FILE_BYTES:
                raise PreflightError("tracked worktree file grew beyond scan limit")
            return content
        finally:
            os.close(file_fd)
    except OSError as exc:
        raise PreflightError(
            f"cannot safely read tracked worktree file: {exc}"
        ) from exc
    finally:
        os.close(directory_fd)


def _looks_like_real_secret(value: str) -> bool:
    lowered = value.lower()
    if any(marker in lowered for marker in PLACEHOLDER_MARKERS):
        return False
    if len(value) < 16 or any(char.isspace() for char in value):
        return False
    classes = sum(
        bool(re.search(pattern, value))
        for pattern in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")
    )
    return (
        classes >= 2
        and bool(re.search(r"[A-Za-z]", value))
        and bool(re.search(r"\d", value))
    )


def _sensitive_json_value(value: Any, key: str = "") -> bool:
    if isinstance(value, Mapping):
        return any(
            _sensitive_json_value(child, str(child_key))
            for child_key, child in value.items()
        )
    if isinstance(value, list):
        return any(_sensitive_json_value(child, key) for child in value)
    sensitive_key = re.search(
        r"(?i)(?:zapi[_-]?token|api[_-]?key|access[_-]?token|app[_-]?secret|"
        r"password|authorization|credential|capability|tenant[_-]?id|patient|"
        r"phone|email|message[_-]?id)",
        key,
    )
    return bool(
        sensitive_key
        and isinstance(value, str)
        and (_looks_like_real_secret(value) or bool(re.search(r"https?://", value)))
    )


def _scan_content(path: Path, content: str) -> list[str]:
    findings: list[str] = []
    lowered_name = path.name.lower()
    if lowered_name.endswith(".raw.json") or "raw-evidence" in lowered_name:
        findings.append("raw evidence filename is tracked")
    if lowered_name.startswith(".env") and lowered_name not in {
        "env.example",
        ".env.example",
    }:
        findings.append("environment credential file is tracked")
    private_key = re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----\s+[A-Za-z0-9+/=\r\n]{32,}\s+"
        r"-----END [A-Z ]*PRIVATE KEY-----"
    )
    if private_key.search(content):
        findings.append("private key material")
    if re.search(r"\bAKIA[0-9A-Z]{16}\b", content):
        findings.append("cloud access key")
    capability = re.compile(r"/webhooks?/zapi/([A-Za-z0-9._~-]{16,})", re.IGNORECASE)
    for match in capability.finditer(content):
        if not any(marker in match.group(1).lower() for marker in PLACEHOLDER_MARKERS):
            findings.append("capability segment in URL/path")
            break
    quoted_assignment = re.compile(
        r"(?i)(?:zapi[_-]?token|api[_-]?key|access[_-]?token|app[_-]?secret|"
        r"password|authorization|credential|capability)"
        r"[\"']?[ \t]*[:=][ \t]*(?:\"([^\"\r\n]+)\"|'([^'\r\n]+)')"
    )
    unquoted_assignment = re.compile(
        r"(?i)(?:zapi[_-]?token|api[_-]?key|access[_-]?token|app[_-]?secret|"
        r"password|authorization|credential|capability)"
        r"[\"']?[ \t]*[:=][ \t]*(?![\"'])([^\s,;#}\]\)\"']{1,512})"
        r"(?=$|[\s,;#}\]\)\"'])"
    )
    assigned_values = (
        (match.group(1) or match.group(2))
        for match in quoted_assignment.finditer(content)
    )
    if any(_looks_like_real_secret(value) for value in assigned_values):
        findings.append("probable credential value")
    elif any(
        _looks_like_real_secret(match.group(1))
        for match in unquoted_assignment.finditer(content)
    ):
        findings.append("probable credential value")
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, Mapping):
        if parsed.get("kind") in {
            RAW_KIND,
            "api-provider-webhook-smoke-raw",
            "api-provider-redis-load-raw",
            "api-provider-rollback-rehearsal-raw",
        } or {
            "source_mode",
            "representative",
            "capability",
            "application_redaction_baseline",
        }.issubset(
            parsed
        ):
            findings.append("raw evidence content is tracked")
        if _sensitive_json_value(parsed):
            findings.append("sensitive JSON field contains a probable secret")
    if lowered_name.endswith(".attestation.json"):
        try:
            parsed = json.loads(content)
            if not isinstance(parsed, Mapping):
                raise PreflightError("attestation root must be an object")
            _validate_attestation_shape(parsed)
            _verify_evidence_signature(parsed, "attestation signature")
            _assert_attestation_safe(parsed)
        except (json.JSONDecodeError, PreflightError) as exc:
            findings.append(f"unsafe attestation: {exc}")
    return findings


def _scan_tracked_command(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    findings: list[str] = []
    index_contents = _index_contents(root)
    for relative, index_bytes in index_contents.items():
        path = root / relative
        try:
            index_content = index_bytes.decode("utf-8")
        except UnicodeDecodeError:
            index_content = ""
        for reason in _scan_content(path, index_content):
            findings.append(f"{relative} (index): {reason}")
        try:
            worktree_bytes = _safe_worktree_bytes(root, relative)
        except PreflightError as exc:
            findings.append(f"{relative} (worktree): unsafe or unreadable: {exc}")
            continue
        if worktree_bytes is None:
            continue
        try:
            worktree_content = worktree_bytes.decode("utf-8")
        except UnicodeDecodeError:
            worktree_content = ""
        if worktree_bytes != index_bytes:
            for reason in _scan_content(path, worktree_content):
                findings.append(f"{relative} (worktree): {reason}")
    if findings:
        print("tracked evidence/security scan failed", file=sys.stderr)
        for finding in sorted(findings):
            print(f"- {finding}", file=sys.stderr)
        return 1
    print("tracked evidence/security scan passed")
    return 0


def _public_https_target(url: str) -> tuple[str, int, str, str]:
    parsed = urlsplit(url)
    allowed_host = os.environ.get("API_PROVIDER_ZAPI_SMOKE_ALLOWED_HOST")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise PreflightError("smoke trigger must be an HTTPS public-host URL")
    hostname = parsed.hostname
    if hostname is None:
        raise PreflightError("smoke trigger hostname is required")
    port = parsed.port or 443
    if (
        not allowed_host
        or urlsplit(f"//{allowed_host}").hostname != allowed_host.lower()
        or hostname.lower() != allowed_host.lower()
    ):
        raise PreflightError("smoke trigger host does not match the allowed host")
    if port != 443:
        raise PreflightError("smoke trigger must use port 443")
    try:
        addresses = {
            str(entry[4][0])
            for entry in socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
        }
    except (OSError, ValueError) as exc:
        raise PreflightError("smoke trigger host cannot be resolved safely") from exc
    if not addresses:
        raise PreflightError("smoke trigger host has no address")
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as exc:
            raise PreflightError(
                "smoke trigger resolved to an invalid address"
            ) from exc
        if not parsed_address.is_global:
            raise PreflightError("smoke trigger must not resolve to a private address")
    target = parsed.path or "/"
    if parsed.query:
        target += f"?{parsed.query}"
    return hostname, port, sorted(addresses)[0], target


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, hostname: str, port: int, address: str) -> None:
        context = ssl.create_default_context()
        super().__init__(hostname, port=port, timeout=30, context=context)
        self._pinned_address = address
        self._pinned_context = context

    def connect(self) -> None:
        sock = socket.create_connection((self._pinned_address, self.port), self.timeout)
        self.sock = self._pinned_context.wrap_socket(sock, server_hostname=self.host)


def _http_json(
    url: str, payload: Mapping[str, Any], headers: Mapping[str, str]
) -> dict[str, Any]:
    hostname, port, address, target = _public_https_target(url)
    connection = _PinnedHTTPSConnection(hostname, port, address)
    try:
        connection.request(
            "POST",
            target,
            body=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", **headers},
        )
        response = connection.getresponse()
        if response.status < 200 or response.status >= 300:
            raise PreflightError(
                "controlled smoke request returned a non-success status"
            )
        encoded = response.read(MAX_SMOKE_RESPONSE_BYTES + 1)
        if len(encoded) > MAX_SMOKE_RESPONSE_BYTES:
            raise PreflightError("controlled smoke response is too large")
        result = json.loads(encoded.decode("utf-8"))
    except (
        OSError,
        ssl.SSLError,
        http.client.HTTPException,
        json.JSONDecodeError,
    ) as exc:
        raise PreflightError(
            f"controlled smoke request failed: {type(exc).__name__}"
        ) from exc
    finally:
        connection.close()
    if not isinstance(result, dict):
        raise PreflightError("controlled smoke response must be a JSON object")
    return result


def _smoke_collect_input(
    path_text: str, environment: str, deployment_id: str, now: datetime
) -> tuple[Mapping[str, Any], int]:
    _reject_unsafe_lexical_path(path_text, "collect-input")
    path = Path(path_text).expanduser().absolute()
    evidence_directory = _evidence_dir()
    if not _is_relative_to(path, evidence_directory) or path == evidence_directory:
        raise PreflightError("collect-input must be inside API_PROVIDER_EVIDENCE_DIR")
    raw = _secure_json_under(evidence_directory, path.relative_to(evidence_directory))
    _verify_evidence_signature(raw, "collect-input signature")
    if (
        set(raw) != LIVE_RAW_FIELDS
        or raw.get("schema_version") != SCHEMA_VERSION
        or raw.get("kind") != RAW_KIND
        or raw.get("phase") not in {"wave0", "release"}
        or raw.get("environment") != environment
        or raw.get("deployment_id") != deployment_id
        or raw.get("representative") is not True
        or raw.get("source_mode") != "live-read-only"
    ):
        raise PreflightError("collect-input identity or live schema is invalid")
    provenance = _closed_mapping(
        raw.get("provenance"),
        "collect-input.provenance",
        {"mode", "environment", "deployment_id"},
    )
    if provenance != {
        "mode": "live-read-only",
        "environment": environment,
        "deployment_id": deployment_id,
    }:
        raise PreflightError("collect-input provenance does not match runtime")
    collected_at = _timestamp(raw.get("collected_at"), "collect-input.collected_at")
    if collected_at > now or now - collected_at > MAX_EVIDENCE_AGE:
        raise PreflightError("collect-input is stale or in the future")
    aggregates = _mapping(raw.get("aggregates"), "collect-input.aggregates")
    active_count = _strict_int(
        aggregates.get("active_zapi_tenants"), "active_zapi_tenants"
    )
    return raw, active_count


def _release_collect_binding(
    environment: str, deployment_id: str, now: datetime
) -> dict[str, Any]:
    collect_path = _evidence_dir() / "release-development.raw.json"
    raw, active_count = _smoke_collect_input(
        str(collect_path), environment, deployment_id, now
    )
    if raw.get("phase") != "release":
        raise PreflightError("release rehearsal requires the signed release collect")
    return {
        "signed_raw_digest": _signed_digest(raw),
        "active_zapi_count": active_count,
    }


def _webhook_smoke_command(args: argparse.Namespace) -> int:
    now = _utc_now(allow_override=False)
    runtime_environment = os.environ.get("API_PROVIDER_RUNTIME_ENV")
    runtime_deployment = os.environ.get("DEPLOYMENT_ID")
    if runtime_environment != args.environment:
        raise PreflightError("API_PROVIDER_RUNTIME_ENV must match --environment")
    if not runtime_deployment or not SAFE_ID.fullmatch(runtime_deployment):
        raise PreflightError("DEPLOYMENT_ID must be an opaque safe runtime reference")
    collect_raw, active_count = _smoke_collect_input(
        args.collect_input, args.environment, runtime_deployment, now
    )
    collect_binding = {
        "signed_raw_digest": _signed_digest(collect_raw),
        "active_zapi_count": active_count,
    }
    if active_count == 0:
        raw = {
            "schema_version": SCHEMA_VERSION,
            "kind": "api-provider-webhook-smoke-raw",
            "collected_at": _iso(now),
            "provenance": {
                "source": "deployment-runtime",
                "environment": args.environment,
                "deployment_id": runtime_deployment,
                "observed_at": _iso(now),
            },
            "status": "not_applicable_no_active_channel",
            "collect_binding": collect_binding,
            "readiness": {"pass": False, "reason": "no-active-channel"},
            "activation": {
                "allowed": False,
                "requires_fresh_smoke_before_activation": True,
            },
            "source_inventory": sorted(
                str(item)
                for item in _sequence(
                    collect_raw.get("source_inventory"), "source_inventory"
                )
            ),
            "sink_inventory": sorted(
                str(item)
                for item in _sequence(
                    collect_raw.get("sink_inventory"), "sink_inventory"
                )
            ),
            "capability": {
                "zapi_path_roundtrip_pass": False,
                "external_edge_redaction_capable": False,
            },
            "application_redaction_baseline": {
                "status": "not_applicable_no_active_channel",
                "complete_redaction_pass": False,
                "leaks": [],
            },
        }
        _sign_evidence(raw)
        _write_raw(args.raw_output, raw)
        return 0

    trigger_url = os.environ.get("API_PROVIDER_ZAPI_SMOKE_TRIGGER_URL")
    bearer = os.environ.get("API_PROVIDER_ZAPI_SMOKE_BEARER")
    sentinel = os.environ.get("API_PROVIDER_ZAPI_SENTINEL")
    if not trigger_url or not bearer or not sentinel:
        raise PreflightError(
            "active-channel webhook smoke requires trigger URL, bearer, and sentinel from the secret manager/environment"
        )
    if not SENTINEL.fullmatch(sentinel):
        raise PreflightError(
            "smoke sentinel must be an opaque 16-128 byte URL-safe segment"
        )
    result = _http_json(
        trigger_url,
        {"sentinel": sentinel},
        {"Authorization": f"Bearer {bearer}"},
    )
    observed = result.get("observed_sentinel")
    edge_template_only = result.get("edge_template_only") is True
    application_leaks = result.get("application_leak_categories", [])
    complete_redaction = result.get("complete_redaction_pass") is True
    observed_sources = sorted(
        str(item)
        for item in _sequence(
            result.get("observed_source_inventory"), "observed_source_inventory"
        )
    )
    observed_sinks = sorted(
        str(item)
        for item in _sequence(
            result.get("observed_sink_inventory"), "observed_sink_inventory"
        )
    )
    if (
        set(observed_sources)
        != {
            "controlled-zapi-collector",
            "external-edge-observability",
            "application-observability",
        }
        or set(observed_sinks) != SINK_NAMES
    ):
        raise PreflightError("smoke observed source/sink inventory is incomplete")
    leaks = sorted(
        str(item)
        for item in _sequence(application_leaks, "application_leak_categories")
        if item in BASELINE_LEAKS
    )
    if complete_redaction == bool(leaks):
        raise PreflightError(
            "smoke must report either observed leaks or complete application redaction"
        )
    path_pass = isinstance(observed, str) and observed.encode(
        "utf-8"
    ) == sentinel.encode("utf-8")
    readiness_pass = path_pass and edge_template_only
    raw = {
        "schema_version": SCHEMA_VERSION,
        "kind": "api-provider-webhook-smoke-raw",
        "collected_at": _iso(now),
        "provenance": {
            "source": "controlled-zapi-collector",
            "environment": args.environment,
            "deployment_id": runtime_deployment,
            "observed_at": _iso(now),
        },
        "status": (
            "passed_real_callback" if readiness_pass else "failed_real_callback"
        ),
        "collect_binding": collect_binding,
        "readiness": {"pass": readiness_pass, "reason": "real-callback-smoke"},
        "activation": {
            "allowed": readiness_pass,
            "requires_fresh_smoke_before_activation": not readiness_pass,
        },
        "source_inventory": observed_sources,
        "sink_inventory": observed_sinks,
        "capability": {
            "zapi_path_roundtrip_pass": path_pass,
            "external_edge_redaction_capable": edge_template_only,
        },
        "application_redaction_baseline": {
            "status": "pass" if complete_redaction else "expected-fail",
            "complete_redaction_pass": complete_redaction,
            "leaks": leaks,
        },
    }
    _sign_evidence(raw)
    _write_raw(args.raw_output, raw)
    return 0 if readiness_pass else 2


async def _redis_load(
    prefix: str, count: int, ttl_seconds: int, claim_bytes: int
) -> dict[str, Any]:
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        raise PreflightError("REDIS_URL is required")
    from redis.asyncio import Redis

    client = Redis.from_url(redis_url, decode_responses=False)
    ownership = secrets.token_urlsafe(24).encode("ascii")
    value = ownership + b":" + b"0" * max(0, claim_bytes - len(ownership) - 1)
    keys = [f"{prefix}:{index}" for index in range(count)]
    inserted = 0
    cleaned = 0
    residual = 0
    before = await client.info("stats")
    memory_before = await client.info("memory")
    started = time.monotonic()
    operation_error: Exception | None = None
    try:
        for offset in range(0, len(keys), REDIS_PIPELINE_SIZE):
            batch = keys[offset : offset + REDIS_PIPELINE_SIZE]
            pipeline = client.pipeline(transaction=False)
            for key in batch:
                pipeline.set(key, value, ex=ttl_seconds, nx=True)
            responses = await pipeline.execute()
            inserted += sum(response is True for response in responses)
        memory_after = await client.info("memory")
        after = await client.info("stats")
    except Exception as exc:  # cleanup and report a lost acknowledgement safely
        operation_error = exc
    finally:
        try:
            cleanup_script = (
                "if redis.call('get', KEYS[1]) == ARGV[1] then "
                "return redis.call('del', KEYS[1]) else return 0 end"
            )
            owned: list[Any] = []
            cursor: int | bytes = 0
            while True:
                cursor, candidates = await client.scan(
                    cursor=cursor,
                    match=f"{prefix}:*",
                    count=REDIS_PIPELINE_SIZE,
                )
                for offset in range(0, len(candidates), REDIS_PIPELINE_SIZE):
                    batch = candidates[offset : offset + REDIS_PIPELINE_SIZE]
                    pipeline = client.pipeline(transaction=False)
                    for key in batch:
                        pipeline.get(key)
                    observed_values = await pipeline.execute()
                    owned.extend(
                        key
                        for key, observed_value in zip(batch, observed_values)
                        if observed_value == value
                    )
                if cursor in {0, b"0", "0"}:
                    break
            inserted = len(owned)
            for offset in range(0, len(owned), REDIS_PIPELINE_SIZE):
                pipeline = client.pipeline(transaction=False)
                for key in owned[offset : offset + REDIS_PIPELINE_SIZE]:
                    pipeline.eval(cleanup_script, 1, key, value)
                cleanup = await pipeline.execute()
                cleaned += sum(result == 1 for result in cleanup)
            cursor = 0
            while True:
                cursor, candidates = await client.scan(
                    cursor=cursor,
                    match=f"{prefix}:*",
                    count=REDIS_PIPELINE_SIZE,
                )
                for offset in range(0, len(candidates), REDIS_PIPELINE_SIZE):
                    batch = candidates[offset : offset + REDIS_PIPELINE_SIZE]
                    pipeline = client.pipeline(transaction=False)
                    for key in batch:
                        pipeline.get(key)
                    observed_values = await pipeline.execute()
                    residual += sum(
                        observed_value == value for observed_value in observed_values
                    )
                if cursor in {0, b"0", "0"}:
                    break
        finally:
            await client.aclose()
    if residual:
        raise PreflightError(
            f"Redis rehearsal cleanup residual: inserted={inserted} "
            f"cleaned={cleaned} residual={residual}"
        )
    if operation_error is not None:
        raise PreflightError(
            f"Redis rehearsal failed after cleanup: inserted={inserted} "
            f"cleaned={cleaned} residual={residual}"
        ) from operation_error
    if inserted != count:
        raise PreflightError(
            f"Redis rehearsal prefix collided: inserted={inserted} "
            f"cleaned={cleaned} residual={residual}"
        )
    elapsed = max(time.monotonic() - started, 0.000001)
    return {
        "attempted_claims": count,
        "claims_per_second": round(count / elapsed, 2),
        "memory_delta_bytes": max(
            0,
            int(memory_after.get("used_memory", 0))
            - int(memory_before.get("used_memory", 0)),
        ),
        "evicted_keys_delta": max(
            0, int(after.get("evicted_keys", 0)) - int(before.get("evicted_keys", 0))
        ),
        "inserted": inserted,
        "cleaned": cleaned,
        "residual": residual,
    }


def _redis_load_command(args: argparse.Namespace) -> int:
    now = _utc_now(allow_override=False)
    if args.ttl_seconds != WINDOW_DAYS * 86400:
        raise PreflightError("ttl-seconds must be exactly 2678400")
    environment = os.environ.get("API_PROVIDER_RUNTIME_ENV")
    if environment != args.environment:
        raise PreflightError("API_PROVIDER_RUNTIME_ENV must match --environment")
    deployment = os.environ.get("DEPLOYMENT_ID")
    if not deployment or not SAFE_ID.fullmatch(deployment):
        raise PreflightError("DEPLOYMENT_ID must be an opaque safe runtime reference")
    if args.environment == "production":
        raise PreflightError("Redis load rehearsal is forbidden in production")
    collect_binding = (
        _release_collect_binding(args.environment, deployment, now)
        if args.environment == "development"
        else None
    )
    if args.claims is None:
        volume = _message_volume_from_env(args.environment, deployment, now)
        claims = _int(volume.get("p95_unique_messages_day"), "claims") * WINDOW_DAYS
        claim_bytes = _int(volume.get("p95_claim_bytes"), "claim_bytes")
    else:
        if args.environment != "local":
            raise PreflightError(
                "manual claim counts are allowed only for local rehearsal"
            )
        claims = args.claims
        claim_bytes = args.claim_bytes
    if claims < 1 or claims > MAX_REDIS_CLAIMS:
        raise PreflightError("claims must be between 1 and 100000")
    if claim_bytes < 32 or claim_bytes > 65536:
        raise PreflightError("claim-bytes must be between 32 and 65536")
    if args.ttl_seconds < 1 or args.ttl_seconds > WINDOW_DAYS * 86400:
        raise PreflightError("ttl-seconds must be between 1 and 2678400")
    run_id = secrets.token_hex(12)
    prefix = f"api-provider-preflight:{args.environment}:{deployment}:{run_id}"
    result = asyncio.run(_redis_load(prefix, claims, args.ttl_seconds, claim_bytes))
    raw = {
        "schema_version": SCHEMA_VERSION,
        "kind": "api-provider-redis-load-raw",
        "collected_at": _iso(now),
        "representative": args.environment in {"development", "production"},
        "environment": args.environment,
        "deployment_id": deployment,
        "provenance": {
            "source": "redis",
            "environment": args.environment,
            "deployment_id": deployment,
            "observed_at": _iso(_utc_now()),
        },
        "input": {"claims": claims, "claim_bytes": claim_bytes},
        "result": result,
        **({"collect_binding": collect_binding} if collect_binding else {}),
    }
    _sign_evidence(raw)
    _write_raw(args.raw_output, raw)
    return 0 if result["evicted_keys_delta"] == 0 else 2


def _rollback_rehearsal_command(args: argparse.Namespace) -> int:
    now = _utc_now(allow_override=False)
    if args.environment != "development":
        raise PreflightError("rollback rehearsal is allowed only in development")
    if os.environ.get("API_PROVIDER_RUNTIME_ENV") != args.environment:
        raise PreflightError("rollback rehearsal environment does not match runtime")
    deployment = os.environ.get("DEPLOYMENT_ID")
    if not deployment or not SAFE_ID.fullmatch(deployment):
        raise PreflightError("DEPLOYMENT_ID must be an opaque safe runtime reference")
    checklist = _external_evidence_json(
        "API_PROVIDER_ROLLBACK_CHECKLIST_FILE",
        "a sanitized operator checklist is required",
    )
    checked_checklist = _closed_mapping(
        checklist,
        "rollback observation",
        {"kind", "environment", "deployment_id", "observed_at", "checks"},
    )
    if (
        checked_checklist.get("kind") != "api-provider-rollback-observation"
        or checked_checklist.get("environment") != args.environment
        or checked_checklist.get("deployment_id") != deployment
        or not deployment
        or not SAFE_ID.fullmatch(deployment)
    ):
        raise PreflightError("rollback observation identity is invalid")
    observed_at = _timestamp(
        checked_checklist.get("observed_at"), "rollback.observed_at"
    )
    if observed_at > now or now - observed_at > MAX_EVIDENCE_AGE:
        raise PreflightError("rollback observation is stale or in the future")
    checks = _closed_mapping(
        checked_checklist.get("checks"), "rollback.checks", ROLLBACK_CHECKS
    )
    result = {item: checks.get(item) is True for item in sorted(ROLLBACK_CHECKS)}
    collect_binding = _release_collect_binding(args.environment, deployment, now)
    raw = {
        "schema_version": SCHEMA_VERSION,
        "kind": "api-provider-rollback-rehearsal-raw",
        "collected_at": _iso(now),
        "environment": args.environment,
        "deployment_id": deployment,
        "provenance": {
            "source": "deployment-runtime",
            "environment": args.environment,
            "deployment_id": deployment,
            "observed_at": _iso(observed_at),
        },
        "checks": result,
        "pass": all(result.values()),
        "collect_binding": collect_binding,
    }
    _sign_evidence(raw)
    _write_raw(args.raw_output, raw)
    return 0 if raw["pass"] else 2


def _release_attestation(
    path_text: str, environment: str, phase: str, now: datetime
) -> Mapping[str, Any]:
    _reject_unsafe_lexical_path(path_text, "attestation path")
    path = Path(path_text).expanduser().absolute()
    root = Path(path.anchor)
    attestation = _secure_json_under(root, path.relative_to(root), expected_mode=0o644)
    _verify_evidence_signature(attestation, "attestation signature")
    _validate_attestation_shape(attestation)
    _assert_attestation_safe(attestation)
    if (
        attestation.get("schema_version") != SCHEMA_VERSION
        or attestation.get("kind") != ATTESTATION_KIND
        or attestation.get("phase") != phase
        or attestation.get("environment") != environment
        or attestation.get("decision") != "go"
    ):
        raise PreflightError(
            f"{environment} attestation identity or decision is invalid"
        )
    deployment_id = attestation.get("deployment_id")
    if not isinstance(deployment_id, str) or not SAFE_ID.fullmatch(deployment_id):
        raise PreflightError(f"{environment} deployment id is invalid")
    collected_at = _timestamp(attestation.get("collected_at"), "collected_at")
    if collected_at > now or now - collected_at > MAX_EVIDENCE_AGE:
        raise PreflightError(f"{environment} attestation is stale or in the future")
    window = _closed_mapping(
        attestation.get("window"), "window", {"days", "start", "end"}
    )
    start = _timestamp(window.get("start"), "window.start")
    end = _timestamp(window.get("end"), "window.end")
    if (
        window.get("days") != WINDOW_DAYS
        or end - start != timedelta(days=WINDOW_DAYS)
        or end != collected_at
    ):
        raise PreflightError(f"{environment} attestation window is invalid")
    aggregates = _closed_mapping(
        attestation.get("aggregates"),
        "aggregates",
        {
            "active_meta_count",
            "active_zapi_count",
            "meta_missing_app_secret_count",
            "worker_count",
            "p95_unique_messages_day",
            "p95_claim_bytes",
        },
    )
    for name, value in aggregates.items():
        parsed = _strict_int(value, f"aggregates.{name}")
        if name in {"worker_count", "p95_claim_bytes"} and parsed == 0:
            raise PreflightError(f"aggregates.{name} must be positive")
    expected_sources = _expected_sources(
        int(aggregates["meta_missing_app_secret_count"]),
        phase,
        int(aggregates["active_zapi_count"]),
    )
    if (
        set(_sequence(attestation.get("source_inventory"), "source_inventory"))
        != expected_sources
    ):
        raise PreflightError(f"{environment} source inventory is incomplete")
    if set(
        _sequence(attestation.get("sink_inventory"), "sink_inventory")
    ) != _expected_sinks(phase, int(aggregates["active_zapi_count"])):
        raise PreflightError(f"{environment} sink inventory is incomplete")
    approvals = _mapping(attestation.get("approvals"), "approvals")
    approval_valid, approval_reason = _approval_valid(
        approvals, int(aggregates["meta_missing_app_secret_count"])
    )
    if not approval_valid:
        raise PreflightError(f"{environment} approval is invalid: {approval_reason}")
    thresholds = _closed_mapping(
        attestation.get("threshold_results"),
        "threshold_results",
        {
            "representative_evidence",
            "fixed_window",
            "zapi_path_roundtrip",
            "external_edge_redaction",
            "redis_capacity",
            "redis_evictions",
            "meta_app_secret_readiness",
            "application_redaction_baseline",
        },
    )
    threshold_fields = {
        "representative_evidence": {"pass", "reason"},
        "fixed_window": {"pass", "observed_days", "required_days"},
        "zapi_path_roundtrip": (
            {"pass", "status"}
            if environment == "production"
            else {
                "pass",
                "status",
                "active_zapi_count",
                "readiness",
                "activation",
                "requires_fresh_smoke_before_activation",
            }
        ),
        "external_edge_redaction": {"pass"},
        "redis_capacity": {
            "pass",
            "projected_bytes",
            "limit_bytes",
            "formula_version",
        },
        "redis_evictions": {"pass", "observed", "required"},
        "meta_app_secret_readiness": {"pass", "affected_count", "reason"},
        "application_redaction_baseline": {
            "status",
            "pass",
            "recorded",
            "leak_categories",
        },
    }
    for name, result in thresholds.items():
        result_mapping = _closed_mapping(
            result, f"threshold_results.{name}", threshold_fields[name]
        )
        planned_production_skip = environment == "production" and name in {
            "zapi_path_roundtrip",
            "application_redaction_baseline",
        }
        if not planned_production_skip and result_mapping.get("pass") is not True:
            raise PreflightError(f"{environment} threshold is not passing: {name}")
        if planned_production_skip and (
            result_mapping.get("pass") is not False
            or result_mapping.get("status") != "not-run-pre-cutover"
        ):
            raise PreflightError(
                f"{environment} pre-cutover threshold state is invalid: {name}"
            )
    active_zapi_count = int(aggregates["active_zapi_count"])
    if environment == "development":
        smoke_threshold = _mapping(
            thresholds["zapi_path_roundtrip"], "zapi_path_roundtrip"
        )
        if smoke_threshold.get("active_zapi_count") != active_zapi_count:
            raise PreflightError(
                "development active count does not match smoke attestation"
            )
        if active_zapi_count == 0:
            smoke_matrix_valid = (
                smoke_threshold.get("pass") is True
                and smoke_threshold.get("status") == "not_applicable_no_active_channel"
                and smoke_threshold.get("readiness") is False
                and smoke_threshold.get("activation") is False
                and smoke_threshold.get("requires_fresh_smoke_before_activation")
                is True
            )
        else:
            smoke_matrix_valid = (
                smoke_threshold.get("pass") is True
                and smoke_threshold.get("status") == "passed_real_callback"
                and smoke_threshold.get("readiness") is True
                and smoke_threshold.get("activation") is True
                and smoke_threshold.get("requires_fresh_smoke_before_activation")
                is False
            )
        if not smoke_matrix_valid:
            raise PreflightError(
                "development active-count/smoke-or-N/A matrix is invalid"
            )
    if (
        thresholds["fixed_window"].get("observed_days") != WINDOW_DAYS
        or thresholds["fixed_window"].get("required_days") != WINDOW_DAYS
        or thresholds["redis_evictions"].get("observed") != 0
        or thresholds["redis_evictions"].get("required") != 0
        or thresholds["redis_capacity"].get("formula_version") != REDIS_CAPACITY_FORMULA
        or thresholds["representative_evidence"].get("reason")
        != "development-or-production-live-evidence-required"
        or thresholds["meta_app_secret_readiness"].get("affected_count")
        != aggregates["meta_missing_app_secret_count"]
        or thresholds["application_redaction_baseline"].get("status")
        != (
            "not-run-pre-cutover"
            if environment == "production"
            else (
                "not_applicable_no_active_channel" if active_zapi_count == 0 else "pass"
            )
        )
        or thresholds["application_redaction_baseline"].get("recorded") is not True
        or thresholds["application_redaction_baseline"].get("leak_categories") != []
    ):
        raise PreflightError(f"{environment} threshold details are incoherent")
    projected = _strict_int(
        thresholds["redis_capacity"].get("projected_bytes"),
        "redis_capacity.projected_bytes",
    )
    limit = _strict_int(
        thresholds["redis_capacity"].get("limit_bytes"),
        "redis_capacity.limit_bytes",
    )
    if projected > limit:
        raise PreflightError(f"{environment} Redis capacity details are incoherent")
    criteria = _sequence(
        attestation.get("design_return_criteria"), "design_return_criteria"
    )
    if criteria:
        raise PreflightError(f"{environment} attestation has return criteria")
    return attestation


def _release_decision_command(args: argparse.Namespace) -> int:
    now = _utc_now(allow_override=False)
    development = _release_attestation(args.development, "development", "release", now)
    production = _release_attestation(args.production, "production", "pre-cutover", now)
    expected_development = os.environ.get("DEVELOPMENT_DEPLOYMENT_ID")
    expected_production = os.environ.get("PRODUCTION_DEPLOYMENT_ID")
    if (
        not expected_development
        or not SAFE_ID.fullmatch(expected_development)
        or development.get("deployment_id") != expected_development
    ):
        raise PreflightError(
            "development attestation does not match DEVELOPMENT_DEPLOYMENT_ID"
        )
    if (
        not expected_production
        or not SAFE_ID.fullmatch(expected_production)
        or production.get("deployment_id") != expected_production
    ):
        raise PreflightError(
            "production attestation does not match PRODUCTION_DEPLOYMENT_ID"
        )
    if development.get("deployment_id") == production.get("deployment_id"):
        raise PreflightError("development and production deployment ids must differ")
    print("go: all supplied attestations authorize release progression")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect and verify API-provider rollout evidence without tracking raw data."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser(
        "collect", help="collect read-only operational observations"
    )
    collect.add_argument("--phase", required=True)
    collect.add_argument(
        "--environment", choices=("local", "development", "production"), required=True
    )
    collect.add_argument("--deployment-id", required=True)
    collect.add_argument("--window-days", type=int, required=True)
    collect.add_argument("--raw-output", required=True)
    collect.set_defaults(handler=_collect_command)

    verify = subparsers.add_parser(
        "verify", help="calculate gates and emit a sanitized attestation"
    )
    verify.add_argument("--phase", required=True)
    verify_input = verify.add_mutually_exclusive_group(required=True)
    verify_input.add_argument("--input")
    verify_input.add_argument("--input-dir")
    verify.add_argument("--attestation", required=True)
    verify.set_defaults(handler=_verify_command)

    scan = subparsers.add_parser(
        "scan-tracked", help="reject tracked raw/sensitive evidence"
    )
    scan.add_argument("--root", default=".")
    scan.set_defaults(handler=_scan_tracked_command)

    redis_load = subparsers.add_parser(
        "redis-load", help="run a bounded, self-cleaning Redis claim load"
    )
    redis_load.add_argument(
        "--environment", choices=("local", "development", "production"), required=True
    )
    redis_load.add_argument("--claims", type=int)
    redis_load.add_argument("--claim-bytes", type=int, default=128)
    redis_load.add_argument("--ttl-seconds", type=int, default=2678400)
    redis_load.add_argument("--raw-output", required=True)
    redis_load.set_defaults(handler=_redis_load_command)

    smoke = subparsers.add_parser(
        "webhook-smoke", help="trigger a controlled Z-API callback smoke"
    )
    smoke.add_argument("--environment", choices=("development",), required=True)
    smoke.add_argument("--collect-input", required=True)
    smoke.add_argument("--raw-output", required=True)
    smoke.set_defaults(handler=_webhook_smoke_command)

    rollback = subparsers.add_parser(
        "rollback-rehearsal", help="record fail-closed rollback rehearsal"
    )
    rollback.add_argument("--environment", choices=("development",), required=True)
    rollback.add_argument("--raw-output", required=True)
    rollback.set_defaults(handler=_rollback_rehearsal_command)

    release = subparsers.add_parser(
        "release-decision", help="make a decision from sanitized attestations"
    )
    release.add_argument("--development", required=True)
    release.add_argument("--production", required=True)
    release.set_defaults(handler=_release_decision_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (KeyError, PreflightError) as exc:
        print(f"preflight error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
