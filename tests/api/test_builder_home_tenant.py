"""``builder_home_tenant_account`` directly (the shared home-tenant fallback
of the credit reads and the export).

Pins the helper's own early checks for any future caller: every refusal that
the token alone decides runs NO SQL, and the happy path runs exactly one
``users`` statement (the token's row and the shared-tenant ``EXISTS``
together).  A fake session records the statements; the row it returns
stands for the ``users`` row.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID, uuid4

import pytest

from src.api.helpers.builder_home_tenant import builder_home_tenant_account
from src.api.middleware.auth import TenantContext


@dataclass
class _Row:
    is_active: bool
    role: str
    builder_id: UUID | None
    tenant_id: UUID
    shared: bool = False


@dataclass
class _Result:
    row: _Row | None

    def one_or_none(self) -> _Row | None:
        return self.row


@dataclass
class _Session:
    row: _Row | None
    statements: list[str] = field(default_factory=list)

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(str(statement))
        return _Result(self.row)


BUILDER = uuid4()
HOME = uuid4()
USER = uuid4()


def _token(**overrides: Any) -> TenantContext:
    values: dict[str, Any] = {
        "tenant_id": HOME,
        "user_id": USER,
        "role": "builder",
        "builder_id": BUILDER,
        "authentication_mode": "jwt",
    }
    values.update(overrides)
    return TenantContext(**values)


def _current() -> _Row:
    return _Row(is_active=True, role="builder", builder_id=BUILDER, tenant_id=HOME)


def _users_statements(session: _Session) -> list[str]:
    return [s for s in session.statements if "FROM users" in s]


async def test_the_happy_path_returns_the_claim_with_one_users_statement() -> None:
    session = _Session(_current())
    assert await builder_home_tenant_account(session, _token(), HOME, None) == BUILDER
    assert len(session.statements) == 1
    assert len(_users_statements(session)) == 1
    # The shared-tenant check is in that same statement.
    assert "EXISTS" in session.statements[0]


@pytest.mark.parametrize(
    "case",
    [
        "api_key",
        "api_key_mode",
        "role_clinic_admin",
        "role_super_admin",
        "role_none",
        "no_builder_claim",
        "no_user_id",
        "clinic_override",
        "tenant_linked_to_the_same_builder",
        "tenant_linked_to_another_builder",
    ],
)
async def test_token_decided_refusals_run_no_sql(case: str) -> None:
    token = _token()
    effective, stored = HOME, None
    if case == "api_key":
        token = _token(api_key="ns_key", authentication_mode="jwt")
    elif case == "api_key_mode":
        token = _token(authentication_mode="api_key")
    elif case == "role_clinic_admin":
        token = _token(role="clinic_admin")
    elif case == "role_super_admin":
        token = _token(role="super_admin")
    elif case == "role_none":
        token = _token(role=None)
    elif case == "no_builder_claim":
        token = _token(builder_id=None)
    elif case == "no_user_id":
        token = _token(user_id=None)
    elif case == "clinic_override":
        effective = uuid4()
    elif case == "tenant_linked_to_the_same_builder":
        # Not the fallback's case: the callers' linked path serves it.
        stored = BUILDER
    else:
        stored = uuid4()
    # A row that WOULD be accepted: only the early checks can refuse here.
    session = _Session(_current())
    assert await builder_home_tenant_account(session, token, effective, stored) is None
    assert session.statements == []


@pytest.mark.parametrize(
    "change",
    [
        {"is_active": False},
        {"role": "clinic_admin"},
        {"builder_id": uuid4()},
        {"builder_id": None},
        {"tenant_id": uuid4()},
        {"shared": True},
        None,
    ],
)
async def test_the_users_row_refusals_take_one_statement(
    change: dict[str, Any] | None,
) -> None:
    row = None if change is None else replace(_current(), **change)
    session = _Session(row)
    assert await builder_home_tenant_account(session, _token(), HOME, None) is None
    assert len(_users_statements(session)) == len(session.statements) == 1
