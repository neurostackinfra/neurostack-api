"""Fail-closed reads of the egress feature flags from AWS AppConfig.

Two sources, selected by ``APPCONFIG_SOURCE``: ``agent`` reads the localhost
AppConfig Agent cache on every call (``AppConfigClient``); ``sdk`` reads
AppConfig Data from inside the process through a background refresher
(``AppConfigDataFlagReader``, follow-up F2).  Both answer ``False`` unless the
requested flag is unambiguously ``{"enabled": true}``.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from ipaddress import ip_address
from typing import Any, Final, Protocol
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr

from src.config.logging import get_logger

SUPPORTED_FLAGS: Final = frozenset(
    {"financial_egress_enabled", "uazapi_egress_enabled"}
)
_MAX_RESPONSE_BYTES: Final = 16 * 1024
_REQUEST_TIMEOUT: Final = httpx.Timeout(
    timeout=0.5,
    connect=0.2,
    read=0.5,
    write=0.2,
    pool=0.2,
)


class AppConfigSettings(Protocol):
    """Settings required by the local feature-flag reader."""

    appconfig_enabled: bool
    appconfig_origin: str
    appconfig_application: str
    appconfig_environment: str
    appconfig_profile: str
    appconfig_token: SecretStr


AsyncClientFactory = Callable[..., httpx.AsyncClient]


class _JsonObject(list[tuple[str, object]]):
    """Preserve object pairs so requested members can reject duplicates."""


_AMBIGUOUS: Final = object()


def _unique_member(document: _JsonObject, name: str) -> object:
    matches = [value for key, value in document if key == name]
    return matches[0] if len(matches) == 1 else _AMBIGUOUS


def _parse_flag_document(body: bytes) -> _JsonObject | None:
    """Parse one flag document, keeping members so duplicates stay visible."""

    try:
        document = json.loads(body, object_pairs_hook=_JsonObject)
    except ValueError:
        return None
    return document if isinstance(document, _JsonObject) else None


def _flag_value(document: _JsonObject | None, flag_name: str) -> bool:
    """Return one flag's ``enabled`` value, or ``False`` for any ambiguity."""

    if document is None:
        return False
    requested = _unique_member(document, flag_name)
    if not isinstance(requested, _JsonObject):
        return False
    enabled = _unique_member(requested, "enabled")
    return enabled if type(enabled) is bool else False


