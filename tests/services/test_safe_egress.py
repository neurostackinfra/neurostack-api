"""Unit tests for :mod:`src.services.safe_egress`.

These tests exhaust the SSRF/DNS/audio matrix without any real network by
injecting a fake DNS resolver and (for the transport tests) an
``httpx.MockTransport``. The invariants under test come straight from the
wish's Group 4 acceptance criteria: HTTPS-only public destinations,
per-provider host allowlists, TOCTOU-safe DNS pinning, no-redirect, audio
MIME allowlist and byte cap, and secret-free error paths.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Any

import httpx
import pytest

from src.services.safe_egress import (
    AUDIO_MIME_ALLOWLIST,
    CUSTOM_PUBLIC_PROVIDERS,
    DEFAULT_MAX_AUDIO_BYTES,
    GEMINI_API_BASE_URL,
    LLM_DROPOUT_HOSTS,
    LLM_GEMINI_HOSTS,
    LLM_OPENAI_HOSTS,
    META_ALLOWED_HOSTS,
    MULTIMODAL_GROQ_HOSTS,
    MULTIMODAL_OPENAI_HOSTS,
    MULTIMODAL_OPENROUTER_HOSTS,
    PROVIDER_POLICIES,
    STT_GROQ_HOSTS,
    STT_OPENAI_HOSTS,
    TTS_ELEVENLABS_HOSTS,
    TTS_GOOGLE_HOSTS,
    TTS_OPENAI_HOSTS,
    ZAPI_ALLOWED_HOSTS,
    ParsedUrl,
    UnsafeEgressError,
    ValidatedRequest,
    build_safe_async_client,
    enforce_content_length,
    enforce_host_allowlist,
    parse_public_https_url,
    resolve_policy,
    resolve_public_ips,
    safe_async_client,
    safe_download_audio,
    validate_audio_mime,
    validate_url,
)

# ---------------------------------------------------------------------------
# Test helpers: fake DNS resolvers so tests never touch real DNS.
# ---------------------------------------------------------------------------


def _addrinfo(ip: str, port: int = 443) -> tuple[Any, ...]:
    """Shape a getaddrinfo entry that matches ``socket.getaddrinfo``."""

    if ":" in ip:
        family = socket.AF_INET6
        sockaddr: tuple[Any, ...] = (ip, port, 0, 0)
    else:
        family = socket.AF_INET
        sockaddr = (ip, port)
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)


def make_resolver(*ips: str):
    """Return a resolver that always yields the given IP list."""

    def _resolver(_hostname: str, port: int) -> list[tuple[Any, ...]]:
        return [_addrinfo(ip, port) for ip in ips]

    return _resolver


def raising_resolver(exc: Exception):
    def _resolver(_hostname: str, _port: int) -> list[tuple[Any, ...]]:
        raise exc

    return _resolver


# ---------------------------------------------------------------------------
# parse_public_https_url — lexical rejection matrix
# ---------------------------------------------------------------------------


class TestParsePublicHttpsUrl:
    def test_accepts_plain_https_with_default_port(self) -> None:
        parsed = parse_public_https_url("https://graph.facebook.com/v18.0/messages")
        assert parsed.scheme == "https"
        assert parsed.hostname == "graph.facebook.com"
        assert parsed.port == 443
        assert parsed.path == "/v18.0/messages"
        assert parsed.query == ""

    def test_accepts_explicit_443(self) -> None:
        parsed = parse_public_https_url("https://api.z-api.io:443/")
        assert parsed.port == 443
        assert parsed.hostname == "api.z-api.io"

    def test_preserves_query_and_path_verbatim(self) -> None:
        parsed = parse_public_https_url("https://host.example/x/y?a=1&b=2")
        assert parsed.path == "/x/y"
        assert parsed.query == "a=1&b=2"

    def test_lowercases_hostname(self) -> None:
        parsed = parse_public_https_url("https://GRAPH.FACEBOOK.COM/foo")
        assert parsed.hostname == "graph.facebook.com"

    def test_rejects_empty_or_non_string(self) -> None:
        with pytest.raises(UnsafeEgressError) as excinfo:
            parse_public_https_url("")
        assert excinfo.value.code == "unsafe_api_url"
        with pytest.raises(UnsafeEgressError):
            parse_public_https_url(None)  # type: ignore[arg-type]

    def test_rejects_non_https_scheme(self) -> None:
        for url in ("http://example.com/", "ftp://example.com/", "file:///etc/passwd"):
            with pytest.raises(UnsafeEgressError) as excinfo:
                parse_public_https_url(url)
            assert excinfo.value.code == "unsafe_api_url"

    def test_rejects_userinfo(self) -> None:
        # ``user:pass@host`` in URL is a classic SSRF exfil vector.
        with pytest.raises(UnsafeEgressError) as excinfo:
            parse_public_https_url("https://alice:secret@example.com/")
        assert excinfo.value.code == "unsafe_api_url"

    def test_rejects_userinfo_username_only(self) -> None:
        with pytest.raises(UnsafeEgressError):
            parse_public_https_url("https://someone@example.com/")

    def test_rejects_fragment(self) -> None:
        with pytest.raises(UnsafeEgressError) as excinfo:
            parse_public_https_url("https://example.com/path#fragment")
        assert excinfo.value.code == "unsafe_api_url"

    def test_rejects_non_443_port(self) -> None:
        with pytest.raises(UnsafeEgressError):
            parse_public_https_url("https://example.com:8443/")
        with pytest.raises(UnsafeEgressError):
            parse_public_https_url("https://example.com:80/")

    def test_rejects_malformed_port(self) -> None:
        with pytest.raises(UnsafeEgressError):
            parse_public_https_url("https://example.com:notaport/")

    def test_rejects_localhost(self) -> None:
        with pytest.raises(UnsafeEgressError) as excinfo:
            parse_public_https_url("https://localhost/foo")
        assert excinfo.value.code == "unsafe_api_url"

    def test_rejects_ipv4_literal_private(self) -> None:
        with pytest.raises(UnsafeEgressError):
            parse_public_https_url("https://10.0.0.1/")
        with pytest.raises(UnsafeEgressError):
            parse_public_https_url("https://192.168.1.1/")
        with pytest.raises(UnsafeEgressError):
            parse_public_https_url("https://172.16.0.5/")

    def test_rejects_ipv4_loopback(self) -> None:
        with pytest.raises(UnsafeEgressError):
            parse_public_https_url("https://127.0.0.1/")

    def test_rejects_ipv4_metadata_service(self) -> None:
        with pytest.raises(UnsafeEgressError):
            parse_public_https_url("https://169.254.169.254/")

    def test_rejects_ipv4_link_local(self) -> None:
        with pytest.raises(UnsafeEgressError):
            parse_public_https_url("https://169.254.10.20/")

    def test_rejects_ipv6_loopback(self) -> None:
        with pytest.raises(UnsafeEgressError):
            parse_public_https_url("https://[::1]/")

    def test_rejects_ipv6_private_ula(self) -> None:
        with pytest.raises(UnsafeEgressError):
            parse_public_https_url("https://[fd00::1]/")

    def test_accepts_public_ipv4_literal(self) -> None:
        parsed = parse_public_https_url("https://8.8.8.8/")
        assert parsed.hostname == "8.8.8.8"

    def test_missing_hostname(self) -> None:
        with pytest.raises(UnsafeEgressError):
            parse_public_https_url("https:///path")


# ---------------------------------------------------------------------------
# enforce_host_allowlist — per-provider pinning
# ---------------------------------------------------------------------------


class TestEnforceHostAllowlist:
    def _parsed(self, host: str) -> ParsedUrl:
        return parse_public_https_url(f"https://{host}/")

    def test_none_allowlist_accepts_any_public_host(self) -> None:
        # ``None`` == custom-public policy: only the DNS/IP filter matters.
        enforce_host_allowlist(self._parsed("evolution.example.net"), None)

    def test_exact_meta_allowlist(self) -> None:
        enforce_host_allowlist(self._parsed("graph.facebook.com"), META_ALLOWED_HOSTS)
        with pytest.raises(UnsafeEgressError):
            enforce_host_allowlist(
                self._parsed("evil.facebook.com.attacker.tld"), META_ALLOWED_HOSTS
            )

    def test_zapi_pinning(self) -> None:
        enforce_host_allowlist(self._parsed("api.z-api.io"), ZAPI_ALLOWED_HOSTS)
        with pytest.raises(UnsafeEgressError):
            enforce_host_allowlist(self._parsed("z-api.io.evil"), ZAPI_ALLOWED_HOSTS)

    def test_openai_and_dropout_and_groq(self) -> None:
        enforce_host_allowlist(self._parsed("api.openai.com"), LLM_OPENAI_HOSTS)
        enforce_host_allowlist(self._parsed("api.neuralake.cloud"), LLM_DROPOUT_HOSTS)
        enforce_host_allowlist(self._parsed("api.groq.com"), STT_GROQ_HOSTS)
        enforce_host_allowlist(self._parsed("api.openai.com"), STT_OPENAI_HOSTS)
        with pytest.raises(UnsafeEgressError):
            enforce_host_allowlist(self._parsed("api.groqcloud.com"), STT_GROQ_HOSTS)

    def test_multimodal_pinning(self) -> None:
        enforce_host_allowlist(
            self._parsed("openrouter.ai"), MULTIMODAL_OPENROUTER_HOSTS
        )
        enforce_host_allowlist(self._parsed("api.groq.com"), MULTIMODAL_GROQ_HOSTS)
        enforce_host_allowlist(self._parsed("api.openai.com"), MULTIMODAL_OPENAI_HOSTS)
        for host, allowlist in (
            ("openrouter.ai.attacker.tld", MULTIMODAL_OPENROUTER_HOSTS),
            ("api.openrouter.ai", MULTIMODAL_OPENROUTER_HOSTS),
            ("api.groqcloud.com", MULTIMODAL_GROQ_HOSTS),
            ("api.openai.com.evil", MULTIMODAL_OPENAI_HOSTS),
        ):
            with pytest.raises(UnsafeEgressError):
                enforce_host_allowlist(self._parsed(host), allowlist)

    def test_tts_pinning(self) -> None:
        enforce_host_allowlist(self._parsed("api.elevenlabs.io"), TTS_ELEVENLABS_HOSTS)
        enforce_host_allowlist(
            self._parsed("texttospeech.googleapis.com"), TTS_GOOGLE_HOSTS
        )
        enforce_host_allowlist(self._parsed("api.openai.com"), TTS_OPENAI_HOSTS)
        with pytest.raises(UnsafeEgressError):
            enforce_host_allowlist(
                self._parsed("api.elevenlabs.io.attacker.tld"),
                TTS_ELEVENLABS_HOSTS,
            )
        with pytest.raises(UnsafeEgressError):
            enforce_host_allowlist(
                self._parsed("api.openai.com.attacker.tld"), TTS_OPENAI_HOSTS
            )


# ---------------------------------------------------------------------------
# resolve_public_ips — the TOCTOU-safe DNS boundary
# ---------------------------------------------------------------------------


class TestResolvePublicIps:
    def test_returns_all_public_ips(self) -> None:
        resolver = make_resolver("93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946")
        ips = resolve_public_ips("example.com", resolver=resolver)
        assert set(ips) == {
            "93.184.216.34",
            "2606:2800:220:1:248:1893:25c8:1946",
        }

    def test_deduplicates(self) -> None:
        resolver = make_resolver("8.8.8.8", "8.8.8.8", "8.8.4.4")
        ips = resolve_public_ips("example.com", resolver=resolver)
        assert ips == ("8.8.8.8", "8.8.4.4")

    def test_rejects_when_any_ip_is_private(self) -> None:
        # Mixed A/AAAA: one public, one private — reject the whole set.
        resolver = make_resolver("8.8.8.8", "10.0.0.5")
        with pytest.raises(UnsafeEgressError) as excinfo:
            resolve_public_ips("example.com", resolver=resolver)
        assert excinfo.value.code == "unsafe_api_url"

    def test_rejects_loopback(self) -> None:
        resolver = make_resolver("127.0.0.1")
        with pytest.raises(UnsafeEgressError):
            resolve_public_ips("example.com", resolver=resolver)

    def test_rejects_metadata_ip(self) -> None:
        resolver = make_resolver("169.254.169.254")
        with pytest.raises(UnsafeEgressError):
            resolve_public_ips("example.com", resolver=resolver)

    def test_rejects_ipv6_metadata_range(self) -> None:
        resolver = make_resolver("fd00:ec2::254")
        with pytest.raises(UnsafeEgressError):
            resolve_public_ips("example.com", resolver=resolver)

    def test_rejects_link_local(self) -> None:
        resolver = make_resolver("169.254.10.10")
        with pytest.raises(UnsafeEgressError):
            resolve_public_ips("example.com", resolver=resolver)
        resolver = make_resolver("fe80::1")
        with pytest.raises(UnsafeEgressError):
            resolve_public_ips("example.com", resolver=resolver)

    def test_rejects_multicast_and_reserved(self) -> None:
        for bad in ("224.0.0.1", "240.0.0.1"):
            resolver = make_resolver(bad)
            with pytest.raises(UnsafeEgressError):
                resolve_public_ips("example.com", resolver=resolver)

    def test_rejects_no_result(self) -> None:
        resolver = make_resolver()  # empty
        with pytest.raises(UnsafeEgressError):
            resolve_public_ips("nowhere.example", resolver=resolver)

    def test_wraps_resolver_exception(self) -> None:
        resolver = raising_resolver(socket.gaierror("boom"))
        with pytest.raises(UnsafeEgressError) as excinfo:
            resolve_public_ips("nowhere.example", resolver=resolver)
        assert excinfo.value.code == "unsafe_api_url"

    def test_ipv6_unique_local_rejected(self) -> None:
        # fc00::/7 is IPv6 ULA (equivalent to RFC1918).
        resolver = make_resolver("fc00::1")
        with pytest.raises(UnsafeEgressError):
            resolve_public_ips("example.com", resolver=resolver)


# ---------------------------------------------------------------------------
# validate_url — end-to-end validation composition
# ---------------------------------------------------------------------------


class TestValidateUrl:
    def test_happy_path_meta(self) -> None:
        resolver = make_resolver("157.240.229.35")
        validated = validate_url(
            "https://graph.facebook.com/v18.0/1234/messages",
            kind="meta",
            resolver=resolver,
        )
        assert isinstance(validated, ValidatedRequest)
        assert validated.parsed.hostname == "graph.facebook.com"
        assert validated.sni_host == "graph.facebook.com"
        assert "157.240.229.35" in validated.pinned_ips
        assert validated.url == "https://graph.facebook.com:443/v18.0/1234/messages"

    def test_rejects_meta_off_host(self) -> None:
        resolver = make_resolver("8.8.8.8")
        with pytest.raises(UnsafeEgressError):
            validate_url(
                "https://api.z-api.io/whatever",
                kind="meta",
                resolver=resolver,
            )

    def test_custom_public_still_dns_checks(self) -> None:
        resolver = make_resolver("10.0.0.5")
        with pytest.raises(UnsafeEgressError):
            validate_url(
                "https://uazapi.customer.example/x",
                kind="uazapi",
                resolver=resolver,
            )

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(UnsafeEgressError):
            resolve_policy("not_a_provider")


class TestProviderPolicies:
    def test_all_operational_have_pinned_hosts(self) -> None:
        assert PROVIDER_POLICIES["meta"].allowed_hosts == META_ALLOWED_HOSTS
        assert PROVIDER_POLICIES["zapi"].allowed_hosts == ZAPI_ALLOWED_HOSTS
        assert PROVIDER_POLICIES["llm_openai"].allowed_hosts == LLM_OPENAI_HOSTS
        assert PROVIDER_POLICIES["llm_dropout"].allowed_hosts == LLM_DROPOUT_HOSTS
        assert PROVIDER_POLICIES["llm_gemini"].allowed_hosts == LLM_GEMINI_HOSTS
        assert PROVIDER_POLICIES["stt_openai"].allowed_hosts == STT_OPENAI_HOSTS
        assert PROVIDER_POLICIES["stt_groq"].allowed_hosts == STT_GROQ_HOSTS

    def test_gemini_policy_pins_dns_host_and_sni(self) -> None:
        seen: list[tuple[str, int]] = []

        def resolver(hostname: str, port: int) -> list[tuple[Any, ...]]:
            seen.append((hostname, port))
            return [_addrinfo("8.8.8.8", port)]

        validated = validate_url(
            f"{GEMINI_API_BASE_URL}/chat/completions",
            kind="llm_gemini",
            resolver=resolver,
        )
        assert seen == [("generativelanguage.googleapis.com", 443)]
        assert validated.pinned_ips == ("8.8.8.8",)
        assert validated.sni_host == "generativelanguage.googleapis.com"

    def test_gemini_policy_rejects_noncanonical_host_before_dns(self) -> None:
        called = False

        def resolver(_hostname: str, _port: int) -> list[tuple[Any, ...]]:
            nonlocal called
            called = True
            return [_addrinfo("8.8.8.8")]

        with pytest.raises(UnsafeEgressError):
            validate_url(
                "https://evil.example/v1beta/openai/chat/completions",
                kind="llm_gemini",
                resolver=resolver,
            )
        assert called is False

    def test_every_multimodal_kind_resolves_with_a_pinned_host(self) -> None:
        # The multimodal credential allowlist has no `outros` entry, so all
        # three providers are known and none may accept an arbitrary host.
        expected = {
            "multimodal_openai": MULTIMODAL_OPENAI_HOSTS,
            "multimodal_groq": MULTIMODAL_GROQ_HOSTS,
            "multimodal_openrouter": MULTIMODAL_OPENROUTER_HOSTS,
        }
        for kind, hosts in expected.items():
            policy = resolve_policy(kind)
            assert policy.kind == kind
            assert policy.allowed_hosts == hosts

    def test_every_tts_kind_resolves_with_a_pinned_host(self) -> None:
        assert resolve_policy("tts_elevenlabs").allowed_hosts == TTS_ELEVENLABS_HOSTS
        assert resolve_policy("tts_google").allowed_hosts == TTS_GOOGLE_HOSTS
        assert resolve_policy("tts_openai").allowed_hosts == TTS_OPENAI_HOSTS

    def test_multimodal_kinds_reject_hosts_outside_the_allowlist(self) -> None:
        resolver = make_resolver("104.18.2.1")
        for kind, good_host in (
            ("multimodal_openrouter", "openrouter.ai"),
            ("multimodal_groq", "api.groq.com"),
            ("multimodal_openai", "api.openai.com"),
        ):
            validated = validate_url(
                f"https://{good_host}/v1/chat/completions",
                kind=kind,
                resolver=resolver,
            )
            assert validated.parsed.hostname == good_host
            with pytest.raises(UnsafeEgressError):
                validate_url(
                    "https://proxy.attacker.tld/v1/chat/completions",
                    kind=kind,
                    resolver=resolver,
                )

    def test_custom_public_kinds_have_no_allowlist(self) -> None:
        for name in ("llm_outros", "stt_outros", "evolution", "uazapi", "audio_url"):
            assert PROVIDER_POLICIES[name].allowed_hosts is None

    def test_no_config_only_provider_registered(self) -> None:
        # Config-only providers (Microsoft and Deepgram) never
        # go through this transport, so they must not have entries here.
        for forbidden in (
            "microsoft",
            "elevenlabs",
            "google",
            "deepgram",
            "deepgram_stt",
            "deepgram_tts",
            "slm_cortex",
        ):
            assert forbidden not in PROVIDER_POLICIES

    def test_custom_public_providers_set_matches_policy(self) -> None:
        for name in CUSTOM_PUBLIC_PROVIDERS:
            # every value in the "custom public" tag maps into a real policy
            resolved_names = {"outros", "evolution", "uazapi", "audio_url"}
            assert name in resolved_names


# ---------------------------------------------------------------------------
# Audio MIME + content-length validators
# ---------------------------------------------------------------------------


class TestValidateAudioMime:
    def test_accepts_audio_wildcard(self) -> None:
        validate_audio_mime("audio/ogg")
        validate_audio_mime("audio/mpeg")
        validate_audio_mime("audio/mp4; codecs=aac")

    def test_accepts_explicit_allowlist(self) -> None:
        for mime in AUDIO_MIME_ALLOWLIST:
            validate_audio_mime(mime)

    def test_rejects_missing(self) -> None:
        for value in ("", None):
            with pytest.raises(UnsafeEgressError) as excinfo:
                validate_audio_mime(value)
            assert excinfo.value.code == "unsupported_audio_mime"

    def test_rejects_disallowed(self) -> None:
        for value in ("image/png", "text/html", "video/mp4", "application/json"):
            with pytest.raises(UnsafeEgressError) as excinfo:
                validate_audio_mime(value)
            assert excinfo.value.code == "unsupported_audio_mime"

    def test_strips_parameters(self) -> None:
        validate_audio_mime("audio/ogg; charset=utf-8")


class TestEnforceContentLength:
    def test_none_or_empty_is_ok(self) -> None:
        enforce_content_length(None, max_bytes=100)
        enforce_content_length("", max_bytes=100)

    def test_within_limit(self) -> None:
        enforce_content_length("42", max_bytes=100)
        enforce_content_length(42, max_bytes=100)

    def test_exactly_at_limit(self) -> None:
        enforce_content_length("100", max_bytes=100)

    def test_over_limit(self) -> None:
        with pytest.raises(UnsafeEgressError) as excinfo:
            enforce_content_length("101", max_bytes=100)
        assert excinfo.value.code == "audio_too_large"

    def test_negative_rejected(self) -> None:
        with pytest.raises(UnsafeEgressError):
            enforce_content_length("-1", max_bytes=100)

    def test_non_numeric_rejected(self) -> None:
        with pytest.raises(UnsafeEgressError):
            enforce_content_length("abc", max_bytes=100)


# ---------------------------------------------------------------------------
# UnsafeEgressError — never carries the caller's secret
# ---------------------------------------------------------------------------


class TestUnsafeEgressErrorRedaction:
    def test_str_is_only_code(self) -> None:
        exc = UnsafeEgressError("unsafe_api_url")
        assert str(exc) == "unsafe_api_url"
        assert exc.code == "unsafe_api_url"
        assert exc.args == ("unsafe_api_url",)

    def test_context_never_contains_secret_by_default(self) -> None:
        # The internal validators never populate context with tokens/keys;
        # even user-supplied context is opt-in.
        exc = UnsafeEgressError("unsafe_api_url")
        assert exc.context == {}

    def test_parse_error_context_is_empty(self) -> None:
        # Simulate the pipeline: caller passes a URL with a secret in
        # userinfo; the error must not include it anywhere.
        SECRET = "sk-live-super-secret-value"  # noqa: N806
        try:
            parse_public_https_url(f"https://user:{SECRET}@host.example/foo")
        except UnsafeEgressError as exc:
            payload = repr(exc) + str(exc) + repr(exc.context)
            assert SECRET not in payload
            assert exc.code == "unsafe_api_url"
        else:  # pragma: no cover - defensive
            pytest.fail("Expected UnsafeEgressError")


# ---------------------------------------------------------------------------
# httpx transport — no-redirect hook and streaming download
# ---------------------------------------------------------------------------


class TestBuildSafeAsyncClient:
    def test_redirect_response_raises(self) -> None:
        validated = ValidatedRequest(
            parsed=ParsedUrl(
                scheme="https",
                hostname="example.com",
                port=443,
                path="/x",
                query="",
            ),
            pinned_ips=("93.184.216.34",),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "https://evil.example/"})

        transport = httpx.MockTransport(handler)

        async def _go():
            async with build_safe_async_client(
                validated, transport=transport
            ) as client:
                with pytest.raises(UnsafeEgressError) as excinfo:
                    await client.get("https://example.com:443/x")
                assert excinfo.value.code == "redirect_forbidden"

        asyncio.run(_go())

    def test_follow_redirects_is_disabled(self) -> None:
        validated = ValidatedRequest(
            parsed=ParsedUrl(
                scheme="https",
                hostname="example.com",
                port=443,
                path="/x",
                query="",
            ),
            pinned_ips=("93.184.216.34",),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)

        async def _go():
            async with build_safe_async_client(
                validated, transport=transport
            ) as client:
                assert client.follow_redirects is False

        asyncio.run(_go())


class TestSafeAsyncClient:
    def test_context_yields_client_and_validated(self) -> None:
        resolver = make_resolver("93.184.216.34")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)

        async def _go():
            async with safe_async_client(
                "https://api.openai.com/v1/chat/completions",
                kind="llm_openai",
                resolver=resolver,
                transport=transport,
            ) as (client, validated):
                assert validated.sni_host == "api.openai.com"
                resp = await client.post(
                    validated.url,
                    json={"hello": "world"},
                    headers={"Host": validated.sni_host},
                )
                assert resp.status_code == 200

        asyncio.run(_go())

    def test_rejects_invalid_url_before_transport(self) -> None:
        # A URL that fails the lexical/DNS check must raise before any
        # transport call is made.
        transport_calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            transport_calls.append(request)
            return httpx.Response(200)

        transport = httpx.MockTransport(handler)

        async def _go():
            with pytest.raises(UnsafeEgressError):
                async with safe_async_client(
                    "http://api.openai.com/v1/x",
                    kind="llm_openai",
                    resolver=make_resolver("8.8.8.8"),
                    transport=transport,
                ):
                    pass  # pragma: no cover
            assert transport_calls == []

        asyncio.run(_go())


# ---------------------------------------------------------------------------
# safe_download_audio — MIME, size cap, streaming
# ---------------------------------------------------------------------------


class TestSafeDownloadAudio:
    def _resolver(self):
        return make_resolver("93.184.216.34")

    def test_happy_path(self) -> None:
        resolver = self._resolver()
        body = b"OggS\x00" + b"\x00" * 100

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "audio/ogg",
                    "Content-Length": str(len(body)),
                },
                content=body,
            )

        transport = httpx.MockTransport(handler)

        async def _go():
            result = await safe_download_audio(
                "https://cdn.example/media/abc",
                resolver=resolver,
                transport=transport,
            )
            assert result.content == body
            assert result.mime_type == "audio/ogg"

        asyncio.run(_go())

    def test_rejects_bad_mime(self) -> None:
        resolver = self._resolver()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                content=b"<html/>",
            )

        transport = httpx.MockTransport(handler)

        async def _go():
            with pytest.raises(UnsafeEgressError) as excinfo:
                await safe_download_audio(
                    "https://cdn.example/media/abc",
                    resolver=resolver,
                    transport=transport,
                )
            assert excinfo.value.code == "unsupported_audio_mime"

        asyncio.run(_go())

    def test_rejects_over_content_length(self) -> None:
        resolver = self._resolver()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={
                    "Content-Type": "audio/ogg",
                    "Content-Length": str(2 * DEFAULT_MAX_AUDIO_BYTES),
                },
                content=b"",
            )

        transport = httpx.MockTransport(handler)

        async def _go():
            with pytest.raises(UnsafeEgressError) as excinfo:
                await safe_download_audio(
                    "https://cdn.example/media/abc",
                    resolver=resolver,
                    transport=transport,
                )
            assert excinfo.value.code == "audio_too_large"

        asyncio.run(_go())

    def test_streaming_overflow_rejected(self) -> None:
        # No Content-Length header, but the streamed body exceeds max_bytes.
        resolver = self._resolver()
        oversized = b"a" * 200

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "audio/mpeg"},
                content=oversized,
            )

        transport = httpx.MockTransport(handler)

        async def _go():
            with pytest.raises(UnsafeEgressError) as excinfo:
                await safe_download_audio(
                    "https://cdn.example/media/abc",
                    max_bytes=100,
                    resolver=resolver,
                    transport=transport,
                )
            assert excinfo.value.code == "audio_too_large"

        asyncio.run(_go())

    def test_http_error_becomes_audio_download_failed(self) -> None:
        resolver = self._resolver()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, content=b"nope")

        transport = httpx.MockTransport(handler)

        async def _go():
            with pytest.raises(UnsafeEgressError) as excinfo:
                await safe_download_audio(
                    "https://cdn.example/media/abc",
                    resolver=resolver,
                    transport=transport,
                )
            assert excinfo.value.code == "audio_download_failed"

        asyncio.run(_go())

    def test_timeout_becomes_audio_download_failed(self) -> None:
        resolver = self._resolver()

        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timeout", request=request)

        transport = httpx.MockTransport(handler)

        async def _go():
            with pytest.raises(UnsafeEgressError) as excinfo:
                await safe_download_audio(
                    "https://cdn.example/media/abc",
                    resolver=resolver,
                    transport=transport,
                )
            assert excinfo.value.code == "audio_download_failed"

        asyncio.run(_go())

    def test_redirect_is_forbidden(self) -> None:
        resolver = self._resolver()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                302,
                headers={"Location": "https://elsewhere.example/whatever"},
            )

        transport = httpx.MockTransport(handler)

        async def _go():
            with pytest.raises(UnsafeEgressError) as excinfo:
                await safe_download_audio(
                    "https://cdn.example/media/abc",
                    resolver=resolver,
                    transport=transport,
                )
            assert excinfo.value.code == "redirect_forbidden"

        asyncio.run(_go())

    def test_secret_absent_from_error(self) -> None:
        resolver = self._resolver()

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                headers={"Content-Type": "text/html"},
                content=b"<html/>",
            )

        transport = httpx.MockTransport(handler)

        SECRET = "eyJhbGciOiJI-SUPER-SECRET-VALUE"  # noqa: N806

        async def _go():
            try:
                await safe_download_audio(
                    "https://cdn.example/media/abc",
                    resolver=resolver,
                    transport=transport,
                    headers={"Authorization": f"Bearer {SECRET}"},
                )
            except UnsafeEgressError as exc:
                payload = repr(exc) + str(exc) + repr(exc.context)
                assert SECRET not in payload

        asyncio.run(_go())


# ---------------------------------------------------------------------------
# DNS rebinding — validating twice must not happen at the transport layer
# ---------------------------------------------------------------------------


class TestDnsRebindingBoundary:
    """Prove ``validate_url`` resolves once and hands back the ordered IPs.

    A caller (or an attacker who controls DNS) that flips the record between
    check and connect must not affect the transport: the transport uses the
    IPs already validated by ``validate_url`` and never re-resolves.
    """

    def test_resolver_called_exactly_once(self) -> None:
        call_count = 0
        results = ["8.8.8.8", "127.0.0.1"]

        def flipping(_hostname: str, port: int) -> list[tuple[Any, ...]]:
            nonlocal call_count
            call_count += 1
            ip = results[min(call_count - 1, len(results) - 1)]
            return [_addrinfo(ip, port)]

        validated = validate_url(
            "https://graph.facebook.com/x",
            kind="meta",
            resolver=flipping,
        )
        assert validated.pinned_ips == ("8.8.8.8",)
        assert call_count == 1

    def test_second_call_would_have_failed(self) -> None:
        # Belt-and-suspenders: if the transport ever re-resolved, the
        # follow-up would fail closed anyway.
        call_count = 0

        def flipping(_hostname: str, port: int) -> list[tuple[Any, ...]]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return [_addrinfo("8.8.8.8", port)]
            return [_addrinfo("127.0.0.1", port)]

        validated = validate_url(
            "https://graph.facebook.com/x",
            kind="meta",
            resolver=flipping,
        )
        assert validated.pinned_ips == ("8.8.8.8",)
        with pytest.raises(UnsafeEgressError):
            resolve_public_ips("graph.facebook.com", resolver=flipping)


# ---------------------------------------------------------------------------
# ValidatedRequest.url — round-trip preserves path/query
# ---------------------------------------------------------------------------


class TestPinnedNetworkBackendTransport:
    """Prove the production transport dials the pre-validated pinned IP.

    A DNS attacker with a short TTL could otherwise flip the resolution
    between :func:`resolve_public_ips` (validation) and the actual TCP
    connect. The httpcore backend must therefore never trigger a second
    ``getaddrinfo`` — it must dial the IP that ``ValidatedRequest`` already
    pinned.
    """

    def test_backend_rewrites_host_to_pinned_ip(self) -> None:
        from src.services import safe_egress as _egress

        recorded: dict[str, Any] = {}

        class _FakeStream:
            async def aclose(self) -> None:
                return None

        class _RecordingBackend:
            async def connect_tcp(
                self,
                host: str,
                port: int,
                timeout: float | None = None,
                local_address: str | None = None,
                socket_options: Any = None,
            ) -> Any:
                recorded["host"] = host
                recorded["port"] = port
                return _FakeStream()

            async def connect_unix_socket(
                self, *args: Any, **kwargs: Any
            ) -> Any:  # pragma: no cover - not exercised
                raise AssertionError("UDS must never be dialed")

            async def sleep(self, seconds: float) -> None:  # pragma: no cover
                return None

        backend = _egress.PinnedAsyncNetworkBackend(
            ("198.51.100.1",),  # TEST-NET-2 (RFC5737), globally routable but reserved
            inner=_RecordingBackend(),
        )

        async def _go() -> None:
            await backend.connect_tcp(host="graph.facebook.com", port=443)

        asyncio.run(_go())
        assert recorded == {"host": "198.51.100.1", "port": 443}

    def test_backend_rejects_uds(self) -> None:
        from src.services import safe_egress as _egress

        backend = _egress.PinnedAsyncNetworkBackend(("198.51.100.1",))

        async def _go() -> None:
            with pytest.raises(UnsafeEgressError) as excinfo:
                await backend.connect_unix_socket("/tmp/socket")
            assert excinfo.value.code == "unsafe_api_url"

        asyncio.run(_go())

    def test_backend_requires_pinned_ip(self) -> None:
        from src.services import safe_egress as _egress

        with pytest.raises(UnsafeEgressError):
            _egress.PinnedAsyncNetworkBackend(())

    def test_client_installs_pinned_backend_when_no_transport(self) -> None:
        # When production callers omit ``transport``, the returned client
        # must have a pinned httpcore backend attached — no second DNS
        # resolution can happen at connect time.
        from src.services import safe_egress as _egress

        validated = ValidatedRequest(
            parsed=ParsedUrl(
                scheme="https",
                hostname="graph.facebook.com",
                port=443,
                path="/v18.0/messages",
                query="",
            ),
            pinned_ips=("198.51.100.1",),
        )
        client = _egress.build_safe_async_client(validated)
        try:
            transport = client._transport
            assert isinstance(transport, httpx.AsyncHTTPTransport)
            backend = transport._pool._network_backend
            assert isinstance(backend, _egress.PinnedAsyncNetworkBackend)
            assert backend._pinned_ips == ("198.51.100.1",)
        finally:
            asyncio.run(client.aclose())

    def test_end_to_end_connect_uses_pinned_ip_not_getaddrinfo(self) -> None:
        # Full stack: patch resolve_public_ips to return a TEST-NET IP,
        # patch the inner network backend to record the host argument,
        # and assert the recorded host equals the pinned IP (never the
        # hostname).
        from src.services import safe_egress as _egress

        # Track calls: the real getaddrinfo would be a second resolution.
        getaddrinfo_calls: list[str] = []

        def _resolver(hostname: str, port: int) -> list[tuple[Any, ...]]:
            getaddrinfo_calls.append(hostname)
            return [_addrinfo("157.240.229.35", port)]

        recorded: dict[str, Any] = {}

        class _FakeStream:
            async def start_tls(self, **kwargs: Any) -> Any:
                # httpcore normally does TLS here; return self so the
                # connect_tcp caller can proceed without a real socket.
                return self

            async def aclose(self) -> None:
                return None

            async def read(self, max_bytes: int, timeout: Any = None) -> bytes:
                return b""

            async def write(self, buf: bytes, timeout: Any = None) -> None:
                return None

            def get_extra_info(self, info: str) -> Any:
                return None

        class _RecordingBackend:
            async def connect_tcp(
                self,
                host: str,
                port: int,
                timeout: float | None = None,
                local_address: str | None = None,
                socket_options: Any = None,
            ) -> Any:
                recorded["host"] = host
                recorded["port"] = port
                return _FakeStream()

            async def connect_unix_socket(self, *args: Any, **kwargs: Any) -> Any:
                raise AssertionError("UDS must never be dialed")

            async def sleep(self, seconds: float) -> None:
                return None

        validated = _egress.validate_url(
            "https://graph.facebook.com/v18.0/messages",
            kind="meta",
            resolver=_resolver,
        )
        assert getaddrinfo_calls == ["graph.facebook.com"]

        client = _egress.build_safe_async_client(validated)
        # Swap the inner (real) backend with our recording backend so we
        # can assert what host is dialed without opening a socket.
        transport = client._transport
        assert isinstance(transport, httpx.AsyncHTTPTransport)
        pinned = transport._pool._network_backend
        assert isinstance(pinned, _egress.PinnedAsyncNetworkBackend)
        pinned._inner = _RecordingBackend()

        async def _go() -> None:
            try:
                await pinned.connect_tcp(host="graph.facebook.com", port=443)
            finally:
                await client.aclose()

        asyncio.run(_go())

        # Exactly one DNS resolution at validation time; the transport
        # dialed the pinned IP directly (no second resolution).
        assert getaddrinfo_calls == ["graph.facebook.com"]
        assert recorded["host"] == "157.240.229.35"
        assert recorded["port"] == 443


class TestValidatedRequestUrl:
    def test_url_with_query(self) -> None:
        parsed = parse_public_https_url("https://host.example/path?a=1&b=2")
        req = ValidatedRequest(parsed=parsed, pinned_ips=("8.8.8.8",))
        assert req.url == "https://host.example:443/path?a=1&b=2"
        assert req.sni_host == "host.example"

    def test_url_without_query(self) -> None:
        parsed = parse_public_https_url("https://host.example/only")
        req = ValidatedRequest(parsed=parsed, pinned_ips=("8.8.8.8",))
        assert req.url == "https://host.example:443/only"
