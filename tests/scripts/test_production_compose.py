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
    assert set(services) == {"api", "postgres", "redis", "qdrant"}
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
    assert "command" not in services["api"]
    assert "entrypoint" not in services["api"]
    assert services["postgres"]["command"] == [
        "postgres",
        "-c",
        "max_connections=100",
    ]


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
        "ABACATEPAY_WEBHOOK_SECRET": "test-abacatepay-webhook-secret",
        "ABACATEPAY_API_KEY": "",
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
