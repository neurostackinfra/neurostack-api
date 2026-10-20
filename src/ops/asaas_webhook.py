"""Deploy-time registration of this environment's ASAAS webhook.

Owner decision A2 (2026-10-19): the ASAAS webhook is registered
automatically at deploy.  A post-deploy one-off ECS task, started from the
service's own task definition, runs::

    python -m src.ops.asaas_webhook reconcile

so the API key and the webhook token never leave the task.

Subcommands (default ``readback``):

``check-config``
    Validates the configuration only; no network call.
``readback``
    Read-only verdict: list every webhook, identify ours, read it by exact
    ID and compare it with the desired configuration.
``reconcile``
    At most one provider write (``POST`` when absent, ``PUT`` when drifted,
    nothing when exact), then the ``smoke`` checks.
``smoke``
    ``readback`` plus a second listing that proves the owned webhook is still
    the only one (the post-condition ``reconcile`` ends with).

Configuration comes from the process environment through the application
settings, never from arguments and never from a ``.env`` file:
``ENVIRONMENT`` (the owned name is ``neurostack-billing-<ENVIRONMENT>``;
only ``staging`` and ``production`` are accepted), ``ASAAS_API_KEY``
(production namespace only), ``ASAAS_WEBHOOK_TOKEN`` (32 to 255 printable
ASCII characters), ``ASAAS_WEBHOOK_URL`` (exactly one of
``ALLOWED_WEBHOOK_URLS``), ``ASAAS_WEBHOOK_NOTIFICATION_EMAIL`` and
``ASAAS_USER_AGENT``.  Any invalid value fails closed before any network
call.

Ownership.  A webhook is a candidate when its name is the owned name or its
URL is ``ASAAS_WEBHOOK_URL``.  Exactly one candidate carrying the owned name
is ours.  Two or more candidates, or one that matches by URL only (another
name), is ``ambiguous`` and is never written: an operator decides.  A
listing is trusted only when its pages are complete and contiguous (every
page but the last full and marked ``hasMore``, a reported ``totalCount``
equal to what was read) and no webhook ID appears twice; otherwise the
verdict is ``error`` / ``listing_inconsistent`` before any decision or
write.  A provider that consistently leaves a webhook out of every page and
reports a matching total cannot be detected by any client.

Writes.  Nothing here ever deletes a webhook; ``GuardedTransport`` refuses
any request other than ``GET /v3/webhooks``, ``GET /v3/webhooks/{id}`` and,
for ``reconcile`` only, ``POST /v3/webhooks`` and ``PUT /v3/webhooks/{id}``.
A run makes at most one logical write.  ``POST`` is re-sent only when the
provider conclusively did not apply it (connection not made, HTTP 429);
when its outcome is unknown, or it is rejected (for example a duplicate
created by a concurrent run), the run lists again and adopts what it finds,
never posting twice.  ``PUT`` is idempotent and is retried like a read.
Fields ASAAS accepts only at creation (``apiVersion``; ``email`` is not read
back) are never "fixed" by an update: drift there reports
``requires_recreate``.  A read whose body omits ``apiVersion`` or sends
``null`` is not compared on it (``api_version_unreported``); any other
non-integer value is drift.  An operator pause outranks
``requires_recreate``: a disabled webhook is reported ``disabled``.

Pause and queue.  Every ``PUT`` sends ``enabled: true`` and
``interrupted: false``.  A webhook disabled in ASAAS (an operator pause) is
therefore never written, not even for other drift, unless ``reconcile`` is
given ``--allow-enable`` (verdict ``disabled``, reason
``webhook_disabled_by_operator``).  A queue ASAAS interrupted by itself
after failed deliveries IS resumed by default, which re-sends the backlog
(reason ``queue_resumed``); ``--no-resume`` makes it report-only.

Token.  ASAAS never returns the token, only ``hasAuthToken``.  An exact
webhook is therefore never rewritten unless ``--force-rewrite-token`` is
given (the rotation step after ``ASAAS_WEBHOOK_TOKEN`` changed).  Every
``PUT`` carries the current token and reports ``token_rewritten``.

Failures.  HTTP 429, 408, 5xx, timeouts and connection errors are retried
at most ``RETRIES`` times per call with capped exponential backoff and
jitter, inside one overall deadline; then the verdict is ``error`` with the
reason ``transient``.  401/403 is ``error``/``auth`` at once.  A failed or
ambiguous read never leads to a write.  When the deadline fires while a
write is in flight the result shows ``writes: 1`` with ``transient`` and
``deadline_exceeded``: the write may have been applied, and the next run
adopts what it finds.

Concurrency.  This command takes no lock.  Two simultaneous runs that both
read an empty listing can both create a webhook.  What is guaranteed is
that neither run reports ``ready`` over a duplicate it can see in its final
listing: if ASAAS rejects the second creation, that run adopts the first
run's webhook; if ASAAS accepts both, both runs exit ``ambiguous``
(nothing is deleted; an operator removes one).  A run whose final listing
predates the other run's creation can still report ``ready``; every later
read then reports ``ambiguous``.  Deploy hooks must run one task at a time.

Output.  Exactly one JSON line on stdout and one JSON log line on stderr,
both built only from closed vocabularies, counts, booleans and the SHA-256
of the webhook ID: never the API key, the token, the webhook URL, the
notification e-mail or the webhook ID.  The exit code is 0 only when the
final verdict is ``ready`` (``EXIT_CODES``).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import (
    Awaitable,
    Callable,
    Final,
    Mapping,
    NoReturn,
    Sequence,
    TextIO,
    TypeVar,
)
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr, ValidationError

# Every ``src`` import builds the global settings, which raise on an invalid
# environment (for example a sandbox ASAAS key).  The failure is kept so the
# command still prints its one JSON line instead of a traceback.
try:
    from src.config.settings import ASAAS_API_BASE_URL, Settings
    from src.services.asaas import (
        ASAAS_MAX_RESPONSE_BYTES,
        AsaasClient,
        AsaasConfigurationError,
        AsaasOutcomeUnknown,
        AsaasRequestRejected,
        WebhookConfig,
        WebhookSettings,
    )
    from src.services.billing_webhook import (
        ASAAS_WEBHOOK_PATH,
        compare_webhook,
        desired_webhook_settings,
    )
except Exception as exc:  # reported by ``main`` as one JSON line
    _IMPORT_ERROR: BaseException | None = exc
else:
    _IMPORT_ERROR = None

LOGGER_NAME: Final = "src.ops.asaas_webhook"
SCHEMA: Final = "neurostack.asaas_webhook_registration.v1"
LOG_EVENT: Final = "asaas_webhook_registration"
COMMANDS: Final = ("check-config", "readback", "reconcile", "smoke")
EXIT_CODES: Final[Mapping[str, int]] = MappingProxyType(
    {
        "ready": 0,
        "absent": 10,
        "drift": 11,
        "interrupted": 12,
        "disabled": 13,
        "ambiguous": 20,
        "unconfigured": 30,
        "error": 40,
    }
)
ACTIONS: Final = frozenset({"none", "created", "updated"})
RETRIES: Final = 3
BACKOFF_BASE_SECONDS: Final = 1.0
BACKOFF_CAP_SECONDS: Final = 8.0
DEFAULT_DEADLINE_SECONDS: Final = 120
MIN_DEADLINE_SECONDS: Final = 10
MAX_DEADLINE_SECONDS: Final = 600
DEPLOYABLE_ENVIRONMENTS: Final = frozenset({"staging", "production"})
PRODUCTION_KEY_PREFIX: Final = "$aact_prod_"
# Deliberate safety net, not configuration: the only webhook URL this
# command registers, exactly the owner-decided D3 parameter
# ``ASAAS_WEBHOOK_URL`` (2026-10-18; ``infra/production/
# asaas-production-wiring.params.yaml``).  If that parameter ever changes,
# change this constant in the same reviewed change, or registration stays
# ``unconfigured``.  The host set is derived from it.
ALLOWED_WEBHOOK_URLS: Final = frozenset(
    {"https://staging.api.neurostack.global/v1/billing/webhook/asaas"}
)
ALLOWED_WEBHOOK_HOSTS: Final = frozenset(
    urlsplit(url).netloc for url in ALLOWED_WEBHOOK_URLS
)
# ``WebhookSettings.payload(create=False)`` omits these: an update cannot
# converge them, so drift there is reported, never written.
CREATION_ONLY_DRIFT: Final = frozenset({"api_version"})

CHECK_NAMES: Final = (
    "settings_loaded",
    "imports_ok",
    "api_key_production_namespace",
    "webhook_token_valid",
    "webhook_url_valid",
    "webhook_host_allowed",
    "webhook_url_allowed",
    "notification_email_valid",
    "environment_deployable",
    "user_agent_valid",
)
COUNT_NAMES: Final = (
    "webhooks_listed",
    "candidates",
    "name_matches",
    "url_matches",
    "requests",
    "retries",
    "writes",
)
# ``compare_webhook``'s vocabulary plus everything this command adds.
REASONS: Final = frozenset(
    {
        # configuration (no network call was made)
        "usage",
        "import_failed",
        "settings_invalid",
        "api_key_missing",
        "api_key_invalid",
        "webhook_token_missing",
        "webhook_token_invalid",
        "webhook_url_missing",
        "webhook_url_invalid",
        "webhook_host_not_allowed",
        "webhook_url_not_allowed",
        "notification_email_missing",
        "notification_email_invalid",
        "environment_not_deployable",
        "user_agent_invalid",
        "api_client_invalid",
        # ownership and listing
        "absent",
        "multiple_owned",
        "foreign_name_on_url",
        "duplicate_detected",
        "owned_id_changed",
        "listing_inconsistent",
        # drift (``compare_webhook``) and what it implies
        "name",
        "url",
        "api_version",
        "send_type",
        "disabled",
        "interrupted",
        "events",
        "token_absent",
        "requires_recreate",
        "api_version_unreported",
        # pause and queue
        "webhook_disabled_by_operator",
        "webhook_reenabled",
        "queue_resumed",
        "queue_not_resumed",
        # writes
        "create_rejected",
        "create_unconfirmed",
        "update_failed",
        "token_rewritten",
        # provider and transport failures
        "auth",
        "transient",
        "deadline_exceeded",
        "invalid_response",
        "not_found",
        "provider_rejected",
        "guard_refused",
        "unexpected",
    }
)
_SETTINGS_FIELD_REASON = re.compile(r"^settings_invalid:[a-z][a-z0-9_]{0,63}$")
_SETTINGS_FIELD = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_PROBE_HOST: Final = "https://probe.invalid"
_PROBE_EMAIL: Final = "probe@probe.invalid"

T = TypeVar("T")
Sleep = Callable[[float], Awaitable[None]]


def _zero_counts() -> dict[str, int]:
    return dict.fromkeys(COUNT_NAMES, 0)


def _no_checks() -> dict[str, bool]:
    return dict.fromkeys(CHECK_NAMES, False)


# ---------------------------------------------------------------------------
# Result contract
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReconcileOptions:
    """Explicit operator permissions for ``reconcile`` (all off by default)."""

    force_rewrite_token: bool = False
    allow_enable: bool = False
    no_resume: bool = False


@dataclass(frozen=True, slots=True)
class RunResult:
    """The one document a run prints; validated against closed vocabularies."""

    command: str | None
    verdict: str
    action: str = "none"
    reasons: tuple[str, ...] = ()
    webhook_id_sha256: str | None = None
    counts: Mapping[str, int] = field(default_factory=_zero_counts)
    checks: Mapping[str, bool] = field(default_factory=_no_checks)
    options: ReconcileOptions = field(default_factory=ReconcileOptions)

    def __post_init__(self) -> None:
        if self.verdict not in EXIT_CODES:
            raise ValueError("verdict outside the closed vocabulary")
        if self.action not in ACTIONS:
            raise ValueError("action outside the closed vocabulary")
        if self.command is not None and self.command not in COMMANDS:
            raise ValueError("command outside the closed vocabulary")
        for reason in self.reasons:
            if reason not in REASONS and not _SETTINGS_FIELD_REASON.fullmatch(reason):
                raise ValueError("reason outside the closed vocabulary")
        if self.webhook_id_sha256 is not None and not _SHA256_HEX.fullmatch(
            self.webhook_id_sha256
        ):
            raise ValueError("webhook digest must be lowercase SHA-256 hex")
        if sorted(self.counts) != sorted(COUNT_NAMES) or not all(
            type(value) is int for value in self.counts.values()
        ):
            raise ValueError("counts outside the closed schema")
        if sorted(self.checks) != sorted(CHECK_NAMES) or not all(
            type(value) is bool for value in self.checks.values()
        ):
            raise ValueError("checks outside the closed schema")
        object.__setattr__(self, "reasons", tuple(dict.fromkeys(self.reasons)))

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.verdict]

    def document(self) -> dict[str, object]:
        return {
            "schema": SCHEMA,
            "command": self.command,
            "verdict": self.verdict,
            "action": self.action,
            "reasons": list(self.reasons),
            "webhook_id_sha256": self.webhook_id_sha256,
            "counts": dict(self.counts),
            "checks": dict(self.checks),
            "force_rewrite_token": self.options.force_rewrite_token,
            "allow_enable": self.options.allow_enable,
            "no_resume": self.options.no_resume,
            "exit_code": self.exit_code,
        }


def verdict_for(drift: Sequence[str]) -> str:
    """One verdict class for the drift reasons of our webhook.

    A webhook disabled by an operator outranks a queue ASAAS interrupted,
    which outranks any other drift.
    """

    if not drift:
        return "ready"
    if "disabled" in drift:
        return "disabled"
    if "interrupted" in drift:
        return "interrupted"
    return "drift"


def _digest(webhook_id: str | None) -> str | None:
    if webhook_id is None:
        return None
    return hashlib.sha256(webhook_id.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Configuration: validated in full before any network call
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, repr=False)
class RegistrationConfig:
    """The settings this command uses; never rendered."""

    environment: str
    api_key: SecretStr
    webhook_token: SecretStr
    webhook_url: str
    notification_email: str
    user_agent: str

    @classmethod
    def from_settings(cls, source: Settings) -> RegistrationConfig:
        return cls(
            environment=source.environment,
            api_key=source.asaas_api_key,
            webhook_token=source.asaas_webhook_token,
            webhook_url=source.asaas_webhook_url,
            notification_email=source.asaas_webhook_notification_email,
            user_agent=source.asaas_user_agent,
        )

    def __repr__(self) -> str:
        return "RegistrationConfig(<redacted>)"


def _accepts(build: Callable[[], object]) -> bool:
    """Whether the application's own validator accepts a value."""

    try:
        build()
    except (AsaasConfigurationError, ValueError):
        return False
    return True


