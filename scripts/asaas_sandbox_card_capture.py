#!/usr/bin/env python3
"""Operator-only ASAAS *sandbox* capture of credit-card payment readbacks.

This tool answers one design question: which date/status/amount fields does
ASAAS return for a CREDIT_CARD payment once it is CONFIRMED (and, if the
sandbox ever gets there, RECEIVED), for a one-off card payment, for the first
payment of a card subscription, and after a partial and a full refund.

It is separate from the production ``AsaasClient`` and imports nothing from
``src``.  It creates no production state and moves no real money.

Safety model:

* the origin is the constant ``https://api-sandbox.asaas.com`` (path prefix
  ``/v3``); there is no override flag, and every outgoing request is
  re-checked by an httpx request hook (scheme, host, ``Host`` header, port,
  userinfo, decoded path) before it reaches the transport;
* the key is read by parsing only the ``ASAAS_SANDBOX_API_KEY`` line of one
  regular, user-owned ``.env`` file (never sourced or exported) and must match
  the sandbox-key allowlist (``$aact_hmlg_`` plus a conservative
  charset/length); anything that looks like a production key (``aact_prod``)
  is refused;
* while a run is active a process audit hook refuses every DNS lookup of a
  host other than ``api-sandbox.asaas.com`` and every connect/sendto/sendmsg
  to an address that is not one the sandbox host resolved to;
* redirects are never followed, ``trust_env`` is off and no proxy is used;
* requests are paced to at most one per second, the status poll is bounded
  by a wall-clock budget (request latency included) and stops early on
  RECEIVED or when nothing changes;
* output is JSON lines built from an allowlist sanitizer: dates, statuses,
  amounts, booleans, counts, field names, truncated ids, ASAAS error codes
  and a fixed mapped reason.  Provider free text (error descriptions,
  customer data, card data, URLs) is never emitted.  Before anything else,
  every string is scrubbed of the key and each generated customer/card value
  (longest first, case-insensitive); a string holding ``$aact_`` or any 12
  consecutive characters of the key is dropped; and any string that is not
  on the closed allowlist of shapes is replaced by ``<refused>``;
* only ASAAS's documented public sandbox test card is used
  (https://docs.asaas.com/docs/faq-sandbox, "Simular aprovacao":
  4444 4444 4444 4444, any future expiry, CCV 123) with a generated test
  customer (algorithmic CPF, ``@example.com`` email, ASAAS doc sample address);
* no webhook or callback URL is ever registered;
* cleanup (delete subscription, payments, customer, each only after a
  readback confirms this run's tag) runs in ``finally``, also on SIGTERM,
  SIGHUP, Ctrl-C and unexpected exceptions, and reports what could not be
  deleted.  ASAAS only deletes PENDING/OVERDUE payments, so a normal live run
  leaves its CONFIRMED/REFUNDED payments behind and ends with
  ``cleanup: incomplete`` (exit 4).

Usage (from the repository root)::

    poetry run python -m scripts.asaas_sandbox_card_capture           # dry run
    poetry run python -m scripts.asaas_sandbox_card_capture --run \\
        --env-file /path/to/neurostack-api/.env \\
        --capture-file /private/dir/capture.json

Exit codes: 0 all fine; 2 guard/config failure (before the first request for
the key, env-file and capture-file guards; a request/egress guard can also
trip mid-run); 4 provider error or incomplete cleanup; 130 / 128+signal when
interrupted; 1 an unexpected exception.
"""

from __future__ import annotations

import argparse
import ast
import errno
import ipaddress
import json
import os
import random
import re
import secrets
import signal
import socket
import stat
import sys
import threading
import time
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import FrameType
from typing import Any, Callable, Iterator, Mapping, Sequence, TextIO
from zoneinfo import ZoneInfo

import httpx

SANDBOX_HOST = "api-sandbox.asaas.com"
SANDBOX_BASE_URL = f"https://{SANDBOX_HOST}"
SANDBOX_API_PATH = "/v3"
SANDBOX_KEY_ENV = "ASAAS_SANDBOX_API_KEY"
ASAAS_KEY_PREFIX = "$aact_"
PRODUCTION_KEY_MARKER = "aact_prod"
SANDBOX_KEY_PREFIX = "$aact_hmlg_"
AUTH_HEADER = "access_token"
USER_AGENT = "NeuroStack/asaas-sandbox-card-capture"
# Every object this tool creates carries ``neurostack-capture-<random>`` as
# its externalReference and in its description; nothing else is ever touched.
REFERENCE_PREFIX = "neurostack-capture-"
DISCOVERY_LIMITS = {"customers": 10, "payments": 20, "subscriptions": 10}
DISCOVERY_DETAIL_CAP = 10
OWNER_PANEL = "owner-created, panel flow"
PRIOR_RUN = "prior-capture-run"
THIS_RUN = "this-run"
HISTORY_NOTE = "none documented; current state only"

# Documented public sandbox test card (docs.asaas.com/docs/faq-sandbox,
# "Simular aprovacao"): any future expiry, CCV 123.
TEST_CARD_NUMBER = "4444444444444444"
TEST_CARD_CCV = "123"
TEST_CARD_SOURCE = "https://docs.asaas.com/docs/faq-sandbox"
# Sample holder address used in the ASAAS API reference examples.
TEST_POSTAL_CODE = "89223-005"
TEST_ADDRESS_NUMBER = "277"
TEST_PHONE = "4738010919"
# RFC 5737 TEST-NET-3 documentation address; the API requires a remoteIp.
TEST_REMOTE_IP = "203.0.113.10"
TEST_HOLDER_NAME = "Asaas Capture Teste"

PAYMENT_VALUE = Decimal("10.00")
PARTIAL_REFUND_VALUE = Decimal("3.00")
SAO_PAULO = ZoneInfo("America/Sao_Paulo")

EXIT_OK = 0
EXIT_UNEXPECTED = 1
EXIT_GUARD = 2
EXIT_PROVIDER = 4
EXIT_INTERRUPTED = 130

MIN_REQUEST_INTERVAL_S = 1.0
POLL_INTERVAL_S = 20.0
POLL_BUDGET_S = 420.0
POLL_STALE_S = 300.0
REFUND_READBACKS = 3
REFUND_READBACK_INTERVAL_S = 5.0
SUBSCRIPTION_LIST_ATTEMPTS = 5
SUBSCRIPTION_LIST_INTERVAL_S = 3.0
MAX_RESPONSE_BYTES = 1_000_000
# A refund entry timestamp counts as "this wall clock" within this tolerance.
WALL_CLOCK_TOLERANCE_S = 120
KEY_ECHO_RUN = 12
CAPTURE_SCHEMA = "asaas_sandbox_card_capture.v2"

# The fields the design question is about, in report order.
DATE_FIELDS = (
    "dateCreated",
    "dueDate",
    "originalDueDate",
    "paymentDate",
    "clientPaymentDate",
    "confirmedDate",
    "creditDate",
    "estimatedCreditDate",
)
EXTRA_DATE_FIELDS = (
    "effectiveDate",
    "refundDate",
    "nextDueDate",
    "endDate",
    "lastInvoiceViewedDate",
    "lastBankSlipViewedDate",
)
STATUS_FIELDS = ("status", "billingType", "cycle", "object")
AMOUNT_FIELDS = ("value", "netValue", "originalValue", "interestValue")
ID_FIELDS = ("id", "customer", "subscription", "installment", "paymentId")
BOOL_FIELDS = ("deleted", "anticipated", "anticipable", "postalService", "hasMore")
COUNT_FIELDS = ("totalCount", "limit", "offset", "installmentNumber")
# Lists whose items are sanitized recursively with the same allowlist.
NESTED_LIST_FIELDS = ("refunds", "data", "errors")

_SANDBOX_KEY = re.compile(r"\$aact_hmlg_[A-Za-z0-9_.:=+/-]{5,501}")
_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)
_PROVIDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
# Field names must start with a letter or underscore (no digit-only names).
_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", re.ASCII)
_DATE_VALUE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$", re.ASCII)
_UPPER_ENUM = re.compile(r"^[A-Z][A-Z0-9_]{0,39}$", re.ASCII)
_LOWER_ENUM = re.compile(r"^[a-z][a-z_]{0,39}$", re.ASCII)
_ERROR_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$", re.ASCII)
_CAPTURE_TAG = re.compile(r"^neurostack-capture-[0-9a-f]{16}$")
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
_SAFE_REQUEST_PATH = re.compile(r"^/[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$", re.ASCII)

# Fixed reasons for provider error descriptions; the description itself is
# matched in memory and never emitted.
_ERROR_REASONS = (
    ("estornada parcialmente no proximo dia", "partial_refund_next_day_only"),
    ("remover cobrancas pendentes ou vencidas", "delete_only_pending_or_overdue"),
)

