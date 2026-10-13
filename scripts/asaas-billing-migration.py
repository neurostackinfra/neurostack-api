#!/usr/bin/env python3
"""Thin localhost client for signed billing-migration administration.

This tool never contacts ASAAS and never prints a JWT, secret, provider
payload, or screenshot.  It is deliberately only a client of the local API;
production provisioning, provider actions, and deployment remain operator
runbook work outside this repository.
"""

from __future__ import annotations

import argparse
import json
import re
import stat
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from src.services.billing_export import (
    BillingExportRejected,
    create_migration_export,
    discard_expired_export,
    encode_migration_export,
    validate_migration_export,
)

_OPERATION_ENDPOINTS = frozenset(
    {
        "/v1/admin/billing/operations/assignments",
        "/v1/admin/billing/operations/assignments/{assignment_id}",
        "/v1/admin/billing/operations/receipts",
        "/v1/admin/billing/rollout/receipts",
        "/v1/admin/billing/migrations/roster",
        "/v1/admin/billing/migrations/cancel-begin",
        "/v1/admin/billing/migrations/cancel-reconcile",
        "/v1/admin/billing/migrations/recreate-pending",
        "/v1/admin/billing/migrations/recreate-reopen",
        "/v1/admin/billing/migrations/recreate-reserve",
        "/v1/admin/billing/migrations/recover-checkout",
        "/v1/admin/billing/migrations/manual-intervention",
        "/v1/admin/billing/migrations/absence-reads",
        "/v1/admin/billing/migrations/provider-flip",
        "/v1/admin/billing/rollout/transitions",
        "/v1/admin/billing/rollout/incident-disable",
        "/v1/admin/billing/topup-capability",
        "/v1/admin/billing/topup-capability/enable",
        "/v1/admin/billing/topup-capability/disable",
    }
)
_OPERATION_METHODS = {
    endpoint: "POST"
    for endpoint in _OPERATION_ENDPOINTS
    if endpoint != "/v1/admin/billing/operations/assignments/{assignment_id}"
}
_OPERATION_METHODS["/v1/admin/billing/operations/assignments/{assignment_id}"] = (
    "DELETE"
)
_ASSIGNMENT_ENDPOINT = re.compile(
    r"^/v1/admin/billing/operations/assignments/([0-9a-fA-F-]{36})$"
)
_FORBIDDEN_OPERATION_KEYS = frozenset(
    {
        "address",
        "api_key",
        "authorization",
        "customer",
        "document",
        "email",
        "password",
        "phone",
        "screenshot",
        "secret",
        "token",
    }
)


class MigrationClientRejected(RuntimeError):
    """A fixed error that cannot disclose a token or filesystem location."""

    code = "billing_migration_client_rejected"

    def __init__(self, code: str | None = None) -> None:
        self.code = code or self.code
        super().__init__(self.code)


def _is_loopback_api(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme in {"http", "https"}
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and (port is None or 1 <= port <= 65535)
    )


def _read_jwt(path: Path) -> str:
    try:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size <= 0
            or metadata.st_size > 8_192
        ):
            raise MigrationClientRejected("billing_migration_jwt_file_invalid")
        token = path.read_text(encoding="ascii").strip()
    except OSError as exc:
        raise MigrationClientRejected("billing_migration_jwt_file_invalid") from exc
    if token.count(".") != 2 or any(character.isspace() for character in token):
        raise MigrationClientRejected("billing_migration_jwt_invalid")
    return token


def _write_export(path: Path, data: bytes) -> tuple[str, int]:
    """Create one private new export outside the repository.

    Delegates to ``create_migration_export``: descriptor-relative creation in
    a ``0700`` directory owned by the effective UID, reached without symbolic
    links, outside this checkout and its main repository; ``O_EXCL`` never
    overwrites and a short write/``fsync`` failure leaves no partial file.
    """

    return create_migration_export(path, data)


def _contains_forbidden_key(value: object) -> bool:
    if isinstance(value, dict):
        return any(
            not isinstance(key, str)
            or key.lower() in _FORBIDDEN_OPERATION_KEYS
            or _contains_forbidden_key(nested)
            for key, nested in value.items()
        )
    if isinstance(value, list):
        return any(_contains_forbidden_key(item) for item in value)
    return False


def _read_operation_payload(path: Path) -> dict[str, object]:
    """Read a private signed request without printing it or its path."""

    try:
        metadata = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size <= 0
            or metadata.st_size > 100_000
        ):
            raise MigrationClientRejected("billing_migration_request_file_invalid")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MigrationClientRejected("billing_migration_request_file_invalid") from exc
    if not isinstance(value, dict) or _contains_forbidden_key(value):
        raise MigrationClientRejected("billing_migration_request_payload_invalid")
    return value


