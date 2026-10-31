#!/usr/bin/env python3
"""Operator tool for the three-actor billing rollout ceremony.

Three distinct ``super_admin`` operators (deployer, approver, reviewer) each
run this tool with their own credential to move the persisted billing
rollout (``disabled -> canary_allowlist -> general``) and to roll it back or
contain an incident.  The runbook is
``docs/runbooks/asaas-rollout-ceremony.md``.

What it is not:

* It signs nothing.  The server signs every operation receipt and derives
  the actor (the JWT subject) and the role (the assignment) itself.
* It decides no authorization.  Every refusal comes from the server and is
  printed as the server's fixed code; the local checks only refuse input the
  server could never use (malformed values, a plan the state machine never
  allows) before the credential is read.

The credential (the 24 h login JWT; the 10-minute staging rehearsal token
for ``readback``) is read only from an inherited pipe or socket descriptor
(``--auth-fd``): never argv, environment, file or terminal.  It is sent only
in the ``Authorization`` header and never written to output, error text or
the journal.

``--dry-run`` prints the exact request without reading a credential, sending
or writing anything.  A mutating step records its request digest and the
server's answer in a local journal (``--journal``): repeating an applied
step prints the recorded answer and sends nothing, so every step is safe to
repeat.

Exit status: 0 done (applied, replayed, read, dry run); 2 blocked locally,
nothing sent; 3 refused by the server (HTTP 4xx), nothing applied; 4 outcome
unknown (no usable answer): repeat the same command.

Standard library only (Python 3.11+); it imports no application module.
"""

from __future__ import annotations

import argparse
import base64
import errno
import fcntl
import hashlib
import json
import os
import re
import select
import ssl
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from typing import IO, Any, Callable, Mapping, NoReturn, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from uuid import UUID, uuid4

# The production-serving API.  That service runs with ``ENVIRONMENT=staging``,
# so ``staging`` is the only control row it reads; the origin is fixed so the
# credential can never be sent to another host.
API_ORIGIN = "https://staging.api.neurostack.global"
ENVIRONMENT = "staging"
BILLING_PREFIX = "/v1/admin/billing"

PLAN_SCHEMA = "asaas_rollout_operation_plan.v1"
JOURNAL_SCHEMA = "asaas_rollout_operator_journal.v1"

MAX_JWT_BYTES = 8192
MAX_RESPONSE_BYTES = 65536
MAX_PLAN_BYTES = 65536
MAX_JOURNAL_BYTES = 4 * 1024 * 1024
FD_READ_TIMEOUT_SECONDS = 3
HTTP_TIMEOUT_SECONDS = 15

EXIT_OK = 0
EXIT_BLOCKED = 2
EXIT_REFUSED = 3
EXIT_UNKNOWN = 4

STATES = ("disabled", "canary_allowlist", "general")
ROLES = ("deployer", "approver", "reviewer")
READINESS_KINDS = ("readiness", "backlog", "provisioning", "smoke")
# The moves ``BillingRepository.transition_rollout`` can ever accept, per
# scope (``incident-disable`` only ever moves to ``disabled``).
MOVES: Mapping[str, frozenset[tuple[str, str]]] = {
    "rollout": frozenset(
        {
            ("disabled", "canary_allowlist"),
            ("canary_allowlist", "general"),
            ("canary_allowlist", "disabled"),
            ("general", "disabled"),
        }
    ),
    "incident": frozenset({("canary_allowlist", "disabled"), ("general", "disabled")}),
}

# ``src/services/billing_operations.py``: the fixed canary budget.
CANARY_CHECKOUT_LIMIT = 2
CANARY_SETTLEMENT_LIMIT = 2
CANARY_AMOUNT_LIMIT_CENTS = 109900
# Every move other than the canary opening signs this one documented value:
# the server interprets ``budget_digest`` only for ``-> canary_allowlist``.
NO_CANARY_BUDGET_DIGEST = hashlib.sha256(
    b"neurostack.billing.rollout.no_canary_budget.v1"
).hexdigest()

SUBCOMMANDS = (
    "plan",
    "whoami",
    "assign",
    "receipt",
    "readiness",
    "transition",
    "incident-disable",
    "revoke",
    "readback",
)

_HEX64 = re.compile(r"[0-9a-f]{64}")
_JWT = re.compile(rb"eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
# Anything that could be (part of) a compact JWT; no legitimate output value
# (UUIDs, hex digests, fixed codes) contains ``eyJ``.
_JWT_LIKE = re.compile(r"eyJ[A-Za-z0-9_\-.]*")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,119}")
_LOGIN_CLAIMS = frozenset({"sub", "tenant_id", "role", "email", "iat", "exp", "jti"})
_REHEARSAL_CLAIMS = _LOGIN_CLAIMS | {"token_use", "token_environment", "scope"}

