"""Group 5: the Z-API and Meta webhooks skip a suspended builder (SQLite).

A builder past its 72 h of debt service answers ``{"ok": true, "skipped":
"credit_suspended"}`` from the adapter's pre-check: no session row, no LGPD
notice, no LLM, no reply to the patient (on Z-API only what already runs
before the pre-check by D4/OWNER-c -- the mark-as-read).  The same overdue
episode changes nothing before ``credit_enforcement_at`` nor with the
switch off: ``main``'s answer and no transition.

debito-cortesia-automatico Group 2: a builder BLOCKED by its courtesy rule
sends its patient nothing on Z-API either -- no audio download, no STT, no
fallback, no read receipt (owner decisions 11 and 16) --, while a suspended
MARKED builder keeps the order above.  Groups 3 and 4: the Uazapi and the
Evolution adapters under the rule (charged, blocked, outside it).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from src.api.routes import webhook
from src.interaction_context import InteractionOrigin
from src.models.clinic_config_table import ClinicConfigTable
from src.models.content_retention import (
    CreditLifecycleEpisode,
    CreditLifecycleTransition,
)
from src.models.credit import (
    CreditAccount,
    CreditEffect,
    CreditEnforcementMark,
    CreditIngressClaim,
    CreditObservationalEffect,
    CreditResponseOutbox,
)
from src.models.event import Event as EventRow
from src.models.handoff import HandoffRecord
from src.models.tenant import Tenant
from src.repositories.credit import CreditRepository
from src.repositories.database import get_db_session
from src.services import credit_metering, egress_policy
from tests.services.test_credit_metering import (
    _BUILDER,
    _LLM_REPLY,
    Providers,
    World,
    _all_rows,
    _audio_body,
    _meta,
    _meta_text,
    _patient_sessions,
    _pause,
    _sends,
    _set_bot_inactive,
    _set_credentials,
    _zapi,
    _zapi_body,
)

pytest_plugins = ["tests.services.test_credit_metering"]

UTC = timezone.utc


async def _suspend(tenant_id: UUID) -> UUID:
    """Plant an overdue episode for the tenant's builder (no capacity)."""

    exhausted = datetime.now(UTC) - timedelta(hours=73)
    database = get_db_session()
    try:
        async with database.begin():
            builder = await database.scalar(
                select(Tenant.builder_id).where(Tenant.id == tenant_id)
            )
            assert builder is not None
            account = await database.scalar(
                select(CreditAccount.id).where(CreditAccount.builder_id == builder)
            )
            if account is None:
                account = uuid4()
                database.add(CreditAccount(id=account, builder_id=builder))
                await database.flush()
            database.add(
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
    finally:
        await database.close()
    return builder


async def _transitions() -> list[str]:
    database = get_db_session()
    try:
        rows = await database.execute(select(CreditLifecycleTransition.transition))
        return [str(value) for (value,) in rows]
    finally:
        await database.close()


def _send(client: Any, world: World, channel: str, phone: str, message_id: str) -> Any:
    if channel == "zapi":
        return _zapi(client, world, _zapi_body(world, message_id, phone=phone))
    return _meta(client, world, _meta_text(message_id, "Olá, quero agendar", phone))


@pytest.mark.parametrize("channel", ["zapi", "meta"])
def test_a_suspended_builder_is_skipped_with_no_patient_message(
    client: Any, enforced: World, providers: Providers, channel: str
) -> None:
    world = enforced
    tenant = world.zapi_tenant if channel == "zapi" else world.meta_tenant
    client._submit(_suspend(tenant))
    phone = "5511900009101"
    response = _send(client, world, channel, phone, f"{channel}-suspended-1")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "skipped": "credit_suspended"}
    sender = "send_text_message" if channel == "zapi" else "send_meta_cloud_text"
    assert _sends(providers, sender, phone) == []
    assert client._submit(_patient_sessions(tenant, phone)) == []
    providers.llm.assert_not_called()
    if channel == "zapi":
        # D4/OWNER-c: Z-API marks the message read BEFORE the pre-check.
        assert [name for name, _ in providers.sent] == ["mark_message_read"]
    else:
        assert providers.sent == []
    assert client._submit(_transitions()) == ["suspended"]


@pytest.mark.parametrize("channel", ["zapi", "meta"])
def test_before_the_enforcement_an_overdue_episode_changes_nothing(
    client: Any, world: World, providers: Providers, force_switch: Any, channel: str
) -> None:
    tenant = world.zapi_tenant if channel == "zapi" else world.meta_tenant
    client._submit(_suspend(tenant))
    response = _send(client, world, channel, "5511900009201", f"{channel}-main-1")
    assert response.status_code == 200
    assert response.json().get("skipped") != "credit_suspended"
    assert response.json().get("replied") is True
    assert client._submit(_transitions()) == []


@pytest.mark.parametrize("channel", ["zapi", "meta"])
def test_with_the_switch_off_an_overdue_episode_changes_nothing(
    client: Any, enforced: World, providers: Providers, force_switch: Any, channel: str
) -> None:
    force_switch(False)
    world = enforced
    tenant = world.zapi_tenant if channel == "zapi" else world.meta_tenant
    client._submit(_suspend(tenant))
    response = _send(client, world, channel, "5511900009301", f"{channel}-off-1")
    assert response.status_code == 200
    assert response.json().get("replied") is True
    assert client._submit(_transitions()) == []


# ---------------------------------------------------------------------------
# debito-cortesia-automatico Group 2: the courtesy rule's block on Z-API and
# Meta, with NO ``credit_enforcement_at`` (the rule alone enforces).  A
# blocked builder is skipped like a suspended one; a charged builder is
# served even with an overdue episode left from a mark era.
# ---------------------------------------------------------------------------


@pytest.fixture
def rule_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    """The process reading at this image's head: the courtesy rule's 046
    gate and the marks' 047 gate open (SQLite has no ``alembic_version``)."""

    from src.services.credit_switch import (
        CreditSwitch,
        CreditSwitchReading,
        load_image_migration_chain,
    )

    reading = CreditSwitchReading(
        True, "schema_ready", load_image_migration_chain()[-1]
    )
    monkeypatch.setattr(CreditSwitch, "reading", lambda _self: reading)


async def _courtesy(tenant_id: UUID, *, keep: bool) -> None:
    """A courtesy grant to the tenant's builder; removed ("Tirar tudo")
    unless ``keep``: blocked, with no paid plan."""

    from src.services import credit_courtesy
    from src.services.credit_ledger import CreditLedger

    database = get_db_session()
    try:
        async with database.begin():
            builder = await database.scalar(
                select(Tenant.builder_id).where(Tenant.id == tenant_id)
            )
            assert builder is not None
            granted = await credit_courtesy.grant(
                database,
                builder_id=builder,
                credits=5,
                reason="courtesy rule",
                actor_user_id=uuid4(),
                idempotency_key=f"rule-{uuid4()}",
                ledger=CreditLedger(database),
            )
        if not keep:
            async with database.begin():
                await credit_courtesy.remove(
                    database,
                    lot_id=granted.lot_id,
                    remove_all=True,
                    reason="courtesy rule",
                    actor_user_id=uuid4(),
                    idempotency_key=f"rule-removal-{uuid4()}",
                    ledger=CreditLedger(database),
                )
    finally:
        await database.close()


@pytest.mark.parametrize("channel", ["zapi", "meta"])
def test_a_blocked_rule_builder_is_skipped_with_no_patient_message(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    channel: str,
) -> None:
    tenant = world.zapi_tenant if channel == "zapi" else world.meta_tenant
    client._submit(_courtesy(tenant, keep=False))
    phone = "5511900009401"
    response = _send(client, world, channel, phone, f"{channel}-rule-blocked-1")
    assert response.status_code == 200
    assert response.json() == {"ok": True, "skipped": "credit_suspended"}
    sender = "send_text_message" if channel == "zapi" else "send_meta_cloud_text"
    assert _sends(providers, sender, phone) == []  # no reply, no LGPD notice
    assert client._submit(_patient_sessions(tenant, phone)) == []
    providers.llm.assert_not_called()
    # Owner decision 16: no read receipt on Z-API for a rule-blocked builder.
    assert providers.sent == []
    assert client._submit(_transitions()) == []  # no episode under the rule


@pytest.mark.parametrize("channel", ["zapi", "meta"])
def test_a_charged_rule_builder_with_a_stale_overdue_episode_is_served(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    channel: str,
) -> None:
    tenant = world.zapi_tenant if channel == "zapi" else world.meta_tenant
    client._submit(_courtesy(tenant, keep=True))
    client._submit(_suspend(tenant))  # left from a mark era, overdue
    response = _send(client, world, channel, "5511900009501", f"{channel}-rule-1")
    assert response.status_code == 200
    assert response.json().get("replied") is True, response.json()
    assert client._submit(_transitions()) == []


