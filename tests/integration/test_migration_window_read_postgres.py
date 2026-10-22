"""The migration-window read program on real PostgreSQL.

One of the release-candidate PostgreSQL suites
(``asaas_release_candidate_postgres_suites`` in ``scripts/wish-gates.sh``,
run by CI); it needs ``POSTGRES_SMOKE_DATABASE_URL`` (a disposable loopback
cluster, as every PostgreSQL suite here). It proves what the read program's
contract rests on, independently of any spelling check: the session is read
only (``default_transaction_read_only=on`` plus a ``READ ONLY`` transaction),
so a write or DDL statement in the payload fails with SQLSTATE 25006 and
changes nothing, and a payload with a second statement fails with 42601
(one statement per prepared query) before anything runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from typing import Any, AsyncIterator
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from scripts import asaas_migration_window as mw
from tests.integration.test_asaas_billing_postgres import (
    _create_child,
    _drop_child,
    _validated_admin_url,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def database() -> AsyncIterator[URL]:
    raw_admin_url = os.getenv("POSTGRES_SMOKE_DATABASE_URL")
    if not raw_admin_url:
        pytest.skip("POSTGRES_SMOKE_DATABASE_URL is not configured")
    admin_url = _validated_admin_url(raw_admin_url)
    admin: AsyncEngine = create_async_engine(
        admin_url, poolclass=NullPool, isolation_level="AUTOCOMMIT"
    )
    name = f"neurostack_asaas_smoke_{uuid4().hex[:12]}"
    await _create_child(admin, name)
    child = admin_url.set(database=name)
    try:
        engine = create_async_engine(child, poolclass=NullPool)
        async with engine.begin() as connection:
            await connection.execute(
                text("CREATE TABLE public.alembic_version (version_num varchar(32))")
            )
            await connection.execute(
                text("INSERT INTO public.alembic_version VALUES ('036')")
            )
        await engine.dispose()
        yield child
    finally:
        await _drop_child(admin, name)
        await admin.dispose()


def _run(program: str, url: URL) -> tuple[int, list[dict[str, Any]]]:
    completed = subprocess.run(
        [sys.executable, "-c", program],
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
    lines = [json.loads(line) for line in completed.stdout.splitlines() if line]
    assert completed.stderr == "", completed.stderr
    return completed.returncode, lines


def _program(sql: str) -> str:
    raw = json.dumps(
        {"sql": sql, "version": mw.VERSION_SQL},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return mw.program_for_payload(mw.WINDOW_037, "post", raw)


async def _version_and_tables(url: URL) -> tuple[list[str], list[str]]:
    engine = create_async_engine(url, poolclass=NullPool)
    try:
        async with engine.connect() as connection:
            versions = (
                await connection.execute(
                    text("SELECT version_num FROM public.alembic_version")
                )
            ).scalars()
            tables = (
                await connection.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                        "ORDER BY 1"
                    )
                )
            ).scalars()
            return list(versions), list(tables)
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "sql",
    [
        "CREATE TABLE public.t8_probe (id int)",
        "UPDATE public.alembic_version SET version_num = 'x'",
        "DROP TABLE public.alembic_version",
        "INSERT INTO public.alembic_version VALUES ('x') RETURNING version_num",
    ],
)
async def test_a_write_in_the_payload_fails_25006_and_changes_nothing(
    sql: str, database: URL
) -> None:
    code, lines = await asyncio.to_thread(_run, _program(sql), database)
    assert code == 3
    assert lines[-1] == {
        "schema": mw.READ_LINE_SCHEMA,
        "seq": len(lines) - 1,
        "kind": "end",
        "status": "error",
        "error_class": "ReadOnlySQLTransactionError",
        "sqlstate": "25006",
    }
    assert await _version_and_tables(database) == (["036"], ["alembic_version"])


async def test_a_second_statement_fails_42601_before_anything_runs(
    database: URL,
) -> None:
    code, lines = await asyncio.to_thread(
        _run, _program("SELECT 1; DROP TABLE public.alembic_version"), database
    )
    assert code == 3
    assert lines[-1]["status"] == "error" and lines[-1]["sqlstate"] == "42601"
    assert await _version_and_tables(database) == (["036"], ["alembic_version"])


async def test_the_real_post_read_reads_the_revision(database: URL) -> None:
    program = mw.read_program(mw.WINDOW_036, mw.WINDOW_036.post_read)
    code, lines = await asyncio.to_thread(_run, program, database)
    assert code == 0
    assert [line["kind"] for line in lines] == ["begin", "version", "end"]
    assert lines[1]["values"] == ["036"]
    assert lines[2] == {
        "schema": mw.READ_LINE_SCHEMA,
        "seq": 2,
        "kind": "end",
        "status": "ok",
        "rows": 0,
    }


async def test_the_session_itself_is_read_only(database: URL) -> None:
    code, lines = await asyncio.to_thread(
        _run,
        _program("SELECT current_setting('default_transaction_read_only')"),
        database,
    )
    assert code == 0
    assert [line for line in lines if line["kind"] == "row"][0]["values"] == ["on"]
    code, lines = await asyncio.to_thread(
        _run, _program("SELECT current_setting('transaction_read_only')"), database
    )
    assert code == 0
    assert [line for line in lines if line["kind"] == "row"][0]["values"] == ["on"]
