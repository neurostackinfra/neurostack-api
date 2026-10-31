"""Fail-closed contracts for the localhost AppConfig Agent reader."""

from __future__ import annotations

import asyncio
import copy
import json
import re
from collections.abc import Callable
from pathlib import Path
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


# ---------------------------------------------------------------------------
# F2: in-process AppConfig Data reader (``APPCONFIG_SOURCE=sdk``).
# ---------------------------------------------------------------------------


class _Body:
    """The ``StreamingBody`` shape returned for ``Configuration``."""

    def __init__(self, content: bytes) -> None:
        self.content = content
        self.read_sizes: list[int | None] = []
        self.closed = False

    def read(self, amt: int | None = None) -> bytes:
        self.read_sizes.append(amt)
        return self.content if amt is None else self.content[:amt]

    def close(self) -> None:
        self.closed = True


def _flags(**values: object) -> bytes:
    import json

    return json.dumps(
        {name: {"enabled": value} for name, value in values.items()}
    ).encode()


class _FakeAppConfigData:
    """A fake ``appconfigdata`` client; no AWS, no network."""

    def __init__(self, polls: list[object]) -> None:
        self.polls = list(polls)
        self.sessions: list[dict[str, Any]] = []
        self.gets: list[dict[str, Any]] = []
        self.bodies: list[_Body] = []
        self._issued = 0

    def start_configuration_session(self, **kwargs: Any) -> dict[str, Any]:
        self.sessions.append(kwargs)
        return {"InitialConfigurationToken": f"initial-{len(self.sessions)}"}

    def get_latest_configuration(self, **kwargs: Any) -> dict[str, Any]:
        self.gets.append(kwargs)
        poll = self.polls.pop(0)
        if isinstance(poll, BaseException):
            raise poll
        if isinstance(poll, dict):
            return poll
        self._issued += 1
        body = _Body(poll)  # type: ignore[arg-type]
        self.bodies.append(body)
        return {
            "NextPollConfigurationToken": f"next-{self._issued}",
            "NextPollIntervalInSeconds": 15,
            "ContentType": "application/json",
            "Configuration": body,
        }


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _data_reader(
    fake: _FakeAppConfigData,
    clock: _Clock | None = None,
    **overrides: Any,
) -> Any:
    from src.infrastructure.appconfig import AppConfigDataFlagReader

    builds: list[int] = []

    def factory() -> _FakeAppConfigData:
        builds.append(1)
        return fake

    reader = AppConfigDataFlagReader(
        _settings(appconfig_source="sdk", **overrides),
        client_factory=factory,
        monotonic=clock or _Clock(),
    )
    reader.test_builds = builds  # type: ignore[attr-defined]
    return reader


@pytest.mark.asyncio
async def test_data_reader_is_closed_before_the_first_successful_read() -> None:
    fake = _FakeAppConfigData([_flags(financial_egress_enabled=True)])
    reader = _data_reader(fake)

    assert await reader.is_enabled("financial_egress_enabled") is False
    assert await reader.is_enabled("uazapi_egress_enabled") is False
    assert fake.sessions == [] and fake.gets == []  # reads never call AWS


@pytest.mark.parametrize("flag", sorted(SUPPORTED_FLAGS))
@pytest.mark.asyncio
async def test_data_reader_open_and_fresh_releases_only_the_open_flag(
    flag: str,
) -> None:
    other = next(name for name in SUPPORTED_FLAGS if name != flag)
    fake = _FakeAppConfigData(
        [_flags(**{flag: True, other: False, "unknown_flag": True})]
    )
    reader = _data_reader(fake)

    assert await reader.refresh_now() is True

    assert await reader.is_enabled(flag) is True
    assert await reader.is_enabled(other) is False
    # Open in the document, but not a supported flag name: rejected.
    assert await reader.is_enabled("unknown_flag") is False
    assert fake.sessions == [
        {
            "ApplicationIdentifier": "neuro/app",
            "EnvironmentIdentifier": "pre prod",
            "ConfigurationProfileIdentifier": "egress?#flags",
            "RequiredMinimumPollIntervalInSeconds": 15,
        }
    ]
    assert fake.gets == [{"ConfigurationToken": "initial-1"}]
    assert fake.bodies[0].closed is True
    assert fake.bodies[0].read_sizes == [16 * 1024 + 1]


