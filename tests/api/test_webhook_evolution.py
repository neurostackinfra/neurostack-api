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


def _baileys_id() -> str:
    """An outbound id in the format Evolution 2.3.7 sends with.

    Baileys 7.0.0-rc.9 (pinned by Evolution 2.3.7) builds message ids as
    ``'3EB0' + 18 uppercase hex`` (``src/Utils/generics.ts`` L194).
    """
    return "3EB0" + uuid4().hex[:18].upper()


def _fake_evolution_send_result(
    message_id: str | None = None,
    *,
    text: str = "Olá!",
    to_phone: str = "5511900000000",
) -> dict[str, Any]:
    """Evolution v2's documented send response: the outbound id is ``key.id``.

    Shape of the documented ``POST /message/sendText/{instance}`` 201 example
    (``openapi-v2.json`` in ``evolution-foundation/docs-evolution``) and of
    ``prepareMessage`` in 2.3.7 ``whatsapp.baileys.service.ts`` (L4652), which
    is what the server returns: there is no top-level ``id``. Evolution also
    reports the send as a ``send.message`` webhook whose ``data`` is this same
    object (L2545); ``evolution_webhook`` ignores that event.
    """
    return {
        "key": {
            "remoteJid": f"{to_phone}@s.whatsapp.net",
            "fromMe": True,
            "id": message_id or _baileys_id(),
        },
        "pushName": "Você",
        "status": "PENDING",
        "message": {"conversation": text},
        "messageType": "conversation",
        "messageTimestamp": 1717689097,
        "instanceId": str(uuid4()),
        "source": "web",
    }


def _legacy_top_level_id_send_result(message_id: str) -> dict[str, Any]:
    """A send response that carries the id at the top level only.

    Not what Evolution returns; kept so the top-level ``id`` read that every
    other caller of the extractor relies on stays covered.
    """
    return {"id": message_id}


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


