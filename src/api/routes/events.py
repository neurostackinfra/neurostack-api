"""
NeuroStack API - Events Endpoint
Main entry point for processing agent events/messages.

This endpoint receives events from builder agents and returns cognitive
decisions including intent classification, funnel stage, safety assessment,
and structured prompts for LLM response generation.

Implements Task 13.0 - End-to-End Integration.
"""

from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.middleware.auth import (
    TenantContext,
    get_tenant_context,
    resolve_effective_tenant,
)
from src.api.middleware.tenant_identity import (
    audit_tenant_identity_mismatch,
    tenant_identity_mismatch_response,
)
from src.api.schemas.base import ErrorResponse
from src.api.schemas.events import (
    CanonicalEventRequest,
    EventDetailResponse,
    EventRequest,
    EventResponse,
    EventType,
)
from src.interaction_context import (
    build_interaction_context,
    classify_interaction_origin,
)
from src.repositories import get_db_session_dependency
from src.services import get_event_service
from src.services.event_service import SessionNotFoundError

router = APIRouter()

# Server-managed provenance. A client may never send it in the public
# ``EventRequest`` body, its ``payload`` or ``payload.metadata``.
RESERVED_INTERACTION_ORIGIN_FIELD = "interaction_origin"
INTERACTION_ORIGIN_SERVER_MANAGED = "interaction_origin_server_managed"


def _client_sent_interaction_origin(raw_body: object) -> bool:
    """True when the raw JSON body carries the reserved origin field."""
    if not isinstance(raw_body, dict):
        return False
    if RESERVED_INTERACTION_ORIGIN_FIELD in raw_body:
        return True
    payload = raw_body.get("payload")
    if not isinstance(payload, dict):
        return False
    if RESERVED_INTERACTION_ORIGIN_FIELD in payload:
        return True
    metadata = payload.get("metadata")
    return isinstance(metadata, dict) and RESERVED_INTERACTION_ORIGIN_FIELD in metadata


# ============================================
# API Responses Documentation
# ============================================

RESPONSES_PROCESS_EVENT: dict[int | str, dict[str, Any]] = {
    200: {
        "description": "Event processed successfully",
        "model": EventResponse,
    },
    400: {
        "description": "Invalid request payload",
        "model": ErrorResponse,
        "content": {
            "application/json": {
                "example": {
                    "error": "validation_error",
                    "message": "Invalid request payload",
                    "details": [
                        {
                            "field": "payload.content",
                            "message": "Content is required",
                            "code": "value_error.missing",
                        }
                    ],
                }
            }
        },
    },
    401: {
        "description": "Authentication required",
        "model": ErrorResponse,
        "content": {
            "application/json": {
                "example": {
                    "error": "unauthorized",
                    "message": "Invalid or missing API key",
                }
            }
        },
    },
    403: {
        "description": (
            "Tenant identity assertion failed, or the JWT role has no "
            "operational policy (`caller_role_forbidden`)"
        ),
        "content": {
            "application/json": {
                "examples": {
                    "tenant_identity_mismatch": {
                        "value": {
                            "error": "tenant_identity_mismatch",
                            "message": "Tenant identity does not match the authenticated request",
                        }
                    },
                    "caller_role_forbidden": {
                        "value": {"detail": {"code": "caller_role_forbidden"}}
                    },
                }
            }
        },
    },
    404: {
        "description": "Tenant or session not found",
        "model": ErrorResponse,
        "content": {
            "application/json": {
                "example": {
                    "error": "not_found",
                    "message": "Session not found",
                }
            }
        },
    },
    422: {
        "description": (
            "Invalid body, or a client-sent `interaction_origin` "
            "(server-managed provenance)"
        ),
        "content": {
            "application/json": {
                "schema": {
                    "oneOf": [
                        {"$ref": "#/components/schemas/HTTPValidationError"},
                        {
                            "type": "object",
                            "required": ["detail"],
                            "properties": {
                                "detail": {
                                    "type": "object",
                                    "required": ["code"],
                                    "properties": {
                                        "code": {
                                            "type": "string",
                                            "enum": [INTERACTION_ORIGIN_SERVER_MANAGED],
                                        }
                                    },
                                }
                            },
                        },
                    ]
                },
                "examples": {
                    "validation_error": {
                        "value": {
                            "detail": [
                                {
                                    "loc": ["body", "event_type"],
                                    "msg": "Field required",
                                    "type": "missing",
                                }
                            ]
                        }
                    },
                    INTERACTION_ORIGIN_SERVER_MANAGED: {
                        "value": {"detail": {"code": INTERACTION_ORIGIN_SERVER_MANAGED}}
                    },
                },
            }
        },
    },
    429: {
        "description": "Rate limit exceeded",
        "model": ErrorResponse,
        "content": {
            "application/json": {
                "example": {
                    "error": "rate_limited",
                    "message": "Rate limit exceeded. Try again in 60 seconds.",
                }
            }
        },
    },
    500: {
        "description": "Internal server error",
        "model": ErrorResponse,
    },
}


