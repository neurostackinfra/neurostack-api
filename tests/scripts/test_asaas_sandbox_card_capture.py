"""Offline tests for the ASAAS sandbox card-capture tool.

Pure logic only: every HTTP exchange goes through ``httpx.MockTransport`` and
real DNS/connects are refused, so nothing here reaches the network.
"""

from __future__ import annotations

import errno
import io
import json
import os
import random
import signal
import socket
import stat
import subprocess
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterator

import httpx
import pytest

from scripts import asaas_sandbox_card_capture as cap

# Fake keys (never valid). Each holds the 12-digit run 100000000000, which
# the scrubber test echoes back as an amount.
SANDBOX_KEY = "$aact_hmlg_100000000000capture-test-key-0001"
PROD_KEY = "$aact_prod_100000000000capture-test-key-0001"
TODAY = date(2026, 10, 18)


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    attempts: list[str] = []
    real_connect = socket.socket.connect

    def refuse(kind: str) -> Callable[..., Any]:
        def _refuse(*args: Any, **_kwargs: Any) -> Any:
            if kind == "connect" and isinstance(args[1], (str, bytes)):
                return real_connect(*args)
            attempts.append(kind)
            raise OSError(errno.ENETUNREACH, "network disabled in tests")

        return _refuse

    monkeypatch.setattr(socket, "getaddrinfo", refuse("getaddrinfo"))
    monkeypatch.setattr(socket, "create_connection", refuse("create_connection"))
    monkeypatch.setattr(socket.socket, "connect", refuse("connect"))
    yield attempts
    assert attempts == []


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


OWNER_PAYMENT = "pay_owner0000001"
OWNER_SUB_PAYMENT = "pay_owner0000002"
OWNER_SUB = "sub_owner0000001"
OWNER_CUSTOMER = "cus_owner0000001"


class FakeAsaas:
    """In-process sandbox double; records every request.

    It also holds owner-created (panel flow) objects without our tag, so the
    tests can prove those are read but never mutated.
    """

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[httpx.Request] = []
        self.call_times: list[float] = []
        self.payments: dict[str, dict[str, Any]] = {}
        self.subscriptions: dict[str, dict[str, Any]] = {}
        self.customers: dict[str, dict[str, Any]] = {}
        self.fail: dict[tuple[str, str], int] = {}
        self.receive_after: float | None = None
        self.echo_tag = True
        self.created_at = clock.now
        self.customers[OWNER_CUSTOMER] = {
            "object": "customer",
            "id": OWNER_CUSTOMER,
            "name": "Owner Real Name",
            "email": "owner@example.org",
            "cpfCnpj": "52998224725",
            "dateCreated": "2026-10-17",
            "externalReference": None,
            "deleted": False,
        }
        self._payment(OWNER_PAYMENT, None, ref=None, customer=OWNER_CUSTOMER)
        self.subscriptions[OWNER_SUB] = {
            "object": "subscription",
            "id": OWNER_SUB,
            "billingType": "CREDIT_CARD",
            "status": "ACTIVE",
            "externalReference": None,
            "dateCreated": "2026-10-17",
            "deleted": False,
        }
        self._payment(OWNER_SUB_PAYMENT, OWNER_SUB, ref=None, customer=OWNER_CUSTOMER)
        pix = self._payment(
            "pay_ownerpix00001", None, ref=None, customer=OWNER_CUSTOMER
        )
        pix["billingType"] = "UNDEFINED"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def _payment(
        self,
        pid: str,
        sub: str | None,
        *,
        ref: str | None,
        customer: str = "cus_000000000001",
    ) -> dict[str, Any]:
        body = {
            "object": "payment",
            "id": pid,
            "customer": customer,
            "subscription": sub,
            "externalReference": ref,
            "status": "CONFIRMED",
            "billingType": "CREDIT_CARD",
            "value": 10.0,
            "netValue": 9.31,
            "dateCreated": "2026-10-18",
            "dueDate": "2026-10-18",
            "originalDueDate": "2026-10-18",
            "paymentDate": None,
            "clientPaymentDate": "2026-10-18",
            "confirmedDate": "2026-10-18",
            "creditDate": None,
            "estimatedCreditDate": "2026-11-19",
            "invoiceUrl": "https://sandbox.asaas.com/i/abc",
            "description": "free text",
            "creditCard": {"creditCardNumber": "4444", "creditCardToken": "tok"},
            "refunds": None,
            "deleted": False,
        }
        self.payments[pid] = body
        return body

    def mutations_of(self, object_id: str) -> list[httpx.Request]:
        return [c for c in self.calls if c.method != "GET" and object_id in c.url.path]

    def _tag(self, sent: dict[str, Any]) -> str | None:
        return sent.get("externalReference") if self.echo_tag else None

    def _list(self, data: list[dict[str, Any]]) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "hasMore": False,
                "totalCount": len(data),
                "data": data,
            },
        )

    def _get(self, store: dict[str, dict[str, Any]], path: str) -> httpx.Response:
        object_id = path.rsplit("/", 1)[1]
        if object_id not in store:
            return httpx.Response(404, json={"errors": [{"code": "not_found"}]})
        return httpx.Response(200, json=store[object_id])

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        self.call_times.append(self.clock.now)
        method, path = request.method, request.url.path
        params = request.url.params
        for (m, prefix), status in self.fail.items():
            if m == method and path.startswith(prefix):
                return httpx.Response(
                    status,
                    json={
                        "errors": [
                            {"code": "invalid_x", "description": "nope 12345678"}
                        ]
                    },
                )
        if (method, path) == ("POST", "/v3/customers"):
            sent = json.loads(request.content)
            body = {
                "object": "customer",
                "id": "cus_000000000001",
                "name": sent["name"],
                "email": sent["email"],
                "externalReference": self._tag(sent),
                "deleted": False,
            }
            self.customers[body["id"]] = body
            return httpx.Response(200, json=body)
        if (method, path) == ("POST", "/v3/payments"):
            sent = json.loads(request.content)
            body = self._payment("pay_000000000001", None, ref=self._tag(sent))
            return httpx.Response(200, json=body)
        if (method, path) == ("POST", "/v3/subscriptions"):
            sent = json.loads(request.content)
            body = {
                "object": "subscription",
                "id": "sub_000000000001",
                "status": "ACTIVE",
                "cycle": "MONTHLY",
                "billingType": "CREDIT_CARD",
                "externalReference": self._tag(sent),
                "deleted": False,
            }
            self.subscriptions[body["id"]] = body
            self._payment("pay_000000000002", body["id"], ref=self._tag(sent))
            return httpx.Response(200, json=body)
        if (method, path) == ("GET", "/v3/customers"):
            return self._list(list(self.customers.values()))
        if (method, path) == ("GET", "/v3/subscriptions"):
            return self._list(list(self.subscriptions.values()))
        if (method, path) == ("GET", "/v3/payments"):
            data = list(self.payments.values())
            if "subscription" in params:
                data = [p for p in data if p["subscription"] == params["subscription"]]
            if "billingType" in params:
                data = [p for p in data if p["billingType"] == params["billingType"]]
            return self._list(data)
        if method == "GET" and path.startswith("/v3/subscriptions/"):
            return self._get(self.subscriptions, path)
        if method == "GET" and path.startswith("/v3/customers/"):
            return self._get(self.customers, path)
        if method == "GET" and path.endswith("/refunds"):
            return httpx.Response(404, json={"errors": [{"code": "not_found"}]})
        if method == "GET" and path.endswith("/status"):
            return httpx.Response(
                200, json={"status": self.payments[path.split("/")[3]]["status"]}
            )
        if method == "GET" and path.startswith("/v3/payments/"):
            response = self._get(self.payments, path)
            body = self.payments.get(path.rsplit("/", 1)[1])
            if (
                body is not None
                and body["externalReference"] is not None
                and self.receive_after is not None
                and self.clock.now - self.created_at >= self.receive_after
            ):
                body["status"] = "RECEIVED"
                body["paymentDate"] = "2026-11-19"
                body["creditDate"] = "2026-11-19"
                response = httpx.Response(200, json=body)
            return response
        if method == "POST" and path.endswith("/refund"):
            pid = path.split("/")[3]
            body = self.payments[pid]
            sent = json.loads(request.content)
            value = sent.get("value", body["value"])
            body["status"] = "REFUNDED" if "value" not in sent else body["status"]
            body["refunds"] = [
                {
                    "status": "PENDING",
                    "value": value,
                    "dateCreated": "2026-10-18 10:00:00",
                    "description": "x",
                    "transactionReceiptUrl": "https://x",
                }
            ]
            return httpx.Response(200, json=body)
        if method == "DELETE":
            object_id = path.rsplit("/", 1)[1]
            for store in (self.payments, self.subscriptions, self.customers):
                if object_id in store:
                    store[object_id]["deleted"] = True
            return httpx.Response(200, json={"deleted": True, "id": object_id})
        return httpx.Response(418)


def make_capture(
    fake: FakeAsaas,
    clock: FakeClock,
    out: io.StringIO,
    policy: cap.PollPolicy | None = None,
) -> cap.Capture:
    reporter = cap.Reporter(out)
    identity = cap.SyntheticIdentity.generate(random.Random(7))
    for value in identity.scrub_values():
        reporter.add_secret(value)
    reporter.add_secret(SANDBOX_KEY)
    http = cap.build_http_client(fake.transport())
    return cap.Capture(
        key=cap.validate_sandbox_key(SANDBOX_KEY),
        http=http,
        identity=identity,
        out=reporter,
        today=TODAY,
        clock=clock,
        sleep=clock.sleep,
        poll_policy=policy or cap.PollPolicy(interval=20, budget=120, stale_after=60),
    )


def lines(out: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.getvalue().splitlines()]


