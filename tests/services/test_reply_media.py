"""The agent's media delivery core (envio-midia-agente Group 5), SQLite + fakes.

Each test owns a SQLite database file (``create_all``) with a builder, its
clinic, a standalone clinic, a foreign clinic and a patient session.  The
24-hour claim is PostgreSQL-only, so here it is a fake that writes the row the
real one would (its proofs, and the whole delivery against the real claim,
run in ``tests/integration/test_reply_media_postgres.py``); the provider is a
fake channel.  Everything else is real: the asset re-reads, the credit mode
read without a ``Request``, the suspension gate, ``charge_egress`` and
``record_observational_egress``.  What is pinned here:

* the constants: ONE ``agent_sends`` permit per process in its own pool of
  ``MediaConcurrencyLimiters`` (the app creates it next to ``downloads``;
  exhausting either pool takes nothing from the other; only it is waited
  for), the closed vocabularies of the metric contract, the wait / send /
  notice / record bounds and the call's worst case,
  the category ceilings (the upload limits, never a copy) and the fixed
  notice in PT and EN;
* the nine steps in their order, observed on one trace;
* every refusal before the claim (``flag_off``, ``asset_gone``,
  ``too_large``, ``busy`` once the bounded wait for the permit ran out,
  ``credit_refused`` for an unavailable mode or a suspended builder) writes
  nothing and sends the notice; ``flag_off`` opens no session at all;
* a format the channel's provider does not send (OGG or M4A on Z-API,
  uazapi or Evolution) is ``asset_gone`` (``mime_not_sendable``) at the
  re-read: no permit, no claim, no charge, no sender call; Meta sends both;
* what the sender's pre-network check refuses on the re-read's 301-byte
  prefix (a stereo OGG on Meta, ``ogg_opus_not_mono``; no bytes at all,
  ``empty_media_payload``) is ``asset_gone`` there too, with the same
  nothing; a mono OGG whose first page has a 255-entry segment table is
  not refused; the re-read never selects the whole payload (Decision 9);
* a taken claim is ``deduplicated`` in silence; a failed claim never reaches
  the egress nor the sender; the claim commits before the egress and the
  send, and no pooled connection is checked out during the send;
* credit mode by mode: off records nothing (no credit SQL), observational
  records the egress with ``size_bytes``, enforced charges it BEFORE the
  sender with one server delivery id per send; ``credit_pending`` (a hold, a
  standalone clinic) refuses after the claim, which stays consumed;
* a failing or hung provider ends ``failed`` with the notice and gives the
  permit back (the deadline bounds the hang); a cancellation gives it back
  too; the media's and the notice's answers reach ``record_sent_id``, whose
  failure or hang changes nothing;
* the bounded wait for the process's only permit (owner, 2026-11-08): a
  different pair -- another patient, another file -- waits for the send in
  flight and then sends, with no notice; waiters are served in arrival
  order; a wait that runs out is ``busy`` with the notice; a waiter
  cancelled mid-wait leaves no permit behind and strands no other waiter;
  the same pair arriving while its first turn waits or sends is
  ``deduplicated`` in silence; a pair leaves the in-flight set however its
  turn ends (``busy`` after the wait and a cancellation while waiting
  included); a delivery stranded in the wait on a loop that closed, closed
  inside another delivery's in-flight critical section (where CPython 3.12
  can run a collection), deadlocks nothing and frees its pair;
* the provider bindings of :func:`reply_media_channel` and
  :func:`read_credit_mode_without_request`'s semantics.
"""

from __future__ import annotations

import asyncio
import threading
import weakref
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, get_args
from uuid import UUID, uuid4

import pytest
from prometheus_client import REGISTRY
from sqlalchemy import delete
from sqlalchemy import event as sa_event
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.models  # noqa: F401  (registers every mapper for create_all)
from src.api.middleware.auth import pwd_context
from src.api.middleware.media_limits import (
    AGENT_MEDIA_SENDS_PER_PROCESS,
    ImmediateLimiter,
    ImmediatePermit,
    MediaConcurrencyLimiters,
    QueuedLimiter,
)
from src.config.settings import settings
from src.models.base import Base
from src.models.billing import BillingTopupOrder
from src.models.builder import Builder
from src.models.content_retention import CreditLifecycleEpisode
from src.models.credit import (
    CreditAccount,
    CreditEnforcementState,
    CreditObservationalEffect,
)
from src.models.credit_inventory import CreditEgressDelivery
from src.models.media_asset import MediaAsset
from src.models.media_delivery import MediaDelivery
from src.models.session import Session
from src.models.tenant import Tenant
from src.observability.metric_dimensions import metric_dimension_contract
from src.repositories import media_deliveries
from src.repositories.credit import CreditRepository
from src.repositories.database import _patch_jsonb_for_sqlite, get_db_session
from src.services import (
    credit_inventory,
    credit_metering,
    egress_policy,
    media_assets,
    meta_service,
    reply_media,
    uazapi_service,
    zapi_service,
)
from src.services.credit_inventory import CREDIT_OFF, read_credit_mode_without_request
from src.services.credit_metering import (
    REPLY_RECORD_TIMEOUT_SECONDS,
    CreditEnforcementUnavailable,
    CreditTenantGone,
)
from src.services.credit_switch import CreditSwitch
from src.services.media_assets import media_payload_sha256
from src.services.media_send_body import MEDIA_SEND_TIMEOUT, require_sendable_media
from src.services.reply_media import (
    AGENT_MEDIA_PERMIT_WAIT_SECONDS,
    MEDIA_DELIVERY_PROVIDERS,
    NOTICE_OUTCOMES,
    REPLY_MEDIA_MAX_SECONDS,
    REPLY_MEDIA_NOTICE_TIMEOUT_SECONDS,
    REPLY_MEDIA_NOTICES,
    REPLY_MEDIA_RECORD_TIMEOUT_SECONDS,
    REPLY_MEDIA_SEND_TIMEOUT_SECONDS,
    MediaDeliveryOutcome,
    MediaDeliveryProvider,
    ReplyMediaChannel,
    ReplyMediaResult,
    deliver_reply_media,
    reply_media_channel,
    reply_media_notice,
)

_PHONE = "5511900004321"
_PT_NOTICE = (
    "Não consegui enviar o arquivo agora. A equipe da clínica pode te enviar por aqui."
)
_EN_NOTICE = (
    "I couldn't send the file right now. The clinic team can send it to you here."
)
_WAIT = 5.0


def _pdf(size: int = 1_000) -> bytes:
    return b"%PDF-1.7\n" + b"0" * (size - 9)


def _png(size: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * (size - 8)


def _ogg_opus(channels: int = 1, *, segments: int = 1) -> bytes:
    """One RFC 3533 page whose first packet is an RFC 7845 OpusHead.

    ``segments`` is the page's segment count: 255 puts the OpusHead at its
    farthest offset, 27 + 255 = 282, ending at byte 301.  Audio-like bytes
    follow, so the payload is longer than the re-read's prefix."""

    head = b"OpusHead" + bytes([1, channels]) + bytes(9)
    table = bytes([len(head)]) + bytes(segments - 1)
    page = b"OggS" + bytes([0, 2]) + bytes(20) + bytes([segments]) + table
    return page + head + b"\x4f" * 512


_AUDIO_PAYLOADS: dict[str, bytes] = {
    "audio/mpeg": b"ID3\x04" + bytes(64),
    "audio/ogg": _ogg_opus(),
    "audio/mp4": bytes(4) + b"ftypM4A " + bytes(52),
}


# ---------------------------------------------------------------------------
# The database.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Db:
    engine: Any
    sessions: async_sessionmaker[AsyncSession]
    builder: UUID
    tenant: UUID
    standalone: UUID
    foreign: UUID
    patient: UUID
    standalone_patient: UUID

    async def count(self, model: Any, *conditions: Any) -> int:
        async with self.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count()).select_from(model).where(*conditions)
                )
                or 0
            )

    async def add(self, *rows: Any) -> None:
        async with self.sessions() as session, session.begin():
            session.add_all(rows)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[_Db]:
    _patch_jsonb_for_sqlite()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'reply.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )
    world = _Db(engine, sessions, *(uuid4() for _ in range(6)))
    async with sessions() as session, session.begin():
        session.add(Builder(id=world.builder, name="Reply media builder"))
        await session.flush()
        for tenant_id, builder in (
            (world.tenant, world.builder),
            (world.standalone, None),
            (world.foreign, world.builder),
        ):
            session.add(
                Tenant(
                    id=tenant_id,
                    name=f"Clinic {tenant_id.hex[:6]}",
                    slug=f"clinic-{tenant_id.hex}",
                    api_key_hash=f"unused-{tenant_id.hex}",
                    builder_id=builder,
                    config={},
                )
            )
        await session.flush()
        for patient, tenant_id in (
            (world.patient, world.tenant),
            (world.standalone_patient, world.standalone),
        ):
            session.add(
                Session(
                    id=patient,
                    tenant_id=tenant_id,
                    channel="whatsapp",
                    cognitive_state={},
                    session_metadata={},
                )
            )
    try:
        yield world
    finally:
        await engine.dispose()


def _asset(
    tenant_id: UUID,
    *,
    payload: bytes | None = None,
    category: str = "pdf",
    mime_type: str = "application/pdf",
    auto_suggest: bool = True,
    trigger_intents: tuple[str, ...] = ("detalhes_servicos",),
    filename: str = "preparo.pdf",
) -> MediaAsset:
    body = _pdf() if payload is None else payload
    return MediaAsset(
        id=uuid4(),
        tenant_id=tenant_id,
        display_name="Preparo do exame",
        original_filename=filename,
        category=category,
        mime_type=mime_type,
        size_bytes=len(body),
        sha256=media_payload_sha256(body),
        payload=body,
        usage_trigger="quando perguntarem do preparo",
        auto_suggest=auto_suggest,
        trigger_intents=list(trigger_intents),
    )


async def _store(db: _Db, row: MediaAsset) -> UUID:
    await db.add(row)
    return row.id


async def _enforce(db: _Db) -> None:
    at = datetime.now(timezone.utc) - timedelta(days=90)
    await db.add(
        CreditEnforcementState(id=1, credit_enforcement_at=at, set_by="t", set_at=at)
    )


