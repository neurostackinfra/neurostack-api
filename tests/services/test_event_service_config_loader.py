"""Real-loader contract tests for the ``scheduling`` config bucket.

Live WhatsApp turns read clinic config through
``EventService._load_clinic_config`` -> ``_load_config_from_db`` ->
``build_cache_payload`` -> Redis. These tests drive that real path with a
fake DB session and a fake Redis, and prove the scheduling-link flow in the
SRM fires from what the loader returns.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any, AsyncIterator, Optional, cast
from uuid import UUID

import pytest

from neurostack_core.srm.engine import SRMEngine
from neurostack_core.srm.models import FunnelStage, SRMInput
from src.services import event_service

TENANT = UUID("a0000000-0000-0000-0000-0000000000c1")
SESSION = UUID("b0000000-0000-0000-0000-0000000000c1")

SCHEDULING_ON = {
    "scheduling_link_enabled": True,
    "scheduling_link_url": "https://agenda.exemplo.com/marcar",
    "scheduling_link_message": "Para concluir seu agendamento, acesse o link abaixo:",
}


def _row(scheduling: Optional[dict[str, Any]]) -> SimpleNamespace:
    return SimpleNamespace(
        identity=None,
        persona=None,
        operations=None,
        business_rules=None,
        services=None,
        pricing=None,
        handoff=None,
        professionals=None,
        api_credentials={"llm_provider": "gemini"},
        lgpd=None,
        scheduling=scheduling,
    )


class _FakeResult:
    def __init__(self, row: Any) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Any:
        return self._row


class _FakeSession:
    def __init__(self, row: Any) -> None:
        self.row = row

    async def execute(self, _stmt: Any) -> _FakeResult:
        return _FakeResult(self.row)


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> Optional[str]:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: Optional[int] = None) -> None:
        self.store[key] = value

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0

    async def aclose(self) -> None:
        return None


def _install_db(monkeypatch: pytest.MonkeyPatch, row: Any) -> None:
    import src.repositories

    @asynccontextmanager
    async def _fake_get_db_session() -> AsyncIterator[_FakeSession]:
        yield _FakeSession(row)

    monkeypatch.setattr(src.repositories, "get_db_session", _fake_get_db_session)


def _install_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake = _FakeRedis()

    async def _open_redis() -> _FakeRedis:
        return fake

    monkeypatch.setattr(event_service, "_open_redis", _open_redis)
    return fake


def _decide(clinic_config: dict[str, Any]) -> Any:
    decision = SRMEngine().process(
        SRMInput(
            tenant_id=TENANT,
            session_id=SESSION,
            message="sim, pode encaminhar",
            turn_count=3,
            current_funnel_stage=FunnelStage.AGENDAMENTO_SOLICITADO,
            flags={"handoff_confirmation_pending": True},
            clinic_config=clinic_config,
        )
    )
    return decision


async def test_load_config_from_db_returns_scheduling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_db(monkeypatch, _row(dict(SCHEDULING_ON)))
    loaded = await event_service._load_config_from_db(TENANT)
    assert loaded is not None
    assert loaded["scheduling"] == SCHEDULING_ON


async def test_load_config_from_db_scheduling_defaults_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_db(monkeypatch, _row(None))
    loaded = await event_service._load_config_from_db(TENANT)
    assert loaded is not None
    assert loaded["scheduling"] == {}


@pytest.mark.parametrize(
    ("scheduling", "expect_link"),
    [
        (SCHEDULING_ON, True),
        ({**SCHEDULING_ON, "scheduling_link_enabled": False}, False),
        ({**SCHEDULING_ON, "scheduling_link_url": ""}, False),
    ],
    ids=["enabled_with_url", "disabled", "enabled_without_url"],
)
async def test_live_loader_drives_scheduling_link(
    monkeypatch: pytest.MonkeyPatch,
    scheduling: dict[str, Any],
    expect_link: bool,
) -> None:
    _install_db(monkeypatch, _row(dict(scheduling)))
    redis = _install_redis(monkeypatch)
    service = event_service.EventService(pipeline=cast(Any, SimpleNamespace()))

    # Cache miss: loads from DB and writes the projected payload.
    miss = await service._load_clinic_config(TENANT)
    assert miss is not None
    assert redis.store, "loader did not write the cache"

    # Cache hit: reads back the JSON payload written above.
    _install_db(monkeypatch, None)
    hit = await service._load_clinic_config(TENANT)
    assert hit is not None

    for result in (miss, hit):
        assert result["scheduling"] == scheduling
        decision = _decide(result)
        if expect_link:
            assert decision.handoff_verification_status == "link_sent"
        else:
            assert decision.handoff_verification_status is None
            assert decision.handoff_required is True