def write_env(tmp_path: Path, text: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return path


# -- base URL guard ----------------------------------------------------------


def test_base_url_constant_is_exactly_the_sandbox() -> None:
    assert cap.SANDBOX_BASE_URL == "https://api-sandbox.asaas.com"
    assert cap.assert_sandbox_base_url("https://api-sandbox.asaas.com")


@pytest.mark.parametrize(
    "url",
    [
        "https://api.asaas.com",
        "https://sandbox.asaas.com/api",
        "http://api-sandbox.asaas.com",
        "https://api-sandbox.asaas.com/",
        "https://api-sandbox.asaas.com.evil.example",
    ],
)
def test_base_url_guard_refuses_anything_else(url: str) -> None:
    with pytest.raises(cap.GuardError, match="base_url_not_sandbox"):
        cap.assert_sandbox_base_url(url)


def test_built_client_is_hardened_and_pinned() -> None:
    http = cap.build_http_client(httpx.MockTransport(lambda r: httpx.Response(200)))
    cap.assert_client_hardened(http)
    assert str(http.base_url) == "https://api-sandbox.asaas.com/v3/"


@pytest.mark.parametrize(
    "url",
    [
        "https://api.asaas.com/v3/payments",
        "https://api-sandbox.asaas.com/api/v3/payments",
        "https://api-sandbox.asaas.com:8443/v3/payments",
        "http://api-sandbox.asaas.com/v3/payments",
    ],
)
def test_request_hook_refuses_non_sandbox_requests(url: str) -> None:
    with pytest.raises(cap.GuardError, match="request_origin_not_sandbox"):
        cap._enforce_sandbox_request(httpx.Request("GET", url))


def test_request_hook_refuses_spoofed_host_header() -> None:
    # The URL host alone must be checked, even when the Host header is forged.
    request = httpx.Request(
        "GET",
        "https://api.asaas.com/v3/payments",
        headers={"Host": "api-sandbox.asaas.com"},
    )
    with pytest.raises(cap.GuardError, match="request_origin_not_sandbox"):
        cap._enforce_sandbox_request(request)


def test_request_hook_accepts_sandbox_v3() -> None:
    cap._enforce_sandbox_request(
        httpx.Request("GET", "https://api-sandbox.asaas.com/v3/payments")
    )


# -- key guard ---------------------------------------------------------------


def test_sandbox_key_accepted_and_never_rendered() -> None:
    key = cap.validate_sandbox_key(SANDBOX_KEY)
    assert key.reveal() == SANDBOX_KEY
    assert SANDBOX_KEY not in repr(key) and SANDBOX_KEY not in str(key)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (None, "sandbox_key_missing"),
        ("", "sandbox_key_missing"),
        (PROD_KEY, "sandbox_key_is_production"),
        ("$AACT_PROD_xxxxxxxxxxxxxxxxxxxx", "sandbox_key_is_production"),
        ("$aact_hmlg_xxxxxxxxxxxxxxxxxxxxaact_prod", "sandbox_key_is_production"),
        ("$aact_other_xxxxxxxxxxxxxxxxxxxx", "sandbox_key_prefix_unrecognized"),
        ("$aact_hmlg_abc", "sandbox_key_malformed"),
        ("$aact_hmlg_xxxxxxxxxxxxxxxxxxxx space", "sandbox_key_malformed"),
        ("plainkey" * 4, "sandbox_key_malformed"),
    ],
)
def test_key_guard_refuses_without_echoing(raw: str | None, code: str) -> None:
    with pytest.raises(cap.GuardError) as info:
        cap.validate_sandbox_key(raw)
    assert info.value.code == code
    rendered = repr(info.value) + str(info.value) + json.dumps(info.value.details)
    if raw:
        assert raw not in rendered
        assert "aact_" not in rendered.replace("sandbox_key", "")


def test_env_parser_reads_only_the_named_variable() -> None:
    text = "\n".join(
        [
            f"ASAAS_API_KEY={PROD_KEY}",
            "# ASAAS_SANDBOX_API_KEY=commented",
            "export ASAAS_SANDBOX_API_KEY='first'",
            'ASAAS_SANDBOX_API_KEY="second"  ',
            "ASAAS_SANDBOX_API_KEY_OTHER=nope",
        ]
    )
    assert cap.parse_env_value(text) == "second"
    assert cap.parse_env_value("X=1\nY=2") is None
    assert cap.parse_env_value("ASAAS_SANDBOX_API_KEY=abc # note") == "abc"


def test_load_key_from_env_file(tmp_path: Path) -> None:
    env = write_env(tmp_path, f"OTHER=1\nASAAS_SANDBOX_API_KEY={SANDBOX_KEY}\n")
    assert cap.load_sandbox_key(env).reveal() == SANDBOX_KEY
    with pytest.raises(cap.GuardError, match="env_file_missing"):
        cap.load_sandbox_key(tmp_path / "missing.env")
    link = tmp_path / "link.env"
    link.symlink_to(env)
    with pytest.raises(cap.GuardError, match="env_file_unsafe"):
        cap.load_sandbox_key(link)


def test_load_key_refuses_production_key_in_file(tmp_path: Path) -> None:
    env = write_env(tmp_path, f"ASAAS_SANDBOX_API_KEY={PROD_KEY}\n")
    with pytest.raises(cap.GuardError, match="sandbox_key_is_production"):
        cap.load_sandbox_key(env)


# -- synthetic data ----------------------------------------------------------


def test_generated_cpfs_are_valid() -> None:
    rng = random.Random(1234)
    for _ in range(300):
        cpf = cap.generate_test_cpf(rng)
        assert cap.is_valid_cpf(cpf), cpf
        assert len(set(cpf)) > 1


def test_cpf_validator_against_known_values() -> None:
    assert cap.is_valid_cpf("52998224725")  # public textbook example
    assert not cap.is_valid_cpf("52998224724")
    assert not cap.is_valid_cpf("52998224735")
    assert not cap.is_valid_cpf("11111111111")
    assert not cap.is_valid_cpf("5299822472")
    assert cap._cpf_check_digit([5, 2, 9, 9, 8, 2, 2, 4, 7]) == 2
    assert cap._cpf_check_digit([5, 2, 9, 9, 8, 2, 2, 4, 7, 2]) == 5


def test_identity_is_clearly_synthetic() -> None:
    identity = cap.SyntheticIdentity.generate(random.Random(3))
    assert identity.email.startswith("asaas-capture+")
    assert identity.email.endswith("@example.com")
    assert identity.reference.startswith(cap.REFERENCE_PREFIX)
    payload = cap.payment_payload(identity, "cus_1", TODAY)
    assert payload["creditCard"]["number"] == "4444444444444444"
    assert payload["creditCard"]["ccv"] == "123"
    assert int(payload["creditCard"]["expiryYear"]) > TODAY.year
    assert payload["billingType"] == "CREDIT_CARD"
    assert (
        "callbackUrl" not in json.dumps(payload)
        and "webhook" not in json.dumps(payload).lower()
    )


# -- sanitizer ---------------------------------------------------------------


def test_sanitizer_strips_customer_and_card_data_and_truncates_ids() -> None:
    raw = {
        "id": "pay_080225913252",
        "customer": "cus_000005219613",
        "name": "Real Person",
        "email": "someone@example.org",
        "cpfCnpj": "52998224725",
        "mobilePhone": "4738010919",
        "creditCard": {"creditCardNumber": "4444", "creditCardToken": "secret-token"},
        "creditCardHolderInfo": {"name": "x"},
        "invoiceUrl": "https://sandbox.asaas.com/i/080225913252",
        "description": "free text with 52998224725",
        "status": "CONFIRMED",
        "value": Decimal("10.00"),
        "confirmedDate": "2026-10-18",
        "paymentDate": None,
        "refunds": [
            {
                "status": "DONE",
                "value": 3,
                "description": "x",
                "dateCreated": "2026-10-18 10:01:02",
            }
        ],
    }
    clean = cap.sanitize(raw)
    text = json.dumps(clean)
    for forbidden in (
        "Real Person",
        "someone@example.org",
        "52998224725",
        "4738010919",
        "secret-token",
        "https://",
        "free text",
        "080225913252",
        "000005219613",
    ):
        assert forbidden not in text
    assert clean["id"] == "pay_...(len=16)"
    assert clean["customer"] == "cus_...(len=16)"
    assert clean["status"] == "CONFIRMED"
    assert clean["value"] == "10.00"
    assert clean["confirmedDate"] == "2026-10-18"
    assert clean["paymentDate"] is None
    assert clean["_other_fields"]["creditCard"] == "object"
    assert clean["_other_fields"]["email"] == "string"
    assert clean["refunds"] == [
        {
            "status": "DONE",
            "value": "3",
            "dateCreated": "2026-10-18 10:01:02",
            "_other_fields": {"description": "string"},
        }
    ]


def test_sanitizer_refuses_non_date_values_in_date_fields() -> None:
    clean = cap.sanitize(
        {"paymentDate": "someone@example.org", "status": "x y", "value": "10"}
    )
    assert clean == {
        "paymentDate": "<unrecognized>",
        "status": "<unrecognized>",
        "value": "<unrecognized>",
    }


def test_date_presence_and_relations() -> None:
    clean = cap.sanitize(
        {
            "dateCreated": "2026-10-18",
            "confirmedDate": "2026-10-18",
            "estimatedCreditDate": "2026-11-19",
            "paymentDate": None,
        }
    )
    presence = cap.date_presence(clean)
    assert presence["paymentDate"] is None
    assert presence["creditDate"] == "absent"
    assert presence["confirmedDate"] == "2026-10-18"
    relations = cap.date_relations(clean)
    assert relations["estimatedCreditDate-dateCreated_days"] == 32
    assert relations["confirmedDate-dateCreated_days"] == 0
    assert relations["paymentDate-dateCreated_days"] is None


def test_error_summary_emits_code_and_fixed_reason_only() -> None:
    body = json.dumps(
        {
            "errors": [
                {
                    "code": "invalid_action",
                    "description": "Esta transação só pode ser estornada "
                    "parcialmente no próximo dia.",
                },
                {
                    "code": "invalid_action",
                    "description": "A cobrança [123456] não pode ser removida: Só é "
                    "possível remover cobranças pendentes ou vencidas.",
                },
                {"code": "invalid_cpfCnpj", "description": "CPF 52998224725 inválido"},
                {"code": "bad code!", "description": None},
            ]
        }
    ).encode()
    summary = cap.error_summary(body)
    assert summary == [
        {"code": "invalid_action", "reason": "partial_refund_next_day_only"},
        {"code": "invalid_action", "reason": "delete_only_pending_or_overdue"},
        {"code": "invalid_cpfCnpj", "reason": "unmapped"},
        {"code": "<?>", "reason": "none"},
    ]
    assert "52998224725" not in json.dumps(summary)


def test_reporter_scrubs_registered_secrets() -> None:
    out = io.StringIO()
    reporter = cap.Reporter(out)
    reporter.add_secret(SANDBOX_KEY)
    reporter.emit("scrub-test", "ok", leaked=SANDBOX_KEY, mixed=f"a{SANDBOX_KEY}b")
    record = lines(out)[0]
    assert SANDBOX_KEY not in out.getvalue()
    assert record["leaked"] == "[REDACTED]"
    assert record["mixed"] == "<refused>"


# -- pacing and poll bounds --------------------------------------------------


def test_pacer_spaces_requests_at_least_one_second() -> None:
    clock = FakeClock()
    pacer = cap.Pacer(clock=clock, sleep=clock.sleep)
    times = []
    for _ in range(5):
        pacer.wait()
        times.append(clock.now)
    assert all(b - a >= 1.0 for a, b in zip(times, times[1:]))
    clock.now += 5
    before = len(clock.sleeps)
    pacer.wait()
    assert len(clock.sleeps) == before  # no sleep when already spaced


def test_poll_stops_when_all_received() -> None:
    clock = FakeClock()
    states = iter([{"a": "CONFIRMED"}, {"a": "CONFIRMED"}, {"a": "RECEIVED"}])
    changes: list[float] = []
    result = cap.poll_until(
        lambda: next(states),
        policy=cap.PollPolicy(interval=10, budget=1000, stale_after=500),
        clock=clock,
        sleep=clock.sleep,
        on_change=lambda t, _o: changes.append(t),
    )
    assert result["reason"] == "all_received"
    assert result["polls"] == 3
    assert changes == [0.0, 20.0]


