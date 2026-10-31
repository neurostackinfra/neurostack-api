"""Boundary tests for the three-actor billing rollout operator CLI.

The CLI is driven through ``main(argv, transport=...)``: a scripted transport
for the client-side guarantees, the real admin router behind the real
``AuthMiddleware`` for the refusals the server owns, and (when
``POSTGRES_SMOKE_DATABASE_URL`` is set) the whole ceremony against a migrated
disposable PostgreSQL.  Nothing here opens a network connection.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Callable, Iterator
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError
from starlette.testclient import TestClient

from src.api.middleware.auth import AuthMiddleware
from src.api.routes import admin as admin_route
from src.api.schemas.admin import (
    BillingOperationAssignmentRequest,
    BillingOperationReceiptRequest,
    BillingRolloutReadinessReceiptRequest,
    BillingRolloutTransitionRequest,
)
from src.config.settings import settings
from src.repositories import get_db_session_dependency
from src.services.billing_operations import (
    canary_budget_digest as server_canary_budget_digest,
)
from src.services.billing_rollout import _readiness_content_digest

SCRIPT = Path(__file__).parents[2] / "scripts" / "asaas-rollout-operator.py"
SPEC = importlib.util.spec_from_file_location("asaas_rollout_operator", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
cli = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cli  # dataclasses resolve their module by name
SPEC.loader.exec_module(cli)

DEPLOYMENT_DIGEST = "d" * 64
RULE_DIGEST = "e" * 64
BUNDLE_DIGEST = "f" * 64
OPERATION_ID = "0b6f0c4e-8f5c-4d47-9a51-1f0e2a3b4c5d"
BUILDER_ID = "6a1d2c3b-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
ACTOR_A = "11111111-1111-4111-8111-111111111111"
ACTOR_B = "22222222-2222-4222-8222-222222222222"
ASSIGNMENT_ID = "33333333-3333-4333-8333-333333333333"
RECEIPT_ID = "44444444-4444-4444-8444-444444444444"
NONCE = "55555555-5555-4555-8555-555555555555"
OTHER_NONCE = "66666666-6666-4666-8666-666666666666"
DEPLOYER_RECEIPT = "77777777-7777-4777-8777-777777777777"
REVIEWER_RECEIPT = "88888888-8888-4888-8888-888888888888"
EXPIRES_AT = "2026-11-01T12:00:00+00:00"
OBSERVED_AT = "2026-11-01T10:00:00+00:00"
# Signs unit-test JWTs only; the CLI never verifies a signature.
UNIT_KEY = "unit-test-only-signing-key-0123456789abcdef"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _token(sub: str = ACTOR_A, *, secret: str = UNIT_KEY, **extra: Any) -> str:
    now = int(time.time())
    claims = {
        "sub": sub,
        "tenant_id": str(uuid4()),
        "role": "super_admin",
        "email": "operator@example.invalid",
        "iat": now,
        "exp": now + 86400,
        "jti": str(uuid4()),
        **extra,
    }
    return jwt.encode(claims, secret, algorithm="HS256")


def _rehearsal_token(sub: str, *, secret: str) -> str:
    now = int(time.time())
    claims = {
        "sub": sub,
        "tenant_id": str(uuid4()),
        "role": "super_admin",
        "email": "operator@example.invalid",
        "iat": now,
        "exp": now + 600,
        "jti": str(uuid4()),
        "token_use": "asaas_staging_rehearsal",
        "token_environment": "staging",
        "scope": "billing.producer.staging.rehearsal",
    }
    return jwt.encode(claims, secret, algorithm="HS256")


TOKEN = _token()
# JWT-shaped but short enough to fit every message/field length bound, so
# only the explicit ``eyJ`` guards can stop it.
SHORT_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2ln"


def _pipe(data: bytes) -> int:
    read_fd, write_fd = os.pipe()
    os.write(write_fd, data)
    os.close(write_fd)
    return read_fd


def _is_open(fd: int) -> bool:
    try:
        os.fstat(fd)
    except OSError:
        return False
    return True


def _response(status: int, body: Any = None) -> Any:
    raw = b"" if body is None else json.dumps(body).encode()
    return cli.HttpResponse(status=status, body=raw)


@dataclass
class Scripted:
    """A transport that answers from a script and records every request."""

    answers: list[Any] = field(default_factory=list)
    requests: list[Any] = field(default_factory=list)

    def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        if not self.answers:
            pytest.fail("the CLI sent a request this test did not expect")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


@dataclass
class Result:
    code: int
    out: str
    err: str

    @property
    def json(self) -> Any:
        return json.loads(self.out)


def _run(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
    transport: Any | None = None,
) -> Result:
    code = cli.main(argv, transport=transport or Scripted())
    captured = capsys.readouterr()
    return Result(code=code, out=captured.out, err=captured.err)


def _plan_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], **overrides: str
) -> Path:
    arguments = {
        "--scope": "rollout",
        "--from-state": "disabled",
        "--to-state": "canary_allowlist",
        "--deployment-digest": DEPLOYMENT_DIGEST,
        "--expires-at": EXPIRES_AT,
        "--operation-id": OPERATION_ID,
        "--canary-builder-id": BUILDER_ID,
    }
    arguments.update(overrides)
    argv = ["plan"]
    for flag, value in arguments.items():
        if value:
            argv += [flag, value]
    result = _run(argv, capsys)
    assert result.code == 0, result.err
    path = tmp_path / f"plan-{uuid4().hex}.json"
    path.write_text(result.out, encoding="utf-8")
    return path


def _general_plan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> Path:
    return _plan_file(
        tmp_path,
        capsys,
        **{
            "--from-state": "canary_allowlist",
            "--to-state": "general",
            "--canary-builder-id": "",
        },
    )


def _assignment_body(role: str = "deployer", actor: str = ACTOR_A) -> dict[str, Any]:
    return {
        "assignment_id": ASSIGNMENT_ID,
        "operation_id": OPERATION_ID,
        "role": role,
        "actor_user_id": actor,
        "active": True,
        "expires_at": "2026-11-01T12:00:00Z",
        "payload_digest": None,  # filled per plan
    }


def _assign_answer(plan: Path, **overrides: Any) -> Any:
    plan_data = json.loads(plan.read_text())
    body = _assignment_body()
    body["payload_digest"] = plan_data["payload_digest"]
    body.update(overrides)
    return _response(201, body)


def _assign_argv(
    plan: Path, journal: Path | None, fd: int | None, **extra: str
) -> list[str]:
    argv = [
        "assign",
        "--plan",
        str(plan),
        "--role",
        extra.get("role", "deployer"),
        "--actor-user-id",
        extra.get("actor", ACTOR_A),
    ]
    if journal is not None:
        argv += ["--journal", str(journal)]
    argv += ["--dry-run"] if fd is None else ["--auth-fd", str(fd)]
    return argv


def _transition_answer(state: str, revision: int) -> Any:
    return _response(
        200,
        {
            "environment": "staging",
            "state": state,
            "rollout_revision": revision,
            "topup_commercial_enabled": False,
            "topup_revision": 0,
        },
    )


def _assert_no_secret(
    result: Result, *secrets: str, files: tuple[Path, ...] = ()
) -> None:
    texts = [result.out, result.err] + [
        path.read_text(encoding="utf-8", errors="replace")
        for path in files
        if path.exists()
    ]
    for secret in secrets:
        for text in texts:
            assert secret not in text
    for text in texts:
        assert "eyJ" not in text


# --------------------------------------------------------------------------
# A. Inherited descriptor guards
# --------------------------------------------------------------------------


def test_auth_fd_reads_an_inherited_pipe_and_closes_it() -> None:
    fd = _pipe(TOKEN.encode() + b"\n")
    assert cli.read_auth_fd(fd) == TOKEN
    assert not _is_open(fd)


def test_auth_fd_accepts_a_socket_and_closes_it() -> None:
    left, right = socket.socketpair()
    try:
        right.sendall(TOKEN.encode())
        right.shutdown(socket.SHUT_WR)
        fd = os.dup(left.fileno())
        assert cli.read_auth_fd(fd) == TOKEN
        assert not _is_open(fd)
    finally:
        left.close()
        right.close()


@pytest.mark.parametrize("fd", [0, 1, 2])
def test_auth_fd_refuses_standard_streams_and_leaves_them_open(fd: int) -> None:
    with pytest.raises(cli.OperatorError, match="stdin, stdout, or stderr"):
        cli.read_auth_fd(fd)
    assert _is_open(fd)


def test_auth_fd_refuses_a_regular_file_and_closes_it(tmp_path: Path) -> None:
    path = tmp_path / "token"
    path.write_text(TOKEN, encoding="ascii")
    fd = os.open(path, os.O_RDONLY)
    with pytest.raises(cli.OperatorError, match="pipe or socket") as error:
        cli.read_auth_fd(fd)
    assert TOKEN not in str(error.value)
    assert not _is_open(fd)


def test_auth_fd_refuses_a_terminal_and_closes_it() -> None:
    controller, terminal = os.openpty()
    try:
        assert os.isatty(terminal)
        with pytest.raises(cli.OperatorError, match="pipe or socket"):
            cli.read_auth_fd(terminal)
        assert not _is_open(terminal)
    finally:
        os.close(controller)


def test_auth_fd_refuses_a_character_device_that_is_not_a_pipe() -> None:
    fd = os.open(os.devnull, os.O_RDONLY)
    with pytest.raises(cli.OperatorError, match="pipe or socket"):
        cli.read_auth_fd(fd)
    assert not _is_open(fd)


def test_auth_fd_refuses_a_value_that_is_not_a_compact_jwt_without_echo() -> None:
    secret = b"not-a-jwt-but-still-a-secret"
    fd = _pipe(secret)
    with pytest.raises(cli.OperatorError) as error:
        cli.read_auth_fd(fd)
    assert secret.decode() not in str(error.value)
    assert not _is_open(fd)


def test_auth_fd_refuses_an_oversized_value() -> None:
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    try:
        os.write(write_fd, b"eyJ" + b"a" * (cli.MAX_JWT_BYTES + 16))
    finally:
        os.close(write_fd)
    with pytest.raises(cli.OperatorError, match="size limit"):
        cli.read_auth_fd(read_fd)
    assert not _is_open(read_fd)


def test_a_descriptor_beyond_the_select_range_is_refused_and_closed() -> None:
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    high = 1500
    if soft <= high:
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(hard, high + 1), hard))
    try:
        source = _pipe(TOKEN.encode())
        os.dup2(source, high)
        os.close(source)
        with pytest.raises(cli.OperatorError, match="could not be consumed"):
            cli.read_auth_fd(high)
        assert not _is_open(high)
    finally:
        if soft <= high:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


def test_whoami_survives_an_out_of_range_expiry(
    capsys: pytest.CaptureFixture[str],
) -> None:
    token = _token(exp=10**20)
    result = _run(["whoami", "--auth-fd", str(_pipe(token.encode()))], capsys)
    assert result.code == cli.EXIT_OK, result.err
    assert (result.json["expires_at"], result.json["expired"]) == (None, None)


def test_a_rejected_descriptor_sends_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    path = tmp_path / "token"
    path.write_text(TOKEN, encoding="ascii")
    fd = os.open(path, os.O_RDONLY)
    transport = Scripted()
    result = _run(_assign_argv(plan, tmp_path / "j.json", fd), capsys, transport)
    assert result.code == cli.EXIT_BLOCKED
    assert "pipe or socket" in result.err
    assert transport.requests == []
    assert not _is_open(fd)
    assert not (tmp_path / "j.json").exists()


def test_invalid_input_is_refused_before_the_credential_is_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    fd = _pipe(TOKEN.encode())
    result = _run(
        _assign_argv(plan, tmp_path / "j.json", fd, actor="not-a-uuid"), capsys
    )
    assert result.code == cli.EXIT_BLOCKED
    assert _is_open(fd)  # untouched: the credential was never consumed
    assert os.read(fd, 8192) == TOKEN.encode()
    os.close(fd)


def test_a_mutating_step_without_journal_is_blocked_before_the_credential(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    fd = _pipe(TOKEN.encode())
    transport = Scripted()
    result = _run(_assign_argv(plan, None, fd), capsys, transport)
    assert result.code == cli.EXIT_BLOCKED
    assert "--journal" in result.err
    assert transport.requests == []
    assert _is_open(fd)
    os.close(fd)


# --------------------------------------------------------------------------
# B. The credential never reaches argv, output, log or journal
# --------------------------------------------------------------------------


def test_the_token_travels_only_in_the_authorization_header(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    transport = Scripted([_assign_answer(plan)])
    fd = _pipe(TOKEN.encode())
    result = _run(_assign_argv(plan, journal, fd), capsys, transport)

    assert result.code == cli.EXIT_OK, result.err
    (request,) = transport.requests
    assert request.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN.encode() not in (request.body or b"")
    assert TOKEN not in request.path
    assert not _is_open(fd)
    _assert_no_secret(
        result, TOKEN, files=(journal, journal.with_name("journal.json.lock"))
    )


@pytest.mark.parametrize(
    "body_factory",
    [
        lambda secret: {"detail": secret},
        lambda secret: {"detail": {"error": secret}},
        lambda secret: {"error": secret, "message": secret},
        lambda secret: {"error": "unauthorized", "message": secret},
        lambda secret: {
            "detail": [
                {
                    "loc": ["body", secret],
                    "type": secret,
                    "msg": secret,
                    "input": secret,
                }
            ]
        },
        lambda secret: secret,
    ],
    ids=["detail", "detail-error", "error", "message", "validation", "plain"],
)
def test_a_server_that_echoes_the_token_in_an_error_is_redacted(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    body_factory: Callable[[str], Any],
) -> None:
    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    other_jwt = _token(ACTOR_B)
    for secret in (TOKEN, other_jwt, SHORT_JWT):
        body = body_factory(secret)
        raw = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        transport = Scripted([cli.HttpResponse(status=409, body=raw)])
        result = _run(
            _assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport
        )
        assert result.code == cli.EXIT_REFUSED
        _assert_no_secret(result, TOKEN, other_jwt, SHORT_JWT, files=(journal,))


def test_transport_exception_text_is_never_printed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    marker = "request-metadata-marker"
    for failure in (
        cli.TransportError(f"{marker}: connection reset sending Bearer {TOKEN}"),
        RuntimeError(f"{marker}: unexpected bug holding {TOKEN}"),
    ):
        transport = Scripted([failure])
        result = _run(
            _assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport
        )
        assert result.code == cli.EXIT_UNKNOWN
        assert marker not in result.out + result.err
        assert "Bearer" not in result.out + result.err
        _assert_no_secret(result, TOKEN, files=(journal,))


def test_the_console_redacts_the_credential_and_any_jwt_shape() -> None:
    import io

    out, err = io.StringIO(), io.StringIO()
    console = cli.Console(out, err)
    console.secrets.append("opaque-credential-value")
    console.emit({"a": "opaque-credential-value", "b": TOKEN})
    console.say(f"x {TOKEN} opaque-credential-value")
    for text in (out.getvalue(), err.getvalue()):
        assert "opaque-credential-value" not in text
        assert "eyJ" not in text
        assert "[REDACTED]" in text


@pytest.mark.parametrize(
    "body",
    [
        {"detail": TOKEN},
        {"detail": SHORT_JWT},
        {"error": "unauthorized", "message": TOKEN},
        {"error": "unauthorized", "message": SHORT_JWT},
        {"detail": "Bearer " + SHORT_JWT},
        {"detail": [{"loc": ["body", TOKEN], "type": "missing"}]},
        {"detail": [{"loc": ["body"], "type": SHORT_JWT}]},
    ],
)
def test_the_refusal_extractor_keeps_no_jwt_shaped_text(body: dict[str, Any]) -> None:
    extracted = json.dumps(cli._refusal(json.dumps(body).encode()))
    assert "eyJ" not in extracted


def test_the_journal_refuses_to_store_credential_material(tmp_path: Path) -> None:
    import io

    console = cli.Console(io.StringIO(), io.StringIO())
    console.secrets.append("opaque-credential-value")
    journal_path = tmp_path / "journal.json"
    with cli.Journal(journal_path, console) as journal:
        with pytest.raises(cli.OperatorError, match="credential"):
            journal.put("0" * 64, {"note": "opaque-credential-value"})
    assert not journal_path.exists()


def test_a_success_body_with_unexpected_fields_is_not_echoed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    transport = Scripted([_assign_answer(plan, debug=TOKEN)])
    result = _run(_assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport)
    assert result.code == cli.EXIT_UNKNOWN
    assert result.json["outcome"] == "unknown"
    _assert_no_secret(result, TOKEN, files=(journal,))


def test_argument_errors_never_echo_a_pasted_token(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    for argv in (
        [
            "assign",
            "--plan",
            str(plan),
            "--role",
            TOKEN,
            "--actor-user-id",
            ACTOR_A,
            "--dry-run",
        ],
        ["readback", "--auth-fd", TOKEN],
        ["readback", "--auth-fd", "3", TOKEN],
    ):
        with pytest.raises(SystemExit) as exit_info:
            cli.main(argv, transport=Scripted())
        captured = capsys.readouterr()
        assert exit_info.value.code == 2
        assert TOKEN not in captured.err + captured.out
        assert "eyJ" not in captured.err + captured.out


def test_whoami_prints_identity_claims_and_never_the_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fd = _pipe(TOKEN.encode())
    result = _run(["whoami", "--auth-fd", str(fd)], capsys)
    assert result.code == cli.EXIT_OK, result.err
    assert result.json["sub"] == ACTOR_A
    assert result.json["role"] == "super_admin"
    assert result.json["token_kind"] == "login_jwt"
    assert "email" not in result.json
    assert not _is_open(fd)
    _assert_no_secret(result, TOKEN)

    rehearsal = _rehearsal_token(ACTOR_A, secret=UNIT_KEY)
    result = _run(["whoami", "--auth-fd", str(_pipe(rehearsal.encode()))], capsys)
    assert result.json["token_kind"] == "staging_rehearsal_token"
    _assert_no_secret(result, rehearsal)


def test_the_tool_has_no_way_to_take_a_token_in_argv_or_environment() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "os.environ" not in source
    assert "getenv" not in source
    parser = cli._parser()
    (subparsers,) = [action for action in parser._actions if action.dest == "command"]
    flags = {
        flag
        for command in subparsers.choices.values()
        for action in command._actions
        for flag in action.option_strings
    }
    assert "--auth-fd" in flags
    for flag in flags:
        assert not any(word in flag for word in ("token", "jwt", "password", "bearer"))
    # The descriptor is the only credential input, and it is a number.
    for command in subparsers.choices.values():
        for action in command._actions:
            if "--auth-fd" in action.option_strings:
                assert action.type is cli._fd_number


# --------------------------------------------------------------------------
# C. Server refusals reach the operator unmasked (real router and middleware)
# --------------------------------------------------------------------------


class _FakeSession:
    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _FakeAuthority:
    """In-memory stand-in for the assignment table and its unique index."""

    assignments: dict[tuple[UUID, UUID], SimpleNamespace] = {}

    def __init__(self, session: Any, *, signing_key: bytes) -> None:
        del session, signing_key

    async def assign(self, *, material: Any, role: str, actor_user_id: UUID) -> Any:
        key = (material.operation_id, actor_user_id)
        if key in self.assignments:
            raise IntegrityError(
                "INSERT INTO billing_operation_assignments ...",
                {},
                Exception(
                    "duplicate key value violates unique constraint "
                    '"uq_billing_operation_assignments_actor"'
                ),
            )
        row = SimpleNamespace(
            id=uuid4(),
            operation_id=material.operation_id,
            role=role,
            actor_user_id=actor_user_id,
            active=True,
            expires_at=material.expires_at,
            payload_digest=material.payload_digest,
        )
        self.assignments[key] = row
        return row


@pytest.fixture
def real_router(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(_FakeAuthority, "assignments", {})
    monkeypatch.setattr(admin_route, "BillingOperationAuthority", _FakeAuthority)
    app = FastAPI()
    app.include_router(admin_route.router, prefix="/v1/admin")
    app.add_middleware(AuthMiddleware)

    async def session() -> AsyncIterator[_FakeSession]:
        yield _FakeSession()

    app.dependency_overrides[get_db_session_dependency] = session
    client = TestClient(app, raise_server_exceptions=False)

    def transport(request: Any) -> Any:
        response = client.request(
            request.method,
            request.path,
            content=request.body,
            headers=dict(request.headers),
        )
        return cli.HttpResponse(status=response.status_code, body=response.content)

    yield transport
    client.close()


def test_repeated_actor_refusal_reaches_the_operator_unmasked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], real_router: Any
) -> None:
    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    token = _token(ACTOR_A, secret=settings.secret_key)

    first = _run(
        _assign_argv(plan, journal, _pipe(token.encode()), role="deployer"),
        capsys,
        real_router,
    )
    assert first.code == cli.EXIT_OK, first.err
    assert first.json["response"]["role"] == "deployer"

    second = _run(
        _assign_argv(plan, journal, _pipe(token.encode()), role="approver"),
        capsys,
        real_router,
    )
    assert second.code == cli.EXIT_REFUSED
    assert second.json["outcome"] == "refused"
    assert second.json["http_status"] == 409
    assert second.json["detail"] == "billing_operation_conflict"
    assert "HTTP 409 billing_operation_conflict" in second.err
    assert "one person" in second.json["hint"]
    # The refused attempt is not recorded; the accepted one is.
    entries = json.loads(journal.read_text())["entries"]
    assert [entry["status"] for entry in entries.values()] == ["done"]
    _assert_no_secret(second, token, files=(journal,))


def test_actor_mismatch_refusal_reaches_the_operator_unmasked(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], real_router: Any
) -> None:
    plan = _plan_file(tmp_path, capsys)
    token = _token(ACTOR_A, secret=settings.secret_key)
    result = _run(
        _assign_argv(plan, tmp_path / "j.json", _pipe(token.encode()), actor=ACTOR_B),
        capsys,
        real_router,
    )
    assert result.code == cli.EXIT_REFUSED
    assert (result.json["http_status"], result.json["detail"]) == (
        403,
        "billing_operation_actor_mismatch",
    )


def test_the_server_not_the_cli_decides_the_token_kind(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], real_router: Any
) -> None:
    """Login JWT for mutations, the 10-minute rehearsal token for readback."""

    plan = _plan_file(tmp_path, capsys)
    login = _token(ACTOR_A, secret=settings.secret_key)
    rehearsal = _rehearsal_token(ACTOR_A, secret=settings.secret_key)

    refused = _run(
        ["readback", "--auth-fd", str(_pipe(login.encode()))], capsys, real_router
    )
    assert refused.code == cli.EXIT_REFUSED
    assert (refused.json["http_status"], refused.json["detail"]) == (
        401,
        "unauthorized",
    )
    assert "rehearsal" in refused.json["hint"]

    refused = _run(
        _assign_argv(plan, tmp_path / "j.json", _pipe(rehearsal.encode())),
        capsys,
        real_router,
    )
    assert refused.code == cli.EXIT_REFUSED
    assert refused.json["http_status"] == 401

    accepted = _run(
        _assign_argv(plan, tmp_path / "j.json", _pipe(login.encode())),
        capsys,
        real_router,
    )
    assert accepted.code == cli.EXIT_OK, accepted.err


def test_a_non_super_admin_jwt_is_refused_with_the_server_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], real_router: Any
) -> None:
    """The runbook's refusal table: a builder JWT gets the plain 403 text."""

    plan = _plan_file(tmp_path, capsys)
    token = _token(
        ACTOR_A, secret=settings.secret_key, role="builder", builder_id=BUILDER_ID
    )
    result = _run(
        _assign_argv(plan, tmp_path / "j.json", _pipe(token.encode())),
        capsys,
        real_router,
    )
    assert result.code == cli.EXIT_REFUSED
    assert (
        result.json["http_status"],
        result.json["detail"],
        result.json["message"],
    ) == (
        403,
        "message",
        "Super admin access required",
    )
    assert "refused: HTTP 403 message" in result.err


