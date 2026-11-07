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
- whatsapp-image-understanding Group 3: a patient image is described and
  answered; a document, video or non-described image gets the owner's file
  acknowledgement and a pending review that the next textual message
  resolves; staff (``fromMe``) media and stickers keep today's exits.
"""

import base64
import copy
import hashlib
import json
import logging
import secrets
import socket
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import httpx
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


# ============================================
# whatsapp-image-understanding Group 3 — patient files on Z-API
# ============================================
# Decision 3: a document, video or non-described image gets the owner's ack
#             and an active pending review; the next textual message (text or
#             transcribed audio, never a caption) resolves it.
# Decision 4: an active review suppresses a new ack; expired or legacy (no
#             timestamp) counts as absent.
# Decision 8: only patient media passes the guards; staff media keeps today's
#             exit and never reaches ``_handle_clinic_message``.
# Decision 9: no claim of its own; the top-level replay claim covers files.
# Decisions 10, 12, 13: caption, tolerant URL, declared MIME, view-once.
# Decision 17: send, track and store the id, set the review, commit.

_ZAPI_MEDIA_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "zapi" / "media-payloads.json"
)
_G3_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF-zapi-g3-fake-jpeg"
_G3_DESCRIPTION = "Foto de uma receita com o texto Dipirona 500 mg."
_G3_LOGGER = "src.api.routes.webhook"
_G3_REPLY = "Resposta da IA"
_G3_DEFAULT_CONFIG = {"identity": {"clinic_name": "Clínica Teste"}}
_G3_EN_CONFIG = {"business_rules": {"agent_language": "en"}}
_G3_LGPD_CONFIG = {
    "lgpd": {"lgpd_enabled": True},
    "identity": {"clinic_name": "Clínica Teste"},
}


def _g3_mid(label: str) -> str:
    return f"3EB0_zapi_{label}_{uuid4().hex[:10]}"


def _g3_marker(description: str = _G3_DESCRIPTION) -> str:
    return f"[Imagem enviada pelo paciente: {description}]"


def _g3_ack(language: str = "pt") -> str:
    from src.api.routes import webhook as webhook_module

    return (
        webhook_module._FILE_ACK_MSG_EN
        if language == "en"
        else webhook_module._FILE_ACK_MSG_PT
    )


def _g3_forwarded(language: str = "pt") -> str:
    from src.api.routes import webhook as webhook_module

    return (
        webhook_module._FILE_FORWARDED_MSG_EN
        if language == "en"
        else webhook_module._FILE_FORWARDED_MSG_PT
    )


def _zapi_media_body(
    kind: str,
    *,
    instance_id: str,
    message_id: str,
    phone: str,
    from_me: bool = False,
    **media: Any,
) -> dict:
    """A variant of the documented Z-API payload of ``kind``.

    Starts from ``tests/fixtures/zapi/media-payloads.json`` (field names from
    the Z-API "Ao receber" examples, not verified on a real payload) and
    changes only the routing fields and the media fields named in ``media``;
    a ``None`` removes the field.
    """
    fixture = json.loads(_ZAPI_MEDIA_FIXTURE.read_text(encoding="utf-8"))
    body = copy.deepcopy(fixture[kind])
    body.update(
        instanceId=instance_id, messageId=message_id, phone=phone, fromMe=from_me
    )
    media_obj = body[kind]
    for key, value in media.items():
        if value is None:
            media_obj.pop(key, None)
        else:
            media_obj[key] = value
    return body


def _g3_review(age: timedelta | None) -> dict[str, Any]:
    """A pending review set ``age`` ago; ``None`` is a legacy flag."""
    if age is None:
        return {"pending_file_review": True}
    set_at = datetime.now(timezone.utc) - age
    return {"pending_file_review": True, "pending_file_review_at": set_at.isoformat()}


def _g3_image_events(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    fields = ("channel", "outcome", "mime", "size_bytes", "latency_ms")
    return [
        {name: getattr(record, name) for name in fields}
        for record in caplog.records
        if getattr(record, "event", None) == "image_description_attempt"
    ]


def _g3_session_row(client: TestClient, tenant_id: UUID, phone: str) -> Any:
    """The phone's session (``id``, ``is_paused``, ``metadata``), or ``None``."""
    from sqlalchemy import select

    from src.models.session import Session as SessionRow
    from src.repositories.database import get_db_session

    async def _run() -> Any:
        db = get_db_session()
        try:
            result = await db.execute(
                select(SessionRow).where(
                    SessionRow.tenant_id == tenant_id,
                    SessionRow.user_id == phone,
                )
            )
            row = result.scalars().first()
            if row is None:
                return None
            return SimpleNamespace(
                id=row.id,
                is_paused=row.is_paused,
                metadata=dict(row.session_metadata or {}),
            )
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined]


def _g3_seed_session(
    client: TestClient,
    tenant_id: UUID,
    phone: str,
    metadata: dict[str, Any] | None = None,
) -> UUID:
    """Create the phone's session through the webhook helper, merging
    ``metadata`` into its ``session_metadata``."""
    from sqlalchemy import select

    from src.api.routes.webhook import _get_or_create_session
    from src.models.session import Session as SessionRow
    from src.repositories.database import get_db_session

    async def _run() -> UUID:
        db = get_db_session()
        try:
            session_id, _ = await _get_or_create_session(tenant_id, phone, db)
            if metadata is not None:
                row = (
                    await db.execute(
                        select(SessionRow).where(SessionRow.id == session_id)
                    )
                ).scalar_one()
                row.session_metadata = {**(row.session_metadata or {}), **metadata}
            await db.commit()
            return session_id
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined]


def _g3_session_count(client: TestClient, tenant_id: UUID) -> int:
    """How many session rows the tenant has, whatever the phone."""
    from sqlalchemy import func, select

    from src.models.session import Session as SessionRow
    from src.repositories.database import get_db_session

    async def _run() -> int:
        db = get_db_session()
        try:
            result = await db.execute(
                select(func.count())
                .select_from(SessionRow)
                .where(SessionRow.tenant_id == tenant_id)
            )
            return int(result.scalar_one())
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined]


def _g3_drop_credential(client: TestClient, tenant_id: UUID, key: str) -> None:
    """Remove one key from the tenant's raw ``api_credentials`` row."""
    from sqlalchemy import select

    from src.models.clinic_config_table import ClinicConfigTable
    from src.repositories.database import get_db_session

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
            credentials = dict(row.api_credentials or {})
            assert credentials.pop(key, None) is not None
            row.api_credentials = credentials
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]