def test_poll_stops_when_nothing_changes() -> None:
    clock = FakeClock()
    result = cap.poll_until(
        lambda: {"a": "CONFIRMED"},
        policy=cap.PollPolicy(interval=10, budget=1000, stale_after=60),
        clock=clock,
        sleep=clock.sleep,
        on_change=lambda *_: None,
    )
    assert result["reason"] == "no_change"
    assert result["elapsed_s"] == 60
    assert result["polls"] == 7


def test_poll_never_exceeds_budget() -> None:
    clock = FakeClock()
    flip = {"n": 0}

    def read() -> dict[str, str]:
        flip["n"] += 1
        return {"a": f"S{flip['n'] % 2}"}

    result = cap.poll_until(
        read,
        policy=cap.PollPolicy(interval=10, budget=95, stale_after=1000),
        clock=clock,
        sleep=clock.sleep,
        on_change=lambda *_: None,
    )
    assert result["reason"] == "budget_exhausted"
    assert result["elapsed_s"] <= 95
    assert result["polls"] == 10


def test_default_bounds_keep_the_run_short() -> None:
    policy = cap.PollPolicy()
    assert policy.budget <= 480
    assert policy.stale_after <= policy.budget
    assert cap.MIN_REQUEST_INTERVAL_S >= 1.0


# -- capture flow ------------------------------------------------------------


def test_full_capture_flow_cleans_up_and_never_leaks() -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    out = io.StringIO()
    capture = make_capture(fake, clock, out)
    capture.run()
    records = lines(out)
    steps = [r["step"] for r in records]
    assert steps[:3] == [
        "discovery-customers",
        "discovery-subscriptions",
        "discovery-payments",
    ]
    assert steps.index("discovery") < steps.index("customer-create")
    assert "subscription-first-payment-readback" in steps
    assert "refund-partial-one-off" in steps
    assert "refund-full-subscription-first" in steps
    assert steps[-1] == "cleanup"
    assert records[-1]["outcome"] == "ok"
    assert len(records[-1]["deleted"]) == 4  # subscription, 2 payments, customer
    partial = next(r for r in records if r["step"] == "refund-partial-one-off")
    assert partial["requested_value"] == "3.00"
    refund_bodies = [
        json.loads(c.content) for c in fake.calls if c.url.path.endswith("/refund")
    ]
    assert refund_bodies[0]["value"] == "3.00"
    assert "value" not in refund_bodies[1]
    # Pacing: never more than one request per second.
    assert all(b - a >= 1.0 for a, b in zip(fake.call_times, fake.call_times[1:]))
    text = out.getvalue()
    identity = capture.identity
    for secret in (SANDBOX_KEY, identity.cpf, identity.email, "4444444444444444"):
        assert secret not in text
    assert "https://" not in text.replace("https://api-sandbox.asaas.com", "")
    assert all(c.url.host == "api-sandbox.asaas.com" for c in fake.calls)
    assert all(c.headers["access_token"] == SANDBOX_KEY for c in fake.calls)
    # Owner-created objects are read but never mutated, and leak no PII.
    for foreign in (OWNER_PAYMENT, OWNER_SUB_PAYMENT, OWNER_SUB, OWNER_CUSTOMER):
        assert fake.mutations_of(foreign) == []
    for pii in ("Owner Real Name", "owner@example.org", "52998224725"):
        assert pii not in text
    # Every creation carries this run's tag.
    for call in fake.calls:
        if call.method == "POST" and call.url.path in (
            "/v3/customers",
            "/v3/payments",
            "/v3/subscriptions",
        ):
            sent = json.loads(call.content)
            assert sent["externalReference"] == identity.reference


def test_discovery_captures_owner_card_payments_read_only() -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    out = io.StringIO()
    capture = make_capture(fake, clock, out)
    owner_ids = capture.discover()
    assert owner_ids == [OWNER_PAYMENT, OWNER_SUB_PAYMENT]  # non-card not detailed
    assert fake.mutations_of("pay_ownerpix00001") == []
    assert {c.method for c in fake.calls} == {"GET"}
    records = lines(out)
    details = [r for r in records if r["step"] == "discovery-owner-payment"]
    assert [r["ownership"] for r in details] == [cap.OWNER_PANEL] * 2
    assert details[0]["snapshot"]["dates"]["confirmedDate"] == "2026-10-18"
    assert details[1]["sanitized"]["subscription"] == "sub_...(len=16)"
    listing = next(r for r in records if r["step"] == "discovery-customers")
    assert listing["items"][0]["ownership"] == cap.OWNER_PANEL
    assert "owner@example.org" not in out.getvalue()
    summary = records[-1]
    assert summary["step"] == "discovery" and summary["mutations"] == 0


def test_ownership_classification() -> None:
    clock = FakeClock()
    capture = make_capture(FakeAsaas(clock), clock, io.StringIO())
    tag = capture.identity.reference
    capture.created.subscriptions.append("sub_mine")
    assert capture.ownership({"externalReference": tag}) == cap.THIS_RUN
    mine = {"object": "payment", "subscription": "sub_mine"}
    assert capture.ownership(mine) == cap.THIS_RUN
    foreign = {"object": "payment", "subscription": OWNER_SUB}
    assert capture.ownership(foreign) == cap.OWNER_PANEL
    old = {"externalReference": cap.REFERENCE_PREFIX + "old"}
    assert capture.ownership(old) == cap.PRIOR_RUN
    assert capture.ownership({"externalReference": None}) == cap.OWNER_PANEL
    assert capture.ownership({"externalReference": tag + "x"}) == cap.PRIOR_RUN


def test_refund_refuses_objects_this_run_did_not_create() -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    out = io.StringIO()
    capture = make_capture(fake, clock, out)
    capture.refund("foreign", OWNER_PAYMENT, None)
    assert fake.mutations_of(OWNER_PAYMENT) == []
    assert lines(out)[-1]["reason"] == "not_this_run"


def test_cleanup_deletes_only_tagged_objects() -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    out = io.StringIO()
    capture = make_capture(fake, clock, out)
    # Simulate a bug that put foreign ids into the created lists.
    capture.created.payments.append(OWNER_PAYMENT)
    capture.created.subscriptions.append(OWNER_SUB)
    capture.created.customers.append(OWNER_CUSTOMER)
    result = capture.cleanup()
    assert [c for c in fake.calls if c.method == "DELETE"] == []
    assert {r["reason"] for r in result["remaining"]} == {"not_this_run"}
    assert len(result["remaining"]) == 3


def test_untagged_creation_is_never_adopted() -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    fake.echo_tag = False
    out = io.StringIO()
    capture = make_capture(fake, clock, out)
    with pytest.raises(cap.ProviderError, match="created_tag_mismatch"):
        capture.run()
    assert capture.created.customers == []
    assert [c for c in fake.calls if c.method == "DELETE"] == []


def test_poll_stops_early_on_received() -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    fake.receive_after = 30
    out = io.StringIO()
    capture = make_capture(
        fake, clock, out, cap.PollPolicy(interval=20, budget=400, stale_after=300)
    )
    capture.run()
    poll = next(r for r in lines(out) if r["step"] == "poll")
    assert poll["reason"] == "all_received"
    assert poll["final"] == {"one-off": "RECEIVED", "subscription-first": "RECEIVED"}


def test_cleanup_runs_when_a_step_fails() -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    fake.fail[("POST", "/v3/subscriptions")] = 400
    out = io.StringIO()
    capture = make_capture(fake, clock, out)
    with pytest.raises(cap.ProviderError, match="provider_status_unexpected"):
        capture.run()
    deletes = [c.url.path for c in fake.calls if c.method == "DELETE"]
    assert deletes == [
        "/v3/payments/pay_000000000001",
        "/v3/customers/cus_000000000001",
    ]
    records = lines(out)
    error = next(r for r in records if r["outcome"] == "provider_error")
    assert error["step"] == "subscription-create"
    assert error["errors"] == [{"code": "<refused>", "reason": "unmapped"}]
    assert "nope" not in out.getvalue()
    assert records[-1]["step"] == "cleanup"


def test_cleanup_reports_what_could_not_be_deleted() -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    fake.fail[("DELETE", "/v3/payments/")] = 400
    out = io.StringIO()
    capture = make_capture(fake, clock, out)
    with pytest.raises(cap.ProviderError, match="cleanup_incomplete"):
        capture.run()
    cleanup = lines(out)[-1]
    assert cleanup["outcome"] == "incomplete"
    assert [r["kind"] for r in cleanup["remaining"]] == ["payment", "payment"]
    assert [d["kind"] for d in cleanup["deleted"]] == ["subscription", "customer"]


# -- CLI -----------------------------------------------------------------------


def test_dry_run_makes_no_http_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def no_client(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("dry run built an HTTP client")

    monkeypatch.setattr(cap, "build_http_client", no_client)
    calls: list[httpx.Request] = []
    env = write_env(tmp_path, f"ASAAS_SANDBOX_API_KEY={SANDBOX_KEY}\n")
    out = io.StringIO()
    code = cap.main(
        ["--env-file", str(env)],
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200)),
        stdout=out,
    )
    assert code == cap.EXIT_OK
    assert calls == []
    record = lines(out)[0]
    assert record["step"] == "dry-run"
    assert record["network_calls"] == 0
    assert record["is_sandbox_key"] is True
    assert SANDBOX_KEY not in out.getvalue()
    assert "aact_" not in out.getvalue()


def test_run_with_production_key_aborts_before_any_request(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    env = write_env(tmp_path, f"ASAAS_SANDBOX_API_KEY={PROD_KEY}\n")
    out = io.StringIO()
    code = cap.main(
        ["--run", "--env-file", str(env)],
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200)),
        stdout=out,
    )
    assert code == cap.EXIT_GUARD
    assert calls == []
    assert PROD_KEY not in out.getvalue()
    assert "sandbox_key_is_production" in out.getvalue()


def test_live_run_writes_only_sanitized_capture_file(tmp_path: Path) -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    env = write_env(tmp_path, f"ASAAS_SANDBOX_API_KEY={SANDBOX_KEY}\n")
    capture_file = tmp_path / "capture.json"
    out = io.StringIO()
    rng = random.Random(99)
    identity = cap.SyntheticIdentity.generate(random.Random(99))
    code = cap.main(
        ["--run", "--env-file", str(env), "--capture-file", str(capture_file)],
        transport=fake.transport(),
        stdout=out,
        clock=clock,
        sleep=clock.sleep,
        rng=rng,
        poll_policy=cap.PollPolicy(interval=20, budget=60, stale_after=40),
    )
    assert code == cap.EXIT_OK
    assert stat.S_IMODE(os.stat(capture_file).st_mode) == 0o600
    stored = capture_file.read_text(encoding="utf-8")
    payload = json.loads(stored)
    assert payload["schema"] == cap.CAPTURE_SCHEMA
    assert payload["emit_errors"] == {"count": 0}
    assert [r["step"] for r in payload["records"]][-2:] == ["requests", "summary"]
    for secret in (SANDBOX_KEY, identity.cpf, identity.email, "4444444444444444"):
        assert secret not in stored
        assert secret not in out.getvalue()
    first_post = next(c for c in fake.calls if c.method == "POST")
    sent_customer = json.loads(first_post.content)
    assert sent_customer["cpfCnpj"] == identity.cpf  # the scrubbed value was real


