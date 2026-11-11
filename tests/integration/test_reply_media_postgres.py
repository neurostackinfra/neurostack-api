"""The agent's media delivery core on real PostgreSQL (envio-midia-agente G5).

One database at ``alembic upgrade head`` (047), shared by the module; every
test seeds its own builder, clinics, patient session and media assets.  The
delivery runs for real -- the asset re-reads, the credit mode read without a
``Request``, the suspension gate, ``charge_egress`` /
``record_observational_egress`` and the 24-hour claim -- against a pooled
engine of its own (a ``QueuePool``, so "no pooled connection is held" means
something); only the AppConfig flag, the credit switch and the provider are
fakes.  Pinned here:

* credit mode by mode: off writes the claim and nothing of credit;
  observational records the egress with ``size_bytes``; enforced charges it
  BEFORE the provider is called, with one server delivery id per send;
  ``credit_pending`` (a quarantine hold) refuses after the claim, which
  stays consumed, and sends the notice;
* during the provider call the claim and the charge are already committed,
  the pair's row is not locked, no delivery connection is checked out and
  no backend is idle in a transaction;
* two turns of the same pair in two processes (one ``agent_sends`` permit
  each): exactly one send; the other is ``deduplicated`` in silence WHILE
  the first is still in its provider call (the claim's row lock is gone
  before the send, also on the ``None`` side);
* in ONE process (one shared permit): two patients at once both get their
  file -- the second waits for the first one's send, holding no connection
  and no claim meanwhile, then is claimed, charged and sent, with no
  notice; the same pair arriving while its first turn waits or sends is
  ``deduplicated`` (``in_flight``) with no write;
* after 24 hours the same pair goes again;
* a claim that fails (a foreign key the database refuses) never reaches the
  egress nor the sender, and sends the notice;
* every refusal before the claim -- flag off, an asset of another tenant or
  no longer automatic, an OGG or M4A on Z-API (a format its sender does not
  send), a stereo OGG on Meta (the re-read's ``substring`` prefix), above
  its ceiling, no permit handed over within the (shortened) wait, an
  unavailable credit mode, a suspended builder -- writes no
  ``media_deliveries`` row and no egress;
* Meta sends a mono OGG (also one whose OpusHead ends at byte 301) and an
  M4A, claimed and charged once each;
* the sender gets the stored ``bytea`` payload exactly.

Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips, and only
``scripts/check-credit-integration.sh`` turns that skip into a failure.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import src.models  # noqa: F401  (registers every mapper)
from src.api.middleware.media_limits import AGENT_MEDIA_SENDS_PER_PROCESS, QueuedLimiter
from src.models.billing import BillingTopupOrder
from src.models.builder import Builder
from src.models.content_retention import CreditLifecycleEpisode
from src.models.credit import CreditAccount, CreditEnforcementState
from src.models.media_asset import MediaAsset
from src.models.session import Session
from src.models.tenant import Tenant
from src.repositories.credit import CreditRepository
from src.services import credit_inventory, credit_metering, egress_policy, reply_media
from src.services.credit_switch import CreditSwitch
from src.services.media_assets import media_payload_sha256
from src.services.reply_media import (
    MediaDeliveryProvider,
    ReplyMediaChannel,
    ReplyMediaResult,
    deliver_reply_media,
)
from tests.integration.test_asaas_billing_postgres import _validated_admin_url
from tests.integration.test_migration_036_postgres import _Cluster, _migrate

pytestmark = pytest.mark.integration

_WAIT = 20.0
_PT_NOTICE = (
    "Não consegui enviar o arquivo agora. A equipe da clínica pode te enviar por aqui."
)


def _pdf(size: int = 1_000) -> bytes:
    return b"%PDF-1.7\n" + b"7" * (size - 9)


def _png(size: int) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * (size - 8)


def _ogg_opus(channels: int = 1, *, segments: int = 1) -> bytes:
    """One RFC 3533 page whose first packet is an RFC 7845 OpusHead.

    ``segments`` 255 puts the OpusHead at its farthest offset (282, ending
    at byte 301, the re-read's prefix); audio-like bytes follow."""

    head = b"OpusHead" + bytes([1, channels]) + bytes(9)
    table = bytes([len(head)]) + bytes(segments - 1)
    page = b"OggS" + bytes([0, 2]) + bytes(20) + bytes([segments]) + table
    return page + head + b"\x4f" * 512


def _m4a() -> bytes:
    return bytes(4) + b"ftypM4A " + bytes(52)


# A filename that matches each stored MIME (fixture fidelity: the sender
# gets ``original_filename`` with the MIME).
_FILENAMES: dict[str, str] = {
    "application/pdf": "preparo.pdf",
    "image/png": "foto.png",
    "audio/ogg": "explicacao.ogg",
    "audio/mp4": "explicacao.m4a",
}


# ---------------------------------------------------------------------------
# The database.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Database:
    url: URL
    probe: AsyncEngine  # NullPool: seeding and the probes, never the delivery


@pytest.fixture(scope="module")
async def database() -> AsyncIterator[_Database]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin = create_async_engine(
        admin_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    cluster = _Cluster(admin_url=admin_url, admin=admin, created=[])
    probe: AsyncEngine | None = None
    try:
        name = await cluster.fresh()
        await _migrate(cluster.url(name), "upgrade", "head")
        probe = create_async_engine(cluster.url(name), poolclass=NullPool)
        async with probe.connect() as connection:
            assert (
                await connection.scalar(text("SELECT version_num FROM alembic_version"))
                == "047"
            )
        yield _Database(url=cluster.url(name), probe=probe)
    finally:
        if probe is not None:
            await probe.dispose()
        for created in list(reversed(cluster.created)):
            await cluster.drop(created)
        await admin.dispose()


@pytest.fixture
async def pooled(database: _Database) -> AsyncIterator[AsyncEngine]:
    """The delivery's own engine: a real pool, created in the test's loop."""

    engine = create_async_engine(database.url, pool_size=4, max_overflow=0)
    try:
        yield engine
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class _World:
    builder: UUID
    tenant: UUID
    foreign: UUID
    patient: UUID


async def _execute(database: _Database, sql: str, **params: Any) -> None:
    async with database.probe.begin() as connection:
        await connection.execute(text(sql), params)


async def _scalar(database: _Database, sql: str, **params: Any) -> Any:
    async with database.probe.connect() as connection:
        return await connection.scalar(text(sql), params)


def _probe_sessions(database: _Database) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(database.probe, expire_on_commit=False, autoflush=False)


@pytest.fixture
async def world(database: _Database) -> _World:
    world = _World(uuid4(), uuid4(), uuid4(), uuid4())
    async with _probe_sessions(database)() as session, session.begin():
        session.add(Builder(id=world.builder, name="Reply media builder"))
        await session.flush()
        for tenant_id in (world.tenant, world.foreign):
            session.add(
                Tenant(
                    id=tenant_id,
                    name=f"Clinic {tenant_id.hex[:6]}",
                    slug=f"reply-media-{tenant_id.hex}",
                    api_key_hash=f"unused-{tenant_id.hex}",
                    plan_type="clinic",
                    builder_id=world.builder,
                    config={},
                )
            )
        await session.flush()
        session.add(
            Session(
                id=world.patient,
                tenant_id=world.tenant,
                channel="whatsapp",
                cognitive_state={},
                session_metadata={},
            )
        )
    return world


async def _asset(
    database: _Database,
    tenant_id: UUID,
    *,
    payload: bytes | None = None,
    category: str = "pdf",
    mime_type: str = "application/pdf",
    auto_suggest: bool = True,
    trigger_intents: tuple[str, ...] = ("detalhes_servicos",),
) -> UUID:
    body = _pdf() if payload is None else payload
    media_id = uuid4()
    async with _probe_sessions(database)() as session, session.begin():
        session.add(
            MediaAsset(
                id=media_id,
                tenant_id=tenant_id,
                display_name="Preparo do exame",
                original_filename=_FILENAMES[mime_type],
                category=category,
                mime_type=mime_type,
                size_bytes=len(body),
                sha256=media_payload_sha256(body),
                payload=body,
                auto_suggest=auto_suggest,
                trigger_intents=list(trigger_intents),
            )
        )
    return media_id


@pytest.fixture(autouse=True)
async def _no_enforcement(database: _Database) -> AsyncIterator[None]:
    """``credit_enforcement_state`` is one global row: absent unless a test
    sets it, and removed after every test."""

    await _execute(database, "DELETE FROM credit_enforcement_state")
    credit_inventory.forget_enforcement_seen()
    credit_metering.forget_enforcement_seen()
    yield
    await _execute(database, "DELETE FROM credit_enforcement_state")
    credit_inventory.forget_enforcement_seen()
    credit_metering.forget_enforcement_seen()


async def _enforce(database: _Database) -> None:
    at = datetime.now(timezone.utc) - timedelta(days=90)
    async with _probe_sessions(database)() as session, session.begin():
        session.add(
            CreditEnforcementState(
                id=1, credit_enforcement_at=at, set_by="t", set_at=at
            )
        )


async def _hold(database: _Database, builder: UUID) -> None:
    async with _probe_sessions(database)() as session, session.begin():
        session.add(
            BillingTopupOrder(
                id=uuid4(),
                builder_id=builder,
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


async def _suspend(database: _Database, builder: UUID) -> None:
    exhausted = datetime.now(timezone.utc) - timedelta(hours=73)
    account = uuid4()
    async with _probe_sessions(database)() as session, session.begin():
        session.add(CreditAccount(id=account, builder_id=builder))
        await session.flush()
        session.add(
            CreditLifecycleEpisode(
                id=uuid4(),
                builder_id=builder,
                account_id=account,
                opened_effect_id=f"lifecycle:{builder}:episode:0:opened",
                exhausted_at=exhausted,
                suspension_due_at=exhausted + timedelta(hours=72),
                deletion_due_at=exhausted + timedelta(days=30),
            )
        )


async def _claims(database: _Database, tenant_id: UUID) -> int:
    return int(
        await _scalar(
            database,
            "SELECT count(*) FROM media_deliveries WHERE tenant_id = :t",
            t=tenant_id,
        )
    )


async def _egress(database: _Database, tenant_id: UUID) -> list[tuple[Any, ...]]:
    async with database.probe.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT mode, bytes, delivery_id, media_id FROM "
                "credit_egress_deliveries WHERE tenant_id = :t ORDER BY recorded_at"
            ),
            {"t": tenant_id},
        )
        return [tuple(row) for row in rows]


# ---------------------------------------------------------------------------
# The fakes.
# ---------------------------------------------------------------------------


@pytest.fixture
def switch(monkeypatch: pytest.MonkeyPatch) -> Callable[[bool], None]:
    def force(effective: bool) -> None:
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: effective)

    force(False)
    return force


@pytest.fixture
def flag(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    state = [True]

    async def read() -> bool:
        return state[0]

    monkeypatch.setattr(egress_policy, "agent_media_send_enabled", read)
    return state


@dataclass
class _Channel:
    during_send: Callable[[], Any] | None = None
    provider: MediaDeliveryProvider = "zapi"
    media_calls: list[bytes] = field(default_factory=list)
    text_calls: list[str] = field(default_factory=list)

    def bound(self) -> ReplyMediaChannel:
        return ReplyMediaChannel(self.provider, self.send_media, self.send_text)

    async def send_media(
        self, payload: bytes, *, category: Any, mime_type: str, filename: str
    ) -> Mapping[str, object]:
        self.media_calls.append(payload)
        if self.during_send is not None:
            await self.during_send()
        return {"messageId": "media-message-id"}

    async def send_text(self, text_value: str) -> Mapping[str, object]:
        self.text_calls.append(text_value)
        return {"messageId": "notice-message-id"}


@dataclass
class _Delivery:
    engine: AsyncEngine
    world: _World
    channel: _Channel = field(default_factory=_Channel)
    pool: QueuedLimiter = field(
        default_factory=lambda: QueuedLimiter(AGENT_MEDIA_SENDS_PER_PROCESS)
    )
    records: list[Mapping[str, object]] = field(default_factory=list)

    async def record(self, sent: Mapping[str, object]) -> None:
        self.records.append(sent)

    async def run(
        self,
        media_id: UUID,
        *,
        session_id: UUID | None = None,
    ) -> ReplyMediaResult:
        return await asyncio.wait_for(
            deliver_reply_media(
                channel=self.channel.bound(),
                media_id=media_id,
                tenant_id=self.world.tenant,
                session_id=session_id or self.world.patient,
                response_language="pt-BR",
                session_factory=async_sessionmaker(
                    self.engine, expire_on_commit=False, autoflush=False
                ),
                agent_sends=self.pool,
                record_sent_id=self.record,
            ),
            _WAIT,
        )


@pytest.fixture
def delivery(pooled: AsyncEngine, world: _World) -> _Delivery:
    return _Delivery(pooled, world)


@pytest.fixture
def reasons(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """The ``reason`` of every delivery's outcome log, in order: an outcome
    alone could come from another path (``credit_refused`` has several)."""

    seen: list[str] = []

    class _Logger:
        def info(self, message: str, **fields: Any) -> None:
            if message == reply_media.DELIVERY_LOG:
                seen.append(fields["reason"])

        def warning(self, message: str, **fields: Any) -> None:
            pass

    monkeypatch.setattr(reply_media, "logger", _Logger())
    return seen


# ---------------------------------------------------------------------------
# Credit, mode by mode.
# ---------------------------------------------------------------------------


async def test_an_automatic_asset_bound_to_no_subject_is_still_delivered(
    database: _Database,
    world: _World,
    delivery: _Delivery,
    flag: list[bool],
    reasons: list[str],
) -> None:
    # "Outras" is implied: a greeting or FAQ turn may pick a file bound to no
    # subject, so the re-read refuses only a file no longer automatic.
    payload = _pdf(1_234)
    media_id = await _asset(database, world.tenant, payload=payload, trigger_intents=())

    assert (await delivery.run(media_id)) == ReplyMediaResult("sent", False)
    assert reasons == ["sent"]
    assert delivery.channel.media_calls == [payload]
    assert await _claims(database, world.tenant) == 1


async def test_off_claims_once_and_records_no_credit(
    database: _Database,
    world: _World,
    delivery: _Delivery,
    flag: list[bool],
    reasons: list[str],
) -> None:
    payload = _pdf(3_210)
    media_id = await _asset(database, world.tenant, payload=payload)

    assert (await delivery.run(media_id)) == ReplyMediaResult("sent", False)
    assert (await delivery.run(media_id)) == ReplyMediaResult("deduplicated", False)
    assert reasons == ["sent", "deduplicated"]

    # The bytea payload, exactly, once.
    assert delivery.channel.media_calls == [payload]
    assert type(delivery.channel.media_calls[0]) is bytes
    assert delivery.channel.text_calls == []
    assert delivery.records == [{"messageId": "media-message-id"}]
    assert await _claims(database, world.tenant) == 1
    assert await _egress(database, world.tenant) == []
    assert (
        await _scalar(
            database,
            "SELECT count(*) FROM credit_observational_effects WHERE tenant_id = :t",
            t=world.tenant,
        )
        == 0
    )


async def test_observational_records_the_egress_with_its_size(
    database: _Database,
    world: _World,
    delivery: _Delivery,
    flag: list[bool],
    switch: Callable[[bool], None],
) -> None:
    switch(True)
    media_id = await _asset(database, world.tenant, payload=_pdf(4_321))

    assert (await delivery.run(media_id)).outcome == "sent"

    [(mode, size, _delivery_id, media)] = await _egress(database, world.tenant)
    assert (mode, size, media) == ("observational", 4_321, media_id)
    assert (
        await _scalar(
            database,
            "SELECT count(*) FROM credit_observational_effects "
            "WHERE tenant_id = :t AND effect_kind = 'egress'",
            t=world.tenant,
        )
        == 1
    )


async def test_enforced_charges_and_commits_before_the_send_holding_nothing(
    database: _Database,
    world: _World,
    delivery: _Delivery,
    pooled: AsyncEngine,
    flag: list[bool],
    switch: Callable[[bool], None],
) -> None:
    switch(True)
    await _enforce(database)
    media_id = await _asset(database, world.tenant, payload=_pdf(2_500))
    seen: dict[str, Any] = {}

    async def during_send() -> None:
        # Measured before the probe connects (the probe is a NullPool).
        seen["checked_out"] = pooled.sync_engine.pool.checkedout()
        async with database.probe.connect() as connection:
            async with connection.begin():
                await connection.execute(text("SET LOCAL lock_timeout = '1s'"))
                # The claim is committed and its row is NOT locked.
                locked = await connection.execute(
                    text(
                        "SELECT id FROM media_deliveries WHERE session_id = :s "
                        "AND media_id = :m FOR UPDATE NOWAIT"
                    ),
                    {"s": world.patient, "m": media_id},
                )
                seen["claim_rows"] = len(locked.all())
            seen["charged"] = await connection.scalar(
                text(
                    "SELECT count(*) FROM credit_egress_deliveries "
                    "WHERE tenant_id = :t AND mode = 'enforced'"
                ),
                {"t": world.tenant},
            )
            seen["idle_in_transaction"] = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = "
                    "current_database() AND pid <> pg_backend_pid() "
                    "AND state LIKE 'idle in transaction%'"
                )
            )

    delivery.channel.during_send = during_send

    assert (await delivery.run(media_id)).outcome == "sent"
    assert seen == {
        "checked_out": 0,
        "claim_rows": 1,
        "charged": 1,
        "idle_in_transaction": 0,
    }
    [(mode, size, delivery_id, media)] = await _egress(database, world.tenant)
    assert (mode, size, media) == ("enforced", 2_500, media_id)
    claim_id = await _scalar(
        database,
        "SELECT id FROM media_deliveries WHERE session_id = :s",
        s=world.patient,
    )
    assert delivery_id != claim_id


async def test_after_24_hours_the_same_pair_goes_again_with_a_new_delivery_id(
    database: _Database,
    world: _World,
    delivery: _Delivery,
    flag: list[bool],
    switch: Callable[[bool], None],
    reasons: list[str],
) -> None:
    switch(True)
    await _enforce(database)
    media_id = await _asset(database, world.tenant)

    assert (await delivery.run(media_id)).outcome == "sent"
    assert (await delivery.run(media_id)).outcome == "deduplicated"
    await _execute(
        database,
        "UPDATE media_deliveries SET last_sent_at = now() - interval '24 hours' "
        "WHERE session_id = :s",
        s=world.patient,
    )
    assert (await delivery.run(media_id)).outcome == "sent"

    assert reasons == ["sent", "deduplicated", "sent"]
    assert len(delivery.channel.media_calls) == 2
    assert await _claims(database, world.tenant) == 1
    charges = await _egress(database, world.tenant)
    assert [row[0] for row in charges] == ["enforced", "enforced"]
    assert len({row[2] for row in charges}) == 2


async def test_credit_pending_refuses_after_the_claim_which_stays_consumed(
    database: _Database,
    world: _World,
    delivery: _Delivery,
    flag: list[bool],
    switch: Callable[[bool], None],
    reasons: list[str],
) -> None:
    switch(True)
    await _enforce(database)
    await _hold(database, world.builder)
    media_id = await _asset(database, world.tenant)

    result = await delivery.run(media_id)

    assert result == ReplyMediaResult("credit_refused", notice_sent=True)
    assert reasons == ["credit_pending"]
    assert delivery.channel.media_calls == []
    assert delivery.channel.text_calls == [_PT_NOTICE]
    assert delivery.records == [{"messageId": "notice-message-id"}]
    assert await _claims(database, world.tenant) == 1
    assert await _egress(database, world.tenant) == []


# ---------------------------------------------------------------------------
# The claim.
# ---------------------------------------------------------------------------


async def test_two_turns_of_one_pair_send_once_and_the_second_never_waits(
    database: _Database,
    world: _World,
    pooled: AsyncEngine,
    flag: list[bool],
    switch: Callable[[bool], None],
    reasons: list[str],
) -> None:
    """Two processes (one permit each): the second turn is ``deduplicated``
    while the first one is still inside its provider call."""

    switch(True)
    await _enforce(database)
    media_id = await _asset(database, world.tenant)
    first, second = _Delivery(pooled, world), _Delivery(pooled, world)
    in_send, release = asyncio.Event(), asyncio.Event()

    async def hold_the_send() -> None:
        in_send.set()
        await release.wait()

    first.channel.during_send = hold_the_send
    sending = asyncio.create_task(first.run(media_id))
    try:
        await asyncio.wait_for(in_send.wait(), _WAIT)
        # Bounded well below the first one's pending send: a claim lock
        # still held would make this wait, and fail.
        racer = await asyncio.wait_for(second.run(media_id), 5)
        assert racer == ReplyMediaResult("deduplicated", notice_sent=False)
        assert not sending.done()
    finally:
        release.set()
    assert (await asyncio.wait_for(sending, _WAIT)).outcome == "sent"
    assert reasons == ["deduplicated", "sent"]

    assert len(first.channel.media_calls) == 1
    assert second.channel.media_calls == second.channel.text_calls == []
    assert second.records == []
    assert await _claims(database, world.tenant) == 1
    assert [row[0] for row in await _egress(database, world.tenant)] == ["enforced"]


async def test_two_turns_of_one_pair_in_one_process_send_once_without_the_notice(
    database: _Database,
    world: _World,
    pooled: AsyncEngine,
    flag: list[bool],
    switch: Callable[[bool], None],
    reasons: list[str],
) -> None:
    """One process (ONE shared ``agent_sends`` permit, Decision 9): the
    second turn is ``deduplicated`` in silence while the first one holds the
    permit in its provider call -- not ``busy`` with the notice (G5 review
    HIGH-1)."""

    switch(True)
    await _enforce(database)
    media_id = await _asset(database, world.tenant)
    first = _Delivery(pooled, world)
    second = _Delivery(pooled, world, pool=first.pool)  # the same process
    in_send, release = asyncio.Event(), asyncio.Event()

    async def hold_the_send() -> None:
        in_send.set()
        await release.wait()

    first.channel.during_send = hold_the_send
    sending = asyncio.create_task(first.run(media_id))
    try:
        await asyncio.wait_for(in_send.wait(), _WAIT)
        racer = await asyncio.wait_for(second.run(media_id), 5)
        assert racer == ReplyMediaResult("deduplicated", notice_sent=False)
        assert not sending.done()
    finally:
        release.set()
    assert (await asyncio.wait_for(sending, _WAIT)).outcome == "sent"
    assert reasons == ["in_flight", "sent"]

    assert len(first.channel.media_calls) == 1
    assert second.channel.media_calls == second.channel.text_calls == []
    assert second.records == []
    assert await _claims(database, world.tenant) == 1
    assert [row[0] for row in await _egress(database, world.tenant)] == ["enforced"]
    assert first.pool.active == 0


async def _another_patient(database: _Database, tenant_id: UUID) -> UUID:
    patient = uuid4()
    async with _probe_sessions(database)() as session, session.begin():
        session.add(
            Session(
                id=patient,
                tenant_id=tenant_id,
                channel="whatsapp",
                cognitive_state={},
                session_metadata={},
            )
        )
    return patient


async def test_two_patients_in_one_process_both_get_their_file_in_arrival_order(
    database: _Database,
    world: _World,
    pooled: AsyncEngine,
    flag: list[bool],
    switch: Callable[[bool], None],
    reasons: list[str],
) -> None:
    """The owner's case (2026-11-08): one worker, ONE permit, two patients'
    files at once.  The second turn waits for the first one's send -- with
    no claim of its own and no pooled connection held -- then is claimed,
    charged and sent; no notice."""

    switch(True)
    await _enforce(database)
    first_file, second_file = _pdf(1_100), _pdf(1_200)
    first_media = await _asset(database, world.tenant, payload=first_file)
    second_media = await _asset(database, world.tenant, payload=second_file)
    second_patient = await _another_patient(database, world.tenant)
    first = _Delivery(pooled, world)
    second = _Delivery(pooled, world, pool=first.pool)  # the same process
    in_send, release = asyncio.Event(), asyncio.Event()

    async def hold_the_send() -> None:
        in_send.set()
        await release.wait()

    first.channel.during_send = hold_the_send
    sending = asyncio.create_task(first.run(first_media))
    try:
        await asyncio.wait_for(in_send.wait(), _WAIT)
        waiting = asyncio.create_task(
            second.run(second_media, session_id=second_patient)
        )
        async with asyncio.timeout(_WAIT):
            while first.pool.waiting != 1:
                await asyncio.sleep(0.01)
        # While it waits: only the first pair is claimed and charged, and
        # neither turn holds a pooled connection.
        assert not waiting.done()
        assert await _claims(database, world.tenant) == 1
        assert len(await _egress(database, world.tenant)) == 1
        assert pooled.sync_engine.pool.checkedout() == 0
    finally:
        release.set()
    assert (await asyncio.wait_for(sending, _WAIT)).outcome == "sent"
    assert await asyncio.wait_for(waiting, _WAIT) == ReplyMediaResult(
        "sent", notice_sent=False
    )

    assert reasons == ["sent", "sent"]
    assert first.channel.media_calls == [first_file]
    assert second.channel.media_calls == [second_file]
    assert first.channel.text_calls == second.channel.text_calls == []
    assert await _claims(database, world.tenant) == 2
    charged = await _egress(database, world.tenant)
    assert [(mode, media) for mode, _bytes, _id, media in charged] == [
        ("enforced", first_media),
        ("enforced", second_media),
    ]
    assert (first.pool.active, first.pool.waiting) == (0, 0)


async def test_the_same_pair_arriving_while_its_first_turn_waits_writes_nothing(
    database: _Database,
    world: _World,
    pooled: AsyncEngine,
    flag: list[bool],
    switch: Callable[[bool], None],
    reasons: list[str],
) -> None:
    """One process: while another pair holds the permit, a patient's two
    turns of one file -- the first waits, the second is ``deduplicated``
    (``in_flight``) in silence, with no claim and no charge; then the first
    one is claimed, charged and sent once."""

    switch(True)
    await _enforce(database)
    media_id = await _asset(database, world.tenant)
    first = _Delivery(pooled, world)
    second = _Delivery(pooled, world, pool=first.pool)  # the same process
    held = first.pool.try_acquire()  # another pair's send
    assert held is not None
    waiting = asyncio.create_task(first.run(media_id))
    try:
        async with asyncio.timeout(_WAIT):
            while first.pool.waiting != 1:
                await asyncio.sleep(0.01)
        racer = await asyncio.wait_for(second.run(media_id), 5)
        assert racer == ReplyMediaResult("deduplicated", notice_sent=False)
        assert first.pool.waiting == 1
        assert await _claims(database, world.tenant) == 0
        assert await _egress(database, world.tenant) == []
    finally:
        held.release()
    assert (await asyncio.wait_for(waiting, _WAIT)).outcome == "sent"

    assert reasons == ["in_flight", "sent"]
    assert len(first.channel.media_calls) == 1
    assert second.channel.media_calls == second.channel.text_calls == []
    assert second.records == []
    assert await _claims(database, world.tenant) == 1
    assert [row[0] for row in await _egress(database, world.tenant)] == ["enforced"]
    assert (first.pool.active, first.pool.waiting) == (0, 0)


async def test_a_failed_claim_never_reaches_the_egress_nor_the_sender(
    database: _Database,
    world: _World,
    delivery: _Delivery,
    flag: list[bool],
    switch: Callable[[bool], None],
    reasons: list[str],
) -> None:
    switch(True)
    await _enforce(database)
    media_id = await _asset(database, world.tenant)

    # A session the database does not have: the claim's INSERT is refused
    # by fk_media_deliveries_session (23503).
    result = await delivery.run(media_id, session_id=uuid4())

    assert result == ReplyMediaResult("failed", notice_sent=True)
    assert reasons == ["claim_failed:IntegrityError"]
    assert delivery.channel.media_calls == []
    assert delivery.channel.text_calls == [_PT_NOTICE]
    assert await _claims(database, world.tenant) == 0
    assert await _egress(database, world.tenant) == []
    assert delivery.pool.active == 0


# ---------------------------------------------------------------------------
# Refusals before the claim write nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("case", "outcome", "reason"),
    [
        ("flag_off", "flag_off", "flag_off"),
        ("foreign_tenant", "asset_gone", "asset_gone"),
        ("auto_suggest_off", "asset_gone", "asset_gone"),
        ("ogg_on_zapi", "asset_gone", "mime_not_sendable"),
        ("m4a_on_zapi", "asset_gone", "mime_not_sendable"),
        ("stereo_ogg_on_meta", "asset_gone", "ogg_opus_not_mono"),
        ("stereo_ogg_long_table_on_meta", "asset_gone", "ogg_opus_not_mono"),
        ("above_ceiling", "too_large", "too_large"),
        ("no_permit", "busy", "busy"),
        ("mode_unavailable", "credit_refused", "credit_enforcement_unavailable"),
        ("suspended", "credit_refused", "tenant_suspended"),
    ],
)
async def test_a_refusal_before_the_claim_writes_nothing(
    database: _Database,
    world: _World,
    delivery: _Delivery,
    flag: list[bool],
    switch: Callable[[bool], None],
    monkeypatch: pytest.MonkeyPatch,
    reasons: list[str],
    case: str,
    outcome: str,
    reason: str,
) -> None:
    switch(True)
    await _enforce(database)
    held = None
    if case == "foreign_tenant":
        media_id = await _asset(database, world.foreign)
    elif case == "auto_suggest_off":
        media_id = await _asset(database, world.tenant, auto_suggest=False)
    elif case in ("ogg_on_zapi", "m4a_on_zapi"):
        ogg = case == "ogg_on_zapi"
        media_id = await _asset(
            database,
            world.tenant,
            payload=_ogg_opus() if ogg else _m4a(),
            category="audio",
            mime_type="audio/ogg" if ogg else "audio/mp4",
        )
    elif case in ("stereo_ogg_on_meta", "stereo_ogg_long_table_on_meta"):
        delivery.channel.provider = "meta"
        segments = 255 if case == "stereo_ogg_long_table_on_meta" else 1
        media_id = await _asset(
            database,
            world.tenant,
            payload=_ogg_opus(channels=2, segments=segments),
            category="audio",
            mime_type="audio/ogg",
        )
    elif case == "above_ceiling":
        media_id = await _asset(
            database,
            world.tenant,
            payload=_png(5_000_001),
            category="image",
            mime_type="image/png",
        )
    else:
        media_id = await _asset(database, world.tenant)
    if case == "flag_off":
        flag[0] = False
    elif case == "no_permit":
        # Held past step 4's bounded wait, shortened here.
        monkeypatch.setattr(reply_media, "AGENT_MEDIA_PERMIT_WAIT_SECONDS", 0.05)
        held = delivery.pool.try_acquire()
    elif case == "mode_unavailable":
        monkeypatch.setattr(credit_inventory, "_enforcement_seen", True)

        async def unreadable(_self: CreditRepository) -> datetime:
            raise OperationalError("SELECT", {}, Exception("database down"))

        monkeypatch.setattr(CreditRepository, "enforcement_at", unreadable)
    elif case == "suspended":
        await _suspend(database, world.builder)

    try:
        result = await delivery.run(media_id)
    finally:
        if held is not None:
            held.release()

    assert (result.outcome, result.notice_sent) == (outcome, True)
    assert reasons == [reason]
    assert delivery.channel.media_calls == []
    assert delivery.channel.text_calls == [_PT_NOTICE]
    assert await _claims(database, world.tenant) == 0
    assert await _egress(database, world.tenant) == []
    assert (delivery.pool.active, delivery.pool.waiting) == (0, 0)


