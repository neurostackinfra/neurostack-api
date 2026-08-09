"""Egress-boundary tests for :mod:`src.services.meta_service`.

The wish pins Meta traffic to ``graph.facebook.com``. These tests prove
every code path in the module routes through
:mod:`src.services.safe_egress` and refuses any URL that a stolen tenant
config (or a bug elsewhere) might smuggle in.
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import contextmanager
from typing import Any

import httpx
import pytest

from src.services import meta_service, safe_egress
from src.services.safe_egress import UnsafeEgressError

META_PUBLIC_IP = "157.240.229.35"


def _addrinfo(ip: str, port: int = 443) -> tuple[Any, ...]:
    if ":" in ip:
        family = socket.AF_INET6
        sockaddr: tuple[Any, ...] = (ip, port, 0, 0)
    else:
        family = socket.AF_INET
        sockaddr = (ip, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


def make_resolver(*ips: str):
    def _resolver(_host: str, port: int) -> list[tuple[Any, ...]]:
        return [_addrinfo(ip, port) for ip in ips]

    return _resolver


@contextmanager
def egress_env(resolver, mock_transport: httpx.MockTransport | None):
    """Patch safe_egress DNS resolver and force a MockTransport.

    Callers of ``safe_async_client``/``safe_download_audio`` in production
    pass ``transport=None`` down to ``build_safe_async_client``. To keep the
    mock in force regardless, ``build_safe_async_client`` is monkey-patched
    to always override ``transport`` with the injected mock.
    """

    orig_dns = safe_egress._default_getaddrinfo
    orig_build = safe_egress.build_safe_async_client

    def _fake_build(validated, *, timeout=None, transport=None):
        # Ignore any transport the caller passed — mock always wins here.
        return orig_build(validated, timeout=timeout, transport=mock_transport)

    safe_egress._default_getaddrinfo = resolver
    safe_egress.build_safe_async_client = _fake_build
    try:
        yield
    finally:
        safe_egress._default_getaddrinfo = orig_dns
        safe_egress.build_safe_async_client = orig_build


class TestSendMetaCloudText:
    def test_pins_to_graph_facebook_com(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["host"] = request.headers.get("Host")
            captured["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, json={"messages": [{"id": "wamid.X"}]})

        with egress_env(make_resolver(META_PUBLIC_IP), httpx.MockTransport(handler)):
            data = asyncio.run(
                meta_service.send_meta_cloud_text(
                    phone_number_id="123",
                    access_token="EAAG-secret-token",
                    to_phone="5511999999999",
                    message="hi",
                )
            )
        assert data == {"messages": [{"id": "wamid.X"}]}
        assert "graph.facebook.com" in captured["url"]
        assert "/v18.0/123/messages" in captured["url"]
        # httpx omits ``:443`` in the request URL when it's the scheme default,
        # so assert the host component directly.
        assert captured["host"] == "graph.facebook.com"
        assert captured["auth"] == "Bearer EAAG-secret-token"

    def test_resolves_to_private_ip_is_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail(f"transport must not be reached, got {request.url}")

        with egress_env(make_resolver("10.0.0.5"), httpx.MockTransport(handler)):
            with pytest.raises(UnsafeEgressError) as excinfo:
                asyncio.run(
                    meta_service.send_meta_cloud_text(
                        phone_number_id="123",
                        access_token="secret",
                        to_phone="5511999999999",
                        message="hi",
                    )
                )
        assert excinfo.value.code == "unsafe_api_url"

    def test_redirect_response_is_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302, headers={"Location": "https://elsewhere.example/"}
            )

        with egress_env(make_resolver(META_PUBLIC_IP), httpx.MockTransport(handler)):
            with pytest.raises(UnsafeEgressError) as excinfo:
                asyncio.run(
                    meta_service.send_meta_cloud_text(
                        phone_number_id="123",
                        access_token="SECRET",
                        to_phone="5511999999999",
                        message="hi",
                    )
                )
        assert excinfo.value.code == "redirect_forbidden"

    def test_secret_absent_from_error(self) -> None:
        SECRET = "EAAG-super-secret-value-xxxxx"  # noqa: N806

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "https://x.example/"})

        with egress_env(make_resolver(META_PUBLIC_IP), httpx.MockTransport(handler)):
            try:
                asyncio.run(
                    meta_service.send_meta_cloud_text(
                        phone_number_id="123",
                        access_token=SECRET,
                        to_phone="5511999999999",
                        message="hi",
                    )
                )
            except UnsafeEgressError as exc:
                payload = repr(exc) + str(exc) + repr(exc.context)
                assert SECRET not in payload


class TestResolveMetaMedia:
    def test_pins_to_graph_facebook_com(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["auth"] = request.headers.get("Authorization")
            return httpx.Response(
                200,
                json={"url": "https://lookaside.fbsbx.com/x", "mime_type": "audio/ogg"},
            )

        with egress_env(make_resolver(META_PUBLIC_IP), httpx.MockTransport(handler)):
            data = asyncio.run(meta_service.resolve_meta_media("MEDIA_ID", "SECRET"))
        assert data["mime_type"] == "audio/ogg"
        assert "graph.facebook.com" in captured["url"]
        assert "/v18.0/MEDIA_ID" in captured["url"]
        assert captured["auth"] == "Bearer SECRET"


class TestDownloadMetaMediaAudio:
    def test_content_length_cap_enforced(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "audio/ogg"},
                content=b"a" * 5000,
            )

        with egress_env(make_resolver(META_PUBLIC_IP), httpx.MockTransport(handler)):
            with pytest.raises(UnsafeEgressError) as excinfo:
                asyncio.run(
                    meta_service.download_meta_media_audio(
                        "https://lookaside.fbsbx.com/media/xyz",
                        access_token="BEARER",
                        max_bytes=1000,
                    )
                )
        # httpx sets Content-Length automatically from content, so the
        # Content-Length branch of enforce_content_length trips first.
        assert excinfo.value.code == "audio_too_large"

    def test_rejects_non_https(self) -> None:
        with pytest.raises(UnsafeEgressError):
            asyncio.run(
                meta_service.download_meta_media_audio(
                    "http://lookaside.fbsbx.com/media/xyz",
                    access_token="BEARER",
                    max_bytes=1000,
                )
            )

    def test_happy_path_returns_bytes(self) -> None:
        body = b"OggS" + b"\x00" * 100

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "audio/ogg"},
                content=body,
            )

        with egress_env(make_resolver(META_PUBLIC_IP), httpx.MockTransport(handler)):
            result = asyncio.run(
                meta_service.download_meta_media_audio(
                    "https://lookaside.fbsbx.com/media/xyz",
                    access_token="BEARER",
                    max_bytes=10_000,
                )
            )
        assert result == body


class TestDownloadMetaMedia:
    def test_rejects_userinfo_url(self) -> None:
        with pytest.raises(UnsafeEgressError):
            asyncio.run(
                meta_service.download_meta_media(
                    "https://user:pw@lookaside.fbsbx.com/x",
                    "BEARER",
                )
            )

    def test_pinned_ip_check_still_runs(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("transport must not be reached")

        with egress_env(make_resolver("192.168.1.1"), httpx.MockTransport(handler)):
            with pytest.raises(UnsafeEgressError):
                asyncio.run(
                    meta_service.download_meta_media(
                        "https://lookaside.fbsbx.com/media/xyz",
                        "BEARER",
                    )
                )

    def test_happy_path_returns_bytes(self) -> None:
        body = b"hello"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=body)

        with egress_env(make_resolver(META_PUBLIC_IP), httpx.MockTransport(handler)):
            data = asyncio.run(
                meta_service.download_meta_media(
                    "https://lookaside.fbsbx.com/media/xyz",
                    "BEARER",
                )
            )
        assert data == body
