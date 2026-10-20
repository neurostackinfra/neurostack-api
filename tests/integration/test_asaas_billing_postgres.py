"""Real PostgreSQL gates for ASAAS billing persistence and concurrency."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import UUID, uuid4

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from src.config.settings import settings
from src.models.base import Base
from src.repositories.billing import (
    BillingCanaryBudgetExceeded,
    BillingCapabilityRejected,
    BillingRepository,
    BillingRevisionConflict,
    BillingTransitionEvidence,
    BillingTransitionRejected,
)
from src.services.billing_outbox import billing_fact_schema_digest

pytestmark = pytest.mark.integration

_ROOT = Path(__file__).resolve().parents[2]
_CHILD_PATTERN = re.compile(r"^neurostack_asaas_smoke_[0-9a-f]{12}$")
_MIGRATION_SNAPSHOT_JSON = json.dumps(
    {
        "catalog_version_id": "catalog-v1",
        "offer_id": "plan_plus_monthly_v1",
        "plan": "plus",
        "billing_cycle": "monthly",
        "coupon_code": None,
        "amount_cents": 99900,
        "credit_grant_mc": 5000,
        "included_clinics": 1,
        "additional_clinics": 0,
        "lines": [
            {
                "line_id": "plan",
                "offer_id": "plan_plus_monthly_v1",
                "kind": "plan",
                "quantity": 1,
                "unit_amount_cents": 99900,
                "line_amount_cents": 99900,
                "credit_grant_mc": 5000,
                "settled_cents": 0,
                "reversed_cents": 0,
                "restored_cents": 0,
                "line_financial_state": "pending",
            }
        ],
    },
    separators=(",", ":"),
)


@dataclass(frozen=True)
class _Harness:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    builder_id: UUID
    foreign_builder_id: UUID
    legacy_transaction_id: UUID
    schemas_equivalent: bool
    legacy_survived_downgrade: bool

    def session(self) -> AsyncSession:
        return self.sessions()


def _validated_admin_url(raw_url: str) -> URL:
    url = make_url(raw_url)
    if not url.drivername.startswith("postgresql"):
        raise ValueError("POSTGRES_SMOKE_DATABASE_URL must use PostgreSQL")
    if url.query:
        raise ValueError("POSTGRES_SMOKE_DATABASE_URL cannot override query options")
    try:
        is_loopback = ipaddress.ip_address(url.host or "").is_loopback
    except ValueError:
        is_loopback = (url.host or "").lower() == "localhost"
    if not is_loopback:
        raise ValueError("POSTGRES_SMOKE_DATABASE_URL must use loopback")
    if url.database != "postgres":
        raise ValueError("POSTGRES_SMOKE_DATABASE_URL must target postgres")
    application = make_url(settings.database_url)
    if (url.host, url.port, url.database) == (
        application.host,
        application.port,
        application.database,
    ):
        raise ValueError("smoke URL cannot target the application database")
    return URL.create(
        drivername="postgresql+asyncpg",
        username=url.username,
        password=url.password,
        host=url.host,
        port=url.port,
        database=url.database,
    )


async def _run_alembic(database_url: URL, *arguments: str) -> None:
    assert len(arguments) == 2
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url.render_as_string(hide_password=False)
    environment.pop("PYTHONPATH", None)
    runner = """
import sys
from alembic import command
from alembic.config import Config

sys.path.insert(0, sys.argv[3])
config = Config(sys.argv[1])
config.set_main_option("script_location", sys.argv[2])
getattr(command, sys.argv[4])(config, sys.argv[5])
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        runner,
        str(_ROOT / "alembic.ini"),
        str(_ROOT / "alembic"),
        str(_ROOT),
        *arguments,
        cwd=Path("/tmp"),
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode(errors="replace")


async def _run_init_script(database_url: URL) -> None:
    executable = Path("/usr/bin/psql")
    if not executable.is_file():
        raise RuntimeError("psql is required for bootstrap parity proof")
    process = await asyncio.create_subprocess_exec(
        str(executable),
        database_url.render_as_string(hide_password=False).replace(
            "postgresql+asyncpg://", "postgresql://"
        ),
        "--set=ON_ERROR_STOP=1",
        "--file",
        str(_ROOT / "scripts/init-db.sql"),
        cwd=Path("/tmp"),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode(errors="replace")


async def _create_child(admin_engine: AsyncEngine, name: str) -> None:
    assert _CHILD_PATTERN.fullmatch(name)
    async with admin_engine.connect() as connection:
        quoted = connection.dialect.identifier_preparer.quote(name)
        await connection.exec_driver_sql(f"CREATE DATABASE {quoted}")


async def _drop_child(admin_engine: AsyncEngine, name: str) -> None:
    assert _CHILD_PATTERN.fullmatch(name)
    async with admin_engine.connect() as connection:
        await connection.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = :database AND pid <> pg_backend_pid()"
            ),
            {"database": name},
        )
        quoted = connection.dialect.identifier_preparer.quote(name)
        await connection.exec_driver_sql(f"DROP DATABASE {quoted}")


async def _schema_fingerprint(engine: AsyncEngine) -> tuple[tuple[Any, ...], ...]:
    async with engine.connect() as connection:
        rows = await connection.execute(text(r"""
                SELECT 'column' AS kind, table_name AS owner,
                       column_name AS name,
                       data_type || ':' || is_nullable || ':' ||
                           coalesce(column_default, '') AS definition
                FROM information_schema.columns
                WHERE table_schema = 'public'
                  AND (
                    left(table_name, 8) = 'billing_'
                    OR (table_name = 'transactions' AND column_name IN (
                        'provider', 'provider_payment_id',
                        'billing_payment_effect_id'
                    ))
                  )
                UNION ALL
                SELECT 'constraint', relation.relname, con.conname,
                       pg_get_constraintdef(con.oid, true)
                FROM pg_constraint AS con
                JOIN pg_class AS relation ON relation.oid = con.conrelid
                JOIN pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = 'public'
                  AND (
                    left(relation.relname, 8) = 'billing_'
                    OR relation.relname = 'transactions'
                  )
                  AND (
                    left(relation.relname, 8) = 'billing_'
                    OR con.conname LIKE '%billing%'
                    OR con.conname LIKE '%provider_payment%'
                  )
                UNION ALL
                SELECT 'index', tablename, indexname, indexdef
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND left(tablename, 8) = 'billing_'
                UNION ALL
                SELECT 'trigger', event_object_table, trigger_name,
                       action_timing || ':' || event_manipulation
                FROM information_schema.triggers
                WHERE trigger_schema = 'public'
                  AND left(event_object_table, 8) = 'billing_'
                ORDER BY 1, 2, 3, 4
            """))
        return tuple(tuple(row) for row in rows)


def _normalized_sql(value: object | None) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"\s+", " ", str(value).strip().lower())
    normalized = re.sub(r"::[a-z ]+(?:\[\])?", "", normalized)
    while normalized.startswith("(") and normalized.endswith(")"):
        normalized = normalized[1:-1].strip()
    return normalized.replace("'", "")


async def _orm_ddl_contract(
    engine: AsyncEngine,
) -> tuple[dict[str, Any], dict[str, Any]]:
    table_names = sorted(
        table.name
        for table in Base.metadata.tables.values()
        if table.name.startswith("billing_") or table.name == "transactions"
    )

    async with engine.connect() as connection:

        def reflect_contract(sync_connection: Any, *, schema: str) -> dict[str, Any]:
            inspector = inspect(sync_connection)
            contract: dict[str, Any] = {}
            for table_name in table_names:
                columns = {
                    column["name"]: (
                        str(column["type"].compile(dialect=sync_connection.dialect)),
                        bool(column["nullable"]),
                        _normalized_sql(column.get("default")),
                    )
                    for column in inspector.get_columns(table_name, schema=schema)
                }
                foreign_keys = sorted(
                    (
                        tuple(item["constrained_columns"]),
                        item["referred_table"],
                        tuple(item["referred_columns"]),
                        (item.get("options") or {}).get("ondelete"),
                    )
                    for item in inspector.get_foreign_keys(table_name, schema=schema)
                )
                checks = {
                    item["name"]: _normalized_sql(item["sqltext"])
                    for item in inspector.get_check_constraints(
                        table_name, schema=schema
                    )
                }
                uniques = sorted(
                    tuple(item["column_names"])
                    for item in inspector.get_unique_constraints(
                        table_name, schema=schema
                    )
                )
                indexes = sorted(
                    (
                        item["name"],
                        tuple(item["column_names"]),
                        bool(item["unique"]),
                        _normalized_sql(
                            (item.get("dialect_options") or {}).get("postgresql_where")
                        ),
                    )
                    for item in inspector.get_indexes(table_name, schema=schema)
                    if not item.get("duplicates_constraint")
                )
                contract[table_name] = (
                    columns,
                    foreign_keys,
                    checks,
                    uniques,
                    indexes,
                )
            return contract

        def build_and_reflect_orm(sync_connection: Any) -> dict[str, Any]:
            schema = f"orm_parity_{uuid4().hex[:12]}"
            quoted = sync_connection.dialect.identifier_preparer.quote(schema)
            sync_connection.exec_driver_sql(f"CREATE SCHEMA {quoted}")
            sync_connection.exec_driver_sql(
                f"SET LOCAL search_path TO {quoted}, public"
            )
            for table in Base.metadata.sorted_tables:
                if table.name in table_names:
                    table.create(sync_connection)
            result = reflect_contract(sync_connection, schema=schema)
            sync_connection.exec_driver_sql(f"DROP SCHEMA {quoted} CASCADE")
            return result

        actual = await connection.run_sync(
            lambda sync_connection: reflect_contract(sync_connection, schema="public")
        )
        expected = await connection.run_sync(build_and_reflect_orm)
    return expected, actual


async def _seed_legacy_022(
    database_url: URL,
    *,
    builder_id: UUID,
    foreign_builder_id: UUID,
    transaction_id: UUID,
) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("""
                    INSERT INTO builders (id, name)
                    VALUES (:builder_id, 'ASAAS Smoke Builder'),
                           (:foreign_builder_id, 'Foreign Builder')
                """),
                {
                    "builder_id": builder_id,
                    "foreign_builder_id": foreign_builder_id,
                },
            )
            await connection.execute(
                text("""
                    INSERT INTO transactions (
                        id, builder_id, builder_name, amount_cents, method,
                        cycle, plan, coupon_code, external_id, event_type
                    ) VALUES (
                        :id, :builder_id, 'ASAAS Smoke Builder', 99900,
                        'credit_card', 'monthly', 'start', NULL,
                        'legacy-mixed-external-id', 'billing.paid'
                    )
                """),
                {"id": transaction_id, "builder_id": builder_id},
            )
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
async def postgres_harness() -> AsyncIterator[_Harness]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if raw_admin_url is None:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin_engine = create_async_engine(
        admin_url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
    )
    migrated_name = f"neurostack_asaas_smoke_{uuid4().hex[:12]}"
    fresh_name = f"neurostack_asaas_smoke_{uuid4().hex[:12]}"
    migrated_url = admin_url.set(database=migrated_name)
    fresh_url = admin_url.set(database=fresh_name)
    builder_id = uuid4()
    foreign_builder_id = uuid4()
    legacy_transaction_id = uuid4()
    migrated_engine: AsyncEngine | None = None
    fresh_engine: AsyncEngine | None = None
    created: list[str] = []
    previous_schema_digest = os.environ.get("BILLING_FACT_SCHEMA_DIGEST")
    if previous_schema_digest is None:
        # The smoke database is local test infrastructure.  Model the
        # deployed-artifact contract explicitly without allowing production
        # code to derive authority from its checkout.
        os.environ["BILLING_FACT_SCHEMA_DIGEST"] = billing_fact_schema_digest()
    try:
        async with admin_engine.connect() as connection:
            can_create = bool(
                await connection.scalar(
                    text(
                        "SELECT rolcreatedb OR rolsuper FROM pg_roles "
                        "WHERE rolname = current_user"
                    )
                )
            )
        if not can_create:
            raise PermissionError("smoke database user must have CREATEDB")

        await _create_child(admin_engine, migrated_name)
        created.append(migrated_name)
        await _run_alembic(migrated_url, "upgrade", "025")
        await _seed_legacy_022(
            migrated_url,
            builder_id=builder_id,
            foreign_builder_id=foreign_builder_id,
            transaction_id=legacy_transaction_id,
        )
        await _run_alembic(migrated_url, "upgrade", "head")
        migrated_engine = create_async_engine(migrated_url, poolclass=NullPool)
        async with migrated_engine.connect() as connection:
            assert (
                await connection.scalar(
                    text("SELECT count(*) FROM transactions WHERE id = :id"),
                    {"id": legacy_transaction_id},
                )
                == 1
            )

        await migrated_engine.dispose()
        migrated_engine = None
        await _run_alembic(migrated_url, "downgrade", "025")
        downgrade_probe = create_async_engine(migrated_url, poolclass=NullPool)
        try:
            async with downgrade_probe.connect() as connection:
                billing_absent = await connection.scalar(
                    text("SELECT to_regclass('public.billing_subscriptions') IS NULL")
                )
                transaction_preserved = await connection.scalar(
                    text("SELECT count(*) FROM transactions WHERE id = :id"),
                    {"id": legacy_transaction_id},
                )
                provider_column_absent = await connection.scalar(text("""
                        SELECT count(*) = 0
                        FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name = 'transactions'
                          AND column_name = 'provider_payment_id'
                    """))
            legacy_survived_downgrade = bool(
                billing_absent and transaction_preserved == 1 and provider_column_absent
            )
        finally:
            await downgrade_probe.dispose()
        await _run_alembic(migrated_url, "upgrade", "head")
        migrated_engine = create_async_engine(migrated_url, poolclass=NullPool)

        await _create_child(admin_engine, fresh_name)
        created.append(fresh_name)
        await _run_init_script(fresh_url)
        pre_migration_engine = create_async_engine(fresh_url, poolclass=NullPool)
        try:
            async with pre_migration_engine.connect() as connection:
                assert await connection.scalar(
                    text("SELECT to_regclass('public.alembic_version') IS NULL")
                )
                application_tables = await connection.scalar(text("""
                    SELECT count(*) FROM information_schema.tables
                    WHERE table_schema = 'public'
                      AND table_name NOT LIKE 'pg_%'
                """))
                assert application_tables == 0
        finally:
            await pre_migration_engine.dispose()
        await _run_alembic(fresh_url, "upgrade", "head")
        fresh_engine = create_async_engine(fresh_url, poolclass=NullPool)
        schemas_equivalent = await _schema_fingerprint(
            migrated_engine
        ) == await _schema_fingerprint(fresh_engine)
        await fresh_engine.dispose()
        fresh_engine = None
        await _drop_child(admin_engine, fresh_name)
        created.remove(fresh_name)

        yield _Harness(
            engine=migrated_engine,
            sessions=async_sessionmaker(
                migrated_engine,
                class_=AsyncSession,
                expire_on_commit=False,
                autoflush=False,
            ),
            builder_id=builder_id,
            foreign_builder_id=foreign_builder_id,
            legacy_transaction_id=legacy_transaction_id,
            schemas_equivalent=schemas_equivalent,
            legacy_survived_downgrade=legacy_survived_downgrade,
        )
    finally:
        if previous_schema_digest is None:
            os.environ.pop("BILLING_FACT_SCHEMA_DIGEST", None)
        else:
            os.environ["BILLING_FACT_SCHEMA_DIGEST"] = previous_schema_digest
        if fresh_engine is not None:
            await fresh_engine.dispose()
        if migrated_engine is not None:
            await migrated_engine.dispose()
        for name in reversed(created):
            await _drop_child(admin_engine, name)
        await admin_engine.dispose()


