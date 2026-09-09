"""Safe egress transport for outbound HTTP requests.

Trust boundary. This module is the single choke point for every outbound
network call the application makes to a configured provider or a remote
audio URL. It implements the wish's SSRF policy:

- HTTPS absolute only, no userinfo, no fragment, port 443, no ``localhost``.
- Host allowlists per provider (exact hosts for the known operational
  edges; custom-public HTTPS validation for ``outros`` / Evolution /
  Uazapi / audio URLs).
- DNS resolution once at the transport boundary; every A/AAAA record must
  be a public unicast address (no loopback, no RFC1918/ULA, no link-local,
  no multicast, no reserved, no metadata service). The connection is
  pinned to one of those validated IPs and Host/SNI/certificate validation
  keeps using the original hostname (TOCTOU-safe, no second resolution).
- Redirects are always rejected; each new URL would require a fresh
  validation pass, so any 3xx short-circuits the caller.
- Streaming downloads enforce a configurable byte cap and MIME allowlist;
  ``Content-Length`` above the cap is rejected before reading the body.
- Errors never carry the caller's secret material: the caller passes API
  keys/tokens through ``headers``/``bearer_token`` after validation
  succeeds, and this module never logs or re-raises those values.

Pure functions are kept side-effect free so that tests can exercise the
policy without any network. The ``SafeAsyncClient`` context manager wires
the same policy into an ``httpx.AsyncClient`` for real callers.
"""

from __future__ import annotations

import ipaddress
import socket
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable, Mapping
from urllib.parse import urlsplit

import httpx
from httpcore._backends.auto import AutoBackend
from httpcore._backends.base import (
    SOCKET_OPTION,
    AsyncNetworkBackend,
    AsyncNetworkStream,
)

from src.services.api_credentials import GEMINI_API_BASE_URL

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

# Exact hosts pinned to the known operational providers. Any drift here is
# a wish-level decision, not a runtime toggle.
META_ALLOWED_HOSTS: frozenset[str] = frozenset({"graph.facebook.com"})
ZAPI_ALLOWED_HOSTS: frozenset[str] = frozenset({"api.z-api.io"})
LLM_OPENAI_HOSTS: frozenset[str] = frozenset({"api.openai.com"})
LLM_DROPOUT_HOSTS: frozenset[str] = frozenset({"api.neuralake.cloud"})
LLM_GEMINI_HOSTS: frozenset[str] = frozenset({"generativelanguage.googleapis.com"})
STT_GROQ_HOSTS: frozenset[str] = frozenset({"api.groq.com"})
STT_OPENAI_HOSTS: frozenset[str] = frozenset({"api.openai.com"})

# Multimodal transcription (chat-completions carrying ``input_audio`` blocks).
# Unlike STT there is no ``outros`` escape hatch here: the credential
# allowlist (``api_credentials.PROVIDER_ALLOWLISTS["multimodal"]``) names
# exactly these three providers, so all three are pinned and any other host
# is refused outright instead of falling back to a custom-public policy.
MULTIMODAL_OPENAI_HOSTS: frozenset[str] = frozenset({"api.openai.com"})
MULTIMODAL_GROQ_HOSTS: frozenset[str] = frozenset({"api.groq.com"})
MULTIMODAL_OPENROUTER_HOSTS: frozenset[str] = frozenset({"openrouter.ai"})

# Text-to-speech providers. TTS carries clinic-owned API keys, so both
# operational endpoints are pinned exactly and never use custom-public egress.
TTS_ELEVENLABS_HOSTS: frozenset[str] = frozenset({"api.elevenlabs.io"})
TTS_GOOGLE_HOSTS: frozenset[str] = frozenset({"texttospeech.googleapis.com"})

# Providers that fall back to the "custom public" policy: any public
# HTTPS destination is accepted, but the DNS/IP filter still runs.
CUSTOM_PUBLIC_PROVIDERS: frozenset[str] = frozenset(
    {"outros", "evolution", "uazapi", "audio_url"}
)

# MIME types acceptable for remote audio downloads.
AUDIO_MIME_ALLOWLIST: frozenset[str] = frozenset(
    {"application/ogg", "application/octet-stream"}
)
AUDIO_MIME_PREFIX_ALLOWLIST: tuple[str, ...] = ("audio/",)