def test_run_refuses_existing_capture_file(tmp_path: Path) -> None:
    env = write_env(tmp_path, f"ASAAS_SANDBOX_API_KEY={SANDBOX_KEY}\n")
    existing = tmp_path / "capture.json"
    existing.write_text("keep", encoding="utf-8")
    calls: list[httpx.Request] = []
    code = cap.main(
        ["--run", "--env-file", str(env), "--capture-file", str(existing)],
        transport=httpx.MockTransport(lambda r: calls.append(r) or httpx.Response(200)),
        stdout=io.StringIO(),
    )
    assert code == cap.EXIT_GUARD
    assert calls == []
    assert existing.read_text(encoding="utf-8") == "keep"


def test_discover_only_run_never_mutates(tmp_path: Path) -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    env = write_env(tmp_path, f"ASAAS_SANDBOX_API_KEY={SANDBOX_KEY}\n")
    out = io.StringIO()
    code = cap.main(
        ["--run", "--discover-only", "--env-file", str(env)],
        transport=fake.transport(),
        stdout=out,
        clock=clock,
        sleep=clock.sleep,
    )
    assert code == cap.EXIT_OK
    assert fake.calls and {c.method for c in fake.calls} == {"GET"}
    assert SANDBOX_KEY not in out.getvalue()


def test_prior_partial_refund_requires_capture_tag() -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    prior = fake._payment(
        "pay_prior0000001", None, ref=cap.REFERENCE_PREFIX + "0123456789abcdef"
    )
    out = io.StringIO()
    capture = make_capture(fake, clock, out)
    # Without the opt-in a prior-run payment is refused.
    capture.refund("prior", prior["id"], Decimal("3.00"))
    assert fake.mutations_of(prior["id"]) == []
    # Owner objects stay refused even with the opt-in.
    capture.refund("owner", OWNER_PAYMENT, Decimal("3.00"), allow_prior=True)
    assert fake.mutations_of(OWNER_PAYMENT) == []
    capture.refund("prior", prior["id"], Decimal("3.00"), allow_prior=True)
    assert len(fake.mutations_of(prior["id"])) == 1


def test_tz_compare_distinguishes_sao_paulo_and_utc_dates() -> None:
    moment = cap.instant(datetime(2026, 10, 19, 0, 45, tzinfo=timezone.utc))
    assert moment["sao_paulo_date"] == "2026-10-18"
    assert moment["utc_date"] == "2026-10-19"
    result = cap.tz_compare(
        {
            "dateCreated": "2026-10-18",
            "confirmedDate": "2026-10-19",
            "paymentDate": None,
        },
        moment,
    )
    assert result["dateCreated"] == "sao_paulo_date"
    assert result["confirmedDate"] == "utc_date"
    assert result["paymentDate"] == "null"
    assert result["creditDate"] == "absent"


# -- fix loop 1: output hygiene ------------------------------------------------

UPPER_KEY = "$aact_hmlg_ABCDEFGHIJKLMNOPQRSTUVWXYZ"
FIXED_WALL = datetime(2026, 10, 18, 13, 0, 0, tzinfo=timezone.utc)  # 10:00 SP


def test_provider_error_text_never_printed() -> None:
    echo = {
        "errors": [
            {
                "code": "invalid_x",
                "description": f"key={SANDBOX_KEY[:30]} mail=owner@example.org "
                "url=https://evil.example/p name=Joao Owner",
            }
        ]
    }
    clock = FakeClock()
    fake = FakeAsaas(clock)
    fake.fail[("POST", "/v3/customers")] = 400
    out = io.StringIO()
    capture = make_capture(fake, clock, out)
    original = fake.__call__

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v3/customers":
            fake.calls.append(request)
            return httpx.Response(400, json=echo)
        return original(request)

    capture.http = cap.build_http_client(httpx.MockTransport(handler))
    with pytest.raises(cap.ProviderError) as info:
        capture.create_customer()
    cap.emit_error(capture.out, info.value, default_step="x")
    text = out.getvalue()
    for needle in (SANDBOX_KEY[:30], "owner@example.org", "evil.example", "Joao"):
        assert needle not in text
    assert lines(out)[-1]["errors"] == [{"code": "<refused>", "reason": "unmapped"}]


def test_scrubber_is_longest_first_and_case_insensitive() -> None:
    key = "$aact_hmlg_abc4738010919XYZsecretTAIL"
    out = io.StringIO()
    reporter = cap.Reporter(out)
    reporter.add_secret(cap.TEST_PHONE)  # registered first, is inside the key
    reporter.add_key(key)
    assert reporter.scrub("bad " + key) == "bad [REDACTED]"
    assert reporter.scrub(key.upper()) == "[REDACTED]"
    assert reporter.scrub("x" + cap.TEST_PHONE + "y") == "x[REDACTED]y"
    reporter.emit("scrub-test", "ok", echo="bad " + key.lower())
    assert "secrettail" not in out.getvalue().lower()


def test_scrubber_drops_partial_key_echoes() -> None:
    out = io.StringIO()
    reporter = cap.Reporter(out)
    reporter.add_key(UPPER_KEY)
    # 12+ consecutive key characters inside an otherwise allowed shape.
    reporter.emit("scrub-test", "ok", status="GHIJKLMNOPQRST_X")
    assert reporter.scrub("ABCDEFGHIJK_X") == "ABCDEFGHIJK_X"  # 11: kept
    record = lines(out)[0]
    assert record["status"] == "[REDACTED]"
    unregistered = cap.Reporter(io.StringIO())
    assert unregistered.scrub("GHIJKLMNOPQRST_X") == "GHIJKLMNOPQRST_X"
    assert unregistered.scrub("x$AACT_y") == "[REDACTED]"


def test_allowlist_refuses_free_text_and_keeps_known_shapes() -> None:
    out = io.StringIO()
    reporter = cap.Reporter(out)
    reporter.emit(
        "allow-test",
        "ok",
        name="Joao Owner",
        word="Joao",
        lower="joao",
        upper="JOAO",
        url="https://evil.example/p",
        mail="owner@example.org",
        status="CONFIRMED",
        multi="REFUND_REQUESTED",
        day="2026-10-18",
        stamp="2026-10-18 21:51:35",
        hint="pay_...(len=20)",
        value="10.00",
        code="invalid_action",
        exc="ConnectError",
        otherExc="MyCustomError",
        nested={"Joao Owner": "x", "52998224725": 1},
    )
    record = lines(out)[0]
    for name in ("name", "word", "lower", "upper", "url", "mail", "otherExc"):
        assert record[name] == "<refused>", name
    assert record["status"] == "CONFIRMED"
    assert record["multi"] == "REFUND_REQUESTED"
    assert record["day"] == "2026-10-18"
    assert record["stamp"] == "2026-10-18 21:51:35"
    assert record["hint"] == "pay_...(len=20)"
    assert record["value"] == "10.00"
    assert record["code"] == "invalid_action"
    assert record["exc"] == "ConnectError"
    assert "Joao" not in out.getvalue() and "52998224725" not in out.getvalue()


def test_listing_counts_are_coerced_and_digit_field_names_dropped() -> None:
    clock = FakeClock()
    body = {
        "object": "list",
        "totalCount": "owner@example.org 52998224725",
        "hasMore": {"x": "https://evil.example"},
        "data": [{"52998224725": 1, "id": "cus_1"}],
    }
    out = io.StringIO()
    capture = make_capture(FakeAsaas(clock), clock, out)
    capture.http = cap.build_http_client(
        httpx.MockTransport(lambda r: httpx.Response(200, json=body))
    )
    capture._read_list("probe-list", "/customers", {"limit": "1"})
    record = lines(out)[0]
    assert "totalCount" not in record and "hasMore" not in record
    text = out.getvalue()
    assert "52998224725" not in text and "evil.example" not in text
    good = {"object": "list", "totalCount": 3, "hasMore": False, "data": []}
    capture.http = cap.build_http_client(
        httpx.MockTransport(lambda r: httpx.Response(200, json=good))
    )
    capture._read_list("probe-list", "/customers", {"limit": "1"})
    record = lines(out)[1]
    assert record["totalCount"] == 3 and record["hasMore"] is False


def test_field_name_allowlist_rejects_digit_only_names() -> None:
    assert cap._safe_field("52998224725") == "<?>"
    assert cap._safe_field("9abc") == "<?>"
    assert cap._safe_field("_other") == "_other"
    assert cap.sanitize({"52998224725": 1, "status": "PENDING"}) == {
        "status": "PENDING"
    }


def test_impossible_dates_never_crash() -> None:
    clean = cap.sanitize({"dateCreated": "2026-02-30", "confirmedDate": "2026-02-30"})
    assert cap.date_relations(clean)["confirmedDate-dateCreated_days"] is None
    moment = cap.instant(FIXED_WALL)
    assert cap.tz_compare(clean, moment)["confirmedDate"] == "unrecognized"
    assert cap.tz_compare({"x": 1}, moment)["paymentDate"] == "absent"
    assert cap.wall_clock_match("2026-02-30 10:00:00", FIXED_WALL, FIXED_WALL) == (
        "unrecognized"
    )


def test_wall_clock_match_tells_sao_paulo_from_utc() -> None:
    before = FIXED_WALL
    after = FIXED_WALL + cap.timedelta(seconds=3)
    assert cap.wall_clock_match("2026-10-18 10:00:01", before, after) == (
        "sao_paulo_wall_clock"
    )
    assert cap.wall_clock_match("2026-10-18 13:00:02", before, after) == (
        "utc_wall_clock"
    )
    assert cap.wall_clock_match("2026-10-18 17:00:00", before, after) == "neither"
    assert cap.wall_clock_match("2026-10-18", before, after) == "date_only"
    assert cap.wall_clock_match(None, before, after) == "unrecognized"


# -- fix loop 1: time evidence for every create and refund -----------------------


def test_every_create_and_refund_records_instants() -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    out = io.StringIO()
    capture = make_capture(fake, clock, out)
    capture.wall = lambda: FIXED_WALL
    capture.run()
    records = {r["step"]: r for r in lines(out)}
    for step in (
        "customer-create",
        "payment-create",
        "subscription-create",
        "subscription-first-payment-readback",
    ):
        assert records[step]["create_instant"]["before"]["sao_paulo"] == (
            "2026-10-18T10:00:00-03:00"
        ), step
        assert records[step]["create_instant"]["before"]["utc_date"] == "2026-10-18"
        assert "tz_compare" in records[step], step
    full = records["refund-full-subscription-first"]
    assert full["refund_instant"]["before"]["utc"] == "2026-10-18T13:00:00+00:00"
    # The fake stamps refunds at 10:00:00, i.e. São Paulo wall-clock time.
    assert full["refund_tz"] == [
        {"dateCreated": "both", "clock": "sao_paulo_wall_clock"}
    ]
    readback = records["refund-full-subscription-first-readback"]
    assert readback["refund_tz"][0]["clock"] == "sao_paulo_wall_clock"


# -- fix loop 1: guards that had no test -----------------------------------------


