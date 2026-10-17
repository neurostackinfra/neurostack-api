from __future__ import annotations

import re
from pathlib import Path

import pytest

from scripts.validate_asaas_registration_database_readiness import (
    ReadinessError,
    validate_readiness_row,
    validate_readiness_sql_contract,
)

ROOT = Path(__file__).resolve().parents[2]
SQL = ROOT / "scripts" / "asaas_registration_database_readiness.sql"


def _ready_row() -> dict[str, object]:
    return {
        "task_ips": ["10.0.1.7", "10.0.1.8"],
        "task_ip_count": 2,
        "matched_task_ip_count": 2,
        "runtime_role_count": 1,
        "api_runtime_role": "neurostack_runtime",
        "required_columns_present": True,
        "required_constraints_present": True,
        "required_privileges_present": True,
        "row_lock_privilege_present": True,
        "readiness_ok": True,
    }


def test_sql_uses_catalog_and_session_metadata_only() -> None:
    sql = SQL.read_text(encoding="utf-8")
    validate_readiness_sql_contract(sql)
    uncommented = re.sub(r"/\*.*?\*/|--[^\n]*", "", sql, flags=re.DOTALL)
    without_literals = re.sub(r"'(?:''|[^'])*'", "''", uncommented)
    assert "pg_stat_activity" in uncommented
    assert "to_json(binding.task_ips) AS task_ips" in uncommented
    assert "pg_catalog.pg_class" in uncommented
    assert "pg_catalog.pg_namespace" in uncommented
    assert "pg_catalog.pg_attribute" in uncommented
    assert "pg_catalog.pg_constraint" in uncommented
    assert "pg_catalog.has_table_privilege" in uncommented
    assert re.search(
        r"has_table_privilege\([^)]*'public\.users',[^)]*'SELECT'\).*?has_table_privilege\([^)]*'public\.users',[^)]*'UPDATE'\).*?AS row_lock_privilege_present",
        uncommented,
        re.S,
    )
    assert "pg_stat_activity.query" not in uncommented
    assert not re.search(
        r"\b(?:INSERT|UPDATE|DELETE|MERGE|CREATE|ALTER|DROP|GRANT|REVOKE|CALL|DO|COPY)\b",
        without_literals,
        re.I,
    )
    assert not re.search(
        r"\b(?:GetSecretValue|SecretString|SecretBinary)\b", without_literals, re.I
    )
    assert "192.0.2.10" in uncommented
    assert "billing_producer_v1_build_attestations" in uncommented
    assert "uq_billing_producer_v1_attestation_digest" in uncommented


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (
            "('billing_producer_v1_build_attestations', 'billing_producer_v1_build_attestations_pkey', 'p', ARRAY['id']::text[], NULL, NULL, NULL, NULL, NULL)",
            "('billing_producer_v1_build_attestations', 'billing_producer_v1_build_attestations_pkey', 'p', ARRAY['operation_id']::text[], NULL, NULL, NULL, NULL, NULL)",
        ),
        (
            "'lengthenvelope_digest=64andenvelope_digest~''^[0-9a-f]{64}$'''",
            "'lengthenvelope_digest=63andenvelope_digest~''^[0-9a-f]{64}$'''",
        ),
        (
            "'lengthpayload_digest=64andpayload_digest~''^[0-9a-f]{64}$'''",
            "'lengthpayload_digest=64andpayload_digest~''^[0-9a-f]{63}$'''",
        ),
        (
            "'public', 'users', ARRAY['id']::text[], 'r')",
            "'public', 'roles', ARRAY['id']::text[], 'r')",
        ),
        (
            "'public', 'users', ARRAY['id']::text[], 'r')",
            "'public', 'users', ARRAY['role']::text[], 'c')",
        ),
    ],
)
def test_static_sql_contract_rejects_wrong_constraint_semantics(
    old: str, new: str
) -> None:
    sql = SQL.read_text(encoding="utf-8")
    assert old in sql
    mutated = sql.replace(old, new, 1)
    with pytest.raises(
        ReadinessError, match="readiness_sql_constraint_contract_invalid"
    ):
        validate_readiness_sql_contract(mutated)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("= expected.check_expression", "<> expected.check_expression"),
        (
            ")::text[] = expected.key_columns",
            ")::text[] = ARRAY['operation_id']::text[]",
        ),
        (
            ")::text[] = expected.referenced_columns",
            ")::text[] = ARRAY['role']::text[]",
        ),
        (
            "constraint_row.confdeltype = expected.delete_action",
            "constraint_row.confdeltype = 'a'",
        ),
    ],
)
def test_static_sql_contract_rejects_weakened_catalog_predicates(
    old: str, new: str
) -> None:
    sql = SQL.read_text(encoding="utf-8")
    assert old in sql
    mutated = sql.replace(old, new, 1)
    with pytest.raises(ReadinessError, match="readiness_sql_semantic_check_missing"):
        validate_readiness_sql_contract(mutated)


def test_complete_schema_privilege_and_task_binding_fixture_passes() -> None:
    result = validate_readiness_row(_ready_row())
    assert result == {
        "schema": "asaas_registration_database_readiness.v1",
        "task_ips": ["10.0.1.7", "10.0.1.8"],
        "task_ip_count": 2,
        "matched_task_ip_count": 2,
        "runtime_role_count": 1,
        "api_runtime_role": "neurostack_runtime",
        "required_columns_present": True,
        "required_constraints_present": True,
        "required_privileges_present": True,
        "row_lock_privilege_present": True,
        "readiness_ok": True,
    }


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("required_columns_present", False, "required_columns_present_failed"),
        ("required_constraints_present", False, "required_constraints_present_failed"),
        ("required_privileges_present", False, "required_privileges_present_failed"),
        ("row_lock_privilege_present", False, "row_lock_privilege_present_failed"),
        ("readiness_ok", False, "readiness_ok_failed"),
    ],
)
def test_missing_schema_constraint_or_runtime_privilege_fails_closed(
    field: str, value: bool, reason: str
) -> None:
    row = _ready_row()
    row[field] = value
    with pytest.raises(ReadinessError, match=reason):
        validate_readiness_row(row)


def test_missing_task_to_runtime_role_binding_fails_closed() -> None:
    row = _ready_row()
    row["matched_task_ip_count"] = 1
    with pytest.raises(
        ReadinessError, match="active_task_to_database_role_binding_failed"
    ):
        validate_readiness_row(row)


@pytest.mark.parametrize(
    "task_ips",
    [
        [],
        ["10.0.1.7", "10.0.1.7"],
        ["10.0.1.7", "2001:db8::1"],
        ["10.0.1.7/32", "10.0.1.8"],
    ],
)
def test_invalid_or_ambiguous_task_ip_set_fails_closed(task_ips: list[str]) -> None:
    row = _ready_row()
    row["task_ips"] = task_ips
    with pytest.raises(ReadinessError, match="readiness_result_task_ips_invalid"):
        validate_readiness_row(row)


def test_task_ip_set_count_must_match_readiness_counts() -> None:
    row = _ready_row()
    row["task_ips"] = ["10.0.1.7"]
    with pytest.raises(
        ReadinessError, match="active_task_to_database_role_binding_failed"
    ):
        validate_readiness_row(row)


@pytest.mark.parametrize(
    "row",
    [
        None,
        {},
        {**_ready_row(), "unreviewed": "extra"},
        {**_ready_row(), "api_runtime_role": None},
    ],
)
def test_unreadable_or_ambiguous_metadata_fails_closed(row: object) -> None:
    with pytest.raises(ReadinessError):
        validate_readiness_row(row)
