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

from src.api.routes import billing as billing_route
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
        await _run_alembic(migrated_url, "upgrade", "026")
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
        await _run_alembic(migrated_url, "upgrade", "026")
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


async def _insert_provider_flip_receipt(
    session: AsyncSession,
    migration_id: UUID,
    *,
    valid_digest: bool = True,
) -> None:
    digest_sql = (
        "encode(sha256(convert_to(jsonb_build_object("
        "'migration_id', migration.id, 'builder_id', migration.builder_id, "
        "'subscription_id', migration.subscription_id, "
        "'operation_id', migration.operation_id, "
        "'source_roster_revision', migration.source_roster_revision, "
        "'target_provider', migration.target_provider, "
        "'target_external_subscription_id', migration.target_external_subscription_id, "
        "'target_catalog_version_id', migration.target_catalog_version_id, "
        "'target_offer_id', migration.target_offer_id, "
        "'target_plan', migration.target_plan, "
        "'target_billing_cycle', migration.target_billing_cycle, "
        "'target_coupon_code', migration.target_coupon_code, "
        "'target_amount_cents', migration.target_amount_cents, "
        "'target_snapshot', migration.target_snapshot"
        ")::text, 'UTF8')), 'hex')"
        if valid_digest
        else "repeat('a', 64)"
    )
    await session.execute(
        text(f"""
            INSERT INTO billing_migration_receipts (
                id, builder_id, migration_id, operation_id, receipt_kind,
                provider, external_reference, status, content_sha256,
                actor_user_id, observed_at
            ) SELECT :id, migration.builder_id, migration.id,
                migration.operation_id, 'provider_flip', 'asaas',
                migration.target_external_subscription_id, 'verified',
                {digest_sql}, :actor, now()
            FROM billing_migrations AS migration
            WHERE migration.id = :migration_id
        """),
        {"id": uuid4(), "migration_id": migration_id, "actor": uuid4()},
    )


async def _seed_bound_legacy_flip(
    session: AsyncSession,
    *,
    builder_id: UUID,
    source_revision: int,
    valid_digest: bool = True,
    include_provider_receipt: bool = True,
) -> tuple[UUID, UUID, str]:
    subscription_id = uuid4()
    migration_id = uuid4()
    operation_id = uuid4()
    target_external_id = f"asaas-{subscription_id}"
    await session.execute(
        text("""
            INSERT INTO billing_subscriptions (
                id, builder_id, provider, external_subscription_id,
                is_active, plan, billing_cycle, status, roster_revision
            ) VALUES (
                :id, :builder_id, NULL, :legacy_external_id,
                false, 'start', 'monthly', 'legacy_cancel_pending', :revision
            )
        """),
        {
            "id": subscription_id,
            "builder_id": builder_id,
            "legacy_external_id": f"legacy-{subscription_id}",
            "revision": source_revision,
        },
    )
    await session.execute(
        text("""
            INSERT INTO billing_migrations (
                id, builder_id, subscription_id, state, renewal_date,
                open_by_date, cutover_by_date, target_external_subscription_id,
                target_catalog_version_id, target_offer_id, target_plan,
                target_billing_cycle, target_amount_cents, target_snapshot,
                operation_id, source_roster_revision, legacy_cancelled_at
            ) VALUES (
                :id, :builder_id, :subscription_id, 'legacy_cancel_pending',
                DATE '2026-12-10', DATE '2026-12-03', DATE '2026-12-08',
                :target_external_id, 'payment-2026-10-03',
                'plan_plus_monthly_v1', 'plus', 'monthly', 99900,
                jsonb_build_object(
                  'catalog_version_id','payment-2026-10-03',
                  'offer_id','plan_plus_monthly_v1','plan','plus',
                  'billing_cycle','monthly','coupon_code',NULL,
                  'amount_cents',99900,'credit_grant_mc',5000,
                  'included_clinics',1,'additional_clinics',0,
                  'lines',jsonb_build_array(jsonb_build_object(
                    'line_id','plan','offer_id','plan_plus_monthly_v1',
                    'kind','plan','quantity',1,'unit_amount_cents',99900,
                    'line_amount_cents',99900,'credit_grant_mc',5000,
                    'settled_cents',0,'reversed_cents',0,'restored_cents',0,
                    'line_financial_state','pending'))),
                :operation_id, :revision, NULL
            )
        """),
        {
            "id": migration_id,
            "builder_id": builder_id,
            "subscription_id": subscription_id,
            "target_external_id": target_external_id,
            "operation_id": operation_id,
            "revision": source_revision,
        },
    )
    await session.execute(
        text("""
            INSERT INTO billing_migration_receipts (
                id, builder_id, migration_id, operation_id, receipt_kind,
                provider, external_reference, status, content_sha256,
                actor_user_id, observed_at
            ) SELECT :id, migration.builder_id, migration.id,
                migration.operation_id, 'cancel_observation', 'abacatepay',
                subscription.external_subscription_id, 'cancelled',
                encode(sha256(convert_to(jsonb_build_object(
                  'migration_id',migration.id,'builder_id',migration.builder_id,
                  'subscription_id',migration.subscription_id,
                  'operation_id',migration.operation_id,
                  'legacy_external_subscription_id',
                    subscription.external_subscription_id,
                  'provider','abacatepay','status','cancelled',
                  'observed_at',CAST(:observed_at AS TIMESTAMPTZ)
                )::text,'UTF8')),'hex'), :actor,
                CAST(:observed_at AS TIMESTAMPTZ)
            FROM billing_migrations migration
            JOIN billing_subscriptions subscription
              ON subscription.id = migration.subscription_id
             AND subscription.builder_id = migration.builder_id
            WHERE migration.id = :migration_id
        """),
        {
            "id": uuid4(),
            "migration_id": migration_id,
            "actor": uuid4(),
            "observed_at": datetime.now(timezone.utc),
        },
    )
    await session.execute(
        text("""
            UPDATE billing_subscriptions SET status = 'legacy_cancelled'
            WHERE id = :subscription_id
        """),
        {"subscription_id": subscription_id},
    )
    if include_provider_receipt:
        await _insert_provider_flip_receipt(
            session,
            migration_id,
            valid_digest=valid_digest,
        )
    return subscription_id, migration_id, target_external_id


