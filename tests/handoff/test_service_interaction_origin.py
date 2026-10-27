"""Group 4B: interaction origin emitted by the canonical handoff writer.

``src.services.handoff_service.HandoffService`` is the only lifecycle writer
(``src.handoff.service`` re-exports it; its identity is pinned in
``tests/handoff/test_service.py``).  These tests drive it directly:

* the synchronous initiation persists the origin, tenant and session of the
  context it received, and never the reserved ``interaction_event_id``;
* without a context the initiation is ``unknown``;
* every late transition that writes an event (accept, complete, timeout) is
  ``unknown`` even when the handoff was initiated as ``production`` -- the
  ``EventData`` default is already ``unknown``, so a test without that seed
  would pass vacuously;
* confirm, confirm_queue and cancel still write no event;
* neither ``HandoffRecord`` nor ``HandoffRequest`` carries origin.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

import src.services.handoff_service as handoff_service_module
from src.handoff.models import HandoffReason, HandoffRequest, HandoffStatus
from src.handoff.webhook import WebhookDispatcher
from src.interaction_context import (
    InteractionOrigin,
    ResolvedInteractionContext,
    build_interaction_context,
    unknown_interaction_context,
)
from src.memory.event_store import EventData, EventType
from src.models.handoff import HandoffRecord
from src.services.handoff_service import (
    HANDOFF_OPERATIONS,
    HandoffPostCommitError,
    HandoffService,
)
from tests.handoff.test_service import (
    _build_test_service,
    _FakeEventStore,
    _FakeHandoffStore,
)

# ``HandoffRequest`` stays unchanged (D1): no trust object or origin rides on a
# data model.  The exact field set is pinned so any new field is a decision.
HANDOFF_REQUEST_FIELDS = frozenset(
    {
        "current_mode",
        "detected_intent",
        "funnel_stage",
        "key_info",
        "reason",
        "recent_messages",
        "requested_by",
        "safety_concern",
        "sector",
        "session_id",
        "sla",
        "summary",
        "tenant_id",
        "turn_count",
        "urgency",
        "verification_status",
    }
)


def _dispatcher() -> WebhookDispatcher:
    return WebhookDispatcher(default_timeout=5, max_retries=1, retry_delay=0)


def _service() -> tuple[HandoffService, _FakeHandoffStore, _FakeEventStore]:
    store = _FakeHandoffStore()
    events = _FakeEventStore()
    return _build_test_service(store, _dispatcher(), event_store=events), store, events


def _request(tenant_id: UUID, session_id: UUID) -> HandoffRequest:
    return HandoffRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        reason=HandoffReason.USER_REQUEST,
        recent_messages=[{"role": "user", "content": "Quero falar com um humano"}],
    )


def _phase_one_context(
    tenant_id: UUID,
    session_id: UUID,
    origin: InteractionOrigin,
) -> ResolvedInteractionContext:
    """The context the pipeline holds in phase 1 of a ``message.in``."""
    context = build_interaction_context(
        effective_tenant_id=tenant_id,
        session_id=session_id,
        origin=origin,
        event_type="message.in",
    )
    assert context.interaction_event_id is not None
    return context


def _only(events: list[EventData], event_type: EventType) -> EventData:
    matching = [event for event in events if event.event_type == event_type.value]
    assert len(matching) == 1, [event.event_type for event in events]
    return matching[0]


def _assert_no_interaction_id(event: EventData, interaction_event_id: UUID) -> None:
    assert "interaction_event_id" not in event.event_data
    assert "interaction_event_id" not in event.event_metadata
    rendered = event.model_dump_json()
    assert "interaction_event_id" not in rendered
    assert str(interaction_event_id) not in rendered
    assert event.id != interaction_event_id


async def _run_late_path(
    service: HandoffService,
    handoff_id: UUID,
    tenant_id: UUID,
    path: tuple[str, ...],
) -> None:
    for step in path:
        if step == "accept":
            await service.accept_handoff(handoff_id, "agent-1", tenant_id=tenant_id)
        elif step == "confirm":
            await service.confirm_handoff(handoff_id, "agent-1", tenant_id=tenant_id)
        elif step == "complete":
            await service.complete_handoff(handoff_id, "done", tenant_id=tenant_id)
        else:
            await service.timeout_handoff(handoff_id, tenant_id=tenant_id)


def _update_error_count() -> float:
    value: float = HANDOFF_OPERATIONS.labels(
        operation="update", status="error"
    )._value.get()
    return value


async def _initiate_production(
    service: HandoffService,
    events: _FakeEventStore,
) -> tuple[UUID, UUID, UUID]:
    """Seed a handoff from a ``production`` initiation and prove the seed."""
    tenant_id, session_id = uuid4(), uuid4()
    response = await service.initiate_handoff(
        _request(tenant_id, session_id),
        context=_phase_one_context(tenant_id, session_id, InteractionOrigin.PRODUCTION),
    )
    seeded = _only(events.appended, EventType.HANDOFF_REQUEST)
    assert seeded.interaction_origin is InteractionOrigin.PRODUCTION
    return response.id, tenant_id, session_id


# ============================================
# Synchronous initiation
# ============================================


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", list(InteractionOrigin))
async def test_initiation_persists_context_origin_tenant_and_session(
    origin: InteractionOrigin,
) -> None:
    service, store, events = _service()
    tenant_id, session_id = uuid4(), uuid4()
    context = _phase_one_context(tenant_id, session_id, origin)

    response = await service.initiate_handoff(
        _request(tenant_id, session_id), context=context
    )

    assert [event.event_type for event in events.appended] == [
        EventType.HANDOFF_REQUEST.value
    ]
    event = events.appended[0]
    assert event.interaction_origin is origin
    assert event.tenant_id == tenant_id == context.effective_tenant_id
    assert event.session_id == session_id == context.session_id
    assert event.event_metadata == {"handoff_id": str(response.id)}
    assert event.to_insert_dict()["interaction_origin"] == origin.value
    assert context.interaction_event_id is not None
    _assert_no_interaction_id(event, context.interaction_event_id)
    record = store.records[response.id]
    assert str(context.interaction_event_id) not in repr(record.context)


@pytest.mark.asyncio
async def test_initiation_never_persists_the_reserved_message_in_id() -> None:
    """Phase 1 handoff precedes the phase 2 ``message.in`` write.

    The reserved ID may never be written, so ``handoff.request`` must not
    reference it in ``event_data``, metadata or anywhere in the row.
    """
    service, _store, events = _service()
    tenant_id, session_id = uuid4(), uuid4()
    context = _phase_one_context(tenant_id, session_id, InteractionOrigin.PRODUCTION)

    await service.initiate_handoff(_request(tenant_id, session_id), context=context)

    event = _only(events.appended, EventType.HANDOFF_REQUEST)
    assert context.interaction_event_id is not None
    _assert_no_interaction_id(event, context.interaction_event_id)
    assert str(context.interaction_event_id) not in repr(event.to_insert_dict())


@pytest.mark.asyncio
async def test_initiation_without_context_is_unknown() -> None:
    service, _store, events = _service()
    tenant_id, session_id = uuid4(), uuid4()

    await service.initiate_handoff(_request(tenant_id, session_id))

    event = _only(events.appended, EventType.HANDOFF_REQUEST)
    assert event.interaction_origin is InteractionOrigin.UNKNOWN
    assert event.tenant_id == tenant_id
    assert event.session_id == session_id


@pytest.mark.asyncio
async def test_initiation_origin_ignores_request_channel_and_metadata() -> None:
    """The origin comes from the context only, never from request contents."""
    service, _store, events = _service()
    tenant_id, session_id = uuid4(), uuid4()
    request = HandoffRequest(
        tenant_id=tenant_id,
        session_id=session_id,
        reason=HandoffReason.USER_REQUEST,
        recent_messages=[
            {
                "role": "user",
                "content": "oi",
                "channel": "whatsapp",
                "interaction_origin": "production",
            }
        ],
        key_info={"channel": "whatsapp", "interaction_origin": "production"},
    )

    await service.initiate_handoff(
        request,
        context=_phase_one_context(tenant_id, session_id, InteractionOrigin.SIMULATOR),
    )
    await service.initiate_handoff(
        HandoffRequest(
            tenant_id=tenant_id,
            session_id=session_id,
            reason=HandoffReason.USER_REQUEST,
            key_info={"channel": "whatsapp", "interaction_origin": "production"},
        )
    )

    origins = [event.interaction_origin for event in events.appended]
    assert origins == [InteractionOrigin.SIMULATOR, InteractionOrigin.UNKNOWN]


# ============================================
# Late transitions
# ============================================


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "event_type"),
    [
        (("accept",), EventType.HANDOFF_ACCEPT),
        (("confirm", "complete"), EventType.HANDOFF_COMPLETE),
        (("timeout",), EventType.HANDOFF_TIMEOUT),
    ],
    ids=["accept", "complete", "timeout"],
)
async def test_late_transition_event_is_unknown_after_production_initiation(
    path: tuple[str, ...],
    event_type: EventType,
) -> None:
    service, store, events = _service()
    handoff_id, tenant_id, session_id = await _initiate_production(service, events)
    # Anything origin-like on the record must be ignored by the late writer.
    record = store.records[handoff_id]
    record.context = {
        **record.context,
        "interaction_origin": "production",
        "origin": "production",
        "channel": "whatsapp",
    }

    await _run_late_path(service, handoff_id, tenant_id, path)

    assert [event.event_type for event in events.appended] == [
        EventType.HANDOFF_REQUEST.value,
        event_type.value,
    ]
    late = _only(events.appended, event_type)
    assert late.interaction_origin is InteractionOrigin.UNKNOWN
    assert late.to_insert_dict()["interaction_origin"] == "unknown"
    assert late.tenant_id == tenant_id
    assert late.session_id == session_id
    assert late.event_data["handoff_id"] == str(handoff_id)
    assert "interaction_event_id" not in late.model_dump_json()
    assert late.event_metadata == {}


LATE_EVENT_PATHS = pytest.mark.parametrize(
    ("path", "event_type"),
    [
        (("accept",), EventType.HANDOFF_ACCEPT),
        (("confirm", "complete"), EventType.HANDOFF_COMPLETE),
        (("timeout",), EventType.HANDOFF_TIMEOUT),
    ],
    ids=["accept", "complete", "timeout"],
)


@pytest.mark.asyncio
@LATE_EVENT_PATHS
async def test_late_transition_origin_comes_from_the_unknown_factory(
    path: tuple[str, ...],
    event_type: EventType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The late writer must call the Group 3 ``unknown`` factory.

    Relying on the ``EventData`` default would also persist ``unknown``; the
    spy pins that the origin is the factory's explicit frozen context.
    """
    service, _store, events = _service()
    handoff_id, tenant_id, session_id = await _initiate_production(service, events)
    calls: list[dict[str, Any]] = []
    returned: list[ResolvedInteractionContext] = []

    def spy(**kwargs: Any) -> ResolvedInteractionContext:
        calls.append(kwargs)
        context = unknown_interaction_context(**kwargs)
        returned.append(context)
        return context

    monkeypatch.setattr(handoff_service_module, "unknown_interaction_context", spy)

    await _run_late_path(service, handoff_id, tenant_id, path)

    assert calls == [
        {
            "effective_tenant_id": tenant_id,
            "session_id": session_id,
            "event_type": event_type.value,
        }
    ]
    assert len(returned) == 1
    assert returned[0].origin is InteractionOrigin.UNKNOWN
    late = _only(events.appended, event_type)
    assert late.interaction_origin is returned[0].origin
    assert late.to_insert_dict()["interaction_origin"] == "unknown"


