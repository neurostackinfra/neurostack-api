"""Trusted, immutable context resolved at an interaction boundary."""

from dataclasses import dataclass
from enum import Enum
from uuid import UUID


class InteractionOrigin(str, Enum):
    """Server-assigned provenance for an interaction and its events."""

    PRODUCTION = "production"
    SIMULATOR = "simulator"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ResolvedInteractionContext:
    """Authorized identifiers and provenance shared by interaction consumers.

    Authentication material deliberately does not cross this boundary. Callers
    must resolve credentials, tenant ownership, and session membership before
    constructing the context.
    """

    effective_tenant_id: UUID
    session_id: UUID
    origin: InteractionOrigin
    interaction_event_id: UUID | None = None
