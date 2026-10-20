"""
NeuroStack API — Uazapi Webhook Tests

Covers Groups 2 + 4 of the uazapi-integration wish:
- POST /v1/webhook/uazapi text flow (tenant resolution by uazapi_instance_name,
  pipeline dispatch, reply via ``send_uazapi_text``)
- Unknown instance short-circuits with a WARNING log
- Non-text events (image, chat_status, etc.) are skipped with ``non-text``
- ``_check_bot_active`` returning a skip dict yields 202 without touching the
  pipeline or the send helper
- A tenant with resolved instance but no ``uazapi_api_url``/``uazapi_token``
  logs a warning and returns ``replied=False`` with ``reason=no_credentials``
- Group 4: patient audio is transcribed via the STT chain or falls back to
  the text reroute; media tests load a tracked, synthetic fixture derived
  from the shape of the locally captured payload.
"""

import copy
import json
import pathlib
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from src.api.routes import webhook as webhook_module
from src.config.settings import settings
from src.handoff.pause_policy import resolve_handoff_pause
from tests.handoff.test_pause_policy import RUNTIME_POLICY_BUCKETS

# Synthetic Uazapi webhook payloads (audio + image), derived from capture
# SHA-256 256b7ffbea39fa09e25041172f2af217424e60470be45c8ef11aabd502fe5f40.
# The tracked fixture preserves only schema, types, MIME values and media
# semantics; every identifying or signed scalar is replaced by a sentinel.
_MEDIA_PAYLOAD_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "fixtures"
    / "uazapi"
    / "media-payload.json"
)

_LONG_DIGIT_RUN = re.compile(r"\d{8,}")
_UUID = re.compile(
    r"(?<![0-9a-f])[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}(?![0-9a-f])",
    re.IGNORECASE,
)
_DOMAIN = re.compile(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", re.I)
_EXAMPLE_URL = re.compile(r"https://example[.]invalid(?:/.*)?")
_TIMESTAMP_KEY = re.compile(r"timestamp|(?:^|_)at$")
_SEMANTIC_STRINGS = {
    ("audio", "EventType"): "messages",
    ("audio", "chat", "wa_lastMessageType"): "AudioMessage",
    ("audio", "message", "content", "mimetype"): "audio/ogg; codecs=opus",
    ("audio", "message", "mediaType"): "ptt",
    ("audio", "message", "messageType"): "AudioMessage",
    ("audio", "message", "type"): "media",
    ("image", "EventType"): "messages",
    ("image", "chat", "wa_lastMessageType"): "ImageMessage",
    ("image", "message", "content", "mimetype"): "image/jpeg",
    ("image", "message", "mediaType"): "image",
    ("image", "message", "messageType"): "ImageMessage",
    ("image", "message", "type"): "media",
}


def _iter_fixture_scalars(value: Any, path: tuple[str, ...] = ()):
    """Yield every scalar with its deterministic object/list path."""
    if isinstance(value, dict):
        for key in sorted(value):
            yield from _iter_fixture_scalars(value[key], (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _iter_fixture_scalars(item, (*path, str(index)))
    else:
        yield path, value


def _load_media_evidence(kind: str) -> dict:
    """Deep-copy the sanitized ``audio`` or ``image`` fixture from disk.

    Deep-copy so per-test mutations (instanceName, fromMe, messageid) never
    leak into the next test. The file is tracked under ``tests/fixtures`` so
    clean checkouts exercise the same schema without depending on local evidence.
    """
    if not _MEDIA_PAYLOAD_PATH.exists():
        raise FileNotFoundError(
            f"Missing sanitized fixture at {_MEDIA_PAYLOAD_PATH}. "
            "Group 4 tests require the tracked media schema."
        )
    doc = json.loads(_MEDIA_PAYLOAD_PATH.read_text(encoding="utf-8"))
    if kind not in doc:
        raise KeyError(f"media-payload.json has no '{kind}' entry")
    return copy.deepcopy(doc[kind])


API_CRED_PATH = "/v1/config/api-credentials"
WEBHOOK_PATH = "/v1/webhook/uazapi"


# ============================================
# Fixtures / helpers
# ============================================


def _make_admin_token(tenant_id: UUID) -> str:
    """Generate a valid clinic_admin JWT bound to a tenant."""
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(tenant_id),
            "role": "clinic_admin",
            "email": "admin@uazapi-webhook-test.com",
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )


def _admin_headers(tenant_id: UUID) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_make_admin_token(tenant_id)}",
        "Content-Type": "application/json",
    }


def _seed_uazapi_credentials(
    client: TestClient,
    tenant_id: UUID,
    instance_name: str,
    *,
    api_url: str = "https://uazapi.example.com",
    token: str | None = None,
) -> None:
    """Seed persisted legacy Uazapi credentials for webhook coverage."""
    _seed_legacy_uazapi_credentials(
        client,
        tenant_id,
        {
            "whatsapp_provider": "uazapi",
            "uazapi_api_url": api_url,
            "uazapi_token": token or f"uz_token_{uuid4().hex}",
            "uazapi_instance_name": instance_name,
        },
    )


def _seed_uazapi_credentials_missing_secrets(
    client: TestClient,
    tenant_id: UUID,
    instance_name: str,
) -> None:
    """Save only the instance_name so the tenant is resolvable but has no
    URL/token — used to exercise the ``no_credentials`` branch."""
    _seed_legacy_uazapi_credentials(
        client,
        tenant_id,
        {"whatsapp_provider": "uazapi", "uazapi_instance_name": instance_name},
    )


def _seed_legacy_uazapi_credentials(
    client: TestClient, tenant_id: UUID, credentials: dict[str, Any]
) -> None:
    """Insert legacy credential state without reopening public provisioning."""
    from src.models.clinic_config_table import ClinicConfigTable
    from src.repositories.database import get_db_session

    async def _run() -> None:
        db = get_db_session()
        try:
            db.add(ClinicConfigTable(tenant_id=tenant_id, api_credentials=credentials))
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]


def _uazapi_text_payload(
    *,
    instance: str,
    from_phone: str,
    body: Any,
    message_id: str,
    msg_type: str = "text",
    media_type: str | None = None,
    text: Any | None = None,
    content: Any | None = None,
    from_me: bool = False,
    chat_jid: str | None = None,
    ptt: bool | None = None,
    sender_pn: str | None = None,
    was_sent_by_api: bool | None = None,
) -> dict:
    """Build a real Uazapi commercial payload (instanceName / message / chat schema).

    ``sender_pn`` defaults to ``from_phone``; pass an explicit value when the
    test needs ``chat.wa_chatid`` and ``message.sender_pn`` to differ — the
    fromMe path resolves the counterparty from ``chat.wa_chatid`` and would
    silently match the wrong phone if both fields carried the same number.
    """
    resolved_sender_pn = (
        sender_pn if sender_pn is not None else f"{from_phone}@s.whatsapp.net"
    )
    message = {
        "sender_pn": resolved_sender_pn,
        "text": body if text is None else text,
        "content": body if content is None else content,
        "type": msg_type,
        "messageType": "Conversation" if msg_type == "text" else msg_type,
        "messageid": message_id,
        "fromMe": from_me,
    }
    if media_type is not None:
        message["mediaType"] = media_type
    if ptt is not None:
        message["ptt"] = ptt
    if was_sent_by_api is not None:
        message["wasSentByApi"] = was_sent_by_api

    return {
        "instanceName": instance,
        "EventType": "messages",
        "owner": "5511940589006",
        "chat": {
            "wa_chatid": chat_jid or f"{from_phone}@s.whatsapp.net",
            "wa_name": "Test User",
            "phone": f"+{from_phone}",
        },
        "message": message,
    }


def _fake_event_response(suggested: str | None = "Olá, tudo bem?"):
    """Return a MagicMock shaped like an EventService.process_event result."""
    prompt = SimpleNamespace(suggested_response=suggested)
    decision = SimpleNamespace(handoff_required=False)
    return SimpleNamespace(
        prompt=prompt,
        handoff_summary=None,
        decision=decision,
        response_language="pt-BR",
    )


def _fake_uazapi_send_result(message_id: str | None = None) -> dict:
    """Return a payload shaped like the Uazapi (wuzapi) send response."""
    return {
        "Details": "Sent",
        "Timestamp": "2026-07-25T10:00:00Z",
        "MessageID": message_id or f"3EB0_{uuid4().hex[:12]}",
    }


@pytest.fixture
def tenant_id() -> UUID:
    """Fresh tenant per test so each ClinicConfigTable row is isolated."""
    return uuid4()


@pytest.mark.parametrize("bucket", RUNTIME_POLICY_BUCKETS)
@pytest.mark.asyncio
async def test_takeover_matches_shared_policy_with_same_clock(
    monkeypatch: pytest.MonkeyPatch,
    bucket: Any,
    tenant_id: UUID,
) -> None:
    now = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)
    # NOTE: pause now flows through SessionRepository.pause_effective (CAS)
    # instead of a raw ``session.is_paused = True`` + flush. The externally
    # visible behavior under test (action/skipped) is preserved; the
    # implementation-detail of which ORM attribute was written is replaced
    # by observing the repo call and the "paused" classification.
    pause_calls: list[dict[str, Any]] = []
    session = SimpleNamespace(
        id=uuid4(),
        is_paused=False,
        paused_until=None,
        get_cognitive_state=lambda: SimpleNamespace(
            prior_episode_context=None,
            to_dict=lambda: {},
        ),
    )

    class Repo:
        def __init__(self, db: Any):
            pass

        async def find_by_user(self, **kwargs: Any) -> list[Any]:
            return [session]

        async def pause_effective(
            self,
            tenant_id: UUID,
            session_id: UUID,
            *,
            cognitive_state: dict[str, Any],
            paused_until: Any,
        ) -> str:
            pause_calls.append(
                {
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "paused_until": paused_until,
                }
            )
            session.is_paused = True
            session.paused_until = paused_until
            return "paused"

    monkeypatch.setattr(webhook_module, "SessionRepository", Repo)
    monkeypatch.setattr(
        webhook_module,
        "_load_handoff_config",
        AsyncMock(return_value=bucket),
    )
    monkeypatch.setattr(
        webhook_module,
        "_load_ai_person_history",
        AsyncMock(return_value=[]),
    )
    db = SimpleNamespace(flush=AsyncMock(), refresh=AsyncMock())

    result = await webhook_module._handle_clinic_message(
        tenant_id,
        "5511999999999",
        "Atendimento humano em andamento",
        db,
        now=now,
    )
    expected = resolve_handoff_pause(bucket, now)

    if expected.should_pause:
        assert result["action"] == "human_takeover"
        assert len(pause_calls) == 1
        assert pause_calls[0]["paused_until"] == expected.paused_until
        db.refresh.assert_awaited_once()
    else:
        assert result["skipped"] == "handoff_inactive"
        assert pause_calls == []
        db.flush.assert_not_awaited()


@pytest.mark.parametrize(
    ("offset", "still_paused", "naive_deadline"),
    [
        (-timedelta(microseconds=1), True, False),
        (timedelta(0), False, False),
        (timedelta(0), False, True),
        (timedelta(seconds=1), False, False),
    ],
)
@pytest.mark.asyncio
async def test_lazy_unpause_before_at_and_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tenant_id: UUID,
    offset: timedelta,
    still_paused: bool,
    naive_deadline: bool,
) -> None:
    deadline = datetime(2026, 9, 29, 15, 0, tzinfo=timezone.utc)
    stored_deadline = deadline.replace(tzinfo=None) if naive_deadline else deadline
    # Auto-reactivation now routes through reactivate_effective_auto (CAS)
    # instead of a raw ``session.is_paused = False`` + flush. The externally
    # visible contract (returned skip-dict or None) is preserved; the test
    # asserts the repo call rather than the ORM attribute mutation.
    auto_calls: list[dict[str, Any]] = []
    session = SimpleNamespace(
        id=uuid4(),
        is_paused=True,
        paused_until=stored_deadline,
        get_cognitive_state=lambda: SimpleNamespace(
            prior_episode_context=None,
            to_dict=lambda: {},
        ),
    )

    class Repo:
        def __init__(self, db: Any):
            pass

        async def find_by_user(self, **kwargs: Any) -> list[Any]:
            return [session]

        async def reactivate_effective_auto(
            self,
            *,
            tenant_id: UUID,
            session_id: UUID,
            new_cognitive_state: dict[str, Any],
            new_episode_id: UUID,
            now: datetime,
        ) -> str:
            auto_calls.append(
                {
                    "tenant_id": tenant_id,
                    "session_id": session_id,
                    "now": now,
                }
            )
            session.is_paused = False
            session.paused_until = None
            return "reactivated"

    monkeypatch.setattr(webhook_module, "SessionRepository", Repo)
    db = SimpleNamespace(flush=AsyncMock(), refresh=AsyncMock())

    result = await webhook_module._check_pause_state(
        tenant_id,
        "5511999999999",
        db,
        now=deadline + offset,
    )

    if still_paused:
        assert result == {"ok": True, "skipped": "session_paused"}
        assert session.is_paused is True
        assert session.paused_until == stored_deadline
        assert auto_calls == []
    else:
        assert result is None
        assert session.is_paused is False
        assert session.paused_until is None
        assert len(auto_calls) == 1
        db.refresh.assert_awaited_once()


