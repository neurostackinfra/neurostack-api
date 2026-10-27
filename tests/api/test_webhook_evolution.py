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
        response_language="pt-BR",
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
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Reply body")
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
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Reply body")
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
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Reply body")
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


class TestEvolutionReplayBoundary:
    def test_missing_message_id_returns_existing_200_envelope_before_effects(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        payload = _evolution_text_payload(
            instance_name=instance,
            from_phone="5511900000400",
            text_body="Olá",
            message_id="",
        )

        with patch("src.api.routes.webhook.get_event_service") as get_service:
            response = client.post(WEBHOOK_PATH, json=payload)

        assert response.status_code == 200
        assert response.json() == {"ok": True, "skipped": "missing_message_id"}
        get_service.assert_not_called()

    def test_duplicate_is_claimed_before_config_session_or_effects(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        payload = _evolution_text_payload(
            instance_name=instance,
            from_phone="5511900000401",
            text_body="Olá",
            message_id=f"evo_dup_{uuid4().hex[:8]}",
        )

        with (
            patch(
                "src.api.routes.webhook._claim_incoming_message",
                return_value=False,
            ) as claim,
            patch("src.api.routes.webhook.get_event_service") as get_service,
            patch("src.api.routes.webhook._get_or_create_session") as create_session,
        ):
            response = client.post(WEBHOOK_PATH, json=payload)

        assert response.status_code == 200
        assert response.json() == {"ok": True, "skipped": "duplicate_message"}
        claim.assert_called_once_with(
            "evolution",
            tenant_id,
            payload["data"]["key"]["id"],
        )
        get_service.assert_not_called()
        create_session.assert_not_called()

    def test_replay_store_unavailable_returns_exact_503(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        from src.api.routes.webhook import ReplayStoreUnavailable

        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        payload = _evolution_text_payload(
            instance_name=instance,
            from_phone="5511900000402",
            text_body="Olá",
            message_id=f"evo_unavailable_{uuid4().hex[:8]}",
        )

        with patch(
            "src.api.routes.webhook._claim_incoming_message",
            side_effect=ReplayStoreUnavailable("redis down"),
        ):
            response = client.post(WEBHOOK_PATH, json=payload)

        assert response.status_code == 503
        assert response.json() == {"error": "replay_store_unavailable"}

    def test_retry_after_post_commit_send_failure_does_not_run_transition_twice(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        payload = _evolution_text_payload(
            instance_name=instance,
            from_phone="5511900000403",
            text_body="Olá, gostaria de saber mais sobre a clínica",
            message_id=f"evo_retry_{uuid4().hex[:8]}",
        )

        with (
            patch(
                "src.api.routes.webhook._get_or_create_session",
                new=AsyncMock(return_value=(uuid4(), False)),
            ),
            patch("src.api.routes.webhook.get_event_service") as get_service,
            patch(
                "src.api.routes.webhook.send_evolution_text",
                new=AsyncMock(side_effect=RuntimeError("post-commit send failed")),
            ),
        ):
            service = MagicMock()
            service._load_clinic_config = AsyncMock(return_value={})
            service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
            service.finish_response = AsyncMock(
                return_value=_fake_event_response("Resposta")
            )
            get_service.return_value = service

            first = client.post(WEBHOOK_PATH, json=payload)
            second = client.post(WEBHOOK_PATH, json=payload)

        assert first.status_code == 200
        assert first.json() == {"ok": True, "replied": False, "reason": "send_failed"}
        assert second.status_code == 200
        assert second.json() == {"ok": True, "skipped": "duplicate_message"}
        service.run_pipeline.assert_awaited_once()
        service.finish_response.assert_awaited_once()


# ============================================
# interaction-origin-training-safety Group 4A — origin of Evolution messages
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


class TestEvolutionInteractionOrigin:
    """Evolution is ``unknown``: its instance name selects a tenant but proves
    nothing about the sender, so no request shape can make it ``production``.
    """

    def test_resolved_instance_threads_one_unknown_context_to_both_phases(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        from src.interaction_context import (
            InteractionOrigin,
            ResolvedInteractionContext,
        )

        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        payload = _evolution_text_payload(
            instance_name=instance,
            from_phone="5511900000501",
            text_body="Olá, quero marcar",
            message_id=f"evo_origin_{uuid4().hex[:8]}",
        )
        built: list[Any] = []
        service = _mock_pipeline_service()

        with (
            patch(
                "src.api.routes.webhook.build_interaction_context",
                new=_recording_context_builder(built),
            ),
            patch("src.api.routes.webhook.get_event_service", return_value=service),
        ):
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert len(built) == 1
        context = built[0]
        event_request = service.run_pipeline.await_args.args[0]
        assert service.run_pipeline.await_args.kwargs["context"] is context
        assert service.finish_response.await_args.kwargs["context"] is context
        assert type(context) is ResolvedInteractionContext
        assert context.origin is InteractionOrigin.UNKNOWN
        assert context.effective_tenant_id == tenant_id == event_request.tenant_id
        assert context.session_id == event_request.session_id
        assert context.interaction_event_id is not None

    @pytest.mark.parametrize(
        "claim",
        ["body_origin", "payload_origin", "api_key_header", "panel_jwt_header"],
    )
    def test_never_production_whatever_the_request_claims(
        self,
        client: TestClient,
        tenant_id: UUID,
        claim: str,
    ) -> None:
        from src.interaction_context import InteractionOrigin

        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        payload: dict[str, Any] = _evolution_text_payload(
            instance_name=instance,
            from_phone="5511900000502",
            text_body="Olá",
            message_id=f"evo_claim_{uuid4().hex[:8]}",
        )
        headers: dict[str, str] = {}
        if claim == "body_origin":
            payload["interaction_origin"] = "production"
        elif claim == "payload_origin":
            payload["data"]["interaction_origin"] = "production"
            payload["data"]["metadata"] = {"interaction_origin": "production"}
        elif claim == "api_key_header":
            headers["X-API-Key"] = "test-api-key-12345"
        else:
            headers.update(_admin_headers(tenant_id))
        service = _mock_pipeline_service()

        with patch("src.api.routes.webhook.get_event_service", return_value=service):
            resp = client.post(WEBHOOK_PATH, json=payload, headers=headers)

        assert resp.status_code == 200, resp.text
        context = service.run_pipeline.await_args.kwargs["context"]
        assert context.origin is InteractionOrigin.UNKNOWN
        assert service.finish_response.await_args.kwargs["context"] is context

    def test_unresolved_instance_classifies_nothing(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        _seed_evolution_credentials(client, tenant_id, f"evo_{uuid4().hex[:8]}")
        payload = _evolution_text_payload(
            instance_name=f"evo_stranger_{uuid4().hex[:8]}",
            from_phone="5511900000503",
            text_body="Olá",
            message_id=f"evo_stranger_{uuid4().hex[:8]}",
        )
        built: list[Any] = []
        service = _mock_pipeline_service()

        with (
            patch(
                "src.api.routes.webhook.build_interaction_context",
                new=_recording_context_builder(built),
            ),
            patch("src.api.routes.webhook.get_event_service", return_value=service),
        ):
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "skipped": "unknown_instance"}
        assert built == []
        service.run_pipeline.assert_not_awaited()
        assert _message_in_rows(client, tenant_id) == []

    def test_persisted_message_in_id_is_the_context_interaction_id(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        from src.services.event_service import EventService

        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        payload = _evolution_text_payload(
            instance_name=instance,
            from_phone="5511900000504",
            text_body="Olá, bom dia",
            message_id=f"evo_persist_{uuid4().hex[:8]}",
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
                "src.api.routes.webhook.send_evolution_text",
                new=AsyncMock(return_value=_fake_evolution_send_result()),
            ),
        ):
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        context = seen["run"]
        assert seen["finish"] is context
        assert context.interaction_event_id is not None
        assert seen["response"].interaction_event_id == context.interaction_event_id
        rows = _message_in_rows(client, tenant_id)
        assert [row.id for row in rows] == [context.interaction_event_id]
        assert rows[0].session_id == context.session_id
        assert _session_origins(client, context.session_id) == {"unknown"}

    def test_paused_capture_persists_unknown_under_the_context_id(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        from src.interaction_context import InteractionOrigin

        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        phone = "5511900000505"
        session_id = _create_paused_session(client, tenant_id, phone)
        payload = _evolution_text_payload(
            instance_name=instance,
            from_phone=phone,
            text_body="Ainda estou aguardando",
            message_id=f"evo_paused_{uuid4().hex[:8]}",
        )
        built: list[Any] = []
        create_session = AsyncMock(side_effect=AssertionError("session created"))

        with (
            patch(
                "src.api.routes.webhook.build_interaction_context",
                new=_recording_context_builder(built),
            ),
            patch("src.api.routes.webhook._get_or_create_session", create_session),
        ):
            resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 202, resp.text
        assert resp.json() == {"ok": True, "skipped": "session_paused"}
        create_session.assert_not_awaited()
        assert len(built) == 1
        context = built[0]
        assert context.origin is InteractionOrigin.UNKNOWN
        assert (context.effective_tenant_id, context.session_id) == (
            tenant_id,
            session_id,
        )
        rows = _message_in_rows(client, tenant_id)
        assert [row.id for row in rows] == [context.interaction_event_id]
        assert rows[0].interaction_origin == "unknown"
        assert rows[0].event_metadata == {"paused_capture": True}