def _printable_ascii(value: str) -> bool:
    return all(33 <= ord(character) <= 126 for character in value)


def validate_config(
    config: RegistrationConfig,
) -> tuple[dict[str, bool], tuple[str, ...]]:
    """Return every check (booleans only) and the reasons of the failed ones.

    Each value is checked with the validator that later consumes it: the
    settings URL and user-agent validators, ``WebhookSettings`` for the URL
    and e-mail, ``WebhookSettings.payload`` for the token.  The key check is
    the production-namespace rule the settings and the client enforce.
    """

    key = config.api_key.get_secret_value()
    token = config.webhook_token.get_secret_value()
    probe_url = _PROBE_HOST + ASAAS_WEBHOOK_PATH

    def desired(*, url: str = probe_url, email: str = _PROBE_EMAIL) -> WebhookSettings:
        return desired_webhook_settings(
            environment=config.environment, url=url, email=email
        )

    url_valid = (
        bool(config.webhook_url)
        and _accepts(lambda: Settings.validate_asaas_webhook_url(config.webhook_url))
        and _accepts(lambda: desired(url=config.webhook_url))
    )
    checks = {
        "settings_loaded": True,
        "imports_ok": True,
        "api_key_production_namespace": key.startswith(PRODUCTION_KEY_PREFIX)
        and _printable_ascii(key),
        "webhook_token_valid": _accepts(
            lambda: desired().payload(auth_token=config.webhook_token, create=False)
        ),
        "webhook_url_valid": url_valid,
        "webhook_host_allowed": url_valid
        and urlsplit(config.webhook_url).netloc in ALLOWED_WEBHOOK_HOSTS,
        "webhook_url_allowed": url_valid and config.webhook_url in ALLOWED_WEBHOOK_URLS,
        "notification_email_valid": bool(config.notification_email)
        and _accepts(lambda: desired(email=config.notification_email)),
        "environment_deployable": config.environment in DEPLOYABLE_ENVIRONMENTS,
        "user_agent_valid": _accepts(
            lambda: Settings.validate_asaas_user_agent(config.user_agent)
        ),
    }
    reasons: list[str] = []
    if not checks["api_key_production_namespace"]:
        reasons.append("api_key_invalid" if key else "api_key_missing")
    if not checks["webhook_token_valid"]:
        reasons.append("webhook_token_invalid" if token else "webhook_token_missing")
    if not checks["webhook_url_valid"]:
        reasons.append(
            "webhook_url_invalid" if config.webhook_url else "webhook_url_missing"
        )
    elif not checks["webhook_host_allowed"]:
        reasons.append("webhook_host_not_allowed")
    elif not checks["webhook_url_allowed"]:
        reasons.append("webhook_url_not_allowed")
    if not checks["notification_email_valid"]:
        reasons.append(
            "notification_email_invalid"
            if config.notification_email
            else "notification_email_missing"
        )
    if not checks["environment_deployable"]:
        reasons.append("environment_not_deployable")
    if not checks["user_agent_valid"]:
        reasons.append("user_agent_invalid")
    return checks, tuple(reasons)


