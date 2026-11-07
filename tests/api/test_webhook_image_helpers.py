"""Unit tests for the channel-agnostic image and file helpers in
:mod:`src.api.routes.webhook` (whatsapp-image-understanding, Group 1).

The uazapi, Z-API and Meta handlers (Groups 2-4) call these helpers; here
they are proven in isolation:

- ``_describe_image_chain`` — capability gate before any call, local size and
  MIME refusals, the empty/marker failure, the composed message on success,
  and one ``image_description_attempt`` event per outcome it decides;
- ``_compose_image_message`` — Decision 10, pt and en, with and without
  caption;
- ``_emit_image_description_attempt`` — the event fields, and that neither
  the image bytes nor the description ever reach a log record;
- ``_pending_file_review_active`` / ``_file_ack_needed`` — Decision 4's
  two-hour window, with legacy (no timestamp), malformed and expired flags
  counting as absent and no write inside the predicate;
- the file-acknowledgement copy of Decision 6.
"""

from __future__ import annotations

import base64
import inspect
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import httpx
import pytest

from src.api.routes import webhook
from src.api.routes.webhook import (
    FILE_REVIEW_WINDOW,
    IMAGE_CHAIN_FAILED,
    IMAGE_CHAIN_NO_CAPABILITY,
    IMAGE_CHAIN_OK,
    IMAGE_DESCRIPTION_OUTCOMES,
    IMAGE_MAX_BYTES,
    _build_meta_file_ack_message,
    _compose_image_message,
    _describe_image_chain,
    _emit_image_description_attempt,
    _file_ack_needed,
    _image_download_outcome,
    _pending_file_review_active,
)
from src.services.multimodal_service import (
    IMAGE_NO_CONTENT_MARKER,
    MultimodalImageError,
)
from src.services.safe_egress import UnsafeEgressError

_LOGGER = "src.api.routes.webhook"
_EVENT = "image_description_attempt"
IMAGE = b"\xff\xd8\xff\xe0\x00\x10JFIF-patient-photo"
DESCRIPTION = "Uma receita manuscrita com o texto 'Amoxicilina 500mg'."
OPERATIONAL: dict[str, Any] = {
    "multimodal_provider": "openrouter",
    "multimodal_api_url": "https://openrouter.ai/api/v1",
    "multimodal_api_key": "sk-or-v1-super-secret",
    "multimodal_model_name": "google/gemini-2.5-flash",
}
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def _events(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if getattr(r, "event", None) == _EVENT]


def _event_fields(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: getattr(record, key)
        for key in ("channel", "outcome", "mime", "size_bytes", "latency_ms")
    }


async def _chain(
    image_bytes: bytes | None = IMAGE,
    credentials: dict[str, Any] | None = None,
    *,
    mime_type: str = "image/jpeg",
    caption: str | None = None,
    agent_language: str = "pt-BR",
    channel: str = "uazapi",
) -> tuple[str, str]:
    return await _describe_image_chain(
        image_bytes,
        OPERATIONAL if credentials is None else credentials,
        mime_type=mime_type,
        caption=caption,
        agent_language=agent_language,
        channel=channel,
    )


def _adapter(**kwargs: Any) -> Any:
    return patch(
        "src.api.routes.webhook._multimodal_describe_image",
        new=AsyncMock(**kwargs),
    )


# ---------------------------------------------------------------------------
# Constants the handlers rely on
# ---------------------------------------------------------------------------


