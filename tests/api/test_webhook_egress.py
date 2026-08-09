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
from src.services.event_service import EventService
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


def _seed_zapi_with_stt(
    client: TestClient,
    tenant_id: UUID,
    instance_id: str,
    raw_token: str,
    stt_provider: str = "outros",
    *,
    whisper_key: str = "sk-test-whisper-key",
) -> None:
    resp = client.patch(
        API_CRED_PATH,
        json={
            "whatsapp_provider": "zapi",
            "zapi_instance_id": instance_id,
            "zapi_token": f"zt_{uuid4().hex}",
            "zapi_inbound_token_digest": _digest(raw_token),
            "stt_provider": stt_provider,
            "stt_api_key": whisper_key,
        },
        headers=_admin_headers(tenant_id),
    )
    assert resp.status_code == 200, resp.text


def _audio_body(instance_id: str, message_id: str, audio_url: str) -> dict[str, Any]:
    return {
        "instanceId": instance_id,
        "phone": "5511900000002",
        "messageId": message_id,
        "fromMe": False,
        "audio": {
            "url": audio_url,
            "mimeType": "audio/ogg",
            "seconds": 5,
        },
        "momment": 1_711_900_000_000,
        "senderName": "Paciente",
    }


def _clinic_config_with_stt(
    instance_id: str,
    stt_provider: str = "outros",
    whisper_key: str = "sk-test-whisper-key",
) -> dict:
    """Minimal clinic config dict including raw stt_api_key for audio processing.

    _load_clinic_config returns a secret-free projected snapshot; this helper
    provides a version that retains the STT key so the audio path can proceed
    past the whisper_key guard and reach safe_download_audio.
    """
    return {
        "api_credentials": {
            "whatsapp_provider": "zapi",
            "zapi_instance_id": instance_id,
            "stt_provider": stt_provider,
            "stt_api_key": whisper_key,
            "audio_max_seconds": 60,
        }
    }


@pytest.fixture(autouse=True)
def _clear_state():
    from src.api.middleware.rate_limit import rate_limit_store
    from src.api.routes.webhook import _INMEM_INCOMING, _INMEM_SENT

    _INMEM_INCOMING.clear()
    _INMEM_SENT.clear()
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

        # _load_clinic_config returns a secret-free projected snapshot, so
        # stt_api_key is stripped. Mock it to include the key so the audio
        # handler proceeds past the whisper_key guard to safe_download_audio.
        clinic_cfg = _clinic_config_with_stt(instance_id)

        with (
            patch.object(
                EventService,
                "_load_clinic_config",
                AsyncMock(return_value=clinic_cfg),
            ),
            patch(
                "src.api.routes.webhook.safe_download_audio",
                AsyncMock(
                    side_effect=UnsafeEgressError(
                        egress_code, url="https://cdn.example.com/audio.ogg"
                    )
                ),
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
        _seed_zapi_with_stt(
            client, tenant_id, instance_id, raw_token, stt_provider="outros"
        )

        clinic_cfg = _clinic_config_with_stt(instance_id)
        fake_download = SafeDownload(content=b"OGGS_AUDIO_BYTES", mime_type="audio/ogg")
        # transcribe_audio is lazily imported from src.services.groq_service inside
        # the handler. Patching at that module path intercepts the call. Raising an
        # exception causes the handler to return {"skipped": "transcription_failed"},
        # confirming the download step completed and STT was reached.
        transcribe_mock = AsyncMock(side_effect=Exception("stop-at-transcription"))

        with (
            patch.object(
                EventService,
                "_load_clinic_config",
                AsyncMock(return_value=clinic_cfg),
            ),
            patch(
                "src.api.routes.webhook.safe_download_audio",
                AsyncMock(return_value=fake_download),
            ),
            patch(
                "src.services.groq_service.transcribe_audio",
                transcribe_mock,
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
        # transcription_failed confirms the download completed and STT was reached
        assert body.get("skipped") == "transcription_failed", body
        transcribe_mock.assert_called_once()


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

        clinic_cfg = _clinic_config_with_stt(instance_id, stt_provider=provider)
        download_mock = AsyncMock(
            side_effect=UnsafeEgressError("audio_download_failed")
        )

        with (
            patch.object(
                EventService,
                "_load_clinic_config",
                AsyncMock(return_value=clinic_cfg),
            ),
            patch("src.api.routes.webhook.safe_download_audio", download_mock),
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
        resp = client.patch(
            API_CRED_PATH,
            json={
                "whatsapp_provider": "meta_cloud",
                "meta_phone_number_id": phone_number_id,
                "meta_app_secret": self.META_APP_SECRET,
                "meta_verify_token": f"vt_{uuid4().hex}",
                # meta_permanent_token supplies meta_token so the handler
                # can pass the no_credentials guard and reach the download step.
                "meta_permanent_token": f"EAA{uuid4().hex}",
            },
            headers=_admin_headers(tenant_id),
        )
        assert resp.status_code == 200, resp.text

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
        resp = client.patch(
            API_CRED_PATH,
            json={
                "whatsapp_provider": "meta_cloud",
                "meta_phone_number_id": phone_number_id,
                "meta_app_secret": self.META_APP_SECRET,
                "meta_verify_token": f"vt_{uuid4().hex}",
                "meta_permanent_token": f"EAA{uuid4().hex}",
                "stt_provider": stt_provider,
                # fresh_api_key_required policy fires on provider change;
                # unsupported providers have no runtime key but the policy
                # still demands one on first write.
                "stt_api_key": f"sk-dummy-{uuid4().hex}",
            },
            headers=_admin_headers(tenant_id),
        )
        assert resp.status_code == 200, resp.text

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