FAKE_ADDRINFO = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.7", 443))]


@pytest.fixture
def egress(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """A fake resolver under the guard's recording wrapper; no real lookup."""

    lookups: list[str] = []

    def fake_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        lookups.append(str(host))
        return FAKE_ADDRINFO if host == cap.SANDBOX_HOST else []

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    yield lookups


def _hook(event: str, *args: Any) -> str:
    try:
        cap._egress_audit_hook(event, args)
    except cap.GuardError:
        return "refused"
    return "allowed"


def test_egress_hook_is_inert_outside_a_run(egress: list[str]) -> None:
    assert cap._EGRESS["active"] == 0
    assert _hook("socket.getaddrinfo", "evil.example", 443) == "allowed"


def test_egress_guard_activates_the_hook(egress: list[str]) -> None:
    with cap.egress_guard():
        assert _hook("socket.getaddrinfo", "evil.example", 443) == "refused"
        assert _hook("socket.gethostbyname", "evil.example") == "refused"
    assert _hook("socket.getaddrinfo", "evil.example", 443) == "allowed"


def test_egress_hook_allows_only_resolved_sandbox_addresses(
    egress: list[str],
) -> None:
    sock = object()
    fake = socket.getaddrinfo
    with cap.egress_guard():
        assert socket.getaddrinfo is not fake  # the recording wrapper is in place
        # Before the sandbox host is resolved no IP literal is allowed.
        assert _hook("socket.connect", sock, ("198.51.100.7", 443)) == "refused"
        assert _hook("socket.getaddrinfo", "API-SANDBOX.ASAAS.COM", 443) == "allowed"
        assert egress == []  # the hook itself never resolves anything
        socket.getaddrinfo("api-sandbox.asaas.com", 443)
        socket.getaddrinfo("evil-but-faked.example", 443)
        assert _hook("socket.connect", sock, ("198.51.100.7", 443)) == "allowed"
        assert _hook("socket.connect", sock, ("198.51.100.8", 443)) == "refused"
        assert _hook("socket.connect", sock, ("2001:db8::1", 443, 0, 0)) == "refused"
        assert _hook("socket.connect", sock, ("evil.example", 443)) == "refused"
        assert _hook("socket.connect", sock, ("api-sandbox.asaas.com", 443)) == (
            "allowed"
        )
        assert _hook("socket.connect", sock, None) == "refused"
        assert _hook("socket.sendto", sock, ("198.51.100.8", 53)) == "refused"
        assert _hook("socket.sendto", sock, ("198.51.100.7", 443)) == "allowed"
        assert _hook("socket.sendmsg", sock, ("198.51.100.8", 53)) == "refused"
        assert _hook("socket.sendmsg", sock, None) == "allowed"
        assert _hook("socket.connect", sock, "/tmp/local.sock") == "allowed"
        assert _hook("socket.getaddrinfo", "api-sandbox.asaas.com.", 443) == ("refused")
    assert socket.getaddrinfo is fake  # restored
    assert cap._EGRESS["addresses"] == set()
    assert egress == ["api-sandbox.asaas.com", "evil-but-faked.example"]


def test_nested_egress_guards_restore_getaddrinfo_once(egress: list[str]) -> None:
    fake = socket.getaddrinfo
    with cap.egress_guard():
        wrapped = socket.getaddrinfo
        with cap.egress_guard():
            assert socket.getaddrinfo is wrapped
        assert socket.getaddrinfo is wrapped
        assert cap._EGRESS["active"] == 1
    assert socket.getaddrinfo is fake


def test_request_hook_port_and_decoded_path_checks() -> None:
    spoofed_port = httpx.Request(
        "GET",
        "https://api-sandbox.asaas.com:8443/v3/payments",
        headers={"Host": "api-sandbox.asaas.com"},
    )
    with pytest.raises(cap.GuardError, match="request_origin_not_sandbox"):
        cap._enforce_sandbox_request(spoofed_port)
    for url in (
        "https://api-sandbox.asaas.com/v3/payments/%2e%2e/customers",
        "https://api-sandbox.asaas.com/v3//payments",
        "https://api-sandbox.asaas.com/v3/payments//x",
    ):
        with pytest.raises(cap.GuardError, match="request_origin_not_sandbox"):
            cap._enforce_sandbox_request(httpx.Request("GET", url))


@pytest.mark.parametrize(
    "path",
    [
        "//evil.example/v3/payments",
        "/payments/%2e%2e/customers",
        "/payments/x?y=1",
        "/payments/x#frag",
        "https://evil.example/v3/x",
        "/@evil.example/x",
        "/payments/x\\..\\y",
        "payments",
    ],
)
def test_capture_send_refuses_path_tricks(path: str) -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    capture = make_capture(fake, clock, io.StringIO())
    with pytest.raises(cap.GuardError, match="request_path_invalid"):
        capture.send("GET", path)
    assert fake.calls == []


def test_hardening_assertion_rejects_weak_clients() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200))
    hooks = {"request": [cap._enforce_sandbox_request]}
    base = "https://api-sandbox.asaas.com/v3"
    weak = [
        httpx.Client(
            base_url=base,
            follow_redirects=True,
            trust_env=False,
            event_hooks=hooks,
            transport=transport,
        ),
        httpx.Client(
            base_url=base,
            follow_redirects=False,
            trust_env=True,
            event_hooks=hooks,
            transport=transport,
        ),
        httpx.Client(
            base_url=base, follow_redirects=False, trust_env=False, transport=transport
        ),
        httpx.Client(
            base_url="https://api.asaas.com/v3",
            follow_redirects=False,
            trust_env=False,
            event_hooks=hooks,
            transport=transport,
        ),
    ]
    for client in weak:
        with pytest.raises(cap.GuardError, match="http_client_not_hardened"):
            cap.assert_client_hardened(client)


def test_env_file_must_be_a_regular_file(tmp_path: Path) -> None:
    with pytest.raises(cap.GuardError, match="env_file_unsafe"):
        cap.load_sandbox_key(tmp_path)
    fifo = tmp_path / "fifo.env"
    os.mkfifo(fifo, 0o600)
    previous = signal.signal(signal.SIGALRM, signal.default_int_handler)
    signal.alarm(5)  # a blocking open would hang: fail the test instead
    try:
        with pytest.raises(cap.GuardError, match="env_file_unsafe"):
            cap.load_sandbox_key(fifo)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_env_file_must_belong_to_the_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = write_env(tmp_path, f"ASAAS_SANDBOX_API_KEY={SANDBOX_KEY}\n")
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    with pytest.raises(cap.GuardError, match="env_file_unsafe"):
        cap.load_sandbox_key(env)


def test_env_file_bom_and_non_utf8_bytes_do_not_crash(tmp_path: Path) -> None:
    bom = tmp_path / "bom.env"
    bom.write_bytes(b"\xef\xbb\xbf" + f"ASAAS_SANDBOX_API_KEY={SANDBOX_KEY}\n".encode())
    assert cap.load_sandbox_key(bom).reveal() == SANDBOX_KEY
    latin1 = tmp_path / "latin1.env"
    latin1.write_bytes(
        b"# configura\xe7\xe3o\n" + f"ASAAS_SANDBOX_API_KEY={SANDBOX_KEY}\n".encode()
    )
    assert cap.load_sandbox_key(latin1).reveal() == SANDBOX_KEY
    broken = tmp_path / "broken.env"
    broken.write_bytes(b"ASAAS_SANDBOX_API_KEY=$aact_hmlg_abc\xff\xfedefghijkl\n")
    with pytest.raises(cap.GuardError, match="sandbox_key_malformed"):
        cap.load_sandbox_key(broken)


def test_env_file_mode_is_a_warning_not_a_refusal(tmp_path: Path) -> None:
    assert cap.env_file_mode_warning(0o100600) is False
    assert cap.env_file_mode_warning(0o100644) is True
    assert cap.env_file_mode_warning(0o100777) is True
    clock = FakeClock()
    env = write_env(tmp_path, f"ASAAS_SANDBOX_API_KEY={SANDBOX_KEY}\n")
    env.chmod(0o644)
    out = io.StringIO()
    code = cap.main(
        ["--run", "--discover-only", "--env-file", str(env)],
        transport=FakeAsaas(clock).transport(),
        stdout=out,
        clock=clock,
        sleep=clock.sleep,
    )
    assert code == cap.EXIT_OK
    assert lines(out)[0]["env_file_mode_warning"] is True


def test_response_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    capture = make_capture(fake, clock, io.StringIO())
    monkeypatch.setattr(cap, "MAX_RESPONSE_BYTES", 100)
    with pytest.raises(cap.ProviderError, match="response_too_large"):
        capture.send("GET", "/payments")


def test_provider_id_allowlist() -> None:
    assert cap.id_hint("pay/../x") is None
    assert cap.id_hint("pay x") is None
    assert cap.id_hint("pay_123") == "pay_...(len=7)"
    clock = FakeClock()
    capture = make_capture(FakeAsaas(clock), clock, io.StringIO())
    tag = capture.identity.reference
    for bad in ("pay/../x", "pay x", "pay.1", ""):
        with pytest.raises(cap.ProviderError, match="created_id_missing"):
            capture._track([], {"id": bad, "externalReference": tag})


def test_sandbox_key_rejected_on_401() -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    fake.fail[("POST", "/v3/customers")] = 401
    capture = make_capture(fake, clock, io.StringIO())
    with pytest.raises(cap.ProviderError, match="sandbox_key_rejected"):
        capture.create_customer()


def test_transport_error_is_reported_without_details() -> None:
    clock = FakeClock()
    capture = make_capture(FakeAsaas(clock), clock, io.StringIO())

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused to secret-host")

    capture.http = cap.build_http_client(httpx.MockTransport(boom))
    with pytest.raises(cap.ProviderError, match="transport_error") as info:
        capture.send("GET", "/payments")
    assert info.value.details == {"exception": "ConnectError"}


def test_cleanup_crash_is_reported_and_fails_the_run() -> None:
    clock = FakeClock()
    out = io.StringIO()
    capture = make_capture(FakeAsaas(clock), clock, out)

    def crash() -> dict[str, Any]:
        raise RuntimeError("boom")

    capture.cleanup = crash  # type: ignore[method-assign]
    with pytest.raises(cap.ProviderError, match="cleanup_incomplete"):
        capture.run()
    crashed = [r for r in lines(out) if r["outcome"] == "cleanup_crashed"]
    assert crashed == [
        {"step": "cleanup", "outcome": "cleanup_crashed", "exception": "RuntimeError"}
    ]


@pytest.mark.parametrize("exc", [RuntimeError("boom"), KeyboardInterrupt()])
def test_cleanup_runs_after_non_capture_errors(exc: BaseException) -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)

    def sleep(seconds: float) -> None:
        if seconds == 20:
            raise exc
        clock.sleep(seconds)

    capture = make_capture(fake, clock, io.StringIO())
    capture.sleep = sleep
    with pytest.raises(type(exc)):
        capture.run()
    deletes = [c.url.path for c in fake.calls if c.method == "DELETE"]
    assert "/v3/subscriptions/sub_000000000001" in deletes
    assert "/v3/customers/cus_000000000001" in deletes


