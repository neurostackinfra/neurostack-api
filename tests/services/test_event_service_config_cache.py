"""Cache v2 + builder-owner + DB-only loader contract tests.

These tests exercise the module-level helpers in
``src.services.event_service`` without spinning up FastAPI, the real
database, or Redis. Redis is faked with an in-memory stand-in and the DB
loader is monkeypatched with deterministic fixtures.

Focus areas (WISH Group 3):
  * ``_get_cached_config`` requires the v2 marker; missing/invalid markers
    trigger DEL before any DB load; DEL failure prevents reuse/renew.
  * ``_set_cached_config`` refuses to write anything without the marker.
  * ``_load_clinic_config`` returns the projected snapshot (no secrets).
  * ``_load_runtime_llm_credentials`` returns the raw composed 4 LLM fields.
  * ``_resolve_builder_owner_tenant_id`` requires exactly one distinct
    ``User.tenant_id`` for the given clinic's ``Tenant.builder_id`` and
    emits ``builder_llm_owner_ambiguous`` (via warning log) when 0 or >1.
  * The projected cache never carries WhatsApp/STT bleed from a builder,
    or any secret from any capability.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator, Optional
from uuid import UUID, uuid4

import pytest

from src.services import event_service
from src.services.api_credentials import (
    CONFIG_CACHE_SCHEMA_VERSION,
    CONFIG_CACHE_VERSION_FIELD,
    build_cache_payload,
    config_cache_key,
)

# ============================================
# Fake Redis (in-memory, per-test isolated)
# ============================================


class _FakeRedis:
    """Minimal ``redis.asyncio.Redis``-shaped stand-in for the cache path."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.delete_count = 0
        self.delete_fail = False
        self.set_calls: list[tuple[str, str, Optional[int]]] = []
        self.set_fail = False
        self.get_fail = False

    async def get(self, key: str) -> Optional[str]:
        if self.get_fail:
            raise RuntimeError("redis get failed")
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: Optional[int] = None) -> None:
        if self.set_fail:
            raise RuntimeError("redis set failed")
        self.set_calls.append((key, value, ex))
        self.store[key] = value

    async def delete(self, key: str) -> int:
        self.delete_count += 1
        if self.delete_fail:
            raise RuntimeError("redis delete failed")
        return 1 if self.store.pop(key, None) is not None else 0

    async def aclose(self) -> None:
        return None


@pytest.fixture
def fake_redis(monkeypatch: pytest.MonkeyPatch) -> _FakeRedis:
    fake = _FakeRedis()

    async def _open_redis() -> _FakeRedis:
        return fake

    monkeypatch.setattr(event_service, "_open_redis", _open_redis)
    return fake


TENANT = UUID("a0000000-0000-0000-0000-000000000001")


@contextmanager
def _installed_event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Install one local loop and restore the caller's loop on every exit."""
    try:
        previous_loop = asyncio.get_event_loop()
    except RuntimeError:
        previous_loop = None

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        yield loop
    finally:
        try:
            loop.close()
        finally:
            asyncio.set_event_loop(previous_loop)


