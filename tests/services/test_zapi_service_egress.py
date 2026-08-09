"""Egress-boundary tests for :mod:`src.services.zapi_service`.

Z-API is pinned to ``api.z-api.io`` exactly; Evolution uses the
custom-public policy (any public HTTPS host, DNS/IP filter still applies).
These tests exercise both branches through the real service functions,
proving the egress path via an in-memory ``httpx.MockTransport``.
"""

from __future__ import annotations

import asyncio
import socket
from contextlib import contextmanager
from typing import Any

import httpx
import pytest

from src.services import safe_egress, zapi_service
from src.services.safe_egress import UnsafeEgressError

ZAPI_PUBLIC_IP = "104.21.42.10"


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


class TestSendTextMessage:
    def test_pins_zapi_host(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["host"] = request.headers.get("Host")
            captured["client_token"] = request.headers.get("Client-Token")
            return httpx.Response(200, json={"zaapId": "abc"})

        with egress_env(make_resolver(ZAPI_PUBLIC_IP), httpx.MockTransport(handler)):
            data = asyncio.run(
                zapi_service.send_text_message(
                    instance_id="INSTANCE1",
                    token="TOKENSECRET",
                    phone="5511999999999",
                    message="olá",
                    client_token="CLIENT-TOKEN-SECRET",
                )
            )
        assert data == {"zaapId": "abc"}
        assert "api.z-api.io" in captured["url"]
        assert "/instances/INSTANCE1/token/TOKENSECRET/send-text" in captured["url"]
        assert captured["host"] == "api.z-api.io"
        assert captured["client_token"] == "CLIENT-TOKEN-SECRET"

    def test_rejects_dns_pointing_to_metadata(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("transport must not be reached")

        with egress_env(make_resolver("169.254.169.254"), httpx.MockTransport(handler)):
            with pytest.raises(UnsafeEgressError) as excinfo:
                asyncio.run(
                    zapi_service.send_text_message(
                        instance_id="I",
                        token="T",
                        phone="+5511",
                        message="hi",
                    )
                )
        assert excinfo.value.code == "unsafe_api_url"

    def test_mixed_ips_rejected(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("transport must not be reached")

        with egress_env(
            make_resolver(ZAPI_PUBLIC_IP, "10.0.0.5"),
            httpx.MockTransport(handler),
        ):
            with pytest.raises(UnsafeEgressError) as excinfo:
                asyncio.run(zapi_service.send_text_message("I", "T", "551199", "hi"))
        assert excinfo.value.code == "unsafe_api_url"


class TestMarkConversationUnread:
    def test_fire_and_forget_returns_false_on_egress_error(self) -> None:
        # Egress rejection should not raise from this fire-and-forget helper.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        with egress_env(make_resolver("192.168.1.1"), httpx.MockTransport(handler)):
            result = asyncio.run(
                zapi_service.mark_conversation_unread(
                    instance_id="I",
                    token="T",
                    phone="+5511",
                )
            )
        assert result is False

    def test_happy_path(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        with egress_env(make_resolver(ZAPI_PUBLIC_IP), httpx.MockTransport(handler)):
            result = asyncio.run(
                zapi_service.mark_conversation_unread("I", "T", "+5511")
            )
        assert result is True


class TestMarkMessageRead:
    def test_happy_path(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        with egress_env(make_resolver(ZAPI_PUBLIC_IP), httpx.MockTransport(handler)):
            result = asyncio.run(
                zapi_service.mark_message_read("I", "T", "+5511", "MSGID")
            )
        assert result is True

    def test_returns_false_on_dns_failure(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200)

        with egress_env(make_resolver("127.0.0.1"), httpx.MockTransport(handler)):
            result = asyncio.run(
                zapi_service.mark_message_read("I", "T", "+5511", "MSGID")
            )
        assert result is False


class TestSendEvolutionText:
    def test_custom_public_host_accepted(self) -> None:
        # Evolution uses the ``custom_public`` policy: any public HTTPS host
        # is accepted as long as DNS points to public unicast IPs.
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["apikey"] = request.headers.get("apikey")
            return httpx.Response(200, json={"status": "sent"})

        with egress_env(make_resolver("1.1.1.1"), httpx.MockTransport(handler)):
            data = asyncio.run(
                zapi_service.send_evolution_text(
                    api_url="https://evo.customer.example",
                    instance_name="inst",
                    api_key="EVOLUTIONKEY",
                    phone="5511",
                    message="hi",
                )
            )
        assert data == {"status": "sent"}
        assert "evo.customer.example" in captured["url"]
        assert captured["apikey"] == "EVOLUTIONKEY"

    def test_rejects_private_dns_for_evolution(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("transport must not be reached")

        with egress_env(make_resolver("10.0.0.5"), httpx.MockTransport(handler)):
            with pytest.raises(UnsafeEgressError) as excinfo:
                asyncio.run(
                    zapi_service.send_evolution_text(
                        api_url="https://evo.customer.example",
                        instance_name="inst",
                        api_key="KEY",
                        phone="5511",
                        message="hi",
                    )
                )
        assert excinfo.value.code == "unsafe_api_url"

    def test_rejects_http_scheme_for_evolution(self) -> None:
        with pytest.raises(UnsafeEgressError):
            asyncio.run(
                zapi_service.send_evolution_text(
                    api_url="http://evo.customer.example",
                    instance_name="inst",
                    api_key="KEY",
                    phone="5511",
                    message="hi",
                )
            )

    def test_evolution_secret_not_in_error(self) -> None:
        SECRET = "EVOLUTION-key-1234567890"  # noqa: N806

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "https://x/"})

        with egress_env(make_resolver("1.1.1.1"), httpx.MockTransport(handler)):
            try:
                asyncio.run(
                    zapi_service.send_evolution_text(
                        api_url="https://evo.customer.example",
                        instance_name="inst",
                        api_key=SECRET,
                        phone="5511",
                        message="hi",
                    )
                )
            except UnsafeEgressError as exc:
                assert SECRET not in (repr(exc) + str(exc) + repr(exc.context))


class TestZapiCapabilityTokenRedaction:
    def test_error_never_carries_zapi_token(self) -> None:
        # The token is embedded in the URL path. Ensure any UnsafeEgressError
        # never carries the token verbatim.
        TOKEN = "capability-token-secret-XYZ-1234567890"  # noqa: N806

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "https://x/"})

        with egress_env(make_resolver(ZAPI_PUBLIC_IP), httpx.MockTransport(handler)):
            try:
                asyncio.run(
                    zapi_service.send_text_message(
                        instance_id="I",
                        token=TOKEN,
                        phone="+5511",
                        message="hi",
                    )
                )
            except UnsafeEgressError as exc:
                payload = repr(exc) + str(exc) + repr(exc.context)
                assert TOKEN not in payload