def _main_with(
    tmp_path: Path,
    fake: FakeAsaas,
    clock: FakeClock,
    *extra: str,
    sleep: Callable[[float], None] | None = None,
    key: str = SANDBOX_KEY,
    rng_seed: int = 5,
) -> tuple[Any, str, Path]:
    env = write_env(tmp_path, f"ASAAS_SANDBOX_API_KEY={key}\n")
    capture_file = tmp_path / "capture.json"
    out = io.StringIO()
    code = cap.main(
        ["--run", "--env-file", str(env), "--capture-file", str(capture_file), *extra],
        transport=fake.transport(),
        stdout=out,
        clock=clock,
        sleep=sleep or clock.sleep,
        rng=random.Random(rng_seed),
        poll_policy=cap.PollPolicy(interval=20, budget=60, stale_after=40),
        wall=lambda: FIXED_WALL,
    )
    return code, out.getvalue(), capture_file


def test_main_keyboard_interrupt_cleans_up_and_writes_capture(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)

    def sleep(seconds: float) -> None:
        if seconds == 20:
            raise KeyboardInterrupt
        clock.sleep(seconds)

    code, text, capture_file = _main_with(tmp_path, fake, clock, sleep=sleep)
    assert code == cap.EXIT_INTERRUPTED
    assert any(c.method == "DELETE" for c in fake.calls)
    stored = json.loads(capture_file.read_text(encoding="utf-8"))["records"]
    assert "interrupted" in [r["outcome"] for r in stored]


def test_main_sigterm_runs_cleanup(tmp_path: Path) -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    before = signal.getsignal(signal.SIGTERM)

    def sleep(seconds: float) -> None:
        if seconds == 20:
            os.kill(os.getpid(), signal.SIGTERM)
        clock.sleep(seconds)

    code, text, capture_file = _main_with(tmp_path, fake, clock, sleep=sleep)
    assert code == 128 + signal.SIGTERM
    assert any(c.method == "DELETE" for c in fake.calls)
    assert capture_file.exists()
    assert signal.getsignal(signal.SIGTERM) == before


def test_main_unexpected_error_still_writes_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)

    def explode(self: cap.Capture, payments: Any) -> Any:
        raise ValueError("unexpected")

    monkeypatch.setattr(cap.Capture, "poll_statuses", explode)
    with pytest.raises(ValueError):
        _main_with(tmp_path, fake, clock)
    stored = json.loads((tmp_path / "capture.json").read_text(encoding="utf-8"))[
        "records"
    ]
    outcomes = [r["outcome"] for r in stored]
    assert "unexpected_error" in outcomes
    assert any(c.method == "DELETE" for c in fake.calls)


def test_impossible_provider_date_does_not_crash_the_run(tmp_path: Path) -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    original = fake._payment

    def bad(*args: Any, **kwargs: Any) -> dict[str, Any]:
        body = original(*args, **kwargs)
        body["confirmedDate"] = "2026-02-30"
        return body

    fake._payment = bad  # type: ignore[method-assign]
    code, text, capture_file = _main_with(tmp_path, fake, clock)
    assert code == cap.EXIT_OK
    assert capture_file.exists()


def test_capture_directory_checked_before_any_request(tmp_path: Path) -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    env = write_env(tmp_path, f"ASAAS_SANDBOX_API_KEY={SANDBOX_KEY}\n")
    out = io.StringIO()
    code = cap.main(
        [
            "--run",
            "--env-file",
            str(env),
            "--capture-file",
            str(tmp_path / "nodir" / "c.json"),
        ],
        transport=fake.transport(),
        stdout=out,
        clock=clock,
        sleep=clock.sleep,
    )
    assert code == cap.EXIT_GUARD
    assert fake.calls == []
    assert "capture_dir_missing" in out.getvalue()


def test_capture_file_is_exclusive_and_never_follows_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reporter = cap.Reporter(io.StringIO())
    existing = tmp_path / "existing.json"
    existing.write_text("keep", encoding="utf-8")
    with pytest.raises(cap.CaptureFileError) as info:
        cap.write_capture_file(existing, [], reporter)
    assert info.value.reason == "target_exists"
    assert info.value.errno == errno.EEXIST
    assert existing.read_text(encoding="utf-8") == "keep"
    assert info.value.temp is not None and info.value.temp.exists()
    dangling = tmp_path / "dangling.json"
    dangling.symlink_to(tmp_path / "target.json")
    with pytest.raises(cap.CaptureFileError, match="target_exists"):
        cap.write_capture_file(dangling, [], reporter)
    assert not (tmp_path / "target.json").exists()
    seen: list[int] = []
    real_open = os.open

    def spy(path: Any, flags: int, mode: int = 0o777) -> int:
        seen.append(flags)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", spy)
    cap.write_capture_file(tmp_path / "new.json", [], reporter)
    assert seen[-1] & os.O_EXCL and seen[-1] & os.O_NOFOLLOW and seen[-1] & os.O_CREAT


def test_dry_run_reports_bad_key_and_exits_non_zero(tmp_path: Path) -> None:
    env = write_env(tmp_path, f"ASAAS_SANDBOX_API_KEY={PROD_KEY}\n")
    out = io.StringIO()
    code = cap.main(["--env-file", str(env)], stdout=out)
    record = lines(out)[0]
    assert code == cap.EXIT_GUARD
    assert record["is_sandbox_key"] is False
    assert record["key_check"] == "sandbox_key_is_production"
    assert PROD_KEY not in out.getvalue()


def test_default_poll_interval_keeps_polls_few() -> None:
    clock = FakeClock()
    assert cap.PollPolicy().interval >= 10
    result = cap.poll_until(
        lambda: {"a": "CONFIRMED"},
        policy=cap.PollPolicy(),
        clock=clock,
        sleep=clock.sleep,
        on_change=lambda *_: None,
    )
    assert result["polls"] <= cap.POLL_STALE_S / 10 + 2
    assert result["reason"] == "no_change"


def test_poll_budget_accounts_for_request_latency() -> None:
    clock = FakeClock()

    def slow_read() -> dict[str, str]:
        clock.now += 15  # each read takes 15 s
        return {"a": f"S{clock.now}"}  # always changing: never stale

    result = cap.poll_until(
        slow_read,
        policy=cap.PollPolicy(interval=10, budget=100, stale_after=1000),
        clock=clock,
        sleep=clock.sleep,
        on_change=lambda *_: None,
    )
    assert result["reason"] == "budget_exhausted"
    assert result["elapsed_s"] <= 100


def test_prior_tag_must_be_exact_capture_format() -> None:
    for ref in (
        cap.REFERENCE_PREFIX + "xyz",
        cap.REFERENCE_PREFIX + "0123456789ABCDEF",
        cap.REFERENCE_PREFIX + "0123456789abcde",
        cap.REFERENCE_PREFIX + "0123456789abcdef0",
    ):
        clock = FakeClock()
        fake = FakeAsaas(clock)
        prior = fake._payment("pay_prior0000001", None, ref=ref)
        capture = make_capture(fake, clock, io.StringIO())
        capture.refund("prior", prior["id"], Decimal("3.00"), allow_prior=True)
        assert fake.mutations_of(prior["id"]) == [], ref


@pytest.mark.parametrize("flag", [False, True])
def test_partial_refund_prior_requires_the_opt_in_flag(
    tmp_path: Path, flag: bool
) -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    prior = fake._payment(
        "pay_prior0000001", None, ref=cap.REFERENCE_PREFIX + "0123456789abcdef"
    )
    extra = ("--partial-refund-prior",) if flag else ()
    _main_with(tmp_path, fake, clock, *extra)
    refunds = [
        c for c in fake.mutations_of(prior["id"]) if c.url.path.endswith("/refund")
    ]
    assert len(refunds) == (1 if flag else 0)
    if flag:
        assert json.loads(refunds[0].content)["value"] == "3.00"


def test_main_registers_identity_values_in_the_scrubber(tmp_path: Path) -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    identity = cap.SyntheticIdentity.generate(random.Random(5))
    original = fake._payment

    def echo_cpf(*args: Any, **kwargs: Any) -> dict[str, Any]:
        body = original(*args, **kwargs)
        body["value"] = int(identity.cpf)  # a provider echo in an amount slot
        return body

    fake._payment = echo_cpf  # type: ignore[method-assign]
    code, text, capture_file = _main_with(tmp_path, fake, clock)
    assert identity.cpf not in text
    assert identity.cpf not in capture_file.read_text(encoding="utf-8")
    assert "[REDACTED]" in text


def test_main_registers_the_key_in_the_scrubber(tmp_path: Path) -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    original = fake._payment

    def echo_key(*args: Any, **kwargs: Any) -> dict[str, Any]:
        body = original(*args, **kwargs)
        body["value"] = 100000000000  # the key's 12-digit run in an amount
        return body

    fake._payment = echo_key  # type: ignore[method-assign]
    code, text, capture_file = _main_with(tmp_path, fake, clock)
    assert "100000000000" not in text
    assert "100000000000" not in capture_file.read_text(encoding="utf-8")
    assert "[REDACTED]" in text


def test_invalid_first_payment_id_is_never_used() -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    original = fake.__call__

    def handler(request: httpx.Request) -> httpx.Response:
        response = original(request)
        if request.method == "POST" and request.url.path == "/v3/subscriptions":
            sub_payment = fake.payments.pop("pay_000000000002")
            sub_payment["id"] = "pay bad/../id"
            fake.payments["pay bad/../id"] = sub_payment
        return response

    out = io.StringIO()
    capture = make_capture(fake, clock, out)
    capture.http = cap.build_http_client(httpx.MockTransport(handler))
    capture.run()
    steps = [r["step"] for r in lines(out)]
    assert "subscription-first-payment-readback" not in steps
    assert not any("bad" in c.url.path for c in fake.calls)


def test_exception_labels_are_a_closed_vocabulary() -> None:
    class SecretLeakingError(Exception):
        pass

    assert cap.exception_label(httpx.ConnectError("x")) == "ConnectError"
    assert cap.exception_label(SecretLeakingError("x")) == "other"


def test_scrubber_matches_identity_values_in_any_case() -> None:
    reporter = cap.Reporter(io.StringIO())
    reporter.add_secret("asaas-capture+abc123@example.com")
    assert reporter.scrub("ASAAS-CAPTURE+ABC123@EXAMPLE.COM") == "[REDACTED]"


def test_scrubber_replaces_the_longest_secret_first() -> None:
    reporter = cap.Reporter(io.StringIO())
    reporter.add_secret("12345")  # a shorter secret that prefixes a longer one
    reporter.add_secret("123456789")
    assert reporter.scrub("123456789") == "[REDACTED]"
    assert reporter.scrub("x12345y") == "x[REDACTED]y"


# -- fix loop 2: real signals in a child process ---------------------------------

