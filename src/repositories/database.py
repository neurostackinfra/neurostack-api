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
