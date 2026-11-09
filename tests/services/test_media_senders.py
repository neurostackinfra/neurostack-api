"""Contract tests for the four WhatsApp media senders (envio-midia-agente G3).

Every sender runs for real against an in-memory ``httpx.MockTransport``
behind the production safe-egress path (DNS pinned through the patched
resolver, redirects refused). For each provider and ``media_assets``
category the tests pin URL, headers, body and timeout; they also prove
errors leave sanitized, the Uazapi gate blocks before any call, and the
peak allocation of a 20 MiB PDF send stays inside the memory budget of
Decision 9.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import re
import socket
import tracemalloc
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from src.services import (
    media_assets,
    media_send_body,
    meta_service,
    safe_egress,
    uazapi_service,
    zapi_service,
)
from src.services.egress_policy import EgressDisabledError, EgressPolicy

PUBLIC_IP = "157.240.229.35"
UAZAPI_URL = "https://uazapi.customer.example"
EVOLUTION_URL = "https://evo.customer.example"
EXPECTED_TIMEOUT = {"connect": 5.0, "read": 60.0, "write": 60.0, "pool": 60.0}
MEMORY_BUDGET_BYTES = 80_000_000
MAX_TRANSPORT_CHUNK_BYTES = 1024 * 1024
PDF_SIZE_BYTES = 20 * 1024 * 1024

# category -> (mime, filename, payload with the real file signature)
ASSETS: dict[str, tuple[str, str, bytes]] = {
    "image": ("image/png", "foto clínica.png", b"\x89PNG\r\n\x1a\n" + b"img" * 7),
    "pdf": ("application/pdf", 'Guia "pré-op".pdf', b"%PDF-1.7\n" + b"pdf" * 9),
    "audio": ("audio/mpeg", "boas-vindas.mp3", b"\xff\xfb\x90\x00" + b"mp3" * 5),
    "video": ("video/mp4", "tour.mp4", b"\x00\x00\x00\x18ftypmp42" + b"vid" * 4),
}
CATEGORIES = list(ASSETS)


def _ogg_opus(channels: int) -> bytes:
    """One RFC 3533 page (one segment) holding an RFC 7845 OpusHead."""

    head = b"OpusHead" + bytes([1, channels]) + b"\x38\x01" + b"\x80\xbb\x00\x00"
    head += b"\x00\x00" + b"\x00"
    assert len(head) == 19
    page = b"OggS\x00\x02" + bytes(8) + b"\x01\x00\x00\x00" + bytes(8)
    return page + bytes([1, len(head)]) + head


# The audio formats migration 044 stores beyond MP3 (mime -> name, payload).
OGG_M4A_ASSETS: dict[str, tuple[str, bytes]] = {
    "audio/ogg": ("recado de voz.opus", _ogg_opus(1)),
    "audio/mp4": ("recado.M4A", b"\x00\x00\x00\x18ftypM4A " + b"m4a" * 4),
}
# Every Meta media case: (category, mime, author file name, payload).
META_CASES: list[tuple[str, str, str, bytes]] = [
    (category, mime, filename, payload)
    for category, (mime, filename, payload) in ASSETS.items()
] + [("audio", mime, name, payload) for mime, (name, payload) in OGG_M4A_ASSETS.items()]
# JPEG too, author-named ``.jpg``: Meta's table spells it ``.jpeg``.
META_CASES.append(
    ("image", "image/jpeg", "fachada.jpg", b"\xff\xd8\xff\xe0" + b"jpg" * 7)
)
# The Meta upload extension of each canonical MIME, spelled out here so a
# drift in ``MEDIA_FILE_EXTENSIONS`` fails the test (error 131053 upstream).
# They are the extensions of Meta's supported-media table: JPEG is ``.jpeg``.
META_UPLOAD_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpeg",
    "application/pdf": ".pdf",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
}


def _addrinfo(ip: str, port: int = 443) -> tuple[Any, ...]:
    return (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (ip, port))


def _resolver(_host: str, port: int) -> list[tuple[Any, ...]]:
    return [_addrinfo(PUBLIC_IP, port)]


@contextmanager
def egress_env(mock_transport: httpx.AsyncBaseTransport) -> Iterator[None]:
    """Pin DNS to a public IP and force ``mock_transport`` (repo pattern)."""

    orig_dns = safe_egress._default_getaddrinfo
    orig_build = safe_egress.build_safe_async_client

    def _fake_build(validated, *, timeout=None, transport=None):  # type: ignore[no-untyped-def]
        return orig_build(validated, timeout=timeout, transport=mock_transport)

    safe_egress._default_getaddrinfo = _resolver
    safe_egress.build_safe_async_client = _fake_build
    try:
        yield
    finally:
        safe_egress._default_getaddrinfo = orig_dns
        safe_egress.build_safe_async_client = orig_build


@pytest.fixture(autouse=True)
def _open_uazapi_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def allow() -> None:
        return None

    monkeypatch.setattr(uazapi_service, "require_uazapi_egress", allow)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _is_meta_upload(request: httpx.Request) -> bool:
    return request.url.host == "graph.facebook.com" and request.url.path.endswith(
        "/media"
    )


def _ok_handler(seen: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if _is_meta_upload(request):
            return httpx.Response(200, json={"id": "MEDIA-1"})
        return httpx.Response(200, json={"messages": [{"id": "wamid.media"}]})

    return handler


def _assert_json_request(request: httpx.Request) -> dict[str, Any]:
    assert request.method == "POST"
    assert request.headers["Content-Type"] == "application/json"
    assert int(request.headers["Content-Length"]) == len(request.content)
    assert "Transfer-Encoding" not in request.headers
    assert request.extensions["timeout"] == EXPECTED_TIMEOUT
    body: dict[str, Any] = json.loads(request.content)
    return body


# ---------------------------------------------------------------------------
# Meta Cloud API
# ---------------------------------------------------------------------------


META_TYPES = {"image": "image", "pdf": "document", "audio": "audio", "video": "video"}


@pytest.mark.parametrize(
    ("category", "mime", "filename", "payload"),
    META_CASES,
    ids=[mime for _category, mime, _name, _payload in META_CASES],
)
async def test_meta_uploads_then_sends_typed_media(
    category: str, mime: str, filename: str, payload: bytes
) -> None:
    seen: list[httpx.Request] = []

    with egress_env(httpx.MockTransport(_ok_handler(seen))):
        result = await meta_service.send_meta_cloud_media(
            "PNID",
            "EAAG-TOKEN",
            "5511999999999",
            payload,
            category=category,  # type: ignore[arg-type]
            mime_type=mime,
            filename=filename,
        )

    assert result == {"messages": [{"id": "wamid.media"}]}
    assert meta_service.extract_meta_message_id(result) == "wamid.media"
    upload, send = seen
    assert str(upload.url) == "https://graph.facebook.com/v18.0/PNID/media"
    assert upload.headers["Authorization"] == "Bearer EAAG-TOKEN"
    assert upload.headers["Host"] == "graph.facebook.com"
    assert upload.headers["Content-Type"].startswith("multipart/form-data; boundary=")
    assert upload.extensions["timeout"] == EXPECTED_TIMEOUT
    form = upload.content
    assert b'name="messaging_product"\r\n\r\nwhatsapp\r\n' in form
    assert f'name="type"\r\n\r\n{mime}\r\n'.encode() in form
    assert f"Content-Type: {mime}\r\n\r\n".encode() + payload + b"\r\n" in form
    # The upload name's extension comes from the canonical MIME, never from
    # the author's file name: Meta refuses a mismatch with error 131053.
    upload_name = f"media{META_UPLOAD_EXTENSIONS[mime]}"
    assert (
        f'Content-Disposition: form-data; name="file"; filename="{upload_name}"'
        f"\r\nContent-Type: {mime}\r\n"
    ).encode() in form
    assert filename.encode() not in form

    assert str(send.url) == "https://graph.facebook.com/v18.0/PNID/messages"
    assert send.headers["Authorization"] == "Bearer EAAG-TOKEN"
    assert send.headers["Host"] == "graph.facebook.com"
    message_type = META_TYPES[category]
    media_object: dict[str, str] = {"id": "MEDIA-1"}
    if category == "pdf":
        # The document keeps the author's display name in the message body.
        media_object["filename"] = filename
    assert _assert_json_request(send) == {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": "5511999999999",
        "type": message_type,
        message_type: media_object,
    }


def _refusing_handler(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"transport must not be reached, got {request.url}")


@pytest.mark.parametrize(
    "payload",
    [
        _ogg_opus(2),
        _ogg_opus(0),
        _ogg_opus(6),
        # No OpusHead where the first packet starts: the channels are unknown.
        _ogg_opus(1).replace(b"OpusHead", b"OpusTags"),
        b"OggS",
    ],
    ids=["stereo", "zero-channels", "surround", "no-opushead", "truncated"],
)
async def test_meta_refuses_an_ogg_that_is_not_mono_opus(payload: bytes) -> None:
    """Meta takes audio/ogg as OPUS with mono input only (audio-messages)."""

    with egress_env(httpx.MockTransport(_refusing_handler)):
        with pytest.raises(ValueError, match="^ogg_opus_not_mono$"):
            await meta_service.send_meta_cloud_media(
                "PNID",
                "T",
                "5511",
                payload,
                category="audio",
                mime_type="audio/ogg",
                filename="recado.ogg",
            )


async def test_meta_missing_upload_id_stops_before_send() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"unexpected": True})

    mime, filename, payload = ASSETS["pdf"]
    with egress_env(httpx.MockTransport(handler)):
        with pytest.raises(RuntimeError, match="^missing_uploaded_media_id$"):
            await meta_service.send_meta_cloud_media(
                "PNID",
                "T",
                "5511",
                payload,
                category="pdf",
                mime_type=mime,
                filename=filename,
            )
    assert [request.url.path for request in seen] == ["/v18.0/PNID/media"]


# ---------------------------------------------------------------------------
# Uazapi
# ---------------------------------------------------------------------------


UAZAPI_TYPES = {"image": "image", "pdf": "document", "audio": "audio", "video": "video"}


@pytest.mark.parametrize("category", CATEGORIES)
async def test_uazapi_sends_data_uri_with_type(category: str) -> None:
    mime, filename, payload = ASSETS[category]
    seen: list[httpx.Request] = []

    with egress_env(httpx.MockTransport(_ok_handler(seen))):
        result = await uazapi_service.send_uazapi_media(
            UAZAPI_URL + "/",
            "UAZ-TOKEN",
            "5511999999999",
            payload,
            category=category,  # type: ignore[arg-type]
            mime_type=mime,
            filename=filename,
        )

    assert result == {"messages": [{"id": "wamid.media"}]}
    (request,) = seen
    assert str(request.url) == f"{UAZAPI_URL}/send/media"
    assert request.headers["Token"] == "UAZ-TOKEN"
    assert request.headers["Host"] == "uazapi.customer.example"
    expected: dict[str, Any] = {
        "number": "5511999999999",
        "type": UAZAPI_TYPES[category],
        "mimetype": mime,
        "file": f"data:{mime};base64,{_b64(payload)}",
    }
    if category == "pdf":
        expected["docName"] = filename
    assert _assert_json_request(request) == expected


async def test_uazapi_gate_closed_makes_no_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _ClosedReader:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def is_enabled(self, flag_name: str) -> bool:
            self.calls.append(flag_name)
            return False

    reader = _ClosedReader()
    safe_client_calls: list[str] = []

    def counted_safe_async_client(url: str, **kwargs: object) -> Any:
        safe_client_calls.append(url)
        raise AssertionError("safe_async_client must not be reached")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"transport must not be reached, got {request.url}")

    monkeypatch.setattr(
        uazapi_service,
        "require_uazapi_egress",
        EgressPolicy(reader).require_uazapi_egress,
    )
    monkeypatch.setattr(uazapi_service, "safe_async_client", counted_safe_async_client)

    mime, filename, payload = ASSETS["pdf"]
    with egress_env(httpx.MockTransport(handler)):
        with pytest.raises(EgressDisabledError) as excinfo:
            await uazapi_service.send_uazapi_media(
                UAZAPI_URL,
                "UAZ-TOKEN",
                "5511",
                payload,
                category="pdf",
                mime_type=mime,
                filename=filename,
            )

    assert excinfo.value.code == "uazapi_egress_disabled"
    assert reader.calls == ["uazapi_egress_enabled"]
    assert safe_client_calls == []


# ---------------------------------------------------------------------------
# Z-API
# ---------------------------------------------------------------------------


ZAPI_ROUTES = {
    "image": ("send-image", "image"),
    "pdf": ("send-document/pdf", "document"),
    "audio": ("send-audio", "audio"),
    "video": ("send-video", "video"),
}


@pytest.mark.parametrize("category", CATEGORIES)
async def test_zapi_uses_per_type_route_and_data_uri(category: str) -> None:
    mime, filename, payload = ASSETS[category]
    seen: list[httpx.Request] = []

    with egress_env(httpx.MockTransport(_ok_handler(seen))):
        result = await zapi_service.send_zapi_media(
            "INSTANCE",
            "ZTOKEN",
            "5511999999999",
            payload,
            category=category,  # type: ignore[arg-type]
            mime_type=mime,
            filename=filename,
            client_token="CLIENT",
        )

    assert result == {"messages": [{"id": "wamid.media"}]}
    (request,) = seen
    route, media_key = ZAPI_ROUTES[category]
    assert str(request.url) == (
        f"https://api.z-api.io/instances/INSTANCE/token/ZTOKEN/{route}"
    )
    assert request.headers["Host"] == "api.z-api.io"
    assert request.headers["Client-Token"] == "CLIENT"
    expected: dict[str, Any] = {
        "phone": "5511999999999",
        media_key: f"data:{mime};base64,{_b64(payload)}",
    }
    if category == "pdf":
        expected["fileName"] = filename
    assert _assert_json_request(request) == expected


async def test_zapi_omits_client_token_when_unset() -> None:
    seen: list[httpx.Request] = []
    mime, filename, payload = ASSETS["image"]
    with egress_env(httpx.MockTransport(_ok_handler(seen))):
        await zapi_service.send_zapi_media(
            "I",
            "T",
            "5511",
            payload,
            category="image",
            mime_type=mime,
            filename=filename,
        )
    assert "Client-Token" not in seen[0].headers


# ---------------------------------------------------------------------------
# Evolution API
# ---------------------------------------------------------------------------


EVOLUTION_TYPES = {"image": "image", "pdf": "document", "video": "video"}


@pytest.mark.parametrize("category", CATEGORIES)
async def test_evolution_routes_media_and_audio(category: str) -> None:
    mime, filename, payload = ASSETS[category]
    seen: list[httpx.Request] = []

    with egress_env(httpx.MockTransport(_ok_handler(seen))):
        result = await zapi_service.send_evolution_media(
            EVOLUTION_URL + "/",
            "clinic-1",
            "EVO-KEY",
            "5511999999999",
            payload,
            category=category,  # type: ignore[arg-type]
            mime_type=mime,
            filename=filename,
        )

    assert result == {"messages": [{"id": "wamid.media"}]}
    (request,) = seen
    assert request.headers["apikey"] == "EVO-KEY"
    assert request.headers["Host"] == "evo.customer.example"
    body = _assert_json_request(request)
    if category == "audio":
        assert str(request.url) == (
            f"{EVOLUTION_URL}/message/sendWhatsAppAudio/clinic-1"
        )
        assert body == {
            "number": "5511999999999",
            "audio": _b64(payload),
            "encoding": True,
        }
    else:
        assert str(request.url) == f"{EVOLUTION_URL}/message/sendMedia/clinic-1"
        assert body == {
            "number": "5511999999999",
            "mediatype": EVOLUTION_TYPES[category],
            "mimetype": mime,
            "media": _b64(payload),
            "fileName": filename,
        }


# ---------------------------------------------------------------------------
# Shared contracts
# ---------------------------------------------------------------------------


SECRET = "SECRET-token-value-9f8e7d"


def _senders(
    category: str = "pdf",
    mime: str | None = None,
    payload: bytes | None = None,
) -> dict[str, Callable[[], Awaitable[dict[str, object]]]]:
    asset_mime, filename, asset_payload = ASSETS.get(
        category, ("image/webp", "x.webp", b"x")
    )
    effective_mime = asset_mime if mime is None else mime
    effective_payload = asset_payload if payload is None else payload
    kwargs: dict[str, Any] = {
        "category": category,
        "mime_type": effective_mime,
        "filename": filename,
    }
    return {
        "meta": lambda: meta_service.send_meta_cloud_media(
            "PNID", SECRET, "5511", effective_payload, **kwargs
        ),
        "uazapi": lambda: uazapi_service.send_uazapi_media(
            f"{UAZAPI_URL}/{SECRET}", SECRET, "5511", effective_payload, **kwargs
        ),
        "zapi": lambda: zapi_service.send_zapi_media(
            "I", SECRET, "5511", effective_payload, client_token=SECRET, **kwargs
        ),
        "evolution": lambda: zapi_service.send_evolution_media(
            f"{EVOLUTION_URL}/{SECRET}",
            "clinic",
            SECRET,
            "5511",
            effective_payload,
            **kwargs,
        ),
    }


PROVIDERS = list(_senders())


def _assert_sanitized(exc: BaseException) -> None:
    rendered = str(exc) + repr(exc)
    assert SECRET not in rendered
    assert "https://" not in rendered
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_http_error_is_sanitized(provider: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    with egress_env(httpx.MockTransport(handler)):
        with pytest.raises(RuntimeError, match="^upstream_http_error status=500$") as e:
            await _senders()[provider]()
    _assert_sanitized(e.value)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_transport_error_is_sanitized(provider: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timed out reading {request.url}", request=request)

    with egress_env(httpx.MockTransport(handler)):
        with pytest.raises(
            RuntimeError, match="^upstream_transport_error type=ReadTimeout$"
        ) as e:
            await _senders()[provider]()
    _assert_sanitized(e.value)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_decoding_error_is_sanitized(provider: str) -> None:
    """``DecodingError`` is a ``RequestError`` outside ``TransportError``."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.DecodingError(f"bad gzip from {request.url}", request=request)

    with egress_env(httpx.MockTransport(handler)):
        with pytest.raises(
            RuntimeError, match="^upstream_transport_error type=DecodingError$"
        ) as e:
            await _senders()[provider]()
    _assert_sanitized(e.value)