# Guidance only: the server's code is always printed verbatim next to it.
# ``billing_operation_conflict`` is the server's generic conflict (a database
# constraint, a changed revision, a move the state no longer allows), so its
# hint depends on the step.
# Server text is printed ONLY when it is one of these known values; anything
# else (it could hold a fragment of a credential) is replaced by
# ``WITHHELD``.  Codes: ``src/services/billing_operations.py``,
# ``src/services/billing_rollout.py`` and ``src/api/routes/admin.py`` (the
# billing routes), ``src/api/middleware/auth.py``, ``rate_limit.py`` and
# ``ingress_body_limit.py``.
WITHHELD = "(server text withheld)"
_KNOWN_CODES = frozenset(
    {
        "billing_checkout_unavailable",
        "billing_incident_transition_invalid",
        "billing_operation_actor_conflict",
        "billing_operation_actor_mismatch",
        "billing_operation_assignment_consumed",
        "billing_operation_assignment_invalid",
        "billing_operation_conflict",
        "billing_operation_digest_invalid",
        "billing_operation_environment_invalid",
        "billing_operation_evidence_type_invalid",
        "billing_operation_expired",
        "billing_operation_expiry_invalid",
        "billing_operation_expiry_too_long",
        "billing_operation_fact_schema_unavailable",
        "billing_operation_nonce_conflict",
        "billing_operation_quorum_incomplete",
        "billing_operation_receipt_invalid",
        "billing_operation_rejected",
        "billing_operation_role_invalid",
        "billing_operation_roles_invalid",
        "billing_operation_rollout_unavailable",
        "billing_operation_scope_invalid",
        "billing_operation_signature_invalid",
        "billing_operation_signing_unavailable",
        "billing_operation_state_invalid",
        "billing_operation_watermark_invalid",
        "billing_rollout_canary_budget_unbound",
        "billing_rollout_from_state_mismatch",
        "billing_rollout_quorum_stale",
        "billing_rollout_readback_unavailable",
        "billing_rollout_readiness_digest_invalid",
        "billing_rollout_readiness_duplicate",
        "billing_rollout_readiness_evidence_invalid",
        "billing_rollout_readiness_evidence_unavailable",
        "billing_rollout_readiness_expired",
        "billing_rollout_readiness_incomplete",
        "billing_rollout_readiness_invalid",
        "billing_rollout_readiness_operation_conflict",
        "billing_rollout_readiness_receipt_invalid",
        "billing_rollout_readiness_receipt_reused",
        "billing_rollout_readiness_recorder_invalid",
        "billing_rollout_readiness_revision_invalid",
        "billing_rollout_readiness_transition_invalid",
        "billing_rollout_readiness_unbound",
        "billing_rollout_transition_invalid",
        "billing_super_admin_jwt_required",
        "forbidden",
        "invalid_request",
        "malformed_request_body",
        "rate_limited",
        "request_body_timeout",
        "request_body_too_large",
        "unauthorized",
        "unsupported_media_type",
    }
)
_KNOWN_MESSAGES = frozenset(
    {
        "Invalid token",
        "Method Not Allowed",
        "Not Found",
        "Not authenticated",
        "Request body exceeds the configured limit",
        "Request body framing is invalid or incomplete",
        "Super admin access required",
        "Tenant account is inactive",
        "Token has expired",
    }
)
# Pydantic error types the request schemas can report; any other is "invalid".
_KNOWN_VALIDATION_TYPES = frozenset(
    {
        "bool_parsing",
        "bool_type",
        "datetime_from_date_parsing",
        "datetime_parsing",
        "datetime_type",
        "dict_type",
        "enum",
        "extra_forbidden",
        "greater_than",
        "greater_than_equal",
        "int_from_float",
        "int_parsing",
        "int_type",
        "json_invalid",
        "less_than",
        "less_than_equal",
        "literal_error",
        "missing",
        "model_attributes_type",
        "model_type",
        "string_pattern_mismatch",
        "string_too_long",
        "string_too_short",
        "string_type",
        "timezone_aware",
        "uuid_parsing",
        "uuid_type",
        "uuid_version",
        "value_error",
    }
)
# A validation location is printed only as request locations and the field
# names this tool itself sends.
_KNOWN_FIELDS = frozenset(
    {
        "body",
        "header",
        "path",
        "query",
        "scope",
        "operation_id",
        "environment",
        "from_state",
        "to_state",
        "budget_digest",
        "deployment_digest",
        "expires_at",
        "role",
        "actor_user_id",
        "assignment_id",
        "nonce",
        "receipt_kind",
        "rollout_revision",
        "status",
        "content_sha256",
        "operation_receipt_id",
        "provider_receipt_id",
        "rule_digest",
        "bundle_digest",
        "app_digest",
        "observed_at",
        "expected_revision",
        "canary_builder_id",
    }
)

_STEP_HINTS: Mapping[tuple[str, str], str] = {
    ("assign", "billing_operation_conflict"): (
        "this account already holds a duty in this operation (one person, one "
        "duty: uq_billing_operation_assignments_actor), or this assignment "
        "already exists"
    ),
    ("receipt", "billing_operation_conflict"): (
        "a database rule refused this receipt (for example a concurrent "
        "receipt for the same assignment)"
    ),
    ("readiness", "billing_operation_conflict"): (
        "this kind is already recorded at this rollout revision"
    ),
    ("transition", "billing_operation_conflict"): (
        "the expected revision is not the current one, the move is not allowed "
        "from the current state, or the canary's lifetime slots belong to "
        "another builder; read back the state"
    ),
    ("incident-disable", "billing_operation_conflict"): (
        "the expected revision is not the current one or the state is already "
        "disabled; read back the state"
    ),
}
_HINTS: Mapping[str, str] = {
    "billing_operation_actor_mismatch": (
        "--actor-user-id must be the user id of the credential's own account "
        "(see whoami)"
    ),
    "billing_operation_assignment_invalid": (
        "the assignment is not this account's, is revoked, already has its "
        "receipt (each assignment signs once: after an unknown outcome the "
        "earlier receipt landed), or was made for different plan material"
    ),
    "billing_operation_nonce_conflict": (
        "this nonce is used by another receipt; use a new nonce"
    ),
    "billing_operation_receipt_invalid": (
        "a receipt of this plan is spent, expired or differs from the plan; "
        "read back the state"
    ),
    "billing_rollout_readiness_duplicate": (
        "this kind is already recorded at this revision (after an unknown "
        "outcome: the earlier attempt landed)"
    ),
    "billing_rollout_readiness_unbound": (
        "the readiness rows do not bind an unspent quorum of this plan "
        "(already spent, or stranded); read back the state"
    ),
    "billing_rollout_quorum_stale": (
        "a receipt of this quorum predates the current state; sign a fresh "
        "quorum under a new plan"
    ),
    "billing_rollout_from_state_mismatch": (
        "the plan's from_state is not the current state; read back the state"
    ),
    "billing_operation_quorum_incomplete": (
        "the server did not find exactly one unspent deployer, approver and "
        "reviewer receipt for this plan"
    ),
    "billing_rollout_readiness_incomplete": (
        "general needs all four readiness rows at the expected revision"
    ),
    "unauthorized": (
        "readback takes the 10-minute staging rehearsal token; every other "
        "step takes the 24 h login JWT"
    ),
}


class OperatorError(ValueError):
    """A local refusal; nothing was sent."""


class TransportError(Exception):
    """No usable HTTP answer: the outcome of the request is unknown."""


# --------------------------------------------------------------------------
# Output: one JSON object on stdout, human lines on stderr, both redacted.
# --------------------------------------------------------------------------


class Console:
    def __init__(self, out: IO[str], err: IO[str]) -> None:
        self._out = out
        self._err = err
        self.secrets: list[str] = []
        self.sent = False

    def clean(self, text: str) -> str:
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return _JWT_LIKE.sub("[REDACTED]", text)

    def emit(self, payload: Mapping[str, Any]) -> None:
        text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=True)
        self._out.write(self.clean(text) + "\n")
        self._out.flush()

    def say(self, line: str) -> None:
        self._err.write(self.clean(line) + "\n")
        self._err.flush()


