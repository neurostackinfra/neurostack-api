"""
NeuroStack API — Meta Cloud API Webhook Tests

Covers Groups 2 + 4 of the evolution-whatsapp-integration wish:
- GET /v1/webhook/meta handshake (accepts on matching verify_token, rejects
  with 403 otherwise)
- POST /v1/webhook/meta text flow (tenant resolution by phone_number_id,
  pipeline dispatch, reply via Graph API)
- Echo dedup consistent with Z-API/Evolution (a message id we just sent must
  not trigger a second pipeline run)
- bot_active=False and pause_state active both suppress automatic replies
- The Meta ``messages[].id`` shows up in the receipt log so operations can
  trace "patient never got a reply" without opening the Meta dashboard

Group 4 additions (media handling):
- Audio via Meta is resolved by ``media_id``, downloaded, transcribed via
  the shared Whisper client, and processed as normal text
- Audio exceeding ``audio_max_seconds`` (approximated by byte size) triggers
  a handoff message + real HandoffService call — never a 500
- File/attachment (image, PDF, Word…) is never downloaded; the bot replies
  with the fixed acknowledgement (personalised with ``lead_name`` when
  known) and marks the session as awaiting the patient's choice
- Choosing "forward it to your team" initiates a real handoff and sends a
  confirmation
- Media-specific log parity: ``logger.warning`` on media_id resolve
  failures, ``logger.exception`` on transcription failures
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from src.config.settings import settings


API_CRED_PATH = "/v1/config/api-credentials"
WEBHOOK_PATH = "/v1/webhook/meta"


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
            "email": "admin@meta-webhook-test.com",
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


def _seed_meta_credentials(
    client: TestClient,
    tenant_id: UUID,
    phone_number_id: str,
) -> str:
    """Save Meta Cloud API credentials for a tenant and return the auto-generated verify_token."""
    resp = client.patch(
        API_CRED_PATH,
        json={
            "whatsapp_provider": "meta_cloud",
            "meta_phone_number_id": phone_number_id,
            "meta_waba_id": f"waba_{uuid4().hex[:8]}",
            "meta_permanent_token": f"EAAG_test_token_{uuid4().hex}",
        },
        headers=_admin_headers(tenant_id),
    )
    assert resp.status_code == 200, resp.text
    verify_token = resp.json()["meta_verify_token"]
    assert verify_token
    return verify_token


def _meta_text_payload(
    *,
    phone_number_id: str,
    from_phone: str,
    text_body: str,
    message_id: str,
    contact_name: str = "Paciente",
) -> dict:
    """Build a Meta Cloud API messages payload for the given inputs."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": f"waba_{uuid4().hex[:8]}",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": from_phone,
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [
                                {
                                    "profile": {"name": contact_name},
                                    "wa_id": from_phone,
                                }
                            ],
                            "messages": [
                                {
                                    "from": from_phone,
                                    "id": message_id,
                                    "timestamp": "1711900000",
                                    "text": {"body": text_body},
                                    "type": "text",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _fake_event_response(suggested: str | None = "Olá, tudo bem?"):
    """Return a MagicMock shaped like an EventService.process_event result.

    We only need ``response.prompt.suggested_response`` for the webhook to
    decide whether to reply. ``handoff_summary`` and ``decision.handoff_required``
    are also read (to decide mark_unread on Z-API paths); for the Meta path we
    only exercise the reply branch, so we return a benign shape.
    """
    prompt = SimpleNamespace(suggested_response=suggested)
    decision = SimpleNamespace(handoff_required=False)
    return SimpleNamespace(
        prompt=prompt,
        handoff_summary=None,
        decision=decision,
    )


def _fake_meta_send_result(message_id: str | None = None) -> dict:
    """Return a payload shaped like the Graph API messages send response."""
    return {
        "messaging_product": "whatsapp",
        "contacts": [{"input": "5511999999999", "wa_id": "5511999999999"}],
        "messages": [{"id": message_id or f"wamid.reply_{uuid4().hex[:10]}"}],
    }


@pytest.fixture
def tenant_id() -> UUID:
    """Fresh tenant per test so each ClinicConfigTable row is isolated."""
    return uuid4()


# ============================================
# GET /v1/webhook/meta — handshake
# ============================================


class TestMetaWebhookVerify:
    def test_handshake_success_echoes_challenge(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        phone_id = f"pn_{uuid4().hex[:8]}"
        verify_token = _seed_meta_credentials(client, tenant_id, phone_id)

        resp = client.get(
            WEBHOOK_PATH,
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": verify_token,
                "hub.challenge": "challenge_ABC_123",
            },
        )
        assert resp.status_code == 200
        # Meta expects raw plain-text challenge, not JSON
        assert resp.text == "challenge_ABC_123"
        assert resp.headers["content-type"].startswith("text/plain")

    def test_handshake_rejects_bad_token(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        _seed_meta_credentials(client, tenant_id, f"pn_{uuid4().hex[:8]}")

        resp = client.get(
            WEBHOOK_PATH,
            params={
                "hub.mode": "subscribe",
                "hub.verify_token": "totally_wrong_token",
                "hub.challenge": "challenge_XYZ",
            },
        )
        assert resp.status_code == 403

    def test_handshake_rejects_missing_mode(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        verify_token = _seed_meta_credentials(
            client, tenant_id, f"pn_{uuid4().hex[:8]}",
        )
        # ``hub.mode`` must equal ``subscribe`` — anything else is rejected
        resp = client.get(
            WEBHOOK_PATH,
            params={
                "hub.mode": "delete",
                "hub.verify_token": verify_token,
                "hub.challenge": "challenge_XYZ",
            },
        )
        assert resp.status_code == 403


# ============================================
# POST /v1/webhook/meta — text flow
# ============================================


class TestMetaWebhookTextFlow:
    def test_text_message_dispatches_pipeline_and_replies(
        self,
        client: TestClient,
        tenant_id: UUID,
        caplog: pytest.LogCaptureFixture,
    ):
        phone_id = f"pn_{uuid4().hex[:8]}"
        _seed_meta_credentials(client, tenant_id, phone_id)

        wamid = f"wamid.HBgN_{uuid4().hex[:10]}"
        payload = _meta_text_payload(
            phone_number_id=phone_id,
            from_phone="5511999998888",
            text_body="Quero agendar uma consulta",
            message_id=wamid,
        )

        with patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service, patch(
            "src.api.routes.webhook.send_meta_cloud_text",
            new=AsyncMock(return_value=_fake_meta_send_result()),
        ) as mock_send:
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.process_event = AsyncMock(
                return_value=_fake_event_response("Claro, para quando gostaria?"),
            )
            mock_get_service.return_value = service

            with caplog.at_level("INFO", logger="src.api.routes.webhook"):
                resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body == {"ok": True, "replied": True}

        # Pipeline was called with the parsed text
        assert service.process_event.await_count == 1
        event_arg = service.process_event.await_args.args[0]
        assert event_arg.payload.content == "Quero agendar uma consulta"
        assert event_arg.payload.metadata["source"] == "meta_cloud"
        assert event_arg.payload.metadata["messageId"] == wamid

        # Reply was sent via Graph API
        assert mock_send.await_count == 1
        pnid_arg, token_arg, phone_arg, text_arg = mock_send.await_args.args
        assert pnid_arg == phone_id
        assert token_arg.startswith("EAAG_test_token_")
        assert phone_arg == "5511999998888"
        assert text_arg == "Claro, para quando gostaria?"

        # The Meta message id is captured on the receipt log line for tracing
        receipt_logs = [
            rec for rec in caplog.records
            if "Meta webhook received message" in rec.getMessage()
        ]
        assert receipt_logs, "expected a receipt log line"
        assert wamid in receipt_logs[0].getMessage()

    def test_unknown_phone_number_id_warns_and_skips(
        self,
        client: TestClient,
        tenant_id: UUID,
        caplog: pytest.LogCaptureFixture,
    ):
        # Seed a clinic with a *different* phone_number_id
        _seed_meta_credentials(client, tenant_id, f"pn_owned_{uuid4().hex[:8]}")

        wamid = f"wamid.HBgN_{uuid4().hex[:10]}"
        stranger_pn = f"pn_stranger_{uuid4().hex[:8]}"
        payload = _meta_text_payload(
            phone_number_id=stranger_pn,
            from_phone="5511900000001",
            text_body="Olá",
            message_id=wamid,
        )

        with caplog.at_level("WARNING", logger="src.api.routes.webhook"):
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "unknown_phone_number_id"}
        # Parity with Z-API/Evolution "no tenant for" warning
        warn_msgs = [rec.getMessage() for rec in caplog.records if rec.levelname == "WARNING"]
        assert any(
            "no tenant for phone_number_id" in m and stranger_pn in m
            for m in warn_msgs
        ), warn_msgs

    def test_audio_without_media_id_is_skipped(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Audio type is accepted (Group 4) but a payload without media_id
        still short-circuits without touching the STT client."""
        phone_id = f"pn_{uuid4().hex[:8]}"
        _seed_meta_credentials(client, tenant_id, phone_id)

        payload = _meta_text_payload(
            phone_number_id=phone_id,
            from_phone="5511900000002",
            text_body="ignored",
            message_id=f"wamid.audio_{uuid4().hex[:8]}",
        )
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["type"] = "audio"
        payload["entry"][0]["changes"][0]["value"]["messages"][0].pop("text", None)

        resp = client.post(WEBHOOK_PATH, json=payload)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "missing_media_id"}

    def test_unknown_type_is_skipped_as_non_text(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Types outside text/audio/image/document/video/sticker fall through."""
        phone_id = f"pn_{uuid4().hex[:8]}"
        _seed_meta_credentials(client, tenant_id, phone_id)

        payload = _meta_text_payload(
            phone_number_id=phone_id,
            from_phone="5511900000010",
            text_body="ignored",
            message_id=f"wamid.loc_{uuid4().hex[:8]}",
        )
        payload["entry"][0]["changes"][0]["value"]["messages"][0]["type"] = "location"
        payload["entry"][0]["changes"][0]["value"]["messages"][0].pop("text", None)

        resp = client.post(WEBHOOK_PATH, json=payload)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "non-text"}

    def test_status_only_events_are_ignored(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Delivery/read receipts arrive as ``statuses`` without ``messages``."""
        phone_id = f"pn_{uuid4().hex[:8]}"
        _seed_meta_credentials(client, tenant_id, phone_id)

        payload = {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": f"waba_{uuid4().hex[:8]}",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "5511900000003",
                                    "phone_number_id": phone_id,
                                },
                                "statuses": [
                                    {
                                        "id": f"wamid.status_{uuid4().hex[:8]}",
                                        "status": "delivered",
                                        "timestamp": "1711900000",
                                        "recipient_id": "5511900000003",
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
        resp = client.post(WEBHOOK_PATH, json=payload)
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "status_only"}

    def test_send_failure_is_logged_via_logger_exception(
        self,
        client: TestClient,
        tenant_id: UUID,
        caplog: pytest.LogCaptureFixture,
    ):
        phone_id = f"pn_{uuid4().hex[:8]}"
        _seed_meta_credentials(client, tenant_id, phone_id)

        payload = _meta_text_payload(
            phone_number_id=phone_id,
            from_phone="5511900000004",
            text_body="oi",
            message_id=f"wamid.fail_{uuid4().hex[:8]}",
        )

        with patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service, patch(
            "src.api.routes.webhook.send_meta_cloud_text",
            new=AsyncMock(side_effect=RuntimeError("graph.facebook.com 500")),
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.process_event = AsyncMock(
                return_value=_fake_event_response("Reply text"),
            )
            mock_get_service.return_value = service

            with caplog.at_level("ERROR", logger="src.api.routes.webhook"):
                resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["replied"] is False
        assert body["reason"] == "send_failed"

        # logger.exception → ERROR level with stack trace attached
        error_records = [rec for rec in caplog.records if rec.levelname == "ERROR"]
        assert error_records, "expected an ERROR-level log for send failure"
        assert any(
            "Meta send failed" in rec.getMessage() for rec in error_records
        )


# ============================================
# POST /v1/webhook/meta — echo dedup
# ============================================


class TestMetaWebhookEchoDedup:
    def test_incoming_message_matching_ai_sent_id_is_dropped(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """A message id we recorded as bot-sent must not trigger the pipeline."""
        from src.api.routes import webhook as webhook_module

        phone_id = f"pn_{uuid4().hex[:8]}"
        _seed_meta_credentials(client, tenant_id, phone_id)

        echo_id = f"wamid.echo_{uuid4().hex[:8]}"
        # Simulate that we just sent this message id
        webhook_module._track_sent_message({"messageId": echo_id})

        payload = _meta_text_payload(
            phone_number_id=phone_id,
            from_phone="5511900000005",
            text_body="loop echo",
            message_id=echo_id,
        )

        with patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service, patch(
            "src.api.routes.webhook.send_meta_cloud_text",
            new=AsyncMock(return_value=_fake_meta_send_result()),
        ) as mock_send:
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.process_event = AsyncMock(
                return_value=_fake_event_response("should never fire"),
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "ai_echo"}
        # Neither the pipeline nor the send helper is called for an echo
        assert service.process_event.await_count == 0
        assert mock_send.await_count == 0


# ============================================
# POST /v1/webhook/meta — bot_active + pause
# ============================================


class TestMetaWebhookBotActiveAndPause:
    def test_bot_paused_blocks_reply(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """When ``_check_bot_active`` returns a skip dict, no reply is sent."""
        phone_id = f"pn_{uuid4().hex[:8]}"
        _seed_meta_credentials(client, tenant_id, phone_id)

        payload = _meta_text_payload(
            phone_number_id=phone_id,
            from_phone="5511900000006",
            text_body="testing bot pause",
            message_id=f"wamid.paused_{uuid4().hex[:8]}",
        )

        with patch(
            "src.api.routes.webhook._check_bot_active",
            new=AsyncMock(return_value={"ok": True, "skipped": "bot_paused"}),
        ), patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service, patch(
            "src.api.routes.webhook.send_meta_cloud_text",
            new=AsyncMock(return_value=_fake_meta_send_result()),
        ) as mock_send:
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.process_event = AsyncMock(
                return_value=_fake_event_response("blocked"),
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        # Same 202 semantics as Z-API/Evolution when bot is paused
        assert resp.status_code == 202
        assert resp.json() == {"ok": True, "skipped": "bot_paused"}
        assert service.process_event.await_count == 0
        assert mock_send.await_count == 0

    def test_session_paused_blocks_reply(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """When ``_check_pause_state`` returns a skip dict, no reply is sent."""
        phone_id = f"pn_{uuid4().hex[:8]}"
        _seed_meta_credentials(client, tenant_id, phone_id)

        payload = _meta_text_payload(
            phone_number_id=phone_id,
            from_phone="5511900000007",
            text_body="handoff in progress",
            message_id=f"wamid.session_paused_{uuid4().hex[:8]}",
        )

        with patch(
            "src.api.routes.webhook._check_pause_state",
            new=AsyncMock(
                return_value={"ok": True, "skipped": "session_paused"},
            ),
        ), patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service, patch(
            "src.api.routes.webhook.send_meta_cloud_text",
            new=AsyncMock(return_value=_fake_meta_send_result()),
        ) as mock_send:
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.process_event = AsyncMock(
                return_value=_fake_event_response("blocked"),
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 202
        assert resp.json() == {"ok": True, "skipped": "session_paused"}
        assert service.process_event.await_count == 0
        assert mock_send.await_count == 0


# ============================================
# POST /v1/webhook/meta — media (Group 4)
# ============================================


def _meta_audio_payload(
    *,
    phone_number_id: str,
    from_phone: str,
    media_id: str,
    message_id: str,
    mime_type: str = "audio/ogg; codecs=opus",
    voice: bool = True,
) -> dict:
    """Build a Meta Cloud API audio message payload."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": f"waba_{uuid4().hex[:8]}",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": from_phone,
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [
                                {
                                    "profile": {"name": "Paciente"},
                                    "wa_id": from_phone,
                                }
                            ],
                            "messages": [
                                {
                                    "from": from_phone,
                                    "id": message_id,
                                    "timestamp": "1711900000",
                                    "type": "audio",
                                    "audio": {
                                        "id": media_id,
                                        "mime_type": mime_type,
                                        "sha256": "abc",
                                        "voice": voice,
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _meta_media_payload(
    *,
    phone_number_id: str,
    from_phone: str,
    media_id: str,
    message_id: str,
    msg_type: str = "image",
    mime_type: str | None = None,
    filename: str | None = None,
) -> dict:
    """Build a Meta Cloud API media (image/document/video) message payload."""
    if mime_type is None:
        mime_type = {
            "image": "image/jpeg",
            "document": "application/pdf",
            "video": "video/mp4",
            "sticker": "image/webp",
        }.get(msg_type, "application/octet-stream")

    media_obj: dict = {
        "id": media_id,
        "mime_type": mime_type,
        "sha256": "sha_placeholder",
    }
    if msg_type == "document" and filename:
        media_obj["filename"] = filename

    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": f"waba_{uuid4().hex[:8]}",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": from_phone,
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [
                                {
                                    "profile": {"name": "Paciente"},
                                    "wa_id": from_phone,
                                }
                            ],
                            "messages": [
                                {
                                    "from": from_phone,
                                    "id": message_id,
                                    "timestamp": "1711900000",
                                    "type": msg_type,
                                    msg_type: media_obj,
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def _seed_meta_credentials_with_whisper(
    client: TestClient,
    tenant_id: UUID,
    phone_number_id: str,
    whisper_key: str = "gsk_test_whisper_key",
    audio_max_seconds: int = 60,
) -> str:
    """Save Meta credentials + Whisper key + audio_max_seconds for a tenant."""
    resp = client.patch(
        API_CRED_PATH,
        json={
            "whatsapp_provider": "meta_cloud",
            "meta_phone_number_id": phone_number_id,
            "meta_waba_id": f"waba_{uuid4().hex[:8]}",
            "meta_permanent_token": f"EAAG_test_token_{uuid4().hex}",
            "whisper_api_key": whisper_key,
            "audio_max_seconds": audio_max_seconds,
        },
        headers=_admin_headers(tenant_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["meta_verify_token"]


class TestMetaWebhookAudioFlow:
    def test_audio_is_resolved_downloaded_transcribed_and_replied(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Full happy path: media_id → URL → bytes → Whisper → text pipeline → reply."""
        phone_id = f"pn_{uuid4().hex[:8]}"
        _seed_meta_credentials_with_whisper(client, tenant_id, phone_id)

        media_id = f"media_{uuid4().hex[:12]}"
        wamid = f"wamid.audio_{uuid4().hex[:10]}"
        payload = _meta_audio_payload(
            phone_number_id=phone_id,
            from_phone="5511900001111",
            media_id=media_id,
            message_id=wamid,
        )

        # ~5 second audio at 16 KB/s = well under the 60s default cap
        fake_media_info = {
            "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=xyz",
            "mime_type": "audio/ogg; codecs=opus",
            "file_size": 5 * 16000,
            "sha256": "abc",
        }
        fake_audio_bytes = b"OggS\x00\x02" + b"\x00" * (5 * 16000 - 6)

        with patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service, patch(
            "src.api.routes.webhook.resolve_meta_media",
            new=AsyncMock(return_value=fake_media_info),
        ) as mock_resolve, patch(
            "src.api.routes.webhook.download_meta_media",
            new=AsyncMock(return_value=fake_audio_bytes),
        ) as mock_download, patch(
            "src.api.routes.webhook._meta_transcribe_audio",
            new=AsyncMock(return_value="Quero remarcar minha consulta"),
        ) as mock_transcribe, patch(
            "src.api.routes.webhook.send_meta_cloud_text",
            new=AsyncMock(return_value=_fake_meta_send_result()),
        ) as mock_send:
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.process_event = AsyncMock(
                return_value=_fake_event_response("Sem problemas, para quando?"),
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True}

        # Media resolution used the correct media_id + tenant token
        assert mock_resolve.await_count == 1
        assert mock_resolve.await_args.args[0] == media_id
        assert mock_download.await_count == 1
        assert mock_download.await_args.args[0] == fake_media_info["url"]

        # Transcription received bytes + mime + creds
        assert mock_transcribe.await_count == 1
        _, kw = mock_transcribe.call_args
        assert kw["audio_bytes"] == fake_audio_bytes
        assert kw["mime_type"] == "audio/ogg; codecs=opus"

        # Pipeline got the transcribed text
        assert service.process_event.await_count == 1
        event_arg = service.process_event.await_args.args[0]
        assert event_arg.payload.content == "Quero remarcar minha consulta"

        # Reply was sent
        assert mock_send.await_count == 1
        assert mock_send.await_args.args[3] == "Sem problemas, para quando?"

    def test_audio_over_size_limit_triggers_handoff_not_500(
        self,
        client: TestClient,
        tenant_id: UUID,
        caplog: pytest.LogCaptureFixture,
    ):
        """An audio whose file_size exceeds audio_max_seconds*16KB is rejected
        with a handoff message + real HandoffService call (never a 500)."""
        phone_id = f"pn_{uuid4().hex[:8]}"
        _seed_meta_credentials_with_whisper(client, tenant_id, phone_id)

        media_id = f"media_{uuid4().hex[:12]}"
        wamid = f"wamid.audio_long_{uuid4().hex[:10]}"
        payload = _meta_audio_payload(
            phone_number_id=phone_id,
            from_phone="5511900002222",
            media_id=media_id,
            message_id=wamid,
        )

        # ~5 minutes of audio ≈ 300 * 16000 = 4.8MB, well over the 60s default
        # cap (which yields max_bytes = 60 * 16000 = 960KB)
        fake_media_info = {
            "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=long",
            "mime_type": "audio/ogg; codecs=opus",
            "file_size": 300 * 16000,
        }

        handoff_mock = AsyncMock()

        with patch(
            "src.api.routes.webhook.resolve_meta_media",
            new=AsyncMock(return_value=fake_media_info),
        ), patch(
            "src.api.routes.webhook.download_meta_media",
            new=AsyncMock(),
        ) as mock_download, patch(
            "src.api.routes.webhook._meta_transcribe_audio",
            new=AsyncMock(),
        ) as mock_transcribe, patch(
            "src.api.routes.webhook.send_meta_cloud_text",
            new=AsyncMock(return_value=_fake_meta_send_result()),
        ) as mock_send, patch(
            "src.api.routes.webhook._meta_initiate_file_handoff",
            new=handoff_mock,
        ), patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service:
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.process_event = AsyncMock()
            mock_get_service.return_value = service

            with caplog.at_level("INFO", logger="src.api.routes.webhook"):
                resp = client.post(WEBHOOK_PATH, json=payload)

        # No 500, no download, no transcription
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "audio_too_long"}
        assert mock_download.await_count == 0
        assert mock_transcribe.await_count == 0

        # But we did send the fixed "audio too long" text to the patient
        assert mock_send.await_count == 1
        sent_body = mock_send.await_args.args[3]
        assert "encaminhar" in sent_body.lower() or "forward" in sent_body.lower()

        # And we initiated a real handoff with the expected note
        assert handoff_mock.await_count == 1
        _, _, phone_arg, note_arg = handoff_mock.await_args.args
        assert phone_arg == "5511900002222"
        assert note_arg == "audio_too_long"

    def test_audio_media_resolve_failure_logs_warning(
        self,
        client: TestClient,
        tenant_id: UUID,
        caplog: pytest.LogCaptureFixture,
    ):
        """A failed media_id resolve short-circuits with a WARNING log
        (parity with 'no tenant for' + 'send failed' log severity)."""
        phone_id = f"pn_{uuid4().hex[:8]}"
        _seed_meta_credentials_with_whisper(client, tenant_id, phone_id)

        media_id = f"media_{uuid4().hex[:12]}"
        payload = _meta_audio_payload(
            phone_number_id=phone_id,
            from_phone="5511900003333",
            media_id=media_id,
            message_id=f"wamid.audio_resolve_fail_{uuid4().hex[:8]}",
        )

        with patch(
            "src.api.routes.webhook.resolve_meta_media",
            new=AsyncMock(side_effect=RuntimeError("expired media_id")),
        ), patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service:
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.process_event = AsyncMock()
            mock_get_service.return_value = service

            with caplog.at_level("WARNING", logger="src.api.routes.webhook"):
                resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "media_resolve_failed"}

        warn_msgs = [
            r.getMessage() for r in caplog.records if r.levelname == "WARNING"
        ]
        assert any(
            "Meta media resolve failed" in m and media_id in m for m in warn_msgs
        ), warn_msgs

    def test_audio_transcription_failure_uses_logger_exception(
        self,
        client: TestClient,
        tenant_id: UUID,
        caplog: pytest.LogCaptureFixture,
    ):
        """Transcription errors are logged via ``logger.exception`` (ERROR + stack)
        matching the send-failure log parity requirement."""
        phone_id = f"pn_{uuid4().hex[:8]}"
        _seed_meta_credentials_with_whisper(client, tenant_id, phone_id)

        media_id = f"media_{uuid4().hex[:12]}"
        payload = _meta_audio_payload(
            phone_number_id=phone_id,
            from_phone="5511900004444",
            media_id=media_id,
            message_id=f"wamid.audio_stt_fail_{uuid4().hex[:8]}",
        )

        fake_media_info = {
            "url": "https://lookaside.fbsbx.com/whatsapp_business/attachments/?mid=ok",
            "mime_type": "audio/ogg; codecs=opus",
            "file_size": 5 * 16000,
        }

        with patch(
            "src.api.routes.webhook.resolve_meta_media",
            new=AsyncMock(return_value=fake_media_info),
        ), patch(
            "src.api.routes.webhook.download_meta_media",
            new=AsyncMock(return_value=b"OggS\x00\x02" + b"\x00" * 100),
        ), patch(
            "src.api.routes.webhook._meta_transcribe_audio",
            new=AsyncMock(side_effect=RuntimeError("groq 503")),
        ), patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service:
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.process_event = AsyncMock()
            mock_get_service.return_value = service

            with caplog.at_level("ERROR", logger="src.api.routes.webhook"):
                resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "transcription_failed"}

        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert error_records, "expected an ERROR log for transcription failure"
        assert any(
            "Meta audio transcription failed" in r.getMessage()
            for r in error_records
        )


# ============================================
# POST /v1/webhook/meta — files / attachments (Group 4)
# ============================================


class TestMetaWebhookFileFlow:
    @pytest.mark.parametrize(
        "msg_type, mime_type, filename",
        [
            ("image", "image/jpeg", None),
            ("document", "application/pdf", "laudo.pdf"),
            (
                "document",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "receita.docx",
            ),
            ("video", "video/mp4", None),
            ("sticker", "image/webp", None),
        ],
    )
    def test_file_never_downloaded_replies_with_fixed_ack(
        self,
        client: TestClient,
        tenant_id: UUID,
        msg_type: str,
        mime_type: str,
        filename: str | None,
    ):
        """Any file/attachment type triggers the fixed ack — never downloaded,
        never analysed, never dispatched to the pipeline."""
        phone_id = f"pn_{uuid4().hex[:8]}"
        _seed_meta_credentials(client, tenant_id, phone_id)

        media_id = f"media_{uuid4().hex[:12]}"
        payload = _meta_media_payload(
            phone_number_id=phone_id,
            from_phone="5511900005555",
            media_id=media_id,
            message_id=f"wamid.file_{uuid4().hex[:8]}",
            msg_type=msg_type,
            mime_type=mime_type,
            filename=filename,
        )

        with patch(
            "src.api.routes.webhook.resolve_meta_media",
            new=AsyncMock(),
        ) as mock_resolve, patch(
            "src.api.routes.webhook.download_meta_media",
            new=AsyncMock(),
        ) as mock_download, patch(
            "src.api.routes.webhook.send_meta_cloud_text",
            new=AsyncMock(return_value=_fake_meta_send_result()),
        ) as mock_send, patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service:
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.process_event = AsyncMock()
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["replied"] is True
        assert body["action"] == "file_ack"

        # Content was never touched
        assert mock_resolve.await_count == 0
        assert mock_download.await_count == 0
        assert service.process_event.await_count == 0

        # But the fixed ack message was sent
        assert mock_send.await_count == 1
        sent_body = mock_send.await_args.args[3]
        # Portuguese default when no lead_name is known
        assert "arquivo" in sent_body.lower()
        assert (
            "encaminh" in sent_body.lower()
            or "equipe" in sent_body.lower()
        )

    def test_file_ack_personalises_with_lead_name_when_known(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """When ``cognitive_state.slots.lead_name`` is set, the ack greets the
        patient by first name."""
        phone_id = f"pn_{uuid4().hex[:8]}"
        _seed_meta_credentials(client, tenant_id, phone_id)

        media_id = f"media_{uuid4().hex[:12]}"
        payload = _meta_media_payload(
            phone_number_id=phone_id,
            from_phone="5511900006666",
            media_id=media_id,
            message_id=f"wamid.file_named_{uuid4().hex[:8]}",
            msg_type="image",
        )

        with patch(
            "src.api.routes.webhook._meta_load_lead_name",
            new=AsyncMock(return_value="Ana Maria Silva"),
        ), patch(
            "src.api.routes.webhook.send_meta_cloud_text",
            new=AsyncMock(return_value=_fake_meta_send_result()),
        ) as mock_send, patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service:
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.process_event = AsyncMock()
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200
        assert resp.json()["action"] == "file_ack"
        assert mock_send.await_count == 1
        sent_body = mock_send.await_args.args[3]
        # Only the first name is used (never the full name)
        assert ", Ana" in sent_body
        assert "Silva" not in sent_body

    def test_file_ack_never_calls_pipeline_or_download(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Regression guard: even a PDF that could look like a medical report
        never reaches the pipeline / download path."""
        phone_id = f"pn_{uuid4().hex[:8]}"
        _seed_meta_credentials(client, tenant_id, phone_id)

        payload = _meta_media_payload(
            phone_number_id=phone_id,
            from_phone="5511900007777",
            media_id=f"media_{uuid4().hex[:12]}",
            message_id=f"wamid.pdf_{uuid4().hex[:8]}",
            msg_type="document",
            mime_type="application/pdf",
            filename="exame_hemograma.pdf",
        )

        with patch(
            "src.api.routes.webhook.resolve_meta_media",
            new=AsyncMock(),
        ) as mock_resolve, patch(
            "src.api.routes.webhook.download_meta_media",
            new=AsyncMock(),
        ) as mock_download, patch(
            "src.api.routes.webhook.send_meta_cloud_text",
            new=AsyncMock(return_value=_fake_meta_send_result()),
        ), patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service:
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.process_event = AsyncMock()
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200
        assert mock_resolve.await_count == 0
        assert mock_download.await_count == 0
        assert service.process_event.await_count == 0


# ============================================
# POST /v1/webhook/meta — forward-to-team choice (Group 4)
# ============================================


class TestMetaWebhookFileForwardChoice:
    def test_forward_text_after_file_initiates_handoff(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """When ``pending_file_review`` is set on the session and the next
        text matches a forward keyword, a real handoff is initiated."""
        phone_id = f"pn_{uuid4().hex[:8]}"
        _seed_meta_credentials(client, tenant_id, phone_id)

        wamid = f"wamid.forward_{uuid4().hex[:10]}"
        payload = _meta_text_payload(
            phone_number_id=phone_id,
            from_phone="5511900008888",
            text_body="Prefiro que você encaminhe pra equipe, obrigado!",
            message_id=wamid,
        )

        handoff_mock = AsyncMock()
        clear_mock = AsyncMock()

        with patch(
            "src.api.routes.webhook._meta_get_pending_file_review",
            new=AsyncMock(return_value=True),
        ), patch(
            "src.api.routes.webhook._meta_clear_pending_file_review",
            new=clear_mock,
        ), patch(
            "src.api.routes.webhook._meta_initiate_file_handoff",
            new=handoff_mock,
        ), patch(
            "src.api.routes.webhook.send_meta_cloud_text",
            new=AsyncMock(return_value=_fake_meta_send_result()),
        ) as mock_send, patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service:
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.process_event = AsyncMock(
                return_value=_fake_event_response("should never fire"),
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["replied"] is True
        assert body["action"] == "file_handoff"

        # Handoff was initiated with the expected note
        assert handoff_mock.await_count == 1
        _, _, phone_arg, note_arg = handoff_mock.await_args.args
        assert phone_arg == "5511900008888"
        assert note_arg == "file_pending_review"

        # Flag was cleared so the next message is a normal one again
        assert clear_mock.await_count >= 1

        # A confirmation message was sent to the patient
        assert mock_send.await_count == 1
        sent_body = mock_send.await_args.args[3]
        assert "equipe" in sent_body.lower() or "team" in sent_body.lower()

        # Pipeline was NOT invoked
        assert service.process_event.await_count == 0

    def test_description_text_after_file_falls_through_to_pipeline(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """When the reply does NOT match a forward keyword, the flag is
        cleared and the text is processed as a normal message."""
        phone_id = f"pn_{uuid4().hex[:8]}"
        _seed_meta_credentials(client, tenant_id, phone_id)

        wamid = f"wamid.describe_{uuid4().hex[:10]}"
        payload = _meta_text_payload(
            phone_number_id=phone_id,
            from_phone="5511900009999",
            text_body="Na verdade queria saber o valor da consulta",
            message_id=wamid,
        )

        handoff_mock = AsyncMock()
        clear_mock = AsyncMock()

        with patch(
            "src.api.routes.webhook._meta_get_pending_file_review",
            new=AsyncMock(return_value=True),
        ), patch(
            "src.api.routes.webhook._meta_clear_pending_file_review",
            new=clear_mock,
        ), patch(
            "src.api.routes.webhook._meta_initiate_file_handoff",
            new=handoff_mock,
        ), patch(
            "src.api.routes.webhook.send_meta_cloud_text",
            new=AsyncMock(return_value=_fake_meta_send_result()),
        ), patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service:
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.process_event = AsyncMock(
                return_value=_fake_event_response("A consulta é R$ 200"),
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "replied": True}

        # No handoff was initiated
        assert handoff_mock.await_count == 0
        # Flag cleared exactly once
        assert clear_mock.await_count >= 1
        # Pipeline was invoked with the description text
        assert service.process_event.await_count == 1
        assert (
            service.process_event.await_args.args[0].payload.content
            == "Na verdade queria saber o valor da consulta"
        )