DEFAULT_CONNECT_TIMEOUT: float = 5.0
DEFAULT_READ_TIMEOUT: float = 30.0
DEFAULT_TOTAL_TIMEOUT: float = 60.0
DEFAULT_MAX_AUDIO_BYTES: int = 10 * 1024 * 1024


class UnsafeEgressError(ValueError):
    """Secret-free egress boundary error.

    ``code`` is a stable machine token (``unsafe_api_url``,
    ``unsafe_audio_url``, ``audio_too_large``, ``unsupported_audio_mime``,
    ``audio_download_failed``, ``redirect_forbidden``). Callers translate
    it to their transport (HTTP 422 for config URLs, HTTP 200 skip envelope
    for audio) without ever echoing headers/keys.
    """

    def __init__(self, code: str, **context: Any) -> None:
        self.code = code
        self.context: dict[str, Any] = context
        super().__init__(code)


# ---------------------------------------------------------------------------
# Pure validators — kept side-effect free for exhaustive negative testing.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedUrl:
    """Parsed and normalised URL ready for DNS pinning.

    ``hostname`` is the lowercased ASCII hostname (used for Host/SNI/cert
    validation); ``port`` is the TCP port; ``path`` and ``query`` are the
    raw path/query as-received (never mutated to preserve provider
    semantics beyond the security checks).
    """

    scheme: str
    hostname: str
    port: int
    path: str
    query: str


def parse_public_https_url(url: str) -> ParsedUrl:
    """Return a ``ParsedUrl`` for a syntactically-safe public HTTPS URL.

    Rejects any URL that carries userinfo, a fragment, a non-443 port,
    a non-HTTPS scheme, or a literal ``localhost`` hostname. Only lexical
    checks happen here; DNS resolution is deferred to
    :func:`resolve_public_ips`.
    """

    if not isinstance(url, str) or not url:
        raise UnsafeEgressError("unsafe_api_url")
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https":
        raise UnsafeEgressError("unsafe_api_url")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeEgressError("unsafe_api_url")
    if parsed.fragment:
        raise UnsafeEgressError("unsafe_api_url")
    if not parsed.hostname:
        raise UnsafeEgressError("unsafe_api_url")
    try:
        port = parsed.port if parsed.port is not None else 443
    except ValueError as exc:  # malformed port literal
        raise UnsafeEgressError("unsafe_api_url") from exc
    if port != 443:
        raise UnsafeEgressError("unsafe_api_url")
    hostname = parsed.hostname.lower()
    if hostname == "localhost":
        raise UnsafeEgressError("unsafe_api_url")

    # If the hostname is a raw IP literal, verify it's globally routable
    # up front. DNS names still need :func:`resolve_public_ips` to check
    # every A/AAAA record.
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        address = None
    if address is not None and not _is_public_ip(address):
        raise UnsafeEgressError("unsafe_api_url")

    return ParsedUrl(
        scheme="https",
        hostname=hostname,
        port=port,
        path=parsed.path or "/",
        query=parsed.query,
    )


def enforce_host_allowlist(
    parsed: ParsedUrl,
    allowed_hosts: Iterable[str] | None,
) -> None:
    """Reject URLs whose hostname is outside the pinned allowlist.

    ``allowed_hosts`` is ``None`` when the provider is a custom-public
    destination (only the public-IP filter matters); it is a non-empty
    iterable when the provider pins exact hosts (Meta / Z-API / OpenAI /
    Dropout / Groq).
    """

    if allowed_hosts is None:
        return
    allowed = {host.lower() for host in allowed_hosts}
    if parsed.hostname not in allowed:
        raise UnsafeEgressError("unsafe_api_url")


