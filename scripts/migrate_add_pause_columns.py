"""
Migration: Add is_paused and paused_until columns to sessions table.
Idempotent - safe to run multiple times.
"""

import asyncio
import sys
import os

# Ensure project root is on sys.path so src.config.settings can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from src.config.settings import settings


async def migrate() -> None:
    engine = create_async_engine(settings.database_url, echo=False)

    async with engine.begin() as conn:
        # is_paused
        await conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'sessions' AND column_name = 'is_paused'
                ) THEN
                    ALTER TABLE sessions ADD COLUMN is_paused BOOLEAN NOT NULL DEFAULT FALSE;
                END IF;
            END
            $$;
        """))

        # paused_until
        await conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns
                    WHERE table_name = 'sessions' AND column_name = 'paused_until'
                ) THEN
                    ALTER TABLE sessions ADD COLUMN paused_until TIMESTAMP WITH TIME ZONE;
                END IF;
            END
            $$;
        """))

    await engine.dispose()
    print("Migration complete: is_paused and paused_until columns ensured on sessions table.")


if __name__ == "__main__":
    asyncio.run(migrate())
