"""Regression coverage for the pytest database safety boundary."""

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from src.models.tenant import Tenant
from src.repositories.database import get_db_engine, get_db_session
from src.repositories.database import init_database as collected_init_database

_CANONICAL_TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
_ROOT = Path(__file__).resolve().parents[1]
_THIS_TEST = Path(__file__).resolve()
_UNSAFE_DATABASE_URLS = (
    "postgresql+asyncpg://user:secret@localhost:5432/neurostack",
    "postgresql+asyncpg://user:secret@localhost:5432/test_neurostack",
    "sqlite+aiosqlite:///development.db",
    "sqlite+aiosqlite://user:secret@localhost/:memory:",
    "sqlite+aiosqlite://localhost/:memory:",
    "sqlite+aiosqlite:///:memory:?mode=memory",
)


def _subprocess_environment(database_url: str) -> dict[str, str]:
    environment = os.environ.copy()
    environment["PYTEST_DATABASE_URL"] = database_url
    core_path = str(_ROOT / "packages" / "neurostack-core")
    current_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        f"{core_path}{os.pathsep}{current_pythonpath}"
        if current_pythonpath
        else core_path
    )
    return environment


def _collect_with_database_url(database_url: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", str(_THIS_TEST)],
        cwd=_ROOT,
        env=_subprocess_environment(database_url),
        check=False,
        capture_output=True,
        text=True,
    )


def test_canonical_database_destination_collects_five_tests() -> None:
    result = _collect_with_database_url(_CANONICAL_TEST_DATABASE_URL)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "collected 5 items" in result.stdout
    assert "ImportError" not in result.stdout + result.stderr


def test_unsafe_database_destinations_fail_before_collection(
    database_url_validator: Callable[[str], str],
) -> None:
    for unsafe_url in _UNSAFE_DATABASE_URLS:
        with pytest.raises(pytest.UsageError):
            database_url_validator(unsafe_url)

        result = _collect_with_database_url(unsafe_url)
        output = result.stdout + result.stderr
        assert result.returncode == pytest.ExitCode.USAGE_ERROR, output
        assert "PYTEST_DATABASE_URL must be exactly" in output
        assert "ImportError" not in output


def test_in_process_pytest_main_restores_database_callable_identities() -> None:
    script = f"""
import pytest
import src.api.main  # load the application before repositories, like conftest
import src.repositories as repositories
from src.repositories import database

originals = (
    database.init_database,
    database.create_async_engine,
    repositories.init_database,
)
status = pytest.main(["--collect-only", "-q", {str(_THIS_TEST)!r}])
assert status == pytest.ExitCode.OK, status
assert database.init_database is originals[0]
assert database.create_async_engine is originals[1]
assert repositories.init_database is originals[2]
print("callable identities restored")
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=_ROOT,
        env=_subprocess_environment(_CANONICAL_TEST_DATABASE_URL),
        check=False,
        capture_output=True,
        text=True,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "callable identities restored" in output


@pytest.mark.asyncio
async def test_collected_direct_init_alias_rejects_runtime_database() -> None:
    runtime_url = "postgresql+asyncpg://user:secret@localhost:5432/neurostack"

    with pytest.raises(pytest.UsageError):
        await collected_init_database(database_url=runtime_url)


def test_committed_rows_do_not_survive_isolated_app_lifespans(
    test_app: object,
    test_database_url: str,
    lifespan_test_client_factory: Callable[[Any], Any],
) -> None:
    tenant_id = uuid4()

    async def seed_committed_tenant() -> None:
        session = get_db_session()
        try:
            session.add(
                Tenant(
                    id=tenant_id,
                    name="Isolation probe",
                    api_key_hash=f"isolation-{tenant_id}",
                    config={},
                )
            )
            await session.commit()
        finally:
            await session.close()

    async def tenant_exists() -> bool:
        session = get_db_session()
        try:
            result = await session.execute(
                select(Tenant.id).where(Tenant.id == tenant_id)
            )
            return result.scalar_one_or_none() is not None
        finally:
            await session.close()

    with lifespan_test_client_factory(test_app) as first_client:
        assert get_db_engine().url == make_url(test_database_url)
        first_client._submit(seed_committed_tenant())
        assert first_client._submit(tenant_exists()) is True

    with lifespan_test_client_factory(test_app) as second_client:
        assert get_db_engine().url == make_url(test_database_url)
        assert second_client._submit(tenant_exists()) is False

    # The second lifespan closes its engine; this remains safe if cleanup
    # behavior changes and keeps this regression locally self-contained.
    try:
        get_db_engine()
    except RuntimeError:
        return
    pytest.fail("application lifespan left the test database initialized")