@pytest.mark.parametrize(
    "detail",
    [
        "billing_rollout_readiness_revision_invalid",
        "billing_rollout_quorum_stale",
        "billing_operation_nonce_conflict",
        "billing_rollout_from_state_mismatch",
        "billing_operation_conflict",
    ],
)
def test_refusal_codes_pass_through_verbatim(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], detail: str
) -> None:
    plan = _plan_file(tmp_path, capsys)
    transport = Scripted([_response(409, {"detail": detail})])
    result = _run(
        [
            "transition",
            "--plan",
            str(plan),
            "--expected-revision",
            "0",
            "--journal",
            str(tmp_path / "j.json"),
            "--auth-fd",
            str(_pipe(TOKEN.encode())),
        ],
        capsys,
        transport,
    )
    assert result.code == cli.EXIT_REFUSED
    assert result.json["detail"] == detail
    assert f"HTTP 409 {detail}" in result.err


def test_validation_errors_name_fields_without_echoing_input(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    body = {
        "detail": [
            {
                "type": "uuid_parsing",
                "loc": ["body", "actor_user_id"],
                "msg": "Input should be a valid UUID",
                "input": "x" * 40,
            },
            {
                # A location or type outside the closed sets is withheld.
                "type": "Xk9_fragment",
                "loc": ["body", "signature", 0],
                "msg": "Extra inputs are not permitted",
                "input": "x" * 40,
            },
        ]
    }
    transport = Scripted([_response(422, body)])
    result = _run(
        _assign_argv(plan, tmp_path / "j.json", _pipe(TOKEN.encode())),
        capsys,
        transport,
    )
    assert result.code == cli.EXIT_REFUSED
    assert result.json["detail"] == "request_validation_failed"
    assert result.json["fields"] == [
        "body.actor_user_id: uuid_parsing",
        f"body.{cli.WITHHELD}.0: invalid",
    ]
    assert "x" * 40 not in result.out + result.err
    assert "signature" not in result.out + result.err
    assert "Xk9_fragment" not in result.out + result.err


# A fragment of a JWT signature: base64url, no ``eyJ``, short enough to fit
# any length bound.
TOKEN_FRAGMENT = TOKEN.rsplit(".", 1)[1][:40]


@pytest.mark.parametrize(
    "body",
    [
        {"error": "unauthorized", "message": f"bad signature {TOKEN_FRAGMENT}"},
        {"error": "unauthorized", "message": TOKEN_FRAGMENT},
        {"detail": TOKEN_FRAGMENT},
        {"detail": f"bad signature {TOKEN_FRAGMENT}"},
        {"detail": TOKEN_FRAGMENT.lower().replace("-", "_")},
        {"detail": {"error": TOKEN_FRAGMENT}},
        {"error": TOKEN_FRAGMENT},
    ],
)
def test_server_text_outside_the_known_set_is_withheld(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], body: dict[str, Any]
) -> None:
    assert "eyJ" not in TOKEN_FRAGMENT
    plan = _plan_file(tmp_path, capsys)
    transport = Scripted([_response(401, body)])
    result = _run(
        _assign_argv(plan, tmp_path / "j.json", _pipe(TOKEN.encode())),
        capsys,
        transport,
    )
    assert result.code == cli.EXIT_REFUSED
    text = result.out + result.err
    assert TOKEN_FRAGMENT not in text
    assert TOKEN_FRAGMENT.lower().replace("-", "_") not in text
    assert cli.WITHHELD in text
    assert "HTTP 401" in result.err


