"""The post-deploy rollout readback on real PostgreSQL.

One of the release-candidate PostgreSQL suites
(``asaas_release_candidate_postgres_suites`` in ``scripts/wish-gates.sh``,
run by CI); it needs ``POSTGRES_SMOKE_DATABASE_URL`` (a disposable loopback
cluster, as every PostgreSQL suite here). It migrates a fresh database with
this tree's migrations and runs the exact programs
``scripts/asaas_rollout_readback.py`` renders for its two windows (``--window
037`` before the 038-042 window, ``--window 042`` after it), as the cluster
superuser and as a throwaway non-superuser login role (its own password,
``SELECT`` through default privileges set before the migrations, as the
application role gets it): at 036 both judges stop on the revision and on
every legacy object of migration 037; at 037 the 037 judge accepts the table
and the 042 judge stops on the revision alone; at 042 the reverse; the reads
changed nothing; with row-level security on, the non-superuser is not exempt
(its rows are hidden), while the superuser sees them and only the RLS row
stops.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from scripts import asaas_migration_window as mw
from scripts import asaas_rollout_readback as rr
from tests.integration.test_asaas_billing_postgres import (
    _create_child,
    _drop_child,
    _run_alembic,
    _validated_admin_url,
)

pytestmark = pytest.mark.integration


@dataclass(frozen=True)
class _Database:
    superuser: URL  # the child database as the smoke URL's superuser
    application: URL  # the same database as a throwaway non-superuser login


@pytest.fixture
async def database() -> AsyncIterator[_Database]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if not raw_admin_url:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    superuser = admin_url.username
    assert superuser, "POSTGRES_SMOKE_DATABASE_URL must name its superuser role"
    admin: AsyncEngine = create_async_engine(
        admin_url, poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    suffix = uuid4().hex[:12]
    name = f"neurostack_asaas_smoke_{suffix}"
    role = f"rollout_readback_app_{suffix}"
    # token_hex is [0-9a-f] only: nothing to escape in the literal. Under
    # scram-sha-256 host auth (CI) a role without a password cannot log in.
    password = secrets.token_hex(24)
    async with admin.connect() as connection:
        await connection.exec_driver_sql(
            f"CREATE ROLE \"{role}\" LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{password}'"
        )
    created = False
    try:
        await _create_child(admin, name)
        created = True
        child = admin_url.set(database=name)
        engine = create_async_engine(
            child, poolclass=NullPool, isolation_level="AUTOCOMMIT"
        )
        try:
            async with engine.connect() as connection:
                await connection.exec_driver_sql(
                    f'GRANT USAGE ON SCHEMA public TO "{role}"'
                )
                # Before any migration: every table the migrations create
                # grants SELECT to the role, so 037's privilege preflight
                # (which compares with a probe table) agrees.
                await connection.exec_driver_sql(
                    f'ALTER DEFAULT PRIVILEGES FOR ROLE "{superuser}" '
                    f'IN SCHEMA public GRANT SELECT ON TABLES TO "{role}"'
                )
        finally:
            await engine.dispose()
        yield _Database(
            superuser=child,
            application=child.set(username=role, password=password),
        )
    finally:
        if created:
            await _drop_child(admin, name)
        async with admin.connect() as connection:
            await connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{role}"')
        await admin.dispose()


def _run(url: URL, window: str) -> tuple[int, list[str]]:
    completed = subprocess.run(
        [sys.executable, "-c", rr.read_program(rr.READ_WINDOWS[window])],
        cwd="/tmp",
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "DATABASE_URL": url.render_as_string(hide_password=False),
            "PYTHONDONTWRITEBYTECODE": "1",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.stderr == "", completed.stderr
    return completed.returncode, completed.stdout.splitlines()


async def _judged(url: URL, window: str) -> tuple[list[str], dict[str, Any]]:
    code, lines = await asyncio.to_thread(_run, url, window)
    assert code == 0, lines
    begin = json.loads(lines[0])
    assert begin["payload_sha256"] == rr.payload_sha256()
    assert (begin["window"], begin["phase"]) == (window, "rollout")
    return rr.judge(mw.parse_read_lines(lines), rr.READ_WINDOWS[window])


async def _execute(url: URL, *statements: str) -> None:
    engine = create_async_engine(url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            for statement in statements:
                await connection.exec_driver_sql(statement)
    finally:
        await engine.dispose()


async def _rollout_rows(url: URL) -> list[tuple[Any, ...]]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text(
                    "SELECT environment, state, topup_commercial_enabled, "
                    "rollout_revision, updated_at FROM public.billing_rollout_control "
                    "ORDER BY environment"
                )
            )
            return [tuple(row) for row in result]
    finally:
        await engine.dispose()


async def test_each_window_passes_only_on_its_own_revision(database: _Database) -> None:
    await _run_alembic(database.superuser, "upgrade", "036")
    for url in (database.superuser, database.application):
        for window in ("037", "042"):
            reasons, report = await _judged(url, window)
            assert report["alembic_version"] == ["036"]
            assert "read_alembic_version_mismatch" in reasons
            assert sum(r.startswith("legacy_table_present:") for r in reasons) == 4
            assert sum(r.startswith("legacy_function_present:") for r in reasons) == 10
            assert len(reasons) == 15, reasons

    for revision in ("037", "042"):
        await _run_alembic(database.superuser, "upgrade", revision)
        before = await _rollout_rows(database.superuser)
        for url in (database.superuser, database.application):
            for window in ("037", "042"):
                reasons, report = await _judged(url, window)
                assert report["alembic_version"] == [revision]
                if window != revision:
                    # The other window's read stops on the revision alone.
                    assert reasons == ["read_alembic_version_mismatch"], reasons
                    continue
                assert reasons == [], reasons
                facts = report["facts"]
                assert [row["state"] for row in facts["billing_rollout_control"]] == [
                    "disabled"
                ] * 4
                assert facts["legacy_tables_absent"] == 4
                assert facts["legacy_functions_absent"] == 10
                assert facts["billing_subscriptions_rows"] == 0
                assert facts["billing_checkout_sessions_rows"] == 0
                assert facts["row_level_security_on_read_tables"] == 0
        assert await _rollout_rows(database.superuser) == before


@pytest.mark.parametrize("window", ["037", "042"])
async def test_row_level_security_is_not_bypassed_for_the_application_role(
    database: _Database, window: str
) -> None:
    await _run_alembic(database.superuser, "upgrade", window)
    await _execute(
        database.superuser,
        "ALTER TABLE public.billing_rollout_control ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE public.billing_checkout_sessions ENABLE ROW LEVEL SECURITY",
    )
    reasons, report = await _judged(database.application, window)
    assert "read_checks_mismatch" in reasons
    assert "row_level_security_on_read_tables" in reasons
    assert "read_fact_missing:rollout_state_not_disabled:production" in reasons
    assert report["facts"]["row_level_security_on_read_tables"] == 2
    # The superuser bypasses RLS: it sees every row, and only the RLS row stops.
    reasons, report = await _judged(database.superuser, window)
    assert reasons == ["row_level_security_on_read_tables"]
    assert report["facts"]["row_level_security_on_read_tables"] == 2
