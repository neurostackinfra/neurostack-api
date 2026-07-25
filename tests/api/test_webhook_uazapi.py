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
    """Save Uazapi credentials for a tenant."""
    resp = client.patch(
        API_CRED_PATH,
        json={
            "whatsapp_provider": "uazapi",
            "uazapi_api_url": api_url,
            "uazapi_token": token or f"uz_token_{uuid4().hex}",
            "uazapi_instance_name": instance_name,
        },
        headers=_admin_headers(tenant_id),
    )
    assert resp.status_code == 200, resp.text


def _seed_uazapi_credentials_missing_secrets(
    client: TestClient,
    tenant_id: UUID,
    instance_name: str,
) -> None:
    """Save only the instance_name so the tenant is resolvable but has no
    URL/token — used to exercise the ``no_credentials`` branch."""
    resp = client.patch(
        API_CRED_PATH,
        json={
            # Provide a real provider so the 501 guard doesn't fire.
            "whatsapp_provider": "zapi",
            "uazapi_instance_name": instance_name,
        },
        headers=_admin_headers(tenant_id),
    )
    assert resp.status_code == 200, resp.text


def _uazapi_text_payload(
    *,
    instance: str,
    from_phone: str,
    body: str,
    message_id: str,
    msg_type: str = "text",
) -> dict:
    """Build a Uazapi/wuzapi incoming text payload."""
    return {
        "instance": instance,
        "event": "message",
        "data": {
            "from": f"{from_phone}@s.whatsapp.net",
            "body": body,
            "type": msg_type,
            "id": message_id,
        },
    }


def _fake_event_response(suggested: str | None = "Olá, tudo bem?"):
    """Return a MagicMock shaped like an EventService.process_event result."""
    prompt = SimpleNamespace(suggested_response=suggested)
    decision = SimpleNamespace(handoff_required=False)
    return SimpleNamespace(
        prompt=prompt,
        handoff_summary=None,
        decision=decision,
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

        with patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service, patch(
            "src.api.routes.webhook.send_uazapi_text",
            new=AsyncMock(return_value=_fake_uazapi_send_result()),
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
        assert resp.json() == {"ok": True, "replied": True}

        # Pipeline was called with the parsed text + Uazapi metadata
        assert service.process_event.await_count == 1
        event_arg = service.process_event.await_args.args[0]
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
            rec for rec in caplog.records
            if "Uazapi webhook: msg" in rec.getMessage()
        ]
        assert receipt_logs, "expected a receipt log line for the incoming msg"
        assert msg_id in receipt_logs[0].getMessage()


class TestUazapiWebhookUnknownInstance:
    def test_unknown_instance_warns_and_skips(
        self,
        client: TestClient,
        tenant_id: UUID,
        caplog: pytest.LogCaptureFixture,
    ):
        # Seed a clinic with a *different* instance
        _seed_uazapi_credentials(
            client, tenant_id, f"owned_{uuid4().hex[:8]}",
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
            "no tenant for instance" in m and stranger_instance in m
            for m in warn_msgs
        ), warn_msgs


class TestUazapiWebhookNonText:
    def test_image_event_is_skipped_as_non_text(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """Uazapi payloads with ``data.type`` outside text/chat are ignored."""
        instance = f"clinica_{uuid4().hex[:8]}"
        _seed_uazapi_credentials(client, tenant_id, instance)

        payload = _uazapi_text_payload(
            instance=instance,
            from_phone="5511900000010",
            body="[image caption]",
            message_id=f"3EB0_img_{uuid4().hex[:8]}",
            msg_type="image",
        )

        with patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service, patch(
            "src.api.routes.webhook.send_uazapi_text",
            new=AsyncMock(return_value=_fake_uazapi_send_result()),
        ) as mock_send:
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.process_event = AsyncMock()
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "non-text"}
        # Never touched the pipeline or the send helper
        assert service.process_event.await_count == 0
        assert mock_send.await_count == 0


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

        with patch(
            "src.api.routes.webhook._check_bot_active",
            new=AsyncMock(return_value={"ok": True, "skipped": "bot_paused"}),
        ), patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service, patch(
            "src.api.routes.webhook.send_uazapi_text",
            new=AsyncMock(return_value=_fake_uazapi_send_result()),
        ) as mock_send:
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.process_event = AsyncMock(
                return_value=_fake_event_response("blocked"),
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        # Same 202 semantics as Z-API/Evolution/Meta paths when bot is paused
        assert resp.status_code == 202
        assert resp.json() == {"ok": True, "skipped": "bot_paused"}
        assert service.process_event.await_count == 0
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

        with patch(
            "src.api.routes.webhook.get_event_service",
        ) as mock_get_service, patch(
            "src.api.routes.webhook.send_uazapi_text",
            new=AsyncMock(return_value=_fake_uazapi_send_result()),
        ) as mock_send:
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.process_event = AsyncMock(
                return_value=_fake_event_response("Reply body"),
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
        assert any(
            "Uazapi credentials missing" in m for m in warn_msgs
        ), warn_msgs