# ---------------------------------------------------------------------------
# Failure classes and bounded retries
# ---------------------------------------------------------------------------


class GuardRefused(RuntimeError):
    """A request outside the webhook allowlist; it never left the process."""


class _Stop(Exception):
    """Ends a run with a verdict and closed-vocabulary reasons."""

    def __init__(self, verdict: str, *reasons: str) -> None:
        super().__init__(verdict)
        self.verdict = verdict
        self.reasons = reasons


def is_auth_failure(exc: BaseException) -> bool:
    return isinstance(exc, AsaasRequestRejected) and exc.status_code in (401, 403)


def was_not_applied(exc: BaseException) -> bool:
    """Transient and conclusive: the provider did not apply it (safe to resend)."""

    return isinstance(exc, AsaasRequestRejected) and (
        exc.status_code == 429
        or (exc.status_code is None and exc.code == "asaas_request_not_sent")
    )


def may_have_applied(exc: BaseException) -> bool:
    """Transient with an unknown outcome: timeout, HTTP 408 or 5xx."""

    if not isinstance(exc, AsaasOutcomeUnknown):
        return False
    if exc.status_code is None:
        return exc.code == "asaas_transport_unknown"
    return exc.status_code == 408 or exc.status_code >= 500


def is_transient(exc: BaseException) -> bool:
    return was_not_applied(exc) or may_have_applied(exc)