@pytest.mark.parametrize("provider", PROVIDERS)
@pytest.mark.parametrize(
    ("category", "mime", "payload", "code"),
    [
        ("image", "image/gif", None, "unsupported_media_type"),
        ("pdf", "image/png", None, "unsupported_media_type"),
        ("sticker", None, None, "unsupported_media_type"),
        ("pdf", None, b"", "empty_media_payload"),
    ],
)
async def test_invalid_media_fails_before_any_call(
    provider: str, category: str, mime: str | None, payload: bytes | None, code: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"transport must not be reached, got {request.url}")

    with egress_env(httpx.MockTransport(handler)):
        with pytest.raises(ValueError, match=f"^{code}$"):
            await _senders(category, mime, payload)[provider]()


@pytest.mark.parametrize("mime", sorted(OGG_M4A_ASSETS))
@pytest.mark.parametrize("provider", ["zapi", "uazapi", "evolution"])
async def test_ogg_and_m4a_are_refused_where_the_provider_does_not_send_them(
    provider: str, mime: str
) -> None:
    """Z-API's send-audio documents only audio/mpeg (OGG and M4A wait for a
    live test); uazapi and Evolution keep their Group 3 formats. Stored audio
    in either format fails before any call, mono Opus included."""

    _name, payload = OGG_M4A_ASSETS[mime]
    with egress_env(httpx.MockTransport(_refusing_handler)):
        with pytest.raises(ValueError, match="^unsupported_media_type$"):
            await _senders("audio", mime, payload)[provider]()