async def _hold(db: _Db) -> None:
    await db.add(
        BillingTopupOrder(
            id=uuid4(),
            builder_id=db.builder,
            payment_method="pix",
            catalog_version_id="payment-2026-10-03",
            offer_id="package_credits_500_v1",
            amount_cents=10_000,
            credit_grant_mc=500_000,
            pricing_snapshot={},
            financial_snapshot={},
            state="quarantined",
        )
    )


async def _suspend(db: _Db) -> None:
    """A builder 73 hours into its debt (past the 72-hour suspension)."""

    exhausted = datetime.now(timezone.utc) - timedelta(hours=73)
    account = uuid4()
    await db.add(CreditAccount(id=account, builder_id=db.builder))
    await db.add(
        CreditLifecycleEpisode(
            id=uuid4(),
            builder_id=db.builder,
            account_id=account,
            opened_effect_id=f"lifecycle:{db.builder}:episode:0:opened",
            exhausted_at=exhausted,
            suspension_due_at=exhausted + timedelta(hours=72),
            deletion_due_at=exhausted + timedelta(days=30),
        )
    )


# ---------------------------------------------------------------------------
# The fakes and the trace.
# ---------------------------------------------------------------------------


class _TracedPermit:
    def __init__(self, permit: ImmediatePermit, trace: list[str]) -> None:
        self._permit = permit
        self._trace = trace

    def release(self) -> None:
        self._trace.append("release")
        self._permit.release()


class _Pool:
    """``agent_sends`` (a real :class:`QueuedLimiter`), traced: ``permit``
    when step 4 starts to wait; ``waits`` keeps each bound it was given."""

    def __init__(self, trace: list[str], limiter: QueuedLimiter) -> None:
        self.limiter = limiter
        self._trace = trace
        self.waits: list[float] = []

    async def acquire(self, timeout: float) -> _TracedPermit | None:
        self._trace.append("permit")
        self.waits.append(timeout)
        permit = await self.limiter.acquire(timeout)
        return None if permit is None else _TracedPermit(permit, self._trace)


class _Factory:
    """The delivery's ``session_factory``: counts the sessions it opens."""

    def __init__(self, db: _Db) -> None:
        self._db = db
        self.opened = 0

    def __call__(self) -> AsyncSession:
        self.opened += 1
        return self._db.sessions()


@dataclass
class _Channel:
    trace: list[str]
    provider: MediaDeliveryProvider = "zapi"
    media_error: BaseException | None = None
    media_hang: bool = False
    text_error: BaseException | None = None
    during_send: Callable[[], Any] | None = None
    # Run the real senders' pre-network check (``require_sendable_media``).
    validate: bool = False
    media_calls: list[dict[str, Any]] = field(default_factory=list)
    text_calls: list[str] = field(default_factory=list)

    def bound(self) -> ReplyMediaChannel:
        return ReplyMediaChannel(self.provider, self.send_media, self.send_text)

    async def send_media(
        self, payload: bytes, *, category: Any, mime_type: str, filename: str
    ) -> Mapping[str, object]:
        self.trace.append("send")
        self.media_calls.append(
            {
                "payload": payload,
                "category": category,
                "mime_type": mime_type,
                "filename": filename,
            }
        )
        if self.validate:
            require_sendable_media(self.provider, category, mime_type, payload)
        if self.during_send is not None:
            await self.during_send()
        if self.media_hang:
            await asyncio.Event().wait()
        if self.media_error is not None:
            raise self.media_error
        return {"messageId": "media-message-id"}

    async def send_text(self, text: str) -> Mapping[str, object]:
        self.trace.append("notice")
        self.text_calls.append(text)
        if self.text_error is not None:
            raise self.text_error
        return {"messageId": "notice-message-id"}


@dataclass
class _Claim:
    """The PostgreSQL claim's stand-in: it writes the row the real one would
    in the delivery's transaction, or answers ``None`` / raises."""

    db: _Db
    trace: list[str]
    taken: bool = False
    error: BaseException | None = None
    row_id: UUID = field(default_factory=uuid4)
    sessions: list[AsyncSession] = field(default_factory=list)
    # The (session, media) pairs that reached the claim, in order.
    pairs: list[tuple[UUID, UUID]] = field(default_factory=list)

    async def __call__(
        self,
        session: AsyncSession,
        *,
        tenant_id: UUID,
        session_id: UUID,
        media_id: UUID,
    ) -> UUID | None:
        self.trace.append("claim")
        assert session.in_transaction()
        self.sessions.append(session)
        self.pairs.append((session_id, media_id))
        if self.error is not None:
            raise self.error
        if self.taken:
            return None
        await session.merge(
            MediaDelivery(
                id=self.row_id,
                tenant_id=tenant_id,
                session_id=session_id,
                media_id=media_id,
                last_sent_at=datetime.now(timezone.utc),
            )
        )
        await session.flush()
        return self.row_id


class _Logs:
    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict[str, Any]]] = []

    def __getattr__(self, level: str) -> Any:
        def log(message: str, **fields: Any) -> None:
            self.records.append((level, message, fields))

        return log


@dataclass
class _Harness:
    db: _Db
    trace: list[str]
    channel: _Channel
    claim: _Claim
    factory: _Factory
    pool: _Pool
    logs: _Logs
    flag: list[bool]
    records: list[Mapping[str, object]] = field(default_factory=list)
    record_error: BaseException | None = None
    record_hang: bool = False
    # The callback's own latency (Redis + the ai_msg_id write): its answer is
    # stored only once it has passed.
    record_delay: float = 0.0

    def reasons(self) -> list[str]:
        """The ``reason`` of every delivery's outcome log, in order."""

        return self.logged("reason")

    def logged(self, name: str) -> list[Any]:
        """The ``name`` field of every delivery's outcome log that has it,
        in order."""

        return [
            fields[name]
            for level, message, fields in self.logs.records
            if level == "info" and message == reply_media.DELIVERY_LOG
            if name in fields
        ]

    async def record(self, sent: Mapping[str, object]) -> None:
        self.trace.append("record")
        if self.record_delay:
            await asyncio.sleep(self.record_delay)
        self.records.append(sent)
        if self.record_hang:
            await asyncio.Event().wait()
        if self.record_error is not None:
            raise self.record_error

    async def deliver(
        self,
        media_id: UUID,
        *,
        tenant_id: UUID | None = None,
        session_id: UUID | None = None,
        language: str = "pt-BR",
        credit_switch_on: bool | None = None,
    ) -> ReplyMediaResult:
        self.factory.opened = 0  # the sessions of THIS delivery
        return await asyncio.wait_for(
            deliver_reply_media(
                channel=self.channel.bound(),
                media_id=media_id,
                tenant_id=tenant_id or self.db.tenant,
                session_id=session_id or self.db.patient,
                response_language=language,
                session_factory=self.factory,
                agent_sends=self.pool,
                record_sent_id=self.record,
                credit_switch_on=credit_switch_on,
            ),
            _WAIT,
        )


@pytest.fixture(autouse=True)
def _fresh_process_state() -> Iterator[None]:
    credit_inventory.forget_enforcement_seen()
    credit_metering.forget_enforcement_seen()
    yield
    credit_inventory.forget_enforcement_seen()
    credit_metering.forget_enforcement_seen()


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    def force(effective: bool) -> None:
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: effective)

    force(False)
    return force


@pytest.fixture
def harness(
    db: _Db, monkeypatch: pytest.MonkeyPatch, switch: Callable[[bool], None]
) -> _Harness:
    trace: list[str] = []
    flag = [True]
    factory = _Factory(db)

    async def flag_read() -> bool:
        trace.append("flag")
        # Read before any session (no transaction open, no media SQL).
        assert factory.opened == 0
        return flag[0]

    claim = _Claim(db, trace)
    logs = _Logs()
    monkeypatch.setattr(egress_policy, "agent_media_send_enabled", flag_read)
    monkeypatch.setattr(media_deliveries, "claim_media_delivery", claim)
    monkeypatch.setattr(reply_media, "logger", logs)
    # The credit steps, traced (the real functions run underneath).
    for name, label in (
        ("read_credit_mode_without_request", "credit_mode"),
        ("charge_egress", "charge"),
        ("record_observational_egress", "observe"),
    ):
        original = getattr(credit_inventory, name)

        def traced(original: Any, label: str) -> Any:
            async def run(*args: Any, **kwargs: Any) -> Any:
                trace.append(label)
                return await original(*args, **kwargs)

            return run

        monkeypatch.setattr(credit_inventory, name, traced(original, label))

    def statement(_conn: Any, _cursor: Any, sql: str, *_args: Any) -> None:
        lowered = sql.lower()
        if lowered.lstrip().startswith("select") and "from media_assets" in lowered:
            if "media_assets.auto_suggest" in lowered:
                # Step 2 reads the payload only as its bounded prefix: a
                # whole payload there (Decision 9) breaks every trace.
                bare = lowered.replace("substring(media_assets.payload,", "")
                whole = "media_assets.payload" in bare
                trace.append("asset_with_payload" if whole else "asset")
            elif "media_assets.payload" in lowered:
                trace.append("payload")

    sa_event.listen(db.engine.sync_engine, "before_cursor_execute", statement)
    return _Harness(
        db=db,
        trace=trace,
        channel=_Channel(trace),
        claim=claim,
        factory=factory,
        pool=_Pool(trace, QueuedLimiter(AGENT_MEDIA_SENDS_PER_PROCESS)),
        logs=logs,
        flag=flag,
    )


def _sample(provider: str, outcome: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "neurostack_agent_media_delivery_total",
            {"provider": provider, "outcome": outcome},
        )
        or 0.0
    )


async def _nothing_written(db: _Db) -> None:
    assert await db.count(MediaDelivery) == 0
    assert await db.count(CreditEgressDelivery) == 0
    assert await db.count(CreditObservationalEffect) == 0


# ---------------------------------------------------------------------------
# Constants and contracts.
# ---------------------------------------------------------------------------


