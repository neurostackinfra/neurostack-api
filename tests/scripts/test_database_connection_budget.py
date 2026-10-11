from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from scripts import check_database_connection_budget as budget
from src.config.settings import Settings

requires_docker_compose = pytest.mark.skipif(
    not Path("/usr/bin/docker").is_file(),
    reason="authoritative Compose renderer is unavailable",
)


def test_approved_profiles_calculate_20_and_40() -> None:
    manifest = Path("config/database-connection-budgets.json")
    assert budget.load_profile(manifest, "vps-compose")["total"] == 20
    assert budget.load_profile(manifest, "ecs-poc")["total"] == 40


@pytest.mark.parametrize(
    "overflow",
    [True, -1, "0", 0.0],
)
def test_overflow_accepts_only_nonnegative_integers(overflow: object) -> None:
    profile = dict(budget.APPROVED_PROFILES["vps-compose"])
    profile["max_overflow"] = overflow
    with pytest.raises(budget.BudgetError):
        budget.validate_profile(profile, "candidate")
    profile["max_overflow"] = 0
    assert budget.validate_profile(profile, "candidate")["total"] == 20


@pytest.mark.parametrize(
    "field",
    ["tasks", "workers", "pool_size", "max_connections", "threshold_percent"],
)
@pytest.mark.parametrize("invalid", [0, -1, True, "1"])
def test_other_factors_require_positive_plain_integers(
    field: str, invalid: object
) -> None:
    profile = dict(budget.APPROVED_PROFILES["vps-compose"])
    profile[field] = invalid
    with pytest.raises(budget.BudgetError):
        budget.validate_profile(profile, "candidate")


@pytest.mark.parametrize("total", [70, 71, 100])
def test_threshold_rejects_exactly_seventy_percent_and_above(total: int) -> None:
    profile = {
        "tasks": 1,
        "workers": 1,
        "pool_size": total,
        "max_overflow": 0,
        "pool_timeout": 30,
        "max_connections": 100,
        "threshold_percent": 70,
    }
    with pytest.raises(budget.BudgetError, match="strictly below"):
        budget.validate_profile(profile, "candidate")


@pytest.mark.parametrize("profile_name", ["vps-compose", "ecs-poc"])
@pytest.mark.parametrize("field", sorted(budget.PROFILE_FIELDS))
def test_manifest_rejects_independent_factor_drift(
    tmp_path: Path, profile_name: str, field: str
) -> None:
    document = json.loads(
        Path("config/database-connection-budgets.json").read_text(encoding="utf-8")
    )
    document["profiles"][profile_name][field] += 1
    manifest = tmp_path / "budgets.json"
    manifest.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(budget.BudgetError, match="drifted|strictly below"):
        budget.load_profile(manifest, profile_name)


