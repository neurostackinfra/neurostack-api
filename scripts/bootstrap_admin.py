#!/usr/bin/env python3
"""
One-off bootstrap of the platform super_admin.

Creates the platform tenant and the single super_admin whose credentials come
only from the runtime environment (Secrets Manager in ECS), so a fresh
database becomes reachable through the panel without any credential in git.

Re-running never writes unless BOOTSTRAP_ADMIN_RESET_PASSWORD=true, which
re-applies the password to the same super_admin; without the flag a password
that differs from the stored one is an error, so the secret and the database
cannot drift silently. super_admin has no self-service password change, so this
is its rotation and recovery path. Only the super_admin on the platform
tenant created by this script is maintained. Unexpected states (another
super_admin, another role, inactive account, an email differing only by case,
an account outside the platform tenant, an existing platform tenant) are
refused without writing anything.

Run once per environment, after ``alembic upgrade head``:
    python -m scripts.bootstrap_admin
"""

import asyncio
import os
import secrets
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from uuid import UUID, uuid4

from passlib.context import CryptContext
from sqlalchemy import String, cast, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

# Keep src.core.exceptions as the first src import: importing src.models or
# src.repositories first fails with a circular import in the production image.
from src.core.exceptions import ConfigurationError
from src.models.tenant import Tenant
from src.models.user import User, UserRole
from src.repositories import close_database, get_db_session, init_database

# Same hashing configuration as the login route
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)

EMAIL_ENV = "BOOTSTRAP_ADMIN_EMAIL"
PASSWORD_ENV = "BOOTSTRAP_ADMIN_PASSWORD"
RESET_ENV = "BOOTSTRAP_ADMIN_RESET_PASSWORD"

MAX_EMAIL_LENGTH = 255
MIN_PASSWORD_LENGTH = 16
MAX_PASSWORD_BYTES = 72  # bcrypt silently truncates beyond this

PLATFORM_TENANT_NAME = "NeuroStack Admin"
PLATFORM_TENANT_SLUG = "neurostack-admin"
PLATFORM_PLAN_TYPE = "admin"
# Tenant the development API key fallback resolves to; never a platform tenant.
DEV_FALLBACK_TENANT_ID = UUID("a0000000-0000-0000-0000-000000000001")


class BootstrapError(Exception):
    """Bootstrap refused. Messages never include the password."""


@dataclass(frozen=True)
class BootstrapRequest:
    email: str
    password: str = field(repr=False)
    reset_password: bool = False


def _is_valid_email(email: str) -> bool:
    local, _, domain = email.partition("@")
    return (
        bool(local)
        and "@" not in domain
        and "." in domain
        and not domain.startswith(".")
        and not domain.endswith(".")
        and not any(char.isspace() for char in email)
    )


def load_request(environ: Mapping[str, str]) -> BootstrapRequest:
    """Read and validate the bootstrap input from the environment."""
    email = environ.get(EMAIL_ENV, "")
    password = environ.get(PASSWORD_ENV, "")
    reset_raw = environ.get(RESET_ENV, "")

    missing = [
        name
        for name, value in ((EMAIL_ENV, email), (PASSWORD_ENV, password))
        if not value
    ]
    if missing:
        raise BootstrapError("missing required environment: " + ", ".join(missing))

    # Login compares the exact text, so the stored value must be the secret value.
    if not _is_valid_email(email) or len(email) > MAX_EMAIL_LENGTH:
        raise BootstrapError(f"{EMAIL_ENV} is not a valid email address")
    if email != email.lower():
        raise BootstrapError(f"{EMAIL_ENV} must be lowercase")

    if len(password) < MIN_PASSWORD_LENGTH:
        raise BootstrapError(
            f"{PASSWORD_ENV} must have at least {MIN_PASSWORD_LENGTH} characters"
        )
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise BootstrapError(
            f"{PASSWORD_ENV} must have at most {MAX_PASSWORD_BYTES} bytes"
        )
    # A trailing newline from a file-based secret would never match a login.
    if password != password.strip() or not password.isprintable():
        raise BootstrapError(
            f"{PASSWORD_ENV} must not have surrounding whitespace "
            "or control characters"
        )

    if reset_raw not in ("", "true", "false"):
        raise BootstrapError(f"{RESET_ENV} must be 'true' or 'false'")

    return BootstrapRequest(
        email=email, password=password, reset_password=reset_raw == "true"
    )


