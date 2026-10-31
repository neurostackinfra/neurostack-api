"""Cancellation and fail-closed contracts for provider egress policy."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.config.settings import settings
from src.infrastructure.appconfig import AppConfigDataFlagReader
from src.services import egress_policy
from src.services.egress_policy import EgressDisabledError, EgressPolicy
from tests.infrastructure.test_appconfig import _Clock, _FakeAppConfigData, _flags


class _FailingFlagReader:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def is_enabled(self, flag_name: str) -> bool:
        raise self._error


@pytest.mark.parametrize(
    "method_name",
    [
        "require_financial_egress",
        "require_uazapi_egress",
    ],
)
@pytest.mark.asyncio
async def test_policy_propagates_cancellation(method_name: str) -> None:
    policy = EgressPolicy(_FailingFlagReader(asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await getattr(policy, method_name)()


@pytest.mark.parametrize(
    ("method_name", "disabled_code"),
    [
        ("require_financial_egress", "financial_egress_disabled"),
        ("require_uazapi_egress", "uazapi_egress_disabled"),
    ],
)
@pytest.mark.asyncio
async def test_policy_maps_operational_errors_to_stable_disabled_code(
    method_name: str,
    disabled_code: str,
) -> None:
    policy = EgressPolicy(_FailingFlagReader(RuntimeError("agent unavailable")))

    with pytest.raises(EgressDisabledError) as exc_info:
        await getattr(policy, method_name)()

    assert exc_info.value.code == disabled_code


# ---------------------------------------------------------------------------
# F2: source selection, the process reader and its lifespan refresher.
# ---------------------------------------------------------------------------

_GATES = [
    (
        "require_financial_egress",
        "financial_egress_enabled",
        "financial_egress_disabled",
    ),
    ("require_uazapi_egress", "uazapi_egress_enabled", "uazapi_egress_disabled"),
]


def _configure(monkeypatch: pytest.MonkeyPatch, *, source: str, enabled: bool) -> None:
    monkeypatch.setattr(settings, "appconfig_source", source)
    monkeypatch.setattr(settings, "appconfig_enabled", enabled)
    monkeypatch.setattr(settings, "appconfig_application", "neurostack")
    monkeypatch.setattr(settings, "appconfig_environment", "production")
    monkeypatch.setattr(settings, "appconfig_profile", "egress-flags")


def _install_reader(
    monkeypatch: pytest.MonkeyPatch,
    fake: _FakeAppConfigData,
    clock: _Clock | None = None,
) -> AppConfigDataFlagReader:
    reader = AppConfigDataFlagReader(
        settings, client_factory=lambda: fake, monotonic=clock or _Clock()
    )
    monkeypatch.setattr(egress_policy, "APPCONFIG_DATA_READER", reader)
    return reader


def _no_agent_http(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(**_: object) -> None:
        pytest.fail("the localhost Agent must not be reached")

    original = egress_policy.AppConfigClient

    def build(config: Any) -> Any:
        return original(config, client_factory=forbidden)

    monkeypatch.setattr(egress_policy, "AppConfigClient", build)


@pytest.mark.parametrize(("gate", "_flag", "code"), _GATES)
@pytest.mark.asyncio
async def test_unconfigured_reader_keeps_todays_closed_behaviour(
    monkeypatch: pytest.MonkeyPatch, gate: str, _flag: str, code: str
) -> None:
    from src.config.settings import Settings

    defaults = Settings(_env_file=None)
    assert (defaults.appconfig_source, defaults.appconfig_enabled) == ("agent", False)
    _configure(monkeypatch, source="agent", enabled=False)
    _no_agent_http(monkeypatch)
    fake = _FakeAppConfigData([])
    _install_reader(monkeypatch, fake)

    with pytest.raises(EgressDisabledError) as exc_info:
        await getattr(egress_policy, gate)()

    assert exc_info.value.code == code
    assert fake.sessions == [] and fake.gets == []
    assert egress_policy.start_egress_flag_refresher() is None


@pytest.mark.parametrize(("gate", "_flag", "code"), _GATES)
@pytest.mark.asyncio
async def test_sdk_source_disabled_is_closed_and_starts_no_refresher(
    monkeypatch: pytest.MonkeyPatch, gate: str, _flag: str, code: str
) -> None:
    _configure(monkeypatch, source="sdk", enabled=False)
    _no_agent_http(monkeypatch)
    fake = _FakeAppConfigData([_flags(financial_egress_enabled=True)])
    reader = _install_reader(monkeypatch, fake)
    assert await reader.refresh_now() is False

    with pytest.raises(EgressDisabledError) as exc_info:
        await getattr(egress_policy, gate)()

    assert exc_info.value.code == code
    assert fake.sessions == []
    assert egress_policy.start_egress_flag_refresher() is None


@pytest.mark.asyncio
async def test_agent_source_never_consults_the_sdk_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, source="agent", enabled=True)
    import httpx

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"financial_egress_enabled": {"enabled": True}}
                )
            ),
            **kwargs,
        )

    original = egress_policy.AppConfigClient
    monkeypatch.setattr(
        egress_policy,
        "AppConfigClient",
        lambda config: original(config, client_factory=factory),
    )
    fake = _FakeAppConfigData([_flags(financial_egress_enabled=False)])
    reader = _install_reader(monkeypatch, fake)
    assert await reader.refresh_now() is True

    await egress_policy.require_financial_egress()  # the Agent said open
    assert egress_policy.start_egress_flag_refresher() is None


@pytest.mark.parametrize(("gate", "flag", "code"), _GATES)
@pytest.mark.parametrize(
    "case", ["open", "closed", "absent", "stale", "error", "never_read"]
)
@pytest.mark.asyncio
async def test_sdk_source_only_open_and_fresh_releases_the_gate(
    monkeypatch: pytest.MonkeyPatch,
    gate: str,
    flag: str,
    code: str,
    case: str,
) -> None:
    _configure(monkeypatch, source="sdk", enabled=True)
    _no_agent_http(monkeypatch)
    other = next(name for _, name, _ in _GATES if name != flag)
    documents = {
        "open": _flags(**{flag: True, other: False}),
        "closed": _flags(**{flag: False, other: True}),
        "absent": _flags(**{other: True}),
        "stale": _flags(**{flag: True}),
        "error": _flags(**{flag: True}),
        "never_read": _flags(**{flag: True}),
    }
    clock = _Clock()
    fake = _FakeAppConfigData([documents[case], RuntimeError("appconfig down")])
    reader = _install_reader(monkeypatch, fake, clock)
    if case != "never_read":
        assert await reader.refresh_now() is True
    if case == "stale":
        clock.now += 3 * reader.poll_interval
    if case == "error":
        assert await reader.refresh_now() is False

    if case == "open":
        await getattr(egress_policy, gate)()
        return
    with pytest.raises(EgressDisabledError) as exc_info:
        await getattr(egress_policy, gate)()
    assert exc_info.value.code == code


@pytest.mark.asyncio
async def test_refresher_starts_only_for_the_enabled_sdk_source_and_stop_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure(monkeypatch, source="sdk", enabled=True)
    fake = _FakeAppConfigData(
        [_flags(financial_egress_enabled=True, uazapi_egress_enabled=True)]
    )
    reader = _install_reader(monkeypatch, fake)
    first_read = asyncio.Event()

    async def run_refresher(**_: object) -> None:
        await reader.refresh_now()
        first_read.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(reader, "run_refresher", run_refresher)

    task = egress_policy.start_egress_flag_refresher()
    assert task is not None
    await asyncio.wait_for(first_read.wait(), 1)
    await egress_policy.require_financial_egress()
    await egress_policy.require_uazapi_egress()

    await egress_policy.stop_egress_flag_refresher(task)

    assert task.done()
    for gate, _, _ in _GATES:
        with pytest.raises(EgressDisabledError):
            await getattr(egress_policy, gate)()
    await egress_policy.stop_egress_flag_refresher(None)  # no-op, never raises


@pytest.mark.asyncio
async def test_stop_never_raises_when_the_refresher_crashed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def crash() -> None:
        raise RuntimeError("refresher crashed")

    task = asyncio.create_task(crash())
    await asyncio.sleep(0)

    await egress_policy.stop_egress_flag_refresher(task)


@pytest.mark.asyncio
async def test_lifespan_starts_and_stops_the_egress_flag_refresher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.api.main as main_module
    import src.repositories as repositories_module

    _configure(monkeypatch, source="sdk", enabled=True)
    fake = _FakeAppConfigData([_flags(financial_egress_enabled=True)])
    reader = _install_reader(monkeypatch, fake)
    first_read = asyncio.Event()
    stopped: list[bool] = []

    async def run_refresher(**_: object) -> None:
        try:
            await reader.refresh_now()
            first_read.set()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            stopped.append(True)
            raise

    async def noop() -> None:
        return None

    monkeypatch.setattr(reader, "run_refresher", run_refresher)
    monkeypatch.setattr(main_module.settings, "metrics_enabled", False)
    monkeypatch.setattr(main_module, "configure_tracing", MagicMock())
    monkeypatch.setattr(main_module, "instrument_redis", MagicMock())
    monkeypatch.setattr(main_module, "instrument_sqlalchemy", MagicMock())
    monkeypatch.setattr(repositories_module, "init_database", noop)
    monkeypatch.setattr(repositories_module, "close_database", noop)
    monkeypatch.setattr(main_module.rlhf, "shutdown", noop)

    async with main_module.lifespan(main_module.app):
        await asyncio.wait_for(first_read.wait(), 1)
        await egress_policy.require_financial_egress()

    assert stopped == [True]
    with pytest.raises(EgressDisabledError):
        await egress_policy.require_financial_egress()
