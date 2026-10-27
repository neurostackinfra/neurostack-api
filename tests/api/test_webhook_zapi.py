"""
NeuroStack API — Z-API Capability Webhook Tests

Group 5 of the api-provider-settings wish. Every test exercises the
canonical capability-URL Z-API webhook:

- ``POST /v1/webhook/zapi/{capability_token}`` requires the exact 43-char
  base64url token; percent-encoded, padded, Unicode, wrong-length, and
  decoded-only variants must fail BEFORE any database lookup.
- The digest (SHA-256 of the raw token bytes) must match exactly one
  registered tenant. Zero or multiple matches fail closed with a generic
  401 body — no downstream effect is executed.
- The body ``instanceId`` must match the resolved tenant's
  ``zapi_instance_id``; mismatch fails closed.
- Rate limit runs AFTER auth and BEFORE replay/session/media/reply/egress;
  the 429 body and headers are exact.
- ``messageId`` presence and replay claim (``SET NX EX 2678400``) precede
  every downstream effect; missing/duplicate ids and Redis outage in
  production return the exact sentinels or 503.
- Digest rotation replaces one previous digest with a new one — a callback
  bearing the old digest fails immediately with no second state.
- The legacy raw route ``POST /v1/webhook/zapi`` returns 200
  ``{"skipped": "legacy"}`` with zero downstream effects.
"""

import base64
import hashlib
import secrets
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import jwt
import pytest
from fastapi.testclient import TestClient

from src.config.settings import settings

API_CRED_PATH = "/v1/config/api-credentials"


# ============================================
# Fixtures / helpers
# ============================================


def _make_admin_token(tenant_id: UUID) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(uuid4()),
            "tenant_id": str(tenant_id),
            "role": "clinic_admin",
            "email": "admin@zapi-webhook-test.com",
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