@pytest.mark.asyncio
async def test_malformed_master_cooldown_uses_safe_one_hour_default(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tenant_id: UUID,
) -> None:
    now = datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc)
    session = SimpleNamespace(id=uuid4(), is_paused=True, paused_until=None)

    class Repo:
        def __init__(self, db: Any):
            pass

        async def find_by_user(self, **kwargs: Any) -> list[Any]:
            return [session]

    monkeypatch.setattr(webhook_module, "SessionRepository", Repo)
    monkeypatch.setattr(
        webhook_module,
        "_load_handoff_config",
        AsyncMock(
            return_value={
                "trigger_message": "mensagem mestra",
                "reactivation_delay_hours": {"invalid": "raw value"},
            }
        ),
    )
    db = SimpleNamespace(flush=AsyncMock())

    with caplog.at_level("WARNING", logger="src.api.routes.webhook"):
        result = await webhook_module._handle_clinic_message(
            tenant_id,
            "5511999999999",
            "Mensagem mestra",
            db,
            now=now,
        )

    assert result["action"] == "trigger_reactivation"
    assert session.paused_until == now + timedelta(hours=1)
    record = caplog.records[-1]
    assert record.field == "reactivation_delay_hours"  # type: ignore[attr-defined]
    assert record.reason == "invalid_duration"  # type: ignore[attr-defined]
    assert "raw value" not in record.getMessage()


@pytest.mark.parametrize(
    ("configured_hours", "expected_hours", "expected_reason"),
    [(3, 3, None), (10**100, 1, "unrepresentable_duration")],
)
@pytest.mark.asyncio
async def test_master_cooldown_handles_valid_and_unrepresentable_integer_hours(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    tenant_id: UUID,
    configured_hours: int,
    expected_hours: int,
    expected_reason: str | None,
) -> None:
    now = datetime(2026, 9, 29, 17, 0, tzinfo=timezone.utc)
    session = SimpleNamespace(id=uuid4(), is_paused=True, paused_until=None)

    class Repo:
        def __init__(self, db: Any):
            pass

        async def find_by_user(self, **kwargs: Any) -> list[Any]:
            return [session]

    monkeypatch.setattr(webhook_module, "SessionRepository", Repo)
    monkeypatch.setattr(
        webhook_module,
        "_load_handoff_config",
        AsyncMock(
            return_value={
                "trigger_message": "mensagem mestra",
                "reactivation_delay_hours": configured_hours,
            }
        ),
    )
    db = SimpleNamespace(flush=AsyncMock())

    with caplog.at_level("WARNING", logger="src.api.routes.webhook"):
        result = await webhook_module._handle_clinic_message(
            tenant_id,
            "5511999999999",
            "Mensagem mestra",
            db,
            now=now,
        )

    assert result["action"] == "trigger_reactivation"
    assert session.paused_until == now + timedelta(hours=expected_hours)
    if expected_reason is None:
        assert not caplog.records
    else:
        record = caplog.records[-1]
        assert record.field == "reactivation_delay_hours"  # type: ignore[attr-defined]
        assert record.reason == expected_reason  # type: ignore[attr-defined]
        assert str(configured_hours) not in record.getMessage()


# ============================================
# POST /v1/webhook/uazapi — text flow
# ============================================


class TestUazapiWebhookTextFlow:
    def test_text_message_dispatches_pipeline_and_replies(
        self,
        client: TestClient,
        tenant_id: UUID,
        caplog: pytest.LogCaptureFixture,
    ):
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        msg_id = f"3EB0_{uuid4().hex[:10]}"
        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511988887777",
            body="Quero agendar uma consulta",
            message_id=msg_id,
        )

        with (
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(return_value=_fake_uazapi_send_result()),
            ) as mock_send,
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Claro, para quando gostaria?")
            )
            mock_get_service.return_value = service

            with caplog.at_level("INFO", logger="src.api.routes.webhook"):
                resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True}

        # Pipeline was called with the parsed text + Uazapi metadata
        assert service.run_pipeline.await_count == 1
        event_arg = service.run_pipeline.await_args.args[0]
        assert event_arg.payload.content == "Quero agendar uma consulta"
        assert event_arg.payload.metadata["source"] == "uazapi"
        assert event_arg.payload.metadata["messageId"] == msg_id
        assert event_arg.payload.metadata["phone"] == "5511988887777"

        # Reply was sent via send_uazapi_text with the expected arguments
        assert mock_send.await_count == 1
        url_arg, token_arg, phone_arg, text_arg = mock_send.await_args.args
        assert url_arg == "https://uazapi.example.com"
        assert token_arg.startswith("uz_token_")
        assert phone_arg == "5511988887777"
        assert text_arg == "Claro, para quando gostaria?"

        # The Uazapi msg id and phone appear in the receipt log
        receipt_logs = [
            rec for rec in caplog.records if "Uazapi webhook: msg" in rec.getMessage()
        ]
        assert receipt_logs, "expected a receipt log line for the incoming msg"
        assert msg_id in receipt_logs[0].getMessage()


class TestUazapiWebhookDuplicateMessage:
    def test_duplicate_message_id_skips_pipeline_and_reply(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        from src.api.routes import webhook as webhook_module

        webhook_module._INMEM_INCOMING.clear()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        msg_id = f"3EB0_dup_{uuid4().hex[:8]}"
        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511988887777",
            body="Olá",
            message_id=msg_id,
        )

        with (
            patch(
                "src.api.routes.webhook._get_redis",
                return_value=None,
            ),
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(return_value=_fake_uazapi_send_result()),
            ) as mock_send,
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Claro, posso ajudar.")
            )
            mock_get_service.return_value = service

            first = client.post(WEBHOOK_PATH, json=payload)
            second = client.post(WEBHOOK_PATH, json=payload)

        assert first.status_code == 200, first.text
        assert first.json() == {"ok": True, "replied": True}
        assert second.status_code == 200, second.text
        assert second.json() == {"ok": True, "skipped": "duplicate_message"}
        assert service.run_pipeline.await_count == 1
        assert mock_send.await_count == 1

        webhook_module._INMEM_INCOMING.clear()


def _unavailable_redis(mode: str) -> MagicMock | None:
    """Simulate a Redis outage: no client at all, or a client whose SET fails."""

    if mode == "no_client":
        return None
    redis_client = MagicMock()
    redis_client.set.side_effect = ConnectionError("redis down")
    return redis_client


class TestUazapiReplayStoreUnavailable:
    """Uazapi translates a replay-store outage into the shared 503 contract.

    Production traffic is served with ``ENVIRONMENT=staging``, where the
    claim fails closed instead of using the per-process memory store; both
    Uazapi claim sites (text and post-download audio) must answer 503
    ``replay_store_unavailable`` rather than leak an unhandled 500.
    """

    @pytest.mark.parametrize("redis_mode", ["no_client", "set_raises"])
    def test_text_claim_returns_exact_503_and_runs_no_effects(
        self,
        client: TestClient,
        tenant_id: UUID,
        redis_mode: str,
    ):
        webhook_module._INMEM_INCOMING.clear()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)
        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511988887777",
            body="Olá",
            message_id=f"3EB0_replay_down_{uuid4().hex[:8]}",
        )

        with (
            patch.object(settings, "environment", "staging"),
            patch(
                "src.api.routes.webhook._get_redis",
                return_value=_unavailable_redis(redis_mode),
            ),
            patch(
                "src.api.routes.webhook.get_event_service",
                side_effect=AssertionError("must not run pipeline"),
            ) as get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(side_effect=AssertionError("must not send")),
            ) as send,
        ):
            response = client.post(WEBHOOK_PATH, json=payload)

        assert response.status_code == 503, response.text
        assert response.json() == {"error": "replay_store_unavailable"}
        get_service.assert_not_called()
        send.assert_not_awaited()
        assert not webhook_module._INMEM_INCOMING

    @pytest.mark.parametrize("redis_mode", ["no_client", "set_raises"])
    def test_audio_claim_after_download_returns_exact_503_and_runs_no_effects(
        self,
        client: TestClient,
        tenant_id: UUID,
        redis_mode: str,
    ):
        webhook_module._INMEM_INCOMING.clear()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_audio_tenant(client, tenant_id, instance, stt=True)
        payload = _build_uazapi_media_body(
            "audio",
            instance=instance,
            message_id=f"3EB0_replay_down_audio_{uuid4().hex[:8]}",
        )
        service = MagicMock()
        service._load_clinic_config = AsyncMock(return_value={})
        service.run_pipeline = AsyncMock()
        service.finish_response = AsyncMock()
        download = AsyncMock(return_value=(b"OggS_replay_down", "audio/ogg"))
        chain = AsyncMock()
        send = AsyncMock()
        reply = AsyncMock()
        fallback = AsyncMock()

        with (
            patch.object(settings, "environment", "staging"),
            patch(
                "src.api.routes.webhook._get_redis",
                return_value=_unavailable_redis(redis_mode),
            ),
            patch("src.api.routes.webhook.get_event_service", return_value=service),
            patch("src.api.routes.webhook.download_uazapi_media", download),
            patch("src.api.routes.webhook._transcribe_audio_chain", chain),
            patch("src.api.routes.webhook.send_uazapi_text", send),
            patch("src.api.routes.webhook._send_reply_with_optional_tts", reply),
            patch("src.api.routes.webhook._send_uazapi_stt_fallback", fallback),
        ):
            response = client.post(WEBHOOK_PATH, json=payload)

        assert response.status_code == 503, response.text
        assert response.json() == {"error": "replay_store_unavailable"}
        # The claim is reached (download succeeded first), then fails closed.
        download.assert_awaited_once()
        chain.assert_not_awaited()
        service.run_pipeline.assert_not_awaited()
        service.finish_response.assert_not_awaited()
        send.assert_not_awaited()
        reply.assert_not_awaited()
        fallback.assert_not_awaited()
        assert not webhook_module._INMEM_INCOMING


class TestUazapiInboundWhenEgressClosed:
    @pytest.mark.parametrize("flag_failure", ["false", "error"])
    def test_new_session_privacy_and_reply_are_suppressed_after_processing(
        self,
        client: TestClient,
        tenant_id: UUID,
        flag_failure: str,
    ) -> None:
        webhook_module._INMEM_INCOMING.clear()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)
        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511988887777",
            body="Primeiro contato",
            message_id=f"3EB0_closed_new_{uuid4().hex[:8]}",
        )
        flag_reader = AsyncMock(
            return_value=False,
            side_effect=(
                RuntimeError("agent unavailable") if flag_failure == "error" else None
            ),
        )
        safe_client = MagicMock()

        with (
            patch("src.api.routes.webhook._get_redis", return_value=None),
            patch("src.api.routes.webhook.get_event_service") as mock_get_service,
            patch(
                "src.services.egress_policy.AppConfigClient.is_enabled",
                flag_reader,
            ),
            patch("src.services.uazapi_service.safe_async_client", safe_client),
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(
                return_value=_lgpd_config(enabled=True)
            )
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Resposta local")
            )
            mock_get_service.return_value = service

            first = client.post(WEBHOOK_PATH, json=payload)
            duplicate = client.post(WEBHOOK_PATH, json=payload)

        assert first.status_code == 200, first.text
        assert first.json() == {
            "ok": True,
            "replied": False,
            "reason": "uazapi_egress_disabled",
        }
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json() == {"ok": True, "skipped": "duplicate_message"}
        service.run_pipeline.assert_awaited_once()
        service.finish_response.assert_awaited_once()
        assert flag_reader.await_count == 2
        safe_client.assert_not_called()
        webhook_module._INMEM_INCOMING.clear()

    def test_privacy_suppression_is_reported_without_a_suggested_response(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        webhook_module._INMEM_INCOMING.clear()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)
        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511988887777",
            body="Primeiro contato sem resposta",
            message_id=f"3EB0_closed_no_reply_{uuid4().hex[:8]}",
        )
        flag_reader = AsyncMock(return_value=False)
        safe_client = MagicMock()

        with (
            patch("src.api.routes.webhook._get_redis", return_value=None),
            patch("src.api.routes.webhook.get_event_service") as mock_get_service,
            patch(
                "src.services.egress_policy.AppConfigClient.is_enabled",
                flag_reader,
            ),
            patch("src.services.uazapi_service.safe_async_client", safe_client),
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(
                return_value=_lgpd_config(enabled=True)
            )
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(return_value=_fake_event_response(None))
            mock_get_service.return_value = service

            response = client.post(WEBHOOK_PATH, json=payload)

        assert response.status_code == 200, response.text
        assert response.json() == {
            "ok": True,
            "replied": False,
            "reason": "uazapi_egress_disabled",
        }
        service.run_pipeline.assert_awaited_once()
        service.finish_response.assert_awaited_once()
        flag_reader.assert_awaited_once_with("uazapi_egress_enabled")
        safe_client.assert_not_called()
        webhook_module._INMEM_INCOMING.clear()

    @pytest.mark.parametrize("flag_failure", ["false", "error"])
    def test_text_processes_once_and_suppresses_normal_reply(
        self,
        client: TestClient,
        tenant_id: UUID,
        flag_failure: str,
    ) -> None:
        webhook_module._INMEM_INCOMING.clear()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)
        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511988887777",
            body="Mensagem normal",
            message_id=f"3EB0_closed_existing_{uuid4().hex[:8]}",
        )
        flag_reader = AsyncMock(
            return_value=False,
            side_effect=(
                RuntimeError("agent unavailable") if flag_failure == "error" else None
            ),
        )
        safe_client = MagicMock()

        with (
            patch("src.api.routes.webhook._get_redis", return_value=None),
            patch("src.api.routes.webhook.get_event_service") as mock_get_service,
            patch(
                "src.services.egress_policy.AppConfigClient.is_enabled",
                flag_reader,
            ),
            patch("src.services.uazapi_service.safe_async_client", safe_client),
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Resposta local")
            )
            mock_get_service.return_value = service
            response = client.post(WEBHOOK_PATH, json=payload)

        assert response.status_code == 200, response.text
        assert response.json() == {
            "ok": True,
            "replied": False,
            "reason": "uazapi_egress_disabled",
        }
        service.run_pipeline.assert_awaited_once()
        service.finish_response.assert_awaited_once()
        flag_reader.assert_awaited_once_with("uazapi_egress_enabled")
        safe_client.assert_not_called()
        webhook_module._INMEM_INCOMING.clear()


