"""The response outbox: inline send, publisher and retention (credit 3b-2).

SQLite, each test against its own database file; the races under real
concurrency (``FOR UPDATE SKIP LOCKED``, two publishers, the inline claim
waiting on the publisher's row lock, a session lock during the bookkeeping)
and the JSONB sweep filter are PostgreSQL-only:
``tests/integration/test_credit_metering_postgres.py``.

What is pinned here (with the owner decisions of the 3b-2 review):

* the constants, the send timeout DERIVED from the real httpx timeouts of
  the senders and the TTS providers (OD-5), and the windows built on it;
* a tick claims nothing with the switch off (no SQL), re-reads the switch
  before every row, and claims nothing before ``credit_enforcement_at``;
* one send per row (OD-1): the publisher only recovers the crash windows --
  a ``pending`` row older than the minimum age, a ``sending`` row older than
  its lease -- and at most once; a failed or timed-out (uncertain) send is
  ``failed`` at once with its PII erased, inline or recovered;
* before ANY send (OD-2): the session's pause state must still be the one
  phase 1 left, the bot on, the reply younger than its maximum age;
* only the provider call is under the send timeout; the bookkeeping after
  it is bounded, shielded and only logs its failure (OD-5);
* the retention sweep runs config on or off and only erases (OD-3);
* no outbox log carries the phone or the text (M6); the lifespan starts the
  sweeper always and the publisher only with the config on.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.models  # noqa: F401  (registers every mapper for create_all)
from src.config.settings import settings
from src.models.base import Base
from src.models.builder import Builder
from src.models.credit import CreditEnforcementState, CreditResponseOutbox
from src.models.session import Session
from src.models.tenant import Tenant
from src.repositories.credit import CreditRepository
from src.repositories.database import _patch_jsonb_for_sqlite
from src.services import credit_metering
from src.services.credit_metering import (
    PUBLISHER_MAX_ATTEMPTS,
    PUBLISHER_MIN_AGE_MARGIN_SECONDS,
    PUBLISHER_MIN_AGE_SECONDS,
    PUBLISHER_POLL_SECONDS,
    REPLY_MAX_AGE_SECONDS,
    REPLY_PAYLOAD_KIND,
    REPLY_RECORD_TIMEOUT_SECONDS,
    REPLY_SEND_TIMEOUT_SECONDS,
    SENDING_LEASE_SECONDS,
    CreditReplyUndeliverable,
    CreditResponsePublisher,
    deliver_reply_inline,
    scrub_reply,
    start_credit_response_publisher,
    stop_credit_response_publisher,
    sweep_expired_replies,
)
from src.services.credit_switch import CreditSwitch

_SECONDS = timedelta(seconds=1)
_PHONE = "5511900001234"
_TEXT = "Olá! Posso ajudar com o seu agendamento."
_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class _Db:
    engine: Any
    sessions: async_sessionmaker[AsyncSession]
    tenant: UUID
    session: UUID


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[_Db]:
    _patch_jsonb_for_sqlite()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'outbox.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    builder, tenant, patient = uuid4(), uuid4(), uuid4()
    async with sessions() as session, session.begin():
        session.add(Builder(id=builder, name="Outbox builder"))
        await session.flush()
        session.add(
            Tenant(
                id=tenant,
                name="Outbox clinic",
                slug=f"outbox-{tenant.hex}",
                api_key_hash=f"unused-{tenant.hex}",
                builder_id=builder,
                config={},
            )
        )
        await session.flush()
        session.add(
            Session(
                id=patient,
                tenant_id=tenant,
                channel="whatsapp",
                cognitive_state={},
                session_metadata={},
            )
        )
    try:
        yield _Db(engine=engine, sessions=sessions, tenant=tenant, session=patient)
    finally:
        await engine.dispose()


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> Any:
    def force(effective: bool) -> None:
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: effective)

    force(True)
    return force


async def _enforce(db: _Db) -> None:
    at = datetime.now(timezone.utc) - timedelta(minutes=1)
    async with db.sessions() as session, session.begin():
        session.add(
            CreditEnforcementState(
                id=1, credit_enforcement_at=at, set_by="test", set_at=at
            )
        )


def _reply(db: _Db, *, decided: timedelta = timedelta(0), **extra: Any) -> dict:
    """A stored reply as ``run_webhook_final`` writes it."""

    return {
        "kind": REPLY_PAYLOAD_KIND,
        "channel": "zapi",
        "tenant_id": str(db.tenant),
        "session_id": str(db.session),
        "phone": _PHONE,
        "text": _TEXT,
        "language_code": "pt-BR",
        "input_was_audio": False,
        "force_text": False,
        "handoff": False,
        "decided_at": (datetime.now(timezone.utc) - decided).isoformat(),
        "session_pause": [False, None],
        **extra,
    }


async def _row(
    db: _Db,
    *,
    state: str = "pending",
    age: timedelta = timedelta(0),
    payload: Mapping[str, Any] | None = None,
    attempts: int = 0,
) -> UUID:
    row_id = uuid4()
    async with db.sessions() as session, session.begin():
        session.add(
            CreditResponseOutbox(
                id=row_id,
                tenant_id=db.tenant,
                effect_id=f"trace:{db.tenant}:{uuid4()}:decision:1",
                payload=dict(_reply(db) if payload is None else payload),
                state=state,
                attempts=attempts,
                created_at=datetime.now(timezone.utc) - age,
            )
        )
    return row_id


async def _get(db: _Db, row_id: UUID) -> CreditResponseOutbox:
    async with db.sessions() as session:
        row = await session.get(CreditResponseOutbox, row_id)
        assert row is not None
        return row


async def _set_session(db: _Db, **values: Any) -> None:
    async with db.sessions() as session, session.begin():
        await session.execute(
            update(Session).where(Session.id == db.session).values(**values)
        )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class _Sender:
    """The injected send adapter (``prepare`` / ``send`` / ``record``)."""

    def __init__(
        self,
        *,
        fail: Exception | None = None,
        refuse: str | None = None,
        send_delay: float = 0,
        record_delay: float = 0,
        record_error: Exception | None = None,
    ) -> None:
        self.prepared: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.recorded: list[Any] = []
        self.fail = fail
        self.refuse = refuse
        self.send_delay = send_delay
        self.record_delay = record_delay
        self.record_error = record_error
        self.release_record: asyncio.Event | None = None

    @property
    def payloads(self) -> list[dict[str, Any]]:
        return self.sent

    async def prepare(self, payload: Mapping[str, Any], db: AsyncSession) -> Any:
        assert isinstance(db, AsyncSession)
        if self.refuse is not None:
            raise CreditReplyUndeliverable(self.refuse)
        self.prepared.append(dict(payload))
        return dict(payload)

    async def send(self, prepared: Any) -> Any:
        self.sent.append(prepared)
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        if self.fail is not None:
            raise self.fail
        return {"messageId": f"sent-{len(self.sent)}"}

    async def record(self, prepared: Any, sent: Any, db: AsyncSession) -> None:
        assert isinstance(db, AsyncSession)
        if self.release_record is not None:
            await self.release_record.wait()
        if self.record_delay:
            await asyncio.sleep(self.record_delay)
        if self.record_error is not None:
            raise self.record_error
        self.recorded.append(sent)


class _Logs:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def _record(self, level: str) -> Any:
        def log(message: str, **fields: Any) -> None:
            self.records.append((level, message, fields))

        return log

    def __getattr__(self, level: str) -> Any:
        return self._record(level)

    def alerts(self, level: str) -> list[dict[str, Any]]:
        return [f for lvl, _m, f in self.records if lvl == level and "alert" in f]


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> _Logs:
    recorder = _Logs()
    monkeypatch.setattr(credit_metering, "logger", recorder)
    return recorder


def _publisher(db: _Db, sender: _Sender) -> CreditResponsePublisher:
    return CreditResponsePublisher(sender, session_factory=db.sessions)


async def _inline(db: _Db, row: UUID, sender: _Sender) -> Any:
    return await deliver_reply_inline(row, sender=sender, session_factory=db.sessions)


class _Statements:
    def __init__(self, engine: Any) -> None:
        self.seen: list[str] = []
        self.engine = engine.sync_engine

    def __enter__(self) -> "_Statements":
        sa_event.listen(self.engine, "before_cursor_execute", self.record)
        return self

    def __exit__(self, *_exc: Any) -> None:
        sa_event.remove(self.engine, "before_cursor_execute", self.record)

    def record(self, _conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        self.seen.append(statement)


# ---------------------------------------------------------------------------
# The constants: the send timeout derived from the real providers (OD-5).
# ---------------------------------------------------------------------------


def _request_worst_cases(module_path: str) -> dict[str, float]:
    """Per function of ``module_path``: the sum, over its ``httpx.Timeout``
    requests, of connect + write + read (``httpx.Timeout(x, connect=c)`` sets
    read = write = pool = x) -- the worst case of one request with one
    operation per phase and no pool wait (a fresh client per call)."""

    tree = ast.parse((_ROOT / module_path).read_text(encoding="utf-8"))
    worst: dict[str, float] = {}
    for function in ast.walk(tree):
        if not isinstance(function, ast.AsyncFunctionDef):
            continue
        total = 0.0
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "Timeout"
            ):
                default = float(ast.literal_eval(node.args[0]))
                connect = default
                for keyword in node.keywords:
                    if keyword.arg == "connect":
                        connect = float(ast.literal_eval(keyword.value))
                total += connect + 2 * default
        if total:
            worst[function.name] = total
    return worst


def test_the_send_timeout_covers_the_real_worst_case_of_a_reply() -> None:
    """``_send_reply_with_optional_tts`` synthesizes (TTS), sends the audio,
    and on an audio failure falls back to the text: the worst path of one
    reply is TTS + audio + text, from the providers' own httpx timeouts."""

    from src.api.routes import webhook

    called = {
        getattr(node.func, "id", getattr(node.func, "attr", ""))
        for node in ast.walk(
            ast.parse(inspect.getsource(webhook._send_reply_with_optional_tts))
        )
        if isinstance(node, ast.Call)
    }
    assert {"synthesize_speech", "send_audio", "send_text"} <= called
    tts = _request_worst_cases("src/services/tts_service.py")
    zapi = _request_worst_cases("src/services/zapi_service.py")
    meta = _request_worst_cases("src/services/meta_service.py")
    synthesis = max(
        tts[name]
        for name in (
            "_synthesize_elevenlabs",
            "_synthesize_google",
            "_synthesize_openai",
        )
    )
    zapi_reply = synthesis + zapi["send_zapi_audio"] + zapi["send_text_message"]
    meta_reply = (
        synthesis + meta["send_meta_cloud_audio"] + meta["send_meta_cloud_text"]
    )
    assert (synthesis, zapi_reply, meta_reply) == (100, 200, 235)
    assert REPLY_SEND_TIMEOUT_SECONDS >= max(zapi_reply, meta_reply)
    # The bookkeeping bound covers its provider call (mark unread on Z-API).
    assert REPLY_RECORD_TIMEOUT_SECONDS >= zapi["mark_conversation_unread"]


