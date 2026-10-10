"""Tests for the one-off platform super_admin bootstrap."""

import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from passlib.context import CryptContext
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from scripts import bootstrap_admin as bootstrap
from src.core.exceptions import ConfigurationError
from src.models.tenant import Tenant
from src.models.user import User, UserRole
from src.repositories import close_database, get_db_session, init_database

EMAIL = "platform@example.com"
PASSWORD = "correct-horse-battery-staple"
NEW_PASSWORD = "another-long-password-0042"
DEV_FALLBACK_TENANT_ID = UUID("a0000000-0000-0000-0000-000000000001")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
# Sample of API keys versioned in the repository (seed, init-db.sql, fallback).
VERSIONED_API_KEYS = (
    "ns_admin_key",
    "ns_builder_key",
    "ns_investor_key",
    "ns_dev_demo_key",
    "test-api-key",
    "ns_dev_test_key_12345",
    "ns_dev_builder_key_67890",
    "dev-api-key-12345",
)

verifier = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _env(**overrides: str | None) -> dict[str, str]:
    env: dict[str, str | None] = {
        bootstrap.EMAIL_ENV: EMAIL,
        bootstrap.PASSWORD_ENV: PASSWORD,
    }
    env.update(overrides)
    return {name: value for name, value in env.items() if value is not None}


def _request(**overrides: str | None) -> bootstrap.BootstrapRequest:
    return bootstrap.load_request(_env(**overrides))


def _reset(password: str = NEW_PASSWORD) -> bootstrap.BootstrapRequest:
    return _request(**{bootstrap.PASSWORD_ENV: password, bootstrap.RESET_ENV: "true"})


@pytest.fixture
async def database() -> AsyncIterator[None]:
    await init_database()
    try:
        yield
    finally:
        await close_database()


async def _apply(request: bootstrap.BootstrapRequest) -> str:
    async with get_db_session() as session:
        async with session.begin():
            outcome, _ = await bootstrap.bootstrap_admin(session, request)
    return outcome


async def _add_tenant(**fields: object) -> Tenant:
    tenant_id = fields.pop("id", uuid4())
    tenant = Tenant(
        id=tenant_id,
        name=fields.pop("name", f"Tenant {tenant_id}"),
        api_key_hash=f"hash-{tenant_id}",
        config={},
        **fields,
    )
    async with get_db_session() as session:
        async with session.begin():
            session.add(tenant)
    return tenant


async def _add_user(
    email: str,
    role: UserRole,
    *,
    is_active: bool = True,
    password: str = "existing-password-123",
    tenant_id: UUID | None = None,
) -> User:
    if tenant_id is None:
        tenant_id = (await _add_tenant()).id
    user = User(
        id=uuid4(),
        tenant_id=tenant_id,
        email=email,
        password_hash=verifier.hash(password),
        role=role,
        is_active=is_active,
    )
    async with get_db_session() as session:
        async with session.begin():
            session.add(user)
    return user


async def _snapshot() -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    async with get_db_session() as session:
        tenants = await session.execute(
            select(
                Tenant.id,
                Tenant.slug,
                Tenant.plan_type,
                Tenant.is_active,
                Tenant.updated_at,
            ).order_by(Tenant.id)
        )
        users = await session.execute(
            select(
                User.id,
                User.email,
                User.role,
                User.is_active,
                User.tenant_id,
                User.created_at,
                User.password_hash,
                User.updated_at,
            ).order_by(User.id)
        )
        return [tuple(row) for row in tenants], [tuple(row) for row in users]


async def _user(email: str = EMAIL) -> User:
    async with get_db_session() as session:
        result = await session.execute(select(User).where(User.email == email))
        return result.scalars().one()