@dataclass
class _ZapiFileWorld:
    """One Z-API tenant (real credential row) plus the patched boundaries.

    ``calls`` records ``(kind, detail)`` in order across the Z-API sends, the
    image description and the pipeline, so tests can assert sequencing (the
    LGPD notice before the description, for instance).
    """

    client: TestClient
    tenant_id: UUID
    instance_id: str
    raw_token: str
    phone: str
    send: AsyncMock
    download: AsyncMock
    describe: AsyncMock
    handoff: AsyncMock
    service: MagicMock
    calls: list[tuple[str, str]]
    send_results: list[dict[str, Any]]
    openrouter_requests: list[httpx.Request] = field(default_factory=list)

    def post(self, body: dict) -> Any:
        return self.client.post(f"/v1/webhook/zapi/{self.raw_token}", json=body)

    def media(
        self,
        kind: str,
        *,
        message_id: str | None = None,
        from_me: bool = False,
        **media: Any,
    ) -> dict:
        return _zapi_media_body(
            kind,
            instance_id=self.instance_id,
            message_id=message_id or _g3_mid(kind),
            phone=self.phone,
            from_me=from_me,
            **media,
        )

    def image(self, **kwargs: Any) -> dict:
        return self.media("image", **kwargs)

    def document(self, **kwargs: Any) -> dict:
        return self.media("document", **kwargs)

    def video(self, **kwargs: Any) -> dict:
        return self.media("video", **kwargs)

    def text(
        self, body: str, *, message_id: str | None = None, from_me: bool = False
    ) -> dict:
        return _zapi_body(
            instance_id=self.instance_id,
            message_id=message_id or _g3_mid("txt"),
            phone=self.phone,
            text=body,
            from_me=from_me,
        )

    def audio(self) -> dict:
        return _zapi_audio_body(
            instance_id=self.instance_id,
            message_id=_g3_mid("audio"),
            phone=self.phone,
        )

    def sent(self) -> list[str]:
        return [detail for kind, detail in self.calls if kind == "send"]

    def pipeline_contents(self) -> list[str]:
        return [detail for kind, detail in self.calls if kind == "pipeline"]

    def session(self) -> Any:
        row = _g3_session_row(self.client, self.tenant_id, self.phone)
        assert row is not None
        return row

    def metadata(self) -> dict[str, Any]:
        return dict(self.session().metadata)

    def seed_session(self, metadata: dict[str, Any] | None = None) -> UUID:
        return _g3_seed_session(self.client, self.tenant_id, self.phone, metadata)


def _g3_patch_openrouter_http(
    stack: ExitStack, description: str, world: _ZapiFileWorld
) -> None:
    """Run the real downloader and image adapter on a recording transport.

    A GET is the Z-API CDN, answering ``application/octet-stream`` (the
    declared ``image.mimeType`` must cover it); a POST is OpenRouter.
    """
    from src.services import safe_egress

    def _resolver(_host: str, port: int) -> list[tuple[Any, ...]]:
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("104.18.2.1", port),
            )
        ]

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            world.calls.append(("download", str(request.url)))
            return httpx.Response(
                200,
                content=_G3_JPEG,
                headers={"Content-Type": "application/octet-stream"},
            )
        world.openrouter_requests.append(request)
        world.calls.append(("describe", str(request.url)))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": description}}]
            },
        )

    mock_transport = httpx.MockTransport(_handler)
    original_build = safe_egress.build_safe_async_client

    def _build(validated: Any, *, timeout: Any = None, transport: Any = None) -> Any:
        return original_build(validated, timeout=timeout, transport=mock_transport)

    stack.enter_context(patch.object(safe_egress, "_default_getaddrinfo", _resolver))
    stack.enter_context(patch.object(safe_egress, "build_safe_async_client", _build))


@pytest.fixture
def zapi_file_world(
    client: TestClient,
    tenant_id: UUID,
    caplog: pytest.LogCaptureFixture,
) -> Iterator[Callable[..., _ZapiFileWorld]]:
    """Factory for :class:`_ZapiFileWorld` (one call per test).

    Seeds a real Z-API credential row (with the OpenRouter bundle unless
    ``multimodal=False``) and patches the Z-API send, ``safe_download_image``,
    the multimodal adapter (or, with ``openrouter_http``, only DNS and the
    transport under the real downloader and adapter), the file handoff's
    creation, the event service and Redis (absent: in-process stores only).
    """
    from src.services.safe_egress import SafeDownload

    caplog.set_level(logging.INFO, logger=_G3_LOGGER)
    with ExitStack() as stack:

        def make(
            *,
            multimodal: bool = True,
            stt: bool = False,
            clinic_config: dict[str, Any] | None = None,
            suggested: str | None = _G3_REPLY,
            openrouter_http: str | None = None,
            phone: str = "5511977003300",
        ) -> _ZapiFileWorld:
            instance_id = f"3E{uuid4().hex[:10]}"
            raw_token = _new_canonical_token()
            _seed_zapi_audio_tenant(
                client,
                tenant_id,
                instance_id,
                raw_token,
                stt=stt,
                multimodal=multimodal,
            )
            calls: list[tuple[str, str]] = []
            send_results: list[dict[str, Any]] = []

            async def _send(
                _inst: str, _tok: str, _phone: str, text: str, client_token: str = ""
            ) -> dict:
                calls.append(("send", text))
                result = _fake_send_result(_g3_mid("sent"))
                send_results.append(result)
                return result

            async def _download(url: str, **_kwargs: Any) -> Any:
                calls.append(("download", url))
                return SafeDownload(content=_G3_JPEG, mime_type="image/jpeg")

            async def _describe(**kwargs: Any) -> str:
                calls.append(("describe", kwargs["mimetype"]))
                return _G3_DESCRIPTION

            async def _run_pipeline(event_request: Any, *_a: Any, **_k: Any) -> Any:
                calls.append(("pipeline", event_request.payload.content))
                return MagicMock(), {}, []

            service = MagicMock()
            service._load_clinic_config = AsyncMock(
                return_value=clinic_config or _G3_DEFAULT_CONFIG
            )
            service.run_pipeline = AsyncMock(side_effect=_run_pipeline)
            service.finish_response = AsyncMock(
                return_value=_fake_event_response(suggested)
            )
            world = _ZapiFileWorld(
                client=client,
                tenant_id=tenant_id,
                instance_id=instance_id,
                raw_token=raw_token,
                phone=phone,
                send=AsyncMock(side_effect=_send),
                download=AsyncMock(side_effect=_download),
                describe=AsyncMock(side_effect=_describe),
                handoff=AsyncMock(return_value=SimpleNamespace(id=uuid4())),
                service=service,
                calls=calls,
                send_results=send_results,
            )
            webhook = "src.api.routes.webhook"
            stack.enter_context(patch(f"{webhook}._get_redis", return_value=None))
            stack.enter_context(patch(f"{webhook}.send_text_message", world.send))
            stack.enter_context(patch(f"{webhook}.mark_message_read", AsyncMock()))
            stack.enter_context(
                patch(f"{webhook}.mark_conversation_unread", AsyncMock())
            )
            stack.enter_context(
                patch(f"{webhook}._meta_initiate_file_handoff", world.handoff)
            )
            stack.enter_context(
                patch(f"{webhook}.get_event_service", return_value=service)
            )
            if openrouter_http is None:
                stack.enter_context(
                    patch(f"{webhook}.safe_download_image", world.download)
                )
                stack.enter_context(
                    patch(
                        "src.services.multimodal_service.describe_image_multimodal",
                        world.describe,
                    )
                )
            else:
                _g3_patch_openrouter_http(stack, openrouter_http, world)
            return world

        yield make


