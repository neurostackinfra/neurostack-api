"""
NeuroStack API - Events Endpoint
Main entry point for processing agent events/messages.

This endpoint receives events from builder agents and returns cognitive
decisions including intent classification, funnel stage, safety assessment,
and structured prompts for LLM response generation.

Implements Task 13.0 - End-to-End Integration.
"""

import json
from contextlib import suppress
from typing import Any, Optional
from uuid import UUID

import anyio
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
from src.config.logging import get_logger
from src.interaction_context import (
    ResolvedInteractionContext,
    build_interaction_context,
    classify_interaction_origin,
)
from src.repositories import get_db_session_dependency
from src.services import credit_metering, get_event_service
from src.services.credit_metering import (
    CREDIT_ENFORCEMENT_UNAVAILABLE_CODE,
    CREDIT_PENDING_CODE,
    CREDIT_SUSPENDED_CODE,
    DUPLICATE_EVENT_CODE,
    EVENT_FAILED_CODE,
    EVENT_IN_PROGRESS_CODE,
    IDEMPOTENCY_KEY_HEADER,
    IDEMPOTENCY_KEY_INVALID,
    IDEMPOTENCY_KEY_REQUIRED,
    PAYLOAD_CONFLICT_CODE,
    RETRY_AFTER_MAX_SECONDS,
    ClaimAnswer,
    CreditEnforcementUnavailable,
    CreditTenantGone,
    EventsEnforcement,
    record_events_decision,
)
from src.services.event_service import SessionNotFoundError
from src.services.handoff_service import (
    dispatch_committed_handoff_webhooks,
    record_handoff_post_commit_dispatch_failure,
)

router = APIRouter()

# Server-managed provenance. A client may never send it in the public
# ``EventRequest`` body, its ``payload`` or ``payload.metadata``.
RESERVED_INTERACTION_ORIGIN_FIELD = "interaction_origin"
INTERACTION_ORIGIN_SERVER_MANAGED = "interaction_origin_server_managed"


