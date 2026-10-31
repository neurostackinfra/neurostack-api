"""Credit schema contracts that need no PostgreSQL (neurostack-credit-system).

Migration 038 against the credit models, statically: the revision chain,
the tables and columns on both sides, every named constraint and index, the
additive-only upgrade, the guarded downgrade and its offline SQL order.  The
SQLite metadata (what ``init_database`` builds in tests) creates the new
tables; the thin repositories' pure parts (derived-balance fold, effects
digest) and their SQLite paths behave.  Real-database behaviour (triggers,
locks, foreign keys, the downgrade refusal) is proven in
``tests/integration/test_credit_*_postgres.py``.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import os
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy import CheckConstraint, ForeignKeyConstraint, create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import RelationshipProperty, configure_mappers

import src.models as model_exports
import src.repositories as repository_exports
from src.models.base import Base
from src.models.credit import LEDGER_ENTRY_TYPES
from src.repositories.credit import (
    DEBT_DECREASES,
    DEBT_INCREASES,
    LOT_DECREASES,
    LOT_INCREASES,
    CreditRepository,
    DerivedBalance,
    LedgerMovement,
    fold_ledger,
)
from src.repositories.credit_fact_ack import (
    ACK_SCHEMA_VERSION,
    EMPTY_EFFECTS_DIGEST,
    effects_digest,
)
from src.repositories.database import _patch_jsonb_for_sqlite
from tests.alembic_graph import current_head, migration_revisions

_ROOT = Path(__file__).resolve().parents[2]
_MIGRATION = _ROOT / "alembic" / "versions" / "20261026_000001_038_credit_system.py"
_TABLES = {
    "credit_accounts",
    "credit_capability_readiness",
    "credit_debt_origins",
    "credit_debt_settlements",
    "credit_debts",
    "credit_effects",
    "credit_enforcement_binding",
    "credit_enforcement_state",
    "credit_fact_inbox",
    "credit_ingress_claims",
    "credit_ledger_entries",
    "credit_lots",
    "credit_observational_effects",
    "credit_response_outbox",
    "credit_revision_gaps",
}
_MODELS = {
    "CreditAccount",
    "CreditCapabilityReadiness",
    "CreditDebt",
    "CreditDebtOrigin",
    "CreditDebtSettlement",
    "CreditEffect",
    "CreditEnforcementBinding",
    "CreditEnforcementState",
    "CreditFactInbox",
    "CreditIngressClaim",
    "CreditLedgerEntry",
    "CreditLot",
    "CreditObservationalEffect",
    "CreditResponseOutbox",
    "CreditRevisionGap",
}
# The ASAAS producer set never imports credit code (wish, "Conjunto producer").
_PRODUCER_FILES = (
    *(
        f"src/services/billing_{name}.py"
        for name in (
            "outbox",
            "fact_worker",
            "webhook",
            "checkout",
            "capability",
            "catalog",
            "operations",
            "rollout",
            "read_contract",
            "producer_grants",
            "producer_handoff",
            "signed_envelope",
        )
    ),
    "src/repositories/billing.py",
    "src/repositories/billing_producer.py",
    "src/models/billing.py",
    "src/models/billing_producer.py",
    "src/models/billing_producer_signed.py",
    "src/api/routes/billing.py",
    "src/api/routes/billing_producer.py",
    "src/api/routes/billing_producer_setup.py",
    "src/api/schemas/billing.py",
    "src/ops/asaas_webhook.py",
    "src/services/asaas.py",
    "src/services/asaas_secret_rotation.py",
)


def _load() -> ModuleType:
    import alembic as alembic_package

    if not hasattr(alembic_package, "op"):
        setattr(alembic_package, "op", SimpleNamespace())
    spec = importlib.util.spec_from_file_location("migration_038", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def migration() -> ModuleType:
    return _load()


# Group 4a/4b/5: inventory, object-copy and retention tables (pinned by
# tests/integration/test_credit_orm_ddl_drift_postgres.py and the 039 part of
# tests/integration/test_credit_migration_guards_postgres.py).
_LATER_TABLES = {
    "credit_inventory_versions",
    "credit_allocations",
    "credit_egress_deliveries",
    "credit_object_copies",
    "credit_object_copy_jobs",
    "credit_qdrant_jobs",
    # Group 5, migration 041 (pinned by the same two suites).
    "credit_lifecycle_episodes",
    "credit_lifecycle_transitions",
    "credit_threshold_alerts",
    "credit_operational_role_assignments",
    "credit_operational_authorizations",
    "credit_operational_authorization_receipts",
    "credit_hold_generations",
    "credit_hold_events",
    "credit_purge_fences",
    "credit_retention_tombstones",
    "credit_purge_jobs",
    "credit_purge_results",
    "credit_purge_receipts",
}


def _credit_tables() -> dict[str, sa.Table]:
    """The credit tables of migration 038 (later credit migrations' own
    tables are pinned by their own suites)."""

    tables = {
        name: table
        for name, table in Base.metadata.tables.items()
        if name.startswith("credit_")
    }
    assert _LATER_TABLES <= set(tables)
    return {name: table for name, table in tables.items() if name not in _LATER_TABLES}


def _created_columns(statement: str) -> list[str]:
    """Column names of one ``CREATE TABLE`` of the migration, in order."""

    body = statement[statement.index("(") + 1 : statement.rindex(")")]
    names = []
    for line in body.splitlines():
        match = re.match(
            r"^([a-z][a-z0-9_]*) (UUID|VARCHAR|BIGINT|INTEGER|SMALLINT|BOOLEAN|DATE"
            r"|TIMESTAMPTZ|JSONB)\b",
            line.strip(),
        )
        if match:
            names.append(match.group(1))
    return names


# ---------------------------------------------------------------------------
# Revision chain and the migration's own shape.
# ---------------------------------------------------------------------------


def test_038_revises_037_and_is_the_single_head(migration: ModuleType) -> None:
    assert (migration.revision, migration.down_revision) == ("038", "037")
    # Group 4b: 040 (additional object copies) follows 039; Group 5: 041
    # (lifecycle, legal hold, purge and roles) follows 040 and is the head.
    assert current_head() == "041"
    assert migration_revisions()[_MIGRATION.name] == ("038", "037")
    assert re.fullmatch(r"\d{8}_\d{6}_038_credit_system\.py", _MIGRATION.name)


def test_the_migration_creates_exactly_the_mapped_tables(
    migration: ModuleType,
) -> None:
    assert set(migration.CREATED_TABLES) == set(_credit_tables()) == _TABLES
    assert len(migration.CREATED_TABLES) == len(set(migration.CREATED_TABLES))
    created = {}
    for statement in migration.TABLES:
        match = re.search(r"CREATE TABLE (credit_[a-z_]+) \(", statement)
        if match:
            created[match.group(1)] = _created_columns(statement)
    assert list(created) == list(migration.CREATED_TABLES)
    for name, table in _credit_tables().items():
        assert len(created[name]) == len(set(created[name])), name
        assert sorted(created[name]) == sorted(table.columns.keys()), name


def test_every_named_constraint_and_index_is_in_the_migration() -> None:
    source = _MIGRATION.read_text(encoding="utf-8")
    for table in _credit_tables().values():
        names = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint.name, str)
        } | {index.name for index in table.indexes if isinstance(index.name, str)}
        for name in names:
            assert name in source, (table.name, name)


def test_the_upgrade_only_adds(migration: ModuleType) -> None:
    statements = (*migration.FUNCTIONS, *migration.TABLES, *migration.TRIGGERS)
    for statement in statements:
        sql = " ".join(statement.split())
        assert re.match(r"^CREATE (FUNCTION|TABLE|INDEX|TRIGGER) ", sql), sql[:60]
        assert "ALTER " not in sql and "DROP " not in sql, sql[:60]
        assert "CREATE OR REPLACE" not in sql, sql[:60]
        if sql.startswith("CREATE TRIGGER") or sql.startswith("CREATE INDEX"):
            target = re.search(r" ON ([a-z_]+)", sql)
            assert target and target.group(1).startswith("credit_"), sql[:80]
        assert not sa.text(statement)._bindparams, sql[:60]
    referenced = {
        match
        for statement in migration.TABLES
        for match in re.findall(r"REFERENCES ([a-z_]+)\(", statement)
    }
    # The only existing table referenced; no FK to builders or ASAAS tables.
    assert referenced - set(migration.CREATED_TABLES) == {"tenants"}
    tenant_keys = [
        " ".join(statement.split())
        for statement in migration.TABLES
        if "REFERENCES tenants(id)" in statement
    ]
    assert len(tenant_keys) == 2
    for sql in tenant_keys:
        assert "REFERENCES tenants(id) ON DELETE CASCADE ON UPDATE NO ACTION" in sql
    assert {table for table, _ in migration.TENANT_FOREIGN_KEYS} == {
        "credit_ingress_claims",
        "credit_response_outbox",
    }
    assert migration.LOCK_TIMEOUT == "SET LOCAL lock_timeout = '5s'"


def test_history_is_append_only_by_trigger(migration: ModuleType) -> None:
    triggers = " ".join(" ".join(t.split()) for t in migration.TRIGGERS)
    for table in (
        "credit_ledger_entries",
        "credit_effects",
        "credit_fact_inbox",
        "credit_capability_readiness",
        "credit_observational_effects",
    ):
        assert table in migration.APPEND_ONLY_TABLES
    for table in migration.APPEND_ONLY_TABLES:
        assert (
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON "
            f"{table} FOR EACH ROW EXECUTE FUNCTION reject_credit_history_mutation()"
        ) in triggers
    # Mutable rows (queue, claim, state) and the guarded ones are not frozen.
    assert not {
        "credit_ingress_claims",
        "credit_response_outbox",
        "credit_enforcement_state",
        "credit_enforcement_binding",
        "credit_revision_gaps",
    } & set(migration.APPEND_ONLY_TABLES)
    for table in migration.TENANT_BUILDER_CHECKED_TABLES:
        assert (
            f"ON {table} FOR EACH ROW EXECUTE FUNCTION check_credit_tenant_builder()"
            in triggers
        )
    assert (
        "BEFORE INSERT OR UPDATE OF tenant_id, builder_id ON credit_ingress_claims"
        in triggers
    )


def test_the_downgrade_refuses_any_row_then_drops_exactly_038(
    migration: ModuleType,
) -> None:
    preflight = migration.downgrade_preflight()
    for table in (*migration.CREATED_TABLES, "billing_fact_acks"):
        assert f"EXISTS (SELECT 1 FROM {table})" in preflight, table
    assert f"LOCK TABLE {', '.join(migration.DOWNGRADE_LOCKED)}" in preflight
    assert "IN ACCESS EXCLUSIVE MODE" in preflight
    assert "LOCK TABLE billing_fact_acks IN SHARE MODE" in preflight
    assert migration.DOWNGRADE_LOCKED[0] == "tenants"
    assert "USING ERRCODE = 'object_not_in_prerequisite_state'" in preflight
    assert "'alembic_version is %s, not 038'" in preflight
    assert "downgrade of migration 038 refused (nothing was changed): %" in preflight
    assert not sa.text(preflight)._bindparams
    drops = migration.DOWNGRADE_DROPS
    assert drops == (
        f"DROP TABLE {', '.join(reversed(migration.CREATED_TABLES))}",
        f"DROP FUNCTION {', '.join(reversed(migration.CREATED_FUNCTIONS))}",
    )
    assert not any("CASCADE" in drop for drop in drops)
    created_functions = {
        re.search(r"CREATE FUNCTION ([a-z_]+\(\))", statement).group(1)  # type: ignore[union-attr]
        for statement in migration.FUNCTIONS
    }
    assert created_functions == set(migration.CREATED_FUNCTIONS)


def _offline(*arguments: str) -> str:
    alembic_executable = Path(sys.executable).with_name("alembic")
    assert alembic_executable.is_file()
    environment = os.environ.copy()
    environment["DATABASE_URL"] = (
        "postgresql+asyncpg://offline:offline@localhost/offline"
    )
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [str(alembic_executable), *arguments, "--sql"],
        cwd=_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def test_offline_upgrade_sets_the_lock_timeout_before_any_object() -> None:
    sql = _offline("upgrade", "037:038")
    timeout = sql.index("SET LOCAL lock_timeout = '5s';")
    first = sql.index("CREATE FUNCTION reject_credit_history_mutation()")
    tenant_key = sql.index("REFERENCES tenants(id)")
    stamp = sql.index("UPDATE alembic_version SET version_num='038'")
    assert timeout < first < tenant_key < stamp
    assert "ALTER TABLE" not in sql[timeout:stamp]


def test_offline_downgrade_refuses_before_dropping() -> None:
    sql = _offline("downgrade", "038:037")
    timeout = sql.index("SET LOCAL lock_timeout = '5s';")
    lock = sql.index("LOCK TABLE tenants, credit_accounts")
    refusal = sql.index("downgrade of migration 038 refused (nothing was changed)")
    drop_tables = sql.index("DROP TABLE credit_observational_effects")
    drop_functions = sql.index("DROP FUNCTION guard_credit_enforcement_binding()")
    stamp = sql.index("UPDATE alembic_version SET version_num='037'")
    assert timeout < lock < refusal < drop_tables < drop_functions < stamp


def test_the_legacy_init_script_has_no_credit_object() -> None:
    bootstrap = (_ROOT / "scripts" / "init-db.sql").read_text(encoding="utf-8")
    assert "credit_" not in bootstrap


# ---------------------------------------------------------------------------
# ORM metadata.
# ---------------------------------------------------------------------------


def test_credit_models_are_registered_and_exported() -> None:
    configure_mappers()
    assert _MODELS <= set(model_exports.__all__)
    for name in _MODELS:
        model = getattr(model_exports, name)
        assert model.__tablename__ in _TABLES
    assert {"CreditRepository", "CreditFactAckRepository"} <= set(
        repository_exports.__all__
    )


def test_no_relationship_touches_a_credit_table() -> None:
    for mapper in Base.registry.mappers:
        source = str(mapper.local_table.name)
        for prop in mapper.attrs:
            if isinstance(prop, RelationshipProperty):
                target = str(prop.mapper.local_table.name)
                assert not source.startswith("credit_"), (source, prop.key)
                assert not target.startswith("credit_"), (source, prop.key)


def test_foreign_keys_follow_the_wish_policy() -> None:
    for name, table in _credit_tables().items():
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            target = constraint.referred_table.name
            local = [column.name for column in constraint.columns]
            if target == "tenants":
                assert name in {"credit_ingress_claims", "credit_response_outbox"}
                assert constraint.ondelete == "CASCADE" and local == ["tenant_id"]
            else:
                assert target.startswith("credit_"), (name, target)
                # Inside the credit schema every reference is builder-scoped.
                assert local[0] == "builder_id", (name, constraint.name)
            assert target != "builders", name
    binding = _credit_tables()["credit_enforcement_binding"]
    assert {
        fk.referred_table.name
        for fk in binding.constraints
        if isinstance(fk, ForeignKeyConstraint)
    } == {"credit_effects"}


def test_enumerations_are_the_wish_values() -> None:
    def check(table: str, name: str) -> str:
        constraint = next(
            c
            for c in _credit_tables()[table].constraints
            if isinstance(c, CheckConstraint) and c.name == name
        )
        return str(constraint.sqltext)

    assert "('monthly', 'topup')" in check("credit_lots", "ck_credit_lots_kind")
    assert (
        "('grant', 'debit', 'expiry', 'reversal', 'debt', 'debt_settlement', "
        "'debt_release', 'settlement_release', 'lot_regrant')"
    ) in check("credit_ledger_entries", "ck_credit_ledger_entries_type")
    assert "('events', 'zapi', 'meta', 'uazapi', 'evolution')" in check(
        "credit_ingress_claims", "ck_credit_ingress_claims_source"
    )
    assert "('observational', 'enforced')" in check(
        "credit_ingress_claims", "ck_credit_ingress_claims_mode"
    )
    assert "('trace', 'rag', 'media', 'egress', 'maintenance')" in check(
        "credit_observational_effects", "ck_credit_observational_effects_kind"
    )
    assert "('production', 'simulator', 'unknown')" in check(
        "credit_observational_effects", "ck_credit_observational_effects_origin"
    )
    assert "('active', 'reversed')" in check(
        "credit_enforcement_binding", "ck_credit_enforcement_binding_status"
    )
    assert "('accepted', 'rejected')" in check(
        "credit_fact_inbox", "ck_credit_fact_inbox_outcome"
    )
    assert check(
        "credit_enforcement_state", "ck_credit_enforcement_state_singleton"
    ) == ("id = 1")
    inbox_seq = _credit_tables()["credit_fact_inbox"].columns["inbox_seq"]
    assert inbox_seq.identity is not None and inbox_seq.identity.always is True


def test_sqlite_create_all_builds_the_credit_tables() -> None:
    _patch_jsonb_for_sqlite()
    engine = create_engine("sqlite:///:memory:")
    try:
        Base.metadata.create_all(engine)
        with engine.connect() as connection:
            rows = connection.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type = 'table' "
                "AND name LIKE 'credit_%'"
            ).all()
    finally:
        engine.dispose()
    ddl = dict(rows)
    assert set(ddl) == _TABLES | _LATER_TABLES
    # PostgreSQL-only regex CHECKs stay out of SQLite; portable ones stay in.
    assert "~" not in ddl["credit_fact_inbox"]
    assert "ck_credit_fact_inbox_outcome" in ddl["credit_fact_inbox"]


def test_no_producer_module_imports_credit_code() -> None:
    for relative in _PRODUCER_FILES:
        path = _ROOT / relative
        assert path.is_file(), relative
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert ".credit" not in name, (relative, name)


# ---------------------------------------------------------------------------
# Thin repositories: pure parts and the SQLite path.
# ---------------------------------------------------------------------------


def test_every_entry_type_has_a_direction() -> None:
    assert LOT_INCREASES | LOT_DECREASES | DEBT_INCREASES | DEBT_DECREASES == set(
        LEDGER_ENTRY_TYPES
    )
    assert not LOT_INCREASES & LOT_DECREASES
    assert not DEBT_INCREASES & DEBT_DECREASES
    assert LOT_DECREASES & DEBT_DECREASES == {"debt_settlement"}


def test_fold_ledger_reference_vector() -> None:
    monthly, topup = uuid4(), uuid4()
    movements = [
        LedgerMovement("grant", 5_000_000, monthly),
        LedgerMovement("grant", 500_000, topup),
        LedgerMovement("debit", 1_000, monthly),
        LedgerMovement("reversal", 200_000, topup),
        LedgerMovement("debt", 3_000, None),
        LedgerMovement("debt_settlement", 1_000, monthly),
        LedgerMovement("debt_release", 500, None),
        LedgerMovement("settlement_release", 1_000, monthly),
        LedgerMovement("lot_regrant", 100_000, topup),
        LedgerMovement("expiry", 4_999_000, monthly),
    ]
    assert fold_ledger(movements) == DerivedBalance(
        lots={monthly: 0, topup: 400_000}, debt_mc=1_500
    )
    assert fold_ledger([]) == DerivedBalance()


@pytest.mark.parametrize(
    ("movement", "message"),
    [
        (LedgerMovement("bonus", 1, uuid4()), "unknown ledger entry type"),
        (LedgerMovement("grant", 0, uuid4()), "positive"),
        (LedgerMovement("debit", 1, None), "name a lot"),
    ],
)
def test_fold_ledger_refuses_what_the_schema_refuses(
    movement: LedgerMovement, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        fold_ledger([movement])


def test_effects_digest_is_the_pinned_formula() -> None:
    assert EMPTY_EFFECTS_DIGEST == hashlib.sha256(b"[]").hexdigest()
    assert effects_digest([]) == EMPTY_EFFECTS_DIGEST
    assert (
        effects_digest(["b", "a"])
        == effects_digest(["a", "b"])
        == (hashlib.sha256(b'["a","b"]').hexdigest())
    )


def test_the_ack_schema_matches_what_the_reconciler_reads() -> None:
    from src.services.billing_outbox import ACK_SCHEMA_VERSION as producer_schema

    assert ACK_SCHEMA_VERSION == producer_schema == "billing.fact.ack.v1"


async def test_ensure_account_is_idempotent_on_sqlite() -> None:
    _patch_jsonb_for_sqlite()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        sessions = async_sessionmaker(engine, class_=AsyncSession)
        builder_id = uuid4()
        async with sessions() as session:
            repository = CreditRepository(session)
            first = await repository.ensure_account(builder_id)
            second = await repository.ensure_account(builder_id)
            other = await repository.ensure_account(uuid4())
            await session.commit()
            assert first == second != other
            locked = await repository.lock_account(builder_id)
            assert locked is not None and locked.id == first
            assert await repository.lock_lots(builder_id) == []
            assert await repository.derived_balance(builder_id) == DerivedBalance()
            assert await repository.enforcement_at() is None
            assert await repository.inbox_high_watermark() == 0
    finally:
        await engine.dispose()