def test_the_media_fixture_carries_the_documented_field_names() -> None:
    """Deliverable 1: the names come from the Z-API docs, marked unverified."""
    fixture = json.loads(_ZAPI_MEDIA_FIXTURE.read_text(encoding="utf-8"))

    assert fixture["_meta"]["source"].startswith("https://developer.z-api.io/")
    assert fixture["_meta"]["verified"] == "não verificado em payload real"
    assert {"imageUrl", "caption", "mimeType", "viewOnce"} <= set(
        fixture["image"]["image"]
    )
    assert {"documentUrl", "mimeType", "fileName"} <= set(
        fixture["document"]["document"]
    )
    assert {"videoUrl", "caption", "mimeType"} <= set(fixture["video"]["video"])
    for kind in ("image", "document", "video", "sticker"):
        assert fixture[kind]["phone"] == "TEST_PHONE"  # no real phone numbers
        assert fixture[kind]["fromMe"] is False


class TestZapiImageDescribed:
    """Decisions 1, 10, 12: a patient image is described once and answered."""

    def test_captioned_image_reaches_openrouter_once_with_an_image_url_block(
        self,
        zapi_file_world: Callable[..., _ZapiFileWorld],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """The real downloader and adapter: the CDN answers octet-stream, the
        declared ``mimeType`` covers it, and OpenRouter gets one data URL."""
        world = zapi_file_world(openrouter_http=_G3_DESCRIPTION)
        cdn_url = "https://example.com/zapi/patient-image"

        resp = world.post(world.image(caption="Olha a minha receita", imageUrl=cdn_url))

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True}
        assert ("download", cdn_url) in world.calls
        [request] = world.openrouter_requests
        assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
        assert request.headers["Host"] == "openrouter.ai"
        assert json.loads(request.content)["model"] == "google/gemini-2.5-flash"
        [message] = json.loads(request.content)["messages"]
        blocks = [b for b in message["content"] if b["type"] == "image_url"]
        encoded = base64.b64encode(_G3_JPEG).decode("ascii")
        assert [b["image_url"]["url"] for b in blocks] == [
            f"data:image/jpeg;base64,{encoded}"
        ]
        assert world.pipeline_contents() == [f"Olha a minha receita\n\n{_g3_marker()}"]
        assert world.sent() == [_G3_REPLY]
        [event] = _g3_image_events(caplog)
        assert event["channel"] == "zapi"
        assert event["outcome"] == "described"
        assert event["mime"] == "image/jpeg"
        assert event["size_bytes"] == len(_G3_JPEG)
        assert isinstance(event["latency_ms"], int)
        for record in caplog.records:
            assert _G3_DESCRIPTION not in record.getMessage()
            assert encoded not in record.getMessage()
            assert cdn_url not in record.getMessage()

    def test_image_without_caption_sends_only_the_marker(
        self, zapi_file_world: Callable[..., _ZapiFileWorld]
    ) -> None:
        from src.api.routes.webhook import IMAGE_MAX_BYTES

        world = zapi_file_world()
        payload = world.image()
        assert payload["image"]["caption"] == ""  # the fixture's default

        resp = world.post(payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True}
        assert world.pipeline_contents() == [_g3_marker()]
        world.download.assert_awaited_once_with(
            payload["image"]["imageUrl"],
            max_bytes=IMAGE_MAX_BYTES,
            declared_mime="image/jpeg",
        )
        world.describe.assert_awaited_once()
        kwargs = world.describe.await_args.kwargs
        assert kwargs["image_bytes"] == _G3_JPEG
        assert kwargs["mimetype"] == "image/jpeg"
        assert kwargs["api_key"].startswith("sk-fake-mm-")
        assert kwargs["model"] == "google/gemini-2.5-flash"
        assert kwargs["base_url"] == "https://openrouter.ai/api/v1"
        assert kwargs["agent_language"] == "pt-BR"
        assert world.sent() == [_G3_REPLY]

    def test_image_url_is_read_tolerantly(
        self, zapi_file_world: Callable[..., _ZapiFileWorld]
    ) -> None:
        world = zapi_file_world()

        resp = world.post(
            world.image(imageUrl=None, url="https://example.com/zapi/by-url.jpeg")
        )

        assert resp.json() == {"ok": True, "replied": True}
        assert world.download.await_args.args == (
            "https://example.com/zapi/by-url.jpeg",
        )
        assert world.pipeline_contents() == [_g3_marker()]

    def test_the_downloaded_mime_is_the_one_described(
        self, zapi_file_world: Callable[..., _ZapiFileWorld]
    ) -> None:
        from src.services.safe_egress import SafeDownload

        world = zapi_file_world()
        world.download.side_effect = None
        world.download.return_value = SafeDownload(
            content=_G3_JPEG, mime_type="image/png"
        )

        resp = world.post(world.image(mimeType="image/jpeg"))

        assert resp.json() == {"ok": True, "replied": True}
        assert world.download.await_args.kwargs["declared_mime"] == "image/jpeg"
        assert world.describe.await_args.kwargs["mimetype"] == "image/png"

    def test_english_agent_gets_the_english_marker(
        self, zapi_file_world: Callable[..., _ZapiFileWorld]
    ) -> None:
        world = zapi_file_world(clinic_config=_G3_EN_CONFIG)

        world.post(world.image(caption="My prescription"))

        assert world.pipeline_contents() == [
            f"My prescription\n\n[Image sent by the patient: {_G3_DESCRIPTION}]"
        ]
        assert world.describe.await_args.kwargs["agent_language"] == "en"