def _evidence() -> BillingTransitionEvidence:
    return BillingTransitionEvidence(
        nonce=uuid4(),
        deployment_digest="d" * 64,
        actor_user_id=uuid4(),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


async def _seed_capability_binding(
    session: AsyncSession,
    *,
    environment: str,
    capability_receipt_id: UUID,
) -> None:
    """Seed backend-issued capability receipts for repository-level gates."""

    receipt_ids: list[UUID] = []
    actor_ids: list[UUID] = []
    (
        operation_id,
        deployment_digest,
        request_id,
        inbox_high_watermark,
        fact_schema_digest,
    ) = (
        await session.execute(
            text("""
                SELECT operation_id, deployment_digest, request_id,
                       inbox_high_watermark, billing_fact_schema_digest
                FROM billing_topup_capability_receipts
                WHERE id = :capability_receipt_id
            """),
            {"capability_receipt_id": capability_receipt_id},
        )
    ).one()
    digests = ["a" * 64, "b" * 64, "c" * 64, "d" * 64]
    roles = ("deployer", "approver", "reviewer", "deployer")
    expires_at = datetime.now(timezone.utc) + timedelta(hours=2)
    evidence_types = ("producer", "credit", "reconciliation", "verification")
    for digest, role, evidence_type in zip(digests, roles, evidence_types, strict=True):
        assignment_id = uuid4()
        receipt_id = uuid4()
        actor_id = uuid4()
        receipt_ids.append(receipt_id)
        actor_ids.append(actor_id)
        await session.execute(
            text("""
                INSERT INTO billing_operation_assignments (
                    id, operation_id, environment, scope, role,
                    actor_user_id, payload_digest, expires_at
                ) VALUES (
                    :assignment_id, :operation_id, :environment,
                    'topup_capability', :role, :actor_id, :payload_digest,
                    :expires_at
                )
            """),
            {
                "assignment_id": assignment_id,
                "operation_id": operation_id,
                "environment": environment,
                "role": role,
                "actor_id": actor_id,
                "payload_digest": digest,
                "expires_at": expires_at,
            },
        )
        await session.execute(
            text("""
                INSERT INTO billing_operation_receipts (
                    id, assignment_id, operation_id, environment, scope,
                    role, actor_user_id, nonce, payload_digest,
                    deployment_digest, budget_digest, from_state, to_state,
                    evidence_type, request_id, inbox_high_watermark,
                    billing_fact_schema_digest, expires_at, signature_sha256
                ) VALUES (
                    :receipt_id, :assignment_id, :operation_id, :environment,
                    'topup_capability', :role, :actor_id, :nonce,
                    :payload_digest, :deployment_digest, :budget_digest,
                    'topup_disabled', 'topup_enabled', :evidence_type,
                    :request_id, :inbox_high_watermark, :fact_schema_digest,
                    :expires_at,
                    :signature_sha256
                )
            """),
            {
                "receipt_id": receipt_id,
                "assignment_id": assignment_id,
                "operation_id": operation_id,
                "environment": environment,
                "role": role,
                "actor_id": actor_id,
                "nonce": uuid4(),
                "payload_digest": digest,
                "deployment_digest": deployment_digest,
                "budget_digest": digest,
                "expires_at": expires_at,
                "signature_sha256": "f" * 64,
                "evidence_type": evidence_type,
                "request_id": request_id,
                "inbox_high_watermark": inbox_high_watermark,
                "fact_schema_digest": fact_schema_digest,
            },
        )
    await session.execute(
        text("""
            INSERT INTO billing_topup_capability_bindings (
                id, environment, capability_receipt_id,
                producer_receipt_id, credit_receipt_id,
                reconciliation_receipt_id, verification_receipt_id,
                producer_digest, credit_digest, reconciliation_digest,
                verification_digest, producer_actor_id, credit_actor_id,
                reconciliation_actor_id, verification_actor_id, verified_at
            ) VALUES (
                :id, :environment, :capability_receipt_id,
                :producer_receipt_id, :credit_receipt_id,
                :reconciliation_receipt_id, :verification_receipt_id,
                :producer_digest, :credit_digest, :reconciliation_digest,
                :verification_digest, :producer_actor_id, :credit_actor_id,
                :reconciliation_actor_id, :verification_actor_id, :verified_at
            )
        """),
        {
            "id": uuid4(),
            "environment": environment,
            "capability_receipt_id": capability_receipt_id,
            "producer_receipt_id": receipt_ids[0],
            "credit_receipt_id": receipt_ids[1],
            "reconciliation_receipt_id": receipt_ids[2],
            "verification_receipt_id": receipt_ids[3],
            "producer_digest": digests[0],
            "credit_digest": digests[1],
            "reconciliation_digest": digests[2],
            "verification_digest": digests[3],
            "producer_actor_id": actor_ids[0],
            "credit_actor_id": actor_ids[1],
            "reconciliation_actor_id": actor_ids[2],
            "verification_actor_id": actor_ids[3],
            "verified_at": datetime.now(timezone.utc),
        },
    )


async def _concurrent_unique(
    harness: _Harness,
    statement: str,
    first: dict[str, Any],
    second: dict[str, Any],
) -> None:
    session_one = harness.session()
    session_two = harness.session()
    await session_one.begin()
    await session_two.begin()
    try:
        await session_one.execute(text(statement), first)

        async def insert_second() -> None:
            try:
                await session_two.execute(text(statement), second)
                await session_two.commit()
            except BaseException:
                await session_two.rollback()
                raise

        contender = asyncio.create_task(insert_second())
        done, _ = await asyncio.wait({contender}, timeout=0.15)
        assert not done, "concurrent insert did not wait on the unique boundary"
        await session_one.commit()
        with pytest.raises(IntegrityError):
            await contender
    finally:
        if session_one.in_transaction():
            await session_one.rollback()
        if session_two.in_transaction():
            await session_two.rollback()
        await session_one.close()
        await session_two.close()


async def test_upgrade_downgrade_and_fresh_schema_are_equivalent(
    postgres_harness: _Harness,
) -> None:
    assert postgres_harness.schemas_equivalent is True
    assert postgres_harness.legacy_survived_downgrade is True
    async with postgres_harness.engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "036"
        )
        legacy = (
            await connection.execute(
                text("""
                    SELECT external_id, provider, provider_payment_id
                    FROM transactions WHERE id = :id
                """),
                {"id": postgres_harness.legacy_transaction_id},
            )
        ).one()
        assert tuple(legacy) == ("legacy-mixed-external-id", None, None)


async def test_live_schema_matches_orm_metadata_and_required_triggers(
    postgres_harness: _Harness,
) -> None:
    expected, actual = await _orm_ddl_contract(postgres_harness.engine)
    assert actual == expected

    async with postgres_harness.engine.connect() as connection:
        triggers = set(tuple(row) for row in (await connection.execute(text("""
                    SELECT event_object_table, trigger_name
                    FROM information_schema.triggers
                    WHERE trigger_schema = 'public'
                      AND event_object_table LIKE 'billing_%'
                """))).all())
    assert {
        (
            "billing_topup_capability_receipts",
            "trg_billing_topup_capability_lifetime",
        ),
        (
            "billing_topup_capability_receipts",
            "trg_billing_topup_capability_receipts_immutable",
        ),
        ("billing_rollout_control", "trg_billing_rollout_gate_validate"),
        (
            "billing_checkout_sessions",
            "trg_billing_canary_checkout_validate",
        ),
        (
            "billing_payment_projections",
            "trg_billing_canary_projection_validate",
        ),
        ("billing_payment_effects", "trg_billing_canary_effect_validate"),
        ("billing_rollout_receipts", "trg_billing_rollout_receipts_immutable"),
        ("billing_migration_receipts", "trg_billing_migration_receipts_immutable"),
        ("billing_outbox", "trg_billing_outbox_immutable"),
        ("billing_fact_handoffs", "trg_billing_fact_handoffs_immutable"),
        ("billing_fact_acks", "trg_billing_fact_acks_immutable"),
        (
            "billing_fact_reconciliation_alerts",
            "trg_billing_fact_reconciliation_alerts_immutable",
        ),
        ("billing_topup_orders", "trg_billing_topup_fact_atomic"),
        (
            "billing_subscription_cycles",
            "trg_billing_subscription_cycle_fact_atomic",
        ),
    }.issubset(triggers)


async def test_bootstrap_is_fail_closed_for_every_environment(
    postgres_harness: _Harness,
) -> None:
    async with postgres_harness.engine.connect() as connection:
        rows = (await connection.execute(text("""
                    SELECT rollout.environment, rollout.state,
                           rollout.topup_commercial_enabled,
                           rollout.rollout_revision, rollout.topup_revision,
                           roster.billing_roster_revision
                    FROM billing_rollout_control AS rollout
                    CROSS JOIN billing_roster_control AS roster
                    ORDER BY rollout.environment
                """))).all()
    assert rows == [
        ("development", "disabled", False, 0, 0, 0),
        ("production", "disabled", False, 0, 0, 0),
        ("staging", "disabled", False, 0, 0, 0),
        ("test", "disabled", False, 0, 0, 0),
    ]


# Group 10 E5 (migration 036): the legacy saga cannot run any more.  The
# saga-mechanics tests that used to follow (roster lock holder/writer order,
# exact approved provider flip, concurrent flips on the global roster, stale
# provider-flip receipts and legacy rebase, malformed receipts, receipt vs
# target serialization, the three-transaction flip order) drove the 035
# legacy branches of protect_billing_canary_subscription and the migration
# receipt validators through roster_revision, which 036 dropped; they were
# removed with it.  What 036 does instead is pinned here and in section
# (e2) of test_asaas_subscription_triggers_postgres.py; the 035 definitions
# are frozen in alembic/frozen/036_downgrade_restores_035.json and proven by
# test_migration_036_postgres.py.
#
# Inventory for Group 10 E6, same convention as the characterization module:
# the tests of this module that still touch a legacy table 037 drops.
TESTS_THAT_BREAK_AFTER_037: tuple[str, ...] = (
    # Pins the head revision ("036").
    "test_upgrade_downgrade_and_fresh_schema_are_equivalent",
    # Requires trg_billing_migration_receipts_immutable; the ORM still maps
    # the legacy tables until E6 removes their models.
    "test_live_schema_matches_orm_metadata_and_required_triggers",
    # Reads billing_roster_control.
    "test_bootstrap_is_fail_closed_for_every_environment",
    # Insert into billing_migrations.
    "test_cross_builder_correlations_and_snapshot_pii_are_rejected",
    "test_migration_snapshot_requires_exact_coherent_shape",
)


async def test_break_inventory_names_only_collected_tests(
    request: pytest.FixtureRequest,
) -> None:
    """Every inventory entry is a real node name of this module (not stale)."""

    module = request.node.parent
    assert isinstance(module, pytest.Module)
    collected = {item.name for item in module.collect()}
    assert len(set(TESTS_THAT_BREAK_AFTER_037)) == len(TESTS_THAT_BREAK_AFTER_037)
    assert sorted(set(TESTS_THAT_BREAK_AFTER_037) - collected) == []


async def test_group_zero_roster_keeps_annual_and_refuses_the_retired_saga(
    postgres_harness: _Harness,
) -> None:
    """A Group 0 legacy binding (annual AbacatePay row with its coupon) stays
    representable; since migration 036 none of the retired saga steps can
    move it or a NULL-provider legacy row: the pending cancellation, the
    self-asserted cancellation and the provider flip are refused."""

    annual_id, legacy_id = uuid4(), uuid4()
    async with postgres_harness.session() as session:
        async with session.begin():
            await session.execute(
                text("""
                    INSERT INTO billing_subscriptions (
                        id, builder_id, provider, external_subscription_id,
                        is_active, plan, billing_cycle, coupon_code, status
                    ) VALUES (
                        :annual, :builder_id, 'abacatepay', 'sub-annual', true,
                        'start', 'annual', 'LEGACY10', 'active'
                    ), (
                        :legacy, :builder_id, NULL, :legacy_external, false,
                        'start', 'monthly', NULL, 'legacy_cancel_pending'
                    )
                """),
                {
                    "annual": annual_id,
                    "legacy": legacy_id,
                    "builder_id": postgres_harness.builder_id,
                    "legacy_external": f"legacy-{legacy_id}",
                },
            )
        stored = (
            await session.execute(
                text("""
                    SELECT plan, billing_cycle, coupon_code
                    FROM billing_subscriptions WHERE id = :id
                """),
                {"id": annual_id},
            )
        ).one()
        assert tuple(stored) == ("start", "annual", "LEGACY10")
        await session.commit()

    refusals = (
        # The retired saga's entry point: AbacatePay -> pending cancellation.
        (
            annual_id,
            "SET provider = NULL, is_active = false, "
            "status = 'legacy_cancel_pending'",
            "legacy subscription snapshot is immutable",
            "23000",
        ),
        (
            annual_id,
            "SET plan = 'forbidden-mutation'",
            "legacy subscription snapshot is immutable",
            "23000",
        ),
        # Cancellation the saga used to confirm from a receipt (035: P0001).
        (
            legacy_id,
            "SET status = 'legacy_cancelled'",
            "legacy cancellation cannot be self-asserted",
            "P0001",
        ),
        # The provider flip onto the exact ASAAS target the saga approved.
        (
            legacy_id,
            "SET provider = 'asaas', external_subscription_id = 'asaas-target', "
            "plan = 'plus', billing_cycle = 'monthly', "
            "catalog_version_id = 'payment-2026-10-03', "
            "offer_id = 'plan_plus_monthly_v1', amount_cents = 99900, "
            "status = 'inactive'",
            "legacy subscription snapshot is immutable",
            "23000",
        ),
    )
    for row_id, assignment, message, sqlstate in refusals:
        async with postgres_harness.session() as session:
            with pytest.raises(DBAPIError, match=re.escape(message)) as info:
                async with session.begin():
                    await session.execute(
                        text(
                            f"UPDATE billing_subscriptions {assignment} WHERE id = :id"
                        ),
                        {"id": row_id},
                    )
        assert _sqlstate(info.value) == sqlstate, assignment
    async with postgres_harness.session() as session:
        rows = (
            await session.execute(
                text("""
                    SELECT id, provider, status, plan FROM billing_subscriptions
                    WHERE id IN (:annual, :legacy)
                """),
                {"annual": annual_id, "legacy": legacy_id},
            )
        ).all()
    assert sorted(tuple(row) for row in rows) == sorted(
        [
            (annual_id, "abacatepay", "active", "start"),
            (legacy_id, None, "legacy_cancel_pending", "start"),
        ]
    )


def _sqlstate(error: DBAPIError) -> str | None:
    for candidate in (error.orig, getattr(error.orig, "__cause__", None)):
        state = getattr(candidate, "sqlstate", None) or getattr(
            candidate, "pgcode", None
        )
        if state:
            return str(state)
    return None


