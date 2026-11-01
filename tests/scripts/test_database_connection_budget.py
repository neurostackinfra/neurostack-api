from __future__ import annotations

import itertools
import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool, Pool

from scripts import check_database_connection_budget as budget
from src.config.settings import Settings

# Connections ONE process's billing fact worker and the credit consumer it
# presents to (asaas-rollout-opening Group 2c, transport on) may hold at the
# same time, out of that process's pool: the publisher commits its lease --
# returning its connection -- before the presentation, and the consumer runs
# in its own session.  The rest of the pool stays with the request path.
WORKER_AND_CONSUMER_CONNECTION_BUDGET = 1


class PoolCounter:
    """Concurrent checkouts of one pool, from its own events."""

    def __init__(self, pool: Pool) -> None:
        self.current = 0
        self.peak = 0
        self.checkouts = 0
        event.listen(pool, "checkout", self._checkout)
        event.listen(pool, "checkin", self._checkin)

    def _checkout(self, *_args: Any) -> None:
        self.current += 1
        self.checkouts += 1
        self.peak = max(self.peak, self.current)

    def _checkin(self, *_args: Any) -> None:
        self.current -= 1


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
        # Group 5 production requirements (``${VAR:?...}`` in the production
        # Compose file): receipt signing key, mounted rule/smoke/schema
        # digests, runtime acknowledgement and receiver.
        "BILLING_OPERATION_RECEIPT_SIGNING_KEY=" + "k" * 32 + "\n"
        "BILLING_PROMETHEUS_RULES_DIGEST=" + "c" * 64 + "\n"
        "BILLING_OBSERVABILITY_SMOKE_DIGEST=" + "d" * 64 + "\n"
        "BILLING_OBSERVABILITY_SMOKE_TOKEN=test_smoke_token\n"
        "BILLING_FACT_SCHEMA_DIGEST=" + "e" * 64 + "\n"
        "BILLING_OBSERVABILITY_ACKNOWLEDGEMENT=test_acknowledgement\n"
        "BILLING_OBSERVABILITY_ACKNOWLEDGEMENT_DIGEST=" + "f" * 64 + "\n"
        "BILLING_ALERT_RECEIVER_URL=https://alerts.example.test/hook\n",
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


# The production stage's own CMD (the development CMD carries --reload).
_PRODUCTION_CMD = (
    'CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000", '
    '"--workers"'
)


