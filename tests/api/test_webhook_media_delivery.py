"""The agent's file after the reply on the four inline webhooks (envio-midia-agente G6).

The real application (SQLite ``:memory:``) and the real webhook routes, with
the real phase 2 -- ``finish_response``, the media selection's exclusions,
the LLM messages and the ``[[MIDIA:n]]`` parser -- behind a canned phase 1,
and the real delivery core (:mod:`src.services.reply_media`, run by
:mod:`src.services.reply_media_runner`).  Only what is PostgreSQL-only is a
stand-in: the selection's two SQL reads and the 24-hour claim (their proofs
run in ``tests/integration``).  Every provider call -- the text, the file and
the notice -- goes through the real senders to an ``httpx.MockTransport``.

Pinned here:

* the success case on Z-API, Meta, uazapi and Evolution (their origin forced
  ``production`` by the test, as the WISH asks): the patient gets the text
  without the marker, THEN the file with the right type (a PDF as a
  document with its ``original_filename``); the webhook answers as before;
* the selection and the delivery get the same provider value (a spy on
  both, and the call sites' structure); the delivery meters under the credit
  switch the request captured at entry, never the process verdict (``None``);
* no file unless the text's send returned (a 5xx text: no file);
* a channel the delivery cannot bind (its ``ValueError``) never turns the
  webhook into a 500 after the text went out;
* the ``fromMe`` echo of the agent's audio file on Z-API, and of the notice
  on Z-API and Evolution, is the agent's (no pause) -- by the in-process
  tracking and, with it cleared, by the id stored in the session.  Evolution
  answers its sends in the real v2 shape (the id at ``key.id`` only, a
  ``3EB0`` socket id), so its echo depends on #203's ``key.id`` read;
* a turn without a file (text, audio reply, scheduling link, handoff) never
  starts a delivery and sends exactly today's requests;
* without the application's ``agent_sends`` pool the turn is offered none.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import hmac
import inspect
import json
import secrets
import socket
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import httpx
import pytest
from prometheus_client import REGISTRY
from sqlalchemy import select

import src.services.event_service as event_service_module
from src.api.routes import webhook
from src.core.pipeline import CognitivePipeline, PipelineResult
from src.interaction_context import InteractionOrigin
from src.models.clinic_config_table import ClinicConfigTable
from src.models.media_asset import MediaAsset
from src.models.media_delivery import MediaDelivery
from src.models.session import Session
from src.models.tenant import Tenant
from src.repositories import media_deliveries
from src.repositories.database import get_db_session
from src.services import (
    egress_policy,
    media_selection,
    reply_media,
    reply_media_runner,
    safe_egress,
    uazapi_service,
)
from src.services.api_credentials import media_delivery_provider
from src.services.event_service import EventService
from src.services.media_assets import media_payload_sha256

_PUBLIC_IP = "157.240.229.35"
_META_SECRET = "meta_media_delivery_test_secret"
_REPLY = "Segue o preparo do exame."
_LLM_WITH_MARKER = f"{_REPLY} [[MIDIA:1]]"
_PT_NOTICE = reply_media.REPLY_MEDIA_NOTICES["pt"]
_PROVIDERS = ("zapi", "meta", "uazapi", "evolution")


# ---------------------------------------------------------------------------
# HTTP: every provider call through the real senders.
# ---------------------------------------------------------------------------


def _addrinfo(_host: str, port: int) -> list[tuple[Any, ...]]:
    return [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (_PUBLIC_IP, port))
    ]


@dataclass
class _Call:
    kind: str  # text | media | upload | other
    provider: str
    path: str
    body: Any
    mid: str


@dataclass
class _Http:
    """The providers' side: records every request in order and answers each
    with a fresh message id in the provider's shape."""

    calls: list[_Call] = field(default_factory=list)
    fail: dict[str, int] = field(default_factory=dict)  # kind -> HTTP status

    def _kind(self, request: httpx.Request) -> tuple[str, str]:
        host, path = request.url.host, request.url.path
        if host == "api.z-api.io":
            if path.endswith("/send-text"):
                return "zapi", "text"
            if path.endswith(("/read-message", "/modify-chat")):
                return "zapi", "other"
            return "zapi", "media"
        if host == "graph.facebook.com":
            if path.endswith("/media"):
                return "meta", "upload"
            body = json.loads(request.content)
            return "meta", "text" if body.get("type") == "text" else "media"
        if host == "uazapi.example.com":
            return "uazapi", "text" if path.endswith("/send/text") else "media"
        if host == "evolution.example.com":
            return "evolution", "text" if "/sendText/" in path else "media"
        return "unknown", "other"

    def handler(self, request: httpx.Request) -> httpx.Response:
        provider, kind = self._kind(request)
        try:
            body: Any = json.loads(request.content) if request.content else None
        except ValueError:
            body = {"multipart": True}
        if provider == "evolution":
            # Baileys' socket id: ``3EB0`` + 18 uppercase hex.
            mid = f"3EB0{len(self.calls) + 1:04X}{secrets.token_hex(7).upper()}"
        else:
            mid = f"{provider}-{kind}-{len(self.calls) + 1}"
        self.calls.append(_Call(kind, provider, request.url.path, body, mid))
        status = self.fail.get(kind)
        if status is not None:
            return httpx.Response(status, json={"error": "refused"})
        if provider == "meta":
            if kind == "upload":
                return httpx.Response(200, json={"id": "meta-upload-1"})
            return httpx.Response(
                200, json={"messaging_product": "whatsapp", "messages": [{"id": mid}]}
            )
        if provider == "uazapi":
            return httpx.Response(200, json={"messageid": mid, "id": f"owner:{mid}"})
        if provider == "evolution":
            # Evolution v2's send answer is the Baileys message: the id only
            # at ``key.id``, no top-level ``id`` (``openapi-v2.json``).
            jid = f"{(body or {}).get('number', '')}@s.whatsapp.net"
            return httpx.Response(
                200,
                json={
                    "key": {"remoteJid": jid, "fromMe": True, "id": mid},
                    "message": {},
                    "messageTimestamp": 1711900000,
                    "status": "SERVER_ACK",
                },
            )
        return httpx.Response(200, json={"zaapId": "z", "messageId": mid, "id": mid})

    def of(self, kind: str) -> list[_Call]:
        return [call for call in self.calls if call.kind == kind]

    def kinds(self) -> list[str]:
        return [call.kind for call in self.calls if call.kind != "other"]