async def test_concurrent_uniques_cover_intent_delivery_projection_effect_receipt_outbox(
    postgres_harness: _Harness,
) -> None:
    builder_id = postgres_harness.builder_id
    topup_order_id = uuid4()
    projection_id = uuid4()
    projection_payment_id = f"pay-{projection_id}"
    delivery_id = uuid4()
    async with postgres_harness.engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO billing_topup_orders (
                    id, builder_id, payment_method, catalog_version_id,
                    offer_id, amount_cents, credit_grant_mc, pricing_snapshot
                ) VALUES (
                    :id, :builder_id, 'pix', 'catalog-v1',
                    'package_credits_500_v1', 10000, 500,
                    '{"catalog_version_id":"catalog-v1"}'::jsonb
                )
            """),
            {"id": topup_order_id, "builder_id": builder_id},
        )
        await connection.execute(
            text("""
                INSERT INTO billing_webhook_deliveries (
                    id, builder_id, provider, event_id, event_name,
                    provider_created_at, body_bytes, body_sha256
                ) VALUES (
                    :id, :builder_id, 'asaas', :event_id, 'PAYMENT_CREATED',
                    now(), 2, :sha
                )
            """),
            {
                "id": delivery_id,
                "builder_id": builder_id,
                "event_id": f"evt-{delivery_id}",
                "sha": "a" * 64,
            },
        )
        await connection.execute(
            text("""
                INSERT INTO billing_payment_projections (
                    id, builder_id, provider, payment_id, aggregate_kind,
                    topup_order_id, status, amount_cents, provider_created_at,
                    ordering_key, last_event_id
                ) VALUES (
                    :id, :builder_id, 'asaas', :payment_id, 'topup',
                    :topup_order_id, 'pending', 10000, now(),
                    '2026-10-10T00:00:00Z:0', :event_id
                )
            """),
            {
                "id": projection_id,
                "builder_id": builder_id,
                "payment_id": projection_payment_id,
                "topup_order_id": topup_order_id,
                "event_id": f"evt-{delivery_id}",
            },
        )

    intent_id = uuid4()
    session_statement = """
        INSERT INTO billing_checkout_sessions (
            id, builder_id, intent_kind, intent_id, attempt_no,
            catalog_version_id, offer_id, amount_cents, pricing_snapshot
        ) VALUES (
            :id, :builder_id, 'subscription', :intent_id,
            :attempt_no, 'catalog-v1', 'plan_plus_monthly_v1', 99900,
            '{"catalog_version_id":"catalog-v1"}'::jsonb
        )
    """
    await _concurrent_unique(
        postgres_harness,
        session_statement,
        {
            "id": uuid4(),
            "builder_id": builder_id,
            "intent_id": intent_id,
            "attempt_no": 1,
        },
        {
            "id": uuid4(),
            "builder_id": builder_id,
            "intent_id": intent_id,
            "attempt_no": 2,
        },
    )

    for table_statement, first, second in (
        (
            """
            INSERT INTO billing_webhook_deliveries (
                id, builder_id, provider, event_id, event_name,
                provider_created_at, body_bytes, body_sha256
            ) VALUES (
                :id, :builder_id, 'asaas', :natural_id, 'PAYMENT_UPDATED',
                now(), 2, :sha
            )
            """,
            {
                "id": uuid4(),
                "builder_id": builder_id,
                "natural_id": f"evt-concurrent-{uuid4()}",
                "sha": "b" * 64,
            },
            {},
        ),
        (
            """
            INSERT INTO billing_payment_projections (
                id, builder_id, provider, payment_id, aggregate_kind,
                topup_order_id, status, amount_cents, provider_created_at,
                ordering_key, last_event_id
            ) VALUES (
                :id, :builder_id, 'asaas', :natural_id, 'topup',
                :topup_order_id, 'pending', 10000, now(),
                '2026-10-10T00:00:00Z:1', :event_id
            )
            """,
            {
                "id": uuid4(),
                "builder_id": builder_id,
                "natural_id": f"pay-concurrent-{uuid4()}",
                "topup_order_id": topup_order_id,
                "event_id": f"evt-{delivery_id}",
            },
            {},
        ),
        (
            """
            INSERT INTO billing_payment_effects (
                id, builder_id, projection_id, delivery_id, provider,
                payment_id, effect_kind, source_revision, amount_cents
            ) VALUES (
                :id, :builder_id, :projection_id, :delivery_id, 'asaas',
                :natural_id, 'settlement', 'settlement:v1', 10000
            )
            """,
            {
                "id": uuid4(),
                "builder_id": builder_id,
                "projection_id": projection_id,
                "delivery_id": delivery_id,
                "natural_id": projection_payment_id,
            },
            {},
        ),
        (
            """
            INSERT INTO billing_outbox (
                id, builder_id, schema_version, event_type, aggregate_kind,
                aggregate_id, topup_order_id, financial_revision,
                payload, payload_sha256
            ) VALUES (
                :id, :builder_id, 'billing.fact.v1',
                'topup.payment_settled.v1', 'topup_order', :aggregate_id,
                :topup_order_id, :natural_id, '{}'::jsonb, :sha
            )
            """,
            {
                "id": uuid4(),
                "builder_id": builder_id,
                "aggregate_id": topup_order_id,
                "topup_order_id": topup_order_id,
                "natural_id": 1,
                "sha": "d" * 64,
            },
            {},
        ),
    ):
        second.update(first)
        second["id"] = uuid4()
        await _concurrent_unique(
            postgres_harness,
            table_statement,
            first,
            second,
        )


async def test_rollout_locks_serialize_stale_writers(
    postgres_harness: _Harness,
) -> None:
    async with postgres_harness.session() as session:
        async with session.begin():
            await session.execute(
                text("""
                    UPDATE billing_checkout_sessions
                    SET state = 'completed'
                    WHERE builder_id = :builder_id
                      AND canary_environment IS NULL
                      AND state IN (
                          'creating', 'active', 'paid_pending_subscription',
                          'reconciliation_required', 'terminal_pending_readback'
                      )
                """),
                {"builder_id": postgres_harness.builder_id},
            )
    checkout_id = uuid4()
    checkout_writer = postgres_harness.session()
    opening_writer = postgres_harness.session()
    await checkout_writer.begin()
    await opening_writer.begin()
    try:
        await checkout_writer.execute(
            text("""
                INSERT INTO billing_checkout_sessions (
                    id, builder_id, intent_kind, intent_id, attempt_no,
                    catalog_version_id, offer_id, amount_cents, pricing_snapshot
                ) VALUES (
                    :id, :builder_id, 'subscription', :intent_id, 1,
                    'catalog-v1', 'plan_plus_monthly_v1', 99900, '{}'::jsonb
                )
            """),
            {
                "id": checkout_id,
                "builder_id": postgres_harness.builder_id,
                "intent_id": uuid4(),
            },
        )
        opening = asyncio.create_task(
            BillingRepository(opening_writer).transition_rollout(
                environment="development",
                expected_revision=0,
                to_state="canary_allowlist",
                canary_builder_id=postgres_harness.builder_id,
                evidence=_evidence(),
            )
        )
        done, _ = await asyncio.wait({opening}, timeout=0.15)
        assert not done
        await checkout_writer.commit()
        with pytest.raises(DBAPIError):
            await opening
        await opening_writer.rollback()
    finally:
        if checkout_writer.in_transaction():
            await checkout_writer.rollback()
        if opening_writer.in_transaction():
            await opening_writer.rollback()
        await checkout_writer.close()
        await opening_writer.close()

    async with postgres_harness.session() as session:
        async with session.begin():
            await session.execute(
                text("""
                    UPDATE billing_checkout_sessions SET state = 'completed'
                    WHERE id = :id
                """),
                {"id": checkout_id},
            )

    session_one = postgres_harness.session()
    session_two = postgres_harness.session()
    checkout_contender = postgres_harness.session()
    await session_one.begin()
    await session_two.begin()
    await checkout_contender.begin()
    try:
        async with session_one.begin_nested():
            await BillingRepository(session_one).transition_rollout(
                environment="development",
                expected_revision=0,
                to_state="canary_allowlist",
                canary_builder_id=postgres_harness.builder_id,
                evidence=_evidence(),
            )
        contender = asyncio.create_task(
            BillingRepository(session_two).transition_rollout(
                environment="development",
                expected_revision=0,
                to_state="canary_allowlist",
                canary_builder_id=postgres_harness.builder_id,
                evidence=_evidence(),
            )
        )
        late_checkout = asyncio.create_task(
            checkout_contender.execute(
                text("""
                    INSERT INTO billing_checkout_sessions (
                        id, builder_id, intent_kind, intent_id, attempt_no,
                        catalog_version_id, offer_id, amount_cents,
                        pricing_snapshot
                    ) VALUES (
                        :id, :builder_id, 'subscription', :intent_id, 1,
                        'catalog-v1', 'plan_plus_monthly_v1', 99900,
                        '{}'::jsonb
                    )
                """),
                {
                    "id": uuid4(),
                    "builder_id": postgres_harness.builder_id,
                    "intent_id": uuid4(),
                },
            )
        )
        done, _ = await asyncio.wait({contender}, timeout=0.15)
        assert not done
        done, _ = await asyncio.wait({late_checkout}, timeout=0.15)
        assert not done
        await session_one.commit()
        with pytest.raises(BillingRevisionConflict):
            await contender
        with pytest.raises(DBAPIError):
            await late_checkout
        await session_two.rollback()
        await checkout_contender.rollback()
    finally:
        if session_one.in_transaction():
            await session_one.rollback()
        if session_two.in_transaction():
            await session_two.rollback()
        if checkout_contender.in_transaction():
            await checkout_contender.rollback()
        await session_one.close()
        await session_two.close()
        await checkout_contender.close()

    async with postgres_harness.session() as session:
        async with session.begin():
            control = await BillingRepository(session).transition_rollout(
                environment="development",
                expected_revision=1,
                to_state="general",
                evidence=_evidence(),
            )
            assert control.topup_commercial_enabled is False
        row = (await session.execute(text("""
                    SELECT state, rollout_revision, topup_commercial_enabled,
                           topup_revision
                    FROM billing_rollout_control
                    WHERE environment = 'development'
                """))).one()
        assert tuple(row) == ("general", 2, False, 0)


async def test_topup_gate_requires_valid_capability_and_general_preserves_it(
    postgres_harness: _Harness,
) -> None:
    now = datetime.now(timezone.utc)
    for to_state, ttl_seconds, delay_seconds, expected_guard in (
        ("canary_allowlist", 1.0, 1.1, "transition receipt expired before commit"),
        ("general", 3600.0, 0.0, "rollout transition receipt is required"),
    ):
        receipt_id = uuid4()
        async with postgres_harness.session() as session:
            with pytest.raises(DBAPIError, match=expected_guard) as rejected:
                async with session.begin():
                    await session.execute(
                        text("""
                            INSERT INTO billing_rollout_receipts (
                                id, environment, receipt_kind, nonce,
                                deployment_digest, from_state, to_state,
                                expected_revision, resulting_revision,
                                actor_user_id, expires_at
                            ) VALUES (
                                :id, 'production', 'rollout_transition', :nonce,
                                :digest, 'disabled', :to_state, 0, 1, :actor,
                                now() + make_interval(
                                    secs => CAST(:ttl_seconds AS DOUBLE PRECISION)
                                )
                            )
                        """),
                        {
                            "id": receipt_id,
                            "nonce": uuid4(),
                            "digest": "4" * 64,
                            "to_state": to_state,
                            "actor": uuid4(),
                            "ttl_seconds": ttl_seconds,
                        },
                    )
                    await session.execute(
                        text("""
                            UPDATE billing_rollout_control
                            SET state = CAST(:to_state AS VARCHAR),
                                canary_builder_id = CASE
                                    WHEN CAST(:to_state AS VARCHAR)
                                        = 'canary_allowlist'
                                    THEN CAST(:builder_id AS uuid) ELSE NULL END,
                                rollout_revision = 1,
                                rollout_receipt_id = :receipt_id
                            WHERE environment = 'production'
                        """),
                        {
                            "to_state": to_state,
                            "builder_id": postgres_harness.builder_id,
                            "receipt_id": receipt_id,
                        },
                    )
                    if delay_seconds:
                        await session.execute(
                            text("SELECT pg_sleep(:delay)"),
                            {"delay": delay_seconds},
                        )
            assert _sqlstate(rejected.value) == "23000", rejected.value
    async with postgres_harness.session() as session:
        with pytest.raises(DBAPIError):
            async with session.begin():
                await session.execute(text("""
                    UPDATE billing_rollout_control
                    SET state = 'general'
                    WHERE environment = 'production'
                """))
    async with postgres_harness.session() as session:
        with pytest.raises(DBAPIError):
            async with session.begin():
                await session.execute(
                    text("""
                        INSERT INTO billing_topup_capability_receipts (
                            id, environment, schema_version, request_id,
                            credit_deployment_digest, billing_fact_schema_digest,
                            producer_compatibility_digest,
                            consumer_compatibility_digest, inbox_high_watermark,
                            reconciliation_receipt_id, approved_by, approved_at,
                            expires_at, reconciliation_complete, readback_complete,
                            has_open_gap, has_backlog, has_alert
                        ) VALUES (
                            :id, 'production', 'credit_capability_ready.v1',
                            :request_id, 'credit-image-sha256', :digest, :digest,
                            :digest, 0, :reconciliation_id, :approved_by,
                            :approved_at, :expires_at, true, true,
                            false, false, false
                        )
                    """),
                    {
                        "id": uuid4(),
                        "request_id": uuid4(),
                        "digest": "9" * 64,
                        "reconciliation_id": uuid4(),
                        "approved_by": uuid4(),
                        "approved_at": now + timedelta(hours=1),
                        "expires_at": now + timedelta(hours=2),
                    },
                )


async def test_topup_authorization_reserves_budget_atomically_and_disable_wins(
    postgres_harness: _Harness,
) -> None:
    order_ids = [uuid4(), uuid4(), uuid4()]
    subscription_checkout_id = uuid4()
    subscription_id = uuid4()
    async with postgres_harness.session() as session:
        async with session.begin():
            await BillingRepository(session).transition_rollout(
                environment="test",
                expected_revision=0,
                to_state="canary_allowlist",
                canary_builder_id=postgres_harness.builder_id,
                evidence=_evidence(),
            )
            subscription_checkout = await BillingRepository(
                session
            ).reserve_canary_subscription_checkout(
                environment="test",
                builder_id=postgres_harness.builder_id,
                intent_id=subscription_id,
                checkout_session_id=subscription_checkout_id,
                next_due_date=date(2026, 11, 10),
            )
            assert subscription_checkout.offer_id == "plan_plus_monthly_v1"
            assert subscription_checkout.pricing_snapshot["payment_method"] == (
                "credit_card"
            )
            assert (
                await BillingRepository(session).reserve_canary_subscription_checkout(
                    environment="test",
                    builder_id=postgres_harness.builder_id,
                    intent_id=subscription_id,
                    checkout_session_id=subscription_checkout_id,
                    next_due_date=date(2026, 11, 10),
                )
            ).id == subscription_checkout_id
            with pytest.raises(BillingCanaryBudgetExceeded):
                await BillingRepository(session).reserve_canary_subscription_checkout(
                    environment="test",
                    builder_id=postgres_harness.builder_id,
                    intent_id=uuid4(),
                    checkout_session_id=uuid4(),
                    next_due_date=date(2026, 11, 10),
                )
            for order_id in order_ids:
                await session.execute(
                    text("""
                        INSERT INTO billing_topup_orders (
                            id, builder_id, payment_method, catalog_version_id,
                            offer_id, amount_cents, credit_grant_mc,
                            pricing_snapshot
                        ) VALUES (
                            :id, :builder_id, 'pix', 'payment-2026-10-03',
                            'package_credits_500_v1', 10000, 500000,
                            CAST(:snapshot AS jsonb)
                        )
                    """),
                    {
                        "id": order_id,
                        "builder_id": postgres_harness.builder_id,
                        "snapshot": (
                            '{"catalog_version_id":"payment-2026-10-03",'
                            '"offer_id":"package_credits_500_v1",'
                            '"amount_cents":10000,"credit_grant_mc":500000,'
                            '"payment_method":"pix",'
                            '"lines":[{"line_id":"topup"}]}'
                        ),
                    },
                )
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        text("""
                            INSERT INTO billing_checkout_sessions (
                                id, builder_id, intent_kind, intent_id,
                                topup_order_id, attempt_no, catalog_version_id,
                                offer_id, amount_cents, pricing_snapshot
                            )
                            SELECT :checkout_id, builder_id, 'topup', id, id, 1,
                                   catalog_version_id, offer_id, amount_cents,
                                   pricing_snapshot
                            FROM billing_topup_orders WHERE id = :order_id
                        """),
                        {
                            "checkout_id": uuid4(),
                            "order_id": order_ids[1],
                        },
                    )

    first = postgres_harness.session()
    second = postgres_harness.session()
    await first.begin()
    await second.begin()
    try:
        reserved = await BillingRepository(first).authorize_and_reserve_topup_checkout(
            environment="test",
            builder_id=postgres_harness.builder_id,
            topup_order_id=order_ids[0],
            checkout_session_id=uuid4(),
        )
        assert reserved.intent_id == order_ids[0]

        async def reserve_second() -> None:
            try:
                await BillingRepository(second).authorize_and_reserve_topup_checkout(
                    environment="test",
                    builder_id=postgres_harness.builder_id,
                    topup_order_id=order_ids[1],
                    checkout_session_id=uuid4(),
                )
                await second.commit()
            except BaseException:
                await second.rollback()
                raise

        contender = asyncio.create_task(reserve_second())
        done, _ = await asyncio.wait({contender}, timeout=0.15)
        assert not done
        await first.commit()
        with pytest.raises(BillingCanaryBudgetExceeded):
            await contender
    finally:
        if first.in_transaction():
            await first.rollback()
        if second.in_transaction():
            await second.rollback()
        await first.close()
        await second.close()

    topup_checkout_id = reserved.id
    subscription_effect_id = uuid4()
    topup_effect_id = uuid4()
    substitute_subscription_id = uuid4()
    subscription_external_id = f"canary-{subscription_id}"
    async with postgres_harness.session() as session:
        async with session.begin():
            await session.execute(
                text("""
                    INSERT INTO billing_subscriptions (
                        id, builder_id, provider, external_subscription_id,
                        is_active, plan, billing_cycle, catalog_version_id,
                        offer_id, amount_cents, status
                    ) VALUES (
                        :id, :builder_id, 'asaas', :external_id, true, 'plus',
                        'monthly', 'payment-2026-10-03',
                        'plan_plus_monthly_v1', 99900, 'active'
                    )
                """),
                {
                    "id": subscription_id,
                    "builder_id": postgres_harness.builder_id,
                    "external_id": subscription_external_id,
                },
            )
            await session.execute(
                text("""
                    INSERT INTO billing_subscriptions (
                        id, builder_id, provider, external_subscription_id,
                        is_active, plan, billing_cycle, catalog_version_id,
                        offer_id, amount_cents, status
                    ) VALUES (
                        :id, :builder_id, 'asaas', :external_id, true, 'plus',
                        'monthly', 'payment-2026-10-03',
                        'plan_plus_monthly_v1', 99900, 'active'
                    )
                """),
                {
                    "id": substitute_subscription_id,
                    "builder_id": postgres_harness.builder_id,
                    "external_id": f"substitute-{substitute_subscription_id}",
                },
            )
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        text("""
                            UPDATE billing_subscriptions
                            SET external_subscription_id = :replacement
                            WHERE id = :subscription_id
                        """),
                        {
                            "replacement": f"replacement-{subscription_id}",
                            "subscription_id": subscription_id,
                        },
                    )
            await session.execute(
                text("""
                    UPDATE billing_checkout_sessions
                    SET external_checkout_id = :external_checkout_id,
                        resulting_external_subscription_id = :external_subscription_id
                    WHERE id = :checkout_id
                """),
                {
                    "external_checkout_id": f"checkout-{subscription_checkout_id}",
                    "external_subscription_id": subscription_external_id,
                    "checkout_id": subscription_checkout_id,
                },
            )
            for slot, checkout_id, aggregate_id, payment_id, amount, effect_id in (
                (
                    "subscription",
                    subscription_checkout_id,
                    subscription_id,
                    "canary-subscription-payment",
                    99900,
                    subscription_effect_id,
                ),
            ):
                delivery_id = uuid4()
                projection_id = uuid4()
                await session.execute(
                    text("""
                        INSERT INTO billing_webhook_deliveries (
                            id, builder_id, provider, event_id, event_name,
                            provider_created_at, body_bytes, body_sha256
                        ) VALUES (
                            :id, :builder_id, 'asaas', :event_id,
                            'PAYMENT_CONFIRMED', now(), 2, :sha
                        )
                    """),
                    {
                        "id": delivery_id,
                        "builder_id": postgres_harness.builder_id,
                        "event_id": f"event-{effect_id}",
                        "sha": "a" * 64,
                    },
                )
                await session.execute(
                    text("""
                        INSERT INTO billing_payment_projections (
                            id, builder_id, provider, payment_id,
                            aggregate_kind, subscription_id, topup_order_id,
                            checkout_session_id, canary_environment, canary_slot,
                            status, amount_cents, settled_cents,
                            provider_created_at, ordering_key, last_event_id
                        ) VALUES (
                            :id, :builder_id, 'asaas', :payment_id,
                            :slot, :subscription_id, :topup_order_id,
                            :checkout_id, 'test', :slot, 'settled',
                            :amount, :amount, now(), :ordering_key, :last_event_id
                        )
                    """),
                    {
                        "id": projection_id,
                        "builder_id": postgres_harness.builder_id,
                        "payment_id": payment_id,
                        "slot": slot,
                        "subscription_id": (
                            aggregate_id if slot == "subscription" else None
                        ),
                        "topup_order_id": (aggregate_id if slot == "topup" else None),
                        "checkout_id": checkout_id,
                        "amount": amount,
                        "ordering_key": f"order-{effect_id}",
                        "last_event_id": f"event-{effect_id}",
                    },
                )
                if slot == "subscription":
                    with pytest.raises(DBAPIError):
                        async with session.begin_nested():
                            await session.execute(
                                text("""
                                    UPDATE billing_payment_projections
                                    SET subscription_id = :subscription_id
                                    WHERE id = :projection_id
                                """),
                                {
                                    "subscription_id": substitute_subscription_id,
                                    "projection_id": projection_id,
                                },
                            )
                    with pytest.raises(DBAPIError):
                        async with session.begin_nested():
                            await session.execute(
                                text("""
                                    INSERT INTO billing_payment_effects (
                                        id, builder_id, projection_id,
                                        delivery_id, provider, payment_id,
                                        effect_kind, source_revision, amount_cents
                                    ) VALUES (
                                        :id, :builder_id, :projection_id,
                                        :delivery_id, 'asaas', :payment_id,
                                        'settlement', 'settlement:v1', 99900
                                    )
                                """),
                                {
                                    "id": uuid4(),
                                    "builder_id": postgres_harness.builder_id,
                                    "projection_id": projection_id,
                                    "delivery_id": delivery_id,
                                    "payment_id": payment_id,
                                },
                            )
                await session.execute(
                    text("""
                        INSERT INTO billing_payment_effects (
                            id, builder_id, projection_id, delivery_id,
                            provider, payment_id, effect_kind, source_revision,
                            amount_cents, canary_environment, canary_slot
                        ) VALUES (
                            :id, :builder_id, :projection_id, :delivery_id,
                            'asaas', :payment_id, 'settlement', 'settlement:v1',
                            :amount, 'test', :slot
                        )
                    """),
                    {
                        "id": effect_id,
                        "builder_id": postgres_harness.builder_id,
                        "projection_id": projection_id,
                        "delivery_id": delivery_id,
                        "payment_id": payment_id,
                        "amount": amount,
                        "slot": slot,
                    },
                )

            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        text("""
                            INSERT INTO transactions (
                                id, builder_id, builder_name, amount_cents,
                                method, cycle, plan, event_type, provider,
                                provider_payment_id, billing_payment_effect_id
                            ) VALUES (
                                :id, :builder_id, 'Mismatch', 99900,
                                'credit_card', 'monthly', 'plus', 'paid',
                                'asaas', 'wrong-payment', :effect_id
                            )
                        """),
                        {
                            "id": uuid4(),
                            "builder_id": postgres_harness.builder_id,
                            "effect_id": subscription_effect_id,
                        },
                    )

            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        text("""
                            INSERT INTO billing_payment_projections (
                                id, builder_id, provider, payment_id,
                                aggregate_kind, topup_order_id, status,
                                amount_cents, settled_cents, provider_created_at,
                                ordering_key, last_event_id
                            ) VALUES (
                                :id, :builder_id, 'asaas', :payment_id, 'topup',
                                :order_id, 'settled', 10000, 10000, now(),
                                :ordering_key, :last_event_id
                            )
                        """),
                        {
                            "id": uuid4(),
                            "builder_id": postgres_harness.builder_id,
                            "payment_id": f"untagged-{uuid4()}",
                            "order_id": order_ids[1],
                            "ordering_key": f"untagged-{uuid4()}",
                            "last_event_id": f"event-{uuid4()}",
                        },
                    )
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        text("""
                            INSERT INTO billing_payment_effects (
                                id, builder_id, projection_id, delivery_id,
                                provider, payment_id, effect_kind,
                                source_revision, amount_cents,
                                canary_environment, canary_slot
                            )
                            SELECT :id, builder_id, projection_id, delivery_id,
                                   provider, payment_id, effect_kind,
                                   source_revision, amount_cents,
                                   canary_environment, canary_slot
                            FROM billing_payment_effects WHERE id = :source_id
                        """),
                        {"id": uuid4(), "source_id": subscription_effect_id},
                    )
            subscription_effect = await BillingRepository(
                session
            ).reserve_canary_settlement(
                environment="test", payment_effect_id=subscription_effect_id
            )
            assert subscription_effect.id == subscription_effect_id
            assert (
                await BillingRepository(session).reserve_canary_settlement(
                    environment="test", payment_effect_id=subscription_effect_id
                )
            ).id == subscription_effect_id
            await session.execute(
                text("""
                    UPDATE billing_subscriptions
                    SET status = 'active_linked'
                    WHERE id = :subscription_id
                """),
                {"subscription_id": subscription_id},
            )
            for status in (
                "readback_required",
                "active",
                "readback_required",
                "active_linked",
            ):
                await session.execute(
                    text("""
                        UPDATE billing_subscriptions
                        SET status = :status
                        WHERE id = :subscription_id
                    """),
                    {"status": status, "subscription_id": subscription_id},
                )
            for status, is_active in (("active", False), ("cancelled", True)):
                with pytest.raises(DBAPIError):
                    async with session.begin_nested():
                        await session.execute(
                            text("""
                                UPDATE billing_subscriptions
                                SET status = :status, is_active = :is_active
                                WHERE id = :subscription_id
                            """),
                            {
                                "status": status,
                                "is_active": is_active,
                                "subscription_id": subscription_id,
                            },
                        )
            await session.execute(
                text("""
                    UPDATE billing_subscriptions
                    SET status = 'cancelled', is_active = false
                    WHERE id = :subscription_id
                """),
                {"subscription_id": subscription_id},
            )
            for statement, parameters in (
                (
                    """
                    UPDATE billing_subscriptions
                    SET status = 'active', is_active = true
                    WHERE id = :subscription_id
                    """,
                    {"subscription_id": subscription_id},
                ),
                (
                    """
                    UPDATE billing_checkout_sessions
                    SET external_checkout_id = :replacement
                    WHERE id = :checkout_id
                    """,
                    {
                        "replacement": f"replacement-{subscription_checkout_id}",
                        "checkout_id": subscription_checkout_id,
                    },
                ),
                (
                    """
                    UPDATE billing_checkout_sessions
                    SET resulting_external_subscription_id = :replacement
                    WHERE id = :checkout_id
                    """,
                    {
                        "replacement": f"substitute-{substitute_subscription_id}",
                        "checkout_id": subscription_checkout_id,
                    },
                ),
            ):
                with pytest.raises(DBAPIError):
                    async with session.begin_nested():
                        await session.execute(text(statement), parameters)

    replay_one = postgres_harness.session()
    replay_two = postgres_harness.session()
    await replay_one.begin()
    await replay_two.begin()
    try:
        assert (
            await BillingRepository(replay_one).reserve_canary_settlement(
                environment="test", payment_effect_id=subscription_effect_id
            )
        ).id == subscription_effect_id
        replay = asyncio.create_task(
            BillingRepository(replay_two).reserve_canary_settlement(
                environment="test", payment_effect_id=subscription_effect_id
            )
        )
        done, _ = await asyncio.wait({replay}, timeout=0.15)
        assert not done
        await replay_one.commit()
        assert (await replay).id == subscription_effect_id
        await replay_two.commit()
    finally:
        if replay_one.in_transaction():
            await replay_one.rollback()
        if replay_two.in_transaction():
            await replay_two.rollback()
        await replay_one.close()
        await replay_two.close()

    disabler = postgres_harness.session()
    authorizer = postgres_harness.session()
    await disabler.begin()
    await authorizer.begin()
    try:
        await BillingRepository(disabler).transition_rollout(
            environment="test",
            expected_revision=1,
            to_state="disabled",
            evidence=_evidence(),
        )
        authorization = asyncio.create_task(
            BillingRepository(authorizer).authorize_and_reserve_topup_checkout(
                environment="test",
                builder_id=postgres_harness.builder_id,
                topup_order_id=order_ids[2],
                checkout_session_id=uuid4(),
            )
        )
        done, _ = await asyncio.wait({authorization}, timeout=0.15)
        assert not done
        await disabler.commit()
        with pytest.raises(BillingTransitionRejected):
            await authorization
        await authorizer.rollback()
    finally:
        if disabler.in_transaction():
            await disabler.rollback()
        if authorizer.in_transaction():
            await authorizer.rollback()
        await disabler.close()
        await authorizer.close()

    async with postgres_harness.session() as session:
        async with session.begin():
            with pytest.raises(BillingTransitionRejected):
                await BillingRepository(session).transition_rollout(
                    environment="test",
                    expected_revision=2,
                    to_state="canary_allowlist",
                    canary_builder_id=postgres_harness.foreign_builder_id,
                    evidence=_evidence(),
                )

    async with postgres_harness.session() as session:
        async with session.begin():
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        text("""
                            INSERT INTO billing_payment_projections (
                                id, builder_id, provider, payment_id,
                                aggregate_kind, subscription_id, status,
                                amount_cents, settled_cents, provider_created_at,
                                ordering_key, last_event_id
                            ) VALUES (
                                :id, :builder_id, 'asaas', :payment_id,
                                'subscription', :subscription_id, 'settled',
                                99900, 99900, now(), :ordering_key, :last_event_id
                            )
                        """),
                        {
                            "id": uuid4(),
                            "builder_id": postgres_harness.builder_id,
                            "payment_id": f"late-untagged-{uuid4()}",
                            "subscription_id": subscription_id,
                            "ordering_key": f"late-untagged-{uuid4()}",
                            "last_event_id": f"late-event-{uuid4()}",
                        },
                    )

            await session.execute(
                text("""
                    UPDATE billing_checkout_sessions
                    SET external_checkout_id = :external_checkout_id
                    WHERE id = :checkout_id
                """),
                {
                    "external_checkout_id": f"checkout-{topup_checkout_id}",
                    "checkout_id": topup_checkout_id,
                },
            )
            delivery_id = uuid4()
            projection_id = uuid4()
            await session.execute(
                text("""
                    INSERT INTO billing_webhook_deliveries (
                        id, builder_id, provider, event_id, event_name,
                        provider_created_at, body_bytes, body_sha256
                    ) VALUES (
                        :id, :builder_id, 'asaas', :event_id,
                        'PAYMENT_CONFIRMED', now(), 2, :sha
                    )
                """),
                {
                    "id": delivery_id,
                    "builder_id": postgres_harness.builder_id,
                    "event_id": f"event-{topup_effect_id}",
                    "sha": "b" * 64,
                },
            )
            await session.execute(
                text("""
                    INSERT INTO billing_payment_projections (
                        id, builder_id, provider, payment_id,
                        aggregate_kind, topup_order_id, checkout_session_id,
                        canary_environment, canary_slot, status, amount_cents,
                        settled_cents, provider_created_at, ordering_key,
                        last_event_id
                    ) VALUES (
                        :id, :builder_id, 'asaas', 'canary-topup-payment',
                        'topup', :order_id, :checkout_id, 'test', 'topup',
                        'settled', 10000, 10000, now(), :ordering_key, :event_id
                    )
                """),
                {
                    "id": projection_id,
                    "builder_id": postgres_harness.builder_id,
                    "order_id": order_ids[0],
                    "checkout_id": topup_checkout_id,
                    "ordering_key": f"order-{topup_effect_id}",
                    "event_id": f"event-{topup_effect_id}",
                },
            )
            await session.execute(
                text("""
                    INSERT INTO billing_payment_effects (
                        id, builder_id, projection_id, delivery_id, provider,
                        payment_id, effect_kind, source_revision, amount_cents,
                        canary_environment, canary_slot
                    ) VALUES (
                        :id, :builder_id, :projection_id, :delivery_id, 'asaas',
                        'canary-topup-payment', 'settlement', 'settlement:v1',
                        10000, 'test', 'topup'
                    )
                """),
                {
                    "id": topup_effect_id,
                    "builder_id": postgres_harness.builder_id,
                    "projection_id": projection_id,
                    "delivery_id": delivery_id,
                },
            )
            late_effect = await BillingRepository(session).reserve_canary_settlement(
                environment="test", payment_effect_id=topup_effect_id
            )
            assert late_effect.id == topup_effect_id

    async with postgres_harness.engine.connect() as connection:
        row = (await connection.execute(text("""
                SELECT state, canary_checkout_count, canary_settlement_count,
                       canary_amount_reserved_cents,
                       canary_subscription_checkout_id,
                       canary_topup_checkout_id,
                       canary_subscription_effect_id,
                       canary_topup_effect_id
                FROM billing_rollout_control WHERE environment = 'test'
            """))).one()
        assert tuple(row[:4]) == ("disabled", 2, 2, 109900)
        assert tuple(row[4:]) == (
            subscription_checkout_id,
            topup_checkout_id,
            subscription_effect_id,
            topup_effect_id,
        )

    async with postgres_harness.session() as session:
        with pytest.raises(DBAPIError):
            async with session.begin():
                await session.execute(text("""
                        UPDATE billing_rollout_control
                        SET canary_topup_checkout_id =
                            canary_subscription_checkout_id
                        WHERE environment = 'test'
                    """))
        with pytest.raises(DBAPIError):
            async with session.begin():
                await session.execute(
                    text("""
                        UPDATE billing_checkout_sessions
                        SET builder_id = :builder_id
                        WHERE id = :checkout_id
                    """),
                    {
                        "builder_id": postgres_harness.foreign_builder_id,
                        "checkout_id": subscription_checkout_id,
                    },
                )

    immutable_statements = (
        (
            "DELETE FROM billing_rollout_control WHERE environment = 'test'",
            {},
        ),
        (
            """
            UPDATE billing_rollout_control
            SET rollout_revision = 0
            WHERE environment = 'test'
            """,
            {},
        ),
        (
            """
            UPDATE billing_checkout_sessions
            SET pricing_snapshot = pricing_snapshot - 'included_clinics'
            WHERE id = :checkout_id
            """,
            {"checkout_id": subscription_checkout_id},
        ),
        (
            """
            UPDATE billing_subscriptions
            SET amount_cents = 1
            WHERE id = :subscription_id
            """,
            {"subscription_id": subscription_id},
        ),
        (
            "DELETE FROM billing_payment_effects WHERE id = :effect_id",
            {"effect_id": topup_effect_id},
        ),
    )
    for statement, parameters in immutable_statements:
        async with postgres_harness.session() as session:
            with pytest.raises(DBAPIError):
                async with session.begin():
                    await session.execute(text(statement), parameters)

    now = datetime.now(timezone.utc)
    async with postgres_harness.session() as session:
        async with session.begin():
            with pytest.raises(BillingCapabilityRejected):
                await BillingRepository(session).set_topup_commercial_enabled(
                    environment="production",
                    expected_revision=0,
                    enabled=True,
                    evidence=_evidence(),
                )

    capability_id = uuid4()
    async with postgres_harness.session() as session:
        async with session.begin():
            await session.execute(
                text("""
                    INSERT INTO billing_topup_capability_receipts (
                        id, environment, schema_version, request_id,
                        operation_id, deployment_digest,
                        credit_deployment_digest, billing_fact_schema_digest,
                        producer_compatibility_digest,
                        consumer_compatibility_digest, inbox_high_watermark,
                        reconciliation_receipt_id, approved_by, approved_at,
                        expires_at, reconciliation_complete, readback_complete,
                        has_open_gap, has_backlog, has_alert
                    ) VALUES (
                        :id, 'production', 'credit_capability_ready.v1', :request_id,
                        :operation_id, :deployment_digest,
                        'credit-image-sha256', :schema_digest, :digest, :digest, 0,
                        :reconciliation_id, :approved_by, :approved_at,
                        :expires_at, true, true, false, false, false
                    )
                """),
                {
                    "id": capability_id,
                    "request_id": uuid4(),
                    "operation_id": uuid4(),
                    "deployment_digest": "d" * 64,
                    "digest": "e" * 64,
                    "schema_digest": billing_fact_schema_digest(),
                    "reconciliation_id": uuid4(),
                    "approved_by": uuid4(),
                    "approved_at": now,
                    "expires_at": now + timedelta(hours=2),
                },
            )
            await _seed_capability_binding(
                session,
                environment="production",
                capability_receipt_id=capability_id,
            )
    async with postgres_harness.session() as session:
        with pytest.raises(DBAPIError):
            async with session.begin():
                await session.execute(
                    text("""
                        UPDATE billing_rollout_control
                        SET topup_commercial_enabled = true,
                            active_topup_capability_id = :capability_id,
                            topup_revision = topup_revision + 1
                        WHERE environment = 'production'
                    """),
                    {"capability_id": capability_id},
                )

    async with postgres_harness.session() as session:
        with pytest.raises(DBAPIError):
            async with session.begin():
                await session.execute(
                    text("""
                        INSERT INTO billing_rollout_receipts (
                            id, environment, receipt_kind, nonce,
                            deployment_digest, from_state, to_state,
                            expected_revision, resulting_revision,
                            capability_receipt_id, actor_user_id, expires_at
                        ) VALUES (
                            :id, 'production', 'topup_gate_transition', :nonce,
                            :digest, 'topup_disabled', 'topup_enabled', 0, 1,
                            :capability_id, :actor, now() + interval '1 hour'
                        )
                    """),
                    {
                        "id": uuid4(),
                        "nonce": uuid4(),
                        "digest": "7" * 64,
                        "capability_id": capability_id,
                        "actor": uuid4(),
                    },
                )

    async with postgres_harness.session() as session:
        async with session.begin():
            async with session.begin_nested():
                control = await BillingRepository(session).set_topup_commercial_enabled(
                    environment="production",
                    expected_revision=0,
                    enabled=True,
                    evidence=_evidence(),
                    capability_receipt_id=capability_id,
                )
            assert control.state == "disabled"
            assert control.topup_commercial_enabled is True

    async with postgres_harness.session() as session:
        async with session.begin():
            await BillingRepository(session).transition_rollout(
                environment="production",
                expected_revision=0,
                to_state="canary_allowlist",
                canary_builder_id=postgres_harness.builder_id,
                evidence=_evidence(),
            )
        async with session.begin():
            control = await BillingRepository(session).transition_rollout(
                environment="production",
                expected_revision=1,
                to_state="general",
                evidence=_evidence(),
            )
            assert control.topup_commercial_enabled is True
            assert control.active_topup_capability_id == capability_id

    async with postgres_harness.session() as session:
        with pytest.raises(DBAPIError):
            async with session.begin():
                await session.execute(
                    text("""
                        UPDATE billing_topup_capability_receipts
                        SET has_alert = true WHERE id = :id
                    """),
                    {"id": capability_id},
                )

    async with postgres_harness.session() as session:
        async with session.begin():
            await BillingRepository(session).set_topup_commercial_enabled(
                environment="production",
                expected_revision=1,
                enabled=False,
                evidence=_evidence(),
                disable_reason="health",
            )
        async with session.begin():
            with pytest.raises(BillingCapabilityRejected):
                await BillingRepository(session).set_topup_commercial_enabled(
                    environment="production",
                    expected_revision=2,
                    enabled=True,
                    evidence=_evidence(),
                    capability_receipt_id=capability_id,
                )

    receipt_insert = text("""
        INSERT INTO billing_rollout_receipts (
            id, environment, receipt_kind, nonce, deployment_digest,
            from_state, to_state, expected_revision, resulting_revision,
            capability_receipt_id, actor_user_id, expires_at
        ) VALUES (
            :id, :environment, 'topup_gate_transition', :nonce, :digest,
            'topup_disabled', 'topup_enabled', 90, 91,
            :capability_id, :actor, now() + interval '1 hour'
        )
    """)
    for environment in ("production", "staging"):
        async with postgres_harness.session() as session:
            with pytest.raises(IntegrityError):
                async with session.begin():
                    await session.execute(
                        receipt_insert,
                        {
                            "id": uuid4(),
                            "environment": environment,
                            "nonce": uuid4(),
                            "digest": "5" * 64,
                            "capability_id": capability_id,
                            "actor": uuid4(),
                        },
                    )


async def test_canary_typed_slots_reject_two_topups_without_plus(
    postgres_harness: _Harness,
) -> None:
    order_ids = (uuid4(), uuid4())
    async with postgres_harness.session() as session:
        async with session.begin():
            await BillingRepository(session).transition_rollout(
                environment="staging",
                expected_revision=0,
                to_state="canary_allowlist",
                canary_builder_id=postgres_harness.builder_id,
                evidence=_evidence(),
            )
            for order_id in order_ids:
                await session.execute(
                    text("""
                        INSERT INTO billing_topup_orders (
                            id, builder_id, payment_method, catalog_version_id,
                            offer_id, amount_cents, credit_grant_mc,
                            pricing_snapshot
                        ) VALUES (
                            :id, :builder_id, 'pix', 'payment-2026-10-03',
                            'package_credits_500_v1', 10000, 500000,
                            CAST(:snapshot AS jsonb)
                        )
                    """),
                    {
                        "id": order_id,
                        "builder_id": postgres_harness.builder_id,
                        "snapshot": (
                            '{"catalog_version_id":"payment-2026-10-03",'
                            '"offer_id":"package_credits_500_v1",'
                            '"amount_cents":10000,"credit_grant_mc":500000,'
                            '"payment_method":"pix"}'
                        ),
                    },
                )
            await BillingRepository(session).authorize_and_reserve_topup_checkout(
                environment="staging",
                builder_id=postgres_harness.builder_id,
                topup_order_id=order_ids[0],
                checkout_session_id=uuid4(),
            )
            with pytest.raises(BillingCanaryBudgetExceeded):
                await BillingRepository(session).authorize_and_reserve_topup_checkout(
                    environment="staging",
                    builder_id=postgres_harness.builder_id,
                    topup_order_id=order_ids[1],
                    checkout_session_id=uuid4(),
                )
            row = (await session.execute(text("""
                    SELECT canary_subscription_checkout_id,
                           canary_topup_checkout_id, canary_checkout_count,
                           canary_amount_reserved_cents
                    FROM billing_rollout_control
                    WHERE environment = 'staging'
                """))).one()
            assert row[0] is None
            assert row[1] is not None
            assert tuple(row[2:]) == (1, 10000)


async def test_alias_identities_and_rollout_receipt_semantics_are_rejected(
    postgres_harness: _Harness,
) -> None:
    order_id = uuid4()
    delivery_id = uuid4()
    projection_id = uuid4()
    payment_id = f"pay-{projection_id}"
    builder_id = postgres_harness.foreign_builder_id
    async with postgres_harness.engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO billing_topup_orders (
                    id, builder_id, payment_method, catalog_version_id,
                    offer_id, amount_cents, credit_grant_mc, pricing_snapshot
                ) VALUES (
                    :id, :builder_id, 'pix', 'catalog-v1',
                    'package_credits_500_v1', 10000, 500,
                    '{"catalog_version_id":"catalog-v1"}'::jsonb
                )
            """),
            {"id": order_id, "builder_id": builder_id},
        )
        await connection.execute(
            text("""
                INSERT INTO billing_webhook_deliveries (
                    id, builder_id, provider, event_id, event_name,
                    provider_created_at, body_bytes, body_sha256
                ) VALUES (
                    :id, :builder_id, 'asaas', :event_id,
                    'PAYMENT_CREATED', now(), 2, :sha
                )
            """),
            {
                "id": delivery_id,
                "builder_id": builder_id,
                "event_id": f"evt-{delivery_id}",
                "sha": "7" * 64,
            },
        )
        await connection.execute(
            text("""
                INSERT INTO billing_payment_projections (
                    id, builder_id, provider, payment_id, aggregate_kind,
                    topup_order_id, status, amount_cents,
                    provider_created_at, ordering_key, last_event_id
                ) VALUES (
                    :id, :builder_id, 'asaas', :payment_id, 'topup',
                    :order_id, 'pending', 10000, now(), 'identity:1', :event_id
                )
            """),
            {
                "id": projection_id,
                "builder_id": builder_id,
                "payment_id": payment_id,
                "order_id": order_id,
                "event_id": f"evt-{delivery_id}",
            },
        )

    invalid_statements = (
        (
            """
            INSERT INTO billing_checkout_sessions (
                id, builder_id, intent_kind, intent_id, topup_order_id,
                attempt_no, catalog_version_id, offer_id, amount_cents,
                pricing_snapshot
            ) VALUES (
                :id, :builder_id, 'topup', :alias_id, :order_id, 1,
                'catalog-v1', 'package_credits_500_v1', 10000,
                '{"catalog_version_id":"catalog-v1"}'::jsonb
            )
            """,
            {"alias_id": uuid4(), "order_id": order_id},
        ),
        (
            """
            INSERT INTO billing_payment_effects (
                id, builder_id, projection_id, delivery_id, provider,
                payment_id, effect_kind, source_revision, amount_cents
            ) VALUES (
                :id, :builder_id, :projection_id, :delivery_id, 'asaas',
                'pay-alias', 'settlement', 'settlement:v1', 10000
            )
            """,
            {"projection_id": projection_id, "delivery_id": delivery_id},
        ),
        (
            """
            INSERT INTO billing_outbox (
                id, builder_id, schema_version, event_type, aggregate_kind,
                aggregate_id, topup_order_id, financial_revision,
                payload, payload_sha256
            ) VALUES (
                :id, :builder_id, 'billing.fact.v1',
                'topup.payment_settled.v1', 'topup_order', :alias_id,
                :order_id, 1, '{}'::jsonb, :sha
            )
            """,
            {"alias_id": uuid4(), "order_id": order_id, "sha": "8" * 64},
        ),
        (
            """
            INSERT INTO billing_rollout_receipts (
                id, environment, receipt_kind, nonce, deployment_digest,
                from_state, to_state, expected_revision, resulting_revision,
                actor_user_id, expires_at
            ) VALUES (
                :id, 'development', 'rollout_transition', :nonce, :digest,
                'topup_disabled', 'topup_enabled', 40, 41, :actor,
                now() + interval '1 hour'
            )
            """,
            {"nonce": uuid4(), "digest": "6" * 64, "actor": uuid4()},
        ),
    )
    for statement, parameters in invalid_statements:
        async with postgres_harness.session() as session:
            with pytest.raises(IntegrityError):
                async with session.begin():
                    await session.execute(
                        text(statement),
                        {
                            "id": uuid4(),
                            "builder_id": builder_id,
                            **parameters,
                        },
                    )