async def test_upgrade_downgrade_and_fresh_schema_are_equivalent(
    postgres_harness: _Harness,
) -> None:
    assert postgres_harness.schemas_equivalent is True
    assert postgres_harness.legacy_survived_downgrade is True
    async with postgres_harness.engine.connect() as connection:
        assert (
            await connection.scalar(text("SELECT version_num FROM alembic_version"))
            == "026"
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


async def test_group_zero_binding_fences_uninitialized_roster_and_keeps_annual(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(billing_route.settings, "environment", "test")
    async with postgres_harness.session() as session:
        with pytest.raises(billing_route.AbacatePayRosterUnavailable):
            await billing_route._get_abacatepay_roster_binding("sub-annual", session)

    async with postgres_harness.session() as session:
        async with session.begin():
            await session.execute(text("""
                    UPDATE billing_roster_control
                    SET billing_roster_revision = 1, updated_at = now()
                    WHERE singleton = true
                """))
        assert (
            await billing_route._get_abacatepay_roster_binding(
                "sub-not-present", session
            )
            is None
        )

    subscription_id = uuid4()
    async with postgres_harness.session() as session:
        async with session.begin():
            await session.execute(
                text("""
                    INSERT INTO billing_subscriptions (
                        id, builder_id, provider, external_subscription_id,
                        is_active, plan, billing_cycle, coupon_code, status,
                        roster_revision
                    ) VALUES (
                        :id, :builder_id, 'abacatepay', 'sub-annual', true,
                        'start', 'annual', 'LEGACY10', 'active', 1
                    )
                """),
                {"id": subscription_id, "builder_id": postgres_harness.builder_id},
            )
        binding = await billing_route._get_abacatepay_roster_binding(
            "sub-annual", session
        )
        assert binding == billing_route.AbacatePayRosterBinding(
            builder_id=postgres_harness.builder_id,
            plan="start",
            billing_cycle="annual",
            coupon_code="LEGACY10",
        )
        await session.commit()
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await session.execute(
                    text("""
                        UPDATE billing_subscriptions
                        SET provider = NULL, is_active = false,
                            status = 'legacy_cancel_pending', roster_revision = 2
                        WHERE id = :id
                    """),
                    {"id": subscription_id},
                )


async def test_legacy_receiver_and_roster_writer_serialize_in_both_orders(
    postgres_harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(billing_route.settings, "environment", "test")

    async def seed_legacy(session: AsyncSession, revision: int) -> tuple[UUID, str]:
        subscription_id = uuid4()
        external_id = f"legacy-race-{subscription_id}"
        await session.execute(
            text("""
                INSERT INTO billing_subscriptions (
                    id, builder_id, provider, external_subscription_id,
                    is_active, plan, billing_cycle, status, roster_revision
                ) VALUES (
                    :id, :builder_id, 'abacatepay', :external_id,
                    true, 'start', 'monthly', 'active', :revision
                )
            """),
            {
                "id": subscription_id,
                "builder_id": postgres_harness.builder_id,
                "external_id": external_id,
                "revision": revision,
            },
        )
        await session.execute(
            text("""
                INSERT INTO billing_migrations (
                    id, builder_id, subscription_id, state, renewal_date,
                    open_by_date, cutover_by_date,
                    target_external_subscription_id,
                    target_catalog_version_id, target_offer_id, target_plan,
                    target_billing_cycle, target_amount_cents, target_snapshot,
                    operation_id, source_roster_revision
                ) VALUES (
                    :id, :builder_id, :subscription_id, 'in_progress',
                    DATE '2026-12-10', DATE '2026-12-03', DATE '2026-12-08',
                    :target, 'payment-2026-10-03',
                    'plan_plus_monthly_v1', 'plus', 'monthly', 99900,
                    jsonb_build_object(
                      'catalog_version_id','payment-2026-10-03',
                      'offer_id','plan_plus_monthly_v1','plan','plus',
                      'billing_cycle','monthly','coupon_code',NULL,
                      'amount_cents',99900,'credit_grant_mc',5000,
                      'included_clinics',1,'additional_clinics',0,
                      'lines',jsonb_build_array(jsonb_build_object(
                        'line_id','plan','offer_id','plan_plus_monthly_v1',
                        'kind','plan','quantity',1,'unit_amount_cents',99900,
                        'line_amount_cents',99900,'credit_grant_mc',5000,
                        'settled_cents',0,'reversed_cents',0,'restored_cents',0,
                        'line_financial_state','pending'))),
                    :operation_id, :revision
                )
            """),
            {
                "id": uuid4(),
                "builder_id": postgres_harness.builder_id,
                "subscription_id": subscription_id,
                "target": f"asaas-{subscription_id}",
                "operation_id": uuid4(),
                "revision": revision,
            },
        )
        return subscription_id, external_id

    async with postgres_harness.session() as setup:
        async with setup.begin():
            await setup.execute(text("""
                UPDATE billing_roster_control SET billing_roster_revision = 1
                WHERE singleton = true AND billing_roster_revision = 0
            """))
            revision = await setup.scalar(
                text("SELECT billing_roster_revision FROM billing_roster_control")
            )
            assert revision is not None
            first_id, first_external = await seed_legacy(setup, revision)

    receiver = postgres_harness.session()
    writer = postgres_harness.session()
    await receiver.begin()
    await writer.begin()
    try:
        assert await billing_route._get_abacatepay_roster_binding(
            first_external, receiver
        )

        async def cancel_first() -> None:
            await writer.execute(
                text("""
                    UPDATE billing_subscriptions
                    SET provider = NULL, is_active = false,
                        status = 'legacy_cancel_pending',
                        roster_revision = :revision
                    WHERE id = :id
                """),
                {"id": first_id, "revision": revision + 1},
            )

        contender = asyncio.create_task(cancel_first())
        done, _ = await asyncio.wait({contender}, timeout=0.15)
        assert not done
        await receiver.commit()
        await contender
        await writer.commit()
    finally:
        if receiver.in_transaction():
            await receiver.rollback()
        if writer.in_transaction():
            await writer.rollback()
        await receiver.close()
        await writer.close()

    async with postgres_harness.session() as setup:
        async with setup.begin():
            second_id, second_external = await seed_legacy(setup, revision + 1)

    writer = postgres_harness.session()
    receiver = postgres_harness.session()
    await writer.begin()
    await receiver.begin()
    try:
        await writer.execute(
            text("""
                UPDATE billing_subscriptions
                SET provider = NULL, is_active = false,
                    status = 'legacy_cancel_pending', roster_revision = :revision
                WHERE id = :id
            """),
            {"id": second_id, "revision": revision + 2},
        )

        async def receive_after_writer() -> object:
            return await billing_route._get_abacatepay_roster_binding(
                second_external, receiver
            )

        contender = asyncio.create_task(receive_after_writer())
        done, _ = await asyncio.wait({contender}, timeout=0.15)
        assert not done
        await writer.commit()
        assert await contender is None
        await receiver.commit()
    finally:
        if writer.in_transaction():
            await writer.rollback()
        if receiver.in_transaction():
            await receiver.rollback()
        await writer.close()
        await receiver.close()


async def test_legacy_provider_flip_requires_exact_approved_roster_target(
    postgres_harness: _Harness,
) -> None:
    subscription_id = uuid4()
    migration_id = uuid4()
    operation_id = uuid4()
    target_external_id = f"asaas-{subscription_id}"
    async with postgres_harness.session() as session:
        async with session.begin():
            await session.execute(text("""
                UPDATE billing_roster_control
                SET billing_roster_revision = 1, updated_at = now()
                WHERE singleton = true AND billing_roster_revision = 0
            """))
            current_revision = (await session.execute(text("""
                        SELECT billing_roster_revision
                        FROM billing_roster_control
                        WHERE singleton = true
                        FOR UPDATE
                    """))).scalar_one()
            with pytest.raises(DBAPIError, match="roster revision is saga-owned"):
                async with session.begin_nested():
                    await session.execute(text("""
                        UPDATE billing_roster_control
                        SET billing_roster_revision = billing_roster_revision + 1
                        WHERE singleton = true
                    """))
            for provider, status, is_active in (
                ("abacatepay", "active", True),
                (None, "provider_unknown", False),
            ):
                guarded_id = uuid4()
                await session.execute(
                    text("""
                        INSERT INTO billing_subscriptions (
                            id, builder_id, provider, external_subscription_id,
                            is_active, plan, billing_cycle, status, roster_revision
                        ) VALUES (
                            :id, :builder_id, :provider, :external_id,
                            :is_active, 'start', 'monthly', :status, :revision
                        )
                    """),
                    {
                        "id": guarded_id,
                        "builder_id": postgres_harness.builder_id,
                        "provider": provider,
                        "external_id": f"guarded-{guarded_id}",
                        "is_active": is_active,
                        "status": status,
                        "revision": current_revision,
                    },
                )
                with pytest.raises(DBAPIError):
                    async with session.begin_nested():
                        await session.execute(
                            text("""
                                UPDATE billing_subscriptions
                                SET provider = CASE
                                        WHEN provider IS NULL THEN 'asaas'
                                        ELSE provider
                                    END,
                                    plan = 'forbidden-mutation'
                                WHERE id = :id
                            """),
                            {"id": guarded_id},
                        )
                if provider is None:
                    with pytest.raises(DBAPIError):
                        async with session.begin_nested():
                            await session.execute(
                                text("""UPDATE billing_subscriptions
                                         SET status = 'legacy_cancelled'
                                         WHERE id = :id"""),
                                {"id": guarded_id},
                            )
            await session.execute(
                text("""
                    INSERT INTO billing_subscriptions (
                        id, builder_id, provider, external_subscription_id,
                        is_active, plan, billing_cycle, status, roster_revision
                    ) VALUES (
                        :id, :builder_id, NULL, :external_id,
                        false, 'start', 'monthly', 'legacy_cancel_pending', :revision
                    )
                """),
                {
                    "id": subscription_id,
                    "builder_id": postgres_harness.builder_id,
                    "external_id": f"legacy-{subscription_id}",
                    "revision": current_revision,
                },
            )
            await session.execute(
                text("""
                    INSERT INTO billing_migrations (
                        id, builder_id, subscription_id, state, renewal_date,
                        open_by_date, cutover_by_date, target_external_subscription_id,
                        target_catalog_version_id, target_offer_id, target_plan,
                        target_billing_cycle, target_amount_cents, target_snapshot,
                        operation_id, source_roster_revision,
                        legacy_cancelled_at
                    ) VALUES (
                        :id, :builder_id, :subscription_id, 'legacy_cancel_pending',
                        DATE '2026-12-10', DATE '2026-12-03', DATE '2026-12-08',
                        :target_external_id, 'payment-2026-10-03',
                        'plan_plus_monthly_v1', 'plus', 'monthly', 99900,
                        jsonb_build_object(
                          'catalog_version_id','payment-2026-10-03',
                          'offer_id','plan_plus_monthly_v1','plan','plus',
                          'billing_cycle','monthly','coupon_code',NULL,
                          'amount_cents',99900,'credit_grant_mc',5000,
                          'included_clinics',1,'additional_clinics',0,
                          'lines',jsonb_build_array(jsonb_build_object(
                            'line_id','plan','offer_id','plan_plus_monthly_v1',
                            'kind','plan','quantity',1,
                            'unit_amount_cents',99900,
                            'line_amount_cents',99900,'credit_grant_mc',5000,
                            'settled_cents',0,'reversed_cents',0,
                            'restored_cents',0,
                            'line_financial_state','pending'))),
                        :operation_id, :revision, NULL
                    )
                """),
                {
                    "id": migration_id,
                    "builder_id": postgres_harness.builder_id,
                    "subscription_id": subscription_id,
                    "target_external_id": target_external_id,
                    "operation_id": operation_id,
                    "revision": current_revision,
                },
            )
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        text("""UPDATE billing_migrations
                                 SET state = 'legacy_cancelled'
                                 WHERE id = :id"""),
                        {"id": migration_id},
                    )
            observed_at = datetime.now(timezone.utc)
            await session.execute(
                text("""
                    INSERT INTO billing_migration_receipts (
                        id, builder_id, migration_id, operation_id, receipt_kind,
                        provider, external_reference, status, content_sha256,
                        actor_user_id, observed_at
                    ) SELECT :id, migration.builder_id, migration.id,
                        migration.operation_id, 'cancel_observation',
                        'abacatepay', subscription.external_subscription_id,
                        'cancelled', encode(sha256(convert_to(jsonb_build_object(
                          'migration_id',migration.id,
                          'builder_id',migration.builder_id,
                          'subscription_id',migration.subscription_id,
                          'operation_id',migration.operation_id,
                          'legacy_external_subscription_id',
                            subscription.external_subscription_id,
                          'provider','abacatepay','status','cancelled',
                          'observed_at',CAST(:observed_at AS TIMESTAMPTZ)
                        )::text,'UTF8')),'hex'), :actor,
                        CAST(:observed_at AS TIMESTAMPTZ)
                    FROM billing_migrations migration
                    JOIN billing_subscriptions subscription
                      ON subscription.id = migration.subscription_id
                    WHERE migration.id = :migration_id
                """),
                {
                    "id": uuid4(),
                    "migration_id": migration_id,
                    "actor": uuid4(),
                    "observed_at": observed_at,
                },
            )
            await session.execute(
                text("""UPDATE billing_subscriptions
                         SET status = 'legacy_cancelled' WHERE id = :id"""),
                {"id": subscription_id},
            )
            await session.execute(
                text("""
                    INSERT INTO billing_migration_receipts (
                        id, builder_id, migration_id, operation_id, receipt_kind,
                        provider, external_reference, status, content_sha256,
                        actor_user_id, observed_at
                    ) SELECT :id, migration.builder_id, migration.id,
                        migration.operation_id, 'provider_flip', 'asaas',
                        migration.target_external_subscription_id, 'verified',
                        encode(sha256(convert_to(jsonb_build_object(
                            'migration_id', migration.id,
                            'builder_id', migration.builder_id,
                            'subscription_id', migration.subscription_id,
                            'operation_id', migration.operation_id,
                            'source_roster_revision', migration.source_roster_revision,
                            'target_provider', migration.target_provider,
                            'target_external_subscription_id',
                                migration.target_external_subscription_id,
                            'target_catalog_version_id',
                                migration.target_catalog_version_id,
                            'target_offer_id', migration.target_offer_id,
                            'target_plan', migration.target_plan,
                            'target_billing_cycle', migration.target_billing_cycle,
                            'target_coupon_code', migration.target_coupon_code,
                            'target_amount_cents', migration.target_amount_cents,
                            'target_snapshot', migration.target_snapshot
                        )::text, 'UTF8')), 'hex'), :actor, now()
                    FROM billing_migrations AS migration
                    WHERE migration.id = :migration_id
                """),
                {
                    "id": uuid4(),
                    "migration_id": migration_id,
                    "actor": uuid4(),
                },
            )
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        text("""
                            UPDATE billing_migrations
                            SET target_plan = 'mutable-after-receipt'
                            WHERE id = :migration_id
                        """),
                        {"migration_id": migration_id},
                    )
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        text("""
                            UPDATE billing_subscriptions
                            SET plan = 'mutable-while-null'
                            WHERE id = :subscription_id
                        """),
                        {"subscription_id": subscription_id},
                    )
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await _seed_bound_legacy_flip(
                        session,
                        builder_id=postgres_harness.builder_id,
                        source_revision=current_revision,
                        valid_digest=False,
                    )
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        text("""
                            UPDATE billing_subscriptions
                            SET provider = 'asaas',
                                external_subscription_id = :target_external_id,
                                plan = 'plus', billing_cycle = 'monthly',
                                coupon_code = NULL,
                                catalog_version_id = 'payment-2026-10-03',
                                offer_id = 'plan_plus_monthly_v1',
                                amount_cents = 99900, status = 'inactive',
                                roster_revision = :stale_revision
                            WHERE id = :subscription_id
                        """),
                        {
                            "subscription_id": subscription_id,
                            "target_external_id": target_external_id,
                            "stale_revision": current_revision,
                        },
                    )
            await session.execute(
                text("""
                    UPDATE billing_subscriptions
                    SET provider = 'asaas',
                        external_subscription_id = :target_external_id,
                        plan = 'plus', billing_cycle = 'monthly', coupon_code = NULL,
                        catalog_version_id = 'payment-2026-10-03',
                        offer_id = 'plan_plus_monthly_v1', amount_cents = 99900,
                        status = 'inactive', roster_revision = :revision
                    WHERE id = :subscription_id
                """),
                {
                    "subscription_id": subscription_id,
                    "target_external_id": target_external_id,
                    "revision": current_revision + 1,
                },
            )
            state = (
                await session.execute(
                    text("SELECT state FROM billing_migrations WHERE id = :id"),
                    {"id": migration_id},
                )
            ).scalar_one()
            assert state == "migrated"
            revision = (
                await session.execute(
                    text("SELECT billing_roster_revision FROM billing_roster_control")
                )
            ).scalar_one()
            assert revision == current_revision + 1
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(
                        text("""
                            UPDATE billing_subscriptions SET plan = 'post-flip-mutation'
                            WHERE id = :subscription_id
                        """),
                        {"subscription_id": subscription_id},
                    )