async def _assert_refused(request: bootstrap.BootstrapRequest, message: str) -> None:
    before = await _snapshot()
    with pytest.raises(bootstrap.BootstrapError, match=message):
        await _apply(request)
    assert await _snapshot() == before


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        (
            {bootstrap.EMAIL_ENV: None},
            "missing required environment: BOOTSTRAP_ADMIN_EMAIL",
        ),
        (
            {bootstrap.PASSWORD_ENV: None},
            "missing required environment: BOOTSTRAP_ADMIN_PASSWORD",
        ),
        ({bootstrap.EMAIL_ENV: "Platform@example.com"}, "must be lowercase"),
        ({bootstrap.EMAIL_ENV: " platform@example.com"}, "not a valid email"),
        ({bootstrap.EMAIL_ENV: "platform@example.com\n"}, "not a valid email"),
        ({bootstrap.EMAIL_ENV: "platform.example.com"}, "not a valid email"),
        ({bootstrap.EMAIL_ENV: "@example.com"}, "not a valid email"),
        ({bootstrap.EMAIL_ENV: "a@b@example.com"}, "not a valid email"),
        ({bootstrap.EMAIL_ENV: "platform@localhost"}, "not a valid email"),
        ({bootstrap.EMAIL_ENV: "platform@example."}, "not a valid email"),
        ({bootstrap.EMAIL_ENV: "platform@.example.com"}, "not a valid email"),
        ({bootstrap.EMAIL_ENV: "x" * 244 + "@example.com"}, "not a valid email"),
        ({bootstrap.PASSWORD_ENV: "x" * 15}, "at least 16 characters"),
        ({bootstrap.PASSWORD_ENV: "é" * 37}, "at most 72 bytes"),
        ({bootstrap.PASSWORD_ENV: PASSWORD + "\n"}, "surrounding whitespace"),
        ({bootstrap.PASSWORD_ENV: " " + PASSWORD}, "surrounding whitespace"),
        (
            {bootstrap.PASSWORD_ENV: "correct-horse\tbattery-staple"},
            "control characters",
        ),
        ({bootstrap.RESET_ENV: "yes"}, "must be 'true' or 'false'"),
        ({bootstrap.RESET_ENV: "TRUE"}, "must be 'true' or 'false'"),
    ],
)
async def test_invalid_input_fails_before_touching_the_database(
    overrides: dict[str, str | None],
    message: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def forbidden_init_database() -> None:
        raise AssertionError("database must not be initialized for invalid input")

    monkeypatch.setattr(bootstrap, "init_database", forbidden_init_database)

    assert await bootstrap.main(_env(**overrides)) == 1

    out, err = capsys.readouterr()
    assert out == ""
    assert message in err
    rejected_password = overrides.get(bootstrap.PASSWORD_ENV) or PASSWORD
    assert rejected_password.strip() not in err


def test_valid_input_accepts_boundaries() -> None:
    long_email = "x" * 243 + "@example.com"
    assert _request(**{bootstrap.EMAIL_ENV: long_email}).email == long_email
    assert _request(**{bootstrap.PASSWORD_ENV: "x" * 16}).reset_password is False
    assert _request(**{bootstrap.PASSWORD_ENV: "é" * 36}).password == "é" * 36
    assert _request(**{bootstrap.RESET_ENV: "false"}).reset_password is False
    assert _request(**{bootstrap.RESET_ENV: "true"}).reset_password is True


def test_request_repr_hides_password() -> None:
    assert PASSWORD not in repr(_request())


# ---------------------------------------------------------------------------
# Evaluation table
# ---------------------------------------------------------------------------


async def test_row8_empty_database_creates_tenant_and_super_admin(
    database: None,
) -> None:
    assert await _apply(_request()) == "created"

    user = await _user()
    assert user.role == UserRole.super_admin
    assert user.is_active is True
    assert verifier.verify(PASSWORD, user.password_hash)

    async with get_db_session() as session:
        tenant = await session.get(Tenant, user.tenant_id)
    assert tenant is not None
    assert tenant.id != DEV_FALLBACK_TENANT_ID
    assert tenant.slug == bootstrap.PLATFORM_TENANT_SLUG
    assert tenant.plan_type == "admin"
    assert tenant.is_active is True
    assert not any(
        verifier.verify(key, tenant.api_key_hash) for key in VERSIONED_API_KEYS
    )


async def test_row8_dev_fallback_tenant_is_never_used(database: None) -> None:
    await _add_tenant(id=DEV_FALLBACK_TENANT_ID, name="Development Tenant")

    assert await _apply(_request()) == "created"

    assert (await _user()).tenant_id != DEV_FALLBACK_TENANT_ID


async def test_row4_same_password_without_flag_is_a_noop(database: None) -> None:
    await _apply(_request())
    before = await _snapshot()

    assert await _apply(_request()) == "skip"

    assert await _snapshot() == before


async def test_row5_other_password_without_flag_is_refused(database: None) -> None:
    await _apply(_request())

    await _assert_refused(
        _request(**{bootstrap.PASSWORD_ENV: NEW_PASSWORD}),
        "differs from the stored password",
    )
    assert verifier.verify(PASSWORD, (await _user()).password_hash)


async def test_rows4_5_reset_flag_changes_only_the_password(database: None) -> None:
    await _apply(_request())
    tenants_before, users_before = await _snapshot()

    assert await _apply(_reset()) == "reset"

    tenants_after, users_after = await _snapshot()
    assert tenants_after == tenants_before
    assert len(users_after) == len(users_before) == 1
    # id, email, role, is_active, tenant_id, created_at
    assert users_after[0][:6] == users_before[0][:6]
    user = await _user()
    assert verifier.verify(NEW_PASSWORD, user.password_hash)
    assert not verifier.verify(PASSWORD, user.password_hash)


async def test_rows4_5_own_email_wins_over_another_super_admin(
    database: None,
) -> None:
    await _apply(_request())
    await _add_user("other@example.com", UserRole.super_admin)

    assert await _apply(_request()) == "skip"
    assert await _apply(_reset()) == "reset"


async def test_row1_case_variants_of_the_email_are_refused(database: None) -> None:
    await _add_user("Platform@Example.com", UserRole.super_admin, password=PASSWORD)
    await _add_user("PLATFORM@example.com", UserRole.super_admin, password=PASSWORD)

    for request in (_request(), _reset()):
        await _assert_refused(request, "ignoring case; refusing to choose one")


@pytest.mark.parametrize("role", [UserRole.clinic_admin, UserRole.builder])
async def test_row2_existing_account_is_never_promoted(
    database: None, role: UserRole
) -> None:
    await _add_user("Platform@Example.com", role, password=PASSWORD)

    for request in (_request(), _reset()):
        await _assert_refused(request, "refusing to promote it")


async def test_row3_inactive_super_admin_is_never_reactivated(database: None) -> None:
    await _add_user(EMAIL, UserRole.super_admin, is_active=False, password=PASSWORD)

    for request in (_request(), _reset()):
        await _assert_refused(request, "refusing to reactivate it")


async def test_case_only_email_difference_is_refused(database: None) -> None:
    await _add_user("Platform@Example.com", UserRole.super_admin, password=PASSWORD)

    for request in (_request(), _reset()):
        await _assert_refused(request, "only by case; refusing to maintain it")


@pytest.mark.parametrize(
    "tenant_fields",
    [
        # Shape created by scripts/seed_demo.py: fallback ID with the platform slug.
        {
            "id": DEV_FALLBACK_TENANT_ID,
            "slug": bootstrap.PLATFORM_TENANT_SLUG,
            "plan_type": "admin",
        },
        {"slug": "other-tenant", "plan_type": "admin"},
        {"slug": bootstrap.PLATFORM_TENANT_SLUG, "plan_type": "pilot"},
    ],
)
async def test_super_admin_outside_platform_tenant_is_never_maintained(
    database: None, tenant_fields: dict[str, object]
) -> None:
    tenant = await _add_tenant(**tenant_fields)
    await _add_user(EMAIL, UserRole.super_admin, password=PASSWORD, tenant_id=tenant.id)

    for request in (_request(), _reset()):
        await _assert_refused(request, "not on the bootstrap platform tenant")


async def test_reset_flag_applies_even_with_matching_password(database: None) -> None:
    await _apply(_request())
    _, users_before = await _snapshot()

    assert await _apply(_reset(PASSWORD)) == "reset"

    _, users_after = await _snapshot()
    assert users_after[0][6] != users_before[0][6]  # fresh bcrypt salt
    assert verifier.verify(PASSWORD, (await _user()).password_hash)


async def test_second_email_after_bootstrap_reports_existing_super_admin(
    database: None,
) -> None:
    await _apply(_request())

    await _assert_refused(
        _request(**{bootstrap.EMAIL_ENV: "other@example.com"}),
        "another super_admin already exists",
    )


@pytest.mark.parametrize("is_active", [True, False])
async def test_row6_second_super_admin_is_refused(
    database: None, is_active: bool
) -> None:
    await _add_user("other@example.com", UserRole.super_admin, is_active=is_active)

    for request in (_request(), _reset()):
        await _assert_refused(request, "another super_admin already exists")


@pytest.mark.parametrize(
    "tenant_fields",
    [
        {"plan_type": "admin", "is_active": True},
        {"plan_type": "admin", "is_active": False},
        {"plan_type": "pilot", "is_active": True},
        # Shape created by scripts/seed_demo.py, with a versioned API key.
        {"id": DEV_FALLBACK_TENANT_ID, "plan_type": "admin", "is_active": True},
    ],
)
async def test_row7_existing_platform_tenant_is_never_adopted(
    database: None, tenant_fields: dict[str, object]
) -> None:
    await _add_tenant(slug=bootstrap.PLATFORM_TENANT_SLUG, **tenant_fields)

    for request in (_request(), _reset()):
        await _assert_refused(request, "refusing to adopt it")


# ---------------------------------------------------------------------------
# Output never leaks the password or its hash
# ---------------------------------------------------------------------------


def _assert_no_secret(output: str) -> None:
    assert PASSWORD not in output
    assert NEW_PASSWORD not in output
    assert "$2b$" not in output


async def test_main_created_prints_one_result_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert await bootstrap.main(_env()) == 0

    out, err = capsys.readouterr()
    assert out.startswith(f"[created] super_admin {EMAIL} on tenant ")
    assert out.count("\n") == 1
    _assert_no_secret(out + err)


@pytest.mark.parametrize(
    ("env", "outcome"),
    [
        (_env(), "skip"),
        (
            _env(**{bootstrap.PASSWORD_ENV: NEW_PASSWORD, bootstrap.RESET_ENV: "true"}),
            "reset",
        ),
    ],
)
async def test_main_rerun_prints_outcome(
    env: dict[str, str],
    outcome: str,
    database: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    await _apply(_request())
    capsys.readouterr()

    assert await bootstrap.main(env) == 0

    out, err = capsys.readouterr()
    assert out.startswith(f"[{outcome}] super_admin {EMAIL} on tenant ")
    _assert_no_secret(out + err)


@pytest.mark.parametrize(
    "env",
    [
        _env(**{bootstrap.PASSWORD_ENV: NEW_PASSWORD}),
        _env(**{bootstrap.EMAIL_ENV: "other@example.com"}),
    ],
)
async def test_main_refusal_exits_nonzero(
    env: dict[str, str], database: None, capsys: pytest.CaptureFixture[str]
) -> None:
    await _apply(_request())
    capsys.readouterr()

    assert await bootstrap.main(env) == 1

    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("[error] ")
    _assert_no_secret(err)


@pytest.fixture
def keep_database(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() disposes the engine, which would drop the in-memory database."""

    async def keep_open() -> None:
        return None

    monkeypatch.setattr(bootstrap, "close_database", keep_open)


async def test_main_commits_created_super_admin(
    database: None, keep_database: None
) -> None:
    assert await bootstrap.main(_env()) == 0

    user = await _user()
    assert verifier.verify(PASSWORD, user.password_hash)
    async with get_db_session() as session:
        tenant = await session.get(Tenant, user.tenant_id)
    assert tenant is not None
    assert tenant.slug == bootstrap.PLATFORM_TENANT_SLUG


async def test_main_commits_reset_password(database: None, keep_database: None) -> None:
    await _apply(_request())

    env = _env(**{bootstrap.PASSWORD_ENV: NEW_PASSWORD, bootstrap.RESET_ENV: "true"})
    assert await bootstrap.main(env) == 0

    assert verifier.verify(NEW_PASSWORD, (await _user()).password_hash)


def test_module_imports_before_any_other_src_module() -> None:
    """The image runs the module directly, so its own import order must work."""
    result = subprocess.run(
        [sys.executable, "-c", "import scripts.bootstrap_admin"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert result.returncode == 0, result.stderr[-2000:]


async def test_main_database_error_prints_type_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    leaked_hash = verifier.hash(PASSWORD)

    async def conflicting_bootstrap(*_: object) -> None:
        raise IntegrityError(
            "INSERT INTO users ...",
            {"password_hash": leaked_hash, "email": EMAIL},
            Exception("duplicate key value violates unique constraint"),
        )

    monkeypatch.setattr(bootstrap, "bootstrap_admin", conflicting_bootstrap)

    assert await bootstrap.main(_env()) == 1

    out, err = capsys.readouterr()
    assert out == ""
    assert err.strip() == "[error] database error: IntegrityError"
    _assert_no_secret(err)


async def test_main_unavailable_database_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def unavailable_database() -> None:
        raise ConfigurationError("Failed to initialize database connection")

    monkeypatch.setattr(bootstrap, "init_database", unavailable_database)

    assert await bootstrap.main(_env()) == 1

    out, err = capsys.readouterr()
    assert out == ""
    assert err.strip() == "[error] database unavailable"
