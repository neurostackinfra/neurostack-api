"""Contracts for the trusted interaction context."""

from dataclasses import FrozenInstanceError, fields
from uuid import uuid4

import pytest

from src.core.interaction_context import InteractionOrigin, ResolvedInteractionContext


def test_interaction_origin_is_a_closed_string_contract() -> None:
    assert {origin.value for origin in InteractionOrigin} == {
        "production",
        "simulator",
        "unknown",
    }
    with pytest.raises(ValueError):
        InteractionOrigin("client-supplied")


def test_resolved_interaction_context_is_frozen_and_auth_free() -> None:
    context = ResolvedInteractionContext(
        effective_tenant_id=uuid4(),
        session_id=uuid4(),
        origin=InteractionOrigin.PRODUCTION,
        interaction_event_id=uuid4(),
    )

    with pytest.raises(FrozenInstanceError):
        context.origin = InteractionOrigin.UNKNOWN  # type: ignore[misc]

    assert {field.name for field in fields(context)} == {
        "effective_tenant_id",
        "session_id",
        "origin",
        "interaction_event_id",
    }


def test_interaction_event_id_is_explicitly_nullable() -> None:
    context = ResolvedInteractionContext(
        effective_tenant_id=uuid4(),
        session_id=uuid4(),
        origin=InteractionOrigin.UNKNOWN,
    )
    assert context.interaction_event_id is None