# ============================================
# Endpoints
# ============================================


@router.post(
    "",
    response_model=EventResponse,
    status_code=status.HTTP_200_OK,
    summary="Process Event",
    description="""
Process an incoming event through the NeuroStack cognitive pipeline.

This is the **main entry point** for all agent interactions. Send user messages
here to receive structured cognitive decisions.

## Flow

1. **Load Session State**: Retrieve current session from Memory Store
2. **Apply Policies**: Merge global + tenant policies
3. **SRM Decision**: Classify intent, detect funnel stage, assess safety
4. **CTX Context**: Build context with history and relevant information
5. **Prompt Engine**: Render structured prompt for LLM
6. **Return Decision**: Complete cognitive decision + prompt

## Event Types

- `message.in`: Incoming message from user (most common)
- `session.start`: Explicit session start event
- `handoff.complete`: Human handoff completed notification

## Response Modes

- `normal`: Standard conversational flow
- `rag`: Knowledge retrieval required
- `safety`: Safety-constrained response
- `handoff`: Human handoff initiated
    """,
    responses=RESPONSES_PROCESS_EVENT,
    tags=["Events"],
    openapi_extra={
        "x-code-samples": [
            {
                "lang": "curl",
                "label": "cURL",
                "source": """curl -X POST https://api.neurostack.ai/v1/events \\
  -H "Content-Type: application/json" \\
  -H "X-API-Key: your-api-key" \\
  -d '{
    "session_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
    "event_type": "message.in",
    "payload": {
      "content": "Olá, gostaria de agendar uma consulta",
      "channel": "whatsapp",
      "user_id": "user-xyz-123"
    }
  }'""",
            },
            {
                "lang": "python",
                "label": "Python",
                "source": """import requests

response = requests.post(
    "https://api.neurostack.ai/v1/events",
    headers={
        "Content-Type": "application/json",
        "X-API-Key": "your-api-key",
    },
    json={
        "session_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
        "event_type": "message.in",
        "payload": {
            "content": "Olá, gostaria de agendar uma consulta",
            "channel": "whatsapp",
            "user_id": "user-xyz-123",
        },
    },
)

decision = response.json()
# decision["decision"]["intent"]  # scheduling_request
# decision["prompt"]["system"]     # System prompt for LLM
""",
            },
        ]
    },
)
async def process_event(
    request: EventRequest,
    http_request: Request,
    tenant: TenantContext = Depends(get_tenant_context),
    db: AsyncSession = Depends(get_db_session_dependency),
) -> EventResponse | JSONResponse:
    """
    Process an incoming event through the NeuroStack cognitive pipeline.

    This is the main entry point for all agent interactions.
    """
    from src.config.logging import get_logger as _get_logger

    _logger = _get_logger(__name__)

    # Fixed error order (#68 contract kept; the tenant assertion already ran in
    # TenantIdentityMiddleware): JWT role allowlist (403 caller_role_forbidden,
    # the rate-limit slot stays consumed) → reserved origin field (422) →
    # session proved by the tenant-scoped lock (403 tenant_identity_mismatch).
    effective_tenant_id = await resolve_effective_tenant(http_request, tenant, None, db)

    if _client_sent_interaction_origin(await http_request.json()):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={"code": INTERACTION_ORIGIN_SERVER_MANAGED},
        )

    # TenantIdentityMiddleware treated the optional payload value as an
    # assertion. Canonicalize the model before any service/store receives it.
    canonical_request = CanonicalEventRequest(
        tenant_id=effective_tenant_id,
        session_id=request.session_id,
        event_type=request.event_type,
        payload=request.payload,
    )
    # The interaction's single context: built once here, consumed only after
    # run_pipeline's lock proves the session inside the tenant.
    context = build_interaction_context(
        effective_tenant_id=effective_tenant_id,
        session_id=request.session_id,
        origin=classify_interaction_origin(tenant),
        event_type=request.event_type,
    )

    service = get_event_service()

    # ── Phase 1: fast DB work (load state → pipeline → flush state) ──────────
    # Any exception here rolls back the transaction so nothing is left dirty.
    try:
        result, clinic_config, message_history = await service.run_pipeline(
            canonical_request, session=db, context=context
        )
    except SessionNotFoundError:
        await db.rollback()
        http_request.state.rate_limit_refund = True
        audit_tenant_identity_mismatch(
            request_id=getattr(http_request.state, "request_id", None),
            route=http_request.url.path,
            method=http_request.method,
            authentication_mode=tenant.authentication_mode,
            authenticated_tenant=tenant.tenant_id,
            claimed_tenant=None,
        )
        return tenant_identity_mismatch_response()
    except HTTPException:
        await db.rollback()
        raise
    except Exception as e:
        await db.rollback()
        _logger.error(
            "Pipeline error in process_event (phase 1 — no LLM called)",
            tenant_id=str(canonical_request.tenant_id),
            session_id=str(canonical_request.session_id),
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Pipeline processing failed",
        )

    # ── Early commit: release the DB connection before the slow LLM call ─────
    # Overlapping /v1/events calls for the same session now read the updated
    # cognitive_state.  If commit fails, abort before the LLM call runs.
    try:
        await db.commit()
    except Exception as e:
        await db.rollback()
        _logger.error(
            "DB commit failed after state flush (phase 1 commit)",
            tenant_id=str(canonical_request.tenant_id),
            session_id=str(canonical_request.session_id),
            error=str(e),
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="State persistence failed",
        )

    # ── Phase 2: slow I/O (LLM + event-store) ────────────────────────────────
    # DB session is already committed/released; failures here are independent.
    try:
        return await service.finish_response(
            canonical_request,
            result,
            clinic_config,
            message_history,
            context=context,
        )
    except HTTPException:
        raise
    except Exception as e:
        _logger.error(
            "Error in process_event phase 2 (LLM/event-store)",
            tenant_id=str(canonical_request.tenant_id),
            session_id=str(canonical_request.session_id),
            error=str(e),
            error_type=type(e).__name__,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Response generation failed",
        )


