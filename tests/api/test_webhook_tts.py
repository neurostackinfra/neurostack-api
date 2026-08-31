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
        input_was_audio=False,
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
        credentials={"tts_enabled": True, "tts_reply_policy": "audio_input"},
        text="Olá",
        language_code="pt-BR",
        channel="test",
        phone="5511",
        input_was_audio=True,
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
    fallback_calls: list[str] = []

    async def synthesize(*args, **kwargs):
        assert args[0] == "Resposta original"
        if failure_stage == "synthesis":
            raise TtsSynthesisError("provider_failed")
        return SynthesizedAudio(b"mp3")

    async def send_audio(_content: bytes, _mime: str) -> dict:
        if failure_stage == "delivery":
            raise RuntimeError("channel_failed")
        return {"messageId": "audio"}

    async def send_text() -> dict:
        fallback_calls.append("Resposta original")
        return {"messageId": "fallback-text"}

    monkeypatch.setattr(webhook, "synthesize_speech", synthesize)
    result, used_audio = await webhook._send_reply_with_optional_tts(
        credentials={"tts_enabled": True, "tts_reply_policy": "audio_input"},
        text="Resposta original",
        language_code="pt-BR",
        channel="test",
        phone="5511",
        input_was_audio=True,
        send_audio=send_audio,
        send_text=send_text,
    )
    assert result == {"messageId": "fallback-text"}
    assert used_audio is False
    assert fallback_calls == ["Resposta original"]


@pytest.mark.asyncio
async def test_force_text_bypasses_tts_when_enabled(monkeypatch) -> None:
    """Deterministic responses (e.g. scheduling links) must never become audio.

    Even when the clinic has TTS enabled, ``force_text=True`` sends the raw
    text so the URL stays clickable in WhatsApp.
    """

    async def unexpected_synthesis(*args, **kwargs):
        raise AssertionError("TTS must not run when force_text=True")

    async def send_audio(_content: bytes, _mime: str) -> dict:
        raise AssertionError("audio sender must not run when force_text=True")

    captured: dict = {}

    async def send_text() -> dict:
        captured["called"] = True
        return {"messageId": "det-text"}

    monkeypatch.setattr(webhook, "synthesize_speech", unexpected_synthesis)
    result, used_audio = await webhook._send_reply_with_optional_tts(
        credentials={"tts_enabled": True, "tts_reply_policy": "all_inputs"},
        text="Agende: https://example.com/book/abc",
        language_code="pt-BR",
        channel="test",
        phone="5511",
        input_was_audio=False,
        send_audio=send_audio,
        send_text=send_text,
        force_text=True,
    )
    assert captured.get("called") is True
    assert result == {"messageId": "det-text"}
    assert used_audio is False


@pytest.mark.asyncio
async def test_force_text_false_preserves_audio_behaviour(monkeypatch) -> None:
    """Regression guard: non-deterministic replies must still speak when TTS is on."""

    async def synthesize(*args, **kwargs):
        return SynthesizedAudio(b"mp3")

    async def send_audio(content: bytes, mime: str) -> dict:
        assert content == b"mp3"
        assert mime == "audio/mpeg"
        return {"messageId": "audio-2"}

    async def send_text() -> dict:
        raise AssertionError("text sender must not run when audio succeeds")

    monkeypatch.setattr(webhook, "synthesize_speech", synthesize)
    result, used_audio = await webhook._send_reply_with_optional_tts(
        credentials={"tts_enabled": True, "tts_reply_policy": "audio_input"},
        text="Resposta normal",
        language_code="pt-BR",
        channel="test",
        phone="5511",
        input_was_audio=True,
        send_audio=send_audio,
        send_text=send_text,
        force_text=False,
    )
    assert result == {"messageId": "audio-2"}
    assert used_audio is True


@pytest.mark.asyncio
async def test_force_text_with_tts_disabled_still_sends_text(monkeypatch) -> None:
    """When TTS is off the branch is the same regardless of force_text."""

    async def unexpected_synthesis(*args, **kwargs):
        raise AssertionError("TTS must not run while disabled")

    async def send_audio(_content: bytes, _mime: str) -> dict:
        raise AssertionError("audio sender must not run while disabled")

    async def send_text() -> dict:
        return {"messageId": "text-off"}

    monkeypatch.setattr(webhook, "synthesize_speech", unexpected_synthesis)
    result, used_audio = await webhook._send_reply_with_optional_tts(
        credentials={"tts_enabled": False},
        text="https://example.com/book/xyz",
        language_code="pt-BR",
        channel="test",
        phone="5511",
        input_was_audio=True,
        send_audio=send_audio,
        send_text=send_text,
        force_text=True,
    )
    assert result == {"messageId": "text-off"}
    assert used_audio is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tts_enabled", "policy", "input_was_audio", "expected_audio"),
    [
        (False, "all_inputs", False, False),
        (True, None, False, False),
        (True, None, True, True),
        (True, "audio_input", False, False),
        (True, "audio_input", True, True),
        (True, "all_inputs", False, True),
        (True, "all_inputs", True, True),
        (True, "invalid_legacy_value", True, False),
    ],
)
async def test_tts_master_policy_origin_matrix(
    monkeypatch,
    tts_enabled: bool,
    policy: str | None,
    input_was_audio: bool,
    expected_audio: bool,
) -> None:
    calls: list[str] = []

    async def synthesize(*args, **kwargs):
        calls.append("synthesize")
        return SynthesizedAudio(b"mp3")

    async def send_audio(_content: bytes, _mime: str) -> dict:
        calls.append("audio")
        return {"messageId": "audio"}

    async def send_text() -> dict:
        calls.append("text")
        return {"messageId": "text"}

    credentials: dict[str, object] = {"tts_enabled": tts_enabled}
    if policy is not None:
        credentials["tts_reply_policy"] = policy

    monkeypatch.setattr(webhook, "synthesize_speech", synthesize)
    result, used_audio = await webhook._send_reply_with_optional_tts(
        credentials=credentials,
        text="Resposta",
        language_code="pt-BR",
        channel="test",
        phone="5511",
        input_was_audio=input_was_audio,
        send_audio=send_audio,
        send_text=send_text,
    )

    assert used_audio is expected_audio
    if expected_audio:
        assert result == {"messageId": "audio"}
        assert calls == ["synthesize", "audio"]
    else:
        assert result == {"messageId": "text"}
        assert calls == ["text"]