class TestZapiImageNotDescribed:
    """Decisions 3, 12, 13: every non-described image gets the file ack, and
    every attempt emits exactly one ``image_description_attempt``."""

    @pytest.mark.parametrize(
        ("media", "multimodal", "outcome"),
        [
            pytest.param({}, False, "no_capability", id="no_capability"),
            pytest.param({"viewOnce": True}, True, "view_once", id="view_once"),
            pytest.param({"imageUrl": None}, True, "download_failed", id="no_url"),
            pytest.param({"imageUrl": ""}, True, "download_failed", id="empty_url"),
            pytest.param(
                {"mimeType": "image/gif"},
                True,
                "unsupported_mime",
                id="declared_gif",
            ),
        ],
    )
    def test_refused_before_any_download(
        self,
        zapi_file_world: Callable[..., _ZapiFileWorld],
        caplog: pytest.LogCaptureFixture,
        media: dict[str, Any],
        multimodal: bool,
        outcome: str,
    ) -> None:
        world = zapi_file_world(multimodal=multimodal)

        resp = world.post(world.image(caption="Minha foto", **media))

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True, "action": "file_ack"}
        world.download.assert_not_awaited()
        world.describe.assert_not_awaited()
        [event] = _g3_image_events(caplog)
        assert event["channel"] == "zapi"
        assert event["outcome"] == outcome
        assert event["size_bytes"] is None  # Z-API declares no size
        assert event["latency_ms"] is None
        assert world.sent() == [_g3_ack()]
        assert world.pipeline_contents() == []
        assert world.metadata()["pending_file_review"] is True

    def test_an_image_object_without_anything_is_acknowledged_not_silenced(
        self,
        zapi_file_world: Callable[..., _ZapiFileWorld],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        world = zapi_file_world()
        payload = world.image()
        payload["image"] = {}

        resp = world.post(payload)

        assert resp.json() == {"ok": True, "replied": True, "action": "file_ack"}
        world.download.assert_not_awaited()
        assert [e["outcome"] for e in _g3_image_events(caplog)] == ["download_failed"]
        assert world.sent() == [_g3_ack()]

    @pytest.mark.parametrize(
        ("download", "describe", "outcome", "described"),
        [
            pytest.param(
                "image_download_failed", None, "download_failed", False, id="http"
            ),
            pytest.param(
                "redirect_forbidden", None, "download_failed", False, id="redirect"
            ),
            pytest.param(
                "unsafe_api_url", None, "download_failed", False, id="unsafe_url"
            ),
            pytest.param(
                RuntimeError("socket closed"),
                None,
                "download_failed",
                False,
                id="download_crash",
            ),
            pytest.param(
                "image_too_large", None, "too_large", False, id="streamed_too_large"
            ),
            pytest.param(
                "unsupported_image_mime",
                None,
                "unsupported_mime",
                False,
                id="served_bad_mime",
            ),
            pytest.param(
                b"\xff" * 5_000_001, None, "too_large", False, id="chain_too_large"
            ),
            pytest.param(
                None,
                "provider_http_error",
                "provider_failed",
                True,
                id="provider_http_error",
            ),
            pytest.param(
                None,
                RuntimeError("socket closed"),
                "provider_failed",
                True,
                id="provider_crash",
            ),
            pytest.param(None, "no_content", "no_content", True, id="no_content"),
        ],
    )
    def test_refused_after_the_download(
        self,
        zapi_file_world: Callable[..., _ZapiFileWorld],
        caplog: pytest.LogCaptureFixture,
        download: Any,
        describe: Any,
        outcome: str,
        described: bool,
    ) -> None:
        from src.services.multimodal_service import (
            IMAGE_NO_CONTENT_MARKER,
            MultimodalImageError,
        )
        from src.services.safe_egress import SafeDownload, UnsafeEgressError

        world = zapi_file_world()
        if isinstance(download, str):
            world.download.side_effect = UnsafeEgressError(download)
        elif isinstance(download, Exception):
            world.download.side_effect = download
        elif isinstance(download, bytes):
            world.download.side_effect = None
            world.download.return_value = SafeDownload(
                content=download, mime_type="image/jpeg"
            )
        if describe == "no_content":
            world.describe.side_effect = None
            world.describe.return_value = IMAGE_NO_CONTENT_MARKER
        elif isinstance(describe, str):
            world.describe.side_effect = MultimodalImageError(describe)
        elif isinstance(describe, Exception):
            world.describe.side_effect = describe

        resp = world.post(world.image(caption="Minha foto"))

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True, "action": "file_ack"}
        world.download.assert_awaited_once()
        assert world.describe.await_count == (1 if described else 0)
        assert [e["outcome"] for e in _g3_image_events(caplog)] == [outcome]
        assert world.sent() == [_g3_ack()]
        assert world.pipeline_contents() == []
        assert world.metadata()["pending_file_review"] is True


class TestZapiFileGatesAndDedupe:
    """Decisions 8, 9: AI-off and pause gates, LGPD order, the top claim."""

    @pytest.mark.parametrize("kind", ["image", "document"])
    def test_ai_off_does_nothing(
        self,
        zapi_file_world: Callable[..., _ZapiFileWorld],
        caplog: pytest.LogCaptureFixture,
        kind: str,
    ) -> None:
        world = zapi_file_world()
        payload = world.image(caption="Foto") if kind == "image" else world.document()

        with patch(
            "src.api.routes.webhook._check_bot_active",
            new=AsyncMock(return_value={"ok": True, "skipped": "bot_paused"}),
        ):
            resp = world.post(payload)

        assert resp.status_code == 202
        assert resp.json() == {"ok": True, "skipped": "bot_paused"}
        world.download.assert_not_awaited()
        world.describe.assert_not_awaited()
        world.send.assert_not_awaited()
        assert world.pipeline_contents() == []
        assert _g3_image_events(caplog) == []

    @pytest.mark.parametrize("kind", ["image", "document"])
    def test_paused_session_does_nothing(
        self,
        zapi_file_world: Callable[..., _ZapiFileWorld],
        caplog: pytest.LogCaptureFixture,
        kind: str,
    ) -> None:
        world = zapi_file_world()
        session_id = _create_paused_session(world.client, world.tenant_id, world.phone)
        payload = world.image(caption="Foto") if kind == "image" else world.document()

        resp = world.post(payload)

        assert resp.status_code == 202
        assert resp.json() == {"ok": True, "skipped": "session_paused"}
        world.download.assert_not_awaited()
        world.describe.assert_not_awaited()
        world.send.assert_not_awaited()
        assert world.pipeline_contents() == []
        assert _g3_image_events(caplog) == []
        session = world.session()
        assert session.id == session_id
        assert "pending_file_review" not in session.metadata

    @pytest.mark.parametrize(
        ("kind", "after_notice"),
        [
            pytest.param("image", ["describe", "pipeline", "send"], id="image"),
            pytest.param("document", ["send"], id="document"),
        ],
    )
    def test_new_session_sends_the_lgpd_notice_first_and_once(
        self,
        zapi_file_world: Callable[..., _ZapiFileWorld],
        kind: str,
        after_notice: list[str],
    ) -> None:
        world = zapi_file_world(clinic_config=_G3_LGPD_CONFIG)
        payload = (
            world.image(caption="Primeira foto")
            if kind == "image"
            else (world.document())
        )

        resp = world.post(payload)

        assert resp.status_code == 200, resp.text
        kinds = [k for k, _ in world.calls if k != "download"]
        assert kinds == ["send", *after_notice]
        notice = world.calls[0][1]
        assert "LGPD" in notice or "Política de Privacidade" in notice
        assert world.sent()[1] == (_G3_REPLY if kind == "image" else _g3_ack())

    @pytest.mark.parametrize("kind", ["image", "document"])
    def test_redelivery_is_a_duplicate_and_never_describes_twice(
        self,
        zapi_file_world: Callable[..., _ZapiFileWorld],
        caplog: pytest.LogCaptureFixture,
        kind: str,
    ) -> None:
        world = zapi_file_world()
        payload = world.image(caption="Foto") if kind == "image" else world.document()

        first = world.post(payload)
        second = world.post(payload)

        assert first.status_code == 200, first.text
        assert second.json() == {"ok": True, "skipped": "duplicate_message"}
        if kind == "image":
            assert first.json() == {"ok": True, "replied": True}
            world.download.assert_awaited_once()  # the top claim stopped it
            world.describe.assert_awaited_once()
            assert world.sent() == [_G3_REPLY]
            assert [e["outcome"] for e in _g3_image_events(caplog)] == ["described"]
        else:
            assert first.json() == {"ok": True, "replied": True, "action": "file_ack"}
            assert world.sent() == [_g3_ack()]