@contextmanager
def _providers(http: _Http) -> Iterator[None]:
    mock = httpx.MockTransport(http.handler)
    original_dns = safe_egress._default_getaddrinfo
    original_build = safe_egress.build_safe_async_client

    def build(validated: Any, *, timeout: Any = None, transport: Any = None) -> Any:
        return original_build(validated, timeout=timeout, transport=mock)

    safe_egress._default_getaddrinfo = _addrinfo  # type: ignore[assignment]
    safe_egress.build_safe_async_client = build  # type: ignore[assignment]
    try:
        yield
    finally:
        safe_egress._default_getaddrinfo = original_dns  # type: ignore[assignment]
        safe_egress.build_safe_async_client = original_build  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Phase 2 for real, behind a canned phase 1.
# ---------------------------------------------------------------------------


def _result(request: Any, **overrides: Any) -> PipelineResult:
    values: dict[str, Any] = {
        "tenant_id": request.tenant_id,
        "session_id": request.session_id,
        "intent": "detalhes_servicos",
        "intent_category": "information",
        "funnel_stage": "em_qualificacao",
        "action": "provide_info",
        "confidence": 0.9,
        "safety_concern": False,
        "safety_category": "none",
        "needs_rag": False,
        "handoff_required": False,
        "system_prompt": "sys",
        "instructions": "inst",
        "mode": "normal",
        "state_patch": {"slots": {}, "last_intent": "detalhes_servicos"},
    }
    values.update(overrides)
    return PipelineResult(**values)