@pytest.mark.parametrize(
    "body",
    [
        _flags(financial_egress_enabled=False),  # closed
        _flags(uazapi_egress_enabled=True),  # absent
        b"{}",  # absent
        _flags(financial_egress_enabled="true"),  # not a boolean
        _flags(financial_egress_enabled=1),  # not a boolean
        b'{"financial_egress_enabled":true}',  # not an object
        b'{"financial_egress_enabled":{"enabled":false},'
        b'"financial_egress_enabled":{"enabled":true}}',  # duplicate flag
        b'{"financial_egress_enabled":{"enabled":false,"enabled":true}}',
    ],
)
@pytest.mark.asyncio
async def test_data_reader_closed_absent_or_ambiguous_flag_stays_closed(
    body: bytes,
) -> None:
    reader = _data_reader(_FakeAppConfigData([body]))

    assert await reader.refresh_now() is True

    assert await reader.is_enabled("financial_egress_enabled") is False


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"[]",
        b"null",
        # A valid open document padded past the 16 KiB bound: its bounded
        # prefix still parses, so only the size check can close it.
        _flags(financial_egress_enabled=True, uazapi_egress_enabled=True)
        + b" " * (16 * 1024),
    ],
)
@pytest.mark.asyncio
async def test_data_reader_invalid_document_closes_every_flag(body: bytes) -> None:
    fake = _FakeAppConfigData(
        [_flags(financial_egress_enabled=True, uazapi_egress_enabled=True), body]
    )
    reader = _data_reader(fake)
    assert await reader.refresh_now() is True

    assert await reader.refresh_now() is False

    assert await reader.is_enabled("financial_egress_enabled") is False
    assert await reader.is_enabled("uazapi_egress_enabled") is False


