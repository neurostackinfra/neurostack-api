"""Deploy-time ASAAS webhook registration CLI (owner decision A2, 2026-10-19).

Every network test runs against an in-process fake of the ASAAS v3 webhook
API behind ``httpx.MockTransport``: nothing here can reach the real provider
(production or sandbox).  Every output channel is scanned for leaks: the
fake keys are the bare ASAAS key namespaces (the validators check only the
prefix, and the release-candidate scan flags any alphanumeric after it), so
the scan looks for the namespace itself; the token, URL, e-mail and webhook
IDs carry distinctive canaries.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import hashlib
import inspect
import io
import json
import logging
import os
import random
import re
import ssl
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from src.config.settings import Settings
from src.ops import asaas_webhook as cli
from src.services.asaas import ASAAS_MAX_RESPONSE_BYTES, ASAAS_TIMEOUT, AsaasClient
from src.services.billing_webhook import (
    WEBHOOK_EVENTS,
    AsaasWebhookProvisioner,
    desired_webhook_settings,
)

ROOT = Path(__file__).resolve().parents[2]

KEY = "$aact_prod_"
SANDBOX_KEY = "$aact_hmlg_"
TOKEN = "CANARYTOKEN" + "x" * 32
# The decided URL: its host is the only one ``ALLOWED_WEBHOOK_HOSTS`` admits.
URL = "https://staging.api.neurostack.global/v1/billing/webhook/asaas"
FOREIGN_HOST_URL = "https://canary-url-host.example/v1/billing/webhook/asaas"
EMAIL = "canary-mailbox@example.test"
NAME = "neurostack-billing-staging"
OWNED_ID = "canaryWebhookId0001"
OTHER_ID = "canaryWebhookId0002"
CANARIES = (
    KEY,
    SANDBOX_KEY,
    "aact_",  # any ASAAS key namespace, whatever its rendering
    "CANARYTOKEN",
    "staging.api.neurostack.global",
    "billing/webhook",
    "canary-url-host",
    "canary-mailbox",
    "canaryWebhookId",
)
ALL_EVENTS = sorted(WEBHOOK_EVENTS)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def webhook(webhook_id: str = OWNED_ID, **overrides: Any) -> dict[str, Any]:
    """One ASAAS ``webhook`` object as the v3 API returns it."""

    item: dict[str, Any] = {
        "object": "webhook",
        "id": webhook_id,
        "name": NAME,
        "url": URL,
        "email": EMAIL,
        "enabled": True,
        "interrupted": False,
        "apiVersion": 3,
        "hasAuthToken": True,
        "sendType": "SEQUENTIALLY",
        "events": list(ALL_EVENTS),
        "penalizedRequestsCount": 0,
    }
    item.update(overrides)
    return item


def foreign(index: int) -> dict[str, Any]:
    return webhook(
        f"foreignHook{index:04d}",
        name=f"other-integration-{index}",
        url=f"https://partner-{index}.example/hooks",
    )


Step = Any  # int status | "timeout" | "connect" | Callable[[httpx.Request], Any]
_FAKES: list["FakeAsaas"] = []


@dataclass
class FakeAsaas:
    """Stateful fake of ``/v3/webhooks`` with scripted failures per operation."""

    hooks: list[dict[str, Any]] = field(default_factory=list)
    reject_duplicates: bool = False
    scripted: dict[str, list[Step]] = field(default_factory=dict)
    seen: list[tuple[str, str, str]] = field(default_factory=list)
    bodies: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    created: int = 0
    # Provider paging quirks: serve at most ``page_cap`` items per page (while
    # still advertising ``hasMore`` as if pages were full), and report
    # ``total_count`` ("auto": the true count; "omit": no field; else as is).
    page_cap: int | None = None
    total_count: Any = "auto"
    # ``pager(offset, limit) -> items`` overrides which items a page serves
    # (``hasMore``/``totalCount`` still describe ``hooks`` consistently).
    pager: Callable[[int, int], list[dict[str, Any]]] | None = None

    def __post_init__(self) -> None:
        _FAKES.append(self)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def script(self, op: str, *steps: Step) -> "FakeAsaas":
        self.scripted.setdefault(op, []).extend(steps)
        return self

    def methods(self) -> list[str]:
        return [method for method, _path, _query in self.seen]

    def ops(self) -> list[str]:
        return [self._op(method, path) for method, path, _query in self.seen]

    @staticmethod
    def _op(method: str, path: str) -> str:
        if path == "/v3/webhooks":
            return {"GET": "list", "POST": "post"}.get(method, "other")
        if path.startswith("/v3/webhooks/"):
            return {"GET": "get", "PUT": "put"}.get(method, "other")
        return "other"

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(
            (request.method, request.url.path, request.url.query.decode("ascii"))
        )
        assert request.headers.get("access_token") == KEY
        op = self._op(request.method, request.url.path)
        queue = self.scripted.get(op)
        if queue:
            step = queue.pop(0)
            if step == "timeout":
                raise httpx.ReadTimeout("scripted", request=request)
            if step == "connect":
                raise httpx.ConnectError("scripted", request=request)
            if isinstance(step, int):
                return _error(step)
            if callable(step):
                outcome = step(request)
                if isinstance(outcome, httpx.Response):
                    return outcome
        handler: Callable[[httpx.Request], httpx.Response] = getattr(
            self, "_" + op, self._other
        )
        return handler(request)

    def _other(self, request: httpx.Request) -> httpx.Response:
        return _error(405)

    def _list(self, request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        limit = int(request.url.params["limit"])
        served = min(limit, self.page_cap or limit)
        page = (
            self.pager(offset, limit)
            if self.pager is not None
            else self.hooks[offset : offset + served]
        )
        payload: dict[str, Any] = {
            "object": "list",
            "hasMore": offset + limit < len(self.hooks),
            "limit": limit,
            "offset": offset,
            "data": [dict(hook) for hook in page],
        }
        if self.total_count == "auto":
            payload["totalCount"] = len(self.hooks)
        elif self.total_count != "omit":
            payload["totalCount"] = self.total_count
        return httpx.Response(200, json=payload)

    def _find(self, request: httpx.Request) -> dict[str, Any] | None:
        webhook_id = request.url.path.rsplit("/", 1)[-1]
        return next((h for h in self.hooks if h["id"] == webhook_id), None)

    def _get(self, request: httpx.Request) -> httpx.Response:
        hook = self._find(request)
        return httpx.Response(200, json=hook) if hook else _error(404)

    def _post(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(("POST", body))
        if self.reject_duplicates and any(
            h["name"] == body["name"] or h["url"] == body["url"] for h in self.hooks
        ):
            return _error(400)
        self.created += 1
        hook = self.apply_create(body, f"canaryWebhookIdNew{self.created}")
        return httpx.Response(200, json=hook)

    def apply_create(self, body: dict[str, Any], webhook_id: str) -> dict[str, Any]:
        hook = webhook(
            webhook_id,
            name=body["name"],
            url=body["url"],
            email=body["email"],
            enabled=body["enabled"],
            interrupted=body["interrupted"],
            apiVersion=body["apiVersion"],
            sendType=body["sendType"],
            events=list(body["events"]),
            hasAuthToken=bool(body.get("authToken")),
        )
        self.hooks.append(hook)
        return hook

    def _put(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(("PUT", body))
        hook = self._find(request)
        if hook is None:
            return _error(404)
        for key in ("name", "url", "enabled", "interrupted", "sendType", "events"):
            hook[key] = body[key]
        hook["hasAuthToken"] = bool(body.get("authToken"))
        return httpx.Response(200, json=hook)


def _error(status: int) -> httpx.Response:
    return httpx.Response(
        status,
        json={"errors": [{"code": "scripted", "description": "scripted failure"}]},
    )


@pytest.fixture(autouse=True)
def _never_delete() -> Iterator[None]:
    """Whatever a test does, no fake ever received a DELETE (or other verb)."""

    _FAKES.clear()
    yield
    for fake in _FAKES:
        assert set(fake.methods()) <= {"GET", "POST", "PUT"}
        assert "other" not in fake.ops()


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "_env_file": None,
        "environment": "staging",
        "secret_key": "s" * 48,
        "asaas_api_key": SecretStr(KEY),
        "asaas_webhook_token": SecretStr(TOKEN),
        "asaas_webhook_url": URL,
        "asaas_webhook_notification_email": EMAIL,
    }
    values.update(overrides)
    return Settings(**values)


def make_config(**overrides: Any) -> cli.RegistrationConfig:
    config = cli.RegistrationConfig.from_settings(make_settings())
    return dataclasses.replace(config, **overrides) if overrides else config


async def run(
    fake: FakeAsaas,
    command: str = "reconcile",
    *,
    config: cli.RegistrationConfig | None = None,
    force: bool = False,
    allow_enable: bool = False,
    no_resume: bool = False,
    sleeps: list[float] | None = None,
    deadline: float = 60.0,
) -> cli.RunResult:
    slept = sleeps if sleeps is not None else []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    return await cli.run(
        command,
        config or make_config(),
        transport=fake.transport,
        sleep=sleep,
        rng=random.Random(7),
        deadline_seconds=deadline,
        force_rewrite_token=force,
        allow_enable=allow_enable,
        no_resume=no_resume,
    )


def invoke(
    argv: list[str],
    *,
    fake: FakeAsaas | None = None,
    settings: Settings | None = None,
) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()

    async def sleep(_seconds: float) -> None:
        return None

    code = cli.main(
        argv,
        settings=settings or make_settings(),
        transport=(fake or FakeAsaas()).transport,
        sleep=sleep,
        rng=random.Random(1),
        stdout=out,
        stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


def _assert_no_canary(*texts: str) -> None:
    for text in texts:
        for canary in CANARIES:
            assert canary not in text


# ---------------------------------------------------------------------------
# Absent / create
# ---------------------------------------------------------------------------


async def test_readback_reports_absent_and_never_writes() -> None:
    fake = FakeAsaas([foreign(1)])
    result = await run(fake, "readback")
    assert (result.verdict, result.action, result.exit_code) == ("absent", "none", 10)
    assert result.reasons == ("absent",)
    assert result.webhook_id_sha256 is None
    assert fake.ops() == ["list"]


async def test_reconcile_creates_the_absent_webhook_once_then_verifies() -> None:
    fake = FakeAsaas([foreign(1)])
    result = await run(fake)
    assert (result.verdict, result.action, result.exit_code) == ("ready", "created", 0)
    assert result.reasons == ()
    assert result.webhook_id_sha256 == _sha("canaryWebhookIdNew1")
    assert fake.ops() == ["list", "post", "get", "list"]
    ((method, body),) = fake.bodies
    assert method == "POST"
    assert body == {
        "name": NAME,
        "url": URL,
        "enabled": True,
        "interrupted": False,
        "authToken": TOKEN,
        "sendType": "SEQUENTIALLY",
        "events": ALL_EVENTS,
        "email": EMAIL,
        "apiVersion": 3,
    }
    assert result.counts["writes"] == 1
    assert result.counts["candidates"] == 1 and result.counts["webhooks_listed"] == 2


async def test_reconcile_of_an_exact_webhook_makes_no_write() -> None:
    fake = FakeAsaas([foreign(1), webhook()])
    result = await run(fake)
    assert (result.verdict, result.action, result.exit_code) == ("ready", "none", 0)
    assert set(fake.methods()) == {"GET"}
    assert fake.bodies == []
    assert result.webhook_id_sha256 == _sha(OWNED_ID)
    assert result.counts["writes"] == 0


async def test_reconcile_is_idempotent_across_runs() -> None:
    fake = FakeAsaas([])
    first = await run(fake)
    second = await run(fake)
    assert (first.action, second.action) == ("created", "none")
    assert [m for m, _ in fake.bodies] == ["POST"]
    assert len(fake.hooks) == 1


# ---------------------------------------------------------------------------
# Drift: every compared field
# ---------------------------------------------------------------------------

DRIFTS = [
    ({"url": "https://old-host.example/v1/billing/webhook/asaas"}, "url", "drift"),
    ({"sendType": "NON_SEQUENTIALLY"}, "send_type", "drift"),
    ({"enabled": False}, "disabled", "disabled"),
    ({"interrupted": True}, "interrupted", "interrupted"),
    ({"events": ["PAYMENT_RECEIVED"]}, "events", "drift"),
    ({"events": ALL_EVENTS + ["PAYMENT_RECEIVED"]}, "events", "drift"),
    ({"events": None}, "events", "drift"),
    ({"hasAuthToken": False}, "token_absent", "drift"),
    ({"hasAuthToken": None}, "token_absent", "drift"),
    ({"enabled": None}, "disabled", "disabled"),
    ({"interrupted": None}, "interrupted", "interrupted"),
]


@pytest.mark.parametrize(("overrides", "reason", "verdict"), DRIFTS)
async def test_readback_classifies_each_drift_without_writing(
    overrides: dict[str, Any], reason: str, verdict: str
) -> None:
    fake = FakeAsaas([webhook(**overrides)])
    result = await run(fake, "readback")
    assert result.verdict == verdict
    assert result.reasons == (reason,)
    assert result.exit_code == cli.EXIT_CODES[verdict] != 0
    assert set(fake.methods()) == {"GET"}


WRITABLE_DRIFTS = [
    (overrides, reason) for overrides, reason, verdict in DRIFTS if verdict == "drift"
]


@pytest.mark.parametrize(("overrides", "reason"), WRITABLE_DRIFTS)
async def test_reconcile_updates_each_drift_once_then_reads_back_ready(
    overrides: dict[str, Any], reason: str
) -> None:
    fake = FakeAsaas([foreign(1), webhook(**overrides)])
    result = await run(fake)
    assert (result.verdict, result.action, result.exit_code) == ("ready", "updated", 0)
    # Every PUT carries the current token, and says so.
    assert result.reasons == ("token_rewritten",)
    assert fake.ops() == ["list", "get", "put", "get", "list"]
    ((method, body),) = fake.bodies
    assert method == "PUT"
    assert "email" not in body and "apiVersion" not in body
    assert body["authToken"] == TOKEN and body["events"] == ALL_EVENTS
    assert body["enabled"] is True and body["interrupted"] is False
    assert result.webhook_id_sha256 == _sha(OWNED_ID)


async def test_disabled_outranks_interrupted_and_drift_in_the_verdict() -> None:
    fake = FakeAsaas([webhook(interrupted=True, enabled=False, sendType="X")])
    result = await run(fake, "readback")
    assert result.verdict == "disabled"
    assert set(result.reasons) == {"interrupted", "disabled", "send_type"}
    fake = FakeAsaas([webhook(interrupted=True, sendType="X")])
    assert (await run(fake, "readback")).verdict == "interrupted"


# ---------------------------------------------------------------------------
# Operator pause (enabled=false) and ASAAS queue interruption
# ---------------------------------------------------------------------------

# (webhook overrides, allow_enable, no_resume) ->
#   (verdict, action, reasons that must be present, whether a PUT is sent)
PAUSE_MATRIX = [
    # An operator pause is report-only by default, whatever else drifted.
    (
        {"enabled": False},
        False,
        False,
        ("disabled", "none", {"disabled", "webhook_disabled_by_operator"}, False),
    ),
    (
        {"enabled": None},
        False,
        False,
        ("disabled", "none", {"disabled", "webhook_disabled_by_operator"}, False),
    ),
    (
        {"enabled": False, "events": ["PAYMENT_RECEIVED"]},
        False,
        False,
        (
            "disabled",
            "none",
            {"disabled", "events", "webhook_disabled_by_operator"},
            False,
        ),
    ),
    (
        {"enabled": False, "interrupted": True},
        False,
        False,
        (
            "disabled",
            "none",
            {"disabled", "interrupted", "webhook_disabled_by_operator"},
            False,
        ),
    ),
    # --allow-enable re-enables (and, being a PUT, also resumes the queue).
    (
        {"enabled": False},
        True,
        False,
        ("ready", "updated", {"webhook_reenabled", "token_rewritten"}, True),
    ),
    (
        {"enabled": False, "events": ["PAYMENT_RECEIVED"]},
        True,
        False,
        ("ready", "updated", {"webhook_reenabled", "token_rewritten"}, True),
    ),
    (
        {"enabled": False, "interrupted": True},
        True,
        False,
        (
            "ready",
            "updated",
            {"webhook_reenabled", "queue_resumed", "token_rewritten"},
            True,
        ),
    ),
    # An interrupted queue is resumed by default and reported.
    (
        {"interrupted": True},
        False,
        False,
        ("ready", "updated", {"queue_resumed", "token_rewritten"}, True),
    ),
    (
        {"interrupted": True, "events": ["PAYMENT_RECEIVED"]},
        False,
        False,
        ("ready", "updated", {"queue_resumed", "token_rewritten"}, True),
    ),
    # --no-resume makes it report-only, other drift included.
    (
        {"interrupted": True},
        False,
        True,
        ("interrupted", "none", {"interrupted", "queue_not_resumed"}, False),
    ),
    (
        {"interrupted": True, "events": ["PAYMENT_RECEIVED"]},
        False,
        True,
        (
            "interrupted",
            "none",
            {"interrupted", "events", "queue_not_resumed"},
            False,
        ),
    ),
    # Both permissions are needed when both apply; the refusal wins.
    (
        {"enabled": False, "interrupted": True},
        True,
        True,
        ("disabled", "none", {"disabled", "interrupted", "queue_not_resumed"}, False),
    ),
    # Flags that do not apply change nothing.
    (
        {"events": ["PAYMENT_RECEIVED"]},
        False,
        True,
        ("ready", "updated", {"token_rewritten"}, True),
    ),
    ({}, True, True, ("ready", "none", set(), False)),
]


@pytest.mark.parametrize(
    ("overrides", "allow_enable", "no_resume", "expected"), PAUSE_MATRIX
)
async def test_reconcile_respects_operator_pause_and_queue_permissions(
    overrides: dict[str, Any],
    allow_enable: bool,
    no_resume: bool,
    expected: tuple[str, str, set[str], bool],
) -> None:
    verdict, action, reasons, puts = expected
    fake = FakeAsaas([webhook(**overrides)])
    result = await run(fake, allow_enable=allow_enable, no_resume=no_resume)
    assert (result.verdict, result.action) == (verdict, action)
    assert result.exit_code == cli.EXIT_CODES[verdict]
    assert reasons <= set(result.reasons)
    assert ("put" in fake.ops()) is puts
    assert fake.ops().count("put") == int(puts)
    if not puts:
        assert fake.bodies == [] and result.counts["writes"] == 0
    assert result.webhook_id_sha256 == _sha(OWNED_ID)


@pytest.mark.parametrize("force", [False, True])
async def test_a_paused_webhook_is_not_rewritten_even_when_forced(force: bool) -> None:
    fake = FakeAsaas([webhook(enabled=False)])
    result = await run(fake, force=force)
    assert (result.verdict, result.action) == ("disabled", "none")
    assert "webhook_disabled_by_operator" in result.reasons
    assert fake.bodies == []


async def test_force_rewrite_token_with_other_drift_still_reports_the_rewrite() -> None:
    fake = FakeAsaas([webhook(sendType="NON_SEQUENTIALLY")])
    result = await run(fake, force=True)
    assert (result.verdict, result.action) == ("ready", "updated")
    assert result.reasons == ("token_rewritten",)
    assert fake.ops().count("put") == 1


@pytest.mark.parametrize("force", [False, True])
async def test_creation_only_drift_is_never_written(force: bool) -> None:
    fake = FakeAsaas([webhook(apiVersion=2, sendType="X")])
    result = await run(fake, force=force)
    assert (result.verdict, result.action, result.exit_code) == ("drift", "none", 11)
    assert "api_version" in result.reasons and "requires_recreate" in result.reasons
    assert set(fake.methods()) == {"GET"}


async def test_force_rewrite_token_puts_an_exact_webhook_once() -> None:
    fake = FakeAsaas([webhook()])
    result = await run(fake, force=True)
    assert (result.verdict, result.action) == ("ready", "updated")
    assert result.reasons == ("token_rewritten",)
    assert [m for m, _ in fake.bodies] == ["PUT"]
    assert fake.bodies[0][1]["authToken"] == TOKEN


async def test_force_rewrite_token_on_an_absent_webhook_just_creates() -> None:
    fake = FakeAsaas([])
    result = await run(fake, force=True)
    assert (result.verdict, result.action, result.reasons) == ("ready", "created", ())


# ---------------------------------------------------------------------------
# Ambiguity: never written
# ---------------------------------------------------------------------------

AMBIGUOUS = [
    ([webhook(), webhook(OTHER_ID)], "multiple_owned"),
    (
        [webhook(), webhook(OTHER_ID, name="legacy", url=URL)],
        "multiple_owned",
    ),
    (
        [
            webhook(url="https://old-host.example/v1/billing/webhook/asaas"),
            webhook(OTHER_ID, name="legacy"),
        ],
        "multiple_owned",
    ),
    ([webhook(OTHER_ID, name="someone-else")], "foreign_name_on_url"),
    ([webhook(OTHER_ID, name=None)], "foreign_name_on_url"),
]


@pytest.mark.parametrize(("hooks", "reason"), AMBIGUOUS)
@pytest.mark.parametrize("command", ["readback", "reconcile", "smoke"])
async def test_ambiguous_ownership_is_reported_and_never_written(
    hooks: list[dict[str, Any]], reason: str, command: str
) -> None:
    fake = FakeAsaas([foreign(1), *hooks])
    result = await run(fake, command, force=command == "reconcile")
    assert (result.verdict, result.action, result.exit_code) == (
        "ambiguous",
        "none",
        20,
    )
    assert reason in result.reasons
    assert fake.ops() == ["list"]


async def test_exact_read_with_a_foreign_name_is_refused() -> None:
    """The list said ours; the authoritative GET says another name: no write."""

    fake = FakeAsaas([webhook(enabled=False)])
    fake.script("get", lambda r: httpx.Response(200, json=webhook(name="renamed")))
    result = await run(fake)
    assert (result.verdict, result.reasons) == ("ambiguous", ("foreign_name_on_url",))
    assert fake.bodies == []


# ---------------------------------------------------------------------------
# Configuration: fail closed before any network call
# ---------------------------------------------------------------------------

BAD_CONFIGS = [
    ({"webhook_token": SecretStr("")}, "webhook_token_missing"),
    ({"webhook_token": SecretStr("x" * 31)}, "webhook_token_invalid"),
    ({"webhook_token": SecretStr("x" * 256)}, "webhook_token_invalid"),
    ({"webhook_token": SecretStr("x" * 20 + " " + "x" * 20)}, "webhook_token_invalid"),
    ({"webhook_token": SecretStr("x" * 40 + "é")}, "webhook_token_invalid"),
    ({"api_key": SecretStr("")}, "api_key_missing"),
    ({"api_key": SecretStr(SANDBOX_KEY)}, "api_key_invalid"),
    ({"api_key": SecretStr("$aact_YTU5YTE0M2M2N2I4MTIxNzlkMjM")}, "api_key_invalid"),
    ({"api_key": SecretStr(KEY + " ")}, "api_key_invalid"),
    ({"webhook_url": ""}, "webhook_url_missing"),
    (
        {"webhook_url": "http://canary-url-host.example/v1/billing/webhook/asaas"},
        "webhook_url_invalid",
    ),
    ({"webhook_url": "https://canary-url-host.example/other"}, "webhook_url_invalid"),
    # The host allowlist: anything but the exact decided host is refused.
    ({"webhook_url": FOREIGN_HOST_URL}, "webhook_host_not_allowed"),
    (
        {
            "webhook_url": "https://STAGING.api.neurostack.global/v1/billing/webhook/asaas"
        },
        "webhook_host_not_allowed",
    ),
    (
        {
            "webhook_url": (
                "https://staging.api.neurostack.global:443/v1/billing/webhook/asaas"
            )
        },
        "webhook_host_not_allowed",
    ),
    (
        {
            "webhook_url": "https://payments.staging.api.neurostack.global/v1/billing/webhook/asaas"
        },
        "webhook_host_not_allowed",
    ),
    # The URL pin: the decided host with any other path is refused too.
    (
        {
            "webhook_url": (
                "https://staging.api.neurostack.global/x/v1/billing/webhook/asaas"
            )
        },
        "webhook_url_not_allowed",
    ),
    (
        {
            "webhook_url": (
                "https://staging.api.neurostack.global/v1/v1/billing/webhook/asaas"
            )
        },
        "webhook_url_not_allowed",
    ),
    ({"notification_email": ""}, "notification_email_missing"),
    ({"notification_email": "canary-mailbox at example"}, "notification_email_invalid"),
    ({"environment": "development"}, "environment_not_deployable"),
    ({"environment": "test"}, "environment_not_deployable"),
    ({"user_agent": "no-slash"}, "user_agent_invalid"),
]


@pytest.mark.parametrize(("overrides", "reason"), BAD_CONFIGS)
@pytest.mark.parametrize("command", ["check-config", "readback", "reconcile", "smoke"])
async def test_invalid_configuration_fails_closed_before_any_request(
    overrides: dict[str, Any], reason: str, command: str
) -> None:
    fake = FakeAsaas([webhook()])
    result = await run(fake, command, config=make_config(**overrides))
    assert (result.verdict, result.exit_code) == ("unconfigured", 30)
    assert result.reasons == (reason,)
    assert fake.seen == []
    assert result.counts["requests"] == 0


@pytest.mark.parametrize("length", [32, 255])
async def test_token_length_boundaries_are_accepted(length: int) -> None:
    fake = FakeAsaas([webhook()])
    result = await run(
        fake, "readback", config=make_config(webhook_token=SecretStr("y" * length))
    )
    assert result.verdict == "ready"


async def test_check_config_prints_only_booleans_and_makes_no_request() -> None:
    fake = FakeAsaas([webhook()])
    result = await run(fake, "check-config")
    assert (result.verdict, result.exit_code, result.reasons) == ("ready", 0, ())
    assert result.checks == {
        "settings_loaded": True,
        "imports_ok": True,
        "api_key_production_namespace": True,
        "webhook_token_valid": True,
        "webhook_url_valid": True,
        "webhook_host_allowed": True,
        "webhook_url_allowed": True,
        "notification_email_valid": True,
        "environment_deployable": True,
        "user_agent_valid": True,
    }
    assert all(isinstance(value, bool) for value in result.checks.values())
    assert fake.seen == []


def _decided_webhook_url() -> str:
    """``ASAAS_WEBHOOK_URL`` exactly as the reviewed D3 parameters file has it."""

    params = (ROOT / "infra/production/asaas-production-wiring.params.yaml").read_text(
        encoding="utf-8"
    )
    (line,) = [
        line
        for line in params.splitlines()
        if line.strip().startswith("ASAAS_WEBHOOK_URL:")
    ]
    return line.split(":", 1)[1].strip()


def test_the_url_pin_is_exactly_the_decided_d3_parameter() -> None:
    """A deliberate safety net: change it with the D3 parameters, never alone."""

    decided = _decided_webhook_url()
    assert cli.ALLOWED_WEBHOOK_URLS == frozenset({decided})
    assert cli.ALLOWED_WEBHOOK_HOSTS == frozenset({"staging.api.neurostack.global"})
    assert URL == decided
    checks, reasons = cli.validate_config(make_config(webhook_url=decided))
    assert checks["webhook_url_allowed"] and checks["webhook_host_allowed"]
    assert reasons == ()


async def test_the_decided_host_with_another_path_fails_closed() -> None:
    other = "https://staging.api.neurostack.global/x/v1/billing/webhook/asaas"
    fake = FakeAsaas([webhook(url=other)])
    result = await run(fake, config=make_config(webhook_url=other))
    assert (result.verdict, result.reasons) == (
        "unconfigured",
        ("webhook_url_not_allowed",),
    )
    assert result.checks["webhook_host_allowed"] is True
    assert result.checks["webhook_url_allowed"] is False
    assert fake.seen == []


async def test_a_foreign_host_fails_closed_with_the_url_otherwise_valid() -> None:
    fake = FakeAsaas([webhook(url=FOREIGN_HOST_URL)])
    result = await run(fake, config=make_config(webhook_url=FOREIGN_HOST_URL))
    assert (result.verdict, result.reasons) == (
        "unconfigured",
        ("webhook_host_not_allowed",),
    )
    assert result.checks["webhook_url_valid"] is True
    assert result.checks["webhook_host_allowed"] is False
    assert fake.seen == []


def test_user_agent_is_checked_by_the_settings_validator_without_a_key() -> None:
    source = Path(cli.__file__).read_text(encoding="utf-8")
    assert "PRODUCTION_KEY_PREFIX +" not in source
    assert "Settings.validate_asaas_user_agent(config.user_agent)" in source
    for agent, valid in [("NeuroStack/x", True), ("no-slash", False), (" ", False)]:
        checks, _ = cli.validate_config(make_config(user_agent=agent))
        assert checks["user_agent_valid"] is valid


def test_fake_keys_are_exactly_what_the_validators_accept_and_reject() -> None:
    """The bare namespaces are valid fakes: only the prefix is checked."""

    assert KEY == "$aact_prod_" and SANDBOX_KEY == "$aact_hmlg_"
    # Production namespace: accepted by the settings, the client and this CLI.
    assert make_settings().asaas_api_key.get_secret_value() == KEY
    AsaasClient(api_key=SecretStr(KEY), user_agent="NeuroStack/test")
    checks, reasons = cli.validate_config(make_config())
    assert checks["api_key_production_namespace"] is True and reasons == ()
    # Sandbox namespace: refused by the settings field validator, the client
    # and this CLI, without echoing the value.
    with pytest.raises(ValidationError) as caught:
        make_settings(asaas_api_key=SecretStr(SANDBOX_KEY))
    errors = caught.value.errors(include_url=False)
    assert [(e["loc"], e["type"]) for e in errors] == [
        (("asaas_api_key",), "value_error")
    ]
    _assert_no_canary(str(caught.value), repr(caught.value))
    with pytest.raises(cli.AsaasConfigurationError):
        AsaasClient(api_key=SecretStr(SANDBOX_KEY), user_agent="NeuroStack/test")
    checks, reasons = cli.validate_config(make_config(api_key=SecretStr(SANDBOX_KEY)))
    assert checks["api_key_production_namespace"] is False
    assert reasons == ("api_key_invalid",)


def test_unloadable_settings_print_one_unconfigured_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError) as caught:
        make_settings(asaas_api_key=SecretStr(SANDBOX_KEY))
    monkeypatch.setattr(cli, "_IMPORT_ERROR", caught.value)
    code, out, err = invoke(["reconcile"])
    document = json.loads(out)
    assert code == 30
    assert document["verdict"] == "unconfigured"
    assert document["reasons"] == ["settings_invalid:asaas_api_key"]
    assert document["checks"]["settings_loaded"] is False
    _assert_no_canary(out, err)


def test_a_broken_image_import_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_IMPORT_ERROR", ImportError("no module named canary"))
    code, out, _err = invoke(["check-config"])
    assert code == 40
    assert json.loads(out)["reasons"] == ["import_failed"]
    assert "canary" not in out


# ---------------------------------------------------------------------------
# Provider failures: auth, transient retries, invalid responses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
@pytest.mark.parametrize("op", ["list", "get"])
async def test_auth_failure_is_an_error_without_retry(status: int, op: str) -> None:
    fake = FakeAsaas([webhook(enabled=False)]).script(op, status)
    sleeps: list[float] = []
    result = await run(fake, sleeps=sleeps)
    assert (result.verdict, result.reasons, result.exit_code) == (
        "error",
        ("auth",),
        40,
    )
    assert sleeps == [] and result.counts["retries"] == 0
    assert fake.bodies == []


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failure_on_a_write_is_an_error_without_reread(status: int) -> None:
    fake = FakeAsaas([]).script("post", status)
    result = await run(fake)
    assert (result.verdict, result.action) == ("error", "none")
    assert "auth" in result.reasons
    assert fake.ops() == ["list", "post"]
    fake = FakeAsaas([webhook(sendType="X")]).script("put", status)
    result = await run(fake)
    assert "auth" in result.reasons and fake.ops() == ["list", "get", "put"]


@pytest.mark.parametrize(
    "failure", [429, 500, 502, 503, 504, 408, "timeout", "connect"]
)
async def test_transient_reads_retry_three_times_then_error(failure: Step) -> None:
    fake = FakeAsaas([]).script("list", *[failure] * 4)
    sleeps: list[float] = []
    result = await run(fake, sleeps=sleeps)
    assert (result.verdict, result.reasons, result.exit_code) == (
        "error",
        ("transient",),
        40,
    )
    assert fake.ops() == ["list"] * 4
    assert len(sleeps) == 3 and result.counts["retries"] == 3
    for attempt, delay in enumerate(sleeps):
        assert 0.5 * 2**attempt <= delay <= 2**attempt
    assert fake.bodies == []  # never escalates to a write


async def test_transient_reads_recover_within_the_retry_budget() -> None:
    fake = FakeAsaas([webhook()]).script("list", 503, "timeout")
    fake.script("get", 429)
    result = await run(fake, "smoke")
    assert result.verdict == "ready"
    assert result.counts["retries"] == 3


async def test_backoff_is_capped() -> None:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    retrier = cli.Retrier(sleep=sleep, rng=random.Random(3), retries=8)

    async def always() -> None:
        raise cli.AsaasOutcomeUnknown("asaas_transport_unknown")

    with pytest.raises(cli.AsaasOutcomeUnknown):
        await retrier.run(always, retry_if=cli.is_transient)
    assert len(sleeps) == 8
    assert max(sleeps) <= cli.BACKOFF_CAP_SECONDS


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"data": [], "hasMore": "no"}),
        httpx.Response(302, headers={"Location": "https://elsewhere.example"}),
        httpx.Response(200, json={"data": [{"id": "bad id!"}], "hasMore": False}),
    ],
)
async def test_invalid_list_responses_are_errors_without_retry_or_write(
    response: httpx.Response,
) -> None:
    fake = FakeAsaas([]).script("list", lambda r: response)
    result = await run(fake)
    assert (result.verdict, result.reasons) == ("error", ("invalid_response",))
    assert fake.ops() == ["list"]


async def test_vanished_webhook_on_exact_read_is_an_error() -> None:
    fake = FakeAsaas([webhook(enabled=False)]).script("get", 404)
    result = await run(fake)
    assert (result.verdict, result.reasons) == ("error", ("not_found",))
    assert fake.bodies == []


async def test_overall_deadline_bounds_a_hanging_provider() -> None:
    async def hang(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        raise AssertionError("unreachable")

    async def no_sleep(_seconds: float) -> None:
        return None

    result = await cli.run(
        "reconcile",
        make_config(),
        transport=httpx.MockTransport(hang),
        sleep=no_sleep,
        rng=random.Random(1),
        deadline_seconds=0.05,
    )
    assert (result.verdict, result.exit_code) == ("error", 40)
    assert result.reasons == ("transient", "deadline_exceeded")


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


async def test_pagination_reads_all_three_pages_and_finds_the_owned_webhook() -> None:
    fake = FakeAsaas([*[foreign(i) for i in range(24)], webhook()])
    result = await run(fake, "readback")
    assert result.verdict == "ready"
    lists = [query for method, path, query in fake.seen if path == "/v3/webhooks"]
    assert lists == ["offset=0&limit=10", "offset=10&limit=10", "offset=20&limit=10"]
    assert result.counts["webhooks_listed"] == 25


async def test_pagination_detects_a_duplicate_on_the_last_page() -> None:
    fake = FakeAsaas([webhook(), *[foreign(i) for i in range(23)], webhook(OTHER_ID)])
    result = await run(fake)
    assert (result.verdict, result.reasons) == ("ambiguous", ("multiple_owned",))
    assert fake.bodies == []


async def test_pagination_absent_across_three_pages_creates_once() -> None:
    fake = FakeAsaas([foreign(i) for i in range(25)])
    result = await run(fake)
    assert (result.verdict, result.action) == ("ready", "created")
    assert fake.ops().count("post") == 1
    assert fake.ops().count("list") == 6  # 3 pages before, 3 pages after


async def test_a_failing_middle_page_restarts_the_listing() -> None:
    fake = FakeAsaas([*[foreign(i) for i in range(24)], webhook()])
    pages = iter([503])

    def second_page_fails_once(request: httpx.Request) -> Any:
        if request.url.params["offset"] == "10":
            status = next(pages, None)
            return _error(status) if status else None
        return None

    fake.script("list", *[second_page_fails_once] * 6)
    result = await run(fake, "readback")
    assert result.verdict == "ready"
    assert result.counts["retries"] == 1


# ---------------------------------------------------------------------------
# Writes: duplicate creation, unknown outcomes, update failures
# ---------------------------------------------------------------------------


async def test_post_rejected_as_duplicate_rereads_and_converges() -> None:
    """Another run created it between our list and our POST."""

    fake = FakeAsaas([], reject_duplicates=True)

    def concurrent_creation(request: httpx.Request) -> None:
        body = json.loads(request.content)
        fake.apply_create(body, OTHER_ID)
        return None  # the default handler now rejects the duplicate (400)

    fake.script("post", concurrent_creation)
    result = await run(fake)
    assert (result.verdict, result.action, result.exit_code) == ("ready", "none", 0)
    assert result.reasons == ("create_rejected",)
    assert fake.ops() == ["list", "post", "list", "get", "list"]
    assert fake.ops().count("post") == 1 and len(fake.hooks) == 1
    assert result.webhook_id_sha256 == _sha(OTHER_ID)


async def test_post_rejected_with_nothing_found_is_an_error() -> None:
    fake = FakeAsaas([]).script("post", 400)
    result = await run(fake)
    assert (result.verdict, result.reasons) == ("error", ("create_rejected",))
    assert fake.ops() == ["list", "post", "list"]


@pytest.mark.parametrize("failure", ["timeout", 500, 502])
async def test_post_with_unknown_outcome_is_never_resent(failure: Step) -> None:
    fake = FakeAsaas([]).script("post", failure)
    sleeps: list[float] = []
    result = await run(fake, sleeps=sleeps)
    assert result.verdict == "error"
    assert set(result.reasons) == {"transient", "create_unconfirmed"}
    assert fake.ops().count("post") == 1
    assert fake.ops() == ["list", "post", "list"]


async def test_post_applied_but_unanswered_is_found_by_reread() -> None:
    fake = FakeAsaas([])

    def applied_then_lost(request: httpx.Request) -> None:
        fake.apply_create(json.loads(request.content), OTHER_ID)
        raise httpx.ReadTimeout("lost", request=request)

    fake.script("post", applied_then_lost)
    result = await run(fake)
    assert (result.verdict, result.action) == ("ready", "none")
    assert result.reasons == ("create_unconfirmed",)
    assert fake.ops().count("post") == 1 and len(fake.hooks) == 1


@pytest.mark.parametrize("failure", [429, "connect"])
async def test_post_that_was_not_sent_is_retried(failure: Step) -> None:
    fake = FakeAsaas([]).script("post", failure)
    result = await run(fake)
    assert (result.verdict, result.action) == ("ready", "created")
    assert fake.ops().count("post") == 2 and len(fake.hooks) == 1


async def test_post_never_sent_after_the_retry_budget_is_transient() -> None:
    fake = FakeAsaas([]).script("post", *[429] * 4)
    result = await run(fake)
    assert (result.verdict, result.reasons) == ("error", ("transient",))
    assert fake.ops().count("post") == 4 and fake.hooks == []


async def test_idempotent_put_is_retried_on_transient_failures() -> None:
    fake = FakeAsaas([webhook(sendType="X")]).script("put", 503, "timeout")
    result = await run(fake)
    assert (result.verdict, result.action) == ("ready", "updated")
    assert fake.ops().count("put") == 3


@pytest.mark.parametrize(
    ("failure", "reason"),
    [(404, "not_found"), (400, "provider_rejected"), (422, "provider_rejected")],
)
async def test_put_rejected_is_an_error(failure: int, reason: str) -> None:
    fake = FakeAsaas([webhook(sendType="X")]).script("put", failure)
    result = await run(fake)
    assert result.verdict == "error"
    assert set(result.reasons) == {reason, "update_failed"}
    assert fake.ops() == ["list", "get", "put"]


async def test_put_that_does_not_converge_reports_the_remaining_drift() -> None:
    fake = FakeAsaas([webhook(sendType="X")])
    fake.script("put", lambda r: httpx.Response(200, json=webhook(sendType="X")))
    result = await run(fake)
    assert (result.verdict, result.action, result.reasons) == (
        "drift",
        "updated",
        ("send_type", "token_rewritten"),
    )
    assert fake.ops().count("put") == 1


# ---------------------------------------------------------------------------
# Smoke and concurrency
# ---------------------------------------------------------------------------


async def test_smoke_rereads_and_detects_a_duplicate_appearing() -> None:
    fake = FakeAsaas([webhook()])
    calls = {"n": 0}

    def second_listing_has_a_twin(request: httpx.Request) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            fake.hooks.append(webhook(OTHER_ID))
        return None

    fake.script("list", second_listing_has_a_twin, second_listing_has_a_twin)
    result = await run(fake, "smoke")
    assert result.verdict == "ambiguous"
    assert set(result.reasons) == {"multiple_owned", "duplicate_detected"}


async def test_smoke_detects_the_webhook_vanishing_between_reads() -> None:
    fake = FakeAsaas([webhook()])
    calls = {"n": 0}

    def second_listing_is_empty(request: httpx.Request) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            fake.hooks.clear()
        return None

    fake.script("list", second_listing_is_empty, second_listing_is_empty)
    result = await run(fake, "smoke")
    assert (result.verdict, result.reasons) == ("error", ("listing_inconsistent",))


async def test_smoke_of_an_exact_webhook_is_ready_and_read_only() -> None:
    fake = FakeAsaas([webhook()])
    result = await run(fake, "smoke")
    assert (result.verdict, result.action, result.exit_code) == ("ready", "none", 0)
    assert fake.ops() == ["list", "get", "list"]


class _Barrier:
    """Force the worst interleaving of two runs.

    Both runs receive their (empty) listing before either POSTs, and both
    POSTs reach the provider before either run reads back: exactly the race
    a lock-free registrar cannot prevent.  Each response is produced first
    and then held at the gate, so neither snapshot sees the other's write.
    """

    def __init__(self, fake: FakeAsaas, parties: int = 2) -> None:
        self.fake = fake
        self.parties = parties
        self.arrived = {"list": 0, "post": 0}
        self.released = {"list": asyncio.Event(), "post": asyncio.Event()}

    async def _gate(self, op: str) -> None:
        event = self.released[op]
        if event.is_set():
            return
        self.arrived[op] += 1
        if self.arrived[op] >= self.parties:
            event.set()
        await event.wait()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        op = FakeAsaas._op(request.method, request.url.path)
        response = self.fake.handle(request)
        if op in self.released:
            await self._gate(op)
        return response


async def _concurrent_reconciles(fake: FakeAsaas) -> list[cli.RunResult]:
    barrier = _Barrier(fake)

    async def no_sleep(_seconds: float) -> None:
        return None

    async def one() -> cli.RunResult:
        return await cli.run(
            "reconcile",
            make_config(),
            transport=httpx.MockTransport(barrier.handle),
            sleep=no_sleep,
            rng=random.Random(5),
            deadline_seconds=30,
        )

    return list(await asyncio.gather(one(), one()))


async def test_concurrent_runs_converge_when_asaas_rejects_the_duplicate() -> None:
    fake = FakeAsaas([], reject_duplicates=True)
    results = await _concurrent_reconciles(fake)
    assert [r.verdict for r in results] == ["ready", "ready"]
    assert sorted(r.action for r in results) == ["created", "none"]
    assert len(fake.hooks) == 1
    assert {r.webhook_id_sha256 for r in results} == {_sha(fake.hooks[0]["id"])}


async def test_concurrent_runs_never_hide_a_duplicate_asaas_accepted() -> None:
    fake = FakeAsaas([], reject_duplicates=False)
    results = await _concurrent_reconciles(fake)
    assert len(fake.hooks) == 2  # the provider accepted both POSTs
    assert [r.verdict for r in results] == ["ambiguous", "ambiguous"]
    for result in results:
        assert result.action == "created"
        assert set(result.reasons) == {"multiple_owned", "duplicate_detected"}
        assert result.exit_code == 20
    # And every later run refuses to touch them.
    later = await run(fake, force=True)
    assert (later.verdict, later.action) == ("ambiguous", "none")


# ---------------------------------------------------------------------------
# No DELETE anywhere; read-only commands cannot write
# ---------------------------------------------------------------------------


def test_webhook_client_surface_and_cli_never_issue_delete() -> None:
    for method in (
        AsaasClient.list_webhooks,
        AsaasClient.get_webhook,
        AsaasClient.create_webhook,
        AsaasClient.update_webhook,
        AsaasClient._pages,
    ):
        source = inspect.getsource(method)
        assert "DELETE" not in source and "delete" not in source.lower()
    tree = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert node.value.strip().upper() != "DELETE"
        if isinstance(node, ast.Attribute):
            assert "delete" not in node.attr.lower()
        if isinstance(node, ast.Name):
            assert "delete" not in node.id.lower()


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("DELETE", "https://api.asaas.com/v3/webhooks/canaryWebhookId0001"),
        ("DELETE", "https://api.asaas.com/v3/webhooks"),
        ("PATCH", "https://api.asaas.com/v3/webhooks/canaryWebhookId0001"),
        ("GET", "https://api.asaas.com/v3/payments"),
        ("POST", "https://api.asaas.com/v3/checkouts"),
        ("POST", "https://api.asaas.com/v3/webhooks/canaryWebhookId0001"),
        ("PUT", "https://api.asaas.com/v3/webhooks"),
        ("GET", "https://evil.example/v3/webhooks"),
        ("GET", "http://api.asaas.com/v3/webhooks"),
        ("GET", "https://api.asaas.com:8443/v3/webhooks"),
    ],
)
async def test_guard_refuses_everything_outside_the_webhook_allowlist(
    method: str, url: str
) -> None:
    inner = FakeAsaas([webhook()])
    guard = cli.GuardedTransport(inner.transport, allow_writes=True)
    with pytest.raises(cli.GuardRefused):
        await guard.handle_async_request(httpx.Request(method, url))
    assert inner.seen == []


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", "https://api.asaas.com/v3/webhooks"),
        ("PUT", "https://api.asaas.com/v3/webhooks/canaryWebhookId0001"),
    ],
)
async def test_read_only_guard_refuses_writes(method: str, url: str) -> None:
    inner = FakeAsaas([webhook()])
    guard = cli.GuardedTransport(inner.transport, allow_writes=False)
    with pytest.raises(cli.GuardRefused):
        await guard.handle_async_request(httpx.Request(method, url, content=b"{}"))
    assert inner.seen == []


async def test_guard_caps_write_sends_per_run() -> None:
    inner = FakeAsaas([])
    guard = cli.GuardedTransport(inner.transport, allow_writes=True, max_write_sends=2)
    body = json.dumps(
        {
            "name": NAME,
            "url": URL,
            "email": EMAIL,
            "enabled": True,
            "interrupted": False,
            "apiVersion": 3,
            "sendType": "SEQUENTIALLY",
            "events": ALL_EVENTS,
            "authToken": TOKEN,
        }
    ).encode()
    request = httpx.Request(
        "POST",
        "https://api.asaas.com/v3/webhooks",
        content=body,
        headers={"access_token": KEY},
    )
    await guard.handle_async_request(request)
    await guard.handle_async_request(request)
    with pytest.raises(cli.GuardRefused):
        await guard.handle_async_request(request)
    assert inner.ops() == ["post", "post"]


@pytest.mark.parametrize("command", ["readback", "smoke"])
async def test_read_only_commands_never_write_even_on_drift(command: str) -> None:
    fake = FakeAsaas([webhook(enabled=False, hasAuthToken=False)])
    result = await run(fake, command)
    assert result.verdict == "disabled"
    assert set(fake.methods()) == {"GET"}
    assert result.counts["writes"] == 0


# ---------------------------------------------------------------------------
# Agreement with the in-process readiness refresher
# ---------------------------------------------------------------------------

STATES = [
    [],
    [webhook()],
    [foreign(1), webhook()],
    [webhook(enabled=False)],
    [webhook(hasAuthToken=False)],
    [webhook(), webhook(OTHER_ID)],
    [webhook(OTHER_ID, name="someone-else")],
    [webhook(url="https://old-host.example/v1/billing/webhook/asaas")],
    [webhook(apiVersion=2)],
]


@pytest.mark.parametrize("hooks", STATES)
async def test_cli_ready_matches_the_refresher_readiness_predicate(
    hooks: list[dict[str, Any]],
) -> None:
    fake = FakeAsaas([dict(h) for h in hooks])
    result = await run(fake, "readback")
    config = make_config()
    refresher = await AsaasWebhookProvisioner(
        AsaasClient(
            api_key=config.api_key,
            user_agent=config.user_agent,
            transport=fake.transport,
        ),
        desired_webhook_settings(
            environment=config.environment,
            url=config.webhook_url,
            email=config.notification_email,
        ),
        auth_token=config.webhook_token,
    ).readback()
    assert (result.verdict == "ready") is refresher.ready


# ---------------------------------------------------------------------------
# Output contract: one JSON line, exit codes, no secrets anywhere
# ---------------------------------------------------------------------------

DOCUMENT_KEYS = {
    "schema",
    "command",
    "verdict",
    "action",
    "reasons",
    "webhook_id_sha256",
    "counts",
    "checks",
    "force_rewrite_token",
    "allow_enable",
    "no_resume",
    "exit_code",
}


def test_exit_codes_are_distinct_and_zero_only_for_ready() -> None:
    assert cli.EXIT_CODES == {
        "ready": 0,
        "absent": 10,
        "drift": 11,
        "interrupted": 12,
        "disabled": 13,
        "ambiguous": 20,
        "unconfigured": 30,
        "error": 40,
    }
    assert len(set(cli.EXIT_CODES.values())) == len(cli.EXIT_CODES)


SCENARIOS: list[tuple[list[str], Callable[[], FakeAsaas], str]] = [
    (["reconcile"], lambda: FakeAsaas([]), "ready"),
    (["reconcile", "--force-rewrite-token"], lambda: FakeAsaas([webhook()]), "ready"),
    (["reconcile"], lambda: FakeAsaas([webhook(sendType="X")]), "ready"),
    (["reconcile"], lambda: FakeAsaas([webhook(enabled=False)]), "disabled"),
    (
        ["reconcile", "--allow-enable"],
        lambda: FakeAsaas([webhook(enabled=False)]),
        "ready",
    ),
    (
        ["reconcile", "--no-resume"],
        lambda: FakeAsaas([webhook(interrupted=True)]),
        "interrupted",
    ),
    (["readback"], lambda: FakeAsaas([webhook(sendType="X")]), "drift"),
    ([], lambda: FakeAsaas([webhook(interrupted=True)]), "interrupted"),
    (["smoke"], lambda: FakeAsaas([webhook(), webhook(OTHER_ID)]), "ambiguous"),
    (["readback"], lambda: FakeAsaas([]), "absent"),
    (["reconcile"], lambda: FakeAsaas([]).script("list", 401), "error"),
    (["reconcile"], lambda: FakeAsaas([]).script("list", *[503] * 4), "error"),
    (["reconcile"], lambda: FakeAsaas([]).script("post", "timeout"), "error"),
    (["check-config"], lambda: FakeAsaas([]), "ready"),
]


@pytest.mark.parametrize(("argv", "fake_factory", "verdict"), SCENARIOS)
def test_main_prints_exactly_one_json_line_and_no_secret(
    argv: list[str],
    fake_factory: Callable[[], FakeAsaas],
    verdict: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake = fake_factory()
    code, out, err = invoke(argv, fake=fake)
    lines = out.splitlines()
    assert len(lines) == 1 and out.endswith("\n")
    document = json.loads(lines[0])
    assert set(document) == DOCUMENT_KEYS
    assert document["schema"] == "neurostack.asaas_webhook_registration.v1"
    assert document["verdict"] == verdict
    assert code == document["exit_code"] == cli.EXIT_CODES[verdict]
    assert all(isinstance(v, bool) for v in document["checks"].values())
    assert all(isinstance(v, int) for v in document["counts"].values())
    (log_line,) = err.splitlines()
    log = json.loads(log_line)
    assert log["event"] == "asaas_webhook_registration"
    assert {k: log[k] for k in DOCUMENT_KEYS} == document
    _assert_no_canary(out, err, caplog.text)
    for record in caplog.records:
        _assert_no_canary(record.getMessage(), str(record.args))
    digest = document["webhook_id_sha256"]
    assert digest is None or (
        len(digest) == 64 and set(digest) <= set("0123456789abcdef")
    )


def test_main_silences_httpx_request_logs_that_carry_the_webhook_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """httpx logs ``GET .../v3/webhooks/<id>`` at INFO when its logger allows it."""

    httpx_logger = logging.getLogger("httpx")
    original = httpx_logger.level
    httpx_logger.setLevel(logging.DEBUG)
    caplog.set_level(logging.DEBUG)
    try:
        code, out, err = invoke(["reconcile"], fake=FakeAsaas([webhook()]))
        assert httpx_logger.getEffectiveLevel() >= logging.WARNING
    finally:
        httpx_logger.setLevel(original)
    assert code == 0
    _assert_no_canary(out, err, caplog.text)


def test_main_defaults_to_read_only_readback() -> None:
    fake = FakeAsaas([webhook(enabled=False)])
    code, out, _err = invoke([], fake=fake)
    assert json.loads(out)["command"] == "readback"
    assert code == 13 and set(fake.methods()) == {"GET"}


@pytest.mark.parametrize(
    "argv",
    [
        ["-h"],
        ["--help"],
        ["delete"],
        ["readback", "--force-rewrite-token"],
        ["smoke", "--force-rewrite-token"],
        ["readback", "--allow-enable"],
        ["smoke", "--no-resume"],
        ["check-config", "--allow-enable"],
        ["reconcile", "--allow"],
        ["reconcile", "--no-res"],
        ["reconcile", "--deadline-seconds", "5"],
        ["reconcile", "--deadline-seconds", "601"],
        ["reconcile", "--deadline-seconds", "abc"],
        ["reconcile", "extra"],
        ["reconcile", "--apply"],
    ],
)
def test_usage_errors_are_unconfigured_and_never_exit_zero(argv: list[str]) -> None:
    fake = FakeAsaas([webhook()])
    code, out, err = invoke(argv, fake=fake)
    document = json.loads(out)
    assert code == 30
    assert (document["verdict"], document["reasons"]) == ("unconfigured", ["usage"])
    assert fake.seen == []
    assert len(err.splitlines()) == 1


def test_unexpected_failure_still_prints_one_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*_args: Any, **_kwargs: Any) -> cli.RunResult:
        raise RuntimeError("canary-mailbox@example.test leaked?")

    monkeypatch.setattr(cli, "run", boom)
    code, out, err = invoke(["reconcile"])
    document = json.loads(out)
    assert code == 40 and document["reasons"] == ["unexpected"]
    _assert_no_canary(out, err)


def test_reasons_are_a_closed_vocabulary() -> None:
    with pytest.raises(ValueError):
        cli.RunResult(command="readback", verdict="error", reasons=("free text",))
    with pytest.raises(ValueError):
        cli.RunResult(command="readback", verdict="nope")
    result = cli.RunResult(
        command="readback", verdict="error", reasons=("settings_invalid:asaas_api_key",)
    )
    assert result.exit_code == 40


def test_config_repr_never_shows_values() -> None:
    config = make_config()
    _assert_no_canary(repr(config), str(config))


# ---------------------------------------------------------------------------
# The real entry point, as the one-off ECS task runs it
# ---------------------------------------------------------------------------


def _subprocess(
    tmp_path: Path,
    argv: list[str],
    env: dict[str, str],
    *,
    dotenv: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """``python -m src.ops.asaas_webhook`` from a directory with no ``.env``.

    ``src`` is linked into an empty directory so the exact module invocation
    runs without PYTHONPATH and without reading any developer ``.env``;
    ``dotenv`` plants one there on purpose.
    """

    workdir = tmp_path / "app"
    workdir.mkdir()
    (workdir / "src").symlink_to(ROOT / "src", target_is_directory=True)
    if dotenv is not None:
        (workdir / ".env").write_text(dotenv, encoding="utf-8")
    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "ENVIRONMENT": "staging",
        "SECRET_KEY": "s" * 48,
    }
    return subprocess.run(
        [sys.executable, "-m", "src.ops.asaas_webhook", *argv],
        cwd=workdir,
        env={**base, **env},
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_entry_point_fails_closed_on_a_sandbox_key(tmp_path: Path) -> None:
    result = _subprocess(
        tmp_path,
        ["reconcile"],
        {
            "ASAAS_API_KEY": SANDBOX_KEY,
            "ASAAS_WEBHOOK_TOKEN": TOKEN,
            "ASAAS_WEBHOOK_URL": URL,
            "ASAAS_WEBHOOK_NOTIFICATION_EMAIL": EMAIL,
        },
    )
    assert result.returncode == 30, result.stderr[-2000:]
    (line,) = result.stdout.splitlines()
    document = json.loads(line)
    assert document["verdict"] == "unconfigured"
    assert document["reasons"] == ["settings_invalid:asaas_api_key"]
    assert len(result.stderr.splitlines()) == 1
    _assert_no_canary(result.stdout, result.stderr)


def test_entry_point_check_config_passes_without_network(tmp_path: Path) -> None:
    result = _subprocess(
        tmp_path,
        ["check-config"],
        {
            "ASAAS_API_KEY": KEY,
            "ASAAS_WEBHOOK_TOKEN": TOKEN,
            "ASAAS_WEBHOOK_URL": URL,
            "ASAAS_WEBHOOK_NOTIFICATION_EMAIL": EMAIL,
        },
    )
    assert result.returncode == 0, result.stderr[-2000:]
    (line,) = result.stdout.splitlines()
    document = json.loads(line)
    assert document["verdict"] == "ready"
    assert set(document["checks"].values()) == {True}
    _assert_no_canary(result.stdout, result.stderr)


def test_entry_point_short_token_never_reaches_the_network(tmp_path: Path) -> None:
    result = _subprocess(
        tmp_path,
        [],
        {
            "ASAAS_API_KEY": KEY,
            "ASAAS_WEBHOOK_TOKEN": "CANARYTOKEN-short",
            "ASAAS_WEBHOOK_URL": URL,
            "ASAAS_WEBHOOK_NOTIFICATION_EMAIL": EMAIL,
        },
    )
    assert result.returncode == 30, result.stderr[-2000:]
    document = json.loads(result.stdout)
    assert (document["command"], document["reasons"]) == (
        "readback",
        ["webhook_token_invalid"],
    )
    assert document["counts"]["requests"] == 0
    _assert_no_canary(result.stdout, result.stderr)


def test_production_image_ships_the_module() -> None:
    """The ``production`` stage copies ``src/`` whole; ``scripts/`` is not shipped.

    The stage is found by its alias, whatever image it builds ``FROM``.
    """

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    header = re.search(
        r"^FROM\s+\S+\s+AS\s+production\s*$", dockerfile, re.MULTILINE | re.IGNORECASE
    )
    assert header is not None, "Dockerfile has no production stage"
    production = re.split(r"^FROM\s", dockerfile[header.end() :], flags=re.MULTILINE)[0]
    assert "COPY --chown=neurostack:neurostack src ./src" in production
    assert (ROOT / "src/ops/__init__.py").is_file()
    assert (ROOT / "src/ops/asaas_webhook.py").is_file()


def test_entry_point_ignores_a_dotenv_in_the_working_directory(
    tmp_path: Path,
) -> None:
    """Only the process environment configures the webhook, never ``.env``."""

    result = _subprocess(
        tmp_path,
        ["check-config"],
        {
            "ASAAS_API_KEY": KEY,
            "ASAAS_WEBHOOK_TOKEN": TOKEN,
            "ASAAS_WEBHOOK_NOTIFICATION_EMAIL": EMAIL,
        },
        dotenv=(
            "ASAAS_WEBHOOK_URL="
            "https://canary-dotenv-host.example/v1/billing/webhook/asaas\n"
        ),
    )
    assert result.returncode == 30, result.stderr[-2000:]
    document = json.loads(result.stdout)
    assert document["reasons"] == ["webhook_url_missing"]
    assert "canary-dotenv-host" not in result.stdout + result.stderr


# ---------------------------------------------------------------------------
# Listing completeness: a provider that pages differently cannot hide ours
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["reconcile", "readback", "smoke"])
async def test_short_pages_that_still_say_has_more_are_refused(command: str) -> None:
    """Five items per page while ``hasMore`` is true (reviewer's probe).

    ``AsaasClient`` advances by ten, so items 5-9 of every page are never
    read: the owned webhook at index 7 is invisible, a naive run would POST
    a duplicate, and its final listing would hide that duplicate the same way.
    """

    hooks = [
        *[foreign(i) for i in range(7)],
        webhook(),
        *[foreign(i) for i in range(8, 25)],
    ]
    fake = FakeAsaas(hooks, page_cap=5)
    result = await run(fake, command)
    assert (result.verdict, result.reasons, result.exit_code) == (
        "error",
        ("listing_inconsistent",),
        40,
    )
    assert set(fake.ops()) == {"list"}
    assert result.counts["writes"] == 0 and fake.bodies == []


@pytest.mark.parametrize(
    ("total", "complete"),
    [
        ("auto", True),
        ("omit", True),
        (None, True),
        (26, False),
        (24, False),
        ("25", False),
        (True, False),
        (-1, False),
    ],
)
async def test_a_reported_total_count_must_equal_what_was_read(
    total: Any, complete: bool
) -> None:
    fake = FakeAsaas([*[foreign(i) for i in range(24)], webhook()], total_count=total)
    result = await run(fake)
    if complete:
        assert (result.verdict, result.action) == ("ready", "none")
    else:
        assert (result.verdict, result.reasons) == ("error", ("listing_inconsistent",))
    assert fake.bodies == []


@pytest.mark.parametrize("total", [3, "x"])
async def test_an_inconsistent_listing_never_leads_to_a_create(total: Any) -> None:
    fake = FakeAsaas([foreign(1)], total_count=total)
    result = await run(fake)
    assert (result.verdict, result.reasons) == ("error", ("listing_inconsistent",))
    assert "post" not in fake.ops() and result.counts["writes"] == 0


def test_listing_complete_rules() -> None:
    page = cli.ListingPage

    def full(offset: int, total: int | None = 25) -> cli.ListingPage:
        return page(offset, 10, 10, True, total, True)

    last = page(20, 10, 5, False, 25, True)
    assert cli.listing_complete([full(0), full(10), last], 25)
    assert cli.listing_complete([page(0, 10, 0, False, None, True)], 0)
    assert not cli.listing_complete([], 0)
    assert not cli.listing_complete([full(0), full(10), last], 24)  # collected
    assert not cli.listing_complete([full(0), full(20), last], 25)  # gap
    assert not cli.listing_complete([full(0), full(10)], 20)  # last says more
    assert not cli.listing_complete(
        [page(0, 10, 10, False, None, True), last], 15
    )  # an early page says no more
    assert not cli.listing_complete(
        [page(0, 10, 5, True, None, True), page(10, 10, 5, False, None, True)], 10
    )  # short page with hasMore (also misaligns the next offset)
    assert not cli.listing_complete(
        [page(0, 10, 5, True, None, True), page(5, 10, 5, False, None, True)], 10
    )  # short page with hasMore even when the next page starts where it ended
    assert not cli.listing_complete([full(0, 30), full(10), last], 25)  # totals
    assert not cli.listing_complete([page(0, 10, None, None, None, False)], 0)
    assert not cli.listing_complete([page(0, None, 3, False, None, True)], 3)
    assert not cli.listing_complete([page(0, 10, 3, False, None, False)], 3)


async def test_an_oversized_listing_page_is_refused_by_the_guard() -> None:
    huge = httpx.Response(
        200,
        json={"data": [], "hasMore": False, "pad": "x" * 70_000},
    )
    fake = FakeAsaas([]).script("list", lambda r: huge)
    result = await run(fake)
    assert (result.verdict, result.reasons) == ("error", ("listing_inconsistent",))
    assert fake.bodies == []


# ---------------------------------------------------------------------------
# The production transport (every other test injects one)
# ---------------------------------------------------------------------------


async def test_the_production_transport_verifies_tls_and_ignores_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No network I/O: the real transport's send is routed to the fake.

    With ``trust_env=True`` httpx would honour ``SSLKEYLOGFILE`` and write
    the TLS session keys of the connection carrying the API key; with
    ``verify=False`` any certificate would do.
    """

    keylog = tmp_path / "tls-keys.log"
    monkeypatch.setenv("SSLKEYLOGFILE", str(keylog))
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    fake = FakeAsaas([webhook()])
    transports: list[httpx.AsyncHTTPTransport] = []
    clients: list[AsaasClient] = []

    async def offline(
        self: httpx.AsyncHTTPTransport, request: httpx.Request
    ) -> httpx.Response:
        transports.append(self)
        return fake.handle(request)

    class Recording(AsaasClient):
        __slots__ = ()

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            clients.append(self)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", offline)
    monkeypatch.setattr(cli, "AsaasClient", Recording)
    result = await cli.run(
        "smoke", make_config(), transport=None, sleep=no_sleep, rng=random.Random(1)
    )
    assert result.verdict == "ready"
    assert len({id(t) for t in transports}) == 1 and len(transports) == 3
    transport = transports[0]
    context = transport._pool._ssl_context
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.keylog_filename is None
    assert not keylog.exists()
    (client,) = clients
    http = client._http()
    try:
        assert http.follow_redirects is False
        assert http.trust_env is False
        assert http._mounts == {}
        assert http.base_url == httpx.URL("https://api.asaas.com/v3/")
        assert http.timeout == ASAAS_TIMEOUT
        assert isinstance(http._transport, cli.GuardedTransport)
        assert http._transport._inner is transport
    finally:
        await http.aclose()


def test_default_transport_alone_has_the_same_tls_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SSLKEYLOGFILE", str(tmp_path / "keys.log"))
    context = cli.default_transport()._pool._ssl_context
    assert (context.verify_mode, context.check_hostname, context.keylog_filename) == (
        ssl.CERT_REQUIRED,
        True,
        None,
    )


# ---------------------------------------------------------------------------
# apiVersion that a read does not report
# ---------------------------------------------------------------------------


def _without_api_version(**overrides: Any) -> dict[str, Any]:
    hook = webhook(**overrides)
    del hook["apiVersion"]
    return hook


@pytest.mark.parametrize("command", ["readback", "smoke", "reconcile"])
@pytest.mark.parametrize("missing", ["absent", "null"])
async def test_an_unreported_api_version_is_not_compared(
    command: str, missing: str
) -> None:
    hook = _without_api_version() if missing == "absent" else webhook(apiVersion=None)
    fake = FakeAsaas([hook])
    result = await run(fake, command)
    assert (result.verdict, result.exit_code) == ("ready", 0)
    assert result.reasons == ("api_version_unreported",)
    assert fake.bodies == []


async def test_a_webhook_created_here_is_ready_even_if_reads_omit_api_version() -> None:
    fake = FakeAsaas([])
    fake.script(
        "get",
        lambda r: httpx.Response(
            200, json=_without_api_version(id="canaryWebhookIdNew1")
        ),
    )
    result = await run(fake)
    assert (result.verdict, result.action) == ("ready", "created")
    assert result.reasons == ("api_version_unreported",)


async def test_an_unreported_api_version_does_not_hide_other_drift() -> None:
    fake = FakeAsaas([_without_api_version(sendType="X")])
    result = await run(fake, "readback")
    assert result.verdict == "drift"
    assert set(result.reasons) == {"send_type", "api_version_unreported"}


async def _refresher_readback(fake: FakeAsaas) -> Any:
    config = make_config()
    return await AsaasWebhookProvisioner(
        AsaasClient(
            api_key=config.api_key,
            user_agent=config.user_agent,
            transport=fake.transport,
        ),
        desired_webhook_settings(
            environment=config.environment,
            url=config.webhook_url,
            email=config.notification_email,
        ),
        auth_token=config.webhook_token,
    ).readback()


async def test_known_divergence_refresher_still_compares_an_unreported_api_version() -> (
    None
):
    """Pinned until ``compare_webhook`` skips an absent ``apiVersion`` too.

    This command reports ``ready``; the in-process refresher still reports
    drift, so hosted checkout would stay closed.  The companion one-line
    change belongs to ``src/services/billing_webhook.py`` (separate work).
    """

    fake = FakeAsaas([_without_api_version()])
    assert (await run(fake, "readback")).verdict == "ready"
    refresher = await _refresher_readback(fake)
    assert refresher.ready is False and "api_version" in refresher.reasons


# ---------------------------------------------------------------------------
# Branches the review found untested
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["smoke", "reconcile"])
async def test_the_owned_webhook_replaced_between_reads_is_ambiguous(
    command: str,
) -> None:
    fake = FakeAsaas([webhook()])
    calls = {"n": 0}

    def replaced_on_second_listing(request: httpx.Request) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            fake.hooks[:] = [webhook(OTHER_ID)]
        return None

    fake.script("list", replaced_on_second_listing, replaced_on_second_listing)
    result = await run(fake, command)
    assert (result.verdict, result.reasons, result.exit_code) == (
        "ambiguous",
        ("owned_id_changed",),
        20,
    )
    assert fake.bodies == []