def _vps_files(tmp_path: Path) -> tuple[Path, Path, Path]:
    compose = tmp_path / "compose.yml"
    dockerfile = tmp_path / "Dockerfile"
    env_file = tmp_path / ".env"
    compose.write_text(
        Path("docker-compose.production.yml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    dockerfile.write_text(
        Path("Dockerfile").read_text(encoding="utf-8"), encoding="utf-8"
    )
    env_file.write_text(
        "GEMINI_IMAGE_DIGEST=registry.example/api@sha256:" + "a" * 64 + "\n"
        "SECRET_KEY=test-secret\n"
        "ALLOWED_ORIGINS=[]\n"
        "ALLOWED_HOSTS=[]\n"
        "POSTGRES_USER=test_user\n"
        "POSTGRES_PASSWORD=test_password\n"
        "POSTGRES_DB=test_database\n"
        "DATABASE_POOL_SIZE=5\n"
        "DATABASE_MAX_OVERFLOW=0\n"
        "DATABASE_POOL_TIMEOUT=30\n"
        "REDIS_PASSWORD=test_redis\n"
        "QDRANT_API_KEY=test_qdrant\n"
        "ASAAS_API_KEY=test_asaas_api_key\n"
        "ASAAS_WEBHOOK_TOKEN=test_asaas_webhook_token\n"
        "ABACATEPAY_WEBHOOK_SECRET=test_abacatepay_webhook_secret\n",
        encoding="utf-8",
    )
    return compose, dockerfile, env_file


@requires_docker_compose
def test_vps_structure_accepts_only_the_manifest_topology(tmp_path: Path) -> None:
    compose, dockerfile, env_file = _vps_files(tmp_path)
    profile = budget.load_profile(
        Path("config/database-connection-budgets.json"), "vps-compose"
    )

    budget.validate_vps_facts(
        profile, compose=compose, dockerfile=dockerfile, env_file=env_file
    )


def test_pool_defaults_and_templates_match_approved_vps_profile() -> None:
    profile = budget.load_profile(
        Path("config/database-connection-budgets.json"), "vps-compose"
    )
    expected = {
        "DATABASE_POOL_SIZE": str(profile["pool_size"]),
        "DATABASE_MAX_OVERFLOW": str(profile["max_overflow"]),
        "DATABASE_POOL_TIMEOUT": str(profile["pool_timeout"]),
    }
    for template in (Path(".env.example"), Path("env.example")):
        values = dict(
            line.split("=", 1)
            for line in template.read_text(encoding="utf-8").splitlines()
            if "=" in line
        )
        assert {name: values[name] for name in expected} == expected
    settings_defaults = {
        "DATABASE_POOL_SIZE": Settings.model_fields["database_pool_size"].default,
        "DATABASE_MAX_OVERFLOW": Settings.model_fields["database_max_overflow"].default,
        "DATABASE_POOL_TIMEOUT": Settings.model_fields["database_pool_timeout"].default,
    }
    assert {name: str(value) for name, value in settings_defaults.items()} == expected
    compose = yaml.safe_load(
        Path("docker-compose.production.yml").read_text(encoding="utf-8")
    )
    compose_env = compose["services"]["api"]["environment"]
    assert {name: compose_env[name] for name in expected} == {
        name: f"${{{name}:?required}}" for name in expected
    }


@pytest.mark.parametrize(
    "mutate",
    [
        lambda text: text.replace(
            'command: ["postgres", "-c", "max_connections=100"]',
            '# command: ["postgres", "-c", "max_connections=100"]\n'
            '    command: ["postgres", "-c", "max_connections=20"]',
        ),
        lambda text: text.replace(
            "  redis:\n",
            "  decoy:\n"
            "    image: postgres@sha256:" + "0" * 64 + "\n"
            '    command: ["postgres", "-c", "max_connections=100"]\n'
            "  redis:\n",
        ).replace(
            'command: ["postgres", "-c", "max_connections=100"]',
            'command: ["postgres", "-c", "max_connections=20"]',
            1,
        ),
        lambda text: text.replace(
            "    restart: unless-stopped\n    ports:",
            "    restart: unless-stopped\n"
            "    deploy:\n      replicas: ${API_REPLICAS:-1}\n"
            "    ports:",
            1,
        ),
        lambda text: text.replace(
            "      DATABASE_POOL_SIZE: ${DATABASE_POOL_SIZE:?required}",
            "      DATABASE_POOL_SIZE: ${DATABASE_POOL_SIZE:?required}\n"
            "      DATABASE_POOL_SIZE: ${DATABASE_POOL_SIZE:?required}",
        ),
        lambda text: text.replace(
            "    restart: unless-stopped\n    ports:",
            '    restart: unless-stopped\n    command: ["uvicorn", "--workers", "4"]\n    ports:',
            1,
        ),
    ],
)
@requires_docker_compose
def test_vps_structure_rejects_decoys_duplicates_and_unresolved_topology(
    tmp_path: Path, mutate: object
) -> None:
    compose, dockerfile, env_file = _vps_files(tmp_path)
    compose.write_text(
        mutate(compose.read_text(encoding="utf-8")),  # type: ignore[operator]
        encoding="utf-8",
    )
    profile = budget.load_profile(
        Path("config/database-connection-budgets.json"), "vps-compose"
    )

    with pytest.raises(budget.BudgetError):
        budget.validate_vps_facts(
            profile, compose=compose, dockerfile=dockerfile, env_file=env_file
        )


@requires_docker_compose
def test_vps_structure_rejects_actual_twenty_of_twenty_hidden_by_comment(
    tmp_path: Path,
) -> None:
    compose, dockerfile, env_file = _vps_files(tmp_path)
    text = compose.read_text(encoding="utf-8").replace(
        'command: ["postgres", "-c", "max_connections=100"]',
        '# max_connections=100\n    command: ["postgres", "-c", "max_connections=20"]',
    )
    compose.write_text(text, encoding="utf-8")
    profile = budget.load_profile(
        Path("config/database-connection-budgets.json"), "vps-compose"
    )

    with pytest.raises(budget.BudgetError, match="max_connections"):
        budget.validate_vps_facts(
            profile, compose=compose, dockerfile=dockerfile, env_file=env_file
        )


@requires_docker_compose
def test_vps_effective_scale_four_is_rejected(tmp_path: Path) -> None:
    compose, dockerfile, env_file = _vps_files(tmp_path)
    compose.write_text(
        compose.read_text(encoding="utf-8").replace(
            "    restart: unless-stopped\n",
            "    restart: unless-stopped\n    scale: 4\n",
            1,
        ),
        encoding="utf-8",
    )
    profile = budget.load_profile(
        Path("config/database-connection-budgets.json"), "vps-compose"
    )

    with pytest.raises(budget.BudgetError, match="replica count"):
        budget.validate_vps_facts(
            profile, compose=compose, dockerfile=dockerfile, env_file=env_file
        )


@pytest.mark.parametrize(
    "command",
    [
        ["uvicorn", "--workers", "4", "--workers=99"],
        ["uvicorn", "--workers=4", "--workers=99"],
        ["uvicorn", "--workers"],
        ["uvicorn", "--workers", "four"],
        ["sh", "-c", "uvicorn --workers 4"],
    ],
)
def test_worker_parser_rejects_mixed_duplicate_missing_and_decoy_forms(
    command: list[str],
) -> None:
    with pytest.raises(budget.BudgetError):
        budget.parse_worker_count(command, "test command")


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (["uvicorn", "--workers", "4"], 4),
        (["uvicorn", "--workers=4"], 4),
    ],
)
def test_worker_parser_accepts_both_literal_forms(
    command: list[str], expected: int
) -> None:
    assert budget.parse_worker_count(command, "test command") == expected


def test_docker_effective_entrypoint_and_cmd_reject_mixed_worker_options(
    tmp_path: Path,
) -> None:
    _compose, dockerfile, _env_file = _vps_files(tmp_path)
    dockerfile.write_text(
        dockerfile.read_text(encoding="utf-8").replace(
            "FROM base AS deps",
            'ENTRYPOINT ["uvicorn", "--workers=99"]\n\nFROM base AS deps',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(budget.BudgetError, match="unambiguous"):
        budget._production_workers(dockerfile)
