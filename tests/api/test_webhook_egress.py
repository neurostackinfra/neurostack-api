"""
NeuroStack API — Webhook Audio/Egress Matrix Tests

Group 5 acceptance: the audio download path in Z-API and Meta webhook
handlers uses ``safe_download_audio`` / ``download_meta_media`` from Group 4,
and every ``UnsafeEgressError`` variant produces the exact skip envelope
BEFORE any STT request, reply, handoff or session mutation.

Also covers:
- Unsupported STT providers (elevenlabs_scribe, google_stt, deepgram_stt,
  microsoft_stt) return the skip envelope *before* any download.
- Supported providers (outros, openai_whisper) proceed to the download step.
"""

import base64
import hashlib
import hmac as _hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from src.config.settings import settings
from src.services.safe_egress import SafeDownload, UnsafeEgressError

API_CRED_PATH = "/v1/config/api-credentials"

# ---- helpers ----------------------------------------------------------------


def _admin_token(tenant_id: UUID) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(tenant_id),
            "role": "clinic_admin",
            "iat": now,
            "exp": now + timedelta(hours=1),
            "jti": str(uuid4()),
        },
        settings.secret_key,
        algorithm="HS256",
    )


def _admin_headers(tenant_id: UUID) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_admin_token(tenant_id)}",
        "Content-Type": "application/json",
    }


def _new_zapi_token() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _seed_legacy_credentials(
    client: TestClient, tenant_id: UUID, credentials: dict[str, Any]
) -> None:
    """Insert legacy runtime credentials without reopening public writes."""
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


def _seed_zapi_with_stt(
    client: TestClient,
    tenant_id: UUID,
    instance_id: str,
    raw_token: str,
    stt_provider: str = "outros",
    *,
    whisper_key: str = "sk-test-whisper-key",
) -> None:
    _seed_legacy_credentials(
        client,
        tenant_id,
        {
            "whatsapp_provider": "zapi",
            "zapi_instance_id": instance_id,
            "zapi_token": f"zt_{uuid4().hex}",
            "zapi_inbound_token_digest": _digest(raw_token),
            "stt_provider": stt_provider,
            "stt_api_key": whisper_key,
        },
    )


def _audio_body(
    instance_id: str,
    message_id: str,
    audio_url: str,
    *,
    seconds: int = 5,
    from_me: bool = False,
) -> dict[str, Any]:
    return {
        "instanceId": instance_id,
        "phone": "5511900000002",
        "messageId": message_id,
        "fromMe": from_me,
        "audio": {
            "url": audio_url,
            "mimeType": "audio/ogg",
            "seconds": seconds,
        },
        "momment": 1_711_900_000_000,
        "senderName": "Paciente",
    }


def _create_session(client: TestClient, tenant_id: UUID, phone: str) -> UUID:
    """Create an active session for ``phone`` on the app's own event loop."""
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


def _session_is_paused(client: TestClient, session_id: UUID) -> bool:
    from sqlalchemy import select

    from src.models.session import Session as SessionRow
    from src.repositories.database import get_db_session

    async def _run() -> bool:
        db = get_db_session()
        try:
            row = await db.execute(
                select(SessionRow.is_paused).where(SessionRow.id == session_id)
            )
            return bool(row.scalar_one())
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined]


@pytest.fixture(autouse=True)
def _clear_state():
    from src.api.middleware.rate_limit import rate_limit_store
    from src.api.routes.webhook import _INMEM_INCOMING, _INMEM_SENT, _INMEM_STT_NOTICE

    _INMEM_INCOMING.clear()
    _INMEM_SENT.clear()
    # Without this the 6h reroute-notice window leaks across tests: the first
    # one to trigger the chain sends, every later one is suppressed.
    _INMEM_STT_NOTICE.clear()
    rate_limit_store._buckets.clear()
    rate_limit_store._tenant_limits.clear()
    yield


# ---- Z-API audio egress: UnsafeEgressError variants -------------------------