async def _create_platform_tenant(session: AsyncSession) -> Tenant:
    """Create the platform tenant; an existing one is never adopted."""
    result = await session.execute(
        select(Tenant.id).where(Tenant.slug == PLATFORM_TENANT_SLUG)
    )
    existing_id = result.scalars().first()
    if existing_id is not None:
        raise BootstrapError(
            f"tenant slug {PLATFORM_TENANT_SLUG} already exists ({existing_id}) "
            "without a super_admin; refusing to adopt it"
        )

    tenant = Tenant(
        id=uuid4(),
        name=PLATFORM_TENANT_NAME,
        slug=PLATFORM_TENANT_SLUG,
        # The column is required and unique, but the platform tenant never
        # authenticates by API key: hash a throwaway token.
        api_key_hash=pwd_context.hash(secrets.token_urlsafe(32)),
        is_active=True,
        plan_type=PLATFORM_PLAN_TYPE,
        config={},
    )
    session.add(tenant)
    await session.flush()
    return tenant


async def _maintain(
    session: AsyncSession, existing: User, request: BootstrapRequest
) -> str:
    """Skip or reset the bootstrap super_admin bound to the requested email."""
    if existing.role != UserRole.super_admin:
        raise BootstrapError(
            f"user {request.email} already exists with role "
            f"{existing.role.value}; refusing to promote it"
        )
    if not existing.is_active:
        raise BootstrapError(
            f"super_admin {request.email} is inactive; refusing to reactivate it"
        )
    if existing.email != request.email:
        raise BootstrapError(
            f"stored email {existing.email} differs from {request.email} "
            "only by case; refusing to maintain it"
        )
    tenant = await session.get(Tenant, existing.tenant_id)
    if (
        tenant is None
        or tenant.id == DEV_FALLBACK_TENANT_ID
        or tenant.slug != PLATFORM_TENANT_SLUG
        or tenant.plan_type != PLATFORM_PLAN_TYPE
    ):
        raise BootstrapError(
            f"super_admin {request.email} is not on the bootstrap platform "
            "tenant; refusing to maintain it"
        )
    if request.reset_password:
        existing.password_hash = pwd_context.hash(request.password)
        return "reset"
    if not pwd_context.verify(request.password, existing.password_hash):
        raise BootstrapError(
            f"{PASSWORD_ENV} differs from the stored password; "
            f"set {RESET_ENV}=true to apply it"
        )
    return "skip"


async def bootstrap_admin(
    session: AsyncSession, request: BootstrapRequest
) -> tuple[str, User]:
    """Apply the bootstrap; return ``created``, ``skip`` or ``reset`` and the user."""
    result = await session.execute(
        select(User).where(func.lower(User.email) == request.email)
    )
    matches = result.scalars().all()
    if len(matches) > 1:
        raise BootstrapError(
            f"{len(matches)} users match {request.email} ignoring case; "
            "refusing to choose one"
        )
    if matches:
        outcome = await _maintain(session, matches[0], request)
        await session.flush()
        return outcome, matches[0]

    # users.role is VARCHAR in the migrated schema; compare as text like the
    # rest of the application does.
    result = await session.execute(
        select(User.email).where(cast(User.role, String) == UserRole.super_admin.value)
    )
    other_email = result.scalars().first()
    if other_email is not None:
        raise BootstrapError(
            f"another super_admin already exists ({other_email}); "
            "refusing to create a second one"
        )

    tenant = await _create_platform_tenant(session)
    user = User(
        id=uuid4(),
        tenant_id=tenant.id,
        email=request.email,
        password_hash=pwd_context.hash(request.password),
        role=UserRole.super_admin,
        is_active=True,
    )
    session.add(user)
    await session.flush()
    return "created", user


async def main(environ: Mapping[str, str] | None = None) -> int:
    """Validate the input, then bootstrap inside a single transaction."""
    try:
        request = load_request(os.environ if environ is None else environ)
    except BootstrapError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1

    try:
        await init_database()
        async with get_db_session() as session:
            async with session.begin():
                outcome, user = await bootstrap_admin(session, request)
    except BootstrapError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 1
    except ConfigurationError:
        print("[error] database unavailable", file=sys.stderr)
        return 1
    except SQLAlchemyError as exc:
        print(f"[error] database error: {type(exc).__name__}", file=sys.stderr)
        return 1
    finally:
        await close_database()

    print(f"[{outcome}] super_admin {user.email} on tenant {user.tenant_id}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
