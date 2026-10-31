"""Operator requeue of exhausted ``billing.fact.v1`` deliveries (SQLite part).

Selection, idempotency, the audit row and the output contract run here on
SQLite (no row locks: ``FOR UPDATE SKIP LOCKED`` is a no-op).  The same
behaviour on migration-head PostgreSQL, with deliveries driven to ``blocked``
by the real worker and the reconciler, is
``tests/integration/test_billing_fact_requeue_postgres.py``.
"""

from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.models  # noqa: F401 - register every table for create_all
from src.models.base import Base
from src.models.billing import (
    BillingFactAck,
    BillingFactDelivery,
    BillingFactReconciliationAlert,
)
from src.ops import billing_fact_requeue as requeue
from src.repositories.database import _patch_jsonb_for_sqlite
from src.services.billing_outbox import (
    ACK_MISSING_DETAIL,
    ACK_SCHEMA_VERSION,
    RETRY_EXHAUSTED_DETAIL,
)

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "src" / "ops" / "billing_fact_requeue.py"
RUNBOOK = ROOT / "docs" / "runbooks" / "asaas-billing-reconciliation.md"
CLOCK = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def _clock() -> datetime:
    return CLOCK


@pytest.fixture
async def sessions(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    _patch_jsonb_for_sqlite()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'requeue.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class _World:
    exhausted: UUID
    divergent_ack: UUID
    rejected_ack: UUID
    no_evidence: UUID
    pending: UUID
    acknowledged: UUID


async def _delivery(
    sessions: async_sessionmaker[AsyncSession],
    *,
    state: str,
    attempts: int,
    age_minutes: int = 0,
    alerts: tuple[str, ...] = (),
    ack: str | None = None,
) -> UUID:
    """One delivery; the fact id is free (SQLite does not enforce the FK)."""

    outbox_id = uuid4()
    created = CLOCK - timedelta(days=1) + timedelta(minutes=age_minutes)
    async with sessions() as session, session.begin():
        session.add(
            BillingFactDelivery(
                id=uuid4(),
                outbox_event_id=outbox_id,
                event_id=outbox_id,
                state=state,
                attempt_count=attempts,
                last_attempt_at=created if attempts else None,
                created_at=created,
                updated_at=created,
            )
        )
        for detail in alerts:
            session.add(
                BillingFactReconciliationAlert(
                    id=uuid4(),
                    outbox_event_id=outbox_id,
                    kind="backlog",
                    detail_sha256=requeue.detail_digest(detail),
                    observed_at=created,
                )
            )
        if ack is not None:
            session.add(
                BillingFactAck(
                    id=uuid4(),
                    outbox_event_id=outbox_id,
                    event_id=outbox_id,
                    schema_version=ACK_SCHEMA_VERSION,
                    outcome=ack,
                    inbox_event_id=uuid4(),
                    fact_sha256="0" * 64,
                    bundle_digest="b" * 64,
                    deployment_digest="d" * 64,
                    effects_digest="e" * 64,
                    acknowledged_at=created,
                )
            )
    return outbox_id


async def _world(sessions: async_sessionmaker[AsyncSession]) -> _World:
    return _World(
        exhausted=await _delivery(
            sessions, state="blocked", attempts=3, alerts=(RETRY_EXHAUSTED_DETAIL,)
        ),
        # The reconciler blocks a delivery only once an ack exists.  The
        # exhaustion alert is there too: an ack can arrive after the cap.
        divergent_ack=await _delivery(
            sessions,
            state="blocked",
            attempts=3,
            alerts=(RETRY_EXHAUSTED_DETAIL,),
            ack="accepted",
        ),
        rejected_ack=await _delivery(
            sessions, state="blocked", attempts=1, ack="rejected"
        ),
        no_evidence=await _delivery(
            sessions, state="blocked", attempts=3, alerts=(ACK_MISSING_DETAIL,)
        ),
        pending=await _delivery(
            sessions, state="pending", attempts=0, alerts=(RETRY_EXHAUSTED_DETAIL,)
        ),
        acknowledged=await _delivery(
            sessions, state="acknowledged", attempts=1, ack="accepted"
        ),
    )


async def _snapshot(
    sessions: async_sessionmaker[AsyncSession],
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    async with sessions() as session:
        deliveries = [
            tuple(row)
            for row in await session.execute(
                select(
                    BillingFactDelivery.outbox_event_id,
                    BillingFactDelivery.state,
                    BillingFactDelivery.attempt_count,
                    BillingFactDelivery.last_attempt_at,
                ).order_by(BillingFactDelivery.outbox_event_id)
            )
        ]
        alerts = [
            tuple(row)
            for row in await session.execute(
                select(
                    BillingFactReconciliationAlert.id,
                    BillingFactReconciliationAlert.outbox_event_id,
                    BillingFactReconciliationAlert.kind,
                    BillingFactReconciliationAlert.detail_sha256,
                ).order_by(BillingFactReconciliationAlert.id)
            )
        ]
    return deliveries, alerts


async def _state(
    sessions: async_sessionmaker[AsyncSession], outbox_id: UUID
) -> tuple[str, int, datetime | None]:
    async with sessions() as session:
        row = (
            await session.execute(
                select(
                    BillingFactDelivery.state,
                    BillingFactDelivery.attempt_count,
                    BillingFactDelivery.last_attempt_at,
                ).where(BillingFactDelivery.outbox_event_id == outbox_id)
            )
        ).one()
    return str(row[0]), int(row[1]), row[2]


async def _requeue_rows(
    sessions: async_sessionmaker[AsyncSession],
) -> list[UUID | None]:
    async with sessions() as session:
        return list(
            await session.scalars(
                select(BillingFactReconciliationAlert.outbox_event_id).where(
                    BillingFactReconciliationAlert.kind == "backlog",
                    BillingFactReconciliationAlert.detail_sha256
                    == requeue.detail_digest(requeue.REQUEUE_DETAIL),
                )
            )
        )


def _counts(**values: int) -> dict[str, int]:
    return {**dict.fromkeys(requeue.COUNT_NAMES, 0), **values}


# ---------------------------------------------------------------------------
# Selection: only blocked + exhaustion evidence + no ack at all
# ---------------------------------------------------------------------------


async def test_plan_classifies_every_blocked_delivery_and_writes_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _world(sessions)
    before = await _snapshot(sessions)

    result = await requeue.run("plan", sessions, now=_clock)

    assert await _snapshot(sessions) == before
    assert result.action == "none"
    assert result.counts == _counts(
        eligible_before=1,
        eligible_remaining=1,
        blocked_with_ack=2,
        blocked_without_evidence=1,
    )
    assert result.verdict == "investigate"
    assert result.reasons == (
        "blocked_with_ack",
        "blocked_without_evidence",
        "eligible_remaining",
    )


async def test_requeue_reopens_only_the_exhausted_delivery_without_an_ack(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    world = await _world(sessions)
    untouched = {
        outbox_id: await _state(sessions, outbox_id)
        for outbox_id in (
            world.divergent_ack,
            world.rejected_ack,
            world.no_evidence,
            world.pending,
            world.acknowledged,
        )
    }

    result = await requeue.run("requeue", sessions, now=_clock)

    assert await _state(sessions, world.exhausted) == ("pending", 0, None)
    for outbox_id, state in untouched.items():
        assert await _state(sessions, outbox_id) == state
    assert await _requeue_rows(sessions) == [world.exhausted]
    assert result.action == "requeued"
    assert result.counts == _counts(
        eligible_before=1,
        requeued=1,
        blocked_with_ack=2,
        blocked_without_evidence=1,
    )
    # The ack-blocked and unexplained deliveries still need a person.
    assert result.verdict == "investigate"
    assert result.reasons == ("blocked_with_ack", "blocked_without_evidence")


async def test_the_audit_row_is_a_fixed_backlog_detail_at_the_run_clock(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _delivery(
        sessions, state="blocked", attempts=3, alerts=(RETRY_EXHAUSTED_DETAIL,)
    )

    await requeue.run("requeue", sessions, now=_clock)

    async with sessions() as session:
        (row,) = list(
            await session.scalars(
                select(BillingFactReconciliationAlert).where(
                    BillingFactReconciliationAlert.detail_sha256
                    == requeue.detail_digest("operator requeue")
                )
            )
        )
    assert row.outbox_event_id == outbox_id
    assert row.kind == "backlog"
    assert row.observed_at.replace(tzinfo=timezone.utc) == CLOCK


async def test_a_second_run_changes_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _world(sessions)
    first = await requeue.run("requeue", sessions, now=_clock)
    after_first = await _snapshot(sessions)

    second = await requeue.run("requeue", sessions, now=_clock)

    assert first.counts["requeued"] == 1
    assert await _snapshot(sessions) == after_first
    assert second.action == "none"
    assert second.counts == _counts(blocked_with_ack=2, blocked_without_evidence=1)


async def test_a_reexhausted_delivery_is_requeued_again_with_a_second_audit_row(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    """Each real reopen is evidence of its own; replays add none."""

    outbox_id = await _delivery(
        sessions, state="blocked", attempts=3, alerts=(RETRY_EXHAUSTED_DETAIL,)
    )
    await requeue.run("requeue", sessions, now=_clock)
    # The worker exhausts it again (its exhaustion alert is de-duplicated).
    async with sessions() as session, session.begin():
        delivery = await session.scalar(
            select(BillingFactDelivery).where(
                BillingFactDelivery.outbox_event_id == outbox_id
            )
        )
        assert delivery is not None
        delivery.state, delivery.attempt_count = "blocked", 3

    result = await requeue.run("requeue", sessions, now=_clock)

    assert result.counts["requeued"] == 1
    assert await _requeue_rows(sessions) == [outbox_id, outbox_id]


async def test_only_exhausted_deliveries_end_clean_and_plan_says_pending(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _delivery(
        sessions, state="blocked", attempts=3, alerts=(RETRY_EXHAUSTED_DETAIL,)
    )

    planned = await requeue.run("plan", sessions, now=_clock)
    done = await requeue.run("requeue", sessions, now=_clock)

    assert (planned.verdict, planned.exit_code) == ("pending", 10)
    assert planned.reasons == ("eligible_remaining",)
    assert (done.verdict, done.exit_code) == ("clean", 0)
    assert done.reasons == ()


async def test_limit_reopens_the_oldest_and_reports_the_rest_pending(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    ids = [
        await _delivery(
            sessions,
            state="blocked",
            attempts=3,
            age_minutes=minutes,
            alerts=(RETRY_EXHAUSTED_DETAIL,),
        )
        for minutes in (30, 10, 20)
    ]

    result = await requeue.run("requeue", sessions, limit=2, now=_clock)

    assert sorted(await _requeue_rows(sessions)) == sorted([ids[1], ids[2]])
    assert (await _state(sessions, ids[0]))[0] == "blocked"
    assert result.counts == _counts(eligible_before=3, requeued=2, eligible_remaining=1)
    assert (result.verdict, result.exit_code) == ("pending", 10)


async def test_nothing_blocked_is_clean(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    await _delivery(sessions, state="pending", attempts=0)

    result = await requeue.run("requeue", sessions, now=_clock)

    assert (result.verdict, result.action, result.counts) == (
        "clean",
        "none",
        _counts(),
    )
    assert await _requeue_rows(sessions) == []


async def test_a_naive_clock_writes_nothing(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    outbox_id = await _delivery(
        sessions, state="blocked", attempts=3, alerts=(RETRY_EXHAUSTED_DETAIL,)
    )

    with pytest.raises(ValueError, match="timezone-aware"):
        await requeue.run("requeue", sessions, now=lambda: CLOCK.replace(tzinfo=None))

    assert (await _state(sessions, outbox_id))[0] == "blocked"


@pytest.mark.parametrize("limit", [0, requeue.MAX_LIMIT + 1])
async def test_run_refuses_an_out_of_bounds_limit(
    sessions: async_sessionmaker[AsyncSession], limit: int
) -> None:
    with pytest.raises(ValueError, match="limit"):
        await requeue.run("requeue", sessions, limit=limit)


# ---------------------------------------------------------------------------
# Output contract and entry point
# ---------------------------------------------------------------------------


def _main(argv: list[str], **kwargs: Any) -> tuple[int, dict[str, Any], dict[str, Any]]:
    out, err = io.StringIO(), io.StringIO()
    code = requeue.main(argv, stdout=out, stderr=err, **kwargs)
    (line,) = out.getvalue().splitlines()
    (log,) = err.getvalue().splitlines()
    return code, json.loads(line), json.loads(log)


async def _seed_one_exhausted(tmp_path: Path) -> async_sessionmaker[AsyncSession]:
    _patch_jsonb_for_sqlite()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'main.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await _delivery(
        factory, state="blocked", attempts=3, alerts=(RETRY_EXHAUSTED_DETAIL,)
    )
    await engine.dispose()
    return factory


def test_main_prints_one_closed_document_and_one_log_line(tmp_path: Path) -> None:
    import asyncio

    factory = asyncio.run(_seed_one_exhausted(tmp_path))

    code, document, log = _main(["requeue", "--limit", "5"], sessions=factory)

    assert code == 0
    assert document == {
        "schema": "neurostack.billing_fact_requeue.v1",
        "command": "requeue",
        "verdict": "clean",
        "action": "requeued",
        "reasons": [],
        "counts": _counts(eligible_before=1, requeued=1),
        "limit": 5,
    }
    assert {key: log[key] for key in document} == document
    assert log["event"] == "billing_fact_requeue"
    assert log["logger"] == "src.ops.billing_fact_requeue"
    assert log["level"] == "info"
    assert log["error_class"] is None


def test_main_defaults_to_a_read_only_plan(tmp_path: Path) -> None:
    import asyncio

    factory = asyncio.run(_seed_one_exhausted(tmp_path))

    code, document, _log = _main([], sessions=factory)
    again, _document, _log = _main([], sessions=factory)

    assert (code, again) == (10, 10)  # the plan never reopened it
    assert document["command"] == "plan"
    assert document["counts"] == _counts(eligible_before=1, eligible_remaining=1)
    assert document["limit"] == requeue.DEFAULT_LIMIT


@pytest.mark.parametrize(
    "argv",
    [
        ["bogus"],
        ["plan", "--limit", "5"],
        ["requeue", "--limit", "0"],
        ["requeue", "--limit", "501"],
        ["requeue", "--limit", "x"],
        ["requeue", "--limit=+5"],
        ["requeue", "--lim", "5"],
        ["--help"],
    ],
)
def test_usage_errors_are_unconfigured_and_touch_nothing(argv: list[str]) -> None:
    def never() -> AsyncSession:
        raise AssertionError("no session for a usage error")

    code, document, _log = _main(argv, sessions=never)

    assert code == 30
    assert document["verdict"] == "unconfigured"
    assert document["reasons"] == ["usage"]
    assert document["command"] is None


def test_a_database_failure_is_an_error_without_its_message() -> None:
    def broken() -> AsyncSession:
        raise OperationalError("SELECT canary-statement", {}, Exception("canary-pw"))

    out, err = io.StringIO(), io.StringIO()
    code = requeue.main(["requeue"], sessions=broken, stdout=out, stderr=err)

    document = json.loads(out.getvalue())
    log = json.loads(err.getvalue())
    assert code == 40
    assert document["verdict"] == "error"
    assert document["reasons"] == ["database_error"]
    assert log["error_class"] == "OperationalError"
    assert "canary" not in out.getvalue() + err.getvalue()


def test_an_unexpected_failure_is_an_error_without_its_message() -> None:
    def broken() -> AsyncSession:
        raise RuntimeError("canary-detail")

    out, err = io.StringIO(), io.StringIO()
    code = requeue.main(["plan"], sessions=broken, stdout=out, stderr=err)

    assert code == 40
    assert json.loads(out.getvalue())["reasons"] == ["unexpected"]
    assert "canary" not in out.getvalue() + err.getvalue()


def test_the_result_contract_is_closed() -> None:
    with pytest.raises(ValueError):
        requeue.RunResult(command="plan", verdict="ok")
    with pytest.raises(ValueError):
        requeue.RunResult(command="drop", verdict="clean")
    with pytest.raises(ValueError):
        requeue.RunResult(command="plan", verdict="clean", reasons=("free text",))
    with pytest.raises(ValueError):
        requeue.RunResult(command="plan", verdict="clean", counts={"requeued": 1})
    with pytest.raises(ValueError):
        requeue.RunResult(command="plan", verdict="clean", counts=_counts(requeued=-1))
    assert requeue.EXIT_CODES["clean"] == 0
    assert all(
        code for verdict, code in requeue.EXIT_CODES.items() if verdict != "clean"
    )


def _subprocess(
    tmp_path: Path,
    argv: list[str],
    env: dict[str, str],
    *,
    dotenv: str | None = None,
    unset: tuple[str, ...] = (),
) -> Any:
    """``python -m src.ops.billing_fact_requeue`` with no PYTHONPATH or ``.env``.

    ``dotenv`` plants a ``.env`` in the working directory on purpose;
    ``unset`` drops base variables so only that file could supply them.
    """

    workdir = tmp_path / "app"
    workdir.mkdir()
    (workdir / "src").symlink_to(ROOT / "src", target_is_directory=True)
    if dotenv is not None:
        (workdir / ".env").write_text(dotenv, encoding="utf-8")
    base = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "ENVIRONMENT": "staging",
        "SECRET_KEY": "s" * 48,
    }
    merged = {**base, **env}
    for name in unset:
        merged.pop(name)
    return subprocess.run(
        [sys.executable, "-m", "src.ops.billing_fact_requeue", *argv],
        cwd=workdir,
        env=merged,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_entry_point_runs_as_a_module_against_database_url(tmp_path: Path) -> None:
    import asyncio

    asyncio.run(_seed_one_exhausted(tmp_path))
    url = f"sqlite+aiosqlite:///{tmp_path / 'main.db'}"

    planned = _subprocess(tmp_path, ["plan"], {"DATABASE_URL": url})

    assert planned.returncode == 10, planned.stderr[-2000:]
    (line,) = planned.stdout.splitlines()
    document = json.loads(line)
    assert document["counts"]["eligible_before"] == 1
    assert len(planned.stderr.splitlines()) == 1
    assert "main.db" not in planned.stdout + planned.stderr


def test_entry_point_ignores_a_dotenv_in_the_working_directory(
    tmp_path: Path,
) -> None:
    """Only the process environment configures the run, never ``.env``.

    The planted ``.env`` holds another ``DATABASE_URL`` (a seeded canary
    database) and the ``SECRET_KEY`` the environment lacks.  Read from the
    environment alone the settings are invalid (the default key is refused in
    staging), so the run stops before any engine exists; a ``Settings()``
    that read the file would plan against the canary database instead.
    """

    import asyncio

    asyncio.run(_seed_one_exhausted(tmp_path))
    canary = f"sqlite+aiosqlite:///{tmp_path / 'main.db'}"

    result = _subprocess(
        tmp_path,
        ["plan"],
        # Never the default ``localhost:5434`` if the key check is relaxed.
        {"DATABASE_URL": f"sqlite+aiosqlite:///{tmp_path / 'absent' / 'never.db'}"},
        dotenv=f"DATABASE_URL={canary}\nSECRET_KEY={'d' * 48}\n",
        unset=("SECRET_KEY",),
    )

    assert result.returncode == 30, result.stderr[-2000:]
    (line,) = result.stdout.splitlines()
    document = json.loads(line)
    assert document["verdict"] == "unconfigured"
    assert document["reasons"] == ["settings_invalid:secret_key"]
    assert document["counts"] == _counts()
    assert len(result.stderr.splitlines()) == 1
    assert "main.db" not in result.stdout + result.stderr


def test_invalid_settings_are_unconfigured_with_the_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Settings`` raising in ``main`` is exit 30, never an ``error``."""

    monkeypatch.setenv("SECRET_KEY", "short-canary")
    out, err = io.StringIO(), io.StringIO()

    code = requeue.main(["requeue"], stdout=out, stderr=err)

    document = json.loads(out.getvalue())
    assert code == 30
    assert document["verdict"] == "unconfigured"
    assert document["reasons"] == ["settings_invalid:secret_key"]
    assert json.loads(err.getvalue())["error_class"] is None
    assert "short-canary" not in out.getvalue() + err.getvalue()


def test_the_engine_hides_statement_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database error text never carries bound values (ids, timestamps)."""

    seen: dict[str, Any] = {}
    real_engine = requeue.create_async_engine

    def fake_engine(url: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return real_engine("sqlite+aiosqlite://")

    async def fake_run(command: str, sessions: Any, *, limit: int) -> Any:
        return requeue.RunResult(command=command, verdict="clean", limit=limit)

    monkeypatch.setattr(requeue, "create_async_engine", fake_engine)
    monkeypatch.setattr(requeue, "run", fake_run)
    settings = requeue.Settings(_env_file=None)  # type: ignore[call-arg]

    code = requeue.main(
        ["plan"], settings=settings, stdout=io.StringIO(), stderr=io.StringIO()
    )

    assert code == 0
    assert seen.get("hide_parameters") is True


# ---------------------------------------------------------------------------
# Boundaries
# ---------------------------------------------------------------------------


def test_the_module_never_imports_credit_code() -> None:
    """A producer module (``tests/models/test_credit_models.py``)."""

    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    assert names
    assert not [name for name in names if ".credit" in name or "ledger" in name]


def test_no_http_route_reaches_the_requeue() -> None:
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "src").rglob("*.py")
        if path != MODULE and "billing_fact_requeue" in path.read_text("utf-8")
    ]
    assert offenders == []


def test_the_runbook_covers_the_real_transport_and_the_requeue() -> None:
    runbook = RUNBOOK.read_text(encoding="utf-8")

    assert "has no transport" not in runbook
    assert "no requeue path" not in runbook
    for needle in (
        "handle_billing_fact",
        "BILLING_FACT_TRANSPORT_ENABLED",
        "python -m src.ops.billing_fact_requeue plan",
        "python -m src.ops.billing_fact_requeue requeue",
        f"'{requeue.REQUEUE_DETAIL}'",
        "blocked_with_ack",
        "blocked_without_evidence",
        "no actor column",
    ):
        assert needle in runbook, needle
