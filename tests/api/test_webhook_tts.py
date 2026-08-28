"""Tests for optional voice delivery and its text safety fallback."""

from __future__ import annotations

import pytest

from src.api.routes import webhook
from src.services.tts_service import SynthesizedAudio, TtsSynthesisError


@pytest.mark.asyncio
async def test_disabled_tts_sends_text_without_synthesis(monkeypatch) -> None:
    async def unexpected_synthesis(*args, **kwargs):
        raise AssertionError("TTS must not run while disabled")

    async def send_audio(_content: bytes, _mime: str) -> dict:
        raise AssertionError("audio sender must not run while disabled")

    async def send_text() -> dict:
        return {"messageId": "text-1"}

    monkeypatch.setattr(webhook, "synthesize_speech", unexpected_synthesis)
    result, used_audio = await webhook._send_reply_with_optional_tts(
        credentials={"tts_enabled": False},
        text="Olá",
        language_code="pt-BR",
        channel="test",
        phone="5511",
        send_audio=send_audio,
        send_text=send_text,
    )
    assert result == {"messageId": "text-1"}
    assert used_audio is False


@pytest.mark.asyncio
async def test_enabled_tts_sends_synthesized_audio(monkeypatch) -> None:
    async def synthesize(*args, **kwargs):
        return SynthesizedAudio(b"mp3")

    async def send_audio(content: bytes, mime: str) -> dict:
        assert content == b"mp3"
        assert mime == "audio/mpeg"
        return {"messageId": "audio-1"}

    async def send_text() -> dict:
        raise AssertionError("text fallback must not run on success")

    monkeypatch.setattr(webhook, "synthesize_speech", synthesize)
    result, used_audio = await webhook._send_reply_with_optional_tts(
        credentials={"tts_enabled": True},
        text="Olá",
        language_code="pt-BR",
        channel="test",
        phone="5511",
        send_audio=send_audio,
        send_text=send_text,
    )
    assert result == {"messageId": "audio-1"}
    assert used_audio is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["synthesis", "delivery"])
async def test_any_tts_failure_falls_back_to_original_text(
    monkeypatch, failure_stage: str
) -> None:
    async def synthesize(*args, **kwargs):
        if failure_stage == "synthesis":
            raise TtsSynthesisError("provider_failed")
        return SynthesizedAudio(b"mp3")

    async def send_audio(_content: bytes, _mime: str) -> dict:
        if failure_stage == "delivery":
            raise RuntimeError("channel_failed")
        return {"messageId": "audio"}

    async def send_text() -> dict:
        return {"messageId": "fallback-text"}

    monkeypatch.setattr(webhook, "synthesize_speech", synthesize)
    result, used_audio = await webhook._send_reply_with_optional_tts(
        credentials={"tts_enabled": True},
        text="Resposta original",
        language_code="pt-BR",
        channel="test",
        phone="5511",
        send_audio=send_audio,
        send_text=send_text,
    )
    assert result == {"messageId": "fallback-text"}
    assert used_audio is False