class TestZapiFileAck:
    """Decisions 3, 4, 6, 17, 18: the owner's acknowledgement for files."""

    @pytest.mark.parametrize("kind", ["document", "video"])
    def test_document_and_video_get_the_ack(
        self,
        zapi_file_world: Callable[..., _ZapiFileWorld],
        caplog: pytest.LogCaptureFixture,
        kind: str,
    ) -> None:
        world = zapi_file_world()

        resp = world.post(world.media(kind))

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True, "action": "file_ack"}
        assert world.sent() == [_g3_ack()]
        world.download.assert_not_awaited()
        world.describe.assert_not_awaited()
        assert world.pipeline_contents() == []
        assert _g3_image_events(caplog) == []
        assert f"Z-API file ack sent to {world.phone} (type={kind})" in [
            record.getMessage() for record in caplog.records
        ]
        metadata = world.metadata()
        assert metadata["pending_file_review"] is True
        set_at = datetime.fromisoformat(metadata["pending_file_review_at"])
        assert datetime.now(timezone.utc) - set_at < timedelta(minutes=1)
        # Decision 17: the ack's id is tracked and stored like the reply's.
        ack_mid = world.send_results[-1]["messageId"]
        assert ack_mid in [entry[0] for entry in metadata["ai_msg_ids"]]

    def test_captioned_video_gets_the_ack_and_never_the_pipeline(
        self, zapi_file_world: Callable[..., _ZapiFileWorld]
    ) -> None:
        world = zapi_file_world()

        resp = world.post(world.video(caption="Olha o vídeo do exame"))

        assert resp.json() == {"ok": True, "replied": True, "action": "file_ack"}
        assert world.sent() == [_g3_ack()]
        assert world.pipeline_contents() == []

    def test_english_agent_gets_the_english_ack(
        self, zapi_file_world: Callable[..., _ZapiFileWorld]
    ) -> None:
        world = zapi_file_world(clinic_config=_G3_EN_CONFIG)

        world.post(world.document())

        assert world.sent() == [_g3_ack("en")]

    def test_a_burst_of_files_gets_one_ack(
        self,
        zapi_file_world: Callable[..., _ZapiFileWorld],
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        world = zapi_file_world()

        first = world.post(world.document())
        stamped = world.metadata()["pending_file_review_at"]
        video = world.post(world.video())
        gif = world.post(world.image(mimeType="image/gif"))

        assert first.json() == {"ok": True, "replied": True, "action": "file_ack"}
        suppressed = {"ok": True, "replied": False, "action": "file_ack_suppressed"}
        assert video.json() == suppressed
        assert gif.json() == suppressed
        assert world.sent() == [_g3_ack()]
        # The non-described image still reports its own outcome, once.
        assert [e["outcome"] for e in _g3_image_events(caplog)] == ["unsupported_mime"]
        assert world.metadata()["pending_file_review_at"] == stamped

    def test_seeded_active_review_suppresses_the_ack(
        self, zapi_file_world: Callable[..., _ZapiFileWorld]
    ) -> None:
        world = zapi_file_world()
        review = _g3_review(timedelta(minutes=10))
        world.seed_session(review)

        resp = world.post(world.document())

        assert resp.json() == {
            "ok": True,
            "replied": False,
            "action": "file_ack_suppressed",
        }
        world.send.assert_not_awaited()
        assert world.metadata()["pending_file_review_at"] == (
            review["pending_file_review_at"]
        )

    @pytest.mark.parametrize(
        "review",
        [
            pytest.param(_g3_review(timedelta(hours=3)), id="expired"),
            pytest.param(_g3_review(None), id="no_timestamp"),
        ],
    )
    def test_expired_or_legacy_review_resends_the_ack(
        self,
        zapi_file_world: Callable[..., _ZapiFileWorld],
        review: dict[str, Any],
    ) -> None:
        world = zapi_file_world()
        world.seed_session(review)

        resp = world.post(world.document())

        assert resp.json() == {"ok": True, "replied": True, "action": "file_ack"}
        assert world.sent() == [_g3_ack()]
        set_at = datetime.fromisoformat(world.metadata()["pending_file_review_at"])
        assert datetime.now(timezone.utc) - set_at < timedelta(minutes=1)

    def test_missing_send_credentials_set_no_flag(
        self, zapi_file_world: Callable[..., _ZapiFileWorld]
    ) -> None:
        world = zapi_file_world()
        _g3_drop_credential(world.client, world.tenant_id, "zapi_token")

        resp = world.post(world.document())

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": False, "reason": "no_credentials"}
        world.send.assert_not_awaited()
        assert "pending_file_review" not in world.metadata()

    def test_failed_send_leaves_nothing_pending(
        self, zapi_file_world: Callable[..., _ZapiFileWorld]
    ) -> None:
        from src.api.routes import webhook as webhook_module

        world = zapi_file_world()
        world.send.side_effect = RuntimeError("upstream_http_error status=500")

        failed = world.post(world.document())

        assert failed.json() == {"ok": True, "replied": False, "reason": "send_failed"}
        metadata = world.metadata()
        assert "pending_file_review" not in metadata
        assert "ai_msg_ids" not in metadata
        assert list(webhook_module._INMEM_SENT) == []

        world.send.side_effect = None
        world.send.return_value = _fake_send_result(_g3_mid("retry"))
        retried = world.post(world.document())

        assert retried.json() == {"ok": True, "replied": True, "action": "file_ack"}
        assert world.metadata()["pending_file_review"] is True

    @pytest.mark.parametrize(
        ("cross_worker", "skipped"),
        [(False, "ai_echo"), (True, "ai_echo_db")],
    )
    def test_ack_echo_is_an_ai_echo_and_never_pauses(
        self,
        zapi_file_world: Callable[..., _ZapiFileWorld],
        cross_worker: bool,
        skipped: str,
    ) -> None:
        from src.api.routes import webhook as webhook_module

        world = zapi_file_world()
        assert world.post(world.document()).json()["action"] == "file_ack"
        ack_mid = world.send_results[-1]["messageId"]
        if cross_worker:
            # Another worker, Redis down: only the database knows the id.
            webhook_module._INMEM_SENT.clear()

        echo = world.text(_g3_ack(), message_id=ack_mid, from_me=True)
        resp = world.post(echo)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "skipped": skipped}
        assert world.session().is_paused is False


