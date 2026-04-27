"""
NeuroStack API - Events Endpoint
Main entry point for processing agent events/messages.

This endpoint receives events from builder agents and returns cognitive
decisions including intent classification, funnel stage, safety assessment,
and structured prompts for LLM response generation.

Implements Task 13.0 - End-to-End Integration.
"""

from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas.base import ErrorResponse
from src.api.schemas.events import (
    EventDetailResponse,
    EventRequest,
    EventResponse,
    EventType,
)
from src.repositories import get_db_session_dependency
from src.services import EventService, get_event_service

router = APIRouter()


# ============================================
# API Responses Documentation
# ============================================

RESPONSES_PROCESS_EVENT = {
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
    "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
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
        "tenant_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
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
async def process_event(request: EventRequest) -> EventResponse:
    """
    Process an incoming event through the NeuroStack cognitive pipeline.

    This is the main entry point for all agent interactions.
    """
    service = get_event_service()
    return await service.process_event(request)


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
    session: AsyncSession = Depends(get_db_session_dependency),
) -> EventDetailResponse:
    """
    Retrieve details of a previously processed event.
    """
    service = get_event_service()
    try:
        return await service.get_event(event_id, session)
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
    session_id: Optional[UUID] = Query(default=None, description="Filter by session ID"),
    event_type: Optional[EventType] = Query(default=None, description="Filter by event type"),
    limit: int = Query(default=50, ge=1, le=100, description="Maximum events to return"),
    offset: int = Query(default=0, ge=0, description="Offset for pagination"),
    session: AsyncSession = Depends(get_db_session_dependency),
) -> list[EventDetailResponse]:
    """
    List events with filtering and pagination.
    """
    service = get_event_service()
    return await service.list_events(
        tenant_id=tenant_id,
        session=session,
        session_id=session_id,
        event_type=event_type,
        limit=limit,
        offset=offset,
    )