def test_the_delivery_windows_are_pinned_on_the_send_timeout() -> None:
    assert REPLY_SEND_TIMEOUT_SECONDS == 240
    assert REPLY_RECORD_TIMEOUT_SECONDS == 30
    assert PUBLISHER_MIN_AGE_MARGIN_SECONDS > 0
    assert (
        PUBLISHER_MIN_AGE_SECONDS
        > REPLY_SEND_TIMEOUT_SECONDS + PUBLISHER_MIN_AGE_MARGIN_SECONDS
    )
    assert SENDING_LEASE_SECONDS > (
        REPLY_SEND_TIMEOUT_SECONDS + REPLY_RECORD_TIMEOUT_SECONDS
    )
    # ~10 minutes, and still past both recovery windows (OD-2).
    assert REPLY_MAX_AGE_SECONDS == 600
    assert REPLY_MAX_AGE_SECONDS > PUBLISHER_MIN_AGE_SECONDS + PUBLISHER_POLL_SECONDS
    assert REPLY_MAX_AGE_SECONDS > SENDING_LEASE_SECONDS + PUBLISHER_POLL_SECONDS
    assert PUBLISHER_MAX_ATTEMPTS == 1  # OD-1: the publisher sends a row once
    assert credit_metering.REPLY_PII_KEYS == ("phone", "text")


