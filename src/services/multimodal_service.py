"""NeuroStack - Multimodal audio transcription and image description service.

Second step of the audio fallback chain: when the dedicated STT provider
fails, the raw audio bytes are handed to a multimodal chat model through the
OpenAI ``chat/completions`` shape, using ``input_audio`` content blocks.

The same tenant bundle also describes images a patient sends on WhatsApp
(:func:`describe_image_multimodal`, whatsapp-image-understanding): one
``image_url`` content block carrying a base64 ``data:`` URL, a neutral,
non-diagnostic prompt in the agent's language, and egress pinned to
OpenRouter only. The description is text for the main LLM, never a reply.

Deliberately *not* part of :mod:`src.services.groq_service`: that module is
named per-provider around ``/audio/transcriptions``, while this one has to
serve OpenRouter, OpenAI and (in principle) Groq through a different
endpoint, a different body shape and its own egress policies.

Provider compatibility is not symmetric and was established against the live
endpoints (see
``.genie/wishes/fallback-texto/evidence/multimodal-format-probe.md``):

- **OpenRouter** accepts OGG/Opus — the container WhatsApp actually delivers
  — and transcribes it verbatim. It is the only provider proven operational.
- **OpenAI** only accepts ``wav``/``mp3`` in ``input_audio.format``; OGG is
  rejected by the schema enum, and OGG bytes mislabelled as ``wav`` are
  rejected as ``unsupported_format``. Usable only for already-WAV/MP3 audio.
- **Groq** rejects the ``input_audio`` content block itself: its
  chat-completions schema only allows ``text``/``image_url``/``document``
  parts, so no MIME mapping can rescue it.

All outbound HTTP flows through :mod:`src.services.safe_egress` with a
pinned-host policy per provider. There is no ``outros`` fallback: an
unrecognised base URL is refused rather than downgraded to custom-public.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from src.services.safe_egress import (
    IMAGE_MIME_ALLOWLIST,
    MULTIMODAL_GROQ_HOSTS,
    MULTIMODAL_OPENAI_HOSTS,
    MULTIMODAL_OPENROUTER_HOSTS,
    UnsafeEgressError,
    parse_public_https_url,
    safe_async_client,
)

OPENROUTER_API_BASE = "https://openrouter.ai/api/v1"
OPENAI_API_BASE = "https://api.openai.com/v1"
GROQ_API_BASE = "https://api.groq.com/openai/v1"

DEFAULT_MULTIMODAL_MODEL = "google/gemini-2.5-flash"

#: Marker the prompt asks for when the audio carries no intelligible speech,
#: so a caller can tell "nothing was said" from "transcription failed". The
#: marker is intentionally language-neutral (no PT or EN text) so the audio
#: and text turns share the same downstream language-decision machine.
NO_SPEECH_MARKER = "[[NO_SPEECH]]"

#: Instruction sent alongside the audio. Kept here (not at the call site) so
#: the transcript-only contract of this function is enforced in one place.
#: The prompt is language-neutral: the model must transcribe literally in
#: whichever language was spoken (PT or EN) and never translate — the PT/EN
#: response-policy decision happens later, on the text decision machine, not
#: inside this transcription step.
DEFAULT_TRANSCRIPTION_PROMPT = (
    "Transcribe this audio literally, in the exact language that was spoken. "
    "Do not translate. Do not add commentary, quotes, prefixes, or any other "
    "text — reply with only the raw transcription. "
    f"If no intelligible speech is present, reply exactly: {NO_SPEECH_MARKER}"
)

#: MIME type -> ``input_audio.format`` label. The key is the MIME type with
#: parameters stripped and lowercased (``audio/ogg; codecs=opus`` ->
#: ``audio/ogg``). WhatsApp voice notes arrive as ``audio/ogg``.
MIMETYPE_TO_FORMAT: dict[str, str] = {
    "audio/ogg": "ogg",
    "audio/oga": "ogg",
    "audio/opus": "ogg",
    "audio/x-opus+ogg": "ogg",
    "application/ogg": "ogg",
    "audio/wav": "wav",
    "audio/wave": "wav",
    "audio/x-wav": "wav",
    "audio/vnd.wave": "wav",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mpeg3": "mp3",
    "audio/x-mpeg-3": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/aac": "aac",
    "audio/flac": "flac",
    "audio/x-flac": "flac",
    "audio/webm": "webm",
}

#: ``input_audio.format`` values proven accepted by OpenRouter against the
#: live endpoints (see
#: ``.genie/wishes/fallback-texto/evidence/multimodal-format-probe.md``):
#: ``ogg`` on ``google/gemini-2.5-flash`` and
#: ``mistralai/voxtral-small-24b-2507``, ``wav`` on ``google/gemini-2.5-flash``.
OPENROUTER_MEASURED_FORMATS = frozenset({"ogg", "wav"})

#: Formats OpenRouter is *expected* to accept but which were never probed.
#: They are let through rather than refused locally because the cost of being
#: wrong is one provider 400 surfacing as ``HTTPStatusError``, after which the
#: fallback chain moves on — whereas refusing locally would guarantee a
#: failure for audio that may well have worked. Promote an entry into
#: :data:`OPENROUTER_MEASURED_FORMATS` only with a probe recorded in the
#: evidence file.
OPENROUTER_UNVERIFIED_FORMATS = frozenset({"mp3", "m4a", "aac", "flac", "webm"})

#: ``input_audio.format`` values the adapter will attempt per provider. An
#: empty set means "provider proven incompatible with ``input_audio``
#: altogether" and the adapter refuses before spending a request. The OpenAI
#: and Groq rows are measured in full; the OpenRouter row mixes measured and
#: unverified entries — see the two constants above for which is which.
ACCEPTED_FORMATS_BY_KIND: dict[str, frozenset[str]] = {
    # Rejects 'ogg' by enum ("Supported values are: 'wav' and 'mp3'") and
    # rejects OGG bytes mislabelled 'wav' as 'unsupported_format'.
    "multimodal_openai": frozenset({"wav", "mp3"}),
    # Rejects the input_audio content block itself, whatever the format.
    "multimodal_groq": frozenset(),
    "multimodal_openrouter": OPENROUTER_MEASURED_FORMATS
    | OPENROUTER_UNVERIFIED_FORMATS,
}


class MultimodalTranscriptionError(RuntimeError):
    """Secret-free multimodal transcription failure.

    ``code`` is a stable machine token (``unsupported_audio_format``,
    ``provider_not_multimodal_capable``, ``empty_multimodal_response``) that
    the fallback chain can branch on without ever echoing credentials,
    headers or audio bytes.
    """

    def __init__(self, code: str, **context: Any) -> None:
        self.code = code
        self.context: dict[str, Any] = context
        super().__init__(code)


def multimodal_kind_for_url(base_url: str) -> str:
    """Return the safe-egress kind label for a multimodal base URL.

    Raises :class:`~src.services.safe_egress.UnsafeEgressError` for any host
    outside the three pinned providers: the multimodal credential allowlist
    has no ``outros`` entry, so there is no custom-public case to fall back
    to and an unknown host must be refused.
    """

    parsed = parse_public_https_url(base_url)
    if parsed.hostname in MULTIMODAL_OPENROUTER_HOSTS:
        return "multimodal_openrouter"
    if parsed.hostname in MULTIMODAL_GROQ_HOSTS:
        return "multimodal_groq"
    if parsed.hostname in MULTIMODAL_OPENAI_HOSTS:
        return "multimodal_openai"
    raise UnsafeEgressError("unsafe_api_url")


def normalize_mimetype(mimetype: str | None) -> str:
    """Lowercase a MIME type and drop its parameters (``; codecs=opus``)."""

    return (mimetype or "").split(";", 1)[0].strip().lower()


def format_for_mimetype(mimetype: str | None) -> str:
    """Map an audio MIME type onto an ``input_audio.format`` label.

    Raises :class:`MultimodalTranscriptionError` for unknown types: guessing
    a label produces a provider-side 400 that is far harder to read than an
    explicit local refusal, and the fallback chain treats both the same way.
    """

    normalized = normalize_mimetype(mimetype)
    audio_format = MIMETYPE_TO_FORMAT.get(normalized)
    if audio_format is None:
        raise MultimodalTranscriptionError(
            "unsupported_audio_format", mimetype=normalized
        )
    return audio_format


def _assert_provider_accepts(kind: str, audio_format: str) -> None:
    """Refuse locally when the provider is known not to accept the format."""

    accepted = ACCEPTED_FORMATS_BY_KIND.get(kind, frozenset())
    if not accepted:
        raise MultimodalTranscriptionError("provider_not_multimodal_capable", kind=kind)
    if audio_format not in accepted:
        raise MultimodalTranscriptionError(
            "unsupported_audio_format", kind=kind, audio_format=audio_format
        )


def _extract_text(payload: Any) -> str:
    """Pull the assistant text out of a chat-completions response body.

    Returns ``""`` when the body has no usable text; the caller turns that
    into an explicit error so an empty transcript can never be mistaken for
    a successful one.
    """

    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first = choices[0]
    if not isinstance(first, dict):
        return ""
    message = first.get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        # Some providers answer with a content-block list instead of a string.
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "".join(parts).strip()
    return ""


async def transcribe_audio_multimodal(
    api_key: str,
    audio_bytes: bytes,
    mimetype: str,
    model: str = DEFAULT_MULTIMODAL_MODEL,
    base_url: str | None = None,
    prompt: str | None = None,
) -> str:
    """Transcribe audio with a multimodal chat model. Returns the text.

    Args:
        api_key: Bearer token for the multimodal provider.
        audio_bytes: Raw audio file content (WhatsApp delivers OGG/Opus).
        mimetype: MIME type of the audio (e.g. ``"audio/ogg"``), mapped onto
            the provider's ``input_audio.format`` label.
        model: Model identifier (provider-specific).
        base_url: Base URL of the provider. Defaults to OpenRouter, the only
            provider proven to accept WhatsApp's OGG/Opus.
        prompt: Optional override for the transcription instruction.

    Raises:
        UnsafeEgressError: base URL outside the pinned multimodal hosts, or
            any other egress-policy violation.
        MultimodalTranscriptionError: MIME type not mappable, provider known
            not to accept the resulting format, or a response carrying no
            usable text.
        httpx.HTTPError: transport failure or a non-2xx provider response.
    """

    api_base = (base_url or "").rstrip("/") or OPENROUTER_API_BASE
    kind = multimodal_kind_for_url(api_base)
    audio_format = format_for_mimetype(mimetype)
    _assert_provider_accepts(kind, audio_format)

    target_url = f"{api_base}/chat/completions"
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt or DEFAULT_TRANSCRIPTION_PROMPT},
                    {
                        "type": "input_audio",
                        "input_audio": {
                            "data": base64.b64encode(audio_bytes).decode("ascii"),
                            "format": audio_format,
                        },
                    },
                ],
            }
        ],
    }
    async with safe_async_client(
        target_url,
        kind=kind,
        timeout=httpx.Timeout(60.0, connect=10.0),
    ) as (client, validated):
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Host": validated.sni_host,
        }
        resp = await client.post(validated.url, headers=headers, json=body)
        resp.raise_for_status()
        try:
            payload = resp.json()
        except ValueError as exc:
            raise MultimodalTranscriptionError("empty_multimodal_response") from exc

    text = _extract_text(payload)
    if not text:
        raise MultimodalTranscriptionError("empty_multimodal_response")
    return text


# ---------------------------------------------------------------------------
# Image description (whatsapp-image-understanding, Decisions 1, 11 and 12)
# ---------------------------------------------------------------------------

#: Marker the image prompt asks for when nothing describable is visible.
#: Like :data:`NO_SPEECH_MARKER` it is language-neutral; a response carrying
#: it is a failure (``no_image_content``), never a description.
IMAGE_NO_CONTENT_MARKER = "[[NO_IMAGE_CONTENT]]"

#: Description instructions, one per agent language (Decision 11): an
#: objective description plus a literal transcription of any visible text,
#: and never a diagnosis or clinical opinion. Kept here (not at the call
#: site) so the describe-only contract is enforced in one place.
IMAGE_DESCRIPTION_PROMPT_PT = (
    "Descreva esta imagem de forma objetiva, em português do Brasil, e "
    "transcreva literalmente, sem traduzir, todo texto visível nela. Não faça "
    "diagnóstico, não dê opinião clínica e não interprete o significado "
    "médico do que aparece: apenas descreva o que se vê. Responda somente com "
    "a descrição, sem comentários, prefixos ou aspas. Se não houver nada que "
    f"possa ser descrito, responda exatamente: {IMAGE_NO_CONTENT_MARKER}"
)
IMAGE_DESCRIPTION_PROMPT_EN = (
    "Describe this image objectively, in English, and transcribe verbatim, "
    "without translating, any text visible in it. Do not make a diagnosis, do "
    "not give a clinical opinion and do not interpret the medical meaning of "
    "what appears: only describe what is seen. Reply with only the "
    "description, without commentary, prefixes or quotes. If there is nothing "
    f"that can be described, reply exactly: {IMAGE_NO_CONTENT_MARKER}"
)

#: The only egress kind allowed to receive patient images: the tenant's
#: OpenRouter bundle (Decisions 1 and 15). OpenAI and Groq are pinned for
#: audio but never receive an image.
IMAGE_DESCRIPTION_KIND = "multimodal_openrouter"

#: Stable codes of :class:`MultimodalImageError` (Decision 11).
IMAGE_ERROR_CODES: frozenset[str] = frozenset(
    {
        "provider_not_multimodal_capable",
        "unsupported_image_mime",
        "provider_http_error",
        "provider_transport_error",
        "invalid_provider_response",
        "no_image_content",
    }
)


class MultimodalImageError(RuntimeError):
    """Secret-free multimodal image-description failure.

    Sibling of :class:`MultimodalTranscriptionError`. ``code`` is one of
    :data:`IMAGE_ERROR_CODES`; ``context`` never carries credentials, headers,
    image bytes or the model's text.
    """

    def __init__(self, code: str, **context: Any) -> None:
        self.code = code
        self.context: dict[str, Any] = context
        super().__init__(code)


def image_description_prompt(agent_language: str) -> str:
    """The description prompt in the agent's language (``en`` or pt-BR)."""

    if agent_language == "en":
        return IMAGE_DESCRIPTION_PROMPT_EN
    return IMAGE_DESCRIPTION_PROMPT_PT