@pytest.mark.parametrize(
    ("body", "status", "detail", "message"),
    [
        (
            {"detail": "billing_operation_conflict"},
            409,
            "billing_operation_conflict",
            None,
        ),
        (
            {"detail": "Super admin access required"},
            403,
            "message",
            "Super admin access required",
        ),
        (
            {"error": "unauthorized", "message": "Token has expired"},
            401,
            "unauthorized",
            "Token has expired",
        ),
        (
            {"detail": {"error": "billing_super_admin_jwt_required"}},
            403,
            "billing_super_admin_jwt_required",
            None,
        ),
    ],
)
def test_known_server_text_still_passes_through(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    body: dict[str, Any],
    status: int,
    detail: str,
    message: str | None,
) -> None:
    plan = _plan_file(tmp_path, capsys)
    transport = Scripted([_response(status, body)])
    result = _run(
        _assign_argv(plan, tmp_path / "j.json", _pipe(TOKEN.encode())),
        capsys,
        transport,
    )
    assert result.code == cli.EXIT_REFUSED
    assert result.json["detail"] == detail
    assert result.json.get("message") == message
    assert f"refused: HTTP {status} {detail}" in result.err
    assert cli.WITHHELD not in result.out + result.err


# --------------------------------------------------------------------------
# D. --dry-run shows what would be sent, sends nothing
# --------------------------------------------------------------------------