ROOT = Path(__file__).resolve().parents[2]
SIGNAL_CHILD = r"""
import errno, importlib.util, io, os, random, signal, sys
from pathlib import Path
from scripts import asaas_sandbox_card_capture as cap

root, work, mode = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
spec = importlib.util.spec_from_file_location(
    "t", root + "/tests/scripts/test_asaas_sandbox_card_capture.py"
)
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)
log = open(work / "calls.log", "a", buffering=1)
state = {"deletes": 0, "signalled": False}


def fire(sig):
    state["signalled"] = True
    os.kill(os.getpid(), sig)


class Fake(t.FakeAsaas):
    def __call__(self, request):
        log.write(f"{request.method} {request.url.path}\n")
        if request.method == "DELETE":
            state["deletes"] += 1
            if state["deletes"] == 1:
                if mode in ("term_then_term", "term_only_in_cleanup"):
                    fire(signal.SIGTERM)
                if mode == "term_then_int":
                    fire(signal.SIGINT)
                if mode == "int_raised_in_cleanup":
                    raise KeyboardInterrupt
        return super().__call__(request)


clock = t.FakeClock()
fake = Fake(clock)


def sleep(seconds):
    if seconds == 20 and not state["signalled"]:
        if mode in ("hup_in_poll", "hup_dead_stdout"):
            fire(signal.SIGHUP)
        if mode in ("term_then_term", "term_then_int"):
            fire(signal.SIGTERM)
        if mode == "int_raised_in_cleanup":
            state["signalled"] = True
            raise KeyboardInterrupt
    clock.sleep(seconds)


class DeadAfterSignal(io.TextIOBase):
    def write(self, text):
        if state["signalled"]:
            raise OSError(errno.EIO, "Input/output error")
        sys.__stdout__.write(text)
        return len(text)

    def flush(self):
        pass


if mode == "term_in_capture_write":
    original_write = cap.write_capture_file

    def write(*args, **kwargs):
        os.kill(os.getpid(), signal.SIGTERM)
        return original_write(*args, **kwargs)

    cap.write_capture_file = write
env = work / "s.env"
env.write_text(f"ASAAS_SANDBOX_API_KEY={t.SANDBOX_KEY}\n")
env.chmod(0o600)
sys.exit(
    cap.main(
        ["--run", "--env-file", str(env), "--capture-file", str(work / "cap.json")],
        transport=fake.transport(),
        stdout=DeadAfterSignal() if mode == "hup_dead_stdout" else None,
        clock=clock,
        sleep=sleep,
        rng=random.Random(5),
        poll_policy=cap.PollPolicy(interval=20, budget=60, stale_after=40),
        wall=lambda: t.FIXED_WALL,
    )
)
"""


def _run_signal_child(tmp_path: Path, mode: str) -> dict[str, Any]:
    script = tmp_path / "signal_child.py"
    script.write_text(SIGNAL_CHILD, encoding="utf-8")
    work = tmp_path / mode
    work.mkdir()
    proc = subprocess.run(
        [sys.executable, str(script), str(ROOT), str(work), mode],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT), "PATH": os.environ.get("PATH", "/usr/bin")},
        capture_output=True,
        text=True,
        timeout=120,
    )
    calls = (work / "calls.log").read_text(encoding="utf-8").splitlines()
    capture_file = work / "cap.json"
    payload = (
        json.loads(capture_file.read_text(encoding="utf-8"))
        if capture_file.exists()
        else None
    )
    printed = [json.loads(line) for line in proc.stdout.splitlines() if line]
    return {
        "rc": proc.returncode,
        "stderr": proc.stderr,
        "deletes": [c for c in calls if c.startswith("DELETE")],
        "payload": payload,
        "mode": stat.S_IMODE(capture_file.stat().st_mode) if payload else None,
        "printed": printed,
        "leftovers": [p.name for p in work.iterdir() if p.name.endswith(".tmp")],
    }


def _records(result: dict[str, Any]) -> list[dict[str, Any]]:
    assert result["payload"] is not None, result["stderr"][-2000:]
    records: list[dict[str, Any]] = result["payload"]["records"]
    return records


def test_sighup_with_dead_stdout_still_writes_the_capture_file(
    tmp_path: Path,
) -> None:
    result = _run_signal_child(tmp_path, "hup_dead_stdout")
    assert result["rc"] == 128 + signal.SIGHUP
    assert "Traceback" not in result["stderr"]
    assert len(result["deletes"]) == 4
    records = _records(result)
    assert result["mode"] == 0o600
    cleanup = [r for r in records if r["step"] == "cleanup"]
    assert cleanup and cleanup[-1]["remaining"] == []
    assert records[-1]["step"] == "summary"
    assert records[-1]["exit_code"] == 128 + signal.SIGHUP
    errors = result["payload"]["emit_errors"]
    assert errors["count"] >= 1
    assert errors["first_exception"] == "OSError"
    assert errors["first_errno"] == errno.EIO


def test_sighup_during_poll_cleans_up(tmp_path: Path) -> None:
    result = _run_signal_child(tmp_path, "hup_in_poll")
    assert result["rc"] == 128 + signal.SIGHUP
    assert len(result["deletes"]) == 4
    assert result["printed"][-1]["outcome"] == "interrupted"
    assert result["printed"][-1]["exit_code"] == 128 + signal.SIGHUP
    assert _records(result)[-1] == result["printed"][-1]


@pytest.mark.parametrize("mode", ["term_then_term", "term_then_int"])
def test_second_signal_during_cleanup_is_deferred(tmp_path: Path, mode: str) -> None:
    result = _run_signal_child(tmp_path, mode)
    assert result["rc"] == 128 + signal.SIGTERM
    assert len(result["deletes"]) == 4  # cleanup ran to the end
    cleanup = [r for r in _records(result) if r["step"] == "cleanup"]
    assert cleanup[-1]["outcome"] == "ok" and cleanup[-1]["remaining"] == []
    assert "Traceback" not in result["stderr"]


def test_signal_arriving_only_during_cleanup_is_raised_after_it(
    tmp_path: Path,
) -> None:
    result = _run_signal_child(tmp_path, "term_only_in_cleanup")
    assert len(result["deletes"]) == 4
    assert result["rc"] == 128 + signal.SIGTERM
    assert result["printed"][-1]["exit_code"] == 128 + signal.SIGTERM


def test_interrupt_raised_inside_cleanup_lists_what_is_left(tmp_path: Path) -> None:
    result = _run_signal_child(tmp_path, "int_raised_in_cleanup")
    assert result["rc"] == cap.EXIT_INTERRUPTED
    cleanup = [r for r in _records(result) if r["step"] == "cleanup"]
    assert cleanup[-1]["outcome"] == "interrupted"
    left = cleanup[-1]["remaining"]
    assert [r["kind"] for r in left] == [
        "subscription",
        "payment",
        "payment",
        "customer",
    ]
    assert {r["reason"] for r in left} == {"cleanup_interrupted"}


def test_signal_during_capture_write_is_deferred(tmp_path: Path) -> None:
    result = _run_signal_child(tmp_path, "term_in_capture_write")
    records = _records(result)
    assert result["mode"] == 0o600
    assert result["leftovers"] == []
    assert result["rc"] == cap.EXIT_OK
    assert records[-1]["exit_code"] == result["rc"]
    assert result["printed"][-1]["exit_code"] == result["rc"]


# -- fix loop 2: in-process guards -------------------------------------------------