class TestSharedConstants:
    def test_image_cap_is_the_credit_media_image_cap(self) -> None:
        from src.services.media_assets import MEDIA_IMAGE_MAX_BYTES

        assert IMAGE_MAX_BYTES == MEDIA_IMAGE_MAX_BYTES == 5_000_000

    def test_safe_download_image_is_importable_from_the_webhook_module(self) -> None:
        from src.services import safe_egress

        assert webhook.safe_download_image is safe_egress.safe_download_image

    def test_chain_reasons_are_distinct_and_ok_is_falsy(self) -> None:
        assert IMAGE_CHAIN_OK == ""
        assert IMAGE_CHAIN_NO_CAPABILITY and IMAGE_CHAIN_FAILED
        assert IMAGE_CHAIN_NO_CAPABILITY != IMAGE_CHAIN_FAILED

    def test_outcomes_are_exactly_the_wish_set(self) -> None:
        assert IMAGE_DESCRIPTION_OUTCOMES == frozenset(
            {
                "described",
                "no_capability",
                "too_large",
                "unsupported_mime",
                "view_once",
                "download_failed",
                "provider_failed",
                "no_content",
            }
        )

    def test_review_window_is_two_hours(self) -> None:
        assert FILE_REVIEW_WINDOW == timedelta(hours=2)


# ---------------------------------------------------------------------------
# _describe_image_chain
# ---------------------------------------------------------------------------


