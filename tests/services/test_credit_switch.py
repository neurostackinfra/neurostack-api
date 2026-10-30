"""Credit dominance switch (neurostack-credit-system Group 1b).

Pins, without PostgreSQL (``tests/integration/test_credit_schema_dominance_
postgres.py`` repeats the matrix on the real app against 037 and 038):

* the switch matrix: config off => off with no database read; config on and
  the schema at 037 => off; at 038 (or later in the image chain) => on; a
  read error, a missing table, two rows, a revision outside the image chain,
  an unreadable chain or a timeout => off; the background refresh reflects
  an upgrade and a downgrade within one interval;
* the verdict is captured once per request at entry: flipping it in the
  middle of a request does not change that request's path, and no header,
  query or body turns it on;
* the guards: ``credit_router``/``CreditSwitchRoute`` answer the stable
  ``503 {"code": "credit_unavailable"}`` before the body is parsed or any
  dependency (database session included) runs, ``require_credit_switch``
  never yields a 500, the worker tick claims nothing and the UI panel is
  ``None`` while off;
* metrics: with the switch off no ``neurostack_credit_*`` family has a
  ``_total`` above zero and ``neurostack_credit_switch_effective`` is 0;
* the lifespan reads once at start-up, starts the refresher only with the
  config on, cancels it and turns the verdict off at shutdown;
* the modules import, and read the image chain, in a tree without ``tests/``.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, Depends, FastAPI, Request
from httpx import ASGITransport, AsyncClient
from prometheus_client import REGISTRY
from pydantic import BaseModel
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from src.api.middleware.tenant_identity import TenantIdentityMiddleware
from src.config.settings import settings
from src.services import credit_switch
from src.services.credit_switch import (
    CREDIT_SCHEMA_MIN_REVISION,
    CREDIT_SWITCH_STATE_KEY,
    IMAGE_VERSIONS_DIR,
    CreditSwitch,
    CreditSwitchReading,
    CreditSwitchRoute,
    CreditUnavailableError,
    ImageMigrationChainError,
    capture_credit_switch,
    credit_router,
    credit_switch_effective,
    credit_switch_reading,
    credit_unavailable_exception_handler,
    evaluate_schema_revision,
    load_credit_panel,
    load_image_migration_chain,
    refresh_credit_switch,
    request_credit_switch,
    require_credit_switch,
    run_credit_switch_refresher,
    run_credit_worker_tick,
    start_credit_switch_refresher,
    stop_credit_switch,
)
from tests.alembic_graph import current_head, migration_revisions

_ON = CreditSwitchReading(True, "schema_ready", CREDIT_SCHEMA_MIN_REVISION)
_OFF = CreditSwitchReading(False, "config_disabled")
_UNAVAILABLE = {"code": "credit_unavailable"}


@pytest.fixture(autouse=True)
def _restore_verdict() -> Iterator[None]:
    """Every test leaves the process verdict exactly as it found it."""

    before = credit_switch._SWITCH.reading()
    try:
        yield
    finally:
        credit_switch._SWITCH.publish(before)


def _publish(effective: bool) -> None:
    credit_switch._SWITCH.publish(_ON if effective else _OFF)


# ---------------------------------------------------------------------------
# The image's migration chain.
# ---------------------------------------------------------------------------


def _expected_chain() -> tuple[str, ...]:
    parents = dict(migration_revisions().values())
    children = {down: revision for revision, down in parents.items() if down}
    chain = [next(revision for revision, down in parents.items() if down is None)]
    while chain[-1] in children:
        chain.append(children[chain[-1]])
    return tuple(chain)


def test_the_image_chain_is_this_repository_s_linear_chain() -> None:
    chain = load_image_migration_chain()

    assert IMAGE_VERSIONS_DIR == Path(__file__).resolve().parents[2] / "alembic" / (
        "versions"
    )
    assert chain == _expected_chain()
    assert chain[0] == "001"
    assert chain[-1] == current_head()
    # 038/039/040 are the additive credit schema, inventory and copy chain.
    assert chain.index("037") + 3 == chain.index(CREDIT_SCHEMA_MIN_REVISION)


def test_the_minimum_is_the_highest_credit_revision_of_the_wish() -> None:
    credit_revisions = [
        revision
        for name, (revision, _down) in migration_revisions().items()
        if "_credit_" in name
    ]
    assert credit_revisions
    chain = load_image_migration_chain()
    assert CREDIT_SCHEMA_MIN_REVISION == max(credit_revisions, key=chain.index)


def _script(revision: str, down: str | None) -> str:
    return f'revision: str = "{revision}"\ndown_revision = {down!r}\n'


@pytest.mark.parametrize(
    ("scripts", "reason"),
    [
        ({}, "no migration script"),
        ({"a.py": _script("001", None), "b.py": _script("001", "001")}, "duplicate"),
        (
            {
                "a.py": _script("001", None),
                "b.py": _script("002", "001"),
                "c.py": _script("003", "001"),
            },
            "more than one migration revises",
        ),
        ({"a.py": _script("001", None), "b.py": _script("002", "999")}, "unknown"),
        ({"a.py": _script("001", None), "b.py": _script("002", None)}, "one root"),
        (
            {"a.py": _script("001", None), "b.py": 'revision = "002"\n'},
            "not found",
        ),
        (
            {
                "a.py": _script("001", None),
                "b.py": 'revision = "002"\ndown_revision = ("001", "x")\n',
            },
            "not a string literal",
        ),
        ({"a.py": "revision = (\n"}, "unreadable"),
        (
            {
                "a.py": _script("001", None),
                "b.py": _script("002", "003"),
                "c.py": _script("003", "002"),
            },
            "outside the linear chain",
        ),
    ],
)
def test_a_chain_that_is_not_one_readable_line_is_refused(
    tmp_path: Path, scripts: dict[str, str], reason: str
) -> None:
    for name, source in scripts.items():
        (tmp_path / name).write_text(source, encoding="utf-8")

    with pytest.raises(ImageMigrationChainError, match=reason):
        load_image_migration_chain(tmp_path)


# ---------------------------------------------------------------------------
# The verdict for alembic_version rows.
# ---------------------------------------------------------------------------

_CHAIN = ("001", "036", "037", "038", "039", "040")


@pytest.mark.parametrize(
    ("rows", "effective", "reason"),
    [
        (("038",), False, "schema_below_minimum"),
        (("039",), False, "schema_below_minimum"),
        (("040",), True, "schema_ready"),
        (("037",), False, "schema_below_minimum"),
        (("001",), False, "schema_below_minimum"),
        ((), False, "version_row_count"),
        (("038", "037"), False, "version_row_count"),
        (("038", "038"), False, "version_row_count"),
        (("999",), False, "revision_outside_image_chain"),
        (("038 ",), False, "revision_outside_image_chain"),
    ],
)
def test_schema_verdict_matrix(
    rows: tuple[str, ...], effective: bool, reason: str
) -> None:
    reading = evaluate_schema_revision(rows, _CHAIN, "040")
    assert (reading.effective, reading.reason) == (effective, reason)


def test_a_minimum_outside_the_image_chain_is_off() -> None:
    reading = evaluate_schema_revision(("038",), ("001", "038"), "040")
    assert reading == CreditSwitchReading(False, "revision_outside_image_chain", "038")


# ---------------------------------------------------------------------------
# Refresh against a database.
# ---------------------------------------------------------------------------


@dataclass
class _VersionDb:
    factory: async_sessionmaker[AsyncSession]
    statements: list[str]

    async def rows(self, *revisions: str) -> None:
        async with self.factory() as session:
            await session.execute(text("DELETE FROM alembic_version"))
            for revision in revisions:
                await session.execute(
                    text("INSERT INTO alembic_version VALUES (:revision)"),
                    {"revision": revision},
                )
            await session.commit()
        self.statements.clear()

    async def drop(self) -> None:
        async with self.factory() as session:
            await session.execute(text("DROP TABLE alembic_version"))
            await session.commit()
        self.statements.clear()


@pytest.fixture
async def version_db() -> AsyncIterator[_VersionDb]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    async with engine.begin() as connection:
        await connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        )
    try:
        yield _VersionDb(
            async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False),
            statements,
        )
    finally:
        await engine.dispose()


def _never_called() -> AsyncSession:
    raise AssertionError("the database must not be read")


async def test_config_off_is_off_without_any_database_read() -> None:
    _publish(True)

    reading = await refresh_credit_switch(enabled=False, session_factory=_never_called)

    assert reading == CreditSwitchReading(False, "config_disabled")
    assert credit_switch_effective() is False


async def test_config_off_by_default_settings_is_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "credit_hot_path_enabled", False)
    _publish(True)

    reading = await refresh_credit_switch(session_factory=_never_called)

    assert reading.reason == "config_disabled"
    assert credit_switch_effective() is False


@pytest.mark.parametrize(
    ("enabled", "rows", "effective", "reason"),
    [
        (False, ("040",), False, "config_disabled"),
        (True, ("037",), False, "schema_below_minimum"),
        # Group 4b: 040 is the minimum; 038/039 alone are below it.
        (True, ("038",), False, "schema_below_minimum"),
        (True, ("039",), False, "schema_below_minimum"),
        (True, ("040",), True, "schema_ready"),
        (True, (), False, "version_row_count"),
        (True, ("038", "037"), False, "version_row_count"),
        (True, ("deadbeef",), False, "revision_outside_image_chain"),
    ],
)
async def test_the_switch_matrix(
    version_db: _VersionDb,
    enabled: bool,
    rows: tuple[str, ...],
    effective: bool,
    reason: str,
) -> None:
    await version_db.rows(*rows)

    reading = await refresh_credit_switch(
        enabled=enabled, session_factory=version_db.factory
    )

    assert (reading.effective, reading.reason) == (effective, reason)
    assert credit_switch_effective() is effective
    assert credit_switch_reading() == reading
    expected_sql = ["SELECT version_num FROM alembic_version"] if enabled else []
    assert version_db.statements == expected_sql


async def test_the_current_head_is_on(version_db: _VersionDb) -> None:
    await version_db.rows(current_head())

    reading = await refresh_credit_switch(
        enabled=True, session_factory=version_db.factory
    )

    assert reading == CreditSwitchReading(True, "schema_ready", current_head())


async def test_a_missing_version_table_is_off(version_db: _VersionDb) -> None:
    await version_db.drop()
    _publish(True)

    reading = await refresh_credit_switch(
        enabled=True, session_factory=version_db.factory
    )

    assert reading == CreditSwitchReading(False, "read_error")
    assert credit_switch_effective() is False


async def test_a_read_error_is_off() -> None:
    def broken() -> AsyncSession:
        raise RuntimeError("Database not initialized. Call init_database() first.")

    _publish(True)

    reading = await refresh_credit_switch(enabled=True, session_factory=broken)

    assert reading == CreditSwitchReading(False, "read_error")
    assert credit_switch_effective() is False


async def test_an_uninitialized_pool_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.repositories import database

    monkeypatch.setattr(database, "_session_factory", None)
    _publish(True)

    reading = await refresh_credit_switch(enabled=True)

    assert reading == CreditSwitchReading(False, "read_error")


async def test_an_unreadable_image_chain_is_off_without_reading(
    tmp_path: Path,
) -> None:
    _publish(True)

    reading = await refresh_credit_switch(
        enabled=True, session_factory=_never_called, versions_dir=tmp_path
    )

    assert reading == CreditSwitchReading(False, "image_chain_unavailable")


class _HangingConnection:
    def __init__(self) -> None:
        self.invalidated = False

    async def execute(self, _statement: Any) -> Any:
        await asyncio.sleep(30)

    async def invalidate(self) -> None:
        self.invalidated = True


class _HangingSession:
    def __init__(self, connection: _HangingConnection | None) -> None:
        self._connection = connection
        self.closed = False

    async def connection(self) -> _HangingConnection:
        if self._connection is None:
            await asyncio.sleep(30)  # waiting for a pool slot
        assert self._connection is not None
        return self._connection

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("stuck_in", ["pool_checkout", "statement"])
async def test_a_read_over_budget_is_off_and_never_returns_to_the_pool(
    stuck_in: str,
) -> None:
    connection = None if stuck_in == "pool_checkout" else _HangingConnection()
    session = _HangingSession(connection)
    _publish(True)

    started = asyncio.get_running_loop().time()
    reading = await refresh_credit_switch(
        enabled=True,
        session_factory=lambda: session,  # type: ignore[arg-type,return-value]
        timeout_seconds=0.05,
    )

    assert asyncio.get_running_loop().time() - started < 5
    assert reading == CreditSwitchReading(False, "read_error")
    assert session.closed
    if connection is not None:
        assert connection.invalidated


async def test_refresh_reflects_an_upgrade_and_a_downgrade(
    version_db: _VersionDb,
) -> None:
    await version_db.rows("037")
    assert not (
        await refresh_credit_switch(enabled=True, session_factory=version_db.factory)
    ).effective

    await version_db.rows("040")
    assert (
        await refresh_credit_switch(enabled=True, session_factory=version_db.factory)
    ).effective

    await version_db.rows("037")
    assert not (
        await refresh_credit_switch(enabled=True, session_factory=version_db.factory)
    ).effective


async def _wait_for(effective: bool, deadline_seconds: float) -> float:
    loop = asyncio.get_running_loop()
    started = loop.time()
    while credit_switch_effective() is not effective:
        if loop.time() - started > deadline_seconds:
            raise AssertionError(f"switch did not become {effective}")
        await asyncio.sleep(0.005)
    return loop.time() - started


async def test_the_background_refresher_follows_the_schema_within_an_interval(
    version_db: _VersionDb,
) -> None:
    interval = 0.05
    await version_db.rows("037")
    await refresh_credit_switch(enabled=True, session_factory=version_db.factory)

    async def refresh() -> CreditSwitchReading:
        return await refresh_credit_switch(
            enabled=True, session_factory=version_db.factory
        )

    task = asyncio.create_task(run_credit_switch_refresher(interval, refresh=refresh))
    try:
        assert credit_switch_effective() is False
        await version_db.rows("040")
        assert await _wait_for(True, 2.0) <= 2 * interval + 1.0
        await version_db.rows("037")
        assert await _wait_for(False, 2.0) <= 2 * interval + 1.0
    finally:
        await stop_credit_switch(task)

    assert task.cancelled()
    assert credit_switch_reading() == CreditSwitchReading(False, "stopped")


async def test_the_refresher_survives_a_failing_refresh_and_fails_closed() -> None:
    calls: list[int] = []

    async def refresh() -> CreditSwitchReading:
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        reading = _ON
        credit_switch._SWITCH.publish(reading)
        return reading

    _publish(True)
    task = asyncio.create_task(run_credit_switch_refresher(0.0, refresh=refresh))
    try:
        # Bounded: a refresher that lost its containment dies on the first
        # failure, and this must fail rather than spin forever.
        async with asyncio.timeout(5):
            while len(calls) < 2:
                await asyncio.sleep(0.001)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(calls) >= 2


async def test_a_failing_refresh_publishes_off() -> None:
    async def refresh() -> CreditSwitchReading:
        raise RuntimeError("boom")

    async def sleep(_seconds: float) -> None:
        if credit_switch_effective() is False:
            raise asyncio.CancelledError
        await asyncio.sleep(0)

    _publish(True)
    with pytest.raises(asyncio.CancelledError):
        await run_credit_switch_refresher(1.0, refresh=refresh, sleep=sleep)
    assert credit_switch_reading() == CreditSwitchReading(False, "read_error")


def test_no_refresher_starts_with_the_config_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "credit_hot_path_enabled", False)
    assert start_credit_switch_refresher() is None


async def test_the_refresher_starts_with_the_config_on_and_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "credit_hot_path_enabled", True)
    monkeypatch.setattr(settings, "credit_switch_refresh_seconds", 300)

    task = start_credit_switch_refresher()

    assert task is not None
    assert task.get_name() == "credit-switch-refresher"
    await stop_credit_switch(task)
    assert task.cancelled()
    assert credit_switch_effective() is False


async def test_stopping_a_refresher_that_failed_does_not_raise() -> None:
    async def failed() -> None:
        raise RuntimeError("boom")

    task: asyncio.Task[None] = asyncio.create_task(failed())
    await asyncio.sleep(0)
    _publish(True)

    await stop_credit_switch(task)

    assert credit_switch_reading() == CreditSwitchReading(False, "stopped")


def test_the_settings_defaults_and_bounds() -> None:
    from src.config.settings import Settings

    assert Settings.model_fields["credit_hot_path_enabled"].default is False
    assert Settings.model_fields["credit_switch_refresh_seconds"].default == 60
    for invalid in (0, 301):
        with pytest.raises(ValueError):
            Settings(credit_switch_refresh_seconds=invalid)


# ---------------------------------------------------------------------------
# Synchronous reads fail closed.
# ---------------------------------------------------------------------------


def test_an_unreadable_verdict_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_self: CreditSwitch) -> bool:
        raise RuntimeError("boom")

    monkeypatch.setattr(CreditSwitch, "effective", broken)
    assert credit_switch_effective() is False


def test_only_a_literal_true_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(CreditSwitch, "effective", lambda _self: 1)
    assert credit_switch_effective() is False


# ---------------------------------------------------------------------------
# Captured once per request, at entry.
# ---------------------------------------------------------------------------


def test_the_first_capture_wins() -> None:
    _publish(True)
    scope: dict[str, Any] = {"type": "http"}

    assert capture_credit_switch(scope) is True
    _publish(False)
    assert capture_credit_switch(scope) is True
    assert scope["state"][CREDIT_SWITCH_STATE_KEY] is True
    assert capture_credit_switch({"type": "http"}) is False


def test_a_broken_scope_is_captured_as_off() -> None:
    _publish(True)
    assert capture_credit_switch({"type": "http", "state": None}) is False


def _probe_app() -> FastAPI:
    app = FastAPI()

    @app.api_route("/probe", methods=["GET", "POST"])
    async def probe(request: Request) -> dict[str, Any]:
        at_entry = request_credit_switch(request)
        # The process verdict flips while this request is in flight.
        _publish(not at_entry)
        return {
            "entry": at_entry,
            "later": request_credit_switch(request),
            "state": getattr(request.state, CREDIT_SWITCH_STATE_KEY),
            "process_now": credit_switch_effective(),
        }

    app.add_middleware(TenantIdentityMiddleware)
    return app


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize("effective", [True, False])
async def test_a_flip_mid_request_does_not_change_that_request(
    method: str, effective: bool
) -> None:
    _publish(effective)
    async with AsyncClient(
        transport=ASGITransport(app=_probe_app()), base_url="http://test"
    ) as client:
        first = await client.request(method, "/probe")
        second = await client.request(method, "/probe")

    assert first.json() == {
        "entry": effective,
        "later": effective,
        "state": effective,
        "process_now": not effective,
    }
    # The next request captures the new verdict at its own entry.
    assert second.json()["entry"] is (not effective)


async def test_no_header_query_or_body_turns_the_switch_on() -> None:
    _publish(False)
    async with AsyncClient(
        transport=ASGITransport(app=_probe_app()), base_url="http://test"
    ) as client:
        response = await client.post(
            "/probe?credit_hot_path_enabled=true&credit_switch_effective=1",
            json={"credit_hot_path_enabled": True, CREDIT_SWITCH_STATE_KEY: True},
            headers={
                "X-Credit-Hot-Path-Enabled": "true",
                "X-Credit-Switch-Effective": "1",
                "Cookie": "credit_hot_path_enabled=true",
            },
        )

    assert response.json()["entry"] is False


async def test_non_http_scopes_are_not_captured() -> None:
    seen: list[dict[str, Any]] = []

    async def inner(scope: Any, _receive: Any, _send: Any) -> None:
        seen.append(dict(scope))

    _publish(True)
    middleware = TenantIdentityMiddleware(inner)
    await middleware({"type": "lifespan"}, None, None)  # type: ignore[arg-type]

    assert seen == [{"type": "lifespan"}]


# ---------------------------------------------------------------------------
# Guards.
# ---------------------------------------------------------------------------


class _Body(BaseModel):
    amount: int


def _guarded_app(dependency_calls: list[str], *, with_handler: bool = True) -> FastAPI:
    app = FastAPI()
    if with_handler:  # as create_app registers it
        app.add_exception_handler(
            CreditUnavailableError, credit_unavailable_exception_handler
        )

    async def session_dependency() -> AsyncIterator[str]:
        dependency_calls.append("opened")
        yield "session"

    router = credit_router()

    @router.post("/balance")
    async def post_balance(
        body: _Body, session: str = Depends(session_dependency)
    ) -> dict[str, Any]:
        return {"ok": True, "amount": body.amount, "session": session}

    @router.get("/balance")
    async def get_balance(session: str = Depends(session_dependency)) -> dict[str, Any]:
        return {"ok": True, "session": session}

    plain = APIRouter()

    @plain.get("/plain", dependencies=[Depends(require_credit_switch)])
    async def plain_route(session: str = Depends(session_dependency)) -> dict[str, Any]:
        return {"ok": True}

    app.include_router(router, prefix="/v1/credits")
    app.include_router(plain)
    app.add_middleware(TenantIdentityMiddleware)
    return app


async def test_credit_routes_answer_a_stable_503_before_any_work() -> None:
    calls: list[str] = []
    _publish(False)
    async with AsyncClient(
        transport=ASGITransport(app=_guarded_app(calls)), base_url="http://test"
    ) as client:
        responses = [
            await client.get("/v1/credits/balance"),
            await client.get("/v1/credits/balance"),
            await client.post("/v1/credits/balance", json={"amount": 1}),
            # Not even parsed: no 422 while the switch is off.
            await client.post(
                "/v1/credits/balance",
                content=b"{not json",
                headers={"content-type": "application/json"},
            ),
            await client.post("/v1/credits/balance", json={"amount": "x"}),
        ]

    assert [response.status_code for response in responses] == [503] * 5
    assert all(response.json() == _UNAVAILABLE for response in responses)
    assert calls == []


async def test_credit_routes_run_normally_with_the_switch_on() -> None:
    calls: list[str] = []
    _publish(True)
    async with AsyncClient(
        transport=ASGITransport(app=_guarded_app(calls)), base_url="http://test"
    ) as client:
        ok = await client.post("/v1/credits/balance", json={"amount": 7})
        invalid = await client.post("/v1/credits/balance", json={"amount": "x"})

    assert ok.status_code == 200
    assert ok.json() == {"ok": True, "amount": 7, "session": "session"}
    # FastAPI's own behaviour once the guard lets the request through.
    assert invalid.status_code == 422
    assert calls == ["opened", "opened"]


async def test_the_guard_route_uses_the_value_captured_at_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flipping between the middleware and the route keeps the entry value."""

    calls: list[str] = []
    app = _guarded_app(calls)
    _publish(True)
    original = credit_switch.capture_credit_switch

    def capture_then_flip(scope: Any) -> bool:
        value = original(scope)
        _publish(False)
        return value

    monkeypatch.setattr(
        "src.api.middleware.tenant_identity.capture_credit_switch", capture_then_flip
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/v1/credits/balance")

    assert response.status_code == 200
    assert credit_switch_effective() is False


@pytest.mark.parametrize("effective", [False, True])
async def test_require_credit_switch_alone_answers_the_exact_body(
    effective: bool,
) -> None:
    calls: list[str] = []
    _publish(effective)
    async with AsyncClient(
        transport=ASGITransport(app=_guarded_app(calls)), base_url="http://test"
    ) as client:
        response = await client.get("/plain")

    if effective:
        assert (response.status_code, response.json()) == (200, {"ok": True})
    else:
        assert (response.status_code, response.json()) == (503, _UNAVAILABLE)
        assert calls == []


async def test_an_app_without_the_handler_still_answers_503_never_500() -> None:
    calls: list[str] = []
    _publish(False)
    async with AsyncClient(
        transport=ASGITransport(app=_guarded_app(calls, with_handler=False)),
        base_url="http://test",
    ) as client:
        response = await client.get("/plain")

    assert (response.status_code, response.json()) == (
        503,
        {"detail": "credit_unavailable"},
    )
    assert calls == []


def test_create_app_registers_the_credit_unavailable_handler() -> None:
    from src.api.main import app, create_app

    for application in (app, create_app()):
        handler = application.exception_handlers[CreditUnavailableError]
        assert handler is credit_unavailable_exception_handler


def _real_app_with_probe_routes() -> FastAPI:
    """A fresh ``create_app()`` plus one bare and one guarded credit route.

    The paths sit under the public ``/v1/webhook/`` prefix so that no
    credential (nor database) is needed to reach them.
    """

    from src.api.main import create_app

    application = create_app()

    async def credit_probe(body: _Body) -> dict[str, Any]:
        return {"ok": True, "amount": body.amount}

    guard = [Depends(require_credit_switch)]
    application.router.add_api_route(
        "/v1/webhook/credit-probe-bare",
        credit_probe,
        methods=["POST"],
        dependencies=guard,
    )
    application.router.add_api_route(
        "/v1/webhook/credit-probe-guarded",
        credit_probe,
        methods=["POST"],
        dependencies=guard,
        route_class_override=CreditSwitchRoute,
    )
    return application


async def test_the_real_app_answers_a_bare_dependency_with_the_exact_body() -> None:
    """Documented: only the route class answers before the body is parsed.

    FastAPI reads and parses the body before it resolves dependencies, so a
    bare ``require_credit_switch`` lets a malformed body get its 422 first;
    ``CreditSwitchRoute`` (``credit_router()``) answers 503 for it too.
    """

    application = _real_app_with_probe_routes()
    headers = {"content-type": "application/json"}
    bodies = {
        "valid": b'{"amount": 1}',
        "invalid_field": b'{"amount": "x"}',
        "malformed": b"{not json",
    }

    async def answers(path: str) -> dict[str, tuple[int, Any]]:
        async with AsyncClient(
            transport=ASGITransport(app=application), base_url="http://test"
        ) as client:
            result = {}
            for name, raw in bodies.items():
                response = await client.post(path, content=raw, headers=headers)
                result[name] = (response.status_code, response.json())
            return result

    _publish(False)
    bare = await answers("/v1/webhook/credit-probe-bare")
    guarded = await answers("/v1/webhook/credit-probe-guarded")

    assert bare["valid"] == bare["invalid_field"] == (503, _UNAVAILABLE)
    assert bare["malformed"][0] == 422
    assert bare["malformed"][1]["detail"][0]["type"] == "json_invalid"
    assert all(answer == (503, _UNAVAILABLE) for answer in guarded.values())

    _publish(True)
    assert (await answers("/v1/webhook/credit-probe-bare"))["valid"] == (
        200,
        {"ok": True, "amount": 1},
    )
    assert (await answers("/v1/webhook/credit-probe-guarded"))["valid"] == (
        200,
        {"ok": True, "amount": 1},
    )


async def test_the_guard_opens_no_database_session_and_runs_no_sql() -> None:
    """With the real pool: a refused credit route issues no SQL at all."""

    from src import repositories
    from src.repositories.database import (
        close_database,
        get_db_engine,
        get_db_session_dependency,
    )

    app = FastAPI()
    router = credit_router()

    @router.get("/balance")
    async def balance(
        session: AsyncSession = Depends(get_db_session_dependency),
    ) -> dict[str, Any]:
        await session.execute(text("SELECT count(*) FROM credit_accounts"))
        return {"ok": True}

    app.include_router(router, prefix="/v1/credits")
    app.add_middleware(TenantIdentityMiddleware)

    await repositories.init_database()
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    engine = get_db_engine().sync_engine
    event.listen(engine, "before_cursor_execute", record)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            _publish(False)
            refused = await client.get("/v1/credits/balance")
            assert statements == []
            _publish(True)
            allowed = await client.get("/v1/credits/balance")
    finally:
        event.remove(engine, "before_cursor_execute", record)
        await close_database()

    assert (refused.status_code, refused.json()) == (503, _UNAVAILABLE)
    assert allowed.status_code == 200
    assert any("credit_accounts" in statement for statement in statements)


async def test_the_worker_tick_claims_nothing_while_off() -> None:
    ticks: list[int] = []

    async def tick() -> str:
        ticks.append(1)
        return "claimed"

    _publish(False)
    assert await run_credit_worker_tick(tick) is None
    assert ticks == []

    _publish(True)
    assert await run_credit_worker_tick(tick) == "claimed"
    assert ticks == [1]


async def test_the_ui_panel_degrades_to_none_while_off() -> None:
    loads: list[int] = []

    async def load() -> dict[str, int]:
        loads.append(1)
        return {"available_mc": 1000}

    app = FastAPI()

    @app.get("/summary")
    async def summary(request: Request) -> dict[str, Any]:
        return {"credits": await load_credit_panel(request, load)}

    app.add_middleware(TenantIdentityMiddleware)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        _publish(False)
        off = await client.get("/summary")
        _publish(True)
        on = await client.get("/summary")

    assert off.json() == {"credits": None}
    assert on.json() == {"credits": {"available_mc": 1000}}
    assert loads == [1]


# ---------------------------------------------------------------------------
# Metrics.
# ---------------------------------------------------------------------------


def _credit_totals() -> dict[str, float]:
    return {
        sample.name: sample.value
        for family in REGISTRY.collect()
        if family.name.startswith("neurostack_credit_")
        for sample in family.samples
        if sample.name.endswith("_total")
    }


def test_switch_off_exports_zero_and_no_credit_total_above_zero() -> None:
    _publish(False)

    assert REGISTRY.get_sample_value("neurostack_credit_switch_effective") == 0.0
    assert all(value == 0 for value in _credit_totals().values())


def test_the_gauge_follows_the_verdict_including_a_forced_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _publish(True)
    assert REGISTRY.get_sample_value("neurostack_credit_switch_effective") == 1.0

    _publish(False)
    monkeypatch.setattr(CreditSwitch, "effective", lambda _self: True)
    assert REGISTRY.get_sample_value("neurostack_credit_switch_effective") == 1.0


# ---------------------------------------------------------------------------
# Lifespan.
# ---------------------------------------------------------------------------


async def _task_names() -> set[str]:
    return {task.get_name() for task in asyncio.all_tasks()}


def _in_app(client: Any, coroutine: Any) -> Any:
    return client._submit(coroutine)


def test_the_lifespan_with_the_config_off_reads_nothing_and_starts_no_task(
    monkeypatch: pytest.MonkeyPatch,
    test_app: Any,
    lifespan_test_client_factory: Any,
) -> None:
    monkeypatch.setattr(settings, "credit_hot_path_enabled", False)
    _publish(True)

    with lifespan_test_client_factory(test_app) as client:
        reading = credit_switch_reading()
        names = _in_app(client, _task_names())
        response = client.get("/health")

    assert response.status_code == 200
    assert reading == CreditSwitchReading(False, "config_disabled")
    assert "credit-switch-refresher" not in names
    assert REGISTRY.get_sample_value("neurostack_credit_switch_effective") == 0.0
    assert credit_switch_reading() == CreditSwitchReading(False, "stopped")


def test_the_lifespan_with_the_config_on_fails_closed_on_sqlite(
    monkeypatch: pytest.MonkeyPatch,
    test_app: Any,
    lifespan_test_client_factory: Any,
) -> None:
    """SQLite has no alembic_version: on in config, off in effect, task cancelled."""

    monkeypatch.setattr(settings, "credit_hot_path_enabled", True)
    monkeypatch.setattr(settings, "credit_switch_refresh_seconds", 1)

    with lifespan_test_client_factory(test_app) as client:
        reading = credit_switch_reading()
        names = _in_app(client, _task_names())

    assert reading == CreditSwitchReading(False, "read_error")
    assert "credit-switch-refresher" in names
    # LifespanTestClient raises on a leaked task: the refresher was cancelled.
    assert credit_switch_reading() == CreditSwitchReading(False, "stopped")


def test_install_spa_stays_the_last_registration_of_create_app() -> None:
    source = (
        Path(__file__).resolve().parents[2] / "src" / "api" / "main.py"
    ).read_text(encoding="utf-8")
    body = source[source.index("def create_app") :]
    spa = body.index("install_spa(app)")
    for registration in ("include_router(", "app.mount(", "add_middleware("):
        assert body.rfind(registration) < spa, registration


# ---------------------------------------------------------------------------
# The forced plugin really delivers True (a subprocess: in-process probes
# would count as reads of the run under test).
# ---------------------------------------------------------------------------

_FORCED_PROBE = """\
import asyncio
import os

from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from src.api.middleware.tenant_identity import TenantIdentityMiddleware
from src.services import credit_switch


def test_the_forced_verdict_reaches_request_state():
    origin = os.path.realpath(credit_switch.__file__)
    assert origin.startswith(os.environ["CREDIT_PROBE_ROOT"] + os.sep), origin
    # The real verdict says off: only the plugin can make the request see on.
    credit_switch._SWITCH.publish(
        credit_switch.CreditSwitchReading(False, "config_disabled")
    )
    app = FastAPI()

    @app.post("/probe")
    async def probe(request: Request):
        key = credit_switch.CREDIT_SWITCH_STATE_KEY
        return {"captured": getattr(request.state, key)}

    app.add_middleware(TenantIdentityMiddleware)

    async def call():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://p") as client:
            return (await client.post("/probe")).json()

    assert asyncio.run(call()) == {"captured": True}
"""


def _run_forced_probe(tmp_path: Path, *plugin: str) -> subprocess.CompletedProcess[str]:
    probe = tmp_path / "test_forced_switch_probe.py"
    probe.write_text(_FORCED_PROBE, encoding="utf-8")
    root = Path(__file__).resolve().parents[2]
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONSTARTUP"}
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["CREDIT_PROBE_ROOT"] = os.path.realpath(root)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
            "--rootdir",
            str(tmp_path),
            *plugin,
            str(probe),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )


def test_the_forced_plugin_delivers_true_to_request_state(tmp_path: Path) -> None:
    forced = _run_forced_probe(tmp_path, "-p", "tests.credit_switch_forced_plugin")

    assert forced.returncode == 0, forced.stdout + forced.stderr
    assert re.search(r"\b1 passed\b", forced.stdout), forced.stdout
    reads = re.search(
        r"credit switch forced effective: (\d+) verdict reads by the code",
        forced.stdout,
    )
    assert reads is not None and int(reads.group(1)) >= 1, forced.stdout


def test_the_forced_probe_fails_without_the_plugin(tmp_path: Path) -> None:
    """The probe is not vacuous: off stays off without the plugin."""

    plain = _run_forced_probe(tmp_path)

    assert plain.returncode == 1, plain.stdout + plain.stderr
    assert "'captured': False" in plain.stdout, plain.stdout


# ---------------------------------------------------------------------------
# A tree without tests/ (the production image).
# ---------------------------------------------------------------------------


def test_the_modules_import_and_read_the_chain_without_a_tests_tree(
    tmp_path: Path,
) -> None:
    from tests.services.test_credit_no_tests_dir import (
        _PROBE,
        ROOT,
        _copy_tree,
        _image_sources,
    )

    probe = _PROBE + (
        "from pathlib import Path\n"
        "from src.services.credit_switch import (\n"
        "    CREDIT_SCHEMA_MIN_REVISION, IMAGE_VERSIONS_DIR,\n"
        "    credit_switch_effective, load_image_migration_chain,\n"
        ")\n"
        "assert IMAGE_VERSIONS_DIR == Path(root) / 'alembic' / 'versions'\n"
        "chain = load_image_migration_chain()\n"
        "assert CREDIT_SCHEMA_MIN_REVISION in chain\n"
        "assert credit_switch_effective() is False\n"
        "refuse_reads()\n"
        "print('image chain head:', chain[-1])\n"
    )
    image = tmp_path / "app"
    for source in _image_sources():
        _copy_tree(ROOT / source, image / source)
    assert not (image / "tests").exists()

    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONSTARTUP"}
    }
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            probe,
            str(image),
            "src.services.credit_switch",
            "src.services.credit_principal",
        ],
        cwd=image,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert f"image chain head: {current_head()}" in completed.stdout
