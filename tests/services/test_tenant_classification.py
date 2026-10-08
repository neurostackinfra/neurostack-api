"""Truth table for the canonical role-based clinic classifier."""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import postgresql

from src.models.user import UserRole
from src.services.tenant_classification import (
    is_real_clinic_user_roles,
    real_clinic_tenant_filter,
)


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        ([], True),
        ([UserRole.clinic_admin], True),
        ([UserRole.builder, UserRole.clinic_admin], True),
        ([UserRole.super_admin, UserRole.clinic_admin], True),
        ([UserRole.super_admin], False),
        ([UserRole.builder], False),
        ([UserRole.investor], False),
        ([UserRole.builder, UserRole.super_admin, UserRole.investor], False),
    ],
)
def test_role_classifier_preserves_admin_semantics(
    roles: list[UserRole],
    expected: bool,
) -> None:
    assert is_real_clinic_user_roles(roles) is expected


def test_sql_classifier_is_role_only_and_keeps_userless_tenants() -> None:
    compiled = str(
        real_clinic_tenant_filter().compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert compiled.count("EXISTS") == 2
    assert "users.tenant_id = tenants.id" in compiled
    assert "builder" in compiled
    assert "super_admin" in compiled
    assert "investor" in compiled
    assert "tenants.is_active" not in compiled
    assert "tenants.plan_type" not in compiled