# ---------------------------------------------------------------------------
# The worker guard: nothing with the switch off, nothing before enforcement.
# ---------------------------------------------------------------------------


async def test_with_the_switch_off_a_tick_runs_no_sql(db: _Db, switch: Any) -> None:
    switch(False)
    await _enforce(db)
    row = await _row(db, age=timedelta(hours=1))
    sender = _Sender()
    with _Statements(db.engine) as statements:
        assert await _publisher(db, sender).run_once() is None
    assert statements.seen == [] and sender.payloads == []
    assert (await _get(db, row)).state == "pending"


async def test_the_switch_is_re_read_before_every_row(
    db: _Db, switch: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enforce(db)
    first = await _row(db, age=timedelta(hours=2))
    second = await _row(db, age=timedelta(hours=1))

    class _TurnsOff(_Sender):
        async def send(self, prepared: Any) -> Any:
            switch(False)  # the switch goes off while the first row is sent
            return await super().send(prepared)

    sender = _TurnsOff()
    publisher = _publisher(db, sender)
    switch(True)
    tick = await publisher.run_once()
    assert tick is not None and tick.outcomes == ["delivered"]
    assert (await _get(db, first)).state == "delivered"
    assert (await _get(db, second)).state == "pending"  # never claimed


async def test_before_the_enforcement_a_tick_claims_nothing(
    db: _Db, switch: Any
) -> None:
    row = await _row(db, age=timedelta(days=3))
    sender = _Sender()
    with _Statements(db.engine) as statements:
        tick = await _publisher(db, sender).run_once()
    assert tick is not None and tick.enforced is False and tick.outcomes == []
    stored = await _get(db, row)
    assert stored.state == "pending" and stored.attempts == 0
    assert stored.payload.get("phone") and sender.payloads == []
    touched = {s.split("FROM")[-1].split()[0] for s in statements.seen if "FROM" in s}
    assert touched == {"credit_enforcement_state"}
    assert not [s for s in statements.seen if "credit_response_outbox" in s]


# ---------------------------------------------------------------------------
# One send per row (OD-1): the publisher recovers crash windows only.
# ---------------------------------------------------------------------------


async def test_a_young_pending_row_is_left_to_the_inline_send(
    db: _Db, switch: Any
) -> None:
    await _enforce(db)
    young = await _row(db, age=timedelta(seconds=PUBLISHER_MIN_AGE_SECONDS - 5))
    old = await _row(db, age=timedelta(seconds=PUBLISHER_MIN_AGE_SECONDS + 5))
    sender = _Sender()
    tick = await _publisher(db, sender).run_once()
    assert tick is not None and tick.outcomes == ["delivered"]
    assert (await _get(db, young)).state == "pending"
    delivered = await _get(db, old)
    assert (delivered.state, delivered.attempts) == ("delivered", 1)
    assert "phone" not in delivered.payload and "text" not in delivered.payload
    assert delivered.payload["kind"] == REPLY_PAYLOAD_KIND
    [sent] = sender.payloads
    assert sent["phone"] == _PHONE and sent["text"] == _TEXT  # the send had them
    assert len(sender.recorded) == 1  # the bookkeeping ran


async def test_a_failed_recovered_send_is_failed_at_once_and_never_resent(
    db: _Db, switch: Any, logs: _Logs
) -> None:
    await _enforce(db)
    row = await _row(db, age=timedelta(hours=1))
    sender = _Sender(fail=RuntimeError("provider 502"))
    publisher = _publisher(db, sender)
    tick = await publisher.run_once()
    assert tick is not None and tick.outcomes == ["failed"]
    failed = await _get(db, row)
    assert (failed.state, failed.attempts) == ("failed", 1)
    assert "phone" not in failed.payload and "text" not in failed.payload
    again = await publisher.run_once()
    assert again is not None and again.outcomes == []
    assert len(sender.payloads) == 1
    [alert] = logs.alerts("error")
    assert (alert["alert"], alert["reason"]) == (
        "credit_response_failed",
        "send_failed",
    )


@pytest.mark.parametrize(
    "reason", ["no_credentials", "uazapi_egress_disabled", "payload_unusable"]
)
async def test_a_refused_preparation_is_failed_without_a_send(
    db: _Db, switch: Any, logs: _Logs, reason: str
) -> None:
    """Missing credentials, a closed egress, a 4xx: never retried (OD-1)."""

    await _enforce(db)
    row = await _row(db, age=timedelta(hours=1))
    sender = _Sender(refuse=reason)
    tick = await _publisher(db, sender).run_once()
    assert tick is not None and tick.outcomes == ["failed"]
    assert (await _get(db, row)).state == "failed" and sender.payloads == []
    assert [a["reason"] for a in logs.alerts("error")] == [reason]


async def test_a_row_left_sending_is_recovered_once_after_its_lease(
    db: _Db, switch: Any, logs: _Logs
) -> None:
    """The send -> mark crash window of the INLINE send (attempts 0): one
    resend after the lease, the accepted at-least-once case.  A row the
    publisher itself already took (attempts 1) is ``failed`` without a send:
    its send is uncertain and never repeated.  Rows inside the lease wait."""

    await _enforce(db)
    lease = timedelta(seconds=SENDING_LEASE_SECONDS + 5)
    inline_crash = await _row(db, state="sending", age=lease)
    publisher_crash = await _row(db, state="sending", age=lease, attempts=1)
    in_flight = await _row(
        db, state="sending", age=timedelta(seconds=SENDING_LEASE_SECONDS - 5)
    )
    sender = _Sender()
    tick = await _publisher(db, sender).run_once()
    assert tick is not None and sorted(tick.outcomes) == ["delivered", "failed"]
    assert (await _get(db, inline_crash)).state == "delivered"
    refused = await _get(db, publisher_crash)
    assert refused.state == "failed" and "phone" not in refused.payload
    assert (await _get(db, in_flight)).state == "sending"
    assert len(sender.payloads) == 1
    assert "resend_refused" in [a["reason"] for a in logs.alerts("error")]


async def test_a_swept_pending_row_fails_without_a_send(
    db: _Db, switch: Any, logs: _Logs
) -> None:
    await _enforce(db)
    row = await _row(db, age=timedelta(hours=1), payload={})
    sender = _Sender()
    tick = await _publisher(db, sender).run_once()
    assert tick is not None and tick.outcomes == ["failed"]
    assert (await _get(db, row)).state == "failed" and sender.payloads == []
    [alert] = logs.alerts("error")
    assert alert["reason"] == "payload_unusable"


async def test_one_row_at_a_time_each_claim_committed_before_its_send(
    db: _Db, switch: Any
) -> None:
    await _enforce(db)
    first = await _row(db, age=timedelta(hours=2))
    second = await _row(db, age=timedelta(hours=1))
    seen: list[tuple[str, str]] = []

    class _Peek(_Sender):
        async def send(self, prepared: Any) -> Any:
            states = [(await _get(db, r)).state for r in (first, second)]
            seen.append((states[0], states[1]))
            return await super().send(prepared)

    tick = await _publisher(db, _Peek()).run_once()
    assert tick is not None and tick.outcomes == ["delivered", "delivered"]
    assert seen == [("sending", "pending"), ("delivered", "sending")]


# ---------------------------------------------------------------------------
# OD-2: re-check the session, the bot and the age before ANY send.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["inline", "publisher"])
@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("takeover", "session_changed"),
        ("reactivation", "session_changed"),
        ("bot_off", "bot_inactive"),
        ("expired", "reply_expired"),
        ("session_gone", "session_gone"),
    ],
)
async def test_a_reply_is_never_sent_once_the_conversation_moved_on(
    db: _Db,
    switch: Any,
    logs: _Logs,
    path: str,
    change: str,
    reason: str,
) -> None:
    await _enforce(db)
    paused_until = datetime.now(timezone.utc) + timedelta(hours=1)
    payload = _reply(db)
    if change == "takeover":
        await _set_session(db, is_paused=True, paused_until=paused_until)
    elif change == "reactivation":
        # Phase 1 left the session paused (a handoff); it was reactivated.
        payload["session_pause"] = [True, paused_until.isoformat()]
    elif change == "bot_off":
        async with db.sessions() as session, session.begin():
            await session.execute(
                update(Tenant).where(Tenant.id == db.tenant).values(bot_active=False)
            )
    elif change == "expired":
        payload = _reply(db, decided=timedelta(seconds=REPLY_MAX_AGE_SECONDS + 1))
    else:
        payload["session_id"] = str(uuid4())
    if path == "inline":
        row = await _row(db, payload=payload)
        delivery = await _inline(db, row, sender := _Sender())
        assert (delivery.outcome, delivery.reason) == ("failed", reason)
    else:
        row = await _row(db, payload=payload, age=timedelta(hours=1))
        tick = await _publisher(db, sender := _Sender()).run_once()
        assert tick is not None and tick.outcomes == ["failed"]
    assert sender.prepared == [] and sender.payloads == []
    stored = await _get(db, row)
    assert stored.state == "failed" and "phone" not in stored.payload
    assert [a["reason"] for a in logs.alerts("error")] == [reason]


