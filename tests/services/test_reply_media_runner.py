"""The runner of the agent's media delivery (envio-midia-agente Group 6).

Pinned here:

* the outer bound per provider is the delivery's own worst case
  (``REPLY_MEDIA_MAX_SECONDS``, Meta 375 s, the others 250 s) plus the
  database allowance (5 steps x the pool's 30 s checkout wait = 150 s):
  Meta 525 s, Z-API, uazapi and Evolution 400 s;
* a delivery the bound cuts ends logged (``agent_media_outer_bound``,
  ``reason="outer_bound"``) and counted once as ``failed``, the task never
  raises; one cut after the core counted its outcome (in the notice) is
  logged, never counted twice; on a single-connection pool (SQLite
  ``:memory:``) no bound applies;
* the cut-safe sessions: a cancellation inside a transaction invalidates
  the connection (never pooled), an ordinary error does not; a
  single-connection pool's factory is never wrapped;
* the owned tasks: ``wait_idle`` drains them, ``shutdown`` cancels a
  delivery waiting for the permit and leaves no permit behind; that
  cancellation is logged (``reason="shutdown"``, the session and media ids,
  no phone), counted once as ``failed`` and re-raised.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import event as sa_event
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.api.middleware.media_limits import QueuedLimiter
from src.config.settings import Settings
from src.services import reply_media_runner
from src.services.reply_media import (
    AGENT_MEDIA_PERMIT_WAIT_SECONDS,
    REPLY_MEDIA_MAX_SECONDS,
    ReplyMediaChannel,
)
from src.services.reply_media_runner import (
    REPLY_MEDIA_DB_SECONDS,
    REPLY_MEDIA_DB_STEPS,
    REPLY_MEDIA_OUTER_SECONDS,
    CutSafeSession,
    ReplyMediaJob,
    cut_safe_sessions,
    start_reply_media,
)


class _Logs:
    def __init__(self) -> None:
        self.errors: list[dict[str, Any]] = []

    def error(self, _message: str, **fields: Any) -> None:
        self.errors.append(fields)

    def __getattr__(self, _level: str) -> Any:
        return lambda *_a, **_k: None


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> _Logs:
    recorder = _Logs()
    monkeypatch.setattr(reply_media_runner, "logger", recorder)
    return recorder


@pytest.fixture
async def queued(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """A pooled (``QueuePool``) SQLite file database."""

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'runner.db'}")
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _send(*_args: Any, **_kwargs: Any) -> Any:
    return {"messageId": "m"}


def _job(provider: str = "zapi") -> ReplyMediaJob:
    async def record(_sent: Any) -> None:
        return None

    return ReplyMediaJob(
        channel=ReplyMediaChannel(provider, _send, _send),  # type: ignore[arg-type]
        media_id=uuid4(),
        tenant_id=uuid4(),
        session_id=uuid4(),
        response_language="pt-BR",
        record_sent_id=record,
    )


def _failed(provider: str = "zapi") -> float:
    return (
        REGISTRY.get_sample_value(
            "neurostack_agent_media_delivery_total",
            {"provider": provider, "outcome": "failed"},
        )
        or 0.0
    )


def test_the_outer_bound_covers_the_delivery_and_its_database_steps() -> None:
    assert dict(REPLY_MEDIA_OUTER_SECONDS) == {
        "meta": 525,
        "zapi": 400,
        "uazapi": 400,
        "evolution": 400,
    }
    for provider, worst in REPLY_MEDIA_MAX_SECONDS.items():
        # The wait for the permit, the send's deadline, the notice and its
        # record (G5's worst case) never reach it: only the database time.
        assert REPLY_MEDIA_OUTER_SECONDS[provider] == worst + REPLY_MEDIA_DB_SECONDS
        assert worst > AGENT_MEDIA_PERMIT_WAIT_SECONDS
    default_pool_wait = Settings.model_fields["database_pool_timeout"].default
    assert default_pool_wait == 30
    assert REPLY_MEDIA_DB_SECONDS == REPLY_MEDIA_DB_STEPS * default_pool_wait


async def test_a_delivery_the_bound_cuts_is_logged_and_never_raises(
    queued: async_sessionmaker[AsyncSession],
    logs: _Logs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def hung(**_kwargs: Any) -> Any:
        await asyncio.Event().wait()

    monkeypatch.setattr(reply_media_runner, "deliver_reply_media", hung)
    monkeypatch.setattr(reply_media_runner, "REPLY_MEDIA_OUTER_SECONDS", {"zapi": 0.05})
    failed_before = _failed()
    job = _job()
    task = start_reply_media(job, session_factory=queued, agent_sends=QueuedLimiter(1))
    assert task is not None and task.get_name() == "agent-media-delivery"
    await asyncio.wait_for(task, 5)
    assert task.exception() is None
    [alert] = logs.errors
    assert alert["alert"] == "agent_media_outer_bound"
    assert (alert["reason"], alert["outcome"], alert["counted"]) == (
        "outer_bound",
        "failed",
        True,
    )
    assert alert["bound_seconds"] == 0.05
    assert (alert["session_id"], alert["media_id"]) == (
        str(job.session_id),
        str(job.media_id),
    )
    # Counted once, as ``failed``: visible to the "failed > 2%" trigger.
    assert _failed() == failed_before + 1


async def test_a_cut_after_the_core_counted_its_outcome_is_never_counted_twice(
    queued: async_sessionmaker[AsyncSession],
    logs: _Logs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The core counted ``failed`` (say) and is sending the notice when the
    bound cuts: logged, not counted a second time."""

    async def hung_notice(**kwargs: Any) -> Any:
        await kwargs["channel"].send_text("notice")

    async def hangs(_text: str) -> Any:
        await asyncio.Event().wait()

    job = replace(_job(), channel=ReplyMediaChannel("zapi", _send, hangs))  # type: ignore[arg-type]
    monkeypatch.setattr(reply_media_runner, "deliver_reply_media", hung_notice)
    monkeypatch.setattr(reply_media_runner, "REPLY_MEDIA_OUTER_SECONDS", {"zapi": 0.05})
    failed_before = _failed()
    task = start_reply_media(job, session_factory=queued, agent_sends=QueuedLimiter(1))
    assert task is not None
    await asyncio.wait_for(task, 5)
    [alert] = logs.errors
    assert (alert["reason"], alert["counted"]) == ("outer_bound", False)
    assert _failed() == failed_before