@pytest.mark.asyncio
@LATE_EVENT_PATHS
async def test_late_event_construction_error_is_a_post_commit_error(
    path: tuple[str, ...],
    event_type: EventType,
) -> None:
    """A late event that cannot be built is reported like any post-commit
    failure: the transition stays committed, ``HandoffPostCommitError`` wraps
    the cause and the ``update/error`` metric is incremented once."""
    service, store, events = _service()
    handoff_id, tenant_id, _session_id = await _initiate_production(service, events)
    *setup, last = path
    await _run_late_path(service, handoff_id, tenant_id, tuple(setup))
    record = store.records[handoff_id]
    record.session_id = "not-a-uuid"  # type: ignore[assignment]
    before = _update_error_count()

    with pytest.raises(HandoffPostCommitError) as excinfo:
        await _run_late_path(service, handoff_id, tenant_id, (last,))

    assert isinstance(excinfo.value.__cause__, TypeError)
    assert _update_error_count() == before + 1
    expected_status = {
        EventType.HANDOFF_ACCEPT: HandoffStatus.ACCEPTED,
        EventType.HANDOFF_COMPLETE: HandoffStatus.COMPLETED,
        EventType.HANDOFF_TIMEOUT: HandoffStatus.TIMEOUT,
    }[event_type]
    assert store.records[handoff_id].status == expected_status.value
    assert [event.event_type for event in events.appended] == [
        EventType.HANDOFF_REQUEST.value
    ]
    assert events.append_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["confirm", "confirm_queue", "cancel"])
