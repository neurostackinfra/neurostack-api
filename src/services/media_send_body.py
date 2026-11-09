"""
NeuroStack — shared pieces of the WhatsApp media senders.

The Meta, Uazapi, Z-API and Evolution media senders share the category and
MIME contract of ``media_assets``, the formats each provider sends, the media
timeout, the URL-free error sanitizer and the streamed base64 JSON body. They
live here once so the four senders cannot drift apart.
"""

import binascii
import json
from collections.abc import AsyncIterator, Mapping
from typing import Final, Literal, get_args

import httpx

# isort keeps a comment above the first import of its block, so this one names
# its line: the second import re-exports ``ogg_opus_channel_count`` for the
# delivery core. The channel count comes from the upload sniff's OpusHead
# locator, never a second parser.
from src.services.media_assets import MEDIA_CANONICAL_MIME_TYPES
from src.services.media_assets import ogg_opus_channel_count as ogg_opus_channel_count

MediaCategory = Literal["image", "pdf", "audio", "video"]

# Per-operation timeouts for each hop of a media send: 20 MB assets need more
# than the 15 s text budget, the connect phase stays short. httpx applies each
# value to a single connect, read, write or pool-acquire operation, so this is
# not a ceiling on the whole request: a peer that keeps trickling bytes can
# hold it open longer.
MEDIA_SEND_TIMEOUT: Final = httpx.Timeout(60.0, connect=5.0)

# Raw bytes per base64 chunk. A multiple of 3 keeps every chunk padding-free,
# so the concatenated chunks equal one ``b64encode`` of the whole payload.
BASE64_CHUNK_BYTES: Final = 3 * 256 * 1024


def _stored_category(mime_type: str) -> str:
    return "pdf" if mime_type == "application/pdf" else mime_type.split("/", 1)[0]


# The formats we STORE: canonical MIME types per media_assets category, the
# ``ck_media_assets_category_mime`` CHECK after migration 044 (audio = mpeg,
# ogg, mp4). Derived from the upload allowlist ``MEDIA_CANONICAL_MIME_TYPES``,
# never listed twice; a test pins it against migration 044 and the model.
STORED_MEDIA_MIME_TYPES: Final[Mapping[str, frozenset[str]]] = {
    category: frozenset(
        mime
        for mime in MEDIA_CANONICAL_MIME_TYPES
        if _stored_category(mime) == category
    )
    for category in get_args(MediaCategory)
}

# The formats each provider's media sender SENDS, a subset of the stored ones.
# The single source of the senders, of the agent's media selection and of
# ``sendable_mime_types`` in GET /v1/media-assets/bindable-intents. Keyed by
# the delivery provider name (the ``provider`` label of the delivery counter).
_SENDABLE_MIME_TYPES: Final[Mapping[str, frozenset[str]]] = {
    # developers.facebook.com/docs/whatsapp/cloud-api/reference/media and
    # messages/audio-messages (docs scout, 2026-11-08): audio/ogg is OPUS only
    # with mono input (see ``require_sendable_media``), audio/mp4 is M4A.
    "meta": frozenset(
        {
            "image/jpeg",
            "image/png",
            "application/pdf",
            "audio/mpeg",
            "audio/ogg",
            "audio/mp4",
            "video/mp4",
        }
    ),
    # developer.z-api.io/en/message/send-message-audio (docs scout, 2026-11-08)
    # shows only audio/mpeg and is silent on OGG and M4A. They stay out until
    # a live test on a real instance; unlocking them is adding them here.
    "zapi": frozenset(
        {"image/jpeg", "image/png", "application/pdf", "audio/mpeg", "video/mp4"}
    ),
    # uazapi and Evolution keep their Group 3 formats. Evolution is never
    # ready for agent media; uazapi only while its verified turns are
    # production and its egress is open (``media_delivery_provider_ready``).
    "uazapi": frozenset(
        {"image/jpeg", "image/png", "application/pdf", "audio/mpeg", "video/mp4"}
    ),
    "evolution": frozenset(
        {"image/jpeg", "image/png", "application/pdf", "audio/mpeg", "video/mp4"}
    ),
}