def _all_dry_runs(plan: Path, general: Path, journal: Path) -> list[list[str]]:
    common = ["--journal", str(journal), "--dry-run"]
    return [
        [
            "assign",
            "--plan",
            str(plan),
            "--role",
            "approver",
            "--actor-user-id",
            ACTOR_A,
        ]
        + common,
        [
            "receipt",
            "--plan",
            str(plan),
            "--assignment-id",
            ASSIGNMENT_ID,
            "--nonce",
            NONCE,
        ]
        + common,
        ["transition", "--plan", str(plan), "--expected-revision", "0"] + common,
        [
            "readiness",
            "--plan",
            str(general),
            "--kind",
            "smoke",
            "--rollout-revision",
            "1",
            "--deployer-receipt-id",
            DEPLOYER_RECEIPT,
            "--reviewer-receipt-id",
            REVIEWER_RECEIPT,
            "--rule-digest",
            RULE_DIGEST,
            "--bundle-digest",
            BUNDLE_DIGEST,
            "--observed-at",
            OBSERVED_AT,
        ]
        + common,
        ["revoke", "--assignment-id", ASSIGNMENT_ID] + common,
        ["readback", "--dry-run"],
    ]


def test_dry_run_sends_nothing_reads_no_credential_and_writes_no_journal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    general = _general_plan(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    incident = _plan_file(
        tmp_path,
        capsys,
        **{
            "--scope": "incident",
            "--from-state": "general",
            "--to-state": "disabled",
            "--canary-builder-id": "",
        },
    )
    runs = _all_dry_runs(plan, general, journal) + [
        [
            "incident-disable",
            "--plan",
            str(incident),
            "--expected-revision",
            "4",
            "--journal",
            str(journal),
            "--dry-run",
        ],
    ]
    for argv in runs:
        transport = Scripted()
        result = _run(argv, capsys, transport)
        assert result.code == cli.EXIT_OK, (argv, result.err)
        assert transport.requests == []
        output = result.json
        assert output["outcome"] == "dry_run"
        would_send = output["would_send"]
        assert would_send["url"].startswith(cli.API_ORIGIN + "/v1/admin/billing/")
        assert "auth-fd" in would_send["headers"]["Authorization"]
        assert not journal.exists()
        assert not journal.with_name("journal.json.lock").exists()


def test_dry_run_and_a_credential_are_mutually_exclusive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    fd = _pipe(TOKEN.encode())
    argv = _assign_argv(plan, tmp_path / "j.json", fd) + ["--dry-run"]
    with pytest.raises(SystemExit):
        cli.main(argv, transport=Scripted())
    capsys.readouterr()
    assert _is_open(fd)
    os.close(fd)


def test_dry_run_body_is_exactly_the_body_that_is_sent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    dry = _run(_assign_argv(plan, journal, None), capsys)
    transport = Scripted([_assign_answer(plan)])
    sent = _run(_assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport)
    assert sent.code == cli.EXIT_OK, sent.err
    (request,) = transport.requests
    would_send = dry.json["would_send"]
    assert json.loads(request.body) == would_send["body"]
    assert (request.method, cli.API_ORIGIN + request.path) == (
        would_send["method"],
        would_send["url"],
    )
    assert dry.json["request_digest"] == sent.json["request_digest"]


def test_dry_run_reports_a_recorded_step_without_sending(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    assert _run(_assign_argv(plan, journal, None), capsys).json["journal"] == "no_entry"
    transport = Scripted([_assign_answer(plan)])
    assert (
        _run(_assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport).code
        == 0
    )
    before = journal.read_bytes()
    dry = _run(_assign_argv(plan, journal, None), capsys)
    assert dry.json["journal"] == "done"
    assert journal.read_bytes() == before


# --------------------------------------------------------------------------
# E. Repeating a step is idempotent
# --------------------------------------------------------------------------


def test_repeating_an_applied_step_replays_it_without_sending(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    transport = Scripted([_assign_answer(plan)])

    first = _run(_assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport)
    fd = _pipe(TOKEN.encode())
    second = _run(_assign_argv(plan, journal, fd), capsys, transport)

    assert (first.code, second.code) == (cli.EXIT_OK, cli.EXIT_OK)
    assert len(transport.requests) == 1
    assert first.json["outcome"] == "applied"
    assert second.json["outcome"] == "replayed"
    assert second.json["response"] == first.json["response"]
    assert second.json["request_digest"] == first.json["request_digest"]
    assert not _is_open(fd)


def test_repeating_a_transition_replays_it_without_sending(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    transport = Scripted([_transition_answer("canary_allowlist", 1)])
    argv = [
        "transition",
        "--plan",
        str(plan),
        "--expected-revision",
        "0",
        "--journal",
        str(journal),
        "--auth-fd",
    ]
    first = _run(argv + [str(_pipe(TOKEN.encode()))], capsys, transport)
    second = _run(argv + [str(_pipe(TOKEN.encode()))], capsys, transport)
    assert (first.code, second.code) == (0, 0), (first.err, second.err)
    assert len(transport.requests) == 1
    assert json.loads(transport.requests[0].body)["canary_builder_id"] == BUILDER_ID
    assert second.json["outcome"] == "replayed"
    assert second.json["response"]["state"] == "canary_allowlist"


def test_an_unknown_outcome_is_resent_and_then_recorded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    transport = Scripted(
        [
            cli.TransportError("timed out"),
            _response(502, {"detail": "bad_gateway"}),
            _assign_answer(plan),
        ]
    )
    first = _run(_assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport)
    assert first.code == cli.EXIT_UNKNOWN
    assert first.json["outcome"] == "unknown"
    (entry,) = json.loads(journal.read_text())["entries"].values()
    assert (entry["status"], entry["attempts"]) == ("unknown", 1)

    second = _run(_assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport)
    assert second.code == cli.EXIT_UNKNOWN
    assert second.json["http_status"] == 502
    assert "unknown outcome" in second.err

    third = _run(_assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport)
    assert third.code == cli.EXIT_OK
    (entry,) = json.loads(journal.read_text())["entries"].values()
    assert (entry["status"], entry["attempts"]) == ("done", 3)

    fourth = _run(_assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport)
    assert fourth.json["outcome"] == "replayed"
    assert len(transport.requests) == 3


def test_a_refused_step_is_not_recorded_and_can_be_sent_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    transport = Scripted(
        [_response(409, {"detail": "billing_operation_conflict"}), _assign_answer(plan)]
    )
    first = _run(_assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport)
    assert first.code == cli.EXIT_REFUSED
    assert json.loads(journal.read_text())["entries"] == {}
    second = _run(_assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport)
    assert second.code == cli.EXIT_OK
    assert len(transport.requests) == 2


def test_a_refusal_after_an_unknown_attempt_keeps_the_doubt_and_says_so(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    argv = [
        "receipt",
        "--plan",
        str(plan),
        "--assignment-id",
        ASSIGNMENT_ID,
        "--nonce",
        NONCE,
        "--journal",
        str(journal),
        "--auth-fd",
    ]
    transport = Scripted(
        [
            cli.TransportError("reset"),
            _response(409, {"detail": "billing_operation_nonce_conflict"}),
        ]
    )
    assert _run(argv + [str(_pipe(TOKEN.encode()))], capsys, transport).code == 4
    second = _run(argv + [str(_pipe(TOKEN.encode()))], capsys, transport)
    assert second.code == cli.EXIT_REFUSED
    assert second.json["earlier_attempt_unknown"] is True
    (entry,) = json.loads(journal.read_text())["entries"].values()
    assert entry["status"] == "unknown"


def test_changed_inputs_are_a_new_request(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    receipt = {
        "receipt_id": RECEIPT_ID,
        "operation_id": OPERATION_ID,
        "role": "deployer",
        "actor_user_id": ACTOR_A,
        "applied": False,
    }
    transport = Scripted(
        [
            _response(201, {**receipt, "nonce": NONCE}),
            _response(409, {"detail": "billing_operation_conflict"}),
        ]
    )
    for nonce, code in ((NONCE, 0), (OTHER_NONCE, cli.EXIT_REFUSED)):
        result = _run(
            [
                "receipt",
                "--plan",
                str(plan),
                "--assignment-id",
                ASSIGNMENT_ID,
                "--nonce",
                nonce,
                "--journal",
                str(journal),
                "--auth-fd",
                str(_pipe(TOKEN.encode())),
            ],
            capsys,
            transport,
        )
        assert result.code == code, result.err
    assert len(transport.requests) == 2


def test_a_tampered_journal_is_refused_before_anything_is_sent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    transport = Scripted([_assign_answer(plan)])
    assert (
        _run(_assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport).code
        == 0
    )
    data = json.loads(journal.read_text())
    (entry,) = data["entries"].values()
    entry["body"]["role"] = "approver"
    journal.write_text(json.dumps(data))
    result = _run(_assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport)
    assert result.code == cli.EXIT_BLOCKED
    assert "journal" in result.err
    assert len(transport.requests) == 1


def test_a_journal_in_use_by_another_invocation_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import fcntl

    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    lock = os.open(
        journal.with_name("journal.json.lock"), os.O_RDWR | os.O_CREAT, 0o600
    )
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        transport = Scripted()
        result = _run(
            _assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport
        )
    finally:
        os.close(lock)
    assert result.code == cli.EXIT_BLOCKED
    assert "in use" in result.err
    assert transport.requests == []


def test_a_locked_or_tampered_journal_leaves_the_credential_unread(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import fcntl

    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    lock = os.open(
        journal.with_name("journal.json.lock"), os.O_RDWR | os.O_CREAT, 0o600
    )
    fcntl.flock(lock, fcntl.LOCK_EX)
    fd = _pipe(TOKEN.encode())
    try:
        result = _run(_assign_argv(plan, journal, fd), capsys, Scripted())
    finally:
        os.close(lock)
    assert result.code == cli.EXIT_BLOCKED and "in use" in result.err
    assert os.read(fd, 8192) == TOKEN.encode()  # the pipe was never read
    os.close(fd)

    transport = Scripted([_assign_answer(plan)])
    assert (
        _run(_assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport).code
        == 0
    )
    data = json.loads(journal.read_text())
    (entry,) = data["entries"].values()
    entry["body"]["role"] = "approver"
    journal.write_text(json.dumps(data))
    fd = _pipe(TOKEN.encode())
    result = _run(_assign_argv(plan, journal, fd), capsys, transport)
    assert result.code == cli.EXIT_BLOCKED and "journal" in result.err
    assert os.read(fd, 8192) == TOKEN.encode()
    os.close(fd)


@pytest.mark.parametrize(
    "forge",
    [
        lambda response: response.__setitem__("role", "approver"),
        lambda response: response.__setitem__("actor_user_id", ACTOR_B),
        lambda response: response.__setitem__("payload_digest", "0" * 64),
        lambda response: response.__setitem__("assignment_id", "not-a-uuid"),
        lambda response: response.__setitem__("extra", True),
        lambda response: response.pop("active"),
    ],
)
def test_an_edited_recorded_answer_is_refused_on_replay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], forge: Callable[[Any], Any]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    transport = Scripted([_assign_answer(plan)])
    assert (
        _run(_assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport).code
        == 0
    )
    data = json.loads(journal.read_text())
    (entry,) = data["entries"].values()
    forge(entry["response"])
    journal.write_text(json.dumps(data))
    fd = _pipe(TOKEN.encode())
    result = _run(_assign_argv(plan, journal, fd), capsys, transport)
    assert result.code == cli.EXIT_BLOCKED
    assert "recorded answer" in result.err
    assert result.out == ""
    assert len(transport.requests) == 1
    assert os.read(fd, 8192) == TOKEN.encode()  # refused before the credential
    os.close(fd)
    dry = _run(_assign_argv(plan, journal, None), capsys)
    assert dry.code == cli.EXIT_BLOCKED and "recorded answer" in dry.err


def test_an_edited_recorded_status_or_readiness_answer_is_refused_on_replay(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    general = _general_plan(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    argv = _all_dry_runs(general, general, journal)[3][:-1]  # readiness, no --dry-run
    sent = json.loads(_run(argv + ["--dry-run"], capsys).out)["would_send"]["body"]
    transport = Scripted([_response(201, {**sent, "receipt_id": RECEIPT_ID})])
    applied = _run(argv + ["--auth-fd", str(_pipe(TOKEN.encode()))], capsys, transport)
    assert applied.code == 0, applied.err
    replayed = _run(argv + ["--auth-fd", str(_pipe(TOKEN.encode()))], capsys, transport)
    assert (replayed.code, replayed.json["outcome"]) == (0, "replayed")
    for field_name, value in (
        ("rollout_revision", 2),
        ("operation_receipt_id", RECEIPT_ID),
        ("content_sha256", "0" * 64),
        ("extra", True),
        ("http_status", 200),
    ):
        data = json.loads(journal.read_text())
        (entry,) = data["entries"].values()
        original = json.loads(json.dumps(entry))
        if field_name == "http_status":
            entry["http_status"] = value
        else:
            entry["response"][field_name] = value
        journal.write_text(json.dumps(data))
        result = _run(
            argv + ["--auth-fd", str(_pipe(TOKEN.encode()))], capsys, transport
        )
        assert result.code == cli.EXIT_BLOCKED, field_name
        assert "recorded answer" in result.err
        data["entries"] = {next(iter(data["entries"])): original}
        journal.write_text(json.dumps(data))
    assert len(transport.requests) == 1


def test_a_live_readiness_answer_with_another_content_digest_is_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    general = _general_plan(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    argv = _all_dry_runs(general, general, journal)[3][:-1]  # readiness, no --dry-run
    sent = json.loads(_run(argv + ["--dry-run"], capsys).out)["would_send"]["body"]
    answer = {**sent, "receipt_id": RECEIPT_ID, "content_sha256": "0" * 64}
    transport = Scripted([_response(201, answer)])
    result = _run(argv + ["--auth-fd", str(_pipe(TOKEN.encode()))], capsys, transport)
    assert result.code == cli.EXIT_UNKNOWN
    assert result.json["reason"] == "response_mismatch"
    (entry,) = json.loads(journal.read_text())["entries"].values()
    assert entry["status"] == "unknown"


def test_an_empty_journal_path_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    for value in ("", "  ", "/", ".", "..", "./", "x/.."):
        fd = _pipe(TOKEN.encode())
        argv = _assign_argv(plan, None, fd) + ["--journal", value]
        with pytest.raises(SystemExit) as exit_info:
            cli.main(argv, transport=Scripted())
        captured = capsys.readouterr()
        assert exit_info.value.code == cli.EXIT_BLOCKED == 2
        assert "--journal" in captured.err and "journal file" in captured.err
        assert "ValueError" not in captured.err
        assert os.read(fd, 8192) == TOKEN.encode()
        os.close(fd)


def test_the_journal_and_its_lock_are_private_to_the_operator(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import stat

    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    previous = os.umask(0o022)
    try:
        transport = Scripted([_assign_answer(plan)])
        assert (
            _run(
                _assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport
            ).code
            == 0
        )
    finally:
        os.umask(previous)
    for path in (journal, journal.with_name("journal.json.lock")):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600, path


def test_an_unexpected_failure_before_sending_prints_only_a_fixed_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan_file(tmp_path, capsys)
    marker = "pre-send-exception-marker"

    def explode(_: Path) -> Any:
        raise RuntimeError(f"{marker} {TOKEN}")

    monkeypatch.setattr(cli, "load_plan", explode)
    transport = Scripted()
    result = _run(
        _assign_argv(plan, tmp_path / "j.json", _pipe(TOKEN.encode())),
        capsys,
        transport,
    )
    assert result.code == cli.EXIT_BLOCKED
    assert result.out == ""
    assert result.err == "blocked: unexpected RuntimeError; nothing was sent\n"
    assert transport.requests == []


def test_a_writer_that_never_closes_is_refused_within_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    monkeypatch.setattr(cli, "FD_READ_TIMEOUT_SECONDS", 0.3)
    read_fd, write_fd = os.pipe()
    os.write(write_fd, TOKEN.encode()[:20])  # partial, writer held open
    outcome: dict[str, Any] = {}

    def reader() -> None:
        try:
            cli.read_auth_fd(read_fd)
        except cli.OperatorError as error:
            outcome["error"] = str(error)

    thread = threading.Thread(target=reader, daemon=True)
    started = time.monotonic()
    thread.start()
    thread.join(timeout=5)
    elapsed = time.monotonic() - started
    alive = thread.is_alive()
    os.close(write_fd)  # unblocks a reader with no timeout
    thread.join(timeout=5)
    assert not alive, "the descriptor read has no timeout"
    assert outcome.get("error") == "authentication FD read timed out"
    assert elapsed < 3
    assert not _is_open(read_fd)


def test_the_exact_header_set_is_sent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    transport = Scripted(
        [
            _assign_answer(plan),
            cli.HttpResponse(status=204, body=b""),
            _transition_answer("disabled", 7),
        ]
    )
    assert (
        _run(_assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport).code
        == 0
    )
    assert (
        _run(
            [
                "revoke",
                "--assignment-id",
                ASSIGNMENT_ID,
                "--journal",
                str(journal),
                "--auth-fd",
                str(_pipe(TOKEN.encode())),
            ],
            capsys,
            transport,
        ).code
        == 0
    )
    assert (
        _run(
            ["readback", "--auth-fd", str(_pipe(TOKEN.encode()))], capsys, transport
        ).code
        == 0
    )
    base = {
        "Accept": "application/json",
        "Authorization": f"Bearer {TOKEN}",
        "Cache-Control": "no-store",
    }
    assert [dict(request.headers) for request in transport.requests] == [
        {**base, "Content-Type": "application/json"},
        base,
        base,
    ]


def test_revoke_is_a_delete_and_repeats_from_the_journal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = tmp_path / "journal.json"
    transport = Scripted([cli.HttpResponse(status=204, body=b"")])
    argv = [
        "revoke",
        "--assignment-id",
        ASSIGNMENT_ID.upper(),
        "--journal",
        str(journal),
        "--auth-fd",
    ]
    first = _run(argv + [str(_pipe(TOKEN.encode()))], capsys, transport)
    second = _run(argv + [str(_pipe(TOKEN.encode()))], capsys, transport)
    assert (first.code, second.code) == (0, 0), first.err
    (request,) = transport.requests
    assert (request.method, request.path, request.body) == (
        "DELETE",
        f"/v1/admin/billing/operations/assignments/{ASSIGNMENT_ID}",
        None,
    )
    assert second.json["outcome"] == "replayed"


# --------------------------------------------------------------------------
# F. Client canonicalization equals the server's
# --------------------------------------------------------------------------


def _bodies(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    """Each subcommand's dry-run body, keyed by subcommand."""

    plan = _plan_file(
        tmp_path, capsys, **{"--expires-at": "2026-11-01T09:00:00.123456-03:00"}
    )
    general = _general_plan(tmp_path, capsys)
    bodies = {}
    for argv in _all_dry_runs(plan, general, tmp_path / "j.json")[:4]:
        bodies[argv[0]] = _run(argv, capsys).json["would_send"]["body"]
    return bodies


def test_payload_digest_equals_the_server_route_parsing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = [
        {},
        {
            "--from-state": "canary_allowlist",
            "--to-state": "general",
            "--canary-builder-id": "",
        },
        {
            "--from-state": "canary_allowlist",
            "--to-state": "disabled",
            "--canary-builder-id": "",
        },
        {
            "--from-state": "general",
            "--to-state": "disabled",
            "--canary-builder-id": "",
        },
        {
            "--scope": "incident",
            "--from-state": "general",
            "--to-state": "disabled",
            "--canary-builder-id": "",
        },
        {"--expires-at": "2026-11-01T09:00:00.654321-03:00"},
    ]
    for overrides in cases:
        plan = json.loads(_plan_file(tmp_path, capsys, **overrides).read_text())
        body = {**plan["material"], "expected_revision": 0}
        parsed = admin_route._billing_material(
            BillingRolloutTransitionRequest.model_validate(body)
        )
        assert parsed.payload_digest == plan["payload_digest"]


def test_canary_budget_digest_equals_the_server_helper() -> None:
    for builder in (BUILDER_ID, str(uuid4()), str(uuid4()).upper()):
        assert cli.canary_budget_digest(
            "staging", builder
        ) == server_canary_budget_digest(
            environment="staging", canary_builder_id=UUID(builder)
        )


def test_readiness_content_digest_equals_the_server(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = _bodies(tmp_path, capsys)["readiness"]
    expected = _readiness_content_digest(
        environment=body["environment"],
        receipt_kind=body["receipt_kind"],
        rollout_revision=body["rollout_revision"],
        operation_id=UUID(body["operation_id"]),
        operation_receipt_id=UUID(body["operation_receipt_id"]),
        provider_receipt_id=UUID(body["provider_receipt_id"]),
        rule_digest=body["rule_digest"],
        bundle_digest=body["bundle_digest"],
        app_digest=body["app_digest"],
    )
    assert body["content_sha256"] == expected
    assert body["app_digest"] == DEPLOYMENT_DIGEST


def test_every_request_body_is_accepted_by_the_server_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bodies = _bodies(tmp_path, capsys)
    BillingOperationAssignmentRequest.model_validate(bodies["assign"])
    BillingOperationReceiptRequest.model_validate(bodies["receipt"])  # extra=forbid
    BillingRolloutReadinessReceiptRequest.model_validate(bodies["readiness"])
    BillingRolloutTransitionRequest.model_validate(bodies["transition"])


# --------------------------------------------------------------------------
# G. The plan: one shared, non-secret operation material
# --------------------------------------------------------------------------


def test_the_canary_plan_signs_its_builder(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = json.loads(_plan_file(tmp_path, capsys).read_text())
    assert plan["canary_builder_id"] == BUILDER_ID
    assert plan["material"]["budget_digest"] == server_canary_budget_digest(
        environment="staging", canary_builder_id=UUID(BUILDER_ID)
    )
    general = json.loads(_general_plan(tmp_path, capsys).read_text())
    assert general["canary_builder_id"] is None
    assert general["material"]["budget_digest"] == cli.NO_CANARY_BUDGET_DIGEST


@pytest.mark.parametrize(
    "overrides",
    [
        {"--to-state": "general"},
        {"--from-state": "general", "--to-state": "canary_allowlist"},
        {
            "--from-state": "disabled",
            "--to-state": "disabled",
            "--canary-builder-id": "",
        },
        {"--scope": "incident", "--canary-builder-id": ""},
        {
            "--scope": "incident",
            "--from-state": "disabled",
            "--to-state": "disabled",
            "--canary-builder-id": "",
        },
        {"--canary-builder-id": ""},
        {"--from-state": "canary_allowlist", "--to-state": "general"},
        {"--expires-at": "2026-11-01T12:00:00"},
        {"--deployment-digest": "sha256:" + "d" * 64},
        {"--deployment-digest": "D" * 64},
        {"--operation-id": "not-a-uuid"},
    ],
)
def test_the_plan_refuses_material_the_server_can_never_use(
    capsys: pytest.CaptureFixture[str], overrides: dict[str, str]
) -> None:
    arguments = {
        "--scope": "rollout",
        "--from-state": "disabled",
        "--to-state": "canary_allowlist",
        "--deployment-digest": DEPLOYMENT_DIGEST,
        "--expires-at": EXPIRES_AT,
        "--canary-builder-id": BUILDER_ID,
        **overrides,
    }
    argv = ["plan"]
    for flag, value in arguments.items():
        if value:
            argv += [flag, value]
    try:
        result = _run(argv, capsys)
    except SystemExit as exit_info:  # argparse choices
        capsys.readouterr()
        assert exit_info.code == 2
        return
    assert result.code == cli.EXIT_BLOCKED
    assert result.out == ""


@pytest.mark.parametrize(
    "edit",
    [
        lambda plan: plan["material"].__setitem__("to_state", "general"),
        lambda plan: plan["material"].__setitem__("environment", "production"),
        lambda plan: plan.__setitem__("canary_builder_id", str(uuid4())),
        lambda plan: plan["material"].__setitem__("deployment_digest", "c" * 64),
        lambda plan: plan.__setitem__("extra", True),
    ],
)
def test_an_edited_plan_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], edit: Callable[[Any], None]
) -> None:
    path = _plan_file(tmp_path, capsys)
    plan = json.loads(path.read_text())
    edit(plan)
    path.write_text(json.dumps(plan))
    result = _run(_assign_argv(path, tmp_path / "j.json", None), capsys)
    assert result.code == cli.EXIT_BLOCKED
    assert "plan" in result.err


def test_readiness_needs_the_general_plan(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    argv = _all_dry_runs(plan, plan, tmp_path / "j.json")[3]
    result = _run(argv, capsys)
    assert result.code == cli.EXIT_BLOCKED
    assert "canary_allowlist -> general" in result.err


def test_incident_disable_and_transition_need_their_own_scope(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rollout = _plan_file(tmp_path, capsys)
    result = _run(
        [
            "incident-disable",
            "--plan",
            str(rollout),
            "--expected-revision",
            "0",
            "--dry-run",
        ],
        capsys,
    )
    assert result.code == cli.EXIT_BLOCKED
    incident = _plan_file(
        tmp_path,
        capsys,
        **{
            "--scope": "incident",
            "--from-state": "canary_allowlist",
            "--to-state": "disabled",
            "--canary-builder-id": "",
        },
    )
    result = _run(
        [
            "transition",
            "--plan",
            str(incident),
            "--expected-revision",
            "0",
            "--dry-run",
        ],
        capsys,
    )
    assert result.code == cli.EXIT_BLOCKED


# --------------------------------------------------------------------------
# H. Responses are checked against what was asked
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"payload_digest": "0" * 64},
        {"role": "approver"},
        {"actor_user_id": ACTOR_B},
        {"operation_id": str(uuid4())},
        {"active": False},
    ],
)
def test_an_assignment_response_that_differs_from_the_request_is_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], overrides: dict[str, Any]
) -> None:
    plan = _plan_file(tmp_path, capsys)
    journal = tmp_path / "journal.json"
    transport = Scripted([_assign_answer(plan, **overrides)])
    result = _run(_assign_argv(plan, journal, _pipe(TOKEN.encode())), capsys, transport)
    assert result.code == cli.EXIT_UNKNOWN
    assert result.json["reason"] == "response_mismatch"
    (entry,) = json.loads(journal.read_text())["entries"].values()
    assert entry["status"] == "unknown"


@pytest.mark.parametrize(
    ("state", "revision"),
    [("disabled", 1), ("canary_allowlist", 2), ("canary_allowlist", 0)],
)
def test_a_transition_response_must_show_the_target_state_at_the_next_revision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], state: str, revision: int
) -> None:
    plan = _plan_file(tmp_path, capsys)
    transport = Scripted([_transition_answer(state, revision)])
    result = _run(
        [
            "transition",
            "--plan",
            str(plan),
            "--expected-revision",
            "0",
            "--journal",
            str(tmp_path / "j.json"),
            "--auth-fd",
            str(_pipe(TOKEN.encode())),
        ],
        capsys,
        transport,
    )
    assert result.code == cli.EXIT_UNKNOWN


def test_readback_prints_the_closed_snapshot(
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport = Scripted([_transition_answer("disabled", 7)])
    result = _run(
        ["readback", "--auth-fd", str(_pipe(TOKEN.encode()))], capsys, transport
    )
    assert result.code == cli.EXIT_OK, result.err
    assert result.json["response"] == {
        "environment": "staging",
        "state": "disabled",
        "rollout_revision": 7,
        "topup_commercial_enabled": False,
        "topup_revision": 0,
    }
    (request,) = transport.requests
    assert (request.method, request.path, request.body) == (
        "GET",
        "/v1/admin/billing/rollout/readback",
        None,
    )


# --------------------------------------------------------------------------
# I. Production transport and packaging
# --------------------------------------------------------------------------


def test_the_origin_is_the_production_serving_api() -> None:
    from scripts.staging_rehearsal_common import STAGING_API_ORIGIN

    assert cli.API_ORIGIN == STAGING_API_ORIGIN
    assert cli.API_ORIGIN.startswith("https://")


def _fake_opener(
    monkeypatch: pytest.MonkeyPatch, body: bytes | None = None
) -> dict[str, Any]:
    """Replace ``build_opener``; record handlers and what is opened."""

    seen: dict[str, Any] = {}

    class Response:
        status = 201

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def read(self, limit: int | None = None) -> bytes:
            seen["limit"] = limit
            if body is None:
                return b"{}"
            return body if limit is None else body[:limit]

    class Opener:
        def open(self, request: Any, timeout: float) -> Response:
            seen["url"] = request.full_url
            seen["timeout"] = timeout
            return Response()

    def build_opener(*handlers: Any) -> Opener:
        seen["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(cli, "build_opener", build_opener)
    return seen


def _send_one() -> Any:
    return cli._https_send(
        cli.HttpRequest(
            method="POST",
            path="/v1/admin/billing/rollout/transitions",
            body=b"{}",
            headers={"Authorization": "Bearer x.y.z"},
        )
    )


def _raising_opener(monkeypatch: pytest.MonkeyPatch, status: int, body: bytes) -> None:
    """``build_opener`` whose ``open`` raises ``HTTPError`` (every real 4xx)."""

    import io
    from urllib.error import HTTPError

    class Opener:
        def open(self, request: Any, timeout: float) -> Any:
            raise HTTPError(
                request.full_url, status, "Conflict", {}, io.BytesIO(body)  # type: ignore[arg-type]
            )

    monkeypatch.setattr(cli, "build_opener", lambda *handlers: Opener())


def test_the_https_transport_keeps_the_status_and_body_of_an_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = b'{"detail":"billing_operation_conflict"}'
    _raising_opener(monkeypatch, 409, body)
    response = _send_one()
    assert (response.status, response.body) == (409, body)


def test_an_http_error_answer_is_a_refusal_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _plan_file(tmp_path, capsys)
    _raising_opener(monkeypatch, 409, b'{"detail":"billing_operation_conflict"}')
    argv = [
        "transition",
        "--plan",
        str(plan),
        "--expected-revision",
        "0",
        "--journal",
        str(tmp_path / "j.json"),
        "--auth-fd",
        str(_pipe(TOKEN.encode())),
    ]
    code = cli.main(argv, transport=cli._https_send)
    captured = capsys.readouterr()
    assert code == cli.EXIT_REFUSED
    output = json.loads(captured.out)
    assert (output["http_status"], output["detail"]) == (
        409,
        "billing_operation_conflict",
    )
    assert "refused: HTTP 409 billing_operation_conflict" in captured.err


def test_an_oversized_http_error_body_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _raising_opener(monkeypatch, 409, b"x" * (cli.MAX_RESPONSE_BYTES + 1))
    with pytest.raises(cli.TransportError):
        _send_one()
    _raising_opener(monkeypatch, 409, b"x" * cli.MAX_RESPONSE_BYTES)
    assert len(_send_one().body) == cli.MAX_RESPONSE_BYTES


def test_the_https_transport_ignores_proxy_environment_and_verifies_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY"):
        monkeypatch.setenv(name, "http://proxy.invalid:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    seen = _fake_opener(monkeypatch)
    assert _send_one().status == 201
    proxies = [h for h in seen["handlers"] if isinstance(h, cli.ProxyHandler)]
    assert len(proxies) == 1 and proxies[0].proxies == {}
    (https,) = [h for h in seen["handlers"] if isinstance(h, cli.HTTPSHandler)]
    context = https._context
    assert context.verify_mode == cli.ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert seen["timeout"] == cli.HTTP_TIMEOUT_SECONDS


def test_the_https_transport_refuses_an_oversized_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_opener(monkeypatch, body=b"x" * (cli.MAX_RESPONSE_BYTES + 1))
    with pytest.raises(cli.TransportError):
        _send_one()
    _fake_opener(monkeypatch, body=b"x" * cli.MAX_RESPONSE_BYTES)
    assert len(_send_one().body) == cli.MAX_RESPONSE_BYTES


def test_the_https_transport_uses_no_proxy_and_follows_no_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class Response:
        status = 201

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_: Any) -> None:
            return None

        def read(self, limit: int) -> bytes:
            seen["limit"] = limit
            return b"{}"

    class Opener:
        def open(self, request: Any, timeout: float) -> Response:
            seen["url"] = request.full_url
            seen["method"] = request.get_method()
            seen["authorization"] = request.get_header("Authorization")
            seen["timeout"] = timeout
            return Response()

    def build_opener(*handlers: Any) -> Opener:
        seen["handlers"] = handlers
        return Opener()

    monkeypatch.setattr(cli, "build_opener", build_opener)
    response = cli._https_send(
        cli.HttpRequest(
            method="POST",
            path="/v1/admin/billing/rollout/transitions",
            body=b"{}",
            headers={"Authorization": "Bearer x.y.z"},
        )
    )
    assert response.status == 201
    assert seen["url"] == cli.API_ORIGIN + "/v1/admin/billing/rollout/transitions"
    proxies = [h for h in seen["handlers"] if isinstance(h, cli.ProxyHandler)]
    assert len(proxies) == 1 and proxies[0].proxies == {}
    redirects = [h for h in seen["handlers"] if isinstance(h, cli.HTTPRedirectHandler)]
    assert len(redirects) == 1
    assert (
        redirects[0].redirect_request(None, None, 302, "", {}, "https://x.test/")
        is None
    )


def test_the_cli_runs_standalone_without_the_application(tmp_path: Path) -> None:
    program = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('op', {str(SCRIPT)!r})\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "sys.modules['op'] = module\n"
        "spec.loader.exec_module(module)\n"
        "assert not [name for name in sys.modules if name == 'src' or name.startswith('src.')]\n"
    )
    subprocess.run([sys.executable, "-I", "-B", "-c", program], check=True, timeout=30)
    for command in ([], ["plan"], ["assign"], ["readiness"], ["readback"]):
        result = subprocess.run(
            [sys.executable, "-I", "-B", str(SCRIPT), *command, "--help"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr
        assert "usage:" in result.stdout.lower()


def test_the_command_surface_is_the_rollout_ceremony_only() -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert set(cli.SUBCOMMANDS) == {
        "plan",
        "whoami",
        "assign",
        "receipt",
        "readiness",
        "transition",
        "incident-disable",
        "revoke",
        "readback",
    }
    for forbidden in ("topup-capability", "producer-receipts", "/v1/auth/", "hmac"):
        assert forbidden not in source


# --------------------------------------------------------------------------
# K. The whole ceremony through the CLI on a migrated PostgreSQL
# --------------------------------------------------------------------------


@dataclass
class _Ceremony:
    loop: asyncio.AbstractEventLoop
    client: Any
    sessions: Any
    tmp_path: Path
    tokens: dict[str, str]
    actors: dict[str, str]
    requests: list[Any] = field(default_factory=list)
    sent_steps: dict[str, list[str]] = field(default_factory=dict)

    def transport(self, request: Any) -> Any:
        self.requests.append(request)
        future = asyncio.run_coroutine_threadsafe(
            self.client.request(
                request.method,
                request.path,
                content=request.body,
                headers=dict(request.headers),
            ),
            self.loop,
        )
        response = future.result(timeout=60)
        return cli.HttpResponse(status=response.status_code, body=response.content)

    async def run(self, argv: list[str], token: str | None = None) -> tuple[int, Any]:
        import contextlib
        import io

        if token is not None:
            argv = argv + ["--auth-fd", str(_pipe(token.encode()))]
        out, err = io.StringIO(), io.StringIO()

        def call() -> int:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                return int(cli.main(argv, transport=self.transport))

        code = await asyncio.to_thread(call)
        for secret in self.tokens.values():
            assert secret not in out.getvalue() + err.getvalue()
        return code, (json.loads(out.getvalue()) if out.getvalue() else err.getvalue())

    async def scalar(self, sql: str, **params: Any) -> Any:
        from sqlalchemy import text

        async with self.sessions() as session:
            return await session.scalar(text(sql), params)

    async def clock_past(self, sql: str, **params: Any) -> datetime | None:
        """Wait until the wall clock is safely past a database stamp (WSL2)."""

        stamp = await self.scalar(sql, **params)
        if stamp is not None:
            while datetime.now(timezone.utc) <= stamp + timedelta(milliseconds=250):
                await asyncio.sleep(0.01)
        return stamp

    async def plan(self, **arguments: str) -> Path:
        argv = [
            "plan",
            "--deployment-digest",
            DEPLOYMENT_DIGEST,
            "--expires-at",
            (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat(),
        ]
        for flag, value in arguments.items():
            argv += [f"--{flag.replace('_', '-')}", value]
        code, plan = await self.run(argv)
        assert code == 0, plan
        path = self.tmp_path / f"plan-{plan['material']['operation_id']}.json"
        path.write_text(json.dumps(plan), encoding="utf-8")
        return path

    def journal(self, role: str) -> str:
        return str(self.tmp_path / f"journal-{role}.json")

    async def quorum(self, plan: Path) -> dict[str, str]:
        await self.clock_past(
            "SELECT max(created_at) FROM billing_rollout_receipts WHERE environment = 'staging'"
        )
        receipts = {}
        for role in ("deployer", "approver", "reviewer"):
            assign = [
                "assign",
                "--plan",
                str(plan),
                "--role",
                role,
                "--actor-user-id",
                self.actors[role],
            ]
            code, assigned = await self.run(
                assign + ["--journal", self.journal(role)], self.tokens[role]
            )
            assert code == 0, assigned
            receipt_argv = [
                "receipt",
                "--plan",
                str(plan),
                "--assignment-id",
                assigned["response"]["assignment_id"],
                "--nonce",
                str(uuid4()),
            ]
            code, receipt = await self.run(
                receipt_argv + ["--journal", self.journal(role)], self.tokens[role]
            )
            assert code == 0, receipt
            receipts[role] = receipt["response"]["receipt_id"]
            self.sent_steps[f"assign-{role}"] = assign
            self.sent_steps[f"receipt-{role}"] = receipt_argv
        return receipts

    async def resend(self, argv: list[str], role: str) -> tuple[int, Any]:
        """Send an applied step again with an empty journal (no replay)."""

        journal = self.journal(f"resend-{uuid4().hex}")
        return await self.run(argv + ["--journal", journal], self.tokens[role])

    async def readback(self, role: str = "reviewer") -> dict[str, Any]:
        rehearsal = _rehearsal_token(self.actors[role], secret=settings.secret_key)
        self.tokens[f"rehearsal-{role}"] = rehearsal
        code, result = await self.run(["readback"], rehearsal)
        assert code == 0, result
        return dict(result["response"])


@pytest.fixture
async def ceremony(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[_Ceremony]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    import httpx
    from pydantic import SecretStr
    from sqlalchemy.ext.asyncio import (
        AsyncSession,
        async_sessionmaker,
        create_async_engine,
    )
    from sqlalchemy.pool import NullPool

    from tests.integration.test_asaas_billing_postgres import (
        _create_child,
        _drop_child,
        _run_alembic,
        _validated_admin_url,
    )

    monkeypatch.setattr(settings, "environment", "staging")
    monkeypatch.setattr(
        settings,
        "billing_operation_receipt_signing_key",
        SecretStr("rollout-operator-cli-measurement-key-0123456789"),
    )
    admin_url = _validated_admin_url(raw_admin_url)
    admin_engine = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    name = f"neurostack_asaas_smoke_{uuid4().hex[:12]}"
    await _create_child(admin_engine, name)
    engine = create_async_engine(admin_url.set(database=name), poolclass=NullPool)
    try:
        await _run_alembic(admin_url.set(database=name), "upgrade", "head")
        sessions = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        app = FastAPI()
        app.include_router(admin_route.router, prefix="/v1/admin")
        app.add_middleware(AuthMiddleware)

        async def session() -> AsyncIterator[AsyncSession]:
            async with sessions() as db_session:
                yield db_session

        app.dependency_overrides[get_db_session_dependency] = session
        actors = {role: str(uuid4()) for role in ("deployer", "approver", "reviewer")}
        tokens = {
            role: _token(actor, secret=settings.secret_key)
            for role, actor in actors.items()
        }
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://operator.test"
        ) as client:
            yield _Ceremony(
                loop=asyncio.get_running_loop(),
                client=client,
                sessions=sessions,
                tmp_path=tmp_path,
                tokens=tokens,
                actors=actors,
            )
    finally:
        await engine.dispose()
        await _drop_child(admin_engine, name)
        await admin_engine.dispose()


@pytest.mark.integration
async def test_postgres_ceremony_runs_end_to_end_through_the_cli(
    ceremony: _Ceremony,
) -> None:
    from sqlalchemy import text

    from src.services.billing_outbox import billing_fact_bundle_digest

    async with ceremony.sessions() as session:
        async with session.begin():
            await session.execute(
                text("INSERT INTO builders (id, name) VALUES (:id, 'Canary')"),
                {"id": UUID(BUILDER_ID)},
            )
    assert (await ceremony.readback())["state"] == "disabled"

    # 1. disabled -> canary_allowlist: three operators, own credentials.
    canary = await ceremony.plan(
        scope="rollout",
        from_state="disabled",
        to_state="canary_allowlist",
        canary_builder_id=BUILDER_ID,
    )
    await ceremony.quorum(canary)
    # One person, one duty: the server's unique index refusal is shown as is.
    code, refused = await ceremony.run(
        [
            "assign",
            "--plan",
            str(canary),
            "--role",
            "approver",
            "--actor-user-id",
            ceremony.actors["deployer"],
            "--journal",
            ceremony.journal("deployer-2"),
        ],
        ceremony.tokens["deployer"],
    )
    assert code == cli.EXIT_REFUSED
    assert (refused["http_status"], refused["detail"]) == (
        409,
        "billing_operation_conflict",
    )

    transition = [
        "transition",
        "--plan",
        str(canary),
        "--expected-revision",
        "0",
        "--journal",
        ceremony.journal("deployer"),
    ]
    code, opened = await ceremony.run(transition, ceremony.tokens["deployer"])
    assert code == 0, opened
    assert opened["response"]["state"] == "canary_allowlist"
    sent = len(ceremony.requests)
    code, again = await ceremony.run(transition, ceremony.tokens["deployer"])
    assert (code, again["outcome"]) == (0, "replayed")
    assert len(ceremony.requests) == sent
    readback = await ceremony.readback("approver")
    assert (readback["state"], readback["rollout_revision"]) == ("canary_allowlist", 1)

    # What the server answers when an applied step is sent again (the
    # runbook's Recovery section): nothing is applied twice.
    resent = {
        "assign": await ceremony.resend(
            ceremony.sent_steps["assign-reviewer"], "reviewer"
        ),
        "receipt": await ceremony.resend(
            ceremony.sent_steps["receipt-reviewer"], "reviewer"
        ),
        "transition": await ceremony.resend(transition[:5], "deployer"),
    }
    assert {
        step: (code, out["http_status"], out["detail"])
        for step, (code, out) in resent.items()
    } == {
        "assign": (cli.EXIT_REFUSED, 409, "billing_operation_conflict"),
        "receipt": (cli.EXIT_REFUSED, 409, "billing_operation_assignment_invalid"),
        "transition": (cli.EXIT_REFUSED, 409, "billing_rollout_from_state_mismatch"),
    }

    # 1b. Standby containment (runbook): an incident quorum signed now, while
    # the state is canary_allowlist, stays unspent through the next move.
    standby = await ceremony.plan(
        scope="incident", from_state="canary_allowlist", to_state="disabled"
    )
    await ceremony.quorum(standby)

    # 2. canary_allowlist -> general: quorum, four readiness rows, transition.
    general = await ceremony.plan(
        scope="rollout", from_state="canary_allowlist", to_state="general"
    )
    receipts = await ceremony.quorum(general)
    stamp = await ceremony.scalar(
        "SELECT max(created_at) FROM billing_operation_receipts "
        "WHERE id::text = ANY(CAST(:ids AS text[]))",
        ids=[receipts["deployer"], receipts["reviewer"]],
    )
    observed_at = stamp + timedelta(microseconds=1)
    while datetime.now(timezone.utc) <= observed_at + timedelta(milliseconds=250):
        await asyncio.sleep(0.01)
    rule_digest = hashlib.sha256(
        Path("config/prometheus_rules.yml").read_bytes()
    ).hexdigest()

    def readiness_argv(kind: str) -> list[str]:
        return [
            "readiness",
            "--plan",
            str(general),
            "--kind",
            kind,
            "--rollout-revision",
            "1",
            "--deployer-receipt-id",
            receipts["deployer"],
            "--reviewer-receipt-id",
            receipts["reviewer"],
            "--rule-digest",
            rule_digest,
            "--bundle-digest",
            billing_fact_bundle_digest(),
            "--observed-at",
            observed_at.isoformat(),
        ]

    for kind, role in (
        ("readiness", "deployer"),
        ("backlog", "reviewer"),
        ("provisioning", "reviewer"),
        ("smoke", "reviewer"),
    ):
        code, recorded = await ceremony.run(
            readiness_argv(kind) + ["--journal", ceremony.journal(role)],
            ceremony.tokens[role],
        )
        assert code == 0, (kind, recorded)
    code, again = await ceremony.resend(readiness_argv("smoke"), "reviewer")
    assert (code, again["detail"]) == (
        cli.EXIT_REFUSED,
        "billing_rollout_readiness_duplicate",
    )
    general_argv = ["transition", "--plan", str(general), "--expected-revision", "1"]
    code, promoted = await ceremony.run(
        general_argv + ["--journal", ceremony.journal("deployer")],
        ceremony.tokens["deployer"],
    )
    assert code == 0, promoted
    code, again = await ceremony.resend(general_argv, "deployer")
    assert (code, again["detail"]) == (
        cli.EXIT_REFUSED,
        "billing_rollout_readiness_unbound",
    )
    assert (
        promoted["response"]["state"],
        promoted["response"]["rollout_revision"],
    ) == (
        "general",
        2,
    )

    # 3. Incident containment with the standby quorum: signed before the
    # general move, for from_state canary_allowlist, spent from general.
    code, contained = await ceremony.run(
        [
            "incident-disable",
            "--plan",
            str(standby),
            "--expected-revision",
            "2",
            "--journal",
            ceremony.journal("approver"),
        ],
        ceremony.tokens["approver"],
    )
    assert code == 0, contained
    assert (
        contained["response"]["state"],
        contained["response"]["rollout_revision"],
    ) == (
        "disabled",
        3,
    )
    assert (await ceremony.readback())["state"] == "disabled"
    code, again = await ceremony.resend(
        ["incident-disable", "--plan", str(standby), "--expected-revision", "2"],
        "approver",
    )
    assert (code, again["detail"]) == (
        cli.EXIT_REFUSED,
        "billing_operation_receipt_invalid",
    )

    # 4. Re-open the canary (same builder), then roll back with a rollout quorum.
    canary_again = await ceremony.plan(
        scope="rollout",
        from_state="disabled",
        to_state="canary_allowlist",
        canary_builder_id=BUILDER_ID,
    )
    await ceremony.quorum(canary_again)
    code, reopened = await ceremony.run(
        [
            "transition",
            "--plan",
            str(canary_again),
            "--expected-revision",
            "3",
            "--journal",
            ceremony.journal("deployer"),
        ],
        ceremony.tokens["deployer"],
    )
    assert code == 0, reopened
    rollback = await ceremony.plan(
        scope="rollout", from_state="canary_allowlist", to_state="disabled"
    )
    await ceremony.quorum(rollback)
    code, rolled_back = await ceremony.run(
        [
            "transition",
            "--plan",
            str(rollback),
            "--expected-revision",
            "4",
            "--journal",
            ceremony.journal("approver"),
        ],
        ceremony.tokens["approver"],
    )
    assert code == 0, rolled_back
    final = await ceremony.readback("deployer")
    assert (final["state"], final["rollout_revision"]) == ("disabled", 5)

    # 5. Revoke an unused assignment; repeating it is a replay.
    spare = await ceremony.plan(
        scope="rollout",
        from_state="disabled",
        to_state="canary_allowlist",
        canary_builder_id=BUILDER_ID,
    )
    code, assigned = await ceremony.run(
        [
            "assign",
            "--plan",
            str(spare),
            "--role",
            "deployer",
            "--actor-user-id",
            ceremony.actors["deployer"],
            "--journal",
            ceremony.journal("deployer"),
        ],
        ceremony.tokens["deployer"],
    )
    assert code == 0, assigned
    revoke = [
        "revoke",
        "--assignment-id",
        assigned["response"]["assignment_id"],
        "--journal",
        ceremony.journal("deployer"),
    ]
    assert (await ceremony.run(revoke, ceremony.tokens["deployer"]))[0] == 0
    assert (await ceremony.run(revoke, ceremony.tokens["deployer"]))[1][
        "outcome"
    ] == "replayed"
    active = await ceremony.scalar(
        "SELECT active FROM billing_operation_assignments WHERE id = :id",
        id=UUID(assigned["response"]["assignment_id"]),
    )
    assert active is False

    # No journal holds any credential.
    for journal in ceremony.tmp_path.glob("journal-*.json"):
        text_value = journal.read_text(encoding="utf-8")
        for secret in ceremony.tokens.values():
            assert secret not in text_value