def failure_reasons(exc: BaseException) -> tuple[str, tuple[str, ...]]:
    """Map a failure to ``(verdict, reasons)``; never echoes the exception."""

    if isinstance(exc, _Stop):
        return exc.verdict, exc.reasons
    if isinstance(exc, GuardRefused):
        return "error", ("guard_refused",)
    if is_auth_failure(exc):
        return "error", ("auth",)
    if is_transient(exc):
        return "error", ("transient",)
    if isinstance(exc, AsaasRequestRejected):
        return "error", (
            ("not_found",) if exc.status_code == 404 else ("provider_rejected",)
        )
    if isinstance(exc, AsaasOutcomeUnknown):
        return "error", ("invalid_response",)
    if isinstance(exc, AsaasConfigurationError):
        return "unconfigured", ("api_client_invalid",)
    return "error", ("unexpected",)


class Retrier:
    """At most ``retries`` re-attempts with capped exponential backoff + jitter."""

    def __init__(
        self, *, sleep: Sleep, rng: random.Random, retries: int = RETRIES
    ) -> None:
        self._sleep = sleep
        self._rng = rng
        self._retries = retries
        self.retries_used = 0

    async def run(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        retry_if: Callable[[BaseException], bool],
    ) -> T:
        attempt = 0
        while True:
            try:
                return await operation()
            except Exception as exc:
                if attempt >= self._retries or not retry_if(exc):
                    raise
            ceiling = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2**attempt)
            attempt += 1
            self.retries_used += 1
            await self._sleep(ceiling * self._rng.uniform(0.5, 1.0))