def test_agent_sends_is_one_permit_per_process_in_its_own_pool() -> None:
    assert AGENT_MEDIA_SENDS_PER_PROCESS == 1
    limiters = MediaConcurrencyLimiters(uploads=4, downloads=8)
    assert limiters.agent_sends.limit == 1
    pools = (limiters.uploads, limiters.downloads, limiters.agent_sends)
    assert len({id(pool) for pool in pools}) == 3
    # Only the agent's pool is waited for; the panel's never queue.
    assert type(limiters.agent_sends) is QueuedLimiter
    assert type(limiters.uploads) is type(limiters.downloads) is ImmediateLimiter


def test_the_app_creates_the_agent_sends_pool_next_to_downloads(
    test_app: Any,
) -> None:
    limiters = test_app.state.media_concurrency
    assert isinstance(limiters, MediaConcurrencyLimiters)
    assert limiters.agent_sends.limit == AGENT_MEDIA_SENDS_PER_PROCESS == 1
    assert limiters.downloads.limit == settings.media_max_concurrent_downloads
    assert limiters.uploads.limit == settings.media_max_concurrent_uploads
    assert limiters.agent_sends is not limiters.downloads


def test_exhausting_one_pool_takes_no_permit_of_the_other() -> None:
    limiters = MediaConcurrencyLimiters(uploads=4, downloads=8)
    agent = limiters.agent_sends.try_acquire()
    assert agent is not None and limiters.agent_sends.try_acquire() is None
    downloads = [limiters.downloads.try_acquire() for _ in range(8)]
    assert all(permit is not None for permit in downloads)
    assert limiters.downloads.try_acquire() is None
    agent.release()
    # Every download permit taken: the agent's pool still has its own.
    again = limiters.agent_sends.try_acquire()
    assert again is not None
    assert (limiters.downloads.active, limiters.agent_sends.active) == (8, 1)
    again.release()
    for permit in downloads:
        assert permit is not None
        permit.release()


def test_a_panel_download_is_answered_while_the_agent_send_permit_is_taken(
    client: Any,
) -> None:
    key, tenant_id = "reply-media-download-key", uuid4()
    asset = _asset(tenant_id)

    async def seed() -> None:
        session = get_db_session()
        try:
            async with session.begin():
                session.add(
                    Tenant(
                        id=tenant_id,
                        name="Download clinic",
                        slug=f"download-{tenant_id.hex}",
                        api_key_hash=pwd_context.hash(key),
                        config={},
                    )
                )
                await session.flush()
                session.add(asset)
        finally:
            await session.close()

    client._submit(seed())
    limiters = client.application.state.media_concurrency
    permit = limiters.agent_sends.try_acquire()
    assert permit is not None
    try:
        response = client.get(
            f"/v1/media-assets/{asset.id}/content", headers={"X-API-Key": key}
        )
        assert response.status_code == 200
        assert response.content == _pdf()
        assert (limiters.downloads.active, limiters.agent_sends.active) == (0, 1)
    finally:
        permit.release()


def test_the_vocabularies_are_the_metric_contract() -> None:
    contract = metric_dimension_contract("neurostack_agent_media_delivery")
    assert frozenset(get_args(MediaDeliveryOutcome)) == (
        contract.policies["outcome"].vocabulary
    )
    assert (
        frozenset(get_args(MediaDeliveryProvider))
        == MEDIA_DELIVERY_PROVIDERS
        == contract.policies["provider"].vocabulary
    )
    assert NOTICE_OUTCOMES == {
        "failed",
        "asset_gone",
        "too_large",
        "busy",
        "credit_refused",
        "flag_off",
    }


def test_the_bounds() -> None:
    # The senders' own healthy worst case, one connect + write + read per
    # MEDIA_SEND_TIMEOUT request (G5 review LOW-1): Meta uploads, then sends.
    request = MEDIA_SEND_TIMEOUT.connect + MEDIA_SEND_TIMEOUT.write
    request += MEDIA_SEND_TIMEOUT.read
    assert request == 125.0
    assert dict(REPLY_MEDIA_SEND_TIMEOUT_SECONDS) == {
        "meta": 2 * request,
        "zapi": request,
        "uazapi": request,
        "evolution": request,
    }
    assert set(REPLY_MEDIA_SEND_TIMEOUT_SECONDS) == MEDIA_DELIVERY_PROVIDERS
    assert REPLY_MEDIA_NOTICE_TIMEOUT_SECONDS == 35.0
    assert REPLY_MEDIA_RECORD_TIMEOUT_SECONDS == REPLY_RECORD_TIMEOUT_SECONDS
    # Step 4's bounded wait for the permit (owner, 2026-11-08).
    assert AGENT_MEDIA_PERMIT_WAIT_SECONDS == 60
    # The worst case the module docstring states, exported for Group 6: the
    # wait, the send's deadline, the notice and its record.
    assert dict(REPLY_MEDIA_MAX_SECONDS) == {
        provider: AGENT_MEDIA_PERMIT_WAIT_SECONDS
        + REPLY_MEDIA_SEND_TIMEOUT_SECONDS[provider]
        + REPLY_MEDIA_NOTICE_TIMEOUT_SECONDS
        + REPLY_MEDIA_RECORD_TIMEOUT_SECONDS
        for provider in MEDIA_DELIVERY_PROVIDERS
    }
    assert dict(REPLY_MEDIA_MAX_SECONDS) == {
        "meta": 375.0,
        "zapi": 250.0,
        "uazapi": 250.0,
        "evolution": 250.0,
    }
    assert "60 + 250 + 35 + 30 = 375 s" in (reply_media.__doc__ or "")
    assert "60 + 125 + 35 + 30 = 250 s" in (reply_media.__doc__ or "")


def test_the_ceilings_are_the_upload_limits_not_a_copy() -> None:
    assert reply_media.MEDIA_CATEGORY_MAX_BYTES is media_assets.MEDIA_CATEGORY_MAX_BYTES
    assert dict(reply_media.MEDIA_CATEGORY_MAX_BYTES) == {
        "image": 5_000_000,
        "pdf": 20_000_000,
        "audio": 16_000_000,
        "video": 16_000_000,
    }


@pytest.mark.parametrize(
    ("language", "expected"),
    [
        ("pt-BR", _PT_NOTICE),
        ("pt", _PT_NOTICE),
        ("es", _PT_NOTICE),
        ("", _PT_NOTICE),
        (None, _PT_NOTICE),
        ("en", _EN_NOTICE),
        ("en-US", _EN_NOTICE),
        (" EN ", _EN_NOTICE),
    ],
)
def test_the_notice_is_fixed_in_pt_and_en(language: str | None, expected: str) -> None:
    assert reply_media_notice(language) == expected
    assert set(REPLY_MEDIA_NOTICES.values()) == {_PT_NOTICE, _EN_NOTICE}


# ---------------------------------------------------------------------------
# The nine steps.
# ---------------------------------------------------------------------------


async def test_the_nine_steps_run_in_order(
    harness: _Harness, switch: Callable[[bool], None]
) -> None:
    switch(True)
    await _enforce(harness.db)
    media_id = await _store(harness.db, _asset(harness.db.tenant))

    result = await harness.deliver(media_id)

    assert result == ReplyMediaResult("sent", notice_sent=False)
    assert harness.reasons() == ["sent"]
    assert harness.trace == [
        "flag",  # 1
        "asset",  # 2 (3, the ceiling, is pure)
        "permit",  # 4
        "credit_mode",  # 5
        "claim",  # 6
        "charge",  # 7
        "payload",  # 8: the bytes, under the permit
        "send",  # 8
        "release",
        "record",  # 9
    ]
    assert harness.records == [{"messageId": "media-message-id"}]
    assert harness.pool.limiter.active == 0
    # Step 4 waits, bounded: the free permit was handed over at once.
    assert harness.pool.waits == [AGENT_MEDIA_PERMIT_WAIT_SECONDS]


async def test_the_sender_gets_the_reread_asset(
    harness: _Harness,
) -> None:
    payload = _png(2_000)
    media_id = await _store(
        harness.db,
        _asset(
            harness.db.tenant,
            payload=payload,
            category="image",
            mime_type="image/png",
            filename="fachada.png",
        ),
    )

    assert (await harness.deliver(media_id)).outcome == "sent"
    assert harness.channel.media_calls == [
        {
            "payload": payload,
            "category": "image",
            "mime_type": "image/png",
            "filename": "fachada.png",
        }
    ]


# ---------------------------------------------------------------------------
# Refusals before the claim write nothing and send the notice.
# ---------------------------------------------------------------------------


async def test_flag_off_opens_no_session_and_sends_the_notice(
    harness: _Harness,
) -> None:
    media_id = await _store(harness.db, _asset(harness.db.tenant))
    harness.flag[0] = False
    before = _sample("zapi", "flag_off")

    result = await harness.deliver(media_id)

    assert result == ReplyMediaResult("flag_off", notice_sent=True)
    assert harness.reasons() == ["flag_off"]
    assert harness.trace == ["flag", "notice", "record"]
    assert harness.factory.opened == 0
    assert harness.channel.text_calls == [_PT_NOTICE]
    assert harness.records == [{"messageId": "notice-message-id"}]
    assert _sample("zapi", "flag_off") == before + 1
    await _nothing_written(harness.db)


