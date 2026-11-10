"""The agent's file after the reply, on real PostgreSQL (envio-midia-agente G6).

One database at ``alembic upgrade head`` (046) for the module.  Pinned here,
where only PostgreSQL can prove it:

* the real application (lifespan, middleware, its 5-connection pool with no
  overflow) runs a Z-API turn through the real pipeline, the real media
  selection SQL, the real 24-hour claim and the real delivery core: the
  patient gets the text, then the file;
* on the four inline webhooks (Z-API, Evolution, uazapi, Meta), the file's
  step ends the request's transaction BEFORE it starts the delivery: the
  request holds a connection when the step begins (Evolution's path holds
  one on its own; the others are made to) and none once the step returned,
  while the handler has not yet answered; and while that turn waits for the
  process's only ``agent_sends`` permit, NO pooled connection is checked
  out -- not by the request, not by the delivery;
* the outbox: an inline delivery whose ``delivered`` mark was lost sends the
  text and the file; the publisher that recovers the row past its lease
  resends the text, and the real claim refuses the file -- one file in all;
* a delivery whose database statement hangs is cut by the runner's outer
  bound, and the cut connection is invalidated: the next checkout of a
  1-connection pool gets a new backend, never the one the statement held.

Only the AppConfig flag, the credit mode, the LLM and the provider calls are
fakes.  Needs ``POSTGRES_SMOKE_DATABASE_URL``; without it the module skips,
and only ``scripts/check-credit-integration.sh`` turns that skip into a
failure.
"""

from __future__ import annotations

import asyncio
import base64
import functools
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event as sa_event
from sqlalchemy import select, text, update
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

import src.models  # noqa: F401  (registers every mapper)
from src.api.middleware.media_limits import QueuedLimiter
from src.models.builder import Builder
from src.models.clinic_config_table import ClinicConfigTable
from src.models.credit import CreditEnforcementState, CreditResponseOutbox
from src.models.media_asset import MediaAsset
from src.models.media_delivery import MediaDelivery
from src.models.session import Session
from src.models.tenant import Tenant
from src.repositories import media_deliveries
from src.repositories.credit import CreditRepository
from src.services import (
    credit_inventory,
    egress_policy,
    media_selection,
    meta_service,
    reply_media_runner,
    uazapi_service,
    zapi_service,
)
from src.services.credit_inventory import CREDIT_OFF
from src.services.credit_metering import (
    REPLY_PAYLOAD_KIND,
    SENDING_LEASE_SECONDS,
    CreditResponsePublisher,
    deliver_reply_inline,
)
from src.services.credit_switch import CreditSwitch
from src.services.media_assets import (
    MEDIA_BINDABLE_INTENT_SEQUENCE,
    media_payload_sha256,
)
from src.services.reply_media import ReplyMediaChannel
from src.services.reply_media_runner import ReplyMediaJob, start_reply_media
from tests.integration.test_asaas_billing_postgres import _validated_admin_url
from tests.integration.test_migration_036_postgres import _Cluster, _migrate

pytestmark = pytest.mark.integration

_WAIT = 30.0
_REPLY = "Segue o preparo do exame."
_META_SECRET = "pg_media_delivery_meta_secret"


def _pdf() -> bytes:
    return b"%PDF-1.7\n" + b"7" * 1991


@dataclass(frozen=True)
class _Database:
    url: URL
    probe: AsyncEngine  # NullPool: seeding and probes only


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
                == "046"
            )
        yield _Database(url=cluster.url(name), probe=probe)
    finally:
        if probe is not None:
            await probe.dispose()
        for created in list(reversed(cluster.created)):
            await cluster.drop(created)
        await admin.dispose()