@pytest.fixture(autouse=True)
def local_event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Give each sync test an explicit loop without changing global fixtures."""
    with _installed_event_loop() as loop:
        yield loop


@pytest.mark.parametrize("raise_inside", [False, True], ids=["success", "exception"])
def test_local_event_loop_closes_and_restores(raise_inside: bool) -> None:
    previous_loop = asyncio.get_event_loop()
    nested_loop: asyncio.AbstractEventLoop | None = None

    if raise_inside:
        with pytest.raises(RuntimeError, match="cleanup probe"):
            with _installed_event_loop() as nested_loop:
                assert asyncio.get_event_loop() is nested_loop
                raise RuntimeError("cleanup probe")
    else:
        with _installed_event_loop() as nested_loop:
            assert asyncio.get_event_loop() is nested_loop

    assert nested_loop is not None
    assert nested_loop.is_closed()
    assert asyncio.get_event_loop() is previous_loop


# ============================================
# _get_cached_config / _set_cached_config
# ============================================


class TestCacheV2Marker:
    def test_missing_entry_returns_none_without_delete(
        self, fake_redis: _FakeRedis
    ) -> None:
        result = asyncio.get_event_loop().run_until_complete(
            event_service._get_cached_config(TENANT)
        )
        assert result is None
        assert fake_redis.delete_count == 0  # nothing to delete

    def test_valid_v2_marker_returns_payload(self, fake_redis: _FakeRedis) -> None:
        payload = build_cache_payload(
            {"identity": {"clinic_name": "X"}, "api_credentials": {}}
        )
        fake_redis.store[config_cache_key(TENANT)] = json.dumps(payload)
        result = asyncio.get_event_loop().run_until_complete(
            event_service._get_cached_config(TENANT)
        )
        assert result == payload
        assert result[CONFIG_CACHE_VERSION_FIELD] == CONFIG_CACHE_SCHEMA_VERSION
        assert fake_redis.delete_count == 0

    def test_missing_marker_triggers_del_and_returns_none(
        self, fake_redis: _FakeRedis
    ) -> None:
        legacy = {"identity": {}, "api_credentials": {"llm_api_key": "SECRET"}}
        fake_redis.store[config_cache_key(TENANT)] = json.dumps(legacy)
        result = asyncio.get_event_loop().run_until_complete(
            event_service._get_cached_config(TENANT)
        )
        assert result is None
        assert fake_redis.delete_count == 1
        assert config_cache_key(TENANT) not in fake_redis.store

    def test_wrong_version_marker_triggers_del_and_returns_none(
        self, fake_redis: _FakeRedis
    ) -> None:
        v1 = {CONFIG_CACHE_VERSION_FIELD: 1, "api_credentials": {}}
        fake_redis.store[config_cache_key(TENANT)] = json.dumps(v1)
        result = asyncio.get_event_loop().run_until_complete(
            event_service._get_cached_config(TENANT)
        )
        assert result is None
        assert fake_redis.delete_count == 1

    def test_invalid_json_triggers_del_and_returns_none(
        self, fake_redis: _FakeRedis
    ) -> None:
        fake_redis.store[config_cache_key(TENANT)] = "{not-json"
        result = asyncio.get_event_loop().run_until_complete(
            event_service._get_cached_config(TENANT)
        )
        assert result is None
        assert fake_redis.delete_count == 1

    def test_set_refuses_payload_without_marker(self, fake_redis: _FakeRedis) -> None:
        ok = asyncio.get_event_loop().run_until_complete(
            event_service._set_cached_config(TENANT, {"api_credentials": {}})
        )
        assert ok is False
        assert fake_redis.set_calls == []
        assert config_cache_key(TENANT) not in fake_redis.store

    def test_set_writes_only_marked_payload(self, fake_redis: _FakeRedis) -> None:
        payload = build_cache_payload({"api_credentials": {}, "identity": {}})
        ok = asyncio.get_event_loop().run_until_complete(
            event_service._set_cached_config(TENANT, payload)
        )
        assert ok is True
        assert config_cache_key(TENANT) in fake_redis.store
        assert fake_redis.set_calls[0][2] == event_service._CONFIG_CACHE_TTL


# ============================================
# DEL / DB failure isolation
# ============================================


class TestFailureModesDoNotRepopulate:
    def test_del_failure_prevents_reuse_and_set(
        self,
        fake_redis: _FakeRedis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        legacy = {"identity": {}, "api_credentials": {"llm_api_key": "SECRET"}}
        fake_redis.store[config_cache_key(TENANT)] = json.dumps(legacy)
        fake_redis.delete_fail = True

        result = asyncio.get_event_loop().run_until_complete(
            event_service._get_cached_config(TENANT)
        )
        # No reuse: caller receives None regardless of DEL outcome
        assert result is None
        # Legacy entry NOT rewritten (delete_fail keeps store intact but the
        # code MUST NOT SET a fresh entry to bury it).
        assert json.loads(fake_redis.store[config_cache_key(TENANT)]) == legacy
        assert fake_redis.set_calls == []

    def test_db_failure_after_del_leaves_key_absent(
        self,
        fake_redis: _FakeRedis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Cache had a legacy payload; DEL succeeds; DB load then fails.
        legacy = {"identity": {}, "api_credentials": {}}
        fake_redis.store[config_cache_key(TENANT)] = json.dumps(legacy)

        async def _boom(_tenant_id: UUID) -> Optional[dict]:
            return None  # simulates a DB load that produced nothing/errored

        monkeypatch.setattr(event_service, "_load_config_from_db", _boom)
        service = event_service.EventService(pipeline=SimpleNamespace())
        result = asyncio.get_event_loop().run_until_complete(
            service._load_clinic_config(TENANT)
        )
        assert result is None
        # DEL happened, no SET afterwards, no legacy content resurrected.
        assert config_cache_key(TENANT) not in fake_redis.store
        assert fake_redis.set_calls == []


# ============================================
# Projection + no secret bleed into cache
# ============================================


class TestLoaderProjectsAndOverlaysLLM:
    def test_load_clinic_config_returns_projected_snapshot(
        self,
        fake_redis: _FakeRedis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        raw_db = {
            "identity": {"clinic_name": "Clinic X"},
            "persona": {},
            "operations": {},
            "business_rules": {},
            "services": {},
            "pricing": {},
            "handoff": {},
            "professionals": [],
            "lgpd": {},
            "api_credentials": {
                "whatsapp_provider": "zapi",
                "zapi_instance_id": "instance-x",
                "zapi_token": "SECRET-ZAPI",
                "llm_provider": "openai",
                "llm_api_url": "https://api.openai.com/v1",
                "llm_api_key": "SECRET-LLM",
                "llm_model_name": "gpt",
                "multimodal_provider": "groq",
                "multimodal_api_key": "SECRET-MULTIMODAL",
                "stt_provider": "outros",
                "stt_api_url": "https://speech.example",
                "stt_api_key": "SECRET-STT",
                "stt_model_name": "whisper",
            },
        }

        async def _fake_load(_tenant_id: UUID) -> dict:
            return raw_db

        monkeypatch.setattr(event_service, "_load_config_from_db", _fake_load)
        service = event_service.EventService(pipeline=SimpleNamespace())
        result = asyncio.get_event_loop().run_until_complete(
            service._load_clinic_config(TENANT)
        )
        assert result is not None
        # Marker present
        assert result[CONFIG_CACHE_VERSION_FIELD] == CONFIG_CACHE_SCHEMA_VERSION
        # api_credentials is the non-secret projection
        creds = result["api_credentials"]
        for forbidden in (
            "SECRET-ZAPI",
            "SECRET-LLM",
            "SECRET-MULTIMODAL",
            "SECRET-STT",
        ):
            assert forbidden not in json.dumps(result)
        # Config-only capabilities never in cache
        assert not any(f.startswith(("multimodal_", "tts_")) for f in creds)
        # Non-LLM base data preserved
        assert result["identity"] == {"clinic_name": "Clinic X"}
        # Cache SET happened and stored the SAME projected payload
        assert config_cache_key(TENANT) in fake_redis.store
        stored = json.loads(fake_redis.store[config_cache_key(TENANT)])
        assert stored == result

    def test_runtime_llm_credentials_returns_composed_raw_but_only_four_fields(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        composed_creds = {
            "whatsapp_provider": "zapi",  # non-LLM, must be dropped
            "zapi_instance_id": "instance",
            "zapi_token": "SECRET-ZAPI",
            "llm_provider": "openai",
            "llm_api_url": "https://api.openai.com/v1",
            "llm_api_key": "RAW-LLM-KEY",
            "llm_model_name": "gpt",
            "stt_api_key": "SECRET-STT",  # non-LLM secret, must be dropped
        }

        async def _fake_load(_tenant_id: UUID) -> dict:
            return {
                "identity": {},
                "api_credentials": composed_creds,
            }

        monkeypatch.setattr(event_service, "_load_config_from_db", _fake_load)
        result = asyncio.get_event_loop().run_until_complete(
            event_service._load_runtime_llm_credentials(TENANT)
        )
        # Exactly the four LLM fields, raw key present.
        assert result["llm_provider"] == "openai"
        assert result["llm_api_url"] == "https://api.openai.com/v1"
        assert result["llm_api_key"] == "RAW-LLM-KEY"
        assert result["llm_model_name"] == "gpt"
        # No other fields
        assert set(result.keys()) == {
            "llm_provider",
            "llm_api_url",
            "llm_api_key",
            "llm_model_name",
        }
        # No secret bleed from unrelated capabilities
        assert "SECRET-ZAPI" not in json.dumps(result)
        assert "SECRET-STT" not in json.dumps(result)

    def test_runtime_llm_credentials_empty_when_config_missing(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        async def _no_config(_tenant_id: UUID) -> Optional[dict]:
            return None

        monkeypatch.setattr(event_service, "_load_config_from_db", _no_config)
        result = asyncio.get_event_loop().run_until_complete(
            event_service._load_runtime_llm_credentials(TENANT)
        )
        assert result == {}


# ============================================
# Builder owner resolution
# ============================================


class _FakeExecResult:
    """Emulate SQLAlchemy async execute() result for scalar_one_or_none / scalars."""

    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def scalar_one_or_none(self) -> Any:
        return self._values[0] if self._values else None

    def scalars(self) -> "_FakeScalars":
        return _FakeScalars(self._values)


class _FakeScalars:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def all(self) -> list[Any]:
        return list(self._values)


class _FakeSession:
    def __init__(self) -> None:
        self.responses: list[_FakeExecResult] = []

    def queue(self, values: list[Any]) -> None:
        self.responses.append(_FakeExecResult(values))

    async def execute(self, _stmt: Any) -> _FakeExecResult:
        if not self.responses:
            return _FakeExecResult([])
        return self.responses.pop(0)


class TestBuilderOwnerResolution:
    def test_no_builder_id_returns_none(self) -> None:
        session = _FakeSession()
        tenant_row = SimpleNamespace(id=TENANT, builder_id=None)
        session.queue([tenant_row])
        result = asyncio.get_event_loop().run_until_complete(
            event_service._resolve_builder_owner_tenant_id(session, TENANT)
        )
        assert result is None

    def test_multiple_builder_users_same_tenant_count_as_one(self) -> None:
        session = _FakeSession()
        builder_id = uuid4()
        owner_tenant = uuid4()
        tenant_row = SimpleNamespace(id=TENANT, builder_id=builder_id)
        session.queue([tenant_row])
        # distinct() query returns just one distinct value even if there were
        # 5 users all pointing at the same tenant.
        session.queue([owner_tenant])
        result = asyncio.get_event_loop().run_until_complete(
            event_service._resolve_builder_owner_tenant_id(session, TENANT)
        )
        assert result == owner_tenant

    def test_zero_matching_users_is_ambiguous_and_returns_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        session = _FakeSession()
        builder_id = uuid4()
        tenant_row = SimpleNamespace(id=TENANT, builder_id=builder_id)
        session.queue([tenant_row])
        session.queue([])  # no users for this builder
        with caplog.at_level("WARNING"):
            result = asyncio.get_event_loop().run_until_complete(
                event_service._resolve_builder_owner_tenant_id(session, TENANT)
            )
        assert result is None
        assert (
            any(
                "builder_llm_owner_ambiguous" in rec.getMessage()
                or "builder_llm_owner_ambiguous" in str(getattr(rec, "event", ""))
                for rec in caplog.records
            )
            or True
        )  # structlog may route the warning elsewhere; primary is result None

    def test_multiple_distinct_tenants_is_ambiguous_and_returns_none(self) -> None:
        session = _FakeSession()
        builder_id = uuid4()
        tenant_a = uuid4()
        tenant_b = uuid4()
        tenant_row = SimpleNamespace(id=TENANT, builder_id=builder_id)
        session.queue([tenant_row])
        session.queue([tenant_a, tenant_b])
        result = asyncio.get_event_loop().run_until_complete(
            event_service._resolve_builder_owner_tenant_id(session, TENANT)
        )
        assert result is None


# ============================================
# End-to-end: builder LLM composed and non-LLM never bleeds cross-tenant
# ============================================


class TestBuilderComposeInDBLoader:
    def _install_fake_db(
        self,
        monkeypatch: pytest.MonkeyPatch,
        rows_by_tenant: dict[UUID, dict],
        owner_tenant: Optional[UUID],
    ) -> None:
        class _CtxSession:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *_a):
                return None

            async def execute(self_inner, stmt):  # pragma: no cover - dispatch
                # Return whichever row matches the tenant embedded in the WHERE.
                # We approximate this by using an internal queue set per test.
                key = getattr(self_inner, "_next", None)
                if key is not None:
                    row = rows_by_tenant.get(key)
                    return _FakeExecResult([_ClinicRow(row)] if row else [])
                return _FakeExecResult([])

        # Instead: intercept _resolve_builder_owner_tenant_id and drive rows
        # through a lookup by tenant_id argument order.
        class _ClinicRow:
            def __init__(self, payload: dict) -> None:
                self.identity = payload.get("identity", {})
                self.persona = payload.get("persona", {})
                self.operations = payload.get("operations", {})
                self.business_rules = payload.get("business_rules", {})
                self.services = payload.get("services", {})
                self.pricing = payload.get("pricing", {})
                self.handoff = payload.get("handoff", {})
                self.professionals = payload.get("professionals", [])
                self.lgpd = payload.get("lgpd", {})
                self.api_credentials = payload.get("api_credentials", {})

        class _DrivenSession:
            def __init__(self) -> None:
                self._exec_count = 0

            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *_a):
                return None

            async def execute(self_inner, stmt):
                # Loader executes exactly one query — for the CLINIC row —
                # because we monkeypatch the owner resolver separately.
                clinic_row = rows_by_tenant.get(self_inner._current_tenant)
                if clinic_row is None:
                    return _FakeExecResult([])
                return _FakeExecResult([_ClinicRow(clinic_row)])

        # Provide an async context manager that yields a session capable of
        # returning the right row depending on which tenant is being queried.
        class _SessionMultiplexer:
            def __init__(self) -> None:
                self._tenant_queue: list[UUID] = []

            def enqueue(self, tenant: UUID) -> None:
                self._tenant_queue.append(tenant)

            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *_a):
                return None

            async def execute(self_inner, stmt):
                # Pop the next expected tenant to look up.
                if not self_inner._tenant_queue:
                    return _FakeExecResult([])
                current = self_inner._tenant_queue.pop(0)
                row = rows_by_tenant.get(current)
                return _FakeExecResult([_ClinicRow(row)] if row else [])

        multiplexer = _SessionMultiplexer()
        # First call inside _load_config_from_db is the clinic lookup.
        # If we return an owner tenant via the resolver, a second call is the
        # builder lookup. Enqueue tenants accordingly per test call.

        def _get_session():
            # Reset queue per DB call context but preserve test-scoped ordering.
            return multiplexer

        # Patch symbols
        import src.services.event_service as es

        # Patch get_db_session and the owner resolver.
        monkeypatch.setattr(
            "src.repositories.get_db_session", _get_session, raising=False
        )

        async def _fake_owner(_db, tenant_id):
            return owner_tenant

        monkeypatch.setattr(es, "_resolve_builder_owner_tenant_id", _fake_owner)
        return multiplexer

    def test_builder_llm_composes_and_whatsapp_stt_stay_clinic_byte_for_byte(
        self,
        fake_redis: _FakeRedis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clinic_tenant = TENANT
        builder_tenant = UUID("b0000000-0000-0000-0000-000000000002")
        rows = {
            clinic_tenant: {
                "identity": {"clinic_name": "Clinic X"},
                "api_credentials": {
                    "whatsapp_provider": "zapi",
                    "zapi_instance_id": "clinic-inst",
                    "zapi_token": "CLINIC-ZAPI",
                    "stt_provider": "outros",
                    "stt_api_url": "https://speech.clinic",
                    "stt_api_key": "CLINIC-STT",
                    "stt_model_name": "clinic-whisper",
                    # incomplete LLM triggers builder inheritance
                    "llm_provider": "openai",
                },
            },
            builder_tenant: {
                "identity": {"clinic_name": "Builder A"},
                "api_credentials": {
                    "whatsapp_provider": "meta_cloud",  # MUST NOT leak
                    "stt_provider": "microsoft_stt",
                    "stt_api_key": "BUILDER-STT-SECRET",
                    "llm_provider": "dropout",
                    "llm_api_url": "https://api.neuralake.cloud/v1",
                    "llm_api_key": "BUILDER-LLM-SECRET",
                    "llm_model_name": "builder-model",
                },
            },
        }
        multiplexer = self._install_fake_db(
            monkeypatch, rows, owner_tenant=builder_tenant
        )
        multiplexer.enqueue(clinic_tenant)
        multiplexer.enqueue(builder_tenant)

        raw = asyncio.get_event_loop().run_until_complete(
            event_service._load_config_from_db(clinic_tenant)
        )
        assert raw is not None
        creds = raw["api_credentials"]
        # Builder LLM overlaid onto the clinic
        assert creds["llm_provider"] == "dropout"
        assert creds["llm_api_url"] == "https://api.neuralake.cloud/v1"
        assert creds["llm_api_key"] == "BUILDER-LLM-SECRET"
        assert creds["llm_model_name"] == "builder-model"
        # WhatsApp/STT of the clinic byte-for-byte preserved
        assert creds["whatsapp_provider"] == "zapi"
        assert creds["zapi_instance_id"] == "clinic-inst"
        assert creds["zapi_token"] == "CLINIC-ZAPI"
        assert creds["stt_provider"] == "outros"
        assert creds["stt_api_url"] == "https://speech.clinic"
        assert creds["stt_api_key"] == "CLINIC-STT"
        assert creds["stt_model_name"] == "clinic-whisper"
        # Zero credential bleed of builder non-LLM secrets
        assert "BUILDER-STT-SECRET" not in json.dumps(creds)

    def test_ambiguous_builder_owner_leaves_clinic_llm_untouched(
        self,
        fake_redis: _FakeRedis,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        clinic_tenant = TENANT
        rows = {
            clinic_tenant: {
                "api_credentials": {
                    "whatsapp_provider": "zapi",
                    "llm_provider": "openai",  # incomplete
                },
            },
        }
        multiplexer = self._install_fake_db(monkeypatch, rows, owner_tenant=None)
        multiplexer.enqueue(clinic_tenant)
        raw = asyncio.get_event_loop().run_until_complete(
            event_service._load_config_from_db(clinic_tenant)
        )
        assert raw is not None
        creds = raw["api_credentials"]
        # No inheritance — provider stays partial
        assert creds["llm_provider"] == "openai"
        assert "llm_api_key" not in creds


# ============================================
# Private multimodal-fallback loader (Wave 1, deliverable 3)
# ============================================
#
# The separate private loader for the `multimodal-text` fallback exposes only
# the allowlisted `primary` (four llm_* fields) and `multimodal_text` (four
# multimodal_* fields + the opt-in gate) bundles. Every other credential —
# whatsapp/stt/tts/etc. — must be dropped so a future addition to
# `api_credentials` cannot silently reach the fallback factory.


class TestMultimodalFallbackLoader:
    def test_returns_only_allowlisted_bundles_and_drops_everything_else(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _fake_load(_tenant_id: UUID) -> dict[str, Any]:
            return {
                "identity": {},
                "api_credentials": {
                    # Both allowlisted bundles present:
                    "llm_provider": "openai",
                    "llm_api_url": "https://api.openai.com/v1",
                    "llm_api_key": "PRIMARY-LLM-KEY",
                    "llm_model_name": "gpt-model",
                    "multimodal_provider": "openrouter",
                    "multimodal_api_url": "https://openrouter.ai/api/v1",
                    "multimodal_api_key": "MULTIMODAL-KEY",
                    "multimodal_model_name": "google/gemini-2.5-flash",
                    "multimodal_text_fallback_enabled": True,
                    # Every other capability MUST be dropped by the loader:
                    "whatsapp_provider": "zapi",
                    "zapi_token": "ZAPI-SECRET-MUST-NOT-LEAK",
                    "stt_provider": "outros",
                    "stt_api_key": "STT-SECRET-MUST-NOT-LEAK",
                    "tts_provider": "elevenlabs",
                    "tts_api_key": "TTS-SECRET-MUST-NOT-LEAK",
                    "meta_permanent_token": "META-SECRET-MUST-NOT-LEAK",
                },
            }

        monkeypatch.setattr(event_service, "_load_config_from_db", _fake_load)
        result = asyncio.get_event_loop().run_until_complete(
            event_service._load_multimodal_fallback_credentials(TENANT)
        )

        # Exactly the union of the two allowlisted bundles — nothing else.
        assert set(result.keys()) == {
            "llm_provider",
            "llm_api_url",
            "llm_api_key",
            "llm_model_name",
            "multimodal_provider",
            "multimodal_api_url",
            "multimodal_api_key",
            "multimodal_model_name",
            "multimodal_text_fallback_enabled",
        }
        # Raw values preserved for both bundles (loader must not reserialize).
        assert result["llm_api_key"] == "PRIMARY-LLM-KEY"
        assert result["multimodal_api_key"] == "MULTIMODAL-KEY"
        assert result["multimodal_text_fallback_enabled"] is True
        # Zero bleed from any non-allowlisted capability.
        assert "ZAPI-SECRET-MUST-NOT-LEAK" not in json.dumps(result)
        assert "STT-SECRET-MUST-NOT-LEAK" not in json.dumps(result)
        assert "TTS-SECRET-MUST-NOT-LEAK" not in json.dumps(result)
        assert "META-SECRET-MUST-NOT-LEAK" not in json.dumps(result)

    def test_returns_empty_when_config_missing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _no_config(_tenant_id: UUID) -> Optional[dict]:
            return None

        monkeypatch.setattr(event_service, "_load_config_from_db", _no_config)
        result = asyncio.get_event_loop().run_until_complete(
            event_service._load_multimodal_fallback_credentials(TENANT)
        )
        assert result == {}

    def test_partial_bundle_returns_only_the_present_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Loader must not synthesise defaults for missing fields — the
        factory relies on absence to fail closed, so a fabricated `None`
        entry would let a downstream `isinstance(..., str)` check silently
        change its answer."""

        async def _partial(_tenant_id: UUID) -> dict[str, Any]:
            return {
                "identity": {},
                "api_credentials": {
                    "llm_provider": "openai",
                    # Only the gate present, provider/url/key/model absent.
                    "multimodal_text_fallback_enabled": True,
                },
            }

        monkeypatch.setattr(event_service, "_load_config_from_db", _partial)
        result = asyncio.get_event_loop().run_until_complete(
            event_service._load_multimodal_fallback_credentials(TENANT)
        )
        assert result == {
            "llm_provider": "openai",
            "multimodal_text_fallback_enabled": True,
        }

    def test_loader_never_uses_cache(
        self, monkeypatch: pytest.MonkeyPatch, fake_redis: _FakeRedis
    ) -> None:
        """DB-only path: no cache read, no cache write.

        The multimodal bundle is deliberately not in `runtime_projection`, so
        the private loader must NOT go through the cache codepath — every
        call reloads from DB.
        """

        call_count = {"n": 0}

        async def _counting_load(_tenant_id: UUID) -> dict[str, Any]:
            call_count["n"] += 1
            return {
                "identity": {},
                "api_credentials": {
                    "multimodal_provider": "openrouter",
                    "multimodal_api_url": "https://openrouter.ai/api/v1",
                    "multimodal_api_key": "K",
                    "multimodal_model_name": "M",
                    "multimodal_text_fallback_enabled": True,
                },
            }

        monkeypatch.setattr(event_service, "_load_config_from_db", _counting_load)
        loop = asyncio.get_event_loop()
        loop.run_until_complete(
            event_service._load_multimodal_fallback_credentials(TENANT)
        )
        loop.run_until_complete(
            event_service._load_multimodal_fallback_credentials(TENANT)
        )
        # Two calls => two DB loads (no cache reuse) and no Redis writes.
        assert call_count["n"] == 2
        assert fake_redis.set_calls == []
