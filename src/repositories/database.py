"""Process-wide SQLAlchemy engine and session-factory lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Callable, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.config.logging import get_logger
from src.core.exceptions import ConfigurationError

logger = get_logger(__name__)


def _patch_jsonb_for_sqlite() -> None:
    """Register JSONB as JSON on SQLite so create_all works in tests."""
    from sqlalchemy.dialects import sqlite as sqlite_dialect

    setattr(
        sqlite_dialect.base.SQLiteTypeCompiler,
        "visit_JSONB",
        sqlite_dialect.base.SQLiteTypeCompiler.visit_JSON,
    )


SessionFactory = async_sessionmaker[AsyncSession]

_engine: Optional[AsyncEngine] = None
_session_factory: Optional[SessionFactory] = None
_lifecycle_lock = asyncio.Lock()


async def _dispose_once(engine: AsyncEngine) -> None:
    """Finish one accepted/rejected engine disposal even if the caller is cancelled."""
    disposal = asyncio.create_task(engine.dispose())
    try:
        await asyncio.shield(disposal)
    except asyncio.CancelledError:
        await disposal
        raise


async def init_database(
    *,
    database_url: str | None = None,
    engine_factory: Callable[..., AsyncEngine] | None = None,
    session_factory_builder: Callable[..., SessionFactory] | None = None,
) -> None:
    """Create, probe, and atomically publish the process-wide database pool."""
    from src.config.settings import settings

    global _engine, _session_factory

    async with _lifecycle_lock:
        if _engine is not None:
            return

        resolved_database_url = database_url or settings.database_url
        resolved_engine_factory = engine_factory or create_async_engine
        resolved_session_factory_builder = session_factory_builder or async_sessionmaker

        candidate: AsyncEngine | None = None
        try:
            engine_options: dict[str, object] = {
                "echo": settings.debug,
                "future": True,
                "hide_parameters": True,
            }
            if not resolved_database_url.startswith("sqlite"):
                engine_options.update(
                    pool_size=settings.database_pool_size,
                    max_overflow=settings.database_max_overflow,
                    pool_timeout=settings.database_pool_timeout,
                )
            candidate = resolved_engine_factory(resolved_database_url, **engine_options)
            candidate_factory = resolved_session_factory_builder(
                candidate,
                class_=AsyncSession,
                expire_on_commit=False,
                autoflush=False,
                autocommit=False,
            )

            async with candidate.begin() as connection:
                await connection.execute(text("SELECT 1"))

            if candidate.url.drivername.startswith("sqlite"):
                import src.models  # noqa: F401 - register model metadata
                from src.models.base import Base

                _patch_jsonb_for_sqlite()
                async with candidate.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
        except BaseException as error:
            if candidate is not None:
                try:
                    await _dispose_once(candidate)
                except asyncio.CancelledError:
                    if isinstance(error, asyncio.CancelledError):
                        raise error
                    raise
                except Exception:
                    logger.exception("Failed to dispose rejected database engine")
            if not isinstance(error, Exception):
                raise
            logger.error(
                "Failed to initialize database",
                error_type=type(error).__name__,
                exc_info=True,
            )
            raise ConfigurationError(
                "Failed to initialize database connection"
            ) from error

        _engine = candidate
        _session_factory = candidate_factory
        logger.info(
            "Database initialized successfully",
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
        )


async def close_database() -> None:
    """Atomically unpublish and dispose the process-wide pool exactly once."""
    global _engine, _session_factory

    async with _lifecycle_lock:
        engine = _engine
        _engine = None
        _session_factory = None
        if engine is None:
            return
        await _dispose_once(engine)
        logger.info("Database connections closed")


def get_db_engine() -> AsyncEngine:
    """Return the lifespan-owned engine."""
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _engine


def get_db_session_factory() -> SessionFactory:
    """Return the exact lifespan-owned session factory."""
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _session_factory


def get_db_session() -> AsyncSession:
    """Open a session from the lifespan-owned factory."""
    return get_db_session_factory()()


async def get_db_session_dependency() -> AsyncGenerator[AsyncSession, None]:
    """Yield a request session; writes must still commit explicitly."""
    session = get_db_session()
    try:
        yield session
    finally:
        await session.close()
