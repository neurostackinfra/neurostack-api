"""Trusted, immutable context resolved at an interaction boundary.

This module is pure: it imports no route, middleware or service.  It owns the
three pieces every ingress shares:

* :func:`build_interaction_context` -- the only constructor.  It allocates
  ``interaction_event_id`` if and only if the event is ``message.in``.
* :func:`classify_interaction_origin` -- maps an authenticated tenant context
  to an :class:`InteractionOrigin` from its authentication attributes and JWT
  role only (never from ``channel``, body, metadata or headers).
* :func:`unknown_interaction_context` -- the ``unknown`` factory used whenever
  a caller has no proof of origin; it delegates to the constructor.

:func:`require_interaction_context` is the fail-closed check every consumer
runs before trusting a context it was handed.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Final
from uuid import UUID, uuid4


class InteractionOrigin(str, Enum):
    """Server-assigned provenance for an interaction and its events."""

    PRODUCTION = "production"
    SIMULATOR = "simulator"
    UNKNOWN = "unknown"


# The only event type that roots an interaction and receives an interaction ID.
MESSAGE_IN_EVENT_TYPE: Final = "message.in"

# JWT roles with an operational panel policy.  Mirrors the allowlist of the
# shared tenant resolver (``src.api.middleware.auth``); a test pins both sets.
SIMULATOR_JWT_ROLES: Final[frozenset[str]] = frozenset(
    {"clinic_admin", "builder", "super_admin"}
)


class InteractionContextError(RuntimeError):
    """A context was offered for a different call than the one it accompanies.

    Raised before any effect.  The message never carries the context values.
    """


@dataclass(frozen=True, slots=True)
class ResolvedInteractionContext:
    """Authorized identifiers and provenance shared by interaction consumers.

    Authentication material deliberately does not cross this boundary. Callers
    must resolve credentials, tenant ownership, and session membership before
    consuming the context.  Build it only through
    :func:`build_interaction_context` (or the ``unknown`` factory).
    """

    effective_tenant_id: UUID
    session_id: UUID
    origin: InteractionOrigin
    interaction_event_id: UUID | None = None


def _event_type_value(event_type: object) -> str:
    value = getattr(event_type, "value", event_type)
    if not isinstance(value, str):
        raise TypeError("event_type must be a string event type")
    return value


def build_interaction_context(
    *,
    effective_tenant_id: UUID,
    session_id: UUID,
    origin: InteractionOrigin,
    event_type: object,
) -> ResolvedInteractionContext:
    """Build the single context of one interaction.

    ``interaction_event_id`` is a pre-allocated UUID for a ``message.in``
    event and ``None`` for every other event type.  It is only a reservation:
    persistence is proved by the Event Store write, never by this value.
    """

    if not isinstance(effective_tenant_id, UUID) or not isinstance(session_id, UUID):
        raise TypeError("tenant and session identifiers must be UUIDs")
    if not isinstance(origin, InteractionOrigin):
        raise TypeError("origin must be an InteractionOrigin")
    is_root = _event_type_value(event_type) == MESSAGE_IN_EVENT_TYPE
    return ResolvedInteractionContext(
        effective_tenant_id=effective_tenant_id,
        session_id=session_id,
        origin=origin,
        interaction_event_id=uuid4() if is_root else None,
    )


def unknown_interaction_context(
    *,
    effective_tenant_id: UUID,
    session_id: UUID,
    event_type: object,
) -> ResolvedInteractionContext:
    """Context for a caller without proof of origin (``unknown``)."""

    return build_interaction_context(
        effective_tenant_id=effective_tenant_id,
        session_id=session_id,
        origin=InteractionOrigin.UNKNOWN,
        event_type=event_type,
    )


def classify_interaction_origin(tenant: object) -> InteractionOrigin:
    """Classify an authenticated tenant context.  Pure and never raises.

    Reads only ``authentication_mode``, ``tenant_id``, ``api_key`` and
    ``user_id`` (the inputs of ``identity_from_tenant_context``) plus the JWT
    ``role``.  A consistent API key or Meta HMAC context is ``production``; a
    consistent JWT with an operational role is ``simulator``; anything else,
    including a JWT outside the allowlist, is ``unknown``.
    """

    try:
        mode = getattr(tenant, "authentication_mode", None)
        tenant_id = getattr(tenant, "tenant_id", None)
        api_key = getattr(tenant, "api_key", "")
        user_id = getattr(tenant, "user_id", None)
        role = getattr(tenant, "role", None)
        if not isinstance(tenant_id, UUID):
            return InteractionOrigin.UNKNOWN
        if mode == "api_key" and api_key and user_id is None:
            return InteractionOrigin.PRODUCTION
        if mode == "jwt" and not api_key and isinstance(user_id, UUID):
            if isinstance(role, str) and role in SIMULATOR_JWT_ROLES:
                return InteractionOrigin.SIMULATOR
            return InteractionOrigin.UNKNOWN
        if mode == "meta_hmac" and not api_key and user_id is None:
            return InteractionOrigin.PRODUCTION
    except Exception:
        return InteractionOrigin.UNKNOWN
    return InteractionOrigin.UNKNOWN


def require_interaction_context(
    context: object,
    *,
    tenant_id: UUID | None,
    session_id: UUID | None,
    event_type: object | None = None,
) -> ResolvedInteractionContext:
    """Return ``context`` only if it is a real context for this call.

    Raises :class:`TypeError` for anything but an exact
    :class:`ResolvedInteractionContext` (a dict, JSON or look-alike is never
    trusted) and :class:`InteractionContextError` when the tenant or session
    differs from the call, or when ``event_type`` is given and the presence
    of ``interaction_event_id`` does not match it being ``message.in``.
    """

    if type(context) is not ResolvedInteractionContext:
        raise TypeError("context must be a ResolvedInteractionContext")
    if not isinstance(context.origin, InteractionOrigin):
        raise TypeError("context origin must be an InteractionOrigin")
    if context.effective_tenant_id != tenant_id or context.session_id != session_id:
        raise InteractionContextError("interaction context does not match the call")
    if event_type is not None:
        is_root = _event_type_value(event_type) == MESSAGE_IN_EVENT_TYPE
        if is_root != (context.interaction_event_id is not None):
            raise InteractionContextError(
                "interaction context does not match the event type"
            )
    return context


__all__ = [
    "MESSAGE_IN_EVENT_TYPE",
    "SIMULATOR_JWT_ROLES",
    "InteractionContextError",
    "InteractionOrigin",
    "ResolvedInteractionContext",
    "build_interaction_context",
    "classify_interaction_origin",
    "require_interaction_context",
    "unknown_interaction_context",
]