class TestZapiPendingFileReview:
    """Decisions 3, 4, 7, 14, 19: what resolves a pending review, and how."""

    def test_forward_request_opens_the_handoff_and_confirms(
        self, zapi_file_world: Callable[..., _ZapiFileWorld]
    ) -> None:
        from src.api.routes import webhook as webhook_module
        from src.interaction_context import (
            InteractionOrigin,
            ResolvedInteractionContext,
        )

        world = zapi_file_world()
        assert world.post(world.document()).json()["action"] == "file_ack"
        ingress_marker = object()
        ingress = MagicMock(return_value=ingress_marker)
        record = AsyncMock(return_value=None)
        text = world.text("Pode encaminhar para a equipe, por favor")

        with (
            patch("src.api.routes.webhook.zapi_ingress", ingress),
            patch("src.api.routes.webhook.record_meta_file_handoff", record),
        ):
            resp = world.post(text)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True, "action": "file_handoff"}
        session_id = world.session().id
        world.handoff.assert_awaited_once()
        assert world.handoff.await_args.args == (
            world.tenant_id,
            session_id,
            world.phone,
            "file_pending_review",
        )
        # Main's observational branch (no enforcement here): the handoff
        # commits on its own, then the Z-API ingress records it.
        handoff_kwargs = world.handoff.await_args.kwargs
        assert set(handoff_kwargs) == {"context"}
        context = handoff_kwargs["context"]
        assert type(context) is ResolvedInteractionContext
        assert context.origin is InteractionOrigin.PRODUCTION
        assert context.effective_tenant_id == world.tenant_id
        assert context.session_id == session_id
        ingress.assert_called_once()
        assert ingress.call_args.kwargs["body"] == text
        assert ingress.call_args.kwargs["context"] is context
        record.assert_awaited_once()
        assert record.await_args.args[0] is ingress_marker
        assert record.await_args.args[2] is world.handoff.return_value
        assert record.await_args.kwargs == {"note": "file_pending_review"}

        assert world.sent() == [_g3_ack(), _g3_forwarded()]
        assert world.pipeline_contents() == []
        metadata = world.metadata()
        assert "pending_file_review" not in metadata
        assert "pending_file_review_at" not in metadata
        forward_mid = world.send_results[-1]["messageId"]
        assert forward_mid in [entry[0] for entry in metadata["ai_msg_ids"]]
        assert forward_mid in webhook_module._INMEM_SENT
        # Decision 19: the forward opens the handoff but never pauses the AI.
        assert world.session().is_paused is False

    def test_english_forward_confirmation(
        self, zapi_file_world: Callable[..., _ZapiFileWorld]
    ) -> None:
        world = zapi_file_world(clinic_config=_G3_EN_CONFIG)
        world.post(world.document())

        resp = world.post(world.text("Please forward it to the team"))

        assert resp.json() == {"ok": True, "replied": True, "action": "file_handoff"}
        assert world.sent() == [_g3_ack("en"), _g3_forwarded("en")]

    def test_a_failed_confirmation_still_answers_file_handoff(
        self, zapi_file_world: Callable[..., _ZapiFileWorld]
    ) -> None:
        world = zapi_file_world()
        world.post(world.document())
        world.send.side_effect = RuntimeError("upstream_http_error status=500")

        resp = world.post(world.text("Pode encaminhar"))

        assert resp.json() == {"ok": True, "replied": True, "action": "file_handoff"}
        world.handoff.assert_awaited_once()
        assert "pending_file_review" not in world.metadata()

    def test_describing_the_need_clears_the_review_and_runs_the_pipeline(
        self, zapi_file_world: Callable[..., _ZapiFileWorld]
    ) -> None:
        world = zapi_file_world()
        world.post(world.document())

        resp = world.post(world.text("Queria saber se vocês aceitam convênio"))

        assert resp.json() == {"ok": True, "replied": True}
        world.handoff.assert_not_awaited()
        assert world.pipeline_contents() == ["Queria saber se vocês aceitam convênio"]
        assert world.sent() == [_g3_ack(), _G3_REPLY]
        metadata = world.metadata()
        assert "pending_file_review" not in metadata
        assert "pending_file_review_at" not in metadata

    def test_an_image_described_after_the_ack_keeps_the_review(
        self, zapi_file_world: Callable[..., _ZapiFileWorld]
    ) -> None:
        """Decision 14: describing an image leaves an active review alone."""
        world = zapi_file_world()
        world.post(world.document())
        before = world.metadata()

        resp = world.post(world.image())

        assert resp.json() == {"ok": True, "replied": True}
        world.describe.assert_awaited_once()
        assert world.pipeline_contents() == [_g3_marker()]
        assert world.sent() == [_g3_ack(), _G3_REPLY]
        after = world.metadata()
        assert after["pending_file_review"] is True
        assert after["pending_file_review_at"] == before["pending_file_review_at"]

    @pytest.mark.parametrize(
        ("transcript", "handed_off"),
        [("pode encaminhar para a equipe", True), ("quero marcar consulta", False)],
    )
    def test_transcribed_audio_resolves_the_review(
        self,
        zapi_file_world: Callable[..., _ZapiFileWorld],
        transcript: str,
        handed_off: bool,
    ) -> None:
        from src.services.safe_egress import SafeDownload

        world = zapi_file_world(stt=True)
        world.post(world.document())

        with (
            patch(
                "src.api.routes.webhook.safe_download_audio",
                new=AsyncMock(
                    return_value=SafeDownload(
                        content=b"OggS_g3_audio", mime_type="audio/ogg"
                    )
                ),
            ),
            patch(
                "src.services.groq_service.transcribe_audio",
                new=AsyncMock(return_value=transcript),
            ),
        ):
            resp = world.post(world.audio())

        assert resp.status_code == 200, resp.text
        assert "pending_file_review" not in world.metadata()
        if handed_off:
            assert resp.json() == {
                "ok": True,
                "replied": True,
                "action": "file_handoff",
            }
            world.handoff.assert_awaited_once()
            assert world.sent() == [_g3_ack(), _g3_forwarded()]
            assert world.pipeline_contents() == []
        else:
            assert resp.json() == {"ok": True, "replied": True}
            world.handoff.assert_not_awaited()
            assert world.pipeline_contents() == [transcript]

    @pytest.mark.parametrize(
        ("age", "handed_off"),
        [
            pytest.param(timedelta(minutes=10), True, id="active"),
            pytest.param(timedelta(hours=3), False, id="expired"),
            pytest.param(None, False, id="no_timestamp"),
        ],
    )
    def test_forward_words_count_only_inside_the_window(
        self,
        zapi_file_world: Callable[..., _ZapiFileWorld],
        age: timedelta | None,
        handed_off: bool,
    ) -> None:
        world = zapi_file_world()
        world.seed_session(_g3_review(age))

        resp = world.post(world.text("me manda o endereço"))

        if handed_off:
            assert resp.json() == {
                "ok": True,
                "replied": True,
                "action": "file_handoff",
            }
            world.handoff.assert_awaited_once()
            assert world.pipeline_contents() == []
        else:
            assert resp.json() == {"ok": True, "replied": True}
            world.handoff.assert_not_awaited()
            assert world.pipeline_contents() == ["me manda o endereço"]
            # The predicate is read-only: an expired or legacy flag stays.
            assert world.metadata()["pending_file_review"] is True

    def test_captioned_image_never_resolves_and_keeps_the_review(
        self, zapi_file_world: Callable[..., _ZapiFileWorld]
    ) -> None:
        world = zapi_file_world()
        world.post(world.document())
        before = world.metadata()

        resp = world.post(world.image(caption="pode encaminhar para a equipe"))

        assert resp.json() == {"ok": True, "replied": True}
        world.handoff.assert_not_awaited()
        assert world.pipeline_contents() == [
            f"pode encaminhar para a equipe\n\n{_g3_marker()}"
        ]
        after = world.metadata()
        assert after["pending_file_review"] is True
        assert after["pending_file_review_at"] == before["pending_file_review_at"]

    def test_captioned_document_never_resolves_and_is_suppressed(
        self, zapi_file_world: Callable[..., _ZapiFileWorld]
    ) -> None:
        """The documented ``document`` has no caption; a variant carrying one
        (as other WhatsApp gateways do) must still never resolve."""
        world = zapi_file_world()
        world.post(world.document())
        before = world.metadata()

        resp = world.post(world.document(caption="pode encaminhar para a equipe"))

        assert resp.json() == {
            "ok": True,
            "replied": False,
            "action": "file_ack_suppressed",
        }
        world.handoff.assert_not_awaited()
        assert world.sent() == [_g3_ack()]
        after = world.metadata()
        assert after["pending_file_review"] is True
        assert after["pending_file_review_at"] == before["pending_file_review_at"]