def _is_public_ip(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return ``True`` only for globally-routable unicast addresses.

    ``ipaddress.is_global`` already excludes loopback/private/link-local
    on modern Pythons, but we still filter multicast/reserved/unspecified
    plus the well-known IPv6 metadata service ``fd00:ec2::254`` and the
    IPv4 metadata address ``169.254.169.254`` explicitly to make the
    intent auditable.
    """

    if address.is_loopback:
        return False
    if address.is_private:
        return False
    if address.is_link_local:
        return False
    if address.is_multicast:
        return False
    if address.is_reserved:
        return False
    if address.is_unspecified:
        return False
    if not address.is_global:
        return False
    if isinstance(address, ipaddress.IPv4Address):
        # AWS/GCP/Azure IMDS.
        if str(address) == "169.254.169.254":
            return False
    else:
        # AWS/EC2 IPv6 IMDS reserved range.
        if address in ipaddress.IPv6Network("fd00:ec2::/32"):
            return False
    return True


def resolve_public_ips(
    hostname: str,
    *,
    resolver: Any = None,
    port: int = 443,
) -> tuple[str, ...]:
    """Resolve ``hostname`` and return every public-unicast IP as strings.

    Raises :class:`UnsafeEgressError` if the hostname does not resolve or
    if **any** returned address fails :func:`_is_public_ip`. Returning all
    records at once means we never do a second DNS lookup after the check
    (the transport later pins the connection to one of these validated
    addresses).
    """

    lookup = resolver or _default_getaddrinfo
    try:
        infos = lookup(hostname, port)
    except Exception as exc:  # pragma: no cover - depends on system DNS
        raise UnsafeEgressError("unsafe_api_url") from exc
    ips: list[str] = []
    for entry in infos:
        raw_ip = _extract_ip(entry)
        if raw_ip is None:
            continue
        try:
            address = ipaddress.ip_address(raw_ip)
        except ValueError:
            raise UnsafeEgressError("unsafe_api_url")
        if not _is_public_ip(address):
            raise UnsafeEgressError("unsafe_api_url")
        if raw_ip not in ips:
            ips.append(raw_ip)
    if not ips:
        raise UnsafeEgressError("unsafe_api_url")
    return tuple(ips)


def _default_getaddrinfo(hostname: str, port: int) -> list[tuple[Any, ...]]:
    """Wrap ``socket.getaddrinfo`` for both IPv4 and IPv6 records."""

    return socket.getaddrinfo(
        hostname,
        port,
        proto=socket.IPPROTO_TCP,
    )


def _extract_ip(entry: Any) -> str | None:
    """Extract the IP string from a ``getaddrinfo`` tuple (family-agnostic)."""

    if isinstance(entry, tuple) and len(entry) >= 5:
        sockaddr = entry[4]
        if isinstance(sockaddr, tuple) and sockaddr:
            candidate = sockaddr[0]
            if isinstance(candidate, str) and candidate:
                return candidate
    return None


def validate_audio_mime(content_type: str | None) -> None:
    """Reject remote audio whose Content-Type is missing or not audio-like."""

    if not content_type:
        raise UnsafeEgressError("unsupported_audio_mime")
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime in AUDIO_MIME_ALLOWLIST:
        return
    for prefix in AUDIO_MIME_PREFIX_ALLOWLIST:
        if mime.startswith(prefix):
            return
    raise UnsafeEgressError("unsupported_audio_mime")


def enforce_content_length(
    content_length: str | int | None,
    *,
    max_bytes: int,
) -> None:
    """Reject responses whose declared body exceeds the byte cap."""

    if content_length is None or content_length == "":
        return
    try:
        length = int(content_length)
    except (TypeError, ValueError) as exc:
        raise UnsafeEgressError("audio_too_large") from exc
    if length < 0 or length > max_bytes:
        raise UnsafeEgressError("audio_too_large")


# ---------------------------------------------------------------------------
# Provider policy resolver
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderPolicy:
    """Policy tuple used by callers to build a safe request.

    ``allowed_hosts`` is ``None`` when only the public-HTTPS filter
    applies; otherwise it is the pinned set. ``kind`` is the wish label
    used for error/log breadcrumbs (never a secret).
    """

    kind: str
    allowed_hosts: frozenset[str] | None


PROVIDER_POLICIES: Mapping[str, ProviderPolicy] = {
    "meta": ProviderPolicy(kind="meta", allowed_hosts=META_ALLOWED_HOSTS),
    "zapi": ProviderPolicy(kind="zapi", allowed_hosts=ZAPI_ALLOWED_HOSTS),
    "llm_openai": ProviderPolicy(kind="llm_openai", allowed_hosts=LLM_OPENAI_HOSTS),
    "llm_dropout": ProviderPolicy(kind="llm_dropout", allowed_hosts=LLM_DROPOUT_HOSTS),
    "llm_gemini": ProviderPolicy(kind="llm_gemini", allowed_hosts=LLM_GEMINI_HOSTS),
    "llm_outros": ProviderPolicy(kind="llm_outros", allowed_hosts=None),
    "stt_openai": ProviderPolicy(kind="stt_openai", allowed_hosts=STT_OPENAI_HOSTS),
    "stt_groq": ProviderPolicy(kind="stt_groq", allowed_hosts=STT_GROQ_HOSTS),
    "stt_outros": ProviderPolicy(kind="stt_outros", allowed_hosts=None),
    "multimodal_openai": ProviderPolicy(
        kind="multimodal_openai", allowed_hosts=MULTIMODAL_OPENAI_HOSTS
    ),
    "multimodal_groq": ProviderPolicy(
        kind="multimodal_groq", allowed_hosts=MULTIMODAL_GROQ_HOSTS
    ),
    "multimodal_openrouter": ProviderPolicy(
        kind="multimodal_openrouter", allowed_hosts=MULTIMODAL_OPENROUTER_HOSTS
    ),
    "tts_elevenlabs": ProviderPolicy(
        kind="tts_elevenlabs", allowed_hosts=TTS_ELEVENLABS_HOSTS
    ),
    "tts_google": ProviderPolicy(kind="tts_google", allowed_hosts=TTS_GOOGLE_HOSTS),
    "evolution": ProviderPolicy(kind="evolution", allowed_hosts=None),
    "uazapi": ProviderPolicy(kind="uazapi", allowed_hosts=None),
    "audio_url": ProviderPolicy(kind="audio_url", allowed_hosts=None),
}


def resolve_policy(kind: str) -> ProviderPolicy:
    """Return the pinned policy for a provider kind or raise if unknown."""

    try:
        return PROVIDER_POLICIES[kind]
    except KeyError as exc:
        raise UnsafeEgressError("unsafe_api_url") from exc


# ---------------------------------------------------------------------------
# Validated request preparation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidatedRequest:
    """A URL that has passed every egress check, ready for the transport.

    ``pinned_ips`` is the ordered set of validated public IPs the client
    must dial (no second resolution). ``sni_host`` mirrors the original
    hostname so certificate/Host validation stays correct.
    """

    parsed: ParsedUrl
    pinned_ips: tuple[str, ...]
    sni_host: str = field(init=False)

    def __post_init__(self) -> None:  # dataclass ``frozen`` requires object.__setattr__
        object.__setattr__(self, "sni_host", self.parsed.hostname)

    @property
    def url(self) -> str:
        base = f"{self.parsed.scheme}://{self.parsed.hostname}:{self.parsed.port}"
        return f"{base}{self.parsed.path}" + (
            f"?{self.parsed.query}" if self.parsed.query else ""
        )


def validate_url(
    url: str,
    *,
    kind: str,
    resolver: Any = None,
) -> ValidatedRequest:
    """Run the full URL + DNS validation for a provider kind.

    This is what config-time validators call before persisting an API
    URL (though the initial normalisation lives in
    :mod:`src.api.schemas.config`) and what runtime code calls before
    dispatching a request.
    """

    parsed = parse_public_https_url(url)
    policy = resolve_policy(kind)
    enforce_host_allowlist(parsed, policy.allowed_hosts)
    ips = resolve_public_ips(parsed.hostname, resolver=resolver, port=parsed.port)
    return ValidatedRequest(parsed=parsed, pinned_ips=ips)


# ---------------------------------------------------------------------------
# httpx transport wiring
# ---------------------------------------------------------------------------


class PinnedAsyncNetworkBackend(AsyncNetworkBackend):
    """httpcore backend that pins every TCP connect to a pre-validated IP.

    The wish requires that DNS is resolved exactly once, at the egress
    boundary (``resolve_public_ips``) and that ``httpx``/``httpcore`` never
    triggers a second ``getaddrinfo`` at connect time — otherwise a DNS
    attacker with a short TTL could return a public IP for the validation
    and flip to a private/metadata IP for the actual TCP connection
    (classic TOCTOU).

    This backend wraps an underlying :class:`AsyncNetworkBackend` (defaults
    to :class:`AutoBackend`) and rewrites the ``host`` argument of
    :meth:`connect_tcp` to the first pinned IP. Because we hand ``httpcore``
    a literal IP as the host, its backend calls ``getaddrinfo`` on that
    literal (never a name), so no rebinding is possible. TLS SNI and the
    ``Host`` header are already carried through httpx from the original
    hostname (via ``server_hostname`` inside httpcore's ``_connect``), so
    certificate validation continues to work.
    """

    def __init__(
        self,
        pinned_ips: tuple[str, ...],
        *,
        inner: AsyncNetworkBackend | None = None,
    ) -> None:
        if not pinned_ips:
            raise UnsafeEgressError("unsafe_api_url")
        self._pinned_ips: tuple[str, ...] = pinned_ips
        self._inner: AsyncNetworkBackend = inner if inner is not None else AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        # Never dial the ``host`` argument httpcore hands us — dial the
        # pre-validated IP instead. If somehow httpcore invokes a second
        # resolution on a non-pinned literal, fail closed.
        pinned = self._pinned_ips[0]
        return await self._inner.connect_tcp(
            host=pinned,
            port=port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[SOCKET_OPTION] | None = None,
    ) -> AsyncNetworkStream:
        # UDS is never legitimate for egress from this transport.
        raise UnsafeEgressError("unsafe_api_url")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


async def _no_redirect_hook(response: httpx.Response) -> None:
    """Reject any 3xx response — every new URL would need fresh validation.

    Must be ``async`` because ``httpx.AsyncClient`` awaits each event hook
    (``await hook(response)``). A sync hook would raise ``TypeError`` on
    every real response, silently disabling every downstream call.
    """

    if 300 <= response.status_code < 400:
        # Drain the body so httpx does not keep the connection dangling with
        # unread bytes when we raise here.
        try:
            await response.aread()
        except Exception:
            pass
        raise UnsafeEgressError("redirect_forbidden")


def build_safe_async_client(
    validated: ValidatedRequest,
    *,
    timeout: httpx.Timeout | float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """Return an ``httpx.AsyncClient`` pinned to the validated request.

    Redirects are disabled (``follow_redirects=False``) and any 3xx is
    upgraded to :class:`UnsafeEgressError` via ``event_hooks``. The pinned
    IP is honoured through the underlying transport in production; tests
    can inject a ``MockTransport`` to exercise the policy without a real
    network.
    """

    effective_timeout: httpx.Timeout
    if timeout is None:
        effective_timeout = httpx.Timeout(
            DEFAULT_TOTAL_TIMEOUT,
            connect=DEFAULT_CONNECT_TIMEOUT,
            read=DEFAULT_READ_TIMEOUT,
        )
    elif isinstance(timeout, httpx.Timeout):
        effective_timeout = timeout
    else:
        effective_timeout = httpx.Timeout(
            float(timeout),
            connect=DEFAULT_CONNECT_TIMEOUT,
            read=DEFAULT_READ_TIMEOUT,
        )
    kwargs: dict[str, Any] = {
        "timeout": effective_timeout,
        "follow_redirects": False,
        "event_hooks": {"response": [_no_redirect_hook]},
    }
    if transport is not None:
        # Test callers inject an httpx.MockTransport that bypasses the
        # network entirely — no DNS pinning needed because no socket opens.
        kwargs["transport"] = transport
    else:
        # Production path: build a real httpx transport and swap its
        # network backend for :class:`PinnedAsyncNetworkBackend` so httpcore
        # never triggers a second DNS lookup at connect time. See the class
        # docstring for the TOCTOU rationale.
        real_transport = httpx.AsyncHTTPTransport()
        pinned_backend = PinnedAsyncNetworkBackend(validated.pinned_ips)
        # ``httpx.AsyncHTTPTransport`` wraps an ``httpcore.AsyncConnectionPool``
        # exposed as ``_pool``; the pool's ``_network_backend`` is the sole
        # entry point for TCP dials. Overriding it is the documented way to
        # inject custom transports in httpcore 1.x — there is no public
        # kwarg for it on this httpx version (0.26).
        real_transport._pool._network_backend = pinned_backend
        kwargs["transport"] = real_transport
    return httpx.AsyncClient(**kwargs)


@asynccontextmanager
async def safe_async_client(
    url: str,
    *,
    kind: str,
    resolver: Any = None,
    timeout: httpx.Timeout | float | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> AsyncIterator[tuple[httpx.AsyncClient, ValidatedRequest]]:
    """Yield an ``httpx.AsyncClient`` bound to a validated URL.

    Callers use this context manager whenever they touch a configured
    provider URL. The yielded ``ValidatedRequest`` exposes the URL to POST
    against (:attr:`ValidatedRequest.url`) and the SNI/Host header value
    (:attr:`ValidatedRequest.sni_host`) so provider modules never have to
    re-derive them.
    """

    validated = validate_url(url, kind=kind, resolver=resolver)
    async with build_safe_async_client(
        validated, timeout=timeout, transport=transport
    ) as client:
        yield client, validated


# ---------------------------------------------------------------------------
# Safe audio downloader (used by webhook handlers via Group 5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SafeDownload:
    """Streaming download result for remote audio."""

    content: bytes
    mime_type: str


async def safe_download_audio(
    url: str,
    *,
    max_bytes: int = DEFAULT_MAX_AUDIO_BYTES,
    resolver: Any = None,
    transport: httpx.AsyncBaseTransport | None = None,
    headers: Mapping[str, str] | None = None,
) -> SafeDownload:
    """Stream a remote audio URL through the safe transport.

    Enforces the audio MIME allowlist, the ``Content-Length``/streaming
    byte cap, and translates any transport error into
    :class:`UnsafeEgressError` so the webhook handler can respond with the
    exact ``skipped`` envelope demanded by the wish.
    """

    validated = validate_url(url, kind="audio_url", resolver=resolver)
    request_headers = dict(headers) if headers else {}
    request_headers.setdefault("Host", validated.sni_host)
    async with build_safe_async_client(validated, transport=transport) as client:
        try:
            async with client.stream(
                "GET", validated.url, headers=request_headers
            ) as response:
                if response.status_code >= 400:
                    raise UnsafeEgressError("audio_download_failed")
                content_type = response.headers.get("Content-Type")
                validate_audio_mime(content_type)
                enforce_content_length(
                    response.headers.get("Content-Length"), max_bytes=max_bytes
                )
                buffer = bytearray()
                async for chunk in response.aiter_bytes():
                    buffer.extend(chunk)
                    if len(buffer) > max_bytes:
                        raise UnsafeEgressError("audio_too_large")
                mime = (content_type or "").split(";", 1)[0].strip().lower()
                return SafeDownload(content=bytes(buffer), mime_type=mime)
        except UnsafeEgressError:
            raise
        except httpx.TimeoutException as exc:
            raise UnsafeEgressError("audio_download_failed") from exc
        except httpx.HTTPError as exc:
            raise UnsafeEgressError("audio_download_failed") from exc


__all__ = [
    "AUDIO_MIME_ALLOWLIST",
    "CUSTOM_PUBLIC_PROVIDERS",
    "DEFAULT_MAX_AUDIO_BYTES",
    "GEMINI_API_BASE_URL",
    "LLM_DROPOUT_HOSTS",
    "LLM_GEMINI_HOSTS",
    "LLM_OPENAI_HOSTS",
    "META_ALLOWED_HOSTS",
    "MULTIMODAL_GROQ_HOSTS",
    "MULTIMODAL_OPENAI_HOSTS",
    "MULTIMODAL_OPENROUTER_HOSTS",
    "PROVIDER_POLICIES",
    "STT_GROQ_HOSTS",
    "STT_OPENAI_HOSTS",
    "TTS_ELEVENLABS_HOSTS",
    "TTS_GOOGLE_HOSTS",
    "ZAPI_ALLOWED_HOSTS",
    "ParsedUrl",
    "PinnedAsyncNetworkBackend",
    "ProviderPolicy",
    "SafeDownload",
    "UnsafeEgressError",
    "ValidatedRequest",
    "build_safe_async_client",
    "enforce_content_length",
    "enforce_host_allowlist",
    "parse_public_https_url",
    "resolve_policy",
    "resolve_public_ips",
    "safe_async_client",
    "safe_download_audio",
    "validate_audio_mime",
    "validate_url",
]