@pytest.mark.parametrize("command", ["readback", "smoke"])
async def test_read_only_commands_are_wired_to_a_guard_that_refuses_writes(
    command: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Even a read path that tried to write would be stopped by ``run``'s guard."""

    async def rogue(self: cli.WebhookRegistrar) -> tuple[str, ...]:
        await self._client.create_webhook(self._desired, auth_token=self._token)
        return ()

    monkeypatch.setattr(cli.WebhookRegistrar, command, rogue)
    fake = FakeAsaas([])
    result = await run(fake, command)
    assert (result.verdict, result.reasons) == ("error", ("guard_refused",))
    assert fake.seen == []


@pytest.mark.parametrize(
    ("method", "url"),
    [
        ("POST", "https://api.asaas.com/v3/webhooks?x=1"),
        ("PUT", "https://api.asaas.com/v3/webhooks/canaryWebhookId0001?x=1"),
    ],
)
async def test_guard_refuses_a_write_carrying_a_query_string(
    method: str, url: str
) -> None:
    inner = FakeAsaas([webhook()])
    guard = cli.GuardedTransport(inner.transport, allow_writes=True)
    with pytest.raises(cli.GuardRefused):
        await guard.handle_async_request(httpx.Request(method, url, content=b"{}"))
    assert inner.seen == [] and guard.write_sends == 0


async def test_deadline_during_a_create_reports_a_possibly_applied_write() -> None:
    """For the deploy hook: ``writes: 1`` + ``deadline_exceeded`` = may have created."""

    fake = FakeAsaas([])

    async def slow_post(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            fake.handle(request)  # applied by the provider...
            await asyncio.sleep(30)  # ...but never answered in time
        return fake.handle(request)

    async def no_sleep(_seconds: float) -> None:
        return None

    result = await cli.run(
        "reconcile",
        make_config(),
        transport=httpx.MockTransport(slow_post),
        sleep=no_sleep,
        rng=random.Random(1),
        deadline_seconds=0.3,
    )
    assert (result.verdict, result.action) == ("error", "none")
    assert result.reasons == ("transient", "deadline_exceeded")
    assert result.counts["writes"] == 1
    later = await run(fake)  # the next run adopts what the provider holds
    assert (later.verdict, later.action) == ("ready", "none")


# ---------------------------------------------------------------------------
# Fix loop 2: overlapping pages, raw apiVersion, pause precedence, page cap
# ---------------------------------------------------------------------------


def _hooks_with_owned_at(index: int, total: int = 25) -> list[dict[str, Any]]:
    others = [foreign(i) for i in range(total) if i != index]
    return [*others[:index], webhook(), *others[index:]]


def _offset_ignored(hooks: list[dict[str, Any]]) -> Callable[[int, int], list[Any]]:
    """Serves the first items for every offset, in the count a page should have."""

    def pager(offset: int, limit: int) -> list[Any]:
        return [dict(h) for h in hooks[: max(0, min(limit, len(hooks) - offset))]]

    return pager


def _unstable_sort(hooks: list[dict[str, Any]]) -> Callable[[int, int], list[Any]]:
    """Page 2 repeats the last item of page 1 and drops its own first (ours)."""

    def pager(offset: int, limit: int) -> list[Any]:
        page = [dict(h) for h in hooks[offset : offset + limit]]
        if offset == limit:
            page = [dict(hooks[offset - 1]), *page[1:]]
        return page

    return pager


@pytest.mark.parametrize("form", ["offset_ignored", "unstable_sort"])
@pytest.mark.parametrize("command", ["reconcile", "readback", "smoke"])
async def test_overlapping_pages_are_refused_before_any_decision(
    form: str, command: str
) -> None:
    """Consistent ``hasMore``/``totalCount``, yet ours (index 10) is never served."""

    hooks = _hooks_with_owned_at(10)
    pager = (
        _offset_ignored(hooks) if form == "offset_ignored" else _unstable_sort(hooks)
    )
    fake = FakeAsaas(hooks, pager=pager)
    for _ in range(3):  # every later run too: no copy is ever added
        result = await run(fake, command)
        assert (result.verdict, result.reasons, result.exit_code) == (
            "error",
            ("listing_inconsistent",),
            40,
        )
        assert result.counts["writes"] == 0
    assert set(fake.ops()) == {"list"} and fake.bodies == []
    assert len(fake.hooks) == 25


def test_entry_point_refuses_overlapping_pages_without_writing(tmp_path: Path) -> None:
    """The real ``python -m`` entry point, with the transport routed to a fake.

    The driver patches only ``httpx.AsyncHTTPTransport.handle_async_request``
    (no network) and then runs the module exactly as ``-m`` does.
    """

    hooks = _hooks_with_owned_at(15)
    log = tmp_path / "requests.json"
    driver = (
        "import atexit, json, runpy, sys\n"
        "import httpx\n"
        f"HOOKS = json.loads({json.dumps(json.dumps(hooks))})\n"
        "SEEN = []\n"
        "async def offline(self, request):\n"
        "    SEEN.append(request.method)\n"
        "    if request.method == 'GET' and request.url.path == '/v3/webhooks':\n"
        "        off = int(request.url.params['offset'])\n"
        "        lim = int(request.url.params['limit'])\n"
        "        served = max(0, min(lim, len(HOOKS) - off))\n"
        "        return httpx.Response(200, json={\n"
        "            'hasMore': off + lim < len(HOOKS),\n"
        "            'totalCount': len(HOOKS),\n"
        "            'data': HOOKS[:served],\n"
        "        })\n"
        "    return httpx.Response(500, json={})\n"
        "httpx.AsyncHTTPTransport.handle_async_request = offline\n"
        f"atexit.register(lambda: open({str(log)!r}, 'w').write(json.dumps(SEEN)))\n"
        "sys.argv = ['src.ops.asaas_webhook', 'reconcile']\n"
        "runpy.run_module('src.ops.asaas_webhook', run_name='__main__', alter_sys=True)\n"
    )
    workdir = tmp_path / "app"
    workdir.mkdir()
    (workdir / "src").symlink_to(ROOT / "src", target_is_directory=True)
    (workdir / "driver.py").write_text(driver, encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(workdir / "driver.py")],
        cwd=workdir,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "ENVIRONMENT": "staging",
            "SECRET_KEY": "s" * 48,
            "ASAAS_API_KEY": KEY,
            "ASAAS_WEBHOOK_TOKEN": TOKEN,
            "ASAAS_WEBHOOK_URL": URL,
            "ASAAS_WEBHOOK_NOTIFICATION_EMAIL": EMAIL,
        },
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 40, result.stderr[-2000:]
    document = json.loads(result.stdout)
    assert (document["verdict"], document["reasons"]) == (
        "error",
        ["listing_inconsistent"],
    )
    assert document["counts"]["writes"] == 0
    assert json.loads(log.read_text(encoding="utf-8")) == ["GET"] * 3
    _assert_no_canary(result.stdout, result.stderr)


NON_INTEGER_API_VERSIONS = ["2", "3", 3.0, 2.0, "v2", True, False, [3], {"v": 3}]


@pytest.mark.parametrize("value", NON_INTEGER_API_VERSIONS)
async def test_a_present_non_integer_api_version_is_drift_never_ready(
    value: Any,
) -> None:
    fake = FakeAsaas([webhook(apiVersion=value)])
    readback = await run(fake, "readback")
    assert (readback.verdict, readback.exit_code) == ("drift", 11)
    assert readback.reasons == ("api_version",)
    reconciled = await run(fake)
    assert (reconciled.verdict, reconciled.action) == ("drift", "none")
    assert set(reconciled.reasons) == {"api_version", "requires_recreate"}
    assert fake.bodies == []


@pytest.mark.parametrize(
    ("body", "state"),
    [
        (b'{"id": "a"}', "absent"),
        (b'{"apiVersion": null}', "null"),
        (b'{"apiVersion": 3}', "integer"),
        (b'{"apiVersion": "3"}', "invalid"),
        (b'{"apiVersion": 3.0}', "invalid"),
        (b'{"apiVersion": true}', "invalid"),
        (b"[]", "unreadable"),
        (b"not json", "unreadable"),
    ],
)
def test_api_version_state_reads_the_raw_body(body: bytes, state: str) -> None:
    assert cli.api_version_state(200, body) == state
    assert cli.api_version_state(404, body) == "unreadable"


async def test_the_operator_pause_outranks_requires_recreate() -> None:
    fake = FakeAsaas([webhook(apiVersion=2, enabled=False)])
    paused = await run(fake)
    assert (paused.verdict, paused.exit_code, paused.action) == ("disabled", 13, "none")
    assert {
        "disabled",
        "api_version",
        "requires_recreate",
        "webhook_disabled_by_operator",
    } == set(paused.reasons)
    allowed = await run(fake, allow_enable=True)
    assert (allowed.verdict, allowed.exit_code) == ("drift", 11)
    assert "requires_recreate" in allowed.reasons
    assert "webhook_disabled_by_operator" not in allowed.reasons
    queue = FakeAsaas([webhook(apiVersion=2, interrupted=True)])
    held = await run(queue, no_resume=True)
    assert (held.verdict, held.exit_code) == ("interrupted", 12)
    assert {"queue_not_resumed", "requires_recreate"} <= set(held.reasons)
    assert fake.bodies == [] and queue.bodies == []


class _EndlessPage(httpx.AsyncByteStream):
    """A listing page that keeps streaming (16 MiB at most, so a broken cap
    cannot exhaust memory) and counts what was pulled from it."""

    CHUNK = b'{"id":"foreignHook0001","name":"other"},' * 25

    def __init__(self) -> None:
        self.chunks = 0

    async def __aiter__(self) -> Any:
        yield b'{"hasMore":false,"data":['
        while self.chunks * len(self.CHUNK) < 16 * 1024 * 1024:
            self.chunks += 1
            yield self.CHUNK
        yield b"{}]}"

    async def aclose(self) -> None:
        return None


async def test_an_endless_listing_page_stops_at_the_cap_without_buffering_it() -> None:
    stream = _EndlessPage()
    requested = {"pages": 0}

    def endless(request: httpx.Request) -> httpx.Response:
        requested["pages"] += 1
        return httpx.Response(200, stream=stream)

    fake = FakeAsaas([]).script("list", endless)
    result = await run(fake)
    assert (result.verdict, result.reasons) == ("error", ("listing_inconsistent",))
    assert requested["pages"] == 1  # no retry, no further page
    assert stream.chunks <= ASAAS_MAX_RESPONSE_BYTES // len(stream.CHUNK) + 2
    assert result.counts["writes"] == 0 and fake.bodies == []