@router.get(
    "/{event_id}",
    response_model=EventDetailResponse,
    summary="Get Event Details",
    description="""
Retrieve details of a previously processed event by ID.

Use this endpoint to:
- Debug event processing
- Review SRM decisions
- Audit conversation history

Events are stored in the Event Store and retained according to tenant configuration.
    """,
    responses={
        200: {
            "description": "Event details retrieved successfully",
            "model": EventDetailResponse,
        },
        404: {
            "description": "Event not found",
            "model": ErrorResponse,
            "content": {
                "application/json": {
                    "example": {
                        "error": "not_found",
                        "message": "Event not found",
                    }
                }
            },
        },
    },
    tags=["Events"],
)
async def get_event(
    event_id: UUID,
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> EventDetailResponse:
    """
    Retrieve details of a previously processed event.
    """
    service = get_event_service()
    try:
        return await service.get_event(tenant.tenant_id, event_id, session)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e),
        )


@router.get(
    "",
    response_model=list[EventDetailResponse],
    summary="List Events",
    description="""
List events with optional filtering by tenant, session, or event type.

Supports pagination and filtering for efficient querying of the Event Store.
    """,
    responses={
        200: {
            "description": "Events listed successfully",
        },
    },
    tags=["Events"],
)
async def list_events(
    tenant_id: UUID = Query(..., description="Filter by tenant ID"),
    session_id: Optional[UUID] = Query(
        default=None, description="Filter by session ID"
    ),
    event_type: Optional[EventType] = Query(
        default=None, description="Filter by event type"
    ),
    limit: int = Query(
        default=50, ge=1, le=100, description="Maximum events to return"
    ),
    offset: int = Query(default=0, ge=0, description="Offset for pagination"),
    tenant: TenantContext = Depends(get_tenant_context),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> list[EventDetailResponse]:
    """
    List events with filtering and pagination.
    """
    if tenant_id != tenant.tenant_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Tenant or session not found",
        )
    service = get_event_service()
    return await service.list_events(
        tenant_id=tenant_id,
        session=session,
        session_id=session_id,
        event_type=event_type,
        limit=limit,
        offset=offset,
    )