class TestEvolutionReplyIdSharedBetweenDedupLayers:
    """Group 1b Deliverable 6 (Evolution half): both dedup layers receive the
    same outbound id on Evolution's reply path.

    The send fake is Evolution's documented response, whose only id is
    ``key.id``; it is stored verbatim. The ``owner:MSGID`` strip belongs to
    Uazapi's composite top-level ``id`` and is covered by
    ``TestExtractNormalisedSendIdKeyId`` and the Uazapi suite.
    """

    def test_reply_key_id_shared_between_dedup_layers(
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

        reply_id = _baileys_id()
        reply_send = _fake_evolution_send_result(reply_id)

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
                new=AsyncMock(return_value=reply_send),
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
        # The DB layer stores ``key.id`` verbatim ...
        assert stored_ids == [reply_id], stored_ids
        # ... and the in-process / Redis layer receives the same value.
        assert mock_track.call_count == 1
        assert mock_track.call_args.args[0] == {"messageid": reply_id}


class TestExtractNormalisedSendIdKeyId:
    """``_extract_normalised_send_id`` reads Evolution's ``key.id``, last."""

    def test_evolution_text_response_yields_key_id(self) -> None:
        from src.api.routes.webhook import _extract_normalised_send_id

        reply_id = _baileys_id()
        send = _fake_evolution_send_result(reply_id)
        assert _extract_normalised_send_id(send) == reply_id

    def test_evolution_audio_response_yields_key_id(self) -> None:
        from src.api.routes.webhook import _extract_normalised_send_id

        audio_id = _baileys_id()
        send = {
            "key": {
                "remoteJid": "5511900000699@s.whatsapp.net",
                "fromMe": True,
                "id": audio_id,
            },
            "message": {"audioMessage": {"mimetype": "audio/mp4", "ptt": True}},
            "messageTimestamp": "1717689097",
            "status": "PENDING",
        }
        assert _extract_normalised_send_id(send) == audio_id

    def test_top_level_keys_keep_precedence_over_key_id(self) -> None:
        from src.api.routes.webhook import _extract_normalised_send_id

        for top in ("messageid", "MessageID", "messageId", "zapiMessageId", "id"):
            send = {top: "TOP_LEVEL", "key": {"id": "NESTED"}}
            assert _extract_normalised_send_id(send) == "TOP_LEVEL", top

    def test_uazapi_composite_id_still_normalised(self) -> None:
        from src.api.routes.webhook import _extract_normalised_send_id

        send = {"id": "5511999999999:3EB0ABC", "key": {"id": "IGNORED"}}
        assert _extract_normalised_send_id(send) == "3EB0ABC"

    def test_key_id_is_returned_verbatim(self) -> None:
        from src.api.routes.webhook import _extract_normalised_send_id

        assert _extract_normalised_send_id({"key": {"id": "A:B"}}) == "A:B"

    @pytest.mark.parametrize(
        "send",
        [
            None,
            "3EB0",
            ["3EB0"],
            {},
            {"key": None},
            {"key": "3EB0"},
            {"key": ["3EB0"]},
            {"key": {}},
            {"key": {"id": ""}},
            {"key": {"id": 12345}},
            {"key": {"id": None}},
            {"id": "", "key": {"id": None}},
        ],
    )
    def test_unusable_shapes_return_empty_without_raising(self, send: Any) -> None:
        from src.api.routes.webhook import _extract_normalised_send_id

        assert _extract_normalised_send_id(send) == ""


def _evolution_session_row(client: TestClient, tenant_id: UUID, phone: str) -> Any:
    """The phone's session (``id``, ``is_paused``, ``ai_msg_ids``), or ``None``."""
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
            meta = dict(row.session_metadata or {})
            return SimpleNamespace(
                id=row.id,
                is_paused=row.is_paused,
                ai_msg_ids=[entry[0] for entry in meta.get("ai_msg_ids", [])],
            )
        finally:
            await db.close()

    return client._submit(_run())


def _evolution_patient_turn(
    client: TestClient,
    *,
    instance: str,
    phone: str,
    send_results: list[dict[str, Any]],
    clinic_config: dict[str, Any] | None = None,
) -> Any:
    """One patient turn whose agent reply is sent with ``send_results``."""
    payload = _evolution_text_payload(
        instance_name=instance,
        from_phone=phone,
        text_body="Olá, queria marcar uma consulta",
        message_id=f"evo_in_{uuid4().hex[:8]}",
    )
    with (
        patch("src.api.routes.webhook.get_event_service") as get_service,
        patch(
            "src.api.routes.webhook.send_evolution_text",
            new=AsyncMock(side_effect=send_results),
        ),
    ):
        service = MagicMock()
        service._load_clinic_config = AsyncMock(return_value=clinic_config or {})
        service.run_pipeline = AsyncMock(return_value=(MagicMock(), {}, []))
        service.finish_response = AsyncMock(
            return_value=_fake_event_response("Claro! Qual o melhor dia?")
        )
        get_service.return_value = service
        return client.post(WEBHOOK_PATH, json=payload)


class TestEvolutionOwnReplyEcho:
    """Defensive coverage: if an echo of the agent's own Evolution send ever
    arrived as a ``messages.upsert`` with ``fromMe`` true, its ``key.id``
    would be recognised and the session would not be taken over.

    Released Evolution builds do not send that echo. They run Baileys with
    ``emitOwnEvents: false`` (2.3.7 ``whatsapp.baileys.service.ts`` L658; the
    same in every release checked, 1.8.2 to 2.4.0-rc2) and report API sends
    as ``send.message`` (L2545), which ``evolution_webhook`` skips (see
    ``TestEvolutionSendMessageEvent``). The upserts posted here are synthetic;
    they pin the two dedup layers. The real session, the real tracking layers
    and the real ``_handle_clinic_message`` run; only the pipeline and the
    HTTP send are faked.
    """

    def _echo(
        self, client: TestClient, *, instance: str, phone: str, message_id: str
    ) -> Any:
        with patch("src.api.routes.webhook.get_event_service") as get_service:
            get_service.return_value = _mock_pipeline_service()
            return client.post(
                WEBHOOK_PATH,
                json=_evolution_text_payload(
                    instance_name=instance,
                    from_phone=phone,
                    text_body="Claro! Qual o melhor dia?",
                    message_id=message_id,
                    from_me=True,
                ),
            )

    def test_reply_echo_is_recognised_on_the_fast_path(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        phone = "5511900000610"
        reply_id = _baileys_id()

        first = _evolution_patient_turn(
            client,
            instance=instance,
            phone=phone,
            send_results=[_fake_evolution_send_result(reply_id)],
        )
        assert first.status_code == 200, first.text
        assert first.json() == {"ok": True, "replied": True}

        echo = self._echo(client, instance=instance, phone=phone, message_id=reply_id)

        assert echo.status_code == 200, echo.text
        assert echo.json() == {"ok": True, "skipped": "ai_echo"}
        assert _evolution_session_row(client, tenant_id, phone).is_paused is False

    def test_reply_echo_is_recognised_by_the_db_layer_on_another_worker(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        phone = "5511900000611"
        reply_id = _baileys_id()

        first = _evolution_patient_turn(
            client,
            instance=instance,
            phone=phone,
            send_results=[_fake_evolution_send_result(reply_id)],
        )
        assert first.status_code == 200, first.text
        assert _evolution_session_row(client, tenant_id, phone).ai_msg_ids == [reply_id]

        # The echo lands on a worker whose in-process / Redis fast path
        # has never seen the id: only the session's ``ai_msg_ids`` can tell.
        with patch(
            "src.api.routes.webhook._is_ai_sent_message",
            return_value=False,
        ):
            echo = self._echo(
                client, instance=instance, phone=phone, message_id=reply_id
            )

        assert echo.status_code == 200, echo.text
        assert echo.json() == {"ok": True, "skipped": "ai_echo_db"}
        assert _evolution_session_row(client, tenant_id, phone).is_paused is False

    def test_privacy_notice_echo_is_recognised(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        phone = "5511900000612"
        notice_id = _baileys_id()
        reply_id = _baileys_id()

        first = _evolution_patient_turn(
            client,
            instance=instance,
            phone=phone,
            send_results=[
                _fake_evolution_send_result(notice_id),
                _fake_evolution_send_result(reply_id),
            ],
            clinic_config=_lgpd_config(enabled=True),
        )
        assert first.status_code == 200, first.text
        assert first.json() == {"ok": True, "replied": True}

        for mid in (notice_id, reply_id):
            echo = self._echo(client, instance=instance, phone=phone, message_id=mid)
            assert echo.status_code == 200, echo.text
            assert echo.json() == {"ok": True, "skipped": "ai_echo"}, mid
        assert _evolution_session_row(client, tenant_id, phone).is_paused is False

    def test_legacy_top_level_id_echo_is_still_recognised(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        phone = "5511900000613"
        reply_id = "LEGACY_TOP_LEVEL_1"

        first = _evolution_patient_turn(
            client,
            instance=instance,
            phone=phone,
            send_results=[_legacy_top_level_id_send_result(reply_id)],
        )
        assert first.status_code == 200, first.text

        echo = self._echo(client, instance=instance, phone=phone, message_id=reply_id)

        assert echo.status_code == 200, echo.text
        assert echo.json() == {"ok": True, "skipped": "ai_echo"}
        assert _evolution_session_row(client, tenant_id, phone).is_paused is False

    def test_untracked_from_me_message_still_pauses(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        """Control: a ``fromMe`` upsert whose id the agent never sent (a staff
        message typed on the clinic's phone) still takes over, so the tests
        above are not passing because nothing pauses.
        """
        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        phone = "5511900000614"

        first = _evolution_patient_turn(
            client,
            instance=instance,
            phone=phone,
            send_results=[_fake_evolution_send_result(_baileys_id())],
        )
        assert first.status_code == 200, first.text

        echo = self._echo(
            client, instance=instance, phone=phone, message_id=uuid4().hex.upper()
        )

        assert echo.status_code == 200, echo.text
        assert echo.json() == {"ok": True, "action": "human_takeover"}
        assert _evolution_session_row(client, tenant_id, phone).is_paused is True


def _evolution_send_message_event(
    *, instance: str, send_result: dict[str, Any]
) -> dict[str, Any]:
    """The webhook Evolution 2.3.7 posts after an API send.

    ``sendMessageWithTyping`` calls ``sendDataWebhook(Events.SEND_MESSAGE,
    messageRaw)`` (``whatsapp.baileys.service.ts`` L2545), ``SEND_MESSAGE`` is
    ``'send.message'`` (``wa.types.ts`` L17), and the body is built in
    ``webhook.controller.ts`` L93-103. ``data`` is the same ``messageRaw`` the
    HTTP send returns.
    """
    return {
        "event": "send.message",
        "instance": instance,
        "data": send_result,
        "destination": "https://api.example.test/v1/webhook/evolution",
        "date_time": "2026-11-08T12:00:00.000Z",
        "sender": "5511900000000@s.whatsapp.net",
        "server_url": "https://evolution.example.com",
        "apikey": f"evk_{uuid4().hex}",
    }


class TestEvolutionSendMessageEvent:
    """What protects production today: Evolution reports the agent's own API
    sends as ``send.message``, and ``evolution_webhook`` skips every event
    other than ``messages.upsert`` before the ``fromMe`` takeover runs.
    """

    def test_own_send_report_is_skipped_and_never_pauses(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        phone = "5511900000615"
        reply_id = _baileys_id()
        reply = "Claro! Qual o melhor dia?"

        first = _evolution_patient_turn(
            client,
            instance=instance,
            phone=phone,
            send_results=[
                _fake_evolution_send_result(reply_id, text=reply, to_phone=phone)
            ],
        )
        assert first.status_code == 200, first.text
        assert first.json() == {"ok": True, "replied": True}

        # Evolution emits ``send.message`` (L2545) before the HTTP send
        # returns (L2557), so the report can arrive before the reply's id is
        # tracked: cover the tracked id and one that was never tracked.
        for sent_id in (reply_id, _baileys_id()):
            report = _evolution_send_message_event(
                instance=instance,
                send_result=_fake_evolution_send_result(
                    sent_id, text=reply, to_phone=phone
                ),
            )
            with patch("src.api.routes.webhook.get_event_service") as get_service:
                get_service.return_value = _mock_pipeline_service()
                resp = client.post(WEBHOOK_PATH, json=report)

            assert resp.status_code == 200, resp.text
            assert resp.json() == {"ok": True, "skipped": "non_message_event"}

        session = _evolution_session_row(client, tenant_id, phone)
        assert session.is_paused is False
        # The DB dedup layer was not reached: the tracked id was not consumed.
        assert session.ai_msg_ids == [reply_id]


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


# ============================================
# Call auto-reply (``msgCall``) must not read as a human takeover
# ============================================
#
# Evolution 2.2.0 to 2.4.0-rc2 answer a call with the instance's ``msgCall``
# text and then re-emit that send as ``messages.upsert`` with ``type:
# "notify"`` (2.3.7 ``whatsapp.baileys.service.ts:1890-1896``). The message
# is generated on this socket, so Baileys stamps it ``status: PENDING``
# (Baileys 7.0.0-rc.9 ``src/Utils/messages.ts:702``) and Evolution's
# ``prepareMessage`` maps that to the string ``"PENDING"`` (2.3.7 L4663 with
# ``src/utils/renderStatus.ts``). Its id is Baileys' socket id, ``3EB0`` + 18
# uppercase hex (``src/Utils/generics.ts:178-195``). A message typed on
# another device of the clinic's number is decoded from the wire, and Baileys
# stamps every decoded ``fromMe`` message ``SERVER_ACK``
# (``src/Utils/decode-wa-message.ts:215-216``). The one exception is the
# placeholder resend (``src/Utils/process-message.ts:339-358``), which keeps a
# status the phone wrote, so the skip also requires the socket id shape.
# The payloads below copy each shape field by field.


def _evolution_call_auto_reply_payload(
    *, instance_name: str, phone: str, text_body: str, message_id: str
) -> dict:
    """The ``messages.upsert`` Evolution 2.3.7 posts for its ``msgCall`` reply.

    ``data`` is ``prepareMessage(msg)`` of the ``sendMessage`` result:
    ``key`` is Baileys' generated key (``3EB0`` + 18 hex, ``fromMe`` true,
    no participant for a 1:1 chat), ``pushName`` falls back to ``"Você"``
    because a generated message has none, ``status`` is ``"PENDING"``, the
    ``extendedTextMessage`` is folded into ``conversation`` and ``source`` is
    ``getDevice(key.id)``. The envelope is ``webhook.controller.ts:93-102``.
    """
    return {
        "event": "messages.upsert",
        "instance": instance_name,
        "data": {
            "key": {
                "remoteJid": f"{phone}@s.whatsapp.net",
                "fromMe": True,
                "id": message_id,
            },
            "pushName": "Você",
            "status": "PENDING",
            "message": {"conversation": text_body},
            "messageType": "conversation",
            "messageTimestamp": 1762617600,
            "instanceId": str(uuid4()),
            "source": "web",
        },
        "destination": "https://api.example.com/v1/webhook/evolution",
        "date_time": "2026-11-08T13:00:00.000Z",
        "sender": "5511900000999@s.whatsapp.net",
        "server_url": "https://evolution.example.com",
        "apikey": "evk_instance_token",
    }


def _evolution_staff_payload(
    *,
    instance_name: str,
    phone: str,
    text_body: str,
    message_id: str,
    push_name: str = "Clínica Teste",
    source: str = "android",
    status: str | None = "SERVER_ACK",
) -> dict:
    """The ``messages.upsert`` for a message staff typed on another device.

    The default is the clinic's phone (Android, 32 hex id). ``status=None``
    drops the field, the shape of a minimal or older payload.
    """
    data: dict[str, Any] = {
        "key": {
            "remoteJid": f"{phone}@s.whatsapp.net",
            "fromMe": True,
            "id": message_id,
        },
        "pushName": push_name,
        "message": {"conversation": text_body},
        "messageType": "conversation",
        "messageTimestamp": 1762617600,
        "instanceId": str(uuid4()),
        "source": source,
    }
    if status is not None:
        data["status"] = status
    return {
        "event": "messages.upsert",
        "instance": instance_name,
        "data": data,
        "sender": "5511900000999@s.whatsapp.net",
    }


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


def _session_pause_state(
    client: TestClient, session_id: UUID
) -> tuple[bool, datetime | None]:
    """``(is_paused, paused_until)`` of a session, read in a new DB session."""
    from sqlalchemy import select

    from src.models.session import Session as SessionRow
    from src.repositories.database import get_db_session

    async def _run() -> tuple[bool, datetime | None]:
        db = get_db_session()
        try:
            row = (
                await db.execute(
                    select(SessionRow.is_paused, SessionRow.paused_until).where(
                        SessionRow.id == session_id
                    )
                )
            ).one()
            return bool(row[0]), row[1]
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[attr-defined]


_MSG_CALL_TEXT = "Não atendemos ligações por aqui. Por favor, envie sua mensagem."


class TestEvolutionCallAutoReply:
    """A patient's call must not pause the AI through the ``msgCall`` echo."""

    def test_call_auto_reply_does_not_pause_active_session(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        phone = "5511900000600"
        session_id = _create_active_session(client, tenant_id, phone)
        payload = _evolution_call_auto_reply_payload(
            instance_name=instance,
            phone=phone,
            text_body=_MSG_CALL_TEXT,
            message_id="3EB0A1B2C3D4E5F6A7B8C9",
        )

        resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "skipped": "call_auto_reply"}
        assert _session_pause_state(client, session_id) == (False, None)

    def test_staff_message_from_phone_still_pauses(
        self,
        client: TestClient,
        tenant_id: UUID,
    ) -> None:
        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        phone = "5511900000601"
        session_id = _create_active_session(client, tenant_id, phone)
        payload = _evolution_staff_payload(
            instance_name=instance,
            phone=phone,
            text_body="Oi, aqui é a Ana da recepção, vou te ajudar.",
            message_id="A1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D6",
        )

        resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "action": "human_takeover"}
        assert _session_pause_state(client, session_id) == (True, None)

    @pytest.mark.parametrize(
        ("push_name", "source", "status", "text_body"),
        [
            # WhatsApp Web on the clinic's number: same id shape, ``source``,
            # fallback ``pushName`` and text as the auto-reply; only
            # ``status`` tells them apart.
            ("Você", "web", "SERVER_ACK", _MSG_CALL_TEXT),
            # No ``status`` at all: never read as an auto-reply.
            ("Clínica Teste", "android", None, _MSG_CALL_TEXT),
            # The word in the text is not the status field.
            (
                "Clínica Teste",
                "android",
                "SERVER_ACK",
                "Seu pedido aparece como PENDING no sistema, já verifico.",
            ),
        ],
        ids=["whatsapp_web_server_ack", "status_absent", "status_word_in_text"],
    )
    def test_staff_message_resembling_auto_reply_still_pauses(
        self,
        client: TestClient,
        tenant_id: UUID,
        push_name: str,
        source: str,
        status: str | None,
        text_body: str,
    ) -> None:
        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        phone = "5511900000602"
        session_id = _create_active_session(client, tenant_id, phone)
        payload = _evolution_staff_payload(
            instance_name=instance,
            phone=phone,
            text_body=text_body,
            message_id="3EB0F1E2D3C4B5A6978877",
            push_name=push_name,
            source=source,
            status=status,
        )

        resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "action": "human_takeover"}
        assert _session_pause_state(client, session_id) == (True, None)

    @pytest.mark.parametrize(
        "message_id",
        [
            "A1B2C3D4E5F6A7B8C9D0E1F2A3B4C5D6",  # Android, 32 hex
            "3A1B2C3D4E5F6A7B8C9D",  # iOS, ``3A`` + 18
        ],
        ids=["android_id", "ios_id"],
    )
    def test_pending_staff_message_with_phone_id_still_pauses(
        self,
        client: TestClient,
        tenant_id: UUID,
        message_id: str,
    ) -> None:
        """A placeholder resend can carry the phone's ``PENDING``; it pauses."""
        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        phone = "5511900000603"
        session_id = _create_active_session(client, tenant_id, phone)
        payload = _evolution_staff_payload(
            instance_name=instance,
            phone=phone,
            text_body="Oi, aqui é a Ana da recepção, vou te ajudar.",
            message_id=message_id,
            status="PENDING",
        )

        resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "action": "human_takeover"}
        assert _session_pause_state(client, session_id) == (True, None)

    @pytest.mark.parametrize(
        "message_id",
        [
            "BAE5F1E2D3C4B5A6",  # older Baileys id format
            "3eb0a1b2c3d4e5f6a7b8c9",  # lowercase
            "3EB0A1B2C3D4E5F6A7B8C9D0E1",  # ``3EB0`` prefix, 22 hex after it
            "3EB0A1B2C3D4E5F6A7B8",  # ``3EB0`` prefix, 16 hex after it
        ],
        ids=["legacy_bae5", "lowercase", "too_long", "too_short"],
    )
    def test_auto_reply_shape_without_socket_id_is_not_skipped(
        self,
        client: TestClient,
        tenant_id: UUID,
        message_id: str,
    ) -> None:
        instance = f"evo_{uuid4().hex[:8]}"
        _seed_evolution_credentials(client, tenant_id, instance)
        phone = "5511900000604"
        session_id = _create_active_session(client, tenant_id, phone)
        payload = _evolution_call_auto_reply_payload(
            instance_name=instance,
            phone=phone,
            text_body=_MSG_CALL_TEXT,
            message_id=message_id,
        )

        resp = client.post(WEBHOOK_PATH, json=payload)

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True, "action": "human_takeover"}
        assert _session_pause_state(client, session_id) == (True, None)