@pytest.fixture
def audio_spies(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Counting the Z-API audio download (a stand-in: nothing leaves the
    process) and the STT chain: called through with nothing downloaded (no
    STT configured: "never configured", no provider call), else a stand-in
    failure, so no STT provider is ever called."""

    from src.services.safe_egress import SafeDownload

    calls = {"download": 0, "transcribe": 0}
    chain = webhook._transcribe_audio_chain

    async def download(*_args: Any, **_kwargs: Any) -> Any:
        calls["download"] += 1
        return SafeDownload(content=b"OggS_fake", mime_type="audio/ogg")

    async def transcribe(audio_bytes: Any, *args: Any, **kwargs: Any) -> Any:
        calls["transcribe"] += 1
        if audio_bytes is not None:
            return "", webhook.STT_CHAIN_FAILED
        return await chain(audio_bytes, *args, **kwargs)

    monkeypatch.setattr(webhook, "safe_download_audio", download)
    monkeypatch.setattr(webhook, "_transcribe_audio_chain", transcribe)
    return calls


async def _mark(tenant_id: UUID) -> None:
    """Mark the tenant's builder (cobranca-credito-por-builder, 047)."""

    database = get_db_session()
    try:
        async with database.begin():
            builder = await database.scalar(
                select(Tenant.builder_id).where(Tenant.id == tenant_id)
            )
            assert builder is not None
            database.add(
                CreditEnforcementMark(
                    id=uuid4(),
                    builder_id=builder,
                    kind="mark",
                    occurred_at=datetime.now(UTC) - timedelta(minutes=5),
                    actor_user_id=uuid4(),
                    reason="teste",
                )
            )
    finally:
        await database.close()


async def _paused_captures(tenant_id: UUID) -> list[str]:
    database = get_db_session()
    try:
        rows = (
            await database.execute(
                select(EventRow).where(EventRow.tenant_id == tenant_id)
            )
        ).scalars()
        return [
            str((row.event_data or {}).get("content"))
            for row in rows
            if (row.event_metadata or {}).get("paused_capture")
        ]
    finally:
        await database.close()


@pytest.mark.parametrize("stt", [False, True], ids=["no_stt", "stt"])
def test_a_blocked_rule_builder_zapi_audio_sends_nothing_and_runs_no_stt(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    audio_spies: dict[str, int],
    stt: bool,
) -> None:
    """HIGH-1: the patient audio of a rule-blocked builder is never
    downloaded or transcribed, gets no "Recebi seu áudio!" fallback, no
    unread mark and no read receipt: ``credit_suspended``."""

    tenant = world.zapi_tenant
    client._submit(_courtesy(tenant, keep=False))
    if stt:
        client._submit(_set_credentials(tenant, stt_api_key="sk-rule-blocked"))
    response = _zapi(client, world, _audio_body(world, f"zapi-rule-audio-{stt}", 5))
    assert response.status_code == 200
    assert response.json() == {"ok": True, "skipped": "credit_suspended"}
    assert audio_spies == {"download": 0, "transcribe": 0}
    assert providers.sent == []  # no fallback, no unread mark, no read receipt
    assert client._submit(_patient_sessions(tenant, "5511900000555")) == []
    providers.llm.assert_not_called()


def test_a_blocked_rule_builder_with_a_paused_session_still_captures_the_text(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    audio_spies: dict[str, int],
) -> None:
    """DESIGN item 8 on Z-API: the paused session's text-only capture
    stays, for the clinic's team; an audio is never transcribed for it and
    nothing is sent (no read receipt either)."""

    tenant = world.zapi_tenant
    phone = "5511900000555"  # _audio_body's patient
    client._submit(_courtesy(tenant, keep=False))
    client._submit(_set_credentials(tenant, stt_api_key="sk-rule-blocked"))
    client._submit(_pause(tenant, phone))
    text = _zapi(
        client,
        world,
        _zapi_body(world, "zapi-rule-paused-text", phone=phone, text="Ainda aí?"),
    )
    assert text.status_code == 202
    assert text.json() == {"ok": True, "skipped": "session_paused"}
    audio = _zapi(client, world, _audio_body(world, "zapi-rule-paused-audio", 5))
    assert audio.status_code == 202
    assert audio.json() == {"ok": True, "skipped": "session_paused"}
    assert client._submit(_paused_captures(tenant)) == ["Ainda aí?"]
    assert audio_spies == {"download": 0, "transcribe": 0}
    assert providers.sent == []
    providers.llm.assert_not_called()


def test_a_suspended_marked_builder_keeps_the_fallback_and_the_read_receipt(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    audio_spies: dict[str, int],
) -> None:
    """The control (dominance under a mark): the order before the rule --
    the STT fallback reaches the patient and the message is marked read,
    then the pre-check answers ``credit_suspended`` -- even for a builder
    that also holds a removed courtesy (the mark decides first)."""

    tenant = world.zapi_tenant
    phone = "5511900000555"  # _audio_body's patient
    client._submit(_courtesy(tenant, keep=False))
    client._submit(_mark(tenant))
    client._submit(_suspend(tenant))
    fallback = _zapi(client, world, _audio_body(world, "zapi-marked-audio", 5))
    assert fallback.status_code == 200
    assert fallback.json()["action"] == "stt_fallback", fallback.json()
    assert [name for name, _ in providers.sent] == [
        "send_text_message",
        "mark_conversation_unread",
    ]
    assert audio_spies == {"download": 0, "transcribe": 1}  # no STT configured
    providers.sent.clear()
    text = _zapi(client, world, _zapi_body(world, "zapi-marked-text", phone=phone))
    assert text.status_code == 200
    assert text.json() == {"ok": True, "skipped": "credit_suspended"}
    assert [name for name, _ in providers.sent] == ["mark_message_read"]
    assert client._submit(_patient_sessions(tenant, phone)) == []
    assert client._submit(_transitions()) == ["suspended"]


@pytest.mark.parametrize("mode", ["none", "mark", "global"])
def test_zapi_reads_the_mode_once_and_only_where_it_did(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    audio_spies: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Dominance on Z-API under no mode, a mark and the global: one mode
    read per patient message that reaches the read receipt or the audio
    chain, decided at the pre-check on that capture (no second read), and
    none for a message that leaves before both (a sticker, an empty
    text); the answers are the ones before the rule."""

    from tests.services.test_credit_metering import _enforce_credits

    tenant = world.zapi_tenant
    if mode == "mark":
        client._submit(_mark(tenant))
    elif mode == "global":
        client._submit(_enforce_credits())
    credit_metering.forget_enforcement_seen()
    reads: list[UUID] = []
    original = CreditRepository.tenant_effective_enforcement

    async def counted(self: CreditRepository, tenant_id: UUID) -> Any:
        reads.append(tenant_id)
        return await original(self, tenant_id)

    monkeypatch.setattr(CreditRepository, "tenant_effective_enforcement", counted)
    try:
        sticker = _zapi_body(world, f"zapi-{mode}-sticker", sticker={"url": "x"})
        sticker.pop("text")
        assert _zapi(client, world, sticker).json() == {
            "ok": True,
            "skipped": "non-text",
        }
        empty = _zapi(client, world, _zapi_body(world, f"zapi-{mode}-empty", text=""))
        assert empty.json() == {"ok": True, "skipped": "empty"}
        # LOW-A (Group 2 re-review): the clinic's own messages -- a text and
        # a staff voice note (``fromMe``) -- never reach the capture.
        clinic = _zapi(
            client,
            world,
            _zapi_body(world, f"zapi-{mode}-clinic-text", fromMe=True, text="Oi"),
        )
        assert clinic.status_code == 200, clinic.text
        staff_audio = _audio_body(world, f"zapi-{mode}-staff-audio", 5)
        staff_audio["fromMe"] = True
        staff = _zapi(client, world, staff_audio)
        assert staff.status_code == 200, staff.text
        assert audio_spies == {"download": 0, "transcribe": 0}
        assert reads == []
        served = _zapi(client, world, _zapi_body(world, f"zapi-{mode}-text"))
        assert served.status_code == 200
        assert served.json().get("replied") is True, served.json()
        assert reads == [tenant]
        fallback = _zapi(client, world, _audio_body(world, f"zapi-{mode}-audio", 5))
        assert fallback.json()["action"] == "stt_fallback", fallback.json()
        assert reads == [tenant, tenant]
        assert "mark_message_read" in [name for name, _ in providers.sent]
    finally:
        credit_metering.forget_enforcement_seen()


def test_a_tenant_seen_under_the_rule_whose_read_fails_is_503_at_once_on_zapi(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    audio_spies: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOW-B (Group 2 re-review): the rule's per-tenant D1 on Z-API is
    answered at the capture, at once -- a patient audio of a tenant seen
    under the rule whose read then fails is 503 with no download, no
    transcription and nothing sent (no fallback, no read receipt)."""

    tenant = world.zapi_tenant
    client._submit(_courtesy(tenant, keep=True))
    credit_metering.forget_enforcement_rule_seen()
    try:
        served = _zapi(client, world, _zapi_body(world, "zapi-d1-seen"))
        assert served.json().get("replied") is True, served.json()
        assert credit_metering.enforcement_rule_seen(tenant)
        client._submit(_set_credentials(tenant, stt_api_key="sk-rule-d1"))
        providers.sent.clear()
        providers.llm.reset_mock()

        async def unreadable(_self: CreditRepository, _tenant_id: UUID) -> Any:
            raise RuntimeError("courtesy lots unreadable")

        monkeypatch.setattr(
            CreditRepository, "tenant_effective_enforcement", unreadable
        )
        audio = _zapi(client, world, _audio_body(world, "zapi-d1-audio", 5))
        assert audio.status_code == 503
        assert audio.json() == {"error": "credit_enforcement_unavailable"}
        assert audio_spies == {"download": 0, "transcribe": 0}
        assert providers.sent == []
        providers.llm.assert_not_called()
    finally:
        credit_metering.forget_enforcement_rule_seen()


# ---------------------------------------------------------------------------
# debito-cortesia-automatico Group 3: the Uazapi adapter under the courtesy
# rule, with NO ``credit_enforcement_at`` and no mark.  The world's Uazapi
# tenant is linked to the builder; nothing leaves the process (the sends,
# the media download, the STT chain and the egress question are stand-ins).
# ---------------------------------------------------------------------------

_UAZAPI = "/v1/webhook/uazapi"
_UAZAPI_PHONE = "5511900000301"


@dataclass
class _Uazapi:
    """The stand-ins of the Uazapi route's outbound edges."""

    egress: list[bool]  # the answers of ``uazapi_egress_open``, in order
    egress_reads: int = 0
    downloads: int = 0
    transcriptions: int = 0
    descriptions: int = 0
    transcript: str | None = None


@pytest.fixture
def uazapi(
    client: Any,
    world: World,
    monkeypatch: pytest.MonkeyPatch,
) -> _Uazapi:
    """The Uazapi tenant under the world's builder (the shared world seeds it
    standalone), its egress open, its download, STT and image chain counted;
    a transcription answers ``transcript`` (or fails when ``None``)."""

    client._submit(_link_to_builder(world.uazapi_tenant))
    edges = _Uazapi(egress=[True])

    async def egress_open() -> bool:
        edges.egress_reads += 1
        return edges.egress[-1]

    async def download(*_args: Any, **_kwargs: Any) -> tuple[bytes, str]:
        edges.downloads += 1
        return b"OggS_fake", "audio/ogg"

    async def transcribe(audio_bytes: Any, *_args: Any, **_kwargs: Any) -> Any:
        edges.transcriptions += 1
        if edges.transcript is None:
            return "", webhook.STT_CHAIN_FAILED
        return edges.transcript, None

    async def describe(*_args: Any, **_kwargs: Any) -> tuple[str, Any]:
        edges.descriptions += 1
        return "Paciente enviou uma foto de um exame.", None

    async def egress_gate() -> None:
        from src.services.egress_policy import EgressDisabledError

        if not edges.egress[-1]:
            raise EgressDisabledError("uazapi_egress_disabled")

    monkeypatch.setattr(egress_policy, "uazapi_egress_open", egress_open)
    monkeypatch.setattr(webhook, "require_uazapi_egress", egress_gate)
    # No Redis: the replay claim and the echo store are this process's
    # memory (outside ``is_production``), never a shared server.
    monkeypatch.setattr(webhook, "_get_redis", lambda: None)
    monkeypatch.setattr(webhook, "download_uazapi_media", download)
    monkeypatch.setattr(webhook, "_transcribe_audio_chain", transcribe)
    monkeypatch.setattr(webhook, "_describe_image_chain", describe)
    return edges


async def _link_to_builder(tenant_id: UUID) -> None:
    database = get_db_session()
    try:
        async with database.begin():
            tenant = await database.get(Tenant, tenant_id)
            assert tenant is not None
            tenant.builder_id = _BUILDER
    finally:
        await database.close()


def _uazapi_message(
    world: World,
    message_id: str,
    *,
    phone: str = _UAZAPI_PHONE,
    text: str | None = "Olá, quero agendar",
    from_me: bool = False,
    **message: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "messageid": message_id,
        "type": "text",
        "fromMe": from_me,
        "sender_pn": f"{phone}@s.whatsapp.net",
        **message,
    }
    if text is not None:
        body["text"] = text
    return {
        "instanceName": world.uazapi_instance,
        "message": body,
        "chat": {"wa_chatid": f"{phone}@s.whatsapp.net"},
    }


def _uazapi_audio(
    world: World, message_id: str, *, seconds: int = 5, phone: str = _UAZAPI_PHONE
) -> dict[str, Any]:
    return _uazapi_message(
        world,
        message_id,
        phone=phone,
        text=None,
        type="media",
        messageType="AudioMessage",
        mediaType="ptt",
        content={"seconds": seconds, "mimetype": "audio/ogg; codecs=opus"},
    )


def _uazapi_image(
    world: World, message_id: str, *, phone: str = _UAZAPI_PHONE
) -> dict[str, Any]:
    return _uazapi_message(
        world,
        message_id,
        phone=phone,
        text=None,
        type="media",
        messageType="ImageMessage",
        mediaType="image",
        content={"mimetype": "image/jpeg", "fileLength": 1200},
    )


def _uazapi_document(
    world: World, message_id: str, *, phone: str = _UAZAPI_PHONE
) -> dict[str, Any]:
    return _uazapi_message(
        world,
        message_id,
        phone=phone,
        text=None,
        type="media",
        messageType="DocumentMessage",
        mediaType="document",
        content={"mimetype": "application/pdf", "fileName": "exame.pdf"},
    )


def _uazapi_post(client: Any, body: dict[str, Any]) -> Any:
    return client.post(_UAZAPI, json=body)


def _uazapi_texts(providers: Providers, phone: str = _UAZAPI_PHONE) -> list[str]:
    return _sends(providers, "send_uazapi_text", phone)


def _uazapi_rows(client: Any) -> dict[str, Any]:
    from tests.services.test_credit_metering import _ledger

    claims = _all_rows(client, CreditIngressClaim)
    return {
        "claims": sorted(
            (claim.source, claim.mode, claim.provider_message_id) for claim in claims
        ),
        # The decisions' effects (a grant's and a removal's are the setup's).
        "effects": sorted(
            (effect.effect_kind, effect.effect_id)
            for effect in _all_rows(client, CreditEffect)
            if effect.effect_kind not in ("grant", "courtesy_removal")
        ),
        "ledger": _ledger(client),
        "outbox": [row.state for row in _all_rows(client, CreditResponseOutbox)],
        "observed": sorted(
            (effect.origin, effect.authenticated, effect.would_debit_mc)
            for effect in _all_rows(client, CreditObservationalEffect)
        ),
    }


_COURTESY_GRANT = {"grant": 5_000}


@pytest.mark.parametrize("kind", ["text", "audio", "image", "document"])
def test_a_blocked_rule_builder_uazapi_turn_sends_nothing(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    uazapi: _Uazapi,
    kind: str,
) -> None:
    """DESIGN items 5 and 8: a builder blocked by its courtesy rule answers
    ``credit_suspended`` on Uazapi -- no session, no reply, no LGPD notice,
    no file ack, no LLM; a patient audio is never downloaded nor transcribed
    (no fallback either), an image never described -- and the block is
    decided WITHOUT reading ``uazapi_egress_enabled`` (wish Decision 7).
    The Redis replay claim stays where it is: a redelivery of the text, the
    audio and the document is ``duplicate_message``."""

    tenant = world.uazapi_tenant
    client._submit(_courtesy(tenant, keep=False))
    client._submit(_set_credentials(tenant, stt_api_key="sk-rule-blocked"))
    uazapi.egress = [False]  # closed: still never read for a blocked builder
    body = {
        "text": _uazapi_message,
        "audio": _uazapi_audio,
        "image": _uazapi_image,
        "document": _uazapi_document,
    }[kind](world, f"uazapi-rule-blocked-{kind}")
    response = _uazapi_post(client, body)
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "skipped": "credit_suspended"}
    assert providers.sent == []
    assert client._submit(_patient_sessions(tenant, _UAZAPI_PHONE)) == []
    providers.llm.assert_not_called()
    assert (uazapi.downloads, uazapi.transcriptions, uazapi.descriptions) == (0, 0, 0)
    assert uazapi.egress_reads == 0
    assert _uazapi_rows(client) == {
        "claims": [],
        "effects": [],
        "ledger": {"grant": 5_000, "courtesy_removal": 5_000},
        "outbox": [],
        "observed": [],
    }
    again = _uazapi_post(client, body)
    if kind == "image":
        # An image claims after its download (Decision 9 of the image wish):
        # never reached here, so a redelivery is decided again, as blocked.
        assert again.json() == {"ok": True, "skipped": "credit_suspended"}
    else:
        assert again.json() == {"ok": True, "skipped": "duplicate_message"}
    assert providers.sent == []


def test_a_blocked_rule_builder_with_a_paused_uazapi_session_keeps_the_text(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    uazapi: _Uazapi,
) -> None:
    """DESIGN item 8: the paused session's text-only capture stays for the
    clinic's team; an audio is never transcribed for it and nothing is
    sent."""

    tenant = world.uazapi_tenant
    client._submit(_courtesy(tenant, keep=False))
    client._submit(_set_credentials(tenant, stt_api_key="sk-rule-blocked"))
    client._submit(_pause(tenant, _UAZAPI_PHONE))
    text = _uazapi_post(
        client, _uazapi_message(world, "uazapi-paused-text", text="Ainda aí?")
    )
    assert text.status_code == 202
    assert text.json() == {"ok": True, "skipped": "session_paused"}
    audio = _uazapi_post(client, _uazapi_audio(world, "uazapi-paused-audio"))
    assert audio.status_code == 202
    assert audio.json() == {"ok": True, "skipped": "session_paused"}
    assert client._submit(_paused_captures(tenant)) == ["Ainda aí?"]
    assert (uazapi.downloads, uazapi.transcriptions) == (0, 0)
    assert providers.sent == []
    providers.llm.assert_not_called()


def test_a_charged_rule_builder_uazapi_text_is_charged_once_with_its_echo_known(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    uazapi: _Uazapi,
) -> None:
    """DESIGN item 8: claimed (Redis AND PostgreSQL ``uazapi``), charged one
    trace on the courtesy lot, the reply sent from the outbox by the Uazapi
    branch of ``CreditReplySender`` -- its normalised id stored, so its echo
    is the bot's (``ai_echo``) and never pauses the session -- and a
    redelivery is ``duplicate_message``: no second debit, no second reply."""

    tenant = world.uazapi_tenant
    client._submit(_courtesy(tenant, keep=True))
    body = _uazapi_message(world, "uazapi-rule-charged-1")
    response = _uazapi_post(client, body)
    assert response.json() == {"ok": True, "replied": True}, response.json()
    assert _uazapi_texts(providers) == [_LLM_REPLY]
    assert uazapi.egress_reads == 1
    rows = _uazapi_rows(client)
    assert rows["claims"] == [("uazapi", "enforced", "uazapi-rule-charged-1")]
    assert [kind for kind, _ in rows["effects"]] == ["trace"]
    assert rows["ledger"] == {"grant": 5_000, "debit": 1_000}
    assert rows["outbox"] == ["delivered"] and rows["observed"] == []
    [session] = client._submit(_patient_sessions(tenant, _UAZAPI_PHONE))
    assert [m for m, _ in session.session_metadata["ai_msg_ids"]] == ["uazapi-reply"]
    # The echo of the charged reply is the bot's, never the clinic's: the
    # normalised id is tracked in this worker (``_track_sent_message``) AND
    # stored for every other one (``_store_ai_msg_id_to_db``) -- the echo
    # below reaches a worker that only has the stored id.
    assert "uazapi-reply" in webhook._INMEM_SENT
    webhook._INMEM_SENT.clear()
    echo = _uazapi_message(world, "uazapi-reply", from_me=True, text=_LLM_REPLY)
    assert _uazapi_post(client, echo).json() == {"ok": True, "skipped": "ai_echo_db"}
    [session] = client._submit(_patient_sessions(tenant, _UAZAPI_PHONE))
    assert session.is_paused is False
    again = _uazapi_post(client, body)
    assert again.json() == {"ok": True, "skipped": "duplicate_message"}
    assert _uazapi_rows(client)["ledger"] == {"grant": 5_000, "debit": 1_000}
    assert _uazapi_texts(providers) == [_LLM_REPLY]


def test_with_the_uazapi_egress_closed_a_charged_turn_is_not_charged(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    uazapi: _Uazapi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DESIGN Decision 19: ``uazapi_egress_enabled`` closed -- nothing would be
    delivered -- keeps main's observational path for a charged builder: no
    enforced claim, no debit, main's ``uazapi_egress_disabled`` answer."""

    from src.services.egress_policy import EgressDisabledError

    async def closed(*_args: Any, **_kwargs: Any) -> Any:
        raise EgressDisabledError("uazapi_egress_disabled")

    monkeypatch.setattr(webhook, "send_uazapi_text", closed)
    tenant = world.uazapi_tenant
    client._submit(_courtesy(tenant, keep=True))
    uazapi.egress = [False]
    response = _uazapi_post(client, _uazapi_message(world, "uazapi-rule-closed-1"))
    assert response.json() == {
        "ok": True,
        "replied": False,
        "reason": "uazapi_egress_disabled",
    }
    assert uazapi.egress_reads == 1
    rows = _uazapi_rows(client)
    assert rows["claims"] == [("uazapi", "observational", "uazapi-rule-closed-1")]
    assert rows["effects"] == [] and rows["outbox"] == []
    assert rows["ledger"] == _COURTESY_GRANT  # nothing charged
    assert rows["observed"] == [("unknown", False, 0)]  # main's observation


def test_a_charged_uazapi_reply_refused_by_a_closing_egress_is_main_s_answer(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    uazapi: _Uazapi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The egress closes between the pre-check and the send: the charged
    decision stands (one trace, like any lost reply, OD-1), its outbox row is
    ``failed`` with nothing sent, and the answer is main's
    ``uazapi_egress_disabled``."""

    from src.services.egress_policy import EgressDisabledError

    async def closed(*_args: Any, **_kwargs: Any) -> Any:
        raise EgressDisabledError("uazapi_egress_disabled")

    monkeypatch.setattr(webhook, "send_uazapi_text", closed)
    client._submit(_courtesy(world.uazapi_tenant, keep=True))
    response = _uazapi_post(client, _uazapi_message(world, "uazapi-rule-closing-1"))
    assert response.json() == {
        "ok": True,
        "replied": False,
        "reason": "uazapi_egress_disabled",
    }
    rows = _uazapi_rows(client)
    assert rows["claims"] == [("uazapi", "enforced", "uazapi-rule-closing-1")]
    assert rows["ledger"] == {"grant": 5_000, "debit": 1_000}
    assert rows["outbox"] == ["failed"]


def test_a_charged_uazapi_audio_is_transcribed_then_charged_as_an_audio_turn(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    uazapi: _Uazapi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STT/audio: downloaded, claimed after its download (Redis), transcribed,
    then the enforced decision -- the PostgreSQL claim, one trace, the reply
    stored with ``input_was_audio`` (the TTS policy's input)."""

    seen: list[Any] = []
    original = webhook._credit_reply_payload

    def spy(target: Any, *args: Any) -> Any:
        seen.append(target)
        return original(target, *args)

    monkeypatch.setattr(webhook, "_credit_reply_payload", spy)
    tenant = world.uazapi_tenant
    client._submit(_courtesy(tenant, keep=True))
    client._submit(_set_credentials(tenant, stt_api_key="sk-rule-charged"))
    uazapi.transcript = "Quero marcar uma consulta"
    body = _uazapi_audio(world, "uazapi-rule-audio-1")
    response = _uazapi_post(client, body)
    assert response.json() == {"ok": True, "replied": True}, response.json()
    assert (uazapi.downloads, uazapi.transcriptions) == (1, 1)
    assert [target.channel for target in seen] == ["uazapi"]
    assert seen[0].input_was_audio is True and seen[0].phone == _UAZAPI_PHONE
    rows = _uazapi_rows(client)
    assert rows["claims"] == [("uazapi", "enforced", "uazapi-rule-audio-1")]
    assert rows["ledger"] == {"grant": 5_000, "debit": 1_000}
    from tests.services.test_credit_metering import _message_in_contents

    assert _message_in_contents(client) == ["Quero marcar uma consulta"]
    assert _uazapi_post(client, body).json() == {
        "ok": True,
        "skipped": "duplicate_message",
    }
    # Main's order: a patient audio claims after its download, so the
    # redelivery downloads again and stops at the Redis claim, untranscribed.
    assert (uazapi.downloads, uazapi.transcriptions) == (2, 1)
    assert _uazapi_rows(client)["ledger"] == {"grant": 5_000, "debit": 1_000}


def test_a_charged_uazapi_audio_whose_stt_fails_gets_the_free_fallback(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    uazapi: _Uazapi,
) -> None:
    """As on Z-API: a failed transcription of a charged builder sends the
    fixed fallback (no decision: nothing claimed in PostgreSQL, nothing
    charged)."""

    tenant = world.uazapi_tenant
    client._submit(_courtesy(tenant, keep=True))
    client._submit(_set_credentials(tenant, stt_api_key="sk-rule-charged"))
    response = _uazapi_post(client, _uazapi_audio(world, "uazapi-rule-fallback-1"))
    assert response.json()["action"] == "stt_fallback", response.json()
    assert len(_uazapi_texts(providers)) == 1
    rows = _uazapi_rows(client)
    assert rows["claims"] == [] and rows["ledger"] == _COURTESY_GRANT


def test_a_charged_uazapi_long_audio_hands_off_in_one_charged_transaction(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    uazapi: _Uazapi,
    announced: list[UUID],
    file_handoff_outcomes: list[tuple[Any, Any]],
) -> None:
    """Long audio: never downloaded nor transcribed; the handoff, its
    ``uazapi`` claim and its one trace commit together (Meta's O2), the
    notice goes out after; the session is paused by the stand-by policy."""

    tenant = world.uazapi_tenant
    client._submit(_courtesy(tenant, keep=True))
    response = _uazapi_post(
        client, _uazapi_audio(world, "uazapi-rule-long-1", seconds=600)
    )
    assert response.json() == {"ok": True, "skipped": "audio_too_long"}
    [(enforcement, outcome)] = file_handoff_outcomes
    assert (enforcement.source, enforcement.provider_message_id) == (
        "uazapi",
        "uazapi-rule-long-1",
    )
    assert enforcement.origin is InteractionOrigin.UNKNOWN
    assert outcome.answer is None and outcome.handoff is not None
    [record] = _all_rows(client, HandoffRecord)
    rows = _uazapi_rows(client)
    assert rows["claims"] == [("uazapi", "enforced", "uazapi-rule-long-1")]
    assert record.context["credit_effect_id"] == rows["effects"][0][1]
    assert rows["ledger"] == {"grant": 5_000, "debit": 1_000}
    assert rows["observed"] == []  # the observational writer stood down
    assert announced == [record.id]
    assert _uazapi_texts(providers)[-1] == webhook._AUDIO_TOO_LONG_MSG_PT
    assert (uazapi.downloads, uazapi.transcriptions) == (0, 0)
    [session] = client._submit(_patient_sessions(tenant, _UAZAPI_PHONE))
    assert session.is_paused is True


def test_a_charged_uazapi_file_forward_is_one_charged_handoff_and_the_ack_free(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    uazapi: _Uazapi,
    announced: list[UUID],
    file_handoff_outcomes: list[tuple[Any, Any]],
) -> None:
    """File: the acknowledgement of a document is free (no claim, no
    debit); the patient's forward request is a charged handoff."""

    tenant = world.uazapi_tenant
    client._submit(_courtesy(tenant, keep=True))
    ack = _uazapi_post(client, _uazapi_document(world, "uazapi-rule-doc-1"))
    assert ack.json() == {"ok": True, "replied": True, "action": "file_ack"}
    assert _uazapi_rows(client)["ledger"] == _COURTESY_GRANT
    assert _uazapi_rows(client)["claims"] == []
    forward = _uazapi_post(
        client,
        _uazapi_message(world, "uazapi-rule-forward-1", text="Pode encaminhar"),
    )
    assert forward.json() == {"ok": True, "replied": True, "action": "file_handoff"}
    [(enforcement, outcome)] = file_handoff_outcomes
    assert enforcement.source == "uazapi" and outcome.handoff is not None
    rows = _uazapi_rows(client)
    assert rows["claims"] == [("uazapi", "enforced", "uazapi-rule-forward-1")]
    assert rows["ledger"] == {"grant": 5_000, "debit": 1_000}
    assert _uazapi_texts(providers)[-1] == webhook._FILE_FORWARDED_MSG_PT


def test_a_charged_uazapi_image_is_described_then_charged_as_a_turn(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    uazapi: _Uazapi,
) -> None:
    """Image: claimed after its download (Redis), described, then charged
    like any turn (one trace); the LGPD notice of a new session is free."""

    from tests.services.test_credit_metering import _OPENROUTER_BUNDLE

    tenant = world.uazapi_tenant
    client._submit(_courtesy(tenant, keep=True))
    client._submit(_set_credentials(tenant, **_OPENROUTER_BUNDLE))
    body = _uazapi_image(world, "uazapi-rule-image-1")
    response = _uazapi_post(client, body)
    assert response.json() == {"ok": True, "replied": True}, response.json()
    assert (uazapi.downloads, uazapi.descriptions) == (1, 1)
    rows = _uazapi_rows(client)
    assert rows["claims"] == [("uazapi", "enforced", "uazapi-rule-image-1")]
    assert rows["ledger"] == {"grant": 5_000, "debit": 1_000}
    assert _uazapi_post(client, body).json() == {
        "ok": True,
        "skipped": "duplicate_message",
    }
    assert rows == _uazapi_rows(client)


def test_a_charged_uazapi_new_session_gets_its_lgpd_notice_free(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    uazapi: _Uazapi,
) -> None:
    """LGPD: a new session's notice goes out first, free; then the charged
    reply -- one trace in all."""

    tenant = world.uazapi_tenant
    client._submit(_courtesy(tenant, keep=True))
    client._submit(_lgpd_on(tenant))
    response = _uazapi_post(client, _uazapi_message(world, "uazapi-rule-lgpd-1"))
    assert response.json() == {"ok": True, "replied": True}, response.json()
    texts = _uazapi_texts(providers)
    assert len(texts) == 2 and texts[-1] == _LLM_REPLY
    assert _uazapi_rows(client)["ledger"] == {"grant": 5_000, "debit": 1_000}


async def _lgpd_on(tenant_id: UUID) -> None:
    from src.services.event_service import invalidate_clinic_config_cache

    database = get_db_session()
    try:
        async with database.begin():
            row = (
                await database.execute(
                    select(ClinicConfigTable).where(
                        ClinicConfigTable.tenant_id == tenant_id
                    )
                )
            ).scalar_one()
            row.lgpd = {"lgpd_enabled": True}
    finally:
        await database.close()
    await invalidate_clinic_config_cache(tenant_id)


@pytest.mark.parametrize("verified", [False, True], ids=["unverified", "verified"])
def test_a_charged_uazapi_turn_digests_its_real_origin(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    uazapi: _Uazapi,
    monkeypatch: pytest.MonkeyPatch,
    verified: bool,
) -> None:
    """DESIGN item 10: an unverified Uazapi turn is charged as ``unknown``,
    a verified one (the envelope token matched, auth enforced) as
    ``production`` -- in every ``decision_digest`` of the decision."""

    digests: list[str] = []
    original = credit_metering.decision_digest

    def spy(**kwargs: Any) -> str:
        digests.append(kwargs["origin"].value)
        return original(**kwargs)

    monkeypatch.setattr(credit_metering, "decision_digest", spy)
    body = _uazapi_message(world, f"uazapi-rule-origin-{verified}")
    if verified:

        async def enforced() -> bool:
            return True

        monkeypatch.setattr(webhook, "uazapi_webhook_auth_enforced", enforced)
        body["token"] = "uazapi-outbound-token"  # the stored instance token
    client._submit(_courtesy(world.uazapi_tenant, keep=True))
    response = _uazapi_post(client, body)
    assert response.json() == {"ok": True, "replied": True}, response.json()
    assert digests == ["production" if verified else "unknown"]
    assert _uazapi_rows(client)["ledger"] == {"grant": 5_000, "debit": 1_000}


@pytest.mark.parametrize("mode", ["none", "mark", "global", "protected"])
@pytest.mark.parametrize("verified", [False, True], ids=["unverified", "verified"])
def test_uazapi_outside_the_rule_reads_the_mode_once_and_stays_observational(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    uazapi: _Uazapi,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    verified: bool,
) -> None:
    """Dominance (DESIGN Decision 18, wish Decision 8): under no mode, a mark,
    the global or a protected courtesy (spent, a paid plan in force), a
    Uazapi turn -- verified or not -- is main's observational turn: no
    enforced claim, no debit, no egress question, the answer and the
    observation main writes; its ONE new statement is the mode read, read
    exactly once per turn -- a message that leaves at the pause or the
    AI-off gate included (read before them, decided at the pre-check)."""

    from tests.services.test_credit_metering import _enforce_credits

    tenant = world.uazapi_tenant
    if mode == "mark":
        client._submit(_mark(tenant))
    elif mode == "global":
        client._submit(_enforce_credits())
    elif mode == "protected":
        client._submit(_courtesy(tenant, keep=False))
        client._submit(_paid_plan(tenant))
    if verified:

        async def enforced() -> bool:
            return True

        monkeypatch.setattr(webhook, "uazapi_webhook_auth_enforced", enforced)
    credit_metering.forget_enforcement_seen()
    reads: list[UUID] = []
    original = CreditRepository.tenant_effective_enforcement

    async def counted(self: CreditRepository, tenant_id: UUID) -> Any:
        reads.append(tenant_id)
        return await original(self, tenant_id)

    monkeypatch.setattr(CreditRepository, "tenant_effective_enforcement", counted)
    # NIT-1 (Group 3 re-check): ``precheck_uazapi`` decides through
    # ``decide_webhook`` on its capture (never ``precheck_webhook``), so the
    # decision -- the only place a lock, a claim or a block could start -- is
    # spied there.
    prechecks: list[Any] = []
    real_decide = credit_metering.decide_webhook

    async def counted_decide(db: Any, capture: Any) -> Any:
        prechecks.append(capture.source)
        return await real_decide(db, capture)

    monkeypatch.setattr(credit_metering, "decide_webhook", counted_decide)
    try:
        body = _uazapi_message(world, f"uazapi-{mode}-{verified}-text")
        if verified:
            body["token"] = "uazapi-outbound-token"
        served = _uazapi_post(client, body)
        assert served.json() == {"ok": True, "replied": True}, served.json()
        assert reads == [tenant]
        client._submit(_pause(tenant, "5511900000399"))
        paused = _uazapi_message(
            world, f"uazapi-{mode}-{verified}-paused", phone="5511900000399"
        )
        if verified:
            paused["token"] = "uazapi-outbound-token"
        assert _uazapi_post(client, paused).json() == {
            "ok": True,
            "skipped": "session_paused",
        }
        assert reads == [tenant, tenant]  # read once, before the pause gate
        client._submit(_set_bot_inactive(tenant))
        off = _uazapi_message(world, f"uazapi-{mode}-{verified}-off")
        if verified:
            off["token"] = "uazapi-outbound-token"
        assert _uazapi_post(client, off).json() == {
            "ok": True,
            "skipped": "bot_paused",
        }
        assert reads == [tenant, tenant, tenant]  # once, before the AI-off gate
        assert prechecks == [] and uazapi.egress_reads == 0
        rows = _uazapi_rows(client)
        assert [claim for claim in rows["claims"] if claim[1] == "enforced"] == []
        assert [kind for kind, _ in rows["effects"]] == []
        assert "debit" not in rows["ledger"] and rows["outbox"] == []
        if mode == "global":
            assert rows["observed"] == []  # the writer stands down, as before
        else:
            assert rows["observed"] == [
                ("production", True, 1_000) if verified else ("unknown", False, 0)
            ]
    finally:
        credit_metering.forget_enforcement_seen()


@pytest.mark.parametrize("mode", ["none", "mark", "global", "protected"])
def test_a_failed_uazapi_mode_read_outside_the_rule_keeps_the_lazy_unpause(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    uazapi: _Uazapi,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """MEDIUM-1 (Group 3 fix loop): a builder NOT under its courtesy rule,
    a session whose pause just expired and the mode read failing with no D1
    latch -- the read rolls the session back, so it runs BEFORE the pause
    gate: the gate's lazy unpause is not undone, the patient is answered and
    the session ends unpaused (as on main)."""

    from tests.services.test_credit_metering import _enforce_credits

    tenant = world.uazapi_tenant
    if mode == "mark":
        client._submit(_mark(tenant))
    elif mode == "global":
        client._submit(_enforce_credits())
    elif mode == "protected":
        client._submit(_courtesy(tenant, keep=False))
        client._submit(_paid_plan(tenant))
    client._submit(_expired_pause(tenant, _UAZAPI_PHONE))
    credit_metering.forget_enforcement_seen()
    reads: list[UUID] = []

    async def failing(self: CreditRepository, tenant_id: UUID) -> Any:
        reads.append(tenant_id)
        raise RuntimeError("mode read down")

    monkeypatch.setattr(CreditRepository, "tenant_effective_enforcement", failing)
    try:
        body = _uazapi_message(world, f"uazapi-{mode}-expired-pause")
        response = _uazapi_post(client, body)
        assert response.json() == {"ok": True, "replied": True}, response.json()
        assert reads == [tenant]
        assert _uazapi_texts(providers) == [_LLM_REPLY]
        [session] = client._submit(_patient_sessions(tenant, _UAZAPI_PHONE))
        assert session.is_paused is False
        assert session.paused_until is None
    finally:
        credit_metering.forget_enforcement_seen()


async def _expired_pause(tenant_id: UUID, phone: str) -> None:
    """A paused WhatsApp session whose ``paused_until`` already passed."""

    from tests.services.test_credit_metering import _session_row

    database = get_db_session()
    try:
        async with database.begin():
            database.add(
                _session_row(
                    uuid4(),
                    tenant_id,
                    channel="whatsapp",
                    user_id=phone,
                    is_paused=True,
                    paused_until=datetime.now(UTC) - timedelta(minutes=1),
                )
            )
    finally:
        await database.close()


async def _paid_plan(tenant_id: UUID) -> None:
    """A top-up lot in force for the tenant's builder: a paid plan in force
    (wish Decision 5), so a spent courtesy is ``protected``."""

    from tests.services.test_credit_lifecycle import _grant_topup

    database = get_db_session()
    try:
        builder = await database.scalar(
            select(Tenant.builder_id).where(Tenant.id == tenant_id)
        )
    finally:
        await database.close()
    assert builder is not None
    await _grant_topup(builder, 10_000, datetime.now(UTC))


def test_a_uazapi_rule_change_between_capture_and_lock_answers_503(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    uazapi: _Uazapi,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DESIGN item 11 on Uazapi: the courtesy removed between the capture and
    the account lock -- 503 ``credit_enforcement_unavailable`` before any
    write (no session, no claim, no debit, nothing sent); the message is
    lost like any webhook 503 (its Redis claim is spent)."""

    tenant = world.uazapi_tenant
    lot = client._submit(_courtesy_lot(tenant))
    real = credit_metering.capture_uazapi
    real_decide = credit_metering.decide_webhook

    async def then_remove(db: Any, capture: Any) -> Any:
        await _remove(lot)
        return await real_decide(db, capture)

    monkeypatch.setattr(credit_metering, "decide_webhook", then_remove)
    assert real is credit_metering.capture_uazapi
    body = _uazapi_message(world, "uazapi-rule-race-1")
    response = _uazapi_post(client, body)
    assert response.status_code == 503
    assert response.json() == {"error": "credit_enforcement_unavailable"}
    assert providers.sent == []
    assert client._submit(_patient_sessions(tenant, _UAZAPI_PHONE)) == []
    rows = _uazapi_rows(client)
    assert rows["claims"] == [] and rows["outbox"] == []
    assert rows["ledger"] == {"grant": 5_000, "courtesy_removal": 5_000}
    lost = _uazapi_post(client, body)
    assert lost.json() == {"ok": True, "skipped": "duplicate_message"}


async def _courtesy_lot(tenant_id: UUID) -> UUID:
    from src.services import credit_courtesy
    from src.services.credit_ledger import CreditLedger

    database = get_db_session()
    try:
        async with database.begin():
            builder = await database.scalar(
                select(Tenant.builder_id).where(Tenant.id == tenant_id)
            )
            assert builder is not None
            granted = await credit_courtesy.grant(
                database,
                builder_id=builder,
                credits=5,
                reason="courtesy rule",
                actor_user_id=uuid4(),
                idempotency_key=f"rule-{uuid4()}",
                ledger=CreditLedger(database),
            )
    finally:
        await database.close()
    return UUID(str(granted.lot_id))


async def _remove(lot_id: UUID) -> None:
    from src.services import credit_courtesy
    from src.services.credit_ledger import CreditLedger

    database = get_db_session()
    try:
        async with database.begin():
            await credit_courtesy.remove(
                database,
                lot_id=lot_id,
                remove_all=True,
                reason="courtesy rule",
                actor_user_id=uuid4(),
                idempotency_key=f"rule-removal-{uuid4()}",
                ledger=CreditLedger(database),
            )
    finally:
        await database.close()


@pytest.mark.parametrize("kind", ["text", "audio"])
def test_a_tenant_seen_under_the_rule_whose_rule_fails_is_503_on_uazapi(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    uazapi: _Uazapi,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    """The rule's per-tenant D1 on Uazapi: once seen under the rule, a failed
    read answers 503 at once -- for an audio before its download, STT or any
    send (and before its own Redis claim: the provider's retry is served)."""

    tenant = world.uazapi_tenant
    client._submit(_courtesy(tenant, keep=True))
    credit_metering.forget_enforcement_rule_seen()
    try:
        first = _uazapi_post(client, _uazapi_message(world, f"uazapi-d1-seen-{kind}"))
        assert first.json() == {"ok": True, "replied": True}, first.json()
        assert credit_metering.enforcement_rule_seen(tenant)
        providers.sent.clear()
        client._submit(_set_credentials(tenant, stt_api_key="sk-rule-d1"))

        async def unreadable(_self: CreditRepository, _tenant_id: UUID) -> Any:
            raise RuntimeError("courtesy lots unreadable")

        body = (
            _uazapi_message(world, "uazapi-d1-text", phone="5511900000398")
            if kind == "text"
            else _uazapi_audio(world, "uazapi-d1-audio", phone="5511900000398")
        )
        with monkeypatch.context() as patch:
            patch.setattr(CreditRepository, "tenant_effective_enforcement", unreadable)
            refused = _uazapi_post(client, body)
        assert refused.status_code == 503
        assert refused.json() == {"error": "credit_enforcement_unavailable"}
        assert providers.sent == []
        assert (uazapi.downloads, uazapi.transcriptions) == (0, 0)
        assert client._submit(_patient_sessions(tenant, "5511900000398")) == []
        retried = _uazapi_post(client, body)
        if kind == "audio":
            # Refused before the audio's own claim: the retry is served.
            assert retried.json()["action"] == "stt_fallback", retried.json()
        else:
            assert retried.json() == {"ok": True, "skipped": "duplicate_message"}
    finally:
        credit_metering.forget_enforcement_rule_seen()


def test_a_charged_uazapi_ai_handoff_is_one_trace_charged_in_phase1(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    uazapi: _Uazapi,
    announced: list[UUID],
) -> None:
    """The pipeline's own handoff (phase 1) on Uazapi under the rule: one
    trace, debited from the courtesy lot in phase 1 and linked to the handoff
    record; the reply goes out once, with no unread mark (Uazapi has none)."""

    from tests.services.test_credit_metering import _HANDOFF_TEXT

    tenant = world.uazapi_tenant
    client._submit(_courtesy(tenant, keep=True))
    body = _uazapi_message(world, "uazapi-rule-handoff-1", text=_HANDOFF_TEXT)
    response = _uazapi_post(client, body)
    assert response.json() == {"ok": True, "replied": True}, response.json()
    rows = _uazapi_rows(client)
    assert rows["claims"] == [("uazapi", "enforced", "uazapi-rule-handoff-1")]
    [(kind, effect_id)] = rows["effects"]
    assert kind == "trace" and rows["ledger"] == {"grant": 5_000, "debit": 1_000}
    [record] = _all_rows(client, HandoffRecord)
    assert record.context["credit_effect_id"] == effect_id
    assert announced == [record.id]
    assert len(_uazapi_texts(providers)) == 1
    assert "mark_conversation_unread" not in [name for name, _ in providers.sent]
    [outbox] = _all_rows(client, CreditResponseOutbox)
    assert outbox.state == "delivered" and outbox.payload["handoff"] is True
    assert outbox.payload["channel"] == "uazapi"


# ---------------------------------------------------------------------------
# debito-cortesia-automatico Group 4: the Evolution adapter under the courtesy
# rule, with NO ``credit_enforcement_at`` and no mark.  The world's Evolution
# tenant belongs to the builder; every Evolution callback is ``unknown`` (no
# origin proof) and Evolution has no egress flag.  Nothing leaves the
# process (the sends are stand-ins, the replay store is this process's
# memory).
# ---------------------------------------------------------------------------

_EVOLUTION = "/v1/webhook/evolution"
_EVOLUTION_PHONE = "5511900000401"
# The id the stand-in ``send_evolution_text`` answers (``key.id``).
_EVOLUTION_REPLY_ID = "evolution-reply"


@dataclass
class _Evolution:
    """The mode reads of the Evolution route (``tenant_effective_enforcement``)
    and the egress question, which Evolution must never ask."""

    reads: list[UUID]
    egress_reads: int = 0


@pytest.fixture
def evolution(client: Any, world: World, monkeypatch: pytest.MonkeyPatch) -> _Evolution:
    edges = _Evolution(reads=[])
    original = CreditRepository.tenant_effective_enforcement

    async def counted(self: CreditRepository, tenant_id: UUID) -> Any:
        edges.reads.append(tenant_id)
        return await original(self, tenant_id)

    async def egress_open() -> bool:
        edges.egress_reads += 1
        return True

    monkeypatch.setattr(CreditRepository, "tenant_effective_enforcement", counted)
    monkeypatch.setattr(egress_policy, "uazapi_egress_open", egress_open)
    # No Redis: the replay claim and the echo store are this process's
    # memory (outside ``is_production``), never a shared server.
    monkeypatch.setattr(webhook, "_get_redis", lambda: None)
    credit_metering.forget_enforcement_rule_seen()
    yield edges
    credit_metering.forget_enforcement_rule_seen()


def _evolution_body(
    world: World,
    message_id: str,
    *,
    phone: str = _EVOLUTION_PHONE,
    message: Any = None,
    from_me: bool = False,
    event: str = "messages.upsert",
    **data: Any,
) -> dict[str, Any]:
    return {
        "event": event,
        "instance": world.evolution_instance,
        "data": {
            "key": {
                "remoteJid": f"{phone}@s.whatsapp.net",
                "fromMe": from_me,
                "id": message_id,
            },
            "message": (
                {"conversation": "Olá, quero agendar"} if message is None else message
            ),
            "pushName": "Paciente",
            **data,
        },
    }


def _evolution_post(client: Any, body: dict[str, Any]) -> Any:
    return client.post(_EVOLUTION, json=body)


def _evolution_texts(providers: Providers, phone: str = _EVOLUTION_PHONE) -> list[str]:
    """The texts ``send_evolution_text(url, name, key, phone, text)`` sent."""

    return [
        args[4]
        for sent, args in providers.sent
        if sent == "send_evolution_text" and args[3] == phone
    ]


_EVOLUTION_NO_ROWS = {
    "claims": [],
    "effects": [],
    "ledger": {"grant": 5_000, "courtesy_removal": 5_000},
    "outbox": [],
    "observed": [],
}

# Every Evolution message type a patient can send, as ``data.message`` (or the
# event itself): the two text shapes the route takes, and every other one --
# answered by main's early exits before anything is read, downloaded or sent.
_EVOLUTION_KINDS: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {
    "text": ({"message": {"conversation": "Olá, quero agendar"}}, {}),
    "extended_text": (
        {"message": {"extendedTextMessage": {"text": "Olá, vi o link"}}},
        {},
    ),
    "audio": (
        {
            "message": {
                "audioMessage": {
                    "url": "https://mmg.invalid/a.enc",
                    "mimetype": "audio/ogg; codecs=opus",
                    "seconds": 7,
                    "ptt": True,
                }
            },
            "messageType": "audioMessage",
        },
        {"ok": True, "skipped": "non-text"},
    ),
    "image": (
        {
            "message": {
                "imageMessage": {
                    "url": "https://mmg.invalid/i.enc",
                    "mimetype": "image/jpeg",
                    "caption": "meu exame",
                }
            },
            "messageType": "imageMessage",
        },
        {"ok": True, "skipped": "non-text"},
    ),
    "document": (
        {
            "message": {
                "documentMessage": {
                    "fileName": "exame.pdf",
                    "mimetype": "application/pdf",
                }
            },
            "messageType": "documentMessage",
        },
        {"ok": True, "skipped": "non-text"},
    ),
    "video": (
        {"message": {"videoMessage": {"mimetype": "video/mp4", "caption": "olha"}}},
        {"ok": True, "skipped": "non-text"},
    ),
    "sticker": (
        {"message": {"stickerMessage": {"mimetype": "image/webp"}}},
        {"ok": True, "skipped": "non-text"},
    ),
    "reaction": (
        {"message": {"reactionMessage": {"key": {"id": "X"}, "text": "👍"}}},
        {"ok": True, "skipped": "non-text"},
    ),
    "location": (
        {
            "message": {
                "locationMessage": {
                    "degreesLatitude": -23.55,
                    "degreesLongitude": -46.63,
                }
            }
        },
        {"ok": True, "skipped": "non-text"},
    ),
    "contact": (
        {
            "message": {
                "contactMessage": {"displayName": "Fulano", "vcard": "BEGIN:VCARD"}
            }
        },
        {"ok": True, "skipped": "non-text"},
    ),
    "button_reply": (
        {
            "message": {
                "buttonsResponseMessage": {
                    "selectedButtonId": "1",
                    "selectedDisplayText": "Sim",
                }
            }
        },
        {"ok": True, "skipped": "non-text"},
    ),
    "list_reply": (
        {
            "message": {
                "listResponseMessage": {
                    "title": "Consulta",
                    "singleSelectReply": {"selectedRowId": "r1"},
                }
            }
        },
        {"ok": True, "skipped": "non-text"},
    ),
    "edited": (
        {
            "message": {
                "editedMessage": {
                    "message": {
                        "protocolMessage": {
                            "type": "MESSAGE_EDIT",
                            "editedMessage": {"conversation": "texto editado"},
                        }
                    }
                }
            }
        },
        {"ok": True, "skipped": "non-text"},
    ),
    "deleted": (
        {"message": {"protocolMessage": {"type": "REVOKE", "key": {"id": "X"}}}},
        {"ok": True, "skipped": "non-text"},
    ),
    "call": ({"event": "call"}, {"ok": True, "skipped": "non_message_event"}),
}


@pytest.mark.parametrize("kind", sorted(_EVOLUTION_KINDS))
def test_a_blocked_rule_builder_evolution_message_sends_nothing(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    evolution: _Evolution,
    kind: str,
) -> None:
    """DESIGN items 5 and 9, owner decision 11: a builder blocked by its
    courtesy rule sends its Evolution patient nothing, whatever the message
    type.  A text (either shape) is captured once and answered
    ``credit_suspended`` at the pre-check -- no session, no reply, no LGPD
    notice, no LLM --; every other type leaves at main's early exits
    (``non-text``, ``non_message_event``) before any read, download, STT,
    description or send (the Evolution route takes text only).  The Redis
    replay claim stays where it is: a redelivered text is
    ``duplicate_message``.  No egress flag is ever read."""

    tenant = world.evolution_tenant
    client._submit(_courtesy(tenant, keep=False))
    shape, skipped = _EVOLUTION_KINDS[kind]
    body = _evolution_body(world, f"evolution-rule-blocked-{kind}")
    if "event" in shape:
        body["event"] = shape["event"]
    else:
        body["data"].update(shape)
    response = _evolution_post(client, body)
    assert response.status_code == 200, response.text
    if skipped:
        assert response.json() == skipped
        assert evolution.reads == []  # nothing captured before main's exit
    else:
        assert response.json() == {"ok": True, "skipped": "credit_suspended"}
        assert evolution.reads == [tenant]  # once
    assert providers.sent == []
    assert client._submit(_patient_sessions(tenant, _EVOLUTION_PHONE)) == []
    providers.llm.assert_not_called()
    assert evolution.egress_reads == 0
    assert _uazapi_rows(client) == _EVOLUTION_NO_ROWS
    again = _evolution_post(client, body)
    if skipped:
        assert again.json() == skipped
    else:
        assert again.json() == {"ok": True, "skipped": "duplicate_message"}
    assert providers.sent == []


def test_a_blocked_rule_builder_with_a_paused_evolution_session_keeps_the_text(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    evolution: _Evolution,
) -> None:
    """DESIGN items 5 and 9: the paused session's text-only capture stays for
    the clinic's team (the capture runs before the pause gate, the decision
    after it), and nothing is sent; the AI-off gate also answers before the
    block, as on main."""

    tenant = world.evolution_tenant
    client._submit(_courtesy(tenant, keep=False))
    client._submit(_pause(tenant, _EVOLUTION_PHONE))
    paused = _evolution_post(
        client,
        _evolution_body(
            world, "evolution-paused-text", message={"conversation": "Ainda aí?"}
        ),
    )
    assert paused.status_code == 202
    assert paused.json() == {"ok": True, "skipped": "session_paused"}
    assert client._submit(_paused_captures(tenant)) == ["Ainda aí?"]
    client._submit(_set_bot_inactive(tenant))
    off = _evolution_post(
        client, _evolution_body(world, "evolution-off", phone="5511900000498")
    )
    assert off.status_code == 202
    assert off.json() == {"ok": True, "skipped": "bot_paused"}
    assert evolution.reads == [tenant, tenant]  # once per message
    assert providers.sent == []
    providers.llm.assert_not_called()
    assert _uazapi_rows(client) == _EVOLUTION_NO_ROWS


@pytest.mark.parametrize("kind", ["echo", "call_auto_reply", "staff"])
def test_evolution_from_me_messages_are_routed_before_any_capture(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    evolution: _Evolution,
    kind: str,
) -> None:
    """DESIGN item 9: the ``fromMe`` routing -- the bot's own echo, the
    instance's call auto-reply (``_is_evolution_call_auto_reply``) and a staff
    message -- stays BEFORE the capture: none of them reads the mode, charges
    or is blocked, even for a blocked builder (never charged, DESIGN OUT)."""

    tenant = world.evolution_tenant
    client._submit(_courtesy(tenant, keep=False))
    if kind == "echo":
        webhook._track_sent_message({"messageid": "evolution-own-echo"})
        body = _evolution_body(world, "evolution-own-echo", from_me=True)
        expected: dict[str, Any] = {"ok": True, "skipped": "ai_echo"}
    elif kind == "call_auto_reply":
        body = _evolution_body(
            world,
            "3EB0" + "A1B2C3D4E5F6071829",
            from_me=True,
            message={"conversation": "Não posso atender agora."},
            status="PENDING",
        )
        expected = {"ok": True, "skipped": "call_auto_reply"}
    else:
        body = _evolution_body(
            world,
            "evolution-staff-1",
            from_me=True,
            message={"conversation": "Oi, aqui é a recepção"},
        )
        # Main's staff routing (no session for this phone yet).
        expected = {"ok": True, "action": "no_session"}
    response = _evolution_post(client, body)
    assert response.status_code == 200, response.text
    assert response.json() == expected
    assert evolution.reads == []
    assert providers.sent == []
    providers.llm.assert_not_called()
    assert _uazapi_rows(client) == _EVOLUTION_NO_ROWS


def test_a_charged_rule_builder_evolution_text_is_charged_once_with_its_echo_known(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    evolution: _Evolution,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DESIGN item 9: claimed (Redis AND PostgreSQL ``evolution``), charged one
    trace on the courtesy lot, the reply sent from the outbox by the
    Evolution branch of ``CreditReplySender`` -- its ``key.id`` stored, so
    its echo is the bot's (``ai_echo_db`` on a worker that only has the
    stored id) and never pauses the session -- and a redelivery is
    ``duplicate_message``: no second debit, no second reply.  The decision's
    digest carries ``unknown``, the mode is read once, no egress flag."""

    digests: list[str] = []
    original = credit_metering.decision_digest

    def spy(**kwargs: Any) -> str:
        digests.append(kwargs["origin"].value)
        return original(**kwargs)

    monkeypatch.setattr(credit_metering, "decision_digest", spy)
    tenant = world.evolution_tenant
    client._submit(_courtesy(tenant, keep=True))
    body = _evolution_body(world, "evolution-rule-charged-1")
    response = _evolution_post(client, body)
    assert response.json() == {"ok": True, "replied": True}, response.json()
    assert _evolution_texts(providers) == [_LLM_REPLY]
    assert evolution.reads == [tenant] and evolution.egress_reads == 0
    assert digests == ["unknown"]
    rows = _uazapi_rows(client)
    assert rows["claims"] == [("evolution", "enforced", "evolution-rule-charged-1")]
    assert [kind for kind, _ in rows["effects"]] == ["trace"]
    assert rows["ledger"] == {"grant": 5_000, "debit": 1_000}
    assert rows["outbox"] == ["delivered"] and rows["observed"] == []
    [outbox] = _all_rows(client, CreditResponseOutbox)
    assert outbox.payload["channel"] == "evolution"
    assert outbox.payload["input_was_audio"] is False
    [session] = client._submit(_patient_sessions(tenant, _EVOLUTION_PHONE))
    assert [m for m, _ in session.session_metadata["ai_msg_ids"]] == [
        _EVOLUTION_REPLY_ID
    ]
    # The echo of the charged reply is the bot's, never the clinic's: tracked
    # in this worker AND stored for every other one -- the echo below reaches
    # a worker that only has the stored id.
    assert _EVOLUTION_REPLY_ID in webhook._INMEM_SENT
    webhook._INMEM_SENT.clear()
    echo = _evolution_body(
        world,
        _EVOLUTION_REPLY_ID,
        from_me=True,
        message={"conversation": _LLM_REPLY},
    )
    assert _evolution_post(client, echo).json() == {
        "ok": True,
        "skipped": "ai_echo_db",
    }
    [session] = client._submit(_patient_sessions(tenant, _EVOLUTION_PHONE))
    assert session.is_paused is False
    again = _evolution_post(client, body)
    assert again.json() == {"ok": True, "skipped": "duplicate_message"}
    assert _uazapi_rows(client)["ledger"] == {"grant": 5_000, "debit": 1_000}
    assert _evolution_texts(providers) == [_LLM_REPLY]
    assert evolution.reads == [tenant]  # neither the echo nor the redelivery


def test_a_charged_evolution_turn_without_a_reply_is_answered_and_decided_once(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    evolution: _Evolution,
) -> None:
    """A charged Evolution turn whose LLM produces nothing answers the shared
    enforced body (``no_llm_response``; the observational route answers
    ``{"ok": true, "replied": false}``): nothing is sent, no outbox row, and
    the decision still stands -- claimed ``enforced`` once, one
    ``trace_no_response`` effect (of zero, no debit) -- and a redelivery is
    ``duplicate_message``."""

    providers.llm.return_value = None  # the builder has no LLM configured
    tenant = world.evolution_tenant
    client._submit(_courtesy(tenant, keep=True))
    body = _evolution_body(world, "evolution-rule-no-reply-1")
    response = _evolution_post(client, body)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "ok": True,
        "replied": False,
        "reason": "no_llm_response",
    }
    assert providers.sent == []
    assert evolution.reads == [tenant] and evolution.egress_reads == 0
    rows = _uazapi_rows(client)
    assert rows["claims"] == [("evolution", "enforced", "evolution-rule-no-reply-1")]
    assert [kind for kind, _ in rows["effects"]] == ["trace_no_response"]
    assert rows["ledger"] == {"grant": 5_000}  # an effect of zero, as on Z-API
    assert rows["outbox"] == [] and rows["observed"] == []
    again = _evolution_post(client, body)
    assert again.json() == {"ok": True, "skipped": "duplicate_message"}
    assert _uazapi_rows(client) == rows
    assert providers.sent == []
    assert evolution.reads == [tenant]


def test_a_charged_evolution_new_session_gets_its_lgpd_notice_free(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    evolution: _Evolution,
) -> None:
    """The LGPD notice (the one internal route Evolution has before the
    reply): a new session's notice goes out first, free; then the charged
    reply -- one trace in all."""

    tenant = world.evolution_tenant
    client._submit(_courtesy(tenant, keep=True))
    client._submit(_lgpd_on(tenant))
    response = _evolution_post(client, _evolution_body(world, "evolution-lgpd-1"))
    assert response.json() == {"ok": True, "replied": True}, response.json()
    texts = _evolution_texts(providers)
    assert len(texts) == 2 and texts[-1] == _LLM_REPLY
    assert _uazapi_rows(client)["ledger"] == {"grant": 5_000, "debit": 1_000}


def test_a_charged_evolution_reply_without_credentials_is_main_s_answer(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    evolution: _Evolution,
) -> None:
    """Main answers ``no_credentials`` after the decision; charged, the
    decision stands (one trace, like any lost reply, OD-1) and its outbox
    row is ``failed`` with nothing sent."""

    tenant = world.evolution_tenant
    client._submit(_courtesy(tenant, keep=True))
    client._submit(_set_credentials(tenant, evolution_api_key=""))
    response = _evolution_post(client, _evolution_body(world, "evolution-nocred-1"))
    assert response.json() == {
        "ok": True,
        "replied": False,
        "reason": "no_credentials",
    }
    rows = _uazapi_rows(client)
    assert rows["claims"] == [("evolution", "enforced", "evolution-nocred-1")]
    assert rows["ledger"] == {"grant": 5_000, "debit": 1_000}
    assert rows["outbox"] == ["failed"]
    assert providers.sent == []


def test_a_charged_evolution_ai_handoff_is_one_trace_charged_in_phase1(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    evolution: _Evolution,
    announced: list[UUID],
) -> None:
    """The pipeline's own handoff (phase 1) on Evolution under the rule: one
    trace, debited from the courtesy lot in phase 1 and linked to the handoff
    record; the reply goes out once, with no unread mark (Evolution has
    none)."""

    from tests.services.test_credit_metering import _HANDOFF_TEXT

    tenant = world.evolution_tenant
    client._submit(_courtesy(tenant, keep=True))
    body = _evolution_body(
        world, "evolution-rule-handoff-1", message={"conversation": _HANDOFF_TEXT}
    )
    response = _evolution_post(client, body)
    assert response.json() == {"ok": True, "replied": True}, response.json()
    rows = _uazapi_rows(client)
    assert rows["claims"] == [("evolution", "enforced", "evolution-rule-handoff-1")]
    [(kind, effect_id)] = rows["effects"]
    assert kind == "trace" and rows["ledger"] == {"grant": 5_000, "debit": 1_000}
    [record] = _all_rows(client, HandoffRecord)
    assert record.context["credit_effect_id"] == effect_id
    assert announced == [record.id]
    assert len(_evolution_texts(providers)) == 1
    assert "mark_conversation_unread" not in [name for name, _ in providers.sent]
    [outbox] = _all_rows(client, CreditResponseOutbox)
    assert outbox.state == "delivered" and outbox.payload["handoff"] is True
    assert outbox.payload["channel"] == "evolution"


@pytest.mark.parametrize("change", ["removed", "marked", "granted"])
def test_an_evolution_redelivery_across_a_mode_change_is_a_duplicate(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    evolution: _Evolution,
    change: str,
) -> None:
    """DESIGN Decision 16: the Redis replay claim, kept on every turn where it
    was, answers a redelivery of the same ``data.key.id`` whatever the mode
    became in between -- never read again, never a second debit or reply
    (the replay store down: the PostgreSQL suite)."""

    tenant = world.evolution_tenant
    lot = client._submit(_courtesy_lot(tenant))
    body = _evolution_body(world, f"evolution-redelivered-{change}")
    first = _evolution_post(client, body)
    assert first.json() == {"ok": True, "replied": True}, first.json()
    if change == "removed":
        client._submit(_remove(lot))
    elif change == "marked":
        client._submit(_mark(tenant))
    else:
        client._submit(_courtesy_lot(tenant))
    again = _evolution_post(client, body)
    assert again.json() == {"ok": True, "skipped": "duplicate_message"}
    assert evolution.reads == [tenant]
    assert _evolution_texts(providers) == [_LLM_REPLY]
    rows = _uazapi_rows(client)
    assert rows["claims"] == [
        ("evolution", "enforced", f"evolution-redelivered-{change}")
    ]
    assert rows["ledger"]["debit"] == 1_000


@pytest.mark.parametrize("mode", ["none", "mark", "global", "protected"])
def test_evolution_outside_the_rule_reads_the_mode_once_and_stays_observational(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    evolution: _Evolution,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """Dominance (DESIGN Decision 18, wish Decision 8): under no mode, a mark,
    the global or a protected courtesy (spent, a paid plan in force), an
    Evolution turn is main's observational turn: no decision, no enforced
    claim, no debit, the answer and the observation main writes; its ONE new
    statement block is the mode read, read exactly once per turn -- a
    message that leaves at the pause or the AI-off gate included (read
    before them, decided at the pre-check)."""

    from tests.services.test_credit_metering import _enforce_credits

    tenant = world.evolution_tenant
    if mode == "mark":
        client._submit(_mark(tenant))
    elif mode == "global":
        client._submit(_enforce_credits())
    elif mode == "protected":
        client._submit(_courtesy(tenant, keep=False))
        client._submit(_paid_plan(tenant))
    credit_metering.forget_enforcement_seen()
    decisions: list[Any] = []
    real_decide = credit_metering.decide_webhook

    async def counted_decide(db: Any, capture: Any) -> Any:
        decisions.append(capture.source)
        return await real_decide(db, capture)

    monkeypatch.setattr(credit_metering, "decide_webhook", counted_decide)
    try:
        served = _evolution_post(client, _evolution_body(world, f"evolution-{mode}"))
        assert served.json() == {"ok": True, "replied": True}, served.json()
        assert evolution.reads == [tenant]
        client._submit(_pause(tenant, "5511900000499"))
        paused = _evolution_body(
            world, f"evolution-{mode}-paused", phone="5511900000499"
        )
        assert _evolution_post(client, paused).json() == {
            "ok": True,
            "skipped": "session_paused",
        }
        assert evolution.reads == [tenant, tenant]  # once, before the pause gate
        client._submit(_set_bot_inactive(tenant))
        off = _evolution_body(world, f"evolution-{mode}-off", phone="5511900000497")
        assert _evolution_post(client, off).json() == {
            "ok": True,
            "skipped": "bot_paused",
        }
        assert evolution.reads == [tenant, tenant, tenant]  # before the AI-off gate
        assert decisions == [] and evolution.egress_reads == 0
        rows = _uazapi_rows(client)
        assert [claim for claim in rows["claims"] if claim[1] == "enforced"] == []
        assert [kind for kind, _ in rows["effects"]] == []
        assert "debit" not in rows["ledger"] and rows["outbox"] == []
        if mode == "global":
            assert rows["observed"] == []  # the writer stands down, as before
        else:
            assert rows["claims"] == [
                ("evolution", "observational", f"evolution-{mode}")
            ]
            assert rows["observed"] == [("unknown", False, 0)]
    finally:
        credit_metering.forget_enforcement_seen()


@pytest.mark.parametrize("mode", ["none", "mark", "global", "protected"])
def test_a_failed_evolution_mode_read_outside_the_rule_keeps_the_lazy_unpause(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    evolution: _Evolution,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    """The Uazapi MEDIUM-1 lesson on Evolution: a builder NOT under its
    courtesy rule, a session whose pause just expired and the mode read
    failing with no D1 latch -- the read rolls the session back, so it runs
    BEFORE the pause gate: the gate's lazy unpause is not undone, the patient
    is answered and the session ends unpaused (as on main)."""

    from tests.services.test_credit_metering import _enforce_credits

    tenant = world.evolution_tenant
    if mode == "mark":
        client._submit(_mark(tenant))
    elif mode == "global":
        client._submit(_enforce_credits())
    elif mode == "protected":
        client._submit(_courtesy(tenant, keep=False))
        client._submit(_paid_plan(tenant))
    client._submit(_expired_pause(tenant, _EVOLUTION_PHONE))
    credit_metering.forget_enforcement_seen()
    reads: list[UUID] = []

    async def failing(self: CreditRepository, tenant_id: UUID) -> Any:
        reads.append(tenant_id)
        raise RuntimeError("mode read down")

    monkeypatch.setattr(CreditRepository, "tenant_effective_enforcement", failing)
    try:
        body = _evolution_body(world, f"evolution-{mode}-expired-pause")
        response = _evolution_post(client, body)
        assert response.json() == {"ok": True, "replied": True}, response.json()
        assert reads == [tenant]
        assert _evolution_texts(providers) == [_LLM_REPLY]
        [session] = client._submit(_patient_sessions(tenant, _EVOLUTION_PHONE))
        assert session.is_paused is False
        assert session.paused_until is None
    finally:
        credit_metering.forget_enforcement_seen()


def test_an_evolution_rule_change_between_capture_and_lock_answers_503(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    evolution: _Evolution,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DESIGN item 11 on Evolution: the courtesy removed between the capture
    and the account lock -- 503 ``credit_enforcement_unavailable`` before any
    write (no session, no claim, no debit, nothing sent); the message is lost
    like any webhook 503 (its Redis claim is spent)."""

    tenant = world.evolution_tenant
    lot = client._submit(_courtesy_lot(tenant))
    real_decide = credit_metering.decide_webhook

    async def then_remove(db: Any, capture: Any) -> Any:
        await _remove(lot)
        return await real_decide(db, capture)

    monkeypatch.setattr(credit_metering, "decide_webhook", then_remove)
    body = _evolution_body(world, "evolution-rule-race-1")
    response = _evolution_post(client, body)
    assert response.status_code == 503
    assert response.json() == {"error": "credit_enforcement_unavailable"}
    assert providers.sent == []
    assert client._submit(_patient_sessions(tenant, _EVOLUTION_PHONE)) == []
    rows = _uazapi_rows(client)
    assert rows["claims"] == [] and rows["outbox"] == []
    assert rows["ledger"] == {"grant": 5_000, "courtesy_removal": 5_000}
    lost = _evolution_post(client, body)
    assert lost.json() == {"ok": True, "skipped": "duplicate_message"}


def test_a_tenant_seen_under_the_rule_whose_rule_fails_is_503_on_evolution(
    client: Any,
    world: World,
    providers: Providers,
    force_switch: Any,
    rule_reading: None,
    evolution: _Evolution,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rule's per-tenant D1 on Evolution: once seen under the rule, a
    failed read answers 503 at once -- before the AI-off and pause gates,
    the session and any send; the message is lost (its Redis claim is
    spent, as on Z-API)."""

    tenant = world.evolution_tenant
    client._submit(_courtesy(tenant, keep=True))
    first = _evolution_post(client, _evolution_body(world, "evolution-d1-seen"))
    assert first.json() == {"ok": True, "replied": True}, first.json()
    assert credit_metering.enforcement_rule_seen(tenant)
    providers.sent.clear()

    async def unreadable(_self: CreditRepository, _tenant_id: UUID) -> Any:
        raise RuntimeError("courtesy lots unreadable")

    body = _evolution_body(world, "evolution-d1-text", phone="5511900000496")
    with monkeypatch.context() as patch:
        patch.setattr(CreditRepository, "tenant_effective_enforcement", unreadable)
        refused = _evolution_post(client, body)
    assert refused.status_code == 503
    assert refused.json() == {"error": "credit_enforcement_unavailable"}
    assert providers.sent == []
    assert client._submit(_patient_sessions(tenant, "5511900000496")) == []
    retried = _evolution_post(client, body)
    assert retried.json() == {"ok": True, "skipped": "duplicate_message"}