class TestZapiAudioEgressRejection:
    """Each UnsafeEgressError code from safe_download_audio maps to a skip."""

    @pytest.mark.parametrize(
        "egress_code,expected_skip",
        [
            ("unsafe_api_url", "unsafe_audio_url"),
            ("audio_too_large", "audio_too_large"),
            ("unsupported_audio_mime", "unsupported_audio_mime"),
            ("audio_download_failed", "audio_download_failed"),
            ("redirect_forbidden", "unsafe_audio_url"),
            ("unknown_code_xyz", "audio_download_failed"),  # default fallback
        ],
    )
    def test_egress_error_maps_to_skip_before_stt(
        self,
        client: TestClient,
        egress_code: str,
        expected_skip: str,
    ) -> None:
        tenant_id = uuid4()
        instance_id = f"inst-{uuid4().hex[:8]}"
        raw_token = _new_zapi_token()
        _seed_zapi_with_stt(client, tenant_id, instance_id, raw_token)

        # No credential loader is patched: the handler reads stt_api_key
        # straight from the seeded clinic_configs row.
        with patch(
            "src.api.routes.webhook.safe_download_audio",
            AsyncMock(
                side_effect=UnsafeEgressError(
                    egress_code, url="https://cdn.example.com/audio.ogg"
                )
            ),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_audio_body(
                    instance_id, str(uuid4()), "https://cdn.example.com/audio.ogg"
                ),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        assert body.get("skipped") == expected_skip, body

    def test_successful_download_reaches_transcription(
        self,
        client: TestClient,
    ) -> None:
        """When the download succeeds, transcription is attempted (no skip)."""
        tenant_id = uuid4()
        instance_id = f"inst-{uuid4().hex[:8]}"
        raw_token = _new_zapi_token()
        whisper_key = f"sk-real-row-{uuid4().hex}"
        _seed_zapi_with_stt(
            client,
            tenant_id,
            instance_id,
            raw_token,
            stt_provider="outros",
            whisper_key=whisper_key,
        )

        fake_download = SafeDownload(content=b"OGGS_AUDIO_BYTES", mime_type="audio/ogg")
        # transcribe_audio is lazily imported from src.services.groq_service inside
        # the STT rung. Patching at that module path intercepts the call. Raising
        # an exception drops the audio into the fallback chain, which — with no
        # multimodal configured — sends the text reroute. The reroute envelope
        # confirms the download step completed and STT was reached.
        transcribe_mock = AsyncMock(side_effect=Exception("stop-at-transcription"))
        send_mock = AsyncMock(return_value={"messageId": f"m_{uuid4().hex}"})

        # No credential loader is patched — neither _load_clinic_config nor
        # _load_tenant_credentials. The whisper key must come from the row
        # seeded directly in the persisted clinic_configs row.
        with (
            patch(
                "src.api.routes.webhook.safe_download_audio",
                AsyncMock(return_value=fake_download),
            ),
            patch(
                "src.services.groq_service.transcribe_audio",
                transcribe_mock,
            ),
            patch("src.api.routes.webhook.send_text_message", send_mock),
            patch("src.api.routes.webhook.mark_conversation_unread", AsyncMock()),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_audio_body(
                    instance_id, str(uuid4()), "https://cdn.example.com/audio.ogg"
                ),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        body = resp.json()
        # stt_fallback confirms the download completed and STT was reached
        assert body == {"ok": True, "replied": True, "action": "stt_fallback"}, body
        transcribe_mock.assert_called_once()
        # The key reached STT straight from the DB row — nothing was injected.
        assert transcribe_mock.call_args.kwargs["api_key"] == whisper_key


# ---- Long-audio handoff: end-to-end through the webhook ---------------------


class TestZapiLongAudioHandoff:
    """Audio longer than audio_max_seconds hands off to the clinic team.

    Enters through the public webhook with credentials read from the seeded
    clinic_configs row — no credential loader is patched.
    """

    def test_long_audio_sends_handoff_and_marks_unread(
        self, client: TestClient
    ) -> None:
        tenant_id = uuid4()
        instance_id = f"inst-{uuid4().hex[:8]}"
        raw_token = _new_zapi_token()
        _seed_zapi_with_stt(
            client, tenant_id, instance_id, raw_token, stt_provider="outros"
        )

        send_mock = AsyncMock(return_value={"messageId": f"m_{uuid4().hex}"})
        unread_mock = AsyncMock(return_value={"ok": True})
        download_mock = AsyncMock()
        transcribe_mock = AsyncMock()

        with (
            patch("src.api.routes.webhook.send_text_message", send_mock),
            patch("src.api.routes.webhook.mark_conversation_unread", unread_mock),
            patch("src.api.routes.webhook.safe_download_audio", download_mock),
            patch("src.services.groq_service.transcribe_audio", transcribe_mock),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_audio_body(
                    instance_id,
                    str(uuid4()),
                    "https://cdn.example.com/audio.ogg",
                    # default audio_max_seconds is 60
                    seconds=180,
                ),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("skipped") == "audio_too_long", body

        send_mock.assert_called_once()
        unread_mock.assert_called_once()
        # instance id comes from the DB row; the message is the handoff notice
        assert send_mock.call_args.args[0] == instance_id
        assert send_mock.call_args.args[1]  # zapi_token was resolved, not empty
        assert "equipe" in send_mock.call_args.args[3]
        assert unread_mock.call_args.args[0] == instance_id

        # The handoff replaces transcription entirely
        download_mock.assert_not_called()
        transcribe_mock.assert_not_called()


# ---- fromMe audio: never downloaded, never transcribed ----------------------


class TestZapiFromMeAudioSkips:
    """A clinic staff member's own voice note must not cost an STT call.

    ``fromMe`` is part of the audio block's own condition, so no branch inside
    it — download, transcription, handoff or reply — is reachable for staff
    audio. The payload is still routed to the clinic-message handling with an
    empty transcript so an active human takeover pauses the AI.
    """

    def test_from_me_audio_skips_before_download_and_stt(
        self, client: TestClient
    ) -> None:
        tenant_id = uuid4()
        instance_id = f"inst-{uuid4().hex[:8]}"
        raw_token = _new_zapi_token()
        _seed_zapi_with_stt(
            client, tenant_id, instance_id, raw_token, stt_provider="outros"
        )

        download_mock = AsyncMock(
            return_value=SafeDownload(content=b"OGGS", mime_type="audio/ogg")
        )
        transcribe_mock = AsyncMock(return_value="transcribed")
        send_mock = AsyncMock(return_value={})
        unread_mock = AsyncMock(return_value={})

        with (
            patch("src.api.routes.webhook.safe_download_audio", download_mock),
            patch("src.services.groq_service.transcribe_audio", transcribe_mock),
            patch("src.api.routes.webhook.send_text_message", send_mock),
            patch("src.api.routes.webhook.mark_conversation_unread", unread_mock),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_audio_body(
                    instance_id,
                    str(uuid4()),
                    "https://cdn.example.com/audio.ogg",
                    from_me=True,
                ),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        # No session for this phone: the staff audio reached the clinic-message
        # handling (instead of the old "audio_from_me" dead end) and stopped
        # there — still without touching download or STT.
        assert resp.json().get("action") == "no_session", resp.json()
        download_mock.assert_not_called()
        transcribe_mock.assert_not_called()
        send_mock.assert_not_called()
        unread_mock.assert_not_called()

    def test_from_me_audio_pauses_session_for_human_takeover(
        self, client: TestClient
    ) -> None:
        """A secretary replying by voice pauses the AI, with no STT cost."""
        tenant_id = uuid4()
        instance_id = f"inst-{uuid4().hex[:8]}"
        raw_token = _new_zapi_token()
        _seed_zapi_with_stt(
            client, tenant_id, instance_id, raw_token, stt_provider="outros"
        )
        session_id = _create_session(client, tenant_id, "5511900000002")
        assert _session_is_paused(client, session_id) is False

        download_mock = AsyncMock(
            return_value=SafeDownload(content=b"OGGS", mime_type="audio/ogg")
        )
        transcribe_mock = AsyncMock(return_value="transcribed")
        send_mock = AsyncMock(return_value={})
        unread_mock = AsyncMock(return_value={})

        with (
            patch("src.api.routes.webhook.safe_download_audio", download_mock),
            patch("src.services.groq_service.transcribe_audio", transcribe_mock),
            patch("src.api.routes.webhook.send_text_message", send_mock),
            patch("src.api.routes.webhook.mark_conversation_unread", unread_mock),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_audio_body(
                    instance_id,
                    str(uuid4()),
                    "https://cdn.example.com/audio.ogg",
                    from_me=True,
                ),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        assert resp.json().get("action") == "human_takeover", resp.json()
        assert _session_is_paused(client, session_id) is True
        # Decision 13: the clinic's own audio is never downloaded or transcribed
        download_mock.assert_not_called()
        transcribe_mock.assert_not_called()
        send_mock.assert_not_called()
        unread_mock.assert_not_called()

    def test_ai_sent_audio_echo_is_not_a_takeover(self, client: TestClient) -> None:
        """An AI-sent audio echo is recognised as an echo and pauses nothing."""
        from src.api.routes.webhook import _track_sent_message

        tenant_id = uuid4()
        instance_id = f"inst-{uuid4().hex[:8]}"
        raw_token = _new_zapi_token()
        _seed_zapi_with_stt(
            client, tenant_id, instance_id, raw_token, stt_provider="outros"
        )
        session_id = _create_session(client, tenant_id, "5511900000002")

        message_id = str(uuid4())
        _track_sent_message({"messageId": message_id})

        download_mock = AsyncMock()
        transcribe_mock = AsyncMock()

        with (
            patch("src.api.routes.webhook.safe_download_audio", download_mock),
            patch("src.services.groq_service.transcribe_audio", transcribe_mock),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_audio_body(
                    instance_id,
                    message_id,
                    "https://cdn.example.com/audio.ogg",
                    from_me=True,
                ),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        assert resp.json().get("skipped") == "ai_echo", resp.json()
        assert _session_is_paused(client, session_id) is False
        download_mock.assert_not_called()
        transcribe_mock.assert_not_called()

    def test_from_me_long_audio_does_not_hand_off(self, client: TestClient) -> None:
        """The fromMe guard fires before the duration branch, not after it."""
        tenant_id = uuid4()
        instance_id = f"inst-{uuid4().hex[:8]}"
        raw_token = _new_zapi_token()
        _seed_zapi_with_stt(
            client, tenant_id, instance_id, raw_token, stt_provider="outros"
        )

        send_mock = AsyncMock(return_value={})
        unread_mock = AsyncMock(return_value={})
        download_mock = AsyncMock()

        with (
            patch("src.api.routes.webhook.send_text_message", send_mock),
            patch("src.api.routes.webhook.mark_conversation_unread", unread_mock),
            patch("src.api.routes.webhook.safe_download_audio", download_mock),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_audio_body(
                    instance_id,
                    str(uuid4()),
                    "https://cdn.example.com/audio.ogg",
                    seconds=180,
                    from_me=True,
                ),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        assert resp.json().get("action") == "no_session", resp.json()
        send_mock.assert_not_called()
        unread_mock.assert_not_called()
        download_mock.assert_not_called()


# ---- Event metadata reports the model STT actually used ---------------------


class TestZapiAudioEventMetadataModel:
    """``metadata["whisper_model"]`` must match the model passed to STT."""

    def test_openai_whisper_default_model_is_reported(self, client: TestClient) -> None:
        tenant_id = uuid4()
        instance_id = f"inst-{uuid4().hex[:8]}"
        raw_token = _new_zapi_token()
        # openai_whisper with neither stt_model_name nor whisper_model set:
        # _STT_DEFAULT_MODELS resolves it to "whisper-1".
        _seed_zapi_with_stt(
            client, tenant_id, instance_id, raw_token, stt_provider="openai_whisper"
        )

        from src.services.event_service import EventService

        captured: dict[str, Any] = {}

        class _StubResponse:
            prompt = None

        async def _capture(self: Any, event_request: Any, session: Any = None) -> Any:
            captured["metadata"] = dict(event_request.payload.metadata or {})
            return _StubResponse()

        transcribe_mock = AsyncMock(return_value="quero agendar")

        with (
            patch(
                "src.api.routes.webhook.safe_download_audio",
                AsyncMock(
                    return_value=SafeDownload(content=b"OGGS", mime_type="audio/ogg")
                ),
            ),
            patch("src.services.groq_service.transcribe_audio", transcribe_mock),
            patch("src.api.routes.webhook.mark_message_read", AsyncMock()),
            patch.object(EventService, "process_event", _capture),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_audio_body(
                    instance_id, str(uuid4()), "https://cdn.example.com/audio.ogg"
                ),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200, resp.text
        transcribe_mock.assert_called_once()
        used_model = transcribe_mock.call_args.kwargs["model"]
        assert used_model == "whisper-1"
        assert captured["metadata"]["is_audio"] is True
        # The event must not claim a Groq model for an OpenAI transcription
        assert captured["metadata"]["whisper_model"] == used_model, captured


# ---- Unsupported STT providers: skip before download ------------------------


class TestUnsupportedSttSkipBeforeDownload:
    """elevenlabs_scribe, google_stt, deepgram_stt, microsoft_stt must return
    the skip envelope BEFORE any audio download, STT call, reply, handoff or
    session mutation."""

    @pytest.mark.parametrize(
        "provider",
        ["elevenlabs_scribe", "google_stt", "deepgram_stt", "microsoft_stt"],
    )
    def test_unsupported_stt_returns_skip_without_download(
        self,
        client: TestClient,
        provider: str,
    ) -> None:
        tenant_id = uuid4()
        instance_id = f"inst-{uuid4().hex[:8]}"
        raw_token = _new_zapi_token()
        _seed_zapi_with_stt(
            client,
            tenant_id,
            instance_id,
            raw_token,
            stt_provider=provider,
        )

        # stt_provider IS in the runtime projection — no need to mock
        # _load_clinic_config for unsupported providers; the skip fires
        # at the provider-check gate before the whisper_key guard.
        download_mock = AsyncMock()

        with patch(
            "src.api.routes.webhook.safe_download_audio",
            download_mock,
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_audio_body(
                    instance_id, str(uuid4()), "https://cdn.example.com/audio.ogg"
                ),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        assert body.get("skipped") == "stt_provider_unsupported", body
        assert body.get("provider") == provider
        download_mock.assert_not_called()

    @pytest.mark.parametrize(
        "provider",
        ["outros", "openai_whisper"],
    )
    def test_supported_stt_proceeds_to_download(
        self,
        client: TestClient,
        provider: str,
    ) -> None:
        """Supported providers (outros, openai_whisper) reach the download step."""
        tenant_id = uuid4()
        instance_id = f"inst-{uuid4().hex[:8]}"
        raw_token = _new_zapi_token()
        _seed_zapi_with_stt(
            client,
            tenant_id,
            instance_id,
            raw_token,
            stt_provider=provider,
        )

        download_mock = AsyncMock(
            side_effect=UnsafeEgressError("audio_download_failed")
        )

        with patch("src.api.routes.webhook.safe_download_audio", download_mock):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_audio_body(
                    instance_id, str(uuid4()), "https://cdn.example.com/audio.ogg"
                ),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200
        body = resp.json()
        # The unsupported-skip sentinel must NOT appear — the download was reached
        assert body.get("skipped") != "stt_provider_unsupported"
        download_mock.assert_called_once()


# ---- Meta audio download failure --------------------------------------------


class TestMetaAudioEgressRejection:
    """Meta webhook: audio download failure from download_meta_media returns
    the media_download_failed skip without any STT/reply/handoff/session effect.
    """

    META_APP_SECRET = "test_app_secret_32bytes_abcdefgh"

    @staticmethod
    def _meta_headers(body_bytes: bytes, app_secret: str) -> dict[str, str]:
        sig = _hmac.new(app_secret.encode(), body_bytes, digestmod="sha256").hexdigest()
        return {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={sig}",
        }

    def _seed_meta_tenant(
        self,
        client: TestClient,
        tenant_id: UUID,
        phone_number_id: str,
    ) -> None:
        _seed_legacy_credentials(
            client,
            tenant_id,
            {
                "whatsapp_provider": "meta_cloud",
                "meta_phone_number_id": phone_number_id,
                "meta_app_secret": self.META_APP_SECRET,
                "meta_verify_token": f"vt_{uuid4().hex}",
                # meta_permanent_token supplies meta_token so the handler
                # can pass the no_credentials guard and reach the download step.
                "meta_permanent_token": f"EAA{uuid4().hex}",
            },
        )

    @staticmethod
    def _meta_audio_body(phone_number_id: str, message_id: str, media_id: str) -> dict:
        return {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba_id",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": phone_number_id},
                                "messages": [
                                    {
                                        "id": message_id,
                                        "from": "5511900000003",
                                        "type": "audio",
                                        "audio": {"id": media_id},
                                    }
                                ],
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        }

    def test_meta_audio_download_failure_returns_skip(self, client: TestClient) -> None:
        tenant_id = uuid4()
        phone_number_id = f"ph_{uuid4().hex[:12]}"
        media_id = f"media_{uuid4().hex}"
        message_id = str(uuid4())

        self._seed_meta_tenant(client, tenant_id, phone_number_id)

        body_dict = self._meta_audio_body(phone_number_id, message_id, media_id)
        body_bytes = json.dumps(body_dict).encode()
        headers = self._meta_headers(body_bytes, self.META_APP_SECRET)

        # resolve_meta_media and download_meta_media are top-level imports in
        # webhook.py — both patchable at the module path. Return a minimal
        # media_info so the handler advances past the resolve step; then the
        # download raises, causing media_download_failed before any STT call.
        with (
            patch(
                "src.api.routes.webhook.resolve_meta_media",
                AsyncMock(
                    return_value={
                        "url": "https://lookaside.fbsbx.com/media/audio.ogg",
                        "file_size": 0,
                    }
                ),
            ),
            patch(
                "src.api.routes.webhook.download_meta_media_audio",
                AsyncMock(side_effect=Exception("download failed")),
            ),
        ):
            resp = client.post(
                "/v1/webhook/meta",
                content=body_bytes,
                headers=headers,
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("skipped") == "media_download_failed", body


class TestMetaUnsupportedSttSkipBeforeResolve:
    """Meta webhook: unsupported STT providers skip before any external call.

    WISH.md criterion: "quatro STT unsupported nao produzem efeitos" — resolve,
    download, transcription, reply, handoff and session mutation must all be
    unreachable when stt_provider is in the unsupported set.
    """

    META_APP_SECRET = "test_app_secret_meta_stt_skip__"

    @staticmethod
    def _meta_headers(body_bytes: bytes, app_secret: str) -> dict[str, str]:
        sig = _hmac.new(app_secret.encode(), body_bytes, digestmod="sha256").hexdigest()
        return {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={sig}",
        }

    def _seed_meta_tenant_with_stt(
        self,
        client: TestClient,
        tenant_id: UUID,
        phone_number_id: str,
        stt_provider: str,
    ) -> None:
        _seed_legacy_credentials(
            client,
            tenant_id,
            {
                "whatsapp_provider": "meta_cloud",
                "meta_phone_number_id": phone_number_id,
                "meta_app_secret": self.META_APP_SECRET,
                "meta_verify_token": f"vt_{uuid4().hex}",
                "meta_permanent_token": f"EAA{uuid4().hex}",
                "stt_provider": stt_provider,
                # The runtime still receives an explicit legacy STT key.
                "stt_api_key": f"sk-dummy-{uuid4().hex}",
            },
        )

    @staticmethod
    def _meta_audio_body(phone_number_id: str, message_id: str, media_id: str) -> dict:
        return {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba_id",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": phone_number_id},
                                "messages": [
                                    {
                                        "id": message_id,
                                        "from": "5511900000003",
                                        "type": "audio",
                                        "audio": {"id": media_id},
                                    }
                                ],
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        }

    @pytest.mark.parametrize(
        "provider",
        ["elevenlabs_scribe", "google_stt", "deepgram_stt", "microsoft_stt"],
    )
    def test_meta_unsupported_stt_skips_before_resolve(
        self, client: TestClient, provider: str
    ) -> None:
        tenant_id = uuid4()
        phone_number_id = f"ph_{uuid4().hex[:12]}"
        media_id = f"media_{uuid4().hex}"
        message_id = str(uuid4())

        self._seed_meta_tenant_with_stt(client, tenant_id, phone_number_id, provider)

        body_dict = self._meta_audio_body(phone_number_id, message_id, media_id)
        body_bytes = json.dumps(body_dict).encode()
        headers = self._meta_headers(body_bytes, self.META_APP_SECRET)

        resolve_mock = AsyncMock()
        download_mock = AsyncMock()

        with (
            patch("src.api.routes.webhook.resolve_meta_media", resolve_mock),
            patch("src.api.routes.webhook.download_meta_media_audio", download_mock),
        ):
            resp = client.post(
                "/v1/webhook/meta",
                content=body_bytes,
                headers=headers,
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body.get("ok") is True
        assert body.get("skipped") == "stt_provider_unsupported", body
        assert body.get("provider") == provider
        # Neither resolve nor download may be called
        resolve_mock.assert_not_called()
        download_mock.assert_not_called()


# ---- Cross-channel parity of the STT fallback chain -------------------------


class TestCrossChannelSttFallbackParity:
    """Decision 9: one chain, two channels, the same message for the same cause.

    Parity here is about *what the patient receives and why*, not about the
    envelope: Z-API additionally marks the conversation unread (the Cloud API
    has no such primitive), so the two responses differ by design.
    """

    META_APP_SECRET = "test_app_secret_parity_channels_"

    @staticmethod
    def _meta_headers(body_bytes: bytes, app_secret: str) -> dict[str, str]:
        sig = _hmac.new(app_secret.encode(), body_bytes, digestmod="sha256").hexdigest()
        return {
            "Content-Type": "application/json",
            "X-Hub-Signature-256": f"sha256={sig}",
        }

    def _seed_meta(
        self,
        client: TestClient,
        tenant_id: UUID,
        phone_number_id: str,
        *,
        with_stt: bool,
    ) -> None:
        payload: dict[str, Any] = {
            "whatsapp_provider": "meta_cloud",
            "meta_phone_number_id": phone_number_id,
            "meta_app_secret": self.META_APP_SECRET,
            "meta_verify_token": f"vt_{uuid4().hex}",
            "meta_permanent_token": f"EAA{uuid4().hex}",
        }
        if with_stt:
            payload["stt_provider"] = "outros"
            payload["stt_api_key"] = f"sk-fake-stt-{uuid4().hex}"
        _seed_legacy_credentials(client, tenant_id, payload)

    def _seed_zapi(
        self,
        client: TestClient,
        tenant_id: UUID,
        instance_id: str,
        raw_token: str,
        *,
        with_stt: bool,
    ) -> None:
        payload: dict[str, Any] = {
            "whatsapp_provider": "zapi",
            "zapi_instance_id": instance_id,
            "zapi_token": f"zt_{uuid4().hex}",
            "zapi_client_token": f"ct_{uuid4().hex}",
            "zapi_inbound_token_digest": _digest(raw_token),
        }
        if with_stt:
            payload["stt_provider"] = "outros"
            payload["stt_api_key"] = f"sk-fake-stt-{uuid4().hex}"
        _seed_legacy_credentials(client, tenant_id, payload)

    @staticmethod
    def _meta_audio_body(phone_number_id: str, message_id: str, media_id: str) -> dict:
        return {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "waba_parity",
                    "changes": [
                        {
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {"phone_number_id": phone_number_id},
                                "messages": [
                                    {
                                        "id": message_id,
                                        "from": "5511900000002",
                                        "type": "audio",
                                        "audio": {
                                            "id": media_id,
                                            "mime_type": "audio/ogg; codecs=opus",
                                        },
                                    }
                                ],
                            },
                            "field": "messages",
                        }
                    ],
                }
            ],
        }

    def _zapi_delivered_message(
        self,
        client: TestClient,
        *,
        with_stt: bool,
    ) -> str:
        tenant_id = uuid4()
        instance_id = f"inst-{uuid4().hex[:8]}"
        raw_token = _new_zapi_token()
        self._seed_zapi(client, tenant_id, instance_id, raw_token, with_stt=with_stt)

        send_mock = AsyncMock(return_value={"messageId": f"z_{uuid4().hex[:8]}"})

        with (
            patch(
                "src.api.routes.webhook.safe_download_audio",
                AsyncMock(
                    return_value=SafeDownload(content=b"OGGS", mime_type="audio/ogg")
                ),
            ),
            patch(
                "src.services.groq_service.transcribe_audio",
                AsyncMock(side_effect=RuntimeError("stt 503")),
            ),
            patch("src.api.routes.webhook.send_text_message", send_mock),
            patch("src.api.routes.webhook.mark_conversation_unread", AsyncMock()),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_audio_body(
                    instance_id, str(uuid4()), "https://cdn.example.com/audio.ogg"
                ),
                headers={"Content-Type": "application/json"},
            )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True, "action": "stt_fallback"}
        assert send_mock.await_count == 1
        return str(send_mock.await_args.args[3])

    def _meta_delivered_message(
        self,
        client: TestClient,
        *,
        with_stt: bool,
    ) -> str:
        tenant_id = uuid4()
        phone_number_id = f"ph_{uuid4().hex[:12]}"
        self._seed_meta(client, tenant_id, phone_number_id, with_stt=with_stt)

        body_dict = self._meta_audio_body(
            phone_number_id, str(uuid4()), f"media_{uuid4().hex}"
        )
        body_bytes = json.dumps(body_dict).encode()
        headers = self._meta_headers(body_bytes, self.META_APP_SECRET)

        send_mock = AsyncMock(
            return_value={"messages": [{"id": f"wamid.{uuid4().hex[:8]}"}]}
        )

        with (
            patch(
                "src.api.routes.webhook.resolve_meta_media",
                AsyncMock(
                    return_value={
                        "url": "https://lookaside.fbsbx.com/media/audio.ogg",
                        "mime_type": "audio/ogg; codecs=opus",
                        "file_size": 5 * 16000,
                    }
                ),
            ),
            patch(
                "src.api.routes.webhook.download_meta_media_audio",
                AsyncMock(return_value=b"OGGS"),
            ),
            patch(
                "src.services.groq_service.transcribe_audio",
                AsyncMock(side_effect=RuntimeError("stt 503")),
            ),
            patch("src.api.routes.webhook.send_meta_cloud_text", send_mock),
        ):
            resp = client.post("/v1/webhook/meta", content=body_bytes, headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True, "action": "stt_fallback"}
        assert send_mock.await_count == 1
        return str(send_mock.await_args.args[3])

    def test_transcription_failure_delivers_the_same_message_on_both_channels(
        self,
        client: TestClient,
    ) -> None:
        from src.api.routes.webhook import _STT_TEMPORARY_FAILURE_MSG_PT

        zapi_msg = self._zapi_delivered_message(client, with_stt=True)
        meta_msg = self._meta_delivered_message(client, with_stt=True)

        assert zapi_msg == meta_msg
        assert zapi_msg == _STT_TEMPORARY_FAILURE_MSG_PT

    def test_never_configured_delivers_the_same_message_on_both_channels(
        self,
        client: TestClient,
    ) -> None:
        from src.api.routes.webhook import (
            _STT_NEVER_CONFIGURED_MSG_PT,
            _STT_TEMPORARY_FAILURE_MSG_PT,
        )

        zapi_msg = self._zapi_delivered_message(client, with_stt=False)
        meta_msg = self._meta_delivered_message(client, with_stt=False)

        assert zapi_msg == meta_msg
        assert zapi_msg == _STT_NEVER_CONFIGURED_MSG_PT
        # Decision 6: the two causes must not collapse into one message
        assert _STT_NEVER_CONFIGURED_MSG_PT != _STT_TEMPORARY_FAILURE_MSG_PT