# Providers whose audio/ogg must be mono Opus (Meta's audio-messages doc).
_MONO_OPUS_PROVIDERS: Final = frozenset({"meta"})


def sendable_mime_types(provider: str) -> frozenset[str]:
    """The canonical MIME types ``provider``'s media sender sends.

    ``provider`` is a delivery provider name (``meta``, ``zapi``, ``uazapi``,
    ``evolution``); any other name sends nothing (empty set, fail closed).
    """

    return _SENDABLE_MIME_TYPES.get(provider, frozenset())


def require_sendable_media(
    provider: str, category: str, mime_type: str, payload: bytes
) -> None:
    """Fail closed before any call on media ``provider`` cannot send.

    Raises ``ValueError``:
    - ``unsupported_media_type``: not a stored category/MIME pair, or a MIME
      outside :func:`sendable_mime_types` of ``provider``;
    - ``empty_media_payload``: no bytes;
    - ``ogg_opus_not_mono``: an audio/ogg for a mono-only provider (Meta)
      whose OpusHead is missing or does not say one channel. Upload already
      refuses a non-mono OGG (``media_audio_not_mono``), so a stored OGG
      passes; this stays as the fail-closed check right before the send.
    """

    stored = STORED_MEDIA_MIME_TYPES.get(category)
    if (
        stored is None
        or mime_type not in stored
        or mime_type not in sendable_mime_types(provider)
    ):
        raise ValueError("unsupported_media_type")
    if not payload:
        raise ValueError("empty_media_payload")
    if (
        mime_type == "audio/ogg"
        and provider in _MONO_OPUS_PROVIDERS
        and ogg_opus_channel_count(payload) != 1
    ):
        raise ValueError("ogg_opus_not_mono")


def sanitize_transport_error(exc: httpx.RequestError) -> RuntimeError:
    """Return a URL-free RuntimeError naming only the request failure class.

    ``exc.request`` carries the URL, and the Z-API URL carries the token, so
    the original exception is never chained or echoed.
    """

    return RuntimeError(f"upstream_transport_error type={type(exc).__name__}")


def base64_json_body(
    fields: Mapping[str, object],
    media_key: str,
    payload: bytes,
    *,
    data_uri_mime: str | None = None,
) -> tuple[AsyncIterator[bytes], int]:
    """Stream ``{**fields, media_key: "<base64 of payload>"}`` as JSON bytes.

    The base64 text is produced chunk by chunk while httpx writes the body,
    so neither a base64 copy of the payload nor a joined JSON copy is ever
    held: the peak stays at the payload plus one chunk. The exact length is
    known upfront, so the caller sends ``Content-Length`` instead of chunked
    transfer encoding. The base64 alphabet and the ``data:`` prefix need no
    JSON escaping; every other field goes through ``json.dumps``.

    Returns:
        ``(body, content_length)`` for ``client.post(content=body)``.
    """

    if media_key in fields:
        raise ValueError("duplicate_media_key")
    value_prefix = f"data:{data_uri_mime};base64," if data_uri_mime else ""
    # ``media_key`` is inserted last, so the document ends with its string
    # value and the closing brace: cut them off and stream the payload there.
    document = json.dumps({**fields, media_key: value_prefix})
    if not document.endswith('"}'):
        raise ValueError("unexpected_json_layout")
    head = document[:-2].encode("ascii")
    tail = b'"}'
    encoded_length = 4 * ((len(payload) + 2) // 3)

    async def _chunks() -> AsyncIterator[bytes]:
        yield head
        view = memoryview(payload)
        for start in range(0, len(view), BASE64_CHUNK_BYTES):
            yield binascii.b2a_base64(
                view[start : start + BASE64_CHUNK_BYTES], newline=False
            )
        yield tail

    return _chunks(), len(head) + encoded_length + len(tail)