@pytest.mark.parametrize("audio", ["ogg", "ogg_long_table", "m4a"])
async def test_meta_sends_a_mono_ogg_and_an_m4a_claimed_and_charged_once(
    database: _Database,
    world: _World,
    delivery: _Delivery,
    flag: list[bool],
    switch: Callable[[bool], None],
    reasons: list[str],
    audio: str,
) -> None:
    """``ogg_long_table``: the OpusHead at its farthest offset, ending at
    byte 301, is inside the re-read's prefix, so the mono file is sent."""

    switch(True)
    await _enforce(database)
    delivery.channel.provider = "meta"
    mime_type = "audio/mp4" if audio == "m4a" else "audio/ogg"
    payload = {
        "ogg": _ogg_opus(),
        "ogg_long_table": _ogg_opus(segments=255),
        "m4a": _m4a(),
    }[audio]
    media_id = await _asset(
        database, world.tenant, payload=payload, category="audio", mime_type=mime_type
    )

    assert (await delivery.run(media_id)) == ReplyMediaResult("sent", False)
    assert reasons == ["sent"]
    assert delivery.channel.media_calls == [payload]
    assert delivery.channel.text_calls == []
    assert await _claims(database, world.tenant) == 1
    ((mode, size, _delivery_id, charged_media),) = await _egress(database, world.tenant)
    assert (mode, size, charged_media) == ("enforced", len(payload), media_id)