class TestUazapiWebhookUnknownInstance:
    def test_unknown_instance_warns_and_skips(
        self,
        client: TestClient,
        tenant_id: UUID,
        caplog: pytest.LogCaptureFixture,
    ):
        # Seed a clinic with a *different* instance
        _seed_uazapi_credentials(
            client,
            tenant_id,
            f"owned_{uuid4().hex[:8]}",
        )

        stranger_instance = f"stranger_{uuid4().hex[:8]}"
        payload = _uazapi_text_payload(
            instance=stranger_instance,
            from_phone="5511900000001",
            body="Olá",
            message_id=f"3EB0_{uuid4().hex[:10]}",
        )

        with caplog.at_level("WARNING", logger="src.api.routes.webhook"):
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "unknown_instance"}
        warn_msgs = [
            rec.getMessage() for rec in caplog.records if rec.levelname == "WARNING"
        ]
        assert any(
            "no tenant for instance" in m and stranger_instance in m for m in warn_msgs
        ), warn_msgs

    def test_unknown_instance_media_skips_before_non_text(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        _seed_uazapi_credentials(
            client,
            tenant_id,
            f"owned_{uuid4().hex[:8]}",
        )

        payload = _uazapi_text_payload(
            instance=f"stranger_{uuid4().hex[:8]}",
            from_phone="5511900000002",
            body="[caption]",
            text="[caption]",
            content="[caption]",
            message_id=f"3EB0_media_{uuid4().hex[:8]}",
            msg_type="media",
            media_type="image",
        )

        with patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service:
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "unknown_instance"}
        assert mock_get_service.call_count == 0


class TestUazapiWebhookNonText:
    def test_media_image_with_caption_is_skipped_as_non_text(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000010",
            body="[image caption]",
            message_id=f"3EB0_img_{uuid4().hex[:8]}",
            msg_type="media",
            media_type="image",
        )

        with (
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(return_value=_fake_uazapi_send_result()),
            ) as mock_send,
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.run_pipeline = AsyncMock()
            service.finish_response = AsyncMock()
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "non-text"}
        # Never touched the pipeline or the send helper
        assert service.run_pipeline.await_count == 0
        assert mock_send.await_count == 0

    def test_media_audio_predicate_wins_even_with_text_present(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Group 4 update: patient audio no longer exits as ``non-text``.

        Instead it enters the STT chain. With no STT/multimodal capability
        seeded, the chain returns ``STT_CHAIN_NO_CAPABILITY`` and the text
        reroute takes over — Decision 6's honest variant, same as Z-API's
        ``test_no_capability_reroutes_without_downloading``.
        """
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000011",
            body="voice note with caption",
            message_id=f"3EB0_audio_{uuid4().hex[:8]}",
            msg_type="media",
            media_type="audio",
            ptt=True,
        )

        send_mock = AsyncMock(return_value=_fake_uazapi_send_result())
        with (
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch("src.api.routes.webhook.send_uazapi_text", send_mock),
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True, "action": "stt_fallback"}
        # Capability gate held — nothing pipeline-side ran
        assert send_mock.await_count == 1

    def test_from_me_audio_bypasses_media_gate_and_reaches_clinic_handler(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Group 3 update: the fromMe audio carve-out from Group 2 now
        reaches ``_handle_clinic_message``. Without an existing session for
        the counterparty, the helper returns ``no_session`` — same shape as
        the Z-API reference (``test_from_me_audio_skips_before_download_and_stt``).
        """
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000012",  # sender_pn / clinic
            body="",
            text="",
            content="",
            message_id=f"3EB0_staff_audio_{uuid4().hex[:8]}",
            msg_type="media",
            media_type="audio",
            ptt=True,
            from_me=True,
            # counterparty patient on a *different* number so the fromMe
            # routing resolves to the patient, not the clinic
            chat_jid="5516900000012@s.whatsapp.net",
        )

        with patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service:
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "action": "no_session"}
        assert mock_get_service.call_count == 0

    def test_from_me_image_still_exits_in_media_gate(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000013",
            body="[staff image]",
            message_id=f"3EB0_staff_image_{uuid4().hex[:8]}",
            msg_type="media",
            media_type="image",
            from_me=True,
        )

        with patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service:
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "non-text"}
        assert mock_get_service.call_count == 0


class TestUazapiWebhookContentExtraction:
    def test_content_object_text_reaches_pipeline_without_dict_repr(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000014",
            body="",
            text="",
            content={"text": "texto real do content"},
            message_id=f"3EB0_content_{uuid4().hex[:8]}",
        )

        with (
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(return_value=_fake_uazapi_send_result()),
            ),
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(return_value=_fake_event_response("ok"))
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True}
        event_arg = service.run_pipeline.await_args.args[0]
        assert event_arg.payload.content == "texto real do content"
        assert not event_arg.payload.content.startswith("{")

    def test_unknown_content_object_is_skipped_as_non_text(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000015",
            body="",
            text="",
            content={"caption": "sem texto reconhecido"},
            message_id=f"3EB0_content_unknown_{uuid4().hex[:8]}",
        )

        with patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service:
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "non-text"}
        assert mock_get_service.call_count == 0


class TestUazapiWebhookGroupChat:
    def test_group_chat_is_skipped_without_creating_session(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000016",
            body="mensagem no grupo",
            message_id=f"3EB0_group_{uuid4().hex[:8]}",
            chat_jid="120363000000000000@g.us",
        )

        with (
            patch(
                "src.api.routes.webhook._get_or_create_session",
                new=AsyncMock(return_value=(uuid4(), True)),
            ) as mock_get_or_create_session,
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
        ):
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "group_chat"}
        assert mock_get_or_create_session.await_count == 0
        assert mock_get_service.call_count == 0


class TestUazapiWebhookBotPaused:
    def test_bot_paused_blocks_reply_with_202(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """When ``_check_bot_active`` returns a skip dict, no reply is sent."""
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000020",
            body="testing bot pause",
            message_id=f"3EB0_paused_{uuid4().hex[:8]}",
        )

        with (
            patch(
                "src.api.routes.webhook._check_bot_active",
                new=AsyncMock(return_value={"ok": True, "skipped": "bot_paused"}),
            ),
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(return_value=_fake_uazapi_send_result()),
            ) as mock_send,
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("blocked")
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        # Same 202 semantics as Z-API/Evolution/Meta paths when bot is paused
        assert resp.status_code == 202
        assert resp.json() == {"ok": True, "skipped": "bot_paused"}
        assert service.run_pipeline.await_count == 0
        assert mock_send.await_count == 0


class TestUazapiWebhookMissingCredentials:
    def test_tenant_resolved_but_no_credentials_replies_false(
        self,
        client: TestClient,
        tenant_id: UUID,
        caplog: pytest.LogCaptureFixture,
    ):
        """When the instance resolves to a tenant that never saved a URL/token,
        the handler logs a warning and returns ``replied=False`` with
        ``reason=no_credentials`` — never a 500."""
        instance = f"partial_{uuid4().hex[:8]}"
        # Seed only the instance_name so tenant resolution succeeds but
        # ``uazapi_api_url`` / ``uazapi_token`` are absent.
        _seed_uazapi_credentials_missing_secrets(client, tenant_id, instance)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000030",
            body="Olá",
            message_id=f"3EB0_nocred_{uuid4().hex[:8]}",
        )

        with (
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(return_value=_fake_uazapi_send_result()),
            ) as mock_send,
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Reply body")
            )
            mock_get_service.return_value = service

            with caplog.at_level("WARNING", logger="src.api.routes.webhook"):
                resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["replied"] is False
        assert body["reason"] == "no_credentials"

        # Pipeline still ran (we only bail after generating a suggestion) but
        # the send helper was never called because credentials are missing.
        assert mock_send.await_count == 0

        warn_msgs = [
            rec.getMessage() for rec in caplog.records if rec.levelname == "WARNING"
        ]
        assert any("Uazapi credentials missing" in m for m in warn_msgs), warn_msgs


# ============================================
# uazapi-parity Group 1b — Privacy notice + echo identity
# ============================================


def _lgpd_config(enabled: bool = True, message_pt: str | None = None) -> dict:
    """Build a clinic_config dict with an LGPD section for testing."""
    lgpd_section: dict[str, Any] = {"lgpd_enabled": enabled}
    if message_pt is not None:
        lgpd_section["lgpd_message"] = message_pt
    return {"lgpd": lgpd_section, "identity": {"clinic_name": "Clínica Teste"}}


class TestUazapiPrivacyNoticeSend:
    """Group 1b Deliverable 4 + Acceptance: new session + lgpd_enabled sends."""

    def test_new_session_with_lgpd_enabled_sends_notice_before_reply(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        from src.api.routes import webhook as webhook_module

        webhook_module._INMEM_SENT.clear()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000100",
            body="Primeiro contato",
            message_id=f"3EB0_first_{uuid4().hex[:8]}",
        )

        privacy_send = _fake_uazapi_send_result(
            message_id=f"3EB0_privacy_{uuid4().hex[:8]}",
        )
        reply_send = _fake_uazapi_send_result(
            message_id=f"3EB0_reply_{uuid4().hex[:8]}",
        )

        # Mock _get_or_create_session so is_new=True is deterministic
        with (
            patch(
                "src.api.routes.webhook._get_or_create_session",
                new=AsyncMock(return_value=(uuid4(), True)),
            ),
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(side_effect=[privacy_send, reply_send]),
            ) as mock_send,
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(
                return_value=_lgpd_config(enabled=True),
            )
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Reply body")
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True}

        # Two sends: the aviso first, then the AI reply
        assert mock_send.await_count == 2, "expected LGPD aviso + reply"
        first_text = mock_send.await_args_list[0].args[3]
        second_text = mock_send.await_args_list[1].args[3]
        # First call is the LGPD notice (default PT copy contains "LGPD")
        assert "LGPD" in first_text or "Política de Privacidade" in first_text
        assert second_text == "Reply body"

    def test_existing_session_never_sends_privacy_notice(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000101",
            body="Segunda mensagem",
            message_id=f"3EB0_second_{uuid4().hex[:8]}",
        )

        reply_send = _fake_uazapi_send_result()

        with (
            patch(
                "src.api.routes.webhook._get_or_create_session",
                new=AsyncMock(return_value=(uuid4(), False)),
            ),
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(return_value=reply_send),
            ) as mock_send,
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(
                return_value=_lgpd_config(enabled=True),
            )
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Reply body")
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True}
        # Only the reply — no aviso for existing session
        assert mock_send.await_count == 1
        assert mock_send.await_args_list[0].args[3] == "Reply body"

    def test_lgpd_disabled_new_session_does_not_send_notice(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000102",
            body="Primeiro contato sem LGPD",
            message_id=f"3EB0_nolgpd_{uuid4().hex[:8]}",
        )

        with (
            patch(
                "src.api.routes.webhook._get_or_create_session",
                new=AsyncMock(return_value=(uuid4(), True)),
            ),
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(return_value=_fake_uazapi_send_result()),
            ) as mock_send,
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(
                return_value=_lgpd_config(enabled=False),
            )
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Reply body")
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True}
        # Only the reply — LGPD disabled means no aviso
        assert mock_send.await_count == 1
        assert mock_send.await_args_list[0].args[3] == "Reply body"

    def test_missing_uazapi_credentials_does_not_break_reply_path(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """AC: 'credenciais Uazapi ausentes: nenhum aviso, nenhuma exceção
        propagada — falha no envio do aviso não impede a resposta normal'.
        With no url/token, the aviso block is skipped silently and the
        handler eventually returns replied=False + no_credentials for the
        AI reply itself (the pipeline still runs).
        """
        instance = f"partial_{uuid4().hex[:8]}"
        # Seed the instance_name only so tenant resolves, but no url/token
        _seed_uazapi_credentials_missing_secrets(client, tenant_id, instance)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000103",
            body="Sem credenciais",
            message_id=f"3EB0_nocred_lgpd_{uuid4().hex[:8]}",
        )

        with (
            patch(
                "src.api.routes.webhook._get_or_create_session",
                new=AsyncMock(return_value=(uuid4(), True)),
            ),
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(return_value=_fake_uazapi_send_result()),
            ) as mock_send,
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(
                return_value=_lgpd_config(enabled=True),
            )
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Reply body")
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # AI reply path bails on no_credentials, aviso never attempted
        assert body["replied"] is False
        assert body["reason"] == "no_credentials"
        # send_uazapi_text was never called (no url/token)
        assert mock_send.await_count == 0

    def test_privacy_notice_language_pt_default(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000104",
            body="Oi",
            message_id=f"3EB0_pt_{uuid4().hex[:8]}",
        )

        with (
            patch(
                "src.api.routes.webhook._get_or_create_session",
                new=AsyncMock(return_value=(uuid4(), True)),
            ),
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(
                    side_effect=[
                        _fake_uazapi_send_result(),
                        _fake_uazapi_send_result(),
                    ],
                ),
            ) as mock_send,
        ):
            service = MagicMock()
            # No agent_language override → default is pt-BR
            service._load_clinic_config = AsyncMock(
                return_value=_lgpd_config(enabled=True),
            )
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Resposta")
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        first_text = mock_send.await_args_list[0].args[3]
        # PT default text mentions "LGPD" — the EN default does not
        assert "LGPD" in first_text

    def test_privacy_notice_language_en_selected(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000105",
            body="Hi",
            message_id=f"3EB0_en_{uuid4().hex[:8]}",
        )

        en_clinic_config = {
            "lgpd": {"lgpd_enabled": True},
            "identity": {"clinic_name": "Test Clinic"},
            "business_rules": {"agent_language": "en"},
        }

        with (
            patch(
                "src.api.routes.webhook._get_or_create_session",
                new=AsyncMock(return_value=(uuid4(), True)),
            ),
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(
                    side_effect=[
                        _fake_uazapi_send_result(),
                        _fake_uazapi_send_result(),
                    ],
                ),
            ) as mock_send,
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(
                return_value=en_clinic_config,
            )
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Reply")
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        first_text = mock_send.await_args_list[0].args[3]
        # EN default text starts with "Hello!" — PT starts with "Olá!"
        assert first_text.startswith("Hello")

    def test_privacy_send_failure_does_not_break_ai_reply(
        self,
        client: TestClient,
        tenant_id: UUID,
        caplog: pytest.LogCaptureFixture,
    ):
        """AC: 'falha no envio do aviso não impede a resposta normal da IA
        ao paciente'. The privacy send raises, the aviso helper swallows
        and logs, and the AI reply pipeline still runs.
        """
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000106",
            body="Continua respondendo",
            message_id=f"3EB0_privfail_{uuid4().hex[:8]}",
        )

        reply_send = _fake_uazapi_send_result()

        # First call (aviso) raises; second call (reply) succeeds
        send_mock = AsyncMock(
            side_effect=[RuntimeError("boom"), reply_send],
        )

        with (
            patch(
                "src.api.routes.webhook._get_or_create_session",
                new=AsyncMock(return_value=(uuid4(), True)),
            ),
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=send_mock,
            ),
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(
                return_value=_lgpd_config(enabled=True),
            )
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Reply body")
            )
            mock_get_service.return_value = service

            with caplog.at_level("WARNING", logger="src.api.routes.webhook"):
                resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True}
        # aviso attempted (raised) + reply (succeeded)
        assert send_mock.await_count == 2
        # Warning line for the failed aviso is present
        warn_msgs = [
            rec.getMessage() for rec in caplog.records if rec.levelname == "WARNING"
        ]
        assert any("Uazapi LGPD send failed" in m for m in warn_msgs), warn_msgs