async def test_cross_builder_correlations_and_snapshot_pii_are_rejected(
    postgres_harness: _Harness,
) -> None:
    subscription_id = uuid4()
    async with postgres_harness.engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO billing_subscriptions (
                    id, builder_id, provider, external_subscription_id,
                    is_active, plan, billing_cycle, status
                ) VALUES (
                    :id, :builder_id, 'abacatepay', :external_id,
                    true, 'start', 'monthly', 'active'
                )
            """),
            {
                "id": subscription_id,
                "builder_id": postgres_harness.builder_id,
                "external_id": f"scope-{subscription_id}",
            },
        )
        await connection.execute(
            text("""
                INSERT INTO billing_subscriptions (
                    id, builder_id, provider, external_subscription_id,
                    is_active, plan, billing_cycle, status
                ) VALUES (
                    :id, :builder_id, 'asaas', :external_id,
                    true, 'plus', 'monthly', 'active'
                )
            """),
            {
                "id": uuid4(),
                "builder_id": postgres_harness.foreign_builder_id,
                "external_id": f"foreign-scope-{subscription_id}",
            },
        )

    async with postgres_harness.session() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                await session.execute(
                    text("""
                        INSERT INTO billing_checkout_sessions (
                            id, builder_id, intent_kind, intent_id, attempt_no,
                            resulting_external_subscription_id,
                            catalog_version_id, offer_id, amount_cents,
                            pricing_snapshot
                        ) VALUES (
                            :id, :builder_id, 'subscription', :intent_id, 1,
                            :external_id, 'catalog-v1', 'plan_plus_monthly_v1',
                            99900, '{}'::jsonb
                        )
                    """),
                    {
                        "id": uuid4(),
                        "builder_id": postgres_harness.builder_id,
                        "intent_id": uuid4(),
                        "external_id": f"foreign-scope-{subscription_id}",
                    },
                )

    async with postgres_harness.session() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                await session.execute(
                    text("""
                        INSERT INTO billing_migrations (
                            id, builder_id, subscription_id, renewal_date,
                            open_by_date, cutover_by_date,
                            target_catalog_version_id, target_offer_id,
                            target_plan, target_amount_cents, target_snapshot,
                            source_roster_revision
                        ) VALUES (
                            :id, :foreign_builder, :subscription_id,
                            DATE '2026-12-10', DATE '2026-12-03',
                            DATE '2026-12-08', 'catalog-v1',
                            'plan_plus_monthly_v1', 'plus', 99900,
                            '{"catalog_version_id":"catalog-v1"}'::jsonb, 1
                        )
                    """),
                    {
                        "id": uuid4(),
                        "foreign_builder": postgres_harness.foreign_builder_id,
                        "subscription_id": subscription_id,
                    },
                )

    insert_topup = text("""
        INSERT INTO billing_topup_orders (
            id, builder_id, payment_method, catalog_version_id,
            offer_id, amount_cents, credit_grant_mc, pricing_snapshot
        ) VALUES (
            :id, :builder_id, 'pix', 'catalog-v1',
            'package_credits_500_v1', 10000, 500,
            CAST(:snapshot AS jsonb)
        )
    """)
    async with postgres_harness.session() as session:
        async with session.begin():
            await session.execute(
                insert_topup,
                {
                    "id": uuid4(),
                    "builder_id": postgres_harness.builder_id,
                    "snapshot": (
                        '{"catalog_version_id":"catalog-v1",'
                        '"lines":[{"line_id":"topup",'
                        '"kind":"topup","quantity":1}]}'
                    ),
                },
            )

    for forbidden_key in ("email", "customer", "document"):
        async with postgres_harness.session() as session:
            with pytest.raises(IntegrityError):
                async with session.begin():
                    await session.execute(
                        insert_topup,
                        {
                            "id": uuid4(),
                            "builder_id": postgres_harness.builder_id,
                            "snapshot": (
                                '{"catalog_version_id":"catalog-v1",'
                                f'"lines":[{{"{forbidden_key}":"forbidden"}}]}}'
                            ),
                        },
                    )

    invalid_snapshots = (
        '{"catalog_version_id":"catalog-v1","lines":[{"note":"x"}]}',
        '{"catalog_version_id":"catalog-v1","lines":[{"full_name":"x"}]}',
        '{"catalog_version_id":"catalog-v1","lines":[' '{"line_id":{"alias":"x"}}]}',
        '{"catalog_version_id":"catalog-v1","lines":[' '{"line_id":["topup"]}]}',
        '{"catalog_version_id":"catalog-v1",'
        '"offer_id":{"alias":"package_credits_500_v1"}}',
    )
    for snapshot in invalid_snapshots:
        async with postgres_harness.session() as session:
            with pytest.raises(DBAPIError):
                async with session.begin():
                    await session.execute(
                        insert_topup,
                        {
                            "id": uuid4(),
                            "builder_id": postgres_harness.builder_id,
                            "snapshot": snapshot,
                        },
                    )

    async with postgres_harness.session() as session:
        async with session.begin():
            await session.execute(
                insert_topup,
                {
                    "id": uuid4(),
                    "builder_id": postgres_harness.builder_id,
                    "snapshot": (
                        '{"catalog_version_id":"catalog-v1",'
                        '"offer_id":"package_credits_500_v1",'
                        '"amount_cents":10000,"credit_grant_mc":500,'
                        '"payment_method":"pix","lines":[{'
                        '"line_id":"topup","offer_id":'
                        '"package_credits_500_v1","kind":"topup",'
                        '"quantity":1,"unit_amount_cents":10000,'
                        '"line_amount_cents":10000,"credit_grant_mc":500,'
                        '"settled_cents":0,"reversed_cents":0,'
                        '"restored_cents":0,'
                        '"line_financial_state":"pending"}]}'
                    ),
                },
            )


async def test_migration_snapshot_requires_exact_coherent_shape(
    postgres_harness: _Harness,
) -> None:
    subscription_id = uuid4()
    async with postgres_harness.session() as session:
        async with session.begin():
            await session.execute(
                text("""
                    INSERT INTO billing_subscriptions (
                        id, builder_id, provider, external_subscription_id,
                        is_active, plan, billing_cycle, status
                    ) VALUES (
                        :id, :builder_id, NULL, :external_id,
                        false, 'start', 'monthly', 'provider_unknown'
                    )
                """),
                {
                    "id": subscription_id,
                    "builder_id": postgres_harness.builder_id,
                    "external_id": f"snapshot-{subscription_id}",
                },
            )

            canonical = json.loads(_MIGRATION_SNAPSHOT_JSON)
            invalid_snapshots = [
                {},
                {"catalog_version_id": "catalog-v1"},
                {**canonical, "unexpected": True},
                {**canonical, "amount_cents": 99899},
                {**canonical, "lines": [{**canonical["lines"][0], "extra": 1}]},
            ]
            statement = text("""
                INSERT INTO billing_migrations (
                    id, builder_id, subscription_id, renewal_date,
                    open_by_date, cutover_by_date, target_catalog_version_id,
                    target_offer_id, target_plan, target_billing_cycle,
                    target_amount_cents, target_snapshot, source_roster_revision
                ) VALUES (
                    :id, :builder_id, :subscription_id, DATE '2026-12-10',
                    DATE '2026-12-03', DATE '2026-12-08', 'catalog-v1',
                    'plan_plus_monthly_v1', 'plus', 'monthly', 99900,
                    CAST(:snapshot AS jsonb), 1
                )
            """)
            for snapshot in invalid_snapshots:
                # Pinned: a literal source revision (CHECK > 0), so only the
                # snapshot allowlist can refuse these rows.
                with pytest.raises(
                    DBAPIError, match="ck_billing_migrations_snapshot_allowlist"
                ):
                    async with session.begin_nested():
                        await session.execute(
                            statement,
                            {
                                "id": uuid4(),
                                "builder_id": postgres_harness.builder_id,
                                "subscription_id": subscription_id,
                                "snapshot": json.dumps(snapshot),
                            },
                        )


async def test_receipts_and_outbox_reject_update_and_delete(
    postgres_harness: _Harness,
) -> None:
    # The billing_migration_receipts case left with migration 036: its
    # validator reads billing_subscriptions.roster_revision, so the legacy
    # table (empty by 036's preflight, dropped by 037) accepts no row.
    subscription_id = uuid4()
    outbox_id = uuid4()
    async with postgres_harness.engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO billing_subscriptions (
                    id, builder_id, provider, external_subscription_id,
                    is_active, plan, billing_cycle, status
                ) VALUES (
                    :id, :builder_id, 'abacatepay', :external_id,
                    true, 'start', 'monthly', 'active'
                )
            """),
            {
                "id": subscription_id,
                "builder_id": postgres_harness.builder_id,
                "external_id": f"immutable-{subscription_id}",
            },
        )
        await connection.execute(
            text("""
                INSERT INTO billing_outbox (
                    id, builder_id, schema_version, event_type,
                    aggregate_kind, aggregate_id, subscription_id,
                    financial_revision, payload, payload_sha256
                ) VALUES (
                    :id, :builder_id, 'billing.fact.v1',
                    'subscription.cycle_settled.v1', 'subscription_cycle',
                    :subscription_id, :subscription_id, 1, '{}'::jsonb, :sha
                )
            """),
            {
                "id": outbox_id,
                "builder_id": postgres_harness.builder_id,
                "subscription_id": subscription_id,
                "sha": "1" * 64,
            },
        )

    for statement, row_id in (
        (
            "UPDATE billing_outbox SET payload_sha256 = :sha WHERE id = :id",
            outbox_id,
        ),
        ("DELETE FROM billing_outbox WHERE id = :id", outbox_id),
    ):
        async with postgres_harness.session() as session:
            parameters: dict[str, Any] = {"id": row_id, "sha": "2" * 64}
            with pytest.raises(DBAPIError):
                async with session.begin():
                    await session.execute(text(statement), parameters)


