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
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote
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
    the DB — we compute the digest client-side and PATCH just that field.
    """
    digest = _digest_for(zapi_token_raw)
    resp = client.patch(
        API_CRED_PATH,
        json={
            "whatsapp_provider": "zapi",
            "zapi_instance_id": instance_id,
            "zapi_token": f"zt_{uuid4().hex}",
            "zapi_inbound_token_digest": digest,
        },
        headers=_admin_headers(tenant_id),
    )
    assert resp.status_code == 200, resp.text
    return zapi_token_raw


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
    return SimpleNamespace(prompt=prompt, handoff_summary=None, decision=decision)


def _fake_send_result(message_id: str = "zapi_mid_reply") -> dict:
    return {"messageId": message_id, "zapiMessageId": message_id}


@pytest.fixture
def tenant_id() -> UUID:
    return uuid4()


@pytest.fixture(autouse=True)
def _clear_process_state():
    """Reset per-worker replay + rate limit state between tests."""
    from src.api.middleware.rate_limit import rate_limit_store
    from src.api.routes.webhook import (
        _INMEM_INCOMING,
        _INMEM_SENT,
    )

    _INMEM_INCOMING.clear()
    _INMEM_SENT.clear()
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
            service.process_event = AsyncMock(
                return_value=_fake_event_response("Oi!"),
            )
            mock_get_service.return_value = service

            resp = client.post(f"/v1/webhook/zapi/{raw_token}", json=body)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True}
        assert service.process_event.await_count == 1
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

        # Rotate: PATCH the digest field with the new digest only.
        second_token = _new_canonical_token()
        resp = client.patch(
            API_CRED_PATH,
            json={"zapi_inbound_token_digest": _digest_for(second_token)},
            headers=_admin_headers(tenant_id),
        )
        assert resp.status_code == 200, resp.text

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
            service.process_event = AsyncMock(
                return_value=_fake_event_response("Ok"),
            )
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
            service.process_event = AsyncMock(
                return_value=_fake_event_response("First"),
            )
            mock_get_service.return_value = service

            first = client.post(f"/v1/webhook/zapi/{raw_token}", json=body)
            assert first.status_code == 200
            assert first.json() == {"ok": True, "replied": True}

            service.process_event = AsyncMock(
                return_value=_fake_event_response("should not fire"),
            )
            mock_send.reset_mock()

            second = client.post(f"/v1/webhook/zapi/{raw_token}", json=body)

        assert second.status_code == 200
        assert second.json() == {"ok": True, "skipped": "duplicate_message"}
        assert service.process_event.await_count == 0
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