def _credit_code_schema(*codes: str) -> dict[str, Any]:
    """The body of a credit answer: ``{"code": ...}`` (top level)."""
    return {
        "type": "object",
        "required": ["code"],
        "properties": {"code": {"type": "string", "enum": list(codes)}},
    }


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
        "description": (
            "Invalid request payload; or, once credits are enforced, a "
            "production (API key) event without a usable `Idempotency-Key` "
            "(`idempotency_key_required` when absent, `idempotency_key_invalid` "
            "when empty, longer than 255 characters or not visible ASCII)"
        ),
        "content": {
            "application/json": {
                "schema": {
                    "oneOf": [
                        {"$ref": "#/components/schemas/ErrorResponse"},
                        _credit_code_schema(
                            IDEMPOTENCY_KEY_REQUIRED, IDEMPOTENCY_KEY_INVALID
                        ),
                    ]
                },
                "examples": {
                    "validation_error": {
                        "value": {
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
                    },
                    IDEMPOTENCY_KEY_REQUIRED: {
                        "value": {"code": IDEMPOTENCY_KEY_REQUIRED}
                    },
                    IDEMPOTENCY_KEY_INVALID: {
                        "value": {"code": IDEMPOTENCY_KEY_INVALID}
                    },
                },
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
    402: {
        "description": (
            "Credits are enforced and the clinic cannot be charged yet "
            "(`credit_pending`: a clinic without a builder account, or a "
            "builder whose payment is in quarantine). No LLM runs and nothing "
            "is recorded; the rate-limit slot stays consumed. "
            "`credit_suspended`: the builder's 72 hours of debt service after "
            "exhaustion are over (the agent is suspended); no LLM runs."
        ),
        "content": {
            "application/json": {
                "schema": _credit_code_schema(
                    CREDIT_PENDING_CODE, CREDIT_SUSPENDED_CODE
                ),
                "example": {"code": CREDIT_PENDING_CODE},
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
    409: {
        "description": (
            "Credits are enforced and this `Idempotency-Key` was already used: "
            "`payload_conflict` (a different event under the same key), "
            "`duplicate_event` (claimed before the enforcement, or its stored "
            "answer is past the replay retention), `event_in_progress` (being "
            "decided now; retry after `Retry-After` seconds, never more than "
            f"{RETRY_AFTER_MAX_SECONDS}) or `event_failed` (its decision "
            "started and can no longer finish; final, never re-executed). "
            "A decided key within the retention replays the original 200."
        ),
        "headers": {
            "Retry-After": {
                "description": (
                    "Only with `event_in_progress`: whole seconds, at most "
                    f"{RETRY_AFTER_MAX_SECONDS}."
                ),
                "schema": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": RETRY_AFTER_MAX_SECONDS,
                },
            }
        },
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["code"],
                    "properties": {
                        "code": {
                            "type": "string",
                            "enum": [
                                PAYLOAD_CONFLICT_CODE,
                                DUPLICATE_EVENT_CODE,
                                EVENT_IN_PROGRESS_CODE,
                                EVENT_FAILED_CODE,
                            ],
                        },
                        "interaction_id": {"type": "string", "format": "uuid"},
                    },
                },
                "examples": {
                    PAYLOAD_CONFLICT_CODE: {"value": {"code": PAYLOAD_CONFLICT_CODE}},
                    DUPLICATE_EVENT_CODE: {
                        "value": {
                            "code": DUPLICATE_EVENT_CODE,
                            "interaction_id": "e5f6a7b8-c9d0-1234-ef01-345678901234",
                        }
                    },
                    EVENT_IN_PROGRESS_CODE: {"value": {"code": EVENT_IN_PROGRESS_CODE}},
                    EVENT_FAILED_CODE: {
                        "value": {
                            "code": EVENT_FAILED_CODE,
                            "interaction_id": "e5f6a7b8-c9d0-1234-ef01-345678901234",
                        }
                    },
                },
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
    503: {
        "description": (
            "Credits are enforced and this server can no longer read the "
            "enforcement state (`credit_enforcement_unavailable`): a "
            "production (API key) event is refused rather than served free. "
            "Retry later; panel (JWT) callers are never refused."
        ),
        "content": {
            "application/json": {
                "schema": _credit_code_schema(CREDIT_ENFORCEMENT_UNAVAILABLE_CODE),
                "example": {"code": CREDIT_ENFORCEMENT_UNAVAILABLE_CODE},
            }
        },
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
        "parameters": [
            {
                "name": IDEMPOTENCY_KEY_HEADER,
                "in": "header",
                "required": False,
                "description": (
                    "Client-chosen key of this event (1 to 255 visible ASCII "
                    "characters). Optional today; once credits are enforced it "
                    "is REQUIRED for production (API key) callers -- absent: "
                    "400 `idempotency_key_required`; empty, too long or not "
                    "visible ASCII: 400 `idempotency_key_invalid` -- and a "
                    "retry with the same key never charges twice: it replays "
                    "the stored 200 or answers 409. Panel (JWT) callers never "
                    "need it."
                ),
                "schema": {"type": "string", "minLength": 1, "maxLength": 255},
            }
        ],
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
        ],
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

    # Credits (neurostack-credit-system Group 3b-1): read credit_enforcement_at
    # once, only for a production request that captured the switch on; when it
    # is set the request runs the enforced path end to end.  Otherwise this is
    # main's path below (and the Group 3a observational writer).
    try:
        enforcement = await credit_metering.capture_events_enforcement(
            http_request,
            db,
            tenant=tenant,
            canonical_request=canonical_request,
            context=context,
        )
    except CreditEnforcementUnavailable:
        # D1: this process saw the enforcement set (it is one-way) and can no
        # longer read it -- never main's free path; the slot stays consumed.
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"code": CREDIT_ENFORCEMENT_UNAVAILABLE_CODE},
        )
    if enforcement is not None:
        return await _process_enforced_event(
            enforcement,
            http_request=http_request,
            tenant=tenant,
            db=db,
            service=service,
            canonical_request=canonical_request,
            context=context,
        )

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
    # A phase-1 handoff is announced to the clinic only now that it is durable.
    # The announcement is best effort: the handoff and pause are committed, so
    # its failure is logged and counted and the patient still gets the reply.
    try:
        await dispatch_committed_handoff_webhooks(db)
    except Exception as e:
        record_handoff_post_commit_dispatch_failure(
            e,
            owner="events",
            tenant_id=canonical_request.tenant_id,
            session_id=canonical_request.session_id,
        )
    try:
        response = await service.finish_response(
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

    # Observational metering (neurostack-credit-system Group 3a): only when
    # this request captured the credit switch on at entry; after the route's
    # transaction ended; bounded and best-effort -- never changes the answer.
    await record_events_decision(
        http_request,
        db,
        canonical_request=canonical_request,
        context=context,
        response=response,
    )
    return response


# ============================================
# Enforced credits (neurostack-credit-system Group 3b-1)
# ============================================


class _StoredAnswerResponse(JSONResponse):
    """The 200 of an enforced decision -- the original and every replay.

    Serialized with sorted keys: the stored payload is JSONB on PostgreSQL,
    which keeps no key order, so this is what makes a replay byte-identical
    to the original answer (LOW-7).
    """

    def render(self, content: Any) -> bytes:
        return json.dumps(
            content,
            ensure_ascii=False,
            allow_nan=False,
            indent=None,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


def _claim_answer(answer: ClaimAnswer) -> JSONResponse:
    if answer.replay:
        return _StoredAnswerResponse(
            status_code=status.HTTP_200_OK, content=dict(answer.body)
        )
    headers = (
        None if answer.retry_after is None else {"Retry-After": str(answer.retry_after)}
    )
    return JSONResponse(
        status_code=answer.status, content=dict(answer.body), headers=headers
    )


async def _session_mismatch(
    http_request: Request, tenant: TenantContext, db: AsyncSession
) -> JSONResponse:
    """Main's 403 ``tenant_identity_mismatch`` -- the one answer that gives the
    rate-limit slot back."""
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


async def _process_enforced_event(
    enforcement: EventsEnforcement,
    *,
    http_request: Request,
    tenant: TenantContext,
    db: AsyncSession,
    service: Any,
    canonical_request: CanonicalEventRequest,
    context: ResolvedInteractionContext,
) -> JSONResponse:
    """``POST /v1/events`` once credits are enforced (production only).

    Answers in the pinned order (MEDIUM-1): 400 ``Idempotency-Key`` -> 403
    session ownership (plain read; the slot comes back) -> the claim's
    table (200 replay / 409) -> 402 ``credit_pending`` (nothing written, no
    LLM, the slot stays consumed) -> claim insert or takeover -> phase 1 ->
    LLM -> final transaction.  ``ensure_account`` and the claim are never
    written before the session ownership is proved.
    """
    if enforcement.key_problem is not None:
        return JSONResponse(status_code=400, content={"code": enforcement.key_problem})
    try:
        if not await credit_metering.session_in_tenant(db, enforcement):
            return await _session_mismatch(http_request, tenant, db)
        verdict = await credit_metering.read_events_claim(db, enforcement)
        if isinstance(verdict, ClaimAnswer):
            return _claim_answer(verdict)
        precheck = await credit_metering.precheck_events(db, enforcement)
        if precheck == "tenant_gone":
            return await _session_mismatch(http_request, tenant, db)
        if precheck == "credit_pending":
            return JSONResponse(status_code=402, content={"code": CREDIT_PENDING_CODE})
        if precheck == "credit_suspended":
            # Group 5: suspended after its 72 h of debt (no LLM, no claim).
            return JSONResponse(
                status_code=402, content={"code": CREDIT_SUSPENDED_CODE}
            )
        leased = await credit_metering.acquire_events_claim(
            db, enforcement, takeover=verdict
        )
    except CreditTenantGone:
        return await _session_mismatch(http_request, tenant, db)
    except Exception as error:
        # Every step above ran in its own short transaction, rolled back on
        # failure: nothing partial is left (no account, no claim).
        await db.rollback()
        get_logger(__name__).error(
            "Credit pre-check failed in enforced process_event (no LLM called)",
            tenant_id=str(canonical_request.tenant_id),
            session_id=str(canonical_request.session_id),
            error=str(error),
            error_type=type(error).__name__,
            exc_info=True,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Credit check failed",
        )
    if isinstance(leased, ClaimAnswer):
        return _claim_answer(leased)

    try:
        return await _run_enforced_decision(
            enforcement,
            leased,
            http_request=http_request,
            tenant=tenant,
            db=db,
            service=service,
            canonical_request=canonical_request,
            context=context,
        )
    finally:
        # The phase 1 handoff is announced only after the END of the final
        # transaction (commit or rollback), never holding it nor the lease;
        # best effort: a delivery error never changes the answer (MEDIUM-2).
        try:
            await dispatch_committed_handoff_webhooks(db)
        except Exception as error:
            record_handoff_post_commit_dispatch_failure(
                error,
                owner="events",
                tenant_id=canonical_request.tenant_id,
                session_id=canonical_request.session_id,
            )


async def _release_on_cancellation(
    db: AsyncSession,
    enforcement: EventsEnforcement,
    leased: credit_metering.ClaimLease,
    *,
    session_version: Optional[int],
) -> None:
    """LOW-1: a cancellation is a known failure too.  In this stack a client
    disconnect does not cancel the endpoint; a forced shutdown or a harness
    cancelling the request task does.  End the route's transaction and
    release the lease so the key is not blocked for the whole lease, inside
    a shielded cancel scope: Starlette's task groups re-deliver the
    cancellation at every await (level-triggered), and the shield lets the
    release run to its end once, without re-awaiting in a loop.  The caller
    re-raises.  Its own errors are only logged (by ``release_events_lease``).
    """

    with anyio.CancelScope(shield=True):
        with suppress(Exception):
            await db.rollback()
        await credit_metering.release_events_lease(
            db, enforcement, leased, session_version=session_version
        )


async def _run_enforced_decision(
    enforcement: EventsEnforcement,
    leased: credit_metering.ClaimLease,
    *,
    http_request: Request,
    tenant: TenantContext,
    db: AsyncSession,
    service: Any,
    canonical_request: CanonicalEventRequest,
    context: ResolvedInteractionContext,
) -> JSONResponse:
    logger = get_logger(__name__)
    log_fields = {
        "tenant_id": str(canonical_request.tenant_id),
        "session_id": str(canonical_request.session_id),
        "interaction_id": str(leased.interaction_id),
    }

    async def run_pipeline() -> Any:
        return await service.run_pipeline(
            canonical_request, session=db, context=context
        )

    # ── Phase 1 (route session; the marker is its last statement) ──────────
    try:
        phase1 = await credit_metering.run_enforced_phase1(
            db, enforcement, leased, run_pipeline=run_pipeline
        )
    except (SessionNotFoundError, CreditTenantGone):
        await db.rollback()
        await credit_metering.release_events_lease(
            db, enforcement, leased, session_version=None
        )
        return await _session_mismatch(http_request, tenant, db)
    except HTTPException:
        await db.rollback()
        await credit_metering.release_events_lease(
            db, enforcement, leased, session_version=None
        )
        raise
    except Exception as error:
        await db.rollback()
        await credit_metering.release_events_lease(
            db, enforcement, leased, session_version=None
        )
        logger.error(
            "Pipeline error in enforced process_event (phase 1 — no LLM called)",
            error=str(error),
            error_type=type(error).__name__,
            exc_info=True,
            **log_fields,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Pipeline processing failed",
        )
    except BaseException:  # cancelled: release before it propagates (LOW-1)
        await _release_on_cancellation(db, enforcement, leased, session_version=None)
        raise
    if phase1 is None:  # lost the phase 1 fence: everything rolled back
        return _claim_answer(await credit_metering.reread_events_claim(db, enforcement))

    # ── The LLM, outside every transaction ──────────────────────────────────
    try:
        generated = await service.generate_response(
            canonical_request,
            phase1.result,
            phase1.clinic_config,
            phase1.message_history,
            context=context,
        )
    except HTTPException:
        await credit_metering.release_events_lease(
            db, enforcement, leased, session_version=phase1.session_version
        )
        raise
    except Exception as error:
        await credit_metering.release_events_lease(
            db, enforcement, leased, session_version=phase1.session_version
        )
        logger.error(
            "Error in enforced process_event phase 2 (LLM)",
            error=str(error),
            error_type=type(error).__name__,
            exc_info=True,
            **log_fields,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Response generation failed",
        )
    except BaseException:  # cancelled: release before it propagates (LOW-1)
        await _release_on_cancellation(
            db, enforcement, leased, session_version=phase1.session_version
        )
        raise

    # ── Final transaction: bundle, effect, stored answer, fence ─────────────
    async def write_bundle(credit_effect_id: Optional[str]) -> Optional[UUID]:
        written: Optional[UUID] = await service.write_interaction_bundle(
            canonical_request,
            phase1.result,
            generated,
            session=db,
            context=context,
            credit_effect_id=credit_effect_id,
        )
        return written

    try:
        final = await credit_metering.run_enforced_final(
            db,
            enforcement,
            leased,
            phase1,
            response=generated.response,
            write_bundle=write_bundle,
        )
    except CreditTenantGone:
        await db.rollback()
        await credit_metering.release_events_lease(
            db, enforcement, leased, session_version=phase1.session_version
        )
        return await _session_mismatch(http_request, tenant, db)
    except Exception as error:
        await db.rollback()
        await credit_metering.release_events_lease(
            db, enforcement, leased, session_version=phase1.session_version
        )
        logger.error(
            "Final transaction failed in enforced process_event",
            error=str(error),
            error_type=type(error).__name__,
            exc_info=True,
            **log_fields,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Decision persistence failed",
        )
    except BaseException:  # cancelled: release before it propagates (LOW-1)
        await _release_on_cancellation(
            db, enforcement, leased, session_version=phase1.session_version
        )
        raise
    if final is None:  # lost the final fence: no bundle, no debit
        return _claim_answer(await credit_metering.reread_events_claim(db, enforcement))
    # The stored answer IS the answer (replay-identical, interaction_event_id
    # set only now that the bundle is committed).
    return _StoredAnswerResponse(
        status_code=status.HTTP_200_OK, content=dict(final.payload)
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