class _WebhookService(EventService):
    """The real phase 2 (``finish_response``); phase 1 is canned."""

    def __init__(self) -> None:
        super().__init__(pipeline=CognitivePipeline())
        self.providers: list[Any] = []

    async def _load_clinic_config(self, tenant_id: Any) -> Any:  # type: ignore[override]
        return {}

    async def run_pipeline(  # type: ignore[override]
        self, request: Any, session: Any = None, *, context: Any
    ) -> Any:
        return _result(request), {}, []

    async def finish_response(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
        self.providers.append(kwargs.get("media_delivery_provider"))
        return await super().finish_response(*args, **kwargs)

    async def _save_events_to_store(  # type: ignore[override]
        self, *_args: Any, **_kwargs: Any
    ) -> Optional[UUID]:
        return None


class _LLM:
    def __init__(self, content: str) -> None:
        self.content = content

    async def generate(self, messages: list[dict[str, str]], **_kwargs: Any) -> Any:
        return {"content": self.content, "usage": {}, "model": "stub"}


@dataclass
class _World:
    http: _Http
    service: _WebhookService
    asset_id: UUID
    selected: list[str]  # the selection's ``delivery_provider``, per turn
    delivered: list[str]  # the delivery channel's provider, per delivery
    started: list[Any]  # every start_reply_media call
    claims: list[tuple[UUID, UUID]]
    llm: _LLM


def _seed(client: Any, rows: list[Any]) -> None:
    async def run() -> None:
        db = get_db_session()
        try:
            for row in rows:
                db.add(row)
                await db.flush()
            await db.commit()
        finally:
            await db.close()

    client._submit(run())


def _tenant(tenant_id: UUID) -> Tenant:
    return Tenant(
        id=tenant_id,
        name=f"Media delivery {tenant_id}",
        api_key_hash=f"media-delivery-{tenant_id}",
        is_active=True,
        config={},
    )


def _pdf() -> bytes:
    return b"%PDF-1.7\n" + b"7" * 991


def _mp3() -> bytes:
    return b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\x00" * 990


def _asset(tenant_id: UUID, *, category: str = "pdf") -> MediaAsset:
    payload, mime, name = (
        (_pdf(), "application/pdf", "preparo.pdf")
        if category == "pdf"
        else (_mp3(), "audio/mpeg", "explicacao.mp3")
    )
    return MediaAsset(
        id=uuid4(),
        tenant_id=tenant_id,
        display_name="Preparo do exame",
        original_filename=name,
        category=category,
        mime_type=mime,
        size_bytes=len(payload),
        sha256=media_payload_sha256(payload),
        payload=payload,
        usage_trigger="quando perguntarem do preparo",
        auto_suggest=True,
        trigger_intents=["detalhes_servicos"],
    )


def _session_row(client: Any, tenant_id: UUID) -> Session:
    async def run() -> Session:
        db = get_db_session()
        try:
            rows = (
                (
                    await db.execute(
                        select(Session).where(Session.tenant_id == tenant_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(rows) == 1, rows
            return rows[0]
        finally:
            await db.close()

    return client._submit(run())  # type: ignore[no-any-return]


def _delivery_rows(client: Any, tenant_id: UUID) -> list[MediaDelivery]:
    async def run() -> list[MediaDelivery]:
        db = get_db_session()
        try:
            return list(
                (
                    await db.execute(
                        select(MediaDelivery).where(
                            MediaDelivery.tenant_id == tenant_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        finally:
            await db.close()

    return client._submit(run())  # type: ignore[no-any-return]


def _drain(client: Any) -> None:
    client._submit(reply_media_runner.wait_idle())


def _deliveries(provider: str, outcome: str) -> float:
    return (
        REGISTRY.get_sample_value(
            "neurostack_agent_media_delivery_total",
            {"provider": provider, "outcome": outcome},
        )
        or 0.0
    )


@pytest.fixture(autouse=True)
def _process_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from src.api.middleware.rate_limit import rate_limit_store

    webhook._INMEM_INCOMING.clear()
    webhook._INMEM_SENT.clear()
    webhook._INMEM_STT_NOTICE.clear()
    rate_limit_store._buckets.clear()
    rate_limit_store._tenant_limits.clear()
    # No Redis: the echo is recognised in process or by the stored id only.
    monkeypatch.setattr(webhook, "_get_redis", lambda: None)
    monkeypatch.delenv("SCHEDULING_GATE_MODE", raising=False)
    yield
    webhook._INMEM_SENT.clear()


@pytest.fixture
def world(client: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    http = _Http()
    service = _WebhookService()
    llm = _LLM(_LLM_WITH_MARKER)
    selected: list[str] = []
    delivered: list[str] = []
    started: list[Any] = []
    claims: list[tuple[UUID, UUID]] = []
    asset_ref: dict[str, MediaAsset] = {}

    async def flag_on() -> bool:
        return True

    monkeypatch.setattr(media_selection, "agent_media_send_enabled", flag_on)
    monkeypatch.setattr(egress_policy, "agent_media_send_enabled", flag_on)
    monkeypatch.setattr("src.llm.client.get_tenant_llm_client", lambda *_a: llm)
    monkeypatch.setattr(
        "src.llm.client.get_multimodal_text_llm_client", lambda *_a: None
    )
    monkeypatch.setattr(
        event_service_module,
        "_load_runtime_llm_credentials",
        AsyncMock(return_value={"llm_model_name": "stub"}),
    )
    monkeypatch.setattr(
        event_service_module,
        "_load_multimodal_fallback_credentials",
        AsyncMock(return_value={}),
    )
    monkeypatch.setattr(webhook, "get_event_service", lambda: service)

    real_select = media_selection.select_media_candidates

    async def select_spy(**kwargs: Any) -> Any:
        selected.append(kwargs["delivery_provider"])
        return await real_select(**kwargs)

    async def candidates(_db: Any, **kwargs: Any) -> list[Any]:
        asset = asset_ref["asset"]
        assert asset.mime_type in kwargs["sendable"]
        return [
            SimpleNamespace(
                id=asset.id,
                display_name=asset.display_name,
                usage_trigger=asset.usage_trigger,
                category=asset.category,
                mime_type=asset.mime_type,
                size_bytes=asset.size_bytes,
            )
        ]

    async def recent(_db: Any, **_kwargs: Any) -> list[str]:
        return []

    monkeypatch.setattr(media_selection, "select_media_candidates", select_spy)
    monkeypatch.setattr(media_selection, "_read_candidates", candidates)
    monkeypatch.setattr(media_selection, "_read_recently_sent_names", recent)

    async def claim(
        session: Any, *, tenant_id: UUID, session_id: UUID, media_id: UUID
    ) -> UUID | None:
        """The PostgreSQL claim's stand-in: the row, once per pair."""

        claims.append((session_id, media_id))
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

    real_deliver = reply_media_runner.deliver_reply_media

    async def deliver_spy(**kwargs: Any) -> Any:
        delivered.append(kwargs["channel"].provider)
        return await real_deliver(**kwargs)

    monkeypatch.setattr(reply_media_runner, "deliver_reply_media", deliver_spy)
    real_start = reply_media_runner.start_reply_media

    def start_spy(job: Any, **kwargs: Any) -> Any:
        started.append(job)
        return real_start(job, **kwargs)

    monkeypatch.setattr(reply_media_runner, "start_reply_media", start_spy)

    async def allow() -> None:
        return None

    monkeypatch.setattr(uazapi_service, "require_uazapi_egress", allow)

    built = _World(
        http=http,
        service=service,
        asset_id=uuid4(),
        selected=selected,
        delivered=delivered,
        started=started,
        claims=claims,
        llm=llm,
    )
    built._asset_ref = asset_ref  # type: ignore[attr-defined]
    with _providers(http):
        yield built
    _drain(client)


def _force_production(monkeypatch: pytest.MonkeyPatch) -> None:
    """uazapi and Evolution classify ``unknown`` today; the WISH's case gives
    them ``production`` from the test (the cloud-readiness origin)."""

    real = webhook.build_interaction_context

    def production(**kwargs: Any) -> Any:
        kwargs["origin"] = InteractionOrigin.PRODUCTION
        return real(**kwargs)

    monkeypatch.setattr(webhook, "build_interaction_context", production)


# ---------------------------------------------------------------------------
# The four providers: tenants and inbound messages.
# ---------------------------------------------------------------------------


@dataclass
class _Clinic:
    provider: str
    tenant_id: UUID
    phone: str
    post: Callable[[str, str], Any]  # (message id, text) -> response
    echo: Callable[[str, str], Any]  # (message id, text) -> fromMe response
    audio_echo: Optional[Callable[[str], Any]] = None


def _clinic(
    client: Any,
    world: _World,
    provider: str,
    *,
    category: str = "pdf",
    extra_credentials: Optional[dict[str, Any]] = None,
) -> _Clinic:
    tenant_id = uuid4()
    phone = f"55119{secrets.randbelow(10**8):08d}"
    asset = _asset(tenant_id, category=category)
    world._asset_ref["asset"] = asset  # type: ignore[attr-defined]
    world.asset_id = asset.id
    credentials: dict[str, Any]
    if provider == "zapi":
        token = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
        instance = f"3E{uuid4().hex[:10]}"
        credentials = {
            "whatsapp_provider": "zapi",
            "zapi_instance_id": instance,
            "zapi_token": f"zt_{uuid4().hex}",
            "zapi_inbound_token_digest": hashlib.sha256(token.encode()).hexdigest(),
        }

        def body(mid: str, text: str, *, from_me: bool = False, **extra: Any) -> dict:
            payload: dict[str, Any] = {
                "instanceId": instance,
                "phone": phone,
                "messageId": mid,
                "fromMe": from_me,
                "momment": 1711900000000,
                "senderName": "Paciente",
            }
            if text:
                payload["text"] = {"message": text}
            payload.update(extra)
            return payload

        def post(mid: str, text: str) -> Any:
            return client.post(f"/v1/webhook/zapi/{token}", json=body(mid, text))

        def echo(mid: str, text: str) -> Any:
            return client.post(
                f"/v1/webhook/zapi/{token}", json=body(mid, text, from_me=True)
            )

        def audio_echo(mid: str) -> Any:
            audio = {"audioUrl": "https://cdn.z-api.io/a.mp3", "seconds": 3}
            return client.post(
                f"/v1/webhook/zapi/{token}",
                json=body(mid, "", from_me=True, audio=audio),
            )

        made = _Clinic(provider, tenant_id, phone, post, echo, audio_echo)
    elif provider == "meta":
        pnid = f"pnid{secrets.randbelow(10**9)}"
        credentials = {
            "whatsapp_provider": "meta_cloud",
            "meta_phone_number_id": pnid,
            "meta_waba_id": f"waba_{pnid}",
            "meta_permanent_token": f"EAAG_{uuid4().hex}",
            "meta_verify_token": f"verify_{uuid4().hex}",
            "meta_app_secret": _META_SECRET,
        }

        def post(mid: str, text: str) -> Any:
            raw = json.dumps(
                {
                    "object": "whatsapp_business_account",
                    "entry": [
                        {
                            "id": f"waba_{pnid}",
                            "changes": [
                                {
                                    "field": "messages",
                                    "value": {
                                        "messaging_product": "whatsapp",
                                        "metadata": {
                                            "display_phone_number": phone,
                                            "phone_number_id": pnid,
                                        },
                                        "contacts": [
                                            {
                                                "profile": {"name": "Paciente"},
                                                "wa_id": phone,
                                            }
                                        ],
                                        "messages": [
                                            {
                                                "from": phone,
                                                "id": mid,
                                                "timestamp": "1711900000",
                                                "text": {"body": text},
                                                "type": "text",
                                            }
                                        ],
                                    },
                                }
                            ],
                        }
                    ],
                }
            ).encode()
            signature = hmac.new(_META_SECRET.encode(), raw, hashlib.sha256).hexdigest()
            return client.request(
                "POST",
                "/v1/webhook/meta",
                content=raw,
                headers={
                    "Content-Type": "application/json",
                    "X-Hub-Signature-256": f"sha256={signature}",
                },
            )

        def no_echo(_mid: str, _text: str) -> Any:  # Meta sends no echo
            raise AssertionError("Meta has no fromMe echo")

        made = _Clinic(provider, tenant_id, phone, post, no_echo)
    elif provider == "uazapi":
        instance = f"uz_{uuid4().hex[:8]}"
        credentials = {
            "whatsapp_provider": "uazapi",
            "uazapi_api_url": "https://uazapi.example.com",
            "uazapi_token": f"uz_token_{uuid4().hex}",
            "uazapi_instance_name": instance,
        }

        def uazapi_body(mid: str, text: str, *, from_me: bool) -> dict:
            return {
                "instanceName": instance,
                "EventType": "messages",
                "owner": "5511940589006",
                "chat": {
                    "wa_chatid": f"{phone}@s.whatsapp.net",
                    "wa_name": "Paciente",
                    "phone": f"+{phone}",
                },
                "message": {
                    "sender_pn": f"{phone}@s.whatsapp.net",
                    "text": text,
                    "content": text,
                    "type": "text",
                    "messageType": "Conversation",
                    "messageid": mid,
                    "fromMe": from_me,
                },
            }

        def post(mid: str, text: str) -> Any:
            return client.post(
                "/v1/webhook/uazapi", json=uazapi_body(mid, text, from_me=False)
            )

        def echo(mid: str, text: str) -> Any:
            return client.post(
                "/v1/webhook/uazapi", json=uazapi_body(mid, text, from_me=True)
            )

        made = _Clinic(provider, tenant_id, phone, post, echo)
    else:
        instance = f"evo_{uuid4().hex[:8]}"
        credentials = {
            "whatsapp_provider": "evolution",
            "evolution_api_url": "https://evolution.example.com",
            "evolution_instance_name": instance,
            "evolution_api_key": f"evk_{uuid4().hex}",
        }

        def evolution_body(mid: str, text: str, *, from_me: bool) -> dict:
            return {
                "event": "messages.upsert",
                "instance": instance,
                "data": {
                    "key": {
                        "remoteJid": f"{phone}@s.whatsapp.net",
                        "fromMe": from_me,
                        "id": mid,
                    },
                    "message": {"conversation": text},
                    "pushName": "Paciente",
                },
            }

        def post(mid: str, text: str) -> Any:
            return client.post(
                "/v1/webhook/evolution", json=evolution_body(mid, text, from_me=False)
            )

        def echo(mid: str, text: str) -> Any:
            return client.post(
                "/v1/webhook/evolution", json=evolution_body(mid, text, from_me=True)
            )

        made = _Clinic(provider, tenant_id, phone, post, echo)
    credentials.update(extra_credentials or {})
    _seed(
        client,
        [
            _tenant(tenant_id),
            ClinicConfigTable(tenant_id=tenant_id, api_credentials=credentials),
            asset,
        ],
    )
    return made


def _turn(client: Any, clinic: _Clinic, text: str = "Como é o preparo?") -> Any:
    response = clinic.post(f"in-{uuid4().hex[:12]}", text)
    _drain(client)
    return response


# ---------------------------------------------------------------------------
# The success case on the four providers.
# ---------------------------------------------------------------------------


def _media_request_shape(call: _Call) -> tuple[str, Any]:
    """(what the provider was asked to send, the file's name as sent)."""

    body = call.body
    if call.provider == "zapi":
        return call.path.rsplit("/", 2)[-2] + "/" + call.path.rsplit("/", 1)[
            -1
        ], body.get("fileName")
    if call.provider == "meta":
        return body["type"], body[body["type"]].get("filename")
    if call.provider == "uazapi":
        return body["type"], body.get("docName")
    return body["mediatype"], body.get("fileName")


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_the_chosen_pdf_reaches_the_patient_after_the_text(
    client: Any, world: _World, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    if provider in ("uazapi", "evolution"):
        _force_production(monkeypatch)
    clinic = _clinic(client, world, provider)
    sent_before = _deliveries(provider, "sent")
    switches: list[Any] = []
    real_switch = webhook.request_credit_switch

    def switch_spy(request: Any) -> Any:
        switches.append(real_switch(request))
        return switches[-1]

    monkeypatch.setattr(webhook, "request_credit_switch", switch_spy)

    response = _turn(client, clinic)

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "replied": True}
    # The text without the marker, THEN the file: one document, its name.
    [text] = world.http.of("text")
    assert "[[" not in json.dumps(text.body) and _REPLY in json.dumps(
        text.body, ensure_ascii=False
    )
    assert world.http.kinds()[0] == "text"
    [media] = world.http.of("media")
    assert world.http.calls.index(media) > world.http.calls.index(text)
    kind, filename = _media_request_shape(media)
    expected_kind = {
        "zapi": "send-document/pdf",
        "meta": "document",
        "uazapi": "document",
        "evolution": "document",
    }[provider]
    assert (kind, filename) == (expected_kind, "preparo.pdf")
    if provider == "meta":
        assert world.http.kinds() == ["text", "upload", "media"]
    else:
        assert world.http.kinds() == ["text", "media"]
    # Claimed once for the turn's session and file, counted ``sent``.
    session = _session_row(client, clinic.tenant_id)
    assert world.claims == [(session.id, world.asset_id)]
    assert [
        (row.session_id, row.media_id)
        for row in _delivery_rows(client, clinic.tenant_id)
    ] == [(session.id, world.asset_id)]
    assert _deliveries(provider, "sent") == sent_before + 1
    # The same provider value for the selection and the delivery.
    assert world.selected == [provider] == world.delivered
    assert world.service.providers == [provider]
    # The delivery meters under the switch the request captured, never the
    # process verdict read later (``None``).
    [job] = world.started
    assert switches and isinstance(switches[0], bool)
    assert job.credit_switch_on is switches[0]
    # Its echo id is the agent's: tracked and stored in the session.
    stored = [
        entry[0] for entry in (session.session_metadata or {}).get("ai_msg_ids", [])
    ]
    assert media.mid in stored and text.mid in stored


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_the_selection_and_the_delivery_name_the_route_s_provider(
    provider: str,
) -> None:
    """Each webhook's constant is its WhatsApp provider through the map the
    Central de Mídias reads, and the call sites hand ONE ``media`` object to
    the selection (``_run_pipeline_transactionally``) and to the delivery
    (``_send_inline_reply_media``)."""

    constants = {
        "zapi": (
            webhook._ZAPI_MEDIA_PROVIDER,
            "zapi",
            "_process_zapi_authenticated_body",
        ),
        "meta": (webhook._META_MEDIA_PROVIDER, "meta_cloud", "meta_webhook"),
        "uazapi": (webhook._UAZAPI_MEDIA_PROVIDER, "uazapi", "uazapi_webhook"),
        "evolution": (
            webhook._EVOLUTION_MEDIA_PROVIDER,
            "evolution",
            "evolution_webhook",
        ),
    }
    constant, whatsapp_provider, function_name = constants[provider]
    assert constant == media_delivery_provider(whatsapp_provider) == provider
    tree = ast.parse(inspect.getsource(webhook))
    [function] = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == function_name
    ]
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]

    def named(call: ast.Call) -> str:
        return getattr(call.func, "id", getattr(call.func, "attr", ""))

    [wiring] = [call for call in calls if named(call) == "_inline_media"]
    assert (
        ast.unparse(wiring.args[1])
        == {
            "zapi": "_ZAPI_MEDIA_PROVIDER",
            "meta": "_META_MEDIA_PROVIDER",
            "uazapi": "_UAZAPI_MEDIA_PROVIDER",
            "evolution": "_EVOLUTION_MEDIA_PROVIDER",
        }[provider]
    )
    [pipeline] = [
        call for call in calls if named(call) == "_run_pipeline_transactionally"
    ]
    assert {k.arg: ast.unparse(k.value) for k in pipeline.keywords}["media"] == "media"
    sends = [call for call in calls if named(call) == "_send_inline_reply_media"]
    assert sends and all(ast.unparse(call.args[0]) == "media" for call in sends)
    assigned = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.Assign)
        and any(getattr(target, "id", None) == "media" for target in node.targets)
    ]
    assert len(assigned) == 1


def test_the_credit_reply_channels_map_to_the_same_providers() -> None:
    assert dict(webhook._CREDIT_MEDIA_PROVIDERS) == {
        "zapi": webhook._ZAPI_MEDIA_PROVIDER,
        "meta": webhook._META_MEDIA_PROVIDER,
    }


# ---------------------------------------------------------------------------
# Only after the text's 2xx; never a 500 after the text.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_a_text_the_provider_refused_sends_no_file(
    client: Any, world: _World, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    if provider in ("uazapi", "evolution"):
        _force_production(monkeypatch)
    clinic = _clinic(client, world, provider)
    world.http.fail["text"] = 500

    response = _turn(client, clinic)

    assert response.status_code == 200, response.text
    assert response.json()["replied"] is False
    assert response.json()["reason"] == "send_failed"
    assert world.http.of("media") == [] and world.http.of("upload") == []
    assert world.started == [] and world.claims == []


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_a_channel_the_delivery_cannot_bind_is_logged_never_a_500(
    client: Any, world: _World, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    if provider in ("uazapi", "evolution"):
        _force_production(monkeypatch)
    clinic = _clinic(client, world, provider)

    def refuse(*_args: Any) -> Any:
        raise ValueError("reply_media_credentials_incomplete")

    monkeypatch.setattr(webhook, "reply_media_channel", refuse)

    response = _turn(client, clinic)

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "replied": True}
    assert world.http.kinds() == ["text"]
    assert world.started == [] and world.claims == []


# ---------------------------------------------------------------------------
# The fromMe echo of what the delivery sent is the agent's.
# ---------------------------------------------------------------------------


def _paused(session: Session) -> bool:
    return bool(getattr(session, "is_paused", False)) or (
        session.paused_until is not None
    )


@pytest.mark.parametrize("stored_only", [False, True], ids=["in_process", "stored"])
def test_the_echo_of_the_agent_s_audio_file_does_not_pause_zapi(
    client: Any, world: _World, stored_only: bool
) -> None:
    clinic = _clinic(client, world, "zapi", category="audio")

    assert _turn(client, clinic).json() == {"ok": True, "replied": True}
    [media] = world.http.of("media")
    assert media.path.endswith("/send-audio")
    if stored_only:
        webhook._INMEM_SENT.clear()  # another worker: only the stored id
    assert clinic.audio_echo is not None
    echo = clinic.audio_echo(media.mid)

    assert echo.status_code == 200, echo.text
    assert echo.json()["skipped"] == ("ai_echo_db" if stored_only else "ai_echo")
    assert not _paused(_session_row(client, clinic.tenant_id))


@pytest.mark.parametrize("provider", ["zapi", "evolution"])
@pytest.mark.parametrize("stored_only", [False, True], ids=["in_process", "stored"])
def test_the_echo_of_the_failure_notice_does_not_pause(
    client: Any,
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    stored_only: bool,
) -> None:
    """Z-API delivers this echo for real; on Evolution (``emitOwnEvents``
    off, own sends reported as ``send.message`` and dropped) it is
    defensive coverage."""

    if provider == "evolution":
        _force_production(monkeypatch)
    clinic = _clinic(client, world, provider)
    world.http.fail["media"] = 500  # the file fails: the fixed notice goes

    assert _turn(client, clinic).json() == {"ok": True, "replied": True}
    reply, notice = world.http.of("text")
    assert _PT_NOTICE in json.dumps(notice.body, ensure_ascii=False)
    if stored_only:
        webhook._INMEM_SENT.clear()
    echo = clinic.echo(notice.mid, _PT_NOTICE)

    assert echo.status_code == 200, echo.text
    assert echo.json()["skipped"] == ("ai_echo_db" if stored_only else "ai_echo")
    assert not _paused(_session_row(client, clinic.tenant_id))


# ---------------------------------------------------------------------------
# A turn without a file is today's turn.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", _PROVIDERS)
def test_a_reply_without_a_marker_sends_only_the_text(
    client: Any, world: _World, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    if provider in ("uazapi", "evolution"):
        _force_production(monkeypatch)
    clinic = _clinic(client, world, provider)
    world.llm.content = _REPLY  # candidates offered, none chosen

    response = _turn(client, clinic)

    assert response.json() == {"ok": True, "replied": True}
    assert world.http.kinds() == ["text"]
    assert world.started == [] and world.claims == []
    assert world.selected == [provider]


@dataclass(frozen=True)
class _Variant:
    name: str
    response: Any
    credentials: dict[str, Any] = field(default_factory=dict)


def _canned(**extra: Any) -> Any:
    values = {
        "prompt": SimpleNamespace(suggested_response="Olá!", deterministic=False),
        "handoff_summary": None,
        "decision": SimpleNamespace(handoff_required=False),
        "response_language": "pt-BR",
    }
    values.update(extra)
    return SimpleNamespace(**values)


_VARIANTS = (
    _Variant("text", _canned()),
    _Variant(
        "audio_reply",
        _canned(),
        {"tts_enabled": True, "tts_reply_policy": "all_inputs"},
    ),
    _Variant(
        "scheduling_link",
        _canned(
            prompt=SimpleNamespace(
                suggested_response="Agende: https://agenda.example/x",
                deterministic=True,
            )
        ),
        {"tts_enabled": True, "tts_reply_policy": "all_inputs"},
    ),
    _Variant(
        "handoff",
        _canned(
            handoff_summary="Paciente quer falar com a equipe",
            decision=SimpleNamespace(handoff_required=True),
        ),
    ),
)


@pytest.mark.parametrize("variant", _VARIANTS, ids=[v.name for v in _VARIANTS])
@pytest.mark.parametrize("provider", _PROVIDERS)
def test_a_turn_without_a_file_never_starts_a_delivery(
    client: Any,
    world: _World,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    variant: _Variant,
) -> None:
    clinic = _clinic(client, world, provider, extra_credentials=variant.credentials)
    service = MagicMock()
    service._load_clinic_config = AsyncMock(return_value={})
    service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
    service.finish_response = AsyncMock(return_value=variant.response)
    monkeypatch.setattr(webhook, "get_event_service", lambda: service)
    speech = SimpleNamespace(content=b"ID3-voice", mime_type="audio/mpeg")
    monkeypatch.setattr(webhook, "synthesize_speech", AsyncMock(return_value=speech))

    response = _turn(client, clinic)

    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "replied": True}
    assert world.started == [] and world.claims == []
    deterministic = variant.name == "scheduling_link"
    voiced = variant.name == "audio_reply"
    # Exactly today's requests: one reply (text, or the TTS audio).
    expected = ["media"] if voiced else ["text"]
    if voiced and provider == "meta":
        expected = ["upload", "media"]
    assert world.http.kinds() == expected, (provider, variant.name)
    if deterministic:  # the link stays text, whatever the TTS setting
        assert "agenda.example" in json.dumps(world.http.of("text")[0].body)
    # The provider still reached phase 2: an attachment-less answer.
    assert (
        service.finish_response.await_args.kwargs["media_delivery_provider"] == provider
    )


def test_without_the_application_pool_the_turn_is_offered_no_file(
    client: Any, world: _World, monkeypatch: pytest.MonkeyPatch, test_app: Any
) -> None:
    clinic = _clinic(client, world, "zapi")
    monkeypatch.setattr(test_app.state, "media_concurrency", None)

    response = _turn(client, clinic)

    assert response.json() == {"ok": True, "replied": True}
    assert world.service.providers == [None]
    assert world.selected == [] and world.started == []
    assert world.http.kinds() == ["text"]