# ---------------------------------------------------------------------------
# Listing completeness (page metadata the client does not expose)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ListingPage:
    """What one ``GET /v3/webhooks`` page said about itself (``None``: unreadable)."""

    offset: int | None
    limit: int | None
    items: int | None
    has_more: bool | None
    total_count: int | None
    total_count_valid: bool


def _int_param(value: str | None) -> int | None:
    if value is None or not (value.isascii() and value.isdecimal()):
        return None
    return int(value)


def api_version_state(status: int, body: bytes) -> str:
    """How a webhook read carried ``apiVersion``.

    ``AsaasClient`` maps both a missing value and a non-integer to ``None``;
    only ``absent`` and ``null`` mean "not reported".
    """

    if not 200 <= status < 300:
        return "unreadable"
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return "unreadable"
    if not isinstance(payload, dict):
        return "unreadable"
    if "apiVersion" not in payload:
        return "absent"
    value = payload["apiVersion"]
    if value is None:
        return "null"
    return "integer" if type(value) is int else "invalid"


def _listing_page(request: httpx.Request, status: int, body: bytes) -> ListingPage:
    offset = _int_param(request.url.params.get("offset"))
    limit = _int_param(request.url.params.get("limit"))
    unreadable = ListingPage(offset, limit, None, None, None, False)
    if not 200 <= status < 300:
        return unreadable
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return unreadable
    if not isinstance(payload, dict):
        return unreadable
    data = payload.get("data")
    has_more = payload.get("hasMore")
    total = payload.get("totalCount")
    return ListingPage(
        offset=offset,
        limit=limit,
        items=len(data) if isinstance(data, list) else None,
        has_more=has_more if isinstance(has_more, bool) else None,
        total_count=total if type(total) is int else None,
        total_count_valid=total is None or (type(total) is int and total >= 0),
    )


def listing_complete(pages: Sequence[ListingPage], collected: int) -> bool:
    """Whether the pages read prove a complete, contiguous listing.

    ``AsaasClient`` advances by a fixed page size and accepts a short page
    that still says ``hasMore``, so a provider that pages differently could
    make it skip webhooks.  Every page but the last must be full and say
    ``hasMore``, the last must not, each page must start where the items
    read so far end, and a reported ``totalCount`` must equal the items read.
    Repeated items are checked by ID in ``WebhookRegistrar._list``.
    """

    if not pages:
        return False
    seen = 0
    totals: set[int] = set()
    for index, page in enumerate(pages):
        last = index == len(pages) - 1
        if (
            page.items is None
            or page.limit is None
            or page.offset != seen
            or page.has_more is None
            or page.has_more is last
            or not page.total_count_valid
        ):
            return False
        if page.has_more and page.items != page.limit:
            return False
        if page.total_count is not None:
            totals.add(page.total_count)
        seen += page.items
    return seen == collected and totals <= {seen}


# ---------------------------------------------------------------------------
# The only HTTP path: webhook endpoints, no DELETE, writes only on reconcile
# ---------------------------------------------------------------------------


def default_transport() -> httpx.AsyncHTTPTransport:
    """The production transport: TLS verified, process environment ignored.

    ``trust_env=False`` is what ``AsaasClient`` builds for itself: proxies,
    ``SSL_CERT_FILE``/``SSL_CERT_DIR`` and ``SSLKEYLOGFILE`` (which would
    write the TLS session keys of the connection carrying the API key) stay
    out of the request path.  Certificate and host-name verification stay on.
    """

    return httpx.AsyncHTTPTransport(trust_env=False)