def test_docker_effective_entrypoint_and_cmd_reject_mixed_worker_options(
    tmp_path: Path,
) -> None:
    _compose, dockerfile, _env_file = _vps_files(tmp_path)
    text = dockerfile.read_text(encoding="utf-8")
    assert text.count(_PRODUCTION_CMD) == 1
    dockerfile.write_text(
        text.replace(
            _PRODUCTION_CMD,
            'ENTRYPOINT ["uvicorn", "--workers=99"]\n' + _PRODUCTION_CMD,
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(budget.BudgetError, match="unambiguous"):
        budget._production_workers(dockerfile)


def test_build_stage_entrypoint_does_not_reach_the_runtime_stage(
    tmp_path: Path,
) -> None:
    # production starts afresh from the pinned python image, not from base:
    # nothing declared in the build stages is inherited by it.
    _compose, dockerfile, _env_file = _vps_files(tmp_path)
    text = dockerfile.read_text(encoding="utf-8")
    # A renamed anchor would leave the Dockerfile untouched and the test vacuous.
    assert text.count("FROM base AS deps") == 1
    dockerfile.write_text(
        text.replace(
            "FROM base AS deps",
            'ENTRYPOINT ["uvicorn", "--workers=99"]\n\nFROM base AS deps',
            1,
        ),
        encoding="utf-8",
    )
    assert budget._production_workers(dockerfile) == 4
    # The ENTRYPOINT did land in the build stages.
    assert 'ENTRYPOINT ["uvicorn", "--workers=99"]' in dockerfile.read_text(
        encoding="utf-8"
    )


@requires_docker_compose
@pytest.mark.parametrize(
    "variable",
    ["BILLING_OPERATION_RECEIPT_SIGNING_KEY", "BILLING_ALERT_RECEIVER_URL"],
)
def test_vps_structure_still_refuses_missing_group5_production_secret(
    tmp_path: Path, variable: str
) -> None:
    compose, dockerfile, env_file = _vps_files(tmp_path)
    env_file.write_text(
        "".join(
            line + "\n"
            for line in env_file.read_text(encoding="utf-8").splitlines()
            if not line.startswith(f"{variable}=")
        ),
        encoding="utf-8",
    )
    profile = budget.load_profile(
        Path("config/database-connection-budgets.json"), "vps-compose"
    )
    with pytest.raises(
        budget.BudgetError, match="^cannot render authoritative Compose structure$"
    ):
        budget.validate_vps_facts(
            profile, compose=compose, dockerfile=dockerfile, env_file=env_file
        )


# ---------------------------------------------------------------------------
# The worker plus the consumer, counted with the transport on
# (asaas-rollout-opening Group 2c)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile_name", ["vps-compose", "ecs-poc"])
def test_the_worker_and_consumer_share_leaves_every_process_pool_room(
    profile_name: str,
) -> None:
    profile = budget.load_profile(
        Path("config/database-connection-budgets.json"), profile_name
    )
    per_process = profile["pool_size"] + profile["max_overflow"]
    # Never the whole pool: requests keep at least one connection per process.
    assert 0 < WORKER_AND_CONSUMER_CONNECTION_BUDGET < per_process
    processes = profile["tasks"] * profile["workers"]
    assert processes * WORKER_AND_CONSUMER_CONNECTION_BUDGET < profile["total"]


@pytest.fixture
def inbox_sequence() -> Iterator[None]:
    """SQLite stand-in for the PostgreSQL ``inbox_seq`` identity."""

    from src.models.credit_fact_inbox import CreditFactInbox

    counter = itertools.count(1)

    def assign(_mapper: Any, _connection: Any, target: Any) -> None:
        if target.inbox_seq is None:
            target.inbox_seq = next(counter)

    event.listen(CreditFactInbox, "before_insert", assign)
    try:
        yield
    finally:
        event.remove(CreditFactInbox, "before_insert", assign)


@pytest.mark.usefixtures("inbox_sequence")
async def test_the_worker_and_the_consumer_stay_within_the_budget_transport_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One process pool, sized like the approved profile, shared by the real
    worker and the real consumer exactly as ``src/api/main.py`` wires them."""

    from src.api import main as main_module
    from src.models.base import Base
    from src.models.billing import BillingFactAck, BillingFactDelivery
    from src.models.credit_fact_inbox import CreditFactInbox
    from src.repositories.database import _patch_jsonb_for_sqlite
    from src.services import credit_fact_consumer as consumer_module
    from src.services.billing_fact_worker import (
        BillingFactWorker,
        BillingFactWorkerConfig,
    )
    from src.services.credit_switch import CreditSwitch
    from tests.services.test_billing_fact_worker import _seed_fact

    profile = budget.load_profile(
        Path("config/database-connection-budgets.json"), "ecs-poc"
    )
    _patch_jsonb_for_sqlite()
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'budget.db'}",
        poolclass=AsyncAdaptedQueuePool,
        pool_size=profile["pool_size"],
        max_overflow=profile["max_overflow"],
        pool_timeout=profile["pool_timeout"],
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
        )
        facts = [await _seed_fact(sessions) for _ in range(3)]
        monkeypatch.setattr(CreditSwitch, "effective", lambda _self: True)
        monkeypatch.setattr(
            consumer_module, "default_session_factory", lambda: sessions
        )
        counter = PoolCounter(engine.sync_engine.pool)
        clock = [datetime.now(timezone.utc)]
        worker = BillingFactWorker(
            sessions,
            transport=main_module._credit_fact_transport(),
            transport_ready=main_module._billing_fact_transport_ready,
            config=BillingFactWorkerConfig.from_settings(Settings()),
            now=lambda: clock[0],
        )
        presenting = await worker.run_tick()
        clock[0] += timedelta(minutes=2)
        await worker.run_tick()  # the maintenance pass reads the acks
        async with sessions() as session:
            inbox = await session.scalar(select(func.count(CreditFactInbox.inbox_seq)))
            acks = await session.scalar(select(func.count(BillingFactAck.id)))
            states = set(
                (
                    await session.scalars(
                        select(BillingFactDelivery.state).where(
                            BillingFactDelivery.outbox_event_id.in_(facts)
                        )
                    )
                ).all()
            )
    finally:
        await engine.dispose()
    # Not vacuous: the real consumer applied every fact through the wiring.
    assert (presenting.presented, presenting.failed, presenting.not_ready) == (
        3,
        0,
        0,
    )
    assert (inbox, acks, states) == (3, 3, {"acknowledged"})
    assert counter.checkouts > 0 and counter.current == 0
    assert counter.peak <= WORKER_AND_CONSUMER_CONNECTION_BUDGET, counter.peak
