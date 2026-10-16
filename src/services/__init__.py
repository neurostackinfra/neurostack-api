"""Business logic services with lazy compatibility exports."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.services.event_service import EventService, get_event_service
    from src.services.handoff_service import (
        HandoffService,
        close_handoff_service,
        get_handoff_service,
    )
    from src.services.knowledge_service import (
        KnowledgeService,
        MockKnowledgeService,
        get_knowledge_service,
    )
    from src.services.session_service import SessionService, get_session_service

__all__ = [
    "EventService",
    "get_event_service",
    "HandoffService",
    "get_handoff_service",
    "close_handoff_service",
    "KnowledgeService",
    "MockKnowledgeService",
    "get_knowledge_service",
    "SessionService",
    "get_session_service",
]

_EXPORTS = {
    "EventService": ("event_service", "EventService"),
    "get_event_service": ("event_service", "get_event_service"),
    "HandoffService": ("handoff_service", "HandoffService"),
    "get_handoff_service": ("handoff_service", "get_handoff_service"),
    "close_handoff_service": ("handoff_service", "close_handoff_service"),
    "KnowledgeService": ("knowledge_service", "KnowledgeService"),
    "MockKnowledgeService": ("knowledge_service", "MockKnowledgeService"),
    "get_knowledge_service": ("knowledge_service", "get_knowledge_service"),
    "SessionService": ("session_service", "SessionService"),
    "get_session_service": ("session_service", "get_session_service"),
}


def __getattr__(name: str) -> Any:
    """Load legacy service exports only when a caller requests them."""

    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from error
    value = getattr(import_module(f"{__name__}.{module_name}"), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted((*globals(), *_EXPORTS))