# ---------------------------------------------------------------------------
# The stored formats and each provider's sendable formats
# ---------------------------------------------------------------------------


_MIGRATION_044 = (
    Path(__file__).resolve().parents[2]
    / "alembic/versions/20261107_000001_044_media_assets_m4a.py"
)
_CLAUSE = re.compile(
    r"category = '(?P<category>\w+)' AND mime_type "
    r"(?:= '(?P<one>[^']+)'|IN \((?P<many>[^)]*)\))"
)


def _invariant_pairs(invariant: str) -> dict[str, frozenset[str]]:
    """``{category: mime types}`` of a ``ck_media_assets_category_mime`` text."""

    pairs: dict[str, frozenset[str]] = {}
    for match in _CLAUSE.finditer(" ".join(invariant.split())):
        listed = match["one"] or match["many"]
        assert match["category"] not in pairs
        pairs[match["category"]] = frozenset(re.findall(r"'([^']+)'", f"'{listed}'"))
    return pairs


def _migration_044_invariant(monkeypatch: pytest.MonkeyPatch) -> str:
    # The repository's ``alembic/`` package shadows the library's ``op``
    # outside an Alembic run (the pattern of test_migration_037_drop_legacy);
    # 044 calls ``op`` only inside upgrade/downgrade, never at import.
    import alembic as alembic_package

    if not hasattr(alembic_package, "op"):
        monkeypatch.setattr(alembic_package, "op", SimpleNamespace(), raising=False)
    spec = importlib.util.spec_from_file_location("_migration_044", _MIGRATION_044)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "044"
    invariant: str = module._invariant(module._AUDIO_WITH_M4A)
    return invariant