def _get_json(api_url: str, token: str, path: str) -> object:
    if not path.startswith("/v1/admin/billing/"):
        raise MigrationClientRejected("billing_migration_endpoint_invalid")
    try:
        with httpx.Client(
            timeout=10.0, trust_env=False, follow_redirects=False
        ) as client:
            response = client.get(
                api_url.rstrip("/") + path,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as exc:
        raise MigrationClientRejected("billing_migration_api_unavailable") from exc
    if response.status_code != 200:
        raise MigrationClientRejected("billing_migration_api_rejected")
    try:
        return response.json()
    except ValueError as exc:
        raise MigrationClientRejected("billing_migration_api_invalid") from exc


def _operation_endpoint(
    path: str, payload: dict[str, object]
) -> tuple[str, str, dict[str, object] | None]:
    """Resolve one allowlisted endpoint to its explicit HTTP method."""

    method = _OPERATION_METHODS.get(path)
    if method == "DELETE":
        assignment_id = payload.get("assignment_id")
        if path.endswith("{assignment_id}"):
            if not isinstance(assignment_id, str) or not _ASSIGNMENT_ENDPOINT.fullmatch(
                path.replace("{assignment_id}", assignment_id)
            ):
                raise MigrationClientRejected("billing_migration_endpoint_invalid")
            path = path.replace("{assignment_id}", assignment_id)
        return path, method, None
    if method == "POST":
        return path, method, payload
    match = _ASSIGNMENT_ENDPOINT.fullmatch(path)
    if match:
        return path, "DELETE", None
    raise MigrationClientRejected("billing_migration_endpoint_invalid")


def _post_json(
    api_url: str, token: str, path: str, payload: dict[str, object]
) -> object:
    resolved_path, method, body = _operation_endpoint(path, payload)
    try:
        with httpx.Client(
            timeout=10.0, trust_env=False, follow_redirects=False
        ) as client:
            url = api_url.rstrip("/") + resolved_path
            headers = {"Authorization": f"Bearer {token}"}
            if method == "DELETE":
                response = client.delete(url, headers=headers)
                expected_statuses = {204}
            else:
                response = client.post(url, headers=headers, json=body)
                expected_statuses = {200, 201}
    except httpx.HTTPError as exc:
        raise MigrationClientRejected("billing_migration_api_unavailable") from exc
    if response.status_code not in expected_statuses:
        raise MigrationClientRejected("billing_migration_api_rejected")
    if response.status_code == 204:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise MigrationClientRejected("billing_migration_api_invalid") from exc


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--jwt-file", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export-roster")
    export.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify-export")
    verify.add_argument("--input", type=Path, required=True)
    verify.add_argument("--digest", required=True)
    verify.add_argument("--count", type=int, required=True)
    discard = commands.add_parser("discard-expired")
    discard.add_argument("--input", type=Path, required=True)
    operation = commands.add_parser("call")
    operation.add_argument("--endpoint", required=True)
    operation.add_argument("--input", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv or sys.argv[1:])
    try:
        if not _is_loopback_api(args.api_url):
            raise MigrationClientRejected("billing_migration_api_url_invalid")
        token = _read_jwt(args.jwt_file)
        if args.command == "export-roster":
            payload = _get_json(args.api_url, token, "/v1/admin/billing/migrations")
            if not isinstance(payload, list):
                raise MigrationClientRejected("billing_migration_api_invalid")
            rows = [item for item in payload if isinstance(item, dict)]
            if len(rows) != len(payload):
                raise MigrationClientRejected("billing_migration_api_invalid")
            encoded = encode_migration_export(rows)
            digest, count = _write_export(args.output, encoded)
            print(json.dumps({"sha256": digest, "count": count}, sort_keys=True))
            return 0
        if args.command == "verify-export":
            verified = validate_migration_export(
                args.input, expected_digest=args.digest, expected_count=args.count
            )
            print(
                json.dumps(
                    {"sha256": verified.digest, "count": verified.row_count},
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "discard-expired":
            print(json.dumps({"discarded": discard_expired_export(args.input)}))
            return 0
        if args.command == "call":
            payload = _read_operation_payload(args.input)
            response = _post_json(args.api_url, token, args.endpoint, payload)
            print(json.dumps(response, sort_keys=True))
            return 0
        raise MigrationClientRejected("billing_migration_command_invalid")
    except (MigrationClientRejected, BillingExportRejected) as exc:
        print(
            getattr(exc, "code", "billing_migration_client_rejected"), file=sys.stderr
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