class TestZapiFileMediaUnchanged:
    """Stickers and staff (``fromMe``) media keep today's ``non-text`` exit."""

    def test_patient_sticker_stays_non_text(
        self, zapi_file_world: Callable[..., _ZapiFileWorld]
    ) -> None:
        world = zapi_file_world()

        resp = world.post(world.media("sticker"))

        assert resp.json() == {"ok": True, "skipped": "non-text"}
        world.download.assert_not_awaited()
        world.send.assert_not_awaited()
        assert world.pipeline_contents() == []
        assert _g3_session_row(world.client, world.tenant_id, world.phone) is None

    @pytest.mark.parametrize("kind", ["image", "document", "video"])
    def test_staff_media_keeps_the_non_text_exit_and_never_pauses(
        self,
        zapi_file_world: Callable[..., _ZapiFileWorld],
        caplog: pytest.LogCaptureFixture,
        kind: str,
    ) -> None:
        world = zapi_file_world()
        world.seed_session()
        clinic_message = AsyncMock()

        with patch("src.api.routes.webhook._handle_clinic_message", clinic_message):
            resp = world.post(world.media(kind, from_me=True, caption="Da equipe"))

        assert resp.json() == {"ok": True, "skipped": "non-text"}
        clinic_message.assert_not_awaited()  # no human takeover
        assert world.session().is_paused is False
        world.download.assert_not_awaited()
        world.describe.assert_not_awaited()
        world.send.assert_not_awaited()
        assert world.pipeline_contents() == []
        assert _g3_image_events(caplog) == []

    @pytest.mark.parametrize("kind", ["image", "document", "video"])
    @pytest.mark.parametrize(
        "chat",
        [
            pytest.param({"isGroup": True}, id="isGroup"),
            pytest.param({"isNewsletter": True}, id="isNewsletter"),
            pytest.param({"phone": "120363019502650977-group"}, id="group_phone"),
        ],
    )
    def test_group_and_channel_media_take_the_group_chat_skip(
        self,
        zapi_file_world: Callable[..., _ZapiFileWorld],
        caplog: pytest.LogCaptureFixture,
        kind: str,
        chat: dict[str, Any],
    ) -> None:
        """Owner decision 2026-11-07 (MEDIUM-1): like Uazapi, media in a group
        or a channel stays out of the file flow. Since the route-level group
        skip (``_is_zapi_group_or_channel``) it never reaches the media gate."""
        world = zapi_file_world()
        payload = world.media(kind, caption="pode encaminhar para a equipe")
        payload.update(chat)

        resp = world.post(payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "skipped": "group_chat"}
        world.download.assert_not_awaited()
        world.describe.assert_not_awaited()
        world.send.assert_not_awaited()
        world.handoff.assert_not_awaited()
        assert world.pipeline_contents() == []
        assert _g3_image_events(caplog) == []
        assert _g3_session_count(world.client, world.tenant_id) == 0


# ============================================
# Group and channel chats are skipped
# ============================================
#
# Z-API "on-message-received": a group payload carries ``"isGroup": true``
# and a ``"<id>-group"`` phone; a channel carries ``"isNewsletter": true``.
# Mirrors the Uazapi ``group_chat`` skip: nothing past the replay claim runs.

_GROUP_PHONE = "120363019502650977-group"
_NEWSLETTER_PHONE = "120363166555745933@newsletter"

# Shapes without a ``phone`` keep the 1:1 phone of the base body, so the flag
# alone has to trigger the skip; shapes without flags pin each phone suffix.
_GROUP_OR_CHANNEL_SHAPES = {
    "is_group_flag": {"isGroup": True, "phone": _GROUP_PHONE},
    "is_newsletter_flag": {"isNewsletter": True, "phone": _NEWSLETTER_PHONE},
    "group_phone_without_flags": {"phone": _GROUP_PHONE},
    "is_group_flag_with_direct_phone": {"isGroup": True},
    "is_newsletter_flag_with_direct_phone": {"isNewsletter": True},
    "g_us_jid_without_flags": {"phone": "120363019502650977@g.us"},
    "newsletter_jid_without_flags": {"phone": _NEWSLETTER_PHONE},
}


def _group_message_body(*, instance_id: str, kind: str, shape: str) -> dict:
    """A Z-API payload of ``kind`` reshaped into a group or channel chat."""
    message_id = f"mid_group_{uuid4().hex[:10]}"
    if kind == "patient_audio":
        body = _zapi_audio_body(instance_id=instance_id, message_id=message_id)
    elif kind == "patient_image":
        body = _zapi_body(instance_id=instance_id, message_id=message_id)
        del body["text"]
        body["image"] = {
            "imageUrl": "https://cdn.example.com/photo.jpg",
            "mimeType": "image/jpeg",
            "caption": "",
        }
    else:
        body = _zapi_body(
            instance_id=instance_id,
            message_id=message_id,
            from_me=kind == "from_me_text",
        )
    body["participantPhone"] = "5511900000901"
    body.update(_GROUP_OR_CHANNEL_SHAPES[shape])
    return body