def test_the_stored_formats_mirror_the_check_after_migration_044(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``STORED_MEDIA_MIME_TYPES`` is the CHECK migration 044 installs, the
    model's CHECK, and the upload allowlist split by category."""

    from src.models.media_asset import MediaAsset
    from src.services.media_assets import MEDIA_CANONICAL_MIME_TYPES

    stored = dict(media_send_body.STORED_MEDIA_MIME_TYPES)
    assert stored == {
        "image": {"image/jpeg", "image/png"},
        "pdf": {"application/pdf"},
        "audio": {"audio/mpeg", "audio/ogg", "audio/mp4"},
        "video": {"video/mp4"},
    }
    assert stored == _invariant_pairs(_migration_044_invariant(monkeypatch))
    (model_check,) = [
        constraint
        for constraint in MediaAsset.__table__.constraints
        if constraint.name == "ck_media_assets_category_mime"
    ]
    assert stored == _invariant_pairs(str(model_check.sqltext))  # type: ignore[attr-defined]
    assert frozenset().union(*stored.values()) == MEDIA_CANONICAL_MIME_TYPES


def test_each_provider_sends_exactly_its_documented_formats() -> None:
    always = {"image/jpeg", "image/png", "application/pdf", "audio/mpeg", "video/mp4"}
    assert {
        provider: media_send_body.sendable_mime_types(provider)
        for provider in ("meta", "zapi", "uazapi", "evolution")
    } == {
        "meta": always | {"audio/ogg", "audio/mp4"},
        "zapi": always,
        "uazapi": always,
        "evolution": always,
    }
    # A name without a media sender sends nothing (fail closed).
    for unknown in ("meta_cloud", "dialog360", "twilio", ""):
        assert media_send_body.sendable_mime_types(unknown) == frozenset()


def test_every_sendable_format_is_stored_and_has_an_upload_extension() -> None:
    stored = frozenset().union(*media_send_body.STORED_MEDIA_MIME_TYPES.values())
    for provider in ("meta", "zapi", "uazapi", "evolution"):
        assert media_send_body.sendable_mime_types(provider) <= stored
    assert media_assets.MEDIA_FILE_EXTENSIONS == META_UPLOAD_EXTENSIONS
    assert set(META_UPLOAD_EXTENSIONS) == stored
    # Every stored format goes through the Meta upload test above.
    assert {mime for _category, mime, _name, _payload in META_CASES} == stored


def test_the_senders_count_channels_with_the_upload_locator() -> None:
    # One OpusHead locator: the sender's mono check can never disagree with
    # the upload sniff on where the identification header is.
    assert media_send_body.ogg_opus_channel_count is media_assets.ogg_opus_channel_count


@pytest.mark.parametrize(
    ("payload", "channels"),
    [
        (_ogg_opus(1), 1),
        (_ogg_opus(2), 2),
        (_ogg_opus(1).replace(b"OpusHead", b"OpusTags"), None),
        (b"OggS" + bytes(22) + b"\x05", None),
        (b"ID3\x04" + bytes(40), None),
        # OpusHead where the first packet would start, without the capture
        # pattern: not an Ogg page.
        (b"XXXX" + _ogg_opus(1)[4:], None),
    ],
    ids=[
        "mono",
        "stereo",
        "no-opushead",
        "short-segment-table",
        "not-ogg",
        "no-capture-pattern",
    ],
)
def test_ogg_opus_channel_count_reads_the_identification_header(
    payload: bytes, channels: int | None
) -> None:
    assert media_send_body.ogg_opus_channel_count(payload) == channels


@pytest.mark.parametrize("provider", ["uazapi", "zapi", "evolution"])
async def test_multi_chunk_base64_matches_single_encode(provider: str) -> None:
    """Chunked base64 must equal one ``b64encode`` across chunk boundaries."""

    chunk = media_send_body.BASE64_CHUNK_BYTES
    assert chunk % 3 == 0
    payload = b"%PDF-1.7\n" + bytes(range(256)) * ((2 * chunk) // 256) + b"tail!"
    assert len(payload) % 3 != 0 and len(payload) > 2 * chunk
    seen: list[httpx.Request] = []

    with egress_env(httpx.MockTransport(_ok_handler(seen))):
        await _senders(payload=payload)[provider]()

    body = _assert_json_request(seen[0])
    media = next(v for k, v in body.items() if k in {"file", "document", "media"})
    assert base64.b64decode(media.split(",", 1)[-1], validate=True) == payload


# ---------------------------------------------------------------------------
# Memory budget (Decision 9)
# ---------------------------------------------------------------------------


class _DrainingMockTransport(httpx.MockTransport):
    """``MockTransport`` that drains the request body in chunks, keeping none.

    The stock ``MockTransport`` calls ``request.aread()``, which joins the
    whole body into one ``bytes``: a copy that never exists on the real
    socket path. This subclass iterates ``request.stream`` like the network
    transport does and only counts bytes. It also records the largest chunk
    handed over: the real TLS transport copies each chunk whole, so one big
    chunk multiplies the peak even when no Python-level copy shows up here.
    """

    def __init__(self) -> None:
        super().__init__(self._respond)
        self.drained: list[tuple[str, int, int]] = []
        self.max_chunk = 0

    @staticmethod
    def _respond(request: httpx.Request) -> httpx.Response:
        if _is_meta_upload(request):
            return httpx.Response(200, json={"id": "MEDIA-1"})
        return httpx.Response(200, json={"messages": [{"id": "wamid.big"}]})

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        streamed = 0
        stream = request.stream
        assert isinstance(stream, httpx.AsyncByteStream)
        async for part in stream:
            streamed += len(part)
            self.max_chunk = max(self.max_chunk, len(part))
        declared = int(request.headers["Content-Length"])
        self.drained.append((request.url.path, declared, streamed))
        return self._respond(request)


@pytest.mark.parametrize("provider", PROVIDERS)
async def test_20mib_pdf_send_peak_stays_under_budget(provider: str) -> None:
    transport = _DrainingMockTransport()
    with egress_env(transport):
        tracemalloc.start()
        try:
            # The asset bytes are allocated inside the window on purpose: the
            # budget covers the whole send, starting from the loaded asset.
            payload = b"%PDF-1.7\n".ljust(PDF_SIZE_BYTES, b"\x00")
            sender = _senders(payload=payload)[provider]
            result = await sender()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

    assert result["messages"] == [{"id": "wamid.big"}]
    for _path, declared, streamed in transport.drained:
        assert declared == streamed
    body_bytes = transport.drained[0][2]
    assert body_bytes >= PDF_SIZE_BYTES
    print(
        f"\n[memory] provider={provider} payload={PDF_SIZE_BYTES / 2**20:.1f} MiB "
        f"body={body_bytes / 2**20:.1f} MiB peak={peak / 2**20:.2f} MiB "
        f"max_chunk={transport.max_chunk / 2**20:.2f} MiB"
    )
    assert peak < MEMORY_BUDGET_BYTES, f"{provider} peak {peak / 2**20:.2f} MiB"
    assert transport.max_chunk <= MAX_TRANSPORT_CHUNK_BYTES, (
        f"{provider} handed the transport a "
        f"{transport.max_chunk / 2**20:.2f} MiB chunk"
    )