async def test_the_decisions_own_pause_does_not_stop_its_reply(db: _Db) -> None:
    """A handoff decision paused the session in phase 1: the snapshot holds
    that pause, so its handoff reply still goes out."""

    paused_until = datetime.now(timezone.utc) + timedelta(hours=1)
    await _set_session(db, is_paused=True, paused_until=paused_until)
    async with db.sessions() as session:
        snapshot = await CreditRepository(session).session_pause_state(
            db.tenant, db.session
        )
    assert snapshot is not None and snapshot[0] is True
    row = await _row(db, payload=_reply(db, session_pause=snapshot))
    sender = _Sender()
    delivery = await _inline(db, row, sender)
    assert delivery.outcome == "delivered" and len(sender.payloads) == 1


# ---------------------------------------------------------------------------
# The inline send, the provider timeout and the shielded bookkeeping (OD-5).
# ---------------------------------------------------------------------------


async def test_the_inline_send_delivers_and_erases_the_pii(db: _Db) -> None:
    row = await _row(db)
    sender = _Sender()
    delivery = await _inline(db, row, sender)
    assert delivery.outcome == "delivered"
    stored = await _get(db, row)
    assert stored.state == "delivered" and stored.attempts == 0  # inline: no attempt
    assert stored.payload == scrub_reply(sender.payloads[0])
    assert len(sender.recorded) == 1


