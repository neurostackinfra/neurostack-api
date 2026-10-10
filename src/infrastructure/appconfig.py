"""Fail-closed reads from the localhost AWS AppConfig Agent cache."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from ipaddress import ip_address
from typing import Final, Protocol
from urllib.parse import quote, urlsplit

import httpx
from pydantic import SecretStr

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
            document = json.loads(body, object_pairs_hook=_JsonObject)
        except asyncio.CancelledError:
            raise
        except Exception:
            return False

        if not isinstance(document, _JsonObject):
            return False
        requested = _unique_member(document, flag_name)
        if not isinstance(requested, _JsonObject):
            return False
        enabled = _unique_member(requested, "enabled")
        return enabled if type(enabled) is bool else False