async def test_concurrent_provider_flips_serialize_on_global_roster(
    postgres_harness: _Harness,
) -> None:
    async with postgres_harness.session() as setup:
        async with setup.begin():
            await setup.execute(text("""
                UPDATE billing_roster_control
                SET billing_roster_revision = 1, updated_at = now()
                WHERE singleton = true AND billing_roster_revision = 0
            """))
            current_revision = (
                await setup.execute(
                    text("SELECT billing_roster_revision FROM billing_roster_control")
                )
            ).scalar_one()
            first_id, first_migration_id, first_target = await _seed_bound_legacy_flip(
                setup,
                builder_id=postgres_harness.builder_id,
                source_revision=current_revision,
                include_provider_receipt=False,
            )

    statement = text("""
        UPDATE billing_subscriptions
        SET provider = 'asaas', external_subscription_id = :target_external_id,
            plan = 'plus', billing_cycle = 'monthly', coupon_code = NULL,
            catalog_version_id = 'payment-2026-10-03',
            offer_id = 'plan_plus_monthly_v1', amount_cents = 99900,
            status = 'inactive', roster_revision = :revision
        WHERE id = :subscription_id
    """)
    first = postgres_harness.session()
    second = postgres_harness.session()
    await first.begin()
    await second.begin()
    try:
        await _insert_provider_flip_receipt(first, first_migration_id)
        await first.execute(
            statement,
            {
                "subscription_id": first_id,
                "target_external_id": first_target,
                "revision": current_revision + 1,
            },
        )

        async def flip_second() -> None:
            second_id, _, second_target = await _seed_bound_legacy_flip(
                second,
                builder_id=postgres_harness.builder_id,
                source_revision=current_revision + 1,
            )
            await second.execute(
                statement,
                {
                    "subscription_id": second_id,
                    "target_external_id": second_target,
                    "revision": current_revision + 2,
                },
            )
            await second.commit()

        contender = asyncio.create_task(flip_second())
        done, _ = await asyncio.wait({contender}, timeout=0.15)
        assert not done
        await first.commit()
        await contender
        async with postgres_harness.session() as verify:
            revision = await verify.scalar(
                text("SELECT billing_roster_revision FROM billing_roster_control")
            )
            assert revision == current_revision + 2
    finally:
        if first.in_transaction():
            await first.rollback()
        if second.in_transaction():
            await second.rollback()
        await first.close()
        await second.close()