@pytest.mark.parametrize(
    ("sender", "reason", "error"),
    [
        (_Sender(fail=RuntimeError("provider down")), "send_failed", "provider down"),
        (_Sender(refuse="no_credentials"), "no_credentials", None),
    ],
)
async def test_a_failed_inline_send_is_failed_at_once_like_main(
    db: _Db, switch: Any, logs: _Logs, sender: _Sender, reason: str, error: Any
) -> None:
    """OD-1: main loses a reply whose send failed; so does the enforced path
    -- ``failed`` at once, PII erased, and the publisher never sends it."""

    await _enforce(db)
    row = await _row(db)
    delivery = await _inline(db, row, sender)
    assert (delivery.outcome, delivery.reason, delivery.error) == (
        "failed",
        reason,
        error,
    )
    stored = await _get(db, row)
    assert stored.state == "failed" and "phone" not in stored.payload
    # Even once old enough for the publisher: nothing to recover.
    async with db.sessions() as session, session.begin():
        await session.execute(
            update(CreditResponseOutbox)
            .where(CreditResponseOutbox.id == row)
            .values(created_at=datetime.now(timezone.utc) - timedelta(hours=1))
        )
    later = _Sender()
    tick = await _publisher(db, later).run_once()
    assert tick is not None and tick.outcomes == [] and later.payloads == []