def _redacting_error(message: str) -> str:
    return _JWT_LIKE.sub("[REDACTED]", message)


class _Parser(argparse.ArgumentParser):
    """argparse echoes bad values; never let a pasted token through."""

    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {_redacting_error(message)}\n")


# --------------------------------------------------------------------------
# Credential: one inherited pipe or socket, consumed once and closed.
# --------------------------------------------------------------------------


def _read_inherited_fd(fd: int, *, label: str, limit: int) -> bytes:
    """Consume and close one non-standard inherited anonymous pipe/socket."""

    if fd in (0, 1, 2):
        raise OperatorError(f"{label} FD must not be stdin, stdout, or stderr")
    try:
        descriptor_stat = os.fstat(fd)
    except OSError:
        raise OperatorError(f"{label} FD is unavailable") from None
    chunks: list[bytes] = []
    total = 0
    try:
        if stat.S_ISREG(descriptor_stat.st_mode) or os.isatty(fd):
            raise OperatorError(f"{label} FD must be an inherited pipe or socket")
        if not stat.S_ISFIFO(descriptor_stat.st_mode) and not stat.S_ISSOCK(
            descriptor_stat.st_mode
        ):
            raise OperatorError(f"{label} FD must be an inherited pipe or socket")
        while True:
            readable, _, _ = select.select([fd], [], [], FD_READ_TIMEOUT_SECONDS)
            if not readable:
                raise OperatorError(f"{label} FD read timed out")
            chunk = os.read(fd, min(8192, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > limit:
                raise OperatorError(f"{label} exceeds the size limit")
    except OperatorError:
        raise
    except (OSError, ValueError):  # select() refuses descriptors >= FD_SETSIZE
        raise OperatorError(f"{label} FD could not be consumed") from None
    finally:
        os.close(fd)
    return b"".join(chunks).strip()


def read_auth_fd(fd: int) -> str:
    """Return the compact JWT read from *fd*; the descriptor is closed."""

    token = _read_inherited_fd(fd, label="authentication", limit=MAX_JWT_BYTES)
    if not _JWT.fullmatch(token):
        raise OperatorError("authentication FD did not contain a compact JWT")
    return token.decode("ascii")


def _token_claims(token: str) -> dict[str, Any]:
    """The JWT payload as read, NOT verified (the server verifies)."""

    segment = token.split(".")[1]
    try:
        raw = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))
        claims = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise OperatorError("authentication token payload is unreadable") from None
    if not isinstance(claims, dict):
        raise OperatorError("authentication token payload is unreadable")
    return claims


# --------------------------------------------------------------------------
# Canonical values, byte-for-byte the server's (parity is tested).
# --------------------------------------------------------------------------


def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _uuid(value: object, label: str) -> str:
    try:
        return str(UUID(str(value)))
    except ValueError:
        raise OperatorError(f"{label} must be a UUID") from None


def _digest(value: object, label: str) -> str:
    if not isinstance(value, str) or not _HEX64.fullmatch(value):
        raise OperatorError(f"{label} must be 64 lowercase hex characters")
    return value


def _instant(value: object, label: str) -> str:
    """An aware ISO 8601 instant, normalized to UTC ``isoformat()``."""

    if not isinstance(value, str):
        raise OperatorError(f"{label} must be an ISO 8601 date-time")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise OperatorError(f"{label} must be an ISO 8601 date-time") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise OperatorError(f"{label} must carry a UTC offset")
    return parsed.astimezone(timezone.utc).isoformat()


def _revision(value: object, label: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,18}", value):
        raise OperatorError(f"{label} must be a non-negative integer")
    return int(value)


def canary_budget_digest(environment: str, canary_builder_id: str) -> str:
    """``billing_operations.canary_budget_digest``: the canary signs its builder."""

    return _sha256(
        {
            "canary_amount_limit_cents": CANARY_AMOUNT_LIMIT_CENTS,
            "canary_builder_id": _uuid(canary_builder_id, "canary builder id"),
            "canary_checkout_limit": CANARY_CHECKOUT_LIMIT,
            "canary_settlement_limit": CANARY_SETTLEMENT_LIMIT,
            "environment": environment,
        }
    )


def operation_payload_digest(material: Mapping[str, str]) -> str:
    """``billing_operations.operation_payload_digest`` of one plan material."""

    return _sha256(
        {
            "budget_digest": material["budget_digest"],
            "deployment_digest": material["deployment_digest"],
            "environment": material["environment"],
            "expires_at": _instant(material["expires_at"], "expires_at"),
            "from_state": material["from_state"],
            "operation_id": _uuid(material["operation_id"], "operation id"),
            "scope": material["scope"],
            "to_state": material["to_state"],
        }
    )


def readiness_content_digest(
    *,
    environment: str,
    receipt_kind: str,
    rollout_revision: int,
    operation_id: str,
    operation_receipt_id: str,
    provider_receipt_id: str,
    rule_digest: str,
    bundle_digest: str,
    app_digest: str,
) -> str:
    """``billing_rollout._readiness_content_digest`` (the nine documented keys)."""

    return _sha256(
        {
            "app_digest": app_digest,
            "bundle_digest": bundle_digest,
            "environment": environment,
            "operation_id": operation_id,
            "operation_receipt_id": operation_receipt_id,
            "provider_receipt_id": provider_receipt_id,
            "receipt_kind": receipt_kind,
            "rollout_revision": rollout_revision,
            "rule_digest": rule_digest,
        }
    )


# --------------------------------------------------------------------------
# Plan: the shared, non-secret material all three operators sign.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    material: Mapping[str, str]
    canary_builder_id: str | None

    @property
    def payload_digest(self) -> str:
        return operation_payload_digest(self.material)

    def as_json(self) -> dict[str, Any]:
        return {
            "schema_version": PLAN_SCHEMA,
            "material": dict(self.material),
            "canary_builder_id": self.canary_builder_id,
            "payload_digest": self.payload_digest,
        }


_MATERIAL_KEYS = frozenset(
    {
        "scope",
        "operation_id",
        "environment",
        "from_state",
        "to_state",
        "budget_digest",
        "deployment_digest",
        "expires_at",
    }
)