def _sessions(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


@dataclass(frozen=True)
class _Clinic:
    tenant: UUID
    token: str
    instance: str
    asset: UUID
    patient: UUID  # a session seeded for the outbox tests


async def _clinic(database: _Database, provider: str = "zapi") -> _Clinic:
    token = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    clinic = _Clinic(
        tenant=uuid4(),
        token=token,
        instance=f"3E{uuid4().hex[:10]}",
        asset=uuid4(),
        patient=uuid4(),
    )
    builder = uuid4()
    body = _pdf()
    async with _sessions(database.probe)() as session, session.begin():
        session.add(Builder(id=builder, name="Webhook media builder"))
        await session.flush()
        session.add(
            Tenant(
                id=clinic.tenant,
                name=f"Webhook media {clinic.tenant.hex[:6]}",
                slug=f"webhook-media-{clinic.tenant.hex}",
                api_key_hash=f"unused-{clinic.tenant.hex}",
                plan_type="clinic",
                builder_id=builder,
                config={},
            )
        )
        await session.flush()
        session.add_all(
            [
                ClinicConfigTable(
                    tenant_id=clinic.tenant,
                    api_credentials=_credentials(clinic, provider),
                ),
                MediaAsset(
                    id=clinic.asset,
                    tenant_id=clinic.tenant,
                    display_name="Preparo do exame",
                    original_filename="preparo.pdf",
                    category="pdf",
                    mime_type="application/pdf",
                    size_bytes=len(body),
                    sha256=media_payload_sha256(body),
                    payload=body,
                    usage_trigger="quando perguntarem do preparo ou dos serviços",
                    auto_suggest=True,
                    # Every informative intent but the price one: whichever of
                    # them the real SRM gives the turn, the file is a candidate.
                    trigger_intents=[
                        intent
                        for intent in MEDIA_BINDABLE_INTENT_SEQUENCE
                        if intent != "pedido_preco"
                    ],
                ),
                Session(
                    id=clinic.patient,
                    tenant_id=clinic.tenant,
                    channel="whatsapp",
                    cognitive_state={},
                    session_metadata={},
                ),
            ]
        )
    return clinic


def _credentials(clinic: _Clinic, provider: str) -> dict[str, Any]:
    if provider == "zapi":
        return {
            "whatsapp_provider": "zapi",
            "zapi_instance_id": clinic.instance,
            "zapi_token": f"zt-{uuid4().hex}",
            "zapi_inbound_token_digest": hashlib.sha256(
                clinic.token.encode("ascii")
            ).hexdigest(),
        }
    if provider == "evolution":
        return {
            "whatsapp_provider": "evolution",
            "evolution_api_url": "https://evolution.example.com",
            "evolution_instance_name": clinic.instance,
            "evolution_api_key": f"evk-{uuid4().hex}",
        }
    if provider == "uazapi":
        return {
            "whatsapp_provider": "uazapi",
            "uazapi_api_url": "https://uazapi.example.com",
            "uazapi_token": f"uz-{uuid4().hex}",
            "uazapi_instance_name": clinic.instance,
        }
    assert provider == "meta"
    return {
        "whatsapp_provider": "meta_cloud",
        "meta_phone_number_id": clinic.instance,
        "meta_waba_id": f"waba_{clinic.instance}",
        "meta_permanent_token": f"EAAG_{uuid4().hex}",
        "meta_verify_token": f"verify_{uuid4().hex}",
        "meta_app_secret": _META_SECRET,
    }


@dataclass
class _Provider:
    """The provider's side of the file and the notice."""

    files: list[dict[str, Any]] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    async def send_zapi_media(
        self, _instance: str, _token: str, phone: str, payload: bytes, **kwargs: Any
    ) -> Mapping[str, object]:
        self.files.append({"phone": phone, "payload": payload, **kwargs})
        return {"messageId": f"pg-media-{len(self.files)}"}

    async def send_text_message(
        self, _instance: str, _token: str, _phone: str, text_: str, **_kwargs: Any
    ) -> Mapping[str, object]:
        self.notices.append(text_)
        return {"messageId": f"pg-notice-{len(self.notices)}"}

    async def send_evolution_media(
        self,
        _url: str,
        _name: str,
        _key: str,
        phone: str,
        payload: bytes,
        **kwargs: Any,
    ) -> Mapping[str, object]:
        self.files.append({"phone": phone, "payload": payload, **kwargs})
        return {"id": f"pg-media-{len(self.files)}"}

    async def send_evolution_text(
        self, _url: str, _name: str, _key: str, _phone: str, text_: str
    ) -> Mapping[str, object]:
        self.notices.append(text_)
        return {"id": f"pg-notice-{len(self.notices)}"}

    async def send_uazapi_media(
        self, _url: str, _token: str, phone: str, payload: bytes, **kwargs: Any
    ) -> Mapping[str, object]:
        self.files.append({"phone": phone, "payload": payload, **kwargs})
        return {"messageid": f"pg-media-{len(self.files)}"}

    async def send_uazapi_text(
        self, _url: str, _token: str, _phone: str, text_: str
    ) -> Mapping[str, object]:
        self.notices.append(text_)
        return {"messageid": f"pg-notice-{len(self.notices)}"}

    async def send_meta_media(
        self, _pnid: str, _token: str, phone: str, payload: bytes, **kwargs: Any
    ) -> Mapping[str, object]:
        self.files.append({"phone": phone, "payload": payload, **kwargs})
        return {"messages": [{"id": f"pg-media-{len(self.files)}"}]}

    async def send_meta_text(
        self, _pnid: str, _token: str, _phone: str, text_: str
    ) -> Mapping[str, object]:
        self.notices.append(text_)
        return {"messages": [{"id": f"pg-notice-{len(self.notices)}"}]}


class _LLM:
    async def generate(self, messages: list[dict[str, str]], **_kwargs: Any) -> Any:
        offered = any("[[MIDIA:n]]" in m.get("content", "") for m in messages)
        content = f"{_REPLY} [[MIDIA:1]]" if offered else _REPLY
        return {
            "content": content,
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "model": "webhook-media-fake",
        }


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> _Provider:
    from src.api.routes import webhook

    provider = _Provider()
    llm = _LLM()

    async def flag_on() -> bool:
        return True

    monkeypatch.setattr(media_selection, "agent_media_send_enabled", flag_on)
    monkeypatch.setattr(egress_policy, "agent_media_send_enabled", flag_on)
    monkeypatch.setattr("src.llm.client.get_llm_client", MagicMock(return_value=llm))
    monkeypatch.setattr(
        "src.llm.client.get_tenant_llm_client", MagicMock(return_value=llm)
    )
    monkeypatch.setattr(
        "src.llm.client.get_multimodal_text_llm_client", MagicMock(return_value=None)
    )
    monkeypatch.setattr(zapi_service, "send_zapi_media", provider.send_zapi_media)
    monkeypatch.setattr(zapi_service, "send_text_message", provider.send_text_message)
    monkeypatch.setattr(
        zapi_service, "send_evolution_media", provider.send_evolution_media
    )
    monkeypatch.setattr(
        zapi_service, "send_evolution_text", provider.send_evolution_text
    )
    monkeypatch.setattr(uazapi_service, "send_uazapi_media", provider.send_uazapi_media)
    monkeypatch.setattr(uazapi_service, "send_uazapi_text", provider.send_uazapi_text)
    monkeypatch.setattr(meta_service, "send_meta_cloud_media", provider.send_meta_media)
    monkeypatch.setattr(meta_service, "send_meta_cloud_text", provider.send_meta_text)
    monkeypatch.setattr(webhook, "mark_message_read", AsyncMock(return_value=None))
    monkeypatch.setattr(webhook, "mark_conversation_unread", AsyncMock())
    monkeypatch.setattr(webhook, "_get_redis", lambda: None)
    webhook._INMEM_INCOMING.clear()
    webhook._INMEM_SENT.clear()
    return provider


@asynccontextmanager
async def _application(
    database: _Database, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[Any, httpx.AsyncClient]]:
    """The real app on this database, its process pool and lifespan."""

    import src.repositories as repositories
    from src.api.main import app
    from src.config.settings import settings
    from tests.conftest import _production_init_database

    monkeypatch.setattr(settings, "credit_hot_path_enabled", False)
    monkeypatch.setattr(
        repositories,
        "init_database",
        functools.partial(
            _production_init_database,
            database_url=database.url.render_as_string(hide_password=False),
            engine_factory=create_async_engine,
        ),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("testclient", 50000))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as client:
            yield app, client


def _turn(provider: str, clinic: _Clinic, phone: str, text_: str) -> tuple[str, Any]:
    if provider == "zapi":
        return f"/v1/webhook/zapi/{clinic.token}", {
            "instanceId": clinic.instance,
            "phone": phone,
            "messageId": f"pg-in-{uuid4().hex[:12]}",
            "fromMe": False,
            "momment": 1711900000000,
            "senderName": "Paciente",
            "text": {"message": text_},
        }
    if provider == "uazapi":
        return "/v1/webhook/uazapi", {
            "instanceName": clinic.instance,
            "EventType": "messages",
            "owner": "5511940589006",
            "chat": {
                "wa_chatid": f"{phone}@s.whatsapp.net",
                "wa_name": "Paciente",
                "phone": f"+{phone}",
            },
            "message": {
                "sender_pn": f"{phone}@s.whatsapp.net",
                "text": text_,
                "content": text_,
                "type": "text",
                "messageType": "Conversation",
                "messageid": f"pg-in-{uuid4().hex[:12]}",
                "fromMe": False,
            },
        }
    if provider == "meta":
        return "/v1/webhook/meta", {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": f"waba_{clinic.instance}",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": phone,
                                    "phone_number_id": clinic.instance,
                                },
                                "contacts": [
                                    {"profile": {"name": "Paciente"}, "wa_id": phone}
                                ],
                                "messages": [
                                    {
                                        "from": phone,
                                        "id": f"pg-in-{uuid4().hex[:12]}",
                                        "timestamp": "1711900000",
                                        "text": {"body": text_},
                                        "type": "text",
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
    return "/v1/webhook/evolution", {
        "event": "messages.upsert",
        "instance": clinic.instance,
        "data": {
            "key": {
                "remoteJid": f"{phone}@s.whatsapp.net",
                "fromMe": False,
                "id": f"pg-in-{uuid4().hex[:12]}",
            },
            "message": {"conversation": text_},
            "pushName": "Paciente",
        },
    }


async def _until(condition: Any, what: str) -> None:
    for _ in range(int(_WAIT / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


async def _deliveries(database: _Database, tenant: UUID) -> list[tuple[UUID, UUID]]:
    async with _sessions(database.probe)() as session:
        rows = await session.execute(
            select(MediaDelivery.session_id, MediaDelivery.media_id).where(
                MediaDelivery.tenant_id == tenant
            )
        )
        return [(row.session_id, row.media_id) for row in rows]


def _post(client: httpx.AsyncClient, path: str, body: Any) -> Any:
    if path != "/v1/webhook/meta":
        return client.post(path, json=body)
    raw = json.dumps(body).encode()
    signature = hmac.new(_META_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return client.post(
        path,
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={signature}",
        },
    )


_TEXT_SENDERS = {
    "zapi": ("send_text_message", 3),
    "evolution": ("send_evolution_text", 4),
    "uazapi": ("send_uazapi_text", 3),
    "meta": ("send_meta_cloud_text", 3),
}


@pytest.mark.parametrize("provider", ["zapi", "evolution", "uazapi", "meta"])
async def test_a_turn_waiting_for_the_permit_holds_no_connection(
    database: _Database,
    fakes: _Provider,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    """Z-API and Meta: the real e2e.  Evolution and uazapi: origin forced
    ``production``, as the cloud-readiness origin will be.  Evolution's path
    re-reads the credentials after the pipeline and, with a send that
    returns no id, never commits -- the request DOES hold a connection when
    the file's step begins.  On the other three the probe makes it hold one
    (a read on the request's session, as any later step of the path could
    do).  Then the step must release it BEFORE it starts the delivery: the
    request holds none once the step returned, its handler not yet
    answered, and none while the turn waits for the permit."""

    from src.api.routes import webhook
    from src.interaction_context import InteractionOrigin
    from src.repositories import database as database_module

    clinic = await _clinic(database, provider)
    phone = f"55119{secrets.randbelow(10**8):08d}"
    reply = AsyncMock(return_value={})  # no id: no commit after the send
    sender, text_arg = _TEXT_SENDERS[provider]
    monkeypatch.setattr(webhook, sender, reply)
    if provider in ("evolution", "uazapi"):
        real_context = webhook.build_interaction_context

        def production(**kwargs: Any) -> Any:
            kwargs["origin"] = InteractionOrigin.PRODUCTION
            return real_context(**kwargs)

        monkeypatch.setattr(webhook, "build_interaction_context", production)
    at_entry: list[int] = []
    engaged: list[int] = []
    after_step: list[int] = []
    real_step = webhook._send_inline_reply_media
    async with _application(database, monkeypatch) as (app, client):
        engine = database_module._engine
        assert engine is not None
        pool = engine.sync_engine.pool

        async def probed(*args: Any, **kwargs: Any) -> None:
            at_entry.append(pool.checkedout())
            if not at_entry[-1]:
                await kwargs["db"].execute(text("SELECT 1"))
            engaged.append(pool.checkedout())
            await real_step(*args, **kwargs)
            # The step returned (its task created, not yet run): the
            # request's transaction is over -- before the handler answers.
            after_step.append(pool.checkedout())

        monkeypatch.setattr(webhook, "_send_inline_reply_media", probed)
        agent_sends = app.state.media_concurrency.agent_sends
        held = await agent_sends.acquire(1)  # another patient's file, sending
        assert held is not None
        path, body = _turn(provider, clinic, phone, "Quais serviços vocês oferecem?")
        try:
            post = asyncio.create_task(_post(client, path, body))
            await _until(lambda: agent_sends.waiting == 1, "the turn's permit wait")
            # Waiting for the permit, the turn holds no pooled connection:
            # neither the request nor the delivery.
            assert pool.checkedout() == 0
            await _until(post.done, "the webhook's answer")  # answered meanwhile
            assert agent_sends.waiting == 1 and pool.checkedout() == 0
            assert fakes.files == []
        finally:
            held.release()
        response = await post
        await asyncio.wait_for(reply_media_runner.wait_idle(), _WAIT)
        assert pool.checkedout() == 0
    if provider == "evolution":
        assert at_entry == [1]  # the request held one when the step began
    else:
        assert len(at_entry) == 1
    assert engaged == [1]  # the request holds one when the step begins
    assert after_step == [0]  # and none once the step returned
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "replied": True}
    [sent] = reply.await_args_list
    text_sent = sent.args[text_arg]
    assert "[[" not in text_sent and _REPLY in text_sent
    [file] = fakes.files
    assert (file["phone"], file["category"], file["filename"]) == (
        phone,
        "pdf",
        "preparo.pdf",
    )
    assert file["payload"] == _pdf()
    assert fakes.notices == []
    [(session_id, media_id)] = await _deliveries(database, clinic.tenant)
    assert media_id == clinic.asset and session_id != clinic.patient


async def test_a_recovered_row_resends_the_text_and_the_claim_refuses_the_file(
    database: _Database, fakes: _Provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.api.routes import webhook

    clinic = await _clinic(database)
    engine = create_async_engine(database.url, pool_size=4, max_overflow=0)
    sessions = _sessions(engine)
    texts = AsyncMock(return_value={"messageId": "pg-reply"})
    monkeypatch.setattr(webhook, "send_text_message", texts)
    monkeypatch.setattr(CreditSwitch, "effective", lambda _self: True)

    async def off(*_args: Any, **_kwargs: Any) -> Any:
        return CREDIT_OFF

    monkeypatch.setattr(credit_inventory, "read_credit_mode_without_request", off)
    real_mark = CreditRepository.mark_outbox_delivered
    lost = [True]

    async def lose_once(self: Any, *args: Any, **kwargs: Any) -> Any:
        if lost:
            lost.clear()
            raise RuntimeError("the delivered mark was lost")
        return await real_mark(self, *args, **kwargs)

    monkeypatch.setattr(CreditRepository, "mark_outbox_delivered", lose_once)
    pool = QueuedLimiter(1)
    row = uuid4()
    at = datetime.now(timezone.utc) - timedelta(minutes=1)
    try:
        async with sessions() as session, session.begin():
            if await session.get(CreditEnforcementState, 1) is None:
                session.add(
                    CreditEnforcementState(
                        id=1, credit_enforcement_at=at, set_by="test", set_at=at
                    )
                )
            session.add(
                CreditResponseOutbox(
                    id=row,
                    tenant_id=clinic.tenant,
                    effect_id=f"trace:{clinic.tenant}:{uuid4()}:decision:1",
                    payload={
                        "kind": REPLY_PAYLOAD_KIND,
                        "channel": "zapi",
                        "tenant_id": str(clinic.tenant),
                        "session_id": str(clinic.patient),
                        "phone": "5511900006060",
                        "text": _REPLY,
                        "language_code": "pt-BR",
                        "input_was_audio": False,
                        "force_text": False,
                        "handoff": False,
                        "decided_at": datetime.now(timezone.utc).isoformat(),
                        "session_pause": [False, None],
                        "media_id": str(clinic.asset),
                    },
                    state="pending",
                    attempts=0,
                )
            )
        inline = await asyncio.wait_for(
            deliver_reply_inline(
                row,
                sender=webhook.CREDIT_REPLY_SENDER,
                session_factory=sessions,
                agent_sends=pool,
            ),
            _WAIT,
        )
        await asyncio.wait_for(reply_media_runner.wait_idle(), _WAIT)
        assert inline.outcome == "delivered"
        assert len(fakes.files) == 1
        async with sessions() as session:
            stored = await session.get(CreditResponseOutbox, row)
            assert stored is not None and stored.state == "sending"
        async with sessions() as session, session.begin():
            await session.execute(
                update(CreditResponseOutbox)
                .where(CreditResponseOutbox.id == row)
                .values(
                    created_at=datetime.now(timezone.utc)
                    - timedelta(seconds=SENDING_LEASE_SECONDS + 5)
                )
            )
        publisher = CreditResponsePublisher(
            webhook.CREDIT_REPLY_SENDER, session_factory=sessions, agent_sends=pool
        )
        tick = await asyncio.wait_for(publisher.run_once(), _WAIT)
        await asyncio.wait_for(reply_media_runner.wait_idle(), _WAIT)
        assert tick is not None and tick.outcomes == ["delivered"]
        assert texts.await_count == 2  # the text, resent once (at-least-once)
        assert len(fakes.files) == 1  # the file, never: the claim refused it
        assert fakes.notices == []  # ``deduplicated`` is silent
        assert await _deliveries(database, clinic.tenant) == [
            (clinic.patient, clinic.asset)
        ]
        async with sessions() as session:
            stored = await session.get(CreditResponseOutbox, row)
            assert stored is not None and stored.state == "delivered"
    finally:
        await engine.dispose()


async def test_the_outer_bound_cuts_a_hung_statement_and_drops_its_connection(
    database: _Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    clinic = await _clinic(database)
    engine = create_async_engine(database.url, pool_size=1, max_overflow=0)
    sessions = _sessions(engine)
    invalidated: list[Any] = []
    sa_event.listen(
        engine.sync_engine.pool,
        "invalidate",
        lambda _dbapi, _record, exception: invalidated.append(exception),
    )
    held_pid: list[int] = []

    async def hung_claim(session: AsyncSession, **_kwargs: Any) -> Any:
        held_pid.append(int(await session.scalar(text("SELECT pg_backend_pid()"))))
        await session.execute(text("SELECT pg_sleep(5)"))
        raise AssertionError("the outer bound did not cut the statement")

    async def flag_on() -> bool:
        return True

    async def off(*_args: Any, **_kwargs: Any) -> Any:
        return CREDIT_OFF

    sends: list[Any] = []

    async def send(*args: Any, **kwargs: Any) -> Mapping[str, object]:
        sends.append((args, kwargs))
        return {"messageId": "never"}

    alerts: list[dict[str, Any]] = []

    class Logs:
        def error(self, _message: str, **fields: Any) -> None:
            alerts.append(fields)

        def __getattr__(self, _level: str) -> Any:
            return lambda *_a, **_k: None

    monkeypatch.setattr(media_deliveries, "claim_media_delivery", hung_claim)
    monkeypatch.setattr(egress_policy, "agent_media_send_enabled", flag_on)
    monkeypatch.setattr(credit_inventory, "read_credit_mode_without_request", off)
    monkeypatch.setattr(reply_media_runner, "REPLY_MEDIA_OUTER_SECONDS", {"zapi": 1.0})
    monkeypatch.setattr(reply_media_runner, "logger", Logs())

    async def record(_sent: Any) -> None:
        return None

    try:
        task = start_reply_media(
            ReplyMediaJob(
                channel=ReplyMediaChannel("zapi", send, send),  # type: ignore[arg-type]
                media_id=clinic.asset,
                tenant_id=clinic.tenant,
                session_id=clinic.patient,
                response_language="pt-BR",
                record_sent_id=record,
            ),
            session_factory=sessions,
            agent_sends=QueuedLimiter(1),
        )
        assert task is not None
        await asyncio.wait_for(task, _WAIT)
        assert [alert["alert"] for alert in alerts] == ["agent_media_outer_bound"]
        assert sends == []  # cut before any byte: neither the file nor a notice
        assert len(invalidated) >= 1
        assert engine.sync_engine.pool.checkedout() == 0
        # The pool's only connection is a NEW backend, never the cut one.
        async with sessions() as session:
            fresh_pid = int(await session.scalar(text("SELECT pg_backend_pid()")))
        [cut_pid] = held_pid
        assert fresh_pid != cut_pid
        assert await _deliveries(database, clinic.tenant) == []
    finally:
        await engine.dispose()