async def test_provider_flip_rejects_receipt_bound_to_stale_source(
    postgres_harness: _Harness,
) -> None:
    async with postgres_harness.session() as setup:
        async with setup.begin():
            await setup.execute(text("""
                UPDATE billing_roster_control SET billing_roster_revision = 1
                WHERE singleton = true AND billing_roster_revision = 0
            """))
            source_revision = await setup.scalar(
                text("SELECT billing_roster_revision FROM billing_roster_control")
            )
            assert source_revision is not None
            first_id, first_migration_id, first_target = await _seed_bound_legacy_flip(
                setup,
                builder_id=postgres_harness.builder_id,
                source_revision=source_revision,
                include_provider_receipt=False,
            )
            stale_subscription_id, stale_migration_id, stale_target = (
                await _seed_bound_legacy_flip(
                    setup,
                    builder_id=postgres_harness.builder_id,
                    source_revision=source_revision,
                    include_provider_receipt=False,
                )
            )

    async with postgres_harness.session() as session:
        async with session.begin():
            await _insert_provider_flip_receipt(session, first_migration_id)
            await session.execute(
                text("""
                    UPDATE billing_subscriptions
                    SET provider='asaas', external_subscription_id=:target,
                        plan='plus', billing_cycle='monthly',
                        catalog_version_id='payment-2026-10-03',
                        offer_id='plan_plus_monthly_v1', amount_cents=99900,
                        status='inactive', roster_revision=:revision
                    WHERE id=:id
                """),
                {
                    "id": first_id,
                    "target": first_target,
                    "revision": source_revision + 1,
                },
            )

    async with postgres_harness.session() as session:
        with pytest.raises(DBAPIError, match="source revision is stale"):
            async with session.begin():
                await _insert_provider_flip_receipt(session, stale_migration_id)

    async with postgres_harness.session() as session:
        async with session.begin():
            rebased = await BillingRepository(session).rebase_legacy_migration(
                migration_id=stale_migration_id,
                expected_source_revision=source_revision,
            )
            assert rebased == source_revision + 1
            await _insert_provider_flip_receipt(session, stale_migration_id)
            await session.execute(
                text("""
                    UPDATE billing_subscriptions
                    SET provider='asaas', external_subscription_id=:target,
                        plan='plus', billing_cycle='monthly',
                        catalog_version_id='payment-2026-10-03',
                        offer_id='plan_plus_monthly_v1', amount_cents=99900,
                        status='inactive', roster_revision=:revision
                    WHERE id=:id
                """),
                {
                    "id": stale_subscription_id,
                    "target": stale_target,
                    "revision": source_revision + 2,
                },
            )


