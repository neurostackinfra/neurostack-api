"""ASAAS v3 HTTP client for hosted Checkout creation and containment.

Trust boundary. Every request goes to the code-constant production origin
``https://api.asaas.com/v3`` with the ``access_token`` header read from a
``SecretStr``; redirects are never followed, proxies/environment are ignored,
responses are size-bounded and parsed without floats, and every exception is
a fixed code without payloads, URLs, headers or credentials.

Creation is never retried here: a request that may have reached the provider
is reported as ``AsaasOutcomeUnknown`` so the caller can block retries and
reconcile by exact identifiers. Only ``create_checkout`` creates provider
state and it requires a single-use ``CreationEgressPermit`` that is minted
exclusively by ``authorize_creation_egress`` after the financial egress gate.
Containment methods (cancel/delete/refund/readback) deliberately do not
consult the creation gate.

Contract source: https://docs.asaas.com (retrieved 2026-10-11) —
``POST /v3/checkouts`` (required ``billingTypes``, ``chargeTypes``,
``callback.successUrl/cancelUrl``, ``items[].imageBase64/name<=30/quantity/
value``), ``POST /v3/checkouts/{id}/cancel``, ``DELETE /v3/subscriptions/{id}``,
``POST /v3/payments/{id}/refund``, ``GET /v3/subscriptions`` and
``GET /v3/payments`` (``externalReference``/``checkoutSession`` filters). The
payer link is ``https://asaas.com/checkoutSession/show?id=<checkout.id>``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Awaitable, Callable, Final, Literal, Mapping
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx
from pydantic import SecretStr

from src.config.settings import ASAAS_API_BASE_URL, ASAAS_AUTH_HEADER

CheckoutKind = Literal["subscription", "topup"]

ASAAS_CHECKOUT_PAGE_URL: Final = "https://asaas.com/checkoutSession/show"
ASAAS_CHECKOUT_MINUTES_TO_EXPIRE: Final = 60
ASAAS_MAX_RESPONSE_BYTES: Final = 65_536
ASAAS_TIMEOUT: Final = httpx.Timeout(connect=5.0, read=20.0, write=10.0, pool=5.0)
ASAAS_LIST_LIMIT: Final = 100
# Group 3 paginated readbacks: one page of full payment objects (~2 KiB each)
# stays far below ``ASAAS_MAX_RESPONSE_BYTES``.
ASAAS_PAGE_SIZE: Final = 10
ASAAS_MAX_PAGES: Final = 50
MAX_AMOUNT_CENTS: Final = 100_000_000_00

# Backend-owned item image: a 1x1 transparent PNG. The official OpenAPI schema
# marks ``items[].imageBase64`` as required while the guides omit it; sending
# this neutral constant satisfies both without carrying any customer data.
CHECKOUT_ITEM_IMAGE_BASE64: Final = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8"
    "AAAAASUVORK5CYII="
)

_CHARGE_TYPES: Final[Mapping[str, tuple[str, ...]]] = {
    "subscription": ("RECURRENT",),
    "topup": ("DETACHED",),
}
_BILLING_TYPES: Final[Mapping[str, tuple[str, ...]]] = {
    "subscription": ("CREDIT_CARD",),
    "topup": ("CREDIT_CARD", "PIX"),
}
_PROVIDER_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_CHECKOUT_STATUSES: Final = frozenset({"ACTIVE", "CANCELED", "EXPIRED", "PAID"})
_CONCLUSIVE_REJECTION_STATUSES: Final = frozenset(
    {400, 401, 403, 404, 405, 413, 415, 422, 429}
)


class AsaasError(Exception):
    """Redacted ASAAS boundary failure carrying only a fixed code."""

    default_code = "asaas_error"

    def __init__(self, code: str | None = None, *, status_code: int | None = None):
        self.code = code or self.default_code
        self.status_code = status_code
        super().__init__(self.code)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(code={self.code!r})"


class AsaasConfigurationError(AsaasError):
    """The client cannot be built from protected configuration."""

    default_code = "asaas_configuration_invalid"


class AsaasPermitError(AsaasError):
    """A creation request lacked a fresh financial-egress permit."""

    default_code = "asaas_creation_permit_invalid"


class AsaasRequestRejected(AsaasError):
    """Conclusive: the provider did not apply the request.

    Raised when the request provably never left this process (connection or
    pool failures) or the provider answered with a definitive rejection.
    """

    default_code = "asaas_request_rejected"


class AsaasOutcomeUnknown(AsaasError):
    """Ambiguous: the request may have been applied; never retry blindly."""

    default_code = "asaas_outcome_unknown"


def validate_provider_id(value: object) -> str:
    """Return an opaque provider identifier or raise without echoing it."""

    if not isinstance(value, str) or not _PROVIDER_ID.fullmatch(value):
        raise AsaasOutcomeUnknown("asaas_invalid_provider_id")
    return value


def checkout_page_url(checkout_id: str) -> str:
    """Build the payer URL from the fixed HTTPS origin and a validated ID."""

    validated = validate_provider_id(checkout_id)
    return f"{ASAAS_CHECKOUT_PAGE_URL}?{urlencode({'id': validated})}"


def brl_amount(amount_cents: int) -> Decimal:
    """Convert integer cents to an exact two-place BRL ``Decimal``."""

    if type(amount_cents) is not int or not 0 < amount_cents <= MAX_AMOUNT_CENTS:
        raise ValueError("amount_cents must be a positive integer")
    value = Decimal(amount_cents).scaleb(-2)
    if value.as_tuple().exponent != -2:
        raise ValueError("amount must have exactly two decimal places")
    return value


def _encode(value: object) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if type(value) is int:
        return str(value)
    if isinstance(value, float):
        raise TypeError("float is forbidden at the ASAAS money boundary")
    if isinstance(value, Decimal):
        if not value.is_finite() or value.as_tuple().exponent != -2:
            raise TypeError("money must be a finite two-place Decimal")
        return format(value, "f")
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(_encode(item) for item in value) + "]"
    if isinstance(value, Mapping):
        fields: list[str] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            fields.append(f"{json.dumps(key)}:{_encode(item)}")
        return "{" + ",".join(fields) + "}"
    raise TypeError("unsupported JSON value at the ASAAS boundary")


def encode_json(payload: Mapping[str, object]) -> bytes:
    """Encode a request body with exact Decimal money and no floats."""

    return _encode(payload).encode("utf-8")


@dataclass(frozen=True, slots=True, repr=False)
class CheckoutItem:
    """One hosted Checkout item derived from a persisted snapshot line."""

    name: str
    description: str
    quantity: int
    unit_amount_cents: int

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not 1 <= len(self.name) <= 30:
            raise ValueError("checkout item name must have 1..30 characters")
        if not isinstance(self.description, str) or len(self.description) > 150:
            raise ValueError("checkout item description is too long")
        if type(self.quantity) is not int or self.quantity <= 0:
            raise ValueError("checkout item quantity must be a positive integer")
        brl_amount(self.unit_amount_cents)

    def __repr__(self) -> str:
        return "CheckoutItem(<redacted>)"


def _is_https_page_url(value: object) -> bool:
    if not isinstance(value, str) or not value.isascii() or len(value) > 600:
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment
    )


@dataclass(frozen=True, slots=True, repr=False)
class CheckoutCallback:
    """Navigation-only return URLs derived from one configured HTTPS page."""

    success_url: str
    cancel_url: str
    expired_url: str

    def __post_init__(self) -> None:
        for url in (self.success_url, self.cancel_url, self.expired_url):
            if not _is_https_page_url(url):
                raise AsaasConfigurationError("asaas_callback_url_invalid")

    @classmethod
    def from_return_url(cls, return_url: str) -> "CheckoutCallback":
        if not _is_https_page_url(return_url) or "?" in return_url:
            raise AsaasConfigurationError("asaas_callback_url_invalid")
        return cls(
            success_url=f"{return_url}?checkout=success",
            cancel_url=f"{return_url}?checkout=cancel",
            expired_url=f"{return_url}?checkout=expired",
        )

    def __repr__(self) -> str:
        return "CheckoutCallback(<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class CheckoutCreateRequest:
    """Closed hosted-Checkout contract built only from a persisted snapshot."""

    kind: CheckoutKind
    external_reference: UUID
    items: tuple[CheckoutItem, ...]
    callback: CheckoutCallback
    next_due_date: date | None = None

    def __post_init__(self) -> None:
        if self.kind not in _CHARGE_TYPES:
            raise ValueError("unsupported checkout kind")
        if not isinstance(self.external_reference, UUID):
            raise ValueError("external reference must be the internal session UUID")
        if not self.items or not all(
            isinstance(item, CheckoutItem) for item in self.items
        ):
            raise ValueError("checkout requires backend-owned items")
        if self.kind == "subscription" and not isinstance(self.next_due_date, date):
            raise ValueError("subscription checkout requires a backend due date")
        if self.kind == "topup" and self.next_due_date is not None:
            raise ValueError("detached checkout cannot carry a due date")

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "billingTypes": list(_BILLING_TYPES[self.kind]),
            "chargeTypes": list(_CHARGE_TYPES[self.kind]),
            "minutesToExpire": ASAAS_CHECKOUT_MINUTES_TO_EXPIRE,
            "externalReference": str(self.external_reference),
            "callback": {
                "successUrl": self.callback.success_url,
                "cancelUrl": self.callback.cancel_url,
                "expiredUrl": self.callback.expired_url,
            },
            "items": [
                {
                    "imageBase64": CHECKOUT_ITEM_IMAGE_BASE64,
                    "name": item.name,
                    "description": item.description,
                    "quantity": item.quantity,
                    "value": brl_amount(item.unit_amount_cents),
                }
                for item in self.items
            ],
        }
        if self.kind == "subscription":
            assert self.next_due_date is not None
            payload["subscription"] = {
                "cycle": "MONTHLY",
                "nextDueDate": self.next_due_date.isoformat(),
            }
        return payload

    def body(self) -> bytes:
        return encode_json(self.to_payload())

    def __repr__(self) -> str:
        return f"CheckoutCreateRequest(kind={self.kind!r}, items={len(self.items)})"


@dataclass(frozen=True, slots=True)
class CheckoutCreated:
    """Validated provider identity of a created hosted Checkout."""

    checkout_id: str

    @property
    def url(self) -> str:
        return checkout_page_url(self.checkout_id)


@dataclass(frozen=True, slots=True)
class CheckoutStatus:
    """Readback status of one hosted Checkout."""

    checkout_id: str
    status: str


@dataclass(frozen=True, slots=True)
class PaymentRefund:
    """Provider acknowledgement of a refund request."""

    payment_id: str
    status: str


@dataclass(frozen=True, slots=True)
class ProviderRecord:
    """Exact-ID readback row; no amounts, customer data or raw payload."""

    record_id: str
    external_reference: str | None
    status: str | None
    deleted: bool


def provider_cents(value: object) -> int | None:
    """Return exact integer cents for a provider BRL amount, else ``None``.

    Responses are parsed with ``parse_float=Decimal``; a value that is not an
    exact, non-negative two-place amount is never rounded into a number that
    could later be compared with a persisted snapshot.
    """

    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        return None
    amount = Decimal(value)
    if not amount.is_finite() or amount < 0:
        return None
    cents = amount.scaleb(2)
    if cents != cents.to_integral_value():
        return None
    result = int(cents)
    return result if result <= MAX_AMOUNT_CENTS else None


def _optional_provider_id(value: object) -> str | None:
    if isinstance(value, str) and _PROVIDER_ID.fullmatch(value):
        return value
    return None


def _optional_text(value: object, *, limit: int = 100) -> str | None:
    if isinstance(value, str) and 0 < len(value) <= limit and value.isascii():
        return value
    return None


def _optional_date(value: object) -> date | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
        return parsed if parsed.isoformat() == value else None
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class SubscriptionReadback:
    """Allowlisted fields of one exact-ID subscription read; no customer data."""

    subscription_id: str
    status: str | None
    deleted: bool
    cycle: str | None
    value_cents: int | None
    next_due_date: date | None
    external_reference: str | None


@dataclass(frozen=True, slots=True)
class PaymentReadback:
    """Allowlisted fields of one exact-ID payment read; no customer data.

    ``refunded_cents`` is the exact sum of ``DONE`` refunds, or ``None`` when
    the refund list cannot be read exactly (the caller must not guess).
    """

    payment_id: str
    status: str | None
    deleted: bool
    value_cents: int | None
    subscription_id: str | None
    checkout_id: str | None
    external_reference: str | None
    billing_type: str | None
    refunded_cents: int | None
    due_date: date | None = None
    payment_date: date | None = None


@dataclass(frozen=True, slots=True)
class WebhookConfig:
    """Provider-side webhook configuration as read back; never the token."""

    webhook_id: str
    name: str | None
    url: str | None
    enabled: bool | None
    interrupted: bool | None
    api_version: int | None
    has_auth_token: bool | None
    send_type: str | None
    events: tuple[str, ...] | None
    penalized_requests_count: int | None


_WEBHOOK_SEND_TYPES: Final = frozenset({"SEQUENTIALLY", "NON_SEQUENTIALLY"})
_WEBHOOK_EVENT = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")


@dataclass(frozen=True, slots=True, repr=False)
class WebhookSettings:
    """Backend-owned desired webhook configuration for one environment."""

    name: str
    url: str
    email: str
    events: tuple[str, ...]
    send_type: str = "SEQUENTIALLY"
    api_version: int = 3

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not 1 <= len(self.name) <= 100:
            raise AsaasConfigurationError("asaas_webhook_settings_invalid")
        if not _is_https_page_url(self.url) or "?" in self.url:
            raise AsaasConfigurationError("asaas_webhook_settings_invalid")
        if (
            not isinstance(self.email, str)
            or "@" not in self.email
            or len(self.email) > 254
            or any(ord(c) < 33 or ord(c) > 126 for c in self.email)
        ):
            raise AsaasConfigurationError("asaas_webhook_settings_invalid")
        if (
            not self.events
            or len(set(self.events)) != len(self.events)
            or not all(
                isinstance(e, str) and _WEBHOOK_EVENT.fullmatch(e) for e in self.events
            )
        ):
            raise AsaasConfigurationError("asaas_webhook_settings_invalid")
        if self.send_type not in _WEBHOOK_SEND_TYPES or self.api_version != 3:
            raise AsaasConfigurationError("asaas_webhook_settings_invalid")

    def payload(self, *, auth_token: SecretStr, create: bool) -> dict[str, object]:
        token = auth_token.get_secret_value() if auth_token else ""
        # ASAAS contract: 32..255 characters and no whitespace.
        if not 32 <= len(token) <= 255 or any(
            ord(c) < 33 or ord(c) > 126 for c in token
        ):
            raise AsaasConfigurationError("asaas_webhook_token_invalid")
        payload: dict[str, object] = {
            "name": self.name,
            "url": self.url,
            "enabled": True,
            "interrupted": False,
            "authToken": token,
            "sendType": self.send_type,
            "events": list(self.events),
        }
        if create:
            # ``email`` and ``apiVersion`` are creation-only fields.
            payload["email"] = self.email
            payload["apiVersion"] = self.api_version
        return payload

    def __repr__(self) -> str:
        return f"WebhookSettings(events={len(self.events)})"


def _subscription_readback(payload: Mapping[str, Any]) -> SubscriptionReadback:
    return SubscriptionReadback(
        subscription_id=validate_provider_id(payload.get("id")),
        status=_optional_text(payload.get("status")),
        deleted=payload.get("deleted") is True,
        cycle=_optional_text(payload.get("cycle")),
        value_cents=provider_cents(payload.get("value")),
        next_due_date=_optional_date(payload.get("nextDueDate")),
        external_reference=_optional_text(payload.get("externalReference"), limit=200),
    )


def _refunded_cents(refunds: object) -> int | None:
    if refunds is None:
        return 0
    if not isinstance(refunds, list) or len(refunds) > ASAAS_LIST_LIMIT:
        return None
    total = 0
    for refund in refunds:
        if not isinstance(refund, dict):
            return None
        status = refund.get("status")
        if not isinstance(status, str):
            return None
        if status != "DONE":
            continue
        cents = provider_cents(refund.get("value"))
        if cents is None:
            return None
        total += cents
    return total


def _payment_readback(payload: Mapping[str, Any]) -> PaymentReadback:
    return PaymentReadback(
        payment_id=validate_provider_id(payload.get("id")),
        status=_optional_text(payload.get("status")),
        deleted=payload.get("deleted") is True,
        value_cents=provider_cents(payload.get("value")),
        subscription_id=_optional_provider_id(payload.get("subscription")),
        checkout_id=_optional_provider_id(payload.get("checkoutSession")),
        external_reference=_optional_text(payload.get("externalReference"), limit=200),
        billing_type=_optional_text(payload.get("billingType"), limit=30),
        refunded_cents=_refunded_cents(payload.get("refunds")),
        due_date=_optional_date(payload.get("dueDate")),
        payment_date=_optional_date(payload.get("paymentDate")),
    )


def _webhook_config(payload: Mapping[str, Any]) -> WebhookConfig:
    events = payload.get("events")
    parsed_events: tuple[str, ...] | None = None
    if isinstance(events, list) and all(
        isinstance(e, str) and _WEBHOOK_EVENT.fullmatch(e) for e in events
    ):
        parsed_events = tuple(events)
    api_version = payload.get("apiVersion")
    penalized = payload.get("penalizedRequestsCount")
    url = payload.get("url")
    return WebhookConfig(
        webhook_id=validate_provider_id(payload.get("id")),
        name=_optional_text(payload.get("name")),
        url=url if isinstance(url, str) and len(url) <= 600 else None,
        enabled=(
            payload.get("enabled") if isinstance(payload.get("enabled"), bool) else None
        ),
        interrupted=(
            payload.get("interrupted")
            if isinstance(payload.get("interrupted"), bool)
            else None
        ),
        api_version=(
            api_version
            if type(api_version) is int  # noqa: E721 - bool is not a version
            else None
        ),
        has_auth_token=(
            payload.get("hasAuthToken")
            if isinstance(payload.get("hasAuthToken"), bool)
            else None
        ),
        send_type=_optional_text(payload.get("sendType"), limit=30),
        events=parsed_events,
        penalized_requests_count=(
            penalized if type(penalized) is int else None  # noqa: E721
        ),
    )


_PERMIT_SEAL: Final = object()


class CreationEgressPermit:
    """Single-use proof that the financial creation gate was just consulted."""

    __slots__ = ("_used",)

    def __init__(self, seal: object) -> None:
        if seal is not _PERMIT_SEAL:
            raise AsaasPermitError()
        self._used = False

    def consume(self) -> None:
        if self._used:
            raise AsaasPermitError("asaas_creation_permit_consumed")
        self._used = True

    def __repr__(self) -> str:
        return "CreationEgressPermit(<sealed>)"


async def authorize_creation_egress(
    check: Callable[[], Awaitable[None]],
) -> CreationEgressPermit:
    """Run the financial egress gate once and mint one creation permit.

    ``check`` must be ``require_financial_egress``; it raises when the flag is
    false, invalid or unreadable, in which case no permit exists.
    """

    await check()
    return CreationEgressPermit(_PERMIT_SEAL)


@dataclass(frozen=True, slots=True)
class _ProviderResponse:
    status_code: int
    body: bytes


def _parse_object(body: bytes) -> dict[str, Any]:
    try:
        parsed = json.loads(body, parse_float=Decimal, parse_constant=_reject)
    except (ValueError, UnicodeDecodeError) as exc:
        raise AsaasOutcomeUnknown("asaas_invalid_response") from exc
    if not isinstance(parsed, dict):
        raise AsaasOutcomeUnknown("asaas_invalid_response")
    return parsed


def _reject(_constant: str) -> Any:
    raise ValueError("non-finite JSON constant")


class AsaasClient:
    """Async ASAAS client with fixed origin, headers and fail-closed parsing."""

    __slots__ = ("_api_key", "_user_agent", "_transport")

    def __init__(
        self,
        *,
        api_key: SecretStr,
        user_agent: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if (
            not isinstance(api_key, SecretStr)
            or not api_key.get_secret_value().startswith("$aact_prod_")
            or any(ord(c) < 33 or ord(c) > 126 for c in api_key.get_secret_value())
        ):
            raise AsaasConfigurationError()
        if (
            not isinstance(user_agent, str)
            or "/" not in user_agent
            or not user_agent.strip()
            or any(ord(c) < 32 or ord(c) > 126 for c in user_agent)
        ):
            raise AsaasConfigurationError()
        self._api_key = api_key
        self._user_agent = user_agent
        self._transport = transport

    def __repr__(self) -> str:
        return f"AsaasClient(base_url={ASAAS_API_BASE_URL!r})"

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=ASAAS_API_BASE_URL,
            timeout=ASAAS_TIMEOUT,
            follow_redirects=False,
            trust_env=False,
            transport=self._transport,
        )

    async def _send(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        params: Mapping[str, str] | None = None,
    ) -> _ProviderResponse:
        headers = {
            ASAAS_AUTH_HEADER: self._api_key.get_secret_value(),
            "User-Agent": self._user_agent,
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        async with self._http() as http:
            request = http.build_request(
                method,
                path,
                content=body,
                params=dict(params) if params else None,
                headers=headers,
            )
            try:
                response = await http.send(request, stream=True)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                raise AsaasRequestRejected("asaas_request_not_sent") from None
            except httpx.HTTPError:
                raise AsaasOutcomeUnknown("asaas_transport_unknown") from None
            try:
                declared = response.headers.get("Content-Length")
                if declared is not None and (
                    not declared.isdigit() or int(declared) > ASAAS_MAX_RESPONSE_BYTES
                ):
                    raise AsaasOutcomeUnknown("asaas_response_too_large")
                chunks: list[bytes] = []
                size = 0
                # Mock/in-process transports may return an already-consumed
                # response stream. Its buffered content is still subject to
                # the same byte limit; never parse a response unbounded.
                if response.is_stream_consumed:
                    chunks.append(response.content)
                    size = len(response.content)
                    if size > ASAAS_MAX_RESPONSE_BYTES:
                        raise AsaasOutcomeUnknown("asaas_response_too_large")
                else:
                    async for chunk in response.aiter_raw():
                        size += len(chunk)
                        if size > ASAAS_MAX_RESPONSE_BYTES:
                            raise AsaasOutcomeUnknown("asaas_response_too_large")
                        chunks.append(chunk)
            except AsaasError:
                raise
            except httpx.HTTPError:
                raise AsaasOutcomeUnknown("asaas_transport_unknown") from None
            finally:
                await response.aclose()
        if response.headers.get("Content-Encoding", "identity") not in (
            "identity",
            "",
        ):
            raise AsaasOutcomeUnknown("asaas_invalid_response")
        return _ProviderResponse(response.status_code, b"".join(chunks))

    @staticmethod
    def _success_object(response: _ProviderResponse) -> dict[str, Any]:
        status = response.status_code
        if 200 <= status < 300:
            return _parse_object(response.body)
        if status in _CONCLUSIVE_REJECTION_STATUSES:
            raise AsaasRequestRejected("asaas_request_rejected", status_code=status)
        raise AsaasOutcomeUnknown("asaas_outcome_unknown", status_code=status)

    async def create_checkout(
        self,
        request: CheckoutCreateRequest,
        *,
        permit: CreationEgressPermit,
    ) -> CheckoutCreated:
        """Create one hosted Checkout; ambiguous outcomes are never retried."""

        if not isinstance(permit, CreationEgressPermit):
            raise AsaasPermitError()
        if not isinstance(request, CheckoutCreateRequest):
            raise TypeError("checkout request must be a CheckoutCreateRequest")
        body = request.body()
        permit.consume()
        response = await self._send("POST", "/checkouts", body=body)
        payload = self._success_object(response)
        checkout_id = validate_provider_id(payload.get("id"))
        echoed_reference = payload.get("externalReference")
        if echoed_reference is not None and echoed_reference != str(
            request.external_reference
        ):
            raise AsaasOutcomeUnknown("asaas_reference_mismatch")
        status = payload.get("status")
        if status is not None and status not in _CHECKOUT_STATUSES:
            raise AsaasOutcomeUnknown("asaas_invalid_response")
        return CheckoutCreated(checkout_id=checkout_id)

    async def cancel_checkout(self, checkout_id: str) -> CheckoutStatus:
        """Cancel an open Checkout; containment never consults the creation gate."""

        validated = validate_provider_id(checkout_id)
        response = await self._send(
            "POST", f"/checkouts/{validated}/cancel", body=encode_json({})
        )
        payload = self._success_object(response)
        if payload.get("id") != validated:
            raise AsaasOutcomeUnknown("asaas_invalid_response")
        status = payload.get("status")
        if status not in _CHECKOUT_STATUSES:
            raise AsaasOutcomeUnknown("asaas_invalid_response")
        return CheckoutStatus(checkout_id=validated, status=str(status))

    async def delete_subscription(self, subscription_id: str) -> bool:
        """Remove a provider subscription by exact ID for containment."""

        validated = validate_provider_id(subscription_id)
        response = await self._send("DELETE", f"/subscriptions/{validated}")
        payload = self._success_object(response)
        if payload.get("id") != validated or payload.get("deleted") is not True:
            raise AsaasOutcomeUnknown("asaas_invalid_response")
        return True

    async def refund_payment(
        self,
        payment_id: str,
        *,
        amount_cents: int | None = None,
    ) -> PaymentRefund:
        """Request a full (default) or exact partial refund by payment ID."""

        validated = validate_provider_id(payment_id)
        body: dict[str, object] = {}
        if amount_cents is not None:
            body["value"] = brl_amount(amount_cents)
        response = await self._send(
            "POST", f"/payments/{validated}/refund", body=encode_json(body)
        )
        payload = self._success_object(response)
        status = payload.get("status")
        if payload.get("id") != validated or not isinstance(status, str):
            raise AsaasOutcomeUnknown("asaas_invalid_response")
        return PaymentRefund(payment_id=validated, status=status)

    async def _list_exact(
        self,
        resource: Literal["subscriptions", "payments"],
        *,
        filter_name: Literal["externalReference", "checkoutSession"],
        filter_value: str,
    ) -> tuple[ProviderRecord, ...]:
        params = {filter_name: filter_value, "limit": str(ASAAS_LIST_LIMIT)}
        if resource == "subscriptions":
            params["includeDeleted"] = "true"
        payload = self._success_object(
            await self._send("GET", f"/{resource}", params=params)
        )
        data = payload.get("data")
        if (
            not isinstance(data, list)
            or payload.get("hasMore") is not False
            or len(data) > ASAAS_LIST_LIMIT
        ):
            raise AsaasOutcomeUnknown("asaas_invalid_response")
        records: list[ProviderRecord] = []
        for item in data:
            # A row that does not carry the exact filtered identifier means the
            # provider did not apply the filter; such a read proves nothing.
            if not isinstance(item, dict) or item.get(filter_name) != filter_value:
                raise AsaasOutcomeUnknown("asaas_unfiltered_readback")
            reference = item.get("externalReference")
            status = item.get("status")
            records.append(
                ProviderRecord(
                    record_id=validate_provider_id(item.get("id")),
                    external_reference=(
                        reference if isinstance(reference, str) else None
                    ),
                    status=status if isinstance(status, str) else None,
                    deleted=item.get("deleted") is True,
                )
            )
        return tuple(records)

    async def list_subscriptions_by_external_reference(
        self, external_reference: UUID
    ) -> tuple[ProviderRecord, ...]:
        """Read back subscriptions whose externalReference is exactly this UUID."""

        return await self._list_exact(
            "subscriptions",
            filter_name="externalReference",
            filter_value=str(external_reference),
        )

    async def list_payments_by_external_reference(
        self, external_reference: UUID
    ) -> tuple[ProviderRecord, ...]:
        """Read back payments whose externalReference is exactly this UUID."""

        return await self._list_exact(
            "payments",
            filter_name="externalReference",
            filter_value=str(external_reference),
        )

    async def list_payments_by_checkout(
        self, checkout_id: str
    ) -> tuple[ProviderRecord, ...]:
        """Read back payments generated by exactly this hosted Checkout."""

        return await self._list_exact(
            "payments",
            filter_name="checkoutSession",
            filter_value=validate_provider_id(checkout_id),
        )

    # ------------------------------------------------------------------
    # Group 3 readback and webhook management.  None of these create a
    # charge; they deliberately do not consult the creation gate.
    # Contract source: https://docs.asaas.com (retrieved 2026-10-11) —
    # ``GET /v3/subscriptions/{id}``, ``GET /v3/payments/{id}`` (``refunds[]``
    # with status ``DONE``), ``GET /v3/payments?subscription=``,
    # ``GET|POST /v3/webhooks`` and ``GET|PUT /v3/webhooks/{id}`` (response
    # carries ``hasAuthToken``, never the token; ``email``/``apiVersion`` are
    # creation-only).
    # ------------------------------------------------------------------

    async def get_subscription(self, subscription_id: str) -> SubscriptionReadback:
        """Read one subscription by exact provider ID."""

        validated = validate_provider_id(subscription_id)
        payload = self._success_object(
            await self._send("GET", f"/subscriptions/{validated}")
        )
        if payload.get("id") != validated:
            raise AsaasOutcomeUnknown("asaas_invalid_response")
        return _subscription_readback(payload)

    async def get_payment(self, payment_id: str) -> PaymentReadback:
        """Read one payment by exact provider ID."""

        validated = validate_provider_id(payment_id)
        payload = self._success_object(
            await self._send("GET", f"/payments/{validated}")
        )
        if payload.get("id") != validated:
            raise AsaasOutcomeUnknown("asaas_invalid_response")
        return _payment_readback(payload)

    async def _pages(
        self, path: str, params: Mapping[str, str]
    ) -> list[dict[str, Any]]:
        """Read every page of a list, each page small enough for the cap.

        ``ASAAS_PAGE_SIZE`` keeps one page of full payment objects far below
        ``ASAAS_MAX_RESPONSE_BYTES``; more than ``ASAAS_MAX_PAGES`` pages, a
        page without progress or a malformed page proves nothing and raises.
        """

        items: list[dict[str, Any]] = []
        for page in range(ASAAS_MAX_PAGES):
            payload = self._success_object(
                await self._send(
                    "GET",
                    path,
                    params={
                        **params,
                        "offset": str(page * ASAAS_PAGE_SIZE),
                        "limit": str(ASAAS_PAGE_SIZE),
                    },
                )
            )
            data = payload.get("data")
            has_more = payload.get("hasMore")
            if (
                not isinstance(data, list)
                or not isinstance(has_more, bool)
                or len(data) > ASAAS_PAGE_SIZE
                or not all(isinstance(item, dict) for item in data)
            ):
                raise AsaasOutcomeUnknown("asaas_invalid_response")
            items.extend(data)
            if not has_more:
                return items
            if not data:
                raise AsaasOutcomeUnknown("asaas_invalid_response")
        raise AsaasOutcomeUnknown("asaas_readback_too_large")

    async def list_payments_by_subscription(
        self, subscription_id: str
    ) -> tuple[PaymentReadback, ...]:
        """Read every payment of exactly this subscription (paginated)."""

        validated = validate_provider_id(subscription_id)
        records: list[PaymentReadback] = []
        for item in await self._pages("/payments", {"subscription": validated}):
            if item.get("subscription") != validated:
                raise AsaasOutcomeUnknown("asaas_unfiltered_readback")
            records.append(_payment_readback(item))
        return tuple(records)

    async def list_webhooks(self) -> tuple[WebhookConfig, ...]:
        """Read every webhook configuration of the account (paginated)."""

        return tuple(
            _webhook_config(item) for item in await self._pages("/webhooks", {})
        )

    async def get_webhook(self, webhook_id: str) -> WebhookConfig:
        """Read one webhook configuration by exact provider ID."""

        validated = validate_provider_id(webhook_id)
        payload = self._success_object(
            await self._send("GET", f"/webhooks/{validated}")
        )
        if payload.get("id") != validated:
            raise AsaasOutcomeUnknown("asaas_invalid_response")
        return _webhook_config(payload)

    async def create_webhook(
        self, desired: WebhookSettings, *, auth_token: SecretStr
    ) -> WebhookConfig:
        """Create the environment webhook; the token is sent, never returned."""

        body = encode_json(desired.payload(auth_token=auth_token, create=True))
        payload = self._success_object(await self._send("POST", "/webhooks", body=body))
        return _webhook_config(payload)

    async def update_webhook(
        self, webhook_id: str, desired: WebhookSettings, *, auth_token: SecretStr
    ) -> WebhookConfig:
        """Converge an existing webhook to the desired configuration."""

        validated = validate_provider_id(webhook_id)
        body = encode_json(desired.payload(auth_token=auth_token, create=False))
        payload = self._success_object(
            await self._send("PUT", f"/webhooks/{validated}", body=body)
        )
        if payload.get("id") != validated:
            raise AsaasOutcomeUnknown("asaas_invalid_response")
        return _webhook_config(payload)