async def test_rollbacks_leave_no_delivery_effect_or_order_outbox_partial(
    postgres_harness: _Harness,
) -> None:
    delivery_id = uuid4()
    projection_id = uuid4()
    effect_id = uuid4()
    order_id = uuid4()
    outbox_id = uuid4()

    async with postgres_harness.session() as session:
        await session.begin()
        try:
            await session.execute(
                text("""
                    INSERT INTO billing_webhook_deliveries (
                        id, builder_id, provider, event_id, event_name,
                        provider_created_at, body_bytes, body_sha256
                    ) VALUES (
                        :id, :builder_id, 'asaas', :event_id,
                        'PAYMENT_CONFIRMED', now(), 2, :sha
                    )
                """),
                {
                    "id": delivery_id,
                    "builder_id": postgres_harness.builder_id,
                    "event_id": f"rollback-{delivery_id}",
                    "sha": "3" * 64,
                },
            )
            raise RuntimeError("crash before projection/effect")
        except RuntimeError:
            await session.rollback()

    async with postgres_harness.session() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM billing_webhook_deliveries WHERE id = :id"),
                {"id": delivery_id},
            )
            == 0
        )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM billing_payment_projections WHERE id = :id"),
                {"id": projection_id},
            )
            == 0
        )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM billing_payment_effects WHERE id = :id"),
                {"id": effect_id},
            )
            == 0
        )

    async with postgres_harness.session() as session:
        await session.begin()
        try:
            await session.execute(
                text("""
                    INSERT INTO billing_topup_orders (
                        id, builder_id, payment_method, catalog_version_id,
                        offer_id, amount_cents, credit_grant_mc,
                        pricing_snapshot, state, financial_revision,
                        settled_cents
                    ) VALUES (
                        :id, :builder_id, 'pix', 'catalog-v1',
                        'package_credits_500_v1', 10000, 500,
                        '{"catalog_version_id":"catalog-v1"}'::jsonb,
                        'settled', 1, 10000
                    )
                """),
                {"id": order_id, "builder_id": postgres_harness.builder_id},
            )
            raise RuntimeError("crash before outbox")
        except RuntimeError:
            await session.rollback()

    async with postgres_harness.session() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM billing_topup_orders WHERE id = :id"),
                {"id": order_id},
            )
            == 0
        )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM billing_outbox WHERE id = :id"),
                {"id": outbox_id},
            )
            == 0
        )


