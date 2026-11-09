"""The file of a stored webhook reply (envio-midia-agente Group 6), SQLite.

The credit response outbox delivers an enforced Z-API/Meta reply inline or,
after a crash, from the publisher.  When the reply's turn chose a Central de
Mídias file, the outbox row carries its ``media_id`` and the file goes as a
step AFTER ``_finish_delivered`` (the bookkeeping, then the ``delivered``
mark), started as a task of its own by ``CreditReplySender.start_media``.
Each test owns a SQLite database file; the real ``CreditReplySender`` (its
``prepare`` / ``send`` / ``record``), the real outbox and the real delivery
core run, with the provider calls, the flag, the credit mode and the
PostgreSQL-only claim as stand-ins (the claim's real proof of "no resend":
``tests/integration/test_webhook_media_delivery_postgres.py``).  Pinned:

* the stored reply carries ``media_id`` only when its turn chose a file;
* the file starts only after the text was sent, recorded and marked
  ``delivered`` -- with the in-memory phone and credentials, the row's
  being erased by then -- through the provider the selection was given; a
  text the provider refused turns the row ``failed`` and starts no file;
* it never runs inside the publisher's serial tick: the tick delivers its
  next row while a file is still being sent;
* a file that fails (or a start that raises) never changes the text's row;
* a recovered row whose mark was lost resends the text, never the file;
* the file's echo bookkeeping is ``CreditReplySender.record`` on a session of
  its own, never marks the conversation unread and never swallows a
  cancellation -- it re-raises it, the delivery's record bound can cut it,
  and the cut connection is invalidated;
* the pool reaches the publisher through ``start_credit_response_publisher``
  -- the application's one ``agent_sends`` -- and the outbox budgets are
  unchanged.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.models  # noqa: F401  (registers every mapper for create_all)
from src.api.middleware.media_limits import QueuedLimiter
from src.api.routes import webhook
from src.api.schemas.events import CanonicalEventRequest, EventPayload, EventType
from src.models.base import Base
from src.models.builder import Builder
from src.models.clinic_config_table import ClinicConfigTable
from src.models.credit import CreditEnforcementState, CreditResponseOutbox
from src.models.media_asset import MediaAsset
from src.models.media_delivery import MediaDelivery
from src.models.session import Session
from src.models.tenant import Tenant
from src.repositories import media_deliveries
from src.repositories.credit import CreditRepository
from src.repositories.database import _patch_jsonb_for_sqlite
from src.services import (
    credit_inventory,
    credit_metering,
    egress_policy,
    reply_media,
    reply_media_runner,
)
from src.services.credit_inventory import CREDIT_OFF
from src.services.credit_metering import (
    PUBLISHER_MIN_AGE_SECONDS,
    REPLY_MAX_AGE_SECONDS,
    REPLY_PAYLOAD_KIND,
    REPLY_RECORD_TIMEOUT_SECONDS,
    REPLY_SEND_TIMEOUT_SECONDS,
    SENDING_LEASE_SECONDS,
    CreditResponsePublisher,
    deliver_reply_inline,
)
from src.services.credit_switch import CreditSwitch
from src.services.media_assets import media_payload_sha256
from src.services.reply_media import ReplyMediaChannel

_PHONE = "5511900007777"
_TEXT = "Segue o preparo do exame."
_WAIT = 10.0


@dataclass(frozen=True)
class _Db:
    engine: Any
    sessions: async_sessionmaker[AsyncSession]
    tenant: UUID
    session: UUID


def _pdf() -> bytes:
    return b"%PDF-1.7\n" + b"7" * 491


def _asset(tenant: UUID) -> MediaAsset:
    body = _pdf()
    return MediaAsset(
        id=uuid4(),
        tenant_id=tenant,
        display_name="Preparo do exame",
        original_filename="preparo.pdf",
        category="pdf",
        mime_type="application/pdf",
        size_bytes=len(body),
        sha256=media_payload_sha256(body),
        payload=body,
        usage_trigger="quando perguntarem do preparo",
        auto_suggest=True,
        trigger_intents=["detalhes_servicos"],
    )


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[_Db]:
    _patch_jsonb_for_sqlite()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'media.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    builder, tenant, patient = uuid4(), uuid4(), uuid4()
    async with sessions() as session, session.begin():
        session.add(Builder(id=builder, name="Media builder"))
        await session.flush()
        session.add(
            Tenant(
                id=tenant,
                name="Media clinic",
                slug=f"media-{tenant.hex}",
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
        session.add(
            ClinicConfigTable(
                tenant_id=tenant,
                api_credentials={
                    "whatsapp_provider": "zapi",
                    "zapi_instance_id": "3EMEDIA",
                    "zapi_token": "zt-media",
                    "zapi_client_token": "ct-media",
                },
            )
        )
        at = datetime.now(timezone.utc) - timedelta(minutes=1)
        session.add(
            CreditEnforcementState(
                id=1, credit_enforcement_at=at, set_by="test", set_at=at
            )
        )
    try:
        yield _Db(engine=engine, sessions=sessions, tenant=tenant, session=patient)
    finally:
        await engine.dispose()


async def _add(db: _Db, row: Any) -> Any:
    async with db.sessions() as session, session.begin():
        session.add(row)
    return row


def _reply(db: _Db, **extra: Any) -> dict[str, Any]:
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
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "session_pause": [False, None],
        **extra,
    }


async def _row(
    db: _Db, payload: Mapping[str, Any], *, state: str = "pending", age: float = 0
) -> UUID:
    row_id = uuid4()
    await _add(
        db,
        CreditResponseOutbox(
            id=row_id,
            tenant_id=db.tenant,
            effect_id=f"trace:{db.tenant}:{uuid4()}:decision:1",
            payload=dict(payload),
            state=state,
            attempts=0,
            created_at=datetime.now(timezone.utc) - timedelta(seconds=age),
        ),
    )
    return row_id


async def _get(db: _Db, row_id: UUID) -> CreditResponseOutbox:
    async with db.sessions() as session:
        row = await session.get(CreditResponseOutbox, row_id)
        assert row is not None
        return row


@dataclass
class _Media:
    """The provider's side of the file (bound by ``reply_media_channel``) and
    everything the step observed."""

    db: _Db
    trace: list[str]
    gate: asyncio.Event | None = None
    error: BaseException | None = None
    bound: list[tuple[str, dict[str, Any], str]] = field(default_factory=list)
    sends: list[dict[str, Any]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    row_states: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    watch: UUID | None = None

    def channel(
        self, provider: str, credentials: Mapping[str, Any], phone: str
    ) -> ReplyMediaChannel:
        self.bound.append((provider, dict(credentials), phone))
        return ReplyMediaChannel(provider, self.send_media, self.send_text)  # type: ignore[arg-type]

    async def send_media(
        self, payload: bytes, *, category: Any, mime_type: str, filename: str
    ) -> Mapping[str, object]:
        self.trace.append("send_media")
        if self.watch is not None:
            row = await _get(self.db, self.watch)
            self.row_states.append((row.state, dict(row.payload)))
        self.sends.append({"payload": payload, "filename": filename})
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return {"messageId": f"media-{len(self.sends)}"}

    async def send_text(self, text: str) -> Mapping[str, object]:
        self.notices.append(text)
        return {"messageId": f"notice-{len(self.notices)}"}


@dataclass
class _World:
    db: _Db
    media: _Media
    asset: MediaAsset
    pool: QueuedLimiter
    trace: list[str]
    texts: AsyncMock
    claims: list[tuple[UUID, UUID]]
    unread: AsyncMock


@pytest.fixture
async def world(db: _Db, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[_World]:
    trace: list[str] = []
    claims: list[tuple[UUID, UUID]] = []
    asset = await _add(db, _asset(db.tenant))
    media = _Media(db, trace)
    monkeypatch.setattr(CreditSwitch, "effective", lambda _self: True)
    monkeypatch.setattr(webhook, "_get_redis", lambda: None)
    texts = AsyncMock(return_value={"messageId": "reply-1"})
    monkeypatch.setattr(webhook, "send_text_message", texts)
    unread = AsyncMock()
    monkeypatch.setattr(webhook, "mark_conversation_unread", unread)
    monkeypatch.setattr(webhook, "reply_media_channel", media.channel)

    async def flag_on() -> bool:
        return True

    monkeypatch.setattr(egress_policy, "agent_media_send_enabled", flag_on)

    async def off(*_args: Any, **_kwargs: Any) -> Any:
        return CREDIT_OFF

    monkeypatch.setattr(credit_inventory, "read_credit_mode_without_request", off)

    async def claim(
        session: Any, *, tenant_id: UUID, session_id: UUID, media_id: UUID
    ) -> UUID | None:
        claims.append((session_id, media_id))
        trace.append("claim")
        existing = await session.scalar(
            select(MediaDelivery.id).where(
                MediaDelivery.session_id == session_id,
                MediaDelivery.media_id == media_id,
            )
        )
        if existing is not None:
            return None
        row_id = uuid4()
        session.add(
            MediaDelivery(
                id=row_id,
                tenant_id=tenant_id,
                session_id=session_id,
                media_id=media_id,
                last_sent_at=datetime.now(timezone.utc),
            )
        )
        return row_id

    monkeypatch.setattr(media_deliveries, "claim_media_delivery", claim)

    real_record = webhook.CreditReplySender.record

    async def record(self: Any, prepared: Any, sent: Any, session: Any) -> None:
        trace.append(f"record:{sent.get('messageId')}")
        await real_record(self, prepared, sent, session)

    monkeypatch.setattr(webhook.CreditReplySender, "record", record)
    real_mark = CreditRepository.mark_outbox_delivered

    async def mark(self: Any, *args: Any, **kwargs: Any) -> Any:
        trace.append("mark")
        return await real_mark(self, *args, **kwargs)

    monkeypatch.setattr(CreditRepository, "mark_outbox_delivered", mark)
    real_start = webhook.CreditReplySender.start_media

    def start(self: Any, *args: Any, **kwargs: Any) -> Any:
        trace.append("start_media")
        return real_start(self, *args, **kwargs)

    monkeypatch.setattr(webhook.CreditReplySender, "start_media", start)
    yield _World(db, media, asset, QueuedLimiter(1), trace, texts, claims, unread)
    if media.gate is not None:
        media.gate.set()
    await reply_media_runner.wait_idle()


def _publisher(world: _World) -> CreditResponsePublisher:
    return CreditResponsePublisher(
        webhook.CREDIT_REPLY_SENDER,
        session_factory=world.db.sessions,
        agent_sends=world.pool,
    )


async def _inline(world: _World, row: UUID) -> Any:
    return await asyncio.wait_for(
        deliver_reply_inline(
            row,
            sender=webhook.CREDIT_REPLY_SENDER,
            session_factory=world.db.sessions,
            agent_sends=world.pool,
        ),
        _WAIT,
    )


async def _stored_ids(db: _Db) -> list[str]:
    async with db.sessions() as session:
        row = await session.get(Session, db.session)
        assert row is not None
        return [
            entry[0] for entry in (row.session_metadata or {}).get("ai_msg_ids", [])
        ]


# ---------------------------------------------------------------------------
# The stored reply.
# ---------------------------------------------------------------------------


def _response(media_id: UUID | None) -> Any:
    return SimpleNamespace(
        prompt=SimpleNamespace(suggested_response=_TEXT, deterministic=False),
        response_language="pt-BR",
        handoff_summary=None,
        decision=None,
        attachment=None if media_id is None else SimpleNamespace(media_id=media_id),
    )


def test_the_stored_reply_carries_the_chosen_file_and_nothing_else() -> None:
    request = CanonicalEventRequest(
        tenant_id=uuid4(),
        session_id=uuid4(),
        event_type=EventType.MESSAGE_IN,
        payload=EventPayload(content="oi", channel="whatsapp", user_id=_PHONE),
    )
    target = webhook._ReplyTarget("zapi", _PHONE, False)
    without = webhook._credit_reply_payload(target, request, _response(None))
    assert without is not None and "media_id" not in without
    assert set(without) == {
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
    }
    media_id = uuid4()
    chosen = webhook._credit_reply_payload(target, request, _response(media_id))
    assert chosen == {**without, "media_id": str(media_id)}
    # A stand-in response without the field (the suites') chose none.
    bare = SimpleNamespace(
        **{k: v for k, v in vars(_response(None)).items() if k != "attachment"}
    )
    assert webhook._credit_reply_payload(target, request, bare) == without


# ---------------------------------------------------------------------------
# After the delivered mark, off the serial tick.
# ---------------------------------------------------------------------------


async def test_the_file_goes_after_the_text_is_recorded_and_marked_delivered(
    world: _World,
) -> None:
    row = await _row(world.db, _reply(world.db, media_id=str(world.asset.id)))
    world.media.watch = row

    delivery = await _inline(world, row)
    await reply_media_runner.wait_idle()

    assert delivery.outcome == "delivered"
    assert world.trace == [
        "record:reply-1",
        "mark",
        "start_media",
        "claim",
        "send_media",
        "record:media-1",
    ]
    # The row was already ``delivered``, its phone and text erased; the
    # file went to the in-memory phone, through the selection's provider.
    [(state, payload)] = world.media.row_states
    assert state == "delivered" and "phone" not in payload and "text" not in payload
    assert payload["media_id"] == str(world.asset.id)
    [(provider, credentials, phone)] = world.media.bound
    assert (provider, phone) == (webhook._CREDIT_MEDIA_PROVIDERS["zapi"], _PHONE)
    assert credentials["zapi_token"] == "zt-media"
    assert world.media.sends[0]["filename"] == "preparo.pdf"
    assert world.media.sends[0]["payload"] == _pdf()
    assert world.claims == [(world.db.session, world.asset.id)]
    # Its echo id is tracked and stored, like the text's.
    assert set(await _stored_ids(world.db)) >= {"reply-1", "media-1"}
    assert "media-1" in webhook._INMEM_SENT
    world.unread.assert_not_awaited()


async def test_a_text_the_provider_refused_starts_no_file(world: _World) -> None:
    """The credit twin of the inline paths' rule: no file unless the text's
    send returned.  The row turns ``failed`` (OD-1) and nothing is started,
    bound or claimed."""

    row = await _row(world.db, _reply(world.db, media_id=str(world.asset.id)))
    world.texts.side_effect = RuntimeError("upstream_http_error status=500")

    delivery = await _inline(world, row)
    await reply_media_runner.wait_idle()

    assert (delivery.outcome, delivery.reason) == ("failed", "send_failed")
    assert (await _get(world.db, row)).state == "failed"
    world.texts.assert_awaited_once()
    assert world.trace == []  # no record, no mark, no start_media
    assert world.media.bound == [] and world.claims == []
    assert world.media.sends == [] and world.media.notices == []


async def test_a_reply_without_a_file_starts_nothing(world: _World) -> None:
    row = await _row(world.db, _reply(world.db))
    assert (await _inline(world, row)).outcome == "delivered"
    await reply_media_runner.wait_idle()
    assert world.trace == ["record:reply-1", "mark"]
    assert world.media.bound == [] and world.claims == []


async def test_the_publisher_s_tick_never_waits_for_a_file(world: _World) -> None:
    second = await _add(world.db, _asset(world.db.tenant))
    aged = PUBLISHER_MIN_AGE_SECONDS + 5
    first_row = await _row(
        world.db, _reply(world.db, media_id=str(world.asset.id)), age=aged + 1
    )
    second_row = await _row(
        world.db, _reply(world.db, media_id=str(second.id)), age=aged
    )
    world.media.gate = asyncio.Event()  # every file send hangs until released

    tick = await asyncio.wait_for(_publisher(world).run_once(), _WAIT)

    assert tick is not None and tick.outcomes == ["delivered", "delivered"]
    assert (await _get(world.db, first_row)).state == "delivered"
    assert (await _get(world.db, second_row)).state == "delivered"
    # Both texts went while the first file is still in its provider call
    # and the second waits for the process's one permit.
    assert world.texts.await_count == 2
    for _ in range(200):
        if len(world.media.sends) == 1 and world.pool.waiting == 1:
            break
        await asyncio.sleep(0.01)
    assert len(world.media.sends) == 1 and world.pool.waiting == 1
    world.media.gate.set()
    await asyncio.wait_for(reply_media_runner.wait_idle(), _WAIT)
    assert [send["filename"] for send in world.media.sends] == ["preparo.pdf"] * 2


async def test_a_failed_file_never_changes_the_text_s_row(world: _World) -> None:
    row = await _row(world.db, _reply(world.db, media_id=str(world.asset.id)))
    world.media.error = RuntimeError("upstream_http_error status=500")

    assert (await _inline(world, row)).outcome == "delivered"
    marked = await _get(world.db, row)
    await reply_media_runner.wait_idle()

    after = await _get(world.db, row)
    assert (after.state, after.payload, after.attempts) == (
        marked.state,
        marked.payload,
        marked.attempts,
    )
    assert after.state == "delivered"
    assert world.media.notices == [reply_media.REPLY_MEDIA_NOTICES["pt"]]
    assert "notice-1" in await _stored_ids(world.db)


async def test_a_start_that_raises_is_logged_and_the_row_stays_delivered(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("cannot start")

    monkeypatch.setattr(webhook.CreditReplySender, "start_media", broken)
    alerts: list[dict[str, Any]] = []

    class Logs:
        def warning(self, _message: str, **fields: Any) -> None:
            alerts.append(fields)

        def __getattr__(self, _level: str) -> Any:
            return lambda *_a, **_k: None

    monkeypatch.setattr(credit_metering, "logger", Logs())
    row = await _row(world.db, _reply(world.db, media_id=str(world.asset.id)))

    assert (await _inline(world, row)).outcome == "delivered"
    assert (await _get(world.db, row)).state == "delivered"
    assert [
        a["reason"] for a in alerts if a.get("alert") == "credit_response_media"
    ] == ["media_not_started"]


async def test_a_recovered_row_resends_the_text_never_the_file(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inline send's mark is lost: the text and its file went, the row
    stays ``sending``.  Past the lease the publisher recovers it and resends
    the TEXT (the one accepted at-least-once case); the file is deduplicated
    by the 24-hour claim -- one file in all."""

    row = await _row(world.db, _reply(world.db, media_id=str(world.asset.id)))
    real_mark = CreditRepository.mark_outbox_delivered
    lost = [True]

    async def lose_once(self: Any, *args: Any, **kwargs: Any) -> Any:
        if lost:
            lost.clear()
            raise RuntimeError("the delivered mark was lost")
        return await real_mark(self, *args, **kwargs)

    monkeypatch.setattr(CreditRepository, "mark_outbox_delivered", lose_once)

    assert (await _inline(world, row)).outcome == "delivered"
    await reply_media_runner.wait_idle()
    assert (await _get(world.db, row)).state == "sending"
    assert len(world.media.sends) == 1  # the file went (G6 Q3: mark lost too)

    async with world.db.sessions() as session, session.begin():
        await session.execute(
            update(CreditResponseOutbox)
            .where(CreditResponseOutbox.id == row)
            .values(
                created_at=datetime.now(timezone.utc)
                - timedelta(seconds=SENDING_LEASE_SECONDS + 5)
            )
        )
    tick = await asyncio.wait_for(_publisher(world).run_once(), _WAIT)
    await reply_media_runner.wait_idle()

    assert tick is not None and tick.outcomes == ["delivered"]
    assert world.texts.await_count == 2  # the text, resent once
    assert len(world.media.sends) == 1  # the file, never resent
    assert world.claims == [(world.db.session, world.asset.id)] * 2
    assert world.media.notices == []  # deduplicated is silent