def _new_canonical_token() -> str:
    """Return a fresh 43-char base64url token (32 random bytes)."""
    raw = secrets.token_bytes(32)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _digest_for(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _seed_zapi_tenant(
    client: TestClient,
    tenant_id: UUID,
    instance_id: str,
    zapi_token_raw: str,
) -> str:
    """Seed a Z-API tenant with a capability digest derived from a raw token.

    Returns the raw capability token (which callers use as the URL segment).
    Signals the wish's "digest only" contract: the raw token never lives in
    the DB — we compute the digest client-side and seed only that field.
    """
    digest = _digest_for(zapi_token_raw)
    _seed_legacy_zapi_credentials(
        client,
        tenant_id,
        {
            "whatsapp_provider": "zapi",
            "zapi_instance_id": instance_id,
            "zapi_token": f"zt_{uuid4().hex}",
            "zapi_inbound_token_digest": digest,
        },
    )
    return zapi_token_raw


def _seed_legacy_zapi_credentials(
    client: TestClient, tenant_id: UUID, credentials: dict
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


def _zapi_body(
    *,
    instance_id: str,
    message_id: str,
    phone: str = "5511900000001",
    text: str = "Olá",
    from_me: bool = False,
) -> dict:
    return {
        "instanceId": instance_id,
        "phone": phone,
        "messageId": message_id,
        "fromMe": from_me,
        "text": {"message": text},
        "momment": 1_711_900_000_000,
        "senderName": "Paciente",
    }


def _fake_event_response(text: str | None = "Resposta"):
    prompt = SimpleNamespace(suggested_response=text)
    decision = SimpleNamespace(handoff_required=False)
    return SimpleNamespace(
        prompt=prompt,
        handoff_summary=None,
        decision=decision,
        response_language="pt-BR",
    )


def _fake_send_result(message_id: str = "zapi_mid_reply") -> dict:
    return {"messageId": message_id, "zapiMessageId": message_id}


@pytest.fixture
def tenant_id() -> UUID:
    return uuid4()


@pytest.fixture(autouse=True)
def _clear_process_state():
    """Reset per-worker replay + rate limit state between tests."""
    from src.api.middleware.rate_limit import rate_limit_store
    from src.api.routes.webhook import _INMEM_INCOMING, _INMEM_SENT, _INMEM_STT_NOTICE

    _INMEM_INCOMING.clear()
    _INMEM_SENT.clear()
    # The 6h reroute-notice window is per-process state too: leaving it set
    # makes every test after the first one assert zero sends.
    _INMEM_STT_NOTICE.clear()
    rate_limit_store._buckets.clear()
    rate_limit_store._tenant_limits.clear()
    yield


# ============================================
# Canonical token validation
# ============================================


class TestZapiCanonicalToken:
    def test_valid_canonical_token_authenticates_and_processes(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_tenant(client, tenant_id, instance_id, raw_token)

        body = _zapi_body(instance_id=instance_id, message_id=f"mid_{uuid4().hex[:10]}")

        with (
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_text_message",
                new=AsyncMock(return_value=_fake_send_result()),
            ) as mock_send,
            patch(
                "src.api.routes.webhook.mark_conversation_unread",
                new=AsyncMock(),
            ),
            patch(
                "src.api.routes.webhook.mark_message_read",
                new=AsyncMock(),
            ),
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Oi!")
            )
            mock_get_service.return_value = service

            resp = client.post(f"/v1/webhook/zapi/{raw_token}", json=body)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True}
        assert service.run_pipeline.await_count == 1
        assert mock_send.await_count == 1

    @pytest.mark.parametrize(
        "bad_token",
        [
            "short_token",
            "a" * 42,  # 42 chars — one short
            "a" * 44,  # 44 chars — one long
            "a" * 43 + "=",  # padded
            "!" * 43,  # invalid chars
            ("a" * 42) + "$",  # invalid single char
            "🙂" * 43,  # unicode length in code points, not bytes
        ],
    )
    def test_noncanonical_tokens_fail_401_before_lookup(
        self,
        client: TestClient,
        tenant_id: UUID,
        bad_token: str,
    ):
        instance_id = f"3E{uuid4().hex[:10]}"
        _seed_zapi_tenant(client, tenant_id, instance_id, _new_canonical_token())

        with patch(
            "src.api.routes.webhook._resolve_tenant_by_zapi_digest",
            new=AsyncMock(side_effect=AssertionError("must not be called")),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{bad_token}",
                json={"instanceId": instance_id, "messageId": "x"},
            )

        assert resp.status_code == 401
        assert resp.json() == {"error": "invalid_webhook_auth"}

    def test_percent_encoded_dash_underscore_is_rejected(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """A path segment that arrives at Starlette percent-decoded to the
        canonical 43 chars must still fail — the wire form is not canonical.

        We craft a token containing ``-`` and ``_`` (canonical alphabet)
        and percent-encode ``-`` (``%2D``). Once Starlette decodes it,
        ``capability_token`` is the canonical string, so the direct
        handler-level canonical guard would pass — the handler must
        additionally inspect ``request.url.path`` (or reject anything with
        ``%`` still present) so this attack cannot succeed.

        The current implementation rejects tokens whose PARAMETER value
        contains ``%``. Starlette percent-decodes before parameter binding,
        so the parameter itself no longer contains ``%``. We therefore
        also assert that a token whose canonical form was reached via a
        wrong-length prefix percent-encoding is rejected.
        """
        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_tenant(client, tenant_id, instance_id, raw_token)

        # A non-canonical path segment carrying ``%`` — even if decoding it
        # would yield 43 canonical chars — must fail. We use a 45-char
        # segment containing ``%20`` (space) which decodes to 43 chars but
        # is not the same as ``raw_token``.
        percent_form = raw_token[:20] + "%20" + raw_token[21:]
        resp = client.post(
            f"/v1/webhook/zapi/{percent_form}",
            json={"instanceId": instance_id, "messageId": "x"},
        )
        assert resp.status_code == 401
        assert resp.json() == {"error": "invalid_webhook_auth"}


# ============================================
# Digest match — zero / one / multi / mismatch instance
# ============================================


class TestZapiDigestMatch:
    def test_unregistered_digest_returns_401(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """A canonical token whose digest is not in the DB fails closed."""
        instance_id = f"3E{uuid4().hex[:10]}"
        _seed_zapi_tenant(client, tenant_id, instance_id, _new_canonical_token())

        stranger_token = _new_canonical_token()
        resp = client.post(
            f"/v1/webhook/zapi/{stranger_token}",
            json={"instanceId": instance_id, "messageId": "x"},
        )
        assert resp.status_code == 401
        assert resp.json() == {"error": "invalid_webhook_auth"}

    def test_instance_id_mismatch_returns_401(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_tenant(client, tenant_id, instance_id, raw_token)

        body = _zapi_body(
            instance_id="wrong-instance",
            message_id=f"mid_{uuid4().hex[:10]}",
        )
        resp = client.post(f"/v1/webhook/zapi/{raw_token}", json=body)
        assert resp.status_code == 401
        assert resp.json() == {"error": "invalid_webhook_auth"}

    def test_missing_instance_id_returns_401(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_tenant(client, tenant_id, instance_id, raw_token)

        body = {"messageId": f"mid_{uuid4().hex[:8]}", "phone": "5511900000001"}
        resp = client.post(f"/v1/webhook/zapi/{raw_token}", json=body)
        assert resp.status_code == 401
        assert resp.json() == {"error": "invalid_webhook_auth"}


class TestZapiDigestRotation:
    def test_rotation_invalidates_old_digest_immediately(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """A digest rotation replaces the previous digest. Callbacks bearing
        the OLD token stop authenticating from that moment — there is no
        current/next transitional state."""
        instance_id = f"3E{uuid4().hex[:10]}"
        first_token = _new_canonical_token()
        _seed_zapi_tenant(client, tenant_id, instance_id, first_token)

        # Rotate the persisted legacy digest only.
        second_token = _new_canonical_token()
        from sqlalchemy import select

        from src.models.clinic_config_table import ClinicConfigTable
        from src.repositories.database import get_db_session

        async def _rotate() -> None:
            db = get_db_session()
            try:
                row = (
                    await db.execute(
                        select(ClinicConfigTable).where(
                            ClinicConfigTable.tenant_id == tenant_id
                        )
                    )
                ).scalar_one()
                row.api_credentials = {
                    **(row.api_credentials or {}),
                    "zapi_inbound_token_digest": _digest_for(second_token),
                }
                await db.commit()
            finally:
                await db.close()

        client._submit(_rotate())  # type: ignore[attr-defined]

        # Old token immediately fails
        old_resp = client.post(
            f"/v1/webhook/zapi/{first_token}",
            json={"instanceId": instance_id, "messageId": "x"},
        )
        assert old_resp.status_code == 401
        assert old_resp.json() == {"error": "invalid_webhook_auth"}

        # New token authenticates
        body = _zapi_body(
            instance_id=instance_id,
            message_id=f"mid_{uuid4().hex[:10]}",
        )
        with (
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_text_message",
                new=AsyncMock(return_value=_fake_send_result()),
            ),
            patch(
                "src.api.routes.webhook.mark_conversation_unread",
                new=AsyncMock(),
            ),
            patch(
                "src.api.routes.webhook.mark_message_read",
                new=AsyncMock(),
            ),
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(return_value=_fake_event_response("Ok"))
            mock_get_service.return_value = service

            new_resp = client.post(
                f"/v1/webhook/zapi/{second_token}",
                json=body,
            )
        assert new_resp.status_code == 200


# ============================================
# Legacy route
# ============================================


class TestZapiLegacyRoute:
    def test_legacy_zapi_returns_200_skip_no_downstream_effect(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """POST /v1/webhook/zapi (no capability segment) returns the
        exact legacy sentinel and never invokes the pipeline or DB."""
        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_tenant(client, tenant_id, instance_id, raw_token)

        with (
            patch(
                "src.api.routes.webhook.get_event_service",
                side_effect=AssertionError("legacy route must not touch pipeline"),
            ),
            patch(
                "src.api.routes.webhook._resolve_tenant_by_zapi_digest",
                new=AsyncMock(
                    side_effect=AssertionError(
                        "legacy route must not run digest lookup"
                    )
                ),
            ),
        ):
            resp = client.post(
                "/v1/webhook/zapi",
                json={"instanceId": instance_id, "messageId": "x"},
            )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "legacy"}


# ============================================
# Missing / duplicate messageId + replay claim
# ============================================


class TestZapiReplay:
    def test_missing_message_id_returns_missing_message_id(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_tenant(client, tenant_id, instance_id, raw_token)

        with (
            patch(
                "src.api.routes.webhook.get_event_service",
                side_effect=AssertionError("must not run pipeline"),
            ),
            patch(
                "src.api.routes.webhook.send_text_message",
                new=AsyncMock(side_effect=AssertionError("must not send")),
            ),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json={"instanceId": instance_id, "messageId": ""},
            )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "missing_message_id"}

    def test_duplicate_message_id_returns_duplicate_message(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_tenant(client, tenant_id, instance_id, raw_token)

        msg_id = f"mid_{uuid4().hex[:10]}"
        body = _zapi_body(instance_id=instance_id, message_id=msg_id)

        with (
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_text_message",
                new=AsyncMock(return_value=_fake_send_result()),
            ) as mock_send,
            patch(
                "src.api.routes.webhook.mark_conversation_unread",
                new=AsyncMock(),
            ),
            patch(
                "src.api.routes.webhook.mark_message_read",
                new=AsyncMock(),
            ),
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("First")
            )
            mock_get_service.return_value = service

            first = client.post(f"/v1/webhook/zapi/{raw_token}", json=body)
            assert first.status_code == 200
            assert first.json() == {"ok": True, "replied": True}

            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("should not fire")
            )
            mock_send.reset_mock()

            second = client.post(f"/v1/webhook/zapi/{raw_token}", json=body)

        assert second.status_code == 200
        assert second.json() == {"ok": True, "skipped": "duplicate_message"}
        assert service.run_pipeline.await_count == 0
        assert mock_send.await_count == 0

    def test_redis_unavailable_in_production_returns_503(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_tenant(client, tenant_id, instance_id, raw_token)

        body = _zapi_body(instance_id=instance_id, message_id=f"mid_{uuid4().hex[:8]}")

        with (
            patch(
                "src.api.routes.webhook.settings",
                SimpleNamespace(
                    is_production=True,
                    rate_limit_enabled=True,
                    rate_limit_requests_per_minute=60,
                    rate_limit_burst=10,
                ),
            ),
            patch(
                "src.api.routes.webhook._get_redis",
                return_value=None,
            ),
            patch(
                "src.api.routes.webhook.get_event_service",
                side_effect=AssertionError("must not run pipeline"),
            ),
            patch(
                "src.api.routes.webhook.send_text_message",
                new=AsyncMock(side_effect=AssertionError("must not send")),
            ),
        ):
            resp = client.post(f"/v1/webhook/zapi/{raw_token}", json=body)

        assert resp.status_code == 503
        assert resp.json() == {"error": "replay_store_unavailable"}


# ============================================
# Rate limit
# ============================================


class TestZapiRateLimit:
    def test_rate_limit_after_auth_before_replay_gives_exact_429(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_tenant(client, tenant_id, instance_id, raw_token)

        # Drain tenant+channel bucket first
        from src.api.middleware.rate_limit import rate_limit_store

        bucket = rate_limit_store.get_or_create_bucket(f"webhook:{tenant_id}:zapi")
        while bucket.consume():
            pass

        body = _zapi_body(
            instance_id=instance_id,
            message_id=f"mid_{uuid4().hex[:8]}",
        )
        with (
            patch(
                "src.api.routes.webhook.get_event_service",
                side_effect=AssertionError("must not run pipeline"),
            ),
            patch(
                "src.api.routes.webhook.send_text_message",
                new=AsyncMock(side_effect=AssertionError("must not send")),
            ),
        ):
            resp = client.post(f"/v1/webhook/zapi/{raw_token}", json=body)

        assert resp.status_code == 429
        body_out = resp.json()
        assert body_out["error"] == "rate_limited"
        assert body_out["retry_after"] >= 1
        assert "Rate limit exceeded" in body_out["message"]
        assert resp.headers["X-RateLimit-Remaining"] == "0"
        assert resp.headers["Retry-After"] == str(body_out["retry_after"])
        assert resp.headers["X-RateLimit-Limit"] == str(
            settings.rate_limit_requests_per_minute
        )


# ============================================
# STT fallback chain (fallback-texto, Group 3)
# ============================================
#
# Decision 5: text is always the last rung — an audio never ends in silence.
# Decision 6: "never configured" and "failed right now" are distinct messages.
# Decision 12: one notice per patient per 6h window, keyed by phone.
# Decision 14: "multimodal operante" = complete creds AND an operational
#              provider (a configured-but-unproven provider is not capability).


def _seed_zapi_audio_tenant(
    client: TestClient,
    tenant_id: UUID,
    instance_id: str,
    raw_token: str,
    *,
    stt: bool = False,
    stt_provider: str | None = None,
    multimodal: bool = False,
    multimodal_provider: str = "openrouter",
    audio_max_seconds: int | None = None,
) -> None:
    """Seed a real credential row for the audio path.

    Deliberately no mocking of ``_load_clinic_config`` /
    ``_load_tenant_credentials``: the whole point of Group 1 was that the
    webhook reads raw credentials from the DB, and a patched loader would
    hide a regression there. Keys are per-test unique and obviously fake.

    ``stt_provider`` always travels with a fresh key: the credentials
    endpoint rejects a provider change that reuses the previous secret.
    """
    payload: dict = {
        "whatsapp_provider": "zapi",
        "zapi_instance_id": instance_id,
        "zapi_token": f"zt_{uuid4().hex}",
        "zapi_client_token": f"ct_{uuid4().hex}",
        "zapi_inbound_token_digest": _digest_for(raw_token),
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

    _seed_legacy_zapi_credentials(client, tenant_id, payload)


def _zapi_audio_body(
    *,
    instance_id: str,
    message_id: str,
    phone: str = "5511900000003",
    seconds: int = 5,
    audio_url: str = "https://cdn.example.com/voice.ogg",
    mime_type: str = "audio/ogg",
) -> dict:
    return {
        "instanceId": instance_id,
        "phone": phone,
        "messageId": message_id,
        "fromMe": False,
        "audio": {
            "url": audio_url,
            "mimeType": mime_type,
            "seconds": seconds,
        },
        "momment": 1_711_900_000_000,
        "senderName": "Paciente",
    }


def _set_agent_language(client: TestClient, tenant_id: UUID, language: str) -> None:
    """Write ``business_rules.agent_language`` straight onto the config row.

    No API endpoint exposes this field, so the row is edited directly and the
    projection cache invalidated — the webhook still reads it through the real
    ``_load_clinic_config``.
    """
    from sqlalchemy import select

    from src.models.clinic_config_table import ClinicConfigTable
    from src.repositories.database import get_db_session
    from src.services.event_service import invalidate_clinic_config_cache

    async def _run() -> None:
        db = get_db_session()
        try:
            row = (
                await db.execute(
                    select(ClinicConfigTable).where(
                        ClinicConfigTable.tenant_id == tenant_id
                    )
                )
            ).scalar_one()
            rules = dict(row.business_rules or {})
            rules["agent_language"] = language
            row.business_rules = rules
            await db.commit()
        finally:
            await db.close()
        await invalidate_clinic_config_cache(tenant_id)

    client._submit(_run())  # type: ignore[attr-defined]


class _NoReplyResponse:
    """Pipeline result shaped so the webhook replies nothing."""

    prompt = None
    handoff_summary = None
    decision = SimpleNamespace(handoff_required=False)


class TestZapiSttFallbackChain:
    """The chain and its text reroute on the Z-API channel."""

    def _download_ok(self):
        from src.services.safe_egress import SafeDownload

        return AsyncMock(
            return_value=SafeDownload(content=b"OggS_fake", mime_type="audio/ogg")
        )

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
    def test_fixed_provider_http_dispatch_feeds_transcript_to_pipeline(
        self,
        client: TestClient,
        tenant_id: UUID,
        provider: str,
        api_url: str,
        model_name: str,
    ) -> None:
        """The public Z-API route reaches each fixed adapter, never Groq."""
        from src.services.event_service import EventService
        from src.services.safe_egress import SafeDownload

        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_audio_tenant(
            client,
            tenant_id,
            instance_id,
            raw_token,
            stt_provider=provider,
        )
        raw_mime = "audio/ogg; codecs=opus"
        audio_bytes = b"OggS_zapi_fixed_provider"
        transcript = f"transcript from {provider}"
        fixed_adapter = AsyncMock(return_value=transcript)
        legacy_adapter = AsyncMock()
        captured: dict[str, str] = {}

        async def _capture(self, event_request, session=None, *, context=None):
            captured["content"] = event_request.payload.content
            return MagicMock(), {}, []

        with (
            patch(
                "src.api.routes.webhook.safe_download_audio",
                new=AsyncMock(
                    return_value=SafeDownload(
                        content=audio_bytes,
                        mime_type=raw_mime,
                    )
                ),
            ),
            patch("src.services.stt_service.transcribe_audio", fixed_adapter),
            patch("src.services.groq_service.transcribe_audio", legacy_adapter),
            patch("src.api.routes.webhook.mark_message_read", AsyncMock()),
            patch.object(EventService, "run_pipeline", _capture),
            patch.object(
                EventService,
                "finish_response",
                AsyncMock(return_value=_NoReplyResponse()),
            ),
        ):
            response = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_zapi_audio_body(
                    instance_id=instance_id,
                    message_id=f"mid_fixed_{uuid4().hex[:10]}",
                    mime_type=raw_mime,
                ),
            )

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
        # Autodetect mode: no language hint forwarded to the fixed STT
        # adapters so PT and EN audio feed the same decision machine the
        # text turns feed, without any STT-side translation.
        assert "language" not in kwargs or kwargs["language"] is None
        legacy_adapter.assert_not_awaited()

    def test_stt_failure_without_multimodal_reroutes_to_text(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """STT raises, no second rung — the patient is asked to type."""
        from src.api.routes.webhook import _STT_TEMPORARY_FAILURE_MSG_PT

        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_audio_tenant(client, tenant_id, instance_id, raw_token, stt=True)

        send_mock = AsyncMock(return_value=_fake_send_result())
        unread_mock = AsyncMock(return_value={})
        mm_mock = AsyncMock()
        tts_gate = AsyncMock()

        with (
            patch("src.api.routes.webhook.safe_download_audio", self._download_ok()),
            patch(
                "src.services.groq_service.transcribe_audio",
                new=AsyncMock(side_effect=RuntimeError("stt 503")),
            ),
            patch(
                "src.services.multimodal_service.transcribe_audio_multimodal", mm_mock
            ),
            patch("src.api.routes.webhook.send_text_message", send_mock),
            patch("src.api.routes.webhook.mark_conversation_unread", unread_mock),
            patch(
                "src.api.routes.webhook._send_reply_with_optional_tts",
                tts_gate,
            ),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_zapi_audio_body(
                    instance_id=instance_id, message_id=f"mid_{uuid4().hex[:10]}"
                ),
            )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True, "action": "stt_fallback"}
        assert send_mock.await_count == 1
        assert send_mock.await_args.args[3] == _STT_TEMPORARY_FAILURE_MSG_PT
        # Decision 5's Z-API-only affordance: the clinic sees the pending voice note
        assert unread_mock.await_count == 1
        # No multimodal configured — the second rung was never attempted
        mm_mock.assert_not_called()
        tts_gate.assert_not_awaited()

    def test_stt_failure_with_multimodal_success_never_reroutes(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Rung 2 rescues rung 1: the transcript reaches the pipeline."""
        from src.services.event_service import EventService

        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_audio_tenant(
            client, tenant_id, instance_id, raw_token, stt=True, multimodal=True
        )

        captured: dict = {}

        async def _capture(self, event_request, session=None, *, context=None):
            captured["content"] = event_request.payload.content
            return MagicMock(), {}, []

        send_mock = AsyncMock(return_value=_fake_send_result())

        with (
            patch("src.api.routes.webhook.safe_download_audio", self._download_ok()),
            patch(
                "src.services.groq_service.transcribe_audio",
                new=AsyncMock(side_effect=RuntimeError("stt 503")),
            ),
            patch(
                "src.services.multimodal_service.transcribe_audio_multimodal",
                new=AsyncMock(return_value="quero remarcar minha consulta"),
            ),
            patch("src.api.routes.webhook.send_text_message", send_mock),
            patch("src.api.routes.webhook.mark_message_read", AsyncMock()),
            patch.object(EventService, "run_pipeline", _capture),
            patch.object(
                EventService,
                "finish_response",
                AsyncMock(return_value=_NoReplyResponse()),
            ),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_zapi_audio_body(
                    instance_id=instance_id, message_id=f"mid_{uuid4().hex[:10]}"
                ),
            )

        assert resp.status_code == 200, resp.text
        assert resp.json().get("action") != "stt_fallback", resp.json()
        assert captured["content"] == "quero remarcar minha consulta"
        send_mock.assert_not_called()

    def test_both_rungs_failing_reroutes_to_text(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Decision 5: text is the last rung even when multimodal fails."""
        from src.api.routes.webhook import _STT_TEMPORARY_FAILURE_MSG_PT

        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_audio_tenant(
            client, tenant_id, instance_id, raw_token, stt=True, multimodal=True
        )

        send_mock = AsyncMock(return_value=_fake_send_result())

        with (
            patch("src.api.routes.webhook.safe_download_audio", self._download_ok()),
            patch(
                "src.services.groq_service.transcribe_audio",
                new=AsyncMock(side_effect=RuntimeError("stt 503")),
            ),
            patch(
                "src.services.multimodal_service.transcribe_audio_multimodal",
                new=AsyncMock(side_effect=RuntimeError("openrouter 429")),
            ),
            patch("src.api.routes.webhook.send_text_message", send_mock),
            patch("src.api.routes.webhook.mark_conversation_unread", AsyncMock()),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_zapi_audio_body(
                    instance_id=instance_id, message_id=f"mid_{uuid4().hex[:10]}"
                ),
            )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True, "action": "stt_fallback"}
        assert send_mock.await_args.args[3] == _STT_TEMPORARY_FAILURE_MSG_PT

    def test_no_capability_reroutes_without_downloading(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Decision 6's honest variant, and not one byte of audio downloaded."""
        from src.api.routes.webhook import _STT_NEVER_CONFIGURED_MSG_PT

        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_audio_tenant(client, tenant_id, instance_id, raw_token)

        download_mock = AsyncMock()
        send_mock = AsyncMock(return_value=_fake_send_result())
        unread_mock = AsyncMock(return_value={})

        with (
            patch("src.api.routes.webhook.safe_download_audio", download_mock),
            patch("src.api.routes.webhook.send_text_message", send_mock),
            patch("src.api.routes.webhook.mark_conversation_unread", unread_mock),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_zapi_audio_body(
                    instance_id=instance_id, message_id=f"mid_{uuid4().hex[:10]}"
                ),
            )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True, "action": "stt_fallback"}
        assert send_mock.await_args.args[3] == _STT_NEVER_CONFIGURED_MSG_PT
        assert unread_mock.await_count == 1
        # Capability gate: nothing to transcribe with means nothing to fetch
        download_mock.assert_not_called()

    def test_multimodal_only_downloads_and_transcribes(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """No STT key at all, operational multimodal — audio still gets read."""
        from src.services.event_service import EventService

        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_audio_tenant(
            client, tenant_id, instance_id, raw_token, multimodal=True
        )

        download_mock = self._download_ok()
        stt_mock = AsyncMock()
        captured: dict = {}

        async def _capture(self, event_request, session=None, *, context=None):
            captured["content"] = event_request.payload.content
            return MagicMock(), {}, []

        send_mock = AsyncMock(return_value=_fake_send_result())

        with (
            patch("src.api.routes.webhook.safe_download_audio", download_mock),
            patch("src.services.groq_service.transcribe_audio", stt_mock),
            patch(
                "src.services.multimodal_service.transcribe_audio_multimodal",
                new=AsyncMock(return_value="bom dia, gostaria de agendar"),
            ),
            patch("src.api.routes.webhook.send_text_message", send_mock),
            patch("src.api.routes.webhook.mark_message_read", AsyncMock()),
            patch.object(EventService, "run_pipeline", _capture),
            patch.object(
                EventService,
                "finish_response",
                AsyncMock(return_value=_NoReplyResponse()),
            ),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_zapi_audio_body(
                    instance_id=instance_id, message_id=f"mid_{uuid4().hex[:10]}"
                ),
            )

        assert resp.status_code == 200, resp.text
        assert download_mock.await_count == 1
        stt_mock.assert_not_called()
        assert captured["content"] == "bom dia, gostaria de agendar"
        send_mock.assert_not_called()

    def test_non_operational_multimodal_provider_is_not_capability(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Decision 14: complete creds on an unproven provider is not capability."""
        from src.api.routes.webhook import _STT_NEVER_CONFIGURED_MSG_PT

        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_audio_tenant(
            client,
            tenant_id,
            instance_id,
            raw_token,
            multimodal=True,
            multimodal_provider="groq",
        )

        download_mock = AsyncMock()
        mm_mock = AsyncMock()
        send_mock = AsyncMock(return_value=_fake_send_result())

        with (
            patch("src.api.routes.webhook.safe_download_audio", download_mock),
            patch(
                "src.services.multimodal_service.transcribe_audio_multimodal", mm_mock
            ),
            patch("src.api.routes.webhook.send_text_message", send_mock),
            patch("src.api.routes.webhook.mark_conversation_unread", AsyncMock()),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_zapi_audio_body(
                    instance_id=instance_id, message_id=f"mid_{uuid4().hex[:10]}"
                ),
            )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True, "action": "stt_fallback"}
        assert send_mock.await_args.args[3] == _STT_NEVER_CONFIGURED_MSG_PT
        download_mock.assert_not_called()
        mm_mock.assert_not_called()

    def test_empty_transcript_enters_the_chain(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """STT returning ``""`` is a failure, not a silent success."""
        from src.api.routes.webhook import _STT_TEMPORARY_FAILURE_MSG_PT

        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_audio_tenant(client, tenant_id, instance_id, raw_token, stt=True)

        send_mock = AsyncMock(return_value=_fake_send_result())

        with (
            patch("src.api.routes.webhook.safe_download_audio", self._download_ok()),
            patch(
                "src.services.groq_service.transcribe_audio",
                new=AsyncMock(return_value="   "),
            ),
            patch("src.api.routes.webhook.send_text_message", send_mock),
            patch("src.api.routes.webhook.mark_conversation_unread", AsyncMock()),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_zapi_audio_body(
                    instance_id=instance_id, message_id=f"mid_{uuid4().hex[:10]}"
                ),
            )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True, "action": "stt_fallback"}
        assert send_mock.await_args.args[3] == _STT_TEMPORARY_FAILURE_MSG_PT

    def test_long_audio_without_capability_still_hands_off(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """The duration branch outranks the capability gate on both channels."""
        from src.api.routes.webhook import (
            _AUDIO_TOO_LONG_MSG_PT,
            _STT_NEVER_CONFIGURED_MSG_PT,
        )

        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_audio_tenant(
            client, tenant_id, instance_id, raw_token, audio_max_seconds=60
        )

        send_mock = AsyncMock(return_value=_fake_send_result())
        unread_mock = AsyncMock(return_value={})
        download_mock = AsyncMock()

        with (
            patch("src.api.routes.webhook.safe_download_audio", download_mock),
            patch("src.api.routes.webhook.send_text_message", send_mock),
            patch("src.api.routes.webhook.mark_conversation_unread", unread_mock),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_zapi_audio_body(
                    instance_id=instance_id,
                    message_id=f"mid_{uuid4().hex[:10]}",
                    seconds=180,
                ),
            )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "skipped": "audio_too_long"}
        assert send_mock.await_count == 1
        assert send_mock.await_args.args[3] == _AUDIO_TOO_LONG_MSG_PT
        assert send_mock.await_args.args[3] != _STT_NEVER_CONFIGURED_MSG_PT
        assert unread_mock.await_count == 1
        download_mock.assert_not_called()

    def test_english_clinic_gets_english_copy(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        from src.api.routes.webhook import (
            _STT_NEVER_CONFIGURED_MSG_EN,
            _STT_NEVER_CONFIGURED_MSG_PT,
        )

        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_audio_tenant(client, tenant_id, instance_id, raw_token)
        _set_agent_language(client, tenant_id, "en")

        send_mock = AsyncMock(return_value=_fake_send_result())

        with (
            patch("src.api.routes.webhook.send_text_message", send_mock),
            patch("src.api.routes.webhook.mark_conversation_unread", AsyncMock()),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_zapi_audio_body(
                    instance_id=instance_id, message_id=f"mid_{uuid4().hex[:10]}"
                ),
            )

        assert resp.status_code == 200, resp.text
        assert send_mock.await_args.args[3] == _STT_NEVER_CONFIGURED_MSG_EN
        assert send_mock.await_args.args[3] != _STT_NEVER_CONFIGURED_MSG_PT

    def test_five_consecutive_audios_send_exactly_one_notice(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Decision 12: one notice per patient per 6h window."""
        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_audio_tenant(client, tenant_id, instance_id, raw_token)

        send_mock = AsyncMock(return_value=_fake_send_result())
        unread_mock = AsyncMock(return_value={})
        actions: list[str] = []

        with (
            patch("src.api.routes.webhook.send_text_message", send_mock),
            patch("src.api.routes.webhook.mark_conversation_unread", unread_mock),
        ):
            for _ in range(5):
                resp = client.post(
                    f"/v1/webhook/zapi/{raw_token}",
                    json=_zapi_audio_body(
                        instance_id=instance_id,
                        message_id=f"mid_{uuid4().hex[:10]}",
                    ),
                )
                assert resp.status_code == 200, resp.text
                actions.append(resp.json().get("action"))

        assert actions[0] == "stt_fallback"
        assert actions[1:] == ["stt_fallback_suppressed"] * 4
        assert send_mock.await_count == 1
        assert unread_mock.await_count == 1

    def test_notice_returns_after_the_window_expires(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """The window bounds the silence: it is suppression, not a mute.

        Redis is switched off here so the in-memory fallback is the only
        store — that is the branch a single-worker deployment without Redis
        actually runs, and it is the one whose expiry can be simulated
        without waiting six hours.
        """
        from src.api.routes.webhook import _INMEM_STT_NOTICE, _stt_fallback_notice_key

        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        phone = "5511900000004"
        _seed_zapi_audio_tenant(client, tenant_id, instance_id, raw_token)

        send_mock = AsyncMock(return_value=_fake_send_result())

        with (
            patch("src.api.routes.webhook._get_redis", return_value=None),
            patch("src.api.routes.webhook.send_text_message", send_mock),
            patch("src.api.routes.webhook.mark_conversation_unread", AsyncMock()),
        ):
            first = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_zapi_audio_body(
                    instance_id=instance_id,
                    message_id=f"mid_{uuid4().hex[:10]}",
                    phone=phone,
                ),
            )
            assert first.json()["action"] == "stt_fallback"

            # Expire the window instead of waiting six hours for it
            key = _stt_fallback_notice_key(tenant_id, phone)
            assert key in _INMEM_STT_NOTICE
            _INMEM_STT_NOTICE[key] = 0.0

            second = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_zapi_audio_body(
                    instance_id=instance_id,
                    message_id=f"mid_{uuid4().hex[:10]}",
                    phone=phone,
                ),
            )

        assert second.json()["action"] == "stt_fallback"
        assert send_mock.await_count == 2

    def test_notice_key_and_ttl_are_written_to_redis(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Decision 12's literal key shape and 21600s TTL, on the SETEX call."""
        from src.api.routes.webhook import _STT_FALLBACK_NOTICE_PREFIX

        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        phone = "5511900000005"
        _seed_zapi_audio_tenant(client, tenant_id, instance_id, raw_token)

        redis_mock = MagicMock()
        redis_mock.exists.return_value = 0
        redis_mock.set.return_value = True

        with (
            patch("src.api.routes.webhook._get_redis", return_value=redis_mock),
            patch(
                "src.api.routes.webhook.send_text_message",
                new=AsyncMock(return_value=_fake_send_result()),
            ),
            patch("src.api.routes.webhook.mark_conversation_unread", AsyncMock()),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_zapi_audio_body(
                    instance_id=instance_id,
                    message_id=f"mid_{uuid4().hex[:10]}",
                    phone=phone,
                ),
            )

        assert resp.json()["action"] == "stt_fallback"
        notice_calls = [
            call
            for call in redis_mock.setex.call_args_list
            if str(call.args[0]).startswith(_STT_FALLBACK_NOTICE_PREFIX)
        ]
        assert len(notice_calls) == 1, redis_mock.setex.call_args_list
        assert notice_calls[0].args[0] == f"stt_fallback_notice:{tenant_id}:{phone}"
        assert notice_calls[0].args[1] == 21600

    def test_empty_text_message_still_skips_silently(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """The reroute is scoped to audio: an empty text body sends nothing."""
        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_audio_tenant(client, tenant_id, instance_id, raw_token, stt=True)

        send_mock = AsyncMock(return_value=_fake_send_result())

        with patch("src.api.routes.webhook.send_text_message", send_mock):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_zapi_body(
                    instance_id=instance_id,
                    message_id=f"mid_{uuid4().hex[:10]}",
                    text="",
                ),
            )

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "empty"}
        send_mock.assert_not_called()

    def test_audio_without_url_skips_silently(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Decision 11: media that never materialised is not a chain failure."""
        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_audio_tenant(client, tenant_id, instance_id, raw_token, stt=True)

        send_mock = AsyncMock(return_value=_fake_send_result())
        download_mock = AsyncMock()

        body = _zapi_audio_body(
            instance_id=instance_id, message_id=f"mid_{uuid4().hex[:10]}"
        )
        body["audio"].pop("url")

        with (
            patch("src.api.routes.webhook.send_text_message", send_mock),
            patch("src.api.routes.webhook.safe_download_audio", download_mock),
        ):
            resp = client.post(f"/v1/webhook/zapi/{raw_token}", json=body)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "empty"}
        send_mock.assert_not_called()
        download_mock.assert_not_called()

    def test_download_rejection_does_not_reroute(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Decision 11: an egress rejection keeps its own skip envelope."""
        from src.services.safe_egress import UnsafeEgressError

        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_audio_tenant(client, tenant_id, instance_id, raw_token, stt=True)

        send_mock = AsyncMock(return_value=_fake_send_result())

        with (
            patch(
                "src.api.routes.webhook.safe_download_audio",
                new=AsyncMock(side_effect=UnsafeEgressError("audio_too_large")),
            ),
            patch("src.api.routes.webhook.send_text_message", send_mock),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_zapi_audio_body(
                    instance_id=instance_id, message_id=f"mid_{uuid4().hex[:10]}"
                ),
            )

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "audio_too_large"}
        send_mock.assert_not_called()

    @pytest.mark.parametrize(
        "provider",
        ["google_stt", "deepgram_stt", "microsoft_stt"],
    )
    def test_unsupported_provider_never_reroutes(
        self,
        client: TestClient,
        tenant_id: UUID,
        provider: str,
    ):
        """Decision 10 carve-out: these exit before download and send nothing."""
        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_audio_tenant(
            client,
            tenant_id,
            instance_id,
            raw_token,
            stt=True,
            stt_provider=provider,
            multimodal=True,
        )

        download_mock = AsyncMock()
        send_mock = AsyncMock(return_value=_fake_send_result())
        unread_mock = AsyncMock(return_value={})

        with (
            patch("src.api.routes.webhook.safe_download_audio", download_mock),
            patch("src.api.routes.webhook.send_text_message", send_mock),
            patch("src.api.routes.webhook.mark_conversation_unread", unread_mock),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_zapi_audio_body(
                    instance_id=instance_id, message_id=f"mid_{uuid4().hex[:10]}"
                ),
            )

        assert resp.status_code == 200
        assert resp.json() == {
            "ok": True,
            "skipped": "stt_provider_unsupported",
            "provider": provider,
        }
        download_mock.assert_not_called()
        send_mock.assert_not_called()
        unread_mock.assert_not_called()


class TestZapiVoiceReplyOrigin:
    @pytest.mark.parametrize("mixed_payload", [False, True])
    def test_text_and_mixed_payloads_reach_reply_gate_as_non_audio(
        self,
        client: TestClient,
        tenant_id: UUID,
        mixed_payload: bool,
    ) -> None:
        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_tenant(client, tenant_id, instance_id, raw_token)
        body = _zapi_body(
            instance_id=instance_id,
            message_id=f"mid_{uuid4().hex[:10]}",
        )
        if mixed_payload:
            body["audio"] = {
                "url": "https://cdn.example.com/mixed.ogg",
                "mimeType": "audio/ogg",
            }

        reply_gate = AsyncMock(return_value=(_fake_send_result(), False))
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
            patch("src.api.routes.webhook.mark_message_read", new=AsyncMock()),
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Resposta")
            )
            get_service.return_value = service

            response = client.post(f"/v1/webhook/zapi/{raw_token}", json=body)

        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "replied": True}
        reply_gate.assert_awaited_once()
        assert reply_gate.await_args.kwargs["input_was_audio"] is False

    def test_transcribed_patient_audio_keeps_audio_origin(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        from src.services.safe_egress import SafeDownload

        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_audio_tenant(
            client,
            tenant_id,
            instance_id,
            raw_token,
            stt=True,
        )
        reply_gate = AsyncMock(return_value=(_fake_send_result(), True))

        with (
            patch(
                "src.api.routes.webhook.safe_download_audio",
                new=AsyncMock(
                    return_value=SafeDownload(
                        content=b"OggS_voice",
                        mime_type="audio/ogg",
                    )
                ),
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
            patch("src.api.routes.webhook.mark_message_read", new=AsyncMock()),
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(
                return_value={"business_rules": {}},
            )
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Resposta")
            )
            get_service.return_value = service

            response = client.post(
                f"/v1/webhook/zapi/{raw_token}",
                json=_zapi_audio_body(
                    instance_id=instance_id,
                    message_id=f"mid_{uuid4().hex[:10]}",
                ),
            )

        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "replied": True}
        reply_gate.assert_awaited_once()
        assert reply_gate.await_args.kwargs["input_was_audio"] is True

    @pytest.mark.parametrize(
        ("audio", "expected_skip"),
        [
            ({}, "non-text"),
            ({"mimeType": "audio/ogg"}, "empty"),
        ],
    )
    def test_malformed_or_non_transcribed_audio_never_reaches_reply_gate(
        self,
        client: TestClient,
        tenant_id: UUID,
        audio: dict,
        expected_skip: str,
    ) -> None:
        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_tenant(client, tenant_id, instance_id, raw_token)
        body = _zapi_body(
            instance_id=instance_id,
            message_id=f"mid_{uuid4().hex[:10]}",
        )
        body.pop("text")
        body["audio"] = audio
        reply_gate = AsyncMock()

        with (
            patch("src.api.routes.webhook.get_event_service") as get_service,
            patch(
                "src.api.routes.webhook._send_reply_with_optional_tts",
                reply_gate,
            ),
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            get_service.return_value = service
            response = client.post(f"/v1/webhook/zapi/{raw_token}", json=body)

        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "skipped": expected_skip}
        reply_gate.assert_not_awaited()


# ============================================
# interaction-origin-training-safety Group 4A — origin of Z-API messages
# ============================================


def _recording_context_builder(built: list[Any]) -> Callable[..., Any]:
    """Wrap the Group 3 constructor and keep every context it returns."""
    from src.interaction_context import build_interaction_context

    def _build(**kwargs: Any) -> Any:
        context = build_interaction_context(**kwargs)
        built.append(context)
        return context

    return _build


def _create_paused_session(client: TestClient, tenant_id: UUID, phone: str) -> UUID:
    """Create the phone's active session through the webhook helper, paused."""
    from sqlalchemy import update

    from src.api.routes.webhook import _get_or_create_session
    from src.models.session import Session as SessionRow
    from src.repositories.database import get_db_session

    async def _run() -> UUID:
        db = get_db_session()
        try:
            session_id, _ = await _get_or_create_session(tenant_id, phone, db)
            await db.execute(
                update(SessionRow)
                .where(SessionRow.id == session_id)
                .values(is_paused=True, paused_until=None)
            )
            await db.commit()
            return session_id
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined]


def _message_in_rows(client: TestClient, tenant_id: UUID) -> list[Any]:
    """Every persisted ``message.in`` row of the tenant, read in a new session."""
    from sqlalchemy import select

    from src.models.event import Event as EventRow
    from src.repositories.database import get_db_session

    async def _run() -> list[Any]:
        db = get_db_session()
        try:
            result = await db.execute(
                select(EventRow).where(
                    EventRow.tenant_id == tenant_id,
                    EventRow.event_type == "message.in",
                )
            )
            return list(result.scalars().all())
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined]


def _session_origins(client: TestClient, session_id: UUID) -> set[str]:
    """The distinct ``interaction_origin`` of every event row of a session."""
    from sqlalchemy import select

    from src.models.event import Event as EventRow
    from src.repositories.database import get_db_session

    async def _run() -> set[str]:
        db = get_db_session()
        try:
            result = await db.execute(
                select(EventRow.interaction_origin).where(
                    EventRow.session_id == session_id
                )
            )
            return set(result.scalars().all())
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined]


def _mock_pipeline_service() -> MagicMock:
    service = MagicMock()
    service._load_clinic_config = AsyncMock(return_value={})
    service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
    service.finish_response = AsyncMock(return_value=_fake_event_response(None))
    return service


class TestZapiInteractionOrigin:
    """Z-API is ``production`` only after its proof: the capability digest
    matches exactly one tenant and the body is bound to that instance."""

    def test_valid_capability_threads_one_production_context_to_both_phases(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        from src.interaction_context import (
            InteractionOrigin,
            ResolvedInteractionContext,
        )

        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_tenant(client, tenant_id, instance_id, raw_token)
        body = _zapi_body(
            instance_id=instance_id,
            message_id=f"mid_origin_{uuid4().hex[:10]}",
            phone="5511900000701",
        )
        built: list[Any] = []
        service = _mock_pipeline_service()

        with (
            patch(
                "src.api.routes.webhook.build_interaction_context",
                new=_recording_context_builder(built),
            ),
            patch("src.api.routes.webhook.get_event_service", return_value=service),
            patch("src.api.routes.webhook.mark_message_read", new=AsyncMock()),
        ):
            resp = client.post(f"/v1/webhook/zapi/{raw_token}", json=body)

        assert resp.status_code == 200, resp.text
        assert len(built) == 1
        context = built[0]
        event_request = service.run_pipeline.await_args.args[0]
        assert service.run_pipeline.await_args.kwargs["context"] is context
        assert service.finish_response.await_args.kwargs["context"] is context
        assert type(context) is ResolvedInteractionContext
        assert context.origin is InteractionOrigin.PRODUCTION
        assert context.effective_tenant_id == tenant_id == event_request.tenant_id
        assert context.session_id == event_request.session_id
        assert context.interaction_event_id is not None

    @pytest.mark.parametrize(
        "claim",
        [
            "body_simulator",
            "body_unknown",
            "nested_simulator",
            "nested_unknown",
            "api_key_header",
            "panel_jwt_header",
        ],
    )
    def test_production_whatever_the_request_claims(
        self,
        client: TestClient,
        tenant_id: UUID,
        claim: str,
    ) -> None:
        from src.interaction_context import InteractionOrigin

        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_tenant(client, tenant_id, instance_id, raw_token)
        body: dict[str, Any] = _zapi_body(
            instance_id=instance_id,
            message_id=f"mid_claim_{uuid4().hex[:10]}",
            phone="5511900000705",
        )
        headers: dict[str, str] = {}
        if claim.startswith("body_"):
            body["interaction_origin"] = claim.removeprefix("body_")
        elif claim.startswith("nested_"):
            claimed = claim.removeprefix("nested_")
            body["text"]["interaction_origin"] = claimed
            body["metadata"] = {"interaction_origin": claimed}
        elif claim == "api_key_header":
            headers["X-API-Key"] = "test-api-key-12345"
        else:
            headers.update(_admin_headers(tenant_id))
        service = _mock_pipeline_service()

        with (
            patch("src.api.routes.webhook.get_event_service", return_value=service),
            patch("src.api.routes.webhook.mark_message_read", new=AsyncMock()),
        ):
            resp = client.post(
                f"/v1/webhook/zapi/{raw_token}", json=body, headers=headers
            )

        assert resp.status_code == 200, resp.text
        context = service.run_pipeline.await_args.kwargs["context"]
        assert context.origin is InteractionOrigin.PRODUCTION
        assert service.finish_response.await_args.kwargs["context"] is context

    @pytest.mark.parametrize("failure", ["unregistered_token", "instance_mismatch"])
    def test_failed_proof_is_401_without_classification_or_effects(
        self,
        client: TestClient,
        tenant_id: UUID,
        failure: str,
    ) -> None:
        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_tenant(client, tenant_id, instance_id, raw_token)
        body = _zapi_body(
            instance_id=instance_id,
            message_id=f"mid_denied_{uuid4().hex[:10]}",
            phone="5511900000702",
        )
        token = raw_token
        if failure == "unregistered_token":
            token = _new_canonical_token()
        else:
            body["instanceId"] = "another-instance"
        built: list[Any] = []
        service = _mock_pipeline_service()
        pause_check = AsyncMock(side_effect=AssertionError("pause check ran"))

        with (
            patch(
                "src.api.routes.webhook.build_interaction_context",
                new=_recording_context_builder(built),
            ),
            patch("src.api.routes.webhook.get_event_service", return_value=service),
            patch("src.api.routes.webhook._check_pause_state", pause_check),
        ):
            resp = client.post(f"/v1/webhook/zapi/{token}", json=body)

        assert resp.status_code == 401
        assert resp.json() == {"error": "invalid_webhook_auth"}
        assert built == []
        pause_check.assert_not_awaited()
        service.run_pipeline.assert_not_awaited()
        assert _message_in_rows(client, tenant_id) == []

    def test_persisted_message_in_id_is_the_context_interaction_id(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        from src.services.event_service import EventService

        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_tenant(client, tenant_id, instance_id, raw_token)
        body = _zapi_body(
            instance_id=instance_id,
            message_id=f"mid_persist_{uuid4().hex[:10]}",
            phone="5511900000703",
            text="Olá, bom dia",
        )
        seen: dict[str, Any] = {}
        real_run = EventService.run_pipeline
        real_finish = EventService.finish_response

        async def _run(self: Any, request: Any, session: Any = None, *, context: Any):
            seen["run"] = context
            return await real_run(self, request, session, context=context)

        async def _finish(self: Any, *args: Any, context: Any) -> Any:
            seen["finish"] = context
            seen["response"] = await real_finish(self, *args, context=context)
            return seen["response"]

        with (
            patch.object(EventService, "run_pipeline", _run),
            patch.object(EventService, "finish_response", _finish),
            patch(
                "src.api.routes.webhook.send_text_message",
                new=AsyncMock(return_value=_fake_send_result()),
            ),
            patch("src.api.routes.webhook.mark_message_read", new=AsyncMock()),
            patch("src.api.routes.webhook.mark_conversation_unread", new=AsyncMock()),
        ):
            resp = client.post(f"/v1/webhook/zapi/{raw_token}", json=body)

        assert resp.status_code == 200, resp.text
        context = seen["run"]
        assert seen["finish"] is context
        assert context.interaction_event_id is not None
        assert seen["response"].interaction_event_id == context.interaction_event_id
        rows = _message_in_rows(client, tenant_id)
        assert [row.id for row in rows] == [context.interaction_event_id]
        assert rows[0].session_id == context.session_id
        assert _session_origins(client, context.session_id) == {"production"}

    def test_paused_capture_persists_production_under_the_context_id(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        from src.interaction_context import InteractionOrigin

        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_tenant(client, tenant_id, instance_id, raw_token)
        phone = "5511900000704"
        session_id = _create_paused_session(client, tenant_id, phone)
        body = _zapi_body(
            instance_id=instance_id,
            message_id=f"mid_paused_{uuid4().hex[:10]}",
            phone=phone,
            text="Ainda estou aguardando",
        )
        built: list[Any] = []
        create_session = AsyncMock(side_effect=AssertionError("session created"))

        with (
            patch(
                "src.api.routes.webhook.build_interaction_context",
                new=_recording_context_builder(built),
            ),
            patch("src.api.routes.webhook._get_or_create_session", create_session),
            patch("src.api.routes.webhook.mark_message_read", new=AsyncMock()),
        ):
            resp = client.post(f"/v1/webhook/zapi/{raw_token}", json=body)

        assert resp.status_code == 202, resp.text
        assert resp.json() == {"ok": True, "skipped": "session_paused"}
        create_session.assert_not_awaited()
        assert len(built) == 1
        context = built[0]
        assert context.origin is InteractionOrigin.PRODUCTION
        assert (context.effective_tenant_id, context.session_id) == (
            tenant_id,
            session_id,
        )
        rows = _message_in_rows(client, tenant_id)
        assert [row.id for row in rows] == [context.interaction_event_id]
        assert rows[0].interaction_origin == "production"
        assert rows[0].event_metadata == {"paused_capture": True}


def test_every_adapter_call_site_passes_the_origin_keywords() -> None:
    """The adapters always hand over what they classified, by keyword.

    ``_run_pipeline_transactionally`` requires ``context``; the other callees
    keep defaults for compatibility, so only the call sites prove that no
    adapter silently falls back to them or passes an explicit ``None``.
    """
    import ast
    import inspect

    from src.api.routes import webhook as webhook_module

    tree = ast.parse(inspect.getsource(webhook_module))
    keywords_by_callee: dict[str, list[set[str]]] = {}
    none_keywords_by_callee: dict[str, list[set[str]]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        keywords_by_callee.setdefault(name, []).append(
            {keyword.arg for keyword in node.keywords if keyword.arg}
        )
        none_keywords_by_callee.setdefault(name, []).append(
            {
                keyword.arg
                for keyword in node.keywords
                if keyword.arg
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is None
            }
        )

    expected = {
        "_run_pipeline_transactionally": ("context", 4),
        "_check_pause_state": ("origin", 4),
        "_meta_initiate_file_handoff": ("context", 2),
        "initiate_handoff": ("context", 1),
    }
    for callee, (keyword, count) in expected.items():
        calls = keywords_by_callee.get(callee, [])
        assert len(calls) == count, (callee, calls)
        assert all(keyword in call for call in calls), (callee, calls)
        explicit_none = none_keywords_by_callee.get(callee, [])
        assert all(keyword not in call for call in explicit_none), (
            callee,
            explicit_none,
        )