class GuardedTransport(httpx.AsyncBaseTransport):
    """Allow only this command's ASAAS requests; count and audit what is sent.

    Allowed: ``GET /v3/webhooks``, ``GET /v3/webhooks/{id}`` and, only when
    ``allow_writes`` (``reconcile``), ``POST /v3/webhooks`` and
    ``PUT /v3/webhooks/{id}`` without a query string, at most
    ``max_write_sends`` times per run.  Everything else (any other verb,
    path, host, scheme or port) raises ``GuardRefused`` before the inner
    transport sees it.  Each listing page and each webhook read is read here
    (bounded like the client's own read, without ever buffering more) and
    recorded for ``listing_complete`` and ``api_version_state``; the client
    receives the same status, headers and bytes.  ``aclose`` is a no-op
    because ``AsaasClient`` closes its transport after every request; the run
    closes the inner transport once with ``close_inner``.
    """

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        *,
        allow_writes: bool,
        max_write_sends: int = 1 + RETRIES,
    ) -> None:
        origin = urlsplit(ASAAS_API_BASE_URL)
        self._scheme = origin.scheme
        self._host = origin.hostname
        self._list_path = origin.path.rstrip("/") + "/webhooks"
        self._item_path = re.compile(
            "^" + re.escape(self._list_path) + r"/[A-Za-z0-9_-]{1,100}$"
        )
        self._inner = inner
        self._allow_writes = allow_writes
        self._max_write_sends = max_write_sends
        self._pages: list[ListingPage] = []
        self._api_versions: dict[str, str] = {}
        self.requests = 0
        self.write_sends = 0

    def start_listing(self) -> None:
        self._pages = []

    def listing_pages(self) -> tuple[ListingPage, ...]:
        return tuple(self._pages)

    def api_version_reported(self, webhook_id: str) -> str | None:
        """``api_version_state`` of the latest read of this webhook."""

        return self._api_versions.get(webhook_id)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.scheme != self._scheme or url.host != self._host or url.port:
            raise GuardRefused("origin")
        listing = url.path == self._list_path
        item = self._item_path.fullmatch(url.path) is not None
        method = request.method
        write = not url.query and (
            (method == "POST" and listing) or (method == "PUT" and item)
        )
        if not write and not (method == "GET" and (listing or item)):
            raise GuardRefused("request")
        if write:
            if not self._allow_writes or self.write_sends >= self._max_write_sends:
                raise GuardRefused("write")
            self.write_sends += 1
        self.requests += 1
        response = await self._inner.handle_async_request(request)
        if method != "GET":
            return response
        if listing:
            # An oversized page proves nothing about the listing.
            content = await self._bounded(
                response, too_large=_Stop("error", "listing_inconsistent")
            )
            self._pages.append(_listing_page(request, response.status_code, content))
        else:
            content = await self._bounded(
                response, too_large=AsaasOutcomeUnknown("asaas_response_too_large")
            )
            self._api_versions[url.path.rsplit("/", 1)[-1]] = api_version_state(
                response.status_code, content
            )
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            content=content,
            extensions=response.extensions,
        )

    @staticmethod
    async def _bounded(response: httpx.Response, *, too_large: Exception) -> bytes:
        """The body, read at most one byte past the client's own cap."""

        body = bytearray()
        try:
            # In-process transports hand back an already-read body (the same
            # case ``AsaasClient`` handles); it is bounded all the same.
            if response.is_stream_consumed:
                body += response.content
            else:
                async for chunk in response.aiter_raw():
                    body += chunk
                    if len(body) > ASAAS_MAX_RESPONSE_BYTES:
                        break
            if len(body) > ASAAS_MAX_RESPONSE_BYTES:
                raise too_large
        finally:
            await response.aclose()
        return bytes(body)

    async def aclose(self) -> None:
        return None

    async def close_inner(self) -> None:
        await self._inner.aclose()


# ---------------------------------------------------------------------------
# The algorithm
# ---------------------------------------------------------------------------