async def test_malformed_receipts_do_not_poison_slot_and_standalone_commit_fails(
    postgres_harness: _Harness,
) -> None:
    async with postgres_harness.session() as setup:
        async with setup.begin():
            await setup.execute(text("""
                UPDATE billing_roster_control SET billing_roster_revision = 1
                WHERE singleton = true AND billing_roster_revision = 0
            """))
            revision = await setup.scalar(
                text("SELECT billing_roster_revision FROM billing_roster_control")
            )
            assert revision is not None
            subscription_id, migration_id, target = await _seed_bound_legacy_flip(
                setup,
                builder_id=postgres_harness.builder_id,
                source_revision=revision,
                include_provider_receipt=False,
            )

    malformed_insert = text("""
        INSERT INTO billing_migration_receipts (
            id, builder_id, migration_id, operation_id, receipt_kind,
            provider, external_reference, status, content_sha256,
            actor_user_id, observed_at
        ) SELECT :id, builder_id, id,
            CASE WHEN :wrong_operation THEN gen_random_uuid() ELSE operation_id END,
            'provider_flip', :provider,
            CASE WHEN :use_target THEN target_external_subscription_id
                 ELSE :external_reference END,
            :status,
            CASE WHEN :valid_digest THEN encode(sha256(convert_to(jsonb_build_object(
              'migration_id', id, 'builder_id', builder_id,
              'subscription_id', subscription_id, 'operation_id', operation_id,
              'source_roster_revision', source_roster_revision,
              'target_provider', target_provider,
              'target_external_subscription_id', target_external_subscription_id,
              'target_catalog_version_id', target_catalog_version_id,
              'target_offer_id', target_offer_id, 'target_plan', target_plan,
              'target_billing_cycle', target_billing_cycle,
              'target_coupon_code', target_coupon_code,
              'target_amount_cents', target_amount_cents,
              'target_snapshot', target_snapshot)::text,'UTF8')),'hex')
            ELSE repeat('f', 64) END,
            :actor, now()
        FROM billing_migrations WHERE id = :migration_id
    """)
    malformed = (
        {
            "provider": None,
            "use_target": True,
            "external_reference": None,
            "status": "verified",
            "wrong_operation": False,
            "valid_digest": True,
        },
        {
            "provider": "abacatepay",
            "use_target": True,
            "external_reference": None,
            "status": "verified",
            "wrong_operation": False,
            "valid_digest": True,
        },
        {
            "provider": "asaas",
            "use_target": False,
            "external_reference": "wrong",
            "status": "verified",
            "wrong_operation": False,
            "valid_digest": True,
        },
        {
            "provider": "asaas",
            "use_target": True,
            "external_reference": None,
            "status": "failed",
            "wrong_operation": False,
            "valid_digest": True,
        },
        {
            "provider": "asaas",
            "use_target": True,
            "external_reference": None,
            "status": "verified",
            "wrong_operation": True,
            "valid_digest": True,
        },
        {
            "provider": "asaas",
            "use_target": True,
            "external_reference": None,
            "status": "verified",
            "wrong_operation": False,
            "valid_digest": False,
        },
    )
    async with postgres_harness.session() as session:
        async with session.begin():
            for values in malformed:
                with pytest.raises(DBAPIError):
                    async with session.begin_nested():
                        await session.execute(
                            malformed_insert,
                            {
                                "id": uuid4(),
                                "migration_id": migration_id,
                                "actor": uuid4(),
                                **values,
                            },
                        )
            count = await session.scalar(
                text("""
                    SELECT count(*) FROM billing_migration_receipts
                    WHERE migration_id = :migration_id
                      AND receipt_kind = 'provider_flip'
                """),
                {"migration_id": migration_id},
            )
            assert count == 0

    async with postgres_harness.session() as session:
        with pytest.raises(
            DBAPIError, match="provider flip receipt requires migrated saga at commit"
        ):
            async with session.begin():
                await _insert_provider_flip_receipt(session, migration_id)

    async with postgres_harness.session() as session:
        async with session.begin():
            await _insert_provider_flip_receipt(session, migration_id)
            await session.execute(
                text("""
                    UPDATE billing_subscriptions
                    SET provider='asaas', external_subscription_id=:target,
                        plan='plus', billing_cycle='monthly',
                        catalog_version_id='payment-2026-10-03',
                        offer_id='plan_plus_monthly_v1', amount_cents=99900,
                        status='inactive', roster_revision=:revision
                    WHERE id=:id
                """),
                {"id": subscription_id, "target": target, "revision": revision + 1},
            )


async def test_provider_receipt_and_target_updates_are_serialized(
    postgres_harness: _Harness,
) -> None:
    digest_query = text("""
        SELECT encode(sha256(convert_to(jsonb_build_object(
          'migration_id', id, 'builder_id', builder_id,
          'subscription_id', subscription_id, 'operation_id', operation_id,
          'source_roster_revision', source_roster_revision,
          'target_provider', target_provider,
          'target_external_subscription_id', target_external_subscription_id,
          'target_catalog_version_id', target_catalog_version_id,
          'target_offer_id', target_offer_id, 'target_plan', target_plan,
          'target_billing_cycle', target_billing_cycle,
          'target_coupon_code', target_coupon_code,
          'target_amount_cents', target_amount_cents,
          'target_snapshot', target_snapshot)::text,'UTF8')),'hex')
        FROM billing_migrations WHERE id = :migration_id
    """)
    insert_receipt = text("""
        INSERT INTO billing_migration_receipts (
            id, builder_id, migration_id, operation_id, receipt_kind,
            provider, external_reference, status, content_sha256,
            actor_user_id, observed_at
        ) SELECT :id, builder_id, id, operation_id, 'provider_flip',
            'asaas', target_external_subscription_id, 'verified', :digest,
            :actor, now()
        FROM billing_migrations WHERE id = :migration_id
    """)

    async with postgres_harness.session() as setup:
        async with setup.begin():
            await setup.execute(text("""
                UPDATE billing_roster_control SET billing_roster_revision = 1
                WHERE singleton = true AND billing_roster_revision = 0
            """))
            revision = await setup.scalar(
                text("SELECT billing_roster_revision FROM billing_roster_control")
            )
            assert revision is not None
            _, stale_migration_id, _ = await _seed_bound_legacy_flip(
                setup,
                builder_id=postgres_harness.builder_id,
                source_revision=revision,
                include_provider_receipt=False,
            )
            stale_digest = await setup.scalar(
                digest_query, {"migration_id": stale_migration_id}
            )

    updater = postgres_harness.session()
    receiver = postgres_harness.session()
    await updater.begin()
    await receiver.begin()
    try:
        await updater.execute(
            text("""
                UPDATE billing_migrations
                SET target_plan = 'premium',
                    target_snapshot = jsonb_set(
                        target_snapshot, '{plan}', '"premium"'::jsonb)
                WHERE id = :migration_id
            """),
            {"migration_id": stale_migration_id},
        )

        async def insert_stale_receipt() -> None:
            await receiver.execute(
                insert_receipt,
                {
                    "id": uuid4(),
                    "migration_id": stale_migration_id,
                    "digest": stale_digest,
                    "actor": uuid4(),
                },
            )

        contender = asyncio.create_task(insert_stale_receipt())
        done, _ = await asyncio.wait({contender}, timeout=0.15)
        assert not done
        await updater.commit()
        with pytest.raises(DBAPIError):
            await contender
        await receiver.rollback()
    finally:
        if updater.in_transaction():
            await updater.rollback()
        if receiver.in_transaction():
            await receiver.rollback()
        await updater.close()
        await receiver.close()

    async with postgres_harness.session() as setup:
        async with setup.begin():
            bound_subscription_id, bound_migration_id, bound_target = (
                await _seed_bound_legacy_flip(
                    setup,
                    builder_id=postgres_harness.builder_id,
                    source_revision=revision,
                    include_provider_receipt=False,
                )
            )
            bound_digest = await setup.scalar(
                digest_query, {"migration_id": bound_migration_id}
            )

    receipt_owner = postgres_harness.session()
    target_writer = postgres_harness.session()
    await receipt_owner.begin()
    await target_writer.begin()
    try:
        await receipt_owner.execute(
            insert_receipt,
            {
                "id": uuid4(),
                "migration_id": bound_migration_id,
                "digest": bound_digest,
                "actor": uuid4(),
            },
        )
        await receipt_owner.execute(
            text("""
                UPDATE billing_subscriptions
                SET provider = 'asaas', external_subscription_id = :target,
                    plan = 'plus', billing_cycle = 'monthly', coupon_code = NULL,
                    catalog_version_id = 'payment-2026-10-03',
                    offer_id = 'plan_plus_monthly_v1', amount_cents = 99900,
                    status = 'inactive', roster_revision = :revision
                WHERE id = :subscription_id
            """),
            {
                "subscription_id": bound_subscription_id,
                "target": bound_target,
                "revision": revision + 1,
            },
        )

        async def mutate_bound_target() -> None:
            await target_writer.execute(
                text("""
                    UPDATE billing_migrations
                    SET target_plan = 'premium',
                        target_snapshot = jsonb_set(
                            target_snapshot, '{plan}', '"premium"'::jsonb)
                    WHERE id = :migration_id
                """),
                {"migration_id": bound_migration_id},
            )

        contender = asyncio.create_task(mutate_bound_target())
        done, _ = await asyncio.wait({contender}, timeout=0.15)
        assert not done
        await receipt_owner.commit()
        with pytest.raises(
            DBAPIError, match="receipt-bound migration target is immutable"
        ):
            await contender
    finally:
        if receipt_owner.in_transaction():
            await receipt_owner.rollback()
        if target_writer.in_transaction():
            await target_writer.rollback()
        await receipt_owner.close()
        await target_writer.close()


