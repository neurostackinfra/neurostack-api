"""Real-lifespan coverage for opt-in cognitive-core metrics."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE_SOURCE = ROOT / "packages" / "neurostack-core"


def run_isolated(
    source: str, *, metrics_enabled: bool
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["METRICS_ENABLED"] = "true" if metrics_enabled else "false"
    env["PYTHONPATH"] = os.pathsep.join((str(ROOT), str(CORE_SOURCE)))
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def assert_isolated_ok(source: str, *, metrics_enabled: bool) -> None:
    result = run_isolated(source, metrics_enabled=metrics_enabled)
    assert result.returncode == 0, result.stdout + result.stderr


def test_configured_lifespan_registers_core_metrics_and_exposes_decision() -> None:
    assert_isolated_ok(
        """
        import asyncio
        from unittest.mock import AsyncMock
        from uuid import UUID, uuid4

        from httpx import ASGITransport, AsyncClient
        from prometheus_client import REGISTRY
        from prometheus_client.parser import text_string_to_metric_families

        import src.api.main as main
        import src.repositories as repositories
        import src.services.event_service as event_service
        import src.llm.client as llm_client
        from src.api.middleware.auth import APIKeyStore, TenantContext
        from src.models.session import CognitiveStateData
        from src.repositories import get_db_session_dependency
        from src.services import get_event_service

        families = {
            "neurostack_srm_decisions_total",
            "neurostack_srm_latency_seconds",
            "neurostack_srm_safety_triggers_total",
            "neurostack_srm_handoffs_total",
            "neurostack_flow_transitions_total",
            "neurostack_flow_handler_duration_seconds",
            "neurostack_flow_decisions_total",
            "neurostack_policy_evaluations_total",
            "neurostack_policy_violations_total",
            "neurostack_policy_latency_seconds",
            "neurostack_prompt_generations_total",
            "neurostack_prompt_generation_seconds",
            "neurostack_prompt_tokens_total",
            "neurostack_output_validations_total",
        }
        assert main.settings.metrics_enabled is True
        assert families.isdisjoint(REGISTRY._names_to_collectors)

        async def noop():
            return None

        class FakeDB:
            commit = AsyncMock()
            rollback = AsyncMock()

        async def fake_db_dependency():
            yield FakeDB()

        async def accept_test_key(_cls, _api_key):
            return TenantContext(
                tenant_id=UUID("a0000000-0000-0000-0000-000000000001"),
                api_key="test-api-key",
                is_active=True,
            )

        repositories.init_database = noop
        repositories.close_database = noop
        main.rlhf.shutdown = noop
        main.configure_tracing = lambda: None
        main.instrument_redis = lambda: None
        main.instrument_sqlalchemy = lambda: None
        APIKeyStore.validate_api_key = classmethod(accept_test_key)
        main.app.dependency_overrides[get_db_session_dependency] = fake_db_dependency

        service = get_event_service()
        service._load_clinic_config = AsyncMock(return_value=None)
        service._lock_session_state = AsyncMock(
            return_value=(object(), CognitiveStateData())
        )
        service._load_message_history = AsyncMock(return_value=[])
        service._persist_session_state = AsyncMock(return_value=None)
        service._save_events_to_store = AsyncMock(return_value=None)
        event_service._load_runtime_llm_credentials = AsyncMock(return_value={})
        llm_client.get_tenant_llm_client = lambda *_args, **_kwargs: None
        llm_client.get_multimodal_text_llm_client = lambda *_args, **_kwargs: None

        def family_names(text):
            return {
                line.split()[2]
                for line in text.splitlines()
                if line.startswith("# TYPE neurostack_")
            }

        def decision_value(text, labels):
            for metric in text_string_to_metric_families(text):
                for sample in metric.samples:
                    if sample.name == "neurostack_srm_decisions_total" and sample.labels == labels:
                        return sample.value
            return 0.0

        async def exercise():
            async with main.lifespan(main.app):
                assert families <= set(REGISTRY._names_to_collectors)
                transport = ASGITransport(app=main.app)
                async with AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                    follow_redirects=True,
                ) as client:
                    before = await client.get("/metrics")
                    event = await client.post(
                        "/v1/events",
                        headers={"X-API-Key": "test-api-key"},
                        json={
                            "tenant_id": str(
                                UUID("a0000000-0000-0000-0000-000000000001")
                            ),
                            "session_id": str(uuid4()),
                            "event_type": "message.in",
                            "payload": {
                                "content": "Olá",
                                "channel": "web",
                                "user_id": "metrics-test",
                            },
                        },
                    )
                    after = await client.get("/metrics")

                assert before.status_code == 200
                assert event.status_code == 200, event.text
                assert after.status_code == 200
                assert family_names(after.text) >= families
                body = event.json()
                labels = {
                    "intent_category": body["decision"]["intent_category"],
                    "funnel_stage": body["decision"]["funnel_stage"],
                    "mode": body["mode"],
                }
                assert decision_value(after.text, labels) - decision_value(before.text, labels) == 1

            async with main.lifespan(main.app):
                transport = ASGITransport(app=main.app)
                async with AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                    follow_redirects=True,
                ) as client:
                    repeated = await client.get("/metrics")
                assert repeated.status_code == 200
                assert family_names(repeated.text) >= families

            main.app.dependency_overrides.clear()

        asyncio.run(exercise())
        """,
        metrics_enabled=True,
    )


def test_disabled_lifespan_does_not_register_or_mount_core_metrics() -> None:
    assert_isolated_ok(
        """
        import asyncio

        from httpx import ASGITransport, AsyncClient
        from prometheus_client import REGISTRY

        import src.api.main as main
        import src.repositories as repositories

        families = {
            "neurostack_srm_decisions_total",
            "neurostack_srm_latency_seconds",
            "neurostack_srm_safety_triggers_total",
            "neurostack_srm_handoffs_total",
            "neurostack_flow_transitions_total",
            "neurostack_flow_handler_duration_seconds",
            "neurostack_flow_decisions_total",
            "neurostack_policy_evaluations_total",
            "neurostack_policy_violations_total",
            "neurostack_policy_latency_seconds",
            "neurostack_prompt_generations_total",
            "neurostack_prompt_generation_seconds",
            "neurostack_prompt_tokens_total",
            "neurostack_output_validations_total",
        }
        assert main.settings.metrics_enabled is False
        assert families.isdisjoint(REGISTRY._names_to_collectors)

        async def noop():
            return None

        repositories.init_database = noop
        repositories.close_database = noop
        main.rlhf.shutdown = noop
        main.configure_tracing = lambda: None
        main.instrument_redis = lambda: None
        main.instrument_sqlalchemy = lambda: None

        async def exercise():
            async with main.lifespan(main.app):
                assert families.isdisjoint(REGISTRY._names_to_collectors)
                transport = ASGITransport(app=main.app)
                async with AsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                    follow_redirects=True,
                ) as client:
                    response = await client.get("/metrics")
                assert response.status_code == 404
                assert all(family not in response.text for family in families)

        asyncio.run(exercise())
        assert families.isdisjoint(REGISTRY._names_to_collectors)
        """,
        metrics_enabled=False,
    )