# ---------------------------------------------------------------------------
# The file's echo bookkeeping: CreditReplySender.record as RecordSentId.
# ---------------------------------------------------------------------------


def _prepared(db: _Db, *, handoff: bool = False) -> Any:
    return webhook._PreparedReply(
        channel="zapi",
        session_id=db.session,
        phone=_PHONE,
        text=_TEXT,
        language_code="pt-BR",
        input_was_audio=False,
        force_text=False,
        handoff=handoff,
        credentials={"zapi_instance_id": "3EMEDIA", "zapi_token": "zt-media"},
    )


async def test_the_recorder_stores_the_id_and_never_marks_unread(
    db: _Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(webhook, "_get_redis", lambda: None)
    unread = AsyncMock()
    monkeypatch.setattr(webhook, "mark_conversation_unread", unread)
    recorder = webhook.CREDIT_REPLY_SENDER.media_recorder(
        _prepared(db, handoff=True), reply_media_runner.cut_safe_sessions(db.sessions)
    )

    await recorder({"messageId": "media-77"})

    assert "media-77" in await _stored_ids(db)
    assert "media-77" in webhook._INMEM_SENT
    unread.assert_not_awaited()


async def test_the_recorder_never_swallows_the_record_bound_s_cancellation(
    db: _Db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The delivery bounds each record (``_record``, 30 s) by cancelling it;
    a record that swallowed that cancellation would hold the shielded wait
    for ever.  The cut connection is invalidated, never pooled."""

    monkeypatch.setattr(webhook, "_get_redis", lambda: None)
    monkeypatch.setattr(reply_media, "REPLY_MEDIA_RECORD_TIMEOUT_SECONDS", 0.1)
    real_store = webhook._store_ai_msg_id_to_db

    async def hung_store(session_id: UUID, mid: str, session: Any) -> None:
        await session.get(Session, session_id)  # a transaction is open
        await asyncio.Event().wait()
        await real_store(session_id, mid, session)

    monkeypatch.setattr(webhook, "_store_ai_msg_id_to_db", hung_store)
    invalidated: list[Any] = []
    sa_event.listen(
        db.engine.sync_engine.pool,
        "invalidate",
        lambda _dbapi, _record, exception: invalidated.append(exception),
    )
    recorder = webhook.CREDIT_REPLY_SENDER.media_recorder(
        _prepared(db), reply_media_runner.cut_safe_sessions(db.sessions)
    )

    await asyncio.wait_for(
        reply_media._record(recorder, {"messageId": "media-9"}, "media", {}), 5
    )

    assert len(invalidated) == 1
    assert db.engine.sync_engine.pool.checkedout() == 0
    assert "media-9" not in await _stored_ids(db)

    # The recorder itself re-raises the cancellation (never returns as if
    # it recorded): cancelled mid-record, its task ends cancelled.
    task = asyncio.create_task(recorder({"messageId": "media-10"}))
    for _ in range(200):
        if db.engine.sync_engine.pool.checkedout():
            break
        await asyncio.sleep(0.01)
    assert db.engine.sync_engine.pool.checkedout() == 1  # inside the record
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 5)
    assert task.cancelled()
    assert len(invalidated) == 2
    assert db.engine.sync_engine.pool.checkedout() == 0


# ---------------------------------------------------------------------------
# The pool, the lifespan and the budgets.
# ---------------------------------------------------------------------------


async def test_the_publisher_gets_the_pool_through_its_new_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[Any] = []

    class Recording(CreditResponsePublisher):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr(credit_metering, "CreditResponsePublisher", Recording)
    pool = QueuedLimiter(1)
    task = credit_metering.start_credit_response_publisher(
        webhook.CREDIT_REPLY_SENDER,
        SimpleNamespace(credit_hot_path_enabled=True),  # type: ignore[arg-type]
        agent_sends=pool,
    )
    assert task is not None
    await credit_metering.stop_credit_response_publisher(task)
    [publisher] = built
    assert publisher._agent_sends is pool


async def test_the_lifespan_hands_the_publisher_the_application_s_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.api import main as main_module
    from src.config.settings import settings

    built: list[Any] = []

    class Recording(CreditResponsePublisher):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr(credit_metering, "CreditResponsePublisher", Recording)
    monkeypatch.setattr(settings, "credit_hot_path_enabled", True)
    monkeypatch.setattr(settings, "credit_switch_refresh_seconds", 3600)
    app = main_module.app
    async with app.router.lifespan_context(app):
        pass
    [publisher] = built
    assert publisher._agent_sends is app.state.media_concurrency.agent_sends
    tree = ast.parse(inspect.getsource(main_module.lifespan))
    [start] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", "") == "start_credit_response_publisher"
    ]
    keywords = {k.arg: ast.unparse(k.value) for k in start.keywords}
    assert "media_concurrency" in keywords["agent_sends"]
    assert "agent_sends" in keywords["agent_sends"]
    shutdown = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and ast.unparse(node) == "reply_media_runner.shutdown"
    ]
    assert len(shutdown) == 1


def test_the_outbox_budgets_are_unchanged() -> None:
    assert REPLY_SEND_TIMEOUT_SECONDS == 240
    assert REPLY_RECORD_TIMEOUT_SECONDS == 30
    assert SENDING_LEASE_SECONDS == 330
    assert PUBLISHER_MIN_AGE_SECONDS == 300
    assert REPLY_MAX_AGE_SECONDS == 600


# ---------------------------------------------------------------------------
# The enforced webhook branch hands the same provider to both ends.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("channel", ["zapi", "meta"])
async def test_the_enforced_branch_gives_selection_and_delivery_one_provider(
    monkeypatch: pytest.MonkeyPatch, channel: str
) -> None:
    media_id = uuid4()
    pool = QueuedLimiter(1)
    provider = webhook._CREDIT_MEDIA_PROVIDERS[channel]
    media = webhook._InlineMedia(provider, pool, True)
    seen: dict[str, Any] = {}

    async def claim(*_args: Any) -> Any:
        return credit_metering.ClaimLease(uuid4(), uuid4(), datetime.now(timezone.utc))

    async def phase1(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(result=object(), clinic_config={}, message_history=[])

    async def final(*_args: Any, reply: Any, **_kwargs: Any) -> Any:
        seen["reply"] = reply
        return SimpleNamespace(interaction_event_id=None, outbox_id=uuid4())

    async def inline(_outbox: UUID, *, sender: Any, agent_sends: Any) -> Any:
        seen["agent_sends"] = agent_sends
        return credit_metering.InlineDelivery("delivered")

    async def nothing(*_args: Any) -> None:
        return None

    monkeypatch.setattr(credit_metering, "acquire_webhook_claim", claim)
    monkeypatch.setattr(credit_metering, "run_webhook_phase1", phase1)
    monkeypatch.setattr(credit_metering, "run_webhook_final", final)
    monkeypatch.setattr(credit_metering, "deliver_reply_inline", inline)
    monkeypatch.setattr(webhook, "_dispatch_phase1_handoffs", nothing)

    class Service:
        async def generate_response(self, *_args: Any, **kwargs: Any) -> Any:
            seen["provider"] = kwargs["media_delivery_provider"]
            return SimpleNamespace(response=_response(media_id))

    request = CanonicalEventRequest(
        tenant_id=uuid4(),
        session_id=uuid4(),
        event_type=EventType.MESSAGE_IN,
        payload=EventPayload(content="oi", channel="whatsapp", user_id=_PHONE),
    )
    body = await webhook._run_enforced_webhook_decision(
        service=Service(),
        event_request=request,
        db=AsyncMock(),
        context=SimpleNamespace(),  # type: ignore[arg-type]
        enforcement=SimpleNamespace(),  # type: ignore[arg-type]
        reply=webhook._ReplyTarget(channel, _PHONE, False),
        media=media,
    )

    assert body == {"ok": True, "replied": True}
    assert seen["provider"] == provider == channel
    assert seen["agent_sends"] is pool
    assert seen["reply"]["media_id"] == str(media_id)
    assert seen["reply"]["channel"] == channel