def _build_plan(
    *,
    scope: object,
    operation_id: object,
    environment: object,
    from_state: object,
    to_state: object,
    deployment_digest: object,
    expires_at: object,
    canary_builder_id: object,
    budget_digest: object | None = None,
) -> Plan:
    if scope not in MOVES:
        raise OperatorError("plan scope must be rollout or incident")
    if environment != ENVIRONMENT:
        raise OperatorError(
            "plan environment must be staging (the production-serving service)"
        )
    if (from_state, to_state) not in MOVES[str(scope)]:
        raise OperatorError(
            f"plan move is never accepted for scope {scope}; allowed: "
            + ", ".join(f"{a} -> {b}" for a, b in sorted(MOVES[str(scope)]))
        )
    builder: str | None = None
    if to_state == "canary_allowlist":
        if canary_builder_id is None:
            raise OperatorError("a canary plan needs --canary-builder-id")
        builder = _uuid(canary_builder_id, "canary builder id")
        expected_budget = canary_budget_digest(ENVIRONMENT, builder)
    else:
        if canary_builder_id is not None:
            raise OperatorError("only the canary opening names a canary builder")
        expected_budget = NO_CANARY_BUDGET_DIGEST
    if budget_digest is not None and budget_digest != expected_budget:
        raise OperatorError("plan budget_digest does not match its move")
    material = {
        "scope": str(scope),
        "operation_id": _uuid(operation_id, "operation id"),
        "environment": ENVIRONMENT,
        "from_state": str(from_state),
        "to_state": str(to_state),
        "budget_digest": expected_budget,
        "deployment_digest": _digest(deployment_digest, "deployment digest"),
        "expires_at": _instant(expires_at, "expires_at"),
    }
    return Plan(material=material, canary_builder_id=builder)


def _strict_json(raw: bytes, *, label: str) -> Any:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise OperatorError(f"{label} contains duplicate fields")
            result[key] = value
        return result

    def reject_number(_value: str) -> Any:
        raise OperatorError(f"{label} contains an unsupported number")

    try:
        return json.loads(
            raw,
            object_pairs_hook=object_pairs,
            parse_float=reject_number,
            parse_constant=reject_number,
        )
    except OperatorError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        raise OperatorError(f"{label} is invalid JSON") from None


