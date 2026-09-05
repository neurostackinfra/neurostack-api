"""
NeuroStack API — Evolution API Webhook Tests

Created by Group 1b of the ``uazapi-parity`` wish. Before this file no
test in the repository reached ``evolution_webhook`` and the LGPD block
that Group 1b substitutes was completely uncovered.

Scope is deliberately narrow: this file exercises the LGPD privacy notice
block on Evolution (the block the group refactored) and the new
outbound-id normalisation on the reply path. Full coverage of the
Evolution handler remains out of scope (declared in the wish under OUT).
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
WEBHOOK_PATH = "/v1/webhook/evolution"


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
            "email": "admin@evolution-webhook-test.com",
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


def _seed_evolution_credentials(
    client: TestClient,
    tenant_id: UUID,
    instance_name: str,
) -> None:
    """Seed a persisted legacy Evolution credential row for webhook coverage."""
    from src.models.clinic_config_table import ClinicConfigTable
    from src.repositories.database import get_db_session

    async def _run() -> None:
        db = get_db_session()
        try:
            db.add(
                ClinicConfigTable(
                    tenant_id=tenant_id,
                    api_credentials={
                        "whatsapp_provider": "evolution",
                        "evolution_api_url": "https://evolution.example.com",
                        "evolution_instance_name": instance_name,
                        "evolution_api_key": f"evk_{uuid4().hex}",
                    },
                )
            )
            await db.commit()
        finally:
            await db.close()

    client._submit(_run())  # type: ignore[attr-defined]


def _evolution_text_payload(
    *,
    instance_name: str,
    from_phone: str,
    text_body: str,
    message_id: str,
    from_me: bool = False,
) -> dict:
    """Build an Evolution API messages.upsert payload."""
    return {
        "event": "messages.upsert",
        "instance": instance_name,
        "data": {
            "key": {
                "remoteJid": f"{from_phone}@s.whatsapp.net",
                "fromMe": from_me,
                "id": message_id,
            },
            "message": {"conversation": text_body},
            "pushName": "Paciente",
        },
    }


def _fake_event_response(suggested: str | None = "Olá, tudo bem?"):
    prompt = SimpleNamespace(suggested_response=suggested)
    decision = SimpleNamespace(handoff_required=False)
    return SimpleNamespace(
        prompt=prompt,
        handoff_summary=None,
        decision=decision,
    )


def _fake_evolution_send_result(message_id: str | None = None) -> dict:
    """Evolution's send response — the ``id`` key carries the outbound id."""
    return {
        "key": {
            "remoteJid": "5511999999999@s.whatsapp.net",
            "fromMe": True,
            "id": message_id or f"evo_reply_{uuid4().hex[:10]}",
        },
        # Also mimic the shape that ``id`` sometimes appears at the top
        "id": message_id or f"evo_reply_{uuid4().hex[:10]}",
    }


def _lgpd_config(enabled: bool = True) -> dict:
    return {
        "lgpd": {"lgpd_enabled": enabled},
        "identity": {"clinic_name": "Clínica Teste"},
    }


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
    _INMEM_STT_NOTICE.clear()
    rate_limit_store._buckets.clear()
    rate_limit_store._tenant_limits.clear()
    yield


# ============================================
# uazapi-parity Group 1b — Privacy notice on Evolution
# ============================================