_GLOBAL_ROSTER_FENCE_KEY = 3486705124568449331
_DEADLOCK_DETECTED_SQLSTATE = "40P01"


def _sqlstate(error: DBAPIError) -> str | None:
    for candidate in (error.orig, getattr(error.orig, "__cause__", None)):
        state = getattr(candidate, "sqlstate", None) or getattr(
            candidate, "pgcode", None
        )
        if state:
            return str(state)
    return None


async def _wait_for_global_fence_waiters(
    engine: AsyncEngine,
    holder_pid: int,
    waiter_pids: set[int],
    *,
    timeout: float = 15.0,
) -> None:
    """Return once every waiter is observably queued on the global roster fence."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    fence = {
        "high": _GLOBAL_ROSTER_FENCE_KEY >> 32,
        "low": _GLOBAL_ROSTER_FENCE_KEY & 0xFFFFFFFF,
        "holder": holder_pid,
        "waiters": sorted(waiter_pids),
    }
    blocked: set[int] = set()
    holder_granted = False
    async with engine.connect() as monitor:
        while True:
            rows = (
                await monitor.execute(
                    text("""
                        SELECT lock.pid, lock.granted
                        FROM pg_locks AS lock
                        LEFT JOIN pg_stat_activity AS activity
                          ON activity.pid = lock.pid
                        WHERE lock.locktype = 'advisory'
                          AND lock.classid::bigint = :high
                          AND lock.objid::bigint = :low
                          AND lock.objsubid = 1
                          AND (
                            (lock.pid = :holder AND lock.granted)
                            OR (
                              lock.pid = ANY(:waiters)
                              AND NOT lock.granted
                              AND activity.wait_event_type = 'Lock'
                              AND activity.wait_event = 'advisory'
                            )
                          )
                    """),
                    fence,
                )
            ).all()
            await monitor.rollback()
            holder_granted = any(pid == holder_pid for pid, _ in rows)
            blocked = {pid for pid, granted in rows if not granted}
            if holder_granted and blocked == waiter_pids:
                return
            if loop.time() >= deadline:
                observed = (
                    await monitor.execute(
                        text("""
                            SELECT activity.pid, activity.state,
                                   activity.wait_event_type, activity.wait_event,
                                   lock.locktype, lock.classid, lock.objid,
                                   lock.granted
                            FROM pg_stat_activity AS activity
                            LEFT JOIN pg_locks AS lock
                              ON lock.pid = activity.pid
                             AND lock.locktype = 'advisory'
                            WHERE activity.pid = ANY(:pids)
                        """),
                        {"pids": [holder_pid, *sorted(waiter_pids)]},
                    )
                ).all()
                raise AssertionError(
                    "global roster fence waiters were not observed within "
                    f"{timeout}s: holder_granted={holder_granted}, "
                    f"blocked={sorted(blocked)}, expected={sorted(waiter_pids)}, "
                    f"observed={[tuple(row) for row in observed]}"
                )
            await asyncio.sleep(0.02)


async def test_three_transaction_target_receipt_flip_order_has_no_deadlock(
    postgres_harness: _Harness,
) -> None:
    async with postgres_harness.session() as setup:
        async with setup.begin():
            await setup.execute(text("""
                UPDATE billing_roster_control SET billing_roster_revision = 1
                WHERE singleton = true AND billing_roster_revision = 0
            """))
            revision = await setup.scalar(
                text("SELECT billing_roster_revision FROM billing_roster_control")
            )
            assert revision is not None
            flip_first_subscription_id, flip_first_migration_id, flip_first_target = (
                await _seed_bound_legacy_flip(
                    setup,
                    builder_id=postgres_harness.builder_id,
                    source_revision=revision,
                    include_provider_receipt=False,
                )
            )
            subscription_id, migration_id, target = await _seed_bound_legacy_flip(
                setup,
                builder_id=postgres_harness.builder_id,
                source_revision=revision,
                include_provider_receipt=False,
            )

    update_target = text("""
        UPDATE billing_migrations
        SET target_plan = 'premium',
            target_snapshot = jsonb_set(
                target_snapshot, '{plan}', '"premium"'::jsonb)
        WHERE id = :migration_id
    """)
    flip = text("""
        UPDATE billing_subscriptions
        SET provider='asaas', external_subscription_id=:target,
            plan='premium', billing_cycle='monthly',
            catalog_version_id='payment-2026-10-03',
            offer_id='plan_plus_monthly_v1', amount_cents=99900,
            status='inactive', roster_revision=:revision
        WHERE id=:id
    """)
    expected_failures = {
        "receipt": "invalid provider flip receipt",
        "flip": "ASAAS provider flip requires exact bound target",
    }

    def assert_fail_closed(name: str, task: asyncio.Task[Any]) -> None:
        assert not task.cancelled(), f"{name} waiter was cancelled"
        error = task.exception()
        assert error is not None, f"{name} waiter must fail closed but succeeded"
        assert isinstance(error, DBAPIError), f"{name} waiter raised {error!r}"
        sqlstate = _sqlstate(error)
        assert sqlstate is not None, f"{name} waiter error has no SQLSTATE"
        assert (
            sqlstate != _DEADLOCK_DETECTED_SQLSTATE
        ), f"{name} waiter hit deadlock_detected: {error}"
        assert expected_failures[name] in str(error), f"{name} waiter: {error}"

    async def race(
        first: str,
        *,
        race_subscription_id: UUID,
        race_migration_id: UUID,
        race_target: str,
    ) -> None:
        """Queue both sibling waiters behind the target writer in a fixed order.

        PostgreSQL grants the exclusive advisory fence to queued waiters in
        arrival order, so ``first`` selects which waiter resumes first after
        the target writer commits. The assertions do not depend on that
        order: whichever waiter finishes first, both must fail closed with
        their own integrity error and neither may report deadlock_detected.
        """

        target_writer = postgres_harness.session()
        receipt_writer = postgres_harness.session()
        flip_writer = postgres_harness.session()
        sessions = {"receipt": receipt_writer, "flip": flip_writer}
        tasks: dict[str, asyncio.Task[Any]] = {}
        await target_writer.begin()
        await receipt_writer.begin()
        await flip_writer.begin()
        try:
            pid_query = text("SELECT pg_backend_pid()")
            target_pid = await target_writer.scalar(pid_query)
            pids = {
                "receipt": await receipt_writer.scalar(pid_query),
                "flip": await flip_writer.scalar(pid_query),
            }
            starters = {
                "receipt": lambda: _insert_provider_flip_receipt(
                    receipt_writer, race_migration_id
                ),
                "flip": lambda: flip_writer.execute(
                    flip,
                    {
                        "id": race_subscription_id,
                        "target": race_target,
                        "revision": revision + 1,
                    },
                ),
            }
            await target_writer.execute(
                update_target, {"migration_id": race_migration_id}
            )
            queue_order = (first, *(name for name in starters if name != first))
            for name in queue_order:
                tasks[name] = asyncio.create_task(starters[name]())
                # Start the next waiter only once this one is observably
                # queued on the global fence held by the target writer.
                await _wait_for_global_fence_waiters(
                    postgres_harness.engine,
                    target_pid,
                    {pids[started] for started in tasks},
                )
            assert not any(task.done() for task in tasks.values())

            await target_writer.commit()
            done, _ = await asyncio.wait(
                tasks.values(), timeout=15, return_when=asyncio.FIRST_COMPLETED
            )
            assert done, "no sibling waiter resumed after the target writer commit"
            # PostgreSQL releases an aborted transaction's locks at the error,
            # so the second waiter may already have finished too. Either
            # completion order is valid; every finished waiter must fail closed.
            for name in queue_order:
                if tasks[name] in done:
                    assert_fail_closed(name, tasks[name])
                    await sessions[name].rollback()
            for name in queue_order:
                if tasks[name] not in done:
                    finished, _ = await asyncio.wait({tasks[name]}, timeout=15)
                    assert finished, f"{name} waiter never resumed"
                    assert_fail_closed(name, tasks[name])
                    await sessions[name].rollback()
        finally:
            for task in tasks.values():
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks.values(), return_exceptions=True)
            for session in (target_writer, receipt_writer, flip_writer):
                if session.in_transaction():
                    await session.rollback()
                await session.close()

        async with postgres_harness.session() as verify_race:
            leftover = (
                await verify_race.execute(
                    text("""
                        SELECT subscription.provider, migration.state,
                               (SELECT count(*) FROM billing_migration_receipts r
                                WHERE r.migration_id = migration.id
                                  AND r.receipt_kind = 'provider_flip')
                        FROM billing_migrations migration
                        JOIN billing_subscriptions subscription
                          ON subscription.id = migration.subscription_id
                        WHERE migration.id = :migration_id
                    """),
                    {"migration_id": race_migration_id},
                )
            ).one()
            assert tuple(leftover) == (None, "legacy_cancelled", 0)

    # Flip waiter queued first, then receipt waiter queued first.
    await race(
        "flip",
        race_subscription_id=flip_first_subscription_id,
        race_migration_id=flip_first_migration_id,
        race_target=flip_first_target,
    )
    await race(
        "receipt",
        race_subscription_id=subscription_id,
        race_migration_id=migration_id,
        race_target=target,
    )

    async with postgres_harness.session() as recovery:
        async with recovery.begin():
            await _insert_provider_flip_receipt(recovery, migration_id)
            await recovery.execute(
                flip,
                {"id": subscription_id, "target": target, "revision": revision + 1},
            )
    async with postgres_harness.session() as verify:
        row = (
            await verify.execute(
                text("""
                    SELECT migration.state, subscription.provider,
                           subscription.plan, subscription.roster_revision,
                           roster.billing_roster_revision
                    FROM billing_migrations migration
                    JOIN billing_subscriptions subscription
                      ON subscription.id = migration.subscription_id
                    CROSS JOIN billing_roster_control roster
                    WHERE migration.id = :migration_id
                """),
                {"migration_id": migration_id},
            )
        ).one()
        assert tuple(row) == (
            "migrated",
            "asaas",
            "premium",
            revision + 1,
            revision + 1,
        )


async def test_concurrent_uniques_cover_intent_delivery_projection_effect_receipt_outbox(
    postgres_harness: _Harness,
) -> None:
    builder_id = postgres_harness.builder_id
    subscription_id = uuid4()
    migration_id = uuid4()
    topup_order_id = uuid4()
    projection_id = uuid4()
    projection_payment_id = f"pay-{projection_id}"
    delivery_id = uuid4()
    async with postgres_harness.engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO billing_subscriptions (
                    id, builder_id, provider, external_subscription_id,
                    is_active, plan, billing_cycle, status, roster_revision
                ) VALUES (
                    :id, :builder_id, 'abacatepay', :external_id,
                    true, 'start', 'monthly', 'active', 1
                )
            """),
            {
                "id": subscription_id,
                "builder_id": builder_id,
                "external_id": f"sub-{subscription_id}",
            },
        )
        await connection.execute(
            text("""
                INSERT INTO billing_migrations (
                    id, builder_id, subscription_id, renewal_date,
                    open_by_date, cutover_by_date, target_catalog_version_id,
                    target_offer_id, target_plan, target_amount_cents,
                    target_snapshot, source_roster_revision
                ) VALUES (
                    :id, :builder_id, :subscription_id, DATE '2026-12-10',
                    DATE '2026-12-03', DATE '2026-12-08', 'catalog-v1',
                    'plan_plus_monthly_v1', 'plus', 99900,
                    CAST(:target_snapshot AS JSONB), 1
                )
            """),
            {
                "id": migration_id,
                "builder_id": builder_id,
                "subscription_id": subscription_id,
                "target_snapshot": _MIGRATION_SNAPSHOT_JSON,
            },
        )
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

    intent_id = migration_id
    session_statement = """
        INSERT INTO billing_checkout_sessions (
            id, builder_id, intent_kind, intent_id, migration_id, attempt_no,
            catalog_version_id, offer_id, amount_cents, pricing_snapshot
        ) VALUES (
            :id, :builder_id, 'subscription', :intent_id, :migration_id,
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
            "migration_id": migration_id,
            "attempt_no": 1,
        },
        {
            "id": uuid4(),
            "builder_id": builder_id,
            "intent_id": intent_id,
            "migration_id": migration_id,
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
            INSERT INTO billing_migration_receipts (
                id, builder_id, migration_id, operation_id, receipt_kind,
                provider, external_reference, status, content_sha256,
                actor_user_id, observed_at
            ) VALUES (
                :id, :builder_id, :migration_id, :natural_id,
                'roster_import', 'abacatepay', 'opaque-ref', 'observed',
                :sha, :actor, now()
            )
            """,
            {
                "id": uuid4(),
                "builder_id": builder_id,
                "migration_id": migration_id,
                "natural_id": uuid4(),
                "sha": "c" * 64,
                "actor": uuid4(),
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


async def test_rollout_and_roster_locks_serialize_stale_writers(
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

    async with postgres_harness.session() as setup:
        async with setup.begin():
            await setup.execute(text("""
                UPDATE billing_roster_control SET billing_roster_revision = 1
                WHERE singleton = true AND billing_roster_revision = 0
            """))
            current_roster_revision = await setup.scalar(
                text("SELECT billing_roster_revision FROM billing_roster_control")
            )
            assert current_roster_revision is not None
            _, migration_id, _ = await _seed_bound_legacy_flip(
                setup,
                builder_id=postgres_harness.builder_id,
                source_revision=current_roster_revision,
                include_provider_receipt=False,
            )
            advance_id, advance_migration_id, advance_target = (
                await _seed_bound_legacy_flip(
                    setup,
                    builder_id=postgres_harness.builder_id,
                    source_revision=current_roster_revision,
                    include_provider_receipt=False,
                )
            )
            await _insert_provider_flip_receipt(setup, advance_migration_id)
            await setup.execute(
                text("""
                    UPDATE billing_subscriptions
                    SET provider='asaas', external_subscription_id=:target,
                        plan='plus', billing_cycle='monthly',
                        catalog_version_id='payment-2026-10-03',
                        offer_id='plan_plus_monthly_v1', amount_cents=99900,
                        status='inactive', roster_revision=:revision
                    WHERE id=:id
                """),
                {
                    "id": advance_id,
                    "target": advance_target,
                    "revision": current_roster_revision + 1,
                },
            )

    roster_one = postgres_harness.session()
    roster_two = postgres_harness.session()
    await roster_one.begin()
    await roster_two.begin()
    try:
        revision = await BillingRepository(roster_one).rebase_legacy_migration(
            migration_id=migration_id,
            expected_source_revision=current_roster_revision,
        )
        assert revision == current_roster_revision + 1
        contender = asyncio.create_task(
            BillingRepository(roster_two).rebase_legacy_migration(
                migration_id=migration_id,
                expected_source_revision=current_roster_revision,
            )
        )
        done, _ = await asyncio.wait({contender}, timeout=0.15)
        assert not done
        await roster_one.commit()
        with pytest.raises(BillingRevisionConflict):
            await contender
        await roster_two.rollback()
    finally:
        if roster_one.in_transaction():
            await roster_one.rollback()
        if roster_two.in_transaction():
            await roster_two.rollback()
        await roster_one.close()
        await roster_two.close()


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
                            'package_credits_500_v1', 10000, 500,
                            CAST(:snapshot AS jsonb)
                        )
                    """),
                    {
                        "id": order_id,
                        "builder_id": postgres_harness.builder_id,
                        "snapshot": (
                            '{"catalog_version_id":"payment-2026-10-03",'
                            '"offer_id":"package_credits_500_v1",'
                            '"amount_cents":10000,"credit_grant_mc":500,'
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
                        offer_id, amount_cents, status, roster_revision
                    ) VALUES (
                        :id, :builder_id, 'asaas', :external_id, true, 'plus',
                        'monthly', 'payment-2026-10-03',
                        'plan_plus_monthly_v1', 99900, 'active', 1
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
                        offer_id, amount_cents, status, roster_revision
                    ) VALUES (
                        :id, :builder_id, 'asaas', :external_id, true, 'plus',
                        'monthly', 'payment-2026-10-03',
                        'plan_plus_monthly_v1', 99900, 'active', 1
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
                        credit_deployment_digest, billing_fact_schema_digest,
                        producer_compatibility_digest,
                        consumer_compatibility_digest, inbox_high_watermark,
                        reconciliation_receipt_id, approved_by, approved_at,
                        expires_at, reconciliation_complete, readback_complete,
                        has_open_gap, has_backlog, has_alert
                    ) VALUES (
                        :id, 'production', 'credit_capability_ready.v1', :request_id,
                        'credit-image-sha256', :digest, :digest, :digest, 0,
                        :reconciliation_id, :approved_by, :approved_at,
                        :expires_at, true, true, false, false, false
                    )
                """),
                {
                    "id": capability_id,
                    "request_id": uuid4(),
                    "digest": "e" * 64,
                    "reconciliation_id": uuid4(),
                    "approved_by": uuid4(),
                    "approved_at": now,
                    "expires_at": now + timedelta(hours=2),
                },
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
                            'package_credits_500_v1', 10000, 500,
                            CAST(:snapshot AS jsonb)
                        )
                    """),
                    {
                        "id": order_id,
                        "builder_id": postgres_harness.builder_id,
                        "snapshot": (
                            '{"catalog_version_id":"payment-2026-10-03",'
                            '"offer_id":"package_credits_500_v1",'
                            '"amount_cents":10000,"credit_grant_mc":500,'
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
                    is_active, plan, billing_cycle, status, roster_revision
                ) VALUES (
                    :id, :builder_id, 'abacatepay', :external_id,
                    true, 'start', 'monthly', 'active', 1
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
                    is_active, plan, billing_cycle, status, roster_revision
                ) VALUES (
                    :id, :builder_id, 'asaas', :external_id,
                    true, 'plus', 'monthly', 'active', 1
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
            revision = await session.scalar(
                text("SELECT billing_roster_revision FROM billing_roster_control")
            )
            await session.execute(
                text("""
                    INSERT INTO billing_subscriptions (
                        id, builder_id, provider, external_subscription_id,
                        is_active, plan, billing_cycle, status, roster_revision
                    ) VALUES (
                        :id, :builder_id, NULL, :external_id,
                        false, 'start', 'monthly', 'provider_unknown', :revision
                    )
                """),
                {
                    "id": subscription_id,
                    "builder_id": postgres_harness.builder_id,
                    "external_id": f"snapshot-{subscription_id}",
                    "revision": revision,
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
                    CAST(:snapshot AS jsonb), :revision
                )
            """)
            for snapshot in invalid_snapshots:
                with pytest.raises(DBAPIError):
                    async with session.begin_nested():
                        await session.execute(
                            statement,
                            {
                                "id": uuid4(),
                                "builder_id": postgres_harness.builder_id,
                                "subscription_id": subscription_id,
                                "snapshot": json.dumps(snapshot),
                                "revision": revision,
                            },
                        )