class WebhookRegistrar:
    """Read, decide and (for ``reconcile``) write this environment's webhook."""

    def __init__(
        self,
        client: AsaasClient,
        desired: WebhookSettings,
        *,
        token: SecretStr,
        retrier: Retrier,
        guard: GuardedTransport,
        options: ReconcileOptions,
    ) -> None:
        self._client = client
        self._desired = desired
        self._token = token
        self._retrier = retrier
        self._guard = guard
        self._options = options
        self.action = "none"
        self.notes: list[str] = []
        self.webhook_id: str | None = None
        self.writes = 0
        self.counts = {
            "webhooks_listed": 0,
            "candidates": 0,
            "name_matches": 0,
            "url_matches": 0,
        }

    async def _list(self) -> tuple[WebhookConfig, ...]:
        async def attempt() -> tuple[WebhookConfig, ...]:
            self._guard.start_listing()
            return await self._client.list_webhooks()

        configs = await self._retrier.run(attempt, retry_if=is_transient)
        # A repeated ID means pages overlapped (an ignored ``offset``, an
        # unstable sort), so another webhook may never have been served.
        if len({c.webhook_id for c in configs}) != len(configs):
            raise _Stop("error", "listing_inconsistent")
        if not listing_complete(self._guard.listing_pages(), len(configs)):
            raise _Stop("error", "listing_inconsistent")
        return configs

    def _owned(self, configs: Sequence[WebhookConfig]) -> WebhookConfig | None:
        """Our webhook, ``None`` when absent; ambiguity stops the run."""

        by_name = {c.webhook_id: c for c in configs if c.name == self._desired.name}
        by_url = {c.webhook_id: c for c in configs if c.url == self._desired.url}
        candidates = {**by_name, **by_url}
        self.counts.update(
            webhooks_listed=len(configs),
            candidates=len(candidates),
            name_matches=len(by_name),
            url_matches=len(by_url),
        )
        if len(candidates) > 1:
            raise _Stop("ambiguous", "multiple_owned")
        if not candidates:
            return None
        (config,) = candidates.values()
        if config.webhook_id not in by_name:
            raise _Stop("ambiguous", "foreign_name_on_url")
        return config

    async def _exact(self, webhook_id: str) -> tuple[str, ...]:
        """Drift reasons from the authoritative read by exact ID."""

        exact = await self._retrier.run(
            lambda: self._client.get_webhook(webhook_id), retry_if=is_transient
        )
        if exact.name != self._desired.name:
            raise _Stop("ambiguous", "foreign_name_on_url")
        drift = compare_webhook(exact, self._desired)
        if "api_version" in drift and self._guard.api_version_reported(webhook_id) in (
            "absent",
            "null",
        ):
            # A read that omits the version proves nothing either way; any
            # other value that is not the integer 3 stays drift.
            self.notes.append("api_version_unreported")
            drift = tuple(reason for reason in drift if reason != "api_version")
        return drift

    async def _confirm(self, webhook_id: str) -> tuple[str, ...]:
        """Smoke checks: exact read, then a fresh listing naming only it."""

        drift = await self._exact(webhook_id)
        try:
            again = self._owned(await self._list())
        except _Stop as stop:
            duplicate = (
                ("duplicate_detected",) if "multiple_owned" in stop.reasons else ()
            )
            raise _Stop(stop.verdict, *stop.reasons, *duplicate) from None
        if again is None:
            raise _Stop("error", "listing_inconsistent")
        if again.webhook_id != webhook_id:
            raise _Stop("ambiguous", "owned_id_changed")
        return drift

    async def readback(self) -> tuple[str, ...] | None:
        owned = self._owned(await self._list())
        if owned is None:
            return None
        self.webhook_id = owned.webhook_id
        return await self._exact(owned.webhook_id)

    async def smoke(self) -> tuple[str, ...] | None:
        owned = self._owned(await self._list())
        if owned is None:
            return None
        self.webhook_id = owned.webhook_id
        return await self._confirm(owned.webhook_id)

    async def reconcile(self) -> tuple[str, ...]:
        owned = self._owned(await self._list())
        if owned is None:
            webhook_id = await self._create()
        else:
            webhook_id = owned.webhook_id
            self.webhook_id = webhook_id
            drift = await self._exact(webhook_id)
            recreate = (
                ("requires_recreate",)
                if CREATION_ONLY_DRIFT.intersection(drift)
                else ()
            )
            # Every PUT sends enabled=true and interrupted=false: it would
            # re-enable an operator's pause and resume ASAAS's queue.  The
            # pause is checked first: a disabled webhook reports ``disabled``
            # whatever else drifted, ``requires_recreate`` included.
            refused: list[str] = []
            if "disabled" in drift and not self._options.allow_enable:
                refused.append("webhook_disabled_by_operator")
            if "interrupted" in drift and self._options.no_resume:
                refused.append("queue_not_resumed")
            if refused:
                raise _Stop(verdict_for(drift), *drift, *recreate, *refused)
            if recreate:
                raise _Stop("drift", *drift, *recreate)
            if drift or self._options.force_rewrite_token:
                await self._update(webhook_id)
                self.notes.append("token_rewritten")
                if "disabled" in drift:
                    self.notes.append("webhook_reenabled")
                if "interrupted" in drift:
                    self.notes.append("queue_resumed")
        return await self._confirm(webhook_id)

    async def _create(self) -> str:
        self.writes += 1
        try:
            created = await self._retrier.run(
                lambda: self._client.create_webhook(
                    self._desired, auth_token=self._token
                ),
                retry_if=was_not_applied,
            )
        except (AsaasRequestRejected, AsaasOutcomeUnknown) as exc:
            if is_auth_failure(exc) or was_not_applied(exc):
                raise
            # Rejected (e.g. a concurrent run's duplicate) or unknown outcome:
            # never POST twice; learn what the provider holds instead.
            rejected = isinstance(exc, AsaasRequestRejected)
            self.notes.append("create_rejected" if rejected else "create_unconfirmed")
            owned = self._owned(await self._list())
            if owned is None:
                raise _Stop(
                    "error", *(() if rejected else failure_reasons(exc)[1])
                ) from None
            self.webhook_id = owned.webhook_id
            return owned.webhook_id
        self.action = "created"
        self.webhook_id = created.webhook_id
        return created.webhook_id

    async def _update(self, webhook_id: str) -> None:
        self.writes += 1
        try:
            await self._retrier.run(
                lambda: self._client.update_webhook(
                    webhook_id, self._desired, auth_token=self._token
                ),
                retry_if=is_transient,
            )
        except Exception:
            self.notes.append("update_failed")
            raise
        self.action = "updated"


async def _dispatch(
    registrar: WebhookRegistrar, command: str
) -> tuple[str, ...] | None:
    if command == "reconcile":
        return await registrar.reconcile()
    if command == "smoke":
        return await registrar.smoke()
    return await registrar.readback()