class TestEvolutionPrivacyNotice:
    """Group 1b Deliverable 3: minimal coverage of the LGPD block."""

    def test_new_session_with_lgpd_enabled_sends_privacy_notice(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """AC: sessão nova + lgpd_enabled ativo envia o aviso antes da
        resposta da IA (asserção sobre a chamada de envio).
        """
        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)

        payload = _evolution_text_payload(
            instance_name=instance,
            from_phone="5511900000200",
            text_body="Primeiro contato",
            message_id=f"evo_msg_{uuid4().hex[:8]}",
        )

        privacy_send = _fake_evolution_send_result("evo_privacy_id")
        reply_send = _fake_evolution_send_result("evo_reply_id")

        with (
            patch(
                "src.api.routes.webhook._get_or_create_session",
                new=AsyncMock(return_value=(uuid4(), True)),
            ),
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_evolution_text",
                new=AsyncMock(side_effect=[privacy_send, reply_send]),
            ) as mock_send,
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(
                return_value=_lgpd_config(enabled=True),
            )
            service.process_event = AsyncMock(
                return_value=_fake_event_response("Reply body"),
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True}
        # Two calls: aviso first, then AI reply
        assert mock_send.await_count == 2
        first_text = mock_send.await_args_list[0].args[4]
        second_text = mock_send.await_args_list[1].args[4]
        # PT default LGPD copy contains "LGPD"
        assert "LGPD" in first_text
        assert second_text == "Reply body"

    def test_existing_session_does_not_send_privacy_notice(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """AC: sessão já existente: nenhum aviso enviado, resposta normal
        segue.
        """
        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)

        payload = _evolution_text_payload(
            instance_name=instance,
            from_phone="5511900000201",
            text_body="Segunda mensagem",
            message_id=f"evo_msg_{uuid4().hex[:8]}",
        )

        with (
            patch(
                "src.api.routes.webhook._get_or_create_session",
                new=AsyncMock(return_value=(uuid4(), False)),
            ),
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_evolution_text",
                new=AsyncMock(return_value=_fake_evolution_send_result()),
            ) as mock_send,
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(
                return_value=_lgpd_config(enabled=True),
            )
            service.process_event = AsyncMock(
                return_value=_fake_event_response("Reply body"),
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True}
        # Only the reply — no aviso
        assert mock_send.await_count == 1
        assert mock_send.await_args_list[0].args[4] == "Reply body"

    def test_lgpd_disabled_does_not_send_privacy_notice(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        """AC: lgpd_enabled desligado: nenhum aviso, resposta normal
        segue.
        """
        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)

        payload = _evolution_text_payload(
            instance_name=instance,
            from_phone="5511900000202",
            text_body="Primeiro contato sem LGPD",
            message_id=f"evo_msg_{uuid4().hex[:8]}",
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
                "src.api.routes.webhook.send_evolution_text",
                new=AsyncMock(return_value=_fake_evolution_send_result()),
            ) as mock_send,
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(
                return_value=_lgpd_config(enabled=False),
            )
            service.process_event = AsyncMock(
                return_value=_fake_event_response("Reply body"),
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "replied": True}
        # Only the reply — LGPD disabled means no aviso
        assert mock_send.await_count == 1
        assert mock_send.await_args_list[0].args[4] == "Reply body"


class TestEvolutionReplyIdNormalisation:
    """Group 1b Deliverable 6 (Evolution half): id normalisation applied
    to Evolution's reply path — the ``id`` key that previously diverged
    between the two dedup layers is now used in the normalised form.
    """

    def test_reply_id_normalised_and_shared_between_dedup_layers(
        self,
        client: TestClient,
        tenant_id: UUID,
    ):
        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)

        payload = _evolution_text_payload(
            instance_name=instance,
            from_phone="5511900000210",
            text_body="ping",
            message_id=f"evo_ping_{uuid4().hex[:8]}",
        )

        # Reply send_result carries composite ``id`` — normalisation must
        # strip the ``owner:`` prefix on both dedup layers.
        composite_send = {"id": "owner_evo:REPLY_TAIL"}

        stored_ids: list[str] = []

        async def _store_spy(sid, mid, db):
            stored_ids.append(mid)

        with (
            patch(
                "src.api.routes.webhook._get_or_create_session",
                new=AsyncMock(return_value=(uuid4(), False)),
            ),
            patch(
                "src.api.routes.webhook.get_event_service",
            ) as mock_get_service,
            patch(
                "src.api.routes.webhook.send_evolution_text",
                new=AsyncMock(return_value=composite_send),
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
            service.process_event = AsyncMock(
                return_value=_fake_event_response("Reply body"),
            )
            mock_get_service.return_value = service

            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        # Stored id is the tail, no colon
        assert stored_ids == ["REPLY_TAIL"], stored_ids
        assert all(":" not in v for v in stored_ids)
        # _track_sent_message called with the same normalised messageid form
        assert mock_track.call_count == 1
        tracked_arg = mock_track.call_args.args[0]
        assert tracked_arg == {"messageid": "REPLY_TAIL"}
        assert ":" not in tracked_arg["messageid"]


class TestEvolutionVoiceReplyOrigin:
    def test_text_reply_reaches_gate_as_non_audio(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        payload = _evolution_text_payload(
            instance_name=instance,
            from_phone="5511900000290",
            text_body="Olá",
            message_id=f"evo_msg_{uuid4().hex[:8]}",
        )
        reply_gate = AsyncMock(
            return_value=(_fake_evolution_send_result("evo_voice_gate"), False),
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
            service.process_event = AsyncMock(
                return_value=_fake_event_response("Resposta"),
            )
            get_service.return_value = service
            response = client.post(WEBHOOK_PATH, json=payload)

        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "replied": True}
        reply_gate.assert_awaited_once()
        assert reply_gate.await_args.kwargs["input_was_audio"] is False

    def test_audio_remains_non_text_without_reaching_tts_gate(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        payload = _evolution_text_payload(
            instance_name=f"evo_{uuid4().hex[:8]}",
            from_phone="5511900000291",
            text_body="",
            message_id=f"evo_audio_{uuid4().hex[:8]}",
        )
        payload["data"]["message"] = {
            "audioMessage": {"url": "https://example.com/audio.ogg"}
        }
        reply_gate = AsyncMock()

        with patch(
            "src.api.routes.webhook._send_reply_with_optional_tts",
            reply_gate,
        ):
            response = client.post(WEBHOOK_PATH, json=payload)

        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "skipped": "non-text"}
        reply_gate.assert_not_awaited()
