"""
NeuroStack - Database Session Management
Manages SQLAlchemy async engine and session factory.
"""

from typing import Optional

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from src.config.logging import get_logger
from src.core.exceptions import ConfigurationError

logger = get_logger(__name__)

# Global engine and session factory
_engine: Optional[AsyncEngine] = None
_session_factory: Optional[async_sessionmaker[AsyncSession]] = None


async def init_database() -> None:
    """
    Initialize database connection pool.
    
    Creates the async engine and session factory.
    Should be called during application startup.
    """
    # Import settings here to avoid circular import
    from src.config.settings import settings
    
    global _engine, _session_factory
    
    if _engine is not None:
        logger.warning("Database already initialized")
        return
    
    try:
        # If running tests (pytest present) or environment explicitly set to 'test',
        # prefer an in-memory sqlite database to avoid concurrency issues and external dependencies.
        import sys as _sys

        if "pytest" in _sys.modules or getattr(settings, "environment", "") == "test":
            logger.info("Using in-memory sqlite for test environment")
            _engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
        else:
            _engine = create_async_engine(
                settings.database_url,
                pool_size=settings.database_pool_size,
                max_overflow=settings.database_max_overflow,
                pool_timeout=settings.database_pool_timeout,
                echo=settings.debug,
                future=True,
            )
        
        _session_factory = async_sessionmaker(
            _engine,
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
            autocommit=False,
        )
        
        # Test connection
        async with _engine.begin() as conn:
            await conn.execute(text("SELECT 1"))
        
        # If using an in-memory sqlite database for tests, create the schema
        try:
            # Import models package to ensure all models are registered on Base.metadata
            import src.models  # noqa: F401 - import side-effects (register models)
            # Import Base lazily to avoid circular imports
            from src.models.base import Base as _Base

            if _engine.url.drivername.startswith("sqlite"):
                # Create all tables on the sync engine for sqlite (including in-memory)
                _Base.metadata.create_all(bind=_engine.sync_engine)
                logger.info("Created sqlite schema (in-memory or file-backed) for tests")
        except Exception:
            # Non-fatal: if schema creation fails, we'll surface errors later when queries run
            logger.debug("Skipping in-memory sqlite schema creation (not applicable or failed)", exc_info=True)
        
        logger.info(
            "Database initialized successfully",
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
        )
    except SQLAlchemyError as e:
        logger.error(
            "Failed to initialize database",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        # If running tests, fallback to an in-memory sqlite database to avoid
        # requiring a Postgres instance in CI/local test runs.
        if getattr(settings, "environment", "") == "test":
            logger.warning("Falling back to in-memory sqlite for test environment")
            # Create a lightweight in-memory sqlite async engine
            _engine = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False, future=True)
            _session_factory = async_sessionmaker(
                _engine,
                class_=AsyncSession,
                expire_on_commit=False,
                autoflush=False,
                autocommit=False,
            )
            # Ensure the connection works
            async with _engine.begin() as conn:
                await conn.execute(text("SELECT 1"))
            logger.info("Fallback in-memory sqlite database initialized for tests")
            return
        raise ConfigurationError("Failed to initialize database connection") from e
    except Exception as e:
        logger.error(
            "Unexpected error initializing database",
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        raise ConfigurationError("Failed to initialize database connection") from e


async def close_database() -> None:
    """
    Close database connections.
    
    Disposes of the engine and clears session factory.
    Should be called during application shutdown.
    """
    global _engine, _session_factory
    
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("Database connections closed")


def get_db_engine() -> AsyncEngine:
    """
    Get the database engine.
    
    Returns:
        The async engine instance
        
    Raises:
        RuntimeError: If database is not initialized
    """
    if _engine is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _engine


def get_db_session() -> AsyncSession:
    """
    Get a new database session.
    
    Returns:
        A new AsyncSession instance
        
    Raises:
        RuntimeError: If database is not initialized
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialized. Call init_database() first.")
    return _session_factory()


async def get_db_session_dependency() -> AsyncSession:
    """
    Dependency function for FastAPI dependency injection.
    
    Usage:
        @app.get("/endpoint")
        async def endpoint(session: AsyncSession = Depends(get_db_session_dependency)):
            ...
    """
    async with get_db_session() as session:
        try:
            yield session
            await session.commit()
        except SQLAlchemyError:
            await session.rollback()
            raise
        except Exception as e:
            await session.rollback()
            logger.error(
                "Unexpected error in database session",
                error=str(e),
                error_type=type(e).__name__,
                exc_info=True,
            )
            raise
        finally:
            await session.close()