_KNOWN_EXCEPTIONS = frozenset(
    {
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ReadError",
        "WriteError",
        "RemoteProtocolError",
        "LocalProtocolError",
        "ProxyError",
        "UnsupportedProtocol",
        "DecodingError",
        "TooManyRedirects",
        "TimeoutException",
        "NetworkError",
        "TransportError",
        "HTTPError",
        "RuntimeError",
        "ValueError",
        "TypeError",
        "KeyError",
        "OSError",
        "AttributeError",
        "BrokenPipeError",
        "KeyboardInterrupt",
        "Interrupted",
    }
)
# Multi-word ASAAS enums that may appear in a status slot.
_ASAAS_MULTI_WORD_ENUMS = frozenset(
    {
        "RECEIVED_IN_CASH",
        "REFUND_REQUESTED",
        "REFUND_IN_PROGRESS",
        "PARTIALLY_REFUNDED",
        "CHARGEBACK_REQUESTED",
        "CHARGEBACK_DISPUTE",
        "AWAITING_CHARGEBACK_REVERSAL",
        "DUNNING_REQUESTED",
        "DUNNING_RECEIVED",
        "AWAITING_RISK_ANALYSIS",
        "DEBIT_CARD",
    }
)
# ASAAS error codes printed as they are; any other code prints <refused>.
_ASAAS_ERROR_CODES = frozenset(
    {
        "invalid_action",
        "invalid_value",
        "invalid_customer",
        "invalid_object",
        "invalid_billingType",
        "invalid_dueDate",
        "invalid_access_token",
        "invalid_environment",
        "not_found",
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class CaptureError(Exception):
    """A redacted capture failure carrying a fixed code and safe details."""

    exit_code = EXIT_PROVIDER
    outcome = "provider_error"

    def __init__(
        self,
        code: str,
        *,
        details: Mapping[str, object] | None = None,
        step: str | None = None,
    ) -> None:
        self.code = code
        self.details: dict[str, object] = dict(details or {})
        self.step = step
        self.reported = False
        super().__init__(code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class GuardError(CaptureError):
    """A safety or configuration guard refused to run."""

    exit_code = EXIT_GUARD
    outcome = "guard_failure"


class ProviderError(CaptureError):
    """The provider failed, rejected a request or the outcome is unknown."""

    exit_code = EXIT_PROVIDER
    outcome = "provider_error"


class Interrupted(BaseException):
    """Raised from a SIGTERM/SIGHUP handler so that ``finally`` blocks run."""

    def __init__(self, signum: int) -> None:
        self.signum = signum
        super().__init__(signum)


def exception_label(exc: BaseException) -> str:
    """A closed vocabulary for exception class names."""

    name = type(exc).__name__
    return name if name in _KNOWN_EXCEPTIONS else "other"


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def assert_sandbox_base_url(base_url: str) -> str:
    """The only accepted origin is exactly ``https://api-sandbox.asaas.com``."""

    if base_url != "https://api-sandbox.asaas.com":
        raise GuardError("base_url_not_sandbox")
    return base_url


class SandboxKey:
    """Opaque holder for the sandbox key; never rendered."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "SandboxKey(<redacted>)"

    __str__ = __repr__

    def __reduce__(self) -> Any:
        raise TypeError("SandboxKey cannot be serialized")


def validate_sandbox_key(raw: str | None) -> SandboxKey:
    """Allowlist the key shape; refuse production-looking keys.

    Error codes never contain the key or any part of it.
    """

    if raw is None or raw == "":
        raise GuardError("sandbox_key_missing")
    if PRODUCTION_KEY_MARKER in raw.lower():
        raise GuardError("sandbox_key_is_production")
    if raw.lower().startswith(ASAAS_KEY_PREFIX) and not raw.startswith(
        SANDBOX_KEY_PREFIX
    ):
        raise GuardError("sandbox_key_prefix_unrecognized")
    if not _SANDBOX_KEY.fullmatch(raw):
        raise GuardError("sandbox_key_malformed")
    return SandboxKey(raw)


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    # Unquoted: drop an inline comment introduced by whitespace + '#'.
    return re.split(r"\s+#", value, maxsplit=1)[0].strip()


def parse_env_value(text: str, name: str = SANDBOX_KEY_ENV) -> str | None:
    """Return the last assignment of ``name`` in ``.env`` text; nothing else."""

    found: str | None = None
    for line in text.splitlines():
        match = _ENV_LINE.match(line)
        if match and match.group(1) == name:
            found = _unquote(match.group(2))
    return found


def env_file_mode_warning(mode: int) -> bool:
    """True when group or others may read or write the file.

    This is a warning, not a refusal: ``.env`` files on a WSL ``/mnt/c``
    (drvfs) mount report mode 0777 whatever their real ACL is, and a swapped
    key still has to pass the sandbox-key allowlist.
    """

    return bool(mode & 0o077)


def load_sandbox_key(env_file: Path) -> SandboxKey:
    """Read only ``ASAAS_SANDBOX_API_KEY`` from a regular, user-owned file.

    The file is opened without following a final symlink and non-blocking
    (so a FIFO cannot hang the tool); its type and owner are checked on the
    opened descriptor before anything is read.  A UTF-8 BOM is accepted and
    undecodable bytes never raise (they can only invalidate the key).
    """

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(env_file, flags)
    except FileNotFoundError:
        raise GuardError("env_file_missing") from None
    except OSError:
        raise GuardError("env_file_unsafe") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise GuardError("env_file_unsafe")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise GuardError("env_file_unsafe")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            if sum(len(c) for c in chunks) > MAX_RESPONSE_BYTES:
                raise GuardError("env_file_too_large")
    finally:
        os.close(fd)
    text = b"".join(chunks).decode("utf-8-sig", errors="replace")
    return validate_sandbox_key(parse_env_value(text))


def _enforce_sandbox_request(request: httpx.Request) -> None:
    """Request hook: refuse any request that is not to the sandbox API."""

    url = request.url
    path = url.path
    if (
        url.scheme != "https"
        or url.host != SANDBOX_HOST
        or request.headers.get("Host", SANDBOX_HOST) != SANDBOX_HOST
        or url.port is not None
        or url.userinfo
        or not path.startswith(SANDBOX_API_PATH + "/")
        or ".." in path
        or "//" in path
    ):
        raise GuardError("request_origin_not_sandbox")


# ---------------------------------------------------------------------------
# Process-wide egress guard (defence in depth behind the hardened client)
# ---------------------------------------------------------------------------

_NAME_EVENTS = frozenset(
    {
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyname_ex",
        "socket.gethostbyaddr",
    }
)
_SEND_EVENTS = frozenset({"socket.connect", "socket.sendto", "socket.sendmsg"})
_EGRESS_LOCK = threading.Lock()
_EGRESS: dict[str, Any] = {
    "installed": False,
    "active": 0,
    "addresses": set(),
    "original_getaddrinfo": None,
}


def _host_text(value: object) -> str | None:
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return None
    return value.lower() if isinstance(value, str) else None


def _normalized_ip(value: str) -> str | None:
    try:
        return str(ipaddress.ip_address(value.split("%", 1)[0]))
    except ValueError:
        return None


def _recording_getaddrinfo(original: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap ``socket.getaddrinfo`` to remember what the sandbox host resolved to.

    The wrapper performs no lookup of its own: it records the result of the
    lookup the HTTP client already makes, so only those addresses may be
    connected to.
    """

    def wrapper(host: Any, port: Any, *args: Any, **kwargs: Any) -> Any:
        infos = original(host, port, *args, **kwargs)
        if _host_text(host) == SANDBOX_HOST:
            for info in infos:
                normalized = _normalized_ip(str(info[4][0]))
                if normalized is not None:
                    _EGRESS["addresses"] = set(_EGRESS["addresses"]) | {normalized}
        return infos

    return wrapper


def _egress_audit_hook(event: str, args: tuple[Any, ...]) -> None:
    """Refuse DNS/connect/send to anything but the sandbox while active.

    Lookups are allowed only for the sandbox host.  Connects and datagram
    sends are allowed to the sandbox host name or to an IP literal the
    sandbox host resolved to during this run; any other IP is refused.  The
    hook itself never touches the network, and nothing is logged.
    """

    if not _EGRESS["active"] or not event.startswith("socket."):
        return
    if event in _NAME_EVENTS:
        if _host_text(args[0] if args else None) != SANDBOX_HOST:
            raise GuardError("network_egress_refused", details={"event": event})
        return
    if event not in _SEND_EVENTS:
        return
    address = args[1] if len(args) > 1 else None
    if event == "socket.sendmsg" and address is None:
        return  # connected socket: its peer was checked at connect
    if isinstance(address, (str, bytes)):
        return  # AF_UNIX: local plumbing, never egress
    host = _host_text(address[0] if isinstance(address, tuple) and address else None)
    if host is None:
        allowed = False
    elif _normalized_ip(host) is not None:
        allowed = _normalized_ip(host) in _EGRESS["addresses"]
    else:
        allowed = host == SANDBOX_HOST
    if not allowed:
        raise GuardError("network_egress_refused", details={"event": event})


@contextmanager
def egress_guard() -> Iterator[None]:
    with _EGRESS_LOCK:
        if not _EGRESS["installed"]:
            sys.addaudithook(_egress_audit_hook)
            _EGRESS["installed"] = True
        if _EGRESS["active"] == 0:
            _EGRESS["addresses"] = set()
            _EGRESS["original_getaddrinfo"] = socket.getaddrinfo
            socket.getaddrinfo = _recording_getaddrinfo(socket.getaddrinfo)
        _EGRESS["active"] += 1
    try:
        yield
    finally:
        with _EGRESS_LOCK:
            _EGRESS["active"] -= 1
            if _EGRESS["active"] == 0:
                socket.getaddrinfo = _EGRESS["original_getaddrinfo"]
                _EGRESS["original_getaddrinfo"] = None
                _EGRESS["addresses"] = set()


def build_http_client(transport: httpx.BaseTransport | None = None) -> httpx.Client:
    """Build the only HTTP client: pinned origin, no redirects, no env, no proxy."""

    return httpx.Client(
        base_url=assert_sandbox_base_url(SANDBOX_BASE_URL) + SANDBOX_API_PATH,
        timeout=_TIMEOUT,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
        event_hooks={"request": [_enforce_sandbox_request]},
    )


def assert_client_hardened(http: httpx.Client) -> None:
    if (
        http.follow_redirects is not False
        or http.trust_env is not False
        or str(http.base_url) != SANDBOX_BASE_URL + SANDBOX_API_PATH + "/"
        or _enforce_sandbox_request not in http.event_hooks.get("request", [])
    ):
        raise GuardError("http_client_not_hardened")


def signal_exception(signum: int) -> BaseException:
    """The exception a deferred signal turns into once cleanup is done."""

    if signum == getattr(signal, "SIGINT", None):
        return KeyboardInterrupt()
    return Interrupted(signum)


@contextmanager
def deferred_signals() -> Iterator[list[int]]:
    """Record SIGTERM/SIGHUP/SIGINT instead of acting on them.

    Used while cleanup runs and while the capture file is written, so that a
    second Ctrl-C, a second ``kill`` or a closed terminal cannot abort them.
    The caller decides what a recorded signal means afterwards.
    """

    received: list[int] = []

    def handler(signum: int, frame: FrameType | None) -> None:
        received.append(signum)

    previous: dict[int, Any] = {}
    for name in ("SIGTERM", "SIGHUP", "SIGINT"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous[signum] = signal.signal(signum, handler)
        except ValueError:  # not the main thread
            continue
    try:
        yield received
    finally:
        for signum, old in previous.items():
            signal.signal(signum, old)


@contextmanager
def signals_raise() -> Iterator[None]:
    """Turn SIGTERM/SIGHUP into an exception so ``finally`` cleanup runs."""

    def handler(signum: int, frame: FrameType | None) -> None:
        raise Interrupted(signum)

    previous: dict[int, Any] = {}
    for name in ("SIGTERM", "SIGHUP"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous[signum] = signal.signal(signum, handler)
        except ValueError:  # not the main thread
            continue
    try:
        yield
    finally:
        for signum, old in previous.items():
            signal.signal(signum, old)


# ---------------------------------------------------------------------------
# Synthetic test data
# ---------------------------------------------------------------------------


def _cpf_check_digit(digits: Sequence[int]) -> int:
    weight = len(digits) + 1
    total = sum(d * (weight - i) for i, d in enumerate(digits))
    rest = (total * 10) % 11
    return 0 if rest == 10 else rest


def generate_test_cpf(rng: random.Random) -> str:
    """Generate a check-digit-valid CPF that is not a repeated-digit sequence."""

    while True:
        base = [rng.randrange(10) for _ in range(9)]
        if len(set(base)) > 1:
            break
    first = _cpf_check_digit(base)
    second = _cpf_check_digit(base + [first])
    return "".join(str(d) for d in base + [first, second])


def is_valid_cpf(cpf: str) -> bool:
    if not re.fullmatch(r"\d{11}", cpf) or len(set(cpf)) == 1:
        return False
    digits = [int(c) for c in cpf]
    return (
        _cpf_check_digit(digits[:9]) == digits[9]
        and _cpf_check_digit(digits[:10]) == digits[10]
    )


@dataclass(frozen=True)
class SyntheticIdentity:
    """A clearly synthetic customer; every value is scrubbed from output."""

    name: str
    email: str
    cpf: str
    reference: str

    @classmethod
    def generate(cls, rng: random.Random) -> "SyntheticIdentity":
        tag = "".join(
            rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(10)
        )
        return cls(
            name=TEST_HOLDER_NAME,
            email=f"asaas-capture+{tag}@example.com",
            cpf=generate_test_cpf(rng),
            reference=f"{REFERENCE_PREFIX}{rng.getrandbits(64):016x}",
        )

    def scrub_values(self) -> list[str]:
        return [self.email, self.cpf, TEST_CARD_NUMBER, TEST_PHONE]


def card_expiry(today: date) -> tuple[str, str]:
    """A future expiry (two years ahead), as (MM, YYYY)."""

    return f"{today.month:02d}", str(today.year + 2)


def customer_payload(identity: SyntheticIdentity) -> dict[str, Any]:
    return {
        "name": identity.name,
        "email": identity.email,
        "cpfCnpj": identity.cpf,
        "postalCode": TEST_POSTAL_CODE,
        "addressNumber": TEST_ADDRESS_NUMBER,
        "mobilePhone": TEST_PHONE,
        "externalReference": identity.reference,
        "notificationDisabled": True,
    }


def card_fields(identity: SyntheticIdentity, today: date) -> dict[str, Any]:
    month, year = card_expiry(today)
    return {
        "creditCard": {
            "holderName": identity.name,
            "number": TEST_CARD_NUMBER,
            "expiryMonth": month,
            "expiryYear": year,
            "ccv": TEST_CARD_CCV,
        },
        "creditCardHolderInfo": {
            "name": identity.name,
            "email": identity.email,
            "cpfCnpj": identity.cpf,
            "postalCode": TEST_POSTAL_CODE,
            "addressNumber": TEST_ADDRESS_NUMBER,
            "phone": TEST_PHONE,
            "mobilePhone": TEST_PHONE,
        },
        "remoteIp": TEST_REMOTE_IP,
    }


def payment_payload(
    identity: SyntheticIdentity, customer_id: str, today: date
) -> dict[str, Any]:
    return {
        "customer": customer_id,
        "billingType": "CREDIT_CARD",
        "value": str(PAYMENT_VALUE),
        "dueDate": today.isoformat(),
        "description": f"{identity.reference} one-off",
        "externalReference": identity.reference,
        **card_fields(identity, today),
    }


def subscription_payload(
    identity: SyntheticIdentity, customer_id: str, today: date
) -> dict[str, Any]:
    return {
        "customer": customer_id,
        "billingType": "CREDIT_CARD",
        "value": str(PAYMENT_VALUE),
        "nextDueDate": today.isoformat(),
        "cycle": "MONTHLY",
        "description": f"{identity.reference} subscription",
        "externalReference": identity.reference,
        **card_fields(identity, today),
    }


# ---------------------------------------------------------------------------
# Sanitizer (allowlist)
# ---------------------------------------------------------------------------


def id_hint(value: object) -> str | None:
    if not isinstance(value, str) or not _PROVIDER_ID.fullmatch(value):
        return None
    return f"{value[:4]}...(len={len(value)})"


def _safe_field(name: object) -> str:
    return name if isinstance(name, str) and _FIELD_NAME.fullmatch(name) else "<?>"


def _type_name(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float, Decimal)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "object"
    return "other"


def _date_value(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, str) and _DATE_VALUE.fullmatch(value):
        return value
    return "<unrecognized>"


def _enum_value(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, str) and (
        _UPPER_ENUM.fullmatch(value) or _LOWER_ENUM.fullmatch(value)
    ):
        return value
    return "<unrecognized>"


def _amount_value(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        return "<unrecognized>"
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return "<unrecognized>"
    return str(amount) if amount.is_finite() else "<unrecognized>"


def _count_value(value: object) -> int | None | str:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return "<?>"


def sanitize(obj: object, *, depth: int = 0) -> dict[str, Any]:
    """Keep only allowlisted fields; everything else becomes a type name.

    Customer, card, URL and free-text fields never survive: they are not on
    the allowlist, so only their field name and JSON type are recorded.
    """

    if not isinstance(obj, dict):
        return {"_type": _type_name(obj)}
    kept: dict[str, Any] = {}
    other: dict[str, str] = {}
    for raw_name, value in obj.items():
        name = _safe_field(raw_name)
        if name in DATE_FIELDS or name in EXTRA_DATE_FIELDS:
            kept[name] = _date_value(value)
        elif name in STATUS_FIELDS:
            kept[name] = _enum_value(value)
        elif name in AMOUNT_FIELDS:
            kept[name] = _amount_value(value)
        elif name in ID_FIELDS:
            kept[name] = None if value is None else id_hint(value)
        elif name in BOOL_FIELDS:
            kept[name] = value if isinstance(value, bool) or value is None else "<?>"
        elif name in COUNT_FIELDS:
            kept[name] = _count_value(value)
        elif name == "code" and depth > 0:
            kept[name] = (
                value
                if isinstance(value, str) and _ERROR_CODE.fullmatch(value)
                else "<?>"
            )
        elif name in NESTED_LIST_FIELDS and depth < 2:
            if isinstance(value, list):
                kept[name] = [sanitize(item, depth=depth + 1) for item in value[:20]]
            else:
                kept[name] = None if value is None else {"_type": _type_name(value)}
        elif name == "<?>":
            continue
        else:
            other[name] = _type_name(value)
    if other:
        kept["_other_fields"] = dict(sorted(other.items()))
    return kept


def date_presence(sanitized: Mapping[str, Any]) -> dict[str, object]:
    """For each design-question date: 'absent', None (null) or the value."""

    return {
        name: (sanitized[name] if name in sanitized else "absent")
        for name in DATE_FIELDS
    }


def _day(value: object) -> date | None:
    """The calendar day of a date string; None if missing or impossible."""

    if not isinstance(value, str) or not _DATE_VALUE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def date_relations(sanitized: Mapping[str, Any]) -> dict[str, int | None]:
    """Day offsets of each date relative to ``dateCreated`` (None if missing)."""

    anchor = _day(sanitized.get("dateCreated"))
    relations: dict[str, int | None] = {}
    for name in DATE_FIELDS:
        if name == "dateCreated":
            continue
        day = _day(sanitized.get(name))
        relations[f"{name}-dateCreated_days"] = (
            (day - anchor).days if day is not None and anchor is not None else None
        )
    return relations


def payment_snapshot(sanitized: Mapping[str, Any]) -> dict[str, Any]:
    refunds = sanitized.get("refunds")
    return {
        "status": sanitized.get("status"),
        "value": sanitized.get("value"),
        "netValue": sanitized.get("netValue"),
        "dates": date_presence(sanitized),
        "relations": date_relations(sanitized),
        "refunds": refunds if "refunds" in sanitized else "absent",
    }


def _parse_json(body: bytes) -> Any:
    try:
        return json.loads(body, parse_float=Decimal)
    except (ValueError, UnicodeDecodeError):
        return None


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower()


def error_reason(description: object) -> str:
    """Map a provider description to a fixed reason; the text is not kept."""

    if not isinstance(description, str):
        return "none"
    folded = _fold(description)
    for needle, reason in _ERROR_REASONS:
        if needle in folded:
            return reason
    return "unmapped"


def error_summary(body: bytes) -> list[dict[str, object]]:
    """ASAAS error codes plus a fixed mapped reason; never provider text."""

    parsed = _parse_json(body)
    errors = parsed.get("errors") if isinstance(parsed, dict) else None
    if not isinstance(errors, list):
        return []
    summary: list[dict[str, object]] = []
    for item in errors[:5]:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        summary.append(
            {
                "code": (
                    code
                    if isinstance(code, str) and _ERROR_CODE.fullmatch(code)
                    else "<?>"
                ),
                "reason": error_reason(item.get("description")),
            }
        )
    return summary


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

_ID_HINT = re.compile(r"^[A-Za-z0-9_-]{1,4}\.\.\.\(len=\d{1,3}\)$", re.ASCII)
_AMOUNT = re.compile(r"^-?\d{1,15}(\.\d{1,6})?$", re.ASCII)
_INSTANT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$", re.ASCII)
_OTHER_DAY = re.compile(r"^other:\d{4}-\d{2}-\d{2}$", re.ASCII)
# Shapes that carry no free text: dates, instants, truncated ids.
_TEMP_NAME = re.compile(r"^\.[A-Za-z0-9._-]{1,200}\.[0-9a-f]{16}\.tmp$", re.ASCII)
_STRUCTURAL_SHAPES = (_DATE_VALUE, _INSTANT, _ID_HINT, _OTHER_DAY, _TEMP_NAME)
# Provider field names: lowercase camelCase only (``Owner_Joao`` is refused).
_CAMEL_KEY = re.compile(r"^[a-z][a-zA-Z0-9]{0,63}$", re.ASCII)
_VOCAB_SHAPE = re.compile(
    r"^(?:[a-z][a-z0-9]*(?:[_-][a-z0-9]+)*-?|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*)$",
    re.ASCII,
)
# Amount strings (11+ digit runs included) are printed only under these keys.
_AMOUNT_KEYS = frozenset(set(AMOUNT_FIELDS) | {"requested_value"})
# Numbers are printed only under these keys (plus the date relations).
_NUMBER_KEYS = frozenset(
    set(COUNT_FIELDS)
    | {
        "count",
        "attempt",
        "readback",
        "polls",
        "detailed",
        "owner_card_payments",
        "mutations",
        "sent",
        "http_status",
        "exit_code",
        "network_calls",
        "order",
        "elapsed_s",
        "interval_s",
        "budget_s",
        "stale_after_s",
        "first_errno",
        "errno",
        "signum",
    }
    | {f"{name}-dateCreated_days" for name in DATE_FIELDS}
)
# Step names built at run time from fixed labels.
REFUND_LABELS = (
    "partial-prior-one-off",
    "partial-one-off",
    "full-subscription-first",
    "full-one-off",
)
POLL_LABELS = ("one-off", "subscription-first")
_DYNAMIC_STEPS = frozenset(
    {f"refund-{label}" for label in REFUND_LABELS}
    | {f"refund-{label}-readback" for label in REFUND_LABELS}
    | {f"refund-{label}-refunds-list" for label in REFUND_LABELS}
    | {f"poll-{label}" for label in POLL_LABELS}
    | {f"poll-{label}-final" for label in POLL_LABELS}
    | {f"discovery-owner-payment-{suffix}" for suffix in ("refunds", "status")}
)


# Single words are only printed from these closed sets.
_WORDS = frozenset(
    {
        # outcomes and step names
        "ok",
        "plan",
        "info",
        "skipped",
        "unavailable",
        "incomplete",
        "interrupted",
        "guard",
        "poll",
        "cleanup",
        "discovery",
        "summary",
        "requests",
        "run",
        # JSON type names, ownership/tz/clock labels, error reasons
        "null",
        "bool",
        "number",
        "string",
        "list",
        "object",
        "other",
        "both",
        "neither",
        "absent",
        "none",
        "unmapped",
        "unrecognized",
        # ASAAS object kinds
        "payment",
        "customer",
        "subscription",
        "refund",
        "invoice",
    }
)
_KNOWN_ENUMS = frozenset(
    {
        "PENDING",
        "RECEIVED",
        "CONFIRMED",
        "OVERDUE",
        "REFUNDED",
        "ACTIVE",
        "INACTIVE",
        "EXPIRED",
        "DONE",
        "CANCELLED",
        "CANCELED",
        "AUTHORIZED",
        "PIX",
        "BOLETO",
        "UNDEFINED",
        "WEEKLY",
        "BIWEEKLY",
        "MONTHLY",
        "BIMONTHLY",
        "QUARTERLY",
        "SEMIANNUALLY",
        "YEARLY",
    }
)


def _source_vocabulary() -> tuple[frozenset[str], frozenset[str]]:
    """Every identifier-like string constant and keyword name in this module.

    This is the fixed vocabulary for printed single words, snake_case and
    kebab-case values and for this tool's own dict keys: a value is printed
    only if this source spells it out.  Provider data never extends it.
    """

    try:
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return frozenset(), frozenset()
    words: set[str] = set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if _VOCAB_SHAPE.fullmatch(node.value):
                words.add(node.value)
        elif isinstance(node, ast.keyword) and node.arg is not None:
            names.add(node.arg)
    return frozenset(words), frozenset(names)


_SOURCE_WORDS, _SOURCE_KEYWORDS = _source_vocabulary()
_LITERALS = frozenset(
    {
        SANDBOX_BASE_URL,
        TEST_CARD_SOURCE,
        OWNER_PANEL,
        HISTORY_NOTE,
        "<unrecognized>",
        "<?>",
        "<refused>",
        "[REDACTED]",
    }
    | _NAME_EVENTS
    | _SEND_EVENTS
    | _KNOWN_EXCEPTIONS
    | _ASAAS_ERROR_CODES
    | _ASAAS_MULTI_WORD_ENUMS
    | _DYNAMIC_STEPS
    | _WORDS
    | _KNOWN_ENUMS
    | _SOURCE_WORDS
)
_KNOWN_KEYS = frozenset(
    {"_other_fields", "_type"}
    | _NUMBER_KEYS
    | _SOURCE_KEYWORDS
    | _SOURCE_WORDS
    | set(POLL_LABELS)
)
_PLAN_LITERALS: set[str] = set()
REDACTED = "[REDACTED]"
REFUSED = "<refused>"


class Reporter:
    """JSON-lines output; every value passes scrub then a closed allowlist.

    Records are kept in memory first; a failing output stream (a hung-up
    terminal, a dead ``| tee``) never stops the run: the first failure is
    remembered and reported once through the capture file's ``emit_errors``.
    """

    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream
        self._secrets: list[str] = []
        self._pattern: re.Pattern[str] | None = None
        self._key_runs: set[str] = set()
        self.records: list[dict[str, Any]] = []
        self._stream_failures = 0
        self._first_failure: dict[str, object] | None = None

    def add_secret(self, value: str | None) -> None:
        if not value:
            return
        for variant in (value, json.dumps(value)[1:-1]):
            if variant and variant not in self._secrets:
                self._secrets.append(variant)
        ordered = sorted(self._secrets, key=len, reverse=True)
        self._pattern = re.compile(
            "|".join(re.escape(s) for s in ordered), re.IGNORECASE
        )

    def add_key(self, value: str) -> None:
        """Register the key: exact value plus every 12-character run of it."""

        self.add_secret(value)
        lowered = value.lower()
        for start in range(len(lowered) - KEY_ECHO_RUN + 1):
            self._key_runs.add(lowered[start : start + KEY_ECHO_RUN])

    def has_secret(self, value: str) -> bool:
        return value in self._secrets

    def _echoes_key(self, text: str) -> bool:
        lowered = text.lower()
        if ASAAS_KEY_PREFIX in lowered or "aact_" in lowered:
            return True
        if not self._key_runs:
            return False
        return any(
            lowered[start : start + KEY_ECHO_RUN] in self._key_runs
            for start in range(len(lowered) - KEY_ECHO_RUN + 1)
        )

    def scrub(self, text: str) -> str:
        """Exact secrets out first (longest first, any case), then key echoes."""

        if self._pattern is not None:
            text = self._pattern.sub(REDACTED, text)
        if self._echoes_key(text):
            return REDACTED
        return text

    @staticmethod
    def _allowed(text: str, key: str | None) -> str:
        if text in _LITERALS or text in _PLAN_LITERALS:
            return text
        if any(shape.fullmatch(text) for shape in _STRUCTURAL_SHAPES):
            return text
        if key in _AMOUNT_KEYS and _AMOUNT.fullmatch(text):
            return text
        return REFUSED

    def _clean_key(self, name: object) -> str:
        """Keys: this tool's own names or lowercase camelCase field names."""

        scrubbed = self.scrub(str(name))
        if scrubbed == REDACTED:
            return scrubbed
        if scrubbed in _KNOWN_KEYS or _CAMEL_KEY.fullmatch(scrubbed):
            return scrubbed
        return REFUSED

    def _clean_number(self, value: int | float, key: str | None) -> object:
        if isinstance(value, float) and not (value == value and abs(value) < 1e18):
            return REFUSED  # NaN or infinite: never valid JSON
        if self.scrub(str(value)) != str(value):
            return REDACTED
        return value if key in _NUMBER_KEYS else REFUSED

    def clean(self, value: object, key: str | None = None) -> object:
        """Scrub, then allowlist, every value (keys included) in a record."""

        if isinstance(value, str):
            scrubbed = self.scrub(value)
            return scrubbed if scrubbed == REDACTED else self._allowed(scrubbed, key)
        if isinstance(value, dict):
            cleaned: dict[str, object] = {}
            for name, item in value.items():
                safe = self._clean_key(name)
                cleaned[safe] = self.clean(item, safe)
            return cleaned
        if isinstance(value, (list, tuple)):
            return [self.clean(item, key) for item in value]
        if value is None or isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return self._clean_number(value, key)
        if isinstance(value, Decimal):
            return self.clean(str(value), key)
        return REFUSED

    def record(self, step: str, outcome: str, **fields: object) -> dict[str, Any]:
        """Clean a record and keep it in memory without printing it."""

        try:
            cleaned = self.clean({"step": step, "outcome": outcome, **fields})
        except Exception:  # noqa: BLE001 - emit never raises on content
            cleaned = {"step": REFUSED, "outcome": "emit_failed"}
        record = cleaned if isinstance(cleaned, dict) else {"step": REFUSED}
        self.records.append(record)
        return record

    def print_record(self, record: Mapping[str, Any]) -> None:
        """Print one cleaned record; a failing stream is remembered, not raised."""

        if self._stream_failures:
            self._stream_failures += 1
            return
        stream = self._stream if self._stream is not None else sys.stdout
        try:
            stream.write(json.dumps(record, sort_keys=True) + "\n")
            stream.flush()
        except (OSError, ValueError) as exc:
            self._stream_failures = 1
            self._first_failure = {
                "first_exception": exception_label(exc),
                "first_errno": exc.errno if isinstance(exc, OSError) else None,
            }
            if self._stream is None:
                silence_stdout()

    def emit(self, step: str, outcome: str, **fields: object) -> None:
        self.print_record(self.record(step, outcome, **fields))

    def emit_errors(self) -> dict[str, object]:
        """How many lines could not be printed, and why the first one failed."""

        return {"count": self._stream_failures, **(self._first_failure or {})}


def silence_stdout() -> None:
    """Point the real stdout at /dev/null after it failed (``| head`` exited).

    Later writes and Python's own flush at interpreter exit then succeed, so
    a dead pipe cannot turn the exit code into 120: the process always exits
    with the code its ``summary`` record states.
    """

    try:
        fd = sys.stdout.fileno()
    except (AttributeError, OSError, ValueError):
        return
    try:
        null = os.open(os.devnull, os.O_WRONLY)
    except OSError:
        return
    try:
        os.dup2(null, fd)
    except OSError:
        pass
    finally:
        os.close(null)


def check_capture_target(path: Path) -> None:
    """Refuse before any request if the capture file cannot be created."""

    parent = path.parent
    if not parent.is_dir():
        raise GuardError("capture_dir_missing")
    if not os.access(parent, os.W_OK | os.X_OK):
        raise GuardError("capture_dir_not_writable")


class CaptureFileError(OSError):
    """The capture file could not be put in place.

    ``temp`` is the complete (or, for ``temp_write_failed``, partial) temp
    file that was kept next to the target, or None if none was created.
    """

    def __init__(self, reason: str, temp: Path | None, cause: OSError | None) -> None:
        super().__init__(cause.errno if cause is not None else errno.EEXIST, reason)
        self.reason = reason
        self.temp = temp


def write_capture_file(
    path: Path, records: list[dict[str, Any]], out: Reporter
) -> None:
    """Write the sanitized records atomically, exclusively, with mode 0600.

    The text goes to a new temp file in the same directory, created with
    O_EXCL|O_NOFOLLOW and mode 0600, is fsynced, and is then hard-linked to
    ``path``; a link never replaces an existing file.  Where hard links are
    not supported (EPERM/ENOTSUP/EOPNOTSUPP, e.g. some DrvFs mounts) it falls
    back to a rename, but only while nothing exists at ``path``.  On any
    other failure the temp file is kept, never deleted, and
    :class:`CaptureFileError` names it: the capture is never lost.
    """

    payload = {
        "schema": CAPTURE_SCHEMA,
        "records": records,
        "emit_errors": out.clean(out.emit_errors()),
    }
    text = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    # At most 200 characters of the name, so the temp name stays below 255.
    temp = path.with_name(f".{path.name[:200]}.{secrets.token_hex(8)}.tmp")
    try:
        fd = os.open(
            temp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except OSError as exc:
        raise CaptureFileError("temp_create_failed", None, exc) from exc
    try:
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise CaptureFileError("temp_write_failed", temp, exc) from exc
    try:
        os.link(temp, path)
    except FileExistsError as exc:
        raise CaptureFileError("target_exists", temp, exc) from exc
    except OSError as exc:
        if exc.errno not in (errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP):
            raise CaptureFileError("link_failed", temp, exc) from exc
        # No hard links on this filesystem: fall back to a no-clobber rename.
        if os.path.lexists(path):
            raise CaptureFileError("target_exists", temp, None) from None
        try:
            os.rename(temp, path)
        except OSError as rename_exc:
            raise CaptureFileError("rename_failed", temp, rename_exc) from rename_exc
        return
    try:
        os.unlink(temp)  # the link is in place; the temp name is redundant
    except OSError:
        pass


def capture_failure(exc: OSError) -> dict[str, object]:
    """What a failed capture write reports: reason, kept temp file, errno."""

    if isinstance(exc, CaptureFileError):
        return {
            "reason": exc.reason,
            "temp_file": exc.temp.name if exc.temp is not None else None,
            "errno": exc.errno,
        }
    return {"reason": "write_error", "temp_file": None, "errno": exc.errno}


# ---------------------------------------------------------------------------
# Pacing and polling
# ---------------------------------------------------------------------------


@dataclass
class Pacer:
    """At most one request per ``interval`` seconds."""

    clock: Callable[[], float]
    sleep: Callable[[float], None]
    interval: float = MIN_REQUEST_INTERVAL_S
    _last: float | None = None

    def wait(self) -> None:
        now = self.clock()
        if self._last is not None:
            remaining = self.interval - (now - self._last)
            if remaining > 0:
                self.sleep(remaining)
                now = self.clock()
        self._last = now


@dataclass(frozen=True)
class PollPolicy:
    interval: float = POLL_INTERVAL_S
    budget: float = POLL_BUDGET_S
    stale_after: float = POLL_STALE_S


def poll_until(
    read: Callable[[], Mapping[str, str | None]],
    *,
    policy: PollPolicy,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    on_change: Callable[[float, Mapping[str, str | None]], None],
) -> dict[str, Any]:
    """Poll ``read`` (a {name: status} map) until all RECEIVED, stale or budget.

    ``read`` is called once immediately; ``on_change`` is called for the first
    observation and every time the observed map changes.  The next poll only
    starts if the interval plus the last read's latency still fits the budget.
    """

    start = clock()
    observed = dict(read())
    read_cost = clock() - start
    last_change = start
    on_change(0.0, observed)
    polls = 1
    reason = "budget_exhausted"
    while True:
        if observed and all(v == "RECEIVED" for v in observed.values()):
            reason = "all_received"
            break
        now = clock()
        if now - last_change >= policy.stale_after:
            reason = "no_change"
            break
        if now - start + policy.interval + read_cost > policy.budget:
            reason = "budget_exhausted"
            break
        sleep(policy.interval)
        read_started = clock()
        current = dict(read())
        read_cost = clock() - read_started
        polls += 1
        if current != observed:
            observed = current
            last_change = clock()
            on_change(last_change - start, observed)
    return {
        "reason": reason,
        "polls": polls,
        "elapsed_s": round(clock() - start, 1),
        "final": observed,
    }


# ---------------------------------------------------------------------------
# Time evidence
# ---------------------------------------------------------------------------


def instant(moment: datetime) -> dict[str, str]:
    """One wall-clock instant in UTC and in America/Sao_Paulo."""

    utc = moment.astimezone(timezone.utc)
    local = moment.astimezone(SAO_PAULO)
    return {
        "utc": utc.isoformat(timespec="seconds"),
        "sao_paulo": local.isoformat(timespec="seconds"),
        "utc_date": utc.date().isoformat(),
        "sao_paulo_date": local.date().isoformat(),
    }


def tz_compare(
    sanitized: Mapping[str, Any],
    moment: Mapping[str, str],
    fields: Sequence[str] = DATE_FIELDS,
) -> dict[str, str]:
    """For each date field: does it equal the SP date or the UTC date?"""

    sp, utc = moment["sao_paulo_date"], moment["utc_date"]
    result: dict[str, str] = {}
    for name in fields:
        value = sanitized.get(name)
        if value is None:
            result[name] = "null" if name in sanitized else "absent"
            continue
        day = _day(value)
        if day is None:
            result[name] = "unrecognized"
        elif day.isoformat() == sp and day.isoformat() == utc:
            result[name] = "both"
        elif day.isoformat() == sp:
            result[name] = "sao_paulo_date"
        elif day.isoformat() == utc:
            result[name] = "utc_date"
        else:
            result[name] = f"other:{day.isoformat()}"
    return result


def wall_clock_match(value: object, before: datetime, after: datetime) -> str:
    """Is a naive provider timestamp São Paulo or UTC wall-clock time?"""

    if not isinstance(value, str) or not _DATE_VALUE.fullmatch(value):
        return "unrecognized"
    try:
        stamp = datetime.fromisoformat(value.replace(" ", "T"))
    except ValueError:
        return "unrecognized"
    if len(value) <= 10:
        return "date_only"
    tolerance = timedelta(seconds=WALL_CLOCK_TOLERANCE_S)
    matches = []
    for label, zone in (
        ("sao_paulo_wall_clock", SAO_PAULO),
        ("utc_wall_clock", timezone.utc),
    ):
        low = before.astimezone(zone).replace(tzinfo=None) - tolerance
        high = after.astimezone(zone).replace(tzinfo=None) + tolerance
        if low <= stamp <= high:
            matches.append(label)
    return matches[0] if len(matches) == 1 else ("both" if matches else "neither")


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Recorded:
    status: int
    body: bytes

    @property
    def json(self) -> Any:
        return _parse_json(self.body)


@dataclass
class Created:
    customers: list[str] = field(default_factory=list)
    payments: list[str] = field(default_factory=list)
    subscriptions: list[str] = field(default_factory=list)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Capture:
    key: SandboxKey
    http: httpx.Client
    identity: SyntheticIdentity
    out: Reporter
    today: date
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    poll_policy: PollPolicy = field(default_factory=PollPolicy)
    created: Created = field(default_factory=Created)
    requests_sent: int = 0
    _pacer: Pacer | None = None
    wall: Callable[[], datetime] = _utc_now
    last_moments: tuple[datetime, datetime] | None = None
    prior_payments: list[str] = field(default_factory=list)
    allow_prior_partial: bool = False

    def __post_init__(self) -> None:
        self._pacer = Pacer(clock=self.clock, sleep=self.sleep)

    # -- transport ---------------------------------------------------------

    def send(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
    ) -> Recorded:
        if not _SAFE_REQUEST_PATH.fullmatch(path) or ".." in path:
            raise GuardError("request_path_invalid")
        assert self._pacer is not None
        self._pacer.wait()
        headers = {
            AUTH_HEADER: self.key.reveal(),
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        content = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            content = json.dumps(body, default=str).encode()
        request = self.http.build_request(
            method, path, content=content, headers=headers, params=params
        )
        self.requests_sent += 1
        before = self.wall()
        try:
            response = self.http.send(request)
        except httpx.HTTPError as exc:
            raise ProviderError(
                "transport_error", details={"exception": exception_label(exc)}
            ) from exc
        self.last_moments = (before, self.wall())
        content_bytes = response.content
        if len(content_bytes) > MAX_RESPONSE_BYTES:
            raise ProviderError("response_too_large")
        return Recorded(status=response.status_code, body=content_bytes)

    def instants(self) -> dict[str, dict[str, str]]:
        """The last request's start/end instants, in UTC and São Paulo time."""

        assert self.last_moments is not None
        before, after = self.last_moments
        return {"before": instant(before), "after": instant(after)}

    def _ok(self, rec: Recorded, step: str) -> dict[str, Any]:
        if 200 <= rec.status < 300 and isinstance(rec.json, dict):
            return rec.json
        if rec.status == 401:
            raise ProviderError("sandbox_key_rejected", step=step)
        raise ProviderError(
            "provider_status_unexpected",
            details={"http_status": rec.status, "errors": error_summary(rec.body)},
            step=step,
        )

    def _track(self, bucket: list[str], payload: Mapping[str, Any]) -> str:
        """Track a created object only if it carries this run's tag."""

        raw_id = payload.get("id")
        if not isinstance(raw_id, str) or not _PROVIDER_ID.fullmatch(raw_id):
            raise ProviderError("created_id_missing")
        if payload.get("externalReference") != self.identity.reference:
            # Never adopt an object whose tag we cannot see: it stays untouched.
            raise ProviderError("created_tag_mismatch", details={"id": id_hint(raw_id)})
        if raw_id not in bucket:
            bucket.append(raw_id)
        return raw_id

    # -- ownership -----------------------------------------------------------

    def ownership(self, raw: Mapping[str, Any]) -> str:
        """Classify an object: this run's tag, an older capture run, or owner.

        A payment generated by a subscription this run created (and verified
        as tagged) also belongs to this run.
        """

        reference = raw.get("externalReference")
        if reference == self.identity.reference:
            return THIS_RUN
        subscription = raw.get("subscription")
        if (
            raw.get("object") == "payment"
            and isinstance(subscription, str)
            and subscription in self.created.subscriptions
        ):
            return THIS_RUN
        if isinstance(reference, str) and reference.startswith(REFERENCE_PREFIX):
            return PRIOR_RUN
        return OWNER_PANEL

    def verify_ours(
        self, path: str, step: str, *, allow_prior: bool = False
    ) -> tuple[bool, str]:
        """Re-read ``path`` and confirm it belongs to this run before mutating."""

        rec = self.send("GET", path)
        raw = rec.json
        if rec.status == 404:
            return False, "already_gone"
        if not (200 <= rec.status < 300 and isinstance(raw, dict)):
            return False, "readback_failed"
        if raw.get("deleted") is True:
            return False, "already_deleted"
        owner = self.ownership(raw)
        tagged_prior = (
            allow_prior
            and owner == PRIOR_RUN
            and isinstance(raw.get("externalReference"), str)
            and bool(_CAPTURE_TAG.fullmatch(raw["externalReference"]))
        )
        if owner != THIS_RUN and not tagged_prior:
            self.out.emit(step, "refused_not_ours", ownership=owner)
            return False, "not_this_run"
        return True, "ours"

    def get_payment(self, payment_id: str, step: str) -> dict[str, Any]:
        payload = self._ok(self.send("GET", f"/payments/{payment_id}"), step)
        return sanitize(payload)

    def emit_payment(
        self, step: str, sanitized: Mapping[str, Any], **extra: object
    ) -> None:
        self.out.emit(
            step,
            "ok",
            payment_id=sanitized.get("id"),
            snapshot=payment_snapshot(sanitized),
            sanitized=sanitized,
            **extra,
        )

    # -- read-only discovery of objects this tool did not create ----------------

    def _read_list(
        self, step: str, path: str, params: Mapping[str, str]
    ) -> list[dict[str, Any]]:
        rec = self.send("GET", path, params=params)
        listing = rec.json
        if not (200 <= rec.status < 300 and isinstance(listing, dict)):
            self.out.emit(
                step,
                "unavailable",
                http_status=rec.status,
                errors=error_summary(rec.body),
            )
            return []
        data = listing.get("data")
        items = (
            [i for i in data if isinstance(i, dict)] if isinstance(data, list) else []
        )
        classified = [{"ownership": self.ownership(i), **sanitize(i)} for i in items]
        classified.sort(key=lambda i: str(i.get("dateCreated") or ""), reverse=True)
        extra: dict[str, object] = {}
        total = listing.get("totalCount")
        if isinstance(total, int) and not isinstance(total, bool):
            extra["totalCount"] = total
        has_more = listing.get("hasMore")
        if isinstance(has_more, bool):
            extra["hasMore"] = has_more
        self.out.emit(step, "ok", count=len(items), items=classified, **extra)
        return items

    def discover(self) -> list[str]:
        """List existing customers/subscriptions/card payments; mutate nothing.

        For every CREDIT_CARD payment not created by this tool, capture the
        sanitized readback, its refunds list and its status endpoint.  ASAAS
        documents no status/date history endpoint, so only current state is
        observable.
        """

        limits = DISCOVERY_LIMITS
        self._read_list(
            "discovery-customers", "/customers", {"limit": str(limits["customers"])}
        )
        subscriptions = self._read_list(
            "discovery-subscriptions",
            "/subscriptions",
            {"limit": str(limits["subscriptions"])},
        )
        payments = self._read_list(
            "discovery-payments",
            "/payments",
            {"limit": str(limits["payments"])},
        )
        owner_ids: list[str] = []

        def add(item: Mapping[str, Any]) -> None:
            raw = item.get("id")
            if (
                self.ownership(item) == OWNER_PANEL
                and item.get("billingType") == "CREDIT_CARD"
                and isinstance(raw, str)
                and _PROVIDER_ID.fullmatch(raw)
                and raw not in owner_ids
            ):
                owner_ids.append(raw)

        for item in payments:
            add(item)
            raw_id = item.get("id")
            if (
                self.ownership(item) == PRIOR_RUN
                and item.get("billingType") == "CREDIT_CARD"
                and item.get("status") == "CONFIRMED"
                and item.get("subscription") is None
                and isinstance(raw_id, str)
                and _PROVIDER_ID.fullmatch(raw_id)
            ):
                self.prior_payments.append(raw_id)
        for sub in subscriptions[:DISCOVERY_DETAIL_CAP]:
            sub_id = sub.get("id")
            if (
                self.ownership(sub) != OWNER_PANEL
                or sub.get("billingType") != "CREDIT_CARD"
                or not isinstance(sub_id, str)
                or not _PROVIDER_ID.fullmatch(sub_id)
            ):
                continue
            for item in self._read_list(
                "discovery-owner-subscription-payments",
                "/payments",
                {"subscription": sub_id, "limit": "10"},
            ):
                add(item)
        for payment_id in owner_ids[:DISCOVERY_DETAIL_CAP]:
            rec = self.send("GET", f"/payments/{payment_id}")
            if not (200 <= rec.status < 300 and isinstance(rec.json, dict)):
                self.out.emit(
                    "discovery-owner-payment",
                    "unavailable",
                    payment_id=id_hint(payment_id),
                    http_status=rec.status,
                )
                continue
            self.emit_payment(
                "discovery-owner-payment",
                sanitize(rec.json),
                ownership=OWNER_PANEL,
            )
            for suffix in ("refunds", "status"):
                extra = self.send("GET", f"/payments/{payment_id}/{suffix}")
                self.out.emit(
                    f"discovery-owner-payment-{suffix}",
                    "ok" if 200 <= extra.status < 300 else "unavailable",
                    payment_id=id_hint(payment_id),
                    http_status=extra.status,
                    sanitized=sanitize(extra.json),
                )
        self.out.emit(
            "discovery",
            "ok",
            owner_card_payments=len(owner_ids),
            detailed=min(len(owner_ids), DISCOVERY_DETAIL_CAP),
            history_endpoint=HISTORY_NOTE,
            mutations=0,
        )
        return owner_ids

    # -- scenarios -----------------------------------------------------------

    def create_customer(self) -> str:
        step = "customer-create"
        payload = self._ok(
            self.send("POST", "/customers", body=customer_payload(self.identity)), step
        )
        moment = self.instants()
        customer_id = self._track(self.created.customers, payload)
        clean = sanitize(payload)
        self.out.emit(
            step,
            "ok",
            customer_id=id_hint(customer_id),
            create_instant=moment,
            tz_compare=tz_compare(clean, moment["before"], ("dateCreated",)),
        )
        return customer_id

    def create_one_off(self, customer_id: str) -> str:
        step = "payment-create"
        body = payment_payload(self.identity, customer_id, self.today)
        payload = self._ok(self.send("POST", "/payments", body=body), step)
        moment = self.instants()
        payment_id = self._track(self.created.payments, payload)
        clean = sanitize(payload)
        self.emit_payment(
            step,
            clean,
            create_instant=moment,
            tz_compare=tz_compare(clean, moment["before"]),
        )
        self.emit_payment(
            "payment-readback", self.get_payment(payment_id, "payment-readback")
        )
        return payment_id

    def create_subscription(self, customer_id: str) -> tuple[str, str | None]:
        step = "subscription-create"
        body = subscription_payload(self.identity, customer_id, self.today)
        payload = self._ok(self.send("POST", "/subscriptions", body=body), step)
        moment = self.instants()
        subscription_id = self._track(self.created.subscriptions, payload)
        clean = sanitize(payload)
        self.out.emit(
            step,
            "ok",
            create_instant=moment,
            tz_compare=tz_compare(clean, moment["before"], ("dateCreated",)),
            sanitized=clean,
        )
        first: str | None = None
        for attempt in range(SUBSCRIPTION_LIST_ATTEMPTS):
            listing = self._ok(
                self.send("GET", "/payments", params={"subscription": subscription_id}),
                "subscription-payments-list",
            )
            data = listing.get("data")
            items = (
                [i for i in data if isinstance(i, dict)]
                if isinstance(data, list)
                else []
            )
            items = [i for i in items if self.ownership(i) == THIS_RUN]
            for item in items:
                raw = item.get("id")
                if isinstance(raw, str) and _PROVIDER_ID.fullmatch(raw):
                    if raw not in self.created.payments:
                        self.created.payments.append(raw)
            self.out.emit(
                "subscription-payments-list",
                "ok",
                attempt=attempt + 1,
                count=len(items),
                payments_carry_tag=[
                    i.get("externalReference") == self.identity.reference for i in items
                ],
                sanitized=sanitize(listing),
            )
            if items:
                ordered = sorted(items, key=lambda i: str(i.get("dueDate") or ""))
                first_raw = ordered[0].get("id")
                first = (
                    first_raw
                    if isinstance(first_raw, str) and _PROVIDER_ID.fullmatch(first_raw)
                    else None
                )
                break
            if attempt + 1 < SUBSCRIPTION_LIST_ATTEMPTS:
                self.sleep(SUBSCRIPTION_LIST_INTERVAL_S)
        sub = self._ok(
            self.send("GET", f"/subscriptions/{subscription_id}"),
            "subscription-readback",
        )
        self.out.emit("subscription-readback", "ok", sanitized=sanitize(sub))
        if first is not None:
            first_clean = self.get_payment(first, "subscription-first-payment-readback")
            # The first payment is created by the subscription POST above.
            self.emit_payment(
                "subscription-first-payment-readback",
                first_clean,
                create_instant=moment,
                tz_compare=tz_compare(first_clean, moment["before"]),
            )
        return subscription_id, first

    def poll_statuses(self, payments: Mapping[str, str]) -> dict[str, Any]:
        """Poll {label: payment_id} for status changes within the budget."""

        latest: dict[str, dict[str, Any]] = {}

        def read() -> dict[str, str | None]:
            statuses: dict[str, str | None] = {}
            for label, payment_id in payments.items():
                sanitized = self.get_payment(payment_id, f"poll-{label}")
                latest[label] = sanitized
                status = sanitized.get("status")
                statuses[label] = status if isinstance(status, str) else None
            return statuses

        def on_change(elapsed: float, observed: Mapping[str, str | None]) -> None:
            for label, sanitized in latest.items():
                self.emit_payment(
                    f"poll-{label}",
                    sanitized,
                    elapsed_s=round(elapsed, 1),
                    statuses=dict(observed),
                )

        result = poll_until(
            read,
            policy=self.poll_policy,
            clock=self.clock,
            sleep=self.sleep,
            on_change=on_change,
        )
        for label, sanitized in latest.items():
            self.emit_payment(f"poll-{label}-final", sanitized)
        self.out.emit("poll", "ok", **result)
        return result

    def _refund_evidence(
        self, sanitized: Mapping[str, Any], moments: tuple[datetime, datetime]
    ) -> list[dict[str, str]]:
        """For each refund entry: does its timestamp follow SP or UTC?"""

        before, after = moments
        refund_day = instant(before)
        entries = sanitized.get("refunds")
        evidence: list[dict[str, str]] = []
        if not isinstance(entries, list):
            return evidence
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            stamp = entry.get("dateCreated")
            evidence.append(
                {
                    **tz_compare(entry, refund_day, ("dateCreated",)),
                    "clock": wall_clock_match(stamp, before, after),
                }
            )
        return evidence

    def refund(
        self,
        label: str,
        payment_id: str,
        value: Decimal | None,
        *,
        allow_prior: bool = False,
    ) -> None:
        step = f"refund-{label}"
        ours, reason = self.verify_ours(
            f"/payments/{payment_id}", step, allow_prior=allow_prior
        )
        if not ours:
            self.out.emit(step, "skipped", reason=reason)
            return
        body: dict[str, Any] = {"description": f"{self.identity.reference} refund"}
        if value is not None:
            body["value"] = str(value)
        rec = self.send("POST", f"/payments/{payment_id}/refund", body=body)
        refund_instant = self.instants()
        assert self.last_moments is not None
        moments = self.last_moments
        requested = None if value is None else str(value)
        if not (200 <= rec.status < 300 and isinstance(rec.json, dict)):
            self.out.emit(
                step,
                "provider_error",
                http_status=rec.status,
                errors=error_summary(rec.body),
                requested_value=requested,
                refund_instant=refund_instant,
            )
            return
        clean = sanitize(rec.json)
        self.emit_payment(
            step,
            clean,
            requested_value=requested,
            refund_instant=refund_instant,
            refund_tz=self._refund_evidence(clean, moments),
        )
        for index in range(REFUND_READBACKS):
            if index:
                self.sleep(REFUND_READBACK_INTERVAL_S)
            readback = self.get_payment(payment_id, f"{step}-readback")
            self.emit_payment(
                f"{step}-readback",
                readback,
                readback=index + 1,
                refund_tz=self._refund_evidence(readback, moments),
            )
        listing = self.send("GET", f"/payments/{payment_id}/refunds")
        if 200 <= listing.status < 300 and isinstance(listing.json, dict):
            listed = sanitize(listing.json)
            self.out.emit(
                f"{step}-refunds-list",
                "ok",
                sanitized=listed,
                refund_tz=self._refund_evidence(
                    {"refunds": listed.get("data")}, moments
                ),
            )
        else:
            self.out.emit(
                f"{step}-refunds-list",
                "unavailable",
                http_status=listing.status,
                errors=error_summary(listing.body),
            )

    # -- cleanup -------------------------------------------------------------

    def cleanup(self) -> dict[str, Any]:
        """Delete this run's tagged objects; always report what is left.

        If cleanup itself is interrupted (an exception raised from code: real
        signals are deferred by :meth:`run`), every object not yet handled is
        listed as remaining before the interruption propagates.
        """

        deleted: list[dict[str, object]] = []
        remaining: list[dict[str, object]] = []
        plan = (
            [("subscription", "/subscriptions", i) for i in self.created.subscriptions]
            + [("payment", "/payments", i) for i in self.created.payments]
            + [("customer", "/customers", i) for i in self.created.customers]
        )
        index = 0
        try:
            for index, (kind, prefix, object_id) in enumerate(plan):
                self._cleanup_one(kind, prefix, object_id, deleted, remaining)
        except BaseException as exc:
            interrupted = isinstance(exc, (KeyboardInterrupt, Interrupted))
            reason = "cleanup_interrupted" if interrupted else "cleanup_crashed"
            for kind, _prefix, object_id in plan[index:]:
                remaining.append(
                    {"kind": kind, "id": id_hint(object_id), "reason": reason}
                )
            self.out.emit(
                "cleanup",
                "interrupted" if interrupted else "cleanup_crashed",
                deleted=deleted,
                remaining=remaining,
            )
            raise
        result = {"deleted": deleted, "remaining": remaining}
        self.out.emit("cleanup", "ok" if not remaining else "incomplete", **result)
        return result

    def _cleanup_one(
        self,
        kind: str,
        prefix: str,
        object_id: str,
        deleted: list[dict[str, object]],
        remaining: list[dict[str, object]],
    ) -> None:
        entry: dict[str, object] = {"kind": kind, "id": id_hint(object_id)}
        try:
            # Only objects that still carry this run's tag are deleted.
            ours, reason = self.verify_ours(f"{prefix}/{object_id}", "cleanup")
            if not ours:
                if reason in ("already_gone", "already_deleted"):
                    deleted.append({**entry, "note": reason})
                else:
                    remaining.append({**entry, "reason": reason})
                return
            rec = self.send("DELETE", f"{prefix}/{object_id}")
        except CaptureError as exc:
            remaining.append({**entry, "reason": exc.code})
            return
        parsed = rec.json if isinstance(rec.json, dict) else {}
        if 200 <= rec.status < 300 and parsed.get("deleted") is True:
            deleted.append(entry)
        else:
            remaining.append(
                {**entry, "http_status": rec.status, "errors": error_summary(rec.body)}
            )

    # -- orchestration -------------------------------------------------------

    def run(self) -> dict[str, Any]:
        """All scenarios, then cleanup, whatever happened before it.

        Cleanup also runs after KeyboardInterrupt, ``Interrupted`` (SIGTERM or
        SIGHUP via :func:`signals_raise`) and unexpected exceptions, and runs
        with SIGTERM/SIGHUP/SIGINT deferred, so a second signal cannot abort
        it.  The first interruption is re-raised once cleanup is done; a
        signal that arrived only during cleanup is raised after it.  Only a
        hard kill (SIGKILL, power loss) skips cleanup.
        """

        error: CaptureError | None = None
        pending: BaseException | None = None
        try:
            self.discover()
            if self.allow_prior_partial:
                for prior in self.prior_payments[:1]:
                    self.refund(
                        "partial-prior-one-off",
                        prior,
                        PARTIAL_REFUND_VALUE,
                        allow_prior=True,
                    )
            customer_id = self.create_customer()
            one_off = self.create_one_off(customer_id)
            _, first = self.create_subscription(customer_id)
            targets = {"one-off": one_off}
            if first is not None:
                targets["subscription-first"] = first
            self.poll_statuses(targets)
            self.refund("partial-one-off", one_off, PARTIAL_REFUND_VALUE)
            if first is not None:
                self.refund("full-subscription-first", first, None)
            else:
                self.refund("full-one-off", one_off, None)
        except CaptureError as exc:
            error = exc
            emit_error(self.out, exc, default_step="run")
        except BaseException as exc:  # interrupts and bugs: clean up first
            pending = exc
        with deferred_signals() as late:
            try:
                cleanup = self.cleanup()
            except Exception as exc:  # noqa: BLE001 - reported, original kept
                self.out.emit(
                    "cleanup", "cleanup_crashed", exception=exception_label(exc)
                )
                cleanup = {"deleted": [], "remaining": [{"reason": "cleanup_crashed"}]}
        if pending is not None:
            raise pending
        if late:
            raise signal_exception(late[0])
        if error is not None:
            raise error
        if cleanup["remaining"]:
            raise ProviderError("cleanup_incomplete", step="cleanup")
        return cleanup


def emit_error(out: Reporter, exc: CaptureError, *, default_step: str) -> None:
    exc.reported = True
    out.emit(
        exc.step or default_step,
        exc.outcome,
        code=exc.code,
        exit_code=exc.exit_code,
        **exc.details,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

PLAN: tuple[dict[str, object], ...] = (
    {
        "order": 0,
        "request": "GET /v3/customers, /v3/subscriptions, "
        "/v3/payments (+ details of owner CREDIT_CARD payments)",
        "creates": None,
        "mutates_foreign_objects": False,
    },
    {"order": 1, "request": "POST /v3/customers", "creates": "customer"},
    {
        "order": 2,
        "request": "POST /v3/payments (CREDIT_CARD, R$10.00)",
        "creates": "payment",
    },
    {"order": 3, "request": "GET /v3/payments/{id}", "creates": None},
    {
        "order": 4,
        "request": "POST /v3/subscriptions (CREDIT_CARD, MONTHLY)",
        "creates": "subscription",
    },
    {"order": 5, "request": "GET /v3/payments?subscription={id}", "creates": None},
    {
        "order": 6,
        "request": "GET /v3/subscriptions/{id} + GET first payment",
        "creates": None,
    },
    {
        "order": 7,
        "request": "poll GET /v3/payments/{id} for both",
        "bounds": {
            "interval_s": POLL_INTERVAL_S,
            "budget_s": POLL_BUDGET_S,
            "stale_after_s": POLL_STALE_S,
        },
        "creates": None,
    },
    {
        "order": 8,
        "request": f"POST /v3/payments/{{one-off}}/refund value={PARTIAL_REFUND_VALUE}",
        "creates": "refund",
    },
    {
        "order": 9,
        "request": "POST /v3/payments/{subscription-first}/refund (full)",
        "creates": "refund",
    },
    {
        "order": 10,
        "request": "GET /v3/payments/{id} x3 + GET /v3/payments/{id}/refunds",
        "creates": None,
    },
    {
        "order": 11,
        "request": "DELETE subscription, payments, customer (finally), "
        "each only after a GET confirms this run's tag",
        "creates": None,
    },
)
_PLAN_LITERALS.update(
    str(entry["request"]) for entry in PLAN if isinstance(entry.get("request"), str)
)


def plan_lines(today: date) -> list[dict[str, object]]:
    return [
        *PLAN,
        {
            "due_date": today.isoformat(),
            "test_card_source": TEST_CARD_SOURCE,
            "tag_prefix": REFERENCE_PREFIX,
        },
    ]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="asaas_sandbox_card_capture",
        description=(
            "Capture sanitized ASAAS sandbox card-payment readbacks "
            f"({SANDBOX_BASE_URL} only). Dry run unless --run is given."
        ),
    )
    parser.add_argument(
        "--run", action="store_true", help="perform the live sandbox capture"
    )
    parser.add_argument(
        "--partial-refund-prior",
        action="store_true",
        help="with --run: also try a partial refund on one CONFIRMED one-off "
        "payment left by an earlier run (must carry a neurostack-capture- tag)",
    )
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help="with --run: only the read-only discovery (creates nothing)",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(__file__).resolve().parents[1] / ".env",
        help=f"file holding {SANDBOX_KEY_ENV} (only that line is parsed)",
    )
    parser.add_argument(
        "--capture-file",
        type=Path,
        default=None,
        help="write the sanitized JSON records here (created exclusively, 0600)",
    )
    return parser


def _env_mode_warning(env_file: Path) -> bool:
    try:
        return env_file_mode_warning(os.stat(env_file).st_mode)
    except OSError:
        return False


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.BaseTransport | None = None,
    stdout: TextIO | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    poll_policy: PollPolicy | None = None,
    reporter: Reporter | None = None,
    wall: Callable[[], datetime] = _utc_now,
) -> int:
    args = _parser().parse_args(argv)
    out = reporter if reporter is not None else Reporter(stdout)
    today = wall().astimezone(SAO_PAULO).date()
    if not args.run:
        # Local file read only: report a boolean, never the key or its prefix.
        try:
            out.add_key(load_sandbox_key(args.env_file).reveal())
            key_check = "ok"
        except GuardError as exc:
            key_check = exc.code
        out.emit(
            "dry-run",
            "plan",
            base_url=SANDBOX_BASE_URL,
            is_sandbox_key=key_check == "ok",
            key_check=key_check,
            env_file_mode_warning=_env_mode_warning(args.env_file),
            network_calls=0,
            plan=plan_lines(today),
        )
        return EXIT_OK if key_check == "ok" else EXIT_GUARD
    identity = SyntheticIdentity.generate(rng or random.SystemRandom())
    for value in identity.scrub_values():
        out.add_secret(value)
    code = EXIT_UNEXPECTED
    capture: Capture | None = None
    # A file that already existed is refused by a guard before any request;
    # one that appears during the run is reported as target_exists.
    existed_at_start = args.capture_file is not None and args.capture_file.exists()
    summary: dict[str, object] = {"outcome": "unexpected_error"}
    try:
        with signals_raise():
            try:
                key = load_sandbox_key(args.env_file)
                out.add_key(key.reveal())
                out.emit(
                    "guard",
                    "ok",
                    is_sandbox_key=True,
                    base_url=SANDBOX_BASE_URL,
                    env_file_mode_warning=_env_mode_warning(args.env_file),
                )
                if args.capture_file is not None and args.capture_file.exists():
                    raise GuardError("capture_file_exists")
                if args.capture_file is not None:
                    check_capture_target(args.capture_file)
                with egress_guard(), build_http_client(transport) as http:
                    assert_client_hardened(http)
                    capture = Capture(
                        key=key,
                        http=http,
                        identity=identity,
                        out=out,
                        today=today,
                        clock=clock,
                        sleep=sleep,
                        poll_policy=poll_policy or PollPolicy(),
                        allow_prior_partial=args.partial_refund_prior,
                        wall=wall,
                    )
                    if args.discover_only:
                        capture.discover()
                    else:
                        capture.run()
            except CaptureError as exc:
                if not exc.reported:
                    emit_error(out, exc, default_step="summary-error")
                code = exc.exit_code
                summary = {"outcome": exc.outcome, "code": exc.code}
            else:
                code = EXIT_OK
                summary = {"outcome": "ok"}
    except (KeyboardInterrupt, Interrupted) as exc:
        code = 128 + exc.signum if isinstance(exc, Interrupted) else EXIT_INTERRUPTED
        summary = {"outcome": "interrupted"}
    except Exception as exc:
        summary = {"outcome": "unexpected_error", "exception": exception_label(exc)}
        raise
    finally:
        # Signals are deferred until the capture file is written and the
        # summary printed; one arriving now changes nothing (the run is over),
        # so the exit code always matches the printed summary.
        with deferred_signals():
            if capture is not None:
                out.emit("requests", "info", sent=capture.requests_sent)
            outcome = str(summary.pop("outcome"))
            final = out.record("summary", outcome, exit_code=code, **summary)
            if args.capture_file is not None and not existed_at_start:
                try:
                    write_capture_file(args.capture_file, out.records, out)
                except OSError as exc:
                    out.print_record(
                        out.record(
                            "capture-file", "write_failed", **capture_failure(exc)
                        )
                    )
            out.print_record(final)
    return code


if __name__ == "__main__":
    sys.exit(main())
