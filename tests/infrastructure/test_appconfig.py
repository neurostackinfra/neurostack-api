"""Fail-closed contracts for the localhost AppConfig Agent reader."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from src.config.settings import Settings
from src.infrastructure.appconfig import SUPPORTED_FLAGS, AppConfigClient


def _settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "appconfig_enabled": True,
        "appconfig_origin": "http://127.0.0.1:2772",
        "appconfig_application": "neuro/app",
        "appconfig_environment": "pre prod",
        "appconfig_profile": "egress?#flags",
        "appconfig_token": "local-agent-token-sentinel",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


def _factory(
    handler: Callable[[httpx.Request], httpx.Response],
    creations: list[dict[str, Any]],
) -> Callable[..., httpx.AsyncClient]:
    def build(**kwargs: Any) -> httpx.AsyncClient:
        creations.append(kwargs)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return build


@pytest.mark.asyncio
async def test_successive_calls_observe_agent_changes_without_application_cache() -> (
    None
):
    values = iter([False, True, False])
    requests: list[httpx.Request] = []
    creations: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        enabled = next(values)
        return httpx.Response(
            200,
            json={
                "financial_egress_enabled": {"enabled": enabled},
                "ignored_extra_flag": {"enabled": True, "secret": "never-read"},
            },
        )

    client = AppConfigClient(_settings(), client_factory=_factory(handler, creations))

    assert await client.is_enabled("financial_egress_enabled") is False
    assert await client.is_enabled("financial_egress_enabled") is True
    assert await client.is_enabled("financial_egress_enabled") is False

    assert len(creations) == 3
    assert all(options["trust_env"] is False for options in creations)
    assert all(options["follow_redirects"] is False for options in creations)
    assert all(
        request.headers["authorization"] == "Bearer local-agent-token-sentinel"
        for request in requests
    )
    assert all(
        str(request.url)
        == "http://127.0.0.1:2772/applications/neuro%2Fapp/environments/pre%20prod/configurations/egress%3F%23flags"
        for request in requests
    )


@pytest.mark.asyncio
async def test_invalid_requested_flag_does_not_contaminate_other_flag() -> None:
    creations: list[dict[str, Any]] = []

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "financial_egress_enabled": {"enabled": "true"},
                "uazapi_egress_enabled": {"enabled": True},
                "extra": {"body": "ignored"},
            },
        )

    client = AppConfigClient(_settings(), client_factory=_factory(handler, creations))

    assert await client.is_enabled("financial_egress_enabled") is False
    assert await client.is_enabled("uazapi_egress_enabled") is True


@pytest.mark.parametrize(
    "body",
    [
        b'{"financial_egress_enabled":{"enabled":false},'
        b'"financial_egress_enabled":{"enabled":true}}',
        b'{"financial_egress_enabled":{"enabled":false,"enabled":true}}',
    ],
)
@pytest.mark.asyncio
async def test_duplicate_requested_members_fail_closed(body: bytes) -> None:
    client = AppConfigClient(
        _settings(),
        client_factory=_factory(lambda _: httpx.Response(200, content=body), []),
    )

    assert await client.is_enabled("financial_egress_enabled") is False


@pytest.mark.parametrize(
    "other_flag",
    [
        b'"uazapi_egress_enabled":{"enabled":false},'
        b'"uazapi_egress_enabled":{"enabled":true}',
        b'"uazapi_egress_enabled":{"enabled":false,"enabled":true}',
    ],
)
@pytest.mark.asyncio
async def test_duplicate_unrequested_members_do_not_contaminate_requested_flag(
    other_flag: bytes,
) -> None:
    body = b'{"financial_egress_enabled":{"enabled":true},' + other_flag + b"}"
    client = AppConfigClient(
        _settings(),
        client_factory=_factory(lambda _: httpx.Response(200, content=body), []),
    )

    assert await client.is_enabled("financial_egress_enabled") is True


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="local agent unavailable"),
        httpx.Response(200, content=b"not-json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={}),
        httpx.Response(200, json={"financial_egress_enabled": None}),
        httpx.Response(
            200,
            json={"financial_egress_enabled": {"enabled": 1}},
        ),
        httpx.Response(200, content=b" " * (16 * 1024 + 1)),
    ],
)
@pytest.mark.asyncio
async def test_bad_status_document_or_oversized_body_fails_closed_without_logging(
    response: httpx.Response,
    caplog: pytest.LogCaptureFixture,
) -> None:
    creations: list[dict[str, Any]] = []
    client = AppConfigClient(
        _settings(),
        client_factory=_factory(lambda _: response, creations),
    )

    assert await client.is_enabled("financial_egress_enabled") is False
    assert "local-agent-token-sentinel" not in caplog.text
    assert "local agent unavailable" not in caplog.text


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout])
@pytest.mark.asyncio
async def test_transport_failure_or_timeout_fails_closed(
    failure: type[httpx.HTTPError],
) -> None:
    creations: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        raise failure("agent failure", request=request)

    client = AppConfigClient(_settings(), client_factory=_factory(handler, creations))

    assert await client.is_enabled("financial_egress_enabled") is False


class _CancelledBody(httpx.AsyncByteStream):
    async def __aiter__(self):  # type: ignore[no-untyped-def]
        raise asyncio.CancelledError
        yield b""  # pragma: no cover


@pytest.mark.parametrize("phase", ["request", "read"])
@pytest.mark.asyncio
async def test_request_or_body_cancellation_propagates(phase: str) -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        if phase == "request":
            raise asyncio.CancelledError
        return httpx.Response(200, stream=_CancelledBody())

    client = AppConfigClient(
        _settings(),
        client_factory=_factory(handler, []),
    )

    with pytest.raises(asyncio.CancelledError):
        await client.is_enabled("financial_egress_enabled")


@pytest.mark.asyncio
async def test_redirect_is_not_followed() -> None:
    request_count = 0
    creations: list[dict[str, Any]] = []

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        if request_count == 1:
            return httpx.Response(
                302, headers={"location": "http://127.0.0.1:2772/other"}
            )
        return httpx.Response(
            200,
            json={"financial_egress_enabled": {"enabled": True}},
        )

    client = AppConfigClient(_settings(), client_factory=_factory(handler, creations))

    assert await client.is_enabled("financial_egress_enabled") is False
    assert request_count == 1


@pytest.mark.parametrize(
    "origin",
    [
        "https://127.0.0.1:2772",
        "http://169.254.169.254:2772",
        "http://example.com:2772",
        "http://user:pass@127.0.0.1:2772",
        "http://127.0.0.1:2772/path",
        "http://127.0.0.1:2772?query=1",
        "http://127.0.0.1:2772#fragment",
    ],
)
@pytest.mark.asyncio
async def test_non_loopback_or_ambiguous_origin_is_rejected_before_http(
    origin: str,
) -> None:
    creations: list[dict[str, Any]] = []
    client = AppConfigClient(
        _settings(appconfig_origin=origin),
        client_factory=_factory(
            lambda _: pytest.fail("HTTP must not be reached"),
            creations,
        ),
    )

    assert await client.is_enabled("financial_egress_enabled") is False
    assert creations == []


@pytest.mark.asyncio
async def test_disabled_incomplete_and_unknown_flags_make_zero_http_requests() -> None:
    creations: list[dict[str, Any]] = []
    factory = _factory(lambda _: pytest.fail("HTTP must not be reached"), creations)

    disabled = AppConfigClient(
        _settings(appconfig_enabled=False),
        client_factory=factory,
    )
    incomplete = AppConfigClient(
        _settings(appconfig_application=""),
        client_factory=factory,
    )
    enabled = AppConfigClient(_settings(), client_factory=factory)

    assert await disabled.is_enabled("financial_egress_enabled") is False
    assert await incomplete.is_enabled("financial_egress_enabled") is False
    assert await enabled.is_enabled("unknown_flag") is False
    assert SUPPORTED_FLAGS == {
        "financial_egress_enabled",
        "uazapi_egress_enabled",
    }
    assert creations == []


@pytest.mark.asyncio
async def test_environment_proxies_are_explicitly_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    monkeypatch.delenv("NO_PROXY", raising=False)
    creations: list[dict[str, Any]] = []

    client = AppConfigClient(
        _settings(appconfig_token=""),
        client_factory=_factory(
            lambda _: httpx.Response(
                200,
                json={"uazapi_egress_enabled": {"enabled": True}},
            ),
            creations,
        ),
    )

    assert await client.is_enabled("uazapi_egress_enabled") is True
    assert creations[0]["trust_env"] is False