def _loopback_origin(value: str) -> str | None:
    """Normalize a safe HTTP loopback origin or reject it."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "http"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        return None
    if port is not None and not 1 <= port <= 65535:
        return None

    hostname = parsed.hostname.lower()
    if hostname != "localhost":
        try:
            if not ip_address(hostname).is_loopback:
                return None
        except ValueError:
            return None

    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    rendered_port = f":{port}" if port is not None else ""
    return f"http://{rendered_host}{rendered_port}"


def _request_url(config: AppConfigSettings) -> str | None:
    if not config.appconfig_enabled:
        return None
    origin = _loopback_origin(config.appconfig_origin)
    identifiers = (
        config.appconfig_application,
        config.appconfig_environment,
        config.appconfig_profile,
    )
    if origin is None or any(not value.strip() for value in identifiers):
        return None
    application, environment, profile = (quote(value, safe="") for value in identifiers)
    return (
        f"{origin}/applications/{application}/environments/{environment}"
        f"/configurations/{profile}"
    )


async def _read_limited(response: httpx.Response) -> bytes | None:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > _MAX_RESPONSE_BYTES:
                return None
        except ValueError:
            return None

    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
            return None
        body.extend(chunk)
    return bytes(body)


class AppConfigClient:
    """Read one current flag value per call without application-side caching."""

    def __init__(
        self,
        config: AppConfigSettings,
        *,
        client_factory: AsyncClientFactory | None = None,
    ) -> None:
        self._config = config
        self._client_factory = client_factory

    async def is_enabled(self, flag_name: str) -> bool:
        """Return the current requested flag, or ``False`` for any ambiguity."""

        if flag_name not in SUPPORTED_FLAGS:
            return False
        request_url = _request_url(self._config)
        if request_url is None:
            return False

        headers = {"Accept": "application/json"}
        token = self._config.appconfig_token.get_secret_value()
        if token:
            if any(ord(character) < 32 for character in token):
                return False
            headers["Authorization"] = f"Bearer {token}"

        factory = self._client_factory or httpx.AsyncClient
        try:
            async with factory(
                timeout=_REQUEST_TIMEOUT,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "GET",
                    request_url,
                    headers=headers,
                ) as response:
                    if response.status_code != httpx.codes.OK:
                        return False
                    body = await _read_limited(response)
            if body is None:
                return False
            document = _parse_flag_document(body)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

        return _flag_value(document, flag_name)


# ---------------------------------------------------------------------------
# F2: in-process AppConfig Data reader (``APPCONFIG_SOURCE=sdk``).
# ---------------------------------------------------------------------------

MIN_POLL_INTERVAL_SECONDS: Final = 15
# Upper cap on the server's ``NextPollIntervalInSeconds``: polling sooner than
# the hint is allowed, and the cap bounds staleness to 3 x 60 s whatever the
# service answers (and keeps a huge hint from overflowing the sleep).
_MAX_POLL_INTERVAL_SECONDS: Final = 60
STALE_AFTER_POLL_INTERVALS: Final = 3
_MAX_REFRESH_BACKOFF_SECONDS: Final = 300.0
_APPCONFIG_REGION: Final = "us-east-1"
_MAX_VERSION_LABEL_CHARS: Final = 64
_AWS_ERROR_CODE: Final = re.compile(r"[A-Za-z0-9_.-]{1,64}")
logger = get_logger(__name__)


class AppConfigDataSettings(Protocol):
    """Settings required by the in-process AppConfig Data reader."""

    appconfig_enabled: bool
    appconfig_application: str
    appconfig_environment: str
    appconfig_profile: str


class AppConfigDataClient(Protocol):
    """The two ``appconfigdata`` operations the reader uses (boto3 shape)."""

    def start_configuration_session(self, **kwargs: Any) -> Mapping[str, Any]: ...

    def get_latest_configuration(self, **kwargs: Any) -> Mapping[str, Any]: ...


AppConfigDataClientFactory = Callable[[], AppConfigDataClient]


class _PollFailed(Exception):
    """A poll answer that cannot be trusted; the reader closes."""


def _default_appconfigdata_client() -> AppConfigDataClient:
    import boto3
    from botocore.config import Config

    client: AppConfigDataClient = boto3.client(
        "appconfigdata",
        region_name=_APPCONFIG_REGION,
        config=Config(
            connect_timeout=2,
            read_timeout=5,
            retries={"mode": "standard", "total_max_attempts": 2},
        ),
    )
    return client


def _aws_error_code(exc: BaseException) -> str | None:
    """Return a botocore ``ClientError`` code, sanitized; never its message."""

    try:
        from botocore.exceptions import ClientError
    except ImportError:  # pragma: no cover - botocore is a main dependency
        return None
    if not isinstance(exc, ClientError):
        return None
    response = getattr(exc, "response", None)
    error = response.get("Error") if isinstance(response, Mapping) else None
    code = error.get("Code") if isinstance(error, Mapping) else None
    if isinstance(code, str) and _AWS_ERROR_CODE.fullmatch(code):
        return code
    return "unknown"


def _session_identifiers(config: AppConfigDataSettings) -> tuple[str, str, str] | None:
    if not config.appconfig_enabled:
        return None
    identifiers = (
        config.appconfig_application,
        config.appconfig_environment,
        config.appconfig_profile,
    )
    if any(not value.strip() for value in identifiers):
        return None
    return identifiers


def _read_configuration(
    response: Mapping[str, Any],
) -> tuple[str, int, bytes, str | None]:
    """Validate one ``GetLatestConfiguration`` answer and read its payload."""

    token = response.get("NextPollConfigurationToken")
    interval = response.get("NextPollIntervalInSeconds")
    body = response.get("Configuration")
    if not isinstance(token, str) or not token:
        raise _PollFailed("next poll token")
    if type(interval) is not int:
        raise _PollFailed("next poll interval")
    if body is None or not callable(getattr(body, "read", None)):
        raise _PollFailed("configuration payload")
    try:
        content = body.read(_MAX_RESPONSE_BYTES + 1)
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    if not isinstance(content, bytes) or len(content) > _MAX_RESPONSE_BYTES:
        raise _PollFailed("configuration payload")
    label = response.get("VersionLabel")
    version_label = label[:_MAX_VERSION_LABEL_CHARS] if isinstance(label, str) else None
    interval = min(max(interval, MIN_POLL_INTERVAL_SECONDS), _MAX_POLL_INTERVAL_SECONDS)
    return token, interval, content, version_label


class AppConfigDataFlagReader:
    """Process-local, fail-closed cache of the AppConfig Data flag document.

    Modelled on the webhook readiness gate: request-time reads
    (``is_enabled``) never touch the network; one background refresher per
    process (``run_refresher``, started by the app lifespan) polls
    ``GetLatestConfiguration`` every ``NextPollIntervalInSeconds`` (clamped
    to 15-60 s).  A flag reads ``True`` only when the requested member is
    unambiguously ``{"enabled": true}`` in the last document, the last poll
    succeeded, and that success is younger than three poll intervals.  Before
    the first success, after any failed poll, when stale, and after
    ``reset`` every flag reads ``False``.  An empty ``Configuration`` means
    the document is unchanged.
    """

    def __init__(
        self,
        config: AppConfigDataSettings,
        *,
        client_factory: AppConfigDataClientFactory | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._client_factory = client_factory or _default_appconfigdata_client
        self._monotonic = monotonic
        self._client: AppConfigDataClient | None = None
        self._token: str | None = None
        self._document: _JsonObject | None = None
        self._succeeded_at: float | None = None
        self._poll_interval = MIN_POLL_INTERVAL_SECONDS
        self._version_label: str | None = None
        self._logged: tuple[object, ...] | None = None
        self._lock = asyncio.Lock()

    @property
    def poll_interval(self) -> int:
        return self._poll_interval

    def reset(self) -> None:
        """Close every flag and forget the session (next poll starts anew)."""

        self._token = None
        self._document = None
        self._succeeded_at = None
        self._version_label = None

    def _log_transition(
        self,
        *,
        poll_ok: bool,
        error_type: str | None,
        error_code: str | None = None,
    ) -> None:
        """Log only when the observable state changes; never secrets or bodies."""

        state = (
            poll_ok,
            _flag_value(self._document, "financial_egress_enabled"),
            _flag_value(self._document, "uazapi_egress_enabled"),
            self._version_label,
            error_type,
            error_code,
        )
        if state == self._logged:
            return
        self._logged = state
        try:
            (logger.info if poll_ok else logger.warning)(
                "appconfig_egress_flags_changed",
                poll_ok=poll_ok,
                financial_egress_enabled=state[1],
                uazapi_egress_enabled=state[2],
                version_label=self._version_label,
                error_type=error_type,
                error_code=error_code,
            )
        except Exception:  # pragma: no cover - telemetry is best effort
            pass

    def _is_fresh(self) -> bool:
        if self._document is None or self._succeeded_at is None:
            return False
        age = self._monotonic() - self._succeeded_at
        return age < STALE_AFTER_POLL_INTERVALS * self._poll_interval

    async def is_enabled(self, flag_name: str) -> bool:
        """Return the cached flag, or ``False`` for any doubt (no network)."""

        if flag_name not in SUPPORTED_FLAGS:
            return False
        if _session_identifiers(self._config) is None:
            return False
        if not self._is_fresh():
            return False
        return _flag_value(self._document, flag_name)

    def _poll(
        self, identifiers: tuple[str, str, str], token: str | None
    ) -> tuple[str, int, bytes, str | None]:
        """Blocking AWS round trip; runs in a worker thread."""

        if self._client is None:
            self._client = self._client_factory()
        if token is None:
            application, environment, profile = identifiers
            session = self._client.start_configuration_session(
                ApplicationIdentifier=application,
                EnvironmentIdentifier=environment,
                ConfigurationProfileIdentifier=profile,
                RequiredMinimumPollIntervalInSeconds=MIN_POLL_INTERVAL_SECONDS,
            )
            token = session.get("InitialConfigurationToken")
            if not isinstance(token, str) or not token:
                raise _PollFailed("initial configuration token")
        return _read_configuration(
            self._client.get_latest_configuration(ConfigurationToken=token)
        )

    async def refresh_now(self) -> bool:
        """Poll AppConfig once; return whether the poll succeeded.

        Any failure closes every flag and discards the session token, so the
        next poll starts a new session.  Cancellation propagates (and also
        discards the token, which the cancelled call may have consumed).
        """

        async with self._lock:
            identifiers = _session_identifiers(self._config)
            if identifiers is None:
                self.reset()
                if self._config.appconfig_enabled:
                    # Enabled with a blank ID: closed forever, so say so once.
                    self._log_transition(
                        poll_ok=False, error_type="configuration_incomplete"
                    )
                return False
            try:
                token, interval, content, version_label = await asyncio.to_thread(
                    self._poll, identifiers, self._token
                )
                if content:
                    document = _parse_flag_document(content)
                    if document is None:
                        raise _PollFailed("configuration document")
                elif self._document is not None:
                    # Empty payload: unchanged document and version.
                    document = self._document
                    version_label = self._version_label
                else:
                    raise _PollFailed("no configuration to keep")
            except asyncio.CancelledError:
                self.reset()
                raise
            except Exception as exc:
                self.reset()
                self._log_transition(
                    poll_ok=False,
                    error_type=type(exc).__name__,
                    error_code=_aws_error_code(exc),
                )
                return False
            self._token = token
            self._poll_interval = interval
            self._document = document
            self._version_label = version_label
            self._succeeded_at = self._monotonic()
            self._log_transition(poll_ok=True, error_type=None)
            return True

    async def run_refresher(
        self,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """Keep this process's flags warm (app lifespan task, until cancelled).

        Success sleeps ``NextPollIntervalInSeconds``; failure keeps the flags
        closed and retries with exponential backoff from 15 s up to 300 s.
        Every worker process runs its own refresher.  Stopping it closes the
        flags of this process.
        """

        backoff = float(MIN_POLL_INTERVAL_SECONDS)
        try:
            while True:
                if await self.refresh_now():
                    backoff = float(MIN_POLL_INTERVAL_SECONDS)
                    await sleep(self._poll_interval)
                else:
                    await sleep(backoff)
                    backoff = min(backoff * 2, _MAX_REFRESH_BACKOFF_SECONDS)
        finally:
            self.reset()
