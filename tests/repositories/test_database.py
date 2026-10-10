"""Lifecycle contracts for the process-wide database pool."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import create_async_engine

from src.config.settings import Settings
from src.core.exceptions import ConfigurationError
from src.repositories import database

DatabaseLifecycle = Callable[..., Awaitable[None]]


def _engine(*, execute: AsyncMock | None = None) -> MagicMock:
    engine = MagicMock()
    engine.url.drivername = "postgresql+asyncpg"
    engine.dispose = AsyncMock()
    connection = AsyncMock()
    connection.execute = execute or AsyncMock()
    connection.__aenter__ = AsyncMock(return_value=connection)
    connection.__aexit__ = AsyncMock(return_value=None)
    engine.begin.return_value = connection
    return engine


@pytest.fixture(autouse=True)
def isolated_database_globals(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(database, "_engine", None)
    monkeypatch.setattr(database, "_session_factory", None)
    monkeypatch.setattr(database, "_lifecycle_lock", asyncio.Lock())


@pytest.mark.asyncio
async def test_concurrent_startup_creates_and_publishes_one_engine(
    monkeypatch: pytest.MonkeyPatch,
    production_database_lifecycle: DatabaseLifecycle,
) -> None:
    probe_started = asyncio.Event()
    release_probe = asyncio.Event()

    async def probe(*_args: object) -> None:
        probe_started.set()
        await release_probe.wait()

    candidate = _engine(execute=AsyncMock(side_effect=probe))
    engine_factory = MagicMock(return_value=candidate)
    session_factory = MagicMock()
    monkeypatch.setattr(
        database, "async_sessionmaker", MagicMock(return_value=session_factory)
    )

    first = asyncio.create_task(
        production_database_lifecycle(engine_factory=engine_factory)
    )
    await probe_started.wait()
    second = asyncio.create_task(
        production_database_lifecycle(engine_factory=engine_factory)
    )
    await asyncio.sleep(0)

    assert database._engine is None
    assert database._session_factory is None
    engine_factory.assert_called_once()

    release_probe.set()
    await asyncio.gather(first, second)

    assert database.get_db_engine() is candidate
    assert database.get_db_session_factory() is session_factory
    engine_factory.assert_called_once()


@pytest.mark.asyncio
async def test_failed_startup_disposes_candidate_without_publishing(
    production_database_lifecycle: DatabaseLifecycle,
) -> None:
    candidate = _engine(execute=AsyncMock(side_effect=RuntimeError("probe failed")))
    engine_factory = MagicMock(return_value=candidate)

    with pytest.raises(ConfigurationError, match="initialize database"):
        await production_database_lifecycle(engine_factory=engine_factory)

    assert database._engine is None
    assert database._session_factory is None
    candidate.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_startup_disposes_candidate_without_publishing(
    production_database_lifecycle: DatabaseLifecycle,
) -> None:
    probe_started = asyncio.Event()
    never_release = asyncio.Event()

    async def probe(*_args: object) -> None:
        probe_started.set()
        await never_release.wait()

    candidate = _engine(execute=AsyncMock(side_effect=probe))
    engine_factory = MagicMock(return_value=candidate)

    startup = asyncio.create_task(
        production_database_lifecycle(engine_factory=engine_factory)
    )
    await probe_started.wait()
    startup.cancel()
    with pytest.raises(asyncio.CancelledError):
        await startup

    assert database._engine is None
    assert database._session_factory is None
    candidate.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_concurrent_shutdown_disposes_accepted_engine_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    candidate = _engine()
    monkeypatch.setattr(database, "_engine", candidate)
    monkeypatch.setattr(database, "_session_factory", MagicMock())

    await asyncio.gather(database.close_database(), database.close_database())
    await database.close_database()

    candidate.dispose.assert_awaited_once()
    assert database._engine is None
    assert database._session_factory is None


@pytest.mark.asyncio
async def test_runtime_pool_options_come_from_settings(
    production_database_lifecycle: DatabaseLifecycle,
) -> None:
    candidate = _engine()
    engine_factory = MagicMock(return_value=candidate)

    await production_database_lifecycle(engine_factory=engine_factory)

    _, options = engine_factory.call_args
    from src.config.settings import settings

    assert options["pool_size"] == settings.database_pool_size
    assert options["max_overflow"] == settings.database_max_overflow
    assert options["pool_timeout"] == settings.database_pool_timeout


@pytest.mark.asyncio
async def test_pytest_import_never_switches_the_configured_database_url(
    production_database_lifecycle: DatabaseLifecycle,
) -> None:
    candidate = _engine()
    engine_factory = MagicMock(return_value=candidate)

    await production_database_lifecycle(engine_factory=engine_factory)

    from src.config.settings import settings

    assert engine_factory.call_args.args[0] == settings.database_url


@pytest.mark.asyncio
async def test_isolated_database_url_requires_explicit_injection(
    production_database_lifecycle: DatabaseLifecycle,
) -> None:
    candidate = _engine()
    candidate.url.drivername = "sqlite+aiosqlite"
    engine_factory = MagicMock(return_value=candidate)

    await production_database_lifecycle(
        database_url="sqlite+aiosqlite:///:memory:",
        engine_factory=engine_factory,
    )

    assert engine_factory.call_args.args[0] == "sqlite+aiosqlite:///:memory:"


@pytest.mark.asyncio
async def test_isolated_session_factory_requires_explicit_injection(
    production_database_lifecycle: DatabaseLifecycle,
) -> None:
    candidate = _engine()
    isolated_factory = MagicMock()
    builder = MagicMock(return_value=isolated_factory)

    await production_database_lifecycle(
        engine_factory=MagicMock(return_value=candidate),
        session_factory_builder=builder,
    )

    assert database.get_db_session_factory() is isolated_factory
    builder.assert_called_once()


@pytest.mark.asyncio
async def test_bare_init_remains_guarded_by_the_isolated_pytest_database() -> None:
    await database.init_database()

    assert str(database.get_db_engine().url) == "sqlite+aiosqlite:///:memory:"
    await database.close_database()


@pytest.mark.asyncio
async def test_production_lifecycle_helper_rejects_real_engine_factory(
    production_database_lifecycle: DatabaseLifecycle,
) -> None:
    with pytest.raises(TypeError, match="mock engine factory"):
        await production_database_lifecycle(engine_factory=create_async_engine)


@pytest.mark.asyncio
@pytest.mark.parametrize("fatal", [KeyboardInterrupt(), SystemExit(3)])
async def test_fatal_startup_errors_are_disposed_and_reraised_unchanged(
    fatal: BaseException,
    production_database_lifecycle: DatabaseLifecycle,
) -> None:
    candidate = _engine(execute=AsyncMock(side_effect=fatal))

    with pytest.raises(type(fatal)) as raised:
        await production_database_lifecycle(
            engine_factory=MagicMock(return_value=candidate)
        )

    assert raised.value is fatal
    candidate.dispose.assert_awaited_once()


@pytest.mark.asyncio
async def test_event_store_and_handoff_resolve_the_published_session_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.memory import event_store as event_store_module
    from src.services.handoff_service import HandoffService

    session = MagicMock()
    shared_factory = MagicMock(return_value=session)
    monkeypatch.setattr(database, "_session_factory", shared_factory)
    store = await event_store_module.get_event_store()
    service = HandoffService(
        event_store=store,
        summarizer=MagicMock(),
        webhook_dispatcher=MagicMock(),
    )

    assert store._session_factory is database.get_db_session_factory()
    assert service._open_session() is session
    shared_factory.assert_called_once_with()


def test_only_database_application_module_creates_async_engines() -> None:
    assert database.__file__ is not None
    database_path = Path(database.__file__)
    source_root = database_path.parents[1]
    offenders = []
    for path in source_root.rglob("*.py"):
        if path == database_path:
            continue
        if "create_async_engine" in path.read_text(encoding="utf-8"):
            offenders.append(str(path.relative_to(source_root)))
    assert offenders == []


@pytest.mark.parametrize("value", [True, False, 5.0, "5.0", "+5", " 5", "5 "])
@pytest.mark.parametrize(
    "field",
    ["database_pool_size", "database_max_overflow", "database_pool_timeout"],
)
def test_database_pool_settings_reject_coercible_non_integer_values(
    field: str, value: object
) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
