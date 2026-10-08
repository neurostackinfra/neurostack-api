"""Canonical role-based classification for real clinic tenants."""

from collections.abc import Iterable

from sqlalchemy import String, cast, select
from sqlalchemy.sql.elements import ColumnElement

from src.models.tenant import Tenant
from src.models.user import User, UserRole

NON_CLINIC_USER_ROLES: tuple[UserRole, ...] = (
    UserRole.builder,
    UserRole.super_admin,
    UserRole.investor,
)


def real_clinic_tenant_filter() -> ColumnElement[bool]:
    """Select tenants with no users or at least one clinic-role user.

    This is the role-only predicate historically used by the admin dashboard.
    Callers with stricter requirements, such as Media Assets, add their own
    active and plan filters alongside this expression.
    """
    has_users = select(User.id).where(User.tenant_id == Tenant.id).exists()
    has_clinic_user = (
        select(User.id)
        .where(
            User.tenant_id == Tenant.id,
            ~cast(User.role, String).in_(
                tuple(role.value for role in NON_CLINIC_USER_ROLES)
            ),
        )
        .exists()
    )
    return ~(has_users & ~has_clinic_user)


def is_real_clinic_user_roles(roles: Iterable[UserRole]) -> bool:
    """Apply the same role predicate to an already loaded user collection."""
    collected = tuple(roles)
    return not collected or any(role not in NON_CLINIC_USER_ROLES for role in collected)
