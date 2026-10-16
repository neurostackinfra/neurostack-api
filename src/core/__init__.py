"""Core compatibility exports, loaded on demand to avoid import cycles."""

from importlib import import_module
from typing import Any

__all__ = [
    # Pipeline
    "CognitivePipeline",
    "PipelineResult",
    "get_cognitive_pipeline",
    "process_event",
    # Exceptions - Base
    "NeuroStackError",
    "ServiceError",
    "RepositoryError",
    "ValidationError",
    "ConfigurationError",
    # Exceptions - Service
    "HandoffServiceError",
    "HandoffNotFoundError",
    "HandoffConfigError",
    "KnowledgeServiceError",
    "EventServiceError",
    "SessionServiceError",
    "PolicyServiceError",
    # Exceptions - Repository
    "EventStoreError",
    "EventStoreConnectionError",
    "StateStoreError",
    "StateNotFoundError",
    "StateStoreConnectionError",
    # Exceptions - Webhook
    "WebhookError",
    "WebhookConfigError",
    "WebhookDeliveryError",
]

_EXPORTS = {
    name: ("pipeline", name)
    for name in (
        "CognitivePipeline",
        "PipelineResult",
        "get_cognitive_pipeline",
        "process_event",
    )
}
_EXPORTS.update(
    {
        name: ("exceptions", name)
        for name in (
            "NeuroStackError",
            "ServiceError",
            "RepositoryError",
            "ValidationError",
            "ConfigurationError",
            "HandoffServiceError",
            "HandoffNotFoundError",
            "HandoffConfigError",
            "KnowledgeServiceError",
            "EventServiceError",
            "SessionServiceError",
            "PolicyServiceError",
            "EventStoreError",
            "EventStoreConnectionError",
            "StateStoreError",
            "StateNotFoundError",
            "StateStoreConnectionError",
            "WebhookError",
            "WebhookConfigError",
            "WebhookDeliveryError",
        )
    }
)


def __getattr__(name: str) -> Any:
    """Load legacy core exports only when a caller requests them."""

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
