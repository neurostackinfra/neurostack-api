"""Tests for cross-worker AI-message dedup helpers in webhook.py.

Covers ``_store_ai_msg_id_to_db`` — a defect where the writer touched
``sess.metadata`` (SQLAlchemy's declarative ``MetaData`` object) instead of the
mapped ``session_metadata`` attribute, silently raising ``TypeError`` for every
bot-sent message.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.models  # noqa: F401 - register models with Base.metadata
from src.api.routes.webhook import _store_ai_msg_id_to_db
from src.models.base import Base
from src.models.session import Session
from src.models.tenant import Tenant
from src.repositories.database import _patch_jsonb_for_sqlite
from src.repositories.session import SessionRepository


async def _keep_event_loop_responsive() -> None:
    """Prevent lost thread-safe wakeups from stalling aiosqlite in tests."""
    while True:
        await asyncio.sleep(0.01)


@pytest.fixture(autouse=True)
async def _heartbeat() -> AsyncGenerator[None, None]:
    task = asyncio.create_task(_keep_event_loop_responsive())
    try:
        yield
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.fixture
async def dedup_db() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    _patch_jsonb_for_sqlite()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def _seed_session(factory: async_sessionmaker[AsyncSession]) -> UUID:
    tenant_id = uuid4()
    session_id = uuid4()
    async with factory() as db:
        db.add(
            Tenant(
                id=tenant_id,
                name="Clinic X",
                slug=f"clinic-{uuid4().hex}",
                api_key_hash=f"hash-{uuid4().hex}",
                config={},
            )
        )
        db.add(
            Session(
                id=session_id,
                tenant_id=tenant_id,
                user_id="user-1",
                channel="whatsapp",
                cognitive_state={},
                session_metadata={},
            )
        )
        await db.commit()
    return session_id


@pytest.mark.asyncio
async def test_store_ai_msg_id_round_trips_via_session_metadata(
    dedup_db: async_sessionmaker[AsyncSession],
) -> None:
    """Write an id in one AsyncSession, reload in a NEW one, and assert it stuck."""
    session_id = await _seed_session(dedup_db)
    message_id = "wamid.HBgN-ROUNDTRIP-123"

    async with dedup_db() as db:
        await _store_ai_msg_id_to_db(session_id, message_id, db)
        await db.commit()

    async with dedup_db() as db2:
        sess = await SessionRepository(db2).get_by_id(session_id)
        assert sess is not None
        entries = (sess.session_metadata or {}).get("ai_msg_ids", [])

    stored_ids = [entry[0] for entry in entries]
    assert (
        message_id in stored_ids
    ), f"expected {message_id!r} in session_metadata['ai_msg_ids'], got {entries!r}"


@pytest.mark.asyncio
async def test_store_ai_msg_id_logs_warning_on_failure(
    dedup_db: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A DB error inside the helper must surface as WARNING, not DEBUG."""

    class _ExplodingSession:
        async def get_by_id(self, _id: UUID):  # noqa: D401
            raise RuntimeError("boom: simulated repo failure")

    import src.repositories.session as repo_mod

    monkeypatch.setattr(
        repo_mod,
        "SessionRepository",
        lambda _db: _ExplodingSession(),
    )

    session_id = await _seed_session(dedup_db)
    async with dedup_db() as db:
        with caplog.at_level(logging.DEBUG, logger="src.api.routes.webhook"):
            await _store_ai_msg_id_to_db(session_id, "wamid.HBgN-FAIL", db)

    warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "_store_ai_msg_id_to_db" in r.getMessage()
    ]
    debugs = [
        r
        for r in caplog.records
        if r.levelno == logging.DEBUG and "_store_ai_msg_id_to_db" in r.getMessage()
    ]
    assert warnings, (
        f"expected WARNING for helper failure, got records="
        f"{[r.levelname + ':' + r.getMessage() for r in caplog.records]!r}"
    )
    assert not debugs, "helper failure must not be logged at DEBUG anymore"