def _read_private_file(path: Path, *, label: str, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        raise OperatorError(f"{label} cannot be opened (symlinks are refused)") from None
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > limit:
            raise OperatorError(f"{label} must be a regular file under the size limit")
        raw = os.read(fd, limit + 1)
    except OSError:
        raise OperatorError(f"{label} cannot be read") from None
    finally:
        os.close(fd)
    if len(raw) > limit:
        raise OperatorError(f"{label} exceeds the size limit")
    return raw


def load_plan(path: Path) -> Plan:
    data = _strict_json(
        _read_private_file(path, label="plan", limit=MAX_PLAN_BYTES), label="plan"
    )
    if not isinstance(data, dict) or set(data) != {
        "schema_version",
        "material",
        "canary_builder_id",
        "payload_digest",
    }:
        raise OperatorError("plan has missing or extra fields")
    material = data["material"]
    if data["schema_version"] != PLAN_SCHEMA or not isinstance(material, dict):
        raise OperatorError("plan schema is not " + PLAN_SCHEMA)
    if set(material) != _MATERIAL_KEYS or not all(
        isinstance(value, str) for value in material.values()
    ):
        raise OperatorError("plan material has missing, extra or non-text fields")
    plan = _build_plan(
        scope=material["scope"],
        operation_id=material["operation_id"],
        environment=material["environment"],
        from_state=material["from_state"],
        to_state=material["to_state"],
        deployment_digest=material["deployment_digest"],
        expires_at=material["expires_at"],
        canary_builder_id=data["canary_builder_id"],
        budget_digest=material["budget_digest"],
    )
    if dict(plan.material) != material or data["payload_digest"] != plan.payload_digest:
        raise OperatorError("plan was edited: its material or payload digest differs")
    return plan


# --------------------------------------------------------------------------
# HTTP: fixed origin, TLS verified, no proxy, no redirect.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpRequest:
    method: str
    path: str
    body: bytes | None
    headers: Mapping[str, str]


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes


Transport = Callable[[HttpRequest], HttpResponse]


class _RejectRedirects(HTTPRedirectHandler):
    """A redirect could carry the Authorization header to another host."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _https_send(request: HttpRequest) -> HttpResponse:
    opener = build_opener(
        ProxyHandler({}),
        HTTPSHandler(context=ssl.create_default_context()),
        _RejectRedirects(),
    )
    url_request = Request(
        f"{API_ORIGIN}{request.path}",
        data=request.body,
        headers=dict(request.headers),
        method=request.method,
    )
    try:
        with opener.open(url_request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            status = int(response.status)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        status = int(error.code)
        try:
            raw = error.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, HTTPException):
            raw = b""
        finally:
            error.close()
    except (URLError, OSError, HTTPException, ValueError):
        # Never keep transport exception text: it can hold request metadata.
        raise TransportError("no HTTP answer") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise TransportError("answer exceeds the size limit")
    return HttpResponse(status=status, body=raw)


def _headers(token: str | None, *, has_body: bool) -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Authorization": (
            f"Bearer {token}"
            if token is not None
            else "Bearer <read from --auth-fd when run without --dry-run>"
        ),
        "Cache-Control": "no-store",
    }
    if has_body:
        headers["Content-Type"] = "application/json"
    return headers


def _safe_text(value: object, pattern: re.Pattern[str]) -> str | None:
    if isinstance(value, str) and pattern.fullmatch(value) and "eyJ" not in value:
        return value
    return None


def _known(value: object, known: frozenset[str]) -> str | None:
    return value if isinstance(value, str) and value in known else None


def _location_part(part: object) -> str:
    if type(part) is int:
        return str(part)
    return _known(part, _KNOWN_FIELDS) or WITHHELD


def _refusal(raw: bytes, step: str = "") -> dict[str, Any]:
    """Pass through only known server codes and messages from an error body."""

    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return {"detail": "unrecognized_error_body"}
    result: dict[str, Any] = {}
    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        code = _known(detail, _KNOWN_CODES)
        message = None if code else _known(detail, _KNOWN_MESSAGES)
        result["detail"] = code or ("message" if message else WITHHELD)
        if message:
            result["message"] = message
    elif isinstance(detail, dict):
        result["detail"] = _known(detail.get("error"), _KNOWN_CODES) or WITHHELD
    elif isinstance(detail, list):
        result["detail"] = "request_validation_failed"
        fields = []
        for item in detail[:20]:
            if not isinstance(item, dict) or not isinstance(item.get("loc"), list):
                continue
            location = [_location_part(part) for part in item["loc"][:8]]
            kind = _known(item.get("type"), _KNOWN_VALIDATION_TYPES) or "invalid"
            fields.append(".".join(location) + f": {kind}")
        result["fields"] = fields
    elif isinstance(body, dict) and "error" in body:
        result["detail"] = _known(body.get("error"), _KNOWN_CODES) or WITHHELD
        if "message" in body:
            result["message"] = _known(body.get("message"), _KNOWN_MESSAGES) or WITHHELD
    else:
        result["detail"] = "unrecognized_error_body"
    refused_code = str(result["detail"])
    hint = _STEP_HINTS.get((step, refused_code)) or _HINTS.get(refused_code)
    if hint:
        result["hint"] = hint
    return result


# --------------------------------------------------------------------------
# Journal: a local, non-secret record that makes every step repeatable.
# --------------------------------------------------------------------------


_ENTRY_KEYS = frozenset(
    {"step", "method", "path", "body", "status", "attempts", "http_status", "response"}
)


def request_digest(method: str, path: str, body: Mapping[str, Any] | None) -> str:
    return _sha256({"method": method, "path": path, "body": body})


class Journal:
    """One JSON file per operator, written atomically under an exclusive lock."""

    def __init__(self, path: Path, console: Console) -> None:
        self.path = path
        self._console = console
        self._lock_fd: int | None = None
        self.entries: dict[str, dict[str, Any]] = {}

    def __enter__(self) -> "Journal":
        lock_path = self.path.with_name(self.path.name + ".lock")
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            self._lock_fd = os.open(lock_path, flags, 0o600)
        except OSError:
            raise OperatorError("journal lock cannot be opened") from None
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(self._lock_fd)
            self._lock_fd = None
            if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise OperatorError("journal is in use by another invocation") from None
            raise OperatorError("journal lock cannot be taken") from None
        try:
            self.entries = read_journal(self.path)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *_: object) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)
            self._lock_fd = None

    def put(self, digest: str, entry: dict[str, Any]) -> None:
        self.entries[digest] = entry
        self._write()

    def delete(self, digest: str) -> None:
        self.entries.pop(digest, None)
        self._write()

    def _write(self) -> None:
        text = json.dumps(
            {"schema_version": JOURNAL_SCHEMA, "entries": self.entries},
            indent=2,
            sort_keys=True,
            ensure_ascii=True,
        )
        if self._console.clean(text) != text:
            raise OperatorError("refusing to write credential material to the journal")
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            fd = os.open(temporary, flags, 0o600)
            try:
                os.write(fd, (text + "\n").encode("ascii"))
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise OperatorError("journal cannot be written") from None


def read_journal(path: Path) -> dict[str, dict[str, Any]]:
    """Load and fully re-verify a journal; a missing file is an empty one."""

    if not os.path.lexists(path):
        return {}
    data = _strict_json(
        _read_private_file(path, label="journal", limit=MAX_JOURNAL_BYTES),
        label="journal",
    )
    if (
        not isinstance(data, dict)
        or set(data) != {"schema_version", "entries"}
        or data["schema_version"] != JOURNAL_SCHEMA
        or not isinstance(data["entries"], dict)
    ):
        raise OperatorError("journal schema is not " + JOURNAL_SCHEMA)
    entries: dict[str, dict[str, Any]] = data["entries"]
    for digest, entry in entries.items():
        if (
            not isinstance(entry, dict)
            or set(entry) != _ENTRY_KEYS
            or entry["status"] not in ("done", "unknown")
            or type(entry["attempts"]) is not int
            or entry["attempts"] < 1
            or not isinstance(entry["method"], str)
            or not isinstance(entry["path"], str)
            or not (entry["body"] is None or isinstance(entry["body"], dict))
            or (entry["status"] == "done") != isinstance(entry["response"], dict)
            or digest != request_digest(entry["method"], entry["path"], entry["body"])
        ):
            raise OperatorError(
                "journal entry does not match its request digest; it was edited "
                "or damaged (keep it as evidence and start a new journal)"
            )
    return entries


# --------------------------------------------------------------------------
# Steps: one request each, with the answer the server must give back.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    name: str
    method: str
    path: str
    body: dict[str, Any] | None
    expected_status: int
    check: Callable[[bytes], dict[str, Any]]
    payload_digest: str | None = None
    mutating: bool = True
    # Re-checks a recorded answer (the form ``check`` returned) on replay;
    # by default the recorded answer goes through ``check`` again.
    replay_check: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    @property
    def digest(self) -> str:
        return request_digest(self.method, self.path, self.body)


def _answer(raw: bytes, keys: frozenset[str]) -> dict[str, Any]:
    data = _strict_json(raw, label="server answer")
    if not isinstance(data, dict) or set(data) != keys:
        raise OperatorError("server answer has missing or extra fields")
    return data


def _same_uuid(value: object, expected: str) -> bool:
    try:
        return str(UUID(str(value))) == expected
    except ValueError:
        return False


def _is_uuid_text(value: object) -> bool:
    return isinstance(value, str) and _same_uuid(value, value.lower())


def _same_instant(value: object, expected: str) -> bool:
    try:
        return _instant(value, "answer time") == expected
    except OperatorError:
        return False


def _require(condition: bool) -> None:
    if not condition:
        raise OperatorError("server answer does not match the request")


_CONTROL_KEYS = frozenset(
    {
        "environment",
        "state",
        "rollout_revision",
        "topup_commercial_enabled",
        "topup_revision",
    }
)


def _control(raw: bytes) -> dict[str, Any]:
    data = _answer(raw, _CONTROL_KEYS)
    _require(
        data["environment"] == ENVIRONMENT
        and data["state"] in STATES
        and type(data["rollout_revision"]) is int
        and data["rollout_revision"] >= 0
        and type(data["topup_commercial_enabled"]) is bool
        and type(data["topup_revision"]) is int
    )
    return data


def _assign_step(plan: Plan, role: str, actor: str) -> Step:
    body = {**plan.material, "role": role, "actor_user_id": actor}

    def check(raw: bytes) -> dict[str, Any]:
        data = _answer(
            raw,
            frozenset(
                {
                    "assignment_id",
                    "operation_id",
                    "role",
                    "actor_user_id",
                    "active",
                    "expires_at",
                    "payload_digest",
                }
            ),
        )
        _require(
            _is_uuid_text(data["assignment_id"])
            and _same_uuid(data["operation_id"], plan.material["operation_id"])
            and data["role"] == role
            and _same_uuid(data["actor_user_id"], actor)
            and data["active"] is True
            and _same_instant(data["expires_at"], plan.material["expires_at"])
            and data["payload_digest"] == plan.payload_digest
        )
        return data

    return Step(
        "assign",
        "POST",
        f"{BILLING_PREFIX}/operations/assignments",
        body,
        201,
        check,
        plan.payload_digest,
    )


def _receipt_step(plan: Plan, assignment_id: str, nonce: str) -> Step:
    body = {**plan.material, "assignment_id": assignment_id, "nonce": nonce}

    def check(raw: bytes) -> dict[str, Any]:
        data = _answer(
            raw,
            frozenset(
                {"receipt_id", "operation_id", "role", "actor_user_id", "nonce", "applied"}
            ),
        )
        _require(
            _is_uuid_text(data["receipt_id"])
            and _same_uuid(data["operation_id"], plan.material["operation_id"])
            and data["role"] in ROLES
            and _is_uuid_text(data["actor_user_id"])
            and _same_uuid(data["nonce"], nonce)
            and data["applied"] is False
        )
        return data

    return Step(
        "receipt",
        "POST",
        f"{BILLING_PREFIX}/operations/receipts",
        body,
        201,
        check,
        plan.payload_digest,
    )


def _readiness_step(plan: Plan, args: argparse.Namespace) -> Step:
    material = plan.material
    if (
        material["scope"],
        material["from_state"],
        material["to_state"],
    ) != ("rollout", "canary_allowlist", "general"):
        raise OperatorError(
            "readiness belongs to the rollout canary_allowlist -> general plan"
        )
    revision = _revision(args.rollout_revision, "--rollout-revision")
    deployer = _uuid(args.deployer_receipt_id, "--deployer-receipt-id")
    reviewer = _uuid(args.reviewer_receipt_id, "--reviewer-receipt-id")
    rule = _digest(args.rule_digest, "--rule-digest")
    bundle = _digest(args.bundle_digest, "--bundle-digest")
    observed = _instant(args.observed_at, "--observed-at")
    content = readiness_content_digest(
        environment=material["environment"],
        receipt_kind=args.kind,
        rollout_revision=revision,
        operation_id=material["operation_id"],
        operation_receipt_id=deployer,
        provider_receipt_id=reviewer,
        rule_digest=rule,
        bundle_digest=bundle,
        app_digest=material["deployment_digest"],
    )
    body: dict[str, Any] = {
        **material,
        "receipt_kind": args.kind,
        "rollout_revision": revision,
        "status": "passed",
        "content_sha256": content,
        "operation_receipt_id": deployer,
        "provider_receipt_id": reviewer,
        "rule_digest": rule,
        "bundle_digest": bundle,
        "app_digest": material["deployment_digest"],
        "observed_at": observed,
    }

    recorded = (
        "receipt_id",
        "receipt_kind",
        "rollout_revision",
        "content_sha256",
        "operation_receipt_id",
        "provider_receipt_id",
    )

    def verify(data: Mapping[str, Any]) -> dict[str, Any]:
        _require(
            _is_uuid_text(data["receipt_id"])
            and data["receipt_kind"] == args.kind
            and type(data["rollout_revision"]) is int
            and data["rollout_revision"] == revision
            and data["content_sha256"] == content
            and _same_uuid(data["operation_receipt_id"], deployer)
            and _same_uuid(data["provider_receipt_id"], reviewer)
        )
        return {key: data[key] for key in recorded}

    def check(raw: bytes) -> dict[str, Any]:
        data = _answer(raw, frozenset(body) | {"receipt_id"})
        _require(_same_uuid(data["operation_id"], material["operation_id"]))
        return verify(data)

    def replay_check(data: dict[str, Any]) -> dict[str, Any]:
        _require(set(data) == set(recorded))
        return verify(data)

    return Step(
        "readiness",
        "POST",
        f"{BILLING_PREFIX}/rollout/receipts",
        body,
        201,
        check,
        plan.payload_digest,
        replay_check=replay_check,
    )


def _transition_step(plan: Plan, expected: int, *, incident: bool) -> Step:
    scope = "incident" if incident else "rollout"
    if plan.material["scope"] != scope:
        raise OperatorError(
            f"{'incident-disable' if incident else 'transition'} needs a "
            f"{scope}-scope plan"
        )
    body: dict[str, Any] = {**plan.material, "expected_revision": expected}
    if plan.canary_builder_id is not None:
        body["canary_builder_id"] = plan.canary_builder_id

    def check(raw: bytes) -> dict[str, Any]:
        data = _control(raw)
        _require(
            data["state"] == plan.material["to_state"]
            and data["rollout_revision"] == expected + 1
        )
        return data

    path = "incident-disable" if incident else "transitions"
    return Step(
        "incident-disable" if incident else "transition",
        "POST",
        f"{BILLING_PREFIX}/rollout/{path}",
        body,
        200,
        check,
        plan.payload_digest,
    )


def _revoke_step(assignment_id: str) -> Step:
    def check(raw: bytes) -> dict[str, Any]:
        return {}

    return Step(
        "revoke",
        "DELETE",
        f"{BILLING_PREFIX}/operations/assignments/{assignment_id}",
        None,
        204,
        check,
    )


def _readback_step() -> Step:
    return Step(
        "readback",
        "GET",
        f"{BILLING_PREFIX}/rollout/readback",
        None,
        200,
        _control,
        mutating=False,
    )


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


def _summary(step: Step) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "step": step.name,
        "method": step.method,
        "path": step.path,
        "request_digest": step.digest,
    }
    if step.payload_digest is not None:
        summary["payload_digest"] = step.payload_digest
    return summary


def _verify_recorded(step: Step, entry: Mapping[str, Any]) -> None:
    """A recorded answer must pass the same check a live answer gets."""

    if entry["status"] != "done":
        return
    response = entry["response"]
    try:
        if step.replay_check is not None:
            checked = step.replay_check(response)
        else:
            checked = step.check(_canonical(response))
        valid = entry["http_status"] == step.expected_status and checked == response
    except (OperatorError, KeyError, TypeError):
        valid = False
    if not valid:
        raise OperatorError(
            "journal entry's recorded answer does not match the request; it was "
            "edited or damaged (keep it as evidence and start a new journal)"
        )


def _dry_run(step: Step, journal: Path | None, console: Console) -> int:
    status = "not_given"
    if journal is not None and step.mutating:
        entry = read_journal(journal).get(step.digest)
        if entry is not None:
            _verify_recorded(step, entry)
        status = "no_entry" if entry is None else str(entry["status"])
    console.emit(
        {
            **_summary(step),
            "outcome": "dry_run",
            "journal": status,
            "would_send": {
                "method": step.method,
                "url": f"{API_ORIGIN}{step.path}",
                "headers": _headers(None, has_body=step.body is not None),
                "body": step.body,
            },
        }
    )
    return EXIT_OK


def _send(
    step: Step, token: str, transport: Transport, console: Console
) -> HttpResponse:
    body = None if step.body is None else _canonical(step.body)
    console.sent = True
    return transport(
        HttpRequest(
            method=step.method,
            path=step.path,
            body=body,
            headers=_headers(token, has_body=body is not None),
        )
    )


def _unknown(step: Step, console: Console, *, reason: str, status: int | None) -> int:
    console.emit(
        {**_summary(step), "outcome": "unknown", "reason": reason, "http_status": status}
    )
    console.say(
        f"unknown: {step.name} has no confirmed outcome ({reason}); repeat the "
        "same command, or read back the state, before doing anything else"
    )
    return EXIT_UNKNOWN


def _refused(
    step: Step, response: HttpResponse, console: Console, *, doubt: bool
) -> int:
    refusal = _refusal(response.body, step.name)
    output = {
        **_summary(step),
        "outcome": "refused",
        "http_status": response.status,
        **refusal,
    }
    if doubt:
        output["earlier_attempt_unknown"] = True
    console.emit(output)
    console.say(f"refused: HTTP {response.status} {refusal['detail']}")
    if "hint" in refusal:
        console.say(f"hint: {refusal['hint']}")
    if doubt:
        console.say(
            "note: an earlier attempt of this exact request had an unknown "
            "outcome; this refusal may mean that attempt landed (see the runbook)"
        )
    return EXIT_REFUSED


def _read_only(step: Step, token: str, transport: Transport, console: Console) -> int:
    try:
        response = _send(step, token, transport, console)
    except TransportError:
        return _unknown(step, console, reason="no_http_answer", status=None)
    if 400 <= response.status < 500:
        return _refused(step, response, console, doubt=False)
    if response.status != step.expected_status:
        return _unknown(step, console, reason="unexpected_status", status=response.status)
    try:
        data = step.check(response.body)
    except OperatorError:
        return _unknown(
            step, console, reason="response_mismatch", status=response.status
        )
    console.emit(
        {**_summary(step), "outcome": "read", "http_status": 200, "response": data}
    )
    return EXIT_OK


def _mutate(
    step: Step, auth_fd: int, journal_path: Path, transport: Transport, console: Console
) -> int:
    # The journal is locked and verified (including a recorded answer) BEFORE
    # the credential is read: a locked or edited journal leaves the pipe unread.
    with Journal(journal_path, console) as journal:
        entry = journal.entries.get(step.digest)
        if entry is not None:
            _verify_recorded(step, entry)
        token = read_auth_fd(auth_fd)
        console.secrets.append(token)
        if entry is not None and entry["status"] == "done":
            console.emit(
                {
                    **_summary(step),
                    "outcome": "replayed",
                    "http_status": entry["http_status"],
                    "response": entry["response"],
                }
            )
            console.say(f"{step.name}: already applied; nothing was sent")
            return EXIT_OK
        doubt = entry is not None
        attempts = 1 + (int(entry["attempts"]) if entry is not None else 0)
        record: dict[str, Any] = {
            "step": step.name,
            "method": step.method,
            "path": step.path,
            "body": step.body,
            "status": "unknown",
            "attempts": attempts,
            "http_status": None,
            "response": None,
        }
        journal.put(step.digest, record)
        if doubt:
            console.say(
                "note: an earlier attempt of this exact request has an unknown "
                "outcome; sending it again (the server applies it at most once)"
            )
        try:
            response = _send(step, token, transport, console)
        except TransportError:
            return _unknown(step, console, reason="no_http_answer", status=None)
        if 400 <= response.status < 500:
            # Refused before commit: nothing was applied by THIS attempt.
            if doubt:
                journal.put(step.digest, record)
            else:
                journal.delete(step.digest)
            return _refused(step, response, console, doubt=doubt)
        if response.status != step.expected_status:
            journal.put(step.digest, {**record, "http_status": response.status})
            return _unknown(
                step, console, reason="unexpected_status", status=response.status
            )
        try:
            data = step.check(response.body)
        except OperatorError:
            journal.put(step.digest, {**record, "http_status": response.status})
            return _unknown(
                step, console, reason="response_mismatch", status=response.status
            )
        output = {
            **_summary(step),
            "outcome": "applied",
            "http_status": response.status,
            "response": data,
        }
        try:
            journal.put(
                step.digest,
                {**record, "status": "done", "http_status": response.status, "response": data},
            )
        except OperatorError:
            console.emit({**output, "journal": "not_updated"})
            console.say(
                "applied, but the journal could not be updated: keep the output "
                "above as the record of this step"
            )
            return EXIT_UNKNOWN
        console.emit(output)
        return EXIT_OK


def _step_from_args(args: argparse.Namespace) -> Step:
    command = args.command
    if command == "readback":
        return _readback_step()
    if command == "revoke":
        return _revoke_step(_uuid(args.assignment_id, "--assignment-id"))
    plan = load_plan(args.plan)
    if command == "assign":
        return _assign_step(plan, args.role, _uuid(args.actor_user_id, "--actor-user-id"))
    if command == "receipt":
        return _receipt_step(
            plan,
            _uuid(args.assignment_id, "--assignment-id"),
            _uuid(args.nonce, "--nonce"),
        )
    if command == "readiness":
        return _readiness_step(plan, args)
    expected = _revision(args.expected_revision, "--expected-revision")
    return _transition_step(plan, expected, incident=command == "incident-disable")


def _plan_command(args: argparse.Namespace, console: Console) -> int:
    plan = _build_plan(
        scope=args.scope,
        operation_id=args.operation_id or str(uuid4()),
        environment=ENVIRONMENT,
        from_state=args.from_state,
        to_state=args.to_state,
        deployment_digest=args.deployment_digest,
        expires_at=args.expires_at,
        canary_builder_id=args.canary_builder_id,
    )
    console.emit(plan.as_json())
    console.say(
        f"payload_digest {plan.payload_digest}: all three operators must see "
        "this same value before anyone signs"
    )
    return EXIT_OK


def _whoami(args: argparse.Namespace, console: Console) -> int:
    token = read_auth_fd(args.auth_fd)
    console.secrets.append(token)
    claims = _token_claims(token)
    keys = set(claims) - {"builder_id"}
    if keys == _LOGIN_CLAIMS:
        kind = "login_jwt"
    elif keys == _REHEARSAL_CLAIMS and claims.get("token_use") == "asaas_staging_rehearsal":
        kind = "staging_rehearsal_token"
    else:
        kind = "unrecognized"
    expires = claims.get("exp")
    expires_at: str | None = None
    if type(expires) is int:
        try:
            expires_at = datetime.fromtimestamp(expires, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            expires = None
    console.emit(
        {
            "step": "whoami",
            "verified": False,
            "sub": _safe_text(claims.get("sub"), re.compile(r"[0-9a-fA-F-]{36}")),
            "role": _safe_text(claims.get("role"), _CODE),
            "token_kind": kind,
            "expires_at": expires_at,
            "expired": (
                None
                if type(expires) is not int
                else expires <= int(datetime.now(timezone.utc).timestamp())
            ),
        }
    )
    return EXIT_OK


def _dispatch(args: argparse.Namespace, console: Console, transport: Transport) -> int:
    if args.command == "plan":
        return _plan_command(args, console)
    if args.command == "whoami":
        return _whoami(args, console)
    step = _step_from_args(args)  # every local check, before the credential
    journal: Path | None = getattr(args, "journal", None)
    if args.dry_run:
        return _dry_run(step, journal, console)
    if not step.mutating:
        token = read_auth_fd(args.auth_fd)
        console.secrets.append(token)
        return _read_only(step, token, transport, console)
    if journal is None:
        raise OperatorError("--journal is required to send a mutating request")
    return _mutate(step, args.auth_fd, journal, transport, console)


def _fd_number(value: str) -> int:
    if not re.fullmatch(r"[0-9]{1,4}", value):
        raise argparse.ArgumentTypeError("must be a descriptor number")
    return int(value)


def _journal_path(value: str) -> Path:
    path = Path(value)
    if not value.strip() or path.name in ("", ".", ".."):
        raise argparse.ArgumentTypeError("must name a journal file")
    return path


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="asaas-rollout-operator.py",
        description="Three-actor billing rollout ceremony (see "
        "docs/runbooks/asaas-rollout-ceremony.md).",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)

    def sub(name: str, text: str) -> argparse.ArgumentParser:
        return commands.add_parser(name, help=text, description=text, allow_abbrev=False)

    def credential(command: argparse.ArgumentParser, *, journal: bool) -> None:
        group = command.add_mutually_exclusive_group(required=True)
        group.add_argument(
            "--auth-fd",
            type=_fd_number,
            help="inherited pipe/socket descriptor carrying your own JWT",
        )
        group.add_argument(
            "--dry-run",
            action="store_true",
            help="print the exact request; read no credential, send nothing",
        )
        if journal:
            command.add_argument(
                "--journal",
                type=_journal_path,
                help="this operator's journal file (required to send)",
            )

    def plan_arg(command: argparse.ArgumentParser) -> None:
        command.add_argument("--plan", type=Path, required=True, help="plan file")

    plan = sub("plan", "write the shared operation plan (JSON) to stdout")
    plan.add_argument("--scope", choices=tuple(MOVES), required=True)
    plan.add_argument("--from-state", choices=STATES, required=True)
    plan.add_argument("--to-state", choices=STATES, required=True)
    plan.add_argument("--deployment-digest", required=True)
    plan.add_argument("--expires-at", required=True)
    plan.add_argument("--canary-builder-id")
    plan.add_argument("--operation-id")

    whoami = sub("whoami", "show the identity claims of your credential")
    whoami.add_argument("--auth-fd", type=_fd_number, required=True)

    assign = sub("assign", "assign yourself one duty in the plan's operation")
    plan_arg(assign)
    assign.add_argument("--role", choices=ROLES, required=True)
    assign.add_argument("--actor-user-id", required=True)
    credential(assign, journal=True)

    receipt = sub("receipt", "ask the server to sign your receipt for the plan")
    plan_arg(receipt)
    receipt.add_argument("--assignment-id", required=True)
    receipt.add_argument("--nonce", required=True)
    credential(receipt, journal=True)

    readiness = sub("readiness", "record one readiness row for the general move")
    plan_arg(readiness)
    readiness.add_argument("--kind", choices=READINESS_KINDS, required=True)
    readiness.add_argument("--rollout-revision", required=True)
    readiness.add_argument("--deployer-receipt-id", required=True)
    readiness.add_argument("--reviewer-receipt-id", required=True)
    readiness.add_argument("--rule-digest", required=True)
    readiness.add_argument("--bundle-digest", required=True)
    readiness.add_argument("--observed-at", required=True)
    credential(readiness, journal=True)

    transition = sub("transition", "apply the plan's rollout move")
    plan_arg(transition)
    transition.add_argument("--expected-revision", required=True)
    credential(transition, journal=True)

    incident = sub("incident-disable", "apply the plan's incident containment")
    plan_arg(incident)
    incident.add_argument("--expected-revision", required=True)
    credential(incident, journal=True)

    revoke = sub("revoke", "revoke an unused assignment")
    revoke.add_argument("--assignment-id", required=True)
    credential(revoke, journal=True)

    readback = sub("readback", "read the persisted rollout state")
    credential(readback, journal=False)
    return parser


def main(argv: Sequence[str] | None = None, *, transport: Transport | None = None) -> int:
    console = Console(sys.stdout, sys.stderr)
    args = _parser().parse_args(argv)
    try:
        return _dispatch(args, console, transport or _https_send)
    except OperatorError as error:
        if console.sent:
            console.say(f"unknown: {error}; the request was sent")
            return EXIT_UNKNOWN
        console.say(f"blocked: {error}")
        return EXIT_BLOCKED
    except Exception as error:  # never print exception text: it may hold data
        name = type(error).__name__
        if console.sent:
            console.say(
                f"unknown: unexpected {name} after the request was sent; repeat "
                "the same command or read back the state"
            )
            return EXIT_UNKNOWN
        console.say(f"blocked: unexpected {name}; nothing was sent")
        return EXIT_BLOCKED


if __name__ == "__main__":
    sys.exit(main())