async def run(
    command: str,
    config: RegistrationConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
    deadline_seconds: float = DEFAULT_DEADLINE_SECONDS,
    force_rewrite_token: bool = False,
    allow_enable: bool = False,
    no_resume: bool = False,
) -> RunResult:
    """Run one subcommand; every outcome is a ``RunResult``, never a raise."""

    options = ReconcileOptions(
        force_rewrite_token=force_rewrite_token,
        allow_enable=allow_enable,
        no_resume=no_resume,
    )
    checks, problems = validate_config(config)
    if problems:
        return RunResult(
            command=command,
            verdict="unconfigured",
            reasons=problems,
            checks=checks,
            options=options,
        )
    if command == "check-config":
        return RunResult(
            command=command, verdict="ready", checks=checks, options=options
        )
    desired = desired_webhook_settings(
        environment=config.environment,
        url=config.webhook_url,
        email=config.notification_email,
    )
    guard = GuardedTransport(
        transport if transport is not None else default_transport(),
        allow_writes=command == "reconcile",
    )
    retrier = Retrier(
        sleep=sleep, rng=rng if rng is not None else random.SystemRandom()
    )
    registrar = WebhookRegistrar(
        AsaasClient(
            api_key=config.api_key, user_agent=config.user_agent, transport=guard
        ),
        desired,
        token=config.webhook_token,
        retrier=retrier,
        guard=guard,
        options=options,
    )
    reasons: tuple[str, ...]
    try:
        drift = await asyncio.wait_for(
            _dispatch(registrar, command),
            timeout=deadline_seconds,
        )
    except TimeoutError:
        verdict, reasons = "error", ("transient", "deadline_exceeded")
    except Exception as exc:
        verdict, reasons = failure_reasons(exc)
    else:
        if drift is None:
            verdict, reasons = "absent", ("absent",)
        else:
            verdict, reasons = verdict_for(drift), drift
    finally:
        if transport is None:
            await guard.close_inner()
    return RunResult(
        command=command,
        verdict=verdict,
        action=registrar.action,
        reasons=(*reasons, *registrar.notes),
        webhook_id_sha256=_digest(registrar.webhook_id),
        counts={
            **registrar.counts,
            "requests": guard.requests,
            "retries": retrier.retries_used,
            "writes": registrar.writes,
        },
        checks=checks,
        options=options,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise _UsageError()


def _deadline(value: str) -> int:
    if not (value.isascii() and value.isdecimal()):
        raise argparse.ArgumentTypeError("deadline")
    seconds = int(value)
    if not MIN_DEADLINE_SECONDS <= seconds <= MAX_DEADLINE_SECONDS:
        raise argparse.ArgumentTypeError("deadline")
    return seconds


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    # No ``--help``: the only exit 0 is a ``ready`` verdict.
    parser = _Parser(
        prog="python -m src.ops.asaas_webhook", add_help=False, allow_abbrev=False
    )
    parser.add_argument("command", nargs="?", choices=COMMANDS, default="readback")
    parser.add_argument("--force-rewrite-token", action="store_true")
    parser.add_argument("--allow-enable", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument(
        "--deadline-seconds", type=_deadline, default=DEFAULT_DEADLINE_SECONDS
    )
    args = parser.parse_args(list(argv))
    reconcile_only = (args.force_rewrite_token, args.allow_enable, args.no_resume)
    if any(reconcile_only) and args.command != "reconcile":
        raise _UsageError()
    return args


def _settings_failure(command: str, error: BaseException) -> RunResult:
    """Settings that cannot load are ``unconfigured``; a broken image is an error."""

    if isinstance(error, ValidationError):
        fields = sorted(
            {
                location[0]
                for item in error.errors(
                    include_url=False, include_context=False, include_input=False
                )
                if (location := item.get("loc"))
                and isinstance(location[0], str)
                and _SETTINGS_FIELD.fullmatch(location[0])
            }
        )
        reasons = tuple(f"settings_invalid:{name}" for name in fields)
        return RunResult(
            command=command,
            verdict="unconfigured",
            reasons=reasons or ("settings_invalid",),
        )
    if type(error).__name__ == "ConfigurationError":
        return RunResult(
            command=command, verdict="unconfigured", reasons=("settings_invalid",)
        )
    return RunResult(command=command, verdict="error", reasons=("import_failed",))


def _quiet_http_logs() -> None:
    """httpx logs request URLs (with the webhook ID) at INFO; keep them off."""

    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        if logger.getEffectiveLevel() < logging.WARNING:
            logger.setLevel(logging.WARNING)


def _dumps(document: Mapping[str, object]) -> str:
    return json.dumps(document, sort_keys=True, separators=(",", ":"))


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: Settings | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    sleep: Sleep = asyncio.sleep,
    rng: random.Random | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Run the command; print one JSON line (stdout) and one log line (stderr)."""

    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    started = time.monotonic()
    _quiet_http_logs()
    error_class: str | None = None
    result: RunResult
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
    except _UsageError:
        result = RunResult(command=None, verdict="unconfigured", reasons=("usage",))
    else:
        # Anything stray goes to stderr: stdout carries exactly one line.
        with contextlib.redirect_stdout(err):
            try:
                if _IMPORT_ERROR is not None:
                    result = _settings_failure(args.command, _IMPORT_ERROR)
                else:
                    result = asyncio.run(
                        run(
                            args.command,
                            RegistrationConfig.from_settings(
                                # The process environment only: a ``.env`` in
                                # the working directory must not be able to
                                # redirect the webhook.
                                settings
                                if settings is not None
                                # pydantic-settings' documented init
                                # option; mypy only sees the fields.
                                else Settings(_env_file=None)  # type: ignore[call-arg]
                            ),
                            transport=transport,
                            sleep=sleep,
                            rng=rng,
                            deadline_seconds=args.deadline_seconds,
                            force_rewrite_token=args.force_rewrite_token,
                            allow_enable=args.allow_enable,
                            no_resume=args.no_resume,
                        )
                    )
            except ValidationError as exc:
                result = _settings_failure(args.command, exc)
            except Exception as exc:
                error_class = type(exc).__name__
                result = RunResult(
                    command=args.command,
                    verdict="error",
                    reasons=("unexpected",),
                    options=ReconcileOptions(
                        force_rewrite_token=args.force_rewrite_token,
                        allow_enable=args.allow_enable,
                        no_resume=args.no_resume,
                    ),
                )
    document = result.document()
    out.write(_dumps(document) + "\n")
    out.flush()
    err.write(
        _dumps(
            {
                **document,
                "event": LOG_EVENT,
                "level": "info" if result.exit_code == 0 else "warning",
                "logger": LOGGER_NAME,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "error_class": error_class,
            }
        )
        + "\n"
    )
    err.flush()
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