def _create_active_session(client: TestClient, tenant_id: UUID, phone: str) -> UUID:
    """Create the phone's active, unpaused session through the webhook helper."""
    from src.api.routes.webhook import _get_or_create_session
    from src.repositories.database import get_db_session

    async def _run() -> UUID:
        db = get_db_session()
        try:
            session_id, _ = await _get_or_create_session(tenant_id, phone, db)
            await db.commit()
            return session_id
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined]


def _tenant_sessions(client: TestClient, tenant_id: UUID) -> list[Any]:
    """Every session row of the tenant, read in a new DB session."""
    from sqlalchemy import select

    from src.models.session import Session as SessionRow
    from src.repositories.database import get_db_session

    async def _run() -> list[Any]:
        db = get_db_session()
        try:
            result = await db.execute(
                select(SessionRow).where(SessionRow.tenant_id == tenant_id)
            )
            return list(result.scalars().all())
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined]


class TestZapiGroupAndChannelSkip:
    @pytest.mark.parametrize("shape", sorted(_GROUP_OR_CHANNEL_SHAPES))
    @pytest.mark.parametrize(
        "kind", ["patient_text", "patient_audio", "patient_image", "from_me_text"]
    )
    def test_group_or_channel_message_is_skipped_before_any_side_effect(
        self,
        client: TestClient,
        tenant_id: UUID,
        kind: str,
        shape: str,
    ) -> None:
        from src.api.routes import webhook as webhook_module
        from src.services.safe_egress import SafeDownload

        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        # Real STT + multimodal credentials, read through the real loader, and
        # a non-empty clinic config: a patient audio that got past the skip
        # would reach the download.
        _seed_zapi_audio_tenant(
            client, tenant_id, instance_id, raw_token, stt=True, multimodal=True
        )
        body = _group_message_body(instance_id=instance_id, kind=kind, shape=shape)
        # A session the bot already holds for this chat (how the bug looked
        # in production): a clinic post in the group must not pause it.
        group_session_id = _create_active_session(
            client, tenant_id, webhook_module._normalise_phone(body["phone"])
        )

        service = _mock_pipeline_service()
        service._load_clinic_config = AsyncMock(return_value={"business_rules": {}})
        mock_send = AsyncMock(return_value=_fake_send_result())
        mock_download = AsyncMock(
            return_value=SafeDownload(content=b"OggS_fake", mime_type="audio/ogg")
        )
        mock_transcribe = AsyncMock(return_value=("Olá", None))
        mock_read = AsyncMock()
        # Call-through spies: the real takeover, credential read, credit
        # pre-check and session lookup run if the skip is ever bypassed.
        spy_clinic_message = AsyncMock(
            side_effect=webhook_module._handle_clinic_message
        )
        spy_creds = AsyncMock(side_effect=webhook_module._load_tenant_credentials)
        spy_precheck = AsyncMock(
            side_effect=webhook_module.credit_metering.precheck_zapi
        )
        spy_session = AsyncMock(side_effect=webhook_module._get_or_create_session)
        with (
            patch("src.api.routes.webhook.get_event_service", return_value=service),
            patch("src.api.routes.webhook.send_text_message", new=mock_send),
            patch("src.api.routes.webhook.send_zapi_audio", new=AsyncMock()),
            patch("src.api.routes.webhook.safe_download_audio", new=mock_download),
            patch(
                "src.api.routes.webhook._transcribe_audio_chain", new=mock_transcribe
            ),
            patch("src.api.routes.webhook.mark_message_read", new=mock_read),
            patch("src.api.routes.webhook.mark_conversation_unread", new=AsyncMock()),
            patch(
                "src.api.routes.webhook._handle_clinic_message",
                new=spy_clinic_message,
            ),
            patch("src.api.routes.webhook._load_tenant_credentials", new=spy_creds),
            patch(
                "src.api.routes.webhook.credit_metering.precheck_zapi",
                new=spy_precheck,
            ),
            patch("src.api.routes.webhook._get_or_create_session", new=spy_session),
        ):
            resp = client.post(f"/v1/webhook/zapi/{raw_token}", json=body)

        # Persisted state first, then the side-effect seams, then the envelope:
        # a bypassed skip fails on the effect it actually caused.
        sessions = _tenant_sessions(client, tenant_id)
        assert [row.id for row in sessions] == [group_session_id]
        assert sessions[0].is_paused is False
        assert mock_download.await_count == 0
        assert mock_transcribe.await_count == 0
        assert mock_read.await_count == 0
        assert mock_send.await_count == 0
        assert spy_clinic_message.await_count == 0
        assert spy_creds.await_count == 0
        assert spy_precheck.await_count == 0
        assert spy_session.await_count == 0
        assert service._load_clinic_config.await_count == 0
        assert service.run_pipeline.await_count == 0
        assert service.finish_response.await_count == 0
        assert _message_in_rows(client, tenant_id) == []
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "skipped": "group_chat"}

    @pytest.mark.parametrize(
        "body",
        [
            {"isGroup": "true", "phone": "5511900000903"},
            {"isGroup": "false", "phone": "5511900000903"},
            {"phone": "5511900000903@lid"},
            {"phone": "5511900000903"},
        ],
        ids=["is_group_string_true", "is_group_string_false", "lid_phone", "digits"],
    )
    def test_non_group_payloads_are_not_classified_as_group(self, body: dict) -> None:
        from src.api.routes.webhook import _is_zapi_group_or_channel

        assert _is_zapi_group_or_channel(body) is False

    def test_one_to_one_message_still_reaches_the_pipeline(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        instance_id = f"3E{uuid4().hex[:10]}"
        raw_token = _new_canonical_token()
        _seed_zapi_tenant(client, tenant_id, instance_id, raw_token)
        body = _zapi_body(
            instance_id=instance_id,
            message_id=f"mid_direct_{uuid4().hex[:10]}",
            phone="5511900000902",
        )
        body.update({"isGroup": False, "isNewsletter": False})

        service = _mock_pipeline_service()
        service.finish_response = AsyncMock(return_value=_fake_event_response("Oi!"))
        mock_send = AsyncMock(return_value=_fake_send_result())
        with (
            patch("src.api.routes.webhook.get_event_service", return_value=service),
            patch("src.api.routes.webhook.send_text_message", new=mock_send),
            patch("src.api.routes.webhook.mark_conversation_unread", new=AsyncMock()),
            patch("src.api.routes.webhook.mark_message_read", new=AsyncMock()),
        ):
            resp = client.post(f"/v1/webhook/zapi/{raw_token}", json=body)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True}
        assert service.run_pipeline.await_count == 1
        assert mock_send.await_count == 1
        assert [row.user_id for row in _tenant_sessions(client, tenant_id)] == [
            "5511900000902"
        ]