async def test_receipts_and_outbox_reject_update_and_delete(
    postgres_harness: _Harness,
) -> None:
    subscription_id = uuid4()
    migration_id = uuid4()
    receipt_id = uuid4()
    outbox_id = uuid4()
    async with postgres_harness.engine.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO billing_subscriptions (
                    id, builder_id, provider, external_subscription_id,
                    is_active, plan, billing_cycle, status, roster_revision
                ) VALUES (
                    :id, :builder_id, 'abacatepay', :external_id,
                    true, 'start', 'monthly', 'active', 1
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
                INSERT INTO billing_migrations (
                    id, builder_id, subscription_id, renewal_date,
                    open_by_date, cutover_by_date, target_catalog_version_id,
                    target_offer_id, target_plan, target_amount_cents,
                    target_snapshot, source_roster_revision
                ) VALUES (
                    :id, :builder_id, :subscription_id, DATE '2026-12-10',
                    DATE '2026-12-03', DATE '2026-12-08', 'catalog-v1',
                    'plan_plus_monthly_v1', 'plus', 99900,
                    CAST(:target_snapshot AS JSONB), 1
                )
            """),
            {
                "id": migration_id,
                "builder_id": postgres_harness.builder_id,
                "subscription_id": subscription_id,
                "target_snapshot": _MIGRATION_SNAPSHOT_JSON,
            },
        )
        await connection.execute(
            text("""
                INSERT INTO billing_migration_receipts (
                    id, builder_id, migration_id, operation_id, receipt_kind,
                    status, content_sha256, actor_user_id, observed_at
                ) VALUES (
                    :id, :builder_id, :migration_id, :operation_id,
                    'recreate_authorized', 'verified', :sha, :actor, now()
                )
            """),
            {
                "id": receipt_id,
                "builder_id": postgres_harness.builder_id,
                "migration_id": migration_id,
                "operation_id": uuid4(),
                "sha": "f" * 64,
                "actor": uuid4(),
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
            "UPDATE billing_migration_receipts SET status = 'failed' WHERE id = :id",
            receipt_id,
        ),
        ("DELETE FROM billing_migration_receipts WHERE id = :id", receipt_id),
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
                        offer_id, amount_cents, status, roster_revision
                    ) VALUES (
                        :id, :builder_id, 'asaas', :external_id, true, 'plus',
                        'monthly', 'payment-2026-10-03',
                        'plan_plus_monthly_v1', :amount, 'active', 1
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
                        :approved_at, :expires_at, true, true, false, false,
                        false
                    )
                """),
                {
                    "id": capability_id,
                    "request_id": uuid4(),
                    "digest": "f" * 64,
                    "reconciliation_id": uuid4(),
                    "approved_by": uuid4(),
                    "approved_at": now - timedelta(minutes=1),
                    "expires_at": now + timedelta(hours=2),
                },
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