class TestUazapiEchoIdNormalisation:
    """Group 1b Deliverable 6 + AC: composite ``owner:MSGID`` is normalised."""

    def test_composite_id_stored_without_owner_prefix(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """AC: Given send_result carrying only ``id='owner:MSGID'``, the
        value stored is ``MSGID`` (no prefix) and ``_track_sent_message``
        receives ``{"messageid": MSGID}``.
        """
        from src.api.routes import webhook as webhook_module

        webhook_module._INMEM_SENT.clear()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        session_id = uuid4()
        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000110",
            body="ping",
            message_id=f"3EB0_ping_{uuid4().hex[:8]}",
        )

        # send_result ONLY contains ``id`` in the composite form. This is
        # the exact payload shape captured from the live Uazapi instance.
        composite_send_result = {
            "id": "5511940589006:3EB08AA1C8596FA5A17973",
        }

        stored_ids: list[str] = []

        async def _store_spy(sid, mid, db):
            stored_ids.append(mid)

        with (
            patch(
                "src.api.routes.webhook._get_or_create_session",
                new=AsyncMock(return_value=(session_id, False)),
            ),
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(return_value=composite_send_result),
            ),
            patch(
                "src.api.routes.webhook._store_ai_msg_id_to_db",
                new=AsyncMock(side_effect=_store_spy),
            ),
            patch(
                "src.api.routes.webhook._track_sent_message",
            ) as mock_track,
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Reply")
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True}

        # The stored id must be the tail — never the composite form
        assert stored_ids == ["3EB08AA1C8596FA5A17973"], stored_ids
        assert all(":" not in v for v in stored_ids)

        # _track_sent_message received {"messageid": <normalized>}
        assert mock_track.call_count == 1
        tracked_arg = mock_track.call_args.args[0]
        assert tracked_arg == {"messageid": "3EB08AA1C8596FA5A17973"}
        # And critically — the value inside carries no colon
        assert ":" not in tracked_arg["messageid"]

    def test_privacy_notice_composite_id_normalised(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Same normalisation applies to the aviso send path, not just the
        reply. The aviso helper writes {"messageid": <normalized>} into
        _track_sent_message and stores the tail-only id in the DB.
        """
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        session_id = uuid4()
        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000111",
            body="Novo paciente",
            message_id=f"3EB0_newp_{uuid4().hex[:8]}",
        )

        # Aviso send result carries only composite ``id``; reply gets a
        # normal top-level ``messageid``.
        composite_aviso = {"id": "owner:PRIVACY_ID_TAIL"}
        reply_send = {"messageid": "REPLY_ID"}

        stored_ids: list[str] = []

        async def _store_spy(sid, mid, db):
            stored_ids.append(mid)

        with (
            patch(
                "src.api.routes.webhook._get_or_create_session",
                new=AsyncMock(return_value=(session_id, True)),
            ),
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(side_effect=[composite_aviso, reply_send]),
            ),
            patch(
                "src.api.routes.webhook._store_ai_msg_id_to_db",
                new=AsyncMock(side_effect=_store_spy),
            ),
            patch(
                "src.api.routes.webhook._track_sent_message",
            ) as mock_track,
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(
                return_value=_lgpd_config(enabled=True),
            )
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Reply")
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text

        # Both dedup layers agree on the aviso id and the reply id
        assert stored_ids == ["PRIVACY_ID_TAIL", "REPLY_ID"], stored_ids
        # None carry a colon — critical invariant
        assert all(":" not in v for v in stored_ids)

        # _track_sent_message called for aviso and reply, always with the
        # normalised messageid form
        tracked_calls = [c.args[0] for c in mock_track.call_args_list]
        assert tracked_calls == [
            {"messageid": "PRIVACY_ID_TAIL"},
            {"messageid": "REPLY_ID"},
        ]
        assert all(":" not in c["messageid"] for c in tracked_calls)


# ============================================
# uazapi-parity Group 3 — Human takeover via ``fromMe``
# ============================================


def _create_session(client: TestClient, tenant_id: UUID, phone: str) -> UUID:
    """Create an active session for ``phone`` on the app's own event loop.

    Mirrors the helper in ``test_webhook_egress.py``: takeover tests need a
    real session in the same DB the request handler will read, otherwise the
    fromMe path bottoms out in ``no_session`` and there's nothing to pause.
    """
    from src.api.routes.webhook import _get_or_create_session, _normalise_phone
    from src.repositories.database import get_db_session

    async def _run() -> UUID:
        db = get_db_session()
        try:
            session_id, _ = await _get_or_create_session(
                tenant_id, _normalise_phone(phone), db
            )
            await db.commit()
            return session_id
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined]


def _session_row(client: TestClient, session_id: UUID):
    """Load the raw session row in a *new* AsyncSession so assertions see the
    committed state, not the in-memory object the request handler mutated.
    """
    from sqlalchemy import select

    from src.models.session import Session as SessionRow
    from src.repositories.database import get_db_session

    async def _run():
        db = get_db_session()
        try:
            row = await db.execute(
                select(SessionRow.is_paused, SessionRow.paused_until).where(
                    SessionRow.id == session_id
                )
            )
            r = row.one()
            return {"is_paused": bool(r.is_paused), "paused_until": r.paused_until}
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined]


def _session_metadata(client: TestClient, session_id: UUID) -> dict:
    """Load ``session_metadata`` in a new AsyncSession — used by the aviso
    echo end-to-end test, which needs to plant the aviso id server-side."""
    from sqlalchemy import select

    from src.models.session import Session as SessionRow
    from src.repositories.database import get_db_session

    async def _run() -> dict:
        db = get_db_session()
        try:
            row = await db.execute(
                select(SessionRow.session_metadata).where(SessionRow.id == session_id)
            )
            return dict(row.scalar_one() or {})
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined]


def _seed_handoff_config(
    client: TestClient,
    tenant_id: UUID,
    handoff: dict[str, Any],
) -> None:
    """Persist an arbitrary legacy handoff bucket for runtime coverage."""
    from sqlalchemy import update

    from src.models.clinic_config_table import ClinicConfigTable
    from src.repositories.database import get_db_session

    async def _run() -> None:
        db = get_db_session()
        try:
            await db.execute(
                update(ClinicConfigTable)
                .where(ClinicConfigTable.tenant_id == tenant_id)
                .values(handoff=handoff)
            )
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]


def _seed_handoff_active(
    client: TestClient,
    tenant_id: UUID,
    *,
    active: str = "yes",
) -> None:
    """Save the legacy activation flag used by handoff-inactive coverage."""
    _seed_handoff_config(client, tenant_id, {"handoff_active": active})


class TestUazapiFromMeHumanTakeover:
    """Group 3 AC: a real clinic message via fromMe pauses the *patient's* session.

    Decision 3 governs the counterparty resolution — the patient is on the
    ``chat.wa_chatid`` side, never on ``sender_pn`` (which holds the clinic's
    own number when the clinic replies from its phone).
    """

    def _clear_state(self) -> None:
        from src.api.routes import webhook as webhook_module

        webhook_module._INMEM_INCOMING.clear()
        webhook_module._INMEM_SENT.clear()

    def test_from_me_pauses_patient_session_resolved_by_chatid(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        clinic_number = "5511940589006"  # sender_pn — the clinic's own line
        patient_number = "5516994420645"  # chatid — the patient we must pause

        # Session exists ONLY for the patient. No session exists for the
        # clinic number — asserted after the call.
        patient_session_id = _create_session(client, tenant_id, patient_number)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone=clinic_number,  # ignored for the fromMe path
            body="Vou te ligar em 5 min",
            message_id=f"3EB0_takeover_{uuid4().hex[:8]}",
            from_me=True,
            sender_pn=f"{clinic_number}@s.whatsapp.net",
            chat_jid=f"{patient_number}@s.whatsapp.net",
        )

        with (
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(return_value=_fake_uazapi_send_result()),
            ) as mock_send,
        ):
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json().get("action") == "human_takeover", resp.json()

        # DB round-trip: fresh AsyncSession, not the in-memory session obj —
        # this is the acceptance criterion that would fail without the
        # ``await db.commit()`` after ``_handle_clinic_message``.
        row = _session_row(client, patient_session_id)
        assert row["is_paused"] is True, row

        # Zero pipeline / send touch — takeover never enters the LLM path
        assert mock_get_service.call_count == 0
        assert mock_send.await_count == 0

    def test_public_from_me_endpoint_applies_fallback_with_safe_notices(
        self,
        client: TestClient,
        tenant_id: UUID,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)
        raw = "RAW-STANDBY-DO-NOT-LOG"
        _seed_handoff_config(
            client,
            tenant_id,
            {
                "handoff_active": "yes",
                "standby_mode": "active",
                "handoff_period": "cycle",
                "handoff_cycle_days": raw,
            },
        )
        clinic_number = "5511940589006"
        patient_number = "5516994420699"
        patient_session_id = _create_session(client, tenant_id, patient_number)
        payload = _uazapi_text_payload(
            instance=instance,
            from_phone=clinic_number,
            body="Atendimento humano em andamento",
            message_id=f"3EB0_fallback_{uuid4().hex[:8]}",
            from_me=True,
            sender_pn=f"{clinic_number}@s.whatsapp.net",
            chat_jid=f"{patient_number}@s.whatsapp.net",
        )
        before = datetime.now(timezone.utc)

        with caplog.at_level("WARNING", logger="src.api.routes.webhook"):
            response = client.post(WEBHOOK_PATH, json=payload)
        after = datetime.now(timezone.utc)

        assert response.status_code == 200, response.text
        assert response.json()["action"] == "human_takeover"
        row = _session_row(client, patient_session_id)
        paused_until = row["paused_until"]
        assert paused_until is not None
        if paused_until.tzinfo is None:
            paused_until = paused_until.replace(tzinfo=timezone.utc)
        assert before + timedelta(days=7) <= paused_until <= after + timedelta(days=7)

        notices = [
            record
            for record in caplog.records
            if record.getMessage() == "handoff_pause_policy_tolerated"
        ]
        assert {(record.field, record.reason) for record in notices} == {  # type: ignore[attr-defined]
            ("handoff_cycle_days", "invalid_duration"),
            ("handoff_cycle", "fallback_seven_days"),
        }
        for record in notices:
            rendered = repr(record.__dict__)
            assert raw not in rendered
            assert patient_number not in rendered

    def test_from_me_never_creates_session_for_clinic_number(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Deliverable 1 AC: the fromMe path resolves the patient via
        chatid, so no session is ever seeded for the clinic's own number.
        """
        from sqlalchemy import select

        from src.api.routes.webhook import _normalise_phone
        from src.models.session import Session as SessionRow
        from src.repositories.database import get_db_session

        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        clinic_number = "5511940589006"
        patient_number = "5516994420600"
        # Pre-seed only the patient's session
        _create_session(client, tenant_id, patient_number)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone=clinic_number,
            body="mensagem da secretária",
            message_id=f"3EB0_noclinic_{uuid4().hex[:8]}",
            from_me=True,
            sender_pn=f"{clinic_number}@s.whatsapp.net",
            chat_jid=f"{patient_number}@s.whatsapp.net",
        )

        with (
            patch(
                "src.api.routes.webhook.get_event_service",
            ),
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(return_value=_fake_uazapi_send_result()),
            ),
        ):
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text

        async def _count_clinic_sessions() -> int:
            db = get_db_session()
            try:
                row = await db.execute(
                    select(SessionRow.id).where(
                        SessionRow.tenant_id == tenant_id,
                        SessionRow.user_id == _normalise_phone(clinic_number),
                    )
                )
                return len(row.all())
            finally:
                await db.close()

        clinic_session_count = client._submit(_count_clinic_sessions())  # type: ignore[attr-defined]
        assert (
            clinic_session_count == 0
        ), "fromMe path must never create a session for the clinic's own number"