class TestDescribeImageChainCapability:
    @pytest.mark.parametrize(
        "credentials",
        [
            {},
            {**OPERATIONAL, "multimodal_provider": "openai"},
            {**OPERATIONAL, "multimodal_provider": "groq"},
            {**OPERATIONAL, "multimodal_api_url": ""},
            {**OPERATIONAL, "multimodal_api_key": ""},
            {**OPERATIONAL, "multimodal_model_name": None},
        ],
    )
    async def test_incomplete_credentials_never_call_the_provider(
        self, credentials: dict[str, Any], caplog: pytest.LogCaptureFixture
    ) -> None:
        with _adapter(return_value=DESCRIPTION) as adapter:
            with caplog.at_level(logging.INFO, logger=_LOGGER):
                result = await _chain(credentials=credentials)

        assert result == ("", IMAGE_CHAIN_NO_CAPABILITY)
        assert adapter.await_count == 0
        [event] = _events(caplog)
        assert _event_fields(event) == {
            "channel": "uazapi",
            "outcome": "no_capability",
            "mime": "image/jpeg",
            "size_bytes": len(IMAGE),
            "latency_ms": None,
        }

    async def test_no_capability_wins_even_without_bytes(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with _adapter(return_value=DESCRIPTION) as adapter:
            with caplog.at_level(logging.INFO, logger=_LOGGER):
                result = await _chain(None, credentials={})
        assert result == ("", IMAGE_CHAIN_NO_CAPABILITY)
        assert adapter.await_count == 0
        [event] = _events(caplog)
        assert event.outcome == "no_capability"
        assert event.size_bytes is None


class TestDescribeImageChainLocalRefusals:
    @pytest.mark.parametrize("image_bytes", [None, b""])
    async def test_missing_bytes_fail_without_a_call(
        self, image_bytes: bytes | None, caplog: pytest.LogCaptureFixture
    ) -> None:
        with _adapter(return_value=DESCRIPTION) as adapter:
            with caplog.at_level(logging.INFO, logger=_LOGGER):
                result = await _chain(image_bytes)
        assert result == ("", IMAGE_CHAIN_FAILED)
        assert adapter.await_count == 0
        [event] = _events(caplog)
        assert event.outcome == "download_failed"
        assert event.latency_ms is None

    async def test_oversized_image_never_reaches_the_provider(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        oversized = b"\x00" * (IMAGE_MAX_BYTES + 1)
        with _adapter(return_value=DESCRIPTION) as adapter:
            with caplog.at_level(logging.INFO, logger=_LOGGER):
                result = await _chain(oversized)
        assert result == ("", IMAGE_CHAIN_FAILED)
        assert adapter.await_count == 0
        [event] = _events(caplog)
        assert event.outcome == "too_large"
        assert event.size_bytes == IMAGE_MAX_BYTES + 1

    async def test_image_exactly_at_the_cap_is_described(self) -> None:
        with _adapter(return_value=DESCRIPTION) as adapter:
            text, reason = await _chain(b"\x00" * IMAGE_MAX_BYTES)
        assert reason == IMAGE_CHAIN_OK
        assert adapter.await_count == 1

    @pytest.mark.parametrize(
        "mime_type", ["image/gif", "image/heic", "application/pdf", ""]
    )
    async def test_unsupported_mime_never_reaches_the_provider(
        self, mime_type: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        with _adapter(return_value=DESCRIPTION) as adapter:
            with caplog.at_level(logging.INFO, logger=_LOGGER):
                result = await _chain(mime_type=mime_type)
        assert result == ("", IMAGE_CHAIN_FAILED)
        assert adapter.await_count == 0
        [event] = _events(caplog)
        assert event.outcome == "unsupported_mime"


class TestDescribeImageChainSuccess:
    async def test_success_returns_the_composed_message(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with _adapter(return_value=f"  {DESCRIPTION}  ") as adapter:
            with caplog.at_level(logging.INFO, logger=_LOGGER):
                result = await _chain(caption="Olha essa receita", channel="zapi")

        assert result == (
            f"Olha essa receita\n\n[Imagem enviada pelo paciente: {DESCRIPTION}]",
            IMAGE_CHAIN_OK,
        )
        adapter.assert_awaited_once_with(IMAGE, "image/jpeg", OPERATIONAL, "pt-BR")
        [event] = _events(caplog)
        fields = _event_fields(event)
        assert fields["channel"] == "zapi"
        assert fields["outcome"] == "described"
        assert fields["mime"] == "image/jpeg"
        assert fields["size_bytes"] == len(IMAGE)
        assert isinstance(fields["latency_ms"], int) and fields["latency_ms"] >= 0

    async def test_english_agent_without_caption(self) -> None:
        with _adapter(return_value="A handwritten prescription."):
            result = await _chain(agent_language="en", mime_type="IMAGE/PNG; q=1")
        assert result == (
            "[Image sent by the patient: A handwritten prescription.]",
            IMAGE_CHAIN_OK,
        )

    async def test_the_adapter_receives_the_tenant_bundle(self) -> None:
        target = "src.services.multimodal_service.describe_image_multimodal"
        with patch(target, new=AsyncMock(return_value=DESCRIPTION)) as adapter:
            await _chain(mime_type="image/webp", agent_language="en")
        adapter.assert_awaited_once_with(
            api_key="sk-or-v1-super-secret",
            image_bytes=IMAGE,
            mimetype="image/webp",
            model="google/gemini-2.5-flash",
            base_url="https://openrouter.ai/api/v1",
            agent_language="en",
        )


class TestDescribeImageChainFailures:
    @pytest.mark.parametrize(
        "returned",
        [
            "",
            "   \n",
            IMAGE_NO_CONTENT_MARKER,
            f" {IMAGE_NO_CONTENT_MARKER} ",
            f"Foto escura {IMAGE_NO_CONTENT_MARKER}",
        ],
    )
    async def test_empty_or_marker_is_a_failure(
        self, returned: str, caplog: pytest.LogCaptureFixture
    ) -> None:
        with _adapter(return_value=returned):
            with caplog.at_level(logging.INFO, logger=_LOGGER):
                result = await _chain()
        assert result == ("", IMAGE_CHAIN_FAILED)
        [event] = _events(caplog)
        assert event.outcome == "no_content"
        assert isinstance(event.latency_ms, int)

    @pytest.mark.parametrize(
        ("error", "outcome"),
        [
            (MultimodalImageError("no_image_content"), "no_content"),
            (MultimodalImageError("unsupported_image_mime"), "unsupported_mime"),
            (
                MultimodalImageError("provider_http_error", status_code=402),
                "provider_failed",
            ),
            (MultimodalImageError("provider_transport_error"), "provider_failed"),
            (MultimodalImageError("invalid_provider_response"), "provider_failed"),
            (
                MultimodalImageError("provider_not_multimodal_capable"),
                "provider_failed",
            ),
            (UnsafeEgressError("unsafe_api_url"), "provider_failed"),
            (httpx.ConnectError("boom"), "provider_failed"),
            (RuntimeError("unexpected"), "provider_failed"),
        ],
    )
    async def test_adapter_errors_are_failures_with_an_outcome(
        self,
        error: Exception,
        outcome: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        with _adapter(side_effect=error):
            with caplog.at_level(logging.INFO, logger=_LOGGER):
                result = await _chain()
        assert result == ("", IMAGE_CHAIN_FAILED)
        [event] = _events(caplog)
        assert event.outcome == outcome
        assert isinstance(event.latency_ms, int)

    async def test_no_log_ever_carries_the_bytes_or_the_description(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        encoded = base64.b64encode(IMAGE).decode("ascii")
        with caplog.at_level(logging.DEBUG):
            with _adapter(return_value=DESCRIPTION):
                await _chain(caption="legenda")
            with _adapter(return_value=f"{DESCRIPTION} {IMAGE_NO_CONTENT_MARKER}"):
                await _chain()
            with _adapter(side_effect=MultimodalImageError("provider_http_error")):
                await _chain()
            with _adapter(side_effect=RuntimeError("unexpected")):
                await _chain()

        assert len(_events(caplog)) == 4
        for record in caplog.records:
            rendered = record.getMessage() + repr(record.__dict__)
            assert DESCRIPTION not in rendered
            assert encoded not in rendered
            assert repr(IMAGE) not in rendered
            assert "sk-or-v1-super-secret" not in rendered


# ---------------------------------------------------------------------------
# _compose_image_message — Decision 10
# ---------------------------------------------------------------------------


class TestComposeImageMessage:
    @pytest.mark.parametrize(
        ("caption", "language", "expected"),
        [
            (None, "pt-BR", "[Imagem enviada pelo paciente: um gato]"),
            ("", "pt-BR", "[Imagem enviada pelo paciente: um gato]"),
            ("   ", "pt-BR", "[Imagem enviada pelo paciente: um gato]"),
            (
                "Meu gato",
                "pt-BR",
                "Meu gato\n\n[Imagem enviada pelo paciente: um gato]",
            ),
            (None, "en", "[Image sent by the patient: um gato]"),
            ("My cat", "en", "My cat\n\n[Image sent by the patient: um gato]"),
            (
                "  Meu gato \n",
                "es",
                "Meu gato\n\n[Imagem enviada pelo paciente: um gato]",
            ),
        ],
    )
    def test_composition(
        self, caption: str | None, language: str, expected: str
    ) -> None:
        assert _compose_image_message(caption, "  um gato \n", language) == expected


# ---------------------------------------------------------------------------
# _emit_image_description_attempt
# ---------------------------------------------------------------------------


class TestEmitImageDescriptionAttempt:
    def test_signature_has_no_slot_for_bytes_or_description(self) -> None:
        parameters = inspect.signature(_emit_image_description_attempt).parameters
        assert set(parameters) == {
            "channel",
            "outcome",
            "mime",
            "size_bytes",
            "latency_ms",
        }
        assert all(
            p.kind is inspect.Parameter.KEYWORD_ONLY for p in parameters.values()
        )

    def test_event_fields_and_message(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            _emit_image_description_attempt(
                channel="meta",
                outcome="described",
                mime="image/png",
                size_bytes=1234,
                latency_ms=870,
            )
        [record] = _events(caplog)
        assert record.levelno == logging.INFO
        assert _event_fields(record) == {
            "channel": "meta",
            "outcome": "described",
            "mime": "image/png",
            "size_bytes": 1234,
            "latency_ms": 870,
        }
        assert record.getMessage() == (
            "image_description_attempt channel=meta outcome=described "
            "mime=image/png size_bytes=1234 latency_ms=870"
        )

    def test_nullable_fields(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            _emit_image_description_attempt(
                channel="uazapi",
                outcome="view_once",
                mime=None,
                size_bytes=None,
                latency_ms=None,
            )
        [record] = _events(caplog)
        assert record.mime is None
        assert record.size_bytes is None
        assert record.latency_ms is None

    @pytest.mark.parametrize(
        ("raw", "logged"),
        [
            ("IMAGE/JPEG; charset=binary", "image/jpeg"),
            ("  image/heic ", "image/heic"),
            ("", None),
            ("image/jpeg\ninjected=1", "invalid"),
            ("not a mime", "invalid"),
            ("image/" + "x" * 200, "invalid"),
        ],
    )
    def test_mime_is_normalized_and_bounded(
        self, raw: str, logged: str | None, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            _emit_image_description_attempt(
                channel="zapi",
                outcome="unsupported_mime",
                mime=raw,
                size_bytes=10,
                latency_ms=None,
            )
        [record] = _events(caplog)
        assert record.mime == logged
        assert "\n" not in record.getMessage()

    def test_unknown_outcome_is_not_emitted(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.INFO, logger=_LOGGER):
            _emit_image_description_attempt(
                channel="zapi",
                outcome="exploded",  # type: ignore[arg-type]
                mime="image/jpeg",
                size_bytes=1,
                latency_ms=1,
            )
        assert _events(caplog) == []
        assert any(r.levelno == logging.ERROR for r in caplog.records)


# ---------------------------------------------------------------------------
# Pending file review — Decision 4
# ---------------------------------------------------------------------------


class _Scalars:
    def __init__(self, value: Any) -> None:
        self._value = value

    def first(self) -> Any:
        return self._value


class _Result:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalars(self) -> _Scalars:
        return _Scalars(self._value)


class _ReadOnlyDb:
    """Answers every SELECT with ``metadata`` and records any write attempt."""

    def __init__(self, metadata: Any) -> None:
        self.metadata = metadata
        self.statements: list[Any] = []
        self.writes: list[str] = []

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        return _Result(self.metadata)

    async def flush(self) -> None:
        self.writes.append("flush")

    async def commit(self) -> None:
        self.writes.append("commit")

    def add(self, _obj: Any) -> None:
        self.writes.append("add")


def _flag(at: Any, flag: Any = True) -> dict[str, Any]:
    return {
        "phone": "5511900000000",
        "pending_file_review": flag,
        "pending_file_review_at": at,
    }


class TestPendingFileReviewActive:
    @pytest.mark.parametrize(
        ("metadata", "active"),
        [
            # Fresh: set just now, one hour ago, one second before the window.
            (_flag(NOW.isoformat()), True),
            (_flag((NOW - timedelta(hours=1)).isoformat()), True),
            (
                _flag((NOW - FILE_REVIEW_WINDOW + timedelta(seconds=1)).isoformat()),
                True,
            ),
            (_flag("2026-11-07T11:30:00Z"), True),
            (_flag("2026-11-07T08:30:00-03:00"), True),  # 11:30 UTC
            # Expired: exactly at the window and beyond.
            (_flag((NOW - FILE_REVIEW_WINDOW).isoformat()), False),
            (_flag((NOW - timedelta(days=30)).isoformat()), False),
            # Legacy row: flag without timestamp.
            ({"pending_file_review": True}, False),
            # Malformed timestamp.
            (_flag(None), False),
            (_flag(""), False),
            (_flag("yesterday"), False),
            (_flag(1730980800), False),
            (_flag("2026-11-07T11:30:00"), False),  # naive: not UTC ISO
            (_flag((NOW + timedelta(minutes=5)).isoformat()), False),  # future
            # Malformed or absent flag.
            (_flag(NOW.isoformat(), flag="true"), False),
            (_flag(NOW.isoformat(), flag=1), False),
            (_flag(NOW.isoformat(), flag=False), False),
            ({"pending_file_review_at": NOW.isoformat()}, False),
            ({}, False),
            (None, False),
            ("not-a-dict", False),
        ],
    )
    async def test_window(self, metadata: Any, active: bool) -> None:
        db = _ReadOnlyDb(metadata)
        result = await _pending_file_review_active(uuid4(), db, now=NOW)
        assert result is active
        assert db.writes == []
        assert len(db.statements) == 1

    async def test_file_ack_needed_is_the_negation(self) -> None:
        fresh = _ReadOnlyDb(_flag(datetime.now(timezone.utc).isoformat()))
        legacy = _ReadOnlyDb({"pending_file_review": True})
        expired = _ReadOnlyDb(
            _flag((datetime.now(timezone.utc) - timedelta(hours=3)).isoformat())
        )
        assert await _file_ack_needed(uuid4(), fresh) is False
        assert await _file_ack_needed(uuid4(), legacy) is True
        assert await _file_ack_needed(uuid4(), expired) is True
        assert fresh.writes == legacy.writes == expired.writes == []


def _new_session(client: Any) -> UUID:
    from src.repositories.database import get_db_session

    async def _run() -> UUID:
        db = get_db_session()
        try:
            session_id, _ = await webhook._get_or_create_session(
                uuid4(), "5511900004242", db
            )
            await db.commit()
            return session_id
        finally:
            await db.close()

    return client._submit(_run())  # type: ignore[no-any-return]


def _in_db(client: Any, operation: Any) -> Any:
    from src.repositories.database import get_db_session

    async def _run() -> Any:
        db = get_db_session()
        try:
            result = await operation(db)
            await db.commit()
            return result
        finally:
            await db.close()

    return client._submit(_run())


def _metadata(client: Any, session_id: UUID) -> dict[str, Any]:
    from sqlalchemy import select

    from src.models.session import Session as SessionRow

    async def read(db: Any) -> dict[str, Any]:
        row = await db.execute(
            select(SessionRow.session_metadata).where(SessionRow.id == session_id)
        )
        return dict(row.scalars().first() or {})

    return _in_db(client, read)  # type: ignore[no-any-return]


class TestPendingFileReviewRoundTrip:
    def test_set_writes_both_keys_and_expires_after_two_hours(
        self, client: Any
    ) -> None:
        session_id = _new_session(client)

        async def set_review(db: Any) -> None:
            await webhook._meta_set_pending_file_review(session_id, db, now=NOW)

        _in_db(client, set_review)
        metadata = _metadata(client, session_id)
        assert metadata["pending_file_review"] is True
        stamped = datetime.fromisoformat(metadata["pending_file_review_at"])
        assert stamped == NOW and stamped.utcoffset() == timedelta(0)
        assert metadata["phone"] == "5511900004242"  # other keys preserved

        def active_at(moment: datetime) -> bool:
            async def check(db: Any) -> bool:
                return await _pending_file_review_active(session_id, db, now=moment)

            return _in_db(client, check)  # type: ignore[no-any-return]

        assert active_at(NOW + timedelta(minutes=119)) is True
        assert active_at(NOW + FILE_REVIEW_WINDOW) is False
        assert active_at(NOW + timedelta(days=1)) is False
        # The predicate never rewrote the row.
        assert _metadata(client, session_id) == metadata

    def test_set_overwrites_and_clear_removes_both_keys(self, client: Any) -> None:
        session_id = _new_session(client)
        later = NOW + timedelta(hours=5)

        async def set_at(db: Any, moment: datetime) -> None:
            await webhook._meta_set_pending_file_review(session_id, db, now=moment)

        _in_db(client, lambda db: set_at(db, NOW))
        _in_db(client, lambda db: set_at(db, later))
        metadata = _metadata(client, session_id)
        assert datetime.fromisoformat(metadata["pending_file_review_at"]) == later

        async def clear(db: Any) -> None:
            await webhook._meta_clear_pending_file_review(session_id, db)

        _in_db(client, clear)
        metadata = _metadata(client, session_id)
        assert "pending_file_review" not in metadata
        assert "pending_file_review_at" not in metadata
        assert metadata["phone"] == "5511900004242"

    def test_default_timestamp_is_the_current_utc_time(self, client: Any) -> None:
        session_id = _new_session(client)
        before = datetime.now(timezone.utc)

        async def set_review(db: Any) -> None:
            await webhook._meta_set_pending_file_review(session_id, db)

        _in_db(client, set_review)
        after = datetime.now(timezone.utc)
        stamped = datetime.fromisoformat(
            _metadata(client, session_id)["pending_file_review_at"]
        )
        assert before <= stamped <= after

        async def needed(db: Any) -> bool:
            return await _file_ack_needed(session_id, db)

        assert _in_db(client, needed) is False

    def test_legacy_flag_without_timestamp_counts_as_absent(self, client: Any) -> None:
        from sqlalchemy import update

        from src.models.session import Session as SessionRow

        session_id = _new_session(client)

        async def write_legacy(db: Any) -> None:
            await db.execute(
                update(SessionRow)
                .where(SessionRow.id == session_id)
                .values(session_metadata={"pending_file_review": True})
            )

        _in_db(client, write_legacy)

        async def needed(db: Any) -> bool:
            return await _file_ack_needed(session_id, db)

        assert _in_db(client, needed) is True
        # No write inside the predicate: the legacy row is left as it was.
        assert _metadata(client, session_id) == {"pending_file_review": True}

        # Clearing a legacy row removes the flag even with no timestamp.
        async def clear(db: Any) -> None:
            await webhook._meta_clear_pending_file_review(session_id, db)

        _in_db(client, clear)
        assert _metadata(client, session_id) == {}


# ---------------------------------------------------------------------------
# File acknowledgement copy — Decision 6
# ---------------------------------------------------------------------------

_DECISION_6_PT = (
    "Recebi seu arquivo! Mas, no momento, não consigo acessar o conteúdo por aqui."
    "\n\nVocê poderia me contar um pouco mais sobre o arquivo e o que precisa? "
    "Assim, consigo entender melhor a situação e tentar te orientar.\n\nSe "
    "preferir, também posso encaminhar seu caso para nossa equipe avaliar com "
    "mais detalhes."
)
_DECISION_6_EN = (
    "I received your file! But right now I can't access its contents here.\n\n"
    "Could you tell me a bit more about the file and what you need? That way I "
    "can better understand the situation and try to help.\n\nIf you prefer, I "
    "can also forward your case to our team to review in more detail."
)


class TestFileAckCopy:
    def test_copy_is_exactly_decision_6(self) -> None:
        assert webhook._FILE_ACK_MSG_PT == _DECISION_6_PT
        assert webhook._FILE_ACK_MSG_EN == _DECISION_6_EN

    @pytest.mark.parametrize(
        ("language", "expected"),
        [("pt-BR", _DECISION_6_PT), ("en", _DECISION_6_EN), ("", _DECISION_6_PT)],
    )
    def test_builder_picks_the_agent_language(
        self, language: str, expected: str
    ) -> None:
        assert _build_meta_file_ack_message(language) == expected

    def test_builder_takes_only_the_language(self) -> None:
        parameters = inspect.signature(_build_meta_file_ack_message).parameters
        assert list(parameters) == ["agent_language"]
        assert not hasattr(webhook, "_meta_load_lead_name")

    def test_forwarded_copy_is_unchanged(self) -> None:
        assert webhook._FILE_FORWARDED_MSG_PT == (
            "Perfeito! Já encaminhei seu arquivo para a nossa equipe. Em breve "
            "alguém entra em contato com você. 🙂"
        )
        assert webhook._FILE_FORWARDED_MSG_EN == (
            "Got it! I've forwarded your file to our team. Someone will follow up "
            "with you shortly. 🙂"
        )


class TestImageDownloadOutcome:
    @pytest.mark.parametrize(
        ("code", "outcome"),
        [
            ("image_too_large", "too_large"),
            ("unsupported_image_mime", "unsupported_mime"),
            ("unsafe_api_url", "download_failed"),
            ("redirect_forbidden", "download_failed"),
            ("image_download_failed", "download_failed"),
            ("something_new", "download_failed"),
        ],
    )
    def test_maps_download_error_code_to_event_outcome(
        self, code: str, outcome: str
    ) -> None:
        result = _image_download_outcome(code)
        assert result == outcome
        assert result in IMAGE_DESCRIPTION_OUTCOMES
