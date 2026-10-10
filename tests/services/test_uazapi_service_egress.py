"""Egress-boundary tests for :mod:`src.services.uazapi_service`.

Uazapi is self-hosted, so the wish's egress policy is ``custom_public``:
any public HTTPS host, but the DNS/IP filter still runs. These tests
prove the private/metadata/link-local matrix is refused and that a valid
public host is honoured.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from contextlib import contextmanager
from typing import Any, Coroutine
from unittest.mock import MagicMock

import httpx
import pytest

from src.services import safe_egress, uazapi_service
from src.services.egress_policy import EgressDisabledError, EgressPolicy
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


@pytest.fixture(autouse=True)
def _enable_uazapi_gate_for_preexisting_transport_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def allow_uazapi_egress() -> None:
        return None

    monkeypatch.setattr(
        uazapi_service,
        "require_uazapi_egress",
        allow_uazapi_egress,
    )


class _FlagReader:
    def __init__(self, result: object = False, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[str] = []

    async def is_enabled(self, flag_name: str) -> bool:
        self.calls.append(flag_name)
        if self.error is not None:
            raise self.error
        return self.result  # type: ignore[return-value]


def _invoke_operation(operation: str) -> Coroutine[Any, Any, object]:
    if operation == "text":
        return uazapi_service.send_uazapi_text(
            "https://uazapi.customer.example", "TOKEN", "5511", "hello"
        )
    if operation == "audio":
        return uazapi_service.send_uazapi_audio(
            "https://uazapi.customer.example", "TOKEN", "5511", b"audio"
        )
    return uazapi_service.download_uazapi_media(
        "https://uazapi.customer.example", "TOKEN", "message-1"
    )


def _install_enabled_policy_and_safe_client_spy(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_FlagReader, list[str]]:
    reader = _FlagReader(True)
    safe_client_calls: list[str] = []
    real_safe_async_client = uazapi_service.safe_async_client

    def counted_safe_async_client(
        url: str,
        **kwargs: object,
    ) -> Any:
        safe_client_calls.append(url)
        return real_safe_async_client(url, **kwargs)

    monkeypatch.setattr(
        uazapi_service,
        "require_uazapi_egress",
        EgressPolicy(reader).require_uazapi_egress,
    )
    monkeypatch.setattr(
        uazapi_service,
        "safe_async_client",
        counted_safe_async_client,
    )
    return reader, safe_client_calls


@pytest.mark.asyncio
async def test_enabled_real_policy_allows_one_safe_text_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, safe_client_calls = _install_enabled_policy_and_safe_client_spy(monkeypatch)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"MessageID": "MID.enabled"})

    with egress_env(make_resolver("1.1.1.1"), httpx.MockTransport(handler)):
        result = await uazapi_service.send_uazapi_text(
            "https://uazapi.customer.example",
            "TEXT-TOKEN",
            "5511",
            "olá",
        )

    assert result == {"MessageID": "MID.enabled"}
    assert reader.calls == ["uazapi_egress_enabled"]
    assert safe_client_calls == ["https://uazapi.customer.example/send/text"]
    assert len(seen) == 1
    assert seen[0].url.path == "/send/text"
    assert seen[0].headers["Token"] == "TEXT-TOKEN"
    assert seen[0].headers["Host"] == "uazapi.customer.example"
    assert json.loads(seen[0].content) == {"number": "5511", "text": "olá"}


@pytest.mark.asyncio
async def test_enabled_real_policy_allows_one_safe_audio_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, safe_client_calls = _install_enabled_policy_and_safe_client_spy(monkeypatch)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "owner:MID.enabled"})

    with egress_env(make_resolver("1.1.1.1"), httpx.MockTransport(handler)):
        result = await uazapi_service.send_uazapi_audio(
            "https://uazapi.customer.example",
            "AUDIO-TOKEN",
            "5511",
            b"mp3",
        )

    assert result == {"id": "owner:MID.enabled"}
    assert reader.calls == ["uazapi_egress_enabled"]
    assert safe_client_calls == ["https://uazapi.customer.example/send/media"]
    assert len(seen) == 1
    assert seen[0].url.path == "/send/media"
    assert seen[0].headers["Token"] == "AUDIO-TOKEN"
    assert seen[0].headers["Host"] == "uazapi.customer.example"
    assert json.loads(seen[0].content) == {
        "number": "5511",
        "type": "ptt",
        "file": "data:audio/mpeg;base64,bXAz",
        "mimetype": "audio/mpeg",
    }


@pytest.mark.asyncio
async def test_enabled_real_policy_allows_one_safe_download_effect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader, safe_client_calls = _install_enabled_policy_and_safe_client_spy(monkeypatch)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "base64Data": base64.b64encode(b"OggS").decode("ascii"),
                "mimetype": "audio/ogg; codecs=opus",
            },
        )

    with egress_env(make_resolver("1.1.1.1"), httpx.MockTransport(handler)):
        result = await uazapi_service.download_uazapi_media(
            "https://uazapi.customer.example/",
            "DOWNLOAD-TOKEN",
            "message-enabled",
        )

    assert result == (b"OggS", "audio/ogg; codecs=opus")
    assert reader.calls == ["uazapi_egress_enabled"]
    assert safe_client_calls == ["https://uazapi.customer.example/message/download"]
    assert len(seen) == 1
    assert seen[0].url.path == "/message/download"
    assert seen[0].headers["Token"] == "DOWNLOAD-TOKEN"
    assert seen[0].headers["Host"] == "uazapi.customer.example"
    assert json.loads(seen[0].content) == {
        "id": "message-enabled",
        "return_base64": True,
        "return_link": False,
        "generate_mp3": False,
    }


@pytest.mark.parametrize("operation", ["text", "audio", "download"])
@pytest.mark.parametrize(
    "mode",
    ["false", "invalid", "agent-error"],
    ids=["false", "invalid", "agent-error"],
)
@pytest.mark.asyncio
async def test_gate_fails_before_safe_client_dns_socket_or_http(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    mode: str,
) -> None:
    reader = (
        _FlagReader(error=RuntimeError("agent_unavailable"))
        if mode == "agent-error"
        else _FlagReader("true" if mode == "invalid" else False)
    )
    network_calls: list[str] = []

    def forbidden_safe_client(*_args: object, **_kwargs: object) -> Any:
        network_calls.append("safe_async_client")
        raise AssertionError("safe client reached")

    def forbidden_dns(*_args: object, **_kwargs: object) -> Any:
        network_calls.append("dns")
        raise AssertionError("DNS reached")

    def forbidden_socket(*_args: object, **_kwargs: object) -> Any:
        network_calls.append("socket")
        raise AssertionError("socket reached")

    class ForbiddenAsyncClient:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            network_calls.append("http")
            raise AssertionError("HTTP client reached")

    monkeypatch.setattr(
        uazapi_service,
        "require_uazapi_egress",
        EgressPolicy(reader).require_uazapi_egress,
    )
    monkeypatch.setattr(uazapi_service, "safe_async_client", forbidden_safe_client)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden_dns)
    monkeypatch.setattr(socket, "socket", forbidden_socket)
    monkeypatch.setattr(httpx, "AsyncClient", ForbiddenAsyncClient)

    with pytest.raises(EgressDisabledError) as exc_info:
        await _invoke_operation(operation)

    assert exc_info.value.code == "uazapi_egress_disabled"
    assert reader.calls == ["uazapi_egress_enabled"]
    assert network_calls == []


def test_uazapi_gate_is_independent_from_financial_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IndependentFlags:
        calls: list[str] = []

        async def is_enabled(self, flag_name: str) -> bool:
            self.calls.append(flag_name)
            return {"financial_egress_enabled": True, "uazapi_egress_enabled": False}[
                flag_name
            ]

    reader = IndependentFlags()
    safe_client = MagicMock()
    monkeypatch.setattr(
        uazapi_service,
        "require_uazapi_egress",
        EgressPolicy(reader).require_uazapi_egress,
    )
    monkeypatch.setattr(uazapi_service, "safe_async_client", safe_client)

    with pytest.raises(EgressDisabledError):
        asyncio.run(_invoke_operation("text"))

    assert reader.calls == ["uazapi_egress_enabled"]
    safe_client.assert_not_called()


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