async def test_an_unreadable_flag_is_off(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def raising() -> bool:
        raise RuntimeError("reader broken")

    monkeypatch.setattr(egress_policy, "agent_media_send_enabled", raising)

    result = await harness.deliver(uuid4())

    assert result == ReplyMediaResult("flag_off", notice_sent=True)
    assert harness.reasons() == ["flag_unreadable:RuntimeError"]
    assert harness.factory.opened == 0


@pytest.mark.parametrize("case", ["missing", "foreign_tenant", "auto_suggest_off"])
async def test_an_asset_gone_or_no_longer_automatic_is_refused_before_the_permit(
    harness: _Harness, case: str
) -> None:
    db = harness.db
    if case == "missing":
        media_id = uuid4()
    elif case == "foreign_tenant":
        media_id = await _store(db, _asset(db.foreign))
    else:
        media_id = await _store(db, _asset(db.tenant, auto_suggest=False))

    result = await harness.deliver(media_id, language="en")

    assert result == ReplyMediaResult("asset_gone", notice_sent=True)
    assert harness.reasons() == ["asset_gone"]
    assert harness.trace == ["flag", "asset", "notice", "record"]
    assert harness.channel.text_calls == [_EN_NOTICE]
    await _nothing_written(db)


async def test_an_automatic_asset_bound_to_no_subject_is_still_sent(
    harness: _Harness,
) -> None:
    # "Outras" is implied: a greeting or FAQ turn may pick a file bound to no
    # subject, so the re-read refuses only a file no longer automatic.
    db = harness.db
    media_id = await _store(db, _asset(db.tenant, trigger_intents=()))

    assert (await harness.deliver(media_id)).outcome == "sent"
    assert harness.reasons() == ["sent"]


async def test_the_category_ceiling_is_inclusive_and_refuses_one_byte_more(
    harness: _Harness,
) -> None:
    db = harness.db
    at_ceiling = await _store(
        db,
        _asset(
            db.tenant, payload=_png(5_000_000), category="image", mime_type="image/png"
        ),
    )
    above = await _store(
        db,
        _asset(
            db.tenant, payload=_png(5_000_001), category="image", mime_type="image/png"
        ),
    )

    refused = await harness.deliver(above)
    assert refused == ReplyMediaResult("too_large", notice_sent=True)
    assert harness.reasons() == ["too_large"]
    assert harness.trace == ["flag", "asset", "notice", "record"]
    await _nothing_written(db)

    assert (await harness.deliver(at_ceiling)).outcome == "sent"


# Which audio each provider's sender sends, written out here (never read
# from ``sendable_mime_types``).
_AUDIO_SENT: dict[str, frozenset[str]] = {
    "meta": frozenset({"audio/mpeg", "audio/ogg", "audio/mp4"}),
    "zapi": frozenset({"audio/mpeg"}),
    "uazapi": frozenset({"audio/mpeg"}),
    "evolution": frozenset({"audio/mpeg"}),
}


@pytest.mark.parametrize("mime_type", sorted(_AUDIO_PAYLOADS))
@pytest.mark.parametrize("provider", sorted(_AUDIO_SENT))
async def test_a_format_the_provider_does_not_send_is_refused_at_the_reread(
    harness: _Harness,
    switch: Callable[[bool], None],
    provider: MediaDeliveryProvider,
    mime_type: str,
) -> None:
    """Enforced, so a refusal after the claim would leave a claim AND a
    charge: a format outside the provider's set leaves neither, nor a
    permit nor a sender call; one inside it goes through."""

    switch(True)
    db = harness.db
    await _enforce(db)
    harness.channel.provider = provider
    media_id = await _store(
        db,
        _asset(
            db.tenant,
            payload=_AUDIO_PAYLOADS[mime_type],
            category="audio",
            mime_type=mime_type,
            filename="audio",
        ),
    )
    before = _sample(provider, "asset_gone")

    result = await harness.deliver(media_id)

    if mime_type in _AUDIO_SENT[provider]:
        assert result == ReplyMediaResult("sent", notice_sent=False)
        assert harness.reasons() == ["sent"]
        assert [call["mime_type"] for call in harness.channel.media_calls] == [
            mime_type
        ]
        assert await db.count(MediaDelivery) == 1
        assert await db.count(CreditEgressDelivery) == 1
        return
    assert result == ReplyMediaResult("asset_gone", notice_sent=True)
    assert harness.reasons() == ["mime_not_sendable"]
    assert harness.trace == ["flag", "asset", "notice", "record"]
    assert harness.channel.media_calls == []
    assert harness.channel.text_calls == [_PT_NOTICE]
    assert harness.pool.limiter.active == 0
    assert _sample(provider, "asset_gone") == before + 1
    await _nothing_written(db)


async def test_a_sendable_ogg_on_meta_runs_the_nine_steps(
    harness: _Harness, switch: Callable[[bool], None]
) -> None:
    switch(True)
    db = harness.db
    await _enforce(db)
    harness.channel.provider = "meta"
    payload = _ogg_opus()
    media_id = await _store(
        db,
        _asset(
            db.tenant,
            payload=payload,
            category="audio",
            mime_type="audio/ogg",
            filename="explicacao.ogg",
        ),
    )

    result = await harness.deliver(media_id)

    assert result == ReplyMediaResult("sent", notice_sent=False)
    assert harness.trace == [
        "flag",
        "asset",
        "permit",
        "credit_mode",
        "claim",
        "charge",
        "payload",
        "send",
        "release",
        "record",
    ]
    assert harness.channel.media_calls == [
        {
            "payload": payload,
            "category": "audio",
            "mime_type": "audio/ogg",
            "filename": "explicacao.ogg",
        }
    ]


async def test_a_mono_ogg_whose_first_page_has_a_long_segment_table_is_sent(
    harness: _Harness, switch: Callable[[bool], None]
) -> None:
    """The OpusHead at its farthest offset (byte 282, ending at 301): the
    re-read's prefix holds it whole, so a mono file is never refused there.
    A prefix of 292 bytes (the old 10-byte locator's figure) would cut it."""

    switch(True)
    db = harness.db
    await _enforce(db)
    harness.channel.provider = "meta"
    harness.channel.validate = True
    payload = _ogg_opus(segments=255)
    assert len(payload) > reply_media.REREAD_PREFIX_BYTES == 301
    media_id = await _store(
        db,
        _asset(
            db.tenant,
            payload=payload,
            category="audio",
            mime_type="audio/ogg",
            filename="longa.ogg",
        ),
    )

    result = await harness.deliver(media_id)

    assert result == ReplyMediaResult("sent", notice_sent=False)
    assert harness.reasons() == ["sent"]
    assert harness.trace[:2] == ["flag", "asset"]
    assert [call["payload"] for call in harness.channel.media_calls] == [payload]
    assert await db.count(MediaDelivery) == 1
    assert await db.count(CreditEgressDelivery) == 1


@pytest.mark.parametrize("segments", [1, 255])
async def test_a_stereo_ogg_on_meta_is_refused_at_the_reread_before_the_claim(
    harness: _Harness, switch: Callable[[bool], None], segments: int
) -> None:
    """Lacuna R-2: the bridge image (``249a55c6``) stores stereo OGG, so a
    file uploaded between window 045 and the behaviour release can be one.
    Enforced, so a refusal after the claim would leave a claim AND a charge:
    the re-read's prefix refuses it first -- no permit, no claim, no charge,
    no sender call, and the notice."""

    switch(True)
    db = harness.db
    await _enforce(db)
    harness.channel.provider = "meta"
    harness.channel.validate = True
    media_id = await _store(
        db,
        _asset(
            db.tenant,
            payload=_ogg_opus(channels=2, segments=segments),
            category="audio",
            mime_type="audio/ogg",
            filename="estereo.ogg",
        ),
    )
    before = _sample("meta", "asset_gone")

    result = await harness.deliver(media_id)

    assert result == ReplyMediaResult("asset_gone", notice_sent=True)
    assert harness.reasons() == ["ogg_opus_not_mono"]
    assert harness.trace == ["flag", "asset", "notice", "record"]
    assert harness.channel.media_calls == []
    assert harness.channel.text_calls == [_PT_NOTICE]
    assert harness.pool.limiter.active == 0
    assert _sample("meta", "asset_gone") == before + 1
    await _nothing_written(db)


async def test_an_empty_stored_payload_is_refused_at_the_reread(
    harness: _Harness, switch: Callable[[bool], None]
) -> None:
    """Unreachable through the schema (``size_bytes > 0`` and
    ``length(payload) = size_bytes``); with the CHECKs bypassed, an empty
    prefix is refused before the claim as the sender would refuse it:
    ``asset_gone`` / ``empty_media_payload``."""

    switch(True)
    db = harness.db
    await _enforce(db)
    row = _asset(db.tenant)
    row.payload, row.size_bytes = b"", 0
    async with db.sessions() as session, session.begin():
        await session.execute(text("PRAGMA ignore_check_constraints = ON"))
        session.add(row)
        await session.flush()
        await session.execute(text("PRAGMA ignore_check_constraints = OFF"))

    result = await harness.deliver(row.id)

    assert result == ReplyMediaResult("asset_gone", notice_sent=True)
    assert harness.reasons() == ["empty_media_payload"]
    assert harness.trace == ["flag", "asset", "notice", "record"]
    assert harness.channel.media_calls == []
    await _nothing_written(db)


async def test_an_ogg_rewritten_stereo_after_the_reread_is_refused_by_the_meta_sender(
    harness: _Harness,
    switch: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth, pinned as it is: a row rewritten between the re-read
    and step 8 (a direct write; the upload refuses stereo) passes the
    re-read and still never reaches Meta.  The sender's own pre-network
    check (``require_sendable_media``, as ``send_meta_cloud_media`` runs it)
    refuses the bytes step 8 read: ``failed``, notice sent, window consumed,
    the observation kept."""

    switch(True)
    db = harness.db
    harness.channel.provider = "meta"
    harness.channel.validate = True
    mono, stereo = _ogg_opus(), _ogg_opus(channels=2)
    media_id = await _store(
        db,
        _asset(
            db.tenant,
            payload=mono,
            category="audio",
            mime_type="audio/ogg",
            filename="estereo.ogg",
        ),
    )
    observe = credit_inventory.record_observational_egress

    async def rewrite_then_observe(*args: Any, **kwargs: Any) -> Any:
        async with db.sessions() as session, session.begin():
            await session.execute(
                update(MediaAsset)
                .where(MediaAsset.id == media_id)
                .values(payload=stereo, sha256=media_payload_sha256(stereo))
            )
        return await observe(*args, **kwargs)

    monkeypatch.setattr(
        credit_inventory, "record_observational_egress", rewrite_then_observe
    )

    result = await harness.deliver(media_id)

    assert result == ReplyMediaResult("failed", notice_sent=True)
    assert harness.reasons() == ["send_failed:ValueError"]
    assert harness.trace[:7] == [
        "flag",
        "asset",
        "permit",
        "credit_mode",
        "claim",
        "observe",
        "payload",
    ]
    assert [call["payload"] for call in harness.channel.media_calls] == [stereo]
    assert await db.count(MediaDelivery) == 1
    assert await db.count(CreditObservationalEffect) > 0


async def test_no_permit_handed_over_within_the_wait_is_busy_with_the_notice(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wait for the permit is bounded (shortened here): a permit held
    past the bound makes the turn ``busy`` with the notice; it then holds
    no permit, has left the queue and wrote nothing."""

    _short_wait(monkeypatch)
    media_id = await _store(harness.db, _asset(harness.db.tenant))
    held = harness.pool.limiter.try_acquire()
    assert held is not None
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        result = await harness.deliver(media_id)
        waited = loop.time() - started
        # Only the held permit is taken, and nobody is left queued.
        assert (harness.pool.limiter.active, harness.pool.limiter.waiting) == (1, 0)
    finally:
        held.release()

    assert result == ReplyMediaResult("busy", notice_sent=True)
    assert harness.reasons() == ["busy"]
    assert harness.trace == ["flag", "asset", "permit", "notice", "record"]
    assert harness.pool.waits == [_SHORT_WAIT]
    assert waited >= _SHORT_WAIT * 0.9
    [logged_wait] = harness.logged("permit_wait_seconds")
    assert logged_wait >= _SHORT_WAIT * 0.9
    assert harness.channel.text_calls == [_PT_NOTICE]
    assert harness.pool.limiter.active == 0
    await _nothing_written(harness.db)


async def test_an_unavailable_credit_mode_is_refused_before_the_claim(
    harness: _Harness,
    switch: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switch(True)
    media_id = await _store(harness.db, _asset(harness.db.tenant))
    # The enforcement was seen in this process and can no longer be read (D1).
    monkeypatch.setattr(credit_inventory, "_enforcement_seen", True)

    async def unreadable(_self: CreditRepository) -> datetime:
        raise OperationalError("SELECT", {}, Exception("database down"))

    monkeypatch.setattr(CreditRepository, "enforcement_at", unreadable)

    result = await harness.deliver(media_id)

    assert result == ReplyMediaResult("credit_refused", notice_sent=True)
    assert harness.reasons() == ["credit_enforcement_unavailable"]
    assert harness.trace == [
        "flag",
        "asset",
        "permit",
        "credit_mode",
        "release",
        "notice",
        "record",
    ]
    assert harness.pool.limiter.active == 0
    await _nothing_written(harness.db)


async def test_a_suspended_builder_is_refused_before_the_claim(
    harness: _Harness, switch: Callable[[bool], None]
) -> None:
    switch(True)
    await _enforce(harness.db)
    await _suspend(harness.db)
    media_id = await _store(harness.db, _asset(harness.db.tenant))

    result = await harness.deliver(media_id)

    assert result == ReplyMediaResult("credit_refused", notice_sent=True)
    assert harness.reasons() == ["tenant_suspended"]
    assert "claim" not in harness.trace and "send" not in harness.trace
    assert harness.trace[-3:] == ["release", "notice", "record"]
    await _nothing_written(harness.db)


@pytest.mark.parametrize("where", ["open", "close"])
async def test_a_credit_mode_session_that_fails_to_open_or_close_is_refused(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """Step 5's session is inside the step's ``try``: the delivery never
    raises (G5 review LOW-4)."""

    media_id = await _store(harness.db, _asset(harness.db.tenant))
    factory = harness.factory

    class _Broken:
        def __init__(self, session: AsyncSession) -> None:
            self._session = session

        async def __aenter__(self) -> AsyncSession:
            if where == "open":
                raise ConnectionError("pool exhausted")
            return await self._session.__aenter__()

        async def __aexit__(self, *exc: Any) -> None:
            await self._session.__aexit__(*exc)
            if where == "close":
                raise ConnectionError("connection reset")

    def opening() -> Any:
        factory.opened += 1
        session = harness.db.sessions()
        return _Broken(session) if factory.opened == 2 else session

    monkeypatch.setattr(harness, "factory", opening)

    result = await harness.deliver(media_id)

    assert result == ReplyMediaResult("credit_refused", notice_sent=True)
    assert harness.reasons() == ["credit_mode_failed:ConnectionError"]
    assert "claim" not in harness.trace and "send" not in harness.trace
    assert harness.pool.limiter.active == 0
    await _nothing_written(harness.db)


async def test_an_unreadable_mode_before_any_enforcement_stays_observational(
    harness: _Harness,
    switch: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """read_credit_mode's rule: a failed read never refuses before the
    enforcement was seen (main's answer)."""

    switch(True)
    media_id = await _store(harness.db, _asset(harness.db.tenant))
    original = CreditRepository.enforcement_at
    calls: list[int] = []

    async def first_read_fails(self: CreditRepository) -> datetime | None:
        calls.append(1)
        if len(calls) == 1:
            raise OperationalError("SELECT", {}, Exception("blip"))
        return await original(self)

    monkeypatch.setattr(CreditRepository, "enforcement_at", first_read_fails)

    assert (await harness.deliver(media_id)).outcome == "sent"
    assert "observe" in harness.trace and "charge" not in harness.trace


# ---------------------------------------------------------------------------
# The claim.
# ---------------------------------------------------------------------------


async def test_a_taken_claim_is_deduplicated_in_silence(
    harness: _Harness, switch: Callable[[bool], None]
) -> None:
    switch(True)
    await _enforce(harness.db)
    media_id = await _store(harness.db, _asset(harness.db.tenant))
    harness.claim.taken = True
    before = _sample("zapi", "deduplicated")

    result = await harness.deliver(media_id)

    assert result == ReplyMediaResult("deduplicated", notice_sent=False)
    assert harness.reasons() == ["deduplicated"]
    assert harness.trace == [
        "flag",
        "asset",
        "permit",
        "credit_mode",
        "claim",
        "release",
    ]
    assert harness.channel.text_calls == [] and harness.records == []
    assert not harness.claim.sessions[0].in_transaction()
    assert _sample("zapi", "deduplicated") == before + 1
    await _nothing_written(harness.db)


@pytest.mark.parametrize(
    "error",
    [
        OperationalError("INSERT", {}, Exception("database down")),
        media_deliveries.MediaDeliveryClaimUnsupported("sqlite"),
    ],
)
async def test_a_failed_claim_never_reaches_the_egress_nor_the_sender(
    harness: _Harness, switch: Callable[[bool], None], error: Exception
) -> None:
    switch(True)
    await _enforce(harness.db)
    media_id = await _store(harness.db, _asset(harness.db.tenant))
    harness.claim.error = error

    result = await harness.deliver(media_id)

    assert result == ReplyMediaResult("failed", notice_sent=True)
    assert harness.reasons() == [f"claim_failed:{type(error).__name__}"]
    assert harness.trace == [
        "flag",
        "asset",
        "permit",
        "credit_mode",
        "claim",
        "release",
        "notice",
        "record",
    ]
    assert harness.channel.media_calls == []
    assert harness.records == [{"messageId": "notice-message-id"}]
    await _nothing_written(harness.db)


async def test_the_claim_commits_before_the_egress_and_no_connection_is_held_during_the_send(
    harness: _Harness,
    switch: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switch(True)
    await _enforce(harness.db)
    db = harness.db
    media_id = await _store(db, _asset(db.tenant))
    seen: dict[str, Any] = {}
    charge = credit_inventory.charge_egress

    async def charge_after_the_claim(*args: Any, **kwargs: Any) -> Any:
        # The claim's transaction ended and its row is committed.
        seen["claim_open_at_charge"] = harness.claim.sessions[0].in_transaction()
        seen["claimed_rows_at_charge"] = await db.count(MediaDelivery)
        return await charge(*args, **kwargs)

    monkeypatch.setattr(credit_inventory, "charge_egress", charge_after_the_claim)

    async def during_send() -> None:
        seen["checked_out"] = db.engine.sync_engine.pool.checkedout()
        seen["claimed_rows_at_send"] = await db.count(MediaDelivery)
        seen["charged_at_send"] = await db.count(
            CreditEgressDelivery, CreditEgressDelivery.mode == "enforced"
        )

    harness.channel.during_send = during_send

    assert (await harness.deliver(media_id)).outcome == "sent"
    assert seen == {
        "claim_open_at_charge": False,
        "claimed_rows_at_charge": 1,
        # Only the probe's own session above was open; none of the delivery's.
        "checked_out": 0,
        "claimed_rows_at_send": 1,
        "charged_at_send": 1,
    }


# ---------------------------------------------------------------------------
# Credit, mode by mode.
# ---------------------------------------------------------------------------


async def test_off_records_nothing_and_runs_no_credit_sql(
    harness: _Harness, switch: Callable[[bool], None]
) -> None:
    switch(False)
    media_id = await _store(harness.db, _asset(harness.db.tenant))
    credit_sql: list[str] = []

    def statement(_conn: Any, _cursor: Any, sql: str, *_args: Any) -> None:
        if "credit_" in sql.lower():
            credit_sql.append(sql)

    sa_event.listen(harness.db.engine.sync_engine, "before_cursor_execute", statement)
    try:
        assert (await harness.deliver(media_id)).outcome == "sent"
    finally:
        sa_event.remove(
            harness.db.engine.sync_engine, "before_cursor_execute", statement
        )
    assert credit_sql == []
    assert "observe" not in harness.trace and "charge" not in harness.trace
    assert await harness.db.count(CreditEgressDelivery) == 0
    assert await harness.db.count(CreditObservationalEffect) == 0


async def test_observational_records_the_egress_with_its_size_before_the_send(
    harness: _Harness, switch: Callable[[bool], None]
) -> None:
    switch(True)
    db = harness.db
    media_id = await _store(db, _asset(db.tenant, payload=_pdf(4_321)))

    assert (await harness.deliver(media_id)).outcome == "sent"

    assert harness.trace.index("observe") < harness.trace.index("send")
    assert "charge" not in harness.trace
    async with db.sessions() as session:
        deliveries = list(
            (await session.execute(select(CreditEgressDelivery))).scalars()
        )
        effects = list(
            (
                await session.execute(
                    select(CreditObservationalEffect).where(
                        CreditObservationalEffect.effect_kind == "egress"
                    )
                )
            ).scalars()
        )
    assert [(d.mode, d.bytes, d.media_id) for d in deliveries] == [
        ("observational", 4_321, media_id)
    ]
    assert len(effects) == 1


async def test_enforced_charges_before_the_send_with_one_server_delivery_id_per_send(
    harness: _Harness, switch: Callable[[bool], None]
) -> None:
    switch(True)
    await _enforce(harness.db)
    db = harness.db
    media_id = await _store(db, _asset(db.tenant, payload=_pdf(2_500)))

    # The same claim row twice, as a second send after 24 hours would be.
    assert (await harness.deliver(media_id)).outcome == "sent"
    assert (await harness.deliver(media_id)).outcome == "sent"

    trace = harness.trace
    assert trace.count("charge") == 2
    first_send = trace.index("send")
    assert trace.index("charge") < first_send
    assert trace.index("charge", first_send) < trace.index("send", first_send + 1)
    async with db.sessions() as session:
        deliveries = list(
            (await session.execute(select(CreditEgressDelivery))).scalars()
        )
    assert {(d.mode, d.bytes) for d in deliveries} == {("enforced", 2_500)}
    ids = {d.delivery_id for d in deliveries}
    assert len(deliveries) == len(ids) == 2
    assert harness.claim.row_id not in ids


@pytest.mark.parametrize("case", ["hold", "standalone"])
async def test_credit_pending_refuses_after_the_claim_which_stays_consumed(
    harness: _Harness, switch: Callable[[bool], None], case: str
) -> None:
    switch(True)
    db = harness.db
    await _enforce(db)
    if case == "hold":
        await _hold(db)
        tenant, patient = db.tenant, db.patient
    else:
        tenant, patient = db.standalone, db.standalone_patient
    media_id = await _store(db, _asset(tenant))

    result = await harness.deliver(media_id, tenant_id=tenant, session_id=patient)

    assert result == ReplyMediaResult("credit_refused", notice_sent=True)
    assert harness.reasons() == ["credit_pending"]
    assert harness.trace == [
        "flag",
        "asset",
        "permit",
        "credit_mode",
        "claim",
        "charge",
        "release",
        "notice",
        "record",
    ]
    assert harness.channel.media_calls == []
    # The window is consumed (accepted, DESIGN Scope IN 5); nothing charged.
    assert await db.count(MediaDelivery) == 1
    assert await db.count(CreditEgressDelivery) == 0


@pytest.mark.parametrize(
    ("error", "outcome", "reason"),
    [
        (CreditTenantGone("gone"), "asset_gone", "tenant_gone"),
        (
            OperationalError("UPDATE", {}, Exception("down")),
            "credit_refused",
            "egress_failed:OperationalError",
        ),
    ],
)
async def test_a_failing_charge_sends_no_byte(
    harness: _Harness,
    switch: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    outcome: str,
    reason: str,
) -> None:
    switch(True)
    await _enforce(harness.db)
    media_id = await _store(harness.db, _asset(harness.db.tenant))

    async def failing(*_args: Any, **_kwargs: Any) -> Any:
        harness.trace.append("charge")
        raise error

    monkeypatch.setattr(credit_inventory, "charge_egress", failing)

    result = await harness.deliver(media_id)

    assert (result.outcome, result.notice_sent) == (outcome, True)
    assert harness.reasons() == [reason]
    assert harness.channel.media_calls == []
    assert harness.pool.limiter.active == 0


# ---------------------------------------------------------------------------
# The sender, the permit and the callback.
# ---------------------------------------------------------------------------


async def _until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(_WAIT):
        while not condition():
            await asyncio.sleep(0.01)


def _short_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        reply_media,
        "REPLY_MEDIA_SEND_TIMEOUT_SECONDS",
        dict.fromkeys(MEDIA_DELIVERY_PROVIDERS, 0.05),
    )


_SHORT_WAIT = 0.05


def _short_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Step 4's bound on the wait for the permit, shortened: only for a
    test whose permit is held past it on purpose (a wait that must NOT run
    out keeps the real 60 s, inside the harness's own bound)."""

    monkeypatch.setattr(reply_media, "AGENT_MEDIA_PERMIT_WAIT_SECONDS", _SHORT_WAIT)


async def _another_patient(db: _Db) -> UUID:
    patient = uuid4()
    await db.add(
        Session(
            id=patient,
            tenant_id=db.tenant,
            channel="whatsapp",
            cognitive_state={},
            session_metadata={},
        )
    )
    return patient


def _hold_the_send(channel: _Channel) -> tuple[asyncio.Event, asyncio.Event]:
    """The media send blocks until ``release`` is set; ``in_send`` says it
    started."""

    in_send, release = asyncio.Event(), asyncio.Event()

    async def hold() -> None:
        in_send.set()
        await release.wait()

    channel.during_send = hold
    return in_send, release


async def test_a_failing_provider_is_failed_with_the_notice(
    harness: _Harness,
) -> None:
    media_id = await _store(harness.db, _asset(harness.db.tenant))
    harness.channel.media_error = RuntimeError("upstream_http_error status=500")
    before = _sample("zapi", "failed")

    result = await harness.deliver(media_id)

    assert result == ReplyMediaResult("failed", notice_sent=True)
    assert harness.reasons() == ["send_failed:RuntimeError"]
    assert harness.trace[-5:] == ["payload", "send", "release", "notice", "record"]
    # The media's answer never came; the notice's did.
    assert harness.records == [{"messageId": "notice-message-id"}]
    assert harness.pool.limiter.active == 0
    assert _sample("zapi", "failed") == before + 1


async def test_a_hung_provider_is_cut_by_the_deadline_and_gives_the_permit_back(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _short_deadline(monkeypatch)
    media_id = await _store(harness.db, _asset(harness.db.tenant))
    harness.channel.media_hang = True

    result = await harness.deliver(media_id)

    assert result == ReplyMediaResult("failed", notice_sent=True)
    assert harness.pool.limiter.active == 0
    assert harness.trace[-4:] == ["send", "release", "notice", "record"]
    assert harness.reasons() == ["send_timeout"]


async def test_a_cancelled_delivery_gives_the_permit_back(harness: _Harness) -> None:
    media_id = await _store(harness.db, _asset(harness.db.tenant))
    harness.channel.media_hang = True
    task = asyncio.create_task(harness.deliver(media_id))
    while "send" not in harness.trace:
        await asyncio.sleep(0.01)
    assert harness.pool.limiter.active == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert harness.pool.limiter.active == 0
    assert harness.channel.text_calls == []


async def test_a_second_turn_of_the_pair_in_flight_in_this_process_is_deduplicated(
    harness: _Harness,
) -> None:
    """Two turns of ONE pair in ONE process share its only permit: one send
    and one ``deduplicated`` in silence -- never ``busy`` with the notice
    while the file is going (G5 review HIGH-1)."""

    media_id = await _store(harness.db, _asset(harness.db.tenant))
    in_send, release = _hold_the_send(harness.channel)
    before = _sample("zapi", "deduplicated")
    first = asyncio.create_task(harness.deliver(media_id))
    try:
        await asyncio.wait_for(in_send.wait(), _WAIT)
        second = await harness.deliver(media_id)
        assert not first.done()
    finally:
        release.set()

    assert second == ReplyMediaResult("deduplicated", notice_sent=False)
    assert await first == ReplyMediaResult("sent", notice_sent=False)
    assert harness.reasons() == ["in_flight", "sent"]
    # The second turn asked for no permit, claimed nothing and sent nothing.
    assert harness.trace.count("permit") == 1
    assert harness.trace.count("claim") == 1
    assert len(harness.channel.media_calls) == 1
    assert harness.channel.text_calls == []
    assert harness.records == [{"messageId": "media-message-id"}]
    assert await harness.db.count(MediaDelivery) == 1
    assert _sample("zapi", "deduplicated") == before + 1
    assert harness.pool.limiter.active == 0


@pytest.mark.parametrize("other", ["patient", "media"])
async def test_a_different_pair_waits_for_the_send_in_flight_then_gets_its_file(
    harness: _Harness, other: str
) -> None:
    """The owner's case (2026-11-08): two patients -- or two files -- at once
    in one worker, which has ONE permit.  The second turn waits for the
    first one's send to end, then sends: two ``sent`` in arrival order,
    never overlapping, and no notice."""

    db = harness.db
    media_id = await _store(db, _asset(db.tenant, filename="primeiro.pdf"))
    if other == "media":
        second_media = await _store(db, _asset(db.tenant, filename="segundo.pdf"))
        second_session = db.patient
    else:
        second_media, second_session = media_id, await _another_patient(db)
    in_send, release = _hold_the_send(harness.channel)
    first = asyncio.create_task(harness.deliver(media_id))
    try:
        await asyncio.wait_for(in_send.wait(), _WAIT)
        second = asyncio.create_task(
            harness.deliver(second_media, session_id=second_session)
        )
        await _until(lambda: harness.pool.limiter.waiting == 1)
        # Waiting: no claim, no send and no notice of its own yet.
        assert not second.done()
        assert harness.claim.pairs == [(db.patient, media_id)]
        assert len(harness.channel.media_calls) == 1
        assert harness.channel.text_calls == []
    finally:
        release.set()

    assert await first == ReplyMediaResult("sent", notice_sent=False)
    assert await second == ReplyMediaResult("sent", notice_sent=False)
    assert harness.reasons() == ["sent", "sent"]
    assert harness.claim.pairs == [
        (db.patient, media_id),
        (second_session, second_media),
    ]
    # ONE permit: the second send began only once the first released it.
    assert [step for step in harness.trace if step in ("send", "release")] == [
        "send",
        "release",
        "send",
        "release",
    ]
    if other == "media":
        assert [call["filename"] for call in harness.channel.media_calls] == [
            "primeiro.pdf",
            "segundo.pdf",
        ]
    assert harness.channel.text_calls == []
    assert harness.records == [{"messageId": "media-message-id"}] * 2
    assert (harness.pool.limiter.active, harness.pool.limiter.waiting) == (0, 0)
    first_wait, second_wait = harness.logged("permit_wait_seconds")
    assert second_wait > first_wait


@pytest.mark.parametrize("other", ["patient", "media"])
async def test_a_different_pair_is_busy_once_the_wait_runs_out(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, other: str
) -> None:
    _short_wait(monkeypatch)
    db = harness.db
    media_id = await _store(db, _asset(db.tenant))
    other_media = await _store(db, _asset(db.tenant)) if other == "media" else media_id
    other_session = await _another_patient(db) if other == "patient" else db.patient
    in_send, release = _hold_the_send(harness.channel)
    first = asyncio.create_task(harness.deliver(media_id))
    try:
        await asyncio.wait_for(in_send.wait(), _WAIT)
        second = await harness.deliver(other_media, session_id=other_session)
        assert not first.done()
        assert harness.pool.limiter.waiting == 0
    finally:
        release.set()

    assert second == ReplyMediaResult("busy", notice_sent=True)
    assert (await first).outcome == "sent"
    assert harness.reasons() == ["busy", "sent"]
    assert harness.channel.text_calls == [_PT_NOTICE]
    # The busy turn never claimed and never sent.
    assert harness.claim.pairs == [(db.patient, media_id)]
    assert len(harness.channel.media_calls) == 1
    assert (harness.pool.limiter.active, harness.pool.limiter.waiting) == (0, 0)


async def test_the_same_pair_arriving_while_its_first_turn_waits_is_deduplicated(
    harness: _Harness,
) -> None:
    """A patient's two messages while ANOTHER pair's send holds the permit:
    the first turn waits; the second, arriving during that wait, is
    ``deduplicated`` in silence -- it neither queues nor sends the notice
    nor writes -- and the first one then sends once."""

    db = harness.db
    media_id = await _store(db, _asset(db.tenant))
    held = harness.pool.limiter.try_acquire()  # another pair's send
    assert held is not None
    before = _sample("zapi", "deduplicated")
    first = asyncio.create_task(harness.deliver(media_id))
    try:
        await _until(lambda: harness.pool.limiter.waiting == 1)
        second = await harness.deliver(media_id)
        assert harness.pool.limiter.waiting == 1  # the second never queued
        assert not first.done()
    finally:
        held.release()

    assert second == ReplyMediaResult("deduplicated", notice_sent=False)
    assert await first == ReplyMediaResult("sent", notice_sent=False)
    assert harness.reasons() == ["in_flight", "sent"]
    assert harness.trace.count("permit") == 1
    assert harness.claim.pairs == [(db.patient, media_id)]
    assert len(harness.channel.media_calls) == 1
    assert harness.channel.text_calls == []
    assert await db.count(MediaDelivery) == 1
    assert _sample("zapi", "deduplicated") == before + 1
    assert (harness.pool.limiter.active, harness.pool.limiter.waiting) == (0, 0)


async def test_waiting_turns_are_served_in_arrival_order(harness: _Harness) -> None:
    db = harness.db
    names = ["0.pdf", "1.pdf", "2.pdf", "3.pdf"]
    media = [await _store(db, _asset(db.tenant, filename=name)) for name in names]
    in_send, release = _hold_the_send(harness.channel)
    turns = [asyncio.create_task(harness.deliver(media[0]))]
    try:
        await asyncio.wait_for(in_send.wait(), _WAIT)
        for queued, media_id in enumerate(media[1:], start=1):
            turns.append(asyncio.create_task(harness.deliver(media_id)))
            await _until(lambda count=queued: harness.pool.limiter.waiting == count)
    finally:
        release.set()
    results = await asyncio.wait_for(asyncio.gather(*turns), _WAIT)

    assert results == [ReplyMediaResult("sent", notice_sent=False)] * 4
    assert [call["filename"] for call in harness.channel.media_calls] == names
    assert harness.claim.pairs == [(db.patient, media_id) for media_id in media]
    assert harness.channel.text_calls == []
    assert (harness.pool.limiter.active, harness.pool.limiter.waiting) == (0, 0)


async def test_a_turn_cancelled_while_it_waits_leaves_no_permit_and_strands_nobody(
    harness: _Harness,
) -> None:
    db = harness.db
    names = ["enviando.pdf", "cancelado.pdf", "seguinte.pdf"]
    sending, cancelled, after = [
        await _store(db, _asset(db.tenant, filename=name)) for name in names
    ]
    in_send, release = _hold_the_send(harness.channel)
    first = asyncio.create_task(harness.deliver(sending))
    try:
        await asyncio.wait_for(in_send.wait(), _WAIT)
        leaving = asyncio.create_task(harness.deliver(cancelled))
        await _until(lambda: harness.pool.limiter.waiting == 1)
        last = asyncio.create_task(harness.deliver(after))
        await _until(lambda: harness.pool.limiter.waiting == 2)

        leaving.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leaving
        assert (harness.pool.limiter.active, harness.pool.limiter.waiting) == (1, 1)
    finally:
        release.set()

    assert await first == ReplyMediaResult("sent", notice_sent=False)
    assert await last == ReplyMediaResult("sent", notice_sent=False)
    assert [call["filename"] for call in harness.channel.media_calls] == [
        "enviando.pdf",
        "seguinte.pdf",
    ]
    # The cancelled turn claimed nothing, sent no notice and logged no outcome.
    assert harness.claim.pairs == [(db.patient, sending), (db.patient, after)]
    assert harness.channel.text_calls == []
    assert harness.reasons() == ["sent", "sent"]
    assert (harness.pool.limiter.active, harness.pool.limiter.waiting) == (0, 0)
    # Its pair is no longer in flight: its next turn sends.
    assert await harness.deliver(cancelled) == ReplyMediaResult(
        "sent", notice_sent=False
    )


@pytest.mark.parametrize(
    "end",
    ["sent", "failed", "timeout", "cancelled", "busy", "cancelled_while_waiting"],
)
async def test_the_pair_is_no_longer_in_flight_however_its_send_ends(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, end: str
) -> None:
    """``busy`` after the bounded wait and a cancellation WHILE waiting
    included: the pair is in flight from before the wait, so an exit the
    ``finally`` missed would leave it there, and every later turn of it in
    this worker would be ``deduplicated`` without a send until the process
    restarts."""

    _short_deadline(monkeypatch)
    media_id = await _store(harness.db, _asset(harness.db.tenant))
    if end in ("busy", "cancelled_while_waiting"):
        held = harness.pool.limiter.try_acquire()  # another pair's send
        assert held is not None
        try:
            if end == "busy":
                _short_wait(monkeypatch)
                assert (await harness.deliver(media_id)).outcome == "busy"
            else:
                task = asyncio.create_task(harness.deliver(media_id))
                await _until(lambda: harness.pool.limiter.waiting == 1)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        finally:
            held.release()
    elif end == "cancelled":
        harness.channel.media_hang = True
        task = asyncio.create_task(harness.deliver(media_id))
        await _until(lambda: "send" in harness.trace)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        if end == "failed":
            harness.channel.media_error = RuntimeError("upstream_http_error 500")
        harness.channel.media_hang = end == "timeout"
        await harness.deliver(media_id)
    assert (harness.pool.limiter.active, harness.pool.limiter.waiting) == (0, 0)

    # The next turn of the pair is the claim's to decide again, not refused
    # as in flight.
    harness.channel.media_error, harness.channel.media_hang = None, False
    harness.claim.taken = True
    harness.trace.clear()
    assert await harness.deliver(media_id) == ReplyMediaResult(
        "deduplicated", notice_sent=False
    )
    assert harness.trace[:5] == ["flag", "asset", "permit", "credit_mode", "claim"]
    assert harness.reasons()[-1] == "deduplicated"


class _ClosingInFlight(weakref.WeakKeyDictionary[Any, Any]):
    """``_IN_FLIGHT``, closing the given coroutines when ``_enter_in_flight``
    calls ``setdefault`` -- inside ``_IN_FLIGHT_LOCK``, where CPython 3.12
    runs a collection that fell due as the lock was taken, and the collector
    closes a garbage coroutine exactly so (``GeneratorExit``)."""

    def __init__(self, closing: list[Coroutine[Any, Any, Any]]) -> None:
        super().__init__()
        self.closing = closing

    def setdefault(self, key: Any, default: Any = None) -> Any:
        while self.closing:
            self.closing.pop().close()
        return super().setdefault(key, default)


def test_a_stranded_delivery_closed_inside_the_in_flight_lock_deadlocks_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A delivery waiting for the permit on a loop that then closes is
    garbage once its permit is revoked; its ``finally`` takes
    ``_IN_FLIGHT_LOCK``.  Closed while another delivery of the same thread
    holds that lock, it must neither deadlock (the lock is re-entrant) nor
    leave its pair in flight."""

    async def flag_on() -> bool:
        return True

    async def reread(*_args: Any) -> Any:
        return reply_media._Asset("pdf", "application/pdf", 1_000, "a.pdf", _pdf())

    async def answer(*_args: Any, **_kwargs: Any) -> Mapping[str, object]:
        return {"messageId": "x"}

    async def record(_sent: Mapping[str, object]) -> None:
        return None

    monkeypatch.setattr(egress_policy, "agent_media_send_enabled", flag_on)
    monkeypatch.setattr(reply_media, "_read_asset", reread)
    monkeypatch.setattr(reply_media, "AGENT_MEDIA_PERMIT_WAIT_SECONDS", 0.01)
    pool = QueuedLimiter(AGENT_MEDIA_SENDS_PER_PROCESS)
    channel = ReplyMediaChannel("zapi", answer, answer)

    def delivery(session_id: UUID) -> Coroutine[Any, Any, ReplyMediaResult]:
        return deliver_reply_media(
            channel=channel,
            media_id=uuid4(),
            tenant_id=uuid4(),
            session_id=session_id,
            response_language="pt-BR",
            session_factory=lambda: None,  # type: ignore[arg-type,return-value]
            agent_sends=pool,
            record_sent_id=record,
        )

    held = pool.try_acquire()
    assert held is not None
    dead = asyncio.new_event_loop()
    stranded = dead.create_task(delivery(uuid4()))
    while pool.waiting == 0:
        dead.run_until_complete(asyncio.sleep(0.001))
    dead.close()
    held.release()  # its loop is closed: taken back, the delivery is garbage
    stranded._log_destroy_pending = False  # type: ignore[attr-defined]
    assert len(reply_media._IN_FLIGHT[pool]) == 1  # its pair, still in flight
    closing = _ClosingInFlight([stranded.get_coro()])
    closing.update(reply_media._IN_FLIGHT)
    monkeypatch.setattr(reply_media, "_IN_FLIGHT", closing)
    blocker = pool.try_acquire()  # the next delivery waits, then is busy
    results: list[ReplyMediaResult] = []
    thread = threading.Thread(
        target=lambda: results.append(asyncio.run(delivery(uuid4()))), daemon=True
    )
    thread.start()
    thread.join(_WAIT)

    assert not thread.is_alive(), "deadlock: a finalizer re-entered the lock"
    assert stranded.get_coro().cr_frame is None
    assert [result.outcome for result in results] == ["busy"]
    assert closing[pool] == set()  # neither pair is left in flight
    assert blocker is not None
    blocker.release()
    assert (pool.active, pool.waiting) == (0, 0)


@pytest.mark.parametrize("kind", ["media", "notice"])
async def test_a_cancellation_once_the_provider_answered_still_records_its_id(
    harness: _Harness, kind: str
) -> None:
    """A caller's bound (or a shutdown) firing after the provider accepted
    the message must not cut step 9: an unrecorded echo id of an agent audio
    would pause the AI on Z-API (G5 review MEDIUM-1).  The cancellation
    still reaches the caller, once the record has finished."""

    if kind == "media":
        media_id = await _store(harness.db, _asset(harness.db.tenant))
        expected = {"messageId": "media-message-id"}
    else:
        harness.flag[0] = False
        media_id = uuid4()
        expected = {"messageId": "notice-message-id"}
    harness.record_delay = 0.2
    task = asyncio.create_task(harness.deliver(media_id))
    await _until(lambda: "record" in harness.trace)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert harness.records == [expected]
    assert harness.pool.limiter.active == 0


async def test_an_asset_deleted_after_the_claim_is_gone_before_any_byte(
    harness: _Harness,
    switch: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switch(True)
    db = harness.db
    media_id = await _store(db, _asset(db.tenant))
    observe = credit_inventory.record_observational_egress

    async def delete_then_observe(*args: Any, **kwargs: Any) -> Any:
        async with db.sessions() as session, session.begin():
            await session.execute(delete(MediaAsset).where(MediaAsset.id == media_id))
        return await observe(*args, **kwargs)

    monkeypatch.setattr(
        credit_inventory, "record_observational_egress", delete_then_observe
    )

    result = await harness.deliver(media_id)

    assert result == ReplyMediaResult("asset_gone", notice_sent=True)
    assert harness.reasons() == ["asset_gone_before_send"]
    assert harness.channel.media_calls == []
    assert harness.pool.limiter.active == 0


async def test_the_media_and_the_notice_answers_reach_the_callback(
    harness: _Harness,
) -> None:
    db = harness.db
    sent = await _store(db, _asset(db.tenant))
    gone = uuid4()

    assert (await harness.deliver(sent)).outcome == "sent"
    assert (await harness.deliver(gone)).outcome == "asset_gone"

    assert harness.records == [
        {"messageId": "media-message-id"},
        {"messageId": "notice-message-id"},
    ]


@pytest.mark.parametrize("failure", ["error", "hang"])
async def test_a_failing_or_hung_callback_changes_nothing(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    monkeypatch.setattr(reply_media, "REPLY_MEDIA_RECORD_TIMEOUT_SECONDS", 0.05)
    media_id = await _store(harness.db, _asset(harness.db.tenant))
    if failure == "error":
        harness.record_error = RuntimeError("redis down")
    else:
        harness.record_hang = True

    result = await harness.deliver(media_id)

    assert result == ReplyMediaResult("sent", notice_sent=False)
    warnings = [f for level, _m, f in harness.logs.records if level == "warning"]
    assert [w["reason"] for w in warnings] == ["media_record_failed"]


async def test_a_failing_notice_changes_no_outcome(harness: _Harness) -> None:
    harness.flag[0] = False
    harness.channel.text_error = RuntimeError("upstream_http_error status=500")

    result = await harness.deliver(uuid4())

    assert result == ReplyMediaResult("flag_off", notice_sent=False)
    assert harness.records == []


async def test_no_log_carries_the_phone_or_the_text(harness: _Harness) -> None:
    db = harness.db
    media_id = await _store(db, _asset(db.tenant))
    await harness.deliver(media_id)
    harness.channel.media_error = RuntimeError(f"failed for {_PHONE}")
    harness.claim.row_id = uuid4()
    await harness.deliver(media_id)
    harness.channel.text_error = RuntimeError(f"failed for {_PHONE}")
    await harness.deliver(uuid4())

    assert len(harness.logs.records) >= 4
    for _level, _message, fields in harness.logs.records:
        rendered = repr(fields)
        assert _PHONE not in rendered and _PT_NOTICE not in rendered
        assert {"tenant_id", "session_id", "media_id", "provider"} <= set(fields)


# ---------------------------------------------------------------------------
# The provider bindings.
# ---------------------------------------------------------------------------


_CREDENTIALS: dict[str, dict[str, str]] = {
    "meta": {"meta_phone_number_id": " pnid ", "meta_permanent_token": " tok "},
    "zapi": {
        "zapi_instance_id": " inst ",
        "zapi_token": " tok ",
        "zapi_client_token": " client ",
    },
    "uazapi": {"uazapi_api_url": " https://u.example ", "uazapi_token": " tok "},
    "evolution": {
        "evolution_api_url": " https://e.example ",
        "evolution_instance_name": " name ",
        "evolution_api_key": " key ",
    },
}
_BINDINGS: dict[
    str,
    tuple[
        Any, str, tuple[Any, ...], dict[str, Any], str, tuple[Any, ...], dict[str, Any]
    ],
] = {
    "meta": (
        meta_service,
        "send_meta_cloud_media",
        ("pnid", "tok", _PHONE),
        {},
        "send_meta_cloud_text",
        ("pnid", "tok", _PHONE, "aviso"),
        {},
    ),
    "zapi": (
        zapi_service,
        "send_zapi_media",
        ("inst", "tok", _PHONE),
        {"client_token": "client"},
        "send_text_message",
        ("inst", "tok", _PHONE, "aviso"),
        {"client_token": "client"},
    ),
    "uazapi": (
        uazapi_service,
        "send_uazapi_media",
        ("https://u.example", "tok", _PHONE),
        {},
        "send_uazapi_text",
        ("https://u.example", "tok", _PHONE, "aviso"),
        {},
    ),
    "evolution": (
        zapi_service,
        "send_evolution_media",
        ("https://e.example", "name", "key", _PHONE),
        {},
        "send_evolution_text",
        ("https://e.example", "name", "key", _PHONE, "aviso"),
        {},
    ),
}


@pytest.mark.parametrize("provider", sorted(_BINDINGS))
async def test_each_provider_binds_its_media_and_text_senders(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    module, media_name, media_args, media_kwargs, text_name, text_args, text_kwargs = (
        _BINDINGS[provider]
    )
    calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def recorder(name: str) -> Any:
        async def call(*args: Any, **kwargs: Any) -> dict[str, object]:
            calls.append((name, args, kwargs))
            return {"messageId": name}

        return call

    monkeypatch.setattr(module, media_name, recorder(media_name))
    monkeypatch.setattr(module, text_name, recorder(text_name))
    channel = reply_media_channel(provider, _CREDENTIALS[provider], _PHONE)

    assert channel.provider == provider
    payload = _pdf()
    assert await channel.send_media(
        payload, category="pdf", mime_type="application/pdf", filename="a.pdf"
    ) == {"messageId": media_name}
    assert await channel.send_text("aviso") == {"messageId": text_name}
    assert calls == [
        (
            media_name,
            (*media_args, payload),
            {
                "category": "pdf",
                "mime_type": "application/pdf",
                "filename": "a.pdf",
                **media_kwargs,
            },
        ),
        (text_name, text_args, text_kwargs),
    ]


@pytest.mark.parametrize(
    ("provider", "credentials", "phone", "message"),
    [
        ("webchat", _CREDENTIALS["zapi"], _PHONE, "reply_media_provider_unknown"),
        ("meta", {"meta_phone_number_id": "p"}, _PHONE, "credentials_incomplete"),
        ("zapi", {"zapi_instance_id": "i", "zapi_token": " "}, _PHONE, "incomplete"),
        ("uazapi", {"uazapi_token": "t"}, _PHONE, "credentials_incomplete"),
        ("evolution", {"evolution_api_url": "u"}, _PHONE, "credentials_incomplete"),
        ("zapi", _CREDENTIALS["zapi"], "", "reply_media_phone_missing"),
    ],
)
def test_an_unknown_provider_or_incomplete_credentials_never_bind(
    provider: str, credentials: dict[str, str], phone: str, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        reply_media_channel(provider, credentials, phone)


# ---------------------------------------------------------------------------
# The credit mode without a Request.
# ---------------------------------------------------------------------------


async def test_the_mode_without_request_follows_read_credit_mode(
    db: _Db, switch: Callable[[bool], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    statements: list[str] = []

    def statement(_conn: Any, _cursor: Any, sql: str, *_args: Any) -> None:
        statements.append(sql)

    sa_event.listen(db.engine.sync_engine, "before_cursor_execute", statement)
    try:
        async with db.sessions() as session:
            switch(False)
            assert await read_credit_mode_without_request(session) == CREDIT_OFF
            assert statements == []
            # The caller's captured switch wins over the process verdict.
            switch(True)
            assert (
                await read_credit_mode_without_request(session, switch_on=False)
                == CREDIT_OFF
            )
            assert statements == []
            observational = await read_credit_mode_without_request(session)
            assert observational.observational and not observational.enforced
            switch(False)
            captured = await read_credit_mode_without_request(session, switch_on=True)
            assert captured.observational
            await session.rollback()
        assert await read_credit_mode_without_request(object()) == CREDIT_OFF
        await _enforce(db)
        async with db.sessions() as session:
            switch(True)
            enforced = await read_credit_mode_without_request(session)
            assert enforced.enforced and enforced.enforcement_at is not None
    finally:
        sa_event.remove(db.engine.sync_engine, "before_cursor_execute", statement)

    async def unreadable(_self: CreditRepository) -> datetime:
        raise OperationalError("SELECT", {}, Exception("down"))

    monkeypatch.setattr(CreditRepository, "enforcement_at", unreadable)
    async with db.sessions() as session:
        # Seen enforced in this process: unreadable is unavailable (D1).
        with pytest.raises(CreditEnforcementUnavailable, match="no longer be read"):
            await read_credit_mode_without_request(session)
    credit_inventory.forget_enforcement_seen()
    async with db.sessions() as session:
        # Never seen: unreadable keeps the caller observational.
        fallback = await read_credit_mode_without_request(session)
        assert fallback.observational