async def test_a_cut_during_the_file_s_record_is_never_counted_twice(
    queued: async_sessionmaker[AsyncSession],
    logs: _Logs,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: list[Any] = []

    async def sent_then_record(**kwargs: Any) -> Any:
        await kwargs["record_sent_id"]({"messageId": "file-1"})

    async def slow_record(sent: Any) -> None:
        recorded.append(sent)
        await asyncio.Event().wait()

    job = replace(_job(), record_sent_id=slow_record)
    monkeypatch.setattr(reply_media_runner, "deliver_reply_media", sent_then_record)
    monkeypatch.setattr(reply_media_runner, "REPLY_MEDIA_OUTER_SECONDS", {"zapi": 0.05})
    failed_before = _failed()
    task = start_reply_media(job, session_factory=queued, agent_sends=QueuedLimiter(1))
    assert task is not None
    await asyncio.wait_for(task, 5)
    assert recorded == [{"messageId": "file-1"}]  # the job's own record ran
    [alert] = logs.errors
    assert (alert["reason"], alert["counted"]) == ("outer_bound", False)
    assert _failed() == failed_before


async def test_a_single_connection_pool_runs_the_delivery_unbounded(
    logs: _Logs, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    memory = async_sessionmaker(engine)
    seen: list[Any] = []

    async def slow(**kwargs: Any) -> Any:
        seen.append(kwargs["session_factory"])
        await asyncio.sleep(0.2)

    monkeypatch.setattr(reply_media_runner, "deliver_reply_media", slow)
    monkeypatch.setattr(reply_media_runner, "REPLY_MEDIA_OUTER_SECONDS", {"zapi": 0.01})
    try:
        task = start_reply_media(
            _job(), session_factory=memory, agent_sends=QueuedLimiter(1)
        )
        assert task is not None
        await asyncio.wait_for(task, 5)
    finally:
        await engine.dispose()
    assert logs.errors == []
    assert seen == [memory]  # never wrapped: nothing is ever invalidated there


async def test_the_delivery_gets_cut_safe_sessions_of_the_caller_s_factory(
    queued: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []

    async def capture(**kwargs: Any) -> Any:
        seen.append(kwargs)

    monkeypatch.setattr(reply_media_runner, "deliver_reply_media", capture)
    job = _job("meta")
    pool = QueuedLimiter(1)
    task = start_reply_media(job, session_factory=queued, agent_sends=pool)
    assert task is not None
    await asyncio.wait_for(task, 5)
    [kwargs] = seen
    assert kwargs["agent_sends"] is pool
    # The job's channel, its notice sender watched (the outcome's count).
    channel = kwargs["channel"]
    assert channel.provider == "meta" and channel.send_media is job.channel.send_media
    assert await channel.send_text("x") == {"messageId": "m"}
    assert kwargs["media_id"] == job.media_id
    assert kwargs["credit_switch_on"] is None
    session = kwargs["session_factory"]()
    try:
        assert isinstance(session, CutSafeSession)
        assert session.bind is queued.kw["bind"]
    finally:
        await session.close()


def test_only_a_pooled_session_maker_is_wrapped() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    memory = async_sessionmaker(engine)
    assert cut_safe_sessions(memory) is memory

    def fake() -> Any:
        return object()

    assert cut_safe_sessions(fake) is fake  # type: ignore[arg-type]


async def test_a_cancellation_inside_a_transaction_invalidates_the_connection(
    queued: async_sessionmaker[AsyncSession],
) -> None:
    sessions = cut_safe_sessions(queued)
    engine = queued.kw["bind"]
    invalidated: list[Any] = []
    sa_event.listen(
        engine.sync_engine.pool,
        "invalidate",
        lambda _dbapi, _record, exception: invalidated.append(exception),
    )
    started = asyncio.Event()

    async def cut() -> None:
        async with sessions() as session:
            await session.execute(text("SELECT 1"))
            started.set()
            await asyncio.Event().wait()  # a statement in flight, say

    task = asyncio.create_task(cut())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(invalidated) == 1
    assert engine.sync_engine.pool.checkedout() == 0

    async def fails() -> None:
        async with sessions() as session:
            await session.execute(text("SELECT 1"))
            raise RuntimeError("an ordinary error: the statement finished")

    with pytest.raises(RuntimeError):
        await fails()
    assert len(invalidated) == 1  # only the cancellation invalidates
    assert engine.sync_engine.pool.checkedout() == 0


async def test_shutdown_cancels_a_delivery_waiting_for_the_permit(
    queued: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pool = QueuedLimiter(1)
    held = await pool.acquire(1)
    assert held is not None

    async def waits(**kwargs: Any) -> Any:
        permit = await kwargs["agent_sends"].acquire(60)
        if permit is not None:
            permit.release()

    monkeypatch.setattr(reply_media_runner, "deliver_reply_media", waits)
    logs = _Logs()
    monkeypatch.setattr(reply_media_runner, "logger", logs)
    failed_before = _failed()
    job = _job()
    task = start_reply_media(job, session_factory=queued, agent_sends=pool)
    assert task is not None
    for _ in range(100):
        if pool.waiting:
            break
        await asyncio.sleep(0.01)
    assert pool.waiting == 1

    await reply_media_runner.shutdown()

    assert task.cancelled()  # the cancellation was re-raised, not swallowed
    assert pool.waiting == 0 and pool.active == 1  # only the test's permit
    held.release()
    assert pool.active == 0
    # Logged with the session and media ids, never the phone; counted once
    # as ``failed`` (the patient got neither the file nor the notice).
    [alert] = logs.errors
    assert (alert["alert"], alert["reason"], alert["outcome"], alert["counted"]) == (
        "agent_media_task",
        "shutdown",
        "failed",
        True,
    )
    assert (alert["session_id"], alert["media_id"]) == (
        str(job.session_id),
        str(job.media_id),
    )
    assert set(alert) == {
        "alert",
        "reason",
        "outcome",
        "counted",
        "provider",
        "tenant_id",
        "session_id",
        "media_id",
    }  # no phone
    assert _failed() == failed_before + 1


async def test_wait_idle_drains_this_loop_s_deliveries(
    queued: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    done: list[str] = []

    async def quick(**_kwargs: Any) -> Any:
        await asyncio.sleep(0.05)
        done.append("delivered")

    monkeypatch.setattr(reply_media_runner, "deliver_reply_media", quick)
    for _ in range(3):
        start_reply_media(_job(), session_factory=queued, agent_sends=QueuedLimiter(1))
    await reply_media_runner.wait_idle()
    assert done == ["delivered"] * 3