@pytest.mark.asyncio
async def test_data_reader_stale_success_closes_at_three_poll_intervals() -> None:
    clock = _Clock()
    reader = _data_reader(
        _FakeAppConfigData([_flags(financial_egress_enabled=True)]), clock
    )
    assert await reader.refresh_now() is True

    clock.now += 3 * 15 - 0.001
    assert await reader.is_enabled("financial_egress_enabled") is True
    clock.now += 0.001
    assert await reader.is_enabled("financial_egress_enabled") is False
    assert await reader.is_enabled("uazapi_egress_enabled") is False


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("appconfig unavailable"),
        TimeoutError("read timeout"),
        {
            "NextPollConfigurationToken": "",
            "NextPollIntervalInSeconds": 15,
            "Configuration": _Body(b"{}"),
        },
        {
            "NextPollConfigurationToken": "next",
            "NextPollIntervalInSeconds": "15",
            "Configuration": _Body(b"{}"),
        },
        {
            "NextPollConfigurationToken": "next",
            "NextPollIntervalInSeconds": True,
            "Configuration": _Body(b"{}"),
        },
        {"NextPollConfigurationToken": "next", "NextPollIntervalInSeconds": 15},
    ],
)
@pytest.mark.asyncio
async def test_data_reader_error_closes_immediately_and_restarts_the_session(
    failure: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = _FakeAppConfigData(
        [
            _flags(financial_egress_enabled=True, uazapi_egress_enabled=True),
            failure,
            _flags(financial_egress_enabled=True),
        ]
    )
    reader = _data_reader(fake)
    assert await reader.refresh_now() is True
    assert await reader.is_enabled("financial_egress_enabled") is True

    assert await reader.refresh_now() is False

    # Fresh by the clock, but the last read failed: closed for both flags.
    assert await reader.is_enabled("financial_egress_enabled") is False
    assert await reader.is_enabled("uazapi_egress_enabled") is False
    assert "appconfig unavailable" not in caplog.text

    # The consumed token is discarded: the next poll starts a new session.
    assert await reader.refresh_now() is True
    assert len(fake.sessions) == 2
    assert fake.gets[-1] == {"ConfigurationToken": "initial-2"}
    assert await reader.is_enabled("financial_egress_enabled") is True


@pytest.mark.asyncio
async def test_data_reader_session_start_error_fails_closed() -> None:
    class Broken(_FakeAppConfigData):
        def start_configuration_session(self, **kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("AccessDenied")

    reader = _data_reader(Broken([]))

    assert await reader.refresh_now() is False
    assert await reader.is_enabled("financial_egress_enabled") is False


@pytest.mark.asyncio
async def test_data_reader_client_build_error_fails_closed() -> None:
    from src.infrastructure.appconfig import AppConfigDataFlagReader

    def factory() -> Any:
        raise RuntimeError("no credentials")

    reader = AppConfigDataFlagReader(
        _settings(appconfig_source="sdk"), client_factory=factory
    )

    assert await reader.refresh_now() is False
    assert await reader.is_enabled("financial_egress_enabled") is False


@pytest.mark.asyncio
async def test_data_reader_empty_configuration_means_unchanged_and_rotates_token() -> (
    None
):
    clock = _Clock()
    fake = _FakeAppConfigData([_flags(financial_egress_enabled=True), b"", b""])
    reader = _data_reader(fake, clock)
    assert await reader.refresh_now() is True

    for _ in range(2):
        clock.now += 30
        assert await reader.refresh_now() is True
        # Still open: the empty payload confirmed the cached document.
        assert await reader.is_enabled("financial_egress_enabled") is True
        clock.now += 30
        # 60 s after the first read: fresh only because the empty polls counted.
        assert await reader.is_enabled("financial_egress_enabled") is True

    assert [call["ConfigurationToken"] for call in fake.gets] == [
        "initial-1",
        "next-1",
        "next-2",
    ]
    assert len(fake.sessions) == 1


@pytest.mark.asyncio
async def test_data_reader_empty_first_configuration_stays_closed() -> None:
    fake = _FakeAppConfigData([b"", _flags(financial_egress_enabled=True)])
    reader = _data_reader(fake)

    assert await reader.refresh_now() is False
    assert await reader.is_enabled("financial_egress_enabled") is False
    assert await reader.refresh_now() is True
    assert len(fake.sessions) == 2


@pytest.mark.asyncio
async def test_data_reader_new_document_replaces_the_previous_one() -> None:
    reader = _data_reader(
        _FakeAppConfigData(
            [
                _flags(financial_egress_enabled=True),
                _flags(financial_egress_enabled=False),
            ]
        )
    )
    assert await reader.refresh_now() is True
    assert await reader.is_enabled("financial_egress_enabled") is True

    assert await reader.refresh_now() is True  # containment: false deployed
    assert await reader.is_enabled("financial_egress_enabled") is False


@pytest.mark.parametrize(
    ("advertised", "expected"),
    [
        (-1, 15),
        (0, 15),
        (5, 15),
        (14, 15),
        (15, 15),
        (16, 16),
        (59, 59),
        (60, 60),
        (61, 60),
        (86400, 60),
        (10**9, 60),
        (10**400, 60),
    ],
)
@pytest.mark.asyncio
async def test_data_reader_honours_next_poll_interval_with_a_15_second_floor(
    advertised: int,
    expected: int,
) -> None:
    clock = _Clock()
    fake = _FakeAppConfigData(
        [
            {
                "NextPollConfigurationToken": "next-1",
                "NextPollIntervalInSeconds": advertised,
                "Configuration": _Body(_flags(financial_egress_enabled=True)),
            }
        ]
    )
    reader = _data_reader(fake, clock)
    assert await reader.refresh_now() is True

    assert reader.poll_interval == expected
    clock.now += 3 * expected - 0.001
    assert await reader.is_enabled("financial_egress_enabled") is True
    clock.now += 0.001
    assert await reader.is_enabled("financial_egress_enabled") is False


@pytest.mark.parametrize("advertised", [1.5, 15.0, None, "60", True, False, [60]])
@pytest.mark.asyncio
async def test_data_reader_non_integer_poll_interval_fails_closed(
    advertised: object,
) -> None:
    fake = _FakeAppConfigData(
        [
            _flags(financial_egress_enabled=True),
            {
                "NextPollConfigurationToken": "next-2",
                "NextPollIntervalInSeconds": advertised,
                "Configuration": _Body(_flags(financial_egress_enabled=True)),
            },
        ]
    )
    reader = _data_reader(fake)
    assert await reader.refresh_now() is True

    assert await reader.refresh_now() is False
    assert await reader.is_enabled("financial_egress_enabled") is False


@pytest.mark.asyncio
async def test_data_reader_missing_poll_interval_fails_closed() -> None:
    fake = _FakeAppConfigData(
        [
            {
                "NextPollConfigurationToken": "next-1",
                "Configuration": _Body(_flags(financial_egress_enabled=True)),
            }
        ]
    )
    reader = _data_reader(fake)

    assert await reader.refresh_now() is False
    assert await reader.is_enabled("financial_egress_enabled") is False


@pytest.mark.asyncio
async def test_data_reader_cached_true_closes_after_three_capped_intervals() -> None:
    clock = _Clock()
    fake = _FakeAppConfigData(
        [
            {
                "NextPollConfigurationToken": "next-1",
                "NextPollIntervalInSeconds": 86400,
                "Configuration": _Body(_flags(financial_egress_enabled=True)),
            }
        ]
    )
    reader = _data_reader(fake, clock)
    assert await reader.refresh_now() is True

    clock.now += 3 * 60 - 0.001
    assert await reader.is_enabled("financial_egress_enabled") is True
    clock.now += 0.001  # 180 s with no further poll, despite the 86400 hint
    assert await reader.is_enabled("financial_egress_enabled") is False


@pytest.mark.asyncio
async def test_data_refresher_survives_a_huge_poll_interval() -> None:
    fake = _FakeAppConfigData(
        [
            {
                "NextPollConfigurationToken": f"next-{index}",
                "NextPollIntervalInSeconds": 10**400,
                "Configuration": _Body(_flags(financial_egress_enabled=True)),
            }
            for index in (1, 2, 3)
        ]
    )
    reader = _data_reader(fake)
    sleeps: list[float] = []
    states: list[bool] = []

    async def sleep(seconds: float) -> None:
        # The event loop turns the delay into a float deadline; an unbounded
        # hint would raise OverflowError there and kill the refresher.
        sleeps.append(float(seconds))
        states.append(await reader.is_enabled("financial_egress_enabled"))
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await reader.run_refresher(sleep=sleep)

    assert sleeps == [60.0, 60.0, 60.0]
    assert states == [True, True, True]
    assert [call["ConfigurationToken"] for call in fake.gets] == [
        "initial-1",
        "next-1",
        "next-2",
    ]


@pytest.mark.parametrize(
    "session",
    [{}, {"InitialConfigurationToken": ""}, {"InitialConfigurationToken": None}],
)
@pytest.mark.asyncio
async def test_data_reader_session_without_token_fails_closed_with_no_get(
    session: dict[str, Any],
) -> None:
    class NoToken(_FakeAppConfigData):
        def start_configuration_session(self, **kwargs: Any) -> dict[str, Any]:
            self.sessions.append(kwargs)
            return session

    fake = NoToken([_flags(financial_egress_enabled=True)])
    reader = _data_reader(fake)

    assert await reader.refresh_now() is False
    assert await reader.is_enabled("financial_egress_enabled") is False
    assert len(fake.sessions) == 1
    assert fake.gets == []


@pytest.mark.asyncio
async def test_data_reader_concurrent_refreshes_share_one_session() -> None:
    fake = _FakeAppConfigData(
        [_flags(financial_egress_enabled=True), _flags(financial_egress_enabled=True)]
    )
    reader = _data_reader(fake)

    assert await asyncio.gather(reader.refresh_now(), reader.refresh_now()) == [
        True,
        True,
    ]

    assert len(fake.sessions) == 1
    assert [call["ConfigurationToken"] for call in fake.gets] == [
        "initial-1",
        "next-1",
    ]
    assert await reader.is_enabled("financial_egress_enabled") is True


@pytest.mark.asyncio
async def test_data_reader_disabled_or_incomplete_makes_zero_aws_calls() -> None:
    for overrides in (
        {"appconfig_enabled": False},
        {"appconfig_application": ""},
        {"appconfig_environment": " "},
        {"appconfig_profile": ""},
    ):
        fake = _FakeAppConfigData([_flags(financial_egress_enabled=True)])
        reader = _data_reader(fake, **overrides)

        assert await reader.refresh_now() is False
        assert await reader.is_enabled("financial_egress_enabled") is False
        assert reader.test_builds == []
        assert fake.sessions == [] and fake.gets == []


@pytest.mark.asyncio
async def test_data_reader_closes_when_configuration_is_disabled_after_a_read() -> None:
    config = _settings(appconfig_source="sdk")
    from src.infrastructure.appconfig import AppConfigDataFlagReader

    fake = _FakeAppConfigData([_flags(financial_egress_enabled=True)])
    reader = AppConfigDataFlagReader(
        config, client_factory=lambda: fake, monotonic=_Clock()
    )
    assert await reader.refresh_now() is True
    assert await reader.is_enabled("financial_egress_enabled") is True

    config.appconfig_enabled = False

    assert await reader.is_enabled("financial_egress_enabled") is False


@pytest.mark.asyncio
async def test_data_reader_reset_closes_and_forgets_the_session() -> None:
    fake = _FakeAppConfigData(
        [_flags(financial_egress_enabled=True), _flags(financial_egress_enabled=True)]
    )
    reader = _data_reader(fake)
    assert await reader.refresh_now() is True

    reader.reset()

    assert await reader.is_enabled("financial_egress_enabled") is False
    assert await reader.refresh_now() is True
    assert len(fake.sessions) == 2


@pytest.mark.asyncio
async def test_data_reader_refresh_cancellation_propagates_and_discards_token() -> None:
    fake = _FakeAppConfigData(
        [
            _flags(financial_egress_enabled=True),
            asyncio.CancelledError(),
            _flags(financial_egress_enabled=True),
        ]
    )
    reader = _data_reader(fake)
    assert await reader.refresh_now() is True

    with pytest.raises(asyncio.CancelledError):
        await reader.refresh_now()

    assert await reader.is_enabled("financial_egress_enabled") is False
    assert await reader.refresh_now() is True
    assert len(fake.sessions) == 2


@pytest.mark.asyncio
async def test_data_refresher_sleeps_the_poll_interval_and_backs_off_on_failure() -> (
    None
):
    fake = _FakeAppConfigData(
        [
            {
                "NextPollConfigurationToken": "next-1",
                "NextPollIntervalInSeconds": 60,
                "Configuration": _Body(_flags(financial_egress_enabled=True)),
            },
            *(RuntimeError(f"failure-{index}") for index in range(7)),
            _flags(financial_egress_enabled=True),
            RuntimeError("after recovery"),
        ]
    )
    reader = _data_reader(fake)
    sleeps: list[float] = []
    states: list[bool] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        states.append(await reader.is_enabled("financial_egress_enabled"))
        if len(sleeps) == 10:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await reader.run_refresher(sleep=sleep)

    # Seven consecutive failures reach and hold the 300 s cap; a success
    # resets the backoff to 15 s.
    assert sleeps == [60, 15, 30, 60, 120, 240, 300, 300, 15, 15]
    assert states == [True, *[False] * 7, True, False]
    # Stopping the refresher closes the gate for this process.
    assert await reader.is_enabled("financial_egress_enabled") is False


@pytest.mark.asyncio
async def test_data_reader_logs_transitions_once_without_secrets_or_bodies() -> None:
    from structlog.testing import capture_logs

    fake = _FakeAppConfigData(
        [
            {
                "NextPollConfigurationToken": "token-sentinel-1",
                "NextPollIntervalInSeconds": 15,
                "VersionLabel": "v-open",
                "Configuration": _Body(
                    b'{"financial_egress_enabled":{"enabled":true},'
                    b'"body_sentinel":{"enabled":true}}'
                ),
            },
            b"",
            RuntimeError("message-sentinel"),
            RuntimeError("message-sentinel"),
        ]
    )
    reader = _data_reader(fake)

    with capture_logs() as logs:
        assert await reader.refresh_now() is True
        assert await reader.refresh_now() is True  # unchanged: no new line
        assert await reader.refresh_now() is False
        assert await reader.refresh_now() is False  # still failing: no new line

    events = [
        entry for entry in logs if entry["event"] == "appconfig_egress_flags_changed"
    ]
    assert [
        (
            entry["poll_ok"],
            entry["financial_egress_enabled"],
            entry["uazapi_egress_enabled"],
            entry["version_label"],
            entry["error_type"],
        )
        for entry in events
    ] == [
        (True, True, False, "v-open", None),
        (False, False, False, None, "RuntimeError"),
    ]
    rendered = repr(logs)
    for sentinel in ("token-sentinel", "body_sentinel", "message-sentinel", "initial-"):
        assert sentinel not in rendered


def _event_rows(logs: list[dict[str, Any]]) -> list[tuple[object, ...]]:
    return [
        (
            entry["poll_ok"],
            entry["financial_egress_enabled"],
            entry["version_label"],
            entry["error_type"],
            entry["error_code"],
        )
        for entry in logs
        if entry["event"] == "appconfig_egress_flags_changed"
    ]


@pytest.mark.asyncio
async def test_data_reader_logs_incomplete_configuration_once() -> None:
    from structlog.testing import capture_logs

    fake = _FakeAppConfigData([])
    reader = _data_reader(fake, appconfig_environment=" ")

    with capture_logs() as logs:
        for _ in range(3):
            assert await reader.refresh_now() is False

    assert _event_rows(logs) == [
        (False, False, None, "configuration_incomplete", None),
    ]
    assert logs[0]["log_level"] == "warning"
    assert reader.test_builds == [] and fake.sessions == [] and fake.gets == []


@pytest.mark.asyncio
async def test_data_reader_disabled_logs_nothing() -> None:
    from structlog.testing import capture_logs

    reader = _data_reader(_FakeAppConfigData([]), appconfig_enabled=False)

    with capture_logs() as logs:
        assert await reader.refresh_now() is False

    assert _event_rows(logs) == []


def _client_error(code: object, message: str) -> Exception:
    from botocore.exceptions import ClientError

    return ClientError(
        {"Error": {"Code": code, "Message": message}},  # type: ignore[typeddict-item]
        "GetLatestConfiguration",
    )


@pytest.mark.parametrize(
    ("code", "logged"),
    [
        ("AccessDeniedException", "AccessDeniedException"),
        ("ResourceNotFoundException", "ResourceNotFoundException"),
        ("Throttling.Slow_down-2", "Throttling.Slow_down-2"),
        ("X" * 64, "X" * 64),
        ("X" * 65, "unknown"),
        ("", "unknown"),
        ("Access Denied", "unknown"),
        ("AccessDenied\nforged=line", "unknown"),
        ("AccessDenied\n", "unknown"),
        ("token=code-sentinel; drop", "unknown"),
        ("Ac\u00e7\u00e3o", "unknown"),
        (None, "unknown"),
        (403, "unknown"),
    ],
)
@pytest.mark.asyncio
async def test_data_reader_logs_a_sanitized_aws_error_code_never_the_message(
    code: object,
    logged: str,
) -> None:
    from structlog.testing import capture_logs

    fake = _FakeAppConfigData([_client_error(code, "message-sentinel arn:secret")])
    reader = _data_reader(fake)

    with capture_logs() as logs:
        assert await reader.refresh_now() is False

    assert _event_rows(logs) == [(False, False, None, "ClientError", logged)]
    rendered = repr(logs)
    assert "message-sentinel" not in rendered
    assert "arn:secret" not in rendered
    if logged == "unknown" and isinstance(code, str) and code:
        assert code not in rendered


@pytest.mark.asyncio
async def test_data_reader_logs_a_change_of_aws_error_code() -> None:
    from structlog.testing import capture_logs

    fake = _FakeAppConfigData(
        [
            _client_error("AccessDeniedException", "a"),
            _client_error("AccessDeniedException", "b"),
            _client_error("ResourceNotFoundException", "c"),
            RuntimeError("plain"),
        ]
    )
    reader = _data_reader(fake)

    with capture_logs() as logs:
        for _ in range(4):
            assert await reader.refresh_now() is False

    assert [row[3:] for row in _event_rows(logs)] == [
        ("ClientError", "AccessDeniedException"),
        ("ClientError", "ResourceNotFoundException"),
        ("RuntimeError", None),
    ]


_RUNBOOK = Path(__file__).resolve().parents[2] / "docs/runbooks/asaas-deployment.md"


def _runbook_appconfig_examples() -> tuple[str, str]:
    """The hosted and the retrieved JSON examples of the runbook F2 section."""

    text = _RUNBOOK.read_text(encoding="utf-8")
    start = text.index("### AppConfig and opening financial egress (follow-up F2)")
    section = text[start : text.index("\n### ", start + 1)]
    blocks = re.findall(r"^ *```json\n(.*?)^ *```$", section, re.M | re.S)
    assert len(blocks) == 2, blocks
    marker = section.index("<!-- appconfig-retrieved-flags-example -->")
    assert section.index(blocks[1]) > marker > section.index(blocks[0])
    return blocks[0], blocks[1]


@pytest.mark.asyncio
async def test_runbook_retrieved_flags_example_is_what_the_reader_accepts() -> None:
    hosted, retrieved = _runbook_appconfig_examples()
    from src.infrastructure.appconfig import _flag_value, _parse_flag_document

    document = _parse_flag_document(retrieved.encode())
    assert document is not None
    # The runbook example is the closed state; both flags present and false.
    assert [key for key, _ in document] == sorted(SUPPORTED_FLAGS)
    assert all(
        json.loads(retrieved)[name] == {"enabled": False} for name in SUPPORTED_FLAGS
    )
    for name in SUPPORTED_FLAGS:
        assert _flag_value(document, name) is False

    # Opening exactly as the runbook says (only ``enabled`` of the financial
    # flag becomes ``true``) releases that flag and nothing else.
    opened = retrieved.replace(
        '"financial_egress_enabled": {"enabled": false}',
        '"financial_egress_enabled": {"enabled": true}',
    )
    assert opened != retrieved
    reader = _data_reader(_FakeAppConfigData([opened.encode()]))
    assert await reader.refresh_now() is True
    assert await reader.is_enabled("financial_egress_enabled") is True
    assert await reader.is_enabled("uazapi_egress_enabled") is False

    # The hosted FeatureFlags document carries the same values under
    # ``values``; fed to the parser as-is it would read closed, which is why
    # the reader depends on AppConfig returning only the values.
    hosted_document = json.loads(hosted)
    assert hosted_document["version"] == "1"
    assert set(hosted_document["flags"]) == set(SUPPORTED_FLAGS)
    assert hosted_document["values"] == json.loads(retrieved)
    raw = _parse_flag_document(hosted.encode())
    for name in SUPPORTED_FLAGS:
        assert _flag_value(raw, name) is False
    # An OPEN hosted envelope must read closed too: the reader never unwraps
    # ``values`` (the closed example alone could not detect that).
    opened_hosted = copy.deepcopy(hosted_document)
    opened_hosted["values"]["financial_egress_enabled"] = {"enabled": True}
    assert (
        _flag_value(
            _parse_flag_document(json.dumps(opened_hosted).encode()),
            "financial_egress_enabled",
        )
        is False
    )
    reader = _data_reader(_FakeAppConfigData([json.dumps(opened_hosted).encode()]))
    assert await reader.refresh_now() is True  # poll_ok, but closed
    assert await reader.is_enabled("financial_egress_enabled") is False


@pytest.mark.parametrize(
    "payload",
    [
        b"$ion_1_0 {financial_egress_enabled:{enabled:true}}",
        b"['flag'::{name:\"financial_egress_enabled\",enabled:true}]",
        b"\xe0\x01\x00\xea\xee\x8f\x81\x83",
    ],
)
@pytest.mark.asyncio
async def test_data_reader_ion_payload_fails_closed_and_recovers(
    payload: bytes,
) -> None:
    from structlog.testing import capture_logs

    def poll(token: str, body: bytes, content_type: str) -> dict[str, Any]:
        return {
            "NextPollConfigurationToken": token,
            "NextPollIntervalInSeconds": 15,
            "ContentType": content_type,
            "Configuration": _Body(body),
        }

    open_json = _flags(financial_egress_enabled=True)
    fake = _FakeAppConfigData(
        [
            poll("next-1", open_json, "application/json"),
            poll("next-2", payload, "application/x-amazon-ion"),
            poll("next-3", open_json, "application/json"),
        ]
    )
    reader = _data_reader(fake)
    sleeps: list[float] = []
    states: list[bool] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        states.append(await reader.is_enabled("financial_egress_enabled"))
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    with capture_logs() as logs:
        with pytest.raises(asyncio.CancelledError):
            await reader.run_refresher(sleep=sleep)

    assert states == [True, False, True]
    assert sleeps == [15, 15, 15]
    warnings = [row for row in _event_rows(logs) if row[0] is False]
    assert warnings == [(False, False, None, "_PollFailed", None)]
    assert len(fake.sessions) == 2