def test_recording_wrapper_ignores_other_hosts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.99", 443))]

    def fake_getaddrinfo(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        return FAKE_ADDRINFO if host == cap.SANDBOX_HOST else other

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with cap.egress_guard():
        socket.getaddrinfo("evil.example", 443)
        assert cap._EGRESS["addresses"] == set()
        socket.getaddrinfo(cap.SANDBOX_HOST, 443)
        assert cap._EGRESS["addresses"] == {"198.51.100.7"}
        assert _hook("socket.connect", object(), ("203.0.113.99", 443)) == "refused"


def test_egress_guard_starts_with_an_empty_address_set(
    monkeypatch: pytest.MonkeyPatch, egress: list[str]
) -> None:
    monkeypatch.setitem(cap._EGRESS, "addresses", {"198.51.100.9"})
    with cap.egress_guard():
        assert cap._EGRESS["addresses"] == set()
        assert _hook("socket.connect", object(), ("198.51.100.9", 443)) == "refused"


def test_interrupt_inside_cleanup_of_a_normal_run_propagates() -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    original = fake.__call__

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            raise KeyboardInterrupt
        return original(request)

    out = io.StringIO()
    capture = make_capture(fake, clock, out)
    capture.http = cap.build_http_client(httpx.MockTransport(handler))
    with pytest.raises(KeyboardInterrupt):
        capture.run()
    cleanup = [r for r in lines(out) if r["step"] == "cleanup"]
    assert cleanup[-1]["outcome"] == "interrupted"
    assert len(cleanup[-1]["remaining"]) == 4


def test_deferred_signals_record_and_restore() -> None:
    before = signal.getsignal(signal.SIGTERM)
    with cap.deferred_signals() as received:
        os.kill(os.getpid(), signal.SIGTERM)
        os.kill(os.getpid(), signal.SIGINT)
    assert received == [signal.SIGTERM, signal.SIGINT]
    assert signal.getsignal(signal.SIGTERM) == before
    assert isinstance(cap.signal_exception(signal.SIGINT), KeyboardInterrupt)
    interrupted = cap.signal_exception(signal.SIGHUP)
    assert isinstance(interrupted, cap.Interrupted)
    assert interrupted.signum == signal.SIGHUP


def test_emit_tolerates_a_failing_stream() -> None:
    class Broken(io.TextIOBase):
        def write(self, text: str) -> int:
            raise BrokenPipeError(errno.EPIPE, "broken pipe")

    reporter = cap.Reporter(Broken())
    reporter.emit("cleanup", "ok", count=1)
    reporter.emit("summary", "ok", exit_code=0)
    assert [r["step"] for r in reporter.records] == ["cleanup", "summary"]
    assert reporter.emit_errors() == {
        "count": 2,
        "first_exception": "BrokenPipeError",
        "first_errno": errno.EPIPE,
    }


def test_numbers_are_scrubbed_and_only_printed_under_count_keys() -> None:
    out = io.StringIO()
    reporter = cap.Reporter(out)
    reporter.add_secret("52998224725")
    reporter.emit(
        "scrub-test",
        "ok",
        totalCount=52998224725,
        count=3,
        elapsed_s=1.5,
        stray=7,
        nan=float("nan"),
    )
    record = lines(out)[0]
    assert record["totalCount"] == "[REDACTED]"
    assert record["count"] == 3 and record["elapsed_s"] == 1.5
    assert record["stray"] == "<refused>" and record["nan"] == "<refused>"


def test_amounts_keys_and_identifiers_use_fixed_lists() -> None:
    out = io.StringIO()
    reporter = cap.Reporter(out)
    reporter.emit(
        "scrub-test",
        "ok",
        value="52998224725",
        status="52998224725",
        reason="some_made_up_reason",
        kind="made-up-kind",
        multi="NOT_AN_ASAAS_STATUS",
        nested={"Owner_Joao": 1, "joaoOwner": None, "invoiceUrl": "string"},
    )
    record = lines(out)[0]
    assert record["value"] == "52998224725"  # an amount slot
    assert record["status"] == "<refused>"
    assert record["reason"] == "<refused>"
    assert record["kind"] == "<refused>"
    assert record["multi"] == "<refused>"
    assert "Owner_Joao" not in out.getvalue()
    assert set(record["nested"]) == {"<refused>", "joaoOwner", "invoiceUrl"}


def test_write_failure_is_printed_before_the_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(cap, "write_capture_file", fail)
    code, text, capture_file = _main_with(tmp_path, fake, clock)
    steps = [json.loads(line)["step"] for line in text.splitlines()]
    assert steps[-2:] == ["capture-file", "summary"]
    assert not capture_file.exists()


def test_dry_run_shows_the_env_mode_warning(tmp_path: Path) -> None:
    env = write_env(tmp_path, f"ASAAS_SANDBOX_API_KEY={SANDBOX_KEY}\n")
    env.chmod(0o644)
    out = io.StringIO()
    assert cap.main(["--env-file", str(env)], stdout=out) == cap.EXIT_OK
    assert lines(out)[0]["env_file_mode_warning"] is True


def test_capture_write_is_atomic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reporter = cap.Reporter(io.StringIO())
    target = tmp_path / "atomic.json"
    cap.write_capture_file(target, [{"step": "summary"}], reporter)
    assert json.loads(target.read_text(encoding="utf-8"))["records"] == [
        {"step": "summary"}
    ]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == ["atomic.json"]

    def crash(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("killed before the link")

    monkeypatch.setattr(os, "link", crash)
    second = tmp_path / "second.json"
    with pytest.raises(RuntimeError):
        cap.write_capture_file(second, [{"step": "summary"}], reporter)
    assert not second.exists()
    # Killed before the link: the complete temp file is kept, not deleted.
    kept = [p for p in tmp_path.iterdir() if p.name != "atomic.json"]
    assert len(kept) == 1 and kept[0].name.startswith(".second.json.")
    assert json.loads(kept[0].read_text(encoding="utf-8"))["records"] == [
        {"step": "summary"}
    ]
    assert stat.S_IMODE(kept[0].stat().st_mode) == 0o600


def test_capture_file_exists_before_the_summary_is_printed(tmp_path: Path) -> None:
    clock = FakeClock()
    fake = FakeAsaas(clock)
    env = write_env(tmp_path, f"ASAAS_SANDBOX_API_KEY={SANDBOX_KEY}\n")
    capture_file = tmp_path / "capture.json"
    seen: list[bool] = []

    class Watcher(io.TextIOBase):
        def write(self, text: str) -> int:
            if '"step": "summary"' in text:
                seen.append(capture_file.exists())
            return len(text)

    cap.main(
        ["--run", "--env-file", str(env), "--capture-file", str(capture_file)],
        transport=fake.transport(),
        stdout=Watcher(),
        clock=clock,
        sleep=clock.sleep,
        poll_policy=cap.PollPolicy(interval=20, budget=60, stale_after=40),
    )
    assert seen == [True]  # printed once, after the file was written


def test_non_finite_numbers_are_refused_even_under_count_keys() -> None:
    out = io.StringIO()
    reporter = cap.Reporter(out)
    reporter.emit("scrub-test", "ok", elapsed_s=float("nan"), count=float("inf"))
    record = lines(out)[0]
    assert record["elapsed_s"] == "<refused>" and record["count"] == "<refused>"
    assert "NaN" not in out.getvalue() and "Infinity" not in out.getvalue()


# -- round 3: capture write failures never lose the capture -----------------------


def _raise_errno(code: int) -> Callable[..., Any]:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise OSError(code, os.strerror(code))

    return fail


def _temps(directory: Path) -> list[Path]:
    return [p for p in directory.iterdir() if p.name.endswith(".tmp")]


@pytest.mark.parametrize(
    "code",
    [errno.ENOSYS, errno.EACCES, errno.EXDEV, errno.EINVAL, errno.EMLINK, errno.EIO],
)
def test_unexpected_link_errors_keep_the_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    monkeypatch.setattr(os, "link", _raise_errno(code))
    target = tmp_path / "cap.json"
    with pytest.raises(cap.CaptureFileError) as info:
        cap.write_capture_file(
            target, [{"step": "summary"}], cap.Reporter(io.StringIO())
        )
    assert info.value.reason == "link_failed" and info.value.errno == code
    assert not target.exists()  # no rename fallback for these errors
    kept = _temps(tmp_path)
    assert kept == [info.value.temp]
    assert json.loads(kept[0].read_text(encoding="utf-8"))["records"] == [
        {"step": "summary"}
    ]


@pytest.mark.parametrize("code", [errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP])
def test_no_hard_links_falls_back_to_a_rename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    monkeypatch.setattr(os, "link", _raise_errno(code))
    target = tmp_path / "cap.json"
    cap.write_capture_file(target, [{"step": "summary"}], cap.Reporter(io.StringIO()))
    assert json.loads(target.read_text(encoding="utf-8"))["records"] == [
        {"step": "summary"}
    ]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert _temps(tmp_path) == []


def test_rename_fallback_never_replaces_a_file_that_appeared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "cap.json"

    def link_then_race(source: Any, destination: Any) -> None:
        Path(destination).write_text("raced", encoding="utf-8")
        raise OSError(errno.EPERM, "no hard links here")

    monkeypatch.setattr(os, "link", link_then_race)
    with pytest.raises(cap.CaptureFileError) as info:
        cap.write_capture_file(target, [], cap.Reporter(io.StringIO()))
    assert info.value.reason == "target_exists"
    assert target.read_text(encoding="utf-8") == "raced"
    assert _temps(tmp_path) == [info.value.temp]


def test_fsync_failure_keeps_the_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "fsync", _raise_errno(errno.EIO))
    target = tmp_path / "cap.json"
    with pytest.raises(cap.CaptureFileError) as info:
        cap.write_capture_file(target, [], cap.Reporter(io.StringIO()))
    assert info.value.reason == "temp_write_failed" and info.value.errno == errno.EIO
    assert not target.exists()
    assert _temps(tmp_path) == [info.value.temp]


def test_capture_is_fsynced_before_it_is_linked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[str] = []
    real_fsync, real_link = os.fsync, os.link

    def fsync(fd: int) -> None:
        events.append("fsync")
        real_fsync(fd)

    def link(source: Any, destination: Any) -> None:
        events.append("link")
        real_link(source, destination)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "link", link)
    cap.write_capture_file(tmp_path / "cap.json", [], cap.Reporter(io.StringIO()))
    assert events == ["fsync", "link"]


def test_target_created_during_the_run_is_reported(tmp_path: Path) -> None:
    clock = FakeClock()
    target = tmp_path / "capture.json"
    original = FakeAsaas.__call__

    class Racer(FakeAsaas):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.method == "DELETE" and not target.exists():
                target.write_text("someone else", encoding="utf-8")
            return original(self, request)

    code, text, capture_file = _main_with(tmp_path, Racer(clock), clock)
    assert capture_file == target
    assert target.read_text(encoding="utf-8") == "someone else"
    printed = [json.loads(line) for line in text.splitlines()]
    failed = [r for r in printed if r["step"] == "capture-file"]
    assert len(failed) == 1 and failed[0]["reason"] == "target_exists"
    kept = _temps(tmp_path)
    assert [p.name for p in kept] == [failed[0]["temp_file"]]
    assert json.loads(kept[0].read_text(encoding="utf-8"))["schema"] == (
        cap.CAPTURE_SCHEMA
    )
    assert printed[-1]["step"] == "summary"


def test_pre_existing_capture_file_is_never_touched(tmp_path: Path) -> None:
    env = write_env(tmp_path, f"ASAAS_SANDBOX_API_KEY={SANDBOX_KEY}\n")
    existing = tmp_path / "capture.json"
    existing.write_text("keep", encoding="utf-8")
    out = io.StringIO()
    code = cap.main(
        ["--run", "--env-file", str(env), "--capture-file", str(existing)],
        transport=httpx.MockTransport(lambda r: httpx.Response(200)),
        stdout=out,
    )
    assert code == cap.EXIT_GUARD
    assert existing.read_text(encoding="utf-8") == "keep"
    assert _temps(tmp_path) == []
    assert "capture-file" not in [r["step"] for r in lines(out)]


def test_record_never_raises_on_content() -> None:
    class Hostile:
        def __str__(self) -> str:
            raise RuntimeError("cannot render")

    out = io.StringIO()
    reporter = cap.Reporter(out)
    reporter.emit("scrub-test", "ok", nested={Hostile(): 1})
    assert lines(out) == [{"step": "<refused>", "outcome": "emit_failed"}]


def test_source_vocabulary_holds_only_identifier_like_constants() -> None:
    assert "invalid_action" in cap._LITERALS
    for text in (cap.TEST_HOLDER_NAME, cap.USER_AGENT, cap.TEST_POSTAL_CODE):
        assert text not in cap._SOURCE_WORDS
    out = io.StringIO()
    cap.Reporter(out).emit("scrub-test", "ok", status=cap.TEST_HOLDER_NAME)
    assert lines(out)[0]["status"] == "<refused>"


# -- round 3: a real stdout pipe that dies early ------------------------------


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("plain", cap.EXIT_OK), ("hup_in_poll", 128 + signal.SIGHUP)],
)
def test_dead_stdout_pipe_keeps_the_summary_exit_code(
    tmp_path: Path, mode: str, expected: int
) -> None:
    script = tmp_path / "signal_child.py"
    script.write_text(SIGNAL_CHILD, encoding="utf-8")
    work = tmp_path / mode
    work.mkdir()
    proc = subprocess.Popen(
        [sys.executable, str(script), str(ROOT), str(work), mode],
        cwd=ROOT,
        env={"PYTHONPATH": str(ROOT), "PATH": os.environ.get("PATH", "/usr/bin")},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None
    proc.stdout.read(200)  # like `| head -c 200`
    proc.stdout.close()
    stderr = proc.stderr.read().decode("utf-8", errors="replace")
    rc = proc.wait(timeout=120)
    payload = json.loads((work / "cap.json").read_text(encoding="utf-8"))
    summary = payload["records"][-1]
    assert summary["step"] == "summary"
    assert rc == summary["exit_code"] == expected
    assert payload["emit_errors"]["first_errno"] == errno.EPIPE
    assert "BrokenPipeError" not in stderr and "Traceback" not in stderr


def test_long_capture_names_still_get_a_temp_file(tmp_path: Path) -> None:
    target = tmp_path / ("c" * 240 + ".json")
    cap.write_capture_file(target, [], cap.Reporter(io.StringIO()))
    assert target.exists() and _temps(tmp_path) == []


def test_failed_fallback_rename_keeps_the_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(os, "link", _raise_errno(errno.EPERM))
    monkeypatch.setattr(os, "rename", _raise_errno(errno.EXDEV))
    target = tmp_path / "cap.json"
    with pytest.raises(cap.CaptureFileError) as info:
        cap.write_capture_file(target, [], cap.Reporter(io.StringIO()))
    assert info.value.reason == "rename_failed" and info.value.errno == errno.EXDEV
    assert not target.exists()
    assert _temps(tmp_path) == [info.value.temp]