class TestUazapiFromMeAiEcho:
    """AC: an AI-sent reply echo is still recognised — no pause, no takeover."""

    def _clear_state(self) -> None:
        from src.api.routes import webhook as webhook_module

        webhook_module._INMEM_INCOMING.clear()
        webhook_module._INMEM_SENT.clear()

    def test_reply_echo_is_dedup_dropped_and_does_not_pause(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        from src.api.routes.webhook import _track_sent_message

        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        clinic_number = "5511940589006"
        patient_number = "5516994420601"
        patient_session_id = _create_session(client, tenant_id, patient_number)

        # Simulate an outbound AI reply whose id will echo back
        echo_id = f"3EB0_echo_{uuid4().hex[:12]}"
        _track_sent_message({"messageid": echo_id})

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone=clinic_number,
            body="Bot reply echoed back",
            message_id=echo_id,
            from_me=True,
            sender_pn=f"{clinic_number}@s.whatsapp.net",
            chat_jid=f"{patient_number}@s.whatsapp.net",
        )

        resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "ai_echo"}
        row = _session_row(client, patient_session_id)
        assert row["is_paused"] is False

    def test_privacy_notice_echo_end_to_end_does_not_pause(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """AC: sends the aviso (Group 1b commits its id), then simulates the
        aviso's own fromMe echo returning through the same webhook — nothing
        pauses. This is the exact scenario Decision 2 prevents.
        """
        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        clinic_number = "5511940589006"
        patient_number = "5516994420602"

        privacy_mid = f"3EB0_priv_echo_{uuid4().hex[:12]}"
        privacy_send = _fake_uazapi_send_result(message_id=privacy_mid)
        reply_send = _fake_uazapi_send_result()

        first_payload = _uazapi_text_payload(
            instance=instance,
            from_phone=patient_number,
            body="Primeiro contato",
            message_id=f"3EB0_first_{uuid4().hex[:8]}",
        )

        with (
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(side_effect=[privacy_send, reply_send]),
            ) as mock_send,
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(
                return_value=_lgpd_config(enabled=True),
            )
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Reply body")
            )
            mock_get_service.return_value = service

            first_resp = client.post(WEBHOOK_PATH, json=first_payload)

        assert first_resp.status_code == 200, first_resp.text
        assert mock_send.await_count == 2  # aviso + reply

        # The aviso id was committed to session_metadata by Group 1b —
        # verify from a NEW DB session (not the request's).
        from sqlalchemy import select

        from src.api.routes.webhook import _normalise_phone
        from src.models.session import Session as SessionRow
        from src.repositories.database import get_db_session

        async def _find_patient_session() -> UUID:
            db = get_db_session()
            try:
                row = await db.execute(
                    select(SessionRow.id).where(
                        SessionRow.tenant_id == tenant_id,
                        SessionRow.user_id == _normalise_phone(patient_number),
                    )
                )
                return row.scalar_one()
            finally:
                await db.close()

        session_id = client._submit(_find_patient_session())  # type: ignore[attr-defined]
        meta = _session_metadata(client, session_id)
        ids = [entry[0] for entry in (meta.get("ai_msg_ids") or [])]
        assert (
            privacy_mid in ids
        ), f"expected the aviso id in session_metadata, got {meta!r}"

        # Now the aviso's own echo returns as a fromMe delivery.
        # Sender_pn = clinic (owner of the echo), chatid = patient. The
        # _INMEM_SENT store was cleared by the first request's echo layer
        # (or was never written, since the aviso path stores via
        # _store_ai_msg_id_to_db). The DB-backed dedup inside
        # _handle_clinic_message is what has to catch this.
        echo_payload = _uazapi_text_payload(
            instance=instance,
            from_phone=clinic_number,
            body="[echoed LGPD notice]",
            message_id=privacy_mid,
            from_me=True,
            sender_pn=f"{clinic_number}@s.whatsapp.net",
            chat_jid=f"{patient_number}@s.whatsapp.net",
        )

        with patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service2:
            echo_resp = client.post(WEBHOOK_PATH, json=echo_payload)

        assert echo_resp.status_code == 200, echo_resp.text
        # Either the in-memory ``ai_echo`` skip or the DB-backed
        # ``ai_echo_db`` inside _handle_clinic_message — both mean the
        # echo was recognised and nothing paused.
        body = echo_resp.json()
        assert body.get("skipped") in {"ai_echo", "ai_echo_db"}, body

        row = _session_row(client, session_id)
        assert (
            row["is_paused"] is False
        ), "the aviso echo must never pause the session it just opened"
        assert mock_get_service2.call_count == 0

    def test_duplicate_delivery_of_same_echo_is_dropped_by_claim(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """AC (Decision 9): the provider re-delivers each message; the second
        delivery of the same fromMe echo must not sneak past the id-based
        layers (which *consume* on match) and pause the session. The lower
        bound of the ordering — claim before route — closes this window.
        """
        from src.api.routes.webhook import _track_sent_message

        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        clinic_number = "5511940589006"
        patient_number = "5516994420603"
        patient_session_id = _create_session(client, tenant_id, patient_number)

        echo_id = f"3EB0_dup_echo_{uuid4().hex[:12]}"
        _track_sent_message({"messageid": echo_id})

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone=clinic_number,
            body="Bot reply",
            message_id=echo_id,
            from_me=True,
            sender_pn=f"{clinic_number}@s.whatsapp.net",
            chat_jid=f"{patient_number}@s.whatsapp.net",
        )

        # Force in-memory claim path so the test isolates behaviour from a
        # possibly-real Redis (which the test env may or may not have).
        with patch(
            "src.api.routes.webhook._get_redis",
            return_value=None,
        ):
            first = client.post(WEBHOOK_PATH, json=payload)
            second = client.post(WEBHOOK_PATH, json=payload)

        assert first.status_code == 200
        assert first.json() == {"ok": True, "skipped": "ai_echo"}
        assert second.status_code == 200
        # Second delivery is barred by the claim (lower bound of Decision 9).
        # Without the reorder the id-based layers would have consumed the
        # entry on the first call and this delivery would pause the session.
        assert second.json() == {"ok": True, "skipped": "duplicate_message"}
        row = _session_row(client, patient_session_id)
        assert row["is_paused"] is False


class TestUazapiFromMeStaffAudioTakeover:
    """AC: the Group 2 audio carve-out finally produces a pause on Group 3."""

    def _clear_state(self) -> None:
        from src.api.routes import webhook as webhook_module

        webhook_module._INMEM_INCOMING.clear()
        webhook_module._INMEM_SENT.clear()

    def test_staff_voice_note_pauses_session(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        clinic_number = "5511940589006"
        patient_number = "5516994420604"
        patient_session_id = _create_session(client, tenant_id, patient_number)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone=clinic_number,
            body="",
            text="",
            content="",
            message_id=f"3EB0_staff_ptt_{uuid4().hex[:8]}",
            msg_type="media",
            media_type="audio",
            ptt=True,
            from_me=True,
            sender_pn=f"{clinic_number}@s.whatsapp.net",
            chat_jid=f"{patient_number}@s.whatsapp.net",
        )

        resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json().get("action") == "human_takeover", resp.json()
        row = _session_row(client, patient_session_id)
        assert row["is_paused"] is True


class TestUazapiFromMeWasSentByApiGuard:
    """AC: fromMe *without* a messageid is not a takeover if wasSentByApi=True.

    The claim is conditional on ``msg_id`` (``if msg_id and not
    _claim_incoming_message(...)``); an idless fromMe would slip past it and
    also past ``_is_ai_sent_message`` (which needs an id). Decision 8's
    ``wasSentByApi`` is the guard that covers this exact hole.
    """

    def _clear_state(self) -> None:
        from src.api.routes import webhook as webhook_module

        webhook_module._INMEM_INCOMING.clear()
        webhook_module._INMEM_SENT.clear()

    def test_idless_echo_with_was_sent_by_api_true_is_dropped(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        clinic_number = "5511940589006"
        patient_number = "5516994420605"
        patient_session_id = _create_session(client, tenant_id, patient_number)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone=clinic_number,
            body="idless echo",
            message_id="",  # exact hole the guard covers
            from_me=True,
            sender_pn=f"{clinic_number}@s.whatsapp.net",
            chat_jid=f"{patient_number}@s.whatsapp.net",
            was_sent_by_api=True,
        )

        # Re-deliver the same payload — without wasSentByApi both deliveries
        # would pause; with it, both must be dropped as ai_echo.
        first = client.post(WEBHOOK_PATH, json=payload)
        second = client.post(WEBHOOK_PATH, json=payload)

        assert first.status_code == 200
        assert first.json() == {"ok": True, "skipped": "ai_echo"}
        assert second.status_code == 200
        assert second.json() == {"ok": True, "skipped": "ai_echo"}

        row = _session_row(client, patient_session_id)
        assert (
            row["is_paused"] is False
        ), "idless echo with wasSentByApi must never pause the session"


class TestUazapiFromMeTriggerReactivation:
    """AC: trigger phrase from the clinic re-arms the session (paused_until in
    future). Only reachable because fromMe routing runs *before*
    _check_pause_state — the upper bound of Decision 9's ordering.
    """

    def _clear_state(self) -> None:
        from src.api.routes import webhook as webhook_module

        webhook_module._INMEM_INCOMING.clear()
        webhook_module._INMEM_SENT.clear()

    def _pause_session(self, client: TestClient, session_id: UUID) -> None:
        from sqlalchemy import update

        from src.models.session import Session as SessionRow
        from src.repositories.database import get_db_session

        async def _run() -> None:
            db = get_db_session()
            try:
                await db.execute(
                    update(SessionRow)
                    .where(SessionRow.id == session_id)
                    .values(is_paused=True, paused_until=None)
                )
                await db.commit()
            finally:
                await db.close()

        client._submit(_run())  # type: ignore[attr-defined]

    def test_trigger_phrase_reactivates_paused_session(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        clinic_number = "5511940589006"
        patient_number = "5516994420606"
        patient_session_id = _create_session(client, tenant_id, patient_number)
        self._pause_session(client, patient_session_id)

        # The default trigger from _handle_clinic_message is
        # "Precisando de algo mais,estamos a disposição." — normalization
        # collapses spaces and strips whitespace around punctuation, so an
        # extra space here is tolerated.
        trigger_text = "Precisando de algo mais, estamos a disposição."
        payload = _uazapi_text_payload(
            instance=instance,
            from_phone=clinic_number,
            body=trigger_text,
            message_id=f"3EB0_trigger_{uuid4().hex[:8]}",
            from_me=True,
            sender_pn=f"{clinic_number}@s.whatsapp.net",
            chat_jid=f"{patient_number}@s.whatsapp.net",
        )

        resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json().get("action") == "trigger_reactivation", resp.json()

        row = _session_row(client, patient_session_id)
        assert row["is_paused"] is True  # still paused, but with a future clock
        paused_until = row["paused_until"]
        assert paused_until is not None
        # SQLite drops the tz info; the production DB (Postgres) preserves it.
        # Normalize so the assertion is meaningful on both — what matters is
        # the wall clock, not the tzinfo attribute.
        if paused_until.tzinfo is None:
            paused_until = paused_until.replace(tzinfo=timezone.utc)
        assert paused_until > datetime.now(timezone.utc)


class TestUazapiFromMeHandoffInactive:
    """AC: when the tenant disabled handoff, fromMe never pauses the session."""

    def _clear_state(self) -> None:
        from src.api.routes import webhook as webhook_module

        webhook_module._INMEM_INCOMING.clear()
        webhook_module._INMEM_SENT.clear()

    def test_handoff_inactive_leaves_session_alone(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)
        _seed_handoff_active(client, tenant_id, active="no")

        clinic_number = "5511940589006"
        patient_number = "5516994420607"
        patient_session_id = _create_session(client, tenant_id, patient_number)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone=clinic_number,
            body="secretaria falando",
            message_id=f"3EB0_hoff_off_{uuid4().hex[:8]}",
            from_me=True,
            sender_pn=f"{clinic_number}@s.whatsapp.net",
            chat_jid=f"{patient_number}@s.whatsapp.net",
        )

        resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "handoff_inactive"}
        row = _session_row(client, patient_session_id)
        assert row["is_paused"] is False


def _patient_events(client: TestClient, session_id: UUID) -> list[dict[str, Any]]:
    """Return every event row for a session as dicts, ordered by created_at.

    Reads back through a *new* ``AsyncSession`` (mirrors ``_session_row``) so
    assertions see the committed state rather than anything cached on the
    request handler's own session.
    """
    from sqlalchemy import select

    from src.models.event import Event as EventRow
    from src.repositories.database import get_db_session

    async def _run() -> list[dict[str, Any]]:
        db = get_db_session()
        try:
            result = await db.execute(
                select(EventRow)
                .where(EventRow.session_id == session_id)
                .order_by(EventRow.created_at)
            )
            rows = list(result.scalars().all())
            return [
                {
                    "event_type": r.event_type,
                    "event_data": dict(r.event_data or {}),
                    "event_metadata": dict(r.event_metadata or {}),
                }
                for r in rows
            ]
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined]