async def test_a_timed_out_provider_call_is_uncertain_and_never_resent(
    db: _Db, switch: Any, logs: _Logs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(credit_metering, "REPLY_SEND_TIMEOUT_SECONDS", 0.05)
    await _enforce(db)
    row = await _row(db)
    delivery = await _inline(db, row, _Sender(send_delay=5))
    assert (delivery.outcome, delivery.reason, delivery.error) == (
        "failed",
        "send_failed",
        "send_timeout",
    )
    stored = await _get(db, row)
    assert stored.state == "failed" and "phone" not in stored.payload
    assert [a["reason"] for a in logs.alerts("error")] == ["send_uncertain"]


async def test_only_the_provider_call_is_under_the_send_timeout(
    db: _Db, logs: _Logs, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow bookkeeping (the echo id waiting on a session lock) never turns
    an accepted send into a failure; a failed bookkeeping only logs."""

    monkeypatch.setattr(credit_metering, "REPLY_SEND_TIMEOUT_SECONDS", 0.2)
    slow = await _row(db)
    sender = _Sender(record_delay=0.5)
    assert (await _inline(db, slow, sender)).outcome == "delivered"
    assert (await _get(db, slow)).state == "delivered" and len(sender.recorded) == 1
    broken = await _row(db)
    failing = _Sender(record_error=RuntimeError("session row locked"))
    assert (await _inline(db, broken, failing)).outcome == "delivered"
    assert (await _get(db, broken)).state == "delivered"
    assert [a["reason"] for a in logs.alerts("warning")] == ["record_failed"]


async def test_the_bookkeeping_has_its_own_bound(
    db: _Db, logs: _Logs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(credit_metering, "REPLY_RECORD_TIMEOUT_SECONDS", 0.05)
    row = await _row(db)
    sender = _Sender(record_delay=5)
    assert (await _inline(db, row, sender)).outcome == "delivered"
    assert (await _get(db, row)).state == "delivered" and sender.recorded == []
    [alert] = logs.alerts("warning")
    assert (alert["reason"], alert["error_type"]) == ("record_failed", "TimeoutError")


async def test_the_bookkeeping_and_the_mark_survive_a_cancelled_caller(
    db: _Db,
) -> None:
    """Once the provider returned, the request being cancelled (a shutdown)
    neither loses the echo bookkeeping nor leaves the row ``sending``."""

    row = await _row(db)
    sender = _Sender()
    sender.release_record = asyncio.Event()
    task = asyncio.create_task(_inline(db, row, sender))
    for _ in range(200):
        if sender.payloads:
            break
        await asyncio.sleep(0.01)
    assert sender.payloads  # the provider returned; the bookkeeping waits
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    sender.release_record.set()
    for _ in range(200):
        if (await _get(db, row)).state == "delivered":
            break
        await asyncio.sleep(0.01)
    assert (await _get(db, row)).state == "delivered" and len(sender.recorded) == 1


# ---------------------------------------------------------------------------
# The fences and the inline/publisher race.
# ---------------------------------------------------------------------------


async def test_every_transition_out_of_sending_is_fenced_by_its_observed_claim(
    db: _Db,
) -> None:
    row = await _row(db, age=timedelta(hours=1))
    async with db.sessions() as session:
        claim = await CreditRepository(session).claim_outbox_for_publish(
            min_age=timedelta(0), sending_lease=timedelta(hours=1)
        )
        await session.commit()
    assert claim is not None and claim.id == row and claim.attempts == 1
    # Another owner reclaimed the row (its claim timed out): new created_at.
    async with db.sessions() as session, session.begin():
        await session.execute(
            update(CreditResponseOutbox)
            .where(CreditResponseOutbox.id == row)
            .values(created_at=datetime.now(timezone.utc) + _SECONDS)
        )
    async with db.sessions() as session:
        repository = CreditRepository(session)
        scrubbed = scrub_reply(claim.payload)
        assert not await repository.mark_outbox_delivered(
            row, observed=claim.observed, payload=scrubbed
        )
        assert not await repository.mark_outbox_failed(
            row, observed=claim.observed, payload=scrubbed
        )
        await session.commit()
    stale = await _get(db, row)
    assert stale.state == "sending" and stale.payload["phone"]


async def test_the_inline_send_never_sends_a_row_the_publisher_took(
    db: _Db, switch: Any
) -> None:
    await _enforce(db)
    row = await _row(db, age=timedelta(hours=1))
    async with db.sessions() as session:
        assert await CreditRepository(session).claim_outbox_for_publish(
            min_age=timedelta(0), sending_lease=timedelta(hours=1)
        )
        await session.commit()
    inline = _Sender()
    delivery = await _inline(db, row, inline)
    assert delivery.outcome == "not_claimed" and inline.payloads == []


async def test_the_publisher_never_takes_the_row_the_inline_send_holds(
    db: _Db, switch: Any
) -> None:
    await _enforce(db)
    row = await _row(db, age=timedelta(hours=1))
    published = _Sender()
    publisher = _publisher(db, published)
    ticks: list[Any] = []

    class _Inline(_Sender):
        async def send(self, prepared: Any) -> Any:
            ticks.append(await publisher.run_once())
            return await super().send(prepared)

    inline = _Inline()
    delivery = await _inline(db, row, inline)
    assert delivery.outcome == "delivered"
    assert [t.outcomes for t in ticks] == [[]] and published.payloads == []
    assert len(inline.payloads) == 1
    stored = await _get(db, row)
    assert stored.state == "delivered" and "phone" not in stored.payload


# ---------------------------------------------------------------------------
# M6: no outbox log carries the phone or the text.
# ---------------------------------------------------------------------------


async def test_no_outbox_log_carries_the_phone_or_the_text(
    db: _Db, switch: Any, logs: _Logs, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _enforce(db)
    monkeypatch.setattr(credit_metering, "REPLY_SEND_TIMEOUT_SECONDS", 0.05)
    await _inline(db, await _row(db), _Sender(fail=RuntimeError("down")))
    await _inline(db, await _row(db), _Sender(refuse="no_credentials"))
    await _inline(db, await _row(db), _Sender(send_delay=1))
    await _inline(db, await _row(db), _Sender(record_error=RuntimeError("x")))
    await _set_session(db, is_paused=True)
    await _inline(db, await _row(db), _Sender())
    await _row(db, age=timedelta(hours=1))
    await _row(db, state="sending", age=timedelta(hours=1), attempts=1)
    await _publisher(db, _Sender()).run_once()
    assert len(logs.records) >= 7
    for level, message, fields in logs.records:
        rendered = repr((message, fields))
        assert _PHONE not in rendered and _TEXT not in rendered, (level, fields)


# ---------------------------------------------------------------------------
# The retention sweep (OWNER-d; OD-3: config on or off, erase only).
# ---------------------------------------------------------------------------


async def _sweep_world(db: _Db) -> dict[str, UUID]:
    retention = timedelta(hours=settings.credit_replay_retention_hours)
    old = retention + timedelta(minutes=1)
    return {
        "delivered": await _row(db, state="delivered", age=old),
        "failed": await _row(db, state="failed", age=old),
        # Abandoned while the switch was off (never claimed).
        "pending": await _row(db, state="pending", age=old),
        "events": await _row(
            db,
            state="delivered",
            age=old,
            payload={"event_id": str(uuid4()), "prompt": {"suggested_response": "x"}},
        ),
        "sending": await _row(db, state="sending", age=old),
        "young": await _row(
            db, state="delivered", age=retention - timedelta(minutes=5)
        ),
    }


@pytest.mark.parametrize("config_on", [False, True])
async def test_the_sweep_empties_every_old_row_that_is_not_sending(
    db: _Db, switch: Any, monkeypatch: pytest.MonkeyPatch, config_on: bool
) -> None:
    """OD-3: whatever the switch and the config (here: off, and on), and
    without any enforcement row: only erasing, never a send or a claim."""

    switch(config_on)
    monkeypatch.setattr(settings, "credit_hot_path_enabled", config_on)
    rows = await _sweep_world(db)
    assert await sweep_expired_replies(db.sessions) == 4
    for name in ("delivered", "failed", "pending", "events"):
        assert (await _get(db, rows[name])).payload == {}, name
    pending = await _get(db, rows["pending"])
    assert (pending.state, pending.attempts) == ("pending", 0)  # not claimed
    assert (await _get(db, rows["sending"])).payload["phone"]
    assert (await _get(db, rows["young"])).payload["phone"]
    assert await sweep_expired_replies(db.sessions) == 0  # never rewritten


async def test_the_sweeper_task_sweeps_with_the_switch_off(
    db: _Db, switch: Any
) -> None:
    switch(False)
    rows = await _sweep_world(db)
    task = asyncio.create_task(
        credit_metering.run_retention_sweeper(0.01, session_factory=db.sessions)
    )
    try:
        for _ in range(300):
            if (await _get(db, rows["pending"])).payload == {}:
                break
            await asyncio.sleep(0.02)
    finally:
        await credit_metering.stop_credit_retention_sweeper(task)
    assert (await _get(db, rows["pending"])).payload == {}
    assert task.cancelled()


# ---------------------------------------------------------------------------
# The lifespan.
# ---------------------------------------------------------------------------


async def test_the_publisher_task_starts_only_with_the_config_on() -> None:
    sender = _Sender()
    off = SimpleNamespace(credit_hot_path_enabled=False)
    assert start_credit_response_publisher(sender, off) is None  # type: ignore[arg-type]
    on = SimpleNamespace(credit_hot_path_enabled=True)
    task = start_credit_response_publisher(sender, on)  # type: ignore[arg-type]
    assert task is not None and task.get_name() == "credit-response-publisher"
    await asyncio.sleep(0)
    await stop_credit_response_publisher(task)
    assert task.cancelled()
    await stop_credit_response_publisher(None)  # nothing to stop


@pytest.fixture
def quiet_refresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(settings, "credit_switch_refresh_seconds", 3600)
    yield


def _running(name: str) -> list[asyncio.Task[Any]]:
    return [
        task
        for task in asyncio.all_tasks()
        if task.get_name() == name and not task.done()
    ]


@pytest.mark.parametrize("config_on", [False, True])
async def test_the_lifespan_starts_and_cancels_its_outbox_tasks(
    quiet_refresh: None, monkeypatch: pytest.MonkeyPatch, config_on: bool
) -> None:
    from src.api import main as main_module

    monkeypatch.setattr(settings, "credit_hot_path_enabled", config_on)
    async with main_module.app.router.lifespan_context(main_module.app):
        sweepers = _running("credit-retention-sweep")
        publishers = _running("credit-response-publisher")
        assert len(sweepers) == 1  # always (OD-3)
        assert len(publishers) == (1 if config_on else 0)
    assert all(task.cancelled() for task in sweepers + publishers)
    assert _running("credit-retention-sweep") == []
    assert _running("credit-response-publisher") == []


def test_the_publisher_gets_the_webhook_route_s_sender() -> None:
    from src.api import main as main_module
    from src.api.routes import webhook

    tree = ast.parse(inspect.getsource(main_module.lifespan))
    [start] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "start_credit_response_publisher"
    ]
    assert ast.unparse(start.args[0]) == "webhook.CREDIT_REPLY_SENDER"
    assert main_module.webhook is webhook
    assert webhook.credit_metering is credit_metering
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(ast.parse(inspect.getsource(credit_metering)))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not [name for name in imported if name and "src.api.routes" in name]


def test_install_spa_is_still_the_last_registration() -> None:
    from src.api import main as main_module

    tree = ast.parse(inspect.getsource(main_module.create_app))
    registrations = [
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            getattr(node.func, "id", "") == "install_spa"
            or getattr(node.func, "attr", "") in {"include_router", "add_api_route"}
        )
    ]
    assert registrations and registrations[-1] == "install_spa"


async def test_the_outbox_rows_carry_no_credential(db: _Db) -> None:
    """Every key the route stores (``_credit_reply_payload``) is a send
    argument; ``run_webhook_final`` adds only ``decided_at`` and
    ``session_pause``: no token, secret or credential."""

    from src.api.routes import webhook

    response = SimpleNamespace(
        prompt=SimpleNamespace(suggested_response="Olá!", deterministic=True),
        response_language="pt-BR",
        handoff_summary=None,
        decision=SimpleNamespace(handoff_required=False),
    )
    request = SimpleNamespace(tenant_id=uuid4(), session_id=uuid4())
    payload = webhook._credit_reply_payload(
        webhook._ReplyTarget("zapi", "5511900000000", True),
        request,  # type: ignore[arg-type]
        response,  # type: ignore[arg-type]
    )
    assert payload is not None
    stored = {**payload, "decided_at": "x", "session_pause": [False, None]}
    assert set(stored) == {
        "kind",
        "channel",
        "tenant_id",
        "session_id",
        "phone",
        "text",
        "language_code",
        "input_was_audio",
        "force_text",
        "handoff",
        "decided_at",
        "session_pause",
    }
    assert payload["force_text"] is True and payload["input_was_audio"] is True
    assert not [k for k in stored if any(w in k for w in ("token", "secret", "key"))]
    nothing = SimpleNamespace(
        prompt=SimpleNamespace(suggested_response=None),
        response_language="pt-BR",
        handoff_summary=None,
        decision=None,
    )
    assert (
        webhook._credit_reply_payload(
            webhook._ReplyTarget("meta", "5511", False),
            request,  # type: ignore[arg-type]
            nothing,  # type: ignore[arg-type]
        )
        is None
    )
    assert set(scrub_reply(stored)) == set(stored) - {"phone", "text"}
    async with db.sessions() as session:
        rows = (await session.execute(select(CreditResponseOutbox))).scalars().all()
    assert rows == []