async def test_financial_revision_requires_atomic_effect_outbox_lineage(
    postgres_harness: _Harness,
) -> None:
    """A committed financial aggregate cannot exist without its fact row."""

    orphan_order_id = uuid4()
    with pytest.raises(DBAPIError):
        async with postgres_harness.engine.begin() as connection:
            await connection.execute(
                text("""
                    INSERT INTO billing_topup_orders (
                        id, builder_id, payment_method, catalog_version_id,
                        offer_id, amount_cents, credit_grant_mc, pricing_snapshot,
                        state, financial_revision, settled_cents
                    ) VALUES (
                        :id, :builder_id, 'pix', 'catalog-v1',
                        'package_credits_500_v1', 10000, 500,
                        '{"catalog_version_id":"catalog-v1"}'::jsonb,
                        'settled', 1, 10000
                    )
                """),
                {
                    "id": orphan_order_id,
                    "builder_id": postgres_harness.foreign_builder_id,
                },
            )

    async with postgres_harness.engine.connect() as connection:
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM billing_topup_orders WHERE id = :id"),
                {"id": orphan_order_id},
            )
            == 0
        )

    order_id = uuid4()
    projection_id = uuid4()
    delivery_id = uuid4()
    effect_id = uuid4()
    outbox_id = uuid4()
    payment_id = f"pay-fact-{projection_id}"
    async with postgres_harness.engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO billing_topup_orders (
                    id, builder_id, payment_method, catalog_version_id,
                    offer_id, amount_cents, credit_grant_mc, pricing_snapshot
                ) VALUES (
                    :id, :builder_id, 'pix', 'catalog-v1',
                    'package_credits_500_v1', 10000, 500,
                    '{"catalog_version_id":"catalog-v1"}'::jsonb
                )
            """),
            {"id": order_id, "builder_id": postgres_harness.foreign_builder_id},
        )
        await connection.execute(
            text("""
                INSERT INTO billing_webhook_deliveries (
                    id, builder_id, provider, event_id, event_name,
                    provider_created_at, body_bytes, body_sha256
                ) VALUES (
                    :id, :builder_id, 'asaas', :event_id, 'PAYMENT_CONFIRMED',
                    now(), 2, :sha
                )
            """),
            {
                "id": delivery_id,
                "builder_id": postgres_harness.foreign_builder_id,
                "event_id": f"fact-{delivery_id}",
                "sha": "a" * 64,
            },
        )
        await connection.execute(
            text("""
                INSERT INTO billing_payment_projections (
                    id, builder_id, provider, payment_id, aggregate_kind,
                    topup_order_id, status, amount_cents, provider_created_at,
                    ordering_key, last_event_id
                ) VALUES (
                    :id, :builder_id, 'asaas', :payment_id, 'topup',
                    :order_id, 'pending', 10000, now(),
                    '2026-10-12T00:00:00Z:0', :event_id
                )
            """),
            {
                "id": projection_id,
                "builder_id": postgres_harness.foreign_builder_id,
                "payment_id": payment_id,
                "order_id": order_id,
                "event_id": f"fact-{delivery_id}",
            },
        )
        await connection.execute(
            text("""
                INSERT INTO billing_payment_effects (
                    id, builder_id, projection_id, delivery_id, provider,
                    payment_id, effect_kind, source_revision, amount_cents
                ) VALUES (
                    :id, :builder_id, :projection_id, :delivery_id, 'asaas',
                    :payment_id, 'settlement', 'settlement:v1', 10000
                )
            """),
            {
                "id": effect_id,
                "builder_id": postgres_harness.foreign_builder_id,
                "projection_id": projection_id,
                "delivery_id": delivery_id,
                "payment_id": payment_id,
            },
        )
        await connection.execute(
            text("""
                UPDATE billing_topup_orders
                SET state = 'settled', financial_revision = 1, settled_cents = 10000
                WHERE id = :id
            """),
            {"id": order_id},
        )
        await connection.execute(
            text("""
                INSERT INTO billing_outbox (
                    id, builder_id, schema_version, event_type, aggregate_kind,
                    aggregate_id, topup_order_id, payment_effect_id,
                    financial_revision, payload, payload_sha256
                ) VALUES (
                    :id, :builder_id, 'billing.fact.v1',
                    'topup.payment_settled.v1', 'topup_order',
                    :order_id, :order_id, :effect_id, 1, '{}'::jsonb, :sha
                )
            """),
            {
                "id": outbox_id,
                "builder_id": postgres_harness.foreign_builder_id,
                "order_id": order_id,
                "effect_id": effect_id,
                "sha": "b" * 64,
            },
        )

    async with postgres_harness.engine.connect() as connection:
        assert (
            await connection.scalar(
                text(
                    "SELECT financial_revision FROM billing_topup_orders WHERE id = :id"
                ),
                {"id": order_id},
            )
            == 1
        )
        assert (
            await connection.scalar(
                text("SELECT count(*) FROM billing_outbox WHERE id = :id"),
                {"id": outbox_id},
            )
            == 1
        )

    with pytest.raises(IntegrityError):
        async with postgres_harness.engine.begin() as connection:
            unknown_order_id = uuid4()
            await connection.execute(
                text("""
                    INSERT INTO billing_outbox (
                        id, builder_id, schema_version, event_type, aggregate_kind,
                        aggregate_id, topup_order_id, financial_revision,
                        payload, payload_sha256
                    ) VALUES (
                        :id, :builder_id, 'billing.fact.v1',
                        'topup.payment_settled.v1', 'topup_order',
                        :order_id, :order_id, 1, '{}'::jsonb, :sha
                    )
                """),
                {
                    "id": uuid4(),
                    "builder_id": postgres_harness.builder_id,
                    "order_id": unknown_order_id,
                    "sha": "c" * 64,
                },
            )


# ===========================================================================
# Group 2 (ASAAS checkout) additions — fix loop 1.
#
# These tests only append to the shared module harness: each seeds its own
# fresh builder, and the two re-authorization tests run last because they
# drive the ``development`` and ``production`` controls from the ``general``
# state the earlier Group 1 tests leave behind (asserted, not assumed).
# ===========================================================================


@dataclass(frozen=True)
class _CouponCheckout:
    builder_id: UUID
    code: str
    checkout_id: UUID
    transaction_ids: tuple[UUID, ...]


async def _seed_settled_coupon_checkout(
    harness: _Harness,
    *,
    payments: int,
    max_uses: int | None,
    uses_count: int,
) -> _CouponCheckout:
    """Seed one completed coupon checkout and ``payments`` settled transactions.

    The pricing snapshot is produced by the Group 2 service function, so the
    real ``billing_checkout_sessions`` CHECKs also validate its coupon shape.
    """

    from src.services.billing_catalog import BILLING_CATALOG
    from src.services.billing_checkout import subscription_pricing_snapshot

    builder_id = uuid4()
    subscription_id = uuid4()
    checkout_id = uuid4()
    code = f"G2C{uuid4().hex[:10].upper()}"
    snapshot = subscription_pricing_snapshot(
        BILLING_CATALOG["plan_plus_monthly_v1"],
        active_clinics=1,
        coupon_code=code,
        coupon_discount_percent=10,
    )
    amount = snapshot["amount_cents"]
    assert amount == 99_900 - 99_900 * 10 // 100 == 89_910
    transaction_ids: list[UUID] = []
    async with harness.session() as session:
        async with session.begin():
            await session.execute(
                text("INSERT INTO builders (id, name) VALUES (:id, 'G2 Coupon')"),
                {"id": builder_id},
            )
            await session.execute(
                text("""
                    INSERT INTO coupons (
                        id, code, discount_percent, max_uses, uses_count,
                        is_active
                    ) VALUES (:id, :code, 10, :max_uses, :uses_count, true)
                """),
                {
                    "id": uuid4(),
                    "code": code,
                    "max_uses": max_uses,
                    "uses_count": uses_count,
                },
            )
            await session.execute(
                text("""
                    INSERT INTO billing_subscriptions (
                        id, builder_id, provider, external_subscription_id,
                        is_active, plan, billing_cycle, catalog_version_id,
                        offer_id, amount_cents, status
                    ) VALUES (
                        :id, :builder_id, 'asaas', :external_id, true, 'plus',
                        'monthly', 'payment-2026-10-03',
                        'plan_plus_monthly_v1', :amount, 'active'
                    )
                """),
                {
                    "id": subscription_id,
                    "builder_id": builder_id,
                    "external_id": f"g2-sub-{subscription_id}",
                    "amount": amount,
                },
            )
            await session.execute(
                text("""
                    INSERT INTO billing_checkout_sessions (
                        id, builder_id, intent_kind, intent_id, attempt_no,
                        provider, state, catalog_version_id, offer_id,
                        amount_cents, pricing_snapshot, next_due_date
                    ) VALUES (
                        :id, :builder_id, 'subscription', :intent_id, 1,
                        'asaas', 'completed', 'payment-2026-10-03',
                        'plan_plus_monthly_v1', :amount,
                        CAST(:snapshot AS jsonb), DATE '2026-10-11'
                    )
                """),
                {
                    "id": checkout_id,
                    "builder_id": builder_id,
                    "intent_id": subscription_id,
                    "amount": amount,
                    "snapshot": json.dumps(snapshot),
                },
            )
            for index in range(payments):
                delivery_id = uuid4()
                projection_id = uuid4()
                effect_id = uuid4()
                transaction_id = uuid4()
                payment_id = f"g2-pay-{checkout_id.hex[:12]}-{index}"
                await session.execute(
                    text("""
                        INSERT INTO billing_webhook_deliveries (
                            id, builder_id, provider, event_id, event_name,
                            provider_created_at, body_bytes, body_sha256
                        ) VALUES (
                            :id, :builder_id, 'asaas', :event_id,
                            'PAYMENT_CONFIRMED', now(), 2, :sha
                        )
                    """),
                    {
                        "id": delivery_id,
                        "builder_id": builder_id,
                        "event_id": f"g2-event-{effect_id}",
                        "sha": "c" * 64,
                    },
                )
                await session.execute(
                    text("""
                        INSERT INTO billing_payment_projections (
                            id, builder_id, provider, payment_id,
                            aggregate_kind, subscription_id,
                            checkout_session_id, status, amount_cents,
                            settled_cents, provider_created_at, ordering_key,
                            last_event_id
                        ) VALUES (
                            :id, :builder_id, 'asaas', :payment_id,
                            'subscription', :subscription_id, :checkout_id,
                            'settled', :amount, :amount, now(), :ordering_key,
                            :last_event_id
                        )
                    """),
                    {
                        "id": projection_id,
                        "builder_id": builder_id,
                        "payment_id": payment_id,
                        "subscription_id": subscription_id,
                        "checkout_id": checkout_id,
                        "amount": amount,
                        "ordering_key": f"g2-order-{effect_id}",
                        "last_event_id": f"g2-event-{effect_id}",
                    },
                )
                await session.execute(
                    text("""
                        INSERT INTO billing_payment_effects (
                            id, builder_id, projection_id, delivery_id,
                            provider, payment_id, effect_kind, source_revision,
                            amount_cents
                        ) VALUES (
                            :id, :builder_id, :projection_id, :delivery_id,
                            'asaas', :payment_id, 'settlement', 'settlement:v1',
                            :amount
                        )
                    """),
                    {
                        "id": effect_id,
                        "builder_id": builder_id,
                        "projection_id": projection_id,
                        "delivery_id": delivery_id,
                        "payment_id": payment_id,
                        "amount": amount,
                    },
                )
                await session.execute(
                    text("""
                        INSERT INTO transactions (
                            id, builder_id, builder_name, amount_cents, method,
                            cycle, plan, event_type, provider,
                            provider_payment_id, billing_payment_effect_id
                        ) VALUES (
                            :id, :builder_id, 'G2 Coupon', :amount,
                            'credit_card', 'monthly', 'plus', 'paid', 'asaas',
                            :payment_id, :effect_id
                        )
                    """),
                    {
                        "id": transaction_id,
                        "builder_id": builder_id,
                        "amount": amount,
                        "payment_id": payment_id,
                        "effect_id": effect_id,
                    },
                )
                transaction_ids.append(transaction_id)
    return _CouponCheckout(
        builder_id=builder_id,
        code=code,
        checkout_id=checkout_id,
        transaction_ids=tuple(transaction_ids),
    )


async def _coupon_state(
    harness: _Harness, seeded: _CouponCheckout
) -> tuple[int, bool, list[str | None]]:
    async with harness.session() as session:
        coupon = (
            await session.execute(
                text("SELECT uses_count, is_active FROM coupons WHERE code = :code"),
                {"code": seeded.code},
            )
        ).one()
        codes = [
            (
                await session.scalar(
                    text("SELECT coupon_code FROM transactions WHERE id = :id"),
                    {"id": transaction_id},
                )
            )
            for transaction_id in seeded.transaction_ids
        ]
    return int(coupon[0]), bool(coupon[1]), codes


async def test_g2_checkout_coupon_consumption_serializes_on_the_checkout_row(
    postgres_harness: _Harness,
) -> None:
    seeded = await _seed_settled_coupon_checkout(
        postgres_harness, payments=2, max_uses=2, uses_count=1
    )
    first = postgres_harness.session()
    second = postgres_harness.session()
    replay = postgres_harness.session()

    async def consume(session: AsyncSession, transaction_id: UUID) -> str | None:
        try:
            async with session.begin():
                return await BillingRepository(session).consume_checkout_coupon(
                    builder_id=seeded.builder_id, transaction_id=transaction_id
                )
        finally:
            await session.close()

    try:
        await first.begin()
        consumed = await BillingRepository(first).consume_checkout_coupon(
            builder_id=seeded.builder_id,
            transaction_id=seeded.transaction_ids[0],
        )
        assert consumed == seeded.code
        # Another settled payment of the same checkout and a replay of the same
        # transaction must both wait on the locked checkout row.
        other_payment = asyncio.create_task(consume(second, seeded.transaction_ids[1]))
        same_payment = asyncio.create_task(consume(replay, seeded.transaction_ids[0]))
        done, _ = await asyncio.wait({other_payment, same_payment}, timeout=0.3)
        assert not done, "concurrent coupon consumers did not wait on the lock"
        await first.commit()
        assert await other_payment is None
        assert await same_payment is None
    finally:
        if first.in_transaction():
            await first.rollback()
        await first.close()

    uses_count, is_active, codes = await _coupon_state(postgres_harness, seeded)
    assert uses_count == 2
    assert is_active is False  # deactivated on reaching max_uses
    assert codes == [seeded.code, None]

    # Sequential replay after commit is still a no-op.
    async with postgres_harness.session() as session:
        async with session.begin():
            assert (
                await BillingRepository(session).consume_checkout_coupon(
                    builder_id=seeded.builder_id,
                    transaction_id=seeded.transaction_ids[0],
                )
                is None
            )
    assert await _coupon_state(postgres_harness, seeded) == (
        2,
        False,
        [seeded.code, None],
    )

    # Tenant scope and settlement provenance are mandatory.
    for builder_id, transaction_id in (
        (postgres_harness.foreign_builder_id, seeded.transaction_ids[1]),
        (postgres_harness.builder_id, postgres_harness.legacy_transaction_id),
    ):
        async with postgres_harness.session() as session:
            with pytest.raises(
                BillingTransitionRejected,
                match="^settled checkout transaction unavailable$",
            ):
                async with session.begin():
                    await BillingRepository(session).consume_checkout_coupon(
                        builder_id=builder_id, transaction_id=transaction_id
                    )
    assert await _coupon_state(postgres_harness, seeded) == (
        2,
        False,
        [seeded.code, None],
    )


async def test_g2_checkout_coupon_is_consumed_exactly_once_by_a_concurrent_storm(
    postgres_harness: _Harness,
) -> None:
    seeded = await _seed_settled_coupon_checkout(
        postgres_harness, payments=3, max_uses=None, uses_count=0
    )
    start = asyncio.Event()

    async def consume(transaction_id: UUID) -> str | None:
        async with postgres_harness.session() as session:
            await start.wait()
            async with session.begin():
                return await BillingRepository(session).consume_checkout_coupon(
                    builder_id=seeded.builder_id, transaction_id=transaction_id
                )

    tasks = [
        asyncio.create_task(consume(transaction_id))
        for transaction_id in seeded.transaction_ids * 2
    ]
    await asyncio.sleep(0)
    start.set()
    results = await asyncio.gather(*tasks)

    assert sorted(result or "" for result in results) == [""] * 5 + [seeded.code]
    uses_count, is_active, codes = await _coupon_state(postgres_harness, seeded)
    assert uses_count == 1
    assert is_active is True  # unlimited coupon stays active
    assert sorted(code or "" for code in codes) == ["", "", seeded.code]


async def _ensure_general_rollout(harness: _Harness, environment: str) -> int:
    """Drive ``environment`` to ``general`` through legal transitions.

    In the full module run the Group 1 tests already left it ``general``; when
    these tests run alone the control is freshly ``disabled``.  The harness
    builder is the canary builder because any lifetime slot belongs to it.
    """

    # One transition per transaction: each receipt must match the committed
    # final control state (deferred Group 1 receipt trigger).
    for _ in range(3):
        async with harness.session() as session:
            async with session.begin():
                repository = BillingRepository(session)
                control = await repository.lock_rollout_control(environment=environment)
                if control.state == "general":
                    return int(control.rollout_revision)
                await repository.transition_rollout(
                    environment=environment,
                    expected_revision=control.rollout_revision,
                    to_state=(
                        "canary_allowlist" if control.state == "disabled" else "general"
                    ),
                    canary_builder_id=(
                        harness.builder_id if control.state == "disabled" else None
                    ),
                    evidence=_evidence(),
                )
    raise AssertionError(f"{environment} rollout did not reach general")


async def test_g2_prepost_recheck_denies_disabled_rollout_and_failed_frees_intent(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import AsyncMock

    from src.services import billing_checkout as checkout_module
    from src.services.billing_checkout import (
        BillingCheckoutService,
        CheckoutUnavailable,
    )

    revision = await _ensure_general_rollout(postgres_harness, "development")

    builder_id = uuid4()
    async with postgres_harness.session() as session:
        async with session.begin():
            await session.execute(
                text("INSERT INTO builders (id, name) VALUES (:id, 'G2 Recheck')"),
                {"id": builder_id},
            )

    monkeypatch.setattr(checkout_module.settings, "environment", "development")
    monkeypatch.setattr(
        checkout_module.settings,
        "asaas_checkout_return_url",
        "https://app.example.test/billing",
    )
    monkeypatch.setattr(
        checkout_module, "require_billing_checkout_rollout", AsyncMock()
    )
    monkeypatch.setattr(
        checkout_module, "authorize_creation_egress", AsyncMock(return_value=object())
    )
    original = BillingCheckoutService._reauthorize_before_post
    reserved: list[UUID] = []

    async def disable_then_recheck(
        self: BillingCheckoutService, *, builder_id: UUID, checkout_session_id: UUID
    ) -> None:
        reserved.append(checkout_session_id)
        # A concurrent operator disables rollout after the reservation commit.
        async with postgres_harness.session() as operator:
            async with operator.begin():
                await BillingRepository(operator).transition_rollout(
                    environment="development",
                    expected_revision=revision,
                    to_state="disabled",
                    evidence=_evidence(),
                )
        await original(
            self, builder_id=builder_id, checkout_session_id=checkout_session_id
        )

    monkeypatch.setattr(
        BillingCheckoutService, "_reauthorize_before_post", disable_then_recheck
    )
    posts: list[object] = []

    class Client:
        async def create_checkout(self, request: object, *, permit: object) -> None:
            posts.append(request)
            raise AssertionError("provider POST after a disabled rollout")

    async with postgres_harness.session() as session:
        with pytest.raises(CheckoutUnavailable):
            await BillingCheckoutService(
                session,
                client_factory=Client,
                clock=lambda: datetime(2026, 10, 12, 2, 59, 59, tzinfo=timezone.utc),
            ).create_checkout(
                builder_id=builder_id,
                offer_id="plan_plus_monthly_v1",
                payment_method="credit_card",
            )

    assert posts == []
    assert len(reserved) == 1
    async with postgres_harness.session() as session:
        row = (
            await session.execute(
                text("""
                    SELECT state, reconciliation_attempts, external_checkout_id,
                           intent_id, next_due_date
                    FROM billing_checkout_sessions WHERE id = :id
                """),
                {"id": reserved[0]},
            )
        ).one()
    assert tuple(row[:3]) == ("failed", 0, None)
    assert row[4] == date(2026, 10, 11)  # São Paulo date of 02:59:59Z

    # The repository re-check itself denies with its pinned guard message.
    async with postgres_harness.session() as session:
        await session.execute(
            text("""
                INSERT INTO billing_checkout_sessions (
                    id, builder_id, intent_kind, intent_id, attempt_no,
                    provider, state, catalog_version_id, offer_id,
                    amount_cents, pricing_snapshot, next_due_date
                )
                SELECT :id, builder_id, intent_kind, intent_id, 2, provider,
                       'creating', catalog_version_id, offer_id, amount_cents,
                       pricing_snapshot, next_due_date
                FROM billing_checkout_sessions WHERE id = :source_id
            """),
            {"id": uuid4(), "source_id": reserved[0]},
        )
        # ^ the failed attempt no longer occupies the live-intent index: a
        # new live attempt for the very same intent is admitted.
        retry_id = await session.scalar(
            text("""
                SELECT id FROM billing_checkout_sessions
                WHERE intent_id = :intent_id AND attempt_no = 2
            """),
            {"intent_id": row[3]},
        )
        with pytest.raises(
            BillingTransitionRejected,
            match="^checkout egress requires general rollout$",
        ):
            await BillingRepository(session).authorize_reserved_checkout_egress(
                environment="development",
                builder_id=builder_id,
                checkout_session_id=retry_id,
            )
        await session.rollback()
    async with postgres_harness.session() as session:
        with pytest.raises(
            BillingTransitionRejected,
            match="^checkout attempt is not awaiting egress$",
        ):
            await BillingRepository(session).authorize_reserved_checkout_egress(
                environment="development",
                builder_id=builder_id,
                checkout_session_id=reserved[0],
            )
        await session.rollback()


async def test_g2_prepost_recheck_reads_topup_gate_capability_and_order(
    postgres_harness: _Harness,
) -> None:
    from src.services.billing_checkout import BillingCheckoutService

    async with postgres_harness.session() as session:
        async with session.begin():
            repository = BillingRepository(session)
            control = await repository.lock_rollout_control(environment="production")
            if control.topup_commercial_enabled:
                await repository.set_topup_commercial_enabled(
                    environment="production",
                    expected_revision=control.topup_revision,
                    enabled=False,
                    evidence=_evidence(),
                    disable_reason="health",
                )
    await _ensure_general_rollout(postgres_harness, "production")
    async with postgres_harness.session() as session:
        topup_revision, gate = (await session.execute(text("""
                    SELECT topup_revision, topup_commercial_enabled
                    FROM billing_rollout_control
                    WHERE environment = 'production'
                """))).one()
    assert gate is False, "the production top-up gate starts closed"

    builder_id = uuid4()
    order_id = uuid4()
    checkout_id = uuid4()
    capability_id = uuid4()
    now = datetime.now(timezone.utc)
    async with postgres_harness.session() as session:
        async with session.begin():
            await session.execute(
                text("INSERT INTO builders (id, name) VALUES (:id, 'G2 Topup')"),
                {"id": builder_id},
            )
            await session.execute(
                text("""
                    INSERT INTO billing_topup_capability_receipts (
                        id, environment, schema_version, request_id,
                        operation_id, deployment_digest,
                        credit_deployment_digest, billing_fact_schema_digest,
                        producer_compatibility_digest,
                        consumer_compatibility_digest, inbox_high_watermark,
                        reconciliation_receipt_id, approved_by, approved_at,
                        expires_at, reconciliation_complete, readback_complete,
                        has_open_gap, has_backlog, has_alert
                    ) VALUES (
                        :id, 'production', 'credit_capability_ready.v1',
                        :request_id, :operation_id, :deployment_digest,
                        'credit-image-sha256', :schema_digest, :digest,
                        :digest, 0, :reconciliation_id, :approved_by,
                        :approved_at, :expires_at, true, true, false, false,
                        false
                    )
                """),
                {
                    "id": capability_id,
                    "request_id": uuid4(),
                    "operation_id": uuid4(),
                    "deployment_digest": "d" * 64,
                    "digest": "f" * 64,
                    "schema_digest": billing_fact_schema_digest(),
                    "reconciliation_id": uuid4(),
                    "approved_by": uuid4(),
                    "approved_at": now - timedelta(minutes=1),
                    "expires_at": now + timedelta(hours=2),
                },
            )
            await _seed_capability_binding(
                session,
                environment="production",
                capability_receipt_id=capability_id,
            )
    async with postgres_harness.session() as session:
        async with session.begin():
            await BillingRepository(session).set_topup_commercial_enabled(
                environment="production",
                expected_revision=topup_revision,
                enabled=True,
                evidence=_evidence(),
                capability_receipt_id=capability_id,
            )
            await session.execute(
                text("""
                    INSERT INTO billing_topup_orders (
                        id, builder_id, payment_method, catalog_version_id,
                        offer_id, amount_cents, credit_grant_mc,
                        pricing_snapshot
                    ) VALUES (
                        :id, :builder_id, 'pix', 'payment-2026-10-03',
                        'package_credits_1000_v1', 20000, 1000,
                        CAST(:snapshot AS jsonb)
                    )
                """),
                {
                    "id": order_id,
                    "builder_id": builder_id,
                    "snapshot": (
                        '{"catalog_version_id":"payment-2026-10-03",'
                        '"offer_id":"package_credits_1000_v1",'
                        '"amount_cents":20000,"credit_grant_mc":1000,'
                        '"payment_method":"pix"}'
                    ),
                },
            )
            await BillingRepository(session).authorize_and_reserve_topup_checkout(
                environment="production",
                builder_id=builder_id,
                topup_order_id=order_id,
                checkout_session_id=checkout_id,
            )

    # Open gate + valid capability + pending order: authorized (real SQL).
    async with postgres_harness.session() as session:
        async with session.begin():
            authorized = await BillingRepository(
                session
            ).authorize_reserved_checkout_egress(
                environment="production",
                builder_id=builder_id,
                checkout_session_id=checkout_id,
            )
            assert authorized.id == checkout_id
            assert authorized.state == "creating"
        # A foreign builder never sees the attempt.
        with pytest.raises(
            BillingTransitionRejected,
            match="^checkout attempt is not awaiting egress$",
        ):
            async with session.begin():
                await BillingRepository(session).authorize_reserved_checkout_egress(
                    environment="production",
                    builder_id=postgres_harness.foreign_builder_id,
                    checkout_session_id=checkout_id,
                )

    # The gate closes after the reservation commit: the re-check denies.
    async with postgres_harness.session() as session:
        async with session.begin():
            await BillingRepository(session).set_topup_commercial_enabled(
                environment="production",
                expected_revision=topup_revision + 1,
                enabled=False,
                evidence=_evidence(),
                disable_reason="health",
            )
    async with postgres_harness.session() as session:
        with pytest.raises(
            BillingCapabilityRejected,
            match="^topup capability is required$",
        ):
            async with session.begin():
                await BillingRepository(session).authorize_reserved_checkout_egress(
                    environment="production",
                    builder_id=builder_id,
                    checkout_session_id=checkout_id,
                )
        # The never-sent attempt ends conclusively and legally as ``failed``.
        await BillingCheckoutService(
            session, client_factory=lambda: None  # type: ignore[arg-type,return-value]
        )._mark_failed_not_sent(builder_id=builder_id, checkout_session_id=checkout_id)
    async with postgres_harness.session() as session:
        assert (
            await session.scalar(
                text("SELECT state FROM billing_checkout_sessions WHERE id = :id"),
                {"id": checkout_id},
            )
            == "failed"
        )


async def test_fact_producer_inserts_outbox_delivery_and_handoff_in_fk_order(
    postgres_harness: _Harness,
) -> None:
    """Group 4 defect found by the Group 5 cycle: one transaction, three FK rows.

    ``BillingFactProducer.record_effect`` inserts the outbox fact, its delivery
    retry row and its handoff.  The mappers have no ``relationship()``, so a
    single flush let the unit of work emit ``billing_fact_deliveries`` before
    ``billing_outbox`` and PostgreSQL refused ``fk_billing_fact_deliveries_outbox``.
    Inputs (order, webhook delivery, settled projection, settlement effect) are
    pre-existing webhook state; only the producer writes the three rows.
    """

    from src.models.billing import BillingPaymentEffect, BillingPaymentProjection
    from src.services.billing_catalog import BILLING_CATALOG
    from src.services.billing_checkout import topup_pricing_snapshot
    from src.services.billing_outbox import BillingFactProducer

    builder_id = postgres_harness.foreign_builder_id
    order_id, delivery_id, projection_id, effect_id = (uuid4() for _ in range(4))
    payment_id = f"pay-fk-order-{projection_id}"
    snapshot = topup_pricing_snapshot(
        BILLING_CATALOG["package_credits_500_v1"], payment_method="pix"
    )
    async with postgres_harness.engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO billing_topup_orders (
                    id, builder_id, payment_method, catalog_version_id,
                    offer_id, amount_cents, credit_grant_mc, pricing_snapshot
                ) VALUES (
                    :id, :builder_id, 'pix', :catalog_version_id,
                    'package_credits_500_v1', 10000, :credit_grant_mc,
                    CAST(:snapshot AS JSONB)
                )
            """),
            {
                "id": order_id,
                "builder_id": builder_id,
                "catalog_version_id": snapshot["catalog_version_id"],
                "credit_grant_mc": snapshot["credit_grant_mc"],
                "snapshot": json.dumps(snapshot),
            },
        )
        await connection.execute(
            text("""
                INSERT INTO billing_webhook_deliveries (
                    id, builder_id, provider, event_id, event_name,
                    provider_created_at, body_bytes, body_sha256
                ) VALUES (
                    :id, :builder_id, 'asaas', :event_id, 'PAYMENT_RECEIVED',
                    now(), 2, :sha
                )
            """),
            {
                "id": delivery_id,
                "builder_id": builder_id,
                "event_id": f"fk-order-{delivery_id}",
                "sha": "a" * 64,
            },
        )
        await connection.execute(
            text("""
                INSERT INTO billing_payment_projections (
                    id, builder_id, provider, payment_id, aggregate_kind,
                    topup_order_id, status, amount_cents, settled_cents,
                    paid_at, provider_created_at, ordering_key, last_event_id
                ) VALUES (
                    :id, :builder_id, 'asaas', :payment_id, 'topup',
                    :order_id, 'settled', 10000, 10000, DATE '2026-10-11', now(),
                    '2026-10-12T00:00:00Z:0', :event_id
                )
            """),
            {
                "id": projection_id,
                "builder_id": builder_id,
                "payment_id": payment_id,
                "order_id": order_id,
                "event_id": f"fk-order-{delivery_id}",
            },
        )
    async with postgres_harness.session() as session:
        async with session.begin():
            await session.execute(
                text("""
                    INSERT INTO billing_payment_effects (
                        id, builder_id, projection_id, delivery_id, provider,
                        payment_id, effect_kind, source_revision, amount_cents
                    ) VALUES (
                        :id, :builder_id, :projection_id, :delivery_id, 'asaas',
                        :payment_id, 'settlement', 'settlement:v1', 10000
                    )
                """),
                {
                    "id": effect_id,
                    "builder_id": builder_id,
                    "projection_id": projection_id,
                    "delivery_id": delivery_id,
                    "payment_id": payment_id,
                },
            )
            effect = await session.get(BillingPaymentEffect, effect_id)
            projection = await session.get(BillingPaymentProjection, projection_id)
            assert effect is not None and projection is not None
            outbox = await BillingFactProducer(session).record_effect(
                effect, projection
            )
            assert outbox is not None
            outbox_id = outbox.id

    async with postgres_harness.session() as session:
        row = (
            await session.execute(
                text("""
                    SELECT outbox.payment_effect_id, outbox.topup_order_id,
                           delivery.outbox_event_id, delivery.event_id,
                           handoff.outbox_event_id, handoff.delivery_id = delivery.id,
                           orders.financial_revision, orders.state
                    FROM billing_outbox outbox
                    JOIN billing_fact_deliveries delivery
                      ON delivery.outbox_event_id = outbox.id
                    JOIN billing_fact_handoffs handoff
                      ON handoff.outbox_event_id = outbox.id
                    JOIN billing_topup_orders orders ON orders.id = outbox.topup_order_id
                    WHERE outbox.id = :id
                """),
                {"id": outbox_id},
            )
        ).one()
    assert tuple(row) == (
        effect_id,
        order_id,
        outbox_id,
        outbox_id,
        outbox_id,
        True,
        1,
        "settled",
    )
