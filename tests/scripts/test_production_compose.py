"""Static and executable contracts for the standalone production compose."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docker-compose.production.yml"
IMAGE = re.compile(
    r"(?:\$\{GEMINI_IMAGE_DIGEST:[^}]+\}|[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{64})"
)


def _config() -> dict[str, object]:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_compose_uses_only_approved_app_digest_and_named_volumes() -> None:
    config = _config()
    services = config["services"]
    assert (
        services["api"]["image"]
        == "${GEMINI_IMAGE_DIGEST:?checker-approved repository@sha256 digest required}"
    )
    assert set(services) == {
        "api",
        "postgres",
        "redis",
        "qdrant",
        "prometheus",
        "alertmanager",
    }
    assert all(IMAGE.fullmatch(service["image"]) for service in services.values())
    assert services["postgres"]["image"] == (
        "postgres@sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685"
    )
    assert services["redis"]["image"] == (
        "redis@sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf"
    )
    assert services["qdrant"]["image"] == (
        "qdrant/qdrant@sha256:6ac4807063bbecddca0250bfbcff52acf18c22263b904d12919349e6d0a408f1"
    )
    assert all("build" not in service for service in services.values())
    for service in services.values():
        for volume in service.get("volumes", []):
            source = volume.split(":", 1)[0]
            assert not source.startswith((".", "/", "~"))
    api_env = services["api"]["environment"]
    assert api_env["ENVIRONMENT"] == "production"
    assert api_env["DEBUG"] == "false"
    assert "SECRET_KEY:?" in api_env["SECRET_KEY"]
    assert "ALLOWED_ORIGINS:?" in api_env["ALLOWED_ORIGINS"]
    assert api_env["DATABASE_POOL_SIZE"] == "${DATABASE_POOL_SIZE:?required}"
    assert api_env["DATABASE_MAX_OVERFLOW"] == ("${DATABASE_MAX_OVERFLOW:?required}")
    assert api_env["DATABASE_POOL_TIMEOUT"] == ("${DATABASE_POOL_TIMEOUT:?required}")
    assert (
        "BILLING_OPERATION_RECEIPT_SIGNING_KEY:?"
        in api_env["BILLING_OPERATION_RECEIPT_SIGNING_KEY"]
    )
    assert (
        "BILLING_PROMETHEUS_RULES_DIGEST:?"
        in api_env["BILLING_PROMETHEUS_RULES_DIGEST"]
    )
    assert (
        "BILLING_OBSERVABILITY_SMOKE_DIGEST:?"
        in api_env["BILLING_OBSERVABILITY_SMOKE_DIGEST"]
    )
    assert (
        "BILLING_OBSERVABILITY_SMOKE_TOKEN:?"
        in api_env["BILLING_OBSERVABILITY_SMOKE_TOKEN"]
    )
    assert "BILLING_ALERT_RECEIVER_URL:?" in api_env["BILLING_ALERT_RECEIVER_URL"]
    assert "command" not in services["api"]
    assert "entrypoint" not in services["api"]
    assert services["postgres"]["command"] == [
        "postgres",
        "-c",
        "max_connections=100",
    ]
    assert services["prometheus"]["configs"]
    assert services["alertmanager"]["configs"]
    assert services["api"]["configs"]
    assert "billing_observability_smoke" in config["configs"]


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI unavailable")
def test_docker_compose_config_accepts_strong_synthetic_inputs() -> None:
    digest = "registry.example/neurostack/api@sha256:" + "a" * 64
    env = {
        **os.environ,
        "GEMINI_IMAGE_DIGEST": digest,
        "SECRET_KEY": "S" * 48,
        "ALLOWED_ORIGINS": '["https://app.production.invalid"]',
        "ALLOWED_HOSTS": '["api.production.invalid"]',
        "POSTGRES_USER": "prod_user",
        "POSTGRES_PASSWORD": "P" * 32,
        "POSTGRES_DB": "prod_db",
        "REDIS_PASSWORD": "R" * 32,
        "QDRANT_API_KEY": "Q" * 32,
        "DATABASE_POOL_SIZE": "5",
        "DATABASE_MAX_OVERFLOW": "0",
        "DATABASE_POOL_TIMEOUT": "30",
        "ASAAS_API_KEY": "test-asaas-api-key",
        "ASAAS_WEBHOOK_TOKEN": "test-asaas-webhook-token",
        "ASAAS_PROVIDER_EVIDENCE_KEY_ID": "fixture-v1",
        "ASAAS_PROVIDER_EVIDENCE_PUBLIC_KEY": "a" * 64,
        "ABACATEPAY_WEBHOOK_SECRET": "test-abacatepay-webhook-secret",
        "ABACATEPAY_API_KEY": "",
        "BILLING_OPERATION_RECEIPT_SIGNING_KEY": "K" * 32,
        "BILLING_PROMETHEUS_RULES_DIGEST": "a" * 64,
        "BILLING_OBSERVABILITY_SMOKE_DIGEST": "b" * 64,
        "BILLING_OBSERVABILITY_SMOKE_TOKEN": "T" * 32,
        "BILLING_FACT_SCHEMA_DIGEST": "c" * 64,
        "BILLING_OBSERVABILITY_ACKNOWLEDGEMENT": "{}",
        "BILLING_OBSERVABILITY_ACKNOWLEDGEMENT_DIGEST": "d" * 64,
        "BILLING_ALERT_RECEIVER_URL": "https://alerts.production.invalid/v1/billing",
    }
    completed = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert digest in completed.stdout
    for image in (
        "postgres@sha256:cf78e76683b9ca8c5733cbbdce6c9262b45b6767934dd0a95e671f9a0fc20685",
        "redis@sha256:ff02b58f971e7d7d156a1267e283fcbbeee91773b6aa36c49dac28ecfe28eadf",
        "qdrant/qdrant@sha256:6ac4807063bbecddca0250bfbcff52acf18c22263b904d12919349e6d0a408f1",
        "prom/prometheus@sha256:6573196e5dbe834779ac4d34cb053113cc502a69e9e20ba3be96d37f33e81545",
        "prom/alertmanager@sha256:5ba0431d6cb9bda30640de0a8c2c41ecad12bd78847def3e5907e626ed5a49e9",
    ):
        assert image in completed.stdout


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI unavailable")
def test_docker_compose_config_rejects_missing_required_inputs() -> None:
    completed = subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE), "config"],
        cwd=ROOT,
        env={"PATH": os.environ.get("PATH", "")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0


def test_production_docs_link_the_only_authorized_deploy_flow() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    docs_index = (ROOT / "docs/README.md").read_text(encoding="utf-8")
    assert "scripts/deploy-vps.sh" in readme
    assert "./docs/api-provider-settings-rollout.md" in readme
    assert "./api-provider-settings-rollout.md" in docs_index
    assert "docker build --target production" not in readme
    assert "neurostack-api:latest" not in readme
    assert (ROOT / "docs/api-provider-settings-rollout.md").is_file()
