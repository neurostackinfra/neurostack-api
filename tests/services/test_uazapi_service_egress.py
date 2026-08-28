"""Egress-boundary tests for :mod:`src.services.uazapi_service`.

Uazapi is self-hosted, so the wish's egress policy is ``custom_public``:
any public HTTPS host, but the DNS/IP filter still runs. These tests
prove the private/metadata/link-local matrix is refused and that a valid
public host is honoured.
"""

from __future__ import annotations

import asyncio
import json
import socket
from contextlib import contextmanager
from typing import Any

import httpx
import pytest

from src.services import safe_egress, uazapi_service
from src.services.safe_egress import UnsafeEgressError


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
    orig_dns = safe_egress._default_getaddrinfo
    orig_build = safe_egress.build_safe_async_client

    def _fake_build(validated, *, timeout=None, transport=None):
        return orig_build(validated, timeout=timeout, transport=mock_transport)

    safe_egress._default_getaddrinfo = resolver
    safe_egress.build_safe_async_client = _fake_build
    try:
        yield
    finally:
        safe_egress._default_getaddrinfo = orig_dns
        safe_egress.build_safe_async_client = orig_build


class TestSendUazapiText:
    def test_happy_path_public_host(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["token"] = request.headers.get("Token")
            captured["host"] = request.headers.get("Host")
            return httpx.Response(200, json={"MessageID": "MID.1"})

        with egress_env(make_resolver("1.1.1.1"), httpx.MockTransport(handler)):
            data = asyncio.run(
                uazapi_service.send_uazapi_text(
                    api_url="https://uazapi.customer.example",
                    token="UAZAPI-TOKEN",
                    phone="5511",
                    message="olá",
                )
            )
        assert data == {"MessageID": "MID.1"}
        assert "uazapi.customer.example" in captured["url"]
        assert "/send/text" in captured["url"]
        assert captured["token"] == "UAZAPI-TOKEN"
        assert captured["host"] == "uazapi.customer.example"

    def test_audio_is_sent_as_ptt_media(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"id": "owner:MID"})

        with egress_env(make_resolver("1.1.1.1"), httpx.MockTransport(handler)):
            data = asyncio.run(
                uazapi_service.send_uazapi_audio(
                    "https://uazapi.customer.example", "TOKEN", "5511", b"mp3"
                )
            )
        assert data == {"id": "owner:MID"}
        assert seen[0].url.path == "/send/media"
        payload = json.loads(seen[0].content)
        assert payload["number"] == "5511"
        assert payload["type"] == "ptt"
        assert payload["mimetype"] == "audio/mpeg"
        assert payload["file"].startswith("data:audio/mpeg;base64,")

    def test_strips_trailing_slash(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            return httpx.Response(200, json={})

        with egress_env(make_resolver("1.1.1.1"), httpx.MockTransport(handler)):
            asyncio.run(
                uazapi_service.send_uazapi_text(
                    api_url="https://uazapi.customer.example/",
                    token="T",
                    phone="5511",
                    message="hi",
                )
            )
        # ``rstrip('/')`` prevents ``//send/text``.
        assert captured["path"] == "/send/text"

    def test_rejects_loopback(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("transport must not be reached")

        with egress_env(make_resolver("127.0.0.1"), httpx.MockTransport(handler)):
            with pytest.raises(UnsafeEgressError) as excinfo:
                asyncio.run(
                    uazapi_service.send_uazapi_text(
                        api_url="https://uazapi.customer.example",
                        token="T",
                        phone="5511",
                        message="hi",
                    )
                )
        assert excinfo.value.code == "unsafe_api_url"

    def test_rejects_private_ip(self) -> None:
        with egress_env(
            make_resolver("192.168.100.10"),
            httpx.MockTransport(lambda r: httpx.Response(200)),
        ):
            with pytest.raises(UnsafeEgressError):
                asyncio.run(
                    uazapi_service.send_uazapi_text(
                        api_url="https://uazapi.customer.example",
                        token="T",
                        phone="5511",
                        message="hi",
                    )
                )

    def test_rejects_metadata_ip(self) -> None:
        with egress_env(
            make_resolver("169.254.169.254"),
            httpx.MockTransport(lambda r: httpx.Response(200)),
        ):
            with pytest.raises(UnsafeEgressError):
                asyncio.run(
                    uazapi_service.send_uazapi_text(
                        api_url="https://uazapi.customer.example",
                        token="T",
                        phone="5511",
                        message="hi",
                    )
                )

    def test_rejects_userinfo_url(self) -> None:
        with pytest.raises(UnsafeEgressError):
            asyncio.run(
                uazapi_service.send_uazapi_text(
                    api_url="https://user:pw@uazapi.customer.example",
                    token="T",
                    phone="5511",
                    message="hi",
                )
            )

    def test_rejects_fragment_url(self) -> None:
        with pytest.raises(UnsafeEgressError):
            asyncio.run(
                uazapi_service.send_uazapi_text(
                    api_url="https://uazapi.customer.example#frag",
                    token="T",
                    phone="5511",
                    message="hi",
                )
            )

    def test_rejects_non_443_port(self) -> None:
        with pytest.raises(UnsafeEgressError):
            asyncio.run(
                uazapi_service.send_uazapi_text(
                    api_url="https://uazapi.customer.example:8443",
                    token="T",
                    phone="5511",
                    message="hi",
                )
            )

    def test_rejects_http_scheme(self) -> None:
        with pytest.raises(UnsafeEgressError):
            asyncio.run(
                uazapi_service.send_uazapi_text(
                    api_url="http://uazapi.customer.example",
                    token="T",
                    phone="5511",
                    message="hi",
                )
            )

    def test_redirect_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302, headers={"Location": "https://elsewhere.example/"}
            )

        with egress_env(make_resolver("1.1.1.1"), httpx.MockTransport(handler)):
            with pytest.raises(UnsafeEgressError) as excinfo:
                asyncio.run(
                    uazapi_service.send_uazapi_text(
                        api_url="https://uazapi.customer.example",
                        token="T",
                        phone="5511",
                        message="hi",
                    )
                )
        assert excinfo.value.code == "redirect_forbidden"

    def test_secret_absent_from_error(self) -> None:
        SECRET = "UAZAPI-SUPER-SECRET-TOKEN-VALUE-XYZ"  # noqa: N806

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "https://x/"})

        with egress_env(make_resolver("1.1.1.1"), httpx.MockTransport(handler)):
            try:
                asyncio.run(
                    uazapi_service.send_uazapi_text(
                        api_url="https://uazapi.customer.example",
                        token=SECRET,
                        phone="5511",
                        message="hi",
                    )
                )
            except UnsafeEgressError as exc:
                assert SECRET not in (repr(exc) + str(exc) + repr(exc.context))