class TestUazapiPausedPatientMessageRetention:
    """AC12 (D2): a patient message arriving mid-pause is retained, not
    dropped, yet stays out of ``prior_episode_context``/the AI's automatic
    history composer.

    Before D2, ``_check_pause_state`` returned the ``session_paused`` skip
    dict without writing anything — the message simply vanished. It now
    persists a ``message.in`` event tagged ``paused_capture: True`` using the
    session's existing event retention, while the externally visible skip
    response is unchanged (non-regression check).
    """

    def _clear_state(self) -> None:
        from src.api.routes import webhook as webhook_module

        webhook_module._INMEM_INCOMING.clear()
        webhook_module._INMEM_SENT.clear()

    def _pause_session(self, client: TestClient, session_id: UUID) -> None:
        from sqlalchemy import update

        from src.models.session import Session as SessionRow
        from src.repositories.database import get_db_session

        async def _run() -> None:
            db = get_db_session()
            try:
                await db.execute(
                    update(SessionRow)
                    .where(SessionRow.id == session_id)
                    .values(is_paused=True, paused_until=None)
                )
                await db.commit()
            finally:
                await db.close()

        client._submit(_run())  # type: ignore[attr-defined]

    def test_patient_message_persisted_during_pause(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        """AC12: the skip response is unchanged, but the message is now
        persisted instead of silently dropped."""
        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        patient_number = "5516994420699"
        session_id = _create_session(client, tenant_id, patient_number)
        self._pause_session(client, session_id)

        message_text = "Oi, ainda estou esperando a resposta"
        payload = _uazapi_text_payload(
            instance=instance,
            from_phone=patient_number,
            body=message_text,
            message_id=f"3EB0_paused_{uuid4().hex[:10]}",
        )

        resp = client.post(WEBHOOK_PATH, json=payload)

        # Externally-visible contract is unchanged — only the internal
        # persistence changed.
        assert resp.status_code == 202, resp.text
        assert resp.json() == {"ok": True, "skipped": "session_paused"}

        events = _patient_events(client, session_id)
        matching = [
            e
            for e in events
            if e["event_type"] == "message.in"
            and e["event_data"].get("content") == message_text
        ]
        assert matching, events
        assert matching[0]["event_metadata"].get("paused_capture") is True

    def test_paused_capture_message_excluded_from_ai_person_history(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        """AC12: the retained message stays out of the automatic AI context,
        proven end-to-end against a message that went through the real
        webhook route (not a hand-built ``Event``).

        A sanity-control ordinary ``message.in`` event (no ``paused_capture``
        flag) is seeded for the same session and must still surface in the
        composer's output — proving the filter discriminates rather than the
        history simply being empty.
        """
        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        patient_number = "5516994420700"
        session_id = _create_session(client, tenant_id, patient_number)
        self._pause_session(client, session_id)

        paused_message_text = "Alguém pode me responder, por favor?"
        payload = _uazapi_text_payload(
            instance=instance,
            from_phone=patient_number,
            body=paused_message_text,
            message_id=f"3EB0_paused_ctx_{uuid4().hex[:10]}",
        )

        resp = client.post(WEBHOOK_PATH, json=payload)
        assert resp.status_code == 202, resp.text
        assert resp.json() == {"ok": True, "skipped": "session_paused"}

        control_text = "Mensagem de controle sem paused_capture"

        async def _seed_control() -> None:
            from src.repositories import EventRepository
            from src.repositories.database import get_db_session

            db = get_db_session()
            try:
                await EventRepository(db).create(
                    tenant_id=tenant_id,
                    session_id=session_id,
                    event_type="message.in",
                    event_data={
                        "content": control_text,
                        "channel": "whatsapp",
                        "user_id": patient_number,
                        "role": "user",
                    },
                    event_metadata={},
                )
                await db.commit()
            finally:
                await db.close()

        client._submit(_seed_control())  # type: ignore[attr-defined]

        async def _load_history() -> list[dict[str, str]]:
            from src.api.routes.webhook import _load_ai_person_history
            from src.repositories.database import get_db_session

            db = get_db_session()
            try:
                return await _load_ai_person_history(
                    tenant_id, session_id, db, limit=20
                )
            finally:
                await db.close()

        history = client._submit(_load_history())  # type: ignore[attr-defined]

        contents = [h.get("content") for h in history]
        assert paused_message_text not in contents, history
        assert control_text in contents, history


class TestUazapiPatientMessageUnchanged:
    """AC: a normal patient message (fromMe=false) keeps today's behaviour."""

    def _clear_state(self) -> None:
        from src.api.routes import webhook as webhook_module

        webhook_module._INMEM_INCOMING.clear()
        webhook_module._INMEM_SENT.clear()

    def test_patient_message_still_dispatches_pipeline(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        msg_id = f"3EB0_pat_{uuid4().hex[:10]}"
        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511988887710",
            body="Quero agendar",
            message_id=msg_id,
        )

        with (
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(return_value=_fake_uazapi_send_result()),
            ) as mock_send,
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Claro!")
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True}
        assert service.run_pipeline.await_count == 1
        assert mock_send.await_count == 1


# ============================================
# Group 4 — Patient audio ingestion (media-payload.json driven)
# ============================================


def _seed_uazapi_audio_tenant(
    client: TestClient,
    tenant_id: UUID,
    instance_name: str,
    *,
    stt: bool = False,
    stt_provider: str | None = None,
    multimodal: bool = False,
    multimodal_provider: str = "openrouter",
    audio_max_seconds: int | None = None,
    api_url: str = "https://uazapi.example.com",
    token: str | None = None,
) -> None:
    """Seed a real credential row for the Uazapi audio path.

    Mirrors ``_seed_zapi_audio_tenant`` in ``test_webhook_zapi.py``: no
    mocking of ``_load_tenant_credentials`` — the point is that the webhook
    reads raw credentials from the DB. Keys are per-test unique and
    obviously fake.
    """
    payload: dict = {
        "whatsapp_provider": "uazapi",
        "uazapi_api_url": api_url,
        "uazapi_token": token or f"uz_token_{uuid4().hex}",
        "uazapi_instance_name": instance_name,
    }
    if stt or stt_provider is not None:
        provider = stt_provider or "outros"
        fixed_bundle = {
            "elevenlabs_scribe": (
                "https://api.elevenlabs.io/v1",
                "scribe_v2",
            ),
            "openrouter": (
                "https://openrouter.ai/api/v1",
                "openai/whisper-large-v3-turbo",
            ),
        }.get(provider)
        payload.update(
            {
                "stt_provider": provider,
                "stt_api_key": f"sk-fake-stt-{uuid4().hex}",
                "stt_api_url": (
                    fixed_bundle[0]
                    if fixed_bundle
                    else "https://api.groq.com/openai/v1"
                ),
                "stt_model_name": (
                    fixed_bundle[1] if fixed_bundle else "whisper-large-v3-turbo"
                ),
            }
        )
    if multimodal:
        payload.update(
            {
                "multimodal_provider": multimodal_provider,
                "multimodal_api_url": "https://openrouter.ai/api/v1",
                "multimodal_api_key": f"sk-fake-mm-{uuid4().hex}",
                "multimodal_model_name": "google/gemini-2.5-flash",
            }
        )
    if audio_max_seconds is not None:
        payload["audio_max_seconds"] = audio_max_seconds

    _seed_legacy_uazapi_credentials(client, tenant_id, payload)


def _build_uazapi_media_body(
    kind: str,
    *,
    instance: str,
    message_id: str | None = None,
    from_me: bool | None = None,
    seconds_override: int | None = None,
) -> dict:
    """Return the sanitized Uazapi fixture body with minimal test edits.

    Only ``instanceName`` (required to route the test's tenant) and the
    optional ``fromMe`` / ``messageid`` / ``content.seconds`` are touched;
    everything else retains the captured schema with synthetic scalar values.
    """
    body = _load_media_evidence(kind)
    body["instanceName"] = instance
    if message_id is not None:
        body["message"]["messageid"] = message_id
        # ``id`` is the composite ``owner:MSGID`` form; keep the tail
        # aligned so any downstream normalisation stays consistent.
        owner = body["message"].get("owner") or body.get("owner") or ""
        body["message"]["id"] = f"{owner}:{message_id}" if owner else message_id
    if from_me is not None:
        body["message"]["fromMe"] = from_me
    if seconds_override is not None:
        content = body["message"].setdefault("content", {})
        content["seconds"] = seconds_override
    return body


class TestUazapiAudioWhenEgressClosed:
    def _clear_state(self) -> None:
        webhook_module._INMEM_INCOMING.clear()
        webhook_module._INMEM_SENT.clear()
        webhook_module._INMEM_STT_NOTICE.clear()

    @pytest.mark.parametrize("flag_failure", ["false", "error"])
    def test_blocked_download_is_retryable_without_consuming_replay_claim(
        self,
        client: TestClient,
        tenant_id: UUID,
        flag_failure: str,
    ) -> None:
        from src.services.event_service import EventService

        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_audio_tenant(client, tenant_id, instance, stt=True)
        payload = _build_uazapi_media_body(
            "audio",
            instance=instance,
            message_id=f"3EB0_closed_download_{uuid4().hex[:8]}",
        )
        flag_reader = AsyncMock(
            return_value=False,
            side_effect=(
                RuntimeError("agent unavailable") if flag_failure == "error" else None
            ),
        )
        safe_client = MagicMock()
        run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
        finish_response = AsyncMock(return_value=_fake_event_response(None))

        with (
            patch("src.api.routes.webhook._get_redis", return_value=None),
            patch(
                "src.services.egress_policy.AppConfigClient.is_enabled",
                flag_reader,
            ),
            patch("src.services.uazapi_service.safe_async_client", safe_client),
        ):
            blocked = client.post(WEBHOOK_PATH, json=payload)

        assert blocked.status_code == 503, blocked.text
        assert blocked.json() == {
            "ok": False,
            "error": "uazapi_egress_disabled",
            "retryable": True,
        }
        flag_reader.assert_awaited_once_with("uazapi_egress_enabled")
        safe_client.assert_not_called()

        with (
            patch("src.api.routes.webhook._get_redis", return_value=None),
            patch(
                "src.api.routes.webhook.download_uazapi_media",
                new=AsyncMock(return_value=(b"OggS_retry", "audio/ogg")),
            ) as download,
            patch(
                "src.services.groq_service.transcribe_audio",
                new=AsyncMock(return_value="transcrição após retry"),
            ),
            patch.object(EventService, "run_pipeline", run_pipeline),
            patch.object(EventService, "finish_response", finish_response),
        ):
            retried = client.post(WEBHOOK_PATH, json=payload)
            duplicate = client.post(WEBHOOK_PATH, json=payload)

        assert retried.status_code == 200, retried.text
        assert retried.json() == {"ok": True, "replied": False}
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json() == {"ok": True, "skipped": "duplicate_message"}
        assert download.await_count == 2
        run_pipeline.assert_awaited_once()
        finish_response.assert_awaited_once()
        self._clear_state()

    def test_closed_stt_fallback_does_not_attempt_an_alternate_send(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_audio_tenant(
            client,
            tenant_id,
            instance,
            stt=True,
            multimodal=True,
        )
        payload = _build_uazapi_media_body(
            "audio",
            instance=instance,
            message_id=f"3EB0_closed_fallback_{uuid4().hex[:8]}",
        )
        safe_client = MagicMock()

        with (
            patch("src.api.routes.webhook._get_redis", return_value=None),
            patch(
                "src.api.routes.webhook.download_uazapi_media",
                new=AsyncMock(return_value=(b"OggS_failed", "audio/ogg")),
            ),
            patch(
                "src.services.groq_service.transcribe_audio",
                new=AsyncMock(side_effect=RuntimeError("stt failed")),
            ),
            patch(
                "src.services.multimodal_service.transcribe_audio_multimodal",
                new=AsyncMock(side_effect=RuntimeError("multimodal failed")),
            ),
            patch(
                "src.services.egress_policy.AppConfigClient.is_enabled",
                new=AsyncMock(return_value=False),
            ),
            patch("src.services.uazapi_service.safe_async_client", safe_client),
        ):
            response = client.post(WEBHOOK_PATH, json=payload)

        assert response.status_code == 200, response.text
        assert response.json() == {
            "ok": True,
            "replied": False,
            "action": "stt_fallback_suppressed",
            "reason": "uazapi_egress_disabled",
        }
        safe_client.assert_not_called()
        self._clear_state()

    def test_closed_long_audio_notice_is_acknowledged_without_download(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_audio_tenant(
            client,
            tenant_id,
            instance,
            stt=True,
            audio_max_seconds=10,
        )
        payload = _build_uazapi_media_body(
            "audio",
            instance=instance,
            message_id=f"3EB0_closed_long_{uuid4().hex[:8]}",
            seconds_override=30,
        )
        download = AsyncMock()
        safe_client = MagicMock()

        with (
            patch("src.api.routes.webhook._get_redis", return_value=None),
            patch("src.api.routes.webhook.download_uazapi_media", download),
            patch(
                "src.services.egress_policy.AppConfigClient.is_enabled",
                new=AsyncMock(side_effect=RuntimeError("agent unavailable")),
            ),
            patch("src.services.uazapi_service.safe_async_client", safe_client),
        ):
            response = client.post(WEBHOOK_PATH, json=payload)
            duplicate = client.post(WEBHOOK_PATH, json=payload)

        assert response.status_code == 200, response.text
        assert response.json() == {
            "ok": True,
            "skipped": "audio_too_long",
            "reason": "uazapi_egress_disabled",
        }
        assert duplicate.status_code == 200, duplicate.text
        assert duplicate.json() == {"ok": True, "skipped": "duplicate_message"}
        download.assert_not_awaited()
        safe_client.assert_not_called()
        self._clear_state()

    def test_closed_tts_audio_does_not_fallback_to_text(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_legacy_uazapi_credentials(
            client,
            tenant_id,
            {
                "whatsapp_provider": "uazapi",
                "uazapi_api_url": "https://uazapi.example.com",
                "uazapi_token": f"uz_token_{uuid4().hex}",
                "uazapi_instance_name": instance,
                "tts_enabled": True,
                "tts_reply_policy": "all_inputs",
            },
        )
        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511988887777",
            body="Responder em áudio",
            message_id=f"3EB0_closed_tts_{uuid4().hex[:8]}",
        )
        send_text = AsyncMock()
        safe_client = MagicMock()

        with (
            patch("src.api.routes.webhook._get_redis", return_value=None),
            patch("src.api.routes.webhook.get_event_service") as mock_get_service,
            patch(
                "src.api.routes.webhook.synthesize_speech",
                new=AsyncMock(
                    return_value=SimpleNamespace(
                        content=b"audio",
                        mime_type="audio/mpeg",
                    )
                ),
            ),
            patch("src.api.routes.webhook.send_uazapi_text", send_text),
            patch(
                "src.services.egress_policy.AppConfigClient.is_enabled",
                new=AsyncMock(return_value=False),
            ),
            patch("src.services.uazapi_service.safe_async_client", safe_client),
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Resposta em áudio")
            )
            mock_get_service.return_value = service
            response = client.post(WEBHOOK_PATH, json=payload)

        assert response.status_code == 200, response.text
        assert response.json() == {
            "ok": True,
            "replied": False,
            "reason": "uazapi_egress_disabled",
        }
        service.run_pipeline.assert_awaited_once()
        service.finish_response.assert_awaited_once()
        send_text.assert_not_awaited()
        safe_client.assert_not_called()
        self._clear_state()


class TestUazapiGroup4MediaEvidenceShape:
    """Sanity-check the loaded evidence so downstream asserts have a firm base."""

    def test_fixture_scalars_are_recursively_sanitized(self):
        document = json.loads(_MEDIA_PAYLOAD_PATH.read_text(encoding="utf-8"))

        assert _UUID.search("TEST_abcdefab-cdef-abcd-efab-cdefabcdefab")
        assert _DOMAIN.findall("TEST_evil.com") == ["evil.com"]

        for path, value in _iter_fixture_scalars(document):
            key = path[-1]
            location = ".".join(path)
            normalized_key = re.sub(r"(?<!^)(?=[A-Z])", "_", key).lower()

            if isinstance(value, str):
                assert not _LONG_DIGIT_RUN.search(value), location
                assert not _UUID.search(value), location

                domains = {match.lower() for match in _DOMAIN.findall(value)}
                assert domains <= {"example.invalid"}, (location, domains)
                if "://" in value:
                    assert _EXAMPLE_URL.fullmatch(value), location

                if value:
                    allowed = (
                        value.startswith(("TEST_", "REDACTED_"))
                        or _EXAMPLE_URL.fullmatch(value) is not None
                        or _SEMANTIC_STRINGS.get(path) == value
                    )
                    assert allowed, (location, value)

            if _TIMESTAMP_KEY.search(normalized_key):
                assert type(value) is int, location
                assert 0 <= value <= 10, location

    def test_evidence_file_shape_matches_group_expectations(self):
        audio = _load_media_evidence("audio")
        image = _load_media_evidence("image")

        # Captured payload semantics Group 4 depends on
        assert audio["message"]["fromMe"] is False
        assert audio["message"]["mediaType"] == "ptt"
        assert audio["message"]["type"] == "media"
        assert audio["message"]["content"]["mimetype"] == "audio/ogg; codecs=opus"
        assert isinstance(audio["message"]["content"]["seconds"], int)
        assert audio["message"]["messageid"]  # non-empty id

        assert image["message"]["fromMe"] is False
        assert image["message"]["mediaType"] == "image"
        assert image["message"]["type"] == "media"
        assert image["message"]["content"]["mimetype"] == "image/jpeg"


class TestUazapiGroup4NonAudioMedia:
    """Non-audio media exits as ``non-text`` (Z-API reference, not Meta's
    file-ack). Uses the sanitized captured-shape *image* fixture."""

    def _clear_state(self) -> None:
        from src.api.routes import webhook as webhook_module

        webhook_module._INMEM_INCOMING.clear()
        webhook_module._INMEM_SENT.clear()
        webhook_module._INMEM_STT_NOTICE.clear()

    def test_patient_image_exits_as_non_text_without_download(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_audio_tenant(
            client, tenant_id, instance, stt=True, multimodal=True
        )

        payload = _build_uazapi_media_body(
            "image",
            instance=instance,
            message_id=f"3EB0_img_{uuid4().hex[:10]}",
        )

        download_mock = AsyncMock()
        send_mock = AsyncMock(return_value=_fake_uazapi_send_result())
        with (
            patch("src.api.routes.webhook.download_uazapi_media", download_mock),
            patch("src.api.routes.webhook.send_uazapi_text", send_mock),
            patch("src.api.routes.webhook.get_event_service") as mock_get_service,
        ):
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "skipped": "non-text"}
        download_mock.assert_not_called()
        send_mock.assert_not_called()
        assert mock_get_service.call_count == 0


class TestUazapiGroup4UnknownInstanceStopsBeforeDownload:
    """AC: media from an unknown instance never triggers a download."""

    def _clear_state(self) -> None:
        from src.api.routes import webhook as webhook_module

        webhook_module._INMEM_INCOMING.clear()
        webhook_module._INMEM_SENT.clear()
        webhook_module._INMEM_STT_NOTICE.clear()

    def test_unknown_instance_audio_never_downloads(
        self,
        client: TestClient,
        tenant_id: UUID,  # unused: the point is that the tenant isn't wired
    ):
        self._clear_state()
        # No _seed_uazapi_audio_tenant call — the instanceName below never
        # matches any credential row, so tenant resolution returns None.
        payload = _build_uazapi_media_body(
            "audio",
            instance=f"unknown_{uuid4().hex[:8]}",
            message_id=f"3EB0_orphan_{uuid4().hex[:10]}",
        )

        download_mock = AsyncMock()
        send_mock = AsyncMock(return_value=_fake_uazapi_send_result())
        with (
            patch("src.api.routes.webhook.download_uazapi_media", download_mock),
            patch("src.api.routes.webhook.send_uazapi_text", send_mock),
        ):
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "skipped": "unknown_instance"}
        download_mock.assert_not_called()
        send_mock.assert_not_called()


class TestUazapiGroup4PatientAudioTranscription:
    """AC: patient audio is transcribed and reaches the pipeline; on chain
    failure the text reroute takes over — never silence."""

    def _clear_state(self) -> None:
        from src.api.routes import webhook as webhook_module

        webhook_module._INMEM_INCOMING.clear()
        webhook_module._INMEM_SENT.clear()
        webhook_module._INMEM_STT_NOTICE.clear()

    @pytest.mark.parametrize(
        ("provider", "api_url", "model_name"),
        [
            (
                "elevenlabs_scribe",
                "https://api.elevenlabs.io/v1",
                "scribe_v2",
            ),
            (
                "openrouter",
                "https://openrouter.ai/api/v1",
                "openai/whisper-large-v3-turbo",
            ),
        ],
    )
    def test_fixed_provider_http_dispatch_feeds_pipeline_with_raw_audio(
        self,
        client: TestClient,
        tenant_id: UUID,
        provider: str,
        api_url: str,
        model_name: str,
    ) -> None:
        """Captured-shape Uazapi audio reaches each fixed adapter, never Groq."""
        from src.services.event_service import EventService

        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_audio_tenant(
            client,
            tenant_id,
            instance,
            stt_provider=provider,
        )
        payload = _build_uazapi_media_body(
            "audio",
            instance=instance,
            message_id=f"3EB0_fixed_{uuid4().hex[:10]}",
        )
        raw_mime = "audio/ogg; codecs=opus"
        audio_bytes = b"OggS_uazapi_fixed_provider"
        transcript = f"transcript from {provider}"
        fixed_adapter = AsyncMock(return_value=transcript)
        legacy_adapter = AsyncMock()
        captured: dict[str, str] = {}

        async def _capture(self, event_request, session=None):
            captured["content"] = event_request.payload.content
            return MagicMock(), {}, []

        no_reply = SimpleNamespace(
            prompt=None,
            handoff_summary=None,
            decision=SimpleNamespace(handoff_required=False),
            response_language="pt-BR",
        )

        with (
            patch(
                "src.api.routes.webhook.download_uazapi_media",
                new=AsyncMock(return_value=(audio_bytes, raw_mime)),
            ),
            patch("src.services.stt_service.transcribe_audio", fixed_adapter),
            patch("src.services.groq_service.transcribe_audio", legacy_adapter),
            patch.object(EventService, "run_pipeline", _capture),
            patch.object(
                EventService,
                "finish_response",
                AsyncMock(return_value=no_reply),
            ),
        ):
            response = client.post(WEBHOOK_PATH, json=payload)

        assert response.status_code == 200, response.text
        assert response.json().get("action") != "stt_fallback"
        assert captured == {"content": transcript}
        fixed_adapter.assert_awaited_once()
        kwargs = fixed_adapter.await_args.kwargs
        assert kwargs["provider"] == provider
        assert kwargs["api_url"] == api_url
        assert kwargs["api_key"].startswith("sk-fake-stt-")
        assert kwargs["audio_bytes"] == audio_bytes
        assert kwargs["mimetype"] == raw_mime
        assert kwargs["model_name"] == model_name
        # Language hint must NOT be forwarded: the fixed STT adapters are now
        # driven in autodetect mode so audio turns feed the same PT/EN
        # decision machine the text turns feed, with no translation.
        assert "language" not in kwargs or kwargs["language"] is None
        legacy_adapter.assert_not_awaited()

    @pytest.mark.parametrize("provider", ["elevenlabs_scribe", "openrouter"])
    def test_fixed_provider_operational_error_runs_multimodal_then_reroutes(
        self,
        client: TestClient,
        tenant_id: UUID,
        provider: str,
    ) -> None:
        """A closed fixed-adapter error preserves STT→multimodal→text order."""
        from src.api.routes.webhook import _STT_TEMPORARY_FAILURE_MSG_PT
        from src.services.stt_service import SttTranscriptionError

        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_audio_tenant(
            client,
            tenant_id,
            instance,
            stt_provider=provider,
            multimodal=True,
        )
        payload = _build_uazapi_media_body(
            "audio",
            instance=instance,
            message_id=f"3EB0_fixed_fail_{uuid4().hex[:10]}",
        )
        fixed_adapter = AsyncMock(
            side_effect=SttTranscriptionError("stt_provider_http_error")
        )
        legacy_adapter = AsyncMock()
        multimodal = AsyncMock(side_effect=RuntimeError("multimodal unavailable"))
        send = AsyncMock(return_value=_fake_uazapi_send_result())

        with (
            patch(
                "src.api.routes.webhook.download_uazapi_media",
                new=AsyncMock(
                    return_value=(b"OggS_uazapi_failed", "audio/ogg; codecs=opus")
                ),
            ),
            patch("src.services.stt_service.transcribe_audio", fixed_adapter),
            patch("src.services.groq_service.transcribe_audio", legacy_adapter),
            patch(
                "src.services.multimodal_service.transcribe_audio_multimodal",
                multimodal,
            ),
            patch("src.api.routes.webhook.send_uazapi_text", send),
        ):
            response = client.post(WEBHOOK_PATH, json=payload)

        assert response.status_code == 200, response.text
        assert response.json() == {
            "ok": True,
            "replied": True,
            "action": "stt_fallback",
        }
        assert "stt_provider_http_error" not in response.text
        assert "multimodal unavailable" not in response.text
        fixed_adapter.assert_awaited_once()
        assert fixed_adapter.await_args.kwargs["provider"] == provider
        assert fixed_adapter.await_args.kwargs["mimetype"] == ("audio/ogg; codecs=opus")
        legacy_adapter.assert_not_awaited()
        multimodal.assert_awaited_once()
        send.assert_awaited_once()
        assert send.await_args.args[3] == _STT_TEMPORARY_FAILURE_MSG_PT

    @pytest.mark.parametrize(
        "provider",
        ["google_stt", "deepgram_stt", "microsoft_stt"],
    )
    def test_unsupported_provider_is_zero_effect_before_uazapi_downstream(
        self,
        client: TestClient,
        tenant_id: UUID,
        provider: str,
    ) -> None:
        """Unsupported STT exits before download, adapters, reply or session."""
        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_audio_tenant(
            client,
            tenant_id,
            instance,
            stt_provider=provider,
            multimodal=True,
        )
        payload = _build_uazapi_media_body(
            "audio",
            instance=instance,
            message_id=f"3EB0_unsupported_{uuid4().hex[:10]}",
        )
        download = AsyncMock()
        fixed_adapter = AsyncMock()
        legacy_adapter = AsyncMock()
        chain = AsyncMock()
        send = AsyncMock()
        reply = AsyncMock()
        session = AsyncMock()
        fallback = AsyncMock()

        with (
            patch("src.api.routes.webhook.download_uazapi_media", download),
            patch("src.services.stt_service.transcribe_audio", fixed_adapter),
            patch("src.services.groq_service.transcribe_audio", legacy_adapter),
            patch("src.api.routes.webhook._transcribe_audio_chain", chain),
            patch("src.api.routes.webhook.send_uazapi_text", send),
            patch("src.api.routes.webhook._send_reply_with_optional_tts", reply),
            patch("src.api.routes.webhook._get_or_create_session", session),
            patch("src.api.routes.webhook._send_uazapi_stt_fallback", fallback),
        ):
            response = client.post(WEBHOOK_PATH, json=payload)

        assert response.status_code == 200, response.text
        assert response.json() == {
            "ok": True,
            "skipped": "stt_provider_unsupported",
            "provider": provider,
        }
        download.assert_not_awaited()
        fixed_adapter.assert_not_awaited()
        legacy_adapter.assert_not_awaited()
        chain.assert_not_awaited()
        send.assert_not_awaited()
        reply.assert_not_awaited()
        session.assert_not_awaited()
        fallback.assert_not_awaited()

    def test_patient_audio_transcript_feeds_pipeline(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """STT succeeds: transcript is what the pipeline sees, no reroute."""
        from src.services.event_service import EventService

        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_audio_tenant(client, tenant_id, instance, stt=True)

        payload = _build_uazapi_media_body(
            "audio",
            instance=instance,
            message_id=f"3EB0_pttok_{uuid4().hex[:10]}",
        )

        captured: dict = {}

        async def _capture(self, event_request, session=None):
            captured["content"] = event_request.payload.content
            return MagicMock(), {}, []

        no_reply = SimpleNamespace(
            prompt=None,
            handoff_summary=None,
            decision=SimpleNamespace(handoff_required=False),
            response_language="pt-BR",
        )

        download_mock = AsyncMock(
            return_value=(b"OggS_fake_bytes", "audio/ogg; codecs=opus")
        )
        send_mock = AsyncMock(return_value=_fake_uazapi_send_result())

        with (
            patch("src.api.routes.webhook.download_uazapi_media", download_mock),
            patch(
                "src.services.groq_service.transcribe_audio",
                new=AsyncMock(return_value="quero remarcar minha consulta"),
            ),
            patch("src.api.routes.webhook.send_uazapi_text", send_mock),
            patch.object(EventService, "run_pipeline", _capture),
            patch.object(
                EventService,
                "finish_response",
                AsyncMock(return_value=no_reply),
            ),
        ):
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        # No prompt.suggested_response -> replied=False, but no fallback either
        assert resp.json().get("action") != "stt_fallback", resp.json()
        assert download_mock.await_count == 1
        assert captured.get("content") == "quero remarcar minha consulta"
        send_mock.assert_not_called()

    def test_patient_audio_chain_failure_triggers_text_reroute(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Both rungs fail — the patient gets the ``fallback-texto`` copy."""
        from src.api.routes.webhook import _STT_TEMPORARY_FAILURE_MSG_PT

        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_audio_tenant(
            client, tenant_id, instance, stt=True, multimodal=True
        )

        payload = _build_uazapi_media_body(
            "audio",
            instance=instance,
            message_id=f"3EB0_pttfail_{uuid4().hex[:10]}",
        )

        download_mock = AsyncMock(
            return_value=(b"OggS_fake_bytes", "audio/ogg; codecs=opus")
        )
        send_mock = AsyncMock(return_value=_fake_uazapi_send_result())
        tts_gate = AsyncMock()

        with (
            patch("src.api.routes.webhook.download_uazapi_media", download_mock),
            patch(
                "src.services.groq_service.transcribe_audio",
                new=AsyncMock(side_effect=RuntimeError("stt 503")),
            ),
            patch(
                "src.services.multimodal_service.transcribe_audio_multimodal",
                new=AsyncMock(side_effect=RuntimeError("mm 429")),
            ),
            patch("src.api.routes.webhook.send_uazapi_text", send_mock),
            patch(
                "src.api.routes.webhook._send_reply_with_optional_tts",
                tts_gate,
            ),
        ):
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True, "action": "stt_fallback"}
        assert send_mock.await_count == 1
        assert send_mock.await_args.args[3] == _STT_TEMPORARY_FAILURE_MSG_PT
        tts_gate.assert_not_awaited()

    def test_patient_audio_download_error_skips_with_labelled_reason(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Uazapi ``download_uazapi_media`` failure: no chain, distinct skip."""
        from src.services.uazapi_service import UazapiMediaDownloadError

        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_audio_tenant(client, tenant_id, instance, stt=True)

        payload = _build_uazapi_media_body(
            "audio",
            instance=instance,
            message_id=f"3EB0_pttdl_{uuid4().hex[:10]}",
        )

        download_mock = AsyncMock(
            side_effect=UazapiMediaDownloadError("http_error: 500")
        )
        send_mock = AsyncMock(return_value=_fake_uazapi_send_result())
        stt_mock = AsyncMock()

        with (
            patch("src.api.routes.webhook.download_uazapi_media", download_mock),
            patch("src.services.groq_service.transcribe_audio", stt_mock),
            patch("src.api.routes.webhook.send_uazapi_text", send_mock),
        ):
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "skipped": "audio_download_failed"}
        stt_mock.assert_not_called()
        send_mock.assert_not_called()

    def test_patient_audio_long_duration_hands_off_without_downloading(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Duration gate outranks capability: no download, handoff message."""
        from src.api.routes.webhook import _AUDIO_TOO_LONG_MSG_PT

        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_audio_tenant(
            client, tenant_id, instance, stt=True, audio_max_seconds=60
        )

        payload = _build_uazapi_media_body(
            "audio",
            instance=instance,
            message_id=f"3EB0_pttlong_{uuid4().hex[:10]}",
            seconds_override=180,
        )

        download_mock = AsyncMock()
        send_mock = AsyncMock(return_value=_fake_uazapi_send_result())

        with (
            patch("src.api.routes.webhook.download_uazapi_media", download_mock),
            patch("src.api.routes.webhook.send_uazapi_text", send_mock),
        ):
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "skipped": "audio_too_long"}
        download_mock.assert_not_called()
        assert send_mock.await_count == 1
        assert send_mock.await_args.args[3] == _AUDIO_TOO_LONG_MSG_PT

    def test_patient_audio_no_capability_reroutes_without_downloading(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Decision 6 honest variant: capability gate holds, chain reports it."""
        from src.api.routes.webhook import _STT_NEVER_CONFIGURED_MSG_PT

        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_audio_tenant(client, tenant_id, instance)  # neither STT nor MM

        payload = _build_uazapi_media_body(
            "audio",
            instance=instance,
            message_id=f"3EB0_pttnocap_{uuid4().hex[:10]}",
        )

        download_mock = AsyncMock()
        send_mock = AsyncMock(return_value=_fake_uazapi_send_result())
        with (
            patch("src.api.routes.webhook.download_uazapi_media", download_mock),
            patch("src.api.routes.webhook.send_uazapi_text", send_mock),
        ):
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True, "action": "stt_fallback"}
        # Capability gate: not one byte fetched from Uazapi
        download_mock.assert_not_called()
        assert send_mock.await_count == 1
        assert send_mock.await_args.args[3] == _STT_NEVER_CONFIGURED_MSG_PT


class TestUazapiGroup4FromMeAudioUntouched:
    """AC: fromMe audio (staff voice note) never downloads / transcribes /
    reroutes AND still pauses the session. Both halves asserted here."""

    def _clear_state(self) -> None:
        from src.api.routes import webhook as webhook_module

        webhook_module._INMEM_INCOMING.clear()
        webhook_module._INMEM_SENT.clear()
        webhook_module._INMEM_STT_NOTICE.clear()

    def test_from_me_audio_bypasses_download_and_pauses_session(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_audio_tenant(
            client, tenant_id, instance, stt=True, multimodal=True
        )

        clinic_number = "5511940589006"
        patient_number = "5516994420699"
        patient_session_id = _create_session(client, tenant_id, patient_number)

        # Captured-shape audio fixture, retargeted to fromMe with the clinic as sender
        # and the patient as counterparty (matches Group 3's fromMe routing).
        payload = _build_uazapi_media_body(
            "audio",
            instance=instance,
            message_id=f"3EB0_staff_evidence_{uuid4().hex[:10]}",
            from_me=True,
        )
        payload["message"]["sender_pn"] = f"{clinic_number}@s.whatsapp.net"
        payload["chat"]["wa_chatid"] = f"{patient_number}@s.whatsapp.net"
        payload["message"]["chatid"] = f"{patient_number}@s.whatsapp.net"

        download_mock = AsyncMock()
        stt_mock = AsyncMock()
        mm_mock = AsyncMock()
        send_mock = AsyncMock(return_value=_fake_uazapi_send_result())

        with (
            patch("src.api.routes.webhook.download_uazapi_media", download_mock),
            patch("src.services.groq_service.transcribe_audio", stt_mock),
            patch(
                "src.services.multimodal_service.transcribe_audio_multimodal", mm_mock
            ),
            patch("src.api.routes.webhook.send_uazapi_text", send_mock),
        ):
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json().get("action") == "human_takeover", resp.json()
        # Half 1: no download / STT / MM / reroute happened
        download_mock.assert_not_called()
        stt_mock.assert_not_called()
        mm_mock.assert_not_called()
        send_mock.assert_not_called()
        # Half 2: the patient's session got paused
        row = _session_row(client, patient_session_id)
        assert row["is_paused"] is True


class TestInactiveQueueReactivationRuntimeRegression:
    """The queue command and common webhook pause lookup choose the same row."""

    def test_reactivated_effective_duplicate_continues_to_ai_pipeline(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        from sqlalchemy import update

        from src.api.routes import webhook as webhook_module
        from src.models.session import Session as SessionRow
        from src.repositories.database import get_db_session

        webhook_module._INMEM_INCOMING.clear()
        webhook_module._INMEM_SENT.clear()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)
        phone = "5511988887722"
        old_session_id = _create_session(client, tenant_id, phone)
        effective_session_id = UUID(int=old_session_id.int + 1)
        now = datetime.now(timezone.utc)

        async def _seed_duplicate() -> None:
            db = get_db_session()
            try:
                await db.execute(
                    update(SessionRow)
                    .where(SessionRow.id == old_session_id)
                    .values(
                        is_paused=True,
                        paused_until=None,
                        last_activity_at=now,
                    )
                )
                db.add(
                    SessionRow(
                        id=effective_session_id,
                        tenant_id=tenant_id,
                        user_id=phone,
                        channel="whatsapp",
                        cognitive_state={"funnel_stage": "lead_novo"},
                        session_metadata={"source": "webhook"},
                        last_activity_at=now,
                        is_active=True,
                        is_paused=True,
                        paused_until=None,
                    )
                )
                await db.commit()
            finally:
                await db.close()

        client._submit(_seed_duplicate())  # type: ignore[attr-defined]

        reactivated = client.post(
            f"/v1/sessions/{effective_session_id}/reactivate",
            headers=_admin_headers(tenant_id),
        )
        assert reactivated.status_code == 200, reactivated.text
        assert reactivated.json()["status"] == "reactivated"

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone=phone,
            body="Quero continuar o atendimento",
            message_id=f"3EB0_reactivated_{uuid4().hex[:8]}",
        )
        with (
            patch("src.api.routes.webhook.get_event_service") as mock_get_service,
            patch(
                "src.api.routes.webhook.send_uazapi_text",
                new=AsyncMock(return_value=_fake_uazapi_send_result()),
            ),
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Atendimento retomado")
            )
            mock_get_service.return_value = service

            response = client.post(WEBHOOK_PATH, json=payload)

        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "replied": True}
        assert service.run_pipeline.await_count == 1
        event = service.run_pipeline.await_args.args[0]
        assert event.session_id == effective_session_id
        assert _session_row(client, effective_session_id)["is_paused"] is False
        assert _session_row(client, old_session_id)["is_paused"] is True


class TestUazapiVoiceReplyOrigin:
    @staticmethod
    def _clear_state() -> None:
        from src.api.routes import webhook as webhook_module

        webhook_module._INMEM_INCOMING.clear()
        webhook_module._INMEM_SENT.clear()
        webhook_module._INMEM_STT_NOTICE.clear()

    def test_text_reply_reaches_gate_as_non_audio(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)
        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000390",
            body="Olá",
            message_id=f"3EB0_text_{uuid4().hex[:8]}",
        )
        reply_gate = AsyncMock(
            return_value=(_fake_uazapi_send_result("voice_gate_text"), False),
        )

        with (
            patch(
                "src.api.routes.webhook._get_or_create_session",
                new=AsyncMock(return_value=(uuid4(), False)),
            ),
            patch("src.api.routes.webhook.get_event_service") as get_service,
            patch(
                "src.api.routes.webhook._send_reply_with_optional_tts",
                reply_gate,
            ),
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Resposta")
            )
            get_service.return_value = service
            response = client.post(WEBHOOK_PATH, json=payload)

        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "replied": True}
        reply_gate.assert_awaited_once()
        assert reply_gate.await_args.kwargs["input_was_audio"] is False

    def test_transcribed_patient_audio_keeps_audio_origin(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        self._clear_state()
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_audio_tenant(client, tenant_id, instance, stt=True)
        payload = _build_uazapi_media_body(
            "audio",
            instance=instance,
            message_id=f"3EB0_voice_{uuid4().hex[:8]}",
        )
        reply_gate = AsyncMock(
            return_value=(_fake_uazapi_send_result("voice_gate_audio"), True),
        )

        with (
            patch(
                "src.api.routes.webhook.download_uazapi_media",
                new=AsyncMock(return_value=(b"OggS_voice", "audio/ogg")),
            ),
            patch(
                "src.api.routes.webhook._transcribe_audio_chain",
                new=AsyncMock(return_value=("áudio transcrito", None)),
            ),
            patch(
                "src.api.routes.webhook._get_or_create_session",
                new=AsyncMock(return_value=(uuid4(), False)),
            ),
            patch("src.api.routes.webhook.get_event_service") as get_service,
            patch(
                "src.api.routes.webhook._send_reply_with_optional_tts",
                reply_gate,
            ),
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Resposta")
            )
            get_service.return_value = service
            response = client.post(WEBHOOK_PATH, json=payload)

        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "replied": True}
        reply_gate.assert_awaited_once()
        assert reply_gate.await_args.kwargs["input_was_audio"] is True
