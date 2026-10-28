"""``EventStore.append_batch(..., session=...)`` joins the caller's transaction.

A phase 1 handoff writes ``handoff.request`` in the transaction that holds the
``sessions`` row lock (#145). With a caller session the store sends the rows
in it and never opens, commits or rolls back anything of its own; without one
it keeps its own commit.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from src.core.exceptions import EventStoreError
from src.interaction_context import InteractionOrigin
from src.memory.event_store import EventData, EventStore, EventType


def _event(origin: InteractionOrigin = InteractionOrigin.SIMULATOR) -> EventData:
    return EventData(
        tenant_id=uuid4(),
        session_id=uuid4(),
        event_type=EventType.HANDOFF_REQUEST.value,
        interaction_origin=origin,
        event_data={"reason": "user_request"},
        event_metadata={"handoff_id": str(uuid4())},
    )


def _refusing_factory() -> MagicMock:
    return MagicMock(side_effect=AssertionError("caller session path opened a session"))


def _caller_session() -> AsyncMock:
    session = AsyncMock()
    session.commit = AsyncMock(side_effect=AssertionError("committed caller session"))
    session.rollback = AsyncMock(side_effect=AssertionError("rolled back caller"))
    session.close = AsyncMock(side_effect=AssertionError("closed caller session"))
    return session


@pytest.mark.asyncio
async def test_caller_session_receives_every_row_and_is_never_committed() -> None:
    session = _caller_session()
    factory = _refusing_factory()
    store = EventStore(session_factory=factory)
    events = [_event(), _event(InteractionOrigin.PRODUCTION)]

    appended = await store.append_batch(events, session=session)

    assert appended == events
    factory.assert_not_called()
    assert session.execute.await_count == 2
    rows: list[dict[str, Any]] = [
        call.args[1] for call in session.execute.await_args_list
    ]
    assert [row["id"] for row in rows] == [event.id for event in events]
    assert [row["interaction_origin"] for row in rows] == [
        InteractionOrigin.SIMULATOR.value,
        InteractionOrigin.PRODUCTION.value,
    ]
    assert rows[0]["metadata"] == events[0].event_metadata
    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_caller_session_works_without_a_store_factory() -> None:
    session = _caller_session()

    await EventStore().append_batch([_event()], session=session)

    session.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_caller_session_failure_is_left_to_the_caller() -> None:
    session = _caller_session()
    session.execute = AsyncMock(side_effect=SQLAlchemyError("fk violation"))

    with pytest.raises(EventStoreError, match="Failed to append batch"):
        await EventStore(session_factory=_refusing_factory()).append_batch(
            [_event()], session=session
        )

    session.commit.assert_not_awaited()
    session.rollback.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_caller_session_the_store_still_commits_its_own() -> None:
    own = AsyncMock()
    own.__aenter__ = AsyncMock(return_value=own)
    own.__aexit__ = AsyncMock(return_value=None)
    store = EventStore(session_factory=MagicMock(return_value=own))

    await store.append_batch([_event(), _event()])

    assert own.execute.await_count == 2
    own.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_batch_touches_no_session() -> None:
    session = _caller_session()

    assert await EventStore().append_batch([], session=session) == []

    session.execute.assert_not_awaited()