async def test_confirm_and_cancel_still_write_no_event(operation: str) -> None:
    service, store, events = _service()
    handoff_id, tenant_id, _session_id = await _initiate_production(service, events)

    if operation == "confirm":
        await service.confirm_handoff(handoff_id, "agent-1", tenant_id=tenant_id)
        expected = HandoffStatus.IN_PROGRESS
    elif operation == "confirm_queue":
        await service.confirm_queue_handoff(handoff_id, "agent-1", tenant_id=tenant_id)
        expected = HandoffStatus.IN_PROGRESS
    else:
        await service.cancel_handoff(handoff_id, "gave up", tenant_id=tenant_id)
        expected = HandoffStatus.CANCELLED

    assert store.records[handoff_id].status == expected.value
    assert [event.event_type for event in events.appended] == [
        EventType.HANDOFF_REQUEST.value
    ]
    assert events.append_calls == 1


# ============================================
# Data model boundary
# ============================================


@pytest.mark.asyncio
async def test_record_gains_no_origin_column_or_context_key() -> None:
    columns = set(HandoffRecord.__table__.columns.keys())
    assert not {
        name for name in columns if "origin" in name or "interaction" in name
    }, columns

    service, store, events = _service()
    handoff_id, _tenant_id, _session_id = await _initiate_production(service, events)
    record = store.records[handoff_id]
    assert not hasattr(record, "interaction_origin")
    assert not {
        key for key in record.context if "origin" in key or "interaction" in key
    }, record.context
    assert "production" not in repr(record.context)


def test_handoff_request_fields_are_unchanged() -> None:
    assert set(HandoffRequest.model_fields) == HANDOFF_REQUEST_FIELDS


@pytest.mark.asyncio
async def test_legacy_constructors_and_calls_stay_compatible() -> None:
    store = _FakeHandoffStore()
    events = _FakeEventStore()
    positional = HandoffService(events, None, _dispatcher())
    assert positional.event_store is events
    assert isinstance(HandoffService(), HandoffService)

    service = _build_test_service(store, _dispatcher(), event_store=events)
    tenant_id, session_id = uuid4(), uuid4()
    request: Any = _request(tenant_id, session_id)
    response = await service.initiate_handoff(request)
    await service.accept_handoff(response.id, tenant_id=tenant_id)

    assert [event.interaction_origin for event in events.appended] == [
        InteractionOrigin.UNKNOWN,
        InteractionOrigin.UNKNOWN,
    ]