def image_mimetype(mimetype: str | None) -> str:
    """Normalize an image MIME type, refusing anything off the allowlist."""

    normalized = normalize_mimetype(mimetype)
    if normalized not in IMAGE_MIME_ALLOWLIST:
        raise MultimodalImageError("unsupported_image_mime")
    return normalized


def _extract_image_text(payload: Any) -> str | None:
    """Assistant text of a chat-completions body, or ``None`` when malformed.

    Unlike :func:`_extract_text` this separates a body that is not a
    chat-completions answer at all (``None`` -> ``invalid_provider_response``)
    from a well-formed answer with no text, such as an empty string or a
    refusal (``""`` -> ``no_image_content``).
    """

    if not isinstance(payload, dict):
        return None
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        return "".join(parts).strip()
    return None


async def describe_image_multimodal(
    api_key: str,
    image_bytes: bytes,
    mimetype: str,
    model: str = DEFAULT_MULTIMODAL_MODEL,
    base_url: str | None = None,
    agent_language: str = "pt-BR",
) -> str:
    """Describe an image with the tenant's OpenRouter model. Returns the text.

    The request is an OpenAI-compatible ``chat/completions`` call whose single
    user message carries the :func:`image_description_prompt` text block and
    exactly one ``image_url`` block with a ``data:<mime>;base64,...`` URL of
    the normalized MIME. Egress, client, headers and timeout mirror
    :func:`transcribe_audio_multimodal`.

    Args:
        api_key: Bearer token for OpenRouter.
        image_bytes: Raw image content (JPEG, PNG or WebP).
        mimetype: MIME type of the image; parameters and case are normalized.
        model: OpenRouter model identifier.
        base_url: Provider base URL. Defaults to OpenRouter; any URL that does
            not resolve to ``multimodal_openrouter`` is refused before I/O.
        agent_language: ``"en"`` for the English prompt, else pt-BR.

    Raises:
        UnsafeEgressError: base URL outside the pinned multimodal hosts (no
            DNS lookup, no request), or another egress-policy violation such
            as a redirect.
        MultimodalImageError: ``provider_not_multimodal_capable`` (pinned
            provider other than OpenRouter), ``unsupported_image_mime`` and
            ``no_image_content`` for empty bytes (all before any I/O);
            ``provider_http_error`` (non-2xx), ``provider_transport_error``,
            ``invalid_provider_response`` (not a chat-completions body) and
            ``no_image_content`` (empty text or
            :data:`IMAGE_NO_CONTENT_MARKER`) after the call.
    """

    api_base = (base_url or "").rstrip("/") or OPENROUTER_API_BASE
    kind = multimodal_kind_for_url(api_base)
    if kind != IMAGE_DESCRIPTION_KIND:
        raise MultimodalImageError("provider_not_multimodal_capable", kind=kind)
    normalized = image_mimetype(mimetype)
    if not image_bytes:
        raise MultimodalImageError("no_image_content")

    encoded = base64.b64encode(image_bytes).decode("ascii")
    target_url = f"{api_base}/chat/completions"
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": image_description_prompt(agent_language)},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{normalized};base64,{encoded}"},
                    },
                ],
            }
        ],
    }
    try:
        async with safe_async_client(
            target_url,
            kind=kind,
            timeout=httpx.Timeout(60.0, connect=10.0),
        ) as (client, validated):
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Host": validated.sni_host,
            }
            resp = await client.post(validated.url, headers=headers, json=body)
            resp.raise_for_status()
            try:
                payload = resp.json()
            except ValueError as exc:
                raise MultimodalImageError("invalid_provider_response") from exc
    except httpx.HTTPStatusError as exc:
        raise MultimodalImageError(
            "provider_http_error", status_code=exc.response.status_code
        ) from exc
    except httpx.HTTPError as exc:
        raise MultimodalImageError("provider_transport_error") from exc

    text = _extract_image_text(payload)
    if text is None:
        raise MultimodalImageError("invalid_provider_response")
    # A marker anywhere (not only an exact match) is a refusal to describe:
    # forwarding it would put the marker itself into the patient's message.
    if not text or IMAGE_NO_CONTENT_MARKER in text:
        raise MultimodalImageError("no_image_content")
    return text
